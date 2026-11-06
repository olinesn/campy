"""
A camera's video in several files, after ffmpeg was restarted mid-recording
(restartFfmpeg, writer.WriteFrames).

When a camera's ffmpeg stops while the camera records (killed, crashed, an
encoder error), the camera goes on grabbing, a new ffmpeg starts, and the
video goes on in a new file next to the first: 0.mp4, then 0.part2.mp4,
0.part3.mp4 (PartName). The camera never stopped, so frame numbers go on
from one file to the next. The frames that were inside the stopped ffmpeg,
given to it but not yet in its file (about a second), are lost: CountFrames
reads which frames the old file holds, from its packets' timestamps, without
decoding it.

<camera>/segments.csv, written by the camera's process only when ffmpeg was
restarted, says which frames each file holds, one row per file:

  file         the video file, in its camera's folder
  first_frame  campy's frame number of the first frame given to this file's ffmpeg
  last_frame   and of the last
  frames       frames in the file; empty while it is written, or if they
               could not be counted
  lost         frames given to its ffmpeg that are not in the file
  lost_frames  their frame numbers, as ranges: "3991-4017;4020"
  ended        why the file ended: "ffmpeg stopped ...", or "recording ended"

frametimes.npy and hosttimes.npy keep one row per video frame, the files
read one after the other: the lost frames' rows are taken out as the
recording ends (unicam.SaveMetadata), as dropped frames are left out. A
recording that was cut short (campy killed, a power cut) before that keeps
their rows; LostFrames names them, and campy-align and campy-live-export
leave them out.

campy-session post's join step (campy/post.py) joins the files into one
plain MP4, <stem>.joined.mp4 (JoinedName), without re-encoding. Tools that
open a camera's video use it (VideoFor): its frames are frametimes.npy's
rows, one for one.

A camera that did not close its recording (Closed: no totals in its
metadata.csv) can have rows for frames its video never got, past its end
and, where an encoder's B-frame was not yet written, just before it: the
tools read which frames its files hold (HeldRows) and leave the other rows
out, as campy does itself when a recording ends.
"""

import bisect, csv, os, re, subprocess, sys, time

SEGMENTS_FILE = "segments.csv"
COLUMNS = ("file", "first_frame", "last_frame", "frames", "lost", "lost_frames", "ended")


def PartName(video_filename, part):
	# The video file of part 1, 2, 3, ...: 0.mp4, 0.part2.mp4, 0.part3.mp4
	if part <= 1:
		return video_filename
	stem, ext = os.path.splitext(video_filename)
	return "{}.part{}{}".format(stem, part, ext)


def JoinedName(video_filename):
	# The parts joined into one (campy-session post's join): 0.joined.mp4
	stem, ext = os.path.splitext(video_filename)
	return "{}.joined{}".format(stem, ext)


def Part(name):
	# The part number in a part's file name (0.part2.mp4: 2), or None
	m = re.search(r"\.part(\d+)\.[^.]+$", name)
	return int(m.group(1)) if m else None


def Path(folder):
	return os.path.join(folder, SEGMENTS_FILE)


def Ranges(numbers):
	# Sorted frame numbers as "1-3;7;9-10"
	out, numbers = [], sorted(numbers)
	i = 0
	while i < len(numbers):
		j = i
		while j + 1 < len(numbers) and numbers[j + 1] == numbers[j] + 1:
			j += 1
		out.append(str(numbers[i]) if i == j else "{}-{}".format(numbers[i], numbers[j]))
		i = j + 1
	return ";".join(out)


def ParseRanges(text):
	numbers = []
	for item in str(text or "").split(";"):
		item = item.strip()
		if not item:
			continue
		if "-" in item:
			a, b = item.split("-", 1)
			numbers.extend(range(int(a), int(b) + 1))
		else:
			numbers.append(int(item))
	return numbers


def Write(folder, rows, open_file=None):
	"""
	segments.csv, whole, from rows (dicts with COLUMNS): written beside it
	and moved into place, so a reader never finds half of it. open_file:
	newfiles.Open, in a camera process (never over another recording's file).
	"""
	path = Path(folder)
	tmp = path + ".tmp"
	opener = open_file or open
	with opener(tmp, "w", newline="") as f:
		w = csv.writer(f)
		w.writerow(COLUMNS)
		for row in rows:
			w.writerow(["" if row.get(c) is None else row.get(c) for c in COLUMNS])
	os.replace(tmp, path)


def Read(folder):
	"""segments.csv's rows, numbers as int (None where empty); [] when there is none."""
	path = Path(folder)
	if not os.path.isfile(path):
		return []
	rows = []
	with open(path, newline="", encoding="utf-8") as f:
		for row in csv.DictReader(f):
			for c in ("first_frame", "last_frame", "frames", "lost"):
				value = (row.get(c) or "").strip()
				row[c] = int(value) if value else None
			rows.append(row)
	return rows


def Files(folder):
	"""The video files segments.csv lists, in order; [] when there is none."""
	return [row["file"] for row in Read(folder)]


def LostFrames(folder):
	"""The frame numbers segments.csv lists as lost (a set; empty when there is none)."""
	lost = set()
	for row in Read(folder):
		lost.update(ParseRanges(row.get("lost_frames")))
	return lost


def KeepRows(frame_numbers, lost):
	"""
	Which rows of frametimes.npy to keep (a bool array), leaving out the
	frames named lost: for a recording cut short before campy took their
	rows out. All True when nothing is lost, or it was already done.
	"""
	import numpy as np
	frame_numbers = np.asarray(frame_numbers)
	if not lost:
		return np.ones(len(frame_numbers), bool)
	return ~np.isin(frame_numbers.astype(np.int64), np.fromiter(lost, np.int64, len(lost)))


class NeedsJoin(Exception):
	"""A camera's video is in several files, and they have not been joined into one yet."""


def VideoFor(folder, video_filename):
	"""
	The one video file a reader should open for a camera: video_filename,
	or, when ffmpeg was restarted and the video is in parts, the parts
	joined (JoinedName). Raises NeedsJoin when those are not joined yet.
	"""
	files = Files(folder)
	if len(files) <= 1:
		return video_filename
	joined = JoinedName(files[0])
	if os.path.isfile(os.path.join(folder, joined)):
		return joined
	recording = os.path.dirname(os.path.abspath(folder))
	if " " in recording:
		recording = '"{}"'.format(recording)
	if Closed(folder):
		how = "campy-session post {} --only join".format(recording)
	else:
		# campy-session post skips a recording that did not stop cleanly unless told to
		how = "campy-session post {} --only join --force (it did not close its recording, so post's steps " \
			"run only with --force)".format(recording)
	raise NeedsJoin("{}: its video is in {} files ({}), as its ffmpeg was restarted during the recording. Join "
		"them into one first: {}".format(folder, len(files), ", ".join(files), how))


def NotTheVideo(name):
	"""
	Whether a file beside a camera's video is one of campy's own copies or
	files being written, never a video to read: the fragmented original kept
	by campy-session post's remux (remux: keep), a remux or join stopped
	part way, campy-infer's .slp being written.
	"""
	return name.startswith(".") or any(tag in name for tag in (".fragmented.", ".remux-tmp", ".join-tmp"))


def Closed(folder):
	"""
	Whether the camera's process closed its recording: metadata.csv has its
	totals (framesWritten). Then frametimes.npy has one row per frame its
	video holds. When it did not (campy killed, a power cut, the computer
	crashed), frametimes.npy can have rows for frames that never reached the
	video: they are added as each frame is queued for the writer
	(unicam.GrabFrames), a second or more before they are in the file
	(HeldRows).
	"""
	try:
		with open(os.path.join(folder, "metadata.csv"), newline="", encoding="utf-8") as f:
			return any(len(row) >= 2 and row[0] == "framesWritten" for row in csv.reader(f))
	except (OSError, csv.Error):
		return False


def FrameRate(folder):
	"""metadata.csv's frameRate, or None."""
	try:
		with open(os.path.join(folder, "metadata.csv"), newline="", encoding="utf-8") as f:
			for row in csv.reader(f):
				if len(row) >= 2 and row[0] == "frameRate":
					return float(row[1])
	except (OSError, csv.Error, ValueError):
		pass
	return None


class Held:
	"""
	HeldRows' answer: keep, a bool per row of frametimes.npy, True where
	the camera's video holds that frame; files, [(file, frames it holds)] in
	order; beyond, frames the last file holds past the rows (its frame times
	never reached the disk); corrupt, packets in the files that hold no
	whole frame (cut in the middle); counted, the files that were read.
	"""

	def __init__(self, keep, files, beyond, counted, corrupt=0):
		self.keep, self.files, self.beyond, self.counted = keep, files, beyond, counted
		self.corrupt = corrupt

	@property
	def left_out(self):
		return int(len(self.keep) - self.keep.sum())

	def Text(self, name):
		# A sentence for the camera called name, or "" when nothing is left out
		details = self.Details()
		return "{} did not close its recording; {}.".format(name, details) if details else ""

	def Details(self):
		# What is left out, or ""
		parts = []
		if self.left_out:
			parts.append("{} of frametimes.npy's {} rows are frames its video does not hold (still queued, in the "
				"encoder or in a fragment that was never written when the recording was cut short): left out".format(
				self.left_out, len(self.keep)))
		if self.beyond:
			parts.append("its video holds {} frame{} past the last row (their frame times were never written): not "
				"used".format(self.beyond, "" if self.beyond == 1 else "s"))
		return "; ".join(parts)


def HeldRows(folder, frames, video=None, ffmpeg=None):
	"""
	Which of frametimes.npy's rows (frames: their frame numbers) a camera's
	video files hold, for a camera that did not close its recording
	(Closed): each file whose frames were not counted is read by CountFrames
	(packets only, no decoding). video: the file, when the video is in one
	(no segments.csv). A file's frames are the rows of the frames given to
	its ffmpeg, in order: frame numbers first_frame to last_frame (segments.
	csv), to the end for the last. A file counted already (segments.csv
	has its frames) keeps the rows of all but its lost_frames. Raises
	Uncounted when a file could not be read. No frameRate in metadata.csv:
	one frame's time is the first packet's duration.
	"""
	import numpy as np
	frames = np.asarray(frames, np.int64)
	keep = np.zeros(len(frames), bool)
	rows = Read(folder)
	if len(rows) < 2:
		rows = [dict(file=video or (rows[0]["file"] if rows else None), first_frame=None, last_frame=None,
			frames=None, lost_frames="")]
	rate = FrameRate(folder)
	files, beyond, counted, corrupt = [], 0, [], 0
	lost = LostFrames(folder)
	for k, row in enumerate(rows):
		last = k == len(rows) - 1
		inside = np.ones(len(frames), bool)
		if row.get("first_frame") is not None and len(rows) > 1:
			inside &= frames >= row["first_frame"]
		if not last and row.get("last_frame") is not None:
			inside &= frames <= row["last_frame"]
		place = np.nonzero(inside)[0]
		past = []
		if row.get("frames") is not None and not last:
			held = ~np.isin(frames[place], np.fromiter(lost, np.int64, len(lost))) if lost else \
				np.ones(len(place), bool)
		else:
			if not row.get("file"):
				raise Uncounted("{}: which file is its video is not known".format(folder))
			path = os.path.join(folder, row["file"])
			if not os.path.isfile(path):
				raise Uncounted("{} is not there".format(path))
			if os.path.getsize(path) == 0:
				# cut short before ffmpeg wrote anything: no frame in it
				keep[place] = False
				files.append((row["file"], 0))
				counted.append(row["file"])
				continue
			cut = []
			mask = CountFrames(path, rate, len(place), ffmpeg=ffmpeg, beyond=past if last else None, corrupt=cut)
			held = np.frombuffer(bytes(mask), np.uint8).astype(bool)
			beyond += len(past)
			corrupt += len(cut)
			counted.append(row["file"])
		keep[place] = held
		files.append((row["file"], int(held.sum()) + len(past)))
	return Held(keep, files, beyond, counted, corrupt)


def FfmpegExe():
	if os.environ.get("IMAGEIO_FFMPEG_EXE"):
		return os.environ["IMAGEIO_FFMPEG_EXE"]
	import imageio_ffmpeg
	return imageio_ffmpeg.get_ffmpeg_exe()


def LowPriority():
	# For a subprocess that must not take the CPU from the cameras' threads and encoders
	if sys.platform == "win32":
		return {"creationflags": getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)}
	return {"preexec_fn": lambda: os.nice(10)}


class Uncounted(Exception):
	"""The frames in a video file could not be told."""


def CountFrames(path, frame_rate, given, ffmpeg=None, timeout=600, beyond=None, corrupt=None):
	"""
	Which of the frames given to a video's ffmpeg are in the file: a
	bytearray of one byte per frame given, 1 where the file holds it (0 =
	the first frame given). From each packet's timestamp: ffmpeg lists them
	(framecrc) without decoding, with the file's edit list applied, so the
	first frame's is 0 and each frame's is its place times the frame's
	duration, whatever order the encoder put them in (B-frames). Read line
	by line, so that a day of frames takes a few MB. A fragmented MP4 cut
	off by a stopped ffmpeg holds its fragments up to the last one written;
	a plain one cut off holds none (it has no index: "moov atom not
	found"). Raises Uncounted when the file could not be read, or its
	timestamps are not one frame each. frame_rate None: one frame's time is
	the first packet's duration. beyond: a list, to count frames past
	the given ones in instead (one item each: their place), for a recording
	cut short whose video got further than its frame times. corrupt: a
	list, to put the places of packets ffmpeg flags as corrupt in instead
	of counting them as held: the frame a file was cut in the middle of
	("partial file"), which no decoder gives.
	"""
	import tempfile
	held = bytearray(given)
	command = [ffmpeg or FfmpegExe(), "-v", "error", "-nostdin", "-i", path, "-map", "0:v:0", "-c", "copy",
		"-f", "framecrc", "-"]
	deadline = time.monotonic() + timeout
	tb = None
	bad = None
	with tempfile.TemporaryFile() as errors:
		try:
			proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, stdin=subprocess.DEVNULL,
				**LowPriority())
		except Exception as e:
			raise Uncounted("could not run ffmpeg on {}: {}".format(path, e))
		try:
			for line in proc.stdout:
				if time.monotonic() > deadline:
					proc.kill()
					raise Uncounted("ffmpeg took over {:g} s to read {}".format(timeout, path))
				if bad is not None:
					continue # read to the end, so that ffmpeg is not stopped by a closed pipe
				if line.startswith(b"#tb 0:"):
					num, den = line.split(b":", 1)[1].strip().split(b"/")
					tb = (int(num), int(den))
				elif line[:2] == b"0,":
					if tb is None:
						bad = "ffmpeg gave no time base"
						continue
					fields = line.split(b",")
					pts = int(fields[2])
					if frame_rate is None: # not known: the first packet's duration is one frame's
						duration = int(fields[3])
						if duration <= 0:
							bad = "no frame rate, and the first packet has no duration"
							continue
						frame_rate = tb[1] / float(tb[0] * duration)
					place = pts * tb[0] * float(frame_rate) / tb[1] # its place, from its time
					i = int(round(place))
					if abs(place - i) > 0.01:
						bad = "a packet's timestamp ({}) is not a whole frame".format(pts)
					elif corrupt is not None and any(f.strip().startswith(b"F=") and int(f.strip()[2:], 16) & 2
							for f in fields[6:]): # AV_PKT_FLAG_CORRUPT
						corrupt.append(i)
					elif i >= given and beyond is not None:
						beyond.append(i)
					elif not 0 <= i < given:
						bad = "a packet's timestamp ({}, frame {}) is outside the {} frames given".format(pts, i, given)
					elif held[i]:
						bad = "two packets for frame {}".format(i)
					else:
						held[i] = 1
			code = proc.wait(max(1.0, deadline - time.monotonic()))
		finally:
			if proc.poll() is None:
				proc.kill()
				proc.wait()
		errors.seek(0)
		stderr = errors.read()[-2000:].decode("utf-8", "replace")
	if code != 0:
		if "moov atom not found" in stderr:
			return bytearray(given) # a plain MP4 cut off before ffmpeg wrote its index: nothing in it can be read
		raise Uncounted("ffmpeg could not read {}: {}".format(path, stderr.strip()[-300:]))
	if bad is not None:
		raise Uncounted("{}: {}".format(path, bad))
	return held


def Missing(held):
	"""The places of the frames a CountFrames mask lacks (0 bytes), in order."""
	out, i = [], held.find(0)
	while i >= 0:
		out.append(i)
		i = held.find(0, i + 1)
	return out


class FrameRuns:
	"""
	The frame numbers given to one ffmpeg, in order, kept as runs of
	consecutive numbers (only dropped frames break a run), so that an hour's
	frames take a few bytes: Number(place) gives the frame number of the
	frame at that place.
	"""

	def __init__(self):
		self.starts = [] # place of each run's first frame
		self.firsts = [] # and its frame number
		self.count = 0
		self.last = None

	def Add(self, number):
		if self.last is None or number != self.last + 1:
			self.starts.append(self.count)
			self.firsts.append(number)
		self.last = number
		self.count += 1

	def Number(self, place):
		i = bisect.bisect_right(self.starts, place) - 1
		return self.firsts[i] + place - self.starts[i]

	@property
	def first(self):
		return self.firsts[0] if self.firsts else None
