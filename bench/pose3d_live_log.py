"""
How well campy/pose3d.py grouped a campy-live log's detections across cameras.

There is no ground truth on a live recording, so each set is compared with the
best grouping its own 2D allows: every way of grouping the detections across
the cameras is tried, and the one whose worst camera fits best wins (per
group, each camera's median reprojection error over its nodes scored above
--min-score; a camera with fewer than 3 such nodes counts as 100 px). A group
of that best grouping is *clean* when every camera of it fits within --clean-px;
only clean groups are judged, as elsewhere the 2D itself cannot say which
detection is which animal.

Per log it reports
  error    pose3d's groups re-triangulated from nodes scored above --min-score:
           median reprojection error (px, over views and nodes), p50 / p90
  clean    the share of clean groups pose3d made exactly (the same detection in
           each camera)
  split    the share of pose3d's camera pairs (two cameras of one animal)
           touching a clean group that do not belong to one
  ms       Tracker.Update per set (with --replay)

By default it judges the groups the log holds (campy-live's `members`); with
--replay it runs a fresh Tracker over the log's keypoints instead, with
--options, so settings can be compared on one recording:

  python bench/pose3d_live_log.py live1.npy calibration.toml
  python bench/pose3d_live_log.py live1.npy calibration.toml --replay \\
      --options '{"max_penalty": null, "match_reproj": 0}'

The cameras are the log's (its .json), in the log's order. Needs numpy and scipy.
"""

import os
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
	os.environ.setdefault(_v, "1")

import argparse, itertools, json, math, sys, time, warnings
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from campy import pose3d, strictjson


def BestGroupings(kp, scores, cams, animals, min_score):
	# Per set, the grouping (animals x cameras, detection or -1) whose worst camera fits best,
	# and each of its groups' error (px, the worst camera's median)
	S, C, I, N = kp.shape[:4]
	if math.factorial(animals) ** (C - 1) > 20000:
		raise SystemExit("{} cameras and {} animals: too many groupings to try".format(C, animals))
	norm = np.stack([cams[c].Normalize(kp[:, c]) for c in range(C)], 1)
	good = (scores > min_score) & np.isfinite(norm).all(-1)
	present = np.isfinite(kp).all(-1).any(-1)                          # (S, C, I)
	frame = pose3d._Frame(cams)
	options = list(itertools.product(range(-1, I), repeat=C))
	E = np.full((S, len(options)), np.nan)
	for k, group in enumerate(options):
		used = [c for c, i in enumerate(group) if i >= 0]
		if len(used) < 2:
			E[:, k] = 0.0
		else:
			xy = np.full((C, S, N, 2), np.nan)
			for c in used:
				xy[c] = np.where(good[:, c, group[c]][..., None], norm[:, c, group[c]], np.nan)
			X = pose3d.Triangulate(xy, cams, None, frame)
			e = pose3d.ReprojectionErrors(X, xy, cams)
			e = np.where(np.isfinite(xy).all(-1) & np.isfinite(X).all(-1)[None], e, np.nan)
			with warnings.catch_warnings():
				warnings.simplefilter("ignore", RuntimeWarning)
				per_view = np.where(np.isfinite(e).sum(-1) >= 3, np.nanmedian(e, -1), 100.0)
			E[:, k] = per_view[used].max(0)
		for c in used:
			E[~present[:, c, group[c]], k] = np.nan                     # that detection is not there
	index = {g: k for k, g in enumerate(options)}
	best = np.full((S, animals, C), -1)
	err = np.full((S, animals), np.nan)
	for s in range(S):
		dets = [[i for i in range(I) if present[s, c, i]] for c in range(C)]
		pads = [d[:animals] + [-1] * (animals - len(d[:animals])) for d in dets]
		top = None
		for perms in itertools.product(*(set(itertools.permutations(p)) for p in pads[1:])):
			groups = [tuple([pads[0][a]] + [p[a] for p in perms]) for a in range(animals)]
			cost = sum(E[s, index[g]] for g in groups)
			if np.isfinite(cost) and (top is None or cost < top[0]):
				top = (cost, groups)
		if top:
			best[s] = top[1]
			err[s] = [E[s, index[g]] for g in top[1]]
	return best, err


def MembersError(members, kp, scores, cams, min_score):
	# Each group re-triangulated from its nodes scored above min_score: median reprojection error
	S, A, C = members.shape
	N = kp.shape[3]
	norm = np.stack([cams[c].Normalize(kp[:, c]) for c in range(C)], 1)
	good = (scores > min_score) & np.isfinite(norm).all(-1)
	frame = pose3d._Frame(cams)
	out = np.full((S, A), np.nan)
	for a in range(A):
		xy = np.full((C, S, N, 2), np.nan)
		for c in range(C):
			m = members[:, a, c]
			s = np.nonzero(m >= 0)[0]
			xy[c, s] = np.where(good[s, c, m[s]][..., None], norm[s, c, m[s]], np.nan)
		X = pose3d.Triangulate(xy, cams, None, frame)
		e = pose3d.ReprojectionErrors(X, xy, cams)
		e = np.where(np.isfinite(xy).all(-1), e, np.nan)
		with warnings.catch_warnings():
			warnings.simplefilter("ignore", RuntimeWarning)
			out[:, a] = np.nanmedian(e.transpose(1, 0, 2).reshape(S, -1), 1)
		out[(members[:, a] >= 0).sum(-1) < 2, a] = np.nan
	return out


def Judge(members, best, err, clean_px):
	S, A, C = best.shape
	clean = ((best >= 0).sum(-1) >= 2) & (err <= clean_px)              # (S, A)
	made = sum(any((members[s, t] == best[s, g]).all() for t in range(members.shape[1]))
		for s, g in zip(*np.nonzero(clean)))
	split = pairs = 0
	for s in range(S):
		for t in range(members.shape[1]):
			views = [(c, members[s, t, c]) for c in range(C) if members[s, t, c] >= 0]
			for (a, i), (b, j) in itertools.combinations(views, 2):
				ga = {g for g in range(A) if clean[s, g] and best[s, g, a] == i}
				gb = {g for g in range(A) if clean[s, g] and best[s, g, b] == j}
				if ga or gb:
					pairs += 1
					split += not (ga & gb)
	return made / max(clean.sum(), 1), split / max(pairs, 1), int(clean.sum())


def Main(argv=None):
	p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
	p.add_argument("log", help="campy-live's <out>.npy (its <out>.json beside it)")
	p.add_argument("calibration")
	p.add_argument("--replay", action="store_true", help="run a fresh Tracker over the log's keypoints")
	p.add_argument("--options", default="{}", help="Tracker options as JSON (with --replay)")
	p.add_argument("--min-score", type=float, default=0.5)
	p.add_argument("--clean-px", type=float, default=15.0)
	args = p.parse_args(argv)
	r = np.load(args.log)
	with open(args.log[:-4] + ".json") as f:
		summary = strictjson.load(f)
	cams = pose3d.LoadCalibration(args.calibration, summary["cameras"])
	kp, scores = r["keypoints"].astype(float), r["peak_vals"].astype(float)
	animals = r["members"].shape[1] if "members" in r.dtype.names else kp.shape[2]
	ms = float("nan")
	if args.replay:
		tracker = pose3d.Tracker(cams, animals, **json.loads(args.options))
		members = np.full((len(r), animals, len(cams)), -1)
		times = np.empty(len(r))
		for s in range(len(r)):
			t0 = time.perf_counter()
			members[s] = tracker.Update(kp[s], scores[s], frame=int(r["frame"][s])).members
			times[s] = time.perf_counter() - t0
		ms = float(np.median(times) * 1e3)
	elif "members" in r.dtype.names:
		members = r["members"].astype(int)
	else:
		raise SystemExit("the log has no 3D (campy-live ran without --calibration): use --replay")
	best, err = BestGroupings(kp, scores, cams, animals, args.min_score)
	error = MembersError(members, kp, scores, cams, args.min_score)
	made, split, n = Judge(members, best, err, args.clean_px)
	print("{}: {} sets, {} cameras, {} animals; clean groups {} ({:.1%} of the best grouping's)".format(
		os.path.basename(args.log), len(r), len(cams), animals, n,
		n / max(((best >= 0).sum(-1) >= 2).sum(), 1)))
	print("{}: error p50 {:.1f} p90 {:.1f} px | clean {:.1%} | split {:.1%} | ms {:.2f}".format(
		("replay " + args.options) if args.replay else "logged", np.nanmedian(error), np.nanpercentile(error, 90),
		made, split, ms))
	return dict(error_p50=float(np.nanmedian(error)), clean=made, split=split, ms=ms)


if __name__ == "__main__":
	Main()
