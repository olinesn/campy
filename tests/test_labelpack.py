"""campy-label-pack (campy/labelpack.py) and campy-label-merge (campy/labelmerge.py)
on a made-up recording, its campy-live log and a hand-made sample.

Three cameras record campy frames 1..40 as small grey videos; Camera2's video
lacks frames 21 and 22 (campy's writer dropped them), so from there on its
video is 2 frames behind the others'. campy-live logged every set, with 3
animals in 3D. The sample picks the sets of frames 5, 21 and 30 (21 can't be
packed: Camera2's video lacks it). Then a labeller "corrects" the package as
LUC3D saves it: one set finished, one half done, one untouched.
"""

import csv, json, os

import numpy as np
import pytest

sio = pytest.importorskip("sleap_io")
h5py = pytest.importorskip("h5py")
from scipy.spatial.transform import Rotation

from campy import labelmerge, labelpack, live
import test_pose3d as scene3d

CAMERAS = ["Camera0", "Camera1", "Camera2"]
CALIBRATION_NAMES = ["top", "left", "right"]
FRAMES, ANIMALS, NODES = 40, 3, 12
NODE_NAMES = ["n{}".format(i) for i in range(NODES)]
DROPPED = {"Camera2": (21, 22)}
H, W = 64, 80
PICKS = (5, 21, 30)
HOST0, PERIOD = 10 ** 12, 16666667


def host(c, n):
	return HOST0 + n * PERIOD + 1000 * c


def picture(c, n):
	# A frame whose pixels say its camera and campy frame, so a wrong frame shows
	img = np.zeros((H, W), np.uint8)
	img[:, :] = 20 + 5 * n
	img[: 8 * (c + 1), :8] = 250
	return img


def write_recording(rec, fragmented=False):
	# fragmented: as campy records by default (fragmentedVideo)
	for c, name in enumerate(CAMERAS):
		d = os.path.join(rec, name)
		os.makedirs(d)
		frames = [n for n in range(1, FRAMES + 1) if n not in DROPPED.get(name, ())]
		sio.save_video(np.stack([np.repeat(picture(c, n)[..., None], 3, -1) for n in frames]),
			os.path.join(d, "0.mp4"), fps=60, crf=0, output_params=["-g", "1"] +
			(["-movflags", "frag_keyframe+empty_moov"] if fragmented else []))
		np.save(os.path.join(d, "frametimes.npy"), np.array([frames, (np.array(frames) - 1) / 60.0], float))
		np.save(os.path.join(d, "hosttimes.npy"), np.array([host(c, n) for n in frames], np.int64))
		with open(os.path.join(d, "metadata.csv"), "w", newline="") as f:
			csv.writer(f).writerows([["videoFilename", "0.mp4"], ["cameraName", name]])


def write_calibration(path, cams):
	with open(path, "w") as f:
		for i, cam in enumerate(cams):
			f.write("[cam_{}]\nname = \"{}\"\nsize = [ 1280, 1024,]\n".format(i, cam.name))
			f.write("matrix = {}\ndistortions = {}\n".format(cam.K.tolist(), cam.dist.tolist()))
			f.write("rotation = {}\ntranslation = {}\n\n".format(Rotation.from_matrix(cam.R).as_rotvec().tolist(),
				cam.t.tolist()))


def make_rows(cams, truth):
	rows = np.zeros(FRAMES, live.RowType(3, 5, NODES, ANIMALS))
	rng = np.random.default_rng(3)
	for r in range(FRAMES):
		row, n = rows[r], r + 1
		row["frame"] = n
		row["host_ns"] = [host(c, n) for c in range(3)]
		for key in ("centroids", "centroid_vals", "keypoints", "peak_vals"):
			row[key] = np.nan
		row["members"] = -1
		for c, cam in enumerate(cams):
			for slot, k in enumerate(rng.permutation(ANIMALS)):
				row["keypoints"][c, slot] = cam.Project(truth[r, k, :NODES])
				row["peak_vals"][c, slot] = 0.5 + 0.01 * np.arange(NODES)
				row["centroids"][c, slot] = row["keypoints"][c, slot, 6]
				row["centroid_vals"][c, slot] = 0.8
				row["members"][k, c] = slot
			row["found"][c] = ANIMALS
		row["points3d"] = truth[r, :, :NODES]
		row["n_views"] = 3
		row["reproj_px"] = 0.5
	rows[4]["keypoints"][1, 0, 3] = np.nan # a node the model didn't find
	return rows


@pytest.fixture
def made(tmp_path):
	cams = [scene3d.look_at(n, *where) for n, where in zip(CALIBRATION_NAMES,
		[((0, 30, 700), (0, 0, 0), (0, 1, 0)), ((-550, -300, 450),), ((550, -300, 450),)])]
	rec = str(tmp_path / "2026-10-23_10-00-00")
	write_recording(rec)
	write_calibration(str(tmp_path / "calibration.toml"), cams)
	truth = scene3d.scene(FRAMES, n=ANIMALS)
	log = str(tmp_path / "campy-live_test")
	rows = make_rows(cams, truth)
	np.save(log + ".npy", rows)
	with open(log + ".json", "w") as f:
		json.dump({"cameras": CAMERAS, "nodes": NODE_NAMES, "edges": [[0, 1], [1, 2]],
			"calibration": str(tmp_path / "calibration.toml"), "calibration_names": CALIBRATION_NAMES,
			"animals": ANIMALS}, f)
	flags = str(tmp_path / "flags")
	os.makedirs(flags)
	sample = dict(kind="campy-live-flag sample", version=1, logs=[dict(path=log + ".npy")],
		sets=[dict(log=log + ".npy", row=n - 1, frame=n, time_s=(n - 1) / 60 + 60 * (n == 30), why="missed",
			flags=[dict(reason="missed", camera="Camera1", animal=0, value=1, x=1, y=1)]) for n in PICKS])
	with open(os.path.join(flags, "sample.json"), "w") as f:
		json.dump(sample, f)
	return dict(rec=rec, log=log + ".npy", rows=rows, sample=os.path.join(flags, "sample.json"), tmp=tmp_path)


def pack(made, **kw):
	return labelpack.Pack(made["sample"], [made["rec"]], name="round1", **kw)


def test_the_package_holds_each_cameras_frame_of_each_set(made):
	out = pack(made)
	assert out["sets"] == 2 and out["skipped"] == 1 # frame 21: Camera2's video lacks it
	labels = sio.load_slp(out["package"])
	assert [v.shape[0] for v in labels.videos] == [2, 2, 2]
	for c, name in enumerate(CAMERAS):
		video = sio.Video(os.path.join(made["rec"], name, "0.mp4"))
		for j, n in enumerate((5, 30)):
			idx = n - 1 - sum(d < n for d in DROPPED.get(name, ()))
			got = labels.videos[c][j]
			assert got.shape == (H, W, 1)
			assert np.array_equal(got[..., 0], np.asarray(video[idx])[..., 0])
			assert abs(int(np.median(got)) - (20 + 5 * n)) <= 2 # the right frame, not a neighbour


def test_a_fragmented_recording_packs_the_same_frames(made, tmp_path):
	# campy's default video: sleap-io can't find its frames until it is copied to a plain MP4
	frag = str(tmp_path / "frag" / os.path.basename(made["rec"]))
	write_recording(frag, fragmented=True)
	for name in CAMERAS:
		assert labelpack.Fragmented(os.path.join(frag, name, "0.mp4"))
		assert not labelpack.Fragmented(os.path.join(made["rec"], name, "0.mp4"))
	plain = sio.load_slp(pack(made)["package"])
	out = labelpack.Pack(made["sample"], [frag], out=str(tmp_path / "fragout"), name="round1")
	got = sio.load_slp(out["package"])
	assert out["sets"] == 2
	for c in range(3):
		for j in range(2):
			assert np.array_equal(got.videos[c][j], plain.videos[c][j])


def test_predictions_and_3d_are_the_logs(made):
	out = pack(made)
	labels = sio.load_slp(out["package"])
	rows = made["rows"]
	session = labels.sessions[0]
	assert [session.get_camera(v).name for v in labels.videos] == CAMERAS # campy's names
	for j, n in enumerate((5, 30)):
		row = rows[n - 1]
		for c, v in enumerate(labels.videos):
			lf = labels.find(v, j)[0]
			got = sorted(tuple(np.nan_to_num(i.numpy()[0])) for i in lf.predicted_instances)
			want = sorted(tuple(np.nan_to_num(row["keypoints"][c, s, 0])) for s in range(ANIMALS))
			assert np.allclose(got, want, atol=1e-3)
		fg = session.frame_groups[j]
		assert sorted(int(g.identity.name.split()[-1]) for g in fg.instance_groups) == [0, 1, 2]
		for g in fg.instance_groups:
			k = int(g.identity.name.split()[-1])
			assert np.allclose(g.instance_3d.numpy(), row["points3d"][k], atol=1e-3)
			assert len(g.instance_by_camera) == 3
	# The node the model didn't find stays empty
	lf = labels.find(labels.videos[1], 0)[0]
	slot0 = [i for i in lf.predicted_instances if np.allclose(i.numpy()[0], rows[4]["keypoints"][1, 0, 0])][0]
	assert np.isnan(slot0.numpy()[3]).all()


def test_luc3d_can_read_the_embedded_videos_numbers(made):
	out = pack(made)
	with h5py.File(out["package"], "r") as f:
		for c in range(3):
			g = f["video{}".format(c)]
			assert g["frame_numbers"].dtype == np.int32
			assert all(not isinstance(v, np.int64) for v in g["video"].attrs.values())


def test_sets_csv_says_where_each_set_came_from_and_its_split(made):
	out = pack(made)
	with open(os.path.join(out["folder"], "sets.csv")) as f:
		rows = list(csv.DictReader(f))
	assert [int(r["campy_frame"]) for r in rows] == [5, 30]
	assert [int(r["video_frame_Camera2"]) for r in rows] == [4, 27]
	for r in rows:
		assert int(r["test"]) == labelpack.IsTest(made["rec"], float(r["time_s"]))
	assert os.path.isfile(os.path.join(out["folder"], "calibration.toml"))


def test_a_training_skeletons_extra_nodes_are_left_empty(made, tmp_path):
	skel = sio.Skeleton(nodes=NODE_NAMES + ["Tail_1", "TailTip"], edges=[("n0", "n1")])
	path = str(tmp_path / "train.slp")
	sio.Labels(skeletons=[skel]).save(path)
	out = pack(made, skeleton=path)
	labels = sio.load_slp(out["package"])
	assert [n.name for n in labels.skeletons[0].nodes] == NODE_NAMES + ["Tail_1", "TailTip"]
	inst = labels.find(labels.videos[0], 0)[0].predicted_instances[0]
	assert np.isnan(inst.numpy()[-2:]).all() and np.isfinite(inst.numpy()[:3]).all()


def test_test_minutes_are_whole_stable_and_about_a_third():
	assert labelpack.IsTest("a/2026-10-23_10-00-00", 61.0) == labelpack.IsTest("b/2026-10-23_10-00-00", 119.9)
	share = np.mean([labelpack.IsTest("rec", 60.0 * m) for m in range(3000)])
	assert 0.30 < share < 0.37
	assert np.mean([labelpack.IsTest("rec", 60.0 * m, share=0.0) for m in range(100)]) == 0


# ---------------------------------------------------------------- campy-label-merge

def correct(package, path, finish=(0,), half=(), hide=None):
	"""What LUC3D saves after a labeller corrected the package: in each set of
	finish, every prediction made a label; in each of half, one only; hide:
	(set, camera, node) marked hidden. LUC3D keeps no frames, only labels."""
	labels = sio.load_slp(package)
	swapped = {}
	for lf in labels:
		j = lf.frame_idx
		if j not in finish and j not in half:
			continue
		preds = list(lf.predicted_instances)
		take = preds if j in finish else preds[:1]
		for p in take:
			pts = p.numpy() + 1.0 # moved a pixel
			user = sio.Instance.from_numpy(pts, skeleton=p.skeleton, track=p.track)
			if hide and (j, labels.videos.index(lf.video)) == hide[:2]:
				user.points["visible"][hide[2]] = False
			lf.instances.remove(p)
			lf.instances.append(user)
			swapped[id(p)] = user
	# The session's groups now hold the labels, as LUC3D's converted in place
	for session in labels.sessions:
		for fg in session.frame_groups.values():
			for g in fg.instance_groups:
				for cam, inst in list(g.instance_by_camera.items()):
					g.instance_by_camera[cam] = swapped.get(id(inst), inst)
	labels.save(path, embed=False, verbose=False)
	return path


def test_merge_takes_finished_frames_and_splits_off_the_test_sets(made, monkeypatch):
	monkeypatch.setattr(labelpack, "IsTest", lambda rec, t, share=None: t >= 60) # frame 30 is a test set
	out = pack(made)
	saved = correct(out["package"], str(made["tmp"] / "round1.labels.slp"), finish=(0, 1), hide=(0, 1, 2))
	r = labelmerge.Merge(out["package"], saved)
	assert r["counts"]["taken"] == 6 and r["train"] == 3 and r["test"] == 3
	train = sio.load_slp(r["written"]["train"])
	test = sio.load_slp(r["written"]["test"])
	pkg = sio.load_slp(out["package"])
	for got, j in ((train, 0), (test, 1)):
		assert len(got) == 3
		for lf in got:
			c = got.videos.index(lf.video) # all 3 cameras, in the package's order
			src = pkg.find(pkg.videos[c], j)[0]
			assert lf.frame_idx == j
			assert np.array_equal(lf.image, src.image) # the package's own frame
			want = sorted(tuple(np.nan_to_num(i.numpy()[0] + 1)) for i in src.predicted_instances)
			assert np.allclose(sorted(tuple(np.nan_to_num(i.numpy()[0])) for i in lf.user_instances), want)
	# Node 2, marked hidden on each mouse of set 0 in Camera1, is left out there only
	hidden = [(train.videos.index(x.video), np.isnan(i.numpy()[2]).all()) for x in train for i in x.user_instances]
	assert [c for c, h in hidden if h] == [1, 1, 1]
	with open(r["written"]["table"]) as f:
		rows = list(csv.DictReader(f))
	assert {r_["split"] for r_ in rows if r_["package_frame"] == "1"} == {"test"}


def test_frames_with_predictions_left_are_not_taken(made):
	out = pack(made)
	saved = correct(out["package"], str(made["tmp"] / "round1.labels.slp"), finish=(), half=(0,))
	r = labelmerge.Merge(out["package"], saved)
	assert r["counts"]["taken"] == 0 and r["counts"]["not_finished"] == 3 and r["counts"]["untouched"] == 3
	assert "train" not in r["written"] and "test" not in r["written"]


def test_merge_adds_to_a_training_package_and_a_growing_test_set(made, monkeypatch, tmp_path):
	monkeypatch.setattr(labelpack, "IsTest", lambda rec, t, share=None: t >= 60)
	out = pack(made)
	saved = correct(out["package"], str(made["tmp"] / "round1.labels.slp"), finish=(0, 1))
	# A 15-node training package of 2 frames, as the real one is (tail nodes and all)
	skel = sio.Skeleton(nodes=NODE_NAMES + ["Tail_1", "Tail_2", "TailTip"])
	base_video = sio.Video(os.path.join(made["rec"], "Camera0", "0.mp4"))
	base = sio.Labels(labeled_frames=[sio.LabeledFrame(video=base_video, frame_idx=i,
		instances=[sio.Instance.from_numpy(np.full((15, 2), 10.0 + i), skeleton=skel)]) for i in (0, 1)],
		videos=[base_video], skeletons=[skel])
	base_path = str(tmp_path / "base.pkg.slp")
	base.save(base_path, embed="user", verbose=False)
	tests = str(tmp_path / "tests" / "rig.test.pkg.slp")
	os.makedirs(os.path.dirname(tests))
	r = labelmerge.Merge(out["package"], saved, train_into=base_path, train_out=str(tmp_path / "new.pkg.slp"),
		test_into=tests)
	new = sio.load_slp(r["written"]["train_into"])
	assert len(new) == 2 + 3 and [n.name for n in new.skeletons[0].nodes] == NODE_NAMES + ["Tail_1", "Tail_2",
		"TailTip"]
	added = [lf for lf in new if lf.video.shape[1:3] == (H, W)][-3:]
	assert all(np.isnan(i.numpy()[-3:]).all() for lf in added for i in lf.user_instances) # no tail labels
	assert len(sio.load_slp(tests)) == 3
	# The next round adds to the test set; nothing leaves it
	labelmerge.Merge(out["package"], saved, out=str(tmp_path / "again"), test_into=tests,
		skeleton=base_path)
	grown = sio.load_slp(tests)
	assert len(grown) == 6
	# What measuring a model needs is in it: each video's camera, each mouse's identity, the calibration
	videos, rounds = labelmerge.VideoInfo(grown)
	assert [v["camera"] for v in videos] == CAMERAS * 2 and {v["round"] for v in videos} == {"round1"}
	assert "[cam_0]" in rounds["round1"]["calibration"] and rounds["round1"]["cameras"] == CAMERAS
	assert rounds["round1"]["sets"]["1"] == dict(campy_frame=30, recording="2026-10-23_10-00-00", why="missed", test=1)
	assert {i.track.name for lf in grown for i in lf.user_instances} == {"animal 0", "animal 1", "animal 2"}
	# The training package keeps its own provenance, its videos unnamed
	videos, _ = labelmerge.VideoInfo(new)
	assert videos[0] is None and [v["camera"] for v in videos[1:]] == CAMERAS


def test_merge_refuses_labels_of_another_package(made, tmp_path):
	out = pack(made)
	other = sio.Labels(videos=[sio.Video(filename="x.mp4", open_backend=False)],
		skeletons=[sio.Skeleton(nodes=NODE_NAMES)])
	path = str(tmp_path / "other.slp")
	other.save(path, verbose=False)
	with pytest.raises(labelmerge.MergeError, match="1 videos, the package 3"):
		labelmerge.Merge(out["package"], path)
