"""
campy-model-fit: does a rig's video look like the images a model was trained
on? A model trained on darker frames, or on frames without the camera's gamma,
loses animals on a rig whose frames are brighter (docs/models.md, "Recording
for a model"); this says so before a recording, not after.

Two steps:

	campy-model-fit stats <training package> --out <model folder>/image_stats.json
	campy-model-fit check <model folder> --recording <a short recording on the rig>

stats reads up to --frames labelled frames of the training package (spread
over all its videos) and keeps, over those frames, the spread (5th, 50th and
95th percentile) of:

  mean        the frame's mean brightness (0-255)
  p95         its 95th percentile of brightness
  saturated   the share of its pixels at 250 or more
  animal      the median brightness of the animals: the pixels within 10 px of
              their labelled points (bodies, not background)

and what the package's frames were recorded with, when given (--gamma, the
cameraGamma the training videos had; --encoded, how they were encoded). It
also writes the model's id there (model.json, when the folder has best.ckpt:
the first 12 hex digits of its SHA-256), so a recording can name its model.

check reads --seconds of each camera's video in the recording (or --frames
frames spread over it) and its metadata.csv (cameraGamma, exposure, gain,
codec, quality), works out the same numbers (animal: with --log, campy-live's
keypoints of that recording, else not checked), and says, per camera:

  darker / brighter     its median frame mean is outside the training
                        frames' 5th-95th percentile
  more saturated        over 3 times the training frames' 95th percentile of
                        saturated pixels, and over 0.5% of the frame
  animals darker /      the animals' median brightness below 0.8 or above 1.25
  brighter              times the training animals'
  gamma differs         the camera's gamma isn't the training videos'
                        (--gamma when making the stats; not for campy's
                        emulated cameras, which replay a clip)

and exits 1 if any camera got a warning (2 if it couldn't check). It reads
videos with sleap-io (campy-live's environment).
"""

import argparse, hashlib, json, os, sys

import numpy as np

SATURATED = 250
NEAR_PX = 10
QUANTILES = (5, 50, 95)


class FitError(Exception):
	pass


def Say(text):
	print(text, flush=True)


def FrameStats(img):
	img = np.asarray(img)
	if img.ndim == 3:
		img = img[..., 0]
	return dict(mean=float(img.mean()), p95=float(np.percentile(img, 95)),
		saturated=float(np.mean(img >= SATURATED)))


def AnimalPixels(img, points):
	"""The median brightness of the pixels within NEAR_PX of any of the points
	((n, 2), NaN ignored), or None."""
	img = np.asarray(img)
	if img.ndim == 3:
		img = img[..., 0]
	pts = np.asarray(points, float).reshape(-1, 2)
	pts = pts[np.isfinite(pts).all(-1)]
	if not len(pts):
		return None
	h, w = img.shape
	mask = np.zeros((h, w), bool)
	yy, xx = np.mgrid[-NEAR_PX:NEAR_PX + 1, -NEAR_PX:NEAR_PX + 1]
	disk = yy ** 2 + xx ** 2 <= NEAR_PX ** 2
	for x, y in pts:
		cx, cy = int(round(x)), int(round(y))
		y0, y1, x0, x1 = cy - NEAR_PX, cy + NEAR_PX + 1, cx - NEAR_PX, cx + NEAR_PX + 1
		dy0, dx0 = max(0, -y0), max(0, -x0)
		dy1, dx1 = disk.shape[0] - max(0, y1 - h), disk.shape[1] - max(0, x1 - w)
		if dy1 <= dy0 or dx1 <= dx0:
			continue
		mask[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] |= disk[dy0:dy1, dx0:dx1]
	return float(np.median(img[mask])) if mask.any() else None


def Spread(values):
	v = np.asarray([x for x in values if x is not None], float)
	if not len(v):
		return None
	return {"p{}".format(q): round(float(np.percentile(v, q)), 5) for q in QUANTILES}


def Summarize(per_frame, animal):
	return dict(frames=len(per_frame), mean=Spread([f["mean"] for f in per_frame]),
		p95=Spread([f["p95"] for f in per_frame]), saturated=Spread([f["saturated"] for f in per_frame]),
		animal=Spread(animal))


def ModelId(folder):
	"""The first 12 hex digits of the SHA-256 of the folder's best.ckpt, or None."""
	path = os.path.join(folder, "best.ckpt")
	if not os.path.isfile(path):
		return None
	h = hashlib.sha256()
	with open(path, "rb") as f:
		for block in iter(lambda: f.read(1 << 20), b""):
			h.update(block)
	return h.hexdigest()[:12]


def TrainingStats(package, frames=300, seed=0):
	"""The stats of up to `frames` labelled frames of a training package."""
	import sleap_io as sio
	labels = sio.load_slp(package)
	lfs = [lf for lf in labels if lf.user_instances]
	if not lfs:
		raise FitError("{} has no labelled frames".format(package))
	rng = np.random.default_rng(seed)
	pick = sorted(rng.choice(len(lfs), size=min(frames, len(lfs)), replace=False))
	per_frame, animal = [], []
	for i in pick:
		lf = lfs[i]
		img = lf.image
		per_frame.append(FrameStats(img))
		animal.append(AnimalPixels(img, np.concatenate([inst.numpy() for inst in lf.user_instances])))
	return Summarize(per_frame, animal)


def RigStats(recording, cameras=None, seconds=10.0, frames=30, log=None):
	"""Per camera of the recording: its stats over `frames` frames of its first
	`seconds`, and its metadata.csv settings."""
	import sleap_io as sio
	from campy import liveexport
	from campy.align import ReadMetadata
	names = cameras or sorted(d for d in os.listdir(recording) if os.path.isfile(os.path.join(recording, d,
		"metadata.csv")))
	if not names:
		raise FitError("{} has no camera folders (with metadata.csv)".format(recording))
	rows = None
	if log:
		rows, summary = liveexport.ReadLog(log)
		log_cameras = (summary or {}).get("cameras") or names
	out = {}
	for name in names:
		cam = liveexport.Camera(recording, name)
		meta = ReadMetadata(os.path.join(cam.folder, "metadata.csv"))
		video = sio.Video(os.path.join(cam.folder, cam.video))
		rate = float(meta.get("frameRate") or 30)
		n = min(len(video), max(1, int(seconds * rate)))
		idx = np.unique(np.linspace(0, n - 1, min(frames, n)).astype(int))
		per_frame, animal = [], []
		points = {}
		if rows is not None and name in log_cameras:
			c = log_cameras.index(name)
			vf = cam.Index(np.asarray(rows["frame"]))
			for r in np.nonzero(np.isin(vf, idx))[0]:
				points[int(vf[r])] = np.asarray(rows[int(r)]["keypoints"][c], float)
		for i in idx:
			img = video[int(i)]
			per_frame.append(FrameStats(img))
			if int(i) in points:
				animal.append(AnimalPixels(img, points[int(i)]))
		settings = {k: meta.get(k) for k in ("cameraMake", "cameraGamma", "cameraExposureTimeInUs", "cameraGain",
			"codec", "quality", "gpuMake")}
		out[name] = dict(stats=Summarize(per_frame, animal if points else []), settings=settings)
	return out


def Compare(train, rig, gamma=None):
	"""Warnings (text) for one camera's rig stats against the training stats."""
	warn = []
	s, t = rig["stats"], train["stats"]
	m = s["mean"]["p50"]
	if t.get("mean"):
		lo, hi = t["mean"]["p5"], t["mean"]["p95"]
		if m < lo:
			warn.append("darker: its frames' mean brightness {:.0f} is below most training frames' ({:.0f}-{:.0f})"
				.format(m, lo, hi))
		elif m > hi:
			warn.append("brighter: its frames' mean brightness {:.0f} is above most training frames' ({:.0f}-{:.0f})"
				.format(m, lo, hi))
	if t.get("saturated"):
		sat, lim = s["saturated"]["p50"], t["saturated"]["p95"]
		if sat > max(3 * lim, 0.005):
			warn.append("more saturated: {:.2%} of its pixels at {} or more, against at most {:.2%} in most training "
				"frames".format(sat, SATURATED, lim))
	if s.get("animal") and t.get("animal"):
		a, ta = s["animal"]["p50"], t["animal"]["p50"]
		ratio = a / max(ta, 1e-6)
		if ratio < 0.8:
			warn.append("animals darker: their median brightness {:.0f}, the training animals' {:.0f}".format(a, ta))
		elif ratio > 1.25:
			warn.append("animals brighter: their median brightness {:.0f}, the training animals' {:.0f}".format(a, ta))
	g = rig["settings"].get("cameraGamma")
	tg = gamma if gamma is not None else train.get("gamma")
	emulated = str(rig["settings"].get("cameraMake")).lower() == "emu" # a clip recorded elsewhere: its gamma isn't this
	if tg is not None and g is not None and not emulated and str(g).lower() != str(tg).lower():
		warn.append("gamma differs: the camera's cameraGamma is {}, the training videos' {}".format(g, tg))
	return warn


def ReadStats(path):
	if os.path.isdir(path):
		path = os.path.join(path, "image_stats.json")
	if not os.path.isfile(path):
		raise FitError("no image stats at {}: make them with campy-model-fit stats <training package> --out {}"
			.format(path, path))
	with open(path) as f:
		return json.load(f)


def Main(argv=None):
	p = argparse.ArgumentParser(prog="campy-model-fit", description="Does a rig's video look like a model's "
		"training images? (python -m pydoc campy.modelfit)")
	sub = p.add_subparsers(dest="what", required=True)
	a = sub.add_parser("stats", help="The training images' stats, for a model's folder.")
	a.add_argument("package", help="The training package (.pkg.slp).")
	a.add_argument("--out", required=True, help="image_stats.json to write (a model folder: into it).")
	a.add_argument("--frames", type=int, default=300, help="Labelled frames to read.")
	a.add_argument("--gamma", default=None, help="The cameraGamma the training videos were recorded with (e.g. Off).")
	a.add_argument("--encoded", default=None, help="How the training videos were encoded (e.g. \"h264 crf 15\").")
	b = sub.add_parser("check", help="A rig's recording against a model's stats.")
	b.add_argument("model", help="The model folder with image_stats.json (or the json).")
	b.add_argument("--recording", required=True, help="A recording on the rig (its folder).")
	b.add_argument("--cameras", default="", help="Its cameras, comma-separated (default: every camera folder).")
	b.add_argument("--seconds", type=float, default=10.0, help="Read the first this many seconds.")
	b.add_argument("--frames", type=int, default=30, help="Frames per camera to read.")
	b.add_argument("--log", default="", help="campy-live's log of that recording: the animals' brightness too.")
	b.add_argument("--out", default="", help="Write the comparison here (JSON).")
	args = p.parse_args(argv)
	try:
		if args.what == "stats":
			stats = TrainingStats(args.package, args.frames)
			out = os.path.join(args.out, "image_stats.json") if os.path.isdir(args.out) else args.out
			doc = dict(kind="campy-model-fit stats", package=os.path.basename(args.package), gamma=args.gamma,
				encoded=args.encoded, stats=stats)
			with open(out, "w") as f:
				json.dump(doc, f, indent=1)
			Say("{} frames: mean brightness {p5:.0f}-{p95:.0f} (5th-95th percentile), animals {a}. Written: {o}".format(
				stats["frames"], a="{:.0f}".format(stats["animal"]["p50"]) if stats["animal"] else "-", o=out,
				**stats["mean"]))
			folder = os.path.dirname(os.path.abspath(out))
			mid = ModelId(folder)
			if mid:
				card = os.path.join(folder, "model.json")
				data = {}
				if os.path.isfile(card):
					with open(card) as f:
						data = json.load(f)
				data.update(id=mid, image_stats="image_stats.json", training_package=os.path.basename(args.package))
				with open(card, "w") as f:
					json.dump(data, f, indent=1)
				Say("Model id {} (model.json).".format(mid))
			return 0
		train = ReadStats(args.model)
		rig = RigStats(args.recording, args.cameras.split(",") if args.cameras else None, args.seconds, args.frames,
			args.log or None)
	except FitError as e:
		print("campy-model-fit: {}".format(e), file=sys.stderr)
		return 2
	bad = 0
	result = {}
	for name, r in rig.items():
		warn = Compare(train, r)
		result[name] = dict(r, warnings=warn)
		s = r["stats"]
		Say("{}: mean brightness {:.0f}, saturated {:.2%}{}, gamma {}, {} {}: {}".format(name, s["mean"]["p50"],
			s["saturated"]["p50"], ", animals {:.0f}".format(s["animal"]["p50"]) if s["animal"] else "",
			r["settings"].get("cameraGamma"), r["settings"].get("codec"), r["settings"].get("quality"),
			"; ".join(warn) if warn else "looks like the training images"))
		bad += bool(warn)
	if args.out:
		with open(args.out, "w") as f:
			json.dump(dict(training=train, cameras=result), f, indent=1)
	return 1 if bad else 0


if __name__ == "__main__":
	sys.exit(Main())
