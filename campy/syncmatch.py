"""
The trigger board's random sync pulses, as another recorder saw them, matched
to the pulses the board planned: how the board's clock and the recorder's line
up, and where the recorder missed a pulse or has one the board never made.

  campy-syncmatch <edges.csv | edges.npy> --sync <recording>/sync.json [--sample-rate HZ]

The sync output (syncPin, campy/trigger/trigger.ino; or the ChronicRecord
Pico's, chronicrecord/chronic_timer) makes pulses with random gaps, 200 to 1000
whole milliseconds by default, from a seed campy picks for each recording and
saves in sync.json. Wire it to an input of any recorder with its own clock (an
ephys or photometry system, a DAQ), then give this the recorder's rise times:

- a .npy file: one row per pulse, its rise time; or two columns, rise and fall;
- a .csv/.txt file: the same, one pulse per line (a header line is skipped).

Times are seconds, or samples with --sample-rate. Only the sync line's edges:
pulses of the wrong width (with falls given) are set aside as extra.

What it does:
- works out every planned rise from the seed: board microseconds from the
  trigger's time 0, when its first frame period starts (frame k's trigger
  rises at k x period + period / 2); with trigger.ino, each edge that it held
  back from a camera edge (Hold) where it was made;
- finds the board's clock rate first: any four gaps in a row (each one of 801
  values) say where in the plan they are, read at rates within
  --max-rate-error of nominal (3% by default: a board run from a ceramic
  resonator, 0.5%, or an internal oscillator, 1-2%, as well as a crystal);
- fits recorder time = a + b x board time through every matched pulse: one
  line, or, when one line doesn't fit to --line-us (a clock whose rate wanders
  with temperature, as every clock's does a little), one line per --segment-s (30 s by default);
- reports the offset, the rate (ppm), the residuals, and every pulse missing
  from the recorder or extra on it.

Writes <edges>_syncmatch.csv (one row per pulse seen, missing or extra) and
<edges>_syncmatch.json (the fit and the counts); with --frames N, also
<edges>_frames.csv: each camera frame's trigger on the recorder's clock.
"""

import argparse, csv, json, os, sys

import numpy as np

from campy import strictjson

GAMMA = np.uint64(0x9E3779B97F4A7C15)
RUN = 4 # gaps in a row that place a stretch of pulses in the plan
GAP_TOLERANCE_MS = 0.3 # how far from a whole millisecond a gap may read
RATE_STEP = 1e-4 # the rate search's step: 0.05 ms at most on a 1 s gap
CHUNK = 256 # rises searched together
SEGMENT_S = 30.0 # a line per this many board seconds, when one line does not fit
# trigger.ino's hold: a sync edge due within GUARD us before a camera edge, or on it, waits until AFTER us after it
HOLD_GUARD_US = 50
HOLD_AFTER_US = 10


def SplitMix64(seed, j):
	"""The j-th numbers (array) of SplitMix64 from seed: as trigger.ino's (and chronic_timer.ino's) SplitMix64."""
	with np.errstate(over="ignore"):
		z = np.uint64(seed) + np.asarray(j, np.uint64) * GAMMA
		z = (z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
		z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
		return z ^ (z >> np.uint64(31))


def Gaps(seed, min_gap_ms, max_gap_ms, count):
	"""The first count gaps, whole milliseconds: gap j (from 1) = min + SplitMix64(seed, j) % (max - min + 1)."""
	span = np.uint64(max_gap_ms - min_gap_ms + 1)
	return (SplitMix64(seed, np.arange(1, count + 1, dtype=np.uint64)) % span).astype(np.int64) + min_gap_ms


def PlannedRises(seed, min_gap_ms, max_gap_ms, until_us):
	"""Every sync rise up to until_us, as planned (before any hold): pulse k at the sum of gaps 1..k+1, in us."""
	count = int(until_us // (min_gap_ms * 1000)) + 2
	rises = np.cumsum(Gaps(seed, min_gap_ms, max_gap_ms, count)) * 1000
	return rises[rises <= until_us]


def Hold(t_us, period_us, guard_us=HOLD_GUARD_US, after_us=HOLD_AFTER_US):
	"""
	How long trigger.ino holds each sync edge planned at t_us (us from time 0):
	the camera edges are at 0 and period // 2 in each frame period, and an edge
	due within guard_us before one, or on it, waits until after_us after it.
	As the sketch's SyncHold.
	"""
	t = np.asarray(t_us, np.int64)
	period = int(period_us)
	half = period // 2
	wait = np.zeros(t.shape, np.int64)
	for _ in range(3):
		r = (t + wait) % period
		to_edge = np.where(r == 0, 0, np.where(r <= half, half - r, period - r))
		hit = to_edge <= guard_us
		if not hit.any():
			break
		wait = np.where(hit, wait + to_edge + after_us, wait)
	return wait


def SyncEdges(sync, until_us):
	"""
	(rises, falls): every sync pulse's edges up to until_us, board us from time 0,
	where the board made them. sync: sync.json's settings (seed, min_gap_ms,
	max_gap_ms, width_us; with frame_period_us, trigger.ino's holds).
	"""
	planned = PlannedRises(sync["seed"], sync["min_gap_ms"], sync["max_gap_ms"], until_us)
	falls = planned + int(sync["width_us"])
	period = sync.get("frame_period_us")
	if period:
		guard, after = sync.get("hold_guard_us", HOLD_GUARD_US), sync.get("hold_after_us", HOLD_AFTER_US)
		return planned + Hold(planned, period, guard, after), falls + Hold(falls, period, guard, after)
	return planned, falls


def TriggerPeriodUs(frame_rate):
	"""trigger.ino's frame period for a frameRate: it keeps the whole frames per second, then 1e6 / that, in whole us."""
	fps = int(float(frame_rate))
	return int(1e6 / fps) if fps > 0 else None


# ------------------------------------------------------------------ reading the recorder's edges

def ReadEdges(path, sample_rate=None):
	"""(rises, falls or None), in seconds, sorted: from a .npy array or a csv/txt file (see the top of this file)."""
	if path.lower().endswith(".npy"):
		data = np.asarray(np.load(path), np.float64)
	else:
		rows = []
		with open(path, newline="") as f:
			sample = f.read(4096)
			f.seek(0)
			delimiter = "\t" if "\t" in sample else ("," if "," in sample else None)
			for line in f:
				cells = [c for c in (line.strip().split(delimiter) if delimiter else line.split()) if c.strip() != ""]
				if not cells:
					continue
				try:
					rows.append([float(c) for c in cells[:2]])
				except ValueError:
					if rows:
						raise SystemExit("{}: a line that isn't numbers after the first: {!r}".format(path, line.strip()))
		if not rows:
			raise SystemExit("{}: no edge times in it".format(path))
		width = min(len(r) for r in rows)
		data = np.array([r[:width] for r in rows], np.float64)
	if data.ndim == 1:
		rises, falls = data, None
	elif data.ndim == 2 and data.shape[1] >= 1:
		rises, falls = data[:, 0], (data[:, 1] if data.shape[1] >= 2 else None)
	else:
		raise SystemExit("{}: expected one column of rise times, or two (rise, fall)".format(path))
	if sample_rate:
		rises = rises / float(sample_rate)
		falls = None if falls is None else falls / float(sample_rate)
	order = np.argsort(rises, kind="stable")
	return rises[order], (None if falls is None else falls[order])


# ------------------------------------------------------------------ matching

def Keys(gaps, base):
	"""The plan's keys: key k encodes the RUN gaps from pulse k on (gaps[k+1..k+RUN], gaps[0] being pulse 0's)."""
	n = len(gaps) - RUN
	keys = np.zeros(max(n, 0), np.int64)
	for j in range(RUN - 1, -1, -1):
		keys = keys * base + gaps[1 + j:1 + j + n]
	order = np.argsort(keys, kind="stable")
	return keys[order], order


def SearchChunk(rises, keys, order, min_ms, max_ms, rates):
	"""
	Anchors in one stretch of rises: for each rate tried, the windows of RUN
	gaps that read as whole ms and match one place in the plan. Returns
	(rate index, first rise index, pulse) arrays.
	"""
	d = np.diff(rises) * 1000 # recorder ms
	if len(d) < RUN:
		return np.zeros(0, int), np.zeros(0, int), np.zeros(0, int)
	ms = d[None, :] / rates[:, None]
	whole = np.round(ms).astype(np.int64)
	usable = (np.abs(ms - whole) < GAP_TOLERANCE_MS) & (whole >= min_ms) & (whole <= max_ms)
	base = max_ms + 1
	m = len(d) - RUN + 1
	key = np.zeros((len(rates), m), np.int64)
	ok = np.ones((len(rates), m), bool)
	for j in range(RUN - 1, -1, -1):
		key = key * base + np.where(usable[:, j:j + m], whole[:, j:j + m], 0)
		ok &= usable[:, j:j + m]
	ri, wi = np.nonzero(ok)
	k = key[ri, wi]
	lo, hi = np.searchsorted(keys, k, "left"), np.searchsorted(keys, k, "right")
	one = (hi - lo) == 1
	return ri[one], wi[one], order[lo[one]]


def Anchors(rises, planned_us, gaps, sync, max_rate_error):
	"""
	(rise index, pulse) pairs that place the recorder's rises in the plan, and
	the rate the search found in each stretch (recorder s per board s). Each
	stretch tries the rate before it first (a clock's rate moves slowly), then
	every rate within max_rate_error.
	"""
	min_ms, max_ms = int(sync["min_gap_ms"]), int(sync["max_gap_ms"])
	if (max_ms + 1) ** RUN >= 2 ** 62:
		raise SystemExit("max_gap_ms {} is too large to match on".format(max_ms))
	keys, order = Keys(gaps, max_ms + 1)
	steps = int(np.ceil(max_rate_error / RATE_STEP))
	every = 1 + RATE_STEP * np.arange(-steps, steps + 1)
	pairs, found_rates, last = [], [], None
	for start in range(0, max(len(rises) - RUN, 0), CHUNK - RUN):
		chunk = rises[start:start + CHUNK]
		for rates in ([last + RATE_STEP * np.arange(-10, 11)] if last else []) + [every]:
			ri, wi, k = SearchChunk(chunk, keys, order, min_ms, max_ms, rates)
			if len(ri) == 0:
				continue
			# The true rate gives the most windows; a window that matches by chance is a lone one
			counts = np.bincount(ri, minlength=len(rates))
			best = int(np.argmax(counts))
			if counts[best] < 2:
				continue
			rate = rates[best]
			near = np.abs(ri - best) <= 2
			w, kk = wi[near], k[near]
			# The true windows agree with each other: against the commonest pulse - rise step (no pulse missed in
			# between), each one's time from that one's is the rate times the plan's, to within the rate's step
			step = kk - w
			values, counts = np.unique(step, return_counts=True)
			ref = int(np.flatnonzero(step == values[np.argmax(counts)])[0])
			dt_plan = (planned_us[kk] - planned_us[kk[ref]]) / 1e6
			dt_rec = chunk[w] - chunk[w[ref]]
			keep = np.abs(dt_rec - rate * dt_plan) < 0.002 + 3 * RATE_STEP * np.abs(dt_plan)
			for w, kk in zip(w[keep], kk[keep]):
				pairs += [(start + int(w) + j, int(kk) + j) for j in range(RUN + 1)]
			found_rates.append(float(rate))
			last = float(rate)
			break
	return sorted(set(pairs)), found_rates


class ClockMap:
	"""
	Recorder seconds = a + b x board seconds: one line, or one line per
	segment of board time (bounds: each segment's first board second).
	"""

	def __init__(self, lines, bounds):
		self.lines = lines # [(a, b)]
		self.bounds = np.asarray(bounds, np.float64) # board s where each line starts (the first is -inf)

	@staticmethod
	def Fit(board_s, rec_s, segment_s=None):
		board_s, rec_s = np.asarray(board_s, np.float64), np.asarray(rec_s, np.float64)
		if not segment_s:
			t0 = board_s.mean()
			b, a = np.polyfit(board_s - t0, rec_s, 1)
			return ClockMap([(a - b * t0, b)], [-np.inf])
		edges = np.arange(np.floor(board_s.min() / segment_s), np.floor(board_s.max() / segment_s) + 1) * segment_s
		lines, bounds = [], []
		for lo in edges:
			inside = (board_s >= lo) & (board_s < lo + segment_s)
			if inside.sum() < 3:
				continue # too few: the line before goes on through it
			t0 = board_s[inside].mean()
			b, a = np.polyfit(board_s[inside] - t0, rec_s[inside], 1)
			lines.append((a - b * t0, b))
			bounds.append(lo)
		if not lines:
			return ClockMap.Fit(board_s, rec_s)
		bounds[0] = -np.inf
		return ClockMap(lines, bounds)

	def Segment(self, board_s):
		return np.clip(np.searchsorted(self.bounds, board_s, "right") - 1, 0, len(self.lines) - 1)

	def ToRecorder(self, board_s):
		board_s = np.asarray(board_s, np.float64)
		i = self.Segment(board_s)
		a, b = np.array(self.lines).T
		return a[i] + b[i] * board_s

	def ToBoard(self, rec_s):
		rec_s = np.asarray(rec_s, np.float64)
		a, b = np.array(self.lines).T
		# Each segment's start on the recorder's clock
		starts = np.where(np.isinf(self.bounds), -np.inf, a + b * np.where(np.isinf(self.bounds), 0, self.bounds))
		i = np.clip(np.searchsorted(starts, rec_s, "right") - 1, 0, len(self.lines) - 1)
		return (rec_s - a[i]) / b[i]

	def Rate(self):
		"""The rate over the whole: recorder s per board s (the lines' rates, weighted alike)."""
		return float(np.mean([b for a, b in self.lines]))


def Nearest(planned, board):
	"""For each board time, the nearest planned time and how far from it; none planned: -1 and inf."""
	if not len(planned):
		return np.full(len(board), -1, np.int64), np.full(len(board), np.inf)
	if len(planned) == 1:
		return np.zeros(len(board), np.int64), board - planned[0]
	k = np.clip(np.searchsorted(planned, board), 1, len(planned) - 1)
	k = np.where(np.abs(planned[k - 1] - board) < np.abs(planned[k] - board), k - 1, k)
	return k, board - planned[k]


def Match(rises_s, sync, falls_s=None, max_rate_error=0.03, segment_s=None, line_us=None, until_s=None):
	"""
	The recorder's rises (seconds) matched to the board's plan. segment_s: None
	= one line if it fits to line_us (default 100 us), else SEGMENT_S segments; 0 =
	one line always; a number = segments that long. Returns a dict: the map
	(ClockMap), "pulse" (each rise's planned pulse, -1 for none), "good" (rises
	of the right width), the planned rises (board us), and the summary numbers.
	"""
	rises = np.asarray(rises_s, np.float64)
	width_us = float(sync["width_us"])
	if falls_s is not None:
		width = (np.asarray(falls_s, np.float64) - rises) * 1e6
		good = (width > 0.5 * width_us) & (width < 1.5 * width_us)
	else:
		good = np.ones(len(rises), bool)
	use = np.flatnonzero(good)
	if until_s is None:
		span = (rises[-1] - rises[0]) if len(rises) else 0
		until_s = span * (1 + max_rate_error) + 6 * 3600 # the recorder may have started hours after the trigger
	until_us = sync.get("ends_us") or until_s * 1e6
	planned, _ = SyncEdges(sync, until_us)
	gaps = Gaps(sync["seed"], sync["min_gap_ms"], sync["max_gap_ms"], len(planned)) # gaps[k]: the one that ends at pulse k
	pairs, rates = Anchors(rises[use], planned, gaps, sync, max_rate_error)
	if len(pairs) < RUN + 1:
		raise SystemExit("Too few sync pulses to match: no stretch of {} gaps in a row fits the plan at any rate within "
			"{:g}%. Is this the sync line, and is that its sync.json (seed {})?".format(RUN, max_rate_error * 100,
			sync["seed"]))
	idx = np.array([use[i] for i, k in pairs])
	pk = np.array([k for i, k in pairs])
	tol_us = sync["min_gap_ms"] * 1000 / 4
	line_us = 100.0 if line_us is None else float(line_us)

	def Fits(board_s, rec_s):
		# One line where it fits to line_us (or segment_s is 0), else segments
		if segment_s == 0:
			return None
		if segment_s:
			return float(segment_s)
		line = ClockMap.Fit(board_s, rec_s)
		return None if np.abs(rec_s - line.ToRecorder(board_s)).max() * 1e6 <= line_us else SEGMENT_S

	seg = Fits(planned[pk] / 1e6, rises[idx])
	cmap = ClockMap.Fit(planned[pk] / 1e6, rises[idx], seg)
	for round_ in range(4):
		board = cmap.ToBoard(rises[use]) * 1e6
		k, d = Nearest(planned, board)
		ok = np.abs(d) < tol_us
		pulse = np.where(ok, k, -1)
		# Two rises on one planned pulse: the nearer one is it
		best = {}
		for i in np.flatnonzero(ok):
			if pulse[i] in best and abs(d[best[pulse[i]]]) <= abs(d[i]):
				pulse[i] = -1
				continue
			if pulse[i] in best:
				pulse[best[pulse[i]]] = -1
			best[pulse[i]] = i
		m = pulse >= 0
		board_s, rec_s = planned[pulse[m]] / 1e6, rises[use][m]
		if round_ == 0:
			seg = Fits(board_s, rec_s)
		cmap = ClockMap.Fit(board_s, rec_s, seg)
	mode = "line" if seg is None else "segments"
	all_pulse = np.full(len(rises), -1, np.int64)
	all_pulse[use] = pulse
	matched = all_pulse >= 0
	residual = np.full(len(rises), np.nan)
	residual[matched] = (rises[matched] - cmap.ToRecorder(planned[all_pulse[matched]] / 1e6)) * 1e6
	# Planned pulses within the recorder's stretch of the train that it doesn't have
	lo_b, hi_b = cmap.ToBoard(rises[[0, -1]]) * 1e6
	inside = np.flatnonzero((planned > lo_b + tol_us) & (planned < hi_b - tol_us))
	missing = np.setdiff1d(inside, all_pulse[matched])
	rate = cmap.Rate()
	return dict(map=cmap, pulse=all_pulse, good=good, residual_us=residual, planned_us=planned, missing=missing,
		mode=mode, segment_s=seg, rates_found=rates,
		offset_s=float(cmap.ToRecorder(0.0)), rate=rate, drift_ppm=(rate - 1) * 1e6)


def FrameTimes(result, period_us, frames):
	"""Each camera frame's trigger rise (k from 0) on the recorder's clock: board time k x period + period // 2."""
	board_us = np.arange(frames, dtype=np.int64) * int(period_us) + int(period_us) // 2
	return board_us, result["map"].ToRecorder(board_us / 1e6)


# ------------------------------------------------------------------ the command

def Rows(rises, falls, result):
	planned, pulse, residual = result["planned_us"], result["pulse"], result["residual_us"]
	cmap = result["map"]
	rows = []
	for i in range(len(rises)):
		row = dict(recorder_s=round(float(rises[i]), 7),
			width_ms=round(float(falls[i] - rises[i]) * 1000, 3) if falls is not None else "")
		if pulse[i] >= 0:
			row.update(pulse=int(pulse[i]), board_s=round(planned[pulse[i]] / 1e6, 6), residual_us=round(float(residual[i]), 1))
		else:
			row["note"] = "extra: {}".format("a pulse the board didn't plan" if result["good"][i] else "not the sync pulse's width")
		rows.append(row)
	for j in result["missing"]:
		rows.append(dict(recorder_s=round(float(cmap.ToRecorder(planned[j] / 1e6)), 7), pulse=int(j),
			board_s=round(planned[j] / 1e6, 6), note="missing: planned, not on the recorder"))
	rows.sort(key=lambda r: r["recorder_s"])
	return rows


def Main(argv=None):
	parser = argparse.ArgumentParser(prog="campy-syncmatch", description=__doc__.split("\n\n")[0].strip(),
		epilog=__doc__.split("\n\n", 1)[1], formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("edges", help="the recorder's sync rises: .npy, or .csv/.txt (rise[, fall] per line)")
	parser.add_argument("--sync", required=True, help="the recording's sync.json (or the ChronicRecord Pico's sync_*.json)")
	parser.add_argument("--sample-rate", type=float, help="the edges are sample numbers at this many per second")
	parser.add_argument("--max-rate-error", type=float, default=0.03,
		help="how far the board's clock (against the recorder's) may be from nominal, as a fraction (default 0.03)")
	parser.add_argument("--segment-s", type=float, default=None,
		help="one line per this many seconds of board time; 0: one line for all (default: one line if it fits to "
		"--line-us, else 30)")
	parser.add_argument("--line-us", type=float, default=100, help="how close one line must fit (default 100 us)")
	parser.add_argument("--until-s", type=float, help="how far into the board's train to plan (default: the edges' span "
		"plus 6 hours, for a recorder started after the trigger)")
	parser.add_argument("--frames", type=int, help="also write the first N camera frames' trigger times on the recorder's clock")
	parser.add_argument("--out", help="where the files go, and their name's start (default: next to the edges file)")
	args = parser.parse_args(argv)
	with open(args.sync) as f:
		sync = json.load(f)
	if "sync" in sync and isinstance(sync["sync"], dict): # a trigger.json-style record holding it
		sync = sync["sync"]
	rises, falls = ReadEdges(args.edges, args.sample_rate)
	result = Match(rises, sync, falls, args.max_rate_error, args.segment_s, args.line_us, args.until_s)
	rows = Rows(rises, falls, result)
	res = result["residual_us"][result["pulse"] >= 0]
	extra = int((result["pulse"] < 0).sum())
	summary = dict(edges=os.path.abspath(args.edges), sync_file=os.path.abspath(args.sync), seed=sync["seed"],
		rises=len(rises), matched=int((result["pulse"] >= 0).sum()), missing=int(len(result["missing"])), extra=extra,
		fit=result["mode"], segment_s=result["segment_s"], recorder_s_at_board_0=result["offset_s"],
		recorder_s_per_board_s=result["rate"], drift_ppm=result["drift_ppm"],
		residual_us_max=float(np.abs(res).max()), residual_us_rms=float(np.sqrt((res ** 2).mean())),
		lines=[dict(from_board_s=None if np.isinf(lo) else float(lo), a=a, b=b)
			for (a, b), lo in zip(result["map"].lines, result["map"].bounds)])
	print("{} rises, {} matched to the board's plan (seed {}), {} missing, {} extra.".format(len(rises), summary["matched"],
		sync["seed"], summary["missing"], extra))
	print("Board time 0 is recorder time {:.6f} s.".format(result["offset_s"]))
	print("The recorder counts {:.9f} s per second of the board's clock: {:+.1f} ppm ({:+.1f} ms an hour).".format(
		result["rate"], result["drift_ppm"], result["drift_ppm"] * 3.6))
	if abs(result["drift_ppm"]) > 300:
		print("That is more than a crystal's error: the board (or the recorder) likely runs from a ceramic resonator or "
			"an internal oscillator. The match still holds; its camera frame rate is off by as much.")
	print("Fit: {}; residuals {:.1f} us at most, {:.1f} us rms.".format(
		"one line" if result["mode"] == "line" else "{} lines, one per {:g} s".format(len(result["map"].lines),
		result["segment_s"]), summary["residual_us_max"], summary["residual_us_rms"]))
	for row in [r for r in rows if "note" in r][:20]:
		print("  {:.6f} s: {}".format(row["recorder_s"], row["note"]))
	stem = args.out or os.path.splitext(args.edges)[0]
	with open(stem + "_syncmatch.csv", "w", newline="") as f:
		w = csv.DictWriter(f, fieldnames=["recorder_s", "width_ms", "pulse", "board_s", "residual_us", "note"])
		w.writeheader()
		w.writerows(rows)
	if args.frames:
		period = sync.get("frame_period_us")
		if not period:
			raise SystemExit("--frames needs the trigger's frame period (frame_period_us in campy's sync.json)")
		board_us, rec = FrameTimes(result, period, args.frames)
		with open(stem + "_frames.csv", "w", newline="") as f:
			w = csv.writer(f)
			w.writerow(["frame", "board_s", "recorder_s"])
			for k in range(args.frames):
				w.writerow([k, round(board_us[k] / 1e6, 6), round(float(rec[k]), 7)])
	with open(stem + "_syncmatch.json", "w") as f:
		strictjson.dump(summary, f, indent=1)
	print("Written to {}_syncmatch.csv and .json{}".format(stem, ", and _frames.csv" if args.frames else ""))
	return summary, rows


if __name__ == "__main__":
	Main()
