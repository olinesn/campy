"""
"""

import os, copy, yaml, time, logging
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter, ArgumentTypeError
from campy.cameras import unicam
from campy.utils.utils import Say


def DefaultParams():
	"""
	Default parameters for campy config.
	Omitted parameters will revert to these default values.
	""" 

	params = {}
	# Recording default parameters
	params["numCams"] = 1
	params["videoFolder"] = "./test"
	params["videoFilename"] = "0.mp4"
	params["frameRate"] = 100
	params["recTimeInSec"] = 10

	# Camera default parameters
	params["cameraMake"] = "basler"
	params["cameraSettings"] = "None"
	params["frameWidth"] = 1152
	params["frameHeight"] = 1024
	params["cameraDebug"] = False

	# Flir camera default parameters
	params["cameraTrigger"] = "None" # "Line3"
	params["cameraOut"] = 2
	params["bufferMode"] = "OldestFirst"
	params["bufferSize"] = 100
	params["cameraExposureTimeInUs"] = 1500
	params["cameraGain"] = 1
	params["disableGamma"] = True
	params["cameraLinkLimitMBps"] = "auto" # USB throughput limit: "auto", MB/s, or "None" (flir.ConfigureLinkLimit)

	# RatCam (nir-eye-cam) parameters: see campy/cameras/ratcam.py
	params["ratcamPorts"] = "None" # "None" = find RatCams by USB; or a list of ports, "sim" = simulated
	params["ratcamGain"] = 0 # analog gain code (AGAIN): RP2040 0-3, ESP32-P4 0-28
	params["ratcamLed"] = 0 # IR LED level 0-1000 (ESP32-P4: on/off); 0 = off
	params["ratcamSyncOut"] = False # drive the random sync pulse train on SYNC_OUT

	# Compression default parameters
	params["ffmpegLogLevel"] = "quiet"
	params["ffmpegPath"] = "None" # "/home/usr/Documents/ffmpeg/ffmpeg"
	params["pixelFormatInput"] = "rgb24" # "bayer_bggr8" "rgb24"
	params["pixelFormatOutput"] = "rgb0"
	params["colorRange"] = "limited" # "full": 0-255 video, every gray level of the camera (writer.EncoderArgs)
	params["gpuID"] = -1
	params["gpuMake"] = "nvidia"
	params["codec"] = "h264"  
	params["quality"] = 21
	params["preset"] = "None"
	params["keyframeInterval"] = "None" # seconds between keyframes; "None" = encoder default
	params["fragmentedVideo"] = True # .mp4/.mov in fragments of up to 1 s, readable after a crash (writer.EncoderArgs)
	params["vaapiDevice"] = "/dev/dri/renderD128" # gpuMake "vaapi" only
	params["vaapiLinearSurfaces"] = True # gpuMake "vaapi" on AMD GPUs: see writer.EncoderArgs
	params["writeQueueMaxSec"] = 5 # frames waiting for the encoder, in seconds; "None" = unbounded

	# Launch check parameters (campy/preflight.py)
	params["preflight"] = "error" # "error", "warn" or "off"
	params["capacityFile"] = "None" # written by campy-encode-sweep; "None" = ~/.campy/capacity.json
	params["usbBudgetMBps"] = "None" # per USB bus; "None" = by bus speed (see preflight.py)

	# Display parameters
	params["chunkLengthInSec"] = 5
	params["displayFrameRate"] = 10
	params["displayDownsample"] = 2

	# Trigger parameters
	params["triggerController"] = "arduino"
	params["startArduino"] = False
	params["serialPort"] = "COM3"
	params["digitalPins"] = [0,1,2,3,4,5,6]
	params["readyTimeoutSec"] = 30 # wait this long for every camera before starting the trigger
	params["blinkPin"] = "None" # Arduino pin with campy-synctest's test LED; "None" = no LED
	params["blinkHz"] = "None" # its blink rate: not near a whole or half multiple of frameRate

	return params


def AutoParams(params, default_params):
	# Handle out of range values (reset to default)
	range_params = [
		"numCams",
		"frameRate",
		"recTimeInSec",
		"frameHeight",
		"frameWidth",
		"bufferSize",
		"cameraGain",
		"cameraExposureTimeInUs",
		"quality",
		"chunkLengthInSec",
		"displayFrameRate",
		"displayDownsample",
		]

	for key in range_params:
		default_value = default_params[key]
		# One value, or one per camera: check each
		values = params[key] if isinstance(params[key], list) else [params[key]]
		fixed = []
		for value in values:
			if key == "displayFrameRate" and value == 0:
				pass # 0 = no display window (headless)
			elif value <= 0:
				value = default_value
				Say("{} set to invalid value in config. Setting to default ({})."\
						.format(key, default_value))
			fixed.append(value)
		params[key] = fixed if isinstance(params[key], list) else fixed[0]

	return params


def FillCameraLists(params):
	# Name and select cameras the config does not list. Runs after command-line
	# overrides, so --numCams changes how many cameras get a name.
	if "cameraNames" not in params.keys():
		params["cameraNames"] = ["Camera%s" % n for n in range(params["numCams"])]
	if "cameraSelection" not in params.keys():
		params["cameraSelection"] = [n for n in range(params["numCams"])]
	return params


def ConfigureParams():
	parser = ArgumentParser(description="Campy CLI", 
						formatter_class=ArgumentDefaultsHelpFormatter,)
	clargs = ParseClargs(parser)
	params = CombineConfigAndClargs(clargs)
	ValidateCameraLists(params)
	ValidateColorRange(params)
	if params["startArduino"]:
		TriggerFrameRate(params) # fails here, with the reason, if the rates disagree

	# Optionally, user can manually set path to find ffmpeg binary.
	if params["ffmpegPath"] != "None":
		os.environ["IMAGEIO_FFMPEG_EXE"] = params["ffmpegPath"]

	return params


def ConfigureCamParams(systems, params, n_cam):
	# Insert camera-specific metadata from parameters into cam_params dictionary
	cam_params = params
	cam_params["n_cam"] = n_cam
	cam_params["baseFolder"] = os.getcwd()
	cam_params["cameraName"] = params["cameraNames"][n_cam]

	cam_params = OptParams(cam_params)
	cam_make = cam_params["cameraMake"]
	cam_idx = cam_params["cameraSelection"]

	cam_params["device"] = systems[cam_make]["deviceList"][cam_idx]
	cam_params = unicam.LoadDevice(systems, params, cam_params)

	cam_params["cameraSerialNo"] = systems[cam_make]["serials"][cam_idx]

	return cam_params


# Lists that are not one value per camera
NON_CAMERA_LISTS = ("digitalPins", "ratcamPorts")
# Lists that need a value for every camera: one name or index cannot be shared
EVERY_CAMERA_LISTS = ("cameraNames", "cameraSelection")


def OptParams(cam_params):
	# A single value applies to every camera. A list holds one value per camera,
	# in camera order; a one-item list applies to every camera, and a list longer
	# than numCams uses its first numCams items, so a config written for more
	# cameras also runs with fewer (ValidateCameraLists checks this at startup).
	for key in cam_params:
		if type(cam_params[key]) is list and key not in NON_CAMERA_LISTS:
			values = cam_params[key]
			if len(values) == 1 and key not in EVERY_CAMERA_LISTS:
				cam_params[key] = values[0]
			else:
				cam_params[key] = values[cam_params["n_cam"]]
	return cam_params


def ValidateCameraLists(params):
	# Per-camera lists must cover every camera, so changing numCams fails here
	# with the key's name rather than in a camera process.
	num_cams = params["numCams"]
	for key, value in params.items():
		if type(value) is not list or key in NON_CAMERA_LISTS:
			continue
		if key in EVERY_CAMERA_LISTS and len(value) < num_cams:
			raise ValueError("{} has {} values for {} cameras. It needs one per camera."\
				.format(key, len(value), num_cams))
		if 1 < len(value) < num_cams:
			raise ValueError("{} has {} values for {} cameras. Give one value per camera, "
				"or a single value for all of them.".format(key, len(value), num_cams))
		if len(value) > num_cams:
			Say("{} lists {} values; using the first {} (numCams).".format(key, len(value), num_cams))


def ValidateColorRange(params):
	# One value, or one per camera
	values = params["colorRange"] if isinstance(params["colorRange"], list) else [params["colorRange"]]
	for value in values:
		if str(value).lower() not in ("limited", "full"):
			raise ValueError('colorRange is "limited" or "full", not "{}".'.format(value))


def TriggerFrameRate(params):
	# The rate the trigger controller pulses at: the frame rate of the cameras it
	# triggers (cameraTrigger set), or of every camera if none is set. With one
	# frame rate per camera (a RatCam free-runs at its own), those must agree.
	cams = [CameraParams(params, n) for n in range(params["numCams"])]
	triggered = [cam for cam in cams if str(cam["cameraTrigger"]) != "None"] or cams
	rates = sorted(set(float(cam["frameRate"]) for cam in triggered))
	if len(rates) > 1:
		raise ValueError("The trigger controller pulses at one rate, but the triggered cameras have "
			"frame rates {}. Give them one frameRate, or set cameraTrigger \"None\" on the cameras "
			"it does not trigger.".format(", ".join("{:g}".format(r) for r in rates)))
	return int(rates[0]) if rates[0].is_integer() else rates[0]


def BlinkSettings(params):
	# The test LED's (pin, Hz) for the trigger controller; (-1, 0) when there is none
	pin, hz = params.get("blinkPin", "None"), params.get("blinkHz", "None")
	if str(pin) == "None" or str(hz) == "None" or float(hz) <= 0:
		return -1, 0
	hz = float(hz)
	return int(pin), (int(hz) if hz.is_integer() else hz)


def CameraParams(params, n_cam):
	# One camera's parameters, without opening its device: what OptParams gives
	# the camera's process. For checks that run before any camera starts.
	cam_params = copy.deepcopy(params)
	cam_params["n_cam"] = n_cam
	cam_params = OptParams(cam_params)
	cam_params["cameraName"] = cam_params["cameraNames"]
	return cam_params


def CheckConfig(params, clargs):
	default_params = DefaultParams()
	for key,value in default_params.items():
		if key not in params.keys():
			params[key] = value

	auto_params = AutoParams(params, default_params)
	for key,value in auto_params.items():
		params[key] = value

	invalid_keys = []
	for key in params.keys():
		if key not in clargs.__dict__.keys():
			invalid_keys.append(key)

	if len(invalid_keys) > 0:
		invalid_key_msg = [" %s," % key for key in invalid_keys]
		msg = "Unrecognized keys in the config: %s" % "".join(invalid_key_msg)
		raise ValueError(msg)

	return params


def LoadConfig(config_path):
	try:
		with open(config_path, "rb") as f:
			config = yaml.safe_load(f)
	except Exception as e:
		logging.error('Caught this error at configurator.py LoadConfig: {}. Check your config path!'.format(e))
		raise
	return config


def CombineConfigAndClargs(clargs):
	params = LoadConfig(clargs.config)
	# Overrides go in before CheckConfig, so a value from the command line gets
	# the same range check as one from the config
	for key, value in clargs.__dict__.items():
		if value is not None:
			params[key] = value
	params = CheckConfig(params, clargs)
	params = FillCameraLists(params)
	return params


# What each kind of override value is called in an error
KIND_NAMES = {int: "a whole number", float: "a number", bool: "True or False", str: "text"}


def ConfigValue(kind, words=(), lists=True):
	"""
	An argparse type that reads an override the way LoadConfig reads the config
	(yaml.safe_load), so --key value means what key: value means there: Line3 is
	text, False is False, [24, 26] is one value per camera, and None is the text
	"None" that campy compares against. Each value must be of kind (float takes
	whole numbers too) or one of words, so a typo fails here, naming the option.
	lists=False is for options read once for the whole rig (numCams, the
	trigger's, recTimeInSec in the launch check), which a list would break.
	"""
	def Check(item):
		if isinstance(item, str) and item in words:
			return item
		if kind is float and isinstance(item, str):
			try:
				return float(item) # PyYAML reads 1e3 as text: its floats need a point
			except ValueError:
				pass
		if isinstance(item, bool):
			fits = kind is bool
		else:
			fits = kind is not bool and isinstance(item, (int, float) if kind is float else kind)
		if fits:
			return item
		expected = " or ".join([KIND_NAMES[kind]] + ['"{}"'.format(w) for w in words])
		if lists:
			expected += ", or a list of them"
		if item is None:
			hint = " (YAML reads null, ~ and an empty value as no value: write None)"
		elif isinstance(item, bool):
			hint = " (YAML reads yes, no, on, off, true and false as True or False: quote text, e.g. \"'off'\")"
		else:
			hint = ""
		raise ArgumentTypeError("{!r} is not {}{}".format(item, expected, hint))

	def Parse(text):
		try:
			value = yaml.safe_load(text)
		except yaml.YAMLError as e:
			raise ArgumentTypeError("{!r} is not a value campy can read ({})".format(text, getattr(e, "problem", e)))
		if lists and isinstance(value, list):
			return [Check(item) for item in value]
		return Check(value)

	return Parse


def TextValue(text):
	# Paths, and words YAML would change (preflight's off is not False), are taken
	# as typed, so C:\ffmpeg\bin\ffmpeg.exe keeps its backslashes. One pair of
	# quotes around the whole value is dropped, as in the old "'./configs/rgb24.pfs'".
	if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
		return text[1:-1]
	return text


def ParseClargs(parser, argv=None):
	parser.epilog = "Each --key value means what key: value means in the config file, which is YAML: \
		Line3 and h265 are text, True and False are booleans, and None is the text None. YAML also reads \
		yes, no, on and off as booleans, which the True/False options take; text options refuse them, \
		so quote such text: \"'off'\". Options that take one value per camera also take a list, e.g. \
		--quality \"[24, 26]\" (quoted for the shell). Paths, and --preflight, are taken as typed."
	parser.add_argument(
		"config", metavar="config", help="Campy configuration .yaml file.",
	)

	# Recording arguments
	parser.add_argument(
		"--videoFolder", 
		dest="videoFolder", 
		type=TextValue,
		help="Folder in which to save videos.",
	)
	parser.add_argument(
		"--videoFilename", 
		dest="videoFilename", 
		type=TextValue,
		help="Name for video output file.",
	)
	parser.add_argument(
		"--frameRate", 
		dest="frameRate",
		type=ConfigValue(float),
		help="Frame rate equal to trigger frequency.",
	)
	parser.add_argument(
		"--recTimeInSec",
		dest="recTimeInSec",
		type=ConfigValue(float, lists=False),
		help="Recording time in seconds.",
	)    
	parser.add_argument(
		"--numCams", 
		dest="numCams", 
		type=ConfigValue(int, lists=False),
		help="Number of cameras.",
	)
	parser.add_argument(
		"--cameraNames", 
		dest="cameraNames", 
		type=ConfigValue(str),
		help="Names assigned to the cameras in the order of cameraSelection.",
	)
	parser.add_argument(
		"--cameraSelection",
		dest="cameraSelection",
		type=ConfigValue(int),
		help="Selects and orders camera indices to include in the recording. \
				List length must be equal to numCams",
	)

	# Camera arguments. May be specific to particular camera make
	parser.add_argument(
		"--cameraMake", 
		dest="cameraMake", 
		type=ConfigValue(str),
		help="Company that produced the camera. Currently supported: 'basler', 'flir', 'ratcam' \
			(nir-eye-cam), 'emu' (plays a video file).",
	)
	parser.add_argument(
		"--cameraSettings", 
		dest="cameraSettings",
		type=TextValue,
		help="Path to camera settings file.",
	)
	parser.add_argument(
		"--frameHeight", 
		dest="frameHeight",
		type=ConfigValue(int),
		help="Frame height in pixels.",
	)
	parser.add_argument(
		"--frameWidth", 
		dest="frameWidth",
		type=ConfigValue(int),
		help="Frame width in pixels.",
	)
	parser.add_argument(
		"--cameraDebug", 
		dest="cameraDebug",
		type=ConfigValue(bool),
		help="Flag to turn on camera debug mode.",
	)
	parser.add_argument(
		"--cameraTrigger", 
		dest="cameraTrigger",
		type=ConfigValue(str),
		help="String indicating trigger input to camera (e.g. 'Line3').",
	)
	parser.add_argument(
		"--cameraOut", 
		dest="cameraOut",
		type=ConfigValue(int),
		help="Integer indicating camera output line for exposure active signal (e.g. 2).",
	)
	parser.add_argument(
		"--cameraExposureTimeInUs", 
		dest="cameraExposureTimeInUs",
		type=ConfigValue(int),
		help="Exposure time (in microseconds) for each camera frame.",
	)
	parser.add_argument(
		"--cameraGain", 
		dest="cameraGain",
		type=ConfigValue(float),
		help="Intensity gain applied to each camera frame.",
	)
	parser.add_argument(
		"--disableGamma", 
		dest="disableGamma",
		type=ConfigValue(bool),
		help="Whether to disable gamma (default: True).",
	)
	parser.add_argument(
		"--cameraLinkLimitMBps",
		dest="cameraLinkLimitMBps",
		type=ConfigValue(float, ("auto", "None")),
		help="FLIR USB3 cameras: the camera's USB throughput limit (DeviceLinkThroughputLimit). 'auto' \
			(default) raises it to the camera's maximum when it is too low for frameRate; a number sets \
			it in MB/s; 'None' leaves the camera's own limit.",
	)
	parser.add_argument(
		"--bufferMode", 
		dest="bufferMode",
		type=ConfigValue(str),
		help="Type of buffer to use in camera (default: 'OldestFirst').",
	)
	parser.add_argument(
		"--bufferSize", 
		dest="bufferSize",
		type=ConfigValue(int),
		help="Size of buffer to use in camera in frames (default: 100).",
	)

	# RatCam (nir-eye-cam) arguments
	parser.add_argument(
		"--ratcamPorts",
		dest="ratcamPorts",
		type=ConfigValue(str),
		help="RatCam serial ports, e.g. ['/dev/ttyACM1']; 'sim' is a simulated camera. \
			Default: every RatCam found by USB, in order of serial number.",
	)
	parser.add_argument(
		"--ratcamGain",
		dest="ratcamGain",
		type=ConfigValue(int),
		help="RatCam analog gain code (the firmware's AGAIN): RP2040 0-3 (1x-8x), ESP32-P4 0-28.",
	)
	parser.add_argument(
		"--ratcamLed",
		dest="ratcamLed",
		type=ConfigValue(int),
		help="RatCam IR LED level, 0-1000 (ESP32-P4: on or off). 0 (the default) = off.",
	)
	parser.add_argument(
		"--ratcamSyncOut",
		dest="ratcamSyncOut",
		type=ConfigValue(bool),
		help="Drive the random sync pulse train on this RatCam's SYNC_OUT (one camera per rig).",
	)

	# ffmpeg arguments
	parser.add_argument(
		"--ffmpegPath",
		dest="ffmpegPath",
		type=TextValue,
		help="Location of ffmpeg binary for imageio.",
	)
	parser.add_argument(
		"--ffmpegLogLevel",
		dest="ffmpegLogLevel",
		type=ConfigValue(str),
		help="Sets verbosity level for ffmpeg logging. ('quiet' (no warnings), \
			'warning', 'info' (real-time stats)).",
	)
	parser.add_argument(
		"--pixelFormatInput",
		dest="pixelFormatInput",
		type=ConfigValue(str),
		help="Pixel format input. Use 'rgb24' for RGB or 'bayer_bggr8' for 8-bit bayer pattern.",
	)
	parser.add_argument(
		"--pixelFormatOutput",
		dest="pixelFormatOutput",
		type=ConfigValue(str),
		help="Pixel format output. Use 'rgb0' for best results.",
	)
	parser.add_argument(
		"--colorRange",
		dest="colorRange",
		type=ConfigValue(str),
		help="Video color range: 'limited' (16-235, the default) or 'full' (0-255: every gray level of the \
			camera, and less CPU for ffmpeg; raise quality by about 2 for the same file size).",
	)
	parser.add_argument(
		"--gpuID",
		dest="gpuID",
		type=ConfigValue(int),
		help="List of integers assigning the gpu index to stream each camera. \
			Set to -1 to stream with CPU.",
	)
	parser.add_argument(
		"--gpuMake",
		dest="gpuMake",
		type=ConfigValue(str),
		help="Company that produced the GPU. Currently supported: 'nvidia', 'amd', 'intel' (QuickSync), \
			'vaapi' (Linux VA-API: AMD through Mesa, or Intel).",
	)
	parser.add_argument(
		"--vaapiDevice",
		dest="vaapiDevice",
		type=TextValue,
		help="VA-API render device for gpuMake 'vaapi' (default: /dev/dri/renderD128).",
	)
	parser.add_argument(
		"--vaapiLinearSurfaces",
		dest="vaapiLinearSurfaces",
		type=ConfigValue(bool),
		help="gpuMake 'vaapi' on AMD GPUs: keep video surfaces linear (Mesa's AMD_DEBUG=novideotiling), \
			so each frame goes straight from the CPU into GPU memory instead of being copied there by \
			the GPU's compute engine (default: True).",
	)
	parser.add_argument(
		"--writeQueueMaxSec",
		dest="writeQueueMaxSec",
		type=ConfigValue(float, ("None",)),
		help="Most frames held waiting for the encoder, in seconds of video. When the encoder \
			falls this far behind, new frames are dropped and counted (frametimes.npy shows the \
			gaps, metadata.csv the count) instead of filling memory. 'None' = unbounded.",
	)
	parser.add_argument(
		"--preflight",
		dest="preflight",
		type=TextValue, # off is a mode here, not YAML's False
		help="Launch check of USB bandwidth, encoder capacity and disk: 'error' (stop if the \
			configuration does not fit), 'warn' (report only) or 'off'.",
	)
	parser.add_argument(
		"--capacityFile",
		dest="capacityFile",
		type=TextValue,
		help="Encoder capacity table written by campy-encode-sweep (default: ~/.campy/capacity.json).",
	)
	parser.add_argument(
		"--usbBudgetMBps",
		dest="usbBudgetMBps",
		type=ConfigValue(float, ("None",), lists=False),
		help="Most camera data per USB bus, in MB/s, for the launch check. Default: by bus speed.",
	)
	parser.add_argument(
		"--codec",
		dest="codec",
		type=ConfigValue(str),
		help="Video codec for compression. Currently supported: 'h264', 'h265' (hevc), and 'av1' \
			with gpuMake 'vaapi', 'nvidia' or 'intel' (quality is then AV1's quantizer index, 0-255).",
	)
	parser.add_argument(
		"--quality",
		dest="quality",
		type=ConfigValue(int),
		help="Compression quality: lower is less compression and larger files. libx264/libx265 \
			take it as a CRF, which adapts to the picture; NVENC, VA-API and AMF as a constant \
			quantizer, which spends bits on sensor noise too, so the same number gives much larger \
			files there. One value, or one per camera. campy-encode-quality measures each setting.",
	)
	parser.add_argument(
		"--preset",
		dest="preset",
		type=ConfigValue(str),
		help="Compression preset (e.g. 'slow', 'fast', 'veryfast'). \
				Incorrect settings may break the pipe. Test with ffmpegLogLevel 'warning' or 'info'.",
	)
	parser.add_argument(
		"--keyframeInterval",
		dest="keyframeInterval",
		type=ConfigValue(float, ("None",)),
		help="Seconds between keyframes (I-frames), e.g. 0.5. Shorter makes seeking and \
			sparse frame sampling cheaper (LUCID's image ID-switch check needs 0.5) \
			but files larger. Default: the encoder's own (NVENC: every 250 frames).",
	)
	parser.add_argument(
		"--fragmentedVideo",
		dest="fragmentedVideo",
		type=ConfigValue(bool),
		help="Write .mp4 and .mov videos in fragments of at most a second (default: True), so that a \
			video whose ffmpeg is stopped before it closes the file (a power cut, an OS crash, ffmpeg \
			killed) is still readable up to its last fragment. A plain MP4 is then unreadable. \
			False writes a plain MP4.",
	)

	# Display and CLI feedback arguments
	parser.add_argument(
		"--chunkLengthInSec",
		dest="chunkLengthInSec",
		type=ConfigValue(float),
		help="Length of video chunks in seconds for reporting recording progress.",
	)
	parser.add_argument(
		"--displayFrameRate",
		dest="displayFrameRate",
		type=ConfigValue(float),
		help="Display frame rate in Hz. Max ~30. 0 = no display window (headless).",
	)
	parser.add_argument(
		"--displayDownsample",
		dest="displayDownsample",
		type=ConfigValue(int),
		help="Downsampling factor for displaying images.",
	)

	# Microcontroller triggering arguments
	parser.add_argument(
		"--triggerController",
		dest="triggerController",
		type=ConfigValue(str, lists=False),
		help="Microntroller make for camera triggering. Currently supported: 'arduino'.",
	)
	parser.add_argument(
		"--startArduino",
		dest="startArduino",
		type=ConfigValue(bool, lists=False),
		help="If True, start Arduino after initializing cameras.",
	)
	parser.add_argument(
		"--blinkPin",
		dest="blinkPin",
		type=ConfigValue(int, ("None",), lists=False),
		help="Arduino pin driving a test LED that every camera can see, for campy-synctest.",
	)
	parser.add_argument(
		"--blinkHz",
		dest="blinkHz",
		type=ConfigValue(float, ("None",), lists=False),
		help="Test LED blink rate. Pick one that is not near a whole or half multiple of the \
			frame rate (e.g. 43 Hz at 25 or 60 fps).",
	)
	parser.add_argument(
		"--readyTimeoutSec",
		dest="readyTimeoutSec",
		type=ConfigValue(float, lists=False),
		help="Longest wait for every camera to be grabbing before campy starts the trigger \
			anyway (and says which cameras were not ready).",
	)
	parser.add_argument(
		"--serialPort",
		dest="serialPort",
		type=ConfigValue(str, lists=False),
		help="Serial port for communicating with Arduino.",
	)
	parser.add_argument(
		"--digitalPins",
		dest="digitalPins",
		type=ConfigValue(int),
		help="Digital pins on microcontroller board for sending TTL camera triggers.",
	)

	return parser.parse_args(argv)

