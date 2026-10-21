"""
Which CPUs a program runs on, and at what priority: for campy-live (--cpus,
--priority, --campy-cpus) and campy-live-view's c key, with nothing but the
standard library.

Intel's hybrid CPUs (12th generation Core on, Core Ultra) have performance
cores (P-cores) and efficiency cores (E-cores). Windows numbers them in no
simple order: on a Core Ultra 9 285 the 8 P-cores are 0, 1, 10-13, 22 and 23.
Cores() finds them, from Windows (GetLogicalProcessorInformationEx, the
cores' efficiency class) or Linux (/sys/devices/cpu_core and cpu_atom).

	python -m campy.cpus

says them, with the affinity masks Windows' start /affinity takes.

Pinning a process (Pin) puts all its threads on those CPUs, and the programs
it starts after inherit it (on Windows and Linux): campy-acquire's camera
processes and their ffmpeg encoders follow campy-acquire.
"""

import functools, os, struct, sys

PRIORITIES = ("normal", "above_normal", "high")

# Windows' priority classes (SetPriorityClass); realtime is left out on purpose:
# it can starve the mouse, the disk and campy itself
_WINDOWS_CLASSES = {"normal": 0x20, "above_normal": 0x8000, "high": 0x80}
_WINDOWS_NAMES = {v: k for k, v in _WINDOWS_CLASSES.items()}
# And their nearest POSIX niceness (raising it needs root or CAP_SYS_NICE)
_NICENESS = {"normal": 0, "above_normal": -5, "high": -10}

_SET_INFORMATION, _QUERY_LIMITED = 0x0200, 0x1000 # OpenProcess rights


def ParseList(text):
	"""'0,1,10-13' -> [0, 1, 10, 11, 12, 13]"""
	cpus = set()
	for part in str(text).replace(" ", "").split(","):
		if not part:
			continue
		a, _, b = part.partition("-")
		try:
			first, last = int(a), int(b or a)
		except ValueError:
			raise ValueError("not a CPU list: {!r} (give it as e.g. 0,1,10-13)".format(text))
		if first < 0 or last < first:
			raise ValueError("not a CPU list: {!r} (give it as e.g. 0,1,10-13)".format(text))
		cpus.update(range(first, last + 1))
	if not cpus:
		raise ValueError("no CPUs in {!r}".format(text))
	return sorted(cpus)


def Text(cpus):
	"""[0, 1, 10, 11, 12, 13] -> '0,1,10-13'"""
	cpus, parts = sorted(cpus), []
	i = 0
	while i < len(cpus):
		j = i
		while j + 1 < len(cpus) and cpus[j + 1] == cpus[j] + 1:
			j += 1
		parts.append(str(cpus[i]) if i == j else "{}-{}".format(cpus[i], cpus[j]))
		i = j + 1
	return ",".join(parts)


def Mask(cpus):
	"""The affinity mask, as Windows' start /affinity takes it (hexadecimal)"""
	return "{:X}".format(sum(1 << c for c in cpus))


# ---------------------------------------------------------------- the cores

@functools.lru_cache(maxsize=None)
def _CoresOnce():
	try:
		if sys.platform == "win32":
			return _WindowsCores()
		if sys.platform.startswith("linux"):
			return _LinuxCores()
	except OSError:
		pass
	return None


def Cores():
	"""
	{"performance": [...], "efficiency": [...]}: the logical CPUs of each
	kind; efficiency empty on a CPU of one kind. None where it can't be told.
	"""
	cores = _CoresOnce() # they don't change while a program runs
	return None if cores is None else {k: list(v) for k, v in cores.items()}


def Hybrid():
	"""The performance and efficiency cores, on a CPU that has both; else None"""
	cores = Cores()
	return cores if cores and cores["efficiency"] else None


def _WindowsCores():
	import ctypes
	from ctypes import wintypes
	kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
	size = wintypes.DWORD(0)
	RelationProcessorCore = 0
	kernel32.GetLogicalProcessorInformationEx(RelationProcessorCore, None, ctypes.byref(size))
	buffer = ctypes.create_string_buffer(size.value)
	if not kernel32.GetLogicalProcessorInformationEx(RelationProcessorCore, buffer, ctypes.byref(size)):
		raise ctypes.WinError(ctypes.get_last_error())
	return _ByClass(_ParseCores(buffer.raw[:size.value]))


def _ParseCores(data):
	# SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX entries: Relationship, Size,
	# then PROCESSOR_RELATIONSHIP: Flags, EfficiencyClass, Reserved[20],
	# GroupCount, GROUP_AFFINITY GroupMask[] (Mask, Group, Reserved[3])
	offset, cores = 0, []
	while offset + 8 <= len(data):
		relationship, entry = struct.unpack_from("<II", data, offset)
		if entry <= 0:
			break
		if relationship == 0: # RelationProcessorCore
			efficiency, = struct.unpack_from("<B", data, offset + 9)
			groups, = struct.unpack_from("<H", data, offset + 30)
			cpus = []
			for g in range(groups):
				mask, group = struct.unpack_from("<QH", data, offset + 32 + 16 * g)
				cpus += [64 * group + bit for bit in range(64) if mask >> bit & 1]
			cores.append((efficiency, cpus))
		offset += entry
	return cores


def _ByClass(cores):
	# The cores of the highest efficiency class are the performance ones (1 on
	# Intel's hybrid CPUs; 0 for every core on others)
	if not cores:
		return None
	best = max(c for c, _ in cores)
	return {"performance": sorted(cpu for c, cpus in cores if c == best for cpu in cpus),
		"efficiency": sorted(cpu for c, cpus in cores if c != best for cpu in cpus)}


def _LinuxCores():
	def Read(path):
		try:
			with open(path) as f:
				return ParseList(f.read().strip())
		except (OSError, ValueError):
			return None
	performance, efficiency = Read("/sys/devices/cpu_core/cpus"), Read("/sys/devices/cpu_atom/cpus")
	if performance and efficiency:
		return {"performance": performance, "efficiency": efficiency}
	return {"performance": sorted(os.sched_getaffinity(0)), "efficiency": []}


def All():
	"""Every CPU this computer has"""
	cores = Cores()
	if cores:
		return sorted(cores["performance"] + cores["efficiency"])
	return list(range(os.cpu_count() or 1))


def Resolve(text):
	"""
	'performance' (or 'p'), 'efficiency' ('e'), 'all', or a list such as
	0,1,10-13: the CPUs
	"""
	name = str(text).strip().lower()
	if name == "all":
		return All()
	kind = {"p": "performance", "p-cores": "performance", "performance": "performance",
		"e": "efficiency", "e-cores": "efficiency", "efficiency": "efficiency"}.get(name)
	if kind is None:
		return ParseList(text)
	cores = Cores()
	if cores is None:
		raise ValueError("can't tell this computer's {} cores: give the CPUs as a list, e.g. 0,1,10-13".format(kind))
	if not cores["efficiency"]:
		raise ValueError("this CPU's cores are all of one kind: no {} cores to choose".format(kind))
	return cores[kind]


# ---------------------------------------------------------------- processes

def _Windows():
	import ctypes
	kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
	kernel32.OpenProcess.restype = ctypes.c_void_p
	kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
	kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
	kernel32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
	kernel32.GetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
		ctypes.POINTER(ctypes.c_size_t)]
	kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
	kernel32.GetPriorityClass.argtypes = [ctypes.c_void_p]
	kernel32.GetPriorityClass.restype = ctypes.c_uint32
	return ctypes, kernel32


class _Process:
	# A Windows process handle, closed after the with block
	def __init__(self, pid, rights):
		self.ctypes, self.kernel32 = _Windows()
		self.pid, self.rights = int(pid if pid is not None else os.getpid()), rights

	def __enter__(self):
		self.handle = self.kernel32.OpenProcess(self.rights, False, self.pid)
		if not self.handle:
			raise self.ctypes.WinError(self.ctypes.get_last_error())
		return self

	def __exit__(self, *exc):
		self.kernel32.CloseHandle(self.handle)

	def Check(self, ok):
		if not ok:
			raise self.ctypes.WinError(self.ctypes.get_last_error())


def _Threads(pid):
	# Linux: affinity and niceness are each thread's
	pid = os.getpid() if pid is None else int(pid)
	try:
		return [int(t) for t in os.listdir("/proc/{}/task".format(pid))]
	except OSError:
		return [pid]


def Pin(cpus, pid=None):
	"""A process (this one when pid is None) onto these CPUs"""
	cpus = sorted(set(cpus))
	if sys.platform == "win32":
		if max(cpus) >= 64:
			raise OSError("CPUs past 63 are in another processor group, which this can't pin to")
		with _Process(pid, _SET_INFORMATION | _QUERY_LIMITED) as p:
			p.Check(p.kernel32.SetProcessAffinityMask(p.handle, sum(1 << c for c in cpus)))
	elif hasattr(os, "sched_setaffinity"):
		for tid in _Threads(pid):
			try:
				os.sched_setaffinity(tid, cpus)
			except ProcessLookupError: # a thread that ended
				pass
	else:
		raise OSError("pinning to CPUs isn't supported on {}".format(sys.platform))


def Affinity(pid=None):
	"""The CPUs a process may run on"""
	if sys.platform == "win32":
		with _Process(pid, _QUERY_LIMITED) as p:
			mine, system = p.ctypes.c_size_t(), p.ctypes.c_size_t()
			p.Check(p.kernel32.GetProcessAffinityMask(p.handle, p.ctypes.byref(mine), p.ctypes.byref(system)))
			return [b for b in range(64) if mine.value >> b & 1]
	if hasattr(os, "sched_getaffinity"):
		return sorted(os.sched_getaffinity(0 if pid is None else int(pid)))
	raise OSError("CPU affinity isn't supported on {}".format(sys.platform))


def SetPriority(name, pid=None):
	"""A process's priority: normal, above_normal or high"""
	if name not in _NICENESS:
		raise ValueError("priority {!r}: one of {}".format(name, ", ".join(PRIORITIES)))
	if sys.platform == "win32":
		with _Process(pid, _SET_INFORMATION | _QUERY_LIMITED) as p:
			p.Check(p.kernel32.SetPriorityClass(p.handle, _WINDOWS_CLASSES[name]))
	elif sys.platform.startswith("linux"):
		for tid in _Threads(pid):
			try:
				os.setpriority(os.PRIO_PROCESS, tid, _NICENESS[name])
			except ProcessLookupError:
				pass
	else:
		os.setpriority(os.PRIO_PROCESS, 0 if pid is None else int(pid), _NICENESS[name])


def Priority(pid=None):
	"""A process's priority, by name (or its Windows class / niceness when it is none of PRIORITIES)"""
	if sys.platform == "win32":
		with _Process(pid, _QUERY_LIMITED) as p:
			value = p.kernel32.GetPriorityClass(p.handle)
			p.Check(value)
			return _WINDOWS_NAMES.get(value, hex(value))
	nice = os.getpriority(os.PRIO_PROCESS, 0 if pid is None else int(pid))
	return {v: k for k, v in _NICENESS.items()}.get(nice, "nice {}".format(nice))


def Processes():
	"""{pid: parent pid} of every process"""
	if sys.platform == "win32":
		import ctypes
		from ctypes import wintypes

		class Entry(ctypes.Structure): # PROCESSENTRY32W
			_fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
				("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
				("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
				("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
		kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
		kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
		kernel32.Process32FirstW.argtypes = kernel32.Process32NextW.argtypes = [ctypes.c_void_p,
			ctypes.POINTER(Entry)]
		kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
		snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0) # TH32CS_SNAPPROCESS
		if not snapshot or snapshot == ctypes.c_void_p(-1).value:
			raise ctypes.WinError(ctypes.get_last_error())
		parents = {}
		try:
			entry = Entry()
			entry.dwSize = ctypes.sizeof(Entry)
			ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
			while ok:
				parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
				ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
		finally:
			kernel32.CloseHandle(snapshot)
		return parents
	parents = {}
	for name in os.listdir("/proc") if os.path.isdir("/proc") else []:
		if name.isdigit():
			try:
				with open("/proc/{}/stat".format(name)) as f:
					parents[int(name)] = int(f.read().rsplit(")", 1)[1].split()[1])
			except (OSError, IndexError, ValueError):
				pass
	return parents


def Tree(pids):
	"""These processes and every one they started (and those started), in no order"""
	parents = Processes()
	tree, grew = {int(p) for p in pids if int(p) in parents}, True
	while grew:
		more = {pid for pid, parent in parents.items() if parent in tree and pid not in tree}
		tree |= more
		grew = bool(more)
	return sorted(tree)


class Placement:
	"""
	campy-live on the performance cores at high priority, and campy-acquire
	(its camera processes and encoders) on the efficiency cores: On(True);
	or both on every CPU at normal priority: On(False). For campy-live-view's
	c key; campy-live starts this way itself (its --cpus auto). live: campy-
	live's process id; campy: campy-acquire's (its main process and its
	camera processes, as their feeds say them).
	"""

	def __init__(self, live=None, campy=()):
		self.live, self.campy = live, sorted(set(int(p) for p in campy if p))
		self.cores = Hybrid()

	def Available(self):
		return self.cores is not None and self.live is not None

	def IsOn(self):
		# campy-live on just the performance cores
		try:
			return Affinity(self.live) == self.cores["performance"]
		except (OSError, TypeError):
			return None

	def Set(self, on):
		"""Returns what was done, and what could not be, as text"""
		if self.cores is None:
			return "No efficiency cores on this computer's CPU: nothing to move."
		live_cpus, campy_cpus = (self.cores["performance"], self.cores["efficiency"]) if on else (All(), All())
		priority = "high" if on else "normal"
		done, failed = [], []
		if self.live is not None:
			try:
				Pin(live_cpus, self.live)
				SetPriority(priority, self.live)
				done.append("campy-live on CPUs {} at {} priority".format(Text(live_cpus), priority.replace("_", " ")))
			except OSError as e:
				failed.append("campy-live: {}".format(e))
		moved = 0
		if self.campy:
			try:
				tree = Tree(self.campy)
			except OSError as e:
				tree, failed = [], failed + ["campy-acquire's processes: {}".format(e)]
			for pid in tree:
				if pid in (self.live, os.getpid()):
					continue
				try:
					Pin(campy_cpus, pid)
					moved += 1
				except OSError as e:
					failed.append("process {}: {}".format(pid, e))
			if moved:
				done.append("campy-acquire ({} processes) on CPUs {}".format(moved, Text(campy_cpus)))
		text = "; ".join(done) or "Nothing moved"
		return text + (". Could not: " + "; ".join(failed) if failed else ".")


def Describe():
	cores = Cores()
	if cores is None:
		return "Can't tell this computer's kinds of cores ({}).".format(sys.platform)
	lines = []
	for kind in ("performance", "efficiency"):
		cpus = cores[kind]
		if cpus:
			lines.append("{} cores ({} CPUs): {}  (affinity mask {})".format(kind.capitalize(), len(cpus),
				Text(cpus), Mask(cpus)))
	if not cores["efficiency"]:
		lines.append("All of one kind: no efficiency cores.")
	elif sys.platform == "win32":
		lines.append('campy-acquire on the efficiency cores, from cmd: start "campy" /b /wait /affinity {} '
			"campy-acquire <config>".format(Mask(cores["efficiency"])))
	return "\n".join(lines)


if __name__ == "__main__":
	print(Describe())
