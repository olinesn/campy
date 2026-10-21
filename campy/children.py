"""
Child processes that end with the program that started them.

subprocess.Popen's child lives on when its parent is killed (Stop-Process
or taskkill on Windows, a crash, its console closed): campy-live's nvidia-smi,
reading the GPU's clock every half second, kept polling the GPU, and kept
its working folder from being moved, long after the campy-live that started
it was gone. Popen(command, ...) here starts the child so that it dies with
its parent, however the parent ends:

- Windows: in a job object of its own that kills its processes when the
  job's last handle closes. The parent holds the only handle, and Windows
  closes it when the parent ends.
- Linux: with PR_SET_PDEATHSIG, so the kernel sends the child SIGKILL when
  the parent dies. Strictly, when the parent's thread that started the child
  ends: start it from a thread that lives as long as the program (the main
  thread).
- Elsewhere (macOS): as plain Popen.

The child also starts in the system's temporary folder (unless cwd is
given), so even a child that outlives its parent can't hold a recording's
folder. Close() (or Popen's own terminate and wait) still ends it early.

Only the standard library: campy-live's environment imports this.
"""

import os, subprocess, sys, tempfile

_CREATE_NO_WINDOW = 0x08000000
_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PR_SET_PDEATHSIG = 1


class Child(subprocess.Popen):
	"""
	subprocess.Popen, but the child dies with this process (see above).
	tied says how: "a job object", "PR_SET_PDEATHSIG" or None (it can
	outlive this process). On Windows it has no console window.
	"""

	def __init__(self, args, **kwargs):
		kwargs.setdefault("cwd", tempfile.gettempdir())
		self.tied, self._job = None, None
		if sys.platform == "win32":
			kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
			super().__init__(args, **kwargs)
			try:
				self._job = _Job(self._handle)
				self.tied = "a job object"
			except OSError:
				pass # not tied; Close() still ends it
		elif sys.platform.startswith("linux") and "preexec_fn" not in kwargs:
			tie = _DeathSignal()
			if tie is not None:
				kwargs["preexec_fn"] = tie
				self.tied = "PR_SET_PDEATHSIG"
			super().__init__(args, **kwargs)
		else:
			super().__init__(args, **kwargs)

	def Close(self, timeout=5):
		"""Ends the child (terminate, then kill after timeout s) and waits for it."""
		if self.poll() is None:
			self.terminate()
			try:
				self.wait(timeout=timeout)
			except subprocess.TimeoutExpired:
				self.kill()
				self.wait()
		if self._job is not None:
			_Kernel32().CloseHandle(self._job)
			self._job = None


def _Job(process_handle):
	# A job that kills its processes when its last handle closes, with the child in it
	import ctypes
	from ctypes import wintypes

	class Basic(ctypes.Structure):
		_fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
			("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
			("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
			("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
			("SchedulingClass", wintypes.DWORD)]

	class Io(ctypes.Structure):
		_fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount",
			"OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

	class Extended(ctypes.Structure):
		_fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Io),
			("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
			("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

	k32 = _Kernel32()
	job = k32.CreateJobObjectW(None, None) # its handle isn't inherited
	if not job:
		raise ctypes.WinError(ctypes.get_last_error())
	info = Extended()
	info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	if not k32.SetInformationJobObject(job, _JobObjectExtendedLimitInformation,
			ctypes.byref(info), ctypes.sizeof(info)) or not k32.AssignProcessToJobObject(job, int(process_handle)):
		error = ctypes.WinError(ctypes.get_last_error())
		k32.CloseHandle(job)
		raise error
	return job


def _Kernel32():
	import ctypes
	from ctypes import wintypes
	k32 = ctypes.WinDLL("kernel32", use_last_error=True)
	k32.CreateJobObjectW.restype = wintypes.HANDLE
	k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
	k32.SetInformationJobObject.restype = wintypes.BOOL
	k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
	k32.AssignProcessToJobObject.restype = wintypes.BOOL
	k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
	k32.CloseHandle.restype = wintypes.BOOL
	k32.CloseHandle.argtypes = [wintypes.HANDLE]
	return k32


def _DeathSignal():
	# For preexec_fn: prctl found here, in the parent, so the child (between
	# fork and exec) only calls it. None without libc's prctl.
	import ctypes, signal
	try:
		prctl = ctypes.CDLL(None, use_errno=True).prctl
	except (OSError, AttributeError):
		return None
	prctl.restype = ctypes.c_int
	prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
	parent = os.getpid()

	def Tie():
		prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
		if os.getppid() != parent: # the parent died before the tie was made
			os._exit(1)

	return Tie
