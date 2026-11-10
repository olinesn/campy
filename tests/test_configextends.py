"""Rig profiles: a config extends a profile (or another file), which can extend
another, and campy-config shows where each setting came from."""

import os
import sys
import re
import json
import pytest

from campy import configurator, configtool, sweep


@pytest.fixture(autouse=True)
def home_elsewhere(tmp_path_factory, monkeypatch):
	# Messages show a path in the home folder as ~...: put the home folder
	# beside the tests' files, not around them (on Windows the temp folder is
	# in the home folder), so the messages name them in full
	home = str(tmp_path_factory.mktemp("home"))
	monkeypatch.setenv("HOME", home)
	monkeypatch.setenv("USERPROFILE", home)


@pytest.fixture
def profiles(tmp_path, monkeypatch):
	# This computer's rig profile folder (CAMPY_CONFIG_DIR), with one profile
	folder = tmp_path / "profiles"
	folder.mkdir()
	monkeypatch.setenv("CAMPY_CONFIG_DIR", str(folder))
	(folder / "rig.yaml").write_text(
		"numCams: 3\n"                      # 1
		"cameraMake: emu\n"                 # 2
		"frameRate: 30\n"                   # 3
		"cameraGain: [4, 3, 7]\n"           # 4
		"disableGamma: True\n"              # 5
		"codec: h265\n")                    # 6
	return folder


def write(path, text):
	path.write_text(text)
	return str(path)


def test_a_config_extends_a_rig_profile(tmp_path, profiles):
	exp = write(tmp_path / "exp.yaml", "extends: rig\nframeRate: 80\nquality: 26\n")
	r = configurator.Resolve(exp, ["--recTimeInSec", "60"])
	p = r.params
	assert (p["numCams"], p["frameRate"], p["quality"], p["codec"], p["recTimeInSec"]) == (3, 80, 26, "h265", 60)
	assert p["cameraGamma"] == "Off" and "disableGamma" not in p and "extends" not in p
	rig = str(profiles / "rig.yaml")
	assert r.chain == [exp, rig]
	assert str(r.sources["frameRate"]) == exp + ":2" # the experiment's line, over the rig's
	assert (r.sources["codec"].path, r.sources["codec"].line) == (rig, 6)
	assert str(r.sources["cameraGamma"]) == rig + ":5 (written as disableGamma: True)"
	assert str(r.sources["recTimeInSec"]) == "command line"
	assert str(r.sources["bufferSize"]) == "default"
	assert str(r.sources["cameraNames"]) == "default (one per camera, from numCams)"


def test_a_list_is_replaced_whole(tmp_path, profiles):
	exp = write(tmp_path / "exp.yaml", "extends: rig\ncameraGain: 6\n")
	assert configurator.Resolve(exp).params["cameraGain"] == 6
	exp = write(tmp_path / "exp.yaml", "extends: rig\ncameraGain: [1, 2, 3]\n")
	assert configurator.Resolve(exp).params["cameraGain"] == [1, 2, 3]


def test_fewer_cameras_than_the_rig(tmp_path, profiles):
	# The rig's per-camera list is written for its 3 cameras: campy says which file each came from
	exp = write(tmp_path / "exp.yaml", "extends: rig\nnumCams: 1\n")
	r = configurator.Resolve(exp)
	assert [c["cameraGain"] for c in r.Cameras()] == [4]
	assert any("cameraGain lists more values than numCams: using the first 1. The list is from "
		"rig.yaml, numCams 1 from {}:2".format(exp) in n.replace(str(profiles) + os.sep, "") for n in r.notes)


def test_more_cameras_than_a_list_names_both_files(tmp_path, profiles):
	exp = write(tmp_path / "exp.yaml", "extends: rig\nnumCams: 4\n")
	with pytest.raises(configurator.ConfigError, match=r"cameraGain has 3 values for 4 cameras.*"
			r"\(cameraGain is from .*rig.yaml:4, numCams from .*exp.yaml:2\)"):
		configurator.Resolve(exp)


def test_paths_are_from_the_file_that_names_them(tmp_path, profiles, monkeypatch):
	(tmp_path / "rigs").mkdir()
	(tmp_path / "exps").mkdir()
	write(tmp_path / "rigs" / "base.yaml", "extends: rig\nquality: 30\n") # a profile, from a file
	exp = write(tmp_path / "exps" / "exp.yaml", "extends: ../rigs/base.yaml\nframeRate: 60\n")
	monkeypatch.chdir(profiles) # not where the files are
	r = configurator.Resolve(exp)
	assert (r.params["quality"], r.params["frameRate"], r.params["codec"]) == (30, 60, "h265")
	assert len(r.chain) == 3


def test_extends_problems(tmp_path, profiles):
	def refusal(text):
		with pytest.raises(configurator.ConfigError) as e:
			configurator.Resolve(write(tmp_path / "exp.yaml", text))
		return str(e.value)
	said = refusal("extends: nosuchrig\n")
	assert "exp.yaml:1: extends: nosuchrig, but there is no" in said and "nosuchrig.yaml" in said
	assert "CAMPY_CONFIG_DIR" in said and "campy-config where" in said
	assert "extends: names one config file or rig profile, not ['rig', 'other']" in refusal("extends: [rig, other]\n")
	write(tmp_path / "a.yaml", "extends: ./b.yaml\n")
	write(tmp_path / "b.yaml", "extends: ./a.yaml\n")
	assert "extends: loops:" in refusal("extends: ./a.yaml\n")


def test_each_file_is_checked_where_it_is(tmp_path, profiles):
	(profiles / "rig.yaml").write_text("numCams: 2\nframrate: 30\n")
	with pytest.raises(configurator.ConfigError) as e:
		configurator.Resolve(write(tmp_path / "exp.yaml", "extends: rig\nquality: high\n"))
	found = [p.replace(str(profiles) + os.sep, "").replace(str(tmp_path) + os.sep, "") for p in e.value.problems]
	assert found == ['rig.yaml:2: unknown key "framrate". Did you mean "frameRate"?',
		"exp.yaml:2: quality: 'high' is not a whole number, or a list of them"]


def test_renamed_keys_per_file(tmp_path, profiles):
	# The rig's disableGamma is read as its cameraGamma, which the experiment's own replaces
	exp = write(tmp_path / "exp.yaml", "extends: rig\ncameraGamma: 0.6\n")
	assert configurator.Resolve(exp).params["cameraGamma"] == 0.6
	exp = write(tmp_path / "exp.yaml", "extends: rig\ncameraGamma: 0.6\ndisableGamma: True\n")
	with pytest.raises(configurator.ConfigError, match="exp.yaml gives both cameraGamma and disableGamma"):
		configurator.Resolve(exp)


def test_effective_config_round_trip(tmp_path, profiles):
	exp = write(tmp_path / "exp.yaml", "extends: rig\nframeRate: 80\nffmpegPath: 'C:\\ffmpeg\\bin\\ffmpeg.exe'\n")
	r = configurator.Resolve(exp, ["--quality", "[20, 21, 22]"])
	flat = write(tmp_path / "flat.yaml", r.ToYaml())
	again = configurator.Resolve(flat)
	assert again.params == r.params and again.chain == [flat]
	effective = json.loads(json.dumps(r.Effective()))
	assert effective["quality"] == {"value": [20, 21, 22],
		"source": {"kind": "command line", "file": None, "line": None, "note": None}}
	assert effective["frameRate"]["source"] == {"kind": "file", "file": exp, "line": 2, "note": None}
	assert effective["cameraGamma"]["source"]["note"] == "written as disableGamma: True"
	assert effective["bufferSize"]["source"]["kind"] == "default"


def test_configure_params_keeps_the_resolved(tmp_path, profiles, monkeypatch):
	exp = write(tmp_path / "exp.yaml", "extends: rig\n")
	monkeypatch.setattr(sys, "argv", ["campy-acquire", exp, "--numCams", "2"])
	params, r = configurator.ConfigureParams(resolved=True)
	assert params is r.params and params["numCams"] == 2 and str(r.sources["numCams"]) == "command line"


def test_campy_config_show(tmp_path, profiles, capsys):
	exp = write(tmp_path / "exp.yaml", "extends: rig\nframeRate: 80\n")
	assert configtool.Main(["show", exp, "--changed", "--recTimeInSec", "60"]) == 0
	out = capsys.readouterr().out
	assert "chain: {} -> {} -> campy's defaults".format(exp, profiles / "rig.yaml") in out
	lines = {line.split()[0]: line.split()[1:] for line in out.splitlines()[2:] if line and not line.startswith("note")}
	assert lines["frameRate"] == ["80", "exp.yaml:2"] and lines["codec"] == ["h265", "rig.yaml:6"]
	assert lines["recTimeInSec"] == ["60", "command", "line"] and "bufferSize" not in lines
	assert configtool.Main(["show", exp, "--cameras"]) == 0
	out = capsys.readouterr().out
	assert re.search(r"\ncameraGain +4 +3 +7 +rig.yaml:4\n", out)
	assert configtool.Main(["show", exp, "--json"]) == 0
	assert json.loads(capsys.readouterr().out)["settings"]["frameRate"]["value"] == 80
	with pytest.raises(SystemExit): # as campy-acquire refuses it
		configtool.Main(["show", exp, "--quality", "high"])
	assert "argument --quality: 'high' is not a whole number" in capsys.readouterr().err
	assert configtool.Main(["show", write(tmp_path / "bad.yaml", "extends: rig\nquality: -1\n")]) == 2
	assert "bad.yaml:2: quality: -1 is not" in capsys.readouterr().out


def test_campy_config_diff(tmp_path, profiles, capsys):
	# An old standalone config, and the same as a rig profile and a short experiment
	old = write(tmp_path / "old.yaml", "numCams: 3\ncameraMake: emu\nframeRate: 80\ncameraGain: [4, 3, 7]\n"
		"cameraGamma: 'Off'\ncodec: h265\n")
	new = write(tmp_path / "new.yaml", "extends: rig\nframeRate: 80\n")
	assert configtool.Main(["diff", old, new]) == 0
	assert "No difference" in capsys.readouterr().out
	assert configtool.Main(["diff", old, new, "--quality", "30"]) == 0 # the same options for both
	assert configtool.Main(["diff", old, write(tmp_path / "new.yaml", "extends: rig\n")]) == 1
	out = capsys.readouterr().out
	assert "frameRate" in out and "old.yaml:3" in out and "rig.yaml:3" in out


def test_campy_config_diff_compares_sections(tmp_path, profiles, capsys):
	# It compared the recording settings only: two configs whose live: models differ
	# said "No difference: both record with the same settings."
	(profiles / "rig.yaml").write_text("numCams: 2\nlive:\n  models: /models/v1\n  animals: 2\n")
	a = write(tmp_path / "a.yaml", "extends: rig\npost:\n  copy: /backup\n")
	b = write(tmp_path / "b.yaml", "extends: rig\nlive:\n  models: /models/v2\npico:\n  port: COM9\n")
	assert configtool.Main(["diff", a, b]) == 1
	rows = [line.split() for line in capsys.readouterr().out.splitlines()[1:]]
	assert rows == [["live.models", "/models/v1", "rig.yaml:2", "/models/v2", "b.yaml:2"], # the section's line
		["post.copy", "/backup", "a.yaml:2", "(not", "set)"],
		["pico.port", "(not", "set)", "COM9", "b.yaml:4"]]
	assert [key for key, _, _ in configtool.Diff(configurator.Resolve(a), configurator.Resolve(b))] == \
		["live.models", "post.copy", "pico.port"]
	# The same sections, written another way, are no difference
	c = write(tmp_path / "c.yaml", "numCams: 2\nlive: {animals: 2, models: /models/v1}\npost: {copy: /backup}\n")
	assert configtool.Main(["diff", a, c]) == 0
	assert "No difference: both record with the same settings, and their sections" in capsys.readouterr().out


def test_campy_config_where_and_new(tmp_path, profiles, capsys):
	assert configtool.Main(["new", "evo", "--from", write(tmp_path / "x.yaml", "numCams: 2\nframeRate: 60\n")]) == 0
	text = (profiles / "evo.yaml").read_text()
	assert text.startswith("# Rig profile evo") and text.endswith("numCams: 2\nframeRate: 60\n")
	assert configtool.Main(["new", "evo"]) == 1 # never replaces one
	assert configtool.Main(["new", "blank"]) == 0
	assert configurator.Resolve(str(profiles / "blank.yaml")).params["numCams"] == 1
	assert configtool.Main(["new", "x", "--from", write(tmp_path / "e.yaml", "extends: rig\n")]) == 1
	capsys.readouterr()
	assert configtool.Main(["where"]) == 0
	out = capsys.readouterr().out
	assert str(profiles) in out and "extends: evo" in out and "extends: rig" in out


def test_sweep_reads_the_chain(tmp_path, profiles):
	exp = write(tmp_path / "exp.yaml", "extends: rig\nframeRate: 80\n")
	values = configurator.ReadCheckedConfig(exp)
	assert (values["frameRate"], values["codec"], values["cameraGamma"]) == (80, "h265", "Off")


def test_sections_for_other_programs(tmp_path, profiles):
	# campy-session's live: and post: pass campy-acquire by, combined one level deep
	(profiles / "rig.yaml").write_text("numCams: 2\nlive:\n  models: /models/a\n  animals: 5\npost:\n  remux: True\n")
	exp = write(tmp_path / "exp.yaml", "extends: rig\nlive:\n  animals: 3\n")
	r = configurator.Resolve(exp)
	assert r.sections == {"live": {"models": "/models/a", "animals": 3}, "post": {"remux": True}}
	assert "live" not in r.params and "post" not in r.params # not camera settings
	assert r.section_sources["live"]["animals"].path == exp and r.section_sources["live"]["models"].line == 2
	effective = r.Effective()
	assert effective["live.animals"]["value"] == 3 and effective["live.models"]["source"]["line"] == 2
	again = configurator.Resolve(write(tmp_path / "flat.yaml", r.ToYaml()))
	assert again.sections == r.sections and again.params == r.params
	with pytest.raises(configurator.ConfigError, match="bad.yaml:2: live is a section of key: value lines"):
		configurator.Resolve(write(tmp_path / "bad.yaml", "extends: rig\nlive: yes\n"))
	with pytest.raises(configurator.ConfigError, match='unknown key "lives". Did you mean "live"?'):
		configurator.Resolve(write(tmp_path / "bad.yaml", "lives:\n  animals: 3\n"))
	assert "live.animals" not in configurator.ReadCheckedConfig(exp) and "live" not in configurator.ReadCheckedConfig(exp)


def test_a_section_null_drops_the_profile_s(tmp_path, profiles):
	# A profile's live: couldn't be turned off: live: and live: null were refused, and
	# live: {} kept the profile's keys. live: null (or live: alone) drops it now.
	(profiles / "rig.yaml").write_text("numCams: 2\nlive:\n  model: random\n  animals: 5\npost:\n  remux: True\n")
	for text in ("live: null\n", "live:\n", "live: ~\n"):
		r = configurator.Resolve(write(tmp_path / "exp.yaml", "extends: rig\n" + text))
		assert r.sections == {"post": {"remux": True}}, text
		assert "live" not in r.section_sources and "live.model" not in r.Effective()
		again = configurator.Resolve(write(tmp_path / "flat.yaml", r.ToYaml()))
		assert again.sections == r.sections and again.params == r.params
	from campy import session
	assert session.Config(str(tmp_path / "exp.yaml")).live is None
	assert session.Config(str(profiles / "rig.yaml")).live is not None
	# live: {} adds nothing, as before; a file after the null starts the section again
	exp = write(tmp_path / "exp.yaml", "extends: rig\nlive: {}\n")
	assert configurator.Resolve(exp).sections["live"] == {"model": "random", "animals": 5}
	write(tmp_path / "off.yaml", "extends: rig\nlive: null\n")
	r = configurator.Resolve(write(tmp_path / "on.yaml", "extends: off.yaml\nlive:\n  animals: 3\n"))
	assert r.sections["live"] == {"animals": 3} and r.section_sources["live"]["animals"].path.endswith("on.yaml")
	# A section alone in a file is no section; other words are still refused
	assert configurator.Resolve(write(tmp_path / "alone.yaml", "numCams: 1\nview: null\n")).sections == {}
	with pytest.raises(configurator.ConfigError, match="bad.yaml:2: live is a section of key: value lines \\(indented "
			"under it\\), or null to drop the one a file it extends gives, not False"):
		configurator.Resolve(write(tmp_path / "bad.yaml", "extends: rig\nlive: false\n"))


def test_one_name_alone_across_files(tmp_path, profiles):
	# A one-camera profile's name alone, and an experiment with more cameras: named with both files
	(profiles / "one.yaml").write_text("numCams: 1\ncameraNames: Top\n")
	assert configurator.Resolve(write(tmp_path / "same.yaml", "extends: one\n")).params["cameraNames"] == ["Top"]
	with pytest.raises(configurator.ConfigError) as e:
		configurator.Resolve(write(tmp_path / "exp.yaml", "extends: one\nnumCams: 2\n"))
	assert e.value.problems == ["cameraNames: 'Top' is one value for 2 cameras. It takes one per camera, e.g. "
		"cameraNames: [Camera0, Camera1]. (cameraNames is from {}:2, numCams from {}:2)".format(
		profiles / "one.yaml", tmp_path / "exp.yaml")]


def test_paths_in_the_home_folder_are_shown_with_a_tilde(tmp_path, monkeypatch):
	monkeypatch.setenv("HOME", str(tmp_path))
	monkeypatch.setenv("USERPROFILE", str(tmp_path))
	inside = os.path.join(str(tmp_path), ".campy", "configs", "rig.yaml")
	assert configurator.Shown(inside) == os.path.join("~", ".campy", "configs", "rig.yaml")
	assert configurator.Shown("exp.yaml") == "exp.yaml" # as given


# ------------------------------------------------------------------ one trigger board per rig

def test_one_trigger_board_per_rig(tmp_path, profiles):
	# The rig profile names the board that pulses its cameras (triggerController). On 10 Nov 2026 every
	# ChronicRecord calibration from the panel stopped 2 s in: the Pico had taken over the cameras' trigger, but the
	# calibration config still started the Arduino on COM9, unplugged since 2 Nov
	(profiles / "rig.yaml").write_text("numCams: 2\ntriggerController: pico\nstartArduino: False\ncameraTrigger: Line3\n")
	rig = str(profiles / "rig.yaml")
	ok = configurator.Resolve(write(tmp_path / "ok.yaml", "extends: rig\nframeRate: 10\npico:\n  set: {CAMERA_ONLY: 60}\n"))
	assert ok.params["triggerController"] == "pico" and ok.sections["pico"] == {"set": {"CAMERA_ONLY": 60}}
	# No trigger at all (free-running cameras) is still a config's own choice
	assert configurator.Resolve(write(tmp_path / "free.yaml", "extends: rig\ntriggerController: None\n"
		"cameraTrigger: None\n")).params["triggerController"] == "None"
	# The other board, named in a config: refused, naming the profile
	with pytest.raises(configurator.ConfigError) as e:
		configurator.Resolve(write(tmp_path / "other.yaml", "extends: rig\ntriggerController: arduino\n"))
	assert "triggerController: arduino ({}:2), but this rig's profile {} says pico".format(
		str(tmp_path / "other.yaml"), rig) in str(e.value)
	# Or on the command line
	with pytest.raises(configurator.ConfigError, match="triggerController: Arduino \\(command line\\), but this rig's"):
		configurator.Resolve(str(tmp_path / "ok.yaml"), ["--triggerController", "Arduino"])
	# Starting the Arduino on a Pico rig: refused
	with pytest.raises(configurator.ConfigError) as e:
		configurator.Resolve(write(tmp_path / "arduino.yaml", "extends: rig\nstartArduino: True\n"))
	assert "the Pico makes this rig's camera pulses, and campy starts no Arduino, but startArduino is True ({}:2)".format(
		str(tmp_path / "arduino.yaml")) in str(e.value)


def test_a_pico_section_on_an_arduino_rig(tmp_path, profiles):
	(profiles / "rig.yaml").write_text("numCams: 2\ntriggerController: arduino\nstartArduino: True\nserialPort: COM9\n")
	assert configurator.DrivesTrigger(configurator.Resolve(write(tmp_path / "a.yaml", "extends: rig\n")).params)
	# Both boards would pulse the cameras
	with pytest.raises(configurator.ConfigError, match=r"A pico: section \(.*both.yaml:2\) and startArduino: True "
			r"\(.*rig.yaml:3\): the Pico and the Arduino would both pulse the cameras"):
		configurator.Resolve(write(tmp_path / "both.yaml", "extends: rig\npico:\n  set: {CAMERA_ONLY: 60}\n"))
	# The Arduino not started, but the rig's board is still the Arduino
	with pytest.raises(configurator.ConfigError, match=r"A pico: section .*, but triggerController: arduino "
			r"\(.*rig.yaml:2\): this rig's camera pulses come from its Arduino"):
		configurator.Resolve(write(tmp_path / "pico.yaml", "extends: rig\nstartArduino: False\npico:\n  set: {}\n"))


def test_a_rig_that_names_no_board_is_as_before(tmp_path, profiles):
	# Without triggerController in the profile, a Pico config only needs startArduino False (chronic_pico.yaml as it
	# was), and the Arduino may still be started without a pico: section
	(profiles / "rig.yaml").write_text("numCams: 2\nstartArduino: True\ncameraTrigger: Line3\n")
	assert configurator.Resolve(write(tmp_path / "p.yaml", "extends: rig\nstartArduino: False\npico:\n  set: {}\n"))
	assert configurator.Resolve(write(tmp_path / "a.yaml", "extends: rig\n")).params["startArduino"] is True
	with pytest.raises(configurator.ConfigError, match="the Pico and the Arduino would both pulse the cameras"):
		configurator.Resolve(write(tmp_path / "b.yaml", "extends: rig\npico:\n  set: {}\n"))
