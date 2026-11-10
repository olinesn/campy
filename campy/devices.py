"""
The devices around a recording, for campy-session and its panel: a Pico that
makes the trigger and the experiment's TTLs (the config's pico: section,
campy/pico.py), SpikeGLX recording what the gate opens (spikeglx:,
campy/spikeglx.py), and the Windows settings that can end a long recording
(windows:, campy/winsetup.py). Each section is optional; a config with none
records as before.

A recording with them, in order (campy/session.py calls each step):

1. Checks, before Start: the Pico answers and is idle; SpikeGLX answers (or
   can be opened), is idle and its trigger is TTL; Windows Update paused past
   the recording's end, USB selective suspend off (warnings only). The
   session's length is one of pico.lengths (the page's choice), and campy's
   recTimeInSec is worked out from the Pico's plan, so the two always agree.
2. Before campy starts, in the recording's folder: SpikeGLX's run into
   spikeglx/ with recording enabled, then the gate blinked once: SpikeGLX
   must open a file for it, or nothing starts (and SpikeGLX's run stops).
3. campy starts (and campy-live, the view, as always). Once every camera is
   ready, the Pico's session starts when the page's "Start the Pico" is
   pressed, or at the time set on the page; its log goes to pico/.
4. The end. campy stops by itself after the plan's last frame; the Pico's
   last gate falls a little later (10 s in the default plan), so it is
   waited for (pico.last_gate_s), then SpikeGLX's run stops, closing its
   last file. Stop on the page asks first, then either lets the Pico finish
   its bout (stop after this bout: the bout's SpikeGLX file closes on time),
   then stops campy and SpikeGLX; or stops the Pico at once, then campy,
   then SpikeGLX. If campy fails on its own, the Pico and SpikeGLX are left
   running (an animal's LED schedule goes on, and SpikeGLX records it), and
   the page says so, with a button to stop each.
"""

import os, threading, time
from datetime import datetime

from campy import pico as picomod, session as sessionmod, spikeglx as sglxmod, strictjson, winsetup

STATUS_EVERY_S = 2.0
GATE_FILE_S = 10.0 # how long SpikeGLX has to open a file for the gate's blink


def Say(text):
	sessionmod.Say(text)


class Devices:
	def __init__(self, config, say=Say):
		self.config, self.say = config, say
		try:
			fps = float(config.Value("frameRate", 0)) or None
		except (TypeError, ValueError):
			fps = None
		try:
			self.pico = picomod.Pico(config.pico, sessionmod.Python(), fps) if config.pico is not None else None
		except picomod.PicoError as e:
			raise sessionmod.SessionError(str(e))
		try:
			self.sglx = sglxmod.SpikeGLX(config.spikeglx, say) if config.spikeglx is not None else None
		except sglxmod.SpikeGLXError as e:
			raise sessionmod.SessionError(str(e))
		if self.pico is not None and self.pico.section["gate_check"] and self.sglx is None:
			raise sessionmod.SessionError("pico.gate_check needs SpikeGLX to check the gate with: give a spikeglx: "
				"section, or gate_check: false.")
		self.windows = config.windows
		self.lock = threading.Lock()
		self.Reset()
		self.cached, self.cached_at = {}, 0.0

	def Reset(self):
		# Before each recording
		self.length = None # the session's seconds (the Pico's TOTAL_DURATION)
		self.plan = None
		self.sets = None # chronic_control.py's --set for this recording, as planned (the page's bout settings in them)
		self.settings = None # {settings, from_page}: what the Pico runs, for launcher.json
		self.steps = []
		self.folder = None
		self.wanted = [] # the page's asks, for the main thread: ("start-pico", at or None), ("stop", "bout" or "now")
		self.start_at = None # when the Pico starts by itself (time.time())
		self.pico_started = None # when it started
		self.pico_said = None
		self.sglx_started = False
		self.stop_mode = None
		self.stop_campy_after_pico = False
		self.left_running = None # what was left running after campy failed on its own
		self.cameras_ready = None # (ready, cameras) as the page last showed it

	def Enabled(self):
		return self.pico is not None or self.sglx is not None or self.windows is not None

	# ---------------------------------------------------------- before Start

	def Lengths(self):
		"""[{name, seconds, videoFolder, plan}]: the lengths the page offers, each with its plan."""
		if self.pico is None:
			return []
		lengths = self.pico.lengths or [{"name": picomod.Clock(self.pico.Settings(None)["TOTAL_DURATION"]),
			"seconds": int(self.pico.Settings(None)["TOTAL_DURATION"]), "videoFolder": None}]
		out = []
		for length in lengths:
			try:
				plan = self.pico.Plan(length["seconds"])
			except picomod.PicoError as e:
				plan = {"error": str(e)}
			out.append(dict(length, plan=plan))
		return out

	def Length(self, seconds):
		"""(seconds, its videoFolder or None, its plan) for the page's choice; SessionError if it can't run."""
		if self.pico is None:
			return None, None, None
		lengths = self.Lengths()
		if seconds in (None, ""):
			seconds = lengths[0]["seconds"]
		try:
			seconds = int(seconds)
		except (TypeError, ValueError):
			raise sessionmod.SessionError("The session's length {!r} is not a number of seconds.".format(seconds))
		if seconds <= 0:
			raise sessionmod.SessionError("The session's length must be more than 0 s.")
		folder = next((l["videoFolder"] for l in lengths if l["seconds"] == seconds), None)
		try:
			return seconds, folder, self.pico.Plan(seconds)
		except picomod.PicoError as e:
			raise sessionmod.SessionError(str(e))

	def Args(self, seconds):
		"""campy-acquire's options for a session of seconds: recTimeInSec from the Pico's plan, and its videoFolder."""
		seconds, folder, plan = self.Length(seconds)
		if plan is None:
			return []
		args = ["--recTimeInSec", str(plan["camera_s"])]
		if folder:
			args += ["--videoFolder", folder]
		return args

	def Checks(self, seconds=None):
		"""[(level, text, action)]: "block" stops a start, "warn" only warns, "ok"."""
		checks = []
		planned = None
		if self.pico is not None:
			try:
				seconds, folder, plan = self.Length(seconds)
				planned = plan["ends_s"]
				checks.append(("ok", "The Pico's plan: {}; campy records {} s of it (recTimeInSec), into {}".format(
					plan["text"], plan["camera_s"], folder or self.config.Value("videoFolder")), None))
				if self.pico.page:
					checks.append(("ok", "Bout settings changed on this page (the config file is unchanged): {}".format(
						self.PageText()), None))
			except sessionmod.SessionError as e:
				checks.append(("block", str(e), None))
			try:
				checks.append(("ok", "The Pico answers, idle ({})".format(self.pico.Check()), None))
			except picomod.PicoError as e:
				checks.append(("block", str(e), None))
		if self.sglx is not None:
			try:
				checks.append(("ok", self.sglx.Check(), None))
			except sglxmod.SpikeGLXError as e:
				if self.sglx.section["exe"] and os.path.isfile(self.sglx.section["exe"]) and not self.sglx.Answers() and \
						not sglxmod.ProgramRunning(self.sglx.section["exe"]):
					checks.append(("ok", "SpikeGLX isn't open: it is opened at Start ({})".format(self.sglx.section["exe"]),
						None))
				elif sglxmod.ProgramRunning(self.sglx.section["exe"]) and not self.sglx.Answers():
					checks.append(("block", "SpikeGLX is open, but its command server doesn't answer: in SpikeGLX, Options > "
						"Command Server Settings > Enable; or close SpikeGLX, and it is opened at Start.", None))
				else:
					checks.append(("block", str(e), None))
		if self.windows is not None:
			if planned is None:
				try:
					planned = float(self.config.Value("recTimeInSec", 0))
				except (TypeError, ValueError):
					planned = 0
			checks += winsetup.Checks(self.windows, planned)
		return checks

	def PageText(self):
		# The page's bout settings, against the config's: "Blue LED on 50 ms (the config's 30 ms)"
		shown = {f["key"]: f for f in (self.pico.PageStatus() or {}).get("fields") or []}
		return "; ".join("{} {:g}{} (the config's {:g})".format(f["label"], f["value"], " " + f["unit"] if f["unit"] else "",
			f["config"]) for key, f in shown.items() if f["changed"])

	def Need(self, seconds, cameras, after_s=0.0):
		"""
		What a Pico's session of seconds writes to the disk after after_s of
		it, estimated from its plan: {spikeglx_b, videos_b, total_b, gate_s,
		camera_s}. SpikeGLX (with a spikeglx: section): the seconds its gate
		is open, plus SpikeGLX's trigger margin before and after each
		opening, at spikeglx.write_mb_s. The videos: the plan's camera
		seconds, for every camera, at panel.video_mb_s each (an estimate: a
		video's size depends on what it sees). None without a Pico.
		"""
		if self.pico is None:
			return None
		gates, trains = self.pico.Spans(seconds)
		after = float(after_s) if after_s else float("-inf") # the whole session: the first bout's margin too
		gate_s = 0.0
		if self.sglx is not None:
			margin = float(getattr(self.sglx, "margin", 1.0)) # trgTTLMarginS, once SpikeGLX's check has read it
			gate_s = sum(max(0.0, down + margin - max(up - margin, after)) for up, down in gates)
		camera_s = sum(max(0.0, end - max(start, after)) for start, end in trains)
		sglx_b = gate_s * float(self.sglx.section["write_mb_s"]) * 1e6 if self.sglx is not None else 0.0
		videos_b = camera_s * int(cameras) * float(self.config.panel["video_mb_s"]) * 1e6
		return {"spikeglx_b": sglx_b, "videos_b": videos_b, "total_b": sglx_b + videos_b, "gate_s": gate_s,
			"camera_s": camera_s}

	def NeedLeft(self, cameras):
		"""Need for the rest of the session that records, from where the Pico's session is; None once it has ended
		(or without a Pico)."""
		if self.pico is None or self.plan is None:
			return None
		if not self.pico_started:
			return self.Need(self.length, cameras)
		status = self.cached.get("pico") or {}
		if not status.get("alive"):
			return None
		return self.Need(self.length, cameras, status.get("session_s") or 0)

	# ---------------------------------------------------------- the session's steps (its main thread)

	def Step(self, name, state, text=""):
		with self.lock:
			for step in self.steps:
				if step["name"] == name:
					step.update(state=state, text=text, at=sessionmod.Stamp(time.time()))
					break
			else:
				self.steps.append({"name": name, "state": state, "text": text, "at": sessionmod.Stamp(time.time())})
		if text:
			self.say("{}: {}".format(name, text))

	def Plan(self, seconds, start_at=None):
		# The recording's length, chosen on the page (before the session starts)
		self.Reset()
		self.length, _, self.plan = self.Length(seconds)
		if self.pico is not None:
			# The bout settings as planned: the Pico starts with these, whatever the page says later
			self.sets = self.pico.Sets(self.length)
			self.settings = {"settings": self.pico.Settings(self.length), "from_page": sorted(self.pico.page)}
		start = self.pico.section["start"] if self.pico is not None else "ask"
		if start_at is None and start not in ("ask", ""):
			start_at = start
		if start_at:
			self.start_at = When(start_at)

	def Before(self, s):
		"""SpikeGLX's run, and the gate's check, before campy starts: SessionError if either fails."""
		self.folder = s.folder
		if self.sglx is not None:
			self.Step("SpikeGLX", "running", "opening, starting its run")
			try:
				self.sglx.Open()
				run = "{}{}".format(self.sglx.section["run_prefix"], os.path.basename(os.path.normpath(s.folder)))
				said = self.sglx.Start(os.path.join(s.folder, sglxmod.FOLDER), run)
			except sglxmod.SpikeGLXError as e:
				self.Step("SpikeGLX", "failed", str(e))
				raise sessionmod.SessionError("SpikeGLX's run did not start: {}".format(e))
			self.sglx_started = True
			self.Step("SpikeGLX", "done", said.replace("SpikeGLX: ", "", 1))
		if self.pico is not None and self.pico.section["gate_check"]:
			self.Step("Gate check", "running", "the gate blinks once; SpikeGLX must open a file for it")
			folder = os.path.join(s.folder, sglxmod.FOLDER)
			before = sglxmod.Files(folder)
			ok, said = self.pico.Blink(self.PicoFolder())
			t0 = time.monotonic()
			while ok and sglxmod.Files(folder) <= before and time.monotonic() - t0 < GATE_FILE_S:
				time.sleep(0.5)
			if not ok or sglxmod.Files(folder) <= before:
				text = ("the gate's blink didn't run: {}".format(said) if not ok else "the gate blinked, but SpikeGLX "
					"opened no file in {}. Check its trigger (TTL on the gate's line, follow high) and the gate's "
					"cable.".format(folder))
				self.Step("Gate check", "failed", text)
				self.StopSpikeGLX()
				raise sessionmod.SessionError("The gate check failed: " + text)
			self.Step("Gate check", "done", "SpikeGLX opened a file for the blink (its run's first file)")
		self.Step("Cameras", "running", "campy is starting them: each one waits for the Pico's pulses once it's ready")
		if self.pico is not None:
			self.Step("Pico", "waiting", "starts once every camera is ready{}".format(", at " + Clock(self.start_at)
				if self.start_at else ", when you press Start the Pico"))

	def PicoFolder(self):
		return os.path.join(self.folder, "pico")

	def Ready(self):
		"""(cameras ready, cameras), from the recording's session.json once campy has them all waiting; else None."""
		try:
			with open(os.path.join(self.folder, "session.json"), encoding="utf-8") as f:
				record = strictjson.load(f).get("recording")
		except (OSError, ValueError, AttributeError, TypeError):
			return None
		if not record:
			return None
		cams = record.get("cameras") or []
		return sum(1 for c in cams if c.get("ready")), len(cams)

	def Watch(self, s):
		"""Each moment while campy-acquire runs: the page's asks, the cameras getting ready, the Pico's start at its
		time, a stop after its bout."""
		if self.folder and self.cameras_ready is None or self.cameras_ready and self.cameras_ready[0] < self.cameras_ready[1]:
			ready = self.Ready() if self.folder else None
			if ready and ready != self.cameras_ready:
				self.cameras_ready = ready
				if ready[0] >= ready[1]:
					self.Step("Cameras", "done", "all {} cameras are ready".format(ready[1]))
				else:
					self.Step("Cameras", "running", "{} of {} ready".format(*ready))
		while self.wanted:
			with self.lock:
				ask = self.wanted.pop(0)
			if ask[0] == "start-pico":
				if ask[1]:
					self.start_at = ask[1]
					self.Step("Pico", "waiting", "starts at {}, once every camera is ready".format(Clock(ask[1])))
				else:
					self.StartPico(s)
			elif ask[0] == "stop":
				self.Stop(s, ask[1])
		if self.pico is None:
			return
		if self.start_at and not self.pico_started and time.time() >= self.start_at and not self.stop_mode:
			self.StartPico(s)
		if self.stop_campy_after_pico and not self.pico.Running():
			self.stop_campy_after_pico = False
			self.Step("Stop", "running", "the Pico's bout has ended: campy stops")
			s.RequestStop(1)

	def CanStartPico(self, s=None):
		# None, or why the Pico can't start now
		if self.pico is None:
			return "No Pico in this config."
		if self.pico_started:
			return "The Pico has started already."
		if self.stop_mode:
			return "Stopping."
		if self.folder is None or s is not None and s.state != "recording":
			return "campy isn't recording yet."
		ready = self.Ready()
		if ready is None:
			return "The cameras aren't ready yet."
		if ready[0] < ready[1]:
			return "Only {} of {} cameras are ready: stop, and start again.".format(*ready)
		return None

	def StartPico(self, s):
		why = self.CanStartPico(s)
		if why:
			if self.start_at and not self.pico_started:
				return # at its time, but not yet ready: next moment
			self.Step("Pico", "waiting", why)
			return
		self.start_at = None
		self.Step("Pico", "running", "starting its session: {}".format(self.plan["text"]))
		ok, said = self.pico.Start(self.length, self.PicoFolder(), self.sets)
		self.pico_said = said
		if not ok:
			self.Step("Pico", "failed", "its session didn't start: {}".format(said))
			s.Problem("The Pico's session didn't start: {}. campy and SpikeGLX are still recording: Start the Pico again, "
				"or Stop.".format(said))
			return
		self.pico_started = time.time()
		self.Step("Pico", "done", said.splitlines()[-1] if said else "started")

	def Stop(self, s, mode):
		"""The page's Stop (asked first on the page): mode "bout" (after the Pico's bout) or "now"."""
		if self.stop_mode == "now" or (self.stop_mode == "bout" and mode == "bout"):
			return
		self.stop_mode = mode
		self.start_at = None
		running = self.pico is not None and self.pico_started and self.pico.Running()
		if running and mode == "bout":
			ok, said = self.pico.Stop(now=False)
			self.Step("Stop", "running", "after this bout: {}".format(said if ok else "the Pico didn't answer ({}); "
				"stopping it now".format(said)))
			if ok:
				self.stop_campy_after_pico = True
				return
			mode = self.stop_mode = "now"
		if running:
			ok, said = self.pico.Stop(now=True)
			self.Step("Stop", "running", "the Pico stopped: {}".format(said) if ok else "the Pico didn't stop ({})".format(
				said))
		else:
			self.Step("Stop", "running", "campy stops")
		self.stop_campy_after_pico = False
		s.RequestStop(1)

	def After(self, s, code):
		"""Once campy-acquire has ended (exit code): the Pico's last gate waited for, then SpikeGLX's run stopped."""
		if self.pico is not None and self.pico_started:
			status = self.pico.Running()
			if status and code != 0 and not self.stop_mode:
				# campy failed on its own: the Pico's session (an animal's LED schedule) goes on, and SpikeGLX records it
				self.left_running = ("campy ended with exit code {} on its own, but the Pico's session is still running "
					"(gate, camera pulses, LEDs), and SpikeGLX's run with it, so its bouts are still recorded. Stop them "
					"on this page (Stop the Pico, then Stop SpikeGLX's run), or start a new recording to record the rest."
					.format(code))
				self.Step("End", "failed", self.left_running)
				s.Problem(self.left_running)
				self.pico.CopyLog(self.PicoFolder())
				return
			if status:
				self.Step("End", "running", "waiting for the Pico's last gate (at most {:g} s)".format(
					self.pico.section["last_gate_s"]))
				deadline = time.monotonic() + self.pico.section["last_gate_s"]
				while self.pico.Running() and time.monotonic() < deadline:
					time.sleep(1.0)
				if self.pico.Running():
					ok, said = self.pico.Stop(now=True)
					s.Problem("The Pico's session was still running {:g} s after campy's last frame: stopped ({}).".format(
						self.pico.section["last_gate_s"], said))
			self.pico.CopyLog(self.PicoFolder())
		elif self.pico is not None and self.start_at:
			self.Step("Pico", "skipped", "campy ended before the Pico's start")
		if self.sglx_started:
			time.sleep(3.0) # SpikeGLX closes a file its margin (1 s) after the gate falls
			self.StopSpikeGLX()
		if self.stop_mode:
			self.Step("Stop", "done", "stopped {}".format("after the Pico's bout" if self.stop_mode == "bout" else "now"))
		self.cached_at = 0.0
		self.Step("End", "done", "everything stopped" if code == 0 else "stopped (campy's exit code {})".format(code))

	def StopSpikeGLX(self):
		self.cached_at = 0.0 # the page's next status reads SpikeGLX again
		try:
			said = self.sglx.Stop()
			self.sglx_started = False
			self.Step("SpikeGLX", "done", said.replace("SpikeGLX: ", "", 1))
			return None
		except sglxmod.SpikeGLXError as e:
			self.Step("SpikeGLX", "failed", "its run didn't stop: {}".format(e))
			return str(e)

	# ---------------------------------------------------------- the page

	def Ask(self, *ask):
		with self.lock:
			self.wanted.append(ask)

	def StopLeft(self, what):
		"""The page's buttons when nothing records: stop the Pico (now), or SpikeGLX's run. An error text, or None."""
		self.cached_at = 0.0
		if what == "pico":
			if self.pico is None:
				return "No Pico in this config."
			if not self.pico.Running():
				return "The Pico isn't running a session."
			ok, said = self.pico.Stop(now=True)
			self.say("The Pico stopped from the page: {}".format(said))
			if self.folder and ok:
				self.pico.CopyLog(self.PicoFolder())
			return None if ok else said
		if what == "spikeglx":
			if self.sglx is None:
				return "No SpikeGLX in this config."
			if self.pico is not None and self.pico.Running():
				return "The Pico is still running a session: stop it first, or SpikeGLX would miss its bouts."
			try:
				self.say(self.sglx.Stop())
			except sglxmod.SpikeGLXError as e:
				return str(e)
			self.sglx_started = False
			return None
		return "Nothing called {}.".format(what)

	def Status(self):
		"""What the page shows of the devices (the Pico's and SpikeGLX's state read at most every 2 s)."""
		if not self.Enabled():
			return None
		now = time.monotonic()
		if now - self.cached_at >= STATUS_EVERY_S:
			self.cached_at = now
			cached = {}
			if self.pico is not None:
				status = self.pico.Status()
				cached["pico"] = None if status is None else {k: status.get(k) for k in ("state", "alive", "mode",
					"session_s", "bout", "bouts", "ends_s", "pulses", "done", "error", "end_requested", "started", "log")}
			if self.sglx is not None:
				status = self.sglx.Status() or dict(self.cached.get("spikeglx") or {"answers": True}, busy=True)
				if self.folder:
					status["files"] = sglxmod.Files(os.path.join(self.folder, sglxmod.FOLDER))
					status["openings"] = sglxmod.Openings(os.path.join(self.folder, sglxmod.FOLDER))
				cached["spikeglx"] = status
			self.cached = cached
		pico = dict(self.cached.get("pico") or {}) if self.pico is not None else None
		if pico is not None:
			if pico.get("started") and pico.get("ends_s") is not None:
				pico["ends_at"] = Clock(pico["started"] + pico["ends_s"])
			pico["port"] = self.pico.section["port"] or "the one Pico"
		with self.lock:
			steps = [dict(step) for step in self.steps]
		return {"pico": pico, "spikeglx": self.cached.get("spikeglx") if self.sglx is not None else None,
			"steps": steps, "length": self.length, "plan": self.plan, "settings": self.settings,
			"start_at": Clock(self.start_at) if self.start_at else None,
			"pico_started": sessionmod.Stamp(self.pico_started) if self.pico_started else None,
			"can_start_pico": self.CanStartPico() if self.pico is not None and self.folder else "Not recording.",
			"cameras_ready": list(self.cameras_ready) if self.cameras_ready else None,
			"stop_mode": self.stop_mode, "left_running": self.left_running}


def When(text):
	"""A clock time ("7:20 PM", "19:20") today, or tomorrow if it has passed; or a time.time() number as it is."""
	if isinstance(text, (int, float)):
		return float(text)
	text = str(text).strip().upper().replace(".", "")
	for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
		try:
			t = datetime.strptime(text, fmt)
			break
		except ValueError:
			continue
	else:
		raise sessionmod.SessionError("{!r} is not a clock time (7:20 PM).".format(text))
	now = datetime.now()
	at = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
	if at <= now:
		from datetime import timedelta
		at += timedelta(days=1)
	return at.timestamp()


def Clock(t):
	"""time.time() as the page shows it: "Thu 7:20 PM"."""
	return datetime.fromtimestamp(t).strftime("%a %I:%M %p").replace(" 0", " ")
