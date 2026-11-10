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

import importlib.util, math, os, subprocess, sys, time
from datetime import datetime

# The bout settings the panel's page can change, without changing the config (otherwise pico.set, then
# chronic_control.py's own defaults): (setting, the page's label, the page's unit, the setting's value times this is
# the page's). The others (CAMERA_FPS, which must be campy's frameRate; the camera pulse; the LEDs' lead) stay in pico.set.
PAGE = [
	("INTERVAL", "A bout every", "s", 1),
	("TOTAL_BASELINE", "Baseline: gate open to the LEDs, and camera off to gate closed", "s", 1),
	("CAMERA_LEAD_IN", "Camera on before the LEDs", "s", 1),
	("STANDARD_STIM_DURATION", "Camera time, normal bout", "s", 1),
	("EXTENDED_STIM_DURATION", "Camera time, long bout", "s", 1),
	("EXTENDED_EVERY", "A long bout every (bouts; the first is long)", "", 1),
	("PULSE_WIDTH_C", "Blue LED on", "ms", 1000),
	("PULSE_WIDTH_D", "Yellow LED on", "ms", 1000),
	("YELLOW_FIRST", "Yellow LED first (1), or blue first (0)", "", 1),
	("LED_GAP", "Dark between the two LEDs", "ms", 1000),
	]
# The page's settings that can be 0 (the others must be more than 0), and those that are whole numbers
ZERO_OK = {"YELLOW_FIRST", "LED_GAP"}
WHOLE = {"EXTENDED_EVERY", "YELLOW_FIRST"}
# The Pico's room for a session (chronicrecord/chronic_timer/chronic_timer.ino: MAX_TRAINS, one camera train a bout, and
# MAX_EDGES, 6 a bout): a session with more bouts is refused at its start, so the plan refuses it first
MAX_BOUTS = 2048
# The page's last bout settings for each config, which the next panel opened with it starts from
REMEMBER = "panel-bouts.json"

# The pico: section's keys: (the kinds it takes, its default, what it is)
KEYS = {
	"port": ((str,), "", "the Pico's port, or its USB serial number (as chronic_control.py --port); default: the one Pico"),
	"control": ((str,), "", "chronic_control.py (default: campy's own, chronicrecord/chronic_control.py)"),
	"state_dir": ((str,), "", "chronic_control.py --state-dir: its status.json and session.out (default: its own, "
		"~/.chronic_control)"),
	"lengths": ((list,), [], "the session lengths the panel offers: [{name: \"6 days\", seconds: 518400}, {name: "
		"\"Gate test\", seconds: 900, videoFolder: \"C:\\\\data\\\\gatetest\"}]; the first is the default. copyTo "
		"(optional): the folder the panel's \"Copy each recording to\" box fills in when that length is picked"),
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


def SetText(value):
	# A setting as chronic_control.py's --set reads it: a whole number without a point (it reads one with a point as a
	# float), and a fraction to the microsecond, never in exponent form
	if isinstance(value, float) and math.isfinite(value):
		if value == int(value):
			return str(int(value))
		return "{:.6f}".format(value).rstrip("0")
	return str(value)


def Room(s, seconds):
	# The Pico's room: a session of more bouts than it holds is refused (before planning each of them)
	if not s["CAMERA_ONLY"] and s["INTERVAL"] > 0 and s["TOTAL_DURATION"] // s["INTERVAL"] > MAX_BOUTS:
		raise PicoError("The Pico can't run {}: {:,.0f} bouts, one every {:g} s, and it holds {:,} (7.1 days at one every "
			"300 s). Make the bouts further apart, or the session shorter.".format(Clock(seconds), s["TOTAL_DURATION"] //
			s["INTERVAL"], s["INTERVAL"], MAX_BOUTS))


def RememberFile():
	from campy import session
	return os.path.join(session.StatusDir(), REMEMBER)


def ConfigKey(path):
	return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


def Remembered(path, config):
	"""The page's last bout settings with this config ({page, at}), or None."""
	from campy import strictjson
	try:
		with open(path) as f:
			got = strictjson.load(f).get(ConfigKey(config))
		return got if isinstance(got, dict) else None
	except (OSError, ValueError, AttributeError):
		return None


def Remember(path, config, page):
	"""This config's page settings, for the next panel opened with it (none: the config's)."""
	from campy import session, strictjson
	try:
		with open(path) as f:
			everything = strictjson.load(f)
		if not isinstance(everything, dict):
			everything = {}
	except (OSError, ValueError):
		everything = {}
	if page:
		everything[ConfigKey(config)] = {"page": dict(page), "at": datetime.now().astimezone().isoformat(
			timespec="seconds")}
	else:
		everything.pop(ConfigKey(config), None)
	try:
		os.makedirs(os.path.dirname(path), exist_ok=True)
		session.WriteJson(path, everything)
	except OSError:
		pass


def Shown(at):
	# An ISO time as the page says it: "Mon 9 Nov, 3:12 PM"
	if not at:
		return None
	try:
		t = datetime.fromisoformat(str(at))
	except ValueError:
		return str(at)
	return "{} {} {}, {}".format(t.strftime("%a"), t.day, t.strftime("%b"), t.strftime("%I:%M %p").lstrip("0"))


class Pico:
	"""The pico: section, checked (its keys filled in), and what campy-session asks of the Pico."""

	def __init__(self, section, python=None, fps=None):
		self.section = section
		self.control = section["control"] or CampyControl()
		self.python = python or sys.executable
		self.fps = fps
		self.module = Module(self.control)
		self.spans = {} # Spans' answers, by length
		self.page = {} # the page's bout settings ({setting: its value}), over pico.set: Choose
		self.remembered = None # when the page's settings were last set, if they come from an earlier panel: Restore
		self.state_dir = section["state_dir"] or self.module.STATE_DIR
		self.lengths = []
		for n, length in enumerate(section["lengths"]):
			if not isinstance(length, dict) or not isinstance(length.get("seconds"), int) or length["seconds"] <= 0 or \
					set(length) - {"name", "seconds", "videoFolder", "copyTo"}:
				raise PicoError("pico.lengths: each is {{name: ..., seconds: <a whole number>, videoFolder: <optional>, "
					"copyTo: <optional>}}; "
					"number {} is {!r}.".format(n + 1, length))
			self.lengths.append({"name": str(length.get("name") or Clock(length["seconds"])),
				"seconds": length["seconds"], "videoFolder": length.get("videoFolder"),
				"copyTo": str(length["copyTo"]).strip() if length.get("copyTo") else None})
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
		# chronic_control.py's --set NAME=VALUE for a session of seconds: pico.set, with the page's over it
		values = dict(self.section["set"], **self.page)
		sets = ["{}={}".format(k, SetText(v)) for k, v in values.items()]
		return sets + (["TOTAL_DURATION={}".format(int(seconds))] if seconds is not None else [])

	def Settings(self, seconds):
		try:
			return self.module.Settings(self.Sets(seconds))
		except SystemExit as e:
			raise PicoError(str(e))

	def Base(self):
		# The bout settings without the page's: pico.set, then chronic_control.py's own
		try:
			return self.module.Settings(["{}={}".format(k, SetText(v)) for k, v in self.section["set"].items()])
		except SystemExit as e:
			raise PicoError(str(e))

	def Choose(self, values):
		"""
		The page's bout settings, for this panel's recordings: {setting: its
		value in the page's unit (PAGE), or None for the config's}. A value
		equal to the config's is the config's. An error text (nothing
		changed), or None. A plan they make impossible isn't refused here:
		each length's plan says why, and stops the start.
		"""
		pages = {key: (label, unit, scale) for key, label, unit, scale in PAGE}
		base = self.Base()
		page = dict(self.page)
		for key, value in values.items():
			if key not in pages:
				return "{} is not a bout setting the page can change ({}).".format(key, ", ".join(pages))
			label, unit, scale = pages[key]
			if value is None or str(value).strip() == "":
				page.pop(key, None)
				continue
			try:
				number = float(value)
			except (TypeError, ValueError):
				return "{}: {!r} is not a number.".format(label, value)
			if not math.isfinite(number) or number < 0 or (number == 0 and key not in ZERO_OK):
				return "{}: it must be more than 0.".format(label)
			if key == "YELLOW_FIRST" and number not in (0, 1):
				return "{}: 1 or 0.".format(label)
			if key in WHOLE:
				if number != int(number):
					return "{}: a whole number of bouts.".format(label)
				number = int(number)
			else:
				number = round(number / scale, 6) # to the microsecond
				if number <= 0 and key not in ZERO_OK:
					return "{}: it must be at least a microsecond.".format(label)
			if number == base[key]:
				page.pop(key, None)
			else:
				page[key] = number
		self.page = page
		self.spans = {}
		self.remembered = None
		return None

	def Restore(self, last):
		"""
		The page's settings as the last panel with this config left them
		({page: {setting: value}, at}, Remembered): those a bout setting can
		still be, over the config's. One the config now has itself is the
		config's; any other is left out.
		"""
		base = self.Base()
		keys = {key for key, _, _, _ in PAGE}
		page = {}
		given = (last or {}).get("page")
		for key, value in (given.items() if isinstance(given, dict) else ()):
			if key not in keys or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
					or value < 0 or value == 0 and key not in ZERO_OK or key in WHOLE and value != int(value) \
					or key == "YELLOW_FIRST" and value not in (0, 1):
				continue
			value = int(value) if key in WHOLE else round(float(value), 6)
			if value != base[key]:
				page[key] = value
		self.page, self.spans = page, {}
		self.remembered = (last or {}).get("at") if page else None

	def PageStatus(self):
		"""The page's bout settings box: each setting (in the page's unit, with the config's), and what one bout does."""
		base = self.Base()
		if base["CAMERA_ONLY"]:
			return None # camera pulses only: no bouts
		try:
			now = self.Settings(None)
		except PicoError:
			now = dict(base, **self.page)
		shown = lambda v, scale: round(v * scale, 6)
		return {"fields": [{"key": key, "label": label, "unit": unit, "value": shown(now[key], scale),
			"config": shown(base[key], scale), "changed": key in self.page} for key, label, unit, scale in PAGE],
			"changed": sorted(self.page), "bout": self.Bout(), "remembered": Shown(self.remembered),
			"diagram": self.Diagram()}

	def Bout(self):
		"""
		What one bout does, from the gate's opening, as the page says it: {text,
		warnings} (an LED switching during a frame's exposure), or {error}.
		"""
		try:
			s = self.Settings(None)
			two = 2 if s["EXTENDED_EVERY"] > 1 else 1 # a long bout, then a normal one
			bouts, _, _ = self.module.Plan(dict(s, TOTAL_DURATION=s["INTERVAL"] * two))
		except (PicoError, SystemExit) as e:
			return {"error": "These bout settings can't run: {}".format(e)}
		fps = float(s["CAMERA_FPS"])
		at = lambda b, us: "{:g} s".format(round((us - b["gate_on"]) / 1e6, 3))
		lit = lambda b, colour: self.module.LitFrames(b, b[colour][0], b[colour][1], s)
		first = bouts[0]
		blue, blue_part = lit(first, "blue")
		yellow, yellow_part = lit(first, "yellow")
		ends = lambda b: (at(b, b["camera_start"] + b["pulses"] / fps * 1e6), at(b, b["gate_off"]))
		# The LEDs in the order they light: (name, its on and off, its lit frames)
		one, two = sorted((("blue", first["blue"], blue), ("yellow", first["yellow"], yellow)), key=lambda led: led[1][0])
		ms = lambda led: round((led[1][1] - led[1][0]) / 1000, 3)
		gap = round((two[1][0] - one[1][1]) / 1000, 3)
		text = ("From the gate's opening: camera on at {}; {} LED at {} for {:g} ms (frames {}), then {}{} for {:g} ms "
			"(frames {}); camera off at {} and gate closed at {} in a long bout{}. The next bout starts {:g} s after the "
			"gate opened.".format(at(first, first["camera_start"]), one[0], at(first, one[1][0]), ms(one),
			self.module.Frames(one[2]), "{:g} ms dark, then ".format(gap) if gap else "", two[0], ms(two),
			self.module.Frames(two[2]), *ends(first), ", at {} and {} in a normal one".format(*ends(bouts[1]))
			if len(bouts) > 1 else " (every bout is long)", s["INTERVAL"]))
		warnings = ["the {} LED switches during the exposure of frame(s) {}".format(name, self.module.Frames(part))
			for name, part in (("blue", blue_part), ("yellow", yellow_part)) if part]
		return {"text": text, "warnings": warnings}

	def Plan(self, seconds):
		"""
		{seconds, bouts, pulses, camera_s, ends_s, text}: the session of
		that length; camera_s is campy's recTimeInSec for it.
		"""
		s = self.Settings(seconds)
		Room(s, seconds)
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

	def Diagram(self):
		"""
		The page's drawing of a bout, from the settings as they are now (the
		page's too): {rows: [{pin, name}] (the Pico's outputs, in order: the
		gate, the camera, the blue and the yellow LED), fps, pulse_s,
		interval_s, bouts}, bouts a long one and, when the long ones aren't
		every bout, a normal one: {long, gate: [opens, closes], camera:
		[first pulse, last pulse's end], pulses, leds: [{name, on, off}] in
		the order they light}, in seconds from the gate's opening (the
		Pico's own plan, as Bout's). None when these settings can't run.
		"""
		try:
			s = self.Settings(None)
			if s["CAMERA_ONLY"]:
				return None
			two = 2 if s["EXTENDED_EVERY"] > 1 else 1 # a long bout, then a normal one
			bouts, _, _ = self.module.Plan(dict(s, TOTAL_DURATION=s["INTERVAL"] * two))
		except (PicoError, SystemExit):
			return None
		sec = lambda b, us: round((us - b["gate_on"]) / 1e6, 6)
		shown = []
		for b in bouts[:2]:
			leds = [{"name": name, "on": sec(b, b[name][0]), "off": sec(b, b[name][1])} for name in ("blue", "yellow")
				if b.get(name)]
			shown.append({"long": bool(b["extended"]), "gate": [0.0, sec(b, b["gate_off"])],
				"camera": [sec(b, b["camera_start"]), sec(b, b["camera_end"])], "pulses": b["pulses"],
				"leds": sorted(leds, key=lambda l: l["on"])})
		return {"rows": [{"pin": "GP{}".format(pin), "name": name} for _, pin, name in getattr(self.module, "OUTPUTS", [])],
			"fps": float(s["CAMERA_FPS"]), "pulse_s": s["CAMERA_PULSE_US"] / 1e6, "interval_s": float(s["INTERVAL"]),
			"bouts": shown}

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
			Room(s, seconds)
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

	def Start(self, seconds, log_dir, sets=None):
		"""The session, on the board (sets: Sets(seconds) as the recording planned it): (ok, what chronic_control.py
		said)."""
		try:
			self.out_from = os.path.getsize(os.path.join(self.state_dir, "session.out"))
		except OSError:
			self.out_from = 0
		code, said = self.Command(["start"] + sum((["--set", s] for s in (sets or self.Sets(seconds))), []), log_dir)
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
