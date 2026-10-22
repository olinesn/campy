"""The trigger starts once every camera is grabbing, from campy's main process only."""

import json, os, re, sys, queue, subprocess, threading, time

import pytest

from campy import configurator
from campy.cameras import unicam
from campy.trigger import arduino, trigger

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def params(n=2, timeout=0.5):
	p = configurator.DefaultParams()
	p.update(numCams=n, readyTimeoutSec=timeout)
	return configurator.FillCameraLists(p)


class Result:
	# Stands in for the pool's AsyncResult
	def __init__(self, done=False):
		self.done = done

	def ready(self):
		return self.done


def test_all_ready():
	q, said = queue.Queue(), []
	q.put((1, "Camera1", True, ""))
	q.put((0, "Camera0", True, ""))
	ready, missing = unicam.WaitForCameras(q, params(), Result(), log=said.append)
	assert sorted(ready) == ["Camera0", "Camera1"] and missing == {}
	assert said == ["All 2 cameras are ready."]


def test_a_camera_that_fails_to_open_is_named():
	q, said = queue.Queue(), []
	q.put((0, "Camera0", True, ""))
	q.put((1, "Camera1", False, "failed to open: no such device"))
	ready, missing = unicam.WaitForCameras(q, params(), Result(), log=said.append)
	assert ready == ["Camera0"]
	assert missing == {"Camera1": "failed to open: no such device"}
	assert said == ["1 of 2 cameras are ready. Not ready: Camera1 (failed to open: no such device)."]


def test_waits_no_longer_than_ready_timeout():
	q, said = queue.Queue(), []
	q.put((0, "Camera0", True, ""))
	t0 = time.monotonic()
	ready, missing = unicam.WaitForCameras(q, params(timeout=0.5), Result(), log=said.append)
	assert 0.4 < time.monotonic() - t0 < 2
	assert missing == {"Camera1": "not ready after 0.5 s (readyTimeoutSec)"}


def test_stops_waiting_when_the_camera_processes_end():
	q = queue.Queue()
	t0 = time.monotonic()
	ready, missing = unicam.WaitForCameras(q, params(timeout=30), Result(done=True), log=lambda s: None)
	assert time.monotonic() - t0 < 2
	assert ready == [] and set(missing.values()) == {"stopped before it was ready"}


def test_signal_ready_reports_only_when_asked():
	cam = {"n_cam": 3, "cameraName": "Camera3"}
	unicam.SetReadyQueue(None)
	unicam.SignalReady(cam) # no queue: nothing happens
	q = queue.Queue()
	unicam.SetReadyQueue(q)
	try:
		unicam.SignalReady(cam, False, "why")
		assert q.get_nowait() == (3, "Camera3", False, "why")
	finally:
		unicam.SetReadyQueue(None)


def test_a_port_that_does_not_open_stops_campy(capsys):
	with pytest.raises(arduino.TriggerError) as e:
		arduino.OpenTriggers({}, {"serialPort": "/dev/no-such-port", "digitalPins": [2]})
	assert "Could not open the trigger Arduino's serial port /dev/no-such-port (serialPort)" in str(e.value)
	assert "plug it back in" in str(e.value)
	assert arduino.StartTriggers({}, {}) == {}
	arduino.StopTriggers({}) # used to raise KeyError


class Port:
	# Stands in for serial.Serial: keeps what campy writes, and answers the
	# stop command as trigger.ino does, unless it is busy (ignores the first
	# `busy` commands) or dead (answer None)
	def __init__(self, answer="\r\nFrame rate set to: 0.00 fps.\r\nFrame period set to: 4294967295 microseconds.", busy=0):
		self.written, self.writes, self.waiting = b"", [], ""
		self.answer, self.busy, self.closed = answer, busy, False

	def reset_input_buffer(self):
		self.waiting = ""

	def write(self, data):
		self.written += data
		self.writes.append((time.monotonic(), data.decode()))
		if self.answer is not None and len(self.writes) > self.busy:
			self.waiting += self.answer

	def read(self, n):
		if not self.waiting:
			time.sleep(0.01) # the timeout
		data, self.waiting = self.waiting[:n], self.waiting[n:]
		return data.encode()

	def close(self):
		self.closed = True


def open_with(monkeypatch, port):
	monkeypatch.setattr(arduino.serial, "Serial", lambda **kwargs: port)
	return arduino.OpenTriggers({}, {"serialPort": "COM7", "digitalPins": [2, 3]})


def test_the_check_sends_the_stop_command(monkeypatch, capsys):
	port = Port()
	systems = open_with(monkeypatch, port)
	assert port.written == b"2,2,3,-1,-1,0" # once: it answered
	assert systems["serial"] is port and not port.closed
	assert systems["triggerCheckedAt"] == pytest.approx(port.writes[0][0], abs=0.05) # when it answered
	assert "Trigger Arduino on COM7 answered." in capsys.readouterr().out


def test_the_check_tries_again_while_the_arduino_is_busy(monkeypatch):
	# Still busy with a command (a run that stopped a moment ago): it throws
	# this one away. The start command waits for its answer to the last one.
	monkeypatch.setattr(arduino, "RESEND_S", 0.1)
	port = Port(busy=2)
	systems = open_with(monkeypatch, port)
	assert [data for _, data in port.writes] == ["2,2,3,-1,-1,0"] * 3
	assert systems["triggerCheckedAt"] == pytest.approx(port.writes[-1][0], abs=0.05)


def test_an_arduino_that_does_not_answer_stops_campy(monkeypatch):
	monkeypatch.setattr(arduino, "CHECK_TIMEOUT_S", 0.5)
	monkeypatch.setattr(arduino, "RESEND_S", 0.2)
	port = Port(answer=None)
	t0 = time.monotonic()
	with pytest.raises(arduino.TriggerError) as e:
		open_with(monkeypatch, port)
	assert 0.4 < time.monotonic() - t0 < 2
	assert str(e.value).startswith("The trigger Arduino on COM7 did not answer "
		"(no reply to the stop command 2,2,3,-1,-1,0 within 0.5 s). Unplug its USB cable")
	assert port.closed and len(port.writes) == 3


def test_another_frame_rate_is_not_the_answer(monkeypatch):
	# What a sketch says about some other command is not the check's answer
	monkeypatch.setattr(arduino, "CHECK_TIMEOUT_S", 0.3)
	with pytest.raises(arduino.TriggerError):
		open_with(monkeypatch, Port(answer="Frame rate set to: 80.00 fps."))
	for said in ("Frame rate set to: 0.00 fps.", "Frame rate set to: 0", "Frame rate set to: 0 fps"):
		assert arduino.ANSWER.search(said)
	for said in ("Frame rate set to: 0.50 fps.", "Frame rate set to: 05", "Frame rate set to: 80.00"):
		assert not arduino.ANSWER.search(said)


def test_the_start_command_waits_for_the_arduino(monkeypatch):
	monkeypatch.setattr(arduino, "START_AFTER_CHECK_S", 0.3)
	t0 = time.monotonic()
	assert arduino.WaitToStart({"triggerCheckedAt": t0 - 0.1})
	assert 0.15 < time.monotonic() - t0 < 1
	assert not arduino.WaitToStart({"triggerCheckedAt": time.monotonic()}, interrupted=lambda: True)
	assert arduino.WaitToStart({}) # no check, nothing to wait for


def test_one_pin_without_a_list(capsys):
	# digitalPins: 2 is one pin. len(2) failed, and the trigger did not start.
	p = params(n=1)
	p.update(digitalPins=2, frameRate=60)
	systems = arduino.StartTriggers({"serial": Port()}, p)
	assert systems["serial"].written == b"1,2,60,-1,0" # pin count, pins, fps, no test LED
	assert "is triggering pins [2] at 60 fps" in capsys.readouterr().out


# ------------------------------------------------------------------ end to end

def write_clip(path, frames=30):
	import numpy as np
	import imageio_ffmpeg
	w = imageio_ffmpeg.write_frames(path, (64, 48), fps=20, pix_fmt_in="rgb24", macro_block_size=16)
	w.send(None)
	for i in range(frames):
		w.send(np.full((48, 64, 3), 8 * i, np.uint8))
	w.close()


def start_campy(tmp_path, port, rec=10):
	# campy-acquire, two emulated cameras, the trigger Arduino on port
	config = tmp_path / "config.yaml"
	config.write_text("\n".join([
		# Forward slashes: in YAML's double quotes, Windows' C:\Users would be an escape
		"numCams: 2", 'cameraMake: "emu"', 'videoFolder: "{}"'.format(tmp_path.as_posix()), 'videoFilename: "emuclip.mp4"',
		"frameRate: 20", "recTimeInSec: {}".format(rec), "chunkLengthInSec: 1", "displayFrameRate: 0",
		'pixelFormatInput: "rgb24"', 'pixelFormatOutput: "yuv420p"', "gpuID: -1", 'codec: "h264"',
		'preflight: "off"', "startArduino: True", 'triggerController: "arduino"',
		'serialPort: "{}"'.format(port), "digitalPins: [2, 3]", "blinkPin: 13", "blinkHz: 43", ""]))
	env = dict(os.environ, PYTHONPATH=REPO, PYTHONUNBUFFERED="1")
	env.pop("IMAGEIO_FFMPEG_EXE", None)
	code = "import sys; sys.argv = ['campy-acquire', sys.argv[1]]; from campy.campy import Main; Main()"
	return subprocess.Popen([sys.executable, "-c", code, str(config)], cwd=str(tmp_path), env=env,
		stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def run_campy(tmp_path, port, rec=10):
	proc = start_campy(tmp_path, port, rec)
	try:
		proc.stdout_text, proc.stderr_text = proc.communicate(timeout=120)
	finally:
		proc.kill()
	return subprocess.CompletedProcess(proc.args, proc.returncode, proc.stdout_text, proc.stderr_text)


def sessions(tmp_path):
	# The runs' own folders, inside videoFolder (sessionFolder)
	return [d for d in tmp_path.iterdir() if re.fullmatch(r"\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d", d.name)]


def pty():
	# A pseudo-terminal, raw: campy opens the slave end as the Arduino's port
	import tty
	master, slave = os.openpty()
	tty.setraw(slave)
	return master, slave


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pseudo-terminal for the fake Arduino")
def test_trigger_starts_once_after_every_camera_is_grabbing(tmp_path, sim):
	# The Arduino: trigger.ino itself, in real time (tests/arduino_sim, live) on
	# the pty's master end. It logs what it received and said, and its pins.
	master, slave = pty()
	log_path = tmp_path / "sim.log"
	with open(str(log_path), "w") as log:
		arduino_sim = subprocess.Popen([sim, "live"], stdin=master, stdout=master, stderr=log)
	try:
		for name in ("Camera0", "Camera1"):
			(tmp_path / name).mkdir()
			write_clip(str(tmp_path / name / "clip.mp4")) # emu reads videoFilename minus its first 3 characters
		# The emulated cameras wait for campy to start the trigger, as triggered cameras do
		# (cameras/emu.py StartClock), then record 10 s from there.
		proc = run_campy(tmp_path, os.ttyname(slave))
		time.sleep(0.2)
	finally:
		arduino_sim.terminate()
		arduino_sim.wait()
		os.close(slave)
		os.close(master)
	out = proc.stdout
	assert proc.returncode == 0, out + proc.stderr

	received, said, pin2 = [], [], []
	for line in log_path.read_text().splitlines():
		kind, t, rest = line.split(" ", 2)
		if kind == "rx":
			received.append((int(t) / 1e6, rest))
		elif kind == "serial":
			said.append((int(t) / 1e6, rest))
		elif (t, rest) == ("2", "1"): # pin 2 rises
			pin2.append(int(kind) / 1e6)

	# The check's stop command, which it answered; one start command, from the
	# main process only (each camera process used to send its own), with the
	# test LED; and the stop command at the end, with it off
	assert re.fullmatch(r"(2,2,3,-1,-1,0)+2,2,3,20,13,43" r"2,2,3,-1,-1,0", "".join(data for _, data in received))
	# The start command came START_AFTER_CHECK_S after the check's answer
	# (the sketch says it as it reads the command), and the sketch took it
	start = [t for t, data in received if data.startswith("2,2,3,20")][0]
	answered = [t for t, text in said if text == "0.00" and t < start]
	assert answered and start - answered[-1] >= arduino.START_AFTER_CHECK_S - 0.05
	assert any(t >= start and text == "20.00" for t, text in said)
	assert pin2 and min(pin2) > start + 4.9 # 1 s for one more digit, then the sketch's 4 s

	session, = sessions(tmp_path)
	assert "Recording into {}\n".format(session) in out

	lines = out.splitlines()
	def first(pattern):
		found = [i for i, line in enumerate(lines) if re.search(pattern, line)]
		assert found, "no line matching {!r} in campy's output:\n{}".format(pattern, out)
		return found[0]
	assert first(r"Trigger Arduino on .* answered\.") < first(r"Recording into")
	triggering = first(r"Arduino on port .* is triggering pins \[2, 3\] at 20 fps, and blinking the test LED on pin 13 at 43 Hz")
	assert first(r"All 2 cameras are ready\.") < triggering
	assert first(r"Camera0 ready to trigger") < triggering and first(r"Camera1 ready to trigger") < triggering

	# Each video has the clip's frame size, not the config's (1152x1024, the
	# default), and every frame: it was opened at 1152x1024, and came out empty
	import imageio
	for name in ("Camera0", "Camera1"):
		assert "{}: frameWidth x frameHeight 1152x1024 changed to 64x48, the clip's frame size.".format(name) in out
		video = imageio.get_reader(str(session / name / "emuclip.mp4"))
		try:
			assert video.get_meta_data()["size"] == (64, 48)
			assert video.count_frames() == 200 # 10 s at 20 fps
		finally:
			video.close()

	# What campy-align reads: which cameras were grabbing when campy started the trigger
	record = json.loads((session / "trigger.json").read_text())
	assert (record["triggerStarted"], record["startedBy"]) == (True, "campy")
	assert record["readyBeforeTrigger"] == ["Camera0", "Camera1"] and record["notReady"] == {}
	assert isinstance(record["startedHostNs"], int) and record["startedHostNs"] > 0
	assert (record["blinkPin"], record["blinkHz"]) == (13, 43) # campy-synctest reads the rate


@pytest.mark.parametrize("how", ["no port", "no answer"])
def test_campy_stops_before_the_cameras_when_the_arduino_does_not_answer(tmp_path, how):
	if how == "no port":
		port = "COM250" if sys.platform == "win32" else "/dev/no-such-port"
	elif sys.platform == "win32":
		pytest.skip("needs a pseudo-terminal for the dead Arduino")
	else:
		master, slave = pty() # nothing answers on the other end
		port = os.ttyname(slave)
	t0 = time.monotonic()
	try:
		proc = run_campy(tmp_path, port)
	finally:
		if how == "no answer":
			os.close(slave)
			os.close(master)
	out = proc.stdout
	assert proc.returncode == 1, out + proc.stderr
	if how == "no port":
		assert "Could not open the trigger Arduino's serial port {} (serialPort): ".format(port) in out, out + proc.stderr
	else:
		assert "The trigger Arduino on {} did not answer (no reply to the stop command 2,2,3,-1,-1,0 within 8 s). "\
			"Unplug its USB cable and plug it back in (or power-cycle it), then run campy again.".format(port) in out
		assert time.monotonic() - t0 > arduino.CHECK_TIMEOUT_S
	# Before anything started: no camera, no recording folder
	assert "Camera0" not in out and "Recording into" not in out, out
	assert sessions(tmp_path) == []


def test_ctrl_c_stops_the_check_and_campy(monkeypatch):
	monkeypatch.setattr(arduino, "RESEND_S", 0.1)
	port, t0 = Port(answer=None), time.monotonic()
	with pytest.raises(arduino.TriggerInterrupted) as e:
		monkeypatch.setattr(arduino.serial, "Serial", lambda **kwargs: port)
		arduino.OpenTriggers({}, {"serialPort": "COM7", "digitalPins": [2]},
			interrupted=lambda: time.monotonic() - t0 > 0.35)
	assert time.monotonic() - t0 < 1 # not the 8 s timeout
	assert str(e.value) == "Stopped (Ctrl-C) while checking the trigger Arduino on COM7, before any camera started: nothing was recorded."
	assert port.closed and 3 <= len(port.writes) <= 5


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pseudo-terminal and SIGINT")
def test_ctrl_c_during_the_check_stops_campy_at_once(tmp_path):
	import select, signal
	master, slave = pty() # nothing answers on the other end
	port = os.ttyname(slave)
	proc = start_campy(tmp_path, port)
	try:
		# Once the check has sent its first stop command, Ctrl-C
		heard = b""
		while b"2,2,3,-1,-1,0" not in heard:
			assert select.select([master], [], [], 60)[0], "campy sent no stop command"
			heard += os.read(master, 1024)
		t0 = time.monotonic()
		proc.send_signal(signal.SIGINT)
		out, err = proc.communicate(timeout=30)
		stopped = time.monotonic() - t0
	finally:
		proc.kill()
		os.close(slave)
		os.close(master)
	assert proc.returncode == 1, out + err
	assert stopped < 3, out # not the check's 8 s
	assert "Stopped (Ctrl-C) while checking the trigger Arduino on {}, before any camera started: "\
		"nothing was recorded.".format(port) in out, out
	assert "did not answer" not in out
	assert "Camera0" not in out and "Recording into" not in out, out
	assert sessions(tmp_path) == []


def test_an_external_trigger_is_recorded_as_not_started_by_campy(tmp_path):
	p = params()
	p.update(videoFolder=str(tmp_path / "rec"), startArduino=False)
	path = trigger.SaveStart(p, ["Camera0", "Camera1"], {}, None)
	record = json.loads(open(path).read())
	assert (record["triggerStarted"], record["startedBy"], record["readyBeforeTrigger"]) == (False, "external", [])
