"""
The outputs board (campy/trigger/campy_outputs/campy_outputs.ino) from Python:
what campy-rules (campy/rules.py) switches.

Board opens its serial port, checks that the sketch answers, and then keeps a
thread that reads every line the board says, stamped on campy's host clock
(campy/clock.py): the camera trigger's rises (frame k, on the board's clock),
the pulses made, the answers to commands. The same thread keeps the board's
watchdog quiet (hb, every HEARTBEAT_S) and asks the board's time every
SYNC_S, so that board times can be put on the host clock (ClockMap).

Every line it hears goes to the board log (<out>.board.txt: "<host_ns> <line>"),
and every command it sends ("<host_ns> > <command>"), so a session can be
checked line by line afterwards.

Needs pyserial (campy's own requirement) only to open a real port: tests pass
a connection of their own.
"""

import collections, threading, time

from campy import clock

BAUD = 115200
HEARTBEAT_S = 0.05
SYNC_S = 1.0
ANSWER_TIMEOUT_S = 2.0
SKETCH = "campy_outputs"
VERSION = 1
RISES = 4096 # trigger rises kept (68 s at 60 fps)


class BoardError(Exception):
	pass


def Expected(command):
	# The word an "ok" answer to command starts with, after "ok"
	word = command.split()[0] if command.split() else ""
	return SKETCH if word == "id" else word


class ClockMap:
	"""
	Board time (us since its power-up) to host time (ns), from round trips:
	the board said time b between the host's sending (s) and hearing (h) the
	question, so b is about at (s + h) / 2, give or take half the round trip.
	A line through the quickest of the recent round trips (offset and drift:
	the board's crystal and the host's clock differ by some ppm).
	"""

	def __init__(self, keep=120):
		self.samples = collections.deque(maxlen=keep)
		self.fit = None # (board us, host ns, ns per us) through their middle

	def Add(self, sent_ns, heard_ns, board_us):
		self.samples.append((heard_ns - sent_ns, (sent_ns + heard_ns) / 2.0, float(board_us)))
		trips = sorted(s[0] for s in self.samples)
		best = trips[0]
		use = [s for s in self.samples if s[0] <= max(1.5 * best, best + 200000)]
		b = sum(s[2] for s in use) / len(use)
		h = sum(s[1] for s in use) / len(use)
		slope = 1000.0
		if len(use) >= 3:
			db = [s[2] - b for s in use]
			var = sum(d * d for d in db)
			if var > (5e6) ** 2: # at least seconds apart: the drift is measurable
				slope = sum(d * (s[1] - h) for d, s in zip(db, use)) / var
		self.fit = (b, h, slope)

	def Ready(self):
		return self.fit is not None

	def RoundTrip(self):
		# The quickest round trip, ns: twice the worst error of HostNs
		return min(s[0] for s in self.samples) if self.samples else None

	def HostNs(self, board_us):
		if self.fit is None or board_us is None or board_us < 0:
			return None
		b, h, slope = self.fit
		return int(round(h + (board_us - b) * slope))


class Answer:
	# A command's answer, once the board has given it
	def __init__(self, command, record=None):
		self.command, self.record = command, record # record: the caller's, for the listener
		self.sent_ns = self.written_ns = self.heard_ns = self.line = None
		self.done = threading.Event()

	def Wait(self, timeout=ANSWER_TIMEOUT_S):
		if not self.done.wait(timeout):
			raise BoardError("the outputs board did not answer {!r} within {:g} s".format(self.command, timeout))
		return self.line

	def Ok(self, timeout=ANSWER_TIMEOUT_S):
		line = self.Wait(timeout)
		if not line.startswith("ok"):
			raise BoardError("the outputs board answered {!r} with {!r}".format(self.command, line))
		return line


class Board:
	"""
	listener(kind, fields, heard_ns, line) is called from the reading thread
	with each line the board says on its own ("t", "p", "e", "clash", "wd",
	"ready") and each answer (kind "answer", fields the Answer).
	"""

	def __init__(self, port=None, connection=None, log=None, listener=None, say=print, heartbeat=HEARTBEAT_S,
			sync=SYNC_S):
		self.port, self.say, self.listener = port, say, listener
		self.log = log
		self.heartbeat, self.sync = heartbeat, sync
		if connection is None:
			import serial
			try:
				connection = serial.Serial(port=port, baudrate=BAUD, timeout=0.005, write_timeout=0.5)
			except Exception as e:
				raise BoardError("Could not open the outputs board's serial port {}: {}. Check that it is plugged "
					"in and that no other program (the Arduino IDE's Serial Monitor, another campy-rules) has it "
					"open.".format(port, e))
		self.connection = connection
		self.lock = threading.Lock() # one writer at a time
		self.pending = collections.deque() # Answers, in the order the commands went
		self.clock = ClockMap()
		self.rises = collections.OrderedDict() # frame k -> board us, the newest RISES
		self.last_sent = 0.0
		self.lost = False
		self.error = None
		self.stopping = threading.Event()
		self.thread = threading.Thread(target=self.Run, name="campy-outputs", daemon=True)
		self.thread.start()
		try:
			line = self.Command("id").Ok()
		except BoardError as e:
			self.Close(off=False)
			raise BoardError("The outputs board on {} did not answer as campy_outputs ({}). Is the sketch "
				"campy/trigger/campy_outputs uploaded? Unplug its USB cable and plug it back in, then try "
				"again.".format(port, e))
		words = line.split()
		if len(words) < 3 or words[1] != SKETCH or words[2] != str(VERSION):
			self.Close(off=False)
			raise BoardError("The board on {} answered {!r}: not campy_outputs version {}. Upload "
				"campy/trigger/campy_outputs to it.".format(port, line, VERSION))
		self.Sync()

	# --------------------------------------------------------- sending

	def Write(self, text):
		data = (text + "\n").encode("ascii")
		with self.lock:
			sent_ns = clock.HostNs()
			self.connection.write(data)
			written_ns = clock.HostNs()
			self.last_sent = time.monotonic()
		if self.log is not None:
			self.log.write("{} > {}\n".format(sent_ns, text))
		return sent_ns, written_ns

	def Command(self, text, record=None):
		"""
		Sends a command; returns its Answer at once (Wait or Ok for the line).
		The answer's sent_ns is when the write began, written_ns when it returned.
		"""
		answer = Answer(text, record)
		with self.lock:
			self.pending.append(answer)
			answer.sent_ns = clock.HostNs()
			try:
				self.connection.write((text + "\n").encode("ascii"))
			except Exception:
				self.pending.remove(answer)
				raise
			answer.written_ns = clock.HostNs()
			self.last_sent = time.monotonic()
		if self.log is not None:
			self.log.write("{} > {}\n".format(answer.sent_ns, text))
		return answer

	def Sync(self, rounds=5):
		# A few round trips for the clock map
		for _ in range(rounds):
			self.SyncOnce()

	def SyncOnce(self):
		answer = self.Command("time")
		line = answer.Ok()
		words = line.split()
		self.clock.Add(answer.sent_ns, answer.heard_ns, int(words[2]))

	# --------------------------------------------------------- reading

	def Run(self):
		buffer = b""
		last_sync = time.monotonic()
		while not self.stopping.is_set():
			try:
				data = self.connection.read(256)
			except Exception as e:
				if not self.stopping.is_set():
					self.error = "the outputs board's port failed: {}".format(e)
					self.lost = True
					self.Fail()
				return
			heard_ns = clock.HostNs()
			if data:
				buffer += data
				while b"\n" in buffer:
					raw, buffer = buffer.split(b"\n", 1)
					line = raw.decode("ascii", "replace").strip()
					if line:
						self.Heard(line, heard_ns)
			now = time.monotonic()
			try:
				if self.heartbeat and now - self.last_sent >= self.heartbeat:
					self.Write("hb")
				if self.sync and now - last_sync >= self.sync:
					last_sync = now
					threading.Thread(target=self.SafeSync, daemon=True).start()
			except Exception as e:
				if not self.stopping.is_set():
					self.error = "could not write to the outputs board: {}".format(e)
					self.lost = True
					self.Fail()
				return

	def SafeSync(self):
		try:
			self.SyncOnce()
		except Exception:
			pass

	def Fail(self):
		# Whoever waits for an answer hears that none will come
		while self.pending:
			answer = self.pending.popleft()
			answer.line = "err no board ({})".format(self.error)
			answer.done.set()
		if self.listener is not None:
			self.listener("lost", [self.error], clock.HostNs(), "")

	def Heard(self, line, heard_ns):
		if self.log is not None:
			self.log.write("{} {}\n".format(heard_ns, line))
		words = line.split()
		kind = words[0]
		if kind in ("ok", "err"):
			# The oldest command's answer. An "ok" names its command: commands
			# before it whose answers never came (a line the board couldn't send)
			# get none, so no answer is ever given to the wrong command.
			skipped = []
			with self.lock:
				answer = self.pending.popleft() if self.pending else None
				if kind == "ok" and len(words) > 1:
					while answer is not None and Expected(answer.command) != words[1] and self.pending:
						skipped.append(answer)
						answer = self.pending.popleft()
			for lost in skipped:
				lost.line, lost.heard_ns = "err no answer (the board's line was lost)", heard_ns
				lost.done.set()
				if self.listener is not None:
					self.listener("answer", lost, heard_ns, lost.line)
			if answer is None:
				return
			answer.line, answer.heard_ns = line, heard_ns
			answer.done.set()
			if self.listener is not None:
				self.listener("answer", answer, heard_ns, line)
			return
		if kind == "t" and len(words) == 3:
			k, t = int(words[1]), int(words[2])
			if k == 1:
				self.rises.clear()
			self.rises[k] = t
			while len(self.rises) > RISES:
				self.rises.popitem(last=False)
		if self.listener is not None:
			self.listener(kind, words[1:], heard_ns, line)

	def RiseHostNs(self, k):
		# Frame k's trigger rise on the host clock, or None
		return self.clock.HostNs(self.rises.get(k))

	# --------------------------------------------------------- the rest

	def Close(self, off=True):
		if off and not self.lost:
			try:
				self.Command("alloff").Wait(1.0)
			except Exception:
				pass
		self.stopping.set()
		self.thread.join(1.0)
		try:
			self.connection.close()
		except Exception:
			pass
