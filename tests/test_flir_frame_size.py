"""A FLIR camera's frame size and centring, from whatever frame an earlier run left on it, against stand-ins for PySpin and the camera."""

import importlib
import logging
import sys
import time
import types

import numpy as np
import pytest

from campy import configurator, preflight, writer
from campy.cameras import unicam
from campy.preflight import FAIL


class SpinError(Exception):
	pass


class Axis:
	"""
	One axis of a camera's frame, as the BFS-U3-13Y3M (1280x1024) behaved on
	the EVO-X2 on 2026-10-18 (PySpin 4.4.0.246): the size can go no further
	than the sensor's edge from the offset ("Value = 1280 must be equal or
	smaller than Max = 1120" at OffsetX 160), and the offset no further than
	the sensor's edge from the size. Sizes step by 16, offsets by 4.
	"""
	def __init__(self, sensor, size, offset):
		self.sensor, self.size, self.offset = sensor, size, offset
		self.size_writable = True


class Node:
	def __init__(self, get, set, lowest, highest, step, writable=lambda: True):
		self.get, self.set, self.lowest, self.highest, self.step, self.can_write = \
			get, set, lowest, highest, step, writable

	def writable(self):
		return self.can_write()

	def GetValue(self):
		return self.get()

	def SetValue(self, value):
		if not self.writable():
			raise SpinError("AccessException: node is not writable")
		if value > self.highest():
			raise SpinError("OutOfRangeException= Value = {} must be equal or smaller than Max = {}.".format(
				value, self.highest()))
		if value < self.lowest() or (value - self.lowest()) % self.step:
			raise SpinError("OutOfRangeException= Value = {}".format(value))
		self.set(value)

	def GetMin(self):
		return self.lowest()

	def GetMax(self):
		return self.highest()


class FakeCamera:
	def __init__(self, width=1280, height=1024, offset_x=0, offset_y=0):
		self.x, self.y = Axis(1280, width, offset_x), Axis(1024, height, offset_y)
		self.nodes = {}
		for size, offset, axis in (("Width", "OffsetX", self.x), ("Height", "OffsetY", self.y)):
			self.nodes[size] = Node(lambda a=axis: a.size, lambda v, a=axis: setattr(a, "size", v),
				lambda: 16, lambda a=axis: a.sensor - a.offset, 16, writable=lambda a=axis: a.size_writable)
			self.nodes[offset] = Node(lambda a=axis: a.offset, lambda v, a=axis: setattr(a, "offset", v),
				lambda: 0, lambda a=axis: a.sensor - a.size, 4)
			self.nodes[size + "Max"] = Node(lambda a=axis: a.sensor, None, lambda: 0, lambda: 0, 1,
				writable=lambda: False)
		self.calls = []

	def frame(self):
		return self.x.size, self.y.size, self.x.offset, self.y.offset

	def GetNodeMap(self):
		return self

	def GetNode(self, name):
		return self.nodes.get(name)

	def Init(self):
		self.calls.append("Init")

	def DeInit(self):
		self.calls.append("DeInit")


def FakePySpin():
	spin = types.ModuleType("PySpin")
	for kind in ("CBooleanPtr", "CFloatPtr", "CIntegerPtr", "CEnumerationPtr", "CCommandPtr", "CStringPtr"):
		setattr(spin, kind, lambda node: node)
	spin.IsAvailable = lambda node: node is not None
	spin.IsReadable = lambda node: node is not None
	spin.IsWritable = lambda node: node is not None and node.writable()
	spin.SpinnakerException = SpinError
	return spin


@pytest.fixture
def flir(monkeypatch, tmp_path):
	monkeypatch.setitem(sys.modules, "PySpin", FakePySpin())
	monkeypatch.delitem(sys.modules, "campy.cameras.flir", raising=False)
	module = importlib.import_module("campy.cameras.flir")
	# Only the frame size is set here: LoadSettings' other steps drive QuickSpin
	# nodes the stand-in does not have
	def image_settings(camera, cam_params):
		return module.ConfigureFrameHeight(camera, module.ConfigureFrameWidth(camera, cam_params))

	monkeypatch.setattr(module, "ConfigureTrigger", lambda camera, cam_params: cam_params)
	monkeypatch.setattr(module, "ConfigureCustomImageSettings", image_settings)
	monkeypatch.setattr(module, "ConfigureFrameRate", lambda camera, cam_params: cam_params)
	monkeypatch.setattr(module, "NOTE_FOLDER", str(tmp_path / ".campy"))
	yield module
	sys.modules.pop("campy.cameras.flir", None)


def params(width=1280, height=1024, **values):
	cam = {"cameraName": "Camera0", "cameraSerialNo": "24243505", "cameraTrigger": "None", "frameRate": 80,
		"frameWidth": width, "frameHeight": height, "pixelFormatInput": "gray", "cameraDebug": False}
	cam.update(values)
	return cam


# What a 640x512 run left on the EVO-X2's camera: its launch check centred the
# frame at 320, 256, then the camera process's own open, reading the maximum
# from those offsets, moved it to 160, 128
LEFT_BY_A_SMALLER_RUN = dict(width=640, height=512, offset_x=160, offset_y=128)


def test_full_frame_after_a_smaller_one(flir, capsys, caplog):
	camera = FakeCamera(**LEFT_BY_A_SMALLER_RUN)
	with caplog.at_level(logging.ERROR):
		flir.LoadSettings(camera, params())
	assert camera.frame() == (1280, 1024, 0, 0)
	assert caplog.text == ""
	out = capsys.readouterr().out
	assert "Width set to 1280" in out and "Height set to 1024" in out and "scrambled" not in out


@pytest.mark.parametrize("left", [{}, LEFT_BY_A_SMALLER_RUN, dict(width=1024, height=768, offset_x=256, offset_y=0)])
@pytest.mark.parametrize("asked, frame", [
	((640, 512), (640, 512, 320, 256)),
	((800, 600), (800, 608, 240, 208)), # height rounded up to 16
	((1280, 1024), (1280, 1024, 0, 0)),
	])
def test_a_frame_is_centred_whatever_was_left(flir, left, asked, frame):
	camera = FakeCamera(**left)
	cam = flir.LoadSettings(camera, params(*asked))
	assert camera.frame() == frame
	assert (cam["frameWidth"], cam["frameHeight"]) == frame[:2]


def test_the_launch_check_and_the_camera_process_centre_it_alike(flir):
	# Each opens the camera and sets it up, one after the other
	camera = FakeCamera()
	flir.FrameRateLimits(camera, params(640, 512))
	assert camera.frame() == (640, 512, 320, 256)
	flir.LoadSettings(camera, params(640, 512))
	assert camera.frame() == (640, 512, 320, 256)


def test_without_it_the_frame_could_not_grow_back(flir, monkeypatch, caplog):
	# What campy did before, on the stand-in: the rig's 1120 and 896, and the second open's 160, 128
	monkeypatch.setattr(flir, "ZeroOffset", lambda nodemap, offset, size: None)
	camera = FakeCamera()
	flir.FrameRateLimits(camera, params(640, 512))
	flir.LoadSettings(camera, params(640, 512))
	assert camera.frame() == (640, 512, 160, 128)
	with caplog.at_level(logging.ERROR):
		flir.LoadSettings(camera, params())
	assert camera.frame()[:2] == (640, 512)
	assert "Max = 1120" in caplog.text and "Max = 896" in caplog.text


def test_a_size_the_camera_refuses_is_said(flir, capsys):
	# The writer takes its frames as frameWidth x frameHeight: the video would be scrambled
	camera = FakeCamera(**LEFT_BY_A_SMALLER_RUN)
	camera.x.size_writable = camera.y.size_writable = False
	flir.LoadSettings(camera, params())
	assert "Camera0: its frames are 640x512, not frameWidth x frameHeight 1280x1024, so its video will be scrambled." \
		in capsys.readouterr().out
	# and the frame it has is not moved off its centre
	assert camera.frame() == (640, 512, 160, 128)


def test_a_camera_without_frame_size_nodes(flir, capsys):
	camera = FakeCamera()
	camera.nodes.clear()
	assert flir.CheckFrameSize(camera, params())["frameWidth"] == 1280
	flir.ZeroOffset(camera, "OffsetX", "Width")
	assert "scrambled" not in capsys.readouterr().out


# ------------------------------------------------------------------ the launch check

def test_the_launch_check_reads_the_frame_size(flir):
	camera = FakeCamera(**LEFT_BY_A_SMALLER_RUN)
	limits = flir.FrameRateLimits(camera, params(1000, 600))
	assert limits["frameSize"] == limits["wantedFrameSize"] == (1008, 608) # rounded up to 16, as campy does
	assert limits["maxFrameSize"] == (1280, 1024) and limits["frameSizeLocked"] is False
	assert preflight.FrameSizeFinding(params(1000, 600), limits) is None


def test_the_launch_check_stops_a_camera_it_cannot_resize(flir):
	camera = FakeCamera(**LEFT_BY_A_SMALLER_RUN)
	camera.x.size_writable = camera.y.size_writable = False
	limits = flir.FrameRateLimits(camera, params())
	assert preflight.FrameSizeFinding(params(), limits) == (FAIL,
		"Camera0 (24243505) would send 640x512 frames, not frameWidth x frameHeight 1280x1024, so its video would be "
		"scrambled. Either:\n"
		"  - unplug it and plug it back in: its frame size cannot be changed now\n"
		"  - set frameWidth: 640 and frameHeight: 512, the frames it sends now")


def test_the_launch_check_stops_a_frame_larger_than_the_sensor(flir):
	camera = FakeCamera()
	limits = flir.FrameRateLimits(camera, params(1440, 1080))
	status, text = preflight.FrameSizeFinding(params(1440, 1080), limits)
	assert status == FAIL and "not frameWidth x frameHeight 1440x1088" in text
	assert text.endswith("Either:\n  - set frameWidth and frameHeight to 1280x1024 (its full frame) or less")


# ------------------------------------------------------------------ the writer

class Frame:
	# A grabbed frame of the camera's size at the time
	def __init__(self, width, height, number):
		self.width, self.height, self.number = width, height, number

	def IsIncomplete(self):
		return False

	def GetNDArray(self):
		return np.zeros((self.height, self.width), dtype=np.uint8)

	def GetChunkData(self):
		return self

	def GetTimestamp(self):
		return self.number * 12_500_000 # ns, 80 fps

	def Release(self):
		pass


class GrabbingCamera(FakeCamera):
	# camera.Init takes 0.1-0.2 s on a Blackfly S: the camera thread sets the
	# frame size that much after campy starts it
	def Init(self):
		time.sleep(0.15)
		super().Init()

	def GetTLDeviceNodeMap(self):
		return self

	def BeginAcquisition(self):
		self.grabbed = 0

	def GetNextImage(self, timeout):
		self.grabbed += 1
		return Frame(self.x.size, self.y.size, self.grabbed)

	def EndAcquisition(self):
		pass


def record(flir, monkeypatch, tmp_path, camera, **values):
	"""
	Runs campy.AcquireOneCamera on camera, as a camera process does: it starts
	the camera thread, then opens the writer. The writer is imageio-ffmpeg's
	write_frames stood in for: returns the size the video was opened at, and
	the size (width, height) of each frame it was sent.
	"""
	p = configurator.FillCameraLists(configurator.DefaultParams())
	p.update(cameraMake="flir", videoFolder=str(tmp_path), frameRate=80, recTimeInSec=0.1, chunkLengthInSec=1,
		displayFrameRate=0, pixelFormatInput="gray", preflight="off")
	p.update(values)
	systems = {"flir": {"system": None, "deviceList": [camera], "serials": ["24243505"]}}

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
	try:
		main.AcquireOneCamera(0)
	finally:
		sys.modules.pop("campy.campy", None)
	return opened[0], sent


def test_the_writer_opens_its_video_at_the_size_the_camera_sends(flir, monkeypatch, tmp_path, capsys):
	camera = GrabbingCamera()
	opened, sent = record(flir, monkeypatch, tmp_path, camera, frameWidth=1000, frameHeight=600)
	assert camera.frame() == (1008, 608, 136, 208)
	assert sent and set(sent) == {(1008, 608)}
	assert opened == (1008, 608)
	out = capsys.readouterr().out
	assert "Camera0: frameWidth 1000 rounded up to 1008: campy sets FLIR frame sizes in steps of 16." in out
	assert "Camera0: frameHeight 600 rounded up to 608: campy sets FLIR frame sizes in steps of 16." in out
	assert out.index("rounded up") < out.index("Opened: ") # before the video opens


def test_rounded_only_in_the_camera_thread_the_video_was_scrambled(flir, monkeypatch, tmp_path):
	# What campy did before: the writer opened its video as soon as the camera
	# thread started, before that thread had rounded the size, so 1008x608
	# frames went into a video of 1000x600 (ffmpeg then cuts them into more
	# frames than were recorded, each one scrambled)
	monkeypatch.setattr(flir, "FixedParams", lambda cam_params: cam_params)
	camera = GrabbingCamera()
	opened, sent = record(flir, monkeypatch, tmp_path, camera, frameWidth=1000, frameHeight=600)
	assert set(sent) == {(1008, 608)}
	assert opened == (1000, 600)


def test_a_size_in_steps_of_16_is_left_alone(flir, monkeypatch, tmp_path, capsys):
	camera = GrabbingCamera(**LEFT_BY_A_SMALLER_RUN)
	opened, sent = record(flir, monkeypatch, tmp_path, camera, frameWidth=1280, frameHeight=1024)
	assert opened == (1280, 1024) and set(sent) == {(1280, 1024)}
	assert "rounded up" not in capsys.readouterr().out


@pytest.mark.parametrize("asked, fixed", [
	((1000, 600), (1008, 608)),
	((1280, 1024), (1280, 1024)),
	((1, 1023), (16, 1024)),
	((640, 512), (640, 512)),
	])
def test_fixed_params(flir, asked, fixed):
	cam = flir.FixedParams(params(*asked))
	assert (cam["frameWidth"], cam["frameHeight"]) == fixed
	# and the camera thread sets that size, unchanged
	camera = FakeCamera()
	flir.LoadSettings(camera, dict(cam))
	assert camera.frame()[:2] == fixed


def test_the_launch_check_and_the_recording_want_the_same_size(flir):
	# The launch check opens the camera with the config's size, not FixedParams':
	# its copy is rounded as the camera is set up
	limits = flir.FrameRateLimits(FakeCamera(), params(1000, 600))
	assert limits["wantedFrameSize"] == (1008, 608)
	cam = flir.FixedParams(params(1000, 600))
	assert (cam["frameWidth"], cam["frameHeight"]) == limits["wantedFrameSize"]
