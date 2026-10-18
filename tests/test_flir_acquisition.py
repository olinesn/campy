"""A FLIR camera left acquiring by a run that never closed it, against stand-ins for PySpin and the camera."""

import importlib
import logging
import sys
import types

import pytest


class SpinError(Exception):
	pass


class Device:
	"""
	The camera itself: what it keeps from one process to the next, until it
	is unplugged. As the BFS-U3-13Y3M on the EVO-X2 behaved on 2026-10-18
	(PySpin 4.4.0.246), after campy's camera process was killed mid-recording:
	it goes on acquiring, and its frame size and pixel format stay read only
	to a new process, although that process reads TLParamsLocked 0 (and
	setting it changes nothing); the camera's AcquisitionStop, or Spinnaker's
	BeginAcquisition and EndAcquisition, from the new process end it. stop is
	what its AcquisitionStop does: "stop" (as measured), "ignore" (nothing),
	or None (no such command).
	"""
	def __init__(self, stop="stop"):
		self.stop = stop
		self.acquiring, self.locked = False, 0
		self.width, self.height, self.offset_x, self.offset_y = 1280, 1024, 0, 0
		self.pixel_format = "Mono8"

	def format_writable(self):
		return not self.acquiring


class Node:
	def __init__(self, get, set=None, writable=lambda: True, maximum=None):
		self.get, self.set, self.can_write, self.maximum = get, set, writable, maximum

	def writable(self):
		return self.set is not None and self.can_write()

	def GetValue(self):
		return self.get()

	def SetValue(self, value):
		if not self.writable():
			raise SpinError("AccessException: node is not writable")
		self.set(value)

	def GetMax(self):
		return self.maximum


class Command:
	def __init__(self, execute):
		self.execute = execute

	def writable(self):
		return True

	def Execute(self):
		self.execute()


class FakeCamera:
	"""One process's handle on a Device, as PySpin's CameraPtr: a new process starts with started False."""

	def __init__(self, device):
		self.device = device
		self.started = False
		self.calls = []
		d = device
		self.nodes = {
			"TLParamsLocked": Node(lambda: d.locked, lambda v: setattr(d, "locked", v)),
			"Width": Node(lambda: d.width, lambda v: setattr(d, "width", v), d.format_writable, 1280),
			"Height": Node(lambda: d.height, lambda v: setattr(d, "height", v), d.format_writable, 1024),
			"OffsetX": Node(lambda: d.offset_x, lambda v: setattr(d, "offset_x", v), d.format_writable, 1280),
			"OffsetY": Node(lambda: d.offset_y, lambda v: setattr(d, "offset_y", v), d.format_writable, 1024),
			"PixelFormat": Node(lambda: d.pixel_format, lambda v: setattr(d, "pixel_format", v), d.format_writable),
			"AcquisitionStop": Command(self.acquisition_stop),
			}
		if device.stop is None:
			del self.nodes["AcquisitionStop"]
		self.PixelFormat = self.nodes["PixelFormat"] # QuickSpin, as ConfigurePixelFormat uses it

	def acquisition_stop(self):
		self.calls.append("AcquisitionStop")
		if self.device.stop == "stop":
			self.device.acquiring = False

	def GetNodeMap(self):
		return self

	def GetNode(self, name):
		return self.nodes.get(name)

	def Init(self):
		# A new process reads TLParamsLocked 0, whether or not the camera still acquires
		self.calls.append("Init")
		self.device.locked = 0

	def DeInit(self):
		self.calls.append("DeInit")

	def BeginAcquisition(self):
		# Spinnaker locks the transport layer parameters, then starts the camera
		self.calls.append("BeginAcquisition")
		if self.started:
			raise SpinError("Camera is already streaming")
		self.device.locked, self.device.acquiring, self.started = 1, True, True

	def EndAcquisition(self):
		self.calls.append("EndAcquisition")
		if not self.started:
			raise SpinError("Camera is not started")
		self.device.acquiring, self.device.locked, self.started = False, 0, False


def FakePySpin():
	spin = types.ModuleType("PySpin")
	for kind in ("CBooleanPtr", "CFloatPtr", "CIntegerPtr", "CEnumerationPtr", "CCommandPtr"):
		setattr(spin, kind, lambda node: node)
	spin.IsAvailable = lambda node: node is not None
	spin.IsReadable = lambda node: node is not None
	spin.IsWritable = lambda node: node is not None and node.writable()
	spin.SpinnakerException = SpinError
	spin.PixelFormat_Mono8, spin.PixelFormat_RGB8Packed = "Mono8", "RGB8Packed"
	return spin


@pytest.fixture
def flir(monkeypatch, tmp_path):
	monkeypatch.setitem(sys.modules, "PySpin", FakePySpin())
	monkeypatch.delitem(sys.modules, "campy.cameras.flir", raising=False)
	module = importlib.import_module("campy.cameras.flir")
	# LoadSettings' other steps drive the camera through QuickSpin nodes the
	# stand-in does not have: here only the frame size and pixel format are set
	def image_settings(camera, cam_params):
		for configure in (module.ConfigureFrameWidth, module.ConfigureFrameHeight, module.ConfigurePixelFormat):
			cam_params = configure(camera, cam_params)
		return cam_params

	monkeypatch.setattr(module, "ConfigureTrigger", lambda camera, cam_params: cam_params)
	monkeypatch.setattr(module, "ConfigureCustomImageSettings", image_settings)
	monkeypatch.setattr(module, "ConfigureFrameRate", lambda camera, cam_params: cam_params)
	monkeypatch.setattr(module, "NOTE_FOLDER", str(tmp_path / ".campy"))
	yield module
	sys.modules.pop("campy.cameras.flir", None)


def params(**values):
	cam = {"cameraName": "Camera0", "cameraSerialNo": "24243505", "cameraTrigger": "None", "frameRate": 80,
		"frameWidth": 1280, "frameHeight": 1024, "pixelFormatInput": "gray", "cameraDebug": False}
	cam.update(values)
	return cam


STOPPED = "Camera0: still acquiring from a run that did not close it. Stopped it, so its frame size and pixel format " \
	"can be set."


def killed_run(flir, device, **values):
	# A recording whose camera process dies before CloseCamera (kill -9, a crash)
	camera = FakeCamera(device)
	camera.Init()
	flir.LoadSettings(camera, params(**values))
	assert flir.StartGrabbing(camera)
	assert device.acquiring and device.locked


def new_process(device):
	# The next run's handle on the camera, opened (Init) as OpenCamera does
	camera = FakeCamera(device)
	camera.Init()
	camera.calls.clear()
	return camera


# What a new process's AcquisitionStop does to a camera left acquiring
DEVICES = {
	"stops it": dict(stop="stop"), # a Blackfly S, as measured
	"ignored": dict(stop="ignore"), # Spinnaker's own start and stop do it
	"no AcquisitionStop": dict(stop=None),
	}


@pytest.fixture(params=list(DEVICES))
def device(request):
	return Device(**DEVICES[request.param])


def test_the_next_run_sets_its_own_frame_size_and_pixel_format(flir, device, capsys, caplog):
	killed_run(flir, device)
	capsys.readouterr()
	camera = new_process(device)
	# As on the rig: TLParamsLocked says nothing is wrong
	assert camera.GetNode("TLParamsLocked").GetValue() == 0 and device.acquiring
	cam = flir.LoadSettings(camera, params(frameWidth=640, frameHeight=512, pixelFormatInput="rgb24"))
	out = capsys.readouterr().out
	assert out.startswith(STOPPED + "\n")
	assert "Width set to 640" in out and "Height set to 512" in out
	assert (device.width, device.height, device.pixel_format) == (640, 512, "RGB8Packed")
	assert (device.offset_x, device.offset_y) == (cam["offsetX"], cam["offsetY"]) == (320, 256)
	assert not device.acquiring and not device.locked
	assert "not writable" not in caplog.text
	# and it records, and closes the camera
	assert flir.StartGrabbing(camera)
	flir.CloseCamera(cam, camera)
	assert not device.acquiring and not device.locked


def test_without_it_the_killed_run_s_settings_stay(flir, device, monkeypatch, caplog):
	# What campy did before: only a logged error says so
	monkeypatch.setattr(flir, "RecoverAcquisition", lambda camera, cam_params: None)
	killed_run(flir, device)
	with caplog.at_level(logging.ERROR):
		flir.LoadSettings(new_process(device), params(frameWidth=640, frameHeight=512, pixelFormatInput="rgb24"))
	assert (device.width, device.height, device.pixel_format) == (1280, 1024, "Mono8")
	assert "Error setting pixel format to rgb24" in caplog.text


def test_the_camera_s_own_stop_first(flir):
	# Spinnaker's start and stop only when the camera's own stop does not do it
	device = Device(stop="stop")
	killed_run(flir, device)
	camera = new_process(device)
	flir.LoadSettings(camera, params())
	assert camera.calls == ["AcquisitionStop"]


def test_a_stop_that_fails_falls_back(flir, capsys):
	device = Device(stop="stop")
	killed_run(flir, device)
	capsys.readouterr()
	camera = new_process(device)

	def refuse():
		raise SpinError("Spinnaker: Error writing to Device")

	camera.nodes["AcquisitionStop"].execute = refuse
	flir.LoadSettings(camera, params(frameWidth=640))
	assert camera.calls == ["BeginAcquisition", "EndAcquisition"]
	assert capsys.readouterr().out.startswith(STOPPED + "\n") and device.width == 640


def test_the_launch_check_stops_it_and_says_so(flir, device, capsys):
	# It opens the cameras before their own processes do, and hides the
	# Configure* progress lines; this one is not hidden
	killed_run(flir, device)
	capsys.readouterr()
	camera = FakeCamera(device)
	flir.FrameRateLimits(camera, params(frameWidth=640, frameHeight=512))
	assert capsys.readouterr().out == STOPPED + "\n"
	assert camera.calls[0] == "Init" and camera.calls[-1] == "DeInit"
	assert not device.acquiring and not device.locked and device.width == 640
	# and the camera's own process finds nothing left to stop
	camera = new_process(device)
	flir.LoadSettings(camera, params())
	assert "Stopped it" not in capsys.readouterr().out and camera.calls == []


@pytest.mark.parametrize("trigger", ["None", "Line3"])
def test_a_camera_closed_cleanly_is_left_alone(flir, trigger, capsys):
	device = Device()
	camera = FakeCamera(device)
	cam = flir.LoadSettings(camera, params(cameraTrigger=trigger))
	assert flir.StartGrabbing(camera)
	flir.CloseCamera(cam, camera)
	capsys.readouterr()
	camera = new_process(device)
	flir.LoadSettings(camera, params(cameraTrigger=trigger, frameWidth=640))
	assert camera.calls == [] and device.width == 640
	assert "Stopped it" not in capsys.readouterr().out


def test_a_camera_without_these_nodes_is_left_alone(flir, capsys):
	# Older models, other makes' stand-ins: nothing to read, nothing said
	camera = FakeCamera(Device())
	for name in ("TLParamsLocked", "Width", "Height", "PixelFormat", "AcquisitionStop"):
		del camera.nodes[name]
	flir.RecoverAcquisition(camera, params())
	assert camera.calls == [] and capsys.readouterr().out == ""


def test_a_camera_that_cannot_be_stopped(flir, monkeypatch, capsys):
	# The recording goes ahead, and says what it risks
	device = Device(stop="ignore")
	killed_run(flir, device)
	capsys.readouterr()
	camera = new_process(device)

	def refuse():
		raise SpinError("Camera is already streaming")

	camera.BeginAcquisition = refuse
	flir.LoadSettings(camera, params(frameWidth=640))
	assert capsys.readouterr().out.startswith(
		"Camera0: its frame size and pixel format cannot be set, even after stopping its acquisition, so it keeps "
		"the ones it has. Unplug it and plug it back in.\n")
	assert device.width == 1280


def test_a_killed_triggered_run(flir, device, capsys):
	# A triggered camera is locked the same way
	killed_run(flir, device, cameraTrigger="Line3")
	capsys.readouterr()
	flir.LoadSettings(new_process(device), params(cameraTrigger="Line3", pixelFormatInput="rgb24"))
	assert STOPPED in capsys.readouterr().out
	assert device.pixel_format == "RGB8Packed"
