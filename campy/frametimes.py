"""
Frame times, written to disk while the recording runs.

Each camera's folder gets frametimes.npy (2 x frames: campy's frame number,
and the camera's timestamp in seconds from its first frame) and hosttimes.npy
(when each frame reached the computer, in ns on campy's host clock,
campy/clock.py: one clock in every process). They used to be
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

A batch whose write fails (a disk error, a USB or network disk that dropped
out for a moment, a file an antivirus or Windows Search holds) is kept in
memory, and written first, before the next batch's frames, by the next write
that works: the files stay one row per video frame, in order, and come out
whole once the disk takes writes again (24 bytes a frame wait, 7 MB an hour
at 80 fps). Close tries again a few times. Frame times still not written
then are the camera's frameTimesError: campy's recording failed (exit code
1), and the error says how many of the video's frames each file has. The
rows the files do have are the video's first frames, one for one. Before,
the first failed write stopped every later one for the rest of the
recording, without a failure: frametimes.npy ended there while the video
went on, and campy exited 0.
"""
import logging, os, queue, struct, threading, time
import numpy as np
from campy import newfiles
from campy.utils.utils import Say, ErrorText

HEADER_BYTES = 128 # the whole .npy header: a multiple of 64, as numpy writes it, with room for any shape

# As FrameTimes closes, frame times a failed write left in memory are tried again this many times, this long apart
CLOSE_TRIES = 3
CLOSE_WAIT_S = 1.0


class GrowingNpy:
	"""
	An .npy file that grows. rows None: a 1-D array, appended to value by
	value. rows n: an n x N array, appended to column by column.
	"""

	def __init__(self, path, dtype, rows=None, header_bytes=HEADER_BYTES):
		self.path = path
		self.dtype, self.rows, self.count = np.dtype(dtype), rows, 0 # count: the values (or columns) it surely holds
		self.header_bytes = header_bytes # a multiple of 64, with room for the shape to grow
		self.broken = False # a write failed: Mend, before the next
		self.file = newfiles.Open(path, "w+b") # never over a recording's file; read back by Drop
		self.WriteHeader()

	def Width(self):
		# Bytes per value, or per column
		return self.dtype.itemsize * (self.rows or 1)

	def WriteHeader(self, count=None):
		count = self.count if count is None else count
		shape = (count,) if self.rows is None else (self.rows, count)
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
		# count goes up only once both are out: after a write that raised, the
		# file holds count values, and may hold part of the write besides
		# (Mend cuts it off), so the same values can be written again.
		values = np.asarray(values, self.dtype)
		self.Mend()
		count = self.count + len(values)
		try:
			self.file.write(values.tobytes())
			self.WriteHeader(count)
			self.file.flush()
		except BaseException:
			self.broken = True
			raise
		self.count = count

	def Mend(self):
		# After a write that failed: the file object may still hold part of it,
		# which it would write wherever it next could, and the file may hold a
		# part, or a header that says more than it has. So it is opened again,
		# what the old one held left out, and cut back to its count values.
		if not self.broken:
			return
		try:
			self.file.close()
		except Exception:
			pass # closed all the same: what it still held is left out
		self.file = open(self.path, "r+b") # this process's own file (newfiles.Open made it)
		self.file.truncate(self.header_bytes + self.count * self.Width())
		self.WriteHeader()
		self.file.flush()
		self.broken = False

	def Truncate(self, count):
		# Keeps the first count values (or columns), and drops the rest
		self.Mend()
		count = min(self.count, max(0, int(count)))
		self.file.truncate(self.header_bytes + count * self.Width())
		self.count = count # the file holds no more than this now, whatever the header says
		self.WriteHeader()
		self.file.flush()

	def Drop(self, places):
		# Leaves out the values (or columns) at these places, 0 = the first:
		# the file is rewritten in place, from the first one left out
		places = sorted(set(int(p) for p in places if 0 <= int(p) < self.count))
		if not places:
			return
		self.Mend()
		width = self.Width()
		start = places[0]
		self.file.seek(self.header_bytes + start * width)
		tail = np.frombuffer(self.file.read((self.count - start) * width), self.dtype).reshape(self.count - start, -1)
		tail = np.delete(tail, [p - start for p in places], axis=0)
		self.file.seek(self.header_bytes + start * width)
		self.file.write(tail.tobytes())
		self.count -= len(places)
		self.file.truncate(self.header_bytes + self.count * width)
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
	the failed write may have cut is not appended to; Close raises it
	(campy-live's log). FrameTimes' writes never raise: a batch that could
	not be written waits for the next (FrameTimes.Flush).
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

	A write that fails leaves its frames waiting, in memory, and the next
	write puts them in first (Flush), each file on its own: the files stay
	one row per frame, in order. Close tries again (CLOSE_TRIES), and then
	says, as error, what the files still lack.
	"""

	FILES = (("frametimes.npy", np.float64, 2), ("hosttimes.npy", np.int64, None))

	def __init__(self, folder, every, name=None):
		self.folder, self.every = folder, max(1, int(round(float(every))))
		self.name = name or os.path.basename(os.path.normpath(folder)) # the camera's, in what it says
		self.files = [None, None] # each made with the first batch, as before only for a camera that grabbed
		self.frames, self.hosts = [], [] # the batch not yet handed to the writer
		self.first = None # the first frame's camera timestamp
		self.last = None  # the last frame's, from the first
		self.count = 0 # frames added; once closed, the frames the video holds
		# The writer thread's: for each file, the frames a failed write left
		# out, which go in before the next ones
		self.waiting = ([], [])
		self.failedWrites = 0 # writes that failed, their frames written later or never
		self.failing = None # what the latest write that failed said, while frames wait
		self.error = None # once closed: what the files lack, or None when they have every frame of the video
		self.saved = (0, 0) # once closed: the rows each file holds
		self.writer = Writer("frametimes " + self.name, "the frame times in {}".format(folder))

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
		# On the writer thread: as numbers (24 bytes a frame), behind any
		# frames still waiting. Never raises, so the writer goes on.
		self.waiting[0].append(np.asarray(frames, np.float64).reshape(-1, 2))
		self.waiting[1].append(np.asarray(hosts, np.int64))
		self.Flush()

	def Waiting(self):
		# Frames not yet in each file
		return tuple(sum(len(a) for a in w) for w in self.waiting)

	def Flush(self):
		# Writes the frames waiting, file by file, first made with the first
		# batch. One that fails keeps its frames waiting, for the next try.
		failed = None
		for i, (file_name, dtype, rows) in enumerate(self.FILES):
			if not self.waiting[i]:
				continue
			values = np.concatenate(self.waiting[i]) if len(self.waiting[i]) > 1 else self.waiting[i][0]
			self.waiting[i][:] = [values]
			try:
				if self.files[i] is None:
					os.makedirs(self.folder, exist_ok=True)
					self.files[i] = GrowingNpy(os.path.join(self.folder, file_name), dtype, rows=rows)
				self.files[i].Append(values)
				self.waiting[i].clear()
			except Exception as e:
				failed = failed or (file_name, e)
		if failed is not None:
			self.failedWrites += 1
			if self.failing is None:
				Say("{}: could not write {} ({}): {}. Its frame times wait in memory, and go in with the next write "
					"that works.".format(self.name, failed[0], self.folder, ErrorText(failed[1])))
			self.failing = "{}: {}".format(failed[0], ErrorText(failed[1]))
		elif self.failing is not None:
			self.failing = None
			Say("{}: frame times written again, after {} failed write{}: frametimes.npy and hosttimes.npy have every "
				"frame so far.".format(self.name, self.failedWrites, "" if self.failedWrites == 1 else "s"))

	def Wait(self):
		# Until every batch handed to the writer is in the files, or waiting
		self.writer.Wait()

	def Close(self, keep=None, drop=()):
		# keep: how many frames to keep, from the first, when the video took
		# fewer than were added (its ffmpeg stopped). The rest are dropped from
		# both files. None keeps them all. drop: the places (0 = the first
		# frame added) of frames to leave out besides, that an ffmpeg was
		# given but its file does not hold (segments.py). A file that lacks
		# some of the last frames (writes that failed) is cut the same way, so
		# the rows it has are still the video's first frames. Never raises:
		# what went wrong is in error.
		self.Write()
		problems = []
		try:
			self.writer.Close()
		except Exception as e: # FrameTimes' writes never raise; anything else, said
			problems.append(ErrorText(e))
		for _ in range(CLOSE_TRIES):
			if not any(self.Waiting()):
				break
			time.sleep(CLOSE_WAIT_S)
			self.Flush()
		if keep is not None and keep < self.count:
			self.count = max(0, int(keep))
		drop = sorted(set(int(p) for p in drop or () if 0 <= int(p) < self.count))
		self.count -= len(drop)
		for i, f in enumerate(self.files):
			if f is None:
				continue
			try:
				if keep is not None and keep < f.count:
					f.Truncate(keep)
				f.Drop(drop)
			except Exception as e:
				problems.append("could not cut {} to the video's frames: {}".format(self.FILES[i][0], ErrorText(e)))
			finally:
				try:
					f.Close()
				except Exception as e:
					problems.append("could not close {}: {}".format(self.FILES[i][0], ErrorText(e)))
		self.saved = tuple(f.count if f is not None else 0 for f in self.files)
		short = [(self.FILES[i][0], n) for i, n in enumerate(self.saved) if n < self.count]
		if short:
			why = self.failing or (problems[0] if problems else "?")
			if len(short) == 2 and short[0][1] == short[1][1]:
				has = "frametimes.npy and hosttimes.npy have {} of the video's {} frames".format(short[0][1], self.count)
			else:
				has = ", ".join("{} has {} of the video's {} frames".format(n, k, self.count) for n, k in short)
			self.error = "could not write all its frame times: {} ({}, {} failed write{})".format(has, why,
				self.failedWrites, "" if self.failedWrites == 1 else "s")
		elif problems:
			self.error = "; ".join(problems)


class TriggerGaps:
	"""
	The trigger pulses a triggered camera got no frame for, counted as it
	records, from its own timestamps: a step of n periods between two frames
	is n - 1 pulses without a frame (the camera missed them, or the trigger
	paused). Every frame the camera sent counts, those campy dropped too, so
	campy's own drops are not counted here. The period is the camera's own:
	the median of its first LEARN steps, so a trigger running at another rate
	than frameRate is no gap, and the frames before it is known are counted
	once it is. campy-align does the same over frametimes.npy afterwards.
	"""

	LEARN = 30
	GAP = 1.5 # a step longer than this many periods has pulses without a frame

	def __init__(self):
		self.period = None
		self.first = [] # the first LEARN + 1 timestamps, until the period is known
		self.last = None
		self.missed = 0 # pulses without a frame so far
		self.frame = None # the frame just after the latest of them

	def Add(self, timeStamp, frameNumber):
		"""A frame's timestamp (s, the camera's clock) and number: returns the pulses without a frame found now."""
		if self.period is None:
			self.first.append((timeStamp, frameNumber))
			if len(self.first) <= self.LEARN:
				return 0
			steps = sorted(b[0] - a[0] for a, b in zip(self.first, self.first[1:]) if b[0] > a[0])
			if not steps:
				self.first = [(timeStamp, frameNumber)]
				return 0
			self.period = steps[len(steps) // 2]
			first, self.first, self.last = self.first, [], timeStamp
			new = 0
			for a, b in zip(first, first[1:]):
				n = self.Count(a[0], b[0])
				if n:
					new, self.frame = new + n, b[1]
			self.missed += new
			return new
		new = self.Count(self.last, timeStamp)
		self.last = timeStamp
		if new:
			self.missed += new
			self.frame = frameNumber
		return new

	def Count(self, a, b):
		steps = (b - a) / self.period
		return int(round(steps)) - 1 if steps > self.GAP else 0
