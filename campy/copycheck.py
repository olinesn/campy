"""
A recording's copy, checked byte for byte, and the recording's own folder
deleted only once every file of it is (the copy step, campy/post.py, and the
panel's copies, campy/copyqueue.py).

Check: every file of the recording is read again on both sides, here and in
the copy, a piece at a time side by side, and the pieces compared byte for
byte; each file's SHA-256 (of the bytes read here) goes into the result too. A
file missing from the copy, a size that differs, or a single byte that does
counts as a mismatch. The two sides are read in step, so the recording's disk
is read no faster than the copy's side comes back (a network share: tens of
MB/s), which keeps the reading from disturbing a recording running meanwhile.

The copy's side is read past this computer's cache where the system lets it,
so what is compared is what the other side holds, not what this computer
still remembers having sent it:
- Windows: the file opened unbuffered (FILE_FLAG_NO_BUFFERING), which a
  network share's client doesn't cache: every byte comes from the server.
- Linux: the file's cached pages dropped first (posix_fadvise DONTNEED, after
  an fsync); only pages already written out can be dropped.
- macOS: caching turned off for the file (F_NOCACHE); pages cached before may
  still be used.
ReadBack says which it managed: "uncached", "cache dropped" or "maybe cached".
What the server answers with may still come from its own memory: the check
says the server holds these bytes, not that its disk does.

Kept out of tracking's way (campy-live runs on the same computer, perhaps a
recording with it): the check uses no GPU, and
- reads at most MAX_BYTES_PER_S (100 MB/s) in all, however fast the copy's
  side answers. Its CPU is SHA-256 and a byte comparison: on an M-series Mac
  (2026-11-10, a 2.15 GB file) 0.7 CPU s per GB unlimited (1.6 GB/s, 114% of
  one core), and 15% of one core on average at 100 MB/s;
- runs in threads of their own at the lowest priority (Background): on
  Windows background mode (THREAD_MODE_BACKGROUND_BEGIN: the lowest CPU, disk
  and memory priority) and THREAD_PRIORITY_IDLE; on Linux nice 19 and the idle
  disk class (ioprio, which only the BFQ and CFQ disk schedulers use; NVMe's
  default "none" ignores it). On macOS nothing is lowered.
- on Linux, drops each piece of the recording it has read from the file
  cache (posix_fadvise), so a day's recording read back doesn't push out
  what tracking keeps there. Windows' background mode gives the pages it
  reads the lowest memory priority, so they are dropped first.

Delete: only after a check that found every file the same, and only when
- nothing else wants the folder (a recording into it, a copy of it waiting),
- no program has a file in it open (SpikeGLX, ffmpeg, campy, a viewer): on
  Windows each file must open for this program alone; on Linux no process's
  open files or working folder are in it (/proc); on macOS lsof finds none,
- and every file is as it was checked (the same names, sizes and change times):
  nothing was written since.
The folder is first renamed to <name>.deleting-campy (so nothing can open it
by its name meanwhile), its files are compared with the checked ones once more,
and only then removed. A folder of that name left behind is one whose removal
was cut short; its copy was checked in full.
"""

import hashlib, os, platform, shutil, subprocess, sys, threading, time

CHUNK = 8 * 1024 * 1024 # bytes compared at a time (a multiple of every disk's sector size, for unbuffered reads)
MAX_BYTES_PER_S = 100e6 # the check reads no faster than this (each side), however fast the copy's side answers
DELETING = ".deleting-campy"


# ------------------------------------------------------------------ reading the copy's side

class _WinUnbuffered:
	"""A file opened unbuffered on Windows (FILE_FLAG_NO_BUFFERING): reads into a sector-aligned buffer."""

	def __init__(self, path):
		import ctypes
		from ctypes import wintypes
		self.ctypes = ctypes
		k32 = ctypes.WinDLL("kernel32", use_last_error=True)
		self.CreateFileW, self.ReadFile, self.CloseHandle = k32.CreateFileW, k32.ReadFile, k32.CloseHandle
		self.CreateFileW.restype = wintypes.HANDLE
		self.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
			wintypes.DWORD, wintypes.HANDLE]
		self.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
			wintypes.LPVOID]
		self.ReadFile.restype = wintypes.BOOL
		self.CloseHandle.argtypes = [wintypes.HANDLE]
		GENERIC_READ, SHARE_READ_WRITE, OPEN_EXISTING = 0x80000000, 0x3, 3
		NO_BUFFERING, SEQUENTIAL_SCAN = 0x20000000, 0x08000000
		handle = self.CreateFileW(str(path), GENERIC_READ, SHARE_READ_WRITE, None, OPEN_EXISTING,
			NO_BUFFERING | SEQUENTIAL_SCAN, None)
		if handle is None or handle == wintypes.HANDLE(-1).value:
			raise OSError(ctypes.get_last_error(), "can't open unbuffered", str(path))
		self.handle = handle
		self.raw = ctypes.create_string_buffer(CHUNK + 65536)
		address = ctypes.addressof(self.raw)
		self.at = address + (-address) % 65536 # aligned for any sector size
		self.got = wintypes.DWORD()

	def read(self, n):
		n = min(n, CHUNK) // 4096 * 4096 or 4096
		if not self.ReadFile(self.handle, self.at, n, self.ctypes.byref(self.got), None):
			raise OSError(self.ctypes.get_last_error(), "read failed")
		return self.ctypes.string_at(self.at, self.got.value)

	def close(self):
		if self.handle is not None:
			self.CloseHandle(self.handle)
			self.handle = None


def ReadBack(path):
	"""(the copy's file opened for reading past this computer's cache, how: uncached, cache dropped or maybe cached)."""
	if sys.platform == "win32":
		try:
			return _WinUnbuffered(path), "uncached"
		except (OSError, AttributeError, ValueError):
			return open(path, "rb", buffering=0), "maybe cached"
	f = open(path, "rb", buffering=0)
	how = "maybe cached"
	try:
		if sys.platform == "darwin":
			import fcntl
			fcntl.fcntl(f.fileno(), getattr(fcntl, "F_NOCACHE", 48), 1)
		elif hasattr(os, "posix_fadvise"):
			try:
				os.fsync(f.fileno()) # pages not yet written out can't be dropped
			except OSError:
				pass
			os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
			how = "cache dropped"
	except OSError:
		pass
	return f, how


def ReadAll(f, n):
	# n bytes, or fewer only at the end of the file
	parts, left = [], n
	while left > 0:
		got = f.read(left)
		if not got:
			break
		parts.append(got)
		left -= len(got)
	return b"".join(parts)


# ------------------------------------------------------------------ out of tracking's way

def Background():
	"""
	The calling thread at the lowest priority this system gives without privileges: CPU, and disk where it can
	(see above). True when lowered. For threads of the check's own only: on Linux a thread can't raise itself again.
	"""
	try:
		if sys.platform == "win32":
			import ctypes
			k32 = ctypes.WinDLL("kernel32", use_last_error=True)
			k32.GetCurrentThread.restype = ctypes.c_void_p
			k32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
			me = k32.GetCurrentThread()
			background = bool(k32.SetThreadPriority(me, 0x00010000)) # THREAD_MODE_BACKGROUND_BEGIN: CPU, I/O, memory
			idle = bool(k32.SetThreadPriority(me, -15)) # THREAD_PRIORITY_IDLE
			return background or idle
		if sys.platform.startswith("linux"):
			tid = threading.get_native_id()
			os.setpriority(os.PRIO_PROCESS, tid, 19) # a Linux thread's own nice
			number = {"x86_64": 251, "aarch64": 30}.get(platform.machine()) # ioprio_set
			if number is not None:
				import ctypes
				ctypes.CDLL(None, use_errno=True).syscall(number, 1, tid, 3 << 13) # IOPRIO_WHO_PROCESS, IOPRIO_CLASS_IDLE
			return True
	except (OSError, AttributeError, ValueError):
		pass
	return False


class Pace:
	"""No more than rate bytes a second, over the whole check: Wait(n) after each n bytes read."""

	def __init__(self, rate=None):
		self.rate = MAX_BYTES_PER_S if rate is None else rate
		self.t0, self.bytes = time.monotonic(), 0

	def Wait(self, n):
		self.bytes += n
		if self.rate:
			ahead = self.bytes / self.rate - (time.monotonic() - self.t0)
			if ahead > 0:
				time.sleep(ahead)


def Forget(f, at, n):
	# Linux: the pieces of the recording read so far dropped from the file cache (they're read once only)
	if hasattr(os, "posix_fadvise") and sys.platform.startswith("linux"):
		try:
			os.posix_fadvise(f.fileno(), at, n, os.POSIX_FADV_DONTNEED)
		except OSError:
			pass


# ------------------------------------------------------------------ checking

def Files(folder, skip=()):
	"""The folder's files, relative, with / between folders, sorted; those in skip left out."""
	found = []
	for root, dirs, files in os.walk(folder):
		dirs.sort()
		for name in sorted(files):
			rel = os.path.relpath(os.path.join(root, name), folder).replace("\\", "/")
			if rel not in skip:
				found.append(rel)
	return found


def Snapshot(folder, skip=()):
	"""{file: (size, change time in ns)} of every file in the folder."""
	snap = {}
	for rel in Files(folder, skip):
		try:
			st = os.stat(os.path.join(folder, rel))
			snap[rel] = (st.st_size, st.st_mtime_ns)
		except OSError:
			snap[rel] = None
	return snap


def CompareFile(src, dst, done=None, stop=None, pool=None, pace=None):
	"""
	One file, here (src) and in the copy (dst), byte for byte: {"bytes", "sha256", "result": verified, mismatch,
	missing or error, "why", "read_back"}. done(n): bytes compared so far, as it goes. pace: a Pace, shared by the
	files of one check.
	"""
	result = {"bytes": None, "sha256": None, "result": "error", "why": "", "read_back": None}
	try:
		size = os.path.getsize(src)
	except OSError as e:
		result["why"] = "can't read it here: {}".format(e)
		return result
	result["bytes"] = size
	if not os.path.isfile(dst):
		result.update(result="missing", why="not in the copy")
		return result
	there = os.path.getsize(dst)
	if there != size:
		result.update(result="mismatch", why="{:,} bytes here, {:,} in the copy".format(size, there))
		return result
	h = hashlib.sha256()
	try:
		with open(src, "rb", buffering=0) as a:
			b, how = ReadBack(dst)
			result["read_back"] = how
			try:
				at = 0
				while True:
					if stop is not None and stop():
						result["why"] = "stopped"
						return result
					# The copy's piece read in another thread while this one reads its own: in step, so the two
					# disks (or the network) work at once, and here is read no faster than there comes back
					theirs = pool.submit(ReadAll, b, CHUNK) if pool is not None else None
					mine = ReadAll(a, CHUNK)
					theirs = theirs.result() if theirs is not None else ReadAll(b, CHUNK)
					if mine != theirs:
						n = min(len(mine), len(theirs))
						first = next((i for i in range(n) if mine[i] != theirs[i]), n)
						result.update(result="mismatch", why="differs from byte {:,}".format(at + first))
						return result
					if not mine:
						break
					h.update(mine)
					Forget(a, at, len(mine))
					Forget(b, at, len(mine))
					at += len(mine)
					if done is not None:
						done(len(mine))
					if pace is not None:
						pace.Wait(len(mine))
			finally:
				b.close()
	except OSError as e:
		result["why"] = "couldn't read it: {}".format(e)
		return result
	if at != size:
		result.update(result="mismatch", why="{:,} bytes read, {:,} expected (the file changed?)".format(at, size))
		return result
	result.update(sha256=h.hexdigest(), result="verified")
	return result


def Check(folder, dest, skip=(), progress=None, stop=None, rate=None):
	"""
	Every file of folder against its copy in dest, byte for byte (CompareFile). Returns (files: [{"file", ...}],
	snapshot: Snapshot(folder) taken before the check, total bytes). progress(bytes done, bytes in all). The reading
	and comparing run in two threads of their own at the lowest priority (Background), no faster than rate bytes a
	second (MAX_BYTES_PER_S; 0: no limit).
	"""
	from concurrent.futures import ThreadPoolExecutor
	snapshot = Snapshot(folder, skip)
	total = sum(s[0] for s in snapshot.values() if s)
	done = [0]

	def step(n):
		done[0] += n
		if progress is not None:
			progress(done[0], total)

	def check(pool):
		pace = Pace(rate)
		files = []
		for rel in snapshot:
			if stop is not None and stop():
				files.append({"file": rel, "bytes": None, "sha256": None, "result": "error", "why": "stopped",
					"read_back": None})
				continue
			r = CompareFile(os.path.join(folder, *rel.split("/")), os.path.join(dest, *rel.split("/")), step, stop,
				pool, pace)
			files.append(dict(file=rel, **r))
		return files
	# One thread reads here, compares and hashes; the other reads the copy's side
	with ThreadPoolExecutor(2, thread_name_prefix="campy copy check", initializer=Background) as pool:
		files = pool.submit(check, pool).result()
	return files, snapshot, total


def ReadBackText(files):
	# How the copy's side was read, said once for all the files
	kinds = sorted({f.get("read_back") for f in files if f.get("read_back")})
	return "/".join(kinds) if kinds else "-"


def CopyFiles(folder, dest, names):
	"""
	These few files (post's logs, written after the big check) copied again into dest and checked byte for byte.
	(ok, why not, {file: Snapshot entry of the file here as copied}).
	"""
	snap = {}
	for rel in names:
		src = os.path.join(folder, *rel.split("/"))
		if not os.path.isfile(src):
			continue
		dst = os.path.join(dest, *rel.split("/"))
		try:
			os.makedirs(os.path.dirname(dst), exist_ok=True)
			before = os.stat(src)
			shutil.copyfile(src, dst)
		except OSError as e:
			return False, "couldn't copy {}: {}".format(rel, e), snap
		r = CompareFile(src, dst)
		if r["result"] != "verified":
			return False, "{}: {} ({})".format(rel, r["result"], r["why"]), snap
		after = os.stat(src)
		if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
			return False, "{} was written to while it was copied".format(rel), snap
		snap[rel] = (after.st_size, after.st_mtime_ns)
	return True, "", snap


# ------------------------------------------------------------------ open files

def _OpenWindows(folder):
	# Each file opened for this program alone (no sharing): refused (a sharing violation) when anything has it open
	import ctypes
	from ctypes import wintypes
	k32 = ctypes.WinDLL("kernel32", use_last_error=True)
	k32.CreateFileW.restype = wintypes.HANDLE
	k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
		wintypes.DWORD, wintypes.HANDLE]
	k32.CloseHandle.argtypes = [wintypes.HANDLE]
	invalid = wintypes.HANDLE(-1).value
	busy = []
	for rel in Files(folder):
		path = os.path.join(folder, *rel.split("/"))
		for attempt in range(3): # an antivirus scanner or an indexer may hold a file for a moment
			handle = k32.CreateFileW(path, 0x80000000, 0, None, 3, 0x80, None)
			if handle is not None and handle != invalid:
				k32.CloseHandle(handle)
				break
			error = ctypes.get_last_error()
			if error == 2: # gone meanwhile: the snapshot comparison says so
				break
			if attempt == 2:
				busy.append("{} ({})".format(rel, "open in another program" if error == 32 else
					"Windows error {}".format(error)))
			else:
				time.sleep(1.0)
	return busy


def _OpenLinux(folder):
	# Every process's open files and working folder, from /proc (those of this user's processes: the rig's programs)
	root = os.path.realpath(folder)
	inside = lambda p: p == root or p.startswith(root + os.sep)
	busy = []
	for pid in os.listdir("/proc"):
		if not pid.isdigit():
			continue
		paths = []
		try:
			paths.append(os.readlink("/proc/{}/cwd".format(pid)))
		except OSError:
			pass
		try:
			for fd in os.listdir("/proc/{}/fd".format(pid)):
				try:
					paths.append(os.readlink("/proc/{}/fd/{}".format(pid, fd)))
				except OSError:
					pass
		except OSError:
			continue
		for p in paths:
			if inside(p):
				busy.append("{} (process {})".format(os.path.relpath(p, root).replace("\\", "/"), pid))
	return busy


def _OpenMac(folder):
	out = subprocess.run(["lsof", "-n", "-P", "-F", "pn", "+D", folder], capture_output=True, text=True, timeout=120)
	busy, pid = [], "?"
	for line in out.stdout.splitlines():
		if line.startswith("p"):
			pid = line[1:]
		elif line.startswith("n") and line[1:].startswith(os.path.realpath(folder)):
			busy.append("{} (process {})".format(os.path.relpath(line[1:], os.path.realpath(folder)), pid))
	return busy


def OpenFiles(folder):
	"""
	The files in folder some program has open, each with which: [] when none. Raises OSError when it can't tell (so
	the folder is kept).
	"""
	if sys.platform == "win32":
		return _OpenWindows(folder)
	if os.path.isdir("/proc/self/fd"):
		return _OpenLinux(folder)
	if sys.platform == "darwin":
		try:
			return _OpenMac(folder)
		except (OSError, subprocess.SubprocessError) as e:
			raise OSError("couldn't ask lsof which files are open: {}".format(e))
	raise OSError("can't tell which files are open on this system")


# ------------------------------------------------------------------ deleting

def Changed(folder, snapshot):
	"""What differs between the folder now and snapshot ({file: (size, change time)}): a few words, or ""."""
	now = Snapshot(folder)
	added = sorted(set(now) - set(snapshot))
	gone = sorted(set(snapshot) - set(now))
	changed = sorted(f for f in set(now) & set(snapshot) if now[f] != snapshot[f])
	said = []
	for what, names in (("new since the check", added), ("gone since the check", gone),
			("written to since the check", changed)):
		if names:
			said.append("{}: {}{}".format(what, ", ".join(names[:3]), " and {} more".format(len(names) - 3)
				if len(names) > 3 else ""))
	return "; ".join(said)


def _Writable(func, path, info):
	# shutil.rmtree's onerror: a read-only file (Windows) made writable and removed again
	import stat
	try:
		os.chmod(path, stat.S_IWRITE)
		func(path)
	except OSError:
		raise info[1]


def Delete(folder, snapshot, busy=None):
	"""
	The recording's folder removed from this computer, its copy having been checked in full. snapshot: every file of
	it as checked ({file: (size, change time)}). busy: why something still wants the folder, or "".
	Returns (deleted, what happened). Kept (False) when anything below isn't so; nothing is removed then.
	"""
	folder = os.path.abspath(folder)
	if busy:
		return False, "kept here: {}".format(busy)
	try:
		open_files = OpenFiles(folder)
	except OSError as e:
		return False, "kept here: {}".format(e)
	if open_files:
		return False, "kept here: {} open: {}".format("a file is" if len(open_files) == 1 else
			"{} files are".format(len(open_files)), ", ".join(open_files[:3]))
	changed = Changed(folder, snapshot)
	if changed:
		return False, "kept here: {}".format(changed)
	# Renamed first: nothing can open a file in it by its name any more (and on Windows the rename itself is refused
	# while a file in it is open); then compared again, and only then removed
	aside = folder.rstrip("\\/") + DELETING
	if os.path.exists(aside):
		return False, "kept here: {} is there already (an earlier deletion cut short?)".format(aside)
	try:
		os.rename(folder, aside)
	except OSError as e:
		return False, "kept here: couldn't move it aside to delete it ({}): a file in it may be open".format(e)
	changed = Changed(aside, snapshot)
	if changed:
		try:
			os.rename(aside, folder)
		except OSError as e:
			return False, "kept, as {}: {} (couldn't put its name back: {})".format(aside, changed, e)
		return False, "kept here: {}".format(changed)
	try:
		if sys.version_info >= (3, 12):
			shutil.rmtree(aside, onexc=lambda func, path, e: _Writable(func, path, (None, e)))
		else:
			shutil.rmtree(aside, onerror=_Writable)
	except OSError as e:
		return False, "partly deleted: what is left is in {} ({}); its copy was checked in full".format(aside, e)
	return True, "deleted from this computer"
