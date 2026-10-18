import signal
import sys
import time
import logging


def Say(text):
	# One write per message, newline included. Camera processes print at the same
	# time, and print() writes its text and the newline separately (and each
	# argument and separator), so their lines used to splice together.
	sys.stdout.write("{}\n".format(text))
	sys.stdout.flush()


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
