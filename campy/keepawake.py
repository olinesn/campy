"""
Keep the computer awake while campy records (keepAwake, on by default).

A Windows PC that uses Modern Standby goes into standby once its screen times
out, even in the middle of a recording, when nobody is signed in or nobody
touches it: on a Core Ultra 9 285 PC whose screen turned off after 10 minutes,
campy fell from 80 to 25 frames a second. Sleep timeouts on Linux and macOS
would stop a recording the same way.

While campy records, Main holds a request that keeps the display and the
system on, so standby never starts:
	* Windows: a power request (PowerCreateRequest) for the display, the system
	  and execution. "powercfg /requests" (as administrator) lists it as
	  "campy is recording".
	* Linux: systemd-inhibit blocks idle and sleep. Over SSH, polkit allows
	  blocking idle but not sleep; campy then blocks idle and says so.
	* macOS: caffeinate keeps the display and the system awake.
Each camera process also asks Windows not to throttle it (execution
required), should standby start anyway (the power button, a policy).

Windows refuses the display request from a process with no screen session:
one started over SSH while nobody is signed in. The system and execution
requests still hold, and campy says the screen one was refused; set the
screen timeout to Never on such a PC.

The request ends with the recording, or with the process. A computer where it
cannot be made (no systemd-inhibit, say) records anyway, and campy says so.
"""

import os
import shutil
import subprocess
import sys

REASON = "campy is recording"

# POWER_REQUEST_TYPE
_DISPLAY_REQUIRED = 0
_SYSTEM_REQUIRED = 1
_EXECUTION_REQUIRED = 3


class KeepAwake:
	# with KeepAwake(): ... keeps the computer awake inside the block.
	# whole=False asks only that this process not be throttled (Windows), as
	# each camera process does; the whole computer is kept awake by Main.
	def __init__(self, enabled=True, whole=True, reason=REASON):
		self.enabled = enabled
		self.whole = whole
		self.reason = reason
		self.how = None # what keeps the computer awake, once started
		self.problem = None # why it could not, if it could not
		self._handle = None
		self._types = []
		self.refused = [] # Windows requests refused: "screen", "system", "execution"
		self._process = None

	def __enter__(self):
		self.Start()
		return self

	def __exit__(self, *exc):
		self.Stop()
		return False

	def Start(self):
		if not self.enabled:
			return self
		try:
			if sys.platform == "win32":
				self._StartWindows()
			elif self.whole and sys.platform == "darwin":
				# -w: held until campy ends, even if it ends without stopping it
				self._StartCommand(["caffeinate", "-d", "-i", "-m", "-s", "-w", str(os.getpid())], "caffeinate")
			elif self.whole:
				# Held until campy ends, even if it ends without stopping it. Sleep
				# and idle if allowed, else idle only (over SSH)
				for what in ("idle:sleep", "idle"):
					try:
						self._StartCommand(["systemd-inhibit", "--what=" + what, "--who=campy",
							"--why=" + self.reason, "--mode=block", "tail", "--pid={}".format(os.getpid()),
							"-f", "/dev/null"], "systemd-inhibit, blocking " + what.replace(":", " and "))
						break
					except RuntimeError as e:
						if what == "idle":
							raise
						self.refused.append("sleep")
				if self.refused:
					self.how += "; sleep refused"
		except Exception as e:
			self.problem = "{}: {}".format(type(e).__name__, e)
		return self

	def Stop(self):
		if self._handle is not None:
			k32 = _Kernel32()
			for kind in self._types:
				k32.PowerClearRequest(self._handle, kind)
			k32.CloseHandle(self._handle)
			self._handle = None
			self._types = []
		if self._process is not None:
			self._process.terminate()
			try:
				self._process.wait(timeout=5)
			except subprocess.TimeoutExpired:
				self._process.kill()
			self._process = None

	def _StartWindows(self):
		import ctypes
		k32 = _Kernel32()
		text = ctypes.create_unicode_buffer(self.reason)
		context = _ReasonContext()
		context.Version = 0 # POWER_REQUEST_CONTEXT_VERSION
		context.Flags = 0x1 # POWER_REQUEST_CONTEXT_SIMPLE_STRING
		context.Reason.SimpleReasonString = ctypes.cast(text, ctypes.c_wchar_p)
		handle = k32.PowerCreateRequest(ctypes.byref(context))
		if not handle or handle == ctypes.c_void_p(-1).value:
			raise ctypes.WinError(ctypes.get_last_error())
		self._handle = handle
		kinds = [_DISPLAY_REQUIRED, _SYSTEM_REQUIRED, _EXECUTION_REQUIRED] if self.whole else [_EXECUTION_REQUIRED]
		names = {_DISPLAY_REQUIRED: "screen", _SYSTEM_REQUIRED: "system", _EXECUTION_REQUIRED: "execution"}
		for kind in kinds:
			if k32.PowerSetRequest(handle, kind):
				self._types.append(kind)
			else:
				self.refused.append(names[kind])
		if not self._types:
			error = ctypes.WinError(ctypes.get_last_error())
			self.Stop()
			raise error
		self.how = "a Windows power request"
		if self.refused:
			self.how += "; {} refused{}".format(", ".join(self.refused),
				", as nobody is signed in" if "screen" in self.refused else "")

	def _StartCommand(self, command, name):
		if shutil.which(command[0]) is None:
			raise FileNotFoundError("{} not found".format(command[0]))
		process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
			stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
		# A lock refused (systemd-inhibit: "Access denied") ends the command at once
		try:
			process.wait(timeout=0.5)
		except subprocess.TimeoutExpired:
			process.stderr.close()
			self._process = process
			self.how = name
			return
		raise RuntimeError(process.stderr.read().decode(errors="replace").strip()
			or "{} ended at once".format(command[0]))


def _Kernel32():
	import ctypes
	from ctypes import wintypes
	k32 = ctypes.WinDLL("kernel32", use_last_error=True)
	k32.PowerCreateRequest.restype = wintypes.HANDLE
	k32.PowerCreateRequest.argtypes = [ctypes.POINTER(_ReasonContext)]
	k32.PowerSetRequest.restype = wintypes.BOOL
	k32.PowerSetRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
	k32.PowerClearRequest.restype = wintypes.BOOL
	k32.PowerClearRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
	k32.CloseHandle.restype = wintypes.BOOL
	k32.CloseHandle.argtypes = [wintypes.HANDLE]
	return k32


if sys.platform == "win32":
	import ctypes
	from ctypes import wintypes

	class _Detailed(ctypes.Structure):
		_fields_ = [("LocalizedReasonModule", wintypes.HMODULE), ("LocalizedReasonId", wintypes.ULONG),
			("ReasonStringCount", wintypes.ULONG), ("ReasonStrings", ctypes.POINTER(wintypes.LPWSTR))]

	class _Reason(ctypes.Union):
		_fields_ = [("Detailed", _Detailed), ("SimpleReasonString", ctypes.c_wchar_p)]

	class _ReasonContext(ctypes.Structure): # REASON_CONTEXT
		_fields_ = [("Version", wintypes.ULONG), ("Flags", wintypes.DWORD), ("Reason", _Reason)]
else:
	_ReasonContext = None
