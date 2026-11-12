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
watch.STALL_START_S = 1.0
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
		alertNoFramesSec=1.5, alertDiskFreeGB=1) # 1 GB: a CI runner can have less than the default's 20 free
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
	# emulated cameras, cameraTrigger "None": free-running
	assert texts[0] == "Recording {} started: 2 cameras (Camera0, Camera1) at 30 fps, for 3 s, free-running (no " \
		"trigger).".format(session) + texts[0].split("(no trigger).")[1]
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


def test_a_slow_network_holds_up_neither_the_trigger_stop_nor_session_json(tmp_path, service):
	# Slack answering each request 2 s late. The end summary was said before the trigger's stop and session.json's
	# end, which then waited up to 15 s for it: now it is said after them
	service.delay = 2.0
	code, out = Finish(Start(tmp_path, service, rec=3))
	assert code == 0, out
	ended = os.path.getmtime(str(Session(tmp_path) / "session.json"))
	(summary,) = [t for t, r in zip(service.times, service.requests) if r[0] == "/api/chat.postMessage" and
		"ended after" in r[2]["text"]]
	assert summary >= ended, (summary, ended)
	assert Texts(service)[-1].startswith("Recording ") # every message still went, the summary too
	assert service.Paths()[-1].endswith("/pause")


BROKEN_WAIT = """
from campy.cameras import unicam
def _broken(self, interrupts):
	import time
	time.sleep(1)
	raise RuntimeError("a made-up error in the camera wait")
unicam.CameraProcesses.Wait = _broken
"""


def test_an_error_in_campy_acquire_is_a_critical_end(tmp_path, service):
	# An error that ended campy-acquire's recording was summed up as an info "ended" (which a warning channel never
	# gets), and recording.json said "ended"
	code, out = Finish(Start(tmp_path, service, rec=3, extra=BROKEN_WAIT))
	assert code != 0 and "a made-up error in the camera wait" in out, out
	end = Texts(service)[-1]
	assert end.startswith("Recording {} ended after ".format(Session(tmp_path).name)), end
	assert "with an error in campy-acquire: a made-up error in the camera wait. Its log says more." in end, end
	assert service.Texts()[-1].startswith(":red_circle:")
	status = json.load(open(os.path.join(os.environ["CAMPY_STATUS_DIR"], "recording.json")))
	assert status["state"] == "failed"


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
	proc = Start(tmp_path, service, rec=16, writeQueueMaxSec=2)
	ffmpegs = Ffmpegs(proc)
	# Stopped once its frames have come for longer than STALL_START_S (1 s here), when its encoder has opened and a
	# stall is warned of. Before, it was stopped 1.5 s after the ffmpegs started, before the trigger's first frame
	# on a busy CI runner now and then: the warning (a quarter of writeQueueMaxSec) came in those first frames' second,
	# so wasn't said, and the stall was first said already critical, with no "worse:" (Linux, Python 3.9)
	video = ffmpegs["Camera0"].cmdline()[-1]
	video = video if os.path.isabs(video) else os.path.join(ffmpegs["Camera0"].cwd(), video)
	deadline = time.monotonic() + 20
	while not (os.path.exists(video) and os.path.getsize(video) > 0) and time.monotonic() < deadline:
		time.sleep(0.05)
	assert os.path.getsize(video) > 0, video
	time.sleep(2.0) # STALL_START_S, the watch's next look (EVERY_S 0.25 s) and a margin
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
	assert re.search(r"- Camera0: 480 frames, \d+ dropped", texts[-1]), texts[-1]
	assert code == 0, out


def test_a_camera_unplugged(tmp_path, service):
	# cameraReconnect False: its recording ends there, as before
	code, out = Finish(Start(tmp_path, service, rec=5, faults="1:lost@40", cameraReconnect="False"))
	assert code == 1, out
	assert "Camera1: the camera is gone (unplugged, or its USB link lost) after frame 40. Its recording has ended " \
		"there; the other cameras go on. It needs a replug, and a new recording." in Texts(service), Texts(service)


def test_a_camera_unplugged_and_never_back(tmp_path, service):
	# cameraReconnect (the default): waited for to the recording's end, and said not to have come back
	code, out = Finish(Start(tmp_path, service, rec=4, faults="1:lost@40"))
	assert code == 1, out
	assert "Camera1: the camera is gone (unplugged, or its USB link lost) after frame 40; the other cameras go on. " \
		"campy waits for it: plug it back in, and it rejoins the recording on the next trigger pulse " \
		"(cameraReconnect)." in Texts(service), Texts(service)
	assert "it was not plugged back in before the recording ended" in out, out


def test_a_camera_unplugged_and_plugged_back_in(tmp_path, service):
	# Camera 1 unplugged at its frame 60 (2 s in at 30 fps), plugged back in 2 s later (its clock from 0, as a FLIR
	# camera's after it powers up). It rejoins the recording on the pulse it comes back on: its frame numbers skip the
	# time it was gone, numbered as the other cameras', its timestamps carried on across the gap, and campy-align
	# puts every frame of it on the same trigger pulse as the other cameras' frame of that number
	from campy import align
	names = ("Camera0", "Camera1", "Camera2")
	code, out = Finish(Start(tmp_path, service, names=names, rec=8, faults="1:lost@60+2", cameraTrigger='"Line0"'))
	assert code == 0, out
	assert re.search(r"Camera1: back, plugged in again after \d+\.\d s: its frames go on from frame \d+ \(\d+ frames "
		r"while it was gone\)\.", out), out
	texts = Texts(service)
	assert any(t.startswith("Camera1: the camera is gone") and "campy waits for it" in t for t in texts), texts
	assert any(t.startswith("resolved, after ") and "Camera1: back, plugged in again: recording again (at frame" in t
		for t in texts), texts
	# The time it was gone (about 60 pulses) is not counted as trigger pulses it missed. A camera can still miss one or
	# a few when a busy runner holds its emulated frames up, before or after the gap: camera 2 did, and camera 1 after
	# it came back, on GitHub's Windows runner, 2026-11-12
	missed = [int(m) for t in texts for line in t.splitlines() if "Camera1" in line
		for m in re.findall(r"no frame for (\d+) trigger pulses?", line)]
	assert all(m < 20 for m in missed), (missed, texts)
	assert "with a camera unplugged and plugged back in" in texts[-1] and "unplugged and back 1 time" in texts[-1], texts
	session = Session(tmp_path)
	frames = {n: np.load(str(session / n / "frametimes.npy")) for n in names}
	hosts = {n: np.load(str(session / n / "hosttimes.npy")) for n in names}
	number, stamp = frames["Camera1"][0], frames["Camera1"][1]
	jumps = np.nonzero(np.diff(number) > 1)[0]
	assert len(jumps) == 1, number
	j = jumps[0]
	before, after = int(number[j]), int(number[j + 1])
	assert before == 60 and 55 <= after - before - 1 <= 80, (before, after) # 2 s at 30 fps, and its reopening
	# Carried on across the gap: the step is the frame numbers' (its own clock started again at 0)
	assert abs((stamp[j + 1] - stamp[j]) * 30 - (after - before)) < 0.01, (stamp[j], stamp[j + 1])
	assert np.all(np.diff(stamp) > 0)
	# Numbered as the other cameras: a frame number's host times within half a period of camera 0's
	common, i0, i1 = np.intersect1d(frames["Camera0"][0], number[j + 1:], return_indices=True)
	assert len(common) > 100
	apart = np.abs(hosts["Camera0"][i0].astype(np.int64) - hosts["Camera1"][j + 1:][i1].astype(np.int64)) / 1e6
	assert np.median(apart) < 1000 / 30 / 2, np.median(apart)
	# All end on the same frame number (the recording's length)
	assert abs(int(number[-1]) - int(frames["Camera0"][0][-1])) <= 1
	meta = dict(row for row in csv.reader(open(session / "Camera1" / "metadata.csv")) if len(row) == 2)
	reconnects = json.loads(meta["cameraReconnects"])
	assert [r[:2] for r in reconnects] == [[before, after]] and int(meta["framesWhileGone"]) == after - before - 1
	assert int(meta["totalFrames"]) == len(number)
	# campy-align: the frame numbers' gap as triggers without a frame, every frame on its pulse as camera 0's
	summary = align.Run(str(session), log=lambda *a: None)
	aligned = {n: np.loadtxt(str(session / n / "aligned.csv"), delimiter=",", skiprows=1) for n in ("Camera0", "Camera1")}
	shift = {n: (a[:, 1] - a[:, 0]).astype(int) for n, a in aligned.items()}
	# Across the gap, campy-align puts camera 1's first frame back as many triggers after its last frame before as
	# their numbers are apart; and camera 1 on camera 0's trigger for each number, when both cameras' timestamps came
	# clean. (Emulated cameras time-stamp a frame as it is read, so on a busy runner one held up lands a trigger off,
	# and campy-align's view of that camera wanders: camera 0's did by up to 2 triggers on GitHub's Windows runner,
	# 2026-11-12. That camera 1 is on camera 0's pulses is checked by host time above, and with real cameras, whose
	# timestamps are their own, in docs/alerts.md.)
	a1 = aligned["Camera1"]
	k = int(np.nonzero(a1[:, 0] == before)[0][0])
	assert int(a1[k + 1, 0]) == after and int(a1[k + 1, 1] - a1[k, 1]) == after - before, a1[k:k + 2]
	if len(set(shift["Camera0"])) == 1 and len(set(shift["Camera1"])) == 1:
		assert set(shift["Camera0"]) == set(shift["Camera1"]), {n: sorted(set(v)) for n, v in shift.items()}
	assert summary["cameras"]["Camera1"]["triggers_without_frame"] >= after - before - 1, summary["cameras"]["Camera1"]


def test_the_trigger_pauses(tmp_path, service):
	# Every camera stopping at once. These emulated cameras free-run (cameraTrigger "None"), so no trigger is blamed
	# (tests/test_watch.py has the external trigger's and the Arduino's words for it)
	code, out = Finish(Start(tmp_path, service, names=("Camera0", "Camera1", "Camera2"), rec=8,
		faults="*:stall@40+3.5"))
	assert code == 0, out
	texts = Texts(service)
	stopped = [t for t in texts if t.startswith("No camera has had a frame for")]
	assert stopped == ["No camera has had a frame for 2 s: all 3 stopped together. They free-run, with no trigger: "
		"is the computer, or their USB, stuck?"], texts
	assert any(t.startswith("resolved, after ") and t.endswith(": Frames again") for t in texts), texts
	assert not any("no frame for" in t for t in texts), texts # not one per camera


def test_a_short_trigger_pause_is_said(tmp_path, service):
	# Shorter than alertNoFramesSec: no "stopped", but the pause, from the cameras' own timestamps. At 10 fps, so that
	# a frame a busy test machine grabs late is no pulse missed.
	code, out = Finish(Start(tmp_path, service, names=("Camera0", "Camera1", "Camera2"), rec=6, frameRate=10,
		cameraTrigger='"Line0"', faults="*:stall@20+0.8"))
	assert code == 0, out
	texts = Texts(service)
	(pause,) = [t for t in texts if t.startswith("The trigger paused: ")]
	assert re.match(r"The trigger paused: all 3 cameras got no frame for the same [78] pulses \(0\.[78]0 s\), just "
		r"before frame 21, about \d+ s in\.", pause), pause
	assert not any(t.startswith("No camera has had a frame") for t in texts), texts
	assert texts[-1].split("\n")[0].endswith(", with the trigger paused:"), texts[-1]
	assert "- The trigger paused 1 time: " in texts[-1], texts[-1]


def test_a_planned_pause_is_said_once_not_per_camera(tmp_path, service):
	# A trigger that runs the cameras in bouts (alertTriggerPauseSec): the gap every camera shares is the watch's
	# one "paused as planned" line, not each camera's "no frame for" and "frames again", and the frame rate the
	# console says leaves the pause out
	code, out = Finish(Start(tmp_path, service, names=("Camera0", "Camera1", "Camera2"), rec=8, frameRate=10,
		cameraTrigger='"Line0"', alertTriggerPauseSec=3, alertNoFramesSec=60, chunkLengthInSec=7,
		faults="*:stall@20+5"))
	assert code == 0, out
	assert re.search(r"Trigger: The trigger paused as planned: all 3 cameras got no frame for the same \d+ pulses", out), out
	assert "no frame for" not in out.replace("got no frame for the same", ""), out
	assert "frames again" not in out, out
	for name in ("Camera0", "Camera1", "Camera2"):
		assert re.search(r"{} collected 70 frames at (9\.\d|10\.\d) fps for 7 sec of frames \(1[12] sec in all, with 1 "
			r"trigger pause of [45] sec\)\.".format(name), out), out
		assert re.search(r"{} saved 80 frames at 10 fps over 8 s of frames, with 1 trigger pause of [45] s in all\."
			.format(name), out), out


def test_one_camera_stopping_is_said_with_planned_pauses(tmp_path, service):
	# The others still get frames: that camera's fault, said as before
	code, out = Finish(Start(tmp_path, service, names=("Camera0", "Camera1", "Camera2"), rec=8, frameRate=10,
		cameraTrigger='"Line0"', alertTriggerPauseSec=3, alertNoFramesSec=60, faults="1:stall@20+5"))
	assert code == 0, out
	assert re.search(r"Camera1: no frame for 3 s, since frame 20\.", out), out
	assert re.search(r"Camera1: frames again, after [45]\.\d s without \(frame 21\)\.", out), out
	# The others never went quiet (a busy runner can still make one miss a pulse at its start: a different alert)
	assert not re.search(r"Camera[02]: no frame for [\d.]+ s,", out), out


def test_a_disk_that_fills_stops_the_recording_cleanly(tmp_path, service):
	proc = Start(tmp_path, service, rec=60, extra=FILLING_DISK, alertDiskFreeGB=20)
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
