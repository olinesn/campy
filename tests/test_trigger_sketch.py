"""campy/trigger/trigger.ino, compiled for this computer and run on a simulated clock."""

import os, shutil, subprocess

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SKETCH = os.path.join(os.path.dirname(HERE), "campy", "trigger", "trigger.ino")
CXX = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
pytestmark = pytest.mark.skipif(CXX is None, reason="needs a C++ compiler")


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
	exe = str(tmp_path_factory.mktemp("sketch") / "sim")
	sim_dir = os.path.join(HERE, "arduino_sim")
	subprocess.run([CXX, "-std=c++11", "-O2", "-I", sim_dir, "-include", "Arduino.h", "-x", "c++", SKETCH,
		"-x", "none", os.path.join(sim_dir, "sim.cpp"), "-o", exe], check=True)
	return exe


def run(sim, end_s, command, *later):
	out = subprocess.run([sim, str(int(end_s * 1e6)), command] + ["{}:{}".format(int(t * 1e6), c) for t, c in later],
		capture_output=True, text=True, check=True, timeout=60).stdout
	edges = {}
	for t, pin, level in (map(int, line.split()) for line in out.splitlines()):
		edges.setdefault(pin, []).append((t, level))
	return edges


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
	last_t, last_level = edges[13][-1]
	assert last_level == 0 and last_t < 7.1e6
