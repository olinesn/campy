"""
campy-live --cuda-graphs (campy/live.py: CUDAGraphed): a graph per count of
animals, all of a model's graphs in one memory pool. Needs a GPU (CUDA or
ROCm); skipped without one.
"""
import gc

import pytest

from campy import live

torch = pytest.importorskip("torch")
if not torch.cuda.is_available():
	pytest.skip("needs a CUDA (or ROCm) GPU", allow_module_level=True)

COUNTS = list(range(1, 11)) # animals found: the instance model's batch


def Net():
	# small, but with working memory well over its output's
	layers, c = [], 1
	for _ in range(5):
		layers += [torch.nn.Conv2d(c, 16, 3, padding=1), torch.nn.ReLU()]
		c = 16
	layers.append(torch.nn.Conv2d(16, 15, 1))
	torch.manual_seed(0)
	return torch.nn.Sequential(*layers).cuda().eval()


def Inputs():
	g = torch.Generator(device="cuda").manual_seed(1)
	return {k: torch.rand((k, 1, 96, 96), device="cuda", generator=g) for k in COUNTS}


def Graphed(share, fp16=False):
	saved = live.CUDAGraphed.SHARE_POOL
	live.CUDAGraphed.SHARE_POOL = share
	try:
		return live.CUDAGraphed(Net(), fp16)
	finally:
		live.CUDAGraphed.SHARE_POOL = saved


def Drop():
	gc.collect()
	torch.cuda.synchronize()
	torch.cuda.empty_cache()


@pytest.mark.parametrize("fp16", [False, True])
def test_counts_in_sequence_match_eager(fp16):
	g = Graphed(True, fp16)
	x = Inputs()
	with torch.inference_mode():
		eager = {k: g.Eager(x[k]).clone() for k in COUNTS}
		# recorded in this order, then replayed in others, as counts come
		order = COUNTS + COUNTS[::-1] + [3, 7, 1, 10, 2, 2, 9, 4]
		for k in order:
			out = g(x[k])
			assert torch.equal(out, eager[k]), "count {}".format(k)
	assert len(g.graphs) == len(COUNTS)
	assert len(g.Pools()) == 1
	assert g.max_abs_diff == 0


def test_outputs_stay_until_their_own_next_call():
	# Each graph's output is its own until it is called again: replaying the
	# other counts' graphs (which share its pool) leaves it as it was. Later
	# graphs could have been given an earlier graph's working memory for
	# their output: replaying the earlier ones last would overwrite it.
	g = Graphed(True)
	x = Inputs()
	with torch.inference_mode():
		eager = {k: g.Eager(x[k]).clone() for k in COUNTS}
		for k in COUNTS:
			g(x[k])
		held = {k: g(x[k]) for k in COUNTS[::-1]}
		torch.cuda.synchronize()
		for k in COUNTS:
			assert torch.equal(held[k], eager[k]), "count {}".format(k)
		assert len({v.data_ptr() for v in held.values()}) == len(COUNTS)


def test_one_pool_takes_less_memory():
	# memory_reserved before and after recording every count, with one pool
	# for all (first, so the eager runs' memory counts against it) and with a
	# pool per graph
	x = Inputs()
	grew, pooled = {}, {}
	for share in (True, False):
		Drop()
		before = torch.cuda.memory_reserved()
		g = Graphed(share)
		with torch.inference_mode():
			for k in COUNTS:
				g(x[k])
		torch.cuda.synchronize()
		grew[share] = torch.cuda.memory_reserved() - before
		pooled[share] = live.PoolBytes(g.Pools())
		assert len(g.Pools()) == (1 if share else len(COUNTS))
		del g
	Drop()
	assert grew[True] > 0
	assert grew[True] * 2 < grew[False], (grew, pooled)
	if pooled[True] is not None:
		assert pooled[True] * 2 < pooled[False], (grew, pooled)
		assert pooled[True] <= grew[True], (grew, pooled)


def test_topdown_counts_match_eager():
	# campy-live's random model, as --cuda-graphs runs it: the whole layer,
	# the centroid model's output given a count of animals per frame
	# (FakeCentroids), graphs against the same modules run eagerly
	pytest.importorskip("sleap_nn")
	frames, most = 2, 3
	args = live.ParseArgs(["--model", "random", "--cuda-graphs", "--fixed-crops", "off", "--frame-shape", "256x320x1",
		"--max-instances", str(most), "--animals", str(most)])
	shape = live.FrameShape("256x320x1")
	model = live.MODELS["random"](args, frames, shape)
	host = live.HostBuffer(True, (frames,) + tuple(shape), live.np.uint8)
	host.array[:] = live.np.random.default_rng(2).integers(0, 256, host.array.shape, live.np.uint8)
	model.Warm(host.tensor, crops=range(1, frames * most + 1))
	frames_on_gpu = model.Upload(host.tensor)
	sets = [[1, 0], [3, 3], [0, 2], [2, 1], [3, 0], [1, 1], [0, 0], [2, 3], [1, 0]]

	def Run():
		out = []
		for counts in sets:
			with live.FakeCentroids(model.layer.centroid_layer, counts):
				out.append(model.Predict(frames_on_gpu))
		return out

	graphed = Run()
	for g in model.graphs:
		g.forward = g.Eager
	try:
		eager = Run()
	finally:
		for g in model.graphs:
			del g.forward
	for counts, a, b in zip(sets, graphed, eager):
		for key in ("centroids", "keypoints", "peak_vals"):
			assert (a[key] is None) == (b[key] is None)
			if a[key] is not None:
				assert live.np.array_equal(a[key], b[key], equal_nan=True), (counts, key)
	assert all(len(g.Pools()) == 1 for g in model.graphs)
	assert sum(len(g.graphs) for g in model.graphs) > len(model.graphs)
