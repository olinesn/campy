"""
A live sorter for campy/ephyslive.py, trained on a Kilosort 4 sort of a
recording of the same probe (its phy folder: as Kilosort left it, or
curated in phy).

Training (campy-ephys train): the recording is replayed through the live
filter and spike finder, exactly as live (the same samples give the same
spikes, however they come in pieces). Each live spike within a few samples of
a spike of a kept unit (phy's cluster_group.tsv labels, else Kilosort's
cluster_KSLabel.tsv; "good" and "mua" by default), on a site near that unit's peak
site, takes that unit. Each unit's template is then the median of its live
spikes' waveforms on its 12 nearest sites, as the live filter shapes them
(Kilosort's own templates are whitened and filtered forwards and backwards:
not what the live view sees). Each unit also gets how far its own spikes fall
from its template (its acceptance), and the file keeps everything with what
it was trained on.

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

def Replay(data, stream, start, end, piece_s=1.0):
	"""The samples [start, end) of data (int16 [samples, channels]) through the live filter and spike finder, in
	pieces of piece_s: (filtered float32 [end-start, channels], Spikes (samples counted from 0 in data),
	the detector)."""
	f, d = el.Filter(stream), el.Detector(stream)
	n = int(stream.rate * piece_s)
	filtered = np.empty((end - start, data.shape[1]), np.float32)
	found = []
	for a in range(start, end, n):
		b = min(end, a + n)
		x = f.Apply(np.asarray(data[a:b]))
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
		mine = np.flatnonzero(unit_of == u)
		if u not in peaks or len(mine) < MIN_SPIKES:
			continue
		sites = sites_all[peaks[u]]
		pick = mine[np.linspace(0, len(mine) - 1, min(MAX_SPIKES, len(mine))).astype(int)]
		waves = np.stack([filtered[spikes.sample[i] - start - el.BEFORE:spikes.sample[i] - start + el.AFTER][:, sites]
			for i in pick if el.BEFORE <= spikes.sample[i] - start < len(filtered) - el.AFTER])
		template = np.median(waves, axis=0).astype(np.float32)
		# Its acceptance: its own spikes' distances, as the live sorter will measure them
		d = [Distance(spikes.waveforms[i], spikes.neighbors[i], template, sites, noise)[0] for i in mine]
		d = np.array([v for v in d if v is not None])
		if len(d) < MIN_SPIKES:
			continue
		rows.append((u, sort["labels"][u], peaks[u], sites, template, float(np.quantile(d, accept_q)), len(mine)))
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
	say("Templates for {} of {} kept units, from {:,} of the {:,} live spikes: {}".format(summary["units"],
		summary["kept_units"], summary["trained_spikes"], summary["live_spikes"], out))
	return summary


def Evaluate(data, stream, phy, templates, span=None, warm_s=1.0, say=print, report=None):
	"""The templates' sort of the samples in span against Kilosort's there: per unit (the templates' units with a
	label in report; default all) the share of its Kilosort spikes found live and given to it (sensitivity), and the
	share of the live spikes given to it that are its (precision); the live spikes the templates left unsorted; the
	time a spike took to sort. Every unit with a template competes for the spikes (mua units' templates take their
	own spikes from the good units'), and a live spike is a unit's when it matches one of that unit's Kilosort
	spikes."""
	sorter = TemplateSorter(templates)
	sort = ReadPhy(phy, tuple(set(str(l) for l in sorter.labels)))
	start, end = Span(span, stream.rate, len(data))
	warm = max(0, start - int(warm_s * stream.rate)) # the noise measured before the span, as live would have
	filtered, spikes, _ = Replay(data, stream, warm, end)
	in_span = spikes.sample >= start + el.BEFORE
	spikes = el.Spikes(spikes.sample[in_span], spikes.channel[in_span], spikes.amplitude[in_span],
		waveforms=spikes.waveforms[in_span], neighbors=spikes.neighbors[in_span])
	t0 = time.perf_counter()
	sorter.Assign(spikes)
	per_spike_us = 1e6 * (time.perf_counter() - t0) / max(1, len(spikes))
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
		"sort_us_per_spike": round(per_spike_us, 1), "per_unit": rows}
	say("Sorted {:,} of {:,} live spikes in {:.1f} to {:.1f} s ({:.0f} us a spike). Per unit, median: {:.0%} of its "
		"Kilosort spikes found live and given to it, {:.0%} of the spikes given to it are its.".format(summary["sorted"],
		len(spikes), *summary["span_s"], per_spike_us, summary["sensitivity_median"] or 0, summary["precision_median"]
		or 0))
	return summary
