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

import time

from campy import clock

# A camera's row: its fields, in this order
FIELDS = ("grabbed", "queued", "sent", "dropped", "frameNs", "sentNs", "state", "restarts", "counted", "lost",
	"missed", "missedNs", "missedFrame", "hostNs", "reconnects", "goneFrames")
GRABBED, QUEUED, SENT, DROPPED, FRAME_NS, SENT_NS, STATE, RESTARTS, COUNTED, LOST_FRAMES, MISSED, MISSED_NS, \
	MISSED_FRAME, HOST_NS, RECONNECTS, GONE_FRAMES = range(len(FIELDS))
WIDTH = 16 # words per camera: room for more fields

# Cameras whose last frames are this close stopped together: the trigger, not a camera
# (campy/watch.py, and each camera's own FrameWatch, cameras/unicam.py)
TOGETHER_S = 1.0


def Together(times_ns):
	# Whether these frames' times (ns, Now's clock) are TOGETHER_S or closer: two cameras or more
	times_ns = list(times_ns)
	return len(times_ns) >= 2 and (max(times_ns) - min(times_ns)) / 1e9 <= TOGETHER_S


# state
WAITING = 0 # not grabbing yet
GRABBING = 1
ENDED = 2 # its recording ended as it should (recTimeInSec, Ctrl-C, a stop file, the watch's stop)
NOT_STARTED = 3 # the camera did not start grabbing
LOST = 4 # the camera is gone (unicam.CameraLost); with cameraReconnect, campy waits for it, and it can be GRABBING again
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

	def Frame(self, grabbed, queued, hostNs=0):
		# A frame grabbed (its number), the frames waiting for ffmpeg as it came, and its host time (hosttimes.npy's)
		a = self.a
		if a is not None:
			o = self.o
			a[o + HOST_NS] = hostNs
			a[o + GRABBED] = grabbed
			a[o + QUEUED] = queued
			a[o + FRAME_NS] = Now()

	def Reconnected(self, count, frames):
		# Times the camera came back after it was unplugged (cameraReconnect), and the frame numbers it skipped while
		# it was gone, in all (its frames grabbed are grabbed - goneFrames)
		if self.a is not None:
			self.a[self.o + GONE_FRAMES] = frames
			self.a[self.o + RECONNECTS] = count # last: the watch reads goneFrames once reconnects has changed

	def Others(self):
		"""
		The other cameras grabbing: [(their newest frame's number, its host
		time ns)], for a camera back after it was unplugged to number its
		frames as theirs (unicam.Resume). Empty outside campy-acquire, or with
		no other camera grabbing.
		"""
		a = self.a
		if a is None:
			return []
		def Read():
			return {o: (int(a[o + GRABBED]), int(a[o + HOST_NS])) for o in range(0, len(a) - WIDTH + 1, WIDTH)
				if o != self.o and a[o + STATE] == GRABBING}
		# A camera's number and host time are two stores: read twice, 1 ms apart, and kept where both reads agree
		# (one taken as its next frame came in between could pair a number with the next frame's time)
		first = Read()
		time.sleep(0.001)
		second = Read()
		return [first[o] for o in first if first[o] == second.get(o) and first[o][0] > 0 and first[o][1] > 0]

	def OthersGoOn(self):
		"""
		Whether another camera is still recording (waiting, grabbing, or gone
		and waited for): a camera that was unplugged is waited for while one
		is. True outside campy-acquire, or with one camera: there, its own
		recording's length says.
		"""
		a = self.a
		if a is None:
			return True
		others = [a[o + STATE] for o in range(0, len(a) - WIDTH + 1, WIDTH) if o != self.o]
		return not others or any(state in (WAITING, GRABBING, LOST) for state in others)

	def Dropped(self, count):
		if self.a is not None:
			self.a[self.o + DROPPED] = count

	def Sent(self, count):
		# Frames given to ffmpeg so far
		if self.a is not None:
			self.a[self.o + SENT] = count
			self.a[self.o + SENT_NS] = Now()

	def Restarted(self, count):
		# Times its ffmpeg was restarted (restartFfmpeg)
		if self.a is not None:
			self.a[self.o + RESTARTS] = count

	def Counted(self, counted, lost):
		# Files of a stopped ffmpeg counted so far, and the frames given to those ffmpegs not in their files
		if self.a is not None:
			self.a[self.o + LOST_FRAMES] = lost
			self.a[self.o + COUNTED] = counted # last: the watch reads lost once counted has changed

	def Missed(self, missed, frame):
		# Trigger pulses without a frame so far (frametimes.TriggerGaps), the latest just before this frame
		if self.a is not None:
			o = self.o
			self.a[o + MISSED_NS] = Now()
			self.a[o + MISSED_FRAME] = frame
			self.a[o + MISSED] = missed # last: the watch reads the others once missed has changed

	def QuietTogether(self):
		"""
		Whether this camera and every other camera grabbing stopped together:
		their last frames within TOGETHER_S, none since (Together). Read
		from the other cameras' rows, as campy's main process reads them
		(watch.py CheckQuiet). False outside campy-acquire, or with one camera.
		"""
		a = self.a
		if a is None or a[self.o + STATE] != GRABBING or a[self.o + GRABBED] <= 0:
			return False
		lasts = [a[o + FRAME_NS] for o in range(0, len(a) - WIDTH + 1, WIDTH)
			if a[o + STATE] == GRABBING and a[o + GRABBED] > 0]
		return Together(lasts)

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
