"""cameraSerials: each camera name goes to the camera with that serial number, in whatever order the camera
driver lists the cameras; a serial that isn't connected stops campy before anything records."""

import csv
import os
import re
import subprocess
import sys

import numpy as np
import pytest

from campy import configurator
from campy.cameras import unicam


@pytest.fixture(autouse=True)
def home_elsewhere(tmp_path_factory, monkeypatch):
	home = str(tmp_path_factory.mktemp("home"))
	monkeypatch.setenv("HOME", home)
	monkeypatch.setenv("USERPROFILE", home)
	monkeypatch.setenv("CAMPY_CONFIG_DIR", str(tmp_path_factory.mktemp("profiles")))


def resolve(tmp_path, text):
	path = tmp_path / "rig.yaml"
	path.write_text("numCams: 3\ncameraMake: flir\ncameraNames: [Top, Side, Front]\n" + text)
	return configurator.Resolve(str(path))


def problems(tmp_path, text):
	with pytest.raises(configurator.ConfigError) as e:
		resolve(tmp_path, text)
	return str(e.value)


def test_serials_are_read_as_text(tmp_path):
	p = resolve(tmp_path, "cameraSerials: [19490066, '19060810', 18475377]\n").params
	assert p["cameraSerials"] == ["19490066", "19060810", "18475377"]


def test_no_serials_by_default(tmp_path):
	p = resolve(tmp_path, "").params
	assert p["cameraSerials"] == "None"
	assert p["cameraSelection"] == [0, 1, 2]


def test_one_serial_for_three_cameras_is_refused(tmp_path):
	assert "cameraSerials: '19490066' is one value for 3 cameras" in problems(tmp_path, "cameraSerials: 19490066\n")


def test_too_few_serials_are_refused(tmp_path):
	assert "cameraSerials has 2 values for 3 cameras" in problems(tmp_path, "cameraSerials: [1, 2]\n")


def test_a_serial_twice_is_refused(tmp_path):
	assert "cameraSerials lists 11 twice" in problems(tmp_path, "cameraSerials: [11, 22, 11]\n")


def test_serials_with_a_selection_too_are_refused(tmp_path):
	text = problems(tmp_path, "cameraSerials: [11, 22, 33]\ncameraSelection: [0, 1, 2]\n")
	assert "cameraSerials and cameraSelection both pick the cameras" in text


def params(serials, makes="flir"):
	return {"numCams": len(serials), "cameraMake": makes, "cameraNames": ["Top", "Side", "Front"][:len(serials)],
		"cameraSelection": list(range(len(serials))), "cameraSerials": serials}


def test_each_name_goes_to_its_serial_in_any_order():
	systems = {"flir": {"serials": ["333", "111", "222"]}}
	p = unicam.SelectBySerial(systems, params(["111", "222", "333"]))
	assert p["cameraSelection"] == [1, 2, 0]
	# the driver lists them the other way round next time: the names stay with their cameras
	systems = {"flir": {"serials": ["222", "333", "111"]}}
	assert unicam.SelectBySerial(systems, params(["111", "222", "333"]))["cameraSelection"] == [2, 0, 1]


def test_without_serials_nothing_changes():
	p = params(["a", "b"])
	p["cameraSerials"], p["cameraSelection"] = "None", [1, 0]
	assert unicam.SelectBySerial({"flir": {"serials": ["a", "b"]}}, p)["cameraSelection"] == [1, 0]


def test_a_missing_camera_is_named_with_the_serials_found():
	systems = {"flir": {"serials": ["111", "333"]}}
	with pytest.raises(unicam.CameraMissing) as e:
		unicam.SelectBySerial(systems, params(["111", "222", "333"]))
	text = str(e.value)
	assert "Side: its camera (serial 222) isn't connected" in text
	assert "2 flir cameras found: 111, 333." in text
	assert "Nothing was recorded." in text


def test_each_make_is_searched_on_its_own():
	systems = {"flir": {"serials": ["111"]}, "basler": {"serials": ["222"]}}
	p = unicam.SelectBySerial(systems, params(["222", "111"], makes=["basler", "flir"]))
	assert p["cameraSelection"] == [0, 0]


# ------------------------------------------------------------------ a recording, with emulated cameras (serials emu0, emu1)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write_clip(path, frames=10, size=(64, 48)):
	import imageio_ffmpeg
	w = imageio_ffmpeg.write_frames(path, size, fps=20, pix_fmt_in="gray", macro_block_size=16, ffmpeg_log_level="error")
	w.send(None)
	for i in range(frames):
		w.send(np.full((size[1], size[0]), 20 * i, np.uint8))
	w.close()


def acquire(tmp_path, serials):
	names = ["Top", "Side"]
	for name in names:
		(tmp_path / name).mkdir(exist_ok=True)
		write_clip(str(tmp_path / name / "clip.mp4"))
	config = tmp_path / "config.yaml"
	config.write_text("\n".join([
		"numCams: 2", 'cameraMake: "emu"', "cameraNames: [Top, Side]", "cameraSerials: {}".format(serials),
		'videoFolder: "{}"'.format(tmp_path.as_posix()), "frameRate: 20", "recTimeInSec: 1", "displayFrameRate: 0", 'videoFilename: "emuclip.mp4"',
		'pixelFormatInput: "gray"', 'pixelFormatOutput: "yuv420p"', "gpuID: -1", 'codec: "h264"', 'preflight: "off"',
		"alerts: False", ""]))
	env = dict(os.environ, PYTHONPATH=REPO, PYTHONUNBUFFERED="1")
	env.pop("IMAGEIO_FFMPEG_EXE", None)
	code = "import sys; sys.argv = ['campy-acquire', sys.argv[1]]; from campy.campy import Main; Main()"
	return subprocess.run([sys.executable, "-c", code, str(config)], cwd=str(tmp_path), env=env,
		capture_output=True, text=True, timeout=120)


def sessions(tmp_path):
	return [d for d in tmp_path.iterdir() if re.fullmatch(r"\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d(_\d+)?", d.name)]


def test_a_recording_names_each_camera_by_its_serial(tmp_path):
	proc = acquire(tmp_path, "[emu1, emu0]")
	assert proc.returncode == 0, proc.stdout + proc.stderr
	session, = sessions(tmp_path)
	for name, serial in (("Top", "emu1"), ("Side", "emu0")):
		with open(str(session / name / "metadata.csv")) as f:
			meta = dict(row for row in csv.reader(f) if len(row) == 2)
		assert meta["cameraSerialNo"] == serial, name


def test_a_missing_serial_stops_before_anything_records(tmp_path):
	proc = acquire(tmp_path, "[emu0, emu7]")
	assert proc.returncode == 1, proc.stdout + proc.stderr
	assert "Side: its camera (serial emu7) isn't connected, or isn't found." in proc.stdout
	assert "2 emu cameras found: emu0, emu1." in proc.stdout
	assert not any(s.glob("*/*.mp4") for s in sessions(tmp_path))
