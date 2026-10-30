"""Launch check, against a fake sysfs USB tree and a hand-written capacity table."""

import importlib
import os
import socket
import sys
import types

import pytest
import yaml

from campy import configurator, preflight
from campy.preflight import OK, WARN, FAIL

CTRL_A = "0000:c4:00.3"
CTRL_B = "0000:c6:00.4"
FLIR = "1e10"


def make_sysfs(root, devices):
	"""
	devices: (controller, bus, bus_speed, name, vendor, serial, speed). Lays
	them out as the kernel does: device directories under their root hub
	under the PCI controller, and links to all of them in bus/usb/devices.
	"""
	if sys.platform == "win32":
		# The USB check reads Linux's sysfs, which names things 0000:00:08.1
		pytest.skip("sysfs names have colons, which Windows paths can't contain")
	links = root / "bus" / "usb" / "devices"
	links.mkdir(parents=True)
	hubs = {}
	for controller, bus, bus_speed, name, vendor, serial, speed in devices:
		hub = root / "devices" / "pci0000:00" / "0000:00:08.1" / controller / "usb{}".format(bus)
		if hub not in hubs.values():
			hub.mkdir(parents=True)
			(hub / "speed").write_text("{}\n".format(bus_speed))
			(hub / "idVendor").write_text("1d6b\n")
			os.symlink(hub, links / "usb{}".format(bus))
		hubs[bus] = hub
		dev = hub / name
		dev.mkdir()
		for attr, value in (("idVendor", vendor), ("idProduct", "1234"), ("speed", speed),
				("busnum", bus)):
			(dev / attr).write_text("{}\n".format(value))
		if serial is not None:
			(dev / "serial").write_text("{}\n".format(serial))
		(dev / (name + ":1.0")).mkdir() # an interface: not a device
		os.symlink(dev, links / name)
		os.symlink(dev / (name + ":1.0"), links / (name + ":1.0"))
	return str(root)


def cameras(serials, **values):
	p = configurator.DefaultParams()
	p.update(numCams=len(serials), cameraMake="flir", frameWidth=1280, frameHeight=1024,
		frameRate=60, pixelFormatInput="gray", pixelFormatOutput="yuv420p", preset="veryfast")
	p.update(values)
	p = configurator.FillCameraLists(p)
	systems = {"flir": {"serials": list(serials)}}
	return p, preflight.CameraList(systems, p)


def lines(report, check=None):
	return [(s, t) for s, c, t in report.lines if check is None or c == check]


def test_bytes_per_camera():
	_, cams = cameras(["1"])
	assert preflight.CameraBytesPerSec(cams[0]) == 1280 * 1024 * 60
	assert preflight.BytesPerPixel("bayer_rggb8") == 1
	assert preflight.BytesPerPixel("rgb24") == 3
	assert preflight.BytesPerPixel("mystery") is None


def test_serials_match():
	assert preflight.SerialsMatch("21025699", "21025699")
	assert preflight.SerialsMatch("21025699", "0140D3A3") # the same number in hex
	assert preflight.SerialsMatch("0021025699", "21025699")
	assert not preflight.SerialsMatch("21025699", "21025698")
	assert not preflight.SerialsMatch(None, "21025699")


def test_list_usb_devices(tmp_path):
	root = make_sysfs(tmp_path, [
		(CTRL_A, 4, 10000, "4-1", FLIR, "11", 5000),
		(CTRL_B, 5, 5000, "5-2", "046d", None, 480),
		])
	devices = {d["name"]: d for d in preflight.ListUsbDevices(root)}
	assert set(devices) == {"4-1", "5-2"} # no root hubs, no interfaces
	assert devices["4-1"]["controller"] == CTRL_A
	assert devices["4-1"]["bus"] == 4
	assert devices["4-1"]["bus_speed"] == 10000
	assert devices["4-1"]["speed"] == 5000
	assert devices["5-2"]["serial"] is None


def test_usb_loads_per_bus(tmp_path):
	root = make_sysfs(tmp_path, [
		(CTRL_A, 4, 10000, "4-1", FLIR, "11", 5000),
		(CTRL_A, 4, 10000, "4-2", FLIR, "12", 5000),
		(CTRL_B, 5, 5000, "5-1", FLIR, "0140D3A3", 5000), # serial 21025699 in hex
		(CTRL_A, 3, 480, "3-1", FLIR, "14", 480), # a camera on a USB 2 link
		])
	p, cams = cameras(["11", "12", "21025699", "14", "99"])
	report = preflight.Report()
	preflight.CheckUsb(cams, p, report, root)
	found = lines(report, "usb")
	# 2 cameras on the 10 Gb/s bus: 157 of 600 MB/s
	assert (OK, "bus 4 (10 Gb/s, controller {}): Camera0, Camera1 send 157 of 600 MB/s (26%)".format(CTRL_A)) in found
	assert any(s == OK and "bus 5" in t and "Camera2" in t for s, t in found)
	assert any(s == FAIL and "Camera3 is connected at 480 Mb/s" in t for s, t in found)
	assert any(s == WARN and "Camera4 (serial 99) is not on the USB bus" in t for s, t in found)


def test_usb_bus_over_budget(tmp_path):
	# 5 cameras at 79 MB/s on one 5 Gb/s bus: 393 MB/s of a 300 MB/s budget
	root = make_sysfs(tmp_path, [(CTRL_B, 5, 5000, "5-{}".format(i), FLIR, str(i), 5000) for i in range(1, 6)])
	p, cams = cameras([str(i) for i in range(1, 6)])
	report = preflight.Report()
	preflight.CheckUsb(cams, p, report, root)
	assert [s for s, _ in lines(report, "usb")] == [FAIL]
	assert "393 of 300 MB/s" in lines(report, "usb")[0][1]

	p["usbBudgetMBps"] = 500 # measured on this rig: the bus moves more
	report = preflight.Report()
	preflight.CheckUsb(cams, p, report, root)
	assert [s for s, _ in lines(report, "usb")] == [OK]


def test_usb_check_needs_sysfs_or_windows(tmp_path, monkeypatch):
	monkeypatch.setattr(preflight, "WINDOWS", False) # macOS
	p, cams = cameras(["1"])
	report = preflight.Report()
	preflight.CheckUsb(cams, p, report, str(tmp_path / "nothing"))
	assert lines(report) == [(WARN, "USB bandwidth not checked: it is read from Linux's sysfs, or from Windows' "
		"USB hubs.")]


def on_windows(monkeypatch, windows):
	from campy import usbwin
	monkeypatch.setattr(preflight, "WINDOWS", True)
	monkeypatch.setattr(usbwin, "Api", lambda: windows)


def test_usb_on_windows(tmp_path, monkeypatch):
	# The Dell Pro Max Slim FCS1250's two cameras on USB 3, and a third on a USB 2 link
	from test_usbwin import slim, HS
	windows = slim(**{"0171ED30": HS})
	on_windows(monkeypatch, windows)
	p, cams = cameras(["22168577", "24243504", "99"], frameRate=30)
	report = preflight.Report()
	preflight.CheckUsb(cams, p, report, str(tmp_path / "no-sysfs"))
	assert lines(report, "usb") == [
		(FAIL, "Camera1 is connected at 480 Mb/s, not USB 3, and needs 39 MB/s. Check its cable and port."),
		(WARN, "Camera2 (serial 99) is not on the USB bus: a GigE camera, or its USB serial is written differently."),
		(OK, "controller 8086:7f6e, its USB 3 links: Camera0 send 39 of 300 MB/s (13%)"),
		]


def test_usb_over_budget_on_windows_is_a_warning(tmp_path, monkeypatch):
	# 4 cameras at 60 fps on one controller: 315 MB/s, over the 300 MB/s of a 5 Gb/s link, but the controller may
	# run at 10 or 20 Gb/s: Windows doesn't say, so a WARN, not a FAIL
	from test_usbwin import FakeWindows, ROOT, CONTROLLER, SS, camera
	windows = FakeWindows(dict([(ROOT, (CONTROLLER, 0, None))] +
		[(camera("{:08X}".format(i)), (ROOT, 16 + i, SS)) for i in range(1, 5)]))
	on_windows(monkeypatch, windows)
	p, cams = cameras(["1", "2", "3", "4"])
	report = preflight.Report()
	preflight.CheckUsb(cams, p, report, str(tmp_path / "no-sysfs"))
	(status, text), = lines(report, "usb")
	assert status == WARN
	assert text.startswith("controller 8086:7f6e, its USB 3 links: Camera0, Camera1, Camera2, Camera3 send 315 of "
		"300 MB/s (105%). Windows doesn't say how fast a USB controller is")

	p["usbBudgetMBps"] = 250 # measured on this rig: then over it is a FAIL, as on Linux
	report = preflight.Report()
	preflight.CheckUsb(cams, p, report, str(tmp_path / "no-sysfs"))
	assert [s for s, _ in lines(report, "usb")] == [FAIL]


def test_usb_on_windows_unreadable(tmp_path, monkeypatch):
	from test_usbwin import FakeWindows, slim
	p, cams = cameras(["22168577", "24243504"])
	on_windows(monkeypatch, slim(**{"0171ED30": OSError("Opening hub X: Windows error 5")}))
	report = preflight.Report()
	preflight.CheckUsb(cams, p, report, str(tmp_path / "no-sysfs"))
	assert (WARN, "Camera1: its USB link speed could not be read (Opening hub X: Windows error 5).") in \
		lines(report, "usb")
	assert any(s == OK and "Camera0 send " in t for s, t in lines(report, "usb"))

	on_windows(monkeypatch, FakeWindows({}, gone="Listing USB devices failed (CONFIGRET 0x3)"))
	report = preflight.Report()
	preflight.CheckUsb(cams, p, report, str(tmp_path / "no-sysfs"))
	assert lines(report, "usb") == [(WARN, "USB links not checked: Listing USB devices failed (CONFIGRET 0x3).")]


TABLE = {
	"host": {"hostname": socket.gethostname()},
	"entries": [{
		"key": "libx264/veryfast", "width": 1280, "height": 1024, "fps": 60.0,
		"pix_fmt_in": "gray", "pix_fmt_out": "yuv420p", "headroom": 1.25,
		"max_streams": 6, "max_tested": 8, "max_mpx_s": 471.9, "bitrate_mbps": 4.0,
		}],
	}


def test_capacity_fits_and_fails():
	report = preflight.Report()
	preflight.CheckCapacity(cameras(list("1234"))[1], TABLE, report, "t.json")
	assert lines(report) == [(OK, "libx264/veryfast: 4 cameras at 1280x1024 60 fps; measured to keep up with 6 (x1.25 headroom)")]

	report = preflight.Report()
	preflight.CheckCapacity(cameras(list("12345678"))[1], TABLE, report, "t.json")
	assert lines(report)[0][0] == FAIL


def test_capacity_estimated_for_other_rates():
	report = preflight.Report()
	preflight.CheckCapacity(cameras(list("1234"), frameRate=30)[1], TABLE, report, "t.json")
	status, text = lines(report)[0]
	assert status == OK and "need 157 Mpx/s" in text and "estimate" in text


def test_capacity_unmeasured_and_mixed():
	_, cams = cameras(list("1234"), gpuID=[-1, -1, 0, 0], gpuMake="vaapi")
	report = preflight.Report()
	preflight.CheckCapacity(cams, TABLE, report, "t.json")
	found = lines(report)
	assert any(s == WARN and t.startswith("h264_vaapi was not measured") for s, t in found)
	assert any(s == WARN and "measured one at a time" in t for s, t in found)

	report = preflight.Report()
	preflight.CheckCapacity(cams, None, report, "t.json")
	assert lines(report)[0][0] == WARN and "campy-encode-sweep" in lines(report)[0][1]


# A sweep with --streams 1,2,4,6,8 that kept up at every count: 8 is as many
# as it tried, not the encoder's limit
SWEPT_ALL = dict(TABLE["entries"][0], max_streams=8, max_tested=8, max_mpx_s=629.1,
	results=[{"streams": n, "pass": True} for n in (1, 2, 4, 6, 8)])

# What campy-encode-sweep saved for an encoder this computer does not have
# (VA-API on a Mac), before it marked such an entry as an error
ERRORED = dict(TABLE["entries"][0], key="h264_vaapi", max_streams=0, max_tested=1, max_mpx_s=0.0,
	bitrate_mbps=None, results=[{"streams": 1, "error": "stream 0: OSError('[Errno 32] Broken pipe')",
	"pass": False}])


def capacity(cams, *entries):
	report = preflight.Report()
	preflight.CheckCapacity(cams, dict(TABLE, entries=list(entries)), report, "t.json")
	return lines(report)


def test_too_many_from_the_sweeps_rows():
	rows = lambda *counts: [{"streams": n, "pass": ok} for n, ok in counts]
	assert preflight.TooMany(SWEPT_ALL) is None
	# --keep-going: 6 was too slow, and 8 kept up once after it. 6 is the limit.
	assert preflight.TooMany(dict(SWEPT_ALL, max_streams=4, results=rows((4, True), (6, False), (8, True)))) == 6
	# A table without the sweep's rows: the sweep stops at the first count that fails
	assert preflight.TooMany(TABLE["entries"][0]) == 8


def test_capacity_beyond_the_counts_tried():
	# 10 cameras, after a sweep that kept up at every count up to 8: a WARN, not a FAIL
	ten = cameras([str(n) for n in range(10)])[1]
	assert capacity(ten, SWEPT_ALL) == [(WARN, "libx264/veryfast: 10 cameras at 1280x1024 60 fps; measured to "
		"keep up with 8+ (x1.25 headroom). It was not tried with more than 8: run campy-encode-sweep with "
		"--streams up to 10.")]
	# At another frame rate: 629 Mpx/s is what it kept up with, not its limit
	ten = cameras([str(n) for n in range(10)], frameRate=62)[1]
	assert capacity(ten, SWEPT_ALL) == [(WARN, "libx264/veryfast: 10 cameras need 813 Mpx/s; the 1280x1024 "
		"60 fps measurement kept up with 629+ Mpx/s (estimate for 62 fps). It was not tried with more than 8 "
		"streams: run campy-encode-sweep with this config and --streams up to 10 to measure it.")]


def test_capacity_between_the_counts_tried():
	# 6 kept up and 8 did not: 7 was not tried, and 8 FAILs (test_capacity_fits_and_fails)
	[(status, text)] = capacity(cameras(list("1234567"))[1], TABLE["entries"][0])
	assert status == WARN and text.endswith(". It did not keep up with 8, and 7 was not tried: run "
		"campy-encode-sweep with --streams 7.")
	# At 62 fps: 7 cameras need more than 6 streams of the measurement, less than 8
	[(status, text)] = capacity(cameras(list("1234567"), frameRate=62)[1], TABLE["entries"][0])
	assert status == WARN and "need 569 Mpx/s; the 1280x1024 60 fps measurement kept up with 472 Mpx/s, and not "\
		"with 629 (estimate for 62 fps). It was not tried in between" in text
	[(status, text)] = capacity(cameras(list("12345678"), frameRate=62)[1], TABLE["entries"][0])
	assert status == FAIL and text.startswith("libx264/veryfast: 8 cameras need 650 Mpx/s")


def test_capacity_per_gpu():
	# 16 NVENC cameras, 8 on each of two GPUs, and a sweep that kept up with
	# 12 on one: each GPU has its own encoder
	entry = dict(TABLE["entries"][0], key="hevc_nvenc/fast", max_streams=12, max_tested=16)
	assert capacity(nvenc_cameras(16, gpuID=[0] * 8 + [1] * 8), entry) == [
		(OK, "hevc_nvenc/fast on GPU 0: 8 cameras at 1280x1024 60 fps; measured to keep up with 12 on one GPU "
			"(x1.25 headroom)"),
		(OK, "hevc_nvenc/fast on GPU 1: 8 cameras at 1280x1024 60 fps; measured to keep up with 12 on one GPU "
			"(x1.25 headroom)"),
		(WARN, "encoders were measured one at a time, and hevc_nvenc/fast on GPU 0 and hevc_nvenc/fast on GPU 1 "
			"share this computer's CPU (VA-API converts every frame on the CPU too). Check the mix with a test "
			"recording."),
		]
	# All 16 on one GPU: the sweep found 16 too slow
	[(status, text)] = capacity(nvenc_cameras(16), entry)
	assert status == FAIL and text.startswith("hevc_nvenc/fast: 16 cameras")

	# VA-API: one encoder per render node
	_, cams = cameras(list("12"), gpuID=0, gpuMake="vaapi", vaapiDevice=["/dev/dri/renderD128", "/dev/dri/renderD129"])
	found = capacity(cams, dict(entry, key="h264_vaapi"))
	assert [t.split(":")[0] for s, t in found if s == OK] == ["h264_vaapi on /dev/dri/renderD128",
		"h264_vaapi on /dev/dri/renderD129"]
	# Quick Sync takes the default GPU whatever gpuID says: one encoder
	_, cams = cameras(list("12"), gpuID=[0, 1], gpuMake="intel")
	assert capacity(cams, dict(entry, key="h264_qsv/veryfast")) == [(OK, "h264_qsv/veryfast: 2 cameras at "
		"1280x1024 60 fps; measured to keep up with 12 (x1.25 headroom)")]


def test_capacity_estimate_says_what_differs():
	entry = TABLE["entries"][0]
	# Only pixelFormatInput differs: rgb24 frames have 3 times the bytes of the
	# measured gray ones for ffmpeg to read, so 2 cameras come close
	assert capacity(cameras(list("12"), pixelFormatInput="rgb24")[1], entry) == [(WARN, "libx264/veryfast: "
		"2 cameras need 157 Mpx/s; the 1280x1024 60 fps gray measurement kept up with 472 Mpx/s, and not with "
		"629 (estimate for rgb24 input: 3 bytes a pixel, not 1). With 3 times the bytes a pixel for ffmpeg to "
		"read, that may be close to its limit.")]
	[(status, _)] = capacity(cameras(["1"], pixelFormatInput="rgb24")[1], entry)
	assert status == OK
	# Gray cameras against an rgb24 measurement: fewer bytes may keep up with more
	[(status, text)] = capacity(cameras(list("12345678"))[1], dict(entry, pix_fmt_in="rgb24"))
	assert status == WARN and "(estimate for gray input: 1 byte a pixel, not 3). With fewer bytes a pixel it "\
		"may keep up with more" in text
	# Frame size and rate
	[(_, text)] = capacity(cameras(list("12"), frameWidth=640, frameHeight=512, frameRate=30)[1], entry)
	assert text.endswith("(estimate for 640x512 frames at 30 fps)")
	[(_, text)] = capacity(cameras(list("12"), frameRate=[60, 30])[1], entry)
	assert text.endswith("(estimate for cameras of 2 frame shapes)")


def test_capacity_errored_sweep_is_not_measured():
	_, cams = cameras(list("12"), gpuID=0, gpuMake="vaapi")
	unmeasured = (WARN, "h264_vaapi was not measured (Camera0, Camera1): campy-encode-sweep stopped with an "
		"error on it, which t.json holds. Check that this encoder works here, then run campy-encode-sweep again.")
	# As sweeps saved it before (only error rows), and as they save it now
	assert capacity(cams, ERRORED) == [unmeasured]
	assert capacity(cams, dict(ERRORED, error="stream 0: OSError('[Errno 32] Broken pipe')")) == [unmeasured]
	# A measurement at another frame shape still gives an estimate
	[(status, text)] = capacity(cams, ERRORED, dict(TABLE["entries"][0], key="h264_vaapi", fps=30.0))
	assert status == OK and text.endswith("(estimate for 60 fps)")
	# and no recording size comes from it
	assert preflight.FindEntry(dict(TABLE, entries=[ERRORED]), cams[0]) is None


def missing_camera(serials):
	# numCams 4, cameraSelection 0 to 3, with these cameras connected
	p, _ = cameras(list("1234"))
	systems = {"flir": {"serials": serials}}
	return p, systems, preflight.CameraList(systems, p)


def test_missing_camera_fails():
	_, systems, cams = missing_camera(["1", "2", "3"])
	report = preflight.Report()
	preflight.CheckDevices(systems, cams, report)
	assert lines(report) == [(FAIL, "Camera3: no flir camera for cameraSelection 3; 3 were found (cameraSelection "
		"0 to 2). Check that it is connected, or change cameraSelection.")]

	_, systems, cams = missing_camera(["1"])
	report = preflight.Report()
	preflight.CheckDevices(systems, cams, report)
	assert [s for s, _ in lines(report)] == [FAIL] * 3
	assert lines(report)[0][1].endswith("; 1 was found (cameraSelection 0). Check that it is connected, or change "
		"cameraSelection.")

	_, systems, cams = missing_camera([])
	report = preflight.Report()
	preflight.CheckDevices(systems, cams, report)
	assert lines(report)[0] == (FAIL, "Camera0: no flir camera for cameraSelection 0; none were found. Check that "
		"it is connected.")

	# A make that does not list its devices is not checked
	report = preflight.Report()
	preflight.CheckDevices({}, cams, report)
	assert report.lines == []


def test_run_stops_on_a_missing_camera(tmp_path, monkeypatch):
	root = make_sysfs(tmp_path / "sys", [(CTRL_A, 4, 10000, "4-{}".format(n), FLIR, str(n), 5000) for n in (1, 2, 3)])
	monkeypatch.setattr(preflight, "CheckEncoders", lambda cams, report: None)
	p, systems, _ = missing_camera(["1", "2", "3"])
	p.update(capacityFile=str(tmp_path / "none.json"), videoFolder=str(tmp_path))
	with pytest.raises(preflight.PreflightError, match="1 check failed"):
		preflight.Run(systems, dict(p, preflight="error"), root)
	# The USB check leaves it to that FAIL: it has no serial number to look for
	report = preflight.Run(systems, dict(p, preflight="warn"), root)
	assert lines(report, "camera") == [(FAIL, "Camera3: no flir camera for cameraSelection 3; 3 were found "
		"(cameraSelection 0 to 2). Check that it is connected, or change cameraSelection.")]
	assert not any("Camera3" in t for _, t in lines(report, "usb"))


def test_run_reads_ffmpegs_encoders_once(tmp_path, monkeypatch):
	fake_ffmpeg(monkeypatch)
	monkeypatch.setattr(preflight, "FfmpegExe", lambda: "ffmpeg")
	asked = []
	monkeypatch.setattr(preflight, "FfmpegEncoders", lambda exe: asked.append(exe) or {"hevc_nvenc"})
	p, _ = cameras(["1"], gpuID=0, gpuMake="nvidia", codec="h265",
		capacityFile=str(tmp_path / "none.json"), videoFolder=str(tmp_path))
	report = preflight.Run({"flir": {"serials": ["1"]}}, dict(p, preflight="warn"), str(tmp_path / "no-sysfs"))
	assert asked == ["ffmpeg"]
	assert [s for s, _ in lines(report, "encoders")] == [OK, OK] # has hevc_nvenc; it encodes


def test_encoder_check():
	_, cams = cameras(list("12"), gpuID=[-1, 0], gpuMake="vaapi", vaapiDevice="/nonexistent/renderD128")
	report = preflight.Report()
	preflight.CheckEncoders(cams, report, exe="ffmpeg", encoders={"libx264"})
	found = lines(report)
	assert (OK, "libx264 for Camera0") in found
	assert any(s == FAIL and "has no h264_vaapi encoder" in t for s, t in found)
	assert any(s == FAIL and "/nonexistent/renderD128 does not exist" in t for s, t in found)


def nvenc_cameras(n=2, **values):
	settings = dict(gpuID=0, gpuMake="nvidia", codec="h265", quality=24, pixelFormatOutput="rgb0",
		preset="None")
	settings.update(values)
	return cameras([str(i) for i in range(1, n + 1)], **settings)[1]


def fake_ffmpeg(monkeypatch, returncode=0, stderr="", timeout=False):
	"""Replaces subprocess.run in preflight: records each command, answers like ffmpeg."""
	calls = []
	def run(cmd, **kwargs):
		calls.append((cmd, kwargs))
		if timeout:
			raise preflight.subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
		return types.SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)
	monkeypatch.setattr(preflight.subprocess, "run", run)
	return calls


# ffmpeg 4.2.2's error for every camera on a Windows PC with NVIDIA driver 596.71
FFMPEG_42_PRESET_ERROR = (
	"[hevc_nvenc @ 000002b8e1f0c2c0] Cannot get the preset configuration: unsupported param (12)\n"
	"Error initializing output stream 0:0 -- Error while opening encoder for output stream #0:0 - "
	"maybe incorrect parameters such as bit_rate, rate, width or height\n")

# An ffmpeg built with NVENC 13.1 headers there (wording from FFmpeg 9.0's nvenc.c)
FFMPEG_TOO_NEW = (
	"[hevc_nvenc @ 0000016f4c0d2a40] Driver does not support the required nvenc API version. "
	"Required: 13.1 Found: 13.0\n"
	"[hevc_nvenc @ 0000016f4c0d2a40] The minimum required Nvidia driver for nvenc is 610.00 or newer\n"
	"[vost#0:0/hevc_nvenc @ 0000016f4c0d4380] Error while opening encoder - maybe incorrect "
	"parameters such as bit_rate, rate, width or height.\n")


def test_nvenc_probe_command():
	# The writer's codec, pixel format and output options, at the size imageio-ffmpeg
	# pads frames to (1000x750 -> 1008x752), into a file of the video's own type
	cam = nvenc_cameras(1, frameWidth=1000, frameHeight=750, frameRate=60, videoFilename="0.mkv")[0]
	assert preflight.NvencProbeCommand("ffmpeg", cam) == [
		"ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-an",
		"-f", "lavfi", "-i", "testsrc2=size=1008x752:rate=60.00", "-frames:v", "10",
		"-vcodec", "hevc_nvenc", "-pix_fmt", "rgb0",
		"-preset", "fast", "-qp", "24", "-bf:v", "0", "-gpu", "0",
		"-y", "nvenc_test.mkv"]
	cmd = preflight.NvencProbeCommand("ffmpeg", dict(cam, keyframeInterval=0.5, gpuID=1, codec="h264"))
	assert cmd[cmd.index("-vcodec") + 1] == "h264_nvenc"
	assert cmd[cmd.index("-gpu") + 1] == "1" and cmd[cmd.index("-g") + 1] == "30"


def test_nvenc_probe_command_av1():
	# codec "av1" probes av1_nvenc, with AV1's quantizer index as -qp
	cam = nvenc_cameras(1, codec="av1", quality=88)[0]
	cmd = preflight.NvencProbeCommand("ffmpeg", cam)
	assert cmd[cmd.index("-vcodec") + 1] == "av1_nvenc"
	assert cmd[cmd.index("-qp") + 1] == "88"


def test_nvenc_probe_skipped_without_nvenc(monkeypatch):
	# No ffmpeg runs at all unless a camera encodes with NVENC: not for the CPU,
	# not for VA-API, and not for gpuMake "nvidia" with gpuID -1 (the CPU)
	def no_ffmpeg(*args, **kwargs):
		raise AssertionError("ran ffmpeg")
	monkeypatch.setattr(preflight.subprocess, "run", no_ffmpeg)
	monkeypatch.setattr(preflight, "FfmpegExe", no_ffmpeg)
	for values in (dict(gpuID=-1), dict(gpuID=-1, gpuMake="nvidia"), dict(gpuID=0, gpuMake="vaapi")):
		report = preflight.Report()
		preflight.CheckNvenc(cameras(list("12"), **values)[1], report)
		assert report.lines == [], values


def test_nvenc_probe_ok(monkeypatch):
	calls = fake_ffmpeg(monkeypatch)
	report = preflight.Report()
	preflight.CheckNvenc(nvenc_cameras(3, gpuID=[0, 0, 1]), report, exe="ffmpeg", encoders={"hevc_nvenc"})
	# One probe per GPU: cameras with the same settings share one
	assert [cmd[cmd.index("-gpu") + 1] for cmd, _ in calls] == ["0", "1"]
	assert all(kwargs["timeout"] == preflight.NVENC_PROBE_TIMEOUT and kwargs["cwd"] for _, kwargs in calls)
	assert lines(report) == [
		(OK, "hevc_nvenc on GPU 0 (Camera0, Camera1) encoded 10 test frames with campy's settings"),
		(OK, "hevc_nvenc on GPU 1 (Camera2) encoded 10 test frames with campy's settings"),
		]


def test_nvenc_probe_old_ffmpeg(monkeypatch):
	fake_ffmpeg(monkeypatch, returncode=1, stderr=FFMPEG_42_PRESET_ERROR)
	report = preflight.Report()
	preflight.CheckNvenc(nvenc_cameras(2), report, exe="ffmpeg", encoders={"hevc_nvenc"})
	[(status, text)] = lines(report, "encoders")
	assert status == FAIL
	first, *rest = text.splitlines()
	assert first == "hevc_nvenc on GPU 0 (Camera0, Camera1) could not encode test frames with campy's settings. ffmpeg said:"
	# ffmpeg's own words, then what they mean, then the command to try by hand
	assert rest[0] == "  [hevc_nvenc @ 000002b8e1f0c2c0] Cannot get the preset configuration: unsupported param (12)"
	assert rest[1].startswith("  Error initializing output stream 0:0")
	assert "ffmpeg 4.4 or newer" in rest[2] and "ffmpegPath" in rest[2] and "591.44" in rest[2]
	assert rest[3].startswith("To run it by hand: ffmpeg -hide_banner") and rest[3].endswith("-gpu 0 -y nvenc_test.mp4")
	# In the printed check, ffmpeg's lines sit under the finding
	printed = report.text("Launch check:").splitlines()
	assert printed[1].startswith("  FAIL  encoders  hevc_nvenc on GPU 0")
	assert printed[2] == " " * 18 + "  [hevc_nvenc @ 000002b8e1f0c2c0] Cannot get the preset configuration: unsupported param (12)"


def test_nvenc_probe_driver_too_old(monkeypatch):
	fake_ffmpeg(monkeypatch, returncode=1, stderr=FFMPEG_TOO_NEW)
	report = preflight.Report()
	preflight.CheckNvenc(nvenc_cameras(1), report, exe="ffmpeg", encoders={"hevc_nvenc"})
	[(status, text)] = lines(report)
	assert status == FAIL
	assert "  [hevc_nvenc @ 0000016f4c0d2a40] The minimum required Nvidia driver for nvenc is 610.00 or newer" in text.splitlines()
	assert "Update the NVIDIA driver" in text and "BtbN's 8.1 build" in text


@pytest.mark.parametrize("stderr, expected", [
	("[h264_nvenc @ 0x5581] Cannot load libnvidia-encode.so.1\n", "Is the NVIDIA driver installed?"),
	("[h264_nvenc @ 0x5581] Requested GPU 1, but only 1 GPUs are available!\n", "No GPU has this gpuID"),
	("[h264_nvenc @ 0x5581] No capable devices found\n", "support matrix"),
	("[h264_nvenc @ 0x5581] InitializeEncoder failed: invalid param (8)\n", "-v info instead of -v error"),
	("", "(nothing; exit code 1)"),
	])
def test_nvenc_probe_other_failures(monkeypatch, stderr, expected):
	fake_ffmpeg(monkeypatch, returncode=1, stderr=stderr)
	report = preflight.Report()
	preflight.CheckNvenc(nvenc_cameras(1, codec="h264"), report, exe="ffmpeg", encoders={"h264_nvenc"})
	[(status, text)] = lines(report)
	assert status == FAIL and expected in text and "To run it by hand" in text


def test_nvenc_probe_timeout(monkeypatch):
	fake_ffmpeg(monkeypatch, timeout=True)
	report = preflight.Report()
	preflight.CheckNvenc(nvenc_cameras(1), report, exe="ffmpeg", encoders={"hevc_nvenc"})
	[(status, text)] = lines(report)
	assert status == FAIL and "(did not finish in 60 s)" in text


def test_nvenc_probe_leaves_missing_encoders_to_encoder_check(monkeypatch):
	# An ffmpeg without the encoder, or one that would not run, FAILs in
	# CheckEncoders: the probe does not run it again or report it twice
	calls = fake_ffmpeg(monkeypatch)
	report = preflight.Report()
	preflight.CheckNvenc(nvenc_cameras(2), report, exe="ffmpeg", encoders={"libx264", "h264_nvenc"})
	assert calls == [] and report.lines == []
	monkeypatch.setattr(preflight, "FfmpegEncoders", lambda exe: None)
	preflight.CheckNvenc(nvenc_cameras(2), report, exe="ffmpeg")
	assert calls == [] and report.lines == []


def test_disk_estimate(tmp_path, monkeypatch):
	usage = types.SimpleNamespace(free=10e9)
	monkeypatch.setattr(preflight.shutil, "disk_usage", lambda path: usage)
	folder = str(tmp_path / "new" / "session") # does not exist yet
	# 4 cameras x 4 Mbit/s: 1 h is 7.2 GB, 4500 s is 9 GB, 6000 s is 12 GB of 10 GB free
	for seconds, status in ((3600, OK), (4500, WARN), (6000, FAIL)):
		p, cams = cameras(list("1234"), videoFolder=folder, recTimeInSec=seconds)
		report = preflight.Report()
		preflight.CheckDisk(cams, p, TABLE, report)
		assert lines(report)[0][0] == status, seconds

	report = preflight.Report()
	preflight.CheckDisk(cams, p, None, report)
	assert lines(report)[0][0] == OK and "not estimated" in lines(report)[0][1]


def test_run_modes(tmp_path, monkeypatch):
	root = make_sysfs(tmp_path, [(CTRL_A, 3, 480, "3-1", FLIR, "1", 480)]) # a USB 2 link
	monkeypatch.setattr(preflight, "CheckEncoders", lambda cams, report: None)
	p, _ = cameras(["1"], capacityFile=str(tmp_path / "none.json"), videoFolder=str(tmp_path))
	systems = {"flir": {"serials": ["1"]}}

	with pytest.raises(preflight.PreflightError, match="1 check failed"):
		preflight.Run(systems, dict(p, preflight="error"), root)
	report = preflight.Run(systems, dict(p, preflight="warn"), root)
	assert report.count(FAIL) == 1
	assert preflight.Run(systems, dict(p, preflight="off"), root) is None
	# Unquoted, a config's off is YAML's False: that ran the check in error mode
	assert preflight.Run(systems, dict(p, **yaml.safe_load("preflight: off")), root) is None


def test_run_stops_on_nvenc_failure(tmp_path, monkeypatch):
	fake_ffmpeg(monkeypatch, returncode=1, stderr=FFMPEG_42_PRESET_ERROR)
	monkeypatch.setattr(preflight, "FfmpegExe", lambda: "ffmpeg")
	monkeypatch.setattr(preflight, "FfmpegEncoders", lambda exe: {"hevc_nvenc"})
	p, _ = cameras(["1"], gpuID=0, gpuMake="nvidia", codec="h265",
		capacityFile=str(tmp_path / "none.json"), videoFolder=str(tmp_path))
	systems = {"flir": {"serials": ["1"]}}
	with pytest.raises(preflight.PreflightError, match="1 check failed"):
		preflight.Run(systems, dict(p, preflight="error"), str(tmp_path / "no-sysfs"))
	report = preflight.Run(systems, dict(p, preflight="warn"), str(tmp_path / "no-sysfs"))
	assert [s for s, _ in lines(report, "encoders")] == [OK, FAIL] # has hevc_nvenc; it fails


def test_ratcam_usb_links(tmp_path):
	# RatCams are USB 2 devices: the RP2040 build at full speed (about 1 MB/s),
	# the ESP32-P4 build at high speed. Their frames are mono, whatever the config says.
	RP2040, P4 = "2e8a", "303a"
	root = make_sysfs(tmp_path, [
		(CTRL_A, 3, 480, "3-1", RP2040, "E6614C311B000001", 12),
		(CTRL_A, 3, 480, "3-2", RP2040, "E6614C311B000002", 12),
		(CTRL_A, 3, 480, "3-3", P4, "60:55:F9:00:00:03", 480),
		(CTRL_A, 4, 10000, "4-1", FLIR, "11", 5000),
		])
	p = configurator.DefaultParams()
	p.update(numCams=4, cameraMake=["ratcam", "ratcam", "ratcam", "flir"], cameraSelection=[0, 1, 2, 0],
		frameWidth=[160, 320, 320, 1280], frameHeight=[120, 320, 320, 1024], frameRate=[30, 30, 30, 60],
		pixelFormatInput="rgb24")
	p = configurator.FillCameraLists(p)
	systems = {
		"ratcam": {"serials": ["E6614C311B000001", "E6614C311B000002", "60:55:F9:00:00:03"]},
		"flir": {"serials": ["11"]},
		}
	cams = preflight.CameraList(systems, p)
	assert [cam["pixelFormatInput"] for cam in cams] == ["gray", "gray", "gray", "rgb24"]
	report = preflight.Report()
	preflight.CheckUsb(cams, p, report, root)
	found = lines(report, "usb")
	assert (OK, "Camera0 is connected at 12 Mb/s, which carries about 1 MB/s; it needs 0.58 MB/s (58%)") in found
	assert any(s == FAIL and t.startswith("Camera1 is connected at 12 Mb/s") and "needs 3.07 MB/s" in t
		and "ESP32-P4" in t for s, t in found)
	assert any(s == OK and t.startswith("Camera2 is connected at 480 Mb/s") for s, t in found)
	# Their USB 2 bus is checked too; the FLIR's USB 3 bus on its own
	assert (OK, "bus 3 (0.48 Gb/s, controller {}): Camera0, Camera1, Camera2 send 6.7 of 35 MB/s (19%)"\
		.format(CTRL_A)) in found
	assert not any(s == FAIL for s, t in found if "Camera3" in t)


# ------------------------------------------------------------------ frame rate

# What the BFS-U3-13Y3M on the EVO-X2 reported with campy's settings
# (1280x1024 Mono8, 1500 us exposure): at its saved 380 MB/s, and after it had
# been left at 43 MB/s, when campy recorded it at 32.6 fps for a frameRate of 60
FULL_LINK = {"fps": 170.2, "frameBytes": 1310720, "throughputLimit": 380000000, "exposureUs": 1500,
	"frameRateCap": None}
SLOW_LINK = dict(FULL_LINK, fps=32.6, throughputLimit=43000000)


def frame_rate_report(limits, **values):
	_, cams = cameras(["24243505"], **values)
	report = preflight.Report()
	preflight.CheckFrameRate(cams, report, lambda cam: limits)
	return lines(report, "camera")


def test_frame_rate_fits():
	assert frame_rate_report(FULL_LINK) == [(OK, "Camera0 (24243505): up to 170.2 fps with campy's settings; frameRate 60")]
	# Within 20% of the most it can do
	[(status, text)] = frame_rate_report(dict(FULL_LINK, fps=70))
	assert status == WARN and text.endswith("frameRate 60 is close to that")


def test_frame_rate_names_the_usb_limit():
	[(status, text)] = frame_rate_report(SLOW_LINK)
	assert status == FAIL
	assert text.startswith("Camera0 (24243505) can run at only 32.6 fps with campy's settings, below frameRate 60: "
		"its USB link is limited to 43 MB/s (DeviceLinkThroughputLimit), and 1.3 MB frames at 60 fps need 79 MB/s.")


def test_frame_rate_names_a_long_exposure():
	[(status, text)] = frame_rate_report(dict(FULL_LINK, fps=49.75, exposureUs=19998))
	assert status == FAIL
	assert text.endswith("its exposure, 19998 us (cameraExposureTimeInUs), is longer than a frame at 60 fps (16667 us).")


def test_frame_rate_names_a_frame_rate_cap():
	[(_, text)] = frame_rate_report(dict(FULL_LINK, fps=30, frameRateCap=30))
	assert text.endswith("AcquisitionFrameRate caps it at 30.0 fps (AcquisitionFrameRateEnable is on).")


def test_frame_rate_names_the_tightest_limit():
	# Both the link (32.8 fps) and the exposure (50 fps) are too slow: the link says why it is 32.6
	[(_, text)] = frame_rate_report(dict(SLOW_LINK, exposureUs=19998))
	assert "DeviceLinkThroughputLimit" in text and "cameraExposureTimeInUs" not in text


def test_frame_rate_without_a_known_cause():
	[(_, text)] = frame_rate_report(dict(FULL_LINK, fps=50))
	assert text.endswith("its sensor reads out 1280x1024 frames no faster.")


def test_frame_rate_unreadable():
	assert frame_rate_report(None) == [] # a make that cannot say
	[(status, text)] = frame_rate_report(dict(FULL_LINK, fps=None))
	assert status == WARN and "could not read" in text
	_, cams = cameras(["1"])
	report = preflight.Report()

	def broken(cam):
		raise RuntimeError("Spinnaker: camera busy")

	preflight.CheckFrameRate(cams, report, broken)
	assert lines(report, "camera") == [(WARN, "Camera0: could not read its frame rate limits (Spinnaker: camera busy)")]


def test_camera_limits_opens_the_selected_device(monkeypatch):
	opened = []

	class Make:
		@staticmethod
		def FrameRateLimits(device, cam):
			opened.append((device, cam["cameraName"]))
			return FULL_LINK

	monkeypatch.setattr(preflight.unicam, "ImportCam", lambda make: Make)
	_, cams = cameras(["a", "b"], cameraSelection=[1, 0])
	systems = {"flir": {"serials": ["a", "b"], "deviceList": ["device a", "device b"]}}
	assert [preflight.CameraLimits(systems, cam) for cam in cams] == [FULL_LINK, FULL_LINK]
	assert opened == [("device b", "Camera0"), ("device a", "Camera1")]
	# Nothing to open: no device list (as in the other tests here), or no such camera
	assert preflight.CameraLimits({"flir": {"serials": ["a"]}}, cams[0]) is None
	_, missing = cameras(["a"], cameraSelection=[3])
	assert preflight.CameraLimits(systems, missing[0]) is None


def test_run_stops_on_a_slow_camera(tmp_path, monkeypatch):
	monkeypatch.setattr(preflight, "CheckEncoders", lambda cams, report: None)
	monkeypatch.setattr(preflight, "CameraLimits", lambda systems, cam: SLOW_LINK)
	p, _ = cameras(["24243505"], capacityFile=str(tmp_path / "none.json"), videoFolder=str(tmp_path))
	systems = {"flir": {"serials": ["24243505"]}}
	with pytest.raises(preflight.PreflightError, match="1 check failed"):
		preflight.Run(systems, dict(p, preflight="error"), str(tmp_path / "no-sysfs"))


# ------------------------------------------------------------------ frame size

# The same camera, as the launch check read it: its frames, frameWidth x
# frameHeight, its full frame, and whether its frame size can be changed now
RIGHT_SIZE = dict(FULL_LINK, frameSize=(1280, 1024), wantedFrameSize=(1280, 1024), maxFrameSize=(1280, 1024),
	frameSizeLocked=False)
# Left at 640x512 by an earlier run, and still acquiring from it
STUCK = dict(RIGHT_SIZE, frameSize=(640, 512), frameSizeLocked=True)
OK_LINE = (OK, "Camera0 (24243505): up to 170.2 fps with campy's settings; frameRate 60")


def test_frame_size_fits():
	assert frame_rate_report(RIGHT_SIZE) == [OK_LINE]
	# A make or camera that cannot say
	assert frame_rate_report(dict(RIGHT_SIZE, frameSize=None)) == [OK_LINE]


def test_frame_size_locked_by_an_earlier_run():
	assert frame_rate_report(STUCK) == [OK_LINE, (FAIL,
		"Camera0 (24243505) would send 640x512 frames, not frameWidth x frameHeight 1280x1024, so its video would be "
		"scrambled. Either:\n"
		"  - unplug it and plug it back in: its frame size cannot be changed now\n"
		"  - set frameWidth: 640 and frameHeight: 512, the frames it sends now")]


def test_frame_size_refused_for_another_reason():
	[_, (status, text)] = frame_rate_report(dict(STUCK, frameSizeLocked=False))
	assert status == FAIL and "  - unplug it and plug it back in\n" in text and "cannot be changed" not in text


def test_frame_size_larger_than_the_sensor():
	# frameWidth 1440 x frameHeight 1080 on a 1280x1024 camera, which kept its full frame
	[_, (status, text)] = frame_rate_report(dict(RIGHT_SIZE, wantedFrameSize=(1440, 1088)))
	assert status == FAIL and text == (
		"Camera0 (24243505) would send 1280x1024 frames, not frameWidth x frameHeight 1440x1088, so its video would be "
		"scrambled. Either:\n"
		"  - set frameWidth and frameHeight to 1280x1024 (its full frame) or less")
	# and the frames it sends now, when they are not that
	[_, (_, text)] = frame_rate_report(dict(STUCK, wantedFrameSize=(1440, 1088), frameSizeLocked=False))
	assert text.endswith("  - set frameWidth and frameHeight to 1280x1024 (its full frame) or less\n"
		"  - set frameWidth: 640 and frameHeight: 512, the frames it sends now")


def test_frame_size_report_text():
	# The options go under the finding, indented with its other lines
	report = preflight.Report()
	status, text = preflight.FrameSizeFinding(cameras(["24243505"])[1][0], STUCK)
	report.add(status, "camera", text)
	assert report.text("Launch check:").splitlines()[1:] == [
		"  FAIL  camera    Camera0 (24243505) would send 640x512 frames, not frameWidth x frameHeight 1280x1024, "
		"so its video would be scrambled. Either:",
		"                    - unplug it and plug it back in: its frame size cannot be changed now",
		"                    - set frameWidth: 640 and frameHeight: 512, the frames it sends now"]


def test_run_stops_on_a_frame_size_it_cannot_set(tmp_path, monkeypatch, capsys):
	monkeypatch.setattr(preflight, "CheckEncoders", lambda cams, report: None)
	monkeypatch.setattr(preflight, "CameraLimits", lambda systems, cam: STUCK)
	p, _ = cameras(["24243505"], capacityFile=str(tmp_path / "none.json"), videoFolder=str(tmp_path))
	systems = {"flir": {"serials": ["24243505"]}}
	with pytest.raises(preflight.PreflightError, match="1 check failed"):
		preflight.Run(systems, dict(p, preflight="error"), str(tmp_path / "no-sysfs"))
	assert "set frameWidth: 640 and frameHeight: 512" in capsys.readouterr().out


# ------------------------------------------------------------------ GigE search

GIGE_WARN = (WARN, "every FLIR camera here is USB3 (Camera0), but Spinnaker searches each network interface for "
	"GigE cameras as it starts, here and in each camera process: about 0.25 s per interface. Without the search, "
	"campy was ready to trigger in 1.0 s instead of 2.9 on the EVO-X2. To turn it off for this user: "
	"campy-flir-reset --gige off")


def gige_report(found, serials=("24243505",), **values):
	_, cams = cameras(list(serials), **values)
	report = preflight.Report()
	preflight.CheckGige(cams, report, lambda: found)
	return lines(report)


@pytest.fixture
def gige_on_linux(monkeypatch):
	# The GigE lines name whom Spinnaker keeps the setting for: the user on Linux
	monkeypatch.setattr(preflight.sys, "platform", "linux")


def test_gige_scope_on_windows(monkeypatch):
	# Windows keeps it for the whole computer
	monkeypatch.setattr(preflight.sys, "platform", "win32")
	assert gige_report((True, ["USB3Vision"]))[0][1].endswith(
		"To turn it off for this computer: campy-flir-reset --gige off")
	_, _, cams = missing_camera(["1", "2", "3"])
	report = preflight.Report()
	preflight.CheckGige(cams, report, lambda: (False, ["USB3Vision"] * 3))
	assert lines(report)[0][1].startswith("Spinnaker's search for GigE cameras is off for this computer, so")


def test_gige_search_for_usb3_cameras(gige_on_linux):
	# As the EVO-X2 was until 2026-10-18: the search on, one Blackfly S on USB3
	assert gige_report((True, ["USB3Vision"])) == [GIGE_WARN]


def test_gige_search_that_is_needed(gige_on_linux):
	# A GigE camera, or one Spinnaker cannot say: the search stays
	assert gige_report((True, ["USB3Vision", "GigEVision"]), ["1", "2"]) == []
	assert gige_report((True, [None])) == []
	# A camera that was not found may be a GigE camera
	_, _, cams = missing_camera(["1", "2", "3"])
	report = preflight.Report()
	preflight.CheckGige(cams, report, lambda: (True, ["USB3Vision"] * 3))
	assert report.lines == []


def test_gige_search_off(gige_on_linux):
	assert gige_report((False, ["USB3Vision"])) == [
		(OK, "Spinnaker does not search for GigE cameras (campy-flir-reset --gige off)")]
	# Then a camera that was not found may be a GigE camera it did not look for
	_, _, cams = missing_camera(["1", "2", "3"])
	report = preflight.Report()
	preflight.CheckGige(cams, report, lambda: (False, ["USB3Vision"] * 3))
	assert lines(report) == [(WARN, "Spinnaker's search for GigE cameras is off for this user, so it finds no GigE "
		"camera. If Camera3 is one, turn the search on: campy-flir-reset --gige on.")]


def test_gige_not_checked(gige_on_linux):
	# No FLIR system, a Spinnaker without the setting, no FLIR camera in the config
	assert gige_report(None) == []
	assert gige_report((None, ["USB3Vision"])) == []
	assert gige_report((True, ["USB3Vision"]), cameraMake="basler") == []
	# Spinnaker would not say: the launch check goes on
	report = preflight.Report()

	def broken():
		raise RuntimeError("Spinnaker: node map not readable")

	preflight.CheckGige(cameras(["1"])[1], report, broken)
	assert lines(report) == [(WARN, "could not read whether Spinnaker searches for GigE cameras (Spinnaker: node "
		"map not readable)")]


def test_flir_discovery(monkeypatch):
	class Make:
		@staticmethod
		def GigeDiscovery(system):
			return system == "system with the search on"

		@staticmethod
		def DeviceType(device):
			return {"device a": "USB3Vision", "device b": "GigEVision"}[device]

	monkeypatch.setattr(preflight.unicam, "ImportCam", lambda make: Make)
	systems = {"flir": {"system": "system with the search on", "serials": ["a", "b"],
		"deviceList": ["device a", "device b"]}}
	assert preflight.FlirDiscovery(systems) == (True, ["USB3Vision", "GigEVision"])
	# Nothing to ask: no FLIR system, or no device list (as in the other tests here)
	assert preflight.FlirDiscovery({}) is None
	assert preflight.FlirDiscovery({"flir": {"serials": ["a"]}}) is None


def test_run_warns_of_the_gige_search(gige_on_linux, tmp_path, monkeypatch):
	monkeypatch.setattr(preflight, "CheckEncoders", lambda cams, report: None)
	monkeypatch.setattr(preflight, "FlirDiscovery", lambda systems: (True, ["USB3Vision"]))
	p, _ = cameras(["24243505"], capacityFile=str(tmp_path / "none.json"), videoFolder=str(tmp_path))
	systems = {"flir": {"serials": ["24243505"]}}
	# A WARN: it does not stop campy
	report = preflight.Run(systems, dict(p, preflight="error"), str(tmp_path / "no-sysfs"))
	assert lines(report, "gige") == [GIGE_WARN]


class Value:
	def __init__(self, value):
		self.value = value

	def GetValue(self):
		return self.value

	def GetCurrentEntry(self):
		return self

	def GetSymbolic(self):
		return self.value


class NodeMap(dict):
	def GetNode(self, name):
		return self.get(name)


@pytest.fixture
def flir(monkeypatch):
	spin = types.ModuleType("PySpin")
	for kind in ("CBooleanPtr", "CEnumerationPtr"):
		setattr(spin, kind, lambda node: node)
	spin.IsAvailable = lambda node: node is not None
	spin.IsReadable = lambda node: node is not None
	monkeypatch.setitem(sys.modules, "PySpin", spin)
	monkeypatch.delitem(sys.modules, "campy.cameras.flir", raising=False)
	yield importlib.import_module("campy.cameras.flir")
	sys.modules.pop("campy.cameras.flir", None)


def test_flir_reads_the_search_and_the_connection(flir):
	system = lambda nodes: types.SimpleNamespace(GetTLNodeMap=lambda: NodeMap(nodes))
	assert flir.GigeDiscovery(system({"EnumerateGEVInterfaces": Value(True)})) is True
	assert flir.GigeDiscovery(system({"EnumerateGEVInterfaces": Value(False)})) is False
	assert flir.GigeDiscovery(system({})) is None # before Spinnaker 3.2
	device = lambda nodes: types.SimpleNamespace(GetTLDeviceNodeMap=lambda: NodeMap(nodes))
	assert flir.DeviceType(device({"DeviceType": Value("USB3Vision")})) == "USB3Vision"
	assert flir.DeviceType(device({})) is None
