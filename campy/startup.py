"""
What a camera says as it opens, said as one line once it is ready.

Cameras open at the same time, one process each, and each used to say every
setting as it set it ("Height set to 1024...", "Gain set to 3 dB.", "Buffer
count now set to: 100", "Opened: ...\\0.mp4 using GPU 0 ..."): with four
cameras the lines interleaved, and no line said which camera it was about.
Now each camera process holds those steps (Step) and the values they set
(Setting), and says them once, as one line, when the camera is ready to
trigger (Ready):

  Camera1 ready to trigger: flir Blackfly S BFS-U3-13Y3M serial 23501329, 1280x1024 at 80 fps, trigger Line3,
  exposure 750 us, gain 3 dB, gamma off, 100 buffers (oldest first), USB limit 380 MB/s (frameRate needs 79),
  hevc on GPU 0 -> C:\\campy_data\\...\\Camera1\\0.mp4

(one line in the console). Problems are said at once, with the camera's name
(Warn). A camera that is not ready (Failed) says every step it held, so what
it did before it failed is still there to read.

Only between Begin (the camera starts opening, unicam.OpenCamera) and Ready or
Failed: anywhere else (campy's tests, campy-encode-sweep, a setting changed
while recording) Step says its text at once, as it always did. Nothing here
changes what a camera does or when: what is held is only text, and Say never
waits (utils.Printer).
"""

import threading

from campy.utils.utils import Say

_lock = threading.Lock()
_current = None # this process's camera, while it opens (one camera per process)


class _Startup:
	def __init__(self, cam_params):
		self.cam_params = cam_params
		self.name = cam_params.get("cameraName", "camera")
		self.steps = [] # each step's text, in order
		self.settings = {} # what the summary says, by key, in the order set
		self.videoSaid = False # the video's first open, held (Video)


def Begin(cam_params):
	"""This camera starts opening: its steps are held from here on."""
	global _current
	with _lock:
		_current = _Startup(cam_params)


def Step(text):
	"""
	A step as a camera opens ("Gain set to 3 dB."): held while it opens, said
	with Failed if it does not get ready. Otherwise said at once, as before.
	"""
	with _lock:
		if _current is not None:
			_current.steps.append(str(text))
			return
	Say(text)


def Setting(key, text):
	"""What the ready line says for key ("gain": "gain 3 dB"); set again, replaced."""
	with _lock:
		if _current is not None:
			_current.settings[key] = str(text)


def Warn(text):
	"""A problem, said at once, after the camera's name while it opens."""
	with _lock:
		name = _current.name if _current is not None else None
	text = str(text)
	if name and not text.startswith(name):
		text = "{}: {}".format(name, text)
	Say(text)


def Video(text, cam_params, first):
	"""
	The writer's "Opened: <video> using ... to compress the stream." first: the
	recording's first open of its video, which the ready line names; that one
	is held (or left out, once, the ready line having already said it).
	Another (ffmpeg started again, a restart's next part) is said at once.
	"""
	global _said
	name = cam_params.get("cameraName")
	with _lock:
		if first and _current is not None and _current.name == name:
			_current.steps.append(str(text))
			return
		if first and _said is not None and _said.name == name:
			_said = None
			return
	Say(text)


_said = None # the camera whose ready line was said, in this process


def Ready(cam_params):
	"""
	The camera is ready to trigger: its one line. Without Begin, the line
	campy always said.
	"""
	global _current, _said
	with _lock:
		startup, _current = _current, None
		_said = startup
	if startup is None:
		Say("{} ready to trigger.".format(cam_params["cameraName"]))
		return
	Say(Summary(startup, cam_params))


def Failed(cam_params, why=""):
	"""
	The camera will not get ready: every step it held, so that what it did
	before it stopped is still said.
	"""
	global _current
	with _lock:
		startup, _current = _current, None
	if startup is None or not startup.steps:
		return
	name = startup.name
	Say("\n".join(["{} is not ready{}. What it did as it opened:".format(name, ": {}".format(why) if why else "")]
		+ ["  {}: {}".format(name, step.strip()) for step in startup.steps if step.strip()]))


def End():
	"""Forget this process's camera (tests that open several in one process)."""
	global _current, _said
	with _lock:
		_current = _said = None


def Summary(startup, cam_params):
	# "<name> ready to trigger: <make model serial>, <size> at <rate>, <settings...>, <video>"
	p = cam_params
	s = dict(startup.settings)
	parts = []
	camera = " ".join(str(x) for x in (p.get("cameraMake"), p.get("cameraModel")) if x not in (None, ""))
	serial = p.get("cameraSerialNo")
	if serial not in (None, ""):
		camera = "{} serial {}".format(camera, serial).strip()
	if camera:
		parts.append(camera)
	rate = s.pop("rate", None)
	if rate is None and p.get("frameRate") is not None:
		rate = "{:g} fps".format(float(p["frameRate"]))
	size = "{}x{}".format(p.get("frameWidth"), p.get("frameHeight"))
	if p.get("offsetX") or p.get("offsetY"):
		size = "{} (offset {},{})".format(size, p.get("offsetX") or 0, p.get("offsetY") or 0)
	parts.append("{} at {}".format(size, rate) if rate else size)
	if "trigger" not in s and p.get("cameraMake") != "ratcam":
		trigger = str(p.get("cameraTrigger", "None"))
		s = dict([("trigger", "no trigger (free-running)" if trigger == "None" else "trigger {}".format(trigger))],
			**s)
	parts.extend(s.values())
	video = VideoText(p)
	if video:
		parts.append(video)
	return "{} ready to trigger: {}".format(startup.name, ", ".join(parts))


def VideoText(cam_params):
	# "hevc on GPU 0 -> <path>", as the writer opens it (writer.EncoderArgs: gpuID -1 is the CPU)
	try:
		from campy import newfiles
		path = newfiles.VideoPath(cam_params)
	except Exception:
		return None
	gpu = cam_params.get("gpuID", -1)
	device = "CPU" if gpu == -1 or str(gpu) == "-1" else "GPU {}".format(gpu)
	codec = cam_params.get("codec")
	return "{} on {} -> {}".format(codec, device, path) if codec else "{} -> {}".format(device, path)
