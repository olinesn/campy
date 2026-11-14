"""
campy-distill-train (campy/distill.py, campy/distillsleap.py): the geometry
that puts the teacher's maps on the student's grid, and the dataset's teacher
crop. The first tests need numpy only; the rest need torch and sleap-nn 0.3.5
or later (skipped without them; run on a training machine).
"""

import math, os

import numpy as np
import pytest

from campy import distill

# the DoublePoke pair: student 320 px crops shrunk 0.4 to 128, output stride 2,
# sigma 2; teacher 450 px crops at full size, output stride 2, sigma 2.5
S_CROP, S_SCALE, S_STRIDE, S_SIGMA = 320, 0.4, 2, 2.0
T_CROP, T_STRIDE, T_SIGMA = 450, 2, 2.5
OFF = (T_CROP - S_CROP) / 2


def test_teacher_index_maps_the_same_crop_point():
	# a point at student-crop pixel x is student output x*scale/stride and
	# teacher output (x + offset)/teacher stride
	for x in (0.0, 5.0, 160.0, 315.0):   # student output pixels are 5 crop px apart
		i = x * S_SCALE / S_STRIDE
		assert float(i).is_integer()
		k = distill.TeacherIndex(int(i) + 1, S_STRIDE, S_SCALE, OFF, T_STRIDE)[-1]
		assert k == pytest.approx((x + OFF) / T_STRIDE)


def test_blur_widens_the_teacher_spots_to_the_students():
	b = distill.BlurSigma(S_SIGMA, S_SCALE, T_SIGMA, T_STRIDE)
	# student spot 2 px at scale 0.4 = 5 full-frame px; teacher 2.5 px
	assert b == pytest.approx(math.sqrt(5.0 ** 2 - 2.5 ** 2) / 2)
	assert distill.BlurSigma(1.0, 1.0, 3.0, 2) == 0.0


def test_node_index_by_name():
	assert distill.NodeIndex(["Nose", "Trunk"], ["Trunk", "Tail", "Nose"]) == [2, 0]
	with pytest.raises(distill.DistillError, match="Ear"):
		distill.NodeIndex(["Ear"], ["Nose"])


def test_args_pass_overrides_to_sleap_nn():
	a, rest = distill.ParseArgs(["s.yaml", "--teacher", "t", "--alpha", "0.5", "trainer_config.optimizer.lr=5e-5"])
	assert (a.config, a.teacher, a.alpha, a.peak_floor) == ("s.yaml", "t", 0.5, 0.3)
	assert rest == ["trainer_config.optimizer.lr=5e-5"]
	with pytest.raises(SystemExit):
		distill.ParseArgs(["s.yaml", "--teacher", "t", "--alpha", "2"])


def _gauss(h, w, x, y, s, peak=1.0):
	yy, xx = np.mgrid[0:h, 0:w]
	return peak * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * s * s))


def _peak(m):
	"""argmax refined by a 3x3 weighted centroid (x, y)"""
	y, x = np.unravel_index(np.argmax(m), m.shape)
	w = m[y - 1:y + 2, x - 1:x + 2]
	yy, xx = np.mgrid[y - 1:y + 2, x - 1:x + 2]
	return (w * xx).sum() / w.sum(), (w * yy).sum() / w.sum()


def test_teacher_maps_land_on_the_students_grid():
	torch = pytest.importorskip("torch")
	pytest.importorskip("sleap_nn")
	from campy import distillsleap
	Ht = Wt = (T_CROP + 30) // T_STRIDE   # 450 padded to 480 (max stride 32)
	h = w = S_CROP * S_SCALE / S_STRIDE   # 64
	x, y = 200.0, 120.0                   # student-crop pixels
	tm = _gauss(Ht, Wt, (x + OFF) / T_STRIDE, (y + OFF) / T_STRIDE, T_SIGMA / T_STRIDE, peak=0.6)
	blur = distill.BlurSigma(S_SIGMA, S_SCALE, T_SIGMA, T_STRIDE)
	out = distillsleap.ToStudentGrid(torch.tensor(tm)[None, None], (int(h), int(w)), S_STRIDE, S_SCALE, OFF, T_STRIDE, blur)
	m = out[0, 0].numpy()
	px, py = _peak(m)
	assert (px, py) == pytest.approx((x * S_SCALE / S_STRIDE, y * S_SCALE / S_STRIDE), abs=0.15)
	# the teacher's confidence kept: the height of its map's highest pixel
	assert m.max() == pytest.approx(tm.max(), abs=0.02)
	# its width is the student's label spot: sigma 2 input px = 1 output px
	label = _gauss(int(h), int(w), x * S_SCALE / S_STRIDE, y * S_SCALE / S_STRIDE, S_SIGMA / S_STRIDE, peak=tm.max())
	assert np.abs(m - label).max() < 0.06
	# --peak-floor: a sure body part rescaled to 1, an unsure one kept
	both = np.stack([tm, tm * 0.2 / 0.6])
	out = distillsleap.ToStudentGrid(torch.tensor(both)[None], (int(h), int(w)), S_STRIDE, S_SCALE, OFF, T_STRIDE, blur, 0.3)
	assert out[0, 0].max().item() == pytest.approx(1.0, abs=0.01)
	assert out[0, 1].max().item() == pytest.approx(tm.max() / 3, abs=0.01)


def test_dataset_cuts_the_teacher_crop_around_the_same_centre(tmp_path, monkeypatch):
	pytest.importorskip("torch")
	pytest.importorskip("sleap_nn")
	sio = pytest.importorskip("sleap_io")
	import cv2
	from omegaconf import OmegaConf
	monkeypatch.setenv(distill.ENV_CROP, str(T_CROP))
	monkeypatch.setenv(distill.ENV_ALPHA, "0.5")
	from campy import distillsleap
	H, W = 1024, 1280
	pts = np.array([[610.0, 420.0], [640.0, 500.0], [700.0, 470.0]])   # Nose, Trunk, Tail
	img = np.zeros((H, W), np.float64)
	for x, y in pts:
		img += _gauss(H, W, x, y, 3.0, 250.0)
	path = str(tmp_path / "f.png")
	cv2.imwrite(path, np.clip(img, 0, 255).astype(np.uint8))
	skel = sio.Skeleton(["Nose", "Trunk", "Tail"])
	video = sio.Video.from_filename([path])
	lf = sio.LabeledFrame(video=video, frame_idx=0, instances=[sio.Instance.from_numpy(pts, skeleton=skel)])
	labels = sio.Labels(labeled_frames=[lf], videos=[video], skeletons=[skel])
	head = OmegaConf.create({"part_names": ["Nose", "Trunk", "Tail"], "anchor_part": "Trunk", "sigma": S_SIGMA,
		"output_stride": S_STRIDE})
	ds = distillsleap.DistillInstanceDataset(labels=[labels], confmap_head_config=head, max_stride=32, anchor_ind=1,
		user_instances_only=True, ensure_rgb=False, ensure_grayscale=True, intensity_aug=None, geometric_aug=None,
		scale=S_SCALE, apply_aug=False, crop_size=S_CROP, max_hw=(H, W), cache_img=None)
	s = ds[0]
	t = s["teacher_image"][0, 0].numpy().astype(float)
	assert t.shape == (T_CROP, T_CROP)
	# each spot sits at its frame position minus the crop's corner, the crop
	# centred on the trunk: the same centre as the student's crop
	corner = pts[1] - (T_CROP - 1) / 2
	for p in pts:
		x, y = p - corner
		xi, yi = int(round(x)), int(round(y))
		win = t[yi - 4:yi + 5, xi - 4:xi + 5]
		yy, xx = np.mgrid[yi - 4:yi + 5, xi - 4:xi + 5]
		assert ((win * xx).sum() / win.sum(), (win * yy).sum() / win.sum()) == pytest.approx((x, y), abs=0.6)
	# and the student's labels (input px) are the same points through its own crop
	assert np.allclose(s["instance"][0].numpy() / S_SCALE + OFF, pts - corner, atol=0.6)
