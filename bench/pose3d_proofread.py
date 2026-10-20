"""
campy/pose3d.py against proofread identities: 3 cameras of a session at a time,
scored with each camera's proofread tracks as the truth.

A session folder holds calibration.toml and one folder per camera with that
camera's proofread predictions, *.proofread.slp (SLEAP's tracks corrected by
hand; the keypoints are the model's). A camera's proofread track names need not
match another camera's, so each camera's tracks are mapped to the reference
camera's (--ref, the first camera by default) by 2-view geometry: the pairing
with the lowest median reprojection error over the session.

The Tracker runs on the proofread files' keypoints and node scores (it never
sees the tracks). Per camera triple it reports
  correct   detections in the triple's cameras whose pose3d identity is their
            animal's (identities mapped to animals once, by most detections shared);
            a detection left without an identity counts as wrong
  pure      pose3d's groups (2+ cameras) whose detections are all one animal
  switches  lasting identity switches: per animal and camera, how often the
            identity holding it changes to one it then keeps for 30+ sets;
            averaged over the 3 cameras
  ms        Tracker.Update per set

  python bench/pose3d_proofread.py --sessions <folder> [<folder> ...] --out <cache folder> \\
      --triples top,mid,back topL,midL,backL top,topL,midL --ref top [--options '{...}']

It needs sleap-io (to read the .slp files, once; cached in --out).
"""

import os
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
	os.environ.setdefault(_v, "1")

import argparse, glob, json, sys, time
import numpy as np
from scipy.optimize import linear_sum_assignment

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from campy import pose3d


def LoadSession(session, cameras, cache):
	# Every camera's proofread keypoints, scores and track index: (F, C, I, N, 2), (F, C, I, N), (F, C, I)
	path = os.path.join(cache, os.path.basename(os.path.normpath(session)) + ".npz")
	if os.path.exists(path):
		z = np.load(path)
		return z["xy"].astype(float), z["ps"].astype(float), z["trk"]
	import sleap_io as sio
	per = []
	for name in cameras:
		files = glob.glob(os.path.join(session, name, "*.proofread.slp"))
		if len(files) != 1:
			raise SystemExit("{}: expected one *.proofread.slp, found {}".format(os.path.join(session, name), len(files)))
		labels = sio.load_slp(files[0])
		tracks = list(labels.tracks)
		F = max(lf.frame_idx for lf in labels.labeled_frames) + 1
		I = max(len(lf.instances) for lf in labels.labeled_frames)
		N = len(labels.skeleton.nodes)
		xy = np.full((F, I, N, 2), np.nan, np.float32)
		ps = np.full((F, I, N), np.nan, np.float32)
		trk = np.full((F, I), -1, np.int16)
		for lf in labels.labeled_frames:
			for i, inst in enumerate(lf.instances):
				a = inst.numpy(scores=True) if hasattr(inst, "score") else np.c_[inst.numpy(), np.ones(N)]
				xy[lf.frame_idx, i], ps[lf.frame_idx, i] = a[:, :2], a[:, 2]
				trk[lf.frame_idx, i] = tracks.index(inst.track) if inst.track is not None else -1
		per.append((xy, ps, trk))
	F = min(p[0].shape[0] for p in per)
	I = max(p[0].shape[1] for p in per)
	N = per[0][0].shape[2]
	XY = np.full((F, len(cameras), I, N, 2), np.nan, np.float32)
	PS = np.full((F, len(cameras), I, N), np.nan, np.float32)
	TRK = np.full((F, len(cameras), I), -1, np.int16)
	for c, (xy, ps, trk) in enumerate(per):
		k = xy.shape[1]
		XY[:, c, :k], PS[:, c, :k], TRK[:, c, :k] = xy[:F], ps[:F], trk[:F]
	os.makedirs(cache, exist_ok=True)
	np.savez_compressed(path, xy=XY, ps=PS, trk=TRK)
	return XY.astype(float), PS.astype(float), TRK


def Animals(xy, ps, trk, cams, ref, step=7):
	# animal[f, c, i]: each detection's animal (the reference camera's track index), -1 unknown
	F, C, I = trk.shape
	T = int(trk.max()) + 1
	animals = int(trk[:, ref].max()) + 1
	amap = np.full((C, T), -1)
	amap[ref, :animals] = np.arange(animals)
	norm = np.stack([cams[c].Normalize(xy[:, c]) for c in range(C)], 1)
	good = (ps > 0.5) & np.isfinite(norm).all(-1)
	for c in range(C):
		if c == ref:
			continue
		errs = {}
		for f in range(0, F, step):
			for i in np.nonzero(trk[f, c] >= 0)[0]:
				for j in np.nonzero(trk[f, ref] >= 0)[0]:
					ok = good[f, c, i] & good[f, ref, j]
					if ok.sum() < 4:
						continue
					p = np.stack([norm[f, c, i][ok], norm[f, ref, j][ok]])
					X = pose3d.Triangulate(p, [cams[c], cams[ref]])
					e = np.median(pose3d.ReprojectionErrors(X, p, [cams[c], cams[ref]]))
					errs.setdefault((trk[f, c, i], trk[f, ref, j]), []).append(e)
		M = np.full((T, animals), 1e6)
		for (a, b), v in errs.items():
			if len(v) >= 20:
				M[a, b] = np.median(v)
		for a, b in zip(*linear_sum_assignment(M)):
			if M[a, b] < 30:
				amap[c, a] = b
	animal = np.where(trk >= 0, amap[np.arange(C)[None, :, None], np.maximum(trk, 0)], -1)
	return animal, animals


def RunTriple(xy, ps, animal, cams, triple, animals, options, min_run=30):
	F = xy.shape[0]
	tracker = pose3d.Tracker([cams[c] for c in triple], animals, **options)
	members = np.full((F, animals, 3), -1)
	times = np.empty(F)
	for f in range(F):
		t0 = time.perf_counter()
		members[f] = tracker.Update(xy[f, triple], ps[f, triple], frame=f).members
		times[f] = time.perf_counter() - t0
	an = animal[:, triple]                                             # (F, 3, I)
	# the animal of each member: (F, animals, 3), -2 = no member
	ma = np.where(members >= 0, an[np.arange(F)[:, None, None], np.arange(3)[None, None], np.maximum(members, 0)], -2)
	groups = ((members >= 0).sum(-1) >= 2) & ((ma >= 0) | (ma == -2)).all(-1)
	pure = [len(set(row[row >= 0])) == 1 for row in ma[groups]]
	shared = np.zeros((animals, animals))
	for t in range(animals):
		v = ma[:, t][ma[:, t] >= 0]
		np.add.at(shared[t], v, 1)
	rows, cols = linear_sum_assignment(-shared)
	switches = 0
	for k in range(3):
		for a in range(animals):
			holders = [int(np.nonzero(ma[f, :, k] == a)[0][0]) for f in range(F) if (ma[f, :, k] == a).any()]
			runs = []
			for h in holders:
				if runs and runs[-1][0] == h:
					runs[-1][1] += 1
				else:
					runs.append([h, 1])
			runs = [r for r in runs if r[1] >= min_run]
			switches += sum(r0[0] != r1[0] for r0, r1 in zip(runs, runs[1:]))
	return dict(correct=float(shared[rows, cols].sum() / max((an >= 0).sum(), 1)),
		pure=float(np.mean(pure)) if pure else float("nan"), switches=switches / 3.0,
		ms=float(np.median(times) * 1e3))


def Main(argv=None):
	p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
	p.add_argument("--sessions", nargs="+", required=True)
	p.add_argument("--out", required=True, help="cache folder (the proofread keypoints, once per session)")
	p.add_argument("--triples", nargs="+", required=True, help="camera triples, e.g. top,mid,back")
	p.add_argument("--ref", help="camera whose tracks name the animals (default: the first camera of the first triple)")
	p.add_argument("--options", default="{}", help="Tracker options as JSON")
	args = p.parse_args(argv)
	triples = [t.split(",") for t in args.triples]
	cameras = sorted({c for t in triples for c in t} | ({args.ref} if args.ref else set()))
	ref = cameras.index(args.ref or triples[0][0])
	options = json.loads(args.options)
	print("options", options)
	print("{:<28} {:<20} {:>7} {:>8} {:>8} {:>6}".format("session", "cameras", "animals", "correct", "pure", "sw"))
	results = []
	for session in args.sessions:
		xy, ps, trk = LoadSession(session, cameras, args.out)
		cams = pose3d.LoadCalibration(os.path.join(session, "calibration.toml"), cameras)
		animal, animals = Animals(xy, ps, trk, cams, ref)
		for t in triples:
			r = RunTriple(xy, ps, animal, cams, [cameras.index(c) for c in t], animals, options)
			r.update(session=os.path.basename(os.path.normpath(session)), cameras=",".join(t), animals=animals)
			results.append(r)
			print("{:<28} {:<20} {:>7} {:>8.2%} {:>8.3%} {:>6.1f}".format(r["session"], r["cameras"], animals,
				r["correct"], r["pure"], r["switches"]), flush=True)
	c = np.array([r["correct"] for r in results])
	print("{} runs: correct mean {:.2%} median {:.2%} | pure {:.3%} | lasting switches {:.1f} | {:.2f} ms/set".format(
		len(results), c.mean(), np.median(c), np.nanmean([r["pure"] for r in results]),
		sum(r["switches"] for r in results), np.median([r["ms"] for r in results])))
	return results


if __name__ == "__main__":
	Main()
