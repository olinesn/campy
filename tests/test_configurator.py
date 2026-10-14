"""Per-camera lists: one value for all cameras, one per camera, or more than needed."""

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
