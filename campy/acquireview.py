"""
campy-acquire's one window: every camera in one campy-live-view.

campy-acquire run by itself (from a prompt, not by campy-session) with
displayFrameRate above 0 shows its cameras in one campy-live-view window, at
displayFrameRate frames per second. campy has no window per camera any more.
To feed it, the live feed (liveFeed) is turned on for every camera: the
camera processes read the config again, so the main process tells them in
their environment (FEED_ENV), which livefeed.Feed reads. displayFrameRate 0
opens no window (over SSH, headless).

campy-session (and its panel) starts its own campy-live-view, with campy-live's
results when it tracks; campy-acquire then opens none (manifest.LAUNCHER_ENV
in its environment says campy-session started it).

The view is campy-live-view's Qt window (PyQt5, campy/liveviewqt.py), drawn
with OpenCV (opencv-python-headless, both in campy's requirements); without
PyQt5, OpenCV's own window where it has one. It is a program of its own
(python -m campy.liveview), at a low
priority (its --priority low), tied to campy-acquire (campy/children.py): it
dies with it, and is closed when the recording ends. Ctrl-C at the console
does not reach it, only campy-acquire. Its python is this
environment's when it has OpenCV, else campy-live's registered one
(campy-session register-live). Its output goes to campy-live-view.log in the
recording's folder. If it cannot start, or stops early (no OpenCV, no
desktop), campy says so once and records on.
"""

import importlib.util, os, subprocess, sys, tempfile, threading, time

from campy import children, livefeed, manifest
from campy.utils.utils import Say, ErrorText

FEED_ENV = livefeed.FEED_ENV # set by campy-acquire's main process for its camera processes: "1" turns liveFeed on
LOG = "campy-live-view.log"


def ByLauncher():
	"""Whether campy-session started this campy-acquire: it shows the cameras itself."""
	return bool(os.environ.get(manifest.LAUNCHER_ENV))


def Wanted(params):
	"""Whether this campy-acquire opens its own view: displayFrameRate above 0, and not run by campy-session."""
	try:
		rate = float(params.get("displayFrameRate", 0) or 0)
	except (TypeError, ValueError):
		return False
	return rate > 0 and not ByLauncher()


def Python():
	"""
	The python that runs the view, and None with why when there is none:
	this environment's when it has OpenCV, else campy-live's registered one.
	"""
	exe = sys.executable
	if sys.platform == "win32" and os.path.basename(exe).lower() == "pythonw.exe":
		exe = os.path.join(os.path.dirname(exe), "python.exe")
	if importlib.util.find_spec("cv2") is not None:
		return exe, None
	from campy import session
	try:
		python = session.LivePython({})[0]
	except session.SessionError:
		python = None
	if python and os.path.exists(python):
		return python, None
	return None, "this environment has no OpenCV (pip install opencv-python-headless: campy needs it since 2.5.2), " \
		"and no campy-live environment is registered (campy-session register-live)"


def Args(params):
	"""campy-live-view's options: the cameras, their feed, displayFrameRate, and the trigger's planned pauses."""
	args = ["--cameras", str(int(params["numCams"])), "--name", str(params.get("liveFeedName", "campy-live")),
		"--fps", "{:g}".format(float(params["displayFrameRate"]))]
	for key, option in (("alertTriggerPauseSec", "--pause-sec"), ("alertNoFramesSec", "--stop-sec")):
		value = params.get(key)
		try:
			if value not in (None, "None", "") and float(value) > 0:
				args += [option, str(float(value))]
		except (TypeError, ValueError):
			pass
	return args


class View:
	"""
	campy-acquire's campy-live-view (see above). Prepare() before the camera
	processes start (the feed on in their environment), Start() once they
	are, Stop() at the end. Meanwhile a thread checks it every second
	(Check): one that stopped early is said once. Nothing here ever stops
	the recording.
	"""

	def __init__(self, params, folder=None, say=Say):
		self.params, self.folder, self.say = params, folder, say
		self.on = Wanted(params)
		self.process = None
		self.log = None
		self.told = False
		self.stopping = False

	def Prepare(self):
		# The camera processes read it from their environment as they start
		if self.on:
			os.environ[FEED_ENV] = "1"
			self.params["liveFeed"] = True
		return self

	def Start(self, popen=children.Child):
		if not self.on:
			return self
		python, why = Python()
		if python is None:
			return self.Tell("No campy-live-view window: {}. Recording without it.".format(why))
		command = [python, "-m", "campy.liveview"] + Args(self.params)
		folder = self.folder if self.folder and os.path.isdir(self.folder) else tempfile.gettempdir()
		self.log = os.path.join(folder, LOG)
		try:
			with open(self.log, "ab") as out:
				env = dict(os.environ, PYTHONUNBUFFERED="1")
				env.pop(FEED_ENV, None)
				self.process = popen(command, stdout=out, stderr=out, stdin=subprocess.DEVNULL, env=env, **Apart())
		except Exception as e:
			self.process = None
			return self.Tell("campy-live-view did not start: {}. Recording without it.".format(ErrorText(e)))
		self.say("All {} cameras in one campy-live-view window, at {:g} fps (displayFrameRate; 0 for none). Its "
			"output: {}".format(self.params["numCams"], float(self.params["displayFrameRate"]), self.log))
		threading.Thread(target=self.Watch, daemon=True).start()
		return self

	def Watch(self, every=1.0):
		while self.process is not None and not self.told and not self.stopping:
			self.Check()
			time.sleep(every)

	def Check(self):
		# A view that ended while the cameras record: said once, with the last line it wrote
		if self.process is None or self.told or self.stopping:
			return
		process = self.process
		code = process.poll() if process is not None else None
		if code is None:
			return
		if code == 0:
			self.told = True # closed by hand: nothing to say
			return
		self.Tell("campy-live-view stopped (exit code {}){}. Recording goes on without it; see {}.".format(code,
			LastLine(self.log), self.log))

	def Tell(self, text):
		if not self.told:
			self.told = True
			self.say(text)
		return self

	def Stop(self):
		self.stopping = True
		os.environ.pop(FEED_ENV, None)
		process, self.process = self.process, None
		if process is None:
			return
		try:
			if hasattr(process, "Close"):
				process.Close()
			else:
				process.terminate()
				process.wait(timeout=5)
		except Exception as e:
			self.say("Could not close campy-live-view: {}".format(ErrorText(e)))


def Apart():
	# Out of the console's Ctrl-C, which is campy-acquire's to stop the cameras with: the view is closed after them
	if sys.platform == "win32":
		return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
	return {"start_new_session": True}


def LastLine(path):
	# The last line a log holds, as ": <line>", or nothing
	try:
		with open(path, "rb") as f:
			f.seek(max(0, os.path.getsize(path) - 4096))
			lines = [l.strip() for l in f.read().decode("utf-8", "replace").splitlines() if l.strip()]
	except (OSError, TypeError):
		return ""
	return ": {}".format(lines[-1]) if lines else ""
