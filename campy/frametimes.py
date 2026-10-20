"""
Frame times, written to disk while the recording runs.

Each camera's folder gets frametimes.npy (2 x frames: campy's frame number,
and the camera's timestamp in seconds from its first frame) and hosttimes.npy
(when each frame reached the computer, time.monotonic_ns). They used to be
kept in Python lists and saved when the recording ended: about 110 bytes of
memory per frame (780 MB per camera per day at 80 fps), and nothing at all if
campy stopped without closing the camera. Now each file grows as frames
arrive, a batch about every second, and is a valid .npy after every batch:
its header, padded to a fixed size, is rewritten in place to say how many
frames it holds. A recording cut short keeps its frames up to the last batch.

frametimes.npy is stored column by column (the .npy format's fortran_order),
so that each frame's column can be appended to the 2 x frames array; np.load
returns the same array as before, of the same shape and values.

The batches are written by a thread of their own (FrameTimes' writer), not by
the camera thread that adds the frames: a write, its header rewrite and its
flush sometimes wait tens of ms for the disk (ext4's journal, while ffmpeg
writes the videos), and the camera thread, waiting with them, gave the frames
after late arrival times, and handed them late to the live feed (the EVO-X2,
3 cameras at 60 fps: every gap over 25 ms in hosttimes.npy, up to 48 ms, was
right after a batch). A recording cut short keeps its frames up to the last
batch written; Close waits for the batches still queued.
"""
import logging, os, queue, struct, threading
import numpy as np
from campy import newfiles

HEADER_BYTES = 128 # the whole .npy header: a multiple of 64, as numpy writes it, with room for any shape


class GrowingNpy:
	"""
	An .npy file that grows. rows None: a 1-D array, appended to value by
	value. rows n: an n x N array, appended to column by column.
	"""

	def __init__(self, path, dtype, rows=None, header_bytes=HEADER_BYTES):
		self.dtype, self.rows, self.count = np.dtype(dtype), rows, 0
		self.header_bytes = header_bytes # a multiple of 64, with room for the shape to grow
		self.file = newfiles.Open(path, "wb") # never over a recording's file
		self.WriteHeader()

	def WriteHeader(self):
		shape = (self.count,) if self.rows is None else (self.rows, self.count)
		text = "{{'descr': {!r}, 'fortran_order': {}, 'shape': {}, }}".format(
			np.lib.format.dtype_to_descr(self.dtype), self.rows is not None, shape)
		text = text.ljust(self.header_bytes - 11) + "\n" # magic, version and length take 10 bytes
		self.file.seek(0)
		self.file.write(b"\x93NUMPY\x01\x00" + struct.pack("<H", len(text)) + text.encode("latin1"))
		self.file.seek(0, os.SEEK_END)

	def Append(self, values):
		# values: one per frame, or one column (of rows values) per frame. The
		# frames go in first and the header after, so a file cut off between
		# the two still says only what it holds (np.load reads that many).
		values = np.asarray(values, self.dtype)
		self.file.write(values.tobytes())
		self.count += len(values)
		self.WriteHeader()
		self.file.flush()

	def Truncate(self, count):
		# Keeps the first count values (or columns), and drops the rest
		self.count = min(self.count, max(0, int(count)))
		self.file.truncate(self.header_bytes + self.count * self.dtype.itemsize * (self.rows or 1))
		self.WriteHeader()
		self.file.flush()

	def Close(self):
		self.file.close()


class Writer:
	"""
	A thread of its own that makes the writes handed to it, one after
	another, in order: the thread that hands them over (a camera's, or
	campy-live's) goes on at once, whatever the disk makes the write wait.
	A daemon, started with the first write, so a process that ends without
	Close is not held open by it; its files keep what was written. A write
	that fails is said once, and the ones after it are left out, as a file
	the failed write may have cut is not appended to; Close raises it.
	"""

	def __init__(self, name, what):
		self.name, self.what = name, what # what: said with an error ("the frame times in <folder>")
		self.writes = queue.Queue() # (function, args); None stops the thread
		self.thread = None
		self.error = None

	def Do(self, write, *args):
		if self.thread is None:
			self.thread = threading.Thread(target=self.Run, daemon=True, name=self.name)
			self.thread.start()
		self.writes.put((write, args))

	def Run(self):
		while True:
			item = self.writes.get()
			try:
				if item is None:
					return
				if self.error is None:
					item[0](*item[1])
			except Exception as e:
				self.error = e
				logging.error("Could not write {}: {}".format(self.what, e))
			finally:
				self.writes.task_done()

	def Wait(self):
		# Until every write handed over is made
		self.writes.join()

	def Close(self):
		# Waits for the writes still queued; raises the one that failed
		if self.thread is not None:
			self.writes.put(None)
			self.thread.join()
			self.thread = None
		if self.error is not None:
			raise self.error


class FrameTimes:
	"""
	One camera's frametimes.npy and hosttimes.npy, written in batches of
	`every` frames, and the rest when closed. The camera's timestamps count
	from its first frame's. Add hands each batch to the writer thread, which
	writes it while the camera goes on; Close waits for it.
	"""

	def __init__(self, folder, every):
		self.folder, self.every = folder, max(1, int(round(float(every))))
		self.files = None # made with the first batch, as before only for a camera that grabbed
		self.frames, self.hosts = [], [] # the batch not yet handed to the writer
		self.first = None # the first frame's camera timestamp
		self.last = None  # the last frame's, from the first
		self.count = 0
		self.writer = Writer("frametimes " + os.path.basename(os.path.normpath(folder)),
			"the frame times in {}".format(folder))

	def Add(self, frameNumber, timeStamp, hostTime):
		if self.first is None:
			self.first = timeStamp
		self.last = timeStamp - self.first
		self.frames.append((frameNumber, self.last))
		self.hosts.append(hostTime)
		self.count += 1
		if len(self.hosts) >= self.every:
			self.Write()

	def Write(self):
		# Hands the batch to the writer thread
		if not self.hosts:
			return
		self.writer.Do(self.WriteBatch, self.frames, self.hosts)
		self.frames, self.hosts = [], []

	def WriteBatch(self, frames, hosts):
		if self.files is None:
			os.makedirs(self.folder, exist_ok=True)
			self.files = (GrowingNpy(os.path.join(self.folder, "frametimes.npy"), np.float64, rows=2),
				GrowingNpy(os.path.join(self.folder, "hosttimes.npy"), np.int64))
		self.files[0].Append(frames)
		self.files[1].Append(hosts)

	def Wait(self):
		# Until every batch handed to the writer is in the files
		self.writer.Wait()

	def Close(self, keep=None):
		# keep: how many frames to keep, from the first, when the video took
		# fewer than were added (its ffmpeg stopped). The rest are dropped from
		# both files. None keeps them all.
		self.Write()
		try:
			self.writer.Close()
			if keep is not None and keep < self.count:
				self.count = max(0, int(keep))
				if self.files is not None:
					for f in self.files:
						f.Truncate(self.count)
		finally:
			if self.files is not None:
				for f in self.files:
					f.Close()
