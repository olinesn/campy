"""
Centroid peak finding with fixed shapes, by sleap-nn's own rule (0.3.5 on).

sleap-nn finds the centroids of animals as the local peaks of the centroid
model's confidence map. Up to 0.3.4 a peak had to be one pixel strictly
greater than all 8 of its neighbours. In fp16 (campy-live's default) the map
has about 3 significant digits, and the two top pixels of a peak sometimes
round to the same value: neither was kept, and that animal wasn't found in
that frame (about 0.7% of the centroids of a 5-mouse replay;
talmolab/sleap-nn#781). sleap-nn 0.3.5 fixed it (#782): a flat top of equal
pixels is one peak if it fits in a 2x2 block (2 pixels, an L of 3, or all
4) and every pixel around it is lower, and the peak is at the mean of its
pixels (so it can be half a pixel, or a third, off the grid). A wider flat
top, or one next to a higher pixel, gives no peak. A pixel with no equal
neighbour gets the same answer as before.

campy-live's layer (sleap-nn's own TopDownLayer) uses sleap-nn's peak
finding as it is. FixedCrops (campy/live.py) finds the peaks itself, with
fixed shapes and no wait for the host, so it can run in one CUDA graph:
FlatTopPeaks is that rule as a whole-map computation, the way sleap-nn's
exported models do it (#785, BaseExportWrapper._local_peaks), so FixedCrops
finds the same peaks at the same points as the layer.

Before sleap-nn 0.3.5, campy patched sleap-nn's peak finding with its own
tie-break (pull request 132: keep the first of two equal pixels). campy
needs sleap-nn 0.3.5 or later now (campy/live.py: SleapProblem), and that
patch is gone.
"""


def FlatTopPeaks(cms, threshold):
	"""
	cms: (..., H, W) confidence maps. Returns (peak, dx, dy), each the shape
	of cms: whether a peak is marked at the pixel (the first pixel of its
	flat top in reading order, which has the top's value), and the offset in
	x and y (float32) from that pixel to the peak's point, the mean of the
	top's pixels (0 for a one-pixel top). sleap-nn's find_local_peaks_rough
	(0.3.5 on) finds the same peaks, in the same order, at the same points.
	No host syncs (captures in a CUDA graph, exports to ONNX).
	"""
	import torch
	import torch.nn.functional as F
	p = F.pad(cms, (1, 1, 1, 1), mode="constant", value=float("-inf"))

	def At(padded, dr, dc): # each pixel's (dr, dc) neighbour, in a 1-pixel border
		return padded[..., 1 + dr:(dr - 1) or None, 1 + dc:(dc - 1) or None]

	before = torch.maximum(torch.maximum(At(p, -1, -1), At(p, -1, 0)), torch.maximum(At(p, -1, 1), At(p, 0, -1)))
	after = torch.maximum(torch.maximum(At(p, 0, 1), At(p, 1, -1)), torch.maximum(At(p, 1, 0), At(p, 1, 1)))
	# greater than the 4 neighbours before it, at least equal to the 4 after:
	# every flat top has one such pixel, its first
	peak = (cms > before) & (cms >= after) & (cms > threshold)
	# each pixel's count of neighbours at least as high as it
	ge = torch.zeros_like(cms, dtype=torch.float32)
	for dr in (-1, 0, 1):
		for dc in (-1, 0, 1):
			if dr or dc:
				ge = ge + (At(p, dr, dc) >= cms).float()
	ge = F.pad(ge, (1, 1, 1, 1))
	later = {"E": (0, 1), "SW": (1, -1), "S": (1, 0), "SE": (1, 1)}
	tied = {k: At(p, *d) == cms for k, d in later.items()}
	t = {k: v.float() for k, v in tied.items()}
	n = t["E"] + t["SW"] + t["S"] + t["SE"] + 1 # the top's pixels
	# it fits in a 2x2 block unless it has SW and also E or SE; it is closed
	# (no pixel around it as high) when each of its pixels has n - 1 such
	# neighbours, the rest of the top
	peak = peak & ~(tied["SW"] & (tied["E"] | tied["SE"]))
	for k, d in later.items():
		peak = peak & (~tied[k] | (At(ge, *d) == n - 1))
	zero = torch.zeros_like(n)
	dx = torch.where(peak, (t["E"] + t["SE"] - t["SW"]) / n, zero)
	dy = torch.where(peak, (t["SW"] + t["S"] + t["SE"]) / n, zero)
	return peak, dx, dy
