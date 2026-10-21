"""
campy-live-view: the cameras' live frames, with campy-live's results drawn on
them, as campy records.

    campy-live-view                      every camera, in a grid
    campy-live-view --camera 0           camera 0 alone
    campy-live-view --no-window --png views --png-every 30 --seconds 20

Start it before or after campy-acquire (liveFeed True) and campy-live: it
waits for them, and stays open from one recording to the next (with
--no-window it ends with the recording). It runs in campy-live's Python
environment (numpy, scipy and OpenCV; PyQt5 for Qt's window, else OpenCV's,
--gui), as a program of its own, at a lower
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

The 3D view
-----------
With a calibration, a tile beside the cameras' shows the same row in 3D
(Scene): each animal's 3D pose in its identity's colour (the nearer drawn
over the farther, a thin line down to the floor under its middle), the
calibrated cameras as frustums (a triangle on each one's top edge), and a
floor grid. It starts from the cameras' side, 50 degrees up, the cage filling
it; drag in it to turn it, right-drag (or middle) to move it, wheel to zoom
(out, to see the cameras above the cage), double click or r to put it back.
The calibration's world is often its first camera's, and knows nothing of the
floor: up is at first away from where the cameras look, then the plane the
animals' middles move in (--up x,y,z sets it), and the floor is under their
lowest well triangulated nodes. It draws in about 1 ms (the grid and cameras
are drawn again only when the view of them moves). Its own drawing with
numpy and cv2, in this window, rather than matplotlib or a browser: no other
package or program, the same row as the cameras' tiles by construction, and
cheap enough to draw at 30 a second.

The panel lists each identity's cameras and reprojection error (campy-live's:
mean over nodes and cameras, undistorted px), and how late the results and
the view are.

Is the rig all right, and the tracking?
---------------------------------------
So that a problem shows at a glance (campy/livehealth.py says how each is
told):
- Each camera's tile says its frames a second at its top right, in green, or
  in red what is wrong, with a red frame round the tile: behind the other
  cameras or no frames (12.0 FPS, NO FRAMES), frames the camera lost (LOST
  n), frame numbers missing from its feed (GAP n) or frames that came a frame
  late (LATE n), in the last 10 s, and a camera whose host times are a frame
  or more off the others' for the same frame number (1 FRAME AHEAD: as a
  camera left grabbing after a double Ctrl+C was on 2026-10-20, and campy-live
  called every set misaligned). The view reads this from the cameras' rings
  (their frames' numbers and times, not the frames) 20 times a second.
- The panel starts with what is wrong, in red, then: each identity's strip
  of the last 10 s (its height the cameras it was triangulated from, its
  colour its reprojection error: green within 10 px, yellow within --bad-px,
  red above, gray no 3D), with a warning when fewer identities than
  campy-live's --animals have good 3D, or when one's error is far above the
  others'; how long the recording has been going, each camera's frames a
  second, and how far apart their host times are.
- An identity whose reprojection error is above --bad-px (20 px) is drawn
  dimmed and dashed, on the cameras' tiles and in 3D, its label red ("#2
  bad 3D: 234 px"): a detection on equipment outside the cage, say, or a
  wrong match. It took about 0.5 ms of each view's drawing.

Buttons in the panel turn the predictions (the 2D), the reprojections (the
3D) and the lines on or off, as do the keys d, p and l, and the 3D view, as
does \\ (--no-3d: off at first).

Every camera (the grid) or one alone (single view), with LUC3D's keys: v one
camera alone, the one last picked (clicked, or the last shown alone; an
outline marks it in the grid), with the 3D beside it unless \\ closed it; g
the grid; the arrows the previous or next camera (wrapping; in the grid,
which one v shows); a double click on a camera's tile shows it alone, or the
grid again. Also 1-9 that camera alone, 0 or a the grid.

Other keys, in its window: z the window to the view's size (no black
margins); space pause (the keys, buttons and the 3D view's mouse still work
on the paused view); s save the view as a PNG (into --png, or the current
folder); q or Esc quit.

Qt's window (campy/liveviewqt.py; PyQt5, as campy's preview windows) shows
the same view beside a panel of Qt's own checkboxes, buttons and text;
OpenCV's draws the panel into the view. Both take the same keys and mouse.

The window opens at --size, or what the screen allows of it, and comes to
the front when it opens and when a recording's frames come (also from
minimized). The view is drawn at the window's own size, one pixel for one:
resized, it is laid out again (the frames in their ratio, at most their own
size, in the middle of the window), never stretched.
"""

import argparse, functools, json, os, sys, time
from collections import deque
from datetime import datetime

import numpy as np
import cv2

from campy import livefeed, livehealth, liveresults

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
BUTTON_ON = (90, 70, 20) # a button that is on (BGR: dark blue)
PICKED = (255, 200, 80) # the outline of the camera picked, in the grid (light blue)
WARN = (40, 40, 255) # what is wrong: red
FINE = (90, 200, 90) # green
FAIR = (0, 200, 230) # yellow
NO_3D = (75, 75, 75)
KEYS = ("Keys: v one camera, g all (grid), arrows the previous or next camera, 1-{} that camera; d predictions, "
	"p reprojections, l lines{}; z window to the view; space pause, s save, q quit")
SHIFT = 4 # cv2's subpixel bits
RADIUS_2D, RADIUS_3D = 4, 2 # px: a detection's open circles, a 3D node's dot
FONT = cv2.FONT_HERSHEY_SIMPLEX
LINE = 20 # px between the panel's lines
PAD = 8
STALE_S = 0.5 # no row for this long: the frames alone
STRIP_H, STRIP_LABEL = 14, 110 # px: each identity's strip, its label before it
# The strip's colours, as a cv2.LUT: between identities, no row from campy-live, no 3D, good, fair, bad
STRIP_COLORS = np.zeros((1, 256, 3), np.uint8)
STRIP_COLORS[0, :6] = [BOX, (12, 12, 12), NO_3D, FINE, FAIR, WARN]
KEPT = 36000 # the timings kept for the summary: the last 20 minutes at 30 a second


def Say(text):
	print("[{}] {}".format(datetime.now().strftime("%H:%M:%S"), text), flush=True)


def Color(identity):
	return COLORS[int(identity) % len(COLORS)]


def Dimmed(color):
	# An identity's colour, for 3D not to trust: toward a dark gray
	return tuple(int(0.4 * v + 45) for v in color)


def Bad(row, bad_px):
	# The identities whose 3D, in row, is off by more than bad_px (mean reprojection error over the nodes)
	if not Has3D(row) or not bad_px:
		return set()
	with np.errstate(invalid="ignore"):
		return set(np.nonzero(livehealth.Errors(row) > bad_px)[0].tolist())


def Dashes(p, q, dash=6 << SHIFT):
	"""
	The dashes of dashed lines from p to q (points (N, 2), with SHIFT
	bits): (dashes, 2, 2) int32, for one cv2.polylines. About 6 px each, and
	at most 12 a line: a line 234 px off is still a few calls.
	"""
	p, q = np.asarray(p, float).reshape(-1, 2), np.asarray(q, float).reshape(-1, 2)
	if not len(p):
		return np.zeros((0, 2, 2), np.int32)
	n = np.clip(np.linalg.norm(q - p, axis=1) // dash, 1, 24).astype(int) # pieces: dash, gap, dash, ...
	count = (n + 1) // 2 # dashes a line
	i = np.repeat(np.arange(len(p)), count)
	k = 2 * (np.arange(count.sum()) - np.repeat(np.cumsum(count) - count, count)) # each dash's first piece
	m = n[i]
	a = p[i] + (q[i] - p[i]) * (k / m)[:, None]
	b = p[i] + (q[i] - p[i]) * (np.minimum(k + 1, m) / m)[:, None]
	return np.stack([a, b], 1).astype(np.int32)


def Dashed(img, p, q, passes):
	# Dashed lines from p to q (points (N, 2), with SHIFT bits), in one call for each of passes, (colour,
	# thickness): an outline, then the colour. Not antialiased: a third of the time, and less than the
	# solid antialiased lines they stand for
	dashes = list(Dashes(p, q))
	if dashes:
		for color, thickness in passes:
			cv2.polylines(img, dashes, False, color, thickness, cv2.LINE_8, SHIFT)


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


def DrawSkeleton(img, px, scale, edges, color, radius, thickness, filled, dashed=False):
	"""An animal's nodes (N, 2, pixels of the frame) and edges (dashed: not to trust), on img (a tile of the frame at scale)."""
	ok, pts = Points(px, scale)
	lines = [(a, b) for a, b in (edges or ()) if a < len(ok) and b < len(ok) and ok[a] and ok[b]]
	nodes = np.nonzero(ok)[0]
	if dashed and lines:
		Dashed(img, [pts[a] for a, _ in lines], [pts[b] for _, b in lines], ((OUTLINE, thickness + 2),
			(color, thickness)))
	for col, extra in ((OUTLINE, 2), (color, 0)): # the outline under the colour
		for a, b in (() if dashed else lines):
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


@functools.lru_cache(maxsize=1024)
def Wrap(text, width, size):
	# text in lines no wider than width px (kept: the panel says much the same from one view to the next)
	lines, line = [], ""
	for word in text.split(" "):
		trial = (line + " " + word) if line else word
		if line and cv2.getTextSize(trial, FONT, size, 1)[0][0] > width:
			lines.append(line)
			line = word
		else:
			line = trial
	return tuple(lines + [line])


def Has3D(row):
	return row is not None and "points3d" in row.dtype.names


def DrawCamera(img, scale, row, c, camera=None, edges=None, show2d=True, show3d=True, lines=True,
		bad_px=livehealth.BAD_PX):
	"""
	Camera c's part of a results row on img, its frame at scale (a number, or
	x and y): its 2D detections, and with camera (its pose3d.Camera) each
	animal's 3D projected into it; an identity whose reprojection error is
	above bad_px dimmed and dashed, its label red. Returns {identity: mean
	px, in this camera, between its projected 3D and the detection it was
	made from (NaN: no detection here)} for the identities with 3D.
	"""
	scale = np.broadcast_to(np.asarray(scale, float), (2,))
	bad = Bad(row, bad_px)
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
					cv2.line(img, p2, p3, Dimmed(Color(a)) if a in bad else Color(a), 1, cv2.LINE_AA, SHIFT)
	if show3d:
		for a, px in projected.items():
			DrawSkeleton(img, px, scale, edges, Dimmed(Color(a)) if a in bad else Color(a), radius=RADIUS_3D,
				thickness=2, filled=True, dashed=a in bad)
		errors = livehealth.Errors(row) if bad else None
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
			if a in bad: # not to trust: campy-live's error, over every camera
				label = "#{}  bad 3D: {:.0f} px".format(a, errors[a])
			Text(img, label, (p[:, 0].min(), p[:, 1].min() - 8), WARN if a in bad else Color(a), 0.45)
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
		# panel_lines 0: no panel (the Qt window has its own, beside the view)
		h, w = frame_shape[:2]
		def Fit(cols):
			rows = -(-n // cols)
			inGrid = n < rows * cols
			strip = 0 if inGrid or not panel_lines else LINE * panel_lines + 2 * PAD
			return min(size[0] / (cols * w), max(1, size[1] - strip) / (rows * h), 1.0), rows, inGrid, strip
		# The columns that fit the tiles largest; when two fit them as large, 2 columns up to 4 tiles,
		# then 3 (3 cameras and the 3D: 3 columns, which leave the panel a cell, at 1800 x 960)
		usual = 1 if n == 1 else 2 if n <= 4 else 3
		self.cols = max(range(1, min(n, 3) + 1), key=lambda cols: (round(Fit(cols)[0], 6), cols == usual))
		fit, self.rows, self.inGrid, strip = Fit(self.cols)
		if not scale:
			scale = fit
		th, tw = max(1, int(h * scale + 1e-9)), max(1, int(w * scale + 1e-9)) # down: within the size
		self.tile = (th, tw)
		self.scale = (tw / w, th / h)
		if not panel_lines:
			self.panel = None
		elif self.inGrid:
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


# ---------------------------------------------------------------- the 3D view

BACKDROP, GRID, GRID_MID, FRUSTUM = (34, 28, 24), (66, 62, 58), (110, 104, 98), (200, 200, 200)
FOV = 35 # degrees: the virtual camera's view across the tile (a long lens: little perspective)
GOOD_PX = 10 # a 3D node reprojected within this (px) tells where the floor is


def Unit(v):
	v = np.asarray(v, float)
	n = np.linalg.norm(v)
	return v / n if n > 1e-12 else v


def Nice(x):
	# 1, 2 or 5 times a power of ten, near x
	p = 10.0 ** np.floor(np.log10(x))
	m = x / p
	return p * (1 if m < 1.5 else 2 if m < 3.5 else 5 if m < 7.5 else 10)


def Up(cameras):
	"""
	Which way is up in the calibration's world (its own frame, often the
	first camera's, which knows nothing of the floor): away from where the
	cameras look, when they look down on the cage; otherwise their images' up.
	"""
	look = np.mean([c.R[2] for c in cameras], 0)
	up = -look if np.linalg.norm(look) > 0.5 else -np.mean([c.R[1] for c in cameras], 0)
	return Unit(up) if np.linalg.norm(up) > 1e-6 else np.array([0.0, 0.0, 1.0])


def Aim(cameras):
	# The point nearest every camera's optical axis: what they look at, the cage
	A, b = np.zeros((3, 3)), np.zeros(3)
	for c in cameras:
		M = np.eye(3) - np.outer(c.R[2], c.R[2])
		A += M
		b += M @ c.center
	if np.linalg.cond(A) < 1e6:
		return np.linalg.solve(A, b)
	centres = np.array([c.center for c in cameras]) # one camera, or parallel ones: in front of them
	spread = max(1.0, float(np.linalg.norm(centres - centres.mean(0), axis=1).max()))
	return centres.mean(0) + np.mean([c.R[2] for c in cameras], 0) * spread


class Scene:
	"""
	The 3D tile: the animals' 3D poses, the calibrated cameras (frustums) and
	a floor grid, as a virtual camera sees them. It orbits the middle of the
	cage, about the world's up: Orbit turns it (by pixels dragged), Pan moves
	what it looks at, Zoom moves it nearer, Reset puts it back (from the
	cameras' side, 50 degrees up, the cage filling the tile; the cameras,
	above it, come into view zoomed out).

	Up is at first away from where the cameras look (Up), or --up, and the
	cage's middle is what they look at (Aim). Learn (each row taken) then
	levels the floor with where the animals go: up is the normal of the
	plane their middles move in (unless --up), the floor is under their
	lowest nodes, and the cage is where their middles went; only well
	triangulated ones count (2 cameras or more, reprojected within GOOD_PX).

	Draw(img, row) draws a row on img, its tile: the grid and cameras drawn
	once for each view of them (Background), then the animals, farthest
	first. Project gives where a world point lands on a tile.
	"""

	def __init__(self, cameras, names=None, up=None):
		self.cameras = list(cameras)
		self.names = list(names or [c.name for c in self.cameras])
		self.fixedUp = up is not None
		self.camerasUp = Unit(up) if up is not None else Up(self.cameras)
		self.target = Aim(self.cameras)
		centres = np.array([c.center for c in self.cameras], float)
		reach = np.linalg.norm(centres - self.target, axis=1).max()
		self.reach = float(reach) if np.isfinite(reach) and reach > 1e-9 else 1.0 # the scene's size
		self.spacing = Nice(self.reach / 6) # the grid's
		self.near = self.reach * 0.01
		self.side = centres.mean(0) - self.target # the cameras' side: where the view starts
		self.floor = 0.0 # the floor's height above the target, along up
		self.home = self.target # the cage's middle (its height: the target's)
		self.extent = 0.45 * self.reach # how far the cage goes from its middle, along the floor
		self.nodes = deque(maxlen=3000) # well triangulated nodes
		self.middles = deque(maxlen=1000) # well triangulated animals' middles
		self.taken = 0
		self.cache = (None, None)
		self.Level(self.camerasUp)
		self.Reset()

	def Basis(self, up):
		# The floor's directions under up: e1 toward the cameras' side, e2 = up x e1 (e1, e2, up:
		# right-handed, as the calibration's world)
		side = self.side - (self.side @ up) * up
		if np.linalg.norm(side) < 1e-6 * self.reach:
			side = np.cross(up, [1.0, 0, 0]) if abs(up[0]) < 0.9 else np.cross(up, [0, 1.0, 0])
		e1 = Unit(side)
		return e1, np.cross(up, e1)

	def Level(self, up):
		self.up = Unit(up)
		self.e1, self.e2 = self.Basis(self.up)

	def Reset(self):
		self.azimuth, self.elevation = 0.0, np.radians(50)
		ground = self.Ground()
		self.centre = ground + 0.05 * self.reach * self.up
		# Near enough for the cage (extent around its middle, 0.15 reach high) to fill a tile of the frames' shape
		shape = (1024, 1280)
		see = np.array([ground + self.extent * (i * self.e1 + j * self.e2) + k * 0.15 * self.reach * self.up
			for i in (-1, 1) for j in (-1, 1) for k in (0, 1)])
		lo, hi = 0.2 * self.reach, 50 * self.reach
		for _ in range(30):
			self.distance = np.sqrt(lo * hi)
			px = self.Project(see, shape)
			if np.isfinite(px).all() and (np.abs(px - [640, 512]) <= [0.48 * 1280, 0.46 * 1024]).all():
				hi = self.distance
			else:
				lo = self.distance
		self.distance = hi
		self.touched = False # by the mouse, since

	def Ground(self):
		# The floor under the cage's middle
		return self.home + (self.floor - (self.home - self.target) @ self.up) * self.up

	def Orbit(self, dx, dy):
		# Dragged dx, dy px: turned about up, and up or down over the scene (not over the top)
		self.azimuth -= dx * 0.008
		self.elevation = float(np.clip(self.elevation + dy * 0.008, np.radians(-85), np.radians(85)))
		self.touched = True

	def Zoom(self, steps):
		# Nearer by 15 % a step (a wheel's click), farther for steps < 0
		self.distance = float(np.clip(self.distance * 0.85 ** steps, self.reach * 0.05, self.reach * 50))
		self.touched = True

	def Pan(self, dx, dy, width):
		# What it looks at moved with the mouse, dx, dy px on a tile width px wide
		R, _ = self.Eye()
		k = self.distance / self.Focal(width)
		self.centre = self.centre - (R[0] * dx + R[1] * dy) * k
		self.touched = True

	def Eye(self):
		# The virtual camera: world -> its axes (right, down, forward, as OpenCV's), and where it is
		ce, se = np.cos(self.elevation), np.sin(self.elevation)
		away = ce * (np.cos(self.azimuth) * self.e1 + np.sin(self.azimuth) * self.e2) + se * self.up
		forward = -away
		right = Unit(np.cross(forward, self.up))
		return np.stack([right, np.cross(forward, right), forward]), self.centre + self.distance * away

	@staticmethod
	def Focal(width):
		return 0.5 * width / np.tan(np.radians(FOV) / 2)

	def View(self, shape):
		R, eye = self.Eye()
		h, w = shape[:2]
		return R, eye, self.Focal(w), np.array([w / 2.0, h / 2.0])

	def Project(self, X, shape):
		"""World points (..., 3) -> pixels (..., 2) on a tile of shape, NaN behind the virtual camera."""
		R, eye, f, c = self.View(shape)
		p = (np.asarray(X, float) - eye) @ R.T
		with np.errstate(invalid="ignore", divide="ignore"):
			px = f * p[..., :2] / p[..., 2:3] + c
		px[~(p[..., 2] > self.near)] = np.nan
		return px

	def Learn(self, row):
		# A row's well triangulated nodes and animals; every 15 rows, the floor levelled with them
		if not Has3D(row):
			return
		X = np.asarray(row["points3d"], float)
		good = np.isfinite(X).all(-1) & (np.asarray(row["n_views"])[:, None] >= 2) & \
			(np.asarray(row["reproj_px"], float) < GOOD_PX)
		self.nodes.extend(X[good])
		for a in range(len(X)):
			if good[a].sum() >= max(3, good.shape[1] // 2):
				self.middles.append(X[a][good[a]].mean(0))
		self.taken += 1
		if self.taken % 15 != 1 or len(self.nodes) < 100:
			return
		def Near(P): # without what is far from the rest (detections outside the cage)
			P = np.array(P)
			return P[np.linalg.norm(P - np.median(P, 0), axis=1) < self.reach]
		up = self.up
		middles = Near(self.middles) if len(self.middles) >= 150 and not self.fixedUp else np.zeros((0, 3))
		if len(middles) >= 100:
			w, v = np.linalg.eigh(np.cov(middles.T))
			if w[0] < 0.25 * w[1] and np.sqrt(w[1]) > 0.1 * self.reach: # they went about, on a plane: the floor
				normal = v[:, 0] * np.sign(v[:, 0] @ self.camerasUp)
				if normal @ self.up < np.cos(np.radians(2)):
					up = normal
		step = self.spacing / 10
		floor = float(np.round(np.percentile((Near(self.nodes) - self.target) @ up, 2) / step) * step)
		home, extent = self.home, self.extent
		if len(self.middles) >= 30:
			# The cage: the middle of where the animals went (5 to 95 %, each way along the floor), and how far
			e = np.stack(self.Basis(up))
			lo, hi = np.percentile((Near(self.middles) - self.target) @ e.T, [5, 95], axis=0)
			middle = self.target + np.round((lo + hi) / 2 / step) * step @ e
			if np.linalg.norm(middle - self.home) > step:
				home = middle
			extent = float(np.round(max(0.15 * self.reach, (hi - lo).max() / 2 + 0.1 * self.reach) / step) * step)
		if floor == self.floor and up is self.up and home is self.home and extent == self.extent:
			return
		before = self.Ground()
		self.floor, self.home, self.extent = floor, home, extent
		if up is not self.up:
			self.Level(up)
		if not self.touched:
			self.Reset()
		else:
			self.centre = self.centre + self.Ground() - before # the view moves with the floor

	def Lines(self, img, view, segments, color, thickness=1):
		# World segments (N, 2, 3) on img: cut where they go behind the virtual camera, then at its edges
		R, eye, f, c = view
		P = (np.asarray(segments, float) - eye) @ R.T
		z0, z1 = P[:, 0, 2], P[:, 1, 2]
		keep = (z0 > self.near) | (z1 > self.near)
		with np.errstate(invalid="ignore", divide="ignore"):
			cut = P[:, 0] + ((self.near - z0) / (z1 - z0))[:, None] * (P[:, 1] - P[:, 0])
			A = np.where((z0 > self.near)[:, None], P[:, 0], cut)
			B = np.where((z1 > self.near)[:, None], P[:, 1], cut)
			a = np.clip(f * A[:, :2] / A[:, 2:3] + c, -1e7, 1e7)
			b = np.clip(f * B[:, :2] / B[:, 2:3] + c, -1e7, 1e7)
		h, w = img.shape[:2]
		for i in np.nonzero(keep & np.isfinite(a).all(1) & np.isfinite(b).all(1))[0]:
			inside, p, q = cv2.clipLine((0, 0, w, h), (int(a[i, 0]), int(a[i, 1])), (int(b[i, 0]), int(b[i, 1])))
			if inside:
				cv2.line(img, p, q, color, thickness, cv2.LINE_AA)

	def Background(self, shape):
		"""The grid and the cameras, for this view of them, on a tile of shape (drawn again only when they move)."""
		key = (tuple(shape[:2]), self.azimuth, self.elevation, self.distance, tuple(self.centre), self.floor)
		if self.cache[0] == key:
			return self.cache[1]
		img = np.empty(tuple(shape[:2]) + (3,), np.uint8)
		img[:] = BACKDROP
		view = self.View(shape)
		s, n = self.spacing, int(np.ceil(0.9 * self.reach / self.spacing))
		o = self.Ground()
		for k in range(-n, n + 1):
			color = GRID_MID if k == 0 else GRID
			self.Lines(img, view, [[o + k * s * self.e1 - n * s * self.e2, o + k * s * self.e1 + n * s * self.e2],
				[o + k * s * self.e2 - n * s * self.e1, o + k * s * self.e2 + n * s * self.e1]], color)
		depth = 0.12 * self.reach
		for k, cam in enumerate(self.cameras):
			w, h = cam.size if cam.size is not None else 2 * cam.c
			# The image's corners, depth in front of the camera, and a triangle on its top edge
			uv = np.array([[0, 0], [w, 0], [w, h], [0, h], [0.35 * w, 0], [0.65 * w, 0], [0.5 * w, -0.18 * h]], float)
			rays = np.column_stack([(uv - cam.c) / cam.f, np.ones(len(uv))]) * depth
			X = cam.center + rays @ cam.R
			apex = cam.center
			segments = [[apex, X[i]] for i in range(4)] + [[X[i], X[(i + 1) % 4]] for i in range(4)] + \
				[[X[4], X[6]], [X[6], X[5]]]
			self.Lines(img, view, segments, FRUSTUM)
			at = self.Project(apex, shape)
			if np.isfinite(at).all() and (0 <= at).all() and at[0] < shape[1] and at[1] < shape[0]:
				Text(img, "{} {}".format(k + 1, self.names[k] if k < len(self.names) else cam.name),
					(at[0] + 6, at[1] - 6), FRUSTUM, 0.4)
		Text(img, "grid squares {:g}".format(s), (PAD, shape[0] - PAD), DIM, 0.4)
		self.cache = (key, img)
		return img

	def Draw(self, img, row, edges=None, bad_px=livehealth.BAD_PX):
		"""
		A row's animals on img (a tile), over the grid and the cameras; nearer
		over farther; those whose reprojection error is above bad_px dimmed and
		dashed, their labels red, under the others.
		"""
		img[:] = self.Background(img.shape)
		if row is None or not Has3D(row):
			return
		bad = Bad(row, bad_px)
		errors = livehealth.Errors(row) if bad else None
		view = self.View(img.shape)
		R, eye, f, c = view
		radius = self.reach / 100 # a node's, in the world
		items, labels = [], [] # (depth, edge or node, identity, which); (identity, where)
		floor = self.Ground()
		for a, X in enumerate(np.asarray(row["points3d"], float)):
			ok = np.isfinite(X).all(-1)
			if not ok.any():
				continue
			p = (X - eye) @ R.T
			front = ok & (p[:, 2] > self.near)
			if not front.any():
				continue
			with np.errstate(invalid="ignore", divide="ignore"):
				px = f * p[:, :2] / p[:, 2:3] + c
			# A thin line down to the floor, under its middle, to see where it is
			mid = X[ok].mean(0)
			self.Lines(img, view, [[mid, mid - ((mid - floor) @ self.up) * self.up]],
				tuple(int(v * 0.6) for v in (Dimmed(Color(a)) if a in bad else Color(a))))
			drawable, q = Points(np.where(front[:, None], px, np.nan), 1.0) # with SHIFT bits, once for the animal
			depth = p[:, 2].tolist()
			for i in np.nonzero(drawable)[0].tolist():
				items.append((depth[i], 1, a, (q[i], int(round(max(2.5, min(9.0, f * radius / depth[i])) * (1 << SHIFT))))))
			for u, v in edges or ():
				if u < len(drawable) and v < len(drawable) and drawable[u] and drawable[v]:
					items.append(((depth[u] + depth[v]) / 2, 0, a, (q[u], q[v])))
			top = px[front][np.argmin(px[front][:, 1])]
			labels.append((a, top))
		# The bad first, under the others (not to trust), each one's dashes at once; then the others, farthest first
		for a in sorted(bad):
			lines = [q for _, node, b, q in items if b == a and not node]
			if lines:
				Dashed(img, [p for p, _ in lines], [q for _, q in lines], ((OUTLINE, 4), (Dimmed(Color(a)), 2)))
		items.sort(key=lambda t: (t[2] not in bad, -t[0], t[1]))
		for _, node, a, (q0, q1) in items:
			color = Dimmed(Color(a)) if a in bad else Color(a)
			if node: # q0 the node, q1 its radius
				cv2.circle(img, q0, q1 + (1 << SHIFT), OUTLINE, -1, cv2.LINE_AA, SHIFT)
				cv2.circle(img, q0, q1, color, -1, cv2.LINE_AA, SHIFT)
			elif a in bad:
				pass # dashed, above
			else:
				cv2.line(img, q0, q1, OUTLINE, 4, cv2.LINE_AA, SHIFT)
				cv2.line(img, q0, q1, color, 2, cv2.LINE_AA, SHIFT)
		for a, at in labels:
			if a in bad:
				Text(img, "#{}  bad 3D: {:.0f} px".format(a, errors[a]), (at[0] + 6, at[1] - 10), WARN, 0.45)
			else:
				Text(img, "#{}".format(a), (at[0] + 6, at[1] - 10), Color(a), 0.45)


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
		self.selected = args.camera # None: every camera (the grid); a camera's index: that one (single view)
		self.current = args.camera or 0 # the camera last picked: single view starts with it
		self.panel = True # the panel drawn in the view (the Qt window shows its own)
		self.cells = [] # where each camera's tile is: (camera, y, x, height, width)
		self.origin = (0, 0) # where the view is in its window's image (centred), y, x
		self.count = 0 # results shown
		self.show2d = self.show3d = self.lines = True
		self.scene = None # the 3D tile's Scene (with a calibration)
		self.showScene = not args.no_3d
		self.sceneCell = None # where the 3D tile is in the view: y, x, height, width
		self.drag = None # the mouse button dragging in the 3D tile, and where it was
		self.mouseAt = (-1, -1)
		self.moved = False # the 3D turned since it was drawn
		self.windowSize = None # its window's, width x height, when it has one
		self.after = 0 # the last row taken
		self.lastRow = self.lastRaw = self.checked = 0.0
		self.ended = False # a recording's feed came and ended
		self.layoutKey = self.layout = None
		# For the panel and the summary
		self.rows, self.gone, self.passed, self.raw = 0, 0, 0, 0 # rows taken, gone, passed over; views of frames alone
		self.rate = [] # (monotonic, rows written): each new count of the last second
		self.drawMs, self.shownMs, self.resultMs = deque(maxlen=KEPT), deque(maxlen=KEPT), deque(maxlen=KEPT)
		self.sceneMs = deque(maxlen=KEPT) # of drawMs: the 3D tile's
		self.rig = None # livehealth.Rig: how each camera keeps up, from its ring's slots
		self.tracking = None # livehealth.Tracking: each identity's 3D over the last 10 s
		self.rigAt = 0.0 # when the slots were last read
		self.rigMs = deque(maxlen=KEPT) # reading them
		self.healthMs = deque(maxlen=KEPT) # of drawMs: the warnings, the tiles' marks and the strip
		self.panelMs = deque(maxlen=KEPT) # Qt's panel, said again (liveviewqt.Window.Say)
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
				self.rig = livehealth.Rig([r.cameraName or r.name for r in readers], readers[0].Info("frameRate"),
					readers[0].slots)
				self.rigAt = 0.0
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
		self.calibration = self.scene = None
		path = self.args.calibration or d.get("calibration")
		if path:
			names = self.args.calibration_names.split(",") if self.args.calibration_names else d.get("calibration_names")
			try:
				from campy import pose3d
				self.calibration = pose3d.LoadCalibration(path, names=names)
			except Exception as e:
				Say("Could not read the calibration {}: {}. Showing the 2D only.".format(path, e))
			else:
				self.scene = Scene(self.calibration, d.get("cameras") or names, self.args.up)
		self.tracking = livehealth.Tracking(d.get("animals"), d.get("frame_rate"), self.args.bad_px) \
			if self.calibration else None
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
		if now - self.rigAt >= self.rig.Every():
			# Each camera's newest frames' numbers and times (not the frames): how it keeps up
			self.rigAt = now
			start = time.perf_counter()
			self.rig.Sample(self.readers)
			self.rigMs.append((time.perf_counter() - start) * 1e3)
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
					if self.tracking is not None:
						self.tracking.Add(row)
					# perf_counter is one clock in every process, the frames' written times' too, and
					# precise on Windows (monotonic, host_ns's, moves in 15.6 ms steps there)
					self.lastSeen = (seen - int(np.max(row["written_ns"]))) / 1e6
					self.resultMs.append(self.lastSeen)
					frames = self.Read(int(row["frame"]))
					if frames is None:
						self.gone += 1 # its frames already replaced in the feed
						return None
					if self.scene is not None:
						self.scene.Learn(row)
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

	def Showing3D(self):
		return self.scene is not None and self.showScene

	def Size(self):
		# The view's size, width x height: its window's (drawn at its own pixels: never stretched), or --size
		return self.windowSize or self.args.size

	def Layout(self, shape):
		key = (len(self.Shown()) + self.Showing3D(), shape, self.Size(), self.args.scale, self.PanelLines())
		if key != self.layoutKey:
			self.layoutKey = key
			self.layout = Layout(key[0], shape, key[2], self.args.scale, key[4])
		return self.layout

	def Canvas(self, size):
		# A black image of size (height, width), or of the window's size when it is larger
		h, w = size
		if self.windowSize:
			w, h = max(w, self.windowSize[0]), max(h, self.windowSize[1])
		return np.zeros((h, w, 3), np.uint8)

	def Fit(self):
		# The window's size (width, height) that shows the view without the black margins
		return None if self.layout is None else (self.layout.size[1], self.layout.size[0])

	def PanelLines(self):
		# Lines in the strip below the frames, when it is one (the panel's lines wrap), the buttons' two
		if not self.panel:
			return 0
		animals = int(self.description.get("animals") or 0) if self.calibration else 0
		# The rig's line and a warning or two; with 3D, each identity's line and its strip, the strip's legend
		return 12 + (2 * animals + 4 if self.calibration else 0)

	def Compose(self, frames, row):
		start = time.perf_counter()
		shown = self.Shown()
		layout = self.Layout(frames[0].image.shape)
		# The view in the middle of the window's image: where things are (cells, sceneCell) is in the
		# window's image, the layout's cells in the view
		full = self.Canvas(layout.size)
		oy, ox = (full.shape[0] - layout.size[0]) // 2, (full.shape[1] - layout.size[1]) // 2
		self.origin = (oy, ox)
		canvas = full[oy:oy + layout.size[0], ox:ox + layout.size[1]]
		edges = self.description.get("edges")
		cameraNames = self.description.get("cameras") or [r.cameraName for r in self.readers]
		calibNames = self.description.get("calibration_names") or cameraNames
		health = time.perf_counter()
		rig = self.rig.Status() if self.rig is not None else None
		healthS = time.perf_counter() - health
		self.cells = []
		for k, (c, frame) in enumerate(zip(shown, frames)):
			tile = Gray(frame.image, layout.tile)
			if row is not None:
				DrawCamera(tile, layout.scale, row, c, self.calibration[c] if self.calibration else None, edges,
					self.show2d, self.show3d, self.lines, self.args.bad_px)
			name = cameraNames[c] if c < len(cameraNames) else str(c)
			if c < len(calibNames) and calibNames[c] != name:
				name += " ({})".format(calibNames[c])
			text = "{}  {}  frame {}".format(c + 1, name, frame.frameNumber)
			if row is not None:
				text += "  found {}".format(int(row["found"][c]))
			if self.selected is not None: # single view: which of how many (as LUC3D)
				text += "  ({}/{})".format(c + 1, len(self.readers) if self.readers else len(self.ringNames))
			Text(tile, text, (PAD, PAD + 12))
			if len(shown) > 1 and c == self.current: # the grid: the camera v shows alone
				cv2.rectangle(tile, (1, 1), (tile.shape[1] - 2, tile.shape[0] - 2), PICKED, 2)
			health = time.perf_counter()
			self.Badge(tile, c, rig)
			healthS += time.perf_counter() - health
			y, x = layout.Cell(k)
			canvas[y:y + layout.tile[0], x:x + layout.tile[1]] = tile
			self.cells.append((c, y + oy, x + ox) + layout.tile)
		self.sceneCell = None
		if self.Showing3D():
			y, x = layout.Cell(len(shown))
			self.sceneCell = (y + oy, x + ox) + layout.tile
			self.Draw3D(full, row, frames[0].frameNumber)
		if self.panel:
			healthS += self.Panel(canvas, layout, frames, row)
		self.last = (frames, row) # drawn again when a button is clicked while paused
		self.lastCameras = shown
		self.drawMs.append((time.perf_counter() - start) * 1e3)
		self.healthMs.append(healthS * 1e3)
		return full

	def Badge(self, tile, c, rig):
		# Camera c's health on its tile, at its top right: its frames a second (green), or what is wrong, in
		# red, with a red frame round the tile
		if rig is None or c >= len(rig.cameras):
			return
		s = rig.cameras[c]
		if s.problems:
			text = "  ".join(tag for tag, _ in s.problems)
			cv2.rectangle(tile, (1, 1), (tile.shape[1] - 2, tile.shape[0] - 2), WARN, 3)
		elif s.fps is not None:
			text = "{:.1f} fps".format(s.fps)
		else:
			return
		(w, h), base = cv2.getTextSize(text, FONT, 0.45, 1)
		x, y = tile.shape[1] - w - PAD - 4, PAD + 12
		if s.problems:
			cv2.rectangle(tile, (x - 4, y - h - 4), (x + w + 4, y + base + 2), WARN, -1)
			Text(tile, text, (x, y), (255, 255, 255), 0.45, box=False)
		else:
			Text(tile, text, (x, y), FINE, 0.45)

	def Again(self):
		"""
		The last view drawn again, with what changed since (a key, a click, the
		window's size): its row, on its frames, read again from the feed for
		the cameras shown now if they are others. None if there is none, or
		those frames are gone from the feed.
		"""
		if not getattr(self, "last", None):
			return None
		frames, row = self.last
		if self.Shown() != self.lastCameras:
			frames = self.Read(frames[0].frameNumber) if self.readers is not None else None
			if frames is None:
				return None
		return self.Compose(frames, row)

	def Draw3D(self, canvas, row, frame):
		# The 3D tile, in its cell of canvas: row's animals (the row the cameras' tiles show)
		y, x, h, w = self.sceneCell
		tile = canvas[y:y + h, x:x + w]
		start = time.perf_counter()
		self.scene.Draw(tile, row, self.description.get("edges"), self.args.bad_px)
		Text(tile, "3D  frame {}".format(frame) + ("" if row is not None else "  (no results)"), (PAD, PAD + 12))
		Text(tile, "drag: turn  right-drag: move  wheel: zoom  r: reset", (PAD, PAD + 34), DIM, 0.4)
		self.moved = False
		self.sceneMs.append((time.perf_counter() - start) * 1e3)

	def In3D(self, x, y):
		if self.sceneCell is None:
			return False
		y0, x0, h, w = self.sceneCell
		return x0 <= x < x0 + w and y0 <= y < y0 + h

	def CameraAt(self, x, y):
		# The camera whose tile is at (x, y) of the view, or None
		for c, y0, x0, h, w in self.cells:
			if x0 <= x < x0 + w and y0 <= y < y0 + h:
				return c
		return None

	def Press(self, x, y, button="left"):
		"""
		A mouse button pressed at (x, y) of the view: the key of a button
		there (the panel drawn in the view), or None. Left on a camera's tile
		picks it (v shows it alone); in the 3D tile, left drags turn it, right
		or middle drags move it.
		"""
		if button == "left":
			key = self.Click(x, y)
			if key is not None:
				return key
			camera = self.CameraAt(x, y)
			if camera is not None:
				self.current = camera
			elif self.In3D(x, y):
				self.drag = ("turn", (x, y))
		elif button in ("right", "middle") and self.In3D(x, y):
			self.drag = ("move", (x, y))
		return None

	def Move(self, x, y):
		# The mouse at (x, y): a drag in the 3D tile turns or moves it (then moved is True, to draw it again)
		self.mouseAt = (x, y)
		if self.sceneCell is None: # hidden while dragged
			self.drag = None
		if self.drag is not None:
			button, (x0, y0) = self.drag
			if button == "turn":
				self.scene.Orbit(x - x0, y - y0)
			else:
				self.scene.Pan(x - x0, y - y0, self.sceneCell[3])
			self.drag = (button, (x, y))
			self.moved = True

	def Release(self):
		self.drag = None

	def Wheel(self, steps, x=None, y=None):
		# The wheel turned steps clicks (> 0: away from you) at (x, y), or where the mouse last moved: over the 3D, zoom
		if self.In3D(*((x, y) if x is not None else self.mouseAt)) and steps:
			self.scene.Zoom(steps)
			self.moved = True

	def DoubleClick(self, x, y):
		# On the 3D tile: turned back. On a camera's: that camera alone, or back to the grid (as v and g)
		if self.In3D(x, y):
			self.scene.Reset()
			self.moved = True
		else:
			camera = self.CameraAt(x, y)
			if camera is not None:
				self.current = camera
				self.Key(ord("g") if self.selected is not None else ord("v"))

	def Mouse(self, event, x, y, flags):
		"""OpenCV's mouse events, to Press, Move, Release, Wheel and DoubleClick: the key of a button clicked, or None."""
		if event == cv2.EVENT_MOUSEWHEEL:
			# Its x, y are the screen's on Windows: where the mouse last moved says where it is. Its
			# delta is flags' top 16 bits (OpenCV 5's Python has no getMouseWheelDelta)
			self.Wheel(1 if flags >> 16 > 0 else -1 if flags >> 16 < 0 else 0)
		elif event == cv2.EVENT_MOUSEMOVE:
			self.Move(x, y)
		elif event in (cv2.EVENT_LBUTTONUP, cv2.EVENT_RBUTTONUP, cv2.EVENT_MBUTTONUP):
			self.Release()
		elif event in (cv2.EVENT_LBUTTONDOWN, cv2.EVENT_RBUTTONDOWN, cv2.EVENT_MBUTTONDOWN):
			return self.Press(x, y, {cv2.EVENT_LBUTTONDOWN: "left", cv2.EVENT_RBUTTONDOWN: "right"}.get(event, "middle"))
		elif event == cv2.EVENT_LBUTTONDBLCLK:
			self.DoubleClick(x, y)
		return None

	def Buttons(self, canvas, y0, x0, width):
		# The on/off buttons, at (x0, y0) of canvas, in rows no wider than width; where each is, for
		# Click; returns the height they take
		self.buttons = []
		x, y = x0 + PAD, y0 + PAD
		buttons = [("Predictions (d)", ord("d"), self.show2d), ("Reprojections (p)", ord("p"), self.show3d),
			("Lines (l)", ord("l"), self.lines)]
		if self.scene is not None:
			buttons.append(("3D view (\\)", ord("\\"), self.showScene))
		for label, key, on in buttons:
			(tw, th), _ = cv2.getTextSize(label, FONT, 0.5, 1)
			if x > x0 + PAD and x + tw + 34 > x0 + width - PAD: # the next row
				x, y = x0 + PAD, y + 26 + PAD
			box = (x, y, x + tw + 34, y + 26)
			cv2.rectangle(canvas, box[:2], box[2:], BUTTON_ON if on else BOX, -1)
			cv2.rectangle(canvas, box[:2], box[2:], TEXT if on else DIM, 1)
			mark = (box[0] + 8, box[1] + 7, box[0] + 20, box[1] + 19) # a check box
			cv2.rectangle(canvas, mark[:2], mark[2:], TEXT if on else DIM, -1 if on else 1)
			Text(canvas, label, (box[0] + 26, box[1] + 18), TEXT if on else DIM, 0.5, box=False)
			self.buttons.append(box + (key,))
			x = box[2] + 10
		return y - y0 + 26 + PAD

	def Click(self, x, y):
		# A click at (x, y) of the window's image: the key of the button there, or None
		x, y = x - self.origin[1], y - self.origin[0] # the buttons are where they are in the view
		for x0, y0, x1, y1, key in getattr(self, "buttons", ()):
			if x0 <= x <= x1 and y0 <= y <= y1:
				return key
		return None

	def Panel(self, canvas, layout, frames, row):
		# The panel drawn in the view: its buttons, what is wrong (red), the tracking strip, then the rest.
		# Returns the seconds the warnings and the strip took
		y0, x0, h, w = layout.panel
		top = self.Buttons(canvas, y0, x0, w)
		y0, h = y0 + top, h - top
		panel = canvas[y0:y0 + h, x0:x0 + w]
		start = time.perf_counter()
		y = self.Lines(panel, self.Alerts(), PAD + 14)
		strips = self.Strips(w - 2 * PAD)
		if strips is not None and y - 14 + strips.shape[0] <= h:
			panel[y - 14:y - 14 + strips.shape[0], PAD:PAD + strips.shape[1]] = strips
			y += strips.shape[0] + 4
		took = time.perf_counter() - start
		self.Lines(panel, self.PanelText(row), y)
		return took

	def Lines(self, panel, lines, y):
		# lines, (text, colour), on panel from baseline y, wrapped; where the next would go
		w, h = panel.shape[1], panel.shape[0]
		for text, color in lines:
			for part in Wrap(text, w - 2 * PAD, 0.45):
				if y > h - 4:
					return y
				Text(panel, part, (PAD, y), color, 0.45, box=False)
				y += LINE
		return y

	def Alerts(self):
		"""What is wrong now, (text, red), for the top of the panel: each camera's feed, then the tracking."""
		lines = []
		if self.rig is not None and self.readers is not None:
			for s in self.rig.Status().cameras:
				lines += [(text, WARN) for _, text in s.problems]
		if self.tracking is not None and self.results is not None and self.lastRow \
				and time.monotonic() - self.lastRow < 2 * STALE_S:
			lines += [(text, WARN) for text in self.tracking.Alerts()]
		return lines

	def Strips(self, width):
		"""
		The tracking strip: each identity's last 10 s, an image width px wide
		(BGR), or None without 3D. Each identity's label (its error in the
		newest row), then a strip, the newest at its right: its height the
		cameras it was triangulated from, its colour its reprojection error
		(green within 10 px, yellow within --bad-px, red above; gray: no 3D;
		dark: no row from campy-live).
		"""
		if self.tracking is None:
			return None
		got = self.tracking.Arrays()
		animals = self.tracking.animals or (got[1].shape[1] if got is not None else 0)
		sw = width - STRIP_LABEL
		if not animals or sw < 20:
			return None
		step = STRIP_H + 6
		img = np.empty((animals * step + 14, width, 3), np.uint8)
		img.fill(BOX[0]) # a gray: 100 times quicker than img[:] = BOX
		fps = self.tracking.frameRate
		span = livehealth.WINDOW_S * fps
		newest = int(got[0][-1]) if got is not None else 0
		if self.readers is not None:
			newest = max(newest, min(r.Latest() for r in self.readers)) # campy-live behind: dark at the right
		# Drawn a column a frame (n), then widened to sw: a tenth of the work at 30 fps
		n = int(max(10, min(sw, round(span))))
		cols = newest - (n - 1 - np.arange(n)) * span / n # each column's frame number
		cameras = max(1, len(self.ringNames))
		# Each pixel's colour, as its index in STRIP_COLORS: the gaps between strips, a strip with no row, ...
		index = np.zeros((animals * step, n), np.uint8)
		levels = (STRIP_H - np.arange(STRIP_H))[:, None] # each pixel row's height, from the strip's foot
		bad = self.args.bad_px or np.inf
		if got is not None:
			frames, err, views = got
			idx = np.searchsorted(frames, cols, "right") - 1
			ok = (idx >= 0) & (cols - frames[np.maximum(idx, 0)] <= max(fps / 4, 1.5 * span / n))
			idx = np.maximum(idx, 0)
			e, v = err[idx], views[idx] # (n, animals)
			found = np.isfinite(e)
			with np.errstate(invalid="ignore"):
				kind = np.where(~found, 2, np.where(e <= livehealth.GOOD_PX, 3, np.where(e <= bad, 4, 5)))
			height = np.where(found, np.maximum(3, np.round(STRIP_H * np.minimum(v, cameras) / cameras)), 3)
		for a in range(animals):
			y = a * step + 2
			if got is None or a >= err.shape[1]:
				index[y:y + STRIP_H] = 1
			else:
				index[y:y + STRIP_H] = np.where((levels <= height[None, :, a]) & ok[None, :], kind[None, :, a], 1)
		colors = cv2.LUT(cv2.cvtColor(index, cv2.COLOR_GRAY2BGR), STRIP_COLORS)
		img[:animals * step, STRIP_LABEL:] = cv2.resize(colors, (sw, animals * step), interpolation=cv2.INTER_NEAREST)
		for a in range(animals):
			y = a * step + 2 + STRIP_H - 2
			now = float(got[1][-1, a]) if got is not None and a < got[1].shape[1] else float("nan")
			Text(img, "#{}".format(a), (2, y), Color(a), 0.45, box=False)
			if np.isfinite(now):
				Text(img, "{:.0f} px".format(now), (34, y), WARN if now > bad else TEXT, 0.45, box=False)
			else:
				Text(img, "no 3D", (34, y), DIM, 0.45, box=False)
		y = animals * step + 10
		Text(img, "-{:.0f} s".format(livehealth.WINDOW_S), (STRIP_LABEL, y), DIM, 0.35, box=False)
		Text(img, "now", (width - 24, y), DIM, 0.35, box=False)
		return img

	def RigText(self):
		# The rig's line for the panel: how long the recording has gone, each camera's frames a second, how far
		# apart their host times are, and what they lost since the view began
		if self.rig is None or self.readers is None:
			return None
		status = self.rig.Status()
		text = "Recording {}".format(livehealth.Duration(status.recording_s)) if status.recording_s is not None \
			else "Recording"
		text += "; frames/s: {}".format(", ".join("{} {}".format(s.name, "-" if s.fps is None else "{:.1f}".format(s.fps))
			for s in status.cameras))
		offsets = [s.offset_ms for s in status.cameras if s.offset_ms is not None]
		if len(offsets) > 1:
			text += "; host times within {:.0f} ms".format(max(offsets) - min(offsets))
		totals = {k: sum(s.totals[k] for s in status.cameras) for k in ("lost", "gaps", "late")}
		text += "; since the view began: {} lost, {} gaps, {} late".format(totals["lost"], totals["gaps"],
			totals["late"])
		return text, (WARN if any(s.problems for s in status.cameras) else TEXT)

	def PanelText(self, row):
		# The panel's lines, (text, colour): the rig, the row drawn, campy-live, and the keys
		lines = []
		rig = self.RigText()
		if rig is not None:
			lines.append(rig)
		if row is not None:
			lines.append(("Frame {}: its results here {:.1f} ms after the frame{}".format(int(row["frame"]),
				self.lastSeen, "; the last view shown {:.1f} ms after its frame".format(self.lastShown)
				if self.lastShown is not None else ""), TEXT))
			if Has3D(row) and self.calibration:
				for a in range(len(row["n_views"])):
					views = int(row["n_views"][a])
					err = np.asarray(row["reproj_px"][a], float)
					color = Color(a)
					if views >= 2 and np.isfinite(err).any():
						mean = float(np.nanmean(err))
						text = "#{}: from {} cameras, reprojection {:.1f} px".format(a, views, mean)
						if self.args.bad_px and mean > self.args.bad_px:
							text += ": above {:g} px, drawn dimmed, not to trust".format(self.args.bad_px)
							color = WARN
					else:
						text = "#{}: no 3D ({} camera{})".format(a, views, "" if views == 1 else "s")
					lines.append((text, color))
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
			lines.append(("Reprojections (dots): the 3D, projected. Predictions (open circles): this camera's 2D "
				"(white: matched to no animal). Line: from the 2D to the 3D. Label: #identity, and px between them.", DIM))
		if self.tracking is not None:
			lines.append(("Strip: each identity's last {:.0f} s. Height: the cameras it was triangulated from; green: "
				"within {:g} px, yellow: within {:g} px, red: above (dimmed and dashed); gray: no 3D.".format(
				livehealth.WINDOW_S, livehealth.GOOD_PX, self.args.bad_px or float("inf")), DIM))
		lines.append((KEYS.format(len(self.ringNames), "; \\ 3D view, r turn it back" if self.scene is not None else ""),
			DIM))
		return lines

	def Waiting(self):
		canvas = self.Canvas(self.Size()[::-1]) # the view's size: OpenCV sizes a new window to its first image
		Text(canvas, "Waiting for the live feed: {}".format(", ".join(self.ringNames)), (PAD, 40))
		Text(canvas, "(campy-acquire with liveFeed True)", (PAD, 70), DIM)
		Text(canvas, "campy-live's results: {}".format("there" if self.results else "not yet"), (PAD, 100), DIM)
		return canvas

	def Key(self, key):
		"""
		A key pressed in the window: its character's code, or "left",
		"right", "up", "down" for the arrows. False to quit. As LUC3D: v one
		camera alone (single view; the one last picked), g every camera (the
		grid), the arrows the previous or next camera (in the grid: which one
		v shows), \\ the 3D view on or off (it stays beside a single view).
		"""
		n = len(self.readers) if self.readers else len(self.ringNames)
		if key in (ord("q"), 27):
			return False
		if key in ("left", "right", "up", "down"):
			step = 1 if key in ("right", "down") else -1
			self.current = ((self.selected if self.selected is not None else self.current) + step) % max(1, n)
			if self.selected is not None:
				self.selected = self.current
		elif isinstance(key, str):
			pass
		elif ord("1") <= key <= ord("9") and key - ord("1") < n:
			self.selected = self.current = key - ord("1")
		elif key in (ord("0"), ord("a"), ord("g")):
			self.selected = None
		elif key == ord("v"):
			if self.selected is None:
				self.selected = min(self.current, n - 1)
		elif key == ord("d"):
			self.show2d = not self.show2d
		elif key == ord("p"):
			self.show3d = not self.show3d
		elif key == ord("l"):
			self.lines = not self.lines
		elif key == ord("\\"):
			self.showScene = not self.showScene
		elif key == ord("r") and self.scene is not None:
			self.scene.Reset()
			self.moved = True
		return True

	def Delivered(self, image, frame, row):
		# A view shown: how late, and saved (every --png-every-th result)
		if row is None:
			return
		self.lastShown = (time.perf_counter_ns() - int(np.max(row["written_ns"]))) / 1e6
		self.shownMs.append(self.lastShown)
		self.count += 1
		if self.args.png and self.count % self.args.png_every == 0:
			cv2.imwrite(os.path.join(self.args.png, "view_{:08d}.png".format(frame)), image)

	def Save(self, image):
		# s: the view as a PNG, into --png or the current folder
		path = os.path.join(self.args.png or ".", "view_{}.png".format(datetime.now().strftime("%H-%M-%S")))
		cv2.imwrite(path, image)
		Say("Saved {}.".format(path))

	def HealthSummary(self):
		# The rig (the last recording's) and the tracking, for the summary
		out = {}
		if self.rig is not None:
			status = self.rig.Status()
			out["recording_s"] = status.recording_s
			def Rates(rates, q):
				return float(np.percentile(rates, q)) if len(rates) else None
			out["rig"] = [{"camera": s.name, "fps_p50": Rates(rates, 50), "fps_p1": Rates(rates, 1), "fps_last": s.fps,
				"offset_ms": s.offset_ms, "frames_off": s.frames_off, **s.totals,
				"warnings": [text for _, text in s.problems]} for s, rates in zip(status.cameras, self.rig.rates)]
		if self.tracking is not None:
			out["tracking"] = self.tracking.Summary()
		return out

	def Summary(self, seconds):
		def P(values, q):
			return float(np.percentile(values, q)) if len(values) else float("nan")
		# The timings are of the last KEPT views
		return {"seconds": seconds, "results_taken": self.rows, "results_drawn": self.rows - self.gone,
			"results_gone": self.gone, "results_passed_over": self.passed, "frames_alone": self.raw,
			"draw_ms": {"p50": P(self.drawMs, 50), "p99": P(self.drawMs, 99)},
			"draw_3d_ms": {"p50": P(self.sceneMs, 50), "p99": P(self.sceneMs, 99)},
			"draw_health_ms": {"p50": P(self.healthMs, 50), "p99": P(self.healthMs, 99)},
			"rig_read_ms": {"p50": P(self.rigMs, 50), "p99": P(self.rigMs, 99)},
			**({"qt_panel_ms": {"p50": P(self.panelMs, 50), "p99": P(self.panelMs, 99)}} if self.panelMs else {}),
			**self.HealthSummary(),
			"results_here_after_frame_ms": {"p50": P(self.resultMs, 50), "p99": P(self.resultMs, 99)},
			"shown_after_frame_ms": {"p50": P(self.shownMs, 50), "p99": P(self.shownMs, 99)}}


# ---------------------------------------------------------------- the program

def WindowSize():
	# The view window's inside, (width, height), or None (minimized, or no window)
	try:
		_, _, w, h = cv2.getWindowImageRect(TITLE)
	except (cv2.error, AttributeError):
		return None
	return (w, h) if w > 0 and h > 0 else None


def Raise():
	# The window back from minimized, and in front of the others: a program started from a script may not take
	# the focus on Windows, but a window put on top, then not, stays in front. True if it was minimized
	restored = False
	if os.name == "nt":
		try:
			import ctypes
			user32 = ctypes.windll.user32
			user32.FindWindowW.restype = ctypes.c_void_p
			user32.IsIconic.argtypes = [ctypes.c_void_p]
			user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
			hwnd = user32.FindWindowW(None, TITLE)
			if hwnd and user32.IsIconic(hwnd):
				user32.ShowWindow(hwnd, 9) # SW_RESTORE
				restored = True
		except Exception:
			pass
	try:
		cv2.setWindowProperty(TITLE, cv2.WND_PROP_TOPMOST, 1)
		cv2.setWindowProperty(TITLE, cv2.WND_PROP_TOPMOST, 0)
	except (cv2.error, AttributeError):
		pass
	return restored


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


# OpenCV's codes of the arrow keys (pollKey, waitKeyEx): Windows, GTK, macOS
ARROWS = {0x250000: "left", 0x260000: "up", 0x270000: "right", 0x280000: "down",
	65361: "left", 65362: "up", 65363: "right", 65364: "down", 63234: "left", 63232: "up", 63235: "right", 63233: "down"}


def Run(args):
	cv2.setNumThreads(1) # a few small images: not worth taking every core from campy
	if args.priority == "low":
		LowerPriority()
	if args.png:
		os.makedirs(args.png, exist_ok=True)
	window = not args.no_window
	if window and args.gui != "opencv":
		try:
			from campy import liveviewqt
		except ImportError as e:
			if args.gui == "qt":
				Say("--gui qt, but Qt cannot be loaded here: {}.".format(e))
				return 1
			Say("No Qt here ({}): the window is OpenCV's (pip install PyQt5 for Qt's).".format(e))
		else:
			return liveviewqt.Run(args)
	return RunOpenCV(args, window)


def RunOpenCV(args, window):
	if window:
		try:
			# Drawn at the window's own size (Windows' OpenCV stretches an image to its window, keeping no ratio)
			cv2.namedWindow(TITLE, cv2.WINDOW_NORMAL)
			cv2.resizeWindow(TITLE, *args.size) # what the screen allows of it
		except cv2.error as e:
			Say("Cannot open a window ({}): this OpenCV has no window support, or there is no desktop. "
				"Use --no-window --png <folder>.".format(str(e).strip().splitlines()[-1]))
			return 1
	viewer = Viewer(args)
	start = time.monotonic()
	paused, image = False, None
	raised = None # whether there was a feed when it was last put in front
	clicks = [] # the keys of the buttons clicked
	if window:
		def Mouse(event, x, y, flags, param):
			key = viewer.Mouse(event, x, y, flags)
			if key is not None:
				clicks.append(key)
		cv2.setMouseCallback(TITLE, Mouse)
	Say("Showing {}; results from {}.".format(", ".join(viewer.ringNames), viewer.resultsName))
	try:
		while True:
			if args.seconds and time.monotonic() - start >= args.seconds:
				break
			if window:
				size = WindowSize()
				if size != viewer.windowSize:
					viewer.windowSize = size
					if size and image is not None and (paused or viewer.readers is None): # drawn again at the new size
						again = viewer.Waiting() if viewer.readers is None else viewer.Again()
						if again is not None:
							image = again
							cv2.imshow(TITLE, image)
			got = None if paused else viewer.Step()
			if got is not None:
				image, frame, row = got
				if window:
					cv2.imshow(TITLE, image)
					feed = viewer.readers is not None
					if raised is None or (feed and not raised):
						# In front when it opens, and when a recording's frames come; at --size when it opens,
						# or comes back from minimized (started minimized, the first size never took)
						if Raise() or raised is None:
							cv2.resizeWindow(TITLE, *args.size)
					raised = feed
				viewer.Delivered(image, frame, row)
			if window:
				# The window's events, without waiting: waitKey(1) waits a whole clock tick on Windows (15.6 ms)
				key = cv2.pollKey() if hasattr(cv2, "pollKey") else cv2.waitKey(1)
				if key < 0 and clicks:
					key = clicks.pop(0) or -1
				if key >= 0:
					key = ARROWS.get(key, key & 0xFF)
					if key == ord(" "):
						paused = not paused
					elif key == ord("z") and viewer.Fit():
						cv2.resizeWindow(TITLE, *viewer.Fit())
					elif key == ord("s") and image is not None:
						viewer.Save(image)
					elif not viewer.Key(key):
						break
					elif paused and image is not None:
						again = viewer.Again() # the same row, with the change
						if again is not None:
							image = again
							cv2.imshow(TITLE, image)
				if viewer.moved and image is not None and viewer.sceneCell is not None and getattr(viewer, "last", None):
					# Turned: the 3D tile again, of the row the cameras' tiles show
					viewer.Draw3D(image, viewer.last[1], viewer.last[0][0].frameNumber)
					cv2.imshow(TITLE, image)
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
	return Finish(viewer, time.monotonic() - start, args)


def Finish(viewer, seconds, args):
	# What it did, said and in --summary
	summary = viewer.Summary(seconds)
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


def Direction(text):
	try:
		v = np.array([float(x) for x in text.split(",")])
	except ValueError:
		v = np.zeros(0)
	if v.shape != (3,) or not np.linalg.norm(v) > 0:
		raise argparse.ArgumentTypeError("give it as x,y,z, e.g. 0,-1,0")
	return v


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
	p.add_argument("--no-3d", action="store_true", help="Start without the 3D view (\\ shows it).")
	p.add_argument("--gui", default="auto", choices=["auto", "qt", "opencv"], help="The window: Qt's (PyQt5), or "
		"OpenCV's; auto: Qt's when PyQt5 is there.")
	p.add_argument("--up", type=Direction, default=None, help="The world's up in the calibration, as x,y,z "
		"(default: away from where the cameras look, when they look down).")
	p.add_argument("--bad-px", type=float, default=livehealth.BAD_PX, help="An identity whose reprojection error "
		"(campy-live's, mean over the nodes) is above this is drawn dimmed and dashed, its label red (0: never).")
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
