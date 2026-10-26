"""
The kernel log's AMD GPU messages, for the recording watch (campy/watch.py):
firmware stalls, hung GPU jobs and GPU resets, as they are logged.

On a Ryzen AI Max (Strix Halo) the GPU's engines can miss interrupts. The
driver then waits for its fallback timer ("amdgpu: Fence fallback timer
expired on ring comp_1.0.0"), and whatever ran on that engine stalls for
half a second: a camera's VA-API encoder (20-70 times an hour with 8 cameras,
before linear surfaces, writer.py's vaapiLinearSurfaces), or campy-live,
which shares the GPU. A job that hangs for longer times out ("ring ...
timeout"), and the driver resets the ring or the whole GPU, which ends what
was running on it. The watch otherwise only sees these as frames waiting for
ffmpeg.

Reader reads the log on a thread of its own, in the process that starts it:
`journalctl -k -f -n 0 -o cat` (new kernel messages only, their text only),
started as a child that dies with campy (campy/children.py), or else
/dev/kmsg. journalctl reads the kernel log for users in the adm or
systemd-journal group; /dev/kmsg only for root on most distributions
(kernel.dmesg_restrict 1, Ubuntu's default). When neither works it says so,
once, and reads nothing. The thread waits on the pipe without using the CPU:
the kernel logs a few lines an hour. Take() hands over what matched.

Only on Linux, with an amdgpu device (Amdgpu()). CAMPY_KERNEL_LOG_COMMAND
replaces the journalctl command (tests: a script that prints made-up lines).

The messages are the amdgpu driver's own (Linux 6.17), sorted into
STALL, HANG and RESET by Classify(). Not counted: "Freeing queue vital
buffer ..., queue evicted", logged when a GPU program (campy-live, a
benchmark) exits; the display's timeouts at boot.

Only the standard library.
"""

import collections, glob, os, re, select, shlex, subprocess, sys, threading, time

STALL, HANG, RESET = "stall", "hang", "reset"
COMMAND_ENV = "CAMPY_KERNEL_LOG_COMMAND"
JOURNALCTL = ["journalctl", "-k", "-f", "-n", "0", "-o", "cat", "--no-pager"]
KMSG = "/dev/kmsg"
STARTUP_S = 2.0 # journalctl that has neither ended nor said it can't read the journal by then reads it
FIX = "To let campy read it, add yourself to the adm group (sudo usermod -aG adm $USER), then log out and back in."

# (pattern, kind, counted). The ring (or the memory hub of a page fault) is group "ring", where there is one.
# counted False: part of a reset or hang already counted by another line (it still raises the alert)
PATTERNS = [(re.compile(p), kind, counted) for p, kind, counted in (
	(r"Fence fallback timer expired on ring (?P<ring>\S+)", STALL, True),
	(r"ring (?P<ring>\S+) timeout, but soft recovered", HANG, True),
	(r"ring (?P<ring>\S+) timeout, signaled seq", HANG, True),
	(r"MES(\(\d+\))? failed to respond to msg", HANG, True),
	(r"Queue preemption failed", HANG, True),
	(r"cp queue pipe \d+ queue \d+ preemption failed", HANG, True),
	(r"Failed to (evict|restore) queues of process", HANG, True),
	(r"IB test timed out", HANG, True),
	(r"IP block:\S+ is hung", HANG, True),
	(r"\[(?P<ring>\w+hub)\]( \w+)? page fault \(", HANG, True),
	(r"Starting (?P<ring>\S+) ring reset", RESET, False),
	(r"Ring (?P<ring>\S+) reset (succeeded|failed)", RESET, True),
	(r"GPU reset\(\d+\) (succeeded|failed)", RESET, True),
	(r"GPU reset succeeded, trying to resume", RESET, False),
	(r"GPU reset end with ret", RESET, False),
	(r"GPU Recovery Failed", RESET, True),
	(r"(ASIC|GPU mode1|GPU mode2|GPU link|Mode2|smu) reset failed", RESET, True),
	(r"VRAM is lost due to GPU reset", RESET, False),
	(r"MES might be in unrecoverable state", RESET, False),
)]

# What each ring is, by the start of its name, for the alerts' text
RINGS = (("comp", "compute: campy-live, and Mesa's copies into tiled video surfaces"),
	("gfx", "graphics: the desktop and Mesa's shaders"), ("sdma", "copy engine"),
	("vcn", "video encoder: the cameras' VA-API encoders"), ("jpeg", "JPEG decoder"), ("vpe", "video processing"),
	("mes", "firmware scheduler"), ("kiq", "firmware scheduler"), ("gfxhub", "graphics memory"),
	("mmhub", "video and copy engines' memory"))


def Classify(line):
	"""(kind, ring or None, counted) for a kernel log line, or None: not one of the GPU's errors."""
	if "Freeing queue vital buffer" in line:
		return None
	for pattern, kind, counted in PATTERNS:
		m = pattern.search(line)
		if m:
			ring = m.groupdict().get("ring")
			return kind, ring.rstrip(",.") if ring else None, counted
	return None


def RingText(ring):
	"""A ring's name and what it is: "comp_1.0.0 (compute: ...)"."""
	for start, what in RINGS:
		if ring.startswith(start):
			return "{} ({})".format(ring, what)
	return ring


def Amdgpu(sysfs_root="/sys"):
	"""Whether this computer has a GPU that the amdgpu driver runs (Linux)."""
	for card in glob.glob(os.path.join(sysfs_root, "class", "drm", "card*", "device", "driver")):
		try:
			if os.path.basename(os.readlink(card)) == "amdgpu":
				return True
		except OSError:
			pass
	return False


def Wanted(sysfs_root="/sys"):
	return sys.platform.startswith("linux") and Amdgpu(sysfs_root)


class Reader:
	"""
	Reads the kernel log from Start() to Stop(), on a thread of its own.
	Start() it from a thread that lives as long as the reading, as the
	journalctl child dies with the thread that started it (children.py).
	source: "journalctl", "/dev/kmsg", or None (not read: why says why).
	Take(): the GPU messages since the last call, as (kind, ring, counted,
	line); Notes(): what to say, once, as (text, for alerts.log too): it
	reads the log (the console only), or it can't, or it stopped.
	"""

	def __init__(self, command=None, kmsg=KMSG):
		env = os.environ.get(COMMAND_ENV)
		self.command = command or (shlex.split(env) if env else list(JOURNALCTL))
		self.kmsg = kmsg
		self.source, self.why = None, None
		self.events = collections.deque() # (kind, ring, counted, line), for Take
		self.notes = collections.deque()
		self.child = None
		self.done = threading.Event()
		self.thread = None

	def Start(self):
		self.child = self.Spawn()
		self.thread = threading.Thread(target=self.Run, name="campy kernel log", daemon=True)
		self.thread.start()
		return self

	def Spawn(self):
		from campy import children
		try:
			return children.Child(self.command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
				stderr=subprocess.STDOUT, env=dict(os.environ, SYSTEMD_COLORS="0", LC_ALL="C"))
		except OSError as e:
			self.why = "{} did not start: {}".format(self.command[0], e)
			return None

	def Stop(self, timeout=3.0):
		self.done.set()
		if self.child is not None:
			try:
				self.child.Close(timeout)
			except Exception:
				pass
		if self.thread is not None:
			self.thread.join(timeout)

	def Take(self):
		taken = []
		while self.events:
			taken.append(self.events.popleft())
		return taken

	def Notes(self):
		taken = []
		while self.notes:
			taken.append(self.notes.popleft())
		return taken

	# ------------------------------------------------------------------ the thread

	def Run(self):
		try:
			if self.child is not None:
				self.ReadJournal()
			if self.source is None and not self.done.is_set():
				self.ReadKmsg()
		except Exception as e: # never the recording's problem
			self.notes.append(("Watch: reading the kernel log stopped: {}. GPU errors are no longer watched.".format(e),
				True))

	def Line(self, line):
		found = Classify(line)
		if found is not None:
			self.events.append(found + (line.strip(),))

	def ReadJournal(self):
		name = os.path.basename(self.command[0])
		fd = self.child.stdout.fileno()
		# Its first seconds: journalctl says at once when it can't read the system's journal, or ends
		early, deadline = b"", time.monotonic() + STARTUP_S
		while True:
			left = deadline - time.monotonic()
			if left <= 0 or not select.select([fd], [], [], left)[0]:
				break
			chunk = os.read(fd, 65536)
			if not chunk:
				last = early.decode("utf-8", "replace").strip().splitlines()
				self.why = "{} ended at once (exit code {}{})".format(name, self.child.wait(),
					": " + last[-1] if last else "")
				return
			early += chunk
		text = early.decode("utf-8", "replace")
		if "not seeing messages from other users and the system" in text or "No journal files" in text:
			self.why = "{} can't read the kernel log: you are in neither the adm nor the systemd-journal group".format(
				name)
			self.child.Close(1)
			return
		self.Ready(name)
		rest, chunk = b"", early
		while True:
			lines = (rest + chunk).split(b"\n")
			rest = lines.pop()
			for raw in lines:
				line = raw.decode("utf-8", "replace")
				if not line.startswith("Hint:") and not line.startswith("-- "):
					self.Line(line)
			chunk = os.read(fd, 65536) # waits for the kernel's next message
			if not chunk:
				break
		if not self.done.is_set():
			self.notes.append(("Watch: {} stopped reading the kernel log (exit code {}). GPU errors are no longer "
				"watched in this recording.".format(name, self.child.wait()), True))

	def Ready(self, source):
		if self.source is None:
			self.source = source
			self.notes.append(("Watch: reading the kernel log ({}) for GPU errors.".format(source), False))

	def ReadKmsg(self):
		try:
			fd = os.open(self.kmsg, os.O_RDONLY | os.O_NONBLOCK)
		except OSError as e:
			why = "{}; {}: {}".format(self.why, self.kmsg, e.strerror or e) if self.why else "{}: {}".format(
				self.kmsg, e.strerror or e)
			self.why = why
			self.notes.append(("Watch: can't read the kernel log ({}), so GPU firmware stalls and resets are not "
				"watched in this recording. {}".format(why, FIX), True))
			return
		try:
			try:
				os.lseek(fd, 0, os.SEEK_END) # new messages only
			except OSError:
				pass # (tests: a pipe)
			self.Ready(self.kmsg)
			while not self.done.is_set():
				ready, _, _ = select.select([fd], [], [], 1.0)
				if not ready:
					continue
				try:
					record = os.read(fd, 8192)
				except BlockingIOError:
					continue
				except BrokenPipeError: # messages overwritten before they were read: go on from the next
					continue
				if not record:
					break
				# One record a read: "priority,sequence,microseconds,flags;text", then "key=value" lines starting with
				# a space
				for text in record.decode("utf-8", "replace").split("\n"):
					if text and not text.startswith(" "):
						self.Line(text.split(";", 1)[1] if ";" in text else text)
		finally:
			os.close(fd)
