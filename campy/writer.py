"""
Video writer: one ffmpeg process per camera, fed raw frames through a pipe.

EncoderArgs() turns a camera's parameters into ffmpeg encoder settings, and
OpenWriter() starts ffmpeg with them. campy-encode-sweep (campy/sweep.py)
calls EncoderArgs() too, so the capacity it measures is for the same ffmpeg
command a recording runs.
"""
from imageio_ffmpeg import write_frames
import os, sys, time, logging
from campy.utils.utils import QueueKeyboardInterrupt, Say


def EncoderArgs(cam_params):
	"""
	ffmpeg encoder settings for one camera, as a dict:
	  codec, pix_fmt_out, input_params, output_params, macro_block_size
	  key: short name of the encoder configuration ("libx264/fast",
	       "h264_vaapi", ...). The capacity table is indexed by it.
	  device: "cpu" or "gpu"
	  env: environment variables ffmpeg needs (OpenWriter sets them)
	"""
	# Load encoding parameters from cam_params
	pix_fmt_out = cam_params["pixelFormatOutput"]
	codec = str(cam_params["codec"])
	# quality is a CRF for libx264/libx265 (adapts to the picture), a constant
	# quantizer for NVENC, VA-API and AMF (spends bits on sensor noise too) and
	# -q for QSV, so one number gives very different files on different
	# encoders. For AV1 it is the quantizer index, 0-255, on every GPU path.
	# campy-encode-quality measures what each setting costs.
	quality = str(cam_params["quality"])
	preset = str(cam_params["preset"])
	gpuID = str(cam_params["gpuID"])

	# Load defaults
	input_params = ["-an"] # "-an" no audio
	macro_block_size = 16 # imageio-ffmpeg pads frames up to a multiple of this
	gpu_params = []
	key_preset = None
	env = {}

	# CPU compression
	if cam_params["gpuID"] == -1:
		device = "cpu"
		if preset == "None":
			preset = "fast"
		key_preset = preset
		gpu_params = [
			"-preset", preset,
			"-tune", "fastdecode",
			"-crf", quality,
			"-bufsize", "20M",
			"-maxrate", "10M",
			"-bf:v", "4",
			]
		if pix_fmt_out == "rgb0" or pix_fmt_out == "bgr0":
			pix_fmt_out = "yuv420p"
		if cam_params["codec"] == "h264":
			codec = "libx264"
			gpu_params.append("-x264-params")
			gpu_params.append("nal-hrd=cbr")
		elif cam_params["codec"] == "h265":
			codec = "libx265"

	# GPU compression
	else:
		device = "gpu"
		# Nvidia GPU (NVENC) encoder optimized parameters
		if cam_params["gpuMake"] == "nvidia":
			if preset == "None":
				preset = "fast"
			key_preset = preset
			gpu_params = [
				"-preset", preset, # "fast", or p1 (fastest) to p7 from ffmpeg 4.4; ffmpeg 9 dropped "llhp" and "llhq"
				"-qp", quality,
				"-bf:v", "0",
				"-gpu", gpuID,
				]
			if cam_params["codec"] == "h264":
				codec = "h264_nvenc"
			elif cam_params["codec"] == "h265":
				codec = "hevc_nvenc"
			elif cam_params["codec"] == "av1":
				codec = "av1_nvenc" # Ada (RTX 40) and newer; -qp is then AV1's quantizer index

		# AMD GPU (AMF/VCE) encoder optimized parameters. On Linux, AMF needs
		# AMD's proprietary runtime; "vaapi" below uses the open Mesa driver.
		elif cam_params["gpuMake"] == "amd":
			# Preset not supported by AMF
			gpu_params = [
				"-usage", "lowlatency",
				"-rc", "cqp", # constant quantization parameter
				"-qp_i", quality,
				"-qp_p", quality,
				"-qp_b", quality,
				"-bf:v", "0",
				"-hwaccel_device", gpuID,]
			if pix_fmt_out == "rgb0" or pix_fmt_out == "bgr0":
				pix_fmt_out = "yuv420p"
			if cam_params["codec"] == "h264":
				codec = "h264_amf"
			elif cam_params["codec"] == "h265":
				codec = "hevc_amf"

		# Intel iGPU encoder (Quick Sync) optimized parameters
		elif cam_params["gpuMake"] == "intel":
			if preset == "None":
				preset = "faster"
			key_preset = preset
			# QSV's constant quantizer: quality + 1 for H.264 and H.265, as campy
			# always passed it; AV1's quantizer index (0-255) as it is, as on
			# NVENC and VA-API.
			gpu_params = [
					"-bf:v", "0",
					"-preset", preset,
					"-q", quality if cam_params["codec"] == "av1" else str(int(quality)+1),]
			# Quick Sync encodes 8-bit video from NV12 only: 4:2:0 like yuv420p,
			# with the two chroma planes interleaved. Asked for yuv420p, ffmpeg
			# switches to NV12 by itself and warns "Incompatible pixel format".
			if pix_fmt_out in ("rgb0", "bgr0", "yuv420p"):
				pix_fmt_out = "nv12"
			if cam_params["codec"] == "h264":
				codec = "h264_qsv"
			elif cam_params["codec"] == "h265":
				codec = "hevc_qsv"
			elif cam_params["codec"] == "av1":
				codec = "av1_qsv" # Arc GPUs and Core Ultra iGPUs

		# Linux VA-API: AMD GPUs through Mesa, Intel through its media driver.
		# The encoder takes NV12 surfaces, so ffmpeg converts each frame to NV12
		# on the CPU and uploads it to vaapiDevice. Frames are not padded: the
		# encoder handles sizes that are not a multiple of 16 itself.
		elif cam_params["gpuMake"] == "vaapi":
			input_params += [
				"-init_hw_device", "vaapi=va:{}".format(cam_params["vaapiDevice"]),
				"-filter_hw_device", "va",
				]
			# Constant quantization parameter. AV1 takes its quantizer index
			# (0-255, against H.264's and H.265's QP of 0-51) from -global_quality:
			# its VA-API encoder ignores -qp.
			quantizer = ["-global_quality" if cam_params["codec"] == "av1" else "-qp", quality]
			gpu_params = ["-vf", "format=nv12,hwupload", "-rc_mode", "CQP"] + quantizer + ["-bf", "0"]
			pix_fmt_out = "vaapi" # the encoder's input is the uploaded surface
			macro_block_size = 1
			# Mesa's AMD driver keeps video surfaces tiled, so every uploaded frame
			# is copied into place by a shader on the GPU's compute engine. On a
			# Ryzen AI Max (Strix Halo) that engine misses interrupts ("amdgpu:
			# Fence fallback timer expired on ring comp_..." in the kernel log), and
			# each miss stalls one camera's encoder for half a second: 20-70 times
			# an hour with 8 cameras. Linear surfaces take the frame straight from
			# the CPU, so the compute engine does nothing and the video is the same.
			# Intel's driver ignores AMD_DEBUG.
			if cam_params.get("vaapiLinearSurfaces", True):
				env["AMD_DEBUG"] = "novideotiling"
			if cam_params["codec"] == "h264":
				codec = "h264_vaapi"
			elif cam_params["codec"] == "h265":
				codec = "hevc_vaapi"
			elif cam_params["codec"] == "av1":
				codec = "av1_vaapi"

	# Keyframe (I-frame) interval in seconds, for any encoder. "None" keeps the
	# encoder's default (NVENC: a keyframe every 250 frames, ~4 s at 60 fps).
	# Every decoded frame depends on the frames back to its keyframe, so shorter
	# intervals make seeking and sparse frame sampling much cheaper (0.5 s: what
	# LUCID's image ID-switch check needs to decode only keyframes) at the cost of
	# larger files (~+50% for HEVC at a fixed QP).
	# imageio-ffmpeg rescales a frame whose sides are not multiples of
	# macro_block_size, which would stretch a RatCam's 160x120 to 160x128.
	# Even sides are all 4:2:0 needs, and every RatCam size is even.
	if cam_params.get("cameraMake") == "ratcam" and macro_block_size > 1:
		macro_block_size = 2

	keyframe_interval = str(cam_params.get("keyframeInterval", "None"))
	if keyframe_interval != "None":
		gop = max(1, int(round(float(cam_params["frameRate"]) * float(keyframe_interval))))
		gpu_params.extend(["-g", str(gop)])

	# Color range (colorRange). "limited" is video's usual range, 16-235, and
	# what campy has always written: ffmpeg rescales every pixel of a camera's
	# 0-255 frame into it. "full" keeps 0-255: a mono frame goes into the video
	# as it is, with all 256 of its gray levels, ffmpeg does less work (Quick
	# Sync H.265 at 1280x1024: 1.7 ms of CPU a frame instead of 3.3), and the
	# video is marked full range, so readers decode the camera's own levels
	# back. The same quality number then spends ~20% more bits, on the extra
	# levels: for the same file size, raise it by about 2. Only where ffmpeg
	# makes the YUV frames: NVENC given RGB (pixelFormatOutput "rgb0") converts
	# them itself, to limited range.
	if FullRange(cam_params) and pix_fmt_out not in RGB_FORMATS:
		if pix_fmt_out == "vaapi":
			vf = gpu_params.index("-vf") + 1
			gpu_params[vf] = "scale=out_range=full," + gpu_params[vf]
		else:
			# imageio-ffmpeg pads a frame to macro_block_size with a -vf of its
			# own, which this one would replace (ffmpeg keeps the last -vf), so
			# the padding is done here instead
			size = PaddedSize(cam_params, macro_block_size)
			gpu_params.extend(["-vf", "scale=out_range=full" if size is None else
				"scale={}:{}:out_range=full".format(*size)])
			macro_block_size = 1
		gpu_params.extend(["-color_range", "pc"])

	return {
		"codec": codec,
		"pix_fmt_out": pix_fmt_out,
		"input_params": input_params,
		"output_params": gpu_params,
		"macro_block_size": macro_block_size,
		"key": codec if key_preset is None else "{}/{}".format(codec, key_preset),
		"device": device,
		"env": env,
		}


RGB_FORMATS = ("rgb0", "bgr0", "rgb24", "bgr24", "rgba", "bgra")


def FullRange(cam_params):
	return str(cam_params.get("colorRange", "limited")).lower() == "full"


def PaddedSize(cam_params, macro_block_size):
	# The size imageio-ffmpeg scales a frame up to, so that its sides are
	# multiples of macro_block_size; None when they already are
	width, height = int(cam_params["frameWidth"]), int(cam_params["frameHeight"])
	if macro_block_size <= 1 or (width % macro_block_size == 0 and height % macro_block_size == 0):
		return None
	return width + -width % macro_block_size, height + -height % macro_block_size


def SetEnvironment(env):
	# imageio-ffmpeg starts ffmpeg with this process's environment and takes no
	# env of its own. Each camera writes from its own process, so this reaches
	# only that camera's ffmpeg. AMD_DEBUG is a comma-separated list of Mesa
	# flags: add to it rather than replace what the user set.
	for key, value in env.items():
		if key == "AMD_DEBUG":
			flags = [f for f in os.environ.get(key, "").split(",") if f]
			value = ",".join(flags + [f for f in value.split(",") if f not in flags])
		if os.environ.get(key) != value:
			os.environ[key] = value


def OpenWriter(cam_params, queue):
	try:
		writing = False
		folder_name = os.path.join(cam_params["videoFolder"], cam_params["cameraName"])
		file_name = cam_params["videoFilename"]
		full_file_name = os.path.join(folder_name, file_name)

		if not os.path.isdir(folder_name):
			os.makedirs(folder_name, exist_ok=True) # the grab thread may make it too (metadata.csv)
			Say("Made directory {}.".format(folder_name))

		# Flip blue and red for flir camera input
		if cam_params["pixelFormatInput"] == "bayer_bggr8" and cam_params["cameraMake"] == "flir":
			cam_params["pixelFormatInput"] == "bayer_rggb8"

		enc = EncoderArgs(cam_params)
		SetEnvironment(enc["env"])
		if enc["device"] == "cpu":
			Say("Opened: {} using CPU to compress the stream.".format(full_file_name))
		else:
			Say("Opened: {} using GPU {} to compress the stream.".format(full_file_name, cam_params["gpuID"]))
		if FullRange(cam_params) and "-color_range" not in enc["output_params"]:
			Say("{}: colorRange \"full\" does nothing with pixelFormatOutput \"{}\": {} converts RGB to limited-range "
				"video itself. Use pixelFormatOutput \"yuv420p\" for full range.".format(
				cam_params["cameraName"], enc["pix_fmt_out"], enc["codec"]))

	except Exception as e:
		logging.error("Caught exception at writer.py OpenWriter: {}".format(e))
		raise

	# Initialize writer object (imageio-ffmpeg)
	while(True):
		try:
			writer = write_frames(
				full_file_name,
				[cam_params["frameWidth"], cam_params["frameHeight"]], # size [W,H]
				fps=cam_params["frameRate"],
				quality=None,
				codec=enc["codec"],
				pix_fmt_in=cam_params["pixelFormatInput"], # "bayer_bggr8", "gray", "rgb24", "bgr0", "yuv420p"
				pix_fmt_out=enc["pix_fmt_out"],
				bitrate=None,
				ffmpeg_log_level=cam_params["ffmpegLogLevel"], # "warning", "quiet", "info"
				input_params=enc["input_params"],
				output_params=enc["output_params"],
				macro_block_size=enc["macro_block_size"],
				)
			writer.send(None) # Initialize the generator
			writing = True
			break

		except Exception as e:
			logging.error("Caught exception at writer.py OpenWriter: {}".format(e))
			raise
			break

	# Initialize read queue object to signal interrupt
	readQueue = {}
	readQueue["queue"] = queue
	readQueue["message"] = "STOP"

	return writer, writing, readQueue

def WriteFrames(cam_params, writeQueue, stopReadQueue, stopWriteQueue):
	# Start ffmpeg video writer
	writer, writing, readQueue = OpenWriter(cam_params, stopReadQueue)

	with QueueKeyboardInterrupt(readQueue):
		# Write until interrupted and/or stop message received
		while(writing):
			if writeQueue:
				writer.send(writeQueue.popleft())
			else:
				# Once queue is depleted and grabber stops, then stop writing
				if stopWriteQueue:
					writing = False
				# Otherwise continue writing
				time.sleep(0.01)

	# Close up...
	Say("Closing video writer for {}. Please wait...".format(cam_params["cameraName"]))
	time.sleep(1)
	writer.close()


