"""chronicrecord/chronic_control.py: the Pi's side of the ChronicRecord timer.

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


@pytest.fixture
def board(tmp_path, timer_sim):
	# The Nano: chronic_timer.ino live on a pty's master end; its log has every pin change
	if sys.platform == "win32":
		pytest.skip("needs a pseudo-terminal")
	master, slave = pty()
	log_path = tmp_path / "board.log"
	with open(str(log_path), "w") as log:
		proc = subprocess.Popen([timer_sim, "live"], stdin=master, stdout=master, stderr=log)
	time.sleep(0.3)
	try:
		yield os.ttyname(slave), log_path
	finally:
		proc.terminate()
		proc.wait()
		os.close(slave)
		os.close(master)


def control(tmp_path, port, *args):
	command = [sys.executable, CONTROL] + list(args) + ["--port", port, "--state-dir", str(tmp_path / "state"),
		"--log-dir", str(tmp_path / "logs")] + sum((["--set", s] for s in SHORT), [])
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
