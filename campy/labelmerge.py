"""
campy-label-merge: the sets corrected in LUC3D, as training and test packages
for the next model.

	campy-label-merge chronicrecord_2026-10-23.pkg.slp chronicrecord_2026-10-23.labels.slp \\
		--skeleton labels.v015_threeFemales_n=1000.pkg.slp

The first file is campy-label-pack's package (its frames are in it), the
second what LUC3D saved after correcting it (the labels; LUC3D saves no
frames, so they are matched to the package's by camera and frame). It writes,
next to the labels (or into --out):

  <name>.train.pkg.slp   the corrected camera frames that aren't test sets, with
                         their frames embedded, in --skeleton's nodes
  <name>.test.pkg.slp    the test sets' (campy-label-pack chose them by whole
                         minutes of the recording), never to be trained on
  <name>.merge.csv       each camera frame: its set, camera, campy frame,
                         train or test, and what was done with it

A camera frame is taken when it has labels and no prediction is left in it:
correcting a set means making each prediction a label or deleting it, and a
mouse left unlabelled in a training frame would teach the model there is none
there. Frames with predictions left are listed ("not finished") and left out.
A camera frame whose predictions were all deleted, with no labels, could be a
frame with no animal or one LUC3D dropped: it is left out too, unless
--keep-emptied (then it is a frame with no animals).

Points the labeller marked hidden (LUC3D's occluded, and the points LUC3D
fills in when a prediction is made a label) are left out: not visible, as
SLEAP trains on them. Nodes the labels don't have (the tail, with a 12-node
model's predictions) are empty.

Adding them to the training set and the test set
-------------------------------------------------
--train-into <training package> --train-out <new package> writes the training
package with the new frames added (on the cluster: a 10,000-frame package is
a few GB). --skeleton defaults to the training package's, so run it once per
package (the 15-node one for the centroid model, the 12-node one for the
centered-instance model): each gets the nodes it has.
--test-into <rig's test package> adds the test frames to that rig's test set
(made if it isn't there yet). The test set only grows: a test frame is never
moved to training.

It needs sleap-io (campy-live's environment has it).
"""

import argparse, contextlib, csv, io, os, sys

import numpy as np


KEY = "campy-label-merge" # the provenance of its packages: each video's camera and round, each round's calibration


class MergeError(Exception):
	pass


def VideoInfo(labels):
	"""Each video's camera and round (dicts, None for a video campy-label-merge
	didn't add), and the rounds, from a package's provenance."""
	prov = (labels.provenance or {}).get(KEY) or {}
	videos = list(prov.get("videos") or [])
	videos += [None] * (len(labels.videos) - len(videos))
	return videos[:len(labels.videos)], dict(prov.get("rounds") or {})


def Say(text):
	print(text, flush=True)


def CameraOf(labels, video):
	# The camera of a video, by the labels' session; None without one
	for s in labels.sessions:
		cam = s.get_camera(video)
		if cam is not None:
			return cam.name
	return None


def MatchVideos(package, labels):
	"""For each of the labels' videos, the package's video of the same camera."""
	if len(labels.videos) != len(package.videos):
		raise MergeError("the labels have {} videos, the package {}: are they the same package's?".format(
			len(labels.videos), len(package.videos)))
	pk = {CameraOf(package, v): v for v in package.videos}
	out = {}
	for i, v in enumerate(labels.videos):
		cam = CameraOf(labels, v)
		if cam is not None and cam in pk and None not in pk:
			out[v] = pk[cam]
		else: # no session to say: by order, as LUC3D keeps them
			out[v] = package.videos[i]
	return out


def SetTable(package):
	prov = (package.provenance or {}).get("campy-label-pack")
	if not prov:
		raise MergeError("the package wasn't made by campy-label-pack (its provenance has no sets)")
	return {int(s["package_frame"]): s for s in prov["sets"]}


def Convert(inst, skeleton, tracks):
	"""A labelled instance in skeleton's nodes (by name): the points the
	labeller placed and left visible; the rest empty. Its track (the animal's
	identity, "animal k") is kept, from tracks (name: Track)."""
	import sleap_io as sio
	names = [n.name for n in inst.skeleton.nodes]
	pts = inst.points
	xy = np.asarray(pts["xy"], float)
	vis = np.asarray(pts["visible"], bool) & np.isfinite(xy).all(-1)
	out = np.full((len(skeleton.nodes), 2), np.nan)
	for i, node in enumerate(skeleton.nodes):
		if node.name in names:
			j = names.index(node.name)
			if vis[j]:
				out[i] = xy[j]
	track = None
	if inst.track is not None:
		track = tracks.setdefault(inst.track.name, sio.Track(name=inst.track.name))
	return sio.Instance.from_numpy(out, skeleton=skeleton, track=track)


def Merge(package_path, labels_path, skeleton=None, out=None, keep_emptied=False, train_into=None,
		train_out=None, test_into=None):
	import sleap_io as sio
	package = sio.load_slp(package_path)
	labels = sio.load_slp(labels_path, open_videos=False)
	sets = SetTable(package)
	videos = MatchVideos(package, labels)
	base = sio.load_slp(train_into) if train_into else None
	if skeleton:
		skel = sio.load_slp(skeleton, open_videos=False).skeletons[0] if skeleton.endswith(".slp") else None
		if skel is None:
			from campy import liveexport
			n, e = liveexport.ReadSkeleton(skeleton)
			skel = sio.Skeleton(nodes=list(n), edges=[(n[a], n[b]) for a, b in e])
	elif base is not None:
		skel = base.skeletons[0]
	else:
		skel = package.skeletons[0]
	if base is not None and [n.name for n in base.skeletons[0].nodes] != [n.name for n in skel.nodes]:
		raise MergeError("--skeleton's nodes differ from the training package's: leave --skeleton out with "
			"--train-into")
	have = {n.name for n in labels.skeletons[0].nodes}
	if not have & {n.name for n in skel.nodes}:
		raise MergeError("the labels' nodes ({}) and the skeleton's ({}) have none in common".format(
			", ".join(sorted(have)), ", ".join(n.name for n in skel.nodes)))

	# What the package offered on each camera frame, and what came back
	offered = {(v, lf.frame_idx): len(lf.instances) for v in package.videos for lf in package.find(v)}
	back = {}
	for lf in labels:
		back[(videos[lf.video], lf.frame_idx)] = lf
	train, test, table, tracks = [], [], [], {}
	counts = dict(taken=0, not_finished=0, untouched=0, emptied=0)
	for v in package.videos:
		cam = CameraOf(package, v) or "video {}".format(package.videos.index(v))
		for j in sorted(sets):
			s = sets[j]
			lf = back.get((v, j))
			user = list(lf.user_instances) if lf is not None else []
			pred = list(lf.predicted_instances) if lf is not None else []
			if user and not pred:
				what = "taken"
			elif user:
				what = "not_finished"
			elif pred or not offered.get((v, j)):
				what = "untouched"
			else:
				what = "emptied"
			counts[what] += 1
			take = what == "taken" or (what == "emptied" and keep_emptied)
			if take:
				new = sio.LabeledFrame(video=v, frame_idx=j, instances=[Convert(i, skel, tracks) for i in user])
				(test if int(s.get("test", 0)) else train).append(new)
			table.append(dict(package_frame=j, camera=cam, campy_frame=s.get("campy_frame"),
				recording=s.get("recording"), why=s.get("why"), split="test" if int(s.get("test", 0)) else "train",
				result=what, used=int(take), labels=len(user), predictions_left=len(pred)))

	name = os.path.basename(package_path)
	for ext in (".pkg.slp", ".slp"):
		if name.endswith(ext):
			name = name[:-len(ext)]
			break
	folder = out or os.path.dirname(os.path.abspath(labels_path))
	os.makedirs(folder, exist_ok=True)
	written = {}

	prov = package.provenance["campy-label-pack"]
	round_name = prov.get("name") or name

	def save(frames, path):
		used = [v for v in package.videos if any(f.video is v for f in frames)]
		new = sio.Labels(labeled_frames=frames, videos=used, skeletons=[skel], tracks=list(tracks.values()),
			provenance={KEY: dict(videos=[dict(camera=CameraOf(package, v), round=round_name) for v in used],
				rounds={round_name: dict(package=os.path.abspath(package_path), labels=os.path.abspath(labels_path),
				cameras=prov.get("cameras"), calibration=prov.get("calibration"),
				sets={str(j): dict(campy_frame=t.get("campy_frame"), recording=t.get("recording"), why=t.get("why"),
				test=int(t.get("test", 0))) for j, t in sets.items()})})})
		with contextlib.redirect_stdout(io.StringIO()):
			new.save(path, embed="user", verbose=False)
		return path

	if train:
		written["train"] = save(train, os.path.join(folder, name + ".train.pkg.slp"))
	if test:
		written["test"] = save(test, os.path.join(folder, name + ".test.pkg.slp"))
	with open(os.path.join(folder, name + ".merge.csv"), "w", newline="") as f:
		w = csv.DictWriter(f, fieldnames=list(table[0]))
		w.writeheader()
		w.writerows(table)
	written["table"] = os.path.join(folder, name + ".merge.csv")

	if base is not None and train:
		if not train_out:
			raise MergeError("--train-into needs --train-out, the new training package")
		written["train_into"] = Append(base, written["train"], train_out)
	if test_into and test:
		old = sio.load_slp(test_into) if os.path.isfile(test_into) else None
		if old is not None and [n.name for n in old.skeletons[0].nodes] != [n.name for n in skel.nodes]:
			raise MergeError("{}'s nodes differ from --skeleton's".format(test_into))
		written["test_into"] = Append(old, written["test"], test_into)
	return dict(counts=counts, train=len(train), test=len(test), written=written)


def Append(base, new_path, out_path):
	"""base (Labels, or None) with new_path's frames added, saved with every
	labelled frame embedded at out_path (written beside it first, then moved)."""
	import sleap_io as sio
	new = sio.load_slp(new_path)
	new_videos, new_rounds = VideoInfo(new)
	if base is None:
		frames, videos, skel = list(new.labeled_frames), list(new.videos), new.skeletons[0]
		tracks, provenance, info, rounds = list(new.tracks), {}, new_videos, new_rounds
	else:
		skel = base.skeletons[0]
		if [n.name for n in skel.nodes] != [n.name for n in new.skeletons[0].nodes]:
			raise MergeError("the new frames' nodes differ from {}'s".format(out_path))
		tracks = {t.name: t for t in base.tracks}
		frames = list(base.labeled_frames) + [sio.LabeledFrame(video=lf.video, frame_idx=lf.frame_idx,
			instances=[sio.Instance.from_numpy(i.numpy(), skeleton=skel, track=None if i.track is None else
			tracks.setdefault(i.track.name, sio.Track(name=i.track.name))) for i in lf.user_instances]) for lf in new]
		videos = list(base.videos) + list(new.videos)
		tracks = list(tracks.values())
		base_videos, rounds = VideoInfo(base)
		info = base_videos + new_videos
		rounds.update(new_rounds)
		provenance = {k: v for k, v in (base.provenance or {}).items() if k != KEY}
	provenance[KEY] = dict(videos=info, rounds=rounds)
	os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
	tmp = out_path[:-len(".slp")] + ".writing.slp" if out_path.endswith(".slp") else out_path + ".writing.slp"
	with contextlib.redirect_stdout(io.StringIO()):
		sio.Labels(labeled_frames=frames, videos=videos, skeletons=[skel], tracks=tracks,
			provenance=provenance).save(tmp, embed="user", verbose=False)
	check = sio.load_slp(tmp)
	if len(check) != len(frames):
		os.remove(tmp)
		raise MergeError("{} came out with {} frames, not {}".format(out_path, len(check), len(frames)))
	os.replace(tmp, out_path)
	return out_path


def Main(argv=None):
	p = argparse.ArgumentParser(prog="campy-label-merge", description="The sets corrected in LUC3D, as training "
		"and test packages (python -m pydoc campy.labelmerge).")
	p.add_argument("package", help="campy-label-pack's <name>.pkg.slp")
	p.add_argument("labels", help="What LUC3D saved after correcting it")
	p.add_argument("--skeleton", default="", help="The nodes to write: a training package (.slp) or a model "
		"folder (default: --train-into's, else the package's).")
	p.add_argument("--out", default="", help="Folder for the files (default: the labels').")
	p.add_argument("--keep-emptied", action="store_true", help="Take camera frames whose predictions were all "
		"deleted as frames with no animals.")
	p.add_argument("--train-into", default="", help="A training package to add the new training frames to.")
	p.add_argument("--train-out", default="", help="With --train-into: the new training package.")
	p.add_argument("--test-into", default="", help="The rig's test package to add the test frames to (made if "
		"it isn't there).")
	args = p.parse_args(argv)
	try:
		import sleap_io
	except ImportError:
		print("campy-label-merge needs sleap-io: run it in campy-live's environment, or pip install sleap-io.",
			file=sys.stderr)
		return 1
	try:
		r = Merge(args.package, args.labels, skeleton=args.skeleton or None, out=args.out or None,
			keep_emptied=args.keep_emptied, train_into=args.train_into or None, train_out=args.train_out or None,
			test_into=args.test_into or None)
	except MergeError as e:
		print("campy-label-merge: {}".format(e), file=sys.stderr)
		return 1
	c = r["counts"]
	Say("Camera frames: {} taken ({} to train, {} to test), {} not finished (predictions left), {} untouched, "
		"{} emptied{}.".format(c["taken"], r["train"], r["test"], c["not_finished"], c["untouched"], c["emptied"],
		" (taken as no animals)" if args.keep_emptied else " (left out; --keep-emptied takes them)"))
	for key, label in (("train", "training frames"), ("test", "test frames"), ("train_into", "new training package"),
			("test_into", "rig's test set"), ("table", "what was done with each")):
		if key in r["written"]:
			Say("  {}: {}".format(label, r["written"][key]))
	return 0


if __name__ == "__main__":
	sys.exit(Main())
