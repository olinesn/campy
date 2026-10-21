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
are drawn again only when the view of them moves).

On its floor, each identity's trail: where its middle went over the last
--trail-seconds (60), in its colour, fading with age, broken where it was
not seen for a second (t, or the panel's button, shows or hides them). And
the occupancy (o, or --occupancy at first): the floor's cells (a quarter of
a grid square) where the animals' middles have been, the more often the
lighter, since campy-live's results began or over the last --heat-minutes.
Both count only well triangulated animals (as the floor's levelling: 2
cameras or more, reprojected within 10 px), so false 3D from a detection
outside the cage in one camera does not paint them. The occupancy is drawn
into the grid's picture, again once a second (about 3 ms); the trails each
view (about 1.3 ms for 3 mice over 60 s on the ChronicRecord PC).

With the trails on (t), the cameras' tiles have trails too, as LUC3D's node
trails: each node's path over the last quarter second (--camera-trail-seconds;
LUC3D's lengths: a quarter, half, 1 or 2 s), one segment from each result to
the one before, the older thinner, more transparent and darker, in its
identity's hue (--trail-shade: dark, its colour times 0.6, by default; very
dark to very light), under the skeletons: the predictions' (d) and the
reprojections' (p). The Qt panel picks the length and the shade.

The 3D view is its own drawing with numpy and cv2, in this window, rather
than matplotlib or a browser: no other package or program, the same row as
the cameras' tiles by construction, and cheap enough to draw at 30 a second.

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
  wrong match. Its error's median over the last half second, not one row's:
  on 2026-10-20's recordings a mouse's error crossed 20 px 100 to 180 times
  a minute, and the dimming blinked several times a second; the median
  crosses it 13 to 26 times a minute, dimmed as much of the time (within 3
  points). On the ChronicRecord PC this all took 1.4 ms of each view's
  drawing (10.5 ms, from 9.1): 1.1 ms the warnings, the tiles' labels and
  the strip, the rest the dashes (2 of the 3 mice above 20 px most of the
  time); reading the rings' slots 0.4 ms, 20 times a second.

Buttons in the panel turn the predictions (the 2D), the reprojections (the
3D) and the lines on or off, as do the keys d, p and l, the 3D view, as
does \\ (--no-3d: off at first), and its trails and occupancy, as do t and o.

Every camera (the grid) or one alone (single view), with LUC3D's keys: v one
camera alone, the one last picked (clicked, or the last shown alone; an
outline marks it in the grid), with the 3D beside it unless \\ closed it; g
the grid; the arrows the previous or next camera (wrapping; in the grid,
which one v shows); a double click on a camera's tile shows it alone, or the
grid again. Also 1-9 that camera alone, a the grid.

Zoom, as LUC3D's: the wheel over a camera's tile zooms its picture about
the mouse (a drag on it then moves the picture; a click without one still
picks the camera); + or = and - or _ zoom every camera shown (1.2 times a
press, about each one's middle); 0 shows every camera's whole frame again;
shift 0 one frame pixel for one screen pixel (again: the whole frames). The
drawing goes with the zoom: it is drawn on the part of the frame shown, cut
out before it is scaled, so a zoomed tile costs no more than a whole one.
Each camera keeps its zoom from the grid to the single view and back.

Other keys, in its window: z the window to the view's size (no black
margins); space pause (the keys, buttons and the 3D view's mouse still work
on the paused view); s save the view as a PNG (into --png, or the current
folder); q or Esc quit.

Bookmarks
---------
m (or the Qt panel's Bookmark button) marks the moment, for finding it later
in the videos (and in LUC3D); Shift+1-9 (or the panel's 1-9) marks it as
event 1-9, for kinds of event of your own. A bookmark is a line of a CSV
file (Bookmarks says its columns): the frame number shown in the view
(campy's, as in frametimes.npy), each camera's newest frame number and when
it reached the computer, the time, and the note typed in the Qt panel. The
line is written, and flushed to the disk, as the key is pressed: a crash
loses none. The file is bookmarks.csv in the recording's folder, which the
cameras' live feeds say (campy-acquire from this campy on), or else
--bookmarks. Paused, a bookmark is of the frame shown. The view is outlined
for a moment; the panel lists the last few (OpenCV's: the last). OpenCV's
window reads Shift+1-9 as the characters they type on a US keyboard
(!@#$%^&*(); Qt's reads the keys themselves on Windows.

Qt's window (campy/liveviewqt.py; PyQt5, as campy's preview windows) shows
the same view beside a panel of Qt's own checkboxes, buttons and text, and
draws the tiles' text (names, frames, labels) over the view in Qt's fonts,
sharp at any size, where OpenCV's would be: the Viewer hands it over (Words)
instead of drawing it. OpenCV's window draws the panel and the text into
the view. Both take the same keys and mouse.

The window opens at --size, or what the screen allows of it, and comes to
the front when it opens and when a recording's frames come (also from
minimized). The view is drawn at the window's own size, one pixel for one:
resized, it is laid out again (the frames in their ratio, at most their own
size, in the middle of the window), never stretched.
"""

import argparse, csv, functools, json, os, sys, time
from collections import deque
from datetime import datetime

import numpy as np
import cv2

from campy import cpus, livefeed, livehealth, liveresults

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
KEYS = ("Keys: v one camera, g all (grid), arrows the previous or next camera, 1-{} that camera; wheel or + and - "
	"zoom a camera (drag: move it), 0 the whole frame, shift 0 one pixel for one; d predictions, p reprojections, "
	"l lines{}; c campy-live on the fast CPUs or not; z window to the view; m bookmark (Shift+1-9: event 1-9); space pause, s save, q quit")
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
	# The identities whose 3D, in row alone, is off by more than bad_px (mean reprojection error over the
	# nodes): {identity: px}. The view dims by the last half second's (livehealth.Tracking.Dimmed): steadier
	if not Has3D(row) or not bad_px:
		return {}
	errors = livehealth.Errors(row)
	with np.errstate(invalid="ignore"):
		return {int(a): float(errors[a]) for a in np.nonzero(errors > bad_px)[0]}


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


def Place(px, scale, origin=(0, 0)):
	"""
	Pixels of the frame (..., 2) -> where they are on a tile showing the
	frame at scale from origin (the frame's point at the tile's top left
	corner), as cv2.resize puts them: a pixel spans 1 px about its middle, at
	whole numbers, so the frame's [0, width] is the tile's [0, width * scale]
	(and a pixel x of the frame lands at (x + 0.5 - origin) * scale - 0.5).
	"""
	return (np.asarray(px, float) + 0.5 - origin) * scale - 0.5


def Points(px, scale, origin=(0, 0)):
	# Pixels of the frame (N, 2) -> which are drawable, and where in the tile (Place), with SHIFT subpixel bits
	px = Place(px, scale, origin)
	ok = np.isfinite(px).all(-1) & (np.abs(px) < 1e5).all(-1)
	q = np.zeros(px.shape, np.int64)
	q[ok] = np.round(px[ok] * (1 << SHIFT))
	return ok, [tuple(int(v) for v in p) for p in q]


def Centre(px):
	# The mean of the nodes there are, or None
	px = np.asarray(px, float)
	ok = np.isfinite(px).all(-1)
	return px[ok].mean(0) if ok.any() else None


def DrawSkeleton(img, px, scale, edges, color, radius, thickness, filled, dashed=False, origin=(0, 0)):
	"""
	An animal's nodes (N, 2, pixels of the frame) and edges (dashed: not to trust), on img (a tile of the frame
	at scale, from origin).
	"""
	ok, pts = Points(px, scale, origin)
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


def Text(img, text, at, color=TEXT, size=0.5, box=True, words=None):
	# Text at (x, y) of its baseline's left end, on a dark box (an outline drawn thicker than the
	# text comes out wider than it in OpenCV 5's font, and off to one side). With words (a tile's
	# Pen), not drawn: handed to the window, to draw over the image in its own fonts
	if words is not None:
		words.Add(text, at, color, size, box)
		return
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


class Words:
	"""
	The view's text, for a window that draws it over the image in its own
	fonts (Qt's) rather than have it drawn into the image with OpenCV's.
	Items: (text, x, y, colour (BGR), size (OpenCV's font scale), box, clip):
	x, y the left end of the baseline in the view's image, as Text's (x None:
	10 px after the item before), box whether on a dark box, clip the tile's
	(x, y, width, height), which the text is cut to. Tile gives the Pen a
	tile's text goes to; drawing the tile again replaces its items.
	"""

	def __init__(self):
		self.parts = {}

	def Tile(self, name, y, x, h, w):
		self.parts[name] = []
		return Pen(self.parts[name], x, y, (x, y, w, h))

	def Items(self):
		return [item for part in self.parts.values() for item in part]


class Pen:
	# Where a tile's text goes: its items, at the tile's place in the view's image
	def __init__(self, items, x, y, clip):
		self.items, self.x, self.y, self.clip = items, x, y, clip

	def Add(self, text, at, color, size, box):
		x = None if at[0] is None else int(at[0]) + self.x
		self.items.append((text, x, int(at[1]) + self.y, tuple(int(v) for v in color), size, box, self.clip))


def Has3D(row):
	return row is not None and "points3d" in row.dtype.names


def DrawCamera(img, scale, row, c, camera=None, edges=None, show2d=True, show3d=True, lines=True, origin=(0, 0),
		frame=None, words=None, bad_px=livehealth.BAD_PX, bad=None):
	"""
	Camera c's part of a results row on img, its frame at scale (a number, or
	x and y) from origin (the frame's pixel at the tile's top left: zoomed,
	the part of the frame shown): its 2D detections, and with camera (its
	pose3d.Camera) each animal's 3D projected into it; the identities in bad
	({identity: px}; default: those whose reprojection error in row is above
	bad_px) dimmed and dashed, their labels red. frame, the frame's
	width and height (default: what the tile shows): the 3D off it is listed
	at the tile's foot. words: a Pen for the text (Text). Returns {identity:
	mean px, in this camera, between its projected 3D and the detection it
	was made from (NaN: no detection here)} for the identities with 3D.
	"""
	scale = np.broadcast_to(np.asarray(scale, float), (2,))
	origin = np.asarray(origin, float)
	if frame is None:
		frame = origin + np.array(img.shape[1::-1]) / scale
	if bad is None:
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
				thickness=1, filled=False, origin=origin)
	offsets = {}
	for a, px in projected.items():
		i = int(members[a])
		d = np.linalg.norm(px - kp[i], axis=-1) if 0 <= i < len(kp) else np.full(len(px), np.nan)
		offsets[a] = float(np.nanmean(d)) if np.isfinite(d).any() else float("nan")
		if lines and show2d and show3d and 0 <= i < len(kp):
			a2, a3 = Centre(kp[i]), Centre(px)
			if a2 is not None and a3 is not None:
				ok, (p2, p3) = Points(np.stack([a2, a3]), scale, origin)
				if ok.all():
					cv2.line(img, p2, p3, OUTLINE, 3, cv2.LINE_AA, SHIFT)
					cv2.line(img, p2, p3, Dimmed(Color(a)) if a in bad else Color(a), 1, cv2.LINE_AA, SHIFT)
	if show3d:
		for a, px in projected.items():
			DrawSkeleton(img, px, scale, edges, Dimmed(Color(a)) if a in bad else Color(a), radius=RADIUS_3D,
				thickness=2, filled=True, dashed=a in bad, origin=origin)
		away = [] # 3D that lands nowhere on the frame (or behind the camera)
		for a, px in projected.items():
			px = np.asarray(px, float)
			with np.errstate(invalid="ignore"):
				onFrame = np.isfinite(px).all(-1) & (px >= 0).all(-1) & (px < frame).all(-1)
				p = Place(px, scale, origin)
				ok = onFrame & (p >= 0).all(-1) & (p[:, 0] < img.shape[1]) & (p[:, 1] < img.shape[0])
			if not onFrame.any():
				away.append(a)
			if not ok.any(): # off the frame, or off the part of it shown
				continue
			p = p[ok]
			label = "#{}".format(a)
			if np.isfinite(offsets[a]):
				label += "  {:.0f} px".format(offsets[a])
			views = int(row["n_views"][a])
			if views < len(row["members"][a]): # not every camera
				label += "  ({} cam{})".format(views, "" if views == 1 else "s")
			if a in bad: # not to trust: campy-live's error, over every camera
				label = "#{}  bad 3D: {:.0f} px".format(a, bad[a])
			Text(img, label, (p[:, 0].min(), p[:, 1].min() - 8), WARN if a in bad else Color(a), 0.45, words=words)
		x = PAD
		for k, a in enumerate(away):
			text = "{}#{}".format("3D off this image: " if k == 0 else "", a)
			# The window's fonts are not OpenCV's: there, each after the one before
			Text(img, text, (x if k == 0 or words is None else None, img.shape[0] - PAD), Color(a), 0.45, words=words)
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


def Gray(img, shape, scale=None, origin=(0, 0)):
	"""
	A frame (height x width x channels, mono or RGB) on a tile of shape
	(height, width), as BGR, at scale from origin as Place puts the drawing
	(default: the whole frame in the tile). Halved by averaging while it is
	twice the tile's size or more, then interpolated (averaging at a ratio
	that is not a whole number took 8 ms for a 1280x1024 frame, this 0.07
	ms). Zoomed, only the part of the frame the tile shows is halved and
	interpolated (cv2.warpAffine, which places it exactly: 0.4 ms for a tile
	of 600 x 480 against cv2.resize's 0.1), so it costs no more than the
	whole frame.
	"""
	h, w = shape
	H, W = img.shape[:2]
	sx, sy = (w / W, h / H) if scale is None else np.broadcast_to(np.asarray(scale, float), (2,))
	x0, y0 = origin
	if img.ndim == 3 and img.shape[2] == 1:
		img = img[:, :, 0]
	if x0 == 0 and y0 == 0 and abs(sx * W - w) < 1e-6 and abs(sy * H - h) < 1e-6:
		# The whole frame: as cv2.resize puts it
		while img.shape[0] >= 2 * h and img.shape[1] >= 2 * w:
			img = cv2.resize(img, (img.shape[1] // 2, img.shape[0] // 2), interpolation=cv2.INTER_AREA)
		if img.shape[:2] != (h, w):
			img = cv2.resize(img, (w, h), interpolation=cv2.INTER_LINEAR)
	else:
		# The pixels shown, and one more each side for the interpolation
		a, c = max(0, int(np.floor(x0)) - 1), max(0, int(np.floor(y0)) - 1)
		b, d = min(W, int(np.ceil(x0 + w / sx)) + 2), min(H, int(np.ceil(y0 + h / sy)) + 2)
		img = img[c:d, a:b]
		# A pixel x of img is x * kx + bx of the frame (likewise y), as img is halved
		kx = ky = 1.0
		bx = by = 0.0
		while sx * kx <= 0.5 and sy * ky <= 0.5 and min(img.shape[:2]) >= 4:
			img = img[:img.shape[0] // 2 * 2, :img.shape[1] // 2 * 2] # even: each pixel the mean of 2 x 2
			img = cv2.resize(img, (img.shape[1] // 2, img.shape[0] // 2), interpolation=cv2.INTER_AREA)
			bx, by = bx + 0.5 * kx, by + 0.5 * ky # pixel 0 of the half is the middle of 0 and 1
			kx, ky = 2 * kx, 2 * ky
		# The tile's pixel u shows img's pixel x where u = Place(a + bx + kx x)
		M = np.array([[sx * kx, 0, sx * (a + bx + 0.5 - x0) - 0.5], [0, sy * ky, sy * (c + by + 0.5 - y0) - 0.5]])
		if M[0, 0] == 1 and M[1, 1] == 1 and (M[:, 2] == np.round(M[:, 2])).all() and (M[:, 2] <= 0).all() and \
				img.shape[0] >= h - M[1, 2] and img.shape[1] >= w - M[0, 2]:
			img = img[int(-M[1, 2]):int(-M[1, 2]) + h, int(-M[0, 2]):int(-M[0, 2]) + w] # one for one: as it is
		else:
			img = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
	if img.ndim == 2:
		return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
	return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)


ZOOM_KEY, ZOOM_WHEEL = 1.2, 1.1 # LUC3D's: each + or - press, each click of the wheel
ZOOM_MOST = 8 # screen px per frame px, at most


class Zoom:
	"""
	A camera's zoom, as LUC3D's: how many times the fit (the whole frame in
	its tile) it is shown, about which pixel of the frame (None: its middle),
	or native, one frame pixel for one screen pixel (held through resizes).
	It is of the frame, not the tile: kept from the grid to the single view
	and back. Window gives where the tile is on the frame. Its points are
	from the frame's and the tile's top left edges, as Place's origin (the
	frame is [0, width] wide).
	"""

	def __init__(self):
		self.times, self.centre, self.native = 1.0, None, False

	def Window(self, frame, tile, fit):
		"""
		The tile's scale (x, y) and origin (the frame's pixel at its top
		left), for a frame (height, width) on a tile (height, width) that the
		whole frame fits at fit (x, y).
		"""
		H, W = frame[:2]
		h, w = tile[:2]
		if self.native:
			scale = np.array([1.0, 1.0]) if fit[0] < 1 else np.asarray(fit, float) # a window as large as the frames: the fit
		else:
			scale = np.asarray(fit, float) * max(1.0, self.times)
		seen = np.array([w, h]) / scale
		centre = np.array([W, H]) / 2.0 if self.centre is None else np.asarray(self.centre, float)
		origin = np.clip(centre - seen / 2, 0, np.maximum(0, np.array([W, H]) - seen))
		if self.native:
			origin = np.round(origin) # on the screen's pixels: none resampled
		return scale, origin

	def Times(self, fit):
		return 1.0 / fit[0] if self.native else self.times

	def About(self, factor, at, frame, tile, fit):
		# factor times nearer (< 1: farther), the tile's pixel at (x, y) staying where it is
		scale, origin = self.Window(frame, tile, fit)
		under = origin + np.asarray(at, float) / scale # the frame's pixel there
		self.times = float(np.clip(self.Times(fit) * factor, 1.0, max(1.0, ZOOM_MOST / fit[0])))
		self.native = False
		scale = np.asarray(fit, float) * self.times
		self.centre = under - np.asarray(at, float) / scale + np.array(tile[1::-1]) / scale / 2
		self.Clamp(frame, tile, fit)

	def Pan(self, dx, dy, frame, tile, fit):
		# Dragged dx, dy tile px: the frame with the mouse
		scale, origin = self.Window(frame, tile, fit)
		self.centre = origin + np.array(tile[1::-1]) / scale / 2 - np.array([dx, dy]) / scale
		self.Clamp(frame, tile, fit)

	def Clamp(self, frame, tile, fit):
		# The centre where the tile's window puts it: no further than the frame's edge
		scale, origin = self.Window(frame, tile, fit)
		self.centre = origin + np.array(tile[1::-1]) / scale / 2

	def Native(self, on, frame=None, tile=None, fit=None):
		# 1:1 about what it shows now, or off: the whole frame again (as LUC3D)
		if on:
			if frame is not None:
				self.Clamp(frame, tile, fit)
			self.native = True
		else:
			self.Reset()

	def Reset(self):
		self.times, self.centre, self.native = 1.0, None, False

	def Zoomed(self):
		return self.native or self.times > 1.0


# ---------------------------------------------------------------- the 3D view

BACKDROP, GRID, GRID_MID, FRUSTUM = (34, 28, 24), (66, 62, 58), (110, 104, 98), (200, 200, 200)
FOV = 35 # degrees: the virtual camera's view across the tile (a long lens: little perspective)
GOOD_PX = 10 # a 3D node reprojected within this (px) tells where the floor is
TRAIL_BANDS = 8 # a trail's ages, each drawn in one shade, from its identity's colour to near the backdrop
TRAIL_GAP_S = 1.0 # an animal not well triangulated for this long: its trail breaks
HEAT = (225, 225, 225) # the occupancy's most visited cells (BGR); the least, near BACKDROP
HEAT_EVERY_S = 1.0 # the occupancy drawn again this often (in the background, drawn once for each)


class Trail:
	"""An animal's middles, (time, 3D), oldest first, in arrays (drawn whole, at 30 a second)."""

	def __init__(self, capacity):
		self.T, self.X, self.n = np.empty(capacity), np.empty((capacity, 3)), 0

	def Add(self, t, X, since):
		if self.n == len(self.T): # full: what is older than since goes, or the oldest quarter
			k = max(len(self.T) // 4, int(np.searchsorted(self.T[:self.n], since)))
			self.T[:self.n - k], self.X[:self.n - k] = self.T[k:self.n].copy(), self.X[k:self.n].copy()
			self.n -= k
		self.T[self.n], self.X[self.n] = t, X
		self.n += 1

	def Since(self, t):
		i = int(np.searchsorted(self.T[:self.n], t))
		return self.T[i:self.n], self.X[i:self.n]


class Occupancy:
	"""
	Where the animals' middles have been on the floor: counts in square
	cells, cell wide, n each way from o (a point of the floor) along the
	floor's directions (basis: o, e1, e2). Since it began (minutes 0), or
	over the last minutes (to within a tenth of them).
	"""

	def __init__(self, basis, cell, n, minutes=0):
		self.basis, self.cell, self.n = basis, cell, n
		self.window = 60.0 * minutes
		self.total = np.zeros((2 * n, 2 * n)) # [i along e1, j along e2]
		self.bins = deque() # with a window: (start, counts) of each tenth of it

	def Cells(self, X):
		# Points (N, 3) -> their cells' i, j, and which are on the grid
		o, e1, e2 = self.basis
		d = np.asarray(X, float).reshape(-1, 3) - o
		i = np.floor(d @ e1 / self.cell).astype(np.int64) + self.n
		j = np.floor(d @ e2 / self.cell).astype(np.int64) + self.n
		ok = (i >= 0) & (i < 2 * self.n) & (j >= 0) & (j < 2 * self.n)
		return i[ok], j[ok]

	def Add(self, X, t):
		i, j = self.Cells(X)
		if not len(i):
			return
		if self.window:
			if not self.bins or t - self.bins[-1][0] >= self.window / 10:
				self.bins.append((t, np.zeros_like(self.total)))
			np.add.at(self.bins[-1][1], (i, j), 1)
		np.add.at(self.total, (i, j), 1)

	def Expire(self, t):
		# The tenths wholly older than the window, gone
		while self.window and self.bins and self.bins[0][0] + self.window / 10 <= t - self.window:
			self.total -= self.bins.popleft()[1]
		if self.window and not self.bins:
			self.total[:] = 0 # not a sum's rounding left

	def Rebase(self, basis):
		# The floor levelled again. Along up only: the same cells. Turned: each cell's count to the new
		# floor's cells about its middle (shared between the 4 nearest, as bilinear interpolation does: none left empty)
		o, e1, e2 = self.basis
		self.basis = basis
		if np.allclose(e1, basis[1]) and np.allclose(e2, basis[2]) and \
				abs((basis[0] - o) @ e1) < 1e-6 * self.cell and abs((basis[0] - o) @ e2) < 1e-6 * self.cell:
			return
		k = (np.arange(2 * self.n) + 0.5 - self.n) * self.cell
		d = (o + k[:, None, None] * e1 + k[None, :, None] * e2).reshape(-1, 3) - basis[0]
		u = d @ basis[1] / self.cell + self.n - 0.5 # the new cells', in which cell i's middle is at i
		v = d @ basis[2] / self.cell + self.n - 0.5
		i0, j0 = np.floor(u).astype(np.int64), np.floor(v).astype(np.int64)
		fu, fv = u - i0, v - j0
		def Moved(counts):
			moved = np.zeros_like(counts)
			counts = counts.reshape(-1)
			for di, dj, w in ((0, 0, (1 - fu) * (1 - fv)), (1, 0, fu * (1 - fv)), (0, 1, (1 - fu) * fv), (1, 1, fu * fv)):
				i, j = i0 + di, j0 + dj
				ok = (i >= 0) & (i < 2 * self.n) & (j >= 0) & (j < 2 * self.n) & (counts > 0)
				np.add.at(moved, (i[ok], j[ok]), counts[ok] * w[ok])
			return moved
		self.total = Moved(self.total)
		self.bins = deque((start, Moved(counts)) for start, counts in self.bins)


# The cameras' node trails, as LUC3D's (talmolab/luc3d ui/overlays.js drawNodeTrails, after SLEAP's
# TrackTrailOverlay): its lengths, shades and ramp
CAMERA_TRAILS = ((0.0, "Off"), (0.25, "¼ s"), (0.5, "½ s"), (1.0, "1 s"), (2.0, "2 s")) # seconds
TRAIL_SHADES = {"very-dark": -0.65, "dark": -0.4, "normal": 0.0, "light": 0.4, "very-light": 0.7}
TRAIL_AGE_SHIFT = 0.45 # the oldest segment shaded this much further than the newest
MAX_TRAIL_FRAMES = 500


def TrailFrames(seconds, fps):
	# Frames in a trail seconds long at fps (30 while none is known): at least 1 when on, at most 500
	if seconds <= 0:
		return 0
	return int(min(MAX_TRAIL_FRAMES, max(1, round(seconds * (fps if fps and fps > 0 else 30)))))


def TrailColor(color, amount, age):
	# An identity's colour (BGR) in a trail of shade amount (< 0 darker: its channels times 1 + amount; > 0
	# lighter: toward white), age (0 the newest segment, toward 1 the oldest) moving it further the same way
	color = np.asarray(color, float)
	if amount > 0:
		f = amount + (1 - amount) * TRAIL_AGE_SHIFT * age
		color = color + (255 - color) * f
	else:
		color = color * (1 + amount) * (1 - TRAIL_AGE_SHIFT * age)
	return tuple(int(round(v)) for v in np.clip(color, 0, 255))


class CameraTrails:
	"""
	Each identity's node trails on the cameras' tiles, as LUC3D draws them:
	each node's path over the last rows taken (the last --camera-trail-seconds
	of frames, and the one shown), one segment from each row to the one before,
	the older thinner, more transparent and further shaded from the identity's
	colour (--trail-shade: dark, its channels times 0.6, by default). Two sets:
	the predictions' (each identity's detection in this camera, 1 px as they
	are drawn) and the reprojections' (its 3D projected, 2 px), each with its
	layer (d, p). A node missing in a row breaks its path there.
	"""

	def __init__(self, seconds=0.25, shade="dark"):
		self.seconds, self.shade = float(seconds), shade
		self.rows = deque() # (frame, row, {camera: (2D, 3D) per identity}), the newest last
		self.fps = None

	def Frames(self):
		return TrailFrames(self.seconds, self.fps)

	def Add(self, row):
		frames = self.Frames()
		if not frames:
			self.rows.clear()
			return
		n = int(row["frame"])
		if self.rows and n <= self.rows[-1][0]:
			self.rows.clear() # a new recording, or campy-live again
		self.rows.append((n, row, {}))
		while len(self.rows) > frames + 1:
			self.rows.popleft()

	def Clear(self):
		self.rows.clear()

	def Points(self, entry, c, camera):
		# One row's nodes in camera c: 2D (identities, nodes, 2) from its detections, 3D projected (or None)
		n, row, cache = entry
		if c not in cache:
			members = np.asarray(row["members"][:, c])
			kp = np.asarray(row["keypoints"][c], float)
			twoD = np.full((len(members),) + kp.shape[1:], np.nan)
			for a, i in enumerate(members):
				if 0 <= i < len(kp):
					twoD[a] = kp[i]
			threeD = None
			if camera is not None:
				X = np.asarray(row["points3d"], float)
				threeD = Project(camera, X.reshape(-1, 3)).reshape(X.shape[:-1] + (2,))
			cache[c] = (twoD, threeD)
		return cache[c]

	def Draw(self, img, c, camera, scale, origin=(0, 0), show2d=True, show3d=True, bad=()):
		"""
		The trails on img, camera c's tile at scale from origin (as DrawCamera),
		up to the row shown, the newest taken. bad: the identities drawn dimmed
		(their trails in Dimmed colours).
		"""
		rows = [entry for entry in self.rows if Has3D(entry[1])]
		W = len(rows)
		if W < 2 or not (show2d or show3d):
			return
		rows = rows[::-1] # k = 0: the newest
		k = np.arange(W)
		t = 1 - k / (W + 1)
		alpha = np.maximum(0.1, 0.9 * t)
		taper = (W - k) / (W - 1)
		age = 1 - t
		amount = TRAIL_SHADES.get(self.shade, TRAIL_SHADES["dark"])
		layers = []
		if show3d and camera is not None:
			layers.append((1, 2)) # the reprojections first (under), as LUC3D
		if show2d:
			layers.append((0, 1))
		h, w = img.shape[:2]
		overlay = mask = None
		box = [w, h, 0, 0]
		for which, width in layers:
			P = np.stack([self.Points(entry, c, camera)[which] for entry in rows]) # (W, identities, nodes, 2)
			Q = Place(P, np.broadcast_to(np.asarray(scale, float), (2,)), np.asarray(origin, float))
			ok = np.isfinite(Q).all(-1) & (np.abs(Q) < 1e5).all(-1)
			ok &= (Q[..., 0] > -w) & (Q[..., 0] < 2 * w) & (Q[..., 1] > -h) & (Q[..., 1] < 2 * h)
			q = np.zeros(Q.shape, np.int32)
			q[ok] = np.round(Q[ok] * (1 << SHIFT))
			seg = ok[:-1] & ok[1:] # (W - 1, identities, nodes): the segment from row k - 1 to row k
			if not seg.any():
				continue
			if overlay is None:
				overlay, mask = np.zeros_like(img), np.zeros(img.shape[:2], np.uint8)
			for k2 in range(W - 1, 0, -1): # the oldest first: the newer over it
				thick = max(1, int(round(max(min(0.8, width), width * taper[k2]))))
				for a in range(P.shape[1]):
					s = seg[k2 - 1, a]
					if not s.any():
						continue
					base = Dimmed(Color(a)) if a in bad else Color(a)
					lines = np.stack([q[k2 - 1, a][s], q[k2, a][s]], 1) # (segments, 2, 2)
					cv2.polylines(overlay, lines, False, TrailColor(base, amount, age[k2]), thick + 1, cv2.LINE_8, SHIFT)
					cv2.polylines(mask, lines, False, int(round(255 * alpha[k2])), thick, cv2.LINE_AA, SHIFT)
			pts = Q[ok]
			box = [min(box[0], pts[:, 0].min()), min(box[1], pts[:, 1].min()), max(box[2], pts[:, 0].max()),
				max(box[3], pts[:, 1].max())]
		if overlay is None:
			return
		# One blend, over where the trails are
		x0, y0 = max(0, int(box[0]) - 4), max(0, int(box[1]) - 4)
		x1, y1 = min(w, int(box[2]) + 5), min(h, int(box[3]) + 5)
		if x1 <= x0 or y1 <= y0:
			return
		a = mask[y0:y1, x0:x1, None].astype(np.float32) * (1 / 255)
		part = img[y0:y1, x0:x1]
		part[:] = (part * (1 - a) + overlay[y0:y1, x0:x1] * a + 0.5).astype(np.uint8)


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

	Learn also keeps each identity's trail (its middle over the last
	trail_seconds) and the occupancy (Occupancy: where the middles have been
	on the floor, since it began or over the last heat_minutes), of the same
	well triangulated animals: false 3D, from detections outside the cage in
	one camera, is not counted.

	Draw(img, row) draws a row on img, its tile: the grid and cameras (and
	the occupancy, under the grid) drawn once for each view of them
	(Background), then the trails on the floor, fading with age, then the
	animals, farthest first. Project gives where a world point lands on a
	tile.
	"""

	def __init__(self, cameras, names=None, up=None, trail_seconds=60.0, heat_minutes=0.0):
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
		self.cache = (None, None, None)
		self.Level(self.camerasUp)
		self.Reset()
		self.trailSeconds = float(trail_seconds)
		self.trails = {} # identity: its Trail
		self.shades = {} # identity: its trail's colour in each band
		self.now = None # the time of the last row taken (Learn): the trails' ages are from it
		n = 4 * int(np.ceil(1.2 * self.reach / self.spacing)) # cells a quarter of the grid's, past it
		self.occupancy = Occupancy(self.FloorBasis(), self.spacing / 4, n, heat_minutes)
		self.heatStamp, self.heatAt = 0, None # the occupancy drawn: changed when it is drawn again

	def FloorBasis(self):
		# The occupancy's: the floor under what the cameras look at, which moves only with up (the cage's
		# middle, Ground, moves as the animals go about)
		return self.target + self.floor * self.up, self.e1, self.e2

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

	def Learn(self, row, t=None):
		"""
		A row's well triangulated nodes and animals (at t, s: default now),
		to the trails and the occupancy; every 15 rows, the floor levelled with
		them.
		"""
		if not Has3D(row):
			return
		t = time.monotonic() if t is None else float(t)
		self.now = t
		X = np.asarray(row["points3d"], float)
		good = np.isfinite(X).all(-1) & (np.asarray(row["n_views"])[:, None] >= 2) & \
			(np.asarray(row["reproj_px"], float) < GOOD_PX)
		self.nodes.extend(X[good])
		self.occupancy.Expire(t)
		for a in range(len(X)):
			if good[a].sum() >= max(3, good.shape[1] // 2):
				middle = X[a][good[a]].mean(0)
				self.middles.append(middle)
				if self.trailSeconds > 0:
					if a not in self.trails: # 30 a second, and room to spare
						self.trails[a] = Trail(int(self.trailSeconds * 45) + 64)
					self.trails[a].Add(t, middle, t - self.trailSeconds)
				self.occupancy.Add(middle, t)
		if self.heatAt is None or t - self.heatAt >= HEAT_EVERY_S:
			self.heatStamp, self.heatAt = self.heatStamp + 1, t
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
		self.occupancy.Rebase(self.FloorBasis())
		self.heatStamp += 1

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

	def Background(self, shape, heat=False, words=None):
		"""
		The grid and the cameras, for this view of them, on a tile of shape
		(drawn again only when they move), over the occupancy with heat (then
		also every HEAT_EVERY_S). With words (a Pen), their text is handed to
		it instead of drawn.
		"""
		key = (tuple(shape[:2]), self.azimuth, self.elevation, self.distance, tuple(self.centre), self.floor,
			bool(heat) and self.heatStamp, words is not None)
		if self.cache[0] != key:
			self.cache = (key,) + self.DrawBackground(shape, heat, words is not None)
		for args in self.cache[2]:
			Text(None, *args, words=words)
		return self.cache[1]

	def DrawBackground(self, shape, heat, apart):
		# The background, and its text (Text's arguments) when apart, else drawn in it
		img = np.empty(tuple(shape[:2]) + (3,), np.uint8)
		img[:] = BACKDROP
		view = self.View(shape)
		texts = []
		def Say(*args):
			if apart:
				texts.append(args)
			else:
				Text(img, *args)
		if heat:
			self.DrawOccupancy(img, view)
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
				Say("{} {}".format(k + 1, self.names[k] if k < len(self.names) else cam.name),
					(at[0] + 6, at[1] - 6), FRUSTUM, 0.4)
		Say("grid squares {:g}".format(s), (PAD, shape[0] - PAD), DIM, 0.4)
		return img, texts

	def DrawOccupancy(self, img, view):
		# The occupancy's cells on the floor (img: the backdrop), the more visited the lighter: a picture
		# of the cells, put on the tile by the floor's homography, where the floor is in front of the eye
		counts = self.occupancy.total
		if not counts.any():
			return
		R, eye, f, c = view
		h, w = img.shape[:2]
		o, e1, e2 = self.occupancy.basis
		cell, n = self.occupancy.cell, self.occupancy.n
		v = np.log1p(counts.T) / np.log1p(counts.max()) # [j, i]: the picture's rows along e2
		weight = np.where(counts.T > 0, 0.15 + 0.85 * v, 0)[..., None]
		cells = np.empty(v.shape + (4,), np.uint8)
		cells[..., :3] = np.round(np.asarray(BACKDROP, float) + (np.asarray(HEAT, float) - BACKDROP) * weight)
		cells[..., 3] = np.where(counts.T > 0, 255, 0)
		# The picture's (i, j) -> the floor's point o + (i + 0.5 - n) cell e1 + ... -> the tile
		K = np.array([[f, 0, c[0]], [0, f, c[1]], [0, 0, 1.0]])
		corner = o + (0.5 - n) * cell * (e1 + e2)
		H = K @ R @ np.column_stack([cell * e1, cell * e2, corner - eye])
		warped = cv2.warpPerspective(cells, H, (w, h), flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT,
			borderValue=0)
		shown = warped[..., 3] > 0
		corners = o + n * cell * np.array([[-1, -1], [-1, 1], [1, -1], [1, 1]]) @ np.stack([e1, e2])
		if not (((corners - eye) @ R[2]) > self.near).all():
			# Some of the cells behind the eye: only where a ray from the eye meets the floor in front of it (not
			# above the horizon, where the homography puts the floor behind)
			r = R @ self.up
			down = r[0] * ((np.arange(w) - c[0]) / f)[None, :] + r[1] * ((np.arange(h) - c[1]) / f)[:, None] + r[2]
			shown &= down * ((eye - o) @ self.up) < 0
		np.copyto(img, warped[..., :3], where=shown[..., None])

	def DrawTrails(self, img, view):
		# Each identity's trail on the floor, under its middle, in its colour, fading with age: in
		# TRAIL_BANDS shades, a polyline each
		if self.now is None or self.trailSeconds <= 0:
			return
		R, eye, f, c = view
		floor = self.Ground()
		backdrop = np.asarray(BACKDROP, float)
		shades = self.shades
		for a, trail in self.trails.items():
			T, X = trail.Since(self.now - self.trailSeconds)
			if len(T) < 2:
				continue
			X = X - ((X - floor) @ self.up)[:, None] * self.up
			p = (X - eye) @ R.T
			with np.errstate(invalid="ignore", divide="ignore"):
				px = f * p[:, :2] / p[:, 2:3] + c
			ok = (p[:, 2] > self.near) & np.isfinite(px).all(1) & (np.abs(px) < 1e5).all(1)
			q = np.zeros(px.shape, np.int32)
			q[ok] = np.round(px[ok] * (1 << SHIFT))
			# Segment k, from point k to k + 1: in the band of its newer end (-1: a gap breaks it, or not drawable),
			# in runs of one band, each a polyline
			band = np.clip(((self.now - T[1:]) / self.trailSeconds * TRAIL_BANDS).astype(int), 0, TRAIL_BANDS - 1)
			band[~(ok[:-1] & ok[1:] & (np.diff(T) <= TRAIL_GAP_S))] = -1
			cuts = (np.flatnonzero(np.diff(band)) + 1).tolist()
			lines = {}
			for s0, s1 in zip([0] + cuts, cuts + [len(band)]):
				if band[s0] >= 0:
					lines.setdefault(int(band[s0]), []).append(q[s0:s1 + 1])
			if a not in shades:
				shades[a] = [tuple(int(round(v)) for v in backdrop + (np.asarray(Color(a), float) - backdrop) *
					(1.0 - 0.8 * b / max(1, TRAIL_BANDS - 1))) for b in range(TRAIL_BANDS)]
			for b in sorted(lines, reverse=True): # oldest first: the newer over it
				# The newest band 2 px wide, the others 1 (anti-aliased, 2 px takes 10-20 times as long a point)
				cv2.polylines(img, lines[b], False, shades[a][b], 2 if b == 0 else 1, cv2.LINE_AA, SHIFT)

	def Draw(self, img, row, edges=None, words=None, trails=True, heat=False, bad_px=livehealth.BAD_PX, bad=None):
		"""
		A row's animals on img (a tile), over the grid and the cameras (and,
		with heat, the occupancy) and the trails; nearer over farther; those
		in bad ({identity: px}; default: those whose reprojection error in row
		is above bad_px) dimmed and dashed, their labels red, under the
		others. words: a Pen for its text (Text).
		"""
		img[:] = self.Background(img.shape, heat, words)
		view = self.View(img.shape)
		if trails:
			self.DrawTrails(img, view)
		if row is None or not Has3D(row):
			return
		if bad is None:
			bad = Bad(row, bad_px)
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
				Text(img, "#{}  bad 3D: {:.0f} px".format(a, bad[a]), (at[0] + 6, at[1] - 10), WARN, 0.45,
					words=words)
			else:
				Text(img, "#{}".format(a), (at[0] + 6, at[1] - 10), Color(a), 0.45, words=words)


# ---------------------------------------------------------------- the view

# ---------------------------------------------------------------- bookmarks

BOOKMARKS = "bookmarks.csv" # in the recording's folder
SHIFTED = "!@#$%^&*(" # Shift+1-9, on a US keyboard
RECENT = 6 # the bookmarks the Qt panel lists
FLASH_S = 0.6 # the view's outline, after a bookmark
MARKED = (0, 215, 255) # its colour, and the bookmarks' in the panel (BGR: amber)


def BookmarkKind(key):
	"""The bookmark a key makes: "mark" (m), "1"-"9" (Shift+1-9), or None."""
	if key == ord("m"):
		return "mark"
	if isinstance(key, int) and 32 < key < 127 and chr(key) in SHIFTED:
		return str(SHIFTED.index(chr(key)) + 1)
	return None


def RecordingFolder(readers):
	"""
	The recording's folder: the one the cameras' folders are in, as their
	feeds say (livefeed.Feed's description). None when one does not say (a
	campy from before), or they say different ones.
	"""
	folders = set()
	for reader in readers:
		try:
			folder = json.loads(reader.Description() or "{}").get("folder")
		except (ValueError, AttributeError):
			folder = None
		if not folder:
			return None
		folders.add(os.path.dirname(os.path.normpath(folder)))
	return folders.pop() if len(folders) == 1 else None


def MarkText(mark):
	# A bookmark, in a line: 3 at 17:03:12, frame 1203, event 2: rears
	text = "{} at {}, frame {}".format(mark["number"], mark["time"][11:19],
		"-" if mark["frame"] is None else mark["frame"])
	if mark["event"] != "mark":
		text += ", event " + mark["event"]
	return text + (": " + mark["note"] if mark["note"] else "")


def Existing(path, columns):
	# The bookmarks in the file already, under these columns: 0 when it is new, None when its columns are others
	try:
		with open(path, newline="") as f:
			rows = [r for r in csv.reader(f) if r]
	except FileNotFoundError:
		return 0
	if not rows:
		return 0
	return len(rows) - 1 if rows[0] == columns else None


class Bookmarks:
	"""
	The bookmarks made in the view. Each is a line of a CSV file, added as it
	is made: the file opened, the line written, flushed and synced to the
	disk, the file closed, so a crash (or the power going) loses none made
	before it. The columns:

	  number       1, 2, ... in the file (on from its last, after a restart)
	  time         the computer's clock, local time, to the ms
	  host_ns      time.monotonic_ns(): the clock of campy's hosttimes.npy
	  event        mark (m), or 1-9 (Shift+1-9)
	  note         the note typed in the Qt panel, or empty
	  frame        the frame number shown in the view: campy's frame number,
	               as in frametimes.npy's first row (empty: no frames shown)
	  frame_<i>    camera i's (in feed order: campy's camera index) newest
	               frame then; the frame shown is a frame or two older
	  host_ns_<i>  when that frame reached the computer (hosttimes.npy's)
	  recording    the recording's folder, or empty

	The file is bookmarks.csv in the recording's folder, when the cameras'
	feeds say where that is, else fallback (--bookmarks); there too when the
	recording's cannot be written. A file there of other columns (as many
	cameras) is left as it is: they go to <name>_2.csv (_3, ...) beside it.
	"""

	def __init__(self, fallback):
		self.fallback = fallback
		self.recent = deque(maxlen=RECENT) # the last ones, the newest last: their fields, and "path"
		self.count = 0 # made, in all
		self.files = {} # (file asked for, columns): [the file written, its last number]

	@staticmethod
	def Columns(cameras):
		return ["number", "time", "host_ns", "event", "note", "frame"] + \
			[f.format(c) for c in range(cameras) for f in ("frame_{}", "host_ns_{}")] + ["recording"]

	def Where(self, recording):
		# Where a bookmark goes first
		return os.path.join(recording, BOOKMARKS) if recording else self.fallback

	def Add(self, mark):
		"""
		Writes mark (a dict: time, host_ns, event, note, frame, cameras: a
		(frame, host_ns) per camera, recording) into its file, numbering it.
		Returns the file, or None when it could be written nowhere (said).
		"""
		columns = self.Columns(len(mark["cameras"]))
		for base in dict.fromkeys([self.Where(mark["recording"]), self.fallback]):
			try:
				path = self.Write(base, columns, mark)
			except OSError as e:
				Say("Could not write the bookmark into {}: {}.".format(base, e))
				continue
			mark["path"] = path
			self.count += 1
			self.recent.append(mark)
			return path
		return None

	def Write(self, base, columns, mark):
		key = (base, tuple(columns))
		if key not in self.files:
			path, k = base, 1
			number = Existing(path, columns)
			while number is None:
				k += 1
				stem, ext = os.path.splitext(base)
				path = "{}_{}{}".format(stem, k, ext)
				number = Existing(path, columns)
			self.files[key] = [path, number]
		path, number = self.files[key]
		mark["number"] = number + 1
		def Blank(v):
			return "" if v is None else v
		line = [mark["number"], mark["time"], mark["host_ns"], mark["event"], mark["note"], Blank(mark["frame"])] + \
			[Blank(v) for pair in mark["cameras"] for v in pair] + [mark["recording"] or ""]
		os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
		new = not os.path.exists(path) or os.path.getsize(path) == 0
		with open(path, "a", newline="") as f:
			writer = csv.writer(f)
			if new:
				writer.writerow(columns)
			writer.writerow(line)
			f.flush()
			os.fsync(f.fileno())
		self.files[key][1] += 1
		return path


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
		self.zooms = {} # each camera's Zoom, by feed index
		self.frameShape = None # the frames' (height, width), as last drawn
		self.panned = False # a camera's zoom changed since it was drawn
		self.textOver = False # the window draws the text over the view (Qt's fonts): handed to it in words
		self.words = None # the last view's text (Words), when textOver
		self.render = None # the window's: the view with its text drawn over it, for the PNGs
		self.scene = None # the 3D tile's Scene (with a calibration)
		self.showScene = not args.no_3d
		self.trails = args.trail_seconds > 0 # the 3D view's trails shown, and the cameras' (cameraTrails)
		self.cameraTrails = CameraTrails(args.camera_trail_seconds, args.trail_shade)
		self.cameraTrailMs = deque(maxlen=KEPT) # of drawMs: the cameras' trails
		self.occupancy = args.occupancy # and the occupancy
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
		self.inference = deque() # (monotonic, ms): campy-live's inference time of the rows of the last 2 s
		self.cpusSaid = None # what the c key last did
		self.drawMs, self.shownMs, self.resultMs = deque(maxlen=KEPT), deque(maxlen=KEPT), deque(maxlen=KEPT)
		self.sceneMs = deque(maxlen=KEPT) # of drawMs: the 3D tile's
		self.rig = None # livehealth.Rig: how each camera keeps up, from its ring's slots
		self.tracking = None # livehealth.Tracking: each identity's 3D over the last 10 s
		self.rigAt = 0.0 # when the slots were last read
		self.rigMs = deque(maxlen=KEPT) # reading them
		self.healthMs = deque(maxlen=KEPT) # of drawMs: the warnings, the tiles' marks and the strip
		self.panelMs = deque(maxlen=KEPT) # Qt's panel, said again (liveviewqt.Window.Say)
		self.wordsMs = deque(maxlen=KEPT) # the window's drawing of the text over the view (Qt's), not in drawMs
		self.lastSeen = self.lastShown = None # ms after the row's frames: it reached the view, the last view was shown
		# Bookmarks (m, Shift+1-9)
		self.bookmarks = Bookmarks(os.path.abspath(args.bookmarks))
		self.recording = None # the recording's folder, as the cameras' feeds say: kept once it ends
		self.flashUntil = 0.0 # a bookmark's outline, drawn until then
		self.flashDrawn = False # in the last view drawn

	# -- the shared memory

	def Check(self):
		# Attaches what has come, lets go of what ended
		self.checked = time.monotonic()
		if self.results is not None and self.results.Ended():
			self.results.Close()
			self.results = None
			self.rate = [] # its results a second: none now
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
				self.cameraTrails.fps = self.cameraTrails.fps or readers[0].Info("frameRate")
				self.rig = livehealth.Rig([r.cameraName or r.name for r in readers], readers[0].Info("frameRate"),
					readers[0].slots)
				self.rigAt = 0.0
				self.recording = RecordingFolder(readers)
				Say("Live feed: {}. Bookmarks (m) go to {}.".format(", ".join("{} {}x{}".format(r.cameraName or r.name,
					r.shape[1], r.shape[0]) for r in readers), self.bookmarks.Where(self.recording)))
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
				self.scene = Scene(self.calibration, d.get("cameras") or names, self.args.up, self.args.trail_seconds,
					self.args.heat_minutes)
		self.tracking = livehealth.Tracking(d.get("animals"), d.get("frame_rate"), self.args.bad_px) \
			if self.calibration else None
		self.cameraTrails.Clear() # another campy-live's rows
		self.cameraTrails.fps = d.get("frame_rate") or self.cameraTrails.fps
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
					if "inference_ns" in row.dtype.names and row["inference_ns"] > 0:
						self.inference.append((now, row["inference_ns"] / 1e6))
					while self.inference and self.inference[0][0] < now - 2.0:
						self.inference.popleft()
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
						self.scene.Learn(row, now)
					self.cameraTrails.Add(row)
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

	def Dimmed(self, row):
		# The identities to draw dimmed in row, {identity: px}: those whose error's median over the last half
		# second is above --bad-px (livehealth.Tracking.Dimmed); without the tracking, or for a row it did not
		# take, those above it in the row alone
		if not Has3D(row) or not self.args.bad_px:
			return {}
		bad = self.tracking.Dimmed(int(row["frame"])) if self.tracking is not None else None
		return Bad(row, self.args.bad_px) if bad is None else bad

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
		# The rig's line and a warning or two; with 3D, each identity's line and its strip, the strip's legend;
		# the last bookmark
		return 12 + (2 * animals + 4 if self.calibration else 0) + bool(self.bookmarks.recent)

	def ZoomOf(self, c):
		if c not in self.zooms:
			self.zooms[c] = Zoom()
		return self.zooms[c]

	def Compose(self, frames, row):
		start = time.perf_counter()
		self.words = Words() if self.textOver else None
		self.frameShape = frames[0].image.shape[:2]
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
		bad = self.Dimmed(row)
		trailS = 0.0
		for k, (c, frame) in enumerate(zip(shown, frames)):
			y, x = layout.Cell(k)
			words = self.words.Tile(("camera", k), y + oy, x + ox, *layout.tile) if self.words is not None else None
			# Zoomed: the part of the frame shown, cut out before it is scaled, and the drawing on it
			zoom = self.ZoomOf(c)
			scale, origin = zoom.Window(frame.image.shape, layout.tile, layout.scale)
			tile = Gray(frame.image, layout.tile, scale, origin)
			if row is not None:
				if self.trails and self.cameraTrails.seconds > 0: # under the skeletons
					trailStart = time.perf_counter()
					self.cameraTrails.Draw(tile, c, self.calibration[c] if self.calibration else None, scale, origin,
						self.show2d, self.show3d, bad)
					trailS += time.perf_counter() - trailStart
				DrawCamera(tile, scale, row, c, self.calibration[c] if self.calibration else None, edges,
					self.show2d, self.show3d, self.lines, origin, frame.image.shape[1::-1], words, bad=bad)
			name = cameraNames[c] if c < len(cameraNames) else str(c)
			if c < len(calibNames) and calibNames[c] != name:
				name += " ({})".format(calibNames[c])
			text = "{}  {}  frame {}".format(c + 1, name, frame.frameNumber)
			if row is not None:
				text += "  found {}".format(int(row["found"][c]))
			if self.selected is not None: # single view: which of how many (as LUC3D)
				text += "  ({}/{})".format(c + 1, len(self.readers) if self.readers else len(self.ringNames))
			if zoom.Zoomed():
				text += "  1:1" if zoom.native else "  zoom {:.1f}x".format(zoom.times)
			Text(tile, text, (PAD, PAD + 12), words=words)
			if len(shown) > 1 and c == self.current: # the grid: the camera v shows alone
				cv2.rectangle(tile, (1, 1), (tile.shape[1] - 2, tile.shape[0] - 2), PICKED, 2)
			health = time.perf_counter()
			self.Badge(tile, c, rig)
			healthS += time.perf_counter() - health
			canvas[y:y + layout.tile[0], x:x + layout.tile[1]] = tile
			self.cells.append((c, y + oy, x + ox) + layout.tile)
		self.sceneCell = None
		if self.Showing3D():
			y, x = layout.Cell(len(shown))
			self.sceneCell = (y + oy, x + ox) + layout.tile
			self.Draw3D(full, row, frames[0].frameNumber)
		if self.panel:
			healthS += self.Panel(canvas, layout, frames, row)
		self.flashDrawn = time.monotonic() < self.flashUntil
		if self.flashDrawn:
			self.Flash(canvas, self.words.Tile("flash", oy, ox, *layout.size) if self.words is not None else None)
		self.last = (frames, row) # drawn again when a button is clicked while paused
		self.lastCameras = shown
		self.panned = False
		self.drawMs.append((time.perf_counter() - start) * 1e3)
		self.healthMs.append(healthS * 1e3)
		if row is not None and self.trails and self.cameraTrails.seconds > 0:
			self.cameraTrailMs.append(trailS * 1e3)
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
		words = self.words.Tile("3d", y, x, h, w) if self.words is not None else None
		self.scene.Draw(tile, row, self.description.get("edges"), words, self.trails, self.occupancy,
			self.args.bad_px, self.Dimmed(row))
		header = "3D  frame {}".format(frame) + ("" if row is not None else "  (no results)")
		if self.trails and self.scene.trailSeconds > 0:
			header += "  trails {:g} s".format(self.scene.trailSeconds)
		if self.occupancy:
			minutes = self.scene.occupancy.window / 60
			header += "  occupancy " + ("since the start" if not minutes else "{:g} min".format(minutes))
		Text(tile, header, (PAD, PAD + 12), words=words)
		Text(tile, "drag: turn  right-drag: move  wheel: zoom  r: reset", (PAD, PAD + 34), DIM, 0.4, words=words)
		self.moved = False
		self.sceneMs.append((time.perf_counter() - start) * 1e3)

	def In3D(self, x, y):
		if self.sceneCell is None:
			return False
		y0, x0, h, w = self.sceneCell
		return x0 <= x < x0 + w and y0 <= y < y0 + h

	def CameraAt(self, x, y):
		# The camera whose tile is at (x, y) of the view, or None
		cell = self.CellAt(x, y)
		return None if cell is None else cell[0]

	def CellAt(self, x, y):
		# The camera's tile at (x, y) of the view: (camera, y, x, height, width), or None
		for cell in self.cells:
			c, y0, x0, h, w = cell
			if x0 <= x < x0 + w and y0 <= y < y0 + h:
				return cell
		return None

	def ZoomCamera(self, c, factor, at=None):
		# Camera c's tile zoomed factor times (< 1: out), about its pixel at (x, y) (default: its middle)
		if self.layout is None or self.frameShape is None:
			return
		h, w = self.layout.tile
		self.ZoomOf(c).About(factor, (w / 2, h / 2) if at is None else np.asarray(at) + 0.5, self.frameShape,
			self.layout.tile, self.layout.scale) # (the mouse's pixel, from the tile's edge)
		self.panned = True

	def ZoomKey(self, key):
		# As LUC3D's, for every camera shown: + or = nearer, - or _ farther, 0 the whole frame again (every
		# camera), ) (shift 0) native resolution on or off. True if it was one of these keys
		if key in (ord("+"), ord("=")):
			for c in self.Shown():
				self.ZoomCamera(c, ZOOM_KEY)
		elif key in (ord("-"), ord("_")):
			for c in self.Shown():
				self.ZoomCamera(c, 1 / ZOOM_KEY)
		elif key == ord("0"):
			for zoom in self.zooms.values():
				zoom.Reset()
		elif key == ord(")"):
			on = not all(self.ZoomOf(c).native for c in self.Shown())
			for c in self.Shown():
				self.ZoomOf(c).Native(on, *((self.frameShape, self.layout.tile, self.layout.scale)
					if self.layout is not None and self.frameShape is not None else ()))
		else:
			return False
		self.panned = True
		return True

	def Press(self, x, y, button="left"):
		"""
		A mouse button pressed at (x, y) of the view: the key of a button
		there (the panel drawn in the view), or None. Left on a camera's tile
		picks it (v shows it alone), and a drag from there moves its picture
		(zoomed); in the 3D tile, left drags turn it, right or middle drags
		move it.
		"""
		if button == "left":
			key = self.Click(x, y)
			if key is not None:
				return key
			camera = self.CameraAt(x, y)
			if camera is not None:
				self.current = camera
				self.drag = ("pan", (x, y), camera)
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
		if self.drag is not None and self.drag[0] == "pan":
			# A camera's picture, zoomed, with the mouse (while its tile is shown)
			button, (x0, y0), camera = self.drag
			zoom = self.ZoomOf(camera)
			if camera not in [cell[0] for cell in self.cells] or self.layout is None:
				self.drag = None
			elif (x, y) != (x0, y0) and zoom.Zoomed():
				zoom.Pan(x - x0, y - y0, self.frameShape, self.layout.tile, self.layout.scale)
				self.drag = (button, (x, y), camera)
				self.panned = True
		elif self.drag is not None:
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
		# The wheel turned steps clicks (> 0: away from you) at (x, y), or where the mouse last moved: over the
		# 3D, it zooms; over a camera's tile, that camera's picture, about the mouse
		x, y = (x, y) if x is not None else self.mouseAt
		if not steps:
			return
		if self.In3D(x, y):
			self.scene.Zoom(steps)
			self.moved = True
			return
		cell = self.CellAt(x, y)
		if cell is not None:
			c, y0, x0, h, w = cell
			self.ZoomCamera(c, ZOOM_WHEEL ** steps, (x - x0, y - y0))

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
			buttons += [("Trails (t)", ord("t"), self.trails), ("Occupancy (o)", ord("o"), self.occupancy)]
		fast = self.CpusOn()
		if fast is not None:
			buttons.append(("Fast CPUs (c)", ord("c"), fast))
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
		dark: no row from campy-live). None too once the recording's feed has
		ended (its last 10 s are not now's).
		"""
		if self.tracking is None or self.ended:
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
				bad = self.Dimmed(row)
				for a in range(len(row["n_views"])):
					views = int(row["n_views"][a])
					err = np.asarray(row["reproj_px"][a], float)
					color = Color(a)
					if views >= 2 and np.isfinite(err).any():
						mean = float(np.nanmean(err))
						text = "#{}: from {} cameras, reprojection {:.1f} px".format(a, views, mean)
						if a in bad:
							text += "; median over {:g} s {:.0f} px: above {:g} px, drawn dimmed, not to trust".format(
								livehealth.DIM_S, bad[a], self.args.bad_px)
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
		cpuLine = self.CpusText()
		if cpuLine is not None:
			lines.append(cpuLine)
		if self.cpusSaid:
			lines.append(("c: " + self.cpusSaid, WARN if "Could not" in self.cpusSaid or "No CPUs" in self.cpusSaid
				else DIM))
		if row is not None:
			lines.append(("Reprojections (dots): the 3D, projected. Predictions (open circles): this camera's 2D "
				"(white: matched to no animal). Line: from the 2D to the 3D. Label: #identity, and px between them.", DIM))
		if self.tracking is not None:
			lines.append(("Strip: each identity's last {:.0f} s. Height: the cameras it was triangulated from; green: "
				"within {:g} px, yellow: within {:g} px, red: above (dimmed and dashed while its median over {:g} s "
				"is); gray: no 3D.".format(livehealth.WINDOW_S, livehealth.GOOD_PX, self.args.bad_px or float("inf"),
				livehealth.DIM_S), DIM))
		if self.panel and self.bookmarks.recent: # the Qt window lists them in its own panel
			lines.append(("Bookmark {} ({} made)".format(MarkText(self.bookmarks.recent[-1]), self.bookmarks.count),
				MARKED))
		lines.append((KEYS.format(len(self.ringNames), "; \\ 3D view, r turn it back, t trails, o occupancy"
			if self.scene is not None else ""),
			DIM))
		return lines

	# -- campy-live's and campy-acquire's CPUs (the c key)

	def Placement(self):
		# Who to move: campy-live (its results say its process) and campy-acquire (its feeds say its processes)
		return cpus.Placement(live=self.description.get("pid") if self.results is not None else None,
			campy=livefeed.Processes(self.readers or []))

	def CpusOn(self):
		# True: campy-live on the performance cores; False: not; None: can't be told or changed here
		placement = self.Placement()
		return placement.IsOn() if placement.Available() else None

	def ToggleCpus(self):
		placement = self.Placement()
		if not placement.Available():
			self.cpusSaid = "No CPUs to move: {}.".format("campy-live's results don't say its process (a campy-live "
				"from before)" if placement.cores else "this CPU has no efficiency cores, or this system can't tell")
		else:
			self.cpusSaid = placement.Set(not placement.IsOn())
		Say("CPUs: " + self.cpusSaid)

	def CpusText(self):
		# The panel's line: where campy-live runs, and its inference time over the last 2 s
		times = np.array([ms for _, ms in self.inference])
		speed = "campy-live's inference p50 {:.1f} p99 {:.1f} ms over the last 2 s".format(
			np.percentile(times, 50), np.percentile(times, 99)) if len(times) else None
		on = self.CpusOn()
		if on is None:
			return None if speed is None else (speed[0].upper() + speed[1:], DIM)
		cores = cpus.Hybrid()
		where = "campy-live on the performance cores ({}) at high priority, campy-acquire on the efficiency cores".format(
			cpus.Text(cores["performance"])) if on else "campy-live and campy-acquire on any CPU, normal priority"
		text = "CPUs (c): {}{}".format(where, "; " + speed if speed else "")
		return text, TEXT

	def Waiting(self):
		canvas = self.Canvas(self.Size()[::-1]) # the view's size: OpenCV sizes a new window to its first image
		self.words = Words() if self.textOver else None
		words = self.words.Tile("waiting", 0, 0, *canvas.shape[:2]) if self.words is not None else None
		Text(canvas, "Waiting for the live feed: {}".format(", ".join(self.ringNames)), (PAD, 40), words=words)
		Text(canvas, "(campy-acquire with liveFeed True)", (PAD, 70), DIM, words=words)
		Text(canvas, "campy-live's results: {}".format("there" if self.results else "not yet"), (PAD, 100), DIM,
			words=words)
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
		elif BookmarkKind(key):
			self.Mark(BookmarkKind(key))
		elif self.ZoomKey(key):
			pass
		elif ord("1") <= key <= ord("9") and key - ord("1") < n:
			self.selected = self.current = key - ord("1")
		elif key in (ord("a"), ord("g")):
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
		elif key == ord("t"):
			self.trails = not self.trails
			self.moved = True
		elif key == ord("o"):
			self.occupancy = not self.occupancy
			self.moved = True
		elif key == ord("r") and self.scene is not None:
			self.scene.Reset()
			self.moved = True
		elif key == ord("c"):
			self.ToggleCpus()
		return True

	def Mark(self, event, note=""):
		"""
		A bookmark of the moment (event: "mark", or "1"-"9"), with what the
		view shows, written at once (Bookmarks). Returns it (a dict of its
		fields), or None when it could be written nowhere.
		"""
		now, host = datetime.now(), time.monotonic_ns()
		cameras = []
		for c in range(len(self.readers) if self.readers else len(self.ringNames)):
			n = self.readers[c].Latest() if self.readers else 0
			cameras.append((n, self.readers[c].HostTime(n)) if n > 0 else (None, None))
		shown = getattr(self, "last", None)
		mark = {"time": now.isoformat(" ", "milliseconds"), "host_ns": host, "event": event,
			"note": " ".join(str(note).split()), "frame": shown[0][0].frameNumber if shown and self.readers else None,
			"cameras": cameras, "recording": self.recording}
		if self.bookmarks.Add(mark) is None:
			return None
		self.flashUntil = time.monotonic() + FLASH_S
		Say("Bookmark {}, in {}.".format(MarkText(mark), mark["path"]))
		return mark

	def Flash(self, canvas, words=None):
		# A bookmark was just made: the view outlined, and which it was (words: a Pen for the text)
		h, w = canvas.shape[:2]
		cv2.rectangle(canvas, (2, 2), (w - 3, h - 3), MARKED, 5)
		if self.bookmarks.recent:
			text = "Bookmark " + MarkText(self.bookmarks.recent[-1])
			(tw, _), _ = cv2.getTextSize(text, FONT, 0.8, 1)
			Text(canvas, text, (max(PAD, (w - tw) // 2), 44), MARKED, 0.8, words=words)

	def FlashOver(self):
		# The last view has a bookmark's outline that is over: to be drawn again without it, when paused
		if self.flashDrawn and time.monotonic() >= self.flashUntil:
			self.flashDrawn = False
			return True
		return False

	def Delivered(self, image, frame, row):
		# A view shown: how late, and saved (every --png-every-th result)
		if row is None:
			return
		self.lastShown = (time.perf_counter_ns() - int(np.max(row["written_ns"]))) / 1e6
		self.shownMs.append(self.lastShown)
		self.count += 1
		if self.args.png and self.count % self.args.png_every == 0:
			cv2.imwrite(os.path.join(self.args.png, "view_{:08d}.png".format(frame)), self.Rendered(image))

	def Rendered(self, image):
		# The view as its window shows it: with the text it draws over it (Qt's), when it does
		return self.render(image) if self.render is not None and self.words is not None else image

	def Save(self, image):
		# s: the view as a PNG, into --png or the current folder
		path = os.path.join(self.args.png or ".", "view_{}.png".format(datetime.now().strftime("%H-%M-%S")))
		cv2.imwrite(path, self.Rendered(image))
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
			"camera_trails_ms": {"p50": P(self.cameraTrailMs, 50), "p99": P(self.cameraTrailMs, 99)},
			"rig_read_ms": {"p50": P(self.rigMs, 50), "p99": P(self.rigMs, 99)},
			**({"qt_panel_ms": {"p50": P(self.panelMs, 50), "p99": P(self.panelMs, 99)}} if self.panelMs else {}),
			**self.HealthSummary(),
			"text_over_ms": {"p50": P(self.wordsMs, 50), "p99": P(self.wordsMs, 99)},
			"results_here_after_frame_ms": {"p50": P(self.resultMs, 50), "p99": P(self.resultMs, 99)},
			"shown_after_frame_ms": {"p50": P(self.shownMs, 50), "p99": P(self.shownMs, 99)},
			"bookmarks": self.bookmarks.count,
			"bookmark_files": sorted({path for path, number in self.bookmarks.files.values() if number})}


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
				over = paused and viewer.FlashOver() # a bookmark's outline, gone
				if paused and (over or viewer.panned) and image is not None:
					again = viewer.Again() # (or a camera's zoom), on the same row
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
	if summary["bookmarks"]:
		Say("{} bookmarks, in {}.".format(summary["bookmarks"], ", ".join(summary["bookmark_files"])))
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
	p.add_argument("--trail-seconds", type=float, default=60, help="The 3D view's trails: each identity's path over "
		"the last this many seconds (0: none; t shows or hides them).")
	p.add_argument("--camera-trail-seconds", type=float, default=0.25, help="The cameras' tiles' trails (LUC3D's node "
		"trails, shown with the 3D view's: t): each node's path over the last this many seconds of frames (LUC3D's "
		"lengths: 0.25, 0.5, 1, 2; 0: none).")
	p.add_argument("--trail-shade", default="dark", choices=list(TRAIL_SHADES), help="The cameras' trails' colour, "
		"from the identity's (LUC3D's shades): dark, its channels times 0.6, and darker with age.")
	p.add_argument("--occupancy", action="store_true", help="Start with the 3D view's occupancy shown (o).")
	p.add_argument("--heat-minutes", type=float, default=0, help="The occupancy: where the animals went over the last "
		"this many minutes (0: since campy-live's results began).")
	p.add_argument("--up", type=Direction, default=None, help="The world's up in the calibration, as x,y,z "
		"(default: away from where the cameras look, when they look down).")
	p.add_argument("--bad-px", type=float, default=livehealth.BAD_PX, help="An identity whose reprojection error "
		"(campy-live's, mean over the nodes; its median over the last 0.5 s, not to flicker) is above this is drawn "
		"dimmed and dashed, its label red (0: never).")
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
	p.add_argument("--bookmarks", default="campy-live-view_bookmarks.csv", help="The bookmarks' file (m) when the "
		"cameras' feeds do not say the recording's folder (campy-acquire from before this campy); else bookmarks.csv "
		"there.")
	return p.parse_args(argv)


def Main(argv=None):
	sys.exit(Run(ParseArgs(argv)))


if __name__ == "__main__":
	Main()
