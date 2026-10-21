#!/usr/bin/env python3
"""
chronic_control.py: ChronicRecord's stimulus sessions, made by the Arduino
Nano ESP32 (chronic_timer/chronic_timer.ino) and run from the Raspberry Pi.

The Pi plans the whole session (the same bouts as chronicControlv13.py) and
loads it onto the Nano over USB. The Nano then makes every TTL from its own
crystal clock: the OneBox record gate, the 60 Hz camera trigger and the two
LED controllers, each edge on its microsecond, the LEDs on whole camera frames.
The Pi only starts it, logs what the Nano reports, and stops it.

Over SSH (each returns at once; the session runs in the background):
  python3 chronic_control.py start        start a session
  python3 chronic_control.py stop         end it after the current bout (on time)
  python3 chronic_control.py stop --now   end it now: the camera finishes its
                                          pulse, then everything goes low
  python3 chronic_control.py status       what it is doing
  python3 chronic_control.py plan         print the session's timeline (no board)
  python3 chronic_control.py run          the session in this terminal (start
                                          runs this); Ctrl-C ends it now
Change a setting for one session without editing this file:
  python3 chronic_control.py start --set TOTAL_DURATION=1800 --set INTERVAL=180

Needs pyserial (sudo apt install python3-serial) and the user in the dialout
group; no sudo, no GPIO on the Pi.
"""

import argparse, glob, json, os, signal, subprocess, sys, time
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
)

# --- Outputs: Nano ESP32 screw terminal, the board's channel, what it is ---
OUTPUTS = [
	# name, Arduino pin, description (as chronicControlv13.py's log names them)
	("A", 2, "Record Gate"),       # D2 -> OneBox / SpikeGLX level gate
	("B", 3, "Camera"),            # D3 -> FLIR Line3, 60 Hz
	("C", 4, "Blue LED"),          # D4 -> 465 nm LED controller TTL
	("D", 5, "Yellow LED"),        # D5 -> 560 nm LED controller TTL
]
GATE, CAMERA, BLUE, YELLOW = range(4)

STATE_DIR = os.path.expanduser("~/.chronic_control")
LOG_ROOT = os.path.expanduser("~/Documents")


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


def FindPort():
	ports = sorted(glob.glob("/dev/serial/by-id/*Nano_ESP32*")) or sorted(glob.glob("/dev/serial/by-id/*Arduino*"))
	return ports[0] if ports else "/dev/ttyACM0"


class Board:
	"""The Nano on its serial port: commands answered with ok/err; the rest are its reports."""

	def __init__(self, port, on_report):
		import serial
		self.port = port
		self.on_report = on_report
		self.connection = serial.Serial(port=port, baudrate=115200, timeout=0.05)
		self.heard = b""

	def close(self):
		try:
			self.connection.close()
		except Exception:
			pass

	def Poll(self):
		# What it said since, each report passed on; the answers among it returned
		self.heard += self.connection.read(4096)
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

	def __init__(self, s, port, state_dir, log_dir):
		self.s = s
		self.port = port
		self.state_dir = state_dir
		os.makedirs(state_dir, exist_ok=True)
		self.log_dir = os.path.join(log_dir, datetime.now().strftime("%Y-%m-%d"))
		os.makedirs(self.log_dir, exist_ok=True)
		self.log_path = os.path.join(self.log_dir, "timestamps.txt")
		self.bouts, self.trains, self.edges = Plan(s)
		self.labels = {}
		for b in self.bouts:
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
		path = os.path.join(self.state_dir, "status.json")
		with open(path + ".new", "w") as f:
			json.dump(status, f)
		os.replace(path + ".new", path)

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
		if self.end_requested == "bout" and b is not None:
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
		self.Log("=== NEW SESSION (chronic_control.py, timing by the Nano ESP32) ===")
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
		self.Log("SYSTEM: Session started: {} bouts, ending at board t={:.1f} s".format(len(self.bouts),
			self.bouts[-1]["gate_off"] / 1e6))
		self.WriteStatus()
		last_status = time.monotonic()
		lost_since = None
		while self.done is None:
			try:
				if self.board is None:
					if not self.Reconnect():
						time.sleep(1.0)
						continue
					lost_since = None
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
			return json.load(f)
	except (OSError, ValueError):
		return None


def Alive(pid):
	try:
		os.kill(pid, 0)
		return True
	except (OSError, TypeError):
		return False


def Running(state_dir):
	status = ReadStatus(state_dir)
	if status and status["state"] in ("starting", "running") and Alive(status["pid"]):
		return status
	return None


def CmdRun(args, s):
	session = Session(s, args.port, args.state_dir, args.log_dir)
	signal.signal(signal.SIGUSR1, lambda *a: session.RequestEnd("bout"))
	signal.signal(signal.SIGTERM, lambda *a: session.RequestEnd("now"))
	signal.signal(signal.SIGINT, lambda *a: session.RequestEnd("now"))
	session.WriteStatus()
	return session.Run()


def CmdStart(args, s):
	status = Running(args.state_dir)
	if status:
		print("A session is already running (pid {}, log {}). Stop it first.".format(status["pid"], status["log"]))
		return 1
	Plan(s) # a plan that can't run says so here
	os.makedirs(args.state_dir, exist_ok=True)
	try:
		os.remove(os.path.join(args.state_dir, "status.json"))
	except OSError:
		pass
	out = open(os.path.join(args.state_dir, "session.out"), "a")
	command = [sys.executable, os.path.abspath(__file__), "run", "--port", args.port, "--state-dir", args.state_dir,
		"--log-dir", args.log_dir] + sum((["--set", item] for item in args.set), [])
	child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
	deadline = time.monotonic() + 30
	while time.monotonic() < deadline:
		status = ReadStatus(args.state_dir)
		if status and status["pid"] == child.pid and status["state"] != "starting":
			break
		if child.poll() is not None:
			break
		time.sleep(0.1)
	status = ReadStatus(args.state_dir)
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
	os.kill(status["pid"], signal.SIGTERM if args.now else signal.SIGUSR1)
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
		status["state"] = "gone (the program stopped; the board may still be running: 'stop --now')"
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
	parser.add_argument("command", choices=["start", "stop", "status", "plan", "run"])
	parser.add_argument("--now", action="store_true", help="stop: end now rather than after the current bout")
	parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="change a timing setting")
	parser.add_argument("--port", default=None, help="the Nano's serial port (default: found in /dev/serial/by-id)")
	parser.add_argument("--state-dir", default=STATE_DIR)
	parser.add_argument("--log-dir", default=LOG_ROOT, help="timestamps.txt goes in <log-dir>/<date>/")
	args = parser.parse_args(argv)
	args.port = args.port or FindPort()
	s = Settings(args.set)
	return {"start": CmdStart, "stop": CmdStop, "status": CmdStatus, "plan": CmdPlan, "run": CmdRun}[args.command](args, s)


if __name__ == "__main__":
	sys.exit(Main())
