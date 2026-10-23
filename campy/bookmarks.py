"""
Bookmarks of moments in a recording: one CSV line each, in bookmarks.csv in
the recording's folder. campy-live-view makes them (m, Shift+1-9, its Qt
panel's button), and so does campy-session's panel: both write the same
file, the same columns. Only the standard library: campy-session's
environment has no OpenCV.

Each bookmark is numbered on from the file's last line as it is written, so
two programs writing into one file (the view and the panel) never give two
bookmarks the same number.
"""

import csv, os
from collections import deque

BOOKMARKS = "bookmarks.csv" # in the recording's folder
RECENT = 6 # the bookmarks a panel lists


def say(text):
	print(text, flush=True)


def MarkText(mark):
	# A bookmark, in a line: 3 at 17:03:12, frame 1,203, event 2: rears
	text = "{} at {}, frame {}".format(mark["number"], mark["time"][11:19],
		"-" if mark["frame"] is None else "{:,}".format(int(mark["frame"])))
	if mark["event"] != "mark":
		text += ", event " + mark["event"]
	return text + (": " + mark["note"] if mark["note"] else "")


def Existing(path, columns):
	# The bookmarks in the file already, under these columns: 0 when it is new, None when its columns are others.
	# Its last line's number, so that a file another program also writes is numbered on from where it is
	try:
		with open(path, newline="") as f:
			rows = [r for r in csv.reader(f) if r]
	except FileNotFoundError:
		return 0
	if not rows:
		return 0
	if rows[0] != columns:
		return None
	try:
		return max(len(rows) - 1, int(rows[-1][0])) if len(rows) > 1 else 0
	except (ValueError, IndexError):
		return len(rows) - 1


class Bookmarks:
	"""
	The bookmarks made in the view. Each is a line of a CSV file, added as it
	is made: the file opened, the line written, flushed and synced to the
	disk, the file closed, so a crash (or the power going) loses none made
	before it. The columns:

	  number       1, 2, ... in the file (on from its last, after a restart)
	  time         the computer's clock, local time, to the ms
	  host_ns      now on the clock of campy's hosttimes.npy (campy/clock.py)
	  event        mark (m), or 1-9 (Shift+1-9)
	  note         the note typed in the Qt panel, or empty
	  frame        the frame number shown in the view: campy's frame number,
	               as in frametimes.npy's first row (empty: no frames shown)
	  frame_<i>    camera i's (in feed order: campy's camera index) newest
	               frame then; the frame shown is a frame or two older
	  host_ns_<i>  when that frame reached the computer (hosttimes.npy's)
	  recording    the recording's folder, or empty

	The file is bookmarks.csv in the recording's folder, when the cameras'
	feeds say where that is, else fallback (--bookmarks); there too when the
	recording's cannot be written. A file there of other columns (as many
	cameras) is left as it is: they go to <name>_2.csv (_3, ...) beside it.
	"""

	def __init__(self, fallback, say=say):
		self.fallback = fallback
		self.say = say
		self.recent = deque(maxlen=RECENT) # the last ones, the newest last: their fields, and "path"
		self.count = 0 # made, in all
		self.files = {} # (file asked for, columns): [the file written, its last number]

	@staticmethod
	def Columns(cameras):
		return ["number", "time", "host_ns", "event", "note", "frame"] + \
			[f.format(c) for c in range(cameras) for f in ("frame_{}", "host_ns_{}")] + ["recording"]

	def Where(self, recording):
		# Where a bookmark goes first
		return os.path.join(recording, BOOKMARKS) if recording else self.fallback

	def Add(self, mark):
		"""
		Writes mark (a dict: time, host_ns, event, note, frame, cameras: a
		(frame, host_ns) per camera, recording) into its file, numbering it.
		Returns the file, or None when it could be written nowhere (said).
		"""
		columns = self.Columns(len(mark["cameras"]))
		for base in dict.fromkeys([self.Where(mark["recording"]), self.fallback]):
			try:
				path = self.Write(base, columns, mark)
			except OSError as e:
				self.say("Could not write the bookmark into {}: {}.".format(base, e))
				continue
			mark["path"] = path
			self.count += 1
			self.recent.append(mark)
			return path
		return None

	def Write(self, base, columns, mark):
		key = (base, tuple(columns))
		if key not in self.files:
			path, k = base, 1
			number = Existing(path, columns)
			while number is None:
				k += 1
				stem, ext = os.path.splitext(base)
				path = "{}_{}{}".format(stem, k, ext)
				number = Existing(path, columns)
			self.files[key] = [path, number]
		path = self.files[key][0]
		# Numbered on from the file's last line as it is now: another program (the view, the panel) may have added some
		number = Existing(path, columns)
		mark["number"] = (number if number is not None else self.files[key][1]) + 1
		def Blank(v):
			return "" if v is None else v
		line = [mark["number"], mark["time"], mark["host_ns"], mark["event"], mark["note"], Blank(mark["frame"])] + \
			[Blank(v) for pair in mark["cameras"] for v in pair] + [mark["recording"] or ""]
		os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
		new = not os.path.exists(path) or os.path.getsize(path) == 0
		with open(path, "a", newline="") as f:
			writer = csv.writer(f)
			if new:
				writer.writerow(columns)
			writer.writerow(line)
			f.flush()
			os.fsync(f.fileno())
		self.files[key][1] = mark["number"]
		return path
