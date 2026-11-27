"""
A pair of models scored in 3D on test sets labelled in every camera, against
the hand labels triangulated the same way (docs/models.md, "Round 5").

	python bench/model_eval_3d.py test.slp calibration.toml \\
		--pair new=runs/centroid,runs/instance --pair old=models/centroid,models/instance \\
		--animals 2 --live-args --max-instances 2 --animals 2

test.slp holds labelled camera frames; the sets used are the frame numbers
labelled in every camera of the calibration (each video's camera is the
CameraN_name in its file name). Per set, every camera's frame goes through
campy-live's own inference code (campy/modeleval.py's Pair, as
campy-model-eval), then campy-live's 3D (campy/pose3d.py's Tracker, fresh for
each set). The hand labels go through the same 3D. Per pair it reports

  found3d     labelled 3D animals with a predicted 3D animal whose middle is
              within --match-mm (50) of theirs, matched one to one
  err3d_mm    node distance between matched predicted and labelled 3D
              animals: p50, p90, p99, share within 5 and 10 mm
  reproj_px   the predicted 3D projected into each camera, against that
              camera's 2D labels (matched per camera): what a closed-loop
              rule reading the 3D sees, in pixels
  pred_reproj_px_p50   the 3D's own reprojection error (how well the
              cameras' 2D predictions agree), median

Run it in campy-live's environment (torch, sleap-nn, sleap-io); any of
campy-live's options can follow --live-args (last). --animals is how
many animals the 3D looks for. Results also go to --out (JSON).
"""

import argparse, json, re, sys
import numpy as np
import sleap_io as sio
from scipy.optimize import linear_sum_assignment
from campy import modeleval, pose3d, strictjson


def camera(v):
	while getattr(v, "source_video", None) is not None:
		v = v.source_video
	m = re.search(r"(Camera\d_[A-Za-z]+)", str(v.filename).replace("\\", "/").split("/")[-1])
	return m.group(1)


def stats(e, unit):
	e = np.asarray(e, float)
	if not len(e):
		return {}
	return {"n": int(len(e)), "p50": round(float(np.percentile(e, 50)), 2), "p90": round(float(np.percentile(e, 90)), 2),
			"p99": round(float(np.percentile(e, 99)), 2), f"within5{unit}": round(float(np.mean(e <= 5)), 4),
			f"within10{unit}": round(float(np.mean(e <= 10)), 4)}


ap = argparse.ArgumentParser()
ap.add_argument("labels"); ap.add_argument("calibration"); ap.add_argument("--pair", action="append", required=True)
ap.add_argument("--animals", type=int, default=2); ap.add_argument("--out", default="")
ap.add_argument("--match-mm", type=float, default=50.0)
ap.add_argument("--live-args", nargs=argparse.REMAINDER, default=[])
a = ap.parse_args()
L = sio.load_slp(a.labels)
names = [n.name for n in L.skeletons[0].nodes]
sets = {}
for lf in L.labeled_frames:
	if lf.user_instances:
		sets.setdefault(int(lf.frame_idx), {})[camera(lf.video)] = lf
cams = sorted({c for s in sets.values() for c in s})
sets = {f: s for f, s in sorted(sets.items()) if len(s) == len(cams)}
cal = pose3d.LoadCalibration(a.calibration, cams)
shape = next(iter(next(iter(sets.values())).values())).image.shape
print(f"{len(sets)} sets x {len(cams)} cameras: {', '.join(cams)}", flush=True)
results = {}
lab3d = {}
for spec in a.pair:
	name, rest = spec.split("=", 1); cen, ins = rest.split(",")
	pair = modeleval.Pair(name, cen, ins, a.live_args, len(cams), shape)
	mn = pair.names or names
	e3, rp, own, found, total = [], [], [], 0, 0
	for f, s in sets.items():
		imgs = np.stack([s[c].image for c in cams])
		r = pair.Run(imgs)
		kp = np.asarray(r["keypoints"], float)					   # (cams, K, N, 2)
		labs = [modeleval.LabelArray(s[c], names, mn)[0] for c in cams]
		K = max(len(x) for x in labs)
		la = np.full((len(cams), max(K, 1), len(mn), 2), np.nan)
		for i, x in enumerate(labs):
			la[i, :len(x)] = x
		if f not in lab3d.setdefault(tuple(mn), {}):
			lab3d[tuple(mn)][f] = pose3d.Tracker(cal, a.animals).Update(la, frame=f).points3d
		XL = lab3d[tuple(mn)][f]
		p = pose3d.Tracker(cal, a.animals).Update(kp, frame=f)
		XP = p.points3d
		own += [float(x) for x in np.ravel(p.reproj_px) if np.isfinite(x)]
		okL = np.isfinite(XL).all(-1).any(-1); okP = np.isfinite(XP).all(-1).any(-1)
		total += int(okL.sum())
		cL = np.array([np.nanmedian(x, 0) for x in XL]); cP = np.array([np.nanmedian(x, 0) for x in XP])
		if okL.any() and okP.any():
			d = np.linalg.norm(cL[okL][:, None] - cP[okP][None], axis=-1)
			ri, ci = linear_sum_assignment(np.nan_to_num(d, nan=1e9))
			iL, iP = np.nonzero(okL)[0], np.nonzero(okP)[0]
			for i, j in zip(ri, ci):
				if d[i, j] > a.match_mm:
					continue
				found += 1
				dd = np.linalg.norm(XL[iL[i]] - XP[iP[j]], axis=-1)
				e3 += dd[np.isfinite(dd)].tolist()
		# predicted 3D projected into each camera vs its 2D labels
		for ci_, cam in enumerate(cal):
			P2 = cam.Project(XP[okP]) if okP.any() else np.zeros((0, len(mn), 2))
			lab = labs[ci_]
			if not len(P2) or not len(lab):
				continue
			d = np.array([[np.nanmedian(np.linalg.norm(P2[j] - lab[i], axis=-1)) for j in range(len(P2))] for i in range(len(lab))])
			ri, cj = linear_sum_assignment(np.nan_to_num(d, nan=1e9))
			for i, j in zip(ri, cj):
				dd = np.linalg.norm(P2[j] - lab[i], axis=-1)
				rp += dd[np.isfinite(dd)].tolist()
	results[name] = {"animals3d": total, "found3d": found, "err3d_mm": stats(e3, "mm"), "reproj_px": stats(rp, "px"),
					 "pred_reproj_px_p50": round(float(np.median(own)), 2) if own else None}
	print(name, json.dumps(results[name]), flush=True)
if a.out:
	with open(a.out, "w") as fh:
		strictjson.dump(results, fh, indent=1)
