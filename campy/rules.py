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

Outputs: a channel of the board (0-7) on an Arduino pin. With a train, the
board itself makes a pulse of width_us, offset_us after the camera trigger of
every frame k with (k - phase) % every == 0 (photometry excitation: 3 ms on
each frame, two wavelengths on alternate frames); run: session runs it from
start to end, whatever the poses; else rules run it. Outputs of one group are
never high together (the board skips the pulse and says so). max_on_s: the
board switches a level off after that long, whatever campy-rules says.

Rules: when is one condition, or all: [...], any: [...], not: {...}:
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
refractory_s: it doesn't start holding again until that long after it last did.
Several rules on one output: on (or running) while any of them holds.

Timing: each command names the frame its set came from, so the board says
how long after that frame's camera trigger its pin changed, and refuses one
that came more than bound_ms after it (nothing ever follows an old pose).
campy-rules checks the bound too before sending. Switching off is never
refused. If no set comes for --stale-ms (200), what the rules switched on goes
off (the session's trains go on: they don't depend on the poses).

Safety: the board's outputs are low from power-up; its watchdog switches
everything off when campy-rules says nothing for watchdog_ms (a crash, kill
or unplugged cable); campy-rules switches everything off when it ends
(campy-live ended, Ctrl-C, an error). --dry-run opens no port: it logs what
it would do. The board's frame numbers count camera triggers from 1 after a
pause of half a second or more, as campy counts its frames: power the board
and start campy-rules before the recording starts, so frame k is campy's.

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

import argparse, json, math, os, signal, sys, threading, time
from datetime import datetime

import numpy as np

from campy import clock, liveresults, strictjson
from campy.frametimes import GrowingNpy


class RulesError(Exception):
	pass


# ---------------------------------------------------------------- the file

ACTIONS = ("level", "pulse", "train", "burst")


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


def Condition(spec, d, regions, where):
	"""
	The condition spec as a function (row, holding) -> True, False or None
	(unknown); holding: whether its rule holds now (hysteresis).
	"""
	if not isinstance(spec, dict) or len(spec) != 1:
		raise RulesError("{}: one of inside, near, test, all, any, not".format(where))
	(kind, args), = spec.items()
	if kind in ("all", "any"):
		if not isinstance(args, list) or not args:
			raise RulesError("{}: {} takes a list of conditions".format(where, kind))
		parts = [Condition(a, d, regions, "{} {}[{}]".format(where, kind, i)) for i, a in enumerate(args)]
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
		part = Condition(args, d, regions, where + " not")

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
	raise RulesError("{}: unknown condition {!r} (inside, near, test, all, any, not)".format(where, kind))


# ---------------------------------------------------------------- rules

class Rule:
	def __init__(self, spec, d, regions, outputs, index):
		where = "rules[{}]".format(index)
		if not isinstance(spec, dict):
			raise RulesError("{}: must be a mapping".format(where))
		self.name = str(spec.get("name", "rule{}".format(index)))
		where = "rule {}".format(self.name)
		self.condition = Condition(Need(spec, "when", where), d, regions, where)
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
		self.Reset()

	def Reset(self):
		self.holding = False
		self.value = None       # the condition's last known value
		self.known_ns = None    # when it was last known
		self.since_ns = None    # since when it has said what it says now
		self.started_ns = None  # when it last started holding

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
		rules = config.get("rules") or []
		if not isinstance(rules, list):
			raise RulesError("rules: must be a list")
		self.rules = [Rule(spec, d, self.regions, self.outputs, i) for i, spec in enumerate(rules)]
		names = [r.name for r in self.rules]
		if len(set(names)) != len(names):
			raise RulesError("rules: two rules of the same name")
		self.holders = {name: set() for name in self.outputs} # rules holding a level or train

	def Setup(self):
		# The board's commands for the outputs (pins, groups, limits, trains)
		out = []
		for o in self.outputs.values():
			out += ["pin {} {}".format(o.channel, o.pin), "group {} {}".format(o.channel, o.group),
				"max {} {}".format(o.channel, int(o.max_on_s * 1e6))]
			if o.train:
				t = o.train
				out.append("train {} {} {} {} {}".format(o.channel, t["every"], t["phase"], t["offset_us"], t["width_us"]))
		return out

	def SessionStarts(self):
		return [(None, o, "train", "run {} -1".format(o.channel), False) for o in self.outputs.values()
			if o.run == "session"]

	def Step(self, row, t):
		values, commands = [], []
		for rule in self.rules:
			raw, change = rule.Step(row, t)
			values.append(-1 if raw is None else int(raw))
			if change:
				commands += self.Act(rule, change)
		return values, commands

	def Act(self, rule, change):
		o, ch = rule.output, rule.output.channel
		holders = self.holders[o.name]
		if rule.action in ("level", "train"):
			was = bool(holders)
			if change == "start":
				holders.add(rule.name)
			else:
				holders.discard(rule.name)
			if bool(holders) == was:
				return []
			if rule.action == "level":
				return [(rule, o, "on" if holders else "off", "on {}".format(ch) if holders else "off {}".format(ch),
					bool(holders))]
			return [(rule, o, "run" if holders else "stop", "run {} {}".format(ch, -1 if holders else 0), bool(holders))]
		if change != "start":
			return []
		if rule.action == "pulse":
			return [(rule, o, "pulse", "pulse {} {}".format(ch, rule.pulse_us), True)]
		if holders:
			return [] # its train runs already
		return [(rule, o, "burst", "run {} {}".format(ch, rule.pulses), True)]

	def Release(self):
		# Everything the rules switched on, off (rules start over)
		commands = []
		for o in self.outputs.values():
			if self.holders[o.name]:
				commands.append((None, o, "off", "off {}".format(o.channel), False))
				if o.train:
					commands.append((None, o, "stop", "run {} 0".format(o.channel), False))
				self.holders[o.name].clear()
		for rule in self.rules:
			rule.Reset()
		return commands


# ---------------------------------------------------------------- live

def RowNs(row):
	# When the set was complete on the host: its last camera's frame
	host = np.asarray(row["host_ns"])
	host = host[host > 0]
	return int(host.max()) if len(host) else 0


def Stats(values):
	v = np.asarray([x for x in values if x is not None], float)
	if not len(v):
		return None
	return dict(n=int(len(v)), p50=round(float(np.percentile(v, 50)), 3), p99=round(float(np.percentile(v, 99)), 3),
		max=round(float(v.max()), 3), min=round(float(v.min()), 3))


class FrameMatch:
	"""
	campy's frame F as the board's frame k: k = F + offset. Both count the
	camera triggers from 1, so offset is 0 when the board was counting before
	the recording started and campy grabbed every frame. Checked against the
	times, once the board has reported each frame's trigger: a frame reaches
	the host some milliseconds after its trigger (exposure, readout, USB),
	about the same every frame. A delay below zero, of more than 3 frames, or
	a frame 3 or more ahead of the board's count means another offset: then
	the one that puts the frame after its trigger by less than a frame, said
	each time it changes.
	"""

	def __init__(self, board, say):
		self.board, self.say = board, say
		self.offset, self.changes = 0, 0
		self.delays = [] # ms, frame's trigger to the host
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
				self.delays.append(delay / 1e6)
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
		self.offset = best[0] - frame
		self.changes += 1
		self.say("campy-rules: campy's frame {} is the board's trigger {} (offset {}): the board's count and "
			"campy's differ (the board started counting after the recording, or campy missed a frame). Matched "
			"by time, to a frame.".format(frame, best[0], self.offset))

	def Period(self):
		rises = list(self.board.rises.values())[-8:]
		if len(rises) < 2:
			return None
		return int(np.median(np.diff(rises))) * 1000


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
			watchdog=0)

	def Event(self, record):
		with self.lock:
			if self.events is not None:
				self.events.write(strictjson.dumps(record, separators=(",", ":")) + "\n")

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
				self.say("campy-rules: the board refused {!r}: {}".format(answer.command, answer.line))
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
				self.say("campy-rules: the board's watchdog switched everything off ({}).".format(line))
			self.Event(dict(kind=kind, line=line, heard_ns=heard_ns))
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
				self.Event(record)
				return
			text = "{} {}".format(text, row_info["k"])
			record["command"] = text
		self.counts["commands"] += 1
		if self.board is None:
			record["answer"] = "dry run"
			self.Event(record)
			return
		self.board.Command(text, record) # the listener completes the record with the answer

	def Run(self):
		args = self.args
		out = args.out or "campy-rules_{}".format(datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
		name = liveresults.RingName(args.name)
		results = self.Attach(name)
		if results is None:
			return 1
		d = Description.From(results.description, results.rows_type)
		try:
			rules = Rules(self.config, d)
		except RulesError as e:
			results.Close()
			self.say("campy-rules: {}".format(e))
			return 2
		board_cfg = rules.board
		self.bound_ms = args.bound_ms if args.bound_ms is not None else float(board_cfg.get("bound_ms", 40))
		rows_type = np.dtype([("frame", "<i8"), ("row_ns", "<i8"), ("pose_ns", "<i8"), ("read_ns", "<i8"),
			("decided_ns", "<i8"), ("board_frame", "<i8"), ("values", "<i1", (max(1, len(rules.rules)),)),
			("holding", "?", (max(1, len(rules.rules)),))])
		log = GrowingNpy(out + ".npy", rows_type, header_bytes=1024)
		self.events = open(out + ".events.jsonl", "x")
		boardlog = None
		match = None
		try:
			if not args.dry_run:
				from campy.trigger import outputs
				port = args.port or board_cfg.get("port")
				if not port:
					raise RulesError("no port for the outputs board: --port, or board: port: in the rules")
				boardlog = open(out + ".board.txt", "x", buffering=1)
				self.board = outputs.Board(port, log=boardlog, listener=self.Listen, say=self.say)
				for command in ["alloff", "trig {}".format(int(board_cfg.get("trigger_pin", 10))),
						"watchdog {}".format(int(board_cfg.get("watchdog_ms", 250))),
						"bound {}".format(int(self.bound_ms * 1000))] + rules.Setup():
					self.board.Command(command).Ok()
				match = FrameMatch(self.board, self.say)
			self.say("campy-rules: {} rules on {} outputs, {}; results from {} ({} cameras, {}).".format(
				len(rules.rules), len(rules.outputs), "dry run (no board)" if self.board is None else
				"board on {}".format(args.port or board_cfg.get("port")), name, len(d.cameras),
				"{} animals in 3D".format(d.animals) if d.animals else "2D"))
			for rule, o, action, text, framed in rules.SessionStarts():
				self.Send(rule, o, action, text, framed)
			self.Loop(results, rules, log, match)
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
			results.Close()
			log.Close()
			summary = self.Summary(rules, out, match)
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

	def Loop(self, results, rules, log, match):
		n, last_row = 0, time.monotonic()
		poll = self.args.poll_ms / 1e3
		stale = self.args.stale_ms / 1e3
		released = True
		end = time.monotonic() + self.args.duration_s if self.args.duration_s else None
		while not self.stop.is_set():
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
				if results.Ended():
					self.say("campy-rules: campy-live ended.")
					return
				if end and now > end:
					return
				if poll:
					time.sleep(poll)
				continue
			first = max(n + 1, latest - results.slots + 1)
			for m in range(first, latest + 1):
				row = results.Read(m)
				read_ns = clock.HostNs()
				if row is None:
					continue
				self.Row(row, read_ns, rules, log, match)
			n, last_row, released = latest, time.monotonic(), False

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
			self.Send(*c, row_info=info)
		log.Append(np.array([(frame, row_ns, pose_ns, read_ns, decided_ns, k if match else -1,
			values or [0], [r.holding for r in rules.rules] or [False])], log.dtype))

	def Summary(self, rules, out, match):
		if match is not None:
			match.Check()
		summary = dict(counts=self.counts, rules=[r.name for r in rules.rules],
			outputs={o.name: dict(channel=o.channel, pin=o.pin, group=o.group, run=o.run, train=o.train)
				for o in rules.outputs.values()},
			bound_ms=getattr(self, "bound_ms", None), dry_run=self.board is None,
			latency_ms={k: Stats(v) for k, v in self.latency.items()},
			frame_offset=match.offset if match else None, frame_offset_changes=match.changes if match else None,
			trigger_to_host_all_ms=Stats(match.delays) if match else None,
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
	p.add_argument("--duration-s", type=float, default=0.0, help="stop after this long (0: when campy-live ends)")
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
