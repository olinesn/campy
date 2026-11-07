"""GrabFrames' per-frame work at the config's rates: the fps messages, and no window per camera at any displayFrameRate."""

import types
from collections import deque

import numpy as np

from campy import configurator
from campy.cameras import unicam


def grab(tmp_path, monkeypatch, **params):
	# GrabFrames on a fake camera; returns the grabs anything asked it to show (none: there is no per-camera display)
	shown = []
	camera = types.SimpleNamespace(
		OpenCamera=lambda cam_params: (object(), dict(cam_params, cameraModel="fake", cameraSerialNo="0")),
		StartGrabbing=lambda camera: True,
		GrabFrame=lambda camera, n: n + 1,
		GetImageArray=lambda grab: np.zeros((4, 4), dtype=np.uint8),
		GetTimeStamp=lambda grab: (grab - 1) / p["frameRate"],
		DisplayImage=lambda cam_params, q, grab: shown.append(grab),
		OpenPylonImageWindow=lambda cam_params: shown.append("Pylon window"),
		ReleaseFrame=lambda grab: None,
		CloseCamera=lambda cam_params, camera: None,
		)
	monkeypatch.setattr(unicam, "ImportCam", lambda make: camera)
	p = configurator.DefaultParams()
	p.update(cameraMake="fake", cameraName="Camera0", videoFolder=str(tmp_path), writeQueueMaxSec="None")
	p.update(params)
	(tmp_path / "Camera0").mkdir(parents=True)
	unicam.GrabFrames(p, deque(), deque([], 1), deque([], 1))
	return shown


def test_no_camera_shows_its_frames_in_a_window_of_its_own(tmp_path, monkeypatch):
	# campy-live-view shows every camera in one window (campy/acquireview.py); the camera thread shows nothing
	for rate in (10, 30):
		assert grab(tmp_path / str(rate), monkeypatch, frameRate=40, recTimeInSec=0.25, chunkLengthInSec=1,
			displayFrameRate=rate) == []


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
