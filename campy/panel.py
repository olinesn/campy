"""
campy-session panel: a recording run from a page in the browser.

  campy-session panel <config>.yaml [--key value ...] [--port 8765] [--no-browser]

It opens a page (http://127.0.0.1:<port>/?t=<key>) where the recording is
started (the Start button, the only way: never on its own), watched and
stopped, as campy-session run would run it (campy/session.py): the same
programs, the same folder and logs, the same steps after it. One recording
after another, each into a folder of its own, until the panel is closed
(Ctrl-C in its console when nothing records).

What the page shows, once a second:

- before the start: the experiment's fields (experiment, animal IDs, notes,
  for session.json) and the checks: what stops a start (campy-live's
  environment not found or not the same campy, another campy-session
  running) apart from what only warns (disk space). The recording's length
  is the config's recTimeInSec.
- while it records: elapsed and planned time; each camera's frames/s,
  frames and health, from the cameras' rings with campy-live-view's own
  checks (campy/livehealth.py: NO FRAMES, behind, LOST, GAP, LATE, frames
  ahead or behind), and their dropped frames and write queue from
  campy-acquire's watch (~/.campy/status/recording.json, when there); a
  grey thumbnail of each camera, once a second; disk space and the time left
  at the rate it fills; campy-live's 3D (animals with good 3D in the last
  second) from its results feed; the warnings (the alerts' logs in the
  recording's folder, campy-session's problems); bookmarks, written into the
  recording's bookmarks.csv as campy-live-view writes them; each program's
  state, and a button to open the view again.
- after it: each post-step's progress (campy/post.py).

The page is served from this computer only (127.0.0.1): over Remote
Desktop, open it in the rig's browser; from another computer, through an SSH
tunnel (ssh -L 8765:127.0.0.1:8765 <rig>). Every request needs the key in
the page's address, so no other program on the computer can press Start
or Stop. Closing the page stops nothing; it can be opened again at any time.

Only the standard library and numpy (campy's recording environment): the
page is one file of HTML and JavaScript (campy/panelpage.py), no build step.
"""

import argparse, collections, http.server, json, os, platform, secrets, shutil, struct, sys, threading, time, \
	webbrowser, zlib
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import numpy as np

from campy import bookmarks, children, clock, livefeed, livehealth, liveresults, session, stopfile, strictjson

PORT = 8765
THUMB_WIDTH = 192 # px, each camera's thumbnail
SAMPLE_S = 0.25 # how often the rings' slots are read (campy-live-view: about 20 times a second)
THUMB_S = 1.0 # and a frame of each camera, for its thumbnail
STATUS_FRESH_S = 20 # recording.json older than this is not the recording's now


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


class Health:
	"""
	The cameras' rings and campy-live's results, read from a thread of its
	own as campy-live-view reads them (only the slots' fields, and a frame a
	second for the thumbnails): livehealth's Rig and Tracking. It attaches
	when a recording's rings appear, and again for the next recording.
	"""

	def __init__(self, name, cameras, animals=0):
		self.names = [livefeed.RingName(name, c) for c in range(cameras)]
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
			readers[0].slots)
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
				"problems": [tag for tag, _ in c.problems], "sentences": [text for _, text in c.problems]})
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
	return os.environ.get("CAMPY_STATUS_DIR") or os.path.join(session.CampyFolder(), "status")


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


# ------------------------------------------------------------------ the panel

class Panel:
	def __init__(self, config_path, args=(), port=PORT, browser=True):
		self.config_path, self.args = config_path, list(args)
		self.config = session.Config(config_path, self.args) # a config campy-session can't use stops here
		self.port, self.browser = port, browser
		self.token = secrets.token_urlsafe(18)
		self.session = None
		self.last = None # the last recording's launcher status, after it
		self.error = None # why the last start failed, before it recorded
		self.wanted = None # the Start button's fields, for the main thread
		self.lock = threading.Lock()
		n = self.config.Cameras()[0]
		live = self.config.live or {}
		self.health = Health(str(self.config.Value("liveFeedName", "campy-live")), n, live.get("animals", 0))
		self.marks = None
		self.disk = collections.deque(maxlen=60) # (time, free bytes), for the rate the disk fills
		self.checks = self.Checks()

	# ---------------------------------------------------------- before the start

	def Checks(self):
		"""[(level, text)]: "block" stops a start, "warn" only warns."""
		checks = []
		live = self.config.live
		if live is not None:
			try:
				python, where = session.LivePython(live)
				found = session.CheckLiveEnv(python, dict(os.environ, **{str(k): str(v) for k, v in live["env"].items()}))
				if live["model"] == "sleap" and not (found["torch"] and found["sleap_nn"]):
					checks.append(("block", "campy-live's environment ({}) has no torch or sleap-nn".format(python)))
				else:
					checks.append(("ok", "campy-live's environment: {} ({})".format(python, where)))
			except session.SessionError as e:
				checks.append(("block", str(e)))
		n, names = self.config.Cameras()
		planned = self.config.Value("recTimeInSec", 0)
		checks.append(("ok", "{} cameras ({}), {} each (recTimeInSec)".format(n, ", ".join(names), Duration(planned))))
		free = self.Free()
		if free is not None:
			least = self.config.Value("alertDiskFreeGB", 20)
			try:
				least = float(least)
			except (TypeError, ValueError):
				least = 20.0
			checks.append(("warn" if free / 1e9 < least else "ok", "{:.0f} GB free where it records".format(free / 1e9)))
		return checks

	def Free(self):
		where = os.path.abspath(str(self.config.Value("videoFolder", ".")))
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
			self.checks = self.Checks()
			blocks = [text for level, text in self.checks if level == "block"]
			if blocks:
				return "Can't start: " + "; ".join(blocks)
			self.wanted = fields
			self.error = None
		return None

	def Args(self, fields):
		# The experiment's fields, as campy-acquire's options for this run (session.json)
		args = list(self.args)
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

	def Stop(self, level):
		s = self.session
		if s is None:
			return "Nothing is recording."
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
		problems = list((launcher or {}).get("problems") or [])
		marks = [{"number": m["number"], "time": m["time"][11:19], "frame": m["frame"], "note": m["note"]}
			for m in reversed(self.marks.recent)] if self.marks is not None and folder and \
			self.marks.fallback.startswith(folder) else []
		state = "starting" if self.wanted is not None else (s.state if s is not None else "ready")
		return {"state": state, "rig": platform.node(), "config": self.config.path, "error": self.error,
			"orphan": Orphan() if s is None else None,
			"checks": [{"level": level, "text": text} for level, text in self.checks],
			"launcher": launcher, "health": self.health.snapshot, "recording": recording,
			"disk": {"free_gb": round(free / 1e9, 1) if free is not None else None,
				"hours_left": round(free / rate / 3600, 1) if free is not None and rate and rate > 1e5 else None},
			"alerts": {"open": open_, "resolved": resolved}, "problems": problems, "bookmarks": marks,
			"fields": {"experiment": str(self.config.Value("experiment", "") or "").replace("None", ""),
				"animal_ids": ", ".join(str(a) for a in (self.config.Value("animalIds", []) or []))}}

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

			def Allowed(self, query):
				return secrets.compare_digest(query.get("t", [""])[0] or self.headers.get("X-Campy-Token", ""),
					panel.token)

			def Send(self, code, body, kind="application/json"):
				data = body if isinstance(body, bytes) else strictjson.dumps(body).encode()
				self.send_response(code)
				self.send_header("Content-Type", kind)
				self.send_header("Content-Length", str(len(data)))
				self.send_header("Cache-Control", "no-store")
				self.end_headers()
				self.wfile.write(data)

			def do_GET(self):
				url = urlparse(self.path)
				query = parse_qs(url.query)
				if not self.Allowed(query):
					return self.Send(403, {"error": "this page's address has the wrong key: open the one campy-session "
						"printed"})
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

			def do_POST(self):
				url = urlparse(self.path)
				query = parse_qs(url.query)
				if not self.Allowed(query):
					return self.Send(403, {"error": "wrong key"})
				try:
					size = int(self.headers.get("Content-Length") or 0)
					body = json.loads(self.rfile.read(size) or b"{}") if size else {}
				except ValueError:
					return self.Send(400, {"error": "not JSON"})
				if url.path == "/start":
					error = panel.Start(body)
				elif url.path == "/stop":
					error = panel.Stop(2 if body.get("now") else 1)
				elif url.path == "/stop-orphan":
					error = panel.StopOrphan(bool(body.get("now")))
				elif url.path == "/viewer":
					error = panel.Viewer()
				elif url.path == "/bookmark":
					error, mark = panel.Bookmark(body.get("note", ""))
				elif url.path == "/check":
					panel.checks, error = panel.Checks(), None
				else:
					return self.Send(404, {"error": "no such action"})
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
					while self.wanted is None:
						time.sleep(0.2)
				except KeyboardInterrupt:
					Say("Closed.")
					return 0
				self.Record()
		finally:
			self.health.Stop()
			if getattr(self, "server", None) is not None:
				self.server.shutdown()
			lock.Release()

	def Record(self):
		# One recording, in this (the main) thread: campy-session's Ctrl-C and its tied children need it
		fields, args = self.wanted, self.Args(self.wanted)
		try:
			config = session.Config(self.config_path, args)
			s = session.Session(config, lock=False, detached=True)
			self.session = s
			self.wanted = None
			s.Run()
		except session.SessionError as e:
			self.error = str(e)
			Say(str(e))
		finally:
			self.wanted = None
			if self.session is not None and self.session.folder:
				self.last = self.session.StatusDict()
			self.session = None
			self.checks = self.Checks()


def Duration(seconds):
	try:
		s = int(float(seconds))
	except (TypeError, ValueError):
		return "?"
	return "{}:{:02d}:{:02d}".format(s // 3600, s // 60 % 60, s % 60)


def Main(argv=None):
	p = argparse.ArgumentParser(prog="campy-session panel", description="Record from a page in the browser: Start, "
		"each camera's health, disk space, bookmarks, Stop (python -m pydoc campy.panel).",
		epilog="Any other --key value is campy-acquire's, for every recording the panel starts.")
	p.add_argument("config", help="the config, as for campy-session run")
	p.add_argument("--port", type=int, default=PORT, help="the page's port on 127.0.0.1 (the next free one if taken)")
	p.add_argument("--no-browser", action="store_true", help="only print the page's address")
	args, rest = p.parse_known_args(argv)
	try:
		return Panel(args.config, rest, port=args.port, browser=not args.no_browser).Run()
	except session.SessionError as e:
		print("campy-session panel: {}".format(e), file=sys.stderr)
		return 1
