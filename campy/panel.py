"""
campy-session panel: a recording run from a page in the browser.

  campy-session panel <config>.yaml [--key value ...] [--port 8765] [--no-browser]
  campy-session panel <folder of configs> [--key value ...] [--port 8765] [--no-browser]

It opens a page (http://127.0.0.1:<port>/?t=<key>) where the recording is
started (the Start button, the only way: never on its own), watched and
stopped, as campy-session run would run it (campy/session.py): the same
programs, the same folder and logs, the same steps after it. One recording
after another, each into a folder of its own, until the panel is closed
(Ctrl-C in its console when nothing records).

One panel for every experiment on a rig: opened with a folder of configs
(the rig's one desktop icon, campy-session shortcut <folder>), the page
starts with an Experiment list: the configs starred on it (the favourites;
before any star, those with a panel.title), each by its panel.title or
its file's name. "Show all configs" lists every config in the folder
(*.yaml, *.yml), with why one can't be used, a star to add or remove it,
and Use. Picking one, while nothing records (nor an earlier panel's
recording, nor a Pico or SpikeGLX left running), loads it as if the panel
had been opened with it: the cameras, the models, the Pico's lengths and
bout settings, the checks (each config's bout settings and model pick are
remembered apart, as before). The favourites and the last pick are kept for
each folder in ~/.campy/status/panel-types.json, so the next panel opens on
the experiment the last one was left on. The rig's lock (panel.rig_lock) is
held when any config in the folder asks for it.

What the page shows, once a second:

- before the start: the experiment's fields (experiment, animal IDs, notes,
  for session.json); with trained models (live: model: sleap), the model
  picker: the centroid and instance models campy-live tracks with, from the
  config's pair, panel.model_sets and the folders in panel.models, each with its
  settings and score (campy/livemodels.py), for this panel's recordings only
  (the config is never changed); and the checks: what stops a start (campy-live's
  environment not found or not the same campy, another campy-session
  running) apart from what only warns (disk space). The recording's length
  is the config's recTimeInSec. With a Pico, the length is the page's choice,
  and the disk check is what that length's plan needs (SpikeGLX while the
  gate is open, and the videos: an estimate) against the free space: a
  length that doesn't fit stops the start, unless a confirm on the page
  passes over it.
- while it records: elapsed and planned time; each camera's frames/s,
  frames and health, from the cameras' rings with campy-live-view's own
  checks (campy/livehealth.py: NO FRAMES, behind, LOST, GAP, LATE, frames
  ahead or behind), and their dropped frames and write queue from
  campy-acquire's watch (~/.campy/status/recording.json, when there); a
  grey thumbnail of each camera, once a second; disk space and the time left
  at the rate it fills (with a Pico, what the rest of its plan needs); campy-live's 3D (animals with good 3D in the last
  second) from its results feed; the warnings (the alerts' logs in the
  recording's folder, campy-session's problems); bookmarks, written into the
  recording's bookmarks.csv as campy-live-view writes them; each program's
  state, and a button to open the view again.
- after it: each post-step's progress (campy/post.py).
- copies: with a folder in the page's "Copy each recording to" box, each
  recording is copied into it once it and its post-steps have ended, in the
  background, one at a time, while the next records (campy/copyqueue.py; the
  config's own post copy step is off in the panel, the box takes its place and
  starts from its copy.to). Closing the panel waits for copies still running,
  unless the page leaves them.

The page is served from this computer only (127.0.0.1): over Remote
Desktop, open it in the rig's browser; from another computer, through an SSH
tunnel (ssh -L 8765:127.0.0.1:8765 <rig>). Every request needs the key in
the page's address, so no other program on the computer can press Start
or Stop, and must be addressed to this computer (Host 127.0.0.1, localhost
or [::1]; a POST's Origin the same), so a web page that has learnt the key
still can't. Closing the page stops nothing; it can be opened again at any
time, from the address in the console, or by starting the panel again (the
desktop icon): a panel already open writes its address into
~/.campy/status/panel.json (readable by its user only), and a second one
that finds it answering opens that page in the browser and ends.

Only the standard library and numpy (campy's recording environment): the
page is one file of HTML and JavaScript (campy/panelpage.py), no build step.
"""

import argparse, collections, http.server, json, os, platform, re, secrets, shutil, struct, sys, threading, time, \
	webbrowser, zlib
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import numpy as np

from campy import bookmarks, children, clock, copyqueue, devices, endsummary, livefeed, livehealth, livemodels, \
	liveresults, pico, session, stopfile, strictjson

PORT = 8765
THUMB_WIDTH = 192 # px, each camera's thumbnail
SAMPLE_S = 0.25 # how often the rings' slots are read (campy-live-view: about 20 times a second)
THUMB_S = 1.0 # and a frame of each camera, for its thumbnail
STATUS_FRESH_S = 20 # recording.json older than this is not the recording's now
WARN_FRACTION = 0.8 # the disk check warns when a length needs more than this share of the free space (as the launch
# check's, campy/preflight.py)


LOCAL_NAMES = ("127.0.0.1", "localhost", "[::1]")


def LocalName(host):
	# A Host header (name[:port]) that names this computer, or none (not from a browser): not a name a web page made
	# point to 127.0.0.1 (DNS rebinding)
	if not host:
		return True
	name = host.strip().lower()
	name = name[:name.find("]") + 1] if name.startswith("[") else name.split(":")[0]
	return name in LOCAL_NAMES


def LocalOrigin(origin):
	# A request's Origin: none (not from a web page), or this computer's own page
	if origin is None:
		return True
	url = urlparse(origin)
	return url.scheme == "http" and LocalName(url.netloc)


def PanelFile():
	# The panel that is open: {pid, url, config, started}, so a second launch opens its page
	return os.path.join(session.CampyFolder(), "status", "panel.json")


def Running():
	"""
	The panel already open on this computer (panel.json), when its page
	answers with its key; else None.
	"""
	try:
		with open(PanelFile()) as f:
			info = strictjson.load(f)
		pid, url = int(info["pid"]), str(info["url"])
	except (OSError, ValueError, KeyError, TypeError):
		return None
	if pid == os.getpid() or not children.Alive(pid):
		return None
	import urllib.request
	try:
		page = urlparse(url)
		check = "{}://{}/status.json?{}".format(page.scheme, page.netloc, page.query)
		opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) # 127.0.0.1: never through a proxy
		with opener.open(check, timeout=5) as r:
			if r.status != 200:
				return None
	except Exception:
		return None
	return info


def Say(text):
	sys.stdout.write("[panel] {}\n".format(text))
	sys.stdout.flush()


# ------------------------------------------------------------------ thumbnails

def Png(gray):
	"""A grey 8-bit PNG of a 2D uint8 array (the standard library's zlib: no image package)."""
	gray = np.ascontiguousarray(gray, np.uint8)
	h, w = gray.shape
	raw = np.zeros((h, w + 1), np.uint8)
	raw[:, 1:] = gray # each row: filter 0, then its pixels
	def Chunk(kind, data):
		return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
	return b"\x89PNG\r\n\x1a\n" + Chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)) + \
		Chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)) + Chunk(b"IEND", b"")


def Thumb(image, width=THUMB_WIDTH):
	# A camera frame (height x width x channels), grey and about width px wide, as PNG
	img = np.asarray(image)
	if img.ndim == 3:
		img = img[:, :, 0] if img.shape[2] == 1 else img.mean(2).astype(np.uint8)
	step = max(1, -(-img.shape[1] // width))
	return Png(img[::step, ::step])


# ------------------------------------------------------------------ the rings

def Good3D(tracking):
	# (animals with good 3D over the last second, animals wanted), as campy-live-view's warning counts them
	got = tracking.Last(1.0) if tracking is not None and tracking.taken else None
	if got is None:
		return None
	_, err, views = got
	with np.errstate(invalid="ignore"):
		good = ((views >= 2) & (err <= tracking.bad)).mean(0) >= 0.5
	return int(good.sum()), int(tracking.animals or err.shape[1])


def Seconds(value):
	# A config's number of seconds, or None ("None", unset, not a number)
	try:
		return float(value) if value not in (None, "None", "") else None
	except (TypeError, ValueError):
		return None


class Health:
	"""
	The cameras' rings and campy-live's results, read from a thread of its
	own as campy-live-view reads them (only the slots' fields, and a frame a
	second for the thumbnails): livehealth's Rig and Tracking. It attaches
	when a recording's rings appear, and again for the next recording.
	"""

	def __init__(self, name, cameras, animals=0, pause_s=None, stop_s=None):
		self.names = [livefeed.RingName(name, c) for c in range(cameras)]
		self.pause_s, self.stop_s = pause_s, stop_s # alertTriggerPauseSec, alertNoFramesSec (livehealth.Rig)
		self.resultsName = liveresults.RingName(name)
		self.animals = animals
		self.readers = self.rig = self.results = self.tracking = None
		self.lastRow = 0
		self.thumbs = {}
		self.thumbAt = 0.0
		self.snapshot = {"feed": False}
		self.lock = threading.Lock() # the readers: the thread samples them, Mark reads them
		self.done = threading.Event()
		self.thread = None

	def Start(self):
		self.thread = threading.Thread(target=self.Run, name="campy panel health", daemon=True)
		self.thread.start()
		return self

	def Stop(self):
		self.done.set()
		if self.thread is not None:
			self.thread.join(2)
		with self.lock:
			self.Detach()

	def Run(self):
		while not self.done.is_set():
			try:
				with self.lock:
					self.Step()
			except Exception as e:
				self.snapshot = {"feed": False, "error": "{}: {}".format(type(e).__name__, e)}
			self.done.wait(SAMPLE_S)

	def Attach(self):
		readers = []
		try:
			for name in self.names:
				readers.append(livefeed.RingReader(name))
		except (OSError, ValueError):
			for r in readers:
				r.Close()
			return False
		if any(r.Ended() for r in readers):
			for r in readers:
				r.Close()
			return False
		self.readers = readers
		self.frameRate = float(readers[0].Info("frameRate") or 0) or 30.0
		self.rig = livehealth.Rig([r.cameraName or n for r, n in zip(readers, self.names)], self.frameRate,
			readers[0].slots, self.pause_s, self.stop_s)
		self.thumbs, self.lastRow = {}, 0
		return True

	def Detach(self):
		for r in self.readers or []:
			r.Close()
		if self.results is not None:
			self.results.Close()
		self.readers = self.rig = self.results = self.tracking = None

	def Step(self):
		if self.readers is None and not self.Attach():
			self.snapshot = {"feed": False}
			return
		if any(r.Replaced() for r in self.readers):
			self.Detach() # the next recording's rings
			return
		ended = all(r.Ended() for r in self.readers)
		self.rig.Sample(self.readers)
		status = self.rig.Status()
		cameras = []
		for r, c in zip(self.readers, status.cameras):
			cameras.append({"name": c.name, "fps": round(c.fps, 1) if c.fps is not None else None, "frame": r.Latest(),
				"problems": [tag for tag, _ in c.problems], "sentences": [text for _, text in c.problems],
				"paused_s": round(c.paused_s, 1) if c.paused_s is not None else None})
		self.Results()
		now = time.monotonic()
		if now - self.thumbAt >= THUMB_S:
			self.thumbAt = now
			for c, r in enumerate(self.readers):
				frame = r.Read(r.Latest()) if r.Latest() > 0 else None
				if frame is not None:
					self.thumbs[c] = Thumb(frame.image)
		good = Good3D(self.tracking)
		self.snapshot = {"feed": True, "ended": ended, "recording_s": status.recording_s, "cameras": cameras,
			"tracking": None if self.results is None else {"rows": self.lastRow,
				"good3d": good[0] if good else None, "animals": good[1] if good else self.animals or None,
				"alerts": self.tracking.Alerts() if self.tracking is not None else []}}
		if ended:
			self.Detach()

	def Results(self):
		if self.results is None:
			try:
				self.results = liveresults.Results(self.resultsName)
			except (OSError, ValueError):
				return
			animals = self.results.description.get("animals") or self.animals
			self.tracking = livehealth.Tracking(animals, self.frameRate)
			self.lastRow = 0
		if self.results.Ended():
			return
		latest = self.results.Latest()
		for n in range(max(self.lastRow + 1, latest - self.results.slots + 1), latest + 1):
			row = self.results.Read(n)
			if row is not None:
				self.tracking.Add(row)
		self.lastRow = latest

	def Cameras(self):
		# Each camera's newest frame and when it reached the computer, for a bookmark
		with self.lock:
			if self.readers is None:
				return [(None, None)] * len(self.names)
			out = []
			for r in self.readers:
				n = r.Latest()
				out.append((n, r.HostTime(n)) if n > 0 else (None, None))
			return out


# ------------------------------------------------------------------ what other programs wrote

def StatusDir():
	return session.StatusDir()


def RecordingStatus(folder):
	# campy-acquire's watch's ~/.campy/status/recording.json, when it is this recording's and fresh
	try:
		path = os.path.join(StatusDir(), "recording.json")
		if time.time() - os.path.getmtime(path) > STATUS_FRESH_S:
			return None
		with open(path) as f:
			status = strictjson.load(f)
	except (OSError, ValueError):
		return None
	where = (status.get("session") or {}).get("folder")
	if folder and where and os.path.normcase(os.path.abspath(where)) != os.path.normcase(os.path.abspath(folder)):
		return None
	return status


def Alerts(folder, keep=200):
	"""The alerts' logs in the recording's folder (JSON lines): (open ones, the last resolved ones)."""
	latest = collections.OrderedDict()
	for name in ("alerts.log", "alerts-live.log"):
		try:
			with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as f:
				lines = collections.deque(f, maxlen=keep)
		except OSError:
			continue
		for line in lines:
			try:
				event = strictjson.loads(line)
			except ValueError:
				continue
			key = event.get("key")
			if key:
				latest.pop(key, None)
				latest[key] = event
	events = list(latest.values())
	open_ = [e for e in events if e.get("state") == "open"]
	resolved = [e for e in events if e.get("state") == "resolved"][-3:]
	return open_, resolved


CALIBRATION_MAX_AGE_H = 24 # a 3D calibration older than this is a warning: one is recorded each day


def CalibrationCheck(where, now=None):
	"""
	The check line for live: calibration: which calibration.toml the next recording uses (a folder of calibration
	recordings: the newest whole one), how old, and its cameras' reprojection error. Older than a day: a warning
	(record a calibration today); none: it stops the start.
	"""
	from campy import calibrate
	path, summary = calibrate.Newest(where)
	if path is None:
		return ("block", "3D calibration: none in {} that campy-calibrate made whole: record a calibration (the "
			"Calibration experiment) first".format(where) if os.path.isdir(str(where)) else
			"3D calibration: {} isn't there".format(where))
	age = calibrate.AgeHours(summary, now) if summary else None
	if age is None:
		try:
			age = ((now.timestamp() if now else time.time()) - os.path.getmtime(path)) / 3600
		except OSError:
			age = None
	text = "3D calibration: {} ({})".format(path, calibrate.Describe(summary) if summary else "no summary beside it")
	if age is not None and age > CALIBRATION_MAX_AGE_H:
		return ("warn", "{}: {:.0f} days old. Record a calibration today (the Calibration experiment), so the 3D "
			"follows any camera that moved".format(text, age / 24) if age >= 48 else
			"{}: {:.0f} hours old. Record a calibration today (the Calibration experiment)".format(text, age))
	return ("ok", text)


def SinceRelease(describe, version):
	"""
	git describe's distance from the newest release tag (v2.7.0-96-g7bf012e): ("+96", "96 commits after the v2.7.0
	release"), or ("(v2.6.0 +12)", ...) when that tag isn't the version setup.py gives; None on the tag itself, or
	without git describe.
	"""
	m = re.match(r"^(.+)-(\d+)-g[0-9a-f]+$", str(describe or ""))
	if not m or int(m.group(2)) == 0:
		return None
	tag, n = m.group(1), int(m.group(2))
	text = "{} commit{} after the {} release".format(n, "" if n == 1 else "s", tag)
	if tag.lstrip("v") == str(version):
		return "+{}".format(n), text
	return "({} +{})".format(tag, n), text


def VersionLabel():
	"""
	Which campy the panel runs, for the page's top right corner (as LUC3D shows its own): {text, title}, e.g.
	text "v2.7.0 +96 · dev · 7bf012ec" (version; how many commits the checkout is past that release's tag, from git
	describe, so a checkout newer than the release doesn't read as the release; branch; commit; "modified" when the
	checkout's files differ from the commit), title the whole of it. Read once as the panel starts: a checkout updated under it shows only once the
	panel is opened again, and so does the label.
	"""
	try:
		from campy import manifest
		info = manifest.CampyInfo()
	except Exception as e: # never the panel's start for it
		return {"text": "version unknown", "title": "Could not tell which campy this is: {}".format(e)}
	version, commit, branch = info.get("version"), info.get("commit"), info.get("branch")
	parts = ["v{}".format(version) if version else "version unknown"]
	after = SinceRelease(info.get("describe"), version)
	if after:
		parts[0] += " " + after[0]
	if branch and branch != "HEAD":
		parts.append(branch)
	if commit:
		parts.append(commit[:8])
	if info.get("dirty"):
		parts.append("modified")
	title = "campy {}".format(version or "(version unknown)")
	if commit:
		title += ", commit {}{}".format(commit, " on branch " + branch if branch and branch != "HEAD" else "")
		if after:
			title += " ({})".format(after[1])
		elif info.get("describe"):
			title += " ({})".format(info["describe"])
	if info.get("dirty"):
		title += ", with files changed since that commit"
	title += ", from " + str(info.get("path"))
	return {"text": " \u00b7 ".join(parts), "title": title}


def Ended(status):
	"""A recording's launcher status once it has ended, with whether it stopped cleanly (post.CleanStop): "clean"."""
	try:
		from campy import post
		clean, why = post.CleanStop(status["folder"]) # every camera closed its video, with its totals
	except Exception as e:
		clean, why = False, "could not be told ({})".format(e)
	status["clean"] = {"clean": bool(clean), "why": why}
	return status


def Previous():
	"""
	The last recording this computer's campy-session made, from
	~/.campy/status/launcher.json, when it has ended and its folder is still
	there: a panel opened again shows it as the last recording, with its
	steps and what went wrong. None otherwise (one still running is Orphan's).
	"""
	try:
		with open(os.path.join(session.CampyFolder(), "status", "launcher.json")) as f:
			status = strictjson.load(f)
	except (OSError, ValueError):
		return None
	acquire = (status.get("parts") or {}).get("acquire") or {}
	folder = status.get("folder")
	if status.get("state") not in ("done", "failed") or acquire.get("state") != "ended" or not folder or \
			not os.path.isdir(folder):
		return None
	return Ended(status)


def Orphan():
	"""
	A recording an earlier campy-session started that is still running,
	though that campy-session has ended (its window was closed: the panel
	runs campy-acquire apart from it, so the recording went on). From its
	~/.campy/status/launcher.json: {folder, pid, stop (its stop file),
	started}, or None.
	"""
	try:
		with open(os.path.join(session.CampyFolder(), "status", "launcher.json")) as f:
			status = strictjson.load(f)
	except (OSError, ValueError):
		return None
	acquire = (status.get("parts") or {}).get("acquire") or {}
	pid, folder = acquire.get("pid"), status.get("folder")
	if not pid or not folder or acquire.get("state") == "ended" or status.get("pid") == os.getpid():
		return None
	if children.Alive(status.get("pid")) or not children.Alive(pid):
		return None # its campy-session is still there, or the recording has ended
	try:
		import psutil
		if "campy" not in " ".join(psutil.Process(pid).cmdline()):
			return None # that pid is another program's now
	except Exception:
		pass
	return {"folder": folder, "pid": pid, "stop": os.path.join(folder, "logs", "campy-acquire.stop"),
		"started": acquire.get("started")}


# ------------------------------------------------------------------ one panel for a folder of configs

CONFIG_TYPES = (".yaml", ".yml")


def TypesFile():
	# Each folder's favourites and last pick: {folder: {favorites: [config, ...], last: config}}
	return os.path.join(session.CampyFolder(), "status", "panel-types.json")


def Same(a, b):
	return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def Describe(path, args=()):
	"""
	A config of the folder, for the Experiment list: {path, file, title
	(panel.title set in that file itself, else the file's name), titled,
	error (why campy-session can't use it, or None), rig_lock, owner}.
	"""
	path = os.path.abspath(path)
	entry = {"path": path, "file": os.path.basename(path), "title": os.path.splitext(os.path.basename(path))[0],
		"titled": False, "error": None, "rig_lock": "", "owner": ""}
	try:
		config = session.Config(path, args)
	except Exception as e: # anything (a file that isn't YAML, or isn't campy's) is a line on the list, not a failure
		text = str(e) if isinstance(e, session.SessionError) else "{}: {}".format(type(e).__name__, e)
		entry["error"] = text if len(text) <= 400 else text[:400] + "..."
		return entry
	# Its own title only: a config built on another with extends: (a trial of the daily one, say) doesn't take the
	# other's, which would put two lines of one name on the list
	title = " ".join(str(config.panel["title"] or "").split())
	source = config.resolved.section_sources.get("panel", {}).get("title")
	if title and getattr(source, "path", None) and Same(source.path, path):
		entry["title"], entry["titled"] = title, True
	entry["rig_lock"], entry["owner"] = config.panel["rig_lock"], config.panel["owner"]
	return entry


class Types:
	"""
	The experiments of a panel opened with a folder of configs: every config
	in it (Describe), the starred ones (the favourites, the page's
	Experiment list) and the last one picked, both kept for the folder in
	~/.campy/status/panel-types.json. Before any star, the favourites are the
	configs with a panel.title.
	"""

	def __init__(self, folder, args=()):
		self.folder, self.args = os.path.abspath(folder), list(args)
		self.configs = []
		self.Scan()

	def Scan(self):
		try:
			names = sorted(os.listdir(self.folder), key=str.lower)
		except OSError as e:
			raise session.SessionError("Could not read the folder of configs {}: {}".format(self.folder, e))
		self.configs = [Describe(os.path.join(self.folder, name), self.args) for name in names
			if not name.startswith(".") and os.path.splitext(name)[1].lower() in CONFIG_TYPES and
			os.path.isfile(os.path.join(self.folder, name))]
		return self.configs

	def Find(self, path):
		if not isinstance(path, str) or not path:
			return None
		return next((c for c in self.configs if Same(c["path"], path)), None)

	def Saved(self):
		# This folder's {favorites, last}, as the last panel left them
		try:
			with open(TypesFile()) as f:
				entry = strictjson.load(f).get(os.path.normcase(self.folder))
		except (OSError, ValueError, AttributeError):
			return {}
		return entry if isinstance(entry, dict) else {}

	def Save(self, **change):
		path = TypesFile()
		try:
			with open(path) as f:
				data = strictjson.load(f)
			if not isinstance(data, dict):
				data = {}
		except (OSError, ValueError):
			data = {}
		key = os.path.normcase(self.folder)
		data[key] = dict(data[key] if isinstance(data.get(key), dict) else {}, **change)
		try:
			os.makedirs(os.path.dirname(path), exist_ok=True)
			session.WriteJson(path, data)
		except OSError as e:
			Say("Could not write {} ({}): the next panel won't remember it.".format(path, e))

	def Favorites(self):
		"""The starred configs that are in the folder, in the order they were starred."""
		saved = self.Saved().get("favorites")
		if not isinstance(saved, list):
			return [c for c in self.configs if c["titled"]]
		out = []
		for path in saved:
			c = self.Find(path)
			if c is not None and c not in out:
				out.append(c)
		return out

	def Star(self, path, on):
		c = self.Find(path)
		if c is None:
			return "No config {} in {}.".format(path, self.folder)
		favorites = [f["path"] for f in self.Favorites() if not Same(f["path"], c["path"])]
		if on:
			favorites.append(c["path"])
		self.Save(favorites=favorites)
		return None

	def First(self):
		"""The config a panel opens on: the last one picked, else the first favourite, else the folder's first."""
		for c in [self.Find(self.Saved().get("last"))] + self.Favorites() + self.configs:
			if c is not None and not c["error"]:
				return c["path"]
		if not self.configs:
			raise session.SessionError("No config in {} (*.yaml, *.yml).".format(self.folder))
		raise session.SessionError("No config in {} can be used: {}".format(self.folder, "; ".join("{}: {}".format(
			c["file"], c["error"]) for c in self.configs[:5])))

	def RigLock(self):
		# (panel.rig_lock, panel.owner) of the first config (favourites first) that asks for the rig's lock, or ("", "")
		for c in self.Favorites() + self.configs:
			if c["rig_lock"]:
				return c["rig_lock"], c["owner"]
		return "", ""

	def Status(self, chosen):
		favorites = self.Favorites()
		starred = [f["path"] for f in favorites]
		def Entry(c):
			return {"path": c["path"], "file": c["file"], "title": c["title"], "error": c["error"],
				"favorite": c["path"] in starred}
		return {"folder": self.folder, "chosen": chosen, "favorites": [Entry(c) for c in favorites],
			"all": [Entry(c) for c in self.configs]}


# ------------------------------------------------------------------ the panel

class Panel:
	def __init__(self, config_path, args=(), port=PORT, browser=True):
		# The copies of finished recordings (the page's "Copy each recording to"): the panel's for as long as it is
		# open, whichever config it records with
		self.copies = copyqueue.CopyQueue(say=Say)
		self.close_when_copied = False # the page's Close while copies were running: close once they are done
		self.opened, self.args = os.path.abspath(config_path), list(args)
		self.port, self.browser = port, browser
		self.token = secrets.token_urlsafe(18)
		self.session = None
		self.last = None # the last recording's launcher status, after it
		self.summaries = endsummary.Cache() # what went wrong in it, for the page's After the recording
		self.version = VersionLabel() # which campy this panel runs, for the page's top right corner
		self.error = None # why the last start failed, before it recorded
		self.wanted = None # the Start button's fields, for the main thread
		self.lock = threading.Lock()
		self.marks = None
		self.disk = collections.deque(maxlen=60) # (time, free bytes), for the rate the disk fills
		self.closing = False
		self.health = None
		# Opened with a folder: its configs, one of them in use at a time (the page's Experiment list)
		self.types = Types(config_path, self.args) if os.path.isdir(config_path) else None
		# A config campy-session can't use stops here
		self.Use(self.types.First() if self.types is not None else config_path)
		# A panel opened again after a recording has ended: that recording's steps and summary, as the last panel left them
		self.last = Previous()

	def Use(self, config_path):
		"""
		The config the panel records with, and all the page shows from it:
		the cameras' health, the Pico, SpikeGLX and Windows' settings, the
		lengths, the model picker, the checks. A config campy-session can't
		use raises SessionError, and the panel stays as it was.
		"""
		config = session.Config(config_path, self.args)
		n = config.Cameras()[0]
		live = config.live or {}
		health = Health(str(config.Value("liveFeedName", "campy-live")), n, live.get("animals", 0),
			Seconds(config.Value("alertTriggerPauseSec", None)), Seconds(config.Value("alertNoFramesSec", None)))
		# The Pico, SpikeGLX, Windows' settings, when the config has them (campy/devices.py)
		devs = devices.Devices(config, say=Say)
		if not devs.Enabled():
			devs = None
		if devs is not None and devs.pico is not None:
			# The bout settings the last panel with this config left on its page
			try:
				devs.pico.Restore(pico.Remembered(pico.RememberFile(), config.path))
			except pico.PicoError:
				pass
		lengths = devs.Lengths() if devs is not None else []
		# The model picker (campy/livemodels.py): with trained models only
		picker = livemodels.Picker(config) if config.live is not None and config.live["model"] == "sleap" else None
		old = self.health
		self.config_path, self.config, self.devices, self.lengths, self.picker = config.path, config, devs, lengths, picker
		self.length = lengths[0]["seconds"] if lengths else None # the page's choice
		self.health = health
		self.disk.clear()
		self.checks = self.Checks()
		if old is not None and old.thread is not None: # the panel is open: this config's cameras from now on
			old.Stop()
			health.Start()

	def Busy(self):
		# Why the experiment can't be changed now, or None
		if self.session is not None or self.wanted is not None:
			return "A recording is running: pick another experiment once it has ended."
		orphan = Orphan()
		if orphan:
			return "A recording an earlier panel started is still running ({}): stop it first.".format(orphan["folder"])
		if self.devices is not None:
			status = self.devices.Status()
			if (status.get("pico") or {}).get("alive") or (status.get("spikeglx") or {}).get("running"):
				return "The Pico or SpikeGLX is still running (Still running, on the page): stop it first."
		return None

	def Choose(self, path):
		# The page's Experiment list: record with another config of the folder; an error text, or None
		if self.types is None:
			return "This panel was opened with one config: open it with the folder of configs to pick another."
		c = self.types.Find(path)
		if c is None:
			return "No config {} in {}.".format(path, self.types.folder)
		with self.lock:
			why = self.Busy()
			if why:
				return why
			if not Same(c["path"], self.config.path):
				try:
					self.Use(c["path"])
				except (session.SessionError, pico.PicoError) as e:
					c["error"] = str(e)
					return "{} can't be used: {}".format(c["file"], e)
				c["error"] = None
				self.error = None
				Say("Experiment: {} ({}).".format(c["title"], c["path"]))
			self.types.Save(last=c["path"])
		return None

	def Favorite(self, path, on):
		if self.types is None:
			return "This panel was opened with one config."
		return self.types.Star(path, bool(on))

	def Rescan(self):
		if self.types is None:
			return "This panel was opened with one config."
		self.types.Scan()
		return None

	# ---------------------------------------------------------- before the start

	def Checks(self):
		"""[(level, text) or (level, text, action)]: "block" stops a start, "warn" only warns."""
		checks = []
		live = self.config.live
		if live is not None:
			try:
				python, where = session.LivePython(live)
				found = session.CheckLiveEnv(python, dict(os.environ, **{str(k): str(v) for k, v in live["env"].items()}))
				problem = session.LiveEnvProblem(python, found, live["model"])
				if problem:
					checks.append(("block", problem))
				else:
					checks.append(("ok", "campy-live's environment: {} ({})".format(python, where)))
			except session.SessionError as e:
				checks.append(("block", str(e)))
		if getattr(self, "picker", None) is not None:
			checks += self.picker.Checks()
		if live is not None and live.get("calibration"):
			checks.append(CalibrationCheck(live["calibration"]))
		n, names = self.config.Cameras()
		if self.devices is not None and self.devices.pico is not None:
			checks.append(("ok", "{} cameras ({})".format(n, ", ".join(names))))
		else:
			planned = self.config.Value("recTimeInSec", 0)
			checks.append(("ok", "{} cameras ({}), {} each (recTimeInSec)".format(n, ", ".join(names), Duration(planned))))
		if self.devices is not None:
			checks += self.devices.Checks(self.length)
		need = self.DiskCheck()
		if need is not None:
			checks.append(need)
			return checks
		free = self.Free()
		if free is not None:
			least = self.config.Value("alertDiskFreeGB", 20)
			try:
				least = float(least)
			except (TypeError, ValueError):
				least = 20.0
			checks.append(("warn" if free / 1e9 < least else "ok", "{:.0f} GB free where it records".format(free / 1e9)))
		return checks

	def DiskCheck(self):
		"""
		With a Pico: the disk the chosen length needs (its plan's SpikeGLX
		and videos, devices.Need) against the free space where it records,
		keeping stopAtFreeGB: (level, text, "disk"), "block" when it doesn't
		fit (a start then needs the page's confirm, disk_override), "warn"
		above 80% of the free space. None without a Pico, or when the plan or
		the free space can't be told (then the free space alone, as before).
		"""
		if self.devices is None or self.devices.pico is None:
			return None
		try:
			_, folder, _ = self.devices.Length(self.length)
			need = self.devices.Need(self.length, self.config.Cameras()[0])
		except (session.SessionError, pico.PicoError):
			return None # the plan's own check says why
		free = self.Free(folder)
		if need is None or free is None:
			return None
		keep = Seconds(self.config.Value("stopAtFreeGB", 2)) or 0.0
		wanted = need["total_b"] + keep * 1e9
		parts = []
		if self.devices.sglx is not None:
			parts.append("SpikeGLX {}: {:,.0f} s of open gate with its trigger margin, at {:g} MB/s".format(
				GB(need["spikeglx_b"]), need["gate_s"], float(self.devices.sglx.section["write_mb_s"])))
		parts.append("videos about {}, at {:g} MB/s a camera".format(GB(need["videos_b"]),
			float(self.config.panel["video_mb_s"])))
		how = "an estimate ({}; plus {:g} GB kept free, stopAtFreeGB)".format("; ".join(parts), keep)
		text = "Disk: this length needs about {} of the {} free".format(GB(need["total_b"]), GB(free))
		if wanted > free:
			return ("block", "{}: {} short, {}. Pick a shorter length, or add a data disk.".format(text, GB(wanted - free),
				how), "disk")
		return ("warn" if wanted > WARN_FRACTION * free else "ok", "{}, {}".format(text, how), "disk")

	def Free(self, folder=None):
		where = os.path.abspath(str(folder or self.config.Value("videoFolder", ".")))

		while where and not os.path.isdir(where):
			parent = os.path.dirname(where)
			if parent == where:
				break
			where = parent
		try:
			return shutil.disk_usage(where).free
		except OSError:
			return None

	def Start(self, fields):
		# From the page (any thread): ask the main thread to start; an error text, or None
		with self.lock:
			if self.session is not None or self.wanted is not None:
				return "A recording is already running."
			orphan = Orphan()
			if orphan:
				return "A recording an earlier panel started is still running ({}): stop it first.".format(orphan["folder"])
			if self.devices is not None and self.devices.pico is not None:
				try:
					self.length = self.devices.Length(fields.get("length") or self.length)[0]
					if fields.get("pico_at"):
						devices.When(fields["pico_at"])
				except session.SessionError as e:
					return str(e)
			self.checks = self.Checks()
			# The disk check alone can be passed over, from the page's confirm that names the shortfall (data moved off
			# during the session, say); every other block can't
			blocks = [c[1] for c in self.checks if c[0] == "block" and not (fields.get("disk_override") and
				len(c) > 2 and c[2] == "disk")]
			if blocks:
				return "Can't start: " + "; ".join(blocks)
			if fields.get("disk_override") and any(c[0] == "block" for c in self.checks):
				Say("Started though the disk check says it doesn't fit (confirmed on the page): {}".format(next(
					c[1] for c in self.checks if c[0] == "block")))
			# The page's "Copy each recording to": a folder this computer reaches, kept for the next panel too
			to = str(fields.get("copy_to") or "").strip()
			problem = copyqueue.Problem(to, self.config.Value("videoFolder", "."))
			if problem:
				return problem
			copyqueue.Remember(copyqueue.RememberFile(), self.config.path, to)
			self.close_when_copied = False # recording again: the page's earlier Close no longer stands
			self.wanted = fields
			self.error = None
		return None

	def Args(self, fields):
		# The experiment's fields, as campy-acquire's options for this run (session.json); with a Pico, the session's
		# length (recTimeInSec, from its plan) and folder
		args = list(self.args)
		if self.devices is not None:
			args += self.devices.Args(self.length)
		experiment = " ".join(str(fields.get("experiment") or "").split())
		if experiment:
			args += ["--experiment", experiment]
		ids = [a.strip() for a in str(fields.get("animal_ids") or "").replace(";", ",").split(",") if a.strip()]
		if ids:
			args += ["--animalIds", strictjson.dumps(ids)]
		notes = " ".join(str(fields.get("notes") or "").split())
		if notes:
			args += ["--notes", notes]
		return args

	# ---------------------------------------------------------- while it records

	def Stop(self, level, mode="now"):
		s = self.session
		if s is None:
			return "Nothing is recording."
		if self.devices is not None and level == 1:
			# The Pico (after its bout, or now), then campy, then SpikeGLX: from the session's main thread
			if mode not in ("bout", "now"):
				return "Stop after this bout, or now?"
			self.devices.Ask("stop", mode)
			Say("Stop ({}) asked from the page.".format("after this bout" if mode == "bout" else "now"))
			return None
		if not s.RequestStop(level):
			return None if s.stop_written >= level else "campy-acquire isn't running."
		Say("Stop{} asked from the page.".format(" now" if level == 2 else ""))
		return None

	def StopOrphan(self, now=False):
		orphan = Orphan()
		if orphan is None:
			return "No earlier recording is running."
		try:
			stopfile.Write(orphan["stop"], now=now)
		except OSError as e:
			return "Could not write its stop file: {}".format(e)
		Say("Stop{} asked of the earlier recording in {}.".format(" now" if now else "", orphan["folder"]))
		return None

	def StartPico(self, at):
		s = self.session
		if self.devices is None or self.devices.pico is None:
			return "No Pico in this config."
		if s is None or s.state != "recording":
			return "campy isn't recording yet."
		if at:
			try:
				at = devices.When(at)
			except session.SessionError as e:
				return str(e)
		else:
			why = self.devices.CanStartPico(s)
			if why:
				return why
		self.devices.Ask("start-pico", at or None)
		return None

	def StopLeft(self, what):
		# The Pico's session or SpikeGLX's run, left running (campy failed, or the panel's window was closed)
		if self.devices is None:
			return "No Pico or SpikeGLX in this config."
		if self.session is not None:
			return "A recording is running: use Stop."
		return self.devices.StopLeft(what)

	def PauseUpdates(self):
		from campy import winsetup
		seconds = self.devices.Length(self.length)[2]["ends_s"] if self.devices is not None and self.devices.pico \
			is not None else float(self.config.Value("recTimeInSec", 0) or 0)
		ok, said = winsetup.PauseUpdates(winsetup.Days(seconds))
		Say("Pause Windows Update: {}".format(said))
		self.checks = self.Checks()
		return None if ok else "Windows Update was not paused: {}".format(said)

	def Models(self, body, scan=False):
		# The page's model pick ({set} or {centroid[, instance]}), or a look through panel.models again
		if self.picker is None:
			return "This config has no trained models to pick (live: model: sleap)."
		if self.session is not None or self.wanted is not None:
			return "A recording is running: the models change for the next one."
		if scan:
			self.picker.Scan()
			error = None
		else:
			error = self.picker.Choose(body.get("centroid"), body.get("instance") if "instance" in body else None,
				body.get("set"))
		self.checks = self.Checks()
		return error

	def Bouts(self, body):
		# The page's bout settings ({values: {setting: value}}, or {reset: true} for the config's), for this panel's
		# recordings: every length's plan, the disk check and the checks again
		if self.devices is None or self.devices.pico is None:
			return "This config has no Pico."
		if self.session is not None or self.wanted is not None:
			return "A recording is running: the bout settings change for the next one."
		if body.get("reset"):
			self.devices.pico.page, self.devices.pico.spans, self.devices.pico.remembered, error = {}, {}, None, None
		else:
			values = body.get("values")
			if not isinstance(values, dict):
				return "No bout settings given."
			error = self.devices.pico.Choose(values)
		if error is None:
			pico.Remember(pico.RememberFile(), self.config.path, self.devices.pico.page) # for the next panel
		self.lengths = self.devices.Lengths()
		self.checks = self.Checks()
		return error

	def Close(self, leave=False):
		"""The page's Close. With copies still running, the panel closes by itself once they are done
		(close_when_copied); leave closes it now, and each folder not copied says how to copy it by hand."""
		if self.session is not None or self.wanted is not None:
			return "A recording is running: the panel closes once it has ended and you close it."
		if self.copies.Pending() and not leave:
			if not self.close_when_copied:
				Say("Closing once the copies are done ({} left).".format(len(self.copies.Pending())))
			self.close_when_copied = True
			return None
		for job in self.copies.Leave():
			Say("Not copied: {} ({}).".format(job.folder, job.message))
		self.closing = True
		return None

	def MakeCopyFolder(self, path):
		# The page's Create this folder, for a "Copy each recording to" folder that isn't there yet
		error = copyqueue.Create(path, self.config.Value("videoFolder", "."))
		if not error:
			Say("Made the copy folder {} (Create this folder, on the page).".format(str(path).strip()))
		return error

	def CopyTo(self):
		# What the page's box starts from: the folder this config's recordings were last copied into from a page, or
		# the config's own post copy.to
		remembered = copyqueue.Remembered(copyqueue.RememberFile(), self.config.path)
		if remembered is not None:
			return remembered
		copy = (self.config.post or {}).get("copy") or {}
		return str(copy.get("to") or "") if isinstance(copy, dict) else str(copy or "")

	def Viewer(self):
		s = self.session
		if s is None or s.state != "recording":
			return "Nothing is recording."
		s.view_wanted = True # the main thread starts it (a child tied to this process must start from it)
		return None

	def Bookmark(self, note):
		s = self.session
		folder = s.folder if s is not None and s.folder else None
		if folder is None:
			return "Nothing is recording.", None
		if self.marks is None or self.marks.fallback != os.path.join(folder, bookmarks.BOOKMARKS):
			self.marks = bookmarks.Bookmarks(os.path.join(folder, bookmarks.BOOKMARKS), say=Say)
		cameras = self.health.Cameras()
		frames = [f for f, _ in cameras if f]
		mark = {"time": datetime.now().isoformat(" ", "milliseconds"), "host_ns": clock.HostNs(), "event": "mark",
			"note": " ".join(str(note or "").split()), "frame": min(frames) if frames else None, "cameras": cameras,
			"recording": folder}
		if self.marks.Add(mark) is None:
			return "Could not write the bookmark.", None
		Say("Bookmark {}, in {}.".format(bookmarks.MarkText(mark), mark["path"]))
		return None, mark

	# ---------------------------------------------------------- what the page shows

	def Status(self):
		s = self.session
		launcher = s.StatusDict() if s is not None and s.folder else self.last
		folder = (launcher or {}).get("folder")
		free = self.Free() if not folder else self.FreeAt(folder)
		now = time.time()
		if free is not None:
			self.disk.append((now, free))
		rate = None
		if len(self.disk) >= 2 and self.disk[-1][0] - self.disk[0][0] >= 10:
			rate = (self.disk[0][1] - self.disk[-1][1]) / (self.disk[-1][0] - self.disk[0][0])
		recording = RecordingStatus(folder) if folder else None
		open_, resolved = Alerts(folder) if folder else ([], [])
		# Once it has ended (its post steps too): what went wrong in it, until the next recording starts
		ended = folder and (s is None or s.state in ("post", "done", "failed", "stopped"))
		summary = self.summaries.Get(folder) if ended else None
		problems = list((launcher or {}).get("problems") or [])
		marks = [{"number": m["number"], "time": m["time"][11:19], "frame": m["frame"], "note": m["note"]}
			for m in reversed(self.marks.recent)] if self.marks is not None and folder and \
			self.marks.fallback.startswith(folder) else []
		state = "starting" if self.wanted is not None else (s.state if s is not None else "ready")
		if s is not None and state in ("done", "failed", "stopped"):
			state = "finishing" # the recording has ended, and the panel is letting go of it: Start can't take one yet
		return {"state": state, "rig": platform.node(), "config": self.config.path, "error": self.error,
			"version": getattr(self, "version", None),
			"types": self.types.Status(self.config.path) if getattr(self, "types", None) is not None else None,
			"orphan": Orphan() if s is None else None,
			"checks": [{"level": c[0], "text": c[1], "action": c[2] if len(c) > 2 else None} for c in self.checks],
			"devices": (launcher or {}).get("devices") if s is not None and s.folder else
				self.devices.Status() if self.devices is not None else None,
			"lengths": [{"name": l["name"], "seconds": l["seconds"], "text": l["plan"].get("text") or l["plan"].get("error"),
				"videoFolder": l["videoFolder"]} for l in self.lengths], "length": self.length,
			"bouts": self.BoutStatus(),
			"launcher": launcher, "health": self.health.snapshot, "recording": recording,
			"models": self.picker.Status() if getattr(self, "picker", None) is not None else None,
			"disk": self.Disk(free, rate, s),
			"alerts": {"open": open_, "resolved": resolved}, "problems": problems, "bookmarks": marks, "summary": summary,
			"copies": dict(self.copies.Status(), close_when_copied=self.close_when_copied),
			"fields": {"experiment": str(self.config.Value("experiment", "") or "").replace("None", ""),
				"animal_ids": ", ".join(str(a) for a in (self.config.Value("animalIds", []) or [])),
				"copy_to": self.CopyTo()}}

	def BoutStatus(self):
		# The page's bout settings box, with a Pico's bouts (none for a camera-only session)
		if self.devices is None or self.devices.pico is None:
			return None
		try:
			return self.devices.pico.PageStatus()
		except pico.PicoError as e:
			return {"error": str(e), "fields": [], "changed": [], "bout": None}

	def Disk(self, free, rate, s):
		"""
		The page's disk tile: the free space, and how long it lasts at the
		rate it fills (hours_left). With a Pico's session, what the rest of
		its plan still needs (plan_left_gb, devices.Need) in place of the
		hours: the rate before the Pico starts is the cameras' none at all,
		and SpikeGLX writes only while the gate is open.
		"""
		disk = {"free_gb": round(free / 1e9, 1) if free is not None else None, "hours_left": None, "plan_left_gb": None}
		try:
			left = self.devices.NeedLeft(self.config.Cameras()[0]) if self.devices is not None and s is not None and \
				s.folder else None
		except (session.SessionError, pico.PicoError):
			left = None
		if left is not None:
			disk["plan_left_gb"] = round(left["total_b"] / 1e9, 1)
		elif free is not None and rate and rate > 1e5 and not (self.devices is not None and self.devices.pico is not None):
			disk["hours_left"] = round(free / rate / 3600, 1)
		return disk

	@staticmethod
	def FreeAt(folder):
		try:
			return shutil.disk_usage(folder).free
		except OSError:
			return None

	# ---------------------------------------------------------- running it

	def Serve(self):
		panel = self
		from campy import panelpage

		class Handler(http.server.BaseHTTPRequestHandler):
			def log_message(self, *args):
				pass

			def Refused(self, query):
				# Why this request is refused ("key": not the page's key), or None
				if not LocalName(self.headers.get("Host")) or not LocalOrigin(self.headers.get("Origin")):
					return "this request is not from this computer's panel page: open the address campy-session printed"
				given = query.get("t", [""])[0] or self.headers.get("X-Campy-Token", "")
				if not (given.isascii() and secrets.compare_digest(given, panel.token)): # compare_digest: ASCII only
					return "key"
				return None

			def Send(self, code, body, kind="application/json"):
				data = body if isinstance(body, bytes) else strictjson.dumps(body).encode()
				self.send_response(code)
				self.send_header("Content-Type", kind)
				self.send_header("Content-Length", str(len(data)))
				self.send_header("Cache-Control", "no-store")
				self.end_headers()
				self.wfile.write(data)

			def do_GET(self):
				self.Safely(self.Get)

			def do_POST(self):
				self.Safely(self.Post)

			def Safely(self, handle):
				# A mistake in the panel answers 500 with what it was (and its traceback in the console), never a
				# dropped connection
				try:
					handle()
				except Exception as e:
					import traceback
					Say("Error answering {} {}:\n{}".format(self.command, urlparse(self.path).path, traceback.format_exc()))
					try:
						self.Send(500, {"error": "the panel failed: {}: {}".format(type(e).__name__, e)})
					except Exception:
						pass

			def Get(self):
				url = urlparse(self.path)
				query = parse_qs(url.query)
				why = self.Refused(query)
				if why:
					return self.Send(403, {"error": "this page's address has the wrong key: open the one campy-session "
						"printed" if why == "key" else why})
				if url.path == "/":
					return self.Send(200, panelpage.PAGE.encode(), "text/html; charset=utf-8")
				if url.path == "/status.json":
					return self.Send(200, panel.Status())
				if url.path.startswith("/thumb/") and url.path.endswith(".png"):
					try:
						png = panel.health.thumbs.get(int(url.path[7:-4]))
					except ValueError:
						png = None
					return self.Send(200, png, "image/png") if png else self.Send(404, {"error": "no frame yet"})
				return self.Send(404, {"error": "no such page"})

			def Post(self):
				url = urlparse(self.path)
				query = parse_qs(url.query)
				why = self.Refused(query)
				if why:
					return self.Send(403, {"error": "wrong key" if why == "key" else why})
				try:
					size = int(self.headers.get("Content-Length") or 0)
					body = json.loads(self.rfile.read(size) or b"{}") if size else {}
				except ValueError:
					return self.Send(400, {"error": "not JSON"})
				if url.path == "/start":
					error = panel.Start(body)
				elif url.path == "/stop":
					error = panel.Stop(2 if body.get("now") else 1, body.get("mode") or "now")
				elif url.path == "/pico-start":
					error = panel.StartPico(body.get("at"))
				elif url.path == "/stop-left":
					error = panel.StopLeft(str(body.get("what")))
				elif url.path == "/pause-updates":
					error = panel.PauseUpdates()
				elif url.path == "/length":
					if panel.session is not None or panel.devices is None:
						error = "Not now."
					else:
						try:
							panel.length, error = panel.devices.Length(body.get("seconds"))[0], None
							panel.checks = panel.Checks()
						except session.SessionError as e:
							error = str(e)
				elif url.path == "/close":
					error = panel.Close(bool(body.get("leave")))
					if not error and not panel.closing:
						return self.Send(200, {"ok": True, "waiting": len(panel.copies.Pending())})
				elif url.path == "/stop-orphan":
					error = panel.StopOrphan(bool(body.get("now")))
				elif url.path == "/viewer":
					error = panel.Viewer()
				elif url.path == "/bookmark":
					error, mark = panel.Bookmark(body.get("note", ""))
				elif url.path == "/bouts":
					error = panel.Bouts(body)
				elif url.path == "/models":
					error = panel.Models(body)
				elif url.path == "/models-scan":
					error = panel.Models(body, scan=True)
				elif url.path == "/type":
					error = panel.Choose(body.get("path"))
				elif url.path == "/favorite":
					error = panel.Favorite(body.get("path"), body.get("on"))
				elif url.path == "/types-scan":
					error = panel.Rescan()
				elif url.path == "/copy-folder":
					error = panel.MakeCopyFolder(body.get("path"))
				elif url.path == "/check":
					panel.checks, error = panel.Checks(), None
				else:
					return self.Send(404, {"error": "no such action"})
				if error and url.path == "/start" and copyqueue.Creatable(body.get("copy_to")):
					# The copy folder isn't there yet, though the folder it goes in is: the page offers to make it
					return self.Send(409, {"error": error, "create": str(body.get("copy_to")).strip()})
				return self.Send(409 if error else 200, {"error": error} if error else {"ok": True})

		for port in range(self.port, self.port + 20):
			try:
				server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
				break
			except OSError:
				continue
		else:
			raise session.SessionError("No free port from {} to {} for the panel.".format(self.port, self.port + 19))
		server.daemon_threads = True
		self.server, self.port = server, port
		threading.Thread(target=server.serve_forever, name="campy panel server", daemon=True).start()
		self.url = "http://127.0.0.1:{}/?t={}".format(port, self.token)
		return self.url

	def Run(self):
		lock = session.Lock().Take()
		try:
			self.health.Start()
			url = self.Serve()
			try:
				session.WriteJson(PanelFile(), {"pid": os.getpid(), "url": url, "config": self.opened, "started": datetime.now().astimezone().isoformat(timespec="seconds")}, private=True)
			except OSError as e:
				Say("Could not write {} ({}): a second launch won't find this panel.".format(PanelFile(), e))
			Say("The panel: {}".format(url))
			Say("Open it in this computer's browser (over Remote Desktop too); from another computer through an SSH "
				"tunnel: ssh -L {0}:127.0.0.1:{0} <this computer>. Ctrl-C here closes the panel when nothing records."
				.format(self.port))
			if self.browser:
				try:
					webbrowser.open(url)
				except Exception:
					pass
			while True:
				try:
					while self.wanted is None and not self.closing:
						if self.close_when_copied and not self.copies.Pending():
							self.closing = True
							break
						time.sleep(0.2)
				except KeyboardInterrupt:
					for job in self.copies.Leave():
						Say("Not copied: {} ({}).".format(job.folder, job.message))
					Say("Closed.")
					return 0
				if self.closing:
					Say("Closed from the page.")
					return 0
				self.Record()
		finally:
			self.health.Stop()
			if getattr(self, "server", None) is not None:
				self.server.shutdown()
			try:
				with open(PanelFile()) as f:
					if strictjson.load(f).get("pid") == os.getpid():
						os.remove(PanelFile())
			except (OSError, ValueError, AttributeError):
				pass
			lock.Release()

	def Record(self):
		# One recording, in this (the main) thread: campy-session's Ctrl-C and its tied children need it
		fields, args = self.wanted, self.Args(self.wanted)
		copy_to = str(fields.get("copy_to") or "").strip()
		try:
			pick = self.picker.Pick() if self.picker is not None else None
			page = dict(self.devices.pico.page) if self.devices is not None and self.devices.pico is not None else None
			config = session.Config(self.config_path, args, live=pick, pico_set=page)
			# The page's box copies it, in the background, once it has ended: not the post copy step, which would keep
			# the next recording waiting
			config.post["copy"] = {}
			if page:
				Say("Bout settings from the page: {}".format(self.devices.PageText()))
			if pick:
				Say("Live models: centroid {}, instance {}".format(pick["centroid"], pick["instance"]))
				self.picker.Remember()
			if self.devices is not None:
				self.devices.config = config
				self.devices.Plan(self.length, fields.get("pico_at") or None)
			s = session.Session(config, lock=False, detached=True, devices=self.devices)
			self.session = s
			self.wanted = None
			s.Run()
		except session.SessionError as e:
			self.error = str(e)
			Say(str(e))
		finally:
			self.wanted = None
			if self.session is not None and self.session.folder:
				self.last = Ended(self.session.StatusDict())
				if copy_to:
					self.copies.Add(self.session.folder, copy_to)
			self.session = None
			self.checks = self.Checks()


def GB(n):
	# Bytes as the page says them: "7.5 GB", "2,910 GB"
	return "{:,.1f} GB".format(n / 1e9) if n < 9.95e9 else "{:,.0f} GB".format(n / 1e9)


def Duration(seconds):
	try:
		s = int(float(seconds))
	except (TypeError, ValueError):
		return "?"
	return "{}:{:02d}:{:02d}".format(s // 3600, s // 60 % 60, s % 60)


def RigPy(setting):
	"""The rig.py that panel.rig_lock names: "auto" is the rig's installed one (RIG_ROOT, else its usual folder)."""
	if setting != "auto":
		return os.path.abspath(os.path.expanduser(setting))
	root = os.environ.get("RIG_ROOT") or ("C:\\campy_data\\live" if sys.platform == "win32" else
		os.path.expanduser("~/sleap-bench"))
	return os.path.join(root, "rig.py")


def IgnoreCtrlC():
	"""
	Ctrl-C in this window, from now on, does nothing: under rig.py it would
	reach rig.py too, which then ends its whole job (the panel, campy, the
	Pico's session program) before anything is stopped in order. Windows:
	the console's processed input off, so Ctrl-C is only a key; elsewhere
	SIGINT ignored, which every program started from here inherits.
	"""
	if sys.platform == "win32":
		import ctypes
		from ctypes import wintypes
		k32 = ctypes.WinDLL("kernel32", use_last_error=True)
		k32.GetStdHandle.restype = wintypes.HANDLE
		k32.GetStdHandle.argtypes = (wintypes.DWORD,)
		k32.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
		k32.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
		handle = k32.GetStdHandle(wintypes.DWORD(-10 & 0xFFFFFFFF)) # STD_INPUT_HANDLE
		mode = wintypes.DWORD()
		if k32.GetConsoleMode(handle, ctypes.byref(mode)):
			k32.SetConsoleMode(handle, mode.value & ~0x0001) # ENABLE_PROCESSED_INPUT
	else:
		import signal
		signal.signal(signal.SIGINT, signal.SIG_IGN)


def UnderRig(rig_lock, owner, name, argv):
	"""
	With panel.rig_lock: this panel run again under rig.py (the rig's lock,
	held while the panel is open), unless it already runs in the rig's job.
	Its exit code, or None to go on here. name: the config's (or the
	folder's), for the job's tag.
	"""
	if not rig_lock or os.environ.get("RIG_JOB"):
		return None
	rig = RigPy(rig_lock)
	if not os.path.isfile(rig):
		raise session.SessionError("panel.rig_lock: {} isn't there (the rig's rig.py).".format(rig))
	logs = os.path.join(session.CampyFolder(), "logs")
	os.makedirs(logs, exist_ok=True)
	tag = "panel-{}-{}".format(name, datetime.now().strftime("%Y%m%d-%H%M%S"))
	Say("Taking the rig's lock ({}): the panel waits for the rig to be free, then holds it while it is open. Ctrl-C "
		"does nothing in this window: close the panel from its page.".format(rig))
	IgnoreCtrlC()
	import subprocess
	return subprocess.call([session.Python(), rig, "run", "--log-dir", logs, owner or "panel", tag, "--",
		session.Python(), "-m", "campy.session", "panel"] + list(argv))


def Main(argv=None):
	p = argparse.ArgumentParser(prog="campy-session panel", description="Record from a page in the browser: Start, "
		"each camera's health, disk space, bookmarks, Stop (python -m pydoc campy.panel).",
		epilog="Any other --key value is campy-acquire's, for every recording the panel starts.")
	p.add_argument("config", help="the config, as for campy-session run; or a folder of configs: one panel for all of "
		"them, with an Experiment list on its page")
	p.add_argument("--port", type=int, default=PORT, help="the page's port on 127.0.0.1 (the next free one if taken)")
	p.add_argument("--no-browser", action="store_true", help="only print the page's address")
	argv = sys.argv[1:] if argv is None else list(argv)
	args, rest = p.parse_known_args(argv)
	running = Running()
	if running is not None:
		# Opened again (the desktop icon, its page closed): that panel's page, not a second panel
		Say("The panel is already open (pid {}): {}".format(running["pid"], running["url"]))
		if running.get("config") and not Same(args.config, running["config"]):
			Say("It was opened with {}, not {}: close it (Close the panel, on its page) to open the panel with "
				"this one.".format(running["config"], os.path.abspath(args.config)))
		if not args.no_browser:
			try:
				webbrowser.open(running["url"])
			except Exception:
				pass
		return 0
	try:
		if os.path.isdir(args.config):
			rig_lock, owner = Types(args.config, rest).RigLock()
			name = os.path.basename(os.path.normpath(os.path.abspath(args.config)))
		else:
			config = session.Config(args.config, rest)
			rig_lock, owner = config.panel["rig_lock"], config.panel["owner"]
			name = os.path.splitext(os.path.basename(config.path))[0]
		code = UnderRig(rig_lock, owner, name, argv)
		if code is not None:
			return code
		return Panel(args.config, rest, port=args.port, browser=not args.no_browser).Run()
	except session.SessionError as e:
		print("campy-session panel: {}".format(e), file=sys.stderr)
		return 1
