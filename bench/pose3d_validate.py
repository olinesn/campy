"""
campy/pose3d.py on recorded sessions: a 3-camera rig emulated with 3 of a session's
cameras, against the session's all-camera tracking by LUC3D as the reference.

Inputs, per session folder (LUC3D's layout: calibration.toml + one folder per
camera holding that camera's SLEAP predictions .slp):
  1. the predictions, read with sleap-io once and cached as <out>/<session>/pred.npz
  2. LUC3D's Track All grouping of every camera, from bench/pose3d_luc3d_ref.mjs
     (LUC3D's own pose/cross-view-tracker.js, run in node), <out>/<session>/luc3d_ref.json

Reference animal per frame = a LUC3D target holding detections from >= 2 cameras that
frame; its reference 3D = DLT over all its cameras (what LUC3D's Triangulate All gives).

Per camera triple it reports
  assoc      reference animals seen by >= 2 of the 3 cameras whose 3-camera group
             pose3d made exactly (the same detection in each of those cameras)
  pure       pose3d's groups whose detections all belong to one reference animal
  id         exact groups whose pose3d identity is the one mapped to that reference
             animal (identities mapped once, for the session, by most frames shared);
             switches = times a reference animal's pose3d identity changes
  2-view     pose3d animals triangulated from only 2 cameras
  3D err     per node, pose3d 3D vs reference 3D, pure groups (median / p95 mm)
  ms         Tracker.Update per set (median / p99), one thread

  python bench/pose3d_validate.py --session ~/Documents/luc3d/20260506_155203-stressTestFiveMice \\
      --animals 5 --out /tmp/pose3d --triples topB,topL,topR mid,topL,sideR
"""

import os
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
	os.environ.setdefault(_v, "1")

import argparse, glob, hashlib, itertools, json, subprocess, sys, time
import numpy as np
from scipy.optimize import linear_sum_assignment

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from campy import pose3d


def CameraNames(session):
	toml = pose3d._ReadToml(os.path.join(session, "calibration.toml"))
	return [toml[k]["name"] for k in sorted((k for k in toml if k.startswith("cam_")), key=lambda k: int(k[4:]))]


def LoadPredictions(session, cache):
	path = os.path.join(cache, "pred.npz")
	if os.path.exists(path):
		z = np.load(path)
		return z["xy"], z["pscore"], z["count"], list(z["names"])
	import sleap_io as sio
	names = CameraNames(session)
	per = []
	for name in names:
		files = glob.glob(os.path.join(session, name, "*.slp"))
		if len(files) != 1:
			raise SystemExit("{}: expected one .slp, found {}".format(os.path.join(session, name), len(files)))
		labels = sio.load_slp(files[0])
		F = max(lf.frame_idx for lf in labels.labeled_frames) + 1
		I = max(len(lf.instances) for lf in labels.labeled_frames)
		N = len(labels.skeleton.nodes)
		xy = np.full((F, I, N, 2), np.nan, np.float32)
		ps = np.full((F, I, N), np.nan, np.float32)
		cnt = np.zeros(F, np.int16)
		for lf in labels.labeled_frames:
			for i, inst in enumerate(lf.instances):
				a = inst.numpy(scores=True)
				xy[lf.frame_idx, i], ps[lf.frame_idx, i] = a[:, :2], a[:, 2]
			cnt[lf.frame_idx] = len(lf.instances)
		per.append((xy, ps, cnt))
		print("  read", name, F, "frames", flush=True)
	F = min(p[0].shape[0] for p in per)
	I = max(p[0].shape[1] for p in per)
	N = per[0][0].shape[2]
	XY = np.full((F, len(names), I, N, 2), np.nan, np.float32)
	PS = np.full((F, len(names), I, N), np.nan, np.float32)
	CNT = np.zeros((F, len(names)), np.int16)
	for c, (xy, ps, cnt) in enumerate(per):
		XY[:, c, :xy.shape[1]], PS[:, c, :xy.shape[1]], CNT[:, c] = xy[:F], ps[:F], cnt[:F]
	os.makedirs(cache, exist_ok=True)
	np.savez_compressed(path, xy=XY, pscore=PS, count=CNT, names=np.array(names))
	return XY, PS, CNT, names


def LoadReference(session, cache, xy, count, animals, luc3d):
	path = os.path.join(cache, "luc3d_ref.json")
	if not os.path.exists(path):
		# The node script reads the predictions as flat binaries
		xy.astype("<f8").tofile(os.path.join(cache, "xy.f64"))
		count.astype("<i4").tofile(os.path.join(cache, "count.i32"))
		toml = pose3d._ReadToml(os.path.join(session, "calibration.toml"))
		cams = [dict(name=c["name"], matrix=c["matrix"], dist=c["distortions"], rvec=c["rotation"],
			tvec=c["translation"], size=c["size"])
			for c in (toml[k] for k in sorted((k for k in toml if k.startswith("cam_")), key=lambda k: int(k[4:])))]
		with open(os.path.join(cache, "meta.json"), "w") as f:
			json.dump(dict(shape=list(xy.shape), cams=cams), f)
		script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pose3d_luc3d_ref.mjs")
		subprocess.run(["node", script, cache, str(animals), luc3d], check=True)
		for name in ("xy.f64", "count.i32"):
			os.remove(os.path.join(cache, name))
	with open(path) as f:
		rows = json.load(f)
	F, C = len(rows), xy.shape[1]
	ref = np.full((F, animals, C), -1, np.int16)     # [frame, reference id, camera] = slot
	for fi, frame_rows in enumerate(rows):
		for r in frame_rows:
			ref[fi, r[0]] = r[1:]
	return ref


def ReferenceAll3D(cams, xy, ref):
	# DLT of each reference animal from all its cameras that frame
	F, A, C = ref.shape
	N = xy.shape[3]
	norm = np.empty(xy.shape, float)
	for c, cam in enumerate(cams):
		norm[:, c] = cam.Normalize(xy[:, c])
	fi, a = np.nonzero((ref >= 0).sum(2) >= 2)
	vxy = np.full((C, len(fi), N, 2), np.nan)
	for c in range(C):
		s = ref[fi, a, c]
		ok = s >= 0
		vxy[c, ok] = norm[fi[ok], c, s[ok]]
	X = np.full((F, A, N, 3), np.nan)
	frame = pose3d._Frame(cams)
	for k in range(0, len(fi), 20000):
		X[fi[k:k + 20000], a[k:k + 20000]] = pose3d.Triangulate(vxy[:, k:k + 20000], cams, None, frame)
	return X


def RunTriple(cams, xy, ps, ref, ref3d, triple, animals, options):
	F = xy.shape[0]
	tc = [c for c in triple]
	tracker = pose3d.Tracker([cams[c] for c in tc], animals, **options)
	scores = ps if options.get("weighted") or options.get("min_score") else None
	live_members = np.full((F, animals, 3), -1, np.int16)
	live3d = np.full((F, animals, xy.shape[3], 3), np.nan, np.float32)
	nviews = np.zeros((F, animals), np.int8)
	times = np.empty(F)
	for fi in range(F):
		k = xy[fi, tc]
		s = None if scores is None else scores[fi, tc]
		t0 = time.perf_counter()
		p = tracker.Update(k, s, frame=fi)
		times[fi] = time.perf_counter() - t0
		live_members[fi], live3d[fi], nviews[fi] = p.members, p.points3d, p.n_views
	rt = ref[:, :, tc]                                    # (F, A_ref, 3)
	ref_on = (rt >= 0).sum(2) >= 2                         # reference animals the triple can triangulate
	live_on = nviews >= 2
	# Exact matches: live group == reference restricted to the triple
	eq = (live_members[:, :, None, :] == rt[:, None, :, :]).all(-1) & live_on[:, :, None] & ref_on[:, None, :]   # (F, live, ref)
	# Pure: every live member belongs to that reference animal (missing views allowed)
	same = ((live_members[:, :, None, :] == rt[:, None, :, :]) | (live_members[:, :, None, :] < 0))
	pure = same.all(-1) & live_on[:, :, None]
	exact_ref = eq.any(1)                                  # (F, ref)
	# Identity map: live id -> reference id by most exact matches
	co = eq.sum(0)                                         # (live, ref)
	li, ri = linear_sum_assignment(-co)
	idmap = dict(zip(li, ri))
	id_ok = sum(int(eq[:, l, r].sum()) for l, r in idmap.items())
	switches = 0
	for r in range(ref.shape[1]):
		ids = [int(np.argmax(eq[f, :, r])) for f in range(F) if exact_ref[f, r]]
		switches += sum(1 for x, y in zip(ids, ids[1:]) if x != y)
	# 3D error of pure groups
	pf, pl, pr = np.nonzero(pure)
	d = np.linalg.norm(live3d[pf, pl] - ref3d[pf, pr], axis=-1).ravel()
	d = d[np.isfinite(d)]
	two = nviews[live_on] == 2
	return dict(
		ref_sets=int(ref_on.sum()), assoc=float(exact_ref.sum() / max(ref_on.sum(), 1)),
		live_sets=int(live_on.sum()), pure=float(pure.any(2).sum() / max(live_on.sum(), 1)),
		missed=float((ref_on & ~pure.any(1)).sum() / max(ref_on.sum(), 1)),
		id=float(id_ok / max(exact_ref.sum(), 1)), switches=switches,
		two_view=float(two.mean()) if two.size else float("nan"),
		err_med=float(np.median(d)) if d.size else float("nan"),
		err_p95=float(np.percentile(d, 95)) if d.size else float("nan"),
		ms_med=float(np.median(times) * 1e3), ms_p99=float(np.percentile(times, 99) * 1e3),
		ms_max=float(times.max() * 1e3))


def Resolve(names, short):
	# "topB" -> the index of the camera whose name ends in _topB
	out = []
	for s in short.split(","):
		hits = [i for i, n in enumerate(names) if n == s or n.endswith("_" + s)]
		if len(hits) != 1:
			raise SystemExit("camera {!r}: {} matches in {}".format(s, len(hits), names))
		out.append(hits[0])
	return tuple(out)


def Line(name, r):
	return ("{:<22} {:>6} {:>7.4f} {:>6.4f} {:>6.4f} {:>6.4f} {:>5} {:>6.3f} {:>6.1f} {:>6.1f} {:>6.3f} {:>6.3f}".format(
		name, r["ref_sets"], r["assoc"], r["pure"], r["missed"], r["id"], r["switches"], r["two_view"],
		r["err_med"], r["err_p95"], r["ms_med"], r["ms_p99"]))


HEADER = "{:<22} {:>6} {:>7} {:>6} {:>6} {:>6} {:>5} {:>6} {:>6} {:>6} {:>6} {:>6}".format(
	"cameras", "sets", "assoc", "pure", "missed", "id", "sw", "2-view", "mm50", "mm95", "ms50", "ms99")


def Main(argv=None):
	p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
	p.add_argument("--session", required=True)
	p.add_argument("--animals", type=int, required=True)
	p.add_argument("--out", required=True, help="cache folder (predictions, LUC3D reference, results)")
	p.add_argument("--luc3d", default=os.path.expanduser("~/Desktop/luc3d"), help="LUC3D clone (read only)")
	p.add_argument("--triples", nargs="*", default=[], help="camera triples, e.g. topB,topL,topR")
	p.add_argument("--all-triples", action="store_true")
	p.add_argument("--frames", type=int, default=0, help="only the first N frames")
	p.add_argument("--options", default="{}", help="Tracker options as JSON")
	args = p.parse_args(argv)
	session = os.path.expanduser(args.session)
	cache = os.path.join(args.out, os.path.basename(os.path.normpath(session)))
	os.makedirs(cache, exist_ok=True)
	xy, ps, count, names = LoadPredictions(session, cache)
	ref = LoadReference(session, cache, xy, count, args.animals, os.path.expanduser(args.luc3d))
	if args.frames:
		xy, ps, count, ref = xy[:args.frames], ps[:args.frames], count[:args.frames], ref[:args.frames]
	cams = pose3d.LoadCalibration(os.path.join(session, "calibration.toml"), names)
	ref3d = ReferenceAll3D(cams, xy, ref)
	options = json.loads(args.options)
	triples = [Resolve(names, t) for t in args.triples]
	if args.all_triples:
		triples += [t for t in itertools.combinations(range(len(names)), 3) if t not in triples]
	print("{}: {} frames, {} cameras, reference animals with >= 2 cameras per frame: mean {:.2f}".format(
		os.path.basename(session), xy.shape[0], len(names), ((ref >= 0).sum(2) >= 2).sum(1).mean()))
	print("options", options)
	print(HEADER)
	results = {}
	for t in triples:
		label = ",".join(names[c].split("_", 1)[-1] for c in t)
		r = RunTriple(cams, xy, ps, ref, ref3d, t, args.animals, options)
		results[label] = r
		print(Line(label, r), flush=True)
	tag = hashlib.md5(json.dumps(options, sort_keys=True).encode()).hexdigest()[:8]
	with open(os.path.join(cache, "results_{}.json".format(tag)), "w") as f:
		json.dump(dict(options=options, results=results), f, indent=1)
	return results


if __name__ == "__main__":
	Main()
