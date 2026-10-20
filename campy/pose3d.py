"""
Live 3D poses from one synchronized set of 2D predictions, camera by camera.

Given what a top-down SLEAP model found in each camera of one frame set, this
decides which detection in each camera is the same animal, triangulates each
animal's nodes to 3D, and keeps each animal's identity from one set to the
next. It is meant to run inside the live inference process (campy-live),
once per set, on one CPU core: numpy and scipy only.

The association is LUC3D's cross-view tracker (pose/cross-view-tracker.js in
LUC3D, itself a port of sleap-3d's CrossViewTracker) with LUC3D's Track All
settings, so the live identities and 3D are what LUC3D would make offline:

- Each animal is a target with a 3D pose and, per camera, the detection it
  last matched there. Each set, camera by camera, a Hungarian assignment
  matches the targets to that camera's detections. The score of a pair adds,
  per node, a 3D term (how close the target's 3D point is to the ray of the
  detection's node; weight corr3d, normalized by distance_threshold mm) and a
  2D term (how close the target's 3D projects to the detection; weight
  corr2d). Every camera is scored against the targets as they stood at the
  start of the set; each target's 3D is solved again once, at the end.
- A camera's detection older than `stale` frames is dropped from its target
  before the set is matched, so a target is never held in place by a view it
  has not had for a while (LUC3D's stale-anchor fix). The one setting changed
  from LUC3D's: 60 frames instead of 20. With 3 cameras an animal is often
  seen by one camera only for a while, and its target, left with that view,
  keeps its old 3D; on recorded sessions (bench/pose3d_validate.py) 60 gave
  fewer identity switches against LUC3D's own 8-camera tracking on 3 of 12
  camera triples and more on none.
- Detections left over in two cameras start new targets, paired by epipolar
  distance, until there are n_animals targets. Targets never end, so
  identities are the numbers 0 .. n_animals-1 for the whole recording: an
  animal that is lost (unseen) keeps its number and is matched again when it
  comes back.

Then each animal seen in at least 2 cameras this set is triangulated from this
set's detections only (DLT on undistorted points, in LUC3D's camera-centred
frame, optionally weighted by node score); with reproj_threshold, a node whose
reprojection error is over it loses its worst view (down to 2) or, with 2 views,
is dropped.

Interface
---------
    from campy import pose3d
    cameras = pose3d.LoadCalibration("calibration.toml",
        names=["Camera0_mid", "Camera1_topB", "Camera2_topC"])
    tracker = pose3d.Tracker(cameras, n_animals=5)
    for each set:
        poses = tracker.Update(keypoints, scores, frame=n)

keypoints  (cameras, instances, nodes, 2) pixels in the camera's own
           (distorted) image, as the model gives them; NaN for a missing node,
           and an instance with no node at all is no detection (so padding
           rows of NaN are fine). Cameras in the order given to Tracker.
scores     (cameras, instances, nodes) node confidences, or None
frame      the set's frame number (default: the previous one + 1). Staleness
           is counted in frames, so pass it when sets can be skipped.

poses.points3d  (n_animals, nodes, 3) world units of the calibration (mm),
                row = identity; NaN where not triangulated this set
poses.members   (n_animals, cameras) the instance used in each camera, -1 none
poses.n_views   (n_animals,) cameras that saw the animal this set
poses.reproj_px (n_animals, nodes) mean reprojection error, undistorted pixels
poses.frame     the set's frame number

An animal seen in fewer than 2 cameras this set has no 3D (its row is NaN),
but keeps its identity. The Tracker holds per-camera state from set to set,
so give it every set in order (skipping some is fine; say so with frame=).

Lower level: Camera (Normalize: pixels -> undistorted normalized coordinates,
Project: 3D -> pixels), Triangulate (DLT over views), ReprojectionErrors.
"""

import ast
import math
import re
import warnings

import numpy as np
from scipy.optimize import linear_sum_assignment


# ---------------------------------------------------------------- cameras

def Rodrigues(r):
	# Rotation matrix from a rotation vector (or a 3x3 matrix, returned as is)
	r = np.asarray(r, float)
	if r.shape == (3, 3):
		return r
	theta = np.linalg.norm(r)
	if theta < 1e-12:
		return np.eye(3)
	k = r / theta
	K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
	return np.eye(3) + math.sin(theta) * K + (1 - math.cos(theta)) * (K @ K)


class Camera:
	"""One calibrated camera: OpenCV pinhole + distortion (k1, k2, p1, p2, k3), world -> camera [R|t]."""

	def __init__(self, name, matrix, dist, rotation, translation, size=None):
		self.name = name
		self.K = np.asarray(matrix, float)
		d = np.zeros(5)
		dist = np.ravel(np.asarray(dist, float))[:5]
		d[:len(dist)] = dist
		self.dist = d
		self.R = Rodrigues(rotation)
		self.t = np.ravel(np.asarray(translation, float))
		self.size = size
		self.Rt = np.hstack([self.R, self.t[:, None]])   # projects to normalized coordinates
		self.center = -self.R.T @ self.t
		self.f = np.array([self.K[0, 0], self.K[1, 1]])
		self.c = np.array([self.K[0, 2], self.K[1, 2]])

	def Normalize(self, xy, iterations=10):
		"""Image pixels (..., 2) -> undistorted normalized coordinates (..., 2), as cv2.undistortPoints.

		The same fixed-point iteration as OpenCV and LUC3D (Camera.undistortPoint)."""
		return _Normalize(np.asarray(xy, float), self.f, self.c, self.dist, iterations)

	def Distort(self, n):
		"""Undistorted normalized coordinates (..., 2) -> image pixels (..., 2)."""
		n = np.asarray(n, float)
		x, y = n[..., 0], n[..., 1]
		k1, k2, p1, p2, k3 = self.dist
		r2 = x * x + y * y
		radial = 1 + r2 * (k1 + r2 * (k2 + r2 * k3))
		xd = x * radial + 2 * p1 * x * y + p2 * (r2 + 2 * x * x)
		yd = y * radial + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y
		return np.stack([xd * self.f[0] + self.c[0], yd * self.f[1] + self.c[1]], -1)

	def ProjectNormalized(self, X):
		"""World points (..., 3) -> normalized coordinates (..., 2)."""
		p = np.asarray(X, float) @ self.R.T + self.t
		return p[..., :2] / p[..., 2:3]

	def Project(self, X):
		"""World points (..., 3) -> image pixels (..., 2), with the lens distortion."""
		return self.Distort(self.ProjectNormalized(X))


def _Normalize(xy, f, c, dist, iterations=10, check_px=0.0):
	# Pixels -> undistorted normalized coordinates. f, c (..., 2) and dist (..., 5)
	# broadcast against xy[..., 0], so one call can do every camera of a set.
	# check_px > 0: points that do not distort back to within check_px of where they
	# were are NaN. With a strong barrel distortion (k1 around -0.35) the lens model
	# folds back near the image corners, where a pixel has no undistorted point.
	x0 = (xy[..., 0] - c[..., 0]) / f[..., 0]
	y0 = (xy[..., 1] - c[..., 1]) / f[..., 1]
	k1, k2, p1, p2, k3 = (dist[..., i] for i in range(5))
	x, y = x0, y0
	if np.any(dist):
		for _ in range(iterations):
			r2 = x * x + y * y
			radial = 1 + r2 * (k1 + r2 * (k2 + r2 * k3))
			x, y = ((x0 - 2 * p1 * x * y - p2 * (r2 + 2 * x * x)) / radial,
				(y0 - p1 * (r2 + 2 * y * y) - 2 * p2 * x * y) / radial)
		if check_px > 0:
			r2 = x * x + y * y
			radial = 1 + r2 * (k1 + r2 * (k2 + r2 * k3))
			ex = (x * radial + 2 * p1 * x * y + p2 * (r2 + 2 * x * x) - x0) * f[..., 0]
			ey = (y * radial + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y - y0) * f[..., 1]
			with np.errstate(invalid="ignore"):
				off = ex * ex + ey * ey > check_px * check_px
			x = np.where(off, np.nan, x)
			y = np.where(off, np.nan, y)
	return np.stack([x, y], -1)


def _ReadToml(path):
	# calibration.toml as written by anipose / sleap-anipose. tomllib is Python 3.11+;
	# before that, a reader for that file's subset of TOML: [sections] and key = value
	# lines whose values are numbers, strings, booleans and (nested) arrays.
	try:
		import tomllib
		with open(path, "rb") as f:
			return tomllib.load(f)
	except ImportError:
		pass
	out, section = {}, None
	with open(path, encoding="utf-8") as f:
		for line in f:
			line = line.strip()
			if not line or line.startswith("#"):
				continue
			m = re.fullmatch(r"\[([^\]]+)\]", line)
			if m:
				section = out.setdefault(m.group(1).strip(), {})
				continue
			key, _, value = line.partition("=")
			value = re.sub(r"\btrue\b", "True", re.sub(r"\bfalse\b", "False", value.strip()))
			(section if section is not None else out)[key.strip().strip('"')] = ast.literal_eval(value)
	return out


def LoadCalibration(path, names=None):
	"""Cameras of an anipose calibration.toml, in file order or in the order of `names`."""
	toml = _ReadToml(path)
	cams = {}
	for key in sorted((k for k in toml if k.startswith("cam_")), key=lambda k: int(k[4:]) if k[4:].isdigit() else k):
		c = toml[key]
		cams[c.get("name", key)] = Camera(c.get("name", key), c["matrix"], c.get("distortions", [0] * 5),
			c["rotation"], c["translation"], c.get("size"))
	if names is None:
		return list(cams.values())
	missing = [n for n in names if n not in cams]
	if missing:
		raise KeyError("not in {}: {} (it has {})".format(path, ", ".join(missing), ", ".join(cams)))
	return [cams[n] for n in names]


# ---------------------------------------------------------------- triangulation

class _Frame:
	# LUC3D's DLT frame (triangulation-core.js dltNormalizingFrame): centred on the
	# cameras' centroid, scaled by their mean distance from it. Solving there keeps
	# the algebraic DLT well conditioned and independent of where the world origin is.
	def __init__(self, cameras):
		centres = np.array([c.center for c in cameras])
		self.c = centres.mean(0)
		s = np.linalg.norm(centres - self.c, axis=1).mean()
		self.s = s if np.isfinite(s) and s > 1e-9 else 1.0
		# P' = P diag(s, s, s, 1) with the translation folded in: P'[X'; 1] = P[sX' + c; 1]
		self.P = np.array([np.hstack([c.Rt[:, :3] * self.s, (c.Rt[:, :3] @ self.c + c.Rt[:, 3])[:, None]])
			for c in cameras])


def Triangulate(xy, cameras, weights=None, frame=None):
	"""DLT of each point from its views.

	xy       (views, ..., 2) undistorted normalized coordinates (Camera.Normalize), NaN = not seen
	cameras  the views' Cameras
	weights  (views, ...) or None: each view's rows are scaled by its weight (0 = not used)
	Returns (..., 3) world points, NaN where fewer than 2 views."""
	xy = np.asarray(xy, float)
	V = xy.shape[0]
	shape = xy.shape[1:-1]
	xy = xy.reshape(V, -1, 2)
	w = np.ones(xy.shape[:2]) if weights is None else np.asarray(weights, float).reshape(V, -1).copy()
	ok = np.isfinite(xy).all(-1) & (w > 0)
	w[~ok] = 0.0
	x = np.where(ok, xy[..., 0], 0.0)
	y = np.where(ok, xy[..., 1], 0.0)
	fr = frame if frame is not None else _Frame(cameras)
	P = fr.P                                                       # (V, 3, 4)
	# Rows a = x P2 - P0 and b = y P2 - P1, per view and point: (V, M, 4)
	a = (x[..., None] * P[:, None, 2] - P[:, None, 0]) * w[..., None]
	b = (y[..., None] * P[:, None, 2] - P[:, None, 1]) * w[..., None]
	M = np.einsum("vmi,vmj->mij", a, a) + np.einsum("vmi,vmj->mij", b, b)
	_, vec = np.linalg.eigh(M)
	h = vec[..., 0]                                                # smallest eigenvalue's vector
	with np.errstate(divide="ignore", invalid="ignore"):
		X = h[:, :3] / h[:, 3:4] * fr.s + fr.c
	bad = (ok.sum(0) < 2) | (np.abs(h[:, 3]) < 1e-10)
	X[bad] = np.nan
	return X.reshape(shape + (3,))


def ReprojectionErrors(X, xy, cameras):
	"""Per view, distance (undistorted pixels) between X (..., 3) projected and xy (views, ..., 2) normalized."""
	out = np.empty(np.shape(xy)[:-1])
	for v, cam in enumerate(cameras):
		d = (cam.ProjectNormalized(X) - xy[v]) * cam.f
		out[v] = np.sqrt((d * d).sum(-1))
	return out


def TriangulateChecked(xy, cameras, weights=None, reproj_threshold=None, frame=None, X=None):
	"""Triangulate, then drop views whose reprojection error is over reproj_threshold px.

	While a node is over the threshold and has more than 2 views, it loses the view
	without which its other views fit best; a 2-view node still over it is dropped (NaN). X: Triangulate's result,
	if already known.
	Returns (X (..., 3), reprojection errors (views, ...; NaN = view not used), views used (...))."""
	xy = np.asarray(xy, float)
	w = np.ones(xy.shape[:-1]) if weights is None else np.array(weights, float)
	w[~np.isfinite(xy).all(-1)] = 0.0
	X = Triangulate(xy, cameras, w, frame) if X is None else np.array(X, float)
	err = ReprojectionErrors(X, xy, cameras)
	err[w <= 0] = np.nan
	if reproj_threshold:
		V = len(cameras)
		for _ in range(V - 2):
			e = np.where(np.isfinite(err), err, -1.0)
			over = (e.max(0) > reproj_threshold) & ((w > 0).sum(0) > 2)
			if not over.any():
				break
			# Leave one view out at a time and keep the solve that fits its other views
			# best: the DLT spreads one bad view's error over the others, so the view
			# with the largest error is not always the bad one.
			idx = np.nonzero(over)
			sub = (slice(None),) + idx
			wx, xx = w[sub], xy[sub]                                         # (V, k), (V, k, 2)
			trial = np.repeat(wx[None], V, 0)                                # (V left out, V, k)
			trial[np.arange(V), np.arange(V)] = 0.0
			Xt = Triangulate(np.broadcast_to(xx, (V,) + xx.shape).transpose(1, 0, 2, 3), cameras,
				trial.transpose(1, 0, 2), frame)                             # (V left out, k, 3)
			et = ReprojectionErrors(Xt, np.broadcast_to(xx[:, None], (V, V) + xx.shape[1:]), cameras)
			et = np.where(trial.transpose(1, 0, 2) > 0, et, -1.0).max(0)    # (V left out, k)
			et[wx <= 0] = np.inf                                             # only views it has
			best = et.argmin(0)
			k = np.arange(len(best))
			w[(best,) + idx] = 0.0
			X[idx] = Xt[best, k]
			e = ReprojectionErrors(X[idx], xx, cameras)
			e[w[sub] <= 0] = np.nan
			err[sub] = e
		bad = np.where(np.isfinite(err), err, -1.0).max(0) > reproj_threshold
		X[bad] = np.nan
		err[:, bad] = np.nan
		w[:, bad] = 0.0
	return X, err, (w > 0).sum(0)


# ---------------------------------------------------------------- tracking

class Poses:
	"""One set's result: see the module docstring."""

	def __init__(self, frame, points3d, members, n_views, reproj_px):
		self.frame = frame
		self.points3d = points3d
		self.members = members
		self.n_views = n_views
		self.reproj_px = reproj_px

	def __repr__(self):
		return "Poses(frame={}, animals with 3D: {})".format(self.frame, int((self.n_views >= 2).sum()))


class Tracker:
	"""LUC3D's cross-view tracker, one synchronized set at a time (see the module docstring).

	cameras             Cameras (LoadCalibration), in the order of the keypoints' first axis
	n_animals           how many animals: targets are never more, and identities are 0 .. n_animals-1
	corr2d, corr3d      weights of the 2D and 3D terms of the matching score (LUC3D: 1, 6)
	velocity_threshold  normalizer of the 2D term, normalized image units (LUC3D: 10)
	distance_threshold  normalizer of the 3D term, world units (mm) (LUC3D: 25)
	time_penalty        2D term decay per frame since the target was seen (LUC3D: 0.1)
	stale               frames after which a camera's detection is dropped from its target (LUC3D: 20;
	                    60 here: see the module docstring)
	node_weights        per node weight in the matching score, or None (all 1); 0 leaves a node out
	min_nodes           a detection needs this many nodes to be matched (LUC3D: 0, any)
	min_score           nodes scored below this are treated as missing (0: none)
	weighted            weight each view's DLT rows by its node score (scores needed)
	reproj_threshold    px: a node over it loses its worst view (down to 2), then is dropped (0: off)
	birth_threshold     px: new targets only from detection pairs within this mean epipolar distance (0: any, as LUC3D)
	"""

	def __init__(self, cameras, n_animals, corr2d=1.0, corr3d=6.0, velocity_threshold=10.0,
			distance_threshold=25.0, time_penalty=0.1, stale=60, node_weights=None, min_nodes=0,
			min_score=0.0, weighted=False, reproj_threshold=0.0, birth_threshold=0.0):
		self.cameras = list(cameras)
		self.n_animals = int(n_animals)
		self.corr2d, self.corr3d = float(corr2d), float(corr3d)
		self.vel, self.dist_thr = float(velocity_threshold), float(distance_threshold)
		self.time_penalty, self.stale = float(time_penalty), int(stale)
		self.node_weights = None if node_weights is None else np.asarray(node_weights, float)
		self.min_nodes, self.min_score = int(min_nodes), float(min_score)
		self.weighted = bool(weighted)
		self.reproj_threshold = float(reproj_threshold)
		self.birth_threshold = float(birth_threshold)
		self.frame_of_cameras = _Frame(self.cameras)
		self._f = np.array([c.f for c in self.cameras])[:, None, None]       # (C, 1, 1, 2)
		self._c = np.array([c.c for c in self.cameras])[:, None, None]
		self._dist = np.array([c.dist for c in self.cameras])[:, None, None]
		# Essential matrices between camera pairs, for births: E[a][b] maps a's point to b's line
		self.E = {}
		for a, ca in enumerate(self.cameras):
			for b, cb in enumerate(self.cameras):
				if a != b:
					Rr = cb.R @ ca.R.T
					tr = cb.t - Rr @ ca.t
					tx = np.array([[0, -tr[2], tr[1]], [tr[2], 0, -tr[0]], [-tr[1], tr[0], 0]])
					self.E[a, b] = tx @ Rr
		self.Reset()

	def Reset(self):
		"""Forget every target, as at the start of a recording."""
		A, C = self.n_animals, len(self.cameras)
		self.n_targets = 0
		self.nodes = None
		self.det_xy = None                          # (A, C, nodes, 2) normalized, each target's detection per camera
		self.det_w = None                           # (A, C, nodes) its DLT weights
		self.det_frame = np.full((A, C), np.nan)    # when it was matched
		self.det_slot = np.full((A, C), -1)
		self.X = None                               # (A, nodes, 3) each target's 3D, as matched against
		self.last_frame = None

	# -- one set

	def Update(self, keypoints, scores=None, frame=None):
		keypoints = np.asarray(keypoints, float)
		C, I, N = keypoints.shape[:3]
		if C != len(self.cameras):
			raise ValueError("keypoints are for {} cameras, the tracker has {}".format(C, len(self.cameras)))
		if self.nodes is None:
			self.nodes = N
			A = self.n_animals
			self.det_xy = np.full((A, C, N, 2), np.nan)
			self.det_w = np.zeros((A, C, N))
			self.X = np.full((A, N, 3), np.nan)
		elif N != self.nodes:
			raise ValueError("keypoints have {} nodes, earlier sets had {}".format(N, self.nodes))
		f = (self.last_frame + 1 if self.last_frame is not None else 0) if frame is None else frame
		self.last_frame = f

		# Every detection, undistorted and normalized
		xy = _Normalize(keypoints, self._f, self._c, self._dist, check_px=1.0)
		w = np.ones((C, I, N)) if scores is None or not self.weighted else np.nan_to_num(np.asarray(scores, float))
		if scores is not None and self.min_score > 0:
			xy[np.asarray(scores) < self.min_score] = np.nan
		seen = np.isfinite(xy).all(-1)
		w = np.where(seen, w, 0.0)
		present = seen.sum(-1) >= max(1, self.min_nodes)                 # (C, I)

		# Start of the set: drop stale views; note each target's mean frame and its 3D,
		# which every camera of this set is scored against
		T = self.n_targets
		if self.stale > 0 and T:
			old = self.det_frame[:T] < f - self.stale
			self.det_frame[:T][old] = np.nan
			self.det_slot[:T][old] = -1
			self.det_xy[:T][old] = np.nan
			self.det_w[:T][old] = 0.0
		with np.errstate(invalid="ignore"):
			has = np.isfinite(self.det_frame[:T])
			mean_frame = np.where(has.any(1), np.nansum(self.det_frame[:T], 1) / np.maximum(has.sum(1), 1), 0.0)
		X0 = self.X[:T].copy()
		touched = np.zeros(self.n_animals, bool)
		leftovers = {}

		for c in range(C):
			dets = np.nonzero(present[c])[0]
			taken = np.zeros(len(dets), bool)
			if T and len(dets):
				score = self._Score(c, X0, mean_frame, f, xy[c, dets])
				rows, cols = linear_sum_assignment(score, maximize=True)
				for t, j in zip(rows, cols):
					self._Take(t, c, dets[j], xy[c, dets[j]], w[c, dets[j]], f)
					touched[t] = True
					taken[j] = True
			leftovers[c] = list(dets[~taken])
			born = self._Births(leftovers, xy, w, f)
			if born:
				# New targets are matched against by the cameras after this one, this same set
				X0 = np.concatenate([X0, self.X[T:T + born]])
				mean_frame = np.concatenate([mean_frame, np.full(born, float(f))])
				T += born

		# End of the set. This set's 3D comes from this set's detections only; each
		# matched target's own 3D (what the next set is matched against) from every view
		# it holds, as LUC3D. Mostly they are the same views, solved once.
		A = self.n_animals
		fresh = self.det_frame == f                                      # (A, C)
		held = np.isfinite(self.det_frame)
		members = np.where(fresh, self.det_slot, -1)
		n_views = fresh.sum(1)
		out = np.nonzero(n_views >= 2)[0]
		same = (n_views >= 2) & (fresh == held).all(1) & (not self.weighted)
		# A target holding a single view keeps the 3D it had (LUC3D's _retriangulate)
		extra = np.nonzero(touched & ~same & (held.sum(1) >= 2))[0]
		vxy = np.concatenate([np.where(fresh[out][..., None, None], self.det_xy[out], np.nan),
			self.det_xy[extra]]).transpose(1, 0, 2, 3)                   # (C, k, N, 2)
		vw = None
		if self.weighted:
			vw = np.concatenate([np.where(fresh[out][..., None], self.det_w[out], 0.0),
				held[extra][..., None] * np.ones(N)]).transpose(1, 0, 2)
		points3d = np.full((A, N, 3), np.nan)
		reproj = np.full((A, N), np.nan)
		if vxy.shape[1]:
			Xall = Triangulate(vxy, self.cameras, vw, self.frame_of_cameras)
			k = len(out)
			reuse = touched[out] & same[out]
			self.X[out[reuse]] = Xall[:k][reuse]
			self.X[extra] = Xall[k:]
			if k:
				Xo, err, _ = TriangulateChecked(vxy[:, :k], self.cameras, None if vw is None else vw[:, :k],
					self.reproj_threshold, self.frame_of_cameras, Xall[:k])
				points3d[out] = Xo
				with np.errstate(invalid="ignore"), warnings.catch_warnings():
					warnings.simplefilter("ignore", RuntimeWarning)
					reproj[out] = np.nanmean(err, 0)
		return Poses(f, points3d, members, n_views, reproj)

	# -- pieces

	def _Take(self, t, c, slot, xy, w, f):
		self.det_xy[t, c] = xy
		self.det_w[t, c] = w
		self.det_frame[t, c] = f
		self.det_slot[t, c] = slot

	def _Score(self, c, X, mean_frame, f, dxy):
		# LUC3D's adjacency (higher = better match): targets (T) x detections (D) of camera c
		cam = self.cameras[c]
		nw = np.ones(dxy.shape[1]) if self.node_weights is None else self.node_weights
		with np.errstate(invalid="ignore", divide="ignore"):
			# 2D: the target's 3D projected into this camera vs the detection, normalized units
			proj = cam.ProjectNormalized(X)                                  # (T, N, 2)
			d2 = np.sqrt(((proj[:, None] - dxy[None]) ** 2).sum(-1))         # (T, D, N)
			dt = (f - mean_frame)[:, None, None]
			s2 = self.corr2d * (1 - d2 / (self.vel * (1 + dt))) * np.exp(-self.time_penalty * dt)
			# 3D: distance from the target's 3D to the detection's rays (from the camera centre)
			rays = np.concatenate([dxy, np.ones(dxy.shape[:2] + (1,))], -1) @ cam.R    # R^T [x y 1]
			rays /= np.linalg.norm(rays, axis=-1, keepdims=True)
			v = (X - cam.center)[:, None]                                    # (T, 1, N, 3)
			r = rays[None]                                                   # (1, D, N, 3)
			cx = v[..., 1] * r[..., 2] - v[..., 2] * r[..., 1]               # |v x r|: (T, D, N)
			cy = v[..., 2] * r[..., 0] - v[..., 0] * r[..., 2]
			cz = v[..., 0] * r[..., 1] - v[..., 1] * r[..., 0]
			d3 = np.sqrt(cx * cx + cy * cy + cz * cz)
			s3 = self.corr3d * (1 - d3 / self.dist_thr)
		s = np.where(np.isfinite(s2), s2, 0.0) + np.where(np.isfinite(s3), s3, 0.0)
		return (s * nw).sum(-1)

	def _Births(self, leftovers, xy, w, f):
		# LUC3D's births: the last two cameras (so far this set) with leftover detections,
		# paired by mean epipolar distance (Hungarian); each pair is a new target.
		if self.n_targets >= self.n_animals:
			return 0
		views = [c for c in leftovers if leftovers[c]]
		if len(views) < 2:
			return 0
		a, b = views[-2], views[-1]
		La, Lb = leftovers[a], leftovers[b]
		cost = self._Epipolar(a, b, xy[a, La], xy[b, Lb])
		rows, cols = linear_sum_assignment(np.where(np.isfinite(cost), cost, 1e15))
		born, usedA, usedB = 0, set(), set()
		for i, j in zip(rows, cols):
			if self.n_targets >= self.n_animals:
				break
			if self.birth_threshold > 0 and not cost[i, j] <= self.birth_threshold:
				continue
			t = self.n_targets
			self._Take(t, a, La[i], xy[a, La[i]], w[a, La[i]], f)
			self._Take(t, b, Lb[j], xy[b, Lb[j]], w[b, Lb[j]], f)
			self.X[t] = Triangulate(self.det_xy[t], self.cameras, None, self.frame_of_cameras)
			self.n_targets += 1
			born += 1
			usedA.add(i)
			usedB.add(j)
		leftovers[a] = [d for i, d in enumerate(La) if i not in usedA]
		leftovers[b] = [d for j, d in enumerate(Lb) if j not in usedB]
		return born

	def _Epipolar(self, a, b, pa, pb):
		# Mean distance (px in camera b) of b's nodes to the epipolar lines of a's: (len(pa), len(pb))
		E = self.E[a, b]
		ha = np.concatenate([pa, np.ones(pa.shape[:-1] + (1,))], -1)        # (Da, N, 3)
		lines = ha @ E.T                                                      # (Da, N, 3)
		hb = np.concatenate([pb, np.ones(pb.shape[:-1] + (1,))], -1)        # (Db, N, 3)
		with np.errstate(invalid="ignore", divide="ignore"):
			num = np.abs((lines[:, None] * hb[None]).sum(-1))                # (Da, Db, N)
			d = num / np.linalg.norm(lines[..., :2], axis=-1)[:, None] * self.cameras[b].f.mean()
			ok = np.isfinite(d)
			return np.where(ok.any(-1), np.where(ok, d, 0).sum(-1) / np.maximum(ok.sum(-1), 1), np.inf)
