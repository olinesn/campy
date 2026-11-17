"""
A live sorter for campy/ephyslive.py, trained on a Kilosort 4 sort of a
recording of the same probe (its phy folder: as Kilosort left it, or
curated in phy).

Training (campy-ephys train): the recording is replayed through the live
filter and spike finder, exactly as live (the same samples give the same
spikes, however they come in pieces). Each kept unit (phy's cluster_group.tsv
labels, else Kilosort's cluster_KSLabel.tsv; "good" and "mua" by default) gets
a template: the median of its spikes' live-filtered waveforms on its 12
nearest sites, at Kilosort's spike times moved to where the live filter puts
the trough (so a unit that rarely crosses the threshold has one too).
Kilosort's own templates are whitened and filtered forwards and backwards:
not what the live view sees. Each live spike within a few samples of a kept
unit's spike, on a site near its peak site, is that unit's; each unit's
acceptance is how far those spikes fall from its template (or, for a unit
with too few, its spikes at Kilosort's times). The file keeps everything with
what it was trained on.

Sorting (TemplateSorter, ephyslive's sorter): each live spike is compared
with the units whose sites include its channel, on the sites they share
(its waveform on its 12 nearest sites, theirs on their own): the template is
scaled to the spike (0.5 to 2 times), and the residual, per sample and
divided by each site's noise, is the distance. The spike takes the closest
unit if it is within that unit's acceptance, and stays -1 (not sorted)
otherwise.

What it can't do: units the sort didn't keep stay unsorted; the probe's drift
since the sort isn't followed (no drift correction: train again on a recent
recording); two spikes within 1.5 ms of each other nearby are one live spike
(the spike finder's), sorted as the bigger.

Evaluation (campy-ephys evaluate): the same replay over another span of the
recording, sorted with the templates, against Kilosort's sort there: for
each unit, the share of its spikes found live and given to it, and the share
of the live spikes given to it that are its.
"""

import os, time

import numpy as np

from campy import ephyslive as el

LABELS = ("good", "mua") # the units kept by default (phy's or Kilosort's label): mua units' templates take their own
# spikes, which good units' would otherwise take (on Kilosort's sample: 8% of the spikes of no good unit given one,
# against 42% with good units' templates alone)
TOLERANCE = 10 # samples: a live spike this close to a Kilosort spike is that spike
RADIUS_UM = 60.0 # and on a site this close to its unit's peak site
MIN_SPIKES = 20 # a unit with fewer live spikes in the training span has no template
MAX_SPIKES = 500 # a template is the median of at most this many spikes
ACCEPT_Q = 0.99 # a unit's acceptance: this quantile of its own training spikes' distances
SCALE = (0.5, 2.0) # the template scaled to a spike within these
MIN_SITES = 6 # sites a spike and a unit must share to be compared


# ---------------------------------------------------------------- Kilosort's output (phy's folder)

def ReadPhy(folder, labels=LABELS):
	"""Kilosort's spikes (times in the recording's samples, from 0) and units, with each unit's label (phy's
	cluster_group.tsv if there is one, else cluster_KSLabel.tsv): {"times", "clusters", "labels": {unit: label},
	"kept": [units with a label in labels], "label_file"}."""
	times = np.load(os.path.join(folder, "spike_times.npy")).ravel().astype(np.int64)
	clusters = np.load(os.path.join(folder, "spike_clusters.npy")).ravel().astype(np.int64)
	found = {}
	for name in ("cluster_group.tsv", "cluster_KSLabel.tsv"):
		path = os.path.join(folder, name)
		if os.path.isfile(path):
			with open(path) as f:
				next(f, None)
				for line in f:
					parts = line.rstrip("\n").split("\t")
					if len(parts) >= 2 and parts[0].strip().lstrip("-").isdigit():
						found[int(parts[0])] = parts[1].strip()
			label_file = name
			break
	else:
		label_file = None
	units = np.unique(clusters)
	kept = [int(u) for u in units if found.get(int(u), "unsorted") in labels]
	return {"times": times, "clusters": clusters, "labels": {int(u): found.get(int(u), "unsorted") for u in units},
		"kept": kept, "label_file": label_file}


# ---------------------------------------------------------------- the live replay

def Replay(data, stream, start, end, piece_s=1.0, keep=True):
	"""The samples [start, end) of data (int16 [samples, channels]) through the live filter and spike finder, in
	pieces of piece_s: (filtered float32 [end-start, channels], or None without keep (minutes of a probe: tens of GB),
	Spikes (samples counted from 0 in data), the detector)."""
	f, d = el.Filter(stream), el.Detector(stream)
	n = int(stream.rate * piece_s)
	filtered = np.empty((end - start, data.shape[1]), np.float32) if keep else None
	found = []
	for a in range(start, end, n):
		b = min(end, a + n)
		x = f.Apply(np.asarray(data[a:b]))
		if keep:
			filtered[a - start:b - start] = x
		found.append(d.Feed(a, x))
	keys = ("sample", "channel", "amplitude")
	spikes = el.Spikes(*(np.concatenate([getattr(s, k) for s in found]) for k in keys),
		waveforms=np.concatenate([s.waveforms for s in found]), neighbors=np.concatenate([s.neighbors for s in found]))
	return filtered, spikes, d


def Match(spikes, geometry, times, clusters, peaks, tolerance=TOLERANCE, radius_um=RADIUS_UM):
	"""Each live spike's Kilosort unit (-1: none): the unit of the nearest Kilosort spike in time, within tolerance
	samples, whose unit's peak site is within radius_um of the live spike's site. Also, for each Kilosort spike,
	the live spike that took it (-1: none)."""
	order = np.argsort(spikes.sample)
	s, c = spikes.sample[order], spikes.channel[order]
	best_dt = np.full(len(s), tolerance + 1, np.int64)
	unit = -np.ones(len(s), np.int64)
	taken = -np.ones(len(times), np.int64)
	lo = np.searchsorted(s, times - tolerance)
	hi = np.searchsorted(s, times + tolerance, side="right")
	for i in np.flatnonzero(hi > lo):
		k = np.arange(lo[i], hi[i])
		p = peaks[clusters[i]]
		near = np.hypot(geometry.x[c[k]] - geometry.x[p], geometry.z[c[k]] - geometry.z[p]) <= radius_um
		if not near.any():
			continue
		k = k[near]
		dt = np.abs(s[k] - times[i])
		j = k[np.argmin(dt)]
		taken[i] = order[j]
		if dt.min() < best_dt[j]:
			best_dt[j], unit[j] = dt.min(), clusters[i]
	out = -np.ones(len(s), np.int64)
	out[order] = unit
	return out, taken


def PeakSites(filtered, geometry, times, clusters, units, start, most=300):
	"""Each unit's peak site: where the mean of its spikes' live-filtered waveforms swings most."""
	peaks = {}
	for u in units:
		t = times[clusters == u] - start
		t = t[(t >= el.BEFORE) & (t < len(filtered) - el.AFTER)]
		if len(t) == 0:
			continue
		t = t[np.linspace(0, len(t) - 1, min(most, len(t))).astype(int)]
		mean = np.mean([filtered[i - el.BEFORE:i + el.AFTER] for i in t], axis=0)
		swing = np.ptp(mean, axis=0)
		swing[~geometry.used] = -1
		peaks[u] = int(np.argmax(swing))
	return peaks


def TroughShift(filtered, times, peak, most=300, reach=8):
	"""Samples from Kilosort's spike time to the live trough: where the mean of the unit's live-filtered spikes at
	its peak site is lowest, within reach samples."""
	t = times[(times >= el.BEFORE + reach) & (times < len(filtered) - el.AFTER - reach)]
	if not len(t):
		return 0
	t = t[np.linspace(0, len(t) - 1, min(most, len(t))).astype(int)]
	mean = np.mean([filtered[i - reach:i + reach + 1, peak] for i in t], axis=0)
	return int(np.argmin(mean)) - reach


# ---------------------------------------------------------------- the sorter

def Distance(wave, nb, template, sites, noise):
	"""A spike's distance to a unit: on the sites they share, the template scaled to the spike, the residual's mean
	square over samples and sites, each site's divided by its noise squared. (distance, scale), or (None, None)
	when they share fewer than MIN_SITES sites or the scale is outside SCALE."""
	common, i_spike, i_unit = np.intersect1d(nb, sites, return_indices=True)
	common_ok = common >= 0
	i_spike, i_unit = i_spike[common_ok], i_unit[common_ok]
	if len(i_spike) < MIN_SITES:
		return None, None
	w = wave[:, i_spike]
	t = template[:, i_unit]
	tt = float((t * t).sum())
	if tt <= 0:
		return None, None
	a = float((w * t).sum()) / tt
	if not SCALE[0] <= a <= SCALE[1]:
		return None, None
	n = noise[sites[i_unit]]
	return float((((w - a * t) / n) ** 2).mean()), a


class TemplateSorter:
	"""campy/ephyslive.py's sorter, from a file campy-ephys train made: see the module's notes."""
	name = "Kilosort 4 templates"

	def __init__(self, path):
		f = np.load(path, allow_pickle=False)
		self.path = path
		self.units = f["units"]
		self.labels = f["labels"]
		self.peaks = f["peaks"]
		self.sites = f["sites"] # [units, NEIGHBORS]
		self.templates = f["templates"] # [units, 61, NEIGHBORS]
		self.accept = f["accept"]
		self.noise = f["noise"] # uV, each channel's
		self.trained = int(f["trained_spikes"].sum()) if "trained_spikes" in f else None
		self.n_train = f["trained_spikes"] if "trained_spikes" in f else np.zeros(len(self.units), int)
		n = int(self.sites.max()) + 1 if self.sites.size else 0
		self.by_channel = [[] for _ in range(max(n, len(self.noise)))]
		for k, s in enumerate(self.sites):
			for ch in s[s >= 0]:
				self.by_channel[ch].append(k)
		self.name = "Kilosort 4 templates ({} units)".format(len(self.units))

	def Assign(self, spikes):
		unit = -np.ones(len(spikes), np.int32)
		for i in range(len(spikes)):
			ch = int(spikes.channel[i])
			if ch >= len(self.by_channel):
				continue
			best, best_k = None, None
			for k in self.by_channel[ch]:
				d, a = Distance(spikes.waveforms[i], spikes.neighbors[i], self.templates[k], self.sites[k], self.noise)
				if d is not None and (best is None or d < best):
					best, best_k = d, k
			if best is not None and best <= self.accept[best_k]:
				unit[i] = self.units[best_k]
		spikes.unit = unit
		return spikes

	def Units(self):
		return [{"unit": int(u), "channel": int(p), "label": str(l), "trained_spikes": int(n)} for u, p, l, n in
			zip(self.units, self.peaks, self.labels, self.n_train)]


# ---------------------------------------------------------------- spikes found by the templates (below the threshold)

Z_THRESHOLD = 7.0 # a template spike: the template explains the data this many noise units (its matched filter's score)
COMPONENTS = 8 # the templates' shared shapes in time (their SVD): each template is kept to these (less noise)
MATCH_W = 20 # samples (0.67 ms): one spike of a unit, and of units near it, within this many
DEDUP_W = 10 # samples: a threshold crossing this close to a template spike (on its sites) is that spike
PASSES = 3 # find, take the spikes' templates away, look again: up to this many times (two units' spikes at once)
PAIR_SEARCH = 6 # samples: another unit's spike at once is looked for this far either side of its own score's peak
GROUP = 3 # units whose spikes at once are fitted together (three units spiking within MATCH_W, on sites they share)
SCALE_ALONE_MAX = 4.0 # a peak's size fitted alone may be up to this before the group's fit (alone, the first unit's
# template takes in the others' spikes); after it, each size must be within SCALE
REACH = 2 * (el.BEFORE + el.AFTER) + 4 * MATCH_W # samples a pass's decisions reach (a subtraction, and its neighbors')
BLOCK = 2048 # samples a transform: the scores are worked out this many (less 60) at a time
JUDGE = 3000 # samples (0.1 s): the template spikes are judged once at least this many more can be (each judgment
# works its margins out again: fewer, longer judgments cost less; the card plays 0.3 s behind)


def Cat(parts):
	"""Spikes, the parts put together in the order of their samples."""
	parts = [p for p in parts if len(p)]
	if not parts:
		return el.Spikes()
	s = el.Spikes(np.concatenate([p.sample for p in parts]), np.concatenate([p.channel for p in parts]),
		np.concatenate([p.amplitude for p in parts]), np.concatenate([p.unit for p in parts]),
		np.concatenate([p.waveforms for p in parts]), np.concatenate([p.neighbors for p in parts]),
		np.concatenate([p.frame for p in parts]))
	return Take(s, np.argsort(s.sample, kind="stable"))


def Take(s, which):
	return el.Spikes(s.sample[which], s.channel[which], s.amplitude[which], s.unit[which], s.waveforms[which],
		s.neighbors[which], s.frame[which])


class TemplateMatcher:
	"""
	Spikes found by the templates themselves, as Kilosort finds them: a small
	unit whose trough never crosses the threshold still fits its template.

	Each unit's template, each site's divided by its noise and kept to the
	templates' COMPONENTS shared shapes in time (their SVD, which also takes
	most of the noise out of a median of few spikes), is matched with the
	filtered samples, each site's divided by its noise (the matched filter,
	in the frequency domain: one transform a site, then each unit's sites
	weighted and transformed back). Its score, in noise units, says how much
	of the data the template explains at each sample, and its scale how big
	the spike is next to the template. A spike of unit u is at a sample where
	u's score is above z, its scale within SCALE, the highest of u's within
	MATCH_W samples, and no unit near it (its peak site among u's sites) has a
	higher score within MATCH_W samples.

	Units near a spike found that also peak within MATCH_W are fitted together
	with it (all their templates' sizes at once, by least squares, at the lags
	that fit best), up to GROUP units: alone, the first unit's fit takes in the
	others' spikes on the sites they share, and leaves too little of them to
	be found. Then, as Kilosort does, each spike found has its template (at
	its size) taken away from the data, and the rest is looked at again, up
	to PASSES times. A unit has no second spike within MATCH_W samples of one
	it already has.

	Each sample's scores are worked out once, as its samples come. Taking a
	spike away changes the scores of the units that share its sites by the
	templates' cross-correlations (worked out once, at the start), as in
	Kilosort, and the group fits are made from the scores and the same
	cross-correlations; so nothing is matched twice.

	Like the threshold's spike finder, a sample is judged once all the samples
	it needs have come (each pass reaching REACH further), so the same samples
	give the same spikes however they come in pieces.
	"""

	def __init__(self, sorter, z=Z_THRESHOLD, components=COMPONENTS):
		import scipy.fft
		self.fft = scipy.fft
		self.z = z
		self.units, self.peaks, self.sites = sorter.units, sorter.peaks, sorter.sites
		self.noise = sorter.noise.astype(np.float32)
		U, L = len(self.units), el.BEFORE + el.AFTER
		ok = self.sites >= 0
		self.cols = np.where(ok, self.sites, 0) # a site missing (-1: a probe with fewer channels) as channel 0, weight 0
		Tn = sorter.templates / self.noise[self.cols][:, None, :] * ok[:, None, :] # [units, 61, sites], noise units
		shapes = Tn.transpose(0, 2, 1).reshape(-1, L)
		_, _, vt = np.linalg.svd(shapes, full_matrices=False)
		W = vt[:components].T # [61, K]
		self.Tn = np.einsum("tk,sk,usj->utj", W, W, Tn).astype(np.float32) # each template as its shapes give it
		self.norm2 = np.maximum((self.Tn.astype(np.float64) ** 2).sum(axis=(1, 2)), 1e-12) # its energy
		self.root = np.sqrt(self.norm2)
		self.near = [np.flatnonzero(np.isin(self.peaks, s)) for s in self.sites] # units near each: their peak among its sites
		self.nearmat = np.zeros((U, U), bool) # [u, v]: v near u
		for u, v in enumerate(self.near):
			self.nearmat[u, v] = True
		# The units whose sites each unit's share, and their templates' cross-correlations: K[u][i, d + 60] is unit
		# over[u][i]'s score at t + d lowered by a spike of u at t, of size 1, taken away
		channels = max(len(self.noise), int(self.cols.max()) + 1 if self.cols.size else 0)
		member = np.zeros((U, channels), bool)
		member[np.repeat(np.arange(U), ok.sum(axis=1)), self.sites[ok]] = True
		self.over = [np.flatnonzero(member[:, self.sites[u][ok[u]]].any(axis=1)) for u in range(U)]
		n = 1 << int(np.ceil(np.log2(2 * L - 1)))
		F = np.fft.rfft(self.Tn.astype(np.float64), n=n, axis=1).transpose(0, 2, 1) # [units, sites, freq]
		self.cross, self.pos = [], -np.ones((U, U), np.int32) # pos[u, v]: v's row in cross[u] (-1: none)
		for u in range(U):
			mine = np.zeros((channels, F.shape[2]), complex)
			np.add.at(mine, self.cols[u], F[u])
			V = self.over[u]
			c = np.fft.irfft((mine[self.cols[V]] * np.conj(F[V])).sum(axis=1), n=n, axis=1) # sum_n Tn_u[n+d] Tn_v[n]
			self.cross.append(np.concatenate([c[:, n - (L - 1):], c[:, :L]], axis=1).astype(np.float32))
			self.pos[u, V] = np.arange(len(V))
		self.filters = {}
		self.passes = PASSES
		self.judge = JUDGE
		self.Reset()

	def Reset(self):
		self.tail, self.tail_first, self.next = None, None, None
		self.S, self.s_first = None, None

	def Feed(self, first, x, last=False):
		"""Template spikes judged with the samples given (filtered uV); as Detector.Feed. last: judge all that can be,
		however few (the end of a replay)."""
		if self.tail is not None and first != self.tail_first + len(self.tail):
			self.Reset()
		if self.tail is None:
			buf, bfirst, self.next = x, first, first
		else:
			buf, bfirst = np.concatenate([self.tail, x]), self.tail_first
		# The scores of the samples that now have all they need, once each
		start = bfirst + el.BEFORE if self.S is None else self.s_first + self.S.shape[1]
		end = bfirst + len(buf) - el.AFTER + 1
		if end > start:
			new = self.Scores(buf, start - bfirst, end - bfirst)
			if self.S is None:
				self.S, self.s_first = new, start
			else:
				self.S = np.concatenate([self.S, new], axis=1)
		w = MATCH_W
		reach = self.passes * REACH + 2 * w + 1
		margin_lo, margin_hi = el.BEFORE + reach, el.AFTER + reach
		lo = max(self.next - bfirst, margin_lo)
		hi = len(buf) - margin_hi
		if hi - lo >= (1 if last else self.judge):
			found = self.Judge(buf, bfirst, lo, hi)
			self.next = bfirst + hi
		else:
			found = el.Spikes()
		keep = max(0, self.next - bfirst - margin_lo)
		self.tail, self.tail_first = buf[keep:], bfirst + keep
		if self.S is not None and self.tail_first + el.BEFORE > self.s_first:
			drop = self.tail_first + el.BEFORE - self.s_first
			self.S, self.s_first = self.S[:, drop:], self.s_first + drop
		return found

	def Flush(self):
		"""The template spikes of all that can be judged, however few samples (the end of a replay)."""
		if self.tail is None:
			return el.Spikes()
		return self.Feed(self.tail_first + len(self.tail), self.tail[:0], last=True)

	def Filters(self, n):
		"""Each unit's matched filter on its sites, transformed for blocks of n samples: [units, sites, n // 2 + 1]."""
		H = self.filters.get(n)
		if H is None:
			H = np.ascontiguousarray(np.conj(self.fft.rfft(self.Tn, n=n, axis=1)).transpose(0, 2, 1)).astype(np.complex64)
			self.filters[n] = H
		return H

	def Scores(self, x, a0, b0):
		"""Each unit's score, not yet divided by its template's energy, at samples [a0, b0) of x (filtered uV), whose
		samples a0 - BEFORE to b0 + AFTER - 1 must be there: [units, b0 - a0]."""
		L = el.BEFORE + el.AFTER
		out = np.empty((len(self.units), b0 - a0), np.float32)
		s = a0
		while s < b0:
			n = min(BLOCK, 1 << int(np.ceil(np.log2(b0 - s + L - 1))))
			e = min(b0, s + n - (L - 1))
			H = self.Filters(n)
			X = self.fft.rfft((x[s - el.BEFORE:e + el.AFTER - 1] / self.noise).T, n=n, axis=1) # [channels, freq]
			Y = X[self.cols[:, 0]] * H[:, 0]
			for j in range(1, self.cols.shape[1]):
				Y += X[self.cols[:, j]] * H[:, j]
			out[:, s - a0:e - a0] = self.fft.irfft(Y, n=n, axis=1)[:, :e - s]
			s = e
		return out

	def Subtract(self, R, a0, t, u, a):
		"""Unit u's template at t, of size a, taken away: the scores R (whose first sample is a0) of the units that share
		its sites lowered by their cross-correlations."""
		L = el.BEFORE + el.AFTER
		i0 = t - (L - 1) - a0
		lo, hi = max(0, i0), min(R.shape[1], i0 + 2 * L - 1)
		if hi > lo:
			R[self.over[u], lo:hi] -= a * self.cross[u][:, lo - i0:hi - i0]

	def Candidates(self, z, scale, c0, c1, taken):
		"""Pass's spikes among samples [c0, c1) (the scores' first is c0): (sample, unit) of each, judged where all
		it needs is known ([c0 + 2 MATCH_W, c1 - 2 MATCH_W)); none for a unit within MATCH_W of one it has (taken).
		Also all units' peaks, by sample (cand: their samples and units), for Group."""
		from scipy.ndimage import maximum_filter1d
		w = MATCH_W
		above = (z > self.z) & (scale >= SCALE[0]) & (scale <= SCALE_ALONE_MAX)
		rows = np.flatnonzero(above.any(axis=1))
		peak = np.zeros_like(above)
		if len(rows):
			zr = z[rows]
			peak[rows] = above[rows] & (zr >= maximum_filter1d(zr, 2 * w + 1, axis=1, mode="nearest"))
		us, ts = np.nonzero(peak) # by unit, then sample
		keep = np.ones(len(ts), bool)
		for u, mine in taken.items(): # not a second spike of a unit within MATCH_W of one it has: what is left of
			# another unit's spike under its template doesn't count, nor hide that unit's
			i, j = np.searchsorted(us, [u, u + 1])
			if j > i:
				keep[i:j] = np.abs((c0 + ts[i:j])[:, None] - mine[None, :]).min(axis=1) > w
		us, ts = us[keep], ts[keep]
		# Not the best near it: a unit near it (its peak among its sites) peaks higher within MATCH_W (the lower unit
		# number, if as high)
		zs = z[us, ts]
		order = np.argsort(ts, kind="stable")
		t_o, u_o, z_o = ts[order], us[order], zs[order]
		lose = np.zeros(len(ts), bool)
		for k in range(1, len(ts)):
			i = np.flatnonzero(t_o[k:] - t_o[:-k] <= w)
			if not len(i):
				break
			j = i + k
			ui, uj, zi, zj = u_o[i], u_o[j], z_o[i], z_o[j]
			other = ui != uj
			lose[i[other & self.nearmat[ui, uj] & ((zj > zi) | ((zj == zi) & (uj < ui)))]] = True
			lose[j[other & self.nearmat[uj, ui] & ((zi > zj) | ((zi == zj) & (ui < uj)))]] = True
		best = np.zeros(len(ts), bool)
		best[order] = ~lose
		ok = best & (ts >= 2 * w) & (ts < c1 - c0 - 2 * w)
		return [(c0 + int(t), int(u), float(scale[u, t])) for u, t in zip(us[ok], ts[ok])], (c0 + t_o, u_o)

	def Group(self, R, a0, t, u, cand, paired, accepted):
		"""Other units' spikes at once with unit u's at t: units near the group (their peak among a member's sites)
		peaking within MATCH_W of t (this pass's candidates, not accepted on their own) are added one at a time, the
		one that adds most first, at the lag near its peak that fits best, with all the group's templates' sizes
		fitted together on the sites they cover; one is added when every size stays within SCALE and it adds at least
		the score's threshold squared to what the group explained, up to GROUP units. [(sample, unit, size), ...] for
		a group of two or more; None for u alone."""
		w = MATCH_W
		group = [(t, u)]
		alone = R[u, t - a0] / self.norm2[u]
		fit = (np.array([alone]), alone * R[u, t - a0])
		explained = fit[1]
		peaks_t, peaks_u = cand
		i, j = np.searchsorted(peaks_t, [t - w, t + w + 1])
		peaks_t, peaks_u = peaks_t[i:j], peaks_u[i:j] # this pass's peaks within MATCH_W of t
		while len(group) < GROUP:
			members = [m for _, m in group]
			near = self.nearmat[members][:, peaks_u].any(axis=0)
			for m in members:
				near &= peaks_u != m
			trials = []
			for peak, v in zip(peaks_t[near].tolist(), peaks_u[near].tolist()):
				if (peak, v) in accepted:
					continue
				# Its own score's peak is pulled by the others' spikes: the lag that fits best, near it
				for tv in range(max(t - w, peak - PAIR_SEARCH), min(t + w, peak + PAIR_SEARCH) + 1):
					if (tv, v) not in paired:
						trials.append((tv, v))
			if not trials:
				break
			sizes, together = self.JointFits(R, a0, group, trials)
			gain = together - explained
			ok = (gain > self.z ** 2) & np.all((sizes >= SCALE[0]) & (sizes <= SCALE[1]), axis=1)
			if not ok.any():
				break
			k = int(np.argmax(np.where(ok, gain, -np.inf))) # the first of the best, as tried in turn
			group.append(trials[k])
			explained = together[k]
			fit = (sizes[k], together[k])
		if len(group) == 1:
			return None
		for tv, v in group[1:]:
			paired.add((tv, v))
		return [(tt, uu, float(a)) for (tt, uu), a in zip(group, fit[0])]

	def JointFits(self, R, a0, group, trials):
		"""JointFit of the group with each trial ([(sample, unit), ...]) added, all at once: (sizes [trials, len(group)
		+ 1], what they explain [trials]); a trial whose templates can't be told apart explains -inf."""
		L = el.BEFORE + el.AFTER
		k, n = len(group) + 1, len(trials)
		M, b = np.zeros((n, k, k)), np.zeros((n, k))
		for i, (ti, ui) in enumerate(group):
			b[:, i] = R[ui, ti - a0]
			M[:, i, i] = self.norm2[ui]
			for j in range(i + 1, k - 1):
				tj, uj = group[j]
				p = self.pos[ui, uj]
				if p >= 0 and abs(tj - ti) < L:
					M[:, i, j] = M[:, j, i] = self.cross[ui][p, tj - ti + L - 1]
		tv = np.array([tt for tt, _ in trials])
		v = np.array([uu for _, uu in trials])
		b[:, k - 1] = R[v, tv - a0]
		M[:, k - 1, k - 1] = self.norm2[v]
		for i, (ti, ui) in enumerate(group):
			p = self.pos[ui, v]
			has = (p >= 0) & (np.abs(tv - ti) < L)
			c = np.zeros(n)
			c[has] = self.cross[ui][p[has], (tv - ti + L - 1)[has]]
			M[:, i, k - 1] = M[:, k - 1, i] = c
		if k == 2: # two templates: in closed form (most groups)
			m00, m01, m11 = M[:, 0, 0], M[:, 0, 1], M[:, 1, 1]
			det = m00 * m11 - m01 * m01
			half = 0.5 * (m00 + m11)
			root = np.sqrt(np.maximum(half * half - det, 0.0))
			small = half - root # the eigenvalues: cond = big / small
			bad = ~(small > 0) | ((half + root) > 1e6 * small)
			det = np.where(bad, 1.0, det)
			a = np.stack([(m11 * b[:, 0] - m01 * b[:, 1]) / det, (m00 * b[:, 1] - m01 * b[:, 0]) / det], axis=1)
		else:
			bad = np.linalg.cond(M) > 1e6
			M[bad] = np.eye(k)
			a = np.linalg.solve(M, b[:, :, None])[:, :, 0]
		together = (a * b).sum(axis=1)
		together[bad] = -np.inf
		return a, together

	def JointFit(self, R, a0, members):
		"""Units' templates at their samples ([(sample, unit), ...]) fitted together (least squares, each site divided
		by its noise), from their scores R (whose first sample is a0) and their cross-correlations: (their sizes, what
		they explain together in noise units squared); None if they can't be told apart (their templates too alike
		there)."""
		L = el.BEFORE + el.AFTER
		k = len(members)
		M, b = np.zeros((k, k)), np.zeros(k)
		for i, (ti, ui) in enumerate(members):
			b[i] = R[ui, ti - a0]
			M[i, i] = self.norm2[ui]
			for j in range(i + 1, k):
				tj, uj = members[j]
				p = self.pos[ui, uj]
				if p >= 0 and abs(tj - ti) < L:
					M[i, j] = M[j, i] = self.cross[ui][p, tj - ti + L - 1]
		if np.linalg.cond(M) > 1e6:
			return None
		a = np.linalg.solve(M, b)
		return a, float(a @ b)

	def Judge(self, buf, bfirst, lo, hi):
		w = MATCH_W
		E = self.passes * REACH
		a0, b0 = lo - E - 2 * w, hi + E + 2 * w
		o = bfirst + a0 - self.s_first
		R = self.S[:, o:o + b0 - a0].copy() # the scores of the data less each spike found, as the passes go
		found, taken = [], {}
		for p in range(self.passes):
			c0, c1 = a0 + p * REACH, b0 - p * REACH
			if c1 - c0 <= 4 * w:
				break
			s = R[:, c0 - a0:c1 - a0]
			z, scale = s / self.root[:, None], s / self.norm2[:, None]
			new, cand = self.Candidates(z, scale, c0, c1, taken)
			if not new:
				break # the rest is unchanged: a next pass would find nothing new
			paired = set()
			accepted = {(tt, uu) for tt, uu, _ in new}
			for t, u, a in new:
				# Units near it peaking at once: their templates fitted together (alone, the first one's fit takes in
				# the others' spikes, and leaves too little of them to be found)
				group = self.Group(R, a0, t, u, cand, paired, accepted)
				if group is None and a > SCALE[1]:
					continue # too big for one spike of it, and no other unit's spike at once explains the rest
				fits = [(t, u, a)] if group is None else group
				for tt, uu, aa in fits:
					self.Subtract(R, a0, tt, uu, aa)
					taken[uu] = np.append(taken.get(uu, np.zeros(0, np.int64)), tt)
					found.append((tt, uu))
		found = [(t, u) for t, u in found if lo <= t < hi]
		if not found:
			return el.Spikes()
		t = np.array([f[0] for f in found])
		u = np.array([f[1] for f in found])
		order = np.lexsort((u, t))
		t, u = t[order], u[order]
		sites = self.sites[u]
		rows = t[:, None] + np.arange(-el.BEFORE, el.AFTER)[None, :]
		waves = buf[rows[:, :, None], np.maximum(sites, 0)[:, None, :]].astype(np.float32)
		return el.Spikes(bfirst + t, self.peaks[u], buf[t, self.peaks[u]], self.units[u], waves, sites.astype(np.int32))


class MatchingSorter(TemplateSorter):
	"""
	The sorter that finds spikes with the templates too (TemplateMatcher):
	its spikes have their units; the threshold's spikes that none of them
	explains (within DEDUP_W samples, on its sites) stay as they are,
	unsorted (-1). Both kinds are given in the order of their samples once
	both finders have judged them.
	"""

	def __init__(self, path, z=Z_THRESHOLD):
		super().__init__(path)
		self.matcher = TemplateMatcher(self, z)
		self.name = "Kilosort 4 templates, matched ({} units, z {:g})".format(len(self.units), z)
		self.pending = el.Spikes()
		self.recent = el.Spikes() # template spikes given out, the last few samples: for the next threshold spikes

	def Feed(self, first, filtered, spikes, detector_next):
		"""The spikes for the samples given: the template spikes, and the threshold's spikes (spikes) they don't
		explain, up to what both finders have judged."""
		found = self.matcher.Feed(first, filtered)
		self.pending = Cat([self.pending, found, spikes])
		upto = min(self.matcher.next, detector_next) - DEDUP_W
		out = self.Release(upto)
		return out

	def Release(self, upto):
		ready = self.pending.sample < upto
		out, self.pending = Take(self.pending, ready), Take(self.pending, ~ready)
		later = Take(self.pending, self.pending.unit >= 0) # template spikes not given out yet, just after upto
		tmpl = Cat([self.recent, Take(out, out.unit >= 0), later])
		drop = np.zeros(len(out), bool)
		for i in np.flatnonzero(out.unit < 0):
			near = np.abs(tmpl.sample - out.sample[i]) <= DEDUP_W
			if near.any() and any(out.channel[i] in tmpl.neighbors[j] for j in np.flatnonzero(near)):
				drop[i] = True
		out = Take(out, ~drop)
		given = Take(out, out.unit >= 0)
		self.recent = Take(Cat([self.recent, given]), Cat([self.recent, given]).sample >= upto - 2 * DEDUP_W)
		return out

	def Flush(self):
		"""What is left, at the end of a replay."""
		self.pending = Cat([self.pending, self.matcher.Flush()])
		return self.Release(np.iinfo(np.int64).max)


# ---------------------------------------------------------------- training and evaluation

def Span(seconds, rate, n):
	"""A span "a:b" in seconds (either end may be left out) as samples [start, end) within n."""
	a, _, b = (seconds or ":").partition(":")
	start = int(float(a) * rate) if a.strip() else 0
	end = int(float(b) * rate) if b.strip() else n
	return max(0, start), min(n, end)


def Train(data, stream, phy, out, span=None, labels=LABELS, say=print, accept_q=ACCEPT_Q):
	"""Templates for the kept units of phy's sort, from the samples in span ("a:b" seconds) of data replayed as live.
	Writes out (.npz) and returns its summary."""
	t0 = time.monotonic()
	sort = ReadPhy(phy, labels)
	start, end = Span(span, stream.rate, len(data))
	say("Kilosort's sort: {} units, {} kept ({}: {}); training on {:.1f} s to {:.1f} s".format(len(sort["labels"]),
		len(sort["kept"]), sort["label_file"] or "no labels", "/".join(labels), start / stream.rate, end / stream.rate))
	filtered, spikes, detector = Replay(data, stream, start, end)
	say("Replayed as live: {:,} spikes ({:.0f} s)".format(len(spikes), time.monotonic() - t0))
	inside = (sort["times"] >= start + el.BEFORE) & (sort["times"] < end - el.AFTER) & np.isin(sort["clusters"], sort["kept"])
	times, clusters = sort["times"][inside], sort["clusters"][inside]
	peaks = PeakSites(filtered, stream.geometry, times, clusters, sort["kept"], start)
	keep = np.isin(clusters, list(peaks))
	unit_of, _ = Match(spikes, stream.geometry, times[keep], clusters[keep], peaks)
	noise = np.median(np.abs(filtered[::8]), axis=0) / 0.6745
	sites_all = stream.geometry.Neighbors(np.inf)
	rows = []
	for u in sort["kept"]:
		if u not in peaks:
			continue
		sites = sites_all[peaks[u]]
		# Its spikes at Kilosort's times (also those that never cross the threshold), moved to where the live filter
		# puts their trough: the template is the median of their live-filtered waveforms on its sites
		ks = times[clusters == u] - start
		if len(ks) < MIN_SPIKES:
			continue
		shift = TroughShift(filtered, ks, peaks[u])
		ks = ks + shift
		ks = ks[(ks >= el.BEFORE) & (ks < len(filtered) - el.AFTER)]
		pick = ks[np.linspace(0, len(ks) - 1, min(MAX_SPIKES, len(ks))).astype(int)]
		waves = np.stack([filtered[i - el.BEFORE:i + el.AFTER][:, sites] for i in pick])
		template = np.median(waves, axis=0).astype(np.float32)
		# Its acceptance: its own spikes' distances, as the live sorter will measure them (the threshold's spikes of
		# it; for a unit with too few, its spikes at Kilosort's times)
		mine = np.flatnonzero(unit_of == u)
		if len(mine) >= MIN_SPIKES:
			d = [Distance(spikes.waveforms[i], spikes.neighbors[i], template, sites, noise)[0] for i in mine]
		else:
			d = [Distance(w, sites, template, sites, noise)[0] for w in waves]
		d = np.array([v for v in d if v is not None])
		if len(d) < MIN_SPIKES // 2:
			continue
		rows.append((u, sort["labels"][u], peaks[u], sites, template, float(np.quantile(d, accept_q)), len(ks)))
	if not rows:
		raise el.EphysError("No unit had {} live spikes in the training span: nothing to sort with.".format(MIN_SPIKES))
	np.savez_compressed(out, units=np.array([r[0] for r in rows]), labels=np.array([r[1] for r in rows]),
		peaks=np.array([r[2] for r in rows]), sites=np.stack([r[3] for r in rows]),
		templates=np.stack([r[4] for r in rows]), accept=np.array([r[5] for r in rows], np.float32),
		trained_spikes=np.array([r[6] for r in rows]), noise=noise.astype(np.float32), rate=stream.rate,
		span_samples=np.array([start, end]), phy=os.path.abspath(phy), label_file=sort["label_file"] or "",
		kept_labels=",".join(labels))
	summary = {"units": len(rows), "kept_units": len(sort["kept"]), "trained_spikes": int(sum(r[6] for r in rows)),
		"live_spikes": len(spikes), "matched_live_spikes": int((unit_of >= 0).sum()), "seconds": round(
		time.monotonic() - t0, 1), "out": out}
	say("Templates for {} of {} kept units, from {:,} of their Kilosort spikes ({:,} of the {:,} live spikes "
		"theirs): {}".format(summary["units"], summary["kept_units"], summary["trained_spikes"],
		summary["matched_live_spikes"], summary["live_spikes"], out))
	return summary


def ReplayMatched(data, stream, start, end, sorter, piece_s=0.1):
	"""The samples [start, end) of data through the live filter, the threshold's spike finder and the matching
	sorter, as the live engine runs them (pieces of piece_s): the spikes it gives, and the seconds it took to match
	and sort."""
	f, d = el.Filter(stream), el.Detector(stream)
	n = int(stream.rate * piece_s)
	out, took = [], 0.0
	for a in range(start, end, n):
		b = min(end, a + n)
		x = f.Apply(np.asarray(data[a:b]))
		thr = d.Feed(a, x)
		t0 = time.perf_counter()
		out.append(sorter.Feed(a, x, thr, d.next))
		took += time.perf_counter() - t0
	out.append(sorter.Flush())
	return Cat(out), took


def Evaluate(data, stream, phy, templates, span=None, warm_s=1.0, say=print, report=None, match=0):
	"""The templates' sort of the samples in span against Kilosort's there: per unit (the templates' units with a
	label in report; default all) the share of its Kilosort spikes found live and given to it (sensitivity), and the
	share of the live spikes given to it that are its (precision); the live spikes the templates left unsorted; the
	time a spike took to sort. Every unit with a template competes for the spikes (mua units' templates take their
	own spikes from the good units'), and a live spike is a unit's when it matches one of that unit's Kilosort
	spikes."""
	sorter = MatchingSorter(templates, match) if match else TemplateSorter(templates)
	sort = ReadPhy(phy, tuple(set(str(l) for l in sorter.labels)))
	start, end = Span(span, stream.rate, len(data))
	warm = max(0, start - int(warm_s * stream.rate)) # the noise measured before the span, as live would have
	if match:
		spikes, took = ReplayMatched(data, stream, warm, end, sorter)
		spikes = Take(spikes, spikes.sample >= start + el.BEFORE)
		per_second = took / ((end - warm) / stream.rate)
	else:
		filtered, spikes, _ = Replay(data, stream, warm, end, keep=False)
		in_span = spikes.sample >= start + el.BEFORE
		spikes = el.Spikes(spikes.sample[in_span], spikes.channel[in_span], spikes.amplitude[in_span],
			waveforms=spikes.waveforms[in_span], neighbors=spikes.neighbors[in_span])
		t0 = time.perf_counter()
		sorter.Assign(spikes)
		per_second = (time.perf_counter() - t0) / ((end - start) / stream.rate)
	per_spike_us = 1e6 * per_second * ((end - start) / stream.rate) / max(1, len(spikes))
	units = [int(u) for u in sorter.units]
	peaks = {int(u): int(p) for u, p in zip(sorter.units, sorter.peaks)}
	inside = (sort["times"] >= start + el.BEFORE) & (sort["times"] < end - el.AFTER) & np.isin(sort["clusters"], units)
	times, clusters = sort["times"][inside], sort["clusters"][inside]
	truth, taken = Match(spikes, stream.geometry, times, clusters, peaks)
	rows = []
	label_of = {int(u): str(l) for u, l in zip(sorter.units, sorter.labels)}
	shown = [u for u in units if report is None or label_of[u] in report]
	reported = np.isin(spikes.unit, shown)
	for u in shown:
		ks = clusters == u
		got = taken[ks]
		found = got >= 0
		right = found.copy()
		right[found] = spikes.unit[got[found]] == u
		given = spikes.unit == u
		rows.append({"unit": u, "label": sort["labels"].get(u), "peak_site": peaks[u], "kilosort_spikes": int(ks.sum()),
			"found_live": int(found.sum()), "found_and_given": int(right.sum()), "given": int(given.sum()),
			"given_and_its": int((given & (truth == u)).sum()),
			"sensitivity": float(right.sum() / ks.sum()) if ks.sum() else None,
			"precision": float((given & (truth == u)).sum() / given.sum()) if given.sum() else None})
	sens = [r["sensitivity"] for r in rows if r["sensitivity"] is not None]
	prec = [r["precision"] for r in rows if r["precision"] is not None]
	matched = np.isin(truth, shown)
	summary = {"units": len(rows), "reported_labels": list(report) if report else None,
		"span_s": [start / stream.rate, end / stream.rate], "live_spikes": len(spikes),
		"sorted": int((spikes.unit >= 0).sum()), "given_a_reported_unit": int(reported.sum()),
		"sensitivity_median": float(np.median(sens)) if sens else None,
		"sensitivity_p25_p75": [float(np.percentile(sens, 25)), float(np.percentile(sens, 75))] if sens else None,
		"precision_median": float(np.median(prec)) if prec else None,
		"precision_p25_p75": [float(np.percentile(prec, 25)), float(np.percentile(prec, 75))] if prec else None,
		"live_spikes_of_a_unit_given_it": float((spikes.unit[matched] == truth[matched]).mean()) if matched.any() else None,
		"live_spikes_of_a_unit_left_unsorted": float((spikes.unit[matched] < 0).mean()) if matched.any() else None,
		"live_spikes_of_no_unit_given_one": float(np.isin(spikes.unit[~matched], shown).mean()) if (~matched).any()
			else None,
		"sort_us_per_spike": round(per_spike_us, 1), "sort_s_per_s": round(per_second, 3), "match_z": match or None,
		"template_spikes": int((spikes.unit >= 0).sum()) if match else None, "per_unit": rows}
	say("Sorted {:,} of {:,} live spikes in {:.1f} to {:.1f} s ({:.0f} us a spike). Per unit, median: {:.0%} of its "
		"Kilosort spikes found live and given to it, {:.0%} of the spikes given to it are its.".format(summary["sorted"],
		len(spikes), *summary["span_s"], per_spike_us, summary["sensitivity_median"] or 0, summary["precision_median"]
		or 0))
	return summary
