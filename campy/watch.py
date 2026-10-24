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
                   alert, the trigger has stopped
  first frame      no frame 10 s after the trigger Arduino started (an
                   external trigger: a warning after 10 min of waiting)
  dropped frames   frames dropped in the last minute, because more than
                   writeQueueMaxSec of frames waited for ffmpeg: a warning,
                   critical above 1 % of them; resolved after 2 min without
  write stall      the frames waiting for ffmpeg reach a quarter of
                   writeQueueMaxSec (a warning) or three quarters (critical):
                   the disk or the encoder has stalled, and frames are
                   dropped once it is full. The EVO-X2's SSD stopped taking
                   writes for 10-16 s at a time
  camera ended     ffmpeg stopped, the camera is gone (unplugged), its
                   thread or its process died: critical, no reminders
  disk             free space on each video folder's disk, every 10 s: a
                   warning below alertDiskFreeGB (20 GB); critical when, at
                   the rate it has been filling over the last 5 min, it
                   would be down to stopAtFreeGB before the recording ends;
                   and below stopAtFreeGB (2 GB), the recording is stopped,
                   cleanly, as at Ctrl-C, so that every video, frametimes.npy
                   and metadata.csv is whole (a full disk stops every ffmpeg
                   at once, and leaves no room for them). stopAtFreeGB 0:
                   never stopped
  trigger port     the trigger Arduino's serial port gone (unplugged, or it
                   reset), every 5 s, without sending it anything
  USB              Linux: a USB 3 camera on a USB 2 link, every 60 s
                   (preflight's sysfs reading; not on Windows or macOS)

and says when the recording started and how it ended. With a healthchecks.io
check in the alerts file, it pings it every minute and pauses it at the end
(notify.Notifier.Heartbeat).

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
               dropped, write_queue_s, usb_speed (Mb/s, Linux), state}]
  disk         [{path, free_gb, projected_full_at (ISO, or null)}]
  open         [{key, severity, text}]: the problems open now
  last_error   {time, text} of the last critical problem, or null

written whole each time (a .tmp file, then replaced), by campy-acquire only.

Nothing here acts on the recording but the stop at stopAtFreeGB: the watch
only reads. An error in a check is said, and the others go on.
"""

import collections, datetime, json, math, os, shutil, sys, threading, time

from campy import notify, status as camstatus
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
FIRST_FRAME_S = 10.0 # after the trigger Arduino started
EXTERNAL_WAIT_S = 600.0 # an external trigger (startArduino False): waiting this long is said
TOGETHER_S = 1.0 # cameras whose last frames are this close stopped together: the trigger
SENT_WINDOW_S = 10.0
DiskUsage = shutil.disk_usage # (tests replace it: a disk that fills)
STATUS_EVERY_S = 4.0 # recording.json, written this often
STATUS_SCHEMA = 1
STATUS_ENV = "CAMPY_STATUS_DIR" # where recording.json goes, instead of ~/.campy/status


def DiskText(n):
	# Rounded down: 1.996 GB, below a 2 GB stopAtFreeGB, is "1.99 GB", not "2.00 GB"
	p = 2 if n < 10e9 else 1
	return "{:.{p}f} GB".format(math.floor(n / 1e9 * 10 ** p) / 10 ** p, p=p)


STATE_NAMES = {camstatus.WAITING: "waiting", camstatus.GRABBING: "grabbing", camstatus.ENDED: "ended",
	camstatus.NOT_STARTED: "not started", camstatus.LOST: "lost", camstatus.VIDEO_FAILED: "video failed",
	camstatus.CAMERA_FAILED: "camera failed"}


class Problem:
	def __init__(self, severity, text, hold, remind):
		self.severity, self.text, self.hold, self.remind = severity, text, hold, remind
		self.clearSince = None


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
			port_gone=None):
		self.params, self.systems, self.cameras = params, systems, cameras
		self.names = list(cameras.names)
		self.counters = camstatus.Reader(counters, len(self.names))
		self.folder = folder
		self.clock, self.sysfs_root, self.say = clock, sysfs_root, say
		self.disk_usage = disk_usage or (lambda path: DiskUsage(path))
		self.stopping = stopping or (lambda: False)
		self.request_stop = request_stop # stops the recording as Ctrl-C does (campy.Main's StopRequested)
		self.port_gone = port_gone or PortGone # (tests: a port that is there)
		if notifier is None:
			if params.get("alerts", True):
				notifier = notify.Notifier(say=say)
			else:
				notifier = notify.Notifier(channels=[], say=say)
		self.notifier = notifier
		if folder:
			self.notifier.SetLog(os.path.join(folder, "alerts.log"))
		self.cams = cams
		self.frameRate = float(params["frameRate"])
		self.noFramesSec = float(params.get("alertNoFramesSec", 10))
		self.diskFree = float(params.get("alertDiskFreeGB", 20)) * 1e9
		self.stopAt = float(params.get("stopAtFreeGB", 2)) * 1e9
		limit = params.get("writeQueueMaxSec", "None")
		self.queueLimit = None if limit is None or str(limit) == "None" else float(limit)
		self.recTime = float(params.get("recTimeInSec", 0))
		self.external = "serial" not in systems
		self.problems = {} # key: Problem, open
		self.history = collections.defaultdict(collections.deque) # camera: (t, grabbed, dropped, sent)
		self.disks = collections.defaultdict(collections.deque) # folder: (t, free)
		self.at = collections.defaultdict(lambda: None) # what ran when: "disk", "serial", "usb"
		self.started = None
		self.startedAt = None # wall time, for recording.json
		self.stopped = None # why the watch stopped the recording, if it did
		self.diskState = {} # folder: {path, free_gb, projected_full_at}, for recording.json
		self.usbSpeed = {} # camera: its link's Mb/s, as last read
		self.serialClosed = False # the trigger's port was gone, and its connection closed (CheckSerial)
		self.lastError = None
		self.errors = set()
		self.done = threading.Event()
		self.thread = None

	# ------------------------------------------------------------------ the thread

	def Start(self):
		self.started = self.clock()
		self.startedAt = Wall()
		self.Begun()
		self.WriteStatus("recording")
		self.thread = threading.Thread(target=self.Run, name="campy watch", daemon=True)
		self.thread.start()
		return self

	def Run(self):
		while not self.done.wait(EVERY_S):
			self.Check()

	def Stop(self, failures=None, timeout=15.0):
		"""Ends the watch, says how the recording ended, and waits (up to timeout s) for the alerts to go."""
		self.done.set()
		if self.thread is not None:
			self.thread.join(5)
		try:
			self.Ended(failures or {})
		except Exception as e:
			self.say("Watch: could not say how the recording ended: {}".format(ErrorText(e)))
		self.WriteStatus("failed" if failures or self.stopped or self.StillOpen(notify.CRITICAL) else "ended")
		self.notifier.Pause()
		self.notifier.Flush(timeout)

	def Check(self, now=None):
		"""Every check, once (the thread calls it every EVERY_S)."""
		now = self.clock() if now is None else now
		for name, check, every in (("cameras", self.CheckCameras, 0), ("disk", self.CheckDisk, DISK_EVERY_S),
				("serial", self.CheckSerial, SERIAL_EVERY_S), ("usb", self.CheckUsb, USB_EVERY_S),
				("status", lambda now: self.WriteStatus("stopping" if self.stopping() else "recording"),
				STATUS_EVERY_S)):
			if every and self.at[name] is not None and now - self.at[name] < every:
				continue
			self.at[name] = now
			try:
				check(now)
			except Exception as e:
				if name not in self.errors: # said once
					self.errors.add(name)
					self.say("Watch: the {} check failed: {}".format(name, ErrorText(e)))
		self.Settle(now)
		self.notifier.Heartbeat()
		self.notifier.Tick()

	# ------------------------------------------------------------------ problems: raised, cleared

	def Raise(self, key, severity, text, hold=0.0, remind=True):
		problem = self.problems.get(key)
		if severity == notify.CRITICAL:
			self.lastError = {"time": Wall(), "text": text}
		if problem is None:
			self.problems[key] = Problem(severity, text, hold, remind)
			self.say("Alert: {}".format(text))
		else:
			if notify.LEVELS[severity] > notify.LEVELS[problem.severity]:
				self.say("Alert, worse: {}".format(text))
			problem.severity, problem.text, problem.clearSince = severity, text, None
		self.notifier.Problem(key, text, severity, remind=remind)

	def Clear(self, key, text=None, now=None):
		problem = self.problems.get(key)
		if problem is not None and problem.clearSince is None:
			problem.clearSince = (self.clock() if now is None else now, text)

	def Settle(self, now):
		# Problems clear for their hold: resolved
		for key, problem in list(self.problems.items()):
			if problem.clearSince is not None and now - problem.clearSince[0] >= problem.hold:
				del self.problems[key]
				text = problem.clearSince[1]
				self.say("Alert resolved: {}".format(text or problem.text))
				self.notifier.Resolved(key, text)

	def Open(self):
		return dict(self.problems)

	# ------------------------------------------------------------------ the cameras

	def CheckCameras(self, now):
		if self.stopping():
			return # Ctrl-C: the cameras are ending, as asked
		ns = camstatus.Now()
		rows = [self.counters.Row(c) for c in range(len(self.names))]
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

	def CheckQuiet(self, rows, gaps, now):
		# Cameras with no frame for noFramesSec. When every camera stopped together (their last frames within
		# TOGETHER_S), one alert: the trigger. Until the last of them is quiet too, none is said on its own.
		quiet = [c for c, (gap, _) in gaps.items() if gap > self.noFramesSec]
		nearly = [c for c, (gap, _) in gaps.items() if gap > self.noFramesSec - TOGETHER_S - EVERY_S]
		lasts = [gaps[c][1] for c in nearly]
		together = len(gaps) >= 2 and len(nearly) == len(gaps) and (max(lasts) - min(lasts)) / 1e9 <= TOGETHER_S
		if together and len(quiet) == len(gaps):
			self.Raise("trigger/stopped", notify.CRITICAL, "No camera has had a frame for {:.0f} s: all {} stopped "
				"together, so the trigger has stopped ({}).".format(min(gaps[c][0] for c in quiet), len(gaps),
				self.TriggerText()))
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
			if self.external and waited >= EXTERNAL_WAIT_S:
				self.Raise("trigger/first", notify.WARNING, "No frame yet, {} after campy started waiting for the "
					"external trigger: is it running?".format(notify.Duration(waited)))
			elif not self.external and waited >= FIRST_FRAME_S:
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
			self.Raise(key, severity, "{}: dropped {} frames in the last {} ({} since the start): more than "
				"writeQueueMaxSec ({}) of frames were waiting for ffmpeg.".format(name, dropped,
				notify.Duration(now - then[0]), row["dropped"], self.QueueLimitText()), hold=DROPS_CLEAR_S)
		elif row["dropped"]:
			self.Clear(key, "{}: no frame dropped for {} ({} in all)".format(name, notify.Duration(DROPS_CLEAR_S),
				row["dropped"]), now)

	def QueueLimitText(self):
		return "{:g} s".format(self.queueLimit) if self.queueLimit is not None else "unbounded"

	def CheckQueue(self, c, name, row, now, quiet):
		key = "{}/queue".format(name)
		waiting = row["queued"] / self.frameRate # seconds of frames waiting for ffmpeg
		if quiet:
			return # no frame lately: its count of frames waiting is old
		if self.queueLimit:
			share = waiting / self.queueLimit
			warn, critical, clear = share >= STALL_WARNING, share >= STALL_CRITICAL, share < STALL_CLEAR
		else:
			warn, critical = waiting >= STALL_UNBOUNDED_S[0], waiting >= STALL_UNBOUNDED_S[1]
			clear = waiting < STALL_CLEAR * STALL_UNBOUNDED_S[0]
		then = self.Since(c, SENT_WINDOW_S, now)
		if warn or (key in self.problems and not clear):
			taken = row["sent"] - then[3] if then is not None else 0
			span = now - then[0] if then is not None else 0
			text = "{}: {:.1f} s of frames waiting for ffmpeg{}; it took {} frames in the last {} ({:.0f} a second, of "\
				"{:g}). The disk or the encoder has stalled{}.".format(name, waiting,
				" ({:.0%} of writeQueueMaxSec {:g} s)".format(share, self.queueLimit) if self.queueLimit else "",
				taken, notify.Duration(span), taken / span if span > 0 else 0, self.frameRate,
				": frames are dropped once it reaches 100 %" if self.queueLimit else ", and memory fills")
			severity = notify.CRITICAL if critical else notify.WARNING if warn else self.problems[key].severity
			self.Raise(key, severity, text, hold=STALL_CLEAR_S)
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

	def CheckDisk(self, now):
		left = max(0.0, self.recTime - (now - self.started)) if self.started is not None and self.recTime else 0.0
		for folder in self.Folders():
			path = folder
			while path and not os.path.isdir(path) and os.path.dirname(path) != path:
				path = os.path.dirname(path)
			free = float(self.disk_usage(path).free)
			history = self.disks[folder]
			history.append((now, free))
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
			t0, f0 = history[0]
			rate = (f0 - free) / (now - t0) if now - t0 >= DISK_RATE_MIN_S else None
			full = None
			if rate is not None and rate > 0:
				full = (datetime.datetime.now().astimezone() + datetime.timedelta(seconds=free / rate)).isoformat(
					timespec="seconds")
			self.diskState[folder] = {"path": folder, "free_gb": round(free / 1e9, 2), "projected_full_at": full}
			if rate is None or not left:
				continue
			toStop = (free - self.stopAt) / rate if rate > 0 else float("inf")
			if toStop < left:
				self.Raise(key + "/full", notify.CRITICAL, "{} fills at {:.0f} MB/s: down to {} free in {}, {} before "
					"the recording ends.{}".format(where[0].upper() + where[1:], rate / 1e6, DiskText(self.stopAt),
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
		if not os.path.isdir(os.path.join(self.sysfs_root, "bus", "usb", "devices")):
			return
		devices = preflight.ListUsbDevices(self.sysfs_root)
		for cam in self.cams:
			if cam.get("cameraMake") not in preflight.USB3_MAKES or cam.get("cameraSerialNo") is None:
				continue
			device = preflight.FindCameraDevice(devices, cam)
			if device is None:
				continue # not on the bus: unplugged, which the camera says itself
			key = "{}/usb".format(cam["cameraName"])
			self.usbSpeed[cam["cameraName"]] = device["speed"]
			if device["speed"] < 5000:
				self.Raise(key, notify.CRITICAL, "{} is now on a USB 2 link ({:g} Mb/s), not USB 3: it can't carry its "
					"frames, so expect lost frames. Check its cable and port.".format(cam["cameraName"],
					device["speed"]))
			else:
				self.Clear(key, "{} is back on USB 3 ({:g} Mb/s)".format(cam["cameraName"], device["speed"]), now)

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
				"state": STATE_NAMES.get(row["state"], str(row["state"]))})
		return {"schema": STATUS_SCHEMA, "updated_at": Wall(), "host_model": self.notifier.source or None,
			"campy_version": CampyVersion(), "state": state,
			"session": {"folder": self.folder, "started_at": self.startedAt,
				"elapsed_s": round(now - self.started, 1) if self.started is not None else None,
				"planned_s": self.recTime},
			"cameras": cameras, "disk": list(self.diskState.values()),
			"open": [{"key": k, "severity": p.severity, "text": p.text} for k, p in self.problems.items()],
			"last_error": self.lastError}

	def WriteStatus(self, state):
		try:
			path = StatusPath()
			os.makedirs(os.path.dirname(path), exist_ok=True)
			with open(path + ".tmp", "w", encoding="utf-8") as f:
				json.dump(self.Status(state), f, indent=1)
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
			notify.Duration(self.recTime), ", on an external trigger" if self.external else "",
			" and ".join(frees) or "Unknown space"))
		self.notifier.Heartbeat()

	def StillOpen(self, severity=notify.WARNING):
		# The problems open as the recording ends, at least this bad, oldest first. A camera that ended is left
		# out (Ended says it on that camera's line), and so is the disk stop (its first line). A recording stopped (Ctrl-C, its stop file) while a camera
		# had no frames, or with the trigger unplugged, ends as asked, but did not record what it should have.
		return [p for key, p in self.problems.items() if p.clearSince is None and not key.endswith("/ended") and
			key != "disk/stop" and notify.LEVELS[p.severity] >= notify.LEVELS[severity]]

	def Ended(self, failures):
		lines = []
		dropped = 0
		for c, name in enumerate(self.names):
			row = self.counters.Row(c)
			dropped += row["dropped"]
			text = "{}: {} frames".format(name, row["grabbed"])
			if row["dropped"]:
				text += ", {} dropped".format(row["dropped"])
			if name in failures:
				text += ": " + "; ".join(failures[name])
			lines.append("- " + text)
		took = notify.Duration(self.clock() - self.started) if self.started is not None else "?"
		still = self.StillOpen()
		if still:
			lines += ["Still open as it ended:"] + ["- " + p.text for p in still]
		if self.stopped:
			head, severity = "Recording {} stopped by campy after {}: {}.".format(self.Session(), took, self.stopped), \
				notify.CRITICAL
		elif failures:
			head, severity = "Recording {} ended after {}, {} of {} cameras not to the end:".format(self.Session(), took,
				len(failures), len(self.names)), notify.CRITICAL
		elif self.StillOpen(notify.CRITICAL):
			head, severity = "Recording {} ended after {}, with problems still open:".format(self.Session(), took), \
				notify.CRITICAL
		elif dropped:
			head, severity = "Recording {} ended after {}, with dropped frames:".format(self.Session(), took), \
				notify.WARNING
		elif still:
			head, severity = "Recording {} ended after {}, with problems still open:".format(self.Session(), took), \
				notify.WARNING
		else:
			head, severity = "Recording {} ended after {}:".format(self.Session(), took), notify.INFO
		self.notifier.Note("\n".join([head] + lines), severity)


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
