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

The .slp's provenance keeps how the recording and the tracking were made:
the recording's session.json and the campy-live-session.json of this log
(campy/manifest.py), and the animals' IDs from the session (animalIds). The
identities stay "animal 1", "animal 2", ...: campy-live numbers the animals
as it first matches them, which says nothing of which is which animal ID.
With --calibration, a calibration that is not the one campy-live tracked
with (its SHA-256 differs) is said.

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
The files' big tables (the frames, the detections and their points, and the
session's groups) are written here, a block of the log's rows at a time,
straight into the file and laid out as sleap-io lays them out: sleap-io
writes only the rest (videos, skeleton, tracks, identities, calibration).
sleap-io's own writer builds a Python object per detection and a tuple per
point first, which took 5.6 min and 20.7 GB of memory for an hour of 3
cameras at 60 fps with 5 animals (214,920 sets), and the files are the same
value for value. Now that hour takes 9 s and 0.2 GB (the EVO-X2, Linux; 13 s
and 0.3 GB on an M-series Mac), and a day (5,158,080 sets, a 17.8 GB log)
3.2 min and 0.95 GB, the same with the log read cold from disk. What it
keeps grows with the recording only by where each frame and detection goes.
The process's resident size counts the log's pages too (18.9 GB for the
day), but those are the file's, which the system drops and reads again when
memory is short: capped at 3 GB, the day took 4.6 min. The files take ~3 GB
an hour: for the day, campy-live.slp 37 GB, each camera's 9.5 GB,
points3d.h5 7.5 GB (73 GB).

Opening them is LUC3D's part. File > Load SLP reads the whole file in the
browser: 10 minutes (257 MB) opened, an hour (1.5 GB) ran the tab out of
memory. LUC3D reads big files a frame at a time from File > Load Project
(with the 3D groups, but the animals only as tracks, not identities) and
Load Session Folder (2D): both opened the hour. --frames FIRST:LAST writes
only those of campy's frames, on the full videos' frame numbers, for a part
to open with Load SLP (each run with --force replaces the last part's files).

It needs sleap-io (0.9.2 or later; LUC3D reads its RecordingSession), which
campy-live's environment has with sleap-nn: run it there. campy itself does
not depend on it.
"""

import argparse, json, os, re, sys
import numpy as np
from campy import manifest

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
	rows = np.load(path, mmap_mode="r") # read a block at a time: an hour's log is ~750 MB
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
		self.frames = np.asarray(rows["frame"], np.int64)
		self.video = np.stack([c.Index(self.frames) for c in cameras], axis=1) # rows x cameras
		# How many cameras' videos agree with each camera's; the group goes on
		# the frame most agree on (a tie: the first camera's)
		have = self.video >= 0
		agree = ((self.video[:, :, None] == self.video[:, None, :]) & have[:, None, :]).sum(axis=2)
		agree[~have] = 0
		best = np.argmax(agree, axis=1)
		self.group = np.where(have.any(axis=1), self.video[np.arange(len(self.video)), best], -1)
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


# ---------------------------------------------------------------- the layout

BLOCK = 8192 # rows of the log read at a time (~30 MB)


def Blocks(rows, index=None):
	"""(first, block) for each block of the log's rows, read from the file only
	then; index: those rows only, in that order."""
	n = len(rows) if index is None else len(index)
	for a in range(0, n, BLOCK):
		b = min(a + BLOCK, n)
		yield a, np.asarray(rows[a:b] if index is None else rows[index[a:b]])


class Layout:
	"""
	Where everything goes in the files, worked out from the log before any is
	written, so that they are written a block of rows at a time and never held
	whole: which detections are written (found, on a frame its camera's video
	has), the frames and detections of each camera before each row, and which
	rows' 3D make the groups (one row per video frame: the first there; see
	Placement).
	"""

	def __init__(self, rows, place, animals):
		n, cameras = place.video.shape
		self.cameras, self.animals = cameras, animals
		self.slots, self.nodes = rows.dtype["keypoints"].shape[1:3]
		on = place.video >= 0
		self.found = np.zeros((n, cameras, self.slots), bool) # each detection that is written
		self.seen = np.zeros((n, animals), bool) # each animal's 3D, where it has any
		self.member = np.zeros((n, animals, cameras), bool) # in its group: its detection in each camera
		for a, block in Blocks(rows):
			b = a + len(block)
			self.found[a:b] = found = np.isfinite(block["keypoints"]).any(axis=(3, 4)) & on[a:b, :, None]
			if animals:
				self.seen[a:b] = np.isfinite(block["points3d"]).any(axis=(2, 3))
				slot = block["members"].astype(np.int64) # rows x animals x cameras: its detection, -1: none
				r, c = np.arange(b - a)[:, None, None], np.arange(cameras)[None, None, :]
				self.member[a:b] = (slot >= 0) & (slot < self.slots) & found[r, c, np.clip(slot, 0, self.slots - 1)] & \
					(place.video[a:b, None, :] == place.group[a:b, None, None]) & self.seen[a:b, :, None]
		# campy-live.slp has each camera's frames in turn, in video order
		self.frames = on.sum(axis=0)
		self.first_frame = np.cumsum(self.frames) - self.frames
		self.rank = np.cumsum(on, axis=0, dtype=np.int32) - 1 # each row's frame among its camera's
		self.instances = self.found.sum(axis=(0, 2))
		self.first_instance = np.cumsum(self.instances) - self.instances
		# The groups: a row's 3D, unless another row's are on its frame already
		have = np.nonzero((place.group >= 0) & self.seen.any(axis=1))[0]
		_, first = np.unique(place.group[have], return_index=True)
		self.kept = have[np.sort(first)] # the rows whose 3D are written, in order
		self.taken = len(have) - len(self.kept)
		self.by_frame = self.kept[np.argsort(place.group[self.kept], kind="stable")] # the groups' order
		self.groups = int(self.seen[self.kept].sum())
		self.members = int(self.member[self.kept].sum())


# ---------------------------------------------------------------- writing

# The tables of a .slp (SLEAP's format 2.8, as sleap-io 0.9.2+ writes it and
# LUC3D's sleap-io.js reads it)
FRAME_DTYPE = np.dtype([("frame_id", "u8"), ("video", "u4"), ("frame_idx", "u8"), ("instance_id_start", "u8"),
	("instance_id_end", "u8")])
INSTANCE_DTYPE = np.dtype([("instance_id", "i8"), ("instance_type", "u1"), ("frame_id", "u8"), ("skeleton", "u4"),
	("track", "i4"), ("from_predicted", "i8"), ("score", "f4"), ("point_id_start", "u8"), ("point_id_end", "u8"),
	("tracking_score", "f4")])
PRED_POINT_DTYPE = np.dtype([("x", "f8"), ("y", "f8"), ("visible", "?"), ("complete", "?"), ("score", "f8")])
FRAME_GROUP_DTYPE = np.dtype([("frame_idx", "i8"), ("ig_start", "u8"), ("ig_end", "u8")])
INSTANCE_GROUP_DTYPE = np.dtype([("identity_idx", "i4"), ("score", "f8"), ("instance_3d_score", "f8"),
	("pts3d_start", "i8"), ("pts3d_end", "i8"), ("pts3d_predicted", "u1"), ("member_start", "u8"),
	("member_end", "u8")])
MEMBER_DTYPE = np.dtype([("camera", "u4"), ("lf", "i8"), ("inst", "u4")])
PREDICTED = 1 # instance_type


def StartSlp(path, labels, frames, instances, nodes, groups=None):
	"""
	Writes a .slp but for its big tables: sleap-io writes what labels has,
	everything but the LabeledFrames (videos, skeleton, tracks, identities, the
	session's calibration); then the tables of the frames, instances and points,
	and with groups (frame groups, instance groups, members) the session's, are
	made here at their full size, to be filled a block of rows at a time.
	Returns the open file. sleap-io would make those tables from a Python object
	per instance and a tuple per point: ~20 GB for an hour.
	"""
	import h5py, sleap_io as sio
	sio.save_slp(labels, path, verbose=False)
	f = h5py.File(path, "r+")
	for name, dtype, n in (("frames", FRAME_DTYPE, frames), ("instances", INSTANCE_DTYPE, instances),
			("pred_points", PRED_POINT_DTYPE, instances * nodes)):
		del f[name]
		f.create_dataset(name, shape=(n,), dtype=dtype)
	if groups and groups[0]:
		frame_groups, instance_groups, members = groups
		grp = f.require_group("session_data")
		for name, dtype, n in (("frame_groups", FRAME_GROUP_DTYPE, frame_groups),
				("instance_groups", INSTANCE_GROUP_DTYPE, instance_groups),
				("instance_group_members", MEMBER_DTYPE, members)):
			grp.create_dataset(name, shape=(n,), dtype=dtype, maxshape=(None,))
		grp.create_dataset("instance_group_meta", shape=(instance_groups,), dtype=h5py.special_dtype(vlen=str),
			maxshape=(None,))
		n = instance_groups * nodes
		grp.create_dataset("points_3d", shape=(n, 3), dtype="f8", maxshape=(None, 3), chunks=(min(8192, n), 3),
			compression="gzip")
		# The session's own entry: its frame groups are rows 0 to frame_groups
		session = json.loads(f["sessions_json"][0])
		session["fg_end"] = frame_groups
		del f["sessions_json"]
		f.create_dataset("sessions_json", data=[np.bytes_(json.dumps(session, separators=(",", ":")))],
			maxshape=(None,))
		f["metadata"].attrs["format_id"] = max(float(f["metadata"].attrs["format_id"]), 2.8)
	return f


def Write2D(rows, place, layout, files):
	"""Each camera's frames, its detections as predicted instances and their
	points, into files: for each camera, (open .slp, its first frame there,
	its first instance there, its video there)."""
	nodes, done_frames, done_instances = layout.nodes, [0] * layout.cameras, [0] * layout.cameras
	for a, block in Blocks(rows):
		b = a + len(block)
		for c in range(layout.cameras):
			on = place.video[a:b, c] >= 0 # a LabeledFrame for each row on its video, detections or not
			if not on.any():
				continue
			found = layout.found[a:b, c][on]
			counts = found.sum(axis=1)
			ends = np.cumsum(counts)
			f_idx, slot = np.nonzero(found) # each instance's frame and detection, in order
			frame = np.empty(len(counts), FRAME_DTYPE)
			frame["frame_idx"] = place.video[a:b, c][on]
			inst = np.empty(len(slot), INSTANCE_DTYPE)
			inst["instance_type"], inst["skeleton"], inst["from_predicted"] = PREDICTED, 0, -1
			inst["score"] = np.nan_to_num(block["centroid_vals"][on, c][f_idx, slot])
			inst["tracking_score"] = np.nan
			track = np.full(found.shape, -1, np.int32) # the animal each detection was matched to
			if layout.animals:
				matched = block["members"][on][:, :, c]
				for k in range(layout.animals):
					there = np.nonzero(matched[:, k] >= 0)[0]
					track[there, matched[there, k]] = k
			inst["track"] = track[f_idx, slot]
			xy = block["keypoints"][on, c][f_idx, slot].astype(np.float64).reshape(-1, 2)
			points = np.empty(len(xy), PRED_POINT_DTYPE)
			points["x"], points["y"] = xy[:, 0], xy[:, 1]
			points["visible"], points["complete"] = ~np.isnan(xy[:, 0]), False
			points["score"] = np.nan_to_num(block["peak_vals"][on, c][f_idx, slot].astype(np.float64)).ravel()
			for f, first_frame, first_instance, video in files[c]:
				f0, i0 = first_frame + done_frames[c], first_instance + done_instances[c]
				frame["frame_id"] = f0 + np.arange(len(frame))
				frame["video"] = video
				frame["instance_id_start"], frame["instance_id_end"] = i0 + ends - counts, i0 + ends
				inst["instance_id"] = i0 + np.arange(len(inst))
				inst["frame_id"] = f0 + f_idx
				inst["point_id_start"] = inst["instance_id"] * nodes
				inst["point_id_end"] = inst["point_id_start"] + nodes
				f["frames"][f0:f0 + len(frame)] = frame
				f["instances"][i0:i0 + len(inst)] = inst
				f["pred_points"][i0 * nodes:(i0 + len(inst)) * nodes] = points
			done_frames[c] += len(frame)
			done_instances[c] += len(inst)


def Write3D(f, rows, place, layout):
	"""The session's frame groups, by video frame: on each, one instance group
	per animal seen, with its identity, its 3D and its detections."""
	grp, nodes = f["session_data"], layout.nodes
	done_groups = done_members = 0
	for a, block in Blocks(rows, layout.by_frame):
		at = layout.by_frame[a:a + len(block)]
		seen, member = layout.seen[at], layout.member[at]
		counts = seen.sum(axis=1)
		ends = done_groups + np.cumsum(counts)
		fg = np.empty(len(at), FRAME_GROUP_DTYPE)
		fg["frame_idx"], fg["ig_start"], fg["ig_end"] = place.group[at], ends - counts, ends
		r, k = np.nonzero(seen) # each group's row (in the block) and animal, in order
		has = member[r, k] # its cameras
		m_counts = has.sum(axis=1)
		m_ends = done_members + np.cumsum(m_counts)
		ig = np.empty(len(r), INSTANCE_GROUP_DTYPE)
		ig["identity_idx"], ig["score"], ig["instance_3d_score"], ig["pts3d_predicted"] = k, np.nan, np.nan, 0
		ig["pts3d_start"] = (done_groups + np.arange(len(r))) * nodes
		ig["pts3d_end"] = ig["pts3d_start"] + nodes
		ig["member_start"], ig["member_end"] = m_ends - m_counts, m_ends
		g, c = np.nonzero(has) # each member's group and camera, in order
		row, slot = at[r[g]], block["members"][r[g], k[g], c]
		mem = np.empty(len(g), MEMBER_DTYPE)
		mem["camera"] = c
		mem["lf"] = layout.first_frame[c] + layout.rank[row, c]
		mem["inst"] = np.cumsum(layout.found[row, c], axis=1)[np.arange(len(g)), slot] - 1 # among its frame's
		n = len(r)
		grp["frame_groups"][a:a + len(at)] = fg
		grp["instance_groups"][done_groups:done_groups + n] = ig
		grp["instance_group_members"][done_members:done_members + len(g)] = mem
		grp["instance_group_meta"][done_groups:done_groups + n] = [json.dumps({"lucid": {"identityId": int(i)}},
			separators=(",", ":")) for i in k]
		grp["points_3d"][done_groups * nodes:(done_groups + n) * nodes] = \
			block["points3d"][r, k].astype(np.float64).reshape(-1, 3)
		done_groups += n
		done_members += len(g)


def Export(recording, log, calibration=None, skeleton=None, cameras=None, frames=None, force=False):
	"""Writes the files (the module's doc); returns what it wrote, as a dict.
	frames: (first, last) of campy's frame numbers, to write only those."""
	import sleap_io as sio

	rows, summary = ReadLog(log)
	numbers = np.asarray(rows["frame"])
	if len(numbers) > 1 and not np.all(np.diff(numbers) > 0):
		r = int(np.argmin(np.diff(numbers) > 0))
		raise ExportError("the log {} has campy frame {} after frame {}: campy-live logs each frame once, in order".format(
			log, numbers[r + 1], numbers[r]))
	if frames is not None:
		rows = rows[np.searchsorted(numbers, frames[0]):np.searchsorted(numbers, frames[1], side="right")]
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
	provenance = Provenance(recording, log, cal_path if with3d else None)
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
	videos = [sio.Video(filename=os.path.join(c.name, c.video).replace(os.sep, "/"), open_backend=False)
		for c in cams]
	layout = Layout(rows, place, animals)

	sessions = []
	if with3d:
		sio_cams = [sio.Camera(name=c["name"], matrix=np.asarray(c["matrix"], float),
			dist=np.asarray(c.get("distortions", [0] * 5), float), size=tuple(c["size"]) if c.get("size") else None,
			rvec=np.asarray(c["rotation"], float), tvec=np.asarray(c["translation"], float)) for c in calib]
		session = sio.RecordingSession(camera_group=sio.CameraGroup(cameras=sio_cams))
		for video, cam in zip(videos, sio_cams):
			session.add_video(video, cam)
		sessions = [session]

	place.Report(cams)
	if layout.taken:
		Say("{} sets' 3D left out: their frame already had another set's (videos behind, above).".format(
			layout.taken))
	for p in there:
		os.remove(p)
	# campy-live.slp: every camera's frames, one camera after the other, and
	# the session; each camera's own .slp: its frames alone, its video next to it
	labels = sio.Labels(videos=videos, skeletons=[skel], tracks=tracks, identities=identities, sessions=sessions,
		provenance=provenance)
	n, files = len(nodes), []
	try:
		files.append(StartSlp(out["slp"], labels, int(layout.frames.sum()), int(layout.instances.sum()), n,
			(len(layout.kept), layout.groups, layout.members) if with3d else None))
		for c, cam in enumerate(cams):
			own = sio.Labels(videos=[sio.Video(filename=cam.video, open_backend=False)], skeletons=[skel],
				tracks=tracks)
			files.append(StartSlp(out["camera_slps"][c], own, int(layout.frames[c]), int(layout.instances[c]), n))
		Write2D(rows, place, layout, [[(files[0], layout.first_frame[c], layout.first_instance[c], c),
			(files[c + 1], 0, 0, 0)] for c in range(len(cams))])
		if with3d and len(layout.kept):
			Write3D(files[0], rows, place, layout)
		for f in files:
			f.close()
		sio.save_skeleton(skel, out["skeleton"])
		if with3d:
			WriteCalibration(out["calibration"], calib)
			WritePoints3D(out["points3d"], rows, place, layout, nodes, [AnimalName(k) for k in range(animals)])
	except BaseException:
		# Half-written files would only mislead (and need --force): none are left
		for f in files:
			f.close()
		for p in paths:
			if p and os.path.exists(p):
				os.remove(p)
		raise

	out.update(sets=len(rows), frames_2d=int(layout.frames.sum()), frames_3d=len(layout.kept) if with3d else 0,
		instances=int(layout.instances.sum()))
	return out


def LogPath(log):
	if log.endswith(".json"):
		log = log[:-5]
	return os.path.abspath(log if log.endswith(".npy") else log + ".npy")


def Provenance(recording, log, calibration=None):
	"""
	The .slp's provenance: the log, the recording's session.json and this
	log's campy-live-session.json (the one whose log it is; campy/manifest.py),
	and the session's animalIds. Says when the calibration is not the one
	campy-live used.
	"""
	path = LogPath(log)
	found = manifest.Read(recording)
	live = [m for m in found["live"] if os.path.normcase(str(m.get("log"))) == os.path.normcase(path)]
	if not live and len(found["live"]) == 1:
		live = found["live"] # a log moved since: the recording's one campy-live run
	live = live[0] if live else None
	# A log campy-live wrote beside itself (the feed did not say the recording's folder)
	if live is None and os.path.isfile(path[:-4] + ".session.json"):
		live = manifest.JsonFile(path[:-4] + ".session.json").Read()
	session = found["session"]
	provenance = {"campy-live-export": path, "session": session, "campy-live-session": live,
		"animalIds": ((session or {}).get("experiment") or {}).get("animalIds") or []}
	used = ((live or {}).get("calibration") or {}).get("sha256")
	if calibration and used:
		given = manifest.Sha256(calibration)
		if given and given != used:
			Say("The calibration {} is not the one campy-live tracked with ({}, SHA-256 {}...): its 3D was "
				"made with that one.".format(calibration, live["calibration"].get("path"), used[:12]))
			provenance["calibrationDiffers"] = {"given": given, "campyLive": used}
	if session is None:
		Say("The recording has no session.json (from campy before 2.1.1): the .slp says less of how it was made.")
	return provenance


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


def WritePoints3D(path, rows, place, layout, nodes, tracks):
	# The 3D alone, as LUC3D's File > Load 3D points reads it (file-io.js
	# parsePoints3dH5): the groups' rows, in the log's order
	import h5py
	strings = h5py.string_dtype("utf-8")
	with h5py.File(path, "w") as f:
		points = f.create_dataset("points_3d", shape=(len(layout.kept), layout.animals, layout.nodes, 3),
			dtype=np.float64)
		for a, block in Blocks(rows, layout.kept):
			points[a:a + len(block)] = block["points3d"].astype(np.float64)
		f.create_dataset("frame_indices", data=place.group[layout.kept])
		f.create_dataset("campy_frames", data=place.frames[layout.kept])
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
