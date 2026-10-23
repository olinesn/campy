"""
campy-watch: the live tracking's watch, beside campy-live, sent as alerts
(campy/notify.py: Slack direct messages).

campy-acquire's own watch (campy/watch.py) sees each camera's frames and its
writing. What it can't see is what campy-live-view shows in red: cameras
whose frames of the same number came at different times (a camera a frame
ahead or behind: every set campy-live makes then mixes frames of different
trigger pulses), frames a camera lost before campy got them, a camera
slower than the others, and the tracking itself. campy-watch reads what
campy-live-view reads, read-only, with campy-live-view's own checks
(campy/livehealth.py), and sends them when they last:

  misaligned       a camera a frame or more ahead or behind the others for
                   OFFSET_HOLD_S (10 s): critical (stop and start again)
  lost, gaps, late its frames lost (its timestamps skipped them), missing
                   from its feed, or here late, in each of the last
                   COUNTS_HOLD_S (30 s): a warning
  slow             below the fastest camera's frame rate for COUNTS_HOLD_S
  tracking         good 3D for fewer animals than campy-live's --animals, for
                   COLLAPSE_SHARE (70 %) of the last COLLAPSE_S (3 min): a
                   warning
  campy-live       no new row for STALL_S (30 s) while the cameras record:
                   critical (it stopped or hangs); its results ended while
                   they record: a warning

NO FRAMES is left to campy-acquire's watch, which says it already. What it
finds goes into alerts-live.log in the recording's folder (the cameras'
feeds say which), one JSON object a line as alerts.log (notify.Notifier.Log),
whether or not the alerts file names a channel: campy-acquire writes
alerts.log, and each file has one writer.

It needs only numpy and the standard library, as campy-live-view's checks
do: run it in campy-live's environment, or campy's. It waits for the feed
(liveFeed: True), and ends when the recording does (or at Ctrl-C).
campy-session starts it beside campy-live, with --stop-file (a Ctrl-C for a
program without a console) and --launcher-pid (it stops waiting for the
feed if campy-session ended before the recording began; once it watches a
recording, it watches on to its end).

    campy-watch --name campy-live --cameras 3
"""

import argparse, collections, json, os, sys, time

from campy import children, livefeed, livehealth, liveresults, notify, stopfile

OFFSET_HOLD_S = 10.0
COUNTS_HOLD_S = 30.0
COLLAPSE_S, COLLAPSE_SHARE = 180.0, 0.7
STALL_S = 30.0
MOVING_S = 3.0 # the cameras record: a camera's newest frame changed this recently
ENDED_HOLD_S = 5.0
CHECK_S = 1.0 # how often problems are checked (the rings' slots are read more often: Rig.Every)
ATTACH_S = 1.0


def Kind(tag):
	# A livehealth problem's tag, as a key: "offset", "slow", "lost", "gaps", "late", "noframes"
	if tag == "NO FRAMES":
		return "noframes"
	if tag.endswith("FPS"):
		return "slow"
	if "AHEAD" in tag or "BEHIND" in tag:
		return "offset"
	return {"LOST": "lost", "GAP": "gaps", "LATE": "late"}.get(tag.split()[0], tag.lower())


class LiveWatch:
	"""
	The checks, over a livehealth.Rig and a livehealth.Tracking that the caller
	feeds (Run does: from the shared memory). Check(now) sends what has lasted.
	"""

	def __init__(self, notifier, clock=time.monotonic, say=print):
		self.notifier, self.clock, self.say = notifier, clock, say
		self.rig = None
		self.tracking = None
		self.since = {} # key: when its problem began (as livehealth says it), while it does
		self.raised = set()
		self.collapse = collections.deque() # (t, fewer animals than wanted)
		self.rows = None # (t, rows taken) of the last new row
		self.frames = None # (t, the newest frame number of any camera) as it last changed
		self.resultsEnded = False

	def Problem(self, key, severity, text, now, hold):
		began = self.since.setdefault(key, now)
		if now - began >= hold:
			if key not in self.raised:
				self.say("Alert: {}".format(text))
			self.raised.add(key)
			self.notifier.Problem(key, text, severity)

	def Gone(self, key, text=None):
		self.since.pop(key, None)
		if key in self.raised:
			self.raised.discard(key)
			self.say("Alert resolved: {}".format(text or key))
			self.notifier.Resolved(key, text)

	def Check(self, now=None):
		now = self.clock() if now is None else now
		seen = set()
		if self.rig is not None:
			for camera in self.rig.Status().cameras:
				for tag, sentence in camera.problems:
					kind = Kind(tag)
					if kind == "noframes":
						continue # campy-acquire's watch says it
					key = "live/{}/{}".format(camera.name, kind)
					seen.add(key)
					if kind == "offset":
						self.Problem(key, notify.CRITICAL, sentence, now, OFFSET_HOLD_S)
					else:
						self.Problem(key, notify.WARNING, sentence, now, COUNTS_HOLD_S)
		for key in list(self.since):
			if key.startswith("live/") and key not in seen and not key.startswith("live/tracking") and \
					not key.startswith("live/campy-live"):
				camera, kind = key.split("/")[1:3]
				self.Gone(key, {"offset": "{}: in step with the other cameras again", "slow": "{}: up to speed again",
					"lost": "{}: no frame lost for 10 s", "gaps": "{}: no gap for 10 s", "late": "{}: no late frame for "
					"10 s"}.get(kind, "{}: " + kind + " over").format(camera))
		self.CheckTracking(now)
		self.CheckResults(now)
		self.notifier.Tick()

	def CheckTracking(self, now):
		if self.tracking is None:
			return
		warnings = [w for w in self.tracking.Alerts() if w.startswith("Good 3D for")]
		self.collapse.append((now, bool(warnings)))
		while self.collapse and self.collapse[0][0] < now - COLLAPSE_S:
			self.collapse.popleft()
		span = now - self.collapse[0][0]
		share = sum(1 for _, bad in self.collapse if bad) / len(self.collapse)
		key = "live/tracking"
		if span >= COLLAPSE_S - 2 * CHECK_S and share >= COLLAPSE_SHARE:
			self.since.setdefault(key, now)
			if key not in self.raised:
				self.say("Alert: tracking: good 3D for too few animals")
			self.raised.add(key)
			self.notifier.Problem(key, "Tracking: good 3D for too few animals {:.0%} of the last {}. Now: {}".format(
				share, notify.Duration(span), warnings[0] if warnings else "good"), notify.WARNING)
		elif share < COLLAPSE_SHARE / 2:
			self.Gone(key, "Tracking: good 3D for every animal again ({:.0%} of the last {} short)".format(share,
				notify.Duration(span)))

	def CheckResults(self, now):
		# campy-live's rows (Rows) against the cameras' frames (Frames)
		key = "live/campy-live"
		if self.rows is None or self.frames is None:
			return
		cameras_moving = now - self.frames[0] < MOVING_S
		if self.resultsEnded and cameras_moving:
			self.Problem(key + "/ended", notify.WARNING, "campy-live has ended, while the cameras still record", now,
				ENDED_HOLD_S)
		elif now - self.rows[0] >= STALL_S and cameras_moving:
			self.Problem(key, notify.CRITICAL, "campy-live has made no result for {}, while the cameras record: it has "
				"stopped or hangs".format(notify.Duration(now - self.rows[0])), now, 0)
		else:
			self.Gone(key, "campy-live: results again")
			if not cameras_moving:
				self.Gone(key + "/ended")

	def Rows(self, taken, now):
		if self.rows is None or taken != self.rows[1]:
			self.rows = (now, taken)

	def Frames(self, newest, now):
		if self.frames is None or newest != self.frames[1]:
			self.frames = (now, newest)


def Run(args, notifier=None, say=print):
	notifier = notifier or notify.Notifier(source=args.source, say=say)
	if not notifier.channels:
		say("campy-watch: no Slack channel in the alerts file ({}): alerts are said here only.".format(
			notify.AlertsFile()))
	w = LiveWatch(notifier, say=say)
	names = args.rings.split(",") if args.rings else [livefeed.RingName(args.name, n) for n in range(args.cameras)]
	readers, results, after = None, None, 0
	said = False
	checked = sampled = attached = looked = 0.0
	stopping = []
	stopWatch = stopfile.Watch(os.path.abspath(args.stop_file), stopping.append).Start() \
		if getattr(args, "stop_file", None) else None
	launcher = getattr(args, "launcher_pid", 0)
	try:
		while True:
			now = time.monotonic()
			if stopping:
				say("campy-watch: stopped (its stop file).")
				break
			if readers is None and launcher and not children.Alive(launcher):
				say("campy-watch: no live feed, and the program that started it (pid {}) has ended: stopped.".format(
					launcher))
				break
			if readers is None and now - attached >= ATTACH_S:
				attached = now
				readers = Attach(names)
				if readers is not None:
					w.rig = livehealth.Rig([r.cameraName or r.name for r in readers], readers[0].Info("frameRate"),
						readers[0].slots)
					folder = RecordingFolder(readers)
					if folder:
						notifier.SetLog(os.path.join(folder, "alerts-live.log"))
					say("campy-watch: watching {}.".format(", ".join(r.cameraName or r.name for r in readers)))
				elif not said:
					said = True
					say("campy-watch: waiting for the live feed {} (liveFeed: True in campy-acquire's config)...".format(
						", ".join(names)))
			if readers is not None and any(r.Ended() or r.Replaced() for r in readers):
				say("campy-watch: the recording ended.")
				break
			if results is None and readers is not None and now - looked >= ATTACH_S:
				looked = now
				try:
					results = liveresults.Results(liveresults.RingName(args.name))
				except (OSError, ValueError):
					results = None
				else:
					d = results.description
					w.tracking = livehealth.Tracking(d.get("animals"), d.get("frame_rate"), args.bad_px) \
						if d.get("animals") else None
					w.resultsEnded, after = False, 0
					w.Rows(0, now)
			if readers is not None and now - sampled >= w.rig.Every():
				sampled = now
				w.rig.Sample(readers)
				w.Frames(max(r.Latest() for r in readers), now)
			if results is not None:
				if results.Ended():
					w.resultsEnded = True
					results.Close()
					results = None
				else:
					latest = results.Latest()
					for n in range(max(after + 1, latest - results.slots + 1), latest + 1):
						row = results.Read(n)
						if row is not None and w.tracking is not None:
							w.tracking.Add(row)
					after = latest
					w.Rows(latest, now)
			if now - checked >= CHECK_S:
				checked = now
				w.Check(now)
			time.sleep(0.01 if readers is not None else 0.2)
	except KeyboardInterrupt:
		say("campy-watch: stopped (Ctrl-C).")
	finally:
		if stopWatch is not None:
			stopWatch.Stop()
		for r in readers or ():
			r.Close()
		if results is not None:
			results.Close()
		notifier.Flush(15)
	return 0


def RecordingFolder(readers):
	# The folder the cameras' folders are in, as their feeds say (livefeed.Feed's description), or None
	folders = set()
	for reader in readers:
		try:
			folder = json.loads(reader.Description() or "{}").get("folder")
		except (ValueError, AttributeError):
			folder = None
		if not folder:
			return None
		folders.add(os.path.dirname(os.path.normpath(folder)))
	return folders.pop() if len(folders) == 1 else None


def Attach(names):
	readers = []
	for name in names:
		try:
			reader = livefeed.RingReader(name)
		except (OSError, ValueError):
			break
		if reader.Ended():
			reader.Close()
			break
		readers.append(reader)
	if len(readers) == len(names):
		return readers
	for r in readers:
		r.Close()
	return None


def Main(argv=None):
	p = argparse.ArgumentParser(prog="campy-watch", description="Send alerts about the live tracking as campy "
		"records: a camera a frame ahead or behind, lost or late frames, tracking that collapses, campy-live stopping "
		"(see docs/alerts.md).")
	p.add_argument("--name", default="campy-live", help="campy's liveFeedName (default: campy-live)")
	p.add_argument("--cameras", type=int, default=3, help="How many cameras (default: 3)")
	p.add_argument("--rings", help="The cameras' rings, comma-separated, instead of --name and --cameras")
	p.add_argument("--bad-px", type=float, default=livehealth.BAD_PX, help="A 3D error above this many pixels is "
		"bad, as campy-live-view's --bad-px (default: %(default)s)")
	p.add_argument("--source", help="Which computer it is from (default: the alerts file's rig:)")
	p.add_argument("--stop-file", default="", help="Stop, as at Ctrl-C, once this file is there (campy-session)")
	p.add_argument("--launcher-pid", type=int, default=0, help="Stop waiting for the feed if this process (the "
		"program that started campy-watch, before campy-acquire) ends first")
	return Run(p.parse_args(argv))


if __name__ == "__main__":
	sys.exit(Main())
