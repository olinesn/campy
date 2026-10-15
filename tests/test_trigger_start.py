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


def test_a_trigger_that_never_opened_is_left_alone(capsys):
	systems = arduino.OpenTriggers({}, {"serialPort": "/dev/no-such-port"})
	assert "serial" not in systems
	assert "Could not open the trigger's serial port /dev/no-such-port" in capsys.readouterr().out
	assert arduino.StartTriggers(systems, {}) is systems
	arduino.StopTriggers(systems) # used to raise KeyError


# ------------------------------------------------------------------ end to end

def write_clip(path, frames=30):
	import numpy as np
	import imageio_ffmpeg
	w = imageio_ffmpeg.write_frames(path, (64, 48), fps=20, pix_fmt_in="rgb24", macro_block_size=16)
	w.send(None)
	for i in range(frames):
		w.send(np.full((48, 64, 3), 8 * i, np.uint8))
	w.close()


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pseudo-terminal for the fake Arduino")
def test_trigger_starts_once_after_every_camera_is_grabbing(tmp_path):
	# The fake Arduino: campy opens the pty's slave end; we read what it sends from the master end
	master, slave = os.openpty()
	received = [] # (time, bytes), one per write
	def read_serial():
		while True:
			try:
				data = os.read(master, 1024)
			except OSError:
				return
			if not data:
				return
			received.append((time.monotonic(), data.decode()))
	threading.Thread(target=read_serial, daemon=True).start()

	for name in ("Camera0", "Camera1"):
		(tmp_path / name).mkdir()
		write_clip(str(tmp_path / name / "clip.mp4")) # emu reads videoFilename minus its first 3 characters
	# The emulated cameras free-run rather than wait for triggers, so they must still be
	# recording after the Arduino's 3 s boot: campy starts no trigger for cameras that stopped.
	config = tmp_path / "config.yaml"
	config.write_text("\n".join([
		"numCams: 2", 'cameraMake: "emu"', 'videoFolder: "{}"'.format(tmp_path), 'videoFilename: "emuclip.mp4"',
		"frameRate: 20", "recTimeInSec: 6", "chunkLengthInSec: 1", "displayFrameRate: 0",
		'pixelFormatInput: "rgb24"', 'pixelFormatOutput: "yuv420p"', "gpuID: -1", 'codec: "h264"',
		'preflight: "off"', "startArduino: True", 'triggerController: "arduino"',
		'serialPort: "{}"'.format(os.ttyname(slave)), "digitalPins: [2, 3]", ""]))

	env = dict(os.environ, PYTHONPATH=REPO, PYTHONUNBUFFERED="1")
	env.pop("IMAGEIO_FFMPEG_EXE", None)
	code = "import sys; sys.argv = ['campy-acquire', sys.argv[1]]; from campy.campy import Main; Main()"
	proc = subprocess.run([sys.executable, "-c", code, str(config)], cwd=str(tmp_path), env=env,
		capture_output=True, text=True, timeout=120)
	time.sleep(0.2)
	os.close(slave)
	out = proc.stdout
	assert proc.returncode == 0, out + proc.stderr

	lines = out.splitlines()
	def first(pattern):
		found = [i for i, line in enumerate(lines) if re.search(pattern, line)]
		assert found, "no line matching {!r} in campy's output:\n{}".format(pattern, out)
		return found[0]
	triggering = first(r"Arduino on port .* is triggering pins \[2, 3\]")
	assert first(r"All 2 cameras are ready\.") < triggering
	assert first(r"Camera0 ready to trigger") < triggering and first(r"Camera1 ready to trigger") < triggering

	# One start command, from the main process only (each camera process used to send its own),
	# and the stop command at the end
	sent = "".join(data for _, data in received) # writes run together: "2,2,3,202,2,3,-1"
	commands = [c.replace("20.0", "20") for c in re.findall(r"2,2,3,(?:-1|20(?:\.0)?)", sent)]
	assert commands == ["2,2,3,20", "2,2,3,-1"], sent
	for name in ("Camera0", "Camera1"):
		assert (tmp_path / name / "emuclip.mp4").stat().st_size > 0

	# What campy-align reads: which cameras were grabbing when campy started the trigger
	record = json.loads((tmp_path / "trigger.json").read_text())
	assert (record["triggerStarted"], record["startedBy"]) == (True, "campy")
	assert record["readyBeforeTrigger"] == ["Camera0", "Camera1"] and record["notReady"] == {}
	assert isinstance(record["startedHostNs"], int) and record["startedHostNs"] > 0


def test_an_external_trigger_is_recorded_as_not_started_by_campy(tmp_path):
	p = params()
	p.update(videoFolder=str(tmp_path / "rec"), startArduino=False)
	path = trigger.SaveStart(p, ["Camera0", "Camera1"], {}, None)
	record = json.loads(open(path).read())
	assert (record["triggerStarted"], record["startedBy"], record["readyBeforeTrigger"]) == (False, "external", [])
