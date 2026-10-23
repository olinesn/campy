"""
campy-session: a tracked recording in one command.

  campy-session run <config>.yaml [--key value ...]
  campy-session register-live          (once, in campy-live's environment)

A tracked recording is three programs in two Python environments:
campy-acquire records (the recording environment, with the camera drivers),
campy-live tracks the animals in the live feed (an environment with torch and
sleap-nn), and campy-live-view shows it. campy-session starts them in the
right order, in the right environments, from one config, and ends them
together:

1. It reads the config. Besides campy-acquire's keys, it may hold a live:
   section (campy-live's models, calibration, animals...) and a view:
   section (campy-live-view). It checks them, then writes into the
   recording's folder a copy of the config as given, and the config
   campy-acquire runs: the same without those sections, with liveFeed on.
2. It makes the recording's folder (<videoFolder>/<date_time>/, as
   campy-acquire would) and hands it to campy-acquire (sessionPath), so that
   every log, campy-live's results and the bookmarks go into it from the
   start. Each program's output goes to <folder>/logs/<program>.log, and is
   shown here too, each line marked with the program's name.
3. It starts campy-live, and waits for it to be ready (--ready-file): its
   models loaded and warmed up, 5 to 40 s. If it fails, the recording goes
   on without tracking (live.required false, the default), or does not
   start (true).
4. It starts campy-live-view (view.show), then campy-acquire.
5. The recording ends at recTimeInSec, at Ctrl-C, or with campy-acquire's
   stop file (stopFile), which campy-session writes at Ctrl-C too.
   campy-live then ends by itself, once the feed has, with every frame
   tracked; the view is closed (unless view.keep_open).

The recording comes first:

- campy-acquire is never killed. It runs as it does when started by hand:
  in this console, so Ctrl-C reaches it and its camera processes directly,
  with its own recTimeInSec. Its output goes into its log file, not through
  campy-session (a pipe breaks when campy-session dies). It is not tied to
  campy-session either: if campy-session crashes or is killed, the recording
  goes on to its end, and Ctrl-C still stops it.
- campy-live is not tied to campy-session, and it is started apart from the
  console's Ctrl-C (it would stop before the recording's last frames): if
  campy-session dies, it tracks on to the end of the recording. It waits for
  the feed for at most live.wait_s, and stops waiting if campy-session
  ends before the recording began (--launcher-pid). If it dies while the
  cameras record, it is started again (live.restart, on by default: after
  10 s, 1 min, then 5 min, at most 5 times an hour), into a log of its own
  (campy-live.part2, ...). It is stopped with its own stop file, never killed, unless it does
  not stop within a minute after the recording ended.
- campy-live-view is only a display: it is tied to campy-session
  (campy/children.py) and dies with it.

Finding campy-live's environment (its python): live.python in the config,
else the CAMPY_LIVE_PYTHON environment variable, else the one registered
with `campy-session register-live` run in that environment (written to
~/.campy/envs.json). It is run as <python> -m campy.live, so no PATH or
activation is needed, on Windows or Linux. Both environments must run the
same campy live feed code (campy/livefeed.py, compared by its contents), or
campy-session stops before anything starts.

Status: <folder>/launcher.json (and ~/.campy/status/launcher.json, the state
of this computer's latest session) says how each program started and ended,
rewritten every 2 s. Only one campy-session runs at a time on a computer
(~/.campy/status/launcher.lock).
"""

import argparse, collections, difflib, hashlib, json, os, signal, subprocess, sys, tempfile, time
from datetime import datetime

import yaml

from campy import children, newfiles, stopfile

SCHEMA = 1 # launcher.json's: bumped when a field changes meaning
VERSION = "campy-launcher/{}".format(SCHEMA)
LAUNCHER_ENV = "CAMPY_LAUNCHER" # set in campy-acquire's environment: who started it

# The config's sections campy-session reads, and campy-acquire never sees.
# Each key: (the kinds it takes, its default, what it is)
LIVE = {
	"python": ((str,), "", "campy-live's environment's python (default: CAMPY_LIVE_PYTHON, or register-live's)"),
	"model": ((str,), "sleap", "campy-live --model: sleap (trained models), random or none"),
	"centroid": ((str,), "", "the trained centroid model's folder (--centroid)"),
	"instance": ((str,), "", "the trained centered-instance model's folder (--instance)"),
	"animals": ((int,), 0, "how many animals (--animals, and --max-instances unless args give it); 0: campy-live's default"),
	"calibration": ((str,), "", "calibration.toml, for 3D (--calibration)"),
	"frame_shape": ((str,), "", "--frame-shape, if not the cameras' frameHeight x frameWidth"),
	"args": ((list,), [], "more campy-live options, as a list: [\"--engine\", \"off\"]"),
	"env": ((dict,), {}, "environment variables for campy-live: {CUDA_VISIBLE_DEVICES: \"0\"}"),
	"required": ((bool,), False, "true: no recording if campy-live can't start; false: record without tracking"),
	"ready_timeout_s": ((int, float), 120, "how long to wait for campy-live's models (a first engine build: ~40 s)"),
	"wait_s": ((int, float), 600, "how long campy-live waits for the feed, at most (--wait-sec)"),
	"restart": ((bool,), True, "start campy-live again if it dies while the cameras record"),
	}
VIEW = {
	"show": ((bool,), True, "start campy-live-view (view: true or false says it alone)"),
	"args": ((list,), [], "more campy-live-view options, as a list"),
	"keep_open": ((bool,), False, "leave the view open once the recording has ended"),
	}
POST = {
	"remux": ((bool, str), True, "fragmented MP4s to plain ones, checked packet by packet: true, keep (the fragmented "
		"one stays beside it) or false"),
	"export": ((bool,), True, "campy-live-export, when campy-live ran"),
	"copy": ((dict, str), {}, "copy the recording to copy.to/<its folder's name> (robocopy or rsync); nothing deleted"),
	"run": ((list,), [], "your own commands, {folder} replaced by the recording's folder"),
	"ffmpeg": ((str,), "", "the ffmpeg to remux with (default: imageio-ffmpeg's)"),
	}
POST_DEFAULTS = {key: default for key, (kinds, default, _) in POST.items()}
SECTIONS = {"live": LIVE, "view": VIEW, "post": POST}

RESTART_AFTER_S = (10, 60, 300) # campy-live started again after these, then every 300 s
RESTARTS_PER_HOUR = 5
LIVE_END_S = 60 # once the recording has ended, campy-live has this long to finish, then its stop file
LIVE_STOP_S = 30 # ... and this long after it, then it is ended
STATUS_EVERY_S = 2.0


class SessionError(Exception):
	pass


def Say(text):
	sys.stdout.write("[session] {}\n".format(text))
	sys.stdout.flush()


# ------------------------------------------------------------------ the config

def Suggest(word, words):
	close = difflib.get_close_matches(word, list(words), n=1, cutoff=0.6)
	return " Did you mean \"{}\"?".format(close[0]) if close else ""


def Section(name, given, keys):
	# The section's values, every key filled in, after checking each
	if given is None:
		given = {}
	if not isinstance(given, dict):
		raise SessionError("{}: in the config is not a section of keys (it is {!r}).".format(name, given))
	values = {}
	for key, value in given.items():
		if not isinstance(key, str):
			raise SessionError("{}: the key {!r} is not text (YAML reads yes, no, on and off as True or False: "
				"quote it, or see the keys below). It takes: {}.".format(name, key, ", ".join(keys)))
		if key not in keys:
			raise SessionError("{}.{}: campy-session has no such key.{} It takes: {}.".format(name, key,
				Suggest(key, keys), ", ".join(keys)))
		kinds = keys[key][0]
		if isinstance(value, bool) and bool not in kinds or not isinstance(value, kinds):
			raise SessionError("{}.{}: {!r} is not {}.".format(name, key, value, " or ".join(
				{str: "text", int: "a whole number", float: "a number", bool: "True or False", list: "a list",
				dict: "a section of keys"}[k] for k in kinds)))
		values[key] = value
	for key, (kinds, default, _) in keys.items():
		values.setdefault(key, default.copy() if isinstance(default, (list, dict)) else default)
	return values


def Overrides(args):
	# campy-acquire's --key value options, as values: each read as the config reads it (YAML)
	values, i = {}, 0
	while i < len(args):
		key = args[i]
		if not key.startswith("--") or i + 1 >= len(args):
			raise SessionError("{}: campy-session passes campy-acquire's options on as --key value.".format(key))
		try:
			values[key[2:]] = yaml.safe_load(args[i + 1])
		except yaml.YAMLError:
			values[key[2:]] = args[i + 1]
		i += 2
	return values


class Config:
	"""
	One config: campy-acquire's keys (acquire), with the command line's
	--key value on top (overrides), and campy-session's sections, checked
	(live, view: None when the section is not there).
	"""

	def __init__(self, path, overrides=()):
		self.path = os.path.abspath(path)
		try:
			with open(self.path, "rb") as f:
				raw = yaml.safe_load(f) or {}
		except (OSError, yaml.YAMLError) as e:
			raise SessionError("Could not read the config {}: {}".format(self.path, e))
		if not isinstance(raw, dict):
			raise SessionError("{} is not a campy config (a list of key: value).".format(self.path))
		self.text = open(self.path, encoding="utf-8", errors="replace").read()
		self.args = list(overrides)
		self.overrides = Overrides(self.args)
		if "extends" in raw:
			# A profile (extends:, config profiles) is combined by campy's configurator; the config campy-acquire runs
			# is written elsewhere (logs/), where a relative extends: would no longer point at the profile
			raise SessionError("extends: campy-session does not read profiles yet. Give it the whole config.")
		for key in ("sessionPath", "stopFile"):
			if key in raw or key in self.overrides:
				raise SessionError("{}: campy-session sets it itself; leave it out.".format(key))
		self.acquire = {k: v for k, v in raw.items() if k not in SECTIONS}
		self.live = Section("live", raw["live"], LIVE) if raw.get("live") is not None else None
		view = raw.get("view")
		self.view = Section("view", {"show": view} if isinstance(view, bool) else view, VIEW)
		if "view" not in raw:
			self.view["show"] = self.live is not None # without tracking, the cameras' own preview is there
		self.post = Section("post", raw.get("post"), POST)
		if self.post["remux"] not in (True, False, "keep"):
			raise SessionError("post.remux: {!r} is not true, false or keep.".format(self.post["remux"]))
		if isinstance(self.post["copy"], str):
			self.post["copy"] = {"to": self.post["copy"]}
		if set(self.post["copy"]) - {"to"}:
			raise SessionError("post.copy: it takes to: only (the folder the recording is copied into).")
		if self.live is not None and self.live["model"] == "sleap" and not (self.live["centroid"] and self.live["instance"]):
			raise SessionError("live: give the trained models' folders, centroid: and instance: (or model: random, "
				"or none).")

	def Value(self, key, default=None):
		# What campy-acquire will use: the command line's, the config's, or campy's default
		if key in self.overrides:
			return self.overrides[key]
		if key in self.acquire:
			return self.acquire[key]
		from campy import configurator
		return configurator.DefaultParams().get(key, default)

	def Feed(self):
		# The live feed is on whenever something reads it
		return self.live is not None or self.view["show"] or bool(self.Value("liveFeed"))

	def AcquireConfig(self):
		# The config campy-acquire runs: the same keys, without campy-session's sections; the feed on if read
		acquire = dict(self.acquire)
		if self.Feed():
			acquire["liveFeed"] = True
		return acquire

	def Cameras(self):
		n = int(self.Value("numCams", 1))
		names = self.Value("cameraNames")
		if not isinstance(names, list):
			names = ["Camera{}".format(c) for c in range(n)]
		return n, names[:n]


# ------------------------------------------------------------------ campy-live's environment

def CampyFolder():
	return os.path.expanduser(os.path.join("~", ".campy"))


def EnvsFile():
	return os.path.join(CampyFolder(), "envs.json")


def FeedCode():
	# What the two environments must share: the live feed's and results' layout
	import campy
	here = os.path.dirname(os.path.abspath(campy.__file__))
	digest = hashlib.sha1()
	for name in ("livefeed.py", "liveresults.py"):
		with open(os.path.join(here, name), "rb") as f:
			digest.update(f.read().replace(b"\r\n", b"\n"))
	return here, digest.hexdigest()


PROBE = """
import importlib.util, json, sys
from campy import session
here, code = session.FeedCode()
print(json.dumps({"python": sys.executable, "campy": here, "feed": code,
	"torch": importlib.util.find_spec("torch") is not None, "sleap_nn": importlib.util.find_spec("sleap_nn") is not None}))
"""


def Probe(python, env=None, timeout=120):
	# What campy the environment of python runs, and whether it has torch and sleap-nn
	try:
		# From the temp folder: python -c imports from its folder first, and a campy checkout there would win
		out = subprocess.run([python, "-c", PROBE], capture_output=True, text=True, timeout=timeout, env=env,
			cwd=tempfile.gettempdir())
	except (OSError, subprocess.TimeoutExpired) as e:
		raise SessionError("Could not run campy-live's python {}: {}".format(python, e))
	if out.returncode != 0:
		raise SessionError("campy-live's python {} could not import campy (pip install --no-deps -e <campy folder> "
			"in that environment):\n{}".format(python, (out.stderr or out.stdout).strip()[-1500:]))
	return json.loads(out.stdout.strip().splitlines()[-1])


def LivePython(live):
	"""campy-live's python, and where campy-session found it."""
	if live.get("python"):
		return live["python"], "live.python in the config"
	if os.environ.get("CAMPY_LIVE_PYTHON"):
		return os.environ["CAMPY_LIVE_PYTHON"], "CAMPY_LIVE_PYTHON"
	try:
		with open(EnvsFile()) as f:
			registered = json.load(f)["live"]["python"]
		return registered, "registered in {}".format(EnvsFile())
	except (OSError, ValueError, KeyError, TypeError):
		pass
	raise SessionError("campy-session does not know campy-live's environment. Once, in that environment, run:\n"
		"  campy-session register-live\n(or python -m campy.session register-live), or give its python as live.python "
		"in the config.")


def CheckLiveEnv(python, env=None):
	found = Probe(python, env)
	here, code = FeedCode()
	if found["feed"] != code:
		raise SessionError("campy-live's environment ({}) runs another campy than this one ({} against {}): its live "
			"feed code differs. Install the same campy in both (pip install --no-deps -e <campy folder>).".format(
			python, found["campy"], here))
	return found


def Register(argv=None):
	p = argparse.ArgumentParser(prog="campy-session register-live", description="Run in campy-live's environment: "
		"campy-session then starts campy-live, campy-live-view and campy-live-export with this python.")
	p.parse_args(argv)
	here, code = FeedCode()
	import importlib.util
	missing = [m for m in ("torch", "sleap_nn") if importlib.util.find_spec(m) is None]
	os.makedirs(CampyFolder(), exist_ok=True)
	try:
		with open(EnvsFile()) as f:
			envs = json.load(f)
	except (OSError, ValueError):
		envs = {}
	envs["live"] = {"python": sys.executable, "campy": here,
		"registered": datetime.now().astimezone().isoformat(timespec="seconds")}
	WriteJson(EnvsFile(), envs)
	print("campy-live's environment: {} (campy in {}), written to {}.".format(sys.executable, here, EnvsFile()))
	if missing:
		print("This environment has no {}: campy-live can run only --model none here.".format(" or ".join(missing)))
	return 0


# ------------------------------------------------------------------ files

def WriteJson(path, value):
	# In one step: written beside it, then moved into place (retried a moment on Windows, where a reader has it open)
	tmp = "{}.{}.tmp".format(path, os.getpid())
	with open(tmp, "w") as f:
		json.dump(value, f, indent=1)
	for attempt in range(20):
		try:
			os.replace(tmp, path)
			return
		except PermissionError:
			time.sleep(0.05)
	os.remove(tmp)


def Alive(pid):
	try:
		import psutil
		return psutil.pid_exists(pid)
	except ImportError:
		if sys.platform == "win32":
			return True
		try:
			os.kill(pid, 0)
			return True
		except OSError:
			return False


class Lock:
	# One campy-session per computer: a file made only if it is not there, with its pid
	def __init__(self):
		self.path = os.path.join(CampyFolder(), "status", "launcher.lock")
		self.held = False

	def Take(self):
		os.makedirs(os.path.dirname(self.path), exist_ok=True)
		for attempt in range(2):
			try:
				fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
			except FileExistsError:
				try:
					with open(self.path) as f:
						pid = int(f.read().split()[0])
				except (OSError, ValueError, IndexError):
					pid = None
				if pid is not None and pid != os.getpid() and Alive(pid):
					raise SessionError("Another campy-session is running on this computer (pid {}). Stop it first; "
						"{} is its lock.".format(pid, self.path))
				try:
					os.remove(self.path) # left by one that ended without removing it
				except OSError:
					pass
				continue
			with os.fdopen(fd, "w") as f:
				f.write("{} {}\n".format(os.getpid(), datetime.now().astimezone().isoformat(timespec="seconds")))
			self.held = True
			return self
		raise SessionError("Could not take campy-session's lock {}.".format(self.path))

	def Release(self):
		if self.held:
			try:
				os.remove(self.path)
			except OSError:
				pass
			self.held = False


# ------------------------------------------------------------------ the programs

class Tail:
	"""Shows a log file's new lines here as they are written, each marked with its program's name."""

	def __init__(self, path, name, out=None):
		self.path, self.name = path, name
		self.out = out or sys.stdout
		self.offset = 0
		self.partial = b""
		self.lines = collections.deque(maxlen=200) # the last ones, for the status

	def Read(self):
		try:
			with open(self.path, "rb") as f:
				f.seek(self.offset)
				data = f.read()
		except OSError:
			return
		if not data:
			return
		self.offset += len(data)
		data = self.partial + data
		*lines, self.partial = data.split(b"\n")
		for line in lines:
			text = line.decode("utf-8", "replace").rstrip("\r")
			self.lines.append(text)
			try:
				self.out.write("[{}] {}\n".format(self.name, text))
			except Exception:
				pass
		try:
			self.out.flush()
		except Exception:
			pass


class Part:
	"""
	One program of the session: its command, log file and process, and how
	it ended. tied: dies with campy-session (children.Child); apart: started
	apart from this console's Ctrl-C (its own process group); "console":
	apart from this console altogether (Windows: a hidden console of its
	own; Linux: a session of its own), so that closing campy-session's window
	or terminal does not end it either (the panel's recordings).
	"""

	def __init__(self, name, command, log, env=None, cwd=None, tied=False, apart=False):
		self.name, self.command, self.log = name, list(command), log
		self.env, self.cwd, self.tied, self.apart = env, cwd, tied, apart
		self.process = None
		self.started = self.ended = None
		self.code = None
		self.tail = Tail(log, name)

	def Start(self):
		os.makedirs(os.path.dirname(self.log), exist_ok=True)
		out = open(self.log, "ab")
		kwargs = dict(stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=self.env, cwd=self.cwd)
		try:
			if self.tied:
				self.process = children.Child(self.command, **kwargs)
			else:
				if self.apart:
					if sys.platform == "win32":
						kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP # no Ctrl-C from the console
						if self.apart == "console":
							kwargs["creationflags"] |= subprocess.CREATE_NO_WINDOW # a console of its own, hidden
					else:
						kwargs["start_new_session"] = True # nor the terminal's hang-up when it closes
				self.process = subprocess.Popen(self.command, **kwargs)
		finally:
			out.close() # the child has its own handle
		self.started = time.time()
		return self

	def Running(self):
		return self.process is not None and self.process.poll() is None

	def Check(self):
		# True once, when it has just ended
		self.tail.Read()
		if self.process is None or self.ended is not None:
			return False
		code = self.process.poll()
		if code is None:
			return False
		self.code, self.ended = code, time.time()
		self.tail.Read()
		return True

	def End(self, timeout=5):
		# Only for the parts that may be ended: the view, and campy-live once its stop file failed
		if self.Running():
			if isinstance(self.process, children.Child):
				self.process.Close(timeout)
			else:
				self.process.terminate()
				try:
					self.process.wait(timeout)
				except subprocess.TimeoutExpired:
					self.process.kill()
					self.process.wait()
		self.Check()

	def Status(self):
		return {"command": self.command, "log": self.log, "pid": self.process.pid if self.process else None,
			"started": Stamp(self.started), "ended": Stamp(self.ended), "exit_code": self.code,
			"state": "not started" if self.process is None else "running" if self.ended is None else "ended"}


def Stamp(t):
	return datetime.fromtimestamp(t).astimezone().isoformat(timespec="milliseconds") if t else None


def Python():
	# This environment's python, as a program to start (not pythonw: campy-acquire needs its console)
	exe = sys.executable
	if sys.platform == "win32" and os.path.basename(exe).lower() == "pythonw.exe":
		exe = os.path.join(os.path.dirname(exe), "python.exe")
	return exe


ACQUIRE = "import sys; sys.argv = ['campy-acquire'] + sys.argv[1:]; from campy.campy import Main; Main()"


class Session:
	"""One run of campy-session: run() starts the programs, waits, ends them, and returns campy-acquire's exit code."""

	def __init__(self, config, live_python=None, lock=True, detached=False):
		self.config = config
		self.live_python = live_python
		self.take_lock = lock # False: the panel holds campy-session's lock (campy/panel.py)
		# True (the panel): campy-acquire and campy-live apart from this console altogether, so that closing the
		# panel's window never ends a recording; its Stop, and Ctrl-C here, reach campy-acquire through its stop file
		self.detached = detached
		self.view_wanted = False # the panel's "Open the viewer again", for the main thread (Watch)
		self.folder = None
		self.state = "checking"
		self.parts = {}
		self.live_parts = [] # campy-live, and each time it was started again
		self.restarts = collections.deque() # when campy-live was started again
		self.restart_due = None
		self.problems = [] # what went wrong, in order: (time, text)
		self.presses = 0
		self.last_press = None
		self.status_at = 0
		self.stop_written = 0 # what campy-session asked of campy-acquire's stop file: 1 stop, 2 now
		self.post_results = {}

	# ---------------------------------------------------------- setup

	def Problem(self, text):
		self.problems.append((Stamp(time.time()), text))
		Say(text)

	def MakeFolder(self):
		base = os.path.abspath(str(self.config.Value("videoFolder", "./test")))
		if self.config.Value("sessionFolder", True):
			folder = newfiles.MakeSessionFolder({"videoFolder": base})
		else:
			folder = base
			os.makedirs(folder, exist_ok=True)
		self.folder = folder
		self.logs = os.path.join(folder, "logs")
		os.makedirs(self.logs, exist_ok=True)
		self.stopFile = os.path.join(self.logs, "campy-acquire.stop")
		with open(os.path.join(self.logs, "session-config.yaml"), "w", encoding="utf-8") as f:
			f.write("# {} as campy-session read it, {}; command-line options: {}\n".format(self.config.path,
				Stamp(time.time()), " ".join(self.config.args) or "none"))
			f.write(self.config.text)
		self.acquireConfig = os.path.join(self.logs, "campy-acquire.yaml")
		with open(self.acquireConfig, "w", encoding="utf-8") as f:
			f.write("# What campy-acquire ran: {} without campy-session's sections (live, view, post){}.\n".format(
				self.config.path, ", with liveFeed on" if self.config.Feed() else ""))
			yaml.safe_dump(self.config.AcquireConfig(), f, sort_keys=False, allow_unicode=True)
		return folder

	def LiveCommand(self, part):
		live, n = self.config.live, self.config.Cameras()[0]
		out = os.path.join(self.folder, "campy-live" if part == 1 else "campy-live.part{}".format(part))
		self.liveReady = os.path.join(self.logs, "campy-live{}.ready".format("" if part == 1 else ".part{}".format(part)))
		self.liveStop = os.path.join(self.logs, "campy-live.stop")
		command = [self.live_python, "-m", "campy.live", "--cameras", str(n),
			"--name", str(self.config.Value("liveFeedName", "campy-live")), "--model", live["model"],
			"--out", out, "--ready-file", self.liveReady, "--stop-file", self.liveStop, "--wait-sec", str(live["wait_s"]),
			"--launcher-pid", str(os.getpid())]
		if live["centroid"]:
			command += ["--centroid", live["centroid"]]
		if live["instance"]:
			command += ["--instance", live["instance"]]
		if live["animals"]:
			command += ["--animals", str(live["animals"])]
			if "--max-instances" not in live["args"]:
				command += ["--max-instances", str(live["animals"])]
		if live["calibration"]:
			command += ["--calibration", live["calibration"]]
		shape = live["frame_shape"] or self.FrameShape()
		if shape and "--frame-shape" not in live["args"]:
			command += ["--frame-shape", shape]
		return command + [str(a) for a in live["args"]]

	def FrameShape(self):
		try:
			h, w = int(self.config.Value("frameHeight")), int(self.config.Value("frameWidth"))
		except (TypeError, ValueError):
			return ""
		channels = 1 if str(self.config.Value("pixelFormatInput", "")) in ("gray", "mono8") else 3
		return "{}x{}x{}".format(h, w, channels)

	def LiveEnv(self):
		env = dict(os.environ)
		env.pop(newfiles.SESSION_ENV, None)
		env.update({str(k): str(v) for k, v in self.config.live["env"].items()})
		env["PYTHONUNBUFFERED"] = "1"
		return env

	# ---------------------------------------------------------- the run

	def Run(self):
		self.lock = Lock().Take() if self.take_lock else None
		previous = signal.signal(signal.SIGINT, self.Press)
		try:
			return self.Steps()
		finally:
			signal.signal(signal.SIGINT, previous)
			if self.lock is not None:
				self.lock.Release()

	def Steps(self):
		config = self.config
		if config.live is not None:
			self.live_python = self.live_python or LivePython(config.live)[0]
			found = CheckLiveEnv(self.live_python, self.LiveEnv())
			if config.live["model"] == "sleap" and not (found["torch"] and found["sleap_nn"]):
				raise SessionError("campy-live's environment ({}) has no torch or sleap-nn.".format(self.live_python))
		self.MakeFolder()
		Say("Recording into {}".format(self.folder))
		Say("Logs: {}".format(self.logs))
		self.Status()

		if config.live is not None and not self.StartLive(1, wait=True):
			if config.live["required"]:
				self.state = "failed"
				self.Problem("No recording: campy-live did not start, and live.required is true.")
				self.Status()
				return 1
		if self.presses:
			self.state = "stopped"
			self.Problem("Stopped (Ctrl-C) before the recording started: nothing was recorded.")
			self.EndLive()
			self.Status()
			return 1

		if config.view["show"]:
			self.StartView()

		env = dict(os.environ, PYTHONUNBUFFERED="1")
		env[LAUNCHER_ENV] = VERSION
		env.pop(newfiles.SESSION_ENV, None)
		acquire = Part("acquire", [Python(), "-c", ACQUIRE, self.acquireConfig, "--sessionPath", self.folder,
			"--stopFile", self.stopFile] + config.args, os.path.join(self.logs, "campy-acquire.log"), env=env,
			cwd=os.getcwd(), apart="console" if self.detached else False)
		self.parts["acquire"] = acquire.Start()
		self.state = "recording"
		self.Status()

		while not acquire.Check():
			self.Watch()
			time.sleep(0.2)
		self.state = "stopping"
		code = acquire.code
		if code == 0:
			Say("campy-acquire ended: every camera recorded to the end.")
		else:
			self.Problem("campy-acquire ended with exit code {}: see {}.".format(code, acquire.log))
		self.Status()
		self.EndLive()
		view = self.parts.get("view")
		if view is not None:
			if config.view["keep_open"] and view.Running():
				Say("The view stays open (view.keep_open): close it when you are done.")
			else:
				view.End()
		self.Post()
		self.state = "done" if code == 0 else "failed"
		self.Status()
		return code

	def Post(self):
		# After a clean stop (post.CleanStop reads launcher.json, just written): post.py; a Ctrl-C from here on stops
		# them between steps (ffmpeg, stopped mid-remux, leaves the original as it was)
		from campy import post
		self.state = "post"
		self.Status()
		pressed = self.presses
		self.post_results = post.Run(self.folder, self.config.post, live_python=self.live_python,
			stop=lambda: self.presses > pressed)

	def StartView(self):
		view = Part("view", [self.ViewPython(), "-m", "campy.liveview", "--cameras", str(self.config.Cameras()[0]),
			"--name", str(self.config.Value("liveFeedName", "campy-live"))] + [str(a) for a in self.config.view["args"]],
			os.path.join(self.logs, "campy-live-view.log"), env=self.ViewEnv(), tied=True)
		self.parts["view"] = view.Start()
		return view

	def ReopenView(self):
		# The panel's "Open viewer": a view again, while the cameras record, if the last one has closed
		view = self.parts.get("view")
		if self.state != "recording" or (view is not None and view.Running()):
			return False
		Say("Opening campy-live-view again.")
		self.StartView()
		return True

	def ViewPython(self):
		return self.live_python or Python()

	def ViewEnv(self):
		return self.LiveEnv() if self.config.live is not None else dict(os.environ, PYTHONUNBUFFERED="1")

	def StartLive(self, part, wait=False):
		live = Part("live" if part == 1 else "live.part{}".format(part), self.LiveCommand(part),
			os.path.join(self.logs, "campy-live.log" if part == 1 else "campy-live.part{}.log".format(part)),
			env=self.LiveEnv(), cwd=self.folder, apart="console" if self.detached else True)
		for path in (self.liveReady, self.liveStop):
			if os.path.exists(path):
				os.remove(path)
		self.live_parts.append(live.Start())
		self.parts["live"] = live
		if not wait:
			return True
		self.state = "starting live"
		Say("Starting campy-live (its models load and warm up: 5-40 s)...")
		t0 = time.monotonic()
		while not os.path.exists(self.liveReady):
			if live.Check():
				self.Problem("campy-live stopped as it started (exit code {}): see {}.{}".format(live.code, live.log,
					"" if self.config.live["required"] else " Recording without tracking."))
				return False
			if self.presses:
				return False
			if time.monotonic() - t0 > self.config.live["ready_timeout_s"]:
				self.Problem("campy-live was not ready after {:g} s (live.ready_timeout_s): see {}.{}".format(
					self.config.live["ready_timeout_s"], live.log, "" if self.config.live["required"] else
					" Recording without tracking."))
				self.EndLive()
				return False
			time.sleep(0.1)
		live.tail.Read()
		Say("campy-live is ready, after {:.1f} s.".format(time.monotonic() - t0))
		return True

	def Watch(self):
		# While the cameras record: the logs shown, campy-live started again if it died, the status written
		for name, part in list(self.parts.items()):
			if name == "acquire":
				continue
			if part.Check():
				if name.startswith("live"):
					self.LiveDied(part)
				elif name == "view":
					Say("campy-live-view closed (exit code {}).".format(part.code))
		if self.view_wanted:
			self.view_wanted = False
			self.ReopenView()
		if self.restart_due is not None and time.monotonic() >= self.restart_due:
			self.restart_due = None
			n = len(self.live_parts) + 1
			Say("Starting campy-live again (campy-live.part{}).".format(n))
			self.restarts.append(time.monotonic())
			self.StartLive(n)
		if time.monotonic() - self.status_at >= STATUS_EVERY_S:
			self.Status()

	def LiveDied(self, part):
		# campy-live ends by itself, exit code 0, once the feed has: as the cameras close, before campy-acquire ends
		if part.code == 0:
			Say("campy-live finished: the feed ended.")
			return
		self.Problem("campy-live stopped while the cameras record (exit code {}): see {}. The recording goes on.".format(
			part.code, part.log))
		if not self.config.live["restart"]:
			Say("It is not started again (live.restart is false).")
			return
		now = time.monotonic()
		while self.restarts and now - self.restarts[0] > 3600:
			self.restarts.popleft()
		if len(self.restarts) >= RESTARTS_PER_HOUR:
			self.Problem("campy-live stopped {} times within the hour: not started again.".format(len(self.restarts) + 1))
			return
		after = RESTART_AFTER_S[min(len(self.restarts), len(RESTART_AFTER_S) - 1)]
		self.restart_due = now + after
		Say("Starting it again in {} s.".format(after))

	def EndLive(self):
		# campy-live ends once the feed has; else its stop file, then it is ended
		self.restart_due = None
		live = self.parts.get("live")
		if live is None or not live.Running():
			if live is not None:
				live.Check()
			return
		Say("Waiting for campy-live to finish (it ends once the feed has)...")
		deadline = time.monotonic() + (LIVE_END_S if self.parts.get("acquire") else 0)
		asked = False
		while not live.Check():
			for part in self.parts.values():
				part.tail.Read()
			now = time.monotonic()
			if not asked and now >= deadline:
				stopfile.Write(self.liveStop)
				asked, deadline = True, now + LIVE_STOP_S
				Say("campy-live asked to stop (its stop file).")
			elif asked and now >= deadline:
				self.Problem("campy-live did not stop within {} s of its stop file: ended.".format(LIVE_STOP_S))
				live.End()
				break
			time.sleep(0.2)
		if live.code not in (0, None):
			self.Problem("campy-live ended with exit code {}: see {}.".format(live.code, live.log))

	def Press(self, sig, frame):
		# Ctrl-C reached campy-acquire and its cameras too (this console); the stop file says it again, for a
		# campy-acquire that a Ctrl-C did not reach. A press within a second of the last is the same press.
		now = time.monotonic()
		if self.last_press is not None and now - self.last_press < 1.0:
			return
		self.last_press = now
		self.presses += 1
		acquire = self.parts.get("acquire")
		if acquire is None:
			Say("Ctrl-C: stopping before the recording starts.")
			return
		if self.state == "post":
			Say("Ctrl-C: the steps after the recording stop after this one (campy-session post <folder> runs the rest).")
			return
		level = min(self.presses, 2)
		self.RequestStop(level)
		Say("Ctrl-C: campy-acquire stops its cameras{}.".format(" now, without closing the ones that hang" if level == 2
			else ", each closing its video: please wait"))

	def RequestStop(self, level=1):
		"""
		campy-acquire's stop file: 1 stops the recording as Ctrl-C does, 2
		is the second Ctrl-C (a camera that hangs as it closes). For Ctrl-C
		here, and the panel's Stop. True when it was written.
		"""
		acquire = self.parts.get("acquire")
		if acquire is None or not acquire.Running() or level <= self.stop_written:
			return False
		try:
			stopfile.Write(self.stopFile, now=level == 2)
		except OSError as e:
			Say("Could not write campy-acquire's stop file: {}".format(e))
			return False
		self.stop_written = level
		return True

	# ---------------------------------------------------------- status

	def StatusDict(self):
		acquire = self.parts.get("acquire")
		started = acquire.started if acquire is not None else None
		ended = acquire.ended if acquire is not None else None
		try:
			planned = float(self.config.Value("recTimeInSec", 0))
		except (TypeError, ValueError):
			planned = None
		return {"schema": SCHEMA, "launcher": VERSION, "updated_at": Stamp(time.time()), "state": self.state,
			"folder": self.folder, "config": self.config.path, "pid": os.getpid(),
			"manifests": ["session.json", "campy-live-session.json"],
			"parts": {name: part.Status() for name, part in self.parts.items() if not name.startswith("live")},
			"live": [part.Status() for part in self.live_parts],
			"live_restarts": len(self.live_parts) - 1 if self.live_parts else 0,
			"problems": [{"time": t, "text": text} for t, text in self.problems],
			"post": {step: {"state": state, "message": message} for step, (state, message) in self.post_results.items()},
			"recording": {"started": Stamp(started), "elapsed_s": round((ended or time.time()) - started, 1) if started else
				None, "planned_s": planned, "stop_asked": self.stop_written}}

	def Status(self):
		self.status_at = time.monotonic()
		if self.folder is None:
			return
		status = self.StatusDict()
		for path in (os.path.join(self.folder, "launcher.json"), os.path.join(CampyFolder(), "status", "launcher.json")):
			try:
				os.makedirs(os.path.dirname(path), exist_ok=True)
				WriteJson(path, status)
			except OSError:
				pass


# ------------------------------------------------------------------ the command

def RunMain(argv):
	p = argparse.ArgumentParser(prog="campy-session run", description="Record with live tracking from one config: "
		"campy-live, campy-live-view and campy-acquire, started and ended together (python -m pydoc campy.session).",
		epilog="Any other --key value is campy-acquire's, for this run (as campy-acquire <config> --key value).")
	p.add_argument("config", help="the config: campy-acquire's keys, and optional live: and view: sections")
	args, rest = p.parse_known_args(argv)
	try:
		config = Config(args.config, rest)
		session = Session(config)
		return session.Run()
	except SessionError as e:
		print("campy-session: {}".format(e), file=sys.stderr)
		return 1


def Main(argv=None):
	argv = sys.argv[1:] if argv is None else list(argv)
	from campy import post
	from campy import panel, shortcut
	commands = {"run": RunMain, "register-live": Register, "post": post.Main, "panel": panel.Main,
		"shortcut": shortcut.Main}
	if not argv or argv[0] not in commands:
		print("usage: campy-session run <config>.yaml [--key value ...]\n"
			"       campy-session post <recording folder> [--only remux,export] [--force]\n"
			"       campy-session panel <config>.yaml [--port 8765] [--no-browser]\n"
			"       campy-session shortcut <config>.yaml [--name ...]   (a desktop shortcut that opens the panel)\n"
			"       campy-session register-live   (once, in campy-live's environment)", file=sys.stderr)
		sys.exit(2)
	sys.exit(commands[argv[0]](argv[1:]))


if __name__ == "__main__":
	Main()
