"""
campy-live's results as it makes them, in shared memory, for a program that
shows them as campy records (campy-live-view, campy/liveview.py).

campy-live logs one row per set of frames (campy/live.py says the fields);
with --results-feed on (the default) it also puts each row in a ring of
shared memory of its own, "<name>-results" (campy-live-results). Writing a
row is one copy of a few kB (3 cameras, 3 animals, 12 nodes: 3 kB), and never
waits for a reader, as campy never waits for the readers of its frames.

The ring is a live feed ring (campy/livefeed.py: the same header, slots and
check that a slot was read whole) whose frames are rows: each slot holds one
row's bytes, as a 1 x <row bytes> x 1 frame of uint8, numbered 1, 2, ... in
the order campy-live made the rows. Which camera frames a row was made from
is its own "frame" field: campy-live skips the sets that came while it was
busy, so the rows' frame numbers have gaps. The header's description is JSON:

  kind               "campy-live results"
  version            1
  row                the rows' numpy dtype (np.lib.format.dtype_to_descr)
  cameras            campy's camera names, in feed order
  rings              the cameras' rings, in feed order
  nodes              the model's node names, or null
  edges              the skeleton's edges, as pairs of node indices, or null
  calibration        the calibration.toml's full path, or null (2D only)
  calibration_names  the cameras' names in it, in feed order, or null
  animals            with a calibration: the identities, 0 .. animals - 1
  frame_rate         the feed's

Like livefeed, it needs only numpy and Python's standard library.
"""

import json

import numpy as np

from campy import livefeed, strictjson

KIND, VERSION = "campy-live results", 1
DEFAULT_SLOTS = 64 # 2 s of rows at 30 sets a second


def RingName(prefix):
	return "{}-results".format(prefix)


def RowType(descr):
	# The rows' dtype from its descr, as JSON gives it back (lists for tuples)
	return np.dtype([(str(f[0]), str(f[1])) + ((tuple(f[2]),) if len(f) > 2 else ()) for f in descr])


class Publisher:
	"""
	campy-live's side: Write puts each row in the ring. Whatever goes wrong
	is said once, and the rows stop going in; campy-live goes on.
	"""

	def __init__(self, name, rows_type, say=print, slots=DEFAULT_SLOTS, **description):
		self.name, self.say = name, say
		self.rows_type = np.dtype(rows_type)
		self.count = 0 # rows written
		self.writer = None
		text = strictjson.dumps(dict(kind=KIND, version=VERSION, row=np.lib.format.dtype_to_descr(self.rows_type),
			**description), separators=(",", ":"))
		try:
			self.writer = livefeed.RingWriter(name, (1, self.rows_type.itemsize, 1), np.uint8, slots,
				frameRate=description.get("frame_rate") or 0.0, cameraName=KIND, description=text)
		except Exception as e:
			self.say("Could not put the results in shared memory {}: {}. campy-live goes on without it "
				"(campy-live-view shows the frames only).".format(name, e))

	def Write(self, row):
		if self.writer is None:
			return
		try:
			data = np.frombuffer(np.asarray(row, self.rows_type).tobytes(), np.uint8).reshape(1, -1, 1)
			self.writer.Write(data, self.count + 1, int(np.max(row["host_ns"])), float(row["frame"]))
			self.count += 1
		except Exception as e:
			self.say("The results in shared memory {} stopped, at row {}: {}. campy-live goes on.".format(
				self.name, self.count + 1, e))
			self.Close()

	def Close(self):
		writer, self.writer = self.writer, None
		if writer is not None:
			try:
				writer.Close()
			except Exception:
				pass


class Results:
	"""
	A reader's side: the description, and the rows. Raises FileNotFoundError
	when campy-live has not made the ring (yet), ValueError when the shared
	memory of that name is not campy-live's results (or is being made).
	"""

	def __init__(self, name):
		self.name = name
		self.reader = livefeed.RingReader(name)
		try:
			description = strictjson.loads(self.reader.Description() or "{}")
			if description.get("kind") != KIND:
				raise ValueError("{} is not campy-live's results".format(name))
			if description.get("version") != VERSION:
				raise ValueError("{} has campy-live's results of version {}; this campy reads version {}".format(
					name, description.get("version"), VERSION))
			self.description = description
			self.rows_type = RowType(description["row"])
			if self.rows_type.itemsize != self.reader.shape[1]:
				raise ValueError("{}: its rows are {} bytes, not the {} its description says".format(
					name, self.reader.shape[1], self.rows_type.itemsize))
		except BaseException:
			self.reader.Close()
			raise
		self.slots = self.reader.slots
		self.buffer = np.empty(self.reader.shape, np.uint8)

	def Latest(self):
		# How many rows campy-live has written
		return self.reader.Latest()

	def Read(self, n):
		# Row n (1, 2, ...), or None if the ring no longer holds it whole
		if self.reader.Read(n, self.buffer) is None:
			return None
		return np.frombuffer(self.buffer.tobytes(), self.rows_type)[0]

	def Newest(self, after=0):
		"""
		(n, row): the newest row written after row `after` that the ring holds
		whole, or (latest, None) when there is none.
		"""
		latest = self.Latest()
		for n in range(latest, max(after, latest - self.slots + 1), -1):
			row = self.Read(n)
			if row is not None:
				return n, row
		return latest, None

	def Ended(self):
		# campy-live ended, or made the ring again (Windows reuses the memory)
		return self.reader.Ended() or self.reader.Replaced()

	def Close(self):
		self.reader.Close()
