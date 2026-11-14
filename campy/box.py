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

Timing: the board stamps every edge with its own clock (microseconds since power-up) and the camera frame it fell
in (frame 1 is the first camera trigger after campy-box starts: start it before the recording so its frames are
campy's), and how long after that frame's trigger. campy-box maps board times to campy's host clock
(campy/clock.py, as hosttimes.npy), to within about 0.2 ms. A state change caused by an input carries that
input's times; one by a timer, the host time and the last frame the computer had heard of.

Output (--out, default campy-box_<date_time>):
  <out>.events.jsonl  every event: input edges, link firings, state changes, trials, pulses, levels, servo
                      arrivals, commands and their answers, trigger pauses, the watchdog
  <out>.board.txt     every line to and from the board, with host times
  <out>.json          the summary: counts per input, link and state, trials, the clock map

Needs pyyaml and pyserial (campy's own requirements).
"""

import argparse, os, queue, random, signal, sys, threading, time
from datetime import datetime

from campy import clock, strictjson
from campy.frametimes import Writer
from campy.rules import Output, RulesError

LOOP_ALIVE_S = 0.1 # the board's heartbeat goes only while the task's loop has gone round this recently
LOOP_S = 0.02      # the loop's longest sleep: timers are kept to about this
IMMEDIATE_MAX = 100 # states that move on at once, one after another: more is a loop in the task
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
		if input_name not in box.inputs:
			raise BoxError("{}: no input {!r}".format(where, input_name))
		self.input = box.inputs[input_name]
		if isinstance(spec, str):
			spec = {"to": spec}
		spec = Mapping(spec, where)
		if spec.get("to") not in states:
			raise BoxError("{}: to: {!r} is no state".format(where, spec.get("to")))
		self.to = spec["to"]
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


class Box:
	"""The whole task file, checked: Setup() gives the board's commands."""

	def __init__(self, config):
		known = {"board", "inputs", "outputs", "servos", "links", "task"}
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
		for what, things in (("inputs", self.inputs), ("outputs", self.outputs), ("servos", self.servos)):
			numbers = [getattr(t, "channel", getattr(t, "index", None)) for t in things.values()]
			if len(set(numbers)) != len(numbers):
				raise BoxError("{}: two of them have the same number".format(what))
		names = list(self.inputs) + list(self.outputs) + list(self.servos)
		if len(set(names)) != len(names):
			raise BoxError("inputs, outputs and servos: two share a name")
		trigger = self.board.get("trigger_pin", 10)
		self.trigger_pin = None if trigger is None or str(trigger).lower() == "none" else Pin(trigger, "board: "
			"trigger_pin")
		pins = {}
		for what, pin in ([("the camera trigger", self.trigger_pin)] if self.trigger_pin is not None else []) + \
				[("input " + n, i.pin) for n, i in self.inputs.items()] + \
				[("output " + n, o.pin) for n, o in self.outputs.items()] + \
				[("servo " + n, s.pin) for n, s in self.servos.items()]:
			if pin in pins:
				raise BoxError("{} and {} are both on {}".format(pins[pin], what, PinName(pin)))
			pins[pin] = what
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

	def Setup(self):
		"""The board's commands: everything else freed, then this box's outputs, inputs, servos and links."""
		# All of them first, whatever the board was told before (another task's pins): a pin is freed before
		# anything else takes it
		out = ["alloff"]
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
		return out

	def SessionStarts(self):
		# What runs for the whole session: links armed always, trains with run: session
		out = ["arm {} -1".format(l.number) for l in self.links.values() if l.always]
		out += ["run {} -1".format(o.channel) for o in self.outputs.values() if o.run == "session"]
		return out


# ---------------------------------------------------------------- running it

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

	def __init__(self, args, box, connection=None):
		self.args, self.box = args, box
		self.connection = connection # tests: a port of their own
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
			return # a camera trigger: Board keeps it (rises), for the frames
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
			self.Enter(box.after_watchdog, dict(why="watchdog"))
		elif kind == "lost":
			self.say("campy-box: {}. The board makes the box safe by itself (its watchdog). Stopping.".format(
				fields[0]))
			self.end_why = "board lost"
			self.stop.set()
		elif kind == "ready":
			# The board started again (a reset, power): it knows nothing of the box any more
			self.say("campy-box: the board restarted ({}). Stopping: everything it was told is gone.".format(line))
			self.end_why = "board restarted"
			self.stop.set()

	def Edge(self, inp, active, record):
		state = self.box.states[self.state]
		for tr in state.on:
			if tr.input is not inp or tr.active != active:
				continue
			key = (inp.name, active)
			self.edges[key] = self.edges.get(key, 0) + 1
			if self.edges[key] >= tr.count:
				cause = dict(why="input", input=inp.name, active=int(active), board_us=record["board_us"],
					host_ns=record["host_ns"], frame=record["frame"], after_us=record["after_us"])
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

	def Finish(self, why, cause):
		if self.end_why is None:
			self.end_why = why
			self.Event(dict(kind="end", why=why, state=self.state, trial=self.trial, host_ns=clock.HostNs()))
		self.deadline = None
		self.stop.set()

	def Loop(self):
		box = self.box
		self.started = time.monotonic()
		end_at = None
		if box.end_minutes is not None:
			end_at = self.started + box.end_minutes * 60
		if self.args.duration_s:
			d = self.started + self.args.duration_s
			end_at = d if end_at is None else min(end_at, d)
		self.beat = time.monotonic()
		self.Enter(box.start, dict(why="start"))
		while not self.stop.is_set():
			self.beat = now = time.monotonic()
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
			if end_at is not None and now >= end_at and not self.stop.is_set():
				self.Finish("time", dict(why="time"))
		if self.end_why is None:
			self.end_why = "stopped"
			self.Event(dict(kind="end", why="stopped", state=self.state, trial=self.trial, host_ns=clock.HostNs()))

	def Run(self):
		args, box = self.args, self.box
		out = args.out or "campy-box_{}".format(datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
		folder = os.path.dirname(os.path.abspath(out))
		os.makedirs(folder, exist_ok=True)
		self.events = open(out + ".events.jsonl", "x")
		self.writer = Writer("campy-box logs", out + "'s logs")
		boardlog = None
		code = 0
		try:
			from campy.trigger import outputs
			port = args.port or box.board.get("port")
			if not port and self.connection is None:
				raise BoxError("no port for the board: --port, or board: port: in the task file")
			boardlog = open(out + ".board.txt", "x", buffering=1)
			self.board = outputs.Board(port, connection=self.connection, log=LogFile(self, boardlog),
				listener=self.Listen, say=self.say, alive=self.Alive)
			for command in box.Setup():
				self.board.Command(command).Ok()
			for command in box.SessionStarts():
				self.board.Command(command).Ok()
			self.Event(dict(kind="start", host_ns=clock.HostNs(), seed=self.seed, port=port,
				setup=box.Setup() + box.SessionStarts()))
			self.say("campy-box: {} inputs, {} outputs, {} servos, {} links, {} states, board on {}; seed {}.".format(
				len(box.inputs), len(box.outputs), len(box.servos), len(box.links), len(box.states), port, self.seed))
			self.Loop()
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
			self.say("campy-box: {} trials; ended: {}. Summary in {}.json.".format(self.trial, self.end_why, out))
		return code

	def Summary(self, out):
		board = self.board
		return dict(task=os.path.abspath(self.args.task) if getattr(self.args, "task", None) else None,
			out=os.path.abspath(out), seed=self.seed, trials=self.trial, ended=self.end_why, last_state=self.state,
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
			parts.append("{} {}{} -> {}".format(tr.input.name, "on" if tr.active else "off",
				" x{}".format(tr.count) if tr.count > 1 else "", tr.to))
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
	args = p.parse_args(argv)
	try:
		box = Box(Load(args.task))
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
