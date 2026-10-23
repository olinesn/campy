"""
campy-live-flag: find the sets of frames campy-live probably got wrong, from
its log, and pick a few varied ones to label, so that the next model learns
from this rig's own hard moments.

	campy-live-flag ./campy-live_2026-10-22_15-46-09.npy --budget 25

It reads campy-live's log after the recording (campy/live.py: <out>.npy, one
row per set, and <out>.json, its summary) and changes nothing in campy-live:
what it needs is already in each row (the animals each camera found, their
scores, and with --calibration which detection is which animal in each camera,
the 3D and the reprojection errors). So it costs the live loop nothing. Give
several logs (of several recordings) to pick one sample from all of them.

What it flags
-------------
For each set (one row of the log) and camera:

  missed      an animal that 2 or more other cameras saw (it has 3D), whose 3D
              projects inside this camera's picture (--edge-px from its edge),
              but no detection of this camera is that animal, and none lies
              within --near-px of where it projects. The model missed it here,
              or it is hidden behind something; either way a frame worth a
              look, and the projection is where to look. (3D only)
  unmatched   a detection of this camera that is no animal: the 3D matched it
              to none, though there were no more detections than animals.
              The cameras disagree on where the animals are. (3D only)
  disagree    an animal whose detection in this camera is over --disagree-px
              from where its 3D projects (the median over its nodes): one
              camera put the skeleton somewhere else. With only 2 cameras
              seeing it, which one is wrong can't be told: camera -1. Without
              the calibration's file, the log's own reprojection error, camera
              -1. (3D only)
  jump        an animal whose 3D centre moved over --jump-mm since the last
              set it had 3D (plus 15 mm for each frame between them): a wrong
              match, or a swap. (3D only)
  swap        two animals whose 3D centres are each closer to where the other
              was in the set before than to where they themselves were (by
              half, and over --swap-mm in all): an identity switch. (3D only)
  extra       more detections than --animals (seen only when campy-live's
              --max-instances is above the number of animals)
  fewer       fewer detections than --animals (also when an animal is out of
              view or hidden; missed says which ones should have been there)
  low_score   a detection whose nodes' median score is under --low-score

Without 3D (campy-live without --calibration) only extra, fewer and low_score
can be flagged.

Events and the sample
---------------------
A hard moment lasts many sets: a mouse missed for a second is 60 rows. The
flags of one reason and camera less than --merge-s apart make one event, and
an event gives one set to label: the one with the most flags (of any reason)
in it. Then --budget sets are picked:

- a share (--random, 10%) at random from all the sets, as the model meets
  them: to catch mistakes no flag sees, and so the labels aren't all hard
  cases;
- the rest from the events, one reason at a time in turn (missed, swap, jump,
  unmatched, disagree, extra, fewer, low_score), so that each kind of mistake
  gets its share. Within a reason the cameras take turns, and the next pick
  is the event least like the sets picked so far: where the animals are in
  each camera's picture (a 4 x 4 grid of their centres per camera), how many
  each camera found, and with 3D how close together they are. The first pick
  of a reason is its longest event.
- no two picked sets of one recording are less than --min-gap-s apart;
- when the events run out, the rest of the budget is taken at random.

A set is labelled whole (every camera's frame), so each pick also lists every
flag of its set.

Output (into --out, by default <first log>_flags/)
-------------------------------------------------
  flags.csv     every flag: log, row, frame, time_s (since the log's first
                set), reason, camera, animal (the 3D identity, or -1), value
                (missed: px from the nearest detection, or -1 if none;
                unmatched: px from where the nearest animal projects, or -1;
                disagree: px; jump, swap: mm; extra, fewer: how many;
                low_score: the score), x, y (where in the camera's picture:
                missed, where the animal projects; the others, the detection's
                centroid)
  events.csv    one row per event: reason, camera, first and last frame,
                sets, and the set picked to stand for it
  sample.json   the picked sets, each with its log, row, frame, time, why it
                was picked, and its flags; the settings; and the counts
And a report: per reason and camera, how many sets were flagged (and what
share of all sets), in how many events, and how many were picked.

It needs numpy and scipy only (campy-live's environment, or campy's).
"""

import argparse, csv, json, os, sys

import numpy as np

from campy import liveexport, pose3d

REASONS = ("missed", "swap", "jump", "unmatched", "disagree", "extra", "fewer", "low_score")
NEEDS_3D = ("missed", "swap", "jump", "unmatched", "disagree")
DEFAULT_SIZE = (1280, 1024) # campy's FLIR cameras, width x height
FLAG = np.dtype([("log", "<i2"), ("row", "<i8"), ("frame", "<i8"), ("time_s", "<f8"), ("reason", "<U9"),
	("camera", "<i2"), ("animal", "<i2"), ("value", "<f4"), ("x", "<f4"), ("y", "<f4")])
GRID = 4 # the descriptor's grid of animal centres, per camera
SPEED_MM = 15.0 # how far an animal may move per frame (0.9 m/s at 60 fps), on top of --jump-mm


class FlagError(Exception):
	pass


def Say(text):
	print(text, flush=True)


def Settings(**changes):
	"""The defaults of campy-live-flag's options, as its parser gives them, with changes."""
	args = Parser().parse_args(["x"])
	for k, v in changes.items():
		if not hasattr(args, k):
			raise TypeError("no setting {}".format(k))
		setattr(args, k, v)
	return args


# ---------------------------------------------------------------- one log

class Log:
	"""One campy-live log: its rows, what its summary says, and its cameras'
	calibration (None when it has no 3D, or the calibration's file isn't here)."""

	def __init__(self, path, settings, index=0):
		try:
			self.rows, summary = liveexport.ReadLog(path)
		except liveexport.ExportError as e:
			raise FlagError(str(e))
		self.path, self.index = path, index
		summary = summary or {}
		names = self.rows.dtype.names
		self.n = len(self.rows)
		self.cameras_n = self.rows.dtype["found"].shape[0]
		self.cameras = list(summary.get("cameras") or ["camera {}".format(c) for c in range(self.cameras_n)])
		self.has3d = "members" in names
		calibration, calibration_names, animals = liveexport.SummaryCalibration(summary)
		self.animals = settings.animals or animals or (self.rows.dtype["members"].shape[0] if self.has3d else None)
		if not self.animals:
			raise FlagError("{}: how many animals are there? Its summary doesn't say: give --animals.".format(path))
		self.frame_rate = float(summary.get("frame_rate") or 0) or 60.0
		self.calibration = None
		self.size = [DEFAULT_SIZE] * self.cameras_n
		if self.has3d:
			calibration = settings.calibration or calibration
			names_ = settings.calibration_names.split(",") if settings.calibration_names else calibration_names
			if calibration and os.path.isfile(calibration):
				try:
					self.calibration = pose3d.LoadCalibration(calibration, names_ or None)[:self.cameras_n]
				except (KeyError, ValueError) as e:
					raise FlagError("{}: {}".format(calibration, e))
				self.size = [tuple(int(v) for v in (c.size or DEFAULT_SIZE)) for c in self.calibration]
			else:
				Say("{}: the calibration {} isn't on this computer, so where an animal should have been seen "
					"can't be worked out: no missed flags, and disagree uses the log's reprojection errors. "
					"Give it with --calibration.".format(os.path.basename(path), calibration or "(none)"))
		host = np.asarray(self.rows["host_ns"]) if "host_ns" in names else None
		frames = np.asarray(self.rows["frame"], np.int64)
		self.frames = frames
		if host is not None and np.all(host > 0):
			t = np.median(host, axis=1)
			self.time_s = (t - t[0]) / 1e9 if len(t) else t
		else:
			self.time_s = (frames - (frames[0] if len(frames) else 0)) / self.frame_rate


def Centres(keypoints):
	# Each detection's centre: the median of its nodes (NaN: none)
	with np.errstate(all="ignore"), _quiet():
		return np.nanmedian(keypoints, axis=-2)


class _quiet:
	# numpy's "All-NaN slice" warnings, for rows with nothing in them
	def __enter__(self):
		import warnings
		self.w = warnings.catch_warnings()
		self.w.__enter__()
		warnings.simplefilter("ignore", RuntimeWarning)

	def __exit__(self, *a):
		self.w.__exit__(*a)


def FlagBlock(log, first, block, s, out, centres3d):
	"""The flags of one block of the log's rows (first: the block's first row)
	that need only that set: all but jump and swap. centres3d gets each
	animal's 3D centre, for those."""
	B, C = len(block), log.cameras_n
	rows = first + np.arange(B)
	frames = log.frames[rows]
	times = log.time_s[rows]
	kp = np.asarray(block["keypoints"], float)  # B, C, I, N, 2
	vals = np.asarray(block["peak_vals"], float)  # B, C, I, N
	cen = np.asarray(block["centroids"], float)  # B, C, I, 2
	found = np.asarray(block["found"], int)  # B, C
	det = np.isfinite(cen).all(-1) | np.isfinite(kp).all(-1).any(-1)  # B, C, I
	where = np.where(np.isfinite(cen), cen, Centres(kp))

	def add(r, reason, camera, animal, value, xy):
		# r: a mask (B,) or the block's rows; camera, animal, value, xy: one each, or one per row
		r = np.nonzero(r)[0] if r.dtype == bool else np.asarray(r, int)
		if not len(r):
			return
		f = np.zeros(len(r), FLAG)
		f["log"], f["row"], f["frame"], f["time_s"], f["reason"] = log.index, rows[r], frames[r], times[r], reason
		f["camera"], f["animal"], f["value"] = camera, animal, value
		xy = np.broadcast_to(np.asarray(xy, float), (len(r), 2))
		f["x"], f["y"] = xy[:, 0], xy[:, 1]
		out.append(f)

	n_det = det.sum(-1)  # B, C
	for c in range(C):
		r = np.nonzero(n_det[:, c] > log.animals)[0]
		add(r, "extra", c, -1, n_det[r, c] - log.animals, np.nan)
		r = np.nonzero(n_det[:, c] < log.animals)[0]
		add(r, "fewer", c, -1, log.animals - n_det[r, c], np.nan)
	with _quiet():
		score = np.nanmedian(vals, axis=-1)  # B, C, I
	low = det & (score < s.low_score)

	if not log.has3d:
		b, c, i = np.nonzero(low)
		add(b, "low_score", c, -1, score[b, c, i], where[b, c, i])
		return

	members = np.asarray(block["members"], int)  # B, A, C
	n_views = np.asarray(block["n_views"], int)  # B, A
	p3 = np.asarray(block["points3d"], float)  # B, A, N, 3
	A = members.shape[1]
	with _quiet():
		centres3d[rows] = np.nanmedian(p3, axis=2)
	# Which animal each detection is (-1: none)
	animal_of = np.full(det.shape, -1)
	for k in range(A):
		for c in range(C):
			m = members[:, k, c]
			ok = (m >= 0) & (m < det.shape[2])
			animal_of[np.nonzero(ok)[0], c, m[ok]] = k
	b, c, i = np.nonzero(low)
	add(b, "low_score", c, animal_of[b, c, i], score[b, c, i], where[b, c, i])

	if log.calibration is None:
		# The log's own reprojection error, per animal; the camera can't be told
		with _quiet():
			err = np.nanmedian(np.asarray(block["reproj_px"], float), axis=-1)  # B, A
		b, k = np.nonzero((n_views >= 2) & (err > s.disagree_px))
		add(b, "disagree", -1, k, err[b, k], np.nan)
		b, c, i = np.nonzero(det & (animal_of < 0) & (n_det <= log.animals)[..., None])
		add(b, "unmatched", c, -1, -1, where[b, c, i])
		return

	# Where each animal's 3D projects in each camera
	proj = np.full((B, A, C, 2), np.nan)
	front = np.zeros((B, A, C), bool)
	errs = np.full((B, A, C), np.nan)
	for c, cam in enumerate(log.calibration):
		depth = (p3 @ cam.R.T + cam.t)[..., 2]  # B, A, N
		pts = cam.Project(p3)  # B, A, N, 2
		pts[~(depth > 0)] = np.nan
		with _quiet():
			proj[:, :, c] = np.nanmedian(pts, axis=2)
			front[:, :, c] = np.all((depth > 0) | ~np.isfinite(depth), axis=2) & (np.isfinite(depth).sum(2) >= 3)
		# The animal's own detection here, against its projection
		m = members[:, :, c]
		has = m >= 0
		mine = kp[np.arange(B)[:, None], c, np.clip(m, 0, kp.shape[2] - 1)]  # B, A, N, 2
		with _quiet():
			e = np.nanmedian(np.linalg.norm(mine - pts, axis=-1), axis=-1)
		errs[:, :, c] = np.where(has, e, np.nan)
	w = np.array([sz[0] for sz in log.size], float)
	h = np.array([sz[1] for sz in log.size], float)
	inside = (front & (proj[..., 0] >= s.edge_px) & (proj[..., 0] < w - s.edge_px)
		& (proj[..., 1] >= s.edge_px) & (proj[..., 1] < h - s.edge_px))  # B, A, C
	# The nearest detection of each camera to each animal's projection
	d = np.linalg.norm(proj[:, :, :, None, :] - where[:, None], axis=-1)  # B, A, C, I
	d = np.where(det[:, None] & np.isfinite(d), d, np.inf)
	nearest = np.min(d, axis=-1) if d.shape[-1] else np.full(d.shape[:3], np.inf)
	b, k, c = np.nonzero((n_views >= 2)[..., None] & (members < 0) & inside & ~(nearest <= s.near_px))
	add(b, "missed", c, k, np.where(np.isfinite(nearest[b, k, c]), nearest[b, k, c], -1), proj[b, k, c])
	# A detection that is no animal, with no more detections than animals
	b, c, i = np.nonzero(det & (animal_of < 0) & (n_det <= log.animals)[..., None])
	dd = np.min(np.where(np.isfinite(d[b, :, c, i]), d[b, :, c, i], np.inf), axis=-1) if len(b) else np.zeros(0)
	add(b, "unmatched", c, -1, np.where(np.isfinite(dd), dd, -1), where[b, c, i])
	# One camera's detection far from where the 3D puts it
	top = np.max(np.where(np.isfinite(errs), errs, -np.inf), axis=-1)  # B, A
	worst = np.argmax(np.where(np.isfinite(errs), errs, -np.inf), axis=-1)
	b, k = np.nonzero((n_views >= 2) & (top > s.disagree_px))
	w_c = worst[b, k]
	three = n_views[b, k] >= 3
	xy = where[b, w_c, np.clip(members[b, k, w_c], 0, where.shape[2] - 1)]
	xy[~three] = np.nan
	add(b, "disagree", np.where(three, w_c, -1), k, top[b, k], xy)


def FlagMotion(log, s, centres3d):
	"""swap and jump, from each animal's 3D centre set by set. A swap's two
	animals jump too: those are flagged as the swap only."""
	out = []
	A = centres3d.shape[1]
	frames, times = log.frames, log.time_s

	def flag(r, reason, k, value):
		f = np.zeros(len(r), FLAG)
		f["log"], f["row"], f["frame"], f["time_s"], f["reason"] = log.index, r, frames[r], times[r], reason
		f["camera"], f["animal"], f["value"], f["x"], f["y"] = -1, k, value, np.nan, np.nan
		out.append(f)

	swapped = set() # (row, animal)
	if len(frames) > 1:
		prev, cur = centres3d[:-1], centres3d[1:]
		near = (frames[1:] - frames[:-1]) <= s.max_gap
		for k in range(A):
			for l in range(k + 1, A):
				straight = (np.linalg.norm(cur[:, k] - prev[:, k], axis=-1)
					+ np.linalg.norm(cur[:, l] - prev[:, l], axis=-1))
				cross = (np.linalg.norm(cur[:, k] - prev[:, l], axis=-1)
					+ np.linalg.norm(cur[:, l] - prev[:, k], axis=-1))
				with np.errstate(invalid="ignore"):
					swap = near & (cross < 0.5 * straight) & (straight > s.swap_mm)
				r = np.nonzero(swap)[0] + 1
				flag(r, "swap", k, straight[r - 1])
				flag(r, "swap", l, straight[r - 1])
				swapped.update((int(x), k) for x in r)
				swapped.update((int(x), l) for x in r)
	for k in range(A):
		ok = np.nonzero(np.isfinite(centres3d[:, k]).all(-1))[0]
		if len(ok) < 2:
			continue
		a, b = ok[:-1], ok[1:]
		gap = frames[b] - frames[a]
		dist = np.linalg.norm(centres3d[b, k] - centres3d[a, k], axis=-1)
		jump = (gap <= s.max_gap) & (dist > s.jump_mm + SPEED_MM * np.maximum(gap - 1, 0))
		jump &= np.array([(int(x), k) not in swapped for x in b], bool) if swapped else True
		flag(b[jump], "jump", k, dist[jump])
	return out


def FlagLog(log, s):
	"""Every flag of one log, sorted by row."""
	out = []
	centres3d = np.full((log.n, log.rows.dtype["members"].shape[0] if log.has3d else 0, 3), np.nan)
	for first, block in liveexport.Blocks(log.rows):
		FlagBlock(log, first, block, s, out, centres3d)
	if log.has3d:
		out += FlagMotion(log, s, centres3d)
	flags = np.concatenate(out) if out else np.zeros(0, FLAG)
	return flags[np.argsort(flags["row"], kind="stable")]


# ---------------------------------------------------------------- events and the sample

def Events(flags, s):
	"""One event per run of flags of one reason and camera (in one log) less
	than s.merge_s apart: dicts with the rows they cover."""
	events = []
	if not len(flags):
		return events
	keys = np.stack([flags["log"], flags["camera"]], -1)
	for reason in REASONS:
		of = flags[flags["reason"] == reason]
		for lg, cam in sorted(set(map(tuple, keys[flags["reason"] == reason].tolist()))):
			f = of[(of["log"] == lg) & (of["camera"] == cam)]
			rows, idx = np.unique(f["row"], return_index=True)
			t = f["time_s"][idx]
			cut = np.nonzero(np.diff(t) > s.merge_s)[0] + 1
			for part_rows, part_t in zip(np.split(rows, cut), np.split(t, cut)):
				events.append(dict(reason=reason, log=int(lg), camera=int(cam), rows=part_rows,
					first_frame=None, last_frame=None, start_s=float(part_t[0]), end_s=float(part_t[-1])))
	return events


def Descriptor(log, row):
	"""How a set looks, roughly: where the animals are in each camera's
	picture (a GRID x GRID count of their centres, per camera, over the
	number of animals), each camera's count against the number of animals,
	and with 3D how close together the animals are (the closest pair, over
	100 mm, at most 2)."""
	r = np.asarray(log.rows[row])
	cen = np.asarray(r["centroids"], float)
	kp = np.asarray(r["keypoints"], float)
	where = np.where(np.isfinite(cen), cen, Centres(kp))
	parts = []
	for c in range(log.cameras_n):
		w, h = log.size[c]
		p = where[c][np.isfinite(where[c]).all(-1)]
		grid = np.zeros((GRID, GRID))
		gx = np.clip((p[:, 0] / w * GRID).astype(int), 0, GRID - 1)
		gy = np.clip((p[:, 1] / h * GRID).astype(int), 0, GRID - 1)
		np.add.at(grid, (gy, gx), 1.0)
		parts += [grid.ravel() / log.animals, [(len(p) - log.animals) / log.animals]]
	if log.has3d:
		with _quiet():
			c3 = np.nanmedian(np.asarray(r["points3d"], float), axis=1)
		c3 = c3[np.isfinite(c3).all(-1)]
		close = 2.0
		if len(c3) >= 2:
			d = np.linalg.norm(c3[:, None] - c3[None], axis=-1)
			close = min(2.0, float(d[np.triu_indices(len(c3), 1)].min()) / 100.0)
		parts.append([close])
	return np.concatenate([np.ravel(p) for p in parts])


def Sample(logs, flags, events, s):
	"""The sets to label: dicts (log, row, why), --budget of them at most."""
	rng = np.random.default_rng(s.seed)
	picked, described = [], []

	def free(lg, row):
		t = logs[lg].time_s[row]
		return all(p["log"] != lg or abs(logs[lg].time_s[p["row"]] - t) >= s.min_gap_s for p in picked)

	def take(lg, row, why, event=None):
		picked.append(dict(log=lg, row=int(row), why=why, event=event))
		described.append(Descriptor(logs[lg], row))

	# Each event's set: the one in it with the most flags of any reason
	count = {}
	for lg in range(len(logs)):
		rows, n = np.unique(flags["row"][flags["log"] == lg], return_counts=True)
		count[lg] = dict(zip(rows.tolist(), n.tolist()))
	for e in events:
		n = np.array([count[e["log"]].get(int(r), 0) for r in e["rows"]])
		best = np.nonzero(n == n.max())[0]
		e["pick"] = int(e["rows"][best[len(best) // 2]])
		e["first_frame"] = int(logs[e["log"]].frames[e["rows"][0]])
		e["last_frame"] = int(logs[e["log"]].frames[e["rows"][-1]])

	n_random = int(round(s.budget * s.random))
	n_flagged = s.budget - n_random
	by_reason = {r: [i for i, e in enumerate(events) if e["reason"] == r] for r in REASONS}
	used, turn = set(), {r: 0 for r in REASONS}
	while sum(p["why"] != "random" for p in picked) < n_flagged:
		progress = False
		for reason in REASONS:
			if sum(p["why"] != "random" for p in picked) >= n_flagged:
				break
			cands = [i for i in by_reason[reason] if i not in used and free(events[i]["log"], events[i]["pick"])]
			if not cands:
				continue
			# The cameras take turns: prefer the camera this reason has picked least
			cams = sorted({events[i]["camera"] for i in cands})
			picks = {c: sum(1 for p in picked if p["why"] == reason and events[p["event"]]["camera"] == c)
				for c in cams}
			cam = min(cams, key=lambda c: (picks[c], c))
			cands = [i for i in cands if events[i]["camera"] == cam]
			if not described:
				best = max(cands, key=lambda i: (len(events[i]["rows"]), -i))
			else:
				D = np.stack(described)
				def novelty(i):
					if "descriptor" not in events[i]:
						events[i]["descriptor"] = Descriptor(logs[events[i]["log"]], events[i]["pick"])
					return float(np.min(np.linalg.norm(D - events[i]["descriptor"], axis=1)))
				best = max(cands, key=lambda i: (round(novelty(i), 6), len(events[i]["rows"]), -i))
			used.add(best)
			take(events[best]["log"], events[best]["pick"], reason, best)
			progress = True
		if not progress:
			break
	# The random share, from all the sets, keeping the gap
	pool = [(lg, r) for lg in range(len(logs)) for r in range(logs[lg].n)]
	order = rng.permutation(len(pool)) if pool else []
	want = s.budget - len(picked)
	for j in order:
		if want <= 0:
			break
		lg, r = pool[j]
		if free(lg, r):
			take(lg, r, "random")
			want -= 1
	return picked


# ---------------------------------------------------------------- writing

def WriteFlags(path, logs, flags):
	with open(path, "w", newline="") as f:
		w = csv.writer(f)
		w.writerow(["log", "row", "frame", "time_s", "reason", "camera", "animal", "value", "x", "y"])
		for x in flags:
			w.writerow([os.path.basename(logs[x["log"]].path), int(x["row"]), int(x["frame"]),
				"{:.3f}".format(x["time_s"]), x["reason"], logs[x["log"]].cameras[x["camera"]] if x["camera"] >= 0
				else "", int(x["animal"]), "{:.4g}".format(x["value"]), Num(x["x"]), Num(x["y"])])


def Num(v):
	return "" if not np.isfinite(v) else "{:.1f}".format(v)


def WriteEvents(path, logs, events, picked):
	chosen = {p["event"] for p in picked if p["event"] is not None}
	with open(path, "w", newline="") as f:
		w = csv.writer(f)
		w.writerow(["log", "reason", "camera", "first_frame", "last_frame", "start_s", "end_s", "sets",
			"pick_frame", "picked"])
		for i, e in enumerate(events):
			lg = logs[e["log"]]
			w.writerow([os.path.basename(lg.path), e["reason"], lg.cameras[e["camera"]] if e["camera"] >= 0 else "",
				e["first_frame"], e["last_frame"], "{:.3f}".format(e["start_s"]), "{:.3f}".format(e["end_s"]),
				len(e["rows"]), int(lg.frames[e["pick"]]), int(i in chosen)])


def SampleJson(logs, flags, events, picked, s):
	sets = []
	for p in sorted(picked, key=lambda p: (p["log"], p["row"])):
		lg = logs[p["log"]]
		f = flags[(flags["log"] == p["log"]) & (flags["row"] == p["row"])]
		sets.append(dict(log=lg.path, row=p["row"], frame=int(lg.frames[p["row"]]),
			time_s=round(float(lg.time_s[p["row"]]), 3), why=p["why"],
			flags=[dict(reason=str(x["reason"]), camera=lg.cameras[x["camera"]] if x["camera"] >= 0 else None,
				animal=int(x["animal"]) if x["animal"] >= 0 else None, value=round(float(x["value"]), 3),
				x=None if not np.isfinite(x["x"]) else round(float(x["x"]), 1),
				y=None if not np.isfinite(x["y"]) else round(float(x["y"]), 1)) for x in f]))
	return dict(kind="campy-live-flag sample", version=1,
		logs=[dict(path=lg.path, sets=lg.n, cameras=lg.cameras, animals=lg.animals, has3d=lg.has3d,
			calibration=bool(lg.calibration)) for lg in logs],
		settings={k: v for k, v in vars(s).items() if k not in ("logs", "out")},
		counts=Counts(logs, flags, events, picked), sets=sets)


def Counts(logs, flags, events, picked):
	"""Per reason and camera: sets flagged, their share of all sets, events, picks."""
	total = sum(lg.n for lg in logs)
	out = []
	for reason in REASONS:
		f = flags[flags["reason"] == reason]
		for cam in sorted(set(f["camera"].tolist())):
			g = f[f["camera"] == cam]
			sets = len(set(zip(g["log"].tolist(), g["row"].tolist())))
			ev = [i for i, e in enumerate(events) if e["reason"] == reason and e["camera"] == cam]
			name = logs[0].cameras[cam] if cam >= 0 else "any camera"
			out.append(dict(reason=reason, camera=name, sets=sets, share=round(sets / max(total, 1), 4),
				events=len(ev), picked=sum(1 for p in picked if p["event"] in ev)))
	return out


def Report(logs, flags, events, picked, s):
	total = sum(lg.n for lg in logs)
	Say("{} sets in {} log{}; {} of them flagged.".format(total, len(logs), "s" * (len(logs) != 1),
		len(set(zip(flags["log"].tolist(), flags["row"].tolist())))))
	if not any(lg.has3d for lg in logs):
		Say("No 3D in the log{} (campy-live ran without --calibration): only extra, fewer and low_score "
			"can be flagged.".format("s" * (len(logs) != 1)))
	Say("{:<10} {:<16} {:>8} {:>7} {:>7} {:>7}".format("reason", "camera", "sets", "share", "events", "picked"))
	for c in Counts(logs, flags, events, picked):
		Say("{:<10} {:<16} {:>8} {:>6.1f}% {:>7} {:>7}".format(c["reason"], c["camera"], c["sets"],
			100 * c["share"], c["events"], c["picked"]))
	n_random = sum(p["why"] == "random" for p in picked)
	Say("Picked {} sets to label: {} for their flags, {} at random.".format(len(picked), len(picked) - n_random,
		n_random))


def Run(s):
	if not s.logs:
		raise FlagError("give one or more campy-live logs (.npy)")
	logs = [Log(p, s, i) for i, p in enumerate(s.logs)]
	flags = np.concatenate([FlagLog(lg, s) for lg in logs]) if logs else np.zeros(0, FLAG)
	events = Events(flags, s)
	picked = Sample(logs, flags, events, s)
	out = s.out or os.path.splitext(s.logs[0])[0] + "_flags"
	os.makedirs(out, exist_ok=True)
	WriteFlags(os.path.join(out, "flags.csv"), logs, flags)
	WriteEvents(os.path.join(out, "events.csv"), logs, events, picked)
	sample = SampleJson(logs, flags, events, picked, s)
	with open(os.path.join(out, "sample.json"), "w") as f:
		json.dump(sample, f, indent=1)
	Report(logs, flags, events, picked, s)
	Say("Written: {}".format(out))
	return sample


def Parser():
	p = argparse.ArgumentParser(prog="campy-live-flag", description="Find the sets campy-live probably got "
		"wrong, from its log, and pick a few varied ones to label (python -m pydoc campy.liveflag).",
		formatter_class=argparse.ArgumentDefaultsHelpFormatter)
	p.add_argument("logs", nargs="+", help="campy-live logs (.npy; its .json summary next to it).")
	p.add_argument("--out", default="", help="Folder for flags.csv, events.csv, sample.json (default: "
		"<first log>_flags).")
	p.add_argument("--budget", type=int, default=25, help="How many sets to pick to label (a set is every "
		"camera's frame).")
	p.add_argument("--random", type=float, default=0.1, help="The share of the picks taken at random.")
	p.add_argument("--min-gap-s", type=float, default=2.0, help="No two picks of a recording closer than this.")
	p.add_argument("--merge-s", type=float, default=1.0, help="Flags of a reason and camera closer than this "
		"are one event.")
	p.add_argument("--animals", type=int, default=0, help="How many animals there are (default: the summary's).")
	p.add_argument("--calibration", default="", help="The calibration.toml, when the summary's isn't on this "
		"computer.")
	p.add_argument("--calibration-names", default="", help="Its cameras' names, comma-separated, in the log's "
		"camera order (default: the summary's).")
	p.add_argument("--edge-px", type=float, default=30.0, help="missed: the animal projects at least this far "
		"inside the picture.")
	p.add_argument("--near-px", type=float, default=60.0, help="missed: and no detection is this close to it.")
	p.add_argument("--disagree-px", type=float, default=15.0, help="disagree: a camera's detection this far "
		"from the animal's 3D (median of its nodes).")
	p.add_argument("--jump-mm", type=float, default=50.0, help="jump: the 3D centre moved this far in one set "
		"(plus {:.0f} mm per frame between).".format(SPEED_MM))
	p.add_argument("--swap-mm", type=float, default=30.0, help="swap: the two animals moved at least this far "
		"in all.")
	p.add_argument("--max-gap", type=int, default=30, help="jump, swap: compare sets at most this many frames "
		"apart.")
	p.add_argument("--low-score", type=float, default=0.5, help="low_score: a detection's median node score "
		"under this.")
	p.add_argument("--seed", type=int, default=0, help="For the random picks.")
	return p


def Main(argv=None):
	s = Parser().parse_args(argv)
	try:
		Run(s)
	except FlagError as e:
		Say("campy-live-flag: {}".format(e))
		sys.exit(1)


if __name__ == "__main__":
	Main()
