"""Per-camera lists: one value for all cameras, one per camera, or more than needed."""

import os
import sys
import pytest
import yaml

from campy import configurator


def params(num_cams, **values):
	p = configurator.DefaultParams()
	p["numCams"] = num_cams
	p.update(values)
	return configurator.FillCameraLists(p)


def test_scalar_and_per_camera_lists():
	p = params(3, gpuID=[0, 0, -1], quality=21)
	assert [configurator.CameraParams(p, n)["gpuID"] for n in range(3)] == [0, 0, -1]
	assert configurator.CameraParams(p, 2)["quality"] == 21


def test_one_item_list_applies_to_every_camera():
	p = params(4, gpuID=[0])
	assert [configurator.CameraParams(p, n)["gpuID"] for n in range(4)] == [0, 0, 0, 0]


def test_longer_list_runs_with_fewer_cameras(capsys):
	# A config written for 8 cameras still runs with numCams 4
	p = params(4, gpuID=[0, 0, 0, 0, -1, -1, -1, -1],
		cameraNames=["c{}".format(i) for i in range(8)])
	configurator.ValidateCameraLists(p)
	assert "using the first 4" in capsys.readouterr().out
	cams = [configurator.CameraParams(p, n) for n in range(4)]
	assert [c["cameraName"] for c in cams] == ["c0", "c1", "c2", "c3"]
	assert [c["gpuID"] for c in cams] == [0, 0, 0, 0]


def test_short_list_names_the_key():
	p = params(8, gpuID=[0, 0, 0, 0])
	with pytest.raises(ValueError, match="gpuID has 4 values for 8 cameras"):
		configurator.ValidateCameraLists(p)


def test_camera_names_cannot_be_shared():
	p = params(2, cameraNames=["only"])
	with pytest.raises(ValueError, match="cameraNames"):
		configurator.ValidateCameraLists(p)


def test_digital_pins_are_not_per_camera():
	p = params(2, digitalPins=[3])
	configurator.ValidateCameraLists(p)
	assert configurator.CameraParams(p, 1)["digitalPins"] == [3]


def test_numcams_override_names_every_camera(tmp_path, monkeypatch):
	# Names are generated after --numCams is applied, not from the config's count
	config = tmp_path / "c.yaml"
	config.write_text(yaml.safe_dump({"numCams": 4, "cameraMake": "emu"}))
	monkeypatch.setattr(sys, "argv", ["campy-acquire", str(config), "--numCams", "8"])
	p = configurator.ConfigureParams()
	assert p["numCams"] == 8
	assert len(p["cameraNames"]) == 8
	assert p["cameraSelection"] == list(range(8))


def test_display_rate_zero_means_headless(tmp_path, monkeypatch):
	config = tmp_path / "c.yaml"
	config.write_text(yaml.safe_dump({"numCams": 1, "displayFrameRate": 0}))
	monkeypatch.setattr(sys, "argv", ["campy-acquire", str(config)])
	assert configurator.ConfigureParams()["displayFrameRate"] == 0


def test_range_checks_take_one_value_per_camera(capsys):
	p = params(3, frameRate=[60, 60, -1], cameraExposureTimeInUs=[1500, 1500, 10000])
	p = configurator.AutoParams(p, configurator.DefaultParams())
	assert p["frameRate"] == [60, 60, 100] # the bad one reset to the default
	assert p["cameraExposureTimeInUs"] == [1500, 1500, 10000]
	assert "frameRate set to invalid value" in capsys.readouterr().out


def test_trigger_rate_with_a_free_running_camera():
	# Four triggered FLIRs at 60 fps and an untriggered RatCam at 20
	p = params(5, cameraMake=["ratcam"] + ["flir"] * 4, frameRate=[20, 60, 60, 60, 60],
		cameraTrigger=["None"] + ["Line0"] * 4)
	assert configurator.TriggerFrameRate(p) == 60
	assert configurator.TriggerFrameRate(params(2, frameRate=30)) == 30 # nothing set: every camera

	p["cameraTrigger"] = "Line0" # now the trigger would have to serve both rates
	with pytest.raises(ValueError, match="frame rates 20, 60"):
		configurator.TriggerFrameRate(p)


def test_ratcam_ports_are_not_per_camera():
	p = params(1, ratcamPorts=["sim", "sim"])
	configurator.ValidateCameraLists(p)
	assert configurator.CameraParams(p, 0)["ratcamPorts"] == ["sim", "sim"]


def test_flir_ratcam_example_config(monkeypatch, capsys):
	# The example lists 9 cameras and runs 5: the RatCam and four FLIRs
	config = os.path.join(os.path.dirname(__file__), "..", "configs", "campy_config_flir_ratcam.yaml")
	monkeypatch.setattr(sys, "argv", ["campy-acquire", config]) # wherever pytest runs from
	# The config's ffmpegPath sets IMAGEIO_FFMPEG_EXE: keep it out of the real environment
	monkeypatch.setattr(os, "environ", dict(os.environ))
	p = configurator.ConfigureParams()
	assert "using the first 5" in capsys.readouterr().out
	assert configurator.TriggerFrameRate(p) == 60
	eye, cam0 = configurator.CameraParams(p, 0), configurator.CameraParams(p, 1)
	assert (eye["cameraMake"], eye["frameRate"], eye["gpuID"], eye["cameraSelection"]) == ("ratcam", 30, -1, 0)
	assert (cam0["cameraMake"], cam0["frameRate"], cam0["gpuID"], cam0["cameraSelection"]) == ("flir", 60, 0, 0)


def test_color_range_values():
	configurator.ValidateColorRange({"colorRange": "full"})
	configurator.ValidateColorRange({"colorRange": ["limited", "Full"]}) # one per camera
	with pytest.raises(ValueError, match='colorRange is "limited" or "full", not "pc"'):
		configurator.ValidateColorRange({"colorRange": ["full", "pc"]})
