"""GrabFrames' per-frame work at the config's rates: which frames go to the preview, and the fps messages."""

import types
from collections import deque

import numpy as np

from campy import configurator
from campy.cameras import unicam


def grab(tmp_path, monkeypatch, **params):
	# GrabFrames on a fake camera; returns the frame numbers sent to the preview
	shown = []
	camera = types.SimpleNamespace(
		OpenCamera=lambda cam_params: (object(), dict(cam_params, cameraModel="fake", cameraSerialNo="0")),
		StartGrabbing=lambda camera: True,
		GrabFrame=lambda camera, n: n + 1,
		GetImageArray=lambda grab: np.zeros((4, 4), dtype=np.uint8),
		GetTimeStamp=lambda grab: (grab - 1) / p["frameRate"],
		DisplayImage=lambda cam_params, q, grab: shown.append(grab),
		ReleaseFrame=lambda grab: None,
		CloseCamera=lambda cam_params, camera: None,
		)
	monkeypatch.setattr(unicam, "ImportCam", lambda make: camera)
	p = configurator.DefaultParams()
	p.update(cameraMake="fake", cameraName="Camera0", videoFolder=str(tmp_path), writeQueueMaxSec="None")
	p.update(params)
	(tmp_path / "Camera0").mkdir()
	unicam.GrabFrames(p, deque(), deque([], 2), deque([], 1), deque([], 1))
	return shown


def test_preview_slower_than_the_camera_shows_every_nth_frame(tmp_path, monkeypatch):
	shown = grab(tmp_path, monkeypatch, frameRate=40, recTimeInSec=0.5, chunkLengthInSec=1, displayFrameRate=10)
	assert shown == [4, 8, 12, 16, 20]


def test_preview_faster_than_the_camera_shows_every_frame(tmp_path, monkeypatch):
	# A 20 fps RatCam with a displayFrameRate of 30 for every camera: each frame,
	# not one a second (every frameRate-th)
	shown = grab(tmp_path, monkeypatch, frameRate=20, recTimeInSec=0.5, chunkLengthInSec=1, displayFrameRate=30)
	assert shown == list(range(1, 11))


def test_fps_messages_at_a_chunk_shorter_than_a_frame(tmp_path, monkeypatch, capsys, caplog):
	# chunkLengthInSec 0.001 at 80 fps is 0 frames: every frame raised
	# ZeroDivisionError (logged with cameraDebug), and no fps was said
	grab(tmp_path, monkeypatch, frameRate=80, recTimeInSec=0.05, chunkLengthInSec=0.001, displayFrameRate=0,
		cameraDebug=True)
	assert "Caught exception" not in caplog.text
	out = capsys.readouterr().out
	# Every frame is a chunk; the first has no rate yet
	assert "collected 1 frames" not in out
	assert "Camera0 collected 4 frames at 80.0 fps" in out
