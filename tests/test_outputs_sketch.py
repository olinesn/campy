"""campy/outputs_board/campy_outputs/campy_outputs.ino, compiled for this computer and run on a simulated clock.

The simulated clock (tests/arduino_sim) advances a microsecond each time the
sketch reads it, so an edge's time here is when the sketch wrote it, to the
microsecond. The camera trigger is a simulated input (SIM_INPUT) on pin 10,
the sketch's default: 60 Hz, 3 ms high, from 0.1 s. Channels 0-7 are on pins 2-9. The serial port's
transmit buffer is 64 bytes, emptied in 100 us, as a Nano ESP32's USB (SIM_USB_TX), unless a test says otherwise.
"""

import os, subprocess

import pytest

TRIG = 10
PERIOD = 16667
START = 100000
USB = "64:100"  # the Nano ESP32's USB serial transmit buffer: 64 bytes


def trigger(count=60, start=START, period=PERIOD, high=3000, pin=TRIG):
	return "{}:{}:{}:{}:{}".format(pin, start, period, high, count)


def run(sim, end_s, commands, *later, inputs=None, start_us=0, usb=USB):
	"""
	Run the sketch: commands are waiting at power-up, later ones, (seconds,
	commands), arrive then. Returns each pin's changes [(us, level)] (power-up's
	lows left out; a servo's PWM under ("pwm", pin): [(us, duty)], -1 once stopped) and what it said [(us, line)].
	"""
	env = dict(os.environ, SIM_START_US=str(start_us), SIM_INPUT=inputs if inputs is not None else trigger(),
		SIM_USB_TX=usb)
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
		if line.startswith("pwm "):
			_, t, pin, duty, _freq, _bits = line.split()
			edges.setdefault(("pwm", int(pin)), []).append((int(t), int(duty)))
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
	assert said[0][1] == "ready campy_outputs 3"
	assert ("ok campy_outputs 3" in [line for _, line in said])
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


def test_counting_goes_on_through_a_pause(outputs_sim):
	# A pause of 0.8 s (a planned pause between bouts): frames 10, 11, ... go on, as campy's do, and the pause is said
	inputs = trigger(count=9) + ";" + trigger(count=5, start=START + 9 * PERIOD + 800000)
	edges, said = run(outputs_sim, 1.4, ["id"], inputs=inputs)
	ks = [int(l.split()[1]) for _, l in lines(said, "t")]
	assert ks == list(range(1, 15))
	gaps = [l.split()[1:] for _, l in lines(said, "gap")]
	assert len(gaps) == 1 and gaps[0][0] == "10"
	assert abs(int(gaps[0][1]) - (PERIOD + 800000)) <= 2


def test_a_train_keeps_its_frames_through_a_pause(outputs_sim):
	# every 2, phase 1: odd frames. The pause comes after frame 9 (odd), so a count that started again would put
	# the next pulses on the 10th, 12th and 14th triggers; they stay on frames 11 and 13
	second = START + 9 * PERIOD + 800000
	inputs = trigger(count=9) + ";" + trigger(count=5, start=second)
	edges, said = run(outputs_sim, 1.4, ["train 0 2 1 0 3000", "run 0 -1"], *heartbeats(1.4), inputs=inputs)
	rises = [r for r, _ in pulses(edges, 2)]
	assert len(rises) == 5 + 2
	after = [r for r in rises if r >= second]
	assert len(after) == 2
	for r, k in zip(after, (11, 13)):
		assert 0 <= r - (second + (k - 10) * PERIOD) <= 10, (k, r)
	assert [int(l.split()[2]) for _, l in lines(said, "p")][-2:] == [11, 13]


def test_reset_counts_from_1_again(outputs_sim):
	edges, said = run(outputs_sim, 0.6, ["id"], ((rise_of(10) + 5000) / 1e6, ["reset"]))
	assert any(l.startswith("ok reset") for _, l in said)
	ks = [int(l.split()[1]) for _, l in lines(said, "t")]
	assert ks == list(range(1, 11)) + list(range(1, 21))
	assert not lines(said, "gap")


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


# ---------------------------------------------------------------- the 64-byte USB buffer (a real Nano ESP32's)

def status(said):
	line = [l for _, l in said if l.startswith("ok status")][-1]
	return dict(kv.split("=") for kv in line.split()[2:])


def test_status_comes_whole_through_a_64_byte_usb_buffer(outputs_sim):
	# About 110 characters: more than the Nano ESP32's USB takes at once. It was dropped, every time, on the real
	# board (13 November); now it goes out in pieces as USB takes them
	edges, said = run(outputs_sim, 0.3, ["id"], (0.25, ["status"]))
	st = status(said)
	assert set(st) == {"t", "frame", "period", "high", "running", "clashes", "lates", "wds", "late_max", "lost"}
	assert st["frame"] == "10" and st["lost"] == "0"


def test_answers_back_to_back_all_come_in_order(outputs_sim):
	# Seven commands at once: their answers, about 150 bytes together, come out within microseconds of each other
	commands = ["id", "time", "watchdog 300", "bound 40000", "time", "max 0 0", "id"]
	edges, said = run(outputs_sim, 0.05, commands, inputs="")
	answers = [l.split()[1] for _, l in said if l.startswith("ok ")]
	assert answers == ["campy_outputs", "time", "watchdog", "bound", "time", "max", "campy_outputs"]


def test_a_burst_of_pulse_reports_is_not_lost(outputs_sim):
	# Four trains on every frame: each trigger brings a "t" line, and 3 ms later four "p" lines at once (about
	# 150 bytes), more than USB takes at once. Each pulse is reported, and nothing is counted lost
	commands = ["train {} 1 0 0 3000".format(ch) for ch in range(4)] + ["run {} -1".format(ch) for ch in range(4)]
	edges, said = run(outputs_sim, 0.6, commands, *heartbeats(0.6), (0.55, ["status"]))
	for ch in range(4):
		made = pulses(edges, 2 + ch)
		reported = [l for _, l in lines(said, "p") if l.split()[1] == str(ch)]
		assert len(made) >= 25 and len(reported) == len(made), (ch, len(made), len(reported))
	assert [int(l.split()[1]) for _, l in lines(said, "t")] == list(range(1, len(lines(said, "t")) + 1))
	assert status(said)["lost"] == "0"


def test_without_the_usb_limit_nothing_changes(outputs_sim):
	# The same burst with no transmit buffer limit, as before this test simulated one: the same pulses and reports
	commands = ["train 0 1 0 0 3000", "run 0 -1"]
	limited = run(outputs_sim, 0.4, commands, *heartbeats(0.4))
	free = run(outputs_sim, 0.4, commands, *heartbeats(0.4), usb="")
	assert pulses(limited[0], 2) == pulses(free[0], 2)
	assert [l for _, l in limited[1]] == [l for _, l in free[1]]


# ---------------------------------------------------------------- an operant box (version 3): inputs, servos, links

A = {i: 30 + i for i in range(8)} # A0-A7 on the simulated board (tests/arduino_sim/Arduino.h); "100 + i" in commands
SERVO_PIN = 11


def pulse(pin, start, high):
	# One pulse on an input pin (inverted on a pull-up pin: the sensor pulls it low)
	return "{}:{}:{}:{}:1".format(pin, int(start), int(high) + 1, int(high))


def inputs_with(*pulses_):
	return ";".join([trigger()] + list(pulses_))


def said_inputs(said, i=None):
	# [(in, level, t, k, after)]
	out = [tuple(int(x) for x in l.split()[1:]) for _, l in lines(said, "i")]
	return [o for o in out if i is None or o[0] == i]


def duty(us):
	return int((us * (1 << 14) + 10000) // 20000)


def test_an_input_edge_is_stamped_to_the_microsecond_with_its_frame(outputs_sim):
	# A nose poke on A0 (pull-down, active high) 4 ms after frame 12's trigger, out 50 ms later; another just
	# before frame 20's trigger, while the board watches for it with interrupts off; a third while a train's pulse
	# is due. Each edge's time is when the pin changed, its frame the one it fell in.
	edges_at = [rise_of(12) + 4000, rise_of(20) - 120, rise_of(25) + 2999]
	ins = inputs_with(*[pulse(A[0], t, 50000 if n == 0 else 1000) for n, t in enumerate(edges_at)])
	setup = ["in 0 100 2 1 0", "train 0 1 0 0 3000", "run 0 -1"]
	edges, said = run(outputs_sim, 0.6, setup, *heartbeats(0.6), inputs=ins)
	assert [l for _, l in said if l.startswith("ok in")] == ["ok in 0 30 0"]
	got = said_inputs(said, 0)
	assert [g[1] for g in got] == [1, 0, 1, 0, 1, 0]
	for (i, level, t, k, after), want in zip(got[::2], edges_at):
		assert 0 <= t - want <= 3, (t, want)
		assert rise_of(k) <= t < rise_of(k + 1)
		assert after == t - rise_of(k) or abs(after - (t - rise_of(k))) <= 2
	assert got[0][3] == 12 and got[2][3] == 19 and got[4][3] == 25
	assert 0 <= got[1][2] - (edges_at[0] + 50000) <= 3
	# and the train is as exact as without an input
	for n, (r, f) in enumerate(pulses(edges, 2)[2:], start=3):
		assert 0 <= r - rise_of(n) <= 3 and f - r == 3000, (n, r)


def test_a_beam_break_on_a_pull_up_pin_is_active_low(outputs_sim):
	# An open-collector beam break pulls the pin low while the beam is broken: active 0, reported as 1
	t0 = 0.3e6
	ins = inputs_with(pulse(A[1], t0, 20000))
	edges, said = run(outputs_sim, 0.4, ["in 1 101 1 0 1000"], inputs=ins)
	assert [l for _, l in said if l.startswith("ok in")] == ["ok in 1 31 0"] # beam whole at the start
	got = said_inputs(said, 1)
	assert [g[1] for g in got] == [1, 0]
	assert 0 <= got[0][2] - t0 <= 3 and 0 <= got[1][2] - (t0 + 20000) <= 3


def test_bounces_are_one_edge_and_a_short_pulse_keeps_both(outputs_sim):
	# A lever's contact bouncing: 3 short closings, then closed for 30 ms. With 2 ms of lockout that is one press,
	# at its first contact, and one release. Then a 0.5 ms touch, shorter than the lockout: both its edges are said,
	# the release at its own time, once the lockout ends.
	t0, t1 = 0.2e6, 0.35e6
	bounce = [pulse(A[2], t0, 80), pulse(A[2], t0 + 200, 80), pulse(A[2], t0 + 400, 30000)]
	ins = inputs_with(*bounce, pulse(A[2], t1, 500))
	edges, said = run(outputs_sim, 0.45, ["in 2 102 2 1 2000"], inputs=ins)
	got = said_inputs(said, 2)
	assert [g[1] for g in got] == [1, 0, 1, 0]
	assert 0 <= got[0][2] - t0 <= 3
	assert 0 <= got[1][2] - (t0 + 400 + 30000) <= 3
	assert 0 <= got[2][2] - t1 <= 3 and 0 <= got[3][2] - (t1 + 500) <= 3
	release = [t for t, l in said if l.startswith("i 2 0") and int(l.split()[3]) >= t1][0]
	assert release >= t1 + 2000 # said once the lockout was over


def test_a_link_rewards_the_poke_on_the_board_once(outputs_sim):
	# Poke -> 40 ms valve pulse on channel 3 (D5), armed once: the first poke opens the valve within microseconds,
	# the second (the link spent) doesn't
	pokes = [rise_of(10) + 7000, rise_of(30) + 1000]
	ins = inputs_with(*[pulse(A[0], t, 20000) for t in pokes])
	setup = ["in 0 100 2 1 1000", "link 0 0 1 1 3 40000", "arm 0 1"]
	edges, said = run(outputs_sim, 0.7, setup, *heartbeats(0.7), inputs=ins)
	valve = pulses(edges, 5)
	assert len(valve) == 1
	r, f = valve[0]
	assert 0 <= r - pokes[0] <= 10 and f - r == 40000
	fired = [l.split() for _, l in lines(said, "x")]
	assert len(fired) == 1 and fired[0][1:3] == ["0", "0"] and fired[0][5] == "1"
	assert abs(int(fired[0][3]) - pokes[0]) <= 3 and fired[0][4] == "10"
	assert [int(l.split()[2]) for _, l in lines(said, "p") if l.split()[1] == "3"] == [10] # the pulse's frame


def test_a_link_moves_a_servo_and_a_move_goes_at_its_speed(outputs_sim):
	# A door servo on D11, 1000-2000 us, safe (closed) at 1000, 2000 us/s: a poke opens it (link, until disarmed);
	# 0.5 s for the whole way, in steps of 20 ms; said when it's there
	poke = 0.2e6
	ins = inputs_with(pulse(A[0], poke, 10000))
	setup = ["in 0 100 2 1 1000", "servo 0 11 1000 2000 1000 2000", "link 1 0 1 4 0 2000", "arm 1 -1"]
	edges, said = run(outputs_sim, 1.0, setup, *heartbeats(1.0), inputs=ins)
	pwm = edges[("pwm", SERVO_PIN)]
	assert pwm[0][1] == duty(1000)
	assert pwm[-1][1] == duty(2000)
	assert all(b[1] >= a[1] for a, b in zip(pwm, pwm[1:])) # never back
	moving = [t for t, d in pwm if d != duty(1000)]
	assert 0.2e6 + 15000 <= moving[0] <= 0.2e6 + 25000
	there = [(t, l) for t, l in lines(said, "m")]
	assert len(there) == 1 and there[0][1].split()[1:3] == ["0", "2000"]
	assert 0.69e6 <= int(there[0][1].split()[3]) <= 0.74e6
	for (a, _), (b, _) in zip(pwm[1:], pwm[2:]):
		assert 19000 <= b - a <= 21000


def test_move_at_once_and_its_bounds(outputs_sim):
	setup = ["servo 2 12 1000 2000 1500 0"]
	edges, said = run(outputs_sim, 0.2, setup, (0.05, ["move 2 1100"]), (0.1, ["move 2 2500"]), inputs="")
	pwm = edges[("pwm", 12)]
	assert [d for _, d in pwm] == [duty(1500), duty(1100)]
	assert 50000 <= pwm[1][0] <= 50100
	assert [l for _, l in said if l.startswith("err")] == ["err move: past that servo's min or max"]
	assert [l.split()[1:3] for _, l in lines(said, "m")] == [["2", "1100"]]


def test_the_watchdog_disarms_links_and_sends_servos_to_safety(outputs_sim):
	# Door opened, a reward link armed, then the host goes quiet at 0.2 s: 250 ms later the door goes back to its
	# safe position (at its speed) and a poke after that gives no reward
	poke = 0.7e6
	ins = inputs_with(pulse(A[0], poke, 10000))
	setup = ["in 0 100 2 1 1000", "servo 0 11 1000 2000 1000 4000", "move 0 2000", "link 0 0 1 1 3 40000",
		"arm 0 -1"]
	edges, said = run(outputs_sim, 1.2, setup, (0.1, HB), (0.2, HB), inputs=ins)
	wd_t = int(lines(said, "wd")[0][1].split()[1])
	assert 450000 <= wd_t <= 451000
	pwm = edges[("pwm", SERVO_PIN)]
	assert max(d for _, d in pwm) == duty(2000)
	assert pwm[-1][1] == duty(1000) and pwm[-1][0] > wd_t
	assert pulses(edges, 5) == [] and not lines(said, "x")
	assert len(lines(said, "wd")) == 1 # once back at safety, nothing more to guard


def test_alloff_makes_the_box_safe(outputs_sim):
	setup = ["in 0 100 2 1 1000", "servo 0 11 1000 2000 1000 0", "move 0 1800", "link 0 0 1 2 1 0", "arm 0 -1"]
	edges, said = run(outputs_sim, 0.3, setup, (0.1, ["alloff", "box"]), inputs="")
	assert [d for _, d in edges[("pwm", SERVO_PIN)]] == [duty(1000), duty(1800), duty(1000)]
	box = [l for _, l in said if l.startswith("ok box")][0].split()
	assert box[3:] == ["in=0", "armed=0", "s0=1000/1000"]


def test_pins_are_never_shared_with_an_input_or_a_servo(outputs_sim):
	commands = ["in 0 2 2 1 0", "in 0 10 2 1 0", "in 0 100 2 1 0", "servo 0 100 1000 2000 1500 0", "pin 0 100",
		"trig 100", "in 1 100 2 1 0", "servo 0 11 1000 2000 1500 0", "in 1 11 2 1 0", "in 0 -1", "pin 0 100"]
	edges, said = run(outputs_sim, 0.05, commands, inputs="")
	answers = [l for _, l in said if l.startswith(("ok", "err"))]
	assert answers == ["err that pin is taken", "err that pin is taken", "ok in 0 30 0", "err that pin is taken",
		"err that pin is an input's or a servo's", "err that pin is an input's or a servo's", "err that pin is taken",
		"ok servo 0 11 " + answers[7].split()[4], "err that pin is taken", "ok in 0 -1", "ok pin 0 100"]


def test_licks_alongside_photometry_trains(outputs_sim):
	# A lick sensor at 7 Hz (20 ms contacts) while two photometry trains run on alternate frames: every lick said,
	# both edges, and the trains' pulses as exact as ever
	licks = "{}:{}:{}:{}:{}".format(A[3], 150000, 142857, 20000, 6)
	ins = inputs_with(licks)
	setup = ["in 3 103 2 1 500", "group 0 1", "group 1 1", "train 0 2 1 0 3000", "train 1 2 0 0 3000", "run 0 -1",
		"run 1 -1"]
	edges, said = run(outputs_sim, 1.1, setup, *heartbeats(1.1), inputs=ins)
	got = said_inputs(said, 3)
	assert len(got) == 12 and [g[1] for g in got] == [1, 0] * 6
	for n, (i, level, t, k, after) in enumerate(got[::2]):
		assert 0 <= t - (150000 + n * 142857) <= 3
	for k, (r, f) in enumerate(sorted(pulses(edges, 2) + pulses(edges, 3)), start=1):
		assert 0 <= r - rise_of(k) <= (10 if k <= 2 else 3) and f - r == 3000, (k, r)


def test_bad_box_commands_are_answered(outputs_sim):
	commands = ["in 8 100 2 1 0", "in 0 100 3 1 0", "servo 0 11 400 2000 1500 0", "servo 0 11 1000 2000 2100 0",
		"move 0 1500", "link 0 0 1 1 3 1000", "arm 0 1", "link 0 9 1 1 3 1000", "link 0 0 1 6 3 1000"]
	edges, said = run(outputs_sim, 0.05, commands, inputs="")
	assert len([l for _, l in said if l.startswith("err")]) == len(commands)
