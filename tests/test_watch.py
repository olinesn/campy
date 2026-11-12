"""
campy-acquire's recording watch (campy/watch.py), check by check, on made-up
counters and a made-up clock: a camera with no frames, all of them at once
(the trigger), the trigger pausing, a camera missing trigger pulses, the first
frame late, dropped frames, a stalled writer, a
camera that ends, the disk filling (and the clean stop before it is full),
the trigger's port gone, a camera on USB 2, the GPU's kernel log (firmware
stalls, hung jobs, resets); and the messages as the
recording starts and ends. tests/test_watch_acquire.py runs it in
campy-acquire, with emulated cameras and injected faults.
"""

import ctypes
import json
import os
import sys
import threading
import time
import types

import pytest


from campy import configurator, gpulog, notify, status, watch

from test_preflight import make_sysfs


class Clock:
	def __init__(self):
		self.now = 5000.0

	def __call__(self):
		return self.now


class Capture:
	# A channel that keeps what it is sent
	name = "capture"
	min = notify.INFO

	def __init__(self):
		self.texts, self.edits = [], []

	def Send(self, text):
		self.texts.append(text)
		return ["D1", str(len(self.texts))]

	def Update(self, handle, text):
		self.edits.append(text)


class Disk:
	def __init__(self, free):
		self.free = free

	def __call__(self, path):
		return types.SimpleNamespace(free=self.free)


class Rig:
	"""A watch over made-up cameras: rig.Frame(c) gives camera c a frame now, rig.Tick(s) moves the clock."""

	def __init__(self, monkeypatch, tmp_path, names=("Camera0", "Camera1", "Camera2"), systems=None, free=500e9,
			cams=None, sysfs_root=None, gpu_log=None, **values):
		self.clock = Clock()
		monkeypatch.setattr(status, "Now", lambda: int(self.clock.now * 1e9))
		self.names = list(names)
		self.counters = (ctypes.c_longlong * (len(names) * status.WIDTH))()
		self.rows = [status.Camera(self.counters, c) for c in range(len(names))]
		self.cameras = types.SimpleNamespace(names=self.names, failures={})
		self.channel = Capture()
		self.notifier = notify.Notifier("Test rig", channels=[self.channel], clock=self.clock, background=False,
			say=lambda t: None)
		self.params = configurator.DefaultParams()
		self.params.update(frameRate=30, recTimeInSec=3600, writeQueueMaxSec=10, serialPort="COM3")
		self.params.update(values)
		self.disk = Disk(free)
		(tmp_path / "2026-10-23_01-00-00").mkdir(exist_ok=True)
		self.said = []
		self.stopping = False
		self.stops = [] # what the watch stopped the recording for
		self.watch = watch.Watch(self.params, systems if systems is not None else {"serial": Serial()}, self.cameras,
			self.counters, folder=str(tmp_path / "2026-10-23_01-00-00"), notifier=self.notifier, cams=cams,
			clock=self.clock, disk_usage=self.disk, sysfs_root=sysfs_root or str(tmp_path / "nosys"),
			say=self.said.append, stopping=lambda: self.stopping, request_stop=self.stops.append,
			port_gone=lambda port: None, gpu_log=gpu_log)
		self.grabbed = [0] * len(names)
		self.dropped = [0] * len(names)
		self.missed = [0] * len(names)
		for row in self.rows:
			row.State(status.GRABBING)

	def Start(self):
		self.watch.started = self.clock.now
		self.watch.startedAt = watch.Wall()
		self.watch.Begun()
		self.watch.WriteStatus("recording")
		return self

	def Frame(self, c, n=1, queued=0, sent=None):
		self.grabbed[c] += n
		self.rows[c].Frame(self.grabbed[c], queued)
		self.rows[c].Sent(self.grabbed[c] - queued if sent is None else sent)

	def Pause(self, c, pulses):
		# Camera c's next frame comes after pulses trigger pulses without a frame (frametimes.TriggerGaps)
		self.missed[c] += pulses
		self.Frame(c)
		self.rows[c].Missed(self.missed[c], self.grabbed[c])

	def Run(self, seconds, cameras=None, every=1.0 / 30, queued=0):
		# seconds of frames from these cameras (default: all), the watch checking every EVERY_S
		t = 0.0
		cameras = range(len(self.names)) if cameras is None else cameras
		next_check = self.clock.now + watch.EVERY_S
		while t < seconds - 1e-9:
			step = min(1.0, seconds - t)
			self.clock.now += step
			t += step
			for c in cameras:
				self.Frame(c, int(round(step / every)), queued=queued)
			if self.clock.now >= next_check:
				self.watch.Check()
				next_check = self.clock.now + watch.EVERY_S

	def Sent(self):
		return self.channel.texts


class Serial:
	def __init__(self):
		self.gone = None
		self.closed = False

	def close(self):
		self.closed = True

	@property
	def in_waiting(self):
		if self.gone:
			raise OSError(self.gone)
		return 0


def test_start_message(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path, free=812.4e9).Start()
	assert rig.Sent() == [":information_source: *Test rig*: Recording 2026-10-23_01-00-00 started: 3 cameras "
		"(Camera0, Camera1, Camera2) at 30 fps, for 1 h 0 min. 812.4 GB free."]


def test_a_camera_with_no_frames(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path).Start()
	rig.Run(20)
	rig.Run(watch.EVERY_S * 6, cameras=[0, 2]) # Camera1: none for 12 s
	assert rig.Sent()[-1] == ":red_circle: *Test rig*: Camera1: no frame for 12 s, since frame 600"
	assert "Alert: Camera1: no frame for 12 s, since frame 600" in rig.said
	rig.Run(watch.EVERY_S * 10, cameras=[0, 2]) # still: nothing more
	assert len(rig.Sent()) == 2
	rig.Run(watch.EVERY_S)
	assert rig.Sent()[-1] == ":white_check_mark: *Test rig*: resolved, after 22 s: Camera1: frames again (frame 660)"
	assert rig.channel.edits # the first message, struck through


def test_every_camera_at_once_is_the_trigger(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path).Start()
	rig.Run(20)
	rig.Run(watch.EVERY_S * 8, cameras=[])
	# One alert, not one per camera
	assert rig.Sent()[1:] == [":red_circle: *Test rig*: No camera has had a frame for 12 s: all 3 stopped together, "
		"so the trigger has stopped (the trigger Arduino on COM3: unplugged, or it reset?)."]
	rig.Run(watch.EVERY_S)
	assert rig.Sent()[-1].startswith(":white_check_mark: *Test rig*: resolved, after ")
	assert rig.Sent()[-1].endswith(": Frames again")


def test_an_external_trigger(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path, systems={}, cameraTrigger="Line3").Start()
	assert rig.Sent()[0].endswith("for 1 h 0 min, on an external trigger. 500.0 GB free.")
	rig.Run(watch.EXTERNAL_WAIT_S - 10, cameras=[]) # waiting for it: nothing yet
	assert len(rig.Sent()) == 1
	rig.Run(20, cameras=[])
	assert rig.Sent()[-1] == (":large_orange_circle: *Test rig*: No frame yet, 10 min after campy started waiting "
		"for the external trigger: is it running?")
	rig.Run(20)
	assert rig.Sent()[-1].endswith(": The first frames came")
	rig.Run(watch.EVERY_S * 7, cameras=[])
	assert "so the trigger has stopped (an external trigger: is it still running?)" in rig.Sent()[-1]


def test_an_external_trigger_started_later_on_purpose(monkeypatch, tmp_path):
	# alertExternalWaitSec: a session that starts campy well before the device that makes the pulses
	rig = Rig(monkeypatch, tmp_path, systems={}, cameraTrigger="Line3", alertExternalWaitSec=1800).Start()
	rig.Run(watch.EXTERNAL_WAIT_S + 60, cameras=[]) # past the default 10 min: nothing
	assert len(rig.Sent()) == 1
	rig.Run(1800 - watch.EXTERNAL_WAIT_S - 60 + 10, cameras=[])
	assert rig.Sent()[-1] == (":large_orange_circle: *Test rig*: No frame yet, 30 min after campy started waiting "
		"for the external trigger: is it running?")
	rig.Run(20)
	assert rig.Sent()[-1].endswith(": The first frames came")


def test_an_external_trigger_never_waited_for(monkeypatch, tmp_path):
	# alertExternalWaitSec None: no "No frame yet" however long campy waits; the rest as before
	rig = Rig(monkeypatch, tmp_path, systems={}, cameraTrigger="Line3", alertExternalWaitSec="None").Start()
	rig.Run(3 * 3600, cameras=[])
	rig.Run(20)
	assert len(rig.Sent()) == 1 # the start only: nothing said as the frames came either
	assert not any("No frame yet" in line or "first frames" in line for line in rig.said)
	rig.Run(watch.EVERY_S * 7, cameras=[])
	assert "so the trigger has stopped (an external trigger: is it still running?)" in rig.Sent()[-1]


def test_free_running_cameras_are_not_on_an_external_trigger(monkeypatch, tmp_path):
	# No trigger Arduino and cameraTrigger "None": the cameras time their own frames. The Jetson AGX Orin's first
	# recording (one Blackfly S, no trigger board) was said to be "on an external trigger"
	rig = Rig(monkeypatch, tmp_path, names=["Camera0"], systems={}, recTimeInSec=120).Start()
	assert rig.Sent()[0].endswith("*Test rig*: Recording 2026-10-23_01-00-00 started: 1 camera (Camera0) at 30 fps, "
		"for 2 min, free-running (no trigger). 500.0 GB free."), rig.Sent()[0]
	assert not rig.watch.external and rig.watch.freeRunning


def test_free_running_cameras_with_no_first_frame(monkeypatch, tmp_path):
	# Their frames come at once, so none 10 s in is critical, as with the Arduino: not a 10-min wait for a trigger
	rig = Rig(monkeypatch, tmp_path, systems={}).Start()
	rig.Run(8, cameras=[])
	assert len(rig.Sent()) == 1
	rig.Run(4, cameras=[])
	assert rig.Sent()[-1] == ":red_circle: *Test rig*: No camera has had a frame 10 s after the recording started. " \
		"They free-run, with no trigger, so their frames should have come at once."
	rig.Run(20)
	assert rig.Sent()[-1].endswith(": The first frames came")


def test_free_running_cameras_stopping_together_blame_no_trigger(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path, systems={}).Start()
	rig.Run(20)
	rig.Run(watch.EVERY_S * 8, cameras=[])
	assert rig.Sent()[1:] == [":red_circle: *Test rig*: No camera has had a frame for 12 s: all 3 stopped together. "
		"They free-run, with no trigger: is the computer, or their USB, stuck?"]


def test_which_trigger_from_each_camera(monkeypatch, tmp_path):
	# cameraTrigger is each camera's: one triggered camera is enough for an external trigger; a RatCam free-runs
	# (its SYNC input only marks the pulses); a list in the config gives each camera its own
	def kind(systems=None, cams=None, **values):
		w = Rig(monkeypatch, tmp_path, systems=systems if systems is not None else {}, cams=cams, **values).watch
		return "arduino" if w.board else "external" if w.external else "free" if w.freeRunning else "?"
	free, line = {"cameraTrigger": "None"}, {"cameraTrigger": "Line3"}
	assert kind(cams=[free, free, free]) == "free"
	assert kind(cams=[free, line, free]) == "external"
	assert kind(cams=[dict(line, cameraMake="ratcam")] * 3) == "free"
	assert kind(cameraTrigger=["None", "None", "Line3"]) == "external"
	assert kind(cameraTrigger=["None", "None", "None"]) == "free"
	# The trigger Arduino is campy's, whatever the cameras say: as before
	assert kind(systems={"serial": object()}, cams=[free, free, free]) == "arduino"
	assert kind(systems={"serial": object()}, cams=[line, line, line]) == "arduino"


def test_the_arduino_note_is_unchanged(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path).Start() # cameraTrigger "None", with the trigger Arduino
	assert rig.Sent()[0].endswith("for 1 h 0 min. 500.0 GB free.")


def test_external_wait_leaves_the_arduino_alone(monkeypatch, tmp_path):
	# With the trigger Arduino, no frame 10 s after it started is critical, whatever alertExternalWaitSec says
	rig = Rig(monkeypatch, tmp_path, alertExternalWaitSec="None").Start()
	rig.Run(12, cameras=[])
	assert rig.Sent()[-1] == ":red_circle: *Test rig*: No camera has had a frame 10 s after the trigger Arduino " \
		"on COM3 started."


def test_the_arduino_started_and_no_frame_came(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path).Start()
	rig.Run(12, cameras=[])
	assert rig.Sent()[-1] == ":red_circle: *Test rig*: No camera has had a frame 10 s after the trigger Arduino " \
		"on COM3 started."


def test_one_camera_never_starts(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path).Start()
	rig.Run(12, cameras=[0, 1])
	assert rig.Sent()[-1] == ":red_circle: *Test rig*: Camera2: no frame yet, 10 s after the others' first"


def test_dropped_frames(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path).Start()
	rig.Run(30)
	rig.dropped[1] = 5
	rig.rows[1].Dropped(5)
	rig.Run(watch.EVERY_S)
	assert rig.Sent()[-1] == (":large_orange_circle: *Test rig*: Camera1: dropped 5 frames in the last 30 s (5 "
		"since the start): more than writeQueueMaxSec (10 s) of frames were waiting for ffmpeg.")
	rig.rows[1].Dropped(100) # 95 more in a minute of 1800: above 1 %
	rig.Run(watch.EVERY_S)
	assert rig.Sent()[-1].startswith(":red_circle: *Test rig*: worse: Camera1: dropped 100 frames")
	rig.Run(watch.DROPS_CLEAR_S - 10) # resolved once none was dropped for DROPS_CLEAR_S
	assert "resolved" not in rig.Sent()[-1]
	rig.Run(20)
	assert rig.Sent()[-1].endswith("Camera1: no frame dropped for 2 min (100 in all)")


def test_a_stalled_writer(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path).Start()
	rig.Run(30)
	# Camera0's ffmpeg takes nothing: its frames pile up, 30 a second
	queued = 0
	for _ in range(5):
		rig.clock.now += 1
		queued += 30
		rig.grabbed[0] += 30
		rig.rows[0].Frame(rig.grabbed[0], queued)
		for c in (1, 2):
			rig.Frame(c, 30)
		rig.watch.Check()
	assert rig.Sent()[-1].startswith(":large_orange_circle: *Test rig*: Camera0: 3.0 s of frames waiting for ffmpeg "
		"(30% of writeQueueMaxSec 10 s); it took ")
	assert rig.Sent()[-1].endswith(" a second, of 30). The disk or the encoder has stalled: frames are dropped once "
		"it reaches 100 %.")
	for _ in range(3):
		rig.clock.now += 1
		queued += 30 * 2
		rig.grabbed[0] += 30
		rig.rows[0].Frame(rig.grabbed[0], queued)
		rig.watch.Check()
	assert rig.Sent()[-1].startswith(":red_circle: *Test rig*: worse: Camera0: 9.0 s of frames waiting for ffmpeg (90%")
	# It writes again, and catches up
	rig.Run(watch.STALL_CLEAR_S + watch.EVERY_S * 2, queued=10)
	assert rig.Sent()[-1].endswith("Camera0: caught up (0.3 s of frames waiting for ffmpeg)")


def Pile(rig, c, seconds, queued, per_second=30):
	# Camera c's frames for seconds, each second per_second more waiting for ffmpeg; the others' are taken
	for _ in range(seconds):
		rig.clock.now += 1
		queued += per_second
		rig.grabbed[c] += 30
		rig.rows[c].Frame(rig.grabbed[c], queued)
		for other in range(len(rig.names)):
			if other != c:
				rig.Frame(other, 30)
		rig.watch.Check()
	return queued


def test_an_encoder_opening_at_the_first_frames_is_no_stall(monkeypatch, tmp_path):
	# ffmpeg opens its encoder at its first frame: on the ChronicRecord PC, 4 Quick Sync encoders opening at once
	# after an external trigger's wait let 1.8 s of frames pile up, and then caught up (none dropped)
	rig = Rig(monkeypatch, tmp_path, systems={}, cameraTrigger="Line3").Start()
	rig.Run(30, cameras=[]) # waiting for the trigger
	Pile(rig, 0, 4, 0, per_second=20) # 2.7 s waiting (27 % of writeQueueMaxSec 10 s) after 4 s
	assert waiting(rig, 0) >= watch.STALL_WARNING * 10
	rig.Run(watch.STALL_START_S, queued=3) # caught up
	assert not [t for t in rig.Sent() if "waiting for ffmpeg" in t]


def waiting(rig, c):
	return rig.counters[c * status.WIDTH + status.QUEUED] / 30.0


def test_a_stall_that_goes_on_past_the_start_is_said(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path, systems={}, cameraTrigger="Line3").Start()
	rig.Run(30, cameras=[])
	Pile(rig, 0, int(watch.STALL_START_S) + 2, 0, per_second=8) # 3.2 s waiting at 12 s, and growing
	said = [t for t in rig.Sent() if "waiting for ffmpeg" in t]
	assert len(said) == 1 and said[0].startswith(":large_orange_circle: *Test rig*: Camera0: ")


def test_a_stall_near_dropping_frames_at_the_start_is_still_said(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path, systems={}, cameraTrigger="Line3").Start()
	rig.Run(30, cameras=[])
	Pile(rig, 0, 4, 0, per_second=30 * 2) # 8 s waiting of 10 within 4 s: critical
	assert rig.Sent()[-1].startswith(":red_circle: *Test rig*: Camera0: 8.0 s of frames waiting for ffmpeg (80%")


def test_a_restarted_ffmpeg_opening_its_encoder_is_no_stall(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path).Start()
	rig.Run(60)
	rig.rows[0].Restarted(1) # restartFfmpeg: a new ffmpeg, whose encoder opens at its first frame
	Pile(rig, 0, 4, 0, per_second=20)
	assert waiting(rig, 0) >= watch.STALL_WARNING * 10
	rig.Run(watch.STALL_START_S, queued=3)
	assert not [t for t in rig.Sent() if "waiting for ffmpeg" in t]


def Stall(rig, seconds, stalled=None):
	# seconds of frames, a check each second. stalled: {camera: from which second its ffmpeg takes no frame}: its
	# frames pile up, and past writeQueueMaxSec (10 s, 300 frames) they are dropped. The others' are taken at once
	stalled = stalled or {}
	queued = getattr(rig, "queued", [0] * len(rig.names))
	for second in range(seconds):
		rig.clock.now += 1
		for c in range(len(rig.names)):
			rig.grabbed[c] += 30
			if c in stalled and second >= stalled[c]:
				queued[c] += 30
				if queued[c] > 300:
					rig.dropped[c] += queued[c] - 300
					queued[c] = 300
			else:
				queued[c] = 0
			rig.rows[c].Frame(rig.grabbed[c], queued[c])
			rig.rows[c].Sent(rig.grabbed[c] - queued[c] - rig.dropped[c])
			rig.rows[c].Dropped(rig.dropped[c])
		rig.watch.Check()
	rig.queued = queued


def test_a_disk_stall_in_every_camera_is_one_message(monkeypatch, tmp_path):
	# The EVO-X2's SSD stopped taking writes for 10-16 s: every camera's queue fills, then frames are dropped. Each
	# camera's warning, worse, dropped frames and both resolutions were 15 messages for 3 cameras (40 for 8): now 5,
	# each saying every camera. Camera2's stall shows a second after the others': within GROUP_S, it joins them
	rig = Rig(monkeypatch, tmp_path).Start()
	rig.Run(30)
	Stall(rig, 13, {0: 0, 1: 0, 2: 1})
	sent = rig.Sent()[1:]
	assert len(sent) == 3, sent
	assert sent[0].startswith(":large_orange_circle: *Test rig*: 2 cameras at once: frames waiting for ffmpeg. The "
		"disk or the encoder has stalled: frames are dropped once it reaches 100 %.\n- Camera0: 3.0 s of frames "
		"waiting for ffmpeg (30% of writeQueueMaxSec 10 s); it took ")
	assert "\n- Camera1: 3.0 s of frames waiting" in sent[0] and "Camera2" not in sent[0]
	assert sent[1].startswith(":red_circle: *Test rig*: worse: 3 cameras at once: frames waiting for ffmpeg.")
	assert "\n- Camera2: 7.0 s of frames waiting" in sent[1]
	assert sent[2].startswith(":red_circle: *Test rig*: 2 cameras at once: frames dropped: more than "
		"writeQueueMaxSec (10 s) of frames were waiting for ffmpeg.\n- Camera0: dropped 30 frames in the last ")
	# Each camera's own problems: on the console, and in recording.json
	assert len([t for t in rig.said if t.startswith("Alert: Camera") and "of frames waiting for ffmpeg (" in t]) == 3
	assert sorted(p["key"] for p in rig.watch.Status("recording")["open"]) == ["Camera0/drops", "Camera0/queue",
		"Camera1/drops", "Camera1/queue", "Camera2/drops", "Camera2/queue"]
	Stall(rig, int(watch.DROPS_CLEAR_S) + 10) # the disk takes writes again: caught up
	resolved = rig.Sent()[4:]
	assert len(resolved) == 2, resolved
	assert resolved[0].startswith(":white_check_mark: *Test rig*: resolved, after ") and resolved[0].endswith(
		": 3 cameras:\n- Camera0: caught up (0.0 s of frames waiting for ffmpeg)\n- Camera1: caught up (0.0 s of "
		"frames waiting for ffmpeg)\n- Camera2: caught up (0.0 s of frames waiting for ffmpeg)")
	assert resolved[1].endswith(": 3 cameras:\n- Camera0: no frame dropped for 2 min (90 in all)\n- Camera1: no "
		"frame dropped for 2 min (90 in all)\n- Camera2: no frame dropped for 2 min (60 in all)")
	assert rig.watch.groups == [] and rig.notifier.Open() == []
	# The same stall again, 20 min on: 3 messages again, not "coming and going"
	rig.Run(1200)
	Stall(rig, 13, {0: 0, 1: 0, 2: 0})
	assert len(rig.Sent()) == 6 + 3 and rig.Sent()[6].startswith(":large_orange_circle: *Test rig*: 3 cameras at once")


def test_one_cameras_stall_while_the_others_record_is_its_own(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path).Start()
	rig.Run(30)
	Stall(rig, 3, {0: 0})
	Stall(rig, 4, {0: 0, 1: 0}) # Camera1's starts later: a message of its own
	sent = rig.Sent()[1:]
	assert [t.split("*Test rig*: ", 1)[1][:30] for t in sent] == ["Camera0: 3.0 s of frames waiti",
		"Camera1: 3.0 s of frames waiti"]


def test_cameras_that_end(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path, names=["Camera0", "Camera1", "Camera2", "Camera3"]).Start()
	rig.Run(10)
	rig.rows[0].State(status.LOST)
	rig.rows[1].State(status.VIDEO_FAILED)
	rig.rows[2].State(status.ENDED) # its process then died without saying why
	rig.cameras.failures["Camera2"] = ["its camera process stopped, with exit code -9 (SIGKILL): killed, or crashed"]
	rig.Run(watch.EVERY_S, cameras=[3])
	# Three cameras ending in the same check: one message (a GPU reset ends every VA-API encoder at once)
	assert rig.Sent()[1:] == [":red_circle: *Test rig*: 3 cameras at once: their recordings have ended.\n"
		"- Camera0: the camera is gone (unplugged, or its USB link lost) after frame 300. Its "
		"recording has ended there; the other cameras go on. It needs a replug, and a new recording.\n"
		"- Camera1: ffmpeg stopped after taking 300 frames, and the camera has stopped. Its "
		"video can be read up to its last fragment; the other cameras go on.\n"
		"- Camera2: its camera process stopped, with exit code -9 (SIGKILL): killed, or "
		"crashed. The other cameras go on."]
	assert sorted(p["key"] for p in rig.watch.Status("recording")["open"]) == ["Camera0/ended", "Camera1/ended",
		"Camera2/ended"] # each camera's own, in recording.json
	rig.Run(notify.REMIND_S + 60, cameras=[3])
	assert len(rig.Sent()) == 2 # no reminders: once is enough
	# restartFfmpeg: a camera whose ffmpeg was restarted, and then stopped once too often
	rig.rows[3].Restarted(3)
	rig.rows[3].State(status.VIDEO_FAILED)
	rig.Run(watch.EVERY_S, cameras=[])
	assert rig.Sent()[-1] == ":red_circle: *Test rig*: Camera3: ffmpeg stopped again, in 0.part4.mp4, and the camera " \
		"has stopped, after frame {}. It had been restarted 3 times, and was not again (at most 3 an hour, never when " \
		"the disk is the cause). Its video files can be read up to their last fragments; the other cameras go " \
		"on.".format(status.Reader(rig.counters, 4).Row(3)["grabbed"]), rig.Sent()[-4:]
	rig.watch.Stop(dict(rig.cameras.failures, Camera0=["the camera is gone"], Camera1=["ffmpeg stopped"]))
	assert rig.Sent()[-1].startswith(":red_circle: *Test rig*: Recording 2026-10-23_01-00-00 ended after 31 min, 3 of "
		"4 cameras not to the end:\n- Camera0: 300 frames: the camera is gone\n")


def test_ctrl_c_is_no_alert(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path).Start()
	rig.Run(10)
	rig.stopping = True
	rig.rows[0].State(status.ENDED)
	rig.Run(30, cameras=[1])
	assert len(rig.Sent()) == 1
	rig.watch.Stop({})
	assert rig.Sent()[-1] == (":information_source: *Test rig*: Recording 2026-10-23_01-00-00 ended after 40 s:\n"
		"- Camera0: 300 frames\n- Camera1: 1200 frames\n- Camera2: 300 frames")


def test_the_disk_fills_and_the_recording_stops_in_time(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path, free=30e9, recTimeInSec=7200).Start()
	rate = 50e6 # bytes a second
	said = len(rig.Sent())
	for _ in range(30): # 5 min
		rig.Run(watch.DISK_EVERY_S)
		rig.disk.free -= rate * watch.DISK_EVERY_S
	# 15 GB left, at 50 MB/s: 2 GB free in 4 min 20 s... well before the end
	alerts = rig.Sent()[said:]
	assert ":large_orange_circle: *Test rig*: Only 19.5 GB free on the disk with {} (alertDiskFreeGB " \
		"20).".format(rig.watch.Folders()[0]) in alerts
	full = [t for t in alerts if "fills at 50 MB/s" in t]
	assert full and full[0].startswith(":red_circle: *Test rig*: The disk with ")
	assert "while the cameras record: down to 2.00 GB free after 8 min 20 s more of recording, 1 h 50 min of " \
		"recording before it ends. campy will then stop the recording, cleanly." in full[0]
	assert rig.stops == []
	while rig.disk.free >= 2e9:
		rig.Run(watch.DISK_EVERY_S)
		rig.disk.free -= rate * watch.DISK_EVERY_S
	rig.Run(watch.DISK_EVERY_S)
	assert rig.Sent()[-1].startswith(":red_circle: *Test rig*: Only 1.50 GB free on the disk with ")
	assert rig.Sent()[-1].endswith(": stopping the recording now, cleanly, so that every video and its metadata are "
		"whole (stopAtFreeGB 2).")
	# Stopped as at Ctrl-C (campy.Main's stop request: each camera stops itself)
	assert rig.stops == [rig.watch.stopped] and rig.watch.stopped.startswith("only 1.50 GB free on the disk with ")
	rig.watch.Stop({})
	assert "stopped by campy after" in rig.Sent()[-1]


def test_a_disk_filled_in_bouts_is_counted_in_frames(monkeypatch, tmp_path):
	# A trigger that runs the cameras 10 s in every 300 s: recTimeInSec is camera time (600 s here, an hour and a
	# quarter of wall-clock time). 100 MB/s while they record: 60 GB to go, with 30 GB free. Counted in wall-clock
	# time, the disk filled at 3.3 MB/s and the recording had 10 min left: no alert. In frames, it does not last.
	rig = Rig(monkeypatch, tmp_path, free=30e9, recTimeInSec=600, alertNoFramesSec=400).Start()
	said = len(rig.Sent())
	for _ in range(2):
		for _ in range(10): # the disk fills as the frames come
			rig.disk.free -= 100e6
			rig.Run(1)
		rig.Run(290, cameras=[])
	rig.Run(1)
	full = [t for t in rig.Sent()[said:] if "fills at" in t]
	assert len(full) == 1 and full[0].startswith(":red_circle: *Test rig*: The disk with ")
	assert "fills at 100 MB/s while the cameras record: down to 2.00 GB free after 4 min 20 s more of recording, " \
		"5 min 20 s of recording before it ends." in full[0]


def bouts_with_spikeglx(rig, cycles):
	# The ChronicRecord rig's kind of plan: one long bout then five short ones, one every 300 s. The cameras' videos
	# fill the disk at 100 MB/s while they record, and SpikeGLX at 25 MB/s while the gate is open around them, frames
	# or none (5 s before the cameras, 15 s after a short bout's, 5 s after a long one's)
	def quiet(seconds):
		for _ in range(seconds):
			rig.disk.free -= 25e6
			rig.Run(1, cameras=[])
	def frames(seconds):
		for _ in range(seconds):
			rig.disk.free -= 100e6
			rig.Run(1)
	rig.Run(watch.DISK_EVERY_S, cameras=[]) # the watch's first look at the disk, before the first bout
	for _ in range(cycles):
		quiet(5); frames(100); quiet(5); rig.Run(190, cameras=[])
		for _ in range(5):
			quiet(5); frames(10); quiet(15); rig.Run(270, cameras=[])


def test_a_disk_filled_in_bouts_with_planned_pauses_is_counted_over_the_whole_recording(monkeypatch, tmp_path):
	# 2 cycles: 300 s of camera, 36.5 GB in all, with 40 GB free: it lasts (3.5 GB left). The last 5 minutes over a
	# short bout's frames say 150 MB/s and that it does not (the alerts the rig sent all night); the whole
	# recording's average says about 120 MB/s, and that it does
	rig = Rig(monkeypatch, tmp_path, free=40e9, recTimeInSec=300, alertNoFramesSec=400, alertTriggerPauseSec=30,
		alertDiskFreeGB=1).Start()
	said = len(rig.Sent())
	bouts_with_spikeglx(rig, 2)
	assert not [t for t in rig.Sent()[said:] if "fills at" in t], rig.Sent()[said:]
	assert rig.disk.free > 2e9 and rig.stops == []
	# Without planned pauses, the last 5 minutes as before: here, wrongly, it doesn't last
	rig = Rig(monkeypatch, tmp_path, free=40e9, recTimeInSec=300, alertNoFramesSec=400, alertDiskFreeGB=1).Start()
	said = len(rig.Sent())
	bouts_with_spikeglx(rig, 2)
	assert [t for t in rig.Sent()[said:] if "fills at" in t]


def test_a_disk_that_lasts(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path, free=300e9, recTimeInSec=3600).Start()
	for _ in range(60):
		rig.Run(watch.DISK_EVERY_S)
		rig.disk.free -= 10e6 * watch.DISK_EVERY_S # 36 GB an hour
	assert len(rig.Sent()) == 1


def test_never_stopped_with_stop_at_0(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path, free=1e9, stopAtFreeGB=0).Start()
	rig.Run(watch.DISK_EVERY_S * 2)
	assert rig.stops == [] and rig.watch.stopped is None
	assert "Only 1.00 GB free" in rig.Sent()[-1]


def test_the_trigger_port_is_gone(monkeypatch, tmp_path):
	serial = Serial()
	rig = Rig(monkeypatch, tmp_path, systems={"serial": serial}).Start()
	rig.Run(10)
	serial.gone = "[Errno 6] Device not configured"
	rig.Run(watch.SERIAL_EVERY_S + watch.EVERY_S)
	assert rig.Sent()[-1] == (":red_circle: *Test rig*: The trigger Arduino on COM3 is gone ([Errno 6] Device not "
		"configured): it sends no more trigger pulses, so the cameras get no frames. Replug it; campy does not "
		"restart the trigger mid-recording.")


def test_the_trigger_port_comes_back(monkeypatch, tmp_path):
	# As on the EVO-X2: the port's device file goes with it. Its connection is closed, so that a replug brings it
	# back under the same name (Linux gives a port still held open's replug the next name, /dev/ttyACM1)
	serial = Serial()
	rig = Rig(monkeypatch, tmp_path, systems={"serial": serial}, serialPort="/dev/ttyACM0").Start()
	gone = {"now": None}
	rig.watch.port_gone = lambda port: gone["now"]
	rig.Run(10)
	serial.gone, gone["now"] = "[Errno 5] Input/output error", "/dev/ttyACM0 is gone"
	rig.Run(watch.SERIAL_EVERY_S + watch.EVERY_S)
	assert serial.closed
	assert rig.Sent()[-1].startswith(":red_circle: *Test rig*: The trigger Arduino on /dev/ttyACM0 is gone ([Errno 5]")
	gone["now"] = None # replugged
	rig.Run(watch.SERIAL_EVERY_S + watch.EVERY_S)
	assert rig.Sent()[-1].endswith(": The trigger Arduino's port /dev/ttyACM0 is back. campy does not restart the "
		"trigger: the cameras get frames again in the next recording.")


def test_problems_still_open_as_a_stopped_recording_ends(monkeypatch, tmp_path):
	# Stopped (Ctrl-C, its stop file) while a camera had no frames: it ended as asked, but did not record
	rig = Rig(monkeypatch, tmp_path, names=["Camera0", "Camera1"]).Start()
	rig.Run(10)
	rig.Run(30, cameras=[0])
	assert rig.Sent()[-1] == ":red_circle: *Test rig*: Camera1: no frame for 12 s, since frame 300"
	rig.stopping = True
	rig.Run(5, cameras=[0])
	rig.watch.Stop({})
	assert rig.Sent()[-1].startswith(":red_circle: *Test rig*: Recording 2026-10-23_01-00-00 ended after 45 s, with "
		"problems still open:\n- Camera0: 1350 frames\n- Camera1: 300 frames\nStill open as it ended:\n- Camera1: no "
		"frame for ")
	with open(watch.StatusPath()) as f:
		assert json.load(f)["state"] == "failed"


@pytest.mark.skipif(sys.platform == "win32", reason="device files: Linux and macOS")
def test_port_gone():
	assert watch.PortGone("/dev/ttyACM-no-such-port") == "/dev/ttyACM-no-such-port is gone"
	assert watch.PortGone(os.devnull) is None


def test_a_camera_drops_to_usb_2(monkeypatch, tmp_path):
	devices = [("usb4", 4, 10000, "4-1", "1e10", "24243504", 5000)]
	root = make_sysfs(tmp_path / "sys", devices)
	p = configurator.DefaultParams()
	p.update(numCams=1, cameraMake="flir")
	cam = configurator.CameraParams(configurator.FillCameraLists(p), 0)
	cam["cameraSerialNo"] = "24243504"
	rig = Rig(monkeypatch, tmp_path, names=["Camera0"], cams=[cam], sysfs_root=root).Start()
	rig.Run(watch.USB_EVERY_S)
	assert len(rig.Sent()) == 1
	(tmp_path / "sys" / "devices" / "pci0000:00" / "0000:00:08.1" / "usb4" / "usb4" / "4-1" / "speed").write_text("480\n")
	rig.Run(watch.USB_EVERY_S)
	assert rig.Sent()[-1] == (":red_circle: *Test rig*: Camera0 is now on a USB 2 link (480 Mb/s), not USB 3: it "
		"can't carry its frames, so expect lost frames. Check its cable and port.")


def test_a_camera_drops_to_usb_2_on_windows(monkeypatch, tmp_path):
	# Its cable or port fails: it leaves (the camera says that itself) and comes back on the port's USB 2 side
	from campy import preflight, usbwin
	from test_usbwin import slim, camera, ROOT, HS
	windows = slim()
	monkeypatch.setattr(preflight, "WINDOWS", True)
	monkeypatch.setattr(usbwin, "Api", lambda: windows)
	p = configurator.DefaultParams()
	p.update(numCams=1, cameraMake="flir")
	cam = configurator.CameraParams(configurator.FillCameraLists(p), 0)
	cam["cameraSerialNo"] = "24243504" # 0171ED30 in hex, as Windows' device list writes it
	rig = Rig(monkeypatch, tmp_path, names=["Camera0"], cams=[cam]).Start()
	rig.Run(watch.USB_EVERY_S)
	assert len(rig.Sent()) == 1
	assert rig.watch.Status("recording")["cameras"][0]["usb_speed"] == 5000
	del windows.devices[camera("0171ED30")]
	rig.Run(watch.USB_EVERY_S) # gone: nothing said here
	windows.Plug(camera("0171ED30"), ROOT, 4, HS)
	rig.Run(watch.USB_EVERY_S)
	assert rig.Sent()[-1] == (":red_circle: *Test rig*: Camera0 is now on a USB 2 link (480 Mb/s), not USB 3: it "
		"can't carry its frames, so expect lost frames. Check its cable and port.")
	assert rig.watch.Status("recording")["cameras"][0]["usb_speed"] == 480


def test_windows_usb_check_failing_is_said_once(monkeypatch, tmp_path):
	from campy import preflight, usbwin
	from test_usbwin import FakeWindows
	monkeypatch.setattr(preflight, "WINDOWS", True)
	monkeypatch.setattr(usbwin, "Api", lambda: FakeWindows({}, gone="Listing USB devices failed (CONFIGRET 0x3)"))
	p = configurator.DefaultParams()
	p.update(numCams=1, cameraMake="flir")
	cam = configurator.CameraParams(configurator.FillCameraLists(p), 0)
	cam["cameraSerialNo"] = "24243504"
	rig = Rig(monkeypatch, tmp_path, names=["Camera0"], cams=[cam]).Start()
	rig.Run(watch.USB_EVERY_S * 3)
	assert rig.said.count("Watch: the usb check failed: Listing USB devices failed (CONFIGRET 0x3)") == 1


def test_a_failing_check_is_said_once_and_the_others_go_on(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path).Start()

	def broken(path):
		raise OSError("disk_usage failed")
	rig.watch.disk_usage = broken
	rig.Run(watch.DISK_EVERY_S)
	rig.Run(watch.DISK_EVERY_S * 3, cameras=[0, 2])
	assert rig.said.count("Watch: the disk check failed: disk_usage failed") == 1
	assert "Camera1: no frame for" in rig.Sent()[-1]


def test_a_check_that_keeps_failing_is_sent_once_and_resolved_once_it_works(monkeypatch, tmp_path):
	# A bug in the cameras check turned off the camera alerts silently: two cameras stopped for 2 minutes, and
	# nothing was sent. Now it is a warning, once, and it is resolved once the check works again
	rig = Rig(monkeypatch, tmp_path).Start()
	rig.Run(10)
	works = rig.watch.CheckCameras

	def broken(now):
		raise KeyError("missedNs")
	rig.watch.CheckCameras = broken
	rig.Run(120, cameras=[])
	assert rig.said.count("Watch: the cameras check failed: 'missedNs'") == 1
	assert rig.Sent()[1:] == [":large_orange_circle: *Test rig*: The recording watch's cameras check failed: "
		"'missedNs'. Its alerts are off while it fails; the recording goes on."]
	assert [p["key"] for p in rig.watch.Status("recording")["open"]] == ["watch/cameras"]
	rig.watch.CheckCameras = works
	rig.Run(watch.EVERY_S, cameras=[])
	assert rig.Sent()[-2].startswith(":red_circle: *Test rig*: No camera has had a frame for ")
	assert rig.Sent()[-1] == ":white_check_mark: *Test rig*: resolved, after 2 min: The recording watch's cameras " \
		"check works again."


def test_a_failing_step_outside_the_checks_never_ends_the_watch(monkeypatch, tmp_path):
	# Resolutions, the heartbeat and the alerts' reminders ran outside the checks' try: one that raised ended the
	# watch's thread, and with it every alert, for the rest of the recording
	rig = Rig(monkeypatch, tmp_path).Start()

	def broken(*args):
		raise RuntimeError("made-up error")
	monkeypatch.setattr(rig.watch, "Settle", broken)
	monkeypatch.setattr(rig.notifier, "Heartbeat", broken)
	rig.Run(10)
	assert rig.said.count("Watch: the resolutions check failed: made-up error") == 1
	assert rig.said.count("Watch: the heartbeat check failed: made-up error") == 1
	assert [t.split("*Test rig*: ", 1)[1][:44] for t in rig.Sent()[1:]] == [
		"The recording watch's resolutions check fail", "The recording watch's heartbeat check failed"]
	# In its thread: it goes on checking
	monkeypatch.setattr(watch, "EVERY_S", 0.01)
	monkeypatch.setattr(rig.watch, "CheckCameras", broken)
	rig.watch.thread = threading.Thread(target=rig.watch.Run, daemon=True)
	rig.watch.thread.start()
	time.sleep(0.3)
	assert rig.watch.thread.is_alive()
	rig.watch.Halt()
	assert not rig.watch.thread.is_alive()


def test_an_error_that_ended_campy_acquire_makes_the_summary_critical(monkeypatch, tmp_path):
	# campy-acquire's camera wait raised: its end summary was an info "ended", which a warning channel never got,
	# and recording.json said "ended"
	rig = Rig(monkeypatch, tmp_path).Start()
	rig.Run(60)
	rig.watch.Stop(None, error="RuntimeError: something inside cameras.Wait failed")
	assert rig.Sent()[-1] == ":red_circle: *Test rig*: Recording 2026-10-23_01-00-00 ended after 1 min with an error " \
		"in campy-acquire: RuntimeError: something inside cameras.Wait failed. Its log says more.\n" \
		"- Camera0: 1800 frames\n- Camera1: 1800 frames\n- Camera2: 1800 frames"
	with open(watch.StatusPath()) as f:
		assert json.load(f)["state"] == "failed"


def test_heartbeat_and_pause(monkeypatch, tmp_path):
	beats = []
	hc = types.SimpleNamespace(name="hc", Ping=lambda: beats.append("ping"), Pause=lambda: beats.append("pause"))
	rig = Rig(monkeypatch, tmp_path)
	rig.notifier.heartbeat = hc
	rig.Start()
	rig.Run(150)
	rig.watch.Stop({})
	assert beats == ["ping", "ping", "ping", "pause"]


def test_recording_json(monkeypatch, tmp_path):
	monkeypatch.setenv("CAMPY_STATUS_DIR", str(tmp_path / "status"))
	rig = Rig(monkeypatch, tmp_path, free=100e9, recTimeInSec=600).Start()
	path = tmp_path / "status" / "recording.json"
	first = json.loads(path.read_text())
	assert first["schema"] == 1 and first["state"] == "recording" and first["host_model"] == "Test rig"
	assert first["session"]["folder"].endswith("2026-10-23_01-00-00") and first["session"]["planned_s"] == 600
	rig.Run(30, cameras=[0, 2])
	rig.disk.free -= 1e9
	rig.Run(80)
	status = json.loads(path.read_text())
	assert status["session"]["elapsed_s"] == 110
	cams = {c["name"]: c for c in status["cameras"]}
	assert cams["Camera0"]["frames"] == 3300 and cams["Camera0"]["fps_measured"] == 30 and cams["Camera0"]["state"] \
		== "grabbing" and cams["Camera0"]["write_queue_s"] == 0 and cams["Camera0"]["fps_target"] == 30
	(disk,) = status["disk"]
	assert disk["free_gb"] == 99.0 and disk["projected_full_at"] is not None
	assert status["last_error"]["text"].startswith("Camera1: no frame yet") and status["open"] == []
	rig.watch.Stop({})
	assert json.loads(path.read_text())["state"] == "ended"
	log = [json.loads(line) for line in (tmp_path / "2026-10-23_01-00-00" / "alerts.log").read_text().splitlines()]
	assert [(l["key"], l["event"], l["state"]) for l in log if l["status"] == "queued"] == [(None, "note", None),
		("Camera1/no-frames", "problem", "open"), ("Camera1/no-frames", "resolved", "resolved"), (None, "note", None)]


# ---------------------------------------------------------------------- the GPU's kernel log


class KernelLog:
	"""A made-up gpulog.Reader: Feed(line) is a line the kernel logged."""

	def __init__(self, source="journalctl", why=None):
		self.source, self.why = source, why
		self.events, self.notes = [], []
		self.stopped = False

	def Feed(self, *lines):
		for line in lines:
			found = gpulog.Classify(line)
			if found is not None:
				self.events.append(found + (line,))

	def Take(self):
		taken, self.events = self.events, []
		return taken

	def Notes(self):
		taken, self.notes = self.notes, []
		return taken

	def Stop(self):
		self.stopped = True


DEV = "amdgpu 0000:c5:00.0: amdgpu: "
STALL = DEV + "Fence fallback timer expired on ring comp_1.0.0"


def test_firmware_stalls_above_the_threshold(monkeypatch, tmp_path):
	log = KernelLog()
	rig = Rig(monkeypatch, tmp_path, gpu_log=log).Start()
	rig.Run(10)
	log.Feed(STALL, DEV + "amdgpu: Freeing queue vital buffer 0x7f1c3e400000, queue evicted")
	rig.Run(120)
	log.Feed(DEV + "Fence fallback timer expired on ring vcn_unified_0")
	rig.Run(120)
	assert len(rig.Sent()) == 1 # 2 in 10 min: not yet
	log.Feed(STALL)
	rig.Run(watch.EVERY_S)
	assert rig.Sent()[-1] == (":large_orange_circle: *Test rig*: GPU: 3 firmware stalls in the last 10 min (1 in "
		"the last minute), on comp_1.0.0 (compute: campy-live, and Mesa's copies into tiled video surfaces) \u00d72, "
		"vcn_unified_0 (video encoder: the cameras' VA-API encoders) \u00d71. Each held that engine up for about half "
		"a second; on a camera's encoder it shows as frames waiting for ffmpeg, and in campy-live as a late set. Last: "
		+ STALL)
	log.Feed(STALL, STALL)
	rig.Run(60)
	assert len(rig.Sent()) == 2 # still the same problem: nothing more
	rig.Run(watch.GPU_WINDOW_S - 60 - watch.EVERY_S)
	assert len(rig.Sent()) == 2 # some still within 10 min
	rig.Run(watch.EVERY_S * 3)
	assert rig.Sent()[-1].startswith(":white_check_mark: *Test rig*: resolved, after 10 min")
	assert rig.Sent()[-1].endswith("GPU: no firmware stall for 10 min (5 in all)")


def test_stalls_far_apart_are_no_alert(monkeypatch, tmp_path):
	log = KernelLog()
	rig = Rig(monkeypatch, tmp_path, gpu_log=log, alertGpuStalls=3).Start()
	for _ in range(6): # one every 6 minutes: at most 2 within 10
		log.Feed(STALL)
		rig.Run(360)
	assert len(rig.Sent()) == 1
	rig.watch.Stop({})
	assert rig.Sent()[-1].endswith("- GPU kernel log: 6 firmware stalls, no GPU reset (comp_1.0.0 \u00d76)")


def test_a_lower_threshold(monkeypatch, tmp_path):
	log = KernelLog()
	rig = Rig(monkeypatch, tmp_path, gpu_log=log, alertGpuStalls=1).Start()
	log.Feed(STALL)
	rig.Run(watch.EVERY_S)
	assert rig.Sent()[-1].startswith(":large_orange_circle: *Test rig*: GPU: 1 firmware stalls in the last 10 min")


def test_a_hung_gpu_job(monkeypatch, tmp_path):
	log = KernelLog()
	rig = Rig(monkeypatch, tmp_path, gpu_log=log).Start()
	rig.Run(10)
	line = DEV + "ring comp_1.1.0 timeout, signaled seq=40917, emitted seq=40919"
	log.Feed(line)
	rig.Run(watch.EVERY_S)
	assert rig.Sent()[-1] == (":large_orange_circle: *Test rig*: GPU: a job on the GPU hung (1 in the last 10 min, "
		"on comp_1.1.0 (compute: campy-live, and Mesa's copies into tiled video surfaces) \u00d71): the encoders or "
		"campy-live on it waited. A GPU reset may follow. Last: " + line)
	rig.Run(watch.GPU_WINDOW_S + watch.EVERY_S)
	assert rig.Sent()[-1].endswith("GPU: no hung GPU job for 10 min")


def test_a_gpu_reset_is_critical(monkeypatch, tmp_path):
	log = KernelLog()
	rig = Rig(monkeypatch, tmp_path, gpu_log=log).Start()
	rig.Run(10)
	log.Feed(DEV + "ring comp_1.0.0 timeout, signaled seq=1, emitted seq=3", DEV + "Starting comp_1.0.0 ring reset",
		DEV + "Ring comp_1.0.0 reset succeeded")
	rig.Run(watch.EVERY_S)
	sent = rig.Sent()[1:]
	assert sent[0].startswith(":large_orange_circle: *Test rig*: GPU: a job on the GPU hung")
	assert sent[1] == (":red_circle: *Test rig*: GPU: the driver reset the GPU's comp_1.0.0 (compute: campy-live, and "
		"Mesa's copies into tiled video surfaces) \u00d71. A reset ends what was running there: expect the cameras' "
		"VA-API encoders (ffmpeg) or campy-live to fail; the camera alerts say which. Last: " + DEV + "Ring comp_1.0.0 "
		"reset succeeded")
	# A whole-GPU reset, later: the same problem, its text now this one's
	log.Feed(DEV + "GPU reset(2) succeeded!")
	rig.Run(watch.EVERY_S)
	assert rig.watch.problems["gpu/reset"].text.endswith("Last: " + DEV + "GPU reset(2) succeeded!")
	rig.watch.Stop({})
	end = rig.Sent()[-1]
	assert end.startswith(":red_circle: *Test rig*: Recording 2026-10-23_01-00-00 ended after 14 s, with problems "
		"still open:")
	assert "- GPU kernel log: 1 hung GPU job, 2 GPU resets (comp_1.0.0 \u00d72)" in end
	assert "Still open as it ended:\n- GPU: a job on the GPU hung" in end and "- GPU: the driver reset" in end
	assert log.stopped


def test_the_gpu_reset_resolves_once_quiet(monkeypatch, tmp_path):
	log = KernelLog()
	rig = Rig(monkeypatch, tmp_path, gpu_log=log).Start()
	log.Feed(DEV + "GPU reset(1) succeeded!")
	rig.Run(watch.EVERY_S)
	rig.Run(watch.GPU_WINDOW_S + watch.EVERY_S)
	assert rig.Sent()[-1].endswith("GPU: no GPU reset for 10 min (1 in all)")


def test_a_kernel_log_it_can_not_read_is_said_once(monkeypatch, tmp_path):
	log = KernelLog(source=None, why="journalctl can't read the kernel log")
	rig = Rig(monkeypatch, tmp_path, gpu_log=log).Start()
	log.notes.append(("Watch: can't read the kernel log (journalctl ...), so GPU firmware stalls and resets are not "
		"watched in this recording. " + gpulog.FIX, True))
	rig.Run(30)
	assert [s for s in rig.said if "kernel log" in s] == [log.Notes() or "Watch: can't read the kernel log (journalctl "
		"...), so GPU firmware stalls and resets are not watched in this recording. " + gpulog.FIX]
	logged = [json.loads(line) for line in (tmp_path / "2026-10-23_01-00-00" / "alerts.log").read_text().splitlines()]
	assert any("can't read the kernel log" in (l["text"] or "") for l in logged)
	assert len(rig.Sent()) == 2 and rig.Sent()[1].startswith(":information_source:") # info: only info channels
	assert rig.watch.Status("recording")["gpu_log"]["not_read"] == "journalctl can't read the kernel log"


def test_reading_the_kernel_log_is_said_on_the_console_only(monkeypatch, tmp_path):
	log = KernelLog()
	rig = Rig(monkeypatch, tmp_path, gpu_log=log).Start()
	log.notes.append(("Watch: reading the kernel log (journalctl) for GPU errors.", False))
	rig.Run(10)
	assert "Watch: reading the kernel log (journalctl) for GPU errors." in rig.said
	assert len(rig.Sent()) == 1


def test_recording_json_gpu_log(monkeypatch, tmp_path):
	monkeypatch.setenv("CAMPY_STATUS_DIR", str(tmp_path / "status"))
	log = KernelLog()
	rig = Rig(monkeypatch, tmp_path, gpu_log=log).Start()
	path = tmp_path / "status" / "recording.json"
	assert json.loads(path.read_text())["gpu_log"] == {"read_with": "journalctl", "not_read": None, "stalls": 0,
		"hangs": 0, "resets": 0, "stalls_last_minute": 0, "rings": {}}
	log.Feed(STALL, STALL, DEV + "Fence fallback timer expired on ring vcn_unified_0")
	rig.Run(90)
	log.Feed(STALL)
	rig.Run(10)
	assert json.loads(path.read_text())["gpu_log"] == {"read_with": "journalctl", "not_read": None, "stalls": 4,
		"hangs": 0, "resets": 0, "stalls_last_minute": 1, "rings": {"comp_1.0.0": 3, "vcn_unified_0": 1}}
	# Elsewhere (not Linux, no AMD GPU, alertGpuLog False): null, and no line at the end
	(tmp_path / "plain").mkdir()
	plain = Rig(monkeypatch, tmp_path / "plain").Start()
	assert plain.watch.Status("recording")["gpu_log"] is None
	plain.watch.Stop({})
	assert "GPU" not in plain.Sent()[-1]


def test_start_reads_the_kernel_log_only_where_it_should(monkeypatch, tmp_path):
	started = []

	class Reader:
		def Start(self):
			started.append(self)
			return self

	monkeypatch.setattr(gpulog, "Reader", Reader)
	for wanted, on, reads in ((False, True, False), (True, False, False), (True, True, True)):
		monkeypatch.setattr(gpulog, "Wanted", lambda root, wanted=wanted: wanted)
		rig = Rig(monkeypatch, tmp_path, alertGpuLog=on)
		rig.watch.Run = lambda: None # no thread checking
		rig.watch.Start()
		assert (rig.watch.gpuLog is not None) == reads
		rig.watch.gpuLog = None
		rig.watch.Stop({}, timeout=0)
	assert len(started) == 1


def test_the_trigger_pauses(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path, cameraTrigger="Line3").Start()
	rig.Run(20)
	rig.Pause(0, 68)
	rig.watch.Check() # Camera0's next frame came; the others' are a few ms behind: not said yet
	assert len(rig.Sent()) == 1
	rig.clock.now += 0.004
	rig.Pause(1, 68)
	rig.Pause(2, 68)
	rig.Run(watch.EVERY_S)
	# One warning, not one per camera
	assert rig.Sent()[1:] == [":large_orange_circle: *Test rig*: The trigger paused: all 3 cameras got no frame for the "
		"same 68 pulses (2.27 s), just before frame 601, about 22 s in. No frame was lost, but from there on the frames "
		"are 2.27 s later than their numbers say at 30 fps; frametimes.npy has each one's time."]
	rig.Run(30)
	for c in range(3):
		rig.Pause(c, 10)
	rig.Run(watch.EVERY_S) # a second one, within 2 min: the same alert, its text changed
	assert len(rig.Sent()) == 2
	assert rig.watch.Open()["trigger/paused"].text.endswith(" Pause 2 of the recording.")
	status = rig.watch.Status("recording")
	assert status["trigger_pauses"] == [{"pulses": 68, "seconds": 2.267, "before_frame": 601, "planned": False},
		{"pulses": 10, "seconds": 0.333, "before_frame": 1562, "planned": False}]
	assert [c["trigger_pulses_without_frame"] for c in status["cameras"]] == [78, 78, 78]
	rig.Run(watch.MISSED_CLEAR_S + watch.EVERY_S)
	assert rig.Sent()[-1].endswith("Trigger: no pause for 2 min (2 in all)")
	rig.watch.Stop()
	assert rig.Sent()[-1].startswith(":large_orange_circle: *Test rig*: Recording 2026-10-23_01-00-00 ended after ")
	assert rig.Sent()[-1].split("\n")[0].endswith(", with the trigger paused:")
	assert rig.Sent()[-1].endswith("\n- The trigger paused 2 times: 78 pulses without a frame in every camera "
		"(2.60 s), the first just before frame 601. From each pause on, the frames are later than their numbers say; "
		"frametimes.npy has each one's time.")


def test_a_camera_misses_trigger_pulses(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path, cameraTrigger="Line3").Start()
	rig.Run(20)
	rig.Pause(1, 3)
	rig.Run(watch.EVERY_S)
	assert rig.Sent()[1:] == [":large_orange_circle: *Test rig*: Camera1: no frame for 3 trigger pulses (0.10 s) that "
		"the other cameras got, just before its frame 601. Counted from the start, its frames are out of step with "
		"theirs from there on; campy-align puts every camera on the trigger's timeline."]
	# The trigger pausing as Camera1 also misses a pulse: the pause, and Camera1's one more
	rig.Pause(0, 30)
	rig.Pause(1, 31)
	rig.Pause(2, 30)
	rig.Run(watch.EVERY_S)
	assert rig.Sent()[-1].startswith(":large_orange_circle: *Test rig*: The trigger paused: all 3 cameras got no frame "
		"for the same 30 pulses (1.00 s)")
	assert rig.watch.Open()["Camera1/missed"].text.startswith("Camera1: no frame for 1 trigger pulse (0.03 s) that "
		"the other cameras got, just before its frame 662 (4 in all).")
	rig.watch.Stop()
	lines = rig.Sent()[-1].split("\n")
	assert lines[0].endswith(", with the trigger paused:")
	assert lines[2].startswith("- Camera1: 722 frames, no frame for 4 trigger pulses the other cameras got")
	assert lines[1] == "- Camera0: 721 frames" # (Camera1 had one more: its first pause's frame)


def test_untriggered_cameras_have_no_pulses_to_miss(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path).Start() # cameraTrigger "None": free-running
	rig.Run(20)
	for c in range(3):
		rig.Pause(c, 68)
	rig.Run(watch.EVERY_S)
	rig.watch.Stop()
	assert len(rig.Sent()) == 2 and "paused" not in rig.Sent()[-1]


def test_a_lone_triggered_camera_can_not_tell_a_pause(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path, names=["Camera0"], cameraTrigger="Line3").Start()
	rig.Run(20)
	rig.Pause(0, 15)
	rig.Run(watch.EVERY_S)
	assert rig.Sent()[-1] == ":large_orange_circle: *Test rig*: Camera0: no frame for 15 trigger pulses (0.50 s), just " \
		"before frame 601: the trigger paused, or the camera missed them. From there on its frames are later than " \
		"their numbers say; frametimes.npy has each one's time."
	rig.watch.Stop()
	assert "- Camera0: 661 frames, no frame for 15 trigger pulses" in rig.Sent()[-1]


def bouts(rig, pulses, n=3):
	# n bouts: 10 s of frames, then every camera's next frame after pulses without one
	for _ in range(n):
		rig.Run(10)
		rig.clock.now += pulses / 30
		for c in range(len(rig.names)):
			rig.Pause(c, pulses)
		rig.Run(watch.EVERY_S)


def test_planned_pauses_are_logged_not_sent(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path, cameraTrigger="Line3", alertTriggerPauseSec=60, alertNoFramesSec=400).Start()
	bouts(rig, 8700) # 290 s between bouts
	assert len(rig.Sent()) == 1 # only the start
	assert "trigger/paused" not in rig.watch.Open()
	assert [s for s in rig.said if s.startswith("Trigger: ")][0] == "Trigger: The trigger paused as planned: all 3 " \
		"cameras got no frame for the same 8700 pulses (4 min 50 s), just before frame 301, about 5 min 2 s in " \
		"(alertTriggerPauseSec 60: logged, not sent). Planned pause 1 of the recording."
	with open(os.path.join(rig.watch.folder, "alerts.log"), encoding="utf-8") as f:
		logged = [json.loads(line) for line in f if "planned pause" in line]
	assert len(logged) == 3 and logged[2]["key"] == "trigger/planned-pause" and logged[2]["severity"] == "info"
	assert logged[2]["status"] == "logged only" and logged[2]["text"].endswith("Planned pause 3 of the recording.")
	status = rig.watch.Status("recording")
	assert [p["planned"] for p in status["trigger_pauses"]] == [True] * 3
	assert status["trigger_pauses"][0] == {"pulses": 8700, "seconds": 290.0, "before_frame": 301, "planned": True}
	rig.watch.Stop()
	lines = rig.Sent()[-1].split("\n")
	assert lines[0].startswith(":information_source: *Test rig*: Recording 2026-10-23_01-00-00 ended after ")
	assert lines[-1] == "- The trigger paused as planned 3 times (alertTriggerPauseSec 60): 14 min in all, " \
		"the first just before frame 301."


def test_a_short_pause_is_still_an_alert(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path, cameraTrigger="Line3", alertTriggerPauseSec=60, alertNoFramesSec=400).Start()
	bouts(rig, 8700, n=1)
	bouts(rig, 300, n=1) # 10 s: below alertTriggerPauseSec, a fault
	assert rig.Sent()[-1].startswith(":large_orange_circle: *Test rig*: The trigger paused: all 3 cameras got no frame "
		"for the same 300 pulses (10.00 s)")
	# Its "later than their numbers say" counts the planned pause too
	assert "from there on the frames are 300.00 s later than their numbers say" in rig.Sent()[-1]
	rig.watch.Stop()
	lines = rig.Sent()[-1].split("\n")
	assert lines[0].endswith(", with the trigger paused:")
	assert lines[-2].startswith("- The trigger paused 1 time: 300 pulses")
	assert lines[-1].startswith("- The trigger paused as planned 1 time (alertTriggerPauseSec 60): 4 min 50 s in all")


def test_a_lone_camera_planned_pause(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path, names=["Camera0"], cameraTrigger="Line3", alertTriggerPauseSec=60,
		alertNoFramesSec=400).Start()
	bouts(rig, 8700, n=2)
	assert len(rig.Sent()) == 1
	rig.watch.Stop()
	assert rig.Sent()[-1].split("\n")[1] == "- Camera0: 722 frames" # no "no frame for ... pulses"
