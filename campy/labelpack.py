"""
campy-label-pack: the sets campy-live-flag picked, as one file to correct in
LUC3D, with campy-live's predictions already on them.

	campy-label-pack ./campy-live_2026-10-22_15-46-09_flags/sample.json \\
		--recording ./rec/2026-10-22_15-46-09 --name chronicrecord_2026-10-23 \\
		--test-package tests/chronicrecord.test.pkg.slp

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
chosen by a hash of the recording's name and the minute. The minutes are the
recording's own (campy's frame number over its frame rate, SplitClock), not
the log's, so a set is on the same side in every log of the recording (a
campy-live started late or again, a campy-live.part2.npy), in every round,
whatever else is picked. A replay (emulated cameras) counts the replayed
clip's minutes, keyed on the clip, so a clip is split the same way in every
replay of it. Whole minutes, and a set within --guard-s (3 s) of a test
minute is a test set too, so no training frame is within 3 s of a test
frame. With --test-package (the rig's test set), a set within 3 s of one of
its frames is a test set too; with --train-package, a set that would be a
test set but is within 3 s of a frame already trained on is left out. These
keep packages made before this split (which counted from the log's first
set) apart from new ones. sets.csv's test column says which side, split_source
and split_s the clock it was decided on, and split why; campy-label-merge
splits them off.

The package's name is its round's name in the training and test packages
(default: the recording's, and the start of a hash of sample.json, so
packing one recording's flags twice makes two rounds).

It needs sleap-io (campy-live's environment has it, with sleap-nn), and
reads the videos with it. A recording made with campy's fragmentedVideo (the
default) is copied to plain MP4s in a temporary folder first, stream for
stream (OpenVideo): sleap-io can't find the frames of a fragmented one.
"""

import argparse, contextlib, csv, hashlib, io, json, os, shutil, sys, tempfile

import numpy as np

from campy import liveexport, strictjson

TEST_SHARE = 1 / 3
GUARD_S = 3.0 # no training set this close to a test minute, or to a test frame


class PackError(Exception):
	pass


def Say(text):
	print(text, flush=True)


def IsTest(source, seconds, share=TEST_SHARE):
	"""Whether the minute of source's clock that seconds falls in is a test
	minute: a hash of the source's name (a recording folder's name, or a
	replayed clip's, SplitClock) and the minute, so it is the same on every
	computer and in every round."""
	name = os.path.basename(os.path.normpath(source))
	minute = int(seconds // 60)
	h = hashlib.sha1("{}:{}".format(name, minute).encode()).digest()
	return int.from_bytes(h[:4], "big") / 2 ** 32 < share


class SplitClock:
	"""
	The clock the test split counts minutes on: the recording's own, campy's
	frame number over its frame rate (frame 1 is 0 s), the same in every log
	of the recording, whenever campy-live started. A recording of emulated
	cameras (cameraMake emu) replays a clip: its clock is the clip's, keyed on
	the clip (its folder's name and file name), and when the clip's
	frametimes.npy is beside it, counted from the clip's start again on each
	loop. So a clip replayed into any number of recordings has its frames on
	the same side every time.
	"""

	def __init__(self, recording, camera, summary):
		from campy.align import ReadMetadata
		path = os.path.join(recording, camera, "metadata.csv")
		meta = ReadMetadata(path) if os.path.isfile(path) else {}
		try:
			self.rate = float(meta.get("frameRate") or (summary or {}).get("frame_rate") or 0)
		except (TypeError, ValueError):
			self.rate = 0.0
		if not self.rate > 0:
			raise PackError("{}: no frame rate (metadata.csv's frameRate, or the log's summary), which the test "
				"split counts minutes by".format(os.path.join(recording, camera)))
		self.source = os.path.basename(os.path.normpath(recording))
		self.clip_frames = None
		if str(meta.get("cameraMake", "")).lower() == "emu" and len(str(meta.get("videoFilename", ""))) > 3:
			clip = str(meta["videoFilename"])[3:] # emu.ClipPath: "emuclip.mp4" plays clip.mp4
			root = str(meta.get("videoFolderRoot") or meta.get("videoFolder") or "")
			self.source = "clip {} {}".format(os.path.basename(os.path.normpath(root)) if root else "", clip).replace(
				"/", "_").replace("\\", "_")
			times = os.path.join(root, str(meta.get("cameraName") or camera), "frametimes.npy")
			if root and os.path.isfile(times):
				try:
					self.clip_frames = int(np.load(times, mmap_mode="r").shape[-1]) or None
				except (OSError, ValueError):
					self.clip_frames = None

	def Seconds(self, frame):
		n = int(frame) - 1
		if self.clip_frames:
			n %= self.clip_frames
		return n / self.rate

	def Same(self, other):
		return (self.source, self.rate, self.clip_frames) == (other.source, other.rate, other.clip_frames)


def TestSide(source, seconds, share=TEST_SHARE, guard_s=GUARD_S, tested=None, trained=None):
	"""
	Which side a set is on: (True, why) a test set, (False, why) a training
	set, (None, why) left out, so that no training set is within guard_s of a
	test set. A set in a test minute is a test set, unless a frame already
	trained on (trained: {source: sorted seconds}) is within guard_s: then
	it is left out. A set in a training minute is a training set, unless it
	is within guard_s of a test minute, or of a frame already in the rig's
	test package (tested, the same): then it is left out.
	"""
	def near(frames):
		t = np.asarray((frames or {}).get(source, ()), float)
		return len(t) > 0 and bool(np.any(np.abs(t - seconds) <= guard_s))

	if IsTest(source, seconds, share):
		if near(trained):
			return None, "test minute, within {:g} s of a frame already trained on".format(guard_s)
		return True, "test minute"
	if guard_s > 0 and (IsTest(source, seconds - guard_s, share) or IsTest(source, seconds + guard_s, share)):
		return None, "training minute, within {:g} s of a test minute".format(guard_s)
	if near(tested):
		return None, "training minute, within {:g} s of a frame in the test package".format(guard_s)
	return False, "training minute"


def PackageFrames(path, test, rates):
	"""The frames of a training or test package made by campy-label-merge, as
	{split source: sorted seconds}: test, the test sets (else the training
	ones). Frames packed before the split had its own clock have no source:
	their recording and campy frame stand in, at that recording's rate
	(rates: {recording name: frame rate}, the recordings being packed)."""
	import sleap_io as sio
	from campy import labelmerge
	labels = sio.load_slp(path, open_videos=False)
	info, rounds = labelmerge.VideoInfo(labels)
	out = {}
	for lf in labels.labeled_frames:
		i = info[labels.videos.index(lf.video)]
		if not i:
			continue
		s = ((rounds.get(i["round"]) or {}).get("sets") or {}).get(str(lf.frame_idx))
		if not s or bool(int(s.get("test", 0))) != test:
			continue
		if s.get("split_source") is not None and s.get("split_s") is not None:
			out.setdefault(str(s["split_source"]), set()).add(float(s["split_s"]))
		elif s.get("recording") in rates and s.get("campy_frame") is not None:
			out.setdefault(str(s["recording"]), set()).add((int(s["campy_frame"]) - 1) / rates[s["recording"]])
	return {k: sorted(v) for k, v in out.items()}


def RoundName(sample_path, recording):
	"""A round's default name: the recording's, and the start of a hash of
	the sample, so two rounds of one recording have their own names, and
	packing the same sample again gives the same one."""
	with open(sample_path, "rb") as f:
		digest = hashlib.sha1(f.read()).hexdigest()[:6]
	return "{}_{}".format(os.path.basename(os.path.normpath(recording)), digest)


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
		force=False, guard_s=GUARD_S, test_package=None, train_package=None, old_split=False):
	"""Writes the package (the module's doc); returns a dict of what it wrote.
	test_package, train_package: the rig's (campy-label-merge's), whose
	frames' neighbours are kept off the other side. old_split: the split of
	campy before 2026-11, by seconds since the log's first set and no guard,
	only to pack again a sample packed then."""
	import h5py, imageio.v3 as iio, sleap_io as sio

	sample = ReadSample(sample_path)
	logs = [lg["path"] for lg in sample["logs"]]
	if len(recordings) != len(logs):
		raise PackError("the sample has {} log{} ({}): give one --recording for each, in that order".format(
			len(logs), "s" * (len(logs) != 1), ", ".join(os.path.basename(p) for p in logs)))
	sets = sample["sets"]
	if not sets:
		raise PackError("the sample has no sets")
	name = name or RoundName(sample_path, recordings[0])
	folder = os.path.join(out or os.path.dirname(os.path.abspath(sample_path)), name)
	pkg = os.path.join(folder, name + ".pkg.slp")
	if os.path.exists(pkg) and not force:
		raise PackError("already there: {}. --force replaces it.".format(pkg))

	# Each log: its rows, cameras, nodes, calibration; each recording's videos
	opened = []
	for lg_path, rec in zip(logs, recordings):
		if not os.path.isfile(lg_path):
			# On another computer: the log beside the flags folder, as campy-live-flag puts it.
			# Only one log can be there by that name (campy-session names each campy-live.npy)
			alt = os.path.join(os.path.dirname(os.path.abspath(sample_path)), os.pardir, os.path.basename(lg_path))
			if not os.path.isfile(alt):
				raise PackError("no log at {} (nor at {})".format(lg_path, os.path.normpath(alt)))
			if sum(os.path.basename(p) == os.path.basename(lg_path) for p in logs) > 1:
				raise PackError("no log at {}, and the sample's other logs have its name ({}), so the one beside "
					"the flags folder can't be told apart: put each log back where the sample says, or run "
					"campy-live-flag again on this computer".format(lg_path, os.path.basename(lg_path)))
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
			# Is it this recording's log? Its frames' host times are the videos' (liveexport's check)
			liveexport.Placement(rows, cams)
		except liveexport.ExportError as e:
			raise PackError("{} and --recording {}: {}".format(os.path.basename(lg_path), rec, e))
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
			recording=os.path.abspath(rec), clock=SplitClock(os.path.abspath(rec), names[0], summary)))
	if len({os.path.realpath(o["path"]) for o in opened}) < len(opened):
		raise PackError("two of the sample's logs are the same file here: {}".format(", ".join(o["path"]
			for o in opened)))
	names = opened[0]["names"]
	for o in opened[1:]:
		if o["names"] != names:
			raise PackError("the logs' cameras differ ({} against {}): pack each rig on its own".format(
				", ".join(o["names"]), ", ".join(names)))
	log_nodes = opened[0]["summary"].get("nodes")
	if not log_nodes:
		raise PackError("{}'s summary has no node names".format(opened[0]["path"]))
	# One skeleton and one calibration for the whole package: the logs' must be the same
	for o in opened[1:]:
		if o["summary"].get("nodes") != log_nodes:
			raise PackError("{}'s nodes differ from {}'s: pack each log on its own".format(
				os.path.basename(o["path"]), os.path.basename(opened[0]["path"])))
		if o["calib"] != opened[0]["calib"]:
			raise PackError("{}'s calibration differs from {}'s (the rig was calibrated again between them, or "
				"one has none here): pack each log on its own".format(os.path.basename(o["path"]),
				os.path.basename(opened[0]["path"])))
	# Each set is its log's row, as the sample says
	for s in sets:
		if s["log"] not in logs:
			raise PackError("a set's log {} isn't one of the sample's logs".format(s["log"]))
		o = opened[logs.index(s["log"])]
		row = int(s["row"])
		if not 0 <= row < len(o["rows"]) or int(o["rows"][row]["frame"]) != int(s["frame"]):
			raise PackError("{}: row {} isn't campy frame {}, as the sample says: is it the log campy-live-flag "
				"read?".format(os.path.basename(o["path"]), row, s["frame"]))
	# Frames already in the rig's test and training packages, to keep away from
	rates = {os.path.basename(o["recording"]): o["clock"].rate for o in opened}
	for path in (test_package, train_package):
		if path and not os.path.isfile(path):
			Say("{} isn't there yet: nothing to keep away from.".format(path))
	tested = PackageFrames(test_package, True, rates) if test_package and os.path.isfile(test_package) else None
	trained = PackageFrames(train_package, False, rates) if train_package and os.path.isfile(train_package) \
		else None
	if test_package and os.path.isfile(test_package):
		from campy import labelmerge
		import sleap_io as sio
		if name in labelmerge.VideoInfo(sio.load_slp(test_package, open_videos=False))[1]:
			raise PackError("{} already has a round named {}: give another --name".format(test_package, name))
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
		table, skipped, left_out = [], 0, 0
		order = sorted(range(len(sets)), key=lambda i: (logs.index(sets[i]["log"]), sets[i]["row"]))
		kept = []
		for i in order:
			s = sets[i]
			o = opened[logs.index(s["log"])]
			frame = int(s["frame"])
			# Each camera's own frame of the set (cam_frame: it differs after a camera lost a frame; -1 for one
			# campy-live left out of the set, whose image is then its video's frame of the set's number)
			rows = o["rows"]
			own = [int(v) for v in rows[int(s["row"])]["cam_frame"]] if "cam_frame" in rows.dtype.names else \
				[frame] * len(o["cams"])
			vf = [int(c.Index([m if m > 0 else frame])[0]) for c, m in zip(o["cams"], own)] # 0: not given
			if min(vf) < 0:
				skipped += 1
				Say("Set at campy frame {} left out: {}'s video lacks it.".format(frame,
					", ".join(n for n, v in zip(names, vf) if v < 0)))
				continue
			if old_split:
				source, at = os.path.basename(o["recording"]), float(s["time_s"])
				test, split = IsTest(source, at, test_share), "old split (seconds since the log's first set)"
			else:
				source, at = o["clock"].source, o["clock"].Seconds(frame)
				test, split = TestSide(source, at, test_share, guard_s, tested, trained)
			if test is None:
				left_out += 1
				Say("Set at campy frame {} left out: {}.".format(frame, split))
				continue
			j = len(kept)
			for c in range(len(names)):
				p = os.path.join(tmp, "c{}_{:06d}.png".format(c, j))
				img = Image(videos_src[logs.index(s["log"])][c], vf[c])
				iio.imwrite(p, img[..., 0] if img.shape[-1] == 1 else img)
				images[c].append(p)
			kept.append((s, o, int(s["row"]), test))
			table.append(dict(package_frame=j, log=os.path.basename(o["path"]), campy_frame=frame,
				recording=os.path.basename(o["recording"]), time_s=s["time_s"], why=s["why"],
				flags=";".join(sorted({"{}:{}".format(f["reason"], f["camera"] or "any") for f in s["flags"]})),
				test=int(test), split_source=source, split_s=round(at, 3), split=split,
				**{"video_frame_{}".format(n): v for n, v in zip(names, vf)}))
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
			guard_s=None if old_split else guard_s, split="old" if old_split else "recording clock",
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
	return dict(package=pkg, folder=folder, name=name, sets=len(kept), test=sum(t for *_, t in kept),
		skipped=skipped, left_out=left_out, cameras=len(names), with3d=with3d)


def Main(argv=None):
	p = argparse.ArgumentParser(prog="campy-label-pack", description="The sets campy-live-flag picked, as one "
		"file to correct in LUC3D, with campy-live's predictions on them (python -m pydoc campy.labelpack).")
	p.add_argument("sample", help="campy-live-flag's sample.json")
	p.add_argument("--recording", action="append", default=[], help="The recording's folder (with a folder per "
		"camera), once per log in the sample, in its order.")
	p.add_argument("--out", default="", help="Where the package's folder goes (default: next to sample.json).")
	p.add_argument("--name", default="", help="The package's name, which is also its round's in the training and "
		"test packages (default: the first recording's, and the start of a hash of sample.json).")
	p.add_argument("--skeleton", default="", help="The skeleton to label in: the training package (.slp), a model "
		"folder or a skeleton .json; the log's nodes go to the nodes of the same name (default: the log's).")
	p.add_argument("--calibration", default="", help="The calibration.toml, when the log's isn't on this computer.")
	p.add_argument("--test-share", type=float, default=TEST_SHARE, help="The share of a recording's minutes kept "
		"as test sets.")
	p.add_argument("--guard-s", type=float, default=GUARD_S, help="A set this close (seconds) to a test minute, or "
		"to a frame in --test-package, is a test set too.")
	p.add_argument("--test-package", default="", help="The rig's test package (campy-label-merge --test-into): "
		"sets near its frames are test sets, and its round names are taken.")
	p.add_argument("--train-package", default="", help="The training package: a set that would be a test set but "
		"is near a frame trained on is left out.")
	p.add_argument("--old-split", action="store_true", help="The split campy used before 2026-11 (seconds since "
		"the log's first set, no guard): only to pack again a sample packed with it.")
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
			force=args.force, guard_s=args.guard_s, test_package=args.test_package or None,
			train_package=args.train_package or None, old_split=args.old_split)
	except (PackError, liveexport.ExportError) as e:
		print("campy-label-pack: {}".format(e), file=sys.stderr)
		return 1
	Say("Round {}: {} sets ({} of them test sets), {} cameras each{}: {}".format(out["name"], out["sets"],
		out["test"], out["cameras"], ", with 3D" if out["with3d"] else "", out["package"]))
	Say("  LUC3D: File > Load SLP > {}; correct each set, then save as {}.labels.slp next to it.".format(
		os.path.basename(out["package"]), os.path.basename(out["package"])[:-len(".pkg.slp")]))
	return 0


if __name__ == "__main__":
	sys.exit(Main())
