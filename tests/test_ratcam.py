"""RatCam (nir-eye-cam) cameras, against the nireyecam package's simulated camera."""

import csv
import os
import subprocess
import sys
import textwrap
import time
import tracemalloc
import types
from collections import deque

import numpy as np
import pytest

pytest.importorskip("nireyecam")

from campy import configurator, writer
from campy.cameras import ratcam, unicam


def cam_params(tmp_path, **values):
	p = configurator.DefaultParams()
	p.update(numCams=1, cameraMake="ratcam", ratcamPorts=["sim"], videoFolder=str(tmp_path),
		frameWidth=160, frameHeight=120, frameRate=20, cameraExposureTimeInUs=2000,
		recTimeInSec=1, chunkLengthInSec=1, displayFrameRate=0)
	p.update(values)
	p = configurator.FillCameraLists(p)
	systems = unicam.GetDeviceList(unicam.LoadSystems(p), p)
	return configurator.ConfigureCamParams(systems, p, 0)


def record(tmp_path, **values):
	p = cam_params(tmp_path, **values)
	(tmp_path / "Camera0").mkdir()
	writeQueue = deque() # what the encoder would get
	unicam.GrabFrames(p, writeQueue, deque([], 2), deque([], 1), deque([], 1))
	folder = tmp_path / "Camera0"
	with open(folder / "metadata.csv") as f:
		meta = dict(csv.reader(f))
	with open(folder / "ratcam_frames.csv") as f:
		frames = list(csv.DictReader(f))
	with open(folder / "ratcam_sync.csv") as f:
		sync = list(csv.DictReader(f))
	return writeQueue, np.load(folder / "frametimes.npy"), meta, frames, sync


def test_device_list():
	devices = ratcam.LoadSystem({"ratcamPorts": ["sim", "sim:7", "/dev/no-such-port"]})
	assert devices == [
		{"port": "sim:0", "serial": "sim0"},
		{"port": "sim:7", "serial": "sim7"},
		{"port": "/dev/no-such-port", "serial": "/dev/no-such-port"},
		]


def test_mono_whatever_the_config_says(tmp_path):
	# The writer opens with this before the camera does
	assert cam_params(tmp_path, pixelFormatInput="rgb24")["pixelFormatInput"] == "gray"


def test_records_at_the_configured_rate_on_the_cameras_clock(tmp_path):
	writeQueue, frametimes, meta, frames, sync = record(tmp_path)

	assert len(writeQueue) == 20 # 1 s at 20 fps
	assert writeQueue[0].shape == (120, 160) and writeQueue[0].dtype == np.uint8
	assert list(frametimes[0]) == list(range(1, 21))
	# The camera's own timestamps: 50 ms apart, to its crystal's few-ppm error
	assert np.allclose(np.diff(frametimes[1]), 0.05, rtol=1e-3)

	# The simulated sensor runs 30 fps at 128 lines, so 20 fps is 192 lines
	assert meta["ratcamFrameLength"] == "192"
	assert abs(float(meta["ratcamFrameRate"]) - 20) < 0.01
	assert abs(float(meta["ratcamExposureUs"]) - 2000) < 150 # a whole number of 260 us lines
	assert meta["ratcamFramesLost"] == "0"
	assert meta["cameraModel"] == "sim hm01b0" # board, sensor

	assert [int(r["frame"]) for r in frames] == list(range(1, 21))
	counters = [int(r["camera_frame"]) for r in frames]
	assert counters == list(range(counters[0], counters[0] + 20))
	assert frametimes[1][-1] == pytest.approx((int(frames[-1]["t_us"]) - int(frames[0]["t_us"])) / 1e6)
	assert sync == [] # nothing drove SYNC


def test_sync_pulses_are_kept(tmp_path):
	_, _, meta, _, sync = record(tmp_path, ratcamSyncOut=True, recTimeInSec=1.5)
	assert len(sync) >= 2 and meta["ratcamSyncPulses"] == str(len(sync))
	t = [int(row["t_us"]) for row in sync]
	assert all(400_000 <= b - a <= 1_000_000 for a, b in zip(t, t[1:])) # the random 0.4-1.0 s train


def test_led_on_while_open_and_off_after(tmp_path):
	p = cam_params(tmp_path, ratcamLed=300)
	camera, p = ratcam.OpenCamera(p)
	device = camera.cam._t.device # the simulated firmware
	assert device.led == 300 and not device.ae
	ratcam.CloseCamera(p, camera)
	assert device.led == 0


def test_frames_are_not_rescaled(tmp_path):
	# 120 is not a multiple of 16: imageio-ffmpeg would stretch it to 128
	assert writer.EncoderArgs(cam_params(tmp_path))["macro_block_size"] == 2
	assert writer.EncoderArgs(cam_params(tmp_path, cameraMake="emu"))["macro_block_size"] == 16


def test_a_rate_the_sensor_cannot_reach(tmp_path):
	with pytest.raises(ratcam.RatCamError, match="160x120 runs at up to 30.0 fps on this camera; frameRate is 100"):
		ratcam.OpenCamera(cam_params(tmp_path, frameRate=100))


def test_a_resolution_the_firmware_does_not_have(tmp_path):
	with pytest.raises(Exception, match="supported: 160x120 320x240 320x320"):
		ratcam.OpenCamera(cam_params(tmp_path, frameWidth=640, frameHeight=480))


# ------------------------------------------------------------------ the logs, written while recording

def made_up_camera(n, seed=0):
	"""
	What a RatCam delivers, in order: ("frame" | "pulse", counter, t_us,
	host_ns). Its frame counter skips now and then (frames lost on USB), and
	a few pulses come late, after later ones, some at the same time as another.
	"""
	rng = np.random.default_rng(seed)
	events, counter, pulse, t = [], 100, 0, 5_000_000
	for k in range(n):
		t += 8333
		counter += 1 + (rng.random() < 0.02) * int(rng.integers(1, 4))
		events.append(("frame", counter, t, 9_000_000_000 + t * 1000 + k % 5))
		if rng.random() < 0.1:
			pulse += 1
			late = int(rng.integers(0, 200_000)) if rng.random() < 0.2 else 0
			events.append(("pulse", pulse, t - 3000 - late, 9_000_000_000 + t * 1000))
		if rng.random() < 0.01: # two pulses at one time
			pulse += 1
			events.append(("pulse", pulse, t - 3000, 9_000_000_000 + t * 1000 + 1))
	return events


def old_logs(folder, events):
	# How SaveLogs wrote them before, from every row kept in memory until the end
	frames = [e[1:] for e in events if e[0] == "frame"]
	sync = [e[1:] for e in events if e[0] == "pulse"]
	os.makedirs(folder, exist_ok=True)
	with open(os.path.join(folder, "ratcam_frames.csv"), "w", newline="") as f:
		w = csv.writer(f)
		w.writerow(["frame", "camera_frame", "t_us", "host_ns"])
		for n, (counter, t_us, host_ns) in enumerate(frames, start=1):
			w.writerow([n, counter, t_us, host_ns])
	with open(os.path.join(folder, "ratcam_sync.csv"), "w", newline="") as f:
		w = csv.writer(f)
		w.writerow(["pulse", "t_us", "host_ns"])
		for counter, t_us, host_ns in sorted(sync, key=lambda s: s[1]):
			w.writerow([counter, t_us, host_ns])
	counters = [counter for counter, _, _ in frames]
	return sum(max(b - a - 1, 0) for a, b in zip(counters, counters[1:])), len(sync)


def new_logs(folder, events, before_open=0):
	# The events through Logs, the first before_open of them before it opens
	# (sync pulses while the camera is configured), writing after every event
	logs = ratcam.Logs(folder)
	for k, (kind, counter, t_us, host_ns) in enumerate(events):
		if k == before_open:
			logs.Open()
		(logs.Frame if kind == "frame" else logs.Pulse)(counter, t_us, host_ns)
		logs.WriteIfDue()
	logs.Close()
	return logs


def file_bytes(folder, name):
	with open(os.path.join(folder, name), "rb") as f:
		return f.read()


def test_the_logs_are_the_same_files_as_before(tmp_path, monkeypatch):
	monkeypatch.setattr(ratcam, "WRITE_EVERY_NS", 0) # a write after every row
	events = made_up_camera(3000)
	early = [("pulse", -2, 4_000_000, 1), ("pulse", -1, 4_500_000, 2)]
	t = [e[2] for e in events if e[0] == "pulse"]
	assert sum(b < a for a, b in zip(t, t[1:])) > 5 and len(set(t)) < len(t) # some out of order, some at one time
	lost, pulses = old_logs(str(tmp_path / "old"), early + events)
	logs = new_logs(str(tmp_path / "new"), early + events, before_open=len(early))
	for name in ("ratcam_frames.csv", "ratcam_sync.csv"):
		assert file_bytes(tmp_path / "new", name) == file_bytes(tmp_path / "old", name)
	assert lost > 0 and (logs.lost, logs.sync.count) == (lost, pulses)
	assert not os.path.exists(tmp_path / "new" / "ratcam_sync.csv.sorting")


def test_pulses_in_order_are_not_rewritten(tmp_path, monkeypatch):
	def no(path):
		raise AssertionError("rewrote " + path)
	monkeypatch.setattr(ratcam, "SortByTime", no)
	events = [e for e in made_up_camera(2000) if e[0] == "frame"]
	events += [("pulse", k, 9_000_000 + 1000 * k, 7) for k in range(1, 50)]
	lost, pulses = old_logs(str(tmp_path / "old"), events)
	logs = new_logs(str(tmp_path / "new"), events)
	for name in ("ratcam_frames.csv", "ratcam_sync.csv"):
		assert file_bytes(tmp_path / "new", name) == file_bytes(tmp_path / "old", name)
	assert (logs.lost, logs.sync.count) == (lost, pulses)


def test_memory_holds_about_a_second_of_rows(tmp_path, monkeypatch):
	# Ten minutes at 120 fps, with a pulse every 1/60 s, on a made-up clock
	now = [0]
	monkeypatch.setattr(ratcam.clock, "HostNs", lambda: now[0])
	logs = ratcam.Logs(str(tmp_path))
	logs.Open()
	tracemalloc.start()
	for n in range(1, 120 * 600 + 1):
		now[0] = n * 1_000_000_000 // 120
		logs.WriteIfDue()
		logs.Frame(n + 7, 1000 + n * 8333, now[0])
		if n % 2 == 0:
			logs.Pulse(n // 2, 1000 + n * 8333 - 50, now[0] + 1)
		assert len(logs.frames.rows) <= 120 and len(logs.sync.rows) <= 60
	peak = tracemalloc.get_traced_memory()[1]
	tracemalloc.stop()
	logs.Close()
	assert peak < 1_000_000 # kept in lists until the end, as before: 19 MB
	with open(tmp_path / "ratcam_frames.csv") as f:
		assert sum(1 for _ in f) == 1 + 120 * 600


def grab(camera, frames):
	# As GrabFrames does: GrabFrame until it has the frames, past its timeouts
	n = 0
	while n < frames:
		try:
			ratcam.GrabFrame(camera, n)
			n += 1
		except ratcam.RatCamError:
			pass


def test_the_logs_open_when_grabbing_starts(tmp_path):
	p = cam_params(tmp_path, ratcamSyncOut=True)
	camera, p = ratcam.OpenCamera(p)
	folder = tmp_path / "Camera0"
	try:
		assert not (folder / "ratcam_frames.csv").exists()
		time.sleep(1.2) # pulses come before grabbing starts (at least one in 1 s)
		assert ratcam.StartGrabbing(camera)
		with open(folder / "ratcam_sync.csv") as f:
			early = list(csv.DictReader(f))
		with open(folder / "ratcam_frames.csv") as f:
			assert list(csv.DictReader(f)) == []
		assert len(early) >= 1
		grab(camera, 30) # 1.5 s at 20 fps: a write after a second
		with open(folder / "ratcam_frames.csv") as f:
			assert 15 <= len(list(csv.DictReader(f))) < 30 # on disk while it records
	finally:
		ratcam.CloseCamera(p, camera)
	with open(folder / "ratcam_frames.csv") as f:
		assert [int(r["frame"]) for r in csv.DictReader(f)] == list(range(1, 31))
	with open(folder / "ratcam_sync.csv") as f:
		sync = list(csv.DictReader(f))
	assert sync[:len(early)] == early and p["ratcamSyncPulses"] == len(sync)


def test_a_camera_closed_before_it_grabbed_still_has_its_logs(tmp_path):
	p = cam_params(tmp_path)
	camera, p = ratcam.OpenCamera(p)
	ratcam.CloseCamera(p, camera)
	for name, header in (("ratcam_frames.csv", b"frame,camera_frame,t_us,host_ns\r\n"),
			("ratcam_sync.csv", b"pulse,t_us,host_ns\r\n")):
		assert file_bytes(tmp_path / "Camera0", name) == header
	assert p["ratcamFramesLost"] == 0 and p["ratcamSyncPulses"] == 0


def test_a_recording_that_dies_keeps_its_rows_up_to_the_last_second(tmp_path):
	# campy's camera process, ended by os._exit 50 frames (2.5 s) in: no
	# CloseCamera, no Python cleanup, as a crash would end it. It ends once
	# frametimes.npy has its second batch (frames 21-40): its writer thread
	# writes on its own, and on a slow disk (a Windows runner) had not
	# written it 0.5 s after it was handed over.
	script = textwrap.dedent("""
		import os, sys, time
		import numpy as np
		from collections import deque
		from campy import configurator
		from campy.cameras import ratcam, unicam
		def Written(path, frames, timeout=30):
			deadline = time.monotonic() + timeout
			while time.monotonic() < deadline:
				try:
					if np.load(path, mmap_mode="r").shape[1] >= frames:
						return
				except Exception:
					pass # not there yet, or its header being rewritten
				time.sleep(0.02)
		p = configurator.DefaultParams()
		p.update(numCams=1, cameraMake="ratcam", ratcamPorts=["sim"], videoFolder=sys.argv[1],
			frameWidth=160, frameHeight=120, frameRate=20, cameraExposureTimeInUs=2000,
			recTimeInSec=10, chunkLengthInSec=1, displayFrameRate=0, ratcamSyncOut=True)
		p = configurator.FillCameraLists(p)
		p = configurator.ConfigureCamParams(unicam.GetDeviceList(unicam.LoadSystems(p), p), p, 0)
		grab = ratcam.GrabFrame
		def GrabFrame(camera, n):
			if n >= 50:
				Written(os.path.join(sys.argv[1], "Camera0", "frametimes.npy"), 40)
				print(camera.logs.frames.count, camera.logs.sync.count, flush=True)
				os._exit(3)
			return grab(camera, n)
		ratcam.GrabFrame = GrabFrame
		unicam.GrabFrames(p, deque(), deque([], 2), deque([], 1), deque([], 1))
		""")
	done = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=60)
	assert done.returncode == 3, done.stderr
	grabbed, pulses = map(int, done.stdout.split()[-2:])
	assert grabbed == 50

	folder = tmp_path / "Camera0"
	with open(folder / "ratcam_frames.csv", newline="") as f:
		text = f.read()
	assert text.endswith("\r\n") # whole rows
	frames = list(csv.DictReader(text.splitlines()))
	# Every row up to the last write, about a second (20 frames) before the end
	assert 25 <= len(frames) < 50
	assert [int(r["frame"]) for r in frames] == list(range(1, len(frames) + 1))
	counters = [int(r["camera_frame"]) for r in frames]
	assert counters == list(range(counters[0], counters[0] + len(frames)))
	with open(folder / "ratcam_sync.csv") as f:
		sync = list(csv.DictReader(f))
	t = [int(r["t_us"]) for r in sync]
	assert 1 <= len(sync) <= pulses and t == sorted(t)
	# The other files, from campy/frametimes.py: the same frames, up to its own last write
	assert list(np.load(folder / "frametimes.npy")[0]) == list(range(1, 41)) # 2 batches of 20
	assert not (folder / "frametimes.mat").exists()
