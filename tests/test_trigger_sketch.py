"""campy/trigger/trigger.ino, compiled for this computer and run on a simulated clock."""

import shutil, subprocess

import numpy as np
import pytest

from campy.trigger import arduino

CXX = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
pytestmark = pytest.mark.skipif(CXX is None, reason="needs a C++ compiler")
# sim: the sketch, compiled (conftest.py)


def run(sim, end_s, command, *later, said=None):
	# Each pin's changes; and what the sketch said, into said: (microseconds, text)
	out = subprocess.run([sim, str(int(end_s * 1e6)), command] + ["{}:{}".format(int(t * 1e6), c) for t, c in later],
		capture_output=True, text=True, check=True, timeout=60).stdout
	edges = {}
	for line in out.splitlines():
		if line.startswith("serial "):
			_, t, text = line.split(" ", 2)
			if said is not None:
				said.append((int(t), text.replace("\\r", "\r").replace("\\n", "\n")))
			continue
		t, pin, level = map(int, line.split())
		edges.setdefault(pin, []).append((t, level))
	return edges


def text_after(said, t_s):
	return "".join(text for t, text in said if t >= t_s * 1e6)


def rises(edges, pin):
	return np.array([t for t, level in edges.get(pin, []) if level == 1], float)


def test_trigger_and_test_led(sim):
	edges = run(sim, 8, "2,2,3,25,13,43")
	for pin in (2, 3):
		up = rises(edges, pin)
		assert len(up) > 50
		assert np.all(np.abs(np.diff(up) - 40000) <= 3) # 25 fps, to a few microseconds
	assert np.max(np.abs(rises(edges, 2) - rises(edges, 3))) <= 2 # the pins rise together

	blink = edges[13]
	assert blink[0][1] == 0 # set low at power-up
	toggles = blink[1:]
	t = np.array([t for t, _ in toggles], float)
	half = round(5e5 / 43)
	assert np.all(np.abs(np.diff(t) - half) <= 3) # 43 Hz, 50% duty
	assert [level for _, level in toggles[:4]] == [1, 0, 1, 0]
	# It starts with the trigger, after the sketch's 4 s settling delay
	assert abs(t[0] - rises(edges, 2)[0]) < 40000


def test_a_command_without_the_test_led(sim):
	# What campy sent before the test LED existed: the sketch waits out its timeout, no LED
	edges = run(sim, 9, "2,2,3,25")
	assert set(edges) == {2, 3}
	assert np.all(np.abs(np.diff(rises(edges, 2)) - 40000) <= 3)


def test_stop_turns_both_off(sim):
	edges = run(sim, 14, "2,2,3,25,13,43", (7, "2,2,3,-1,-1,0"))
	assert rises(edges, 2).max() < 7.1e6
	# The test LED goes off once the command's last number is in: a second
	# later, when the timeout for one more digit is up
	last_t, last_level = edges[13][-1]
	assert last_level == 0 and 8e6 <= last_t < 8.1e6


def test_it_answers_campys_check(sim):
	# campy's check (arduino.OpenTriggers): the stop command, to a sketch that is triggering
	said = []
	edges = run(sim, 14, "2,2,3,25,13,43", (7, "2,2,3,-1,-1,0"), said=said)
	answer = [t for t, text in said if t >= 7e6 and text.startswith("0")]
	assert arduino.ANSWER.search(text_after(said, 7))
	assert "Frame rate set to: 0.00 fps." in text_after(said, 7)
	assert answer and answer[0] - 7e6 < 50000 # at once: within a trigger period
	assert rises(edges, 2).max() < 7.1e6


@pytest.mark.parametrize("after, starts", [(4.9, False), (4.99, False), (arduino.START_AFTER_CHECK_S, True)])
def test_a_command_too_soon_after_the_check_is_lost(sim, after, starts):
	# After the check's command the sketch waits a second for one more number,
	# then 4 s, then throws away what came meanwhile: campy's start command
	# goes out START_AFTER_CHECK_S after the check's
	said = []
	edges = run(sim, 7 + after + 6, "1,2,-1,-1,0", (7, "2,2,3,-1,-1,0"), (7 + after, "2,2,3,25,-1,0"), said=said)
	assert ("Frame rate set to: 25.00" in text_after(said, 7)) == starts
	assert (len(rises(edges, 2)) > 0) == starts


def Answered(said):
	# When the check's answer was complete (the sketch prints it in pieces), in seconds
	text = ""
	for t, piece in said:
		text += piece
		if arduino.ANSWER.search(text):
			return t / 1e6
	raise AssertionError("no answer: {!r}".format(text))


@pytest.mark.parametrize("after, starts", [(6.5, False), (6.9, False), (arduino.START_AFTER_CHECK_S, True)])
def test_the_start_reaches_an_older_campys_sketch(old_sim, after, starts):
	# The sketch an older campy flashed (campy_v03's) waits 6 s after a command, not 4, then throws away what came
	# meanwhile; and it reads the check's last two numbers (it has no test LED) as leftovers, the last after its 1 s
	# timeout. campy's start command, START_AFTER_CHECK_S after the check's answer, must come after all that, or the
	# Arduino never triggers (the DoublePoke rig, 2026-11-05). campy's check is the first command it gets (opening
	# the port restarts the board)
	said = []
	run(old_sim, 3, "1,2,-1,-1,0", said=said)
	answered = Answered(said)
	said = []
	edges = run(old_sim, answered + after + 6, "1,2,-1,-1,0", (answered + after, "1,2,25,-1,0"), said=said)
	assert arduino.ANSWER.search(text_after(said, 0)) # it answers campy's check
	assert ("Frame rate set to: 25.00" in text_after(said, answered + 1)) == starts
	assert (len(rises(edges, 2)) > 0) == starts


# ------------------------------------------------------------------ the sync output

from campy import syncmatch

SYNC = dict(seed=123456789, width_us=10000, min_gap_ms=200, max_gap_ms=1000)


def sync_command(fps, seed=SYNC["seed"], pins=(2, 3), blink=(13, 43), sync_pin=7, width=10000, gaps=(200, 1000)):
	return ",".join(str(x) for x in [len(pins), *pins, fps, *blink, sync_pin, seed, width, *gaps])


def time_zero(edges, fps):
	# The trigger's time 0 (the start of its first frame period): half a period before the first camera rise
	period = syncmatch.TriggerPeriodUs(fps)
	return rises(edges, 2)[0] - period // 2, period


@pytest.mark.parametrize("fps", [100, 25, 333])
def test_the_sync_output_follows_its_seed(sim, fps):
	said = []
	edges = run(sim, 40, sync_command(fps), said=said)
	assert "Sync output on pin 7, seed 123456789, 10000 us pulses, gaps 200 to 1000 ms." in text_after(said, 0)
	t0, period = time_zero(edges, fps)
	up = rises(edges, 7) - t0
	down = np.array([t for t, level in edges[7] if level == 0], float)[1:] - t0 # the first: set low at the command
	want_up, want_down = syncmatch.SyncEdges(dict(SYNC, frame_period_us=period), up[-1] + 1)
	assert len(up) >= 30 and len(up) == len(want_up)
	# Each edge where campy-syncmatch says it is, to the loop's few microseconds
	assert np.abs(up - want_up).max() <= 4
	assert np.abs(down[:len(want_down)] - want_down[:len(down)]).max() <= 4
	# The cameras: every frame period as without the sync output (test_trigger_and_test_led), and the pins together
	for pin in (2, 3):
		assert np.all(np.abs(np.diff(rises(edges, pin)) - period) <= 3)
	assert np.max(np.abs(rises(edges, 2) - rises(edges, 3))) <= 2


def test_camera_edges_stay_put_with_the_sync_output(sim):
	# The same trigger with and without the sync output: every camera edge at the same time to within the loop's
	# step, the sync edges near camera edges held clear of them
	fps = 100
	with_sync = run(sim, 30, sync_command(fps))
	without = run(sim, 30, ",".join(str(x) for x in [2, 2, 3, fps, 13, 43]))
	for pin in (2, 3):
		a, b = np.array(with_sync[pin], float), np.array(without[pin], float)
		n = min(len(a), len(b))
		assert n > 2000 and np.array_equal(a[:n, 1], b[:n, 1])
		assert np.abs(a[:n, 0] - b[:n, 0]).max() <= 3
	# No sync edge within the guard before a camera edge, and many that would have been without the hold
	t0, period = time_zero(with_sync, fps)
	sync_t = np.array([t for t, _ in with_sync[7][1:]], float)
	camera_t = np.array([t for t, _ in with_sync[2]], float)
	k = np.searchsorted(camera_t, sync_t)
	ahead = camera_t[np.minimum(k, len(camera_t) - 1)] - sync_t
	assert np.all(ahead[k < len(camera_t)] > syncmatch.HOLD_GUARD_US - 5)
	planned = syncmatch.PlannedRises(SYNC["seed"], 200, 1000, sync_t[-1] - t0)
	assert (syncmatch.Hold(planned, period) > 0).sum() > 5


def test_a_command_without_the_sync_output_leaves_it_off(sim):
	# campy before the sync output: nothing after the test LED's Hz, and no extra wait for numbers that never come
	said = []
	edges = run(sim, 9, "2,2,3,25,13,43", said=said)
	assert 7 not in edges
	assert "Sync output off." in text_after(said, 0)
	# 0.5 s at power-up, 1 s for one more digit, 4 s of the sketch's wait, then half a period: as before it
	assert rises(edges, 2)[0] <= 5.5e6 + 20000 + 10


@pytest.mark.parametrize("command, why", [
	(sync_command(25, sync_pin=3), "pin triggers a camera"),
	(sync_command(25, sync_pin=13), "pin is the test LED's"),
	(sync_command(25, width=250000), "pulse too short, or too long"),
	(sync_command(25, gaps=(500, 200)), "gaps must be 1-60000 ms"),
	(sync_command(-1), "no trigger"),
])
def test_the_sketch_refuses_a_sync_output_it_cannot_make(sim, command, why):
	said = []
	edges = run(sim, 8, command, said=said)
	assert "Sync output off: " + why in text_after(said, 0)
	assert len([t for t, level in edges.get(7, []) if level == 1]) == 0


def test_stop_turns_the_sync_output_off(sim):
	edges = run(sim, 16, sync_command(100), (9, "2,2,3,-1,-1,0"))
	assert rises(edges, 7).max() < 9.1e6
	assert edges[7][-1][1] == 0


def test_the_sync_output_across_micros_wrap(sim):
	# micros() wraps every 71.6 minutes on the boards (32 bits): the sync edges' times wrap with it. The simulated
	# clock is 64 bits, so start it just before 2**32 and check that nothing jumps there
	import os
	start = 2 ** 32 - 8_000_000
	env = dict(os.environ, SIM_START_US=str(start))
	out = subprocess.run([sim, str(start + 20_000_000), sync_command(100)], capture_output=True, text=True,
		check=True, timeout=60, env=env).stdout
	edges = {}
	for line in out.splitlines():
		if not line.startswith("serial "):
			t, pin, level = map(int, line.split())
			edges.setdefault(pin, []).append((t, level))
	t0, period = time_zero(edges, 100)
	up = rises(edges, 7) - t0
	want, _ = syncmatch.SyncEdges(dict(SYNC, frame_period_us=period), up[-1] + 1)
	assert len(up) == len(want) >= 10 and np.abs(up - want).max() <= 4


# ------------------------------------------------------------------ upstream campy (github.com/ksseverson57/campy)

def test_upstream_campys_commands_drive_this_sketch(sim, upstream_sim):
	# Upstream campy's arduino.py sends pin count, pins and frame rate, nothing more, and stops with frame rate -1.
	# This sketch triggers the same pins at the same rate, from the same moment as upstream's own sketch, and stops
	# at once; no other pin moves
	for board in (sim, upstream_sim):
		edges = run(board, 20, "2,2,3,25", (12, "2,2,3,-1"))
		assert set(edges) == {2, 3}
		for pin in (2, 3):
			up = rises(edges, pin)
			assert np.all(np.abs(np.diff(up) - 40000) <= 3)
			assert 5.5e6 <= up[0] <= 5.5e6 + 20000 + 10 # 0.5 s at power-up, 1 s for one more digit, 4 s, half a period
			assert up.max() < 12.05e6
		assert edges[2][-1][1] == 0
	assert np.array_equal(rises(run(sim, 20, "2,2,3,25", (12, "2,2,3,-1")), 2),
		rises(run(upstream_sim, 20, "2,2,3,25", (12, "2,2,3,-1")), 2))


def test_this_campy_drives_upstreams_sketch(upstream_sim):
	# This campy's check, start (with the test LED and the sync output, which that sketch hasn't got) and stop
	said = []
	edges = run(upstream_sim, 30, "2,2,3,-1,-1,0", (9, "2,2,3,25,13,43,7,42,10000,200,1000"), (20, "2,2,3,-1,-1,0"),
		said=said)
	assert arduino.ANSWER.search(text_after(said, 0))
	assert "Test LED" not in text_after(said, 0) and "Sync output" not in text_after(said, 0) # how campy tells
	assert set(edges) == {2, 3}
	up = rises(edges, 2)
	assert len(up) > 100 and np.all(np.abs(np.diff(up) - 40000) <= 3) and up.max() < 20.05e6
