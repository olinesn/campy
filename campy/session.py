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
4. It starts campy-live-view (view.show), then campy-acquire. Without a
   view: section, the view shows when campy-live tracks, or when
   displayFrameRate is above 0 (the cameras alone, at that rate). The
   campy-acquire it starts opens no window of its own (CAMPY_LAUNCHER in its
   environment: campy/acquireview.py).
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
  goes on to its end, and Ctrl-C still stops it. If campy-acquire ends with
  an error and its watch never said how the recording ended (killed,
  crashed: recording.json not ended or failed), campy-session sends a
  critical alert itself (AcquireEnded); one for campy-watch stopping with an
  error, a warning.
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

from campy import children, newfiles, stopfile, strictjson
from campy.utils.utils import ExitCodeText

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
	"calibration": ((str,), "", "calibration.toml, for 3D (--calibration); or a folder of calibration recordings: "
		"the newest one's calibration.toml that campy-calibrate made whole (campy/calibrate.py's Newest)"),
	"frame_shape": ((str,), "", "--frame-shape, if not the cameras' frameHeight x frameWidth"),
	"args": ((list,), [], "more campy-live options, as a list: [\"--engine\", \"off\"]"),
	"env": ((dict,), {}, "environment variables for campy-live: {CUDA_VISIBLE_DEVICES: \"0\"}"),
	"required": ((bool,), False, "true: no recording if campy-live can't start; false: record without tracking"),
	"ready_timeout_s": ((int, float), 120, "how long to wait for campy-live's models (a first engine build: ~40 s)"),
	"wait_s": ((int, float), 600, "how long campy-live waits for the feed, at most (--wait-sec)"),
	"restart": ((bool,), True, "start campy-live again if it dies, or hangs, while the cameras record"),
	"watch": ((bool,), True, "start campy-watch beside campy-live: alerts about the live tracking (docs/alerts.md)"),
	}
VIEW = {
	"show": ((bool,), True, "start campy-live-view (view: true or false says it alone; without a view: section, "
		"when campy-live tracks or displayFrameRate is above 0)"),
	"args": ((list,), [], "more campy-live-view options, as a list"),
	"keep_open": ((bool,), False, "leave the view open once the recording has ended"),
	}
POST = {
	"remux": ((bool, str), True, "fragmented MP4s to plain ones, checked packet by packet: true, keep (the fragmented "
		"one stays beside it) or false"),
	"join": ((bool,), True, "a camera's video files after an ffmpeg restart (restartFfmpeg) joined into one, "
		"without re-encoding, checked packet by packet"),
	"align": ((bool,), True, "campy-align: every camera on the trigger's timeline (aligned.csv, alignment.json), and "
		"where the trigger paused"),
	"export": ((bool,), True, "campy-live-export, when campy-live ran"),
	"calibrate": ((dict,), {}, "for a calibration recording: calibrat3 (headless, in this computer's Chrome) makes its "
		"calibration.toml: {calibrat3: <its checkout>, board: {board_x: 14, board_y: 9, square_length: 20, "
		"marker_length: 15, marker_bits: 5, dict_size: 1000}, target: 300 (frames sampled), browser: chrome} "
		"(campy/calibrate.py); {} (the default): off"),
	"copy": ((dict, str), {}, "copy the recording to copy.to/<its folder's name> (robocopy or rsync); nothing deleted"),
	"run": ((list,), [], "your own commands, {folder} replaced by the recording's folder"),
	"ffmpeg": ((str,), "", "the ffmpeg to remux with (default: imageio-ffmpeg's)"),
	}
PANEL = {
	"rig_lock": ((str,), "", "rig.py (bench/rig.py, installed on the rig): the panel holds the rig's lock while it is "
		"open, and Ctrl-C in its window does nothing (the page's Close ends it); \"auto\": the rig's installed one"),
	"owner": ((str,), "panel", "who holds the rig lock, as rig.py status shows it"),
	"title": ((str,), "", "the experiment's name on a panel opened with a folder of configs (its Experiment list; "
		"default: the file's name); before any star on that page, the configs with a title are its favourites. Only "
		"the file that says it: a config that extends it isn't given its title"),
	"models": ((list, str), [], "folders the panel's model picker looks through for trained models, for live: model: "
		"sleap (campy/livemodels.py); a rig profile's place, as it turns on nothing"),
	"model_sets": ((dict,), {}, "named model pairs for the panel's model picker: {name: {centroid: folder, instance: "
		"folder}}"),
	"led_power": ((list,), [], "the LEDs whose power the page asks for at each Start, e.g. [\"Blue LED (465 nm)\", "
		"\"Yellow LED (560 nm)\"]: each one's driver current (mA) and the power measured at the fiber tip (µW), before "
		"the animal is connected; kept in the recording's session.json (sessionFields.ledPower). Empty: not asked"),
	"video_mb_s": ((int, float), 0.6, "each camera's video while it takes frames, in MB/s, for the panel's disk check "
		"before a Pico's session: an estimate on the high side (0.6: 60 fps H.265 of mice in a lit cage; a still, "
		"empty cage writes far less)"),
	}
VIEW["python"] = ((str,), "", "the python that runs campy-live-view (default: campy-live's, when the config has a "
	"live: section or campy-live's environment is registered; else this one)")
POST_DEFAULTS = {key: default for key, (kinds, default, _) in POST.items()}


def DeviceSections():
	# The devices around a recording (campy/devices.py): the Pico, SpikeGLX, Windows' settings
	from campy import pico, spikeglx, winsetup
	return {"pico": pico.KEYS, "spikeglx": spikeglx.KEYS, "windows": winsetup.KEYS}


SECTIONS = dict({"live": LIVE, "view": VIEW, "post": POST, "panel": PANEL}, **DeviceSections())

def TestSeconds(name, default):
	# A wait that a test can shorten through the environment (CAMPY_TEST_*: tests/test_session.py), so that
	# it needn't record for minutes; nothing else sets these
	value = os.environ.get(name)
	return tuple(float(v) for v in value.split(",")) if value and isinstance(default, tuple) else \
		float(value) if value else default


RESTART_AFTER_S = TestSeconds("CAMPY_TEST_LIVE_RESTART_S", (10, 60, 300)) # campy-live started again after these,
# then every 300 s
RESTARTS_PER_HOUR = 5
LIVE_END_S = 60 # once the recording has ended, campy-live has this long to finish, then its stop file
LIVE_STOP_S = TestSeconds("CAMPY_TEST_LIVE_STOP_S", 30) # ... and this long after it, then it is ended
LIVE_STALL_S = TestSeconds("CAMPY_TEST_LIVE_STALL_S", 30) # campy-watch's (livewatch.STALL_S): no result this long
# while the cameras record, and campy-live hangs
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
	One config, read as campy-acquire reads it (campy's configurator: its
	checks, and profiles, extends:), with the command line's --key value on
	top: every campy-acquire setting (acquire), and campy-session's sections,
	checked (live, view, post; live None when there is none).
	"""

	def __init__(self, path, overrides=(), live=None, pico_set=None):
		from campy import configurator
		self.path = os.path.abspath(path)
		# live: keys the panel's model picker set for this recording ({centroid, instance}), over the config's
		self.picked = dict(live or {})
		# pico.set's settings the panel's page set for this recording ({setting: value}), over the config's
		self.pico_page = dict(pico_set or {})
		try:
			with open(self.path, encoding="utf-8", errors="replace") as f:
				self.text = f.read()
		except OSError as e:
			raise SessionError("Could not read the config {}: {}".format(self.path, e))
		self.args = list(overrides)
		self.overrides = Overrides(self.args)
		for key in ("sessionPath", "stopFile"):
			if key in self.overrides:
				raise SessionError("{}: campy-session sets it itself; leave it out.".format(key))
		self.resolved = self.Resolve()
		self.acquire = self.resolved.params
		self.replaced = []
		for key in ("sessionPath", "stopFile"):
			if str(self.acquire.get(key, "None")) not in ("None", ""):
				source = self.resolved.sources.get(key)
				if key == "stopFile" and getattr(source, "path", None) and os.path.abspath(source.path) != self.path:
					# A profile's, for its own launchers: campy-session's stop file replaces it, as on the command line
					self.replaced.append("stopFile {} (from {}): campy-session uses its own".format(self.acquire[key],
						source.path))
					continue
				raise SessionError("{}: campy-session sets it itself; leave it out of the config.".format(key))
		sections = self.resolved.sections
		self.live = Section("live", sections["live"], LIVE) if "live" in sections else None
		self.view = Section("view", sections.get("view"), VIEW)
		if "view" not in sections:
			# Without tracking, the view shows the cameras alone, as campy-acquire's own window does (displayFrameRate)
			self.view["show"] = self.live is not None or self.DisplayRate() > 0
		self.post = Section("post", sections.get("post"), POST)
		self.panel = Section("panel", sections.get("panel"), PANEL)
		if self.post["remux"] not in (True, False, "keep"):
			raise SessionError("post.remux: {!r} is not true, false or keep.".format(self.post["remux"]))
		if isinstance(self.post["copy"], str):
			self.post["copy"] = {"to": self.post["copy"]}
		if set(self.post["copy"]) - {"to"}:
			raise SessionError("post.copy: it takes to: only (the folder the recording is copied into).")
		for name, keys in DeviceSections().items():
			setattr(self, name, Section(name, sections[name], keys) if name in sections else None)
		self.CheckPicker()
		if self.live is not None and self.live["model"] == "sleap" and not (self.live["centroid"] and self.live["instance"]) \
				and not (self.panel["models"] or self.panel["model_sets"]):
			raise SessionError("live: give the trained models' folders, centroid: and instance: (or model: random, "
				"or none).")

	def CheckPicker(self):
		# The model picker's (campy/livemodels.py), in panel: rather than live:, so that a rig profile can name them without
		# turning on live tracking for every experiment: panel.models, one folder or a list of them, from the folder of
		# the config that says it; panel.model_sets, {name: {centroid, instance}}
		panel = self.panel
		source = self.resolved.section_sources.get("panel", {}).get("models")
		here = os.path.dirname(source.path) if getattr(source, "path", None) else os.path.dirname(self.path)
		if isinstance(panel["models"], str):
			panel["models"] = [panel["models"]] if panel["models"] else []
		for folder in panel["models"]:
			if not isinstance(folder, str):
				raise SessionError("panel.models: {!r} is not a folder.".format(folder))
		panel["models"] = [os.path.join(here, os.path.expanduser(f)) for f in panel["models"]]
		for name, pair in panel["model_sets"].items():
			if not isinstance(pair, dict) or set(pair) != {"centroid", "instance"} or not all(isinstance(v, str) and v
					for v in pair.values()):
				raise SessionError("panel.model_sets.{}: give it centroid: and instance:, each a model's folder.".format(name))

	def Resolve(self, more=()):
		# campy's configurator's reading (configurator.Resolve): every setting, and where it came from
		from campy import configurator
		try:
			resolved = configurator.Resolve(self.path, self.args + list(more))
		except configurator.ConfigError as e:
			raise SessionError(str(e))
		except SystemExit:
			# argparse refused an option, and said which
			raise SessionError("campy-acquire does not take these options: {}".format(" ".join(self.args)))
		if self.picked and isinstance(resolved.sections.get("live"), dict):
			# The panel's pick, as if the config said it, each line saying where it came from (session-config.yaml,
			# campy-acquire.yaml and so session.json)
			for key, value in self.picked.items():
				resolved.sections["live"][key] = value
				resolved.section_sources.setdefault("live", {})[key] = configurator.Source("panel",
					note="picked on campy-session panel's page")
		if self.pico_page and isinstance(resolved.sections.get("pico"), dict):
			# The page's bout settings, in pico.set over the config's, the line saying which came from the page
			given = resolved.sections["pico"].get("set")
			resolved.sections["pico"]["set"] = dict(given if isinstance(given, dict) else {}, **self.pico_page)
			resolved.section_sources.setdefault("pico", {})["set"] = configurator.Source("panel",
				note="bout settings set on campy-session panel's page: {}".format(", ".join(sorted(self.pico_page))))
		return resolved

	def Value(self, key, default=None):
		# What campy-acquire will use: the command line's, the config's (or its profile's), or campy's default
		return self.acquire.get(key, default)

	def DisplayRate(self):
		# displayFrameRate: the view's frames per second without tracking; 0, no view unless view: says so
		try:
			return float(self.Value("displayFrameRate", 0) or 0)
		except (TypeError, ValueError):
			return 0.0

	def Feed(self):
		# The live feed is on whenever something reads it
		return self.live is not None or self.view["show"] or bool(self.Value("liveFeed"))

	def AcquireYaml(self):
		"""
		The config campy-acquire runs, as one file: every setting, no extends:,
		each line saying where its value came from (configurator's ToYaml),
		with the command line's options in it, and the live feed on when
		something reads it.
		"""
		resolved = self.resolved
		if self.Feed() and not self.Value("liveFeed"):
			resolved = self.Resolve(["--liveFeed", "True"])
		return resolved.ToYaml()

	def Cameras(self):
		n = int(self.Value("numCams", 1))
		names = self.Value("cameraNames")
		if not isinstance(names, list):
			names = ["Camera{}".format(c) for c in range(n)]
		return n, names[:n]


# ------------------------------------------------------------------ campy-live's environment

def LiveCalibration(where):
	"""
	live: calibration: as campy-live takes it: a calibration.toml as it is; a folder of calibration recordings, the
	newest one campy-calibrate made whole (campy/calibrate.py). SessionError when that folder has none.
	"""
	if not os.path.isdir(str(where)):
		return str(where)
	from campy import calibrate
	path, summary = calibrate.Newest(where)
	if path is None:
		raise SessionError("live: calibration: {} holds no calibration campy-calibrate made whole (a recording folder "
			"with calibration.toml and calibration_summary.json saying ok). Record one (videoFilename with "
			"\"calibration\" in it, post: calibrate: on), or name a calibration.toml.".format(where))
	return path


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
import importlib.metadata, importlib.util, json, sys
from campy import session
here, code = session.FeedCode()
try:
	version = importlib.metadata.version("sleap-nn")
except importlib.metadata.PackageNotFoundError:
	version = None
print(json.dumps({"python": sys.executable, "campy": here, "feed": code,
	"torch": importlib.util.find_spec("torch") is not None, "sleap_nn": importlib.util.find_spec("sleap_nn") is not None,
	"sleap_nn_version": version}))
"""

# The oldest sleap-nn campy-live runs a SLEAP model with (campy/live.py:
# SLEAP_NN, which campy-live itself checks by the fixes it needs)
LIVE_SLEAP_NN = "0.3.5"


def LiveEnvProblem(python, found, model):
	"""Why campy-live's environment (CheckLiveEnv's found) can't run model, or None."""
	if model != "sleap":
		return None
	if not (found["torch"] and found["sleap_nn"]):
		return "campy-live's environment ({}) has no torch or sleap-nn.".format(python)
	version = found.get("sleap_nn_version")
	if version is not None and Release(version) < Release(LIVE_SLEAP_NN):
		return "campy-live's environment ({}) has sleap-nn {}; campy-live needs {} or later. In that environment: " \
			"pip install -U 'sleap-nn>={}' (docs/live-tracking.md).".format(python, version, LIVE_SLEAP_NN, LIVE_SLEAP_NN)
	return None


def Release(version):
	# "0.3.5", "0.3.6.dev1+g123", "0.4.0a1" -> (0, 3, 5), (0, 3, 6), (0, 4, 0)
	parts = []
	for part in version.split(".")[:3]:
		digits = ""
		for ch in part:
			if not ch.isdigit():
				break
			digits += ch
		parts.append(int(digits or 0))
	return tuple(parts + [0] * (3 - len(parts)))


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
			registered = strictjson.load(f)["live"]["python"]
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
			envs = strictjson.load(f)
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

def WriteJson(path, value, private=False):
	# In one step: written beside it, then moved into place (retried a moment on Windows, where a reader has it open).
	# private: readable by its owner only (on Linux and macOS; on Windows the user's own folder already is)
	tmp = "{}.{}.tmp".format(path, os.getpid())
	if private:
		f = os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w")
	else:
		f = open(tmp, "w")
	with f:
		strictjson.dump(value, f, indent=1)
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


def SessionProcess(pid):
	# False when pid is not a campy-session (campy-session run or panel, the lock's only holders: python -m
	# campy.session, or the campy-session command): a lock's pid that another program has after a crash or a power
	# cut. Not just "campy" in its command line: every python of a campy environment (campy_v...) has that. True when
	# it is one, or when that can't be told (then the lock is respected, as before)
	try:
		import psutil
	except ImportError:
		return True
	try:
		line = " ".join(psutil.Process(pid).cmdline()).lower()
		return "campy.session" in line or "campy-session" in line
	except psutil.NoSuchProcess:
		return False
	except Exception: # AccessDenied (another user's process), ZombieProcess, ...
		return True


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
				if pid is not None and pid != os.getpid() and Alive(pid) and SessionProcess(pid):
					raise SessionError("Another campy-session is running on this computer (pid {}). Stop it first; "
						"{} is its lock.".format(pid, self.path))
				try:
					os.remove(self.path) # left by one that ended without removing it (its pid gone, or another program's)
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


def LastAlert(path, key):
	"""The latest event of key in an alerts log (JSON lines, campy/notify.py), its time in seconds, or None."""
	try:
		with open(path, encoding="utf-8", errors="replace") as f:
			lines = collections.deque(f, maxlen=500)
	except OSError:
		return None
	for line in reversed(lines):
		try:
			event = strictjson.loads(line)
		except ValueError:
			continue
		if event.get("key") == key:
			try:
				event["time"] = datetime.fromisoformat(event["time"]).timestamp()
			except (KeyError, TypeError, ValueError):
				event["time"] = None
			return event
	return None


def Alert(key, text, severity="critical"):
	# To the alerts' channels (campy/notify.py, ~/.campy/alerts.yaml), when there are any; it never stops campy-session
	try:
		from campy import notify
		notifier = notify.Notifier(say=Say)
		notifier.Problem(key, text, severity, remind=False)
		notifier.Finish(30, "campy-session's alert")
	except Exception as e:
		Say("Could not send the alert: {}".format(e))


def StatusDir():
	# Where campy-acquire's watch writes recording.json (campy/watch.py's StatusPath)
	return os.environ.get("CAMPY_STATUS_DIR") or os.path.join(CampyFolder(), "status")


def WatchState(folder, since=None):
	"""
	The state campy-acquire's watch last wrote for the recording in folder
	(recording.json: recording, stopping, ended or failed), or None when it
	wrote none for it (since: not before that time). Its own end is ended or
	failed: written as it said how the recording ended.
	"""
	path = os.path.join(StatusDir(), "recording.json")
	try:
		if since and os.path.getmtime(path) < since - 2:
			return None # an earlier recording's
		with open(path, encoding="utf-8") as f:
			status = strictjson.load(f)
	except (OSError, ValueError):
		return None
	where = (status.get("session") or {}).get("folder") if isinstance(status, dict) else None
	if not where or not folder or os.path.normcase(os.path.abspath(where)) != os.path.normcase(os.path.abspath(folder)):
		return None
	return status.get("state")


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

	def __init__(self, config, live_python=None, lock=True, detached=False, devices=None):
		self.config = config
		# The Pico, SpikeGLX, Windows' settings (campy/devices.py): the panel's, or None
		self.devices = devices
		self.live_python = live_python
		self.take_lock = lock # False: the panel holds campy-session's lock (campy/panel.py)
		# True (the panel): campy-acquire and campy-live apart from this console altogether, so that closing the
		# panel's window never ends a recording; its Stop, and Ctrl-C here, reach campy-acquire through its stop file
		self.detached = detached
		self.view_wanted = False # the panel's "Open the viewer again", for the main thread (Watch)
		self.hung, self.hung_at, self.hung_looked = None, 0.0, 0.0 # campy-live stopped because it hung (CheckHung)
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
		self.post_plan = None # the steps after the recording, in order, once they have started

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
		# The config as given (its profiles, extends:, stay where they are), and the same in one file, profiles and
		# command-line options combined, which campy-session post reads again
		with open(os.path.join(self.logs, "session-config.original.yaml"), "w", encoding="utf-8") as f:
			f.write("# {} as campy-session read it, {}; command-line options: {}\n".format(self.config.path,
				Stamp(time.time()), " ".join(self.config.args) or "none"))
			f.write(self.config.text)
		with open(os.path.join(self.logs, "session-config.yaml"), "w", encoding="utf-8") as f:
			f.write(self.config.resolved.ToYaml())
		self.acquireConfig = os.path.join(self.logs, "campy-acquire.yaml")
		with open(self.acquireConfig, "w", encoding="utf-8") as f:
			f.write("# What campy-acquire ran{}.\n".format(" (liveFeed on: campy-session reads it)" if self.config.Feed()
				and not self.config.Value("liveFeed") else ""))
			f.write(self.config.AcquireYaml())
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
			command += ["--calibration", LiveCalibration(live["calibration"])]
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
			if config.live["model"] == "sleap" and not (config.live["centroid"] and config.live["instance"]):
				raise SessionError("live: no centroid: and instance: in the config. panel.models and panel.model_sets are "
					"the panel's (campy-session panel picks the models); campy-session run needs the two folders.")
			self.live_python = self.live_python or LivePython(config.live)[0]
			found = CheckLiveEnv(self.live_python, self.LiveEnv())
			problem = LiveEnvProblem(self.live_python, found, config.live["model"])
			if problem:
				raise SessionError(problem)
		self.MakeFolder()
		Say("Recording into {}".format(self.folder))
		Say("Logs: {}".format(self.logs))
		for text in config.replaced:
			Say(text)
		self.Status()
		if self.devices is not None:
			self.state = "preparing"
			self.Status()
			try:
				self.devices.Before(self)
			except SessionError as e:
				self.state = "failed"
				self.Problem(str(e))
				self.Status()
				return 1
			if self.presses:
				self.state = "stopped"
				self.Problem("Stopped before the recording started: nothing was recorded.")
				self.devices.After(self, 1)
				self.Status()
				return 1

		if config.live is not None and not self.StartLive(1, wait=True):
			if config.live["required"]:
				self.state = "failed"
				self.Problem("No recording: campy-live did not start, and live.required is true.")
				self.Status()
				return 1
		if config.live is not None and config.live["watch"] and not self.presses:
			self.StartWatch()
		if self.presses:
			self.state = "stopped"
			self.Problem("Stopped (Ctrl-C) before the recording started: nothing was recorded.")
			self.EndLive()
			self.EndWatch()
			self.Status()
			return 1

		if config.view["show"]:
			self.StartView()

		env = dict(os.environ, PYTHONUNBUFFERED="1")
		env[LAUNCHER_ENV] = VERSION
		env.pop(newfiles.SESSION_ENV, None)
		acquire = Part("acquire", [Python(), "-c", ACQUIRE, self.acquireConfig, "--sessionPath", self.folder,
			"--stopFile", self.stopFile], os.path.join(self.logs, "campy-acquire.log"), env=env,
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
			self.AcquireEnded(acquire)
		self.Status()
		if self.devices is not None:
			self.devices.After(self, code)
			self.Status()
		self.EndLive()
		self.EndWatch()
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

	def AcquireEnded(self, acquire):
		"""
		campy-acquire ended with an error: its watch says how the recording
		ended (the end summary, and recording.json ended or failed), unless
		it never got to (killed: SIGKILL, out of memory, Task Manager; a
		crash; or an error before its watch started). Then nothing reached the
		alerts: it is sent from here, critical.
		"""
		state = WatchState(self.folder, acquire.started)
		if state in ("ended", "failed"):
			return False
		how = ExitCodeText(acquire.code)
		if state is None:
			text = "campy-acquire ended with {} before its recording watch started, so the recording may not have " \
				"started at all: {}. See {}.".format(how, self.folder, acquire.log)
		else:
			text = "campy-acquire ended with {} while it recorded {}, without saying how the recording ended: it was " \
				"killed, or crashed. See {}.".format(how, self.folder, acquire.log)
		Alert("session/campy-acquire", text)
		return True

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
			"--name", str(self.config.Value("liveFeedName", "campy-live"))] + self.RateArgs() + self.PauseArgs() +
			[str(a) for a in self.config.view["args"]],
			os.path.join(self.logs, "campy-live-view.log"), env=self.ViewEnv(), tied=True)
		self.parts["view"] = view.Start()
		return view

	def RateArgs(self):
		# Without tracking, the cameras alone at displayFrameRate (with it, the view keeps up with campy-live's results)
		rate = self.config.DisplayRate()
		return ["--fps", "{:g}".format(rate)] if self.config.live is None and rate > 0 else []

	def PauseArgs(self):
		# The viewer tells planned trigger pauses (all cameras at once) from a camera stopping, as the watch does
		out = []
		for key, option in (("alertTriggerPauseSec", "--pause-sec"), ("alertNoFramesSec", "--stop-sec")):
			value = self.config.Value(key, None)
			try:
				if value not in (None, "None", "") and float(value) > 0:
					out += [option, str(float(value))]
			except (TypeError, ValueError):
				pass
		return out

	def ReopenView(self):
		# The panel's "Open viewer": a view again, while the cameras record, if the last one has closed
		view = self.parts.get("view")
		if self.state != "recording" or (view is not None and view.Running()):
			return False
		Say("Opening campy-live-view again.")
		self.StartView()
		return True

	def ViewPython(self):
		if self.config.view["python"]:
			return self.config.view["python"]
		if self.live_python:
			return self.live_python
		try:
			return LivePython({})[0] # campy-live's registered environment draws the view best (OpenCV, Qt)
		except SessionError:
			return Python()

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
				elif name == "watch" and part.code not in (0, None):
					text = "campy-watch stopped ({}): see {}. The recording goes on, without its alerts about the " \
						"live tracking, and without telling when campy-live hangs.".format(
						ExitCodeText(part.code), part.log)
					self.Problem(text)
					Alert("session/campy-watch", text, "warning")
		self.CheckHung()
		if self.devices is not None:
			self.devices.Watch(self)
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

	def StartWatch(self):
		# campy-watch (campy/livewatch.py), beside campy-live: what campy-live-view shows in red, sent as alerts once
		# it lasts, into alerts-live.log in the recording's folder. Not tied to campy-session: it watches the recording
		# to its end, as campy-live tracks it
		self.watchStop = os.path.join(self.logs, "campy-watch.stop")
		watch = Part("watch", [self.live_python or Python(), "-m", "campy.livewatch",
			"--name", str(self.config.Value("liveFeedName", "campy-live")), "--cameras", str(self.config.Cameras()[0]),
			"--stop-file", self.watchStop, "--launcher-pid", str(os.getpid())],
			os.path.join(self.logs, "campy-watch.log"), env=self.LiveEnv(), cwd=self.folder,
			apart="console" if self.detached else True)
		self.parts["watch"] = watch.Start()
		return watch

	def CheckHung(self, now=None):
		"""
		campy-live that hangs (it runs, and makes no result while the cameras
		record): campy-watch says so in alerts-live.log, key live/campy-live,
		after LIVE_STALL_S (30 s). It is then stopped (its stop file; ended LIVE_STOP_S later
		if it does not stop) and started again as one that died (LiveDied).
		"""
		live = self.parts.get("live")
		if live is None or not live.Running() or not self.config.live or not self.config.live["restart"]:
			return
		now = time.time() if now is None else now
		if self.hung is live:
			if now - self.hung_at > LIVE_STOP_S:
				self.Problem("campy-live did not stop within {:g} s of its stop file: ended.".format(LIVE_STOP_S))
				live.End() # which sees it end (Check), so Watch will not: started again from here
				self.LiveDied(live)
			return
		if now - self.hung_looked < STATUS_EVERY_S:
			return
		self.hung_looked = now
		event = LastAlert(os.path.join(self.folder, "alerts-live.log"), "live/campy-live")
		if event is None or event.get("state") != "open" or (event.get("time") or 0) <= (live.started or now):
			return
		self.Problem("campy-watch: campy-live made no result for {:g} s while the cameras record: it is stopped, to "
			"be started again.".format(LIVE_STALL_S))
		self.hung, self.hung_at = live, now
		try:
			stopfile.Write(self.liveStop)
		except OSError as e:
			Say("Could not write campy-live's stop file: {}".format(e))

	def LiveDied(self, part):
		# campy-live ends by itself, exit code 0, once the feed has: as the cameras close, before campy-acquire ends
		hung, self.hung = self.hung is part, None
		if part.code == 0 and not hung:
			Say("campy-live finished: the feed ended.")
			return
		if not hung:
			self.Problem("campy-live stopped while the cameras record (exit code {}): see {}. The recording goes on."
				.format(part.code, part.log))
		if not self.config.live["restart"]:
			Say("It is not started again (live.restart is false).")
			return
		now = time.monotonic()
		while self.restarts and now - self.restarts[0] > 3600:
			self.restarts.popleft()
		if len(self.restarts) >= RESTARTS_PER_HOUR:
			text = "campy-live stopped {} times within the hour: not started again. The recording goes on " \
				"untracked.".format(len(self.restarts) + 1)
			self.Problem(text)
			Alert("live/campy-live/restarts", text)
			return
		after = RESTART_AFTER_S[min(len(self.restarts), len(RESTART_AFTER_S) - 1)]
		self.restart_due = now + after
		Say("Starting it again in {:g} s.".format(after))

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
				self.Problem("campy-live did not stop within {:g} s of its stop file: ended.".format(LIVE_STOP_S))
				live.End()
				break
			time.sleep(0.2)
		if live.code not in (0, None):
			self.Problem("campy-live ended with exit code {}: see {}.".format(live.code, live.log))

	def EndWatch(self):
		# campy-watch ends once the feed has (or, before any recording, at once); else its stop file, then it is ended
		watch = self.parts.get("watch")
		if watch is None or not watch.Running():
			if watch is not None:
				watch.Check()
			return
		deadline = time.monotonic() + (LIVE_END_S if self.parts.get("acquire") else 0)
		asked = False
		while not watch.Check():
			watch.tail.Read()
			now = time.monotonic()
			if not asked and now >= deadline:
				stopfile.Write(self.watchStop)
				asked, deadline = True, now + LIVE_STOP_S
			elif asked and now >= deadline:
				watch.End()
				break
			time.sleep(0.2)

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
		if self.devices is not None and level == 1:
			# The Pico first (its session stopped now), then campy: in the main thread, from Watch
			self.devices.Ask("stop", "now")
			Say("Ctrl-C: stopping the Pico, then campy-acquire.")
			return
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

	def PostStatus(self):
		# The steps after the recording: while they run, as post.Run writes them (logs/post.json: the running step, its
		# latest line and progress), so the panel follows them; after, their results
		if self.state == "post" and self.folder:
			from campy import post
			state = post.Load(self.folder)
			steps = state.get("steps") or {}
			self.post_plan = state.get("plan") or self.post_plan
			return {step: {k: v for k, v in entry.items() if k in ("state", "message", "fraction", "time", "seconds")}
				for step, entry in steps.items()}
		return {step: {"state": state, "message": message} for step, (state, message) in self.post_results.items()}

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
			"live_models": {"centroid": self.config.live["centroid"], "instance": self.config.live["instance"],
				"picked": bool(self.config.picked)} if self.config.live is not None else None,
			"problems": [{"time": t, "text": text} for t, text in self.problems],
			"post": self.PostStatus(),
			"post_plan": self.post_plan,
			"devices": self.devices.Status() if self.devices is not None else None,
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
		if config.pico is not None:
			raise SessionError("This config drives a Pico (pico:): record it from the panel, campy-session panel {}, "
				"which asks before starting and stopping the Pico.".format(args.config))
		session = Session(config)
		return session.Run()
	except SessionError as e:
		print("campy-session: {}".format(e), file=sys.stderr)
		return 1


def Main(argv=None):
	argv = sys.argv[1:] if argv is None else list(argv)
	from campy import post
	from campy import manifest, panel, shortcut
	commands = {"run": RunMain, "register-live": Register, "post": post.Main, "panel": panel.Main,
		"shortcut": shortcut.Main, "note": manifest.NoteMain}
	if not argv or argv[0] not in commands:
		print("usage: campy-session run <config>.yaml [--key value ...]\n"
			"       campy-session post <recording folder> [--only remux,export] [--force]\n"
			"       campy-session note <recording folder> \"text\"   (a dated note in its session.json)\n"
			"       campy-session panel <config>.yaml|<folder of configs> [--port 8765] [--no-browser]\n"
			"       campy-session shortcut <config>.yaml|<folder of configs> [--name ...]   (a desktop shortcut that "
			"opens the panel)\n"
			"       campy-session register-live   (once, in campy-live's environment)", file=sys.stderr)
		sys.exit(2)
	sys.exit(commands[argv[0]](argv[1:]))


if __name__ == "__main__":
	Main()
