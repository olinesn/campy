"""
Peak finding that keeps a peak whose top is two (or more) equal pixels.

sleap-nn (0.3.3) finds the centroids of animals as the local peaks of the
centroid model's confidence map: pixels strictly greater than all 8 of their
neighbours (find_local_peaks_rough). In fp16 (campy-live's default) the map
has about 3 significant digits, and the two top pixels of a peak sometimes
round to the same value: neither is greater than the other, so neither is
kept, and that animal isn't found in that frame. On a 5-mouse replay that
was about 0.7% of the centroids (45-64 of 7,466), always exactly two tied
pixels each; fp32 never ties.

LocalPeaks breaks ties by position instead: a pixel is kept if it is greater
than its 4 neighbours that come before it in reading order (the row above,
and the pixel to its left) and at least equal to its 4 neighbours after it.
Of two equal top pixels, the first in reading order is kept, and the other
is not, so a tied peak is found once. A pixel with no equal neighbour gets
the same answer as sleap-nn's, so every peak sleap-nn finds is found the
same. A flat top of 3 or more pixels shaped so that one of them has no
equal neighbour before it (a V) still gives two peaks next to each other;
that wasn't seen in fp16 maps, which tie in pairs.

UseTiedPeaks puts LocalPeaks in place of sleap-nn's find_local_peaks_rough
for the whole process (campy-live does it unless --strict-peaks), which
find_local_peaks, and so its centroid and bottom-up layers, call. The same
shapes and types come back. The keypoints from the centered-instance model
come from a global peak (an argmax), which ties don't affect.
"""


def PeakMask(cms, threshold):
	"""
	cms: (..., H, W) confidence maps. True where a pixel is a local peak
	above threshold: greater than its neighbours before it in reading order,
	at least equal to those after it. The same shape as cms; no host syncs
	(captures in a CUDA graph, exports to ONNX).
	"""
	import torch
	import torch.nn.functional as F
	h, w = cms.shape[-2:]
	x = cms.reshape(-1, 1, h, w)
	p = F.pad(x, (1, 1, 1, 1), mode="constant", value=float("-inf"))
	before = torch.stack([p[..., :-2, :-2], p[..., :-2, 1:-1], p[..., :-2, 2:], p[..., 1:-1, :-2]]).amax(0)
	after = torch.stack([p[..., 1:-1, 2:], p[..., 2:, :-2], p[..., 2:, 1:-1], p[..., 2:, 2:]]).amax(0)
	mask = (x > before) & (x >= after) & (x > threshold)
	return mask.reshape(cms.shape)


def LocalPeaks(cms, threshold=0.2):
	"""
	sleap-nn's find_local_peaks_rough, with ties kept once (PeakMask).
	cms: (samples, channels, H, W). Returns peak_points (n, 2) x, y float32,
	peak_vals (n,), peak_sample_inds (n,) int32, peak_channel_inds (n,) int32,
	in sleap-nn's order (sample, row, column, channel).
	"""
	import torch
	mask = PeakMask(cms, threshold)
	subs = torch.stack(torch.where(mask.permute(0, 2, 3, 1)), dim=-1)
	vals = cms[subs[:, 0], subs[:, 3], subs[:, 1], subs[:, 2]]
	points = subs[:, [2, 1]].to(torch.float32)
	return points, vals, subs[:, 0].to(torch.int32), subs[:, 3].to(torch.int32)


_saved = {}


def UseTiedPeaks(on=True):
	"""
	on: sleap-nn's find_local_peaks (and the layers that call it) use
	LocalPeaks, in this process. False puts sleap-nn's own back. Returns
	whether it could (False: this sleap-nn has no find_local_peaks_rough).
	"""
	import importlib
	modules = []
	for name in ("sleap_nn.inference.ops.peaks", "sleap_nn.inference.peak_finding"):
		try:
			module = importlib.import_module(name)
		except ImportError:
			continue
		if hasattr(module, "find_local_peaks_rough"):
			modules.append(module)
	for module in modules:
		if module.__name__ not in _saved:
			_saved[module.__name__] = module.find_local_peaks_rough
		module.find_local_peaks_rough = LocalPeaks if on else _saved[module.__name__]
	return bool(modules)


def TiedPeaksInUse():
	try:
		from sleap_nn.inference.ops import peaks
	except ImportError:
		return False
	return peaks.find_local_peaks_rough is LocalPeaks
