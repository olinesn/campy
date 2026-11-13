"""campy-box (campy/box.py): an operant box's task file, and its runs, on a scripted board and on the board's sketch
running in real time (tests/arduino_sim live)."""

import argparse, json, os, subprocess, sys, threading, time

import pytest
import yaml

from campy import box, clock
from campy.trigger import outputs

TASK = """
board: {trigger_pin: 10, watchdog_ms: 250}
inputs:
  poke_left:  {input: 0, pin: A0, pull: up}
  poke_right: {input: 1, pin: A1, pull: up}
  lever:      {input: 2, pin: A2, pull: down, debounce_ms: 5}
outputs:
  valve: {channel: 0, pin: D2, pulse_ms: 40}
  cue:   {channel: 1, pin: D3, max_on_s: 60}
  mark:  {channel: 2, pin: D4, pulse_ms: 5}
servos:
  door: {servo: 0, pin: D11, positions: {closed: 1000, open: 2000}, safe: closed, speed_us_per_s: 2000}
links:
  mark_pokes:  {when: poke_left, do: {output: mark}, armed: always}
  reward_left: {when: poke_left, do: {output: valve}}
task:
  start: iti
  end: {trials: 2}
  states:
    iti:
      do: [{servo: door, to: closed}, {output: cue, action: off}]
      after_s: 0.05
      next: ready
    ready:
      trial: true
      do: [{servo: door, to: open}, {output: cue, action: on}]
      arm: {reward_left: 1}
      on: {poke_left: reward, poke_right: iti}
      after_s: 30
      next: iti
    reward:
      do: [{output: cue, action: off}, {note: rewarded}]
      after_s: 0.05
      next: iti
"""


def Task(text=TASK, **changes):
	config = yaml.safe_load(text)
	for path, value in changes.items():
		where = config
		keys = path.split("__")
		for k in keys[:-1]:
			where = where[k]
		if value is None:
			del where[keys[-1]]
		else:
			where[keys[-1]] = value
	return config


def test_the_board_is_set_up_from_a_clean_slate():
	b = box.Box(Task())
	setup = b.Setup()
	# Everything freed first, whatever another task left on the board, then this box
	assert setup[:21] == ["alloff"] + ["link {} -1".format(n) for n in range(8)] + \
		["in {} -1".format(n) for n in range(8)] + ["servo {} -1".format(n) for n in range(4)]
	assert setup[21:] == ["pin 3 -1", "pin 4 -1", "pin 5 -1", "pin 6 -1", "pin 7 -1", "trig 10", "watchdog 250",
		"pin 0 2", "group 0 0", "max 0 0", "pin 1 3", "group 1 0", "max 1 60000000", "pin 2 4", "group 2 0", "max 2 0",
		"in 0 100 1 0 1000", "in 1 101 1 0 1000", "in 2 102 2 1 5000", "servo 0 11 1000 2000 1000 2000",
		"link 0 0 1 1 2 5000", "link 1 0 1 1 0 40000"]
	assert b.SessionStarts() == ["arm 0 -1"]


def test_check_shows_the_states():
	said = []
	box.Check(box.Box(Task()), say=said.append)
	text = "\n".join(said)
	assert "  iti: does door to closed, cue off; after 0.05 s -> ready" in text
	assert "  ready: starts a trial; does door to open, cue on; arms reward_left (x1); poke_left on -> reward; " \
		"poke_right on -> iti; after 30 s -> iti" in text


def test_on_and_off_unquoted_are_words():
	# YAML 1.1 reads them as true and false
	b = box.Box(Task())
	actions = b.states["iti"].actions
	assert [a.command for a in actions] == ["move 0 1000", "off 1"]
	assert [t.to for t in b.states["ready"].on] == ["reward", "iti"]


@pytest.mark.parametrize("changes, message", [
	(dict(inputs__lever__pin="A9"), r"pin 'A9'"),
	(dict(inputs__lever__pin="D2"), r"output valve and input lever are both on D2|input lever and output valve"),
	(dict(servos__door__pin="D10"), r"the camera trigger and servo door are both on D10"),
	(dict(servos__door__safe="ajar"), r"'ajar' is none of its positions"),
	(dict(servos__door__positions={"closed": 1000, "open": 2600}), r"2600 us is outside"),
	(dict(links__reward_left__when="nose"), r"when: must be one of the inputs"),
	(dict(task__states__ready__on={"poke_middle": "reward"}), r"no input or pose 'poke_middle'"),
	(dict(task__states__ready__next="nowhere"), r"no state 'nowhere'"),
	(dict(task__states__ready__arm={"mark_pokes": 1}), r"armed always already"),
	(dict(task__states__reward__next=None), r"after_s needs next"),
	(dict(task__states__reward__dance=True), r"unknown keys dance"),
	(dict(task__start="begin"), r"start: no state 'begin'"),
	(dict(outputs__cue__channel=0), r"outputs: two of them have the same number"),
	(dict(board__watchdog_ms=20), r"watchdog_ms must be 50-10000"),
	(dict(task__states__reward__do=[{"output": "cue", "action": "burst"}]), r"burst needs the output to have a train"),
])
def test_mistakes_in_the_file_are_said(changes, message):
	with pytest.raises(box.BoxError, match=message):
		box.Box(Task(**changes))


# ---------------------------------------------------------------- a run, on a scripted board

class FakeBoard:
	"""A serial port whose board answers every command as campy_outputs 3 does, and says what the test says."""

	def __init__(self):
		self.written, self.incoming = [], []
		self.lock = threading.Lock()

	def write(self, data):
		command = data.decode().strip()
		words = command.split()
		now = clock.HostNs() // 1000
		with self.lock:
			self.written.append(command)
			if command == "hb":
				return
			if command == "id":
				reply = "ok campy_outputs {}".format(outputs.VERSION)
			elif command == "time":
				reply = "ok time {} 0 -1".format(now)
			elif words[0] in ("pulse", "on"):
				reply = "ok {} {} {} -1 -1".format(words[0], words[1], now)
			elif words[0] == "off":
				reply = "ok off {} {}".format(words[1], now)
			elif words[0] == "move":
				reply = "ok move {} {} {} -1 -1".format(words[1], words[2], now)
			else:
				reply = "ok " + command
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

	def Sent(self):
		with self.lock:
			return [c for c in self.written if c not in ("hb", "time")]

	def close(self):
		pass


def until(test, seconds=5.0):
	deadline = time.monotonic() + seconds
	while not test() and time.monotonic() < deadline:
		time.sleep(0.002)
	return test()


def start(config, tmp_path, port=None, duration_s=0.0):
	args = argparse.Namespace(task=None, port="", out=str(tmp_path / "box"), check=False, duration_s=duration_s)
	runner = box.Runner(args, box.Box(config), connection=port)
	said = []
	runner.say = said.append
	result = {}
	thread = threading.Thread(target=lambda: result.setdefault("code", runner.Run()), daemon=True)
	thread.start()
	return runner, thread, result, said


def events(tmp_path):
	return [json.loads(l) for l in open(str(tmp_path / "box.events.jsonl"))]


def test_a_poke_rewards_and_the_task_ends_after_its_trials(tmp_path):
	port = FakeBoard()
	runner, thread, result, said = start(Task(), tmp_path, port)
	try:
		for trial in (1, 2):
			assert until(lambda: runner.state == "ready" and runner.trial == trial), (runner.state, said)
			assert until(lambda: "arm 1 1" in port.Sent()[-3:])
			t = 1_000_000 * trial
			port.Say("i 0 1 {} {} 300".format(t, 10 * trial)) # poke_left: the board fires both links itself
			port.Say("x 0 0 {} {} 1".format(t, 10 * trial))
			port.Say("x 1 0 {} {} 1".format(t, 10 * trial))
			port.Say("p 0 {} {} {}".format(10 * trial, t + 5, t + 40005))
			assert until(lambda: runner.state != "ready"), said
		thread.join(5)
	finally:
		runner.stop.set()
		thread.join(5)
	assert result["code"] == 0, said
	sent = port.Sent()
	# The setup, the always-armed link, then the states: ready arms the reward once; leaving it disarms it
	assert sent[:2] == ["id", "alloff"] and "arm 0 -1" in sent
	after = sent[sent.index("arm 0 -1") + 1:]
	assert after[:2] == ["move 0 1000", "off 1"]
	assert after[2:5] == ["move 0 2000", "on 1", "arm 1 1"]
	assert after[5:7] == ["arm 1 0", "off 1"]
	assert after[-1] == "alloff" # the end: the box made safe
	ev = events(tmp_path)
	states = [(e["state"], e["why"], e["trial"]) for e in ev if e["kind"] == "state"]
	assert states == [("iti", "start", 0), ("ready", "timer", 1), ("reward", "input", 1), ("iti", "timer", 1),
		("ready", "timer", 2), ("reward", "input", 2), ("iti", "timer", 2)]
	reward = [e for e in ev if e["kind"] == "state" and e["state"] == "reward"][0]
	assert (reward["input"], reward["board_us"], reward["frame"], reward["after_us"]) == ("poke_left", 1_000_000, 10,
		300)
	pokes = [e for e in ev if e["kind"] == "input"]
	assert [(e["input"], e["active"], e["frame"], e["state"]) for e in pokes] == [("poke_left", 1, 10, "ready"),
		("poke_left", 1, 20, "ready")]
	assert all(e["host_ns"] is not None for e in pokes) # on campy's host clock
	links = [(e["link"], e["done"]) for e in ev if e["kind"] == "link"]
	assert links == [("mark_pokes", True), ("reward_left", True)] * 2
	pulses = [e for e in ev if e["kind"] == "pulse"]
	assert [(p["output"], p["frame"], p["fall_us"] - p["rise_us"]) for p in pulses] == [("valve", 10, 40000),
		("valve", 20, 40000)]
	assert [e["text"] for e in ev if e["kind"] == "note"] == ["rewarded", "rewarded"]
	assert ev[-1]["kind"] == "end" and ev[-1]["why"] == "2 trials"
	summary = json.load(open(str(tmp_path / "box.json")))
	assert summary["trials"] == 2 and summary["ended"] == "2 trials"
	assert summary["counts"]["inputs"]["poke_left"] == 2 and summary["counts"]["links"]["reward_left"] == 2
	assert summary["counts"]["states"] == {"iti": 3, "ready": 2, "reward": 2}
	assert os.path.exists(str(tmp_path / "box.board.txt"))


def test_a_lever_pressed_five_times(tmp_path):
	# Fixed ratio 5: the 5th press in the state moves on; releases don't count
	config = Task(task__states__ready__on={"lever": {"to": "reward", "count": 5}}, task__end={"trials": 1})
	port = FakeBoard()
	runner, thread, result, said = start(config, tmp_path, port)
	try:
		assert until(lambda: runner.state == "ready")
		for n in range(4):
			port.Say("i 2 1 {} 0 -1".format(1000 * n))
			port.Say("i 2 0 {} 0 -1".format(1000 * n + 500))
		time.sleep(0.1)
		assert runner.state == "ready"
		port.Say("i 2 1 9000 0 -1")
		thread.join(5)
	finally:
		runner.stop.set()
		thread.join(5)
	ev = events(tmp_path)
	reward = [e for e in ev if e["kind"] == "state" and e["state"] == "reward"]
	assert len(reward) == 1 and reward[0]["board_us"] == 9000 and reward[0]["frame"] is None # no camera yet


def test_after_the_watchdog_the_task_starts_over(tmp_path):
	config = Task(task__end={"trials": 5})
	port = FakeBoard()
	runner, thread, result, said = start(config, tmp_path, port, duration_s=3)
	try:
		assert until(lambda: runner.state == "ready")
		n = len(port.Sent())
		port.Say("wd 123456")
		assert until(lambda: runner.counts["watchdog"] == 1)
		assert until(lambda: "arm 0 -1" in port.Sent()[n:]) # the always-armed link again
		assert until(lambda: runner.state == "ready" and runner.trial == 2)
	finally:
		runner.stop.set()
		thread.join(5)
	ev = events(tmp_path)
	whys = [(e["state"], e["why"]) for e in ev if e["kind"] == "state"]
	assert ("iti", "watchdog") in whys
	assert any("watchdog made the box safe" in s for s in said)


def test_random_times_and_choices_come_again_with_the_seed(tmp_path):
	config = Task(task__seed=7, task__states__iti__after_s=[0.01, 0.03],
		task__states__iti__next={"ready": 1, "reward": 1}, task__states__ready__after_s=0.02, task__end={"trials": 3})
	picks = []
	for run in range(2):
		folder = tmp_path / str(run)
		folder.mkdir()
		port = FakeBoard()
		runner, thread, result, said = start(config, folder, port, duration_s=1.0)
		thread.join(5)
		picks.append([e["state"] for e in events(folder) if e["kind"] == "state"])
		assert json.load(open(str(folder / "box.json")))["seed"] == 7
	assert picks[0] == picks[1] and "reward" in picks[0]


def test_a_board_that_restarts_stops_the_task(tmp_path):
	port = FakeBoard()
	runner, thread, result, said = start(Task(), tmp_path, port)
	try:
		assert until(lambda: runner.state == "ready")
		port.Say("ready campy_outputs 3")
		thread.join(5)
	finally:
		runner.stop.set()
		thread.join(5)
	assert json.load(open(str(tmp_path / "box.json")))["ended"] == "board restarted"


def test_main_check(tmp_path, capsys):
	path = tmp_path / "task.yaml"
	path.write_text(TASK)
	assert box.Main([str(path), "--check"]) == 0
	assert "reward_left (x1)" in capsys.readouterr().out
	path.write_text(TASK.replace("pull: down", "pull: sideways"))
	assert box.Main([str(path), "--check"]) == 2


# ---------------------------------------------------------------- the board's sketch, in real time

def pty():
	import tty
	master, slave = os.openpty()
	tty.setraw(slave)
	return master, slave


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pseudo-terminal for the simulated board")
def test_pokes_on_the_simulated_board(tmp_path, outputs_sim):
	"""
	The whole path: campy-box, the sketch in real time on a pty, a 60 Hz camera trigger, and a nose poke on A0
	(pull-up, active low) every 0.4 s. Each poke in the ready state is rewarded by the board itself (a link), with
	the frame it fell in; the task counts its trials and the door servo moves.
	"""
	period_us = 16667
	pokes = "30:500000:400000:30000:1000" # A0 (pin 30 on the simulated board): 30 ms pokes every 0.4 s
	env = dict(os.environ, SIM_INPUT="10:300000:{}:3000:100000;{}".format(period_us, pokes))
	master, slave = pty()
	log_path = tmp_path / "sim.log"
	with open(str(log_path), "w") as log:
		sim = subprocess.Popen([outputs_sim, "live"], stdin=master, stdout=master, stderr=log, env=env)
	try:
		assert until(lambda: sim.poll() is not None or "ready campy_outputs" in log_path.read_text(), 60)
		config = Task(task__end={"trials": 3}, task__states__iti__after_s=0.1,
			servos__door__speed_us_per_s=0)
		args = argparse.Namespace(task=None, port=os.ttyname(slave), out=str(tmp_path / "box"), check=False,
			duration_s=30)
		runner = box.Runner(args, box.Box(config))
		said = []
		runner.say = said.append
		code = runner.Run()
	finally:
		sim.terminate()
		sim.wait()
		os.close(slave)
		os.close(master)
	assert code == 0, said
	ev = events(tmp_path)
	summary = json.load(open(str(tmp_path / "box.json")))
	assert summary["ended"] == "3 trials", (summary, said)
	rewards = [e for e in ev if e["kind"] == "link" and e["link"] == "reward_left"]
	assert len(rewards) == 3 and all(e["done"] for e in rewards)
	pokes = [e for e in ev if e["kind"] == "input" and e["active"] == 1]
	period = 400000
	for e in pokes:
		# Each poke's board time is when the simulated sensor pulled the pin down, and its frame the one it fell in
		assert (e["board_us"] - 500000) % period < 3000, e
		assert e["frame"] >= 1 and 0 <= e["after_us"] < period_us + 3000, e
	valve = [e for e in ev if e["kind"] == "pulse" and e["output"] == "valve"]
	assert len(valve) == 3
	for r, p in zip(rewards, valve):
		# (in real time the simulated board's clock steps tens of microseconds at a time: the exact timing is
		# tests/test_outputs_sketch.py's, on a simulated clock)
		assert 0 <= p["rise_us"] - r["board_us"] < 5000 and 40000 <= p["fall_us"] - p["rise_us"] < 41000, (r, p)
		assert p["frame"] == r["frame"]
	moves = [e for e in ev if e["kind"] == "servo"]
	assert {e["position"] for e in moves} == {"open", "closed"}
	sim_log = log_path.read_text()
	assert "pwm" in sim_log # the door's PWM


# ---------------------------------------------------------------- with a recording (campy-session's box: section)

def test_with_a_recording_it_waits_for_the_first_frame_and_ends_with_the_stop_file(tmp_path):
	port = FakeBoard()
	paths = {k: str(tmp_path / ("box." + k)) for k in ("ready", "stop", "status")}
	args = argparse.Namespace(task=None, port="", out=str(tmp_path / "box"), check=False, duration_s=0,
		start_with_frames=True, stop_file=paths["stop"], recording_folder="", launcher_pid=0,
		ready_file=paths["ready"], status_file=paths["status"])
	runner = box.Runner(args, box.Box(Task()), connection=port)
	said = []
	runner.say = said.append
	result = {}
	thread = threading.Thread(target=lambda: result.setdefault("code", runner.Run()), daemon=True)
	thread.start()
	try:
		assert until(lambda: os.path.exists(paths["ready"]))
		time.sleep(0.3)
		assert runner.state is None # no camera frame yet
		assert until(lambda: os.path.exists(paths["status"]) and json.load(open(paths["status"]))["waiting"])
		port.Say("i 0 1 500 0 -1") # a poke before the task: logged, nothing moves
		port.Say("t 1 1000")
		assert until(lambda: runner.state == "ready")
		assert until(lambda: json.load(open(paths["status"]))["trial"] == 1)
		open(paths["stop"], "w").write("stop\n")
		thread.join(5)
	finally:
		runner.stop.set()
		thread.join(5)
	assert result["code"] == 0, said
	ev = events(tmp_path)
	first = [e for e in ev if e["kind"] == "state"][0]
	assert first["why"] == "start" and first["first_frame"] == 1
	assert [e["state"] for e in ev if e["kind"] == "input"] == [None]
	summary = json.load(open(str(tmp_path / "box.json")))
	assert summary["ended"] == "stop file" and not summary["task_ended"]
	status = json.load(open(paths["status"]))
	assert status["ended"] == "stop file" and not status["waiting"]
	assert any("starts at the first camera frame" in s for s in said)


def test_it_stops_waiting_when_campy_session_is_gone(tmp_path):
	gone = subprocess.Popen([sys.executable, "-c", "pass"])
	gone.wait()
	args = argparse.Namespace(task=None, port="", out=str(tmp_path / "box"), check=False, duration_s=0,
		start_with_frames=True, stop_file="", recording_folder="", launcher_pid=gone.pid, ready_file="",
		status_file="")
	runner = box.Runner(args, box.Box(Task()), connection=FakeBoard())
	runner.say = lambda text: None
	assert runner.Run() == 0
	assert json.load(open(str(tmp_path / "box.json")))["ended"] == "campy-session ended before the recording started"


def test_the_task_file_is_kept_beside_the_logs(tmp_path):
	path = tmp_path / "task.yaml"
	path.write_text(TASK)
	args = argparse.Namespace(task=str(path), port="", out=str(tmp_path / "out" / "box"), check=False, duration_s=0.3)
	runner = box.Runner(args, box.Box(Task()), connection=FakeBoard())
	runner.say = lambda text: None
	assert runner.Run() == 0
	assert (tmp_path / "out" / "box.task.yaml").read_text() == TASK


# ---------------------------------------------------------------- poses: campy-live's results in the task

POSE_TASK = TASK + """
live: {stale_ms: 150}
regions:
  port: {box: [[0, 0, 0], [100, 100, 50]]}
poses:
  m2_at_port: {when: {inside: {region: port, animal: 2, node: nose}}, enter_ms: 30}
"""


def pose_task(**changes):
	config = Task(POSE_TASK, task__end={"trials": 5}, **changes)
	# ready: waits for mouse 2 at the port; there, a poke rewards only while it is still there, else -> iti
	config["task"]["states"]["ready"]["on"] = {"m2_at_port": "at_port"}
	config["task"]["states"]["at_port"] = {"on": {"poke_left": {"to": "reward", "if": "m2_at_port", "else": "iti"},
		"m2_at_port": {"to": "ready", "edge": "inactive"}}}
	return config


def test_poses_in_the_file_are_checked():
	b = box.Box(pose_task())
	assert b.states["at_port"].on[0].guard == ("m2_at_port", True) and b.states["at_port"].on[0].otherwise == "iti"
	said = []
	box.Check(b, say=said.append)
	text = "\n".join(said)
	assert "Poses (checked against campy-live's results when they come, campy-live): m2_at_port" in text
	assert "poke_left on if m2_at_port (else -> iti) -> reward; m2_at_port lets go -> ready" in text
	bad = pose_task()
	bad["task"]["states"]["at_port"]["on"]["poke_left"]["if"] = "m3_at_port"
	with pytest.raises(box.BoxError, match=r"if: 'm3_at_port' is no pose"):
		box.Box(bad)
	bad = pose_task()
	bad["poses"]["poke_left"] = {"when": {"test": {"every": 2}}}
	with pytest.raises(box.BoxError, match=r"two share a name"):
		box.Box(bad)
	# Fitted to campy-live's results only when they come: a node they don't have is said then
	from test_rules import desc
	bad = pose_task()
	bad["poses"]["m2_at_port"]["when"]["inside"]["node"] = "tail_tip"
	with pytest.raises(box.RulesError, match=r"tail_tip"):
		box.Box(bad).Poses(desc())


def test_a_poke_rewarded_only_while_mouse_2_is_at_the_port(tmp_path):
	from test_livefeed import name
	from test_rules import Feed, make_row
	prefix = name()
	feed = Feed(prefix)
	port = FakeBoard()
	args = argparse.Namespace(task=None, port="", out=str(tmp_path / "box"), check=False, duration_s=10,
		live_name=prefix)
	runner = box.Runner(args, box.Box(pose_task()), connection=port)
	said = []
	runner.say = said.append
	result = {}
	thread = threading.Thread(target=lambda: result.setdefault("code", runner.Run()), daemon=True)
	thread.start()
	frame = [0]

	def sets(where, seconds):
		# Sets at 60 Hz with mouse 2's nose at where
		end = time.monotonic() + seconds
		while time.monotonic() < end:
			frame[0] += 1
			feed.Write(make_row(frame[0], {2: {"nose": where}}, t_ns=clock.HostNs()))
			time.sleep(1 / 60)

	inside, outside = [50, 50, 10], [500, 50, 10]
	try:
		assert until(lambda: runner.state == "ready" and runner.counts["live_sets"] == 0 and
			any("campy-live's results: 1 poses" in s for s in said) or runner.state == "ready", 10)
		sets(outside, 0.3)
		assert runner.state == "ready" # mouse 2 elsewhere: no move
		sets(inside, 0.3)
		assert until(lambda: runner.state == "at_port"), (runner.state, said)
		port.Say("i 0 1 5000000 300 100") # a poke while mouse 2 is there: rewarded
		assert until(lambda: runner.state == "reward")
		assert until(lambda: runner.state == "ready", 5)
		sets(inside, 0.3)
		assert until(lambda: runner.state == "at_port")
		sets(outside, 0.1)
		assert until(lambda: runner.state == "ready") # mouse 2 left before poking: back to ready
		sets(inside, 0.3)
		assert until(lambda: runner.state == "at_port")
		time.sleep(0.4) # no set for longer than stale_ms: the pose lets go
		assert until(lambda: runner.state == "ready")
	finally:
		runner.stop.set()
		thread.join(5)
		feed.Close()
	assert result["code"] == 0, said
	ev = events(tmp_path)
	states = [(e["state"], e["why"]) for e in ev if e["kind"] == "state"]
	assert ("at_port", "pose") in states and ("reward", "input") in states
	reward = [e for e in ev if e["kind"] == "state" and e["state"] == "reward"][0]
	assert reward["guard"] == "m2_at_port" and reward["guard_holds"] is True
	poses = [(e["holds"], e["why"]) for e in ev if e["kind"] == "pose"]
	# Held three times: let go when the sets stopped (stale: while the task rewarded, and at the end) and when mouse
	# 2 left. (A test computer busy enough to hold the sets up past stale_ms adds a let-go and a hold: allowed)
	assert poses[0] == (True, None) and (False, None) in poses and poses[-1] == (False, "stale"), poses
	assert all(a[0] != b[0] for a, b in zip(poses, poses[1:])), poses # holds and lets go in turn
	assert json.load(open(str(tmp_path / "box.json")))["counts"]["poses"]["m2_at_port"] == len(poses) // 2 >= 3


def runner_alone(config):
	# A Runner with no board: its moves only
	args = argparse.Namespace(task=None, port="", out="", check=False, duration_s=0)
	runner = box.Runner(args, box.Box(config))
	runner.Send = lambda command, record=None: None
	runner.Event = lambda record: None
	return runner


def test_a_poke_from_the_wrong_mouse_goes_to_else():
	config = pose_task()
	del config["task"]["states"]["at_port"]["on"]["m2_at_port"]
	runner = runner_alone(config)
	runner.Enter("at_port", dict(why="test"))
	# m2_at_port doesn't hold: the poke goes to else
	runner.Edge(runner.box.inputs["poke_left"], True, dict(board_us=1, host_ns=1, frame=1, after_us=0))
	assert runner.state == "iti"


def test_a_pose_that_already_holds_moves_the_state_at_once():
	runner = runner_alone(pose_task())
	runner.holding["m2_at_port"] = True
	runner.Enter("ready", dict(why="test"))
	assert runner.state == "at_port" # mouse 2 at the port already
	runner.holding["m2_at_port"] = False
	runner.Enter("at_port", dict(why="test"))
	assert runner.state == "ready" # entered without mouse 2 there: its lets-go move at once
