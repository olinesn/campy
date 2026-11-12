"""
The recording watch: campy-acquire's checks of a recording as it goes, sent
as alerts (campy/notify.py: Slack direct messages) and said on the console.

A thread in campy-acquire's main process, which otherwise only waits for the
camera processes, so the cameras never wait for it. Every EVERY_S it reads
each camera's counters (campy/status.py: frames grabbed, waiting for ffmpeg,
given to ffmpeg, dropped; when the last came; what state the camera is in)
and checks:

  no frames        a camera that has sent frames sends none for
                   alertNoFramesSec (10 s): critical. Every camera at once
                   (their last frames within a second of each other): one
                   alert, the trigger has stopped (free-running cameras:
                   one alert too, which doesn't blame a trigger)
  trigger pause    a triggered camera's own timestamps show trigger pulses
                   it got no frame for (status.Camera.Missed, counted by
                   frametimes.TriggerGaps). Every camera missing the same
                   pulses, within a second of each other: the trigger
                   paused, a warning once frames come again (the trigger that stops
                   for good is "no frames" above). One camera only: it
                   missed them, a warning. No frame is lost in a pause, but
                   from there on the frames are later than their numbers
                   say. Each resolved after 2 min without another. With
                   alertTriggerPauseSec, a pause at least that long is
                   planned (a trigger that runs the cameras in bouts):
                   said on the console and written in alerts.log, not sent
  first frame      no frame 10 s after the trigger Arduino started, or
                   after the start when every camera free-runs
                   (cameraTrigger "None"): critical (an external trigger:
                   a warning after alertExternalWaitSec, 10 min, of
                   waiting; None: never)
  dropped frames   frames dropped in the last minute, because more than
                   writeQueueMaxSec of frames waited for ffmpeg: a warning,
                   critical above 1 % of them; resolved after 2 min without
  write stall      the frames waiting for ffmpeg reach a quarter of
                   writeQueueMaxSec (a warning) or three quarters (critical):
                   the disk or the encoder has stalled, and frames are
                   dropped once it is full. The EVO-X2's SSD stopped taking
                   writes for 10-16 s at a time. Not a warning in a camera's
                   first 10 s of frames, or a restarted ffmpeg's: ffmpeg
                   opens its encoder at its first frame (STALL_START_S)
  camera ended     ffmpeg stopped, the camera is gone (unplugged), its
                   thread or its process died: critical, no reminders
  ffmpeg restarted with restartFfmpeg, a camera's ffmpeg stopped and a new
                   one took over, the camera never stopping (writer.py): a
                   warning, once; and another once the stopped ffmpeg's
                   file is counted, with the frames lost in it
  disk             free space on each video folder's disk, every 10 s: a
                   warning below alertDiskFreeGB (20 GB); critical when, at
                   the bytes a frame it has been filling by over the last
                   5 min, the frames still to record (recTimeInSec x
                   frameRate a camera, less those grabbed) would take it
                   down to stopAtFreeGB: frames, not wall-clock time, so a
                   trigger that runs only part of the time is counted right;
                   and below stopAtFreeGB (2 GB), the recording is stopped,
                   cleanly, as at Ctrl-C, so that every video, frametimes.npy
                   and metadata.csv is whole (a full disk stops every ffmpeg
                   at once, and leaves no room for them). stopAtFreeGB 0:
                   never stopped
  trigger port     the trigger Arduino's serial port gone (unplugged, or it
                   reset), every 5 s, without sending it anything
  USB              a USB 3 camera on a USB 2 link, every 60 s (preflight's
                   reading: Linux's sysfs, or on Windows the camera's hub,
                   campy/usbwin.py, about 1.5 ms; not on macOS). A camera
                   whose link falls back to USB 2 leaves and comes back on
                   the port's USB 2 side, so this follows its "the camera is
                   gone": it says why, and what to replace
  GPU              Linux with an AMD GPU: the kernel log's amdgpu errors
                   (campy/gpulog.py, read on a thread of its own). Firmware
                   stalls ("Fence fallback timer expired on ring ..."): a
                   warning at alertGpuStalls (3) within 10 min; a hung GPU
                   job (a ring timeout, the firmware scheduler not
                   answering, a page fault): a warning; a GPU or ring reset:
                   critical. Each resolved after 10 min without another.
                   alertGpuLog False: not read

and says when the recording started and how it ended. With a healthchecks.io
check in the alerts file, it pings it every minute and pauses it at the end
(notify.Notifier.Heartbeat).

Two cameras or more with a problem of the same kind (frames waiting, dropped
frames, no frames, a recording ended, USB 2), starting in the same check, are
one message ("3 cameras at once: ...", key cameras/<kind>), and so is their
resolution, once each camera's is over: a disk that stalls fills every
camera's queue at once. Each camera's own problem is still said on the
console, and listed in recording.json.

How it ended (Stop) is said last in campy-acquire, after the trigger has
stopped and session.json is written, so that a slow network holds up
neither; the summary and the heartbeat's pause go ahead of the messages
still waiting, which then get FINISH_S (30 s). An error that ended
campy-acquire's recording (Stop's error) makes the summary critical, and
recording.json "failed".

What it finds goes into the recording's alerts.log (one JSON object a line:
notify.Notifier.Log), whether or not the alerts file names a channel. And
every STATUS_EVERY_S, and as the recording ends, it writes the recording's
state now to ~/.campy/status/recording.json (Windows:
%USERPROFILE%\\.campy\\status\\recording.json; CAMPY_STATUS_DIR names
another folder), for a control panel to show (campy-session's):

  schema       1
  updated_at   ISO time, with its UTC offset
  host_model   the alerts file's rig:, or null
  campy_version
  state        recording, stopping, ended or failed
  session      {folder, started_at, elapsed_s, planned_s}
  cameras      [{name, serial, make, fps_target, fps_measured, frames,
               dropped, write_queue_s, usb_speed (Mb/s; Linux, Windows), state,
               ffmpeg_restarts, frames_lost_in_encoder,
               trigger_pulses_without_frame (pauses included)}]
  disk         [{path, free_gb, projected_full_at (ISO, or null)}]
  open         [{key, severity, text}]: the problems open now
  last_error   {time, text} of the last critical problem, or null
  trigger_pauses [{pulses, seconds, before_frame, planned}]: the trigger's pauses
               so far; planned: at least alertTriggerPauseSec, not sent
  gpu_log      null (not Linux with an AMD GPU, or alertGpuLog False), or
               {read_with ("journalctl", "/dev/kmsg" or null), not_read
               (why not, or null), stalls, hangs, resets (counts so far),
               stalls_last_minute, rings ({ring: messages})}

written whole each time (a .tmp file, then replaced), by campy-acquire only.

Nothing here acts on the recording but the stop at stopAtFreeGB: the watch
only reads. An error in a check (or in its other steps: resolutions, the
grouping, the heartbeat, the alerts' reminders) is said once on the console,
and sent once as a warning (watch/<check>: its alerts are off while it
fails), resolved once it works again; the other checks go on.
"""

import collections, datetime, json, math, os, shutil, sys, threading, time

from campy import gpulog, notify, segments, strictjson, status as camstatus
from campy.utils.utils import Say, ErrorText

EVERY_S = 2.0 # how often the cameras are checked
DISK_EVERY_S = 10.0
DISK_WINDOW_S = 300.0 # how far back the disk's filling rate is measured
DISK_RATE_MIN_S = 60.0 # the rate is known once it spans this long
SERIAL_EVERY_S = 5.0
USB_EVERY_S = 60.0
DROPS_WINDOW_S = 60.0
DROPS_CLEAR_S = 120.0 # dropped frames are over once none were dropped for this long
DROPS_CRITICAL = 0.01 # of the last minute's frames
STALL_WARNING, STALL_CRITICAL, STALL_CLEAR = 0.25, 0.75, 0.10 # of writeQueueMaxSec
STALL_UNBOUNDED_S = (10.0, 30.0) # writeQueueMaxSec "None": seconds of frames waiting, warning and critical
STALL_CLEAR_S = 30.0
STALL_START_S = 10.0 # a new ffmpeg's first seconds of frames: no write-stall warning (it opens its encoder then)
FIRST_FRAME_S = 10.0 # after the trigger Arduino started
EXTERNAL_WAIT_S = 600.0 # an external trigger (startArduino False): waiting this long is said (alertExternalWaitSec's default)
TOGETHER_S = camstatus.TOGETHER_S # cameras whose last frames are this close stopped together: the trigger
MISSED_SETTLE_S = 1.0 # pulses without a frame are looked at once this old: every camera that missed them has said so
MISSED_CLEAR_S = 120.0 # a trigger pause, or a camera's missed pulses, are over once none came for this long
SENT_WINDOW_S = 10.0
RESTARTS_PER_HOUR = 3 # as writer.RESTARTS_PER_HOUR (a test checks), which this module does not import
DiskUsage = shutil.disk_usage # (tests replace it: a disk that fills)
STATUS_EVERY_S = 4.0 # recording.json, written this often
STATUS_SCHEMA = 1
STATUS_ENV = "CAMPY_STATUS_DIR" # where recording.json goes, instead of ~/.campy/status
GPU_WINDOW_S = 600.0 # firmware stalls counted over this long; each GPU problem resolved after this long without
GPU_KINDS = {gpulog.STALL: "firmware stall", gpulog.HANG: "hung GPU job", gpulog.RESET: "GPU reset"}


def DiskText(n):
	# Rounded down: 1.996 GB, below a 2 GB stopAtFreeGB, is "1.99 GB", not "2.00 GB"
	p = 2 if n < 10e9 else 1
	return "{:.{p}f} GB".format(math.floor(n / 1e9 * 10 ** p) / 10 ** p, p=p)


STATE_NAMES = {camstatus.WAITING: "waiting", camstatus.GRABBING: "grabbing", camstatus.ENDED: "ended",
	camstatus.NOT_STARTED: "not started", camstatus.LOST: "lost", camstatus.VIDEO_FAILED: "video failed",
	camstatus.CAMERA_FAILED: "camera failed"}


FINISH_S = 30.0 # as the recording ends, how long the alerts get to go (notify.Notifier.Finish)

# A camera's problems of these kinds that start in the same check, in two cameras or more, are one message
# ("3 cameras at once: ..."), whose resolution is one message too. A disk that stalls fills every camera's
# queue at once: 5 messages a camera (warning, worse, dropped frames, and each resolved), 40 with 8 cameras
GROUPS = {
	"queue": "frames waiting for ffmpeg. The disk or the encoder has stalled{}.",
	"drops": "frames dropped: more than writeQueueMaxSec ({}) of frames were waiting for ffmpeg.",
	"no-frames": "no frames.",
	"ended": "their recordings have ended.",
	"usb": "on a USB 2 link, not USB 3: they can't carry their frames. Check their cables, hub and ports.",
}


GROUP_S = 5.0 # a camera's problem this soon after a group of its kind began joins it; later, it is its own


class Problem:
	def __init__(self, severity, text, hold, remind, line=None):
		self.severity, self.text, self.hold, self.remind = severity, text, hold, remind
		self.line = line or text # in a message about several cameras at once
		self.clearSince = None
		self.sent = None # a camera's kind in GROUPS: "alone" (its own key), "group" (a Grouped's), None (not yet)
		self.group = None


class Grouped:
	# Several cameras' problems of one kind, sent as one message (key cameras/<kind>, or cameras/<kind>/2 ... while
	# an earlier one is still open)
	def __init__(self, kind, key, began):
		self.kind, self.key, self.began = kind, key, began
		self.members = [] # their keys, open
		self.cleared = [] # what each that is over said


class Watch:
	"""
	One recording's watch. params: campy's; systems: campy's (the trigger's
	serial port, the devices found); cameras: unicam.CameraProcesses;
	counters: status.Make's array; folder: the session's folder (alerts.log
	goes there). stopping(): the user is stopping it (Ctrl-C), when cameras
	ending is no problem. request_stop(why): stops the recording, as Ctrl-C
	does (the disk is nearly full). Start() runs it in a thread; Stop(failures) ends it
	and says how the recording ended.
	"""

	def __init__(self, params, systems, cameras, counters, folder=None, notifier=None, cams=None,
			clock=time.monotonic, disk_usage=None, sysfs_root="/sys", say=Say, stopping=None, request_stop=None,
			port_gone=None, gpu_log=None):
		self.params, self.systems, self.cameras = params, systems, cameras
		self.names = list(cameras.names)
		self.counters = camstatus.Reader(counters, len(self.names))
		self.folder = folder
		self.clock, self.sysfs_root, self.say = clock, sysfs_root, say
		self.disk_usage = disk_usage or (lambda path: DiskUsage(path))
		self.stopping = stopping or (lambda: False)
		self.request_stop = request_stop # stops the recording as Ctrl-C does (campy.Main's StopRequested)
		self.port_gone = port_gone or PortGone # (tests: a port that is there)
		self.gpuLog = gpu_log # a gpulog.Reader (Start makes one on Linux with an AMD GPU; tests pass their own)
		self.gpuStalls = int(params.get("alertGpuStalls", 3))
		self.gpuEvents = collections.defaultdict(collections.deque) # kind: (t, ring), the last GPU_WINDOW_S
		self.gpuCounts = collections.Counter() # kind: messages counted, since the start
		self.gpuRings = collections.Counter() # ring: its messages, since the start
		self.gpuLast = {} # kind: its last message
		if notifier is None:
			if params.get("alerts", True):
				notifier = notify.Notifier(say=say)
			else:
				notifier = notify.Notifier(channels=[], say=say)
		self.notifier = notifier
		if folder:
			self.notifier.SetLog(os.path.join(folder, "alerts.log"))
		self.frameRate = float(params["frameRate"])
		self.noFramesSec = float(params.get("alertNoFramesSec", 10))
		self.diskFree = float(params.get("alertDiskFreeGB", 20)) * 1e9
		self.stopAt = float(params.get("stopAtFreeGB", 2)) * 1e9
		limit = params.get("writeQueueMaxSec", "None")
		self.queueLimit = None if limit is None or str(limit) == "None" else float(limit)
		self.recTime = float(params.get("recTimeInSec", 0))
		planned = params.get("alertTriggerPauseSec", "None")
		self.plannedPause = None if planned is None or str(planned) == "None" else float(planned)
		wait = params.get("alertExternalWaitSec", EXTERNAL_WAIT_S)
		self.externalWait = None if wait is None or str(wait) == "None" else float(wait) # None: never said
		self.cams = cams
		# What makes the frames: the trigger Arduino campy starts (systems' "serial"), else a trigger campy doesn't
		# control (a camera with cameraTrigger set: the Pico, another board), else nothing: the cameras free-run
		self.board = "serial" in systems
		self.freeRunning = not self.board and not any(self.Triggered(c) for c in range(len(self.names)))
		self.external = not self.board and not self.freeRunning
		self.problems = {} # key: Problem, open
		self.history = collections.defaultdict(collections.deque) # camera: (t, grabbed, dropped, sent)
		self.disks = collections.defaultdict(collections.deque) # folder: (t, free, frames grabbed by its cameras)
		self.diskFirst = {} # folder: its first (t, free, frames), for a trigger that runs in bouts
		self.at = collections.defaultdict(lambda: None) # what ran when: "disk", "serial", "usb"
		self.started = None
		self.startedAt = None # wall time, for recording.json
		self.stopped = None # why the watch stopped the recording, if it did
		self.diskState = {} # folder: {path, free_gb, projected_full_at}, for recording.json
		self.usbSpeed = {} # camera: its link's Mb/s, as last read
		self.serialClosed = False # the trigger's port was gone, and its connection closed (CheckSerial)
		self.restartsSaid = collections.defaultdict(int) # camera: ffmpeg restarts said
		self.writeFrom = {} # camera: (when frames were first seen going to its ffmpeg, that ffmpeg's restarts count)
		self.countedSaid = collections.defaultdict(int) # camera: stopped ffmpegs' files counted, as said
		self.lostSaid = collections.defaultdict(int) # camera: frames lost in them, as said
		self.missedSaid = collections.defaultdict(int) # camera: trigger pulses without a frame, as said
		self.pauses = [] # the trigger's pauses: (pulses, the frame after)
		self.planned = [] # its planned pauses (alertTriggerPauseSec), the same way: logged, not sent
		self.missedAlone = collections.defaultdict(int) # camera: pulses it missed that the others got
		self.lastError = None
		self.errors = set() # the checks whose error has been said on the console
		self.failing = set() # the checks failing now (an alert, watch/<check>)
		self.pending = [] # cameras' problems of a kind in GROUPS, raised in this check: sent at its end (Group)
		self.groups = [] # Grouped, open
		self.done = threading.Event()
		self.thread = None
		self.halted = False

	# ------------------------------------------------------------------ the thread

	def Start(self):
		self.started = self.clock()
		self.startedAt = Wall()
		if self.gpuLog is None and self.params.get("alertGpuLog", True) and gpulog.Wanted(self.sysfs_root):
			try:
				self.gpuLog = gpulog.Reader().Start() # from this thread (the main one), which outlives the reading
			except Exception as e:
				self.say("Watch: could not read the kernel log: {}".format(ErrorText(e)))
		self.Begun()
		self.WriteStatus("recording")
		self.thread = threading.Thread(target=self.Run, name="campy watch", daemon=True)
		self.thread.start()
		return self

	def Run(self):
		while not self.done.wait(EVERY_S):
			try:
				self.Check()
			except Exception as e: # (each of its steps catches its own: never let the thread end)
				self.Failed("watch", e)

	def Halt(self):
		"""Ends the checks (the thread, the kernel log's reading), before the trigger's port is closed."""
		self.done.set()
		if self.halted:
			return
		self.halted = True
		if self.thread is not None:
			self.thread.join(5)
		if self.gpuLog is not None:
			try:
				self.gpuLog.Stop()
			except Exception as e:
				self.say("Watch: could not stop reading the kernel log: {}".format(ErrorText(e)))

	def Stop(self, failures=None, error=None, timeout=FINISH_S):
		"""
		Ends the watch (Halt, if not yet), says how the recording ended, and
		waits (up to timeout s) for the alerts to go: the end summary and the
		heartbeat's pause first; what can't go by then is said, and logged.
		error: what ended campy-acquire's recording, if an error did (the
		summary is then critical). campy-acquire calls it last, once the
		trigger has stopped and session.json is written, which a slow network
		then holds up no more.
		"""
		self.Halt()
		self.Step("grouping", self.Group, self.clock())
		try:
			self.Ended(failures or {}, error)
		except Exception as e:
			self.say("Watch: could not say how the recording ended: {}".format(ErrorText(e)))
		self.WriteStatus("failed" if failures or error or self.stopped or self.StillOpen(notify.CRITICAL) else "ended")
		self.notifier.Pause()
		self.notifier.Finish(timeout, "campy-acquire")

	def Check(self, now=None):
		"""Every check, once (the thread calls it every EVERY_S)."""
		now = self.clock() if now is None else now
		for name, check, every in (("cameras", self.CheckCameras, 0), ("disk", self.CheckDisk, DISK_EVERY_S),
				("serial", self.CheckSerial, SERIAL_EVERY_S), ("usb", self.CheckUsb, USB_EVERY_S), ("gpu", self.CheckGpu, 0),
				("status", lambda now: self.WriteStatus("stopping" if self.stopping() else "recording"),
				STATUS_EVERY_S)):
			if every and self.at[name] is not None and now - self.at[name] < every:
				continue
			self.at[name] = now
			self.Step(name, check, now)
		# Outside the checks, but caught the same way: one that raised would end the watch's thread for good
		self.Step("resolutions", self.Settle, now)
		self.Step("grouping", self.Group, now)
		self.Step("heartbeat", lambda now: self.notifier.Heartbeat(), now)
		self.Step("alerts", lambda now: self.notifier.Tick(), now)

	def Step(self, name, call, now):
		# One check, or step of a check: an error is said once on the console, and sent as a warning (its alerts are
		# off while it fails); resolved once it works again
		try:
			call(now)
		except Exception as e:
			self.Failed(name, e)
			return
		if name in self.failing:
			self.failing.discard(name)
			self.Clear("watch/" + name, "The recording watch's {} check works again.".format(name), now)

	def Failed(self, name, e):
		if name not in self.errors: # said once
			self.errors.add(name)
			self.say("Watch: the {} check failed: {}".format(name, ErrorText(e)))
		self.failing.add(name)
		key = "watch/" + name
		text = "The recording watch's {} check failed: {}. Its alerts are off while it fails; the recording goes " \
			"on.".format(name, ErrorText(e))
		problem = self.problems.get(key)
		if problem is None:
			problem = self.problems[key] = Problem(notify.WARNING, text, 0.0, False)
		problem.text, problem.clearSince = text, None
		try:
			self.notifier.Problem(key, text, notify.WARNING, remind=False)
		except Exception:
			pass # the alerts themselves are what fails: said on the console

	# ------------------------------------------------------------------ problems: raised, cleared

	def Raise(self, key, severity, text, hold=0.0, remind=True, line=None):
		problem = self.problems.get(key)
		if severity == notify.CRITICAL:
			self.lastError = {"time": Wall(), "text": text}
		if problem is None:
			problem = self.problems[key] = Problem(severity, text, hold, remind, line)
			self.say("Alert: {}".format(text))
		else:
			if notify.LEVELS[severity] > notify.LEVELS[problem.severity]:
				self.say("Alert, worse: {}".format(text))
			problem.severity, problem.text, problem.clearSince = severity, text, None
			problem.line = line or text
		if self.Kind(key) is None or problem.sent == "alone":
			self.notifier.Problem(key, text, severity, remind=remind)
		elif problem.sent is None and key not in self.pending:
			self.pending.append(key) # sent at the check's end: alone, or with other cameras' (Group)

	def Kind(self, key):
		# A camera's problem of a kind in GROUPS: its kind; else None
		name, _, kind = key.rpartition("/")
		return kind if kind in GROUPS and name in self.names else None

	def Group(self, now):
		# The problems raised in this check, of a kind in GROUPS: in two cameras or more, or into a group of that
		# kind that began within GROUP_S, one message; one camera's, alone. Then each group's message, as its
		# cameras' problems are now (the notifier sends it again only when it is worse)
		pending, self.pending = [k for k in self.pending if k in self.problems], []
		for kind in GROUPS:
			new = [k for k in pending if self.Kind(k) == kind]
			group = next((g for g in self.groups if g.kind == kind and now - g.began <= GROUP_S), None)
			if group is None and len(new) >= 2:
				used, n, key = {g.key for g in self.groups}, 1, "cameras/" + kind
				while key in used:
					n += 1
					key = "cameras/{}/{}".format(kind, n)
				group = Grouped(kind, key, now)
				self.groups.append(group)
			for key in new:
				p = self.problems[key]
				if group is None:
					p.sent = "alone"
					self.notifier.Problem(key, p.text, p.severity, remind=p.remind)
				else:
					p.sent, p.group = "group", group
					group.members.append(key)
		for group in self.groups:
			members = [self.problems[k] for k in sorted(group.members, key=lambda k: self.names.index(self.Name(k)))]
			if members:
				self.notifier.Problem(group.key, self.GroupText(group.kind, members),
					max((p.severity for p in members), key=notify.LEVELS.get), remind=any(p.remind for p in members))

	def Name(self, key):
		return key.rpartition("/")[0]

	def GroupText(self, kind, members):
		if kind == "queue":
			what = GROUPS[kind].format(": frames are dropped once it reaches 100 %" if self.queueLimit else
				", and memory fills")
		elif kind == "drops":
			what = GROUPS[kind].format(self.QueueLimitText())
		else:
			what = GROUPS[kind]
		return "\n".join(["{} cameras at once: {}".format(len(members), what)] + ["- " + p.line for p in members])

	def Clear(self, key, text=None, now=None):
		problem = self.problems.get(key)
		if problem is not None and problem.clearSince is None:
			problem.clearSince = (self.clock() if now is None else now, text)

	def Settle(self, now):
		# Problems clear for their hold: resolved. A camera's in a group leaves it; the group's last, its resolution
		for key, problem in list(self.problems.items()):
			if problem.clearSince is not None and now - problem.clearSince[0] >= problem.hold:
				del self.problems[key]
				text = problem.clearSince[1]
				self.say("Alert resolved: {}".format(text or problem.text))
				if self.Kind(key) is None or problem.sent == "alone":
					self.notifier.Resolved(key, text)
				elif problem.group is not None:
					group = problem.group
					group.members.remove(key)
					group.cleared.append(text or "{}: over".format(self.Name(key)))
					if not group.members:
						self.groups.remove(group)
						self.notifier.Resolved(group.key, "\n".join(["{} cameras:".format(len(group.cleared))] +
							["- " + t for t in group.cleared]))

	def Open(self):
		return dict(self.problems)

	# ------------------------------------------------------------------ the cameras

	def CheckCameras(self, now):
		rows = [self.counters.Row(c) for c in range(len(self.names))]
		for c, (name, row) in enumerate(zip(self.names, rows)):
			self.CheckRestarts(c, name, row)
		ns = camstatus.Now()
		self.CheckMissed(rows, ns, now)
		if self.stopping():
			return # Ctrl-C: the cameras are ending, as asked
		gaps = {} # camera: (seconds without a frame, its last frame's ns), of those grabbing that have sent frames
		failures = self.Failures()
		for c, (name, row) in enumerate(zip(self.names, rows)):
			self.CheckEnded(c, name, row, failures)
			if row["state"] != camstatus.GRABBING:
				continue
			history = self.history[c]
			history.append((now, row["grabbed"], row["dropped"], row["sent"]))
			while history and history[0][0] < now - max(DROPS_WINDOW_S, SENT_WINDOW_S) - EVERY_S:
				history.popleft()
			if row["grabbed"] > 0:
				gaps[c] = ((ns - row["frameNs"]) / 1e9, row["frameNs"])
				if gaps[c][0] <= self.noFramesSec:
					self.Clear("{}/no-frames".format(name), "{}: frames again (frame {})".format(name, row["grabbed"]),
						now)
			self.CheckDrops(c, name, row, now)
			self.CheckQueue(c, name, row, now, c in gaps and gaps[c][0] > self.noFramesSec)
		self.CheckQuiet(rows, gaps, now)
		self.CheckFirst(rows, now)

	def Failures(self):
		try:
			return dict(self.cameras.failures)
		except Exception: # the main thread changed it as it was read: next time
			return {}

	def CheckEnded(self, c, name, row, failures):
		state = row["state"]
		if state == camstatus.LOST:
			self.Raise("{}/ended".format(name), notify.CRITICAL, "{}: the camera is gone (unplugged, or its USB link "
				"lost) after frame {}. Its recording has ended there; the other cameras go on. It needs a replug, and "
				"a new recording.".format(name, row["grabbed"]), remind=False)
		elif state == camstatus.VIDEO_FAILED and row["restarts"]:
			self.Raise("{}/ended".format(name), notify.CRITICAL, "{}: ffmpeg stopped again, in {}, and the camera "
				"has stopped, after frame {}. It had been restarted {} time{}, and was not again (at most {} an hour, "
				"never when the disk is the cause). Its video files can be read up to their last fragments; the other "
				"cameras go on.".format(name, self.VideoName(c, row["restarts"] + 1), row["grabbed"], row["restarts"],
				"" if row["restarts"] == 1 else "s", RESTARTS_PER_HOUR), remind=False)
		elif state == camstatus.VIDEO_FAILED:
			self.Raise("{}/ended".format(name), notify.CRITICAL, "{}: ffmpeg stopped after taking {} frames, and the "
				"camera has stopped. Its video can be read up to its last fragment; the other cameras go on.".format(
				name, row["sent"]), remind=False)
		elif state == camstatus.CAMERA_FAILED:
			self.Raise("{}/ended".format(name), notify.CRITICAL, "{}: the camera thread stopped with an error after "
				"frame {}; the other cameras go on.".format(name, row["grabbed"]), remind=False)
		elif state == camstatus.NOT_STARTED:
			self.Raise("{}/ended".format(name), notify.CRITICAL, "{}: did not start grabbing, so it records nothing."
				.format(name), remind=False)
		elif name in failures and "{}/ended".format(name) not in self.problems:
			# Its process ended without saying how (killed, crashed), or said what went wrong
			self.Raise("{}/ended".format(name), notify.CRITICAL, "{}: {}. The other cameras go on.".format(
				name, "; ".join(failures[name])), remind=False)

	def VideoName(self, c, part):
		# The camera's video file of that part (0.part2.mp4)
		cam = self.cams[c] if self.cams and c < len(self.cams) else {}
		video = cam.get("videoFilename") or self.params.get("videoFilename", "0.mp4")
		if isinstance(video, (list, tuple)):
			video = video[c] if c < len(video) else video[0]
		return segments.PartName(os.path.basename(str(video)), part)

	def CheckRestarts(self, c, name, row):
		# restartFfmpeg: each restart said once, as it happens, and then the frames it lost, once counted
		while self.restartsSaid[c] < row["restarts"]:
			self.restartsSaid[c] += 1
			k = self.restartsSaid[c]
			text = "{}: ffmpeg stopped, and campy started a new one (restartFfmpeg; restart {} of the recording). The " \
				"camera never stopped: its video goes on in a new file, {}. The frames inside the stopped ffmpeg, " \
				"about a second, are lost; segments.csv in its folder will say which.".format(name, k,
				self.VideoName(c, k + 1))
			self.say("Alert: {}".format(text))
			self.notifier.Note(text, notify.WARNING)
		if row["counted"] > self.countedSaid[c] and self.restartsSaid[c]:
			lost = row["lost"] - self.lostSaid[c]
			text = "{}: the stopped ffmpeg's file is counted: {} frame{} given to it {} not in it, and lost{}.".format(
				name, lost, "" if lost == 1 else "s", "is" if lost == 1 else "are",
				" ({} in all)".format(row["lost"]) if row["lost"] != lost else "")
			self.say("Alert: {}".format(text))
			self.notifier.Note(text, notify.WARNING)
		if row["counted"] > self.countedSaid[c]:
			self.countedSaid[c], self.lostSaid[c] = row["counted"], row["lost"]

	def CheckQuiet(self, rows, gaps, now):
		# Cameras with no frame for noFramesSec. When every camera stopped together (their last frames within
		# TOGETHER_S), one alert: the trigger. Until the last of them is quiet too, none is said on its own.
		quiet = [c for c, (gap, _) in gaps.items() if gap > self.noFramesSec]
		nearly = [c for c, (gap, _) in gaps.items() if gap > self.noFramesSec - TOGETHER_S - EVERY_S]
		lasts = [gaps[c][1] for c in nearly]
		together = len(gaps) >= 2 and len(nearly) == len(gaps) and camstatus.Together(lasts)
		if together and len(quiet) == len(gaps):
			if self.freeRunning: # no trigger to blame: the cameras time their own frames
				why = "They free-run, with no trigger: is the computer, or their USB, stuck?"
			else:
				why = "so the trigger has stopped ({}).".format(self.TriggerText())
			self.Raise("trigger/stopped", notify.CRITICAL, "No camera has had a frame for {:.0f} s: all {} stopped "
				"together{}{}".format(min(gaps[c][0] for c in quiet), len(gaps), ". " if self.freeRunning else ", ",
				why))
			return
		if together:
			return # the others are about to be quiet too
		if not quiet:
			self.Clear("trigger/stopped", "Frames again", now)
		if "trigger/stopped" in self.problems:
			return # some cameras have frames again, not all: still that
		for c in quiet:
			self.Raise("{}/no-frames".format(self.names[c]), notify.CRITICAL, "{}: no frame for {:.0f} s, since frame "
				"{}".format(self.names[c], gaps[c][0], rows[c]["grabbed"]))

	def Triggered(self, c):
		# Whether camera c takes a frame at each trigger pulse (as unicam.Triggered): its pulses without a frame count
		if self.cams and c < len(self.cams):
			cam = self.cams[c]
			trigger = cam.get("cameraTrigger", self.params.get("cameraTrigger", "None"))
			make = cam.get("cameraMake", self.params.get("cameraMake"))
		else: # no camera list (tests): the config's own, one a camera where it gives a list
			trigger, make = (PerCamera(self.params.get(key, default), c) for key, default in
				(("cameraTrigger", "None"), ("cameraMake", None)))
		return str(trigger) != "None" and make != "ratcam"

	def CheckMissed(self, rows, ns, now, final=False):
		# Trigger pulses without a frame, new since the last check, once every camera that missed them has had its
		# next frame (MISSED_SETTLE_S; final: now). Every camera at once: the trigger paused. Else each camera's own.
		triggered = [c for c, row in enumerate(rows) if row["grabbed"] > 0 and self.Triggered(c) and
			row["state"] in (camstatus.GRABBING, camstatus.ENDED)] # a camera that failed is said apart
		new = {c: rows[c]["missed"] - self.missedSaid[c] for c in triggered if rows[c]["missed"] > self.missedSaid[c]}
		if not new:
			if self.pauses:
				self.Clear("trigger/paused", "Trigger: no pause for {} ({} in all)".format(
					notify.Duration(MISSED_CLEAR_S), len(self.pauses)), now)
			for c, n in self.missedAlone.items():
				self.Clear("{}/missed".format(self.names[c]), "{}: no trigger pulse missed for {} ({} in all)".format(
					self.names[c], notify.Duration(MISSED_CLEAR_S), n), now)
			return
		if not final and (ns - max(rows[c]["missedNs"] for c in new)) / 1e9 < MISSED_SETTLE_S:
			return
		for c in new:
			self.missedSaid[c] = rows[c]["missed"]
		resumed = [rows[c]["missedNs"] for c in new]
		if len(triggered) >= 2 and len(new) == len(triggered) and camstatus.Together(resumed):
			n = min(new.values())
			frame = min(rows[c]["missedFrame"] for c in new)
			if self.Planned(n):
				self.LogPlanned(len(triggered), n, frame, now)
			else:
				self.pauses.append((n, frame))
				self.RaisePause(len(triggered), n, frame, now)
			new = {c: k - n for c, k in new.items() if k > n} # more than the pause: missed by that camera too
		for c, n in new.items():
			name = self.names[c]
			if len(triggered) == 1 and self.Planned(n): # one camera: a pause this long is the trigger's, planned
				self.LogPlanned(1, n, rows[c]["missedFrame"], now)
				continue
			self.missedAlone[c] += n
			if len(triggered) >= 2:
				text = "{}: no frame for {} trigger pulse{} ({:.2f} s) that the other cameras got, just before its " \
					"frame {}{}. Counted from the start, its frames are out of step with theirs from there on; " \
					"campy-align puts every camera on the trigger's timeline.".format(name, n, "" if n == 1 else "s",
					n / self.frameRate, rows[c]["missedFrame"], self.InAll(self.missedAlone[c], n))
			else:
				text = "{}: no frame for {} trigger pulse{} ({:.2f} s), just before frame {}{}: the trigger paused, " \
					"or the camera missed them. From there on its frames are later than their numbers say; " \
					"frametimes.npy has each one's time.".format(name, n, "" if n == 1 else "s", n / self.frameRate,
					rows[c]["missedFrame"], self.InAll(self.missedAlone[c], n))
			self.Raise("{}/missed".format(name), notify.WARNING, text, hold=MISSED_CLEAR_S)

	def InAll(self, total, n):
		return " ({} in all)".format(total) if total != n else ""

	def Planned(self, n):
		# Whether a pause of n pulses is planned: at least alertTriggerPauseSec long
		return self.plannedPause is not None and n / self.frameRate >= self.plannedPause - 1e-9

	def LogPlanned(self, cameras, n, frame, now):
		# A planned pause: on the console and in alerts.log, not sent
		self.planned.append((n, frame))
		since = notify.Duration(now - self.started) if self.started is not None else "?"
		who = "all {} cameras got no frame for the same".format(cameras) if cameras > 1 else "no frame for"
		text = "The trigger paused as planned: {} {} pulse{} ({}), just before frame {}, about {} in " \
			"(alertTriggerPauseSec {:g}: logged, not sent). Planned pause {} of the recording.".format(who, n,
			"" if n == 1 else "s", notify.Duration(n / self.frameRate), frame, since, self.plannedPause,
			len(self.planned))
		self.say("Trigger: {}".format(text))
		self.notifier.Log(notify.INFO, "trigger/planned-pause", "planned pause", "logged only", text)

	def RaisePause(self, cameras, n, frame, now):
		total = sum(p for p, _ in self.pauses) + sum(p for p, _ in self.planned)
		since = notify.Duration(now - self.started) if self.started is not None else "?"
		text = "The trigger paused: all {} cameras got no frame for the same {} pulse{} ({:.2f} s), just before frame " \
			"{}, about {} in. No frame was lost, but from there on the frames are {:.2f} s later than their numbers " \
			"say at {:g} fps; frametimes.npy has each one's time.".format(cameras, n, "" if n == 1 else "s",
			n / self.frameRate, frame, since, total / self.frameRate, self.frameRate)
		if len(self.pauses) > 1:
			text += " Pause {} of the recording.".format(len(self.pauses))
		self.Raise("trigger/paused", notify.WARNING, text, hold=MISSED_CLEAR_S)

	def PausesText(self):
		if not self.pauses:
			return None
		total = sum(n for n, _ in self.pauses)
		return "The trigger paused {} time{}: {} pulse{} without a frame in every camera ({:.2f} s), the first just " \
			"before frame {}. From each pause on, the frames are later than their numbers say; frametimes.npy has " \
			"each one's time.".format(len(self.pauses), "" if len(self.pauses) == 1 else "s", total,
			"" if total == 1 else "s", total / self.frameRate, self.pauses[0][1])

	def PlannedText(self):
		if not self.planned:
			return None
		total = sum(n for n, _ in self.planned)
		return "The trigger paused as planned {} time{} (alertTriggerPauseSec {:g}): {} in all, the first just before " \
			"frame {}.".format(len(self.planned), "" if len(self.planned) == 1 else "s", self.plannedPause,
			notify.Duration(total / self.frameRate), self.planned[0][1])

	def TriggerNote(self):
		# How the frames are made, for the start note: nothing for the trigger Arduino (said on the console)
		if self.freeRunning:
			return ", free-running (no trigger)"
		return ", on an external trigger" if self.external else ""

	def TriggerText(self):
		if self.external:
			return "an external trigger: is it still running?"
		return "the trigger Arduino on {}: unplugged, or it reset?".format(self.params.get("serialPort"))

	def CheckFirst(self, rows, now):
		waited = now - self.started if self.started is not None else 0
		waiting = [c for c, row in enumerate(rows) if row["state"] == camstatus.GRABBING and row["grabbed"] == 0]
		if not waiting:
			self.Clear("trigger/first", "The first frames came", now)
			return
		if len(waiting) == len(self.names):
			if self.external:
				if self.externalWait is not None and waited >= self.externalWait:
					self.Raise("trigger/first", notify.WARNING, "No frame yet, {} after campy started waiting for the "
						"external trigger: is it running?".format(notify.Duration(waited)))
			elif waited >= FIRST_FRAME_S and self.freeRunning:
				self.Raise("trigger/first", notify.CRITICAL, "No camera has had a frame {:.0f} s after the recording "
					"started. They free-run, with no trigger, so their frames should have come at once.".format(waited))
			elif waited >= FIRST_FRAME_S:
				self.Raise("trigger/first", notify.CRITICAL, "No camera has had a frame {:.0f} s after the trigger "
					"Arduino on {} started.".format(waited, self.params.get("serialPort")))
		elif waited >= FIRST_FRAME_S:
			self.Clear("trigger/first", None, now)
			for c in waiting:
				self.Raise("{}/no-frames".format(self.names[c]), notify.CRITICAL, "{}: no frame yet, {:.0f} s after the "
					"others' first".format(self.names[c], waited))

	def Since(self, c, seconds, now):
		# This camera's sample from about seconds ago, or its oldest
		history = self.history[c]
		for sample in history:
			if sample[0] >= now - seconds:
				return sample
		return history[0] if history else None

	def CheckDrops(self, c, name, row, now):
		# New drops since the last check: said, with the last minute's count; resolved DROPS_CLEAR_S after the last
		key = "{}/drops".format(name)
		then = self.Since(c, DROPS_WINDOW_S, now)
		history = self.history[c]
		if then is None or len(history) < 2:
			return
		dropped = row["dropped"] - then[2]
		frames = max(1, row["grabbed"] - then[1])
		if row["dropped"] > history[-2][2]:
			severity = notify.CRITICAL if dropped > DROPS_CRITICAL * frames else notify.WARNING
			line = "{}: dropped {} frames in the last {} ({} since the start)".format(name, dropped,
				notify.Duration(now - then[0]), row["dropped"])
			self.Raise(key, severity, "{}: more than writeQueueMaxSec ({}) of frames were waiting for ffmpeg.".format(
				line, self.QueueLimitText()), hold=DROPS_CLEAR_S, line=line)
		elif row["dropped"]:
			self.Clear(key, "{}: no frame dropped for {} ({} in all)".format(name, notify.Duration(DROPS_CLEAR_S),
				row["dropped"]), now)

	def QueueLimitText(self):
		return "{:g} s".format(self.queueLimit) if self.queueLimit is not None else "unbounded"

	def CheckQueue(self, c, name, row, now, quiet):
		key = "{}/queue".format(name)
		waiting = row["queued"] / self.frameRate # seconds of frames waiting for ffmpeg
		# ffmpeg opens its encoder only once its first frame comes: four Quick Sync encoders opening at once let
		# about a second of frames pile up (the ChronicRecord PC, 60 fps), and then catch up. So for its first
		# STALL_START_S of frames, a camera's ffmpeg (its first, or a new one after a restart) gets no warning;
		# critical (frames about to be dropped) is still said, and so are dropped frames
		begun = self.writeFrom.get(c)
		if row["grabbed"] > 0 and (begun is None or begun[1] != row["restarts"]):
			begun = self.writeFrom[c] = (now, row["restarts"])
		starting = begun is not None and now - begun[0] < STALL_START_S
		if quiet:
			return # no frame lately: its count of frames waiting is old
		if self.queueLimit:
			share = waiting / self.queueLimit
			warn, critical, clear = share >= STALL_WARNING, share >= STALL_CRITICAL, share < STALL_CLEAR
		else:
			warn, critical = waiting >= STALL_UNBOUNDED_S[0], waiting >= STALL_UNBOUNDED_S[1]
			clear = waiting < STALL_CLEAR * STALL_UNBOUNDED_S[0]
		if starting and not critical and key not in self.problems:
			return # its encoder is opening
		then = self.Since(c, SENT_WINDOW_S, now)
		if warn or (key in self.problems and not clear):
			taken = row["sent"] - then[3] if then is not None else 0
			span = now - then[0] if then is not None else 0
			line = "{}: {:.1f} s of frames waiting for ffmpeg{}; it took {} frames in the last {} ({:.0f} a second, of "\
				"{:g})".format(name, waiting,
				" ({:.0%} of writeQueueMaxSec {:g} s)".format(share, self.queueLimit) if self.queueLimit else "",
				taken, notify.Duration(span), taken / span if span > 0 else 0, self.frameRate)
			text = "{}. The disk or the encoder has stalled{}.".format(line,
				": frames are dropped once it reaches 100 %" if self.queueLimit else ", and memory fills")
			severity = notify.CRITICAL if critical else notify.WARNING if warn else self.problems[key].severity
			self.Raise(key, severity, text, hold=STALL_CLEAR_S, line=line)
		elif clear:
			self.Clear(key, "{}: caught up ({:.1f} s of frames waiting for ffmpeg)".format(name, waiting), now)

	# ------------------------------------------------------------------ the disk

	def Folders(self):
		# Each camera's video folder, once each
		folders = []
		cams = self.cams or []
		for cam in cams:
			f = cam.get("videoFolder")
			if f and f not in folders:
				folders.append(f)
		if not folders:
			f = self.folder or self.params.get("videoFolder")
			folders = [f] if f else []
		return folders

	def FolderCameras(self, folder, folders):
		# The cameras whose videos go to this folder (all of them, with only one folder)
		if len(folders) == 1:
			return list(range(len(self.names)))
		cams = {cam.get("cameraName"): cam for cam in self.cams or []}
		return [c for c, name in enumerate(self.names) if cams.get(name, {}).get("videoFolder", folders[0]) == folder]

	def CheckDisk(self, now):
		# Whether the disk lasts is counted in frames, not wall-clock time: bytes written per frame grabbed (over the
		# last DISK_WINDOW_S), times the frames still to record (recTimeInSec x frameRate each, less those grabbed).
		# With a trigger that runs only part of the time (bouts), the wall clock would say it lasts when it does not;
		# with planned pauses (alertTriggerPauseSec), counted over the whole recording (below).
		folders = self.Folders()
		for folder in folders:
			cameras = self.FolderCameras(folder, folders)
			grabbed = [self.counters.Row(c)["grabbed"] for c in cameras]
			frames = sum(grabbed)
			left = sum(max(0.0, self.recTime * self.frameRate - g) for g in grabbed) if self.recTime else 0.0
			path = folder
			while path and not os.path.isdir(path) and os.path.dirname(path) != path:
				path = os.path.dirname(path)
			free = float(self.disk_usage(path).free)
			history = self.disks[folder]
			history.append((now, free, frames))
			while history and history[0][0] < now - DISK_WINDOW_S:
				history.popleft()
			where = "the disk with {}".format(folder)
			if self.stopAt > 0 and free < self.stopAt and self.stopped is None:
				self.stopped = "only {} free on {} (stopAtFreeGB {:g})".format(DiskText(free), where, self.stopAt / 1e9)
				self.Raise("disk/stop", notify.CRITICAL, "Only {} free on {}: stopping the recording now, cleanly, so "
					"that every video and its metadata are whole (stopAtFreeGB {:g}).".format(DiskText(free), where,
					self.stopAt / 1e9), remind=False)
				if self.request_stop is not None:
					self.request_stop(self.stopped)
				continue
			key = "disk/{}".format(folder)
			if free < self.diskFree:
				self.Raise(key + "/low", notify.WARNING, "Only {} free on {} (alertDiskFreeGB {:g}).".format(
					DiskText(free), where, self.diskFree / 1e9))
			elif free > 1.1 * self.diskFree:
				self.Clear(key + "/low", "{} free on {}".format(DiskText(free), where), now)
			first = self.diskFirst.setdefault(folder, (now, free, frames))
			# With planned trigger pauses (bouts: alertTriggerPauseSec), over the whole recording, not the last
			# DISK_WINDOW_S: other programs fill the disk between the cameras' frames (SpikeGLX while the gate is
			# open, before and after them), and a few minutes' bytes over their frames swing with the kind of bout
			# (on the ChronicRecord rig, 150 to 346 MB/s for about 60 on average: "fills" alerts all night, wrongly).
			# The bouts' plan repeats, so the whole recording's average counts them in the right proportion.
			t0, f0, n0 = first if self.plannedPause is not None else history[0]
			rate = (f0 - free) / (now - t0) if now - t0 >= DISK_RATE_MIN_S else None
			full = None
			if rate is not None and rate > 0:
				full = (datetime.datetime.now().astimezone() + datetime.timedelta(seconds=free / rate)).isoformat(
					timespec="seconds")
			self.diskState[folder] = {"path": folder, "free_gb": round(free / 1e9, 2), "projected_full_at": full}
			if rate is None or not left or frames <= n0 or not cameras:
				continue # (no frame in the window: between bouts, the rate per frame is not known)
			perFrame = (f0 - free) / (frames - n0)
			perSecond = self.frameRate * len(cameras) # frames a second of recording, every camera on this disk
			toStop = (free - self.stopAt) / perFrame / perSecond if perFrame > 0 else float("inf") # seconds of recording
			left /= perSecond
			if toStop < left:
				self.Raise(key + "/full", notify.CRITICAL, "{} fills at {:.0f} MB/s while the cameras record: down to "
					"{} free after {} more of recording, {} of recording before it ends.{}".format(
					where[0].upper() + where[1:], perFrame * perSecond / 1e6, DiskText(self.stopAt),
					notify.Duration(toStop), notify.Duration(left - toStop),
					" campy will then stop the recording, cleanly." if self.stopAt > 0 else ""))
			elif toStop > 1.1 * left + 60:
				self.Clear(key + "/full", "{} will last to the end of the recording ({} free)".format(
					where[0].upper() + where[1:], DiskText(free)), now)

	# ------------------------------------------------------------------ the trigger and USB

	def CheckSerial(self, now):
		connection = self.systems.get("serial")
		if connection is None:
			return
		port = str(self.params.get("serialPort"))
		why = None
		if not self.serialClosed:
			try:
				connection.in_waiting # asks the driver how much it has read: fails once the device is gone
			except Exception as e:
				why = ErrorText(e)
		gone = self.port_gone(port)
		if gone and not self.serialClosed:
			# The device is gone: let go of it. On Linux a port still held open keeps its name, so a replug came
			# back as the next one (/dev/ttyACM1, on the EVO-X2): neither this check nor the next recording would
			# find it. The trigger is not restarted, and campy's own stop at the end finds the port closed.
			self.serialClosed = True
			try:
				connection.close()
			except Exception as e:
				self.say("Watch: could not close the lost trigger port {}: {}".format(port, ErrorText(e)))
		why = why or gone
		if why:
			self.Raise("trigger/serial", notify.CRITICAL, "The trigger Arduino on {} is gone ({}): it sends no more "
				"trigger pulses, so the cameras get no frames. Replug it; campy does not restart the trigger "
				"mid-recording.".format(port, why), remind=False)
		elif self.serialClosed:
			self.Clear("trigger/serial", "The trigger Arduino's port {} is back. campy does not restart the trigger: "
				"the cameras get frames again in the next recording.".format(port), now)
		else:
			self.Clear("trigger/serial", None, now)

	def CheckUsb(self, now):
		if not self.cams:
			return
		from campy import preflight
		devices = preflight.UsbDevices(self.sysfs_root) # (Windows: an OSError is said once, as the check's error)
		if devices is None:
			return
		for cam in self.cams:
			if cam.get("cameraMake") not in preflight.USB3_MAKES or cam.get("cameraSerialNo") is None:
				continue
			device = preflight.FindCameraDevice(devices, cam)
			if device is None or device["speed"] is None:
				continue # not on the bus: unplugged, which the camera says itself; or its hub could not say
			key = "{}/usb".format(cam["cameraName"])
			self.usbSpeed[cam["cameraName"]] = device["speed"]
			if device["speed"] < 5000:
				self.Raise(key, notify.CRITICAL, "{} is now on a USB 2 link ({:g} Mb/s), not USB 3: it can't carry its "
					"frames, so expect lost frames. Check its cable and port.".format(cam["cameraName"],
					device["speed"]))
			else:
				self.Clear(key, "{} is back on USB 3 ({:g} Mb/s)".format(cam["cameraName"], device["speed"]), now)

	# ------------------------------------------------------------------ the GPU's kernel log

	def CheckGpu(self, now):
		if self.gpuLog is None:
			return
		for note, logged in self.gpuLog.Notes():
			self.say(note)
			if logged:
				self.notifier.Note(note) # info: alerts.log, and channels that take info
		new = collections.Counter()
		for kind, ring, counted, line in self.gpuLog.Take():
			new[kind] += 1
			self.gpuLast[kind] = line
			if counted:
				self.gpuEvents[kind].append((now, ring))
				self.gpuCounts[kind] += 1
				if ring:
					self.gpuRings[ring] += 1
			elif not self.gpuEvents[kind]: # a reset's first line: open it, though its outcome's line counts it
				self.gpuEvents[kind].append((now, None))
		for events in self.gpuEvents.values():
			while events and events[0][0] < now - GPU_WINDOW_S:
				events.popleft()
		stalls = self.gpuEvents[gpulog.STALL]
		if len(stalls) >= self.gpuStalls:
			self.Raise("gpu/stalls", notify.WARNING, "GPU: {} firmware stalls in the last {} ({} in the last minute), "
				"on {}. Each held that engine up for about half a second; on a camera's encoder it shows as frames "
				"waiting for ffmpeg, and in campy-live as a late set.{} Last: {}".format(len(stalls),
				notify.Duration(GPU_WINDOW_S), sum(1 for t, _ in stalls if t >= now - 60), self.RingsText(stalls),
				" Is vaapiLinearSurfaces off?" if not self.params.get("vaapiLinearSurfaces", True) else "",
				self.gpuLast[gpulog.STALL]))
		elif not stalls:
			self.Clear("gpu/stalls", "GPU: no firmware stall for {} ({} in all)".format(
				notify.Duration(GPU_WINDOW_S), self.gpuCounts[gpulog.STALL]), now)
		hangs = self.gpuEvents[gpulog.HANG]
		if new[gpulog.HANG]:
			self.Raise("gpu/hang", notify.WARNING, "GPU: a job on the GPU hung ({} in the last {}{}): the encoders or "
				"campy-live on it waited. A GPU reset may follow. Last: {}".format(len(hangs),
				notify.Duration(GPU_WINDOW_S), ", on " + self.RingsText(hangs) if any(r for _, r in hangs) else "",
				self.gpuLast[gpulog.HANG]))
		elif not hangs:
			self.Clear("gpu/hang", "GPU: no hung GPU job for {}".format(notify.Duration(GPU_WINDOW_S)), now)
		resets = self.gpuEvents[gpulog.RESET]
		if new[gpulog.RESET]:
			self.Raise("gpu/reset", notify.CRITICAL, "GPU: the driver reset {}. A reset ends what was running there: "
				"expect the cameras' VA-API encoders (ffmpeg) or campy-live to fail; the camera alerts say which. "
				"Last: {}".format("the GPU's " + self.RingsText(resets) if any(r for _, r in resets) else "the GPU",
				self.gpuLast[gpulog.RESET]))
		elif not resets:
			self.Clear("gpu/reset", "GPU: no GPU reset for {} ({} in all)".format(notify.Duration(GPU_WINDOW_S),
				self.gpuCounts[gpulog.RESET]), now)

	@staticmethod
	def RingsText(events):
		# "comp_1.0.0 (compute: ...) x4, vcn_unified_0 (video encoder: ...) x1", most first
		rings = collections.Counter(ring for _, ring in events if ring)
		return ", ".join("{} \u00d7{}".format(gpulog.RingText(ring), n) for ring, n in rings.most_common()) or \
			"the GPU"

	def GpuStatus(self):
		if self.gpuLog is None:
			return None
		stalls = self.gpuEvents.get(gpulog.STALL) or ()
		now = self.clock()
		return {"read_with": self.gpuLog.source, "not_read": None if self.gpuLog.source else self.gpuLog.why,
			"stalls": self.gpuCounts[gpulog.STALL], "hangs": self.gpuCounts[gpulog.HANG],
			"resets": self.gpuCounts[gpulog.RESET], "stalls_last_minute": sum(1 for t, _ in stalls if t >= now - 60),
			"rings": dict(self.gpuRings)}

	def GpuText(self):
		# The end summary's line, when the GPU logged anything
		if self.gpuLog is None or not sum(self.gpuCounts.values()):
			return None
		parts = []
		for kind in (gpulog.STALL, gpulog.HANG, gpulog.RESET):
			n = self.gpuCounts[kind]
			if n or kind == gpulog.RESET:
				parts.append("{} {}{}".format(n or "no", GPU_KINDS[kind], "" if n <= 1 else "s"))
		rings = ", ".join("{} \u00d7{}".format(ring, n) for ring, n in self.gpuRings.most_common())
		return "GPU kernel log: {}{}".format(", ".join(parts), " ({})".format(rings) if rings else "")

	# ------------------------------------------------------------------ recording.json

	def Status(self, state):
		now = self.clock()
		cameras = []
		cams = {cam.get("cameraName"): cam for cam in self.cams or []}
		for c, name in enumerate(self.names):
			row = self.counters.Row(c)
			history = self.history.get(c)
			fps = None
			if history and len(history) >= 2:
				then = self.Since(c, 5.0, now)
				if then is not None and history[-1][0] > then[0]:
					fps = round((history[-1][1] - then[1]) / (history[-1][0] - then[0]), 2)
			cam = cams.get(name, {})
			cameras.append({"name": name, "serial": cam.get("cameraSerialNo"), "make": cam.get("cameraMake"),
				"fps_target": self.frameRate, "fps_measured": fps, "frames": row["grabbed"], "dropped": row["dropped"],
				"write_queue_s": round(row["queued"] / self.frameRate, 2), "usb_speed": self.usbSpeed.get(name),
				"state": STATE_NAMES.get(row["state"], str(row["state"])), "ffmpeg_restarts": row["restarts"],
				"frames_lost_in_encoder": row["lost"], "trigger_pulses_without_frame": row["missed"]})
		return {"schema": STATUS_SCHEMA, "updated_at": Wall(), "host_model": self.notifier.source or None,
			"campy_version": CampyVersion(), "state": state,
			"session": {"folder": self.folder, "started_at": self.startedAt,
				"elapsed_s": round(now - self.started, 1) if self.started is not None else None,
				"planned_s": self.recTime},
			"cameras": cameras, "disk": list(self.diskState.values()),
			"open": [{"key": k, "severity": p.severity, "text": p.text} for k, p in self.problems.items()],
			"last_error": self.lastError, "gpu_log": self.GpuStatus(),
			"trigger_pauses": sorted([{"pulses": n, "seconds": round(n / self.frameRate, 3), "before_frame": f,
				"planned": planned} for pauses, planned in ((self.pauses, False), (self.planned, True))
				for n, f in pauses], key=lambda p: p["before_frame"])}

	def WriteStatus(self, state):
		try:
			path = StatusPath()
			os.makedirs(os.path.dirname(path), exist_ok=True)
			with open(path + ".tmp", "w", encoding="utf-8") as f:
				strictjson.dump(self.Status(state), f, indent=1)
			os.replace(path + ".tmp", path)
		except Exception as e:
			if "status" not in self.errors:
				self.errors.add("status")
				self.say("Watch: could not write {}: {}".format(StatusPath(), ErrorText(e)))

	# ------------------------------------------------------------------ start and end

	def Session(self):
		return os.path.basename(os.path.normpath(self.folder)) if self.folder else "the recording"

	def Begun(self):
		frees = []
		for folder in self.Folders():
			try:
				path = folder
				while path and not os.path.isdir(path) and os.path.dirname(path) != path:
					path = os.path.dirname(path)
				frees.append(DiskText(self.disk_usage(path).free))
			except Exception:
				pass
		self.notifier.Note("Recording {} started: {} camera{} ({}) at {:g} fps, for {}{}. {} free.".format(
			self.Session(), len(self.names), "" if len(self.names) == 1 else "s", ", ".join(self.names), self.frameRate,
			notify.Duration(self.recTime), self.TriggerNote(),
			" and ".join(frees) or "Unknown space"))
		self.notifier.Heartbeat()

	def StillOpen(self, severity=notify.WARNING):
		# The problems open as the recording ends, at least this bad, oldest first. A camera that ended is left
		# out (Ended says it on that camera's line), and so is the disk stop (its first line), and so are trigger
		# pauses and missed pulses (lines of their own). A recording stopped (Ctrl-C, its stop file) while a camera
		# had no frames, or with the trigger unplugged, ends as asked, but did not record what it should have.
		return [p for key, p in self.problems.items() if p.clearSince is None and not key.endswith("/ended") and
			not key.endswith("/missed") and key not in ("disk/stop", "trigger/paused") and
			notify.LEVELS[p.severity] >= notify.LEVELS[severity]]

	def Ended(self, failures, error=None):
		lines = []
		dropped = restarts = 0
		rows = [self.counters.Row(c) for c in range(len(self.names))]
		try:
			self.CheckMissed(rows, camstatus.Now(), self.clock(), final=True) # in its last seconds
		except Exception as e:
			self.say("Watch: could not count the trigger's pulses without a frame: {}".format(ErrorText(e)))
		for c, name in enumerate(self.names):
			row = rows[c]
			self.CheckRestarts(c, name, row) # one in its last seconds
			dropped += row["dropped"]
			restarts += row["restarts"]
			text = "{}: {} frames".format(name, row["grabbed"])
			if row["dropped"]:
				text += ", {} dropped".format(row["dropped"])
			if row["restarts"]:
				text += ", in {} video files: {} ffmpeg restart{}, {} frame{} lost".format(row["restarts"] + 1,
					row["restarts"], "" if row["restarts"] == 1 else "s", row["lost"], "" if row["lost"] == 1 else "s")
			if self.missedAlone[c]:
				text += ", no frame for {} trigger pulse{} the other cameras got".format(self.missedAlone[c],
					"" if self.missedAlone[c] == 1 else "s") if len(self.names) > 1 else \
					", no frame for {} trigger pulse{}".format(self.missedAlone[c], "" if self.missedAlone[c] == 1 else "s")
			if name in failures:
				text += ": " + "; ".join(failures[name])
			lines.append("- " + text)
		pauses = self.PausesText()
		if pauses:
			lines.append("- " + pauses)
		planned = self.PlannedText()
		if planned:
			lines.append("- " + planned)
		took = notify.Duration(self.clock() - self.started) if self.started is not None else "?"
		gpu = self.GpuText()
		if gpu:
			lines.append("- " + gpu)
		still = self.StillOpen()
		if still:
			lines += ["Still open as it ended:"] + ["- " + p.text for p in still]
		if self.stopped:
			head, severity = "Recording {} stopped by campy after {}: {}.".format(self.Session(), took, self.stopped), \
				notify.CRITICAL
		elif error:
			# An error ended campy-acquire's recording (it stopped waiting for its cameras): not a recording that
			# ended as planned, whatever the cameras' counts say
			head, severity = "Recording {} ended after {} with an error in campy-acquire: {}. Its log says more.".format(
				self.Session(), took, error.rstrip(".")), notify.CRITICAL
		elif failures:
			head, severity = "Recording {} ended after {}, {} of {} cameras not to the end:".format(self.Session(), took,
				len(failures), len(self.names)), notify.CRITICAL
		elif self.StillOpen(notify.CRITICAL):
			head, severity = "Recording {} ended after {}, with problems still open:".format(self.Session(), took), \
				notify.CRITICAL
		elif dropped:
			head, severity = "Recording {} ended after {}, with dropped frames:".format(self.Session(), took), \
				notify.WARNING
		elif restarts:
			head, severity = "Recording {} ended after {}, with ffmpeg restarted:".format(self.Session(), took), \
				notify.WARNING
		elif self.pauses:
			head, severity = "Recording {} ended after {}, with the trigger paused:".format(self.Session(), took), \
				notify.WARNING
		elif any(self.missedAlone.values()):
			head, severity = "Recording {} ended after {}, with trigger pulses missed:".format(self.Session(), took), \
				notify.WARNING
		elif still:
			head, severity = "Recording {} ended after {}, with problems still open:".format(self.Session(), took), \
				notify.WARNING
		else:
			head, severity = "Recording {} ended after {}:".format(self.Session(), took), notify.INFO
		self.notifier.Note("\n".join([head] + lines), severity, urgent=True) # ahead of the messages still waiting


def PerCamera(value, c):
	# A setting a config can give one a camera (a list) or one for all
	if isinstance(value, (list, tuple)):
		return value[c] if c < len(value) else (value[-1] if value else None)
	return value


def Wall():
	# Now, as ISO time with its UTC offset
	return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def StatusPath():
	folder = os.environ.get(STATUS_ENV) or os.path.join(os.path.expanduser("~"), ".campy", "status")
	return os.path.join(folder, "recording.json")


def CampyVersion():
	try:
		from importlib import metadata
		return metadata.version("campy")
	except Exception:
		return None


def PortGone(port):
	# Why the serial port is gone, or None: its device file (Linux, macOS), or Windows's list of ports
	if sys.platform == "win32":
		try:
			from serial.tools import list_ports
			if port.upper() not in [p.device.upper() for p in list_ports.comports()]:
				return "{} is no longer among Windows's ports".format(port)
		except Exception:
			return None
		return None
	if port.startswith("/dev/") and not os.path.exists(port):
		return "{} is gone".format(port)
	return None
