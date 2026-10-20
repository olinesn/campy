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
