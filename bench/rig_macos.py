"""
The macOS half of bench/rig.py: which processes run, and what each uses, read from macOS itself (Python's
standard library only: ps, and ctypes over libproc and sysctl).

macOS has no /proc. What rig.py reads there instead:
  every process     ps -ax (its pid, parent, user, CPU time, start and program). /bin/ps is setuid root,
                    so it shows every user's processes and their CPU time: the CPU load counts them all
  this user's       libproc: the files each has open (proc_pidinfo PROC_PIDLISTFDS, then each file's
  processes only    path), its disk reads and writes (proc_pid_rusage), and its command line and
                    environment (sysctl KERN_PROCARGS2), where RIG_JOB is. macOS shows another user's
                    (root's) only to root, as Linux hides their open files, their I/O and their environment
What each process counts as using:
  trigger  a USB serial port open: /dev/cu.* or /dev/tty.* named usbmodem*, usbserial*, SLAB_USBtoUART*,
           wchusbserial* (an Arduino, an FTDI or CP210x adapter); RIG_EXTRA adds other files
  ephys    a program named spikeglx* or open-ephys* (RIG_EPHYS_NAMES), by its file name or, for a
           Windows program under Wine (whose process is Wine's), by its command line's first word
  desktop  WindowServer, which draws every window on the GPU: noted, never waited for
  gpu, encoder, camera
           not watched: Metal, VideoToolbox and the cameras go through IOKit, not device files, and every
           app with a window uses the GPU. RIG_EXTRA can still name a file as one of them (the tests do)

Importing this module on another OS is fine: only its pure functions (the parsers) work there, which is
what the tests use.
"""

import ctypes, fnmatch, os, re, struct, subprocess, time

# A USB serial port (macOS names each twice: /dev/cu.* to open, /dev/tty.* to wait for a carrier)
SERIAL = re.compile(r"/dev/(cu|tty)\.(usbmodem|usbserial|SLAB_USBtoUART|wchusbserial)", re.I)
DESKTOP = {"WindowServer"}
PS_FIELDS = "pid=,ppid=,uid=,time=,stat=,lstart=,comm="

def parse_cputime(text):
	"""Seconds from ps's time column: '0:00.96', '198:44.50', '1-02:03:04.5' or '02:03:04'; None if unreadable."""
	text = text.strip()
	days, _, text = text.rpartition("-")
	try:
		secs = 0.0
		for part in text.split(":"):
			secs = secs * 60 + float(part)
		return secs + (int(days) * 86400 if days else 0)
	except ValueError:
		return None

def parse_lstart(words):
	"""Seconds since 1970 (local time, whole seconds) from ps's lstart ('Wed Nov 18 00:35:21 2026'); None if unreadable."""
	try:
		return int(time.mktime(time.strptime(" ".join(words), "%a %b %d %H:%M:%S %Y")))
	except (ValueError, OverflowError):
		return None

def parse_ps(text):
	"""[(pid, ppid, uid, cpu s, start, stat, program path)] from ps -o PS_FIELDS (the C locale)."""
	out = []
	for line in (text or "").splitlines():
		f = line.split(None, 10)
		if len(f) < 11 or not (f[0].isdigit() and f[1].isdigit()):
			continue
		try:
			uid = int(f[2])
		except ValueError:
			continue
		out.append((int(f[0]), int(f[1]), uid, parse_cputime(f[3]), parse_lstart(f[5:10]), f[4], f[10].strip()))
	return out

def ps(*args):
	"""ps's output (the C locale, so lstart is in English), or '' if it fails."""
	try:
		r = subprocess.run(["/bin/ps"] + list(args), capture_output=True, text=True, timeout=20,
			env=dict(os.environ, LC_ALL="C", LANG="C"))
	except (OSError, subprocess.SubprocessError):
		return ""
	return r.stdout

def process_table():
	"""parse_ps for every process now."""
	return parse_ps(ps("-axww", "-o", PS_FIELDS))

def process_start(pid):
	"""When pid started (seconds since 1970), None if it isn't running or is a zombie; 0 if it runs but
	that is unknown."""
	rows = parse_ps(ps("-ww", "-o", PS_FIELDS, "-p", str(pid)))
	if not rows:
		return None
	_, _, _, _, start, stat, _ = rows[0]
	return None if "Z" in stat else (start or 0)

def program_name(path):
	"""The file name of a program path (/ or \\ separated: a Windows program's under Wine)."""
	return re.split(r"[\\/]", path or "")[-1]

def matches(name, pats):
	name = (name or "").lower()
	return any(fnmatch.fnmatchcase(name, p) for p in pats)

def parse_procargs(data):
	"""(argv, environment as [b'KEY=value', ...]) from sysctl KERN_PROCARGS2's buffer: argc (int), the
	program's path, NULs, then argc arguments and the environment, each NUL-terminated."""
	if len(data) < 4:
		return [], []
	argc = struct.unpack_from("i", data)[0]
	rest = data[4:]
	_, _, rest = rest.partition(b"\0")  # the program's path
	rest = rest.lstrip(b"\0")
	parts = rest.split(b"\0")
	argv = [a.decode(errors="replace") for a in parts[:argc]]
	env = []
	for item in parts[argc:]:
		if not item:
			break
		env.append(item)
	return argv, env

def job_of(env):
	"""RIG_JOB's value in an environment list, or None."""
	for item in env:
		if item.startswith(b"RIG_JOB="):
			return item[8:].decode(errors="replace")
	return None

# libproc and sysctl through ctypes (only on macOS)

class RUsageInfoV2(ctypes.Structure):
	_fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [(name, ctypes.c_uint64) for name in (
		"ri_user_time", "ri_system_time", "ri_pkg_idle_wkups", "ri_interrupt_wkups", "ri_pageins", "ri_wired_size",
		"ri_resident_size", "ri_phys_footprint", "ri_proc_start_abstime", "ri_proc_exit_abstime", "ri_child_user_time",
		"ri_child_system_time", "ri_child_pkg_idle_wkups", "ri_child_interrupt_wkups", "ri_child_pageins",
		"ri_child_elapsed_abstime", "ri_diskio_bytesread", "ri_diskio_byteswritten")]

class FdInfo(ctypes.Structure):
	_fields_ = [("proc_fd", ctypes.c_int32), ("proc_fdtype", ctypes.c_uint32)]

PROC_PIDLISTFDS, PROX_FDTYPE_VNODE, PROC_PIDFDVNODEPATHINFO = 1, 1, 2
RUSAGE_INFO_V2 = 2
# struct vnode_fdinfowithpath: proc_fileinfo (24 bytes), vnode_info (152), then the path (MAXPATHLEN, 1024)
VNODE_PATH_AT, VNODE_PATH_SIZE = 176, 1200
CTL_KERN, KERN_PROCARGS2 = 1, 49

_lib = {}

def lib():
	"""(libproc, libc), loaded once; None off macOS."""
	if "proc" not in _lib:
		try:
			_lib["proc"] = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
			_lib["c"] = ctypes.CDLL("/usr/lib/libc.dylib", use_errno=True)
			_lib["c"].sysctl.argtypes = (ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
				ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t)
		except OSError:
			_lib["proc"] = _lib["c"] = None
	return (_lib["proc"], _lib["c"]) if _lib["proc"] else None

def disk_io(pid):
	"""The bytes pid has read from and written to disk (a running count), or None if it can't be read."""
	libs = lib()
	if not libs:
		return None
	ru = RUsageInfoV2()
	if libs[0].proc_pid_rusage(pid, RUSAGE_INFO_V2, ctypes.byref(ru)) != 0:
		return None
	return ru.ri_diskio_bytesread + ru.ri_diskio_byteswritten

_vnode = ctypes.create_string_buffer(VNODE_PATH_SIZE)

def open_paths(pid):
	"""The paths of the files pid has open ([] if it can't be read)."""
	libs = lib()
	if not libs:
		return []
	proc = libs[0]
	size = proc.proc_pidinfo(pid, PROC_PIDLISTFDS, 0, None, 0)
	if size <= 0:
		return []
	n = size // ctypes.sizeof(FdInfo) + 16  # room for files opened since
	fds = (FdInfo * n)()
	size = proc.proc_pidinfo(pid, PROC_PIDLISTFDS, 0, fds, ctypes.sizeof(fds))
	out = []
	for i in range(max(0, size) // ctypes.sizeof(FdInfo)):
		if fds[i].proc_fdtype != PROX_FDTYPE_VNODE:
			continue
		if proc.proc_pidfdinfo(pid, fds[i].proc_fd, PROC_PIDFDVNODEPATHINFO, _vnode, VNODE_PATH_SIZE) == VNODE_PATH_SIZE:
			path = ctypes.string_at(ctypes.addressof(_vnode) + VNODE_PATH_AT, 1024).split(b"\0", 1)[0]
			if path:
				out.append(path.decode(errors="replace"))
	return out

_args = {}

def procargs(pid):
	"""(argv, environment) of pid from sysctl KERN_PROCARGS2, or None if it can't be read (another user's)."""
	libs = lib()
	if not libs:
		return None
	if "buf" not in _args:
		size = ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
		argmax = ctypes.c_int(0)
		mib = (ctypes.c_int * 2)(CTL_KERN, 8)  # KERN_ARGMAX
		libs[1].sysctl(mib, 2, ctypes.byref(argmax), ctypes.byref(size), None, 0)
		_args["buf"] = ctypes.create_string_buffer(max(argmax.value, 262144))
	buf = _args["buf"]
	mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
	size = ctypes.c_size_t(ctypes.sizeof(buf))
	if libs[1].sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
		return None
	return parse_procargs(ctypes.string_at(buf, size.value))

def getsid(pid):
	try:
		return os.getsid(pid)
	except OSError:
		return None

def serial_ports():
	"""The USB serial ports plugged in now (/dev/cu.*)."""
	try:
		return sorted("/dev/" + d for d in os.listdir("/dev") if d.startswith("cu.") and SERIAL.match("/dev/" + d))
	except OSError:
		return []
