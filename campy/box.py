"""
campy-box: runs an operant box's task on the outputs board (campy/outputs_board/campy_outputs, version 3):
its sensors in, its servos and TTL outputs out, the task's states in between.

	campy-box task.yaml --port COM7          # start it before the recording, so frame k is campy's
	campy-box task.yaml --check              # check the file and show what it would set up, no board

The task file:

  board:
    port: COM7              # or /dev/ttyACM1 (--port)
    trigger_pin: 10         # the camera trigger, wired to the board's D10; none: no camera
    watchdog_ms: 250        # the board makes the box safe if campy-box goes quiet this long
  inputs:                   # input 0-7: a pin, pull (up, down, none), active (high, low), debounce_ms
    poke_left:  {input: 0, pin: A0, pull: up}          # pull: up means active: low unless it says
    poke_right: {input: 1, pin: A1, pull: up}
    lick:       {input: 2, pin: A2, pull: down, debounce_ms: 0.5}
  outputs:                  # as campy-rules' outputs: channel 0-7, pin, pulse_ms, max_on_s, group, train
    valve: {channel: 0, pin: D2, pulse_ms: 40}
    cue:   {channel: 1, pin: D3, max_on_s: 60}
    mark:  {channel: 2, pin: D4, pulse_ms: 5}
  servos:                   # servo 0-3: pin, min_us, max_us, positions (us), safe (a position), speed_us_per_s
    door: {servo: 0, pin: D11, positions: {closed: 1000, open: 2000}, safe: closed, speed_us_per_s: 2000}
  links:                    # reflexes on the board: an input's edge does something within microseconds
    mark_pokes:  {when: poke_left, do: {output: mark}, armed: always}
    reward_left: {when: poke_left, do: {output: valve}}             # armed by the states that name it
  task:
    start: iti
    end: {trials: 100, minutes: 60}
    states:
      iti:
        do: [{servo: door, to: closed}, {output: cue, action: off}]
        after_s: [8, 12]    # a random time between the two
        next: ready
      ready:
        trial: true         # entering it starts a trial
        do: [{servo: door, to: open}, {output: cue, action: on}]
        arm: {reward_left: 1}   # armed while in this state, for 1 firing
        on: {poke_left: reward, poke_right: iti}
        after_s: 30
        next: iti
      reward:
        do: [{output: cue, action: off}]
        after_s: 2
        next: iti

Inputs: pin is a number, D<n> or A<n>. 1 means active (a beam broken, a nose in the port), whichever level that is
on the wire: active low by default with pull: up, else high. Debouncing is the board's lockout (default 1 ms): the
first edge counts at once.

Servos: positions are pulse widths in microseconds (1000-2000 for most hobby servos); safe is where the servo goes
when nobody is in charge (campy-box's start and end, the board's watchdog). The board can't know where a servo
is, so at the start each servo jumps to its safe position (its pulses stop for a moment while the board is set up).

Links: when (an input), edge (active, the default, or inactive), do (an output with action pulse, the default for
an output with pulse_ms, on, off or train; or a servo with to). armed: always (the whole session) or task (the
default: armed by states). The board fires a link itself, within microseconds of the edge, so a reward never
waits for the computer.

States: do (actions on entering: {output, action: on, off, pulse (ms: to override pulse_ms), train, stop, burst
(pulses: n)}, {servo, to}, {note: text}), arm (links armed while in the state, {link: times} or a list for once
each; -1 until the state ends), on (an input's edge -> a state: {poke_left: reward}, or {poke_left: {to: reward,
edge: inactive, count: 5}} for the 5th such edge in the state), after_s (seconds, or [min, max] for a random time)
and next (a state, a list for a random one, or {state: weight}), trial: true (entering it starts a trial),
end: true (entering it ends the task, after its actions). A state with next and no after_s moves on at once.
Leaving a state disarms the links it armed; outputs and servos stay as they are until a state changes them.

task: start (the first state; default the first one listed), end (trials: the task ends instead of starting
trial N+1; minutes), seed (for the random times and choices; default a new one, logged),
after_watchdog (the state to go to after the board's watchdog made the box safe; default start).

Poses (with campy-live tracking the animals): conditions on the live poses, as campy-rules' (an animal's node in a
region, two animals close: python -m pydoc campy.rules), named, with their own regions:
  live: {name: campy-live, stale_ms: 200}   # campy-live's liveFeedName (--live-name); no set this long: none holds
  regions:
    port: {sphere: {center: [200, 40, 20], radius: 25}}
  poses:
    m2_at_port: {when: {inside: {region: port, animal: 2, node: nose}}, enter_ms: 50, exit_ms: 100}
A pose holds once its condition has been true for enter_ms, and lets go once false for exit_ms; a missing animal
keeps its last value for missing_ms (100), then counts as false. In a state, on: takes a pose as it takes an input:
{m2_at_port: cue} when it starts holding (edge: inactive, when it lets go; count: the n-th time). A pose is a level,
so a state also moves at once if, as it is entered, the pose already holds (edge: inactive: already doesn't). An input's (or a
pose's) move can depend on a pose: {poke_left: {to: reward, if: m2_at_port, else: wrong_mouse}} (unless: the other
way round); without else, an edge that fails its if is ignored. Poses come with campy-live's sets, a few tens of
ms after their frame: a reward that depends on one is the task's (an action), not a link's. The conditions are
checked against campy-live's cameras, nodes and animals when its results first come.

Timing: the board stamps every edge with its own clock (microseconds since power-up) and the camera frame it fell
in (frame 1 is the first camera trigger after campy-box starts: start it before the recording so its frames are
campy's), and how long after that frame's trigger. campy-box maps board times to campy's host clock
(campy/clock.py, as hosttimes.npy), to within about 0.2 ms. A state change caused by an input carries that
input's times; one by a timer, the host time and the last frame the computer had heard of.

With a recording (campy-session's box: section starts it so): --start-with-frames starts the task at the first
camera frame (the board set up and waiting until then, its always-armed links and session trains already on);
--stop-file ends it when the file appears, and --recording-folder when campy-acquire's watch says that recording
ended, so the task ends with the recording even if campy-session is gone; --launcher-pid gives up waiting for the
first frame when campy-session has ended. --ready-file is written once the board is set up, --status-file every
second (the state, the trial, the counts), for campy-session and its panel.

One board for the cameras and the box (docs/design/single-board-box.md): with --camera-fps, the board makes the
camera pulses itself (sketch version 4), and its frames come from them. --camera-pins are the cameras' pins on the
board (campy's digitalPins), --camera-frames how many pulses (recTimeInSec x frameRate; 0 until stopped), and
--sync-pin (with --sync-seed, --sync-pulse-ms, --sync-gap-ms: campy's syncPin, syncSeed, syncPulseMs, syncGapMs) the
sync output for other recorders. The pulses start at once, or with --cameras-when-ready once every camera of the
recording (--recording-folder's session.json) is waiting for them. campy-box then keeps the board's pulses going to
the end of the recording, the task's own end aside: the cameras stop at their count, or at a period's end when the
recording ends (the stop file, the recording's watch, Ctrl-C). If campy-box itself fails, they go on to their count
(the board's own watchdog never stops them; --camera-watch-ms, after that long a silence, does). It writes
<out>.cameras.json (time 0 on the board's and the host's clocks, the period, the count) and sync.json (into the
recording's folder, else <out>.sync.json) for campy-syncmatch: frame k (from 0) rose at time 0 + k x P + P / 2.

Output (--out, default campy-box_<date_time>):
  <out>.events.jsonl  every event: input edges, link firings, state changes, trials, pulses, levels, servo
                      arrivals, commands and their answers, trigger pauses, the watchdog
  <out>.board.txt     every line to and from the board, with host times
  <out>.json          the summary: counts per input, link and state, trials, the clock map
  <out>.task.yaml     the task file, as it was

Needs pyyaml and pyserial (campy's own requirements).
"""

import argparse, os, queue, random, shutil, signal, sys, threading, time
from datetime import datetime

from campy import clock, strictjson
from campy.frametimes import Writer
from campy import rules as rulesmod
from campy.rules import Output, RulesError

LOOP_ALIVE_S = 0.1 # the board's heartbeat goes only while the task's loop has gone round this recently
LOOP_S = 0.02      # the loop's longest sleep: timers are kept to about this
IMMEDIATE_MAX = 100 # states that move on at once, one after another: more is a loop in the task
LOOK_S = 0.5       # how often to look at the stop file, the recording's state and campy-session
LINKWATCH_MS = 2000 # the board re-attaches its USB link after this long without a byte (a heartbeat goes every 50 ms)
RELINK_S = 10.0    # campy-box opens a failed link again for this long (the board is back on USB within about 2.5 s)
STATUS_S = 1.0     # how often the status file is written
# How the task ended by itself, as the summary's "ended" says it (not stopped from outside), besides "<n> trials"
# and "state <name>"
TASK_ENDS = ("time",)
MAX_IN, MAX_SERVO, MAX_LINK, MAX_CH = 8, 4, 8, 8
LINK_WHAT = {"pulse": 1, "on": 2, "off": 3, "move": 4, "train": 5}


class BoxError(RulesError):
	pass


# ---------------------------------------------------------------- the file

def Load(path):
	import yaml
	with open(path) as f:
		config = yaml.safe_load(f) or {}
	if not isinstance(config, dict):
		raise BoxError("{}: not a YAML mapping".format(path))
	return config


def Pin(value, where):
	"""A pin as the board takes it: a number, D<n>, or A<n> (sent as 100 + n)."""
	if isinstance(value, bool):
		raise BoxError("{}: pin {!r} is not a pin".format(where, value))
	if isinstance(value, int):
		if 0 <= value <= 48:
			return value
	elif isinstance(value, str):
		v = value.strip().upper()
		if v.isdigit() and int(v) <= 48:
			return int(v)
		if v[:1] in ("A", "D") and v[1:].isdigit():
			n = int(v[1:])
			if v[0] == "A" and n <= 7:
				return 100 + n
			if v[0] == "D" and n <= 48:
				return n
	raise BoxError("{}: pin {!r}: a number 0-48, D0-D48 or A0-A7".format(where, value))


def PinName(pin):
	return "A{}".format(pin - 100) if pin >= 100 else "D{}".format(pin)


def Seconds(value, where):
	# A time: a number, or [min, max] for a random one between them
	if isinstance(value, bool):
		raise BoxError("{}: {!r} is not a time".format(where, value))
	if isinstance(value, (int, float)) and value >= 0:
		return (float(value), float(value))
	if isinstance(value, list) and len(value) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool)
			for v in value) and 0 <= value[0] <= value[1]:
		return (float(value[0]), float(value[1]))
	raise BoxError("{}: {!r}: seconds, or [min, max]".format(where, value))


def Word(value):
	# YAML 1.1 (pyyaml) reads on and off, unquoted, as true and false: as a key or a value here they are the words
	return "on" if value is True else "off" if value is False else value


def Mapping(value, where):
	if value is None:
		return {}
	if not isinstance(value, dict):
		raise BoxError("{}: must be a mapping".format(where))
	return {Word(k): v for k, v in value.items()}


def Index(spec, key, top, where):
	value = spec.get(key)
	if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < top:
		raise BoxError("{}: needs {}: 0-{}".format(where, key, top - 1))
	return value


class Input:
	def __init__(self, name, spec):
		where = "inputs: {}".format(name)
		spec = Mapping(spec, where)
		self.name = name
		self.index = Index(spec, "input", MAX_IN, where)
		if "pin" not in spec:
			raise BoxError("{}: needs pin".format(where))
		self.pin = Pin(spec["pin"], where)
		pull = str(spec.get("pull", "none")).lower()
		if pull not in ("none", "up", "down"):
			raise BoxError("{}: pull must be up, down or none".format(where))
		self.pull = pull
		active = str(spec.get("active", "low" if pull == "up" else "high")).lower()
		if active not in ("high", "low"):
			raise BoxError("{}: active must be high or low".format(where))
		self.active = active
		debounce = spec.get("debounce_ms", 1)
		if isinstance(debounce, bool) or not isinstance(debounce, (int, float)) or not 0 <= debounce <= 1000:
			raise BoxError("{}: debounce_ms must be 0-1000".format(where))
		self.debounce_us = int(round(debounce * 1000))

	def Command(self):
		return "in {} {} {} {} {}".format(self.index, self.pin, {"none": 0, "up": 1, "down": 2}[self.pull],
			1 if self.active == "high" else 0, self.debounce_us)


class Servo:
	def __init__(self, name, spec):
		where = "servos: {}".format(name)
		spec = Mapping(spec, where)
		self.name = name
		self.index = Index(spec, "servo", MAX_SERVO, where)
		if "pin" not in spec:
			raise BoxError("{}: needs pin".format(where))
		self.pin = Pin(spec["pin"], where)
		self.min_us = int(spec.get("min_us", 1000))
		self.max_us = int(spec.get("max_us", 2000))
		if not 500 <= self.min_us < self.max_us <= 2500:
			raise BoxError("{}: needs 500 <= min_us < max_us <= 2500".format(where))
		self.positions = {}
		for pos, us in Mapping(spec.get("positions"), where + " positions").items():
			self.positions[str(pos)] = self.Us(us, where + " positions: " + str(pos), named=False)
		if "safe" not in spec:
			raise BoxError("{}: needs safe: the position it goes to when nobody is in charge".format(where))
		self.safe_us = self.Us(spec["safe"], where + " safe")
		speed = spec.get("speed_us_per_s", 0)
		if isinstance(speed, bool) or not isinstance(speed, (int, float)) or speed < 0:
			raise BoxError("{}: speed_us_per_s must be 0 (at once) or more".format(where))
		self.speed = int(round(speed))

	def Us(self, value, where, named=True):
		# A position: one of its names, or microseconds
		if named and isinstance(value, str):
			if value not in self.positions:
				raise BoxError("{}: {!r} is none of its positions ({})".format(where, value,
					", ".join(self.positions) or "none"))
			return self.positions[value]
		if isinstance(value, bool) or not isinstance(value, (int, float)):
			raise BoxError("{}: {!r} is not a position".format(where, value))
		us = int(round(value))
		if not self.min_us <= us <= self.max_us:
			raise BoxError("{}: {} us is outside min_us-max_us ({}-{})".format(where, us, self.min_us, self.max_us))
		return us

	def Command(self):
		return "servo {} {} {} {} {} {}".format(self.index, self.pin, self.min_us, self.max_us, self.safe_us, self.speed)


def OutputAction(o, spec, where):
	"""An output's action: (action, the board's command, the link's what/a/b or None)."""
	ch = o.channel
	action = Word(spec.get("action"))
	if action is None:
		action = {"pulse": "pulse", "train": "train", "level": "on"}[o.default_action]
	if action == "pulse":
		ms = spec.get("ms", o.pulse_ms)
		if ms is None or isinstance(ms, bool) or not isinstance(ms, (int, float)) or ms <= 0:
			raise BoxError("{}: a pulse needs ms (or the output's pulse_ms)".format(where))
		us = int(round(ms * 1000))
		return action, "pulse {} {}".format(ch, us), (LINK_WHAT["pulse"], ch, us)
	if action == "on":
		return action, "on {}".format(ch), (LINK_WHAT["on"], ch, 0)
	if action == "off":
		return action, "off {}".format(ch), (LINK_WHAT["off"], ch, 0)
	if action in ("train", "stop", "burst"):
		if o.train is None:
			raise BoxError("{}: {} needs the output to have a train".format(where, action))
		if action == "train":
			return action, "run {} -1".format(ch), (LINK_WHAT["train"], ch, -1)
		if action == "stop":
			return action, "run {} 0".format(ch), None
		n = spec.get("pulses")
		if isinstance(n, bool) or not isinstance(n, int) or n < 1:
			raise BoxError("{}: burst needs pulses: n".format(where))
		return action, "run {} {}".format(ch, n), (LINK_WHAT["train"], ch, n)
	raise BoxError("{}: action {!r}: on, off, pulse, train, stop or burst".format(where, action))


class Action:
	"""What a state does on entering, or a link does: an output's, a servo's move, or a note."""

	def __init__(self, spec, box, where):
		spec = Mapping(spec, where)
		self.note = None
		self.link = None
		if "output" in spec:
			name = spec["output"]
			if name not in box.outputs:
				raise BoxError("{}: no output {!r}".format(where, name))
			self.target = box.outputs[name]
			self.kind = "output"
			self.action, self.command, self.link = OutputAction(self.target, spec, where)
		elif "servo" in spec:
			name = spec["servo"]
			if name not in box.servos:
				raise BoxError("{}: no servo {!r}".format(where, name))
			s = self.target = box.servos[name]
			if "to" not in spec:
				raise BoxError("{}: a servo needs to: a position".format(where))
			self.kind, self.action = "servo", "move"
			self.us = s.Us(spec["to"], where + " to")
			self.position = spec["to"] if isinstance(spec["to"], str) else None
			self.command = "move {} {}".format(s.index, self.us)
			self.link = (LINK_WHAT["move"], s.index, self.us)
		elif "note" in spec:
			self.kind, self.action, self.target, self.command = "note", "note", None, None
			self.note = str(spec["note"])
		else:
			raise BoxError("{}: an action is {{output: ...}}, {{servo: ..., to: ...}} or {{note: ...}}".format(where))

	def Name(self):
		return self.target.name if self.target is not None else None


class Link:
	def __init__(self, name, spec, box, number):
		where = "links: {}".format(name)
		spec = Mapping(spec, where)
		self.name, self.number = name, number
		if spec.get("when") not in box.inputs:
			raise BoxError("{}: when: must be one of the inputs ({})".format(where, ", ".join(box.inputs)))
		self.input = box.inputs[spec["when"]]
		edge = str(spec.get("edge", "active")).lower()
		if edge not in ("active", "inactive"):
			raise BoxError("{}: edge must be active or inactive".format(where))
		self.edge = edge
		self.action = Action(spec.get("do"), box, where + " do")
		if self.action.link is None:
			raise BoxError("{}: a link can't do {}".format(where, self.action.action))
		armed = str(spec.get("armed", "task")).lower()
		if armed not in ("always", "task"):
			raise BoxError("{}: armed must be always or task".format(where))
		self.always = armed == "always"

	def Command(self):
		what, a, b = self.action.link
		return "link {} {} {} {} {} {}".format(self.number, self.input.index, 1 if self.edge == "active" else 0,
			what, a, b)


class Next:
	"""Where a state goes when its time is up: one state, a random one of a list, or of weights."""

	def __init__(self, spec, states, where):
		if isinstance(spec, str):
			choices = {spec: 1.0}
		elif isinstance(spec, list) and spec:
			choices = {}
			for s in spec:
				choices[s] = choices.get(s, 0.0) + 1.0
		elif isinstance(spec, dict) and spec:
			choices = {}
			for s, w in spec.items():
				if isinstance(w, bool) or not isinstance(w, (int, float)) or w < 0:
					raise BoxError("{}: weight {!r} for {}".format(where, w, s))
				choices[s] = float(w)
			if not sum(choices.values()):
				raise BoxError("{}: every weight is 0".format(where))
		else:
			raise BoxError("{}: a state, a list of states or {{state: weight}}".format(where))
		for s in choices:
			if s not in states:
				raise BoxError("{}: no state {!r}".format(where, s))
		self.choices = choices

	def Pick(self, rng):
		names = list(self.choices)
		if len(names) == 1:
			return names[0]
		return rng.choices(names, weights=[self.choices[n] for n in names])[0]


class Transition:
	def __init__(self, input_name, spec, box, states, where):
		self.input = box.inputs.get(input_name)
		self.pose = input_name if input_name in box.poses else None
		if self.input is None and self.pose is None:
			raise BoxError("{}: no input or pose {!r}".format(where, input_name))
		if isinstance(spec, str):
			spec = {"to": spec}
		spec = Mapping(spec, where)
		unknown = set(spec) - {"to", "edge", "count", "if", "unless", "else"}
		if unknown:
			raise BoxError("{}: unknown keys {} (it takes to, edge, count, if, unless, else)".format(where,
				", ".join(sorted(unknown))))
		if spec.get("to") not in states:
			raise BoxError("{}: to: {!r} is no state".format(where, spec.get("to")))
		self.to = spec["to"]
		self.guard = None # (pose, whether it must hold)
		for key, want in (("if", True), ("unless", False)):
			if key in spec:
				if self.guard is not None:
					raise BoxError("{}: if or unless, not both".format(where))
				if spec[key] not in box.poses:
					raise BoxError("{}: {}: {!r} is no pose".format(where, key, spec[key]))
				self.guard = (spec[key], want)
		self.otherwise = spec.get("else")
		if self.otherwise is not None:
			if self.guard is None:
				raise BoxError("{}: else needs if or unless".format(where))
			if self.otherwise not in states:
				raise BoxError("{}: else: {!r} is no state".format(where, self.otherwise))
		edge = str(spec.get("edge", "active")).lower()
		if edge not in ("active", "inactive"):
			raise BoxError("{}: edge must be active or inactive".format(where))
		self.active = edge == "active"
		count = spec.get("count", 1)
		if isinstance(count, bool) or not isinstance(count, int) or count < 1:
			raise BoxError("{}: count must be 1 or more".format(where))
		self.count = count


class State:
	def __init__(self, name, spec, box, states):
		where = "task: states: {}".format(name)
		spec = Mapping(spec, where)
		known = {"do", "arm", "on", "after_s", "next", "trial", "end"}
		unknown = set(spec) - known
		if unknown:
			raise BoxError("{}: unknown keys {} (it takes {})".format(where, ", ".join(sorted(unknown)),
				", ".join(sorted(known))))
		self.name = name
		do = spec.get("do") or []
		if not isinstance(do, list):
			do = [do]
		self.actions = [Action(a, box, "{} do[{}]".format(where, i)) for i, a in enumerate(do)]
		arm = spec.get("arm") or {}
		if isinstance(arm, str):
			arm = {arm: 1}
		elif isinstance(arm, list):
			arm = {a: 1 for a in arm}
		self.arm = {}
		for link, n in Mapping(arm, where + " arm").items():
			if link not in box.links:
				raise BoxError("{} arm: no link {!r}".format(where, link))
			if box.links[link].always:
				raise BoxError("{} arm: {} is armed always already".format(where, link))
			if isinstance(n, bool) or not isinstance(n, int) or (n < 1 and n != -1):
				raise BoxError("{} arm: {}: times must be 1 or more, or -1 until the state ends".format(where, link))
			self.arm[link] = n
		self.on = [Transition(i, t, box, states, "{} on: {}".format(where, i))
			for i, t in Mapping(spec.get("on"), where + " on").items()]
		self.after = Seconds(spec["after_s"], where + " after_s") if "after_s" in spec else None
		self.next = Next(spec["next"], states, where + " next") if "next" in spec else None
		if self.after is not None and self.next is None:
			raise BoxError("{}: after_s needs next".format(where))
		if self.next is not None and self.after is None:
			self.after = (0.0, 0.0) # moves on at once
		self.trial = bool(spec.get("trial", False))
		self.end = bool(spec.get("end", False))
		if not self.end and self.next is None and not self.on:
			raise BoxError("{}: it never ends: give it next, on or end: true".format(where))


class PoseRule(rulesmod.Rule):
	"""A named pose: campy-rules' condition, with its enter_ms, exit_ms and missing_ms (Rule.Step), and no output."""

	def __init__(self, name, spec, d, regions):
		where = "poses: {}".format(name)
		self.name = name
		self.condition = rulesmod.Condition(spec["when"], d, regions, where)
		self.enter_ns = int(rulesmod.Number(spec.get("enter_ms", 0), where) * 1e6)
		self.exit_ns = int(rulesmod.Number(spec.get("exit_ms", 0), where) * 1e6)
		self.missing_ns = int(rulesmod.Number(spec.get("missing_ms", 100), where) * 1e6)
		self.refractory_ns = 0
		self.started_ns = None
		self.Reset()


class Cameras:
	"""
	The camera pulses the board makes, when one board is the cameras' trigger and the box's (sketch version 4): from
	campy's frameRate (trigger.ino's period: floor(1e6 / int(fps)) us), the number of pulses, the cameras' pins and
	the sync output (campy's syncPin, syncSeed, syncPulseMs, syncGapMs).
	"""

	def __init__(self, fps, frames, pins, sync_pin=None, sync_seed=None, sync_pulse_ms=10.0, sync_gap_ms=(200, 1000),
			when_ready=False, watch_ms=0):
		from campy import syncmatch
		self.fps = float(fps)
		self.period = syncmatch.TriggerPeriodUs(self.fps)
		if not self.period or not 100 <= self.period <= 65535:
			raise BoxError("cameras: {:g} fps is a period of {} us; the board's timer takes 100 to 65,535 us (16 fps or "
				"more)".format(self.fps, self.period))
		self.frames = int(frames)
		if self.frames < 0:
			raise BoxError("cameras: the number of frames must be 0 (until stopped) or more")
		self.pins = [Pin(p, "camera pins") for p in pins]
		if not 1 <= len(self.pins) <= 4 or len(set(self.pins)) != len(self.pins):
			raise BoxError("cameras: 1 to 4 camera pins, each once")
		self.sync = None
		if sync_pin is not None and str(sync_pin) != "None":
			gaps = [int(g) for g in sync_gap_ms]
			width_us = int(round(float(sync_pulse_ms) * 1000))
			if len(gaps) != 2 or not 1 <= gaps[0] <= gaps[1] or width_us < 1 or width_us + 3000 > gaps[0] * 1000:
				raise BoxError("sync: a pulse of {:g} ms and gaps of {} ms: the gaps' shortest and longest, and the pulse "
					"ending at least 3 ms before the next one".format(width_us / 1000, gaps))
			if sync_seed is None or str(sync_seed) == "None":
				import secrets
				sync_seed = secrets.randbelow(2 ** 31)
			self.sync = dict(pin=Pin(sync_pin, "sync pin"), seed=int(sync_seed), width_us=width_us,
				min_gap_ms=gaps[0], max_gap_ms=gaps[1])
		self.when_ready = bool(when_ready)
		self.watch_ms = int(watch_ms)

	def Pins(self):
		return [("camera pin", p) for p in self.pins] + ([("the sync output", self.sync["pin"])] if self.sync else [])

	def Commands(self):
		out = ["cam {} {}".format(self.period, " ".join(str(p) for p in self.pins))]
		s = self.sync
		out.append("camsync {} {} {} {} {}".format(s["pin"], s["seed"], s["width_us"], s["min_gap_ms"], s["max_gap_ms"])
			if s else "camsync -1")
		out.append("camwatch {}".format(self.watch_ms))
		return out

	def SyncRecord(self, t0_board_us, t0_host_ns):
		# sync.json, as campy-syncmatch reads it: trigger.ino's, with no holds (the camera edges are the timer's)
		return dict(self.sync, frame_period_us=self.period, hold_guard_us=0, hold_after_us=0, board="campy_outputs",
			t0_board_us=t0_board_us, t0_host_ns=t0_host_ns)


class Box:
	"""The whole task file, checked: Setup() gives the board's commands."""

	def __init__(self, config):
		known = {"board", "inputs", "outputs", "servos", "links", "task", "live", "regions", "poses"}
		unknown = set(config) - known
		if unknown:
			raise BoxError("unknown sections {} (a task file has {})".format(", ".join(sorted(unknown)),
				", ".join(sorted(known))))
		self.board = Mapping(config.get("board"), "board")
		self.inputs = {name: Input(name, spec) for name, spec in Mapping(config.get("inputs"), "inputs").items()}
		self.outputs = {}
		for name, spec in Mapping(config.get("outputs"), "outputs").items():
			spec = dict(Mapping(spec, "outputs: {}".format(name)))
			if "pin" in spec:
				spec["pin"] = Pin(spec["pin"], "outputs: {}".format(name))
			try:
				self.outputs[name] = Output(name, spec)
			except BoxError:
				raise
			except RulesError as e:
				raise BoxError(str(e))
		self.servos = {name: Servo(name, spec) for name, spec in Mapping(config.get("servos"), "servos").items()}
		self.live = Mapping(config.get("live"), "live")
		unknown = set(self.live) - {"name", "stale_ms"}
		if unknown:
			raise BoxError("live: unknown keys {} (it takes name, stale_ms)".format(", ".join(sorted(unknown))))
		stale = self.live.get("stale_ms", 200)
		if isinstance(stale, bool) or not isinstance(stale, (int, float)) or stale <= 0:
			raise BoxError("live: stale_ms must be more than 0")
		self.stale_ms = float(stale)
		self.regions = Mapping(config.get("regions"), "regions")
		self.poses = {}
		for name, spec in Mapping(config.get("poses"), "poses").items():
			spec = Mapping(spec, "poses: {}".format(name))
			unknown = set(spec) - {"when", "enter_ms", "exit_ms", "missing_ms"}
			if unknown or "when" not in spec:
				raise BoxError("poses: {}: needs when:, and takes enter_ms, exit_ms, missing_ms".format(name))
			self.poses[name] = spec
		for what, things in (("inputs", self.inputs), ("outputs", self.outputs), ("servos", self.servos)):
			numbers = [getattr(t, "channel", getattr(t, "index", None)) for t in things.values()]
			if len(set(numbers)) != len(numbers):
				raise BoxError("{}: two of them have the same number".format(what))
		names = list(self.inputs) + list(self.outputs) + list(self.servos) + list(self.poses)
		if len(set(names)) != len(names):
			raise BoxError("inputs, outputs, servos and poses: two share a name")
		trigger = self.board.get("trigger_pin", 10)
		self.trigger_pin = None if trigger is None or str(trigger).lower() == "none" else Pin(trigger, "board: "
			"trigger_pin")
		self.cameras = None
		self.all_lines = False # --all-lines: the board says every frame's line and every train pulse (quiet off)
		self.CheckPins()
		links = Mapping(config.get("links"), "links")
		if len(links) > MAX_LINK:
			raise BoxError("links: at most {}".format(MAX_LINK))
		self.links = {name: Link(name, spec, self, n) for n, (name, spec) in enumerate(links.items())}
		watchdog = self.board.get("watchdog_ms", 250)
		if isinstance(watchdog, bool) or not isinstance(watchdog, (int, float)) or not 50 <= watchdog <= 10000:
			raise BoxError("board: watchdog_ms must be 50-10000")
		self.watchdog_ms = int(watchdog)

		task = Mapping(config.get("task"), "task")
		specs = Mapping(task.get("states"), "task: states")
		if not specs:
			raise BoxError("task: states: none")
		self.states = {}
		for name in specs:
			self.states[name] = None
		for name, spec in specs.items():
			self.states[name] = State(name, spec, self, specs)
		self.start = task.get("start", next(iter(specs)))
		if self.start not in self.states:
			raise BoxError("task: start: no state {!r}".format(self.start))
		self.after_watchdog = task.get("after_watchdog", self.start)
		if self.after_watchdog not in self.states:
			raise BoxError("task: after_watchdog: no state {!r}".format(self.after_watchdog))
		end = Mapping(task.get("end"), "task: end")
		self.end_trials = end.get("trials")
		if self.end_trials is not None and (isinstance(self.end_trials, bool) or not isinstance(self.end_trials, int)
				or self.end_trials < 1):
			raise BoxError("task: end: trials must be 1 or more")
		self.end_minutes = end.get("minutes")
		if self.end_minutes is not None and (isinstance(self.end_minutes, bool) or
				not isinstance(self.end_minutes, (int, float)) or self.end_minutes <= 0):
			raise BoxError("task: end: minutes must be more than 0")
		self.seed = task.get("seed")
		if self.seed is not None and (isinstance(self.seed, bool) or not isinstance(self.seed, int)):
			raise BoxError("task: seed must be a whole number")
		self.by_input = {i.index: i for i in self.inputs.values()}
		self.by_channel = {o.channel: o for o in self.outputs.values()}
		self.by_servo = {s.index: s for s in self.servos.values()}
		self.by_link = {l.number: l for l in self.links.values()}

	def CheckPins(self):
		# No pin is two things': the camera trigger in (or the camera pulses out), the inputs, outputs and servos
		pins = {}
		for what, pin in ([("the camera trigger", self.trigger_pin)] if self.trigger_pin is not None else []) + \
				(self.cameras.Pins() if self.cameras is not None else []) + \
				[("input " + n, i.pin) for n, i in self.inputs.items()] + \
				[("output " + n, o.pin) for n, o in self.outputs.items()] + \
				[("servo " + n, s.pin) for n, s in self.servos.items()]:
			if pin in pins:
				raise BoxError("{} and {} are both on {}".format(pins[pin], what, PinName(pin)))
			pins[pin] = what

	def UseCameras(self, cameras):
		"""The board makes the camera pulses (Cameras): no trigger pin in, the frames come from them."""
		self.cameras, self.trigger_pin = cameras, None
		self.CheckPins()

	def Poses(self, d):
		"""The poses bound to campy-live's results (rules.Description d): PoseRules, by name. RulesError if one
		doesn't fit them (a node, a camera, an animal campy-live doesn't have)."""
		regions = {name: rulesmod.Region(name, spec, d) for name, spec in self.regions.items()}
		return {name: PoseRule(name, spec, d, regions) for name, spec in self.poses.items()}

	def Setup(self):
		"""The board's commands: everything else freed, then this box's outputs, inputs, servos and links."""
		# All of them first, whatever the board was told before (another task's pins): a pin is freed before
		# anything else takes it
		# (cam 0 is refused while a board's camera pulses run: another recording's, which this won't stop)
		out = ["alloff", "cam 0", "camsync -1"]
		out += ["link {} -1".format(n) for n in range(MAX_LINK)]
		out += ["in {} -1".format(n) for n in range(MAX_IN)]
		out += ["servo {} -1".format(n) for n in range(MAX_SERVO)]
		used = {o.channel for o in self.outputs.values()}
		out += ["pin {} -1".format(ch) for ch in range(MAX_CH) if ch not in used]
		out.append("trig {}".format(self.trigger_pin if self.trigger_pin is not None else -1))
		out.append("watchdog {}".format(self.watchdog_ms))
		for o in self.outputs.values():
			out += ["pin {} {}".format(o.channel, o.pin), "group {} {}".format(o.channel, o.group),
				"max {} {}".format(o.channel, int(o.max_on_s * 1e6))]
			if o.train:
				t = o.train
				out.append("train {} {} {} {} {}".format(o.channel, t["every"], t["phase"], t["offset_us"],
					t["width_us"]))
		out += [i.Command() for i in self.inputs.values()]
		out += [s.Command() for s in self.servos.values()]
		out += [l.Command() for l in self.links.values()]
		if self.cameras is not None:
			out += self.cameras.Commands()
		# The USB link: re-attached by the board if it goes quiet 2 s (campy-box sends a heartbeat every 50 ms), and
		# fewer lines: a frame's line once a second (frame 1 always), none per train pulse (they are the train's,
		# frame-locked: the trains' settings say where each one was)
		out += ["linkwatch {}".format(LINKWATCH_MS), "quiet 1 1" if self.all_lines else "quiet 60 0"]
		return out

	def SessionStarts(self):
		# What runs for the whole session: links armed always, trains with run: session
		out = ["arm {} -1".format(l.number) for l in self.links.values() if l.always]
		out += ["run {} -1".format(o.channel) for o in self.outputs.values() if o.run == "session"]
		return out


# ---------------------------------------------------------------- running it

def WriteJson(path, value):
	# Whole or not at all: a reader never sees half of it
	folder = os.path.dirname(os.path.abspath(path))
	os.makedirs(folder, exist_ok=True)
	part = path + ".part"
	with open(part, "w") as f:
		strictjson.dump(value, f)
	os.replace(part, path)


class LogFile:
	# The board log as the board writes it (Board's log.write), its writes on campy-box's writer thread
	def __init__(self, runner, f):
		self.runner, self.f = runner, f

	def write(self, text):
		self.runner.Write(self.f, text)


class Runner:
	"""
	The task's loop: the board's lines come in on its reading thread (Listen) and wait in a queue; the loop takes
	them, moves the task from state to state, and keeps its timers.
	"""

	def __init__(self, args, box, connection=None, opener=None):
		self.args, self.box = args, box
		self.connection = connection # tests: a port of their own
		self.opener = opener         # tests: a new one, for a relink
		self.stop = threading.Event()
		self.say = lambda text: print(text, flush=True)
		self.lock = threading.Lock()
		self.queue = queue.Queue()
		self.events = None
		self.writer = None
		self.board = None
		self.beat = None
		self.seed = box.seed if box.seed is not None else random.SystemRandom().randrange(1 << 31)
		self.rng = random.Random(self.seed)
		self.state = None
		self.entered = None     # when (time.monotonic) the state was entered
		self.deadline = None    # when its time is up (time.monotonic), None: never
		self.next = None        # the state it then goes to
		self.edges = {}         # (input, active) -> edges since the state was entered
		self.armed = set()      # the links the state armed
		self.trial = 0
		self.started = None
		self.end_why = None
		self.counts = dict(inputs={n: 0 for n in box.inputs}, releases={n: 0 for n in box.inputs},
			links={n: 0 for n in box.links}, links_refused={n: 0 for n in box.links},
			states={n: 0 for n in box.states}, pulses=0, servo_moves=0, commands=0, errors=0, watchdog=0, gaps=0,
			clash=0)
		self.said = set()
		self.created = time.time()
		self.holding = {name: False for name in box.poses} # each pose's state, as the task sees it
		self.counts["poses"] = {name: 0 for name in box.poses}
		self.counts.update(live_sets=0, live_gone=0)
		self.live_name = getattr(args, "live_name", "") or box.live.get("name") or "campy-live"
		# The camera pulses the board makes (box.cameras): started, running, ended, their time 0
		self.cams = dict(started=False, running=False, ended=None, t0_board_us=None, t0_host_ns=None, frames=None,
			stop_asked=False)
		self.task_done = False   # the task ended, the camera pulses going on to the recording's end
		self.cams_stop_by = None # campy-box ends by this time (time.monotonic) once it asked the pulses to stop

	def Alive(self):
		return self.beat is None or time.monotonic() - self.beat < LOOP_ALIVE_S

	def Write(self, f, text):
		if self.writer is None:
			f.write(text)
		else:
			self.writer.Do(f.write, text)

	def Event(self, record):
		text = strictjson.dumps(record, separators=(",", ":")) + "\n"
		with self.lock:
			if self.events is not None:
				self.Write(self.events, text)

	def HostNs(self, board_us):
		return self.board.clock.HostNs(board_us) if self.board is not None else None

	def LastFrame(self):
		# The last camera frame the computer has heard of (0: none)
		if self.board is None or not self.board.rises:
			return 0
		return next(reversed(self.board.rises))

	# ------------------------------------------------------------ the board's lines (its reading thread)

	def Listen(self, kind, fields, heard_ns, line):
		if kind == "answer":
			answer = fields
			record = answer.record
			if record is None:
				return
			record.update(sent_ns=answer.sent_ns, heard_ns=answer.heard_ns, answer=answer.line)
			words = answer.line.split()
			if words[:1] == ["ok"] and words[1:2] and words[1] in ("on", "off", "pulse", "move", "run"):
				pos = {"on": 3, "off": 3, "pulse": 3, "move": 4, "run": 4}[words[1]]
				t = int(words[pos])
				record["board_us"] = t
				record["host_ns"] = self.HostNs(t)
			elif words[:1] == ["err"]:
				self.counts["errors"] += 1
				said = (record.get("command"), answer.line)
				if said not in self.said:
					self.said.add(said)
					self.say("campy-box: the board refused {!r}: {}".format(answer.command, answer.line))
			self.Event(record)
			return
		if kind == "t":
			# A camera trigger: Board keeps it (rises), for the frames. While the task waits for the first one, it wakes
			# the loop, so that the task starts with it, not up to a loop's wait (LOOP_S) later, a frame or two on
			if self.state is None:
				self.queue.put((kind, fields, heard_ns, line))
			return
		self.queue.put((kind, fields, heard_ns, line))

	# ------------------------------------------------------------ the loop's side

	def Send(self, command, record=None):
		self.counts["commands"] += 1
		record = dict(record or {}, kind="command", command=command, state=self.state, trial=self.trial)
		self.board.Command(command, record)

	def Handle(self, kind, fields, heard_ns, line):
		box = self.box
		if kind == "i":
			index, active, t, k, after = (int(x) for x in fields[:5])
			inp = box.by_input.get(index)
			name = inp.name if inp else "input {}".format(index)
			if inp is not None:
				self.counts["inputs" if active else "releases"][name] += 1
			record = dict(kind="input", input=name, active=active, board_us=t, host_ns=self.HostNs(t),
				frame=k or None, after_us=after if after >= 0 else None, heard_ns=heard_ns, state=self.state,
				trial=self.trial)
			self.Event(record)
			if inp is not None:
				self.Edge(inp, bool(active), record)
		elif kind == "x":
			number, index, t, k, done = (int(x) for x in fields[:5])
			link = box.by_link.get(number)
			name = link.name if link else "link {}".format(number)
			if link is not None:
				self.counts["links" if done else "links_refused"][name] += 1
			self.Event(dict(kind="link", link=name, done=bool(done), board_us=t, host_ns=self.HostNs(t),
				frame=k or None, heard_ns=heard_ns, state=self.state, trial=self.trial))
		elif kind == "m":
			index, us, t = (int(x) for x in fields[:3])
			servo = box.by_servo.get(index)
			position = None
			if servo is not None:
				position = next((p for p, v in servo.positions.items() if v == us), None)
			self.counts["servo_moves"] += 1
			self.Event(dict(kind="servo", servo=servo.name if servo else index, us=us, position=position,
				board_us=t, host_ns=self.HostNs(t), heard_ns=heard_ns, state=self.state, trial=self.trial))
		elif kind == "p":
			ch, k, rise, fall = (int(x) for x in fields[:4])
			o = box.by_channel.get(ch)
			self.counts["pulses"] += 1
			self.Event(dict(kind="pulse", output=o.name if o else ch, frame=k if k > 0 else None, rise_us=rise,
				fall_us=fall, rise_ns=self.HostNs(rise), fall_ns=self.HostNs(fall), state=self.state,
				trial=self.trial))
		elif kind == "e":
			ch, level, t = (int(x) for x in fields[:3])
			o = box.by_channel.get(ch)
			self.Event(dict(kind="level", output=o.name if o else ch, on=bool(level), why=fields[3] if
				len(fields) > 3 else None, board_us=t, host_ns=self.HostNs(t), state=self.state, trial=self.trial))
		elif kind == "gap":
			self.counts["gaps"] += 1
			k, gap_us = (int(x) for x in fields[:2])
			self.Event(dict(kind="gap", frame=k, gap_ms=gap_us / 1e3, heard_ns=heard_ns))
		elif kind == "clash":
			self.counts["clash"] += 1
			self.Event(dict(kind="clash", line=line, heard_ns=heard_ns))
		elif kind == "wd":
			self.counts["watchdog"] += 1
			self.Event(dict(kind="watchdog", line=line, heard_ns=heard_ns, state=self.state, trial=self.trial))
			self.say("campy-box: the board's watchdog made the box safe ({}): campy-box was held up. The task goes "
				"on from {}.".format(line, box.after_watchdog))
			self.armed = set() # the board disarmed them
			for command in box.SessionStarts():
				self.Send(command)
			if self.state is not None:
				self.Enter(box.after_watchdog, dict(why="watchdog"))
		elif kind == "relink":
			what = fields[0]
			self.counts["relinks" if what == "back" else "link_down"] = self.counts.get(
				"relinks" if what == "back" else "link_down", 0) + 1
			self.Event(dict(kind="relink", what=what, detail=fields[1], heard_ns=heard_ns, state=self.state,
				trial=self.trial))
			if what == "down":
				self.say("campy-box: the board's USB link stalled ({}): opening it again.".format(fields[1]))
			else:
				# Gone over the board's watchdog (250 ms): it made the box safe, as after "wd", so the same again here
				self.say("campy-box: the board's link is back after {:.1f} s; the task goes on from {}.".format(fields[1],
					box.after_watchdog))
				self.armed = set()
				for command in box.SessionStarts():
					self.Send(command)
				if self.state is not None and not self.task_done:
					self.Enter(box.after_watchdog, dict(why="relink"))
		elif kind == "link":
			self.Event(dict(kind="link_reattached", line=line, heard_ns=heard_ns))
		elif kind == "camend":
			frames, t = (int(x) for x in fields[:2])
			self.cams.update(running=False, ended=dict(frames=frames, board_us=t, host_ns=self.HostNs(t)))
			self.Event(dict(kind="cameras_end", frames=frames, board_us=t, host_ns=self.HostNs(t), heard_ns=heard_ns))
			self.say("campy-box: the camera pulses ended: {} frames.".format(frames))
			if self.task_done or self.cams["stop_asked"]:
				self.stop.set()
		elif kind in ("camwd", "camerr"):
			self.Event(dict(kind=kind, line=line, heard_ns=heard_ns))
			self.say("campy-box: the board said {!r}: {}".format(line, "camwatch stopped the camera pulses" if kind ==
				"camwd" else "its timer's count and its frames differ"))
		elif kind == "pose":
			name, holds, frame, row_ns, read_ns, why = fields
			self.holding[name] = holds
			if holds:
				self.counts["poses"][name] += 1
			record = dict(kind="pose", pose=name, holds=holds, frame=frame, row_ns=row_ns, read_ns=read_ns, why=why,
				state=self.state, trial=self.trial)
			self.Event(record)
			self.Edge(None, holds, record, pose=name)
		elif kind == "live":
			what, text = fields
			if what == "gone":
				self.counts["live_gone"] += 1
			self.Event(dict(kind="live", what=what, text=text, heard_ns=heard_ns, state=self.state, trial=self.trial))
			if what == "error":
				self.say("campy-box: {}. Stopping.".format(text))
				self.end_why = "poses: " + text
				self.stop.set()
			else:
				self.say("campy-box: {}".format(text))
		elif kind == "lost":
			self.say("campy-box: {}. The board makes the box safe by itself (its watchdog). Stopping.".format(
				fields[0]))
			self.end_why = "board lost"
			self.stop.set()
		elif kind == "ready":
			# The board started again (a reset, power): it knows nothing of the box any more
			self.say("campy-box: the board restarted ({}). Stopping: everything it was told is gone.".format(line))
			self.cams["running"] = False # its camera pulses too
			self.end_why = "board restarted"
			self.stop.set()

	def Edge(self, inp, active, record, pose=None):
		# An input's edge (inp), or a pose starting or ending (pose: its name): the state's move on it, if any
		if self.state is None or self.task_done:
			return # the task hasn't started (waiting for the first frame), or has ended
		state = self.box.states[self.state]
		name = pose if pose is not None else inp.name
		for tr in state.on:
			if tr.active != active or (tr.pose != pose if pose is not None else tr.input is not inp):
				continue
			if pose is not None:
				cause = dict(why="pose", pose=pose, holds=int(active), frame=record["frame"], row_ns=record["row_ns"])
			else:
				cause = dict(why="input", input=name, active=int(active), board_us=record["board_us"],
					host_ns=record["host_ns"], frame=record["frame"], after_us=record["after_us"])
			if tr.guard is not None:
				guard, want = tr.guard
				cause.update(guard=guard, guard_holds=self.holding[guard])
				if self.holding[guard] != want:
					if tr.otherwise is not None:
						self.Enter(tr.otherwise, dict(cause, why=cause["why"] + " (else)"))
					return
			key = (name, active)
			self.edges[key] = self.edges.get(key, 0) + 1
			if self.edges[key] >= tr.count:
				self.Enter(tr.to, cause)
			return

	def Enter(self, name, cause, depth=0):
		"""Leave the state, enter name: its trial, its actions, its links, its timer."""
		box = self.box
		state = box.states[name]
		for link in sorted(self.armed):
			self.Send("arm {} 0".format(box.links[link].number), dict(link=link))
		self.armed = set()
		if state.trial and box.end_trials is not None and self.trial >= box.end_trials:
			self.Finish("{} trials".format(box.end_trials), cause)
			return
		previous = self.state
		self.state = name
		if state.trial:
			self.trial += 1
		self.counts["states"][name] += 1
		self.entered = time.monotonic()
		self.edges = {}
		record = dict(kind="state", state=name, previous=previous, trial=self.trial, new_trial=state.trial,
			entered_ns=clock.HostNs(), last_frame=self.LastFrame())
		record.update(cause)
		self.Event(record)
		for a in state.actions:
			if a.kind == "note":
				self.Event(dict(kind="note", text=a.note, state=name, trial=self.trial, host_ns=clock.HostNs()))
			else:
				self.Send(a.command, dict(action=a.action, target=a.Name(), position=getattr(a, "position", None)))
		for link, n in state.arm.items():
			self.Send("arm {} {}".format(box.links[link].number, n), dict(link=link))
			self.armed.add(link)
		if state.end:
			self.Finish("state {}".format(name), dict(why="end state"))
			return
		# A pose is a level: a state whose on: names one moves at once if, as it is entered, the pose already holds
		# (edge: inactive: already doesn't)
		for tr in state.on:
			if tr.pose is None or tr.count != 1 or self.holding[tr.pose] != tr.active:
				continue
			cause = dict(why="pose", pose=tr.pose, holds=int(tr.active), at_entry=True)
			to = tr.to
			if tr.guard is not None:
				guard, want = tr.guard
				cause.update(guard=guard, guard_holds=self.holding[guard])
				if self.holding[guard] != want:
					if tr.otherwise is None:
						continue
					to, cause["why"] = tr.otherwise, "pose (else)"
			if depth >= IMMEDIATE_MAX:
				raise BoxError("states moving on at once {} times in a row, from {}: the task loops".format(
					IMMEDIATE_MAX, name))
			self.deadline = None
			self.Enter(to, cause, depth + 1)
			return
		self.deadline = None
		if state.next is not None:
			lo, hi = state.after
			wait = lo if lo == hi else self.rng.uniform(lo, hi)
			self.deadline = self.entered + wait
			self.next = state.next.Pick(self.rng)
			if wait == 0:
				if depth >= IMMEDIATE_MAX:
					raise BoxError("states moving on at once {} times in a row, from {}: the task loops".format(
						IMMEDIATE_MAX, name))
				self.Enter(self.next, dict(why="next"), depth + 1)

	def ReadPoses(self):
		"""
		The poses, from campy-live's results feed (its own thread): each set through every pose; a pose that starts
		or lets go goes to the task's queue. No set for stale_ms, or the feed ended (campy-live restarting): every
		pose lets go, and the feed is looked for again.
		"""
		from campy import liveresults
		ring = liveresults.RingName(self.live_name)
		stale = self.box.stale_ms / 1e3
		results, poses, n, last, released = None, None, 0, time.monotonic(), True
		said_waiting = False

		def Release(why):
			for p in poses.values():
				if p.holding:
					self.queue.put(("pose", (p.name, False, None, None, clock.HostNs(), why), clock.HostNs(), ""))
				p.Reset()

		while not self.stop.is_set():
			if results is None:
				results = rulesmod.Runner.TryAttach(ring)
				if results is None:
					if not said_waiting:
						said_waiting = True
						self.queue.put(("live", ("waiting", "waiting for campy-live's results ({}) for the poses".format(
							self.live_name)), clock.HostNs(), ""))
					self.stop.wait(0.2)
					continue
				try:
					d = rulesmod.Description.From(results.description, results.rows_type)
					poses = self.box.Poses(d)
				except RulesError as e:
					results.Close()
					self.queue.put(("live", ("error", "the poses don't fit campy-live's results: {}".format(e)),
						clock.HostNs(), ""))
					return
				n, last, released = results.Latest(), time.monotonic(), True # from the next set on
				self.queue.put(("live", ("attached", "campy-live's results: {} poses on {} cameras{}".format(len(poses),
					len(d.cameras), ", {} animals in 3D".format(d.animals) if d.animals else "")), clock.HostNs(), ""))
			latest = results.Latest()
			if latest <= n:
				now = time.monotonic()
				if not released and now - last > stale:
					Release("stale")
					released = True
				if results.Ended():
					if not released:
						Release("campy-live ended")
					results.Close()
					results, said_waiting = None, True
					self.queue.put(("live", ("gone", "campy-live's results ended: no pose holds until they come "
						"again"), clock.HostNs(), ""))
					continue
				self.stop.wait(0.002)
				continue
			for m in range(max(n + 1, latest - results.slots + 1), latest + 1):
				row = results.Read(m)
				if row is None:
					continue
				read_ns = clock.HostNs()
				t = rulesmod.RowNs(row)
				self.counts["live_sets"] += 1
				for p in poses.values():
					raw, change = p.Step(row, t)
					if change:
						self.queue.put(("pose", (p.name, change == "start", int(row["frame"]), t, read_ns, None),
							read_ns, ""))
			n, last, released = latest, time.monotonic(), False
		if results is not None:
			results.Close()

	def Finish(self, why, cause):
		"""
		The end: the task's own (its trials or minutes, an end state), or from outside (the stop file, the
		recording's end, campy-session gone, --duration-s). With the camera pulses running, the task's own end leaves
		them going to the recording's end (campy-box goes on for them); one from outside stops them at a period's end
		first.
		"""
		if self.end_why is None:
			self.end_why = why
			self.Event(dict(kind="end", why=why, state=self.state, trial=self.trial, host_ns=clock.HostNs()))
		self.deadline = None
		if self.cams["running"]:
			if why in TASK_ENDS or why.startswith("state ") or why.endswith(" trials"):
				if not self.task_done:
					self.task_done = True
					self.say("campy-box: the task ended ({}); the camera pulses go on.".format(why))
				return
			self.StopCameras()
			return
		self.stop.set()

	def StopCameras(self):
		# At a period's end (camstop); campy-box ends once the board says they ended, or a second after
		if self.cams["stop_asked"] or not self.cams["running"]:
			return
		self.cams["stop_asked"] = True
		self.Send("camstop")
		self.cams_stop_by = time.monotonic() + 1.0 + 2 * self.box.cameras.period / 1e6

	def CamerasReady(self):
		# Every camera of the recording waiting for its pulses (session.json's recording.cameras[].ready, as the panel
		# reads it for the Pico); without a recording folder, at once
		folder = getattr(self.args, "recording_folder", "")
		if not folder or not self.box.cameras.when_ready:
			return True
		try:
			with open(os.path.join(folder, "session.json"), encoding="utf-8") as f:
				record = strictjson.load(f).get("recording")
		except (OSError, ValueError, AttributeError, TypeError):
			return False
		cams = (record or {}).get("cameras") or []
		return bool(cams) and all(c.get("ready") for c in cams)

	def StartCameras(self, out):
		"""camgo: the pulses from now; their time 0 into <out>.cameras.json, and sync.json."""
		cams = self.box.cameras
		words = self.board.Command("camgo {}".format(cams.frames)).Ok().split()
		t0, period = int(words[3]), int(words[4])
		t0_ns = self.HostNs(t0)
		self.cams.update(started=True, running=True, t0_board_us=t0, t0_host_ns=t0_ns, frames=cams.frames)
		record = dict(board="campy_outputs", fps=cams.fps, period_us=period, frames=cams.frames, pins=cams.pins,
			t0_board_us=t0, t0_host_ns=t0_ns, rises="frame k (from 0) at t0 + k * period_us + period_us // 2",
			sync=cams.SyncRecord(t0, t0_ns) if cams.sync else None)
		WriteJson(out + ".cameras.json", record)
		if cams.sync:
			folder = getattr(self.args, "recording_folder", "")
			if folder:
				from campy import newfiles
				with newfiles.Open(os.path.join(folder, newfiles.SYNC_FILE), "w") as f:
					strictjson.dump(cams.SyncRecord(t0, t0_ns), f, indent=2)
			else:
				WriteJson(out + ".sync.json", cams.SyncRecord(t0, t0_ns))
		self.Event(dict(kind="cameras_start", **record))
		self.say("campy-box: the camera pulses started: {:g} fps ({} us), {}.".format(cams.fps, period,
			"{} frames".format(cams.frames) if cams.frames else "until stopped"))

	def Waiting(self):
		# --start-with-frames: no camera frame yet
		return bool(getattr(self.args, "start_with_frames", False)) and (self.box.trigger_pin is not None or
			self.box.cameras is not None) and self.board is not None and not self.board.rises

	def Look(self):
		"""The stop file, the recording's end, campy-session: whether to end, and why."""
		args = self.args
		stop_file = getattr(args, "stop_file", "")
		if stop_file and os.path.exists(stop_file):
			return "stop file"
		folder = getattr(args, "recording_folder", "")
		if folder:
			from campy import session
			if session.WatchState(folder, self.created) in ("ended", "failed"):
				return "the recording ended"
		pid = getattr(args, "launcher_pid", 0)
		if pid and self.state is None:
			from campy import session
			if not session.Alive(pid):
				return "campy-session ended before the recording started"
		return None

	def Loop(self, out=""):
		box = self.box
		self.beat = time.monotonic()
		looked = status_at = ready_at = 0.0
		end_at = run_end = None
		if self.args.duration_s:
			run_end = self.beat + self.args.duration_s
		while not self.stop.is_set():
			self.beat = now = time.monotonic()
			if box.cameras is not None and not self.cams["started"] and now - ready_at >= 0.2:
				ready_at = now
				if self.CamerasReady():
					self.StartCameras(out)
			if self.state is None and not self.Waiting():
				# The start: now, or at the first camera frame
				self.started = now
				if box.end_minutes is not None:
					end_at = self.started + box.end_minutes * 60
				self.Enter(box.start, dict(why="start", first_frame=self.LastFrame() or None))
				if self.stop.is_set():
					break
			wait = LOOP_S
			if self.deadline is not None:
				wait = max(0.0, min(wait, self.deadline - now))
			try:
				item = self.queue.get(timeout=wait)
			except queue.Empty:
				item = None
			if item is not None:
				self.Handle(*item)
				if self.stop.is_set():
					break
			now = time.monotonic()
			if self.deadline is not None and now >= self.deadline:
				self.deadline = None
				self.Enter(self.next, dict(why="timer"))
			if end_at is not None and now >= end_at and not self.stop.is_set() and not self.task_done:
				self.Finish("time", dict(why="time"))
			if run_end is not None and now >= run_end and not self.stop.is_set():
				self.Finish("duration", dict(why="duration"))
				run_end = None
			if self.cams_stop_by is not None and now >= self.cams_stop_by:
				self.stop.set()
			if now - looked >= LOOK_S and not self.stop.is_set() and not self.cams["stop_asked"]:
				looked = now
				why = self.Look()
				if why is not None:
					self.Finish(why, dict(why=why))
			if now - status_at >= STATUS_S:
				status_at = now
				self.WriteStatus()
		if self.end_why is None:
			self.end_why = "stopped"
			self.Event(dict(kind="end", why="stopped", state=self.state, trial=self.trial, host_ns=clock.HostNs()))

	def StatusDict(self):
		return dict(updated_ns=clock.HostNs(), pid=os.getpid(), state=self.state, trial=self.trial,
			trials=self.box.end_trials, started=self.started is not None, waiting=self.state is None and
			self.end_why is None, ended=self.end_why, task_ended=self.end_why in TASK_ENDS or (self.end_why or "")
			.startswith("state ") or (self.end_why or "").endswith(" trials"),
			seconds=round(time.monotonic() - self.started, 1) if self.started else 0, last_frame=self.LastFrame(),
			inputs=self.counts["inputs"], links=self.counts["links"], watchdog=self.counts["watchdog"],
			errors=self.counts["errors"], cameras=dict(self.cams) if self.box.cameras is not None else None)

	def WriteStatus(self):
		path = getattr(self.args, "status_file", "")
		if not path:
			return
		try:
			WriteJson(path, self.StatusDict())
		except OSError:
			pass

	def Run(self):
		args, box = self.args, self.box
		out = args.out or "campy-box_{}".format(datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
		folder = os.path.dirname(os.path.abspath(out))
		os.makedirs(folder, exist_ok=True)
		self.created = time.time()
		self.events = open(out + ".events.jsonl", "x")
		self.writer = Writer("campy-box logs", out + "'s logs")
		if getattr(args, "task", None):
			try:
				shutil.copyfile(args.task, out + ".task.yaml")
			except OSError as e:
				self.say("campy-box: could not keep a copy of the task file: {}".format(e))
		boardlog = None
		code = 0
		try:
			from campy.trigger import outputs
			port = args.port or box.board.get("port")
			if not port and self.connection is None:
				raise BoxError("no port for the board: --port, or board: port: in the task file")
			boardlog = open(out + ".board.txt", "x", buffering=1)
			self.board = outputs.Board(port, connection=self.connection, log=LogFile(self, boardlog),
				listener=self.Listen, say=self.say, alive=self.Alive, relink=RELINK_S, opener=self.opener)
			for command in box.Setup():
				self.board.Command(command).Ok()
			for command in box.SessionStarts():
				self.board.Command(command).Ok()
			self.Event(dict(kind="start", host_ns=clock.HostNs(), seed=self.seed, port=port,
				setup=box.Setup() + box.SessionStarts()))
			self.say("campy-box: {} inputs, {} outputs, {} servos, {} links, {} states, board on {}; seed {}.".format(
				len(box.inputs), len(box.outputs), len(box.servos), len(box.links), len(box.states), port, self.seed))
			if getattr(args, "ready_file", ""):
				WriteJson(args.ready_file, dict(pid=os.getpid(), seed=self.seed, port=port))
			if self.Waiting():
				self.say("campy-box: the board is set up; the task starts at the first camera frame.")
			if box.poses:
				threading.Thread(target=self.ReadPoses, name="campy-box poses", daemon=True).start()
			self.Loop(out)
			self.EndCameras()
		except (BoxError, RulesError) as e:
			self.say("campy-box: {}".format(e))
			code = 2
		except Exception as e:
			self.say("campy-box: stopped by an error: {}: {}. The box is made safe.".format(type(e).__name__, e))
			code = 1
		finally:
			if self.board is not None:
				self.board.Close() # alloff: outputs low, links disarmed, servos to their safe positions
			try:
				self.writer.Close()
			except Exception as e:
				self.say("campy-box: could not write its logs: {}".format(e))
			self.writer = None
			summary = self.Summary(out)
			with self.lock:
				self.events.close()
				self.events = None
			if boardlog is not None:
				boardlog.close()
			with open(out + ".json", "x") as f:
				strictjson.dump(summary, f, indent=2)
			self.WriteStatus()
			self.say("campy-box: {} trials; ended: {}. Summary in {}.json.".format(self.trial, self.end_why, out))
		return code

	def EndCameras(self):
		# Leaving with the camera pulses running (Ctrl-C): stopped at a period's end first, their end heard. Not after an
		# error: then they go on to their count, as when campy-box crashes
		if not self.cams["running"] or self.board is None or self.board.lost:
			return
		self.StopCameras()
		deadline = self.cams_stop_by or time.monotonic() + 1.0
		while self.cams["running"] and time.monotonic() < deadline:
			self.beat = time.monotonic()
			try:
				item = self.queue.get(timeout=LOOP_S)
			except queue.Empty:
				continue
			self.Handle(*item)

	def Summary(self, out):
		board = self.board
		return dict(task=os.path.abspath(self.args.task) if getattr(self.args, "task", None) else None,
			out=os.path.abspath(out), seed=self.seed, trials=self.trial, ended=self.end_why,
			task_ended=self.StatusDict()["task_ended"], last_state=self.state, cameras=self.StatusDict()["cameras"],
			seconds=round(time.monotonic() - self.started, 3) if self.started else 0, counts=self.counts,
			last_frame=self.LastFrame(), clock=dict(
				round_trip_ms=board.clock.RoundTrip() / 1e6 if board is not None and board.clock.RoundTrip() else None,
				fit=list(board.clock.fit) if board is not None and board.clock.fit else None))


def Shown(a):
	# An action, in a few words
	if a.kind == "note":
		return "note"
	if a.kind == "servo":
		return "{} to {}".format(a.Name(), a.position if a.position is not None else "{} us".format(a.us))
	return "{} {}".format(a.Name(), a.action)


def Check(box, say=print):
	"""What --check shows: the board's setup, and each state."""
	say("Board setup ({} commands):".format(len(box.Setup()) + len(box.SessionStarts())))
	for command in box.Setup() + box.SessionStarts():
		say("  " + command)
	if box.poses:
		say("Poses (checked against campy-live's results when they come, {}): {}".format(
			box.live.get("name") or "campy-live", ", ".join(box.poses)))
	say("States (start: {}):".format(box.start))
	for s in box.states.values():
		parts = []
		if s.trial:
			parts.append("starts a trial")
		if s.actions:
			parts.append("does " + ", ".join(Shown(a) for a in s.actions))
		if s.arm:
			parts.append("arms " + ", ".join("{} ({})".format(l, "until it ends" if n == -1 else "x{}".format(n))
				for l, n in s.arm.items()))
		for tr in s.on:
			guard = ""
			if tr.guard is not None:
				guard = " {} {}".format("if" if tr.guard[1] else "unless", tr.guard[0])
				if tr.otherwise is not None:
					guard += " (else -> {})".format(tr.otherwise)
			parts.append("{} {}{}{} -> {}".format(tr.pose or tr.input.name, ("holds" if tr.active else "lets go") if
				tr.pose else ("on" if tr.active else "off"), " x{}".format(tr.count) if tr.count > 1 else "", guard, tr.to))
		if s.next is not None:
			lo, hi = s.after
			when = "at once" if hi == 0 else "after {:g} s".format(lo) if lo == hi else "after {:g}-{:g} s".format(lo, hi)
			parts.append("{} -> {}".format(when, " / ".join(s.next.choices)))
		if s.end:
			parts.append("ends the task")
		say("  {}: {}".format(s.name, "; ".join(parts)))


def Main(argv=None):
	p = argparse.ArgumentParser(prog="campy-box", description="Run an operant box's task on the outputs board "
		"(sketch version 3). See python -m pydoc campy.box.")
	p.add_argument("task", help="the task's YAML file")
	p.add_argument("--port", default="", help="the board's serial port (else board: port: in the task file)")
	p.add_argument("--out", default="", help="log files' name, without extension (default campy-box_<date_time>)")
	p.add_argument("--check", action="store_true", help="check the file and show the board's setup and the states; "
		"no board")
	p.add_argument("--duration-s", type=float, default=0.0, help="stop after this long (0: when the task ends)")
	p.add_argument("--start-with-frames", action="store_true", help="start the task at the first camera frame")
	p.add_argument("--stop-file", default="", help="end when this file appears")
	p.add_argument("--recording-folder", default="", help="end when campy-acquire's watch says this recording ended")
	p.add_argument("--launcher-pid", type=int, default=0, help="campy-session: stop waiting for the first frame if it "
		"has ended")
	p.add_argument("--camera-fps", type=float, default=0.0, help="the board makes the camera pulses at this frame rate "
		"(campy's frameRate; one board for the cameras and the box)")
	p.add_argument("--camera-frames", type=int, default=0, help="that many pulses (recTimeInSec x frameRate); 0: until "
		"stopped")
	p.add_argument("--camera-pins", default="", help="the cameras' pins on the board, comma-separated (campy's "
		"digitalPins)")
	p.add_argument("--cameras-when-ready", action="store_true", help="start the pulses once every camera of the "
		"recording (--recording-folder) is waiting for them")
	p.add_argument("--camera-watch-ms", type=int, default=0, help="the board stops the pulses after that long without a "
		"word from campy-box (0: never)")
	p.add_argument("--sync-pin", default=None, help="the sync output's pin (campy's syncPin)")
	p.add_argument("--sync-seed", default=None, help="its seed (campy's syncSeed; default: a new one)")
	p.add_argument("--sync-pulse-ms", type=float, default=10.0, help="its pulse (syncPulseMs)")
	p.add_argument("--sync-gap-ms", default="200,1000", help="its shortest and longest gap (syncGapMs)")
	p.add_argument("--all-lines", action="store_true", help="the board says every frame's line and every train pulse "
		"(logged), not a frame's line a second and none per train pulse: more USB traffic, which its link stalls "
		"under now and then (recovered: docs/operant-boxes.md, The USB link)")
	p.add_argument("--live-name", default="", help="campy-live's liveFeedName, for the poses (default: live: name: in "
		"the task file, else campy-live)")
	p.add_argument("--ready-file", default="", help="written once the board is set up")
	p.add_argument("--status-file", default="", help="the state, trial and counts, written every second")
	args = p.parse_args(argv)
	try:
		box = Box(Load(args.task))
		box.all_lines = args.all_lines
		if args.camera_fps:
			box.UseCameras(Cameras(args.camera_fps, args.camera_frames, [p for p in args.camera_pins.split(",") if p],
				sync_pin=args.sync_pin, sync_seed=args.sync_seed, sync_pulse_ms=args.sync_pulse_ms,
				sync_gap_ms=[g for g in args.sync_gap_ms.split(",") if g], when_ready=args.cameras_when_ready,
				watch_ms=args.camera_watch_ms))
	except (BoxError, RulesError, OSError) as e:
		print("campy-box: {}".format(e))
		return 2
	except Exception as e: # a YAML error
		print("campy-box: {}: {}".format(args.task, e))
		return 2
	if args.check:
		Check(box)
		return 0
	runner = Runner(args, box)

	def Stop(signum, frame):
		runner.stop.set()
	signal.signal(signal.SIGINT, Stop)
	if hasattr(signal, "SIGTERM"):
		signal.signal(signal.SIGTERM, Stop)
	return runner.Run()


if __name__ == "__main__":
	sys.exit(Main())
