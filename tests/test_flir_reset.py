"""campy-flir-reset, against stand-ins for PySpin, its camera list and the cameras."""

import importlib
import sys
import types

import pytest

SAVED = 380000000 # the BFS-U3-13Y3M's saved limit, bytes/s
USB2 = 43000000   # what its firmware sets on a USB 2 link


class SpinError(Exception):
	pass


class Value:
	# A node with a value; GetCurrentEntry().GetSymbolic() for enumerations
	def __init__(self, get):
		self.get = get

	def GetValue(self):
		return self.get()

	def GetCurrentEntry(self):
		return self

	def GetSymbolic(self):
		return self.get()


class NodeMap(dict):
	def GetNode(self, name):
		return self.get(name)


class Command:
	def __init__(self, run):
		self.Execute = run


class FakeCamera:
	"""
	A FLIR USB3 camera as the BFS-U3-13Y3M on the EVO-X2 behaved on
	2026-10-16: DeviceReset makes it leave the camera list and come back a few
	seconds later on USB 3 with its saved limit. back=False: it never comes
	back; back_speed: the link it comes back on.
	"""
	def __init__(self, serial="24243505", speed="SuperSpeed", limit=USB2, saved=SAVED, has_limit=True,
			back=True, back_speed="SuperSpeed", away=3):
		self.serial, self.speed, self.limit, self.saved, self.has_limit = serial, speed, limit, saved, has_limit
		self.back, self.back_speed, self.away = back, back_speed, away
		self.resets, self.inits, self.open = 0, 0, False
		self.system = None

	def GetTLDeviceNodeMap(self):
		return NodeMap({"DeviceSerialNumber": Value(lambda: self.serial), "DeviceCurrentSpeed": Value(lambda: self.speed)})

	def GetNodeMap(self):
		assert self.open, "nodemap read before Init"
		nodes = NodeMap({"DeviceReset": Command(self.reset)})
		if self.has_limit:
			nodes["DeviceLinkThroughputLimit"] = Value(lambda: self.limit)
		return nodes

	def Init(self):
		self.inits += 1
		self.open = True

	def DeInit(self):
		if self not in self.system.present:
			raise SpinError("Spinnaker: camera has been removed")
		self.open = False

	def IsValid(self):
		return True

	def reset(self):
		self.resets += 1
		self.system.leave(self)

	def restart(self):
		# Back from a reset: its saved settings, on the link it found
		self.limit, self.speed, self.open = self.saved, self.back_speed, False


class Missing:
	def IsValid(self):
		return False


class CameraList:
	def __init__(self, cams):
		self.cams = list(cams)

	def GetSize(self):
		return len(self.cams)

	def __getitem__(self, i):
		return self.cams[i]

	def GetBySerial(self, serial):
		return next((c for c in self.cams if c.serial == serial), Missing())

	def Clear(self):
		self.cams = []


class FakeSystem:
	"""Spinnaker's camera list. hidden: UpdateCameras calls before the cameras show up (the computer starting)."""
	def __init__(self, cams, hidden=0):
		self.cams, self.hidden, self.updates = cams, hidden, 0
		self.present = list(cams) if hidden == 0 else []
		self.gone = {} # camera: UpdateCameras calls until it is back
		for cam in cams:
			cam.system = self

	def leave(self, cam):
		self.present.remove(cam)
		self.gone[cam] = cam.away

	def UpdateCameras(self):
		self.updates += 1
		if self.updates > self.hidden and not self.present and not self.gone:
			self.present = list(self.cams)
		for cam in list(self.gone):
			self.gone[cam] -= 1
			if self.gone[cam] <= 0 and cam.back:
				del self.gone[cam]
				cam.restart()
				self.present.append(cam)
		return True

	def GetCameras(self):
		return CameraList(self.present)


class Clock:
	# time.time and time.sleep: sleeping moves the clock, at once
	def __init__(self):
		self.now = 1000.0

	def time(self):
		return self.now

	def sleep(self, s):
		self.now += s


def FakePySpin():
	spin = types.ModuleType("PySpin")
	for kind in ("CStringPtr", "CIntegerPtr", "CEnumerationPtr", "CCommandPtr"):
		setattr(spin, kind, lambda node: node)
	spin.IsAvailable = lambda node: node is not None
	spin.IsReadable = lambda node: node is not None
	spin.SpinnakerException = SpinError
	return spin


@pytest.fixture
def flirreset(monkeypatch):
	monkeypatch.setitem(sys.modules, "PySpin", FakePySpin())
	monkeypatch.delitem(sys.modules, "campy.flirreset", raising=False)
	yield importlib.import_module("campy.flirreset")
	sys.modules.pop("campy.flirreset", None)


@pytest.fixture
def reset(flirreset, monkeypatch):
	monkeypatch.setattr(flirreset, "time", Clock())
	# no FLIR devices connected as far as the kernel says (a test run on a rig has some)
	monkeypatch.setattr(flirreset, "ConnectedFlirDevices", lambda: 0)
	return flirreset


def test_stuck_camera_is_reset(reset, capsys):
	cam = FakeCamera()
	assert reset.Run(FakeSystem([cam])) == 0
	out = capsys.readouterr().out
	assert cam.resets == 1 and cam.limit == SAVED
	assert "24243505: SuperSpeed link, but throughput limit 43 MB/s, as on USB 2: resetting it." in out
	assert "24243505: back on a SuperSpeed link, throughput limit 380 MB/s." in out


def test_camera_with_its_limit_is_left_alone(reset, capsys):
	cam = FakeCamera(limit=SAVED)
	assert reset.Run(FakeSystem([cam])) == 0
	assert cam.resets == 0 and not cam.open
	assert "24243505: SuperSpeed link, throughput limit 380 MB/s." in capsys.readouterr().out


def test_usb2_camera_is_left_alone(reset, capsys):
	# On a USB 2 link, 43 MB/s is right: the camera is not even opened
	cam = FakeCamera(speed="HighSpeed")
	assert reset.Run(FakeSystem([cam])) == 0
	assert cam.resets == 0 and cam.inits == 0
	assert "24243505: HighSpeed link, left alone." in capsys.readouterr().out


def test_model_without_the_setting_is_left_alone(reset, capsys):
	cam = FakeCamera(has_limit=False)
	assert reset.Run(FakeSystem([cam])) == 0
	assert cam.resets == 0
	assert "SuperSpeed link, left alone." in capsys.readouterr().out


def test_low_saved_limit_comes_back(reset, capsys):
	# A limit saved that low is reset for nothing, and is not a failure
	cam = FakeCamera(limit=50000000, saved=50000000)
	assert reset.Run(FakeSystem([cam])) == 0
	assert cam.resets == 1
	assert "throughput limit 50 MB/s: the limit it saved." in capsys.readouterr().out


def test_only_the_stuck_camera_is_reset(reset, capsys):
	stuck, fine = FakeCamera(serial="1"), FakeCamera(serial="2", limit=SAVED)
	assert reset.Run(FakeSystem([stuck, fine])) == 0
	assert (stuck.resets, fine.resets) == (1, 0)
	assert stuck.limit == SAVED


def test_camera_not_back(reset, capsys):
	cam = FakeCamera(back=False)
	assert reset.Run(FakeSystem([cam])) == 1
	assert "24243505: not back 30 s after its reset." in capsys.readouterr().out


def test_camera_back_on_usb2(reset, capsys):
	cam = FakeCamera(back_speed="HighSpeed")
	assert reset.Run(FakeSystem([cam])) == 1
	assert "24243505: back on a HighSpeed link." in capsys.readouterr().out


def test_waits_for_cameras_at_startup(reset, monkeypatch, capsys):
	# The kernel has connected a FLIR camera that Spinnaker does not list yet
	monkeypatch.setattr(reset, "ConnectedFlirDevices", lambda: 1)
	cam = FakeCamera()
	assert reset.Run(FakeSystem([cam], hidden=3)) == 0
	assert cam.resets == 1


def test_says_when_spinnaker_sees_fewer(reset, monkeypatch, capsys):
	# Two FLIR devices connected, Spinnaker lists one (e.g. no udev rule for the other)
	monkeypatch.setattr(reset, "ConnectedFlirDevices", lambda: 2)
	assert reset.Run(FakeSystem([FakeCamera(limit=SAVED)])) == 0
	assert "2 FLIR USB device(s) connected, but Spinnaker sees 1" in capsys.readouterr().out


def test_no_cameras(reset, capsys):
	assert reset.Run(FakeSystem([])) == 0
	assert "No FLIR cameras." in capsys.readouterr().out


def test_connected_flir_devices(flirreset, tmp_path):
	for name, vendor in [("2-1", "1e10"), ("3-1", "1724"), ("3-4", "046d"), ("usb2", "1d6b")]:
		(tmp_path / name).mkdir()
		(tmp_path / name / "idVendor").write_text(vendor + "\n")
	(tmp_path / "2-0:1.0").mkdir() # an interface: no idVendor
	assert flirreset.ConnectedFlirDevices(str(tmp_path)) == 2
	assert flirreset.ConnectedFlirDevices(str(tmp_path / "nowhere")) == 0
