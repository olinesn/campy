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


def Model(device, fixed=True, graphs=False, precision="fp32", engine="eager"):
	args = live.ParseArgs(["--model", "random", "--device", device, "--frame-shape", SHAPE, "--precision", precision,
		"--max-instances", str(MOST), "--animals", str(MOST), "--nodes", "6", "--engine", engine] +
		(["--fixed-crops"] if fixed else []) + (["--cuda-graphs"] if graphs else []))
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


def ExactCropSize(original):
	"""
	sleap-nn's crop_bboxes, with its crop size made exact. It takes the size
	from the first box's corners, int(abs(bottom - top)) + 1, in float32: for
	some centroids (x 221.547989 with 192 px crops) right - left comes out
	190.99998, not 191, and every crop of the set is cut a pixel short
	(test_sleap_crop_size_rounding). --fixed-crops always cuts the crop size.
	Here the first box's far corners are moved half a pixel further, so the
	size rounds right; the top-lefts, which place the crops, are untouched.
	"""
	def crop_bboxes(images, bboxes, sample_inds):
		if len(bboxes):
			bboxes = bboxes.clone()
			bboxes[0, 3, 1] = bboxes[0, 0, 1] + (torch.round(bboxes[0, 3, 1] - bboxes[0, 0, 1]) + 0.5)
			bboxes[0, 1, 0] = bboxes[0, 0, 0] + (torch.round(bboxes[0, 1, 0] - bboxes[0, 0, 0]) + 0.5)
		return original(images, bboxes, sample_inds)
	return crop_bboxes


@pytest.fixture(autouse=True)
def exact_crop_size(monkeypatch):
	import sleap_nn.inference.layers.topdown as topdown
	monkeypatch.setattr(topdown, "crop_bboxes", ExactCropSize(topdown.crop_bboxes))


def test_sleap_crop_size_rounding():
	# Why the reference needs ExactCropSize: sleap-nn's own crop size, for a
	# centroid --fixed-crops crops at the right size
	from sleap_nn.inference.ops.crops import crop_bboxes, make_centered_bboxes
	image = torch.zeros(1, 1, 256, 320, dtype=torch.uint8)
	boxes = make_centered_bboxes(torch.tensor([[221.54798889160156, 77.36744689941406]]), 192, 192)
	assert tuple(crop_bboxes(image, boxes, torch.tensor([0])).shape[-2:]) == (192, 191)
	assert tuple(ExactCropSize(crop_bboxes)(image, boxes, torch.tensor([0])).shape[-2:]) == (192, 192)


def Reference(model, frames):
	# sleap-nn's TopDownLayer, as campy-live runs it without --fixed-crops
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
	for key in ("centroids", "keypoints"):
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
	assert live.ParseArgs([]).engine == "eager"
	with pytest.raises(SystemExit):
		Model("cpu", engine="compile") # CUDA or ROCm only
	with pytest.raises(SystemExit):
		Model("cpu", fixed=False, engine="auto") # --fixed-crops only
	if GPU and getattr(torch.version, "hip", None):
		with pytest.raises(SystemExit):
			live.FixedEngine.Choose("tensorrt", "cuda")
		assert live.FixedEngine.Choose("auto", "cuda") == "compile"


def test_options():
	args = live.ParseArgs(["--fixed-crops"])
	assert args.fixed_crops and not args.cuda_graphs
	assert not live.ParseArgs([]).fixed_crops
	with pytest.raises(SystemExit):
		Model("cpu", graphs=True)
