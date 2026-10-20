"""
campy-live-view: the cameras' live frames, with campy-live's results drawn on
them, as campy records.

    campy-live-view                      every camera, in a grid
    campy-live-view --camera 0           camera 0 alone
    campy-live-view --no-window --png views --png-every 30 --seconds 20

Start it before or after campy-acquire (liveFeed True) and campy-live: it
waits for them, and stays open from one recording to the next (with
--no-window it ends with the recording). It runs in campy-live's Python
environment (numpy, scipy and OpenCV), as a program of its own, at a lower
priority (--priority): it reads campy-acquire's frames (campy-live-0, ...;
campy/livefeed.py) and campy-live's results (campy-live-results;
campy/liveresults.py) from shared memory, and neither of them waits for it.

Each result is drawn on the frames it was made from. The view takes the
newest row campy-live has made, then reads that row's frame number back from
each camera's feed, which holds the newest 8 frames (0.27 s at 30 fps; a row
comes about 31 ms after its frames). A row whose frames are no longer there
is counted (gone) and not drawn. When no row has come for half a second
(campy-live not running, or far behind), the view shows the newest frames
alone, and says so.

What it draws on each camera's frame
------------------------------------
- 3D (campy-live with --calibration): each animal's 3D pose, projected into
  the camera through the calibration (lens distortion included): filled dots
  and thick lines, one colour per identity. Its label: the identity, how many
  cameras it was triangulated from, and how far, in this camera, its
  projection is from the 2D it was made from (px, mean over the nodes).
- 2D: what the model found in this camera: open circles and thin lines, in
  the colour of the identity campy-live matched them to, white when it
  matched them to none.
- A line from each matched detection's centre to its identity's projected
  centre.
- At the foot of the image, the identities whose 3D lands nowhere on it.
When campy-live matched an animal right, its open circles sit on its dots. A
long line, or a mouse whose open circles are one colour and whose dots
another, is a wrong match.

The panel lists each identity's cameras and reprojection error (campy-live's:
mean over nodes and cameras, undistorted px), and how late the results and
the view are.

Keys, in its window: 1-9 that camera alone; 0 or a every camera; d the 2D, p
the 3D, l the lines on or off; space pause; s save the view as a PNG (into
--png, or the current folder); q or Esc quit.
"""

import argparse, json, os, sys, time
from collections import deque
from datetime import datetime

import numpy as np
import cv2

from campy import livefeed, liveresults

TITLE = "campy-live view"

# One colour per identity (BGR): bright, apart, and drawn over a black outline,
# so they show on white mice and on dark bedding alike
COLORS = [(0, 165, 255), (255, 191, 0), (255, 0, 255), (0, 255, 0), (0, 255, 255), (255, 80, 80),
	(80, 80, 255), (255, 255, 0), (147, 20, 255), (0, 140, 140)] # orange, sky, magenta, green, yellow, blue, red, cyan, pink, olive
UNMATCHED = (255, 255, 255)
OUTLINE = (0, 0, 0)
TEXT = (230, 230, 230)
DIM = (150, 150, 150)
BOX = (25, 25, 25)
SHIFT = 4 # cv2's subpixel bits
RADIUS_2D, RADIUS_3D = 4, 2 # px: a detection's open circles, a 3D node's dot
FONT = cv2.FONT_HERSHEY_SIMPLEX
LINE = 20 # px between the panel's lines
PAD = 8
STALE_S = 0.5 # no row for this long: the frames alone
KEPT = 36000 # the timings kept for the summary: the last 20 minutes at 30 a second


def Say(text):
	print("[{}] {}".format(datetime.now().strftime("%H:%M:%S"), text), flush=True)


def Color(identity):
	return COLORS[int(identity) % len(COLORS)]


# ---------------------------------------------------------------- drawing

def Project(camera, X):
	"""World points (N, 3) -> pixels (N, 2) in the camera's image, NaN behind it."""
	X = np.asarray(X, float)
	with np.errstate(invalid="ignore", divide="ignore"):
		px = camera.Project(X)
		depth = X @ camera.R[2] + camera.t[2]
	px[~(depth > 0)] = np.nan
	return px


def Points(px, scale):
	# Pixels of the frame (N, 2) -> which are drawable, and where in the tile, with SHIFT subpixel bits
	px = np.asarray(px, float) * scale
	ok = np.isfinite(px).all(-1) & (np.abs(px) < 1e5).all(-1)
	q = np.zeros(px.shape, np.int64)
	q[ok] = np.round(px[ok] * (1 << SHIFT))
	return ok, [tuple(int(v) for v in p) for p in q]


def Centre(px):
	# The mean of the nodes there are, or None
	px = np.asarray(px, float)
	ok = np.isfinite(px).all(-1)
	return px[ok].mean(0) if ok.any() else None


def DrawSkeleton(img, px, scale, edges, color, radius, thickness, filled):
	"""An animal's nodes (N, 2, pixels of the frame) and edges, on img (a tile of the frame at scale)."""
	ok, pts = Points(px, scale)
	lines = [(a, b) for a, b in (edges or ()) if a < len(ok) and b < len(ok) and ok[a] and ok[b]]
	nodes = np.nonzero(ok)[0]
	for col, extra in ((OUTLINE, 2), (color, 0)): # the outline under the colour
		for a, b in lines:
			cv2.line(img, pts[a], pts[b], col, thickness + extra, cv2.LINE_AA, SHIFT)
		for i in nodes:
			if filled:
				cv2.circle(img, pts[i], (radius + extra // 2) << SHIFT, col, -1, cv2.LINE_AA, SHIFT)
			else:
				cv2.circle(img, pts[i], radius << SHIFT, col, thickness + extra, cv2.LINE_AA, SHIFT)


def Text(img, text, at, color=TEXT, size=0.5, box=True):
	# Text at (x, y) of its baseline's left end, on a dark box (an outline drawn thicker than the
	# text comes out wider than it in OpenCV 5's font, and off to one side)
	x, y = int(at[0]), int(at[1])
	if box:
		(w, h), base = cv2.getTextSize(text, FONT, size, 1)
		cv2.rectangle(img, (x - 3, y - h - 3), (x + w + 3, y + base + 1), BOX, -1)
	cv2.putText(img, text, (x, y), FONT, size, color, 1, cv2.LINE_AA)


def Wrap(text, width, size):
	# text in lines no wider than width px
	lines, line = [], ""
	for word in text.split(" "):
		trial = (line + " " + word) if line else word
		if line and cv2.getTextSize(trial, FONT, size, 1)[0][0] > width:
			lines.append(line)
			line = word
		else:
			line = trial
	return lines + [line]


def Has3D(row):
	return row is not None and "points3d" in row.dtype.names


def DrawCamera(img, scale, row, c, camera=None, edges=None, show2d=True, show3d=True, lines=True):
	"""
	Camera c's part of a results row on img, its frame at scale (a number, or
	x and y): its 2D detections, and with camera (its pose3d.Camera) each
	animal's 3D projected into it. Returns {identity: mean px, in this
	camera, between its projected 3D and the detection it was made from
	(NaN: no detection here)} for the identities with 3D.
	"""
	scale = np.broadcast_to(np.asarray(scale, float), (2,))
	kp = np.asarray(row["keypoints"][c], float) # (instances, nodes, 2)
	found = [i for i in range(len(kp)) if np.isfinite(kp[i]).all(-1).any()]
	members = np.asarray(row["members"][:, c]) if Has3D(row) else np.zeros(0, int)
	owner = {int(i): a for a, i in enumerate(members) if 0 <= i < len(kp)}
	projected = {}
	if Has3D(row) and camera is not None:
		for a, X in enumerate(np.asarray(row["points3d"], float)):
			if np.isfinite(X).all(-1).any():
				projected[a] = Project(camera, X)
	if show2d: # under the 3D
		for i in found:
			DrawSkeleton(img, kp[i], scale, edges, Color(owner[i]) if i in owner else UNMATCHED, radius=RADIUS_2D,
				thickness=1, filled=False)
	offsets = {}
	for a, px in projected.items():
		i = int(members[a])
		d = np.linalg.norm(px - kp[i], axis=-1) if 0 <= i < len(kp) else np.full(len(px), np.nan)
		offsets[a] = float(np.nanmean(d)) if np.isfinite(d).any() else float("nan")
		if lines and show2d and show3d and 0 <= i < len(kp):
			a2, a3 = Centre(kp[i]), Centre(px)
			if a2 is not None and a3 is not None:
				ok, (p2, p3) = Points(np.stack([a2, a3]), scale)
				if ok.all():
					cv2.line(img, p2, p3, OUTLINE, 3, cv2.LINE_AA, SHIFT)
					cv2.line(img, p2, p3, Color(a), 1, cv2.LINE_AA, SHIFT)
	if show3d:
		for a, px in projected.items():
			DrawSkeleton(img, px, scale, edges, Color(a), radius=RADIUS_3D, thickness=2, filled=True)
		away = [] # 3D that lands nowhere on this image (or behind the camera)
		for a, px in projected.items():
			p = np.asarray(px, float) * scale
			with np.errstate(invalid="ignore"):
				ok = np.isfinite(p).all(-1) & (p >= 0).all(-1) & (p[:, 0] < img.shape[1]) & (p[:, 1] < img.shape[0])
			if not ok.any():
				away.append(a)
				continue
			p = p[ok]
			label = "#{}".format(a)
			if np.isfinite(offsets[a]):
				label += "  {:.0f} px".format(offsets[a])
			views = int(row["n_views"][a])
			if views < len(row["members"][a]): # not every camera
				label += "  ({} cam{})".format(views, "" if views == 1 else "s")
			Text(img, label, (p[:, 0].min(), p[:, 1].min() - 8), Color(a), 0.45)
		x = PAD
		for k, a in enumerate(away):
			text = "{}#{}".format("3D off this image: " if k == 0 else "", a)
			Text(img, text, (x, img.shape[0] - PAD), Color(a), 0.45)
			x += cv2.getTextSize(text, FONT, 0.45, 1)[0][0] + 10
	return offsets


class Layout:
	"""
	Where the tiles go: n cameras' frames of frame_shape in a grid (1, 2 or
	3 columns), scaled to fit size (width, height), and the panel in the
	grid's empty cell, or else in a strip of panel_lines below it.
	"""

	def __init__(self, n, frame_shape, size=(1800, 960), scale=None, panel_lines=8):
		h, w = frame_shape[:2]
		self.cols = 1 if n == 1 else 2 if n <= 4 else 3
		self.rows = -(-n // self.cols)
		self.inGrid = n < self.rows * self.cols
		strip = 0 if self.inGrid else LINE * panel_lines + 2 * PAD
		if not scale:
			scale = min(size[0] / (self.cols * w), max(1, size[1] - strip) / (self.rows * h), 1.0)
		th, tw = max(1, int(round(h * scale))), max(1, int(round(w * scale)))
		self.tile = (th, tw)
		self.scale = (tw / w, th / h)
		if self.inGrid:
			x = (n % self.cols) * tw
			self.panel = ((self.rows - 1) * th, x, th, self.cols * tw - x) # y, x, height, width
		else:
			self.panel = (self.rows * th, 0, strip, self.cols * tw)
		self.size = (self.rows * th + strip, self.cols * tw) # height, width

	def Cell(self, k):
		return (k // self.cols) * self.tile[0], (k % self.cols) * self.tile[1]


def Gray(img, shape):
	# A frame (height x width x channels, mono or RGB), scaled to shape (height, width), as BGR. Halved
	# by averaging while it is twice the size or more, then interpolated: averaging at a ratio
	# that is not a whole number took 8 ms for a 1280x1024 frame, this 0.07 ms.
	h, w = shape
	while img.shape[0] >= 2 * h and img.shape[1] >= 2 * w:
		img = cv2.resize(img, (img.shape[1] // 2, img.shape[0] // 2), interpolation=cv2.INTER_AREA)
	if img.shape[:2] != (h, w):
		img = cv2.resize(img, (w, h), interpolation=cv2.INTER_LINEAR)
	if img.ndim == 2 or img.shape[2] == 1:
		return cv2.cvtColor(img.reshape(shape), cv2.COLOR_GRAY2BGR)
	return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)


# ---------------------------------------------------------------- the view

class Viewer:
	"""
	The feed and the results, and the view of them. Step makes the next
	view, when there is one; whatever is not there yet (campy-acquire's
	feed, campy-live's results) is looked for again every quarter second.
	"""

	def __init__(self, args):
		self.args = args
		self.ringNames = args.rings.split(",") if args.rings else \
			[livefeed.RingName(args.name, n) for n in range(args.cameras)]
		self.resultsName = liveresults.RingName(args.name)
		self.readers = None # each camera's RingReader, in feed order
		self.results = None # liveresults.Results
		self.description = {}
		self.calibration = None # each feed camera's pose3d.Camera, or None (2D only)
		self.buffers = None
		self.selected = args.camera # None: every camera
		self.show2d = self.show3d = self.lines = True
		self.after = 0 # the last row taken
		self.lastRow = self.lastRaw = self.checked = 0.0
		self.ended = False # a recording's feed came and ended
		self.layoutKey = self.layout = None
		# For the panel and the summary
		self.rows, self.gone, self.passed, self.raw = 0, 0, 0, 0 # rows taken, gone, passed over; views of frames alone
		self.rate = [] # (monotonic, rows written): each new count of the last second
		self.drawMs, self.shownMs, self.resultMs = deque(maxlen=KEPT), deque(maxlen=KEPT), deque(maxlen=KEPT)
		self.lastSeen = self.lastShown = None # ms after the row's frames: it reached the view, the last view was shown

	# -- the shared memory

	def Check(self):
		# Attaches what has come, lets go of what ended
		self.checked = time.monotonic()
		if self.results is not None and self.results.Ended():
			self.results.Close()
			self.results = None
		if self.readers is not None and any(r.Ended() or r.Replaced() for r in self.readers):
			for r in self.readers:
				r.Close()
			self.readers = None
			self.ended = True
			Say("The recording ended.")
		if self.results is None:
			try:
				self.results = liveresults.Results(self.resultsName)
			except (OSError, ValueError): # not there, or being made
				pass
			else:
				if self.results.Ended():
					self.results.Close()
					self.results = None
				else:
					self.after = 0 # a new campy-live's rows count from 1
					self.Describe(self.results.description)
		if self.readers is None:
			readers = []
			for name in self.ringNames:
				try:
					reader = livefeed.RingReader(name)
				except (OSError, ValueError):
					break
				if reader.Ended():
					reader.Close()
					break
				readers.append(reader)
			if len(readers) == len(self.ringNames):
				self.readers = readers
				self.buffers = [np.empty(r.shape, r.dtype) for r in readers]
				self.ended = False
				Say("Live feed: {}.".format(", ".join("{} {}x{}".format(r.cameraName or r.name, r.shape[1], r.shape[0])
					for r in readers)))
			else:
				for r in readers:
					r.Close()

	def Describe(self, d):
		# What campy-live said about its results: its cameras' rings, the skeleton, the calibration
		self.description = d
		rings = d.get("rings") or self.ringNames
		if rings != self.ringNames:
			self.ringNames = list(rings)
			if self.readers is not None:
				for r in self.readers:
					r.Close()
				self.readers = None
		self.calibration = None
		path = self.args.calibration or d.get("calibration")
		if path:
			names = self.args.calibration_names.split(",") if self.args.calibration_names else d.get("calibration_names")
			try:
				from campy import pose3d
				self.calibration = pose3d.LoadCalibration(path, names=names)
			except Exception as e:
				Say("Could not read the calibration {}: {}. Showing the 2D only.".format(path, e))
		Say("campy-live's results: {} cameras, {} nodes, {}.".format(len(rings), len(d.get("nodes") or []) or "?",
			"3D of {} animals".format(d.get("animals")) if self.calibration else "2D only"))

	def Close(self):
		for r in self.readers or ():
			r.Close()
		if self.results is not None:
			self.results.Close()
		self.readers = self.results = None

	# -- one view

	def Shown(self):
		# The cameras shown, by feed index
		n = len(self.readers) if self.readers else len(self.ringNames)
		if self.selected is not None and 0 <= self.selected < n:
			return [self.selected]
		return list(range(n))

	def Read(self, n):
		# Frame n of each camera shown, or None if a ring no longer holds it
		frames = []
		for c in self.Shown():
			frame = self.readers[c].Read(n, self.buffers[c])
			if frame is None:
				return None
			frames.append(frame)
		return frames

	def Step(self):
		"""
		The next view: (image, frame number, the row drawn or None), or None
		when there is nothing new to show yet.
		"""
		now = time.monotonic()
		if now - self.checked > 0.25:
			self.Check()
		if self.readers is None:
			if now - self.lastRaw < 0.5:
				return None
			self.lastRaw = now
			return self.Waiting(), None, None
		if self.results is not None:
			latest = self.results.Latest()
			if not self.rate or latest != self.rate[-1][1]:
				self.rate = [(t, n) for t, n in self.rate if t > now - 1.0] + [(now, latest)]
			if latest > self.after:
				n, row = self.results.Newest(self.after)
				seen = time.perf_counter_ns()
				if row is not None and self.after:
					self.passed += n - self.after - 1 # made while the view was busy: the newest goes first
				self.after = n
				if row is not None:
					self.rows += 1
					# perf_counter is one clock in every process, the frames' written times' too, and
					# precise on Windows (monotonic, host_ns's, moves in 15.6 ms steps there)
					self.lastSeen = (seen - int(np.max(row["written_ns"]))) / 1e6
					self.resultMs.append(self.lastSeen)
					frames = self.Read(int(row["frame"]))
					if frames is None:
						self.gone += 1 # its frames already replaced in the feed
						return None
					self.lastRow = now
					return self.Compose(frames, row), int(row["frame"]), row
		if now - self.lastRow > STALE_S and now - self.lastRaw >= 1.0 / max(1e-3, self.args.fps):
			# No results: the newest frames alone
			newest = min(self.readers[c].Latest() for c in self.Shown())
			frames = self.Read(newest) if newest > 0 else None
			if frames is None:
				return None
			self.lastRaw = now
			self.raw += 1
			return self.Compose(frames, None), newest, None
		return None

	def Layout(self, shape):
		key = (len(self.Shown()), shape, self.args.size, self.args.scale, self.PanelLines())
		if key != self.layoutKey:
			self.layoutKey = key
			self.layout = Layout(key[0], shape, self.args.size, self.args.scale, key[4])
		return self.layout

	def PanelLines(self):
		# Lines in the strip below the frames, when it is one (the panel's lines wrap)
		return 7 + (int(self.description.get("animals") or 0) if self.calibration else 0)

	def Compose(self, frames, row):
		start = time.perf_counter()
		shown = self.Shown()
		layout = self.Layout(frames[0].image.shape)
		canvas = np.zeros(layout.size + (3,), np.uint8)
		edges = self.description.get("edges")
		cameraNames = self.description.get("cameras") or [r.cameraName for r in self.readers]
		calibNames = self.description.get("calibration_names") or cameraNames
		for k, (c, frame) in enumerate(zip(shown, frames)):
			tile = Gray(frame.image, layout.tile)
			if row is not None:
				DrawCamera(tile, layout.scale, row, c, self.calibration[c] if self.calibration else None, edges,
					self.show2d, self.show3d, self.lines)
			name = cameraNames[c] if c < len(cameraNames) else str(c)
			if c < len(calibNames) and calibNames[c] != name:
				name += " ({})".format(calibNames[c])
			text = "{}  {}  frame {}".format(c + 1, name, frame.frameNumber)
			if row is not None:
				text += "  found {}".format(int(row["found"][c]))
			Text(tile, text, (PAD, PAD + 12))
			y, x = layout.Cell(k)
			canvas[y:y + layout.tile[0], x:x + layout.tile[1]] = tile
		self.Panel(canvas, layout, frames, row)
		self.drawMs.append((time.perf_counter() - start) * 1e3)
		return canvas

	def Panel(self, canvas, layout, frames, row):
		y0, x0, h, w = layout.panel
		panel = canvas[y0:y0 + h, x0:x0 + w]
		lines = [] # (text, colour)
		if row is not None:
			lines.append(("Frame {}: its results here {:.1f} ms after the frame{}".format(int(row["frame"]),
				self.lastSeen, "; the last view shown {:.1f} ms after its frame".format(self.lastShown)
				if self.lastShown is not None else ""), TEXT))
			if Has3D(row) and self.calibration:
				for a in range(len(row["n_views"])):
					views = int(row["n_views"][a])
					err = np.asarray(row["reproj_px"][a], float)
					if views >= 2 and np.isfinite(err).any():
						text = "#{}: from {} cameras, reprojection {:.1f} px".format(a, views, float(np.nanmean(err)))
					else:
						text = "#{}: no 3D ({} camera{})".format(a, views, "" if views == 1 else "s")
					lines.append((text, Color(a)))
		elif self.results is None:
			lines.append(("No results: campy-live not running, or with --results-feed off", TEXT))
		elif not self.lastRow:
			lines.append(("No results from campy-live yet: the frames alone", TEXT))
		else:
			lines.append(("No new results from campy-live for {:.1f} s: the frames alone".format(
				time.monotonic() - self.lastRow), TEXT))
		rate = (self.rate[-1][1] - self.rate[0][1]) / max(1e-3, self.rate[-1][0] - self.rate[0][0]) \
			if len(self.rate) > 1 else 0.0
		lines.append(("campy-live: {:.1f} results/s. Drawn: {}; their frames gone: {}".format(rate,
			self.rows - self.gone, self.gone), DIM))
		if row is not None:
			lines.append(("Dots: the 3D, projected. Open circles: this camera's 2D (white: matched to no "
				"animal). Line: from the 2D to the 3D. Label: #identity, and px between them.", DIM))
		lines.append(("Keys: 1-{} one camera, 0 all; d 2D, p 3D, l lines; space pause, s save, q quit".format(
			len(self.ringNames)), DIM))
		y = PAD + 14
		for text, color in lines:
			for part in Wrap(text, w - 2 * PAD, 0.45):
				if y > h - 4:
					return
				Text(panel, part, (PAD, y), color, 0.45, box=False)
				y += LINE

	def Waiting(self):
		canvas = np.zeros((130, 760, 3), np.uint8)
		Text(canvas, "Waiting for the live feed: {}".format(", ".join(self.ringNames)), (PAD, 40))
		Text(canvas, "(campy-acquire with liveFeed True)", (PAD, 70), DIM)
		Text(canvas, "campy-live's results: {}".format("there" if self.results else "not yet"), (PAD, 100), DIM)
		return canvas

	def Key(self, key):
		# A key pressed in the window; False to quit
		if key in (ord("q"), 27):
			return False
		if ord("1") <= key <= ord("9") and key - ord("1") < len(self.ringNames):
			self.selected = key - ord("1")
		elif key in (ord("0"), ord("a")):
			self.selected = None
		elif key == ord("d"):
			self.show2d = not self.show2d
		elif key == ord("p"):
			self.show3d = not self.show3d
		elif key == ord("l"):
			self.lines = not self.lines
		return True

	def Summary(self, seconds):
		def P(values, q):
			return float(np.percentile(values, q)) if len(values) else float("nan")
		# The timings are of the last KEPT views
		return {"seconds": seconds, "results_taken": self.rows, "results_drawn": self.rows - self.gone,
			"results_gone": self.gone, "results_passed_over": self.passed, "frames_alone": self.raw,
			"draw_ms": {"p50": P(self.drawMs, 50), "p99": P(self.drawMs, 99)},
			"results_here_after_frame_ms": {"p50": P(self.resultMs, 50), "p99": P(self.resultMs, 99)},
			"shown_after_frame_ms": {"p50": P(self.shownMs, 50), "p99": P(self.shownMs, 99)}}


# ---------------------------------------------------------------- the program

def LowerPriority():
	# Below campy-acquire and campy-live, which must keep up
	try:
		if os.name == "nt":
			import ctypes
			kernel32 = ctypes.windll.kernel32
			kernel32.GetCurrentProcess.restype = ctypes.c_void_p
			kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
			kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x4000) # BELOW_NORMAL_PRIORITY_CLASS
		else:
			os.nice(10)
	except Exception as e:
		Say("Could not lower this program's priority: {}".format(e))


def Run(args):
	cv2.setNumThreads(1) # a few small images: not worth taking every core from campy
	if args.priority == "low":
		LowerPriority()
	if args.png:
		os.makedirs(args.png, exist_ok=True)
	window = not args.no_window
	if window:
		try:
			cv2.namedWindow(TITLE, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
		except cv2.error as e:
			Say("Cannot open a window ({}): this OpenCV has no window support, or there is no desktop. "
				"Use --no-window --png <folder>.".format(str(e).strip().splitlines()[-1]))
			return 1
	viewer = Viewer(args)
	start = time.monotonic()
	shownSize, paused, image, count = None, False, None, 0
	Say("Showing {}; results from {}.".format(", ".join(viewer.ringNames), viewer.resultsName))
	try:
		while True:
			if args.seconds and time.monotonic() - start >= args.seconds:
				break
			got = None if paused else viewer.Step()
			if got is not None:
				image, frame, row = got
				if window:
					if image.shape[:2] != shownSize:
						shownSize = image.shape[:2]
						cv2.resizeWindow(TITLE, shownSize[1], shownSize[0])
					cv2.imshow(TITLE, image)
				if row is not None:
					viewer.lastShown = (time.perf_counter_ns() - int(np.max(row["written_ns"]))) / 1e6
					viewer.shownMs.append(viewer.lastShown)
					count += 1
					if args.png and count % args.png_every == 0:
						cv2.imwrite(os.path.join(args.png, "view_{:08d}.png".format(frame)), image)
			if window:
				# The window's events, without waiting: waitKey(1) waits a whole clock tick on Windows (15.6 ms)
				key = cv2.pollKey() if hasattr(cv2, "pollKey") else cv2.waitKey(1)
				if key >= 0:
					key &= 0xFF
					if key == ord(" "):
						paused = not paused
					elif key == ord("s") and image is not None:
						path = os.path.join(args.png or ".", "view_{}.png".format(datetime.now().strftime("%H-%M-%S")))
						cv2.imwrite(path, image)
						Say("Saved {}.".format(path))
					elif not viewer.Key(key):
						break
				if cv2.getWindowProperty(TITLE, cv2.WND_PROP_VISIBLE) < 1:
					break # its window closed
				if got is None:
					time.sleep(args.poll_ms / 1e3)
			else:
				if viewer.ended:
					break
				time.sleep(args.poll_ms / 1e3)
	except KeyboardInterrupt:
		pass
	finally:
		viewer.Close()
		if window:
			cv2.destroyAllWindows()
	summary = viewer.Summary(time.monotonic() - start)
	Say("Done: {results_drawn} results drawn ({results_gone} whose frames were gone, {results_passed_over} passed "
		"over for a newer one), {frames_alone} views of "
		"frames alone; drawing p50 {d[p50]:.1f} p99 {d[p99]:.1f} ms; shown p50 {s[p50]:.1f} p99 {s[p99]:.1f} ms after "
		"the frame (its results here p50 {r[p50]:.1f} p99 {r[p99]:.1f} ms).".format(d=summary["draw_ms"],
		s=summary["shown_after_frame_ms"], r=summary["results_here_after_frame_ms"], **summary))
	if args.summary:
		with open(args.summary, "w") as f:
			json.dump(summary, f, indent=2)
	return 0


def Size(text):
	try:
		w, h = (int(x) for x in text.lower().split("x"))
		return w, h
	except ValueError:
		raise argparse.ArgumentTypeError("give it as width x height, e.g. 1800x960")


def Camera(text):
	return None if text.lower() == "all" else int(text)


def ParseArgs(argv=None):
	p = argparse.ArgumentParser(prog="campy-live-view", description="The cameras' live frames with campy-live's "
		"results drawn on them (python -m pydoc campy.liveview says what it draws).",
		formatter_class=argparse.ArgumentDefaultsHelpFormatter)
	p.add_argument("--cameras", type=int, default=3, help="How many cameras, until campy-live says.")
	p.add_argument("--name", default=livefeed.DEFAULT_NAME, help="campy's liveFeedName (campy-live's --name).")
	p.add_argument("--rings", default="", help="Or the cameras' rings, comma-separated, until campy-live says.")
	p.add_argument("--camera", type=Camera, default=None, help="all, or one camera's index (0, 1, ...).")
	p.add_argument("--calibration", default="", help="Default: the one campy-live uses.")
	p.add_argument("--calibration-names", default="", help="With --calibration: the cameras' names in it, "
		"comma-separated, in feed order.")
	p.add_argument("--size", type=Size, default=(1800, 960), help="The view's largest width x height, px.")
	p.add_argument("--scale", type=float, default=0, help="Each frame's scale in the view (0: to fit --size).")
	p.add_argument("--fps", type=float, default=30, help="Frames alone (no results) shown per second.")
	p.add_argument("--no-window", action="store_true", help="No window (e.g. over SSH): with --png, saves the "
		"views; ends with the recording.")
	p.add_argument("--png", default="", help="Folder for the views saved as PNG (every --png-every-th, and s).")
	p.add_argument("--png-every", type=int, default=30, help="Save every this many results drawn.")
	p.add_argument("--seconds", type=float, default=0, help="Stop after this long (0: never).")
	p.add_argument("--summary", default="", help="Write the summary (counts, timings) to this .json.")
	p.add_argument("--priority", default="low", choices=["low", "normal"], help="low: below campy-acquire and "
		"campy-live.")
	p.add_argument("--poll-ms", type=float, default=2, help="How often to look for a result.")
	return p.parse_args(argv)


def Main(argv=None):
	sys.exit(Run(ParseArgs(argv)))


if __name__ == "__main__":
	Main()
