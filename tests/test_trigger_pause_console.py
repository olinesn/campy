"""
A trigger that pauses on purpose (alertTriggerPauseSec), on the console: a gap
every camera shares is not said per camera (the recording watch says it once),
Spinnaker's timeout in plain words, and the frame rate without the pauses.
"""

import sys, time, types

from campy import status
from campy.cameras import unicam
from campy.utils import utils


class Stdout:
	def __init__(self):
		self.writes = []

	def write(self, text):
		self.writes.append(text)
		return len(text)

	def flush(self):
		pass


def rows(*cameras):
	# The shared counters of these cameras: (state, grabbed, last frame's ns) each
	a = [0] * (len(cameras) * status.WIDTH)
	for n, (state, grabbed, ns) in enumerate(cameras):
		o = n * status.WIDTH
		a[o + status.STATE], a[o + status.GRABBED], a[o + status.FRAME_NS] = state, grabbed, ns
	return a


def test_quiet_together_reads_every_cameras_last_frame():
	G, s = status.GRABBING, int(1e9)
	together = rows((G, 600, 100 * s), (G, 600, 100 * s + s // 100), (G, 600, 100 * s + s // 60))
	assert status.Camera(together, 0).QuietTogether() and status.Camera(together, 2).QuietTogether()
	# One camera still getting frames: not together, for the quiet one or for it
	apart = rows((G, 600, 100 * s), (G, 900, 105 * s), (G, 600, 100 * s))
	assert not status.Camera(apart, 0).QuietTogether()
	# A camera that ended, or never grabbed, is not counted; one camera alone is never "together"
	ended = rows((G, 600, 100 * s), (status.ENDED, 900, 105 * s), (status.WAITING, 0, 0))
	assert not status.Camera(ended, 0).QuietTogether()
	assert not status.Camera(rows((G, 600, 100 * s)), 0).QuietTogether()
	assert not status.Camera().QuietTogether() # outside campy-acquire


def watch(monkeypatch, quiet, **cam):
	params = dict(cameraName="Camera1", frameRate=60, cameraMake="flir", cameraTrigger="Line3",
		alertTriggerPauseSec=30)
	params.update(cam)
	w = unicam.FrameWatch(params, camera=types.SimpleNamespace(QuietTogether=lambda: quiet))
	clock = [1000.0]
	monkeypatch.setattr(time, "monotonic", lambda: clock[0])
	return w, clock


TIMEOUT = RuntimeError("Spinnaker: Failed waiting for EventData on NEW_BUFFER_DATA event. (GenTL error code: -1011) "
	"[-1011]")


def test_a_pause_every_camera_shares_is_not_said_per_camera(monkeypatch):
	out = Stdout()
	monkeypatch.setattr(sys, "stdout", out)
	w, clock = watch(monkeypatch, quiet=True)
	w.Frame(14400)
	for t in range(1, 300):
		clock[0] = 1000.0 + t
		w.NoFrame(TIMEOUT)
	clock[0] = 1300.0
	w.Frame(14401)
	assert out.writes == []


def test_a_camera_that_stops_alone_is_said(monkeypatch):
	out = Stdout()
	monkeypatch.setattr(sys, "stdout", out)
	w, clock = watch(monkeypatch, quiet=False)
	w.Frame(14400)
	clock[0] = 1003.5
	w.NoFrame(TIMEOUT)
	clock[0] = 1010.0
	w.Frame(14401)
	assert out.writes == ["Camera1: no frame for 4 s, since frame 14400. Last error: timed out waiting for a frame (no "
		"trigger pulse?) [Spinnaker -1011]\n", "Camera1: frames again, after 10.0 s without (frame 14401).\n"]


def test_a_camera_left_without_frames_once_the_others_have_them_is_said(monkeypatch):
	out = Stdout()
	monkeypatch.setattr(sys, "stdout", out)
	quiet = [True]
	w = unicam.FrameWatch(dict(cameraName="Camera1", frameRate=60, cameraMake="flir", cameraTrigger="Line3",
		alertTriggerPauseSec=30), camera=types.SimpleNamespace(QuietTogether=lambda: quiet[0]))
	clock = [1000.0]
	monkeypatch.setattr(time, "monotonic", lambda: clock[0])
	w.Frame(14400)
	clock[0] = 1010.0
	w.NoFrame(TIMEOUT)
	assert out.writes == []
	quiet[0] = False # the other cameras have frames again; this one does not
	clock[0] = 1061.0
	w.NoFrame(TIMEOUT)
	assert out.writes == ["Camera1: no frame for 61 s, since frame 14400. Last error: timed out waiting for a frame "
		"(no trigger pulse?) [Spinnaker -1011]\n"]


def test_without_planned_pauses_every_gap_is_said_as_before(monkeypatch):
	out = Stdout()
	monkeypatch.setattr(sys, "stdout", out)
	for cam in (dict(alertTriggerPauseSec="None"), dict(cameraTrigger="None")): # not set; not triggered
		out.writes.clear()
		w, clock = watch(monkeypatch, quiet=True, **cam)
		w.Frame(10)
		clock[0] = 1004.0
		w.NoFrame(ValueError("x"))
		assert out.writes == ["Camera1: no frame for 4 s, since frame 10. Last error: x\n"]


def test_spinnakers_timeout_in_plain_words():
	assert utils.ErrorText(TIMEOUT) == "timed out waiting for a frame (no trigger pulse?) [Spinnaker -1011]"
	other = RuntimeError("Spinnaker: Failed to read node. [-1011]")
	assert utils.ErrorText(other) == "timed out [Spinnaker -1011]: Spinnaker: Failed to read node. [-1011]"
	assert utils.ErrorText(RuntimeError("Spinnaker: error [-10110]")) == "Spinnaker: error [-10110]"
	assert utils.ErrorText(ValueError()) == "ValueError"


def grab(monkeypatch, planned, stamps, chunk=600):
	out = Stdout()
	monkeypatch.setattr(sys, "stdout", out)
	g = {"cameraName": "Camera1", "chunkLengthInFrames": chunk, "frameTimes": types.SimpleNamespace(first=stamps[0]),
		"planned": planned, "pausedSec": 0.0, "pauses": 0, "lastStamp": None, "framePeriod": 1 / 60.0}
	for n, t in enumerate(stamps, 1):
		unicam.CountFPS(g, n, t)
	return out.writes, g


def bouts(count, frames=600, fps=60.0, gap=290.0):
	# 10 s of frames at 60 fps, then a 290 s pause, count times
	stamps, t = [], 0.0
	for _ in range(count):
		stamps += [t + i / fps for i in range(frames)]
		t = stamps[-1] + gap
	return stamps


def test_the_rate_leaves_out_the_trigger_pauses(monkeypatch):
	said, g = grab(monkeypatch, True, bouts(3))
	assert g["pauses"] == 2 and round(g["pausedSec"]) == 580
	assert said[-1] == "Camera1 collected 1800 frames at 60.0 fps for 30 sec of frames (610 sec in all, with 2 " \
		"trigger pauses of 580 sec).\n"
	# Without alertTriggerPauseSec, as before: over the whole time
	said, _ = grab(monkeypatch, False, bouts(3))
	assert said[-1] == "Camera1 collected 1800 frames at 2.9 fps for 610 sec.\n"
	# No pause yet: as before
	said, _ = grab(monkeypatch, True, bouts(1))
	assert said[-1] == "Camera1 collected 600 frames at 60.0 fps for 10 sec.\n"
