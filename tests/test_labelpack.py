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
			# framesWritten: the camera closed its recording (without it, a crash: its video's frames are counted)
			csv.writer(f).writerows([["videoFilename", "0.mp4"], ["cameraName", name], ["frameRate", 60],
				["framesWritten", len(frames)]])


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
		# Decided on the recording's clock (campy frame over the frame rate), not the log's time_s
		assert r["split_source"] == "2026-10-23_10-00-00"
		assert float(r["split_s"]) == pytest.approx((int(r["campy_frame"]) - 1) / 60, abs=1e-3)
		side, why = labelpack.TestSide(r["split_source"], float(r["split_s"]))
		assert int(r["test"]) == side and r["split"] == why
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

def split_frame_30(rec, t, share=None):
	# A stand-in split: the time of campy frame 30 (0.483 s on the recording's clock) is a test minute's, and
	# frame 5's (0.067 s), 3 s either side, isn't
	return 0.4 <= t < 0.6


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
	monkeypatch.setattr(labelpack, "IsTest", split_frame_30) # frame 30 is a test set
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
	monkeypatch.setattr(labelpack, "IsTest", split_frame_30)
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
	# The same round again is refused before anything is written
	with pytest.raises(labelmerge.MergeError, match="already has round round1"):
		labelmerge.Merge(out["package"], saved, out=str(tmp_path / "again"), test_into=tests, skeleton=base_path)
	assert not os.path.exists(tmp_path / "again")
	# The next round adds to the test set; nothing leaves it
	out2 = labelpack.Pack(made["sample"], [made["rec"]], name="round2", out=str(tmp_path / "r2"))
	saved2 = correct(out2["package"], str(tmp_path / "r2" / "round2.labels.slp"), finish=(0, 1))
	labelmerge.Merge(out2["package"], saved2, out=str(tmp_path / "again"), test_into=tests, skeleton=base_path)
	grown = sio.load_slp(tests)
	assert len(grown) == 6
	# What measuring a model needs is in it: each video's camera, each mouse's identity, the calibration
	videos, rounds = labelmerge.VideoInfo(grown)
	assert [v["camera"] for v in videos] == CAMERAS * 2 and [v["round"] for v in videos] == ["round1"] * 3 + \
		["round2"] * 3
	assert "[cam_0]" in rounds["round1"]["calibration"] and rounds["round1"]["cameras"] == CAMERAS
	assert rounds["round1"]["sets"]["1"] == dict(campy_frame=30, recording="2026-10-23_10-00-00", why="missed",
		test=1, split_source="2026-10-23_10-00-00", split_s=0.483)
	assert {i.track.name for lf in grown for i in lf.user_instances} == {"animal 0", "animal 1", "animal 2"}
	# The training package keeps its own provenance, its videos unnamed
	videos, _ = labelmerge.VideoInfo(new)
	assert videos[0] is None and [v["camera"] for v in videos[1:]] == CAMERAS


def test_merge_refuses_labels_of_another_package(made, tmp_path):
	out = pack(made)
	other = sio.Labels(videos=[sio.Video(filename="x.mp4", open_backend=False)],
		skeletons=[sio.Skeleton(nodes=list(NODE_NAMES))]) # a copy: sleap-io turns the list's names into Nodes
	path = str(tmp_path / "other.slp")
	other.save(path, verbose=False)
	with pytest.raises(labelmerge.MergeError, match="1 videos, the package 3"):
		labelmerge.Merge(out["package"], path)


# ---------------------------------------------------------------- the split, on the recording's own clock

def sides(source, seconds, **kw):
	return np.array([labelpack.TestSide(source, t, **kw)[0] for t in seconds], dtype=object)


def test_a_second_log_of_the_recording_splits_its_frames_the_same(made, tmp_path):
	# campy-live started 10 s (here 10 frames) late, or again: a log from campy frame 11 on, flagged alone
	late = str(tmp_path / "campy-live.part2")
	np.save(late + ".npy", made["rows"][10:])
	with open(made["log"][:-4] + ".json") as f:
		summary = json.load(f)
	with open(late + ".json", "w") as f:
		json.dump(summary, f)
	sample = json.load(open(made["sample"]))
	sample["logs"] = [dict(path=late + ".npy")]
	sample["sets"] = [dict(s, log=late + ".npy", row=s["frame"] - 11, time_s=(s["frame"] - 11) / 60)
		for s in sample["sets"] if s["frame"] > 21]
	path = str(tmp_path / "late_flags" / "sample.json")
	os.makedirs(os.path.dirname(path))
	json.dump(sample, open(path, "w"))
	first = pack(made)
	second = labelpack.Pack(path, [made["rec"]], name="late")
	rows = [list(csv.DictReader(open(os.path.join(o["folder"], "sets.csv")))) for o in (first, second)]
	a = {r["campy_frame"]: r for r in rows[0]}
	for r in rows[1]: # frame 30, in both
		assert (r["test"], r["split_s"], r["split_source"]) == (a[r["campy_frame"]]["test"],
			a[r["campy_frame"]]["split_s"], a[r["campy_frame"]]["split_source"])


def test_over_an_hour_no_frame_changes_side_whatever_the_log_and_none_trains_near_a_test_frame():
	clock = labelpack.SplitClock.__new__(labelpack.SplitClock)
	clock.source, clock.rate, clock.clip_frames = "2026-10-26_14-02-11", 60.0, None
	frames = np.arange(1, 3600 * 60 + 1, 30)
	t = np.array([clock.Seconds(f) for f in frames])
	side = sides(clock.source, t)
	# The log's start doesn't enter it: the seconds are campy's frame over the frame rate
	assert np.array_equal(t, (frames - 1) / 60.0)
	assert 0.28 < np.mean(side == True) < 0.38 and 0.01 < np.mean(side == None) < 0.08 # the guard band
	test_t, train_t = t[side == True], t[side == False]
	gap = np.min(np.abs(train_t[:, None] - test_t[None, :]), axis=1)
	assert gap.min() > labelpack.GUARD_S
	# The old split (minutes from the log's first set) put a share of these on the other side when the log
	# started 10 s late; this one has nothing that could move
	old = np.array([labelpack.IsTest(clock.source, x) for x in t])
	late = np.array([labelpack.IsTest(clock.source, x - 10) for x in t])
	assert np.mean(old != late) > 0.05


def test_a_replay_is_split_on_its_clip_whatever_the_recording(tmp_path):
	# Two replays of one clip, recorded into folders of their own names: the same frames on the same side
	root = tmp_path / "clips"
	(root / "Camera0").mkdir(parents=True)
	np.save(root / "Camera0" / "frametimes.npy", np.zeros((2, 600))) # the clip: 600 frames
	clocks = []
	for rec in ("2026-11-01_09-00-00", "2026-11-02_10-30-00"):
		d = tmp_path / rec / "Camera0"
		d.mkdir(parents=True)
		with open(d / "metadata.csv", "w", newline="") as f:
			csv.writer(f).writerows([["cameraMake", "emu"], ["videoFilename", "emuclip.mp4"], ["cameraName",
				"Camera0"], ["frameRate", 60], ["videoFolderRoot", str(root)]])
		clocks.append(labelpack.SplitClock(str(tmp_path / rec), "Camera0", {}))
	assert clocks[0].Same(clocks[1]) and clocks[0].source == "clip clips clip.mp4"
	assert clocks[0].Seconds(601) == clocks[0].Seconds(1) == 0 # its second loop is the clip's start again
	# The clip elsewhere: metadata.csv's clipFrames (the emulated camera counted them) says where loops start
	other = tmp_path / "2026-11-03_08-00-00" / "Camera0"
	other.mkdir(parents=True)
	with open(other / "metadata.csv", "w", newline="") as f:
		csv.writer(f).writerows([["cameraMake", "emu"], ["videoFilename", "emuclip.mp4"], ["cameraName",
			"Camera0"], ["frameRate", 60], ["videoFolderRoot", "/elsewhere/clips"], ["clipFrames", 600]])
	moved = labelpack.SplitClock(str(tmp_path / "2026-11-03_08-00-00"), "Camera0", {})
	assert moved.Same(clocks[0]) and moved.Seconds(1201) == 0
	assert np.array_equal(moved.SecondsOf([1, 601, 1201, 1260]), [0, 0, 0, 59 / 60])
	# A recording of real cameras is keyed on its own name
	assert labelpack.SplitClock(made_rec(tmp_path), "Camera0", {}).source == "2026-10-23_10-00-00"


def made_rec(tmp_path):
	d = tmp_path / "2026-10-23_10-00-00" / "Camera0"
	d.mkdir(parents=True)
	with open(d / "metadata.csv", "w", newline="") as f:
		csv.writer(f).writerows([["cameraMake", "flir"], ["frameRate", 60]])
	return str(tmp_path / "2026-10-23_10-00-00")


def test_frames_near_the_test_package_are_test_sets_and_near_trained_ones_left_out(monkeypatch):
	monkeypatch.setattr(labelpack, "IsTest", lambda rec, t, share=None: False) # no test minutes at all
	tested, trained = {"rec": [100.0]}, {"rec": [200.0]}
	assert labelpack.TestSide("rec", 102.0, tested=tested) == (None,
		"training minute, within 3 s of a frame in the test package")
	assert labelpack.TestSide("rec", 104.0, tested=tested)[0] is False
	assert labelpack.TestSide("other", 100.0, tested=tested)[0] is False
	assert labelpack.TestSide("rec", 201.0, trained=trained)[0] is False # training near training: fine
	monkeypatch.setattr(labelpack, "IsTest", lambda rec, t, share=None: True) # all test
	assert labelpack.TestSide("rec", 50.0, trained=trained)[0] is True
	assert labelpack.TestSide("rec", 201.0, trained=trained) == (None,
		"test minute, within 3 s of a frame already trained on")
	assert labelpack.TestSide("rec", 101.0, tested=tested)[0] is True


def test_the_test_package_keeps_its_frames_neighbours_out_of_training(made, monkeypatch, tmp_path):
	# Round 1, packed with the split before this one: frame 30 a test set, in the rig's test package
	monkeypatch.setattr(labelpack, "IsTest", split_frame_30)
	out = pack(made)
	saved = correct(out["package"], str(made["tmp"] / "round1.labels.slp"), finish=(0, 1))
	tests = str(tmp_path / "rig.test.pkg.slp")
	assert labelpack.Pack(made["sample"], [made["rec"]], name="round0", out=str(tmp_path / "r0"),
		test_package=tests)["sets"] == 2 # the rig's first round: no test package yet
	labelmerge.Merge(out["package"], saved, test_into=tests)
	# Now no minute is a test minute, but frame 30 is in the test package: no training set near it
	monkeypatch.setattr(labelpack, "IsTest", lambda rec, t, share=None: False)
	with pytest.raises(labelpack.PackError, match="already has a round named round1"):
		labelpack.Pack(made["sample"], [made["rec"]], name="round1", out=str(tmp_path / "r2"), test_package=tests)
	r2 = labelpack.Pack(made["sample"], [made["rec"]], name="round2", out=str(tmp_path / "r2"), test_package=tests,
		guard_s=0.3)
	rows = list(csv.DictReader(open(os.path.join(r2["folder"], "sets.csv"))))
	assert [(r["campy_frame"], r["test"]) for r in rows] == [("5", "0")] and r2["left_out"] == 1
	# A set that would be a test set, near a frame trained on: left out
	monkeypatch.setattr(labelpack, "IsTest", lambda rec, t, share=None: True)
	train = labelmerge.Merge(out["package"], saved, out=str(tmp_path / "t"))["written"]["train"] # frame 5's
	r3 = labelpack.Pack(made["sample"], [made["rec"]], name="round3", out=str(tmp_path / "r3"), train_package=train,
		guard_s=0.3)
	rows = list(csv.DictReader(open(os.path.join(r3["folder"], "sets.csv"))))
	assert [(r["campy_frame"], r["test"]) for r in rows] == [("30", "1")] and r3["left_out"] == 1


def test_old_split_packs_a_sample_as_before(made):
	out = pack(made, old_split=True)
	rows = list(csv.DictReader(open(os.path.join(out["folder"], "sets.csv"))))
	for r in rows:
		assert int(r["test"]) == labelpack.IsTest(made["rec"], float(r["time_s"]))
		assert r["split"].startswith("old split")


# ---------------------------------------------------------------- round names

def test_each_rounds_default_name_is_its_own(made, tmp_path):
	a = labelpack.RoundName(made["sample"], made["rec"])
	assert a == labelpack.RoundName(made["sample"], made["rec"]) and a.startswith("2026-10-23_10-00-00_")
	sample = json.load(open(made["sample"]))
	sample["sets"] = sample["sets"][:2]
	other = str(tmp_path / "other_flags" / "sample.json")
	os.makedirs(os.path.dirname(other))
	json.dump(sample, open(other, "w"))
	assert labelpack.RoundName(other, made["rec"]) != a
	assert labelpack.Pack(made["sample"], [made["rec"]])["name"] == a


def test_merging_a_round_again_is_refused_or_replaces_it(made, monkeypatch, tmp_path):
	monkeypatch.setattr(labelpack, "IsTest", split_frame_30)
	out = pack(made)
	saved = correct(out["package"], str(made["tmp"] / "round1.labels.slp"), finish=(0, 1))
	tests = str(tmp_path / "rig.test.pkg.slp")
	labelmerge.Merge(out["package"], saved, test_into=tests)
	with pytest.raises(labelmerge.MergeError, match="already has round round1"):
		labelmerge.Merge(out["package"], saved, test_into=tests)
	assert len(sio.load_slp(tests)) == 3
	labelmerge.Merge(out["package"], saved, test_into=tests, replace_round=True)
	again = sio.load_slp(tests)
	assert len(again) == 3 and len(again.videos) == 3


def test_the_two_skeleton_merge_checks_the_test_package_before_writing(made, monkeypatch, tmp_path):
	# docs/models.md: one run per training package; --test-into on the run whose nodes are the test package's
	monkeypatch.setattr(labelpack, "IsTest", split_frame_30)
	out = pack(made)
	saved = correct(out["package"], str(made["tmp"] / "round1.labels.slp"), finish=(0, 1))
	tests = str(tmp_path / "rig.test.pkg.slp")
	labelmerge.Merge(out["package"], saved, test_into=tests) # 12 nodes, the package's
	skel = sio.Skeleton(nodes=NODE_NAMES + ["Tail_1", "Tail_2", "TailTip"])
	video = sio.Video(os.path.join(made["rec"], "Camera0", "0.mp4"))
	base = str(tmp_path / "base15.pkg.slp")
	sio.Labels(labeled_frames=[sio.LabeledFrame(video=video, frame_idx=0, instances=[sio.Instance.from_numpy(
		np.full((15, 2), 10.0), skeleton=skel)])], videos=[video], skeletons=[skel]).save(base, embed="user",
		verbose=False)
	new = str(tmp_path / "new15.pkg.slp")
	with pytest.raises(labelmerge.MergeError) as e:
		labelmerge.Merge(out["package"], saved, out=str(tmp_path / "m15"), train_into=base, train_out=new,
			test_into=tests)
	assert "--train-into's" in str(e.value) and "--skeleton" not in str(e.value) and "one" in str(e.value)
	assert not os.path.exists(new) and not os.path.exists(tmp_path / "m15")
	# Without --test-into, that run goes through
	assert os.path.isfile(labelmerge.Merge(out["package"], saved, out=str(tmp_path / "m15"), train_into=base,
		train_out=new)["written"]["train_into"])


# ---------------------------------------------------------------- each log is its recording's

def test_a_log_of_another_recording_is_refused(made, tmp_path):
	other = str(tmp_path / "other" / "2026-10-23_10-00-00")
	write_recording(other)
	for name in CAMERAS: # another recording: its frames read the host clock at other times
		p = os.path.join(other, name, "hosttimes.npy")
		np.save(p, np.load(p) + 5 * 10 ** 9)
	with pytest.raises(labelpack.PackError, match="not this recording's"):
		labelpack.Pack(made["sample"], [other], name="x")


def test_a_set_that_isnt_its_rows_frame_is_refused(made):
	sample = json.load(open(made["sample"]))
	sample["sets"][0]["frame"] = 6
	json.dump(sample, open(made["sample"], "w"))
	with pytest.raises(labelpack.PackError, match="row 4 isn't campy frame 6"):
		pack(made)


def two_logs(made, tmp_path, change=None, missing=False):
	"""A sample of two logs of the made-up recording (frames 1-20 and 21-40),
	both named campy-live.npy, as campy-session names them. change(summary)
	alters the second's summary; missing: neither is where the sample says,
	only one is beside the flags folder."""
	with open(made["log"][:-4] + ".json") as f:
		summary = json.load(f)
	paths = []
	for k, rows in enumerate((made["rows"][:20], made["rows"][20:])):
		d = tmp_path / "logs{}".format(k)
		d.mkdir()
		np.save(d / "campy-live.npy", rows)
		s = dict(summary)
		if k and change:
			change(s)
		json.dump(s, open(d / "campy-live.json", "w"))
		paths.append(str(d / "campy-live.npy"))
	flags = tmp_path / "logs0" / "campy-live_flags"
	flags.mkdir()
	sets = [dict(log=paths[0], row=4, frame=5, time_s=0.0, why="random", flags=[]),
		dict(log=paths[1], row=9, frame=30, time_s=0.0, why="random", flags=[])]
	if missing:
		for p in paths:
			os.rename(p, p + ".away")
		os.rename(paths[0] + ".away", str(tmp_path / "logs0" / "campy-live.npy"))
		paths_ = [p.replace(str(tmp_path), "/somewhere/else") for p in paths]
		for s, p in zip(sets, paths_):
			s["log"] = p
		paths = paths_
	json.dump(dict(kind="campy-live-flag sample", version=1, logs=[dict(path=p) for p in paths], sets=sets),
		open(flags / "sample.json", "w"))
	return str(flags / "sample.json")


def test_two_logs_of_one_recording_pack_together(made, tmp_path):
	out = labelpack.Pack(two_logs(made, tmp_path), [made["rec"]] * 2, name="two")
	assert out["sets"] == 2


def test_the_missing_log_fallback_wont_send_two_logs_to_one_file(made, tmp_path):
	with pytest.raises(labelpack.PackError, match="can't be told apart"):
		labelpack.Pack(two_logs(made, tmp_path, missing=True), [made["rec"]] * 2, name="two")


def test_logs_with_other_calibrations_or_nodes_are_packed_apart(made, tmp_path):
	# The rig calibrated again between the two logs: one calibration can't serve both
	moved = str(tmp_path / "calibration2.toml")
	text = open(str(made["tmp"] / "calibration.toml")).read()
	open(moved, "w").write(text.replace("size = [ 1280, 1024,]", "size = [ 1280, 1000,]", 1))
	(tmp_path / "a").mkdir()
	with pytest.raises(labelpack.PackError, match="calibration differs"):
		labelpack.Pack(two_logs(made, tmp_path / "a", change=lambda s: s.update(calibration=moved)),
			[made["rec"]] * 2, name="two")
	(tmp_path / "b").mkdir()
	with pytest.raises(labelpack.PackError, match="nodes differ"):
		labelpack.Pack(two_logs(made, tmp_path / "b", change=lambda s: s.update(nodes=NODE_NAMES[::-1])),
			[made["rec"]] * 2, name="two")
