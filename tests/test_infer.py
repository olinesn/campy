"""
campy-infer (campy/infer.py): which videos it runs and where their .slp
files go, and the .slp it writes: one labelled frame per frame read, in
order, each with the animals the models found, as sleap-io reads it back.
The .slp test runs campy-live's random models (made-up centroids, a fixed
number of animals per frame) on the CPU, over a made-up video read by
ffmpeg; it needs torch, sleap-nn and sleap-io.
"""

import os

import numpy as np
import pytest

from campy import infer

from test_videoread import B, H, N, W, Encode, Index

ANIMALS, NODES = 3, 4


def Args(*extra):
	return infer.Parser().parse_args(["x.mp4", "--centroid", "c", "--instance", "i", "--max-instances",
		str(ANIMALS)] + list(extra))


def test_output_names(tmp_path):
	cam = os.path.join(str(tmp_path), "s1", "Camera2")
	args = Args()
	assert infer.OutputPath(os.path.join(cam, "Camera2.mp4"), args) == os.path.join(cam, "Camera2.predictions.slp")
	preds = os.path.join(str(tmp_path), "preds")
	args = Args("--out", preds, "--name", "run1_{folder}_{stem}.slp")
	assert infer.OutputPath(os.path.join(cam, "0.mp4"), args) == os.path.join(preds, "run1_Camera2_0.slp")


def test_two_videos_into_one_slp_stops_before_any_model(tmp_path):
	for cam in ("Camera1", "Camera2"):
		(tmp_path / cam).mkdir()
		(tmp_path / cam / "0.mp4").write_bytes(b"")
	with pytest.raises(SystemExit, match="folder"):
		infer.Main([str(tmp_path), "--centroid", "c", "--instance", "i", "--max-instances", "5", "--out",
			str(tmp_path / "preds")])


def test_videos_with_an_slp_are_skipped(tmp_path, capsys):
	(tmp_path / "a.mp4").write_bytes(b"")
	(tmp_path / "a.predictions.slp").write_bytes(b"")
	assert infer.Main([str(tmp_path / "a.mp4"), "--centroid", "c", "--instance", "i", "--max-instances", "5"]) == 0
	assert "skipped" in capsys.readouterr().out


@pytest.fixture
def random_model(monkeypatch):
	torch = pytest.importorskip("torch")
	pytest.importorskip("sleap_nn")
	sio = pytest.importorskip("sleap_io")
	from campy import live
	from sleap_nn.inference.predictor import Predictor
	largs = live.ParseArgs(["--model", "random", "--device", "cpu", "--frame-shape", "{}x{}x1".format(H, W),
		"--max-instances", str(ANIMALS), "--animals", str(ANIMALS), "--nodes", str(NODES), "--crop", "64",
		"--engine", "off"])
	top = live.MODELS["random"](largs, B, (H, W, 1))
	skeleton = sio.Skeleton(["node{}".format(i) for i in range(NODES)])
	predictor = Predictor(layer=top.layer, skeleton=skeleton, device="cpu")
	args = Args("--device", "cpu", "--batch", str(B), "--decoder", "ffmpeg", "--engine", "off", "--progress", "0")
	return infer.Model(args, predictor=predictor), args, torch


def test_slp_has_every_frame_in_order(random_model, tmp_path):
	model, args, torch = random_model
	import sleap_io as sio
	video = Encode(tmp_path / "v.mp4")
	out = str(tmp_path / "preds" / "v.slp")
	result = infer.RunVideo(model, video, out, args)
	assert result["frames"] == N and os.path.exists(out)
	assert not [f for f in os.listdir(tmp_path / "preds") if f != "v.slp"] # no partial file left
	labels = sio.load_slp(out, open_videos=False)
	assert len(labels.videos) == 1 and os.path.abspath(labels.videos[0].filename) == os.path.abspath(video)
	assert [n.name for n in labels.skeleton.nodes] == ["node{}".format(i) for i in range(NODES)]
	frames = sorted(labels.labeled_frames, key=lambda lf: lf.frame_idx)
	assert [lf.frame_idx for lf in frames] == list(range(N))
	assert all(len(lf.instances) == ANIMALS for lf in frames)
	assert labels.provenance["campy_infer"]["engine"] == "eager"
	# sleap-nn's provenance fields, and its system_info's, made without
	# importing the packages sleap-nn only looks up to drop (wandb, Lightning)
	prov = labels.provenance
	assert {"sleap_nn_version", "sleap_io_version", "model_type", "source_file", "inference_config"} <= set(prov)
	assert prov["frame_selection"]["frames_processed"] == N
	assert set(prov["system_info"]) >= {"python_version", "platform", "pytorch_version", "cuda_version", "accelerator",
		"gpu_count"}
	assert prov["system_info"]["accelerator"] in ("cpu", "cuda", "mps")
	# the keypoints are the models' on that frame: the same frames run again,
	# read by ffmpeg, give them (frame i's own picture, not a neighbour's)
	from campy import videoread
	reader = videoread.FfmpegReader(videoread.VideoInfo(video), B)
	for start, batch in reader:
		assert [Index(f) for f in batch] == list(range(start, start + len(batch)))
		again = model.Run(start, np.array(batch))
		for j in range(len(batch)):
			slp = np.stack([inst.numpy()[:, :2] for inst in frames[start + j].instances])
			found = np.isfinite(again.pred_centroids.numpy()[j, :, 0])
			np.testing.assert_allclose(slp, again.pred_keypoints.numpy()[j][found], atol=1e-4)
	assert result["animals"] == N * ANIMALS


def test_the_last_short_batch_is_not_padded_into_the_slp(random_model, tmp_path):
	# 40 frames in batches of 16: the last batch of 8 is filled up to 16 for
	# the fixed shapes, and only its 8 frames reach the .slp
	model, args, torch = random_model
	import sleap_io as sio
	video = Encode(tmp_path / "w.mp4", n=40)
	out = str(tmp_path / "w.slp")
	infer.RunVideo(model, video, out, args)
	labels = sio.load_slp(out, open_videos=False)
	assert sorted(lf.frame_idx for lf in labels.labeled_frames) == list(range(40))


def test_provenance_imports_no_unused_packages():
	# sleap-nn's own system info imports each of 8 packages to find its
	# folder, about 750 modules: seconds on network storage, every job
	import subprocess, sys
	pytest.importorskip("torch")
	pytest.importorskip("sleap_nn")
	code = """
import sys, datetime
sys.path[:0] = {path!r}
from campy import infer
class A: peak_threshold, max_instances = 0.2, 3
class P:
	model_paths, device = ["c", "i"], "cpu"
m = infer.Model.__new__(infer.Model)
import torch
import sleap_nn.inference.provenance # loaded with the models, in a real run
m.torch, m.args, m.B, m.predictor, m.layer = torch, A, 16, P, object()
before = set(sys.modules)
prov = m.Provenance("v.mp4", datetime.datetime.now(), datetime.datetime.now(), 10, {{}})
new = set(sys.modules) - before
assert "system_info" in prov, prov
print(sorted(n for n in new if n.split(".")[0] in ("wandb", "pytorch_lightning", "lightning", "kornia")))
""".format(path=[os.path.dirname(os.path.dirname(os.path.abspath(__file__)))])
	r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
	assert r.returncode == 0, r.stderr[-2000:]
	assert r.stdout.strip().splitlines()[-1] == "[]"
