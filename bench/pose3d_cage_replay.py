"""
What a cage would have done to a recording: campy-live's logged 2D replayed
through campy/pose3d.py's Tracker without a cage and with one (pose3d.Cage,
as campy-live-view's k saves it), set by set, the two trackers taking turns.

    python bench/pose3d_cage_replay.py <recording folder> --cage <calibration>.cage.json [--log campy-live.npy]

It needs the recording's campy-live.npy, campy-live.json and calibration.toml
(campy-live-export writes the calibration; --calibration gives another). Per
run it prints:
- whether the replay without a cage gives the logged 3D back (it should: the
  same code, on the same 2D);
- animal-sets more than 25 cm from the middle of where the animals seen by
  every camera are (no animal can be: the positions a wrong grouping across
  cameras makes), without and with the cage;
- animal-sets the cage set aside, and where they were: below the floor,
  above the top, past a wall;
- the share of sets with every animal in 3D;
- swaps: sets where two identities' positions exchange (by more than 30 mm in
  all, against the positions each had last, within 5 sets);
- the 3D step's time per set, without and with the cage (mean, p50, p99).
"""

import argparse, json, os, sys, time, warnings

import numpy as np
from scipy.optimize import linear_sum_assignment

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from campy import pose3d # noqa: E402


def Swaps(X, frames):
	# Sets where the identities' middles fit the last ones better exchanged (by over 30 mm in all)
	out = []
	last = X[0].copy()
	seen = np.where(np.isfinite(last).all(-1), 0, -10 ** 9)
	for s in range(1, len(X)):
		now = X[s]
		ok = np.isfinite(now).all(-1)
		idx = np.nonzero(ok & (s - seen <= 5))[0]
		if len(idx) >= 2:
			D = np.linalg.norm(now[idx][:, None] - last[idx][None], axis=-1)
			r, c = linear_sum_assignment(D)
			if (c != np.arange(len(idx))).any() and D[np.arange(len(idx)), np.arange(len(idx))].sum() - D[r, c].sum() > 30:
				out.append(int(frames[s]))
		last = np.where(ok[:, None], now, last)
		seen = np.where(ok, s, seen)
	return out


def Main():
	ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
	ap.add_argument("recording")
	ap.add_argument("--cage", required=True)
	ap.add_argument("--log", default="campy-live.npy")
	ap.add_argument("--calibration", default="")
	a = ap.parse_args()
	warnings.simplefilter("ignore", RuntimeWarning)
	log_path = os.path.join(a.recording, a.log)
	summary = json.load(open(os.path.splitext(log_path)[0] + ".json"))
	names = summary.get("calibration_names") or summary["cameras"]
	cameras = pose3d.LoadCalibration(a.calibration or os.path.join(a.recording, "calibration.toml"), names=names)
	cage = pose3d.Cage.Load(a.cage)
	refused = cage.Check(cameras)
	if refused:
		raise SystemExit("The cage does not fit this calibration: {}".format(refused))
	log = np.load(log_path, mmap_mode="r")
	frames = np.asarray(log["frame"]).astype(int)
	keypoints, scores = np.asarray(log["keypoints"]), np.asarray(log["peak_vals"])
	A = summary["animals"]
	trackers = {"none": pose3d.Tracker(cameras, n_animals=A), "cage": pose3d.Tracker(cameras, n_animals=A, cage=cage)}
	S, N = len(frames), keypoints.shape[3]
	out = {k: dict(P=np.full((S, A, N, 3), np.nan, np.float32), outside=np.zeros((S, A), bool), ms=np.zeros(S))
		for k in trackers}
	order = list(trackers)
	for s in range(S):
		for k in (order if s % 2 else order[::-1]): # each first every other set
			start = time.perf_counter()
			p = trackers[k].Update(keypoints[s], scores[s], int(frames[s]))
			out[k]["ms"][s] = (time.perf_counter() - start) * 1e3
			out[k]["P"][s], out[k]["outside"][s] = p.points3d, p.outside
	logged = np.asarray(log["points3d"], np.float32)
	same = np.allclose(np.nan_to_num(out["none"]["P"], nan=1e9), np.nan_to_num(logged, nan=1e9), atol=1e-2)
	print("{}: {} sets, {} animals; the replay without a cage gives the logged 3D back: {}".format(
		os.path.basename(os.path.abspath(a.recording)), S, A, same))
	middle = np.nanmedian(pose3d.AnimalCentres(logged.astype(float))[np.asarray(log["n_views"]) == len(cameras)], 0)
	for k in trackers:
		P = out[k]["P"].astype(float)
		C = pose3d.AnimalCentres(P)
		has = np.isfinite(C).all(-1)
		ms = out[k]["ms"]
		print("  {:4s}: {:5d} animal-sets more than 25 cm from the middle; every animal in 3D {:.2f}% of sets; {} swaps; "
			"3D step {:.3f} ms a set (p50 {:.3f}, p99 {:.3f})".format(k, int((has & (np.linalg.norm(C - middle, axis=-1) >
			250)).sum()), (has.sum(1) == A).mean() * 100, len(Swaps(C, frames)), ms.mean(), np.percentile(ms, 50),
			np.percentile(ms, 99)))
	# Where the cage's set-aside ones were (from the replay without it)
	C = pose3d.AnimalCentres(out["none"]["P"].astype(float))
	away = cage.Outside(C)
	h = ((C - cage.origin) @ cage._frame.T)[..., 2]
	below, above = away & (h < -cage.margin), away & (h > cage.height + cage.margin)
	print("  the cage set aside {} animal-sets ({:.2f}%): {} below the floor (median {:.0f} mm below), {} above the top, "
		"{} past a wall".format(int(out["cage"]["outside"].sum()), out["cage"]["outside"].mean() * 100, int(below.sum()),
		float(np.median(-h[below] - 0)) if below.any() else 0.0, int(above.sum()), int((away & ~below & ~above).sum())))


if __name__ == "__main__":
	Main()
