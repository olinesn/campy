"""campy-rules (campy/rules.py): rules on campy-live's results, and the outputs board they switch.

The rules run on rows made here, as campy-live makes them (campy/live.py's
RowType): 3 cameras, 3 animals, nodes nose, head, tail. The live tests read a
real results feed (campy/liveresults.py) and, for the board, the sketch
itself in real time on a pseudo-terminal (tests/arduino_sim live), with a
simulated 60 Hz camera trigger.
"""

import json, os, subprocess, sys, threading, time, types

import numpy as np
import pytest

from campy import clock, live, liveresults, rules
from campy.trigger import outputs

from test_livefeed import name

NODES = ["nose", "head", "tail"]
CAMERAS = ["cam0", "cam1", "cam2"]
ROW = live.RowType(3, 3, 3, 3)
ROW2D = live.RowType(3, 3, 3, 0)


def desc(animals=3):
	fields = (ROW if animals else ROW2D).names
	return rules.Description(CAMERAS, NODES, animals, fields, 60.0)


def make_row(frame, points=None, t_ns=None, keypoints=None, found=None, members=None, row_type=ROW):
	"""
	points: {animal: {node: [x, y, z]}}; the others NaN. keypoints: {(camera,
	instance): {node: [x, y]}}.
	"""
	row = np.zeros((), row_type)
	row["frame"] = frame
	t = int(t_ns if t_ns is not None else frame * 16_666_667)
	row["host_ns"] = [t - 300_000, t - 100_000, t]
	row["done_ns"] = t + 5_000_000
	for f in ("keypoints", "centroids", "peak_vals", "centroid_vals"):
		row[f] = np.nan
	for (c, i), nodes in (keypoints or {}).items():
		for node, p in nodes.items():
			row["keypoints"][c, i, NODES.index(node)] = p
	row["found"] = found if found is not None else [3, 3, 3]
	if "points3d" in row_type.names:
		row["pose3d_ns"] = t + 6_000_000
		row["points3d"] = np.nan
		row["members"] = members if members is not None else np.tile(np.arange(3)[:, None], (1, 3))
		for a, nodes in (points or {}).items():
			for node, p in nodes.items():
				row["points3d"][a, NODES.index(node)] = p
	return row


def build(config, d=None):
	return rules.Rules(config, d or desc())


OUT = {"laser": {"channel": 2, "pin": 4}, "reward": {"channel": 3, "pin": 5, "pulse_ms": 50},
	"blue": {"channel": 0, "pin": 2, "group": 1, "train": {"every": 2, "phase": 1, "width_us": 3000}},
	"violet": {"channel": 1, "pin": 3, "group": 1, "train": {"every": 2, "phase": 0, "width_us": 3000},
		"run": "session"}}
NEST = {"box": [[0, 0, 0], [100, 100, 50]]}


def steps(r, rows, step_ms=16.666667):
	# Each row's commands' texts, by row
	out = []
	for i, row in enumerate(rows):
		values, commands = r.Step(row, int(i * step_ms * 1e6))
		out.append([c[3] for c in commands])
	return out


# ---------------------------------------------------------------- conditions

def test_inside_a_box_switches_a_level_on_and_off():
	r = build({"outputs": OUT, "regions": {"nest": NEST},
		"rules": [{"name": "m2", "when": {"inside": {"region": "nest", "animal": 2, "node": "nose"}}, "do": {"output": "laser"}}]})
	inside, outside = {2: {"nose": [50, 50, 10]}}, {2: {"nose": [150, 50, 10]}}
	got = steps(r, [make_row(1, outside), make_row(2, inside), make_row(3, inside), make_row(4, outside)])
	assert got == [[], ["on 2"], [], ["off 2"]]


def test_identity_matters():
	# Mouse 1 in the nest: a rule for mouse 2 doesn't hold; one for any does
	r = build({"outputs": OUT, "regions": {"nest": NEST},
		"rules": [{"name": "m2", "when": {"inside": {"region": "nest", "animal": 2, "node": "nose"}}, "do": {"output": "laser"}},
			{"name": "any", "when": {"inside": {"region": "nest", "animal": "any", "node": "nose"}},
			"do": {"output": "reward"}}]})
	pts = {0: {"nose": [500, 0, 0]}, 1: {"nose": [50, 50, 10]}, 2: {"nose": [150, 50, 10]}}
	assert steps(r, [make_row(1, pts)]) == [["pulse 3 50000"]]


def test_debounce_enter_and_exit():
	r = build({"outputs": OUT, "regions": {"nest": NEST}, "rules": [{"name": "m", "enter_ms": 40, "exit_ms": 40,
		"when": {"inside": {"region": "nest", "animal": 0, "node": "nose"}}, "do": {"output": "laser"}}]})
	i, o = {0: {"nose": [50, 50, 10]}}, {0: {"nose": [150, 50, 10]}}
	# 16.7 ms apart: in for 1 set (a blip: nothing), then 3 sets (on at the third, 33 ms... 50 ms in)
	seq = [o, i, o, i, i, i, i, o, i, o, o, o, o]
	got = steps(r, [make_row(k + 1, p) for k, p in enumerate(seq)])
	assert got.index(["on 2"]) == 6 # true since set 3 (index 3): 40 ms later is set 6
	assert got.index(["off 2"]) == 12 # false since 9; held through the blip at 8
	assert sum(len(g) for g in got) == 2


def test_margin_is_hysteresis():
	r = build({"outputs": OUT, "regions": {"nest": NEST}, "rules": [{"name": "m",
		"when": {"inside": {"region": "nest", "animal": 0, "node": "nose", "margin": 10}}, "do": {"output": "laser"}}]})
	at = lambda x: {0: {"nose": [x, 50, 10]}}
	got = steps(r, [make_row(1, at(105)), make_row(2, at(95)), make_row(3, at(105)), make_row(4, at(112))])
	assert got == [[], ["on 2"], [], ["off 2"]]


def test_missing_data_holds_then_switches_off_never_on():
	r = build({"outputs": OUT, "regions": {"nest": NEST}, "rules": [{"name": "m", "missing_ms": 50,
		"when": {"inside": {"region": "nest", "animal": 0, "node": "nose"}}, "do": {"output": "laser"}}]})
	i = {0: {"nose": [50, 50, 10]}}
	# unseen from the start: never on
	assert steps(r, [make_row(k, None) for k in range(1, 6)]) == [[]] * 5
	r = build({"outputs": OUT, "regions": {"nest": NEST}, "rules": [{"name": "m", "missing_ms": 45,
		"when": {"inside": {"region": "nest", "animal": 0, "node": "nose"}}, "do": {"output": "laser"}}]})
	got = steps(r, [make_row(1, i)] + [make_row(k, None) for k in range(2, 8)])
	assert got[0] == ["on 2"]
	assert got.index(["off 2"]) == 3 # last known at 0 ms: held at 16.7 and 33.3 ms, false at 50 > 45


def test_near_two_mice_with_leave():
	r = build({"outputs": OUT, "rules": [{"name": "n2n", "when": {"near": {"a": {"animal": "any", "node": "nose"},
		"b": {"animal": "other", "node": "nose"}, "within": 15, "leave": 25}}, "do": {"output": "laser"}}]})
	def pts(d):
		return {0: {"nose": [0, 0, 0]}, 1: {"nose": [d, 0, 0]}, 2: {"nose": [500, 0, 0]}}
	got = steps(r, [make_row(1, pts(30)), make_row(2, pts(10)), make_row(3, pts(20)), make_row(4, pts(26))])
	assert got == [[], ["on 2"], [], ["off 2"]]


def test_near_same_animal_and_mean_of_nodes():
	# Nose to tail of one mouse (curled up), and the mean of nodes
	r = build({"outputs": OUT, "rules": [{"name": "curl", "when": {"near": {"a": {"animal": 1, "node": "nose"},
		"b": {"animal": "same", "node": "tail"}, "within": 20}}, "do": {"output": "laser"}},
		{"name": "center", "when": {"near": {"a": {"animal": 0, "node": ["nose", "tail"]},
		"b": {"animal": 2, "node": "all"}, "within": 5}}, "do": {"output": "reward"}}]})
	p = {0: {"nose": [0, 0, 0], "tail": [10, 0, 0]}, 1: {"nose": [0, 0, 0], "tail": [10, 0, 0]},
		2: {"nose": [4, 0, 0], "head": [5, 0, 0], "tail": [6, 0, 0]}}
	assert steps(r, [make_row(1, p)]) == [["on 2", "pulse 3 50000"]]


def test_sphere_cylinder_and_combinators():
	r = build({"outputs": OUT, "regions": {"port": {"sphere": {"center": [0, 0, 0], "radius": 10}},
		"arena": {"cylinder": {"center": [0, 0], "radius": 100, "z": [0, 50]}}},
		"rules": [{"name": "c", "when": {"all": [{"inside": {"region": "arena", "animal": 0, "node": "nose"}},
			{"not": {"inside": {"region": "port", "animal": 0, "node": "nose"}}}]}, "do": {"output": "laser"}}]})
	at = lambda p: {0: {"nose": p}}
	got = steps(r, [make_row(1, at([5, 0, 5])), make_row(2, at([50, 0, 5])), make_row(3, at([50, 0, 80]))])
	assert got == [[], ["on 2"], ["off 2"]]


def test_a_camera_polygon_without_calibration():
	# 2D only: per camera, on the detections (any animal)
	d = desc(animals=0)
	r = build({"outputs": OUT, "regions": {"door": {"camera": "cam1", "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]]}},
		"rules": [{"name": "door", "when": {"inside": {"region": "door", "node": "head"}}, "do": {"output": "laser"}}]}, d)
	row_in = make_row(1, keypoints={(1, 0): {"head": [500, 500]}, (1, 1): {"head": [50, 50]}}, found=[0, 2, 0],
		row_type=ROW2D)
	row_out = make_row(2, keypoints={(1, 0): {"head": [500, 500]}, (1, 1): {"head": [150, 50]}}, found=[0, 2, 0],
		row_type=ROW2D)
	assert steps(r, [row_in, row_out]) == [["on 2"], ["off 2"]]


def test_identity_in_one_camera_uses_its_members():
	# Animal 2 is camera 0's detection 1
	r = build({"outputs": OUT, "regions": {"door": {"camera": "cam0", "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]]}},
		"rules": [{"name": "d", "when": {"inside": {"region": "door", "animal": 2, "node": "nose"}}, "do": {"output": "laser"}}]})
	members = np.full((3, 3), -1)
	members[2, 0] = 1
	row = make_row(1, keypoints={(0, 0): {"nose": [500, 500]}, (0, 1): {"nose": [50, 50]}}, members=members)
	assert steps(r, [row]) == [["on 2"]]


def test_refractory_and_pulse():
	r = build({"outputs": OUT, "rules": [{"name": "t", "refractory_s": 0.1, "when": {"test": {"every": 2}},
		"do": {"output": "reward"}}]})
	got = steps(r, [make_row(k) for k in range(1, 15)])
	# true on even frames; a start at most every 100 ms (6 sets)
	starts = [i for i, g in enumerate(got) if g]
	assert all(g == ["pulse 3 50000"] for g in got if g)
	assert starts == [1, 7, 13]


def test_trains_and_bursts_and_several_rules_on_one_output():
	r = build({"outputs": OUT, "regions": {"nest": NEST}, "rules": [
		{"name": "a", "when": {"inside": {"region": "nest", "animal": 0, "node": "nose"}}, "do": {"output": "blue"}},
		{"name": "b", "when": {"inside": {"region": "nest", "animal": 1, "node": "nose"}}, "do": {"output": "blue"}},
		{"name": "c", "when": {"inside": {"region": "nest", "animal": 2, "node": "nose"}},
			"do": {"output": "blue", "action": "burst", "pulses": 5}}]})
	i, o = [50, 50, 10], [500, 0, 0]
	seq = [{0: {"nose": o}, 1: {"nose": o}, 2: {"nose": i}},   # c: a burst
		{0: {"nose": i}, 1: {"nose": o}, 2: {"nose": o}},      # a: runs
		{0: {"nose": i}, 1: {"nose": i}, 2: {"nose": o}},      # b too: nothing new
		{0: {"nose": o}, 1: {"nose": i}, 2: {"nose": i}},      # a stops, b holds; c's burst: train running already
		{0: {"nose": o}, 1: {"nose": o}, 2: {"nose": o}}]      # none: stop
	assert steps(r, [make_row(k + 1, p) for k, p in enumerate(seq)]) == [["run 0 5"], ["run 0 -1"], [], [], ["run 0 0"]]


@pytest.mark.parametrize("first", ["level", "train"])
def test_a_level_and_a_train_on_one_output_each_stop_with_their_own_rules(first):
	# One rule holds the laser's level, the other its train. Whichever starts and stops first, each sends its own
	# stop when its own last rule lets go ("off" doesn't stop a train on the board, "run ch 0" doesn't clear a level)
	out = dict(OUT, laser={"channel": 2, "pin": 4, "train": {"every": 1, "width_us": 3000}})
	actions = {"level": "level", "train": "train"}
	second = "train" if first == "level" else "level"
	r = build({"outputs": out, "regions": {"nest": NEST}, "rules": [
		{"name": first, "when": {"inside": {"region": "nest", "animal": 0, "node": "nose"}},
			"do": {"output": "laser", "action": actions[first]}},
		{"name": second, "when": {"inside": {"region": "nest", "animal": 1, "node": "nose"}},
			"do": {"output": "laser", "action": actions[second]}}]})
	i, o = [50, 50, 10], [500, 0, 0]
	seq = [{0: {"nose": i}, 1: {"nose": o}}, {0: {"nose": i}, 1: {"nose": i}}, {0: {"nose": o}, 1: {"nose": i}},
		{0: {"nose": o}, 1: {"nose": o}}]
	start = {"level": "on 2", "train": "run 2 -1"}
	stop = {"level": "off 2", "train": "run 2 0"}
	got = steps(r, [make_row(k + 1, p) for k, p in enumerate(seq)])
	assert got == [[start[first]], [start[second]], [stop[first]], [stop[second]]]
	# The board's state, as the sketch keeps it: nothing left on
	level = train = False
	for text in sum(got, []):
		level = {"on 2": True, "off 2": False}.get(text, level)
		train = {"run 2 -1": True, "run 2 0": False}.get(text, train)
	assert not level and not train
	assert r.Release() == []


def test_release_stops_a_level_and_a_train_on_one_output():
	out = dict(OUT, laser={"channel": 2, "pin": 4, "train": {"every": 1, "width_us": 3000}})
	r = build({"outputs": out, "regions": {"nest": NEST}, "rules": [
		{"name": "t", "when": {"inside": {"region": "nest", "animal": 0, "node": "nose"}},
			"do": {"output": "laser", "action": "train"}}]})
	steps(r, [make_row(1, {0: {"nose": [50, 50, 10]}})])
	assert sorted(c[3] for c in r.Release()) == ["off 2", "run 2 0"]


def test_a_refused_switch_on_is_sent_again_while_its_rule_holds():
	r = build({"outputs": OUT, "regions": {"nest": NEST}, "rules": [
		{"name": "m", "when": {"inside": {"region": "nest", "animal": 0, "node": "nose"}}, "do": {"output": "laser"}},
		{"name": "b", "when": {"inside": {"region": "nest", "animal": 0, "node": "nose"}}, "do": {"output": "blue"}}]})
	i, o = {0: {"nose": [50, 50, 10]}}, {0: {"nose": [500, 0, 0]}}
	assert steps(r, [make_row(1, i)]) == [["on 2", "run 0 -1"]]
	r.Refused("laser", "level")
	r.Refused("blue", "train")
	values, commands = r.Step(make_row(2, i), 2 * 16_666_667)
	assert sorted(c[3] for c in commands) == ["on 2", "run 0 -1"]
	assert all(isinstance(c, rules.Retry) for c in commands)
	values, commands = r.Step(make_row(3, i), 3 * 16_666_667)
	assert commands == [] # the board said ok: nothing more
	# Refused, then the rule lets go before the next set: nothing is sent again
	r.Refused("laser", "level")
	assert sorted(c[3] for c in r.Step(make_row(4, o), 4 * 16_666_667)[1]) == ["off 2", "run 0 0"]
	assert r.Step(make_row(5, o), 5 * 16_666_667)[1] == []


def test_setup_and_session_trains():
	r = build({"outputs": OUT, "rules": []})
	setup = r.Setup()
	assert "train 0 2 1 0 3000" in setup and "train 1 2 0 0 3000" in setup and "group 1 1" in setup
	assert [c[3] for c in r.SessionStarts()] == ["run 1 -1"]


def test_release_switches_off_what_rules_switched_on():
	r = build({"outputs": OUT, "regions": {"nest": NEST}, "rules": [
		{"name": "a", "when": {"inside": {"region": "nest", "animal": 0, "node": "nose"}}, "do": {"output": "laser"}},
		{"name": "b", "when": {"inside": {"region": "nest", "animal": 0, "node": "nose"}}, "do": {"output": "blue"}}]})
	steps(r, [make_row(1, {0: {"nose": [50, 50, 10]}})])
	assert sorted(c[3] for c in r.Release()) == ["off 0", "off 2", "run 0 0"]
	assert r.Release() == []
	assert not any(rule.holding for rule in r.rules)


@pytest.mark.parametrize("config, words", [
	({"outputs": {}}, "outputs: none"),
	({"outputs": {"a": {"channel": 9}}}, "channel must be 0-7"),
	({"outputs": {"a": {"channel": 1}, "b": {"channel": 1}}}, "two outputs on one channel"),
	({"outputs": OUT, "rules": [{"when": {"test": {"every": 2}}, "do": {"output": "nope"}}]}, "no output"),
	({"outputs": OUT, "rules": [{"when": {"test": {"every": 2}}, "do": {"output": "violet"}}]}, "whole session"),
	({"outputs": OUT, "rules": [{"when": {"inside": {"region": "x", "animal": 0}}, "do": {"output": "laser"}}]}, "no region"),
	({"outputs": OUT, "regions": {"nest": NEST}, "rules": [{"when": {"inside": {"region": "nest", "animal": 7}},
		"do": {"output": "laser"}}]}, "campy-live tracks 3"),
	({"outputs": OUT, "regions": {"nest": NEST}, "rules": [{"when": {"inside": {"region": "nest", "node": "paw"}},
		"do": {"output": "laser"}}]}, "no node 'paw'"),
	({"outputs": OUT, "rules": [{"when": {"near": {"a": {"node": "nose"}, "b": {"node": "nose"}, "within": 9,
		"leave": 5}}, "do": {"output": "laser"}}]}, "leave must be at least"),
	({"outputs": OUT, "rules": [{"when": {"test": {"every": 2}}, "do": {"output": "laser", "action": "burst"}}]},
		"has no train"),
	({"outputs": OUT, "rules": [{"when": {"test": {"every": 2}}, "do": {"output": "laser", "action": "pulse"}}]},
		"needs pulse_ms"),
])
def test_a_bad_file_says_why(config, words):
	with pytest.raises(rules.RulesError, match=words):
		build(config)


def test_3d_needs_a_calibration():
	with pytest.raises(rules.RulesError, match="calibration"):
		build({"outputs": OUT, "regions": {"nest": NEST}, "rules": []}, desc(animals=0))


# ---------------------------------------------------------------- replay

def write_log(path, rows, animals=3):
	np.save(path + ".npy", np.array(rows))
	with open(path + ".json", "w") as f:
		json.dump(dict(cameras=CAMERAS, nodes=NODES, calibration="/x/calibration.toml" if animals else None,
			animals=animals), f)


def write_rules(path, config):
	import yaml
	with open(path, "w") as f:
		yaml.safe_dump(config, f)
	return path


def test_replay_a_log(tmp_path, capsys):
	log = str(tmp_path / "campy-live_x")
	i, o = {1: {"nose": [50, 50, 10]}}, {1: {"nose": [500, 0, 0]}}
	write_log(log, [make_row(k, i if 10 <= k < 70 else o) for k in range(1, 121)])
	path = write_rules(str(tmp_path / "rules.yaml"), {"outputs": OUT, "regions": {"nest": NEST}, "rules": [
		{"name": "m1", "when": {"inside": {"region": "nest", "animal": 1, "node": "nose"}}, "do": {"output": "laser"}}]})
	assert rules.Main([path, "--replay", log + ".npy"]) == 0
	said = capsys.readouterr().out
	assert "m1" in said and "started     1 times" in said
	events = [json.loads(l) for l in open(log + ".rules.events.jsonl")]
	assert [(e["command"], e["frame"]) for e in events] == [("on 2", 10), ("off 2", 70)]


# ---------------------------------------------------------------- live

class Feed:
	"""campy-live's results feed, rows written by the test."""

	def __init__(self, prefix, animals=3):
		self.prefix = prefix
		self.publisher = liveresults.Publisher(liveresults.RingName(prefix), ROW, cameras=CAMERAS, nodes=NODES,
			calibration="/x/calibration.toml", calibration_names=CAMERAS, animals=animals, frame_rate=60.0)

	def Write(self, row):
		self.publisher.Write(row)

	def Close(self):
		self.publisher.Close()


def args_for(prefix, out, *extra):
	p = ["rules.yaml", "--name", prefix, "--out", out, "--poll-ms", "0.2", "--wait-sec", "5"] + list(extra)
	# Runner's own parsing, without running Main's signal setup
	ns = types.SimpleNamespace(rules="rules.yaml", name=prefix, port="", dry_run=False, replay="", out=out,
		bound_ms=None, stale_ms=200.0, poll_ms=0.2, wait_sec=5.0, duration_s=0.0)
	for i in range(0, len(extra), 2):
		key = extra[i].lstrip("-").replace("-", "_")
		value = extra[i + 1]
		ns.__dict__[key] = type(ns.__dict__[key])(value) if ns.__dict__[key] is not None else float(value)
	return ns


def start(runner):
	result = {}
	thread = threading.Thread(target=lambda: result.setdefault("code", runner.Run()), daemon=True)
	thread.start()
	return thread, result


def test_dry_run_logs_and_releases_when_the_feed_stops(tmp_path):
	prefix = name()
	feed = Feed(prefix)
	out = str(tmp_path / "dry")
	config = {"outputs": OUT, "regions": {"nest": NEST}, "rules": [
		{"name": "m0", "when": {"inside": {"region": "nest", "animal": 0, "node": "nose"}}, "do": {"output": "laser"}}]}
	args = args_for(prefix, out)
	args.dry_run = True
	runner = rules.Runner(args, config)
	runner.say = lambda text: None
	thread, result = start(runner)
	try:
		time.sleep(0.3)
		for k in range(1, 11):
			feed.Write(make_row(k, {0: {"nose": [50, 50, 10]}}, t_ns=clock.HostNs()))
			time.sleep(1 / 60)
		time.sleep(0.4) # no set for 200 ms: released
	finally:
		runner.stop.set()
		thread.join(5)
		feed.Close()
	assert result["code"] == 0
	events = [json.loads(l) for l in open(out + ".events.jsonl")]
	commands = [(e["command"], e["answer"]) for e in events if e["kind"] == "command"]
	assert commands == [("run 1 -1", "dry run"), ("on 2", "dry run"), ("off 2", "dry run")]
	rows = np.load(out + ".npy")
	assert list(rows["frame"]) == list(range(1, 11))
	assert rows["holding"][:, 0].all()
	summary = json.load(open(out + ".json"))
	assert summary["counts"]["stale"] == 1 and summary["dry_run"]


def pty():
	import tty
	master, slave = os.openpty()
	tty.setraw(slave)
	return master, slave


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pseudo-terminal for the simulated board")
def test_live_rules_switch_the_board_with_the_frame_and_its_latency(tmp_path, outputs_sim):
	"""
	The whole path: rows into the results feed, campy-rules, the board's
	sketch in real time on a pty, with a 60 Hz trigger. A test rule pulses
	on every 6th set; each pulse names its frame, the board says how long
	after that frame's trigger its pin rose, and campy-rules logs it.
	"""
	period_us = 16667
	master, slave = pty()
	sim_log = tmp_path / "sim.log"
	env = dict(os.environ, SIM_INPUT="10:300000:{}:3000:100000".format(period_us))
	with open(str(sim_log), "w") as log:
		board_sim = subprocess.Popen([outputs_sim, "live"], stdin=master, stdout=master, stderr=log, env=env)
	prefix = name()
	feed = Feed(prefix)
	out = str(tmp_path / "live")
	config = {"board": {"trigger_pin": 10, "bound_ms": 60, "watchdog_ms": 250},
		"outputs": {"mark": {"channel": 3, "pin": 5, "pulse_ms": 2}},
		"rules": [{"name": "every6", "when": {"test": {"every": 6}}, "do": {"output": "mark", "action": "pulse"}}]}
	args = args_for(prefix, out)
	args.port = os.ttyname(slave)
	runner = rules.Runner(args, config)
	said = []
	runner.say = said.append
	thread, result = start(runner)
	try:
		# Once the board's triggers have begun: rows as campy-live makes them,
		# frame k once the board has reported trigger k (a frame never comes
		# before its trigger; the live simulation's own timing is too rough to
		# place it closer). campy's frame numbers are the board's: both count
		# from the first trigger.
		deadline = time.monotonic() + 10
		while time.monotonic() < deadline and (runner.board is None or len(runner.board.rises) < 5 or
				not runner.board.clock.Ready()):
			time.sleep(0.01)
		board = runner.board
		k0 = next(reversed(board.rises))
		for k in range(k0 + 2, k0 + 62):
			while k not in board.rises and time.monotonic() < deadline + 5:
				time.sleep(0.0005)
			# The frame reached the host 5 ms after its trigger, as a camera's does: its time from the trigger's,
			# not from when this loop got to it. On a busy computer (other tests beside this one) the loop can be
			# held up for more than 3 frames, and a frame stamped then would look late by that much, which
			# campy-rules rightly takes for another frame (frame_offset_changes)
			rise = board.RiseHostNs(k)
			feed.Write(make_row(k, t_ns=rise + 5_000_000 if rise is not None else clock.HostNs()))
		time.sleep(0.3)
	finally:
		runner.stop.set()
		thread.join(10)
		feed.Close()
		board_sim.terminate()
		board_sim.wait()
		os.close(slave)
		os.close(master)
	assert result.get("code") == 0, said
	events = [json.loads(l) for l in open(out + ".events.jsonl")]
	commands = [e for e in events if e["kind"] == "command"]
	assert len(commands) == 10, commands
	for e in commands:
		assert e["answer"].startswith("ok pulse 3"), e
		assert e["command"] == "pulse 3 2000 {}".format(e["frame"]) # the frame it was decided on
		# The pin rose after the frame's trigger, by the trigger's report, the
		# row, campy-rules' and the board's own time
		assert 0 < e["after_us"] / 1e3 <= 60, e
	pulses = [e for e in events if e["kind"] == "pulse"]
	# 2 ms each (the live simulation's clock reads sleep 50 us, and macOS can
	# wake it later; the simulated clock's tests check the widths exactly)
	assert len(pulses) == 10 and all(1900 <= p["fall_us"] - p["rise_us"] <= 6000 for p in pulses)
	summary = json.load(open(out + ".json"))
	assert summary["frame_offset"] == 0 and summary["frame_offset_changes"] == 0
	assert summary["latency_ms"]["trigger_to_pin"]["n"] == 10
	t2h = summary["trigger_to_host_all_ms"]
	assert 0 < t2h["p50"] < 16.7 and t2h["n"] >= 55, t2h
	board = open(out + ".board.txt").read()
	assert "> alloff" in board.splitlines()[-2] or "> alloff" in board # switched off at the end
	# The board's own pin: pulses on pin 5, 2 ms each (the live simulation's clock reads take 50 us)
	pin5 = [line.split() for line in sim_log.read_text().splitlines() if line.split()[1:2] == ["5"]]
	highs = [int(t) for t, pin, level in pin5 if level == "1"]
	assert len(highs) == 10


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pseudo-terminal for the simulated board")
def test_board_refuses_a_port_without_the_sketch(tmp_path):
	master, slave = pty()
	try:
		with pytest.raises(outputs.BoardError, match="did not answer"):
			import serial
			outputs.Board(connection=serial.Serial(os.ttyname(slave), 115200, timeout=0.005), port="fake",
				heartbeat=0, sync=0)
	finally:
		os.close(slave)
		os.close(master)


def test_clock_map_is_offset_and_drift():
	m = outputs.ClockMap()
	# board us = (host ns - 5e9) / 1000 * (1 + 20 ppm), round trips 1 ms
	for i in range(30):
		host = 5_000_000_000 + i * 1_000_000_000
		board = (host + 500_000 - 5_000_000_000) / 1000 * (1 + 20e-6)
		m.Add(host, host + 1_000_000, board)
	b = (25_000_000_000 - 5_000_000_000) / 1000 * (1 + 20e-6)
	assert abs(m.HostNs(b) - 25_000_000_000) < 2_000
	assert m.RoundTrip() == 1_000_000


class FakePort:
	"""A serial port whose board answers what the test says, as it says."""

	def __init__(self, version=outputs.VERSION):
		self.written, self.incoming = [], []
		self.lock = threading.Lock()
		self.version = version # what it answers to id

	def write(self, data):
		with self.lock:
			self.written.append(data.decode().strip())
			if data.decode().strip() == "id":
				self.incoming.append("ok campy_outputs {}\r\n".format(self.version).encode())
			elif data.decode().strip() == "time":
				self.incoming.append("ok time {} 0 -1\r\n".format(clock.HostNs() // 1000).encode())

	def read(self, n):
		with self.lock:
			if self.incoming:
				return self.incoming.pop(0)
		time.sleep(0.001)
		return b""

	def Say(self, line):
		with self.lock:
			self.incoming.append((line + "\r\n").encode())

	def close(self):
		pass


def test_board_refuses_an_older_or_newer_sketch():
	# A board still running the sketch from before pull request 304 answers
	# id with version 1: campy-rules says to upload the new one, and why
	with pytest.raises(outputs.BoardError, match=r"older campy_outputs \(version 1\).*needs version 2.*Upload"):
		outputs.Board(connection=FakePort(version=1), port="fake", heartbeat=0, sync=0)
	with pytest.raises(outputs.BoardError, match=r"newer campy_outputs \(version 3\).*Update campy"):
		outputs.Board(connection=FakePort(version=3), port="fake", heartbeat=0, sync=0)
	assert "not campy_outputs version 2" in outputs.Mismatch("fake", "ok something 2")


def test_a_lost_answer_never_goes_to_the_wrong_command():
	port = FakePort()
	board = outputs.Board(connection=port, port="fake", heartbeat=0, sync=0)
	try:
		a, b = board.Command("on 2"), board.Command("pulse 3 1000")
		port.Say("ok pulse 3 100 -1 -1") # the answer to "on 2" was lost
		assert b.Wait(1) == "ok pulse 3 100 -1 -1"
		assert a.Wait(1).startswith("err no answer")
	finally:
		board.Close(off=False)


def test_frame_match_reads_only_the_last_rises_and_keeps_its_delays_bounded(monkeypatch):
	# Period: the median gap of the last 8 trigger rises, without copying all 4096 kept (~60 us a row before);
	# the delays for the summary in bounded memory (campy/runstats.py)
	import collections
	from campy import runstats
	board = types.SimpleNamespace(clock=outputs.ClockMap(), rises=collections.OrderedDict())
	for i in range(5):
		board.clock.Add(10**12 + i * 10**9, 10**12 + i * 10**9 + 400_000, i * 10**6)
	gaps = [16_667, 16_666, 16_668, 16_667, 16_670, 16_660, 16_667, 16_666, 16_667]
	t = 0
	for k in range(1, outputs.RISES + 1):
		t += gaps[k % len(gaps)] if k > outputs.RISES - 8 else 10_000 # older rises: another rate, not used
		board.rises[k] = t
	board.RiseHostNs = lambda k: board.clock.HostNs(board.rises.get(k))
	match = rules.FrameMatch(board, print)
	expected = int(np.median(np.diff(list(board.rises.values())[-8:]))) * 1000 # as it was computed
	assert match.Period() == expected == 16_667_000
	board.rises = collections.OrderedDict([(1, 100)])
	assert match.Period() is None
	assert isinstance(match.delays, runstats.Values) and rules.Stats(match.delays) is None
	monkeypatch.setattr(runstats, "BLOCK", 64)
	monkeypatch.setattr(runstats, "EXACT", 256)
	delays = runstats.Values(np.float64, floor=0.001)
	values = np.random.default_rng(4).normal(8.0, 0.5, 2000)
	for v in values:
		delays.Add(v)
	s = rules.Stats(delays)
	assert s["n"] == 2000 and s["max"] == round(values.max(), 3) and s["min"] == round(values.min(), 3)
	assert s["p50"] == pytest.approx(np.percentile(values, 50), rel=2e-3)


class SketchPort:
	"""
	A serial port whose board answers every command as the sketch does, or as
	refuse(words) says (a line, or None for the sketch's answer). Keeps every
	line written, with its host time.
	"""

	def __init__(self, refuse=None):
		self.written, self.incoming, self.lock = [], [], threading.Lock()
		self.refuse = refuse

	def write(self, data):
		text = data.decode().strip()
		now = clock.HostNs()
		with self.lock:
			self.written.append((now, text))
			w = text.split()
			if not w or w[0] == "hb":
				return
			reply = self.refuse(w) if self.refuse else None
			if reply is None:
				if w[0] == "id":
					reply = "ok campy_outputs {}".format(outputs.VERSION)
				elif w[0] == "time":
					reply = "ok time {} 0 -1".format(now // 1000)
				elif w[0] in ("on", "pulse"):
					reply = "ok {} {} {} -1 -1".format(w[0], w[1], now // 1000)
				elif w[0] == "off":
					reply = "ok off {} {}".format(w[1], now // 1000)
				elif w[0] == "run":
					reply = "ok run {} {} {} -1 -1".format(w[1], w[2], now // 1000)
				else:
					reply = "ok " + w[0]
			self.incoming.append((reply + "\r\n").encode())

	def read(self, n):
		with self.lock:
			if self.incoming:
				return self.incoming.pop(0)
		time.sleep(0.001)
		return b""

	def Say(self, line):
		with self.lock:
			self.incoming.append((line + "\r\n").encode())

	def Lines(self):
		with self.lock:
			return list(self.written)

	def close(self):
		pass


def run_on_port(monkeypatch, tmp_path, port, config, feed_rows, **arg):
	"""
	campy-rules with a board on port, and campy-live's rows: feed_rows(feed,
	runner) writes them. Returns the runner and its events.
	"""
	real = outputs.Board
	monkeypatch.setattr(outputs, "Board", lambda name, **kw: real(port=name, connection=port, **kw))
	prefix = name()
	feed = Feed(prefix)
	out = str(tmp_path / "rules")
	args = args_for(prefix, out)
	args.port = "fake"
	args.__dict__.update(arg)
	runner = rules.Runner(args, config)
	said = []
	runner.say = said.append
	thread, result = start(runner)
	try:
		deadline = time.monotonic() + 10
		while runner.board is None and time.monotonic() < deadline:
			time.sleep(0.01)
		time.sleep(0.1)
		feed_rows(feed, runner)
	finally:
		runner.stop.set()
		thread.join(10)
		feed.Close()
	assert result.get("code") == 0, said
	runner.said_lines = said
	return runner, [json.loads(l) for l in open(out + ".events.jsonl")]


IN_NEST = {0: {"nose": [50, 50, 10]}}
LASER_RULE = {"outputs": OUT, "regions": {"nest": NEST}, "rules": [
	{"name": "m0", "when": {"inside": {"region": "nest", "animal": 0, "node": "nose"}}, "do": {"output": "laser"}}]}


def test_refused_switch_ons_are_sent_again_with_the_next_sets(monkeypatch, tmp_path):
	# The first set reaches campy-rules too late (it doesn't send), the board then refuses the switch-on as late,
	# then for a clash; each time the next fresh set, the mouse still in the nest, sends it again, and the 4th
	# set's is made. Before, the laser stayed off while the rows said the rule held
	answers = iter(["err late 2 2 50000", "err clash 2 3 0"])

	def refuse(w):
		return next(answers, None) if w[0] == "on" else None
	port = SketchPort(refuse)

	def feed_rows(feed, runner):
		feed.Write(make_row(1, IN_NEST, t_ns=clock.HostNs() - 60_000_000)) # 60 ms old: over the 40 ms bound
		for k in range(2, 8):
			time.sleep(1 / 60)
			feed.Write(make_row(k, IN_NEST, t_ns=clock.HostNs()))
		time.sleep(0.1)
	runner, events = run_on_port(monkeypatch, tmp_path, port, LASER_RULE, feed_rows)
	commands = [e for e in events if e["kind"] == "command" and e["output"] == "laser"]
	assert [(e["frame"], e["answer"].split()[:2]) for e in commands] == [(1, ["late", "(campy-rules:"]),
		(2, ["err", "late"]), (3, ["err", "clash"]), (4, ["ok", "on"])]
	assert [bool(e.get("retry")) for e in commands] == [False, True, True, True]
	ons = [x for _, x in port.Lines() if x.startswith("on 2")]
	assert ons == ["on 2 2", "on 2 3", "on 2 4"]
	c = runner.counts
	assert (c["late_host"], c["late_board"], c["clash"], c["retries"]) == (1, 1, 1, 3)


def test_after_the_watchdog_the_rules_start_over_and_the_session_trains_again(monkeypatch, tmp_path):
	port = SketchPort()

	def feed_rows(feed, runner):
		for k in range(1, 6):
			feed.Write(make_row(k, IN_NEST, t_ns=clock.HostNs()))
			time.sleep(1 / 60)
		port.Say("wd 123456") # the board's watchdog switched everything off
		time.sleep(0.05)
		for k in range(6, 11):
			feed.Write(make_row(k, IN_NEST, t_ns=clock.HostNs()))
			time.sleep(1 / 60)
		time.sleep(0.1)
	runner, events = run_on_port(monkeypatch, tmp_path, port, LASER_RULE, feed_rows)
	lines = [x for _, x in port.Lines() if x.split()[0] in ("on", "off", "run")]
	assert lines[:2] == ["run 1 -1", "on 2 1"]
	after = lines[2:]
	assert after[:2] == ["off 2", "run 1 -1"] # released, the session's train started again
	assert after[2].startswith("on 2 ") and int(after[2].split()[2]) >= 6 # the rule holds again: on again
	assert runner.counts["watchdog"] == 1


def test_a_stuck_decision_loop_lets_the_board_watchdog_fire(monkeypatch, tmp_path):
	# The decision loop held up for 1 s with the laser on: the heartbeat stops LOOP_ALIVE_S later, so the board
	# hears nothing for far longer than its watchdog_ms (250) and switches everything off. Before, the reading
	# thread kept the heartbeat going whatever the loop did
	port = SketchPort()
	held = {}
	real_row = rules.Runner.Row

	def Row(self, row, *a):
		if int(row["frame"]) == 5:
			held["at"] = clock.HostNs()
			time.sleep(1.0)
			held["end"] = clock.HostNs()
		return real_row(self, row, *a)
	monkeypatch.setattr(rules.Runner, "Row", Row)

	def feed_rows(feed, runner):
		for k in range(1, 8):
			feed.Write(make_row(k, IN_NEST, t_ns=clock.HostNs()))
			time.sleep(1 / 60)
		time.sleep(1.3)
	run_on_port(monkeypatch, tmp_path, port, LASER_RULE, feed_rows)
	# Nothing at all on the port from LOOP_ALIVE_S into the hold to its end: 0.9 s, well over the watchdog's 250 ms
	quiet_from = held["at"] + int((rules.LOOP_ALIVE_S + 0.02) * 1e9)
	assert [x for t, x in port.Lines() if quiet_from <= t <= held["end"]] == []
	# Heartbeats before it, as usual
	assert any(x == "hb" and t < held["at"] for t, x in port.Lines())


def test_a_slow_row_log_write_holds_up_neither_the_stale_check_nor_the_heartbeat(monkeypatch, tmp_path):
	# One write of the rows' log held 1.5 s by the disk, the laser on, campy-live's rows then stopping: the laser
	# still goes off about --stale-ms (200) after the last row, and the heartbeat goes on. Before, the write was on
	# the decision thread: off came 1.7 s after the last row
	port = SketchPort()
	from campy.frametimes import GrowingNpy

	class SlowLog(GrowingNpy):
		calls = 0

		def Append(self, values):
			SlowLog.calls += 1
			if SlowLog.calls == 5:
				time.sleep(1.5)
			return super().Append(values)
	monkeypatch.setattr(rules, "GrowingNpy", SlowLog)
	last = {}

	def feed_rows(feed, runner):
		for k in range(1, 6):
			feed.Write(make_row(k, IN_NEST, t_ns=clock.HostNs()))
			last["row"] = clock.HostNs()
			time.sleep(1 / 60)
		time.sleep(1.8)
	runner, events = run_on_port(monkeypatch, tmp_path, port, LASER_RULE, feed_rows)
	off = next(t for t, x in port.Lines() if x.startswith("off 2"))
	assert 150 <= (off - last["row"]) / 1e6 <= 700, (off - last["row"]) / 1e6
	written = [t for t, x in port.Lines() if x == "hb" and last["row"] < t < last["row"] + 1_400_000_000]
	assert len(written) >= 10
	assert len(np.load(str(tmp_path / "rules.npy"))) == 5 # every row logged in the end
