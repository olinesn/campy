"""
A camera's ffmpeg that stops taking frames but does not exit (stuck on its
way out, as in a GPU driver call) is killed EXIT_WAIT_S later
(writer.ExitWatch), so that the restart (restartFfmpeg), or the camera's
stop, comes then, rather than when that ffmpeg goes. One that only takes
frames slowly, or that finishes its video slowly as the recording ends, is
not killed.

The ffmpeg here is a stand-in that passes the frames it takes to the real
one (libx264), so its video file is a real one, then does what the test
wants of it.

imageio-ffmpeg's "Error while attempting stop ffmpeg (w): [Errno 32] Broken
pipe" in these tests' logs comes from their 16x16 frames: a frame of 16 KB
or less that could not be written stays partly in Python's 8 KB pipe
buffer, and closing ffmpeg's input writes it again, to the same broken pipe
(the input is closed all the same). Larger frames, such as any camera's
(a 160x120 RatCam's are 19,200 bytes), leave nothing there, and nothing is
said.
"""

import os
import re
import subprocess
import sys
import time

import pytest

from campy import segments, writer
from test_failures import CameraProcess, FakeCamera, params
from test_ffmpeg_restart import Levels, Near, ReadSegments

FRAME = 16 * 16 # bytes: FakeCamera's frames, gray

STAND_IN = r"""
import os, subprocess, sys, time
mode, take, seconds, mark = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
ffmpeg = subprocess.Popen(sys.argv[5:], stdin=subprocess.PIPE)

def Pass(limit):
	# Gives the real ffmpeg what comes in, limit bytes (-1: up to the end)
	got = 0
	while limit < 0 or got < limit:
		data = os.read(0, 65536 if limit < 0 else min(65536, limit - got))
		if not data:
			return
		ffmpeg.stdin.write(data)
		got += len(data)

Pass(take)
if mode == "stall": # takes no frame for a while, its input still open, then goes on
	ffmpeg.stdin.flush()
	time.sleep(seconds)
	Pass(-1)
ffmpeg.stdin.close() # its video is finished, as ffmpeg finishes it before it closes its input
code = ffmpeg.wait()
if mode == "hang": # closes its input, and does not exit
	os.close(0)
	with open(mark, "w") as f:
		f.write(repr(time.time()))
	time.sleep(seconds)
	sys.exit(1)
if mode == "slow": # given every frame, it takes this long to exit
	time.sleep(seconds)
sys.exit(code)
"""


def StandIn(monkeypatch, tmp_path, *modes):
	"""
	The next recording ffmpegs started (one for each mode: what it does, the
	bytes it passes on first, and for how many seconds) are the stand-in,
	running the real ffmpeg; the others are the real ffmpeg. Returns their
	Popens, and the file where a "hang" one writes when it closed its input.
	"""
	modes, launched = list(modes), []
	mark = str(tmp_path / "closed.txt")
	real = subprocess.Popen

	class Popen(real):
		def __init__(self, args, *a, **k):
			if modes and isinstance(args, list) and "-i" in args and args[args.index("-i") + 1] == "-":
				mode, take, seconds = modes.pop(0)
				args = [sys.executable, "-c", STAND_IN, mode, str(take), str(seconds), mark] + list(args)
				launched.append(self)
			super().__init__(args, *a, **k)
	monkeypatch.setattr(subprocess, "Popen", Popen)
	return launched, mark


def Restarts(monkeypatch):
	# When each restart began (time.time)
	began, real = [], writer.Parts.Restart

	def restart(self, text):
		began.append(time.time())
		return real(self, text)
	monkeypatch.setattr(writer.Parts, "Restart", restart)
	return began


KILLED = "and did not exit within {:g} s, so campy killed it".format(writer.EXIT_WAIT_S)
SAID = "Camera0: ffmpeg stopped taking frames and did not exit within {:g} s, stuck on its way out: killed it " \
	"(pid {}).\n"


def test_an_ffmpeg_that_does_not_exit_is_killed_and_restarted(tmp_path, monkeypatch, capsys):
	# It wrote its file and closed its input after 100 frames, then would not exit for 30 s
	launched, mark = StandIn(monkeypatch, tmp_path, ("hang", 100 * FRAME, 30))
	began = Restarts(monkeypatch)
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=6, restartFfmpeg=True, writeQueueMaxSec="None"),
		FakeCamera())
	took = run.Run(timeout=40)
	out = capsys.readouterr().out
	(stuck,) = launched
	assert took < 20, out # not the 30 s it would have waited
	assert stuck.returncode != 0 and (sys.platform == "win32" or stuck.returncode == -9) # killed
	assert SAID.format(writer.EXIT_WAIT_S, stuck.pid) in out

	# Killed EXIT_WAIT_S after it closed its input, give or take the watch's look every EXIT_CHECK_S, and the
	# restart came at once
	with open(mark) as f:
		closed = float(f.read())
	(restart,) = began
	assert writer.EXIT_WAIT_S - 0.05 <= restart - closed < writer.EXIT_WAIT_S + writer.EXIT_CHECK_S + 2.5

	# The camera never stopped, and every frame is in a file or named as lost
	assert run.recording.Failures() == [] and run.recording.restarts == 1
	meta = run.Metadata()
	assert meta["totalFrames"] == "600" and meta["framesDropped"] == "0" and "videoError" not in meta
	rows = ReadSegments(run.Folder())
	assert [r["file"] for r in rows] == ["0.mp4", "0.part2.mp4"]
	first, second = rows
	assert re.fullmatch(r"ffmpeg stopped after taking \d+ frames, {}: \[Errno (32|22)\] .*".format(KILLED),
		first["ended"]), first["ended"]
	assert first["first_frame"] == "1" and first["frames"] == "100" # what the stand-in gave the real ffmpeg
	lost = segments.ParseRanges(first["lost_frames"])
	assert lost == list(range(101, int(first["last_frame"]) + 1)) and int(first["lost"]) == len(lost)
	assert int(second["first_frame"]) == int(first["last_frame"]) + 1 and second["last_frame"] == "600"
	assert second["ended"] == "the recording ended" and second["lost"] == "0"
	expected = [n for n in range(1, 601) if n not in set(lost)]
	assert [int(n) for n in run.FrameTimes()[0]] == expected and meta["framesWritten"] == str(len(expected))
	assert meta["framesLostInEncoder"] == str(len(lost))
	levels = Levels(os.path.join(run.Folder(), "0.mp4")) + Levels(os.path.join(run.Folder(), "0.part2.mp4"))
	assert len(levels) == len(expected) and [n for n, level in zip(expected, levels) if not Near(level, n)] == []


def test_without_restartFfmpeg_an_ffmpeg_that_does_not_exit_is_killed_and_the_camera_stops(tmp_path, monkeypatch,
		capsys):
	launched, _ = StandIn(monkeypatch, tmp_path, ("hang", 100 * FRAME, 30))
	camera = FakeCamera()
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=60), camera)
	took = run.Run(timeout=40)
	out = capsys.readouterr().out
	(stuck,) = launched
	assert took < 20, out
	assert SAID.format(writer.EXIT_WAIT_S, stuck.pid) in out
	meta = run.Metadata()
	said = re.fullmatch(r"ffmpeg stopped after taking (\d+) frames, {}: \[Errno (32|22)\] .*".format(KILLED),
		meta["videoError"])
	assert said, meta["videoError"]
	assert "Camera0: {}. Stopping Camera0.".format(meta["videoError"]) in out and camera.closed
	sent = int(said.group(1))
	assert meta["framesWritten"] == "100" and run.VideoFrames() == 100
	assert meta["framesLostInEncoder"] == str(sent + 1 - 100) # and the frame it failed on
	assert [int(n) for n in run.FrameTimes()[0]] == list(range(1, 101))
	assert int(meta["totalFrames"]) < 2000 # it stopped, rather than grab 6000


def test_an_ffmpeg_finishing_its_video_slowly_is_not_killed(tmp_path, monkeypatch, capsys):
	# As the recording ends its input is closed, and it takes longer than EXIT_WAIT_S to exit: that is the
	# video's end, not a hang
	slow = writer.EXIT_WAIT_S + 2 * writer.EXIT_CHECK_S + 1
	launched, _ = StandIn(monkeypatch, tmp_path, ("slow", -1, slow))
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=1, restartFfmpeg=True), FakeCamera())
	took = run.Run(timeout=30)
	out = capsys.readouterr().out
	(finishing,) = launched
	assert took >= slow and finishing.returncode == 0
	assert "killed" not in out and run.recording.Failures() == []
	meta = run.Metadata()
	assert meta["framesWritten"] == "100" and run.VideoFrames() == 100 and "videoError" not in meta


def test_an_ffmpeg_that_takes_no_frame_for_a_while_is_not_killed(tmp_path, monkeypatch, capsys):
	# Its input open, it takes no frame for longer than EXIT_WAIT_S (a disk stall), then goes on: campy waits
	# (frames wait in memory, and the recording watch says so), whether or not restartFfmpeg is on
	stall = writer.EXIT_WAIT_S + 2 * writer.EXIT_CHECK_S + 1
	launched, _ = StandIn(monkeypatch, tmp_path, ("stall", 50 * FRAME, stall))
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=3, restartFfmpeg=True, writeQueueMaxSec="None"),
		FakeCamera())
	run.Run(timeout=40)
	out = capsys.readouterr().out
	(stalled,) = launched
	assert stalled.returncode == 0 and "killed" not in out
	assert run.recording.Failures() == [] and run.recording.restarts == 0
	meta = run.Metadata()
	assert meta["framesWritten"] == "300" and run.VideoFrames() == 300 and meta["framesDropped"] == "0"
	assert not os.path.exists(os.path.join(run.Folder(), "segments.csv"))


def test_a_killed_ffmpeg_still_there_is_said(monkeypatch, capsys):
	# One that does not go even when killed (stuck in a driver call, in the kernel) is said, once
	monkeypatch.setattr(writer, "EXIT_WAIT_S", 0.2)
	monkeypatch.setattr(writer, "EXIT_CHECK_S", 0.05)
	monkeypatch.setattr(writer, "KILLED_WAIT_S", 0.3)
	child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.PIPE)
	kills = []
	child.kill = lambda: kills.append(1) # a kill the stuck process does not die of
	watch = writer.ExitWatch("Camera0")
	try:
		watch.Add(child)
		child.stdin.close() # as imageio-ffmpeg does when a frame cannot be written
		time.sleep(2)
	finally:
		watch.Stop()
		subprocess.Popen.kill(child)
		child.wait()
	out = capsys.readouterr().out
	assert len(kills) == 1 and watch.Killed(child) == "and did not exit within 0.2 s, so campy killed it"
	assert SAID.format(0.2, child.pid) in out
	assert out.count("Camera0: its ffmpeg (pid {}) is still there 0.3 s after it was killed: it is stuck in the "
		"operating system".format(child.pid)) == 1
