"""
A live feed of each camera's frames, for programs that read them as campy
records (liveFeed, off by default): campy-live runs SLEAP on them.

With liveFeed True, each camera process also copies every frame it grabs into
a ring of shared memory of its own, named <liveFeedName>-<camera index>
("campy-live-0", "campy-live-1", ...), with the frame's number, its camera
timestamp and when it reached the computer. Writing is one copy of the frame
into the next slot: campy never waits for a reader, and a reader that falls
behind finds the frames it missed overwritten. Nothing in campy depends on the
feed: when it fails, the camera says so once and records on without it.

Frames of the same trigger pulse have the same frame number on every camera,
when every camera starts on the trigger's first pulse (campy starts the
trigger once all are grabbing) and none loses a frame before campy gets it.
A camera that loses one (flirFramesLost in metadata.csv) numbers its later
frames one lower than the others; readers can tell from the host times
(campy-live counts the sets whose host times are more than half a frame apart).

This module needs only numpy and Python's standard library, so a program in
another environment (one with torch and sleap-nn, but no camera drivers) can
read the feed: python -m campy.live, or with campy's folder on PYTHONPATH.

Layout
------
All numbers little-endian. One ring holds a header and `slots` slots:

  offset  type        header field
  0       8 bytes     magic b"CAMPYLV1"
  8       uint32      layout version (1)
  12      uint32      header bytes (4096): the first slot starts here
  16      uint32      slots
  20      uint32[3]   frame height, width, channels
  32      8 bytes     frame dtype, numpy's dtype.str, NUL-padded ("|u1")
  40      uint64      slot bytes: from one slot to the next
  48      uint64      frame bytes
  56      int64       ring id: a random number, new for every ring made
  64      int64       state: 1 recording, 2 ended (the camera stopped)
  72      int64       latest: the newest frame number written, 0 before the first
  80      float64     frame rate (frameRate)
  88      int64       camera index (n_cam)
  96      64 bytes    camera name, UTF-8, NUL-padded
  160     int64       frames written
  168     int64       frames not written (a frame of another size or type)

Slot i starts at header bytes + i * slot bytes, and holds frame number n in
slot n % slots:

  offset  type        slot field
  0       int64       begin: the frame number, written before the frame
  8       int64       end: the frame number, written after the frame and the fields below
  16      int64       frame number
  24      int64       host time: time.monotonic_ns() as campy got the frame
                      (as in hosttimes.npy; on Linux and macOS, the same
                      clock in every process)
  32      float64     camera timestamp, seconds (the camera's clock)
  40      int64       written: time.perf_counter_ns() once the frame was copied
  64      frame       height x width x channels of dtype, C order

A frame is whole when end == begin == n, read in this order: end, then the
frame, then begin. The writer sets begin, copies the frame, then sets end, so
a frame read while it was being replaced has begin > n (RingReader.Read does
this). That relies on the CPU keeping stores in order, as x86 does; it holds
on the ARM CPUs campy runs on in practice, where numpy's copy and the stores
around it are separate calls, but is not guaranteed there.

Reading one camera's newest frame from plain numpy (Linux, Python 3.13+;
before 3.13 see Attach for the resource tracker):

    from multiprocessing import shared_memory
    import numpy as np
    shm = shared_memory.SharedMemory("campy-live-0", track=False)
    h = np.frombuffer(shm.buf, np.uint8, 4096)
    slots, height, width, channels = h[16:32].view("<u4")
    slot_bytes = int(h[40:48].view("<u8")[0])
    n = int(h[72:80].view("<i8")[0])
    start = 4096 + (n % slots) * slot_bytes
    frame = np.frombuffer(shm.buf, np.uint8, height * width * channels, start + 64)\\
        .reshape(height, width, channels).copy()
"""

import os, sys, time
import numpy as np
from multiprocessing import shared_memory

MAGIC = b"CAMPYLV1"
VERSION = 1
HEADER_BYTES = 4096
SLOT_HEADER_BYTES = 64
DEFAULT_NAME = "campy-live"
DEFAULT_SLOTS = 8

RECORDING, ENDED = 1, 2

# Header fields: (offset, numpy type)
_HEADER = {
	"version": (8, "<u4"), "headerBytes": (12, "<u4"), "slots": (16, "<u4"),
	"height": (20, "<u4"), "width": (24, "<u4"), "channels": (28, "<u4"),
	"slotBytes": (40, "<u8"), "frameBytes": (48, "<u8"), "ringId": (56, "<i8"),
	"state": (64, "<i8"), "latest": (72, "<i8"), "frameRate": (80, "<f8"),
	"camera": (88, "<i8"), "written": (160, "<i8"), "notWritten": (168, "<i8"),
	}
_DTYPE_AT, _NAME_AT, _NAME_BYTES = 32, 96, 64

# Slot fields, as int64 words (the camera timestamp is a float64 in word 4)
BEGIN, END, FRAME, HOST, STAMP, WRITTEN = range(6)


def RingName(prefix, n_cam):
	# Short: macOS allows 30 characters in a shared memory name
	return "{}-{}".format(prefix, n_cam)


def SlotBytes(frameBytes):
	# Each slot's frame starts 64 bytes in, and each slot on a 4 KiB page
	return -(-(SLOT_HEADER_BYTES + frameBytes) // 4096) * 4096


def RingBytes(shape, dtype, slots):
	return HEADER_BYTES + slots * SlotBytes(int(np.prod(shape)) * np.dtype(dtype).itemsize)


def FrameShape(img):
	# Every frame as height x width x channels: a mono frame has 1 channel
	img = np.asarray(img)
	if img.ndim == 2:
		return img.shape + (1,)
	if img.ndim == 3:
		return img.shape
	raise ValueError("a frame is height x width or height x width x channels, not {}".format(img.shape))


def Attach(name):
	"""
	Opens an existing ring's shared memory, without taking it over. Python
	before 3.13 registers every SharedMemory it opens with its resource
	tracker on POSIX, which removes it when that process ends: a reader that
	ended would take the ring's name away from campy and from the readers
	after it. There it is opened directly instead (SharedPosix).
	"""
	if sys.version_info >= (3, 13):
		return shared_memory.SharedMemory(name, track=False)
	if os.name == "posix":
		return SharedPosix(name)
	return shared_memory.SharedMemory(name)


class SharedPosix:
	"""POSIX shared memory opened as SharedMemory(name) would, but untracked."""

	def __init__(self, name):
		import _posixshmem, mmap
		self.name = name
		self._name = "/" + name # as SharedMemory names it
		fd = _posixshmem.shm_open(self._name, os.O_RDWR, mode=0o600)
		try:
			self.size = os.fstat(fd).st_size
			self._mmap = mmap.mmap(fd, self.size)
		finally:
			os.close(fd)
		self.buf = memoryview(self._mmap)

	def close(self):
		if self.buf is not None:
			self.buf.release()
			self.buf = None
			self._mmap.close()


def _Unlink(shm):
	# Removes the name of shared memory this process opened with Attach,
	# without telling the resource tracker, which was not told of it
	# (SharedMemory.unlink would, and it would print an error)
	import _posixshmem
	_posixshmem.shm_unlink(shm._name)


class Ring:
	"""
	The numpy views of one ring's shared memory. RingWriter makes and writes
	one; RingReader reads one.
	"""

	def __init__(self, shm):
		self.shm = shm
		buf = shm.buf
		self.header = np.frombuffer(buf, np.uint8, HEADER_BYTES)
		if bytes(self.header[:8]) != MAGIC:
			raise ValueError("{} is not a campy live feed ring".format(shm.name))
		if self.Get("version") != VERSION:
			raise ValueError("{} has layout version {}; this campy reads version {}".format(
				shm.name, self.Get("version"), VERSION))
		self.slots = int(self.Get("slots"))
		self.shape = (int(self.Get("height")), int(self.Get("width")), int(self.Get("channels")))
		self.dtype = np.dtype(bytes(self.header[_DTYPE_AT:_DTYPE_AT + 8]).rstrip(b"\0").decode("ascii"))
		self.slotBytes = int(self.Get("slotBytes"))
		self.cameraName = bytes(self.header[_NAME_AT:_NAME_AT + _NAME_BYTES]).rstrip(b"\0").decode("utf-8", "replace")
		self.ringId = int(self.Get("ringId"))
		# Each slot's fields, and its frame
		self.slotFields = np.ndarray((self.slots, 8), "<i8", buf, HEADER_BYTES, (self.slotBytes, 8))
		self.stamps = np.ndarray((self.slots,), "<f8", buf, HEADER_BYTES + 8 * STAMP, (self.slotBytes,))
		height, width, channels = self.shape
		size = self.dtype.itemsize
		self.frames = np.ndarray((self.slots,) + self.shape, self.dtype, buf, HEADER_BYTES + SLOT_HEADER_BYTES,
			(self.slotBytes, width * channels * size, channels * size, size))
		self.fields = {key: np.ndarray((1,), kind, buf, at) for key, (at, kind) in _HEADER.items()}

	def Get(self, key):
		at, kind = _HEADER[key]
		return np.frombuffer(self.header, kind, 1, at)[0].item()

	def Set(self, key, value):
		self.fields[key][0] = value

	def Release(self):
		# The views go first: shared memory with views open cannot close
		self.header = self.slotFields = self.stamps = self.frames = None
		self.fields = {}
		try:
			self.shm.close()
		except Exception:
			pass


class RingWriter:
	"""
	One camera's ring, made by its camera process. Write copies a frame in,
	and never waits; Close marks the ring ended and removes its name.
	"""

	def __init__(self, name, shape, dtype, slots=DEFAULT_SLOTS, frameRate=0.0, camera=0, cameraName=""):
		if len(shape) == 2:
			shape = tuple(shape) + (1,)
		dtype = np.dtype(dtype)
		slots = int(slots)
		if slots < 2:
			raise ValueError("a ring needs at least 2 slots, not {}".format(slots))
		self.name = name
		size = RingBytes(shape, dtype, slots)
		self.shm = self._Make(name, size)
		# The header, with state 0 until it is complete
		header = np.frombuffer(self.shm.buf, np.uint8, HEADER_BYTES)
		header[:] = 0
		header[:8] = np.frombuffer(MAGIC, np.uint8)
		frameBytes = int(np.prod(shape)) * dtype.itemsize
		values = {"version": VERSION, "headerBytes": HEADER_BYTES, "slots": slots,
			"height": shape[0], "width": shape[1], "channels": shape[2],
			"slotBytes": SlotBytes(frameBytes), "frameBytes": frameBytes,
			# Random, not the time: Windows' clock moves in steps of ms, and a
			# ring made again within one (_Make) must still look new to readers
			"ringId": int.from_bytes(os.urandom(8), "little") >> 1,
			"frameRate": float(frameRate), "camera": int(camera)}
		for key, value in values.items():
			at, kind = _HEADER[key]
			np.ndarray((1,), kind, self.shm.buf, at)[0] = value
		text = dtype.str.encode("ascii")[:8]
		header[_DTYPE_AT:_DTYPE_AT + len(text)] = np.frombuffer(text, np.uint8)
		text = str(cameraName).encode("utf-8")[:_NAME_BYTES]
		header[_NAME_AT:_NAME_AT + len(text)] = np.frombuffer(text, np.uint8)
		del header
		self.ring = Ring(self.shm)
		self.ring.slotFields[:] = 0
		self.ring.Set("state", RECORDING)
		self.written = 0
		self.notWritten = 0

	@staticmethod
	def _Make(name, size):
		try:
			return shared_memory.SharedMemory(name, create=True, size=size)
		except FileExistsError:
			pass
		# A ring of that name is there: an earlier campy's that did not end
		# (killed), or one a reader still holds open. Its readers are told it
		# ended. On POSIX its name goes and a new one is made. On Windows, the
		# memory goes with its last handle: a new one is made once no reader
		# holds the old one, or else the old one is used again, if large enough.
		old = Attach(name)
		try:
			ring = Ring(old)
		except ValueError:
			old.close()
			raise FileExistsError("shared memory {} is there, and is not a campy live feed".format(name))
		ring.Set("state", ENDED)
		ring.Release() # closes old
		if os.name == "posix":
			_Unlink(old)
		try:
			return shared_memory.SharedMemory(name, create=True, size=size)
		except FileExistsError:
			if os.name == "posix":
				raise
		old = Attach(name)
		if old.size >= size:
			return old
		old.close()
		raise FileExistsError("an earlier live feed {} is still open, too small for these frames "
			"(a program reading it?)".format(name))

	def Write(self, img, frameNumber, hostTime, timeStamp=float("nan")):
		"""
		Copies one frame into its slot. Returns False, without writing, for a
		frame of another size or type than the ring's.
		"""
		ring = self.ring
		img = np.asarray(img)
		if img.dtype != ring.dtype or FrameShape(img) != ring.shape:
			self.notWritten += 1
			ring.Set("notWritten", self.notWritten)
			return False
		n = int(frameNumber)
		s = n % ring.slots
		fields = ring.slotFields[s]
		fields[BEGIN] = n
		np.copyto(ring.frames[s], img.reshape(ring.shape))
		fields[FRAME] = n
		fields[HOST] = int(hostTime)
		ring.stamps[s] = timeStamp
		fields[WRITTEN] = time.perf_counter_ns()
		fields[END] = n
		ring.Set("latest", n)
		self.written += 1
		ring.Set("written", self.written)
		return True

	def Close(self):
		# Readers see the ring end, and keep what they have open; the name
		# goes, unless a newer ring has it now (on Windows, the memory goes
		# with its last handle)
		if self.ring is None:
			return
		ringId = self.ring.ringId
		try:
			self.ring.Set("state", ENDED)
		finally:
			self.ring.Release()
			self.ring = None
			if os.name == "posix":
				try:
					if self._Named(ringId):
						self.shm.unlink()
					else:
						from multiprocessing import resource_tracker
						resource_tracker.unregister(self.shm._name, "shared_memory")
				except Exception:
					pass

	def _Named(self, ringId):
		# Whether the name is still this ring's
		try:
			shm = Attach(self.name)
		except FileNotFoundError:
			return False
		try:
			ring = Ring(shm)
			mine = ring.ringId == ringId
			ring.Release()
			return mine
		except ValueError:
			shm.close()
			return False


class Frame:
	"""One frame read from a ring: the image and its fields."""
	__slots__ = ("image", "frameNumber", "hostTime", "timeStamp", "written")

	def __init__(self, image, frameNumber, hostTime, timeStamp, written):
		self.image, self.frameNumber, self.hostTime = image, frameNumber, hostTime
		self.timeStamp, self.written = timeStamp, written


class RingReader:
	"""
	Reads a camera's ring, as another process: Latest says the newest frame
	number, and Read copies a frame out, if the ring still holds it whole.
	"""

	def __init__(self, name):
		self.name = name
		self.shm = Attach(name)
		self.ring = Ring(self.shm)
		self.shape, self.dtype, self.slots = self.ring.shape, self.ring.dtype, self.ring.slots
		self.cameraName = self.ring.cameraName
		self.ringId = self.ring.ringId

	def Latest(self):
		return int(self.ring.fields["latest"][0])

	def State(self):
		return int(self.ring.fields["state"][0])

	def Ended(self):
		return self.State() == ENDED

	def Replaced(self):
		# Windows: a new recording reused this ring's memory (RingWriter._Make)
		return int(self.ring.fields["ringId"][0]) != self.ringId

	def Info(self, key):
		return self.ring.Get(key)

	def Read(self, frameNumber, out=None):
		"""
		Frame frameNumber, copied into out (an array of the ring's frame shape
		and type) or a new array. None when the ring does not hold it: not
		written yet, already replaced, or replaced while it was being copied.
		"""
		ring = self.ring
		n = int(frameNumber)
		s = n % ring.slots
		fields = ring.slotFields[s]
		if fields[END] != n:
			return None
		frameNumber, hostTime, written = int(fields[FRAME]), int(fields[HOST]), int(fields[WRITTEN])
		timeStamp = float(ring.stamps[s])
		if out is None:
			out = np.empty(ring.shape, ring.dtype)
		np.copyto(out, ring.frames[s])
		if fields[BEGIN] != n:
			return None # the writer started on this slot again as it was copied
		return Frame(out, frameNumber, hostTime, timeStamp, written)

	def Close(self):
		if self.ring is not None:
			self.ring.Release()
			self.ring = None


# The frames a camera sends for each pixelFormatInput, as far as it says:
# channels per pixel (of uint8). Others are learned from the first frame.
CHANNELS = {"gray": 1, "bayer_bggr8": 1, "bayer_rggb8": 1, "bayer_gbrg8": 1, "bayer_grbg8": 1,
	"rgb24": 3, "bgr24": 3}


class Feed:
	"""
	A camera process's live feed (liveFeed): its ring, made as the camera
	starts grabbing (Prepare), from the frame size it opened with, or else
	from the first frame; and written with every frame. A frame of another
	size or type makes a new ring of its size. Whatever goes wrong is said
	once, and the feed stops; the recording goes on.
	"""

	def __init__(self, cam_params, say=print):
		self.on = bool(cam_params.get("liveFeed", False))
		self.cameraName = cam_params.get("cameraName", "")
		self.name = RingName(str(cam_params.get("liveFeedName", DEFAULT_NAME)), cam_params.get("n_cam", 0))
		self.slots = int(cam_params.get("liveFeedSlots", DEFAULT_SLOTS))
		self.frameRate = float(cam_params.get("frameRate", 0) or 0)
		self.camera = int(cam_params.get("n_cam", 0))
		self.say = say
		self.writer = None

	def Prepare(self, cam_params):
		# The ring, before the first frame, when the camera says what it sends
		channels = CHANNELS.get(str(cam_params.get("pixelFormatInput")))
		if not self.on or channels is None:
			return
		self._Do(lambda: self._Make((int(cam_params["frameHeight"]), int(cam_params["frameWidth"]), channels),
			np.uint8), "as it started")

	def _Make(self, shape, dtype):
		self.Close()
		self.writer = RingWriter(self.name, shape, dtype, self.slots, self.frameRate, self.camera, self.cameraName)
		self.say("{}: live feed in shared memory {} ({} slots of {}x{}x{} {}).".format(
			self.cameraName, self.name, self.slots, *(tuple(shape) + (np.dtype(dtype),))))

	def _Do(self, step, when):
		try:
			step()
		except Exception as e:
			self.on = False
			self.say("{}: the live feed stopped, {}: {}. The recording goes on.".format(self.cameraName, when, e))
			self.Close()

	def Write(self, img, frameNumber, hostTime, timeStamp):
		if self.on:
			self._Do(lambda: self._Write(img, frameNumber, hostTime, timeStamp), "at frame {}".format(frameNumber))

	def _Write(self, img, frameNumber, hostTime, timeStamp):
		img = np.asarray(img)
		writer = self.writer
		if writer is None or img.dtype != writer.ring.dtype or FrameShape(img) != writer.ring.shape:
			self._Make(FrameShape(img), img.dtype)
		self.writer.Write(img, frameNumber, hostTime, timeStamp)

	def Close(self):
		writer, self.writer = self.writer, None
		if writer is not None:
			try:
				writer.Close()
			except Exception:
				pass
