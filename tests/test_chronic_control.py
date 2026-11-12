"""chronicrecord/chronic_control.py: the computer's side of the ChronicRecord timer.

Its plan (no board), and its start/status/stop commands end to end against
chronic_timer.ino running in real time on a pseudo-terminal (tests/arduino_sim, live).
"""

import os, re, subprocess, sys, time

import numpy as np
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROL = os.path.join(REPO, "chronicrecord", "chronic_control.py")
sys.path.insert(0, os.path.join(REPO, "chronicrecord"))
import chronic_control as cc

# A short session: 2 bouts, 3.5 s apart; the first extended (1.5 s of camera), the second 1 s
SHORT = ["TOTAL_BASELINE=1", "CAMERA_LEAD_IN=0.5", "STANDARD_STIM_DURATION=1", "EXTENDED_STIM_DURATION=1.5",
	"INTERVAL=3.5", "TOTAL_DURATION=7"]
# The same bouts' camera pulses (90 and 60), with 2.5 s of baseline either side: bout 1 is 6 s long, not 3
LONGER = ["TOTAL_BASELINE=2.5", "CAMERA_LEAD_IN=0.5", "STANDARD_STIM_DURATION=1", "EXTENDED_STIM_DURATION=1.5",
	"INTERVAL=6.5", "TOTAL_DURATION=13"]
# One bout whose camera runs 10 s (600 pulses, from 0.5 s to 10.5 s): a stop that should come mid-bout does, whatever
# a busy runner does to the Pythons that start and stop it
ONE_LONG_BOUT = ["TOTAL_BASELINE=1", "CAMERA_LEAD_IN=0.5", "STANDARD_STIM_DURATION=1", "EXTENDED_STIM_DURATION=10",
	"INTERVAL=12", "TOTAL_DURATION=12"]


def test_status_json_is_strict_json_and_reads_back_as_numbers(tmp_path):
	# As every JSON file campy writes (campy/strictjson.py): an infinite or NaN
	# number is the text, and the status command gets the number back
	import json, math
	status = {"state": "running", "session_s": math.nan, "ends_s": math.inf, "bouts": [1.5, -math.inf], "pid": 7}
	(tmp_path / "status.json").write_text(json.dumps(cc.StrictJson(status), allow_nan=False))
	def refuse(word):
		raise ValueError("not JSON: " + word)
	written = json.loads((tmp_path / "status.json").read_text(), parse_constant=refuse)
	assert written["session_s"] == "NaN" and written["ends_s"] == "Infinity" and written["bouts"] == [1.5, "-Infinity"]
	back = cc.ReadStatus(str(tmp_path))
	assert math.isnan(back["session_s"]) and back["ends_s"] == math.inf and back["bouts"] == [1.5, -math.inf]
	assert back["state"] == "running" and back["pid"] == 7
	# One written before (bare words) reads the same
	(tmp_path / "status.json").write_text('{"ends_s": Infinity}')
	assert cc.ReadStatus(str(tmp_path))["ends_s"] == math.inf


def test_the_plan_is_chronic_control_v13s_session():
	s = cc.Settings()
	bouts, trains, edges = cc.Plan(s)
	assert len(bouts) == 12
	assert [b["number"] for b in bouts if b["extended"]] == [1, 7]
	assert [b["pulses"] for b in bouts] == [14400] + [600] * 5 + [14400] + [600] * 5
	for i, b in enumerate(bouts):
		b0 = i * 300 * 10**6
		assert b["gate_on"] == b0
		assert b["camera_start"] == b0 + 5 * 10**6 # 5 s pre-baseline
		# The blue LED: 5 s into the camera (pulse 300), switched 1 ms before that pulse
		assert b["blue"] == (b0 + 10 * 10**6 - 1000, b0 + 10 * 10**6 - 1000 + 30000)
		assert b["yellow"] == (b["blue"][1], b["blue"][1] + 30000) # as blue ends
		# Camera for 240 s or 10 s, then the 10 s post-baseline
		assert b["gate_off"] == b["camera_start"] + (240 if b["extended"] else 10) * 10**6 + 10 * 10**6
	assert [t for t, _, _ in edges] == sorted(t for t, _, _ in edges)
	assert len(edges) == 12 * 6 and sum(t[2] for t in trains) == 2 * 14400 + 10 * 600


def test_each_led_lights_two_whole_frames():
	s = cc.Settings()
	bouts, _, _ = cc.Plan(s)
	for b in bouts:
		assert cc.LitFrames(b, *b["blue"], s) == ([300, 301], [])
		assert cc.LitFrames(b, *b["yellow"], s) == ([302, 303], [])


def test_the_snapshot_photometry_pattern():
	# neuro-seeER's snapshot: 50 ms of yellow (560 nm), 150 ms dark, 50 ms of blue (470 nm), once a bout.
	# 50 ms is exactly 3 frames at 60 Hz and 150 ms exactly 9, so every edge stays 1 ms before a camera pulse
	s = cc.Settings(["YELLOW_FIRST=1", "PULSE_WIDTH_C=0.05", "PULSE_WIDTH_D=0.05", "LED_GAP=0.15"])
	bouts, trains, edges = cc.Plan(s)
	for i, b in enumerate(bouts):
		b0 = i * 300 * 10**6
		assert b["yellow"] == (b0 + 10 * 10**6 - 1000, b0 + 10 * 10**6 - 1000 + 50000)
		assert b["blue"] == (b["yellow"][1] + 150000, b["yellow"][1] + 150000 + 50000)
		assert cc.LitFrames(b, *b["yellow"], s) == ([300, 301, 302], [])
		assert cc.LitFrames(b, *b["blue"], s) == ([312, 313, 314], [])
	assert len(edges) == 12 * 6 and [t for t, _, _ in edges] == sorted(t for t, _, _ in edges)
	lines = cc.Describe(s, bouts)
	assert "yellow 9.999000-10.049000 s (frames 300-302), blue 10.199000-10.249000 s (frames 312-314)" in lines[0]
	assert not any("Warning" in line for line in lines)
	# A gap with blue first, as before otherwise
	s = cc.Settings(["LED_GAP=0.1"])
	b = cc.Plan(s)[0][0]
	assert b["yellow"] == (b["blue"][1] + 100000, b["blue"][1] + 100000 + 30000)
	with pytest.raises(SystemExit, match="overlap"):
		cc.Plan(cc.Settings(["LED_GAP=-0.01"]))
	with pytest.raises(SystemExit, match="YELLOW_FIRST"):
		cc.Plan(cc.Settings(["YELLOW_FIRST=2"]))
	with pytest.raises(SystemExit, match="outside its camera time"):
		cc.Plan(cc.Settings(["STANDARD_STIM_DURATION=5.1", "LED_GAP=0.15"])) # the second LED past the camera's end


def test_the_plan_warns_of_a_half_lit_frame():
	# No lead: the LED switches as pulse 300's exposure starts
	s = cc.Settings(["LED_LEAD_US=-100"])
	bouts, _, _ = cc.Plan(s)
	lines = cc.Describe(s, bouts)
	assert any("Warning: the blue LED switches during the exposure of frame(s) 300" in line for line in lines)


def test_settings_on_the_command_line():
	s = cc.Settings(["TOTAL_DURATION=600", "PULSE_WIDTH_C=0.05"])
	assert s["TOTAL_DURATION"] == 600 and s["PULSE_WIDTH_C"] == 0.05
	with pytest.raises(SystemExit):
		cc.Settings(["NO_SUCH=1"])
	with pytest.raises(SystemExit, match="INTERVAL"):
		cc.Plan(cc.Settings(["INTERVAL=200"])) # the 255 s extended bout runs into the next
	with pytest.raises(SystemExit, match="before the gate opens"):
		cc.Plan(cc.Settings(["CAMERA_LEAD_IN=12"])) # the camera 12 s before the LEDs, the gate only 10 s


def test_plan_command_touches_no_board():
	out = subprocess.run([sys.executable, CONTROL, "plan", "--port", "/dev/no-such-port"], capture_output=True,
		text=True, check=True).stdout
	assert "12 bouts; 34800 camera pulses; 72 edges" in out
	assert "blue 9.999000-10.029000 s (frames 300-301), yellow 10.029000-10.059000 s (frames 302-303)" in out


# --- end to end, against the sketch in real time ---

def pty():
	import tty
	master, slave = os.openpty()
	tty.setraw(slave)
	return master, slave


def sim_up(proc, log_path, said, timeout=60):
	"""
	Waits until the live simulator has started its sketch: it said said (its
	first line, in the log). A fixed wait isn't one: the first start of a
	just-compiled simulator on a busy computer (4 tests at once) can take
	seconds, and a program that opens the port first gets no answer in time.
	"""
	deadline = time.monotonic() + timeout
	while proc.poll() is None and said not in log_path.read_text() and time.monotonic() < deadline:
		time.sleep(0.01)
	if proc.poll() is not None or said not in log_path.read_text():
		proc.kill()
		proc.wait()
		raise AssertionError("the simulated board didn't start: {}".format(log_path.read_text()[-2000:]))


@pytest.fixture
def board(tmp_path, timer_sim):
	# The Nano: chronic_timer.ino live on a pty's master end; its log has every pin change
	if sys.platform == "win32":
		pytest.skip("needs a pseudo-terminal")
	master, slave = pty()
	log_path = tmp_path / "board.log"
	with open(str(log_path), "w") as log:
		proc = subprocess.Popen([timer_sim, "live"], stdin=master, stdout=master, stderr=log)
	sim_up(proc, log_path, "ready chronic_timer")
	try:
		yield os.ttyname(slave), log_path
	finally:
		proc.terminate()
		proc.wait()
		os.close(slave)
		os.close(master)


def control(tmp_path, port, *args, settings=SHORT):
	command = [sys.executable, CONTROL] + list(args) + ["--port", port, "--state-dir", str(tmp_path / "state"),
		"--log-dir", str(tmp_path / "logs")] + sum((["--set", s] for s in settings), [])
	return subprocess.run(command, capture_output=True, text=True, timeout=60)


def wait_done(tmp_path, timeout=20):
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		status = cc.ReadStatus(str(tmp_path / "state"))
		if status and status["state"] in ("done", "failed") and not cc.Alive(status["pid"]):
			return status
		time.sleep(0.1)
	raise AssertionError("the session did not end: {}".format(cc.ReadStatus(str(tmp_path / "state"))))


def timestamps(tmp_path):
	(day,) = (tmp_path / "logs").iterdir()
	return (day / "timestamps.txt").read_text()


def pins(log_path):
	# pin -> [(us, level)] from the live sketch's log
	out = {}
	for line in log_path.read_text().splitlines():
		words = line.split()
		if len(words) == 3 and words[0].isdigit():
			out.setdefault(int(words[1]), []).append((int(words[0]), int(words[2])))
	return out


def test_a_whole_session_started_over_ssh(tmp_path, board):
	port, log_path = board
	start = control(tmp_path, port, "start")
	assert start.returncode == 0, start.stdout + start.stderr
	assert "Session started" in start.stdout and "2 bouts" in start.stdout # it returned: the session runs on
	status = control(tmp_path, port, "status")
	assert "Session: running" in status.stdout
	again = control(tmp_path, port, "start")
	assert again.returncode == 1 and "already running" in again.stdout
	assert wait_done(tmp_path)["state"] == "done"

	text = timestamps(tmp_path)
	assert text.count("Output A (Record Gate): HIGH") == 2 and text.count("Output A (Record Gate): LOW") == 2
	for led in ("C (Blue LED)", "D (Yellow LED)"):
		assert text.count("Output {}: ON".format(led)) == 2 and text.count("Output {}: OFF".format(led)) == 2
	assert "OFF after exactly 90 pulses (planned 90)" in text and "OFF after exactly 60 pulses (planned 60)" in text
	assert text.count("Output B (Camera): ON (60 Hz, 3000 us pulses") == 2
	assert "Session over: 150 camera pulses\n" in text
	# The board's own record: 150 camera pulses, and everything low at the end
	p = pins(log_path)
	assert sum(1 for _, level in p[3] if level == 1) == 150
	for pin in (2, 3, 4, 5):
		assert p[pin][-1][1] == 0
	# Board times in the log are the plan's, to the microsecond
	bouts, _, _ = cc.Plan(cc.Settings(SHORT))
	assert "board t={:.6f} s".format(bouts[1]["gate_off"] / 1e6) in text


def test_stop_ends_after_the_current_bout(tmp_path, board):
	port, log_path = board
	assert control(tmp_path, port, "start").returncode == 0
	time.sleep(1.0) # in bout 1
	stop = control(tmp_path, port, "stop")
	assert stop.returncode == 0 and "ends after its current bout" in stop.stdout, stop.stdout
	status = wait_done(tmp_path)
	assert status["state"] == "done"
	text = timestamps(tmp_path)
	assert "the session ends after bout 1, when its gate goes low at board t=3.000000 s" in text
	assert "Output A (Record Gate): LOW (SpikeGLX File Closed)  [board t=3.000000 s" in text
	assert "OFF after exactly 90 pulses (planned 90)" in text and "Session over: 90 camera pulses" in text
	assert text.count("Output A (Record Gate): HIGH") == 1 # bout 2 never started


def test_stop_now_ends_on_a_whole_pulse(tmp_path, board):
	port, log_path = board
	assert control(tmp_path, port, "start").returncode == 0
	time.sleep(1.2) # camera running, LEDs done
	stop = control(tmp_path, port, "stop", "--now")
	assert stop.returncode == 0 and "Session stopped" in stop.stdout, stop.stdout
	text = timestamps(tmp_path)
	n = int(re.search(r"Session over: (\d+) camera pulses", text).group(1))
	assert 20 < n < 90
	p = pins(log_path)
	up = np.array([t for t, level in p[3] if level == 1])
	down = np.array([t for t, level in p[3] if level == 0][1:])
	assert len(up) == len(down) == n
	# Each pulse that rose fell (live, the timing is this computer's, whose
	# serial polls take a millisecond: the microsecond test of a stop is
	# test_chronic_timer.py's, on the simulated clock)
	assert np.all(down > up)
	for pin in (2, 3, 4, 5):
		assert p[pin][-1][1] == 0


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pseudo-terminal")
def test_no_board_no_session(tmp_path):
	master, slave = pty() # nothing answers
	try:
		start = control(tmp_path, os.ttyname(slave), "start")
	finally:
		os.close(slave)
		os.close(master)
	assert start.returncode == 1
	assert "did not start" in start.stdout and "no answer" in start.stdout, start.stdout


def test_the_wiring_check_blinks_each_output_its_letters_number_of_times():
	edges, length = cc.WiringPlan("ABCD")
	for ch, times in ((cc.GATE, 1), (cc.CAMERA, 2), (cc.BLUE, 3), (cc.YELLOW, 4)):
		assert sum(1 for _, c, level in edges if c == ch and level == 1) == times
	assert [t for t, _, _ in edges] == sorted(t for t, _, _ in edges)
	# One output at a time: never two high together
	high = set()
	for t, ch, level in edges:
		(high.add if level else high.discard)(ch)
		assert len(high) <= 1
	assert length == 10 * 2 * cc.BLINK_US + 4 * cc.PAUSE_US
	only, short = cc.WiringPlan("b")
	assert {c for _, c, _ in only} == {cc.CAMERA} and short == 2 * 2 * cc.BLINK_US + cc.PAUSE_US
	_, slow = cc.WiringPlan("AB", pause_us=60 * 10**6) # --pause 60: a minute after the round's last output
	assert slow == 3 * 2 * cc.BLINK_US + cc.PAUSE_US + 60 * 10**6


def test_a_wiring_check_runs_until_stopped(tmp_path, board):
	port, log_path = board
	start = control(tmp_path, port, "wiring", "--channels", "AB")
	assert start.returncode == 0 and "Wiring check started" in start.stdout, start.stdout + start.stderr
	assert "A (Record Gate) 1x, B (Camera) 2x" in start.stdout
	time.sleep(18) # two rounds and more (8 s each)
	assert "Wiring check: running" in control(tmp_path, port, "status").stdout
	stop = control(tmp_path, port, "stop")
	assert stop.returncode == 0 and "stopped" in stop.stdout, stop.stdout
	p = pins(log_path)
	assert sum(1 for _, level in p[2] if level == 1) >= 2 # the gate, a round or more
	assert sum(1 for _, level in p[3] if level == 1) >= 2 # the camera
	assert 4 not in p or all(level == 0 for _, level in p[4]) # the LEDs, never
	assert 5 not in p or all(level == 0 for _, level in p[5])
	for pin in (2, 3):
		assert p[pin][-1][1] == 0
	# Each round lasts its pauses too: the gate blinks once a round, 8 s apart
	_, length = cc.WiringPlan("AB")
	gate = [t for t, level in p[2] if level == 1]
	assert all(b - a > length - 500000 for a, b in zip(gate, gate[1:])), gate
	(day,) = (tmp_path / "logs").iterdir()
	assert "WIRING CHECK" in (day / "wiring.txt").read_text()


def test_blink_switches_every_output_together():
	edges, length = cc.BlinkPlan("ABCD", 2, round_us=10 * 10**6)
	assert length == 10 * 10**6
	for ch in range(4):
		ups = [t for t, c, level in edges if c == ch and level == 1]
		downs = [t for t, c, level in edges if c == ch and level == 0]
		assert ups == list(range(0, 10 * 10**6, 500000)) and downs == [t + 250000 for t in ups]
	assert [t for t, _, _ in edges] == sorted(t for t, _, _ in edges)
	assert len(cc.BlinkPlan("ABCD", 2)[0]) == 300 * 2 * 2 * 4 # a 5-minute round fits the Pico's 12288 edges


def test_loading_is_fast(board):
	# Each command's answer is read as soon as it comes: a 6-day session is
	# 12,000 commands, which at the read timeout (50 ms) each took 10 minutes
	port, log_path = board
	b = cc.Board(port, lambda line: None)
	try:
		b.Check()
		b.Command("clear")
		t = time.monotonic()
		for i in range(300):
			b.Command("edge 2 {} {}".format(i * 1000, i % 2))
		took = time.monotonic() - t
		b.Command("clear")
	finally:
		b.close()
	assert took < 5, "{:.1f} s for 300 commands".format(took) # 15 s at the timeout


# --- on the recording PC (Windows or Linux): finding the Pico, stop, attach ---

class Port:
	def __init__(self, device, vid, serial_number):
		self.device, self.vid, self.serial_number = device, vid, serial_number


def test_find_port_takes_the_one_pico_and_never_an_arduino(monkeypatch):
	# Off Linux (or with no by-id folder): the Pico by its USB IDs. campy's trigger
	# Arduino (a Nano ESP32, Arduino's IDs) may be on the same PC: never taken
	monkeypatch.setattr(sys, "platform", "win32")
	nano = Port("COM9", 0x2341, "ABC")
	pico = Port("COM7", cc.PICO_VID, "E665485457699D30")
	assert cc.FindPort(comports=[nano, pico]) == "COM7"
	with pytest.raises(SystemExit, match="No Pico"):
		cc.FindPort(comports=[nano])
	other = Port("COM4", cc.PICO_VID, "E6654854579F5F2F")
	with pytest.raises(SystemExit, match=r"2 Picos.*COM4 \(E6654854579F5F2F\).*COM7 \(E665485457699D30\)"):
		cc.FindPort(comports=[nano, pico, other])
	# By its USB serial number (any case), or a port named as such
	assert cc.FindPort("e665485457699d30", comports=[nano, pico, other]) == "COM7"
	assert cc.FindPort("COM9", comports=[nano, pico]) == "COM9"
	assert cc.FindPort("socket://127.0.0.1:5000", comports=[]) == "socket://127.0.0.1:5000"


def test_alive_doesnt_end_the_process_it_looks_at():
	# On Windows os.kill(pid, 0) would end it: Alive asks without doing so
	child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
	try:
		assert cc.Alive(child.pid)
		assert cc.Alive(child.pid) # still there after being looked at
		assert child.poll() is None
	finally:
		child.kill()
		child.wait()
	assert not cc.Alive(child.pid)
	assert not cc.Alive(None)


def test_a_stop_request_is_taken_once(tmp_path):
	assert cc.TakeStopRequest(str(tmp_path)) is None
	(tmp_path / cc.STOP_FILE).write_text("now")
	assert cc.TakeStopRequest(str(tmp_path)) == "now"
	assert cc.TakeStopRequest(str(tmp_path)) is None
	(tmp_path / cc.STOP_FILE).write_text("bout")
	assert cc.TakeStopRequest(str(tmp_path)) == "bout"


class FakeBoard:
	"""
	A board on a TCP socket (socket://127.0.0.1:<port>), so the end-to-end tests run
	where the sketch's simulator can't (Windows: no pseudo-terminal). It answers
	as chronic_timer.ino does and keeps the session time while nothing is connected,
	but reports only 'done' (with the pulses made), not each edge.
	"""

	def __init__(self):
		import socket, threading
		self.server = socket.socket()
		self.server.bind(("127.0.0.1", 0))
		self.server.listen(1)
		self.url = "socket://127.0.0.1:{}".format(self.server.getsockname()[1])
		self.lock = threading.Lock()
		self.conn = None
		self.trains, self.edges = [], []
		self.t0 = None
		self.end = None
		self.closed = False
		threading.Thread(target=self.Serve, daemon=True).start()
		threading.Thread(target=self.Clock, daemon=True).start()

	def Now(self):
		return int((time.monotonic() - self.t0) * 1e6)

	def Last(self):
		times = [t for t, _, _ in self.edges] + [cc.Rise(st, n - 1, rate) + w for _, st, n, rate, w in self.trains]
		return max(times)

	def Pulses(self, until):
		return sum(sum(1 for k in range(n) if cc.Rise(st, k, rate) < until) for _, st, n, rate, w in self.trains)

	def Say(self, line):
		try:
			if self.conn:
				self.conn.sendall((line + "\r\n").encode())
		except OSError:
			pass

	def Clock(self):
		while not self.closed:
			with self.lock:
				if self.t0 is not None and self.Now() >= self.end:
					self.Say("done {} pulses={} late_max=0 lost=0 stalls=0".format(self.end, self.Pulses(self.end)))
					self.t0 = None
			time.sleep(0.01)

	def Serve(self):
		while not self.closed:
			try:
				conn, _ = self.server.accept()
			except OSError:
				return
			self.conn = conn
			heard = b""
			while True:
				try:
					data = conn.recv(4096)
				except OSError:
					break
				if not data:
					break
				heard += data
				while b"\n" in heard:
					raw, heard = heard.split(b"\n", 1)
					with self.lock:
						self.Command(raw.decode().split())
			self.conn = None

	def Command(self, words):
		running = self.t0 is not None
		if words[0] == "id":
			self.Say("ok chronic_timer 2")
		elif words[0] == "status":
			self.Say("ok status running t={} pulses={} edges=0/{} trains=0/{} late_max=0".format(self.Now(),
				self.Pulses(self.Now()), len(self.edges), len(self.trains)) if running else
				"ok status idle edges={} trains={}".format(len(self.edges), len(self.trains)))
		elif words[0] == "pin":
			self.Say("ok pin {} {}".format(*words[1:]))
		elif words[0] == "clear":
			self.trains, self.edges = [], []
			self.Say("ok clear")
		elif words[0] == "edge":
			self.edges.append(tuple(int(w) for w in (words[2], words[1], words[3])))
			self.Say("ok edge {}".format(len(self.edges)))
		elif words[0] == "train":
			self.trains.append(tuple(int(w) for w in words[1:]))
			self.Say("ok train {}".format(len(self.trains)))
		elif words[0] == "go":
			self.t0 = time.monotonic() + 0.020
			self.end = self.Last()
			self.Say("ok go")
		elif words[0] in ("end", "stop"):
			if not running:
				self.Say("ok stop idle" if words[0] == "stop" else "err not running")
				return
			t = max(self.Now(), int(words[1]) if words[0] == "end" else 0)
			self.end = min(self.end, t)
			self.Say("ok {} {}".format(words[0], self.end))
		else:
			self.Say("err unknown")

	def close(self):
		self.closed = True
		self.server.close()


@pytest.fixture
def fake_board():
	board = FakeBoard()
	yield board.url
	board.close()


def test_start_status_and_stop_after_the_bout_on_any_computer(tmp_path, fake_board):
	# Start returns at once with the session running on (on Windows: a detached
	# process); stop asks it through the stop file. Stop has to come in bout 1,
	# after a second's wait and two Pythons starting (status, stop): in SHORT's
	# 3 s bout 1 it now and then didn't, on a busy Windows CI runner (the session
	# was over when stop looked). LONGER's bout 1 is 6 s, with the same pulses
	start = control(tmp_path, fake_board, "start", settings=LONGER)
	assert start.returncode == 0 and "Session started" in start.stdout, start.stdout + start.stderr
	assert "Session: running" in control(tmp_path, fake_board, "status", settings=LONGER).stdout
	time.sleep(1.0) # in bout 1
	stop = control(tmp_path, fake_board, "stop", settings=LONGER)
	assert stop.returncode == 0 and "ends after its current bout" in stop.stdout, stop.stdout
	assert wait_done(tmp_path)["state"] == "done"
	text = timestamps(tmp_path)
	assert "the session ends after bout 1, when its gate goes low at board t=6.000000 s" in text
	assert "Session over: 90 camera pulses\n" in text # bout 1's, by the board's count
	assert not (tmp_path / "state" / cc.STOP_FILE).exists()


def test_stop_now_on_any_computer(tmp_path, fake_board):
	# Stop --now ends the bout where it is: some of its camera pulses made, not all. In SHORT's bout 1, whose camera
	# runs 1.5 s (0.5 to 2 s), a stop 1.2 s in plus a Python starting came after it on a busy Windows CI runner (all 90
	# made). ONE_LONG_BOUT's camera runs 10 s
	assert control(tmp_path, fake_board, "start", settings=ONE_LONG_BOUT).returncode == 0
	time.sleep(1.2)
	stop = control(tmp_path, fake_board, "stop", "--now", settings=ONE_LONG_BOUT)
	assert stop.returncode == 0 and "Session stopped" in stop.stdout, stop.stdout
	n = int(re.search(r"Session over: (\d+) camera pulses", timestamps(tmp_path)).group(1))
	assert 20 < n < 600, n


def kill_session(tmp_path):
	# This computer restarting, as the session program sees it: gone at once, no clean-up
	pid = cc.ReadStatus(str(tmp_path / "state"))["pid"]
	if sys.platform == "win32":
		subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
	else:
		os.kill(pid, 9)
	deadline = time.monotonic() + 10
	while cc.Alive(pid) and time.monotonic() < deadline:
		time.sleep(0.05)
	assert not cc.Alive(pid)


def attach_after_restart(tmp_path, port):
	# Attach has to reach the board in bout 1 ("bout 1 of 2"), after a second's wait, the kill, and two Pythons
	# starting (status, attach). In SHORT's 3 s bout 1 it now and then didn't, on a busy CI runner (Linux, Python
	# 3.9): the board had run the whole session, and attach found it idle. LONGER's bout 1 is 6 s
	assert control(tmp_path, port, "start", settings=LONGER).returncode == 0
	time.sleep(1.0) # in bout 1
	kill_session(tmp_path)
	assert "gone" in control(tmp_path, port, "status").stdout
	command = [sys.executable, CONTROL, "attach", "--state-dir", str(tmp_path / "state")] # its port from the session
	attach = subprocess.run(command, capture_output=True, text=True, timeout=60)
	assert attach.returncode == 0 and "Attached" in attach.stdout and "bout 1 of 2" in attach.stdout, attach.stdout + attach.stderr
	assert wait_done(tmp_path)["state"] == "done"
	return timestamps(tmp_path)


def test_attach_follows_a_session_after_a_restart_on_any_computer(tmp_path, fake_board):
	text = attach_after_restart(tmp_path, fake_board)
	assert "=== ATTACHED to the session the board is running" in text
	assert "Session over: 150 camera pulses\n" in text # all of it: the board kept going


def test_attach_follows_the_sketch_after_a_restart(tmp_path, board):
	port, log_path = board
	text = attach_after_restart(tmp_path, port)
	assert "=== ATTACHED to the session the board is running" in text
	assert text.count("=== NEW SESSION") == 1 # the same log
	# After attaching, every edge from then on, with its plan label
	assert "Output A (Record Gate): HIGH (SpikeGLX File Recording Started)" in text.split("ATTACHED")[1] # bout 2
	assert "Session over: 150 camera pulses\n" in text
	assert sum(1 for _, level in pins(log_path)[3] if level == 1) == 150


def test_attach_with_nothing_running(tmp_path, fake_board):
	command = [sys.executable, CONTROL, "attach", "--state-dir", str(tmp_path / "state")]
	out = subprocess.run(command, capture_output=True, text=True, timeout=60)
	assert out.returncode == 1 and "nothing to attach" in out.stdout
	# A session that ended: the board is idle, and attach says so
	assert control(tmp_path, fake_board, "start", "--set", "TOTAL_DURATION=3.5").returncode == 0
	wait_done(tmp_path)
	out = subprocess.run(command, capture_output=True, text=True, timeout=60)
	assert out.returncode == 1 and "not running a session" in out.stdout, out.stdout


# --- the sync train (SYNC=1, output E on GP6) ---

def test_the_sync_settings():
	s = cc.Settings()
	assert s["SYNC"] == 0 and (s["SYNC_WIDTH_US"], s["SYNC_MIN_GAP_MS"], s["SYNC_MAX_GAP_MS"]) == (10000, 200, 1000)
	for bad in (["SYNC=2"], ["SYNC=1", "SYNC_WIDTH_US=200000"], ["SYNC=1", "SYNC_MIN_GAP_MS=900", "SYNC_MAX_GAP_MS=800"]):
		with pytest.raises(SystemExit):
			cc.Settings(bad)
	s = cc.Settings(["SYNC=1", "SYNC_SEED=42"])
	assert cc.SyncCommand(s) == "sync 4 42 10000 200 1000"
	assert "Sync train on output E (GP6): 10 ms pulses, a random 200-1000 ms from one to the next, seed 42" in \
		"\n".join(cc.Describe(s, cc.Plan(s)[0]))
	# The wiring check blinks E five times, only when asked
	edges, _ = cc.WiringPlan("E")
	assert sum(1 for t, ch, level in edges if level == 1) == 5 and {ch for _, ch, _ in edges} == {cc.SYNC_CH}
	assert cc.SYNC_CH not in {ch for _, ch, _ in cc.WiringPlan("ABCD")[0]}


def test_a_session_with_the_sync_train(tmp_path, board):
	# SYNC=1: the board makes the seed's pulses on GP6 from the session's start to its last gate, and the seed is saved
	# beside timestamps.txt for sync_match.py
	import json
	import sync_match as sm
	port, log_path = board
	settings = SHORT + ["SYNC=1", "SYNC_SEED=4242"]
	start = control(tmp_path, port, "start", settings=settings)
	assert start.returncode == 0, start.stdout + start.stderr
	assert wait_done(tmp_path)["state"] == "done"
	(day,) = (tmp_path / "logs").iterdir()
	(path,) = day.glob("sync_*.json")
	sync = json.loads(path.read_text())
	assert (sync["seed"], sync["width_us"], sync["min_gap_ms"], sync["max_gap_ms"], sync["pin"]) == (4242, 10000, 200,
		1000, "GP6")
	bouts, _, _ = cc.Plan(cc.Settings(settings))
	assert sync["ends_us"] == bouts[-1]["gate_off"] and sync["settings"]["SYNC_SEED"] == 4242
	want = sm.PlannedRises(4242, 200, 1000, bouts[-1]["gate_off"])
	assert "sync={}".format(len(want)) in sync["done"]
	text = timestamps(tmp_path)
	assert "Sync train on output E (GP6): 10000 us pulses, gaps of 200-1000 ms from seed 4242" in text
	p = pins(log_path)
	t0 = [t for t, level in p[2] if level == 1][0] # the gate rises at the session's time 0
	up = np.array([t for t, level in p[6] if level == 1]) - t0
	# The live sketch runs on the computer's clock, tens of us off: test_chronic_timer.py checks it to the microsecond
	assert len(up) == len(want) and np.abs(up - want).max() <= 2000
	assert p[6][-1][1] == 0


def test_sync_needs_chronic_timer_3(tmp_path, fake_board):
	# A board still on chronic_timer 2 has no channel 4: the session doesn't start, and says why
	start = control(tmp_path, fake_board, "start", settings=SHORT + ["SYNC=1"])
	assert start.returncode == 1
	assert "needs chronic_timer 3 or later on the board, which runs chronic_timer 2" in start.stdout, start.stdout
