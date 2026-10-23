"""What campy says about a config before any camera opens: unknown keys with the
key probably meant, values of the wrong kind or out of range, keys given twice,
and every problem at once. Every example config still reads as it did."""

import os
import sys
import glob
import pytest
import yaml
from argparse import ArgumentParser

from campy import configurator, configschema, configtool

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIGS = sorted(glob.glob(os.path.join(HERE, "..", "configs", "*.yaml")))


def resolve(tmp_path, text, *argv):
	# campy-acquire c.yaml argv..., for a config written as text
	path = tmp_path / "c.yaml"
	path.write_text(text)
	clargs = configurator.ParseClargs(ArgumentParser(prog="campy-acquire"), [str(path)] + list(argv))
	return configurator.ResolveParams(clargs)


def problems(tmp_path, text, *argv):
	with pytest.raises(configurator.ConfigError) as e:
		resolve(tmp_path, text, *argv)
	return [p.replace(str(tmp_path) + os.sep, "") for p in e.value.problems]


def test_unknown_key_suggests_the_one_meant(tmp_path):
	assert problems(tmp_path, "numCams: 1\nframerate: 30\n") == \
		['c.yaml:2: unknown key "framerate". Did you mean "frameRate"?']
	assert problems(tmp_path, "cameraExposureTimeUs: 750\n") == \
		['c.yaml:1: unknown key "cameraExposureTimeUs". Did you mean "cameraExposureTimeInUs"?']
	assert problems(tmp_path, "exposure: 750\n") == \
		['c.yaml:1: unknown key "exposure". Did you mean "cameraExposureTimeInUs"?']
	assert "lists every key" in problems(tmp_path, "colour: red\n")[0]


def test_unknown_option_suggests_the_one_meant(tmp_path, capsys):
	path = tmp_path / "c.yaml"
	path.write_text("numCams: 1\n")
	for argv, said in ((["--framerate", "30"], "unknown option --framerate. Did you mean --frameRate?"),
			(["--qual", "20"], "unknown option --qual"), # a shortened option is not taken
			(["--quality=20", "--cameraGian", "3"], "Did you mean --cameraGain?")):
		with pytest.raises(SystemExit):
			configurator.ParseClargs(ArgumentParser(prog="campy-acquire"), [str(path)] + argv)
		assert said in capsys.readouterr().err


def test_file_values_are_checked_like_options(tmp_path):
	# Only the command line's were: quality "26" got through and failed later
	assert problems(tmp_path, 'quality: "26"\nstartArduino: "yes"\n') == [
		"c.yaml:1: quality: '26' is not a whole number, or a list of them",
		"c.yaml:2: startArduino: 'yes' is not True or False"]
	assert problems(tmp_path, "codec: x265\n") == \
		['c.yaml:1: codec: \'x265\' is not "h264", "h265" or "av1". Did you mean "h265"?']
	assert problems(tmp_path, "numCams: 2\nrecTimeInSec: [10, 20]\n") == \
		["c.yaml:2: recTimeInSec takes one value for the whole rig, not a list ([10, 20])"]
	assert problems(tmp_path, "cameraTrigger:\n") == ["c.yaml:1: cameraTrigger: None is not text, or a list "
		"of them (YAML reads null, ~ and an empty value as no value: write None)"]


def test_values_campy_takes(tmp_path):
	p = resolve(tmp_path, "numCams: 2\nrecTimeInSec: 1e3\ncolorRange: Full\npreflight: off\n"
		"cameraGamma: [0.45, off]\nkeyframeInterval: None\nquality: [0, 51]\n")
	assert (p["recTimeInSec"], p["colorRange"], p["preflight"]) == (1000.0, "Full", False)
	assert p["cameraGamma"] == [0.45, "Off"] and p["keyframeInterval"] == "None"
	assert resolve(tmp_path, "")["numCams"] == 1 # an empty config is campy's defaults


def test_quality_limit_follows_the_codec(tmp_path):
	assert problems(tmp_path, "quality: 60\n") == ["quality is 0 to 51 for codec h264, not 60."]
	assert problems(tmp_path, "numCams: 2\ncodec: [av1, h265]\nquality: 200\n") == \
		["quality is 0 to 51 for codec h265, not 200 (camera 1)."]
	assert resolve(tmp_path, "codec: av1\nquality: 200\n")["quality"] == 200


def test_a_key_given_twice_is_refused(tmp_path):
	# YAML keeps the last one without a word
	assert problems(tmp_path, "quality: 24\nnumCams: 1\nquality: 30\n") == \
		["c.yaml:3: quality is already set on line 1. Give each key once."]


def test_every_problem_at_once(tmp_path):
	found = problems(tmp_path, "numCams: 3\nframeRate: 0\ngpuID: [0, 1]\nbufferMode: Newest\n")
	assert found == ["c.yaml:2: frameRate: 0 is not a number above 0",
		'c.yaml:4: bufferMode: \'Newest\' is not "OldestFirst", "NewestFirst", "NewestOnly" or '
		'"OldestFirstOverwrite"']
	assert "The config has 2 problems:" in str(configurator.ConfigError(found))
	# List lengths are checked on the result, once each value is right
	assert problems(tmp_path, "numCams: 3\ngpuID: [0, 1]\n") == [
		"gpuID has 2 values for 3 cameras. Give one value per camera, or a single value for all of them. "
		"(gpuID is from c.yaml:2, numCams from c.yaml:1)"]


def test_not_a_config(tmp_path):
	assert problems(tmp_path, "- numCams: 1\n") == ["c.yaml: a campy config is a list of key: value lines, not a list"]
	assert problems(tmp_path, "numCams: [1\n")[0].startswith("c.yaml:2: campy can't read this YAML")


def old_params(path):
	# How campy 2.1.0 read a config with nothing out of range: its values, the
	# defaults for the rest, disableGamma as cameraGamma, then the camera lists
	params = configurator.GammaFromDisableGamma(dict(configurator.LoadConfig(path) or {}), "The config")
	for key, value in configurator.DefaultParams().items():
		params.setdefault(key, value)
	return configurator.FillCameraLists(params)


@pytest.mark.parametrize("path", CONFIGS, ids=os.path.basename)
def test_example_configs_read_as_before(path, monkeypatch):
	monkeypatch.setattr(os, "environ", dict(os.environ)) # ffmpegPath sets IMAGEIO_FFMPEG_EXE
	monkeypatch.setattr(sys, "argv", ["campy-acquire", path])
	new = configurator.ConfigureParams()
	old = old_params(path)
	configurator.ValidateGamma(old)
	assert new == old
	assert list(new) == list(old) # in the same order: metadata.csv lists them so


def test_campy_config_check(tmp_path, capsys):
	good, bad = tmp_path / "good.yaml", tmp_path / "bad.yaml"
	good.write_text("numCams: 1\ndisableGamma: True\n")
	bad.write_text("numCams: 1\nframerate: 30\n")
	assert configtool.Main(["check", str(good)]) == 0
	out = capsys.readouterr().out
	assert "OK    {}".format(good) in out and "good.yaml says disableGamma: True" in out
	assert configtool.Main(["check", str(good), str(bad)]) == 1
	out = capsys.readouterr().out
	assert "FAIL  {}".format(bad) in out and 'Did you mean "frameRate"?' in out
	assert "1 of 2 configs OK" in out


def test_reference_page_is_up_to_date():
	# docs/config-reference.md is made from configschema: regenerate it with
	#   campy-config reference > docs/config-reference.md
	with open(os.path.join(HERE, "..", "docs", "config-reference.md"), encoding="utf-8") as f:
		assert f.read() == configschema.Reference() + "\n"


def test_schema_and_options_agree():
	# Every setting is an option of campy-acquire, of the kind the schema says
	parser = ArgumentParser(prog="campy-acquire")
	configurator.ParseClargs(parser, ["c.yaml"])
	options = {a.dest for a in parser._actions} - {"help", "config"}
	sections = {s.name for s in configschema.SETTINGS if s.kind is configschema.SECTION}
	assert options == set(configschema.BY_NAME) - sections # sections are in the file only
	assert set(configurator.DefaultParams()) == {s.name for s in configschema.SETTINGS
		if s.default is not configschema.FILLED and not s.replaced_by and s.name not in sections}
	for s in configschema.SETTINGS:
		assert s.section in configschema.SECTIONS and s.help, s.name
