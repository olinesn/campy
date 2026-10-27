"""
The cage (campy/pose3d.py's Cage): an animal whose 3D lands outside it is not
found, in campy-live's 3D step and in what campy-live-view draws; and the cage
drawn in campy-live-view from its floor's corners clicked in the cameras.
"""

import json, os, time, types

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from campy import defaults, live, pose3d

import test_pose3d as scene3d


BOX = [(-250, -200, 0), (250, -200, 0), (250, 200, 0), (-250, 200, 0)] # the test rig's cage, mm (world z up)


def box(height=100.0, margin=pose3d.CAGE_MARGIN):
	return pose3d.Cage(BOX, (0, 0, 1), height, margin)


def write_calibration(path, cams):
	# anipose's calibration.toml (as test_liveflag's)
	with open(path, "w") as f:
		for i, cam in enumerate(cams):
			f.write("[cam_{}]\nname = \"{}\"\nsize = [ 1280, 1024,]\n".format(i, cam.name))
			f.write("matrix = {}\ndistortions = {}\n".format(cam.K.tolist(), cam.dist.tolist()))
			f.write("rotation = {}\ntranslation = {}\n\n".format(Rotation.from_matrix(cam.R).as_rotvec().tolist(),
				cam.t.tolist()))


def clicks_of(cams, corners, skip=()):
	# Each corner as each camera sees it, in pixels; skip: (camera, corner) pairs not seen
	return {c: [None if (c, k) in skip else tuple(cam.Project(np.asarray(X, float))) for k, X in enumerate(corners)]
		for c, cam in enumerate(cams)}


# ---------------------------------------------------------------- the cage

def test_a_point_is_in_the_cage_over_its_floor_between_floor_and_top():
	cage = box(height=80, margin=10)
	assert cage.convex
	points = {(0, 0, 40): False, (255, 0, 40): False, (265, 0, 40): True, (0, -205, 40): False, (0, -215, 40): True,
		(0, 0, -5): False, (0, 0, -15): True, (0, 0, 89): False, (0, 0, 95): True, (0, 0, 2000): True,
		(-900, 300, 0): True}
	X = np.array(list(points), float)
	assert cage.Outside(X).tolist() == list(points.values())
	assert cage.Outside(np.full((2, 3), np.nan)).tolist() == [False, False] # not judged
	assert cage.Outside(X.reshape(1, -1, 3)).shape == (1, len(X))


def test_a_cage_whose_floor_is_not_convex():
	# An L: the notch is outside, however near the corners around it
	L = [(0, 0, 0), (200, 0, 0), (200, 100, 0), (100, 100, 0), (100, 200, 0), (0, 200, 0)]
	cage = pose3d.Cage(L, (0, 0, 1), 50, margin=5)
	assert not cage.convex
	X = np.array([(50, 50, 10), (150, 50, 10), (50, 150, 10), (150, 150, 10), (103, 150, 10), (110, 150, 10)], float)
	assert cage.Outside(X).tolist() == [False, False, False, True, False, True]


def test_the_convex_cages_quick_test_is_the_polygons_but_at_its_corners():
	cage = box(margin=20)
	X = np.random.default_rng(0).uniform(-400, 400, (20000, 3)) * [1, 1, 0.3]
	quick = cage.Outside(X)
	cage.convex = False
	exact = cage.Outside(X)
	differ = quick != exact
	assert differ.sum() < 0.005 * len(X)
	# only within the margin of a corner, where the quick one's corner is square, not round
	q = X[differ, :2]
	corners = np.array(BOX)[:, :2]
	assert (np.linalg.norm(q[:, None] - corners[None], axis=-1).min(1) < 20 * np.sqrt(2) + 1e-6).all()
	assert not (quick & ~exact).any() # the quick one keeps more, never less


def test_corners_clicked_in_two_or_more_cameras_make_the_cage():
	cams = scene3d.rig()
	# camera 2 does not see corner 3: two cameras are enough for it
	cage, err = pose3d.Cage.FromClicks(clicks_of(cams, BOX, skip={(2, 2)}), cams, height=120)
	assert np.allclose(cage.corners, BOX, atol=0.1) and err.max() < 0.1
	assert np.allclose(cage.up, [0, 0, 1], atol=1e-6) # the cameras' side of the floor
	assert cage.height == 120 and cage.margin == pose3d.CAGE_MARGIN
	assert cage.clicks["right"][2] is None and np.allclose(cage.clicks["top"][0], cams[0].Project(np.array(BOX[0], float)))
	# Not seen by 2 cameras, or too few corners
	with pytest.raises(ValueError, match="corners 3"):
		pose3d.Cage.FromClicks(clicks_of(cams, BOX, skip={(1, 2), (2, 2)}), cams)
	with pytest.raises(ValueError):
		pose3d.Cage.FromClicks(clicks_of(cams, BOX[:2]), cams)
	# One camera's corners clicked in another order: its rays miss the others'
	wrong = clicks_of(cams, BOX)
	wrong[1] = wrong[1][::-1]
	_, err = pose3d.Cage.FromClicks(wrong, cams)
	assert err.max() > 50


def test_a_cage_is_saved_whole_and_read_back(tmp_path):
	cams = scene3d.rig()
	cage, _ = pose3d.Cage.FromClicks(clicks_of(cams, BOX), cams, height=150)
	path = str(tmp_path / "calibration.cage.json")
	assert pose3d.CagePath(str(tmp_path / "calibration.toml")) == path
	cage.Save(path, by="test")
	assert os.listdir(tmp_path) == ["calibration.cage.json"] # no temporary file left
	d = json.load(open(path))
	assert d["kind"] == "campy cage" and d["by"] == "test" and d["height"] == 150
	back = pose3d.Cage.Load(path)
	assert np.allclose(back.corners, cage.corners, atol=1e-3) and np.allclose(back.up, cage.up)
	assert (back.height, back.margin) == (cage.height, cage.margin) and back.clicks == cage.clicks
	assert back.Check(cams) is None
	# Another calibration: its corners no longer land where they were clicked
	moved = [scene3d.look_at(c.name, eye) for c, eye in zip(cams, [(40, 30, 700), (-560, -280, 450), (530, -300, 470)])]
	assert "another calibration" in back.Check(moved)
	(tmp_path / "other.json").write_text(json.dumps({"kind": "something else"}))
	with pytest.raises(ValueError):
		pose3d.Cage.Load(str(tmp_path / "other.json"))


# ---------------------------------------------------------------- the tracker

def ghost_of(cams, X, s=0.55):
	# What camera 0 sees of X, moved along its rays toward it (s of the way from it): camera 0 cannot tell them
	# apart, and with camera 1's view of it, the two triangulate there, over the cage
	return cams[0].center + s * (X - cams[0].center)


def views(cams, animals):
	# Each camera's detections of animals ([camera][k] = (nodes, 3), or None), as the tracker takes them
	n = max(len(a) for a in animals)
	N = len(scene3d.SKELETON)
	kp = np.full((len(cams), n, N, 2), np.nan)
	for c, cam in enumerate(cams):
		for k, X in enumerate(animals[c]):
			if X is not None:
				kp[c, k] = cam.Project(X)
	return kp, np.ones(kp.shape[:3])


def test_a_wrong_match_outside_the_cage_is_not_found_and_takes_nothing():
	cams = scene3d.rig()
	mouse = scene3d.animal(np.array([20.0, 10, 0]), 0.3)
	ghost = ghost_of(cams, mouse)
	results = {}
	for name, cage in (("none", None), ("cage", box())):
		tracker = pose3d.Tracker(cams, n_animals=1, cage=cage)
		for f in range(5):
			p = tracker.Update(*views(cams, [[mouse]] * 3), frame=f)
			assert np.allclose(p.points3d[0], mouse, atol=1e-3) and not p.outside.any()
		before = tracker.X.copy()
		# Camera 1 sees something else, on its line of sight through the place over the cage that camera 0's
		# rays to the mouse also go through; camera 2 sees nothing
		p = tracker.Update(*views(cams, [[mouse], [ghost], [None]]), frame=5)
		results[name] = p
		if cage is None:
			assert np.allclose(p.points3d[0], ghost, atol=1e-3) and box().Outside(ghost.mean(0)) # over the cage
		else:
			assert np.isnan(p.points3d).all() and np.isnan(p.reproj_px).all()
			assert p.outside.tolist() == [True] and p.n_views.tolist() == [0] and (p.members == -1).all()
			assert np.allclose(tracker.X, before, equal_nan=True) # what the next set is matched against: kept
		p = tracker.Update(*views(cams, [[mouse]] * 3), frame=6)
		assert np.allclose(p.points3d[0], mouse, atol=1e-3) and p.members[0].tolist() == [0, 0, 0]


def test_a_pair_of_detections_outside_the_cage_starts_no_identity():
	cams = scene3d.rig()
	mouse = scene3d.animal(np.array([20.0, 10, 0]), 0.3)
	ghost = ghost_of(cams, mouse)
	for cage in (None, box()):
		tracker = pose3d.Tracker(cams, n_animals=1, cage=cage)
		p = tracker.Update(*views(cams, [[mouse], [ghost], [None]]), frame=0)
		if cage is None:
			assert tracker.n_targets == 1 and np.allclose(p.points3d[0], ghost, atol=1e-3) # identity 0 is the ghost
		else:
			assert tracker.n_targets == 0 and np.isnan(p.points3d).all() and not p.outside.any()
			p = tracker.Update(*views(cams, [[mouse]] * 3), frame=1)
			assert tracker.n_targets == 1 and np.allclose(p.points3d[0], mouse, atol=1e-3) # once there is one


def test_animals_in_the_cage_are_tracked_as_without_it():
	# 5 animals walking, all in the cage: the same identities and 3D with it as without it
	cams = scene3d.rig()
	X = scene3d.scene(120)
	rng = np.random.default_rng(3)
	sets = [scene3d.observe(cams, X[f], rng)[:2] for f in range(len(X))]
	cage = pose3d.Cage([(-400, -400, -60), (400, -400, -60), (400, 400, -60), (-400, 400, -60)], (0, 0, 1), 200)
	a, b = pose3d.Tracker(cams, n_animals=5), pose3d.Tracker(cams, n_animals=5, cage=cage)
	for f, (kp, sc) in enumerate(sets):
		p, q = a.Update(kp, sc, frame=f), b.Update(kp, sc, frame=f)
		assert np.array_equal(p.members, q.members) and np.allclose(p.points3d, q.points3d, equal_nan=True)
		assert not q.outside.any()


def test_a_node_outside_the_cage_is_dropped_its_animal_kept():
	cams = scene3d.rig()
	mouse = scene3d.animal(np.array([20.0, 10, 0]), 0.3)
	cage = pose3d.Cage(BOX, (0, 0, 1), height=100, margin=0)
	lower = mouse - [0, 0, 7] # its lowest nodes just under the floor, the rest in
	p = pose3d.Tracker(cams, n_animals=1, cage=cage).Update(*views(cams, [[lower]] * 3), frame=0)
	under = lower[:, 2] < 0
	assert under.any() and not under.all()
	assert np.isnan(p.points3d[0][under]).all() and np.allclose(p.points3d[0][~under], lower[~under], atol=1e-3)
	assert not p.outside[0] and p.n_views[0] == 3


def test_the_tracker_with_a_cage_is_fast_enough():
	cams = scene3d.rig()
	X = scene3d.scene(200)
	rng = np.random.default_rng(9)
	sets = [scene3d.observe(cams, X[f], rng)[:2] for f in range(len(X))]
	tracker = pose3d.Tracker(cams, n_animals=5, cage=box())
	times = []
	for f, (kp, sc) in enumerate(sets):
		t0 = time.perf_counter()
		tracker.Update(kp, sc, frame=f)
		times.append(time.perf_counter() - t0)
	assert np.median(times[20:]) < 0.02


def test_animal_centres_are_nanmedians():
	rng = np.random.default_rng(1)
	for _ in range(100):
		P = rng.normal(0, 100, (rng.integers(1, 7), rng.integers(1, 16), 3))
		P[rng.random(P.shape[:2]) < 0.4] = np.nan
		with np.errstate(all="ignore"), pytest.warns(RuntimeWarning) if np.isnan(P).all(-1).all(-1).any() else \
				_nothing():
			want = np.nanmedian(P, -2)
		assert np.allclose(pose3d.AnimalCentres(P), want, equal_nan=True)


class _nothing:
	def __enter__(self):
		return self

	def __exit__(self, *a):
		return False


# ---------------------------------------------------------------- campy-live

def live_args(calibration, *extra):
	return live.Parser().parse_args(["--model", "none", "--calibration", calibration, "--animals", "1"] + list(extra))


def test_campy_live_uses_the_cage_beside_the_calibration_from_when_it_is_drawn(tmp_path, monkeypatch):
	monkeypatch.setattr(live, "CAGE_EVERY_S", 0.0)
	said = []
	monkeypatch.setattr(live, "Say", said.append)
	cams = scene3d.rig()
	calibration = str(tmp_path / "calibration.toml")
	write_calibration(calibration, cams)
	names = [c.name for c in cams]
	mouse = scene3d.animal(np.array([20.0, 10, 0]), 0.3)
	ghost = ghost_of(cams, mouse)
	tracker = live.Pose3D(live_args(calibration), names)
	path = pose3d.CagePath(calibration)
	assert tracker.cagePath == path and tracker.tracker.cage is None and "No cage yet" in said[-1]
	for f in range(3):
		tracker.Update(*views(cams, [[mouse]] * 3), f)
	assert not tracker.Update(*views(cams, [[mouse], [ghost], [None]]), 3).outside.any() # no cage: kept
	# Drawn while it runs: used from the next set
	pose3d.Cage.FromClicks(clicks_of(cams, BOX), cams)[0].Save(path)
	p = tracker.Update(*views(cams, [[mouse], [ghost], [None]]), 4)
	assert p.outside.tolist() == [True] and tracker.tracker.cage is not None and "from frame 4" in said[-1]
	# One drawn on another calibration: not used
	moved = [scene3d.look_at(c.name, eye) for c, eye in zip(cams, [(40, 30, 700), (-560, -280, 450), (530, -300, 470)])]
	time.sleep(0.01)
	pose3d.Cage.FromClicks(clicks_of(moved, BOX), moved)[0].Save(path)
	assert not tracker.Update(*views(cams, [[mouse], [ghost], [None]]), 5).outside.any()
	assert tracker.tracker.cage is None and "another calibration" in said[-1]
	# Removed
	pose3d.Cage.FromClicks(clicks_of(cams, BOX), cams)[0].Save(path)
	tracker.Update(*views(cams, [[mouse]] * 3), 6)
	os.remove(path)
	tracker.Update(*views(cams, [[mouse]] * 3), 7)
	assert tracker.tracker.cage is None and "removed" in said[-1]
	assert [c["from_frame"] for c in tracker.cages] == [None, 4, 5, 6, 7]
	assert [c["cage"] is not None for c in tracker.cages] == [False, True, False, True, False]
	assert tracker.cages[2]["refused"] and tracker.cages[1]["cage"]["kind"] == "campy cage"
	# off: never looked at; or a file of its own
	pose3d.Cage.FromClicks(clicks_of(cams, BOX), cams)[0].Save(path)
	off = live.Pose3D(live_args(calibration, "--cage", "off"), names)
	assert off.cagePath is None and off.tracker.cage is None and off.cages == []
	other = str(tmp_path / "elsewhere.cage.json")
	os.replace(path, other)
	assert live.Pose3D(live_args(calibration, "--cage", other), names).tracker.cage is not None


def test_the_log_says_which_animals_were_outside_the_cage(tmp_path):
	log = live.Log(str(tmp_path / "campy-live.npy"), 3, 2, 4, 30, animals3d=2)
	assert "outside_cage" in log.rows_type.names
	frames = [types.SimpleNamespace(hostTime=10, frameNumber=1, written=0, timeStamp=0.0) for _ in range(3)]
	for outside in ([True, False], [True, True]):
		row = np.zeros((), log.rows_type)
		row["frame"], row["read_ns"], row["done_ns"] = 1, 20, 30
		poses = pose3d.Poses(1, np.full((2, 4, 3), np.nan), np.full((2, 3), -1), np.zeros(2, int), np.full((2, 4), np.nan),
			np.array(outside))
		log.Add(row, frames, poses, 40)
		assert row["outside_cage"].tolist() == [int(o) for o in outside]
	assert log.Summary()["outside_cage"] == 3
	log.Close()


def test_a_cage_beside_the_calibration_is_a_changed_default_said_at_startup():
	changed = lambda context: [c.key for c in defaults.Relied({}, {}, context, program="campy-live")]
	assert "--cage" in changed({"cage": True}) and "--cage" not in changed({"cage": False})
	assert "--cage" not in [c.key for c in defaults.Relied({}, {"--cage": "command line"}, {"cage": True},
		program="campy-live")]


# ---------------------------------------------------------------- campy-live-view

def drawn_view(tmp_path):
	from test_liveview import mice, row_for, three_cameras, cv2 # noqa: F401 (cv2: skips without OpenCV)
	cams, truth, keypoints, scores, order = mice()
	row = row_for(5, cams, truth, keypoints, scores, order)
	viewer, frames = three_cameras(cams, row)
	viewer.cagePath, viewer.cageUsed = str(tmp_path / "calibration.cage.json"), True
	viewer.Compose(frames, row) # the tiles' places, for the clicks
	return viewer, frames, row, cams


def click(viewer, c, X, button="left", release=True):
	# A click on camera c's tile where it shows the world point X (release False: the button held)
	from campy import liveview
	cell = [cell for cell in viewer.cells if cell[0] == c][0]
	scale, origin = viewer.ZoomOf(c).Window(viewer.frameShape, viewer.layout.tile, viewer.layout.scale)
	x, y = liveview.Place(viewer.calibration[c].Project(np.asarray(X, float)), scale, origin)
	key = viewer.Press(int(round(cell[2] + x)), int(round(cell[1] + y)), button)
	if release:
		viewer.Release()
	return key


def test_the_cage_is_made_from_its_floors_corners_clicked_in_the_cameras(tmp_path):
	viewer, frames, row, cams = drawn_view(tmp_path)
	from campy import liveview
	from test_liveview import like
	assert viewer.Key(ord("k")) and viewer.drawing == {}
	assert viewer.CageText()[0][0].startswith("Drawing the cage")
	assert viewer.Key(ord("]")) and viewer.cageHeight == 210
	for c in range(3):
		for k, X in enumerate(BOX):
			if (c, k) == (2, 2):
				click(viewer, c, X, "right") # camera 2 does not see corner 3
			else:
				click(viewer, c, X)
	assert len(viewer.draft[0]) == 4 and viewer.draft[2] is not None and viewer.scene.cageDraft is not None
	assert "cage: click corner 5" in viewer.Header(0, frames[0], row, ["a", "b", "c"], ["a", "b", "c"],
		viewer.ZoomOf(0))
	image = viewer.Compose(frames, row)
	c, y, x, h, w = viewer.cells[0]
	assert like(image[y:y + h, x:x + w], liveview.CAGE_DRAFT).sum() > 500
	assert viewer.Key(13) and viewer.drawing is None # Enter: saved
	cage = pose3d.Cage.Load(viewer.cagePath)
	assert np.allclose(cage.corners, BOX, atol=8) and cage.height == 210 # clicked to the tile's pixel
	assert viewer.cage is not None and viewer.scene.cage is not None
	image = viewer.Compose(frames, row)
	for c, y, x, h, w in viewer.cells: # on each camera's tile
		assert like(image[y:y + h, x:x + w], liveview.CAGE).sum() > 500
	y, x, h, w = viewer.sceneCell # and in 3D
	assert like(image[y:y + h, x:x + w], liveview.CAGE).sum() > 200
	assert viewer.CageText()[0][0].startswith("Cage: Cage(4 corners")


def test_corners_clicked_out_of_order_are_not_saved_and_esc_only_stops_drawing(tmp_path):
	viewer, frames, row, cams = drawn_view(tmp_path)
	viewer.Key(ord("k"))
	for c in range(3):
		for X in (BOX if c != 1 else BOX[::-1]):
			click(viewer, c, X)
	assert viewer.Key(13) and viewer.drawing is not None and not os.path.exists(viewer.cagePath)
	assert viewer.Key(8) and len(viewer.drawing[2]) == 3 # Backspace: the last camera clicked's last corner
	assert viewer.Key(27) and viewer.drawing is None # Esc stops drawing; the view goes on
	assert not viewer.Key(27) # and again: quits


def test_x_removes_the_cage(tmp_path):
	viewer, frames, row, cams = drawn_view(tmp_path)
	pose3d.Cage.FromClicks(clicks_of(cams, BOX), cams)[0].Save(viewer.cagePath)
	viewer.cageAt = None
	viewer.WatchCage()
	assert viewer.cage is not None
	viewer.Key(ord("k"))
	assert viewer.cageHeight == viewer.cage.height
	viewer.Key(ord("x"))
	assert viewer.drawing is None and viewer.cage is None and viewer.scene.cage is None
	assert not os.path.exists(viewer.cagePath) and os.path.exists(viewer.cagePath + ".old")


def test_the_view_finds_the_cage_where_campy_live_says(tmp_path):
	from test_liveview import view_args, name, cv2 # noqa: F401
	from campy import liveview
	cams = scene3d.rig()
	calibration = str(tmp_path / "calibration.toml")
	write_calibration(calibration, cams)
	viewer = liveview.Viewer(view_args(name()))
	d = {"rings": viewer.ringNames, "cameras": ["a", "b", "c"], "calibration": calibration,
		"calibration_names": [c.name for c in cams], "animals": 3}
	viewer.Describe(dict(d))
	assert viewer.cagePath == pose3d.CagePath(calibration) and viewer.cageUsed is None # an older campy-live
	viewer.Describe(dict(d, cage=str(tmp_path / "mine.cage.json")))
	assert viewer.cagePath == str(tmp_path / "mine.cage.json") and viewer.cageUsed
	viewer.Describe(dict(d, cage=None)) # its --cage off: drawn beside the calibration, not used
	assert viewer.cagePath == pose3d.CagePath(calibration) and viewer.cageUsed is False


def test_a_corner_clicked_can_be_dragged_where_it_should_be(tmp_path):
	from campy import liveview
	viewer, frames, row, cams = drawn_view(tmp_path)
	viewer.Key(ord("k"))
	off = np.array(BOX[1], float) + [40, 30, 0] # corner 2 clicked off, in camera 0
	for c in range(3):
		for k, X in enumerate(BOX):
			click(viewer, c, off if (c, k) == (0, 1) else X)
	assert viewer.draft[1][1] > 20 # its cameras disagree
	# Pressed on it, it is picked up (no new corner), and moves with the mouse
	assert click(viewer, 0, off, release=False) is None and viewer.drag[:2] == ("corner", 0)
	assert len(viewer.drawing[0]) == 4
	cell = [cell for cell in viewer.cells if cell[0] == 0][0]
	scale, origin = viewer.ZoomOf(0).Window(viewer.frameShape, viewer.layout.tile, viewer.layout.scale)
	x, y = liveview.Place(cams[0].Project(np.array(BOX[1], float)), scale, origin)
	viewer.Move(int(round(cell[2] + x)), int(round(cell[1] + y)))
	viewer.Release()
	assert viewer.drag is None and len(viewer.drawing[0]) == 4 and viewer.draft[1][1] < 5
	# Or by its number, beside it: it keeps where it was from the mouse
	before = viewer.drawing[0][1]
	p = liveview.Place(np.asarray(before, float), scale, origin) + [cell[2], cell[1]]
	(tw, th), _ = liveview.cv2.getTextSize("2", liveview.FONT, 0.5, 1)
	label = np.round(p + [7 + tw / 2, -7 - th / 2]).astype(int)
	assert viewer.Press(*label) is None and viewer.drag[:3] == ("corner", 0, 1)
	viewer.Move(*(label + [20, 10]))
	viewer.Release()
	assert np.allclose(viewer.drawing[0][1], np.asarray(before) + np.array([20, 10]) / scale, atol=2 / scale.min())
	assert len(viewer.drawing[0]) == 4
	# A press away from any corner still adds one
	click(viewer, 0, (0, 0, 0))
	assert len(viewer.drawing[0]) == 5


def test_zoomed_a_drag_while_drawing_moves_the_picture_and_drops_no_corner(tmp_path):
	viewer, frames, row, cams = drawn_view(tmp_path)
	viewer.Key(ord("k"))
	c, y0, x0, h, w = viewer.cells[0]
	viewer.ZoomCamera(0, 3.0)
	before = viewer.ZoomOf(0).centre.copy()
	viewer.Press(x0 + w // 2, y0 + h // 2)
	viewer.Move(x0 + w // 2 + 2, y0 + h // 2 + 1) # a hand's tremble: still a click
	assert viewer.drag[0] == "press"
	viewer.Move(x0 + w // 2 + 60, y0 + h // 2 + 40)
	viewer.Release()
	assert viewer.drawing == {} and not np.allclose(viewer.ZoomOf(0).centre, before) # moved, no corner
	viewer.Press(x0 + w // 2, y0 + h // 2)
	viewer.Move(x0 + w // 2 + 2, y0 + h // 2 + 1)
	viewer.Release()
	assert len(viewer.drawing[0]) == 1 # released where pressed: a corner


def test_a_corners_cross_dragged_in_a_camera_is_that_corner_there(tmp_path):
	from campy import liveview
	viewer, frames, row, cams = drawn_view(tmp_path)
	viewer.Key(ord("k"))
	for c in (0, 1):
		for X in BOX:
			click(viewer, c, X)
	# Camera 2 has no corner: its crosses are where cameras 0 and 1 put them. Corner 3's, dragged a little
	c, y0, x0, h, w = viewer.cells[2]
	scale, origin = viewer.ZoomOf(2).Window(viewer.frameShape, viewer.layout.tile, viewer.layout.scale)
	x, y = np.round(liveview.Place(cams[2].Project(np.array(BOX[2], float)), scale, origin) + [x0, y0]).astype(int)
	viewer.Press(x, y)
	assert viewer.drag[:3] == ("corner", 2, 2) and 2 not in viewer.drawing # nothing yet
	viewer.Move(x + 6, y)
	viewer.Release()
	assert viewer.drawing[2][:2] == [None, None] and len(viewer.drawing[2]) == 3
	assert np.allclose(viewer.drawing[2][2], cams[2].Project(np.array(BOX[2], float)) + [6 / scale[0], 0], atol=2 / scale.min())
	# Clicked on a cross: that corner, where it was clicked
	x, y = np.round(liveview.Place(cams[2].Project(np.array(BOX[3], float)), scale, origin) + [x0, y0]).astype(int)
	viewer.Press(x, y)
	viewer.Release()
	assert len(viewer.drawing[2]) == 4
	assert np.allclose(viewer.drawing[2][3], cams[2].Project(np.array(BOX[3], float)), atol=2 / scale.min())


def test_while_drawing_the_view_says_what_is_missing_and_marks_the_camera_that_is_off(tmp_path):
	from campy import liveview
	from test_liveview import like
	viewer, frames, row, cams = drawn_view(tmp_path)
	viewer.Key(ord("k"))
	status = lambda: viewer.CageStatus()[0]
	assert status()[1] == liveview.WARN and "a cage needs 3" in status()[0]
	for c in range(3):
		for k, X in enumerate(BOX):
			click(viewer, c, np.array(X, float) + ([60, 0, 0] if (c, k) == (2, 0) else 0)) # camera 2's corner 1 off
	assert status()[1] == liveview.WARN and "Corner 1: Camera2 is" in status()[0] and "where the others put it" in status()[0]
	camera, X, px = viewer.draft[3][0]
	assert camera == 2 and np.allclose(X, BOX[0], atol=3) and px > 20
	lines = viewer.CageStatus()
	assert [color for _, color in lines[1:5]] == [liveview.WARN, liveview.FINE, liveview.FINE, liveview.FINE]
	# On camera 2's tile, its corner 1 in red, with a line to where the others put it
	image = viewer.Compose(frames, row)
	c, y, x, h, w = viewer.cells[2]
	assert like(image[y:y + h, x:x + w], liveview.WARN).sum() > 30
	c, y, x, h, w = viewer.cells[1]
	assert like(image[y:y + h, x:x + w], liveview.WARN).sum() > 30 # red in every camera
	# Enter: not saved, and why, in red, until the next change
	assert viewer.Key(13) and not os.path.exists(viewer.cagePath)
	assert viewer.CageStatus()[-1][1] == liveview.WARN and viewer.CageStatus()[-1][0].startswith("Not saved: corner 1")
	viewer.Key(8) # camera 2's last corner (4) taken back: the message goes
	assert not any(text.startswith("Not saved") for text, _ in viewer.CageStatus())
	viewer.drawing[2] = viewer.drawing[2][:0] # camera 2's clicks all gone: the others agree
	viewer.Draft()
	assert status() == ("Ready: Enter saves the cage (4 corners, 200 mm high)", liveview.FINE)
