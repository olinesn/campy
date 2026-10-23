"""
Each camera's counters, shared with campy's main process as it records: what
its recording watch (campy/watch.py) reads to tell when a camera stops
sending frames, drops frames, or falls behind writing them.

campy's main process makes one array of 64-bit integers for every camera
(Make) and gives it to each camera process, as it gives the emulated cameras'
start. The camera thread and the writer only store numbers in their
camera's row, a few per frame (well under a microsecond), never waiting on a
lock or on the main process; the main process only reads them. Each field is
one aligned 64-bit word, so a read never sees half of a store.

Outside campy-acquire (tests, campy-encode-sweep) there is no array, and
This() is a Camera whose stores do nothing.
"""

from campy import clock

# A camera's row: its fields, in this order
FIELDS = ("grabbed", "queued", "sent", "dropped", "frameNs", "sentNs", "state")
GRABBED, QUEUED, SENT, DROPPED, FRAME_NS, SENT_NS, STATE = range(len(FIELDS))
WIDTH = 16 # words per camera: room for more fields

# state
WAITING = 0 # not grabbing yet
GRABBING = 1
ENDED = 2 # its recording ended as it should (recTimeInSec, Ctrl-C, a stop file, the watch's stop)
NOT_STARTED = 3 # the camera did not start grabbing
LOST = 4 # the camera is gone (unicam.CameraLost)
VIDEO_FAILED = 5 # ffmpeg stopped, or did not start
CAMERA_FAILED = 6 # the camera thread stopped with an error


def Make(ctx, numCams):
	"""In campy's main process: the counters of numCams cameras (ctx: its multiprocessing context)."""
	return ctx.Array("q", numCams * WIDTH, lock=False)


def Now():
	# The counters' clock: hosttimes.npy's, the same in every process (clock.HostNs)
	return clock.HostNs()


class Camera:
	"""One camera's row of the array, or nothing (array None)."""

	def __init__(self, array=None, n=0):
		self.a = array
		self.o = n * WIDTH

	def Frame(self, grabbed, queued):
		# A frame grabbed (its number), and the frames waiting for ffmpeg as it came
		a = self.a
		if a is not None:
			o = self.o
			a[o + GRABBED] = grabbed
			a[o + QUEUED] = queued
			a[o + FRAME_NS] = Now()

	def Dropped(self, count):
		if self.a is not None:
			self.a[self.o + DROPPED] = count

	def Sent(self, count):
		# Frames given to ffmpeg so far
		if self.a is not None:
			self.a[self.o + SENT] = count
			self.a[self.o + SENT_NS] = Now()

	def State(self, state, only_from=None):
		# only_from: change it only from these states (an end does not hide a failure said before it)
		if self.a is not None and (only_from is None or self.a[self.o + STATE] in only_from):
			self.a[self.o + STATE] = state


class Reader:
	"""campy's main process's view of every camera's row."""

	def __init__(self, array, numCams):
		self.a, self.n = array, numCams

	def Get(self, c, field):
		return self.a[c * WIDTH + field]

	def Row(self, c):
		o = c * WIDTH
		return {name: self.a[o + i] for i, name in enumerate(FIELDS)}


_camera = Camera()


def Set(array, n):
	"""In a camera process (campy.RunCamera): its row of the main process's array."""
	global _camera
	_camera = Camera(array, n) if array is not None else Camera()


def This():
	return _camera
