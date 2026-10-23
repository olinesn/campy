"""
campy-live --fixed-crops (campy/live.py: FixedCrops): every frame gives
--max-instances crops, the missing animals' padded, with peak finding of
fixed shapes, so the results must be sleap-nn's own TopDownLayer's: the same
animals found, in the same order, the same keypoints. On the CPU and, with a
GPU (CUDA or ROCm), also recorded as one CUDA graph. Needs torch and sleap-nn.
"""
import numpy as np
import pytest

from campy import live

torch = pytest.importorskip("torch")
pytest.importorskip("sleap_nn")

GPU = torch.cuda.is_available()
DEVICES = ["cpu"] + (["cuda"] if GPU else [])
needs_gpu = pytest.mark.skipif(not GPU, reason="needs a CUDA (or ROCm) GPU")

FRAMES, MOST, SHAPE = 2, 3, "256x320x1"
# Animals per frame, set after set: none, fewer than MOST (sleap-nn keeps
# them in raster order), MOST, and more than MOST (its most confident, in
# order of confidence)
SETS = [[1, 0], [3, 3], [0, 0], [2, 1], [4, 0], [1, 1], [0, 2], [2, 4], [1, 0]]
TOL = 1e-4 # px: the instance stage's batch differs (frames x MOST against those found)


class Sharp(torch.nn.Module):
	"""
	A centered-instance model with one sharp peak per node, off the pixel
	grid, where the crop's content puts it: the keypoints say whether each
	crop was cut in the right place, and differ between the crops of
	different animals. Each crop's output depends on that crop alone.
	"""

	def __init__(self, nodes, stride):
		super().__init__()
		self.nodes, self.stride = nodes, stride

	def forward(self, img):
		return self.Maps(img.squeeze(1).float() / 255)

	def Maps(self, img):
		# the confidence maps of a normalized (N, C, H, W) batch
		n, _, height, width = img.shape
		h, w = height // self.stride, width // self.stride
		m = img.mean(dim=(1, 2, 3))[:, None] - 0.5
		k = torch.arange(self.nodes, device=img.device)[None, :]
		cx = w * (k + 1) / (self.nodes + 1) + 9 * m + 0.3
		cy = h / 2 + 7 * m + (k % 3) - 0.4
		yy = torch.arange(h, device=img.device).view(1, 1, h, 1)
		xx = torch.arange(w, device=img.device).view(1, 1, 1, w)
		return torch.exp(-((xx - cx[..., None, None]) ** 2 + (yy - cy[..., None, None]) ** 2) / 4)


def Mode(value):
	return value if isinstance(value, str) else "on" if value else "off"


def Model(device, fixed=True, graphs=False, precision="fp32", strict=False, engine="eager"):
	args = live.ParseArgs(["--model", "random", "--device", device, "--frame-shape", SHAPE, "--precision", precision,
		"--max-instances", str(MOST), "--animals", str(MOST), "--nodes", "6", "--engine", engine,
		"--fixed-crops", Mode(fixed), "--cuda-graphs", Mode(graphs)] + (["--strict-peaks"] if strict else []))
	model = live.MODELS["random"](args, FRAMES, live.FrameShape(SHAPE))
	backend = model.layer.centered_instance_layer.backend
	backend.model = Sharp(6, model.layer.centered_instance_layer.output_stride).to(device)
	host = live.HostBuffer(True, (FRAMES,) + live.FrameShape(SHAPE), np.uint8)
	# a slope across the frame, with noise: each crop's content (and so its
	# keypoints) depends on where it was cut
	rng = np.random.default_rng(3)
	slope = np.linspace(20, 230, host.array.shape[2])[None, None, :, None]
	host.array[:] = np.clip(slope + rng.normal(0, 15, host.array.shape), 0, 255).astype(np.uint8)
	return model, host


def SetCounts(model, counts):
	# Peaks of different heights, off the grid, in the centroid model's
	# output (the random model's SyntheticCentroidPeaks blobs, changed in
	# place, so a graph sees them too)
	blobs = model.layer.centroid_layer.backend.model.blobs
	_, _, h, w = blobs.shape
	yy, xx = torch.meshgrid(torch.arange(h, dtype=torch.float32), torch.arange(w, dtype=torch.float32), indexing="ij")
	new = torch.zeros(blobs.shape)
	for f, k in enumerate(counts):
		for i in range(k):
			cx, cy = w * (i + 1) / (k + 1) + 0.3, h * (0.3 + 0.4 * ((i + f) % 2)) - 0.2
			new[f, 0] += (1 - 0.07 * i) * torch.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / 8)
	blobs.copy_(new.to(blobs.device))


def test_reference_crops_at_crop_size():
	# The reference is campy-live's own layer, whose crops live.ExactCropSize
	# cuts at the crop size; sleap-nn's crop_bboxes alone, for this centroid,
	# cuts them a pixel short (tests/test_live_exact_crops.py), and
	# --fixed-crops always cuts the crop size
	from sleap_nn.inference.ops.crops import crop_bboxes, make_centered_bboxes
	centroid = [221.54798889160156, 77.36744689941406]
	image = torch.zeros(1, 1, 256, 320, dtype=torch.uint8)
	boxes = make_centered_bboxes(torch.tensor([centroid]), 192, 192)
	assert tuple(crop_bboxes(image, boxes, torch.tensor([0])).shape[-2:]) == (192, 191)
	model, _ = Model("cpu", fixed=False)
	instance, seen = model.layer.centered_instance_layer, []
	predict = instance.predict

	def Spy(crops):
		seen.append(tuple(crops.shape[-2:]))
		return predict(crops)

	instance.predict = Spy
	try:
		model.layer._run_stage_2(image, torch.tensor([[centroid]]), torch.ones(1, 1), torch.ones(1, 1, dtype=torch.bool))
	finally:
		instance.predict = predict
	assert seen == [tuple(model.layer.crop_size)] == [(192, 192)]


def Reference(model, frames):
	# sleap-nn's TopDownLayer, as campy-live runs it without --fixed-crops
	# (its crops cut at their real size by live.ExactCropSize, as
	# --fixed-crops cuts them; tests/test_live_exact_crops.py checks that)
	fixed, model.fixed = model.fixed, None
	try:
		return model.Predict(frames)
	finally:
		model.fixed = fixed


def Found(result):
	return (~np.isnan(result["centroids"]).any(-1)).sum(-1)


def Compare(a, b, counts):
	# a: --fixed-crops, b: sleap-nn's. Returns the keypoints' largest difference.
	assert list(Found(a)) == [min(k, MOST) for k in counts]
	assert list(Found(b)) == list(Found(a))
	for key in ("centroids", "centroid_vals"):
		assert np.array_equal(a[key], b[key], equal_nan=True), (counts, key)
	assert np.array_equal(a["found_mask"], ~np.isnan(a["centroids"]).any(-1))
	if not Found(b).sum(): # sleap-nn guesses the node count then: only check none came
		assert np.isnan(a["keypoints"]).all() and np.isnan(a["peak_vals"]).all()
		return 0.0
	diff = 0.0
	for key in ("keypoints", "peak_vals"):
		assert a[key].shape == b[key].shape, key
		assert np.array_equal(np.isnan(a[key]), np.isnan(b[key])), (counts, key)
		if np.isfinite(a[key]).any():
			diff = max(diff, float(np.nanmax(np.abs(a[key] - b[key]))))
	assert diff <= TOL, (counts, diff)
	return diff


@pytest.mark.parametrize("device", DEVICES)
def test_matches_sleap(device):
	model, host = Model(device)
	model.Warm(host.tensor)
	frames = model.Upload(host.tensor)
	for counts in SETS:
		SetCounts(model, counts)
		Compare(model.Predict(frames), Reference(model, frames), counts)
	assert model.nodes == 6


@pytest.mark.parametrize("device", DEVICES)
def test_padded_crops_never_yield_keypoints(device):
	# Sharp gives keypoints for any crop, padded or not: only the real ones may come out
	model, host = Model(device)
	frames = model.Upload(host.tensor)
	for counts in ([0, 0], [1, 0], [0, 3], [2, 1]):
		SetCounts(model, counts)
		r = model.Predict(frames)
		for f, k in enumerate(counts):
			assert np.isfinite(r["keypoints"][f, :k]).all() and np.isfinite(r["peak_vals"][f, :k]).all()
			assert np.isnan(r["keypoints"][f, k:]).all() and np.isnan(r["peak_vals"][f, k:]).all()
			assert np.isnan(r["centroids"][f, k:]).all() and np.isnan(r["centroid_vals"][f, k:]).all()


@needs_gpu
@pytest.mark.parametrize("precision", ["fp32", "fp16"])
def test_one_graph_replays_match(precision):
	# One graph for every count of animals, replayed as the counts change
	# (the centroid model's peaks changed in place): the same as the same
	# steps run eagerly, and as sleap-nn's layer
	model, host = Model("cuda", graphs=True, precision=precision)
	model.Warm(host.tensor)
	fixed = model.fixed
	assert fixed.graph and fixed.max_abs_diff == 0
	recorded = fixed.graph
	frames = model.Upload(host.tensor)
	assert frames is fixed.static_in
	for counts in SETS + SETS[::-1]:
		SetCounts(model, counts)
		graphed = fixed.Run(frames).clone()
		with torch.inference_mode():
			eager = fixed.Pipeline(frames)
		assert live.MaxAbsDiff(graphed, eager) == 0, counts
		if precision == "fp32":
			Compare(model.Predict(frames), Reference(model, model.Upload(host.tensor)), counts)
		else: # fp16: the instance model's rounding differs with its batch; the animals found may not
			assert list(Found(model.Predict(frames))) == list(Found(Reference(model, frames)))
	assert fixed.graph is recorded
	assert "one CUDA graph" in model.DescribeGraphs()


@needs_gpu
def test_results_come_back_in_one_copy():
	# Predict: one copy from the GPU (into pinned memory), nothing else
	model, host = Model("cuda", graphs=True)
	model.Warm(host.tensor)
	frames = model.Upload(host.tensor)
	SetCounts(model, [2, 3])
	model.Predict(frames)
	torch.cuda.synchronize()
	with torch.profiler.profile(activities=[torch.profiler.ProfilerActivity.CPU]) as prof:
		model.Predict(frames) # frames: the graph's own input, already there
	names = [e.name for e in prof.events()]
	assert names.count("aten::copy_") == 1, names
	assert not {"aten::item", "aten::_local_scalar_dense", "aten::nonzero"} & set(names), names
	assert model.fixed.host.is_pinned()


import importlib.util
needs_compile = pytest.mark.skipif(not GPU or not importlib.util.find_spec("triton"),
	reason="needs a GPU and Triton (torch.compile)")
needs_tensorrt = pytest.mark.skipif(not GPU or bool(getattr(torch.version, "hip", None)) or
	not importlib.util.find_spec("tensorrt") or not importlib.util.find_spec("onnx"),
	reason="needs an NVIDIA GPU, tensorrt and onnx")
ENGINE_TOL = 1e-3 # px: a compiled engine rounds the networks' sums its own way


def Near(a, b, counts, tol=ENGINE_TOL):
	# a: --fixed-crops with a compiled engine, b: sleap-nn's. The same animals,
	# keypoints and centroids within tol
	assert list(Found(a)) == [min(k, MOST) for k in counts]
	assert list(Found(b)) == list(Found(a))
	for key in ("centroids", "keypoints") if Found(b).sum() else ("centroids",): # none: sleap-nn guesses the nodes
		assert a[key].shape == b[key].shape, key
		assert np.array_equal(np.isnan(a[key]), np.isnan(b[key])), (counts, key)
		if np.isfinite(a[key]).any():
			assert float(np.nanmax(np.abs(a[key] - b[key]))) <= tol, (counts, key)


@needs_compile
def test_compiled_engine_in_the_graph():
	# --engine compile: both networks compiled, recorded in the set's one
	# graph; replays equal the same steps run eagerly, and match sleap-nn's layer
	model, host = Model("cuda", graphs=True, engine="compile")
	model.Warm(host.tensor)
	fixed = model.fixed
	assert fixed.graph and fixed.max_abs_diff == 0
	assert sorted(e.kind for e in fixed.engines.values()) == ["compile", "compile"]
	frames = model.Upload(host.tensor)
	for counts in SETS:
		SetCounts(model, counts) # the centroid model's buffer, read by the compiled kernels each run
		graphed = fixed.Run(frames).clone()
		with torch.inference_mode():
			eager = fixed.Pipeline(frames)
		assert live.MaxAbsDiff(graphed, eager) == 0, counts
		Near(model.Predict(frames), Reference(model, model.Upload(host.tensor)), counts)
	assert "torch.compile" in model.DescribeGraphs()


class Heads(torch.nn.Module):
	# sleap-nn's Model: a normalized batch in, a dict of heads out
	def __init__(self, name, maps):
		super().__init__()
		self.name, self.maps = name, maps

	def forward(self, x):
		return {self.name: self.maps(x)}


class Lightning(torch.nn.Module):
	# sleap-nn's Lightning module: .model, and a forward that normalizes first
	def __init__(self, name, maps):
		super().__init__()
		self.name, self.model = name, Heads(name, maps)

	def forward(self, img):
		return self.model(img.squeeze(1).float() / 255)[self.name]


class MapsOf(torch.nn.Module):
	# Sharp's confidence maps of a normalized batch
	def __init__(self, sharp):
		super().__init__()
		self.sharp = sharp

	def forward(self, x):
		return self.sharp.Maps(x)


class Blobs(torch.nn.Module):
	# a centroid network whose output is fixed peaks (plus a trace of its input)
	def __init__(self, blobs):
		super().__init__()
		self.register_buffer("blobs", blobs)

	def forward(self, x):
		return self.blobs + 1e-6 * x.mean()


@needs_tensorrt
@pytest.mark.parametrize("graphs", [False, True])
def test_tensorrt_engine(graphs, tmp_path):
	# --engine tensorrt on sleap-nn-shaped models: built, cached, and the
	# same animals and keypoints as sleap-nn's layer
	counts = [2, 3]
	model, host = Model("cuda", graphs=graphs, engine="tensorrt")
	model.fixed.cache = str(tmp_path)
	SetCounts(model, counts)
	c, ci = model.layer.centroid_layer.backend, model.layer.centered_instance_layer.backend
	c.model = Lightning("CentroidConfmapsHead", Blobs(c.model.blobs.clone())).cuda()
	ci.model = Lightning("CenteredInstanceConfmapsHead", MapsOf(ci.model)).cuda()
	model.Warm(host.tensor)
	assert sorted(e.kind for e in model.fixed.engines.values()) == ["tensorrt", "tensorrt"]
	assert len(list(tmp_path.glob("*.plan"))) == 2
	frames = model.Upload(host.tensor)
	Near(model.Predict(frames), Reference(model, model.Upload(host.tensor)), counts)
	assert "TensorRT" in model.DescribeGraphs()


def test_engine_options():
	assert live.ParseArgs([]).engine == "auto"
	with pytest.raises(SystemExit):
		Model("cpu", engine="compile") # CUDA or ROCm only
	with pytest.raises(SystemExit):
		Model("cpu", engine="on")
	with pytest.raises(SystemExit):
		Model("cuda" if GPU else "cpu", fixed=False, engine="compile") # --fixed-crops only
	for engine in ("auto", "off", "eager"): # auto without a GPU, or without fixed crops: none, and nothing said
		for fixed in (True, False):
			model, _ = Model("cpu", fixed=fixed, engine=engine)
			assert model.DescribeEngine() is None
			assert model.fixed is None or model.fixed.engine == "eager"
	assert live.FixedEngine.Choose("off", "cuda") == live.FixedEngine.Choose("eager", "cpu") == "eager"
	assert live.FixedEngine.Pick("cpu") == ("eager", None)
	if GPU and getattr(torch.version, "hip", None):
		with pytest.raises(SystemExit):
			live.FixedEngine.Choose("tensorrt", "cuda")
		assert live.FixedEngine.Choose("auto", "cuda") == live.FixedEngine.Choose("on", "cuda") == "compile"


def Hide(monkeypatch, *modules):
	# importlib.util.find_spec as if these packages weren't installed
	find = importlib.util.find_spec
	monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name in modules else find(name, *a))


@pytest.mark.parametrize("hip", [None, "7.0"])
def test_engine_auto_picks(monkeypatch, hip):
	# auto: TensorRT on NVIDIA where tensorrt and onnx are installed, else
	# torch.compile where Triton is, else none and why; on: the same, or stop
	monkeypatch.setattr(torch.version, "hip", hip)
	monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: object())
	assert live.FixedEngine.Pick("cuda") == ("compile" if hip else "tensorrt", None)
	Hide(monkeypatch, "onnx")
	assert live.FixedEngine.Pick("cuda") == ("compile", None)
	Hide(monkeypatch, "onnx", "tensorrt", "triton")
	kind, why = live.FixedEngine.Pick("cuda")
	assert kind == "eager" and "Triton" in why and ("TensorRT" in why) == (hip is None)
	assert live.FixedEngine.Choose("auto", "cuda") == "eager"
	with pytest.raises(SystemExit, match="--engine on: "):
		live.FixedEngine.Choose("on", "cuda")


@needs_gpu
def test_engine_auto_without_packages(monkeypatch):
	# Neither TensorRT nor Triton: the networks in torch, said once, and the
	# same as --engine off (with --fast-upsample as it would be then)
	Hide(monkeypatch, "tensorrt", "onnx", "triton")
	model, host = Model("cuda", graphs=True, engine="auto")
	model.Warm(host.tensor)
	assert model.fixed.engine == "eager" and not model.fixed.engines and model.fixed.graph
	assert model.DescribeEngine().startswith("--engine auto: ") and "so the networks run in torch" in model.DescribeEngine()
	plain, _ = Model("cuda", graphs=True, engine="off")
	assert len(model.upsamples) == len(plain.upsamples)
	with pytest.raises(SystemExit):
		Model("cuda", engine="on")


@needs_gpu
@pytest.mark.parametrize("error", [RuntimeError("inductor: no kernel for this"),
	SystemExit("--engine tensorrt: TensorRT couldn't build the instance network")])
@pytest.mark.parametrize("graphs", [False, True])
def test_engine_auto_falls_back_when_it_fails(monkeypatch, error, graphs):
	# auto: an engine that fails while warming up (torch.compile, a TensorRT
	# build) leaves the networks in torch, with one message, and the warm-up
	# done again: the results then are --engine off's, graph and all.
	# Chosen explicitly, the same failure stops campy-live
	def Fail(self, *a, **k):
		raise error
	monkeypatch.setattr(live.FixedEngine, "Pick", staticmethod(lambda device: ("compile", None)))
	monkeypatch.setattr(live.FixedEngine, "Choose", staticmethod(lambda engine, device: "compile"))
	monkeypatch.setattr(live.FixedEngine, "__init__", Fail)
	counts = [2, 3]
	model, host = Model("cuda", graphs=graphs, engine="auto")
	SetCounts(model, counts)
	model.Warm(host.tensor)
	fixed = model.fixed
	assert fixed.engine == "eager" and not fixed.engines
	assert bool(fixed.graph) == graphs and fixed.max_abs_diff == 0
	note = model.DescribeEngine()
	assert note.startswith("--engine auto: the torch.compile engine failed (") and str(error) in note
	frames = model.Upload(host.tensor)
	Compare(model.Predict(frames), Reference(model, model.Upload(host.tensor)), counts)
	explicit, host = Model("cuda", graphs=graphs, engine="compile")
	with pytest.raises(type(error)):
		explicit.Warm(host.tensor)


def test_plan_names():
	# A TensorRT plan's file name changes with the weights, the shape, the
	# precision, TensorRT, the GPU and PyTorch (it exports the ONNX)
	net = torch.nn.Conv2d(1, 2, 3)
	args = ("instance", net.state_dict(), (15, 1, 1, 160, 160), True, "11.4.0", "NVIDIA RTX 2000 Ada")
	name = live.FixedEngine.PlanName(*args)
	assert name.startswith("instance_") and name == live.FixedEngine.PlanName(*args)
	for i, other in ((1, torch.nn.Conv2d(1, 2, 3).state_dict()), (2, (15, 1, 1, 192, 192)), (3, False),
			(4, "11.5.0"), (5, "NVIDIA RTX 6000 Ada")):
		changed = list(args)
		changed[i] = other
		assert live.FixedEngine.PlanName(*changed) != name, i


def test_compile_cache_survives_restarts(monkeypatch, tmp_path):
	# torch.compile's kernels go with the TensorRT plans (--engine-cache),
	# not into the temp folder Linux empties at each restart, unless
	# TORCHINDUCTOR_CACHE_DIR says where
	monkeypatch.setattr(torch, "compile", lambda fn, **k: fn)
	engine = live.FixedEngine.__new__(live.FixedEngine)
	engine.torch, engine.fp16, engine.name = torch, False, "centroid"
	# started without it: ours, even once inductor has written its own default into it
	monkeypatch.setattr(live, "_INDUCTOR_DIR", None)
	monkeypatch.setenv("TORCHINDUCTOR_CACHE_DIR", "/tmp/torchinductor_someone")
	engine.Compile(torch.nn.Identity(), str(tmp_path))
	assert live.os.environ["TORCHINDUCTOR_CACHE_DIR"] == str(tmp_path / "inductor")
	# started with it: the user's
	monkeypatch.setattr(live, "_INDUCTOR_DIR", str(tmp_path / "mine"))
	monkeypatch.setenv("TORCHINDUCTOR_CACHE_DIR", str(tmp_path / "mine"))
	engine.Compile(torch.nn.Identity(), "")
	assert live.os.environ["TORCHINDUCTOR_CACHE_DIR"] == str(tmp_path / "mine")
	assert live.FixedEngine.CacheDir("") == live.os.path.expanduser(live.os.path.join("~", ".cache", "campy", "engines"))


def test_options():
	# auto by default (on with a GPU); the bare option is on
	args = live.ParseArgs([])
	assert args.fixed_crops == "auto" and args.cuda_graphs == "auto"
	args = live.ParseArgs(["--fixed-crops", "--cuda-graphs", "off"])
	assert args.fixed_crops == "on" and args.cuda_graphs == "off"
	assert live.OnOff(True) == "on" and live.OnOff(False) == "off"
	with pytest.raises(SystemExit):
		live.ParseArgs(["--fixed-crops", "sometimes"])
	# auto on the CPU: neither, and no error
	model, _ = Model("cpu", fixed="auto", graphs="auto")
	assert model.fixed is None and not model.graphs and model.DescribeGraphs() is None
	with pytest.raises(SystemExit):
		Model("cpu", graphs=True)


@needs_gpu
def test_auto_is_one_graph_with_a_gpu():
	model, _ = Model("cuda", fixed="auto", graphs="auto")
	assert model.fixed is not None and model.fixed.graph is None and not model.graphs # one graph, recorded at the first set


@needs_gpu
def test_auto_falls_back_when_the_model_cant(monkeypatch):
	# a model FixedCrops refuses: sleap-nn's own layer, graphed per count, and why
	def Refuse(*a, **k):
		raise SystemExit("--fixed-crops: integral refinement or none, not local")
	monkeypatch.setattr(live, "FixedCrops", Refuse)
	model, _ = Model("cuda", fixed="auto", graphs="auto")
	assert model.fixed is None and len(model.graphs) == 2
	assert "not local; so sleap-nn's own top-down layer" in model.DescribeGraphs()
	assert "not local" in model.fixedNote
	with pytest.raises(SystemExit):
		Model("cuda", fixed="on", graphs="auto")


@pytest.mark.parametrize("strict", [False, True])
def test_tied_centroid_tops(strict):
	# Each frame's one animal has a top of two equal pixels (as fp16 rounds
	# about 0.7% of them): campy-live keeps it once, --fixed-crops too, the
	# same as the layer; --strict-peaks loses it, as sleap-nn does
	from campy import peaks
	model, host = Model("cpu", strict=strict)
	assert model.fixed.tiedPeaks == (not strict) == peaks.TiedPeaksInUse()
	net = model.layer.centroid_layer.backend.model
	blobs = net.blobs
	_, _, h, w = blobs.shape

	class Flat(torch.nn.Module): # no random model output under the blobs, so the two pixels stay equal
		def forward(self, img):
			return torch.zeros((img.shape[0], 1, h, w), device=img.device)
	net.inner = Flat()
	yy, xx = torch.meshgrid(torch.arange(h, dtype=torch.float32), torch.arange(w, dtype=torch.float32), indexing="ij")
	new = torch.zeros(blobs.shape)
	for f in range(FRAMES):
		cx, cy = w // 2 + 0.5 + 3 * f, h // 2 # half way between two pixels
		new[f, 0] = torch.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / 8)
		assert new[f, 0, cy, int(cx)] == new[f, 0, cy, int(cx) + 1]
	blobs.copy_(new)
	frames = model.Upload(host.tensor)
	a, b = model.Predict(frames), Reference(model, frames)
	assert list(Found(a)) == list(Found(b)) == [0 if strict else 1] * FRAMES
	for key in ("centroids", "centroid_vals"): # the same peak, refined the same (keypoints: test_matches_sleap)
		assert np.array_equal(a[key], b[key], equal_nan=True), key
