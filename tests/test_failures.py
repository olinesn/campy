"""
A camera process, its ffmpeg, or its camera that fails mid-recording: campy
ends, closes what it can, and says what happened.
"""

import csv
import errno
import importlib
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import types
from collections import deque

import numpy as np
import pytest
import imageio_ffmpeg
import multiprocessing as mp

from campy import configurator, frametimes, writer
from campy.cameras import unicam
from campy.utils import utils

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class NoFrame(Exception):
	pass


class FakeCamera:
	"""
	A camera module for unicam.GrabFrames, sending 16x16 frames at frameRate,
	with the failure to try:
	  start: what StartGrabbing returns
	  stall: (frame, seconds): after that many frames, none for that long
	         (each call times out, as FLIR's GetNextImage does), then frames again
	  lost_after: after that many frames, the camera is gone (unicam.CameraLost)
	  close_error: what CloseCamera raises
	  bad_frames: grabs whose image cannot be read, or shown (counted from 1)
	  on_frame: called with each grab's number
	"""

	def __init__(self, frameRate=100, start=True, stall=None, lost_after=None, close_error=None, bad_frames=(),
			on_frame=None):
		self.period = 1.0 / frameRate
		self.start, self.stall, self.lost_after = start, stall, lost_after
		self.close_error, self.bad_frames, self.on_frame = close_error, set(bad_frames), on_frame
		self.stalled = None # when the stall began
		self.grabbed, self.released = [], []
		self.closed = False

	def OpenCamera(self, cam_params):
		cam_params["cameraModel"] = "fake"
		cam_params["cameraSerialNo"] = "0"
		return object(), cam_params

	def StartGrabbing(self, camera):
		return self.start

	def GrabFrame(self, camera, n):
		if self.lost_after is not None and n >= self.lost_after:
			raise unicam.CameraLost("device removed")
		if self.stall is not None and n == self.stall[0]:
			if self.stalled is None:
				self.stalled = time.monotonic()
			if time.monotonic() - self.stalled < self.stall[1]:
				time.sleep(0.02)
				raise NoFrame("no frame within 20 ms")
		time.sleep(self.period)
		grab = len(self.grabbed) + 1 # each grab its own number, whatever campy made of the one before
		self.grabbed.append(grab)
		if self.on_frame is not None:
			self.on_frame(grab)
		return grab

	def GetImageArray(self, grab):
		if grab in self.bad_frames:
			raise ValueError("frame {} cannot be read".format(grab))
		return np.full((16, 16), grab % 256, dtype=np.uint8)

	def GetTimeStamp(self, grab):
		return grab * self.period

	def DisplayImage(self, cam_params, dispQueue, grab):
		if grab in self.bad_frames:
			raise ValueError("frame {} cannot be shown".format(grab))

	def ReleaseFrame(self, grab):
		self.released.append(grab)

	def CloseCamera(self, cam_params, camera):
		self.closed = True
		if self.close_error is not None:
			raise self.close_error


def params(tmp_path, **values):
	p = configurator.DefaultParams()
	p.update(cameraMake="fake", cameraName="Camera0", n_cam=0, videoFolder=str(tmp_path), videoFilename="0.mp4",
		frameRate=100, recTimeInSec=0.5, chunkLengthInSec=10, displayFrameRate=10, frameWidth=16, frameHeight=16,
		pixelFormatInput="gray", pixelFormatOutput="yuv420p", gpuID=-1, ffmpegLogLevel="quiet")
	p.update(values)
	return p


class NoSignal:
	# Stands in for QueueKeyboardInterrupt: it sets a SIGINT handler, which
	# only the main thread may, and here the writer runs in a thread of its own
	def __init__(self, queue):
		pass

	def __enter__(self):
		pass

	def __exit__(self, *exc):
		pass


class CameraProcess:
	"""
	One camera process's two threads, as campy.AcquireOneCamera runs them:
	the camera thread (GrabFrames) and the writer (WriteFrames), sharing a
	unicam.Recording. Both in threads here, so that one that hangs fails the
	test rather than hanging it.
	"""

	def __init__(self, monkeypatch, p, camera):
		monkeypatch.setattr(unicam, "ImportCam", lambda make: camera)
		monkeypatch.setattr(writer, "QueueKeyboardInterrupt", NoSignal)
		sleep = time.sleep
		monkeypatch.setattr(time, "sleep", lambda s: None if s >= 1 else sleep(s)) # the writer's 1 s wait to close
		self.p, self.camera = p, camera
		self.recording = unicam.Recording()
		self.writeQueue, self.dispQueue = deque(), deque([], 2)
		self.stopReadQueue, self.stopWriteQueue, self.openQueue = deque([], 1), deque([], 1), deque([], 1)

	def Run(self, timeout=20, stop_after=None):
		t0 = time.monotonic()
		grab = threading.Thread(target=unicam.GrabFrames, daemon=True, args=(self.p, self.writeQueue,
			self.dispQueue, self.stopReadQueue, self.stopWriteQueue, self.openQueue, self.recording))
		write = threading.Thread(target=writer.WriteFrames, daemon=True, args=(self.p, self.writeQueue,
			self.stopReadQueue, self.stopWriteQueue, self.openQueue), kwargs=dict(recording=self.recording))
		grab.start()
		write.start()
		if stop_after is not None: # Ctrl-C
			time.sleep(stop_after)
			self.stopReadQueue.append("STOP")
		grab.join(timeout)
		write.join(max(0.1, timeout - (time.monotonic() - t0)))
		assert not grab.is_alive(), "the camera thread did not end"
		assert not write.is_alive(), "the writer did not end"
		return time.monotonic() - t0

	def Folder(self):
		return os.path.join(self.p["videoFolder"], self.p["cameraName"])

	def Metadata(self):
		with open(os.path.join(self.Folder(), "metadata.csv")) as f:
			return dict(csv.reader(f))

	def FrameTimes(self):
		return np.load(os.path.join(self.Folder(), "frametimes.npy"))

	def VideoFrames(self):
		return imageio_ffmpeg.count_frames_and_secs(os.path.join(self.Folder(), "0.mp4"))[0]


# ------------------------------------------------------------------ the camera fails to close

def test_a_camera_that_fails_to_close_still_finishes_its_video_and_metadata(tmp_path, monkeypatch, capsys):
	# FLIR's CloseCamera raised a SpinnakerException (EndAcquisition on an
	# unplugged camera, or one that never started). GrabFrames ended without
	# telling the writer, which then waited forever: no video, no totals.
	camera = FakeCamera(close_error=RuntimeError("Spinnaker: Camera is not started. [-1002]"))
	run = CameraProcess(monkeypatch, params(tmp_path), camera)
	run.Run()
	assert camera.closed
	out = capsys.readouterr().out
	assert "Camera0: could not close the camera: Spinnaker: Camera is not started. [-1002]\n" in out
	assert "Saved metadata for Camera0." in out
	meta = run.Metadata()
	assert meta["totalFrames"] == meta["framesWritten"] == "50"
	assert "cameraError" not in meta and "videoError" not in meta # the recording itself is whole
	assert os.path.exists(os.path.join(run.Folder(), "frametimes.mat"))
	assert run.VideoFrames() == 50
	assert list(run.dispQueue)[-1] == "STOP" # and the display was told
	assert run.recording.Failures() == []


def test_each_frame_is_released_whatever_happens_to_it(tmp_path, monkeypatch):
	# A frame that could not be read or shown was never released: a FLIR
	# camera holds its buffer, and stops once they are all held
	camera = FakeCamera(bad_frames={5, 20})
	run = CameraProcess(monkeypatch, params(tmp_path), camera)
	run.Run()
	assert camera.released == camera.grabbed and len(camera.grabbed) >= 50


# ------------------------------------------------------------------ the camera does not send

def test_a_camera_that_did_not_start_grabbing_ends_at_once(tmp_path, monkeypatch, capsys):
	# It used to loop for ever, retrying a camera that was never started
	camera = FakeCamera(start=False)
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=60), camera)
	assert run.Run() < 10
	assert camera.grabbed == [] and camera.closed
	assert "Camera0 did not start grabbing, so it records nothing.\n" in capsys.readouterr().out
	assert run.recording.Failures() == ["did not start grabbing"]
	meta = run.Metadata() # says why there is nothing
	assert meta["cameraError"] == "did not start grabbing" and meta["totalFrames"] == "0"


def test_an_error_anywhere_in_the_camera_thread_still_closes_it(tmp_path, monkeypatch, capsys):
	camera = FakeCamera()
	def start(cam):
		raise OSError("USB transfer failed")
	camera.StartGrabbing = start
	run = CameraProcess(monkeypatch, params(tmp_path), camera)
	assert run.Run() < 10
	why = "the camera thread stopped, after frame 0: USB transfer failed"
	assert "Camera0: {}.\n".format(why) in capsys.readouterr().out
	assert camera.closed and run.recording.Failures() == [why]
	assert run.Metadata()["cameraError"] == why


def test_a_camera_that_is_gone_ends_its_recording(tmp_path, monkeypatch, capsys):
	camera = FakeCamera(lost_after=20)
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=60), camera)
	assert run.Run() < 10
	why = "the camera is gone, after frame 20: device removed"
	assert "Camera0: {}. Its recording ends there.\n".format(why) in capsys.readouterr().out
	assert run.recording.Failures() == [why] and camera.closed
	meta = run.Metadata()
	assert meta["cameraError"] == why
	assert meta["totalFrames"] == meta["framesWritten"] == "20"
	assert run.FrameTimes().shape == (2, 20) and run.VideoFrames() == 20


@pytest.fixture
def quick_watch(monkeypatch):
	# Say a gap after 0.2 s, and again every 0.5 s
	monkeypatch.setattr(unicam, "NO_FRAME_SEC", 0.2)
	monkeypatch.setattr(unicam, "NO_FRAME_REPORT_SEC", 0.5)


def test_a_camera_that_stops_sending_is_said_now_and_then_and_when_it_is_back(tmp_path, monkeypatch, capsys,
		quick_watch):
	# It used to retry every millisecond, and said nothing without cameraDebug
	camera = FakeCamera(stall=(10, 1.3))
	run = CameraProcess(monkeypatch, params(tmp_path), camera)
	run.Run()
	out = capsys.readouterr().out
	gaps = re.findall(r"Camera0: no frame for \d+ s, since frame 10\. Last error: no frame within 20 ms\n", out)
	assert 2 <= len(gaps) <= 4 # at 0.2, 0.7 and 1.2 s: not once a retry
	assert re.search(r"Camera0: frames again, after 1\.\d s without \(frame 11\)\.\n", out)
	assert run.Metadata()["totalFrames"] == "50" and run.recording.Failures() == []


def test_nothing_is_said_before_the_first_frame(tmp_path, monkeypatch, capsys, quick_watch):
	# A triggered camera waits for its first trigger, for as long as that takes
	camera = FakeCamera(stall=(0, 1.0))
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=0.2), camera)
	run.Run()
	out = capsys.readouterr().out
	assert "no frame for" not in out and "frames again" not in out
	assert run.Metadata()["totalFrames"] == "20"


def test_a_camera_that_stops_for_good_still_stops_at_ctrl_c(tmp_path, monkeypatch, capsys, quick_watch):
	# Gone, but not known to be (a FLIR camera that stays valid): said, and
	# Ctrl-C stops it cleanly, with what it recorded
	camera = FakeCamera(stall=(10, 600))
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=60), camera)
	run.Run(stop_after=0.9)
	assert "Camera0: no frame for" in capsys.readouterr().out
	meta = run.Metadata()
	assert meta["totalFrames"] == meta["framesWritten"] == "10" and run.VideoFrames() == 10


# ------------------------------------------------------------------ ffmpeg fails

def test_a_killed_ffmpeg_stops_its_camera_and_is_said(tmp_path, monkeypatch, capsys):
	# The camera used to go on recording into nothing to the end, with
	# metadata.csv saying every frame was written
	found = []
	real = writer.FfmpegProcess
	monkeypatch.setattr(writer, "FfmpegProcess", lambda w: found.append(real(w)) or found[-1])

	def kill(n):
		if n == 250: # 2.5 s: its first fragments (a second each) are in the file
			while not found:
				time.sleep(0.01)
			found[0].kill()

	camera = FakeCamera(on_frame=kill)
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=60), camera)
	assert run.Run() < 15
	out = capsys.readouterr().out
	said = re.search(r"Camera0: ffmpeg stopped after taking (\d+) frames, with exit code (\S+)[^\n]*: (.*)\. "
		r"Stopping Camera0\. Its video can be read up to its last fragment, and its metadata\.csv and frametimes\.npy "
		r"count only "
		r"the frames in it\. ffmpegLogLevel is \"quiet\", so ffmpeg did not say why: \"warning\" shows "
		r"its errors\.\n", out)
	assert said, out
	sent = int(said.group(1))
	if sys.platform != "win32":
		assert said.group(2) == "-9" and "(SIGKILL)" in out
	assert run.recording.framesSent == sent and camera.closed
	meta = run.Metadata()
	assert meta["videoError"] == run.recording.videoError and meta["videoError"].startswith(
		"ffmpeg stopped after taking {} frames".format(sent))
	# Only the frames its video holds, counted from its file: one row each in
	# frametimes.npy. The frames ffmpeg was given but had not written yet (it
	# writes a fragment a second) are left out, and named.
	held = run.VideoFrames()
	assert 100 <= held < sent
	assert meta["framesWritten"] == str(held) and run.FrameTimes().shape == (2, held)
	assert list(run.FrameTimes()[0]) == list(range(1, held + 1))
	assert meta["framesLostInEncoder"] == str(sent + 1 - held) # and the frame it failed on
	assert "Camera0: 0.mp4 holds {} of the {} frames given to its ffmpeg. Lost with that ffmpeg: {} frames ({}-{})." \
		"\n".format(held, sent + 1, sent + 1 - held, held + 1, sent + 1) in out
	assert sent <= int(meta["totalFrames"]) < 400 # it stopped, rather than grab 6000
	assert "Camera0 grabbed {} frames at".format(meta["totalFrames"]) in out
	assert "Its video holds {} of them: its ffmpeg stopped.".format(held) in out
	assert not os.path.exists(os.path.join(run.Folder(), "segments.csv")) # one file: no table


def Uncountable(path, frame_rate, given, **kwargs):
	raise writer.segments.Uncounted("a stand-in ffmpeg")


def fake_write_frames(fail_at=None, sent=None):
	# Stands in for imageio-ffmpeg's write_frames: an ffmpeg that takes
	# fail_at frames, then breaks its pipe
	def write_frames(path, size, **kwargs):
		count = 0
		while True:
			frame = yield
			if frame is None:
				continue
			if fail_at is not None and count >= fail_at:
				raise IOError("[Errno 32] Broken pipe\n\nFFMPEG COMMAND:\nffmpeg ...")
			count += 1
			if sent is not None:
				sent.append(count)
			time.sleep(0.01)
	return write_frames


def test_ffmpeg_failing_after_the_camera_has_stopped(tmp_path, monkeypatch, capsys):
	# The camera has grabbed every frame and closed, and the writer, behind,
	# is giving ffmpeg the rest when it fails: metadata.csv, saved once the
	# writer is done, counts only the frames it took
	monkeypatch.setattr(writer, "write_frames", fake_write_frames(fail_at=30))
	# The stand-in writes no file to count: taken to hold the frames it took, as campy took it before it counted
	monkeypatch.setattr(writer.segments, "CountFrames", Uncountable)
	camera = FakeCamera(frameRate=10000) # far faster than the writer
	run = CameraProcess(monkeypatch, params(tmp_path, frameRate=10000, recTimeInSec=0.005, writeQueueMaxSec="None"),
		camera)
	run.Run()
	out = capsys.readouterr().out
	assert "Camera0: ffmpeg stopped after taking 30 frames: [Errno 32] Broken pipe. Its video can be read up to " \
		"its last fragment" in out
	assert "Stopping Camera0" not in out # it already had
	meta = run.Metadata()
	assert meta["totalFrames"] == "50" and meta["framesWritten"] == "30" and meta["framesLostInEncoder"] == "1"
	assert meta["videoError"] == "ffmpeg stopped after taking 30 frames: [Errno 32] Broken pipe"
	assert "Camera0: could not count the frames in 0.mp4 (a stand-in ffmpeg); taken to hold the 30 its ffmpeg took. " \
		"Lost with that ffmpeg: 1 frame (31).\n" in out
	frames = run.FrameTimes()
	assert list(frames[0]) == list(range(1, 31)) and len(np.load(os.path.join(run.Folder(), "hosttimes.npy"))) == 30


def test_ffmpeg_that_fails_as_it_finishes_the_video(tmp_path, monkeypatch, capsys):
	monkeypatch.setattr(writer, "write_frames", fake_write_frames())
	monkeypatch.setattr(writer, "FfmpegProcess", lambda w: types.SimpleNamespace(returncode=1, pid=1234))
	run = CameraProcess(monkeypatch, params(tmp_path), FakeCamera())
	run.Run()
	why = "ffmpeg ended with exit code 1 as it finished the video"
	assert "Camera0: {}. Its video can be read up to its last fragment".format(why) in capsys.readouterr().out
	meta = run.Metadata()
	assert meta["videoError"] == why and meta["framesWritten"] == "50"


def test_ffmpeg_that_does_not_start(tmp_path, monkeypatch, capsys):
	def write_frames(path, size, **kwargs):
		raise FileNotFoundError("no ffmpeg here")
	monkeypatch.setattr(writer, "write_frames", write_frames)
	camera = FakeCamera()
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=60), camera)
	assert run.Run() < 10
	assert "Camera0: ffmpeg did not start: no ffmpeg here. Stopping Camera0." in capsys.readouterr().out
	assert camera.closed
	meta = run.Metadata()
	assert meta["videoError"] == "ffmpeg did not start: no ffmpeg here" and meta["framesWritten"] == "0"


def test_the_ffmpeg_process_is_found():
	# Its exit code is read from imageio-ffmpeg's own Popen
	w = imageio_ffmpeg.write_frames(os.devnull, (16, 16), pix_fmt_in="gray", output_params=["-f", "null"])
	w.send(None)
	process = writer.FfmpegProcess(w)
	assert process is not None and process.poll() is None
	w.send(np.zeros((16, 16), np.uint8))
	w.close()
	assert process.returncode == 0
	assert writer.FfmpegProcess(fake_write_frames()("x", (16, 16))) is None


# ------------------------------------------------------------------ an encoder that does not open

def opening_write_frames(fail_opens, received, write_first=False):
	# Stands in for imageio-ffmpeg's write_frames: the first fail_opens
	# ffmpegs started take 2 frames and break their pipe, writing nothing
	# (an encoder that would not open), or, with write_first, having written
	# their first frame; the next ones write each frame into the video. The
	# frames that reach a video: received (each frame's value, its grab's
	# number).
	opens = []

	def write_frames(path, size, **kwargs):
		opens.append(path)
		failing = len(opens) <= fail_opens
		count = 0
		while True:
			frame = yield
			if frame is None:
				continue
			if failing and count >= 2:
				raise IOError("[Errno 32] Broken pipe\n\nFFMPEG COMMAND:\nffmpeg ...")
			count += 1
			if not failing or (write_first and count == 1):
				received.append(int(frame[0, 0]))
				with open(path, "ab") as f:
					f.write(b"x")
			time.sleep(0.001)

	write_frames.opens = opens
	return write_frames


def test_an_encoder_that_does_not_open_at_first_is_started_again_with_no_frame_lost(tmp_path, monkeypatch, capsys):
	# Quick Sync on Windows refused to open for every camera as Remote Desktop
	# moved campy's session to another computer: ffmpeg took 2 frames, wrote
	# nothing and stopped, and the recording ended 7 s in
	received = []
	fake = opening_write_frames(2, received)
	monkeypatch.setattr(writer, "write_frames", fake)
	camera = FakeCamera()
	run = CameraProcess(monkeypatch, params(tmp_path), camera)
	run.Run()
	out = capsys.readouterr().out
	assert "Camera0: ffmpeg stopped after taking 2 frames: [Errno 32] Broken pipe, and wrote nothing: its encoder " \
		"did not open. Starting ffmpeg again in 1 s (retry 1 of 3), with the same 2 frames; the camera goes on." in out
	assert "(retry 2 of 3)" in out and "Camera0: ffmpeg started again (retry 2); no frame lost." in out
	assert len(fake.opens) == 3 and len(set(fake.opens)) == 1 # the same file each time
	# Every frame grabbed is in the video, in order, once; nothing failed
	assert received == list(range(1, 51))
	assert run.recording.videoError is None and run.recording.framesSent == 50
	meta = run.Metadata()
	assert "videoError" not in meta and meta["ffmpegRetries"] == "2"
	assert meta["totalFrames"] == "50" and meta["framesWritten"] == "50"
	assert list(run.FrameTimes()[0]) == list(range(1, 51))


def test_an_encoder_that_never_opens_stops_the_camera_after_its_retries(tmp_path, monkeypatch, capsys):
	received = []
	fake = opening_write_frames(100, received)
	monkeypatch.setattr(writer, "write_frames", fake)
	camera = FakeCamera()
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=60), camera)
	assert run.Run() < 15
	out = capsys.readouterr().out
	assert len(fake.opens) == 1 + writer.OPEN_RETRIES and received == []
	why = run.recording.videoError
	assert why.startswith("libx264 did not open in 4 tries, 1 s apart (ffmpeg wrote nothing): ffmpeg stopped ") \
		and why.endswith(": [Errno 32] Broken pipe"), why
	assert "Camera0: {}. Stopping Camera0.".format(why) in out
	assert camera.closed and int(run.Metadata()["totalFrames"]) < 200
	assert run.Metadata()["ffmpegRetries"] == "3"


def test_an_ffmpeg_that_wrote_something_is_not_started_again(tmp_path, monkeypatch, capsys):
	# Its video holds frames: a new ffmpeg would replace them
	received = []
	fake = opening_write_frames(1, received, write_first=True)
	monkeypatch.setattr(writer, "write_frames", fake)
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=60), FakeCamera())
	run.Run()
	out = capsys.readouterr().out
	assert len(fake.opens) == 1 and "started again" not in out and "Starting ffmpeg again" not in out
	assert run.recording.videoError == "ffmpeg stopped after taking 2 frames: [Errno 32] Broken pipe"
	assert "ffmpegRetries" not in run.Metadata()


def test_frames_are_held_only_until_the_video_holds_something(tmp_path):
	p = params(tmp_path)
	os.makedirs(os.path.dirname(writer.newfiles.VideoPath(p)), exist_ok=True)
	first = writer.FirstFrames(p, writer.newfiles.VideoPath(p), unicam.Recording())
	frame = np.zeros((16, 16), np.uint8)
	first.Sent(frame)
	frame[:] = 7 # a held frame is a copy: the camera's buffer may be reused
	assert len(first.frames) == 1 and first.frames[0].max() == 0 and first.Empty()
	with open(first.path, "wb") as f:
		f.write(b"x")
	first.Sent(frame)
	assert first.frames is None and not first.Empty()
	# and for OPEN_HOLD_FRAMES frames at most
	q = params(tmp_path, cameraName="Camera1")
	other = writer.FirstFrames(q, writer.newfiles.VideoPath(q), unicam.Recording())
	for _ in range(writer.OPEN_HOLD_FRAMES):
		other.Sent(frame)
	assert len(other.frames) == writer.OPEN_HOLD_FRAMES
	other.Sent(frame)
	assert other.frames is None


# ------------------------------------------------------------------ the camera modules

class SpinError(Exception):
	pass


class Image:
	def __init__(self, incomplete=False):
		self.incomplete, self.released = incomplete, False

	def IsIncomplete(self):
		return self.incomplete

	def GetImageStatus(self):
		return 3

	def Release(self):
		self.released = True


class SpinCamera:
	"""A FLIR camera, as PySpin's CameraPtr: what it sends, whether it is still valid, what fails as it closes."""
	def __init__(self, images=(), error=None, valid=True, end_error=None):
		self.images, self.error, self.valid, self.end_error = list(images), error, valid, end_error
		self.calls = []

	def GetNextImage(self, timeout):
		if self.error is not None:
			raise self.error
		return self.images.pop(0)

	def IsValid(self):
		if isinstance(self.valid, Exception):
			raise self.valid
		return self.valid

	def EndAcquisition(self):
		self.calls.append("EndAcquisition")
		if self.end_error is not None:
			raise self.end_error

	def DeInit(self):
		self.calls.append("DeInit")


@pytest.fixture
def flir(monkeypatch, tmp_path):
	spin = types.ModuleType("PySpin")
	spin.SpinnakerException = SpinError
	monkeypatch.setitem(sys.modules, "PySpin", spin)
	monkeypatch.delitem(sys.modules, "campy.cameras.flir", raising=False)
	module = importlib.import_module("campy.cameras.flir")
	monkeypatch.setattr(module, "NOTE_FOLDER", str(tmp_path / ".campy"))
	yield module
	sys.modules.pop("campy.cameras.flir", None)


TIMEOUT = SpinError("Spinnaker: Failed waiting for EventData on NEW_BUFFER_DATA event. [-1011]")


def test_flir_releases_an_incomplete_image(flir, capsys):
	# It was raised without being released: the camera's buffer stayed held
	image = Image(incomplete=True)
	with pytest.raises(flir.ImageNotCompleteException):
		flir.GrabFrame(SpinCamera([image]), 0)
	assert image.released
	assert flir.GrabFrame(SpinCamera([Image()]), 0).released is False # a whole one is the caller's to release


def test_flir_timeouts_are_not_a_camera_gone(flir):
	# A triggered camera waits for its first trigger with GetNextImage timing out
	with pytest.raises(SpinError):
		flir.GrabFrame(SpinCamera(error=TIMEOUT), 0)
	# and so with any error, while Spinnaker says the camera is valid, or cannot say
	with pytest.raises(SpinError):
		flir.GrabFrame(SpinCamera(error=SpinError("Spinnaker: Stream has been aborted. [-1012]")), 0)
	with pytest.raises(SpinError):
		flir.GrabFrame(SpinCamera(error=TIMEOUT, valid=SpinError("Camera is not valid")), 0)


def test_flir_a_camera_no_longer_valid_is_gone(flir):
	with pytest.raises(unicam.CameraLost, match=r"Spinnaker says the camera is no longer valid \(unplugged\?\): "
			r"Spinnaker: Stream has been aborted\. \[-1012\]"):
		flir.GrabFrame(SpinCamera(error=SpinError("Spinnaker: Stream has been aborted. [-1012]"), valid=False), 0)


def test_flir_close_goes_on_when_a_step_fails(flir, monkeypatch, capsys):
	# EndAcquisition on an unplugged camera, or one that never started: it was
	# re-raised, and the camera was neither put back nor deinitialized
	restored = []
	monkeypatch.setattr(flir, "RestoreFrameRate", lambda camera, cam_params: restored.append(camera))
	camera = SpinCamera(end_error=SpinError("Spinnaker: Camera is not started. [-1002]"))
	flir.CloseCamera({"cameraName": "Camera0"}, camera)
	assert camera.calls == ["EndAcquisition", "DeInit"] and restored == [camera]
	assert capsys.readouterr().out == "Closing Camera0... Please wait.\n" \
		"Camera0: EndAcquisition failed as it closed: Spinnaker: Camera is not started. [-1002]\n"


class Basler:
	def __init__(self, removed=False, stop_error=None):
		self.removed, self.stop_error, self.calls = removed, stop_error, []

	def RetrieveResult(self, timeout, handling):
		raise RuntimeError("Grab timed out")

	def IsCameraDeviceRemoved(self):
		return self.removed

	def StopGrabbing(self):
		self.calls.append("StopGrabbing")
		if self.stop_error is not None:
			raise self.stop_error

	def Close(self):
		self.calls.append("Close")


@pytest.fixture
def basler(monkeypatch):
	pylon = types.ModuleType("pypylon.pylon")
	pylon.TimeoutHandling_ThrowException = "ThrowException"
	genicam = types.ModuleType("pypylon.genicam")
	package = types.ModuleType("pypylon")
	package.pylon, package.genicam = pylon, genicam
	for name, module in {"pypylon": package, "pypylon.pylon": pylon, "pypylon.genicam": genicam}.items():
		monkeypatch.setitem(sys.modules, name, module)
	monkeypatch.delitem(sys.modules, "campy.cameras.basler", raising=False)
	yield importlib.import_module("campy.cameras.basler")
	sys.modules.pop("campy.cameras.basler", None)


def test_basler_a_removed_camera_is_gone(basler):
	with pytest.raises(RuntimeError, match="Grab timed out"):
		basler.GrabFrame(Basler(), 0)
	with pytest.raises(unicam.CameraLost, match="pylon says the camera was removed: Grab timed out"):
		basler.GrabFrame(Basler(removed=True), 0)


def test_basler_close_goes_on_when_a_step_fails(basler, capsys):
	camera = Basler(stop_error=RuntimeError("Device has been removed"))
	basler.CloseCamera({"cameraName": "Camera0"}, camera)
	assert camera.calls == ["StopGrabbing", "Close"]
	assert "Camera0: StopGrabbing failed as it closed: Device has been removed\n" in capsys.readouterr().out


def test_emu_close_says_what_failed(capsys):
	from campy.cameras import emu
	def fail():
		raise OSError("the clip is gone")
	emu.CloseCamera({"cameraName": "Camera0"}, types.SimpleNamespace(close=fail))
	assert "Camera0: the clip did not close: the clip is gone\n" in capsys.readouterr().out


def ratcam_params(tmp_path, **values):
	p = configurator.DefaultParams()
	p.update(numCams=1, cameraMake="ratcam", ratcamPorts=["sim"], videoFolder=str(tmp_path),
		frameWidth=160, frameHeight=120, frameRate=20, cameraExposureTimeInUs=2000,
		recTimeInSec=1, chunkLengthInSec=1, displayFrameRate=0)
	p.update(values)
	p = configurator.FillCameraLists(p)
	systems = unicam.GetDeviceList(unicam.LoadSystems(p), p)
	return configurator.ConfigureCamParams(systems, p, 0)


def test_a_ratcam_that_loses_its_link_ends_its_recording(tmp_path, monkeypatch, capsys):
	# It said "lost the RatCam" once, then retried for ever
	pytest.importorskip("nireyecam")
	from campy.cameras import ratcam
	grab = ratcam.GrabFrame
	def GrabFrame(camera, frameNumber):
		if frameNumber == 10 and camera.cam.error is None:
			camera.cam.error = OSError(6, "Device not configured") # what its reader thread keeps when the USB link fails
		return grab(camera, frameNumber)
	monkeypatch.setattr(ratcam, "GrabFrame", GrabFrame)

	p = ratcam_params(tmp_path, recTimeInSec=60)
	(tmp_path / "Camera0").mkdir()
	t0 = time.monotonic()
	unicam.GrabFrames(p, deque(), deque([], 2), deque([], 1), deque([], 1))
	assert time.monotonic() - t0 < 15
	why = "the camera is gone, after frame 10: lost the RatCam: [Errno 6] Device not configured"
	assert "Camera0: {}. Its recording ends there.\n".format(why) in capsys.readouterr().out
	with open(tmp_path / "Camera0" / "metadata.csv") as f:
		meta = dict(csv.reader(f))
	assert meta["cameraError"] == why and meta["totalFrames"] == meta["framesWritten"] == "10"
	with open(tmp_path / "Camera0" / "ratcam_frames.csv") as f:
		assert len(list(csv.DictReader(f))) == 10 # its logs, closed with it


# ------------------------------------------------------------------ frame times of a video cut short

def test_frame_times_keep_only_the_frames_the_video_took(tmp_path):
	times = frametimes.FrameTimes(str(tmp_path), 10)
	for k in range(1, 36):
		times.Add(k, 100.0 + k / 80, 1000 + k)
	times.Close(keep=23)
	frames, hosts = np.load(tmp_path / "frametimes.npy"), np.load(tmp_path / "hosttimes.npy")
	assert frames.shape == (2, 23) and list(frames[0]) == list(range(1, 24))
	assert np.allclose(frames[1], (np.arange(1, 24) - 1) / 80)
	assert list(hosts) == list(range(1001, 1024))
	assert os.path.getsize(tmp_path / "hosttimes.npy") == frametimes.HEADER_BYTES + 23 * 8


@pytest.mark.parametrize("keep, count", [(None, 35), (35, 35), (99, 35), (0, 0)])
def test_frame_times_keep(tmp_path, keep, count):
	times = frametimes.FrameTimes(str(tmp_path), 10)
	for k in range(1, 36):
		times.Add(k, k / 80, k)
	times.Close(keep=keep)
	assert times.count == count and np.load(tmp_path / "frametimes.npy").shape == (2, count)


# ------------------------------------------------------------------ camera processes (campy's main process)

def camera_process(n_cam, readyQueue, done):
	# What campy.RunCamera does, by camera: 0 records for 3 s; 1 dies 0.3 s
	# after it is ready (kill -9, or a segfault); 2 records, then sends what
	# went wrong; 3 dies before it is ready
	name = "Camera{}".format(n_cam)
	if n_cam == 3:
		os._exit(70)
	readyQueue.put((n_cam, name, True, ""))
	if n_cam == 1:
		time.sleep(0.3)
		if hasattr(signal, "SIGKILL"):
			os.kill(os.getpid(), signal.SIGKILL)
		os._exit(9)
	time.sleep(1.5 if n_cam == 0 else 0.5)
	done.send(("done", [] if n_cam == 0 else ["ffmpeg stopped after taking 30 frames, with exit code -9 (SIGKILL)"]))
	done.close()


def hanging_camera_process(n_cam, readyQueue, done):
	# Camera0 records and closes. Camera1 hangs as it closes, with its ffmpeg
	# running, and ignores SIGTERM: as one stuck in a camera driver would.
	name = "Camera{}".format(n_cam)
	readyQueue.put((n_cam, name, True, ""))
	if n_cam == 0:
		time.sleep(0.3)
		done.send(("ffmpeg", 1111))
		done.send(("ffmpeg", None)) # it ended
		done.send(("done", []))
		return
	done.send(("ffmpeg", 4242))
	if hasattr(signal, "SIGTERM") and sys.platform != "win32":
		signal.signal(signal.SIGTERM, signal.SIG_IGN)
	time.sleep(600)


def camera_params(n, timeout=30):
	p = configurator.DefaultParams()
	p.update(numCams=n, readyTimeoutSec=timeout)
	return configurator.FillCameraLists(p)


def test_a_camera_process_that_dies_is_said_at_once_and_the_others_go_on():
	# campy used to wait for it in the pool's result.get() forever
	ctx = mp.get_context("spawn")
	said = []
	cameras = unicam.CameraProcesses(ctx, camera_process, camera_params(3), ctx.Queue(),
		log=lambda text: said.append((text, cameras.processes[0].is_alive())))
	t0 = time.monotonic()
	failures = cameras.Wait()
	assert time.monotonic() - t0 < 20
	why = "its camera process stopped, with exit code -9 (SIGKILL): killed, or crashed" \
		if hasattr(signal, "SIGKILL") else "its camera process stopped, with exit code 9: killed, or crashed"
	assert said == [("Camera1: {}. Its files end where it stopped.".format(why), True)] # while Camera0 recorded
	assert failures == {
		"Camera1": [why],
		"Camera2": ["ffmpeg stopped after taking 30 frames, with exit code -9 (SIGKILL)"],
		}
	assert [p.exitcode for p in cameras.processes][0] == 0


def test_a_camera_process_that_dies_before_it_is_ready_is_not_waited_for():
	# It used to be waited for until readyTimeoutSec
	ctx = mp.get_context("spawn")
	readyQueue = ctx.Queue()
	p = camera_params(4, timeout=60)
	p["cameraNames"] = ["Camera0", "Camera1", "Camera2", "Camera3"]
	cameras = unicam.CameraProcesses(ctx, camera_process, p, readyQueue, log=lambda text: None)
	t0 = time.monotonic()
	ready, missing = unicam.WaitForCameras(readyQueue, p, cameras, log=lambda text: None)
	assert time.monotonic() - t0 < 20
	assert missing == {"Camera3": "stopped before it was ready"}
	assert sorted(ready) == ["Camera0", "Camera1", "Camera2"]
	failures = cameras.Wait()
	assert failures["Camera3"] == ["its camera process stopped, with exit code 70: killed, or crashed"]


def test_a_second_ctrl_c_stops_a_camera_process_that_hangs():
	# Main ignores SIGINT, and waited for a camera process that hung as it
	# closed for ever: Ctrl-C did nothing more
	ctx = mp.get_context("spawn")
	readyQueue = ctx.Queue()
	said = []
	cameras = unicam.CameraProcesses(ctx, hanging_camera_process, camera_params(2), readyQueue, log=said.append)
	cameras.grace = 0.5
	interrupts = types.SimpleNamespace(count=1) # the first Ctrl-C: Camera0 closes, Camera1 hangs
	def again():
		time.sleep(2.5)
		interrupts.count = 2
	threading.Thread(target=again, daemon=True).start()
	t0 = time.monotonic()
	failures = cameras.Wait(interrupts)
	assert time.monotonic() - t0 < 15
	why = "stopped at the second Ctrl-C, before it had closed"
	assert failures == {"Camera1": [why]}
	kill = "taskkill /F /PID 4242" if sys.platform == "win32" else "kill -9 4242"
	assert said == ["Ctrl-C again: stopping Camera1 now, without closing it.",
		"Camera1: {}. Its files end where it stopped. If its ffmpeg (pid 4242) is still running, it is finishing the "
		"video, or hangs: {} ends it.".format(why, kill)]
	assert not cameras.processes[1].is_alive()
	if sys.platform != "win32":
		assert cameras.processes[1].exitcode == -signal.SIGKILL # it ignored SIGTERM


def test_after_ctrl_c_a_camera_process_that_dies_is_not_called_crashed():
	# Ctrl-C as a camera process starts (before it handles SIGINT) ends it
	ctx = mp.get_context("spawn")
	said = []
	p = camera_params(4)
	cameras = unicam.CameraProcesses(ctx, camera_process, p, ctx.Queue(), log=said.append)
	failures = cameras.Wait(types.SimpleNamespace(count=1))
	assert failures["Camera3"] == ["its camera process stopped, with exit code 70, after Ctrl-C"]


def test_ctrl_c_stops_the_wait_for_cameras():
	q, said = queue.Queue(), []
	q.put((0, "Camera0", True, ""))
	t0 = time.monotonic()
	ready, missing = unicam.WaitForCameras(q, camera_params(2, timeout=30), log=said.append, interrupted=lambda: True)
	assert time.monotonic() - t0 < 2
	assert ready == ["Camera0"] and missing == {"Camera1": "not ready when campy was stopped (Ctrl-C)"}


def test_ctrl_c_presses_are_counted_once_each(monkeypatch, capsys):
	now = [100.0]
	monkeypatch.setattr(utils, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
	interrupts = utils.HandleKeyboardInterrupt()
	interrupts.handler(signal.SIGINT, None)
	now[0] += 0.3
	interrupts.handler(signal.SIGINT, None) # a double press: the same one
	assert interrupts.count == 1
	now[0] += 2
	interrupts.handler(signal.SIGINT, None)
	assert interrupts.count == 2
	assert capsys.readouterr().out == "SIGINT received. Each camera stops and closes its video: please wait. If one " \
		"hangs as it closes, Ctrl-C again stops it at once, without closing it.\nSIGINT received again.\n"


def test_exit_codes():
	assert utils.ExitCodeText(None) == "no exit code"
	assert utils.ExitCodeText(1) == "exit code 1"
	assert utils.ExitCodeText(-11) == "exit code -11 (SIGSEGV)"
	assert utils.ExitCodeText(3221225477) == "exit code 3221225477 (0xC0000005)" # Windows: an access violation


# ------------------------------------------------------------------ Ctrl-C on Windows: waits that give up

# On Windows, a wait in the main thread on a multiprocessing pipe, queue or
# process also waits on Python's SIGINT event, and gives up when Ctrl-C sets
# it: once campy's handler has counted the press, it raises InterruptedError.
# POSIX waits wait on (PEP 475), so this does it as Windows does.

def interrupted_function_call():
	return InterruptedError(errno.EINTR, "Interrupted function call")


class WindowsWait:
	"""
	multiprocessing.connection.wait, for unicam only, as on Windows when
	Ctrl-C comes. presses: (after how many seconds, interrupts.count after
	it). The first call at or after that time counts the press, as campy's
	handler does, and raises InterruptedError; others wait as wait does.
	"""

	def __init__(self, monkeypatch, interrupts, presses):
		self.interrupts, self.presses = interrupts, list(presses)
		self.raised = 0
		self.t0 = time.monotonic()
		self.real = mp.connection.wait
		monkeypatch.setattr(unicam, "multiprocessing", types.SimpleNamespace(
			connection=types.SimpleNamespace(wait=self.wait)))

	def wait(self, objects, timeout=None):
		if self.presses and time.monotonic() - self.t0 >= self.presses[0][0]:
			_, self.interrupts.count = self.presses.pop(0)
			self.raised += 1
			raise interrupted_function_call()
		return self.real(objects, timeout)


def test_a_wait_for_the_camera_processes_that_ctrl_c_interrupts_goes_on(monkeypatch):
	# On Windows (the ChronicRecord PC) the InterruptedError ended Main while
	# its camera closed its video, and the camera process (daemon) with it
	ctx = mp.get_context("spawn")
	interrupts = types.SimpleNamespace(count=0)
	windows = WindowsWait(monkeypatch, interrupts, [(0, 1), (0, 1)]) # Ctrl-C, then a double press: the same one
	cameras = unicam.CameraProcesses(ctx, camera_process, camera_params(3), ctx.Queue(), log=lambda text: None)
	t0 = time.monotonic()
	failures = cameras.Wait(interrupts)
	assert time.monotonic() - t0 < 20
	assert windows.raised == 2
	why = "its camera process stopped, with exit code -9 (SIGKILL), after Ctrl-C" \
		if hasattr(signal, "SIGKILL") else "its camera process stopped, with exit code 9, after Ctrl-C"
	assert failures == {
		"Camera1": [why],
		"Camera2": ["ffmpeg stopped after taking 30 frames, with exit code -9 (SIGKILL)"],
		}
	assert not any(p.is_alive() for p in cameras.processes)
	assert cameras.processes[0].exitcode == 0 # Camera0 recorded to the end, and closed


class EndingProcess:
	# A camera process that is still running when Check looks, and has ended,
	# having said what went wrong, by the time Wait looks again
	def __init__(self):
		self.calls = 0
		self.exitcode, self.sentinel = 0, None

	def is_alive(self):
		self.calls += 1
		return self.calls == 1

	def join(self, timeout=None):
		pass


def test_a_camera_process_that_ends_just_after_check_still_has_its_failures_reported():
	# Two cameras that refused to record (files already there) ended together:
	# Wait returned with the failures of only one
	receive, send = mp.Pipe(duplex=False)
	send.send(("done", ["not recording"]))
	cameras = unicam.CameraProcesses.__new__(unicam.CameraProcesses)
	cameras.names, cameras.log = ["Camera0"], lambda text: None
	cameras.processes, cameras.connections = [EndingProcess()], [receive]
	cameras.failures, cameras.ended, cameras.done, cameras.ffmpeg, cameras.stopped = {}, set(), {}, {}, set()
	cameras.interrupted = False
	assert cameras.Wait() == {"Camera0": ["not recording"]}


def test_after_a_wait_that_ctrl_c_interrupts_a_second_ctrl_c_still_stops_a_camera_that_hangs(monkeypatch):
	ctx = mp.get_context("spawn")
	said = []
	interrupts = types.SimpleNamespace(count=0)
	windows = WindowsWait(monkeypatch, interrupts, [(0, 1), (2.5, 2)]) # Camera0 closes, Camera1 hangs
	cameras = unicam.CameraProcesses(ctx, hanging_camera_process, camera_params(2), ctx.Queue(), log=said.append)
	cameras.grace = 0.5
	t0 = time.monotonic()
	try:
		failures = cameras.Wait(interrupts)
	finally:
		# Camera1 ignores SIGTERM: left running, it would keep pytest from ending
		for p in cameras.processes:
			if p.is_alive():
				p.kill()
	assert time.monotonic() - t0 < 15
	assert windows.raised == 2
	assert failures == {"Camera1": ["stopped at the second Ctrl-C, before it had closed"]}
	assert said[0] == "Ctrl-C again: stopping Camera1 now, without closing it."
	assert not cameras.processes[1].is_alive()
	assert cameras.processes[0].exitcode == 0


class InterruptedQueue:
	# A ready queue as on Windows: reads are the messages it gives, in turn,
	# and None for a Ctrl-C (counted, and that read raises InterruptedError)
	def __init__(self, reads, interrupts):
		self.reads, self.interrupts = list(reads), interrupts

	def get(self, timeout=None):
		if not self.reads:
			time.sleep(timeout)
			raise queue.Empty
		read = self.reads.pop(0)
		if read is None:
			self.interrupts.count += 1
			raise interrupted_function_call()
		return read


def test_a_wait_for_cameras_that_ctrl_c_interrupts_stops_waiting():
	interrupts = types.SimpleNamespace(count=0)
	q = InterruptedQueue([(0, "Camera0", True, ""), None, (1, "Camera1", True, "")], interrupts)
	ready, missing = unicam.WaitForCameras(q, camera_params(2, timeout=30), log=lambda text: None,
		interrupted=lambda: interrupts.count > 0)
	assert ready == ["Camera0"] and missing == {"Camera1": "not ready when campy was stopped (Ctrl-C)"}


def test_a_wait_for_cameras_that_a_signal_interrupts_reads_again():
	interrupts = types.SimpleNamespace(count=0)
	q = InterruptedQueue([None, (0, "Camera0", True, ""), None, None, (1, "Camera1", True, "")], interrupts)
	ready, missing = unicam.WaitForCameras(q, camera_params(2, timeout=30), log=lambda text: None)
	assert ready == ["Camera0", "Camera1"] and missing == {}
	assert q.reads == []


@pytest.fixture
def ctrl_c(monkeypatch):
	"""
	Real Ctrl-C presses, from another thread: signal.raise_signal sets
	Python's SIGINT event, as Ctrl-C does. On Windows the main thread's wait
	then gives up, with InterruptedError (gave_up has when): unicam's real
	_winapi path. Elsewhere the wait goes on, and campy's handler runs after.
	"""
	gave_up = []
	real = mp.connection.wait
	def wait(objects, timeout=None):
		try:
			return real(objects, timeout)
		except InterruptedError:
			gave_up.append(time.monotonic())
			raise
	monkeypatch.setattr(mp.connection, "wait", wait) # a multiprocessing Queue's get waits with it too

	def press():
		# On Windows, again until a wait has given up (a press can come as the
		# main thread is between waits), for at most 0.8 s: presses within a
		# second of the first are the same one (HandleKeyboardInterrupt).
		# Elsewhere once: the handler runs only as the wait ends, so presses
		# 0.8 s apart could be counted 1 s apart.
		t0 = time.monotonic()
		signal.raise_signal(signal.SIGINT)
		while sys.platform == "win32" and time.monotonic() - t0 < 0.8:
			time.sleep(0.05)
			if gave_up:
				break
			signal.raise_signal(signal.SIGINT)

	old = signal.getsignal(signal.SIGINT)
	try:
		yield types.SimpleNamespace(press=press, gave_up=gave_up)
	finally:
		signal.signal(signal.SIGINT, old) # HandleKeyboardInterrupt leaves its handler


def test_ctrl_c_as_campy_waits_for_its_cameras_to_be_ready(ctrl_c):
	ctx = mp.get_context("spawn")
	readyQueue = ctx.Queue()
	def press_then_ready():
		ctrl_c.press()
		readyQueue.put((0, "Camera0", True, ""))
	with utils.HandleKeyboardInterrupt() as interrupts:
		threading.Thread(target=press_then_ready, daemon=True).start()
		ready, missing = unicam.WaitForCameras(readyQueue, camera_params(1, timeout=20), log=lambda text: None)
	assert ready == ["Camera0"] and missing == {}
	assert interrupts.count == 1
	if sys.platform == "win32":
		assert ctrl_c.gave_up


def test_ctrl_c_as_campy_waits_for_its_camera_processes(ctrl_c):
	# The ChronicRecord PC's first rc3 recording, stopped with one Ctrl-C
	ctx = mp.get_context("spawn")
	with utils.HandleKeyboardInterrupt() as interrupts:
		cameras = unicam.CameraProcesses(ctx, camera_process, camera_params(1), ctx.Queue(), log=lambda text: None)
		threading.Thread(target=ctrl_c.press, daemon=True).start()
		failures = cameras.Wait(interrupts)
	assert failures == {} and interrupts.count == 1
	assert cameras.processes[0].exitcode == 0 # Camera0 closed by itself: Main waited
	if sys.platform == "win32":
		assert ctrl_c.gave_up


# ------------------------------------------------------------------ end to end, on simulated RatCams

def tree(pid):
	# This process's descendants, as (pid, ppid, command)
	rows = []
	for line in subprocess.run(["ps", "-A", "-ww", "-o", "pid=,ppid=,command="], capture_output=True,
			text=True).stdout.splitlines():
		parts = line.split(None, 2)
		if len(parts) == 3:
			rows.append((int(parts[0]), int(parts[1]), parts[2]))
	found, todo = [], [pid]
	while todo:
		parent = todo.pop()
		for row in rows:
			if row[1] == parent:
				found.append(row)
				todo.append(row[0])
	return found


@pytest.mark.skipif(sys.platform == "win32", reason="finds the processes to kill with ps")
def test_campy_ends_and_says_which_camera_failed(tmp_path):
	pytest.importorskip("nireyecam")
	# Three simulated RatCams: EyeA's camera process is killed, EyeB's ffmpeg
	# is killed, and EyeC records to the end
	config = tmp_path / "config.yaml"
	config.write_text("\n".join([
		'videoFolder: "{}"'.format(tmp_path / "rec"), 'videoFilename: "0.mp4"', "recTimeInSec: 5", "numCams: 3",
		'cameraNames: ["EyeA", "EyeB", "EyeC"]', 'cameraMake: "ratcam"', 'ratcamPorts: ["sim", "sim", "sim"]',
		"frameRate: 30", "frameWidth: 160", "frameHeight: 120", "cameraExposureTimeInUs: 3000", "gpuID: -1",
		'pixelFormatOutput: "yuv420p"', "displayFrameRate: 0", 'preflight: "off"', 'ffmpegLogLevel: "warning"',
		"sessionFolder: False", ""]))
	env = dict(os.environ, PYTHONPATH=REPO, PYTHONUNBUFFERED="1")
	code = "import sys; sys.argv = ['campy-acquire', sys.argv[1]]; from campy.campy import Main; Main()"
	t0 = time.monotonic()
	campy = subprocess.Popen([sys.executable, "-c", code, str(config)], cwd=str(tmp_path), env=env,
		stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
	try:
		# Once every camera's ffmpeg has run for a second
		ffmpeg = {}
		while len(ffmpeg) < 3 and time.monotonic() - t0 < 60:
			time.sleep(0.2)
			ffmpeg = {name: (pid, ppid) for pid, ppid, command in tree(campy.pid) if "ffmpeg" in command
				for name in ("EyeA", "EyeB", "EyeC") if "/{}/0.mp4".format(name) in command}
		assert len(ffmpeg) == 3, "the cameras did not all start"
		time.sleep(1)
		os.kill(ffmpeg["EyeA"][1], signal.SIGKILL) # its camera process
		os.kill(ffmpeg["EyeB"][0], signal.SIGKILL) # its ffmpeg
		out, _ = campy.communicate(timeout=60)
	finally:
		if campy.poll() is None:
			campy.kill()
			for pid, ppid, command in tree(campy.pid):
				os.kill(pid, signal.SIGKILL)
	assert campy.returncode == 1, out

	# and its ffmpeg, which it had told campy about, is left to finish the video
	assert "EyeA: its camera process stopped, with exit code -9 (SIGKILL): killed, or crashed. Its files end " \
		"where it stopped. If its ffmpeg (pid {0}) is still running, it is finishing the video, or hangs: kill -9 {0} " \
		"ends it.\n".format(ffmpeg["EyeA"][0]) in out
	said = re.search(r"EyeB: ffmpeg stopped after taking (\d+) frames, with exit code -9 \(SIGKILL\): \[Errno 32\] "
		r"Broken pipe\. Stopping EyeB\.", out)
	assert said, out
	assert "Closing systems...\n" in out # and the trigger stopped, had there been one
	assert "2 of 3 cameras did not record to the end:\n" in out
	assert "  EyeA: its camera process stopped, with exit code -9 (SIGKILL): killed, or crashed\n" in out
	assert re.search(r"  EyeB: ffmpeg stopped after taking \d+ frames, with exit code -9 \(SIGKILL\)", out)

	def metadata(name):
		with open(tmp_path / "rec" / name / "metadata.csv") as f:
			return dict(csv.reader(f))

	# EyeB: closed, with what its video holds (counted from the file: ffmpeg writes a fragment a second, so a
	# second in, it may hold none of the frames it was given)
	sent, meta = int(said.group(1)), metadata("EyeB")
	held = int(meta["framesWritten"])
	assert held <= sent and int(meta["totalFrames"]) < 150
	assert meta["framesLostInEncoder"] == str(sent + 1 - held) # and the frame it failed on
	assert meta["videoError"].startswith("ffmpeg stopped after taking {} frames".format(sent))
	assert np.load(tmp_path / "rec" / "EyeB" / "frametimes.npy").shape == (2, held)
	if held:
		assert imageio_ffmpeg.count_frames_and_secs(str(tmp_path / "rec" / "EyeB" / "0.mp4"))[0] == held
	# EyeC: the whole recording
	meta = metadata("EyeC")
	assert meta["totalFrames"] == meta["framesWritten"] == "150" and "videoError" not in meta
	assert imageio_ffmpeg.count_frames_and_secs(str(tmp_path / "rec" / "EyeC" / "0.mp4"))[0] == 150
	# EyeA: killed, so no totals; its ffmpeg finished the video with the frames it had
	assert "totalFrames" not in metadata("EyeA")
	assert imageio_ffmpeg.count_frames_and_secs(str(tmp_path / "rec" / "EyeA" / "0.mp4"))[0] > 0


def running(pid):
	try:
		os.kill(pid, 0)
	except ProcessLookupError:
		return False
	except PermissionError:
		pass
	return True


# Imported by every Python process the test starts (PYTHONPATH): in each
# camera process, the camera named by CAMPY_TEST_HANG_CLOSE hangs as it
# closes, as one whose driver never returns would
HANG_HOOK = """
import os, time
name = os.environ.get("CAMPY_TEST_HANG_CLOSE")
if name:
	from campy.cameras import ratcam
	close = ratcam.CloseCamera
	def CloseCamera(cam_params, camera):
		if cam_params["cameraName"] == name:
			time.sleep(600)
		return close(cam_params, camera)
	ratcam.CloseCamera = CloseCamera
"""


@pytest.mark.skipif(sys.platform == "win32", reason="sends Ctrl-C to campy's process group, and finds ffmpeg with ps")
def test_a_second_ctrl_c_ends_campy_when_a_camera_hangs_as_it_closes(tmp_path):
	pytest.importorskip("nireyecam")
	hook = tmp_path / "hook"
	hook.mkdir()
	(hook / "sitecustomize.py").write_text(HANG_HOOK)
	config = tmp_path / "config.yaml"
	config.write_text("\n".join([
		'videoFolder: "{}"'.format(tmp_path / "rec"), 'videoFilename: "0.mp4"', "recTimeInSec: 60", "numCams: 2",
		'cameraNames: ["EyeA", "EyeB"]', 'cameraMake: "ratcam"', 'ratcamPorts: ["sim", "sim"]',
		"frameRate: 30", "frameWidth: 160", "frameHeight: 120", "cameraExposureTimeInUs: 3000", "gpuID: -1",
		'pixelFormatOutput: "yuv420p"', "displayFrameRate: 0", 'preflight: "off"', 'ffmpegLogLevel: "warning"',
		"sessionFolder: False", ""]))
	env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(hook), REPO]), PYTHONUNBUFFERED="1",
		CAMPY_TEST_HANG_CLOSE="EyeA")
	code = "import sys; sys.argv = ['campy-acquire', sys.argv[1]]; from campy.campy import Main; Main()"
	t0 = time.monotonic()
	# Its own process group, as campy is in a terminal: Ctrl-C reaches it and its camera processes
	campy = subprocess.Popen([sys.executable, "-c", code, str(config)], cwd=str(tmp_path), env=env,
		stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
	ffmpeg = {}
	try:
		while len(ffmpeg) < 2 and time.monotonic() - t0 < 60:
			time.sleep(0.2)
			ffmpeg = {name: pid for pid, ppid, command in tree(campy.pid) if "ffmpeg" in command
				for name in ("EyeA", "EyeB") if "/{}/0.mp4".format(name) in command}
		assert len(ffmpeg) == 2, "the cameras did not both start"
		time.sleep(1)
		os.killpg(campy.pid, signal.SIGINT) # Ctrl-C: EyeB closes, EyeA hangs as it closes
		time.sleep(3)
		assert campy.poll() is None, "campy should still be waiting for EyeA"
		os.killpg(campy.pid, signal.SIGINT) # Ctrl-C again
		out, _ = campy.communicate(timeout=30)
	finally:
		if campy.poll() is None:
			for pid, ppid, command in tree(campy.pid):
				os.kill(pid, signal.SIGKILL)
			campy.kill()
	assert campy.returncode == 1, out

	assert "SIGINT received. Each camera stops and closes its video: please wait. If one hangs as it closes, " \
		"Ctrl-C again stops it at once, without closing it.\n" in out
	assert "Ctrl-C again: stopping EyeA now, without closing it.\n" in out
	why = "stopped at the second Ctrl-C, before it had closed"
	assert "EyeA: {0}. Its files end where it stopped. If its ffmpeg (pid {1}) is still running, it is finishing the " \
		"video, or hangs: kill -9 {1} ends it.\n".format(why, ffmpeg["EyeA"]) in out
	assert "Closing systems...\n" in out
	assert "1 of 2 cameras did not record to the end:\n  EyeA: {}\n".format(why) in out

	# EyeB closed as usual
	with open(tmp_path / "rec" / "EyeB" / "metadata.csv") as f:
		meta = dict(csv.reader(f))
	assert int(meta["totalFrames"]) > 0 and meta["framesWritten"] == meta["totalFrames"]
	# EyeA's ffmpeg was left running: it gets the end of its input, and finishes the video
	deadline = time.monotonic() + 15
	while time.monotonic() < deadline and running(ffmpeg["EyeA"]):
		time.sleep(0.2)
	assert not running(ffmpeg["EyeA"])
	assert imageio_ffmpeg.count_frames_and_secs(str(tmp_path / "rec" / "EyeA" / "0.mp4"))[0] > 0
