"""
campy-rules: TTL outputs from the animals' live poses, for closed-loop
experiments: a laser while mouse 2 is in the nest, a reward pulse when two
mice touch noses, photometry excitation pulses locked to the camera frames.

	campy-rules rules.yaml --port /dev/ttyACM1      # with campy-live running
	campy-rules rules.yaml --dry-run                 # the same, logging only
	campy-rules rules.yaml --replay campy-live_2026-10-23_10-00-00.npy

It reads campy-live's results as campy-live makes them (the results feed,
campy/liveresults.py, which campy-live never waits for: so it can't slow
campy-live down), checks each set's poses against the rules, and switches the
outputs of a board of their own (campy/outputs_board/campy_outputs, an Arduino Nano
ESP32; campy/trigger/outputs.py talks to it). The rules come from a YAML file:

  board:
    port: /dev/ttyACM1      # or COM5 (--port)
    trigger_pin: 10         # the camera trigger, wired to the board's D10
    bound_ms: 40            # never act on a set older than this (from its trigger)
    watchdog_ms: 250        # everything off if campy-rules goes quiet this long
  outputs:
    blue:   {channel: 0, pin: 2, group: 1, run: session,
             train: {every: 2, phase: 0, offset_us: 0, width_us: 3000}}
    violet: {channel: 1, pin: 3, group: 1, run: session,
             train: {every: 2, phase: 1, offset_us: 0, width_us: 3000}}
    laser:  {channel: 2, pin: 4, max_on_s: 10}
    reward: {channel: 3, pin: 5, pulse_ms: 50}
  regions:
    nest:  {box: [[0, 0, 0], [120, 80, 60]]}               # 3D, the calibration's mm
    port:  {sphere: {center: [200, 40, 20], radius: 25}}
    arena: {cylinder: {center: [0, 0], radius: 200, z: [-10, 100]}}
    door:  {camera: cam1, polygon: [[10, 10], [200, 10], [200, 90], [10, 90]]}  # pixels
  rules:
    - name: mouse2_in_nest
      when: {inside: {region: nest, animal: 2, node: nose, margin: 10}}
      enter_ms: 50          # true this long before it switches on
      exit_ms: 100          # false this long before it switches off
      do: {output: laser}   # on while the rule holds
    - name: nose_to_nose
      when: {near: {a: {animal: any, node: nose}, b: {animal: other, node: nose},
                    within: 15, leave: 25}}
      refractory_s: 5
      do: {output: reward, action: pulse}

Inputs (an operant box's sensors, on the same board: docs/operant-boxes.md): as campy-box's,
  inputs:
    poke_left: {input: 0, pin: A0, pull: up}     # active low with a pull-up
and a rule's condition {input: poke_left} is true for a set while the input is active, or if it went active
since the set before (so a lick shorter than a frame still counts). With a pose condition, under all: a reward
only when mouse 2 is the one at the port. Every input edge is logged (kind input: its board time, host time and
the board's frame).

Outputs: a channel of the board (0-7) on an Arduino pin. With a train, the
board itself makes a pulse of width_us, offset_us after the camera trigger of
every frame k with (k - phase) % every == 0 (photometry excitation: 3 ms on
each frame, two wavelengths on alternate frames); run: session runs it from
start to end, whatever the poses; else rules run it. Outputs of one group are
never high together (the board skips the pulse and says so). max_on_s: the
board switches a level off after that long, whatever campy-rules says.

Rules: when is one condition, or all: [...], any: [...], not: {...}:
  input    an input's name (inputs:): active, or went active since the set before
  inside   {region, animal, node[, margin]}: the animal's node in the region.
           margin (mm, or pixels for a camera's polygon): once inside, it
           leaves only when that far out (hysteresis)
  near     {a: {animal, node}, b: {animal, node}, within[, leave][, camera]}:
           two points within that many mm (pixels with camera:), until
           further than leave (hysteresis; default within)
  test     {every: N}: true on the sets whose frame is a multiple of N (for
           measuring the latency with no animal at all)
  animal:  an identity (0 .. animals - 1, campy-live's --calibration), a
           list of them, or any; b's animal may also be other (not a's) or
           same (a's). Without a calibration, rules work per camera
           (camera: name) on the detections, animal any only.
  node:    a node's name or index, a list of them (their mean), or all.
A condition is unknown when an animal it needs isn't seen (in 3D: by fewer
than 2 cameras): the rule then keeps what it last was for missing_ms (default
100), then counts as false. So a rule never switches an output on from
missing data. do: {output, action}: level (on while the rule holds; the
default for an output without train or pulse_ms), pulse (pulse_ms, or the
output's, each time it starts holding), train (the output's train while it
holds), burst (pulses: N of the output's train, each time it starts holding).
refractory_s: it doesn't start holding again until that long after it last did
(also across a stale feed, the watchdog and campy-live's restart).
Several rules on one output: on (or running) while any of them holds.

Timing: each command names the frame its set came from, so the board says
how long after that frame's camera trigger its pin changed, and refuses one
that came more than bound_ms after it (nothing ever follows an old pose).
campy-rules checks the bound too before sending. Switching off is never
refused. If no set comes for --stale-ms (200), what the rules switched on goes
off (the session's trains go on: they don't depend on the poses).

Several rules on one output: its level is on while any level rule holds, its
train runs while any train rule holds, each stopped when its own last rule
lets go. A switch-on the board didn't make (late, a clash, an error) is sent
again with each next set while its rule holds; pulses and bursts are not
(their moment has passed).

Safety: the board's outputs are low from power-up; its watchdog switches
everything off when campy-rules says nothing for watchdog_ms (a crash, kill,
unplugged cable, or its decision loop stuck: the heartbeat goes only while
the loop goes round, LOOP_ALIVE_S); campy-rules then starts the rules over
and the session's trains again. campy-rules switches everything off when it
ends (the recording ended, Ctrl-C, an error). Its logs are written by a thread
of their own, so a slow disk holds up neither the decisions nor the
heartbeat. --dry-run opens no port: it logs what it would do. The board
counts camera triggers from 1 from when campy-rules starts, and keeps
counting through pauses, as campy counts its frames (a pause of half a
second or more is logged, kind gap): start campy-rules before the recording
starts, so frame k is campy's.

campy-live ending or restarting mid-recording (campy-session restarts a
hung or dead one) doesn't end campy-rules: what the rules switched on goes
off at once, the session's trains go on, the heartbeat too, and campy-rules
waits for the next campy-live's results feed (logged, kinds live_gone and
live_back), then runs the rules on it from the next sets. The recording goes
on while any camera's live feed that campy-live read (its results say which)
is still that feed, not ended, and its camera process alive (Recording); when
none is, campy-rules ends, everything off. It ends too when campy-live's
results end and that can't be told (no sets yet, a campy-live that doesn't
name its rings), after --live-wait-s without campy-live (default: no limit),
or right away with --live-wait-s 0 (as before November 2026). With no set
for a while and the cameras' feeds ended, it ends without waiting for
campy-live's feed to end. campy-session doesn't start campy-rules, nor
restart it: start it by hand, before the recording.

Output (--out, default campy-rules_<date_time>):
  <out>.npy          per set: frame, its times, each rule's value (-1 unknown)
                     and whether it held
  <out>.events.jsonl per switch: the rule, output, command, frame, and its
                     times: the set's host times, when campy-rules read it,
                     decided, wrote the command, heard the answer, and the
                     board's pin time (board clock, and campy's host clock),
                     after_us: the pin's change after the frame's trigger;
                     and each pulse the board made, with its edges
  <out>.board.txt    every line to and from the board, with host times:
                     every camera trigger's rise ("t <frame> <board us>")
  <out>.json         the summary: counts, the latency of each step (p50, p99,
                     worst), and the board-to-host clock map
All host times are campy's host clock (campy/clock.py), as campy's
hosttimes.npy and campy-live's log.

Needs numpy, pyyaml and pyserial (campy's own requirements): it runs in
campy's environment or campy-live's.
"""

import argparse, itertools, json, math, os, signal, sys, threading, time
from datetime import datetime

import numpy as np

from campy import children, clock, livefeed, liveresults, runstats, strictjson
from campy.frametimes import GrowingNpy, Writer

LOOP_ALIVE_S = 0.1 # the board's heartbeat goes only while the decision loop has gone round this recently
RECORDING_CHECK_S = 0.5 # without campy-live's sets: how often to look whether the recording goes on (Recording)
REJOIN_ATTACH_S = 0.05 # waiting for campy-live to come back: how often to look for its new results feed
REJOIN_POLL_S = 0.01 # and how long each round of that wait sleeps (well within LOOP_ALIVE_S)


class RulesError(Exception):
	pass


# ---------------------------------------------------------------- the file

ACTIONS = ("level", "pulse", "train", "burst")
KINDS = ("level", "train") # what a rule holds on: an output's level, its train


def Load(path):
	import yaml
	with open(path) as f:
		config = yaml.safe_load(f) or {}
	if not isinstance(config, dict):
		raise RulesError("{}: not a YAML mapping".format(path))
	return config


def Need(mapping, key, where, kind=None):
	if not isinstance(mapping, dict) or key not in mapping:
		raise RulesError("{}: needs {}".format(where, key))
	value = mapping[key]
	if kind is not None and not isinstance(value, kind):
		raise RulesError("{}: {} must be a {}".format(where, key, kind.__name__ if isinstance(kind, type) else
			" or ".join(k.__name__ for k in kind)))
	return value


def Number(value, where):
	if isinstance(value, bool) or not isinstance(value, (int, float)):
		raise RulesError("{}: {!r} is not a number".format(where, value))
	return float(value)


class Output:
	def __init__(self, name, spec):
		where = "outputs: {}".format(name)
		if not isinstance(spec, dict):
			raise RulesError("{}: must be a mapping".format(where))
		self.name = name
		self.channel = int(Need(spec, "channel", where, int))
		if not 0 <= self.channel < 8:
			raise RulesError("{}: channel must be 0-7".format(where))
		self.pin = int(spec.get("pin", 2 + self.channel))
		self.group = int(spec.get("group", 0))
		self.max_on_s = Number(spec.get("max_on_s", 0), where)
		self.pulse_ms = Number(spec["pulse_ms"], where) if "pulse_ms" in spec else None
		self.train = None
		if "train" in spec:
			t = spec["train"]
			self.train = dict(every=int(Need(t, "every", where + " train", int)), phase=int(t.get("phase", 0)),
				offset_us=int(t.get("offset_us", 0)), width_us=int(Need(t, "width_us", where + " train", int)))
			if self.train["every"] < 1 or not 0 <= self.train["phase"] < self.train["every"] or \
					self.train["width_us"] < 1 or self.train["offset_us"] < 0:
				raise RulesError("{}: train needs every >= 1, 0 <= phase < every, offset_us >= 0, width_us >= 1".format(where))
		self.run = spec.get("run", "rules")
		if self.run not in ("session", "rules"):
			raise RulesError("{}: run must be session or rules".format(where))
		if self.run == "session" and self.train is None:
			raise RulesError("{}: run: session needs a train".format(where))
		self.default_action = "train" if self.train else "pulse" if self.pulse_ms else "level"


class Description:
	"""
	What the rules need to know about campy-live's results: the cameras, the
	nodes, the number of animals in 3D (0: 2D only), the row's fields. From
	the results feed's description, or a log's summary.
	"""

	def __init__(self, cameras, nodes, animals, fields, frame_rate=0.0):
		self.cameras, self.nodes, self.animals = list(cameras or []), list(nodes or []), int(animals or 0)
		self.fields, self.frame_rate = set(fields), float(frame_rate or 0.0)

	@classmethod
	def From(cls, description, rows_type):
		names = description.get("calibration_names") or description.get("cameras") or []
		return cls(description.get("cameras") or names, description.get("nodes"),
			description.get("animals") if description.get("calibration") else 0, rows_type.names,
			description.get("frame_rate"))


# ---------------------------------------------------------------- conditions

def NodeIndices(node, d, where):
	n = max(1, len(d.nodes))
	if node == "all":
		return list(range(n))
	items = node if isinstance(node, list) else [node]
	out = []
	for item in items:
		if isinstance(item, bool):
			raise RulesError("{}: node {!r}".format(where, item))
		if isinstance(item, int):
			if not 0 <= item < n:
				raise RulesError("{}: node {} (the model has {})".format(where, item, n))
			out.append(item)
		elif item in d.nodes:
			out.append(d.nodes.index(item))
		else:
			raise RulesError("{}: no node {!r} (the model's: {})".format(where, item, ", ".join(d.nodes) or "none named"))
	if not out:
		raise RulesError("{}: no node".format(where))
	return out


def Animals(animal, d, where, allow=("any",)):
	if animal in allow:
		return animal
	items = animal if isinstance(animal, list) else [animal]
	for a in items:
		if isinstance(a, bool) or not isinstance(a, int):
			raise RulesError("{}: animal {!r}: an identity, a list of them, or {}".format(where, animal, " or ".join(allow)))
		if d.animals == 0:
			raise RulesError("{}: animal {}: identities need campy-live's --calibration (3D); without it, "
				"use animal: any with camera:".format(where, a))
		if not 0 <= a < d.animals:
			raise RulesError("{}: animal {} (campy-live tracks {}: 0 to {})".format(where, a, d.animals, d.animals - 1))
	return items


def CameraIndex(name, d, where):
	if isinstance(name, int) and not isinstance(name, bool) and 0 <= name < len(d.cameras):
		return name
	if name in d.cameras:
		return d.cameras.index(name)
	raise RulesError("{}: no camera {!r} (campy-live's: {})".format(where, name, ", ".join(map(str, d.cameras))))


def Mean(points):
	# The mean of the rows that have no NaN, or None
	ok = points[~np.isnan(points).any(axis=-1)]
	return ok.mean(axis=0) if len(ok) else None


class Points:
	"""
	Where an animal's node is in a set: 3D (mm), or in one camera (pixels).
	Candidates gives every (animal label, point or None) a selector picks.
	"""

	def __init__(self, d, camera=None):
		self.d, self.camera = d, camera

	def Of(self, row, animal, nodes):
		if self.camera is None:
			return Mean(row["points3d"][animal][nodes])
		if self.d.animals:
			member = int(row["members"][animal, self.camera])
			if member < 0:
				return None
			return Mean(row["keypoints"][self.camera, member][nodes])
		return Mean(row["keypoints"][self.camera, animal][nodes])

	def Labels(self, row, selector):
		if selector != "any":
			return selector
		if self.d.animals:
			return range(self.d.animals)
		return range(int(row["found"][self.camera]))

	def Candidates(self, row, selector, nodes):
		return [(a, self.Of(row, a, nodes)) for a in self.Labels(row, selector)]


def Any3(results):
	# True if any is True; else None if any is None; else False (and None if none at all)
	results = [r if r is None else bool(r) for r in results]
	if any(r is True for r in results):
		return True
	if not results or any(r is None for r in results):
		return None
	return False


class Region:
	def __init__(self, name, spec, d):
		where = "regions: {}".format(name)
		self.name, self.camera = name, None
		if not isinstance(spec, dict):
			raise RulesError("{}: must be a mapping".format(where))
		if "camera" in spec:
			self.camera = CameraIndex(spec["camera"], d, where)
			poly = np.asarray(Need(spec, "polygon", where, list), float)
			if poly.ndim != 2 or poly.shape[1] != 2 or len(poly) < 3:
				raise RulesError("{}: polygon needs 3 or more [x, y] points (pixels)".format(where))
			self.kind, self.polygon = "polygon", poly
			return
		if d.animals == 0:
			raise RulesError("{}: a 3D region needs campy-live's --calibration; without it, give camera: and "
				"polygon: (pixels)".format(where))
		if "box" in spec:
			box = np.asarray(spec["box"], float)
			if box.shape != (2, 3):
				raise RulesError("{}: box is [[x0, y0, z0], [x1, y1, z1]]".format(where))
			self.kind, self.low, self.high = "box", box.min(axis=0), box.max(axis=0)
		elif "sphere" in spec:
			s = spec["sphere"]
			self.kind, self.center = "sphere", np.asarray(Need(s, "center", where), float)
			self.radius = Number(Need(s, "radius", where), where)
			if self.center.shape != (3,):
				raise RulesError("{}: sphere's center is [x, y, z]".format(where))
		elif "cylinder" in spec:
			c = spec["cylinder"]
			self.kind, self.center = "cylinder", np.asarray(Need(c, "center", where), float)
			self.radius = Number(Need(c, "radius", where), where)
			z = c.get("z", [-math.inf, math.inf])
			self.z = (min(z), max(z))
			if self.center.shape != (2,):
				raise RulesError("{}: cylinder's center is [x, y] (its axis is z)".format(where))
		else:
			raise RulesError("{}: needs box, sphere, cylinder, or camera and polygon".format(where))

	def Contains(self, p, margin=0.0):
		if self.kind == "box":
			return bool(np.all(p >= self.low - margin) and np.all(p <= self.high + margin))
		if self.kind == "sphere":
			return float(np.linalg.norm(p - self.center)) <= self.radius + margin
		if self.kind == "cylinder":
			return float(np.linalg.norm(p[:2] - self.center)) <= self.radius + margin and \
				bool(self.z[0] - margin <= p[2] <= self.z[1] + margin)
		inside = InPolygon(p, self.polygon)
		return bool(inside or (margin > 0 and PolygonDistance(p, self.polygon) <= margin))


def InPolygon(p, poly):
	x, y = p
	inside = False
	n = len(poly)
	for i in range(n):
		x0, y0 = poly[i]
		x1, y1 = poly[(i + 1) % n]
		if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
			inside = not inside
	return inside


def PolygonDistance(p, poly):
	best = math.inf
	for i in range(len(poly)):
		a, b = poly[i], poly[(i + 1) % len(poly)]
		ab = b - a
		t = 0.0 if not ab.any() else min(1.0, max(0.0, float(np.dot(p - a, ab) / np.dot(ab, ab))))
		best = min(best, float(np.linalg.norm(p - (a + t * ab))))
	return best


class InputState:
	"""
	An input of the board (an operant box's sensor), as the rules see it: active now, or went active since the
	set before. Its edges come on the board's reading thread (Edge), the sets on the decision thread (Begin, Value,
	End): an edge between a set's Begin and End counts for the next set too.
	"""

	def __init__(self, spec):
		self.spec = spec          # campy.box.Input: its index, pin, pull, active, debounce
		self.name = spec.name
		self.active = False
		self.known = False        # the board said its level (False: dry run, replay, no board)
		self.rises = 0            # how many times it went active
		self.seen = self.taken = 0
		self.count = 0

	def Set(self, active):
		# Its level as the board set it up: known, not an edge
		self.known, self.active = True, bool(active)

	def Edge(self, active):
		self.known = True
		if active:
			self.rises += 1
			self.count += 1
		self.active = bool(active)

	def Begin(self):
		self.taken = self.rises

	def Value(self):
		if not self.known:
			return None
		return self.active or self.taken > self.seen

	def End(self):
		self.seen = self.taken


def Condition(spec, d, regions, where, inputs=None):
	"""
	The condition spec as a function (row, holding) -> True, False or None
	(unknown); holding: whether its rule holds now (hysteresis). inputs: the
	board's inputs by name (InputState), for input conditions.
	"""
	if not isinstance(spec, dict) or len(spec) != 1:
		raise RulesError("{}: one of inside, near, test, input, all, any, not".format(where))
	(kind, args), = spec.items()
	if kind == "input":
		name = args.get("name") if isinstance(args, dict) else args
		if not inputs or name not in inputs:
			raise RulesError("{}: no input {!r} (inputs: {})".format(where, name, ", ".join(inputs or {}) or "none"))
		state = inputs[name]
		return lambda row, holding: state.Value()
	if kind in ("all", "any"):
		if not isinstance(args, list) or not args:
			raise RulesError("{}: {} takes a list of conditions".format(where, kind))
		parts = [Condition(a, d, regions, "{} {}[{}]".format(where, kind, i), inputs) for i, a in enumerate(args)]
		if kind == "any":
			return lambda row, holding: Any3(p(row, holding) for p in parts)

		def All(row, holding):
			results = [p(row, holding) for p in parts]
			if any(r is False for r in results):
				return False
			if any(r is None for r in results):
				return None
			return True
		return All
	if kind == "not":
		part = Condition(args, d, regions, where + " not", inputs)

		def Not(row, holding):
			r = part(row, not holding)
			return None if r is None else not r
		return Not
	if kind == "test":
		every = int(Need(args, "every", where, int))
		if every < 1:
			raise RulesError("{}: test's every must be 1 or more".format(where))
		return lambda row, holding: int(row["frame"]) % every == 0
	if kind == "inside":
		name = Need(args, "region", where)
		if name not in regions:
			raise RulesError("{}: no region {!r}".format(where, name))
		region = regions[name]
		points = Points(d, region.camera)
		animal = Animals(args.get("animal", "any"), d, where)
		nodes = NodeIndices(args.get("node", "all"), d, where)
		margin = Number(args.get("margin", 0), where)

		def Inside(row, holding):
			m = margin if holding else 0.0
			return Any3(None if p is None else region.Contains(p, m) for _, p in points.Candidates(row, animal, nodes))
		return Inside
	if kind == "near":
		a, b = Need(args, "a", where, dict), Need(args, "b", where, dict)
		camera = CameraIndex(args["camera"], d, where) if "camera" in args else None
		if camera is None and d.animals == 0:
			raise RulesError("{}: near in 3D needs campy-live's --calibration; without it, give camera:".format(where))
		points = Points(d, camera)
		a_animal = Animals(a.get("animal", "any"), d, where + " a")
		b_animal = Animals(b.get("animal", "other"), d, where + " b", allow=("any", "other", "same"))
		a_nodes = NodeIndices(Need(a, "node", where + " a"), d, where + " a")
		b_nodes = NodeIndices(Need(b, "node", where + " b"), d, where + " b")
		within = Number(Need(args, "within", where), where)
		leave = Number(args.get("leave", within), where)
		if leave < within:
			raise RulesError("{}: leave must be at least within".format(where))

		def Near(row, holding):
			limit = leave if holding else within
			results = []
			for la, pa in points.Candidates(row, a_animal, a_nodes):
				if b_animal == "same":
					labels = [la]
				elif b_animal == "other":
					labels = [x for x in points.Labels(row, "any") if x != la]
				else:
					labels = [x for x in points.Labels(row, b_animal) if x != la or a_nodes != b_nodes]
				for lb in labels:
					pb = points.Of(row, lb, b_nodes)
					results.append(None if pa is None or pb is None else float(np.linalg.norm(pa - pb)) <= limit)
			return Any3(results)
		return Near
	raise RulesError("{}: unknown condition {!r} (inside, near, test, input, all, any, not)".format(where, kind))


# ---------------------------------------------------------------- rules

class Rule:
	def __init__(self, spec, d, regions, outputs, index, inputs=None):
		where = "rules[{}]".format(index)
		if not isinstance(spec, dict):
			raise RulesError("{}: must be a mapping".format(where))
		self.name = str(spec.get("name", "rule{}".format(index)))
		where = "rule {}".format(self.name)
		self.condition = Condition(Need(spec, "when", where), d, regions, where, inputs)
		self.enter_ns = int(Number(spec.get("enter_ms", 0), where) * 1e6)
		self.exit_ns = int(Number(spec.get("exit_ms", 0), where) * 1e6)
		self.missing_ns = int(Number(spec.get("missing_ms", 100), where) * 1e6)
		self.refractory_ns = int(Number(spec.get("refractory_s", 0), where) * 1e9)
		do = Need(spec, "do", where, dict)
		name = Need(do, "output", where + " do")
		if name not in outputs:
			raise RulesError("{}: no output {!r}".format(where, name))
		self.output = outputs[name]
		if self.output.run == "session":
			raise RulesError("{}: output {} runs the whole session (run: session); rules can't switch it".format(
				where, name))
		self.action = do.get("action", self.output.default_action)
		if self.action not in ACTIONS:
			raise RulesError("{}: action must be one of {}".format(where, ", ".join(ACTIONS)))
		if self.action in ("train", "burst") and self.output.train is None:
			raise RulesError("{}: output {} has no train".format(where, name))
		self.pulse_us = int(Number(do.get("pulse_ms", self.output.pulse_ms or 0), where) * 1000)
		if self.action == "pulse" and self.pulse_us < 1:
			raise RulesError("{}: a pulse needs pulse_ms (in do: or on the output)".format(where))
		self.pulses = int(do.get("pulses", 1))
		if self.action == "burst" and self.pulses < 1:
			raise RulesError("{}: burst needs pulses >= 1".format(where))
		self.started_ns = None  # when it last started holding
		self.Reset()

	def Reset(self):
		# From the next set as from the first (Rules.Release), except refractory_s: it still counts from when the rule
		# last started holding, as the sets' host times go on through a stale feed, the watchdog and campy-live's restart
		self.holding = False
		self.value = None       # the condition's last known value
		self.known_ns = None    # when it was last known
		self.since_ns = None    # since when it has said what it says now

	def Step(self, row, t):
		"""
		The row's value (True, False, None) and the change: "start", "stop" or None.
		"""
		raw = self.condition(row, self.holding)
		value = raw
		if raw is None:
			if self.known_ns is None or t - self.known_ns > self.missing_ns:
				value = False
			else:
				value = self.value
		else:
			self.known_ns = t
		if value != self.value or self.since_ns is None:
			self.since_ns = t
		self.value = value
		change = None
		if value and not self.holding and t - self.since_ns >= self.enter_ns and \
				(self.started_ns is None or t - self.started_ns >= self.refractory_ns):
			self.holding, self.started_ns, change = True, t, "start"
		elif not value and self.holding and t - self.since_ns >= self.exit_ns:
			self.holding, change = False, "stop"
		return raw, change


class Retry(tuple):
	# A switch-on sent again (Rules.Retries): a command tuple like the others
	pass


class Rules:
	"""
	The whole file, bound to campy-live's results: Step(row) gives the
	commands for the board, as (rule, output, action, command text, needs frame).
	"""

	def __init__(self, config, d):
		self.d = d
		self.board = dict(config.get("board") or {})
		outputs = config.get("outputs") or {}
		if not isinstance(outputs, dict) or not outputs:
			raise RulesError("outputs: none")
		self.outputs = {name: Output(name, spec) for name, spec in outputs.items()}
		channels = [o.channel for o in self.outputs.values()]
		if len(set(channels)) != len(channels):
			raise RulesError("outputs: two outputs on one channel")
		regions = config.get("regions") or {}
		self.regions = {name: Region(name, spec, d) for name, spec in regions.items()}
		self.by_input = {}
		self.inputs = self.Inputs(config.get("inputs"))
		rules = config.get("rules") or []
		if not isinstance(rules, list):
			raise RulesError("rules: must be a list")
		self.rules = [Rule(spec, d, self.regions, self.outputs, i, self.inputs) for i, spec in enumerate(rules)]
		names = [r.name for r in self.rules]
		if len(set(names)) != len(names):
			raise RulesError("rules: two rules of the same name")
		self.by_name = {r.name: r for r in self.rules}
		# The rules holding each output's level, and each output's train: apart, as the board keeps them apart ("off"
		# doesn't stop a train, "run ch 0" doesn't clear a level), so each is stopped when its own last rule lets go
		self.holders = {(name, kind): set() for name in self.outputs for kind in KINDS}
		# Switch-ons the board didn't make (refused as late, a clash, an error): sent again with the next set while
		# their rules still hold (Retries). Added by the board's reading thread, so behind a lock
		self.retry = set()
		self.retry_lock = threading.Lock()

	def Inputs(self, spec):
		# An operant box's sensors (campy-box's inputs:, campy/box.py), by name, with their pins checked against the
		# outputs' and the camera trigger's
		if not spec:
			return {}
		from campy import box
		if not isinstance(spec, dict):
			raise RulesError("inputs: must be a mapping")
		inputs = {name: InputState(box.Input(name, s)) for name, s in spec.items()}
		indices = [i.spec.index for i in inputs.values()]
		if len(set(indices)) != len(indices):
			raise RulesError("inputs: two of them have the same number")
		pins = {o.pin: "output " + o.name for o in self.outputs.values()}
		trigger = self.board.get("trigger_pin", 10)
		if trigger is not None and str(trigger).lower() != "none":
			pins.setdefault(box.Pin(trigger, "board: trigger_pin"), "the camera trigger")
		for i in inputs.values():
			if i.spec.pin in pins:
				raise RulesError("input {} and {} are both on {}".format(i.name, pins[i.spec.pin], box.PinName(i.spec.pin)))
			pins[i.spec.pin] = "input " + i.name
		self.by_input.update({i.spec.index: i for i in inputs.values()})
		return inputs

	def Holding(self, o, kind):
		return bool(self.holders[(o.name, kind)])

	def Free(self):
		# Before anything else: what another program (campy-box) left on the board, its camera pulses' settings, inputs,
		# servos and links, freed
		# (alloff has disarmed the links and sent the servos home already). Only for a board with inputs: version 3
		# (cam 0 is refused while the board makes camera pulses for another recording, which campy-rules won't stop)
		out = ["cam 0", "camsync -1"] + ["link {} -1".format(n) for n in range(8)] + ["in {} -1".format(n) for n in range(8)]
		out += ["servo {} -1".format(n) for n in range(4)]
		if self.inputs:
			# A channel the rules don't use keeps no pin: its default (D2-D9) can't be an input's
			used = {o.channel for o in self.outputs.values()}
			out += ["pin {} -1".format(ch) for ch in range(8) if ch not in used]
		return out

	def Setup(self):
		# The board's commands for the outputs (pins, groups, limits, trains), then the inputs
		out = []
		for o in self.outputs.values():
			out += ["pin {} {}".format(o.channel, o.pin), "group {} {}".format(o.channel, o.group),
				"max {} {}".format(o.channel, int(o.max_on_s * 1e6))]
			if o.train:
				t = o.train
				out.append("train {} {} {} {} {}".format(o.channel, t["every"], t["phase"], t["offset_us"], t["width_us"]))
		out += [i.spec.Command() for i in self.inputs.values()]
		return out

	def SessionStarts(self):
		return [(None, o, "train", "run {} -1".format(o.channel), False) for o in self.outputs.values()
			if o.run == "session"]

	def Step(self, row, t):
		values, commands = [], []
		for i in self.inputs.values():
			i.Begin()
		for rule in self.rules:
			raw, change = rule.Step(row, t)
			values.append(-1 if raw is None else int(raw))
			if change:
				commands += self.Act(rule, change)
		for i in self.inputs.values():
			i.End()
		return values, commands + self.Retries(commands)

	def Act(self, rule, change):
		o, ch = rule.output, rule.output.channel
		if rule.action in KINDS:
			key = (o.name, rule.action)
			holders = self.holders[key]
			was = bool(holders)
			if change == "start":
				holders.add(rule.name)
			else:
				holders.discard(rule.name)
			if bool(holders) == was:
				return []
			if not holders:
				with self.retry_lock:
					self.retry.discard(key)
			return [self.Switch(rule, o, rule.action, bool(holders))]
		if change != "start":
			return []
		if rule.action == "pulse":
			return [(rule, o, "pulse", "pulse {} {}".format(ch, rule.pulse_us), True)]
		if self.Holding(o, "train"):
			return [] # its train runs already
		return [(rule, o, "burst", "run {} {}".format(ch, rule.pulses), True)]

	@staticmethod
	def Switch(rule, o, kind, on):
		# The command that starts or stops an output's level or train
		ch = o.channel
		if kind == "level":
			return (rule, o, "on" if on else "off", "on {}".format(ch) if on else "off {}".format(ch), on)
		return (rule, o, "run" if on else "stop", "run {} {}".format(ch, -1 if on else 0), on)

	@staticmethod
	def Kind(action):
		# The kind a command switches on, if it is a switch-on that is held (and so sent again if refused)
		return {"on": "level", "run": "train"}.get(action)

	def Refused(self, output_name, kind):
		# The board didn't make a switch-on (or campy-rules didn't send it: late): send it again with the next set
		# while a rule still holds it. From any thread
		with self.retry_lock:
			self.retry.add((output_name, kind))

	def Retries(self, commands):
		# The switch-ons refused before, again, for those still held (and not just sent anew)
		with self.retry_lock:
			if not self.retry:
				return []
			keys, self.retry = self.retry, set()
		sent = {(c[1].name, self.Kind(c[2])) for c in commands}
		out = []
		for name, kind in sorted(keys):
			holders = self.holders[(name, kind)]
			if holders and (name, kind) not in sent:
				rule = self.by_name[min(holders)]
				out.append(Retry(self.Switch(rule, self.outputs[name], kind, True)))
		return out

	def Release(self):
		# Everything the rules switched on, off (rules start over): both of an output's stops whenever any of its
		# rules held, whichever it was
		commands = []
		for o in self.outputs.values():
			if any(self.holders[(o.name, kind)] for kind in KINDS):
				commands.append((None, o, "off", "off {}".format(o.channel), False))
				if o.train:
					commands.append((None, o, "stop", "run {} 0".format(o.channel), False))
				for kind in KINDS:
					self.holders[(o.name, kind)].clear()
		with self.retry_lock:
			self.retry.clear()
		for rule in self.rules:
			rule.Reset()
		return commands


# ---------------------------------------------------------------- live

class LogFile:
	# The board log as the board writes it (Board's log.write), its writes on campy-rules' writer thread
	def __init__(self, runner, f):
		self.runner, self.f = runner, f

	def write(self, text):
		self.runner.Write(self.f, text)

def RowNs(row):
	# When the set was complete on the host: its last camera's frame
	host = np.asarray(row["host_ns"])
	host = host[host > 0]
	return int(host.max()) if len(host) else 0


def Stats(values):
	if isinstance(values, runstats.Values):
		v = values.Exact()
		if v is None: # past runstats.EXACT values: from its histogram, within 0.05%
			a = values.Approx((50, 99))
			return dict(n=a["n"], p50=round(a[50], 3), p99=round(a[99], 3), max=round(a["max"], 3),
				min=round(a["min"], 3))
	else:
		v = np.asarray([x for x in values if x is not None], float)
	if not len(v):
		return None
	return dict(n=int(len(v)), p50=round(float(np.percentile(v, 50)), 3), p99=round(float(np.percentile(v, 99)), 3),
		max=round(float(v.max()), 3), min=round(float(v.min()), 3))


class FrameMatch:
	"""
	campy's frame F as the board's frame k: k = F + offset. Both count the
	camera triggers from 1, and on through pauses, so offset is 0 when the
	board was counting before the recording started and campy grabbed every
	frame. Checked against the
	times, once the board has reported each frame's trigger: a frame reaches
	the host some milliseconds after its trigger (exposure, readout, USB),
	about the same every frame. A delay below zero, of more than 3 frames, or
	a frame 3 or more ahead of the board's count means another offset: then
	the one that puts the frame after its trigger by less than a frame, said
	each time it changes.
	"""

	def __init__(self, board, say, event=None):
		self.board, self.say, self.event = board, say, event
		self.offset, self.changes = 0, 0
		self.delays = runstats.Values(np.float64, floor=0.001) # ms, frame's trigger to the host: bounded memory
		self.waiting = [] # (frame, row_first_ns) whose trigger the board hasn't reported yet
		self.rows = 0
		self.said_no_trigger = False

	def K(self, frame, row_first_ns):
		"""
		The board's frame for campy's frame, and its trigger's host time (None
		if not known yet).
		"""
		self.Check()
		self.rows += 1
		if row_first_ns:
			self.waiting.append((frame, row_first_ns))
		if not self.board.rises and self.rows >= 120 and not self.said_no_trigger:
			self.said_no_trigger = True
			self.say("campy-rules: the board has seen no camera trigger: is its trigger pin wired to the camera "
				"trigger (board: trigger_pin)? Without it the board can't say how long after each frame it "
				"switched, nor refuse old commands.")
		k = frame + self.offset
		return k, self.board.RiseHostNs(k)

	def Check(self):
		if not self.board.rises:
			return
		newest = next(reversed(self.board.rises))
		period = self.Period()
		slack = max(2_000_000, self.board.clock.RoundTrip() or 0) # the clock map: within half a round trip
		keep = []
		for frame, first in self.waiting:
			k = frame + self.offset
			rise = self.board.RiseHostNs(k)
			if rise is None:
				if k > newest + 3:
					self.Rematch(frame, first, period, slack)
				elif k > newest:
					keep.append((frame, first)) # its trigger's report is on its way
				continue
			delay = first - rise
			if period and (delay < -slack or delay > 3 * period):
				self.Rematch(frame, first, period, slack)
			else:
				self.delays.Add(delay / 1e6)
		self.waiting = keep[-64:]

	def Rematch(self, frame, first, period, slack):
		if not period:
			return
		best = None
		for k, us in list(self.board.rises.items()):
			r = self.board.clock.HostNs(us)
			if r is not None and -slack <= first - r < period - slack and (best is None or r > best[1]):
				best = (k, r)
		if best is None or best[0] - frame == self.offset:
			return
		if self.event is not None:
			self.event(dict(kind="rebase", frame=frame, board_frame=best[0], offset=best[0] - frame,
				was=self.offset))
		self.offset = best[0] - frame
		self.changes += 1
		self.say("campy-rules: campy's frame {} is the board's trigger {} (offset {}): the board's count and "
			"campy's differ (the board started counting after the recording, or campy missed a frame). Matched "
			"by time, to a frame.".format(frame, best[0], self.offset))

	def Period(self):
		# The last 8 rises only: copying all of them (outputs.RISES, 4096) cost ~60 us a row, between deciding and
		# sending
		rises = list(itertools.islice(reversed(self.board.rises.values()), 8))[::-1]
		if len(rises) < 2:
			return None
		return int(np.median(np.diff(rises))) * 1000


class Recording:
	"""
	Whether campy's recording goes on, from its cameras' live feeds: the rings
	campy-live reads, which its results' description names. Snap takes them
	once a results feed has sent sets (so the cameras record then); the
	recording goes on while any of those rings is still the same ring (its
	id), not ended, and its camera process alive. A camera's ring that ended,
	is gone, was made again (the next recording) or was left by a campy that
	was killed: that camera's recording ended. Nothing taken (no sets yet, or
	a campy-live too old to name its rings): unknown, and campy-rules ends with
	campy-live, as before.
	"""

	def __init__(self):
		self.rings = {} # name: ring id

	def Known(self):
		return bool(self.rings)

	@staticmethod
	def _Recording(reader):
		# Not ended, and its camera process (its description's pid, campy from October 2026 on) still there
		if reader.Ended() or reader.Replaced():
			return False
		try:
			pid = int(strictjson.loads(reader.Description() or "{}").get("pid") or 0)
		except (ValueError, TypeError, AttributeError):
			pid = 0
		return not pid or children.Alive(pid)

	def Snap(self, names):
		rings = {}
		for name in names or []:
			try:
				reader = livefeed.RingReader(name)
			except (OSError, ValueError):
				continue
			try:
				if self._Recording(reader):
					rings[name] = reader.ringId
			finally:
				reader.Close()
		self.rings = rings

	def GoesOn(self):
		for name, ring_id in self.rings.items():
			try:
				reader = livefeed.RingReader(name)
			except (OSError, ValueError): # gone, or the next recording's being made
				continue
			try:
				if reader.ringId == ring_id and self._Recording(reader):
					return True
			finally:
				reader.Close()
		return False


class Runner:
	"""
	The live loop: rows from the results feed, through the rules, to the board.
	"""

	def __init__(self, args, config):
		self.args, self.config = args, config
		self.stop = threading.Event()
		self.say = lambda text: print(text, flush=True)
		self.lock = threading.Lock()
		self.events = None
		self.board = None
		self.latency = dict(trigger_to_host=[], host_to_pose=[], pose_to_read=[], read_to_decided=[],
			decided_to_written=[], written_to_pin=[], trigger_to_pin=[], host_to_pin=[])
		self.counts = dict(rows=0, commands=0, late_host=0, late_board=0, clash=0, pulses=0, stale=0, errors=0,
			watchdog=0, retries=0, gaps=0, live_gaps=0, inputs=0)
		self.rules = None
		self.results = None   # campy-live's results feed now (None while waiting for campy-live to come back)
		self.live_gaps = []   # each time campy-live's results were gone while the recording went on (Rejoin)
		self.writer = None    # the log files' writes, on a thread of their own (frametimes.Writer)
		self.beat = None      # when the decision loop last went round (time.monotonic), None: not looping yet
		self.watchdog_fired = threading.Event() # the board's watchdog switched everything off: Loop recovers
		self.said = set()     # the board's errors said already (a retried command's error is said once)

	def Alive(self):
		# Whether the board may hear the heartbeat: the decision loop isn't stuck. Else the board's watchdog fires
		return self.beat is None or time.monotonic() - self.beat < LOOP_ALIVE_S

	def Write(self, f, text):
		# A line for a log file: the writer thread writes it, so a slow disk never holds up the decisions, the
		# board's reading thread or its heartbeat
		if self.writer is None:
			f.write(text)
		else:
			self.writer.Do(f.write, text)

	def Event(self, record):
		text = strictjson.dumps(record, separators=(",", ":")) + "\n"
		with self.lock:
			if self.events is not None:
				self.Write(self.events, text)

	def Listen(self, kind, fields, heard_ns, line):
		if kind == "answer":
			answer = fields
			record = answer.record
			if record is None:
				return
			record.update(sent_ns=answer.sent_ns, written_ns=answer.written_ns, answer=answer.line,
				heard_ns=answer.heard_ns)
			words = answer.line.split()
			if words[:1] == ["ok"] and words[1:2] and words[1] in ("on", "off", "pulse", "run"):
				pos = {"on": 3, "off": 3, "pulse": 3, "run": 4}[words[1]]
				t = int(words[pos])
				record["pin_us"] = t
				record["pin_ns"] = self.board.clock.HostNs(t)
				if words[1] != "off":
					after = int(words[-1])
					record["after_us"] = after if after >= 0 else None
				if record.get("trigger_ns") is None and record.get("k") is not None:
					record["trigger_ns"] = self.board.RiseHostNs(record["k"]) # reported since
				self.Latency(record)
			elif words[:2] == ["err", "late"]:
				self.counts["late_board"] += 1
			elif words[:2] == ["err", "clash"]:
				self.counts["clash"] += 1
			elif words[:1] == ["err"]:
				self.counts["errors"] += 1
				said = (record.get("output"), answer.line)
				if said not in self.said:
					self.said.add(said)
					self.say("campy-rules: the board refused {!r}: {}".format(answer.command, answer.line))
			if words[:1] == ["err"]:
				self.Refused(record)
			self.Event(record)
		elif kind == "p":
			ch, k, rise, fall = (int(x) for x in fields[:4])
			self.counts["pulses"] += 1
			self.Event(dict(kind="pulse", channel=ch, board_frame=k, rise_us=rise, fall_us=fall,
				rise_ns=self.board.clock.HostNs(rise), fall_ns=self.board.clock.HostNs(fall)))
		elif kind in ("clash", "e", "wd"):
			if kind == "clash":
				self.counts["clash"] += 1
			if kind == "wd":
				self.counts["watchdog"] += 1
				self.say("campy-rules: the board's watchdog switched everything off ({}): the rules start over and "
					"the session's trains start again.".format(line))
				self.watchdog_fired.set()
			self.Event(dict(kind=kind, line=line, heard_ns=heard_ns))
		elif kind == "i":
			# An input's edge (an operant box's sensor): the rules see it with the next set
			index, active, t, k, after = (int(x) for x in fields[:5])
			state = self.rules.by_input.get(index) if self.rules is not None and self.rules.inputs else None
			if state is not None:
				state.Edge(active)
			self.counts["inputs"] += 1
			self.Event(dict(kind="input", input=state.name if state else index, active=active, board_us=t,
				host_ns=self.board.clock.HostNs(t) if self.board is not None else None, board_frame=k or None,
				after_us=after if after >= 0 else None, heard_ns=heard_ns))
		elif kind == "gap":
			# The trigger paused (the board keeps counting its frames: campy's frame k stays the board's k)
			self.counts["gaps"] += 1
			k, gap_us = (int(x) for x in fields[:2])
			self.Event(dict(kind="gap", board_frame=k, gap_ms=gap_us / 1e3, heard_ns=heard_ns, line=line))
		elif kind == "relink":
			self.Event(dict(kind="relink", what=fields[0], detail=fields[1], heard_ns=heard_ns))
			if fields[0] == "down":
				self.say("campy-rules: the board's USB link stalled ({}): opening it again.".format(fields[1]))
			else:
				# Over the board's watchdog: it switched everything off; the rules start over, as after "wd"
				self.say("campy-rules: the board's link is back after {:.1f} s: the rules start over and the session's "
					"trains start again.".format(fields[1]))
				self.counts["relinks"] = self.counts.get("relinks", 0) + 1
				self.watchdog_fired.set()
		elif kind == "lost":
			self.say("campy-rules: {}. The board switches its outputs off by itself (its watchdog). "
				"Stopping.".format(fields[0]))
			self.stop.set()

	def Latency(self, r):
		if r.get("action") in ("off", "stop") or r.get("frame") is None:
			return
		L = self.latency
		ms = lambda a, b: None if a is None or b is None else (b - a) / 1e6
		L["trigger_to_host"].append(ms(r.get("trigger_ns"), r.get("row_first_ns")))
		L["host_to_pose"].append(ms(r.get("row_ns"), r.get("pose_ns")))
		L["pose_to_read"].append(ms(r.get("pose_ns"), r.get("read_ns")))
		L["read_to_decided"].append(ms(r.get("read_ns"), r.get("decided_ns")))
		L["decided_to_written"].append(ms(r.get("decided_ns"), r.get("written_ns")))
		L["written_to_pin"].append(ms(r.get("written_ns"), r.get("pin_ns")))
		L["host_to_pin"].append(ms(r.get("row_ns"), r.get("pin_ns")))
		if r.get("after_us") is not None:
			L["trigger_to_pin"].append(r["after_us"] / 1e3)

	def Refused(self, record):
		# A switch-on that a rule holds didn't happen: sent again with the next set while the rule still holds
		kind = Rules.Kind(record.get("action"))
		if kind is not None and record.get("rule") is not None and self.rules is not None:
			self.rules.Refused(record["output"], kind)

	def Send(self, rule, output, action, text, framed, row_info=None):
		record = dict(kind="command", rule=rule.name if rule else None, output=output.name, channel=output.channel,
			action=action, command=text)
		if row_info:
			record.update(row_info)
		if framed and row_info and row_info.get("k") is not None and self.board is not None:
			bound_ns = self.bound_ms * 1e6
			since = row_info.get("trigger_ns") or row_info.get("row_ns")
			if bound_ns and since and row_info["decided_ns"] - since > bound_ns:
				self.counts["late_host"] += 1
				record.update(answer="late (campy-rules: {:.1f} ms after the frame)".format(
					(row_info["decided_ns"] - since) / 1e6))
				self.Refused(record)
				self.Event(record)
				return
			text = "{} {}".format(text, row_info["k"])
			record["command"] = text
		self.counts["commands"] += 1
		if record.get("retry"):
			self.counts["retries"] += 1
		if self.board is None:
			record["answer"] = "dry run"
			self.Event(record)
			return
		self.board.Command(text, record) # the listener completes the record with the answer

	def Run(self):
		args = self.args
		out = args.out or "campy-rules_{}".format(datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
		name = self.name = liveresults.RingName(args.name)
		results = self.results = self.Attach(name)
		if results is None:
			return 1
		d = Description.From(results.description, results.rows_type)
		try:
			rules = Rules(self.config, d)
		except RulesError as e:
			results.Close()
			self.results = None
			self.say("campy-rules: {}".format(e))
			return 2
		board_cfg = rules.board
		self.bound_ms = args.bound_ms if args.bound_ms is not None else float(board_cfg.get("bound_ms", 40))
		rows_type = np.dtype([("frame", "<i8"), ("row_ns", "<i8"), ("pose_ns", "<i8"), ("read_ns", "<i8"),
			("decided_ns", "<i8"), ("board_frame", "<i8"), ("values", "<i1", (max(1, len(rules.rules)),)),
			("holding", "?", (max(1, len(rules.rules)),))])
		self.rules = rules
		log = GrowingNpy(out + ".npy", rows_type, header_bytes=1024)
		self.events = open(out + ".events.jsonl", "x")
		self.writer = Writer("campy-rules logs", out + "'s logs")
		boardlog = None
		match = None
		try:
			if not args.dry_run:
				from campy.trigger import outputs
				port = args.port or board_cfg.get("port")
				if not port:
					raise RulesError("no port for the outputs board: --port, or board: port: in the rules")
				boardlog = open(out + ".board.txt", "x", buffering=1)
				self.board = outputs.Board(port, log=LogFile(self, boardlog), listener=self.Listen, say=self.say,
					alive=self.Alive, relink=10.0) # the board's USB link, if it stalls, opened again (linkwatch)
				for command in ["alloff"] + rules.Free() + ["trig {}".format(int(board_cfg.get("trigger_pin", 10))),
						"watchdog {}".format(int(board_cfg.get("watchdog_ms", 250))),
						"bound {}".format(int(self.bound_ms * 1000)), "linkwatch 2000"] + rules.Setup():
					words = self.board.Command(command).Ok().split()
					if words[:2] == ["ok", "in"] and len(words) == 5 and int(words[2]) in rules.by_input:
						rules.by_input[int(words[2])].Set(words[4] == "1") # its level now
				match = FrameMatch(self.board, self.say, self.Event)
			self.say("campy-rules: {} rules on {} outputs, {}; results from {} ({} cameras, {}).".format(
				len(rules.rules), len(rules.outputs), "dry run (no board)" if self.board is None else
				"board on {}".format(args.port or board_cfg.get("port")), name, len(d.cameras),
				"{} animals in 3D".format(d.animals) if d.animals else "2D"))
			for rule, o, action, text, framed in rules.SessionStarts():
				self.Send(rule, o, action, text, framed)
			self.Loop(log, match)
		except RulesError as e:
			self.say("campy-rules: {}".format(e))
			return 2
		except Exception as e:
			self.say("campy-rules: stopped by an error: {}: {}. Everything is switched off.".format(
				type(e).__name__, e))
			return 1
		finally:
			if self.board is not None:
				self.board.Close()
			if self.results is not None:
				self.results.Close()
				self.results = None
			try:
				self.writer.Close() # the writes still queued
			except Exception as e:
				self.say("campy-rules: could not write its logs: {}".format(e))
			self.writer = None
			log.Close()
			summary = self.Summary(self.rules, out, match)
			with self.lock:
				self.events.close()
				self.events = None
			if boardlog is not None:
				boardlog.close()
			with open(out + ".json", "x") as f:
				strictjson.dump(summary, f, indent=2)
			self.Report(summary)
			self.say("campy-rules: summary in {}.json.".format(out))
		return 0

	def Attach(self, name):
		deadline = time.monotonic() + self.args.wait_sec
		said = False
		while not self.stop.is_set():
			try:
				return liveresults.Results(name)
			except (FileNotFoundError, ValueError) as e:
				if not said:
					self.say("campy-rules: waiting for campy-live's results ({}).".format(name))
					said = True
			if time.monotonic() > deadline:
				self.say("campy-rules: no results feed {} after {:g} s.".format(name, self.args.wait_sec))
				return None
			time.sleep(0.1)
		return None

	def Loop(self, log, match):
		n, last_row = 0, time.monotonic()
		poll = self.args.poll_ms / 1e3
		stale = self.args.stale_ms / 1e3
		released = True
		end = time.monotonic() + self.args.duration_s if self.args.duration_s else None
		recording = Recording() # whether the recording goes on, from its cameras' feeds: taken once sets come
		snapped = False # this results feed's camera rings taken
		looked = 0.0 # when the recording was last looked at, while no set came
		while not self.stop.is_set():
			self.beat = time.monotonic()
			rules, results = self.rules, self.results
			if self.watchdog_fired.is_set():
				# The board switched everything off (campy-rules was quiet too long): the rules start over from the
				# next sets, and the session's trains start again
				self.watchdog_fired.clear()
				for c in rules.Release() + rules.SessionStarts():
					self.Send(*c)
			latest = results.Latest()
			if latest <= n:
				now = time.monotonic()
				if not released and now - last_row > stale:
					self.counts["stale"] += 1
					self.say("campy-rules: no set from campy-live for {:g} ms: what the rules switched on goes "
						"off.".format(self.args.stale_ms))
					for c in rules.Release():
						self.Send(*c)
					released = True
				if not snapped and n:
					# This feed's sets came, so the cameras it reads record: the rings that say whether the
					# recording goes on when campy-live's results end. Between sets, once a feed
					recording.Snap(results.description.get("rings"))
					snapped = True
				if results.Ended():
					for c in rules.Release(): # what the rules switched on, off now (not after --stale-ms)
						self.Send(*c)
					released = True
					if self.args.live_wait_s <= 0 or not recording.Known() or not recording.GoesOn():
						self.say("campy-rules: campy-live ended{}.".format(", and the recording with it"
							if recording.Known() and self.args.live_wait_s > 0 else ""))
						return
					results.Close()
					self.results = None
					self.results = self.Rejoin(recording, end)
					if self.results is None:
						return
					self.Rebind(self.results)
					n, last_row, snapped = 0, time.monotonic(), False
					continue
				if released and recording.Known() and now - looked >= RECORDING_CHECK_S:
					# No set for a while (between bouts, campy-live hung or killed): if the cameras' feeds ended, the
					# recording did, even if campy-live's results didn't say so
					looked = now
					if not recording.GoesOn():
						self.say("campy-rules: the recording ended (its cameras' live feeds did), and campy-live's "
							"results stopped with it.")
						return
				if end and now > end:
					return
				if poll:
					time.sleep(poll)
				continue
			first = max(n + 1, latest - results.slots + 1)
			for m in range(first, latest + 1):
				self.beat = time.monotonic()
				row = results.Read(m)
				read_ns = clock.HostNs()
				if row is None:
					continue
				self.Row(row, read_ns, rules, log, match)
			n, last_row, released = latest, time.monotonic(), False

	def Rejoin(self, recording, end):
		"""
		campy-live's results ended while the recording goes on (campy-live
		ended, or campy-session restarts it). What the rules switched on is off
		already; the session's trains go on, and so does the board's heartbeat:
		this loop goes round every REJOIN_POLL_S. Returns campy-live's next
		results feed, or None when campy-rules ends instead: the recording
		ended, no campy-live for --live-wait-s, --duration-s, Ctrl-C.
		"""
		since, since_ns = time.monotonic(), clock.HostNs()
		self.counts["live_gaps"] += 1
		gap = dict(gone_ns=since_ns, back_ns=None, gap_s=None, back=False, why=None)
		self.live_gaps.append(gap)
		self.Event(dict(kind="live_gone", heard_ns=since_ns))
		trains = [o.name for o in self.rules.outputs.values() if o.run == "session"]
		self.say("campy-rules: campy-live ended: pose rules off, {}, waiting for campy-live (the recording goes "
			"on).".format("session trains on ({})".format(", ".join(trains)) if trains else "no session trains"))
		looked = tried = 0.0
		why, results = "stopped", None
		while not self.stop.is_set():
			now = self.beat = time.monotonic()
			if self.watchdog_fired.is_set():
				self.watchdog_fired.clear()
				for c in self.rules.Release() + self.rules.SessionStarts():
					self.Send(*c)
			if now - tried >= REJOIN_ATTACH_S:
				tried = now
				results = self.TryAttach(self.name)
				if results is not None:
					why = None
					break
			if now - looked >= RECORDING_CHECK_S:
				looked = now
				if not recording.GoesOn():
					why = "the recording ended"
					break
			if now - since > self.args.live_wait_s:
				why = "no campy-live for {:g} s (--live-wait-s)".format(self.args.live_wait_s)
				break
			if end and now > end:
				why = "--duration-s"
				break
			time.sleep(REJOIN_POLL_S)
		back_ns = clock.HostNs()
		gap.update(back_ns=back_ns, gap_s=round((back_ns - since_ns) / 1e9, 3), back=results is not None, why=why)
		self.Event(dict(kind="live_back", gone_ns=since_ns, heard_ns=back_ns, gap_s=gap["gap_s"],
			back=results is not None, why=why))
		if results is None:
			self.say("campy-rules: {} while campy-live was gone ({:.1f} s): campy-rules ends.".format(why, gap["gap_s"]))
		else:
			self.say("campy-rules: campy-live is back after {:.1f} s: the pose rules again, from the next sets "
				"({}).".format(gap["gap_s"], results.name))
		return results

	@staticmethod
	def TryAttach(name):
		# campy-live's results feed, if one is there and recording (an ended one, still held by a reader: not yet)
		try:
			results = liveresults.Results(name)
		except (OSError, ValueError):
			return None
		if results.Ended():
			results.Close()
			return None
		return results

	def Rebind(self, results):
		"""
		The rules on a new campy-live's results: the same rules when its
		cameras, nodes, animals and rows are the earlier one's; else made again
		for it (a RulesError if they can't be: campy-rules ends, everything
		off), each rule's refractory_s still counting from its last start.
		"""
		old = self.rules
		d = Description.From(results.description, results.rows_type)
		was = old.d
		if (d.cameras, d.nodes, d.animals, d.fields) == (was.cameras, was.nodes, was.animals, was.fields):
			return
		try:
			rules = Rules(self.config, d)
		except RulesError as e:
			raise RulesError("campy-live came back with other results ({}), which the rules don't fit: {}".format(
				results.name, e))
		for rule in rules.rules:
			if rule.name in old.by_name:
				rule.started_ns = old.by_name[rule.name].started_ns
		self.say("campy-rules: campy-live came back with other cameras, nodes or animals: the rules are made "
			"again for it.")
		self.rules = rules

	def Row(self, row, read_ns, rules, log, match):
		self.counts["rows"] += 1
		row_ns = RowNs(row)
		host = np.asarray(row["host_ns"])
		first = int(host[host > 0].min()) if (host > 0).any() else 0
		names = row.dtype.names
		pose_ns = int(row["pose3d_ns"]) if "pose3d_ns" in names else int(row["done_ns"])
		frame = int(row["frame"])
		values, commands = rules.Step(row, row_ns)
		decided_ns = clock.HostNs()
		k, trigger_ns = (frame, None) if match is None else match.K(frame, first)
		info = dict(frame=frame, k=k if self.board is not None else None, row_ns=row_ns, row_first_ns=first,
			pose_ns=pose_ns, read_ns=read_ns, decided_ns=decided_ns, trigger_ns=trigger_ns)
		for c in commands:
			self.Send(*c, row_info=dict(info, retry=True) if isinstance(c, Retry) else info)
		row = np.array([(frame, row_ns, pose_ns, read_ns, decided_ns, k if match else -1,
			values or [0], [r.holding for r in rules.rules] or [False])], log.dtype)
		if self.writer is None:
			log.Append(row)
		else:
			self.writer.Do(log.Append, row) # a write, a header rewrite and a flush: never on the decision thread

	def Summary(self, rules, out, match):
		if match is not None:
			match.Check()
		summary = dict(counts=self.counts, rules=[r.name for r in rules.rules],
			inputs={i.name: dict(input=i.spec.index, pin=i.spec.pin, went_active=i.count) for i in rules.inputs.values()},
			outputs={o.name: dict(channel=o.channel, pin=o.pin, group=o.group, run=o.run, train=o.train)
				for o in rules.outputs.values()},
			bound_ms=getattr(self, "bound_ms", None), dry_run=self.board is None,
			latency_ms={k: Stats(v) for k, v in self.latency.items()},
			frame_offset=match.offset if match else None, frame_offset_changes=match.changes if match else None,
			trigger_to_host_all_ms=Stats(match.delays) if match else None,
			live_gaps=self.live_gaps, # campy-live gone while the recording went on: pose rules off (Rejoin)
			files=dict(rows=out + ".npy", events=out + ".events.jsonl", board=None if self.board is None else
				out + ".board.txt"))
		if self.board is not None and self.board.clock.Ready():
			b, h, slope = self.board.clock.fit
			summary["clock"] = dict(board_us=b, host_ns=h, host_ns_per_board_us=slope,
				round_trip_ms=self.board.clock.RoundTrip() / 1e6, note="host_ns = host_ns + (board_us' - board_us) "
				"* host_ns_per_board_us")
		return summary

	def Report(self, summary):
		c = summary["counts"]
		self.say("campy-rules: {} sets, {} commands ({} refused as late by campy-rules, {} by the board), {} pulses, "
			"{} clashes, {} times stale, watchdog {}.".format(c["rows"], c["commands"], c["late_host"],
			c["late_board"], c["pulses"], c["clash"], c["stale"], c["watchdog"]))
		gaps = summary.get("live_gaps") or []
		if gaps:
			self.say("campy-rules: campy-live was gone {} times while the recording went on: pose rules off for {:.1f} s "
				"in all (events log: live_gone, live_back).".format(len(gaps), sum(g["gap_s"] or 0 for g in gaps)))
		for key, s in summary["latency_ms"].items():
			if s:
				self.say("  {:<20} p50 {:7.2f}  p99 {:7.2f}  worst {:7.2f} ms  ({} commands)".format(key, s["p50"],
					s["p99"], s["max"], s["n"]))


# ---------------------------------------------------------------- replay

def Replay(args, config, say=print):
	"""
	The rules over a finished campy-live log, with no board: what they would
	have switched, and when (by the sets' host times). For tuning a rule file
	on real data.
	"""
	rows = np.load(args.replay, mmap_mode="r")
	summary_path = os.path.splitext(args.replay)[0] + ".json"
	description = {}
	if os.path.exists(summary_path):
		with open(summary_path) as f:
			description = strictjson.load(f)
	else:
		say("campy-rules: no summary {}: nodes by index only, cameras by index.".format(summary_path))
		description = dict(cameras=list(range(rows.dtype["host_ns"].shape[0])))
	if not description.get("calibration") and "points3d" in rows.dtype.names:
		description["calibration"] = "(in the log)"
		description["animals"] = rows.dtype["points3d"].shape[0]
	d = Description.From(description, rows.dtype)
	rules = Rules(config, d)
	out = args.out or os.path.splitext(args.replay)[0] + ".rules"
	counts = {r.name: 0 for r in rules.rules}
	held_ns = {r.name: 0 for r in rules.rules}
	since = {}
	with open(out + ".events.jsonl", "x") as events:
		for row in rows:
			t = RowNs(row)
			values, commands = rules.Step(row, t)
			for rule in rules.rules:
				if rule.holding and rule.name not in since:
					since[rule.name] = t
					counts[rule.name] += 1
				elif not rule.holding and rule.name in since:
					held_ns[rule.name] += t - since.pop(rule.name)
			for rule, o, action, text, framed in commands:
				events.write(strictjson.dumps(dict(kind="command", rule=rule.name if rule else None, output=o.name,
					action=action, command=text, frame=int(row["frame"]), row_ns=t), separators=(",", ":")) + "\n")
	end = RowNs(rows[-1]) if len(rows) else 0
	for name, t in since.items():
		held_ns[name] += end - t
	seconds = (RowNs(rows[-1]) - RowNs(rows[0])) / 1e9 if len(rows) > 1 else 0
	say("campy-rules: replayed {} sets ({:.1f} s) of {}:".format(len(rows), seconds, args.replay))
	for r in rules.rules:
		say("  {:<24} started {:5d} times, held {:8.2f} s".format(r.name, counts[r.name], held_ns[r.name] / 1e9))
	say("campy-rules: commands in {}.events.jsonl.".format(out))
	return dict(started=counts, held_s={k: v / 1e9 for k, v in held_ns.items()})


# ---------------------------------------------------------------- main

def Main(argv=None):
	p = argparse.ArgumentParser(prog="campy-rules", description="TTL outputs from campy-live's live poses. "
		"See python -m pydoc campy.rules.")
	p.add_argument("rules", help="the rules' YAML file")
	p.add_argument("--name", default=None, help="campy's liveFeedName (campy-live's --name)")
	p.add_argument("--port", default="", help="the outputs board's serial port (else board: port: in the rules)")
	p.add_argument("--dry-run", action="store_true", help="no board: log what it would do")
	p.add_argument("--replay", default="", help="a campy-live log (.npy): run the rules over it, no board")
	p.add_argument("--out", default="", help="log files' name, without extension (default campy-rules_<date_time>)")
	p.add_argument("--bound-ms", type=float, default=None, help="refuse to act on a set older than this, from its "
		"frame's trigger (default: board: bound_ms, else 40); 0: no bound")
	p.add_argument("--stale-ms", type=float, default=200.0, help="no set for this long: what the rules switched "
		"on goes off")
	p.add_argument("--poll-ms", type=float, default=0.2, help="how often to look for a new set; 0: all the time "
		"(a whole core)")
	p.add_argument("--wait-sec", type=float, default=float("inf"), help="how long to wait for campy-live's results")
	p.add_argument("--duration-s", type=float, default=0.0, help="stop after this long (0: when the recording ends)")
	p.add_argument("--live-wait-s", type=float, default=float("inf"), help="campy-live gone while the recording goes "
		"on (it ended, or campy-session restarts it): how long to wait for it, the session's trains on and the pose "
		"rules off (default: as long as the recording goes on); 0: end with campy-live, as before")
	args = p.parse_args(argv)
	if args.name is None:
		from campy import livefeed
		args.name = livefeed.DEFAULT_NAME
	try:
		config = Load(args.rules)
		if args.replay:
			Replay(args, config)
			return 0
	except (RulesError, OSError) as e:
		print("campy-rules: {}".format(e))
		return 2
	runner = Runner(args, config)

	def Stop(signum, frame):
		runner.stop.set()
	signal.signal(signal.SIGINT, Stop)
	if hasattr(signal, "SIGTERM"):
		signal.signal(signal.SIGTERM, Stop)
	return runner.Run()


if __name__ == "__main__":
	sys.exit(Main())
