"""FLIR gamma (cameraGamma): set, or turned off, on the camera, kept within what it takes, and said in metadata.csv."""

import csv
import importlib
import sys
import types

import pytest


class Switch:
	# GammaEnable (a boolean node)
	def __init__(self, value=False, writable=True):
		self.value, self.available, self.writable = value, True, writable

	def SetValue(self, value):
		self.value = value

	def GetValue(self):
		return self.value


class Gamma:
	# Gamma (a float node). On a Blackfly S it can be set only while GammaEnable is on.
	def __init__(self, switch, value=0.8, lowest=0.25, highest=4.0):
		self.switch, self.value, self.lowest, self.highest, self.available = switch, value, lowest, highest, True
		self.set = []

	@property
	def writable(self):
		return self.switch is None or self.switch.value

	def SetValue(self, value):
		assert self.writable, "Gamma set while GammaEnable is off"
		assert self.lowest <= value <= self.highest, "Gamma set outside its range"
		self.set.append(value)
		self.value = value

	def GetValue(self):
		return self.value

	def GetMin(self):
		return self.lowest

	def GetMax(self):
		return self.highest


class FakeCamera:
	def __init__(self, switch_name="GammaEnable", gamma=True, enabled=False, switch_writable=True):
		self.nodes = {}
		switch = None
		if switch_name:
			switch = self.nodes[switch_name] = Switch(enabled, switch_writable)
		if gamma:
			self.nodes["Gamma"] = Gamma(switch)

	def GetNodeMap(self):
		return self

	def GetNode(self, name):
		return self.nodes.get(name)


def FakePySpin():
	spin = types.ModuleType("PySpin")
	for kind in ("CBooleanPtr", "CFloatPtr", "CIntegerPtr", "CEnumerationPtr"):
		setattr(spin, kind, lambda node: node)
	spin.IsAvailable = lambda node: node is not None and node.available
	spin.IsReadable = lambda node: node is not None and node.available
	spin.IsWritable = lambda node: node is not None and node.writable
	spin.SpinnakerException = type("SpinnakerException", (Exception,), {})
	return spin


@pytest.fixture
def flir(monkeypatch):
	monkeypatch.setitem(sys.modules, "PySpin", FakePySpin())
	monkeypatch.delitem(sys.modules, "campy.cameras.flir", raising=False)
	module = importlib.import_module("campy.cameras.flir")
	yield module
	sys.modules.pop("campy.cameras.flir", None)


def params(gamma):
	return {"cameraName": "Camera0", "cameraDebug": False, "cameraGamma": gamma}


def test_gamma_is_turned_on_and_set(flir, capsys):
	camera = FakeCamera(enabled=False)
	p = flir.ConfigureGamma(camera, params(0.6))
	assert camera.nodes["GammaEnable"].value is True
	assert camera.nodes["Gamma"].set == [0.6]
	assert p["gamma"] == 0.6
	assert capsys.readouterr().out == "Gamma set to 0.6.\n"


def test_off_turns_gamma_off(flir, capsys):
	camera = FakeCamera(enabled=True)
	p = flir.ConfigureGamma(camera, params("Off"))
	assert camera.nodes["GammaEnable"].value is False
	assert camera.nodes["Gamma"].set == []
	assert p["gamma"] == "Off"
	assert capsys.readouterr().out == "Gamma off.\n"


@pytest.mark.parametrize("asked, got, limits", [(0.1, 0.25, "0.25 to 4"), (5, 4.0, "0.25 to 4")])
def test_gamma_outside_the_cameras_range_goes_to_its_limit(flir, capsys, asked, got, limits):
	camera = FakeCamera()
	p = flir.ConfigureGamma(camera, params(asked))
	assert camera.nodes["Gamma"].set == [got]
	assert p["gamma"] == got
	out = capsys.readouterr().out
	assert "Camera0: cameraGamma {} is outside what this camera takes, {}. Gamma set to {:g}.".format(
		asked, limits, got) in out


def test_older_cameras_gamma_switch(flir):
	# Older Point Grey models (Grasshopper3, Chameleon3) call it GammaEnabled
	camera = FakeCamera(switch_name="GammaEnabled")
	assert flir.ConfigureGamma(camera, params(1.2))["gamma"] == 1.2
	assert camera.nodes["GammaEnabled"].value is True


def test_a_camera_without_gamma_says_so(flir, capsys):
	p = flir.ConfigureGamma(FakeCamera(switch_name=None, gamma=False), params(0.6))
	assert "gamma" not in p
	assert capsys.readouterr().out == "Camera0: could not set its gamma to 0.6 (cameraGamma): it has no Gamma " \
		"it can change.\n"
	p = flir.ConfigureGamma(FakeCamera(switch_name=None, gamma=False), params("Off"))
	assert "gamma" not in p
	assert "could not turn its gamma off" in capsys.readouterr().out


def test_metadata_says_the_gamma_the_camera_has(flir, tmp_path):
	from campy.cameras import unicam
	p = flir.ConfigureGamma(FakeCamera(), params(9))
	p["videoFolder"] = str(tmp_path)
	unicam.WriteMetadata(p)
	with open(tmp_path / "Camera0" / "metadata.csv", newline="") as f:
		rows = dict(csv.reader(f))
	assert rows["cameraGamma"] == "9" and rows["gamma"] == "4.0"
