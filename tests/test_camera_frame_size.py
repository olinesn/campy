"""
The size a camera's video opens at, for the makes that set frameWidth and
frameHeight as the camera opens: a Basler (its cameraSettings file), against a
stand-in for pypylon, and an emulated camera (its clip). And a camera that
does not open.
"""

import importlib
import json
import logging
import os
import queue
import sys
import threading
import time
import types
from collections import deque

import numpy as np
import pytest

from campy import configurator, livefeed, writer
from campy.cameras import unicam

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def record(monkeypatch, tmp_path, make, device, **values):
	"""
	Runs campy.AcquireOneCamera, as a camera process does: it starts the camera
	thread, then the writer. imageio-ffmpeg's write_frames is stood in for.
	Returns the size the video was opened at (None if it was not opened), and
	the size (width, height) of each frame it was sent.
	"""
	p = configurator.FillCameraLists(configurator.DefaultParams())
	p.update(cameraMake=make, videoFolder=str(tmp_path), frameRate=40, recTimeInSec=0.25, chunkLengthInSec=1,
		displayFrameRate=0, pixelFormatInput="gray", preflight="off")
	p.update(values)
	systems = {make: {"system": types.SimpleNamespace(CreateDevice=lambda device: device),
		"deviceList": [device], "serials": ["21900001"]}}

	opened, sent = [], []
	def write_frames(path, size, **kwargs):
		opened.append(tuple(size))
		while True:
			frame = yield
			if frame is not None:
				sent.append((frame.shape[1], frame.shape[0]))
	monkeypatch.setattr(writer, "write_frames", write_frames)
	sleep = time.sleep
	monkeypatch.setattr(time, "sleep", lambda s: None if s >= 1 else sleep(s)) # the writer's 1 s wait to close

	# campy.py sets up its systems as it is imported
	monkeypatch.setattr(configurator, "ConfigureParams", lambda: p)
	monkeypatch.setattr(unicam, "LoadSystems", lambda params: systems)
	monkeypatch.setattr(unicam, "GetDeviceList", lambda systems, params: systems)
	monkeypatch.delitem(sys.modules, "campy.campy", raising=False)
	main = importlib.import_module("campy.campy")

	# The camera thread, to wait for it to end
	threads = []
	def thread(**kwargs):
		threads.append(threading.Thread(**kwargs))
		return threads[-1]
	main.threading = types.SimpleNamespace(Thread=thread)
	try:
		main.AcquireOneCamera(0)
	finally:
		sys.modules.pop("campy.campy", None)
	assert len(threads) == 1
	threads[0].join(5)
	assert not threads[0].is_alive()
	return (opened[0] if opened else None), sent


def without_the_wait(monkeypatch):
	# What campy did before: the writer opened its video as soon as the camera
	# thread started, without waiting for the camera to open
	write = writer.WriteFrames
	monkeypatch.setattr(writer, "WriteFrames",
		lambda cam_params, writeQueue, stopReadQueue, stopWriteQueue, openQueue=None, recording=None:
			write(cam_params, writeQueue, stopReadQueue, stopWriteQueue, recording=recording))


# ------------------------------------------------------------------ Basler

class Feature:
	def __init__(self, value):
		self.value = value

	def GetValue(self):
		return self.value

	def SetValue(self, value):
		self.value = value


class Grab:
	def __init__(self, width, height, number):
		self.Array = np.zeros((height, width), dtype=np.uint8)
		self.TimeStamp = number * 25_000_000 # ns, 40 fps

	def Release(self):
		pass


class FakeBasler:
	"""A Basler camera, 1152x1024 until a features file sets its size."""
	def __init__(self):
		self.Width, self.Height = Feature(1152), Feature(1024)
		self.grabbed = 0

	def Open(self):
		time.sleep(0.1) # it takes a moment

	def GetDeviceInfo(self):
		return self

	def GetModelName(self):
		return "acA1300-200um"

	def GetNodeMap(self):
		return self

	def LoadFeatures(self, path):
		# A .pfs file: a feature and its value on each line, with a tab between
		with open(path) as f:
			for line in f:
				name, _, value = line.rstrip("\n").partition("\t")
				if name in ("Width", "Height"):
					getattr(self, name).SetValue(int(value))

	def StartGrabbing(self, strategy):
		pass

	def RetrieveResult(self, timeout, handling):
		self.grabbed += 1
		return Grab(self.Width.GetValue(), self.Height.GetValue(), self.grabbed)

	def StopGrabbing(self):
		pass

	def Close(self):
		pass


class ImageWindow:
	# Pylon's own image window, which campy opens on Windows
	def Create(self, n):
		pass

	def Show(self):
		pass


def FakePypylon():
	pylon = types.ModuleType("pypylon.pylon")
	pylon.InstantCamera = lambda device: device
	pylon.FeaturePersistence = types.SimpleNamespace(
		Load=lambda path, nodemap, validate: nodemap.LoadFeatures(path))
	pylon.PylonImageWindow = ImageWindow
	pylon.GrabStrategy_OneByOne = "OneByOne"
	pylon.TimeoutHandling_ThrowException = "ThrowException"
	genicam = types.ModuleType("pypylon.genicam")
	package = types.ModuleType("pypylon")
	package.pylon, package.genicam = pylon, genicam
	return {"pypylon": package, "pypylon.pylon": pylon, "pypylon.genicam": genicam}


@pytest.fixture
def basler(monkeypatch):
	for name, module in FakePypylon().items():
		monkeypatch.setitem(sys.modules, name, module)
	monkeypatch.delitem(sys.modules, "campy.cameras.basler", raising=False)
	yield importlib.import_module("campy.cameras.basler")
	sys.modules.pop("campy.cameras.basler", None)


def settings_file(tmp_path, width, height):
	# configs/mono8.pfs with another frame size
	with open(os.path.join(REPO, "configs", "mono8.pfs")) as f:
		text = f.read()
	assert "\nWidth\t1152\n" in text and "\nHeight\t1024\n" in text
	path = tmp_path / "1280x1024.pfs"
	path.write_text(text.replace("\nWidth\t1152\n", "\nWidth\t{}\n".format(width))
		.replace("\nHeight\t1024\n", "\nHeight\t{}\n".format(height)))
	return str(path)


def test_a_basler_video_opens_at_the_size_its_settings_file_sets(basler, monkeypatch, tmp_path, capsys):
	camera = FakeBasler()
	opened, sent = record(monkeypatch, tmp_path, "basler", camera,
		cameraSettings=settings_file(tmp_path, 1280, 1024)) # frameWidth x frameHeight: 1152x1024, the default
	assert sent and set(sent) == {(1280, 1024)}
	assert opened == (1280, 1024)
	out = capsys.readouterr().out
	assert "Camera0: frameWidth x frameHeight 1152x1024 changed to 1280x1024, the camera's frame size with its " \
		"cameraSettings." in out
	assert out.index("changed to") < out.index("Opened: ") # before the video opens


def test_a_basler_with_the_configs_size_says_nothing(basler, monkeypatch, tmp_path, capsys):
	opened, sent = record(monkeypatch, tmp_path, "basler", FakeBasler(),
		cameraSettings=os.path.join(REPO, "configs", "mono8.pfs"))
	assert opened == (1152, 1024) and set(sent) == {(1152, 1024)}
	assert "changed to" not in capsys.readouterr().out



def test_a_cameras_live_feed_says_its_folder_in_the_recording(basler, monkeypatch, tmp_path):
	# campy-live-view puts its bookmarks beside the recording: it finds the recording from the cameras' feeds
	descriptions = []
	RingWriter = livefeed.RingWriter
	def Writer(*args, **kwargs):
		descriptions.append(args[7] if len(args) > 7 else kwargs.get("description", ""))
		return RingWriter(*args, **kwargs)
	monkeypatch.setattr(livefeed, "RingWriter", Writer)
	opened, sent = record(monkeypatch, tmp_path, "basler", FakeBasler(),
		cameraSettings=os.path.join(REPO, "configs", "mono8.pfs"), liveFeed=True,
		liveFeedName="cf{}".format(os.getpid() % 100000))
	assert sent and descriptions
	d = json.loads(descriptions[0])
	assert {k: d[k] for k in ("kind", "folder")} == {"kind": "campy camera", "folder": os.path.abspath(str(tmp_path / "Camera0"))}
	# and the processes that record it, for campy-live to put on the efficiency cores (campy/cpus.py)
	assert isinstance(d["pid"], int) and isinstance(d["parent_pid"], int)

def test_without_the_wait_a_basler_video_was_scrambled(basler, monkeypatch, tmp_path):
	# The writer opened its video at the config's 1152x1024 while the camera
	# thread was still loading the settings file, and took each 1280x1024 frame
	# as 1152x1024: scrambled
	without_the_wait(monkeypatch)
	opened, sent = record(monkeypatch, tmp_path, "basler", FakeBasler(),
		cameraSettings=settings_file(tmp_path, 1280, 1024))
	assert set(sent) == {(1280, 1024)}
	assert opened == (1152, 1024)


# ------------------------------------------------------------------ emu

def write_clip(path, size=(64, 48), frames=10):
	import imageio_ffmpeg
	w = imageio_ffmpeg.write_frames(path, size, fps=20, pix_fmt_in="rgb24", macro_block_size=16)
	w.send(None)
	for i in range(frames):
		w.send(np.full((size[1], size[0], 3), 8 * i, np.uint8))
	w.close()


def emu_clip(tmp_path):
	(tmp_path / "Camera0").mkdir()
	write_clip(str(tmp_path / "Camera0" / "clip.mp4")) # emu reads videoFilename minus its first 3 characters
	return dict(videoFilename="emuclip.mp4", pixelFormatInput="rgb24")


def test_an_emu_video_opens_at_the_clips_size(monkeypatch, tmp_path, capsys):
	# Like tests/test_trigger_start.py's: no frameWidth in the config, so 1152x1024
	opened, sent = record(monkeypatch, tmp_path, "emu", "emu0", **emu_clip(tmp_path))
	assert sent and set(sent) == {(64, 48)}
	assert opened == (64, 48)
	out = capsys.readouterr().out
	assert "Camera0: frameWidth x frameHeight 1152x1024 changed to 64x48, the clip's frame size." in out
	assert out.index("changed to") < out.index("Opened: ")


def test_without_the_wait_an_emu_video_was_empty(monkeypatch, tmp_path):
	# ffmpeg took the 64x48 frames as parts of one 1152x1024 frame, and a run
	# this short never filled one: the video had no frames at all
	without_the_wait(monkeypatch)
	opened, sent = record(monkeypatch, tmp_path, "emu", "emu0", **emu_clip(tmp_path))
	assert set(sent) == {(64, 48)}
	assert opened == (1152, 1024)


# ------------------------------------------------------------------ a camera that does not open

def test_a_camera_that_does_not_open(monkeypatch, tmp_path, caplog):
	# An emulated camera without its clip: its OpenCamera raises
	ready, died = queue.Queue(), []
	unicam.SetReadyQueue(ready)
	monkeypatch.setattr(threading, "excepthook", died.append)
	try:
		with caplog.at_level(logging.ERROR):
			opened, sent = record(monkeypatch, tmp_path, "emu", "emu0", videoFilename="emuclip.mp4")
	finally:
		unicam.SetReadyQueue(None)

	assert "Caught error at cameras/unicam.py OpenCamera" in caplog.text
	n_cam, name, ok, error = ready.get_nowait()
	assert (n_cam, name, ok) == (0, "Camera0", False) and error.startswith("failed to open: ")
	assert ready.empty() # and nothing after, such as a ready
	# The camera thread ends without a second error (it raised UnboundLocalError:
	# camera was never set), and no video is opened for a camera that has none
	assert died == []
	assert opened is None and sent == []


def test_open_camera_returns_no_camera_when_it_fails(monkeypatch):
	def fail(cam_params):
		raise RuntimeError("no such device")
	monkeypatch.setattr(unicam, "ImportCam", lambda make: types.SimpleNamespace(OpenCamera=fail))
	stopWriteQueue = deque([], 1)
	cam, camera, cam_params = unicam.OpenCamera({"n_cam": 0, "cameraName": "Camera0", "cameraMake": "x"},
		stopWriteQueue)
	assert camera is None and list(stopWriteQueue) == ["STOP"]
