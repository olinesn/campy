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
         (remux: keep). A check that fails leaves the original as it was.
         Each needs free space for one more copy of the video it remuxes;
         without it, that video is skipped, and said.
export   campy-live-export, in campy-live's environment, when campy-live ran
         (campy-live.npy in the folder). A campy-live started again during
         the recording (campy-session's live.restart) left one log per run
         (campy-live.part2.npy, ...): they are joined into one first, in
         logs/, and that copy removed once exported.
copy     the whole folder to copy.to/<folder's name>: robocopy on Windows,
         rsync elsewhere (or a plain copy when there is no rsync), then every
         file's size checked. Nothing is ever deleted here.
run      the config's own commands, each with {folder} replaced by the
         recording's folder, in a shell; one that fails is said, and the
         next runs.

They run only after a clean stop: campy-acquire ended with exit code 0
(campy-session's launcher.json says so), or, for a recording made without
campy-session, every camera's metadata.csv has its totals and no
cameraError or videoError. Otherwise each is skipped, with why and the
command that runs them anyway (--force) once the recording has been looked
at. A crashed recording keeps its fragmented videos, which are readable up to
their last second.
"""

import argparse, csv, glob, json, os, re, shlex, shutil, struct, subprocess, sys, time
from datetime import datetime

POST_LOG, POST_JSON = "post.log", "post.json"
STEPS = ("remux", "export", "copy", "run")
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

	def Say(self, text):
		for line in str(text).splitlines() or [""]:
			try:
				with open(self.path, "a", encoding="utf-8") as f:
					f.write("{} {}\n".format(Stamp(), line))
			except OSError:
				pass
			self.out.write("[post] {}\n".format(line))
		self.out.flush()


# ------------------------------------------------------------------ clean stop

def CleanStop(folder):
	"""(clean, why not): whether the recording in folder stopped cleanly."""
	launcher = os.path.join(folder, "launcher.json")
	if os.path.isfile(launcher):
		try:
			with open(launcher) as f:
				status = json.load(f)
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
		for key in ("cameraError", "videoError"):
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
				if os.path.isfile(path) and name.lower().endswith(VIDEO_EXTENSIONS) and ".fragmented." not in name \
						and ".remux-tmp" not in name:
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
			os.replace(path, "{}.fragmented{}".format(stem, ext))
		os.replace(tmp, path)
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
		except PostError as e:
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
				summaries.append(json.load(f))
		except (OSError, ValueError):
			pass
	if summaries:
		summary = dict(summaries[0])
		summary["sets"] = total
		summary["parts"] = [os.path.basename(p) for p in logs]
		with open(out[:-4] + ".json", "w") as f:
			json.dump(summary, f, indent=2)
	return total, bool(summaries)


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

def StepCopy(folder, settings, log, stop=None):
	copy = settings.get("copy")
	if not copy:
		return "skipped", "no copy.to"
	to = copy.get("to") if isinstance(copy, dict) else copy
	if not to:
		return "skipped", "no copy.to"
	name = os.path.basename(os.path.normpath(folder))
	remote = re.match(r"^[^/\\:]+@[^:]+:", str(to)) is not None # user@host:/path, for rsync
	dest = "{}/{}".format(str(to).rstrip("/"), name) if remote else os.path.join(str(to), name)
	t0 = time.monotonic()
	if sys.platform == "win32" and not remote:
		command = ["robocopy", folder, dest, "/E", "/Z", "/R:3", "/W:10", "/NP", "/NFL", "/NDL"]
		out = subprocess.run(command, capture_output=True, text=True)
		ok = out.returncode < 8 # robocopy: 0-7 copied (or nothing to copy), 8 and up failed
	elif shutil.which("rsync"):
		command = ["rsync", "-a", "--partial", folder.rstrip("/\\") + "/", dest + "/"]
		out = subprocess.run(command, capture_output=True, text=True)
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
	if not remote:
		missing = []
		for root, dirs, files in os.walk(folder):
			for f in files:
				src = os.path.join(root, f)
				rel = os.path.relpath(src, folder)
				if rel.replace("\\", "/") == "logs/" + POST_LOG:
					continue # written to as it copies
				there = os.path.join(dest, rel)
				if not os.path.isfile(there) or os.path.getsize(there) != os.path.getsize(src):
					missing.append(rel)
		if missing:
			return "failed", "{} files not the same size in {}: {}".format(len(missing), dest, ", ".join(missing[:5]))
	return "done", "copied to {} in {:.0f} s{}".format(dest, time.monotonic() - t0, "" if remote else
		", every file's size checked")


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
			return json.load(f)
	except (OSError, ValueError):
		return {"schema": 1, "steps": {}}


def Save(folder, state):
	path = os.path.join(folder, "logs", POST_JSON)
	tmp = path + ".tmp"
	with open(tmp, "w") as f:
		json.dump(state, f, indent=1)
	os.replace(tmp, path)


def Run(folder, settings, live_python=None, only=None, force=False, ffmpeg=None, stop=None, out=None):
	"""
	Runs the post-steps on folder: those of only (all by default), each
	unless done already (force: again, and after an unclean stop too).
	settings: the post: section. Returns {step: (state, message)}.
	"""
	folder = os.path.abspath(folder)
	log = Log(folder, out)
	state = Load(folder)
	results = {}
	steps = [s for s in STEPS if only is None or s in only]
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
		log.Say("{}...".format(step))
		try:
			if step == "remux":
				result = StepRemux(folder, settings, log, ffmpeg=ffmpeg, stop=stop)
			elif step == "export":
				result = StepExport(folder, settings, log, live_python=live_python, force=force or bool(before), stop=stop)
			elif step == "copy":
				result = StepCopy(folder, settings, log, stop=stop)
			else:
				result = StepRun(folder, settings, log, stop=stop)
		except Exception as e:
			result = ("failed", "{}: {}".format(type(e).__name__, e))
		results[step] = result
		state["steps"][step] = {"state": result[0], "message": result[1], "time": Stamp(),
			"seconds": round(time.monotonic() - t0, 1)}
		Save(folder, state)
		log.Say("{}: {}, {}.".format(step, result[0], result[1]))
	return results


def Main(argv=None):
	"""campy-session post <folder>: the post-steps again, by hand."""
	from campy import session
	p = argparse.ArgumentParser(prog="campy-session post", description="Run the steps after a recording (remux, "
		"export, copy, run) on its folder, those not done yet; python -m pydoc campy.post says what each does.")
	p.add_argument("folder", help="the recording's folder")
	p.add_argument("--config", default="", help="the config whose post: section to use (default: the one "
		"campy-session ran, logs/session-config.yaml; else remux and export)")
	p.add_argument("--only", default="", help="these steps only, comma-separated: " + ", ".join(STEPS))
	p.add_argument("--force", action="store_true", help="run them after an unclean stop too, and again when done")
	p.add_argument("--ffmpeg", default="", help="the ffmpeg to remux with (default: imageio-ffmpeg's)")
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
	try:
		live_python = session.LivePython(live)[0]
	except session.SessionError:
		live_python = None
	results = Run(folder, settings, live_python=live_python, only=only, force=args.force, ffmpeg=args.ffmpeg or None)
	return 1 if any(state == "failed" for state, _ in results.values()) or \
		all(state == "skipped" and "clean stop" in message for state, message in results.values()) else 0
