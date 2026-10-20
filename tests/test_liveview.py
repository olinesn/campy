"""
campy-live-view (campy/liveview.py) and the results it shows
(campy/liveresults.py): each campy-live row is drawn on the frames it was made
from, with each animal's 3D projected into the camera, in its identity's
colour, and the camera's own 2D beside it. No window is opened: the views are
drawn into arrays (and PNGs) and their pixels checked.
"""

import itertools, json, os, threading, time, types

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from campy import live, livefeed, liveresults, liveview, pose3d

import test_pose3d as scene3d
from test_livefeed import name, rings, run_live, seeing # noqa: F401 (rings: a fixture)

EDGES = [[0, 5], [5, 6], [6, 3], [3, 4], [1, 5], [2, 5]]


def mice(n=3):
	# n mice standing still, seen by the test rig's 3 cameras: (cameras, truth (mice, nodes, 3), keypoints, order)
	cams = scene3d.rig()
	truth = scene3d.scene(1, n=n)[0]
	keypoints, scores, order = scene3d.observe(cams, truth, np.random.default_rng(0), noise=0.0, drop_node=0.0,
		instances=n)
	return cams, truth, keypoints, scores, order


def row_for(frame, cams, truth, keypoints, scores, order, wrong=None):
	# campy-live's row for a set: every mouse matched right (identity = true mouse), or with
	# wrong = (camera, identity a, identity b): a's and b's detections there swapped
	n, N = truth.shape[:2]
	row = np.zeros((), live.RowType(len(cams), n, N, n))
	row["frame"] = frame
	row["host_ns"] = 1000
	row["done_ns"], row["pose3d_ns"] = 2000, 3000
	row["keypoints"] = keypoints
	row["peak_vals"] = scores
	row["found"] = n
	row["points3d"] = truth
	row["n_views"] = len(cams)
	row["reproj_px"] = 0.5
	for c in range(len(cams)):
		for slot, mouse in enumerate(order[c]):
			row["members"][mouse, c] = slot
	if wrong is not None:
		c, a, b = wrong
		row["members"][[a, b], c] = row["members"][[b, a], c]
	return row


def like(pixels, color):
	# Which pixels (..., 3) are color, or color blended with black (anti-aliasing, the outline)
	p = np.asarray(pixels, float)
	c = np.asarray(color, float)
	k = (p @ c) / (c @ c)
	return (k > 0.5) & (np.linalg.norm(p - k[..., None] * c, axis=-1) < 25)


def near(img, xy, color, r=3):
	# Whether color is among the pixels within r of xy
	x, y = int(round(xy[0])), int(round(xy[1]))
	patch = img[max(0, y - r):y + r + 1, max(0, x - r):x + r + 1]
	return bool(like(patch, color).any())


def share(flags):
	flags = list(flags)
	return sum(flags) / len(flags)


def scaled(cam, s):
	# The camera, with images s times the size
	K = np.array(cam.K) * s
	K[2, 2] = 1
	return pose3d.Camera(cam.name, K, cam.dist, cam.R, cam.t)


# -------------------------------------------------------------- the results ring

def test_a_ring_carries_its_description(rings):
	ring = name()
	rings(ring, (1, 64, 1), np.uint8, description='{"kind": "x", "nodes": ["Nose"]}')
	reader = livefeed.RingReader(ring)
	try:
		assert json.loads(reader.Description()) == {"kind": "x", "nodes": ["Nose"]}
	finally:
		reader.Close()
	plain = name()
	rings(plain, (4, 4), np.uint8)
	reader = livefeed.RingReader(plain)
	try:
		assert reader.Description() == "" # a camera's
	finally:
		reader.Close()
	with pytest.raises(ValueError, match="at most"):
		livefeed.RingWriter(name(), (4, 4), np.uint8, description="x" * (livefeed.DESCRIPTION_BYTES + 1))


def test_rows_go_through_the_results_ring_as_they_are():
	cams, truth, keypoints, scores, order = mice()
	ring = name()
	rows = [row_for(n, cams, truth, keypoints, scores, order) for n in (5, 9, 10)]
	said = []
	publisher = liveresults.Publisher(ring, rows[0].dtype, say=said.append, slots=4, cameras=["a", "b", "c"],
		edges=EDGES, animals=3, frame_rate=30.0)
	try:
		results = liveresults.Results(ring)
		try:
			assert results.Latest() == 0 and results.Newest() == (0, None)
			for row in rows:
				publisher.Write(row)
			assert results.Latest() == 3
			n, got = results.Newest()
			assert n == 3 and got.tobytes() == rows[2].tobytes()
			assert results.Read(1)["frame"] == 5 and results.Read(2)["frame"] == 9
			assert results.description["edges"] == EDGES and results.description["cameras"] == ["a", "b", "c"]
			assert results.rows_type == rows[0].dtype
			for _ in range(4): # the ring holds the newest 4
				publisher.Write(rows[0])
			assert results.Read(1) is None and results.Read(7) is not None
			assert results.Newest(after=7) == (7, None) # nothing newer
			assert not results.Ended()
			publisher.Close()
			assert results.Ended()
		finally:
			results.Close()
	finally:
		publisher.Close()
	assert said == []


def test_shared_memory_of_another_kind_is_not_results(rings):
	ring = name()
	rings(ring, (4, 4), np.uint8) # a camera's
	with pytest.raises(ValueError, match="not campy-live's results"):
		liveresults.Results(ring)
	with pytest.raises(FileNotFoundError):
		liveresults.Results(name())


def test_a_results_ring_that_fails_is_said_once_and_campy_live_goes_on(monkeypatch):
	cams, truth, keypoints, scores, order = mice()
	row = row_for(1, cams, truth, keypoints, scores, order)
	said = []
	publisher = liveresults.Publisher(name(), row.dtype, say=said.append)
	def Write(*a, **k):
		raise OSError("the memory went")
	monkeypatch.setattr(publisher.writer, "Write", Write)
	for _ in range(3):
		publisher.Write(row) # never raises
	assert len(said) == 1 and "the memory went" in said[0] and publisher.writer is None


# -------------------------------------------------------------- drawing

def test_each_animals_3d_is_drawn_where_the_camera_sees_it():
	cams, truth, keypoints, scores, order = mice()
	row = row_for(1, cams, truth, keypoints, scores, order)
	for c, cam in enumerate(cams):
		img = np.zeros((512, 640, 3), np.uint8)
		offsets = liveview.DrawCamera(img, 0.5, row, c, cam, EDGES, show2d=False, lines=False)
		for a in range(3):
			px = cam.Project(truth[a]) * 0.5
			# Identity a's nodes, in its colour, where the camera sees them (a label may cover one)
			assert share(near(img, p, liveview.Color(a), 1) for p in px) > 0.9
			assert offsets[a] < 0.01 # its 2D is its 3D projected
		# And nothing far from every mouse
		assert (img[:20, -20:] == 0).all()


def test_the_cameras_2d_is_drawn_in_the_colour_it_was_matched_to():
	cams, truth, keypoints, scores, order = mice()
	row = row_for(1, cams, truth, keypoints, scores, order)
	c = 1
	row["members"][2, c] = -1 # mouse 2's detection in camera 1 matched to no animal
	img = np.zeros((1024, 1280, 3), np.uint8)
	liveview.DrawCamera(img, 1.0, row, c, cams[c], EDGES, show3d=False)
	r = liveview.RADIUS_2D
	for slot, mouse in enumerate(order[c]):
		color = liveview.UNMATCHED if mouse == 2 else liveview.Color(mouse)
		# Open circles around each node: their colour r px to the side
		assert share(near(img, p + [r, 0], color, 1) for p in keypoints[c, slot]) > 0.8
	# Open, not filled: a detection of one node alone
	row["keypoints"][c, 0, 1:] = np.nan
	img[:] = 0
	liveview.DrawCamera(img, 1.0, row, c, cams[c], EDGES, show3d=False)
	p = row["keypoints"][c, 0, 0]
	assert near(img, p + [r, 0], liveview.Color(order[c][0]), 1) and not near(img, p, liveview.Color(order[c][0]), 1)


def test_a_wrong_match_shows_its_2d_away_from_its_3d():
	# Identities 0 and 1 swapped in camera 2: there, each one's 2D (open
	# circles) is on the other's mouse, far from its own 3D (dots), and its
	# label says how far; the cameras that matched them right say ~0
	cams, truth, keypoints, scores, order = mice()
	row = row_for(1, cams, truth, keypoints, scores, order, wrong=(2, 0, 1))
	img = np.zeros((1024, 1280, 3), np.uint8)
	offsets = liveview.DrawCamera(img, 1.0, row, 2, cams[2], EDGES)
	apart = np.linalg.norm(cams[2].Project(truth[0]) - cams[2].Project(truth[1]), axis=-1).mean()
	assert offsets[0] == pytest.approx(apart, rel=1e-6) and offsets[1] == pytest.approx(apart, rel=1e-6)
	assert offsets[2] < 0.01
	# Identity 0's open circles on mouse 1, and 1's on mouse 0 (the 3D, drawn over them, left out)
	img[:] = 0
	liveview.DrawCamera(img, 1.0, row, 2, cams[2], EDGES, show3d=False)
	for a, mouse in ((0, 1), (1, 0)):
		assert share(near(img, p + [liveview.RADIUS_2D, 0], liveview.Color(a), 1)
			for p in keypoints[2, order[2].index(mouse)]) > 0.8
	ok = np.zeros((1024, 1280, 3), np.uint8)
	assert all(v < 0.01 for v in liveview.DrawCamera(ok, 1.0, row, 0, cams[0], EDGES).values())


def test_3d_behind_the_camera_or_missing_is_not_drawn():
	cams, truth, keypoints, scores, order = mice()
	row = row_for(1, cams, truth, keypoints, scores, order)
	row["points3d"][0] = np.nan # not triangulated this set
	row["points3d"][1] = cams[0].center + (cams[0].center - truth[1]) # behind camera 0
	img = np.zeros((1024, 1280, 3), np.uint8)
	offsets = liveview.DrawCamera(img, 1.0, row, 0, cams[0], EDGES, show2d=False)
	assert set(offsets) == {1, 2}
	assert not like(img, liveview.Color(0)).any()
	# Identity 1 is only named, at the foot of the image, as 3D that lands off it
	assert not like(img[:-30], liveview.Color(1)).any() and like(img[-30:], liveview.Color(1)).sum() > 20
	assert like(img[:-30], liveview.Color(2)).sum() > 50


def test_2d_only_results_are_drawn_unmatched():
	cams, truth, keypoints, scores, order = mice()
	row = np.zeros((), live.RowType(3, 3, truth.shape[1])) # campy-live without --calibration
	row["keypoints"] = keypoints
	img = np.zeros((1024, 1280, 3), np.uint8)
	assert liveview.DrawCamera(img, 1.0, row, 0, None, EDGES) == {}
	assert near(img, keypoints[0, 0, 0] + [liveview.RADIUS_2D, 0], liveview.UNMATCHED, 1)


def test_layout_fits_the_grid_and_the_panel():
	three = liveview.Layout(3, (1024, 1280), size=(1800, 960))
	assert (three.cols, three.rows, three.inGrid) == (2, 2, True)
	assert three.size[0] <= 960 and three.size[1] <= 1800
	assert three.panel == (three.tile[0], three.tile[1], three.tile[0], three.tile[1]) # the 4th cell
	one = liveview.Layout(1, (1024, 1280), size=(1800, 960), panel_lines=9)
	assert not one.inGrid and one.panel[0] == one.tile[0] and one.size[0] <= 960
	assert liveview.Layout(1, (1024, 1280), scale=0.25).tile == (256, 320)


# -------------------------------------------------------------- the view

def view_args(prefix, *extra):
	return liveview.ParseArgs(["--name", prefix, "--cameras", "3", "--no-window", "--priority", "normal"] + list(extra))


def test_each_result_is_drawn_on_the_frames_it_was_made_from(rings):
	# The feed is ahead of the results: the view reads the row's own frames
	# back from the feed, not the newest
	cams, truth, keypoints, scores, order = mice()
	prefix = name()
	writers = [rings(livefeed.RingName(prefix, c), (64, 80), np.uint8, slots=8, cameraName="Camera{}".format(c))
		for c in range(3)]
	for n in range(1, 13):
		for w in writers:
			w.Write(np.full((64, 80), 10 * n, np.uint8), n, 1000 * n)
	small = [scaled(c, 1 / 16) for c in cams] # the rig at 80x64
	publisher = liveresults.Publisher(liveresults.RingName(prefix), live.RowType(3, 3, truth.shape[1], 3),
		cameras=["Camera0", "Camera1", "Camera2"], rings=[livefeed.RingName(prefix, c) for c in range(3)],
		edges=EDGES, animals=3)
	try:
		viewer = liveview.Viewer(view_args(prefix))
		viewer.Check()
		viewer.calibration = small
		assert viewer.Step()[2] is None # no row yet: the frames alone
		publisher.Write(row_for(9, cams, truth, keypoints / 16, scores, order))
		image, frame, row = viewer.Step()
		assert frame == 9 and row["frame"] == 9
		# Frame 9's pixels (90), not the newest frame's (120), under camera 0's tile
		th, tw = viewer.layout.tile
		corner = image[th - 3, 2] # bottom left of camera 0's tile, clear of the drawing
		assert tuple(corner) == (90, 90, 90)
		assert viewer.Step() is None # nothing newer
		# A row whose frames are no longer in the feed: counted, not drawn
		for n in range(13, 30):
			for w in writers:
				w.Write(np.full((64, 80), n, np.uint8), n, 1000 * n)
		publisher.Write(row_for(10, cams, truth, keypoints / 16, scores, order))
		assert viewer.Step() is None and viewer.gone == 1
		# Rows made while the view was busy: the newest is drawn, the others passed over
		for n in (25, 26, 27):
			publisher.Write(row_for(n, cams, truth, keypoints / 16, scores, order))
		image, frame, row = viewer.Step()
		assert frame == 27 and viewer.passed == 2
		viewer.Close()
	finally:
		publisher.Close()


def test_without_results_the_view_shows_the_newest_frames(rings):
	prefix = name()
	writers = [rings(livefeed.RingName(prefix, c), (64, 80), np.uint8, slots=8) for c in range(3)]
	for n in range(1, 6):
		for w in writers:
			w.Write(np.full((64, 80), 10 * n, np.uint8), n, 1000 * n)
	viewer = liveview.Viewer(view_args(prefix, "--camera", "1"))
	try:
		viewer.Check()
		viewer.lastRow = time.monotonic() - 1 # no row for a second
		image, frame, row = viewer.Step()
		assert viewer.Shown() == [1] and viewer.layout.cols == 1
		th, tw = viewer.layout.tile
		assert frame == 5 and row is None and tuple(image[th - 3, tw - 3]) == (50, 50, 50)
	finally:
		viewer.Close()


def test_keys_pick_the_cameras_and_what_is_drawn():
	viewer = liveview.Viewer(view_args(name()))
	assert viewer.Shown() == [0, 1, 2]
	assert viewer.Key(ord("2")) and viewer.Shown() == [1]
	assert viewer.Key(ord("9")) and viewer.Shown() == [1] # no camera 9
	assert viewer.Key(ord("0")) and viewer.Shown() == [0, 1, 2]
	assert viewer.Key(ord("d")) and not viewer.show2d
	assert viewer.Key(ord("p")) and not viewer.show3d
	assert viewer.Key(ord("l")) and not viewer.lines
	assert not viewer.Key(ord("q")) and not viewer.Key(27)


# -------------------------------------------------------------- with campy-live

def test_campy_live_publishes_every_row_it_logs(tmp_path, monkeypatch):
	# Read as campy-live makes them, the rows in shared memory are the rows of
	# its log, with what the view needs to draw them
	args, truth, order = seeing(tmp_path, monkeypatch)
	prefix = name()
	got, description = {}, {}
	done = threading.Event()

	def Read():
		results = None
		while not done.is_set() or results is not None:
			if results is None:
				try:
					results = liveresults.Results(liveresults.RingName(prefix))
					description.update(results.description)
					after = 0
				except (OSError, ValueError): # not there, or being made
					time.sleep(0.001)
					continue
			latest = results.Latest()
			for n in range(after + 1, latest + 1):
				row = results.Read(n)
				assert row is not None, "row {} replaced before it was read".format(n)
				got[n] = row
			after = latest
			if results.Ended():
				results.Close()
				results = None
				done.set()
			time.sleep(0.0005)

	reader = threading.Thread(target=Read, daemon=True)
	reader.start()
	out = str(tmp_path / "live3d")
	try:
		assert run_live(prefix, out, args, frames=120, rate=100) == 0
	finally:
		done.set()
		reader.join(10)
	rows = np.load(out + ".npy")
	assert sorted(got) == list(range(1, len(rows) + 1))
	assert [got[n].tobytes() for n in sorted(got)] == [r.tobytes() for r in rows]
	assert description["rings"] == [livefeed.RingName(prefix, c) for c in range(3)]
	assert description["calibration"] == os.path.abspath(str(tmp_path / "calibration.toml"))
	assert description["calibration_names"] == ["top", "left", "right"] and description["animals"] == 3
	summary = json.load(open(out + ".json"))
	assert summary["results_feed"] == liveresults.RingName(prefix)


def test_campy_live_without_the_results_feed(tmp_path, monkeypatch):
	args, truth, order = seeing(tmp_path, monkeypatch)
	prefix = name()
	made = []
	publisher = liveresults.Publisher.__init__
	monkeypatch.setattr(liveresults.Publisher, "__init__", lambda *a, **k: made.append(1) or publisher(*a, **k))
	out = str(tmp_path / "live3d")
	# 1 s of frames: campy-live looks for the feed every 0.2 s, and could miss a shorter recording
	assert run_live(prefix, out, args + ["--results-feed", "off"], frames=200) == 0
	assert made == [] and json.load(open(out + ".json"))["results_feed"] is None


def small_seeing(tmp_path, monkeypatch, s=0.25):
	# test_livefeed's seeing, with the rig's cameras at s times the size, as the
	# frames recorded by record(): the 3D falls on them
	cams = [scaled(c, s) for c in scene3d.rig()]
	names = ["top", "left", "right"]
	with open(tmp_path / "calibration.toml", "w") as f:
		for i, (cam, label) in enumerate(zip(cams, names)):
			f.write('[cam_{}]\nname = "{}"\nmatrix = {}\ndistortions = {}\nrotation = {}\ntranslation = {}\n\n'
				.format(i, label, cam.K.tolist(), cam.dist.tolist(), cam.R.tolist(), cam.t.tolist()))
	truth = scene3d.scene(1, n=3)[0]
	keypoints, scores, order = scene3d.observe(cams, truth, np.random.default_rng(0), noise=0.2, drop_node=0.0,
		instances=5)

	class Seeing(live.NoModel):
		instances, nodes, edges = 5, truth.shape[1], EDGES

		def __init__(self, args, n_frames, shape):
			pass

		def Predict(self, frames):
			return {"keypoints": keypoints, "peak_vals": scores, "centroids": keypoints[:, :, 0]}

	monkeypatch.setitem(live.MODELS, "seeing", Seeing)
	return ["--model", "seeing", "--report-sec", "0", "--wait-sec", "30", "--animals", "3", "--calibration",
		str(tmp_path / "calibration.toml"), "--calibration-names", ",".join(names)]


def record(prefix, frames, rate, delay, shape=(256, 320)):
	# 3 cameras' frames of shape, gray, into the feed: 32 slots, for a slow test runner
	time.sleep(delay)
	writers = [livefeed.RingWriter(livefeed.RingName(prefix, c), shape, np.uint8, slots=32, frameRate=rate, camera=c,
		cameraName="Camera{}".format(c)) for c in range(3)]
	try:
		start = time.perf_counter()
		for n in range(1, frames + 1):
			while time.perf_counter() < start + n / rate:
				time.sleep(0.0002)
			for w in writers:
				w.Write(np.full(shape, 60, np.uint8), n, time.monotonic_ns(), n / rate)
	finally:
		for w in writers:
			w.Close()


def test_the_view_of_campy_live_as_it_runs(tmp_path, monkeypatch):
	# campy-live-view beside campy-live, without a window: it ends with the
	# recording, has drawn campy-live's results on their frames, and saved
	# views in which every mouse's 3D is drawn in its identity's colour
	args = small_seeing(tmp_path, monkeypatch)
	prefix = name()
	png = tmp_path / "views"
	codes = []
	view = threading.Thread(target=lambda: codes.append(liveview.Run(view_args(prefix, "--png", str(png),
		"--png-every", "10", "--size", "800x700", "--summary", str(tmp_path / "view.json")))))
	view.start()
	feed = threading.Thread(target=record, args=(prefix, 200, 100, 1.0))
	feed.start()
	try:
		assert live.Run(live.ParseArgs(["--cameras", "3", "--name", prefix, "--out", str(tmp_path / "live3d")]
			+ args)) == 0
	finally:
		feed.join()
		view.join(30)
	assert codes == [0]
	summary = json.load(open(tmp_path / "view.json"))
	assert summary["results_drawn"] > 20 and summary["results_gone"] == 0
	saved = sorted(os.listdir(png))
	assert saved and all(f.startswith("view_") and f.endswith(".png") for f in saved)
	image = cv2.imread(str(png / saved[-1]))
	for a in range(3):
		assert like(image, liveview.Color(a)).sum() > 100
