"""
The recording watch in campy-acquire, end to end: emulated cameras, the
alerts file pointing at a stand-in for Slack and healthchecks.io on this
computer, and a fault injected into each recording. The Slack messages it
gets are what a person would read on their phone:
  - a normal recording: started, ended, the heartbeat pinged then paused
  - ffmpeg killed: that camera's alert, at once, and the end's summary
  - ffmpeg frozen (SIGSTOP) while the camera records: the frames waiting
    for it, then dropped frames, then resolved once it runs again
  - a camera unplugged (an emulated CameraLost)
  - every camera pausing at once (the trigger), then frames again
  - a disk filling up: the recording stops itself, cleanly, before it is
    full, and every video, frametimes.npy and metadata.csv is whole
"""

import csv
import json
import os
import re
import subprocess
import sys
import time

import numpy as np
import psutil
import pytest

from test_emu import write_clip
from test_notify import Service

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Shorter waits than a real recording's, so a test takes seconds: set in campy-acquire's main process, where
# the watch runs, before it starts
QUICK = """
from campy import watch, notify
watch.EVERY_S = 0.25
watch.DISK_EVERY_S = 0.25
watch.DISK_RATE_MIN_S = 1.0
watch.DROPS_CLEAR_S = 3.0
watch.STALL_CLEAR_S = 1.0
"""

# A disk that loses 0.5 GB a second from 3 GB, as the recording starts
FILLING_DISK = """
import time, types
_t0 = []
def _usage(path):
	_t0.append(time.monotonic())
	return types.SimpleNamespace(free=3e9 - 0.5e9 * (time.monotonic() - _t0[0]))
watch.DiskUsage = _usage
"""


@pytest.fixture
def service():
	s = Service()
	yield s
	s.server.shutdown()


def Start(tmp_path, service, names=("Camera0", "Camera1"), rec=6, faults="", extra="", **config):
	for name in names:
		if not (tmp_path / name / "clip.mp4").exists():
			(tmp_path / name).mkdir(exist_ok=True)
			write_clip(str(tmp_path / name / "clip.mp4"), frames=10)
	values = dict(numCams=len(names), cameraMake='"emu"', videoFolder='"{}"'.format(tmp_path.as_posix()),
		videoFilename='"emuclip.mp4"', frameRate=30, recTimeInSec=rec, chunkLengthInSec=100, displayFrameRate=0,
		pixelFormatInput='"gray"', pixelFormatOutput='"yuv420p"', gpuID=-1, codec='"h264"', preflight='"off"',
		alertNoFramesSec=1.5)
	values.update(config)
	(tmp_path / "config.yaml").write_text("".join("{}: {}\n".format(k, v) for k, v in values.items()))
	alerts = tmp_path / "alerts.json"
	alerts.write_text(json.dumps({"rig": "Test rig", "slack": {"token": "xoxb-test", "user": "U1",
		"api": service.url + "/api/", "min_severity": "info"}, "healthchecks": {"ping_url": service.url +
		"/0c5e9a8e-1234-4abc-9def-0123456789ab", "api_key": "k", "api_url": service.url + "/api/v3/"}}))
	env = dict(os.environ, PYTHONPATH=REPO, PYTHONUNBUFFERED="1", CAMPY_ALERTS_FILE=str(alerts),
		CAMPY_EMU_FAULTS=faults)
	env.pop("IMAGEIO_FFMPEG_EXE", None)
	code = QUICK + extra + "\nimport sys; sys.argv = ['campy-acquire', sys.argv[1]]\nfrom campy.campy import Main\nMain()\n"
	return subprocess.Popen([sys.executable, "-c", code, str(tmp_path / "config.yaml")], cwd=str(tmp_path), env=env,
		stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def Finish(proc, timeout=90):
	out, _ = proc.communicate(timeout=timeout)
	return proc.returncode, out


def Ffmpegs(proc, timeout=20):
	# The ffmpeg of each camera, by its video's folder: once every camera's is running
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		found = {}
		for child in psutil.Process(proc.pid).children(recursive=True):
			try:
				cmd = child.cmdline()
			except psutil.Error:
				continue
			if cmd and "ffmpeg" in os.path.basename(cmd[0]) and cmd[-1].endswith("emuclip.mp4"):
				found[os.path.basename(os.path.dirname(cmd[-1]))] = child
		if len(found) >= 2:
			return found
		time.sleep(0.1)
	raise AssertionError("no ffmpeg found")


def Session(tmp_path):
	(session,) = [d for d in tmp_path.iterdir() if re.fullmatch(r"\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d", d.name)]
	return session


def Texts(service):
	return [t.split("*Test rig*: ", 1)[1] for t in service.Texts()]


def test_a_recording_that_goes_well(tmp_path, service):
	code, out = Finish(Start(tmp_path, service, rec=3))
	assert code == 0, out
	texts = Texts(service)
	session = Session(tmp_path).name
	assert texts[0] == "Recording {} started: 2 cameras (Camera0, Camera1) at 30 fps, for 3 s, on an external " \
		"trigger.".format(session) + texts[0].split("external trigger.")[1]
	assert texts[-1] == "Recording {} ended after {}:\n- Camera0: 90 frames\n- Camera1: 90 frames".format(session,
		texts[-1].split("ended after ")[1].split(":")[0])
	assert len(texts) == 2, texts
	hc = [p for p in service.Paths() if not p.startswith("/api/chat")]
	assert hc[0] == "/0c5e9a8e-1234-4abc-9def-0123456789ab" and hc[-1].endswith("/pause")
	log = (Session(tmp_path) / "alerts.log").read_text()
	assert "sent to Slack U1" in log and "xoxb" not in log
	assert [json.loads(line)["event"] for line in log.splitlines() if json.loads(line)["status"] == "queued"] == \
		["note", "note"]
	# The state for a panel, as it ended
	status = json.load(open(os.path.join(os.environ["CAMPY_STATUS_DIR"], "recording.json")))
	assert status["state"] == "ended" and status["session"]["folder"] == str(Session(tmp_path))
	assert [(c["name"], c["frames"], c["dropped"], c["state"]) for c in status["cameras"]] == [
		("Camera0", 90, 0, "ended"), ("Camera1", 90, 0, "ended")]


def test_ffmpeg_killed(tmp_path, service):
	proc = Start(tmp_path, service, rec=8)
	ffmpegs = Ffmpegs(proc)
	time.sleep(1.5)
	ffmpegs["Camera1"].kill()
	killed = time.monotonic()
	while not any("Camera1: ffmpeg stopped" in t for t in Texts(service)) and time.monotonic() - killed < 10:
		time.sleep(0.05)
	took = time.monotonic() - killed
	code, out = Finish(proc)
	assert code == 1, out
	(alert,) = [t for t in Texts(service) if t.startswith("Camera1: ffmpeg stopped")]
	assert re.fullmatch(r"Camera1: ffmpeg stopped after taking \d+ frames, and the camera has stopped\. Its video can "
		r"be read up to its last fragment; the other cameras go on\.", alert), alert
	assert took < 3, took # within a second or two of it
	end = Texts(service)[-1]
	assert re.match(r"Recording \S+ ended after .*, 1 of 2 cameras not to the end:\n- Camera0: 240 frames\n- Camera1: "
		r"\d+ frames: ffmpeg stopped", end), end
	assert service.Texts()[-1].startswith(":red_circle:")


@pytest.mark.skipif(sys.platform == "win32", reason="SIGSTOP")
def test_ffmpeg_frozen(tmp_path, service):
	proc = Start(tmp_path, service, rec=14, writeQueueMaxSec=2)
	ffmpegs = Ffmpegs(proc)
	time.sleep(1.5)
	ffmpegs["Camera0"].suspend() # SIGSTOP: it takes no frames, as when the disk stops taking writes
	time.sleep(5)
	ffmpegs["Camera0"].resume()
	code, out = Finish(proc)
	texts = Texts(service)
	queue = [t for t in texts if t.startswith("Camera0: ") and "waiting for ffmpeg" in t]
	assert queue and "of writeQueueMaxSec 2 s); it took" in queue[0], texts
	assert any(t.startswith("worse: Camera0: ") for t in texts), texts
	assert any(t.startswith("Camera0: dropped ") for t in texts), texts
	assert any(t.startswith("resolved, after ") and "Camera0: caught up" in t for t in texts), texts
	assert any(t.startswith("resolved, after ") and "Camera0: no frame dropped for 3 s" in t for t in texts), texts
	assert not any("Camera1" in t for t in texts[1:-1]), texts
	assert re.search(r"- Camera0: 420 frames, \d+ dropped", texts[-1]), texts[-1]
	assert code == 0, out


def test_a_camera_unplugged(tmp_path, service):
	code, out = Finish(Start(tmp_path, service, rec=5, faults="1:lost@40"))
	assert code == 1, out
	assert "Camera1: the camera is gone (unplugged, or its USB link lost) after frame 40. Its recording has ended " \
		"there; the other cameras go on. It needs a replug, and a new recording." in Texts(service), Texts(service)


def test_the_trigger_pauses(tmp_path, service):
	code, out = Finish(Start(tmp_path, service, names=("Camera0", "Camera1", "Camera2"), rec=8,
		faults="*:stall@40+3.5"))
	assert code == 0, out
	texts = Texts(service)
	stopped = [t for t in texts if t.startswith("No camera has had a frame for")]
	assert stopped == ["No camera has had a frame for 2 s: all 3 stopped together, so the trigger has stopped (an "
		"external trigger: is it still running?)."], texts
	assert any(t.startswith("resolved, after ") and t.endswith(": Frames again") for t in texts), texts
	assert not any("no frame for" in t for t in texts), texts # not one per camera


def test_a_disk_that_fills_stops_the_recording_cleanly(tmp_path, service):
	proc = Start(tmp_path, service, rec=60, extra=FILLING_DISK)
	code, out = Finish(proc)
	assert code == 1, out
	texts = Texts(service)
	assert any(t.startswith("The disk with ") and "campy will then stop the recording, cleanly." in t for t in texts), \
		texts
	(stop,) = [t for t in texts if "stopping the recording now" in t] # below 2 GB: when, on a slow computer, varies
	assert re.match(r"Only [01]\.\d\d GB free on the disk with ", stop), stop
	assert stop.endswith("stopping the recording now, cleanly, so that every video and its metadata are whole "
		"(stopAtFreeGB 2).")
	assert re.match(r"Recording \S+ stopped by campy after \d+ s: only [01]\.\d\d GB free on the disk with ", texts[-1])
	assert "2 of 2 cameras did not record to the end" in out
	# Every camera's files, whole: as many frames in the video as metadata.csv and frametimes.npy say
	import imageio_ffmpeg
	for name in ("Camera0", "Camera1"):
		folder = Session(tmp_path) / name
		with open(folder / "metadata.csv") as f:
			meta = dict(csv.reader(f))
		written = int(meta["framesWritten"])
		assert 30 < written < 30 * 10, meta # stopped after seconds, not the minute
		assert np.load(str(folder / "frametimes.npy")).shape == (2, written)
		assert imageio_ffmpeg.count_frames_and_secs(str(folder / "emuclip.mp4"))[0] == written


def test_ffmpeg_killed_and_restarted(tmp_path, service):
	# restartFfmpeg: Camera1's ffmpeg killed mid-recording, and a new one takes over. The camera never stops, the
	# recording ends as planned, and every frame is accounted for: in one of its two files, or lost in the old ffmpeg
	import imageio_ffmpeg
	from campy import segments
	proc = Start(tmp_path, service, rec=8, restartFfmpeg=True)
	ffmpegs = Ffmpegs(proc)
	time.sleep(2.5)
	ffmpegs["Camera1"].kill()
	code, out = Finish(proc)
	assert code == 0, out # nothing failed
	texts = Texts(service)
	(restarted,) = [t for t in texts if t.startswith("Camera1: ffmpeg stopped, and campy started a new one")]
	assert restarted == "Camera1: ffmpeg stopped, and campy started a new one (restartFfmpeg; restart 1 of the " \
		"recording). The camera never stopped: its video goes on in a new file, emuclip.part2.mp4. The frames inside " \
		"the stopped ffmpeg, about a second, are lost; segments.csv in its folder will say which."
	(counted,) = [t for t in texts if t.startswith("Camera1: the stopped ffmpeg's file is counted")]
	lost = int(re.search(r"counted: (\d+) frames? given to it", counted).group(1))
	assert not any("Camera1/ended" in t or "ffmpeg stopped after taking" in t for t in texts), texts
	assert texts[-1] == "Recording {} ended after {}, with ffmpeg restarted:\n- Camera0: 240 frames\n- Camera1: 240 " \
		"frames, in 2 video files: 1 ffmpeg restart, {} frame{} lost".format(Session(tmp_path).name,
		texts[-1].split("ended after ")[1].split(",")[0], lost, "" if lost == 1 else "s"), texts[-1]
	assert service.Texts()[-1].startswith(":large_orange_circle:")

	folder = Session(tmp_path) / "Camera1"
	rows = segments.Read(str(folder))
	assert [r["file"] for r in rows] == ["emuclip.mp4", "emuclip.part2.mp4"]
	assert rows[0]["lost"] == lost, texts
	assert rows[1]["last_frame"] == 240 and rows[1]["lost"] == 0
	assert rows[1]["first_frame"] == rows[0]["last_frame"] + 1
	gone = segments.ParseRanges(rows[0]["lost_frames"])
	with open(folder / "metadata.csv") as f:
		meta = dict(csv.reader(f))
	held = [imageio_ffmpeg.count_frames_and_secs(str(folder / r["file"]))[0] for r in rows]
	assert held == [r["frames"] for r in rows]
	assert meta["framesWritten"] == str(sum(held)) == str(240 - lost) and meta["framesLostInEncoder"] == str(lost)
	frames = np.load(str(folder / "frametimes.npy"))
	assert [int(n) for n in frames[0]] == [n for n in range(1, 241) if n not in set(gone)]
	assert not (Session(tmp_path) / "Camera0" / "segments.csv").exists() # the other camera: as ever
	# session.json and recording.json say it too
	session = json.load(open(Session(tmp_path) / "session.json"))
	(end,) = [c for c in session["end"]["cameras"] if c["name"] == "Camera1"]
	assert end["ffmpegRestarts"] == 1 and end["videoFiles"] == ["emuclip.mp4", "emuclip.part2.mp4"]
	assert end["framesLostInEncoder"] == lost and session["status"] == "complete"
	status = json.load(open(os.path.join(os.environ["CAMPY_STATUS_DIR"], "recording.json")))
	assert [(c["name"], c["ffmpeg_restarts"], c["frames_lost_in_encoder"]) for c in status["cameras"]] == [
		("Camera0", 0, 0), ("Camera1", 1, lost)]
