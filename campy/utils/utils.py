import signal
import sys
import time
import queue
import logging
import threading


def Say(text):
	# One write per message, newline included. Camera processes print at the same
	# time, and print() writes its text and the newline separately (and each
	# argument and separator), so their lines used to splice together. In a
	# camera process the printer thread writes it (StartPrinter).
	line = "{}\n".format(text)
	if _printer is not None:
		_printer.Put("stdout", line)
	else:
		Write("stdout", line)


def Write(stream, line):
	out = sys.stderr if stream == "stderr" else sys.stdout
	out.write(line)
	out.flush()


class Printer:
	"""
	Writes what a camera process says, from a thread of its own, so the
	camera thread never waits on the console. stdout is often a log file on
	the disk ffmpeg writes the videos to, and a write there could block for
	tens of ms (a camera's frames then got late host times), or seconds when
	the disk pauses. Messages go out in the order they were said, each in
	one write, stdout and stderr (logging) alike. Put never blocks, even
	from a signal handler (SimpleQueue).
	"""

	def __init__(self):
		self.queue = queue.SimpleQueue()
		self.thread = threading.Thread(target=self.Run, name="campy printer", daemon=True)
		self.thread.start()

	def Put(self, stream, line):
		self.queue.put((stream, line))

	def Run(self):
		while True:
			item = self.queue.get()
			if item is None:
				return
			if isinstance(item, threading.Event):
				item.set() # Flush: everything said before it is written
				continue
			try:
				Write(*item)
			except Exception:
				pass # a console that is gone: the rest are still tried

	def Flush(self, timeout=None):
		# Waits until everything said so far is written. True when it was.
		done = threading.Event()
		self.queue.put(done)
		return done.wait(timeout)

	def Stop(self):
		# Writes everything said so far, then ends the thread
		self.queue.put(None)
		self.thread.join()


class PrinterHandler(logging.Handler):
	# logging's messages, through the printer to stderr, in order with Say's.
	# As logging writes them with no handler set up: WARNING and up, the
	# message (and traceback) alone.
	def __init__(self, printer):
		logging.Handler.__init__(self, logging.WARNING)
		self.printer = printer

	def emit(self, record):
		try:
			self.printer.Put("stderr", self.format(record) + "\n")
		except Exception:
			self.handleError(record)


_printer = None
_handler = None


def StartPrinter():
	"""
	From here on, Say and logging (when it has no handler of its own) are
	written by a printer thread (Printer). Each camera process starts one,
	and stops it as it ends (StopPrinter), which writes what is left.
	"""
	global _printer, _handler
	if _printer is not None:
		return _printer
	_printer = Printer()
	root = logging.getLogger()
	if not root.handlers:
		_handler = PrinterHandler(_printer)
		root.addHandler(_handler)
	return _printer


def StopPrinter():
	# Writes everything said so far, and Say writes directly again
	global _printer, _handler
	printer, _printer = _printer, None
	if _handler is not None:
		logging.getLogger().removeHandler(_handler)
		_handler = None
	if printer is not None:
		printer.Stop()


def ErrorText(error):
	# An exception's message, or its kind when it has none
	return str(error).strip() or type(error).__name__


def ExitCodeText(code):
	"""
	A process's exit code, as said in campy's messages: with the signal that
	ended it, where one did (Linux and macOS give it as a negative code), or
	in hex when it is one of Windows' large codes (0xC0000005: an access
	violation, a segfault).
	"""
	if code is None:
		return "no exit code"
	if code < 0:
		try:
			return "exit code {} ({})".format(code, signal.Signals(-code).name)
		except ValueError:
			return "exit code {}".format(code)
	if code > 255:
		return "exit code {} (0x{:08X})".format(code, code)
	return "exit code {}".format(code)


class QueueKeyboardInterrupt(object):
	'''
	Usage:
	from campy.utils.utils import HandleKeyboardInterrupt

	with QueueKeyboardInterrupt(queue):
		# stuff here will not be interrupted by SIGINT
		<critical_code()>
	'''
	def __init__(self, object):
		# Insert queue object into keyboard interrupt handler for signaling
		self.queue = object["queue"]
		self.message = object["message"]

	def __enter__(self):
		self.signal_received = False
		self.old_handler = signal.signal(signal.SIGINT, self.handler)
				
	def handler(self, sig, frame):
		self.signal_received = (sig, frame)
		Say('SIGINT received. KeyboardInterrupt has been queued.')
		self.queue.append(self.message)
	
	def __exit__(self, type, value, traceback):
		pass

class HandleKeyboardInterrupt:
	'''
	campy's main process: Ctrl-C does not end it. Ctrl-C reaches every camera
	process too, and each stops its camera and closes its video
	(QueueKeyboardInterrupt), while the main process waits for them. The
	presses are counted (count), so that a second one can stop the camera
	processes that hang as they close (unicam.CameraProcesses.Wait). A press
	within a second of the one before is the same press (a double press).

	Usage:
	from campy.utils.utils import HandleKeyboardInterrupt

	with HandleKeyboardInterrupt() as interrupts:
		# stuff here will not be interrupted by SIGINT
		<critical_code()>, reading interrupts.count
	'''
	def __init__(self, again_after=1.0):
		self.count = 0 # the presses so far
		self.last = None # when the last one came (time.monotonic)
		self.again_after = again_after

	def __enter__(self):
		self.signal_received = False
		self.old_handler = signal.signal(signal.SIGINT, self.handler)
		return self

	def handler(self, sig, frame):
		self.signal_received = (sig, frame)
		now = time.monotonic()
		if self.last is not None and now - self.last < self.again_after:
			return
		self.last = now
		self.count += 1
		if self.count == 1:
			Say("SIGINT received. Each camera stops and closes its video: please wait. If one hangs as it "
				"closes, Ctrl-C again stops it at once, without closing it.")
		else:
			Say("SIGINT received again.")

	def __exit__(self, type, value, traceback):
		pass
