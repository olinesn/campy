"""
The cage from the enclosure's own 1-inch grid, with no clicking.

An enclosure whose floor and walls carry a printed grid of 1-inch squares
says where it is: on the floor's true plane, every camera sees the grid's
lines at the same places. This finds the floor and the walls that way, from
a calibration and one picture of the empty arena per camera, and makes the
Cage (campy/pose3d.py) that campy-live sets aside 3D outside of. It also
says, for any picture of the arena, whether each camera's picture matches its
own calibration (cameras whose names were swapped do not).

How (a plane sweep with the grid as the texture):
1. Ridge maps: thin bright lines in each camera's picture (skimage's Sato
   filter), blurred by fractions of a square as that camera sees it.
2. The floor: a candidate plane is sampled on a lattice of points, each
   point projected into every camera (with its lens distortion) and the
   ridge maps read there. On the true floor the cameras' rectified maps
   agree; the score is their pairwise normalised correlation, weighted by
   overlap (Agreement). Height and two tilts are swept, wide and blurred
   first, then sharper. Since the cameras see the floor from different
   directions, a plane one square off does not line up for all of them: the
   periodic grid gives no false optimum. The start needs no animals: the
   floor's normal is the direction square to every camera's image rows (a
   camera mounted without a roll keeps them level, and one looking straight
   down has them level anyway), and the sweep goes from below the point the
   cameras look at to their lowest camera.
3. The grid's axes and its square: the cameras' mean rectified floor folded
   along each direction, with a period P; the direction and P with the
   sharpest fold. A 1-inch grid gives 25.4 mm when the calibration is in
   true mm: a check of the calibration's scale.
4. The walls: first where the floor's grid ends each way (strips across the
   floor, outwards, until the cameras stop agreeing on them), then vertical
   planes near there, scored by the cameras on the wall's inner side. A wall
   has a grid when its rectified picture folds at the square both along and
   up it (the floor's lines going on under a clear pane fold one way only).
   A side without one is placed from the opposite wall and the floor's size
   (size), or, without it, where the floor's grid ends. The cage's height:
   how far up the walls' grid goes, on the wall seen best.
5. Squares counted from wall to wall at the measured square.
6. The check: the fitted floor grid projected into every camera, its ridge
   on the lines against the same lines half a square off (Ratios: > 2 when
   the grid lands on the lines, about 1 when it does not); and, along the
   floor's and the gridded walls' lines, where each picture's own line is,
   in pixels from the fit's (Misses; a wall hides what is behind it, Seen).
   Away from the middle of a picture the miss grows with what the
   calibration's lens model gets wrong at its edges, not with the box.
7. Camera identity (Identity): every picture against every camera's
   calibration in the same way. Each should match its own; pictures whose
   cameras' names were swapped (the cameras listed in another order after a
   restart, say) match each other's.

Interface
---------
    from campy import cagegrid, pose3d
    cameras = pose3d.LoadCalibration("calibration.toml")
    fit = cagegrid.Fit(cameras, images, size=(18, 10))   # images: grey uint8, one per camera, in order
    cage = cagegrid.MakeCage(fit, cameras)                # a pose3d.Cage
    cage.Save(pose3d.CagePath("calibration.toml"), made="grid", grid=fit["grid"])
    identity = cagegrid.Identity(cameras, images, fit["grid"])

campy-cage (Main) does it for a recording's or a folder's pictures:
docs/pose3d.md, "The cage from the grid".
"""

import argparse, json, math, os, re, subprocess, sys, time

import numpy as np

from campy import pose3d

INCH = 25.4                   # mm: the grid's square
WALL_BAND = (6.0, 6 * INCH)   # mm above the floor: the part of the walls scored (most cameras see the lower walls)
WALL_CONTRAST = 0.3           # a wall has a grid when its rectified picture folds at least this much of the floor's
GRID_RATIO_OK = 2.0           # a camera whose picture puts the fitted grid on its lines by less than this is said
IDENTITY_MARGIN = 1.5         # a picture matches another camera's calibration: its ratio there over its own


def Unit(v):
	v = np.asarray(v, float)
	return v / np.linalg.norm(v)


# ---------------------------------------------------------------- ridge maps and projection

def RidgeMap(image, sigmas=(1.0, 1.5, 2.0)):
	"""Thin bright lines in a grey picture (H, W): skimage's Sato filter, scaled to 0-1 by its 99.5th percentile."""
	from skimage.filters import sato
	g = np.asarray(image, np.float32)
	if g.ndim == 3:
		g = g.mean(-1)
	g = g / max(float(g.max()), 1.0)
	r = sato(g, sigmas=sigmas, black_ridges=False).astype(np.float32)
	return np.clip(r / (np.percentile(r, 99.5) + 1e-9), 0, 1)


def FoldRadius2(dist):
	"""
	The squared radius (normalized coordinates) where the lens model folds back: past it, r (1 + k1 r^2 + k2 r^4
	+ k3 r^6) shrinks again and far-off points land inside the picture. inf when it never does.
	"""
	k1, k2, _, _, k3 = (float(x) for x in np.ravel(dist)[:5])
	roots = np.roots([7 * k3, 5 * k2, 3 * k1, 1.0]) if (k1 or k2 or k3) else []
	s = [r.real for r in np.atleast_1d(roots) if abs(r.imag) < 1e-12 and r.real > 0]
	return min(s) if s else math.inf


def Remap(m, uv):
	"""The map m (H, W) read at the points uv (N, 2), bilinear: cv2.remap, its points laid out in rows (it takes
	at most 32,767 rows)."""
	import cv2
	uv = np.asarray(uv, np.float32).reshape(-1, 2)
	N = len(uv)
	W = 1024
	rows = -(-N // W) if N else 0
	pad = np.zeros((rows * W, 2), np.float32)
	pad[:N] = uv
	pad = pad.reshape(rows, W, 2)
	return cv2.remap(m, pad[..., 0], pad[..., 1], cv2.INTER_LINEAR).ravel()[:N] if N else np.zeros(0, np.float32)


class Views:
	"""
	Each camera's ridge maps, sharp and blurred, and where world points land in its picture. The blurs are
	fractions of the grid's square as each camera sees it (square_px: pixels; None: 40, as the DoublePoke box's
	cameras see it): a tenth for "coarse", a fifth for "wide", which sweeps from far off.
	"""

	BLUR = {"fine": 0.0, "coarse": 0.1, "wide": 0.2}

	def __init__(self, cameras, images, maps=None, square_px=None):
		import cv2
		self.cameras = list(cameras)
		self.maps = {k: [] for k in self.BLUR}
		ridges = maps if maps is not None else [RidgeMap(im) for im in images]
		for c, r in enumerate(ridges):
			square = square_px[c] if square_px is not None else 40.0
			for k, part in self.BLUR.items():
				self.maps[k].append(cv2.GaussianBlur(r, (0, 0), max(1.0, part * square)))
		self.shapes = [r.shape for r in ridges]
		self.fold = [0.8 * FoldRadius2(c.dist) for c in self.cameras]
		self.centres = np.array([c.center for c in self.cameras])

	def Project(self, c, P):
		"""World points (..., 3) -> pixels (..., 2) in camera c, and which land in its picture, in front of it."""
		cam = self.cameras[c]
		Xc = np.asarray(P, float) @ cam.R.T + cam.t
		z = Xc[..., 2]
		with np.errstate(divide="ignore", invalid="ignore"):
			n = Xc[..., :2] / z[..., None]
			ok = (z > 1e-3) & ((n * n).sum(-1) < self.fold[c])
			uv = cam.Distort(np.where(ok[..., None], n, 0.0))
		h, w = self.shapes[c]
		ok &= np.isfinite(uv).all(-1) & (uv[..., 0] >= 2) & (uv[..., 0] <= w - 3) & (uv[..., 1] >= 2) & \
			(uv[..., 1] <= h - 3)
		return uv, ok

	def Sample(self, P, level="fine", cams=None, maps=None):
		"""P (..., 3) world -> S (C, ...) ridge values, V (C, ...) where seen. cams: which cameras (None: all)."""
		cams = range(len(self.cameras)) if cams is None else cams
		S, V = [], []
		flat = np.asarray(P, float).reshape(-1, 3)
		for c in cams:
			uv, ok = self.Project(c, flat)
			m = (maps or self.maps[level])[c]
			S.append(Remap(m, np.where(ok[:, None], uv, 0)))
			V.append(ok)
		shape = np.shape(P)[:-1]
		return np.stack(S).reshape((-1,) + shape), np.stack(V).reshape((-1,) + shape)


def Agreement(S, V, min_n=300):
	"""
	How well the cameras' rectified maps agree: the mean pairwise normalised correlation, each pair over the
	points both see, weighted by how many (pairs with fewer than min_n are left out); -1 with no pair. All pairs
	at once, from sums over the points (matrix products): each pair's correlation is the one its own masked
	arrays give.
	"""
	C = len(S)
	Sf = np.asarray(S, float).reshape(C, -1)
	Vf = np.asarray(V, float).reshape(C, -1)
	A = Sf * Vf
	n = Vf @ Vf.T                    # points both see
	sx = A @ Vf.T                    # sx[i, j]: camera i's sum over the points j sees too
	sxx = (A * Sf) @ Vf.T
	sxy = A @ A.T
	iu = np.triu_indices(C, 1)
	with np.errstate(divide="ignore", invalid="ignore"):
		cov = sxy - sx * sx.T / n
		var = sxx - sx * sx / n
		r = cov / np.sqrt(var * var.T)
	w, r, den = n[iu], r[iu], (var * var.T)[iu]
	ok = (w >= min_n) & np.isfinite(r) & (den > 0)
	return float((w[ok] * r[ok]).sum() / w[ok].sum()) if ok.any() else -1.0


def Lattice(origin, a1, a2, r1, r2, step):
	s = np.arange(r1[0], r1[1], step)
	t = np.arange(r2[0], r2[1], step)
	return origin + s[:, None, None] * a1 + t[None, :, None] * a2, s, t


def PlaneAxes(n, ref):
	a1 = Unit(ref - (ref @ n) * n)
	return a1, np.cross(n, a1)


def Fold(F, coords, P):
	"""How sharply the picture F folds with period P along coords (first harmonic), and where its lines are."""
	w = F - F.mean()
	z = (w * np.exp(2j * np.pi * coords / P)).sum()
	return abs(z) / (np.abs(w).sum() + 1e-9), (np.angle(z) / (2 * np.pi) * P) % P


# ---------------------------------------------------------------- where to start

def CameraUp(cameras):
	"""
	Up, from the cameras alone: the direction square to every camera's image rows (cameras mounted without a
	roll keep them level; one looking straight down has them level whatever its roll), on the cameras' side.
	Returns (up, how well it is determined: the smallest singular value over the next, small is good).
	"""
	X = np.array([c.R[0] for c in cameras])
	_, s, vt = np.linalg.svd(X)
	up = vt[-1]
	look = np.mean([c.R[2] for c in cameras], 0)   # the cameras look down: against up
	if up @ look > 0:
		up = -up
	return up, float(s[-1] / max(s[-2], 1e-12)) if len(s) > 1 else 1.0


def Aim(cameras):
	"""The point nearest every camera's optical axis: what they look at."""
	A, b = np.zeros((3, 3)), np.zeros(3)
	for c in cameras:
		M = np.eye(3) - np.outer(c.R[2], c.R[2])
		A += M
		b += M @ c.center
	return np.linalg.solve(A, b) if np.linalg.cond(A) < 1e6 else np.mean([c.center for c in cameras], 0)


# ---------------------------------------------------------------- the fit

class FitError(Exception):
	pass


def Fit(cameras, images, size=None, pitch=INCH, up=None, maps=None, say=None):
	"""
	The enclosure from calibrated cameras and a picture of the empty arena from each.

	cameras  pose3d.Cameras; images: grey uint8 pictures (H, W), one per camera, in the same order
	size     the floor's nominal size (W, D), inches, or None: used only to place a wall without a grid
	pitch    the grid's square in the calibration's units (25.4: a 1-inch grid, calibration in mm)
	up       a rough up (3,), or None: from the cameras (CameraUp)
	maps     the pictures' ridge maps, if made already (RidgeMap)
	say      a function to tell what it does, or None
	Returns a dict: corners (4, 3) in order around the floor, up, grid (what the check and Identity need, saved
	with the cage), pitch, squares, walls, scores, per-camera ratios, seconds. Raises FitError when it can't.
	"""
	say = say or (lambda text: None)
	t0 = time.monotonic()
	cameras = list(cameras)
	if len(cameras) < 2 or len(images if images is not None else maps) != len(cameras):
		raise FitError("needs 2 or more cameras, and one picture for each")
	aim = Aim(cameras)
	centres = np.array([c.center for c in cameras])
	far = np.linalg.norm(centres - aim, axis=1)
	reach = float(np.median(far))                                               # how far the cameras are
	square = [pitch * float(np.mean(c.f)) / max(d, 1e-9) for c, d in zip(cameras, far)]   # the grid's square, px
	vw = Views(cameras, images, maps, square)
	say("{} cameras' ridge maps; the grid's square {} px where they look ({:.1f} s)".format(len(cameras),
		"-".join("{:.0f}".format(x) for x in (min(square), max(square))), time.monotonic() - t0))
	if up is None:
		up, spread = CameraUp(cameras)
		if spread > 0.3:
			say("the cameras' image rows don't agree on a level ({:.2f}): up is a rough guess".format(spread))
	up = Unit(up)
	e1 = PlaneAxes(up, np.array([1.0, 0, 0]) if abs(up[0]) < 0.9 else np.array([0, 1.0, 0]))[0]
	e2 = np.cross(up, e1)
	c0 = aim
	half = 0.4 * min(size) * INCH if size else 0.2 * reach                     # the patch that is floor for sure
	half = min(half, 0.35 * reach)
	R1 = R2 = (-half, half)
	# The floor is below every camera and, at most a camera's distance, below what they look at
	lowest = float(((vw.centres - aim) @ up).min())
	dlo, dhi = -0.9 * reach, min(lowest - 0.03 * reach, 0.5 * reach)
	say("start: up {} from the cameras; the floor swept {:.0f} to {:.0f} mm from where they look, over a {:.0f} mm "
		"patch".format(np.round(up, 3).tolist(), dlo, dhi, 2 * half))

	def FloorScore(d, al, be, level, rr1=R1, rr2=R2, step=None):
		n = Unit(up + al * e1 + be * e2)
		a1, a2 = PlaneAxes(n, e1)
		P, _, _ = Lattice(c0 + d * up, a1, a2, rr1, rr2, step or {"wide": 4.0, "coarse": 2.0, "fine": 1.0}[level])
		return Agreement(*vw.Sample(P, level))

	def Sweep(values, score):
		sc = [score(x) for x in values]
		return float(values[int(np.argmax(sc))]), max(sc)

	# ---- the floor: height and tilt, wide to fine
	d, al, be = Sweep(np.arange(dlo, dhi, 3.0), lambda x: FloorScore(x, 0.0, 0.0, "wide"))[0], 0.0, 0.0
	for level, dr, dstep, ar, astep, rounds in (("wide", 30, 3.0, 0.15, 0.01, 2), ("coarse", 12, 1.0, 0.03, 0.004, 2),
			("fine", 3, 0.25, 0.008, 0.001, 2), ("fine", 1, 0.1, 0.002, 0.0005, 1)):
		for _ in range(rounds):
			d = Sweep(np.arange(d - dr, d + dr + 1e-9, dstep), lambda x: FloorScore(x, al, be, level))[0]
			al = Sweep(np.arange(al - ar, al + ar + 1e-9, astep), lambda x: FloorScore(d, x, be, level))[0]
			be = Sweep(np.arange(be - ar, be + ar + 1e-9, astep), lambda x: FloorScore(d, al, x, level))[0]
		say("  floor ({}): {:+.1f} mm, tilt {:+.2f} / {:+.2f} deg, agreement {:.3f} ({:.1f} s)".format(level, d,
			np.degrees(al), np.degrees(be), FloorScore(d, al, be, level), time.monotonic() - t0))
	n = Unit(up + al * e1 + be * e2)
	pf = c0 + d * up                                                            # a point on the floor
	floorAgreement = FloorScore(d, al, be, "fine")
	curve = [(float(x - d), FloorScore(x, al, be, "fine")) for x in np.arange(d - 6, d + 6.1, 3.0)]

	# ---- the grid's axes, lines and square, on the mean rectified floor
	a1, a2 = PlaneAxes(n, e1)

	def MeanFloor(origin, b1, b2, r1, r2, step):
		P, s, t = Lattice(origin, b1, b2, r1, r2, step)
		S, V = vw.Sample(P)
		F = (S * V).sum(0) / np.maximum(V.sum(0), 1)
		return P, F

	P, F = MeanFloor(pf, a1, a2, R1, R2, 1.0)
	co = lambda th: ((P - pf) @ (math.cos(th) * a1 + math.sin(th) * a2))
	ths = np.radians(np.arange(0, 90, 1.0))
	th = max(ths, key=lambda x: Fold(F, co(x), pitch)[0] + Fold(F, co(x + math.pi / 2), pitch)[0])
	ths = th + np.radians(np.arange(-1.5, 1.5001, 0.05))
	th = max(ths, key=lambda x: Fold(F, co(x), pitch)[0] + Fold(F, co(x + math.pi / 2), pitch)[0])
	contrast = Fold(F, co(th), pitch)[0] + Fold(F, co(th + math.pi / 2), pitch)[0]
	# g1 nearer e1 (so the same box gives the same axes), g2 = n x g1
	th = min([th + k * math.pi / 2 for k in range(-2, 3)], key=lambda x: abs(math.atan2(math.sin(x), math.cos(x))))
	g1 = Unit(math.cos(th) * a1 + math.sin(th) * a2)
	g2 = np.cross(n, g1)
	if contrast < 0.05:
		raise FitError("no grid on the floor (fold contrast {:.3f}): is there a {:.1f} mm grid?".format(contrast, pitch))
	say("  grid axes (fold contrast {:.3f}) ({:.1f} s)".format(contrast, time.monotonic() - t0))

	# ---- where the floor's grid ends, each way along each axis: the walls stand there. Strips across the floor,
	# outwards from the patch, until the cameras stop agreeing on them
	cross = (-0.8 * half, 0.8 * half)

	def Strip(gn, ga, a, b):
		Ps, _, _ = Lattice(pf, gn, ga, (min(a, b), max(a, b)), cross, 2.0)
		return Agreement(*vw.Sample(Ps, "coarse"), min_n=100)
	ends, profiles = {}, {}
	for ax, (gn, ga) in enumerate(((g1, g2), (g2, g1))):
		middle = Strip(gn, ga, -0.5 * half, 0.5 * half)
		for side, sign in (("lo", -1), ("hi", 1)):
			offs = np.arange(0.5 * half, half + reach, 4.0)
			prof = np.array([Strip(gn, ga, sign * o, sign * (o + 10)) for o in offs])
			below = prof < 0.4 * middle
			k = next((i for i in range(len(below) - 2) if below[i:i + 3].all()), len(below) - 1)
			ends[(ax, side)] = float(sign * (offs[k] + 5))
			profiles[(ax, side)] = (sign * (offs + 5), prof, middle)
	say("  the floor's grid ends at {} mm along axis 0 and {} mm along axis 1 ({:.1f} s)".format(
		" / ".join("{:+.0f}".format(ends[(0, sd)]) for sd in ("lo", "hi")),
		" / ".join("{:+.0f}".format(ends[(1, sd)]) for sd in ("lo", "hi")), time.monotonic() - t0))

	# ---- the walls: vertical planes along each grid axis, swept near where the floor ends, scored by the cameras on
	# the wall's inner side; a wall has a grid when its picture, rectified, folds at the grid's square as the floor's
	walls, sweeps = {}, {}
	for ax, (gn, ga) in enumerate(((g1, g2), (g2, g1))):
		along = (ends[(1 - ax, "lo")] + 5, ends[(1 - ax, "hi")] - 5)
		for side, sign in (("lo", -1), ("hi", 1)):
			inner = lambda off: [c for c in range(len(cameras)) if sign * ((vw.centres[c] - pf) @ gn - off) < 0]

			def WallSample(off, level, step):
				cams = inner(off)
				if len(cams) < 2:
					return None, None, cams
				Pw, s, z = Lattice(pf + off * gn, ga, n, along, WALL_BAND, step)
				S, V = vw.Sample(Pw, level, cams=cams)
				return S, V, (s, z)

			def WallScore(off, level):
				S, V, _ = WallSample(off, level, 2.0 if level == "coarse" else 1.0)
				return -1.0 if S is None else Agreement(S, V)
			offs = ends[(ax, side)] + sign * np.arange(-35.0, 35.001, 1.0)
			sc = np.array([WallScore(o, "coarse") for o in offs])
			sweeps[(ax, side)] = (offs, sc)
			o = float(offs[int(np.argmax(sc))])
			fs = np.arange(o - 2, o + 2.001, 0.25)
			fsc = [WallScore(x, "fine") for x in fs]
			off = float(fs[int(np.argmax(fsc))])
			S, V, sz = WallSample(off, "fine", 1.0)
			wallContrast, phase = 0.0, [0.0, 0.0]
			if S is not None:
				Fw = (S * V).sum(0) / np.maximum(V.sum(0), 1)
				ss, zz = np.meshgrid(*sz, indexing="ij")
				# A grid has lines both ways; the floor's lines going on under a clear pane fold along it only
				(ca, pa), (cz, pz) = Fold(Fw, ss, pitch), Fold(Fw, zz, pitch)
				wallContrast, phase = 2 * min(ca, cz), [float(pa), float(pz)]
			walls[(ax, side)] = dict(off=off, score=float(max(fsc)), contrast=float(wallContrast), phase=phase,
				floor_end=ends[(ax, side)], cameras=len(inner(off)), along=list(along))
		say("  walls along axis {}: {} ({:.1f} s)".format(ax, "; ".join("{} {:+.1f} mm (agreement {:.3f}, grid contrast "
			"{:.3f}, {} cameras)".format(sd, walls[(ax, sd)]["off"], walls[(ax, sd)]["score"], walls[(ax, sd)]["contrast"],
			walls[(ax, sd)]["cameras"]) for sd in ("lo", "hi")), time.monotonic() - t0))
	for w in walls.values():
		w["from"] = "grid" if w["contrast"] >= WALL_CONTRAST * contrast and w["score"] > 0.05 else None
	got = [[sd for sd in ("lo", "hi") if walls[(ax, sd)]["from"] == "grid"] for ax in (0, 1)]
	nominal = sorted(float(x) * INCH for x in size) if size else None
	if any(len(g) < 2 for g in got) and nominal:
		if all(len(g) == 1 for g in got):
			# Which side is the long one: the axis whose floor is longer
			longer = 0 if ends[(0, "hi")] - ends[(0, "lo")] >= ends[(1, "hi")] - ends[(1, "lo")] else 1
			span = [nominal[1], nominal[0]] if longer == 0 else [nominal[0], nominal[1]]
		else:
			span = [None, None]
			for ax in (0, 1):
				if len(got[ax]) == 2:
					width = walls[(ax, "hi")]["off"] - walls[(ax, "lo")]["off"]
					span[1 - ax] = nominal[0] if abs(width - nominal[1]) <= abs(width - nominal[0]) else nominal[1]
				elif len(got[1 - ax]) == 0:
					span[ax] = None
		for ax in (0, 1):
			if len(got[ax]) == 1 and span[ax] is not None:
				have = walls[(ax, got[ax][0])]
				missing = "hi" if got[ax][0] == "lo" else "lo"
				walls[(ax, missing)].update(off=have["off"] + (span[ax] if missing == "hi" else -span[ax]),
					**{"from": "nominal size"})
	for (ax, side), w in walls.items():
		if w["from"] is None:
			# No grid on it, and no size to place it from: where the floor's grid ends
			w.update(off=w["floor_end"], **{"from": "floor's end"})
		# A wall placed from the size: past where the floor's grid ends, or well inside it (a clear pane may stand on
		# a floor that goes on a little past it), says the size is wrong
		beyond = (w["off"] - w["floor_end"]) * (1 if side == "hi" else -1)
		if w["from"] == "nominal size" and not -3 * pitch <= beyond <= 0.5 * pitch:
			say("  wall {}_{} placed at {:+.1f} mm from the size, but the floor's grid ends at {:+.1f}: is the size "
				"right?".format(ax, side, w["off"], w["floor_end"]))
			w["size_doubt"] = True
	if not any(w["from"] == "grid" for w in walls.values()):
		raise FitError("no wall with a grid found")

	# ---- how high the walls' grid goes, on the wall seen best: quarter-square bands up it, until its cameras stop
	# agreeing (3 bands in a row under 0.3 of the lower wall's)
	(bx, bs), bw = max(((k, w) for k, w in walls.items() if w["from"] == "grid"), key=lambda kw: kw[1]["score"])
	bn, ba = (g1, g2) if bx == 0 else (g2, g1)
	bsign = -1 if bs == "lo" else 1
	bcams = [c for c in range(len(cameras)) if bsign * ((vw.centres[c] - pf) @ bn - bw["off"]) < 0]
	bands = np.arange(0.0, 1.2 * reach, pitch / 4)
	bandScores = [Agreement(*vw.Sample(Lattice(pf + bw["off"] * bn, ba, n, bw["along"], (z, z + pitch / 4), 1.0)[0],
		cams=bcams), min_n=100) for z in bands]
	base = float(np.median(bandScores[4:24]))
	low = [x < 0.3 * base for x in bandScores]
	top = next((float(bands[i]) for i in range(4, len(low) - 2) if low[i] and low[i + 1] and low[i + 2]), None)
	say("  the walls' grid goes up {} on the wall seen best ({}_{}) ({:.1f} s)".format(
		"{:.0f} mm ({:.0f} in)".format(top, top / INCH) if top else "past what the cameras see", bx, bs,
		time.monotonic() - t0))
	wid = [walls[(ax, "hi")]["off"] - walls[(ax, "lo")]["off"] for ax in (0, 1)]

	# ---- the whole floor between the walls (inset 8 mm): its height again, and the square
	r1 = (walls[(0, "lo")]["off"] + 8, walls[(0, "hi")]["off"] - 8)
	r2 = (walls[(1, "lo")]["off"] + 8, walls[(1, "hi")]["off"] - 8)
	dd = np.arange(-3, 3.001, 0.25)
	sc2 = [Agreement(*vw.Sample(Lattice(pf + x * n, g1, g2, r1, r2, 1.5)[0])) for x in dd]
	pf = pf + float(dd[int(np.argmax(sc2))]) * n
	P, F = MeanFloor(pf, g1, g2, r1, r2, 1.0)
	pitches, phases = [], []
	for g in (g1, g2):
		cc = (P - pf) @ g
		ps = np.arange(pitch * 0.9, pitch * 1.1, 0.02)
		p = float(ps[int(np.argmax([Fold(F, cc, x)[0] for x in ps]))])
		ps = np.arange(p - 0.03, p + 0.0301, 0.002)
		pitches.append(float(ps[int(np.argmax([Fold(F, cc, x)[0] for x in ps]))]))
		phases.append(float(Fold(F, cc, pitch)[1]))
	squares = [wid[0] / pitches[0], wid[1] / pitches[1]]
	say("  floor between the walls {:.1f} x {:.1f} mm = {:.2f} x {:.2f} squares of {:.2f} / {:.2f} mm ({:.1f} s)".format(
		wid[0], wid[1], squares[0], squares[1], pitches[0], pitches[1], time.monotonic() - t0))

	corner = lambda o1, o2: pf + o1 * g1 + o2 * g2
	lo1, hi1, lo2, hi2 = (walls[k]["off"] for k in ((0, "lo"), (0, "hi"), (1, "lo"), (1, "hi")))
	corners = np.array([corner(lo1, lo2), corner(hi1, lo2), corner(hi1, hi2), corner(lo1, hi2)])
	grid = dict(origin=pf.tolist(), up=n.tolist(), axes=[g1.tolist(), g2.tolist()], pitch=pitch,
		phase=phases, walls=[[lo1, hi1], [lo2, hi2]],
		gridded=[[walls[(ax, sd)]["from"] == "grid" for sd in ("lo", "hi")] for ax in (0, 1)],
		wall_phase=[[walls[(ax, sd)]["phase"] for sd in ("lo", "hi")] for ax in (0, 1)])
	res = dict(corners=corners, up=n, grid=grid, wall_top=top, wall_bands=bandScores,
		pitch_mm=pitches, squares=squares, floor_mm=wid,
		floor_agreement=floorAgreement, floor_curve=curve, grid_contrast=contrast,
		walls={"{}_{}".format(ax, sd): w for (ax, sd), w in walls.items()}, sweeps=sweeps, profiles=profiles)
	res["ratios"] = Ratios(vw, grid)
	lines = CheckLines(grid, top or WALL_BAND[1])
	res["misses"] = []
	for c in range(len(cameras)):
		uv, miss = Misses(vw, c, lines, seen=lambda P, c=c: Seen(cameras[c], P, grid, top or WALL_BAND[1]))
		res["misses"].append(dict(uv=uv, px=miss))
	res["seconds"] = time.monotonic() - t0
	say("  the grid projected into each camera, on its lines / half a square off: {} ({:.1f} s)".format(
		", ".join("{} {:.2f}".format(c.name, r) for c, r in zip(cameras, res["ratios"])), res["seconds"]))
	say("  where each camera's picture has the lines, px from the fit's (median / 90%): {}".format(", ".join(
		"{} {}".format(c.name, MissText(m["px"])) for c, m in zip(cameras, res["misses"]))))
	# A fit whose grid doesn't land on most cameras' lines found something else (the pictures aren't of the empty
	# arena: a board or animals that stayed)
	res["ok"] = bool(np.median(res["ratios"]) >= GRID_RATIO_OK)
	return res


def MissText(px):
	return "{:.1f} / {:.1f}".format(np.median(px), np.percentile(px, 90)) if len(px) else "-"


# ---------------------------------------------------------------- the check and camera identity

def GridLines(grid, inset=10.0, samples=4.0):
	"""
	The floor grid's lines between the walls (inset mm from them), as 3D polylines, and the same lines half a
	square off (lines, shifted): from Fit's grid (or a saved cage's).
	"""
	pf, (g1, g2) = np.asarray(grid["origin"], float), (np.asarray(g, float) for g in grid["axes"])
	(lo1, hi1), (lo2, hi2) = grid["walls"]
	pitch, (ph1, ph2) = grid["pitch"], grid["phase"]
	corner = lambda o1, o2: pf + np.multiply.outer(o1, g1) + np.multiply.outer(o2, g2)
	k1 = np.arange(math.ceil((lo1 + inset - ph1) / pitch), math.floor((hi1 - inset - ph1) / pitch) + 1) * pitch + ph1
	k2 = np.arange(math.ceil((lo2 + inset - ph2) / pitch), math.floor((hi2 - inset - ph2) / pitch) + 1) * pitch + ph2
	along2 = np.arange(lo2 + inset, hi2 - inset, samples)
	along1 = np.arange(lo1 + inset, hi1 - inset, samples)
	lines = [corner(np.full_like(along2, x), along2) for x in k1] + [corner(along1, np.full_like(along1, y)) for y in k2]
	shifted = [L + 0.5 * pitch * (g1 if i < len(k1) else g2) for i, L in enumerate(lines)]
	return lines, shifted


def CheckLines(grid, height, inset=5.0, samples=3.0):
	"""
	The lines the check measures and draws: the floor's (inset mm from the walls) and each gridded wall's, up to
	height, as [(3D polyline (k, 3), the direction across it, "floor" or "wall")].
	"""
	pf, n = np.asarray(grid["origin"], float), np.asarray(grid["up"], float)
	g1, g2 = (np.asarray(g, float) for g in grid["axes"])
	pitch = grid["pitch"]
	(lo1, hi1), (lo2, hi2) = grid["walls"]
	ph1, ph2 = grid["phase"]
	ticks = lambda lo, hi, ph: np.arange(math.ceil((lo - ph) / pitch), math.floor((hi - ph) / pitch) + 1) * pitch + ph
	out = []
	along2, along1 = np.arange(lo2 + inset, hi2 - inset, samples), np.arange(lo1 + inset, hi1 - inset, samples)
	out += [(pf + x * g1 + np.multiply.outer(along2, g2), g1, "floor") for x in ticks(lo1 + inset, hi1 - inset, ph1)]
	out += [(pf + y * g2 + np.multiply.outer(along1, g1), g2, "floor") for y in ticks(lo2 + inset, hi2 - inset, ph2)]
	for ax, (gn, ga, (lo, hi)) in enumerate(((g1, g2, (lo2, hi2)), (g2, g1, (lo1, hi1)))):
		for k in range(2):
			if not grid["gridded"][ax][k]:
				continue
			base = pf + grid["walls"][ax][k] * gn
			pa, pz = grid.get("wall_phase", [[[0, 0]] * 2] * 2)[ax][k]
			zz, uu = np.arange(inset, height, samples), np.arange(lo + inset, hi - inset, samples)
			out += [(base + u * ga + np.multiply.outer(zz, n), ga, "wall") for u in ticks(lo + inset, hi - inset, pa)]
			out += [(base + z * n + np.multiply.outer(uu, ga), n, "wall") for z in ticks(inset, height, pz)]
	return out


def Seen(camera, P, grid, top):
	"""
	Which of the points P (k, 3), on the box's floor or walls, camera sees: the gridded walls (taken as opaque, up
	to top mm) hide what is behind them, and a camera outside a wall sees its back, not its grid.
	"""
	P = np.asarray(P, float)
	pf, n = np.asarray(grid["origin"], float), np.asarray(grid["up"], float)
	axes = [np.asarray(g, float) for g in grid["axes"]]
	C = camera.center
	seen = np.ones(len(P), bool)
	for ax in (0, 1):
		gn, ga = axes[ax], axes[1 - ax]
		lo, hi = grid["walls"][1 - ax]
		for k, sign in ((0, -1), (1, 1)):
			if not grid["gridded"][ax][k]:
				continue
			off = grid["walls"][ax][k]
			c = sign * ((C - pf) @ gn - off)                # > 0: the camera is outside this wall
			if c <= 0:
				continue
			p = sign * ((P - pf) @ gn - off)                # <= 0: the point is inside it, or on it
			with np.errstate(divide="ignore", invalid="ignore"):
				t = c / (c - p)
			X = C + t[:, None] * (P - C)
			u, z = (X - pf) @ ga, (X - pf) @ n
			seen &= ~((p <= 1.0) & (t > 0) & (t <= 1) & (u >= lo - 1) & (u <= hi + 1) & (z >= -1) & (z <= top + 1))
	return seen


def Misses(vw, c, lines, search=8.0, seen=None):
	"""
	How far camera c's picture puts the grid's lines from where the fit projects them: at each sample along the
	lines (CheckLines), the strongest ridge within search mm across the line, and its distance in pixels. Samples
	with no clear line near them are left out, and those seen(P) says the camera doesn't see. Returns (pixels
	(k, 2), miss px (k,)).
	"""
	ds = np.arange(-search, search + 1e-9, 0.25)
	U, M = [], []
	for P, across, _ in lines:
		uv0, ok0 = vw.Project(c, P)
		if seen is not None:
			ok0 &= seen(P)
		if ok0.sum() < 3:
			continue
		Q = P[None] + ds[:, None, None] * across
		uv, ok = vw.Project(c, Q.reshape(-1, 3))
		uv, ok = uv.reshape(len(ds), -1, 2), ok.reshape(len(ds), -1)
		v = np.where(ok, Remap(vw.maps["fine"][c], np.where(ok[..., None], uv, 0)).reshape(len(ds), -1), 0)
		best = v.argmax(0)
		use = ok0 & ok.all(0) & (v.max(0) > 0.15)
		j = np.nonzero(use)[0]
		U.append(uv0[j])
		M.append(np.linalg.norm(uv[best[j], j] - uv0[j], axis=-1))
	return (np.concatenate(U), np.concatenate(M)) if U else (np.zeros((0, 2)), np.zeros(0))


def MeanRidge(vw, c, ridge, lines):
	"""The mean of the ridge map `ridge` along 3D lines projected by camera c's calibration."""
	P = np.concatenate(lines)
	uv, ok = vw.Project(c, P)
	if not ok.any():
		return float("nan")
	return float(Remap(ridge, uv[ok]).mean())


def Ratios(vw, grid):
	"""Per camera: its picture's ridge on the floor grid projected into it, over the same half a square off."""
	lines, shifted = GridLines(grid)
	out = []
	for c in range(len(vw.cameras)):
		on, off = MeanRidge(vw, c, vw.maps["fine"][c], lines), MeanRidge(vw, c, vw.maps["fine"][c], shifted)
		out.append(on / off if off > 0 else float("nan"))
	return out


def Identity(cameras, images, grid, maps=None):
	"""
	Each picture against every camera's calibration: the floor grid projected with calibration j onto picture i,
	ridge on the lines over half a square off. A camera whose name is right matches its own calibration (the
	diagonal well over 1); pictures swapped between cameras match each other's.
	Returns dict(names, ratio (pictures x calibrations), match {picture's camera: the calibration it matches
	best, one to one}, swaps {camera: calibration} where they differ, weak [cameras whose picture matches no
	calibration: its grid hidden, or a camera moved]).
	"""
	from scipy.optimize import linear_sum_assignment
	vw = Views(cameras, images, maps)
	lines, shifted = GridLines(grid)
	names = [c.name for c in cameras]
	M = np.zeros((len(names), len(names)))
	for i in range(len(names)):
		ridge = vw.maps["fine"][i]
		for j in range(len(names)):
			# Calibration j's projection read on picture i (its size: the calibrations' are all the same here)
			on, off = MeanRidge(vw, j, ridge, lines), MeanRidge(vw, j, ridge, shifted)
			M[i, j] = on / off if off > 0 and np.isfinite(on) else 0.0
	ri, cj = linear_sum_assignment(-M)
	match = {names[i]: names[j] for i, j in zip(ri, cj)}
	swaps = {}
	for i, j in zip(ri, cj):
		# Another calibration fits picture i clearly better than its own
		if i != j and M[i, j] > IDENTITY_MARGIN * max(M[i, i], 1e-9):
			swaps[names[i]] = names[j]
	weak = [names[i] for i in range(len(names)) if M[i, i] < GRID_RATIO_OK and names[i] not in swaps]
	return dict(names=names, ratio=M.round(3).tolist(), match=match, swaps=swaps, weak=weak)


def IdentityText(identity):
	names, M = identity["names"], np.asarray(identity["ratio"])
	w = max(6, max(len(n) for n in names))
	rows = ["picture \\ calibration".ljust(w + 2) + " ".join(n[-w:].rjust(w) for n in names)]
	rows += [n[-w:].rjust(w + 2) + " ".join("{:{}.2f}".format(x, w) for x in M[i]) for i, n in enumerate(names)]
	return "\n".join(rows)


# ---------------------------------------------------------------- the cage

def MakeCage(fit, cameras, height=None, margin=pose3d.CAGE_MARGIN):
	"""
	A pose3d.Cage from Fit's result, as high as height (mm; None: as high as the walls' grid goes, else
	pose3d.CAGE_HEIGHT). Its clicks are its corners projected into each camera that sees them (reproj_px 0):
	Cage.Check then refuses it on another calibration, as it does a clicked cage, and campy-live-view can move
	them as clicked corners.
	"""
	if height is None:
		height = fit.get("wall_top") or pose3d.CAGE_HEIGHT
	corners = np.asarray(fit["corners"], float)
	clicks = {}
	vw = _Bare(cameras)
	for c, cam in enumerate(cameras):
		uv, ok = vw.Project(c, corners)
		if ok.any():
			clicks[cam.name] = [[round(float(u[0]), 2), round(float(u[1]), 2)] if k else None for u, k in zip(uv, ok)]
	return pose3d.Cage(corners, fit["up"], height, margin, clicks, 0.0)


class _Bare(Views):
	# Views without pictures: only Project, with each camera's calibrated size as its picture's
	def __init__(self, cameras):
		self.cameras = list(cameras)
		self.shapes = [(int(c.size[1]), int(c.size[0])) if c.size is not None else (10 ** 6, 10 ** 6) for c in cameras]
		self.fold = [0.8 * FoldRadius2(c.dist) for c in self.cameras]
		self.centres = np.array([c.center for c in self.cameras])


def Describe(fit, cameras):
	"""What the cage's file keeps of the fit: where the cage came from, and how well it fits."""
	return dict(method="campy-cage: plane sweep of the floor's and walls' grid over every camera's picture",
		pitch_mm=np.round(fit["pitch_mm"], 3).tolist(), squares=np.round(fit["squares"], 2).tolist(),
		floor_mm=np.round(fit["floor_mm"], 1).tolist(), floor_agreement=round(fit["floor_agreement"], 3),
		walls={k: {"from": w["from"], "agreement": round(w["score"], 3), "grid_contrast": round(w["contrast"], 3)}
			for k, w in fit["walls"].items()},
		wall_top_mm=None if fit.get("wall_top") is None else round(fit["wall_top"], 1),
		ratios={c.name: round(r, 2) for c, r in zip(cameras, fit["ratios"])},
		miss_px={c.name: [round(float(np.median(m["px"])), 2), round(float(np.percentile(m["px"], 90)), 2)]
			if len(m["px"]) else None for c, m in zip(cameras, fit["misses"])}, seconds=round(fit["seconds"], 1))


def LoadGrid(path):
	"""The grid a cage's file was made from (Fit's grid), or None: a cage clicked by hand, or no file."""
	try:
		with open(path, encoding="utf-8") as f:
			d = json.load(f)
	except (OSError, ValueError):
		return None
	return d.get("grid") if d.get("kind") == pose3d.CAGE_KIND and d.get("made") == "grid" else None


# ---------------------------------------------------------------- pictures

FLOOR, WALL, BOX = (60, 220, 60), (230, 200, 40), (0, 215, 255)   # BGR: green, cyan, yellow


MISS_COLOURS = ((1.5, (60, 220, 60)), (3.0, (0, 220, 255)), (math.inf, (40, 40, 255)))   # under 1.5 px, 3, more


def DrawCheck(image, cameras, c, grid, cage, ratio=None, misses=None, note=None):
	"""
	Camera c's picture with the fitted grid on it, thin and half see-through so its own lines show: the floor's
	(green), the gridded walls' up to the cage's top (cyan), the cage (yellow). misses (Misses: pixels, px): dots
	where the lines were measured, green where the picture's line is within 1.5 px, yellow 3, red further. BGR.
	"""
	import cv2
	vw = _Bare(cameras)
	vw.shapes[c] = image.shape[:2]
	im = image if image.ndim == 3 else cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
	im = cv2.convertScaleAbs(im, alpha=1.8, beta=10)
	over = im.copy()
	lines = CheckLines(grid, cage.height, inset=0.0, samples=2.0)
	box = [np.linspace(a, b, 160) for a, b in cage.Edges()]
	for group, color in (([L for L, _, k in lines if k == "floor"], FLOOR), ([L for L, _, k in lines if k == "wall"],
			WALL), (box, BOX)):
		for L in group:
			uv, ok = vw.Project(c, L)
			if color != BOX:
				ok &= Seen(cameras[c], L, grid, cage.height)    # the grid's lines where the camera sees them
			# each run of points it sees, as its own line
			runs = np.split(np.arange(len(ok)), np.nonzero(np.diff(ok.astype(int)))[0] + 1)
			for r in runs:
				if len(r) > 1 and ok[r[0]]:
					cv2.polylines(over, [np.round(uv[r] * 4).astype(np.int32)], False, color, 1, cv2.LINE_AA, shift=2)
	im = cv2.addWeighted(over, 0.6, im, 0.4, 0)
	text = cameras[c].name + ("" if ratio is None else ": grid on its lines x{:.2f}".format(ratio))
	if misses is not None and len(misses[1]):
		uv, px = misses
		for k in range(0, len(px), 2):
			color = next(col for lim, col in MISS_COLOURS if px[k] < lim)
			cv2.circle(im, (int(round(uv[k, 0])), int(round(uv[k, 1]))), 2, color, -1, cv2.LINE_AA)
		text += ", lines {} px off (median / 90%)".format(MissText(px))
	for line, y in ((text, 40), (note, 80)):
		if line:
			cv2.putText(im, line, (20, y), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 5, cv2.LINE_AA)
			cv2.putText(im, line, (20, y), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2, cv2.LINE_AA)
	return im


def Sheet(pictures, tile=(640, 512), per_row=4):
	"""The pictures side by side, each scaled to tile (width, height)."""
	import cv2
	tiles = [cv2.resize(p, tile, interpolation=cv2.INTER_AREA) for p in pictures]
	while len(tiles) % per_row:
		tiles.append(np.zeros_like(tiles[0]))
	return np.vstack([np.hstack(tiles[i:i + per_row]) for i in range(0, len(tiles), per_row)])


def VideoBackground(path, frames=31, percentile=50, ffmpeg=None):
	"""
	The empty arena from a video: per pixel, a percentile (the median by default) of `frames` frames spread
	through it, read with ffmpeg (in one pass for a short video, else seeking to each). Animals that move, or a
	calibration board, are gone where they
	stayed less than that share of the frames; a lower percentile keeps the darker value, the floor under
	something brighter (a white board, white mice). Grey uint8 (H, W), full range.
	"""
	from campy import videoread
	info = videoread.VideoInfo(path, ffmpeg)
	seconds = info.frames / info.rate if info.rate and info.frames else 0.0
	if seconds <= 0:
		raise ValueError("{}: no duration".format(path))
	h, w = info.height, info.width
	exe = ffmpeg or videoread.FfmpegExe()
	if info.frames <= 40 * frames:
		# A short video (a calibration's): one pass, every k-th frame, is faster than seeking to each (a seek
		# decodes from the keyframe before it, and a slow-rate video has few)
		k = max(1, info.frames // frames)
		r = subprocess.run([exe, "-v", "error", "-nostdin", "-i", path, "-map", "0:v:0", "-vf",
			"select=not(mod(n\\,{}))".format(k), "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
			capture_output=True)
		got = np.frombuffer(r.stdout, np.uint8)[:len(r.stdout) // (h * w) * h * w].reshape(-1, h, w)
		if r.returncode == 0 and len(got) >= max(3, frames // 2):
			return np.percentile(got, percentile, axis=0).round().astype(np.uint8)
	got = []
	for t in (np.arange(frames) + 0.5) / frames * seconds:
		cmd = [exe, "-v", "error", "-nostdin", "-ss", "{:.3f}".format(t), "-i", path,
			"-map", "0:v:0", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
		r = subprocess.run(cmd, capture_output=True)
		if r.returncode == 0 and len(r.stdout) >= h * w:
			got.append(np.frombuffer(r.stdout[:h * w], np.uint8).reshape(h, w))
	if len(got) < max(3, frames // 2):
		raise ValueError("{}: read {} of {} frames".format(path, len(got), frames))
	# ffmpeg's gray is full range already (a limited-range video stretched to 0-255)
	return np.percentile(np.stack(got), percentile, axis=0).round().astype(np.uint8)


PICTURE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")


def Matches(name, camera):
	"""Whether a picture's or a camera folder's name is camera's: the same, or ending in _<camera> (Camera0_mid for
	mid), or camera ending in _<name>."""
	a, b = name.lower(), camera.lower()
	return a == b or a.endswith("_" + b) or b.endswith("_" + a)


def FindPictures(where, cameras):
	"""
	For each camera, its picture or video in where: a folder of pictures (<camera>.png, ...) or a recording's
	folder (<camera>/<video>, or <camera>/calibration_images/<video> for a calibration recording). Returns
	[(kind 'picture' or 'video', path)] in the cameras' order; SystemExit naming what is missing.
	"""
	from campy import segments, videoread
	entries = sorted(os.listdir(where))
	found, missing = [], []
	for cam in cameras:
		pics = [e for e in entries if os.path.splitext(e)[1].lower() in PICTURE_EXTENSIONS and
			Matches(os.path.splitext(e)[0], cam.name)]
		dirs = [e for e in entries if os.path.isdir(os.path.join(where, e)) and Matches(e, cam.name)]
		if len(pics) == 1:
			found.append(("picture", os.path.join(where, pics[0])))
			continue
		videos = []
		for d in dirs:
			for sub in (os.path.join(where, d), os.path.join(where, d, "calibration_images")):
				if os.path.isdir(sub):
					videos += [os.path.join(sub, v) for v in sorted(os.listdir(sub))
						if v.lower().endswith(videoread.VIDEO_EXTENSIONS) and not v.startswith(".")]
		# One video per camera: a restarted ffmpeg's first file or its joined one (the biggest), never a part, nor
		# campy-session post's own copies (remux: keep's .fragmented.mp4, a remux or join stopped part way)
		videos = [v for v in videos if ".part" not in os.path.basename(v) and not segments.NotTheVideo(os.path.basename(v))]
		if len(pics) > 1 or len(dirs) > 1 or not videos:
			missing.append("{} ({})".format(cam.name, "more than one: " + ", ".join(pics + dirs)
				if len(pics) > 1 or len(dirs) > 1 else "no picture or video"))
			continue
		found.append(("video", max(videos, key=os.path.getsize)))
	if missing:
		raise SystemExit("{}: {}".format(where, "; ".join(missing)))
	return found


def CheckFolder(calibration):
	"""Where campy-cage puts its pictures: beside the calibration, calibration.toml -> calibration.cage_check/."""
	return os.path.splitext(calibration)[0] + ".cage_check"


# ---------------------------------------------------------------- campy-cage

def Main(argv=None):
	import cv2
	p = argparse.ArgumentParser(prog="campy-cage", description="The cage (<calibration>.cage.json, which campy-live "
		"uses) from the enclosure's own 1-inch grid, with no clicking; or, with --check, whether each camera's "
		"picture matches its own calibration.")
	p.add_argument("calibration", help="calibration.toml")
	p.add_argument("where", help="a recording's folder (each camera's video, or a calibration recording's), or a "
		"folder of pictures of the empty arena named after the cameras (mid.png or Camera0_mid.png)")
	p.add_argument("--size", default="", help="the floor's size in inches, W,D (18,10): places a wall that has no grid "
		"(a clear pane) from the opposite one")
	p.add_argument("--height", type=float, default=None, help="the cage's height, mm (default: as high as the walls' "
		"grid goes, else {:g})".format(pose3d.CAGE_HEIGHT))
	p.add_argument("--margin", type=float, default=pose3d.CAGE_MARGIN, help="mm past the walls, floor and top that "
		"still count as in (default %(default)g)")
	p.add_argument("--pitch", type=float, default=INCH, help="the grid's square in the calibration's units (default "
		"%(default)g: a 1-inch grid, calibration in mm)")
	p.add_argument("--frames", type=int, default=31, help="frames per video for the empty arena (default %(default)s)")
	p.add_argument("--percentile", type=float, default=50, help="per pixel, of those frames (default %(default)g: the "
		"median; lower keeps the darker floor under a white board or white mice)")
	p.add_argument("--out", default="", help="the cage's file (default: beside the calibration, <calibration>.cage.json)")
	p.add_argument("--check", action="store_true", help="only check the cameras' names: each picture against every "
		"camera's calibration, with the grid of the cage made before")
	p.add_argument("--force", action="store_true", help="save the cage even when a picture matches another camera's "
		"calibration better than its own")
	a = p.parse_args(argv)
	say = lambda text: print(text, flush=True)
	t0 = time.monotonic()
	cameras = pose3d.LoadCalibration(a.calibration)
	cagePath = a.out or pose3d.CagePath(a.calibration)
	sources = FindPictures(a.where, cameras)

	def Picture(source):
		kind, path = source
		if kind == "picture":
			return cv2.imread(path, cv2.IMREAD_GRAYSCALE)
		return VideoBackground(path, a.frames, a.percentile)
	# Each camera's video read by an ffmpeg of its own, side by side
	from concurrent.futures import ThreadPoolExecutor
	with ThreadPoolExecutor(max(1, min(len(sources), os.cpu_count() or 1, 8))) as pool:
		images = list(pool.map(Picture, sources))
	for cam, im, (kind, path) in zip(cameras, images, sources):
		if im is None:
			raise SystemExit("could not read {}".format(path))
		size = [int(x) for x in cam.size] if cam.size is not None else None
		if size and [im.shape[1], im.shape[0]] != size:
			raise SystemExit("{}'s picture is {}x{}, its calibration's {}x{}".format(cam.name, im.shape[1], im.shape[0],
				*size))
	say("{} cameras' pictures from {} ({:.1f} s)".format(len(cameras), a.where, time.monotonic() - t0))
	out = CheckFolder(a.calibration)
	os.makedirs(out, exist_ok=True)
	t = time.monotonic()
	with ThreadPoolExecutor(max(1, min(len(images), os.cpu_count() or 1, 8))) as pool:
		maps = list(pool.map(RidgeMap, images))
	say("ridge maps ({:.1f} s)".format(time.monotonic() - t))
	if a.check:
		grid = LoadGrid(cagePath)
		if grid is None:
			raise SystemExit("{}: no cage made from the grid (run campy-cage without --check first)".format(cagePath))
		identity = Identity(cameras, images, grid, maps)
		say(IdentityText(identity))
		return Verdict(identity, say)
	size = [float(x) for x in re.split(r"[,x]", a.size)] if a.size else None
	try:
		fit = Fit(cameras, None, size=size, pitch=a.pitch, maps=maps, say=say)
	except FitError as e:
		raise SystemExit("No cage: {}".format(e))
	identity = Identity(cameras, images, fit["grid"], maps)
	say(IdentityText(identity))
	verdict = Verdict(identity, say)
	cage = MakeCage(fit, cameras, a.height, a.margin)
	pictures = []
	for c, cam in enumerate(cameras):
		note = "NOT ITS PICTURE: {}'s?".format(identity["swaps"][cam.name]) if cam.name in identity["swaps"] else None
		misses = fit["misses"][c]
		im = DrawCheck(images[c], cameras, c, fit["grid"], cage, fit["ratios"][c], (misses["uv"], misses["px"]), note)
		cv2.imwrite(os.path.join(out, "{}.jpg".format(cam.name)), im, [cv2.IMWRITE_JPEG_QUALITY, 90])
		cv2.imwrite(os.path.join(out, "{}_empty.png".format(cam.name)), images[c])
		pictures.append(im)
	cv2.imwrite(os.path.join(out, "sheet.jpg"), Sheet(pictures), [cv2.IMWRITE_JPEG_QUALITY, 85])
	weak = [cam.name for cam, r in zip(cameras, fit["ratios"]) if not r >= GRID_RATIO_OK]
	say("The cage: floor {:.0f} x {:.0f} mm ({:.2f} x {:.2f} squares of {:.2f} / {:.2f} mm), {:.0f} mm high{}, margin "
		"{:g} mm.".format(fit["floor_mm"][0], fit["floor_mm"][1], fit["squares"][0], fit["squares"][1],
		fit["pitch_mm"][0], fit["pitch_mm"][1], cage.height, " (the walls' grid's top)" if a.height is None and
		fit.get("wall_top") else "", cage.margin))
	if weak:
		say("Warning: the grid lands on the lines of {} by less than x{:g}: look at {}.".format(", ".join(weak),
			GRID_RATIO_OK, out))
	if not fit["ok"]:
		say("Not saved: the grid lands on most cameras' lines by less than x{:g}, so the fit found something else. "
			"Are these pictures of the empty arena? A calibration board held in the middle stays in the median: try "
			"--percentile 25 (the floor is darker than the board), or another recording; look at {}.".format(
			GRID_RATIO_OK, out))
		return 4
	if verdict and not a.force:
		say("Not saved: the pictures don't match their cameras' calibrations (--force saves it anyway).")
		return verdict
	if os.path.exists(cagePath):
		os.replace(cagePath, cagePath + ".old")
	cage.Save(cagePath, made="grid", grid=fit["grid"], fit=Describe(fit, cameras),
		identity={k: identity[k] for k in ("swaps", "weak")})
	say("Saved {} ({} kept as .old if there was one); the check's pictures in {} ({:.1f} s in all).".format(
		cagePath, os.path.basename(cagePath), out, time.monotonic() - t0))
	return 0


def Verdict(identity, say):
	"""0 when every picture matches its own calibration; 3 when some match another camera's (names swapped)."""
	if identity["swaps"]:
		say("CAMERA NAMES SWAPPED? Each of these pictures matches another camera's calibration better than its own "
			"(picture -> calibration): {}. Check the cameras' order (cameraSerials) before tracking.".format(
			", ".join("{} -> {}".format(k, v) for k, v in sorted(identity["swaps"].items()))))
		return 3
	if identity["weak"]:
		say("Each picture matches its own calibration best, but {} only weakly (grid hidden, or the camera moved?)."
			.format(", ".join(identity["weak"])))
	else:
		say("Every camera's picture matches its own calibration.")
	return 0


if __name__ == "__main__":
	sys.exit(Main())
