"""
campy-live-view (campy/liveview.py) and the results it shows
(campy/liveresults.py): each campy-live row is drawn on the frames it was made
from, with each animal's 3D projected into the camera, in its identity's
colour, and the camera's own 2D beside it. No window is opened: the views are
drawn into arrays (and PNGs) and their pixels checked.
"""

import csv, itertools, json, os, threading, time, types

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
	# 3 cameras and the 3D: 3 columns, the tiles as large as 3 cameras', the panel in the grid
	four = liveview.Layout(4, (1024, 1280), size=(1800, 960), panel_lines=14)
	assert (four.cols, four.rows, four.inGrid) == (3, 2, True) and four.tile == three.tile
	assert four.panel == (four.tile[0], four.tile[1], four.tile[0], 2 * four.tile[1]) # the 5th and 6th cells


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
		corner = image[th - 6, 6] # bottom left of camera 0's tile, clear of the drawing (and its outline)
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


# -------------------------------------------------------------- bookmarks

def bookmarks(path):
	# The bookmarks' file, as rows of its columns
	with open(path, newline="") as f:
		return list(csv.DictReader(f))


def camera_feeds(rings, recording=None, frames=12, prefix=None):
	# 3 cameras' feeds (frames 1 to frames; camera c's host times 1000 n + c), saying their folders in the
	# recording as campy-acquire's do, or nothing (a campy from before)
	prefix = prefix or name()
	writers = []
	for c in range(3):
		description = json.dumps({"kind": livefeed.CAMERA_KIND, "folder": str(recording / "Camera{}".format(c))}) \
			if recording else ""
		writers.append(rings(livefeed.RingName(prefix, c), (64, 80), np.uint8, slots=8,
			cameraName="Camera{}".format(c), description=description))
	def Write(n):
		for c, w in enumerate(writers):
			w.Write(np.full((64, 80), 10 * n, np.uint8), n, 1000 * n + c)
	for n in range(1, frames + 1):
		Write(n)
	return prefix, Write


def test_a_campy_cameras_feed_says_its_folder_in_the_recording(tmp_path):
	prefix = name()
	recording = tmp_path / "2026-10-20_16-54-14"
	feed = livefeed.Feed({"liveFeed": True, "liveFeedName": prefix, "n_cam": 1, "cameraName": "Camera1",
		"videoFolder": str(recording), "frameRate": 30}, say=lambda text: None)
	try:
		feed.Write(np.zeros((16, 20), np.uint8), 1, 1000, 0.0)
		reader = livefeed.RingReader(livefeed.RingName(prefix, 1))
		try:
			assert json.loads(reader.Description()) == {"kind": "campy camera",
				"folder": os.path.abspath(str(recording / "Camera1"))}
			assert liveview.RecordingFolder([reader]) == os.path.abspath(str(recording))
			assert reader.HostTime(1) == 1000 and reader.HostTime(2) is None
		finally:
			reader.Close()
	finally:
		feed.Close()


def test_the_bookmark_keys_are_no_others():
	for key in "vgdplzrsqa \\0123456789":
		assert liveview.BookmarkKind(ord(key)) is None
	for key in ("left", "right", "up", "down", 27):
		assert liveview.BookmarkKind(key) is None
	assert liveview.BookmarkKind(ord("m")) == "mark"
	assert [liveview.BookmarkKind(ord(k)) for k in "!@#$%^&*("] == [str(k) for k in range(1, 10)]


def test_a_key_writes_a_bookmark_beside_the_recording_at_once(rings, tmp_path):
	cams, truth, keypoints, scores, order = mice()
	recording = tmp_path / "2026-10-20_16-54-14"
	recording.mkdir()
	prefix, Write = camera_feeds(rings, recording)
	publisher = liveresults.Publisher(liveresults.RingName(prefix), live.RowType(3, 3, truth.shape[1], 3),
		cameras=["Camera0", "Camera1", "Camera2"], rings=[livefeed.RingName(prefix, c) for c in range(3)],
		edges=EDGES, animals=3)
	elsewhere = tmp_path / "elsewhere.csv"
	args = view_args(prefix, "--bookmarks", str(elsewhere))
	viewer = liveview.Viewer(args)
	try:
		viewer.Check()
		assert viewer.recording == str(recording)
		publisher.Write(row_for(9, cams, truth, keypoints / 16, scores, order))
		assert viewer.Step()[1] == 9 # the view shows frame 9
		Write(13)
		Write(14) # the cameras are at frame 14
		assert viewer.Key(ord("m"))
		# In the file at once, while the view goes on: the frame shown, each camera's newest frame
		path = recording / "bookmarks.csv"
		marks = bookmarks(path)
		assert len(marks) == 1
		mark = marks[0]
		assert list(mark) == liveview.Bookmarks.Columns(3)
		assert (mark["number"], mark["event"], mark["note"], mark["frame"]) == ("1", "mark", "", "9")
		assert [(mark["frame_{}".format(c)], mark["host_ns_{}".format(c)]) for c in range(3)] == \
			[("14", "14000"), ("14", "14001"), ("14", "14002")]
		assert mark["recording"] == str(recording) and int(mark["host_ns"]) > 0
		assert mark["time"][:10] == time.strftime("%Y-%m-%d")
		# Shift+3 (# on a US keyboard): event 3, and not camera 3 alone
		assert viewer.Key(ord("#")) and viewer.Shown() == [0, 1, 2]
		marks = bookmarks(path)
		assert [(m["number"], m["event"], m["frame"]) for m in marks] == [("1", "mark", "9"), ("2", "3", "9")]
		assert not elsewhere.exists()
		# The view is outlined for a moment (drawn again, as when paused), then not
		image = viewer.Again()
		assert like(image[:, 3], liveview.MARKED).mean() > 0.9
		assert "Bookmark 2 at" in " ".join(text for text, color in viewer.PanelText(viewer.last[1]))
		assert not viewer.FlashOver()
		viewer.flashUntil = 0
		assert viewer.FlashOver()
		image = viewer.Again()
		assert not like(image[:, 3], liveview.MARKED).any()
		assert viewer.Summary(1)["bookmarks"] == 2 and viewer.Summary(1)["bookmark_files"] == [str(path)]
	finally:
		viewer.Close()
	# Started again: its bookmarks go on in the same file
	viewer = liveview.Viewer(args)
	try:
		viewer.Check()
		viewer.Mark("mark", "  rears\nup ")
		assert [(m["number"], m["note"]) for m in bookmarks(path)][-1] == ("3", "rears up")
	finally:
		viewer.Close()
		publisher.Close()


def test_without_the_recordings_folder_bookmarks_go_to_the_bookmarks_file(rings, tmp_path):
	path = tmp_path / "marks" / "b.csv"
	prefix = name()
	viewer = liveview.Viewer(view_args(prefix, "--bookmarks", str(path)))
	try:
		assert viewer.Key(ord("m")) # before the feed: the time only
		_, Write = camera_feeds(rings, frames=12, prefix=prefix) # a campy from before: its feeds say no folder
		viewer.Check()
		assert viewer.recording is None
		viewer.lastRow = time.monotonic() - 1 # no results: the frames alone
		assert viewer.Step()[1] == 12
		assert viewer.Key(ord("("))
		marks = bookmarks(path)
		assert [(m["number"], m["event"], m["frame"], m["frame_1"], m["host_ns_1"], m["recording"]) for m in marks] \
			== [("1", "mark", "", "", "", ""), ("2", "9", "12", "12", "12001", "")]
	finally:
		viewer.Close()
	# A file of other columns is left as it is
	other = tmp_path / "other.csv"
	other.write_text("a,b\n1,2\n")
	viewer = liveview.Viewer(view_args(prefix, "--bookmarks", str(other)))
	try:
		viewer.Check()
		assert viewer.Mark("mark")["path"] == str(tmp_path / "other_2.csv")
		assert other.read_text() == "a,b\n1,2\n" and len(bookmarks(tmp_path / "other_2.csv")) == 1
	finally:
		viewer.Close()


def test_buttons_turn_the_predictions_and_reprojections_on_and_off():
	# Clicked in the panel, the buttons do what d, p and l do, and show it
	cams, truth, keypoints, scores, order = mice()
	viewer = liveview.Viewer(view_args(name()))
	viewer.description = {"cameras": ["Camera0", "Camera1", "Camera2"], "edges": EDGES, "animals": 3}
	viewer.calibration = cams
	frames = [types.SimpleNamespace(image=np.zeros((1024, 1280, 1), np.uint8), frameNumber=5, hostTime=0)
		for _ in range(3)]
	row = row_for(5, cams, truth, keypoints, scores, order)
	viewer.lastSeen = 30.0
	image = viewer.Compose(frames, row)
	keys = [b[4] for b in viewer.buttons]
	assert keys == [ord("d"), ord("p"), ord("l")]
	def Centre(key):
		x0, y0, x1, y1, _ = next(b for b in viewer.buttons if b[4] == key)
		return (x0 + x1) // 2, (y0 + y1) // 2
	on = image[Centre(ord("d"))[1], Centre(ord("d"))[0] + 60].copy()
	assert viewer.Click(*Centre(ord("d"))) == ord("d") and viewer.Click(*Centre(ord("p"))) == ord("p")
	assert viewer.Click(5, 5) is None # a camera's tile
	def tiles(image): # the identities' colours on the cameras' frames, not in the panel
		y, x, h, w = viewer.layout.panel
		image = image.copy()
		image[y:y + h, x:x + w] = 0
		return sum(like(image, liveview.Color(a)).sum() for a in range(3))
	both = tiles(image)
	viewer.Key(viewer.Click(*Centre(ord("p"))))
	assert not viewer.show3d and viewer.show2d
	image = viewer.Compose(frames, row)
	off = image[Centre(ord("p"))[1], Centre(ord("p"))[0] + 60]
	assert tuple(on) != tuple(off) # drawn as off
	predictions = tiles(image)
	viewer.Key(viewer.Click(*Centre(ord("d"))))
	image = viewer.Compose(frames, row)
	assert 0 < predictions < both and tiles(image) == 0 # neither: nothing of the animals drawn
	viewer.Key(viewer.Click(*Centre(ord("p"))))
	assert 0 < tiles(viewer.Compose(frames, row)) < both # the reprojections alone


# -------------------------------------------------------------- the 3D view

TILE = (480, 600) # height, width


def posed(points, animals=None):
	# A row whose animals' 3D is points (animals, nodes, 3), from 3 cameras each
	points = np.asarray(points, float)
	n, N = points.shape[:2]
	row = np.zeros((), live.RowType(3, n, N, n))
	row["points3d"] = points
	row["n_views"] = 3
	row["reproj_px"] = 0.5
	row["members"] = -1
	return row


def bright(img, xy, r=3):
	# Whether a light gray (the cameras' frustums) is among the pixels within r of xy
	x, y = int(round(xy[0])), int(round(xy[1]))
	patch = img[max(0, y - r):y + r + 1, max(0, x - r):x + r + 1].astype(int)
	return bool(((patch.min(-1) > 150) & (patch.max(-1) - patch.min(-1) < 20)).any())


def test_up_is_away_from_where_the_cameras_look_then_level_with_the_animals():
	# The test rig's floor is z = 0. The cameras alone give about up; the
	# animals going about on the floor, up and the floor themselves
	cams = scene3d.rig()
	scene = liveview.Scene(cams)
	assert np.degrees(np.arccos(scene.up @ [0, 0, 1])) < 30
	assert np.cross(scene.e1, scene.e2) @ scene.up == pytest.approx(1) # right-handed
	truth = scene3d.scene(900, n=3)
	for points in truth:
		scene.Learn(posed(points))
	assert np.degrees(np.arccos(scene.up @ [0, 0, 1])) < 1
	assert 0 <= scene.Ground()[2] <= 10 # the lowest nodes are 5 mm up
	assert np.linalg.norm(scene.Ground()[:2]) < 120 # the arena's middle
	# Badly triangulated nodes (from 1 camera, or far off in the cameras) do not count
	lone = liveview.Scene(cams)
	for points in truth[:300]:
		row = posed(points - [0, 0, 400])
		row["reproj_px"] = 50
		lone.Learn(row)
	assert lone.floor == 0 and lone.up @ liveview.Up(cams) == pytest.approx(1)
	# Told up: it stays
	told = liveview.Scene(cams, up=(0, 0.2, 1))
	for points in truth[:300]:
		told.Learn(posed(points))
	assert told.up == pytest.approx(liveview.Unit((0, 0.2, 1)))


def test_a_3d_point_lands_where_the_virtual_camera_puts_it():
	# Seen level (elevation 0): what it orbits in the tile's middle, a point
	# beside it (e2) to the right, one above it (up) higher, each f * 50 /
	# distance px off, f from the field of view
	scene = liveview.Scene(scene3d.rig())
	scene.elevation = 0.0
	c, d = scene.centre, scene.distance
	f = 0.5 * TILE[1] / np.tan(np.radians(liveview.FOV) / 2)
	points = [[c], [c + 50 * scene.e2], [c + 50 * scene.up]]
	img = np.zeros(TILE + (3,), np.uint8)
	scene.Draw(img, posed(points))
	where = [(300, 240), (300 + f * 50 / d, 240), (300, 240 - f * 50 / d)]
	for a, xy in enumerate(where):
		assert scene.Project(points[a][0], TILE) == pytest.approx(xy)
		assert near(img, xy, liveview.Color(a), 1)
	assert where[1][0] - 300 > 20 # apart enough to tell
	# Behind the virtual camera: not drawn
	_, eye = scene.Eye()
	behind = eye + (eye - c)
	assert np.isnan(scene.Project(behind, TILE)).all()
	img[:] = 0
	scene.Draw(img, posed([[behind]]))
	assert not like(img, liveview.Color(0)).any()


def test_nearer_is_drawn_over_farther():
	scene = liveview.Scene(scene3d.rig())
	R, eye = scene.Eye()
	near_, far_ = eye + R[2] * scene.distance * 0.5, eye + R[2] * scene.distance * 1.5 # on its axis
	for first, second in ((near_, far_), (far_, near_)):
		img = np.zeros(TILE + (3,), np.uint8)
		scene.Draw(img, posed([[first], [second]]))
		nearer = 0 if first is near_ else 1
		assert like(img[240:241, 300:301], liveview.Color(nearer)).all()


def test_orbiting_and_zooming_change_the_view():
	cams, truth, keypoints, scores, order = mice()
	scene = liveview.Scene(cams)
	row = posed(truth)
	def view():
		img = np.zeros(TILE + (3,), np.uint8)
		scene.Draw(img, row, EDGES)
		return img
	before, where = view(), scene.Project(truth, TILE)
	scene.Orbit(100, 0) # dragged 100 px right
	turned = view()
	assert (turned != before).any()
	moved = np.linalg.norm(scene.Project(truth, TILE) - where, axis=-1)
	assert np.median(moved) > 20
	for a in range(3): # still there, where it now lands
		assert share(near(turned, p, liveview.Color(a), 1) for p in scene.Project(truth[a], TILE)) > 0.8
	scene.Orbit(-100, 0)
	assert (view() == before).all() # back
	# Nearer: everything farther from the middle of the tile, by 1 / 0.85
	off = scene.Project(truth, TILE) - [300, 240]
	scene.Zoom(1)
	assert np.linalg.norm(scene.Project(truth, TILE) - [300, 240], axis=-1).mean() > \
		1.1 * np.linalg.norm(off, axis=-1).mean()
	# Not over the top
	scene.Orbit(0, 1e5)
	assert scene.elevation == pytest.approx(np.radians(85))
	scene.Reset()
	assert (view() == before).all()


def test_the_cameras_are_drawn_where_they_are():
	cams = scene3d.rig()
	scene = liveview.Scene(cams)
	scene.Zoom(-6) # out, to see them above the cage
	img = scene.Background(TILE)
	for cam in cams:
		at = scene.Project(cam.center, TILE)
		assert 0 <= at[0] < TILE[1] and 0 <= at[1] < TILE[0] and bright(img, at, 2)
		# Its frustum points where it looks: its image's corners, in front of it
		w, h = 2 * cam.c
		for u, v in ((0, 0), (w, 0), (w, h), (0, h)):
			ray = np.array([(u - cam.c[0]) / cam.f[0], (v - cam.c[1]) / cam.f[1], 1.0]) * 0.12 * scene.reach
			assert bright(img, scene.Project(cam.center + cam.R.T @ ray, TILE), 2)
	assert scene.Background(TILE) is img # drawn once for each view of them
	scene.Orbit(10, 0)
	assert scene.Background(TILE) is not img


def three_cameras(cams, row):
	viewer = liveview.Viewer(view_args(name()))
	viewer.description = {"cameras": ["Camera0", "Camera1", "Camera2"], "edges": EDGES, "animals": 3}
	viewer.calibration = cams
	viewer.scene = liveview.Scene(cams)
	viewer.lastSeen = 30.0
	frames = [types.SimpleNamespace(image=np.zeros((1024, 1280, 1), np.uint8), frameNumber=5, hostTime=0)
		for _ in range(3)]
	return viewer, frames


def test_the_3d_tile_shows_the_row_the_cameras_show():
	cams, truth, keypoints, scores, order = mice()
	row = row_for(5, cams, truth, keypoints, scores, order)
	viewer, frames = three_cameras(cams, row)
	image = viewer.Compose(frames, row)
	# Beside the 3 cameras, as large as without it, the panel in the grid
	assert (viewer.layout.cols, viewer.layout.rows, viewer.layout.inGrid) == (3, 2, True)
	assert viewer.layout.tile == liveview.Layout(3, (1024, 1280), (1800, 960)).tile
	y, x, h, w = viewer.sceneCell
	assert (y, x) == viewer.layout.Cell(3) and (h, w) == viewer.layout.tile
	def drawn(row): # the scene's drawing of row, below the tile's header
		img = np.zeros((h, w, 3), np.uint8)
		viewer.scene.Draw(img, row, EDGES)
		return img[50:]
	tile = image[y:y + h, x:x + w]
	assert (tile[50:] == drawn(row)).all() and sum(like(tile, liveview.Color(a)).sum() > 50 for a in range(3)) == 3
	# Another row: its animals, not the last ones
	other = row_for(6, cams, truth + [0, 0, 60], keypoints, scores, order)
	tile = viewer.Compose(frames, other)[y:y + h, x:x + w]
	assert (tile[50:] == drawn(other)).all() and (tile[50:] != drawn(row)).any()
	# \\ (or its button) hides it: the cameras alone, and no 3D drawn
	keys = [b[4] for b in viewer.buttons]
	assert keys == [ord("d"), ord("p"), ord("l"), ord("\\")]
	x0, y0, x1, y1, _ = viewer.buttons[3]
	viewer.Key(viewer.Mouse(liveview.cv2.EVENT_LBUTTONDOWN, (x0 + x1) // 2, (y0 + y1) // 2, 0))
	assert not viewer.showScene
	image = viewer.Compose(frames, row)
	assert viewer.sceneCell is None and viewer.layout.cols == 2
	assert viewer.Key(ord("\\")) and viewer.showScene
	# Without a calibration (2D only): no 3D, no button
	plain = liveview.Viewer(view_args(name()))
	plain.description = {"cameras": ["Camera0", "Camera1", "Camera2"], "edges": EDGES}
	plain.lastSeen = 30.0
	plain.Compose(frames, np.zeros((), live.RowType(3, 3, truth.shape[1])))
	assert plain.sceneCell is None and len(plain.buttons) == 3


def test_dragging_in_the_3d_tile_turns_it():
	cv2_ = liveview.cv2
	cams, truth, keypoints, scores, order = mice()
	row = row_for(5, cams, truth, keypoints, scores, order)
	viewer, frames = three_cameras(cams, row)
	image = viewer.Compose(frames, row)
	y, x, h, w = viewer.sceneCell
	at = (x + w // 2, y + h // 2)
	scene = viewer.scene
	# On a camera's tile: nothing turns
	assert viewer.Mouse(cv2_.EVENT_LBUTTONDOWN, 10, 10, 0) is None and viewer.drag is None
	# Dragged 60 px to the right in the 3D tile
	assert viewer.Mouse(cv2_.EVENT_LBUTTONDOWN, *at, 0) is None
	turn = scene.azimuth
	viewer.Mouse(cv2_.EVENT_MOUSEMOVE, at[0] + 60, at[1], cv2_.EVENT_FLAG_LBUTTON)
	assert scene.azimuth == pytest.approx(turn - 60 * 0.008) and viewer.moved
	viewer.Mouse(cv2_.EVENT_LBUTTONUP, at[0] + 60, at[1], 0)
	viewer.Mouse(cv2_.EVENT_MOUSEMOVE, at[0] + 200, at[1], 0)
	assert viewer.drag is None and scene.azimuth == pytest.approx(turn - 60 * 0.008)
	# Drawn again as Run does: the 3D tile changes, the cameras' do not
	before = image.copy()
	viewer.Draw3D(image, viewer.last[1], 5)
	assert not viewer.moved
	assert (image[y:y + h, x:x + w] != before[y:y + h, x:x + w]).any()
	th, tw = viewer.layout.tile
	assert (image[:th, :3 * tw][:, :x] == before[:th, :3 * tw][:, :x]).all()
	# The wheel, with the mouse over the 3D tile (its x, y are the screen's on Windows): nearer
	distance = scene.distance
	viewer.Mouse(cv2_.EVENT_MOUSEMOVE, *at, 0)
	viewer.Mouse(cv2_.EVENT_MOUSEWHEEL, 5000, 5000, 120 << 16)
	assert scene.distance == pytest.approx(distance * 0.85)
	viewer.Mouse(cv2_.EVENT_MOUSEMOVE, 10, 10, 0) # over a camera: not
	viewer.Mouse(cv2_.EVENT_MOUSEWHEEL, 5000, 5000, 120 << 16)
	assert scene.distance == pytest.approx(distance * 0.85)
	viewer.Mouse(cv2_.EVENT_MOUSEMOVE, *at, 0) # the wheel the other way, with ctrl held: farther
	viewer.Mouse(cv2_.EVENT_MOUSEWHEEL, 5000, 5000, (-120 << 16) | cv2_.EVENT_FLAG_CTRLKEY)
	assert scene.distance == pytest.approx(distance)
	# The right button moves it; a double click (or r) puts it back
	centre = scene.centre.copy()
	viewer.Mouse(cv2_.EVENT_RBUTTONDOWN, *at, 0)
	viewer.Mouse(cv2_.EVENT_MOUSEMOVE, at[0], at[1] + 40, cv2_.EVENT_FLAG_RBUTTON)
	viewer.Mouse(cv2_.EVENT_RBUTTONUP, at[0], at[1] + 40, 0)
	assert np.linalg.norm(scene.centre - centre) > 1
	viewer.Mouse(cv2_.EVENT_LBUTTONDBLCLK, *at, 0)
	assert scene.azimuth == 0 and scene.centre == pytest.approx(centre)
	scene.Orbit(30, 0)
	viewer.Key(ord("r"))
	assert scene.azimuth == 0
	# Hidden (\\) while dragged: the drag ends
	viewer.Mouse(cv2_.EVENT_LBUTTONDOWN, *at, 0)
	viewer.Key(ord("\\"))
	viewer.Compose(frames, row)
	viewer.Mouse(cv2_.EVENT_MOUSEMOVE, at[0] + 30, at[1], cv2_.EVENT_FLAG_LBUTTON)
	assert viewer.drag is None and scene.azimuth == 0

def test_luc3d_keys_switch_between_the_grid_and_one_camera():
	# As LUC3D: v one camera alone (the one last picked), g the grid, the
	# arrows the previous or next camera (wrapping), \\ the 3D on or off; the
	# 3D stays beside a single camera
	cams, truth, keypoints, scores, order = mice()
	row = row_for(5, cams, truth, keypoints, scores, order)
	viewer, frames = three_cameras(cams, row)
	def view():
		shown = viewer.Shown()
		return viewer.Compose([frames[c] for c in shown], row)
	def picked(image): # the camera whose tile has the outline
		return [c for c, y, x, h, w in viewer.cells if like(image[y + 1:y + 3, x + 20:x + w - 20], liveview.PICKED).all()]
	image = view()
	assert viewer.Shown() == [0, 1, 2] and picked(image) == [0]
	assert viewer.Key("down") and viewer.Shown() == [0, 1, 2] and picked(view()) == [1] # in the grid: which v shows
	assert viewer.Key(ord("v")) and viewer.Shown() == [1]
	view()
	assert (viewer.layout.cols, viewer.layout.rows) == (2, 1) and len(viewer.cells) == 1 # it and the 3D, side by side
	assert viewer.sceneCell is not None # the 3D stays up
	steps = []
	for key in ("down", "down", "up", "left", "right", "right"):
		viewer.Key(key)
		steps.append(viewer.Shown()[0])
	assert steps == [2, 0, 2, 1, 2, 0] # wrapping
	assert viewer.Key(ord("v")) and viewer.Shown() == [0] # already alone: stays
	assert viewer.Key(ord("\\")) and viewer.Shown() == [0]
	view()
	assert viewer.sceneCell is None # closed with \\
	viewer.Key(ord("\\"))
	viewer.Key("right")
	assert viewer.Key(ord("g")) and viewer.Shown() == [0, 1, 2]
	assert picked(view()) == [1] # the one last shown alone
	# Clicks: on a camera's tile picks it; a double click shows it alone, another the grid again
	c, y, x, h, w = viewer.cells[2]
	viewer.Press(x + w // 2, y + h // 2)
	assert viewer.current == 2 and viewer.Shown() == [0, 1, 2]
	c, y, x, h, w = viewer.cells[0]
	viewer.DoubleClick(x + w // 2, y + h // 2)
	assert viewer.Shown() == [0]
	view()
	c, y, x, h, w = viewer.cells[0]
	viewer.DoubleClick(x + w // 2, y + h // 2)
	assert viewer.Shown() == [0, 1, 2]
	assert any("v one camera" in text for text, color in viewer.PanelText(row))

def test_the_view_is_drawn_at_its_windows_size():
	# Windows' OpenCV stretches an image to its window: the view is drawn at
	# the window's own size (what the screen let it have), its frames in
	# their ratio, black around them; z sizes the window to the view
	cams, truth, keypoints, scores, order = mice()
	row = row_for(5, cams, truth, keypoints, scores, order)
	viewer, frames = three_cameras(cams, row)
	sizes = {}
	for size in ((1028, 749), (1800, 960), (2400, 1300)):
		viewer.windowSize = size
		image = viewer.Compose(frames, row)
		assert image.shape == (size[1], size[0], 3)
		th, tw = viewer.layout.tile
		assert abs(th / tw - 1024 / 1280) < 0.01 and viewer.layout.size[0] <= size[1] and viewer.layout.size[1] <= size[0]
		sizes[size] = tw
		# The view in the middle, black around it
		h, w = viewer.layout.size
		oy, ox = viewer.origin
		assert (oy, ox) == ((size[1] - h) // 2, (size[0] - w) // 2)
		inside = np.zeros(image.shape[:2], bool)
		inside[oy:oy + h, ox:ox + w] = True
		assert not image[~inside].any() and image[inside].any()
		c, y, x, th_, tw_ = viewer.cells[0] # where the tiles are: in the window's image
		assert (y, x) == (oy, ox)
	assert sizes[(1028, 749)] < sizes[(1800, 960)] < sizes[(2400, 1300)]
	viewer.windowSize = (9000, 6000) # frames never drawn larger than they are
	viewer.Compose(frames, row)
	assert viewer.layout.scale == (1.0, 1.0)
	viewer.windowSize = (1800, 1000)
	viewer.Compose(frames, row)
	w, h = viewer.Fit()
	assert (h, w) == viewer.layout.size and h < 1000
	viewer.windowSize = (w, h)
	assert viewer.Compose(frames, row).shape == (h, w, 3)
	# Waiting for a feed: the window's size too
	viewer.windowSize = (900, 500)
	assert viewer.Waiting().shape == (500, 900, 3)
	viewer.windowSize = None # no window: --size
	assert viewer.Compose(frames, row).shape[:2] == viewer.layout.size

# -------------------------------------------------------------- with campy-live

# -------------------------------------------------------------- rig health and tracking quality

def test_an_identity_with_a_high_error_is_drawn_dimmed():
	# Its 3D (from a false detection outside the cage, say) is not to trust: dimmed and dashed, its label red
	cams, truth, keypoints, scores, order = mice()
	row = row_for(1, cams, truth, keypoints, scores, order)
	row["reproj_px"][2] = 234
	c, cam = 0, cams[0]
	img = np.zeros((1024, 1280, 3), np.uint8)
	liveview.DrawCamera(img, 1.0, row, c, cam, EDGES, show2d=False, lines=False, bad_px=20)
	for a in range(3):
		px = cam.Project(truth[a])
		bright = share(near(img, p, liveview.Color(a), 1) for p in px)
		dim = share(near(img, p, liveview.Dimmed(liveview.Color(a)), 1) for p in px)
		if a == 2:
			assert bright < 0.1 and dim > 0.9
		else:
			assert bright > 0.9
	assert like(img, liveview.WARN).sum() > 50 # its label: "#2  bad 3D: 234 px"
	# Dashed: along an edge, gaps between the dashes
	u, v = (cam.Project(truth[2][i]) for i in EDGES[0])
	along = [near(img, u + (v - u) * k / 40, liveview.Dimmed(liveview.Color(2)), 0) for k in range(8, 33)]
	assert 0.2 < share(along) < 0.95
	# With --bad-px 0, none is dimmed
	img[:] = 0
	liveview.DrawCamera(img, 1.0, row, c, cam, EDGES, show2d=False, lines=False, bad_px=0)
	assert share(near(img, p, liveview.Color(2), 1) for p in cam.Project(truth[2])) > 0.9
	assert not like(img, liveview.WARN).any()
	# In the 3D tile too
	scene = liveview.Scene(cams)
	tile = np.zeros(TILE + (3,), np.uint8)
	scene.Draw(tile, row, EDGES, bad_px=20)
	assert like(tile, liveview.Color(2)).sum() < 5 < like(tile, liveview.Dimmed(liveview.Color(2))).sum()
	assert like(tile, liveview.Color(0)).sum() > 50 and like(tile, liveview.WARN).sum() > 50


def paced(writers, frames, fps, host_offset=None, skip=None, step=None):
	# Frames 1..frames into each camera's ring at fps, as campy writes them (host_offset: {camera: ns};
	# skip: (camera, frame) never written), calling step() after each
	period = 1.0 / fps
	start = time.perf_counter()
	for n in range(1, frames + 1):
		while time.perf_counter() < start + n * period:
			time.sleep(0.0005)
		host = time.monotonic_ns()
		for c, w in enumerate(writers):
			if skip != (c, n):
				w.Write(np.full(w.ring.shape[:2], 60, np.uint8), n, host + (host_offset or {}).get(c, 0), n * period)
		if step:
			step()


def tile_of(viewer, image, c):
	_, y, x, h, w = [cell for cell in viewer.cells if cell[0] == c][0]
	return image[y:y + h, x:x + w]


@pytest.mark.parametrize("problem", ["ahead", "gap"])
def test_a_camera_one_frame_off_or_with_a_gap_is_warned_of_on_its_tile(rings, problem):
	# Real rings, written at 100 fps: camera 2 a frame ahead (its host times 10 ms early, as a camera
	# left grabbing after a double Ctrl+C was on 2026-10-20), or camera 1's frame 30 never in its feed
	prefix = name()
	writers = [rings(livefeed.RingName(prefix, c), (64, 80), np.uint8, slots=8, frameRate=100.0,
		cameraName="Camera{}".format(c)) for c in range(3)]
	viewer = liveview.Viewer(view_args(prefix))
	try:
		viewer.Check()
		viewer.lastRow = time.monotonic() - 1 # no results: the frames alone
		paced(writers, 60, 100.0, host_offset={2: -10_000_000} if problem == "ahead" else None,
			skip=(1, 30) if problem == "gap" else None, step=viewer.Step)
		viewer.rig.Sample(viewer.readers)
		image = viewer.Compose(viewer.Read(60), None)
		warned = 2 if problem == "ahead" else 1
		for c in range(3):
			red = like(tile_of(viewer, image, c), liveview.WARN).sum()
			assert (red > 200) if c == warned else (red == 0) # its mark, and a red frame round it
		alerts = [text for text, color in viewer.Alerts()]
		assert len(alerts) == 1 and all(color == liveview.WARN for _, color in viewer.Alerts())
		if problem == "ahead":
			assert alerts[0].startswith("Camera2: 1 frame ahead of the other cameras")
			assert viewer.rig.Status().cameras[2].frames_off == 1
		else:
			assert alerts[0] == "Camera1: 1 frame number missing from its feed in the last 10 s"
		status = viewer.rig.Status()
		assert all(abs(s.fps - 100) < 10 for s in status.cameras)
		summary = viewer.Summary(1.0)
		assert [r["camera"] for r in summary["rig"]] == ["Camera0", "Camera1", "Camera2"]
		assert summary["rig"][1]["gaps"] == (1 if problem == "gap" else 0)
		assert summary["rig_read_ms"]["p50"] < 1
	finally:
		viewer.Close()


def test_the_tracking_strip_shows_each_identitys_last_10_s():
	cams, truth, keypoints, scores, order = mice()
	viewer, frames = three_cameras(cams, None)
	viewer.tracking = liveview.livehealth.Tracking(3, 30.0, 20.0)
	viewer.results, viewer.lastRow = object(), time.monotonic()
	for n in range(1, 301):
		row = row_for(n, cams, truth, keypoints, scores, order)
		row["reproj_px"][1] = 15 # fair
		if n > 150:
			row["reproj_px"][2] = 234 # bad, the last 5 s
		if n <= 150:
			row["n_views"][0] = 2 # from 2 of the 3 cameras, the first 5 s
		viewer.tracking.Add(row)
	strips = viewer.Strips(600)
	step = liveview.STRIP_H + 6
	sw = 600 - liveview.STRIP_LABEL
	def strip(a):
		return strips[a * step + 2:a * step + 2 + liveview.STRIP_H, liveview.STRIP_LABEL:]
	assert like(strip(0), liveview.FINE).all(0)[sw // 2:].all() # all its height: 3 cameras
	assert not like(strip(0)[:3], liveview.FINE)[:, :sw // 2 - 5].any() # lower: 2 cameras
	assert like(strip(1), liveview.FAIR).all()
	assert like(strip(2)[:, sw // 2 + 5:], liveview.WARN).all() and like(strip(2)[:, :sw // 2 - 5], liveview.FINE).all()
	# The warnings, at the top of the panel, in red
	alerts = viewer.Alerts()
	assert [text.split(":")[0] for text, _ in alerts] == ["Good 3D for 2 of 3 animals", "#2's error, 234 px, is far above the others' (8 px)"] # the median of 0.5 and 15
	image = viewer.Compose(frames, row)
	y, x, h, w = viewer.layout.panel
	assert like(image[y:y + h, x:x + w], liveview.WARN).sum() > 500
	assert "above 20 px, drawn dimmed" in " ".join(text for text, _ in viewer.PanelText(row))


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
	assert summary["draw_3d_ms"]["p50"] > 0 # with the 3D tile
	saved = sorted(os.listdir(png))
	assert saved and all(f.startswith("view_") and f.endswith(".png") for f in saved)
	image = cv2.imread(str(png / saved[-1]))
	for a in range(3):
		assert like(image, liveview.Color(a)).sum() > 100
