"""Camera processes print at the same time, so each message goes out in one write."""

import sys, types
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


def test_ready_to_trigger_is_one_write(monkeypatch):
	# Name and text used to be written separately, so two cameras made
	# "Camera0Camera1 ready to trigger." and " ready to trigger.": no line had Camera0's
	out = Stdout()
	monkeypatch.setattr(sys, "stdout", out)
	unicam.SetReadyQueue(None)
	cam = types.SimpleNamespace(StartGrabbing=lambda camera: True)
	assert unicam.StartGrabbing(object(), {"n_cam": 0, "cameraName": "Camera0"}, cam)
	assert out.writes == ["Camera0 ready to trigger.\n"]


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
