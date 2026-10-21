"""
campy-live-view's Qt window (campy/liveviewqt.py), on Qt's offscreen
platform: no window opens. The view in it is the Viewer's (tests/
test_liveview.py checks what that draws); these check that the window shows
it at its own size, and passes it the keys, the buttons and the mouse.
"""

import json, os, subprocess, sys, types

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
pytest.importorskip("PyQt5")

from campy import live, livefeed, liveresults, liveview

import test_pose3d as scene3d
from test_liveview import EDGES, bookmarks, like, mice, row_for, scaled, view_args
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
def window(qt, rings, tmp_path):
	# The Qt window on a feed of 3 cameras (frames 1-12, gray), whose recording is in tmp_path, and campy-live's
	# results ring
	cams, truth, keypoints, scores, order = mice()
	prefix = name()
	recording = tmp_path / "2026-10-20_16-54-14"
	recording.mkdir()
	writers = [rings(livefeed.RingName(prefix, c), (256, 320), np.uint8, slots=8, cameraName="Camera{}".format(c),
		description=json.dumps({"kind": livefeed.CAMERA_KIND, "folder": str(recording / "Camera{}".format(c))}))
		for c in range(3)]
	for n in range(1, 13):
		for c, w in enumerate(writers):
			w.Write(np.full((256, 320), 60, np.uint8), n, 1000 * n + c)
	publisher = liveresults.Publisher(liveresults.RingName(prefix), live.RowType(3, 3, truth.shape[1], 3),
		cameras=["Camera0", "Camera1", "Camera2"], rings=[livefeed.RingName(prefix, c) for c in range(3)],
		edges=EDGES, animals=3)
	args = view_args(prefix, "--gui", "qt", "--bookmarks", str(tmp_path / "elsewhere.csv"))
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
	yield types.SimpleNamespace(win=win, viewer=viewer, Result=Result, truth=truth, recording=recording,
		writers=writers, publisher=publisher)
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
	# z: the window to the view. The view of a 1500 x 900 window is wider than the screen (Qt's offscreen one:
	# 800 x 600), so the window is as large as fits, the view's shape kept: within a pixel or two of the view
	# drawn at that size, then, z again, to it
	win.Key(ord("z"))
	qt.app.processEvents()
	window.Result(12)
	assert OnScreen(qt, win)
	assert np.abs(np.subtract(win.picture.Pixels(), viewer.Fit())).max() <= 3
	win.Key(ord("z"))
	qt.app.processEvents()
	window.Result(13)
	assert win.picture.Pixels() == viewer.Fit()


def OnScreen(qt, win):
	# The window, its title bar and borders too, inside the screen's free part (not under the taskbar)
	return win.screen().availableGeometry().contains(win.frameGeometry())


def test_the_window_opens_inside_the_screen(qt, window):
	# --size (1800 x 960 by default) is more than the screen has (on the rig's RDP session: 1512 x 900 free;
	# Qt's offscreen screen: 800 x 600): the window opens as large as fits, not under the taskbar
	win = window.win
	win.move(300, 200)
	win.OnScreen(1800, 960)
	qt.app.processEvents()
	free = win.screen().availableGeometry()
	assert OnScreen(qt, win) and win.frameGeometry().size() == free.size()
	win.OnScreen(700, 400) # it fits: as asked
	qt.app.processEvents()
	assert (win.width(), win.height()) == (700, 400) and OnScreen(qt, win)


def test_the_keys_are_luc3ds(qt, window):
	Qt, QTest = qt.Qt, qt.QtTest.QTest
	win, viewer = window.win, window.viewer
	window.Result(9)
	# The panel's checkboxes, in two columns: d and r a row, l and \\ the next, t and o the next
	at = {key: box.geometry() for key, box in win.checks.items()}
	for left, right in (("d", "r"), ("l", "\\"), ("t", "o")):
		assert at[left].y() == at[right].y() and at[left].right() < at[right].x()
	assert at["d"].bottom() < at["l"].y() < at["t"].y() and at["d"].x() == at["l"].x() == at["t"].x()
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


def test_bookmarks_from_the_panel_and_the_keys(qt, window):
	Qt, QTest = qt.Qt, qt.QtTest.QTest
	win, viewer = window.win, window.viewer
	window.Result(9)
	path = window.recording / "bookmarks.csv"
	assert viewer.recording == str(window.recording)
	# A note typed in the panel: its keys are the note's, not the view's (m would bookmark, s save, q quit)
	QTest.keyClicks(win.note, "mouse squeaks")
	assert win.note.text() == "mouse squeaks" and not path.exists() and win.isVisible()
	win.mark.click()
	marks = bookmarks(path) # written at once, the window still open
	assert [(m["number"], m["event"], m["note"], m["frame"]) for m in marks] == [("1", "mark", "mouse squeaks", "9")]
	assert [(marks[0]["frame_{}".format(c)], marks[0]["host_ns_{}".format(c)]) for c in range(3)] == \
		[("12", "12000"), ("12", "12001"), ("12", "12002")]
	assert win.note.text() == "" # the note goes with its bookmark
	assert "Bookmark 1 at" in win.marks.text() and "mouse squeaks" in win.marks.text()
	# The keys: m, Shift+2 (@ on a US keyboard); the panel's event buttons; Enter in the note
	QTest.keyClick(win.picture, Qt.Key_M)
	QTest.keyClick(win.picture, "@", Qt.ShiftModifier)
	assert viewer.Shown() == [0, 1, 2] # not camera 2 alone
	win.events[4].click()
	QTest.keyClicks(win.note, "rears")
	QTest.keyClick(win.note, Qt.Key_Return)
	marks = bookmarks(path)
	assert [(m["number"], m["event"], m["note"]) for m in marks] == [("1", "mark", "mouse squeaks"),
		("2", "mark", ""), ("3", "2", ""), ("4", "5", ""), ("5", "mark", "rears")]
	text = win.marks.text()
	assert text.index("Bookmark 5 at") < text.index("Bookmark 4 at") # the newest first
	# Esc in the note leaves it, cleared, and does not quit
	QTest.keyClicks(win.note, "oops")
	QTest.keyClick(win.note, Qt.Key_Escape)
	assert win.isVisible() and win.note.text() == "" and len(bookmarks(path)) == 5
	# Paused: the view is outlined at once, and drawn again without it once it is over
	window.Result(10)
	QTest.keyClick(win.picture, Qt.Key_Space)
	assert win.paused
	QTest.keyClick(win.picture, Qt.Key_M)
	oy, ox = viewer.origin
	h, w = viewer.layout.size
	assert like(win.picture.image[oy:oy + h, ox + 3], liveview.MARKED).mean() > 0.9
	assert bookmarks(path)[-1]["frame"] == "10"
	viewer.flashUntil = 0
	win.Step()
	assert not like(win.picture.image[oy:oy + h, ox + 3], liveview.MARKED).any()


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
	# (the fixture's frames were all written at once: the rig may say they stopped; its own tests are elsewhere)
	assert "far above" not in win.alerts.text() and not win.strips.isHidden()
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
	# The recording over (its feed and campy-live ended): no strip, and no results a second, while it waits
	window.publisher.Close()
	for w in window.writers:
		w.Close()
	viewer.Check()
	win.Redraw()
	assert viewer.readers is None and win.strips.isHidden() and "campy-live: 0.0 results/s" in win.status.text()


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


def test_the_labels_are_qts_text_over_the_view(qt, window):
	# The tiles' text is not drawn into the image with OpenCV's font: the
	# Viewer hands it to the window, which draws it over the image in Qt's,
	# where OpenCV's would be (the left end of each baseline), cut to its tile
	win, viewer = window.win, window.viewer
	PAD = liveview.PAD
	window.Result(9)
	def header(items, k):
		return next(item for item in items if item[0].startswith("{}  Camera{}  frame 9".format(k + 1, k)))
	items = win.picture.words
	for k, (c, y, x, h, w) in enumerate(viewer.cells):
		text, X, Y, color, size, box, clip = header(items, k)
		assert (X, Y) == (x + PAD, y + PAD + 12) and clip == (x, y, w, h) and box and color == liveview.TEXT
	labels = [item for item in items if item[0].startswith("#")]
	assert {item[3] for item in labels} == {liveview.Color(a) for a in range(3)} # on the cameras and in 3D
	y, x, h, w = viewer.sceneCell
	assert any(item[0].startswith("3D  frame 9") and item[1:3] == (x + PAD, y + PAD + 12) for item in items)
	assert any(item[0].startswith("grid squares") for item in items) # the 3D tile's background's text
	# Not pixels of the image: camera 0's header is the frame's gray there; Qt's text is on the widget
	c, y, x, h, w = viewer.cells[0]
	region = (slice(y + 4, y + PAD + 16), slice(x + PAD, x + PAD + 120))
	assert (win.picture.image[region] == 60).all()
	# (Qt's offscreen platform on Windows has no fonts, and draws no text: there, the window's own platform does)
	if qt.QtGui.QFontDatabase().families():
		grab = win.picture.grab().toImage().convertToFormat(qt.QtGui.QImage.Format_RGB32)
		bits = grab.constBits()
		bits.setsize(grab.bytesPerLine() * grab.height())
		shown = np.frombuffer(bits, np.uint8).reshape(grab.height(), grab.bytesPerLine() // 4, 4)[..., :3]
		if win.picture.devicePixelRatioF() == 1:
			assert like(shown[region], liveview.TEXT).sum() > 20 and (shown[region] == liveview.BOX).all(-1).sum() > 100
		# The saved views have it too
		rendered = qt.liveviewqt.Rendered(win.picture.image, items)
		assert like(rendered[region], liveview.TEXT).sum() > 20
	# OpenCV's window draws its own into the image, at the same place
	viewer.textOver = False
	opencv = viewer.Again()
	assert viewer.words is None and like(opencv[region], liveview.TEXT).sum() > 20
	viewer.textOver = True
	# Smaller: the items follow the tiles
	win.resize(760, 420)
	qt.app.processEvents()
	window.Result(10)
	c, y, x, h, w = viewer.cells[1]
	text, X, Y, color, size, box, clip = next(item for item in win.picture.words if item[0].startswith("2  Camera1  frame 10"))
	assert (X, Y) == (x + PAD, y + PAD + 12) and clip == (x, y, w, h)
	# Turned (the 3D tile drawn again alone): its items replaced, the cameras' kept
	y, x, h, w = viewer.sceneCell
	def cameras(): # the items cut to the cameras' tiles
		return [item for item in win.picture.words if item[6] != (x, y, w, h)]
	before, scene = cameras(), [item for item in win.picture.words if item[6] == (x, y, w, h)]
	viewer.scene.Orbit(30, 0)
	viewer.moved = True
	win.Step()
	assert cameras() == before and sum(item[0].startswith("3D  frame") for item in win.picture.words) == 1
	assert [item for item in win.picture.words if item[6] == (x, y, w, h)] != scene # its labels moved


def test_the_health_badge_and_labels_are_qts_text_apart(qt, window):
	# On small tiles (2026-10-21, the ChronicRecord PC over RDP), the tile's badge (camera 1 a frame ahead:
	# white on red) is Qt's text too, under the name line where Qt's font has no room for both at its
	# height; nothing covers another, measured in Qt's font
	win, viewer = window.win, window.viewer
	status = viewer.rig.Status
	def Ahead(now=None):
		s = status(now)
		s.cameras[1].problems = [("1 FRAME AHEAD", "Camera1: 1 frame ahead of the other cameras")]
		for k in (0, 2): # the others fine (the test's feed is still: on a slow machine, NO FRAMES)
			s.cameras[k].problems, s.cameras[k].fps = [], 30.0
		return s
	viewer.rig.Status = Ahead
	win.resize(760, 420)
	qt.app.processEvents()
	window.Result(9)
	items, image = win.picture.words, win.picture.image
	qtFont = types.SimpleNamespace(measure=qt.liveviewqt.Measure)
	c, y, x, h, w = viewer.cells[1]
	badge = next(item for item in items if item[0] == "1 FRAME AHEAD")
	text, X, Y, color, size, box, clip = badge
	assert (color, box, clip) == ((255, 255, 255), liveview.WARN, (x, y, w, h))
	x0, y0, x1, y1 = liveview.TextBox(text, (X, Y), size, qtFont)
	assert x1 == x + w - liveview.PAD - 1 # at the tile's top right
	assert not like(image[y0:y1, x0:x1], liveview.WARN).any() # not drawn into the image
	assert like(image[y:y + h, x:x + w], liveview.WARN).sum() > 200 # the red frame round the tile is
	for c, y, x, h, w in viewer.cells:
		mine = [item for item in items if item[6] == (x, y, w, h)]
		boxes = [liveview.TextBox(text, (X, Y), size, qtFont) for text, X, Y, color, size, box, clip in mine]
		assert any(item[0].startswith("#") for item in mine) and any(item[0].endswith("fps") or item is badge
			for item in mine)
		assert not any(liveview.Over(a, b) for k, a in enumerate(boxes) for b in boxes[:k])
	# (Qt's offscreen platform on Windows has no fonts, and draws no text: there, the window's own platform does)
	if qt.QtGui.QFontDatabase().families():
		header = next(item for item in items if item[0].startswith("2  Camera1  frame 9"))
		if liveview.TextBox(header[0], header[1:3], header[4], qtFont)[2] + 6 > x0:
			assert y0 > liveview.TextBox(header[0], header[1:3], header[4], qtFont)[3] # under it
		rendered = qt.liveviewqt.Rendered(image, items)
		assert like(rendered[y0:y1, x0:x1], liveview.WARN).sum() > 100 and (rendered[y0:y1, x0:x1].min(-1) > 200).any()


def test_the_wheel_and_keys_zoom_a_camera(qt, window):
	Qt, QTest, QtCore, QtGui = qt.Qt, qt.QtTest.QTest, qt.QtCore, qt.QtGui
	win, viewer = window.win, window.viewer
	window.Result(9)
	ratio = win.picture.devicePixelRatioF()
	c, y, x, h, w = viewer.cells[1]
	at = QtCore.QPoint(int((x + w / 2) / ratio), int((y + h / 2) / ratio))
	wheel = QtGui.QWheelEvent(QtCore.QPointF(at), QtCore.QPointF(win.picture.mapToGlobal(at)), QtCore.QPoint(0, 0),
		QtCore.QPoint(0, 240), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
	qt.app.sendEvent(win.picture, wheel)
	assert viewer.ZoomOf(1).times == pytest.approx(liveview.ZOOM_WHEEL ** 2) and not viewer.ZoomOf(0).Zoomed()
	window.Result(10)
	assert any("zoom 1.2x" in item[0] for item in win.picture.words)
	# Paused, a drag moves the picture at once
	QTest.keyClick(win.picture, Qt.Key_Space)
	before = win.picture.image.copy()
	def mouse(kind, where, button, buttons):
		qt.app.sendEvent(win.picture, QtGui.QMouseEvent(kind, QtCore.QPointF(where), button, buttons, Qt.NoModifier))
	mouse(QtCore.QEvent.MouseButtonPress, at, Qt.LeftButton, Qt.LeftButton)
	mouse(QtCore.QEvent.MouseMove, at + QtCore.QPoint(30, 0), Qt.NoButton, Qt.LeftButton)
	mouse(QtCore.QEvent.MouseButtonRelease, at + QtCore.QPoint(30, 0), Qt.LeftButton, Qt.NoButton)
	assert (win.picture.image[y:y + h, x:x + w] != before[y:y + h, x:x + w]).any()
	assert int(viewer.last[1]["frame"]) == 10 # the same row
	QTest.keyClick(win.picture, Qt.Key_Space)
	# + and -, 0, shift 0 (as a US keyboard types it)
	QTest.keyClick(win.picture, "+")
	assert viewer.ZoomOf(0).times == pytest.approx(1.2)
	QTest.keyClick(win.picture, "-")
	assert viewer.ZoomOf(0).times == pytest.approx(1)
	QTest.keyClick(win.picture, ")")
	assert all(viewer.ZoomOf(k).native for k in range(3))
	QTest.keyClick(win.picture, "0")
	assert not any(z.Zoomed() for z in viewer.zooms.values()) and viewer.Shown() == [0, 1, 2]
	# t and o: the trails and the occupancy, and their boxes
	QTest.keyClick(win.picture, "o")
	assert viewer.occupancy and win.checks["o"].isChecked()
	win.checks["t"].click()
	assert not viewer.trails and not win.checks["t"].isChecked()


def test_the_cameras_trails_length_and_shade_from_the_panel(qt, window):
	# LUC3D's: Off, a quarter, half, 1 or 2 seconds (a quarter at first), and its shades (dark at first)
	win, viewer = window.win, window.viewer
	window.Result(9)
	win.Sync()
	length, shade = win.trailLength, win.trailShade
	assert [length.itemText(k) for k in range(length.count())] == ["Off", "¼ s", "½ s", "1 s", "2 s"]
	assert length.currentText() == "¼ s" and shade.currentText() == "Dark" and length.isVisible()
	def pick(box, text):
		box.setCurrentIndex(box.findText(text))
		box.activated.emit(box.currentIndex())
		qt.app.processEvents()
	pick(length, "1 s")
	pick(shade, "Very light")
	assert (viewer.cameraTrails.seconds, viewer.cameraTrails.shade) == (1.0, "very-light")
	assert viewer.cameraTrails.Frames() == 30 # at the feed's 30 fps
	pick(length, "Off")
	assert viewer.cameraTrails.seconds == 0
	# Another length from the command line: listed too
	viewer.cameraTrails.seconds = 0.3
	win.Sync()
	assert length.currentText() == "0.3 s"
