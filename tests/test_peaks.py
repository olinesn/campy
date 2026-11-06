"""
campy/peaks.py: FlatTopPeaks, sleap-nn's (0.3.5 on) local peak rule with
fixed shapes, for campy-live's fixed crops: a flat top of equal pixels (fp16
rounding) that fits in a 2x2 block is one peak, at the mean of its pixels.
Needs torch; the comparisons with sleap-nn need sleap-nn 0.3.5 or later.
"""
import pytest

from campy import peaks

torch = pytest.importorskip("torch")


def Blob(h, w, cx, cy, sigma=2.5):
	yy, xx = torch.meshgrid(torch.arange(h, dtype=torch.float32), torch.arange(w, dtype=torch.float32), indexing="ij")
	return torch.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma ** 2))


def Ours(cms, threshold=0.2):
	# FlatTopPeaks as sleap-nn's find_local_peaks_rough returns them: points
	# (x, y), values, sample and channel, in (sample, row, column, channel) order
	peak, dx, dy = peaks.FlatTopPeaks(cms, threshold)
	subs = torch.stack(torch.where(peak.permute(0, 2, 3, 1)), dim=-1)
	s, y, x, c = subs.unbind(1)
	points = torch.stack([x.float() + dx[s, c, y, x], y.float() + dy[s, c, y, x]], dim=-1)
	return points, cms[s, c, y, x], s.to(torch.int32), c.to(torch.int32)


def Found(cms, threshold=0.2):
	points, _, samples, channels = Ours(cms, threshold)
	return [(int(s), int(c), round(x, 4), round(y, 4)) for (x, y), s, c in zip(points.tolist(), samples, channels)]


@pytest.mark.parametrize("dx, dy", [(1, 0), (0, 1), (1, 1), (-1, 1)])
def test_two_equal_top_pixels_give_one_peak_between_them(dx, dy):
	cm = Blob(20, 24, 10, 8)[None, None] * 0.5
	for x, y in ((10, 8), (10 + dx, 8 + dy)):
		cm[0, 0, y, x] = 0.9
	assert Found(cm) == [(0, 0, 10 + dx / 2, 8 + dy / 2)]


def test_a_peak_between_two_pixels():
	cm = Blob(20, 24, 10.5, 8)[None, None]
	assert cm[0, 0, 8, 10] == cm[0, 0, 8, 11]
	assert Found(cm) == [(0, 0, 10.5, 8)]


def test_flat_two_by_two_top():
	assert Found(Blob(20, 24, 10.5, 8.5)[None, None]) == [(0, 0, 10.5, 8.5)]


@pytest.mark.parametrize("top, mean", [
	([(10, 8), (11, 8), (10, 9)], (31 / 3, 25 / 3)), # an L of 3 (3 of a 2x2)
	([(10, 8), (11, 8), (11, 9)], (32 / 3, 25 / 3)),
	([(10, 8), (9, 9), (10, 9)], (29 / 3, 26 / 3)),
	([(10, 8), (11, 8), (12, 8)], None), # a row of 3
	([(10, 8), (11, 9), (12, 10)], None), # a diagonal of 3
	([(10, 8), (11, 8), (9, 9)], None), # wider than 2x2
])
def test_flat_top_shapes(top, mean):
	cm = Blob(20, 24, 10, 8)[None, None] * 0.5
	for x, y in top:
		cm[0, 0, y, x] = 0.9
	assert Found(cm) == ([] if mean is None else [(0, 0, round(mean[0], 4), round(mean[1], 4))])


def test_a_tie_next_to_a_higher_pixel_is_no_peak():
	cm = Blob(20, 24, 10, 8)[None, None] * 0.5
	cm[0, 0, 8, 10] = cm[0, 0, 8, 11] = 0.9
	cm[0, 0, 9, 12] = 0.95 # next to the second only
	assert Found(cm) == [(0, 0, 12, 9)]


def test_separate_peaks_all_found_in_sleap_order():
	h, w = 30, 40
	cm = torch.zeros(2, 2, h, w)
	cm[0, 0] = Blob(h, w, 5, 5) + Blob(h, w, 30.5, 20) # second one tied
	cm[0, 1] = Blob(h, w, 20, 10)
	cm[1, 0] = Blob(h, w, 12, 25, sigma=1.5) * 0.5
	cm[1, 1] = Blob(h, w, 2, 3) * 0.1 # under the threshold
	assert Found(cm) == [(0, 0, 5, 5), (0, 1, 20, 10), (0, 0, 30.5, 20), (1, 0, 12, 25)]


def test_edges_and_threshold():
	cm = torch.zeros(1, 1, 6, 6)
	cm[0, 0, 0, 0] = 0.9 # a corner: off-map neighbours don't count
	cm[0, 0, 5, 4] = cm[0, 0, 5, 5] = 0.6 # tied on the last row
	cm[0, 0, 2, 3] = 0.2 # not above the threshold
	assert Found(cm) == [(0, 0, 0, 0), (0, 0, 4.5, 5)]
	assert Found(torch.zeros(1, 1, 6, 6)) == []


def test_the_maps_shape_and_offsets_as_float32():
	cm = torch.rand(3, 2, 9, 11)
	peak, dx, dy = peaks.FlatTopPeaks(cm, 0.2)
	assert peak.shape == dx.shape == dy.shape == cm.shape
	assert peak.dtype == torch.bool and dx.dtype == dy.dtype == torch.float32
	assert not dx[~peak].any() and not dy[~peak].any()


# ---------------------------------------------------------------- against sleap-nn

@pytest.fixture
def sleap():
	pytest.importorskip("sleap_nn")
	from campy import live
	if live.SleapProblem():
		pytest.skip(live.SleapProblem())
	from sleap_nn.inference.ops import peaks as ops
	return ops


def Same(cms, sleap, threshold=0.2):
	ours = Ours(cms, threshold)
	theirs = sleap.find_local_peaks_rough(cms, threshold=threshold)
	for a, b in zip(ours, theirs):
		assert a.dtype == b.dtype and torch.equal(a, b)
	return len(theirs[0])


def test_same_as_sleap_nn_without_ties(sleap):
	torch.manual_seed(0)
	assert Same(torch.rand(4, 3, 40, 50), sleap) > 100


def test_same_as_sleap_nn_on_coarse_maps(sleap):
	# maps of a few levels: flat tops of every shape, wide plateaus, shoulders
	torch.manual_seed(2)
	for levels in (2, 3, 5, 9, 33):
		cms = torch.floor(torch.rand(6, 2, 24, 30) * levels) / (levels - 1)
		Same(cms, sleap, threshold=0.1)


def test_same_as_sleap_nn_on_fp16_animals(sleap):
	# animals' Gaussians in fp16, where ties happen
	torch.manual_seed(1)
	h, w = 64, 80
	cms = torch.zeros(200, 1, h, w)
	for b in range(cms.shape[0]):
		for _ in range(5):
			cx, cy = torch.rand(2) * torch.tensor([w - 10.0, h - 10.0]) + 5
			cms[b, 0] = torch.maximum(cms[b, 0], Blob(h, w, float(cx), float(cy)) * (0.6 + 0.4 * float(torch.rand(1))))
	cms = cms.half()
	assert Same(cms, sleap) > 900
	points = Ours(cms)[0]
	assert (points != torch.floor(points)).any() # ties did happen


def test_integral_refinement_as_sleap_nn(sleap):
	# sleap-nn refines a flat top around the pixel its point floors to; so
	# does FixedCrops (campy/live.py), checked there against the layer
	cm = Blob(20, 24, 10.5, 8)[None, None]
	points = sleap.find_local_peaks(cm, threshold=0.2, refinement="integral")[0]
	assert points.shape == (1, 2) and 10 < points[0, 0].item() < 10.5 and abs(points[0, 1].item() - 8) < 1e-4
