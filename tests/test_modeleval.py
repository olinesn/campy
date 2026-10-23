"""campy-model-eval's scores (campy/modeleval.py) and campy-model-fit (campy/modelfit.py).

The scores are checked on hand-made labels and detections; the whole of
campy-model-eval on test_labelpack's made-up recording, packed, "corrected"
and merged into a test set, with a stand-in for campy-live's models that
returns the true poses (and one that misses an animal).
"""

import json, os

import numpy as np
import pytest

from campy import modeleval, modelfit

NAMES = ["Nose", "Head", "Trunk", "Tail_0"]


def mouse(x, y):
	return np.array([[x, y - 30], [x, y - 15], [x, y], [x, y + 20]], float)


def test_scores_count_found_missed_extra_and_wrong_mouse():
	labels = np.stack([mouse(100, 100), mouse(300, 100), mouse(500, 100), mouse(700, 100)])
	labels[0, 0] = np.nan # Nose not labelled
	preds = np.full((5, 4, 2), np.nan)
	cents = np.full((5, 2), np.nan)
	preds[0], cents[0] = mouse(100, 100), (100, 100) # exact
	preds[1], cents[1] = mouse(303, 104), (301, 101) # 5 px off
	preds[2], cents[2] = mouse(300, 100) + [0, 0], (502, 100) # centroid on 500, skeleton on the 300 mouse
	preds[2, 3] = np.nan # a node it didn't place
	preds[3], cents[3] = mouse(900, 400), (900, 400) # extra; mouse 700 missed
	s = modeleval.Score(wrong_px=30)
	pairs = s.Add(labels, preds, cents, NAMES, match_px=50)
	assert sorted(pairs) == [(0, 0), (1, 1), (2, 2)]
	r = s.Report()
	assert (r["animals"], r["found"], r["missed"], r["extra"], r["wrong_mouse"]) == (4, 3, 1, 1, 1)
	assert r["node_missed"] == 1
	assert r["nodes"] == 3 + 4 + 3 # mouse 0 without its Nose, mouse 2 without its Tail_0
	assert r["node_p50"] == 5.0 and r["within5"] == pytest.approx(7 / 10)


def test_anchor_is_the_trunk_or_the_median():
	m = mouse(10, 20)
	assert np.allclose(modeleval.Anchor(m, NAMES), (10, 20))
	m[2] = np.nan
	assert np.allclose(modeleval.Anchor(m, NAMES), np.median(mouse(10, 20)[[0, 1, 3]], axis=0))


def test_grouped_needs_one_3d_animal_with_only_that_animals_detections():
	# identities a, b, c seen in cameras 0-2 (detection slots); the 3D's members (animals x cameras)
	slots = {"a": {0: 0, 1: 2, 2: 1}, "b": {0: 1, 1: 0}, "c": {2: 0}}
	right = np.array([[0, 2, 1], [1, 0, -1], [-1, -1, 0]])
	assert modeleval.Grouped(slots, right) == (2, 2) # c is in one camera only: not groupable
	split = np.array([[0, 2, -1], [1, 0, 1], [-1, -1, 0]]) # a's camera-2 view went to b's animal
	assert modeleval.Grouped(slots, split) == (0, 2)
	mixed = np.array([[0, 2, 1], [1, -1, -1], [-1, 0, 0]]) # b's camera-1 view went to c's animal
	assert modeleval.Grouped(slots, mixed) == (1, 2)


def test_frame_stats_and_animal_pixels():
	img = np.full((100, 120), 20, np.uint8)
	img[40:60, 50:70] = 200
	img[0, :12] = 255
	s = modelfit.FrameStats(img)
	assert s["saturated"] == pytest.approx(12 / 12000)
	assert modelfit.AnimalPixels(img, [[60, 50]]) == 200
	assert modelfit.AnimalPixels(img, [[np.nan, np.nan]]) is None
	assert modelfit.AnimalPixels(img, [[-50, -50]]) is None # off the frame


def test_compare_warns_on_brightness_saturation_animals_and_gamma():
	train = dict(gamma="Off", stats=dict(mean=dict(p5=14, p50=24, p95=33), saturated=dict(p5=0, p50=0, p95=0.001),
		animal=dict(p5=90, p50=127, p95=170)))
	ok = dict(stats=dict(mean=dict(p50=24), saturated=dict(p50=0.0005), animal=dict(p50=130)),
		settings=dict(cameraGamma="Off", cameraMake="flir"))
	assert modelfit.Compare(train, ok) == []
	bad = dict(stats=dict(mean=dict(p50=60), saturated=dict(p50=0.02), animal=dict(p50=240)),
		settings=dict(cameraGamma="0.6", cameraMake="flir"))
	text = " ".join(modelfit.Compare(train, bad))
	for word in ("brighter", "more saturated", "animals brighter", "gamma differs"):
		assert word in text
	emu = dict(bad, settings=dict(cameraGamma="0.6", cameraMake="emu"))
	assert not any("gamma" in w for w in modelfit.Compare(train, emu))


# ---------------------------------------------------------------- the whole of it, on a made-up rig

@pytest.fixture
def test_set(tmp_path, monkeypatch):
	sio = pytest.importorskip("sleap_io")
	import test_labelpack as tl
	from campy import labelmerge, labelpack
	made = tl.made.__wrapped__(tmp_path) # test_labelpack's made-up recording, log and sample
	monkeypatch.setattr(labelpack, "IsTest", lambda rec, t, share=None: True) # every set a test set
	out = labelpack.Pack(made["sample"], [made["rec"]], name="round1")
	saved = tl.correct(out["package"], str(tmp_path / "round1.labels.slp"), finish=(0, 1))
	tests = str(tmp_path / "tests" / "rig.test.pkg.slp")
	labelmerge.Merge(out["package"], saved, test_into=tests)
	return tests, sio.load_slp(tests)


class Truth:
	"""Stands in for campy-live's models: the test set's own labels, as
	campy-live's results; miss: drop the first animal of camera 0."""

	def __init__(self, labels, miss=False):
		from campy import labelmerge
		self.labels, self.miss = labels, miss
		self.info, self.rounds = labelmerge.VideoInfo(labels)
		self.calls = 0

	def __call__(self, name, centroid, instance, live_args, n_frames, shape):
		self.n_frames = n_frames
		self.names = [n.name for n in self.labels.skeletons[0].nodes]
		self.order = list(self.labels.find(self.labels.videos[0])) # sets in order, as Evaluate runs them
		return self

	def Run(self, images):
		lf0 = self.order[self.calls]
		self.calls += 1
		kp = np.full((self.n_frames, 5, len(self.names), 2), np.nan)
		cams = self.rounds["round1"]["cameras"]
		for v, info in zip(self.labels.videos, self.info):
			c = cams.index(info["camera"])
			lf = self.labels.find(v, lf0.frame_idx)[0]
			for k, inst in enumerate(lf.user_instances):
				kp[c, k] = inst.numpy()
		if self.miss:
			kp[0, 0] = np.nan
		cents = kp[:, :, 6]
		return dict(keypoints=kp, centroids=cents, peak_vals=np.where(np.isnan(kp[..., 0]), np.nan, 0.9))


def test_evaluate_scores_a_perfect_pair_and_one_that_misses(test_set, monkeypatch):
	path, labels = test_set
	monkeypatch.setattr(modeleval, "ANCHOR", "n6")
	perfect = Truth(labels)
	monkeypatch.setattr(modeleval, "Pair", perfect)
	r = modeleval.Evaluate(path, [("perfect", ("c", "i"))], say=lambda t: None)["perfect"]
	assert r["all"]["animals"] == 18 and r["all"]["missed"] == 0 and r["all"]["extra"] == 0
	assert r["all"]["node_p99"] < 0.01 and r["all"]["wrong_mouse"] == 0
	assert r["all"]["grouped_share"] == 1.0 and r["all"]["groupable"] == 6
	assert set(r) >= {"all", "flagged", "camera Camera0", "camera Camera1", "camera Camera2"}
	monkeypatch.setattr(modeleval, "Pair", Truth(labels, miss=True))
	r = modeleval.Evaluate(path, [("misses", ("c", "i"))], say=lambda t: None)["misses"]
	assert r["all"]["missed"] == 2 and r["camera Camera0"]["missed"] == 2 and r["camera Camera1"]["missed"] == 0
	assert r["all"]["grouped"] == r["all"]["groupable"] == 6 # missed in one camera, still grouped from the other two


def test_model_fit_stats_and_check(tmp_path):
	sio = pytest.importorskip("sleap_io")
	import test_labelpack as tl
	rec = str(tmp_path / "rec")
	tl.write_recording(rec)
	# A training package of the recording's own frames: the rig looks like it
	video = sio.Video(os.path.join(rec, "Camera0", "0.mp4"))
	skel = sio.Skeleton(nodes=["a", "b"])
	labels = sio.Labels(labeled_frames=[sio.LabeledFrame(video=video, frame_idx=i,
		instances=[sio.Instance.from_numpy(np.array([[40.0, 30], [45, 35]]), skeleton=skel)]) for i in range(0, 39, 3)],
		videos=[video], skeletons=[skel])
	pkg = str(tmp_path / "train.pkg.slp")
	labels.save(pkg, embed="user", verbose=False)
	model = tmp_path / "model"
	model.mkdir()
	(model / "best.ckpt").write_bytes(b"weights")
	assert modelfit.Main(["stats", pkg, "--out", str(model), "--gamma", "Off"]) == 0
	card = json.loads((model / "model.json").read_text())
	assert card["id"] == modelfit.ModelId(str(model)) and len(card["id"]) == 12
	# The recording's metadata.csv says no gamma: only brightness is compared, and it matches
	assert modelfit.Main(["check", str(model), "--recording", rec, "--seconds", "1", "--frames", "10"]) == 0
	stats = json.loads((model / "image_stats.json").read_text())
	stats["stats"]["mean"].update(p5=200, p95=230)
	(model / "image_stats.json").write_text(json.dumps(stats))
	assert modelfit.Main(["check", str(model), "--recording", rec]) == 1 # now it is darker
