"""
The fiber photometry snapshots of a ChronicRecord session, read from SpikeGLX's
OneBox files: one row per bout, with each flash's place among the camera frames.

  python photometry_snapshots.py <session folder or spikeglx run folder> [--campy <session folder>] [--csv out.csv]

What it reads (chronicrecord/README.md, "Fiber photometry"): each OneBox file
(<run>_g0_t<N>.obx0.obx.bin, one per gate opening) holds the gate, camera and
LED TTLs as digital bits (XD0's bits 0, 2, 3 and 4 here) and the two
photoreceivers as analog inputs (XA5 green, XA6 red). Everything is on the
OneBox's own clock, a sample every 33 us.

For each bout it gives:
- where each LED's flash was: its OneBox time, its width, the camera pulses
  (frames, counted from the bout's first) whose exposure it lit, and the dark
  between the two flashes;
- the readout: each receiver's mean over the last whole 60 Hz cycle (16.7 ms)
  of each flash, less its dark level, the mean over the 16.7 ms before the
  first flash. Green is XA5 in the blue flash, red XA6 in the yellow flash; the
  other two (XA5 in yellow, XA6 in blue) are the bleed between the channels.
  Averaging over a whole cycle cancels the mains hum the receivers carry;
- how long each receiver took to reach half its step after its LED's edge;
- clipping (a sample at the OneBox's range) and the dark level's noise;
- with --campy, each camera's frames in the bout (from frametimes.npy, split
  where frames stop for more than 1 s), against the OneBox's camera pulses,
  and the flashes' frames in campy's numbering, counted from the recording's
  first frame.

Needs numpy only. Run it on a copy of the session, not on the rig while it
records (any python there counts against the rig lock).
"""

import argparse, csv, glob, os, re, sys

import numpy as np

CYCLE_S = 1 / 60    # one mains cycle: the receivers' hum averages out over it
EDGE_GUARD_S = 0.001  # stay this far from an LED's edge in the windows


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


def Mean(x, start, stop):
	start, stop = max(0, start), min(len(x), stop)
	return float(x[start:stop].mean()) if stop > start else float("nan")


def HalfRise(x, on, dark, top, rate):
	"""Seconds from the LED's edge until x is half way from dark to top (nan if it never is)."""
	if not np.isfinite(top) or abs(top - dark) < 1e-3:
		return float("nan")
	half = (dark + top) / 2
	seg = x[on:on + int(0.01 * rate)]
	hit = np.flatnonzero(seg >= half) if top > dark else np.flatnonzero(seg <= half)
	return float(hit[0] / rate) if len(hit) else float("nan")


def Bout(bin_path, args):
	meta, data = ReadObx(bin_path)
	rate = float(meta["obSampRate"])
	ch = Channels(meta)
	volts = float(meta["obAiRangeMax"]) / float(meta["obMaxInt"])
	t0 = int(meta["firstSample"]) # the run's sample count at the file's first sample
	xd = data[:, ch["XD0"]].astype(np.int32)
	bit = lambda b: (xd >> b) & 1
	cam_on, _ = Edges(bit(args.camera_bit))
	row = {"file": os.path.basename(bin_path), "length_s": round(len(data) / rate, 3), "camera_pulses": len(cam_on)}
	gate_on, gate_off = Edges(bit(args.gate_bit))
	if len(gate_on):
		row["gate_on_s"] = round((t0 + gate_on[0]) / rate, 6)
	if not len(cam_on):
		row["note"] = "no camera pulses (the gate check's blink?)"
		return row
	green = data[:, ch[args.green]].astype(np.float64) * volts
	red = data[:, ch[args.red]].astype(np.float64) * volts
	full = float(meta["obAiRangeMax"]) * 0.999
	cycle = int(round(CYCLE_S * rate))
	guard = int(round(EDGE_GUARD_S * rate))
	flashes = {}
	for name, b in (("blue", args.blue_bit), ("yellow", args.yellow_bit)):
		on, off = Edges(bit(b))
		if len(on) != 1 or len(off) != 1 or off[0] <= on[0]:
			row["note"] = "{}: {} rises and {} falls, not one flash".format(name, len(on), len(off))
			return row
		on, off = int(on[0]), int(off[0])
		lit = np.flatnonzero((cam_on >= on) & (cam_on < off))
		flashes[name] = (on, off)
		row[name + "_on_s"] = round((t0 + on) / rate, 6)
		row[name + "_ms"] = round((off - on) / rate * 1000, 3)
		row[name + "_frames"] = "{}-{}".format(lit[0], lit[-1]) if len(lit) else ""
		# The LED switches LED_LEAD_US (1 ms) before its first frame's pulse
		row[name + "_lead_us"] = round((cam_on[lit[0]] - on) / rate * 1e6) if len(lit) else ""
	first, second = sorted(flashes, key=lambda k: flashes[k][0])
	row["order"] = first + " first"
	row["gap_ms"] = round((flashes[second][0] - flashes[first][1]) / rate * 1000, 3)
	start = flashes[first][0]
	for label, x in (("green", green), ("red", red)):
		dark = Mean(x, start - guard - cycle, start - guard)
		row["dark_{}_V".format(label)] = round(dark, 5)
		row["noise_{}_mV".format(label)] = round(float(x[max(0, start - guard - cycle):start - guard].std()) * 1000, 2)
		for name, (on, off) in flashes.items():
			level = Mean(x, off - guard - cycle, off - guard)
			row["{}_in_{}_V".format(label, name)] = round(level - dark, 5)
			row["{}_in_{}_half_rise_ms".format(label, name)] = round(HalfRise(x, on, dark, level, rate) * 1000, 3)
			clipped = int(np.count_nonzero(np.abs(x[on:off]) >= full))
			if clipped:
				row["note"] = (row.get("note", "") + " {} clipped in {} flash ({} samples);".format(label, name, clipped)).strip()
	# The readout: green from the blue flash, red from the yellow
	row["green_V"] = row["green_in_blue_V"]
	row["red_V"] = row["red_in_yellow_V"]
	return row


def CampyBouts(folder, gap_s=1.0):
	"""{camera: [(first frame number, frames), ...]}, a bout where frames stop for more than gap_s."""
	out = {}
	for path in sorted(glob.glob(os.path.join(folder, "*", "frametimes.npy"))):
		ft = np.load(path)
		frame, t = ft[0].astype(np.int64), ft[1].astype(np.float64)
		if not len(frame):
			out[os.path.basename(os.path.dirname(path))] = []
			continue
		cuts = np.flatnonzero(np.diff(t) > gap_s) + 1
		starts, stops = np.r_[0, cuts], np.r_[cuts, len(frame)]
		out[os.path.basename(os.path.dirname(path))] = [(int(frame[a]), int(b - a)) for a, b in zip(starts, stops)]
	return out


def RunFolder(path):
	"""The SpikeGLX run folder (holding the .obx.bin files): the path, or within a session's spikeglx\\."""
	for pattern in ("*.obx0.obx.bin", os.path.join("*", "*.obx0.obx.bin"), os.path.join("spikeglx", "*", "*.obx0.obx.bin"),
			os.path.join("spikeglx", "*", "*", "*.obx0.obx.bin")):
		found = glob.glob(os.path.join(path, pattern))
		if found:
			return found
	return []


def TIndex(path):
	m = re.search(r"_t(\d+)\.obx0\.obx\.bin$", path)
	return int(m.group(1)) if m else -1


def Main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("folder", help="the session folder (C:\\data\\<date_time>), or SpikeGLX's run folder")
	parser.add_argument("--campy", help="campy's recording folder (Camera0\\frametimes.npy ...): the session folder")
	parser.add_argument("--csv", help="write the rows here (default: photometry_snapshots.csv in the folder)")
	parser.add_argument("--green", default="XA5")
	parser.add_argument("--red", default="XA6")
	parser.add_argument("--gate-bit", type=int, default=0)
	parser.add_argument("--camera-bit", type=int, default=2)
	parser.add_argument("--blue-bit", type=int, default=3)
	parser.add_argument("--yellow-bit", type=int, default=4)
	args = parser.parse_args(argv)
	files = sorted(RunFolder(args.folder), key=TIndex)
	if not files:
		raise SystemExit("No OneBox files (*.obx0.obx.bin) in {} or its spikeglx\\ folder.".format(args.folder))
	rows = [Bout(f, args) for f in files]
	bouts = [r for r in rows if r["camera_pulses"]]
	for i, r in enumerate(bouts):
		r["bout"] = i + 1
	if args.campy:
		cams = CampyBouts(args.campy)
		total = 0
		for i, r in enumerate(bouts):
			for cam, runs in cams.items():
				r[cam + "_frames"] = runs[i][1] if i < len(runs) else ""
			# The flashes' frames in campy's numbering: the bouts before, plus the frame within this one
			for name in ("blue", "yellow"):
				if r.get(name + "_frames"):
					a, b = (int(v) for v in r[name + "_frames"].split("-"))
					r[name + "_campy_frames"] = "{}-{}".format(total + a, total + b)
			total += r["camera_pulses"]
		for cam, runs in cams.items():
			if len(runs) != len(bouts):
				print("{}: {} bouts of frames, the OneBox {} bouts".format(cam, len(runs), len(bouts)))
	keys = []
	for r in rows:
		keys += [k for k in r if k not in keys]
	front = ["bout", "file", "gate_on_s", "camera_pulses", "order", "gap_ms", "green_V", "red_V", "note"]
	keys = [k for k in front if k in keys] + [k for k in keys if k not in front]
	out = args.csv or os.path.join(args.folder, "photometry_snapshots.csv")
	with open(out, "w", newline="") as f:
		w = csv.DictWriter(f, fieldnames=keys)
		w.writeheader()
		w.writerows(rows)
	for r in rows:
		if not r["camera_pulses"]:
			print("{}: {}".format(r["file"], r.get("note", "")))
			continue
		print("bout {bout}: {camera_pulses} pulses, {order}, gap {gap_ms} ms; green {green_V:+.4f} V, red {red_V:+.4f} V "
			"(dark {dark_green_V:+.4f} / {dark_red_V:+.4f} V){note}".format(**dict(r, note="; " + r["note"] if r.get("note")
			else "")) if "green_V" in r else "bout {}: {}".format(r["bout"], r.get("note", "")))
	# Across the bouts: what should be the same in every one
	read = [r for r in bouts if "green_V" in r]
	for key in ("order", "blue_frames", "yellow_frames"):
		seen = sorted({str(r.get(key)) for r in read})
		if seen:
			print("{}: {}".format(key, ", ".join(seen)))
	for key in ("blue_ms", "yellow_ms", "gap_ms", "blue_lead_us", "yellow_lead_us", "green_in_blue_half_rise_ms",
			"red_in_yellow_half_rise_ms", "noise_green_mV", "noise_red_mV"):
		v = [r[key] for r in read if isinstance(r.get(key), (int, float)) and np.isfinite(r[key])]
		if v:
			print("{}: {:g} to {:g}".format(key, min(v), max(v)))
	print("{} bouts; written to {}".format(len(bouts), out))
	return rows


if __name__ == "__main__":
	Main()
