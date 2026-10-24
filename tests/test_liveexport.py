"""campy-live-export (campy/liveexport.py) on a made-up recording and campy-live log.

Three cameras record campy frames 1..40; Camera2's video lacks frames 21 and 22
(campy's writer dropped them), so from there on its video is 2 frames behind
the others'. campy-live logged the sets of every frame but those divisible by
5 (the ones it skipped), with 3 animals whose true 3D is known.
"""

import csv, json, os

import numpy as np
import pytest

sio = pytest.importorskip("sleap_io")
h5py = pytest.importorskip("h5py")
from scipy.spatial.transform import Rotation

from campy import live, liveexport, pose3d
import test_pose3d as scene3d

CAMERAS = ["Camera0", "Camera1", "Camera2"]
CALIBRATION_NAMES = ["top", "left", "right"]  # the same cameras, as the calibration names them
FRAMES = 40
DROPPED = {"Camera2": (21, 22)}
NODES = ["n{}".format(i) for i in range(15)]
EDGES = [[0, 5], [5, 6], [6, 3], [3, 4]]
ANIMALS = 3
HOST0 = 10 ** 12
PERIOD = 16666667


def host(c, n):
	return HOST0 + n * PERIOD + 1000 * c


def write_calibration(path, cams):
	# anipose's calibration.toml: the rig's cameras, and one more the recording did not use
	with open(path, "w") as f:
		for i, cam in enumerate(cams + [scene3d.look_at("extra", (0, 600, 300))]):
			f.write("[cam_{}]\nname = \"{}\"\nsize = [ 1280, 1024,]\n".format(i, cam.name))
			f.write("matrix = {}\ndistortions = {}\n".format(cam.K.tolist(), cam.dist.tolist()))
			f.write("rotation = {}\ntranslation = {}\n\n".format(Rotation.from_matrix(cam.R).as_rotvec().tolist(),
				cam.t.tolist()))


def write_camera(folder, name, frames):
	d = os.path.join(folder, name)
	os.makedirs(d)
	open(os.path.join(d, "0.mp4"), "wb").close()  # the export only names the video
	c = CAMERAS.index(name)
	np.save(os.path.join(d, "frametimes.npy"), np.array([frames, (np.array(frames) - 1) / 60.0], float))
	np.save(os.path.join(d, "hosttimes.npy"), np.array([host(c, n) for n in frames], np.int64))
	with open(os.path.join(d, "metadata.csv"), "w", newline="") as f:
		csv.writer(f).writerows([["videoFilename", "0.mp4"], ["cameraName", name]])


def make_rows(cams, truth, with3d=True, seed=0):
	rng = np.random.default_rng(seed)
	sets = [n for n in range(1, FRAMES + 1) if n % 5]
	rows = np.zeros(len(sets), live.RowType(3, 5, len(NODES), ANIMALS if with3d else 0))
	for r, n in enumerate(sets):
		row = rows[r]
		row["frame"] = n
		row["host_ns"] = [host(c, n) for c in range(3)]
		for key in ("centroids", "centroid_vals", "keypoints", "peak_vals"):
			row[key] = np.nan
		for c, cam in enumerate(cams):
			order = rng.permutation(ANIMALS)  # the detections come in any order
			for slot, k in enumerate(order):
				row["keypoints"][c, slot] = cam.Project(truth[n - 1, k])
				row["peak_vals"][c, slot] = 0.9
				row["centroids"][c, slot] = row["keypoints"][c, slot, 6]
				row["centroid_vals"][c, slot] = 0.8
				if with3d:
					row["members"][k, c] = slot
			row["found"][c] = ANIMALS
		if with3d:
			row["points3d"] = truth[n - 1]
			row["n_views"] = 3
	if with3d:
		# One more detection in Camera0 of frame 3, matched to no animal
		rows[2]["keypoints"][0, 3] = [100.0, 100.0]
		rows[2]["found"][0] = 4
	return rows


@pytest.fixture
def recording(tmp_path):
	cams = [scene3d.look_at(n, *where) for n, where in zip(CALIBRATION_NAMES,
		[((0, 30, 700), (0, 0, 0), (0, 1, 0)), ((-550, -300, 450),), ((550, -300, 450),)])]
	rec = tmp_path / "2026-10-22_15-46-09"
	for name in CAMERAS:
		write_camera(str(rec), name, [n for n in range(1, FRAMES + 1) if n not in DROPPED.get(name, ())])
	write_calibration(str(tmp_path / "calibration.toml"), cams)
	truth = scene3d.scene(FRAMES, n=ANIMALS)
	log = str(tmp_path / "campy-live_test")
	np.save(log + ".npy", make_rows(cams, truth))
	with open(log + ".json", "w") as f:
		json.dump({"cameras": CAMERAS, "nodes": NODES, "edges": EDGES, "pose3d": "3 animals, cameras ...",
			"calibration": str(tmp_path / "calibration.toml"), "calibration_names": CALIBRATION_NAMES,
			"animals": ANIMALS}, f)
	return str(rec), log + ".npy", truth


def stored_videos(path):
	# The video paths as the .slp file holds them (sleap-io resolves them as it loads)
	with h5py.File(path, "r") as f:
		return [json.loads(v)["backend"]["filename"] for v in f["videos_json"][:]]


def video_index(name, n):
	# The video frame holding campy frame n, in camera `name`'s video
	dropped = DROPPED.get(name, ())
	return None if n in dropped else n - 1 - sum(d < n for d in dropped)


def test_each_cameras_2d_is_on_the_video_frame_that_holds_it(recording):
	rec, log, _ = recording
	liveexport.Export(rec, log)
	rows = np.load(log)
	labels = sio.load_slp(os.path.join(rec, "campy-live.slp"))
	# Relative to the .slp: the recording's folder can move
	assert stored_videos(os.path.join(rec, "campy-live.slp")) == [n + "/0.mp4" for n in CAMERAS]
	for c, name in enumerate(CAMERAS):
		own = sio.load_slp(os.path.join(rec, name, "campy-live.slp"))
		assert stored_videos(os.path.join(rec, name, "campy-live.slp")) == ["0.mp4"]  # next to it
		for source in (labels, own):
			video = source.videos[c if source is labels else 0]
			got = {lf.frame_idx: np.stack([i.numpy() for i in lf.instances]) for lf in source.find(video)}
			want = {}
			for row in rows:
				v = video_index(name, int(row["frame"]))
				if v is not None:
					found = ~np.isnan(row["keypoints"][c]).all(axis=(1, 2))
					want[v] = row["keypoints"][c][found]
			assert sorted(got) == sorted(want)
			for v in want:
				assert np.allclose(got[v], want[v], atol=1e-4)
	# Camera2's video lacks frames 21 and 22: those 2 sets have no Camera2 2D
	assert len(own.labeled_frames) == len(rows) - 2


def test_each_animal_is_a_group_with_its_identity_its_3d_and_its_2d(recording):
	rec, log, truth = recording
	liveexport.Export(rec, log)
	labels = sio.load_slp(os.path.join(rec, "campy-live.slp"))
	session = labels.sessions[0]
	assert [c.name for c in session.camera_group.cameras] == CAMERAS  # renamed to campy's names
	assert [i.name for i in labels.identities] == ["animal 0", "animal 1", "animal 2"]
	assert [t.name for t in labels.tracks] == ["animal 0", "animal 1", "animal 2"]
	rows = np.load(log)
	assert sorted(session.frame_groups) == sorted(video_index("Camera0", int(n)) for n in rows["frame"])
	for n in rows["frame"]:
		f = video_index("Camera0", int(n))
		groups = session.frame_groups[f].instance_groups
		assert [g.metadata["lucid"]["identityId"] for g in groups] == [0, 1, 2]
		for k, g in enumerate(groups):
			assert g.identity.name == "animal {}".format(k)
			assert np.allclose(g.points, truth[n - 1, k], atol=1e-3)
			# Its 2D: the detection of that animal, in each camera whose video has this frame here.
			# Camera2's video is behind from frame 21 on, so its 2D are on other frames: left out.
			assert [c.name for c in g.cameras] == (CAMERAS if n < 21 else CAMERAS[:2])
			assert all(inst.track.name == "animal {}".format(k) for inst in g.instances)
	# The detection matched to no animal is there, without a track
	frame3 = [lf for lf in labels.find(labels.videos[0]) if lf.frame_idx == 2][0]
	assert len(frame3.instances) == 4 and sum(i.track is None for i in frame3.instances) == 1


def test_the_group_2d_is_the_animals_own(recording):
	# Each group's 2D, projected from its 3D, lands where the camera saw that animal
	rec, log, truth = recording
	liveexport.Export(rec, log)
	labels = sio.load_slp(os.path.join(rec, "campy-live.slp"))
	calib = pose3d.LoadCalibration(os.path.join(rec, "calibration.toml"))
	for f, fg in labels.sessions[0].frame_groups.items():
		for g in fg.instance_groups:
			for cam, inst in g.instance_by_camera.items():
				c = CAMERAS.index(cam.name)
				assert np.allclose(inst.numpy(), calib[c].Project(g.points), atol=1e-2)


def test_points3d_h5_has_the_3d_by_video_frame(recording):
	rec, log, truth = recording
	liveexport.Export(rec, log)
	rows = np.load(log)
	with h5py.File(os.path.join(rec, "points3d.h5"), "r") as f:
		assert f["points_3d"].shape == (len(rows), ANIMALS, len(NODES), 3)
		assert list(f["campy_frames"][:]) == list(rows["frame"])
		assert list(f["frame_indices"][:]) == [video_index("Camera0", int(n)) for n in rows["frame"]]
		assert [s.decode() for s in f["node_names"][:]] == NODES
		assert [s.decode() for s in f["track_names"][:]] == ["animal 0", "animal 1", "animal 2"]
		assert np.allclose(f["points_3d"][:], truth[rows["frame"] - 1], atol=1e-3)


def test_calibration_toml_has_the_recorded_cameras_under_campys_names(recording, tmp_path):
	rec, log, _ = recording
	liveexport.Export(rec, log)
	got = pose3d.LoadCalibration(os.path.join(rec, "calibration.toml"))
	want = pose3d.LoadCalibration(str(tmp_path / "calibration.toml"), names=CALIBRATION_NAMES)
	assert [c.name for c in got] == CAMERAS  # LUC3D finds each camera's folder by these
	for a, b in zip(got, want):
		assert np.allclose(a.K, b.K) and np.allclose(a.R, b.R) and np.allclose(a.t, b.t)
		assert np.allclose(a.dist, b.dist) and list(a.size) == [1280, 1024]


def test_skeleton_json_has_the_nodes_and_edges(recording):
	# SLEAP's jsonpickle node-link format: the links name the nodes, which are
	# numbered 1, 2, ... in the order they first appear there (py/id), and the
	# "nodes" list gives their order by those numbers. That order is the
	# keypoints' order (LUC3D and SLEAP read it so; sleap-io's own reader
	# keeps the links' order instead).
	rec, log, _ = recording
	liveexport.Export(rec, log)
	with open(os.path.join(rec, "skeleton.json")) as f:
		data = json.load(f)
	by_id, edges = [], []
	for link in data["links"]:
		pair = []
		for end in ("source", "target"):
			ref = link[end]
			if "py/state" in ref:
				name = ref["py/state"]["py/tuple"][0]
				if name not in by_id:
					by_id.append(name)
				pair.append(name)
			else:
				pair.append(by_id[ref["py/id"] - 1])
		edges.append(pair)
	nodes = [by_id[n["id"]["py/id"] - 1] if "py/id" in n["id"] else n["id"]["py/state"]["py/tuple"][0]
		for n in data["nodes"]]
	assert nodes == NODES
	assert sorted(edges) == sorted([NODES[a], NODES[b]] for a, b in EDGES)


def test_a_log_of_another_recording_is_refused(recording):
	rec, log, _ = recording
	rows = np.load(log)
	rows["host_ns"] += 5 * PERIOD  # the same frame numbers, another recording's clock
	np.save(log, rows)
	with pytest.raises(liveexport.ExportError, match="not this recording's"):
		liveexport.Export(rec, log)
	assert not os.path.exists(os.path.join(rec, "campy-live.slp"))


def test_files_are_replaced_only_with_force(recording):
	rec, log, _ = recording
	liveexport.Export(rec, log)
	with pytest.raises(liveexport.ExportError, match="already there"):
		liveexport.Export(rec, log)
	liveexport.Export(rec, log, force=True)
	assert sio.load_slp(os.path.join(rec, "campy-live.slp")).sessions


def test_an_older_summary_gives_its_calibration_in_text_and_the_model_gives_the_edges(recording, tmp_path):
	# campy 2.1.0rc6 and before: no edges, calibration or animals in the summary
	rec, log, _ = recording
	calibration = str(tmp_path / "calibration.toml")
	with open(log[:-4] + ".json", "w") as f:
		json.dump({"cameras": CAMERAS, "nodes": NODES, "pose3d": "3 animals, cameras {} of {}".format(
			", ".join(CALIBRATION_NAMES), calibration)}, f)
	model = tmp_path / "model"
	model.mkdir()
	with open(model / "training_config.yaml", "w") as f:
		f.write("data_config:\n  skeletons:\n  - nodes:\n")
		f.writelines("    - name: {}\n".format(n) for n in NODES)
		f.write("    edges:\n")
		f.writelines("    - source:\n        name: {}\n      destination:\n        name: {}\n".format(
			NODES[a], NODES[b]) for a, b in EDGES)
	liveexport.Export(rec, log, skeleton=str(model))
	sk = sio.load_slp(os.path.join(rec, "campy-live.slp")).skeleton
	assert sorted(map(list, sk.edge_inds)) == sorted(EDGES)
	assert [c.name for c in pose3d.LoadCalibration(os.path.join(rec, "calibration.toml"))] == CAMERAS


def test_a_2d_only_log_writes_the_2d(tmp_path):
	cams = scene3d.rig()
	rec = tmp_path / "rec"
	for name in CAMERAS:
		write_camera(str(rec), name, list(range(1, FRAMES + 1)))
	log = str(tmp_path / "live2d")
	np.save(log + ".npy", make_rows(cams, scene3d.scene(FRAMES, n=ANIMALS), with3d=False))
	with open(log + ".json", "w") as f:
		json.dump({"cameras": CAMERAS, "nodes": NODES, "edges": EDGES, "pose3d": None, "calibration": None,
			"calibration_names": None, "animals": 0}, f)
	out = liveexport.Export(str(rec), log + ".npy")
	labels = sio.load_slp(out["slp"])
	assert not labels.sessions and not labels.tracks
	assert len(labels.labeled_frames) == 3 * len(np.load(log + ".npy"))
	for name in ("calibration.toml", "points3d.h5"):
		assert not os.path.exists(os.path.join(str(rec), name))
	assert os.path.exists(os.path.join(str(rec), "Camera1", "campy-live.slp"))


def test_frames_writes_a_part_on_the_full_videos_frame_numbers(recording):
	rec, log, _ = recording
	assert liveexport.Main([rec, "--log", log, "--frames", "11:19"]) == 0
	labels = sio.load_slp(os.path.join(rec, "campy-live.slp"))
	assert sorted(labels.sessions[0].frame_groups) == [10, 11, 12, 13, 15, 16, 17, 18]  # campy frame 15 was skipped
	assert sorted({lf.frame_idx for lf in labels.labeled_frames}) == [10, 11, 12, 13, 15, 16, 17, 18]


def test_main_says_how_to_open_it(recording, capsys):
	rec, log, _ = recording
	assert liveexport.Main([rec, "--log", log]) == 0
	said = capsys.readouterr().out
	assert "File > Load SLP" in said and "File > Load Session Folder" in said
	# Camera2's video lacks 2 of the logged frames, and is behind the others from there on
	assert "Camera2: 2 of 32 sets are frames its video lacks" in said
	assert "Camera2: its video is behind the others' from video frame 20 (campy frame 23)" in said
	assert liveexport.Main([rec, "--log", log]) == 1  # not again without --force
	assert "already there" in capsys.readouterr().err


def test_a_log_out_of_order_is_refused(recording):
	rec, log, _ = recording
	rows = np.load(log)
	rows[[3, 4]] = rows[[4, 3]]
	np.save(log, rows)
	with pytest.raises(liveexport.ExportError, match="after frame"):
		liveexport.Export(rec, log)


def tables(path):
	# The .slp's tables, as numpy arrays, by name
	out = {}
	with h5py.File(path, "r") as f:
		f.visititems(lambda name, d: out.__setitem__(name, d[()]) if isinstance(d, h5py.Dataset) else None)
		out["format_id"] = f["metadata"].attrs["format_id"]
	return out


def text(array):
	# A dataset of strings as a list of str, whether stored fixed-width or variable-length
	return [x.decode() if isinstance(x, bytes) else str(x) for x in np.asarray(array).ravel()]


@pytest.mark.parametrize("which", ["campy-live.slp", "Camera2/campy-live.slp"])
def test_sleap_io_would_write_the_same_files(recording, tmp_path, which):
	# campy-live-export writes the big tables itself; sleap-io, given what it
	# reads from them, writes them back the same, value for value
	rec, log, _ = recording
	liveexport.Export(rec, log)
	ours = os.path.join(rec, which)
	again = str(tmp_path / "again.slp")
	sio.save_slp(sio.load_slp(ours), again, verbose=False)
	a, b = tables(ours), tables(again)
	big = ["frames", "instances", "pred_points", "points", "format_id"] + [n for n in a if n.startswith("session_data/")]
	if which == "campy-live.slp":
		assert "session_data/frame_groups" in big
		big += ["sessions_json", "tracks_json", "identity/name"]
	for name in big:
		if a[name].dtype.kind in "SOU" and b[name].dtype.kind in "SOU":
			# Rows of JSON text: sleap-io 0.9.3 and older store them zero-padded to
			# a fixed width, 0.9.4 as variable-length strings (the same SLP 2.8
			# format), so the text is compared, not how wide it is stored
			assert a[name].shape == b[name].shape and text(a[name]) == text(b[name]), name
			continue
		assert a[name].dtype == b[name].dtype and a[name].shape == b[name].shape, name
		if a[name].dtype.names:
			for field in a[name].dtype.names:
				assert np.array_equal(a[name][field], b[name][field], equal_nan=a[name][field].dtype.kind == "f"), \
					(name, field)
		else:
			assert np.array_equal(a[name], b[name], equal_nan=a[name].dtype.kind == "f"), name


def big_recording(tmp_path, frames):
	# A recording of `frames` frames and campy-live's log of all of them, made
	# up fast: 3 animals seen by every camera, with their 3D
	rec = tmp_path / "big{}".format(frames)
	numbers = list(range(1, frames + 1))
	for name in CAMERAS:
		write_camera(str(rec), name, numbers)
	rng = np.random.default_rng(1)
	rows = np.zeros(frames, live.RowType(3, 5, len(NODES), ANIMALS))
	rows["frame"] = numbers
	rows["host_ns"] = [[host(c, n) for c in range(3)] for n in numbers]
	for key in ("centroids", "centroid_vals", "keypoints", "peak_vals", "points3d"):
		rows[key] = np.nan
	rows["keypoints"][:, :, :ANIMALS] = rng.uniform(0, 1000, (frames, 3, ANIMALS, len(NODES), 2))
	rows["peak_vals"][:, :, :ANIMALS] = 0.9
	rows["centroid_vals"][:, :, :ANIMALS] = 0.8
	rows["found"] = ANIMALS
	rows["members"] = np.arange(ANIMALS)[None, :, None]
	rows["points3d"] = rng.uniform(-200, 200, (frames, ANIMALS, len(NODES), 3))
	write_calibration(str(tmp_path / "calibration.toml"), scene3d.rig())
	log = str(tmp_path / "big{}".format(frames))
	np.save(log + ".npy", rows)
	with open(log + ".json", "w") as f:
		json.dump({"cameras": CAMERAS, "nodes": NODES, "edges": EDGES, "calibration": str(tmp_path / "calibration.toml"),
			"calibration_names": CALIBRATION_NAMES, "animals": ANIMALS}, f)
	return str(rec), log + ".npy"


def test_memory_hardly_grows_with_the_recording(tmp_path, monkeypatch):
	# The tables are written a block of the log's rows at a time: an export
	# holds only a few hundred bytes per set for the whole recording (where
	# each frame and detection goes). sleap-io's own writer held ~100 kB per
	# set (3.6 GB for 10 minutes of 3 cameras and 5 mice).
	import tracemalloc
	monkeypatch.setattr(liveexport, "BLOCK", 500)
	small, large = big_recording(tmp_path, 2000), big_recording(tmp_path, 8000)
	liveexport.Export(*small) # sleap-io's imports
	peaks = []
	for recording in (small, large):
		tracemalloc.start()
		out = liveexport.Export(*recording, force=True)
		peaks.append(tracemalloc.get_traced_memory()[1])
		tracemalloc.stop()
	assert out["frames_3d"] == 8000 and out["instances"] == 3 * 8000 * ANIMALS
	assert (peaks[1] - peaks[0]) / 6000 < 1000, peaks
	labels = sio.load_slp(out["slp"])
	assert len(labels.labeled_frames) == 3 * 8000 and len(labels.sessions[0].frame_groups) == 8000


def test_an_export_that_fails_leaves_no_files(recording, monkeypatch):
	rec, log, _ = recording
	def fail(*args):
		raise OSError("disk full")
	monkeypatch.setattr(liveexport, "Write3D", fail)
	with pytest.raises(OSError):
		liveexport.Export(rec, log)
	assert not [p for p in os.listdir(rec) if not p.startswith("Camera")]
	assert all(sorted(os.listdir(os.path.join(rec, name))) == ["0.mp4", "frametimes.npy", "hosttimes.npy",
		"metadata.csv"] for name in CAMERAS)
