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
  first flash, every window 1 ms clear of the LEDs' edges. Green is XA5 in the
  blue flash, red XA6 in the yellow flash; the other two (XA5 in yellow, XA6 in
  blue) are the bleed between the channels. Averaging over a whole cycle
  cancels the mains hum the receivers carry. A flash shorter than a cycle and
  two guards (18.7 ms) is read over what it has, with a note that the hum
  wasn't averaged out; one of about 2 ms or less leaves nothing to read: its
  values are blank, with a note;
- how long each receiver took to reach half its step after its LED's edge;
- clipping (a sample at the OneBox's range) and the dark level's noise;
- with --campy, each camera's frames in the bout, against the OneBox's camera
  pulses, and the flashes' frames in campy's video, counted from 0 (the
  recording's first frame). They come from each camera's aligned.csv
  (campy-align, run after every panel recording), which gives every frame
  the Pico pulse behind it: a frame the Pico didn't send (a stray edge on the
  trigger line) or a missing frame shifts nothing else, and frames outside
  the bouts are listed. Without aligned.csv, from frametimes.npy split where
  frames stop for more than 1 s, which such a frame does shift.

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


def Window(stop, cycle, first, rate, what):
	"""
	The readout's window ending at stop: one mains cycle, cut at first (the
	earliest sample it may use: the LED's on edge plus the guard, or the file's
	start). Returns (start, stop, note): note says why a cut window can't be
	trusted as a whole cycle's mean, or that it is empty.
	"""
	start = max(stop - cycle, first, 0)
	if stop <= start:
		return start, stop, "{}: nothing to read 1 ms clear of the edges;".format(what)
	if stop - start < cycle:
		return start, stop, "{}: read over {:.1f} ms, less than one 60 Hz cycle (hum not averaged out);".format(
			what, (stop - start) / rate * 1000)
	return start, stop, ""


def Note(row, text):
	if text and text not in row.get("note", ""):
		row["note"] = (row.get("note", "") + " " + text).strip()


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
	# The first camera pulse on the run's clock, and the pulses' rate on the OneBox's clock: --campy finds each
	# bout's place among the Pico's triggers from them
	row["camera_on_s"] = round((t0 + cam_on[0]) / rate, 6)
	if len(cam_on) > 1:
		row["camera_hz"] = round((len(cam_on) - 1) / ((cam_on[-1] - cam_on[0]) / rate), 6)
	green =data[:, ch[args.green]].astype(np.float64) * volts
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
	# The windows: the last cycle of each flash and the cycle before the first,
	# each 1 ms clear of the LEDs' edges. A short flash's window is cut at its
	# on edge (plus the guard) rather than reaching back into the dark before
	# it (or the other flash): read, with a note, if anything is left; blank if not.
	d0, d1, dark_note = Window(flashes[first][0] - guard, cycle, 0, rate, "dark before the first flash")
	Note(row, dark_note)
	windows = {}
	for name, (on, off) in flashes.items():
		a, b, note = Window(off - guard, cycle, on + guard, rate, name + " flash")
		windows[name] = (a, b)
		Note(row, note)
	for label, x in (("green", green), ("red", red)):
		dark = Mean(x, d0, d1)
		row["dark_{}_V".format(label)] = round(dark, 5) if d1 > d0 else ""
		row["noise_{}_mV".format(label)] = round(float(x[d0:d1].std()) * 1000, 2) if d1 > d0 else ""
		for name, (on, off) in flashes.items():
			a, b = windows[name]
			if b > a and d1 > d0:
				level = Mean(x, a, b)
				row["{}_in_{}_V".format(label, name)] = round(level - dark, 5)
				row["{}_in_{}_half_rise_ms".format(label, name)] = round(HalfRise(x, on, dark, level, rate) * 1000, 3)
			else:
				row["{}_in_{}_V".format(label, name)] = ""
				row["{}_in_{}_half_rise_ms".format(label, name)] = ""
			clipped = int(np.count_nonzero(np.abs(x[on:off]) >= full))
			if clipped:
				Note(row, "{} clipped in {} flash ({} samples);".format(label, name, clipped))
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


def CampyAligned(folder):
	"""{camera: trigger of each saved frame, in the video's order}, from campy-align's aligned.csv (trigger k is the
	Pico's k-th pulse on a grid that keeps counting through its pauses). Cameras without one are left out."""
	out = {}
	for path in sorted(glob.glob(os.path.join(folder, "*", "aligned.csv"))):
		with open(path, newline="") as f:
			rows = list(csv.DictReader(f))
		out[os.path.basename(os.path.dirname(path))] = np.array([int(round(float(r["trigger"]))) for r in rows], np.int64)
	return out


SNAP_TRIGGERS = 5 # a bout's first frame within this many triggers of where the OneBox puts it is its first pulse's


def PlaceBouts(bouts, triggers):
	"""
	Each bout's first trigger, from one camera's triggers. The first bout starts
	at trigger 1, the Pico's first pulse (campy-align's trigger 1 is the first
	pulse any camera has a frame for). Each next one starts the time between the two
	bouts' first pulses later (on the OneBox's clock, in the pulses' own rate
	there), snapped to the first frame within SNAP_TRIGGERS of that, which
	absorbs the two clocks' small difference. A frame the Pico didn't send
	(a stray edge on the trigger line between bouts) is then in no bout.
	"""
	starts = []
	for i, r in enumerate(bouts):
		if not len(triggers):
			return []
		if i == 0:
			start = 1
		else:
			p = bouts[i - 1]
			hz = r.get("camera_hz") or p.get("camera_hz") or 60.0
			start = starts[-1] + int(round((r["camera_on_s"] - p["camera_on_s"]) * hz))
			near = triggers[(triggers >= start - SNAP_TRIGGERS) & (triggers <= start + SNAP_TRIGGERS)]
			if len(near):
				start = int(near.min())
		starts.append(start)
	return starts


def AddCampyFrames(bouts, folder):
	"""
	Each camera's frames in each bout, and the flashes' frames in campy's video
	(counted from 0, the recording's first frame), from aligned.csv: each frame
	is put with the Pico pulse that made it, so a missing or extra frame moves
	nothing else. Without aligned.csv (campy-align didn't run), from
	frametimes.npy, split where frames stop for more than 1 s, as before.
	Returns lines to print.
	"""
	said = []
	aligned = CampyAligned(folder)
	if not aligned:
		said.append("No aligned.csv in {}: campy's frames counted from frametimes.npy, which an extra or missing frame "
			"shifts (run campy-align on the recording for the exact frames).".format(folder))
		cams = CampyBouts(folder)
		total = 0
		for i, r in enumerate(bouts):
			for cam, runs in cams.items():
				r[cam + "_frames"] = runs[i][1] if i < len(runs) else ""
			for name in ("blue", "yellow"):
				if r.get(name + "_frames"):
					a, b = (int(v) for v in r[name + "_frames"].split("-"))
					r[name + "_campy_frames"] = "{}-{}".format(total + a, total + b)
			total += r["camera_pulses"]
		for cam, runs in cams.items():
			if len(runs) != len(bouts):
				said.append("{}: {} bouts of frames, the OneBox {} bouts".format(cam, len(runs), len(bouts)))
		return said
	flash = {cam: {} for cam in aligned}
	for cam, triggers in aligned.items():
		starts = PlaceBouts(bouts, triggers)
		mine = np.zeros(len(triggers), bool)
		for r, start in zip(bouts, starts):
			inside = (triggers >= start) & (triggers < start + r["camera_pulses"])
			mine |= inside
			r[cam + "_frames"] = int(np.count_nonzero(inside))
			for name in ("blue", "yellow"):
				if r.get(name + "_frames"):
					a, b = (int(v) for v in r[name + "_frames"].split("-"))
					at = [np.flatnonzero(triggers == start + k) for k in (a, b)]
					flash[cam][(r["bout"], name)] = "{}-{}".format(at[0][0], at[1][0]) if all(len(x) for x in at) else \
						"missing"
		extra = np.flatnonzero(~mine)
		if len(extra):
			said.append("{}: {} frame{} outside the Pico's bouts (video frame{} {}): not one of its pulses".format(cam,
				len(extra), "s" if len(extra) > 1 else "", "s" if len(extra) > 1 else "",
				", ".join(str(int(x)) for x in extra[:10]) + (", ..." if len(extra) > 10 else "")))
	for r in bouts:
		for name in ("blue", "yellow"):
			seen = {cam: flash[cam].get((r["bout"], name)) for cam in aligned}
			seen = {cam: v for cam, v in seen.items() if v}
			if not seen:
				continue
			if len(set(seen.values())) == 1:
				r[name + "_campy_frames"] = next(iter(seen.values()))
			else:
				r[name + "_campy_frames"] = "; ".join("{} {}".format(cam, v) for cam, v in seen.items())
	return said


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
	parser.add_argument("folder", help="the session folder (D:\\data\\<date_time>), or SpikeGLX's run folder")
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
		for line in AddCampyFrames(bouts, args.campy):
			print(line)
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
		if "green_V" not in r:
			print("bout {}: {}".format(r["bout"], r.get("note", "")))
			continue
		volts = {k: "{:+.4f}".format(r[k]) if isinstance(r[k], float) else "blank"
			for k in ("green_V", "red_V", "dark_green_V", "dark_red_V")}
		print("bout {bout}: {camera_pulses} pulses, {order}, gap {gap_ms} ms; green {green_V} V, red {red_V} V "
			"(dark {dark_green_V} / {dark_red_V} V){note}".format(**dict(r, **volts, note="; " + r["note"] if r.get("note")
			else "")))
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
