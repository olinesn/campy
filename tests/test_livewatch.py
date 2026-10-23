"""
campy-watch (campy/livewatch.py): campy-live-view's checks (livehealth),
sent once they last. A camera a frame behind, for 10 s: critical; lost or
late frames for 30 s: a warning; tracking with too few animals for most of
3 minutes; campy-live stopping while the cameras record. And end to end:
emulated cameras with the live feed, one of them falling half a second
behind, and campy-watch's Slack message about it.
"""

import json
import os
import re
import subprocess
import sys
import types

import pytest

from campy import livewatch, notify

from test_emu import write_clip
from test_notify import Service
from test_watch import Capture, Clock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def Cameras(*problems):
	# A livehealth.Rig whose Status says these problems: [(camera, tag, sentence)]
	names = sorted({p[0] for p in problems} | {"Camera0"})
	cams = [types.SimpleNamespace(name=n, problems=[(t, s) for c, t, s in problems if c == n]) for n in names]
	return types.SimpleNamespace(Status=lambda: types.SimpleNamespace(cameras=cams))


def Make():
	clock = Clock()
	channel = Capture()
	n = notify.Notifier("Test rig", channels=[channel], clock=clock, background=False, say=lambda t: None)
	said = []
	w = livewatch.LiveWatch(n, clock=clock, say=said.append)
	return w, clock, channel, said


def Texts(channel):
	return [t.split("*Test rig*: ", 1)[1] for t in channel.texts]


def Run(w, clock, seconds):
	end = clock.now + seconds
	while clock.now < end:
		clock.now += 1
		w.Check()


def test_kinds():
	assert [livewatch.Kind(t) for t in ("NO FRAMES", "24.8 FPS", "1 FRAME BEHIND", "2 FRAMES AHEAD", "LOST 3",
		"GAP 2", "LATE 7")] == ["noframes", "slow", "offset", "offset", "lost", "gaps", "late"]


def test_a_camera_a_frame_behind(monkeypatch):
	w, clock, channel, said = Make()
	behind = "Camera1: 1 frame behind the other cameras (+33 ms for the same frame number): campy-live's sets mix " \
		"frames of different pulses. Stop the recording and start it again."
	w.rig = Cameras(("Camera1", "1 FRAME BEHIND", behind))
	Run(w, clock, livewatch.OFFSET_HOLD_S - 1)
	assert channel.texts == [] # not yet: a moment's offset is no alert
	Run(w, clock, 2)
	assert Texts(channel) == [behind] and channel.texts[0].startswith(":red_circle:")
	w.rig = Cameras()
	Run(w, clock, 1)
	assert Texts(channel)[-1].endswith(": Camera1: in step with the other cameras again")


def test_lost_frames_and_no_frames(monkeypatch):
	w, clock, channel, said = Make()
	w.rig = Cameras(("Camera2", "LOST 4", "Camera2: the camera lost 4 frames in the last 10 s (its timestamps "
		"skipped them)"), ("Camera0", "NO FRAMES", "Camera0: no frame for 3.0 s"))
	Run(w, clock, livewatch.COUNTS_HOLD_S + 1)
	assert Texts(channel) == ["Camera2: the camera lost 4 frames in the last 10 s (its timestamps skipped them)"]
	assert channel.texts[0].startswith(":large_orange_circle:") # NO FRAMES: campy-acquire's watch says it


def test_a_problem_that_does_not_last(monkeypatch):
	w, clock, channel, said = Make()
	late = ("Camera1", "LATE 2", "Camera1: 2 frames here a frame late or more in the last 10 s")
	for _ in range(5):
		w.rig = Cameras(late)
		Run(w, clock, 10)
		w.rig = Cameras()
		Run(w, clock, 2)
	assert channel.texts == []


def test_tracking_collapse(monkeypatch):
	w, clock, channel, said = Make()
	bad = ["Good 3D for 3 of 5 animals: #1 not found in 3D, #4 error 41 px"]
	state = {"alerts": []}
	w.tracking = types.SimpleNamespace(Alerts=lambda: state["alerts"])
	Run(w, clock, 60)
	state["alerts"] = bad
	Run(w, clock, livewatch.COLLAPSE_S * 0.7)
	assert channel.texts == [] # 70 % of 3 minutes, not yet of the whole 3 minutes
	Run(w, clock, livewatch.COLLAPSE_S * 0.3)
	assert Texts(channel) == ["Tracking: good 3D for too few animals 70% of the last 3 min. Now: Good 3D for 3 of 5 "
		"animals: #1 not found in 3D, #4 error 41 px"]
	state["alerts"] = []
	Run(w, clock, livewatch.COLLAPSE_S * 0.7)
	assert Texts(channel)[-1].startswith("resolved, after ") and "good 3D for every animal again" in Texts(channel)[-1]


def test_campy_live_stops(monkeypatch):
	w, clock, channel, said = Make()
	frame = [0]
	for _ in range(40):
		clock.now += 1
		frame[0] += 30
		w.Frames(frame[0], clock.now)
		w.Rows(frame[0] if clock.now < 5010 else 300, clock.now) # campy-live's rows stop at 10 s
		w.Check()
	assert Texts(channel) == ["campy-live has made no result for 30 s, while the cameras record: it has stopped or "
		"hangs"]
	assert channel.texts[0].startswith(":red_circle:")
	clock.now += 1
	w.Rows(400, clock.now)
	w.Check()
	assert Texts(channel)[-1].endswith(": campy-live: results again")


def test_campy_live_ends_when_the_recording_ends(monkeypatch):
	w, clock, channel, said = Make()
	w.Frames(100, clock.now)
	w.Rows(100, clock.now)
	w.resultsEnded = True # both stop together: no alert
	Run(w, clock, livewatch.STALL_S)
	assert channel.texts == []


# ---------------------------------------------------------------------- end to end

QUICK = """
from campy import livewatch
livewatch.OFFSET_HOLD_S = 1.0
"""


@pytest.fixture
def service():
	s = Service()
	yield s
	s.server.shutdown()


def test_campy_watch_says_a_camera_fell_behind(tmp_path, service):
	names = ["Camera0", "Camera1", "Camera2"]
	for name in names:
		(tmp_path / name).mkdir()
		write_clip(str(tmp_path / name / "clip.mp4"), frames=10)
	feed = "campy-test-watch-{}".format(os.getpid())
	(tmp_path / "config.yaml").write_text("\n".join([
		"numCams: 3", 'cameraMake: "emu"', 'videoFolder: "{}"'.format(tmp_path.as_posix()),
		'videoFilename: "emuclip.mp4"', "frameRate: 30", "recTimeInSec: 10", "chunkLengthInSec: 100",
		"displayFrameRate: 0", 'pixelFormatInput: "gray"', 'pixelFormatOutput: "yuv420p"', "gpuID: -1",
		'codec: "h264"', 'preflight: "off"', "liveFeed: True", 'liveFeedName: "{}"'.format(feed), "alerts: False", ""]))
	alerts = tmp_path / "alerts.json"
	alerts.write_text(json.dumps({"rig": "Test rig", "slack": {"token": "xoxb-test", "user": "U1",
		"api": service.url + "/api/"}}))
	env = dict(os.environ, PYTHONPATH=REPO, PYTHONUNBUFFERED="1", CAMPY_ALERTS_FILE=str(alerts),
		CAMPY_EMU_FAULTS="1:stall@60+0.5") # Camera1 half a second late from frame 60 on: 15 frames behind
	env.pop("IMAGEIO_FFMPEG_EXE", None)
	acquire = "import sys; sys.argv = ['campy-acquire', sys.argv[1]]; from campy.campy import Main; Main()"
	watcher = subprocess.Popen([sys.executable, "-c", QUICK + "import sys\nsys.exit(livewatch.Main(sys.argv[1:]))",
		"--name", feed, "--cameras", "3"], cwd=str(tmp_path), env=env, stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT, text=True)
	try:
		recording = subprocess.run([sys.executable, "-c", acquire, str(tmp_path / "config.yaml")], cwd=str(tmp_path),
			env=env, capture_output=True, text=True, timeout=120)
		assert recording.returncode == 0, recording.stdout + recording.stderr
		out, _ = watcher.communicate(timeout=30)
	finally:
		if watcher.poll() is None:
			watcher.kill()
	assert "campy-watch: watching Camera0, Camera1, Camera2." in out and "campy-watch: the recording ended." in out, out
	texts = [t.split("*Test rig*: ", 1)[1] for t in service.Texts()]
	behind = [t for t in texts if t.startswith("Camera1: ") and " behind the other cameras" in t]
	assert behind, (texts, out)
	# About 15 frames (0.5 s), or a few less: the offset is a mean over the last 2 s, and the alert can go out
	# before all of them are from after the pause
	m = re.match(r"Camera1: (\d+) frames behind the other cameras \(\+(\d+) ms for the same frame number\)", behind[0])
	assert m and 8 <= int(m.group(1)) <= 17 and 250 <= int(m.group(2)) <= 600, behind[0]
	assert service.Texts()[0].startswith(":red_circle:")
	assert watcher.returncode == 0
	# Its own log, beside campy-acquire's alerts.log
	(session,) = [d for d in tmp_path.iterdir() if re.fullmatch(r"\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d", d.name)]
	lines = [json.loads(line) for line in (session / "alerts-live.log").read_text().splitlines()]
	assert any(l["key"] == "live/Camera1/offset" and l["state"] == "open" for l in lines), lines
	assert (session / "alerts.log").exists()
