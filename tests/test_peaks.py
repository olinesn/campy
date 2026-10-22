"""
campy/peaks.py: centroid peak finding that keeps a peak whose top is two
equal pixels (fp16 rounding) once, where sleap-nn's strict comparison loses
it. Needs torch; the comparisons with sleap-nn need sleap-nn too.
"""
import pytest

from campy import peaks

torch = pytest.importorskip("torch")


def Blob(h, w, cx, cy, sigma=2.5):
	yy, xx = torch.meshgrid(torch.arange(h, dtype=torch.float32), torch.arange(w, dtype=torch.float32), indexing="ij")
	return torch.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma ** 2))


def Found(cms, threshold=0.2):
	points, vals, samples, channels = peaks.LocalPeaks(cms, threshold)
	return [(int(s), int(c), int(x), int(y)) for (x, y), s, c in zip(points.tolist(), samples, channels)]


@pytest.mark.parametrize("dx, dy", [(1, 0), (0, 1), (1, 1), (-1, 1)])
def test_two_equal_top_pixels_give_one_peak(dx, dy):
	# two equal top pixels, next to each other in each direction, on a
	# lower hill: the first in reading order (row, then column) is kept
	cm = Blob(20, 24, 10, 8)[None, None] * 0.5
	a, b = (10, 8), (10 + dx, 8 + dy)
	for x, y in (a, b):
		cm[0, 0, y, x] = 0.9
	first = min(a, b, key=lambda p: (p[1], p[0]))
	assert Found(cm) == [(0, 0) + first]


def test_a_peak_between_two_pixels():
	cm = Blob(20, 24, 10.5, 8)[None, None]
	assert cm[0, 0, 8, 10] == cm[0, 0, 8, 11]
	assert Found(cm) == [(0, 0, 10, 8)]


def test_flat_two_by_two_top_gives_one_peak():
	cm = Blob(20, 24, 10.5, 8.5)[None, None]
	assert Found(cm) == [(0, 0, 10, 8)]


def test_separate_peaks_all_found_in_sleap_order():
	h, w = 30, 40
	cm = torch.zeros(2, 2, h, w)
	cm[0, 0] = Blob(h, w, 5, 5) + Blob(h, w, 30.5, 20) # second one tied
	cm[0, 1] = Blob(h, w, 20, 10)
	cm[1, 0] = Blob(h, w, 12, 25, sigma=1.5) * 0.5
	cm[1, 1] = Blob(h, w, 2, 3) * 0.1 # under the threshold
	assert Found(cm) == [(0, 0, 5, 5), (0, 1, 20, 10), (0, 0, 30, 20), (1, 0, 12, 25)]


def test_edges_and_threshold():
	cm = torch.zeros(1, 1, 6, 6)
	cm[0, 0, 0, 0] = 0.9 # a corner: off-map neighbours don't count
	cm[0, 0, 5, 4] = cm[0, 0, 5, 5] = 0.6 # tied on the last row
	cm[0, 0, 2, 3] = 0.2 # not above the threshold
	assert Found(cm) == [(0, 0, 0, 0), (0, 0, 4, 5)]
	assert Found(torch.zeros(1, 1, 6, 6)) == []
	points, vals, samples, channels = peaks.LocalPeaks(torch.zeros(1, 1, 6, 6))
	assert points.shape == (0, 2) and vals.shape == (0,) and samples.dtype == channels.dtype == torch.int32


def test_peak_mask_has_the_maps_shape():
	cm = torch.rand(3, 2, 9, 11)
	assert peaks.PeakMask(cm, 0.2).shape == cm.shape


# ---------------------------------------------------------------- against sleap-nn

def SleapPeaks():
	pytest.importorskip("sleap_nn")
	from sleap_nn.inference.ops import peaks as sleap
	return sleap


@pytest.fixture
def sleap():
	ops = SleapPeaks()
	peaks.UseTiedPeaks(False) # sleap-nn's own, whatever an earlier test left
	yield ops
	peaks.UseTiedPeaks(False)


def test_same_as_sleap_nn_without_ties(sleap):
	# random maps in fp32: no two neighbours equal, so the same peaks, values,
	# order and types as sleap-nn's own
	torch.manual_seed(0)
	cm = torch.rand(4, 3, 40, 50)
	ours = peaks.LocalPeaks(cm, 0.2)
	theirs = sleap.find_local_peaks_rough(cm, threshold=0.2)
	assert len(theirs[0]) > 100
	for a, b in zip(ours, theirs):
		assert a.dtype == b.dtype and torch.equal(a, b)


def test_fp16_maps_lose_no_peaks(sleap):
	# animals' Gaussians in fp16: sleap-nn finds a subset of ours; ours adds
	# one exactly where sleap-nn's top was two equal pixels
	torch.manual_seed(1)
	h, w = 64, 80
	cms = torch.zeros(200, 1, h, w)
	for b in range(cms.shape[0]):
		for _ in range(5):
			cx, cy = torch.rand(2) * torch.tensor([w - 10.0, h - 10.0]) + 5
			cms[b, 0] = torch.maximum(cms[b, 0], Blob(h, w, float(cx), float(cy)) * (0.6 + 0.4 * float(torch.rand(1))))
	cms = cms.half()
	ours = set(Found(cms))
	p, _, s, c = sleap.find_local_peaks_rough(cms, threshold=0.2)
	theirs = {(int(si), int(ci), int(x), int(y)) for (x, y), si, ci in zip(p.tolist(), s, c)}
	assert theirs <= ours
	extra = ours - theirs
	assert extra # fp16 ties do happen
	for s_, c_, x, y in extra:
		top = cms[s_, c_, y, x]
		around = cms[s_, c_, max(y - 1, 0):y + 2, max(x - 1, 0):x + 2]
		assert (around == top).sum() >= 2 and (around <= top).all()


def test_use_tied_peaks_patches_find_local_peaks(sleap):
	cm = Blob(20, 24, 10.5, 8)[None, None]
	assert sleap.find_local_peaks(cm, threshold=0.2)[0].shape[0] == 0 # sleap-nn's own loses it
	assert peaks.UseTiedPeaks() and peaks.TiedPeaksInUse()
	points, vals, s, c = sleap.find_local_peaks(cm, threshold=0.2, refinement="integral")
	assert points.shape == (1, 2)
	assert 10.05 < points[0, 0].item() < 10.5 and abs(points[0, 1].item() - 8) < 1e-4 # refined toward the middle
	from sleap_nn.inference.layers import centroid
	assert centroid.find_local_peaks is sleap.find_local_peaks # the centroid layer calls the patched one
	assert peaks.UseTiedPeaks(False) and not peaks.TiedPeaksInUse()
	assert sleap.find_local_peaks(cm, threshold=0.2)[0].shape[0] == 0


@pytest.mark.parametrize("strict", [False, True])
def test_campy_live_uses_tied_peaks_unless_strict(sleap, strict, monkeypatch):
	from campy import live
	monkeypatch.setattr(live, "ExactCropSize", lambda layer: None) # the stub has no layer to crop with

	class Stub(live.TopDown):
		def Build(self, args):
			return None, "stub"
	argv = ["--model", "sleap", "--device", "cpu"] + (["--strict-peaks"] if strict else [])
	model = Stub(live.ParseArgs(argv), 3, (1024, 1280, 1))
	assert model.tiedPeaks == (not strict) and peaks.TiedPeaksInUse() == (not strict)
