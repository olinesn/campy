"""chronicrecord/chronic_timer/chronic_timer.ino, compiled for this computer and run on a simulated clock.

The simulated clock (tests/arduino_sim) advances a microsecond each time the
sketch reads it, so an edge's time here is when the sketch wrote it, to the
microsecond. Pins: channel 0 (gate) on 2, 1 (camera) on 3, 2 (LED1) on 4, 3 (LED2) on 5.
"""

import os, subprocess, sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chronicrecord"))
import chronic_control as cc

PIN = {0: 2, 1: 3, 2: 4, 3: 5}
GO_DELAY_US = 20000


def run(timer_sim, end_s, commands, *later, start_us=0):
	"""
	Run the sketch: commands are waiting at power-up, later ones, (seconds,
	commands), arrive then. Returns each pin's changes [(us, level)] and what
	it said [(us, line)].
	"""
	env = dict(os.environ, SIM_START_US=str(start_us))
	argv = [timer_sim, str(start_us + int(end_s * 1e6)), "\n".join(commands) + "\n"]
	argv += ["{}:{}".format(start_us + int(t * 1e6), "\n".join(c) + "\n") for t, c in later]
	out = subprocess.run(argv, capture_output=True, text=True, check=True, timeout=120, env=env).stdout
	edges, said, text, text_t = {}, [], "", None
	for line in out.splitlines():
		if line.startswith("serial "):
			_, t, chunk = line.split(" ", 2)
			text_t = int(t) if text_t is None else text_t
			text += chunk
			while "\\r\\n" in text:
				first, text = text.split("\\r\\n", 1)
				said.append((text_t, first))
				text_t = None if not text else int(t)
			continue
		t, pin, level = map(int, line.split())
		edges.setdefault(pin, []).append((t, level))
	return edges, said


def went_go(said):
	# Session time 0, on the simulated clock: GO_DELAY_US after the sketch said "ok go"
	t = [t for t, line in said if line == "ok go"]
	assert len(t) == 1, said
	return t[0] + GO_DELAY_US


def changes(edges, pin, after=0):
	# A pin's changes after power-up's "low"
	return [(t, level) for t, level in edges.get(pin, []) if t >= after]


def rises(edges, pin):
	return np.array([t for t, level in edges.get(pin, []) if level == 1], np.int64)


def falls(edges, pin):
	return np.array([t for t, level in edges.get(pin, []) if level == 0], np.int64)[1:] # not power-up's


def test_it_answers_at_once_and_powers_up_low(timer_sim):
	edges, said = run(timer_sim, 0.1, ["id", "status"])
	assert said[0][1] == "ready chronic_timer 1"
	assert [line for _, line in said[1:]] == ["ok chronic_timer 1", "ok status idle edges=0 trains=0"]
	assert said[1][0] < 1000 # microseconds: no timeouts to wait out
	for pin in PIN.values():
		assert edges[pin] == [(edges[pin][0][0], 0)]


def test_a_camera_train_is_exact(timer_sim):
	# 60 Hz, 600 pulses of 3000 us from 0.5 s: every rise on floor(k * 1e6 / 60)
	edges, said = run(timer_sim, 11.5, ["train 1 500000 600 60000 3000", "go"])
	t0 = went_go(said)
	up, down = rises(edges, 3), falls(edges, 3)
	assert len(up) == 600 and len(down) == 600 # exactly the count
	want = t0 + 500000 + np.arange(600) * 10**9 // 60000
	assert np.abs(up - want).max() <= 2
	assert np.abs(down - up - 3000).max() <= 2
	# Over the whole train, the rate is 60 Hz to the microsecond (16666/16667 us periods)
	assert set(np.diff(up)) <= {16665, 16666, 16667, 16668}
	assert up[-1] - up[0] == 599 * 10**9 // 60000
	cam = [line for _, line in said if line.startswith("cam ")]
	assert cam == ["cam 0 600 500000 {}".format(500000 + 599 * 10**9 // 60000 + 3000)]
	assert [line for _, line in said if line.startswith("camon ")] == ["camon 0 500000"]
	done = [line for _, line in said if line.startswith("done ")]
	assert len(done) == 1 and "pulses=600" in done[0] and "late_max=0" in done[0]


def test_led_edges_land_on_their_pulse(timer_sim):
	# The LEDs 1 ms before pulse 300's rise, 30 ms each, the second as the first goes off
	rise = lambda k: 500000 + k * 10**9 // 60000
	on1 = rise(300) - 1000
	commands = ["train 1 500000 600 60000 3000", "edge 0 0 1", "edge 2 {} 1".format(on1), "edge 2 {} 0".format(on1 + 30000),
		"edge 3 {} 1".format(on1 + 30000), "edge 3 {} 0".format(on1 + 60000), "edge 0 {} 0".format(rise(600) + 100000), "go"]
	edges, said = run(timer_sim, 11.5, commands)
	t0 = went_go(said)
	assert changes(edges, 4, 1) == [(t0 + on1, 1), (t0 + on1 + 30000, 0)]
	assert changes(edges, 5, 1) == [(t0 + on1 + 30000, 1), (t0 + on1 + 60000, 0)]
	assert changes(edges, 2, 1) == [(t0, 1), (t0 + rise(600) + 100000, 0)]
	# LED1 lights the exposures of pulses 300 and 301, LED2 302 and 303, whole
	up = rises(edges, 3) - t0
	lit1 = [k for k, r in enumerate(up) if on1 <= r and r + 200 <= on1 + 30000]
	lit2 = [k for k, r in enumerate(up) if on1 + 30000 <= r and r + 200 <= on1 + 60000]
	assert lit1 == [300, 301] and lit2 == [302, 303]
	# The board reports each edge, on time
	ev = [line.split() for _, line in said if line.startswith("ev ")]
	assert [(int(t), int(ch), int(level), int(late)) for _, t, ch, level, late in ev] == [
		(0, 0, 1, 0), (on1, 2, 1, 0), (on1 + 30000, 2, 0, 0), (on1 + 30000, 3, 1, 0), (on1 + 60000, 3, 0, 0),
		(rise(600) + 100000, 0, 0, 0)]


def test_changes_due_together_are_written_together(timer_sim):
	# An LED edge on a camera rise, and two LEDs switching over, at the same microsecond
	r = 500000 + 10 * 10**9 // 60000
	commands = ["train 1 500000 20 60000 3000", "edge 2 {} 1".format(r), "edge 2 {} 0".format(r + 5000),
		"edge 3 {} 1".format(r + 5000), "edge 3 {} 0".format(r + 9000), "go"]
	edges, said = run(timer_sim, 1.5, commands)
	t0 = went_go(said)
	assert t0 + r in rises(edges, 3)
	assert changes(edges, 4, 1)[0] == (t0 + r, 1)
	assert changes(edges, 4, 1)[1] == (t0 + r + 5000, 0) and changes(edges, 5, 1)[0] == (t0 + r + 5000, 1)


def test_serial_traffic_does_not_move_an_edge(timer_sim):
	# The Pi asking for status ten times a second, through a whole train
	later = [(0.6 + 0.1 * i, ["status"]) for i in range(80)]
	edges, said = run(timer_sim, 10, ["train 1 500000 400 60000 3000", "go"], *later)
	t0 = went_go(said)
	want = t0 + 500000 + np.arange(400) * 10**9 // 60000
	assert np.abs(rises(edges, 3) - want).max() <= 2
	assert np.abs(falls(edges, 3) - want - 3000).max() <= 2
	answers = [line for _, line in said if line.startswith("ok status running")]
	assert len(answers) >= 60


def test_end_after_a_bout_keeps_its_last_edge(timer_sim):
	# "end" at the first bout's gate-low: that edge still happens, on time; nothing after it does
	commands = ["train 1 100000 30 60000 3000", "edge 0 0 1", "edge 0 800000 0", "edge 0 2000000 1",
		"train 1 2100000 30 60000 3000", "edge 0 2800000 0", "go"]
	edges, said = run(timer_sim, 4, commands, (0.3, ["end 800000"]))
	t0 = went_go(said)
	assert changes(edges, 2, 1) == [(t0, 1), (t0 + 800000, 0)]
	assert len(rises(edges, 3)) == 30 # the first train, whole; not the second
	assert "ok end 800000" in [line for _, line in said]
	assert [line for _, line in said if line.startswith("cam ")][1] == "cam 1 0 -1 -1"
	assert any(line.startswith("done ") and "pulses=30" in line for _, line in said)


def test_stop_ends_on_a_whole_pulse_then_everything_low(timer_sim):
	# Stop while a camera pulse is high and an LED is on: the pulse keeps its
	# full width, the LED and gate go low with it, no pulse after
	r = lambda k: 100000 + k * 10**9 // 60000
	commands = ["train 1 100000 600 60000 3000", "edge 0 0 1", "edge 2 {} 1".format(r(10)), "edge 2 {} 0".format(r(200)), "go"]
	t_stop = r(50) + 1000 # 1 ms into pulse 50's 3 ms, after go's 20 ms
	edges, said = run(timer_sim, 3, commands, (GO_DELAY_US / 1e6 + t_stop / 1e6, ["stop"]))
	t0 = went_go(said)
	up, down = rises(edges, 3) - t0, falls(edges, 3) - t0
	n = len(up)
	assert len(down) == n and np.abs(down - up - 3000).max() <= 2 # every pulse whole
	assert np.abs(up - (100000 + np.arange(n) * 10**9 // 60000)).max() <= 2
	assert 49 <= n <= 52
	end = falls(edges, 3)[-1]
	assert changes(edges, 4, 1)[-1][1] == 0 and changes(edges, 2, 1)[-1][1] == 0
	assert changes(edges, 4, 1)[-1][0] <= end + 5 and changes(edges, 2, 1)[-1][0] <= end + 5
	assert any(line.startswith("done ") and "pulses={}".format(n) in line for _, line in said)


def test_it_refuses_what_it_cannot_do(timer_sim):
	commands = ["edge 2 1000 1", "edge 2 500 0", "edge 9 1 1", "train 1 0 10 60000 20000", "train 1 0 10 60000 3000",
		"train 1 100000 10 60000 3000", "edge 1 2000 1", "go", "clear", "go", "nonsense", "edge 2 x 1"]
	edges, said = run(timer_sim, 0.5, commands)
	replies = [line for _, line in said[1:]]
	assert replies == ["ok edge 1", "err edges must come in order of time", "err edge <ch> <t_us> <0|1>",
		"err width must be under the period", "ok train 1", "err trains must come in order, apart", "ok edge 2",
		"err an edge is on a train's channel", "ok clear", "err nothing to run", "err unknown command", "err not a number"]


def test_it_takes_no_new_schedule_while_running(timer_sim):
	edges, said = run(timer_sim, 1.5, ["train 1 0 30 60000 3000", "go"], (0.2, ["edge 2 900000 1", "clear", "status"]))
	replies = [line for t, line in said if t >= 0.2e6]
	assert replies[:2] == ["err running: stop first", "err running: stop first"]
	assert replies[2].startswith("ok status running")
	assert 4 not in edges or len(edges[4]) == 1


def test_it_keeps_time_across_micros_wrap(timer_sim):
	# micros() is 32 bits: it wraps after 71.6 minutes. A train across the wrap stays exact.
	start = 2**32 - 1000000
	edges, said = run(timer_sim, 3, ["train 1 500000 120 60000 3000", "go"], start_us=start)
	t0 = went_go(said)
	assert t0 < 2**32 < t0 + 500000 + 120 * 10**9 // 60000
	want = t0 + 500000 + np.arange(120) * 10**9 // 60000
	assert len(rises(edges, 3)) == 120 and np.abs(rises(edges, 3) - want).max() <= 2


def test_a_standard_bout_from_the_pi_plan(timer_sim):
	# chronic_control.py's plan for one standard bout, loaded as it loads it: every
	# edge within 2 us of the plan, the camera exactly 600 pulses
	s = cc.Settings(["TOTAL_DURATION=300", "EXTENDED_STIM_DURATION=10"]) # one bout, standard length
	bouts, trains, edges = cc.Plan(s)
	commands = ["pin {} {}".format(ch, pin) for ch, (_, pin, _) in enumerate(cc.OUTPUTS)] + ["clear"]
	commands += ["train {} {} {} {} {}".format(*t) for t in trains] + ["edge {} {} {}".format(ch, t, level) for t, ch, level in edges]
	commands += ["go"]
	# Gate high at 0, camera 5-15 s, gate low 10 s after the camera
	assert bouts[0]["pulses"] == 600 and bouts[0]["gate_off"] == 25000000
	got, said = run(timer_sim, 26, commands)
	t0 = went_go(said)
	for t, ch, level in edges:
		pin = cc.OUTPUTS[ch][1]
		assert min(abs(tt - (t0 + t)) for tt, lv in changes(got, pin, 1) if lv == level) <= 2, (t, ch, level)
	up = rises(got, cc.OUTPUTS[cc.CAMERA][1]) - t0
	assert len(up) == 600
	assert np.abs(up - (bouts[0]["camera_start"] + np.arange(600) * 10**9 // 60000)).max() <= 2
	blue, _ = cc.LitFrames(bouts[0], *bouts[0]["blue"], s)
	yellow, _ = cc.LitFrames(bouts[0], *bouts[0]["yellow"], s)
	assert blue == [300, 301] and yellow == [302, 303]
	assert any(line.startswith("done ") and "pulses=600" in line for _, line in said)
