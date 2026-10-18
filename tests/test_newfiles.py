"""
A recording never replaces one: campy-acquire stops before recording when
any file it would write is already there, whatever preflight is set to, and
a camera process refuses on its own. The old files stay byte for byte.
"""

import csv
import hashlib
import os
import subprocess
import sys
import time

import numpy as np
import pytest
import imageio_ffmpeg

from campy import configurator, newfiles, writer
from campy.cameras import unicam
from test_failures import FakeCamera, CameraProcess, fake_write_frames, params as camera_params

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CAMERA_FILES = ("0.mp4", "frametimes.npy", "hosttimes.npy", "frametimes.mat", "metadata.csv",
	"ratcam_frames.csv", "ratcam_sync.csv")


def config(tmp_path, folder, preflight="off", recTimeInSec=1, name="config.yaml"):
	# Two simulated RatCams: every file a recording writes, a RatCam's logs too
	path = tmp_path / name
	path.write_text("\n".join([
		'videoFolder: "{}"'.format(folder), 'videoFilename: "0.mp4"', "recTimeInSec: {}".format(recTimeInSec),
		"numCams: 2", 'cameraNames: ["EyeA", "EyeB"]', 'cameraMake: "ratcam"', 'ratcamPorts: ["sim", "sim"]',
		"frameRate: 30", "frameWidth: 160", "frameHeight: 120", "cameraExposureTimeInUs: 3000", "gpuID: -1",
		'pixelFormatOutput: "yuv420p"', "displayFrameRate: 0", 'preflight: "{}"'.format(preflight),
		'ffmpegLogLevel: "warning"', 'capacityFile: "{}"'.format(tmp_path / "none.json"), ""]))
	return path


def start(tmp_path, config_path, env=None, args=()):
	code = "import sys; sys.argv = ['campy-acquire'] + sys.argv[1:]; from campy.campy import Main; Main()"
	env = dict(dict(os.environ, PYTHONPATH=REPO, PYTHONUNBUFFERED="1"), **(env or {}))
	return subprocess.Popen([sys.executable, "-c", code, str(config_path)] + list(args), cwd=str(tmp_path), env=env,
		stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def acquire(tmp_path, config_path, env=None, timeout=120, args=()):
	campy = start(tmp_path, config_path, env, args)
	try:
		out, _ = campy.communicate(timeout=timeout)
	finally:
		if campy.poll() is None:
			campy.kill()
	return campy.returncode, out


def snapshot(folder):
	# Every file under folder: its path relative to folder, and its contents' hash
	found = {}
	for root, _, files in os.walk(folder):
		for name in files:
			path = os.path.join(root, name)
			with open(path, "rb") as f:
				found[os.path.relpath(path, folder)] = hashlib.sha256(f.read()).hexdigest()
	return found


def recorded(folder, names=("EyeA", "EyeB")):
	# A whole recording: every camera's files, none empty, and its video's frames
	for name in names:
		for file in CAMERA_FILES:
			assert os.path.getsize(os.path.join(folder, name, file)) > 0, (name, file)
		with open(os.path.join(folder, name, "metadata.csv")) as f:
			meta = dict(csv.reader(f))
		assert int(meta["framesWritten"]) > 0
		assert imageio_ffmpeg.count_frames_and_secs(os.path.join(folder, name, "0.mp4"))[0] == \
			int(meta["framesWritten"]) == np.load(os.path.join(folder, name, "frametimes.npy")).shape[1]
	assert os.path.getsize(os.path.join(folder, "trigger.json")) > 0


@pytest.fixture
def ratcams():
	pytest.importorskip("nireyecam")


# ------------------------------------------------------------------ a new folder records as before

@pytest.mark.parametrize("made", [False, True], ids=["missing", "empty"])
def test_a_new_or_empty_folder_records_as_before(tmp_path, ratcams, made):
	folder = tmp_path / "rec"
	if made:
		folder.mkdir()
	code, out = acquire(tmp_path, config(tmp_path, folder))
	assert code == 0, out
	assert "already holds a recording" not in out
	recorded(str(folder))
	# Nothing else, and nothing left empty
	expected = {os.path.join(name, file) for name in ("EyeA", "EyeB") for file in CAMERA_FILES} | {"trigger.json"}
	assert set(snapshot(folder)) == expected


# ------------------------------------------------------------------ a second recording into the same folder

@pytest.fixture(scope="module")
def first_recording(tmp_path_factory):
	# One recording, kept: each test copies it
	pytest.importorskip("nireyecam")
	tmp_path = tmp_path_factory.mktemp("first")
	code, out = acquire(tmp_path, config(tmp_path, tmp_path / "rec"))
	assert code == 0, out
	return tmp_path / "rec"


@pytest.mark.parametrize("preflight", ["error", "warn", "off"])
def test_a_second_recording_into_the_same_folder_stops_before_recording(tmp_path, first_recording, preflight):
	# The ChronicRecord PC, 2026-10-18: the second replaced the first's video,
	# frametimes.npy, hosttimes.npy and metadata.csv, without a word
	import shutil
	folder = tmp_path / "rec"
	shutil.copytree(first_recording, folder)
	before = snapshot(folder)
	code, out = acquire(tmp_path, config(tmp_path, folder, preflight=preflight))
	assert code == 1, out
	assert snapshot(folder) == before
	assert "Stopped before recording: {} already holds a recording, and campy never replaces one. These files are " \
		"there:\n".format(folder) in out
	assert "  trigger.json\n" in out
	for name in ("EyeA", "EyeB"):
		assert "  {}{}{}\n".format(name, os.sep, ", ".join(CAMERA_FILES)) in out
	assert "Record into a new folder (--videoFolder, or videoFolder in the config), or move this one away first.\n" in out
	# Before anything opened: no launch check, no camera
	assert "Launch check" not in out and "Opened" not in out


@pytest.mark.parametrize("existing", [os.path.join("EyeB", file) for file in CAMERA_FILES] + ["trigger.json"])
@pytest.mark.parametrize("preflight", ["error", "warn", "off"])
def test_any_one_file_already_there_stops_the_recording(tmp_path, existing, preflight):
	# Only that one file there, of an earlier recording; campy stops before
	# it looks for a camera, so no RatCam is needed
	folder = tmp_path / "rec"
	path = folder / existing
	path.parent.mkdir(parents=True)
	path.write_bytes(b"an earlier recording's " + existing.encode())
	code, out = acquire(tmp_path, config(tmp_path, folder, preflight=preflight))
	assert code == 1, out
	assert snapshot(folder) == {existing: hashlib.sha256(b"an earlier recording's " + existing.encode()).hexdigest()}
	assert "These files are there:\n  {}\n".format(existing) in out
	assert "Launch check" not in out


def test_a_new_videoFolder_on_the_command_line_records(tmp_path, first_recording):
	# What the message says to do: the config's folder holds a recording, the
	# one given on the command line is new
	before = snapshot(first_recording)
	code, out = acquire(tmp_path, config(tmp_path, first_recording), args=["--videoFolder", str(tmp_path / "rec2")])
	assert code == 0, out
	recorded(str(tmp_path / "rec2"))
	assert snapshot(first_recording) == before


# ------------------------------------------------------------------ two campys at once

def test_a_second_campy_while_the_first_records(tmp_path, ratcams):
	folder = tmp_path / "rec"
	first = start(tmp_path, config(tmp_path, folder, recTimeInSec=6))
	try:
		# Once the first is recording
		deadline = time.monotonic() + 60
		while not (folder / "EyeB" / "ratcam_frames.csv").exists() or \
				not os.path.getsize(folder / "EyeB" / "ratcam_frames.csv"):
			assert first.poll() is None and time.monotonic() < deadline, first.communicate()[0]
			time.sleep(0.1)
		code, out = acquire(tmp_path, config(tmp_path, folder, name="second.yaml"))
		assert code == 1, out
		assert "already holds a recording, and campy never replaces one" in out
		first_out, _ = first.communicate(timeout=60)
	finally:
		if first.poll() is None:
			first.kill()
	assert first.returncode == 0, first_out
	recorded(str(folder))
	with open(folder / "EyeA" / "metadata.csv") as f:
		assert dict(csv.reader(f))["framesWritten"] == "180" # all 6 s


def test_of_two_campys_claiming_one_folder_only_one_gets_it(tmp_path):
	# Both found the folder new (Check), and then claim it: the second stops,
	# and leaves the first's files as they are
	p = configurator.FillCameraLists(dict(configurator.DefaultParams(), videoFolder=str(tmp_path / "rec"),
		numCams=2, cameraMake="ratcam"))
	newfiles.Check(p)
	newfiles.Check(p)
	claimed = newfiles.Claim(p)
	assert sorted(path for path, n in claimed) == sorted(path for path, n in newfiles.RecordingFiles(p))
	(tmp_path / "rec" / "Camera1" / "metadata.csv").write_text("the first campy's")
	before = snapshot(tmp_path / "rec")
	with pytest.raises(newfiles.WouldOverwrite, match="already holds a recording"):
		newfiles.Claim(p)
	assert snapshot(tmp_path / "rec") == before


def test_a_claim_that_fails_removes_only_what_it_made(tmp_path):
	p = configurator.FillCameraLists(dict(configurator.DefaultParams(), videoFolder=str(tmp_path / "rec"),
		numCams=2, cameraMake="ratcam"))
	last = newfiles.RecordingFiles(p)[-1][0] # Camera1's ratcam_sync.csv: made after all the others
	os.makedirs(os.path.dirname(last))
	with open(last, "w") as f:
		f.write("pulse,t_us,host_ns\n")
	with pytest.raises(newfiles.WouldOverwrite) as e:
		newfiles.Claim(p)
	assert snapshot(tmp_path / "rec") == {os.path.join("Camera1", "ratcam_sync.csv"):
		hashlib.sha256(b"pulse,t_us,host_ns\n").hexdigest()}
	assert "These files are there:\n  Camera1{}ratcam_sync.csv\n".format(os.sep) in str(e.value)


def test_release_removes_only_empty_files(tmp_path):
	empty, full = tmp_path / "empty", tmp_path / "full"
	empty.write_bytes(b"")
	full.write_bytes(b"frames")
	newfiles.Release([(str(empty), 0), (str(full), 0), (str(tmp_path / "gone"), None)])
	assert not empty.exists() and full.read_bytes() == b"frames"


def test_two_cameras_with_one_name_would_write_the_same_files(tmp_path):
	p = configurator.FillCameraLists(dict(configurator.DefaultParams(), videoFolder=str(tmp_path),
		numCams=2, cameraNames=["Eye", "Eye"]))
	with pytest.raises(newfiles.WouldOverwrite, match="Eye and Eye would both write .*0.mp4. Give each camera "
			"its own name"):
		newfiles.Check(p)


# ------------------------------------------------------------------ the camera process refuses on its own

# Imported by every Python process the test starts: campy-acquire's own
# check and claim skipped, as if the files had come after them
SKIP_HOOK = """
import multiprocessing
if multiprocessing.current_process().name == "MainProcess":
	from campy import newfiles
	newfiles.Check = lambda params: None
	newfiles.Claim = lambda params: []
"""


def test_a_camera_process_refuses_files_already_there(tmp_path, ratcams):
	hook = tmp_path / "hook"
	hook.mkdir()
	(hook / "sitecustomize.py").write_text(SKIP_HOOK)
	folder = tmp_path / "rec"
	(folder / "EyeA").mkdir(parents=True)
	(folder / "EyeA" / "metadata.csv").write_text("an earlier recording's")
	(folder / "EyeB").mkdir()
	(folder / "EyeB" / "0.mp4").write_bytes(b"an earlier recording's video")
	before = snapshot(folder)
	code, out = acquire(tmp_path, config(tmp_path, folder), env=dict(PYTHONPATH=os.pathsep.join([str(hook), REPO])))
	assert code == 1, out
	# The old files as they were; only trigger.json is new, saying neither camera was ready
	after = snapshot(folder)
	assert set(after) == set(before) | {"trigger.json"} and {k: after[k] for k in before} == before
	for name, file in (("EyeA", "metadata.csv"), ("EyeB", "0.mp4")):
		assert "{}: not recording: {} already holds a recording, and campy never replaces one:\n  {}{}{}\n".format(
			name, folder, name, os.sep, file) in out
	# Neither opened its camera, nor started an ffmpeg
	assert "Opened" not in out
	assert "0 of 2 cameras are ready. Not ready: " in out # in the order they said so
	assert "EyeA (its files are already there)" in out and "EyeB (its files are already there)" in out
	assert "2 of 2 cameras did not record to the end:\n" in out


def test_check_camera(tmp_path):
	p = camera_params(tmp_path, cameraMake="ratcam")
	newfiles.CheckCamera(p) # nothing there
	os.makedirs(tmp_path / "Camera0")
	(tmp_path / "Camera0" / "ratcam_sync.csv").write_bytes(b"")
	newfiles.CheckCamera(p) # campy's own, empty (Claim)
	(tmp_path / "Camera0" / "ratcam_sync.csv").write_bytes(b"pulse")
	with pytest.raises(newfiles.WouldOverwrite, match="ratcam_sync.csv"):
		newfiles.CheckCamera(p)


@pytest.mark.parametrize("existing", ["0.mp4", "frametimes.npy", "hosttimes.npy", "frametimes.mat", "metadata.csv"])
def test_a_camera_writes_over_no_file(tmp_path, monkeypatch, existing):
	# The camera's own writers, without campy-acquire's checks before them:
	# each refuses a file that holds something
	folder = tmp_path / "Camera0"
	folder.mkdir()
	(folder / existing).write_bytes(b"an earlier recording's")
	started = []
	monkeypatch.setattr(writer, "write_frames", lambda *a, **k: started.append(1) or fake_write_frames()(*a, **k))
	run = CameraProcess(monkeypatch, camera_params(tmp_path), FakeCamera())
	run.Run()
	assert (folder / existing).read_bytes() == b"an earlier recording's"
	if existing == "0.mp4":
		assert not started # no ffmpeg
		assert run.recording.videoError.startswith("not recording: ")


def test_a_file_this_process_wrote_it_may_write_again(tmp_path):
	path = str(tmp_path / "metadata.csv")
	with newfiles.Open(path, "w") as f:
		f.write("as the camera starts")
	with newfiles.Open(path, "w") as f:
		f.write("with the totals")
	assert open(path).read() == "with the totals"
	other = tmp_path / "other.csv"
	other.write_text("someone else's")
	with pytest.raises(newfiles.WouldOverwrite):
		newfiles.Open(str(other), "w")
	assert other.read_text() == "someone else's"


def test_the_encode_tools_open_writers_as_before(tmp_path):
	# campy-encode-sweep and campy-encode-quality write into folders of their
	# own (mkdtemp), through OpenWriter without new: it claims nothing
	p = camera_params(tmp_path)
	os.makedirs(tmp_path / "Camera0")
	(tmp_path / "Camera0" / "0.mp4").write_bytes(b"a sweep's earlier stream")
	ffmpeg, _, _ = writer.OpenWriter(p, [])
	ffmpeg.close()
	(tmp_path / "Camera0" / "1.mp4").write_bytes(b"an earlier recording's video")
	with pytest.raises(newfiles.WouldOverwrite, match="1.mp4 is already there"):
		writer.OpenWriter(dict(p, videoFilename="1.mp4"), [], new=True)
	assert (tmp_path / "Camera0" / "1.mp4").read_bytes() == b"an earlier recording's video"
