"""A free-running FLIR camera runs at frameRate, against stand-ins for PySpin and the camera."""

import importlib
import sys
import types

import pytest

from campy import preflight

FRAME_BYTES = 1280 * 1024 # Mono8


class SpinError(Exception):
	pass


class Node:
	# One node of a FakeCamera: its value comes from, and goes to, the camera
	def __init__(self, get, set=None, writable=lambda: True, limits=None):
		self.get, self.set, self.can_write, self.limits = get, set, writable, limits

	def writable(self):
		return self.set is not None and self.can_write()

	def GetValue(self):
		return self.get()

	def SetValue(self, value):
		if not self.writable():
			raise SpinError("AccessException: node is not writable")
		if self.limits and not self.limits()[0] <= value <= self.limits()[1]:
			raise SpinError("OutOfRangeException: {} is outside {}".format(value, self.limits()))
		self.set(value)

	def GetMin(self):
		return self.limits()[0]

	def GetMax(self):
		return self.limits()[1]

	def GetInc(self):
		return 1


class Entry:
	def __init__(self, symbolic, value):
		self.symbolic, self.value = symbolic, value

	def GetSymbolic(self):
		return self.symbolic

	def GetValue(self):
		return self.value


class Enumeration:
	def __init__(self, camera, attr, symbolics):
		self.camera, self.attr = camera, attr
		self.entries = [Entry(s, i) for i, s in enumerate(symbolics)]

	def writable(self):
		return True

	def GetCurrentEntry(self):
		return self.GetEntryByName(getattr(self.camera, self.attr))

	def GetEntryByName(self, name):
		return next((e for e in self.entries if e.symbolic == name), None)

	def SetIntValue(self, value):
		setattr(self.camera, self.attr, self.entries[value].symbolic)


class FakeCamera:
	"""
	A free-running camera as the BFS-U3-13Y3M on the EVO-X2 behaved on
	2026-10-16 (PySpin 4.4.0.246, 1280x1024 Mono8): AcquisitionFrameRate can be
	written only while AcquisitionFrameRateEnable is on, and refuses a value
	outside its range; its maximum follows the USB link's throughput limit but
	not the exposure; it only caps the rate, so a long exposure holds the
	camera below it (AcquisitionResultingFrameRate: 49.75 fps at 20 ms); and
	while the switch is off, AcquisitionFrameRate reads as the most the sensor
	and link allow, which is the rate it has when the switch goes back on.
	legacy=True is an older Point Grey model (Grasshopper3, Chameleon3), with
	the node names of FLIR's ROS driver: AcquisitionFrameRateEnabled, and
	AcquisitionFrameRateAuto, which must be Off before the rate can be set.
	It is given no AcquisitionResultingFrameRate.
	"""
	SENSOR_FPS = 170.2074 # its readout at 1280x1024
	MAX_SETTING = 170.5977 # AcquisitionFrameRate's maximum at 380 MB/s
	EXPOSURE_OVERHEAD_US = 102 # 19998 us exposures came out at 49.75 fps

	def __init__(self, legacy=False, enabled=False, rate=None, exposure=1500.0, link=380000000, auto="Continuous"):
		self.legacy, self.enabled, self.exposure, self.link, self.auto = legacy, enabled, exposure, link, auto
		self.rate = rate if rate is not None else self.SENSOR_FPS
		self.writes = []
		self.calls = []
		switch = "AcquisitionFrameRateEnabled" if legacy else "AcquisitionFrameRateEnable"
		self.nodes = {
			switch: Node(lambda: self.enabled, self.switch),
			"AcquisitionFrameRate": Node(lambda: self.rate if self.enabled else self.uncapped(),
				lambda v: self.write("rate", v * 0.999976), # its own step: 80 is set as 79.998
				writable=lambda: self.enabled and (not legacy or self.auto == "Off"),
				limits=lambda: (1.0, self.highest())),
			"ExposureTime": Node(lambda: self.exposure),
			"PayloadSize": Node(lambda: FRAME_BYTES),
			# 0-500 MB/s in steps of 1 byte/s, as on the BFS-U3-13Y3M
			"DeviceLinkThroughputLimit": Node(lambda: self.link, lambda v: self.write("link", v),
				limits=lambda: (0, 500000000)),
			}
		if legacy:
			self.nodes["AcquisitionFrameRateAuto"] = Enumeration(self, "auto", ["Off", "Continuous"])
		else:
			self.nodes["AcquisitionResultingFrameRate"] = Node(self.resulting)

	def write(self, attr, value):
		self.writes.append(attr)
		setattr(self, attr, value)

	def switch(self, on):
		if on and not self.enabled:
			self.rate = self.uncapped()
		self.write("enabled", on)

	def uncapped(self):
		# AcquisitionFrameRate while the switch is off
		return min(self.SENSOR_FPS, self.link / FRAME_BYTES)

	def highest(self):
		return min(self.MAX_SETTING, self.link / FRAME_BYTES)

	def ceiling(self):
		# The rate without a cap: sensor readout, USB link and exposure
		return min(self.SENSOR_FPS, self.link / FRAME_BYTES, 1e6 / (self.exposure + self.EXPOSURE_OVERHEAD_US))

	def resulting(self):
		return min(self.ceiling(), self.rate) if self.enabled else self.ceiling()

	def GetNodeMap(self):
		return self

	def GetNode(self, name):
		return self.nodes.get(name)

	def Init(self):
		self.calls.append("Init")

	def DeInit(self):
		self.calls.append("DeInit")

	def EndAcquisition(self):
		self.calls.append("EndAcquisition")


def FakePySpin():
	spin = types.ModuleType("PySpin")
	for kind in ("CBooleanPtr", "CFloatPtr", "CIntegerPtr", "CEnumerationPtr"):
		setattr(spin, kind, lambda node: node)
	spin.IsAvailable = lambda node: node is not None
	spin.IsReadable = lambda node: node is not None
	spin.IsWritable = lambda node: node is not None and node.writable()
	spin.SpinnakerException = SpinError
	return spin


@pytest.fixture
def flir(monkeypatch):
	monkeypatch.setitem(sys.modules, "PySpin", FakePySpin())
	monkeypatch.delitem(sys.modules, "campy.cameras.flir", raising=False)
	module = importlib.import_module("campy.cameras.flir")
	# LoadSettings' other steps drive the camera through QuickSpin, which the
	# stand-in does not have: here they do nothing
	monkeypatch.setattr(module, "ConfigureTrigger", lambda camera, cam_params: cam_params)
	monkeypatch.setattr(module, "ConfigureCustomImageSettings", lambda camera, cam_params: cam_params)
	yield module
	sys.modules.pop("campy.cameras.flir", None)


def params(**values):
	cam = {"cameraName": "Camera0", "cameraSerialNo": "24243505", "cameraTrigger": "None", "frameRate": 80,
		"frameWidth": 1280, "frameHeight": 1024}
	cam.update(values)
	return cam


@pytest.fixture(params=["Blackfly S", "Grasshopper3"])
def model(request):
	return request.param == "Grasshopper3"


def test_free_run_runs_at_frame_rate(flir, model, capsys):
	camera = FakeCamera(legacy=model)
	cam = flir.LoadSettings(camera, params())
	assert camera.enabled and camera.rate == pytest.approx(80, abs=0.01)
	assert cam["resultingFrameRate"] == pytest.approx(80, abs=0.01)
	assert "Frame rate set to 79.998 fps (free-running)" in capsys.readouterr().out
	if model:
		assert camera.auto == "Off"


@pytest.mark.parametrize("trigger", ["None", "none", "NONE", None])
def test_no_trigger_is_free_running(flir, trigger):
	assert flir.FreeRuns(params(cameraTrigger=trigger))


@pytest.mark.parametrize("trigger", ["Line0", "Line3", "software"])
def test_triggered_camera_is_left_alone(flir, model, trigger):
	# The trigger sets its rate; a cap someone left on it is the launch check's to report
	camera = FakeCamera(legacy=model, enabled=True, rate=30.0, auto="Off")
	cam = flir.LoadSettings(camera, params(cameraTrigger=trigger))
	assert camera.writes == [] and camera.enabled and camera.rate == 30.0
	assert "resultingFrameRate" not in cam and "frameRateFound" not in cam
	flir.RestoreFrameRate(camera, cam)
	assert camera.writes == []


def test_rate_is_set_after_the_frame_size(flir, monkeypatch):
	# At its old frame size (or USB limit) the camera could not reach 80 fps;
	# once campy's own settings are on it, it can
	camera = FakeCamera(link=43000000)
	order = []

	def image_settings(camera, cam_params):
		order.append("image settings")
		camera.link = 380000000
		return cam_params

	monkeypatch.setattr(flir, "ConfigureCustomImageSettings", image_settings)
	monkeypatch.setattr(flir, "FreeRuns", lambda cam_params: order.append("frame rate") or True)
	flir.LoadSettings(camera, params())
	assert order == ["image settings", "frame rate"]
	assert camera.rate == pytest.approx(80, abs=0.01)


def test_a_long_exposure_holds_it_below_frame_rate(flir, capsys):
	camera = FakeCamera(exposure=19998.0)
	cam = flir.ConfigureFrameRate(camera, params())
	# The cap is set; the exposure is what holds it back
	assert camera.rate == pytest.approx(80, abs=0.01)
	assert cam["resultingFrameRate"] == 49.751
	assert capsys.readouterr().out.strip() == (
		"Camera0 can run at only 49.8 fps free-running with campy's settings, below frameRate 80: "
		"its exposure, 19998 us (cameraExposureTimeInUs), is longer than a frame at 80 fps (12500 us). "
		"It will record at 49.8 fps, and its video will say 80 fps.")


def test_a_slow_usb_link_holds_it_below_frame_rate(flir, capsys):
	# The camera refuses 80 above its maximum (32.8 at 43 MB/s): campy sets the maximum
	camera = FakeCamera(link=43000000)
	cam = flir.ConfigureFrameRate(camera, params())
	assert camera.enabled and camera.rate == pytest.approx(43000000 / FRAME_BYTES, rel=1e-4)
	assert cam["resultingFrameRate"] == pytest.approx(32.8, abs=0.01)
	out = capsys.readouterr().out
	assert out.startswith("Camera0 can run at only 32.8 fps free-running with campy's settings, below frameRate 80: "
		"its USB link is limited to 43 MB/s (DeviceLinkThroughputLimit), and 1.3 MB frames at 80 fps need 105 MB/s.")
	assert out.strip().endswith("It will record at 32.8 fps, and its video will say 80 fps.")


def test_faster_than_the_sensor(flir, capsys):
	camera = FakeCamera()
	flir.ConfigureFrameRate(camera, params(frameRate=200))
	assert camera.rate == pytest.approx(FakeCamera.MAX_SETTING, rel=1e-4)
	assert "below frameRate 200: its sensor reads out 1280x1024 frames no faster." in capsys.readouterr().out


def test_slower_than_the_camera_goes(flir, capsys):
	camera = FakeCamera()
	cam = flir.ConfigureFrameRate(camera, params(frameRate=0.5))
	assert cam["resultingFrameRate"] == pytest.approx(1.0, abs=0.001)
	assert capsys.readouterr().out.strip() == (
		"Camera0: frameRate 0.5 is below the slowest this camera free-runs at, 1.0 fps (AcquisitionFrameRate's "
		"minimum). It will record at 1.0 fps, and its video will say 0.5 fps. Trigger it to record more slowly.")


def test_a_camera_without_frame_rate_control(flir, capsys):
	camera = FakeCamera()
	del camera.nodes["AcquisitionFrameRateEnable"]
	cam = flir.ConfigureFrameRate(camera, params())
	assert "resultingFrameRate" not in cam and "frameRateFound" not in cam
	assert "it free-runs at its own rate, not at frameRate 80" in capsys.readouterr().out


@pytest.mark.parametrize("found", [
	{"enabled": False},
	{"enabled": True, "rate": 59.9947},
	])
def test_closing_puts_back_what_it_found(flir, model, found):
	# A cap left on would hold a later triggered recording to this frameRate
	camera = FakeCamera(legacy=model, **found)
	cam = flir.LoadSettings(camera, params())
	assert camera.enabled and camera.rate == pytest.approx(80, abs=0.01)
	flir.CloseCamera(cam, camera)
	assert camera.calls == ["EndAcquisition", "DeInit"]
	assert camera.enabled == found["enabled"]
	if found["enabled"]:
		assert camera.rate == pytest.approx(59.9947, abs=0.01)
	if model:
		assert camera.auto == "Continuous"


@pytest.mark.parametrize("found", [
	{"enabled": False},
	{"enabled": True, "rate": 59.9947},
	])
def test_launch_check_reads_the_most_it_can_do(flir, model, found):
	# Not the 80 fps cap campy gives it: a WARN for being close to that would be wrong
	camera = FakeCamera(legacy=model, **found)
	limits = flir.FrameRateLimits(camera, params())
	assert limits["frameRateCap"] is None
	if not model: # an older model reports no AcquisitionResultingFrameRate
		assert limits["fps"] == pytest.approx(FakeCamera.SENSOR_FPS)
		assert preflight.FrameRateFinding(params(), limits) == (preflight.OK,
			"Camera0 (24243505): up to 170.2 fps with campy's settings; frameRate 80")
	# It closes the camera as it found it, for the camera's own process to set up
	assert camera.calls == ["Init", "DeInit"] and camera.enabled == found["enabled"]
	if found["enabled"]:
		assert camera.rate == pytest.approx(59.9947, abs=0.01)
	if model:
		assert camera.auto == "Continuous"


def test_launch_check_names_what_holds_a_free_running_camera_back(flir):
	camera = FakeCamera(exposure=19998.0)
	status, text = preflight.FrameRateFinding(params(), flir.FrameRateLimits(camera, params()))
	assert status == preflight.FAIL and "can run at only 49.8 fps" in text and "cameraExposureTimeInUs" in text
	# A link campy is told to leave alone (by default it raises a limit that is too low)
	camera = FakeCamera(link=43000000)
	slow = params(cameraLinkLimitMBps="None")
	status, text = preflight.FrameRateFinding(slow, flir.FrameRateLimits(camera, slow))
	assert status == preflight.FAIL and "DeviceLinkThroughputLimit" in text and "AcquisitionFrameRate caps" not in text
	assert 'Set cameraLinkLimitMBps to "auto"' in text


def test_launch_check_still_names_a_cap_on_a_triggered_camera(flir):
	camera = FakeCamera(enabled=True, rate=30.0)
	limits = flir.FrameRateLimits(camera, params(cameraTrigger="Line3"))
	assert limits["frameRateCap"] == 30.0 and limits["fps"] == 30.0
	assert camera.writes == [] and camera.enabled


# ------------------------------------------------------------------ USB throughput limit

def test_auto_raises_a_link_too_slow_for_frame_rate(flir, capsys):
	# The BFS-U3-13Y3M left at 43 MB/s through a computer's restart (its saved limit is 380)
	camera = FakeCamera(link=43000000)
	flir.LoadSettings(camera, params())
	assert camera.link == 500000000 and camera.rate == pytest.approx(80, abs=0.01)
	out = capsys.readouterr().out
	assert "USB throughput limit set to 500 MB/s (it was 43)" in out and "Frame rate set to 79.998 fps" in out


@pytest.mark.parametrize("link", [380000000, 120000000])
def test_auto_keeps_a_limit_that_is_enough(flir, link, capsys):
	# A lower limit can be on purpose (cameras sharing a USB bus): 80 fps of 1.3 MB frames need 105 MB/s
	camera = FakeCamera(link=link)
	flir.LoadSettings(camera, params())
	assert camera.link == link and "link" not in camera.writes
	assert "USB throughput limit {} MB/s (frameRate needs 105)".format(link // 1000000) in capsys.readouterr().out


def test_auto_raises_a_limit_just_short_of_enough(flir):
	# 110 MB/s is above the 105 the frames need, but inside the 10% margin
	camera = FakeCamera(link=110000000)
	flir.LoadSettings(camera, params())
	assert camera.link == 500000000


@pytest.mark.parametrize("setting, expected", [(200, 200000000), ("150", 150000000), (2.5, 2500000),
	(900, 500000000)])
def test_a_number_sets_the_limit(flir, setting, expected):
	# In MB/s, within the camera's range
	camera = FakeCamera()
	flir.LoadSettings(camera, params(cameraLinkLimitMBps=setting))
	assert camera.link == expected


@pytest.mark.parametrize("setting", ["None", "none", None])
def test_none_leaves_the_limit(flir, setting):
	camera = FakeCamera(link=43000000)
	flir.ConfigureLinkLimit(camera, params(cameraLinkLimitMBps=setting))
	assert camera.link == 43000000 and camera.writes == []


def test_a_camera_without_a_link_limit(flir):
	camera = FakeCamera()
	del camera.nodes["DeviceLinkThroughputLimit"]
	assert flir.ConfigureLinkLimit(camera, params())["cameraName"] == "Camera0"


def test_triggered_camera_gets_the_link_it_needs(flir):
	# The trigger sets the rate, but its frames still have to fit the link
	camera = FakeCamera(link=43000000)
	flir.LoadSettings(camera, params(cameraTrigger="Line3", frameRate=60))
	assert camera.link == 500000000 and camera.writes == ["link"]


def test_launch_check_passes_once_campy_raises_the_link(flir):
	camera = FakeCamera(link=43000000)
	assert preflight.FrameRateFinding(params(), flir.FrameRateLimits(camera, params())) == (preflight.OK,
		"Camera0 (24243505): up to 170.2 fps with campy's settings; frameRate 80")
	# and closes the camera with the limit it had, for the camera's own process to raise, and report
	assert camera.link == 43000000


def test_a_recording_leaves_the_raised_link(flir):
	camera = FakeCamera(link=43000000)
	flir.CloseCamera(flir.LoadSettings(camera, params()), camera)
	assert camera.link == 500000000


def test_a_number_already_set(flir, capsys):
	camera = FakeCamera(link=100000000)
	flir.ConfigureLinkLimit(camera, params(cameraLinkLimitMBps=100))
	assert camera.writes == [] and "USB throughput limit 100 MB/s..." in capsys.readouterr().out
