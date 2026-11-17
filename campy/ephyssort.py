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
COMPONENTS = 8 # the templates' shared shapes in time (their SVD): the matched filter in this many convolutions a site
MATCH_W = 20 # samples (0.67 ms): one spike of a unit, and of units near it, within this many
DEDUP_W = 10 # samples: a threshold crossing this close to a template spike (on its sites) is that spike
PASSES = 3 # find, take the spikes' templates away, look again: up to this many times (two units' spikes at once)
PAIR_SEARCH = 6 # samples: another unit's spike at once is looked for this far either side of its own score's peak
GROUP = 3 # units whose spikes at once are fitted together (three units spiking within MATCH_W, on sites they share)
SCALE_ALONE_MAX = 4.0 # a peak's size fitted alone may be up to this before the group's fit (alone, the first unit's
# template takes in the others' spikes); after it, each size must be within SCALE
REACH = 2 * (el.BEFORE + el.AFTER) + 4 * MATCH_W # samples a pass's decisions reach (a subtraction, and its neighbors')


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

	The filtered samples, each site's divided by its noise, are matched with
	each unit's template (also divided by its sites' noise), through the
	templates' COMPONENTS shared shapes in time (the matched filter: one
	convolution a shape and a site, then each unit's weights on its sites).
	Its score, in noise units, says how much of the data the template
	explains at each sample, and its scale how big the spike is next to the
	template. A spike of unit u is at a sample where u's score is above
	z, its scale within SCALE, the highest of u's within MATCH_W samples, and
	no unit near it (its peak site among u's sites) has a higher score within
	MATCH_W samples.

	Units near a spike found that also peak within MATCH_W are fitted together
	with it (all their templates' sizes at once, by least squares, at the lags
	that fit best), up to GROUP units: alone, the first unit's fit takes in the
	others' spikes on the sites they share, and leaves too little of them to
	be found. Then, as Kilosort does, each spike found has its template (at
	its size) taken away from the data, and the rest is looked at again, up
	to PASSES times. A unit has no second spike within MATCH_W samples of one
	it already has.

	Like the threshold's spike finder, a sample is judged once all the samples
	it needs have come (each pass reaching REACH further), so the same samples
	give the same spikes however they come in pieces.
	"""

	def __init__(self, sorter, z=Z_THRESHOLD, components=COMPONENTS):
		self.z = z
		self.units, self.peaks, self.sites = sorter.units, sorter.peaks, sorter.sites
		self.noise = sorter.noise.astype(np.float32)
		Tn = sorter.templates / self.noise[sorter.sites][:, None, :] # [units, 61, sites]
		shapes = Tn.transpose(0, 2, 1).reshape(-1, Tn.shape[1])
		_, _, vt = np.linalg.svd(shapes, full_matrices=False)
		self.W = vt[:components].T.astype(np.float32) # [61, K]
		self.A = np.einsum("tk,utj->ukj", self.W, Tn).astype(np.float32) # each unit's weights [units, K, sites]
		recon = np.einsum("tk,ukj->utj", self.W, self.A)
		self.norm2 = (recon ** 2).sum(axis=(1, 2)) # each template's energy, as the shapes give it
		self.near = [np.flatnonzero(np.isin(self.peaks, s)) for s in self.sites] # units near each: their peak among its sites
		self.templates = sorter.templates # uV: what a spike found takes away
		self.passes = PASSES
		self.Reset()

	def Reset(self):
		self.tail, self.tail_first, self.next = None, None, None

	def Feed(self, first, x):
		"""Template spikes judged with the samples given (filtered uV); as Detector.Feed."""
		if self.tail is not None and first != self.tail_first + len(self.tail):
			self.Reset()
		if self.tail is None:
			buf, bfirst, self.next = x, first, first
		else:
			buf, bfirst = np.concatenate([self.tail, x]), self.tail_first
		w = MATCH_W
		reach = self.passes * REACH + 2 * w + 1
		margin_lo, margin_hi = el.BEFORE + reach, el.AFTER + reach
		lo = max(self.next - bfirst, margin_lo)
		hi = len(buf) - margin_hi
		found = self.Judge(buf, bfirst, lo, hi) if hi > lo else el.Spikes()
		self.next = bfirst + max(hi, lo)
		keep = max(0, self.next - bfirst - margin_lo)
		self.tail, self.tail_first = buf[keep:], bfirst + keep
		return found

	def Scores(self, x, a0, b0):
		"""Each unit's score and scale at samples [a0, b0) of x (whose samples a0 - BEFORE to b0 + AFTER - 1 must be
		there): [units, b0 - a0] each."""
		from scipy.signal import fftconvolve
		seg = x[a0 - el.BEFORE:b0 + el.AFTER - 1] / self.noise
		F = np.stack([fftconvolve(seg, self.W[::-1, k][:, None], mode="valid", axes=0) for k in range(self.W.shape[1])],
			axis=2).astype(np.float32) # [b0 - a0, channels, K]
		s = np.stack([np.einsum("tjk,kj->t", F[:, self.sites[u], :], self.A[u]) for u in range(len(self.units))])
		return s / np.sqrt(self.norm2)[:, None], s / self.norm2[:, None]

	def Candidates(self, z, scale, c0, c1, taken):
		"""Pass's spikes among samples [c0, c1) (the scores' first is c0): (sample, unit) of each, judged where all
		it needs is known ([c0 + 2 MATCH_W, c1 - 2 MATCH_W)); none for a unit within MATCH_W of one it has (taken)."""
		from scipy.ndimage import maximum_filter1d
		w = MATCH_W
		peak = (z >= maximum_filter1d(z, 2 * w + 1, axis=1, mode="nearest")) & (z > self.z) & (scale >= SCALE[0]) & \
			(scale <= SCALE_ALONE_MAX)
		cand = []
		for u in range(len(self.units)):
			ts = np.flatnonzero(peak[u])
			mine = taken.get(u)
			if mine is not None and len(ts): # not a second spike of a unit within MATCH_W of one it has: what is
				# left of another unit's spike under its template doesn't count, nor hide that unit's
				ts = ts[np.abs((c0 + ts)[:, None] - mine[None, :]).min(axis=1) > w]
			cand.append(ts)
		out = []
		for u, ts in enumerate(cand):
			for t in ts:
				if not 2 * w <= t < c1 - c0 - 2 * w:
					continue
				best = True
				for v in self.near[u]:
					if v == u:
						continue
					c = cand[v]
					close = c[np.abs(c - t) <= w]
					if len(close) and (z[v, close].max() > z[u, t] or (z[v, close].max() == z[u, t] and v < u)):
						best = False
						break
				if best:
					out.append((c0 + t, u, float(scale[u, t])))
		return out, cand

	def Group(self, res, A, t, u, c0, cand, paired, accepted):
		"""Other units' spikes at once with unit u's at t: units near the group (their peak among a member's sites)
		peaking within MATCH_W of t (this pass's candidates, not accepted on their own) are added one at a time, the
		one that adds most first, at the lag near its peak that fits best, with all the group's templates' sizes
		fitted together on the sites they cover; one is added when every size stays within SCALE and it adds at least
		the score's threshold squared to what the group explained, up to GROUP units. [(sample, unit, size), ...] for
		a group of two or more; None for u alone."""
		w = MATCH_W
		group = [(t, u)]
		fit = self.JointFit(res, A, group)
		if fit is None:
			return None
		explained = fit[1]
		while len(group) < GROUP:
			best = None
			members = {m for _, m in group}
			tried = set()
			for _, m in group:
				for v in self.near[m]:
					if v in members or v in tried:
						continue
					tried.add(v)
					for peak in cand[v]:
						peak = c0 + int(peak)
						if abs(peak - t) > w or (peak, v) in accepted:
							continue
						# Its own score's peak is pulled by the others' spikes: the lag that fits best, near it
						for tv in range(max(t - w, peak - PAIR_SEARCH), min(t + w, peak + PAIR_SEARCH) + 1):
							if (tv, v) in paired:
								continue
							trial = self.JointFit(res, A, group + [(tv, v)])
							if trial is None:
								continue
							sizes, together = trial
							gain = together - explained
							if gain > self.z ** 2 and np.all((sizes >= SCALE[0]) & (sizes <= SCALE[1])) and \
									(best is None or gain > best[0]):
								best = (gain, (tv, v), sizes, together)
			if best is None:
				break
			group.append(best[1])
			explained = best[3]
			fit = (best[2], best[3])
		if len(group) == 1:
			return None
		for tv, v in group[1:]:
			paired.add((tv, v))
		return [(tt, uu, float(a)) for (tt, uu), a in zip(group, fit[0])]

	def JointFit(self, res, A, members):
		"""Units' templates at their samples ([(sample, unit), ...]) fitted together (least squares, each site divided
		by its noise): (their sizes, what they explain together in noise units squared); None if they can't be told
		apart (their templates too alike there)."""
		lo_t = min(t for t, _ in members) - el.BEFORE
		hi_t = max(t for t, _ in members) + el.AFTER
		sites = np.unique(np.concatenate([self.sites[k] for _, k in members]))
		D = res[lo_t - A:hi_t - A][:, sites] / self.noise[sites]
		G = np.zeros((len(members), D.size), np.float64)
		for i, (t, k) in enumerate(members):
			g = np.zeros_like(D)
			cols = np.searchsorted(sites, self.sites[k])
			g[t - el.BEFORE - lo_t:t + el.AFTER - lo_t][:, cols] = self.templates[k] / self.noise[self.sites[k]]
			G[i] = g.ravel()
		M = G @ G.T
		b = G @ D.ravel()
		if np.linalg.cond(M) > 1e6:
			return None
		a = np.linalg.solve(M, b)
		return a, float(a @ b)

	def Judge(self, buf, bfirst, lo, hi):
		w = MATCH_W
		E = self.passes * REACH
		a0, b0 = lo - E - 2 * w, hi + E + 2 * w
		A = a0 - el.BEFORE
		res = np.array(buf[A:b0 + el.AFTER], np.float32) # the data less each spike found, as the passes go
		found, taken = [], {}
		for p in range(self.passes):
			c0, c1 = a0 + p * REACH, b0 - p * REACH
			if c1 - c0 <= 4 * w:
				break
			z, scale = self.Scores(res, c0 - A, c1 - A)
			new, cand = self.Candidates(z, scale, c0, c1, taken)
			if not new:
				break # the rest is unchanged: a next pass would find nothing new
			paired = set()
			for t, u, a in new:
				# Units near it peaking at once: their templates fitted together (alone, the first one's fit takes in
				# the others' spikes, and leaves too little of them to be found)
				group = self.Group(res, A, t, u, c0, cand, paired, {(tt, uu) for tt, uu, _ in new})
				if group is None and a > SCALE[1]:
					continue # too big for one spike of it, and no other unit's spike at once explains the rest
				fits = [(t, u, a)] if group is None else group
				for tt, uu, aa in fits:
					res[tt - A - el.BEFORE:tt - A + el.AFTER, self.sites[uu]] -= aa * self.templates[uu]
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
		waves = buf[rows[:, :, None], sites[:, None, :]].astype(np.float32)
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
