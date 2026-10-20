"""campy/pose3d.py on made-up cameras and animals whose true 3D is known."""

import builtins
import time

import numpy as np
import pytest

from campy import pose3d


SIZE = (1280, 1024)
K = [[820.0, 0.0, 639.5], [0.0, 820.0, 511.5], [0.0, 0.0, 1.0]]
DIST = [-0.33, 0.0, 0.0, 0.0, 0.0]      # strong barrel distortion, like the lab's lenses


def look_at(name, eye, target=(0, 0, 0), up=(0, 0, 1), dist=DIST):
	# A camera at eye looking at target (world: mm, z up)
	eye, target, up = (np.asarray(v, float) for v in (eye, target, up))
	fwd = target - eye
	fwd /= np.linalg.norm(fwd)
	right = np.cross(fwd, up)
	if np.linalg.norm(right) < 1e-6:
		right = np.cross(fwd, [0, 1, 0])
	right /= np.linalg.norm(right)
	down = np.cross(fwd, right)
	R = np.array([right, down, fwd])
	return pose3d.Camera(name, K, dist, R, -R @ eye, SIZE)


def rig():
	# The new rig's kind: 3 cameras, one above and two at an angle
	return [look_at("top", (0, 30, 700), up=(0, 1, 0)),
		look_at("left", (-550, -300, 450)),
		look_at("right", (550, -300, 450))]


SKELETON = np.array([[0, 45, 15], [-12, 32, 22], [12, 32, 22], [0, -45, 10], [0, -110, 5],
	[0, 30, 18], [0, 0, 20], [0, -55, 8], [0, -75, 6], [0, -95, 5], [-15, 15, 12], [15, 15, 12],
	[-15, -30, 12], [15, -30, 12], [0, 18, 20]], float)     # 15 nodes, a mouse, mm


def animal(center, heading):
	c, s = np.cos(heading), np.sin(heading)
	Rz = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
	return SKELETON @ Rz.T + center


def scene(frames, n=5, seed=0):
	# n animals walking slow circles in a 500 mm arena, kept apart: (frames, n, 15, 3)
	rng = np.random.default_rng(seed)
	phase = rng.uniform(0, 2 * np.pi, n)
	radius = np.linspace(60, 210, n)
	speed = rng.uniform(0.004, 0.01, n) * rng.choice([-1, 1], n)
	out = np.empty((frames, n, len(SKELETON), 3))
	for f in range(frames):
		a = phase + speed * f
		for i in range(n):
			center = np.array([radius[i] * np.cos(a[i]), radius[i] * np.sin(a[i]), 0.0])
			out[f, i] = animal(center, a[i] + np.sign(speed[i]) * np.pi / 2)
	return out


def observe(cams, X, rng, noise=0.5, drop_node=0.1, drop_animal=0.0, instances=7):
	# What a top-down model would give: per camera, the animals in random order, pixels
	# with noise, some nodes missing, maybe an animal missed, rows padded with NaN.
	# Returns keypoints (C, instances, nodes, 2), scores, and order[c][slot] = true animal.
	n, N = X.shape[:2]
	kp = np.full((len(cams), instances, N, 2), np.nan)
	sc = np.full((len(cams), instances, N), np.nan)
	order = []
	for c, cam in enumerate(cams):
		seen = [i for i in rng.permutation(n) if rng.random() >= drop_animal]
		order.append(seen)
		for slot, i in enumerate(seen):
			px = cam.Project(X[i]) + rng.normal(0, noise, (N, 2))
			px[rng.random(N) < drop_node] = np.nan
			kp[c, slot] = px
			sc[c, slot] = rng.uniform(0.5, 1.0, N)
	return kp, sc, order


# ---------------------------------------------------------------- cameras

def test_normalize_inverts_distort():
	cam = rig()[0]
	rng = np.random.default_rng(1)
	# Within the radius real detections reach (distorted radius 0.63 on the lab's rigs)
	a, r = rng.uniform(0, 2 * np.pi, 500), np.sqrt(rng.uniform(0, 0.55 ** 2, 500))
	n = np.stack([r * np.cos(a), r * np.sin(a)], -1)
	assert np.allclose(cam.Normalize(cam.Distort(n)), n, atol=1e-9)


def test_project_matches_normalized_projection():
	cam = rig()[1]
	X = scene(1)[0].reshape(-1, 3)
	assert np.allclose(cam.Normalize(cam.Project(X)), cam.ProjectNormalized(X), atol=1e-9)


def test_corner_pixels_beyond_the_lens_model_are_dropped():
	# With k1 = -0.33 the distortion folds back before the image corner
	cam = rig()[0]
	f, c, d = cam.f, cam.c, cam.dist
	out = pose3d._Normalize(np.array([[0.0, 0.0], [639.5, 511.5]]), f, c, d, check_px=1.0)
	assert np.isnan(out[0]).all() and np.allclose(out[1], 0)


def test_rodrigues():
	r = np.array([0.3, -0.5, 0.8])
	R = pose3d.Rodrigues(r)
	assert np.allclose(R @ R.T, np.eye(3)) and np.isclose(np.linalg.det(R), 1)
	assert np.allclose(R @ r, r)                         # the axis stays
	assert np.allclose(pose3d.Rodrigues(np.zeros(3)), np.eye(3))
	assert pose3d.Rodrigues(R) is not None and np.allclose(pose3d.Rodrigues(R), R)


CALIBRATION = """\
[cam_0]
name = "Camera0_mid"
size = [ 1280, 1024,]
matrix = [ [ 824.4, 0.0, 639.5,], [ 0.0, 824.4, 511.5,], [ 0.0, 0.0, 1.0,],]
distortions = [ -0.3478, 0.0, 0.0, 0.0, 0.0,]
rotation = [ -0.0200, 0.0192, 0.0143,]
translation = [ -14.86, -10.93, -246.58,]

[cam_1]
name = "Camera1_topB"
size = [ 1280, 1024,]
matrix = [ [ 824.3, 0.0, 639.5,], [ 0.0, 824.3, 511.5,], [ 0.0, 0.0, 1.0,],]
distortions = [ -0.3547, 0.0, 0.0, 0.0, 0.0,]
rotation = [ 1.1652, 0.0206, 0.0523,]
translation = [ -50.47, 718.18, 155.87,]

[metadata]
adjusted = false
error = 0.42
"""


@pytest.mark.parametrize("tomllib", [True, False])
def test_load_calibration(tmp_path, monkeypatch, tomllib):
	path = tmp_path / "calibration.toml"
	path.write_text(CALIBRATION)
	if not tomllib:
		# Python 3.9 / 3.10, which have no tomllib
		real = builtins.__import__
		def no_tomllib(name, *a, **k):
			if name == "tomllib":
				raise ImportError(name)
			return real(name, *a, **k)
		monkeypatch.setattr(builtins, "__import__", no_tomllib)
	cams = pose3d.LoadCalibration(path)
	assert [c.name for c in cams] == ["Camera0_mid", "Camera1_topB"]
	assert np.isclose(cams[1].K[0, 0], 824.3) and np.isclose(cams[1].dist[0], -0.3547)
	assert np.allclose(cams[1].R, pose3d.Rodrigues([1.1652, 0.0206, 0.0523]))
	assert np.allclose(cams[1].t, [-50.47, 718.18, 155.87])
	assert [c.name for c in pose3d.LoadCalibration(path, ["Camera1_topB", "Camera0_mid"])] == ["Camera1_topB", "Camera0_mid"]
	with pytest.raises(KeyError):
		pose3d.LoadCalibration(path, ["Camera2_topC"])


# ---------------------------------------------------------------- triangulation

def normalized_views(cams, X):
	return np.stack([cam.Normalize(cam.Project(X)) for cam in cams])


def test_triangulate_exact():
	cams = rig()
	X = scene(1)[0]                                          # (5, 15, 3)
	assert np.allclose(pose3d.Triangulate(normalized_views(cams, X), cams), X, atol=1e-6)


def test_triangulate_needs_two_views():
	cams = rig()
	X = scene(1)[0, 0]
	xy = normalized_views(cams, X)
	xy[1:, 3] = np.nan                                       # node 3 seen by one camera
	xy[0, 4] = np.nan                                        # node 4 by two
	out = pose3d.Triangulate(xy, cams)
	assert np.isnan(out[3]).all()
	assert np.allclose(out[4], X[4], atol=1e-6)
	w = np.ones(xy.shape[:2])
	w[2, 5] = 0                                              # weight 0 = not seen
	w[1, 5] = 0
	assert np.isnan(pose3d.Triangulate(xy, cams, w)[5]).all()


def test_triangulate_does_not_depend_on_the_world_origin():
	# LUC3D's camera-centred DLT frame: moving the world moves the answer rigidly
	cams = rig()
	rng = np.random.default_rng(2)
	X = scene(1)[0].reshape(-1, 3)
	xy = normalized_views(cams, X) + rng.normal(0, 2e-3, (3, len(X), 2))
	Q = pose3d.Rodrigues([0.4, -1.1, 0.7])
	o = np.array([3000.0, -1200.0, 800.0])
	moved = [pose3d.Camera(c.name, c.K, c.dist, c.R @ Q.T, c.t - c.R @ Q.T @ o) for c in cams]
	a = pose3d.Triangulate(xy, cams)
	b = pose3d.Triangulate(xy, moved)
	assert np.allclose(b, a @ Q.T + o, atol=1e-6)


def test_reprojection_check_drops_the_bad_view():
	cams = rig()
	X = scene(1)[0, 0]
	xy = normalized_views(cams, X)
	xy[2, 6] += 60 / 820                                     # node 6 in camera 2: 60 px off
	plain, err, used = pose3d.TriangulateChecked(xy, cams)
	assert np.linalg.norm(plain[6] - X[6]) > 3 and np.nanmax(err[:, 6]) > 20
	fixed, err, used = pose3d.TriangulateChecked(xy, cams, reproj_threshold=10)
	assert np.allclose(fixed, X, atol=1e-6)
	assert used[6] == 2 and np.isnan(err[2, 6]) and (used[np.arange(15) != 6] == 3).all()
	# With 2 views left a bad node is dropped: moved off the epipolar line (2 views
	# cannot see a move along it)
	xy[0, 7] = np.nan
	line = pose3d.Tracker(cams, 1).E[1, 2] @ np.append(xy[1, 7], 1)
	xy[2, 7] += 60 / 820 * line[:2] / np.linalg.norm(line[:2])
	fixed, err, used = pose3d.TriangulateChecked(xy, cams, reproj_threshold=10)
	assert np.isnan(fixed[7]).all() and used[7] == 0


# ---------------------------------------------------------------- tracking

def identities(order, members):
	# true animal -> the tracker's identity, from which instance each identity used
	out = {}
	for ident, row in enumerate(members):
		truth = {order[c][slot] for c, slot in enumerate(row) if slot >= 0}
		assert len(truth) <= 1, "identity {} mixes animals {}".format(ident, truth)
		if truth:
			out[truth.pop()] = ident
	return out


def test_tracker_matches_and_keeps_identities():
	cams = rig()
	X = scene(300)
	rng = np.random.default_rng(3)
	tracker = pose3d.Tracker(cams, n_animals=5)
	first, errors = None, []
	for f in range(len(X)):
		kp, sc, order = observe(cams, X[f], rng, drop_animal=0.03)
		poses = tracker.Update(kp, sc, frame=f)
		assert poses.points3d.shape == (5, 15, 3) and poses.members.shape == (5, 3)
		ids = identities(order, poses.members)
		if f == 0:
			assert len(ids) == 5 and sorted(ids.values()) == list(range(5))
			first = ids
		for animal_, ident in ids.items():
			assert ident == first[animal_], "frame {}: animal {} was {}, now {}".format(f, animal_, first[animal_], ident)
			if poses.n_views[ident] >= 2:
				errors.append(np.linalg.norm(poses.points3d[ident] - X[f, animal_], axis=-1))
		seen_by = {i: sum(i in o for o in order) for i in range(5)}
		for animal_, n in seen_by.items():
			if n >= 2:
				assert poses.n_views[first[animal_]] == n
	errors = np.concatenate(errors)
	errors = errors[np.isfinite(errors)]
	assert np.median(errors) < 1.0 and np.percentile(errors, 99) < 5.0


def test_tracker_carries_identities_over_missed_and_skipped_sets():
	cams = rig()
	X = scene(160, n=4, seed=5)
	rng = np.random.default_rng(6)
	tracker = pose3d.Tracker(cams, n_animals=4)
	first = None
	for f in range(0, len(X)):
		if 40 <= f < 50:
			continue                                         # sets skipped: the frame numbers tell
		kp, sc, order = observe(cams, X[f], rng)
		if 80 <= f < 110:
			# animal 2 seen by one camera only (no 3D), then by none
			for c in range(3):
				if 2 in order[c] and (c > 0 or f >= 95):
					slot = order[c].index(2)
					kp[c, slot] = np.nan
					order[c][slot] = -1
		poses = tracker.Update(kp, sc, frame=f)
		ids = identities(order, poses.members)
		ids.pop(-1, None)
		if first is None:
			first = ids
		assert all(first[a] == i for a, i in ids.items()), f
		if 80 <= f < 110:
			assert np.isnan(poses.points3d[first[2]]).all() and poses.n_views[first[2]] < 2
		else:
			assert poses.n_views[first[2]] == 3
	assert len(first) == 4


def test_tracker_ignores_padding_and_extra_detections():
	cams = rig()
	X = scene(60, n=3, seed=7)
	rng = np.random.default_rng(8)
	tracker = pose3d.Tracker(cams, n_animals=3)
	first = None
	for f in range(len(X)):
		kp, sc, order = observe(cams, X[f], rng)
		# a false detection in camera 1, far from every animal
		slot = len(order[1])
		kp[1, slot] = cams[1].Project(SKELETON + [900, 900, 0])
		order[1].append(-1)
		poses = tracker.Update(kp, sc)
		assert poses.frame == f
		ids = identities(order, poses.members)
		assert -1 not in ids
		if first is None:
			first = ids
		assert ids == first and len(ids) == 3


def junk_nodes(kp, sc, order, rng, rate):
	# What a model trained on another rig does: some nodes put anywhere in the image,
	# at a low score
	for c in range(len(order)):
		for slot in range(len(order[c])):
			bad = rng.random(kp.shape[2]) < rate
			kp[c, slot, bad] = rng.uniform([0, 0], SIZE, (bad.sum(), 2))
			sc[c, slot, bad] = rng.uniform(0.1, 0.4, bad.sum())


@pytest.mark.parametrize("seed", [1, 2])
def test_tracker_is_not_thrown_by_misplaced_nodes(seed):
	# A misplaced node triangulates to a 3D point far from the animal. With LUC3D's
	# unbounded per-node penalty that one node outweighs all the others, so detections
	# are matched to the wrong animal; with each node's penalty capped (max_penalty) and
	# the animals' own 3D keeping only nodes whose views agree (match_reproj), they are not.
	def run(**options):
		cams = rig()
		X = scene(300, n=3, seed=seed)
		rng = np.random.default_rng(seed + 100)
		tracker = pose3d.Tracker(cams, n_animals=3, **options)
		mixed, switched, first = 0, 0, None
		for f in range(len(X)):
			kp, sc, order = observe(cams, X[f], rng)
			junk_nodes(kp, sc, order, rng, 0.15)
			poses = tracker.Update(kp, sc, frame=f)
			ids = {}
			for ident, row in enumerate(poses.members):
				truth = {order[c][slot] for c, slot in enumerate(row) if slot >= 0}
				if len(truth) > 1:
					mixed += 1
				elif truth:
					ids[truth.pop()] = ident
			first = first or ids
			switched += sum(first.get(a) != i for a, i in ids.items())
		return mixed, switched
	luc3d = run(max_penalty=None, match_reproj=0.0)
	assert luc3d[0] > 10, "LUC3D's scoring should mix animals here: the test lost its point"
	assert run() == (0, 0)


def test_tracker_input_checks():
	tracker = pose3d.Tracker(rig(), n_animals=2)
	with pytest.raises(ValueError):
		tracker.Update(np.zeros((2, 2, 15, 2)))
	tracker.Update(np.full((3, 2, 15, 2), np.nan))
	with pytest.raises(ValueError):
		tracker.Update(np.full((3, 2, 12, 2), np.nan))
	poses = tracker.Update(np.full((3, 2, 15, 2), np.nan))
	assert np.isnan(poses.points3d).all() and (poses.members == -1).all()


def test_tracker_is_fast_enough():
	# Generous: the live budget is ~2 ms per set for 5 animals on one core
	cams = rig()
	X = scene(200)
	rng = np.random.default_rng(9)
	sets = [observe(cams, X[f], rng)[:2] for f in range(len(X))]
	tracker = pose3d.Tracker(cams, n_animals=5)
	times = []
	for f, (kp, sc) in enumerate(sets):
		t0 = time.perf_counter()
		tracker.Update(kp, sc, frame=f)
		times.append(time.perf_counter() - t0)
	assert np.median(times[20:]) < 0.02
