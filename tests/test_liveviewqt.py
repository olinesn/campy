"""
campy-live-view's Qt window (campy/liveviewqt.py), on Qt's offscreen
platform: no window opens. The view in it is the Viewer's (tests/
test_liveview.py checks what that draws); these check that the window shows
it at its own size, and passes it the keys, the buttons and the mouse.
"""

import os, subprocess, sys, types

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
pytest.importorskip("PyQt5")

from campy import live, livefeed, liveresults, liveview

import test_pose3d as scene3d
from test_liveview import EDGES, like, mice, row_for, scaled, view_args
from test_livefeed import name, rings # noqa: F401 (rings: a fixture)

OFFSCREEN = dict(os.environ, QT_QPA_PLATFORM="offscreen")


def QtStarts():
	# A Qt that cannot start aborts its process, so try it in another one first
	try:
		done = subprocess.run([sys.executable, "-c", "from PyQt5.QtWidgets import QApplication; QApplication([])"],
			env=OFFSCREEN, capture_output=True, timeout=60)
	except subprocess.TimeoutExpired:
		return False
	return done.returncode == 0


pytestmark = pytest.mark.skipif(not QtStarts(), reason="Qt's offscreen platform does not start here")


@pytest.fixture(scope="module")
def qt():
	before = os.environ.get("QT_QPA_PLATFORM")
	os.environ["QT_QPA_PLATFORM"] = "offscreen"
	from PyQt5 import QtCore, QtGui, QtTest, QtWidgets
	from campy import liveviewqt
	app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
	yield types.SimpleNamespace(app=app, QtCore=QtCore, QtGui=QtGui, QtTest=QtTest, QtWidgets=QtWidgets,
		liveviewqt=liveviewqt, Qt=QtCore.Qt)
	if before is None:
		os.environ.pop("QT_QPA_PLATFORM", None)
	else:
		os.environ["QT_QPA_PLATFORM"] = before


S = 0.25 # the rig's cameras, at 320 x 256


@pytest.fixture
def window(qt, rings):
	# The Qt window on a feed of 3 cameras (frames 1-12, gray) and campy-live's results ring
	cams, truth, keypoints, scores, order = mice()
	prefix = name()
	writers = [rings(livefeed.RingName(prefix, c), (256, 320), np.uint8, slots=8, cameraName="Camera{}".format(c))
		for c in range(3)]
	for n in range(1, 13):
		for w in writers:
			w.Write(np.full((256, 320), 60, np.uint8), n, 1000 * n)
	publisher = liveresults.Publisher(liveresults.RingName(prefix), live.RowType(3, 3, truth.shape[1], 3),
		cameras=["Camera0", "Camera1", "Camera2"], rings=[livefeed.RingName(prefix, c) for c in range(3)],
		edges=EDGES, animals=3)
	args = view_args(prefix, "--gui", "qt")
	viewer = liveview.Viewer(args)
	viewer.panel = False
	win = qt.liveviewqt.Window(viewer, args)
	win.resize(1100, 600)
	win.show()
	qt.app.processEvents()
	viewer.Check()
	small = [scaled(c, S) for c in cams]
	viewer.calibration, viewer.scene = small, liveview.Scene(small)
	def Result(frame, lift=0.0, bad=None):
		# campy-live's row for frame (its 3D lifted by lift mm; identity bad's 234 px off), shown in the window
		row = row_for(frame, cams, truth + [0, 0, lift], keypoints * S, scores, order)
		if bad is not None:
			row["reproj_px"][bad] = 234
		publisher.Write(row)
		win.Step()
		qt.app.processEvents()
	yield types.SimpleNamespace(win=win, viewer=viewer, Result=Result, truth=truth)
	win.close()
	viewer.Close()
	publisher.Close()


def colours(image):
	return [int(like(image, liveview.Color(a)).sum()) for a in range(3)]


def test_each_result_is_shown_at_the_windows_own_size(qt, window):
	win, viewer = window.win, window.viewer
	window.Result(9)
	image = win.picture.image
	w, h = win.picture.Pixels()
	assert image.shape == (h, w, 3) and (w, h) == viewer.windowSize
	assert viewer.layout.panel is None # the window's panel is Qt's, beside the view
	assert all(n > 50 for n in colours(image)) # every identity, on the cameras and in 3D
	assert int(viewer.last[1]["frame"]) == 9
	# What the panel says, in Qt's text
	text = win.status.text()
	assert "#0: from 3 cameras" in text and "#2: from 3 cameras" in text
	# Smaller, then larger: each result is drawn at the window's size then, its tiles larger as it grows
	win.resize(760, 420)
	qt.app.processEvents()
	window.Result(10)
	w, h = win.picture.Pixels()
	assert win.picture.image.shape == (h, w, 3)
	tile = viewer.layout.tile
	win.resize(1500, 900)
	qt.app.processEvents()
	window.Result(11)
	w, h = win.picture.Pixels()
	assert win.picture.image.shape == (h, w, 3) and viewer.layout.tile[1] > tile[1]
	# z: the window to the view
	win.Key(ord("z"))
	qt.app.processEvents()
	window.Result(12)
	assert win.picture.Pixels() == viewer.Fit()


def test_the_keys_are_luc3ds(qt, window):
	Qt, QTest = qt.Qt, qt.QtTest.QTest
	win, viewer = window.win, window.viewer
	window.Result(9)
	def press(key):
		QTest.keyClick(win.picture, key)
		qt.app.processEvents()
	press(Qt.Key_V)
	assert viewer.Shown() == [0] and win.single.isChecked() and not win.grid.isChecked()
	press(Qt.Key_Down)
	assert viewer.Shown() == [1]
	press(Qt.Key_Right)
	press(Qt.Key_Right)
	assert viewer.Shown() == [0] # wrapped
	press(Qt.Key_Up)
	assert viewer.Shown() == [2]
	window.Result(10)
	assert len(viewer.cells) == 1 and viewer.sceneCell is not None # one camera, and the 3D beside it
	press(Qt.Key_Backslash)
	assert not viewer.showScene and not win.checks["\\"].isChecked()
	press(Qt.Key_Backslash)
	press(Qt.Key_G)
	assert viewer.Shown() == [0, 1, 2] and win.grid.isChecked()
	# The panel's buttons do what the keys do
	win.checks["d"].click()
	assert not viewer.show2d and not win.checks["d"].isChecked()
	win.single.click()
	assert viewer.Shown() == [2] # the camera last shown alone
	win.grid.click()
	assert viewer.Shown() == [0, 1, 2]
	# Paused (space): a key draws the same row again, at once
	window.Result(11)
	press(Qt.Key_Space)
	assert win.paused
	press(Qt.Key_V)
	assert len(viewer.cells) == 1 and int(viewer.last[1]["frame"]) == 11 and "Paused" in win.status.text()
	press(Qt.Key_Space)
	press(Qt.Key_Q)
	assert not win.isVisible()


def test_the_mouse_turns_and_zooms_the_3d(qt, window):
	Qt, QTest, QtCore, QtGui = qt.Qt, qt.QtTest.QTest, qt.QtCore, qt.QtGui
	win, viewer = window.win, window.viewer
	window.Result(9)
	scene = viewer.scene
	y, x, h, w = viewer.sceneCell
	ratio = win.picture.devicePixelRatioF()
	at = QtCore.QPoint(int((x + w / 2) / ratio), int((y + h / 2) / ratio))
	before = win.picture.image[y:y + h, x:x + w].copy()
	turn = scene.azimuth
	def mouse(kind, where, button, buttons): # QTest's moves send no event on the offscreen platform
		qt.app.sendEvent(win.picture, QtGui.QMouseEvent(kind, QtCore.QPointF(where), button, buttons, Qt.NoModifier))
	mouse(QtCore.QEvent.MouseButtonPress, at, Qt.LeftButton, Qt.LeftButton)
	mouse(QtCore.QEvent.MouseMove, at + QtCore.QPoint(40, 0), Qt.NoButton, Qt.LeftButton)
	mouse(QtCore.QEvent.MouseButtonRelease, at + QtCore.QPoint(40, 0), Qt.LeftButton, Qt.NoButton)
	assert scene.azimuth == pytest.approx(turn - 40 * ratio * 0.008)
	win.Step() # no new result: the 3D tile drawn again, turned
	assert (win.picture.image[y:y + h, x:x + w] != before).any()
	distance = scene.distance
	wheel = QtGui.QWheelEvent(QtCore.QPointF(at), QtCore.QPointF(win.picture.mapToGlobal(at)), QtCore.QPoint(0, 0),
		QtCore.QPoint(0, 120), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
	qt.app.sendEvent(win.picture, wheel)
	assert scene.distance == pytest.approx(distance * 0.85)
	# A click on a camera's tile picks it; a double click shows it alone
	c, y, x, h, w = viewer.cells[1]
	QTest.mouseDClick(win.picture, Qt.LeftButton, Qt.NoModifier, QtCore.QPoint(int((x + w / 2) / ratio),
		int((y + h / 2) / ratio)))
	assert viewer.current == 1 and viewer.Shown() == [1]


def test_the_panel_warns_in_red_and_shows_the_tracking_strip(qt, window):
	win, viewer = window.win, window.viewer
	viewer.tracking = liveview.livehealth.Tracking(3, 30.0, 20.0)
	window.Result(9)
	win.Say(viewer.last[1])
	assert win.alerts.isHidden() and not win.strips.isHidden()
	for frame in range(10, 20):
		window.Result(frame, bad=2)
	win.Say(viewer.last[1])
	assert not win.alerts.isHidden()
	text = win.alerts.text()
	assert "#2's error, 234 px, is far above the others'" in text and "#ff2828" in text # in red
	assert "Good 3D for 2 of 3 animals" in text
	assert "above 20 px, drawn dimmed" in win.status.text()
	# The strip: #2's newest columns red
	image = win.strips.pixmap().toImage()
	h, w = image.height(), image.width()
	strip = np.array([[image.pixelColor(x, y).getRgb()[:3] for x in range(w - 6, w - 1)]
		for y in range(2 * (liveview.STRIP_H + 6) + 4, 2 * (liveview.STRIP_H + 6) + liveview.STRIP_H)])
	assert like(strip[..., ::-1], liveview.WARN).all() # RGB as BGR
	assert viewer.panelMs and viewer.Summary(1.0)["qt_panel_ms"]["p50"] < 50


def test_without_qt_the_window_is_opencvs(qt, monkeypatch, capsys):
	# --gui auto without PyQt5: OpenCV's window (here, headless OpenCV has none: it says so)
	import campy
	monkeypatch.setitem(sys.modules, "campy.liveviewqt", None)
	monkeypatch.delattr(campy, "liveviewqt", raising=False)
	code = liveview.Run(liveview.ParseArgs(["--name", name(), "--priority", "normal", "--seconds", "0.2"]))
	said = capsys.readouterr().out
	assert "No Qt here" in said
	assert code in (0, 1) # 1: this OpenCV has no window support
	assert liveview.Run(liveview.ParseArgs(["--name", name(), "--priority", "normal", "--seconds", "0.2",
		"--gui", "qt"])) == 1 and "Qt cannot be loaded" in capsys.readouterr().out
