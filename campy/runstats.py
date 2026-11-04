"""
Numbers a program that runs for days keeps for its summary's percentiles
(campy-live's latencies, campy-rules' trigger delays), in bounded memory.

A Python list of them grew by about 40 bytes a value, so campy-live's seven
grew by ~1 GB a day at 60 sets/s. Values keeps them in numpy blocks, 8 bytes
each, up to EXACT of them (8 MB; at 60 a second, ~4.9 hours): up to there its
percentiles are numpy's own over every value, the same to the last bit as
from a list. Past it, the oldest blocks are folded into a histogram whose
bins are 0.1% wide (and 1/1000 of `floor` wide under `floor`), so the memory
stops growing (~350 KB more), and its percentiles over the whole run are
within half a bin of the exact ones. The count, mean, max and min stay exact
(the mean to float64's rounding). The recent values (a progress line's
window) are always kept as they are. Every value is still in the program's
own log, for exact numbers afterwards.

Add and the readers may run in different threads (campy-live's 3D thread
adds, its main thread reports): a lock keeps them apart.
"""

import math, threading

import numpy as np

BLOCK = 4096 # values a block holds
EXACT = 1 << 20 # values kept as they are; past it, the oldest blocks go into the histogram
RATIO = 1.001 # each bin of the histogram 0.1% wider than the last
LINEAR = 1000 # bins under floor (its magnitudes in 1/1000 steps)
DECADES = 9 # from floor to floor * 1e9: 1 us to 1000 s for ns (floor 1000), or for ms (floor 0.001)
LOG_BINS = int(math.ceil(DECADES * math.log(10) / math.log(RATIO)))
HALF = LINEAR + LOG_BINS # bins for values >= 0; the same, mirrored, for those below 0


class Values:
	def __init__(self, dtype=np.int64, floor=1000):
		self.dtype = np.dtype(dtype)
		self.floor = float(floor)
		self.lock = threading.Lock()
		self.blocks = [] # full blocks, oldest first, not folded
		self.block = np.empty(BLOCK, self.dtype)
		self.fill = 0
		self.count = 0
		self.folded = 0 # how many of the first values are in the histogram, not kept
		self.hist = None
		self.total = 0.0 # of the folded values: their sum, max, min
		self.high = self.low = None

	def __len__(self):
		return self.count

	def Add(self, value):
		with self.lock:
			self.block[self.fill] = value
			self.fill += 1
			self.count += 1
			if self.fill == BLOCK:
				self.blocks.append(self.block)
				self.block = np.empty(BLOCK, self.dtype)
				self.fill = 0
				if len(self.blocks) * BLOCK > EXACT:
					self.Fold(self.blocks.pop(0))

	def Fold(self, values):
		if self.hist is None:
			self.hist = np.zeros(2 * HALF, np.int64)
		self.hist += np.bincount(self.Bins(values), minlength=2 * HALF)
		self.total += float(np.sum(values, dtype=np.float64))
		high, low = values.max(), values.min()
		self.high = high if self.high is None else max(self.high, high)
		self.low = low if self.low is None else min(self.low, low)
		self.folded += len(values)

	def Bins(self, values):
		x = np.asarray(values, np.float64)
		a = np.abs(x)
		step = self.floor / LINEAR
		with np.errstate(divide="ignore", invalid="ignore"):
			logs = LINEAR + np.floor(np.log(np.maximum(a, self.floor) / self.floor) / math.log(RATIO))
		i = np.where(a < self.floor, np.floor(a / step), logs)
		i = np.clip(np.nan_to_num(i, nan=0.0), 0, HALF - 1).astype(np.int64)
		return np.where(x < 0, HALF - 1 - i, HALF + i)

	def Center(self, bins):
		# A bin's middle value
		bins = np.asarray(bins, np.int64)
		i = np.where(bins >= HALF, bins - HALF, HALF - 1 - bins).astype(np.float64)
		step = self.floor / LINEAR
		value = np.where(i < LINEAR, (i + 0.5) * step, self.floor * RATIO ** (i - LINEAR + 0.5))
		return np.where(bins >= HALF, value, -value)

	def Kept(self):
		# The values kept as they are, oldest first (callers hold the lock)
		return np.concatenate(self.blocks + [self.block[:self.fill]])

	def Since(self, since=0):
		"""The values from the since-th on, as they are (only those still kept, past EXACT)."""
		with self.lock:
			kept, folded = self.Kept(), self.folded
		return kept[max(0, since - folded):]

	def Exact(self):
		"""Every value, as they are, or None once some are only in the histogram."""
		with self.lock:
			return None if self.folded else self.Kept()

	def Approx(self, percentiles):
		"""
		{"n", "mean", "max", "min", <each percentile>: value} over every value,
		the percentiles from the histogram (with the kept values added): within
		half a bin, interpolated as numpy's are, between the max and the min.
		"""
		with self.lock:
			kept = self.Kept()
			hist = self.hist.copy() if self.hist is not None else np.zeros(2 * HALF, np.int64)
			total, high, low, n = self.total, self.high, self.low, self.count
		if len(kept):
			hist += np.bincount(self.Bins(kept), minlength=2 * HALF)
			total += float(np.sum(kept, dtype=np.float64))
			high = kept.max() if high is None else max(high, kept.max())
			low = kept.min() if low is None else min(low, kept.min())
		if not n:
			return None
		ends = np.cumsum(hist) # the rank after each bin's last value
		out = {"n": n, "mean": total / n, "max": float(high), "min": float(low)}
		for q in percentiles:
			at = q / 100 * (n - 1) # numpy's linear interpolation between the values ranked floor(at) and ceil(at)
			lo, hi = (float(v) for v in self.Center(np.searchsorted(ends, [math.floor(at), math.ceil(at)], "right")))
			out[q] = min(float(high), max(float(low), lo + (at - math.floor(at)) * (hi - lo)))
		return out
