"""
campy/cagegrid.py (campy-cage): the cage from the enclosure's own 1-inch grid,
on a made-up box whose floor and three walls carry the grid (the fourth a
clear pane), seen by made-up cameras through campy's own lens model, so the
true box is known.
"""

import json, math, os, shutil, subprocess

import numpy as np
import pytest

from campy import cagegrid, pose3d, videoread

import test_pose3d

INCH = 25.4
HALF = (6 * INCH, 4 * INCH)          # the floor: 12 x 8 inches, its middle at the world's origin (z up)
WALL = 6 * INCH                      # the walls' grid goes this high
SHAPE = (384, 480)                   # pictures, height x width
K = [[380.0, 0.0, 239.5], [0.0, 380.0, 191.5], [0.0, 0.0, 1.0]]
DIST = [-0.15, 0.0, 0.0, 0.0, 0.0]


def camera(name, eye, up=(0, 0, 1)):
	c = test_pose3d.look_at(name, eye, (0, 0, 0), up, DIST)
	return pose3d.Camera(name, K, DIST, c.R, c.t, (SHAPE[1], SHAPE[0]))


def rig():
	# Above, in front through the clear pane on both sides, and over the back and left walls
	return [camera("top", (0, -40, 600), up=(0, 1, 0)), camera("frontL", (-300, -450, 380)),
		camera("frontR", (300, -450, 380)), camera("back", (0, 260, 650)), camera("left", (-260, 30, 620))]


def render(cam, seed=0):
	"""
	What cam sees: the box's floor and its walls' inner faces, with 1-inch grid lines (bright, 2 mm wide) on a dark
	ground, the walls' up to their top (WALL); a wall's outer face plain; the front a clear pane (no grid, nothing
	drawn); outside the box, a random texture. Grey uint8.
	"""
	h, w = SHAPE
	v, u = np.mgrid[0:h, 0:w]
	n = cam.Normalize(np.stack([u, v], -1).astype(float))
	d = np.concatenate([n, np.ones(n.shape[:-1] + (1,))], -1) @ cam.R      # each pixel's ray, world
	o = cam.center
	best = np.full((h, w), np.inf)
	value = np.zeros((h, w))

	def lines(a, b):
		# 1 on a grid line (either way), fading over 2 mm
		da = np.abs((a + 50 * INCH) % INCH - INCH / 2) - INCH / 2
		db = np.abs((b + 50 * INCH) % INCH - INCH / 2) - INCH / 2
		return np.exp(-(np.minimum(np.abs(da), np.abs(db)) / 1.0) ** 2)
	X, Y = HALF
	# (axis, where, inside test, grid coordinates, gridded, the inner side's sign)
	planes = [(2, 0.0, lambda P: (abs(P[..., 0]) <= X) & (abs(P[..., 1]) <= Y), lambda P: (P[..., 0] + X, P[..., 1] + Y),
			True, 1),
		(0, -X, lambda P: (abs(P[..., 1]) <= Y) & (P[..., 2] >= 0) & (P[..., 2] <= WALL),
			lambda P: (P[..., 1] + Y, P[..., 2]), True, 1),
		(0, X, lambda P: (abs(P[..., 1]) <= Y) & (P[..., 2] >= 0) & (P[..., 2] <= WALL),
			lambda P: (P[..., 1] + Y, P[..., 2]), True, -1),
		(1, Y, lambda P: (abs(P[..., 0]) <= X) & (P[..., 2] >= 0) & (P[..., 2] <= WALL),
			lambda P: (P[..., 0] + X, P[..., 2]), True, -1)]
	for axis, where, inside, coords, gridded, sign in planes:
		with np.errstate(divide="ignore", invalid="ignore"):
			t = (where - o[axis]) / d[..., axis]
		P = o + t[..., None] * d
		hit = (t > 0) & inside(P) & (t < best)
		a, b = coords(P)
		innerFace = sign * (o[axis] - where) > 0                 # the camera on the box's side of it
		shade = 25 + 200 * lines(a, b) if innerFace else np.full(t.shape, 45.0)
		value = np.where(hit, shade, value)
		best = np.where(hit, t, best)
	rng = np.random.default_rng(seed)
	import cv2
	texture = cv2.GaussianBlur(rng.uniform(0, 120, (h, w)).astype(np.float32), (0, 0), 3) # outside the box
	value = np.where(np.isfinite(best), value, texture)
	return np.clip(value + rng.normal(0, 2, value.shape), 0, 255).astype(np.uint8)


def write_calibration(path, cams):
	# anipose's calibration.toml, each camera at its own size
	from scipy.spatial.transform import Rotation
	with open(path, "w") as f:
		for i, cam in enumerate(cams):
			f.write("[cam_{}]\nname = \"{}\"\nsize = {}\n".format(i, cam.name, list(cam.size)))
			f.write("matrix = {}\ndistortions = {}\n".format(cam.K.tolist(), cam.dist.tolist()))
			f.write("rotation = {}\ntranslation = {}\n\n".format(Rotation.from_matrix(cam.R).as_rotvec().tolist(),
				cam.t.tolist()))


TRUE_CORNERS = np.array([(-HALF[0], -HALF[1], 0), (HALF[0], -HALF[1], 0), (HALF[0], HALF[1], 0), (-HALF[0], HALF[1], 0)])


@pytest.fixture(scope="module")
def scene():
	cams = rig()
	images = [render(c, seed=i) for i, c in enumerate(cams)]
	maps = [cagegrid.RidgeMap(im) for im in images]
	fit = cagegrid.Fit(cams, None, size=(12, 8), maps=maps)
	return cams, images, maps, fit


def nearest(corners, X):
	return min(np.linalg.norm(np.asarray(corners) - X, axis=1))


# ---------------------------------------------------------------- the fit

def test_the_box_is_found_from_its_grid_with_no_animals_and_no_clicks(scene):
	cams, images, maps, fit = scene
	# The floor's plane and up
	assert np.degrees(np.arccos(np.clip(fit["up"] @ [0, 0, 1], -1, 1))) < 0.5
	assert abs(np.asarray(fit["grid"]["origin"]) @ [0, 0, 1]) < 1.0
	# Its corners, each within 2 mm of the true box's (in any order around it)
	for X in TRUE_CORNERS:
		assert nearest(fit["corners"], X) < 2.0
	# The side walls both have a grid: the long way's squares are counted; the clear front is placed from the size
	walls = fit["walls"]
	assert sorted(w["from"] for w in walls.values()) == ["grid", "grid", "grid", "nominal size"]
	counted = [ax for ax in (0, 1) if walls["{}_lo".format(ax)]["from"] == walls["{}_hi".format(ax)]["from"] == "grid"]
	assert len(counted) == 1
	assert fit["squares"][counted[0]] == pytest.approx(12.0, abs=0.08)
	assert fit["squares"][1 - counted[0]] == pytest.approx(8.0, abs=0.05)
	assert fit["pitch_mm"] == pytest.approx([INCH, INCH], abs=0.1)
	# How high the walls' grid goes: the cage's height
	assert WALL <= fit["wall_top"] <= WALL + 20     # its top line, smeared by the ridge filter, reads a little high
	# The grid lands on every camera's lines
	assert min(fit["ratios"]) > cagegrid.GRID_RATIO_OK


def test_without_the_size_a_wall_with_no_grid_is_where_the_floor_ends(scene):
	cams, images, maps, _ = scene
	fit = cagegrid.Fit(cams, None, size=None, maps=maps)
	assert sorted(w["from"] for w in fit["walls"].values()) == ["floor's end", "grid", "grid", "grid"]
	for X in TRUE_CORNERS:
		assert nearest(fit["corners"], X) < 20.0       # the clear side: where the floor's grid stops agreeing


def test_a_picture_with_no_grid_is_refused():
	cams = rig()
	rng = np.random.default_rng(1)
	import cv2
	images = [cv2.GaussianBlur(rng.uniform(0, 255, SHAPE).astype(np.float32), (0, 0), 2).astype(np.uint8) for _ in cams]
	with pytest.raises(cagegrid.FitError):
		cagegrid.Fit(cams, images, size=(12, 8))


# ---------------------------------------------------------------- camera identity

def test_each_picture_matches_its_own_calibration(scene):
	cams, images, maps, fit = scene
	identity = cagegrid.Identity(cams, images, fit["grid"], maps)
	assert identity["swaps"] == {} and identity["weak"] == []
	M = np.asarray(identity["ratio"])
	assert (np.diag(M) > 2).all()
	assert (M[~np.eye(len(cams), dtype=bool)] < np.diag(M).min()).all()
	assert "top" in cagegrid.IdentityText(identity)


def test_pictures_swapped_between_two_cameras_are_named(scene):
	# The cameras listed in another order (a restart, say): frontL's picture under frontR's name and the other way
	cams, images, maps, fit = scene
	swapped = list(images)
	swapped[1], swapped[2] = images[2], images[1]
	identity = cagegrid.Identity(cams, swapped, fit["grid"])
	assert identity["swaps"] == {"frontL": "frontR", "frontR": "frontL"}
	assert cagegrid.Verdict(identity, lambda text: None) == 3


# ---------------------------------------------------------------- the cage

def test_the_cage_from_the_grid_is_a_cage_like_a_clicked_one(scene, tmp_path):
	cams, images, maps, fit = scene
	cage = cagegrid.MakeCage(fit, cams)
	assert cage.height == pytest.approx(fit["wall_top"])
	assert cage.convex and cage.Check(cams) is None
	inside = np.array([[0, 0, 30], [140, 90, 100], [-140, -90, 5]], float)
	outside = np.array([[0, 0, -60], [0, 0, 260], [0, -200, 30], [400, 0, 30]], float)
	assert not cage.Outside(inside).any() and cage.Outside(outside).all()
	# Its corners projected into each camera are its "clicks": on another calibration it is refused
	assert set(cage.clicks) == {c.name for c in cams}
	moved = [pose3d.Camera(c.name, c.K, c.dist, c.R, c.t + [30.0, 0, 0], c.size) for c in cams]
	assert cage.Check(moved) is not None
	# Saved as a campy cage; Load gives it back; the grid is kept for the identity check
	path = tmp_path / "calibration.cage.json"
	cage.Save(str(path), made="grid", grid=fit["grid"], fit=cagegrid.Describe(fit, cams))
	again = pose3d.Cage.Load(str(path))
	np.testing.assert_allclose(again.corners, cage.corners, atol=1e-3)
	assert cagegrid.LoadGrid(str(path))["pitch"] == INCH
	d = json.loads(path.read_text())
	assert d["fit"]["squares"] and d["made"] == "grid"
	# A clicked cage's file has no grid
	cage.Save(str(path))
	assert cagegrid.LoadGrid(str(path)) is None


def test_campy_cage_makes_the_cage_and_the_check_pictures(scene, tmp_path):
	import cv2
	cams, images, maps, fit = scene
	calibration = tmp_path / "calibration.toml"
	write_calibration(str(calibration), cams)
	folder = tmp_path / "empty"
	folder.mkdir()
	for c, im in zip(cams, images):
		cv2.imwrite(str(folder / "Camera{}_{}.png".format(cams.index(c), c.name)), im)
	assert cagegrid.Main([str(calibration), str(folder), "--size", "12x8"]) == 0
	cage = pose3d.Cage.Load(pose3d.CagePath(str(calibration)))
	for X in TRUE_CORNERS:
		assert nearest(cage.corners, X) < 2.0
	check = cagegrid.CheckFolder(str(calibration))
	assert sorted(os.listdir(check)) == sorted(["sheet.jpg"] + ["{}.jpg".format(c.name) for c in cams] +
		["{}_empty.png".format(c.name) for c in cams])
	# --check: the same pictures match; two swapped don't, and exit 3
	assert cagegrid.Main([str(calibration), str(folder), "--check"]) == 0
	names = sorted(os.listdir(folder))
	a, b = folder / names[1], folder / names[2]
	shutil.move(str(a), str(tmp_path / "x.png"))
	shutil.move(str(b), str(a))
	shutil.move(str(tmp_path / "x.png"), str(b))
	assert cagegrid.Main([str(calibration), str(folder), "--check"]) == 3
	# and a cage made from them isn't saved (the one before stays)
	before = open(pose3d.CagePath(str(calibration))).read()
	assert cagegrid.Main([str(calibration), str(folder), "--size", "12,8"]) == 3
	assert open(pose3d.CagePath(str(calibration))).read() == before


# ---------------------------------------------------------------- the pieces

def test_agreement_is_each_pairs_own_correlation():
	# All pairs at once, from sums: the same as each pair's correlation over the points both see
	rng = np.random.default_rng(3)
	S = rng.uniform(0, 1, (5, 4000))
	S[1] = 0.7 * S[0] + 0.3 * S[1]
	V = rng.uniform(0, 1, S.shape) > 0.3
	V[4] = False
	V[4, :100] = True                                   # too few in common with any: left out
	tot = wsum = 0.0
	for i in range(5):
		for j in range(i + 1, 5):
			m = V[i] & V[j]
			k = int(m.sum())
			if k < 300:
				continue
			a, b = S[i][m] - S[i][m].mean(), S[j][m] - S[j][m].mean()
			tot += k * (a @ b) / math.sqrt((a @ a) * (b @ b))
			wsum += k
	assert cagegrid.Agreement(S, V) == pytest.approx(tot / wsum, abs=1e-9)
	assert cagegrid.Agreement(S[:1], V[:1]) == -1.0


def test_the_lens_models_fold_back_radius():
	assert cagegrid.FoldRadius2([-0.3, 0, 0, 0, 0]) == pytest.approx(1 / 0.9)
	assert cagegrid.FoldRadius2([0, 0, 0, 0, 0]) == math.inf
	assert cagegrid.FoldRadius2([0.1, 0.01, 0, 0, 0]) == math.inf
	# with k2: where 1 + 3 k1 s + 5 k2 s^2 = 0 first
	s = cagegrid.FoldRadius2([-0.4, 0.05, 0, 0, 0])
	assert 1 + 3 * -0.4 * s + 5 * 0.05 * s * s == pytest.approx(0, abs=1e-9)


def test_up_from_the_cameras_image_rows():
	cams = rig()
	up, spread = cagegrid.CameraUp(cams)
	assert np.degrees(np.arccos(up @ [0, 0, 1])) < 0.1 and spread < 0.05


def test_pictures_and_videos_found_by_camera_name(tmp_path):
	cams = [pose3d.Camera(n, K, DIST, np.eye(3), [0, 0, 500]) for n in ("mid", "topB")]
	pics = tmp_path / "pics"
	pics.mkdir()
	for name in ("mid.png", "Camera1_topB.png", "Camera1_topB.small.jpg", "notes.txt"):
		(pics / name).write_bytes(b"")
	assert [os.path.basename(p) for _, p in cagegrid.FindPictures(str(pics), cams)] == ["mid.png", "Camera1_topB.png"]
	rec = tmp_path / "rec"
	for name in ("Camera0_mid", "Camera1_topB"):
		(rec / name / "calibration_images").mkdir(parents=True)
		(rec / name / "calibration_images" / "[x]-{}-calibration.mp4".format(name)).write_bytes(b"0")
	found = cagegrid.FindPictures(str(rec), cams)
	assert [k for k, _ in found] == ["video", "video"] and "Camera1_topB" in found[1][1]
	shutil.rmtree(rec / "Camera1_topB")
	with pytest.raises(SystemExit, match="topB"):
		cagegrid.FindPictures(str(rec), cams)


def test_one_video_per_camera_never_campy_session_posts_own_copies(tmp_path):
	# remux: keep leaves the fragmented original beside the plain one (a little bigger); a remux or join stopped part
	# way leaves its temporary file; a restarted ffmpeg's parts are joined into the biggest
	cams = [pose3d.Camera("mid", K, DIST, np.eye(3), [0, 0, 500])]
	cam = tmp_path / "rec" / "Camera0_mid"
	cam.mkdir(parents=True)
	for name, size in (("0.mp4", 10), ("0.fragmented.mp4", 11), ("0.remux-tmp.mp4", 12), (".0.mp4", 13)):
		(cam / name).write_bytes(b"0" * size)
	assert cagegrid.FindPictures(str(tmp_path / "rec"), cams) == [("video", str(cam / "0.mp4"))]
	for name, size in (("0.part2.mp4", 30), ("0.join-tmp.mp4", 50), ("0.joined.mp4", 40)):
		(cam / name).write_bytes(b"0" * size)
	assert cagegrid.FindPictures(str(tmp_path / "rec"), cams) == [("video", str(cam / "0.joined.mp4"))]


def test_the_empty_arena_from_a_video_where_something_moves(tmp_path):
	# A still background with a bright square going across it: the median of frames spread through the video is
	# the background, the square gone
	H, W, n = 96, 128, 60
	yy, xx = np.mgrid[0:H, 0:W]
	background = (40 + 60 * ((xx // 16 + yy // 16) % 2)).astype(np.uint8)
	frames = []
	for i in range(n):
		f = background.copy()
		x = int(i / n * (W - 24))
		f[30:60, x:x + 24] = 230
		frames.append(f)
	path = tmp_path / "v.mp4"
	cmd = [videoread.FfmpegExe(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", "{}x{}".format(W, H),
		"-r", "30", "-i", "-", "-c:v", "libx264", "-g", "10", "-crf", "10", "-pix_fmt", "yuv420p", str(path)]
	subprocess.run(cmd, input=np.stack(frames).tobytes(), check=True)
	got = cagegrid.VideoBackground(str(path), frames=15)
	assert got.shape == (H, W)
	assert np.median(np.abs(got.astype(int) - background)) <= 3
	assert np.abs(got.astype(int) - background)[30:60].max() < 60      # no square left


def test_a_fit_whose_grid_misses_the_cameras_lines_is_not_saved(scene, tmp_path, monkeypatch):
	# Pictures that aren't of the empty arena (a calibration board held in the middle stays in their median): the fit
	# finds something, but its grid doesn't land on the cameras' lines, and campy-cage says so instead of saving it
	import cv2
	cams, images, maps, fit = scene
	calibration = tmp_path / "calibration.toml"
	write_calibration(str(calibration), cams)
	folder = tmp_path / "empty"
	folder.mkdir()
	for c, im in zip(cams, images):
		cv2.imwrite(str(folder / "{}.png".format(c.name)), im)
	bad = dict(fit, ok=False, ratios=[1.0] * len(cams))
	monkeypatch.setattr(cagegrid, "Fit", lambda *a, **k: bad)
	assert cagegrid.Main([str(calibration), str(folder), "--size", "12,8"]) == 4
	assert not os.path.exists(pose3d.CagePath(str(calibration)))
	assert os.path.exists(os.path.join(cagegrid.CheckFolder(str(calibration)), "sheet.jpg"))   # to look at why


def test_the_fit_measures_how_far_each_cameras_lines_are(scene):
	# Where each picture has the grid's lines, against where the fit projects them: the made-up cameras' lens
	# model is exact, so within a pixel or so everywhere
	cams, images, maps, fit = scene
	assert fit["ok"]
	for m in fit["misses"]:
		assert len(m["px"]) > 200 and np.median(m["px"]) < 1.0 and np.percentile(m["px"], 90) < 3.5
	d = cagegrid.Describe(fit, cams)
	assert set(d["miss_px"]) == {c.name for c in cams}


# ---------------------------------------------------------------- campy-live-view's check at the start

class Feed:
	# A camera's live feed, for the view: its one frame
	def __init__(self, image):
		self.image = image

	def Latest(self):
		return 1

	def Read(self, n, buffer):
		import types
		return types.SimpleNamespace(image=self.image[..., None], frameNumber=n, hostTime=0)


def test_the_view_checks_the_cameras_names_at_the_start_of_a_recording(scene, tmp_path):
	pytest.importorskip("cv2")
	import time
	from campy import liveview
	from test_liveview import view_args, name
	cams, images, maps, fit = scene
	viewer = liveview.Viewer(view_args(name()))
	viewer.calibration = cams
	viewer.cagePath = str(tmp_path / "calibration.cage.json")
	cagegrid.MakeCage(fit, cams).Save(viewer.cagePath, made="grid", grid=fit["grid"])
	viewer.WatchCage()
	assert viewer.cage is not None and viewer.cageGrid is not None

	def checked(readers):
		viewer.readers, viewer.buffers, viewer.pixelFormats = readers, [None] * len(readers), [""] * len(readers)
		viewer.namesTried = 0.0
		viewer.names = None
		viewer.CheckNames()
		until = time.monotonic() + 60
		while viewer.names is None and time.monotonic() < until:
			time.sleep(0.05)
		return viewer.names
	names = checked([Feed(im) for im in images])
	assert names["swaps"] == {} and viewer.NamesText() is None
	assert not any("swapped" in text for text, _ in viewer.Alerts())
	# Once a recording: not again for the same feeds
	viewer.namesTried = 0.0
	viewer.CheckNames()
	assert viewer.names is names
	# A new recording whose cameras came up in another order: said in red at the top of the panel
	swapped = [Feed(im) for im in images]
	swapped[1], swapped[2] = swapped[2], swapped[1]
	names = checked(swapped)
	assert names["swaps"] == {"frontL": "frontR", "frontR": "frontL"}
	text, color = viewer.Alerts()[0]
	assert "Camera names swapped?" in text and "frontL -> frontR" in text and color == liveview.WARN
	# A cage clicked by hand has no grid: nothing checked
	pose3d.Cage(fit["corners"], fit["up"]).Save(viewer.cagePath)
	viewer.cageAt = None
	viewer.WatchCage()
	assert viewer.cageGrid is None and viewer.names is None
