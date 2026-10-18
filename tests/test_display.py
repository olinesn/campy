"""The preview window (campy/display.py), on Qt's offscreen platform: no window opens."""

import os
import subprocess
import sys
import textwrap
import types
from collections import deque

import numpy as np
import pytest

from campy.cameras import ratcam

OFFSCREEN = dict(os.environ, QT_QPA_PLATFORM="offscreen")


def QtStarts():
	# A Qt that cannot start aborts its process, so try it in another one first
	try:
		done = subprocess.run([sys.executable, "-c", "from PyQt5.QtWidgets import QApplication; QApplication([])"],
			env=OFFSCREEN, capture_output=True, timeout=60)
	except subprocess.TimeoutExpired:
		return False
	return done.returncode == 0


needs_qt = pytest.mark.skipif(not QtStarts(), reason="Qt's offscreen platform does not start here")


@pytest.fixture(scope="module")
def qt():
	before = os.environ.get("QT_QPA_PLATFORM")
	os.environ["QT_QPA_PLATFORM"] = "offscreen"
	from PyQt5 import QtGui, QtWidgets
	from campy import display
	app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
	yield types.SimpleNamespace(app=app, QtGui=QtGui, display=display)
	if before is None:
		os.environ.pop("QT_QPA_PLATFORM", None)
	else:
		os.environ["QT_QPA_PLATFORM"] = before


def gray(height=48, width=64):
	# A smooth gradient: exact at 1:1, and close to it when scaled
	y, x = np.mgrid[0:height, 0:width]
	return (40 + 2 * x + y).astype(np.uint8)


def rgb_at(image, x, y):
	color = image.pixelColor(x, y)
	return color.red(), color.green(), color.blue()


@needs_qt
def test_mono_frames_are_gray(qt):
	frame = gray(6, 8)
	image, pixels = qt.display.ToQImage(frame)
	assert image.format() == qt.QtGui.QImage.Format_Grayscale8
	assert all(rgb_at(image, x, y) == (frame[y, x],) * 3 for y in range(6) for x in range(8))


@needs_qt
def test_color_frames_are_rgb(qt):
	frame = np.zeros((6, 8, 3), np.uint8)
	frame[..., 0], frame[..., 1], frame[..., 2] = gray(6, 8), 7, 250
	image, pixels = qt.display.ToQImage(frame)
	assert all(rgb_at(image, x, y) == (frame[y, x, 0], 7, 250) for y in range(6) for x in range(8))


@needs_qt
def test_downsampled_frames(qt):
	# DisplayImage sends frame[::step, ::step], whose rows are not contiguous
	frame = gray()
	image, pixels = qt.display.ToQImage(frame[::2, ::2])
	assert (image.width(), image.height()) == (32, 24)
	assert all(rgb_at(image, x, y)[0] == frame[2 * y, 2 * x] for y in range(24) for x in range(32))


@needs_qt
def test_deeper_frames_are_scaled_to_white(qt):
	frame = np.array([[0, 4095], [2048, 1024]], np.uint16) # 12-bit values in 16 bits
	image, pixels = qt.display.ToQImage(frame)
	assert [rgb_at(image, x, y)[0] for y in range(2) for x in range(2)] == [0, 255, 127, 63]


@needs_qt
def test_the_window_shows_the_frame_scaled_to_fit(qt):
	window = qt.display.PreviewWindow("Camera0")
	frame = gray()
	window.ShowFrame(frame)
	assert (window.width(), window.height()) == (64, 48) # the first frame sizes the window

	shot = window.grab().toImage()
	assert all(rgb_at(shot, x, y)[0] == frame[y, x] for y in range(0, 48, 5) for x in range(0, 64, 5))

	window.resize(128, 48) # wider: same size, centred, black either side
	shot = window.grab().toImage()
	assert rgb_at(shot, 5, 24) == (0, 0, 0) and rgb_at(shot, 122, 24) == (0, 0, 0)
	assert rgb_at(shot, 32 + 20, 10)[0] == frame[10, 20]

	window.resize(128, 96) # twice the size
	shot = window.grab().toImage()
	assert abs(int(rgb_at(shot, 2 * 20 + 1, 2 * 10 + 1)[0]) - int(frame[10, 20])) <= 3
	window.close()


@needs_qt
def test_runs_in_its_own_thread_until_the_camera_stops():
	# As in a camera process: Qt's application made in the display thread.
	# A frame it cannot show is reported once, and the frames after it are shown.
	script = textwrap.dedent("""
		import threading
		from collections import deque
		import numpy as np
		from campy import display

		shown = []
		show = display.PreviewWindow.ShowFrame
		def ShowFrame(self, img):
			show(self, img)
			shown.append((img.shape, self.width(), self.height()))
		display.PreviewWindow.ShowFrame = ShowFrame

		frames = deque([np.zeros((48, 64), np.uint8), np.zeros((4, 4, 2), np.uint8),
			np.full((48, 64, 3), 200, np.uint8), np.zeros((4, 4, 2), np.uint8), "STOP"])
		cam = {"cameraName": "Camera0", "cameraMake": "flir", "frameWidth": 128, "frameHeight": 96,
			"displayDownsample": 2}
		thread = threading.Thread(target=display.DisplayFrames, args=(cam, frames), daemon=True)
		thread.start()
		thread.join(30)
		print("finished", not thread.is_alive(), "left", len(frames))
		print("shown", shown)
		""")
	done = subprocess.run([sys.executable, "-c", script], env=OFFSCREEN, capture_output=True, text=True, timeout=120)
	assert done.returncode == 0, done.stderr
	lines = done.stdout.splitlines()
	assert "finished True left 0" in lines
	assert "shown [((48, 64), 64, 48), ((48, 64, 3), 64, 48)]" in lines
	errors = [line for line in lines if "the preview cannot show its frames" in line]
	assert len(errors) == 1 and errors[0].startswith("Camera0: ")


def test_ratcam_frames_go_to_the_display_as_gray():
	frame = gray()
	grab = types.SimpleNamespace(image=lambda: frame)
	queue = deque()
	ratcam.DisplayImage({"displayDownsample": 2}, queue, grab)
	[img] = queue
	assert img.shape == (24, 32) and (img == frame[::2, ::2]).all()
