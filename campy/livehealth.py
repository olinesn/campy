"""
campy-live-view's checks of the rig and of the tracking, as campy records:
what campy-live-view (campy/liveview.py) warns of, in red, and draws in its
tracking strip. Like campy/livefeed.py, it needs only numpy.

The rig (Rig)
-------------
Each camera's ring (campy/livefeed.py) holds its newest frames (8: 0.27 s at
30 fps), each with its frame number, when campy got it (host time), the
camera's timestamp, and when it was in shared memory (written time). The view
reads every slot's fields, not the frames (microseconds), about 20 times a
second, keeps the last few seconds of them, and tells for each camera:

  frames/s   over the last second (from the written times: perf_counter,
             precise, one clock in every process)
  behind     below the fastest camera's rate (by 3 % and half a frame a
             second), or no frame for half a second
  lost       frames its camera timestamps skipped: the camera dropped them
             (flirFramesLost in metadata.csv), and numbers its later frames
             one lower than the other cameras'
  gaps       frame numbers that never reached the feed
  late       frames that reached the computer a frame late or more (for the
             camera's timing); without timestamps, a frame late or lost
  offset     how far its host times are from the other cameras' for the
             same frame number (a trimmed mean over the last 2 s, against
             the middle camera's). Half a frame or more: it is a frame ahead
             (earlier) or behind, and every set campy-live makes mixes
             frames of different trigger pulses (it calls them misaligned).
             That happened on 2026-10-20: a camera left grabbing after a
             second Ctrl+C started the next recording one frame ahead of the
             others, 31 ms off.

lost, gaps and late are counted over the last 10 s (the warnings) and since
the view began. How long the recording has been going is from the first
frame's host time, or, when the view came later, from the frame number.

The tracking (Tracking)
-----------------------
For each identity, from campy-live's rows (with --calibration): its
reprojection error (campy-live's reproj_px, mean over the nodes) and how many
cameras it was triangulated from, over the last 10 s. An identity with an
error above --bad-px is drawn dimmed, its label in red. Warnings: fewer
identities than campy-live's --animals with good 3D (2 cameras or more,
within --bad-px, for half of the last second), and one identity's error
(median over the last 2 s) far above the others' (3 times their median, and
above --bad-px): often a wrong match across cameras, or a detection off the
animal (on equipment outside the cage).
"""

import collections, time, types, warnings

import numpy as np

WINDOW_S = 10.0 # the warnings count this many seconds back, and the strip shows them
OFFSET_S = 2.0 # the host-time offsets: of the frames of the last this many seconds
KEEP = 512 # each camera's newest frames kept (17 s at 30 fps)
GOOD_PX, BAD_PX = 10.0, 20.0 # a reprojection error within GOOD_PX is good; above BAD_PX (--bad-px) bad


def Duration(seconds):
	s = int(max(0, seconds))
	return "{}:{:02d}:{:02d}".format(s // 3600, s // 60 % 60, s % 60)


def Plural(n, word):
	return "{} {}{}".format(n, word, "" if n == 1 else "s")


class Feed:
	# One camera's frames seen in its ring: number -> (host ns, camera timestamp, written ns)
	def __init__(self, name):
		self.name = name
		self.frames = {}
		self.order = collections.deque() # their numbers, as they came
		self.checked = None # every number up to this one was looked for (gaps)
		self.unchecked = 0 # numbers gone from the ring before the view looked (it was busy)
		self.events = collections.deque() # (written ns, kind, count): lost, gaps, late
		self.totals = {"lost": 0, "gaps": 0, "late": 0}
		self.first = None # (frame number, host ns) of the first frame seen
		self.stampSteps = collections.deque(maxlen=120) # camera timestamps, from one frame to the next
		self.writtenSteps = collections.deque(maxlen=120) # written times, ns

	def Event(self, written, kind, count):
		self.events.append((written, kind, count))
		self.totals[kind] += count

	def Recent(self, kind, since):
		return sum(n for t, k, n in self.events if k == kind and t > since)


class Rig:
	"""
	Each camera's feed, from its ring's slots: Sample reads them (every
	camera's RingReader), Add takes one camera's (for tests: made-up ones),
	Status says how each camera is doing (Status.cameras[c].problems: what to
	warn of, (short tag for its tile, sentence for the panel)).
	"""

	def __init__(self, names, frameRate=0.0, slots=8):
		self.feeds = [Feed(n) for n in names]
		self.frameRate = float(frameRate or 0.0)
		self.slots = int(slots)
		self.samples = 0
		self.sampledAt = None # time.perf_counter_ns() of the last Sample
		self.cache = (None, None)

	def Sample(self, readers):
		for c, reader in enumerate(readers):
			latest = reader.Latest()
			self.Add(c, *reader.Slots(), latest=latest)
		self.sampledAt = time.perf_counter_ns() # the frames' rates are as of then
		self.samples += 1

	def Period(self):
		# Seconds from one frame to the next: the median of the written times' steps, or frameRate's
		steps = [s for f in self.feeds for s in f.writtenSteps]
		if len(steps) >= 5:
			return float(np.median(steps)) / 1e9
		return 1.0 / self.frameRate if self.frameRate > 0 else 1.0 / 30

	def StampStep(self, feed):
		# This camera's usual timestamp step (in its own units), or None
		return float(np.median(feed.stampSteps)) if len(feed.stampSteps) >= 5 else None

	def Every(self):
		# How often to read the rings' slots: often enough to see every frame (a frame stays in its ring for
		# slots frames), at most 20 times a second
		rate = 1.0 / self.Period()
		return min(0.05, 0.4 * self.slots / rate)

	def Add(self, c, frames, hosts, stamps, written, latest=None):
		"""Camera c's frames as its ring holds them (numbers, oldest first), and its newest frame number."""
		feed = self.feeds[c]
		period = self.Period()
		usual = self.StampStep(feed)
		for n, h, s, w in zip(np.asarray(frames).tolist(), np.asarray(hosts).tolist(), np.asarray(stamps).tolist(),
				np.asarray(written).tolist()):
			if n in feed.frames:
				continue
			if feed.first is None:
				feed.first = (n, h)
			before = feed.frames.get(n - 1)
			feed.frames[n] = (h, s, w)
			feed.order.append(n)
			if before is None:
				continue
			dw = w - before[2]
			ds = s - before[1]
			if dw > 0:
				feed.writtenSteps.append(dw)
			if np.isfinite(ds) and ds > 0 and usual:
				pulses = ds / usual
				if pulses > 1.5: # the camera skipped frames
					feed.Event(w, "lost", int(round(pulses)) - 1)
				elif dw / 1e9 - pulses * period > period: # here a frame or more after its time
					feed.Event(w, "late", 1)
				if pulses < 1.5:
					feed.stampSteps.append(ds)
			elif not np.isfinite(ds) and dw / 1e9 > 1.5 * period: # no timestamps: late or lost
				feed.Event(w, "late", 1)
			elif np.isfinite(ds) and ds > 0 and not usual:
				feed.stampSteps.append(ds)
		while len(feed.order) > KEEP:
			del feed.frames[feed.order.popleft()]
		if latest is not None and latest > 0:
			# Numbers the ring should hold whole that it never did: gaps. The slot of latest + 1 (latest -
			# slots + 1's) may be being written: the newest slots - 1
			if feed.checked is not None and latest > feed.checked:
				low = max(feed.checked + 1, latest - self.slots + 2)
				feed.unchecked += max(0, low - feed.checked - 1)
				missing = sum(1 for n in range(low, latest + 1) if n not in feed.frames)
				if missing:
					newest = feed.frames[feed.order[-1]][2] if feed.order else 0
					feed.Event(newest, "gaps", missing)
			if feed.checked is None or latest > feed.checked:
				feed.checked = latest

	def Offsets(self, now):
		# Each camera's host times against the middle camera's, ms, for the same frame numbers; None: too few
		feeds = self.feeds
		if len(feeds) < 2 or not feeds[0].order:
			return [None] * len(feeds)
		since = now - OFFSET_S * 1e9
		common = []
		for n in reversed(feeds[0].order):
			if feeds[0].frames[n][2] < since or len(common) >= 120:
				break
			if all(n in f.frames for f in feeds[1:]):
				common.append(n)
		if len(common) < 5:
			return [None] * len(feeds)
		H = np.array([[f.frames[n][0] for n in common] for f in feeds], np.float64)
		middle = np.sort(H, 0)[(len(feeds) - 1) // 2] # the middle camera's (with 2: the earlier one's)
		d = np.sort((H - middle) / 1e6, 1)
		cut = len(common) // 10
		return [float(v) for v in d[:, cut:len(common) - cut].mean(1)]

	def Rate(self, feed, now):
		# Frames a second over the last second, and seconds since its newest frame (None: none yet)
		if not feed.order:
			return None, None
		newest = feed.frames[feed.order[-1]][2]
		first = None
		for n in reversed(feed.order):
			if feed.frames[n][2] <= now - 1e9:
				break
			first = n
		since = (now - newest) / 1e9
		if first is None or first == feed.order[-1]:
			return (0.0 if since > 1 else None), since
		last = feed.order[-1]
		w0 = feed.frames[first][2]
		span = max(newest - w0, now - w0 - self.Period() * 1e9)
		return (last - first) * 1e9 / max(span, 1.0), since

	def Status(self, now=None):
		"""
		How each camera is doing, as of now (time.perf_counter_ns; default:
		the last Sample): .cameras, one
		each (.name, .fps, .lost, .gaps, .late (the last 10 s), .totals,
		.offset_ms, .frames_off, .problems: [(tag, sentence)]), .recording_s
		(how long the recording has been going, or None), .period (s).
		"""
		if now is None:
			now = self.sampledAt if self.sampledAt is not None else time.perf_counter_ns()
		key = (self.samples, now)
		if self.cache[0] == key:
			return self.cache[1]
		period = self.Period()
		since = now - WINDOW_S * 1e9
		rates = [self.Rate(f, now) for f in self.feeds]
		known = [r for r, _ in rates if r is not None]
		fastest = max(known) if known else None
		offsets = self.Offsets(now)
		cameras = []
		for feed, (fps, quiet), offset in zip(self.feeds, rates, offsets):
			while feed.events and feed.events[0][0] < since:
				feed.events.popleft()
			s = types.SimpleNamespace(name=feed.name, fps=fps, quiet_s=quiet, offset_ms=offset, frames_off=0,
				lost=feed.Recent("lost", since), gaps=feed.Recent("gaps", since), late=feed.Recent("late", since),
				totals=dict(feed.totals), problems=[])
			if quiet is not None and quiet > max(0.5, 5 * period):
				s.problems.append(("NO FRAMES", "{}: no frame for {:.1f} s".format(feed.name, quiet)))
			elif fps is not None and fastest and fps < 0.97 * fastest and fastest - fps > 0.5:
				s.problems.append(("{:.1f} FPS".format(fps), "{}: {:.1f} frames/s, behind the fastest camera's {:.1f}"
					.format(feed.name, fps, fastest)))
			if offset is not None and abs(offset) >= 0.5 * period * 1e3:
				k = int(round(abs(offset) / (period * 1e3))) or 1
				s.frames_off = k if offset < 0 else -k
				way = "ahead" if offset < 0 else "behind"
				s.problems.append(("{} FRAME{} {}".format(k, "S" if k > 1 else "", way.upper()),
					"{}: {} {} of the other cameras ({:+.0f} ms for the same frame number): campy-live's sets mix "
					"frames of different pulses. Stop the recording and start it again.".format(feed.name,
					Plural(k, "frame"), way, offset)))
			if s.lost:
				s.problems.append(("LOST {}".format(s.lost), "{}: the camera lost {} in the last {:.0f} s (its "
					"timestamps skipped them)".format(feed.name, Plural(s.lost, "frame"), WINDOW_S)))
			if s.gaps:
				s.problems.append(("GAP {}".format(s.gaps), "{}: {} missing from its feed in the last {:.0f} s"
					.format(feed.name, Plural(s.gaps, "frame number"), WINDOW_S)))
			if s.late:
				s.problems.append(("LATE {}".format(s.late), "{}: {} here a frame late or more in the last {:.0f} s"
					.format(feed.name, Plural(s.late, "frame"), WINDOW_S)))
			cameras.append(s)
		recording = None
		for feed in self.feeds:
			if feed.first is not None and feed.order:
				n0, h0 = feed.first
				t = (feed.frames[feed.order[-1]][0] - h0) / 1e9 + (n0 - 1) * period
				recording = t if recording is None else max(recording, t)
		status = types.SimpleNamespace(cameras=cameras, recording_s=recording, period=period, fastest=fastest)
		self.cache = (key, status)
		return status


def Errors(row, n_views=2):
	"""Each identity's reprojection error in a row (mean over its nodes, px), NaN where it has no 3D."""
	err = np.asarray(row["reproj_px"], float)
	views = np.asarray(row["n_views"])
	with warnings.catch_warnings():
		warnings.simplefilter("ignore", RuntimeWarning) # an identity with no node: NaN
		e = np.nanmean(np.where(np.isfinite(err), err, np.nan), -1)
	e[views < n_views] = np.nan
	return e


class Tracking:
	"""
	Each identity's 3D over the last WINDOW_S, from the rows the view takes
	(Add; kept in numpy arrays, about 450 rows at 30 fps): .Arrays() for the
	strip, .Alerts() for the warnings.
	"""

	def __init__(self, animals, frameRate=30.0, bad_px=BAD_PX):
		self.animals = int(animals or 0)
		self.frameRate = float(frameRate or 30.0)
		self.bad = float(bad_px) if bad_px else np.inf # 0: none bad
		self.size = int(WINDOW_S * self.frameRate * 1.5) + 16 # rows kept: more than WINDOW_S's
		self.frames = self.err = self.views = None # the rows', in a circle: row k in k % size
		self.taken = 0 # rows added
		self.since = 0 # the first of them still in WINDOW_S
		self.said = (None, []) # (rows added, the warnings then)

	def Add(self, row):
		if "points3d" not in row.dtype.names:
			return
		frame = int(row["frame"])
		if self.frames is None:
			a = len(row["n_views"])
			self.frames = np.zeros(self.size, np.int64)
			self.err = np.full((self.size, a), np.nan)
			self.views = np.zeros((self.size, a), np.int64)
		elif self.taken > self.since and frame <= self.frames[(self.taken - 1) % self.size] - self.frameRate:
			self.since = self.taken # frames from 1 again: another recording
		k = self.taken % self.size
		self.frames[k], self.err[k], self.views[k] = frame, Errors(row), row["n_views"]
		self.taken += 1
		self.since = max(self.since, self.taken - self.size)
		while self.frames[self.since % self.size] < frame - WINDOW_S * self.frameRate:
			self.since += 1

	def Arrays(self):
		# frames (k,), errors (k, animals), views (k, animals) of the last WINDOW_S, oldest first; None: none
		if self.taken <= self.since:
			return None
		order = np.arange(self.since, self.taken) % self.size
		return self.frames[order], self.err[order], self.views[order]

	def Last(self, seconds):
		got = self.Arrays()
		if got is None:
			return None
		frames, err, views = got
		keep = frames > frames[-1] - seconds * self.frameRate
		return frames[keep], err[keep], views[keep]

	def Alerts(self):
		"""What to warn of: sentences (worked out again once a row has come)."""
		if self.said[0] != self.taken:
			self.said = (self.taken, self.Warnings())
		return self.said[1]

	def Warnings(self):
		alerts = []
		got = self.Last(1.0)
		if got is None:
			return alerts
		_, err, views = got
		with np.errstate(invalid="ignore"):
			good = ((views >= 2) & (err <= self.bad)).mean(0) >= 0.5
		wanted = self.animals or err.shape[1]
		if good.sum() < wanted:
			why = []
			for a in np.nonzero(~good)[0]:
				found = np.isfinite(err[:, a]).mean()
				if found < 0.5:
					why.append("#{} not found in 3D".format(a))
				else:
					why.append("#{} error {:.0f} px".format(a, np.nanmedian(err[:, a])))
			alerts.append("Good 3D for {} of {} animals: {}".format(int(good.sum()), wanted, ", ".join(why)))
		_, err, _ = self.Last(2.0)
		with warnings.catch_warnings():
			warnings.simplefilter("ignore", RuntimeWarning)
			med = np.nanmedian(err, 0)
		have = np.nonzero(np.isfinite(med))[0]
		if len(have) >= 2:
			for a in have:
				others = float(np.median(med[[b for b in have if b != a]]))
				if med[a] > self.bad and med[a] > 3 * max(others, 1.0):
					alerts.append("#{}'s error, {:.0f} px, is far above the others' ({:.0f} px): a wrong match across "
						"cameras, or a detection off the animal".format(a, med[a], others))
		return alerts

	def Summary(self):
		got = self.Arrays()
		if got is None:
			return {}
		_, err, views = got
		with warnings.catch_warnings():
			warnings.simplefilter("ignore", RuntimeWarning)
			return {"identities": [{"found": float(np.isfinite(err[:, a]).mean()),
				"error_px_p50": float(np.nanmedian(err[:, a])) if np.isfinite(err[:, a]).any() else None,
				"bad": float((err[:, a] > self.bad).mean())} for a in range(err.shape[1])]}
