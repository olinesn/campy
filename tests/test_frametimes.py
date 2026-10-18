"""frametimes.npy and hosttimes.npy, written while the recording runs (campy/frametimes.py)."""

import csv
import os
import subprocess
import sys
import textwrap
import types
from collections import deque

import numpy as np
import pytest
from scipy import io as sio

from campy import configurator, frametimes
from campy.cameras import unicam


def camera_times(n, drop=()):
	# What a camera gives: frame numbers with gaps where frames were dropped,
	# timestamps in seconds on its own clock, arrival times in ns
	numbers = [k for k in range(1, n + 1) if k not in drop]
	stamps = [1726543.210987654 + k * 0.0125 + 1e-7 * np.sin(k) for k in numbers]
	hosts = [123456789012345 + k * 12_500_000 + k % 7 for k in numbers]
	return numbers, stamps, hosts


def old_arrays(numbers, stamps, hosts):
	# How SaveMetadata made them before
	return np.array([numbers, [t - stamps[0] for t in stamps]]), np.array(hosts, dtype=np.int64)


def test_growing_npy_is_a_valid_file_after_every_append(tmp_path):
	one = frametimes.GrowingNpy(tmp_path / "one.npy", np.int64)
	two = frametimes.GrowingNpy(tmp_path / "two.npy", np.float64, rows=2)
	assert np.load(tmp_path / "one.npy").shape == (0,) and np.load(tmp_path / "two.npy").shape == (2, 0)
	expect_one, expect_two = [], []
	for batch in range(1, 5):
		values = list(range(10 * batch, 10 * batch + batch))
		one.Append(values)
		two.Append([(v, v / 3) for v in values])
		expect_one += values
		expect_two += [(v, v / 3) for v in values]
		assert np.array_equal(np.load(tmp_path / "one.npy"), expect_one)
		assert np.array_equal(np.load(tmp_path / "two.npy"), np.array(expect_two).T)
		# The header never changes size: the frames always start at byte 128
		assert os.path.getsize(tmp_path / "two.npy") == 128 + 16 * len(expect_two)
	one.Close()
	two.Close()


def test_same_arrays_as_before(tmp_path):
	numbers, stamps, hosts = camera_times(1000, drop=(17, 18, 400))
	times = frametimes.FrameTimes(str(tmp_path), 80)
	for n, t, h in zip(numbers, stamps, hosts):
		times.Add(n, t, h)
	times.Close()
	old_frames, old_hosts = old_arrays(numbers, stamps, hosts)
	frames, hosts_now = np.load(tmp_path / "frametimes.npy"), np.load(tmp_path / "hosttimes.npy")
	# The same values, bit for bit, in the same shape and type
	assert frames.shape == old_frames.shape and frames.dtype == old_frames.dtype
	assert np.array_equal(frames, old_frames) and np.array_equal(hosts_now, old_hosts) and hosts_now.dtype == np.int64
	assert times.count == len(numbers) and times.last == old_frames[1][-1]


def test_memory_holds_only_the_batch_not_yet_written(tmp_path):
	times = frametimes.FrameTimes(str(tmp_path), 80)
	numbers, stamps, hosts = camera_times(1000)
	for n, t, h in zip(numbers, stamps, hosts):
		times.Add(n, t, h)
		assert len(times.hosts) < 80 and len(times.frames) < 80
	assert np.load(tmp_path / "frametimes.npy").shape == (2, 960) # 12 batches of 80
	times.Close()


def test_a_process_that_dies_keeps_the_frames_it_wrote(tmp_path):
	# No Close, no Python cleanup: os._exit, as a crash would end it
	script = textwrap.dedent("""
		import os, sys
		from campy import frametimes
		times = frametimes.FrameTimes(sys.argv[1], 10)
		for k in range(1, 36):
			times.Add(k, 100.0 + k / 80, 1000 + k)
		os._exit(3)
		""")
	done = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=60)
	assert done.returncode == 3, done.stderr
	frames, hosts = np.load(tmp_path / "frametimes.npy"), np.load(tmp_path / "hosttimes.npy")
	assert frames.shape == (2, 30) and list(frames[0]) == list(range(1, 31)) # 3 batches of 10; 5 not yet written
	assert list(hosts) == list(range(1001, 1031))


class Stopped(BaseException):
	# Not an Exception: GrabFrames' loop does not catch it, so it ends GrabFrames
	# the way a crash would, without SaveMetadata
	pass


def fake_camera_module(crash_after=None):
	def OpenCamera(cam_params):
		cam_params["cameraModel"] = "fake"
		cam_params["cameraSerialNo"] = "0"
		return object(), cam_params

	def GrabFrame(camera, n):
		if crash_after is not None and n >= crash_after:
			raise Stopped()
		return n

	return types.SimpleNamespace(
		OpenCamera=OpenCamera,
		StartGrabbing=lambda camera: True,
		GrabFrame=GrabFrame,
		GetImageArray=lambda grab: np.full((4, 4), grab % 256, dtype=np.uint8),
		GetTimeStamp=lambda grab: 5000.0 + grab * 0.01,
		DisplayImage=lambda cam_params, q, grab: None,
		ReleaseFrame=lambda grab: None,
		CloseCamera=lambda cam_params, camera: None,
		)


def record(tmp_path, monkeypatch, crash_after=None):
	monkeypatch.setattr(unicam, "ImportCam", lambda make: fake_camera_module(crash_after))
	p = configurator.DefaultParams()
	p.update(cameraMake="fake", cameraName="Camera0", videoFolder=str(tmp_path), frameRate=100,
		recTimeInSec=3, chunkLengthInSec=1, displayFrameRate=0, writeQueueMaxSec="None")
	unicam.GrabFrames(p, deque(), deque([], 2), deque([], 1), deque([], 1))
	folder = tmp_path / "Camera0"
	with open(folder / "metadata.csv") as f:
		meta = dict(csv.reader(f))
	return folder, meta


def test_a_recording_cut_short_keeps_its_settings_and_frames(tmp_path, monkeypatch):
	with pytest.raises(Stopped):
		record(tmp_path, monkeypatch, crash_after=250)
	folder = tmp_path / "Camera0"
	with open(folder / "metadata.csv") as f:
		meta = dict(csv.reader(f))
	# The settings, from when the camera started grabbing; no totals, which only a closed recording has
	assert meta["cameraName"] == "Camera0" and meta["cameraModel"] == "fake" and meta["frameRate"] == "100"
	assert "totalFrames" not in meta
	# Every frame up to the last batch (one a second at 100 fps): 200 of the 250
	frames = np.load(folder / "frametimes.npy")
	assert list(frames[0]) == list(range(1, 201))
	assert np.allclose(frames[1], np.arange(200) * 0.01)
	assert len(np.load(folder / "hosttimes.npy")) == 200
	assert not (folder / "frametimes.mat").exists() # written when the recording closes


def test_a_closed_recording_has_every_file(tmp_path, monkeypatch):
	folder, meta = record(tmp_path, monkeypatch)
	frames = np.load(folder / "frametimes.npy")
	assert list(frames[0]) == list(range(1, 301))
	assert meta["totalFrames"] == "300" and meta["framesWritten"] == "300" and meta["framesDropped"] == "0"
	assert float(meta["totalTime"]) == frames[1][-1]
	mat = sio.loadmat(folder / "frametimes.mat")
	assert np.issubdtype(mat["frameNumber"].dtype, np.integer) # integers, as before
	assert mat["frameNumber"].shape == mat["timeStamp"].shape == (1, 300)
	assert np.array_equal(mat["frameNumber"][0], frames[0]) and np.array_equal(mat["timeStamp"][0], frames[1])
