"""FLIR pixel format, trigger source and StartGrabbing's error path, against stand-ins for PySpin and the camera."""

import importlib
import sys
import types

import pytest


class Node:
	# A QuickSpin node (camera.PixelFormat, camera.TriggerSource, ...): records what is set
	def __init__(self, value=None):
		self.value, self.set = value, []

	def GetAccessMode(self):
		return "RW"

	def SetValue(self, value):
		self.set.append(value)
		self.value = value

	def GetValue(self):
		return self.value


class FakeCamera:
	def __init__(self):
		for name in ("PixelFormat", "IspEnable", "TriggerMode", "TriggerSource", "TriggerOverlap"):
			setattr(self, name, Node())

	def BeginAcquisition(self):
		raise RuntimeError("not a SpinnakerException")


def FakePySpin():
	spin = types.ModuleType("PySpin")
	spin.RW = "RW"
	spin.SpinnakerException = type("SpinnakerException", (Exception,), {})
	for name in ("Mono8", "RGB8Packed", "BayerRG8", "BayerBG8", "Mono16"):
		setattr(spin, "PixelFormat_" + name, name)
	for name in ("Line0", "Line1", "Line2", "Line3", "Software"):
		setattr(spin, "TriggerSource_" + name, name)
	spin.TriggerMode_On, spin.TriggerMode_Off, spin.TriggerOverlap_ReadOut = "On", "Off", "ReadOut"
	return spin


@pytest.fixture
def flir(monkeypatch):
	monkeypatch.setitem(sys.modules, "PySpin", FakePySpin())
	monkeypatch.delitem(sys.modules, "campy.cameras.flir", raising=False)
	module = importlib.import_module("campy.cameras.flir")
	yield module
	sys.modules.pop("campy.cameras.flir", None)


def params(**values):
	p = {"cameraName": "Camera0", "cameraDebug": False, "pixelFormatInput": "gray", "cameraTrigger": "None"}
	p.update(values)
	return p


@pytest.mark.parametrize("pixel_format, expected", [
	("gray", "Mono8"), ("rgb24", "RGB8Packed"), ("bayer_rggb8", "BayerRG8"), ("bayer_bggr8", "BayerRG8")])
def test_the_pixel_formats_campy_sets(flir, pixel_format, expected, capsys):
	camera = FakeCamera()
	flir.ConfigurePixelFormat(camera, params(pixelFormatInput=pixel_format))
	assert camera.PixelFormat.set == [expected]
	assert capsys.readouterr().out == ""


@pytest.mark.parametrize("pixel_format", ["gray16le", "Mono16", "yuv420p"])
def test_any_other_pixel_format_is_said_and_left(flir, pixel_format, capsys, caplog):
	# The camera's format is not changed, and campy says why, rather than
	# failing silently (the old lookup was a call that was never closed)
	camera = FakeCamera()
	flir.ConfigurePixelFormat(camera, params(pixelFormatInput=pixel_format))
	assert camera.PixelFormat.set == []
	out = capsys.readouterr().out
	assert 'Camera0: campy does not set pixelFormatInput "{}" on a FLIR camera'.format(pixel_format) in out
	assert "The camera keeps its own pixel format." in out
	assert not caplog.records # no error logged


@pytest.mark.parametrize("trigger, source", [("Line3", "Line3"), ("Line0", "Line0"), ("Software", "Software")])
def test_trigger_sources(flir, trigger, source):
	camera = FakeCamera()
	p = flir.ConfigureTrigger(camera, params(cameraTrigger=trigger))
	assert camera.TriggerSource.set == [source]
	assert camera.TriggerMode.value == "On" and p["configTrig"]


def test_an_unknown_trigger_source_is_said_and_mode_left_off(flir, capsys):
	camera = FakeCamera()
	p = flir.ConfigureTrigger(camera, params(cameraTrigger="Line9"))
	assert camera.TriggerSource.set == [] and camera.TriggerMode.value == "Off" and not p["configTrig"]
	assert 'Camera0: cameraTrigger "Line9" is not a FLIR trigger source' in capsys.readouterr().out


def test_no_trigger_free_runs(flir):
	camera = FakeCamera()
	p = flir.ConfigureTrigger(camera, params(cameraTrigger="None"))
	assert camera.TriggerMode.value == "Off" and camera.TriggerSource.set == [] and not p["configTrig"]


def test_start_grabbing_reports_an_error_that_is_not_spinnaker_s(flir, capsys):
	# It said nothing and raised AttributeError instead: ".foramt"
	assert flir.StartGrabbing(FakeCamera()) is False
	assert "not a SpinnakerException" in capsys.readouterr().out
