"""
campy-acquire's status line (campy/statusline.py, statusLine): one line on
the console, rewritten in place, instead of each camera's "collected N
frames" lines; on a console only, unless asked for.
"""

import io
import multiprocessing as mp
import os
import re
import subprocess
import sys
import time
import types

import pytest

from campy import status, statusline
from campy.cameras import unicam
from test_emu import write_clip

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLEAR = statusline.ANSI_CLEAR


class Tty(io.StringIO):
	def isatty(self):
		return True


class Writes:
	# A stream that keeps each write
	def __init__(self):
		self.writes = []
		self.encoding = "utf-8"

	def write(self, text):
		self.writes.append(text)
		return len(text)

	def flush(self):
		pass

	def isatty(self):
		return True

	def Text(self):
		return "".join(self.writes)


@pytest.fixture(autouse=True)
def no_status_line_left():
	# Prepare sets the camera processes' variable in this process's environment: none is left for other tests
	saved = os.environ.get(statusline.ENV)
	yield
	if saved is None:
		os.environ.pop(statusline.ENV, None)
	else:
		os.environ[statusline.ENV] = saved


# ---------------------------------------------------------------------- whether

def test_auto_shows_it_on_a_console_only(monkeypatch):
	assert statusline.Wanted("auto", Tty()) is True
	assert statusline.Wanted("auto", io.StringIO()) is False # a log file, a pipe, a test
	assert statusline.Wanted(None, io.StringIO()) is False
	assert statusline.Wanted("always", io.StringIO()) is True
	assert statusline.Wanted("Never", Tty()) is False


def test_prepare_tells_the_camera_processes(monkeypatch):
	monkeypatch.delenv(statusline.ENV, raising=False)
	# Tty is no console of Windows' own, whose VT processing campy would turn on: as one where it is on
	monkeypatch.setattr(statusline, "EnableVT", lambda stream=None: True)
	assert statusline.Prepare({"statusLine": "auto"}, io.StringIO(), io.StringIO()) is None
	assert not statusline.Shown() and statusline.ChildMode() is None
	mode = statusline.Prepare({"statusLine": "auto"}, Tty(), io.StringIO())
	assert (mode.how, mode.stderr) == ("ansi", False) and os.environ[statusline.ENV] == "ansi"
	assert statusline.Shown()
	mode = statusline.Prepare({"statusLine": "always"}, io.StringIO(), io.StringIO())
	assert mode.stderr and os.environ[statusline.ENV] == "ansi,stderr"
	assert vars(statusline.ChildMode()) == {"how": "ansi", "stderr": True}
	statusline.Prepare({"statusLine": "never"}, Tty(), Tty())
	assert statusline.ENV not in os.environ
	# Where it can't be turned on (a classic console that refuses it): spaces
	monkeypatch.setattr(statusline, "EnableVT", lambda stream=None: False)
	assert statusline.Prepare({"statusLine": "auto"}, Tty(), io.StringIO()).how == "pad"


def test_without_ansi_it_clears_with_spaces(monkeypatch):
	monkeypatch.setattr(statusline, "Width", lambda: 30)
	assert statusline.ClearText("pad") == "\r" + " " * 30 + "\r"
	assert statusline.ClearText("ansi") == "\r\x1b[K"


# ---------------------------------------------------------------------- lines and the status line

def test_a_message_clears_the_status_line_and_it_comes_back_under_it():
	out, err = Writes(), Writes()
	console = statusline.Console(out, err, "ansi")
	console.Draw("Recording 0:05")
	console.Write("stdout", "Trigger: paused as planned\n")
	assert out.Text() == CLEAR + "Recording 0:05" + CLEAR + "Trigger: paused as planned\n" + CLEAR + "Recording 0:05"
	# stderr: the status line (on stdout) is cleared first, then drawn again on stdout
	out.writes.clear()
	console.Write("stderr", "WARNING something\n")
	assert out.Text() == CLEAR + CLEAR + "Recording 0:05" and err.Text() == "WARNING something\n"


def test_never_drawn_in_the_middle_of_a_line():
	# print() writes its text and its newline apart: the status line waits for the newline
	out, err = Writes(), Writes()
	console = statusline.Console(out, err, "ansi")
	stdout = statusline.Stream(console, "stdout", out)
	console.Draw("Recording 0:01")
	out.writes.clear()
	print("Camera0: something", file=stdout)
	assert out.Text() == CLEAR + "Camera0: something\n" + CLEAR + "Recording 0:01"
	out.writes.clear()
	stdout.write("half a line")
	console.Draw("Recording 0:02") # the status line's tick, from its thread
	assert out.Text() == CLEAR + "half a line"
	stdout.write(", and its end\n")
	assert out.Text() == CLEAR + "half a line, and its end\n" + CLEAR + "Recording 0:02"
	console.Clear()
	assert out.writes[-1] == CLEAR
	out.writes.clear()
	stdout.write("after it\n")
	assert out.Text() == "after it\n" # no status line any more


def test_a_camera_process_line_starts_with_the_clear_and_goes_out_whole():
	real = Writes()
	child = statusline.ChildStream(real, "ansi")
	child.write("Camera0 saved 600 frames")
	assert real.writes == []
	child.write("\n")
	assert real.writes == [CLEAR + "Camera0 saved 600 frames\n"]
	child.write("two\nlines\n")
	assert real.writes[-1] == CLEAR + "two\nlines\n"
	child.write("no newline")
	child.flush()
	assert real.writes[-1] == CLEAR + "no newline"


def test_child_wraps_only_when_the_main_process_shows_it(monkeypatch):
	out, err = Writes(), Writes()
	monkeypatch.setattr(sys, "stdout", out)
	monkeypatch.setattr(sys, "stderr", err)
	monkeypatch.delenv(statusline.ENV, raising=False)
	statusline.Child()
	assert sys.stdout is out and sys.stderr is err
	monkeypatch.setenv(statusline.ENV, "ansi")
	statusline.Child()
	assert isinstance(sys.stdout, statusline.ChildStream) and sys.stderr is err


def test_a_camera_does_not_print_its_progress_lines_under_the_status_line(monkeypatch):
	out = Writes()
	monkeypatch.setattr(sys, "stdout", out)
	g = {"cameraName": "Camera1", "chunkLengthInFrames": 60, "frameTimes": types.SimpleNamespace(first=0.0)}
	monkeypatch.setenv(statusline.ENV, "ansi")
	for n in range(1, 181):
		unicam.CountFPS(g, n, (n - 1) / 60.0)
	assert out.writes == []
	monkeypatch.delenv(statusline.ENV)
	unicam.CountFPS(g, 240, 239 / 60.0)
	assert out.writes == ["Camera1 collected 240 frames at 60.0 fps for 4 sec.\n"]


# ---------------------------------------------------------------------- what it says

class Rig:
	"""Counters as campy's camera processes keep them, on a clock the test moves."""

	def __init__(self, n=4, rate=60.0):
		self.array = status.Make(mp.get_context("spawn"), n)
		self.cams = [status.Camera(self.array, c) for c in range(n)]
		self.t = 100.0 # s, both clocks
		self.rate = rate
		self.names = ["Camera{}".format(c) for c in range(n)]

	def ns(self):
		return int(self.t * 1e9)

	def Line(self, **kw):
		line = statusline.StatusLine(statusline.Mode("ansi", False), self.array, self.names, frame_rate=self.rate,
			clock=lambda: self.t, now_ns=self.ns, **kw)
		line.started = self.t
		return line

	def Run(self, seconds, cams=None, rate=None):
		# Frames at rate on these cameras, the counters read every second as the status line's thread does
		rate = rate or self.rate
		for _ in range(int(seconds)):
			self.t += 1.0
			for c in (range(len(self.cams)) if cams is None else cams):
				row = status.Reader(self.array, len(self.cams)).Row(c)
				self.cams[c].State(status.GRABBING)
				self.array[c * status.WIDTH + status.GRABBED] = row["grabbed"] + int(rate)
				self.array[c * status.WIDTH + status.FRAME_NS] = self.ns()
			yield


@pytest.fixture
def wide(monkeypatch):
	monkeypatch.setattr(statusline, "Width", lambda: 200)


def test_waiting_then_recording(wide):
	rig = Rig()
	line = rig.Line()
	for c in rig.cams:
		c.State(status.GRABBING)
	rig.t += 12
	assert line.Text() == "Waiting for the first frame 0:12 · Camera0 0 · Camera1 0 · Camera2 0 · Camera3 0 frames"
	for _ in rig.Run(180):
		text = line.Text()
	assert text == "Recording 2:59 · Camera0 10,800 · Camera1 10,800 · Camera2 10,800 · Camera3 10,800 frames · " \
		"60.0 fps"


def test_a_trigger_pause_and_after_it(wide):
	rig = Rig(n=2)
	line = rig.Line()
	for _ in rig.Run(10):
		line.Text()
	rig.t += 45
	assert line.Text() == "Recording 0:54 · Camera0 600 · Camera1 600 frames · trigger paused 0:45"
	for _ in rig.Run(5):
		text = line.Text()
	# the rate over the frames since, not lowered by the pause
	assert text == "Recording 0:59 · Camera0 900 · Camera1 900 frames · 60.0 fps · 1 trigger pause"
	rig.t += 30
	assert line.Text().endswith("frames · trigger paused 0:30 (pause 2)")


def test_cameras_at_different_rates_and_one_that_stopped(wide):
	rig = Rig(n=3)
	line = rig.Line()
	for _ in rig.Run(6):
		for c in rig.Run(0):
			pass
		line.Text()
	# Camera2 slower: one rate each
	for _ in rig.Run(6, cams=[2], rate=30):
		pass
	rig.t -= 6
	for _ in rig.Run(6, cams=[0, 1]):
		pass
	text = line.Text()
	assert text.endswith("· fps 60.0/60.0/30.0"), text
	rig.cams[1].State(status.LOST)
	rig.array[status.WIDTH + status.DROPPED] = 0
	rig.array[status.DROPPED] = 12
	text = line.Text()
	assert "Camera1 720 (lost)" in text and "· 12 dropped ·" in text, text


def test_it_fits_the_console(monkeypatch):
	rig = Rig()
	line = rig.Line()
	for _ in rig.Run(3):
		line.Text()
	monkeypatch.setattr(statusline, "Width", lambda: 80)
	text = line.Text()
	assert text == "Recording 0:02 · frames 180 / 180 / 180 / 180 · 60.0 fps"
	monkeypatch.setattr(statusline, "Width", lambda: 30)
	text = line.Text()
	assert len(text) == 30 and text.endswith("…")


def test_clock():
	assert [statusline.Clock(s) for s in (0, 59, 180, 3723, 2 * 86400 + 3723)] == \
		["0:00", "0:59", "3:00", "1:02:03", "2d 01:02:03"]


def test_the_separator_where_the_console_has_no_middle_dot():
	assert statusline.Dot("cp1252").join(["a", "b"]) == "a · b"
	assert statusline.Dot("ascii").join(["a", "b"]) == "a | b"


# ---------------------------------------------------------------------- campy-acquire, emulated cameras

def Acquire(tmp_path, stdout, **config):
	for name in ("Camera0", "Camera1"):
		(tmp_path / name).mkdir(exist_ok=True)
		write_clip(str(tmp_path / name / "clip.mp4"), frames=10)
	values = dict(numCams=2, cameraMake='"emu"', videoFolder='"{}"'.format(tmp_path.as_posix()),
		videoFilename='"emuclip.mp4"', frameRate=30, recTimeInSec=4, chunkLengthInSec=1, displayFrameRate=0,
		pixelFormatInput='"gray"', pixelFormatOutput='"yuv420p"', gpuID=-1, codec='"h264"', preflight='"off"',
		alerts="False", alertDiskFreeGB=0, stopAtFreeGB=0)
	values.update(config)
	(tmp_path / "config.yaml").write_text("".join("{}: {}\n".format(k, v) for k, v in values.items()))
	env = dict(os.environ, PYTHONPATH=REPO, PYTHONUNBUFFERED="1", CAMPY_ALERTS_FILE=str(tmp_path / "none.yaml"),
		CAMPY_STATUS_DIR=str(tmp_path / "status"), COLUMNS="200")
	env.pop("IMAGEIO_FFMPEG_EXE", None)
	env.pop(statusline.ENV, None)
	code = "import sys; sys.argv = ['campy-acquire', sys.argv[1]]\nfrom campy.campy import Main\nMain()\n"
	return subprocess.run([sys.executable, "-c", code, str(tmp_path / "config.yaml")], cwd=str(tmp_path), env=env,
		stdout=stdout, stderr=subprocess.STDOUT, timeout=120)


def test_acquire_into_a_log_keeps_the_lines(tmp_path):
	proc = Acquire(tmp_path, subprocess.PIPE)
	out = proc.stdout.decode().replace("\r\n", "\n") # Windows ends its lines with \r\n
	assert proc.returncode == 0, out
	assert re.search(r"^Camera0 collected 30 frames at", out, re.M), out
	assert "\x1b" not in out and "\r" not in out, [line for line in out.split("\n") if "\x1b" in line or "\r" in line]


@pytest.mark.skipif(sys.platform == "win32", reason="a pseudo-terminal: POSIX only")
def test_acquire_on_a_console_shows_one_status_line(tmp_path):
	import pty
	import tty
	master, slave = pty.openpty()
	tty.setraw(slave) # as a console shows them: "\r" and "\n" as written
	chunks = []
	import threading
	def read():
		while True:
			try:
				data = os.read(master, 65536)
			except OSError:
				return
			if not data:
				return
			chunks.append(data)
	reader = threading.Thread(target=read, daemon=True)
	reader.start()
	try:
		proc = Acquire(tmp_path, slave)
	finally:
		os.close(slave)
		reader.join(5)
		os.close(master)
	out = b"".join(chunks).decode()
	assert proc.returncode == 0, out
	assert "collected" not in out
	assert re.search(r"\r\x1b\[KRecording 0:0\d · Camera0 [\d,]+ · Camera1 [\d,]+ frames", out), out
	# Every other line is whole: none starts after a half-drawn status line, and the end's lines are there
	lines = out.split("\n")
	status = re.compile(r"(Waiting for the first frame|Recording) \d+:\d\d · Camera0 [\d,]+ · Camera1 [\d,]+ frames"
		r"( · [\d.]+ fps| · fps [\d./-]+)?$")
	for text in lines:
		shown = text.split(CLEAR)[-1] # what stays on screen once each clear is done
		assert "Recording 0" not in shown or status.match(shown), text
	assert any(re.match(r"Camera[01] saved 120 frames", t.split(CLEAR)[-1]) for t in lines), out
	# The status line is gone at the end: the last line on screen is the end's own
	assert out.endswith("\n") and "Recording" not in lines[-2].split(CLEAR)[-1], lines[-3:]
