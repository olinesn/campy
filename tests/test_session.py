"""
campy-session (campy/session.py): one command that starts campy-live,
campy-live-view and campy-acquire, from one config, and ends them together,
the recording first. End to end with emulated cameras, and campy-live
--model none (no torch), this test's python standing in for campy-live's.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import types
from datetime import datetime

import numpy as np
import pytest
import yaml

from campy import session
from test_emu import write_clip

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="signals and process groups as on Linux and macOS")


@pytest.fixture
def home(tmp_path, monkeypatch):
	# ~/.campy of the test's own: the lock, envs.json and the status
	h = tmp_path / "home"
	h.mkdir()
	monkeypatch.setenv("HOME", str(h))
	monkeypatch.setenv("USERPROFILE", str(h))
	monkeypatch.delenv("CAMPY_LIVE_PYTHON", raising=False)
	return h


def write_config(path, acquire, **sections):
	text = yaml.safe_dump(acquire, sort_keys=False)
	for name, value in sections.items():
		text += yaml.safe_dump({name: value}, sort_keys=False)
	path.write_text(text)
	return path


# ------------------------------------------------------------------ the config

def test_the_sections_are_checked_and_taken_out(tmp_path):
	config = write_config(tmp_path / "c.yaml", {"numCams": 2, "videoFolder": "x", "liveFeedName": "f"},
		live={"model": "none", "animals": 3, "env": {"CUDA_VISIBLE_DEVICES": 0}}, view=False)
	c = session.Config(str(config), ["--recTimeInSec", "60"])
	assert c.live["model"] == "none" and c.live["animals"] == 3 and c.live["required"] is False
	assert c.live["restart"] is True and c.live["args"] == []
	assert c.view["show"] is False
	assert c.Value("recTimeInSec") == 60 and c.Value("numCams") == 2 and c.Value("frameRate") == 100 # campy's default
	acquire = yaml.safe_load(c.AcquireYaml()) # every setting, the command line's, the feed on for campy-live
	assert acquire["liveFeed"] is True and acquire["recTimeInSec"] == 60 and acquire["frameRate"] == 100
	assert "extends" not in acquire
	assert c.Cameras() == (2, ["Camera0", "Camera1"])


def test_a_typo_in_a_section_says_what_was_meant(tmp_path):
	config = write_config(tmp_path / "c.yaml", {"numCams": 1}, live={"model": "none", "calibraton": "c.toml"})
	with pytest.raises(session.SessionError, match=r'live\.calibraton: .* Did you mean "calibration"\?'):
		session.Config(str(config))


def test_wrong_kinds_are_refused(tmp_path):
	config = write_config(tmp_path / "c.yaml", {"numCams": 1}, live={"model": "none", "animals": "five"})
	with pytest.raises(session.SessionError, match=r"live\.animals: 'five' is not a whole number"):
		session.Config(str(config))
	config = write_config(tmp_path / "c.yaml", {"numCams": 1}, live={"model": "none", "required": 1})
	with pytest.raises(session.SessionError, match=r"live\.required: 1 is not True or False"):
		session.Config(str(config))


def test_yaml_on_as_a_key_is_explained(tmp_path):
	(tmp_path / "c.yaml").write_text("numCams: 1\nview:\n  on: false\n") # YAML reads on as True
	with pytest.raises(session.SessionError, match=r"the key True is not text .* It takes: show, args, keep_open"):
		session.Config(str(tmp_path / "c.yaml"))


def test_trained_models_need_both_folders(tmp_path):
	config = write_config(tmp_path / "c.yaml", {"numCams": 1}, live={"centroid": "c"})
	with pytest.raises(session.SessionError, match="instance"):
		session.Config(str(config))


def test_campy_session_sets_the_folder_and_stop_file_itself(tmp_path):
	config = write_config(tmp_path / "c.yaml", {"numCams": 1})
	with pytest.raises(session.SessionError, match="sessionPath: campy-session sets it itself"):
		session.Config(str(config), ["--sessionPath", "x"])


def test_without_a_live_section_the_view_is_off_and_the_feed_as_configured(tmp_path):
	c = session.Config(str(write_config(tmp_path / "c.yaml", {"numCams": 1})))
	assert c.live is None and c.view["show"] is False and not c.Feed()
	assert yaml.safe_load(c.AcquireYaml())["liveFeed"] is False


def test_campy_lives_options_come_from_the_config(tmp_path):
	config = write_config(tmp_path / "c.yaml", {"numCams": 3, "liveFeedName": "rig", "frameHeight": 1024,
		"frameWidth": 1280, "pixelFormatInput": "gray"}, live={"centroid": "C", "instance": "I", "animals": 4,
		"calibration": "cal.toml", "args": ["--engine", "off"]})
	s = session.Session(session.Config(str(config)), live_python="py")
	s.folder, s.logs = str(tmp_path), str(tmp_path / "logs")
	command = s.LiveCommand(1)
	text = " ".join(command)
	for part in ("py -m campy.live --cameras 3 --name rig --model sleap", "--centroid C --instance I",
			"--animals 4 --max-instances 4", "--calibration cal.toml", "--frame-shape 1024x1280x1", "--engine off",
			"--ready-file", "--stop-file", "--wait-sec 600"):
		assert part in text, (part, text)
	assert command[command.index("--out") + 1] == str(tmp_path / "campy-live")
	assert s.LiveCommand(2)[s.LiveCommand(2).index("--out") + 1] == str(tmp_path / "campy-live.part2")


# ------------------------------------------------------------------ campy-live's environment

def test_campy_lives_python_is_found_in_order(home, monkeypatch):
	with pytest.raises(session.SessionError, match="campy-session register-live"):
		session.LivePython({"python": ""})
	envs = home / ".campy" / "envs.json"
	envs.parent.mkdir()
	envs.write_text(json.dumps({"live": {"python": "/registered/python"}}))
	assert session.LivePython({"python": ""}) == ("/registered/python", "registered in {}".format(envs))
	monkeypatch.setenv("CAMPY_LIVE_PYTHON", "/from/env")
	assert session.LivePython({"python": ""})[0] == "/from/env"
	assert session.LivePython({"python": "/from/config"})[0] == "/from/config"


def test_register_live_writes_this_python(home, capsys):
	assert session.Register([]) == 0
	envs = json.loads((home / ".campy" / "envs.json").read_text())
	assert envs["live"]["python"] == sys.executable
	assert "written to" in capsys.readouterr().out


def test_the_live_environment_must_run_the_same_feed_code(tmp_path):
	env = dict(os.environ, PYTHONPATH=REPO)
	found = session.CheckLiveEnv(sys.executable, env)
	assert found["feed"] == session.FeedCode()[1]
	other = tmp_path / "other"
	shutil.copytree(os.path.join(REPO, "campy"), str(other / "campy"))
	with open(str(other / "campy" / "livefeed.py"), "a") as f:
		f.write("\n# another campy\n")
	with pytest.raises(session.SessionError, match="runs another campy than this one"):
		session.CheckLiveEnv(sys.executable, dict(os.environ, PYTHONPATH=str(other)))


def test_one_campy_session_at_a_time(home):
	lock = session.Lock().Take()
	try:
		with open(lock.path, "w") as f:
			f.write("{} x\n".format(os.getppid())) # another live process holds it
		with pytest.raises(session.SessionError, match="Another campy-session is running"):
			session.Lock().Take()
		with open(lock.path, "w") as f:
			f.write("999999999 x\n") # one that ended without removing it
		session.Lock().Take().Release()
	finally:
		lock.Release()


# ------------------------------------------------------------------ end to end

NAMES = ["Camera0", "Camera1"]


def emu_session(tmp_path, home, rec_s, live=None, view=False, feed="campy-test-session", post=None):
	clips = tmp_path / "clips"
	for name in NAMES:
		(clips / name).mkdir(parents=True)
		write_clip(str(clips / name / "clip.mp4"), frames=7)
	acquire = {"numCams": len(NAMES), "cameraMake": "emu", "videoFolder": clips.as_posix(),
		"videoFilename": "emuclip.mp4", "frameRate": 20, "recTimeInSec": rec_s, "chunkLengthInSec": 1,
		"displayFrameRate": 0, "pixelFormatInput": "gray", "pixelFormatOutput": "yuv420p", "gpuID": -1,
		"codec": "h264", "preflight": "off", "liveFeedName": "{}-{}".format(feed, os.getpid())}
	sections = {"view": view, "post": post if post is not None else {"remux": False, "export": False}}
	if live is not None:
		sections["live"] = dict({"python": sys.executable}, **live)
	return write_config(tmp_path / "config.yaml", acquire, **sections), clips


def start(tmp_path, home, config, *args, group=False):
	env = dict(os.environ, PYTHONPATH=REPO, PYTHONUNBUFFERED="1", HOME=str(home), USERPROFILE=str(home))
	env.pop("IMAGEIO_FFMPEG_EXE", None)
	env.pop("CAMPY_LIVE_PYTHON", None)
	proc = subprocess.Popen([sys.executable, "-m", "campy.session", "run", str(config)] + list(args), cwd=str(tmp_path),
		env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=group)
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


def the_folder(clips):
	folder, = [d for d in clips.iterdir() if re.fullmatch(r"\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d(_\d+)?", d.name)]
	return folder


def frames(folder, name):
	import imageio
	video = imageio.get_reader(str(folder / name / "emuclip.mp4"))
	try:
		return video.count_frames()
	finally:
		video.close()


def recorded(folder):
	# Each camera: its video's frames, which match its hosttimes.npy
	counts = []
	for name in NAMES:
		n = frames(folder, name)
		assert n == len(np.load(str(folder / name / "hosttimes.npy"))), name
		counts.append(n)
	return counts


def test_a_tracked_recording_in_one_command(tmp_path, home):
	config, clips = emu_session(tmp_path, home, 3, live={"model": "none"})
	proc, lines = start(tmp_path, home, config)
	proc.wait(timeout=120)
	out = "".join(lines)
	assert proc.returncode == 0, out
	folder = the_folder(clips)
	assert recorded(folder) == [60, 60]
	# Everything in the recording's folder, each program's output marked here
	logs = folder / "logs"
	assert sorted(p.name for p in logs.iterdir()) == ["campy-acquire.log", "campy-acquire.yaml", "campy-live.log",
		"campy-live.ready", "campy-watch.log", "post.json", "post.log", "session-config.original.yaml",
		"session-config.yaml"]
	assert "campy-watch: watching Camera0, Camera1." in (logs / "campy-watch.log").read_text()
	assert "campy-watch: the recording ended." in (logs / "campy-watch.log").read_text()
	assert "[acquire] Camera0 saved 60 frames" in out and "[live]" in out
	assert "campy-live is ready" in out and "campy-live finished: the feed ended." in out
	ran = yaml.safe_load((logs / "campy-acquire.yaml").read_text())
	assert ran["liveFeed"] is True and ran["recTimeInSec"] == 3
	assert "live:" in (logs / "session-config.original.yaml").read_text()
	assert yaml.safe_load((logs / "session-config.yaml").read_text())["live"]["model"] == "none"
	summary = json.loads((folder / "campy-live.json").read_text())
	assert summary["ended"] == "the recording ended" and summary["sets"] > 30
	status = json.loads((folder / "launcher.json").read_text())
	assert status["state"] == "done" and status["parts"]["acquire"]["exit_code"] == 0
	assert status["parts"]["watch"]["exit_code"] == 0
	assert status["live"][0]["exit_code"] == 0 and status["problems"] == []
	assert json.loads((home / ".campy" / "status" / "launcher.json").read_text())["folder"] == str(folder)
	assert not (home / ".campy" / "status" / "launcher.lock").exists()


def test_campy_acquire_alone_with_logs(tmp_path, home):
	# No live section: campy-acquire alone, its log in the recording's folder
	config, clips = emu_session(tmp_path, home, 2)
	proc, lines = start(tmp_path, home, config)
	proc.wait(timeout=120)
	assert proc.returncode == 0, "".join(lines)
	folder = the_folder(clips)
	assert recorded(folder) == [40, 40]
	assert not (folder / "campy-live.npy").exists()


@POSIX
def test_ctrl_c_stops_the_recording_and_campy_live_tracks_to_its_end(tmp_path, home):
	# Ctrl-C in the console: every process of the console's group gets it, as a terminal sends it.
	# campy-acquire stops its cameras; campy-live, apart from it, ends with the feed.
	config, clips = emu_session(tmp_path, home, 60, live={"model": "none"})
	proc, lines = start(tmp_path, home, config, group=True)
	try:
		assert wait_for(lines, r"\[acquire\] Camera1 collected 40 frames")
		os.killpg(proc.pid, signal.SIGINT)
		proc.wait(timeout=60)
	finally:
		if proc.poll() is None:
			os.killpg(proc.pid, signal.SIGKILL)
	out = "".join(lines)
	assert proc.returncode == 0, out
	folder = the_folder(clips)
	counts = recorded(folder)
	assert 40 <= min(counts) and max(counts) < 400, counts
	summary = json.loads((folder / "campy-live.json").read_text())
	assert summary["ended"] == "the recording ended", summary
	assert not (folder / "logs" / "campy-acquire.stop").exists() # campy-acquire removed it


@POSIX
def test_campy_live_that_dies_is_started_again_and_the_recording_goes_on(tmp_path, home):
	config, clips = emu_session(tmp_path, home, 16, live={"model": "none"})
	proc, lines = start(tmp_path, home, config)
	try:
		assert wait_for(lines, r"\[live\] .*Logging to")
		folder = the_folder(clips)
		pid = json.loads((folder / "launcher.json").read_text())["live"][0]["pid"]
		os.kill(pid, signal.SIGKILL)
		proc.wait(timeout=120)
	finally:
		if proc.poll() is None:
			proc.kill()
	out = "".join(lines)
	assert proc.returncode == 0, out
	assert recorded(folder) == [320, 320]
	assert "campy-live stopped while the cameras record (exit code -9)" in out
	assert "Starting campy-live again (campy-live.part2)" in out
	status = json.loads((folder / "launcher.json").read_text())
	assert status["live_restarts"] == 1 and status["live"][1]["exit_code"] == 0
	assert json.loads((folder / "campy-live.part2.json").read_text())["ended"] == "the recording ended"


@POSIX
def test_without_restart_campy_live_stays_down_and_the_recording_goes_on(tmp_path, home):
	config, clips = emu_session(tmp_path, home, 5, live={"model": "none", "restart": False})
	proc, lines = start(tmp_path, home, config)
	try:
		assert wait_for(lines, r"\[live\] .*Logging to")
		folder = the_folder(clips)
		os.kill(json.loads((folder / "launcher.json").read_text())["live"][0]["pid"], signal.SIGKILL)
		proc.wait(timeout=120)
	finally:
		if proc.poll() is None:
			proc.kill()
	out = "".join(lines)
	assert proc.returncode == 0, out
	assert recorded(folder) == [100, 100]
	assert "It is not started again (live.restart is false)." in out


@POSIX
def test_the_recording_outlives_campy_session(tmp_path, home):
	# campy-session killed mid-recording: campy-acquire records on to recTimeInSec, and campy-live to its end
	config, clips = emu_session(tmp_path, home, 6, live={"model": "none"})
	proc, lines = start(tmp_path, home, config)
	assert wait_for(lines, r"\[acquire\] Camera1 collected 20 frames")
	folder = the_folder(clips)
	status = json.loads((folder / "launcher.json").read_text())
	acquire, live = status["parts"]["acquire"]["pid"], status["live"][0]["pid"]
	proc.kill()
	proc.wait()
	import psutil
	for pid in (acquire, live):
		try:
			psutil.Process(pid).wait(timeout=90)
		except psutil.NoSuchProcess:
			pass
	assert recorded(folder) == [120, 120]
	assert json.loads((folder / "campy-live.json").read_text())["ended"] == "the recording ended"


def test_campy_live_that_cannot_start_leaves_the_recording_alone_or_stops_it(tmp_path, home):
	bad = {"centroid": str(tmp_path / "no-such-model"), "instance": str(tmp_path / "no-such-model")}
	config, clips = emu_session(tmp_path, home, 2, live=dict(bad, required=True))
	# This python may have no torch or sleap-nn: campy-session then stops before anything starts; else campy-live does
	proc, lines = start(tmp_path, home, config)
	proc.wait(timeout=120)
	out = "".join(lines)
	assert proc.returncode == 1, out
	assert "No recording: campy-live did not start" in out or "has no torch or sleap-nn" in out
	assert not any((d / "Camera0").exists() for d in clips.iterdir() if d.is_dir())


def test_campy_live_that_cannot_start_and_is_not_required(tmp_path, home):
	# --model random needs torch; without it campy-live stops as it starts, and the cameras record anyway
	pytest.importorskip("yaml")
	try:
		import torch # noqa: F401
		pytest.skip("this python has torch: --model random would start")
	except ImportError:
		pass
	config, clips = emu_session(tmp_path, home, 2, live={"model": "random"})
	proc, lines = start(tmp_path, home, config)
	proc.wait(timeout=120)
	out = "".join(lines)
	assert proc.returncode == 0, out
	assert "campy-live stopped as it started" in out and "Recording without tracking." in out
	assert recorded(the_folder(clips)) == [40, 40]


def test_a_config_on_a_profile(tmp_path, monkeypatch):
	# extends: a rig profile in ~/.campy/configs (CAMPY_CONFIG_DIR): its keys, and its sections combined one level
	# deep, as campy-acquire combines them; the config campy-acquire runs is one file, profile included
	profiles = tmp_path / "profiles"
	profiles.mkdir()
	monkeypatch.setenv("CAMPY_CONFIG_DIR", str(profiles))
	write_config(profiles / "rig.yaml", {"numCams": 3, "frameRate": 60, "liveFeedName": "rigfeed"},
		live={"model": "sleap", "centroid": "C", "instance": "I", "animals": 5}, post={"remux": "keep"})
	config = write_config(tmp_path / "today.yaml", {"extends": "rig", "recTimeInSec": 600}, live={"animals": 3})
	c = session.Config(str(config))
	assert c.Value("numCams") == 3 and c.Value("frameRate") == 60 and c.Value("recTimeInSec") == 600
	assert (c.live["centroid"], c.live["instance"], c.live["animals"]) == ("C", "I", 3)
	assert c.post["remux"] == "keep"
	ran = yaml.safe_load(c.AcquireYaml())
	assert "extends" not in ran and ran["numCams"] == 3 and ran["liveFeedName"] == "rigfeed" and ran["liveFeed"] is True


def test_view_true_or_false_is_short_for_show(tmp_path):
	for value in (True, False):
		c = session.Config(str(write_config(tmp_path / "c.yaml", {"numCams": 1}, view=value)))
		assert c.view["show"] is value


def test_a_typo_in_campy_acquires_keys_is_caught_before_anything_starts(tmp_path):
	with pytest.raises(session.SessionError, match="frameRate"):
		session.Config(str(write_config(tmp_path / "c.yaml", {"numCams": 1, "framerate": 60})))
	with pytest.raises(session.SessionError, match="campy-session sets it itself"):
		session.Config(str(write_config(tmp_path / "c.yaml", {"numCams": 1, "stopFile": "x"})))


def test_campy_live_stops_waiting_when_campy_session_ended_first(tmp_path):
	# campy-session killed while campy-live warms up: campy-live does not wait live.wait_s for a feed that never comes
	stand_in = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
	threading.Thread(target=stand_in.wait, daemon=True).start() # reaped as it ends, as a shell reaps campy-session
	env = dict(os.environ, PYTHONPATH=REPO, PYTHONUNBUFFERED="1")
	t0 = time.monotonic()
	proc = subprocess.run([sys.executable, "-m", "campy.live", "--model", "none", "--cameras", "1", "--name",
		"campy-test-orphan-{}".format(os.getpid()), "--wait-sec", "600", "--launcher-pid", str(stand_in.pid)],
		cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60)
	stand_in.wait()
	assert proc.returncode == 1 and time.monotonic() - t0 < 30, proc.stdout + proc.stderr
	assert "the program that started campy-live (pid {}) has ended: stopped".format(stand_in.pid) in proc.stdout


def test_alive():
	from campy import children
	assert children.Alive(os.getpid())
	p = subprocess.Popen([sys.executable, "-c", "pass"])
	p.wait()
	assert not children.Alive(p.pid) and not children.Alive(0)


def test_after_a_clean_stop_the_post_steps_run(tmp_path, home):
	from campy import post
	dest = tmp_path / "nas"
	marker = tmp_path / "ran.txt"
	command = "{} -c \"import sys; open(sys.argv[1], 'w').write(sys.argv[2])\" {} {{folder}}".format(
		'"{}"'.format(sys.executable) if sys.platform == "win32" else sys.executable, marker)
	config, clips = emu_session(tmp_path, home, 2, live={"model": "none"},
		post={"remux": "keep", "export": False, "copy": str(dest), "run": [command]})
	proc, lines = start(tmp_path, home, config)
	proc.wait(timeout=180)
	out = "".join(lines)
	assert proc.returncode == 0, out
	folder = the_folder(clips)
	for name in NAMES:
		assert not post.Fragmented(str(folder / name / "emuclip.mp4"))
		assert post.Fragmented(str(folder / name / "emuclip.fragmented.mp4"))
		assert frames(folder, name) == 40
	assert "[post] remux: done, 2 videos remuxed to plain MP4, each checked packet by packet." in out
	assert (dest / folder.name / "Camera0" / "emuclip.mp4").is_file()
	assert marker.read_text() == str(folder)
	status = json.loads((folder / "launcher.json").read_text())
	assert status["state"] == "done"
	assert {k: v["state"] for k, v in status["post"].items()} == {"remux": "done", "join": "done", "export": "skipped",
		"copy": "done", "run": "done"}


def test_the_post_section_is_checked(tmp_path):
	config = write_config(tmp_path / "c.yaml", {"numCams": 1}, post={"remux": "yes please"})
	with pytest.raises(session.SessionError, match="post.remux: 'yes please' is not true, false or keep"):
		session.Config(str(config))
	config = write_config(tmp_path / "c.yaml", {"numCams": 1}, post={"copy": {"to": "x", "delete": True}})
	with pytest.raises(session.SessionError, match="post.copy: it takes to: only"):
		session.Config(str(config))
	c = session.Config(str(write_config(tmp_path / "c.yaml", {"numCams": 1})))
	assert c.post == {"remux": True, "join": True, "export": True, "copy": {}, "run": [], "ffmpeg": ""}


def test_a_campy_live_that_hangs_is_stopped_to_be_started_again(tmp_path, monkeypatch):
	# campy-watch's alert (alerts-live.log, key live/campy-live, open) about the campy-live running now: its stop file
	# is written, and once it has stopped it is started again as one that died; an alert from before it started is not
	config = write_config(tmp_path / "c.yaml", {"numCams": 1}, live={"model": "none"})
	s = session.Session(session.Config(str(config)))
	s.folder, s.logs, s.liveStop = str(tmp_path), str(tmp_path), str(tmp_path / "campy-live.stop")
	t0 = time.time() - 3600 # times of today: Windows' astimezone refuses those just after 1970
	live = types.SimpleNamespace(started=t0 + 1000.0, Running=lambda: True, End=lambda: ended.append(1))
	ended = []
	s.parts["live"] = live
	def alert(when, state="open"):
		with open(str(tmp_path / "alerts-live.log"), "a") as f:
			f.write(json.dumps({"time": datetime.fromtimestamp(when).astimezone().isoformat(), "key": "live/campy-live",
				"severity": "critical", "event": "problem", "state": state, "text": "no result for 30 s"}) + "\n")
	alert(t0 + 990.0) # about an earlier campy-live
	s.CheckHung(now=t0 + 1040.0)
	assert s.hung is None and not (tmp_path / "campy-live.stop").exists()
	alert(t0 + 1035.0)
	s.CheckHung(now=t0 + 1050.0)
	assert s.hung is live and (tmp_path / "campy-live.stop").read_text().strip() == "stop"
	monkeypatch.setattr(session, "Say", lambda text: None)
	live.code = -9
	s.CheckHung(now=t0 + 1050.0 + session.LIVE_STOP_S + 1) # it did not stop: ended, and started again
	assert ended == [1] and s.restart_due is not None and s.hung is None
	s.restart_due, s.hung, s.hung_at = None, live, t0 + 1050.0
	live.code = 0 # one that stopped at its stop file: started again too
	s.LiveDied(live)
	assert s.restart_due is not None and s.hung is None


def test_giving_up_on_campy_live_sends_an_alert(tmp_path, monkeypatch):
	config = write_config(tmp_path / "c.yaml", {"numCams": 1}, live={"model": "none"})
	s = session.Session(session.Config(str(config)))
	s.restarts.extend([time.monotonic()] * session.RESTARTS_PER_HOUR)
	sent = []
	monkeypatch.setattr(session, "Alert", lambda key, text, severity="critical": sent.append((key, severity)))
	s.LiveDied(types.SimpleNamespace(code=1, log="x.log"))
	assert sent == [("live/campy-live/restarts", "critical")] and s.restart_due is None


def test_without_watch_no_campy_watch(tmp_path, home):
	config, clips = emu_session(tmp_path, home, 2, live={"model": "none", "watch": False})
	proc, lines = start(tmp_path, home, config)
	proc.wait(timeout=120)
	assert proc.returncode == 0, "".join(lines)
	assert not (the_folder(clips) / "logs" / "campy-watch.log").exists()


@POSIX
def test_a_frozen_campy_live_is_found_by_campy_watch_and_replaced(tmp_path, home):
	# campy-live frozen (SIGSTOP) mid-recording: campy-watch says "no result for 30 s", campy-session stops it (its
	# stop file, then ended), and starts it again; the recording keeps every frame
	config, clips = emu_session(tmp_path, home, 110, live={"model": "none"}, feed="campy-test-hang")
	proc, lines = start(tmp_path, home, config)
	try:
		assert wait_for(lines, r"\[live\] .*Logging to")
		folder = the_folder(clips)
		pid = json.loads((folder / "launcher.json").read_text())["live"][0]["pid"]
		time.sleep(3)
		os.kill(pid, signal.SIGSTOP)
		proc.wait(timeout=200)
	finally:
		if proc.poll() is None:
			proc.kill()
	out = "".join(lines)
	assert proc.returncode == 0, out
	assert recorded(folder) == [2200, 2200]
	assert "campy-watch: campy-live made no result for 30 s while the cameras record" in out
	assert "Starting campy-live again (campy-live.part2)" in out
	status = json.loads((folder / "launcher.json").read_text())
	assert status["live_restarts"] == 1 and status["live"][1]["exit_code"] == 0
