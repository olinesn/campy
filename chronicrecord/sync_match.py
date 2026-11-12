"""
The Pico's sync pulses in a ChronicRecord session's OneBox files, matched to
the pulses it planned: how the Pico's clock and the OneBox's line up, and
where either missed or added a pulse.

  python sync_match.py <session folder or spikeglx run folder> [--sync <sync_*.json>] [--csv out.csv]

The Pico's sync output (GP6, chronic_control.py's SYNC setting, campy's
picoSync) makes 10 ms pulses with random gaps of 200 to 1000 ms, whole
milliseconds, from a seed the session picks and saves in its pico folder
(sync_<date_time>.json). Wired to the OneBox's XA1, SpikeGLX records them,
while the gate is high, on XD0's bit 1, on the same clock as everything else.
Each gap is one of 801 values, so any four in a row (about 2.4 s of pulses)
name their place in a 6-day session.

What it does:
- works out every planned rise from the seed (board microseconds from the
  session's time 0);
- finds the sync rises in each OneBox file (one per gate opening), on the
  run's own sample count (each file's firstSample), so files line up;
- anchors each file by its runs of four gaps, then fits one straight line,
  OneBox sample = a + b x board time, through every matched pulse;
- reports the offset (the OneBox's time at the board's time 0), the drift
  (the OneBox's sample rate against the Pico's crystal, in parts per million
  of its obSampRate), the residuals, and every pulse missing from the OneBox
  or extra on it (a pulse of the wrong width, or one the Pico didn't plan);
- with the session's settings (in the sync file), the same for the camera,
  gate and LED lines: each OneBox rise put on the board's clock and matched
  to the Pico's plan, so a stray edge (a camera pulse the Pico never made)
  or a missing one shows with its board time.

Writes sync_match.csv (one row per sync pulse seen, missing or extra) and
sync_match.json (the fit and the counts) in the folder. Needs numpy only.
Run it on a copy of the session, not on the rig while it records.
"""

import argparse, csv, glob, json, os, re, sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
GAMMA = np.uint64(0x9E3779B97F4A7C15)
RUN = 4 # gaps in a row that anchor a file


def SplitMix64(seed, j):
	"""The j-th numbers (array) of SplitMix64 from seed: as chronic_timer.ino's SplitMix64."""
	z = np.uint64(seed) + np.asarray(j, np.uint64) * GAMMA
	z = (z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
	z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
	return z ^ (z >> np.uint64(31))


def Gaps(seed, min_gap_ms, max_gap_ms, count):
	"""The first count gaps, whole milliseconds: gap j (from 1) = min + SplitMix64(seed, j) % (max - min + 1)."""
	span = np.uint64(max_gap_ms - min_gap_ms + 1)
	return (SplitMix64(seed, np.arange(1, count + 1, dtype=np.uint64)) % span).astype(np.int64) + min_gap_ms


def PlannedRises(seed, min_gap_ms, max_gap_ms, until_us):
	"""Every sync rise up to until_us, board microseconds from the session's time 0: pulse k at the sum of gaps 1..k+1."""
	count = int(until_us // (min_gap_ms * 1000)) + 2
	rises = np.cumsum(Gaps(seed, min_gap_ms, max_gap_ms, count)) * 1000
	return rises[rises <= until_us]


# ------------------------------------------------------------------ OneBox files (as photometry_snapshots.py)

def ReadMeta(path):
	meta = {}
	with open(path) as f:
		for line in f:
			key, _, value = line.rstrip("\n").partition("=")
			meta[key.lstrip("~")] = value
	return meta


def Channels(meta):
	"""{name: column}: from snsChanMap, (XA0;0:0)(XA1;1:1)...(XD0;12:12)(SY0;13:13)."""
	return {name: int(col) for name, col in re.findall(r"\((\w+);(\d+):\d+\)", meta["snsChanMap"])}


def ReadObx(bin_path):
	meta = ReadMeta(bin_path[:-4] + ".meta")
	n = int(meta["nSavedChans"])
	data = np.fromfile(bin_path, dtype=np.int16)
	data = data[:len(data) // n * n].reshape(-1, n)
	return meta, data


def Edges(bit):
	"""Rises and falls (sample indices of the first sample after each edge) of a 0/1 array."""
	d = np.diff(bit.astype(np.int8))
	return np.flatnonzero(d == 1) + 1, np.flatnonzero(d == -1) + 1


def RunFiles(path):
	"""The OneBox files (*.obx0.obx.bin): in the path, or within a session's spikeglx\\ folder; in order of t."""
	for pattern in ("*.obx0.obx.bin", os.path.join("*", "*.obx0.obx.bin"), os.path.join("spikeglx", "*", "*.obx0.obx.bin"),
			os.path.join("spikeglx", "*", "*", "*.obx0.obx.bin")):
		found = glob.glob(os.path.join(path, pattern))
		if found:
			return sorted(found, key=lambda p: int(re.search(r"_t(\d+)\.obx0\.obx\.bin$", p).group(1))
				if re.search(r"_t(\d+)\.obx0\.obx\.bin$", p) else -1)
	return []


def SyncFile(folder):
	"""The session's sync file: <folder>/pico/<date>/sync_*.json (the panel), or one anywhere below folder."""
	found = sorted(glob.glob(os.path.join(folder, "**", "sync_[0-9]*.json"), recursive=True)) # not sync_match.json
	if not found:
		raise SystemExit("No sync file (sync_*.json, written by chronic_control.py with SYNC=1) in {}: give it with "
			"--sync.".format(folder))
	if len(found) > 1:
		raise SystemExit("{} sync files in {} ({}): give the session's with --sync.".format(len(found), folder,
			", ".join(os.path.relpath(p, folder) for p in found)))
	return found[0]


class File:
	"""One OneBox file's lines, as rises (on the run's sample count) and their widths (samples; -1 cut by the file's end)."""

	def __init__(self, path, args):
		self.path, self.name = path, os.path.basename(path)
		meta, data = ReadObx(path)
		self.rate = float(meta["obSampRate"])
		self.first = int(meta["firstSample"])
		self.n = len(data)
		ch = Channels(meta)
		xd = data[:, ch["XD0"]].astype(np.int32) if "XD0" in ch else np.zeros(self.n, np.int32)
		self.lines = {}
		for name, b in (("sync", args.sync_bit), ("camera", args.camera_bit), ("gate", args.gate_bit),
				("blue", args.blue_bit), ("yellow", args.yellow_bit)):
			if name == "sync" and args.sync_xa:
				volts = float(meta["obAiRangeMax"]) / float(meta["obMaxInt"])
				bit = data[:, ch[args.sync_xa]].astype(np.float64) * volts > args.threshold_v
			elif b is None or b < 0:
				continue
			else:
				bit = (xd >> b) & 1
			on, off = Edges(bit)
			after = np.searchsorted(off, on)
			width = np.where(after < len(off), off[np.minimum(after, len(off) - 1)] - on, -1)
			self.lines[name] = (on + self.first, width)


# ------------------------------------------------------------------ matching

def Anchors(f, planned, min_gap_ms, max_gap_ms, keys, order):
	"""(OneBox sample, planned pulse) pairs from each run of RUN gaps that matches one place in the plan."""
	rises, _ = f.good
	if len(rises) < RUN + 1:
		return []
	ms = np.diff(rises) / f.rate * 1000
	whole = np.round(ms).astype(np.int64)
	usable = (np.abs(ms - whole) < 0.3) & (whole >= min_gap_ms) & (whole <= max_gap_ms)
	base = max_gap_ms + 1
	out = []
	for i in range(len(whole) - RUN + 1):
		if not usable[i:i + RUN].all():
			continue
		key = 0
		for g in whole[i:i + RUN][::-1]:
			key = key * base + int(g)
		lo, hi = np.searchsorted(keys, key, "left"), np.searchsorted(keys, key, "right")
		if hi - lo == 1:
			k = int(order[lo]) # the plan's gap k..k+RUN-1 is from pulse k to pulse k+RUN
			out += [(int(rises[i + j]), k + j) for j in range(RUN + 1)]
	return out


def Nearest(planned, board):
	"""For each board time, the nearest planned pulse and how far from it (us); none planned: -1 and inf."""
	if not len(planned):
		return np.full(len(board), -1, np.int64), np.full(len(board), np.inf)
	k = np.clip(np.searchsorted(planned, board), 1, len(planned) - 1) if len(planned) > 1 else np.zeros(len(board), np.int64)
	if len(planned) > 1:
		k = np.where(np.abs(planned[k - 1] - board) < np.abs(planned[k] - board), k - 1, k)
	return k, board - planned[k]


def Inside(f, planned, a, b, end_guard=1):
	"""The planned pulses whose rise falls inside a file, clear of its two ends."""
	lo = np.searchsorted(planned, (f.first + 1 - a) / b, "right")
	hi = np.searchsorted(planned, (f.first + f.n - end_guard - a) / b, "left")
	return np.arange(lo, hi)


def Fit(pairs, planned):
	s = np.array([p[0] for p in pairs], np.float64)
	t = planned[[p[1] for p in pairs]].astype(np.float64)
	t0 = t.mean()
	b, a = np.polyfit(t - t0, s, 1)
	return a - b * t0, b


def Match(files, planned, sync, width_us):
	"""Every OneBox sync rise matched to its planned pulse: the fit (a, b), and rows for the csv."""
	min_ms, max_ms = sync["min_gap_ms"], sync["max_gap_ms"]
	gaps = np.diff(np.r_[0, planned]) // 1000
	base = max_ms + 1
	if base ** RUN >= 2 ** 62:
		raise SystemExit("max_gap_ms {} is too large to match on".format(max_ms))
	n = len(gaps) - RUN
	keys = np.zeros(max(n, 0), np.int64)
	for j in range(RUN - 1, -1, -1):
		keys = keys * base + gaps[1 + j:1 + j + n] # the gap from pulse k to k+1 is gaps[k+1]
	order = np.argsort(keys, kind="stable")
	keys = keys[order]
	pairs = []
	for f in files:
		rises, width = f.lines["sync"]
		want = width_us * 1e-6 * f.rate
		f.good_mask = (width < 0) | ((width > 0.5 * want) & (width < 1.5 * want))
		f.good = (rises[f.good_mask], width[f.good_mask])
		found = Anchors(f, planned, min_ms, max_ms, keys, order)
		if len(found) >= RUN + 1:
			# A file's true anchors agree on the offset to within the drift across it (25 samples over a 257 s file at
			# 3.2 ppm); a false one, a run of gaps that repeats elsewhere in the plan, is seconds off
			offsets = np.array([s - f.rate * planned[k] / 1e6 for s, k in found])
			middle = np.median(offsets)
			found = [p for p, o in zip(found, offsets) if abs(o - middle) <= 0.03 * f.rate]
		f.anchors = found
		pairs += found
	if len(set(pairs)) < 2:
		raise SystemExit("Too few sync pulses to match: no file has {} gaps in a row that fit the plan. Is the sync line "
			"wired to the OneBox (XA1, XD0 bit 1), and is this the session's sync file?".format(RUN))
	a, b = Fit(sorted(set(pairs)), planned)
	tol_us = min_ms * 1000 / 4
	for _ in range(3):
		matched = []
		for f in files:
			rises, _ = f.good
			board = (rises - a) / b
			k, d = Nearest(planned, board)
			ok = np.abs(d) < tol_us
			f.k = np.where(ok, k, -1)
			# Two rises on one planned pulse: the nearer one is it
			seen = {}
			for i in np.flatnonzero(ok):
				far = abs(d[i])
				if f.k[i] in seen and seen[f.k[i]][1] <= far:
					f.k[i] = -1
					continue
				if f.k[i] in seen:
					f.k[seen[f.k[i]][0]] = -1
				seen[f.k[i]] = (i, far)
			matched += [(int(rises[i]), int(f.k[i])) for i in np.flatnonzero(f.k >= 0)]
		a, b = Fit(matched, planned)
	return a, b, matched


def Rows(files, planned, a, b, width_us):
	rows, missing, extra = [], [], []
	for f in files:
		rises, width = f.lines["sync"]
		k = np.full(len(rises), -1, np.int64)
		k[f.good_mask] = f.k
		for i in range(len(rises)):
			row = dict(file=f.name, sample=int(rises[i]), onebox_s=round(rises[i] / f.rate, 6),
				width_ms=round(width[i] / f.rate * 1000, 3) if width[i] >= 0 else "")
			if k[i] >= 0:
				row.update(pulse=int(k[i]), board_s=round(planned[k[i]] / 1e6, 6),
					residual_us=round((rises[i] - (a + b * planned[k[i]])) / b, 1))
			else:
				row["note"] = "extra: {}".format("not a planned pulse" if f.good_mask[i] else
					"width {} ms, not {:g}".format(row["width_ms"], width_us / 1000))
				extra.append(row)
			rows.append(row)
		# Planned pulses inside the file (clear of its two ends) that it doesn't have
		inside = Inside(f, planned, a, b, width_us * 1e-6 * f.rate + 1)
		got = set(int(x) for x in f.k if x >= 0)
		for j in inside:
			if int(j) not in got:
				row = dict(file=f.name, sample=int(round(a + b * planned[j])), onebox_s=round((a + b * planned[j]) / f.rate, 6),
					pulse=int(j), board_s=round(planned[j] / 1e6, 6), note="missing: planned, not on the OneBox")
				missing.append(row)
				rows.append(row)
		f.inside = len(inside)
	rows.sort(key=lambda r: r["sample"])
	return rows, missing, extra


def PlannedLines(settings):
	"""{line: planned rise times, board us}, from chronic_control.py's plan for the session's settings."""
	sys.path.insert(0, HERE)
	import chronic_control as cc
	bouts, trains, edges = cc.Plan(settings)
	out = {"camera": np.sort(np.concatenate([start + np.arange(count, dtype=np.int64) * 10**9 // rate
		for ch, start, count, rate, width in trains])) if trains else np.zeros(0, np.int64)}
	for name, ch in (("gate", cc.GATE), ("blue", cc.BLUE), ("yellow", cc.YELLOW)):
		out[name] = np.array([t for t, c, level in edges if c == ch and level == 1], np.int64)
	return out


def CheckLine(name, files, planned, a, b, tol_us):
	"""A line's OneBox rises against the Pico's plan, on the board's clock: a summary and its odd pulses."""
	said, odd, dev, n = [], [], [], 0
	for f in files:
		# Only files the sync pulses place in the session (not the gate check's blink before it)
		if name not in f.lines or not (f.k >= 0).any():
			continue
		rises, _ = f.lines[name]
		n += len(rises)
		board = (rises - a) / b
		k, d = Nearest(planned, board)
		ok = np.abs(d) < tol_us
		dev += list(d[ok])
		for i in np.flatnonzero(~ok):
			odd.append("{} {}: a rise at board t={:.6f} s (OneBox {:.6f} s) that the Pico didn't plan".format(f.name, name,
				board[i] / 1e6, rises[i] / f.rate))
		inside = Inside(f, planned, a, b)
		got = set(int(x) for x in k[ok])
		for j in inside:
			if int(j) not in got:
				odd.append("{} {}: planned rise {} at board t={:.6f} s not on the OneBox".format(f.name, name, int(j),
					planned[j] / 1e6))
	if n or odd:
		said.append("{}: {} rises on the OneBox, {} on the plan (within {:g} ms){}{}".format(name, n, len(dev),
			tol_us / 1000, ", {:.0f} us at most from it".format(np.abs(dev).max()) if dev else "",
			", {} odd".format(len(odd)) if odd else ""))
	return said, odd


def Main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("folder", help="the session folder (D:\\data\\<date_time>), or SpikeGLX's run folder")
	parser.add_argument("--sync", help="the session's sync file (default: the one sync_*.json in the folder)")
	parser.add_argument("--csv", help="write the rows here (default: sync_match.csv in the folder)")
	parser.add_argument("--sync-bit", type=int, default=1, help="XD0's bit for the sync line (default 1: XA1's copy)")
	parser.add_argument("--sync-xa", help="read the sync line from this analog channel instead (XA1), above --threshold-v")
	parser.add_argument("--threshold-v", type=float, default=1.5)
	parser.add_argument("--gate-bit", type=int, default=0)
	parser.add_argument("--camera-bit", type=int, default=2)
	parser.add_argument("--blue-bit", type=int, default=3)
	parser.add_argument("--yellow-bit", type=int, default=4)
	parser.add_argument("--no-lines", action="store_true", help="only the sync line, not the camera, gate and LEDs")
	args = parser.parse_args(argv)
	paths = RunFiles(args.folder)
	if not paths:
		raise SystemExit("No OneBox files (*.obx0.obx.bin) in {} or its spikeglx\\ folder.".format(args.folder))
	sync_path = args.sync or SyncFile(args.folder)
	with open(sync_path) as f:
		sync = json.load(f)
	if args.no_lines:
		args.camera_bit = args.gate_bit = args.blue_bit = args.yellow_bit = None
	files = [File(p, args) for p in paths]
	# The board makes no sync pulse after the session's end; without one saved, plan as far as the OneBox recorded
	until = sync.get("ends_us") or max((f.first + f.n) / f.rate * 1e6 for f in files) * 1.01
	planned = PlannedRises(sync["seed"], sync["min_gap_ms"], sync["max_gap_ms"], until)
	a, b, matched = Match(files, planned, sync, sync["width_us"])
	rows, missing, extra = Rows(files, planned, a, b, sync["width_us"])
	rate = files[0].rate
	res = np.array([r["residual_us"] for r in rows if "residual_us" in r])
	summary = dict(sync_file=os.path.abspath(sync_path), seed=sync["seed"], files=len(files),
		matched=len(matched), missing=len(missing), extra=len(extra),
		onebox_s_at_board_0=a / rate, onebox_samples_per_board_s=b * 1e6, onebox_rate_meta=rate,
		drift_ppm=(b * 1e6 / rate - 1) * 1e6, residual_us_max=float(np.abs(res).max()), residual_us_rms=float(np.sqrt(
		(res ** 2).mean())), per_file=[dict(file=f.name, sync_rises=len(f.lines["sync"][0]), planned_inside=f.inside,
		anchored=bool(f.anchors)) for f in files])
	print("{} OneBox files, {} sync pulses matched to the Pico's plan (seed {}), {} missing, {} extra.".format(len(files),
		len(matched), sync["seed"], len(missing), len(extra)))
	print("Board time 0 is OneBox time {:.6f} s (sample {:.1f} of the run).".format(a / rate, a))
	print("The OneBox counts {:.4f} samples per second of the Pico's crystal: {:+.3f} ppm against its obSampRate of {:g} "
		"({:+.1f} ms a day).".format(b * 1e6, summary["drift_ppm"], rate, summary["drift_ppm"] * 86400 / 1000))
	print("Residuals from that line: {:.1f} us at most, {:.1f} us rms (a OneBox sample is {:.1f} us).".format(
		summary["residual_us_max"], summary["residual_us_rms"], 1e6 / rate))
	for row in (missing + extra)[:20]:
		print("  {}: {} at OneBox {:.6f} s{}".format(row["file"], row["note"], row["onebox_s"],
			", board {:.6f} s (pulse {})".format(row["board_s"], row["pulse"]) if "pulse" in row else ""))
	if len(missing) + len(extra) > 20:
		print("  ... {} more in the csv".format(len(missing) + len(extra) - 20))
	odd_all = []
	if "settings" in sync and not args.no_lines:
		plan = PlannedLines(sync["settings"])
		period_us = 1e6 / float(sync["settings"].get("CAMERA_FPS") or 60)
		for name, tol in (("camera", period_us / 4), ("gate", 50000), ("blue", 50000), ("yellow", 50000)):
			said, odd = CheckLine(name, files, plan[name], a, b, tol)
			for line in said:
				print(line)
			for line in odd[:10]:
				print("  " + line)
			if len(odd) > 10:
				print("  ... {} more in sync_match.json".format(len(odd) - 10))
			odd_all += odd
			summary[name + "_odd"] = odd
	out = args.csv or os.path.join(args.folder, "sync_match.csv")
	with open(out, "w", newline="") as f:
		w = csv.DictWriter(f, fieldnames=["file", "sample", "onebox_s", "width_ms", "pulse", "board_s", "residual_us", "note"])
		w.writeheader()
		w.writerows(rows)
	with open(os.path.join(os.path.dirname(os.path.abspath(out)), "sync_match.json"), "w") as f:
		json.dump(summary, f, indent=1)
	print("Written to {} and sync_match.json".format(out))
	return summary, rows


if __name__ == "__main__":
	Main()
