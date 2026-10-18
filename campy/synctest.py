"""
campy-synctest: check that triggered cameras expose together, by filming one blinking LED.

	campy-synctest ./test        # a recording's videoFolder, made with blinkPin and blinkHz set

Wire an LED, through a resistor (330 ohm from the pin), to a spare pin of the
trigger controller, where every camera can see it, and set blinkPin and blinkHz.
The controller blinks it at blinkHz from the clock that triggers the cameras.
Record a minute with a short exposure, then run campy-synctest on the folder.

A frame's LED brightness is how long the LED was lit during that frame's
exposure, so from frame to frame it traces the blink as that camera sampled
it. A sine at blinkHz, fitted a couple of seconds at a time, gives the phase at
which each camera saw the blink; compared across cameras on the same triggers,
the phases say how far apart the exposures were, to about 10 us. Frames in
which both cameras caught an edge of the blink say it finer: there the
difference in how much of the exposure was lit, times the exposure, is the
offset. That needs equal exposures and an LED that does not saturate.

The offset is between exposure centres: with equal exposures, between their
starts. Positive means the camera exposes after the reference.

Choosing the rates: blinkHz / frameRate must not be near a whole or half
number, or every frame catches the LED at the same point of its cycle; and the
exposure must stay well under 1 / (4 blinkHz), or each frame averages the
blink away. The tool warns about both. The video is lossy: a lower quality
number (better quality) makes the edge measurement finer. It runs campy-align
to tie each frame to its trigger, and so writes that tool's files too.
"""

import argparse, json, os, sys
import numpy as np

from campy import align

BLOCK_S = 2.0              # seconds per sine fit
MIN_BLOCKS = 3
MIN_FRAMES_PER_BLOCK = 8
TOLERANCE_US = 100.0       # default pass threshold on the exposure offset
LED_MIN_AMPLITUDE = 2.0    # gray levels: below this the LED is not seen
LED_MIN_COHERENCE = 0.9    # phase agreement between consecutive fits
EDGE_LIT = (0.1, 0.9)      # an "edge frame" is lit for this share of its exposure
CLEAR_LIT = (0.2, 0.8)     # clearly off below, clearly on above
MIN_EDGE_PAIRS = 3         # per fit, to use edge frames rather than the phase
SATURATED = 254            # gray level counted as clipped
SATURATED_SHARE = 0.01     # of frames, before the LED counts as saturating the sensor

PASS, FAIL, INCONCLUSIVE = "PASS", "FAIL", "INCONCLUSIVE"


class SyncTestError(Exception):
	pass


# ------------------------------------------------------------------ frames

def ReadFrames(path):
	"""A video's frames as gray arrays, in order."""
	import imageio_ffmpeg
	reader = imageio_ffmpeg.read_frames(path, pix_fmt="gray", bits_per_pixel=8)
	width, height = next(reader)["size"]
	for b in reader:
		yield np.frombuffer(b, np.uint8).reshape(height, width)


def CellSize(width, height):
	# Side of the square cells the LED is searched over: ~40 across the shorter side
	return max(2, int(round(min(width, height) / 40.0)))


def CellSignals(frames):
	"""
	Mean brightness of every cell in every frame, (frames, rows, cols), the
	cell size, and per cell the number of frames in which one of its pixels clipped.
	"""
	out, clipped, cell = [], None, None
	for f in frames:
		cell = cell or CellSize(f.shape[1], f.shape[0])
		gh, gw = f.shape[0] // cell, f.shape[1] // cell
		cells = f[:gh * cell, :gw * cell].reshape(gh, cell, gw, cell)
		out.append(cells.mean(axis=(1, 3), dtype=np.float32))
		hit = cells.max(axis=(1, 3)) >= SATURATED
		clipped = hit.astype(np.int64) if clipped is None else clipped + hit
	if not out:
		raise SyncTestError("no frames in the video")
	return np.stack(out), cell, clipped


def RoiSignal(frames, roi, name):
	"""Mean brightness inside roi (x, y, w, h) per frame, and the frames in which it clipped."""
	x, y, w, h = roi
	means, clipped = [], 0
	for f in frames:
		# numpy would cut a box that does not fit down to what does, or to
		# nothing ("zero-size array to reduction operation maximum")
		if not means and not (x >= 0 and y >= 0 and x + w <= f.shape[1] and y + h <= f.shape[0]):
			raise SyncTestError("{}: --roi {}={},{},{},{} is not inside its {}x{} frames".format(
				name, name, x, y, w, h, f.shape[1], f.shape[0]))
		box = f[y:y + h, x:x + w]
		means.append(box.mean(dtype=np.float64))
		clipped += int(box.max() >= SATURATED)
	return np.array(means), clipped


# ------------------------------------------------------------------ the fit

def FitBlocks(t, y, hz, edges):
	"""
	Least-squares DC + sine at hz in each block of time [edges[i], edges[i+1]).
	y is (frames,) or (frames, signals). Returns the complex amplitude a + ib of
	a*cos(wt) + b*sin(wt) per block (and signal), NaN where a block has too few
	frames: its angle is the phase at which the camera saw the blink.
	"""
	y2 = y.reshape(len(y), -1).astype(np.float64)
	z = np.full((len(edges) - 1, y2.shape[1]), np.nan + 0j)
	for i in range(len(edges) - 1):
		m = (t >= edges[i]) & (t < edges[i + 1])
		if m.sum() < MIN_FRAMES_PER_BLOCK:
			continue
		ph = 2 * np.pi * hz * t[m]
		design = np.column_stack([np.ones(m.sum()), np.cos(ph), np.sin(ph)])
		coef = np.linalg.lstsq(design, y2[m], rcond=None)[0]
		z[i] = coef[1] + 1j * coef[2]
	return z if y.ndim > 1 else z[:, 0]


def Coherence(z):
	# How steady the phase is from one fit to the next: ~1 for a visible LED, ~0 for noise
	z = z[np.isfinite(z)]
	if len(z) < 2:
		return 0.0
	den = np.sum(np.abs(z[1:]) * np.abs(z[:-1]))
	return float(abs(np.sum(z[1:] * np.conj(z[:-1]))) / den) if den > 0 else 0.0


def FindLed(t, cells, hz, edges):
	"""
	The cells that see the LED, and their mean brightness per frame: the cell
	with the most blink, plus every cell with half its amplitude in phase with it.
	"""
	flat = cells.reshape(len(cells), -1)
	zb = FitBlocks(t, flat, hz, edges)
	amp = np.nanmedian(np.abs(zb), axis=0)
	best = int(np.nanargmax(amp))
	rel = np.nanmedian(np.abs(np.angle(zb * np.conj(zb[:, best:best + 1]))), axis=0)
	mask = (amp >= 0.5 * amp[best]) & (rel < 0.3)
	mask[best] = True
	return mask, flat[:, mask].mean(axis=1, dtype=np.float64)


def LitFraction(b):
	# Brightness rescaled to the share of the exposure the LED was lit: 0 dark, 1 lit throughout
	lo, hi = np.percentile(b, [5, 95])
	return (b - lo) / (hi - lo) if hi > lo else np.zeros_like(b)


def Wrap(x, period):
	return (x + period / 2) % period - period / 2


# ------------------------------------------------------------------ the test

def ReadBlinkHz(folder):
	try:
		with open(os.path.join(folder, "trigger.json")) as f:
			return json.load(f).get("blinkHz")
	except (OSError, ValueError):
		return None


def Analyze(folder, blink_hz=None, reference=None, rois=None, tolerance_us=TOLERANCE_US, block_s=BLOCK_S):
	cams = align.LoadRecording(folder)
	period, _ = align.Align(cams, align.ReadTriggerStart(folder))
	warnings = []
	triggered = [c for c in cams if c["usable"] and not c["ratcam"]]
	for c in cams:
		if c["ratcam"]:
			warnings.append("{}: a RatCam free-runs, so its exposures are not on the trigger; left out".format(c["name"]))
		elif not c["usable"]:
			warnings.append("{}: not on the trigger timeline (see campy-align); left out".format(c["name"]))
	if len(triggered) < 2:
		raise SyncTestError("the sync test needs at least two triggered cameras on the trigger timeline")
	names = [c["name"] for c in triggered]
	reference = reference or names[0]
	if reference not in names:
		raise SyncTestError("--reference {} is not one of {}".format(reference, names))

	blink_hz = blink_hz or ReadBlinkHz(folder)
	if not blink_hz:
		raise SyncTestError("no blink rate: record with blinkPin and blinkHz set, or pass --blink-hz")
	blink_hz = float(blink_hz)
	ratio = blink_hz * period
	eps = ratio % 1
	if min(eps, 1 - eps) < 0.05 or abs(eps - 0.5) < 0.05:
		warnings.append("blinkHz / frame rate = {:.3f} is close to a whole or half number, so frames catch the "
			"LED at nearly the same point of its cycle: pick a blink rate with a ratio near x.3 or x.7 "
			"(for example 43 Hz at 25 or 60 fps)".format(ratio))

	t_first = max((c["trigger"][0] - 1) * period for c in triggered)
	t_last = min((c["trigger"][-1] - 1) * period for c in triggered)
	n_blocks = int((t_last - t_first) // block_s)
	if n_blocks < MIN_BLOCKS:
		raise SyncTestError("the cameras overlap for {:.1f} s: record at least {:.0f} s".format(
			t_last - t_first, MIN_BLOCKS * block_s + 1))
	edges = t_first + block_s * np.arange(n_blocks + 1)

	results = {}
	for c in triggered:
		path = os.path.join(c["folder"], str(c["meta"].get("videoFilename", "")))
		if not os.path.isfile(path):
			raise SyncTestError("{}: no video at {}".format(c["name"], path))
		t_all = (np.asarray(c["trigger"], float) - 1) * period
		if c["name"] in (rois or {}):
			roi = rois[c["name"]]
			b, clipped = RoiSignal(ReadFrames(path), roi, c["name"])
		else:
			grid, cell, clipped_cells = CellSignals(ReadFrames(path))
			n = min(len(grid), len(t_all))
			mask, b = FindLed(t_all[:n], grid[:n], blink_hz, edges)
			clipped = int(clipped_cells.reshape(-1)[mask].max())
			rows, cols = np.nonzero(mask.reshape(grid.shape[1:]))
			roi = (int(cols.min() * cell), int(rows.min() * cell),
				int((cols.max() - cols.min() + 1) * cell), int((rows.max() - rows.min() + 1) * cell))
		n = min(len(b), len(t_all))
		if len(b) != len(t_all):
			warnings.append("{}: {} video frames but {} in frametimes.npy; using the first {}".format(
				c["name"], len(b), len(t_all), n))
		z = FitBlocks(t_all[:n], b[:n], blink_hz, edges)
		exposure_us = c["meta"].get("cameraExposureTimeInUs")
		exposure_us = float(exposure_us) if exposure_us not in (None, "None", "") else None
		if exposure_us and blink_hz * exposure_us / 1e6 > 0.25:
			warnings.append("{}: exposure {:.0f} us is long for a {:g} Hz blink: keep it under {:.0f} us so "
				"frames do not average the blink away".format(c["name"], exposure_us, blink_hz, 2.5e5 / blink_hz))
		amplitude = float(np.nanmedian(np.abs(z))) if np.isfinite(z).any() else 0.0
		r = {"frames": int(n), "roi": [int(v) for v in roi], "amplitude": round(amplitude, 2),
			"coherence": round(Coherence(z), 3), "exposure_us": exposure_us,
			"saturated": bool(clipped > SATURATED_SHARE * n)}
		r["led_seen"] = amplitude >= LED_MIN_AMPLITUDE and r["coherence"] >= LED_MIN_COHERENCE
		if r["saturated"]:
			warnings.append("{}: the LED saturates the sensor; dim it or shorten the exposure, "
				"or the fine offset is biased".format(c["name"]))
		results[c["name"]] = r
		c["led"], c["z"], c["n"] = b[:n], z, n

	by_name = {c["name"]: c for c in triggered}
	pairs = {name: Compare(by_name[name], by_name[reference], results, edges, period, blink_hz, tolerance_us)
		for name in names if name != reference}
	order = {PASS: 0, INCONCLUSIVE: 1, FAIL: 2}
	verdict = max((p["verdict"] for p in pairs.values()), key=order.__getitem__)
	report = {"folder": folder, "trigger_rate_hz": 1 / period, "blink_hz": blink_hz, "reference": reference,
		"tolerance_us": tolerance_us, "block_s": block_s, "cameras": results, "pairs": pairs,
		"warnings": warnings, "verdict": verdict}
	with open(os.path.join(folder, "synctest.json"), "w") as f:
		json.dump(report, f, indent=2)
	return report


def Compare(c, ref, results, edges, period, blink_hz, tolerance_us):
	"""How far camera c's exposures are from the reference's, on the triggers both recorded."""
	p = {"pairs": 0, "offset_us": None, "offset_se_us": None, "offset_min_us": None, "offset_max_us": None,
		"method": None, "edge_pairs": 0, "contradictions": None, "verdict": INCONCLUSIVE, "reason": "", "fits_us": []}
	row_ref = {int(k): i for i, k in enumerate(np.asarray(ref["trigger"][:ref["n"]]))}
	rows = [(row_ref[int(k)], i) for i, k in enumerate(np.asarray(c["trigger"][:c["n"]])) if int(k) in row_ref]
	p["pairs"] = len(rows)
	if not rows:
		p["reason"] = "no trigger was recorded by both cameras"
		return p
	i_ref, i = (np.array(v) for v in zip(*rows))
	lit_ref, lit = LitFraction(ref["led"])[i_ref], LitFraction(c["led"])[i]
	off, on = CLEAR_LIT
	p["contradictions"] = int(np.sum(((lit_ref < off) & (lit > on)) | ((lit_ref > on) & (lit < off))))
	for cam in (ref, c):
		r = results[cam["name"]]
		if not r["led_seen"]:
			p["reason"] = ("{} does not see the blinking LED (amplitude {:.1f} gray levels, coherence {:.2f}): "
				"check it is in view, lit, and that the exposure is short".format(cam["name"], r["amplitude"], r["coherence"]))
			return p

	t = (np.asarray(ref["trigger"], float)[i_ref] - 1) * period # the trigger behind both frames
	block = np.searchsorted(edges, t, side="right") - 1
	used, offsets = [], []
	for k in range(len(edges) - 1):
		zr, zc = ref["z"][k], c["z"][k]
		if np.isfinite(zr) and np.isfinite(zc) and np.any(block == k):
			# Both sampled the blink on the same triggers, so their phases differ by the exposures' offset
			used.append(k)
			offsets.append(Wrap(float(np.angle(zr * np.conj(zc))) / (2 * np.pi * blink_hz), 1 / blink_hz))
	if not used:
		p["reason"] = "no fit holds frames from both cameras"
		return p
	p["method"] = "phase"

	# Finer, from frames that caught an edge of the blink in both cameras: on a rising edge a later
	# exposure is lit for longer, so the lit share's difference x the exposure is the offset (negated
	# on a falling edge). Which edge it was comes from the reference's fitted sine at that trigger.
	e_ref, e = results[ref["name"]]["exposure_us"], results[c["name"]]["exposure_us"]
	if (e_ref and e and abs(e - e_ref) <= 0.01 * e_ref and abs(np.median(offsets)) < e_ref / 2e6
			and not (results[ref["name"]]["saturated"] or results[c["name"]]["saturated"])):
		lo, hi = EDGE_LIT
		both = (lit_ref > lo) & (lit_ref < hi) & (lit > lo) & (lit < hi)
		psi = 2 * np.pi * blink_hz * t - np.angle(ref["z"][np.clip(block, 0, len(edges) - 2)])
		d = np.where(np.sin(psi) < 0, 1.0, -1.0) * (e_ref / 1e6) * (lit - lit_ref)
		counts = [int(np.sum(both & (block == k))) for k in used]
		if min(counts) >= MIN_EDGE_PAIRS:
			offsets = [float(np.median(d[both & (block == k)])) for k in used]
			p["method"], p["edge_pairs"] = "edges", sum(counts)
			all_d = d[both & np.isin(block, used)]
			p["offset_se_us"] = float(1.2533 * np.std(all_d, ddof=1) / np.sqrt(len(all_d)) * 1e6)
	us = np.array(offsets) * 1e6
	if p["offset_se_us"] is None and len(us) > 1:
		p["offset_se_us"] = float(1.2533 * np.std(us, ddof=1) / np.sqrt(len(us)))
	p["fits_us"] = [round(float(v), 2) for v in us]
	p["offset_us"], p["offset_min_us"], p["offset_max_us"] = float(np.median(us)), float(us.min()), float(us.max())
	spread = p["offset_max_us"] - p["offset_min_us"]
	if np.max(np.abs(us)) <= tolerance_us:
		p["verdict"], p["reason"] = PASS, "exposures within {:g} us of the reference's throughout".format(tolerance_us)
	elif spread > tolerance_us:
		p["verdict"], p["reason"] = FAIL, ("the offset drifts by {:.0f} us over {:.0f} s: the cameras are not "
			"exposing on a common trigger".format(spread, edges[-1] - edges[0]))
	else:
		p["verdict"], p["reason"] = FAIL, ("a steady offset of {:+.0f} us: check that the trigger reaches both "
			"cameras and that their exposure settings match".format(p["offset_us"]))
	return p


# ------------------------------------------------------------------ output

def FormatReport(r):
	lines = ["Sync test: {}".format(r["folder"]),
		"  trigger {:g} Hz, blink {:g} Hz (ratio {:.3f}), tolerance {:g} us".format(
			r["trigger_rate_hz"], r["blink_hz"], r["blink_hz"] / r["trigger_rate_hz"], r["tolerance_us"])]
	for name, c in r["cameras"].items():
		x, y, w, h = c["roi"]
		lines.append("  {}: {} frames, LED at x={} y={} ({}x{}), blink amplitude {:.1f} gray levels, "
			"coherence {:.2f}{}".format(name, c["frames"], x, y, w, h, c["amplitude"], c["coherence"],
			"" if c["led_seen"] else "  <- LED not seen"))
	for name, p in r["pairs"].items():
		lines.append("  {} vs {}: {}".format(name, r["reference"], p["verdict"]))
		if p["offset_us"] is not None:
			how = ("{} frame pairs that caught a blink edge".format(p["edge_pairs"]) if p["method"] == "edges"
				else "the blink's phase")
			se = " +/- {:.1f}".format(p["offset_se_us"]) if p["offset_se_us"] is not None else ""
			lines.append("    exposure offset {:+.1f}{} us (from {:+.1f} to {:+.1f} us over {} fits of {:g} s; "
				"from {})".format(p["offset_us"], se, p["offset_min_us"], p["offset_max_us"], len(p["fits_us"]),
				r["block_s"], how))
		if p["contradictions"] is not None:
			lines.append("    LED clearly on in one camera and off in the other on {} of {} shared triggers".format(
				p["contradictions"], p["pairs"]))
		lines.append("    {}".format(p["reason"]))
	for w in r["warnings"]:
		lines.append("  warning: {}".format(w))
	lines.append("Verdict: {}".format(r["verdict"]))
	return "\n".join(lines)


def RoiArg(text):
	name, sep, box = text.partition("=")
	try:
		x, y, w, h = (int(v) for v in box.split(","))
	except ValueError:
		raise argparse.ArgumentTypeError("expected NAME=X,Y,W,H")
	if not sep or w <= 0 or h <= 0:
		raise argparse.ArgumentTypeError("expected NAME=X,Y,W,H")
	return name, (x, y, w, h)


def Main(argv=None):
	parser = argparse.ArgumentParser(prog="campy-synctest", description=__doc__.split("\n\n")[0].strip(),
		formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
	parser.add_argument("folder", help="the recording's videoFolder")
	parser.add_argument("--blink-hz", type=float, help="LED blink rate (default: blinkHz, from trigger.json)")
	parser.add_argument("--reference", help="camera the others are compared with (default: the first)")
	parser.add_argument("--roi", action="append", type=RoiArg, default=[], metavar="NAME=X,Y,W,H",
		help="where the LED is in that camera's image (default: found automatically)")
	parser.add_argument("--tolerance-us", type=float, default=TOLERANCE_US,
		help="largest exposure offset that passes (default {:g})".format(TOLERANCE_US))
	args = parser.parse_args(argv)
	try:
		report = Analyze(args.folder, blink_hz=args.blink_hz, reference=args.reference, rois=dict(args.roi),
			tolerance_us=args.tolerance_us)
	except (SyncTestError, align.AlignError) as e:
		print("campy-synctest: {}".format(e), file=sys.stderr)
		return 2
	print(FormatReport(report))
	return 0 if report["verdict"] == PASS else 1


if __name__ == "__main__":
	sys.exit(Main())
