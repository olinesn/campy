"""
Stopping campy-acquire from another program, or over SSH (stopFile).

Ctrl-C stops a recording: the console sends it to campy-acquire and every
camera process at once, each camera stops and closes its video, and a second
Ctrl-C stops one that hangs as it closes. A program that started
campy-acquire without a console (campy-session's panel), or a run started
detached over SSH, has no Ctrl-C to press. With stopFile set, campy-acquire
looks for that file every 0.2 s while it runs:

- the file appears (anything in it, or nothing): the first Ctrl-C. Each
  camera stops and closes its video, as at Ctrl-C, and the recording ends
  as a stopped one does (exit code 0 when every camera recorded to the end);
- the file holds "now": the second Ctrl-C. A camera process that hangs as
  it closes is stopped at once, without closing. As with two presses of
  Ctrl-C within a second, it comes at least AGAIN_S after the first: a file
  that holds "now" from the start is a stop, then, a second later, a stop
  now.

A stop file already there when campy-acquire starts would stop it at once:
campy-acquire then stops before anything opens, and says to remove it. Once
the recording has ended, campy-acquire removes the stop file it acted on
(only a small one, as a request is: never a file of more than STOP_BYTES).

Only the standard library: campy-session's environment reads it too.
"""

import os, threading, time

NOW = "now" # the file's text for the second Ctrl-C
STOP_BYTES = 64 # a stop request is a few bytes; a bigger file is not one, and is never removed
EVERY_S = 0.2
AGAIN_S = 1.0 # the second request counts only this long after the first (utils.HandleKeyboardInterrupt)


class AlreadyThere(Exception):
	pass


def Path(params):
	"""The stop file stopFile names, made absolute, or None when it is "None"."""
	path = params.get("stopFile", "None")
	if path in (None, "", "None"):
		return None
	return os.path.abspath(str(path))


def Check(params):
	"""Raises AlreadyThere, saying what to do, when the stop file is there before the recording starts."""
	path = Path(params)
	if path and os.path.lexists(path):
		raise AlreadyThere("Stopped before recording: the stop file {} (stopFile) is already there, so the "
			"recording would stop as it starts. Remove it, or give another stopFile.".format(path))


def Level(path):
	"""
	What the file asks for: 0 nothing (it is not there), 1 stop (the first
	Ctrl-C), 2 stop now (the second: it holds "now").
	"""
	try:
		with open(path, "rb") as f:
			text = f.read(STOP_BYTES + 1)
	except FileNotFoundError:
		return 0
	except OSError:
		return 1 if os.path.lexists(path) else 0 # there, though unreadable (being written, on Windows)
	return 2 if text.strip().lower() == NOW.encode() else 1


def Write(path, now=False):
	"""
	Asks the campy-acquire watching path to stop (now: the second Ctrl-C),
	in one step: written beside it, then moved into place, so it never sees
	the file half written.
	"""
	tmp = "{}.{}.tmp".format(path, os.getpid())
	with open(tmp, "w") as f:
		f.write((NOW if now else "stop") + "\n")
	# On Windows a file cannot be replaced while it is open, and campy-acquire
	# opens it for a moment every EVERY_S: try again for a second
	for attempt in range(20):
		try:
			os.replace(tmp, path)
			return
		except PermissionError:
			if attempt == 19:
				os.remove(tmp)
				raise
			time.sleep(0.05)


class Watch:
	"""
	Looks for the stop file every EVERY_S seconds, from a thread of its own,
	and calls request(level) each time it asks for more than before: 1 (stop),
	then 2 (stop now) once the file says "now", at least again seconds after
	the 1.
	"""

	def __init__(self, path, request, every=EVERY_S, again=AGAIN_S):
		self.path, self.request, self.every, self.again = path, request, every, again
		self.level = 0
		self.first = None # when it asked for the stop (time.monotonic)
		self.done = threading.Event()
		self.thread = None

	def Start(self):
		if self.path:
			self.thread = threading.Thread(target=self.Run, name="campy stop file", daemon=True)
			self.thread.start()
		return self

	def Run(self):
		while not self.done.is_set():
			self.Look()
			self.done.wait(self.every)

	def Look(self):
		level = Level(self.path)
		if level > self.level:
			if self.level == 0:
				level, self.first = 1, time.monotonic()
			elif time.monotonic() - self.first < self.again:
				return self.level
			self.level = level
			try:
				self.request(level)
			except Exception:
				pass # the recording goes on as it would without a stop file
		return self.level

	def Stop(self):
		"""Stops looking, and removes the stop file once it has acted on it (a small one only)."""
		self.done.set()
		if self.thread is not None:
			self.thread.join(1.0)
		if self.level and self.path:
			try:
				if os.path.getsize(self.path) <= STOP_BYTES:
					os.remove(self.path)
			except OSError:
				pass
