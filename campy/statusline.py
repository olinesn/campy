"""
One status line for campy-acquire's console, rewritten in place as it records
(statusLine), instead of every camera's own "CameraN collected N frames at X
fps for N sec." line every chunkLengthInSec: 4 cameras made 4 new lines every
5 s, some 400,000 in a 6-day session.

	Recording 3:00 · Camera0 10,800 · Camera1 10,800 · Camera2 10,800 · Camera3 10,800 frames · 60.0 fps

campy's main process draws it, about once a second, from every camera's
counters (status.py), which its recording watch reads too. Every other
message still prints as a line of its own: the status line is cleared before
it and drawn again after it, so a message never lands in the middle of a
half-drawn status line.

  - In the main process, sys.stdout and sys.stderr are wrapped (Console): a
    write clears the drawn status line first, and once every stream is back
    at the start of a line, the status line is drawn again under it.
  - The camera processes print through their printer threads
    (utils.StartPrinter), on the same console. Each of their lines starts
    with the clear (Child), which wipes the status line where it stands;
    the main process draws it again under that line at its next tick.

Only on a console (a TTY): stdout redirected to a file, a launcher's log,
a test or CI keeps the lines as they were (statusLine "auto", the default).
"always" draws it whatever stdout is; "never" never does.

The clear is ANSI's carriage return and erase-to-end-of-line ("\\r\\x1b[K").
On Windows, the console's virtual terminal processing is turned on for it
(Windows Terminal has it on; classic conhost and PowerShell 5 need it asked
for). Where it can't be, the line is cleared with spaces instead. The status
line is cut to the console's width less one, as a line that wraps can't be
rewritten in place.
"""

import os
import shutil
import sys
import threading
import time

from campy import status as camstatus

ENV = "CAMPY_STATUS_LINE" # set by the main process for its camera processes: "ansi" or "pad", ",stderr" if that too
EVERY_S = 1.0 # how often the status line is drawn
RATE_S = 5.0 # the frame rate shown: over the last frames this long
ANSI_CLEAR = "\r\x1b[K"
SAME_FPS = 0.5 # cameras' rates closer than this: one rate (the lowest), else each camera's

STATES = {camstatus.WAITING: "waiting", camstatus.ENDED: "ended", camstatus.NOT_STARTED: "not started",
	camstatus.LOST: "lost", camstatus.VIDEO_FAILED: "video stopped", camstatus.CAMERA_FAILED: "failed"}


# ---------------------------------------------------------------------- whether, and how


def Wanted(setting, stream=None):
	"""Whether to show it: statusLine "always", "never", or "auto" (when stream, stdout, is a console)."""
	setting = str(setting if setting is not None else "auto").lower()
	if setting == "always":
		return True
	if setting == "never":
		return False
	stream = sys.stdout if stream is None else stream
	try:
		return bool(stream.isatty())
	except Exception:
		return False


def EnableVT(stream=None):
	"""
	Whether ANSI's clear works on this stream's console. On Windows, turns
	the console's virtual terminal processing on (classic conhost and
	PowerShell 5 have it off; it is the console's, so the camera processes,
	which share it, get it too). Not a console: the clear is only written,
	for whatever reads it.
	"""
	if os.name != "nt":
		return True
	stream = sys.stdout if stream is None else stream
	try:
		if not stream.isatty():
			return True
		import ctypes
		import msvcrt
		from ctypes import wintypes
		kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
		handle = msvcrt.get_osfhandle(stream.fileno())
		mode = wintypes.DWORD()
		if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
			return False
		VT = 0x0004 # ENABLE_VIRTUAL_TERMINAL_PROCESSING
		if mode.value & VT:
			return True
		return bool(kernel32.SetConsoleMode(handle, mode.value | VT))
	except Exception:
		return False


def Width():
	# The console's columns, less one: a line as wide as the console wraps at once on Windows
	return max(20, shutil.get_terminal_size((100, 24)).columns - 1)


def ClearText(how):
	# What wipes the line the cursor is on: ANSI, or spaces where the console has no ANSI ("pad")
	if how == "pad":
		return "\r" + " " * Width() + "\r"
	return ANSI_CLEAR


def Prepare(params, stdout=None, stderr=None):
	"""
	In the main process, before the camera processes start: whether the
	status line is shown (statusLine), and if so, how it is cleared, which
	the camera processes read from their environment (ENV). Returns that
	(a Mode) or None.
	"""
	stdout = sys.stdout if stdout is None else stdout
	stderr = sys.stderr if stderr is None else stderr
	setting = params.get("statusLine", "auto")
	if not Wanted(setting, stdout):
		os.environ.pop(ENV, None)
		return None
	how = "ansi" if EnableVT(stdout) else "pad"
	forced = str(setting).lower() == "always"
	try:
		err = forced or bool(stderr.isatty())
	except Exception:
		err = forced
	os.environ[ENV] = how + (",stderr" if err else "")
	return Mode(how, err)


class Mode:
	def __init__(self, how, stderr):
		self.how, self.stderr = how, stderr


def Shown():
	"""Whether a status line is shown, in this process or its main process (then a camera's own progress lines aren't)."""
	return bool(os.environ.get(ENV))


def ChildMode():
	value = os.environ.get(ENV)
	if not value:
		return None
	parts = value.split(",")
	return Mode(parts[0], "stderr" in parts[1:])


# ---------------------------------------------------------------------- a camera process


class ChildStream:
	"""
	A camera process's stdout (or stderr): each line it writes starts with the
	clear, which wipes the main process's status line where it stands, and
	goes out whole (a print's text and its newline come as two writes; the
	status line drawn between them would wipe the text).
	"""

	def __init__(self, stream, how):
		self.stream, self.how = stream, how
		self.pending = ""
		self.lock = threading.RLock()

	def write(self, text):
		with self.lock:
			self.pending += text
			cut = self.pending.rfind("\n")
			if cut >= 0:
				lines, self.pending = self.pending[:cut + 1], self.pending[cut + 1:]
				self.stream.write(ClearText(self.how) + lines)
		return len(text)

	def flush(self):
		with self.lock:
			if self.pending:
				self.stream.write(ClearText(self.how) + self.pending)
				self.pending = ""
			self.stream.flush()

	def __getattr__(self, name):
		return getattr(self.stream, name)


def Child():
	"""In a camera process: its lines clear the status line first, when its main process shows one."""
	mode = ChildMode()
	if mode is None:
		return
	if not isinstance(sys.stdout, ChildStream):
		sys.stdout = ChildStream(sys.stdout, mode.how)
	if mode.stderr and not isinstance(sys.stderr, ChildStream):
		sys.stderr = ChildStream(sys.stderr, mode.how)


# ---------------------------------------------------------------------- the main process


class Console:
	"""
	The main process's console with the status line drawn at its bottom: its
	stdout and stderr (Stream) write through it. Before a write, the drawn
	status line is cleared; once each stream has ended its line, it is drawn
	again. Threads (the watch, the stop file's, a signal handler's Say) all
	write through the one lock.
	"""

	def __init__(self, out, err, how):
		self.out, self.err, self.how = out, err, how
		self.lock = threading.RLock()
		self.text = None # the status line, once there is one
		self.drawn = False
		self.midline = set() # streams whose last write did not end its line

	def Write(self, stream, text):
		if not text:
			return 0
		with self.lock:
			if self.drawn:
				self.out.write(ClearText(self.how))
				self.out.flush()
				self.drawn = False
			real = self.err if stream == "stderr" else self.out
			real.write(text)
			if text.endswith("\n"):
				self.midline.discard(stream)
			else:
				self.midline.add(stream)
			if stream == "stderr":
				real.flush()
			self.Redraw()
		return len(text)

	def Redraw(self):
		with self.lock:
			if self.text is None or self.midline:
				return
			self.out.write(ClearText(self.how) + self.text)
			self.out.flush()
			self.drawn = True

	def Draw(self, text):
		with self.lock:
			self.text = text
			self.Redraw()

	def Clear(self):
		with self.lock:
			if self.drawn:
				self.out.write(ClearText(self.how))
				self.out.flush()
				self.drawn = False
			self.text = None


class Stream:
	"""sys.stdout or sys.stderr in the main process, while the status line is shown."""

	def __init__(self, console, name, real):
		self.console, self.name, self.real = console, name, real

	def write(self, text):
		return self.console.Write(self.name, text)

	def flush(self):
		with self.console.lock:
			self.real.flush()

	def __getattr__(self, name):
		return getattr(self.real, name)


class StatusLine:
	"""
	The main process's status line: started once every camera is grabbing
	(or campy is waiting for the trigger), stopped before the recording's
	end is said. mode: Prepare's. counters: status.Make's array. names:
	the cameras'. clock and now_ns are the tests'.
	"""

	def __init__(self, mode, counters, names, frame_rate=None, clock=time.monotonic, now_ns=None):
		self.mode = mode
		self.names = list(names)
		self.reader = camstatus.Reader(counters, len(self.names))
		try:
			self.frameRate = float(frame_rate) if frame_rate else None
		except (TypeError, ValueError):
			self.frameRate = None # one per camera: the shortest pause then
		self.clock = clock
		self.nowNs = now_ns or camstatus.Now
		self.started = None # when it started (clock)
		self.firstFrame = None # when the first frame was seen (clock)
		self.history = [[] for _ in self.names] # each camera's (frameNs, frames), the last RATE_S
		self.paused = None # since when every camera has had no frame (clock), while it lasts
		self.pauses = 0
		self.dot = Dot(getattr(sys.stdout, "encoding", None))
		self.console = None
		self.saved = None
		self.done = threading.Event()
		self.thread = None

	def Start(self):
		self.started = self.clock()
		self.saved = (sys.stdout, sys.stderr)
		self.console = Console(sys.stdout, sys.stderr, self.mode.how)
		sys.stdout = Stream(self.console, "stdout", sys.stdout)
		if self.mode.stderr:
			sys.stderr = Stream(self.console, "stderr", sys.stderr)
		self.Tick()
		self.thread = threading.Thread(target=self.Run, name="campy status line", daemon=True)
		self.thread.start()
		return self

	def Run(self):
		while not self.done.wait(EVERY_S):
			try:
				self.Tick()
			except Exception:
				pass # the status line is only the console's: the recording goes on

	def Tick(self):
		self.console.Draw(self.Text())

	def Stop(self):
		# Wipes the status line and gives the console back, before the recording's end is said
		self.done.set()
		if self.thread is not None:
			self.thread.join(2)
		if self.console is not None:
			self.console.Clear()
			if sys.stdout is not None and isinstance(sys.stdout, Stream):
				sys.stdout = self.saved[0]
			if isinstance(sys.stderr, Stream):
				sys.stderr = self.saved[1]
			self.console = None

	# ------------------------------------------------------------------ what it says

	def Text(self, now=None):
		now = self.clock() if now is None else now
		ns = self.nowNs()
		rows = [self.reader.Row(c) for c in range(len(self.names))]
		rates = [self.Rate(c, row, ns) for c, row in enumerate(rows)]
		grabbing = [row for row in rows if row["state"] == camstatus.GRABBING and row["grabbed"] > 0]
		if self.firstFrame is None and any(row["grabbed"] > 0 for row in rows):
			self.firstFrame = now
		gap = self.Gap()
		quiet = len(grabbing) > 0 and all((ns - row["frameNs"]) / 1e9 >= gap for row in grabbing) and \
			(len(grabbing) == 1 or camstatus.Together(row["frameNs"] for row in grabbing))
		if quiet and self.paused is None:
			self.paused = now - max((ns - row["frameNs"]) / 1e9 for row in grabbing)
			self.pauses += 1
		elif not quiet:
			self.paused = None
		if self.firstFrame is None:
			head = "Waiting for the first frame {}".format(Clock(now - self.started))
		else:
			head = "Recording {}".format(Clock(now - self.firstFrame))
		tail = []
		dropped = sum(row["dropped"] for row in rows)
		if dropped:
			tail.append("{:,} dropped".format(dropped))
		shown = [r for r in rates if r is not None]
		if self.paused is not None:
			tail.append("trigger paused {}{}".format(Clock(now - self.paused),
				" (pause {})".format(self.pauses) if self.pauses > 1 else ""))
		else:
			if shown:
				if max(shown) - min(shown) < SAME_FPS:
					tail.append("{:.1f} fps".format(min(shown)))
				else:
					tail.append("fps " + "/".join("{:.1f}".format(r) if r is not None else "-" for r in rates))
			if self.pauses:
				tail.append("{} trigger pause{}".format(self.pauses, "" if self.pauses == 1 else "s"))
		return self.Fit(head, rows, tail)

	def Fit(self, head, rows, tail):
		# The names with their counts, or the counts alone where they don't fit, cut as a last resort
		dot = self.dot
		def counts(named):
			parts = []
			for name, row in zip(self.names, rows):
				count = "{:,}".format(row["grabbed"])
				said = STATES.get(row["state"])
				if said and not (row["state"] == camstatus.WAITING and self.firstFrame is None):
					count += " ({})".format(said)
				parts.append("{} {}".format(name, count) if named else count)
			return (dot.join(parts) + " frames") if named else "frames " + " / ".join(parts)
		width = Width()
		for named in (True, False):
			text = dot.join([head, counts(named)] + tail)
			if len(text) <= width:
				return text
		return text[:width - 1] + "…" if self.dot.unicode else text[:width - 3] + "..."

	def Gap(self):
		# No frame for this long: a pause (1 s, or 3 frames at a slow rate)
		return max(camstatus.TOGETHER_S, 3.0 / self.frameRate) if self.frameRate else camstatus.TOGETHER_S

	def Rate(self, c, row, ns):
		"""
		Camera c's frames a second over its last RATE_S of frames, by the
		frames' own times: a pause just begun does not lower it, and a
		camera with no frame for a while has none.
		"""
		history = self.history[c]
		if row["grabbed"] <= 0 or row["state"] != camstatus.GRABBING or (ns - row["frameNs"]) / 1e9 >= self.Gap():
			history.clear()
			return None
		if not history or history[-1] != (row["frameNs"], row["grabbed"]):
			history.append((row["frameNs"], row["grabbed"]))
		while len(history) > 2 and (history[-1][0] - history[1][0]) / 1e9 >= RATE_S:
			history.pop(0)
		(ns0, n0), (ns1, n1) = history[0], history[-1]
		if ns1 <= ns0 or n1 <= n0:
			return None
		return (n1 - n0) / ((ns1 - ns0) / 1e9)


class Dot:
	# The separator: a middle dot, or a bar where the console's encoding has none
	def __init__(self, encoding):
		self.unicode = True
		try:
			"·…".encode(encoding or "utf-8")
		except Exception:
			self.unicode = False
		self.sep = " · " if self.unicode else " | "

	def join(self, parts):
		return self.sep.join(parts)


def Clock(seconds):
	# 3:00, 1:02:03, 2d 01:02:03
	seconds = max(0, int(seconds))
	days, seconds = divmod(seconds, 86400)
	hours, seconds = divmod(seconds, 3600)
	minutes, seconds = divmod(seconds, 60)
	if days:
		return "{}d {:02d}:{:02d}:{:02d}".format(days, hours, minutes, seconds)
	if hours:
		return "{}:{:02d}:{:02d}".format(hours, minutes, seconds)
	return "{}:{:02d}".format(minutes, seconds)
