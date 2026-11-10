"""
The panel's copies (campy-session panel's "Copy each recording to" box):
each recording's folder is copied into a folder the page names once the
recording and its other post-steps have ended, in the background, one
recording at a time, while the next recording records.

The copy is campy-session post's copy step (post.StepCopy): robocopy on
Windows, rsync elsewhere, then every file read again on both sides and
compared byte for byte (campy/copycheck.py). Its result, each file's with it,
goes into the recording's logs/post.json as the copy step's, so
campy-session post <folder> sees it done; post's own logs are then copied
again and checked too (post.FinishCopy). A copy that failed or was left can be
made again by hand:

  campy-session post <folder> --only copy --to <folder to copy into>

With the page's "Delete each recording from this computer once its copy is
checked" ticked (off by default; the config's post copy.delete_after_verify
ticks it), a recording whose every file is the same in the copy is then
deleted here: never a recording that didn't stop cleanly (post.CleanStop:
campy-acquire failed, a camera's error), and not while a program has a file in it open, while it is still
wanted (a recording into it, a copy of it waiting), or when anything in it
changed since the check (copycheck.Delete). Nothing is ever deleted in the
copy. A copy that fails, or a folder kept though it was to be deleted (one
that didn't stop cleanly too), is also sent as an alert (session.Alert: ~/.campy/alerts.yaml's channels).

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
ETA_AFTER_S = 5.0 # a phase's time left is only estimated once it has run this long (its rate means something then)
SAME_TIME_S = 2.0 # a copied file's modification time is its original's to within this (FAT keeps it to 2 s)


def RememberFile():
	from campy import session
	return os.path.join(session.StatusDir(), REMEMBER)


def _Remembered(path, config):
	from campy import pico, strictjson
	try:
		with open(path) as f:
			got = strictjson.load(f).get(pico.ConfigKey(config))
		return got if isinstance(got, dict) else {}
	except (OSError, ValueError, AttributeError):
		return {}


def Remembered(path, config):
	"""The folder the page last copied this config's recordings into: text, "" (the box emptied) or None (never set)."""
	got = _Remembered(path, config)
	return str(got["to"]) if "to" in got else None


def RememberedDelete(path, config):
	"""Whether the page last had this config's "Delete each recording ... once its copy is checked" ticked: True,
	False, or None (never set)."""
	got = _Remembered(path, config)
	return got["delete"] if isinstance(got.get("delete"), bool) else None


def Remember(path, config, to, delete=None):
	"""The page's folder for this config, for the next panel opened with it ("" too: copying turned off)."""
	from campy import pico, session, strictjson
	try:
		with open(path) as f:
			everything = strictjson.load(f)
		if not isinstance(everything, dict):
			everything = {}
	except (OSError, ValueError):
		everything = {}
	entry = {"to": str(to), "at": datetime.now().astimezone().isoformat(timespec="seconds")}
	if delete is not None:
		entry["delete"] = bool(delete)
	everything[pico.ConfigKey(config)] = entry
	try:
		os.makedirs(os.path.dirname(path), exist_ok=True)
		session.WriteJson(path, everything)
	except OSError:
		pass


def Problem(to, video_folder=None):
	"""Why recordings can't be copied into to, or None: it must be a folder this computer reaches now, and not the
	folder the recordings go into (video_folder) or inside it (a copy would land in the recording's own place, or the
	next one's). post.StepCopy refuses a copy onto its own recording too, whatever the page said."""
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
	# The folder the recording goes into (the panel's RecordsInto: a Pico length's own videoFolder), as it really is:
	# links followed, and the same folder through a network share of this disk too (post.Within)
	from campy import post
	if video_folder and post.Within(to, str(video_folder)):
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
	def __init__(self, folder, to, delete=False):
		self.order = next(_ORDER)
		self.folder, self.to, self.delete = os.path.abspath(folder), str(to), bool(delete)
		self.name = os.path.basename(os.path.normpath(self.folder))
		self.state, self.message = "waiting", ""
		self.added, self.started, self.ended = time.time(), None, None
		self.bytes, self.done_bytes, self.looked = None, 0, 0.0
		# While it copies: copying, then checking (checked_bytes of check_bytes read again on both sides), then
		# finishing (post's logs copied again; deleting here, when it is to be)
		self.phase, self.checked_bytes, self.check_bytes = None, 0, None
		# When the phase began, and when its count (done_bytes, checked_bytes) was last measured: what eta_s is
		# estimated from (Eta)
		self.phase_at, self.measured_at = None, None
		self.verified, self.mismatched = None, None # files the same in the copy, and not, once checked
		self.local = None # what became of the folder here: deleted, kept (why) or here
		self.copier = None # the copy program (robocopy, rsync), a psutil.Process, while it runs (Progress)

	def Dest(self):
		remote = ":" in self.to and "@" in self.to.split(":", 1)[0]
		return "{}/{}".format(self.to.rstrip("/"), self.name) if remote else os.path.join(self.to, self.name)

	def Dict(self, now):
		d = {"folder": self.folder, "name": self.name, "to": self.to, "state": self.state, "message": self.message,
			"bytes": self.bytes, "done_bytes": self.done_bytes, "seconds": None, "delete": self.delete,
			"phase": self.phase, "checked_bytes": self.checked_bytes, "check_bytes": self.check_bytes,
			"verified": self.verified, "mismatched": self.mismatched, "local": self.local}
		if self.started:
			d["seconds"] = round((self.ended or now) - self.started, 1)
		d["eta_s"] = self.Eta(now)
		return d

	def Phase(self, phase, now=None):
		if phase != self.phase:
			self.phase_at = time.time() if now is None else now
			self.measured_at = None
		self.phase = phase

	def Eta(self, now):
		"""
		Seconds left (from now) in what the copy does now: copying, at the
		phase's average rate so far, or the byte-for-byte check, at its own.
		The rate is the one when the count was last measured (every LOOK_S
		for the copy), counted down from that moment, so it doesn't creep up
		between measurements. None while it can't be estimated yet (under
		ETA_AFTER_S into the phase, nothing done) or the phase has none
		(finishing, deleting).
		"""
		if self.state != "copying" or self.phase_at is None or self.measured_at is None:
			return None
		if self.phase == "copying":
			done, total = self.done_bytes, self.bytes
		elif self.phase == "checking":
			done, total = self.checked_bytes, self.check_bytes
		else:
			return None
		ran = self.measured_at - self.phase_at
		if not total or not done or ran < ETA_AFTER_S:
			return None
		left = (total - done) / (done / ran) - (now - self.measured_at)
		return round(max(0.0, left), 1)


class CopyQueue:
	"""Copies recordings' folders one after another in a thread of its own (Add); Status for the page."""

	def __init__(self, say=None, out=None, busy=None, alert=None):
		self.say = say or (lambda text: print(text, flush=True))
		self.out = out # where post's log lines are shown (default: stdout, as [post])
		self.busy = busy # busy(folder): why the panel still wants that folder (recording into it), or ""
		self.alert = alert # alert(key, text, severity): session.Alert by default
		self.jobs = []
		self.lock = threading.Lock()
		self.wake = threading.Event()
		self.thread = None

	def Add(self, folder, to, delete=False):
		job = Job(folder, to, delete)
		with self.lock:
			self.jobs.append(job)
			if self.thread is None or not self.thread.is_alive():
				self.thread = threading.Thread(target=self.Run, name="campy panel copies", daemon=True)
				self.thread.start()
		self.wake.set()
		self.say("{} is copied into {} once the copies before it are done{}.".format(job.name, job.to,
			", checked byte for byte, then deleted from this computer" if job.delete else ""))
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
		job.Phase("copying")
		self.say("Copying {} ({:.1f} GB) into {}.".format(job.name, job.bytes / 1e9, job.to))
		details = {}

		def checked(done, total):
			job.Phase("checking")
			job.checked_bytes, job.check_bytes, job.measured_at = done, total, time.time()
		try:
			log = post.Log(job.folder, self.out)
			state, message = post.StepCopy(job.folder, {"copy": {"to": job.to}}, log, details=details, progress=checked,
				started=self.Started(job))
		except Exception as e:
			state, message = "failed", "{}: {}".format(type(e).__name__, e)
		job.copier = None
		job.verified, job.mismatched = details.get("verified"), details.get("mismatched")
		with self.lock:
			if job.state == "left": # the panel closed while it copied: say so, not done
				return
			# Into post.json before the job counts as finished: whatever waits for Pending() to empty (the panel
			# closing, a test) then finds the result there. Under the lock, so Leave can't mark it left meanwhile.
			job.ended = time.time()
			self.Record(job, state, message, details)
			finish = state == "done" and details.get("files") is not None and not details.get("mismatched")
			if finish: # under the lock too: from here on Leave lets it end
				job.Phase("deleting" if job.delete else "finishing")
		local = None
		unchecked = state == "done" and details.get("files") is None # an rsync address: never read back
		if finish:
			# Every file the same in the copy: post's logs copied again (the result is in them now), then, ticked,
			# the folder deleted here
			try:
				local, text = post.FinishCopy(job.folder, details, log, delete=job.delete, busy=self.Busy(job))
			except Exception as e:
				local, text = ("kept" if job.delete else "here"), "{}: {}".format(type(e).__name__, e)
			job.local = text
			if local == "deleted":
				message += "; deleted from this computer"
			elif job.delete:
				message += "; " + text
		elif job.delete and unchecked:
			job.local = "kept here: the copy wasn't checked (an rsync address isn't read back), so nothing was deleted"
			message += "; " + job.local
		elif job.delete:
			job.local = "kept here: the copy isn't the same" if state == "failed" else "kept here"
		with self.lock:
			if job.state == "left":
				return
			job.ended = time.time()
			job.state, job.message, job.phase = state, message, None
			if state == "done":
				job.done_bytes = job.bytes
		self.say("Copy of {}: {}, {}.".format(job.name, state, message))
		if state != "done":
			self.Alert("copy/" + job.name, "The copy of {} into {} failed: {}. Its folder is kept on this "
				"computer.".format(job.folder, job.to, message))
		elif job.delete and local != "deleted" and (job.local or "").startswith(post.UNCLEAN_KEPT):
			# Kept on purpose, but the disk isn't freed and the recording failed: said where it is seen away from the page
			self.Alert("copy/" + job.name, "{} didn't stop cleanly, so it was copied into {} and checked, but kept on "
				"this computer though the box to delete it is ticked ({}). Look at its logs, then delete it by "
				"hand.".format(job.folder, job.to, job.local))
		elif job.delete and unchecked:
			# Not checked, so not deleted: the box can't free the disk with an rsync address
			self.Alert("copy/" + job.name, "{} was copied into {}, but the copy wasn't checked (an rsync address "
				"isn't read back), so it was kept on this computer though the box to delete it is ticked. To have "
				"recordings deleted once their copy is checked, copy into a folder this computer reaches (a network "
				"share).".format(job.folder, job.to))
		elif job.delete and local != "deleted":
			self.Alert("copy/" + job.name, "{} was copied into {} and checked, but not deleted from this computer: {}. "
				"Delete it by hand once nothing uses it.".format(job.folder, job.to, job.local))

	def Busy(self, job):
		# Why the folder can't be deleted now though its copy is checked: the panel records into it, or another copy
		# of it is waiting
		why = self.busy(job.folder) if self.busy is not None else ""
		if why:
			return why
		with self.lock:
			if any(j is not job and j.state in ("waiting", "copying") and os.path.normcase(j.folder) ==
					os.path.normcase(job.folder) for j in self.jobs):
				return "another copy of it is waiting"
		return ""

	def Alert(self, key, text):
		try:
			if self.alert is not None:
				self.alert(key, text, "warning")
			else:
				from campy import session
				session.Alert(key, text, "warning")
		except Exception as e:
			self.say("Could not send the alert: {}".format(e))

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
	def Record(job, state, message, details=None):
		# Into the recording's logs/post.json, as campy-session post's copy step
		from campy import post
		if not os.path.isdir(job.folder):
			return # deleted (its copy checked) or gone: never made again just to hold a post.json
		try:
			os.makedirs(os.path.join(job.folder, "logs"), exist_ok=True)
			saved = post.Load(job.folder)
			saved["steps"]["copy"] = {"state": state, "message": message, "time": post.Stamp(), "to": job.to,
				"seconds": round((job.ended or time.time()) - (job.started or time.time()), 1)}
			if details and details.get("files") is not None:
				saved["steps"]["copy"].update({k: details[k] for k in ("dest", "verified", "mismatched", "read_back",
					"files")})
			post.Save(job.folder, saved)
		except (OSError, ValueError, KeyError):
			pass

	def Leave(self):
		"""The panel closes before every copy is done: the folders not copied, with how to copy them by hand. A copy
		already checked and finishing (post's logs, the delete) is not left: it stays in Pending() until it ends."""
		left = []
		with self.lock:
			for job in self.jobs:
				if job.state == "copying" and job.phase in ("finishing", "deleting"):
					# Copied and checked, its result in post.json: post's logs copied again and, ticked, the folder
					# being deleted. Left to end (Pending() keeps it): marking it left would make a folder deleted
					# meanwhile again (Record) and keep its alert from going out
					continue
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
				if job.phase == "copying":
					job.measured_at = now
		shown = [j for j in jobs if j.state in ("waiting", "copying")] + \
			[j for j in jobs if j.state not in ("waiting", "copying")][-KEEP:]
		return {"pending": sum(j.state in ("waiting", "copying") for j in jobs),
			"jobs": [j.Dict(now) for j in sorted(shown, key=lambda j: j.order, reverse=True)]}
