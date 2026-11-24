"""
campy-ephyssync: checks that a recording's cameras and SpikeGLX's run line
up, from the camera trigger pulses SpikeGLX recorded.

  campy-ephyssync <recording> [--bit 2] [--nidq <file.nidq.bin>] [--ap <file.ap.bin>]

For a rig whose trigger board's camera line also goes into one of
SpikeGLX's digital inputs (an NI-DAQ's XD line, a OneBox's XD0), where
SpikeGLX may run on another computer (campy-session's spikeglx: section,
trigger immediate). It reads:

- SpikeGLX's NI stream (<recording>/spikeglx/**/*.nidq.bin and its .meta),
  the digital word's bit that carries the camera pulses (--bit, else
  spikeglx.json's cameraBit);
- campy-align's alignment.json and each camera's aligned.csv (the trigger
  behind each frame; campy-align is run if they are missing) and
  frametimes.npy (each frame's time on the camera's own clock);
- campy-session's spikeglx.json and trigger.json (when SpikeGLX's recording
  was enabled and stopped, and when the trigger started, all on the camera
  computer's clock).

It reports:

- pulses: how many SpikeGLX recorded, against the triggers campy's cameras
  name, and every camera's frames, frames missed and frames that would fall
  on a pulse SpikeGLX doesn't have;
- the trigger board's clock on SpikeGLX's: pulse time = a + b x trigger
  number, the period, how far the board runs from its nominal rate (ppm),
  and the residuals (each within one sample when the line is clean);
- each camera's clock on SpikeGLX's: its timestamps against its triggers'
  pulse times, the rate difference in ppm and the residuals (the camera's
  exposure-start jitter plus the sample's quantization);
- the start and stop margins: how long SpikeGLX recorded before the first
  pulse and after the last (both must be above 0, or pulses were lost), in
  SpikeGLX's own samples; and the same from campy-session's records, on the
  camera computer's clock;
- with SpikeGLX's sync on (its imec base station's 1 Hz square wave recorded
  in the NI stream as well as in the probe's SY channel, --ap): the NI clock
  on the probe's, so each frame's time on the probe's clock.

Writes <recording>/ephyssync.json, and ephys_times.csv in each camera's
folder: frame, trigger, ni_s (its pulse's time in the NI file, from its
first sample) and probe_s when the probe's clock is known.
"""

import argparse, csv, glob, json, os, sys

import numpy as np

CHUNK = 8_000_000 # samples per read of a .bin file


# ------------------------------------------------------------------ SpikeGLX's files

def ReadMeta(path):
	meta = {}
	with open(path, encoding="utf-8", errors="replace") as f:
		for line in f:
			k, sep, v = line.strip().partition("=")
			if sep:
				meta[k.lstrip("~")] = v
	return meta


def DigitalWord(meta):
	"""The saved channel that holds the first digital word: NI (snsMnMaXaDw), OneBox (snsXaDwSy) or imec SY (last)."""
	n = int(meta["nSavedChans"])
	if meta.get("snsSaveChanSubset", "all") not in ("all", "") and meta.get("typeThis") == "nidq":
		raise ValueError("this NI file saved a subset of channels ({}): give the digital word's channel".format(
			meta.get("snsSaveChanSubset")))
	if "snsMnMaXaDw" in meta:
		mn, ma, xa, dw = (int(v) for v in meta["snsMnMaXaDw"].split(","))
		if dw < 1:
			raise ValueError("this NI file has no digital word (snsMnMaXaDw={})".format(meta["snsMnMaXaDw"]))
		return mn + ma + xa, n
	if "snsXaDwSy" in meta:
		xa, dw, sy = (int(v) for v in meta["snsXaDwSy"].split(","))
		return xa, n
	return n - 1, n # imec: the SY word is last


def Rate(meta):
	for key in ("niSampRate", "obSampRate", "imSampRate"):
		if key in meta:
			return float(meta[key])
	raise ValueError("no sample rate in the .meta file")


def Edges(path, channel, n, bit, chunk=CHUNK):
	"""(rises, falls): sample indices (from the file's first sample) where the channel's bit goes 1, and 0."""
	data = np.memmap(path, dtype=np.int16, mode="r")
	total = data.size // n
	data = data[:total * n].reshape(total, n)
	rises, falls, prev = [], [], None
	for start in range(0, total, chunk):
		v = ((data[start:start + chunk, channel].astype(np.int32) >> bit) & 1).astype(np.int8)
		if prev is None:
			prev = v[0]
		d = np.diff(np.concatenate([[prev], v]))
		rises.append(np.flatnonzero(d == 1) + start)
		falls.append(np.flatnonzero(d == -1) + start)
		prev = v[-1]
	cat = lambda parts: np.concatenate(parts) if parts else np.zeros(0, np.int64)
	return cat(rises).astype(np.int64), cat(falls).astype(np.int64), total


def Fit(x, y):
	"""Least-squares y = a + b x: (a, b, residuals)."""
	x, y = np.asarray(x, float), np.asarray(y, float)
	xm, ym = x.mean(), y.mean()
	b = np.sum((x - xm) * (y - ym)) / np.sum((x - xm) ** 2)
	a = ym - b * xm
	return a, b, y - (a + b * x)


def Residuals(r, unit=1e6):
	r = np.asarray(r, float) * unit
	return {"rms_us": round(float(np.sqrt(np.mean(r ** 2))), 3), "p99_us": round(float(np.percentile(np.abs(r), 99)), 3),
		"max_us": round(float(np.max(np.abs(r))), 3)}


# ------------------------------------------------------------------ the check

def LoadJson(path):
	try:
		with open(path, encoding="utf-8") as f:
			return json.load(f)
	except (OSError, ValueError):
		return None


def Cameras(folder, log):
	alignment = LoadJson(os.path.join(folder, "alignment.json"))
	if alignment is None:
		from campy import align
		log("No alignment.json: running campy-align first.")
		align.Run(folder, log=log)
		alignment = LoadJson(os.path.join(folder, "alignment.json"))
	if alignment is None:
		raise SystemExit("campy-align wrote no alignment.json in {}".format(folder))
	cams = {}
	for name, summary in (alignment.get("cameras") or {}).items():
		cam_folder = os.path.join(folder, name)
		path = os.path.join(cam_folder, "aligned.csv")
		if not summary.get("aligned") or not os.path.exists(path):
			continue
		with open(path, newline="") as f:
			rows = list(csv.DictReader(f))
		frames = np.array([int(r["frame"]) for r in rows], np.int64)
		trig = np.array([float(r["trigger"]) for r in rows])
		t = None
		ft = os.path.join(cam_folder, "frametimes.npy")
		if os.path.exists(ft):
			arr = np.load(ft)
			own = dict(zip(arr[0].astype(np.int64).tolist(), arr[1].tolist()))
			t = np.array([own.get(int(k), np.nan) for k in frames])
		cams[name] = {"folder": cam_folder, "frame": frames, "trigger": trig, "t": t}
	return alignment, cams


def Check(folder, bit=None, nidq=None, ap=None, log=print):
	record = LoadJson(os.path.join(folder, "spikeglx.json")) or {}
	trigger_json = LoadJson(os.path.join(folder, "trigger.json")) or {}
	if bit is None:
		bit = record.get("cameraBit", -1)
	if bit is None or bit < 0:
		raise SystemExit("Which bit of SpikeGLX's digital word carries the camera pulses? Give --bit (or spikeglx.camera_bit "
			"in the config).")
	if nidq is None:
		found = sorted(glob.glob(os.path.join(folder, "spikeglx", "**", "*.nidq.bin"), recursive=True))
		if not found:
			raise SystemExit("No *.nidq.bin under {} (give --nidq).".format(os.path.join(folder, "spikeglx")))
		if len(found) > 1:
			log("{} NI files; using {} (give --nidq for another).".format(len(found), found[0]))
		nidq = found[0]
	meta = ReadMeta(nidq[:-4] + ".meta")
	rate = Rate(meta)
	channel, n = DigitalWord(meta)
	rises, falls, total = Edges(nidq, channel, n, bit)
	first_sample = int(float(meta.get("firstSample", 0)))
	out = {"nidq": os.path.relpath(nidq, folder), "sample_rate": rate, "bit": bit, "file_samples": int(total),
		"file_s": round(total / rate, 6), "first_sample": first_sample, "pulses": int(len(rises))}
	log("SpikeGLX's NI file: {} ({:.1f} s at {:g} Hz), digital word channel {}, bit {}: {:,} pulses.".format(
		out["nidq"], total / rate, rate, channel, bit, len(rises)))

	alignment, cams = Cameras(folder, log)
	period = float(alignment.get("trigger_period_s") or 0)
	last = max((int(np.nanmax(c["trigger"])) for c in cams.values() if len(c["trigger"])), default=0)
	out["triggers_named_by_cameras"] = last
	out["nominal_period_s"] = period
	if not len(rises):
		out["ok"] = False
		out["problems"] = ["no camera pulse on bit {} of SpikeGLX's digital word".format(bit)]
		return out
	problems = []

	# The board's clock: pulse k (1-based) = trigger k. A pulse train with gaps (the trigger paused, or SpikeGLX missed
	# pulses) is read by rounding each step to whole periods.
	t_ni = rises / rate
	steps = np.diff(t_ni)
	est = np.median(steps) if len(steps) else period
	k = np.concatenate([[1], 1 + np.cumsum(np.maximum(1, np.rint(steps / est)).astype(np.int64))])
	gaps = int(np.sum(np.rint(steps / est) > 1))
	a, b, res = Fit(k, t_ni)
	out["board"] = {"period_s": round(b, 9), "rate_hz": round(1 / b, 6),
		"ppm_from_nominal": round((b / period - 1) * 1e6, 2) if period else None,
		"first_pulse_s": round(float(t_ni[0]), 6), "last_pulse_s": round(float(t_ni[-1]), 6),
		"steps_over_one_period": gaps, "residuals": Residuals(res)}
	if falls.size:
		widths = []
		j = np.searchsorted(falls, rises)
		ok = j < len(falls)
		widths = (falls[j[ok]] - rises[ok]) / rate
		out["board"]["pulse_ms"] = round(float(np.median(widths)) * 1e3, 3) if len(widths) else None
	pulse_count = int(k[-1])
	out["pulse_numbers"] = pulse_count
	if gaps:
		problems.append("{} steps between pulses longer than one period: the trigger paused, or SpikeGLX missed pulses"
			.format(gaps))
	if last > pulse_count:
		problems.append("the cameras name trigger {:,} but SpikeGLX has {:,} pulses: it missed the first or last ones "
			"(started too late, or stopped too early)".format(last, pulse_count))
	log("Trigger board on SpikeGLX's clock: period {:.6f} ms ({:+.1f} ppm from {:g} fps), residuals rms {:.1f} us, "
		"max {:.1f} us.".format(b * 1e3, out["board"]["ppm_from_nominal"] or 0, 1 / period if period else 0,
		out["board"]["residuals"]["rms_us"], out["board"]["residuals"]["max_us"]))
	log("Pulses: {:,} on SpikeGLX; the cameras' last trigger is {:,}.".format(pulse_count, last))

	# Every camera
	out["cameras"] = {}
	time_of = np.full(pulse_count + 1, np.nan)
	time_of[k] = t_ni
	probe = ProbeClock(folder, meta, nidq, ap, log) if ap is not False else None
	if probe:
		out["probe_clock"] = probe["summary"]
	for name, c in cams.items():
		trig = c["trigger"]
		whole = np.rint(trig).astype(np.int64)
		inside = (whole >= 1) & (whole <= pulse_count)
		ni = np.full(len(trig), np.nan)
		ni[inside] = time_of[whole[inside]]
		summary = {"frames": int(len(trig)), "first_trigger": int(whole.min()) if len(whole) else None,
			"last_trigger": int(whole.max()) if len(whole) else None,
			"frames_without_pulse": int(np.sum(~np.isfinite(ni))),
			"triggers_without_frame": int(pulse_count - len(np.unique(whole[inside])))}
		if summary["frames_without_pulse"]:
			problems.append("{}: {} frames fall on triggers SpikeGLX has no pulse for".format(name,
				summary["frames_without_pulse"]))
		if c["t"] is not None:
			good = np.isfinite(ni) & np.isfinite(c["t"])
			if good.sum() > 2:
				ca, cb, cres = Fit(ni[good], c["t"][good])
				summary["clock"] = {"ppm_vs_spikeglx": round((cb - 1) * 1e6, 2), "residuals": Residuals(cres)}
		rows = {"frame": c["frame"], "trigger": whole, "ni_s": ni}
		if probe:
			rows["probe_s"] = probe["map"](ni)
		WriteCsv(os.path.join(c["folder"], "ephys_times.csv"), rows)
		out["cameras"][name] = summary
		clock = summary.get("clock")
		log("  {}: {:,} frames, triggers {}..{}, {} without a pulse, {:,} pulses without a frame{}.".format(name,
			summary["frames"], summary["first_trigger"], summary["last_trigger"], summary["frames_without_pulse"],
			summary["triggers_without_frame"], "; its clock {:+.1f} ppm from SpikeGLX's, residuals rms {:.1f} us, max "
			"{:.1f} us".format(clock["ppm_vs_spikeglx"], clock["residuals"]["rms_us"], clock["residuals"]["max_us"])
			if clock else ""))

	# Margins: SpikeGLX before the first pulse and after the last, in its own samples
	start_margin = rises[0] / rate
	stop_margin = (total - 1 - rises[-1]) / rate
	out["margins"] = {"spikeglx_before_first_pulse_s": round(float(start_margin), 6),
		"spikeglx_after_last_pulse_s": round(float(stop_margin), 6)}
	if start_margin <= period or stop_margin <= period:
		problems.append("SpikeGLX's file starts {:.3f} s before the first pulse and ends {:.3f} s after the last: a "
			"margin at or below one period may have lost pulses".format(start_margin, stop_margin))
	enabled = (record.get("recordingEnabled") or {}).get("hostNs")
	started = trigger_json.get("startedHostNs")
	stop = (record.get("beforeStop") or {}).get("hostNs")
	if enabled and started:
		# Both on campy's host clock (campy/clock.py), on the camera computer
		out["margins"]["host_enable_to_trigger_start_s"] = round((started - enabled) / 1e9, 3)
	if enabled and stop:
		out["margins"]["host_enable_to_stop_s"] = round((stop - enabled) / 1e9, 3)
		before = (record.get("beforeStop") or {}).get("sample")
		after = (record.get("recordingEnabled") or {}).get("sample")
		if before is not None and after is not None:
			out["margins"]["stream_samples_enable_to_stop"] = int(before - after)
	log("SpikeGLX recorded {:.3f} s before the first pulse and {:.3f} s after the last.".format(start_margin, stop_margin))
	out["problems"] = problems
	out["ok"] = not problems
	for p in problems:
		log("PROBLEM: " + p)
	log("OK: every frame has its pulse on SpikeGLX's clock." if out["ok"] else "{} problem(s).".format(len(problems)))
	return out


def ProbeClock(folder, ni_meta, nidq, ap, log):
	"""
	The NI clock on the probe's, from SpikeGLX's sync wave (its sync source an
	imec base station: syncSourceIdx >= 3), in the NI file (syncNiChan, a
	digital line: syncNiChanType 0) and the probe's SY word (bit 6). None when
	the sync is off or no probe file is found.
	"""
	idx = int(float(ni_meta.get("syncSourceIdx", 0) or 0))
	if idx == 0:
		log("SpikeGLX's sync is off (syncSourceIdx=0): frames are placed on the NI clock only, not the probe's.")
		return None
	if ap is None:
		found = sorted(glob.glob(os.path.join(os.path.dirname(nidq), "**", "*.ap.bin"), recursive=True)) or \
			sorted(glob.glob(os.path.join(folder, "spikeglx", "**", "*.ap.bin"), recursive=True))
		if not found:
			log("No probe file (.ap.bin): frames are placed on the NI clock only.")
			return None
		ap = found[0]
	if int(float(ni_meta.get("syncNiChanType", 0))) != 0:
		log("SpikeGLX's sync is on an analog NI channel: not read here; frames on the NI clock only.")
		return None
	ni_rate = Rate(ni_meta)
	channel, n = DigitalWord(ni_meta)
	ni_bit = int(float(ni_meta.get("syncNiChan", 0)))
	ni_rises = Edges(nidq, channel, n, ni_bit)[0] / ni_rate
	ap_meta = ReadMeta(ap[:-4] + ".meta")
	ap_rate = Rate(ap_meta)
	ap_ch, ap_n = DigitalWord(ap_meta)
	ap_rises = Edges(ap, ap_ch, ap_n, 6)[0] / ap_rate
	m = min(len(ni_rises), len(ap_rises))
	if m < 3:
		log("Too few sync edges ({} NI, {} probe) to put the NI clock on the probe's.".format(len(ni_rises), len(ap_rises)))
		return None
	# The two files start at different moments: match edges by their order after the first edge in both, using the
	# 1 s period to pair them (files from the same run, started within a second of each other)
	offset = int(np.rint(ap_rises[0] - ni_rises[0]))
	ni_e = ni_rises[max(0, -offset):]
	ap_e = ap_rises[max(0, offset):]
	m = min(len(ni_e), len(ap_e))
	a, b, res = Fit(ni_e[:m], ap_e[:m])
	summary = {"ap": os.path.relpath(ap, folder), "edges": int(m), "ppm": round((b - 1) * 1e6, 3),
		"offset_s": round(a, 6), "residuals": Residuals(res)}
	log("NI clock on the probe's: {:+.2f} ppm, residuals rms {:.1f} us over {} sync edges.".format(summary["ppm"],
		summary["residuals"]["rms_us"], m))
	return {"summary": summary, "map": lambda t: a + b * np.asarray(t, float)}


def WriteCsv(path, columns):
	names = list(columns)
	with open(path, "w", newline="") as f:
		w = csv.writer(f)
		w.writerow(names)
		for row in zip(*(columns[n] for n in names)):
			w.writerow(["" if isinstance(v, float) and not np.isfinite(v) else
				("{:.7f}".format(v) if isinstance(v, (float, np.floating)) else int(v)) for v in row])


def Main(argv=None):
	parser = argparse.ArgumentParser(prog="campy-ephyssync", description=__doc__.split("\n\n")[0])
	parser.add_argument("folder", help="the recording's folder")
	parser.add_argument("--bit", type=int, default=None, help="the digital word's bit with the camera pulses (default: "
		"spikeglx.json's cameraBit)")
	parser.add_argument("--nidq", default=None, help="SpikeGLX's NI file (default: the one under <folder>/spikeglx)")
	parser.add_argument("--ap", default=None, help="the probe's .ap.bin, for its clock (default: found beside the NI file)")
	parser.add_argument("--no-probe", action="store_true", help="don't read the probe's file")
	args = parser.parse_args(argv)
	out = Check(args.folder, args.bit, args.nidq, False if args.no_probe else args.ap)
	with open(os.path.join(args.folder, "ephyssync.json"), "w", encoding="utf-8") as f:
		json.dump(out, f, indent=2)
	print("Wrote ephyssync.json and ephys_times.csv in each camera's folder.")
	return 0 if out.get("ok") else 1


if __name__ == "__main__":
	sys.exit(Main())
