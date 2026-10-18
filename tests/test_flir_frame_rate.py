"""A free-running FLIR camera runs at frameRate, against stand-ins for PySpin and the camera."""

import importlib
import json
import os
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
		self.camera.write(self.attr, self.entries[value].symbolic)


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
def flir(monkeypatch, tmp_path):
	monkeypatch.setitem(sys.modules, "PySpin", FakePySpin())
	monkeypatch.delitem(sys.modules, "campy.cameras.flir", raising=False)
	module = importlib.import_module("campy.cameras.flir")
	# LoadSettings' other steps drive the camera through QuickSpin, which the
	# stand-in does not have: here they do nothing
	monkeypatch.setattr(module, "ConfigureTrigger", lambda camera, cam_params: cam_params)
	monkeypatch.setattr(module, "ConfigureCustomImageSettings", lambda camera, cam_params: cam_params)
	# The frame rate notes go here, not to ~/.campy
	monkeypatch.setattr(module, "NOTE_FOLDER", str(tmp_path / ".campy"))
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
	assert camera.enabled == found["enabled"] and not os.path.exists(note(flir))
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
	assert not os.path.exists(note(flir))
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


# ------------------------------------------------------------------ a run that never closes the camera

FOUND = [{"enabled": False}, {"enabled": True, "rate": 59.9947}]


def note(flir):
	return flir.FrameRateNotePath(params())


def killed_run(flir, camera):
	# A free-running recording whose process dies before CloseCamera (killed,
	# crashed, the computer lost power but the camera did not): the camera keeps
	# campy's cap, and the next run starts from a new cam_params
	flir.LoadSettings(camera, params())
	assert camera.enabled and camera.rate == pytest.approx(80, abs=0.01)
	assert os.path.exists(note(flir))


def capped(found, model):
	# An older model with AcquisitionFrameRateAuto Continuous picks its own rate: no cap
	return found["enabled"] and not model


@pytest.mark.parametrize("found", FOUND)
def test_the_next_run_puts_back_what_a_killed_run_found(flir, model, found, capsys):
	camera = FakeCamera(legacy=model, **found)
	killed_run(flir, camera)
	capsys.readouterr()
	# The next run puts that back before it reads what the camera has, so it
	# does not take the killed run's cap for the camera's own and put it back
	# again at its close
	cam = flir.LoadSettings(camera, params())
	said = "Camera0: campy did not close it last time and left it capped at 80.0 fps. "
	said += "Cap set back to 60.0 fps, as before." if capped(found, model) else "Cap removed, as before."
	assert said in capsys.readouterr().out
	assert cam["frameRateFound"]["enabled"] == found["enabled"]
	flir.CloseCamera(cam, camera)
	assert camera.enabled == found["enabled"] and not os.path.exists(note(flir))
	if found["enabled"]:
		assert camera.rate == pytest.approx(59.9947, abs=0.01)
	if model:
		assert camera.auto == "Continuous"


def test_a_triggered_run_after_a_killed_one_removes_its_cap(flir, model, capsys):
	# Else it drops triggers faster than the cap: a Blackfly S capped at 10 fps
	# kept 21 of 100 software triggers at 50 Hz
	camera = FakeCamera(legacy=model)
	killed_run(flir, camera)
	capsys.readouterr()
	cam = flir.LoadSettings(camera, params(cameraTrigger="Line3"))
	assert not camera.enabled and not os.path.exists(note(flir))
	if model:
		assert camera.auto == "Continuous"
	assert "Camera0: campy did not close it last time and left it capped at 80.0 fps. Cap removed, as before." \
		in capsys.readouterr().out
	camera.writes.clear()
	flir.CloseCamera(cam, camera)
	assert camera.writes == []


def test_the_launch_check_puts_it_back_and_says_so(flir, capsys):
	# It opens the cameras before their own processes do, and hides the
	# Configure* progress lines; this one is not hidden
	camera = FakeCamera()
	killed_run(flir, camera)
	capsys.readouterr()
	limits = flir.FrameRateLimits(camera, params(cameraTrigger="Line3"))
	assert capsys.readouterr().out == \
		"Camera0: campy did not close it last time and left it capped at 80.0 fps. Cap removed, as before.\n"
	# So the cap the killed run left does not fail a triggered camera's check
	assert limits["frameRateCap"] is None and limits["fps"] == pytest.approx(FakeCamera.SENSOR_FPS)
	assert not camera.enabled and not os.path.exists(note(flir))
	# and the camera's own process finds nothing left to put back
	flir.LoadSettings(camera, params(cameraTrigger="Line3"))
	assert "did not close" not in capsys.readouterr().out


def test_a_launch_check_that_never_closes_the_camera(flir, monkeypatch, capsys):
	# Killed after it took the cap off to read the most the camera can do
	camera = FakeCamera(enabled=True, rate=59.9947)
	restore = flir.RestoreFrameRate
	monkeypatch.setattr(flir, "RestoreFrameRate", lambda camera, cam_params: None)
	flir.FrameRateLimits(camera, params())
	assert not camera.enabled
	monkeypatch.setattr(flir, "RestoreFrameRate", restore)
	cam = flir.LoadSettings(camera, params())
	assert "Camera0: campy did not close it last time. Cap set back to 60.0 fps, as before." \
		in capsys.readouterr().out
	assert cam["frameRateFound"]["enabled"]
	flir.CloseCamera(cam, camera)
	assert camera.enabled and camera.rate == pytest.approx(59.9947, abs=0.01)


def test_the_note_is_written_before_the_camera_changes(flir, model):
	# A run killed between the two still leaves it
	camera = FakeCamera(legacy=model, enabled=True, rate=59.9947)
	notes = []
	write = camera.write
	camera.write = lambda attr, value: notes.append(flir.ReadFrameRateNote(note(flir))) or write(attr, value)
	flir.LoadSettings(camera, params())
	expected = {"enabled": True, "rate": 59.9947, "auto": "Continuous" if model else None}
	assert notes and all(n == expected for n in notes)


def test_the_note(flir):
	camera = FakeCamera()
	cam = flir.LoadSettings(camera, params())
	path = note(flir)
	assert os.path.dirname(path) == flir.NOTE_FOLDER
	assert os.path.basename(path) == "flir-frame-rate-24243505.json"
	with open(path) as f:
		saved = json.load(f)
	assert saved["enabled"] is False and saved["auto"] is None
	assert saved["cameraName"] == "Camera0" and saved["cameraSerialNo"] == "24243505"
	flir.CloseCamera(cam, camera)
	assert os.listdir(flir.NOTE_FOLDER) == [] # nothing half-written left behind either


def test_a_camera_unplugged_since_is_not_reported(flir, capsys):
	# Unplugging it took the cap off already: nothing to say
	killed_run(flir, FakeCamera())
	capsys.readouterr()
	camera = FakeCamera()
	flir.LoadSettings(camera, params(cameraTrigger="Line3"))
	assert "did not close" not in capsys.readouterr().out
	assert not camera.enabled and not os.path.exists(note(flir))


def test_a_note_that_cannot_be_put_back_is_kept(flir):
	camera = FakeCamera(enabled=True, rate=59.9947)
	killed_run(flir, camera)
	switch = camera.nodes["AcquisitionFrameRateEnable"]
	real = switch.set

	def refuse_once(on):
		switch.set = real
		raise SpinError("AccessException")

	switch.set = refuse_once
	cam = flir.LoadSettings(camera, params())
	# This run takes what to put back from the note, not from the cap the killed run left
	assert cam["frameRateFound"]["rate"] == pytest.approx(59.9947)
	assert flir.ReadFrameRateNote(note(flir))["rate"] == pytest.approx(59.9947)
	flir.CloseCamera(cam, camera)
	assert camera.enabled and camera.rate == pytest.approx(59.9947, abs=0.01)
	assert not os.path.exists(note(flir))


def test_an_unreadable_note_is_dropped(flir):
	os.makedirs(flir.NOTE_FOLDER)
	with open(note(flir), "w") as f:
		f.write("{")
	camera = FakeCamera(enabled=True, rate=30.0)
	flir.LoadSettings(camera, params(cameraTrigger="Line3"))
	assert camera.writes == [] and not os.path.exists(note(flir))


def test_a_note_that_cannot_be_written(flir, monkeypatch, tmp_path, capsys):
	# The recording goes ahead, and says what it risks
	(tmp_path / "file").write_text("")
	monkeypatch.setattr(flir, "NOTE_FOLDER", str(tmp_path / "file" / ".campy"))
	camera = FakeCamera()
	cam = flir.LoadSettings(camera, params())
	assert camera.rate == pytest.approx(80, abs=0.01)
	out = capsys.readouterr().out
	assert "Camera0: could not write " in out
	assert "If campy stops without closing this camera, it keeps its frame rate cap until it is unplugged." in out
	flir.CloseCamera(cam, camera)
	assert not camera.enabled


@pytest.mark.parametrize("serial", [None, []])
def test_a_camera_without_a_serial_number(flir, serial):
	# No note to key by it: it is still set and put back
	camera = FakeCamera()
	cam = flir.LoadSettings(camera, params(cameraSerialNo=serial))
	assert camera.rate == pytest.approx(80, abs=0.01) and not os.path.exists(flir.NOTE_FOLDER)
	flir.CloseCamera(cam, camera)
	assert not camera.enabled


def test_each_camera_has_its_own_note(flir, capsys):
	killed_run(flir, FakeCamera())
	other = FakeCamera(enabled=True, rate=30.0)
	flir.LoadSettings(other, params(cameraName="Camera1", cameraSerialNo="24243506", cameraTrigger="Line3"))
	assert other.writes == [] and os.path.exists(note(flir))
	assert "did not close" not in capsys.readouterr().out


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
