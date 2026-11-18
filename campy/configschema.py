"""
Every campy-acquire setting in one table: its default, the values it takes, and
its help. configurator builds DefaultParams(), the command-line options and the
checks of a config from it, and `campy-config reference` writes
docs/config-reference.md from it, so the code, --help and the docs can't drift.
"""

# kind: what a value is. int, float (whole numbers too), bool and str as YAML
# reads them; TEXT is text taken as typed on the command line (paths, and
# words YAML would change); GAMMA is cameraGamma's number above 0 or "Off".
TEXT = "text"
GAMMA = "gamma"
# SECTION is a mapping of its own key: value lines that campy-acquire passes
# by, for another program to read (campy-session's live:, view:, post:, panel:)
SECTION = "section"
ID = "id" # an ID: text or a number, kept as text (animalIds)
MAPPING = "mapping" # names and their values (sessionFields)

# per_camera: how a list is read
PER_CAMERA = "per camera" # one value for every camera, or a list with one per camera
EVERY_CAMERA = "every camera" # a list with a value for every camera (a name or index can't be shared)
ONE = "one" # one value for the whole rig: a list is refused
LIST = "list" # the value itself may be a list (pins, ports), not one per camera

# The default of a setting campy fills in from numCams (FillCameraLists)
FILLED = object()


class Setting:
	def __init__(self, name, default, kind, help, section, words=(), choices=None, fold_case=False,
			per_camera=PER_CAMERA, low=None, high=None, above=None, replaced_by=None):
		self.name = name
		self.default = default
		self.kind = kind
		self.help = " ".join(help.split())
		self.section = section
		self.words = tuple(words) # words taken besides values of kind, e.g. "None"
		self.choices = tuple(choices) if choices else None # for text: the only values taken
		self.fold_case = fold_case # choices in any case (colorRange "Full")
		self.per_camera = per_camera
		self.low = low # lowest number taken
		self.above = above # numbers must be above this
		self.high = high # highest number taken
		self.replaced_by = replaced_by # an old key, read as this one (configurator.RENAMED)

	def Takes(self):
		# What the setting takes, in words, for the reference page
		if self.kind is GAMMA:
			return 'a number above 0, or "Off"'
		if self.kind is SECTION:
			return "key: value lines of its own"
		if self.kind is ID:
			return "IDs, text or numbers"
		if self.kind is MAPPING:
			return "a mapping, e.g. {cage: B3}"
		if self.choices:
			names = ['"{}"'.format(c) for c in self.choices]
			return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
		if self.kind is bool:
			return "True or False"
		if self.kind in (str, TEXT):
			text = "text" if self.kind is str else "text, as typed"
		else:
			text = "a whole number" if self.kind is int else "a number"
			if self.above is not None:
				text += " above {:g}".format(self.above)
			if self.low is not None and self.high is not None:
				text += " from {:g} to {:g}".format(self.low, self.high)
			elif self.low is not None:
				text += ", {:g} or more".format(self.low)
			elif self.high is not None:
				text += ", {:g} or less".format(self.high)
		if self.words:
			text += ", or " + " or ".join('"{}"'.format(w) for w in self.words)
		return text

	def Lists(self):
		if self.kind is SECTION:
			return "-"
		return {PER_CAMERA: "one, or one per camera", EVERY_CAMERA: "one per camera",
			ONE: "one for the rig", LIST: "a list"}[self.per_camera]


REC = "Recording"
CAM = "Cameras"
FLIR = "FLIR cameras"
RATCAM = "RatCam (nir-eye-cam) cameras"
ENC = "Compression"
LIVE = "Live feed"
ALERTS = "Recording watch and alerts"
CHECK = "Launch check"
DISPLAY = "Display and progress"
TRIGGER = "Trigger"
SESSION = "Session"
PROGRAMS = "Sections other programs read"

SECTIONS = (REC, CAM, FLIR, RATCAM, ENC, LIVE, ALERTS, CHECK, DISPLAY, TRIGGER, SESSION, PROGRAMS)

SETTINGS = [
	# Recording
	Setting("numCams", 1, int, section=REC, per_camera=ONE, low=1, help="Number of cameras."),
	Setting("cameraNames", FILLED, str, section=REC, per_camera=EVERY_CAMERA,
		help="Names assigned to the cameras in the order of cameraSelection. Default: Camera0, Camera1, ..."),
	Setting("cameraSelection", FILLED, int, section=REC, per_camera=EVERY_CAMERA, low=0,
		help="Selects and orders camera indices to include in the recording. List length must be equal to \
			numCams. Default: 0, 1, ..."),
	Setting("cameraSerials", "None", ID, section=REC, per_camera=EVERY_CAMERA,
		help="Each camera's serial number, in the order of cameraNames: each name then goes to the camera with \
			that serial, wherever the camera driver lists it, instead of to cameraSelection's place in the \
			list. A serial that isn't connected stops campy before anything records, naming the camera and the \
			serials found. Default: none (cameras are picked by cameraSelection)."),
	Setting("videoFolder", "./test", TEXT, section=REC,
		help="Folder in which to save videos. Each run records into a new folder inside it, named by the \
			date and time it started (sessionFolder)."),
	Setting("sessionFolder", True, bool, section=REC,
		help="Record each run into a new folder inside videoFolder, named by the date and time it started, \
			e.g. 2026-10-18_17-30-05 (default: True). False records into videoFolder itself, and stops if a \
			recording is already there."),
	Setting("sessionPath", "None", TEXT, section=REC, per_camera=ONE,
		help="Record into this folder instead of a new one inside videoFolder (default: None). It is made if \
			it is not there, and campy stops if a recording is already in it. For a program that starts \
			campy-acquire and chose the folder itself (campy-session)."),
	Setting("stopFile", "None", TEXT, section=REC, per_camera=ONE,
		help="Stop the recording, as Ctrl-C does, once this file is there (default: None); \"now\" in it is \
			a second Ctrl-C. For a program that started campy-acquire, or a run started over SSH. campy stops \
			at once if it is there already, and removes it once it has stopped."),
	Setting("videoFilename", "0.mp4", TEXT, section=REC,
		help="Name for video output file. A name with \"calibration\" in it (0-calibration.mp4) is a \
			calibration video: it goes into <camera>/calibration_images/."),
	Setting("frameRate", 100, float, section=REC, above=0, help="Frame rate equal to trigger frequency."),
	Setting("recTimeInSec", 10, float, section=REC, per_camera=ONE, above=0, help="Recording time in seconds."),

	# Cameras
	Setting("cameraMake", "basler", str, section=CAM, choices=("basler", "flir", "ratcam", "emu"),
		help="Company that produced the camera. Currently supported: 'basler', 'flir', 'ratcam' \
			(nir-eye-cam), 'emu' (plays a video file)."),
	Setting("cameraSettings", "None", TEXT, section=CAM, help="Path to camera settings file."),
	Setting("frameWidth", 1152, int, section=CAM, low=1, help="Frame width in pixels."),
	Setting("frameHeight", 1024, int, section=CAM, low=1, help="Frame height in pixels."),
	Setting("cameraDebug", False, bool, section=CAM, help="Flag to turn on camera debug mode."),
	Setting("cameraTrigger", "None", str, section=CAM,
		help="String indicating trigger input to camera (e.g. 'Line3')."),
	Setting("cameraOut", 2, int, section=CAM, low=0,
		help="Integer indicating camera output line for exposure active signal (e.g. 2)."),
	Setting("bufferMode", "OldestFirst", str, section=CAM,
		choices=("OldestFirst", "NewestFirst", "NewestOnly", "OldestFirstOverwrite"),
		help="Type of buffer to use in camera (default: 'OldestFirst')."),
	Setting("bufferSize", 100, int, section=CAM, low=1,
		help="Size of buffer to use in camera in frames (default: 100)."),
	Setting("cameraExposureTimeInUs", 1500, int, section=CAM, above=0,
		help="Exposure time (in microseconds) for each camera frame."),
	Setting("cameraGain", 1, float, section=CAM, low=0,
		help="Intensity gain applied to each camera frame. The highest gain depends on the camera \
			(18 dB on a Blackfly S BFS-U3-13Y3M)."),

	# FLIR cameras
	Setting("cameraGamma", 0.6, GAMMA, section=FLIR,
		help="FLIR cameras: the gamma the camera applies to each frame (Gamma, with GammaEnable on): \
			brightness out = brightness in ^ cameraGamma, so below 1 brightens the dark and middle grays \
			and leaves black and white where they are. Default 0.6. 'Off' turns the camera's gamma off \
			(GammaEnable False: brightness in proportion to the light, what disableGamma True did). \
			A value the camera cannot take is set to its nearest limit (a Blackfly S takes 0.25 to 4), and \
			metadata.csv's gamma says what the camera has. Basler cameras take gamma from their .pfs file."),
	Setting("disableGamma", None, bool, section=FLIR, replaced_by="cameraGamma",
		help="Replaced by cameraGamma, and read as it: True is cameraGamma 'Off', False is the default, 0.6."),
	Setting("cameraLinkLimitMBps", "auto", float, section=FLIR, words=("auto", "None"), above=0,
		help="FLIR USB3 cameras: the camera's USB throughput limit (DeviceLinkThroughputLimit). 'auto' \
			(default) raises it to the camera's maximum when it is too low for frameRate; a number sets \
			it in MB/s; 'None' leaves the camera's own limit."),

	# RatCam
	Setting("ratcamPorts", "None", str, section=RATCAM, per_camera=LIST,
		help="RatCam serial ports, e.g. ['/dev/ttyACM1']; 'sim' is a simulated camera. \
			Default: every RatCam found by USB, in order of serial number."),
	Setting("ratcamGain", 0, int, section=RATCAM, low=0, high=28,
		help="RatCam analog gain code (the firmware's AGAIN): RP2040 0-3 (1x-8x), ESP32-P4 0-28."),
	Setting("ratcamLed", 0, int, section=RATCAM, low=0, high=1000,
		help="RatCam IR LED level, 0-1000 (ESP32-P4: on or off). 0 (the default) = off."),
	Setting("ratcamSyncOut", False, bool, section=RATCAM,
		help="Drive the random sync pulse train on this RatCam's SYNC_OUT (one camera per rig)."),

	# Compression
	Setting("ffmpegLogLevel", "quiet", str, section=ENC,
		choices=("quiet", "panic", "fatal", "error", "warning", "info", "verbose", "debug", "trace"),
		help="Sets verbosity level for ffmpeg logging. ('quiet' (no warnings), 'warning', 'info' \
			(real-time stats))."),
	Setting("ffmpegPath", "None", TEXT, section=ENC, per_camera=ONE, help="Location of ffmpeg binary for imageio."),
	Setting("pixelFormatInput", "rgb24", str, section=ENC,
		help="Pixel format input. Use 'gray' for mono, 'rgb24' for RGB, or the 8-bit Bayer pattern the camera \
			has: 'bayer_rggb8', 'bayer_bggr8', 'bayer_gbrg8' or 'bayer_grbg8' (a FLIR camera is set to it)."),
	Setting("pixelFormatOutput", "rgb0", str, section=ENC,
		help="Pixel format output. Use 'rgb0' for best results."),
	Setting("colorRange", "limited", str, section=ENC, choices=("limited", "full"), fold_case=True,
		help="Video color range: 'limited' (16-235, the default) or 'full' (0-255: every gray level of the \
			camera, and less CPU for ffmpeg; raise quality by about 2 for the same file size)."),
	Setting("gpuID", -1, int, section=ENC, low=-1,
		help="List of integers assigning the gpu index to stream each camera. Set to -1 to stream with CPU."),
	Setting("gpuMake", "nvidia", str, section=ENC, choices=("nvidia", "amd", "intel", "vaapi", "jetson"),
		help="Company that produced the GPU. Currently supported: 'nvidia', 'amd', 'intel' (QuickSync), \
			'vaapi' (Linux VA-API: AMD through Mesa, or Intel), 'jetson' (an NVIDIA Jetson's video \
			encoder, through GStreamer: gray frames, h264 or h265, always full range; docs/jetson.md)."),
	Setting("codec", "h264", str, section=ENC, choices=("h264", "h265", "av1"),
		help="Video codec for compression. Currently supported: 'h264', 'h265' (hevc), and 'av1' \
			with gpuMake 'vaapi', 'nvidia' or 'intel' (quality is then AV1's quantizer index, 0-255)."),
	Setting("quality", 21, int, section=ENC, low=0, high=255,
		help="Compression quality: lower is less compression and larger files. libx264/libx265 \
			take it as a CRF, which adapts to the picture; NVENC, VA-API and AMF as a constant \
			quantizer, which spends bits on sensor noise too, so the same number gives much larger \
			files there. One value, or one per camera. campy-encode-quality measures each setting. \
			0-51 for h264 and h265, 0-255 for av1."),
	Setting("preset", "None", str, section=ENC,
		help="Compression preset (e.g. 'slow', 'fast', 'veryfast'). Incorrect settings may break the pipe. \
			Test with ffmpegLogLevel 'warning' or 'info'. gpuMake 'jetson': 'ultrafast' (its default), \
			'fast', 'medium' or 'slow'."),
	Setting("keyframeInterval", "None", float, section=ENC, words=("None",), above=0,
		help="Seconds between keyframes (I-frames), e.g. 0.5. Shorter makes seeking and sparse frame \
			sampling cheaper (LUCID's image ID-switch check needs 0.5) but files larger. Default: the \
			encoder's own (NVENC: every 250 frames)."),
	Setting("denoise", "None", str, section=ENC, choices=("None", "atadenoise"),
		help="Temporal denoising before encoding (default: None). 'atadenoise' runs ffmpeg's atadenoise \
			(s=5) on each frame first, on one CPU thread per camera: each pixel is averaged with the \
			same pixel in the 2 frames before and after while they differ by at most 4 gray levels, so \
			no pixel changes by more than 4. Measured on mice: AV1 on Quick Sync files 20-28% smaller, \
			no measurable change in tracking (docs/encoding.md)."),
	Setting("fragmentedVideo", True, bool, section=ENC,
		help="Write .mp4 and .mov videos in fragments of at most a second (default: True), so that a \
			video whose ffmpeg is stopped before it closes the file (a power cut, an OS crash, ffmpeg \
			killed) is still readable up to its last fragment. A plain MP4 is then unreadable. \
			False writes a plain MP4."),
	Setting("vaapiDevice", "/dev/dri/renderD128", TEXT, section=ENC,
		help="VA-API render device for gpuMake 'vaapi' (default: /dev/dri/renderD128)."),
	Setting("vaapiLinearSurfaces", True, bool, section=ENC,
		help="gpuMake 'vaapi' on AMD GPUs: keep video surfaces linear (Mesa's AMD_DEBUG=novideotiling), \
			so each frame goes straight from the CPU into GPU memory instead of being copied there by \
			the GPU's compute engine (default: True)."),
	Setting("writeQueueMaxSec", 5, float, section=ENC, words=("None",), above=0,
		help="Most frames held waiting for the encoder, in seconds of video. When the encoder falls this \
			far behind, new frames are dropped and counted (frametimes.npy shows the gaps, metadata.csv \
			the count) instead of filling memory. 'None' = unbounded."),
	Setting("keepAwake", True, bool, section=REC, per_camera=ONE,
		help="Keep the computer out of sleep and standby while recording, screen included (default: \
			True). A Windows PC with Modern Standby otherwise goes into standby when its screen times \
			out, and the recording slows to a crawl."),

	# Recording watch and alerts (campy/watch.py, campy/notify.py, docs/alerts.md)
	Setting("alerts", True, bool, section=ALERTS, per_camera=ONE,
		help="Send the recording watch's alerts (a camera with no frames, dropped frames, a stalled \
			encoder, ffmpeg stopping, a disk filling up, the trigger gone) to the channels in the alerts \
			file, ~/.campy/alerts.yaml (default: True; without that file nothing is sent). They are said on \
			the console, and written to the recording's alerts.log, either way. See docs/alerts.md."),
	Setting("alertNoFramesSec", 10, float, section=ALERTS, per_camera=ONE, above=0,
		help="A camera that has sent frames and sends none for this many seconds is an alert (default: 10)."),
	Setting("alertTriggerPauseSec", "None", float, section=ALERTS, per_camera=ONE, words=("None",), above=0,
		help="A trigger that pauses on purpose, such as one that runs the cameras in bouts with long gaps \
			between them: a trigger pause at least this many seconds long is planned. It is written in the \
			recording's alerts.log, counted in recording.json and in the message as the recording ends, \
			but not sent as an alert (default: None = every trigger pause is a warning). Shorter pauses, \
			and a camera that misses pulses the others got, are alerts as before. Set it above the longest \
			pause that would be a fault, and alertNoFramesSec above the longest planned gap. See \
			docs/alerts.md."),
	Setting("alertExternalWaitSec", 600, float, section=ALERTS, per_camera=ONE, words=("None",), above=0,
		help="With an external trigger (startArduino False, and a camera with cameraTrigger set): no frame from any camera this many seconds \
			after campy started waiting for the trigger is a warning, 'No frame yet ... is it running?' \
			(default: 600, 10 minutes; None = never). Set it above how long campy can wait on purpose, \
			for a session that starts campy well before the device that makes the pulses. With the \
			trigger Arduino (startArduino True), no frame 10 s after it started is critical, whatever \
			this says. See docs/alerts.md."),
	Setting("alertDiskFreeGB", 20, float, section=ALERTS, per_camera=ONE, low=0,
		help="Less free space than this on a video folder's disk is an alert (default: 20)."),
	Setting("stopAtFreeGB", 2, float, section=ALERTS, per_camera=ONE, low=0,
		help="Below this much free space on a video folder's disk, campy stops the recording, cleanly, as \
			at Ctrl-C, so that every video and its metadata are whole: a full disk stops every ffmpeg at \
			once and leaves no room for them (default: 2; 0 = never)."),
	Setting("alertGpuLog", True, bool, section=ALERTS, per_camera=ONE,
		help="Linux with an AMD GPU: read the kernel log while recording, for the GPU's firmware stalls, hung \
			jobs and resets (default: True). It needs the adm or systemd-journal group; without it, campy \
			says so once and records without it."),
	Setting("alertGpuStalls", 3, int, section=ALERTS, per_camera=ONE, low=1,
		help="This many GPU firmware stalls ('Fence fallback timer expired on ring ...' in the kernel log) \
			within 10 minutes are an alert (default: 3)."),
	Setting("cameraReconnect", True, bool, section=ALERTS, per_camera=ONE,
		help="When a camera is unplugged mid-recording (or its USB link is lost), wait for it, and take it back \
			when it is plugged in again (default: True; FLIR and emulated cameras). Meanwhile the other cameras \
			record on. Its video, frametimes.npy and live feed go on from the trigger pulse it comes back on: \
			the frames while it was gone are a gap in its frame numbers, as dropped frames are, and its \
			timestamps are carried on across the gap, so campy-align puts every later frame on its pulse. \
			metadata.csv's cameraReconnects says when. False ends its recording when it is unplugged, as \
			before. See docs/alerts.md."),
	Setting("restartFfmpeg", False, bool, section=ALERTS, per_camera=ONE,
		help="When a camera's ffmpeg stops mid-recording (killed, crashed, an encoder error), start a new \
			one instead of stopping that camera (default: False). The camera never stops; its video goes \
			on in a new file (0.part2.mp4, ...), and segments.csv in its folder says which frames each \
			file holds. The frames inside the stopped ffmpeg (about a second) are lost. At most 3 restarts \
			per camera an hour, never when the disk is the cause, and never for a calibration video. \
			campy-session post's join step joins the files into one. See docs/alerts.md."),

	# Live feed
	Setting("liveFeed", False, bool, section=LIVE,
		help="Also copy every frame each camera grabs into shared memory, for a program that reads them \
			live, such as campy-live (default: False). One copy per frame: campy never waits for the \
			reader. campy-acquire turns it on by itself for its campy-live-view window (displayFrameRate \
			above 0), and campy-session when something reads it."),
	Setting("liveFeedName", "campy-live", TEXT, section=LIVE,
		help="Name of the live feed's shared memory: camera n's is <liveFeedName>-<n> (default: \
			campy-live). Two campys running at once each need their own."),
	Setting("liveFeedSlots", 8, int, section=LIVE, per_camera=ONE, low=1,
		help="Frames each camera's live feed holds (default: 8): how far behind a reader can be and \
			still read a frame."),

	# Launch check
	Setting("preflight", "error", TEXT, section=CHECK, per_camera=ONE, choices=("error", "warn", "off"),
		fold_case=True,
		help="Launch check of USB bandwidth, encoder capacity and disk: 'error' (stop if the configuration \
			does not fit), 'warn' (report only) or 'off'."),
	Setting("capacityFile", "None", TEXT, section=CHECK, per_camera=ONE,
		help="Encoder capacity table written by campy-encode-sweep (default: ~/.campy/capacity.json)."),
	Setting("usbBudgetMBps", "None", float, section=CHECK, per_camera=ONE, words=("None",), above=0,
		help="Most camera data per USB bus, in MB/s, for the launch check. Default: by bus speed."),

	# Display
	Setting("chunkLengthInSec", 5, float, section=DISPLAY, above=0,
		help="Length of video chunks in seconds for reporting recording progress."),
	Setting("statusLine", "auto", str, section=DISPLAY, per_camera=ONE, choices=("auto", "always", "never"),
		fold_case=True,
		help="While recording, one status line on the console, rewritten in place: the time, each \
			camera's frames, the frame rate, the trigger's pauses (docs/acquire.md). It replaces each \
			camera's 'collected N frames' line every chunkLengthInSec. Other messages still print as \
			lines of their own. 'auto' (default): only when the output is a console, so a log file or a \
			launcher's log keeps the lines as before; 'always'; 'never'."),
	Setting("displayFrameRate", 10, float, section=DISPLAY, low=0,
		help="Frames per second of the one campy-live-view window that shows every camera (default: 10). \
			campy-acquire run by itself opens it, with the live feed on (liveFeed); campy-session opens its \
			own instead (its view: section; without one, the view shows when this is above 0). 0 = no window \
			(over SSH, headless). There is no window per camera."),
	Setting("displayDownsample", 2, int, section=DISPLAY, low=1,
		help="Ignored: it set the old per-camera preview windows' downsampling, and campy has none any more \
			(campy-live-view scales each camera to its tile). Still accepted, so older configs load."),

	# Trigger
	Setting("triggerController", "arduino", str, section=TRIGGER, per_camera=ONE,
		choices=("arduino", "Arduino", "pico", "Pico", "box", "Box", "None", "none"),
		help="The board that makes the cameras' trigger pulses. 'arduino': campy's Arduino, which campy starts \
			itself with startArduino True. 'pico': the ChronicRecord Pico (campy-session's pico: section, or \
			chronic_control.py), never started by campy-acquire: startArduino must be False. 'box': an operant \
			box's board, which campy-box (campy-session's box: section) runs, makes them too: one Nano ESP32 for \
			the cameras and the box (docs/design/single-board-box.md); digitalPins are the cameras' pins on it and \
			syncPin its sync output; never started by campy-acquire: startArduino must be False. 'None' (or 'none'): \
			campy starts no trigger, even with startArduino True. One board per rig: set it in the rig profile, \
			and a config that extends the profile can't name the other board (only None), nor start the \
			Arduino or have a pico: section when the profile names the other one."),
	Setting("startArduino", False, bool, section=TRIGGER, per_camera=ONE,
		help="If True, start Arduino after initializing cameras."),
	Setting("serialPort", "COM3", str, section=TRIGGER, per_camera=ONE,
		help="Serial port for communicating with Arduino."),
	Setting("digitalPins", [0, 1, 2, 3, 4, 5, 6], int, section=TRIGGER, per_camera=LIST, low=0,
		help="Digital pins on microcontroller board for sending TTL camera triggers."),
	Setting("readyTimeoutSec", 30, float, section=TRIGGER, per_camera=ONE, low=0,
		help="Longest wait for every camera to be grabbing before campy starts the trigger anyway (and \
			says which cameras were not ready)."),
	Setting("blinkPin", "None", int, section=TRIGGER, per_camera=ONE, words=("None",), low=0,
		help="Arduino pin driving a test LED that every camera can see, for campy-synctest."),
	Setting("blinkHz", "None", float, section=TRIGGER, per_camera=ONE, words=("None",), low=0,
		help="Test LED blink rate. Pick one that is not near a whole or half multiple of the frame rate \
			(e.g. 43 Hz at 25 or 60 fps)."),
	Setting("syncPin", "None", int, section=TRIGGER, per_camera=ONE, words=("None",), low=0,
		help="Arduino pin for a sync output: pulses with random gaps, from the trigger's clock, for a \
			recorder with a clock of its own (ephys, photometry, a DAQ). Wire it to one of the recorder's \
			inputs; campy-syncmatch then puts the recorder's time on the trigger's, frame by frame \
			(docs/sync.md). Needs the current trigger.ino, a board with a crystal for the best fit. \
			None: off."),
	Setting("syncSeed", "None", int, section=TRIGGER, per_camera=ONE, words=("None",), low=0, high=2 ** 31 - 1,
		help="The sync output's seed, which sets its gaps. None: a new random one for each recording. \
			Either way it is saved in the recording's sync.json."),
	Setting("syncPulseMs", 10, float, section=TRIGGER, per_camera=ONE, above=0,
		help="How long each sync pulse is high, in ms."),
	Setting("syncGapMs", [200, 1000], int, section=TRIGGER, per_camera=LIST, low=1, high=60000,
		help="The sync pulses' shortest and longest gap (rise to rise), in whole ms. Each gap is one of \
			these values, at random, so any four in a row say where in the recording they are."),
	Setting("picoSync", False, bool, section=TRIGGER, per_camera=ONE,
		help="campy-session's Pico (pico: section) also makes its sync train on GP6 for the OneBox (wired to \
			XA1): 10 ms pulses, a random 200 to 1000 ms apart, from a seed saved in the recording's pico \
			folder, so chronicrecord/sync_match.py can line the Pico's clock up with the OneBox's and find any \
			pulse either missed (default: False). Needs chronic_timer 3 on the Pico. Set it in the rig profile."),

	# Session: the experiment, for the recording's session.json (campy/manifest.py), and the
	# warnings for defaults that changed (campy/defaults.py)
	Setting("experiment", "None", TEXT, section=SESSION, per_camera=ONE,
		help="The experiment's name, for the recording's session.json."),
	Setting("animalIds", [], ID, section=SESSION, per_camera=LIST,
		help="The animals recorded, e.g. [M1, M2, M3]: one list for the whole rig, kept in session.json. \
			One ID alone is a list of one."),
	Setting("notes", "None", TEXT, section=SESSION, per_camera=ONE,
		help="Notes on this recording, for session.json. Notes after it: campy-session note <folder> \"text\"."),
	Setting("sessionFields", {}, MAPPING, section=SESSION, per_camera=ONE,
		help="Anything else to keep with the recording in session.json, as a mapping: \
			{cage: B3, condition: saline}."),
	Setting("defaultsVersion", "None", str, section=SESSION, per_camera=ONE,
		help="The campy version whose defaults this config was written for, e.g. \"2.1.0\": campy then \
			says only the defaults changed after it that the config relies on (campy/defaults.py). \
			None: every one."),
	Setting("defaultWarnings", True, bool, section=SESSION, per_camera=ONE,
		help="Say, at startup, each default that changed in a newer campy and that this run relies on \
			(default: True). Setting a key, or defaultsVersion, stops one."),

	# Sections other programs read: campy-acquire takes them as they are (no
	# command-line option), and combines them across extends: one level deep
	Setting("live", None, SECTION, section=PROGRAMS, per_camera=ONE,
		help="campy-session: live tracking while recording (models, calibration, animals, the live \
			environment's python). campy-acquire passes it by; campy-session checks it."),
	Setting("view", None, SECTION, section=PROGRAMS, per_camera=ONE,
		help="campy-session: the live view (campy-live-view); view: true or false is short for view: {show: \
			true}. campy-acquire passes it by."),
	Setting("post", None, SECTION, section=PROGRAMS, per_camera=ONE,
		help="campy-session: what to do after the recording (remux, join, align, export, copy). campy-acquire passes \
			it by."),
	Setting("panel", None, SECTION, section=PROGRAMS, per_camera=ONE,
		help="campy-session: its control panel. campy-acquire passes it by."),
	Setting("pico", None, SECTION, section=PROGRAMS, per_camera=ONE,
		help="campy-session: a Raspberry Pi Pico that makes the trigger and the experiment's TTLs \
			(chronicrecord/chronic_control.py): its session's lengths, port, settings. The panel starts it once every \
			camera is ready. campy-acquire passes it by."),
	Setting("box", None, SECTION, section=PROGRAMS, per_camera=ONE,
		help="campy-session: an operant box's task (campy-box, campy/box.py) on the outputs board, started before \
			the cameras so that it begins with the first frame, and ended with the recording: its task file, port, \
			whether the recording needs it. campy-acquire passes it by."),
	Setting("spikeglx", None, SECTION, section=PROGRAMS, per_camera=ONE,
		help="campy-session: SpikeGLX's run, started into the recording's folder before the cameras and stopped \
			after them, through its command server and SDK. campy-acquire passes it by."),
	Setting("windows", None, SECTION, section=PROGRAMS, per_camera=ONE,
		help="campy-session: warn before a recording that Windows Update could restart the PC during it, or that \
			USB selective suspend is on. campy-acquire passes it by."),
]

BY_NAME = {s.name: s for s in SETTINGS}


def Defaults():
	# What a config that says nothing gets (cameraNames and cameraSelection are filled in later)
	import copy
	return {s.name: copy.deepcopy(s.default) for s in SETTINGS
		if s.default is not FILLED and s.replaced_by is None and s.kind is not SECTION}


def Reference():
	"""docs/config-reference.md: every setting, by section, from this table."""
	def Default(s):
		if s.default is FILLED:
			return "from numCams"
		if s.replaced_by:
			return "replaced by `{}`".format(s.replaced_by)
		if s.kind is SECTION:
			return "none"
		return "`{}`".format(s.default if not isinstance(s.default, str) else '"{}"'.format(s.default))

	lines = [
		"# campy-acquire settings",
		"",
		"<!-- Generated from campy/configschema.py by `campy-config reference > docs/config-reference.md`. "
		"Edit the table there, not this page: a test fails when the two differ. -->",
		"",
		"Every key a campy config file can have, and its command-line option (`--key value` means what "
		"`key: value` means in the file). \"Per camera\" says how a list is read: *one, or one per camera* "
		"takes one value for every camera or a list with a value for each, in camera order; *one per camera* "
		"(`cameraNames`, `cameraSelection`) needs a list with a value for each camera, and takes one value alone "
		"only with `numCams: 1`; *one for the rig* refuses a list. A number is never `.inf` or `.nan`. A config "
		"can also start with `extends: <rig profile or file>`, which takes every setting the config doesn't give "
		"from that file. [Configs](configs.md) explains profiles and how campy "
		"checks a config; `campy-config show` prints where each setting of a config came from.",
		"",
	]
	for section in SECTIONS:
		lines += ["## " + section, "", "| Key | Default | Takes | Per camera | What it does |",
			"|---|---|---|---|---|"]
		for s in SETTINGS:
			if s.section == section:
				lines.append("| `{}` | {} | {} | {} | {} |".format(s.name, Default(s), s.Takes(), s.Lists(),
					s.help.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")))
		lines.append("")
	return "\n".join(lines)
