"""
campy-live-export: open a recording that campy-live tracked in LUC3D, with
campy-live's results already on it.

	campy-live-export ./rec/2026-10-22_15-46-09 --log ./campy-live_2026-10-22_15-46-09.npy

campy-live logs its results (campy/live.py: <out>.npy, and <out>.json, the
summary) by campy's frame numbers, and writes nothing LUC3D (talmolab/luc3d,
the multi-view annotation app) can load. This writes, into the recording's
folder, next to the camera folders:

  campy-live.slp       every camera's 2D predictions and, when campy-live ran
                       with --calibration, a RecordingSession: the cameras'
                       calibration, and on each frame one group per animal,
                       with its identity, its 3D pose and the detections it
                       was made from. In LUC3D: File > Load SLP, then pick the
                       recording's folder when it asks where the videos are
                       (it finds each camera's video by its folder's name).
  <camera>/campy-live.slp   that camera's 2D predictions alone
  calibration.toml     the recorded cameras' calibration, under campy's
                       camera names (with --calibration)
  skeleton.json        the model's skeleton
                       These three are LUC3D's session folder: File > Load
                       Session Folder, the recording's folder. 2D only (LUC3D
                       can triangulate them itself); the way to open a
                       recording tracked without --calibration.
  points3d.h5          the 3D alone, as arrays (with --calibration):
                       points_3d (frames x animals x nodes x 3, NaN: not
                       seen), frame_indices (the video frame of each),
                       track_names, node_names, and campy_frames (campy's
                       frame numbers). LUC3D's File > Load 3D points reads it,
                       as 3D poses not linked to the 2D; also for analysis
                       outside LUC3D.

It runs after the recording, not while campy-live runs: writing these as it
goes would cost the live loop time, an .slp cannot grow safely while it is
written, and which video frame holds which of campy's frames is known for
certain only once the recording has ended.

Frames
------
campy-live takes the newest set of frames each time, so it skips some, and
those frames get no predictions. Each prediction goes on the video frame that
holds its camera frame: campy numbers the frames it grabs 1, 2, ..., and a
camera's frametimes.npy lists, in video order, the numbers of the frames its
video holds. They are the same on every camera unless campy's writer dropped
frames from a video (it fell writeQueueMaxSec behind; metadata.csv's
framesDropped). Then that camera's video runs behind the others from there
on: its 2D still go on its own right frames, a group (3D) keeps only the
cameras whose videos agree on the frame, and campy-live-export says from
which frame each such video is behind. hosttimes.npy confirms the match: each
row's host_ns is the same clock reading as the frame's in hosttimes.npy, so a
log of another recording is refused.

The log and its summary
-----------------------
The summary (<log>.json) says the cameras, the nodes and edges, the
calibration and the number of animals (campy after 2.1.0rc6; older summaries
give the calibration in their "pose3d" text and no edges). campy-live writes
it when it ends; without it (campy-live was stopped by an error), give
--cameras, and --skeleton for the nodes. --skeleton <model folder> (its
training_config.yaml or .json) also gives the edges an older summary lacks.
--calibration gives the calibration file when the summary's path is not on
this computer.

Long recordings
---------------
sleap-io builds the whole .slp in memory as it writes it. 10 minutes of 3
cameras at 60 fps with 5 animals (35,820 sets) took 49 s and 3.4 GB of memory
at most (the Mac, M-series), and made a 257 MB campy-live.slp: about 5 min and
20 GB for an hour. --frames FIRST:LAST writes only those of campy's frames,
on the full videos' frame numbers, so a long recording can be opened a part
at a time (each run with --force replaces the last part's files).

It needs sleap-io (0.9.2 or later; LUC3D reads its RecordingSession), which
campy-live's environment has with sleap-nn: run it there. campy itself does
not depend on it.
"""

import argparse, json, os, re, sys
import numpy as np

SLP = "campy-live.slp"
CALIBRATION = "calibration.toml"
SKELETON = "skeleton.json"
POINTS3D = "points3d.h5"
VIDEO_EXTENSIONS = (".mp4", ".avi", ".mkv", ".mov", ".webm")


class ExportError(Exception):
	pass


def Say(text):
	print(text, flush=True)


def AnimalName(k):
	return "animal {}".format(k)


# ---------------------------------------------------------------- reading

def ReadLog(path):
	"""The log's rows, and its summary (None when there is none)."""
	if path.endswith(".json"):
		path = path[:-5] + ".npy"
	if not path.endswith(".npy"):
		path += ".npy"
	if not os.path.isfile(path):
		raise ExportError("no campy-live log at {}".format(path))
	rows = np.load(path)
	summary = None
	if os.path.isfile(path[:-4] + ".json"):
		with open(path[:-4] + ".json") as f:
			summary = json.load(f)
	return rows, summary


def SummaryCalibration(summary):
	"""(path, names, animals) of the summary's calibration, or Nones (2D only)."""
	if not summary:
		return None, None, None
	if summary.get("calibration"):
		return summary["calibration"], summary.get("calibration_names"), summary.get("animals")
	# campy 2.1.0rc6 and before: only campy-live's text, "5 animals, cameras A, B, C of <path>"
	m = re.match(r"(\d+) animals, cameras (.+?) of (.+)$", summary.get("pose3d") or "")
	if m:
		return m.group(3), [n.strip() for n in m.group(2).split(",")], int(m.group(1))
	return None, None, None


def ReadSkeleton(path):
	"""(nodes, edges) from a model folder, its training_config.yaml/.json, or a
	SLEAP skeleton .json / .slp. Edges are pairs of node indices."""
	if os.path.isdir(path):
		for name in ("training_config.yaml", "training_config.json"):
			if os.path.isfile(os.path.join(path, name)):
				path = os.path.join(path, name)
				break
		else:
			raise ExportError("no training_config.yaml or .json in {}".format(path))
	if path.endswith((".yaml", ".yml")) or os.path.basename(path).startswith("training_config"):
		with open(path) as f:
			if path.endswith(".json"):
				config = json.load(f)
			else:
				import yaml
				config = yaml.safe_load(f)
		skeletons = (config.get("data_config") or {}).get("skeletons")
		if isinstance(skeletons, dict): # {name: {nodes, edges}}
			skeletons = list(skeletons.values())
		if not skeletons:
			raise ExportError("no skeleton in {}".format(path))
		sk = skeletons[0]
		nodes = [n["name"] if isinstance(n, dict) else str(n) for n in sk["nodes"]]
		edges = []
		for e in sk.get("edges") or []:
			if isinstance(e, dict):
				a, b = e["source"], e["destination"]
				a, b = (a["name"] if isinstance(a, dict) else a), (b["name"] if isinstance(b, dict) else b)
			else:
				a, b = e
			edges.append([nodes.index(a), nodes.index(b)])
		return nodes, edges
	import sleap_io as sio
	sk = sio.load_skeleton(path)
	sk = sk[0] if isinstance(sk, list) else sk
	return list(sk.node_names), [list(map(int, e)) for e in sk.edge_inds]


class Camera:
	"""One camera folder of the recording: its video, and which of campy's
	frames each video frame is."""

	def __init__(self, recording, name):
		from campy.align import ReadMetadata
		self.name = name
		self.folder = os.path.join(recording, name)
		if not os.path.isdir(self.folder):
			raise ExportError("{} has no folder {}: is it the recording campy-live tracked?".format(recording, name))
		meta = {}
		if os.path.isfile(os.path.join(self.folder, "metadata.csv")):
			meta = ReadMetadata(os.path.join(self.folder, "metadata.csv"))
		video = str(meta.get("videoFilename", ""))
		if not video or not os.path.isfile(os.path.join(self.folder, video)):
			videos = sorted(f for f in os.listdir(self.folder) if f.lower().endswith(VIDEO_EXTENSIONS))
			if len(videos) != 1:
				raise ExportError("{}: {} videos ({}); which one is the recording's?".format(
					self.folder, len(videos), ", ".join(videos) or "none"))
			video = videos[0]
		self.video = video
		path = os.path.join(self.folder, "frametimes.npy")
		if not os.path.isfile(path):
			raise ExportError("{} has no frametimes.npy: campy's frame numbers of its video".format(self.folder))
		self.frames = np.load(path)[0].astype(np.int64) # campy's frame number of each video frame
		path = os.path.join(self.folder, "hosttimes.npy")
		self.host = np.load(path).astype(np.int64) if os.path.isfile(path) else None

	def Index(self, frames):
		"""The video frame of each of campy's frames, -1 where the video lacks it."""
		frames = np.asarray(frames, np.int64)
		i = np.searchsorted(self.frames, frames)
		inside = i < len(self.frames)
		i = np.where(inside, i, 0)
		return np.where(inside & (self.frames[i] == frames), i, -1)


def ReadCalibration(path, names, cameras):
	"""The calibration's cameras `names` (in the cameras' order), each as a
	dict of its anipose fields, renamed to campy's camera names."""
	from campy.pose3d import _ReadToml
	if not os.path.isfile(path):
		raise ExportError("no calibration at {}: give it with --calibration".format(path))
	toml = _ReadToml(path)
	by_name = {}
	for key, c in toml.items():
		if key.startswith("cam_") and isinstance(c, dict):
			by_name[c.get("name", key)] = c
	missing = [n for n in names if n not in by_name]
	if missing:
		raise ExportError("{} has no camera {} (it has {})".format(path, ", ".join(missing), ", ".join(by_name)))
	out = []
	for name, camera in zip(names, cameras):
		c = dict(by_name[name])
		c["name"] = camera
		out.append(c)
	return out


# ---------------------------------------------------------------- matching

class Placement:
	"""
	Where each row's results go: for each row and camera, the video frame
	(-1: that camera's video lacks it), and the frame of the row's groups
	(-1: none), the one most cameras' videos agree on.
	"""

	def __init__(self, rows, cameras):
		self.frames = rows["frame"].astype(np.int64)
		self.video = np.stack([c.Index(self.frames) for c in cameras], axis=1) # rows x cameras
		self.group = np.full(len(rows), -1, np.int64)
		for r, idx in enumerate(self.video):
			have = idx[idx >= 0]
			if len(have):
				values, counts = np.unique(have, return_counts=True)
				best = values[counts == counts.max()]
				self.group[r] = idx[np.isin(idx, best)][0] # a tie: the first camera's
		self.CheckHostTimes(rows, cameras)

	def CheckHostTimes(self, rows, cameras):
		# Each row's host_ns is the frame's own reading of campy's host clock,
		# the same one hosttimes.npy has: they differ only for another recording
		for c, camera in enumerate(cameras):
			if camera.host is None or "host_ns" not in rows.dtype.names:
				continue
			idx = self.video[:, c]
			ok = (idx >= 0) & (idx < len(camera.host))
			if not ok.any():
				continue
			same = np.mean(camera.host[idx[ok]] == rows["host_ns"][ok, c])
			if same < 0.9:
				raise ExportError("{}: the log's frames are not this recording's (only {:.0%} of their host "
					"times are the video frames'): is it the log of this recording?".format(camera.name, same))

	def Report(self, cameras):
		# What did not land, and which videos run behind the others
		n = len(self.frames)
		for c, camera in enumerate(cameras):
			lost = int(np.sum(self.video[:, c] < 0))
			if lost:
				Say("{}: {} of {} sets are frames its video lacks (campy's writer dropped them); "
					"that camera's 2D of those sets are left out.".format(camera.name, lost, n))
			behind = np.nonzero((self.video[:, c] >= 0) & (self.group >= 0) & (self.video[:, c] != self.group))[0]
			if len(behind):
				r = behind[0]
				Say("{}: its video is behind the others' from video frame {} (campy frame {}): there, its 2D are "
					"on its own frames, and the 3D on the others'.".format(camera.name, self.video[r, c],
					self.frames[r]))


# ---------------------------------------------------------------- writing

def Export(recording, log, calibration=None, skeleton=None, cameras=None, frames=None, force=False):
	"""Writes the files (the module's doc); returns what it wrote, as a dict.
	frames: (first, last) of campy's frame numbers, to write only those."""
	import sleap_io as sio
	from sleap_io.model.instance import PredictedInstance3D

	rows, summary = ReadLog(log)
	if frames is not None:
		rows = rows[(rows["frame"] >= frames[0]) & (rows["frame"] <= frames[1])]
	if not len(rows):
		raise ExportError("the log {} has no rows{}".format(log, " in frames {}:{}".format(*frames) if frames else ""))
	names = cameras or (summary or {}).get("cameras")
	if not names:
		raise ExportError("the log has no summary (<log>.json): give --cameras, the camera names in feed order")
	if len(names) != rows["keypoints"].shape[1]:
		raise ExportError("{} cameras named; the log has {}".format(len(names), rows["keypoints"].shape[1]))

	nodes = (summary or {}).get("nodes")
	edges = (summary or {}).get("edges")
	if skeleton:
		nodes, edges = ReadSkeleton(skeleton)
	if not nodes:
		raise ExportError("no node names: the log has no summary; give --skeleton <model folder>")
	if len(nodes) != rows["keypoints"].shape[3]:
		raise ExportError("{} nodes in the skeleton; the log's keypoints have {}".format(len(nodes),
			rows["keypoints"].shape[3]))
	if edges is None:
		Say("The summary has no skeleton edges (campy 2.1.0rc6 and before): LUC3D will show points only. "
			"--skeleton <the instance model's folder> gives them.")
		edges = []

	cal_path, cal_names, _ = SummaryCalibration(summary)
	with3d = "points3d" in rows.dtype.names
	if with3d:
		cal_path = calibration or cal_path
		if not cal_path:
			raise ExportError("the log has 3D, but no calibration is known: give --calibration")
		calib = ReadCalibration(cal_path, cal_names or names, names)

	recording = os.path.abspath(recording)
	cams = [Camera(recording, n) for n in names]
	place = Placement(rows, cams)

	out = {"slp": os.path.join(recording, SLP),
		"camera_slps": [os.path.join(c.folder, SLP) for c in cams],
		"skeleton": os.path.join(recording, SKELETON)}
	if with3d:
		out["calibration"] = os.path.join(recording, CALIBRATION)
		out["points3d"] = os.path.join(recording, POINTS3D)
	paths = [out["slp"], out["skeleton"]] + out["camera_slps"] + [out.get("calibration"), out.get("points3d")]
	there = [p for p in paths if p and os.path.exists(p)]
	if there and not force:
		raise ExportError("already there: {}. --force replaces them.".format(", ".join(there)))

	skel = sio.Skeleton(nodes=list(nodes), edges=[(nodes[a], nodes[b]) for a, b in edges], name="campy-live")
	animals = rows["points3d"].shape[1] if with3d else 0
	tracks = [sio.Track(name=AnimalName(k)) for k in range(animals)]
	identities = [sio.Identity(name=AnimalName(k)) for k in range(animals)]

	# Each camera's 2D: one LabeledFrame per (camera, video frame), the
	# instances of the detections it found
	frames = {} # (camera, video frame) -> LabeledFrame
	instances = {} # (row, camera, detection) -> PredictedInstance
	videos = [sio.Video(filename=os.path.join(c.name, c.video).replace(os.sep, "/"), open_backend=False)
		for c in cams]
	for r, row in enumerate(rows):
		ident = {}
		if with3d:
			for k in range(animals):
				for c in range(len(cams)):
					if row["members"][k, c] >= 0:
						ident[(c, int(row["members"][k, c]))] = k
		for c in range(len(cams)):
			v = int(place.video[r, c])
			if v < 0:
				continue
			lf = frames.get((c, v))
			if lf is None:
				lf = frames[(c, v)] = sio.LabeledFrame(video=videos[c], frame_idx=v)
			for d in range(row["keypoints"].shape[1]): # the slots past those found are NaN
				xy = row["keypoints"][c, d].astype(np.float64)
				if not np.isfinite(xy).any():
					continue
				k = ident.get((c, d))
				inst = sio.PredictedInstance.from_numpy(points_data=xy, skeleton=skel,
					point_scores=np.nan_to_num(row["peak_vals"][c, d].astype(np.float64)),
					score=float(np.nan_to_num(row["centroid_vals"][c, d])),
					track=tracks[k] if k is not None else None)
				lf.instances.append(inst)
				instances[(r, c, d)] = inst

	labeled = [frames[key] for key in sorted(frames)]
	sessions, h5 = [], None
	if with3d:
		sio_cams = [sio.Camera(name=c["name"], matrix=np.asarray(c["matrix"], float),
			dist=np.asarray(c.get("distortions", [0] * 5), float), size=tuple(c["size"]) if c.get("size") else None,
			rvec=np.asarray(c["rotation"], float), tvec=np.asarray(c["translation"], float)) for c in calib]
		session = sio.RecordingSession(camera_group=sio.CameraGroup(cameras=sio_cams))
		for video, cam in zip(videos, sio_cams):
			session.add_video(video, cam)
		groups_at, taken = {}, 0 # group frame -> its groups
		h5_frames, h5_campy, h5_points = [], [], []
		for r, row in enumerate(rows):
			f = int(place.group[r])
			pts = row["points3d"].astype(np.float64)
			if f < 0 or not np.isfinite(pts).any():
				continue
			if f in groups_at: # another row's groups are there already (videos behind, see Placement)
				taken += 1
				continue
			groups = []
			for k in range(animals):
				if not np.isfinite(pts[k]).any():
					continue
				by_cam = {}
				for c in range(len(cams)):
					d = int(row["members"][k, c])
					if d >= 0 and place.video[r, c] == f and (r, c, d) in instances:
						by_cam[sio_cams[c]] = instances[(r, c, d)]
				groups.append(sio.InstanceGroup(instance_by_camera=by_cam, identity=identities[k],
					instance_3d=PredictedInstance3D(points=pts[k], skeleton=skel),
					metadata={"lucid": {"identityId": k}}))
			groups_at[f] = groups
			h5_frames.append(f)
			h5_campy.append(int(row["frame"]))
			h5_points.append(pts)
		for f in sorted(groups_at):
			by_cam = {sio_cams[c]: frames[(c, f)] for c in range(len(cams)) if (c, f) in frames}
			session._frame_group_by_frame_idx[f] = sio.FrameGroup(frame_idx=f, instance_groups=groups_at[f],
				labeled_frame_by_camera=by_cam)
		if taken:
			Say("{} sets' 3D left out: their frame already had another set's (videos behind, above).".format(taken))
		sessions = [session]
		h5 = (np.asarray(h5_frames, np.int64), np.asarray(h5_campy, np.int64),
			np.asarray(h5_points, np.float64).reshape(-1, animals, len(nodes), 3))

	place.Report(cams)
	for p in there:
		os.remove(p)
	labels = sio.Labels(labeled_frames=labeled, videos=videos, skeletons=[skel], tracks=tracks,
		identities=identities, sessions=sessions, provenance={"campy-live-export": os.path.abspath(
			log if log.endswith(".npy") else log + ".npy")})
	sio.save_slp(labels, out["slp"], verbose=False)
	for c, cam in enumerate(cams):
		video = sio.Video(filename=cam.video, open_backend=False) # next to it
		own = [sio.LabeledFrame(video=video, frame_idx=lf.frame_idx, instances=list(lf.instances))
			for (cc, v), lf in sorted(frames.items()) if cc == c]
		sio.save_slp(sio.Labels(labeled_frames=own, videos=[video], skeletons=[skel], tracks=tracks),
			out["camera_slps"][c], verbose=False)
	sio.save_skeleton(skel, out["skeleton"])
	if with3d:
		WriteCalibration(out["calibration"], calib)
		WritePoints3D(out["points3d"], h5, nodes, [AnimalName(k) for k in range(animals)])

	out.update(sets=len(rows), frames_2d=len(labeled), frames_3d=len(h5[0]) if h5 else 0,
		instances=len(instances))
	return out


def WriteCalibration(path, cameras):
	# anipose's calibration.toml, as sleap-anipose writes it (LUC3D reads it)
	def value(v):
		if isinstance(v, str):
			return json.dumps(v)
		if isinstance(v, (list, tuple, np.ndarray)):
			return "[ " + ", ".join(value(x) for x in v) + ",]"
		return repr(float(v)) if isinstance(v, (float, np.floating)) else repr(v)
	with open(path, "w", encoding="utf-8") as f:
		for i, c in enumerate(cameras):
			f.write("[cam_{}]\n".format(i))
			for key in ("name", "size", "matrix", "distortions", "rotation", "translation"):
				if key in c:
					f.write("{} = {}\n".format(key, value(c[key])))
			f.write("\n")
		f.write("[metadata]\nwritten_by = \"campy-live-export\"\n")


def WritePoints3D(path, h5, nodes, tracks):
	# The 3D alone, as LUC3D's File > Load 3D points reads it (file-io.js parsePoints3dH5)
	import h5py
	frames, campy, points = h5
	strings = h5py.string_dtype("utf-8")
	with h5py.File(path, "w") as f:
		f.create_dataset("points_3d", data=points)
		f.create_dataset("frame_indices", data=frames)
		f.create_dataset("campy_frames", data=campy)
		f.create_dataset("node_names", data=np.array(nodes, dtype=object), dtype=strings)
		f.create_dataset("track_names", data=np.array(tracks, dtype=object), dtype=strings)


def Main(argv=None):
	parser = argparse.ArgumentParser(prog="campy-live-export",
		description="Write what campy-live found in a recording as files LUC3D opens: campy-live.slp "
		"(2D, and 3D with identities), per-camera .slp files, calibration.toml, skeleton.json, points3d.h5.")
	parser.add_argument("recording", help="the recording's folder (videoFolder/<date_time>), with a folder per camera")
	parser.add_argument("--log", required=True, help="campy-live's log of that recording: its <out>.npy")
	parser.add_argument("--calibration", default="", help="the calibration.toml campy-live used, when the "
		"summary's path is not on this computer")
	parser.add_argument("--skeleton", default="", help="the instance model's folder (or its training_config, or "
		"a SLEAP skeleton .json/.slp): the nodes' names and edges, for a log whose summary lacks them")
	parser.add_argument("--cameras", default="", help="the cameras' names in feed order, comma-separated, for a "
		"log without its summary")
	parser.add_argument("--frames", default="", help="FIRST:LAST: only campy's frames FIRST to LAST (frametimes.npy's "
		"numbers, from 1; LUC3D's frame numbers too, unless campy dropped frames), for a long recording")
	parser.add_argument("--force", action="store_true", help="replace these files when they are there already")
	args = parser.parse_args(argv)
	frames = None
	if args.frames:
		try:
			first, last = (int(x) if x else None for x in args.frames.split(":"))
			frames = (first or 1, last if last is not None else 2 ** 62)
		except ValueError:
			parser.error("--frames is FIRST:LAST, e.g. 36001:72000")
	try:
		import sleap_io
	except ImportError:
		print("campy-live-export needs sleap-io: run it in campy-live's environment (sleap-nn has it), or "
			"pip install sleap-io.", file=sys.stderr)
		return 1
	try:
		out = Export(args.recording, args.log, calibration=args.calibration or None, skeleton=args.skeleton or None,
			cameras=args.cameras.split(",") if args.cameras else None, frames=frames, force=args.force)
	except ExportError as e:
		print("campy-live-export: {}".format(e), file=sys.stderr)
		return 1
	Say("{} sets: 2D on {} camera frames ({} detections){}.".format(out["sets"], out["frames_2d"], out["instances"],
		", 3D on {} frames".format(out["frames_3d"]) if "points3d" in out else ""))
	Say("Wrote {}".format(out["slp"]))
	Say("  LUC3D: File > Load SLP > {}, then pick {} for the videos.".format(SLP, os.path.abspath(args.recording)))
	Say("Wrote {} in each camera's folder, and {}".format(SLP, ", ".join(os.path.basename(out[k])
		for k in ("skeleton", "calibration", "points3d") if k in out)))
	Say("  LUC3D: File > Load Session Folder > {} (2D).".format(os.path.abspath(args.recording)))
	return 0


if __name__ == "__main__":
	sys.exit(Main())
