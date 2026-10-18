"""A FLIR camera's frame size and centring, from whatever frame an earlier run left on it, against stand-ins for PySpin and the camera."""

import importlib
import logging
import sys
import types

import pytest


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
	for kind in ("CBooleanPtr", "CFloatPtr", "CIntegerPtr", "CEnumerationPtr", "CCommandPtr"):
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
