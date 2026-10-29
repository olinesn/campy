"""
bench/sleap_infer_bench.py's --fixed-crops (campy-live's own set: FixedCrops,
its graph and engines), --sweep-rows and --trained's checks, on the CPU; the
GPU's graph and engines with a CUDA (or ROCm) GPU. Needs torch and sleap-nn.
"""
import argparse, json, os, subprocess, sys

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("sleap_nn")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(ROOT, "bench")
sys.path.insert(0, BENCH)
import sleap_infer_bench as bench  # noqa: E402

GPU = torch.cuda.is_available()


def Args(**kw):
	# The script's defaults, then kw
	old = sys.argv
	sys.argv = ["sleap_infer_bench.py"]
	try:
		args = bench.parse_args()
	finally:
		sys.argv = old
	args.frames, args.nodes = 2, 5
	for key, value in kw.items():
		setattr(args, key, value)
	return args


def Run(*argv, out=None):
	env = dict(os.environ, PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
	cmd = [sys.executable, os.path.join(BENCH, "sleap_infer_bench.py")] + list(argv)
	if out:
		cmd += ["--json", str(out)]
	return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=ROOT, timeout=600)


def test_sweep_rows():
	assert bench.parse_rows("0-2,7, 30-31", 32) == [0, 1, 2, 7, 30, 31]
	assert bench.parse_rows("5", 32) == [5]
	for bad in ("31-32", "4-2", "x", "-1"):
		with pytest.raises(SystemExit):
			bench.parse_rows(bad, 32)


def test_same_weights_every_build():
	# An engine is kept under a hash of its weights: the same size must get the
	# same weights each row and run, or every row would build its engines anew
	args = Args(fixed_crops=True)
	c, ci = bench.SWEEP_CENTROID[0], dict(bench.SWEEP_INSTANCE[0], output_stride=2)
	one = bench.make_layers(c, ci, args, "cpu", "fp32", 3)
	torch.manual_seed(123) # whatever ran before
	two = bench.make_layers(c, ci, args, "cpu", "fp32", 3)
	for a, b in ((one[3], two[3]), (one[4], two[4])):
		sa, sb = a.state_dict(), b.state_dict()
		assert sa.keys() == sb.keys() and all(torch.equal(sa[k], sb[k]) for k in sa)
	# --fixed-crops: the model as it is (no synthetic peaks), its layer capped at k
	assert one[1].backend.model is one[3]
	assert one[1].max_instances == 3


def test_fixed_crops_row_cpu():
	from campy import live
	bench._LIVE = live
	try:
		args = Args(fixed_crops=True, engine="eager", iters=3, warmup=1)
		c, ci = bench.SWEEP_CENTROID[0], dict(bench.SWEEP_INSTANCE[0], output_stride=2)
		frames = torch.zeros((args.frames, bench.FRAME_H, bench.FRAME_W, 1), dtype=torch.uint8)
		r = bench.run_one(c, ci, args, "cpu", "fp32", 3, frames, "pipeline")
	finally:
		bench._LIVE = None
	# every frame gave k crops, and the set came back as campy-live's results
	assert r["fixed_crops"] and r["engine"] == "eager" and r["crops_per_set"] == 2 * 3
	assert r["keypoints_shape"] == [2, 3, 5, 2]
	assert not r["cuda_graphs"] and "engines" not in r
	assert r["iters"] == 3 and r["p50_ms"] > 0


def test_options_that_need_fixed_crops(tmp_path):
	for argv, said in ((["--engine", "auto"], "--engine: with --fixed-crops only"),
			(["--trained", "a", "b"], "--trained: with --fixed-crops only"),
			(["--fixed-crops", "--compile"], "--engine compile")):
		p = Run("--device", "cpu", *argv)
		assert p.returncode != 0 and said in p.stderr, p.stderr


def test_fixed_crops_sweep_rows_cpu(tmp_path):
	out = tmp_path / "sweep.json"
	p = Run("--device", "cpu", "--sweep", "--fixed-crops", "--sweep-rows", "0,31", "--iters", "2", "--warmup", "0",
		"--frames", "1", "--nodes", "3", "--crops-per-frame", "2", out=out)
	assert p.returncode == 0, p.stderr
	data = json.loads(out.read_text())
	assert data["header"]["fixed_crops"] and data["header"]["engine"] == "eager"
	# the pairs only (no stage alone), the first and the last of the 32
	rows = data["results"]
	assert [r["stage"] for r in rows] == ["pipeline", "pipeline"]
	assert rows[0]["centroid"] == dict(bench.SWEEP_CENTROID[0])
	assert rows[1]["centroid"] == dict(bench.SWEEP_CENTROID[-1])
	assert rows[1]["instance"]["crop"] == bench.SWEEP_INSTANCE[-1]["crop"]
	assert all(r["crops_per_set"] == 2 for r in rows) and data["failed"] == []


@pytest.mark.skipif(not GPU, reason="needs a CUDA (or ROCm) GPU")
def test_fixed_crops_graph_and_engine_gpu(tmp_path):
	# campy-live's default set on this GPU: one graph, the networks as --engine auto makes them
	out = tmp_path / "gpu.json"
	p = Run("--device", "cuda", "--fixed-crops", "--cuda-graphs", "--engine", "auto", "--engine-cache",
		str(tmp_path / "engines"), "--precision", "fp16", "--hold-clock", "on", "--iters", "20", "--warmup", "5",
		out=out)
	assert p.returncode == 0, p.stderr
	[r] = json.loads(out.read_text())["results"]
	assert r["cuda_graphs"] and r["fixed_crops"]
	if r["engine"] != "eager":
		assert len(r["engines"]) == 2 and r["engine_made_s"] > 0
