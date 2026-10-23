"""Camera processes print at the same time, so each message goes out in one write."""

import sys, time, types, logging, threading
from collections import deque

import numpy as np

from campy import configurator, writer
from campy.cameras import unicam
from campy.utils import utils


class Stdout:
	# Stands in for sys.stdout: one entry per write() call
	def __init__(self):
		self.writes = []

	def write(self, text):
		self.writes.append(text)
		return len(text)

	def flush(self):
		pass


def test_say_writes_the_text_and_its_newline_together(monkeypatch):
	out = Stdout()
	monkeypatch.setattr(sys, "stdout", out)
	utils.Say("Camera0 collected 100 frames.")
	utils.Say(ValueError("no such device")) # anything print() takes
	assert out.writes == ["Camera0 collected 100 frames.\n", "no such device\n"]


def test_ready_to_trigger_is_one_write(monkeypatch, tmp_path):
	# Name and text used to be written separately, so two cameras made
	# "Camera0Camera1 ready to trigger." and " ready to trigger.": no line had Camera0's
	out = Stdout()
	monkeypatch.setattr(sys, "stdout", out)
	unicam.SetReadyQueue(None)
	cam = types.SimpleNamespace(StartGrabbing=lambda camera: True)
	assert unicam.StartGrabbing(object(), {"n_cam": 0, "cameraName": "Camera0", "videoFolder": str(tmp_path)}, cam)
	assert out.writes == ["Camera0 ready to trigger.\n"]


class StuckStdout(Stdout):
	# A console that takes no write until it is freed: a log file on a disk that has paused
	def __init__(self):
		Stdout.__init__(self)
		self.free = threading.Event()

	def write(self, text):
		self.free.wait()
		return Stdout.write(self, text)


def test_the_printer_writes_in_order_one_write_each_and_all_of_it_on_stop(monkeypatch):
	out, err = Stdout(), Stdout()
	err.writes = out.writes # one list: the order across both, as in a log file taking both
	monkeypatch.setattr(sys, "stdout", out)
	monkeypatch.setattr(sys, "stderr", err)
	log = logging.getLogger("campy.test_output_lines")
	log.propagate = False
	printer = utils.StartPrinter()
	handler = utils.PrinterHandler(printer)
	log.addHandler(handler)
	try:
		for i in range(1000):
			utils.Say("line {}".format(i))
		log.error("an error")
		log.info("not said: below WARNING, as without a handler")
		utils.Say("the last line")
	finally:
		log.removeHandler(handler)
		utils.StopPrinter()
	utils.Say("written directly again")
	assert out.writes == ["line {}\n".format(i) for i in range(1000)] + \
		["an error\n", "the last line\n", "written directly again\n"]


def fake_camera_module():
	def OpenCamera(cam_params):
		cam_params["cameraModel"] = "fake"
		cam_params["cameraSerialNo"] = "0"
		return object(), cam_params

	return types.SimpleNamespace(
		OpenCamera=OpenCamera,
		StartGrabbing=lambda camera: True,
		GrabFrame=lambda camera, n: n,
		GetImageArray=lambda grab: np.full((16, 16), grab % 256, dtype=np.uint8),
		GetTimeStamp=lambda grab: grab * 0.01,
		DisplayImage=lambda cam_params, q, grab: None,
		ReleaseFrame=lambda grab: None,
		CloseCamera=lambda cam_params, camera: None,
		)


def test_a_recording_prints_whole_lines(tmp_path, monkeypatch):
	# What one camera process says while it records, from grabbing and from the writer
	monkeypatch.setattr(unicam, "ImportCam", lambda make: fake_camera_module())
	p = configurator.DefaultParams()
	p.update(cameraMake="fake", cameraName="Camera0", videoFolder=str(tmp_path), videoFilename="v.mp4",
		frameRate=100, recTimeInSec=0.2, chunkLengthInSec=0.1, displayFrameRate=0, writeQueueMaxSec=0.05,
		frameWidth=16, frameHeight=16, pixelFormatInput="gray", pixelFormatOutput="yuv420p", gpuID=-1)
	(tmp_path / "Camera0").mkdir() # the writer makes it, in a recording
	out = Stdout()
	monkeypatch.setattr(sys, "stdout", out)
	writeQueue, stopWriteQueue = deque(), deque([], 1)
	unicam.GrabFrames(p, writeQueue, deque([], 2), deque([], 1), stopWriteQueue)
	writer.WriteFrames(p, writeQueue, deque([], 1), stopWriteQueue)
	monkeypatch.undo()

	said = "".join(out.writes)
	for message in ("Opened Camera0: fake", "Camera0 ready to trigger.", "Camera0 collected 10 frames",
			"Camera0: the encoder is 5 frames behind", "Camera0 saved 20 frames", "Camera0 dropped 15 of them",
			"Saved metadata for Camera0.", "using CPU to compress the stream.", "Closing video writer for Camera0."):
		assert message in said
	assert [w for w in out.writes if not (w.endswith("\n") and w.count("\n") == 1)] == []


def test_a_stuck_stdout_does_not_hold_up_the_camera_thread(tmp_path, monkeypatch):
	# The camera thread's messages (opened, ready, collected N frames, frames
	# dropped, saved) wait in the printer while stdout takes nothing; the
	# camera still grabs every frame, and once stdout is free, all of them
	# are written, in order
	monkeypatch.setattr(unicam, "ImportCam", lambda make: fake_camera_module())
	p = configurator.DefaultParams()
	p.update(cameraMake="fake", cameraName="Camera0", videoFolder=str(tmp_path), videoFilename="v.mp4",
		frameRate=100, recTimeInSec=0.2, chunkLengthInSec=0.1, displayFrameRate=0, writeQueueMaxSec=0.05,
		frameWidth=16, frameHeight=16, pixelFormatInput="gray", pixelFormatOutput="yuv420p", gpuID=-1)
	(tmp_path / "Camera0").mkdir()
	out = StuckStdout()
	monkeypatch.setattr(sys, "stdout", out)
	utils.StartPrinter()
	try:
		grab = threading.Thread(target=unicam.GrabFrames, daemon=True,
			args=(p, deque(), deque([], 2), deque([], 1), deque([], 1)))
		grab.start()
		grab.join(30)
		assert not grab.is_alive(), "the camera thread waited on stdout"
		assert out.writes == []
	finally:
		out.free.set()
		utils.StopPrinter()
	monkeypatch.undo()

	starts = ["Opened Camera0: fake", "Camera0 ready to trigger.",
		"Camera0: the encoder is 5 frames behind. Dropped 1 frames so far (latest: frame 6).",
		"Camera0 collected 10 frames at", "Camera0 collected 20 frames at", "Camera0 saved 20 frames at",
		"Camera0 dropped 15 of them", "Saved metadata for Camera0."]
	assert len(out.writes) == len(starts)
	for written, start in zip(out.writes, starts):
		assert written.startswith(start) and written.endswith("\n") and written.count("\n") == 1
