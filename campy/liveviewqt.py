"""
campy-live-view's window in Qt (PyQt5), as campy's preview windows
(campy/display.py): the view beside a panel of Qt's own checkboxes, buttons
and text.

The view is what campy/liveview.py's Viewer draws (the cameras' tiles and the
3D tile, from one row of campy-live's results), drawn at the size of the
widget showing it, one pixel for one, and shown as it is: Qt only shows it,
and passes the mouse and the keys to the Viewer. Its text (the tiles' names
and frames, the identities' labels, the 3D tile's header) is not drawn into
it with OpenCV's font: the Viewer hands it over (liveview.Words), and Qt
draws it over the image in its own fonts, sharp at any size, where OpenCV's
would be (the left end of each baseline), each item cut to its tile. The
panel says what the OpenCV window's drawn panel says, in Qt's fonts, a few
times a second.

The keys are the OpenCV window's (liveview.Viewer.Key), as LUC3D's: v one
camera alone, g every camera, the arrows the previous or next camera, \\ the
3D view on or off; the wheel, + and - zoom a camera, 0 the whole frame
again, shift 0 one frame pixel for one screen pixel; d, p, l what is drawn,
t the trails, o the occupancy; z the window to the view; space pause; s
save the view as a PNG (with its text); q or Esc quit.

campy-live-view uses this window when PyQt5 is there (--gui auto, the
default), or with --gui qt.
"""

import os, signal, sys, time, traceback

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

from campy import liveview
from campy.liveview import Say

Qt = QtCore.Qt
PANEL_WIDTH = 330 # px, the panel beside the view
PANEL_EVERY_S = 0.2 # the panel's text, said again this often
KEYS = {Qt.Key_Left: "left", Qt.Key_Right: "right", Qt.Key_Up: "up", Qt.Key_Down: "down", Qt.Key_Escape: 27}
BUTTONS = {Qt.LeftButton: "left", Qt.RightButton: "right", Qt.MiddleButton: "middle"}


FONT_CAP = 24 # px: the capital letters' height in OpenCV's font at scale 1 (Hershey simplex); Qt's are drawn as tall
_fonts = {}


def Font(size):
	# Qt's font whose capitals are as tall as OpenCV's at size (its font scale), in pixels
	if size not in _fonts:
		font = QtGui.QFont()
		font.setPixelSize(100)
		cap = QtGui.QFontMetricsF(font).capHeight() or 70
		font.setPixelSize(max(6, int(round(100 * FONT_CAP * size / cap))))
		_fonts[size] = font
	return _fonts[size]


def PaintWords(painter, items):
	# The view's text items (liveview.Words), on painter, in the image's pixels: each at the left end of
	# its baseline, on a dark box as liveview.Text draws it, cut to its tile
	right = 0
	for text, x, y, color, size, box, clip in items:
		font = Font(size)
		metrics = QtGui.QFontMetrics(font)
		x = right + 10 if x is None else x
		width = metrics.horizontalAdvance(text)
		painter.setClipRect(QtCore.QRect(*clip))
		if box:
			painter.fillRect(QtCore.QRect(x - 3, y - metrics.capHeight() - 3, width + 6,
				metrics.capHeight() + metrics.descent() + 4), QtGui.QColor(*liveview.BOX[::-1]))
		painter.setFont(font)
		painter.setPen(QtGui.QColor(*color[::-1]))
		painter.drawText(QtCore.QPoint(x, y), text)
		right = x + width
	painter.setClipping(False)


def Rendered(image, items):
	# image (BGR) with the text items drawn over it, as the window shows it (for the PNGs)
	h, w = image.shape[:2]
	picture = QtGui.QImage(np.ascontiguousarray(image).data, w, h, image.strides[0],
		QtGui.QImage.Format_BGR888).convertToFormat(QtGui.QImage.Format_RGB32)
	painter = QtGui.QPainter(picture)
	painter.setRenderHint(QtGui.QPainter.TextAntialiasing)
	PaintWords(painter, items)
	painter.end()
	bits = picture.constBits()
	bits.setsize(picture.bytesPerLine() * h)
	return np.frombuffer(bits, np.uint8).reshape(h, picture.bytesPerLine() // 4, 4)[:, :w, :3].copy() # B, G, R, x


def Html(lines):
	# The panel's (text, BGR colour) lines, as Qt's rich text
	def Escape(text):
		return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
	return "".join('<p style="margin: 0 0 6px 0; color: #{2:02x}{1:02x}{0:02x}">{3}</p>'.format(*color, Escape(text))
		for text, color in lines)


class Picture(QtWidgets.QWidget):
	"""The view: the Viewer's image, drawn at this widget's own pixels, and the mouse on it."""

	def __init__(self, window):
		super().__init__()
		self.window_ = window
		self.image = self.qimage = None
		self.words = [] # the image's text (liveview.Words' items), drawn over it
		self.setAttribute(Qt.WA_OpaquePaintEvent) # paintEvent covers every pixel
		self.setMouseTracking(True) # moves without a button too: where the wheel is
		self.setFocusPolicy(Qt.StrongFocus)
		self.setMinimumSize(320, 200)

	def Pixels(self):
		# The widget's size in the screen's pixels, width x height (Qt's are larger on a scaled screen)
		ratio = self.devicePixelRatioF()
		return max(1, int(round(self.width() * ratio))), max(1, int(round(self.height() * ratio)))

	def Show(self, image, words=None):
		self.words = words.Items() if words is not None else []
		self.image = np.ascontiguousarray(image)
		h, w = self.image.shape[:2]
		self.qimage = QtGui.QImage(self.image.data, w, h, self.image.strides[0], QtGui.QImage.Format_BGR888)
		self.qimage.setDevicePixelRatio(self.devicePixelRatioF())
		self.update()

	def paintEvent(self, event):
		painter = QtGui.QPainter(self)
		painter.fillRect(self.rect(), Qt.black)
		if self.qimage is not None:
			painter.drawImage(0, 0, self.qimage)
			start = time.perf_counter()
			ratio = self.devicePixelRatioF()
			painter.scale(1 / ratio, 1 / ratio) # the image's pixels
			painter.setRenderHint(QtGui.QPainter.TextAntialiasing)
			PaintWords(painter, self.words)
			self.window_.viewer.wordsMs.append((time.perf_counter() - start) * 1e3)
		painter.end()

	def At(self, event):
		ratio = self.devicePixelRatioF()
		return int(event.x() * ratio), int(event.y() * ratio)

	def mousePressEvent(self, event):
		button = BUTTONS.get(event.button())
		if button:
			self.window_.viewer.Press(*self.At(event), button)
			self.window_.Changed()

	def mouseMoveEvent(self, event):
		self.window_.viewer.Move(*self.At(event))
		if self.window_.viewer.panned and self.window_.paused:
			self.window_.Redraw() # a camera's picture, moved

	def mouseReleaseEvent(self, event):
		self.window_.viewer.Release()

	def mouseDoubleClickEvent(self, event):
		if event.button() == Qt.LeftButton:
			self.window_.viewer.DoubleClick(*self.At(event))
			self.window_.Changed()

	def wheelEvent(self, event):
		self.window_.viewer.Wheel(event.angleDelta().y() / 120, *self.At(event))
		self.window_.Changed()


class Window(QtWidgets.QWidget):
	"""
	The view (Picture) and its panel. Tick, every --poll-ms, makes the next
	view (Viewer.Step) and shows it; a key, a button or the mouse changes
	what is drawn, at once when paused.
	"""

	def __init__(self, viewer, args):
		super().__init__()
		self.viewer, self.args = viewer, args
		viewer.textOver = True # the view's text, drawn over it in Qt's fonts
		viewer.render = lambda image: Rendered(image, viewer.words.Items())
		self.setWindowTitle(liveview.TITLE)
		self.paused = False
		self.raised = None # whether there was a feed when it was last put in front
		self.saidAt = 0.0 # when the panel's text was last said
		self.saidRow = None # whether it was of a row
		self.failed = None # what went wrong in Tick, which Qt would not say
		self.start = time.monotonic()
		self.picture = Picture(self)
		panel = QtWidgets.QWidget()
		panel.setFixedWidth(PANEL_WIDTH)
		panel.setStyleSheet("QWidget { background: #181818; color: #e6e6e6; font-size: 12px; } "
			"QPushButton { background: #2a2a2a; border: 1px solid #555; padding: 4px 8px; } "
			"QPushButton:checked { background: #14466e; border-color: #e6e6e6; }")
		column = QtWidgets.QVBoxLayout(panel)
		self.checks = {}
		for label, key in (("Predictions (d)", "d"), ("Reprojections (p)", "p"), ("Lines (l)", "l"),
				("3D view (\\)", "\\"), ("Trails (t)", "t"), ("Occupancy (o)", "o")):
			box = QtWidgets.QCheckBox(label)
			box.setFocusPolicy(Qt.NoFocus) # space and the arrows stay the view's
			box.clicked.connect(lambda checked, key=key: self.Key(ord(key)))
			column.addWidget(box)
			self.checks[key] = box
		views = QtWidgets.QHBoxLayout()
		self.grid = QtWidgets.QPushButton("Grid (g)")
		self.single = QtWidgets.QPushButton("Single (v)")
		previous, following = QtWidgets.QPushButton("▲"), QtWidgets.QPushButton("▼")
		previous.setToolTip("The previous camera (up or left arrow)")
		following.setToolTip("The next camera (down or right arrow)")
		for button, key in ((self.grid, ord("g")), (self.single, ord("v")), (previous, "up"), (following, "down")):
			button.setFocusPolicy(Qt.NoFocus)
			button.clicked.connect(lambda checked, key=key: self.Key(key))
			views.addWidget(button)
		self.grid.setCheckable(True)
		self.single.setCheckable(True)
		column.addLayout(views)
		self.status = QtWidgets.QLabel()
		self.status.setWordWrap(True)
		self.status.setTextFormat(Qt.RichText)
		self.status.setAlignment(Qt.AlignTop | Qt.AlignLeft)
		column.addWidget(self.status, 1)
		row = QtWidgets.QHBoxLayout(self)
		row.setContentsMargins(0, 0, 0, 0)
		row.setSpacing(0)
		row.addWidget(self.picture, 1)
		row.addWidget(panel)
		self.Sync()
		self.timer = QtCore.QTimer(self)
		self.timer.setTimerType(Qt.PreciseTimer) # Windows' coarse timers tick every 15.6 ms
		self.timer.timeout.connect(self.Tick)
		self.timer.start(max(1, int(round(args.poll_ms))))

	# -- each tick

	def Tick(self):
		try:
			self.Step()
		except Exception:
			# Qt ends the program on an error in a slot, without a word: say it, and stop
			self.failed = traceback.format_exc()
			Say("campy-live-view stopped:\n" + self.failed)
			self.close()

	def Step(self):
		viewer = self.viewer
		if self.args.seconds and time.monotonic() - self.start >= self.args.seconds:
			self.close()
			return
		size = self.picture.Pixels()
		if size != viewer.windowSize:
			viewer.windowSize = size
			if self.paused or viewer.readers is None:
				self.Redraw() # drawn again at the new size
		got = None if self.paused else viewer.Step()
		if got is not None:
			image, frame, row = got
			self.picture.Show(image, viewer.words)
			feed = viewer.readers is not None
			if self.raised is None or (feed and not self.raised):
				self.Raise() # in front when it opens, and when a recording's frames come
			self.raised = feed
			viewer.Delivered(image, frame, row)
			if time.monotonic() - self.saidAt >= PANEL_EVERY_S or (row is not None) != self.saidRow:
				self.Say(row)
		elif viewer.moved and self.picture.image is not None and viewer.sceneCell is not None \
				and getattr(viewer, "last", None):
			# Turned: the 3D tile again, of the row the cameras' tiles show
			viewer.Draw3D(self.picture.image, viewer.last[1], viewer.last[0][0].frameNumber)
			self.picture.words = viewer.words.Items() if viewer.words is not None else []
			self.picture.update()

	def Redraw(self):
		# The view again (the same frames and row): after a change, while paused or waiting
		viewer = self.viewer
		if viewer.readers is None:
			self.picture.Show(viewer.Waiting(), viewer.words)
			self.Say(None)
		else:
			image = viewer.Again()
			if image is not None:
				self.picture.Show(image, viewer.words)
				self.Say(viewer.last[1])

	def Say(self, row):
		self.saidAt, self.saidRow = time.monotonic(), row is not None
		self.Sync() # the 3D view's box, once campy-live has said its calibration
		lines = self.viewer.PanelText(row)
		if self.paused:
			lines.insert(0, ("Paused: space goes on", liveview.TEXT))
		self.status.setText(Html(lines))

	def Sync(self):
		# The panel's buttons as the viewer is
		viewer = self.viewer
		for key, on in (("d", viewer.show2d), ("p", viewer.show3d), ("l", viewer.lines), ("\\", viewer.showScene),
				("t", viewer.trails), ("o", viewer.occupancy)):
			self.checks[key].setChecked(on)
		for key in "\\to":
			self.checks[key].setVisible(viewer.scene is not None)
		self.grid.setChecked(viewer.selected is None)
		self.single.setChecked(viewer.selected is not None)

	def Changed(self):
		# What is drawn changed (a key, a button, a click)
		self.Sync()
		if self.paused:
			self.Redraw()

	# -- keys

	def Key(self, key):
		if key == ord(" "):
			self.paused = not self.paused
		elif key == ord("z"):
			self.Fit()
		elif key == ord("s"):
			if self.picture.image is not None:
				self.viewer.Save(self.picture.image)
		elif not self.viewer.Key(key):
			self.close()
			return
		self.Changed()

	def keyPressEvent(self, event):
		key = KEYS.get(event.key())
		if event.modifiers() & Qt.ShiftModifier and event.nativeVirtualKey() == 0x30:
			key = ord(")") # shift 0 on Windows, whatever the keyboard's layout makes of it
		if key is None:
			text = event.text()
			if len(text) != 1:
				return super().keyPressEvent(event)
			key = ord(text.lower())
		self.Key(key)

	def Fit(self):
		# z: the window to the view, without the black margins
		fit = self.viewer.Fit()
		if fit:
			ratio = self.picture.devicePixelRatioF()
			w, h = int(round(fit[0] / ratio)), int(round(fit[1] / ratio))
			self.resize(self.width() + w - self.picture.width(), self.height() + h - self.picture.height())

	def Raise(self):
		# Back from minimized, and in front: a program started from a script may not take the focus on
		# Windows, but a window put on top, then not, stays in front
		if self.windowState() & Qt.WindowMinimized:
			self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
		self.show()
		self.raise_()
		self.activateWindow()
		if os.name == "nt":
			try:
				import ctypes
				user32 = ctypes.windll.user32
				user32.IsIconic.argtypes = [ctypes.c_void_p]
				user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
				user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_int] * 4 + [ctypes.c_uint]
				hwnd = int(self.winId())
				if user32.IsIconic(hwnd):
					# Minimized by Windows itself (a program started with -WindowStyle Minimized shows its first
					# window so), which Qt does not know of
					user32.ShowWindow(hwnd, 9) # SW_RESTORE
				for after in (-1, -2): # HWND_TOPMOST, then HWND_NOTOPMOST; SWP_NOMOVE | SWP_NOSIZE
					user32.SetWindowPos(hwnd, after, 0, 0, 0, 0, 0x3)
			except Exception:
				pass

	def closeEvent(self, event):
		self.timer.stop()
		QtWidgets.QApplication.instance().quit()
		super().closeEvent(event)


def Run(args):
	app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([sys.argv[0]])
	viewer = liveview.Viewer(args)
	viewer.panel = False # the window's panel says it
	window = Window(viewer, args)
	width, height = args.size
	window.resize(max(400, width - PANEL_WIDTH) + PANEL_WIDTH, height)
	window.show()
	# Ctrl+C: Python takes it when it next runs, at the next tick; Qt would not stop for it
	previous = signal.signal(signal.SIGINT, lambda *a: window.close())
	Say("Showing {}; results from {} (Qt's window).".format(", ".join(viewer.ringNames), viewer.resultsName))
	try:
		app.exec_()
	finally:
		signal.signal(signal.SIGINT, previous)
		viewer.Close()
	done = liveview.Finish(viewer, time.monotonic() - window.start, args)
	return 1 if window.failed else done
