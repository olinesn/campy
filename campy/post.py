"""
What runs after a recording has stopped cleanly (campy-session's post:
section), and again by hand:

  campy-session post <recording folder> [--only remux,export] [--force]

Each step is logged (<folder>/logs/post.log, shown as [post] lines) and its
result kept in <folder>/logs/post.json, so running them again does only what
is not done yet. In order:

remux    each fragmented MP4 (fragmentedVideo, campy's default: readable after
         a crash) becomes a plain MP4, faststart, without re-encoding:
         ffmpeg -i v.mp4 -map 0 -c copy -movflags +faststart. Fragmented
         files trip some readers and have no frame count in their header.
         The new file is checked before it replaces the old: ffmpeg's
         framemd5 of every packet of both (the same packets, timestamps and
         codec header, line for line), so it is the same video, bit for
         bit. Only then does it replace the fragmented one (remux: true), or
         the fragmented one stays beside it as <name>.fragmented.mp4
         (remux: keep). A check that fails leaves the original as it was,
         and so does a rename Windows refuses (an antivirus scanner or an
         indexer holding a file just closed): each is tried again for a few
         seconds, and in keep mode the original is moved back to its name
         if the copy cannot take it. One video that fails does not stop the
         others.
         Each needs free space for one more copy of the video it remuxes;
         without it, that video is skipped, and said.
join     each camera whose ffmpeg was restarted during the recording
         (restartFfmpeg: its video in several files, 0.mp4, 0.part2.mp4,
         ..., and a segments.csv) gets them joined into one plain MP4,
         0.joined.mp4, without re-encoding (ffmpeg's concat demuxer, -c
         copy, +faststart; auto_convert off, which would otherwise put the
         codec header into the first packet), for the tools that read one video per camera
         (campy-live-export, so LUC3D; campy-label-pack, campy-synctest).
         Checked before it is kept: every packet of the joined video is the
         files' packets in order (ffmpeg's framemd5: same size and hash),
         and their number is frametimes.npy's rows. For a camera that did
         not close its recording (a crash: run with --force), the rows of
         frames its files hold, read from their packets
         (segments.HeldRows): the frames still queued when it was cut short
         are left out, and said. Files whose codec
         headers differ are not joined (a lossless join keeps the first's
         only), and a file that holds no frame is left out. The files stay
         as they are. Needs free space for one more copy of the files.
align    campy-align (campy/align.py): every camera on the trigger's
         timeline, aligned.csv in each camera's folder and alignment.json
         in the recording's. It says where the trigger paused (no camera
         has a frame for some pulses: every frame after is later than its
         number says) and which camera missed pulses the others got.
         Skipped with no triggered camera and no RatCam (nothing to align
         to). Done is done even when the trigger paused: it is said, here
         and in alignment.json, and the recording watch said it as it
         happened.
export   campy-live-export, in campy-live's environment, when campy-live ran
         (campy-live.npy in the folder). A campy-live started again during
         the recording (campy-session's live.restart) left one log per run
         (campy-live.part2.npy, ...): they are joined into one first, in
         logs/, and that copy removed once exported.
copy     the whole folder to copy.to/<folder's name>: robocopy on Windows,
         rsync elsewhere (or a plain copy when there is no rsync), then every
         file checked byte for byte: read again on both sides and compared
         (campy/copycheck.py), each file's result and SHA-256 in post.json
         and post.log. One file that differs or is missing fails the step.
         Then post's own logs, written after the check, are copied again
         and checked too. To an rsync address (user@host:/path) the copy
         isn't read back. Nothing is deleted here, unless campy-session post
         is given --delete-after-verify (or the config's
         copy.delete_after_verify): then, once every file is the same in the
         copy, the recording's folder is deleted from this computer, if no
         program has a file in it open and nothing in it changed since the
         check (copycheck.Delete). A recording that didn't stop cleanly
         is never deleted, even with --force. The panel's copies do the
         same with the page's box ticked. campy-session's own post-steps
         (campy-session run) never delete.
run      the config's own commands, each with {folder} replaced by the
         recording's folder, in a shell; one that fails is said, and the
         next runs.

They run only after a clean stop: campy-acquire ended with exit code 0
(campy-session's launcher.json says so), or, for a recording made without
campy-session, every camera's metadata.csv has its totals and no
cameraError, videoError or frameTimesError. Otherwise each is skipped, with why and the
command that runs them anyway (--force) once the recording has been looked
at. A crashed recording keeps its fragmented videos, which are readable up to
their last second.
"""

import argparse, csv, glob, json, os, re, shlex, shutil, struct, subprocess, sys, time
from datetime import datetime

from campy import align, copycheck, segments, strictjson

POST_LOG, POST_JSON = "post.log", "post.json"
POST_OWN = ("logs/" + POST_LOG, "logs/" + POST_JSON, "logs/" + POST_JSON + ".tmp") # written to as the copy runs
STEPS = ("remux", "join", "align", "calibrate", "export", "copy", "run")
VIDEO_EXTENSIONS = (".mp4", ".m4v", ".mov")
SPARE_BYTES = 200 * 1024 ** 2 # left free on the disk besides the remuxed copy


class PostError(Exception):
	pass


def Stamp(t=None):
	return datetime.fromtimestamp(time.time() if t is None else t).astimezone().isoformat(timespec="seconds")


class Log:
	"""post.log in the folder's logs/, each line also shown here as [post]."""

	def __init__(self, folder, out=None):
		self.path = os.path.join(folder, "logs", POST_LOG)
		os.makedirs(os.path.dirname(self.path), exist_ok=True)
		self.out = out or sys.stdout
		self.on_line = None # Run's: each line also becomes the running step's latest, for the panel

	def Say(self, text):
		for line in str(text).splitlines() or [""]:
			try:
				with open(self.path, "a", encoding="utf-8") as f:
					f.write("{} {}\n".format(Stamp(), line))
			except OSError:
				pass
			self.out.write("[post] {}\n".format(line))
			if self.on_line is not None:
				self.on_line(line.strip(), None)
		self.out.flush()

	def Progress(self, text, fraction):
		# How far the running step has got (0-1), with what it says: post.json, for the panel's progress bar
		self.Say(text)
		if self.on_line is not None:
			self.on_line(str(text).strip(), fraction)


# ------------------------------------------------------------------ clean stop

UNCLEAN_KEPT = "kept here: the recording didn't stop cleanly:" # FinishCopy's why, for a recording never deleted

def CleanStop(folder):
	"""(clean, why not): whether the recording in folder stopped cleanly."""
	launcher = os.path.join(folder, "launcher.json")
	if os.path.isfile(launcher):
		try:
			with open(launcher) as f:
				status = strictjson.load(f)
		except (OSError, ValueError) as e:
			return False, "its launcher.json cannot be read ({})".format(e)
		acquire = (status.get("parts") or {}).get("acquire") or {}
		code = acquire.get("exit_code")
		if acquire.get("state") != "ended":
			return False, "campy-acquire had not ended when campy-session last wrote launcher.json (campy-session " \
				"stopped while it recorded?)"
		if code != 0:
			return False, "campy-acquire ended with exit code {}".format(code)
	problems = []
	cameras = Cameras(folder)
	if not cameras:
		return False, "it has no camera folders with a metadata.csv"
	for name, meta in cameras.items():
		values = Metadata(meta)
		if "framesWritten" not in values:
			problems.append("{} stopped without closing (no totals in its metadata.csv)".format(name))
		for key in ("cameraError", "videoError", "frameTimesError"):
			if values.get(key):
				problems.append("{}: {}".format(name, values[key]))
	return (not problems), "; ".join(problems)


def Cameras(folder):
	# camera name: its metadata.csv, for each camera folder of the recording
	found = {}
	for meta in sorted(glob.glob(os.path.join(folder, "*", "metadata.csv"))):
		found[os.path.basename(os.path.dirname(meta))] = meta
	return found


def Metadata(path):
	# metadata.csv's key, value rows (campy writes one per line)
	values = {}
	try:
		with open(path, newline="") as f:
			for row in csv.reader(f):
				if len(row) >= 2 and row[0]:
					values[row[0]] = row[1]
	except (OSError, csv.Error):
		pass
	return values


# ------------------------------------------------------------------ remux

def Fragmented(path):
	"""Whether an MP4/MOV is fragmented: a moof box among its top-level boxes (read from their headers only)."""
	try:
		size = os.path.getsize(path)
		with open(path, "rb") as f:
			at = 0
			while at + 8 <= size:
				f.seek(at)
				head = f.read(16)
				n, kind = struct.unpack(">I4s", head[:8])
				if kind == b"moof":
					return True
				if n == 1 and len(head) == 16:
					n = struct.unpack(">Q", head[8:16])[0]
				elif n == 0:
					break
				if n < 8:
					break
				at += n
	except (OSError, struct.error):
		return False
	return False


def Videos(folder):
	# Every camera's videos: in its folder, and in calibration_images/
	videos = []
	for camera in sorted(Cameras(folder)):
		for sub in ("", "calibration_images"):
			for path in sorted(glob.glob(os.path.join(folder, camera, sub, "*"))):
				name = os.path.basename(path)
				if os.path.isfile(path) and name.lower().endswith(VIDEO_EXTENSIONS) and not segments.NotTheVideo(name):
					videos.append(path)
	return videos


def Ffmpeg(path=None):
	if path:
		return path
	if os.environ.get("IMAGEIO_FFMPEG_EXE"):
		return os.environ["IMAGEIO_FFMPEG_EXE"]
	import imageio_ffmpeg
	return imageio_ffmpeg.get_ffmpeg_exe()


def Packets(ffmpeg, path):
	"""ffmpeg's framemd5 of every packet of every stream, as lines (its own version line left out)."""
	out = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-i", path, "-map", "0", "-c", "copy", "-f", "framemd5", "-"],
		capture_output=True, text=True)
	if out.returncode != 0:
		raise PostError("ffmpeg could not read {}: {}".format(path, out.stderr.strip()[-500:]))
	return [line for line in out.stdout.splitlines() if not line.startswith("#software")]


REPLACE_TRIES = 50 # Windows: a rename an antivirus scanner or an indexer refuses is tried again, for ~5 s
REPLACE_WAIT_S = 0.1


def ReplaceRetry(src, dst):
	# os.replace, tried again for a moment on Windows, where another program holding either file refuses it
	for attempt in range(REPLACE_TRIES):
		try:
			os.replace(src, dst)
			return
		except PermissionError:
			if attempt == REPLACE_TRIES - 1:
				raise
			time.sleep(REPLACE_WAIT_S)


def Remux(path, ffmpeg, keep=False, log=None):
	"""
	Remuxes one fragmented video into a plain one, checked packet by packet.
	Returns what it did, in a sentence. Raises PostError when it could not
	(the original is then as it was).
	"""
	say = log.Say if log else print
	stem, ext = os.path.splitext(path)
	tmp = "{}.remux-tmp{}".format(stem, ext)
	free = shutil.disk_usage(os.path.dirname(path)).free
	size = os.path.getsize(path)
	if free < size + SPARE_BYTES:
		raise PostError("not enough free space to remux {} ({:.1f} GB needed, {:.1f} GB free): skipped".format(
			path, (size + SPARE_BYTES) / 1e9, free / 1e9))
	if os.path.exists(tmp):
		os.remove(tmp) # left by a remux that was stopped: never the video itself
	t0 = time.monotonic()
	fmt = "mov" if ext.lower() == ".mov" else "mp4"
	out = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-i", path, "-map", "0", "-c", "copy",
		"-movflags", "+faststart", "-f", fmt, tmp], capture_output=True, text=True)
	try:
		if out.returncode != 0:
			raise PostError("ffmpeg could not remux {}: {}".format(path, out.stderr.strip()[-500:]))
		before, after = Packets(ffmpeg, path), Packets(ffmpeg, tmp)
		if before != after:
			first = next((i for i, (a, b) in enumerate(zip(before, after)) if a != b), min(len(before), len(after)))
			raise PostError("the remuxed copy of {} is not the same video (packet lines differ from line {}: {} "
				"against {} lines): the fragmented video is kept as it was".format(path, first + 1, len(before), len(after)))
		if Fragmented(tmp):
			raise PostError("ffmpeg's copy of {} is still fragmented: the original is kept as it was".format(path))
		packets = sum(1 for line in after if not line.startswith("#"))
		if keep:
			kept = "{}.fragmented{}".format(stem, ext)
			try:
				ReplaceRetry(path, kept)
			except OSError as e:
				raise PostError("could not rename {} to {} ({}): the original is kept as it was".format(path,
					os.path.basename(kept), e)) from None
			try:
				ReplaceRetry(tmp, path)
			except OSError as e:
				try:
					ReplaceRetry(kept, path) # the original back under its own name
				except OSError:
					raise PostError("could not put the remuxed copy of {} in place ({}), nor move the original back: "
						"it is {}; rename it to {} by hand".format(path, e, kept, os.path.basename(path))) from None
				raise PostError("could not put the remuxed copy of {} in place ({}): the original is kept as it "
					"was".format(path, e)) from None
		else:
			try:
				ReplaceRetry(tmp, path)
			except OSError as e:
				raise PostError("could not put the remuxed copy of {} in place ({}): the original is kept as it "
					"was".format(path, e)) from None
	except BaseException:
		if os.path.exists(tmp):
			try:
				os.remove(tmp)
			except OSError:
				pass
		raise
	return "{}: {} packets, the same bit for bit, {:.1f} s{}".format(os.path.relpath(path, os.path.dirname(
		os.path.dirname(path))), packets, time.monotonic() - t0, " (the fragmented one kept beside it)" if keep else "")


def StepRemux(folder, settings, log, ffmpeg=None, stop=None):
	if settings.get("remux") in (False, None):
		return "skipped", "remux is off"
	keep = settings.get("remux") == "keep"
	ffmpeg = Ffmpeg(ffmpeg or settings.get("ffmpeg"))
	videos = Videos(folder)
	fragmented = [v for v in videos if Fragmented(v)]
	if not fragmented:
		return "done", "no fragmented video ({} plain already)".format(len(videos)) if videos else "no videos"
	failed = []
	for video in fragmented:
		if stop and stop():
			return "failed", "stopped (Ctrl-C) before {} of {} videos".format(len(fragmented) - fragmented.index(video),
				len(fragmented))
		try:
			log.Say(Remux(video, ffmpeg, keep=keep, log=log))
		except (PostError, OSError) as e: # one video's error never stops the others
			log.Say(str(e))
			failed.append(os.path.basename(os.path.dirname(video)))
	if failed:
		return "failed", "{} of {} videos not remuxed ({}); they stay fragmented and readable".format(len(failed),
			len(fragmented), ", ".join(failed))
	return "done", "{} videos remuxed to plain MP4, each checked packet by packet".format(len(fragmented))


# ------------------------------------------------------------------ export

def LivePartLogs(folder):
	# campy-live's logs in the folder, in the order they were written: campy-live.npy, campy-live.part2.npy, ...
	logs = [os.path.join(folder, "campy-live.npy")] if os.path.isfile(os.path.join(folder, "campy-live.npy")) else []
	parts = glob.glob(os.path.join(folder, "campy-live.part*.npy"))
	def Number(path):
		m = re.search(r"\.part(\d+)\.npy$", path)
		return int(m.group(1)) if m else 0
	return logs + sorted((p for p in parts if Number(p)), key=Number)


def JoinLogs(logs, out):
	"""
	One log from campy-live's runs, written a block at a time (a day's log
	is ~18 GB: never all in memory), and a summary: the first run's that has
	one, with the sets and skipped summed. Raises PostError when they do not
	fit together (another model, frames out of order).
	"""
	import numpy as np
	parts = [np.load(p, mmap_mode="r") for p in logs]
	dtype = parts[0].dtype
	for path, rows in zip(logs, parts):
		if rows.dtype != dtype:
			raise PostError("{} is not like {} (another model, or another camera count): export them one by one, "
				"with campy-live-export".format(path, logs[0]))
	last = None
	for path, rows in zip(logs, parts):
		if len(rows):
			if last is not None and int(rows["frame"][0]) <= last:
				raise PostError("{} starts at frame {}, not after the frame {} of the log before".format(
					path, int(rows["frame"][0]), last))
			last = int(rows["frame"][-1])
	total = sum(len(r) for r in parts)
	joined = np.lib.format.open_memmap(out, mode="w+", dtype=dtype, shape=(total,))
	at, block = 0, 100000
	for rows in parts:
		for i in range(0, len(rows), block):
			piece = rows[i:i + block]
			joined[at:at + len(piece)] = piece
			at += len(piece)
	joined.flush()
	del joined
	summaries = []
	for path in logs:
		try:
			with open(path[:-4] + ".json") as f:
				summaries.append(strictjson.load(f))
		except (OSError, ValueError):
			pass
	if summaries:
		summary = dict(summaries[0])
		summary["sets"] = total
		summary["parts"] = [os.path.basename(p) for p in logs]
		with open(out[:-4] + ".json", "w") as f:
			strictjson.dump(summary, f, indent=2)
	return total, bool(summaries)


# ------------------------------------------------------------------ join

def PacketList(ffmpeg, path):
	"""(the codec header's line, [(size, hash) of each video packet]), from ffmpeg's framemd5 of the video stream."""
	out = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-i", path, "-map", "0:v:0", "-c", "copy", "-f",
		"framemd5", "-"], capture_output=True, text=True)
	if out.returncode != 0:
		raise PostError("ffmpeg could not read {}: {}".format(path, out.stderr.strip()[-500:]))
	header, packets = None, []
	for line in out.stdout.splitlines():
		if line.startswith("#extradata 0"): # "#extradata 0,   44, <md5>": its size and hash
			header = " ".join(line.split(",", 1)[1].split())
		elif line and not line.startswith("#"):
			fields = [f.strip() for f in line.split(",")]
			packets.append((fields[4], fields[5]))
	return header, packets


def Join(camera, ffmpeg, log=None):
	"""
	Joins one camera folder's video files (segments.csv) into one, checked.
	Returns what it did, in a sentence. Raises PostError when it could not
	(nothing is then changed).
	"""
	rows = segments.Read(camera)
	files = [r["file"] for r in rows if r["frames"] != 0] # a file that holds no frame cannot be read
	rows_npy = os.path.join(camera, "frametimes.npy")
	held = None
	if os.path.isfile(rows_npy) and not segments.Closed(camera):
		# Its camera did not close its recording (campy killed, a power cut):
		# the last file ends at its last fragment written, and frametimes.npy
		# can have rows for frames that never reached it. Which rows the
		# files hold is read from their packets, and the others left out, as
		# campy does when a recording ends (and the tools that read it, too)
		import numpy as np
		try:
			held = segments.HeldRows(camera, np.load(rows_npy)[0])
		except segments.Uncounted as e:
			raise PostError("{}: it did not close its recording, and its video's frames could not be counted ({}): "
				"not joined".format(camera, e))
		empty = set(f for f, n in held.files if n == 0)
		files = [f for f in files if f not in empty]
		if not files:
			raise PostError("{}: none of its video files holds a frame: not joined".format(camera))
	joined = os.path.join(camera, segments.JoinedName(rows[0]["file"]))
	stem, ext = os.path.splitext(joined)
	tmp, listing = "{}.join-tmp{}".format(stem, ext), "{}.join-list.txt".format(stem)
	paths = [os.path.join(camera, f) for f in files]
	missing = [f for f, p in zip(files, paths) if not os.path.isfile(p)]
	if missing:
		raise PostError("{}: {} not there".format(camera, ", ".join(missing)))
	size = sum(os.path.getsize(p) for p in paths)
	free = shutil.disk_usage(camera).free
	if free < size + SPARE_BYTES:
		raise PostError("not enough free space to join {}'s video files ({:.1f} GB needed, {:.1f} GB free): "
			"skipped".format(camera, (size + SPARE_BYTES) / 1e9, free / 1e9))
	parts = [PacketList(ffmpeg, p) for p in paths]
	headers = set(h for h, _ in parts)
	if len(headers) > 1:
		raise PostError("{}: its video files' codec headers differ ({}), and a join without re-encoding keeps only "
			"the first's: not joined. Each file is whole on its own".format(camera, " against ".join(sorted(
			str(h) for h in headers))))
	expected = [packet for _, packets in parts for packet in packets]
	if held is not None:
		kept = int(held.keep.sum())
		# The packets: a frame for each row kept, those past the last row, and a frame cut in the middle (at the
		# end of a file cut short), which no decoder gives, so it has no row
		if kept + held.beyond + held.corrupt != len(expected):
			raise PostError("{}: its video files hold {} frames, and {} of frametimes.npy's rows are frames they "
				"hold{}{}: not joined".format(camera, len(expected), kept, " (and {} past the last row)".format(
				held.beyond) if held.beyond else "", " ({} cut in the middle)".format(held.corrupt) if held.corrupt
				else ""))
	elif os.path.isfile(rows_npy):
		import numpy as np
		frames = np.load(rows_npy)[0]
		kept = int(segments.KeepRows(frames, segments.LostFrames(camera)).sum())
		if kept != len(expected):
			raise PostError("{}: its video files hold {} frames, and frametimes.npy has {} rows for them: not "
				"joined".format(camera, len(expected), kept))
	for path in (tmp, listing):
		if os.path.exists(path):
			os.remove(path) # left by a join that was stopped: never a video file
	t0 = time.monotonic()
	try:
		with open(listing, "w", encoding="utf-8") as f:
			for p in paths:
				f.write("file '{}'\n".format(os.path.abspath(p).replace("'", "'\\''")))
		fmt = "mov" if ext.lower() == ".mov" else "mp4"
		out = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-f", "concat", "-safe", "0", "-auto_convert", "0",
			"-i", listing, "-map", "0", "-c", "copy", "-movflags", "+faststart", "-f", fmt, tmp], capture_output=True,
			text=True)
		if out.returncode != 0:
			raise PostError("ffmpeg could not join {}'s video files: {}".format(camera, out.stderr.strip()[-500:]))
		header, packets = PacketList(ffmpeg, tmp)
		if packets != expected or header not in headers:
			first = next((i for i, (a, b) in enumerate(zip(expected, packets)) if a != b), min(len(expected),
				len(packets)))
			raise PostError("{}: the joined video is not its files' packets (from packet {}: {} against {}): not "
				"kept".format(camera, first + 1, len(packets), len(expected)))
		os.replace(tmp, joined)
	finally:
		for path in (tmp, listing):
			if os.path.exists(path):
				try:
					os.remove(path)
				except OSError:
					pass
	said = "{}: {} files joined into {}, {} frames, the same packets, {:.1f} s".format(os.path.basename(camera),
		len(files), os.path.basename(joined), len(expected), time.monotonic() - t0)
	if held is not None and held.Details():
		said += ". It did not close its recording; {}. The tools that read it (campy-live-export, campy-label-pack) " \
			"leave those rows out too".format(held.Details())
	return said


def StepJoin(folder, settings, log, ffmpeg=None, stop=None):
	if settings.get("join", True) is False:
		return "skipped", "join is off"
	ffmpeg = Ffmpeg(ffmpeg or settings.get("ffmpeg"))
	cameras = [os.path.dirname(meta) for meta in Cameras(folder).values()]
	todo = [c for c in cameras if len(segments.Read(c)) > 1]
	if not todo:
		return "done", "no camera's video is in more than one file"
	failed, done = [], 0
	for camera in todo:
		if stop and stop():
			return "failed", "stopped (Ctrl-C) before {} of {} cameras".format(len(todo) - todo.index(camera), len(todo))
		joined = os.path.join(camera, segments.JoinedName(segments.Files(camera)[0]))
		if os.path.isfile(joined):
			log.Say("{}: joined already ({})".format(os.path.basename(camera), os.path.basename(joined)))
			done += 1
			continue
		try:
			log.Say(Join(camera, ffmpeg, log=log))
			done += 1
		except PostError as e:
			log.Say(str(e))
			failed.append(os.path.basename(camera))
	if failed:
		return "failed", "{} of {} cameras' video files not joined ({}); each file is whole on its own".format(
			len(failed), len(todo), ", ".join(failed))
	return "done", "{} video files joined into one, checked packet by packet".format("1 camera's" if done == 1 else
		"{} cameras'".format(done))


def StepAlign(folder, settings, log, stop=None):
	if settings.get("align", True) is False:
		return "skipped", "align is off"
	try:
		summary = align.Run(folder, log=lambda text: log.Say(text))
	except align.AlignError as e:
		return "skipped", "nothing to align: {}".format(e)
	cams = summary["cameras"]
	aligned = [name for name, c in cams.items() if c.get("aligned")]
	if not aligned:
		return "failed", "no camera could be put on the trigger's timeline"
	pauses = summary.get("trigger_pauses") or []
	missed = {name: c["missed_by_camera"] for name, c in cams.items() if c.get("missed_by_camera")}
	text = "{} of {} cameras on the trigger's timeline (alignment.json, aligned.csv)".format(len(aligned), len(cams))
	if pauses:
		n = sum(p["triggers"] for p in pauses)
		text += "; the trigger paused {} time{}, {} pulses ({:.2f} s) with no frame in any camera".format(len(pauses),
			"" if len(pauses) == 1 else "s", n, sum(p["seconds"] for p in pauses))
	if missed:
		text += "; pulses missed by one camera: {}".format(", ".join("{} {}".format(k, v) for k, v in missed.items()))
	if not pauses and not missed:
		text += "; no trigger pulse without a frame"
	return "done", text


def StepCalibrate(folder, settings, log, stop=None):
	"""
	A calibration recording (its videos in <camera>/calibration_images/): calibrat3, headless in this computer's
	Chrome, makes calibration.toml (campy/calibrate.py), when the post: section asks for it (calibrate: {calibrat3:
	<checkout>, board: {...}, target: 300, browser: chrome}).
	"""
	from campy import calibrate
	videos = calibrate.CalibrationVideos(folder)
	if not videos:
		return "skipped", "not a calibration recording"
	options = settings.get("calibrate") or {}
	if not options or options is False:
		return "skipped", "calibrate is off (post: calibrate: {calibrat3: <checkout>} turns it on)"
	if stop and stop():
		return "failed", "stopped (Ctrl-C)"
	def say(text):
		# calibrat3's stages, as far as the step has got: opened, board found, positions, bundle adjustment done
		fraction = next((f for word, f in (("board found", 0.45), ("positions", 0.75), ("made ", 1.0), ("cameras (", 0.1))
			if word in text), None)
		log.Progress("  " + text, fraction)
	try:
		summary = calibrate.Calibrate(folder, options.get("calibrat3"), board=options.get("board"),
			target=options.get("target", calibrate.DEFAULT_TARGET), browser=options.get("browser", "chrome"),
			say=say, force=True)
	except calibrate.CalibrateError as e:
		return "failed", str(e)
	if not summary["ok"]:
		return "failed", "calibration.toml incomplete: {}".format(summary.get("why"))
	cams = summary["cameras"]
	worst = max(cams, key=lambda n: cams[n]["median_px"])
	return "done", "calibration.toml for {} cameras, by calibrat3: reprojection median {:.2f} px, worst {} {:.2f} px " \
		"({:.0f} s)".format(len(cams), summary["median_px"], worst, cams[worst]["median_px"], summary["seconds"])


def StepExport(folder, settings, log, live_python=None, force=False, stop=None):
	if not settings.get("export", True):
		return "skipped", "export is off"
	logs = LivePartLogs(folder)
	if not logs:
		return "skipped", "campy-live did not run (no campy-live.npy)"
	if not live_python:
		return "failed", "campy-live's environment is not known (live.python, CAMPY_LIVE_PYTHON or " \
			"campy-session register-live)"
	joined = None
	log_path = logs[0]
	if len(logs) > 1:
		joined = os.path.join(folder, "logs", "campy-live.joined.npy")
		total, summary = JoinLogs(logs, joined)
		log.Say("Joined campy-live's {} runs ({}) into one log of {:,} sets{}.".format(len(logs),
			", ".join(os.path.basename(p) for p in logs), total, "" if summary else " (none had a summary)"))
		log_path = joined
	command = [live_python, "-m", "campy.liveexport", folder, "--log", log_path] + (["--force"] if force else [])
	log.Say("campy-live-export: {}".format(" ".join(command)))
	try:
		out = subprocess.run(command, capture_output=True, text=True, cwd=folder)
	finally:
		if joined:
			for path in (joined, joined[:-4] + ".json"):
				if os.path.exists(path):
					os.remove(path)
	for line in (out.stdout + out.stderr).splitlines():
		log.Say("  " + line)
	if out.returncode != 0:
		return "failed", "campy-live-export ended with exit code {}".format(out.returncode)
	return "done", "LUC3D's files written (campy-live.slp)"


# ------------------------------------------------------------------ copy

def CopyDest(folder, to):
	"""(where the copy of folder goes in to, whether to is an rsync address user@host:/path)."""
	name = os.path.basename(os.path.normpath(folder))
	remote = re.match(r"^[^/\\:]+@[^:]+:", str(to)) is not None
	return ("{}/{}".format(str(to).rstrip("/"), name) if remote else os.path.join(str(to), name)), remote


def RunCopier(command, started=None):
	"""
	subprocess.run(command, capture_output=True, text=True), with started(the process) called once it runs: the
	panel follows how much it has written so far (copyqueue.Written).
	"""
	with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
		if started is not None:
			try:
				started(proc)
			except Exception:
				pass
		try:
			stdout, stderr = proc.communicate()
		except BaseException:
			proc.kill()
			raise
	return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


def StepCopy(folder, settings, log, stop=None, details=None, progress=None, started=None):
	"""
	The copy (robocopy on Windows, rsync elsewhere), then every file checked byte for byte (copycheck.Check).
	started(the copy program's process), once it runs: the panel's copies follow how much it has written (copyqueue).
	details (a dict): filled with the check's dest, files (each one's result), snapshot and read_back, for FinishCopy.
	progress(bytes checked, in all).
	"""
	copy = settings.get("copy")
	if not copy:
		return "skipped", "no copy.to"
	to = copy.get("to") if isinstance(copy, dict) else copy
	if not to:
		return "skipped", "no copy.to"
	dest, remote = CopyDest(folder, to)
	t0 = time.monotonic()
	if not remote:
		os.makedirs(dest, exist_ok=True) # rsync makes only the last folder of a path (GNU rsync, on Linux)
	if sys.platform == "win32" and not remote:
		command = ["robocopy", folder, dest, "/E", "/Z", "/R:3", "/W:10", "/NP", "/NFL", "/NDL"]
		out = RunCopier(command, started)
		ok = out.returncode < 8 # robocopy: 0-7 copied (or nothing to copy), 8 and up failed
	elif shutil.which("rsync"):
		command = ["rsync", "-a", "--partial", folder.rstrip("/\\") + "/", dest + "/"]
		out = RunCopier(command, started)
		ok = out.returncode == 0
	elif not remote:
		command = ["copy", folder, dest]
		try:
			shutil.copytree(folder, dest, dirs_exist_ok=True)
			out, ok = None, True
		except OSError as e:
			out, ok = None, False
			log.Say(str(e))
	else:
		return "failed", "copying to {} needs rsync".format(to)
	log.Say("copy: {}".format(" ".join(command)))
	if out is not None:
		for line in (out.stdout + out.stderr).strip().splitlines()[-15:]:
			log.Say("  " + line)
	if not ok:
		return "failed", "the copy to {} failed (exit code {})".format(dest, out.returncode if out else "-")
	copied = time.monotonic() - t0
	if remote:
		return "done", "copied to {} in {:.0f} s; not checked byte for byte (an rsync address: only a folder this " \
			"computer reaches is read back)".format(dest, copied)
	# Every file read again on both sides and compared, byte for byte (post's own logs aside: written to as it copies;
	# FinishCopy copies and checks them once the step's result is in them)
	log.Say("Checking the copy: every file read again here and in {}, and compared byte for byte.".format(dest))
	said = [time.monotonic()]

	def checked(done, total):
		if progress is not None:
			progress(done, total)
		if time.monotonic() - said[0] > 10: # post.json for the panel, and a line in post.log, every 10 s
			said[0] = time.monotonic()
			log.Progress("checking the copy: {:.1f} of {:.1f} GB".format(done / 1e9, total / 1e9),
				done / total if total else None)
	t1 = time.monotonic()
	files, snapshot, total = copycheck.Check(folder, dest, skip=POST_OWN, progress=checked, stop=stop)
	for f in files:
		log.Say("  {:9s} {} ({:,} bytes){}".format(f["result"], f["file"], f["bytes"] or 0,
			", sha256 " + f["sha256"] if f["sha256"] else ": " + f["why"]))
	bad = [f for f in files if f["result"] != "verified"]
	how = copycheck.ReadBackText(files)
	if details is not None:
		details.update(dest=dest, snapshot=snapshot, read_back=how, verified=len(files) - len(bad), mismatched=len(bad),
			files=[{k: f[k] for k in ("file", "bytes", "sha256", "result", "why")} for f in files])
	if bad:
		return "failed", "{} of {} files not the same in the copy ({}): {}".format(len(bad), len(files), dest,
			", ".join("{} {}".format(f["file"], f["why"] or f["result"]) for f in bad[:5]))
	return "done", "copied to {} in {:.0f} s; every file checked byte for byte ({} files, {:.1f} GB, in {:.0f} s; " \
		"the copy's side read {})".format(dest, copied, len(files), total / 1e9, time.monotonic() - t1, how)


def RecordLocal(folder, text, dest=False):
	"""What became of the recording's folder here (deleted, kept and why) into its post.json's copy step: the
	folder's own (dest False), or, once deleted, the copy's (dest: the copy's folder), with a line in its post.log."""
	where = dest or folder
	try:
		saved = Load(where)
		saved["steps"].setdefault("copy", {})["local"] = text
		saved["steps"]["copy"]["local_time"] = Stamp()
		Save(where, saved)
		if dest:
			with open(os.path.join(dest, "logs", POST_LOG), "a", encoding="utf-8") as f:
				f.write("{} {}\n".format(Stamp(), text))
	except (OSError, ValueError, KeyError):
		pass


def FinishCopy(folder, details, log, delete=False, busy=None):
	"""
	After a copy that every file of passed (StepCopy's details) and whose result is in post.json: post's own logs
	copied again and checked (copycheck.CopyFiles); then, with delete, the folder deleted from this computer
	(copycheck.Delete; busy: why something still wants it, or ""). Returns (local: "deleted", "kept" or "here", what
	happened). Nothing is deleted when anything isn't so, nor when the recording didn't stop cleanly (CleanStop).
	"""
	dest = details["dest"]
	logs = [f for f in POST_OWN if not f.endswith(".tmp")]
	unclean = None
	if delete:
		# A recording that didn't stop cleanly (campy-acquire failed, a camera's error) stays here, its copy checked or
		# not: it is looked at where it was made, and a failed start's folder is small
		clean, why = CleanStop(folder)
		if not clean:
			unclean = "{} {}".format(UNCLEAN_KEPT, why)
			delete = False
	if delete:
		text = "every file the same in the copy: deleting {} from this computer once post's logs are copied and " \
			"checked too".format(folder)
		log.Say(text)
		RecordLocal(folder, text)
	ok, why, snap = copycheck.CopyFiles(folder, dest, logs)
	if not ok:
		text = "kept here: post's logs didn't copy: {}".format(why) if delete else \
			"post's logs didn't copy: {}".format(why)
		log.Say(text)
		RecordLocal(folder, text)
		return ("kept" if delete else "here"), text
	if unclean:
		log.Say(unclean)
		RecordLocal(folder, unclean)
		copycheck.CopyFiles(folder, dest, logs) # the copy's logs say it too
		return "kept", unclean
	if not delete:
		return "here", "kept here (deleting after the copy is off)"
	snapshot = dict(details["snapshot"], **snap)
	deleted, text = copycheck.Delete(folder, snapshot, busy=busy)
	if deleted:
		text = "{} deleted from this computer, its copy in {} checked byte for byte".format(folder, dest)
		RecordLocal(folder, text, dest=dest)
		log.out.write("[post] {}\n".format(text))
		log.out.flush()
		return "deleted", text
	log.Say(text)
	RecordLocal(folder, text)
	copycheck.CopyFiles(folder, dest, logs) # the copy's logs say it too (not needed: nothing was deleted)
	return "kept", text


# ------------------------------------------------------------------ run

def StepRun(folder, settings, log, stop=None):
	commands = settings.get("run") or []
	if not commands:
		return "skipped", "no commands"
	failed = 0
	for text in commands:
		if stop and stop():
			return "failed", "stopped (Ctrl-C)"
		command = str(text).replace("{folder}", shlex.quote(folder) if sys.platform != "win32" else '"{}"'.format(folder))
		log.Say("run: {}".format(command))
		out = subprocess.run(command, shell=True, capture_output=True, text=True, cwd=folder)
		for line in (out.stdout + out.stderr).strip().splitlines()[-30:]:
			log.Say("  " + line)
		if out.returncode != 0:
			log.Say("  ended with exit code {}".format(out.returncode))
			failed += 1
	if failed:
		return "failed", "{} of {} commands failed".format(failed, len(commands))
	return "done", "{} commands".format(len(commands))


# ------------------------------------------------------------------ all of them

def Load(folder):
	try:
		with open(os.path.join(folder, "logs", POST_JSON)) as f:
			return strictjson.load(f)
	except (OSError, ValueError):
		return {"schema": 1, "steps": {}}


def Save(folder, state):
	path = os.path.join(folder, "logs", POST_JSON)
	tmp = path + ".tmp"
	with open(tmp, "w") as f:
		strictjson.dump(state, f, indent=1)
	os.replace(tmp, path)


def Run(folder, settings, live_python=None, only=None, force=False, ffmpeg=None, stop=None, out=None, delete=False):
	"""
	Runs the post-steps on folder: those of only (all by default), each
	unless done already (force: again, and after an unclean stop too).
	settings: the post: section. Returns {step: (state, message)}.
	delete: once the copy step has copied and checked every file, and every
	other step has run, the folder is deleted from this computer
	(FinishCopy); results then has "local": (deleted or kept, why).
	"""
	folder = os.path.abspath(folder)
	log = Log(folder, out)
	state = Load(folder)
	results = {}
	copied = {} # the copy step's check (StepCopy's details)
	steps = [s for s in STEPS if only is None or s in only]
	# For the panel while they run: the steps in order (plan), the one running, and its latest line and progress
	state.update(plan=steps, running=None, started=Stamp())
	saved = [0.0]

	def line(text, fraction):
		step = state.get("running")
		if not step or not text:
			return
		entry = state["steps"].setdefault(step, {"state": "running"})
		entry["message"] = text
		if fraction is not None:
			entry["fraction"] = fraction
		if time.monotonic() - saved[0] > 0.5 or fraction is not None: # at most twice a second, but every stage
			saved[0] = time.monotonic()
			try:
				Save(folder, state)
			except OSError:
				pass
	log.on_line = line
	clean, why = CleanStop(folder)
	if not clean and not force:
		log.Say("Post-steps skipped: the recording did not stop cleanly: {}.".format(why))
		log.Say("Once you have looked at it, run them anyway: campy-session post {} --force".format(
			'"{}"'.format(folder) if " " in folder else folder))
		for step in steps:
			results[step] = ("skipped", "not a clean stop: {}".format(why))
			state["steps"][step] = {"state": "skipped", "message": results[step][1], "time": Stamp()}
		Save(folder, state)
		return results
	for step in steps:
		before = state["steps"].get(step, {})
		if before.get("state") == "done" and not force:
			results[step] = ("done", "done already, {}".format(before.get("time")))
			log.Say("{}: done already ({}).".format(step, before.get("message")))
			continue
		if stop and stop():
			results[step] = ("skipped", "stopped (Ctrl-C)")
			continue
		t0 = time.monotonic()
		state["running"] = step
		state["steps"][step] = {"state": "running", "message": "", "time": Stamp(), "fraction": 0.0}
		Save(folder, state)
		log.Say("{}...".format(step))
		try:
			if step == "remux":
				result = StepRemux(folder, settings, log, ffmpeg=ffmpeg, stop=stop)
			elif step == "join":
				result = StepJoin(folder, settings, log, ffmpeg=ffmpeg, stop=stop)
			elif step == "align":
				result = StepAlign(folder, settings, log, stop=stop)
			elif step == "calibrate":
				result = StepCalibrate(folder, settings, log, stop=stop)
			elif step == "export":
				result = StepExport(folder, settings, log, live_python=live_python, force=force or bool(before), stop=stop)
			elif step == "copy":
				copied.clear()
				result = StepCopy(folder, settings, log, stop=stop, details=copied)
			else:
				result = StepRun(folder, settings, log, stop=stop)
		except Exception as e:
			result = ("failed", "{}: {}".format(type(e).__name__, e))
		results[step] = result
		state["running"] = None
		state["steps"][step] = {"state": result[0], "message": result[1], "time": Stamp(),
			"seconds": round(time.monotonic() - t0, 1)}
		if step == "copy" and copied.get("files") is not None:
			state["steps"][step].update({k: copied[k] for k in ("dest", "verified", "mismatched", "read_back", "files")})
		Save(folder, state)
		log.Say("{}: {}, {}.".format(step, result[0], result[1]))
	# Post's logs copied again and checked, now that the copy's result is in them; then, with delete, the folder
	# deleted here: last, once nothing more is written into it
	if results.get("copy", ("",))[0] == "done" and copied.get("files") is not None and not copied.get("mismatched"):
		log.on_line = None
		local = FinishCopy(folder, copied, log, delete=delete)
		if delete:
			results["local"] = local
	return results


def Main(argv=None):
	"""campy-session post <folder>: the post-steps again, by hand."""
	from campy import session
	p = argparse.ArgumentParser(prog="campy-session post", description="Run the steps after a recording (remux, "
		"join, align, export, copy, run) on its folder, those not done yet; python -m pydoc campy.post says what each does.")
	p.add_argument("folder", help="the recording's folder")
	p.add_argument("--config", default="", help="the config whose post: section to use (default: the one "
		"campy-session ran, logs/session-config.yaml; else remux and export)")
	p.add_argument("--only", default="", help="these steps only, comma-separated: " + ", ".join(STEPS))
	p.add_argument("--force", action="store_true", help="run them after an unclean stop too, and again when done")
	p.add_argument("--ffmpeg", default="", help="the ffmpeg to remux with (default: imageio-ffmpeg's)")
	p.add_argument("--to", default="", help="the copy step copies into this folder (as copy.to: it makes "
		"<folder>/<the recording's name>), in place of the config's; the panel's \"Copy each recording to\" box says it")
	p.add_argument("--delete-after-verify", action="store_true", help="once the copy step has copied the folder and "
		"found every file the same in the copy, byte for byte, delete the folder from this computer (not when a "
		"program has a file in it open, or something in it changed since the check); also the config's "
		"copy.delete_after_verify. With a copy done already, add --force to copy and check it again")
	args = p.parse_args(argv)
	folder = os.path.abspath(args.folder)
	if not os.path.isdir(folder):
		print("campy-session post: no folder {}".format(folder), file=sys.stderr)
		return 1
	only = [s.strip() for s in args.only.split(",") if s.strip()] or None
	if only and any(s not in STEPS for s in only):
		print("campy-session post: --only takes {}".format(", ".join(STEPS)), file=sys.stderr)
		return 1
	config_path = args.config or os.path.join(folder, "logs", "session-config.yaml")
	settings, live = dict(session.POST_DEFAULTS), {}
	if os.path.isfile(config_path):
		try:
			config = session.Config(config_path)
		except session.SessionError as e:
			print("campy-session post: {}".format(e), file=sys.stderr)
			return 1
		settings, live = config.post, config.live or {}
	copy = settings.get("copy") if isinstance(settings.get("copy"), dict) else {}
	delete = args.delete_after_verify or bool(copy.get("delete_after_verify"))
	if args.to:
		settings = dict(settings, copy={"to": args.to})
	try:
		live_python = session.LivePython(live)[0]
	except session.SessionError:
		live_python = None
	results = Run(folder, settings, live_python=live_python, only=only, force=args.force, ffmpeg=args.ffmpeg or None,
		delete=delete)
	local = results.pop("local", None)
	if delete and local is None and results.get("copy", ("",))[0] == "done":
		print("campy-session post: nothing deleted: the copy was done already (add --force to copy and check it "
			"again, then delete)", file=sys.stderr)
	return 1 if any(state == "failed" for state, _ in results.values()) or (delete and local and local[0] == "kept") or \
		all(state == "skipped" and "clean stop" in message for state, message in results.values()) else 0
