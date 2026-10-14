"""RatCam (nir-eye-cam) cameras, against the nireyecam package's simulated camera."""

import csv
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
