"""The write queue cap: frames the encoder cannot take are counted, not queued."""

import csv
import types
from collections import deque

import numpy as np

from campy import configurator
from campy.cameras import unicam


def fake_camera_module():
	def OpenCamera(cam_params):
		cam_params["cameraModel"] = "fake"
		cam_params["cameraSerialNo"] = "0"
		return object(), cam_params

	return types.SimpleNamespace(
		OpenCamera=OpenCamera,
		StartGrabbing=lambda camera: True,
		GrabFrame=lambda camera, n: n,
		GetImageArray=lambda grab: np.full((4, 4), grab % 256, dtype=np.uint8),
		GetTimeStamp=lambda grab: grab * 0.01,
		DisplayImage=lambda cam_params, q, grab: None,
		ReleaseFrame=lambda grab: None,
		CloseCamera=lambda cam_params, camera: None,
		)


def grab(tmp_path, monkeypatch, max_sec):
	monkeypatch.setattr(unicam, "ImportCam", lambda make: fake_camera_module())
	p = configurator.DefaultParams()
	p.update(cameraMake="fake", cameraName="Camera0", videoFolder=str(tmp_path), frameRate=100,
		recTimeInSec=0.2, chunkLengthInSec=1, displayFrameRate=0, writeQueueMaxSec=max_sec)
	(tmp_path / "Camera0").mkdir()
	writeQueue = deque() # never drained: an encoder that has stopped
	unicam.GrabFrames(p, writeQueue, deque([], 2), deque([], 1), deque([], 1))
	frametimes = np.load(tmp_path / "Camera0" / "frametimes.npy")
	with open(tmp_path / "Camera0" / "metadata.csv") as f:
		meta = dict(csv.reader(f))
	return writeQueue, frametimes, meta


def test_full_queue_drops_and_counts(tmp_path, monkeypatch, capsys):
	writeQueue, frametimes, meta = grab(tmp_path, monkeypatch, 0.05) # 5 frames at 100 fps
	assert len(writeQueue) == 5
	# frametimes.npy has one row per written frame, so it lines up with the video
	assert list(frametimes[0]) == [1, 2, 3, 4, 5]
	assert meta["totalFrames"] == "20"
	assert meta["framesWritten"] == "5"
	assert meta["framesDropped"] == "15"
	out = capsys.readouterr().out
	assert "the encoder is 5 frames behind" in out
	assert "dropped 15 of them" in out


def test_unbounded_queue_keeps_every_frame(tmp_path, monkeypatch):
	writeQueue, frametimes, meta = grab(tmp_path, monkeypatch, "None")
	assert len(writeQueue) == 20
	assert list(frametimes[0]) == list(range(1, 21))
	assert meta["framesDropped"] == "0"


def test_arrival_times_saved_for_written_frames(tmp_path, monkeypatch):
	# campy-align compares cameras through these: one per frame in frametimes.npy
	writeQueue, frametimes, meta = grab(tmp_path, monkeypatch, 0.05)
	host = np.load(tmp_path / "Camera0" / "hosttimes.npy")
	assert host.dtype == np.int64 and len(host) == frametimes.shape[1] == 5
	assert np.all(np.diff(host) >= 0)
