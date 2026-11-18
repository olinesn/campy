"""
The outputs board (campy/outputs_board/campy_outputs/campy_outputs.ino) from Python:
what campy-rules (campy/rules.py) switches.

Board opens its serial port, checks that the sketch answers, and then keeps a
thread that reads every line the board says, stamped on campy's host clock
(campy/clock.py): the camera trigger's rises (frame k, on the board's clock),
the pulses made, the answers to commands. The same thread keeps the board's
watchdog quiet (hb, every HEARTBEAT_S) and asks the board's time every
SYNC_S, so that board times can be put on the host clock (ClockMap). Both
only while alive() says the program using the board is alive (campy-rules:
its decision loop went round lately): a program that is stuck goes quiet, and
the board's watchdog switches everything off.

Every line it hears goes to the board log (<out>.board.txt: "<host_ns> <line>"),
and every command it sends ("<host_ns> > <command>"), so a session can be
checked line by line afterwards.

With relink (seconds), a link that fails (a write that times out, a port that
errs: the Nano ESP32's USB serial driver stalls now and then under heavy
two-way traffic, and the board, with linkwatch, drops off USB and attaches
again) is opened again for up to that long: the listener hears ("relink",
["down", why]) then ("relink", ["back", seconds]); meanwhile commands are
answered "err link down" at once. A board that restarted (its clock went
back) is lost, as before: what it was told is gone. A link still down RESET_AFTER_S
after it failed gets a USB port reset from the computer's side, once (Linux only:
USBDEVFS_RESET on the board's own USB device, as if it had been plugged in again
while the chip runs on; the listener hears ("relink", ["reset", what happened])):
on a real Nano ESP32 the board's own re-attach left its USB stuck once in 4
stalls, and a port reset cleared it.

Needs pyserial (campy's own requirement) only to open a real port: tests pass
a connection of their own (and an opener for relink).
"""

import collections, os, sys, threading, time

from campy import clock

BAUD = 115200
HEARTBEAT_S = 0.05
SYNC_S = 1.0
ANSWER_TIMEOUT_S = 2.0
SKETCH = "campy_outputs"
VERSION = 5 # the sketch's VERSION: 2 counts on through trigger pauses (pull request 304); 3 inputs, servos and links;
# 4 the camera pulses themselves (one board for the cameras and the box); 5 linkwatch and quiet (its USB link)
# What each older version lacks, for the message that refuses it
OLDER = {
	1: "it counts the camera's frames from 1 again after a pause in the trigger, as between two bouts, while campy "
		"doesn't, so trains would land on the wrong frames",
	2: "it has no inputs, servos or links (an operant box's), and campy only works with the sketch it ships",
	3: "it can't make the camera pulses itself (one board for the cameras and the box), and campy only works with the "
		"sketch it ships",
	4: "it can't bring its USB link back when that stalls under heavy traffic (linkwatch), and campy only works with the "
		"sketch it ships",
	}
RISES = 4096 # trigger rises kept (68 s at 60 fps)
RESET_AFTER_S = 5.0 # relink: a link still down this long after it failed gets a USB port reset (the board's own
# re-attach brings it back within about 3 s when it works)
USBDEVFS_RESET = 21780 # Linux's _IO('U', 20)


class BoardError(Exception):
	pass


def UsbDevice(port, sys_root="/sys"):
	"""
	The board's USB device behind its serial port (Linux): its folder in sysfs, which names its place on the
	computer's USB (the same when it attaches again) and its vendor. None if there's none (not Linux, not USB).
	"""
	try:
		tty = os.path.basename(os.path.realpath(port))
		d = os.path.realpath(os.path.join(sys_root, "class", "tty", tty, "device"))
		for _ in range(4): # the interface (ttyACM), or the usb-serial port below it (ttyUSB): up to the device
			if os.path.isfile(os.path.join(d, "busnum")) and os.path.isfile(os.path.join(d, "idVendor")):
				with open(os.path.join(d, "idVendor")) as f:
					return dict(dir=d, vendor=f.read().strip())
			d = os.path.dirname(d)
	except Exception:
		pass
	return None


def ResetUsbPort(usb, dev_root="/dev/bus/usb", ioctl=None):
	"""
	A USB port reset of the board (Linux's USBDEVFS_RESET on its device node): the computer starts its USB over, as
	if it had been plugged in again, while the chip runs on (its sketch, its camera pulses). Needs write access to the
	node, which a udev rule gives without sudo (docs/operant-boxes.md). Returns the node; BoardError if it couldn't.
	"""
	def Read(name):
		with open(os.path.join(usb["dir"], name)) as f:
			return f.read().strip()
	try:
		vendor = Read("idVendor")
		node = os.path.join(dev_root, "{:03d}".format(int(Read("busnum"))), "{:03d}".format(int(Read("devnum"))))
	except (OSError, ValueError):
		raise BoardError("nothing on the board's USB port now")
	if vendor != usb["vendor"]:
		raise BoardError("another device on the board's USB port now (vendor {}, not {})".format(vendor, usb["vendor"]))
	try:
		fd = os.open(node, os.O_WRONLY)
	except PermissionError:
		raise BoardError("no permission to reset {}: a udev rule gives it (docs/operant-boxes.md)".format(node))
	except OSError as e:
		raise BoardError("could not open {}: {}".format(node, e))
	try:
		if ioctl is None:
			import fcntl
			ioctl = fcntl.ioctl
		ioctl(fd, USBDEVFS_RESET, 0)
	except OSError as e:
		raise BoardError("the reset of {} failed: {}".format(node, e))
	finally:
		os.close(fd)
	return node


def Mismatch(port, line):
	"""What to say when the board answered id with line, not campy_outputs VERSION."""
	words = line.split()
	upload = ("Upload campy/outputs_board/campy_outputs from this campy to it (docs/closed-loop.md, "
		"\"Uploading the sketch\").")
	if len(words) >= 3 and words[1] == SKETCH and words[2].isdigit():
		if int(words[2]) < VERSION:
			why = OLDER.get(int(words[2]), OLDER[1])
			return ("The outputs board on {} runs an older campy_outputs (version {}); this campy needs version {}: "
				"{}. {}".format(port, words[2], VERSION, why, upload))
		return ("The outputs board on {} runs a newer campy_outputs (version {}) than this campy knows (version {}). "
			"Update campy, or {}".format(port, words[2], VERSION, upload[0].lower() + upload[1:]))
	return "The board on {} answered {!r}: not campy_outputs version {}. {}".format(port, line, VERSION, upload)


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
	"gap", "ready") and each answer (kind "answer", fields the Answer).
	alive() (if given): whether to keep the watchdog quiet now.
	"""

	def __init__(self, port=None, connection=None, log=None, listener=None, say=print, heartbeat=HEARTBEAT_S,
			sync=SYNC_S, alive=None, relink=0.0, opener=None, resetter=None):
		self.port, self.say, self.listener = port, say, listener
		self.alive = alive
		self.log = log
		self.heartbeat, self.sync = heartbeat, sync
		self.relink = float(relink or 0.0)
		self.relinking = False
		self.board_us = 0 # the board's clock, the latest seen: a board that restarted shows an earlier one
		self.frames = None # the frames the board had counted at its last time answer (every rise, unlike its t lines)
		if opener is None:
			def opener():
				import serial
				return serial.Serial(port=port, baudrate=BAUD, timeout=0.005, write_timeout=0.5)
		self.opener = opener
		if connection is None:
			try:
				connection = opener()
			except Exception as e:
				raise BoardError("Could not open the outputs board's serial port {}: {}. Check that it is plugged "
					"in and that no other program (the Arduino IDE's Serial Monitor, another campy-rules) has it "
					"open.".format(port, e))
		self.connection = connection
		# The USB port reset, if the link stays down (relink): from the port's USB device, found now while it's there
		self.resetter, self.reset_after = resetter, RESET_AFTER_S
		if resetter is None and self.relink and port and sys.platform.startswith("linux"):
			usb = UsbDevice(port)
			if usb is not None:
				self.resetter = lambda: ResetUsbPort(usb)
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
				"campy/outputs_board/campy_outputs uploaded? Unplug its USB cable and plug it back in, then try "
				"again.".format(port, e))
		words = line.split()
		if len(words) < 3 or words[1] != SKETCH or words[2] != str(VERSION):
			self.Close(off=False)
			raise BoardError(Mismatch(port, line))
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
			if self.relinking:
				answer.sent_ns = answer.heard_ns = clock.HostNs()
				answer.line = "err link down"
				answer.done.set()
				return answer
			self.pending.append(answer)
			answer.sent_ns = clock.HostNs()
			try:
				self.connection.write((text + "\n").encode("ascii"))
			except Exception as e:
				self.pending.remove(answer)
				if not self.relink:
					raise
				# The link stalled: the reading thread opens it again (Relink); this command is answered at once
				self.relinking = True
				self.error = "could not write to the outputs board: {}".format(e)
				try:
					self.connection.close()
				except Exception:
					pass
				answer.heard_ns = clock.HostNs()
				answer.line = "err link down"
				answer.done.set()
				return answer
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
		self.board_us = max(self.board_us, int(words[2]))
		if len(words) > 3:
			self.frames = int(words[3])
		self.clock.Add(answer.sent_ns, answer.heard_ns, int(words[2]))

	# --------------------------------------------------------- reading

	def Run(self):
		buffer = b""
		last_sync = time.monotonic()
		while not self.stopping.is_set():
			try:
				# What has come, at least a byte: a line is taken (and stamped) as it comes. read(256) waited for 256
				# bytes or the port's 5 ms timeout (about 15 ms on Windows, the system timer's step), so each line
				# (15-40 bytes) was heard that much late: a trigger rise's report, a command's answer, and the clock
				# map's round trips (14 ms at best on a real Nano ESP32 on Windows, 0.4 ms now)
				data = self.connection.read(max(1, getattr(self.connection, "in_waiting", 0) or 0))
			except Exception as e:
				if self.stopping.is_set():
					return
				if not self.relinking:
					self.error = "the outputs board's port failed: {}".format(e)
				if self.relink and self.Relink():
					buffer = b""
					continue
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
				if self.alive is not None and not self.alive():
					continue # stuck: no heartbeat, no clock question either (a command too keeps the watchdog quiet)
				if self.heartbeat and now - self.last_sent >= self.heartbeat:
					self.Write("hb")
				if self.sync and now - last_sync >= self.sync:
					last_sync = now
					threading.Thread(target=self.SafeSync, daemon=True).start()
			except Exception as e:
				if self.stopping.is_set():
					return
				self.error = "could not write to the outputs board: {}".format(e)
				if self.relink and self.Relink():
					buffer = b""
					continue
				self.lost = True
				self.Fail()
				return

	def Relink(self):
		"""
		The link failed: open the port again, for up to relink seconds, until the board answers id as before and its
		clock went on (not a restart). True once it's back. From the reading thread.
		"""
		with self.lock:
			self.relinking = True
			waiting = list(self.pending)
			self.pending.clear()
		for answer in waiting:
			answer.line, answer.heard_ns = "err link down", clock.HostNs()
			answer.done.set()
		down_ns = clock.HostNs()
		if self.log is not None:
			self.log.write("{} relink: {}\n".format(down_ns, self.error))
		if self.listener is not None:
			self.listener("relink", ["down", self.error], down_ns, "")
		try:
			self.connection.close()
		except Exception:
			pass
		deadline = time.monotonic() + self.relink
		reset_at = time.monotonic() + self.reset_after if self.resetter is not None else None
		while not self.stopping.is_set() and time.monotonic() < deadline:
			if reset_at is not None and time.monotonic() >= reset_at:
				reset_at = None
				self.ResetPort()
			time.sleep(0.2)
			try:
				conn = self.opener()
			except Exception:
				continue
			try:
				got = self.Ask(conn, "id")
				if "ok {} {}".format(SKETCH, VERSION) not in got:
					raise BoardError("answered {!r}".format(got[-100:]))
				got = self.Ask(conn, "time")
				t = int(got.split("ok time ")[1].split()[0])
				if t < self.board_us:
					self.error = "the outputs board restarted (its clock went back): what it was told is gone"
					conn.close()
					return False
			except Exception:
				try:
					conn.close()
				except Exception:
					pass
				continue
			with self.lock:
				self.connection = conn
				self.relinking = False
				self.last_sent = time.monotonic()
			seconds = (clock.HostNs() - down_ns) / 1e9
			if self.log is not None:
				self.log.write("{} relink: back after {:.2f} s\n".format(clock.HostNs(), seconds))
			if self.listener is not None:
				self.listener("relink", ["back", seconds], clock.HostNs(), "")
			return True
		return False

	def ResetPort(self):
		# Still down: a USB port reset from the computer's side, once per relink
		try:
			what = "done ({})".format(self.resetter())
		except Exception as e:
			what = "failed: {}".format(e)
		if self.log is not None:
			self.log.write("{} relink: USB port reset {}\n".format(clock.HostNs(), what))
		if self.listener is not None:
			self.listener("relink", ["reset", what], clock.HostNs(), "")

	@staticmethod
	def Ask(conn, command, timeout=2.0):
		# A command on a connection of its own (relink): what came, up to its "ok <command>" line
		conn.write((command + "\n").encode("ascii"))
		got, end = "", time.monotonic() + timeout
		want = "ok {}".format(SKETCH if command == "id" else command)
		while time.monotonic() < end:
			data = conn.read(max(1, getattr(conn, "in_waiting", 0) or 0))
			if data:
				got += data.decode("ascii", "replace")
				if want in got and got.endswith("\n"):
					return got
		raise BoardError("no answer to {!r}".format(command))

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
			self.board_us = max(self.board_us, t)
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
		if off and not self.lost and self.relink:
			try:
				self.Command("linkwatch 0").Wait(1.0) # the board mustn't drop off USB once nobody talks to it
			except Exception:
				pass
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
