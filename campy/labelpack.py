"""
campy-label-pack: the sets campy-live-flag picked, as one file to correct in
LUC3D, with campy-live's predictions already on them.

	campy-label-pack ./campy-live_2026-10-22_15-46-09_flags/sample.json \\
		--recording ./rec/2026-10-22_15-46-09 --name chronicrecord_2026-10-23

It writes, into --out (default: next to sample.json), <name>/:

  <name>.pkg.slp   a SLEAP package: every camera's frame of each picked set,
                   embedded in the file (PNG, so exactly the video's pixels),
                   so it opens on any computer without the recording's videos.
                   Its videos are the cameras, its frames 0, 1, ... the picked
                   sets in order (campy's own frame numbers are in sets.csv).
                   On each frame, campy-live's detections as predicted
                   instances; with 3D, a RecordingSession (the calibration,
                   and on each set one group per animal, with its identity,
                   its 3D pose and its detections), so LUC3D shows each
                   animal's 3D projected into every camera, also the one that
                   missed it.
  sets.csv         one row per set: its frame in the package, the log, its
                   campy frame, the recording, each camera's video frame, why
                   it was picked, its flags, and whether it is a test set
  calibration.toml the cameras' calibration, under their camera names (3D)

Correcting it in LUC3D
----------------------
File > Load SLP > <name>.pkg.slp (the frames are in it; nothing else to pick).
Shift+Right goes to the next set. A set is corrected whole, every camera:

- double-click an animal's predictions: they become your labels, to drag
  where they belong (dragging a predicted point does the same);
- where a camera missed an animal, its 3D is still projected there: double-click
  that projection to make it a label in that camera, and drag the points home;
- T triangulates the set again: the projections then show where the cameras
  still disagree;
- delete detections that are no animal; the tail points can stay empty (the
  live models don't use them).
Every detection of a set is either made a label or deleted: a camera's frame
that still has a prediction left in it is not used for training (the mouse
left unlabelled there would teach the model that there is none), and
campy-label-merge says which. Then save (File > Save) as <name>.labels.slp
next to the package, and give both to campy-label-merge.

Test sets
---------
Some sets are kept to measure models, and never trained on: every set in a
minute of a recording that falls in the test share (--test-share, a third),
chosen by a hash of the recording's name and the minute, so a set is always
on the same side, in every round and whatever else is picked. Whole minutes,
not single sets, so no frame a few frames from a test frame is trained on.
sets.csv's test column says which; campy-label-merge splits them off.

It needs sleap-io (campy-live's environment has it, with sleap-nn), and
reads the videos with it. A recording made with campy's fragmentedVideo (the
default) is copied to plain MP4s in a temporary folder first, stream for
stream (OpenVideo): sleap-io can't find the frames of a fragmented one.
"""

import argparse, contextlib, csv, hashlib, io, json, os, shutil, sys, tempfile

import numpy as np

from campy import liveexport, strictjson

TEST_SHARE = 1 / 3


class PackError(Exception):
	pass


def Say(text):
	print(text, flush=True)


def IsTest(recording, time_s, share=TEST_SHARE):
	"""Whether the set at time_s (seconds since the log's first set) of this
	recording is a test set: its whole minute is, or isn't."""
	name = os.path.basename(os.path.normpath(recording))
	minute = int(time_s // 60)
	h = hashlib.sha1("{}:{}".format(name, minute).encode()).digest()
	return int.from_bytes(h[:4], "big") / 2 ** 32 < share


def ReadSample(path):
	with open(path) as f:
		sample = strictjson.load(f)
	if sample.get("kind") != "campy-live-flag sample":
		raise PackError("{} is not campy-live-flag's sample.json".format(path))
	return sample


def Skeleton(path, nodes, edges):
	"""The skeleton to label in: --skeleton's (a training package, a model
	folder, a skeleton .json), or the log's own."""
	import sleap_io as sio
	if not path:
		return sio.Skeleton(nodes=list(nodes), edges=[(nodes[a], nodes[b]) for a, b in edges or []], name="campy-live")
	if path.endswith(".slp"):
		labels = sio.load_slp(path, open_videos=False)
		return labels.skeletons[0]
	n, e = liveexport.ReadSkeleton(path)
	return sio.Skeleton(nodes=list(n), edges=[(n[a], n[b]) for a, b in e], name="labels")


def OpenVideo(path, tmp):
	"""A camera's video for sleap-io to read frames from. campy records
	fragmented MP4 by default (fragmentedVideo), which has no index of its
	frames: sleap-io then reads it as 0 frames long (PyAV) or decodes all of it
	to count them (imageio-ffmpeg, minutes per hour of video). Such a video is
	copied, stream for stream (the same frames, nothing decoded), into a plain
	MP4 in tmp first: 0.2 s for 10 minutes of 3 cameras' worth."""
	import sleap_io as sio
	if not Fragmented(path):
		return sio.Video(path)
	out = os.path.join(tmp, "plain_{}_{}".format(len(os.listdir(tmp)), os.path.basename(path)))
	import subprocess
	r = subprocess.run([FfmpegExe(), "-v", "error", "-y", "-i", path, "-map", "0", "-c", "copy", "-movflags",
		"+faststart", out], capture_output=True, text=True)
	if r.returncode != 0:
		raise PackError("{} is a fragmented MP4 and couldn't be copied to a plain one: {}".format(path,
			r.stderr.strip()[-300:]))
	return sio.Video(out)


def Fragmented(path):
	"""Whether an MP4 is fragmented: it has a movie fragment box (moof) at
	the top level. Reads only the boxes' headers."""
	import struct
	try:
		with open(path, "rb") as f:
			while True:
				head = f.read(8)
				if len(head) < 8:
					return False
				size, kind = struct.unpack(">I4s", head)
				if kind in (b"moof", b"mvex"):
					return True
				if size == 1:
					size = struct.unpack(">Q", f.read(8))[0] - 8
				elif size == 0:
					return False
				if kind == b"moov": # a fragmented file's moov holds mvex
					body = f.read(size - 8)
					return b"mvex" in body
				f.seek(size - 8, 1)
	except OSError:
		return False


def FfmpegExe():
	try:
		import imageio_ffmpeg # sleap-io's own dependency
		return imageio_ffmpeg.get_ffmpeg_exe()
	except Exception:
		return shutil.which("ffmpeg") or "ffmpeg"


def Image(video, index):
	# One frame, as the training images are: one channel when the camera's is grey
	img = np.asarray(video[int(index)])
	if img.ndim == 3 and img.shape[2] == 3 and np.array_equal(img[..., 0], img[..., 1]) \
			and np.array_equal(img[..., 0], img[..., 2]):
		img = img[..., :1]
	return img


def Int32Videos(f):
	"""LUC3D (ecbf92f3) reads an embedded video's frame_numbers and its width,
	height, frames and channels as JavaScript BigInts when they are 64-bit, as
	sleap-io writes them, and then fails (\"Cannot mix BigInt and other
	types\"): write them as 32-bit. sleap-io reads either."""
	for key in f:
		if not (key.startswith("video") and key[5:].isdigit()):
			continue
		g = f[key]
		if "frame_numbers" in g:
			numbers = g["frame_numbers"][:]
			del g["frame_numbers"]
			g.create_dataset("frame_numbers", data=numbers.astype(np.int32))
		if "video" in g:
			for name, value in list(g["video"].attrs.items()):
				if isinstance(value, np.integer):
					g["video"].attrs[name] = np.int32(value)


def Pack(sample_path, recordings, out=None, name=None, skeleton=None, calibration=None, test_share=TEST_SHARE,
		force=False):
	"""Writes the package (the module's doc); returns a dict of what it wrote."""
	import h5py, imageio.v3 as iio, sleap_io as sio

	sample = ReadSample(sample_path)
	logs = [lg["path"] for lg in sample["logs"]]
	if len(recordings) != len(logs):
		raise PackError("the sample has {} log{} ({}): give one --recording for each, in that order".format(
			len(logs), "s" * (len(logs) != 1), ", ".join(os.path.basename(p) for p in logs)))
	sets = sample["sets"]
	if not sets:
		raise PackError("the sample has no sets")
	name = name or os.path.basename(os.path.normpath(recordings[0]))
	folder = os.path.join(out or os.path.dirname(os.path.abspath(sample_path)), name)
	pkg = os.path.join(folder, name + ".pkg.slp")
	if os.path.exists(pkg) and not force:
		raise PackError("already there: {}. --force replaces it.".format(pkg))

	# Each log: its rows, cameras, nodes, calibration; each recording's videos
	opened = []
	for lg_path, rec in zip(logs, recordings):
		if not os.path.isfile(lg_path):
			alt = os.path.join(os.path.dirname(os.path.abspath(sample_path)), os.pardir, os.path.basename(lg_path))
			if os.path.isfile(alt):
				lg_path = os.path.normpath(alt)
		try:
			rows, summary = liveexport.ReadLog(lg_path)
		except liveexport.ExportError as e:
			raise PackError(str(e))
		names = (summary or {}).get("cameras")
		if not names:
			raise PackError("{} has no summary saying its cameras".format(lg_path))
		try:
			cams = [liveexport.Camera(os.path.abspath(rec), n) for n in names]
		except liveexport.ExportError as e:
			raise PackError(str(e))
		calib = None
		if "points3d" in rows.dtype.names:
			cal, cal_names, _ = liveexport.SummaryCalibration(summary)
			cal = calibration or cal
			if cal and os.path.isfile(cal):
				calib = liveexport.ReadCalibration(cal, cal_names or names, names)
			else:
				Say("{}: no calibration on this computer ({}): the package has the 2D only, no 3D to correct "
					"against. Give it with --calibration.".format(os.path.basename(lg_path), cal or "none"))
		opened.append(dict(path=lg_path, rows=rows, summary=summary or {}, names=names, cams=cams, calib=calib,
			recording=os.path.abspath(rec)))
	names = opened[0]["names"]
	for o in opened[1:]:
		if o["names"] != names:
			raise PackError("the logs' cameras differ ({} against {}): pack each rig on its own".format(
				", ".join(o["names"]), ", ".join(names)))
	log_nodes = opened[0]["summary"].get("nodes")
	if not log_nodes:
		raise PackError("{}'s summary has no node names".format(opened[0]["path"]))
	skel = Skeleton(skeleton, log_nodes, opened[0]["summary"].get("edges"))
	target = [n.name for n in skel.nodes]
	where = [target.index(n) if n in target else -1 for n in log_nodes] # each log node's place in the skeleton
	if all(w < 0 for w in where):
		raise PackError("none of the log's nodes ({}) is in the skeleton ({})".format(", ".join(log_nodes),
			", ".join(target)))
	calib = next((o["calib"] for o in opened if o["calib"]), None)
	with3d = calib is not None

	tmp = tempfile.mkdtemp(prefix="campy-label-pack-")
	try:
		videos_src = [[OpenVideo(os.path.join(c.folder, c.video), tmp) for c in o["cams"]] for o in opened]
		images = [[] for _ in names]
		table, skipped = [], 0
		order = sorted(range(len(sets)), key=lambda i: (logs.index(sets[i]["log"]), sets[i]["row"]))
		kept = []
		for i in order:
			s = sets[i]
			o = opened[logs.index(s["log"])]
			frame = int(s["frame"])
			vf = [int(c.Index([frame])[0]) for c in o["cams"]]
			if min(vf) < 0:
				skipped += 1
				Say("Set at campy frame {} left out: {}'s video lacks it.".format(frame,
					", ".join(n for n, v in zip(names, vf) if v < 0)))
				continue
			j = len(kept)
			for c in range(len(names)):
				p = os.path.join(tmp, "c{}_{:06d}.png".format(c, j))
				img = Image(videos_src[logs.index(s["log"])][c], vf[c])
				iio.imwrite(p, img[..., 0] if img.shape[-1] == 1 else img)
				images[c].append(p)
			test = IsTest(o["recording"], float(s["time_s"]), test_share)
			kept.append((s, o, int(s["row"]), test))
			table.append(dict(package_frame=j, log=os.path.basename(o["path"]), campy_frame=frame,
				recording=os.path.basename(o["recording"]), time_s=s["time_s"], why=s["why"],
				flags=";".join(sorted({"{}:{}".format(f["reason"], f["camera"] or "any") for f in s["flags"]})),
				test=int(test), **{"video_frame_{}".format(n): v for n, v in zip(names, vf)}))
		if not kept:
			raise PackError("none of the sets is on every camera's video")

		videos = [sio.Video(filename=images[c]) for c in range(len(names))]
		animals = max((o["rows"].dtype["members"].shape[0] for o in opened if "members" in o["rows"].dtype.names),
			default=0)
		identities = [sio.Identity(name=liveexport.AnimalName(k)) for k in range(animals)]
		tracks = [sio.Track(name=liveexport.AnimalName(k)) for k in range(animals)]
		session, sio_cams = None, None
		if with3d:
			sio_cams = [sio.Camera(name=c["name"], matrix=np.asarray(c["matrix"], float),
				dist=np.asarray(c.get("distortions", [0] * 5), float), size=tuple(c["size"]) if c.get("size") else None,
				rvec=np.asarray(c["rotation"], float), tvec=np.asarray(c["translation"], float)) for c in calib]
			session = sio.RecordingSession(camera_group=sio.CameraGroup(cameras=sio_cams))
			for v, cam in zip(videos, sio_cams):
				session.add_video(v, cam)

		def points(xy):
			out = np.full((len(target), 2), np.nan)
			for a, b in enumerate(where):
				if b >= 0:
					out[b] = xy[a]
			return out

		lfs = []
		for j, (s, o, row, test) in enumerate(kept):
			r = np.asarray(o["rows"][row])
			by_cam = []
			for c in range(len(names)):
				insts = {}
				for slot in range(r["keypoints"].shape[1]):
					xy = r["keypoints"][c, slot].astype(float)
					if np.isnan(xy).all():
						continue
					score = r["peak_vals"][c, slot].astype(float)
					sc = np.full(len(target), np.nan)
					for a, b in enumerate(where):
						if b >= 0:
							sc[b] = score[a]
					inst = sio.PredictedInstance.from_numpy(points(xy), skeleton=skel, point_scores=np.nan_to_num(sc),
						score=float(np.nan_to_num(r["centroid_vals"][c, slot])))
					insts[slot] = inst
				lf = sio.LabeledFrame(video=videos[c], frame_idx=j, instances=list(insts.values()))
				lfs.append(lf)
				by_cam.append((lf, insts))
			if with3d and "members" in r.dtype.names:
				groups = []
				for k in range(r["members"].shape[0]):
					members = {sio_cams[c]: by_cam[c][1][int(m)] for c, m in enumerate(r["members"][k])
						if m >= 0 and int(m) in by_cam[c][1]}
					if not members:
						continue
					for inst in members.values():
						inst.track = tracks[k]
					X = np.full((len(target), 3), np.nan)
					for a, b in enumerate(where):
						if b >= 0:
							X[b] = r["points3d"][k, a]
					groups.append(sio.InstanceGroup(instance_by_camera=members, identity=identities[k],
						instance_3d=sio.PredictedInstance3D(points=X, skeleton=skel),
						metadata={"lucid": {"identityId": k}}))
				session.frame_groups[j] = sio.FrameGroup(frame_idx=j, instance_groups=groups,
					labeled_frame_by_camera={sio_cams[c]: by_cam[c][0] for c in range(len(names))})

		cal_text = None
		if with3d:
			liveexport.WriteCalibration(os.path.join(tmp, "calibration.toml"), calib)
			with open(os.path.join(tmp, "calibration.toml")) as f:
				cal_text = f.read()
		provenance = {"campy-label-pack": dict(sample=os.path.abspath(sample_path), test_share=test_share, name=name,
			cameras=list(names), calibration=cal_text, sets=[{k: v for k, v in t.items() if k != "flags"}
			for t in table])}
		labels = sio.Labels(labeled_frames=lfs, videos=videos, skeletons=[skel], tracks=tracks, identities=identities,
			sessions=[session] if session else [], provenance=provenance)
		os.makedirs(folder, exist_ok=True)
		if os.path.exists(pkg):
			os.remove(pkg)
		# sleap-io 0.9.3 drops a session's videos when it embeds them (it serializes the
		# session against the embedded copies, which it doesn't know): save it plain too,
		# and copy its session into the package. The videos are in the same order in both.
		plain = os.path.join(tmp, "plain.slp")
		labels.save(plain, embed=False, verbose=False)
		with contextlib.redirect_stdout(io.StringIO()): # its print of that, for each camera
			labels.save(pkg, embed="all", verbose=False)
		with h5py.File(plain, "r") as a, h5py.File(pkg, "r+") as b:
			for key in ("sessions_json", "session_data"):
				if key in b:
					del b[key]
				if key in a:
					a.copy(a[key], b, key)
			b["metadata"].attrs["format_id"] = max(float(b["metadata"].attrs["format_id"]),
				float(a["metadata"].attrs["format_id"]))
			Int32Videos(b)
		with open(os.path.join(folder, "sets.csv"), "w", newline="") as f:
			w = csv.DictWriter(f, fieldnames=list(table[0]))
			w.writeheader()
			w.writerows(table)
		if with3d:
			liveexport.WriteCalibration(os.path.join(folder, "calibration.toml"), calib)
	finally:
		shutil.rmtree(tmp, ignore_errors=True)
	return dict(package=pkg, folder=folder, sets=len(kept), test=sum(t for *_, t in kept), skipped=skipped,
		cameras=len(names), with3d=with3d)


def Main(argv=None):
	p = argparse.ArgumentParser(prog="campy-label-pack", description="The sets campy-live-flag picked, as one "
		"file to correct in LUC3D, with campy-live's predictions on them (python -m pydoc campy.labelpack).")
	p.add_argument("sample", help="campy-live-flag's sample.json")
	p.add_argument("--recording", action="append", default=[], help="The recording's folder (with a folder per "
		"camera), once per log in the sample, in its order.")
	p.add_argument("--out", default="", help="Where the package's folder goes (default: next to sample.json).")
	p.add_argument("--name", default="", help="The package's name (default: the first recording's).")
	p.add_argument("--skeleton", default="", help="The skeleton to label in: the training package (.slp), a model "
		"folder or a skeleton .json; the log's nodes go to the nodes of the same name (default: the log's).")
	p.add_argument("--calibration", default="", help="The calibration.toml, when the log's isn't on this computer.")
	p.add_argument("--test-share", type=float, default=TEST_SHARE, help="The share of a recording's minutes kept "
		"as test sets.")
	p.add_argument("--force", action="store_true", help="Replace a package already there.")
	args = p.parse_args(argv)
	try:
		import sleap_io
	except ImportError:
		print("campy-label-pack needs sleap-io: run it in campy-live's environment (sleap-nn has it), or "
			"pip install sleap-io.", file=sys.stderr)
		return 1
	try:
		out = Pack(args.sample, args.recording, out=args.out or None, name=args.name or None,
			skeleton=args.skeleton or None, calibration=args.calibration or None, test_share=args.test_share,
			force=args.force)
	except (PackError, liveexport.ExportError) as e:
		print("campy-label-pack: {}".format(e), file=sys.stderr)
		return 1
	Say("{} sets ({} of them test sets), {} cameras each{}: {}".format(out["sets"], out["test"], out["cameras"],
		", with 3D" if out["with3d"] else "", out["package"]))
	Say("  LUC3D: File > Load SLP > {}; correct each set, then save as {}.labels.slp next to it.".format(
		os.path.basename(out["package"]), os.path.basename(out["package"])[:-len(".pkg.slp")]))
	return 0


if __name__ == "__main__":
	sys.exit(Main())
