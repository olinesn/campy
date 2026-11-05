#!/usr/bin/env python3
"""
chronic_control.py: ChronicRecord's stimulus sessions, made by the Raspberry
Pi Pico (chronic_timer/chronic_timer.ino) and run from the computer it is
plugged into: the Raspberry Pi 3B, or the recording PC itself (Windows or Linux).

This program plans the whole session (the same bouts as chronicControlv13.py) and
loads it onto the Pico over USB. The Pico then makes every TTL from its own
crystal clock: the OneBox record gate, the 60 Hz camera trigger and the two
LED controllers, each edge on its microsecond, the LEDs on whole camera frames.
The computer only starts it, logs what the Pico reports, and stops it. (An Arduino
Nano ESP32 with the same sketch works too.)

Over SSH (each returns at once; the session runs in the background):
  python3 chronic_control.py start        start a session
  python3 chronic_control.py stop         end it after the current bout (on time)
  python3 chronic_control.py stop --now   end it now: the camera finishes its
                                          pulse, then everything goes low
  python3 chronic_control.py status       what it is doing
  python3 chronic_control.py plan         print the session's timeline (no board)
  python3 chronic_control.py run          the session in this terminal (start
                                          runs this); Ctrl-C ends it now
  python3 chronic_control.py wiring       a wiring check, in the background until
                                          'stop': each output blinks on its own, its
                                          letter's number of times (A gate 1, B camera
                                          2, C blue 3, D yellow 4), then a pause.
                                          --channels AB checks only those outputs;
                                          --pause 60 waits a minute between rounds.
  python3 chronic_control.py blink        every output on and off together, 2 Hz
                                          (--hz), in the background until 'stop'
  python3 chronic_control.py attach       take over a session the board is still
                                          running after this program stopped (a
                                          restart of this computer, with the Pico on
                                          its own power supply): logs it to the end
Change a setting for one session without editing this file:
  python3 chronic_control.py start --set TOTAL_DURATION=1800 --set INTERVAL=180

Needs pyserial (sudo apt install python3-serial) and the user in the dialout
group; no sudo, no GPIO on the Pi.

On Windows: the same commands with "python" for "python3". It finds the Pico by
its USB IDs (never an Arduino there: campy's trigger Arduino may be on the same
computer); --port COM7 or --port <the Pico's USB serial number> picks one. Start
sessions from a window on the PC's desktop (Remote Desktop is fine): Windows' SSH
server ends what a session started when it disconnects. status and stop work
over SSH.
"""

import argparse, glob, json, math, os, re, signal, subprocess, sys, time
from datetime import datetime

# --- Timing settings (USER EDITABLE), as in chronicControlv13.py ---
SETTINGS = dict(
	TOTAL_BASELINE=10,            # baseline before and after the camera (seconds)
	CAMERA_LEAD_IN=5,             # camera runs this long before the LEDs (seconds)
	STANDARD_STIM_DURATION=10,    # camera time of a standard bout (seconds)
	EXTENDED_STIM_DURATION=240,   # camera time of an extended bout (seconds)
	EXTENDED_EVERY=6,             # bouts 1, 7, 13, ... are extended
	PULSE_WIDTH_C=0.030,          # Blue LED (Output C) on time (seconds)
	PULSE_WIDTH_D=0.030,          # Yellow LED (Output D) on time (seconds); starts as C ends
	TOTAL_DURATION=3600,          # session time (seconds)
	INTERVAL=300,                 # from the start of one bout to the next (seconds)
	CAMERA_FPS=60,
	CAMERA_PULSE_US=3000,         # camera trigger high time: 18% of 60 Hz, as before
	# The blue LED's first lit frame: this many camera pulses after the camera
	# starts (CAMERA_LEAD_IN at CAMERA_FPS: pulse 300, 5 s in)
	BLUE_FRAME=None,
	# The LEDs switch this long before a camera pulse, in the dark between
	# exposures, so that every frame is either wholly lit or wholly dark
	LED_LEAD_US=1000,
	EXPOSURE_US=200,              # the camera's exposure (campy config), to check the above
	# A camera-only session: this many seconds of camera pulses at CAMERA_FPS and
	# nothing else (no bouts, no LEDs, the gate stays low). 0 = the bouts above.
	CAMERA_ONLY=0,
)

# --- Outputs: the Pico's pins (a Nano ESP32's D2-D5), the board's channel, what it is ---
OUTPUTS = [
	# name, Arduino pin, description (as chronicControlv13.py's log names them)
	("A", 2, "Record Gate"),       # GP2 -> OneBox / SpikeGLX level gate
	("B", 3, "Camera"),            # GP3 -> FLIR Line3
	("C", 4, "Blue LED"),          # GP4 -> 465 nm LED controller TTL
	("D", 5, "Yellow LED"),        # GP5 -> 560 nm LED controller TTL
]
GATE, CAMERA, BLUE, YELLOW = range(4)

STATE_DIR = os.path.expanduser("~/.chronic_control")
LOG_ROOT = os.path.expanduser("~/Documents")
WINDOWS = sys.platform == "win32"
PICO_VID = 0x2E8A # Raspberry Pi: the Pico, as a serial port
# stop asks the session to end by writing this file in the state folder: "bout" (after
# the current bout) or "now". (Signals were the Pi's way; Windows has no SIGUSR1.)
STOP_FILE = "stop.request"
# What the session was started with, for attach
SESSION_FILE = "session.json"


class BoardError(Exception):
	pass


def Settings(overrides=()):
	s = dict(SETTINGS)
	for item in overrides:
		name, _, value = item.partition("=")
		if name not in s:
			raise SystemExit("No setting {} (settings: {})".format(name, ", ".join(s)))
		s[name] = None if value == "None" else float(value) if "." in value else int(value)
	if s["BLUE_FRAME"] is None:
		s["BLUE_FRAME"] = round(s["CAMERA_LEAD_IN"] * s["CAMERA_FPS"])
	return s


def Us(seconds):
	return int(round(seconds * 1e6))


# status.json is strict JSON, as every JSON file campy writes (campy/strictjson.py,
# not imported here: the Pi has no campy). JSON has no infinity or NaN; Python's
# json writes them as bare words other readers refuse, so they are the text
# "Infinity", "-Infinity" or "NaN", and read back as the numbers.
NON_FINITE = {"Infinity": math.inf, "-Infinity": -math.inf, "NaN": math.nan}


def StrictJson(data):
	if isinstance(data, dict):
		return {k: StrictJson(v) for k, v in data.items()}
	if isinstance(data, (list, tuple)):
		return [StrictJson(v) for v in data]
	if isinstance(data, float) and not math.isfinite(data):
		return "NaN" if data != data else "Infinity" if data > 0 else "-Infinity"
	return data


def FromStrictJson(data):
	if isinstance(data, dict):
		return {k: FromStrictJson(v) for k, v in data.items()}
	if isinstance(data, list):
		return [FromStrictJson(v) for v in data]
	return NON_FINITE.get(data, data) if isinstance(data, str) else data


def WriteJson(path, data):
	# Whole or not at all: written beside it, then swapped in. On Windows the swap
	# fails while another program (status) has the file open: a moment later it works.
	with open(path + ".new", "w") as f:
		json.dump(StrictJson(data), f, allow_nan=False)
	for attempt in range(50):
		try:
			os.replace(path + ".new", path)
			return
		except PermissionError:
			if attempt == 49:
				raise
			time.sleep(0.02)


def TakeStopRequest(state_dir):
	# What stop asked for ("bout" or "now"), once; None if nothing
	path = os.path.join(state_dir, STOP_FILE)
	try:
		with open(path) as f:
			how = f.read().strip()
		os.remove(path)
	except OSError:
		return None
	return "now" if how == "now" else "bout"


def Rise(start, k, rate_mhz):
	# Pulse k of a train: exactly as chronic_timer.ino works it out
	return start + k * 10**9 // rate_mhz


def Plan(s):
	"""
	The session: bouts (dicts of their times, session microseconds), the
	camera trains (channel, start, count, rate in mHz, width) and every other
	edge (time, channel, level), in order of time.
	"""
	rate = int(round(s["CAMERA_FPS"] * 1000))
	bouts, trains, edges = [], [], []
	if s["CAMERA_ONLY"]:
		count = int(round(s["CAMERA_ONLY"] * s["CAMERA_FPS"]))
		end = Rise(0, count - 1, rate) + s["CAMERA_PULSE_US"]
		bouts.append(dict(number=1, extended=False, gate_on=0, camera_start=0, pulses=count, camera_end=end,
			blue=None, yellow=None, gate_off=end))
		trains.append((CAMERA, 0, count, rate, s["CAMERA_PULSE_US"]))
		return bouts, trains, edges
	for i in range(int(s["TOTAL_DURATION"] // s["INTERVAL"])):
		extended = i % s["EXTENDED_EVERY"] == 0
		b0 = Us(i * s["INTERVAL"])
		cam = b0 + Us(s["TOTAL_BASELINE"] - s["CAMERA_LEAD_IN"])
		count = int(round((s["EXTENDED_STIM_DURATION"] if extended else s["STANDARD_STIM_DURATION"]) * s["CAMERA_FPS"]))
		blue_on = Rise(cam, s["BLUE_FRAME"], rate) - s["LED_LEAD_US"]
		blue_off = blue_on + Us(s["PULSE_WIDTH_C"])
		yellow_on = blue_off
		yellow_off = yellow_on + Us(s["PULSE_WIDTH_D"])
		gate_off = Rise(cam, count, rate) + Us(s["TOTAL_BASELINE"])
		bout = dict(number=i + 1, extended=extended, gate_on=b0, camera_start=cam, pulses=count,
			camera_end=Rise(cam, count - 1, rate) + s["CAMERA_PULSE_US"], blue=(blue_on, blue_off),
			yellow=(yellow_on, yellow_off), gate_off=gate_off)
		if bouts and b0 <= bouts[-1]["gate_off"]:
			raise SystemExit("Bout {} would start before bout {}'s gate goes low: make INTERVAL longer.".format(i + 1, i))
		if not (cam < blue_on and yellow_off < bout["camera_end"]):
			raise SystemExit("Bout {}'s LEDs would be on outside its camera time.".format(i + 1))
		bouts.append(bout)
		trains.append((CAMERA, cam, count, rate, s["CAMERA_PULSE_US"]))
		edges += [(b0, GATE, 1), (blue_on, BLUE, 1), (blue_off, BLUE, 0), (yellow_on, YELLOW, 1),
			(yellow_off, YELLOW, 0), (gate_off, GATE, 0)]
	if not bouts:
		raise SystemExit("TOTAL_DURATION is shorter than INTERVAL: no bouts.")
	# Blue off and yellow on at the same microsecond: the board writes them together
	edges.sort(key=lambda e: e[0])
	return bouts, trains, edges


def LitFrames(bout, on, off, s):
	# The bout's camera pulses whose exposure falls wholly, and partly, within [on, off)
	rate = int(round(s["CAMERA_FPS"] * 1000))
	whole, part = [], []
	for k in range(bout["pulses"]):
		r = Rise(bout["camera_start"], k, rate)
		if r >= off:
			break
		if on <= r and r + s["EXPOSURE_US"] <= off:
			whole.append(k)
		elif r + s["EXPOSURE_US"] > on:
			part.append(k)
	return whole, part


def Describe(s, bouts):
	lines = []
	frames = {}
	for b in bouts:
		if b["blue"] is None:
			lines.append("Camera only: {} pulses at {:g} Hz, {:.6f} s; no LEDs, the gate stays low".format(b["pulses"],
				s["CAMERA_FPS"], b["camera_end"] / 1e6))
			continue
		blue, blue_part = LitFrames(b, *b["blue"], s)
		yellow, yellow_part = LitFrames(b, *b["yellow"], s)
		frames[b["number"]] = (blue, yellow)
		lines.append("Bout {}{}: gate high {:.6f} s, camera {:.6f} s for {} pulses (to {:.6f} s), blue {:.6f}-{:.6f} s "
			"(frames {}), yellow {:.6f}-{:.6f} s (frames {}), gate low {:.6f} s".format(
			b["number"], " (extended)" if b["extended"] else "", b["gate_on"] / 1e6, b["camera_start"] / 1e6, b["pulses"],
			b["camera_end"] / 1e6, b["blue"][0] / 1e6, b["blue"][1] / 1e6, Frames(blue), b["yellow"][0] / 1e6,
			b["yellow"][1] / 1e6, Frames(yellow), b["gate_off"] / 1e6))
		for name, part in (("blue", blue_part), ("yellow", yellow_part)):
			if part:
				lines.append("  Warning: the {} LED switches during the exposure of frame(s) {}".format(name, Frames(part)))
	return lines


def Frames(ks):
	if not ks:
		return "none"
	return "{}-{}".format(ks[0], ks[-1]) if len(ks) > 1 else str(ks[0])


def FindPort(port=None, comports=None):
	"""
	The board's serial port. port: a port name (/dev/ttyACM0, COM7, a pyserial
	URL), a Pico's USB serial number, or None to find it: on Linux in
	/dev/serial/by-id (a Pico, else a Nano ESP32 or another Arduino, as on the Pi);
	elsewhere the one Pico among the serial ports, by its USB IDs. Never an
	Arduino there: campy's trigger Arduino may be on the same computer.
	"""
	if port is not None and (os.path.exists(port) or "://" in port or re.match(r"(?i)^COM\d+$", port)):
		return port
	if port is None and sys.platform.startswith("linux"):
		ports = sorted(glob.glob("/dev/serial/by-id/*Pico*")) or sorted(glob.glob("/dev/serial/by-id/*Nano_ESP32*")) \
			or sorted(glob.glob("/dev/serial/by-id/*Arduino*"))
		return ports[0] if ports else "/dev/ttyACM0"
	if comports is None:
		try:
			from serial.tools import list_ports
			comports = list_ports.comports()
		except ImportError:
			comports = []
	picos = sorted((p for p in comports if p.vid == PICO_VID), key=lambda p: p.device)
	if port is not None:
		for p in picos:
			if (p.serial_number or "").upper() == port.upper():
				return p.device
		return port # a name this computer doesn't list (yet): opening it says so
	if len(picos) == 1:
		return picos[0].device
	if not picos:
		raise SystemExit("No Pico among this computer's serial ports. Is it plugged in? (--port picks a port.)")
	raise SystemExit("{} Picos: pick one with --port, by its port or USB serial number: {}".format(len(picos),
		", ".join("{} ({})".format(p.device, p.serial_number) for p in picos)))


class Board:
	"""The board on its serial port: commands answered with ok/err; the rest are its reports."""

	def __init__(self, port, on_report):
		import serial
		self.port = port
		self.on_report = on_report
		# serial_for_url: a port name, or a URL such as socket://host:port (the tests use one)
		self.connection = serial.serial_for_url(port, baudrate=115200, timeout=0.05)
		self.heard = b""

	def close(self):
		try:
			self.connection.close()
		except Exception:
			pass

	def Poll(self):
		# What it said since, each report passed on; the answers among it returned
		# What's there, or the first byte to come within the timeout: never the
		# whole timeout when an answer is already in (each command waits for one)
		self.heard += self.connection.read(max(1, self.connection.in_waiting))
		answers = []
		while b"\n" in self.heard:
			raw, self.heard = self.heard.split(b"\n", 1)
			line = raw.decode("ascii", "replace").strip()
			if line.startswith("ok") or line.startswith("err"):
				answers.append(line)
			elif line:
				self.on_report(line)
		return answers

	def Command(self, text, timeout=2.0):
		self.connection.write((text + "\n").encode())
		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline:
			for answer in self.Poll():
				if answer.startswith("err"):
					raise BoardError("{} -> {}".format(text, answer))
				return answer
		raise BoardError("no answer to {!r} within {:g} s".format(text, timeout))

	def Check(self):
		# It answers, and runs chronic_timer.ino; returns its status line
		self.connection.reset_input_buffer()
		for attempt in range(3):
			try:
				answer = self.Command("id", timeout=1.0)
				if not answer.startswith("ok chronic_timer"):
					raise BoardError("the board on {} runs something else: {}".format(self.port, answer))
				return self.Command("status")
			except BoardError as e:
				error = e
		raise error


class Session:
	"""Plans, loads and runs one session; logs as chronicControlv13.py did, with the board's times."""

	def __init__(self, s, port, state_dir, log_dir, log_path=None):
		self.s = s
		self.port = port
		self.state_dir = state_dir
		os.makedirs(state_dir, exist_ok=True)
		# log_path: an attached session's, the log it started (even after midnight)
		self.log_path = log_path or os.path.join(log_dir, datetime.now().strftime("%Y-%m-%d"), "timestamps.txt")
		self.log_dir = os.path.dirname(self.log_path)
		os.makedirs(self.log_dir, exist_ok=True)
		self.bouts, self.trains, self.edges = Plan(s)
		self.labels = {}
		for b in self.bouts if not s["CAMERA_ONLY"] else []:
			self.labels[(b["gate_on"], GATE, 1)] = (b, "HIGH (SpikeGLX File Recording Started)")
			self.labels[(b["gate_off"], GATE, 0)] = (b, "LOW (SpikeGLX File Closed)")
			for ch, (on, off) in ((BLUE, b["blue"]), (YELLOW, b["yellow"])):
				self.labels[(on, ch, 1)] = (b, "ON")
				self.labels[(off, ch, 0)] = (b, "OFF")
		self.board = None
		self.t0_wall = None
		self.end_requested = None   # "bout" or "now"
		self.end_sent = None
		self.done = None
		self.pulses = 0
		self.state = "starting"
		self.error = None

	# --- log and status ---

	def Log(self, text, t_us=None):
		wall = datetime.now() if t_us is None or self.t0_wall is None else datetime.fromtimestamp(self.t0_wall + t_us / 1e6)
		line = "[{}] {}".format(wall.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], text)
		with open(self.log_path, "a") as f:
			f.write(line + "\n")
		print(line, flush=True)

	def SessionTime(self):
		return None if self.t0_wall is None else int((time.time() - self.t0_wall) * 1e6)

	def Bout(self, t_us):
		for b in self.bouts:
			if b["gate_on"] <= t_us <= b["gate_off"]:
				return b
		return None

	def WriteStatus(self):
		t = self.SessionTime()
		b = self.Bout(t) if t is not None else None
		status = dict(state=self.state, pid=os.getpid(), port=self.port, log=self.log_path,
			started=self.t0_wall, session_s=None if t is None else t / 1e6,
			bout=None if b is None else b["number"], bouts=len(self.bouts),
			ends_s=self.bouts[-1]["gate_off"] / 1e6 if self.end_sent is None else self.end_sent / 1e6,
			end_requested=self.end_requested, end_sent=self.end_sent is not None, pulses=self.pulses, done=self.done, error=self.error)
		WriteJson(os.path.join(self.state_dir, "status.json"), status)

	# --- what the board reports ---

	def OnReport(self, line):
		words = line.split()
		if words[0] == "ev" and len(words) == 5:
			t, ch, level, late = (int(w) for w in words[1:])
			bout, what = self.labels.get((t, ch, level), (None, "HIGH" if level else "LOW"))
			name, _, description = OUTPUTS[ch]
			self.Log("Output {} ({}): {}  [board t={:.6f} s, {} us late{}]".format(name, description, what, t / 1e6, late,
				", bout {}".format(bout["number"]) if bout else ""), t)
		elif words[0] == "camon" and len(words) == 3:
			i, first = int(words[1]), int(words[2])
			b = self.bouts[i]
			self.Log("Output B (Camera): ON ({:g} Hz, {} us pulses, {} planned)  [board t={:.6f} s, bout {}]".format(
				self.s["CAMERA_FPS"], self.s["CAMERA_PULSE_US"], b["pulses"], first / 1e6, b["number"]), first)
		elif words[0] == "cam" and len(words) == 5:
			i, pulses, first, last = (int(w) for w in words[1:])
			self.pulses += pulses
			b = self.bouts[i]
			self.Log("Output B (Camera): OFF after exactly {} pulses (planned {})  [board t={:.6f} s, from t={:.6f} s, "
				"bout {}]".format(pulses, b["pulses"], last / 1e6, first / 1e6, b["number"]), last if last >= 0 else None)
		elif words[0] == "done":
			self.done = " ".join(words[2:])
			counted = re.search(r"\bpulses=(\d+)", self.done)
			if counted:
				self.pulses = int(counted.group(1)) # the board's own count (an attached session missed some 'cam's)
			if not re.search(r"stalls=0\b", self.done):
				self.Log("SYSTEM: WARNING: the board's output queue ran dry during the session, so edges after that "
					"moved later: {}".format(self.done))
			self.Log("SYSTEM: Board finished the session at t={:.6f} s, every output low: {}".format(int(words[1]) / 1e6,
				self.done))
		elif words[0] == "ready":
			if self.t0_wall is not None and self.done is None:
				self.done = "lost"
				self.error = "the board restarted mid-session ({}): its outputs went low".format(line)
				self.Log("SYSTEM: ERROR: " + self.error)
		else:
			self.Log("SYSTEM: board said {!r}".format(line))

	# --- running it ---

	def Load(self):
		status = self.board.Check()
		if "running" in status:
			raise BoardError("the board is already running a session ({}): run 'stop --now' first".format(status))
		for ch, (name, pin, description) in enumerate(OUTPUTS):
			self.board.Command("pin {} {}".format(ch, pin))
		self.board.Command("clear")
		for train in self.trains:
			self.board.Command("train {} {} {} {} {}".format(*train))
		for t, ch, level in self.edges:
			self.board.Command("edge {} {} {}".format(ch, t, level))

	def Go(self):
		sent = time.time()
		self.board.Command("go")
		# Session time 0 is 20 ms after the board read "go" (GO_DELAY_US)
		self.t0_wall = (sent + time.time()) / 2 + 0.020

	def RequestEnd(self, how):
		if self.end_requested != "now":
			self.end_requested = how

	def SendEnd(self):
		if self.end_requested is None or self.end_sent is not None:
			return
		t = self.SessionTime()
		b = self.Bout(t + 500000) # half a second's margin: a bout about to start still starts
		if self.end_requested == "bout" and b is not None and not self.s["CAMERA_ONLY"]:
			answer = self.board.Command("end {}".format(b["gate_off"]))
			self.Log("SYSTEM: Stop requested: the session ends after bout {}, when its gate goes low at board t={:.6f} s."
				.format(b["number"], b["gate_off"] / 1e6))
		else:
			answer = self.board.Command("stop")
			self.Log("SYSTEM: Stop requested{}: everything goes low now (the camera finishes its pulse).".format(
				"" if self.end_requested == "now" else " between bouts"))
		words = answer.split()
		self.end_sent = int(words[2]) if len(words) > 2 and words[2].lstrip("-").isdigit() else t
		self.WriteStatus()

	def Reconnect(self):
		# The USB link came back (or not yet): the board kept the time meanwhile
		self.board.close()
		self.board = None
		try:
			board = Board(self.port, self.OnReport)
			status = board.Check()
		except Exception:
			return False
		self.board = board
		self.Log("SYSTEM: Board back on {}: {}".format(self.port, status))
		if "idle" in status and self.done is None:
			end = self.end_sent if self.end_sent is not None else self.bouts[-1]["gate_off"]
			if self.SessionTime() >= end:
				self.done = "finished while the board was unreachable (its report was lost)"
			else:
				self.done = "lost"
				self.error = "the board stopped mid-session (restarted, or lost power): its outputs are low"
				self.Log("SYSTEM: ERROR: " + self.error)
		return True

	def Run(self):
		self.Log("=== NEW SESSION (chronic_control.py, timing by the board) ===")
		for line in Describe(self.s, self.bouts):
			self.Log("PLAN: " + line)
		try:
			self.board = Board(self.port, self.OnReport)
			self.Load()
			self.Go()
		except Exception as e:
			self.state, self.error = "failed", "could not start the board on {}: {}".format(self.port, e)
			self.Log("SYSTEM: ERROR: " + self.error)
			self.WriteStatus()
			if self.board:
				self.board.close()
			return 1
		self.state = "running"
		WriteJson(os.path.join(self.state_dir, SESSION_FILE), dict(settings=self.s, port=self.port, log=self.log_path,
			started=self.t0_wall))
		self.Log("SYSTEM: Session started: {} bouts, ending at board t={:.1f} s".format(len(self.bouts),
			self.bouts[-1]["gate_off"] / 1e6))
		self.WriteStatus()
		return self.Follow()

	def Attach(self):
		# A session the board is running, started by this program before it stopped
		# (this computer restarted): its time now from the board, then on as Run
		self.Log("=== ATTACHED to the session the board is running (chronic_control.py attach) ===")
		try:
			self.board = Board(self.port, self.OnReport)
			sent = time.time()
			status = self.board.Check()
			now = (sent + time.time()) / 2
		except Exception as e:
			self.state, self.error = "failed", "could not reach the board on {}: {}".format(self.port, e)
			self.Log("SYSTEM: ERROR: " + self.error)
			self.WriteStatus()
			if self.board:
				self.board.close()
			return 1
		t = re.search(r"\brunning t=(-?\d+)", status)
		if not t:
			self.state = "failed"
			self.error = ("the board is not running a session ({}): it finished, or lost power and restarted, "
				"which sets every output low".format(status))
			self.Log("SYSTEM: ERROR: " + self.error)
			self.WriteStatus()
			self.board.close()
			return 1
		loaded = re.search(r"\bedges=\d+/(\d+) trains=\d+/(\d+)", status)
		if loaded and (int(loaded.group(1)), int(loaded.group(2))) != (len(self.edges), len(self.trains)):
			self.state = "failed"
			self.error = ("the board runs a different session ({}) from the one started here ({} edges, {} trains): "
				"not attached".format(status, len(self.edges), len(self.trains)))
			self.Log("SYSTEM: ERROR: " + self.error)
			self.WriteStatus()
			self.board.close()
			return 1
		self.t0_wall = now - int(t.group(1)) / 1e6
		self.state = "running"
		b = self.Bout(int(t.group(1)))
		self.Log("SYSTEM: Board at t={:.6f} s{}: {}. Edges and camera trains before now were not logged here; "
			"SpikeGLX has them.".format(int(t.group(1)) / 1e6, ", in bout {}".format(b["number"]) if b else ", between bouts",
			status))
		self.WriteStatus()
		return self.Follow()

	def Follow(self):
		# Log what the board reports until it is done; pass on stop requests
		last_status = time.monotonic()
		lost_since = None
		while self.done is None:
			try:
				if self.board is None:
					if not self.Reconnect():
						time.sleep(1.0)
						continue
					lost_since = None
				how = TakeStopRequest(self.state_dir)
				if how:
					self.RequestEnd(how)
				self.SendEnd()
				self.board.Poll()
				if time.monotonic() - last_status > 1.0:
					self.WriteStatus()
					last_status = time.monotonic()
			except BoardError as e:
				self.Log("SYSTEM: board: {}".format(e))
				time.sleep(0.2)
			except Exception as e:
				if lost_since is None:
					lost_since = time.monotonic()
					self.Log("SYSTEM: WARNING: lost the board on {} ({}); it keeps the session going on its own. "
						"Reconnecting...".format(self.port, e))
				if self.board is not None:
					self.board.close()
				self.board = None
				time.sleep(1.0)
		self.state = "done" if self.error is None else "failed"
		self.Log("SYSTEM: Session over: {} camera pulses{}".format(self.pulses, "" if self.error is None else
			". ERROR: " + self.error))
		self.WriteStatus()
		if self.board:
			self.board.close()
		return 0 if self.error is None else 1


# --- commands ---

def ReadStatus(state_dir):
	try:
		with open(os.path.join(state_dir, "status.json")) as f:
			return FromStrictJson(json.load(f))
	except (OSError, ValueError):
		return None


def Alive(pid):
	if not isinstance(pid, int):
		return False
	if WINDOWS:
		# Not os.kill(pid, 0): on Windows that ends the process (TerminateProcess)
		import ctypes
		from ctypes import wintypes
		kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
		kernel32.OpenProcess.restype = wintypes.HANDLE
		kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
		kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
		kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
		handle = kernel32.OpenProcess(0x1000, False, pid) # PROCESS_QUERY_LIMITED_INFORMATION
		if not handle:
			return ctypes.get_last_error() == 5 # access denied: it exists (another user's)
		try:
			code = wintypes.DWORD()
			return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259 # STILL_ACTIVE
		finally:
			kernel32.CloseHandle(handle)
	try:
		os.kill(pid, 0)
		return True
	except OSError:
		return False


def OnStopSignals(handler_bout, handler_now):
	# Ctrl-C (and kill) end it now. SIGUSR1, which the Pi's stop sent before the
	# stop file, still ends it after the bout where there is one.
	if hasattr(signal, "SIGUSR1"):
		signal.signal(signal.SIGUSR1, lambda *a: handler_bout())
	signal.signal(signal.SIGTERM, lambda *a: handler_now())
	signal.signal(signal.SIGINT, lambda *a: handler_now())


def Running(state_dir):
	status = ReadStatus(state_dir)
	if status and status["state"] in ("starting", "running") and Alive(status["pid"]):
		return status
	return None


def CmdRun(args, s):
	session = Session(s, args.port, args.state_dir, args.log_dir)
	OnStopSignals(lambda: session.RequestEnd("bout"), lambda: session.RequestEnd("now"))
	session.WriteStatus()
	return session.Run()


def CmdAttach(args, s):
	status = Running(args.state_dir)
	if status:
		print("A session program is already running here (pid {}): nothing to attach.".format(status["pid"]))
		return 1
	try:
		with open(os.path.join(args.state_dir, SESSION_FILE)) as f:
			saved = FromStrictJson(json.load(f))
	except (OSError, ValueError):
		print("No session was started here (no {}): nothing to attach to.".format(os.path.join(args.state_dir, SESSION_FILE)))
		return 1
	if args.foreground:
		session = Session(saved["settings"], args.port_given or saved["port"], args.state_dir, None, log_path=saved["log"])
		OnStopSignals(lambda: session.RequestEnd("bout"), lambda: session.RequestEnd("now"))
		session.WriteStatus()
		return session.Attach()
	args.port = args.port_given or saved["port"]
	return CmdStart(args, s)


# --- the wiring check ---

BLINK_US = 500000   # high, then low, for each blink
PAUSE_US = 2500000  # after each output's blinks


def WiringPlan(channels, pause_us=PAUSE_US):
	"""
	One round of the wiring check: each output in channels (letters, in the
	order of OUTPUTS) blinks its letter's number of times on its own, A once,
	B twice, C three times, D four. Returns the edges (time, channel, level),
	in order, and the round's length, microseconds.
	"""
	channels = channels.upper()
	edges, t = [], 0
	for ch, (name, pin, description) in enumerate(OUTPUTS):
		if name not in channels:
			continue
		for k in range(ch + 1):
			edges += [(t, ch, 1), (t + BLINK_US, ch, 0)]
			t += 2 * BLINK_US
		t += PAUSE_US
	t += pause_us - PAUSE_US
	return edges, t


BLINK_ROUND_S = 300 # blink: a round of this many seconds, loaded while the last ends


def BlinkPlan(channels, hz, round_us=BLINK_ROUND_S * 10**6):
	"""
	Every output in channels blinking together at hz (half the period on),
	for one round: edges (time, channel, level), in order, and the round's length.
	"""
	period = int(round(1e6 / hz))
	chans = [ch for ch, (name, pin, description) in enumerate(OUTPUTS) if name in channels.upper()]
	edges = []
	for k in range(max(1, round_us // period)):
		edges += [(k * period, ch, 1) for ch in chans] + [(k * period + period // 2, ch, 0) for ch in chans]
	return edges, max(1, round_us // period) * period


def WiringText(channels):
	return ", ".join("{} ({}) {}x".format(name, description, ch + 1) for ch, (name, pin, description) in
		enumerate(OUTPUTS) if name in channels)


def CmdWiringRun(args, s):
	# The check's rounds, one after another until stopped. The board makes each
	# round by itself; between rounds everything is low, so a stop is clean.
	channels = args.channels.upper()
	blink = args.command == "blink-run"
	edges, length = BlinkPlan(channels, args.hz) if blink else WiringPlan(channels, Us(args.pause))
	stop = {"how": None}
	OnStopSignals(lambda: stop.update(how="now"), lambda: stop.update(how="now"))
	os.makedirs(args.state_dir, exist_ok=True)
	day = os.path.join(args.log_dir, datetime.now().strftime("%Y-%m-%d"))
	os.makedirs(day, exist_ok=True)
	log_path = os.path.join(day, "wiring.txt")
	started = time.time()
	status = dict(state="running", mode="wiring", pid=os.getpid(), port=args.port, log=log_path, started=started,
		session_s=0, bout=None, bouts=0, ends_s=None, end_requested=None, end_sent=False, pulses=0, done=None, error=None)

	def Write(**changes):
		status.update(changes, session_s=time.time() - started)
		WriteJson(os.path.join(args.state_dir, "status.json"), status)

	def Log(text):
		line = "[{}] {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], text)
		with open(log_path, "a") as f:
			f.write(line + "\n")
		print(line, flush=True)

	reports = []
	board, rounds, lost_since = None, 0, None
	if blink:
		Log("=== BLINK: {} together at {:g} Hz, in rounds of {:g} s (a pause between rounds, while "
			"the next loads) ===".format(", ".join(n for n, _, _ in OUTPUTS if n in channels), args.hz, length / 1e6))
	else:
		Log("=== WIRING CHECK: {}, each followed by a {:g} s pause; a round every {:g} s ===".format(WiringText(channels),
			PAUSE_US / 1e6, length / 1e6))
	Write()
	while stop["how"] is None:
		try:
			if board is None:
				board = Board(args.port, reports.append)
				board.Check()
				if lost_since is not None:
					Log("Board back on {}.".format(args.port))
				lost_since = None
			if not reports or reports[-1].startswith("done"):
				board.Command("clear")
				for t, ch, level in edges:
					if stop["how"] is not None:
						break
					board.Command("edge {} {} {}".format(ch, t, level))
				if stop["how"] is not None:
					break
				reports.clear()
				board.Command("go")
				board.Command("end {}".format(length)) # the round lasts its pause too: low until then
				rounds += 1
				if rounds == 1 or rounds % 100 == 0:
					Log("Round {} started.".format(rounds))
				Write(bouts=rounds)
			board.Poll()
			if TakeStopRequest(args.state_dir):
				stop.update(how="now")
		except Exception as e:
			if TakeStopRequest(args.state_dir):
				stop.update(how="now")
			if lost_since is None:
				lost_since = time.time()
				Log("Lost the board on {} ({}); it ends its round low by itself. Reconnecting...".format(args.port, e))
			if board is not None:
				board.close()
			board, reports = None, []
			time.sleep(2)
	# Low before leaving: what the board said last, or a stop to be sure
	try:
		if board is None:
			board = Board(args.port, reports.append)
		board.Command("stop")
		board.close()
	except Exception as e:
		Log("Could not reach the board to stop it ({}): it stops by itself at the end of its round.".format(e))
	Log("Wiring check stopped after {} rounds.".format(rounds))
	Write(state="done", done="wiring check stopped after {} rounds".format(rounds))
	return 0


def CmdStart(args, s):
	status = Running(args.state_dir)
	if status:
		print("A session is already running (pid {}, log {}). Stop it first.".format(status["pid"], status["log"]))
		return 1
	wiring = args.command in ("wiring", "blink")
	attach = args.command == "attach"
	if args.command == "blink":
		BlinkPlan(args.channels, args.hz)
	elif wiring:
		WiringPlan(args.channels, Us(args.pause))
	elif not attach:
		Plan(s) # a plan that can't run says so here
	os.makedirs(args.state_dir, exist_ok=True)
	for name in ("status.json", STOP_FILE) + (() if attach else (SESSION_FILE,)):
		try:
			os.remove(os.path.join(args.state_dir, name))
		except OSError:
			pass
	out = open(os.path.join(args.state_dir, "session.out"), "a")
	command = [sys.executable, os.path.abspath(__file__), {"wiring": "wiring-run", "blink": "blink-run",
		"attach": "attach"}.get(args.command, "run"), "--port", args.port, "--state-dir", args.state_dir, "--log-dir",
		args.log_dir, "--channels", args.channels, "--pause", str(args.pause), "--hz", str(args.hz)] \
		+ sum((["--set", item] for item in args.set), []) + (["--foreground"] if attach else [])
	if WINDOWS:
		# Its own process, with no console: closing the window it was started from
		# doesn't end it. It stays in the starter's job object, so under rig.py it is
		# the recording job's; from an SSH session, it ends when SSH disconnects.
		flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008 # DETACHED_PROCESS
		child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=out, creationflags=flags)
	else:
		child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
	deadline = time.monotonic() + 180 # loading a 6-day session is thousands of commands, a millisecond or two each
	while time.monotonic() < deadline:
		status = ReadStatus(args.state_dir)
		if status and status["pid"] == child.pid and status["state"] != "starting":
			break
		if child.poll() is not None:
			break
		time.sleep(0.1)
	status = ReadStatus(args.state_dir)
	if status and status["state"] == "running" and args.command == "blink":
		print("Blinking (pid {}): {} together at {:g} Hz, until 'stop'. Log: {}".format(child.pid,
			", ".join(n for n, _, _ in OUTPUTS if n in args.channels.upper()), args.hz, status["log"]))
		return 0
	if status and status["state"] == "running" and wiring:
		print("Wiring check started (pid {}): {}, every {:g} s, until 'stop'. Log: {}".format(child.pid,
			WiringText(args.channels.upper()), WiringPlan(args.channels, Us(args.pause))[1] / 1e6, status["log"]))
		return 0
	if status and status["state"] == "running" and attach:
		print("Attached (pid {}): {:.0f} s into the session, bout {} of {}; it ends at {:.0f} s. Log: {}".format(child.pid,
			status["session_s"], status["bout"] or "-", status["bouts"], status["ends_s"], status["log"]))
		return 0
	if status and status["state"] == "running":
		print("Session started (pid {}): {} bouts, {:.0f} s. Log: {}".format(child.pid, status["bouts"], status["ends_s"],
			status["log"]))
		return 0
	print("The session did not start: {}".format(status["error"] if status and status.get("error") else
		"see {}".format(os.path.join(args.state_dir, "session.out"))))
	return 1


def CmdStop(args, s):
	status = Running(args.state_dir)
	if status is None:
		if not args.now:
			print("No session is running. ('stop --now' also tells the board itself to stop.)")
			return 0
		board = Board(args.port, lambda line: print("board: " + line))
		try:
			board.Check()
			print("No session was running here; told the board to stop: {}".format(board.Command("stop")))
		finally:
			board.close()
		return 0
	wiring = status.get("mode") == "wiring"
	path = os.path.join(args.state_dir, STOP_FILE)
	with open(path + ".new", "w") as f:
		f.write("now" if args.now or wiring else "bout")
	os.replace(path + ".new", path) # the session takes it within a fraction of a second
	if wiring:
		args.now = True # a wiring check stops at once
	deadline = time.monotonic() + (15 if args.now else 10)
	while time.monotonic() < deadline:
		now = ReadStatus(args.state_dir)
		if not Alive(status["pid"]) or (now and now["state"] in ("done", "failed")):
			print("Session stopped: {}".format(now["done"] if now else "?"))
			return 0
		if not args.now and now and now.get("end_sent"):
			print("The session ends after its current bout, in {:.0f} s (board t={:.1f} s).".format(
				max(0, now["ends_s"] - (now["session_s"] or 0)), now["ends_s"]))
			return 0
		time.sleep(0.1)
	print("Asked the session to stop; it has not answered yet (is the board unplugged?). Check 'status'.")
	return 1


def CmdStatus(args, s):
	status = ReadStatus(args.state_dir)
	if status is None:
		print("No session has run here yet.")
		return 0
	if status["state"] in ("starting", "running") and not Alive(status["pid"]):
		status["state"] = "gone (the program stopped; the board may still be running: 'attach' to follow it, 'stop --now' to end it)"
	if status.get("mode") == "wiring":
		print("Wiring check: {}, {:.0f} s in, {} rounds".format(status["state"], status.get("session_s") or 0, status["bouts"]))
		for key in ("done", "error"):
			if status.get(key):
				print("  {}: {}".format(key, status[key]))
		print("  log: {}".format(status["log"]))
		return 0
	print("Session: {}".format(status["state"]))
	if status.get("session_s") is not None:
		print("  {:.1f} s in, bout {} of {}; ends at {:.1f} s{}".format(status["session_s"], status["bout"] or "-",
			status["bouts"], status["ends_s"], " (stop requested)" if status.get("end_requested") else ""))
	for key in ("done", "error"):
		if status.get(key):
			print("  {}: {}".format(key, status[key]))
	print("  log: {}".format(status["log"]))
	return 0


def CmdPlan(args, s):
	bouts, trains, edges = Plan(s)
	for line in Describe(s, bouts):
		print(line)
	print("{} bouts; {} camera pulses; {} edges; the board's gate goes low last at {:.6f} s".format(
		len(bouts), sum(t[2] for t in trains), len(edges), bouts[-1]["gate_off"] / 1e6))
	return 0


def Main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("command", choices=["start", "stop", "status", "plan", "run", "wiring", "wiring-run", "blink", "blink-run",
		"attach"])
	parser.add_argument("--hz", type=float, default=2, help="blink: blinks a second (default 2)")
	parser.add_argument("--pause", type=float, default=PAUSE_US / 1e6, help="wiring: seconds after the round's last output (default 2.5)")
	parser.add_argument("--channels", default="ABCD", help="wiring: the outputs to blink (A gate, B camera, C blue, D yellow)")
	parser.add_argument("--now", action="store_true", help="stop: end now rather than after the current bout")
	parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="change a timing setting")
	parser.add_argument("--port", default=None, help="the board's serial port, or a Pico's USB serial number (default: "
		"found in /dev/serial/by-id on Linux, the one Pico elsewhere)")
	parser.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS) # attach: the program itself
	parser.add_argument("--state-dir", default=STATE_DIR)
	parser.add_argument("--log-dir", default=LOG_ROOT, help="timestamps.txt goes in <log-dir>/<date>/")
	args = parser.parse_args(argv)
	args.port_given = None
	if args.command == "attach":
		# The port the session used, unless one is given
		args.port_given = FindPort(args.port) if args.port else None
	elif args.command not in ("plan", "status", "stop") or args.now or args.port:
		args.port = FindPort(args.port) # only where it talks to the board
	s = Settings(args.set)
	return {"start": CmdStart, "stop": CmdStop, "status": CmdStatus, "plan": CmdPlan, "run": CmdRun, "wiring": CmdStart,
		"wiring-run": CmdWiringRun, "blink": CmdStart, "blink-run": CmdWiringRun, "attach": CmdAttach}[args.command](args, s)


if __name__ == "__main__":
	sys.exit(Main())
