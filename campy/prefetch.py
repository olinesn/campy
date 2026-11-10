"""
Read the files a program will need, many at once, before it asks for them.

On a cluster whose software is on network storage, a job on a node that
hasn't read it lately spends most of its start-up waiting on files: Python
imports one module at a time, and each file costs a round trip to the file
server (about 11 ms on the cluster measured in docs/offline-inference.md).
campy-infer imports about 5,000 modules (sleap-nn brings in PyTorch,
Lightning, SciPy, pandas, ...) and then reads PyTorch's, cuDNN's and
TensorRT's libraries, the checkpoints and the TensorRT plans: 90 s on a cold
node, 6 s on one that had read them.

Record writes the list of those files after a run (its modules, the
libraries mapped into it, and the files it names), and Start reads the list
back at the next run's start, with many threads, while Python imports: the
files are then in the node's own cache when the import asks for them. It
only reads: nothing is copied or changed, and a file that is gone or a list
that can't be read is passed over.

Standard library only: Start runs before anything big is imported.
"""

import hashlib, os, sys, threading, time

FORMAT = 1


def ListPath(folder):
	# One list per Python environment (its prefix and version): the list
	# names that environment's own files
	key = hashlib.sha1("{} {} {}".format(sys.prefix, sys.version, FORMAT).encode()).hexdigest()[:16]
	return os.path.join(folder, "prefetch", "files_{}.txt".format(key))


class Reading:
	"""Start's threads; Done says how far they got."""

	def __init__(self, files, threads, limit=None):
		# limit: read at most that many bytes of each file
		self.files, self.bytes, self.read, self.t0 = files, 0, 0, time.perf_counter()
		self.limit = limit
		self.seconds = None
		self.lock = threading.Lock()
		self.queue = list(reversed(files))
		self.threads = [threading.Thread(target=self.Worker, name="campy-prefetch", daemon=True)
			for _ in range(min(threads, len(files)))]
		self.left = len(self.threads)
		for t in self.threads:
			t.start()

	def Worker(self):
		buf = bytearray(8 << 20)
		view = memoryview(buf)
		while True:
			with self.lock:
				if not self.queue:
					self.left -= 1
					if self.left == 0:
						self.seconds = time.perf_counter() - self.t0
					return
				path = self.queue.pop()
			n = 0
			try:
				if path.endswith(os.sep):
					os.listdir(path) # a folder Python's import will list
				else:
					with open(path, "rb", buffering=0) as f:
						while self.limit is None or n < self.limit:
							k = f.readinto(view)
							if not k:
								break
							n += k
			except OSError:
				continue
			with self.lock:
				self.bytes += n
				self.read += 1

	def Done(self):
		# (files read, bytes, seconds the threads took or None while still reading)
		with self.lock:
			return self.read, self.bytes, self.seconds


def Start(folder, also=(), threads=32):
	"""
	Start reading the files the last run listed (in folder's prefetch/),
	and also's (this run's own, such as its models' checkpoints): a
	Reading, or None when there is nothing to read.
	"""
	files = list(also)
	try:
		with open(ListPath(folder), encoding="utf-8") as f:
			files += [line.rstrip("\n") for line in f if line.strip() and not line.startswith("#")]
	except OSError:
		pass
	files = list(dict.fromkeys(files))
	return Reading(files, threads) if files else None


# A video is read ahead up to this many bytes: all of a camera's video of
# an hour or so (about 1 GB), where reading it whole is what its decoder
# will do anyway, but not all of a very long one, which would only push the
# rest of the node's cache out
VIDEO_BYTES = 4 << 30


def Video(path):
	"""
	Start reading a video, in one thread, from start to end (up to
	VIDEO_BYTES): a Reading. A campy recording is a fragmented MP4, about 2
	fragments a second, and ffmpeg (campy-infer's first look at a video, and
	the GPU decoder's demuxer) reads every fragment's header before the first
	frame: on network storage the node hasn't read the video from, that is
	one round trip each, 15 s for a 10-minute video, against about 1 s to
	read the whole file in one go.
	"""
	return Reading([path], 1, VIDEO_BYTES)


def ModelFiles(folders):
	"""The files sleap-nn reads from trained model folders: the checkpoints and
	the training configuration (not the labels and metrics kept beside them)."""
	files = []
	for folder in folders:
		try:
			names = sorted(os.listdir(folder))
		except OSError:
			continue
		files += [os.path.join(folder, n) for n in names if n.endswith(".ckpt") or
			n.split(".")[0] in ("training_config", "initial_config") and n.endswith((".yaml", ".yml", ".json"))]
	return files


def Mapped():
	"""
	{file: (bytes the process has of it in memory, the file's size)} for
	the files mapped into this process (Linux's /proc/self/smaps; {}
	elsewhere): the shared libraries, which the dynamic loader maps rather
	than reads, and of which a process often uses a small part.
	"""
	rss, path = {}, None
	try:
		with open("/proc/self/smaps") as smaps:
			for line in smaps:
				first = line.split(None, 1)[0]
				if "-" in first and not first.endswith(":"): # a mapping's first line: address range, ..., path
					parts = line.split(None, 5)
					path = parts[5].strip().replace(" (deleted)", "") if len(parts) == 6 else ""
					path = os.path.realpath(path) if path.startswith("/") else None
				elif first == "Rss:" and path:
					rss[path] = rss.get(path, 0) + int(line.split()[1]) * 1024
	except (OSError, ValueError, IndexError):
		return {}
	out = {}
	for path, used in rss.items():
		try:
			out[path] = (used, os.path.getsize(path))
		except OSError:
			pass
	return out


def LoadedFiles():
	"""
	The files this process has read to get here, in about the order it
	read them, by their real paths: each imported module's source and its
	compiled .pyc (or an extension module's library), the folders imports
	listed, then the other files mapped into the process (Mapped: the CUDA,
	cuDNN and TensorRT libraries are loaded that way, not imported).
	"""
	files, seen = [], set()

	def Add(path):
		if not path:
			return
		path = os.path.join(os.path.realpath(path), "") if path.endswith(os.sep) else os.path.realpath(path)
		if path not in seen:
			seen.add(path)
			files.append(path)
	for module in list(sys.modules.values()):
		f = getattr(module, "__file__", None)
		if not isinstance(f, str):
			continue
		Add(os.path.join(os.path.dirname(f), "")) # its folder, which import lists
		cached = getattr(module, "__cached__", None)
		if isinstance(cached, str) and os.path.exists(cached):
			Add(cached)
		Add(f)
	for path in Mapped():
		Add(path)
	return files


# A library mapped into the process is read ahead whole only when the
# process used much of it: TensorRT maps its 2 GB builder libraries (one of
# them Windows') even when it only reads a built plan, and touches little of
# them; reading those whole would cost more than it saves.
MOST_BYTES = 1 << 30
USED_SHARE = 0.25
SMALL_BYTES = 16 << 20


def Record(folder, extra=()):
	"""
	Write the list of files this run read (LoadedFiles, then extra: the
	TensorRT plans, the ffmpeg program) for the next run's Start. Only
	regular files and folders that exist, under no system folder (/usr/lib,
	/lib, ...: on the node's own disk), and of the mapped libraries only
	those the run used much of (the others are noted in the list, as
	comments). Returns the list's path, or None when it couldn't be written.
	"""
	system = tuple(os.path.join(p, "") for p in ("/usr/lib", "/usr/lib64", "/lib", "/lib64", "/usr/share", "/etc",
		"/proc", "/sys", "/dev"))
	mapped = Mapped()
	files, seen, skipped = [], set(), []
	for path in LoadedFiles() + [os.path.realpath(p) for p in extra if p]:
		if path.startswith(system) or path in seen:
			continue
		seen.add(path)
		if path in mapped:
			used, size = mapped[path]
			if size > MOST_BYTES or size > SMALL_BYTES and used < USED_SHARE * size:
				skipped.append("# not read ahead: {} ({:.0f} MB, {:.0f} MB of it used)".format(path, size / 1e6,
					used / 1e6))
				continue
		if path.endswith(os.sep) and os.path.isdir(path) or os.path.isfile(path):
			files.append(path)
	out = ListPath(folder)
	try:
		os.makedirs(os.path.dirname(out), exist_ok=True)
		try:
			with open(out, encoding="utf-8") as f:
				if [line.rstrip("\n") for line in f if not line.startswith("#")] == files:
					return out # unchanged
		except OSError:
			pass
		# this process's own temporary file: jobs that share the folder may write at once
		part = "{}.{}-{}.part".format(out, os.uname().nodename if hasattr(os, "uname") else "", os.getpid())
		with open(part, "w", encoding="utf-8") as f:
			f.write("# files campy-infer read to start ({}), for the next run to read ahead: campy/prefetch.py\n".format(
				sys.prefix))
			f.writelines(line + "\n" for line in skipped)
			f.writelines(p + "\n" for p in files)
		os.replace(part, out)
		return out
	except OSError:
		return None
