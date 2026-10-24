"""
campy-model-eval: how well a pair of models finds the animals in a rig's test
sets, labelled frames never trained on, measured against the labels; and an
old pair against a new one on the same frames.

	campy-model-eval tests/chronicrecord.test.pkg.slp \\
		--pair new=runs/new_centroid,runs/new_instance --pair old=models/centroid,models/instance

The test package is campy-label-merge's (--test-into): each round's labelled
camera frames, with each video's camera, each mouse's identity ("animal k")
and the rig's calibration. Each set (every camera's frame of one moment) runs
through campy-live's own inference code, as live: the same preprocessing,
--fixed-crops, the tie-tolerant peaks, fp16 and the compiled engines by
default on a GPU (any of campy-live's options can follow --live-args). So the
numbers are those of the models as campy-live runs them, on this computer's
GPU. Run it in campy-live's environment, under the rig lock on a rig.

What it reports, per pair (and per camera):

  animals       labelled animals in the camera frames
  found         labelled animals with a detection: a centroid within
                --match-px of the label's anchor node (the centroid model's,
                Trunk), matched one to one (Hungarian)
  missed        labelled animals with none; their share
  extra         detections matched to no labelled animal, per frame
  wrong mouse   found animals whose skeleton is on another animal: the median
                distance of its nodes from the label's over --wrong-px (30)
  node error    over the labelled nodes of the found animals (wrong mouse
                included), the distance in pixels: median, p90, p99; and the
                share within 5 and 10 px; nodes the model didn't place are
                counted apart (node missed)
  grouped       with the calibration: of the labelled animals seen in 2 or
                more cameras, the share whose detections campy-live's 3D
                (campy/pose3d.py's Tracker, from scratch on each set) put in
                one animal, and no other animal's with them. Identity switches
                over time need labelled sequences, which a test set of single
                sets doesn't have.

All of it twice more: on the test sets picked at random (as the model meets
the rig) and on those picked for a flag (the hard cases), as campy-live-flag
picked them. Results also go to --out (JSON).

The metrics (Score, Grouped) need numpy and scipy only; running the models
needs torch and sleap-nn.
"""

import argparse, json, os, sys, tempfile

import numpy as np
from scipy.optimize import linear_sum_assignment

from campy import strictjson

ANCHOR = "Trunk"


class EvalError(Exception):
	pass


def Say(text):
	print(text, flush=True)


# ---------------------------------------------------------------- matching and scores

def Anchor(points, names):
	"""The label's anchor: its Trunk, or the median of its visible nodes."""
	if ANCHOR in names and np.isfinite(points[names.index(ANCHOR)]).all():
		return points[names.index(ANCHOR)]
	ok = np.isfinite(points).all(-1)
	return np.median(points[ok], axis=0) if ok.any() else np.full(2, np.nan)


def Match(label_anchor, pred_centroid, match_px):
	"""One-to-one pairs (label, detection) whose centroid is within match_px of
	the label's anchor, the closest overall (Hungarian)."""
	if not len(label_anchor) or not len(pred_centroid):
		return []
	d = np.linalg.norm(label_anchor[:, None] - pred_centroid[None], axis=-1)
	d = np.where(np.isfinite(d), d, 1e9)
	r, c = linear_sum_assignment(d)
	return [(int(a), int(b)) for a, b in zip(r, c) if d[a, b] <= match_px]


class Score:
	"""Counts and node errors for one pair of models, added camera frame by
	camera frame."""

	def __init__(self, wrong_px=30.0):
		self.wrong_px = wrong_px
		self.animals = self.found = self.extra = self.wrong = self.frames = 0
		self.node_missed = 0
		self.errors = []
		self.grouped = self.groupable = 0

	def Add(self, labels, preds, centroids, names, match_px):
		"""labels (L, N, 2) NaN where not labelled/visible; preds (K, N, 2) and
		centroids (K, 2) NaN rows for none. Returns the pairs (label, pred)."""
		self.frames += 1
		have = ~np.isnan(centroids).any(-1) | ~np.isnan(preds).all(axis=(1, 2))
		preds, centroids = preds[have], centroids[have]
		idx = np.nonzero(have)[0]
		# a detection without a centroid (shouldn't happen): its median node
		centroids = np.where(np.isfinite(centroids), centroids, np.nanmedian(preds, axis=1) if len(preds) else centroids)
		anchors = np.array([Anchor(l, names) for l in labels]).reshape(-1, 2)
		pairs = Match(anchors, centroids, match_px)
		self.animals += len(labels)
		self.found += len(pairs)
		self.extra += len(preds) - len(pairs)
		for a, b in pairs:
			lab = labels[a]
			ok = np.isfinite(lab).all(-1)
			placed = ok & np.isfinite(preds[b]).all(-1)
			self.node_missed += int((ok & ~placed).sum())
			e = np.linalg.norm(preds[b][placed] - lab[placed], axis=-1)
			if len(e) and np.median(e) > self.wrong_px:
				self.wrong += 1
			self.errors += e.tolist()
		return [(a, int(idx[b])) for a, b in pairs]

	def Report(self):
		e = np.asarray(self.errors)
		out = dict(frames=self.frames, animals=self.animals, found=self.found, missed=self.animals - self.found,
			missed_share=round((self.animals - self.found) / max(self.animals, 1), 4),
			extra=self.extra, extra_per_frame=round(self.extra / max(self.frames, 1), 4), wrong_mouse=self.wrong,
			nodes=int(len(e)), node_missed=self.node_missed)
		if len(e):
			out.update(node_p50=round(float(np.percentile(e, 50)), 2), node_p90=round(float(np.percentile(e, 90)), 2),
				node_p99=round(float(np.percentile(e, 99)), 2), within5=round(float(np.mean(e <= 5)), 4),
				within10=round(float(np.mean(e <= 10)), 4))
		if self.groupable:
			out.update(grouped=self.grouped, groupable=self.groupable,
				grouped_share=round(self.grouped / self.groupable, 4))
		return out


def Grouped(identity_slots, members):
	"""Labelled animals whose detections the 3D put in one animal.

	identity_slots: {identity: {camera: detection slot}} for the labelled
	animals found (matched) in each camera; members (animals, cameras): the
	Tracker's detection slot per animal and camera, -1 none. Returns (grouped,
	groupable): of the labelled animals found in 2 or more cameras, how many
	the 3D grouped exactly: one 3D animal holds all of their detections, and
	none of another labelled animal's."""
	owner = {}
	for k in range(members.shape[0]):
		for c in range(members.shape[1]):
			if members[k, c] >= 0:
				owner[(c, int(members[k, c]))] = k
	grouped = groupable = 0
	for ident, slots in identity_slots.items():
		if len(slots) < 2:
			continue
		groupable += 1
		ks = {owner.get((c, s)) for c, s in slots.items()}
		if len(ks) != 1 or None in ks:
			continue
		k = ks.pop()
		# that 3D animal holds no other labelled animal's detection
		others = {(c, s) for i, sl in identity_slots.items() if i != ident for c, s in sl.items()}
		if not any((c, int(members[k, c])) in others for c in range(members.shape[1]) if members[k, c] >= 0):
			grouped += 1
	return grouped, groupable


# ---------------------------------------------------------------- the test sets

class TestSets:
	"""The test package's sets: per round, its cameras' videos, and for each
	set (frame) the labelled camera frames."""

	def __init__(self, path):
		import sleap_io as sio
		from campy import labelmerge
		self.labels = sio.load_slp(path)
		self.names = [n.name for n in self.labels.skeletons[0].nodes]
		info, self.rounds = labelmerge.VideoInfo(self.labels)
		if not any(info):
			raise EvalError("{} isn't campy-label-merge's test package (no cameras in its provenance)".format(path))
		self.sets = [] # (round, frame, {camera index: (video, LabeledFrame)})
		by_round = {}
		for v, i in zip(self.labels.videos, info):
			if i:
				by_round.setdefault(i["round"], {})[i["camera"]] = v
		for rnd, videos in sorted(by_round.items()):
			cameras = self.rounds.get(rnd, {}).get("cameras") or sorted(videos)
			frames = {}
			for c, cam in enumerate(cameras):
				v = videos.get(cam)
				if v is None:
					continue
				for lf in self.labels.find(v):
					frames.setdefault(lf.frame_idx, {})[c] = lf
			for j in sorted(frames):
				self.sets.append((rnd, j, cameras, frames[j]))

	def Why(self, rnd, j):
		s = (self.rounds.get(rnd, {}).get("sets") or {}).get(str(j)) or {}
		return s.get("why")

	def Calibration(self, rnd):
		from campy import pose3d
		text = self.rounds.get(rnd, {}).get("calibration")
		if not text:
			return None
		with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
			f.write(text)
		try:
			return pose3d.LoadCalibration(f.name, self.rounds[rnd]["cameras"])
		finally:
			os.remove(f.name)


def LabelArray(lf, names, model_names):
	"""The camera frame's labelled animals as (L, model nodes, 2), NaN where not
	labelled, and their identities."""
	cols = [names.index(n) if n in names else -1 for n in model_names]
	out, ids = [], []
	for inst in lf.user_instances:
		pts = np.asarray(inst.numpy(), float)
		out.append(np.stack([pts[c] if c >= 0 else np.full(2, np.nan) for c in cols]))
		ids.append(inst.track.name if inst.track is not None else None)
	return np.array(out).reshape(-1, len(model_names), 2), ids


# ---------------------------------------------------------------- running the models

class Pair:
	"""A centroid and instance model, run by campy-live's code (live.Prepare)
	on sets of frames of one shape."""

	def __init__(self, name, centroid, instance, live_args, n_frames, shape):
		from campy import live
		self.name = name
		argv = ["--model", "sleap", "--centroid", centroid, "--instance", instance] + list(live_args)
		self.args = live.ParseArgs(argv)
		self.model, self.host = live.Prepare(self.args, n_frames, shape, np.uint8)
		self.names = list(self.model.nodeNames or [])

	def Run(self, images):
		self.host.array[...] = images
		frames = self.model.Upload(self.host.tensor)
		r = self.model.Predict(frames)
		return r


def Evaluate(test_path, pairs, live_args=(), match_px=50.0, wrong_px=30.0, say=Say):
	"""{pair: {"all", "random", "flagged", per camera: Score.Report()}}"""
	from campy import pose3d
	tests = TestSets(test_path)
	if not tests.sets:
		raise EvalError("{} has no test sets".format(test_path))
	shapes = {lf.image.shape for _, _, _, frames in tests.sets for lf in frames.values()}
	if len(shapes) != 1:
		raise EvalError("the test frames have several sizes ({}): evaluate each rig's on its own".format(shapes))
	shape = shapes.pop()
	n_frames = max(len(cams) for _, _, cams, _ in tests.sets)
	results = {}
	for name, (centroid, instance) in pairs:
		pair = Pair(name, centroid, instance, live_args, n_frames, shape)
		names = pair.names or tests.names
		scores = {}

		def score(key):
			return scores.setdefault(key, Score(wrong_px))

		for rnd, j, cameras, frames in tests.sets:
			images = np.zeros((n_frames,) + shape, np.uint8)
			for c, lf in frames.items():
				images[c] = lf.image
			r = pair.Run(images)
			why = "random" if tests.Why(rnd, j) == "random" else "flagged"
			slots = {}
			for c, lf in frames.items():
				labels, ids = LabelArray(lf, tests.names, names)
				preds = np.asarray(r["keypoints"][c], float)
				cents = np.asarray(r["centroids"][c], float)
				for key in ("all", why, "camera " + cameras[c]):
					pairs_ = score(key).Add(labels, preds, cents, names, match_px)
				for a, b in pairs_:
					if ids[a] is not None:
						slots.setdefault(ids[a], {})[c] = b
			cams = tests.Calibration(rnd)
			if cams is not None and len(frames) >= 2:
				animals = max(1, len(slots), max((len(lf.user_instances) for lf in frames.values()), default=1))
				tracker = pose3d.Tracker(cams[:n_frames], n_animals=animals)
				kp = np.asarray(r["keypoints"], float)
				poses = tracker.Update(kp, np.asarray(r["peak_vals"], float), frame=1)
				g, n = Grouped(slots, poses.members)
				for key in ("all", why):
					score(key).grouped += g
					score(key).groupable += n
		results[name] = {k: v.Report() for k, v in scores.items()}
		say("{}: {}".format(name, Line(results[name]["all"])))
	return results


def Line(r):
	text = "{} animals, missed {} ({:.1%}), extra {:.3f}/frame, wrong mouse {}".format(r["animals"], r["missed"],
		r["missed_share"], r["extra_per_frame"], r["wrong_mouse"])
	if "node_p50" in r:
		text += ", nodes p50/p90/p99 {}/{}/{} px, within 5 px {:.1%}".format(r["node_p50"], r["node_p90"],
			r["node_p99"], r["within5"])
	if "grouped_share" in r:
		text += ", grouped across cameras {:.1%} ({}/{})".format(r["grouped_share"], r["grouped"], r["groupable"])
	return text


def Table(results):
	keys = sorted({k for r in results.values() for k in r}, key=lambda k: (k != "all", k != "random",
		k != "flagged", k))
	rows = []
	for key in keys:
		for name, r in results.items():
			if key in r:
				rows.append((key, name, r[key]))
	Say("{:<18} {:<8} {:>7} {:>8} {:>7} {:>6} {:>17} {:>7} {:>8}".format("sets", "pair", "animals", "missed", "extra/f",
		"wrong", "nodes p50/p90/p99", "<=5px", "grouped"))
	for key, name, r in rows:
		Say("{:<18} {:<8} {:>7} {:>7.1%} {:>7.3f} {:>6} {:>17} {:>7} {:>8}".format(key, name, r["animals"],
			r["missed_share"], r["extra_per_frame"], r["wrong_mouse"],
			"{}/{}/{}".format(r.get("node_p50", "-"), r.get("node_p90", "-"), r.get("node_p99", "-")),
			"{:.1%}".format(r["within5"]) if "within5" in r else "-",
			"{:.1%}".format(r["grouped_share"]) if "grouped_share" in r else "-"))


def Main(argv=None):
	p = argparse.ArgumentParser(prog="campy-model-eval", description="Measure models against a rig's labelled "
		"test sets, as campy-live runs them (python -m pydoc campy.modeleval).")
	p.add_argument("test", help="The rig's test package (campy-label-merge --test-into).")
	p.add_argument("--pair", action="append", default=[], required=True, help="NAME=CENTROID_FOLDER,INSTANCE_FOLDER; "
		"give it once per pair to compare (e.g. old and new).")
	p.add_argument("--match-px", type=float, default=50.0, help="A detection is a labelled animal's when its "
		"centroid is this close to the label's anchor node.")
	p.add_argument("--wrong-px", type=float, default=30.0, help="Wrong mouse: the median node over this far from "
		"the label's.")
	p.add_argument("--out", default="", help="Write the results here (JSON).")
	p.add_argument("--live-args", nargs=argparse.REMAINDER, default=[], help="campy-live's options for running the "
		"models (e.g. --max-instances 3 --engine off); everything after it goes to campy-live.")
	args = p.parse_args(argv)
	pairs = []
	for text in args.pair:
		name, _, folders = text.partition("=")
		parts = folders.split(",")
		if not name or len(parts) != 2:
			p.error("--pair is NAME=CENTROID_FOLDER,INSTANCE_FOLDER, not {}".format(text))
		pairs.append((name, tuple(parts)))
	try:
		results = Evaluate(args.test, pairs, args.live_args, args.match_px, args.wrong_px)
	except EvalError as e:
		print("campy-model-eval: {}".format(e), file=sys.stderr)
		return 1
	Table(results)
	if args.out:
		with open(args.out, "w") as f:
			strictjson.dump(dict(test=os.path.abspath(args.test), pairs={n: list(f_) for n, f_ in pairs},
				live_args=args.live_args, match_px=args.match_px, wrong_px=args.wrong_px, results=results), f, indent=1)
		Say("Written: {}".format(args.out))
	return 0


if __name__ == "__main__":
	sys.exit(Main())
