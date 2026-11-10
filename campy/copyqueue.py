"""
The panel's copies (campy-session panel's "Copy each recording to" box):
each recording's folder is copied into a folder the page names once the
recording and its other post-steps have ended, in the background, one
recording at a time, while the next recording records.

The copy is campy-session post's copy step (post.StepCopy): robocopy on
Windows, rsync elsewhere, then every file's size checked; nothing is ever
deleted, here or there. Its result goes into the recording's logs/post.json
as the copy step's, so campy-session post <folder> sees it done, and a copy
that failed or was left can be made again by hand:

  campy-session post <folder> --only copy --to <folder to copy into>

Measured on the Dell Pro Tower Plus (8 cameras, 60 fps, AV1 on Quick Sync),
2026-11-09: a copy to a network share at 21 MB/s during a recording changed
none of its frames' arrival times (p99 0.24 ms either way), and nothing was
dropped.

How far a copy has got (the page's bar, GB so far and time left) is what the
copy program has written: robocopy's or rsync's own count of bytes written
(Written), looked at every 3 s. Not the copy's folder's file sizes: robocopy
(Windows' CopyFileEx) makes each file its full size as it starts on it, so a
5.9 GB SpikeGLX file counted as copied at once, and the page then sat at the
same GB for minutes (seen 2026-11-10, copying 7.6 GB to a network share at
21 MB/s). Where that count isn't kept (macOS), or once the program has ended,
the copy's folder is looked at, a file counting only once its size and time
are its original's (Copied).

The folder a page last copied into is remembered for each config, as the
bout settings are (campy/pico.py): RememberFile(), keyed by the config's path.
"""

import itertools, os, sys, threading, time
from datetime import datetime

import psutil

REMEMBER = "panel_copy.json"
KEEP = 30 # finished copies the page lists
LOOK_S = 3.0 # how often a copy's progress is looked at (a network share is slow to list)
SAME_TIME_S = 2.0 # a copied file's modification time is its original's to within this (FAT keeps it to 2 s)


def RememberFile():
	from campy import session
	return os.path.join(session.StatusDir(), REMEMBER)


def Remembered(path, config):
	"""The folder the page last copied this config's recordings into: text, "" (the box emptied) or None (never set)."""
	from campy import pico, strictjson
	try:
		with open(path) as f:
			got = strictjson.load(f).get(pico.ConfigKey(config))
		return str(got["to"]) if isinstance(got, dict) and "to" in got else None
	except (OSError, ValueError, AttributeError):
		return None


def Remember(path, config, to):
	"""The page's folder for this config, for the next panel opened with it ("" too: copying turned off)."""
	from campy import pico, session, strictjson
	try:
		with open(path) as f:
			everything = strictjson.load(f)
		if not isinstance(everything, dict):
			everything = {}
	except (OSError, ValueError):
		everything = {}
	everything[pico.ConfigKey(config)] = {"to": str(to), "at": datetime.now().astimezone().isoformat(timespec="seconds")}
	try:
		os.makedirs(os.path.dirname(path), exist_ok=True)
		session.WriteJson(path, everything)
	except OSError:
		pass


def Problem(to, video_folder=None):
	"""Why recordings can't be copied into to, or None: it must be a folder this computer reaches now, and not the
	recordings' own folder or inside it (a copy would land in the next recording's place)."""
	to = str(to or "").strip()
	if not to:
		return None
	remote = ":" in to and "@" in to.split(":", 1)[0] # user@host:/path, for rsync
	if remote:
		return None if sys.platform != "win32" else "{} is an rsync address: on Windows, give a folder (a network " \
			"share as \\\\server\\share\\folder).".format(to)
	if not os.path.isdir(to):
		if Creatable(to):
			return "{} doesn't exist yet. Press Create this folder, or check the address.".format(to)
		return "Can't reach {} from this computer. Check the address; a network share may need opening once in " \
			"File Explorer (or signing in) first.".format(to)
	if video_folder:
		a = os.path.normcase(os.path.abspath(to))
		b = os.path.normcase(os.path.abspath(str(video_folder)))
		if a == b or a.startswith(b.rstrip("\\/") + os.sep):
			return "{} is where the recordings are made: copy them somewhere else.".format(to)
	return None


def Creatable(to):
	"""
	Whether the page may make the folder to: it isn't there, and the folder it goes in is (one level only, so a
	mistyped share or parent never makes a tree of wrong folders). Not an rsync address.
	"""
	to = str(to or "").strip()
	if not to or (":" in to and "@" in to.split(":", 1)[0]):
		return False
	path = to.rstrip("\\/")
	parent, name = os.path.dirname(path), os.path.basename(path)
	return bool(name) and name not in (".", "..") and not os.path.exists(path) and bool(parent) and \
		os.path.isdir(parent)


def Create(to, video_folder=None):
	"""Make the folder to (Creatable's), for the page's Create this folder: None, or why not."""
	to = str(to or "").strip()
	if os.path.isdir(to):
		return None
	if not Creatable(to):
		return Problem(to, video_folder) or "Can't make {}.".format(to)
	try:
		os.mkdir(to.rstrip("\\/"))
	except OSError as e:
		return "Couldn't make {}: {}".format(to, e)
	return Problem(to, video_folder) # made: still not one to copy into if it's inside the recordings' folder


def Size(folder):
	total = 0
	for root, _, files in os.walk(folder):
		for name in files:
			try:
				total += os.path.getsize(os.path.join(root, name))
			except OSError:
				pass
	return total


def Copied(folder, dest):
	"""
	The bytes in dest (folder's copy, being made) that are really there: a file that has its original in folder
	counts by its size while it is smaller, and in full only once its size and modification time are the original's
	(robocopy makes a file its full size as it starts on it and gives it its time at the end); any other file (rsync's
	temporary .name.XXXXXX, which grows as it writes) by its size.
	"""
	source = {}
	for root, _, files in os.walk(folder):
		for name in files:
			path = os.path.join(root, name)
			try:
				st = os.stat(path)
			except OSError:
				continue
			source[os.path.normcase(os.path.relpath(path, folder))] = (st.st_size, st.st_mtime)
	total = 0
	for root, _, files in os.walk(dest):
		for name in files:
			path = os.path.join(root, name)
			try:
				st = os.stat(path)
			except OSError:
				continue
			original = source.get(os.path.normcase(os.path.relpath(path, dest)))
			if original is None or st.st_size < original[0]:
				total += st.st_size
			elif st.st_size == original[0] and abs(st.st_mtime - original[1]) <= SAME_TIME_S:
				total += st.st_size
			# else: its full size, but not its original's time yet: still being written
	return total


def Written(process):
	"""
	How many bytes the copy program (a psutil.Process) and the programs it started have written so far, the most any
	of them has (rsync's sender writes what its receiver writes again), or None where that isn't known (macOS keeps no
	count; the program has ended). Windows: its WriteTransferCount, every write it made, to a network share too;
	Linux: write_chars, every write (write_bytes there is only what reached a local disk).
	"""
	if process is None:
		return None
	try:
		processes = [process] + process.children(recursive=True)
	except (psutil.Error, OSError):
		return None
	most = None
	for p in processes:
		try:
			io = p.io_counters()
		except (psutil.Error, OSError, AttributeError, NotImplementedError):
			continue
		n = getattr(io, "write_chars", None)
		n = io.write_bytes if n is None else n
		most = n if most is None else max(most, n)
	return most


_ORDER = itertools.count() # the order recordings were queued in: two queued within one tick of Windows' clock
                            # (about 16 ms) have the same time.time()


class Job:
	def __init__(self, folder, to):
		self.order = next(_ORDER)
		self.folder, self.to = os.path.abspath(folder), str(to)
		self.name = os.path.basename(os.path.normpath(self.folder))
		self.state, self.message = "waiting", ""
		self.added, self.started, self.ended = time.time(), None, None
		self.bytes, self.done_bytes, self.looked = None, 0, 0.0
		self.copier = None # the copy program (robocopy, rsync), a psutil.Process, while it runs (Progress)

	def Dest(self):
		remote = ":" in self.to and "@" in self.to.split(":", 1)[0]
		return "{}/{}".format(self.to.rstrip("/"), self.name) if remote else os.path.join(self.to, self.name)

	def Dict(self, now):
		d = {"folder": self.folder, "name": self.name, "to": self.to, "state": self.state, "message": self.message,
			"bytes": self.bytes, "done_bytes": self.done_bytes, "seconds": None}
		if self.started:
			d["seconds"] = round((self.ended or now) - self.started, 1)
		return d


class CopyQueue:
	"""Copies recordings' folders one after another in a thread of its own (Add); Status for the page."""

	def __init__(self, say=None, out=None):
		self.say = say or (lambda text: print(text, flush=True))
		self.out = out # where post's log lines are shown (default: stdout, as [post])
		self.jobs = []
		self.lock = threading.Lock()
		self.wake = threading.Event()
		self.thread = None

	def Add(self, folder, to):
		job = Job(folder, to)
		with self.lock:
			self.jobs.append(job)
			if self.thread is None or not self.thread.is_alive():
				self.thread = threading.Thread(target=self.Run, name="campy panel copies", daemon=True)
				self.thread.start()
		self.wake.set()
		self.say("{} is copied into {} once the copies before it are done.".format(job.name, job.to))
		return job

	def Pending(self):
		with self.lock:
			return [j for j in self.jobs if j.state in ("waiting", "copying")]

	def Next(self):
		with self.lock:
			for job in self.jobs:
				if job.state == "waiting":
					job.state, job.started = "copying", time.time()
					return job
		return None

	def Run(self):
		while True:
			job = self.Next()
			if job is None:
				self.wake.wait(1.0)
				self.wake.clear()
				continue
			self.Copy(job)

	def Copy(self, job):
		from campy import post
		job.bytes = Size(job.folder)
		self.say("Copying {} ({:.1f} GB) into {}.".format(job.name, job.bytes / 1e9, job.to))
		try:
			log = post.Log(job.folder, self.out)
			state, message = post.StepCopy(job.folder, {"copy": {"to": job.to}}, log, started=self.Started(job))
		except Exception as e:
			state, message = "failed", "{}: {}".format(type(e).__name__, e)
		job.copier = None
		with self.lock:
			if job.state == "left": # the panel closed while it copied: say so, not done
				return
			# Into post.json before the job counts as finished: whatever waits for Pending() to empty (the panel
			# closing, a test) then finds the result there. Under the lock, so Leave can't mark it left meanwhile.
			job.ended = time.time()
			self.Record(job, state, message)
			job.state, job.message = state, message
			if state == "done":
				job.done_bytes = job.bytes
		self.say("Copy of {}: {}, {}.".format(job.name, state, message))

	@staticmethod
	def Started(job):
		def started(proc):
			try:
				job.copier = psutil.Process(proc.pid) # psutil's, not the pid: a pid used again later isn't mistaken for it
			except (psutil.Error, OSError):
				job.copier = None
		return started

	@staticmethod
	def Progress(job):
		"""How many of the job's bytes are copied so far: what the copy program has written, or, where that isn't known,
		what is really in the copy's folder (Copied); never more than the job's, never fewer than said before."""
		written = Written(job.copier)
		try:
			there = Copied(job.folder, job.Dest()) if os.path.isdir(job.Dest()) else 0
		except OSError:
			there = 0
		done = max(written or 0, there) # files skipped as already there (a copy made again) aren't written
		return max(job.done_bytes, min(done, job.bytes or 0))

	@staticmethod
	def Record(job, state, message):
		# Into the recording's logs/post.json, as campy-session post's copy step
		from campy import post
		try:
			os.makedirs(os.path.join(job.folder, "logs"), exist_ok=True)
			saved = post.Load(job.folder)
			saved["steps"]["copy"] = {"state": state, "message": message, "time": post.Stamp(), "to": job.to,
				"seconds": round((job.ended or time.time()) - (job.started or time.time()), 1)}
			post.Save(job.folder, saved)
		except (OSError, ValueError, KeyError):
			pass

	def Leave(self):
		"""The panel closes before every copy is done: the folders not copied, with how to copy them by hand."""
		left = []
		with self.lock:
			for job in self.jobs:
				if job.state in ("waiting", "copying"):
					copying = job.state == "copying"
					job.state, job.ended = "left", time.time()
					job.message = ("the panel closed while it copied (the copy may still finish: check it)" if copying
						else "the panel closed before its copy") + "; to copy it: campy-session post \"{}\" --only copy " \
						"--to \"{}\"".format(job.folder, job.to)
					left.append(job)
		for job in left:
			self.Record(job, "skipped", job.message)
		return left

	def Status(self):
		now = time.time()
		with self.lock:
			jobs = list(self.jobs)
		for job in jobs:
			# How far a copy has got (Progress), looked at every 3 s
			if job.state == "copying" and now - job.looked > LOOK_S:
				job.looked = now
				job.done_bytes = self.Progress(job)
		shown = [j for j in jobs if j.state in ("waiting", "copying")] + \
			[j for j in jobs if j.state not in ("waiting", "copying")][-KEEP:]
		return {"pending": sum(j.state in ("waiting", "copying") for j in jobs),
			"jobs": [j.Dict(now) for j in sorted(shown, key=lambda j: j.order, reverse=True)]}
