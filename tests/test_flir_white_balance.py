"""FLIR white balance: turned off on a colour camera, skipped without a message on a mono one."""

import importlib
import sys
import types

import pytest


class SpinError(Exception):
	pass


class WhiteBalance:
	# QuickSpin's camera.BalanceWhiteAuto, and its node in the node map. On a
	# mono Blackfly S (BFS-U3-13Y3M, PySpin 4.4.0.246, 2026-10-16) the node is
	# implemented but not available, and SetValue raises "AccessException=
	# Node is not writable"
	def __init__(self, available, writable=True, value="Continuous"):
		self.available, self.writable, self.value = available, writable, value

	def SetValue(self, value):
		if not (self.available and self.writable):
			raise SpinError("Spinnaker: GenICam::AccessException= Node is not writable. [-2006]")
		self.value = value


class Setting:
	# Any other QuickSpin setting ConfigureCustomImageSettings writes
	def SetValue(self, value):
		self.value = value


class FakeCamera:
	def __init__(self, mono=True, writable=True):
		self.BalanceWhiteAuto = WhiteBalance(available=not mono, writable=writable)
		self.AcquisitionMode = Setting()

	def GetNodeMap(self):
		return self

	def GetNode(self, name):
		return self.BalanceWhiteAuto if name == "BalanceWhiteAuto" else None


def FakePySpin():
	spin = types.ModuleType("PySpin")
	for kind in ("CBooleanPtr", "CFloatPtr", "CIntegerPtr", "CEnumerationPtr"):
		setattr(spin, kind, lambda node: node)
	spin.IsAvailable = lambda node: node is not None and node.available
	spin.IsReadable = lambda node: node is not None and node.available
	spin.SpinnakerException = SpinError
	spin.BalanceWhiteAuto_Off = "Off"
	spin.AcquisitionMode_Continuous = "Continuous"
	return spin


@pytest.fixture
def flir(monkeypatch):
	monkeypatch.setitem(sys.modules, "PySpin", FakePySpin())
	monkeypatch.delitem(sys.modules, "campy.cameras.flir", raising=False)
	module = importlib.import_module("campy.cameras.flir")
	yield module
	sys.modules.pop("campy.cameras.flir", None)


def params():
	return {"cameraName": "Camera0", "cameraDebug": False}


def test_mono_camera_is_skipped_silently(flir, capsys):
	camera = FakeCamera(mono=True)
	flir.ConfigureWhiteBalance(camera, params())
	assert camera.BalanceWhiteAuto.value == "Continuous"
	assert capsys.readouterr().out == ""


def test_colour_camera_turns_auto_white_balance_off(flir, capsys):
	camera = FakeCamera(mono=False)
	flir.ConfigureWhiteBalance(camera, params())
	assert camera.BalanceWhiteAuto.value == "Off"
	assert capsys.readouterr().out == ""


def test_colour_camera_that_refuses_says_so(flir, capsys):
	camera = FakeCamera(mono=False, writable=False)
	flir.ConfigureWhiteBalance(camera, params())
	assert camera.BalanceWhiteAuto.value == "Continuous"
	assert capsys.readouterr().out.startswith("Camera0: could not turn off automatic white balance: "
		"Spinnaker: GenICam::AccessException= Node is not writable.")


def test_image_settings_on_a_mono_camera_print_nothing_about_white_balance(flir, monkeypatch, capsys):
	# The message used to come from here, for every mono camera
	for step in ("ConfigureFrameWidth", "ConfigureFrameHeight", "ConfigurePixelFormat", "ConfigureExposure",
			"ConfigureGain", "ConfigureGamma", "ConfigureBuffer", "ConfigureChunkData"):
		monkeypatch.setattr(flir, step, lambda camera, cam_params: cam_params)
	cam = flir.ConfigureCustomImageSettings(FakeCamera(mono=True), params())
	assert cam["settingsConfig"] is True
	assert "white balance" not in capsys.readouterr().out.lower()
