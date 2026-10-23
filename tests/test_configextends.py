"""Rig profiles: a config extends a profile (or another file), which can extend
another, and campy-config shows where each setting came from."""

import os
import sys
import re
import json
import pytest

from campy import configurator, configtool, sweep


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
