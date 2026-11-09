"""
What went wrong in a recording that has ended, in a few lines, for
campy-session panel's "After the recording" card (campy/panel.py). Built
only from what campy already writes into the recording's folder, so it is
the same for a page opened again, or a panel started again, later:

  - each camera's metadata.csv: its totals as it closed (totalFrames, the
    frames grabbed; framesWritten, the video's; framesDropped,
    framesLostInEncoder, flirFramesLost, ...) and its errors (cameraError,
    videoError, frameTimesError);
  - logs/campy-acquire.log's end line for a camera whose metadata.csv has no
    totals ("Camera2 grabbed 3285 frames at 60 fps; its video holds 2784
    (501 dropped).");
  - alerts.log and alerts-live.log (JSON lines: notify.Notifier.Log): each
    critical alert raised while it recorded, open at the end or resolved,
    and the warnings still open at the end.

Summary(folder) gives {"level": ok, warn or bad, "lines": [{"level", "text"}],
"cameras": how many}. A recording without a problem is one line: "All 8
cameras: every frame saved (3,285 frames each)."
"""

import collections, os, re

from campy import post, strictjson

ALERT_LOGS = ("alerts.log", "alerts-live.log")
ACQUIRE_LOG = os.path.join("logs", "campy-acquire.log")
TEXT_MAX = 240 # an alert's first line, at most, on the page

# campy-acquire's end line for each camera (unicam.EndLine)
GRABBED = re.compile(r"(\S+) grabbed ([\d,]+) frames at [^;]*; (?:its video holds|its \d+ video files hold) "
	r"(all of them|[\d,]+)(?: \(([^)]*)\))?")
SAVED = re.compile(r"(\S+) saved ([\d,]+) frames at ")
ORDER = {"bad": 0, "warn": 1, "ok": 2}


def Int(value):
	try:
		return int(float(str(value).replace(",", "")))
	except (TypeError, ValueError):
		return None


def Thousands(n):
	return "{:,}".format(n)


def Plural(n, one, many=None):
	return "{} {}".format(n, one if n == 1 else many or one + "s")


# ------------------------------------------------------------------ the cameras

def LogTotals(folder):
	"""{camera: {grabbed, held, dropped, lost}} from campy-acquire.log's end lines (the last line for each)."""
	out = {}
	try:
		f = open(os.path.join(folder, ACQUIRE_LOG), encoding="utf-8", errors="replace")
	except OSError:
		return out
	with f:
		for line in f:
			if " frames at " not in line:
				continue
			m = GRABBED.search(line)
			if m:
				grabbed, why = Int(m.group(2)), m.group(4) or ""
				dropped, lost = re.search(r"([\d,]+) dropped", why), re.search(r"([\d,]+) lost in", why)
				out[m.group(1)] = {"grabbed": grabbed, "held": grabbed if m.group(3) == "all of them" else Int(m.group(3)),
					"dropped": Int(dropped.group(1)) if dropped else 0, "lost": Int(lost.group(1)) if lost else 0}
				continue
			m = SAVED.search(line)
			if m:
				grabbed = Int(m.group(2))
				out[m.group(1)] = {"grabbed": grabbed, "held": grabbed, "dropped": 0, "lost": 0}
	return out


def Cameras(folder):
	"""
	Each camera's end, in the order of its folder's name: {name, closed (its
	totals found), grabbed, held, dropped, lost (in stopped ffmpegs),
	camera_lost (before reaching campy), incomplete, errors, queue_s
	(writeQueueMaxSec)}.
	"""
	cams, log = [], None
	for name, meta in post.Cameras(folder).items():
		v = post.Metadata(meta)
		cam = {"name": name, "closed": "framesWritten" in v, "grabbed": Int(v.get("totalFrames")),
			"held": Int(v.get("framesWritten")), "dropped": Int(v.get("framesDropped")) or 0,
			"lost": Int(v.get("framesLostInEncoder")) or 0,
			"camera_lost": (Int(v.get("flirFramesLost")) or 0) + (Int(v.get("ratcamFramesLost")) or 0),
			"incomplete": Int(v.get("flirFramesIncomplete")) or 0,
			"errors": [v[k] for k in ("cameraError", "videoError", "frameTimesError") if v.get(k)],
			"queue_s": v.get("writeQueueMaxSec")}
		if not cam["closed"]:
			# No totals in metadata.csv (it could not be written, or the camera never closed): its end line, if it said one
			if log is None:
				log = LogTotals(folder)
			said = log.get(name) or log.get(v.get("cameraName"))
			if said:
				cam.update(said, closed=True)
		cams.append(cam)
	return cams


def Seconds(values):
	# "5 s" when every camera has the same writeQueueMaxSec, else None
	seconds = set()
	for value in values:
		try:
			seconds.add("{:g} s".format(float(value)))
		except (TypeError, ValueError):
			return None
	return seconds.pop() if len(seconds) == 1 else None


def CameraLines(cams):
	n, lines = len(cams), []
	closed = [c for c in cams if c["closed"]]

	def listing(which, key, of=True):
		return ", ".join("{} {}".format(c["name"], Thousands(c[key]) + (" of " + Thousands(c["grabbed"])
			if of and c["grabbed"] is not None else "")) for c in which)

	dropped = [c for c in closed if c["dropped"]]
	if dropped:
		behind = Seconds(c["queue_s"] for c in dropped)
		lines.append(("bad", "{} of {} dropped frames: {} (the encoder fell more than {} behind: writeQueueMaxSec)"
			.format(len(dropped), Plural(n, "camera"), listing(dropped, "dropped"),
			behind or "its writeQueueMaxSec")))
	lost = [c for c in closed if c["lost"]]
	if lost:
		lines.append(("bad", "{} of {} lost frames in a stopped ffmpeg: {}".format(len(lost), Plural(n, "camera"),
			listing(lost, "lost"))))
	early = [c for c in closed if c["camera_lost"]]
	if early:
		lines.append(("bad", "{} of {} lost frames before they reached campy (flirFramesLost): {}".format(len(early),
			Plural(n, "camera"), listing(early, "camera_lost", of=False))))
	incomplete = [c for c in closed if c["incomplete"]]
	if incomplete:
		lines.append(("warn", "{} of {} had frames that arrived incomplete, left out: {}".format(len(incomplete),
			Plural(n, "camera"), listing(incomplete, "incomplete", of=False))))
	for c in cams:
		for error in c["errors"]:
			lines.append(("bad", "{}: {}".format(c["name"], error)))
		if not c["closed"]:
			lines.append(("bad", "{} stopped without closing (no totals in its metadata.csv)".format(c["name"])))
		elif not c["held"] and not c["errors"]:
			lines.append(("bad", "{} saved no frames".format(c["name"])))
	if not lines:
		counts = sorted({c["held"] for c in closed if c["held"] is not None})
		frames = "" if not counts else " ({} frames{})".format(Thousands(counts[0]), " each" if n > 1 else "") \
			if len(counts) == 1 else " ({} to {} frames)".format(Thousands(counts[0]), Thousands(counts[-1]))
		lines.append(("ok", "{}: every frame saved{}".format("All {} cameras".format(n) if n > 1 else cams[0]["name"],
			frames)))
	return lines


# ------------------------------------------------------------------ the alerts

def Clock(stamp):
	# An alert's ISO time as the page shows it: 14:34:53
	return str(stamp or "")[11:19]


def First(text):
	# An alert's first line, without its "worse: " or "still: " (the full text stays in alerts.log)
	line = re.sub(r"^(worse|still|flapping|coming and going): ", "", str(text or "").strip().split("\n")[0])
	return line if len(line) <= TEXT_MAX else line[:TEXT_MAX - 1].rstrip() + "…"


def AlertHistory(folder):
	"""
	Each alert's key: {critical (ever), text and at (its last critical event's, else its last open one's),
	resolved, state}. An alert's text is as it was then: its counts are not the recording's totals.
	"""
	alerts = collections.OrderedDict()
	for name in ALERT_LOGS:
		try:
			f = open(os.path.join(folder, name), encoding="utf-8", errors="replace")
		except OSError:
			continue
		with f:
			for line in f:
				try:
					event = strictjson.loads(line)
				except ValueError:
					continue
				key, state = event.get("key"), event.get("state")
				if not key or state not in ("open", "resolved"):
					continue # a note, a ping, a send that failed: not an alert's state
				a = alerts.setdefault((name, key), {"critical": False, "text": None, "at": None, "resolved": None,
					"state": None})
				if state == "open":
					if a["state"] != "open":
						a["resolved"] = None # open again
					critical = event.get("severity") == "critical"
					if critical or not a["critical"]:
						a["text"], a["at"] = event.get("text"), event.get("time")
					a["critical"] = a["critical"] or critical
				else:
					a["resolved"] = event.get("time")
				a["state"] = state
	return list(alerts.values())


def AlertLines(folder):
	lines = []
	for a in AlertHistory(folder):
		if a["critical"]:
			when = "still open at the end" if a["state"] == "open" else "resolved at {}".format(Clock(a["resolved"]))
			lines.append(("bad", "Critical at {}, {}: {}".format(Clock(a["at"]), when, First(a["text"]))))
		elif a["state"] == "open":
			lines.append(("warn", "Warning at {}, still open at the end: {}".format(Clock(a["at"]), First(a["text"]))))
	return lines


# ------------------------------------------------------------------ the whole

def Summary(folder):
	"""The recording's summary (the module's doc), or None without a folder."""
	if not folder or not os.path.isdir(folder):
		return None
	cams = Cameras(folder)
	lines = CameraLines(cams) if cams else [("bad", "No camera saved anything: no camera folder with a metadata.csv")]
	lines += AlertLines(folder)
	level = min((level for level, _ in lines), key=ORDER.get)
	return {"level": level, "lines": [{"level": level_, "text": text} for level_, text in lines], "cameras": len(cams)}


def Signature(folder):
	# What Summary reads, by size and time: a summary is made again only when one of them changed
	paths = [os.path.join(folder, name) for name in ALERT_LOGS + (ACQUIRE_LOG,)]
	paths += list(post.Cameras(folder).values())
	sig = [folder]
	for path in paths:
		try:
			st = os.stat(path)
			sig.append((path, st.st_size, st.st_mtime_ns))
		except OSError:
			sig.append((path, None))
	return tuple(sig)


class Cache:
	"""The last folder's summary, made again only when its files change (the page asks every second)."""

	def __init__(self):
		self.sig, self.summary = None, None

	def Get(self, folder):
		if not folder or not os.path.isdir(folder):
			return None
		try:
			sig = Signature(folder)
			if sig != self.sig:
				self.summary, self.sig = Summary(folder), sig
		except Exception as e: # never the page's status for it
			self.summary, self.sig = {"level": "warn", "lines": [{"level": "warn", "text":
				"Could not read what went wrong in this recording: {}".format(e)}], "cameras": 0}, None
		return self.summary
