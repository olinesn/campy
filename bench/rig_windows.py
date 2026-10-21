"""
The Windows half of bench/rig.py: which processes run, what each uses, and the CPU and disk each
takes, read from Windows itself through ctypes (Python's standard library only; 64-bit Python).

What a process counts as using:
  gpu      a CUDA program, as nvidia-smi lists it (rig.py asks), or named python*.exe
           (RIG_GPU_NAMES, comma-separated patterns): on a test PC every Python process is one
           that may start CUDA at any moment (campy-live, sleap-nn, a benchmark), listed or not
  encoder  named ffmpeg*.exe (RIG_ENCODER_NAMES): campy's encoders
  camera   has a Spinnaker DLL loaded (RIG_CAMERA_MODULES): PySpin, SpinView, campy's cameras
  trigger  has a COM port open (any port in HKLM\\HARDWARE\\DEVICEMAP\\SERIALCOMM), found in the
           system's handle table; RIG_EXTRA can add other devices by their NT name, e.g.
           RIG_EXTRA="trigger=\\Device\\Null"
  desktop  a program nvidia-smi lists as drawing on the GPU (dwm.exe, explorer.exe, ...)
Another rig.py (waiting, watching or showing status) is a Python process that uses nothing.

Every process is seen, other users' too; only the ones this user can open show their DLLs, their
open COM ports and their command lines.

Importing this module on another OS is fine: only its pure functions (classify, the parsers) work
there, which is what the tests use.
"""

import ctypes, fnmatch, os, re, struct, threading, time

def patterns(var, default):
	return [s.strip().lower() for s in os.environ.get(var, default).split(",") if s.strip()]

GPU_NAMES = patterns("RIG_GPU_NAMES", "python*.exe")
ENCODER_NAMES = patterns("RIG_ENCODER_NAMES", "ffmpeg*.exe")
CAMERA_MODULES = patterns("RIG_CAMERA_MODULES", "spinnaker*.dll")
RIG_PY = re.compile(r"rig\.py\W+(run|hold|status|break)\b", re.I)
NOT_PROCESSES = (0, 4)  # the idle "process" (its CPU time is idle time) and the kernel

def matches(name, pats):
	name = name.lower()
	return any(fnmatch.fnmatchcase(name, p) for p in pats)

def classify(table, gpu_pids=(), camera_pids=(), ports=None, jobs=None, cmdline=lambda pid, start: None,
		desktop_pids=()):
	"""Records for rig.py, one per process: (pid, ppid, start, cpu s, io bytes, kinds, name, job).

	table: (pid, ppid, image name, start, cpu s, io bytes) per process; gpu_pids: CUDA programs;
	camera_pids: with Spinnaker loaded; ports: {pid: {kind}} from open devices; jobs: {job: pids};
	desktop_pids: programs that draw on the GPU.
	"""
	ports, jobs = ports or {}, jobs or {}
	in_job = {pid: job for job, pids in jobs.items() for pid in pids}
	out = []
	for pid, ppid, name, start, cpu, io in table:
		kinds = set(ports.get(pid, ()))
		python = matches(name, GPU_NAMES)
		if pid in gpu_pids or python:
			kinds.add("gpu")
		if matches(name, ENCODER_NAMES):
			kinds.add("encoder")
		if pid in camera_pids:
			kinds.add("camera")
		if python and pid not in gpu_pids and kinds == {"gpu"} and RIG_PY.search(cmdline(pid, start) or ""):
			kinds = set()  # another rig.py
		if not kinds and pid in desktop_pids:
			kinds.add("desktop")
		out.append((pid, ppid, start, cpu, io, kinds, name, in_job.get(pid)))
	return out

if os.name == "nt":
	import msvcrt, winreg
	from ctypes import wintypes

	k32 = ctypes.WinDLL("kernel32", use_last_error=True)
	ntdll = ctypes.WinDLL("ntdll")
	H, DWORD, BOOL, ULONG = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.c_ulong
	PULL = ctypes.POINTER(ctypes.c_ulonglong)

	def api(dll, name, res, *args):
		f = getattr(dll, name)
		f.restype, f.argtypes = res, args
		return f

	OpenProcess = api(k32, "OpenProcess", H, DWORD, BOOL, DWORD)
	CloseHandle = api(k32, "CloseHandle", BOOL, H)
	GetCurrentProcess = api(k32, "GetCurrentProcess", H)
	GetExitCodeProcess = api(k32, "GetExitCodeProcess", BOOL, H, ctypes.POINTER(DWORD))
	GetProcessTimes = api(k32, "GetProcessTimes", BOOL, H, PULL, PULL, PULL, PULL)
	DuplicateHandle = api(k32, "DuplicateHandle", BOOL, H, H, H, ctypes.POINTER(H), DWORD, BOOL, DWORD)
	GetFileType = api(k32, "GetFileType", DWORD, H)
	CreateToolhelp32Snapshot = api(k32, "CreateToolhelp32Snapshot", H, DWORD, DWORD)
	Module32FirstW = api(k32, "Module32FirstW", BOOL, H, ctypes.c_void_p)
	Module32NextW = api(k32, "Module32NextW", BOOL, H, ctypes.c_void_p)
	CreateJobObjectW = api(k32, "CreateJobObjectW", H, ctypes.c_void_p, wintypes.LPCWSTR)
	AssignProcessToJobObject = api(k32, "AssignProcessToJobObject", BOOL, H, H)
	QueryInformationJobObject = api(k32, "QueryInformationJobObject", BOOL, H, ctypes.c_int, ctypes.c_void_p,
		DWORD, ctypes.POINTER(DWORD))
	TerminateJobObject = api(k32, "TerminateJobObject", BOOL, H, wintypes.UINT)
	NtQuerySystemInformation = api(ntdll, "NtQuerySystemInformation", ctypes.c_long, ctypes.c_int,
		ctypes.c_void_p, ULONG, ctypes.POINTER(ULONG))
	NtQueryInformationProcess = api(ntdll, "NtQueryInformationProcess", ctypes.c_long, H, ctypes.c_int,
		ctypes.c_void_p, ULONG, ctypes.POINTER(ULONG))
	NtQueryObject = api(ntdll, "NtQueryObject", ctypes.c_long, H, ctypes.c_int, ctypes.c_void_p, ULONG,
		ctypes.POINTER(ULONG))
	NtResumeProcess = api(ntdll, "NtResumeProcess", ctypes.c_long, H)

	class MODULEENTRY32W(ctypes.Structure):
		_fields_ = [("dwSize", DWORD), ("th32ModuleID", DWORD), ("th32ProcessID", DWORD),
			("GlblcntUsage", DWORD), ("ProccntUsage", DWORD), ("modBaseAddr", ctypes.c_void_p),
			("modBaseSize", DWORD), ("hModule", ctypes.c_void_p), ("szModule", ctypes.c_wchar * 256),
			("szExePath", ctypes.c_wchar * 260)]

INVALID_HANDLE = ctypes.c_void_p(-1).value
QUERY_LIMITED, DUP_HANDLE, SET_QUOTA, TERMINATE, SUSPEND_RESUME = 0x1000, 0x40, 0x100, 0x1, 0x800
STILL_ACTIVE, ACCESS_DENIED = 259, 5
LENGTH_MISMATCH, BUFFER_TOO_SMALL = -0x3FFFFFFC, -0x3FFFFFDD  # STATUS_INFO_LENGTH_MISMATCH, _BUFFER_TOO_SMALL

def unicode_string(buf, off=0):
	"""The text of a UNICODE_STRING at buf[off] whose characters are elsewhere in memory."""
	n, ptr = struct.unpack_from("<H6xQ", buf, off)
	return ctypes.wstring_at(ptr, n // 2) if ptr and n else ""

_bufs = {}

def system_information(cls, size):
	"""NtQuerySystemInformation(cls) into a buffer kept between calls."""
	buf = _bufs.get(cls)
	while True:
		if buf is None or len(buf) < size:
			buf = _bufs[cls] = ctypes.create_string_buffer(size)
		need = ULONG()
		status = NtQuerySystemInformation(cls, buf, len(buf), ctypes.byref(need))
		if status == LENGTH_MISMATCH:
			size = max(need.value, len(buf)) * 3 // 2
			continue
		if status < 0:
			raise OSError("NtQuerySystemInformation(%d) failed: 0x%08X" % (cls, status & 0xFFFFFFFF))
		return buf

def process_table():
	"""(pid, ppid, image name, start, cpu s, io bytes) for every process (SYSTEM_PROCESS_INFORMATION).

	start is the creation time (100 ns units since 1601), cpu the user + kernel time, io the bytes
	read and written (files, devices and network)."""
	buf = system_information(5, 1 << 20)
	out, off = [], 0
	while True:
		step = struct.unpack_from("<I", buf, off)[0]
		create, user, kernel = struct.unpack_from("<qqq", buf, off + 0x20)
		pid, ppid = struct.unpack_from("<QQ", buf, off + 0x50)
		read, written = struct.unpack_from("<qq", buf, off + 0xE8)
		if pid:
			out.append((pid, ppid, unicode_string(buf, off + 0x38), create, (user + kernel) / 1e7, read + written))
		if not step:
			return out
		off += step

def process_start(pid):
	"""pid's creation time if it runs, None if not, -1 if it runs but its times can't be read."""
	h = OpenProcess(QUERY_LIMITED, False, pid)
	if not h:
		return -1 if ctypes.get_last_error() == ACCESS_DENIED else None
	try:
		code = DWORD()
		if GetExitCodeProcess(h, ctypes.byref(code)) and code.value != STILL_ACTIVE:
			return None
		t = [ctypes.c_ulonglong() for _ in range(4)]
		if not GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
			return -1
		return t[0].value
	finally:
		CloseHandle(h)

_cmds = {}

def command_line(pid, start):
	"""pid's command line, or None if it can't be read."""
	key = (pid, start)
	if key in _cmds:
		return _cmds[key]
	text, h = None, OpenProcess(QUERY_LIMITED, False, pid)
	if h:
		try:
			size = 4096
			while size <= 1 << 20:
				buf, need = ctypes.create_string_buffer(size), ULONG()
				status = NtQueryInformationProcess(h, 60, buf, size, ctypes.byref(need))  # ProcessCommandLineInformation
				if status in (LENGTH_MISMATCH, BUFFER_TOO_SMALL):
					size = max(need.value, size * 2)
					continue
				if status >= 0:
					text = " ".join(unicode_string(buf).split())
				break
		finally:
			CloseHandle(h)
	if len(_cmds) > 5000:
		_cmds.clear()
	_cmds[key] = text
	return text

def modules(pid):
	"""The lower-case names of the DLLs pid has loaded; None if they can't be read now, () if never."""
	for _ in range(3):
		snap = CreateToolhelp32Snapshot(0x18, pid)  # TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32
		if snap and snap != INVALID_HANDLE:
			break
		err = ctypes.get_last_error()
		if err in (ACCESS_DENIED, 87):  # not ours, or gone
			return ()
		if err != 24:  # ERROR_BAD_LENGTH: the process was loading a DLL; try again
			return None
	else:
		return None
	try:
		e = MODULEENTRY32W()
		e.dwSize = ctypes.sizeof(e)
		names, ok = [], Module32FirstW(snap, ctypes.byref(e))
		while ok:
			names.append(e.szModule.lower())
			ok = Module32NextW(snap, ctypes.byref(e))
		return names
	finally:
		CloseHandle(snap)

_cam = {}  # (pid, start): Spinnaker loaded

def camera_pids(table):
	"""The pids with a Spinnaker DLL loaded. A process is looked at when first seen; Python
	processes, which load PySpin when they import it, every time until they have."""
	out, keep = set(), {}
	for pid, ppid, name, start, cpu, io in table:
		if pid in NOT_PROCESSES:
			continue
		key = (pid, start)
		has = _cam.get(key)
		if has is None or (not has and matches(name, GPU_NAMES)):
			mods = modules(pid)
			if mods is None:
				continue
			has = any(matches(m, CAMERA_MODULES) for m in mods)
		keep[key] = has
		if has:
			out.add(pid)
	_cam.clear()
	_cam.update(keep)
	return out

def serial_devices():
	"""{NT device name: COM port} for every serial port Windows has now, e.g. {'\\Device\\USBSER000': 'COM9'}."""
	try:
		k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
	except OSError:
		return {}
	out, i = {}, 0
	try:
		while True:
			try:
				name, value, _ = winreg.EnumValue(k, i)
			except OSError:
				return out
			out[name] = str(value)
			i += 1
	finally:
		winreg.CloseKey(k)

# Open COM ports, from the system's handle table. Each handle that could be one (a file opened to
# read and write) is duplicated into rig.py once and named, in a worker thread that is given up on
# after 0.25 s: naming a synchronous handle with a read pending (a pipe or a console waiting for
# input) waits for that read. Handles are remembered by (pid, start, handle, access, object), so
# only new ones are looked at; where Windows hides the object's address (Windows 11 24H2, for
# users without debug rights) a handle number that was closed and reused would look the same, so
# every handle is looked at again every 30 s.

_file_type = [None]
_names = {}  # key: NT name, or "" (not a device of interest's kind: a disk file, a pipe, ...)
_hung = set()
_last_full = [0.0]
RECHECK = 30.0

def handle_table():
	"""(objects, pids, handles, access, types) of every handle in the system, as lists."""
	buf = system_information(64, 4 << 20)  # SystemExtendedHandleInformation
	n = struct.unpack_from("<Q", buf, 0)[0]
	q = memoryview(ctypes.string_at(ctypes.addressof(buf) + 16, 40 * n)).cast("Q")
	info = q[3::5].tolist()  # GrantedAccess, CreatorBackTraceIndex, ObjectTypeIndex
	return q[0::5].tolist(), q[1::5].tolist(), q[2::5].tolist(), [x & 0xFFFFFFFF for x in info], [x >> 48 for x in info]

def file_type_index():
	"""The handle table's type number for files, from a file rig.py opens itself."""
	if _file_type[0] is None:
		with open(os.devnull, "rb") as f:
			h = msvcrt.get_osfhandle(f.fileno())
			objs, pids, handles, access, types = handle_table()
			me = os.getpid()
			_file_type[0] = next((types[i] for i in range(len(pids)) if pids[i] == me and handles[i] == h), -1)
	return _file_type[0]

def handle_name(hproc, handle):
	"""The NT name of a character device handle in another process, else ''."""
	dup = H()
	if not DuplicateHandle(hproc, handle, GetCurrentProcess(), ctypes.byref(dup), 0, False, 2):  # SAME_ACCESS
		return ""
	try:
		if GetFileType(dup) != 2:  # FILE_TYPE_CHAR: serial ports, NUL, consoles
			return ""
		buf, need = ctypes.create_string_buffer(2048), ULONG()
		if NtQueryObject(dup, 1, buf, len(buf), ctypes.byref(need)) < 0:  # ObjectNameInformation
			return ""
		return unicode_string(buf)
	finally:
		CloseHandle(dup)

def name_handles(todo):
	"""{key: name} for keys (pid, start, handle, access, object), with a 0.25 s limit per handle."""
	out, procs = {}, {}
	for key in todo:
		if key[0] not in procs:
			procs[key[0]] = OpenProcess(DUP_HANDLE, False, key[0])
	pending = [k for k in todo if procs[k[0]]]
	for k in todo:
		if not procs[k[0]]:
			out[k] = ""
	try:
		i = 0
		while i < len(pending):
			state = {"i": i, "stop": False}
			def work(state=state):
				while state["i"] < len(pending) and not state["stop"]:
					k = pending[state["i"]]
					name = handle_name(procs[k[0]], k[2])
					if not state["stop"]:
						out[k] = name
						state["i"] += 1
			t = threading.Thread(target=work, daemon=True)
			t.start()
			last = -1
			while t.is_alive():
				t.join(0.25)
				if t.is_alive() and state["i"] == last:  # stuck on one handle: leave it
					state["stop"] = True
					_hung.add(pending[last])
					out[pending[last]] = ""
					break
				last = state["i"]
			i = state["i"] + 1 if state["stop"] else len(pending)
	finally:
		for h in procs.values():
			if h:
				CloseHandle(h)
	return out

def port_users(table, own=(), extra=None):
	"""{pid: {kind}} for every process outside own with a COM port (trigger) or a RIG_EXTRA device open."""
	wanted = {d.lower(): "trigger" for d in serial_devices()}
	wanted.update({p.lower(): k for p, k in (extra or {}).items() if p.startswith("\\")})
	ft = file_type_index() if wanted else -1
	if ft < 0:
		return {}
	starts = {row[0]: row[3] for row in table}
	objs, pids, handles, access, types = handle_table()
	hidden = not any(objs[:1000])
	now = time.monotonic()
	full = hidden and now - _last_full[0] > RECHECK
	if full:
		_last_full[0] = now
	keys, todo = [], []
	for i in range(len(pids)):
		pid = pids[i]
		if types[i] != ft or access[i] & 3 != 3 or pid in own or pid in NOT_PROCESSES or pid not in starts:
			continue
		key = (pid, starts[pid], handles[i], access[i], objs[i])
		keys.append(key)
		if key not in _hung and (key not in _names or (full and _names[key] == "")):
			todo.append(key)
	if todo:
		_names.update(name_handles(todo))
	out = {}
	for key in keys:
		kind = wanted.get(_names.get(key, "").lower())
		if kind:
			out.setdefault(key[0], set()).add(kind)
	alive = set(keys)
	for key in [k for k in _names if k not in alive]:
		del _names[key]
	_hung.intersection_update(alive)
	return out

class Job:
	"""A Windows job object: every process the job's first process starts belongs to it too."""

	def __init__(self):
		self.h = CreateJobObjectW(None, None)
		if not self.h:
			raise ctypes.WinError(ctypes.get_last_error())

	def add(self, pid):
		"""Put pid in the job; True if it is."""
		h = OpenProcess(SET_QUOTA | TERMINATE, False, pid)
		if not h:
			return False
		try:
			return bool(AssignProcessToJobObject(self.h, h))
		finally:
			CloseHandle(h)

	def pids(self):
		n = 256
		while True:
			buf = ctypes.create_string_buffer(8 + 8 * n)
			if QueryInformationJobObject(self.h, 3, buf, len(buf), None):  # JobObjectBasicProcessIdList
				count = struct.unpack_from("<I", buf, 4)[0]
				return set(struct.unpack_from("<%dQ" % count, buf, 8))
			if ctypes.get_last_error() != 234 or n > 1 << 16:  # ERROR_MORE_DATA
				return set()
			n *= 4

	def terminate(self, code=1):
		TerminateJobObject(self.h, code)

	def close(self):
		CloseHandle(self.h)

def resume(pid):
	"""Resume a process started suspended."""
	h = OpenProcess(SUSPEND_RESUME, False, pid)
	if h:
		NtResumeProcess(h)
		CloseHandle(h)

def scan(jobs=None, gpu_pids=(), extra=None, own=(), desktop_pids=()):
	"""Records (see classify) for every process. jobs: {job name: Job}; own: pids whose COM ports
	needn't be looked at (rig.py's job and rig.py itself)."""
	table = process_table()
	members = {name: job.pids() for name, job in (jobs or {}).items()}
	skip = set(own) | {os.getpid()}
	for pids in members.values():
		skip |= pids
	return classify(table, set(gpu_pids), camera_pids(table), port_users(table, skip, extra), members, command_line,
		set(desktop_pids))
