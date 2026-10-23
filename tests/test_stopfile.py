"""
Stopping campy-acquire from another program (stopFile, campy/stopfile.py),
recording into a folder another program made (sessionPath), and
campy-live's --ready-file: what campy-session (the launcher) needs from the
programs it starts.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import types

import numpy as np
import pytest

from campy import configurator, newfiles, stopfile
from campy.cameras import unicam
from campy.utils import utils
from test_emu import write_clip
from test_failures import camera_params, hanging_camera_process

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------------ the stop file

def test_what_the_stop_file_asks_for(tmp_path):
	path = str(tmp_path / "stop")
	assert stopfile.Level(path) == 0
	open(path, "w").close()
	assert stopfile.Level(path) == 1 # an empty file is a stop
	stopfile.Write(path)
	assert stopfile.Level(path) == 1
	stopfile.Write(path, now=True)
	assert stopfile.Level(path) == 2
	with open(path, "w") as f:
		f.write("  NOW\r\n")
	assert stopfile.Level(path) == 2
	assert os.listdir(str(tmp_path)) == ["stop"] # Write leaves no temporary file


def test_stop_then_now_a_second_later(tmp_path):
	path = str(tmp_path / "stop")
	asked = []
	watch = stopfile.Watch(path, asked.append, every=0.05, again=0.5).Start()
	try:
		time.sleep(0.2)
		assert asked == []
		stopfile.Write(path, now=True) # "now" from the start: a stop first, as two quick Ctrl-Cs are one
		time.sleep(0.25)
		assert asked == [1]
		time.sleep(0.6)
		assert asked == [1, 2]
	finally:
		watch.Stop()
	assert not os.path.exists(path) # removed once acted on


def test_a_big_file_is_never_removed(tmp_path):
	path = tmp_path / "stop"
	path.write_text("x" * 1000)
	watch = stopfile.Watch(str(path), lambda level: None)
	assert watch.Look() == 1
	watch.Stop()
	assert path.exists()


def test_without_a_request_the_file_is_left(tmp_path):
	path = tmp_path / "stop"
	watch = stopfile.Watch(str(path), lambda level: None)
	watch.Look()
	path.write_text("stop")
	watch.Stop() # it never saw it: not its to remove
	assert path.exists()


def test_a_request_counts_as_ctrl_c(capsys):
	interrupts = utils.HandleKeyboardInterrupt()
	interrupts.Request(1, "stopFile x")
	assert interrupts.count == 1
	interrupts.Request(1, "stopFile x") # the same request again: nothing more
	assert interrupts.count == 1
	interrupts.Request(2, "stopFile x")
	assert interrupts.count == 2
	out = capsys.readouterr().out
	assert out.count("Stop requested (stopFile x)") == 1 and out.count("Stop now requested (stopFile x)") == 1


def test_stop_now_stops_a_camera_process_that_hangs(tmp_path):
	# As a second Ctrl-C does (test_failures): Camera0 closes, Camera1 hangs
	# as it closes, and "now" in the stop file stops it
	import multiprocessing as mp
	ctx = mp.get_context("spawn")
	said = []
	cameras = unicam.CameraProcesses(ctx, hanging_camera_process, camera_params(2), ctx.Queue(), log=said.append)
	cameras.grace = 0.5
	interrupts = types.SimpleNamespace(count=0)
	def Request(level):
		interrupts.count = max(interrupts.count, level)
	path = str(tmp_path / "stop")
	watch = stopfile.Watch(path, Request, every=0.05, again=0.5).Start()
	stopfile.Write(path)
	def again():
		time.sleep(2.0)
		stopfile.Write(path, now=True)
	threading.Thread(target=again, daemon=True).start()
	try:
		t0 = time.monotonic()
		failures = cameras.Wait(interrupts)
	finally:
		watch.Stop()
	assert time.monotonic() - t0 < 15
	assert failures == {"Camera1": ["stopped at the second Ctrl-C, before it had closed"]}
	assert not cameras.processes[1].is_alive()


def test_the_camera_loop_sees_the_request():
	value = types.SimpleNamespace(value=0)
	try:
		unicam.SetStopRequest(None)
		assert not unicam.StopRequested()
		unicam.SetStopRequest(value)
		assert not unicam.StopRequested()
		value.value = 1
		assert unicam.StopRequested()
	finally:
		unicam.SetStopRequest(None)


def test_session_path_and_stop_file_options(tmp_path):
	config = tmp_path / "c.yaml"
	config.write_text("numCams: 1\n")
	parser = configurator.ArgumentParser()
	clargs = configurator.ParseClargs(parser, [str(config), "--sessionPath", "D:\\data\\run 1", "--stopFile",
		"C:\\tmp\\stop"])
	assert clargs.sessionPath == "D:\\data\\run 1" and clargs.stopFile == "C:\\tmp\\stop" # as typed
	p = configurator.DefaultParams()
	assert p["sessionPath"] == "None" and p["stopFile"] == "None"
	assert newfiles.GivenSession(p) is None and stopfile.Path(p) is None
	p.update(sessionPath=str(tmp_path / "run"), stopFile=str(tmp_path / "stop"))
	assert newfiles.GivenSession(p) == str(tmp_path / "run") and stopfile.Path(p) == str(tmp_path / "stop")
	stopfile.Check(p)
	(tmp_path / "stop").write_text("stop")
	with pytest.raises(stopfile.AlreadyThere, match="is already there"):
		stopfile.Check(p)


# ------------------------------------------------------------------ campy-acquire, end to end, emulated cameras

NAMES = ["Camera0", "Camera1"]
ACQUIRE = "import sys; sys.argv = ['campy-acquire'] + sys.argv[1:]; from campy.campy import Main; Main()"


def emu_config(tmp_path, *extra):
	clips = tmp_path / "clips"
	for name in NAMES:
		(clips / name).mkdir(parents=True)
		write_clip(str(clips / name / "clip.mp4"), frames=7)
	config = tmp_path / "config.yaml"
	config.write_text("\n".join([
		"numCams: {}".format(len(NAMES)), 'cameraMake: "emu"', 'videoFolder: "{}"'.format(clips.as_posix()),
		'videoFilename: "emuclip.mp4"', "frameRate: 20", "chunkLengthInSec: 1", "displayFrameRate: 0",
		'pixelFormatInput: "gray"', 'pixelFormatOutput: "yuv420p"', "gpuID: -1", 'codec: "h264"', 'preflight: "off"',
		] + list(extra) + [""]))
	return config


def start_acquire(tmp_path, config, *args):
	env = dict(os.environ, PYTHONPATH=REPO, PYTHONUNBUFFERED="1")
	env.pop("IMAGEIO_FFMPEG_EXE", None)
	env.pop(newfiles.SESSION_ENV, None)
	proc = subprocess.Popen([sys.executable, "-c", ACQUIRE, str(config)] + list(args), cwd=str(tmp_path), env=env,
		stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
	lines = []
	threading.Thread(target=lambda: lines.extend(proc.stdout), daemon=True).start()
	return proc, lines


def wait_for(lines, pattern, timeout=60):
	t0 = time.monotonic()
	while time.monotonic() - t0 < timeout:
		if any(re.search(pattern, line) for line in list(lines)):
			return True
		time.sleep(0.1)
	return False


def frames(folder, name):
	import imageio
	video = imageio.get_reader(str(folder / name / "emuclip.mp4"))
	try:
		return video.count_frames()
	finally:
		video.close()


def test_a_stop_file_stops_the_recording_as_ctrl_c_does(tmp_path):
	# A 60 s recording, stopped by its stop file after a few seconds: every
	# camera closes its video and saves its metadata, campy exits 0, and the
	# stop file is gone. Into a folder made beforehand (sessionPath), which
	# holds a file of the program that made it.
	session = tmp_path / "session"
	(session / "logs").mkdir(parents=True)
	(session / "logs" / "launcher.log").write_text("the launcher's own\n")
	stop = session / "stop.request"
	config = emu_config(tmp_path, "recTimeInSec: 60")
	proc, lines = start_acquire(tmp_path, config, "--sessionPath", str(session), "--stopFile", str(stop))
	try:
		assert wait_for(lines, r"Recording into {}".format(re.escape(str(session))))
		# Once frames are coming from both cameras: frametimes are written about once a second
		t0 = time.monotonic()
		while time.monotonic() - t0 < 60 and not all(
				(session / n / "hosttimes.npy").exists() and (session / n / "hosttimes.npy").stat().st_size > 200
				for n in NAMES):
			time.sleep(0.2)
		time.sleep(1)
		stopfile.Write(str(stop))
		proc.wait(timeout=60)
	finally:
		if proc.poll() is None:
			proc.kill()
	out = "".join(lines)
	assert proc.returncode == 0, out
	assert "Stop requested (stopFile {})".format(stop) in out
	assert not stop.exists()
	assert (session / "logs" / "launcher.log").read_text() == "the launcher's own\n"
	assert [d.name for d in (tmp_path / "clips").iterdir() if d.is_dir()] == NAMES # no date folder made there
	counts = []
	for name in NAMES:
		host = np.load(str(session / name / "hosttimes.npy"))
		n = frames(session, name)
		assert n == len(host), (name, n, len(host))
		counts.append(n)
	assert 20 < min(counts) and max(counts) < 60 * 20, counts # stopped long before recTimeInSec


def test_a_stop_file_already_there_stops_campy_before_anything_opens(tmp_path):
	stop = tmp_path / "stop"
	stop.write_text("stop")
	config = emu_config(tmp_path, "recTimeInSec: 5")
	proc, lines = start_acquire(tmp_path, config, "--stopFile", str(stop), "--sessionPath", str(tmp_path / "run"))
	proc.wait(timeout=60)
	out = "".join(lines)
	assert proc.returncode == 1, out
	assert "the stop file {} (stopFile) is already there".format(stop) in out
	assert stop.exists() # not campy's to remove: it never acted on it
	assert not (tmp_path / "run").exists()


def test_a_given_folder_that_holds_a_recording_is_refused(tmp_path):
	session = tmp_path / "run"
	(session / "Camera1").mkdir(parents=True)
	(session / "Camera1" / "emuclip.mp4").write_bytes(b"an earlier recording")
	config = emu_config(tmp_path, "recTimeInSec: 5")
	proc, lines = start_acquire(tmp_path, config, "--sessionPath", str(session))
	proc.wait(timeout=60)
	out = "".join(lines)
	assert proc.returncode == 1, out
	assert "already holds a recording" in out
	assert (session / "Camera1" / "emuclip.mp4").read_bytes() == b"an earlier recording"


def test_recording_to_rec_time_with_a_stop_file_that_never_comes(tmp_path):
	# The stop file changes nothing until it is there; sessionPath made by campy itself
	session = tmp_path / "made" / "by" / "campy"
	config = emu_config(tmp_path, "recTimeInSec: 2")
	proc, lines = start_acquire(tmp_path, config, "--sessionPath", str(session), "--stopFile", str(tmp_path / "stop"))
	proc.wait(timeout=90)
	out = "".join(lines)
	assert proc.returncode == 0, out
	assert "Stop requested" not in out
	for name in NAMES:
		assert frames(session, name) == 40


# ------------------------------------------------------------------ campy-live --ready-file

def test_campy_live_says_it_is_ready(tmp_path):
	# --model none needs no torch: it is ready at once, then finds no feed
	ready = tmp_path / "logs" / "campy-live.ready"
	env = dict(os.environ, PYTHONPATH=REPO, PYTHONUNBUFFERED="1")
	proc = subprocess.run([sys.executable, "-m", "campy.live", "--model", "none", "--cameras", "1", "--name",
		"campy-test-ready-{}".format(os.getpid()), "--wait-sec", "0.5", "--ready-file", str(ready)],
		cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60)
	assert proc.returncode == 1, proc.stdout + proc.stderr # no live feed
	said = json.loads(ready.read_text())
	assert said["pid"] > 0 and re.match(r"\d{4}-\d\d-\d\dT", said["time"])
	assert os.listdir(str(ready.parent)) == ["campy-live.ready"]
