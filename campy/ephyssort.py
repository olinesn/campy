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

import os, threading, time

import numpy as np

from campy import ephyslive as el

LABELS = ("good", "mua") # the units kept by default (phy's or Kilosort's label): mua units' templates take their own
# spikes, which good units' would otherwise take (on Kilosort's sample: 8% of the spikes of no good unit given one,
# against 42% with good units' templates alone)
COMPETE = ("noise",) # units with these labels (phy's: a curated sort's noise clusters) get templates only to compete
# for spikes: what they match isn't given out (a threshold spike stays unsorted), so it isn't given to a unit either
TOLERANCE = 10 # samples: a live spike this close to a Kilosort spike is that spike
RADIUS_UM = 60.0 # and on a site this close to its unit's peak site
MIN_SPIKES = 20 # a unit with fewer live spikes in the training span has no template
MAX_SPIKES = 500 # a template is the median of at most this many spikes
ACCEPT_Q = 0.99 # a unit's acceptance: this quantile of its own training spikes' distances
SCALE = (0.5, 2.0) # the template scaled to a spike within these
MIN_SITES = 6 # sites a spike and a unit must share to be compared
TEMPLATE_SITES = 32 # sites a template covers, nearest first: a spike's whole footprint, so the matcher takes all of a
# spike away (12 sites leave its edges, which a small unit's template there then matches). The threshold's spikes are
# compared with a template's first el.NEIGHBORS sites, those a spike keeps


# ---------------------------------------------------------------- Kilosort's output (phy's folder)

def ReadPhy(folder, labels=LABELS, compete=()):
	"""Kilosort's spikes (times in the recording's samples, from 0) and units, with each unit's label (phy's
	cluster_group.tsv if there is one, else cluster_KSLabel.tsv): {"times", "clusters", "labels": {unit: label},
	"kept": [units with a label in labels], "compete": [units with a label in compete], "label_file"}."""
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
	rivals = [int(u) for u in units if found.get(int(u), "unsorted") in compete and int(u) not in kept]
	return {"times": times, "clusters": clusters, "labels": {int(u): found.get(int(u), "unsorted") for u in units},
		"kept": kept, "compete": rivals, "label_file": label_file}


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
		self.sites = f["sites"] # [units, TEMPLATE_SITES]: its peak's nearest sites, nearest first
		self.templates = f["templates"] # [units, 61, TEMPLATE_SITES]
		self.near_sites = self.sites[:, :el.NEIGHBORS] # those a threshold's spike is compared on
		self.near_templates = self.templates[:, :, :el.NEIGHBORS]
		self.accept = f["accept"]
		self.compete = f["compete"].astype(bool) if "compete" in f else np.zeros(len(self.units), bool) # templates
		# that only compete for spikes (COMPETE): what they match stays unsorted
		self.match_z = f["match_z"].astype(np.float32) if "match_z" in f else None # each unit's template-spike
		# threshold, learned in training (Learn)
		self.noise = f["noise"] # uV, each channel's
		self.whiten = f["whiten"] if "whiten" in f else None # the matcher's whitening (Whitening), and its templates
		# and each channel's noise in the samples whitened
		self.templates_w = f["templates_w"] if "templates_w" in f else None
		self.noise_w = f["noise_w"] if "noise_w" in f else None
		self.trained = int(f["trained_spikes"].sum()) if "trained_spikes" in f else None
		self.n_train = f["trained_spikes"] if "trained_spikes" in f else np.zeros(len(self.units), int)
		n = int(self.sites.max()) + 1 if self.sites.size else 0
		self.by_channel = [[] for _ in range(max(n, len(self.noise)))]
		for k, s in enumerate(self.near_sites):
			for ch in s[s >= 0]:
				self.by_channel[ch].append(k)
		self.given = int((~self.compete).sum())
		self.name = "Kilosort 4 templates ({} units)".format(self.given)

	def Assign(self, spikes):
		unit = -np.ones(len(spikes), np.int32)
		for i in range(len(spikes)):
			ch = int(spikes.channel[i])
			if ch >= len(self.by_channel):
				continue
			best, best_k = None, None
			for k in self.by_channel[ch]:
				d, a = Distance(spikes.waveforms[i], spikes.neighbors[i], self.near_templates[k], self.near_sites[k],
					self.noise)
				if d is not None and (best is None or d < best):
					best, best_k = d, k
			if best is not None and best <= self.accept[best_k] and not self.compete[best_k]:
				unit[i] = self.units[best_k]
		spikes.unit = unit
		return spikes

	def Units(self):
		return [{"unit": int(u), "channel": int(p), "label": str(l), "trained_spikes": int(n)} for u, p, l, n, c in
			zip(self.units, self.peaks, self.labels, self.n_train, self.compete) if not c]


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
SCORE_GROUP = 12 # units whose scores are worked out together: their shapes' weights on the sites they cover, times
# those sites' transforms, in one matrix product
BLOCK = 2048 # samples a transform: the scores are worked out this many (less 60) at a time, in whole blocks counted
# from the first sample scored (so the same samples give the same scores however they come, and no block is half empty)
WHITEN_SITES = 32 # the matcher scores samples whitened as Kilosort 4 whitens them: each channel from the covariance
# of its 32 nearest sites (ZCA). Activity correlated across sites (neurons further away) is then not taken for a
# template's spike
LEARN_Z = 6.0 # training: the template spikes each unit's threshold is learned from are those found at this z
KEY = 1 << 40 # a spike's unit * KEY + its sample: one sorted key for (unit, sample)
JUDGE = 3000 # samples (0.1 s): the template spikes are judged once at least this many more can be (each judgment
# works its margins out again: fewer, longer judgments cost less; the card plays 0.3 s behind)


def Whitening(filtered, geometry, count=WHITEN_SITES, step=8):
	"""Kilosort 4's local whitening (ZCA) for the filtered samples ([samples, channels], every step-th used):
	[channels, channels], each channel's row from the covariance of its count nearest sites (an unused site's: 0).
	x @ W.T is the samples whitened."""
	x = np.asarray(filtered[::step], np.float64)
	C = x.T @ x / max(1, len(x))
	nb = geometry.Neighbors(1e9, count)
	W = np.zeros_like(C)
	for c in range(len(C)):
		if not geometry.used[c]:
			continue
		ix = nb[c][nb[c] >= 0]
		E, D = np.linalg.eigh(C[np.ix_(ix, ix)])
		E = np.maximum(E, 0) + 1e-6 * max(float(E.mean()), 1e-12)
		W[c, ix] = (D / np.sqrt(E)) @ D[0] # its row of D diag(1 / sqrt(E)) D^T (ix's first is c)
	return W.astype(np.float32)


# ---------------------------------------------------------------- numba (optional)

_FAST = {"state": "idle", "module": None, "seconds": None, "error": None, "thread": None}
_FAST_LOCK = threading.Lock()


def StartFast():
	"""Starts loading numba's compiled loop (campy/ephyssort_numba.py) in a thread of its own, once, and returns at
	once: compiling it takes seconds the first time (numba keeps it after). CAMPY_NUMBA=0 in the environment: not at
	all."""
	with _FAST_LOCK:
		if _FAST["state"] != "idle":
			return
		if os.environ.get("CAMPY_NUMBA", "").strip() == "0":
			_FAST["state"] = "off"
			return
		_FAST["state"] = "loading"
		_FAST["thread"] = threading.Thread(target=_LoadFast, daemon=True, name="campy ephyssort numba")
		_FAST["thread"].start()


def _LoadFast():
	t0 = time.perf_counter()
	try:
		import importlib
		module = importlib.import_module("campy.ephyssort_numba")
		module.WarmUp()
	except ImportError as e:
		_FAST.update(state="missing", error=str(e))
		return
	except Exception as e: # numba's compiler: Python and numpy do it
		_FAST.update(state="failed", error="{}: {}".format(type(e).__name__, e))
		return
	_FAST.update(module=module, seconds=time.perf_counter() - t0, state="ready")


def Fast(wait=False):
	"""numba's compiled loop (the campy.ephyssort_numba module) once loaded, else None. wait: start loading it if it
	hasn't started, and wait for it."""
	if wait:
		StartFast()
		thread = _FAST["thread"]
		if thread is not None:
			thread.join()
	return _FAST["module"] if _FAST["state"] == "ready" else None


def FastStatus():
	"""{"state": idle, loading, ready, missing (no numba), failed or off (CAMPY_NUMBA=0), "numba": its version once
	ready, "seconds": how long loading it took, "error"}."""
	m = _FAST["module"]
	return {"state": _FAST["state"], "numba": m.VERSION if m is not None else None, "seconds": _FAST["seconds"],
		"error": _FAST["error"]}


def Solve3(m00, m01, m02, m11, m12, m22, b0, b1, b2):
	"""Three templates' sizes fitted together, for arrays of trials: M a = b (M symmetric, from the scores' cross-
	correlations) by M = L D L^T, written out; what they explain (a . b); bad where they can't be told apart (a pivot
	0 or under a millionth of the largest). The same operations, in the same order, as numba's loop."""
	d0 = m00
	l10 = m01 / d0
	l20 = m02 / d0
	d1 = m11 - l10 * m01
	e = m12 - l20 * m01
	l21 = e / d1
	d2 = m22 - l20 * m02 - l21 * e
	small = np.minimum(np.minimum(d0, d1), d2)
	bad = ~(small > 0) | (np.maximum(np.maximum(d0, d1), d2) > 1e6 * small)
	d0, d1, d2 = np.where(bad, 1.0, d0), np.where(bad, 1.0, d1), np.where(bad, 1.0, d2)
	l10, l20, l21 = np.where(bad, 0.0, l10), np.where(bad, 0.0, l20), np.where(bad, 0.0, l21)
	y1 = b1 - l10 * b0
	y2 = b2 - l20 * b0 - l21 * y1
	x2 = y2 / d2
	x1 = y1 / d1 - l21 * x2
	x0 = b0 / d0 - l10 * x1 - l20 * x2
	together = x0 * b0 + x1 * b1 + x2 * b2
	return np.stack([x0, x1, x2], axis=1), together, bad


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

	def __init__(self, sorter, z=None, components=COMPONENTS, threads=1, fast=True):
		"""z: every unit's threshold; None: each unit's learned in training (sorter.match_z), else Z_THRESHOLD.
		threads: the scores are worked out in this many threads, each for its share of the units (the same scores).
		fast: judge with numba's compiled loop once it is loaded (campy/ephyssort_numba.py; the same spikes)."""
		import scipy.fft
		self.fft = scipy.fft
		self.units, self.peaks, self.sites = sorter.units, sorter.peaks, sorter.sites
		if getattr(sorter, "whiten", None) is not None: # the samples scored whitened, with templates made so
			self.white = np.ascontiguousarray(sorter.whiten.T, np.float32)
			templates, noise = sorter.templates_w, sorter.noise_w
		else:
			self.white, templates, noise = None, sorter.templates, sorter.noise
		if z is None and sorter.match_z is not None:
			self.zu = sorter.match_z.astype(np.float64)
		else:
			self.zu = np.full(len(self.units), Z_THRESHOLD if z is None else float(z))
		self.z = float(self.zu.min()) if len(self.zu) else Z_THRESHOLD
		self.loose = None # set with root, below
		self.give = ~sorter.compete # units whose spikes are given out (the others' only compete for them)
		self.record = None # a list: each template spike found, (sample, unit's index, z, size, pass, in a group, the
		# score of the data as it came (before any spike was taken away), the cosine of the data with the template)
		self.noise = np.where(noise > 0, noise, 1).astype(np.float32) # each channel's, in the samples scored
		U, L = len(self.units), el.BEFORE + el.AFTER
		ok = self.sites >= 0
		self.cols = np.where(ok, self.sites, 0) # a site missing (-1: a probe with fewer channels) as channel 0, weight 0
		Tn = templates / self.noise[self.cols][:, None, :] * ok[:, None, :] # [units, 61, sites], noise units
		shapes = Tn.transpose(0, 2, 1).reshape(-1, L)
		_, _, vt = np.linalg.svd(shapes, full_matrices=False)
		W = vt[:components].T # [61, K]
		self.Tn = np.einsum("tk,sk,usj->utj", W, W, Tn).astype(np.float32) # each template as its shapes give it
		self.W = W # [61, K]: the shapes in time
		self.weights = np.einsum("tk,utj->ukj", W, Tn) # [units, K, sites]: each template's shapes' weights on its sites
		self.norm2 = np.maximum((self.Tn.astype(np.float64) ** 2).sum(axis=(1, 2)), 1e-12) # its energy
		self.root = np.sqrt(self.norm2)
		self.loose = self.zu * self.root * (1 - 1e-9) # a score above this may be above its unit's threshold
		self.near = [np.flatnonzero(np.isin(self.peaks, s)) for s in sorter.near_sites] # units near each: their peak
		# among its NEIGHBORS nearest sites
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
		self.runs = [] # each unit's: the runs of units next to each other in over[u], (first, last + 1, their rows in
		# cross[u]): taking a spike away is a few slices (units saved in their order on the probe: one or two runs)
		for V in self.over:
			cut = np.flatnonzero(np.diff(V) > 1) + 1
			a, b = np.r_[0, cut], np.r_[cut, len(V)]
			self.runs.append([(int(V[i]), int(V[j - 1]) + 1, int(i), int(j)) for i, j in zip(a, b)])
		self.groups = self.ScoreGroups(member)
		self.shapes_f = np.conj(np.fft.rfft(W.T, n=BLOCK, axis=1)).astype(np.complex64) # [K, BLOCK // 2 + 1]
		self.threads = max(1, int(threads))
		self.pool = None
		if self.threads > 1:
			from concurrent.futures import ThreadPoolExecutor
			self.pool = ThreadPoolExecutor(self.threads, thread_name_prefix="campy ephys scores")
		# numba's loop: the cross-correlations one after another (unit u's from row cross_at[u]), and the runs
		self.fast = fast
		if fast:
			StartFast()
		self.cross_flat = np.ascontiguousarray(np.concatenate(self.cross) if self.cross else np.zeros((0, 2 * L - 1)),
			np.float32)
		self.cross_at = np.r_[0, np.cumsum([len(c) for c in self.cross])][:-1].astype(np.int64)
		self.runs_flat = np.array([r for runs in self.runs for r in runs], np.int64).reshape(-1, 4)
		self.runs_at = np.r_[0, np.cumsum([len(r) for r in self.runs])].astype(np.int64)
		self.passes = PASSES
		self.judge = JUDGE
		self.Reset()

	def Reset(self):
		self.tail, self.tail_first, self.next = None, None, None
		self.tail_s = None # the tail as scored (whitened)
		self.s_first = None # the scores so far: S (Sbuf's columns [S_lo, S_hi)), from sample s_first
		if not hasattr(self, "Sbuf"):
			self.Sbuf = None
		self.S_lo = self.S_hi = 0

	@property
	def S(self):
		return None if self.s_first is None else self.Sbuf[:, self.S_lo:self.S_hi]

	def Room(self, n):
		"""Where the next n samples' scores go: n columns after the scores so far (the buffer moves them to its start,
		or grows, when full; so the scores aren't all copied at each piece)."""
		if self.Sbuf is None or self.S_hi + n > self.Sbuf.shape[1]:
			live = self.S_hi - self.S_lo
			if self.Sbuf is None or live + n > self.Sbuf.shape[1] // 2:
				grown = np.empty((len(self.units), max(2 * (live + n), 4 * BLOCK)), np.float32)
				if live:
					grown[:, :live] = self.Sbuf[:, self.S_lo:self.S_hi]
				self.Sbuf = grown
			elif live:
				self.Sbuf[:, :live] = self.Sbuf[:, self.S_lo:self.S_hi].copy()
			self.S_lo, self.S_hi = 0, live
		self.S_hi += n
		return self.Sbuf[:, self.S_hi - n:self.S_hi]

	def Feed(self, first, x, last=False):
		"""Template spikes judged with the samples given (filtered uV); as Detector.Feed. last: judge all that can be,
		however few (the end of a replay)."""
		if self.tail is not None and first != self.tail_first + len(self.tail):
			self.Reset()
		xs = x if self.white is None else x @ self.white
		if self.tail is None:
			buf, bufs, bfirst, self.next = x, xs, first, first
		else:
			buf, bfirst = np.concatenate([self.tail, x]), self.tail_first
			bufs = buf if self.white is None else np.concatenate([self.tail_s, xs])
		# The scores of the samples that now have all they need, once each, in whole blocks (the rest, at the end)
		start = bfirst + el.BEFORE if self.s_first is None else self.s_first + (self.S_hi - self.S_lo)
		end = bfirst + len(buf) - el.AFTER + 1
		step = BLOCK - (el.BEFORE + el.AFTER - 1)
		if not last:
			end = start + max(0, end - start) // step * step
		if end > start:
			if self.s_first is None:
				self.s_first, self.S_lo, self.S_hi = start, 0, 0
			self.Scores(bufs, start - bfirst, end - bfirst, out=self.Room(end - start))
		w = MATCH_W
		reach = self.passes * REACH + 2 * w + 1
		margin_lo, margin_hi = el.BEFORE + reach, el.AFTER + reach
		lo = max(self.next - bfirst, margin_lo)
		hi = len(buf) - margin_hi
		if self.s_first is not None: # what the scores reach
			hi = min(hi, self.s_first + (self.S_hi - self.S_lo) - bfirst - self.passes * REACH - 2 * w)
		if hi - lo >= (1 if last else self.judge):
			self.scored = bufs
			found = self.Judge(buf, bfirst, lo, hi)
			self.next = bfirst + hi
		else:
			found = el.Spikes()
		keep = max(0, self.next - bfirst - margin_lo)
		self.tail, self.tail_s, self.tail_first = buf[keep:], bufs[keep:], bfirst + keep
		if self.s_first is not None and self.tail_first + el.BEFORE > self.s_first:
			drop = self.tail_first + el.BEFORE - self.s_first
			self.S_lo, self.s_first = min(self.S_lo + drop, self.S_hi), self.s_first + drop
		return found

	def Flush(self):
		"""The template spikes of all that can be judged, however few samples (the end of a replay)."""
		if self.tail is None:
			return el.Spikes()
		return self.Feed(self.tail_first + len(self.tail), self.tail[:0], last=True)

	def ScoreGroups(self, member, size=SCORE_GROUP):
		"""Units in groups of about size whose sites overlap (each next unit the one with the most sites in the group's
		so far; a group starts from the remaining unit with the lowest peak channel): [(units, the channels their sites
		cover, their shapes' weights on those channels [units * K, channels] float32)]."""
		U, K = len(self.units), self.weights.shape[1]
		left = np.ones(U, bool)
		start = np.argsort(self.peaks, kind="stable")
		groups = []
		for first in start:
			if not left[first]:
				continue
			mine = [int(first)]
			left[first] = False
			cover = member[first].copy()
			while len(mine) < size and left.any():
				inside = np.where(left, (member & cover).sum(axis=1) - 0.001 * (member & ~cover).sum(axis=1), -np.inf)
				best = int(np.argmax(inside))
				if inside[best] <= 0:
					break
				mine.append(best)
				left[best] = False
				cover |= member[best]
			g = np.array(mine)
			chans = np.flatnonzero(cover)
			A = np.zeros((len(g), K, len(chans)), np.float64)
			for r, u in enumerate(g):
				ok = self.sites[u] >= 0
				A[r][:, np.searchsorted(chans, self.cols[u][ok])] += self.weights[u][:, ok]
			groups.append((g, chans, A.reshape(len(g) * K, len(chans)).astype(np.float32)))
		return groups

	def Scores(self, x, a0, b0, out=None):
		"""Each unit's score, not yet divided by its template's energy, at samples [a0, b0) of x (filtered uV, whitened
		if the templates are), whose samples a0 - BEFORE to b0 + AFTER - 1 must be there: [units, b0 - a0]. In blocks of
		BLOCK samples (BLOCK - 60 scores each) from a0: each channel's transform once, then per group of units, their
		shapes' weights times their channels' transforms (one matrix product), each shape's filter, and back."""
		L = el.BEFORE + el.AFTER
		if out is None:
			out = np.empty((len(self.units), b0 - a0), np.float32)
		n = BLOCK
		s = a0
		while s < b0:
			e = min(b0, s + n - (L - 1))
			X = self.fft.rfft((x[s - el.BEFORE:e + el.AFTER - 1] / self.noise).T, n=n, axis=1, workers=self.threads)
			X = np.ascontiguousarray(X, np.complex64).view(np.float32) # [channels, 2 F]: real and imaginary parts
			if self.pool is None:
				for group in self.groups:
					self.Matched(X, group, out[:, s - a0:e - a0], n)
			else:
				list(self.pool.map(lambda group: self.Matched(X, group, out[:, s - a0:e - a0], n), self.groups))
			s = e
		return out

	def Matched(self, X, group, out, n):
		"""A group's scores into out: its shapes' weights times its channels' transforms, each shape's filter, summed,
		and transformed back."""
		units, chans, A = group
		K = len(self.shapes_f)
		V = (A @ X[chans]).view(np.complex64).reshape(len(units), K, -1) # [units, K, F]
		Y = V[:, 0] * self.shapes_f[0]
		for k in range(1, K):
			Y += V[:, k] * self.shapes_f[k]
		out[units] = self.fft.irfft(Y, n=n, axis=1)[:, :out.shape[1]]

	def Subtract(self, R, a0, t, u, a):
		"""Unit u's template at t, of size a, taken away: the scores R (whose first sample is a0) of the units that share
		its sites lowered by their cross-correlations."""
		L = el.BEFORE + el.AFTER
		i0 = t - (L - 1) - a0
		lo, hi = max(0, i0), min(R.shape[1], i0 + 2 * L - 1)
		if hi > lo:
			c = self.cross[u]
			a = np.float32(a) # in float32 throughout (numba's loop does the same, to the last bit)
			for r0, r1, k0, k1 in self.runs[u]:
				R[r0:r1, lo:hi] -= a * c[k0:k1, lo - i0:hi - i0]

	def Candidates(self, s, c0, c1, taken):
		"""Pass's spikes among samples [c0, c1) (the scores' first is c0), judged where all it needs is known ([c0 + 2
		MATCH_W, c1 - 2 MATCH_W)); none for a unit within MATCH_W of one it has (taken: each spike's unit * KEY + its
		sample, sorted). (samples, units, z, whether a unit near it peaks within MATCH_W (else Group finds no group)),
		and all units' peaks, by sample (cand: their samples and units), for Group."""
		w = MATCH_W
		# Above its unit's threshold (few): z = s / root > threshold, looked for first as s > threshold * root, a little
		# lower (the rounding), then exactly
		us, ts = np.nonzero(s > self.loose[:, None]) # by unit, then sample
		zs = s[us, ts] / self.root[us]
		scale = s[us, ts] / self.norm2[us]
		ok = (zs > self.zu[us]) & (scale >= SCALE[0]) & (scale <= SCALE_ALONE_MAX)
		us, ts, zs = us[ok], ts[ok], zs[ok]
		window = np.clip(ts[:, None] + np.arange(-w, w + 1), 0, s.shape[1] - 1) # the highest of its unit's within
		# MATCH_W
		ok = zs >= (s[us[:, None], window] / self.root[us][:, None]).max(axis=1)
		us, ts, zs = us[ok], ts[ok], zs[ok]
		if len(taken) and len(ts): # not a second spike of a unit within MATCH_W of one it has: what is left of
			# another unit's spike under its template doesn't count, nor hide that unit's
			key = us * KEY + c0 + ts
			i = np.minimum(np.searchsorted(taken, key - w), len(taken) - 1)
			keep = ~((np.abs(taken[i] - key) <= w) | (np.abs(taken[np.maximum(i - 1, 0)] - key) <= w))
			us, ts, zs = us[keep], ts[keep], zs[keep]
		# Not the best near it: a unit near it (its peak among its sites) peaks higher within MATCH_W (the lower unit
		# number, if as high)
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
		tc, uc = ts[ok], us[ok]
		# Whether a unit near each (its peak among its sites) peaks within MATCH_W of it
		a, b = np.searchsorted(t_o, tc - w), np.searchsorted(t_o, tc + w, side="right")
		n = b - a
		which = np.repeat(np.arange(len(tc)), n)
		j = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n - a, n)
		v = u_o[j]
		near = np.bincount(which[(v != uc[which]) & self.nearmat[uc[which], v]], minlength=len(tc)) > 0
		return (c0 + tc, uc, zs[ok], near), (c0 + t_o, u_o)

	def Group(self, R, a0, t, u, cand, paired, taken):
		"""Other units' spikes at once with unit u's at t: units near the group (their peak among a member's sites)
		peaking within MATCH_W of t (this pass's peaks, of units with no spike found within MATCH_W of it) are added
		one at a time, the
		one that adds most first, at the lag near its peak that fits best, with all the group's templates' sizes
		fitted together on the sites they cover; one is added when every size stays within SCALE and it adds at least
		its unit's threshold squared to what the group explained, up to GROUP units. [(sample, unit, size), ...] for
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
				mine = taken.get(v)
				if mine is not None and min(abs(x - peak) for x in mine) <= w:
					continue
				# Its own score's peak is pulled by the others' spikes: the lag that fits best, near it
				for tv in range(max(t - w, peak - PAIR_SEARCH), min(t + w, peak + PAIR_SEARCH) + 1):
					if (tv, v) not in paired:
						trials.append((tv, v))
			if not trials:
				break
			sizes, together = self.PairFits(R, a0, t, u, trials) if len(group) == 1 else self.JointFits(R, a0, group,
				trials)
			gain = together - explained
			zv = self.zu[[v for _, v in trials]]
			ok = (gain > zv * zv) & np.all((sizes >= SCALE[0]) & (sizes <= SCALE[1]), axis=1)
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

	def PairFits(self, R, a0, t, u, trials):
		"""JointFits of unit u's spike at t alone with each trial: the same numbers, with less to set up."""
		L = el.BEFORE + el.AFTER
		tv = np.array([tt for tt, _ in trials])
		v = np.array([uu for _, uu in trials])
		b0 = float(R[u, t - a0])
		b1 = R[v, tv - a0].astype(np.float64)
		m00, m11 = float(self.norm2[u]), self.norm2[v]
		p = self.pos[u, v]
		has = (p >= 0) & (np.abs(tv - t) < L)
		m01 = np.zeros(len(tv))
		m01[has] = self.cross[u][p[has], (tv - t + L - 1)[has]]
		det = m00 * m11 - m01 * m01
		half = 0.5 * (m00 + m11)
		root = np.sqrt(np.maximum(half * half - det, 0.0))
		small = half - root
		bad = ~(small > 0) | ((half + root) > 1e6 * small)
		det = np.where(bad, 1.0, det)
		a = np.stack([(m11 * b0 - m01 * b1) / det, (m00 * b1 - m01 * b0) / det], axis=1)
		together = a[:, 0] * b0 + a[:, 1] * b1
		together[bad] = -np.inf
		return a, together

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
			together = a[:, 0] * b[:, 0] + a[:, 1] * b[:, 1]
		else: # three: M = L D L^T written out (the same arithmetic as numba's loop); can't be told apart when a pivot
			# is 0 or under a millionth of the largest
			a, together, bad = Solve3(M[:, 0, 0], M[:, 0, 1], M[:, 0, 2], M[:, 1, 1], M[:, 1, 2], M[:, 2, 2], b[:, 0],
				b[:, 1], b[:, 2])
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
		found, taken, keys = [], {}, []
		norm2, root, zu = self.norm2.tolist(), self.root.tolist(), self.zu.tolist()
		fast = Fast() if self.fast else None
		if fast is not None: # each unit's spikes found so far (no two within MATCH_W)
			taken_t = np.empty((len(self.units), (b0 - a0) // (w + 1) + 2), np.int64)
			taken_n = np.zeros(len(self.units), np.int64)
		for p in range(self.passes):
			c0, c1 = a0 + p * REACH, b0 - p * REACH
			if c1 - c0 <= 4 * w:
				break
			s = R[:, c0 - a0:c1 - a0]
			if fast is not None:
				have = np.arange(taken_t.shape[1])[None, :] < taken_n[:, None]
				keys = (np.arange(len(self.units))[:, None] * KEY + taken_t)[have]
			(new_t, new_u, new_z, new_near), cand = self.Candidates(s, c0, c1, np.sort(np.array(keys, np.int64)))
			if not len(new_t):
				break # the rest is unchanged: a next pass would find nothing new
			paired = set()
			# The highest first, each looked at again with the spikes found before it taken away: what they explain
			# isn't found again (a small unit's template, sharing a few sites, matching part of a big unit's spike)
			first = np.lexsort((new_u, new_t, -new_z))
			if fast is not None: # numba's loop: the same, to the last bit
				most = len(first) * GROUP
				out_t, out_u, out_a, out_g = (np.empty(most, np.int64), np.empty(most, np.int64), np.empty(most),
					np.empty(most, np.bool_))
				n = fast.Pass(R, a0, new_t[first], new_u[first], new_near[first], cand[0], cand[1], taken_t, taken_n,
					self.norm2, self.root, self.zu, self.nearmat, self.pos, self.cross_flat, self.cross_at,
					self.runs_flat, self.runs_at, w, PAIR_SEARCH, GROUP, SCALE[0], SCALE[1], el.BEFORE + el.AFTER,
					out_t, out_u, out_a, out_g)
				found += [(t, u, a, p, g) for t, u, a, g in zip(out_t[:n].tolist(), out_u[:n].tolist(),
					out_a[:n].tolist(), out_g[:n].tolist())]
				continue
			for t, u, near in zip(new_t[first].tolist(), new_u[first].tolist(), new_near[first].tolist()):
				mine = taken.get(u)
				if mine is not None and min(abs(x - t) for x in mine) <= w:
					continue # found already, with another unit's spike at once
				a = float(R[u, t - a0]) / norm2[u]
				if a * root[u] <= zu[u] or a < SCALE[0]:
					continue # explained by the spikes found before it
				# Units near it peaking at once: their templates fitted together (alone, the first one's fit takes in
				# the others' spikes, and leaves too little of them to be found)
				group = self.Group(R, a0, t, u, cand, paired, taken) if near else None
				if group is None and a > SCALE[1]:
					continue # too big for one spike of it, and no other unit's spike at once explains the rest
				fits = [(t, u, a)] if group is None else group
				for tt, uu, aa in fits:
					self.Subtract(R, a0, tt, uu, aa)
					taken.setdefault(uu, []).append(tt)
					keys.append(uu * KEY + tt)
					found.append((tt, uu, aa, p, group is not None))
		found = [f for f in found if lo <= f[0] < hi]
		if self.record is not None:
			for t, u, a, p, g in found:
				ok = self.sites[u] >= 0
				x = self.scored[t - el.BEFORE:t + el.AFTER][:, self.cols[u]] / self.noise[self.cols[u]] * ok
				raw = float(self.S[u, bfirst + t - self.s_first])
				energy = max(float((x.astype(np.float64) ** 2).sum()), 1e-12)
				self.record.append((bfirst + t, u, a * self.root[u], a, p, g, raw / self.root[u],
					raw / (self.root[u] * np.sqrt(energy))))
		found = [(t, u) for t, u, *_ in found if self.give[u]] # competitors' spikes aren't given out
		if not found:
			return el.Spikes()
		t = np.array([f[0] for f in found])
		u = np.array([f[1] for f in found])
		order = np.lexsort((u, t))
		t, u = t[order], u[order]
		sites = self.sites[u][:, :el.NEIGHBORS]
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

	def __init__(self, path, z=None, threads=1):
		"""z: every unit's template-spike threshold; None: each unit's learned in training (Z_THRESHOLD for a file
		without them). threads: the matcher's scores worked out in this many threads."""
		super().__init__(path)
		self.matcher = TemplateMatcher(self, z, threads=threads)
		self.name = "Kilosort 4 templates, matched ({} units, {})".format(self.given, "z {:g}".format(z) if z is not
			None else "each unit's threshold" if self.match_z is not None else "z {:g}".format(Z_THRESHOLD))
		self.pending = el.Spikes()
		self.recent = el.Spikes() # template spikes given out, the last few samples: for the next threshold spikes

	def Reset(self):
		"""Forget the samples so far (a new run: its samples counted from 0 again)."""
		self.matcher.Reset()
		self.pending, self.recent = el.Spikes(), el.Spikes()

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
		# A threshold spike within DEDUP_W samples of a template spike whose sites include its channel: that spike
		drop = np.zeros(len(out), bool)
		mine = np.flatnonzero(out.unit < 0)
		if len(mine) and len(tmpl):
			a = np.searchsorted(tmpl.sample, out.sample[mine] - DEDUP_W)
			b = np.searchsorted(tmpl.sample, out.sample[mine] + DEDUP_W, side="right")
			n = b - a
			which = np.repeat(np.arange(len(mine)), n)
			j = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n - a, n)
			hit = (tmpl.neighbors[j] == out.channel[mine][which][:, None]).any(axis=1)
			drop[mine[np.unique(which[hit])]] = True
		out = Take(out, ~drop)
		given = Take(out, out.unit >= 0)
		self.recent = Take(Cat([self.recent, given]), Cat([self.recent, given]).sample >= upto - 2 * DEDUP_W)
		return out

	def Flush(self):
		"""What is left, at the end of a replay."""
		self.pending = Cat([self.pending, self.matcher.Flush()])
		return self.Release(np.iinfo(np.int64).max)


def Sorter(path, match=None, threads=1):
	"""The live sorter from a templates file: match None, "" or 0: the threshold's spikes sorted (TemplateSorter);
	"learned": spikes found by the templates too, each unit at its threshold learned in training (MatchingSorter); a
	number: every unit at that z. threads: the matcher's, for its scores."""
	if match in (None, "", 0, "0", "off"):
		return TemplateSorter(path)
	try:
		z = None if match == "learned" else float(match)
	except ValueError:
		raise el.EphysError("--match: \"learned\", a number or 0, not {!r}".format(match))
	return MatchingSorter(path, z, threads)


# ---------------------------------------------------------------- training and evaluation

def Pair(a, b, tolerance=TOLERANCE):
	"""How many of the sorted times a and b pair up one to one, within tolerance samples (in time order)."""
	i = j = n = 0
	while i < len(a) and j < len(b):
		if abs(int(a[i]) - int(b[j])) <= tolerance:
			n, i, j = n + 1, i + 1, j + 1
		elif a[i] < b[j]:
			i += 1
		else:
			j += 1
	return n


def PairMask(a, b, tolerance=TOLERANCE):
	"""Which of the sorted times a pair up one to one with b's, within tolerance samples (as Pair)."""
	paired = np.zeros(len(a), bool)
	i = j = 0
	while i < len(a) and j < len(b):
		d = int(a[i]) - int(b[j])
		if abs(d) <= tolerance:
			paired[i], i, j = True, i + 1, j + 1
		elif d < 0:
			i += 1
		else:
			j += 1
	return paired


def Thresholds(found_t, found_u, found_z, times, clusters, units, floor):
	"""Each unit's template-spike threshold (z), learned against Kilosort: of its template spikes (found_t, their
	units' indices found_u, their z found_z), those above the threshold are paired one to one with its Kilosort spikes
	(times, clusters); the threshold is where F1 (twice those paired, over its Kilosort spikes and its template spikes
	above it) is highest; floor (the z they were found at) when that is all of them. inf for a unit none of whose
	template spikes is its."""
	z_of = np.full(len(units), np.inf)
	for k, u in enumerate(units):
		mine = found_u == k
		t, z = found_t[mine], found_z[mine]
		order = np.argsort(t, kind="stable")
		t, z = t[order], z[order]
		ks = np.sort(times[clusters == u])
		if not len(t) or not len(ks):
			continue
		paired = PairMask(t, ks)
		by_z = np.argsort(-z, kind="stable")
		f1 = 2 * np.cumsum(paired[by_z]) / (len(ks) + np.arange(1, len(t) + 1))
		best = int(np.argmax(f1))
		if f1[best] > 0: # under the last spike kept (z > threshold), halfway to the next one
			kept = z[by_z][best]
			lower = z[by_z][best + 1] if best + 1 < len(t) else floor
			z_of[k] = 0.5 * (kept + lower) if lower < kept else max(floor, kept - 1e-3)
	return z_of


def Span(seconds, rate, n):
	"""A span "a:b" in seconds (either end may be left out) as samples [start, end) within n."""
	a, _, b = (seconds or ":").partition(":")
	start = int(float(a) * rate) if a.strip() else 0
	end = int(float(b) * rate) if b.strip() else n
	return max(0, start), min(n, end)


def Train(data, stream, phy, out, span=None, labels=LABELS, say=print, accept_q=ACCEPT_Q, compete=COMPETE, learn=True,
		whiten=True):
	"""Templates for the kept units of phy's sort (and, to compete for spikes, those labelled compete), from the
	samples in span ("a:b" seconds) of data replayed as live; with whiten, the matcher's whitening and templates
	whitened (Whitening); with learn, each unit's template-spike threshold too (Learn). Writes out (.npz) and returns
	its summary."""
	t0 = time.monotonic()
	sort = ReadPhy(phy, labels, compete)
	start, end = Span(span, stream.rate, len(data))
	say("Kilosort's sort: {} units, {} kept ({}: {}){}; training on {:.1f} s to {:.1f} s".format(len(sort["labels"]),
		len(sort["kept"]), sort["label_file"] or "no labels", "/".join(labels), ", {} to compete ({})".format(
		len(sort["compete"]), "/".join(compete)) if sort["compete"] else "", start / stream.rate, end / stream.rate))
	filtered, spikes, detector = Replay(data, stream, start, end)
	say("Replayed as live: {:,} spikes ({:.0f} s)".format(len(spikes), time.monotonic() - t0))
	every = sort["kept"] + sort["compete"]
	inside = (sort["times"] >= start + el.BEFORE) & (sort["times"] < end - el.AFTER) & np.isin(sort["clusters"], every)
	times, clusters = sort["times"][inside], sort["clusters"][inside]
	peaks = PeakSites(filtered, stream.geometry, times, clusters, every, start)
	keep = np.isin(clusters, list(peaks))
	unit_of, _ = Match(spikes, stream.geometry, times[keep], clusters[keep], peaks)
	noise = np.median(np.abs(filtered[::8]), axis=0) / 0.6745
	sites_all = stream.geometry.Neighbors(1e9, TEMPLATE_SITES)
	rows = []
	rivals = set(sort["compete"])
	for u in every:
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
		waves = np.stack([filtered[i - el.BEFORE:i + el.AFTER][:, np.maximum(sites, 0)] for i in pick])
		waves[:, :, sites < 0] = 0 # a probe with fewer sites
		template = np.median(waves, axis=0).astype(np.float32)
		# Its acceptance: its own spikes' distances, as the live sorter will measure them (the threshold's spikes of
		# it, on the sites a spike keeps; for a unit with too few, its spikes at Kilosort's times)
		near, near_template = sites[:el.NEIGHBORS], template[:, :el.NEIGHBORS]
		mine = np.flatnonzero(unit_of == u)
		if len(mine) >= MIN_SPIKES:
			d = [Distance(spikes.waveforms[i], spikes.neighbors[i], near_template, near, noise)[0] for i in mine]
		else:
			d = [Distance(w[:, :el.NEIGHBORS], near, near_template, near, noise)[0] for w in waves]
		d = np.array([v for v in d if v is not None])
		if len(d) < MIN_SPIKES // 2:
			continue
		rows.append((u, sort["labels"][u], peaks[u], sites, template, float(np.quantile(d, accept_q)), len(ks),
			u in rivals, pick))
	if not any(not r[7] for r in rows):
		raise el.EphysError("No unit had {} live spikes in the training span: nothing to sort with.".format(MIN_SPIKES))
	g = stream.geometry # the units in their order on the probe (their peak site's shank, height, side): units near each
	# other are next to each other, which the matcher's work goes by
	rows.sort(key=lambda r: (int(g.shank[r[2]]), float(g.z[r[2]]), float(g.x[r[2]]), int(r[0])))
	arrays = dict(units=np.array([r[0] for r in rows]), labels=np.array([r[1] for r in rows]),
		peaks=np.array([r[2] for r in rows]), sites=np.stack([r[3] for r in rows]),
		templates=np.stack([r[4] for r in rows]), accept=np.array([r[5] for r in rows], np.float32),
		trained_spikes=np.array([r[6] for r in rows]), compete=np.array([r[7] for r in rows], bool),
		noise=noise.astype(np.float32), rate=stream.rate,
		span_samples=np.array([start, end]), phy=os.path.abspath(phy), label_file=sort["label_file"] or "",
		kept_labels=",".join(labels))
	if whiten: # the matcher's: the samples whitened (in place: minutes of a probe are GB), the templates again
		W = Whitening(filtered, stream.geometry)
		for a in range(0, len(filtered), 1 << 15):
			filtered[a:a + (1 << 15)] = filtered[a:a + (1 << 15)] @ W.T
		noise_w = np.median(np.abs(filtered[::8]), axis=0) / 0.6745
		templates_w = []
		for r in rows:
			sites, pick = r[3], r[8]
			waves = np.stack([filtered[i - el.BEFORE:i + el.AFTER][:, np.maximum(sites, 0)] for i in pick])
			waves[:, :, sites < 0] = 0
			templates_w.append(np.median(waves, axis=0).astype(np.float32))
		arrays.update(whiten=W, templates_w=np.stack(templates_w), noise_w=noise_w.astype(np.float32))
		say("Whitened as Kilosort 4 whitens (each channel from its {} nearest sites), for the matcher".format(
			WHITEN_SITES))
	np.savez_compressed(out, **arrays)
	filtered = None # (minutes of a probe: GB)
	mine = [r for r in rows if not r[7]]
	summary = {"units": len(mine), "kept_units": len(sort["kept"]), "competing_units": len(rows) - len(mine),
		"trained_spikes": int(sum(r[6] for r in mine)), "live_spikes": len(spikes),
		"matched_live_spikes": int(np.isin(unit_of, [r[0] for r in mine]).sum()), "seconds": round(
		time.monotonic() - t0, 1), "out": out}
	say("Templates for {} of {} kept units{}, from {:,} of their Kilosort spikes ({:,} of the {:,} live spikes "
		"theirs): {}".format(summary["units"], summary["kept_units"], ", and {} to compete".format(
		summary["competing_units"]) if summary["competing_units"] else "", summary["trained_spikes"],
		summary["matched_live_spikes"], summary["live_spikes"], out))
	if learn:
		arrays["match_z"] = Learn(data, stream, out, times, clusters, start, end, say)
		np.savez_compressed(out, **arrays)
		z = arrays["match_z"][~arrays["compete"]]
		summary["match_z_median"] = float(np.median(z[np.isfinite(z)])) if np.isfinite(z).any() else None
		summary["units_never_matched"] = int((~np.isfinite(z)).sum())
		summary["seconds"] = round(time.monotonic() - t0, 1)
	return summary


def Learn(data, stream, path, times, clusters, start, end, say=print):
	"""Each unit's template-spike threshold, learned against Kilosort's sort (Thresholds): the samples [start, end)
	replayed through the matching sorter with the templates in path, every unit's spikes looked at from LEARN_Z, and
	paired with Kilosort's (times, clusters) from a second in (as live, the filter settles first)."""
	t0 = time.monotonic()
	sorter = MatchingSorter(path, LEARN_Z)
	Fast(wait=True)
	sorter.matcher.record = []
	ReplayMatched(data, stream, start, end, sorter)
	rec = np.array(sorter.matcher.record, np.float64).reshape(-1, 8)
	first = start + int(stream.rate)
	rec = rec[rec[:, 0] >= first]
	inside = (times >= first) & (times < end - el.AFTER)
	z = Thresholds(rec[:, 0].astype(np.int64), rec[:, 1].astype(np.int64), rec[:, 2], times[inside],
		clusters[inside], sorter.units, LEARN_Z)
	given = ~sorter.compete
	finite = np.isfinite(z) & given
	say("Each unit's template-spike threshold, learned from its {:,} template spikes at z {:g} or more against "
		"Kilosort's ({:.0f} s): median z {:.1f} (a quarter of units under {:.1f}, a quarter over {:.1f}){}".format(
		len(rec), LEARN_Z, time.monotonic() - t0, *(np.percentile(z[finite], [50, 25, 75]) if finite.any() else
		(np.nan,) * 3), "; {} units none of whose template spikes were theirs".format(int((given & ~finite).sum()))
		if (given & ~finite).any() else ""))
	return z.astype(np.float32)


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


def Evaluate(data, stream, phy, templates, span=None, warm_s=1.0, say=print, report=None, match=None, threads=1):
	"""The templates' sort of the samples in span against Kilosort's there: per unit (the templates' units with a
	label in report; default all) the share of its Kilosort spikes found live and given to it (sensitivity), and the
	share of the live spikes given to it that are its (precision); the live spikes the templates left unsorted; the
	time a spike took to sort. Every unit with a template competes for the spikes (mua units' templates take their
	own spikes from the good units'), and a live spike is a unit's when it matches one of that unit's Kilosort
	spikes. match: as Sorter's."""
	sorter = Sorter(templates, match, threads)
	match = isinstance(sorter, MatchingSorter) and match
	sort = ReadPhy(phy, tuple(set(str(l) for l in sorter.labels)))
	start, end = Span(span, stream.rate, len(data))
	warm = max(0, start - int(warm_s * stream.rate)) # the noise measured before the span, as live would have
	if match:
		if sorter.matcher.fast:
			Fast(wait=True) # numba's loop loaded before the clock starts (the same spikes either way)
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
		# Unit by unit: its Kilosort spikes and the spikes given to it, paired one to one (two units' spikes at once
		# each count for their own unit; above, a Kilosort spike pairs with one live spike near it, whoever's)
		own = Pair(np.sort(times[ks]), np.sort(spikes.sample[given]))
		rows.append({"unit": u, "label": sort["labels"].get(u), "peak_site": peaks[u], "kilosort_spikes": int(ks.sum()),
			"found_live": int(found.sum()), "found_and_given": int(right.sum()), "given": int(given.sum()),
			"given_and_its": int((given & (truth == u)).sum()),
			"sensitivity": float(right.sum() / ks.sum()) if ks.sum() else None,
			"precision": float((given & (truth == u)).sum() / given.sum()) if given.sum() else None,
			"own_paired": own, "own_sensitivity": float(own / ks.sum()) if ks.sum() else None,
			"own_precision": float(own / given.sum()) if given.sum() else None})
	sens = [r["sensitivity"] for r in rows if r["sensitivity"] is not None]
	prec = [r["precision"] for r in rows if r["precision"] is not None]
	own_sens = [r["own_sensitivity"] for r in rows if r["own_sensitivity"] is not None]
	own_prec = [r["own_precision"] for r in rows if r["own_precision"] is not None]
	n_ks, n_given, n_own = (sum(r[k] for r in rows) for k in ("kilosort_spikes", "given", "own_paired"))
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
		"sort_us_per_spike": round(per_spike_us, 1), "sort_s_per_s": round(per_second, 3),
		"match_z": (match if match == "learned" else float(match)) if match else None,
		"numba": FastStatus()["numba"] if match and sorter.matcher.fast else None, "threads": threads,
		"given_per_s": round(float(np.isin(spikes.unit, [u for u, c in zip(units, sorter.compete) if not c]).sum()) /
			((end - start) / stream.rate), 1),
		"template_spikes": int((spikes.unit >= 0).sum()) if match else None,
		"own_sensitivity_median": float(np.median(own_sens)) if own_sens else None,
		"own_sensitivity_p25_p75": [float(np.percentile(own_sens, 25)), float(np.percentile(own_sens, 75))] if own_sens
			else None,
		"own_precision_median": float(np.median(own_prec)) if own_prec else None,
		"own_precision_p25_p75": [float(np.percentile(own_prec, 25)), float(np.percentile(own_prec, 75))] if own_prec
			else None,
		"own_pooled_sensitivity": n_own / n_ks if n_ks else None, "own_pooled_precision": n_own / n_given if n_given
			else None, "per_unit": rows}
	say("Sorted {:,} of {:,} live spikes in {:.1f} to {:.1f} s ({:.0f} us a spike). Per unit, median: {:.0%} of its "
		"Kilosort spikes found live and given to it, {:.0%} of the spikes given to it are its. Unit by unit (its "
		"Kilosort spikes and those given to it, paired): {:.0%} and {:.0%}.".format(summary["sorted"], len(spikes),
		*summary["span_s"], per_spike_us, summary["sensitivity_median"] or 0, summary["precision_median"] or 0,
		summary["own_sensitivity_median"] or 0, summary["own_precision_median"] or 0))
	return summary
