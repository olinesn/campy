"""Per-camera lists: one value for all cameras, one per camera, or more than needed.
Command-line overrides: --key value means what key: value means in the config."""

import os
import sys
import pytest
import yaml
from argparse import ArgumentParser

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


def overrides(*argv):
	# What ParseClargs makes of campy-acquire c.yaml argv..., without the options not given
	clargs = configurator.ParseClargs(ArgumentParser(prog="campy-acquire"), ["c.yaml"] + list(argv))
	return {k: v for k, v in vars(clargs).items() if v is not None and k != "config"}


def refusal(capsys, *argv):
	# argparse's error for argv: it exits, and says why on stderr
	with pytest.raises(SystemExit):
		overrides(*argv)
	return capsys.readouterr().err


def test_false_is_false():
	# type=bool read every non-empty word as True, so --startArduino False started the trigger
	assert overrides("--startArduino", "False", "--vaapiLinearSurfaces", "False", "--cameraDebug", "False") == \
		{"startArduino": False, "vaapiLinearSurfaces": False, "cameraDebug": False}
	assert overrides("--startArduino", "True", "--vaapiLinearSurfaces", "false") == \
		{"startArduino": True, "vaapiLinearSurfaces": False}
	assert overrides("--cameraDebug", "yes", "--sessionFolder", "off") == {"cameraDebug": True, "sessionFolder": False}
	assert overrides("--ratcamSyncOut", "[True, False]") == {"ratcamSyncOut": [True, False]}


def test_text_needs_no_quotes():
	# These needed --cameraTrigger "'Line3'": literal_eval refused Line3
	assert overrides("--cameraTrigger", "Line3", "--codec", "h265", "--gpuMake", "intel", "--serialPort", "COM3") == \
		{"cameraTrigger": "Line3", "codec": "h265", "gpuMake": "intel", "serialPort": "COM3"}
	assert overrides("--cameraTrigger", "'Line3'", "--codec", "\"h265\"") == {"cameraTrigger": "Line3", "codec": "h265"}
	assert overrides("--cameraNames", "[EyeL, Cam0]", "--ratcamPorts", "['/dev/ttyACM1']") == \
		{"cameraNames": ["EyeL", "Cam0"], "ratcamPorts": ["/dev/ttyACM1"]}


def test_one_value_per_camera():
	assert overrides("--quality", "[24, 26]", "--gpuID", "[0,1]", "--cameraSelection", "[0, 2]") == \
		{"quality": [24, 26], "gpuID": [0, 1], "cameraSelection": [0, 2]}
	assert overrides("--quality", "24", "--frameRate", "59.94", "--recTimeInSec", "1e3") == \
		{"quality": 24, "frameRate": 59.94, "recTimeInSec": 1000.0}
	assert overrides("--colorRange", "[full, limited]", "--codec", "[h264, h265]") == \
		{"colorRange": ["full", "limited"], "codec": ["h264", "h265"]}


def test_none_is_the_text_none():
	# campy compares with the text "None" (writer's preset, trigger, FreeRuns), as the configs
	# write it. literal_eval gave the None object, which then counted as not given.
	assert overrides("--cameraTrigger", "None", "--preset", "None", "--triggerController", "None") == \
		{"cameraTrigger": "None", "preset": "None", "triggerController": "None"}
	assert overrides("--keyframeInterval", "None", "--writeQueueMaxSec", "None", "--blinkPin", "None") == \
		{"keyframeInterval": "None", "writeQueueMaxSec": "None", "blinkPin": "None"}
	assert overrides("--cameraLinkLimitMBps", "auto") == {"cameraLinkLimitMBps": "auto"}
	assert overrides("--cameraLinkLimitMBps", "380") == {"cameraLinkLimitMBps": 380}


def test_paths_are_taken_as_typed():
	ffmpeg = r"C:\ffmpeg-btbn-8.1\bin\ffmpeg.exe"
	assert overrides("--ffmpegPath", ffmpeg, "--videoFolder", r"D:\videos\rat 12") == \
		{"ffmpegPath": ffmpeg, "videoFolder": r"D:\videos\rat 12"}
	assert overrides("--cameraSettings", "'./configs/rgb24.pfs'") == {"cameraSettings": "./configs/rgb24.pfs"}
	assert overrides("--capacityFile", "None", "--vaapiDevice", "/dev/dri/renderD129") == \
		{"capacityFile": "None", "vaapiDevice": "/dev/dri/renderD129"}
	assert overrides("--preflight", "off") == {"preflight": "off"} # YAML would read off as False


def test_every_option_takes_its_default():
	# Each default, typed as it would be in the config, comes back as that default
	for key, value in configurator.DefaultParams().items():
		assert overrides("--" + key, str(value)) == {key: value}, key


def test_bad_values_name_the_option(capsys):
	assert "argument --quality: 'high' is not a whole number" in refusal(capsys, "--quality", "high")
	assert "argument --quality: '[24, 26' is not a value campy can read" in refusal(capsys, "--quality", "[24, 26")
	assert "argument --startArduino: 1 is not True or False" in refusal(capsys, "--startArduino", "1")
	assert "argument --codec: False is not text" in refusal(capsys, "--codec", "off")
	assert "argument --preset: None is not text" in refusal(capsys, "--preset", "")
	assert "argument --numCams: [2, 3] is not a whole number" in refusal(capsys, "--numCams", "[2, 3]")
	assert "argument --recTimeInSec" in refusal(capsys, "--recTimeInSec", "[10, 20]") # read once for the rig
	assert "argument --keyframeInterval" in refusal(capsys, "--keyframeInterval", "none")


def test_overrides_get_the_config_checks(tmp_path, monkeypatch, capsys):
	config = tmp_path / "c.yaml"
	config.write_text(yaml.safe_dump({"numCams": 2, "cameraMake": "emu", "cameraTrigger": "Line3"}))
	argv = ["campy-acquire", str(config), "--quality", "[24, 26]", "--cameraTrigger", "None", "--frameRate", "-5"]
	monkeypatch.setattr(sys, "argv", argv)
	p = configurator.ConfigureParams()
	assert [configurator.CameraParams(p, n)["quality"] for n in range(2)] == [24, 26]
	assert p["cameraTrigger"] == "None"
	assert p["frameRate"] == 100 # range-checked like the config's own values
	assert "frameRate set to invalid value" in capsys.readouterr().out

	monkeypatch.setattr(sys, "argv", ["campy-acquire", str(config), "--numCams", "4", "--gpuID", "[0, 1]"])
	with pytest.raises(ValueError, match="gpuID has 2 values for 4 cameras"):
		configurator.ConfigureParams()


def run_config(tmp_path, monkeypatch, config, *argv):
	# What campy-acquire c.yaml argv... makes of config
	path = tmp_path / "c.yaml"
	path.write_text(yaml.safe_dump(dict({"numCams": 2, "cameraMake": "emu"}, **config)))
	monkeypatch.setattr(sys, "argv", ["campy-acquire", str(path)] + list(argv))
	return configurator.ConfigureParams()


def test_gamma_defaults_to_0_6(tmp_path, monkeypatch):
	p = run_config(tmp_path, monkeypatch, {})
	assert [configurator.CameraParams(p, n)["cameraGamma"] for n in range(2)] == [0.6, 0.6]


def test_gamma_values(tmp_path, monkeypatch):
	# A number, "Off", or one per camera. YAML reads off as False, which is "Off" too.
	assert run_config(tmp_path, monkeypatch, {"cameraGamma": 1})["cameraGamma"] == 1
	assert run_config(tmp_path, monkeypatch, {"cameraGamma": "Off"})["cameraGamma"] == "Off"
	p = run_config(tmp_path, monkeypatch, {"cameraGamma": [0.45, False]})
	assert [configurator.CameraParams(p, n)["cameraGamma"] for n in range(2)] == [0.45, "Off"]
	assert run_config(tmp_path, monkeypatch, {}, "--cameraGamma", "Off")["cameraGamma"] == "Off"
	assert run_config(tmp_path, monkeypatch, {}, "--cameraGamma", "[0.5, off]")["cameraGamma"] == [0.5, "Off"]
	assert overrides("--cameraGamma", "1.0") == {"cameraGamma": 1.0}


def test_bad_gamma_is_refused(tmp_path, monkeypatch, capsys):
	for bad in (0, -1, "high", True, None):
		with pytest.raises(ValueError, match='cameraGamma is a number above 0 \\(e.g. 0.6\\) or "Off", not '):
			run_config(tmp_path, monkeypatch, {"cameraGamma": [0.6, bad]})
	assert 'argument --cameraGamma: cameraGamma is a number above 0 (e.g. 0.6) or "Off", not -2' in \
		refusal(capsys, "--cameraGamma", "-2")
	assert "argument --cameraGamma" in refusal(capsys, "--cameraGamma", "on") # YAML: True


def test_disable_gamma_is_read_as_camera_gamma(tmp_path, monkeypatch, capsys):
	# Configs written before cameraGamma: disableGamma True kept the camera linear, False now means 0.6
	p = run_config(tmp_path, monkeypatch, {"disableGamma": True})
	assert p["cameraGamma"] == "Off" and "disableGamma" not in p
	assert "The config says disableGamma: True, read as cameraGamma: Off." in capsys.readouterr().out
	assert run_config(tmp_path, monkeypatch, {"disableGamma": False})["cameraGamma"] == 0.6
	assert run_config(tmp_path, monkeypatch, {"disableGamma": [True, False]})["cameraGamma"] == ["Off", 0.6]
	assert run_config(tmp_path, monkeypatch, {}, "--disableGamma", "True")["cameraGamma"] == "Off"
	# The command line's either one wins over the config's other one
	assert run_config(tmp_path, monkeypatch, {"disableGamma": True}, "--cameraGamma", "1")["cameraGamma"] == 1
	assert run_config(tmp_path, monkeypatch, {"cameraGamma": 1}, "--disableGamma", "True")["cameraGamma"] == "Off"


def test_disable_gamma_and_camera_gamma_together_are_refused(tmp_path, monkeypatch):
	with pytest.raises(ValueError, match="The config gives both cameraGamma and disableGamma"):
		run_config(tmp_path, monkeypatch, {"disableGamma": True, "cameraGamma": 0.6})
	with pytest.raises(ValueError, match="The command line gives both cameraGamma and disableGamma"):
		run_config(tmp_path, monkeypatch, {}, "--disableGamma", "False", "--cameraGamma", "0.6")


def test_flir_example_configs_gamma(monkeypatch):
	# The ChronicRecord rig's exposure and gains were tuned with gamma off, to keep
	# its white mice at 20-80% of the range; the other FLIR example uses the default
	monkeypatch.setattr(os, "environ", dict(os.environ)) # the configs' ffmpegPath sets IMAGEIO_FFMPEG_EXE
	for name, gamma in (("campy_config_chronicrecord.yaml", "Off"), ("campy_config_1cam_flir.yaml", 0.6)):
		config = os.path.join(os.path.dirname(__file__), "..", "configs", name)
		monkeypatch.setattr(sys, "argv", ["campy-acquire", config])
		assert configurator.ConfigureParams()["cameraGamma"] == gamma, name
