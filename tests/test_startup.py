"""What a camera says as it opens: one line once it is ready (campy/startup.py)."""

import os, sys

from campy import startup
from campy.cameras import unicam


class Stdout:
	# Stands in for sys.stdout: one entry per write() call
	def __init__(self):
		self.writes = []

	def write(self, text):
		self.writes.append(text)
		return len(text)

	def flush(self):
		pass


def flir_params(**kw):
	p = dict(n_cam=1, cameraName="Camera1", cameraMake="flir", cameraModel="Blackfly S BFS-U3-13Y3M",
		cameraSerialNo="23501329", frameWidth=1280, frameHeight=1024, frameRate=80, cameraTrigger="Line3",
		videoFolder="/data/session", videoFilename="0.mp4", gpuID=0, codec="hevc")
	p.update(kw)
	return p


def open_like_flir(p):
	# What cameras/flir.py says, step by step, as a Blackfly S opens
	startup.Begin(p)
	startup.Step("Trigger source set to Line3...")
	startup.Setting("trigger", "trigger Line3")
	startup.Step("Width set to 1280...")
	startup.Step("Height set to 1024...")
	startup.Step("Shutter time set to 750 us...")
	startup.Setting("exposure", "exposure 750 us")
	startup.Step("Gain set to 3 dB.")
	startup.Setting("gain", "gain 3 dB")
	startup.Step("Gamma off.")
	startup.Setting("gamma", "gamma off")
	startup.Step("Buffer count now set to: 100")
	startup.Setting("buffers", "100 buffers")
	startup.Step("BufferMode has been set to Oldest First")
	startup.Setting("buffers", "100 buffers (oldest first)")
	startup.Step("USB throughput limit 380 MB/s (frameRate needs 79)...")
	startup.Setting("usb", "USB limit 380 MB/s (frameRate needs 79)")
	startup.Step("Opened Camera1: flir Blackfly S BFS-U3-13Y3M serial# 23501329")


def test_a_camera_says_one_line_once_it_is_ready(monkeypatch):
	out = Stdout()
	monkeypatch.setattr(sys, "stdout", out)
	p = flir_params()
	open_like_flir(p)
	startup.Video("Opened: /data/session/Camera1/0.mp4 using GPU 0 to compress the stream.", p, first=True)
	assert out.writes == [] # nothing yet: every step is held
	startup.Ready(p)
	assert out.writes == ["Camera1 ready to trigger: flir Blackfly S BFS-U3-13Y3M serial 23501329, 1280x1024 at 80 fps, "
		"trigger Line3, exposure 750 us, gain 3 dB, gamma off, 100 buffers (oldest first), "
		"USB limit 380 MB/s (frameRate needs 79), hevc on GPU 0 -> {}\n".format(os.path.join("/data/session", "Camera1", "0.mp4"))]


def test_the_video_opened_after_the_ready_line_is_not_said_again(monkeypatch):
	out = Stdout()
	monkeypatch.setattr(sys, "stdout", out)
	p = flir_params()
	open_like_flir(p)
	startup.Ready(p)
	startup.Video("Opened: /data/session/Camera1/0.mp4 using GPU 0 to compress the stream.", p, first=True)
	assert len(out.writes) == 1
	# Another open (ffmpeg restarted into a next part) is said, as before
	startup.Video("Opened: /data/session/Camera1/0.part2.mp4 using GPU 0 to compress the stream.", p, first=False)
	assert out.writes[1] == "Opened: /data/session/Camera1/0.part2.mp4 using GPU 0 to compress the stream.\n"


def test_problems_are_said_at_once_with_the_cameras_name(monkeypatch):
	out = Stdout()
	monkeypatch.setattr(sys, "stdout", out)
	p = flir_params()
	startup.Begin(p)
	startup.Step("Width set to 1280...")
	startup.Warn("OffsetX cannot be set!")
	startup.Warn("Camera1: already named")
	assert out.writes == ["Camera1: OffsetX cannot be set!\n", "Camera1: already named\n"]


def test_a_camera_that_is_not_ready_says_what_it_did(monkeypatch):
	out = Stdout()
	monkeypatch.setattr(sys, "stdout", out)
	unicam.SetReadyQueue(None)
	p = flir_params()
	open_like_flir(p)
	unicam.SignalReady(p, False, "did not start grabbing")
	assert len(out.writes) == 1 # one write: no other camera's line inside it
	lines = out.writes[0].splitlines()
	assert lines[0] == "Camera1 is not ready: did not start grabbing. What it did as it opened:"
	assert lines[1] == "  Camera1: Trigger source set to Line3..."
	assert "  Camera1: BufferMode has been set to Oldest First" in lines
	# Said once: the camera thread's end does not say it again
	startup.Failed(p, "did not start grabbing")
	assert len(out.writes) == 1


def test_outside_a_camera_opening_each_step_is_said_at_once(monkeypatch):
	# campy's launch check, campy-encode-sweep, the FLIR functions on their own
	out = Stdout()
	monkeypatch.setattr(sys, "stdout", out)
	startup.Step("Gamma off.")
	startup.Video("Opened: x.mp4 using CPU to compress the stream.", {"cameraName": "Camera0"}, first=True)
	startup.Ready({"cameraName": "Camera0"})
	assert out.writes == ["Gamma off.\n", "Opened: x.mp4 using CPU to compress the stream.\n",
		"Camera0 ready to trigger.\n"]


def test_the_summary_for_other_cameras():
	# A free-running emulated camera on the CPU; a RatCam, whose own clock sets its rate
	p = dict(cameraName="Camera0", cameraMake="emu", cameraModel="Emulated_Camera", cameraSerialNo=0,
		frameWidth=64, frameHeight=48, frameRate=20, cameraTrigger="None", videoFolder="/v", videoFilename="0.mp4",
		gpuID=-1, codec="h264")
	startup.Begin(p)
	startup.Setting("clip", "playing clip.mp4")
	s = startup.Summary(startup._current, p)
	assert s == "Camera0 ready to trigger: emu Emulated_Camera serial 0, 64x48 at 20 fps, no trigger (free-running), " \
		"playing clip.mp4, h264 on CPU -> {}".format(os.path.join("/v", "Camera0", "0.mp4"))
	r = dict(p, cameraName="Camera2", cameraMake="ratcam", cameraModel="rp2040 hm01b0", cameraSerialNo="COM5",
		frameWidth=320, frameHeight=320)
	startup.Begin(r)
	startup.Setting("rate", "20.00 fps (frame length 400)")
	startup.Setting("exposure", "exposure 5000 us")
	s = startup.Summary(startup._current, r)
	assert s == "Camera2 ready to trigger: ratcam rp2040 hm01b0 serial COM5, 320x320 at 20.00 fps (frame length 400), " \
		"exposure 5000 us, h264 on CPU -> {}".format(os.path.join("/v", "Camera2", "0.mp4"))
