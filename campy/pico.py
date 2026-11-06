"""
The ChronicRecord Pico, for campy-session (the config's pico: section).

A Raspberry Pi Pico on the recording computer's USB makes the rig's TTL
outputs: SpikeGLX's record gate, the cameras' trigger and two LED drivers.
chronicrecord/chronic_control.py plans a session and loads it onto the Pico,
which then runs it from its own clock (chronicrecord/README.md). This module
runs chronic_control.py for campy-session and its panel:

- the plan for a session length: its bouts, its camera pulses (campy's
  recTimeInSec is the pulses over the frame rate, so the two can't differ),
  and when its last gate falls;
- the check that the Pico answers and is idle (its port, by USB serial
  number);
- the gate blink before a recording (SpikeGLX must open a file for it);
- start, stop (after the current bout, or now) and the session's status,
  read from chronic_control.py's status.json.

chronic_control.py itself is run as a program (its start leaves the session
program running in the background, without a window), with this
environment's python: it needs only pyserial. Its plan is read by importing
it, which needs nothing.
"""

import importlib.util, os, subprocess, sys, time

# The pico: section's keys: (the kinds it takes, its default, what it is)
KEYS = {
	"port": ((str,), "", "the Pico's port, or its USB serial number (as chronic_control.py --port); default: the one Pico"),
	"control": ((str,), "", "chronic_control.py (default: campy's own, chronicrecord/chronic_control.py)"),
	"state_dir": ((str,), "", "chronic_control.py --state-dir: its status.json and session.out (default: its own, "
		"~/.chronic_control)"),
	"lengths": ((list,), [], "the session lengths the panel offers: [{name: \"6 days\", seconds: 518400}, {name: "
		"\"Gate test\", seconds: 900, videoFolder: \"C:\\\\data\\\\gatetest\"}]; the first is the default"),
	"set": ((dict,), {}, "chronic_control.py's other settings for every session: {INTERVAL: 300}"),
	"gate_check": ((bool,), True, "blink the gate once before campy starts, and refuse to start if SpikeGLX opens no "
		"file for it (needs a spikeglx: section)"),
	"last_gate_s": ((int, float), 120, "once campy has its last frame, how long to wait for the Pico's last gate "
		"(10 s after the last camera pulse in the default plan) before stopping it"),
	"start": ((str,), "ask", "ask: the page's \"Start the Pico\" button, once every camera is ready; or a clock time, "
		"\"7:20 PM\", for the panel's default"),
	}


class PicoError(Exception):
	pass


def CampyControl():
	# campy's own copy: <checkout>/chronicrecord/chronic_control.py
	import campy
	return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(campy.__file__))), "chronicrecord",
		"chronic_control.py")


_modules = {}


def Module(path):
	"""chronic_control.py, imported from its file (it is a program, not part of the campy package)."""
	path = os.path.abspath(path)
	if path not in _modules:
		if not os.path.isfile(path):
			raise PicoError("chronic_control.py not found: {} (pico.control in the config)".format(path))
		spec = importlib.util.spec_from_file_location("chronic_control_{}".format(len(_modules)), path)
		module = importlib.util.module_from_spec(spec)
		try:
			spec.loader.exec_module(module)
		except Exception as e:
			raise PicoError("Could not read {}: {}".format(path, e))
		_modules[path] = module
	return _modules[path]


def Clock(seconds):
	"""A length as days, hours and minutes: "6 days", "1 h 30 min", "15 min"."""
	s = int(round(seconds))
	d, h, m = s // 86400, s // 3600 % 24, s // 60 % 60
	parts = (["{} day{}".format(d, "" if d == 1 else "s")] if d else []) + (["{} h".format(h)] if h else []) + \
		(["{} min".format(m)] if m else []) + (["{} s".format(s % 60)] if s % 60 and not d else [])
	return " ".join(parts) or "0 s"


class Pico:
	"""The pico: section, checked (its keys filled in), and what campy-session asks of the Pico."""

	def __init__(self, section, python=None, fps=None):
		self.section = section
		self.control = section["control"] or CampyControl()
		self.python = python or sys.executable
		self.fps = fps
		self.module = Module(self.control)
		self.spans = {} # Spans' answers, by length
		self.state_dir = section["state_dir"] or self.module.STATE_DIR
		self.lengths = []
		for n, length in enumerate(section["lengths"]):
			if not isinstance(length, dict) or not isinstance(length.get("seconds"), int) or length["seconds"] <= 0 or \
					set(length) - {"name", "seconds", "videoFolder"}:
				raise PicoError("pico.lengths: each is {{name: ..., seconds: <a whole number>, videoFolder: <optional>}}; "
					"number {} is {!r}.".format(n + 1, length))
			self.lengths.append({"name": str(length.get("name") or Clock(length["seconds"])),
				"seconds": length["seconds"], "videoFolder": length.get("videoFolder")})
		for key, value in section["set"].items():
			if key not in self.module.SETTINGS or key == "TOTAL_DURATION":
				raise PicoError("pico.set: {} is not one of chronic_control.py's settings{} ({}).".format(key,
					" here (the session's length comes from pico.lengths, or the page)" if key == "TOTAL_DURATION" else "",
					", ".join(k for k in self.module.SETTINGS if k != "TOTAL_DURATION")))
		if self.fps is not None and float(self.Settings(None)["CAMERA_FPS"]) != float(self.fps):
			raise PicoError("The Pico's camera pulses are {} a second (CAMERA_FPS), but campy's frameRate is {}: make them "
				"the same (pico.set: {{CAMERA_FPS: ...}}).".format(self.Settings(None)["CAMERA_FPS"], self.fps))

	# ---------------------------------------------------------- the plan

	def Sets(self, seconds):
		# chronic_control.py's --set NAME=VALUE for a session of seconds
		sets = ["{}={}".format(k, v) for k, v in self.section["set"].items()]
		return sets + (["TOTAL_DURATION={}".format(int(seconds))] if seconds is not None else [])

	def Settings(self, seconds):
		try:
			return self.module.Settings(self.Sets(seconds))
		except SystemExit as e:
			raise PicoError(str(e))

	def Plan(self, seconds):
		"""
		{seconds, bouts, pulses, camera_s, ends_s, text}: the session of
		that length; camera_s is campy's recTimeInSec for it.
		"""
		s = self.Settings(seconds)
		try:
			bouts, trains, edges = self.module.Plan(s)
		except SystemExit as e:
			raise PicoError("The Pico can't run {}: {}".format(Clock(seconds), e))
		pulses = sum(t[2] for t in trains)
		fps = float(s["CAMERA_FPS"])
		camera_s = pulses / fps
		if int(round(camera_s * fps)) != pulses:
			raise PicoError("{} camera pulses at {:g} a second: campy's recTimeInSec can't match it.".format(pulses, fps))
		camera_s = int(camera_s) if camera_s == int(camera_s) else camera_s
		ends = bouts[-1]["gate_off"] / 1e6 if bouts else seconds
		return {"seconds": seconds, "bouts": len(bouts), "pulses": pulses, "camera_s": camera_s, "ends_s": ends,
			"text": "{} bouts over {}, {:,} camera pulses ({} of camera)".format(len(bouts), Clock(seconds), pulses,
				Clock(camera_s))}

	def Spans(self, seconds):
		"""
		([(gate up, gate down)], [(camera's first pulse, its end)]) in the
		session's seconds: when the gate is open (SpikeGLX writes) and when
		the cameras take frames (each train's pulses over CAMERA_FPS), for the
		panel's disk estimate. A camera-only session's gate never opens.
		"""
		key = int(seconds)
		if key not in self.spans:
			s = self.Settings(seconds)
			try:
				_, trains, edges = self.module.Plan(s)
			except SystemExit as e:
				raise PicoError("The Pico can't run {}: {}".format(Clock(seconds), e))
			gate, fps = getattr(self.module, "GATE", 0), float(s["CAMERA_FPS"])
			ups = [t / 1e6 for t, channel, level in edges if channel == gate and level]
			downs = [t / 1e6 for t, channel, level in edges if channel == gate and not level]
			self.spans[key] = (list(zip(ups, downs)), [(t[1] / 1e6, t[1] / 1e6 + t[2] / fps) for t in trains])
		return self.spans[key]

	# ---------------------------------------------------------- the board

	def Port(self):
		try:
			return self.module.FindPort(self.section["port"] or None)
		except SystemExit as e:
			raise PicoError(str(e))

	def Check(self):
		"""The Pico answers and isn't running a session: its status line. PicoError otherwise."""
		running = self.Running()
		if running:
			raise PicoError("The Pico is running a session already (pid {}, {}): stop it first.".format(running["pid"],
				running.get("mode") or "bouts"))
		port = self.Port()
		try:
			board = self.module.Board(port, lambda line: None)
		except Exception as e:
			raise PicoError("The Pico ({}) doesn't answer: {}".format(port, e))
		try:
			status = board.Check()
		except Exception as e:
			raise PicoError("The Pico ({}) doesn't answer: {}".format(port, e))
		finally:
			board.close()
		if "running" in status:
			raise PicoError("The Pico ({}) is running something already ({}): 'stop --now' it first.".format(port, status))
		return "{}: {}".format(port, status)

	def Command(self, words, log_dir=None, timeout=300):
		# chronic_control.py <words> --port ... --state-dir ...: (exit code, its output)
		command = [self.python, self.control] + list(words) + ["--state-dir", self.state_dir]
		if self.section["port"]:
			command += ["--port", self.section["port"]]
		if log_dir:
			command += ["--log-dir", log_dir]
		try:
			out = subprocess.run(command, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
		except (OSError, subprocess.TimeoutExpired) as e:
			return 1, "chronic_control.py {}: {}".format(words[0], e)
		return out.returncode, (out.stdout + out.stderr).strip()

	def Start(self, seconds, log_dir):
		"""The session, on the board: (ok, what chronic_control.py said)."""
		try:
			self.out_from = os.path.getsize(os.path.join(self.state_dir, "session.out"))
		except OSError:
			self.out_from = 0
		code, said = self.Command(["start"] + sum((["--set", s] for s in self.Sets(seconds)), []), log_dir)
		return code == 0, said

	def Blink(self, log_dir, high_s=4.0):
		"""The gate (A) high once, about half a second, then the wiring check stopped: (ok, said)."""
		code, said = self.Command(["wiring", "--channels", "A", "--pause", "30"], log_dir)
		if code != 0:
			return False, said
		time.sleep(high_s)
		code2, said2 = self.Command(["stop"])
		return code2 == 0, said + "\n" + said2

	def Stop(self, now):
		"""stop (after the current bout) or stop --now: (ok, said)."""
		code, said = self.Command(["stop"] + (["--now"] if now else []), timeout=60)
		return code == 0, said

	def Status(self):
		"""chronic_control.py's status.json, with alive: its program is running; None if there is none."""
		status = self.module.ReadStatus(self.state_dir)
		if status is None:
			return None
		status["alive"] = status.get("state") in ("starting", "running") and self.module.Alive(status.get("pid"))
		return status

	def Running(self):
		status = self.Status()
		return status if status and status["alive"] else None

	def CopyLog(self, folder):
		# chronic_control.py's own output since this session's start (session.out keeps every session's), beside the
		# session's timestamps.txt
		try:
			os.makedirs(folder, exist_ok=True)
			with open(os.path.join(self.state_dir, "session.out"), "rb") as f:
				f.seek(getattr(self, "out_from", 0))
				data = f.read()
			with open(os.path.join(folder, "session.out"), "wb") as f:
				f.write(data)
			return True
		except OSError:
			return False
