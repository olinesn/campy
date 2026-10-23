"""campy/outputs_board/campy_outputs/campy_outputs.ino, compiled for this computer and run on a simulated clock.

The simulated clock (tests/arduino_sim) advances a microsecond each time the
sketch reads it, so an edge's time here is when the sketch wrote it, to the
microsecond. The camera trigger is a simulated input (SIM_INPUT) on pin 10,
the sketch's default: 60 Hz, 3 ms high, from 0.1 s. Channels 0-7 are on pins 2-9.
"""

import os, subprocess

import pytest

TRIG = 10
PERIOD = 16667
START = 100000


def trigger(count=60, start=START, period=PERIOD, high=3000, pin=TRIG):
	return "{}:{}:{}:{}:{}".format(pin, start, period, high, count)


def run(sim, end_s, commands, *later, inputs=None, start_us=0):
	"""
	Run the sketch: commands are waiting at power-up, later ones, (seconds,
	commands), arrive then. Returns each pin's changes [(us, level)] (power-up's
	lows left out) and what it said [(us, line)].
	"""
	env = dict(os.environ, SIM_START_US=str(start_us), SIM_INPUT=inputs if inputs is not None else trigger())
	argv = [sim, str(start_us + int(end_s * 1e6)), "\n".join(commands) + "\n"]
	argv += ["{}:{}".format(start_us + int(t * 1e6), "\n".join(c) + "\n") for t, c in sorted(later)] # in order
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
		t, pin, level = (int(x) for x in line.split())
		if t == start_us and level == 0 and not edges.get(pin):
			continue # power-up
		edges.setdefault(pin, []).append((t, level))
	return edges, said


def pulses(edges, pin):
	# [(rise, fall)] of a pin
	out, rise = [], None
	for t, level in edges.get(pin, []):
		if level:
			rise = t
		elif rise is not None:
			out.append((rise, t))
			rise = None
	return out


def lines(said, word):
	return [(t, line) for t, line in said if line.split()[0] == word]


def rise_of(k, start=START, period=PERIOD):
	return start + (k - 1) * period


HB = ["hb"]


def heartbeats(until_s, every_s=0.1):
	return [(i * every_s, HB) for i in range(1, int(until_s / every_s))]


def test_frames_are_counted_from_the_trigger(outputs_sim):
	edges, said = run(outputs_sim, 0.6, ["id"])
	assert said[0][1] == "ready campy_outputs 1"
	assert ("ok campy_outputs 1" in [line for _, line in said])
	rises = [(int(l.split()[1]), int(l.split()[2])) for _, l in lines(said, "t")]
	assert [k for k, _ in rises] == list(range(1, 31))
	for k, t in rises:
		assert 0 <= t - rise_of(k) <= 2, (k, t)


def test_interleaved_photometry_trains_are_exact_and_never_overlap(outputs_sim):
	# 465 nm on odd frames, 405 nm on even ones, 3 ms each, on the trigger's rise
	setup = ["group 0 1", "group 1 1", "train 0 2 1 0 3000", "train 1 2 0 0 3000", "run 0 -1", "run 1 -1"]
	edges, said = run(outputs_sim, 1.1, setup, *heartbeats(1.1))
	blue, violet = pulses(edges, 2), pulses(edges, 3)
	assert len(blue) == 30 and len(violet) == 30
	# The first two triggers come unexpected (the board learns the rate from
	# them), and are seen within a turn of its loop; then each to the microsecond
	for i, (r, f) in enumerate(blue):
		k = 2 * i + 1
		assert 0 <= r - rise_of(k) <= (10 if k <= 2 else 3), (k, r)
		assert f - r == 3000
	for i, (r, f) in enumerate(violet):
		k = 2 * i + 2
		assert 0 <= r - rise_of(k) <= (10 if k <= 2 else 3), (k, r)
		assert f - r == 3000
	both = sorted(blue + violet)
	assert all(a[1] <= b[0] for a, b in zip(both, both[1:])) # never on together
	reported = lines(said, "p")
	assert len(reported) == 60
	ch, k, rise, fall = (int(x) for x in reported[0][1].split()[1:])
	assert (ch, k, fall - rise) == (0, 1, 3000)


def test_a_train_with_an_offset_and_every_third_frame(outputs_sim):
	edges, said = run(outputs_sim, 0.6, ["train 2 3 1 1500 2000", "run 2 -1"], *heartbeats(0.6))
	got = pulses(edges, 4)
	assert len(got) == 10
	for i, (r, f) in enumerate(got):
		k = 1 + 3 * i
		assert r - (rise_of(k) + 1500) in (0, 1)
		assert f - r == 2000


def test_a_stopped_train_finishes_its_pulse(outputs_sim):
	# Stop half way through frame 5's pulse: that pulse still lasts 3 ms, then none
	stop_at = (rise_of(5) + 1500) / 1e6
	edges, said = run(outputs_sim, 0.6, ["train 0 1 0 0 3000", "run 0 -1"], (stop_at, ["run 0 0"]), *heartbeats(0.6))
	got = pulses(edges, 2)
	assert len(got) == 5
	assert all(f - r == 3000 for r, f in got)


def test_burst_makes_that_many_pulses(outputs_sim):
	# Asked 5 ms after frame 7's trigger: frames 8, 9 and 10
	edges, said = run(outputs_sim, 0.6, ["train 0 1 0 0 3000"], ((rise_of(7) + 5000) / 1e6, ["run 0 3"]),
		*heartbeats(0.6))
	got = pulses(edges, 2)
	assert len(got) == 3
	assert all(0 <= r - rise_of(8 + i) <= 3 and f - r == 3000 for i, (r, f) in enumerate(got))


def test_a_group_never_has_two_channels_high(outputs_sim):
	# A level on channel 1 between channel 0's pulses, in its group: the train's
	# pulses are skipped while it is on (said); a level asked for during a
	# pulse is refused. Never both high.
	setup = ["group 0 1", "group 1 1", "train 0 1 0 0 3000", "run 0 -1"]
	on, off = (rise_of(10) + 5000) / 1e6, (rise_of(15) + 5000) / 1e6
	during = (rise_of(20) + 1000) / 1e6
	edges, said = run(outputs_sim, 0.6, setup, (on, ["on 1"]), (off, ["off 1"]), (during, ["on 1"]),
		*heartbeats(0.6))
	train, level = pulses(edges, 2), pulses(edges, 3)
	assert len(level) == 1
	lo, hi = level[0]
	assert not any(r < hi and f > lo for r, f in train)
	assert [int(l.split()[2]) for _, l in lines(said, "clash")] == [11, 12, 13, 14, 15]
	assert [l.split()[:3] for _, l in said if l.startswith("err clash")] == [["err", "clash", "1"]]
	assert len(train) == 30 - 5


def test_a_command_too_long_after_its_frame_is_refused(outputs_sim):
	# Frame 5 rose at 166.7 ms; at 180 ms that is 13 ms after (allowed with a 20
	# ms bound), at 200 ms 33 ms after (refused, and the pin never moves)
	later = [(0.180, ["pulse 0 1000 5"]), (0.200, ["pulse 1 1000 5"])]
	edges, said = run(outputs_sim, 0.4, ["bound 20000"], *later, *heartbeats(0.4))
	ok = [l for _, l in said if l.startswith("ok pulse 0")]
	assert ok
	words = ok[0].split()
	t, k, after = int(words[3]), int(words[4]), int(words[5])
	assert k == 5 and after == t - rise_of(5)
	assert 13000 <= after <= 13400
	assert [l.split()[:3] for _, l in said if l.startswith("err late")] == [["err", "late", "1"]]
	assert pulses(edges, 3) == []
	assert pulses(edges, 2) and pulses(edges, 2)[0][1] - pulses(edges, 2)[0][0] == 1000


def test_the_watchdog_switches_everything_off(outputs_sim):
	# Nothing heard after 0.2 s: 250 ms later every output is low, the train stopped
	edges, said = run(outputs_sim, 1.0, ["train 0 1 0 0 3000", "run 0 -1", "on 2"], (0.2, ["hb"]))
	assert lines(said, "wd")
	wd_t = int(lines(said, "wd")[0][1].split()[1])
	assert 450000 <= wd_t <= 451000
	assert all(r < wd_t for r, _ in pulses(edges, 2))
	assert edges[4][-1][1] == 0 and edges[4][-1][0] <= wd_t + 2


def test_nothing_on_nothing_to_watch(outputs_sim):
	# The watchdog guards only outputs that are on: an idle board says nothing
	edges, said = run(outputs_sim, 0.8, ["id"])
	assert not lines(said, "wd")


def test_a_level_stays_on_at_most_its_max(outputs_sim):
	edges, said = run(outputs_sim, 0.5, ["max 0 100000", "on 0"], *heartbeats(0.5))
	got = pulses(edges, 2)
	assert len(got) == 1 and got[0][1] - got[0][0] in range(99990, 100010)
	assert any(l.startswith("e 0 0") and l.endswith("max") for _, l in said)


def test_counting_starts_again_after_a_pause(outputs_sim):
	# Two recordings: the second's triggers are frames 1, 2, ... again
	inputs = trigger(count=10) + ";" + trigger(count=5, start=START + 10 * PERIOD + 800000)
	edges, said = run(outputs_sim, 1.4, ["id"], inputs=inputs)
	ks = [int(l.split()[1]) for _, l in lines(said, "t")]
	assert ks == list(range(1, 11)) + list(range(1, 6))


def test_time_answers_with_the_last_frame(outputs_sim):
	edges, said = run(outputs_sim, 0.3, ["id"], (0.25, ["time"]))
	words = [l for _, l in said if l.startswith("ok time")][0].split()
	t, k, rise = int(words[2]), int(words[3]), int(words[4])
	assert 250000 <= t <= 250100 and k == 10 and rise - rise_of(10) in (0, 1)


def test_across_the_micros_wrap(outputs_sim):
	# micros() wraps at 2^32 us (71.6 minutes): times and pulses go on
	start = (1 << 32) - 300000
	inputs = trigger(start=start + START)
	edges, said = run(outputs_sim, 0.8, ["train 0 1 0 0 3000", "run 0 -1"],
		*[(t, c) for t, c in heartbeats(0.8)], inputs=inputs, start_us=start)
	got = pulses(edges, 2)
	assert len(got) >= 35
	assert all(f - r == 3000 for r, f in got)


def test_bad_commands_are_answered(outputs_sim):
	edges, said = run(outputs_sim, 0.05, ["nonsense", "pin 9 2", "train 0 2 2 0 3000", "run 0 -1", "pulse 0 0",
		"on 0 x"], inputs="")
	errs = [l for _, l in said if l.startswith("err")]
	assert len(errs) == 6
