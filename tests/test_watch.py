"""
campy-acquire's recording watch (campy/watch.py), check by check, on made-up
counters and a made-up clock: a camera with no frames, all of them at once
(the trigger), the first frame late, dropped frames, a stalled writer, a
camera that ends, the disk filling (and the clean stop before it is full),
the trigger's port gone, a camera on USB 2; and the messages as the
recording starts and ends. tests/test_watch_acquire.py runs it in
campy-acquire, with emulated cameras and injected faults.
"""

import ctypes
import json
import os
import sys
import types

import pytest


from campy import configurator, notify, status, watch

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
			cams=None, sysfs_root=None, **values):
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
			port_gone=lambda port: None)
		self.grabbed = [0] * len(names)
		self.dropped = [0] * len(names)
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
	rig = Rig(monkeypatch, tmp_path, systems={}).Start()
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


def test_cameras_that_end(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path, names=["Camera0", "Camera1", "Camera2", "Camera3"]).Start()
	rig.Run(10)
	rig.rows[0].State(status.LOST)
	rig.rows[1].State(status.VIDEO_FAILED)
	rig.rows[2].State(status.ENDED) # its process then died without saying why
	rig.cameras.failures["Camera2"] = ["its camera process stopped, with exit code -9 (SIGKILL): killed, or crashed"]
	rig.Run(watch.EVERY_S, cameras=[3])
	assert rig.Sent()[1:] == [
		":red_circle: *Test rig*: Camera0: the camera is gone (unplugged, or its USB link lost) after frame 300. Its "
		"recording has ended there; the other cameras go on. It needs a replug, and a new recording.",
		":red_circle: *Test rig*: Camera1: ffmpeg stopped after taking 300 frames, and the camera has stopped. Its "
		"video can be read up to its last fragment; the other cameras go on.",
		":red_circle: *Test rig*: Camera2: its camera process stopped, with exit code -9 (SIGKILL): killed, or "
		"crashed. The other cameras go on."]
	rig.Run(notify.REMIND_S + 60, cameras=[3])
	assert len(rig.Sent()) == 4 # no reminders: once is enough
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
	assert "before the recording ends. campy will then stop the recording, cleanly." in full[0]
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


def test_a_failing_check_is_said_once_and_the_others_go_on(monkeypatch, tmp_path):
	rig = Rig(monkeypatch, tmp_path).Start()

	def broken(path):
		raise OSError("disk_usage failed")
	rig.watch.disk_usage = broken
	rig.Run(watch.DISK_EVERY_S)
	rig.Run(watch.DISK_EVERY_S * 3, cameras=[0, 2])
	assert rig.said.count("Watch: the disk check failed: disk_usage failed") == 1
	assert "Camera1: no frame for" in rig.Sent()[-1]


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
