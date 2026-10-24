"""
session.json and campy-live-session.json (campy/manifest.py), and the
warnings for defaults that changed (campy/defaults.py): what a recording
keeps of how it was made, from the config's sources to how it ended, with
emulated cameras end to end.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time

import numpy as np
import pytest

from campy import configurator, defaults, livefeed, manifest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------------ defaults

def test_versions_sort_in_release_order():
	keys = [defaults.VersionKey(v) for v in ("2.0.0", "2.1.0rc3", "2.1.0rc7", "2.1.0", "2.1.1", "2.10")]
	assert keys == sorted(keys)
	assert defaults.VersionKey("v2.1") == defaults.VersionKey("2.1.0")
	assert defaults.VersionKey("latest") is None


def flir(**keys):
	return {"cameras": [dict({"cameraMake": "flir", "videoFilename": "0.mp4"}, **keys)]}


def test_a_flir_config_without_gamma_is_told_once_plainly():
	lines = defaults.Warnings({}, {}, flir())
	assert "cameraGamma not set: since 2.1.0 the default is 0.6 (was Off)." in lines[0]
	assert len([l for l in lines if l.startswith("cameraGamma")]) == 1
	text = defaults.Message(lines)
	assert text.startswith("This run relies on {} defaults that changed".format(len(lines)))
	assert "defaultWarnings: False" in text


def test_a_key_that_is_set_is_not_warned_about():
	for source in ("file", "command line"):
		lines = defaults.Warnings({}, {"cameraGamma": source}, flir())
		assert not any(l.startswith("cameraGamma") for l in lines)


def test_only_changes_that_apply_are_said():
	emu = {"cameras": [{"cameraMake": "emu", "videoFilename": "0.avi"}]}
	keys = [l.split()[0] for l in defaults.Warnings({}, {}, emu)]
	assert "cameraGamma" not in keys and "cameraLinkLimitMBps" not in keys # no FLIR camera
	assert "fragmentedVideo" not in keys # not an .mp4
	assert "writeQueueMaxSec" in keys and "sessionFolder" in keys


def test_defaults_version_says_only_later_changes():
	assert defaults.Warnings({}, {}, flir(), known="2.1.0") == []
	assert len(defaults.Warnings({}, {}, flir(), known="2.0.0")) == len(defaults.Warnings({}, {}, flir()))
	live = defaults.Warnings({}, {}, {"gpu": True}, program="campy-live", known="2.1.0")
	assert [l.split()[0] for l in live] == ["--engine"] # the one after 2.1.0
	with pytest.raises(ValueError, match="defaultsVersion"):
		defaults.Warnings({}, {}, flir(), known="soon")


def test_default_warnings_false_silences_all():
	assert defaults.Warnings({}, {}, flir(), enabled=False) == []
	assert defaults.Warnings({}, {}, flir(), enabled="off") == []


def test_campy_live_changes_apply_with_a_gpu_and_rocm():
	assert defaults.Warnings({}, {}, {"gpu": False}, program="campy-live") == []
	nvidia = [l.split()[0] for l in defaults.Warnings({}, {}, {"gpu": True}, program="campy-live")]
	rocm = [l.split()[0] for l in defaults.Warnings({}, {}, {"gpu": True, "rocm": True}, program="campy-live")]
	assert "--fast-upsample" not in nvidia and "--fast-upsample" in rocm
	assert {"--fixed-crops", "--cuda-graphs", "--engine"} <= set(nvidia)


def test_every_change_names_a_real_default():
	# A key in the table that campy does not have would never be warned about
	params = configurator.DefaultParams()
	from campy import live
	options = {s for a in live.Parser()._actions for s in a.option_strings}
	for change in defaults.CHANGES:
		if change.program == "campy-acquire":
			assert params[change.key] == change.now, change.key
		else:
			assert change.key in options, change.key
			assert live.Parser().get_default(change.key[2:].replace("-", "_")) == change.now, change.key


# ------------------------------------------------------------------ the config's sources

def configure(tmp_path, text, argv=()):
	path = tmp_path / "config.yaml"
	path.write_text(text)
	argv = [str(path)] + list(argv)
	old = sys.argv
	sys.argv = ["campy-acquire"] + argv
	try:
		return configurator.ConfigureParams(), configurator.Sources()
	finally:
		sys.argv = old


def test_each_value_says_where_it_came_from(tmp_path):
	params, sources = configure(tmp_path, "numCams: 2\ncameraMake: emu\nquality: 23\ndisableGamma: True\n",
		["--quality", "25", "--notes", "first day"])
	assert params["quality"] == 25 and sources["quality"] == "command line"
	assert sources["numCams"] == "file" and sources["frameRate"] == "default"
	assert params["cameraGamma"] == "Off" and sources["cameraGamma"] == "file" # read from disableGamma
	assert sources["notes"] == "command line" and params["notes"] == "first day"
	assert sources["cameraNames"] == "default" # made from numCams


def test_animal_ids_are_one_list_for_the_whole_rig(tmp_path):
	params, sources = configure(tmp_path, "numCams: 3\ncameraMake: emu\nanimalIds: [M1, 1234]\n"
		"sessionFields: {cage: B3}\n")
	assert params["animalIds"] == ["M1", "1234"] and params["sessionFields"] == {"cage": "B3"}
	# Not one per camera, as other lists are
	assert configurator.CameraParams(params, 2)["animalIds"] == ["M1", "1234"]
	params, _ = configure(tmp_path, "numCams: 3\ncameraMake: emu\n", ["--animalIds", "M7",
		"--sessionFields", "{condition: saline}"])
	assert params["animalIds"] == ["M7"] and params["sessionFields"] == {"condition": "saline"}


def test_a_bad_experiment_key_fails_at_startup(tmp_path):
	with pytest.raises(ValueError, match="sessionFields: .* is not a mapping"):
		configure(tmp_path, "cameraMake: emu\nsessionFields: cage B3\n")
	with pytest.raises(ValueError, match="defaultsVersion is a campy version"):
		configure(tmp_path, "cameraMake: emu\ndefaultsVersion: soon\n")


def test_the_effective_config_marks_changed_defaults():
	effective = manifest.EffectiveConfig({"cameraGamma": 0.6, "quality": 25},
		{"cameraGamma": "default", "quality": "file"})
	assert effective["quality"] == {"value": 25, "source": "file"}
	assert effective["cameraGamma"]["defaultChanged"]["in"] == "2.1.0"
	assert effective["cameraGamma"]["defaultChanged"]["was"] == "Off"


# ------------------------------------------------------------------ the file

def test_the_first_write_never_replaces_a_file_that_holds_something(tmp_path):
	path = tmp_path / "session.json"
	path.write_text('{"another": "recording"}')
	f = manifest.JsonFile(str(path))
	with pytest.raises(manifest.WouldReplace):
		f.Update(manifest.Fresh(lambda old: {"new": 1}))
	assert json.loads(path.read_text()) == {"another": "recording"}
	path.write_text("") # newfiles.Claim's: empty, so it is ours
	f.Update(manifest.Fresh(lambda old: {"new": 1}))
	f.Update(lambda old: dict(old, more=2))
	assert json.loads(path.read_text()) == {"new": 1, "more": 2}
	assert not os.path.exists(str(path) + ".lock")
	assert [p.name for p in tmp_path.iterdir()] == ["session.json"] # no .partial left


def test_a_stale_lock_is_broken(tmp_path):
	path = tmp_path / "session.json"
	path.write_text("{}")
	lock = str(path) + ".lock"
	open(lock, "w").close()
	old = time.time() - manifest.JsonFile.LOCK_STALE_S - 5
	os.utime(lock, (old, old))
	f = manifest.JsonFile(str(path))
	f.written = True
	f.Update(lambda d: {"ok": True})
	assert json.loads(path.read_text()) == {"ok": True}


def test_a_note_is_added_and_nothing_else_changes(tmp_path):
	(tmp_path / "session.json").write_text(json.dumps({"schema": manifest.SCHEMA, "annotations": [], "x": 1}))
	note = manifest.AddNote(str(tmp_path), "M3 lost its ear tag", at="2026-10-23T10:00:00-04:00")
	manifest.AddNote(str(tmp_path / "session.json"), "second")
	data = json.loads((tmp_path / "session.json").read_text())
	assert data["x"] == 1 and data["annotations"][0] == note and data["annotations"][1]["text"] == "second"
	with pytest.raises(FileNotFoundError):
		manifest.AddNote(str(tmp_path / "nothing"), "x")


def test_a_note_given_the_files_path_says_that_path(tmp_path, capsys):
	(tmp_path / "session.json").write_text(json.dumps({"schema": manifest.SCHEMA, "annotations": []}))
	assert manifest.NoteMain([str(tmp_path / "session.json"), "x"]) == 0
	said = capsys.readouterr().out
	assert "Added to {}:".format(tmp_path / "session.json") in said and "session.json" + os.sep + "session.json" not in said


def test_a_reader_holding_the_file_open(tmp_path, monkeypatch):
	# Windows: a reader that has session.json open stops the rename. Once it
	# lets go, the rename goes through; one that holds on is written into; one
	# that lets no one write fails plainly, and nothing is left behind.
	path = tmp_path / "session.json"
	path.write_text(json.dumps({"annotations": []}))
	monkeypatch.setattr(manifest.JsonFile, "REPLACE_WAIT_S", 0)
	real = os.replace
	tries = []
	def held(n):
		def replace(src, dst):
			tries.append(1)
			if len(tries) <= n:
				raise PermissionError(13, "Access is denied")
			return real(src, dst)
		return replace
	monkeypatch.setattr(manifest.os, "replace", held(3))
	manifest.AddNote(str(tmp_path), "let go")
	monkeypatch.setattr(manifest.os, "replace", held(10 ** 6))
	manifest.AddNote(str(tmp_path), "held on")
	assert [a["text"] for a in json.loads(path.read_text())["annotations"]] == ["let go", "held on"]
	def denied(self, text):
		raise PermissionError(13, "Permission denied", self.path)
	monkeypatch.setattr(manifest.JsonFile, "WriteInPlace", denied)
	with pytest.raises(PermissionError, match="close it and try again"):
		manifest.AddNote(str(tmp_path), "lost")
	assert [a["text"] for a in json.loads(path.read_text())["annotations"]] == ["let go", "held on"]
	assert sorted(p.name for p in tmp_path.iterdir()) == ["session.json"] # no .partial, no .lock


def strict_json(text):
	# As a strict reader (JavaScript's JSON.parse, jq) reads it: Python's
	# json takes the bare words Infinity and NaN, which are not JSON
	def refuse(word):
		raise ValueError("not JSON: " + word)
	return json.loads(text, parse_constant=refuse)


def test_infinity_and_nan_are_written_as_strict_json(tmp_path, monkeypatch):
	path = tmp_path / "campy-live-session.json"
	f = manifest.JsonFile(str(path))
	f.Update(manifest.Fresh(lambda old: {"inf": float("inf"), "minus": -float("inf"), "nan": float("nan"),
		"numpy": [np.float64("inf"), np.float32("nan"), np.float32(0.5)], "nested": {"z": (-np.inf, np.inf)},
		"finite": 2.5, "int": 3, "flag": True, "none": None, "text": "NaN", "string": "Infinity"}))
	data = strict_json(path.read_text())
	assert data["inf"] == "Infinity" and data["minus"] == "-Infinity" and data["nan"] == "NaN"
	assert data["numpy"][:2] == ["Infinity", "NaN"] and data["nested"] == {"z": ["-Infinity", "Infinity"]}
	assert data["finite"] == 2.5 and data["int"] == 3 and data["flag"] is True and data["none"] is None
	assert f.Read() == data
	# The text gives the number back, to Python as to JavaScript's Number()
	assert float(data["inf"]) == np.inf and float(data["minus"]) == -np.inf and np.isnan(float(data["nan"]))
	# A number Strict missed fails the write (and so a test), not a reader
	monkeypatch.setattr(manifest, "Strict", lambda data: data)
	with pytest.raises(ValueError, match="JSON compliant"):
		f.Update(lambda old: {"missed": float("inf")})
	assert strict_json(path.read_text()) == data


def test_a_manifest_from_before_reads_as_a_new_one(tmp_path):
	# campy 2.2.0rc1 and before wrote the bare words: read as the text a new
	# file has, so what copies it (campy-live-export's .slp provenance) is
	# strict JSON too
	(tmp_path / "session.json").write_text('{"schema": "campy-session/1", "x": NaN}')
	(tmp_path / "campy-live-session.json").write_text('{"schema": "campy-live-session/1", "options": '
		'{"--wait-sec": {"value": Infinity, "source": "default"}, "--z": {"value": [-Infinity, 1.5]}}}')
	found = manifest.Read(str(tmp_path))
	assert found["session"]["x"] == "NaN"
	assert found["live"][0]["options"]["--wait-sec"]["value"] == "Infinity"
	assert found["live"][0]["options"]["--z"]["value"] == ["-Infinity", 1.5]
	strict_json(json.dumps(found, allow_nan=False))
	# A note rewrites the old file as strict JSON
	manifest.AddNote(str(tmp_path), "checked")
	assert strict_json((tmp_path / "session.json").read_text())["x"] == "NaN"


def test_wait_sec_still_means_wait_for_ever(tmp_path):
	# --wait-sec's default is infinity: written "Infinity", read back it is
	# the same number campy-live waits for
	from campy import live
	parser = live.Parser()
	for n, (argv, value) in enumerate((([], "Infinity"), (["--wait-sec", "30"], 30),
			(["--wait-sec", "inf"], "Infinity"))):
		args = parser.parse_args(argv)
		path = tmp_path / "live{}.session.json".format(n)
		session = manifest.LiveSession(str(path))
		session.Start(args, parser, argv, None, {}, None, [], facts={"campy": {}, "host": {}})
		session.End({"sets": 0, "latency_ms": {"p50": float("nan")}}, None)
		data = strict_json(path.read_text())
		assert data["status"] == "complete", data
		assert data["options"]["--wait-sec"]["value"] == value
		assert float(data["options"]["--wait-sec"]["value"]) == args.wait_sec
		assert data["end"]["latency_ms"] == {"p50": "NaN"}
	assert parser.parse_args([]).wait_sec == float("inf")


def test_metadata_csvs_inf_and_nan_are_numbers(tmp_path):
	path = tmp_path / "metadata.csv"
	path.write_text("resultingFrameRate,nan\ngain,inf\ncameraModel,Blackfly S\nframeWidth,1440\n")
	meta = manifest.ReadMetadata(str(path))
	assert np.isnan(meta["resultingFrameRate"]) and meta["gain"] == np.inf
	assert meta["cameraModel"] == "Blackfly S" and meta["frameWidth"] == 1440


def test_a_linux_gpu_is_named_from_lspci():
	# lspci -mm on the EVO-X2: options between the quoted fields
	line = 'c6:00.0 "Display controller" "Advanced Micro Devices, Inc. [AMD/ATI]" "Device 1586" -rc1 -p00 ' \
		'"Unknown vendor 2014" "Device 801d"\n'
	assert manifest.LspciName(line) == "Advanced Micro Devices, Inc. [AMD/ATI] Device 1586"
	assert manifest.LspciName('01:00.0 "VGA compatible controller" "NVIDIA Corporation" "AD107GL [RTX 2000 Ada]" '
		'-ra1 "Dell" "Device 0cfd"') == "NVIDIA Corporation AD107GL [RTX 2000 Ada]"
	assert manifest.LspciName("") is None and manifest.LspciName(None) is None and manifest.LspciName('x "un') is None


def test_git_commit_without_git(tmp_path):
	# The rigs' checkouts, read from the files where git is not installed
	git = tmp_path / ".git"
	(git / "refs" / "heads").mkdir(parents=True)
	(git / "HEAD").write_text("ref: refs/heads/dev\n")
	(git / "packed-refs").write_text("# pack-refs\n{} refs/heads/dev\n".format("a" * 40))
	assert manifest.ReadHead(str(git)) == ("a" * 40, "dev")
	(git / "refs" / "heads" / "dev").write_text("b" * 40 + "\n")
	assert manifest.ReadHead(str(git)) == ("b" * 40, "dev")
	(git / "HEAD").write_text("c" * 40 + "\n") # detached
	assert manifest.ReadHead(str(git)) == ("c" * 40, None)


def test_campy_says_its_commit():
	info = manifest.CampyInfo()
	assert info["version"]
	if info["installedFrom"] == "git checkout":
		assert re.fullmatch(r"[0-9a-f]{40}", info["commit"] or "")


# ------------------------------------------------------------------ models

TRAINING_CONFIG = """data_config:
  preprocessing:
    ensure_grayscale: true
    max_height: 1024
    max_width: 1280
    scale: 0.5
    crop_size: null
model_config:
  backbone_config:
    unet:
      filters: 16
      filters_rate: 2.5
      max_stride: 32
      output_stride: 4
  head_configs:
    centroid:
      confmaps:
        anchor_part: Trunk
        sigma: 2.5
        output_stride: 4
"""


def test_a_model_is_named_by_its_weights(tmp_path):
	pytest.importorskip("yaml")
	(tmp_path / "best.ckpt").write_bytes(b"weights")
	(tmp_path / "training_config.yaml").write_text(TRAINING_CONFIG)
	record = manifest.ModelRecord(str(tmp_path))
	import hashlib
	digest = hashlib.sha256(b"weights").hexdigest()
	assert record["ckptSha256"] == digest and record["id"] == digest[:12]
	assert record["settings"] == {"scale": 0.5, "crop_size": None, "max_height": 1024, "max_width": 1280,
		"ensure_grayscale": True, "backbone": "unet", "filters": 16, "filters_rate": 2.5, "max_stride": 32,
		"output_stride": 4, "head": "centroid", "head_anchor_part": "Trunk", "head_sigma": 2.5,
		"head_output_stride": 4}
	assert record["modelJson"] is None and "idMismatch" not in record
	# model.json, the model loop's: kept, and flagged when it is another model's
	(tmp_path / "model.json").write_text(json.dumps({"id": "000000000000", "package": "p1"}))
	record = manifest.ModelRecord(str(tmp_path))
	assert record["modelJson"]["package"] == "p1" and "best.ckpt's is " + digest[:12] in record["idMismatch"]


def test_campy_live_options_say_whether_they_were_given():
	from campy import live
	sources, names = manifest.OptionSources(live.Parser(), ["--model", "sleap", "--fixed-crops", "--fast-up", "off"])
	assert sources["--model"] == sources["--fixed-crops"] == sources["--fast-upsample"] == "command line" # abbreviated
	assert sources["--cuda-graphs"] == "default" and names["fast_upsample"] == "--fast-upsample"


# ------------------------------------------------------------------ campy-acquire, emulated cameras

def record(tmp_path, cams=("Camera0", "Camera1"), seconds=2, extra=(), argv=(), clips=None, interrupt_after=None):
	"""
	campy-acquire with emulated cameras, from a subprocess; returns (the
	process, its output, the session folder)
	"""
	import test_emu
	for name in clips if clips is not None else cams:
		(tmp_path / name).mkdir(exist_ok=True)
		test_emu.write_clip(str(tmp_path / name / "clip.mp4"), frames=7)
	config = tmp_path / "config.yaml"
	config.write_text("\n".join([
		"numCams: {}".format(len(cams)), 'cameraMake: "emu"', "cameraNames: [{}]".format(", ".join(cams)),
		'videoFolder: "{}"'.format(tmp_path.as_posix()), 'videoFilename: "emuclip.mp4"',
		"frameRate: 20", "recTimeInSec: {}".format(seconds), "chunkLengthInSec: 1", "displayFrameRate: 0",
		'pixelFormatInput: "gray"', 'pixelFormatOutput: "yuv420p"', "gpuID: -1", 'codec: "h264"',
		'preflight: "off"', "readyTimeoutSec: 10"] + list(extra) + [""]))
	env = dict(os.environ, PYTHONPATH=REPO, PYTHONUNBUFFERED="1", CAMPY_LAUNCHER="campy-session/test")
	env.pop("IMAGEIO_FFMPEG_EXE", None)
	code = "import sys; sys.argv = ['campy-acquire'] + sys.argv[1:]; from campy.campy import Main; Main()"
	proc = subprocess.Popen([sys.executable, "-c", code, str(config)] + list(argv), cwd=str(tmp_path), env=env,
		stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
	if interrupt_after is not None:
		deadline = time.monotonic() + 60
		# Recording: frames in hosttimes.npy (newfiles.Claim made it empty at the start)
		while not any(p.stat().st_size for p in tmp_path.glob("2*/Camera0/hosttimes.npy")) \
				and time.monotonic() < deadline:
			time.sleep(0.1)
		time.sleep(interrupt_after)
		os.killpg(proc.pid, signal.SIGINT) # Ctrl-C, to campy and its camera processes
	out, _ = proc.communicate(timeout=120)
	sessions = [d for d in tmp_path.iterdir() if re.fullmatch(r"\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d", d.name)]
	return proc, out, sessions[0] if sessions else None


def test_a_recording_says_how_it_was_made(tmp_path):
	proc, out, folder = record(tmp_path, extra=['experiment: "social"', "animalIds: [M1, M2]",
		"sessionFields: {cage: B3, weight: .nan}"], argv=["--notes", "new bedding", "--quality", "23"])
	assert proc.returncode == 0, out
	data = strict_json((folder / "session.json").read_text()) # YAML's .nan, as the text "NaN"
	assert data["schema"] == "campy-session/1" and data["status"] == "complete"
	assert set(data["written"]) == {"start", "recording", "end"}
	assert data["campy"]["version"] and data["host"]["name"] and data["host"]["ffmpeg"]["version"]
	# The config: the file itself, and where each value came from
	config = data["config"]
	assert config["file"]["text"] == (tmp_path / "config.yaml").read_text()
	assert config["file"]["sha256"] == manifest.Sha256(str(tmp_path / "config.yaml"))
	assert config["effective"]["quality"] == {"value": 23, "source": "command line"}
	assert config["effective"]["frameRate"] == {"value": 20, "source": "file"}
	assert config["effective"]["writeQueueMaxSec"]["source"] == "default"
	assert config["effective"]["videoFolder"]["value"] == tmp_path.as_posix() # as configured, not the session's
	assert config["launcher"] == "campy-session/test"
	# The changed defaults it relies on: said once at startup, and kept
	assert config["defaultWarnings"] and all(out.count(line) == 1 for line in config["defaultWarnings"]), out
	assert out.count("This run relies on") == 1
	assert data["experiment"] == {"name": "social", "animalIds": ["M1", "M2"], "notes": "new bedding",
		"fields": {"cage": "B3", "weight": "NaN"}}
	# The cameras: serials as campy found them, and what they opened as
	assert [c["serial"] for c in data["cameras"]] == ["emu0", "emu1"]
	assert [c["video"] for c in data["cameras"]] == ["Camera0/emuclip.mp4", "Camera1/emuclip.mp4"]
	opened = data["recording"]["cameras"]
	assert [c["cameraModel"] for c in opened] == ["Emulated_Camera"] * 2 and all(c["ready"] for c in opened)
	assert opened[0]["frameWidth"] == 64 and opened[0]["frameHeight"] == 48
	assert data["recording"]["trigger"]["startedBy"] == "external"
	# How it ended
	end = data["end"]
	assert end["exitCode"] == 0 and end["failures"] == {}
	assert [c["framesWritten"] for c in end["cameras"]] == [40, 40]
	assert all(c["framesGrabbed"] == 40 and c["framesDropped"] == 0 and c["videoBytes"] > 0 for c in end["cameras"])
	assert not list(folder.glob("session.json.*")) # no lock or partial file left

	# A note afterwards, from the command line
	done = subprocess.run([sys.executable, "-m", "campy.manifest", "note", str(folder), "M2 sleeping"],
		capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=REPO))
	assert done.returncode == 0, done.stderr
	data2 = strict_json((folder / "session.json").read_text())
	assert data2["annotations"][0]["text"] == "M2 sleeping"
	assert {k: v for k, v in data2.items() if k != "annotations"} == {k: v for k, v in data.items() if k != "annotations"}


def test_a_config_that_knows_the_defaults_is_not_warned(tmp_path):
	proc, out, folder = record(tmp_path, seconds=1, extra=['defaultsVersion: "2.1.0"'])
	assert proc.returncode == 0, out
	assert "relies on" not in out
	assert json.loads((folder / "session.json").read_text())["config"]["defaultWarnings"] == []


def test_a_recording_whose_camera_fails_says_so(tmp_path):
	# Camera1 has no clip: it fails as it opens, and Camera0 records on
	proc, out, folder = record(tmp_path, seconds=1, clips=["Camera0"])
	assert proc.returncode == 1, out
	data = json.loads((folder / "session.json").read_text())
	assert data["status"] == "failed" and data["end"]["exitCode"] == 1
	assert list(data["end"]["failures"]) == ["Camera1"]
	assert data["end"]["cameras"][0]["framesWritten"] == 20
	assert data["recording"]["cameras"][1]["ready"] is False


@pytest.mark.skipif(sys.platform == "win32", reason="Ctrl-C to a process group: POSIX")
def test_a_recording_stopped_with_ctrl_c_says_so(tmp_path):
	proc, out, folder = record(tmp_path, seconds=30, interrupt_after=1)
	data = json.loads((folder / "session.json").read_text())
	assert data["status"] == "interrupted", out
	assert 0 < data["end"]["cameras"][0]["framesWritten"] < 600


# ------------------------------------------------------------------ campy-live

def test_campy_live_writes_its_session_into_the_recording(tmp_path, monkeypatch):
	# The feed says where the recording is (each ring's description): campy-
	# live's session goes there, next to campy-acquire's
	import test_livefeed
	recording = tmp_path / "rec"
	recording.mkdir()
	(recording / "session.json").write_text(json.dumps({"schema": manifest.SCHEMA}))
	ring = livefeed.RingWriter

	class Described(ring):
		def __init__(self, name, *a, **k):
			k["description"] = json.dumps({"kind": livefeed.CAMERA_KIND,
				"folder": str(recording / k.get("cameraName", "x"))})
			ring.__init__(self, name, *a, **k)

	monkeypatch.setattr(livefeed, "RingWriter", Described)
	(tmp_path / "calibration.toml").write_text("") # its hash only: no 3D here
	out = str(tmp_path / "live")
	code = test_livefeed.run_live(test_livefeed.name(), out, ["--model", "none", "--report-sec", "0",
		"--engine", "off"], frames=60)
	assert code == 0
	data = strict_json((recording / "campy-live-session.json").read_text())
	assert data["options"]["--wait-sec"] == {"value": "Infinity", "source": "default"}
	assert data["schema"] == "campy-live-session/1" and data["status"] == "complete"
	assert data["recording"] == str(recording) and data["session"] == "session.json"
	assert data["options"]["--engine"] == {"value": "off", "source": "command line"}
	assert data["options"]["--fixed-crops"]["source"] == "default"
	assert data["options"]["--fixed-crops"]["defaultChanged"]["in"] == "2.1.0"
	assert data["feed"]["cameras"] == ["Camera0", "Camera1", "Camera2"]
	assert data["end"]["log"] == out + ".npy" and data["end"]["sets"] > 0
	assert json.loads(open(out + ".json").read())["session"] == str(recording / "campy-live-session.json")
	# A second campy-live on the same recording gets a file of its own
	assert manifest.LiveSession.Place(str(recording), out).endswith("campy-live-session_2.json")
	found = manifest.Read(str(recording))
	assert found["session"]["schema"] == manifest.SCHEMA and found["live"][0]["status"] == "complete"


def test_campy_live_tracks_from_the_first_frame_while_its_session_is_written(tmp_path, monkeypatch, capsys):
	# What campy-live-session.json says of campy, the computer and the models
	# (git, nvidia-smi, PowerShell, best.ckpt's SHA-256) can take seconds: it is
	# read and written from threads, and the first set is not kept waiting
	# for it. Waited for, the first 1.5 s went untracked (frame 68 at 60 fps
	# on the Precision 7875).
	import test_livefeed
	campy_info = manifest.CampyInfo
	def SlowCampyInfo():
		time.sleep(1.5)
		return campy_info()
	monkeypatch.setattr(manifest, "CampyInfo", SlowCampyInfo)
	out = str(tmp_path / "live")
	assert test_livefeed.run_live(test_livefeed.name(), out, ["--model", "none", "--report-sec", "0",
		"--wait-sec", "30", "--frame-shape", "16x20x1"], frames=120, rate=60, delay=0.1) == 0
	rows = np.load(out + ".npy")
	assert rows["frame"][0] <= 3 and rows["frame"][-1] == 120
	# The file is the same as when written before the first set
	data = strict_json(open(out + ".session.json").read())
	assert data["options"]["--wait-sec"] == {"value": 30.0, "source": "command line"}
	assert data["status"] == "complete" and data["campy"]["path"].endswith("campy")
	assert data["host"]["packages"]["numpy"] and data["models"] == {} and data["end"]["sets"] == len(rows)
	assert json.loads(open(out + ".json").read())["session"] == out + ".session.json"
	assert "How this run is made" in capsys.readouterr().out


def test_campy_live_without_a_recording_folder_writes_beside_its_log(tmp_path):
	assert manifest.LiveSession.Place(None, str(tmp_path / "live")) == str(tmp_path / "live") + ".session.json"


# ------------------------------------------------------------------ campy-live-export

def test_the_export_keeps_both_manifests_and_says_a_different_calibration(tmp_path, capsys):
	from campy import liveexport
	log = str(tmp_path / "live.npy")
	(tmp_path / "session.json").write_text(json.dumps({"schema": manifest.SCHEMA,
		"experiment": {"animalIds": ["M1", "M2"]}}))
	(tmp_path / "used.toml").write_text("used")
	(tmp_path / "other.toml").write_text("other")
	(tmp_path / "campy-live-session.json").write_text(json.dumps({"schema": manifest.LIVE_SCHEMA, "log": log,
		"calibration": manifest.FileRecord(str(tmp_path / "used.toml"))}))
	provenance = liveexport.Provenance(str(tmp_path), log, str(tmp_path / "used.toml"))
	assert provenance["session"]["schema"] == manifest.SCHEMA
	assert provenance["campy-live-session"]["log"] == log and provenance["animalIds"] == ["M1", "M2"]
	assert "calibrationDiffers" not in provenance
	provenance = liveexport.Provenance(str(tmp_path), log, str(tmp_path / "other.toml"))
	assert "is not the one campy-live tracked with" in capsys.readouterr().out
	assert provenance["calibrationDiffers"]["campyLive"] == manifest.Sha256(str(tmp_path / "used.toml"))
	json.dumps(provenance) # sleap-io keeps it as JSON


def test_a_manifest_that_cannot_be_written_never_stops_campy_live(tmp_path, monkeypatch, capsys):
	import test_livefeed
	def Fail(*a, **k):
		raise RuntimeError("disk full")
	monkeypatch.setattr(manifest.LiveSession, "Start", Fail)
	out = str(tmp_path / "live")
	assert test_livefeed.run_live(test_livefeed.name(), out, ["--model", "none", "--report-sec", "0",
		"--wait-sec", "30"], frames=60) == 0
	assert "Could not write campy-live-session.json: disk full. Tracking goes on." in capsys.readouterr().out
	assert json.loads(open(out + ".json").read())["session"] is None
