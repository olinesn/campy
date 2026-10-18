"""
Video writer: one ffmpeg process per camera, fed raw frames through a pipe.

EncoderArgs() turns a camera's parameters into ffmpeg encoder settings, and
OpenWriter() starts ffmpeg with them. campy-encode-sweep (campy/sweep.py)
calls EncoderArgs() too, so the capacity it measures is for the same ffmpeg
command a recording runs.
"""
from imageio_ffmpeg import write_frames
import os, re, sys, time, logging, functools, subprocess
from campy.cameras import unicam
from campy.utils.utils import QueueKeyboardInterrupt, Say, ErrorText, ExitCodeText


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
		# ffmpeg before 8 converts gray frames to full-range NV12, what Quick
		# Sync and VA-API take, with both chroma planes 0 instead of 128: the
		# gray levels are right, but a reader that shows the video in colour
		# shows it green (MATLAB, QuickTime, Chrome: black as 28, white as
		# 109). Its full-range yuv420p is right, so the frames go through that,
		# then to NV12 with the range given on both sides (6.1 squeezes it back
		# to limited range otherwise). That costs 0.2-0.5 ms of CPU a 1280x1024
		# frame, so only where it is needed: 6.1, 7.0 and 7.1 do this; 8.0 and
		# 8.1 do not, and RGB and Bayer frames get their chroma right.
		to_nv12 = []
		if (pix_fmt_out in ("nv12", "vaapi") and cam_params.get("pixelFormatInput") == "gray"
				and (FfmpegVersion() or (0,)) < (8,)):
			to_nv12 = ["format=yuv420p", "scale=in_range=full:out_range=full"]
		if pix_fmt_out == "vaapi":
			vf = gpu_params.index("-vf") + 1
			gpu_params[vf] = ",".join(["scale=out_range=full"] + to_nv12 + [gpu_params[vf]])
		else:
			# imageio-ffmpeg pads a frame to macro_block_size with a -vf of its
			# own, which this one would replace (ffmpeg keeps the last -vf), so
			# the padding is done here instead
			size = PaddedSize(cam_params, macro_block_size)
			scale = "scale=out_range=full" if size is None else "scale={}:{}:out_range=full".format(*size)
			gpu_params.extend(["-vf", ",".join([scale] + to_nv12)])
			macro_block_size = 1
		gpu_params.extend(["-color_range", "pc"])

	# Fragmented MP4 (fragmentedVideo, on by default), for .mp4 and .mov. A
	# plain MP4 keeps its index (the moov atom) in memory and writes it when
	# ffmpeg closes the file: if ffmpeg stops first (a power cut, an OS crash,
	# ffmpeg killed), none of the video can be read, however long it ran. A
	# fragmented MP4 indexes each fragment as it writes it, and stays readable
	# up to the last one. ffmpeg kill -9'd 4-20 s into a 1280x1024, 80 fps
	# recording (libx264, preset fast): the plain MP4 had no readable frame;
	# the fragmented one lost its last 0.7-1.5 s (0.9 s with keyframeInterval
	# 0.5), 0.7 s of which was still inside the encoder (55 frames).
	# - frag_keyframe starts a fragment at each keyframe, so with keyframeInterval
	#   up to 1 s every fragment starts with one, as fragmented MP4s usually do;
	#   -frag_duration starts one at least every second when keyframes are
	#   further apart (keyframeInterval "None": libx264 and NVENC every 250
	#   frames, 3 s at 80 fps). Such a fragment can end on a frame that the
	#   CPU path's libx264 encoded ahead of the B-frames shown before it, so a
	#   video cut off there lacks those: its last 5 frames read back need not
	#   be the last 5 sent (11 of 20 cuts at 10 fps). GPU paths have no B-frames.
	# - empty_moov writes the header before the first fragment, and delay_moov
	#   holds it until that fragment is cut, so that it gets an edit list for
	#   the B-frames' reordering delay, as a plain MP4's header does. Without
	#   one, the CPU path's video (-bf 4) starts 2 frames late, and ffmpeg -ss
	#   gave the keyframe for each of the 2 frames before every keyframe (122
	#   of 2401 frames with keyframeInterval 0.5)
	# - default_base_moof: offsets within a fragment count from the fragment
	# - -flush_packets 1: ffmpeg otherwise writes 256 KB at a time, so a
	#   fragment reached the disk only once later ones had filled the buffer:
	#   29 s of a simulated 160x120 RatCam's video
	# The header then has no frame count (ffprobe's nb_frames is N/A): readers
	# count the frames, or take them from the duration, which is exact. The
	# file is 0.2-0.6 KB a second larger (1-2 MB per camera-hour; 140 bytes
	# more for each keyframe the encoder adds itself, at a scene cut). Other
	# containers are left as they are.
	muxer_params = []
	if Fragmented(cam_params):
		muxer_params = [
			"-movflags", "+frag_keyframe+empty_moov+delay_moov+default_base_moof",
			"-frag_duration", "1000000", # microseconds
			"-flush_packets", "1",
			]

	return {
		"codec": codec,
		"pix_fmt_out": pix_fmt_out,
		"input_params": input_params,
		"output_params": muxer_params + gpu_params,
		"macro_block_size": macro_block_size,
		"key": codec if key_preset is None else "{}/{}".format(codec, key_preset),
		"device": device,
		"env": env,
		}


RGB_FORMATS = ("rgb0", "bgr0", "rgb24", "bgr24", "rgba", "bgra")


def FullRange(cam_params):
	return str(cam_params.get("colorRange", "limited")).lower() == "full"


@functools.lru_cache(maxsize=None)
def FfmpegVersion(exe=None):
	# (major, minor) of the ffmpeg campy records with (imageio-ffmpeg's, or
	# ffmpegPath); None if it would not run, or names no release (a build
	# from ffmpeg's master branch says "N-121234-g...")
	try:
		if exe is None:
			import imageio_ffmpeg
			exe = imageio_ffmpeg.get_ffmpeg_exe()
		out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=30).stdout
	except Exception:
		return None
	m = re.match(r"ffmpeg version n?(\d+)\.(\d+)", out)
	return (int(m.group(1)), int(m.group(2))) if m else None


def Fragmented(cam_params):
	# Whether the video is written in fragments (fragmentedVideo: see EncoderArgs)
	extension = os.path.splitext(str(cam_params.get("videoFilename", "")))[1].lower()
	return bool(cam_params.get("fragmentedVideo", True)) and extension in (".mp4", ".m4v", ".mov")


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

def FfmpegProcess(writer):
	"""
	The ffmpeg process (its subprocess.Popen) behind imageio-ffmpeg's writer,
	for its exit code. write_frames keeps it to itself, as p in the generator
	it returns (imageio-ffmpeg 0.6); None where it is not there, as in a
	stand-in for write_frames.
	"""
	frame = getattr(writer, "gi_frame", None)
	process = frame.f_locals.get("p") if frame is not None else None
	return process if hasattr(process, "poll") else None


def FfmpegStopped(ffmpeg, error, frames):
	# Why the video took no more frames, with ffmpeg's exit code where it can be read
	code = None
	if ffmpeg is not None:
		try:
			code = ffmpeg.wait(timeout=5) # imageio-ffmpeg has already waited for it, or killed it
		except Exception:
			code = ffmpeg.poll()
	why = "ffmpeg stopped after taking {} frames".format(frames)
	if code is not None:
		why += ", with {}".format(ExitCodeText(code))
	return "{}: {}".format(why, ErrorText(error).splitlines()[0])


def VideoFailed(cam_params, recording, stopReadQueue, why, stopping=True):
	# The video takes no more frames: said at once, and the camera is stopped
	# (stopReadQueue), so that it is closed and its metadata saved, rather
	# than recording on into nothing
	name = cam_params["cameraName"]
	recording.videoError = why # before the stop: the camera thread reads it as it stops
	if stopping:
		stopReadQueue.append("STOP")
	# A fragmented video can be read up to the last fragment ffmpeg wrote; a
	# plain MP4 only once ffmpeg has closed it (writes its index)
	kept = "can be read up to its last fragment" if Fragmented(cam_params) else "may be unreadable"
	said = "{}: {}. {}Its video {}, and its metadata.csv and frametimes.npy count only the frames " \
		"ffmpeg was given.".format(name, why, "Stopping {}. ".format(name) if stopping else "", kept)
	if str(cam_params.get("ffmpegLogLevel")) == "quiet":
		said += " ffmpegLogLevel is \"quiet\", so ffmpeg did not say why: \"warning\" shows its errors."
	Say(said)


def WriteFrames(cam_params, writeQueue, stopReadQueue, stopWriteQueue, openQueue=None, recording=None):
	# recording: what the writer and the camera thread tell each other
	# (unicam.Recording): how many frames ffmpeg was given, and why the video
	# failed, if it did. The camera thread saves metadata once the writer is
	# done (recording.written).
	if recording is None:
		recording = unicam.Recording()
	ffmpeg = None
	try:
		# Ctrl-C from here on stops the camera (stopReadQueue), which then stops
		# the writer, instead of ending the camera process
		with QueueKeyboardInterrupt({"queue": stopReadQueue, "message": "STOP"}):
			# Wait for the camera thread to open the camera (openQueue: whether it
			# opened). Opening it can set frameWidth and frameHeight (a Basler's
			# cameraSettings file, an emu clip), and ffmpeg takes every frame as the
			# size it was started with: opened before, the video was scrambled. A
			# camera that did not open gets no video.
			if openQueue is not None:
				while not openQueue:
					time.sleep(0.01)
				if not openQueue[0]:
					return

			# Start ffmpeg video writer
			try:
				writer, writing, _ = OpenWriter(cam_params, stopReadQueue)
			except Exception as e:
				VideoFailed(cam_params, recording, stopReadQueue, "ffmpeg did not start: {}".format(ErrorText(e)))
				return
			ffmpeg = FfmpegProcess(writer)
			if ffmpeg is not None:
				# campy's main process says it, if it has to stop this process at a
				# second Ctrl-C while ffmpeg still runs
				unicam.TellMain(("ffmpeg", ffmpeg.pid))

			# Write until interrupted and/or stop message received
			try:
				while(writing):
					if writeQueue:
						writer.send(writeQueue.popleft())
						recording.framesSent += 1
					else:
						# Once queue is depleted and grabber stops, then stop writing
						if stopWriteQueue:
							writing = False
						# Otherwise continue writing
						time.sleep(0.01)
			except Exception as e:
				# ffmpeg has stopped (killed, crashed, or an encoder error), and the
				# frames still queued will not reach the video. The camera is
				# stopped, unless it already has (stopWriteQueue).
				VideoFailed(cam_params, recording, stopReadQueue, FfmpegStopped(ffmpeg, e, recording.framesSent),
					stopping=not stopWriteQueue)
				return

		# Close up...
		Say("Closing video writer for {}. Please wait...".format(cam_params["cameraName"]))
		time.sleep(1)
		writer.close()

		# ffmpeg finishes the video as it closes: an error there can leave it unfinished
		code = ffmpeg.returncode if ffmpeg is not None else None
		if code:
			VideoFailed(cam_params, recording, stopReadQueue,
				"ffmpeg ended with {} as it finished the video".format(ExitCodeText(code)), stopping=False)

	finally:
		if ffmpeg is not None:
			unicam.TellMain(("ffmpeg", None))
		recording.written.set()
