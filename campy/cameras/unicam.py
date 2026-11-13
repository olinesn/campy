"""
Unicam unifies camera APIs with common syntax to simplify multi-camera acquisition and 
reduce redundancy in campy code.
"""

import os, sys, time, csv, queue, logging, threading
import multiprocessing.connection
import numpy as np
from collections import deque
from scipy import io as sio
from campy import clock, frametimes, newfiles, livefeed, status
from campy.utils.utils import Say, ExitCodeText, ErrorText
from campy import startup, statusline


# Set in each camera process (campy.InitCameraProcess): where it reports that its
# camera is ready for triggers, so campy starts the trigger only after all are.
_readyQueue = None


def SetReadyQueue(readyQueue):
	global _readyQueue
	_readyQueue = readyQueue


# Set in each camera process (campy.RunCamera): its end of the pipe to campy's
# main process, which reads it as CameraProcesses
_mainConnection = None


def SetMainConnection(connection):
	global _mainConnection
	_mainConnection = connection


# Set in each camera process (campy.RunCamera): a shared number that campy's
# main process sets when a stop file asks for a stop (campy/stopfile.py). The
# camera then stops as at Ctrl-C, which a program that started campy without
# a console could not send it. Read once per frame: a plain shared int, no lock.
_stopRequest = None


def SetStopRequest(value):
	global _stopRequest
	_stopRequest = value


def StopRequested():
	return _stopRequest is not None and _stopRequest.value != 0


def TellMain(message):
	# A (kind, value) message for campy's main process: ("ffmpeg", pid or
	# None), ("done", what went wrong). Nothing outside a camera process.
	if _mainConnection is not None:
		try:
			_mainConnection.send(message)
		except Exception as e:
			logging.error("Caught exception at cameras/unicam.py TellMain: {}".format(e))


def SignalReady(cam_params, ok=True, error=""):
	# Tell campy's main process this camera is grabbing, or that it never will.
	# One that never will says what it did as it opened (startup.Failed).
	if not ok:
		startup.Failed(cam_params, error)
	if _readyQueue is not None:
		_readyQueue.put((cam_params["n_cam"], cam_params["cameraName"], ok, error))


def WaitForCameras(readyQueue, params, result=None, log=Say, interrupted=None):
	"""
	Waits until every camera has reported ready for triggers, or failed, for at
	most readyTimeoutSec, and stops early if the camera processes end (result:
	CameraProcesses, or anything with ready()), or campy is stopped
	(interrupted(): Ctrl-C). A camera whose process ends before it is ready
	(killed, or crashed as it opened) is not waited for. Returns the cameras
	that are ready, and a reason for each that is not.
	"""
	names = params["cameraNames"]
	pending = {n: names[n] for n in range(params["numCams"])}
	ready, missing = [], {}
	timeout = float(params["readyTimeoutSec"])
	deadline = time.monotonic() + timeout
	ended = getattr(result, "Ended", lambda n_cam: False)
	while pending:
		# Which processes had ended before this read: whatever they sent is in
		# the queue by then, so once it is empty they have nothing more to say
		gone = [n for n in pending if ended(n)]
		try:
			n_cam, name, ok, error = readyQueue.get(timeout=0.2)
		except InterruptedError:
			# Ctrl-C, on Windows: the queue's wait gives up when a signal comes
			# (CameraProcesses.Wait says why). Nothing was read: look again,
			# unless that was campy being stopped.
			if interrupted is not None and interrupted():
				break
			continue
		except queue.Empty:
			for n in gone:
				if n in pending:
					missing[pending.pop(n)] = "stopped before it was ready"
			if result is not None and result.ready():
				break
			if interrupted is not None and interrupted():
				break
			if time.monotonic() > deadline:
				break
			continue
		pending.pop(n_cam, None)
		if ok:
			ready.append(name)
		else:
			missing[name] = error or "did not start grabbing"
	stopped = result is not None and result.ready()
	for name in pending.values():
		if interrupted is not None and interrupted():
			missing[name] = "not ready when campy was stopped (Ctrl-C)"
		elif stopped:
			missing[name] = "stopped before it was ready"
		else:
			missing[name] = "not ready after {:g} s (readyTimeoutSec)".format(timeout)

	if not missing:
		log("{} ready.".format("The camera is" if len(ready) == 1 else "All {} cameras are".format(len(ready))))
	else:
		log("{} of {} cameras are ready. Not ready: {}.".format(len(ready), params["numCams"],
			"; ".join("{} ({})".format(name, why) for name, why in missing.items())))
	return ready, missing


class CameraProcesses:
	"""
	campy's camera processes, one per camera, as its main process watches
	them. Each runs target(n_cam, readyQueue, connection) (campy.RunCamera),
	and tells the main process on connection (TellMain) when its ffmpeg
	starts and ends, and, as its recording ends, what went wrong: a list of
	reasons, empty when nothing did. A process that ends without saying so
	died: killed, or crashed (a segfault in a camera driver, out of memory).
	The multiprocessing Pool campy used before replaced a worker that died
	and waited for its task forever; here it is reported as it happens, by
	name and exit code, while the other cameras go on recording.
	"""

	def __init__(self, ctx, target, params, readyQueue, log=Say, start=None, stop=None, counters=None):
		# start: given to each target too, after connection (campy.RunCamera:
		# the emulated cameras' shared start, cameras/emu.py), stop after it
		# (the stop file's request: SetStopRequest), and counters after that
		# (every camera's counters, for the recording watch: status.Make)
		self.names = [params["cameraNames"][n] for n in range(params["numCams"])]
		self.log = log
		self.readyQueue = readyQueue # kept while they start: they unpickle it as they do
		self.processes, self.connections = [], []
		self.failures = {} # camera name: what went wrong
		self.ended = set() # the cameras whose process has ended, and been reported
		self.done = {} # camera: what went wrong, as its process said when it ended
		self.ffmpeg = {} # camera: the pid of its ffmpeg while it runs
		self.stopped = set() # the cameras stopped at a second Ctrl-C (Stop)
		self.interrupted = False # Ctrl-C was pressed
		self.grace = 3.0 # how long a stopped process has to end before it is killed
		for n in range(params["numCams"]):
			receive, send = ctx.Pipe(duplex=False)
			# Daemon, as the pool's workers were: if campy's main process stops
			# with an error, they stop with it, rather than keep it waiting
			extra = [start, stop, counters]
			while extra and extra[-1] is None:
				extra.pop()
			args = (n, readyQueue, send) + tuple(extra)
			process = ctx.Process(target=target, args=args, name=self.names[n], daemon=True)
			process.start()
			send.close() # the camera process has its own copy
			self.processes.append(process)
			self.connections.append(receive)

	def ready(self):
		# Every camera process has ended (as the pool's AsyncResult.ready, for WaitForCameras)
		return not any(p.is_alive() for p in self.processes)

	def Ended(self, n_cam):
		return not self.processes[n_cam].is_alive()

	def Read(self, n_cam):
		# What the camera process has said so far. It sends before it ends, so
		# once it has ended, everything it said is in the pipe.
		connection = self.connections[n_cam]
		try:
			while connection.poll():
				kind, value = connection.recv()
				if kind == "ffmpeg":
					self.ffmpeg[n_cam] = value
				elif kind == "done":
					self.done[n_cam] = list(value)
		except (EOFError, OSError):
			pass
		except Exception as e:
			logging.error("Caught exception at cameras/unicam.py CameraProcesses.Read: {}".format(e))

	def Check(self):
		# Reports each camera process that has ended, or been stopped, since the last check
		for n, process in enumerate(self.processes):
			if n in self.ended or (process.is_alive() and n not in self.stopped):
				continue
			self.ended.add(n)
			if not process.is_alive():
				process.join()
			self.Read(n)
			reasons = self.done.get(n)
			if reasons is None:
				if n in self.stopped:
					why = "stopped at the second Ctrl-C, before it had closed"
				elif self.interrupted:
					why = "its camera process stopped, with {}, after Ctrl-C".format(ExitCodeText(process.exitcode))
				else:
					why = "its camera process stopped, with {}: killed, or crashed".format(ExitCodeText(process.exitcode))
				said = "{}: {}. Its files end where it stopped.".format(self.names[n], why)
				if process.is_alive():
					said += " Its process did not end even when killed (stuck in a camera driver?)."
				if self.ffmpeg.get(n):
					pid = self.ffmpeg[n]
					said += " If its ffmpeg (pid {}) is still running, it is finishing the video, or hangs: {} " \
						"ends it.".format(pid, "taskkill /F /PID {}".format(pid) if sys.platform == "win32" else
							"kill -9 {}".format(pid))
				self.log(said)
				reasons = [why]
			if reasons:
				self.failures[self.names[n]] = reasons

	def Stop(self):
		"""
		Ends the camera processes still running, at once: for a second Ctrl-C,
		when one hangs as it closes (in a camera driver, or waiting for an
		ffmpeg that does not exit). It does not close its camera or its video:
		its ffmpeg is left to finish the video from the frames it has, and a
		FLIR camera is put right at its next open (flir.RecoverAcquisition).
		"""
		alive = [n for n, p in enumerate(self.processes) if p.is_alive() and n not in self.stopped]
		if not alive:
			return
		self.log("Ctrl-C again: stopping {} now, without closing {}.".format(
			", ".join(self.names[n] for n in alive), "it" if len(alive) == 1 else "them"))
		for n in alive:
			self.stopped.add(n)
			self.processes[n].terminate() # SIGTERM; on Windows, TerminateProcess
		deadline = time.monotonic() + self.grace
		for n in alive:
			self.processes[n].join(max(0.0, deadline - time.monotonic()))
		for n in alive:
			if self.processes[n].is_alive():
				self.processes[n].kill() # SIGKILL, for one that ignores SIGTERM
				self.processes[n].join(self.grace)

	def Wait(self, interrupts=None, every=0.5):
		"""
		Waits for every camera process to end, and says when one dies, as it
		does. A second Ctrl-C (interrupts.count, utils.HandleKeyboardInterrupt)
		stops the ones still running (Stop). Returns {camera name: [what went
		wrong]} for each camera that did not record to the end.
		"""
		while True:
			if interrupts is not None:
				self.interrupted = interrupts.count >= 1
				if interrupts.count >= 2:
					self.Stop()
			self.Check()
			alive = [p.sentinel for n, p in enumerate(self.processes) if p.is_alive() and n not in self.stopped]
			if not alive:
				# One that ended since Check looked is reported too: cameras that
				# stop together (recTimeInSec, or none recording) end within
				# moments of each other, and one's failures were missed
				self.Check()
				return self.failures
			try:
				multiprocessing.connection.wait(alive, timeout=every)
			except InterruptedError:
				# Ctrl-C, on Windows. There this wait (_winapi.WaitForMultipleObjects)
				# also waits on Python's SIGINT event, and gives up when it is set:
				# once campy's handler has counted the press, it raises this, where
				# POSIX waits wait on (PEP 475). It used to end Main, and with it its
				# camera processes (daemon), as they closed their videos. Look again
				# instead, from the top: a second press stops them.
				pass


class CameraLost(Exception):
	"""
	A camera module's GrabFrame raises this when its camera is gone for good
	(unplugged, its link lost), rather than its usual error for no frame yet.
	GrabFrames then ends that camera's recording, where otherwise it would
	wait for frames that will never come.
	"""


class Recording:
	"""
	What a camera process's grab thread (GrabFrames) and its writer
	(writer.WriteFrames) tell each other, and what went wrong, for campy's
	main process (Failures, which campy.RunCamera sends it).
	"""

	def __init__(self):
		self.framesSent = 0 # frames the writer has given ffmpeg
		self.videoError = None # why the video stopped taking frames, or None
		self.cameraError = None # why the camera stopped before the end, or None
		self.frameTimesError = None # what frametimes.npy, hosttimes.npy or frametimes.mat lack (SaveMetadata), or None
		self.written = threading.Event() # the writer is done: everything below is final
		# The frame number of each frame in the write queue, in its order: the
		# camera thread adds a frame's number here before the frame, and the
		# writer takes it with the frame (segments.csv names frames by number)
		self.queuedFrames = deque()
		# frametimes.npy's rows, as the writer leaves them: keep the first
		# keepRows (None: all), and leave out lostRows (given to an ffmpeg
		# that stopped, but not in its file)
		self.keepRows = None
		self.lostRows = []
		self.framesLost = 0 # frames given to an ffmpeg that stopped, but not in its file
		self.restarts = 0 # times ffmpeg was restarted (restartFfmpeg)
		self.ffmpegRetries = 0 # times ffmpeg was started again on its empty file, its encoder not having opened (writer.FirstFrames)
		self.videoFiles = [] # the video's files, when it is in more than one

	def Failures(self):
		return [why for why in (self.cameraError, self.videoError, self.frameTimesError) if why]


# A camera that has sent frames and then sends none for this long (or for
# three frame periods, at a slower frame rate) is said to have stopped, and
# again this often while it lasts
NO_FRAME_SEC = 3
NO_FRAME_REPORT_SEC = 30


def PlannedPauses(cam_params):
	# alertTriggerPauseSec: the trigger pauses on purpose (bouts), and campy's recording watch says each pause once
	# (watch.py LogPlanned): None when it does not
	planned = cam_params.get("alertTriggerPauseSec", "None")
	return None if planned is None or str(planned) == "None" else float(planned)


class FrameWatch:
	"""
	Says when a camera stops sending frames, and when they come again. Only
	once it has sent its first: until then a triggered camera waits for its
	trigger, for as long as that takes.

	With alertTriggerPauseSec (a trigger that pauses on purpose), a gap every
	camera shares, their last frames together (status.Camera.QuietTogether,
	as campy's recording watch tells it), is the trigger's pause, not this
	camera's: it is not said here, but once for every camera by the
	recording watch ("Trigger: The trigger paused as planned ...", or a
	warning when it was shorter). A camera that stops while the others still
	get frames is said as before, and so is a camera left without frames once
	the others have them again.
	"""

	def __init__(self, cam_params, camera=None):
		self.name = cam_params["cameraName"]
		self.after = max(NO_FRAME_SEC, 3.0 / float(cam_params["frameRate"]))
		self.last = None # when the last frame came (time.monotonic)
		self.frameNumber = 0 # and its number
		self.reported = None # when the gap was last said
		# Whether a gap every camera shares is left to the recording watch: a triggered camera, with alertTriggerPauseSec
		self.pauses = PlannedPauses(cam_params) is not None and Triggered(cam_params)
		self.camera = camera if camera is not None else status.This() # this camera's row, and the others'
		self.paused = False # the gap now going is every camera's: not said

	def Frame(self, frameNumber):
		now = time.monotonic()
		if self.reported is not None:
			Say("{}: frames again, after {:.1f} s without (frame {}).".format(self.name, now - self.last, frameNumber))
			self.reported = None
		self.paused = False
		self.last, self.frameNumber = now, frameNumber

	def NoFrame(self, error):
		if self.last is None:
			return
		now = time.monotonic()
		if now - self.last < self.after:
			return
		if self.pauses and self.reported is None and self.camera.QuietTogether():
			self.paused = True # the trigger's pause: the recording watch says it
			return
		if self.reported is not None and now - self.reported < NO_FRAME_REPORT_SEC:
			return
		self.reported = now
		Say("{}: no frame for {:.0f} s, since frame {}. Last error: {}".format(
			self.name, now - self.last, self.frameNumber, ErrorText(error)))


def ImportCam(make):
	if make == "basler":
		from campy.cameras import basler as cam
	elif make == "flir":
		from campy.cameras import flir as cam
	elif make == "emu":
		from campy.cameras import emu as cam
	elif make == "ratcam":
		from campy.cameras import ratcam as cam
	else:
		Say('Camera make is not supported by CamPy. Check config.')
	return cam


def LoadSystems(params):
	try:
		systems = {}
		makes = GetMakeList(params)
		for m in range(len(makes)):
			systems[makes[m]] = {}
			cam = ImportCam(makes[m])
			systems[makes[m]]["system"] = cam.LoadSystem(params)
	except Exception as e:
		logging.error('Caught exception at camera/unicam.py LoadSystems. Check cameraMake: {}'.format(e))
		raise
	return systems


def LoadDevice(systems, params, cam_params):
	try:
		cam = ImportCam(cam_params["cameraMake"])
		cam_params = cam.LoadDevice(systems, params, cam_params)
	except Exception as e:
		logging.error('Caught exception at camera/unicam.py LoadSystems. Check cameraMake: {}'.format(e))
		raise
	return cam_params


def OpenCamera(cam_params, stopWriteQueue, recording=None):
	# Import the cam module
	cam = ImportCam(cam_params["cameraMake"])

	# What it says as it opens is said as one line once it is ready (campy/startup.py)
	startup.Begin(cam_params)
	try:
		camera, cam_params = cam.OpenCamera(cam_params)

		startup.Step("Opened {}: {} {} serial# {}".format( \
			cam_params["cameraName"],
			cam_params["cameraMake"], 
			cam_params["cameraModel"],
			cam_params["cameraSerialNo"]))

	except Exception as e:
		logging.error("Caught error at cameras/unicam.py OpenCamera: {}".format(e))
		SignalReady(cam_params, False, "failed to open: {}".format(e))
		if recording is not None:
			recording.cameraError = "failed to open: {}".format(e)
		stopWriteQueue.append('STOP')
		camera = None

	return cam, camera, cam_params


def SetFrameSize(cam_params, width, height, source):
	"""
	frameWidth and frameHeight set to the size of the frames the camera sends,
	as it opens (source: where that size comes from), and said when that is not
	the config's. The writer opens its video after this, at that size.
	"""
	size = (int(width), int(height))
	asked = (cam_params["frameWidth"], cam_params["frameHeight"])
	if size != asked:
		Say("{}: frameWidth x frameHeight {}x{} changed to {}x{}, {}.".format(
			cam_params["cameraName"], *(asked + size + (source,))))
	cam_params["frameWidth"], cam_params["frameHeight"] = size
	return cam_params


def GetDeviceList(systems, params):
	makes = GetMakeList(params)
	for m in range(len(makes)):
		cam = ImportCam(makes[m])
		system = systems[makes[m]]["system"]
		deviceList = cam.GetDeviceList(system)
		serials = [cam.GetSerialNumber(deviceList[i]) for i in range(len(deviceList))]
		systems[makes[m]]["serials"] = serials
		systems[makes[m]]["deviceList"] = deviceList
	return systems


class CameraMissing(Exception):
	"""A camera that cameraSerials names isn't among the cameras found."""


def SelectBySerial(systems, params):
	"""
	With cameraSerials: each camera's cameraSelection becomes the place of its
	serial number in its make's list of cameras found, so a name always goes to
	the same camera, in whatever order the driver lists them (the order can
	change when a cable or a USB port changes). Runs in every process, after
	GetDeviceList, since each lists the cameras again. A serial that isn't
	there raises CameraMissing, naming the camera and the serials found.
	Without cameraSerials, nothing changes.
	"""
	wanted = params.get("cameraSerials")
	if wanted in (None, "None"):
		return params
	num_cams = params["numCams"]
	makes = params["cameraMake"]
	names = params["cameraNames"]
	selection, missing = [], []
	for n in range(num_cams):
		make = makes[n] if isinstance(makes, list) else makes
		serial = str(wanted[n]).strip()
		found = [str(s).strip() for s in (systems.get(make) or {}).get("serials", [])]
		if serial in found:
			selection.append(found.index(serial))
		else:
			missing.append((names[n], make, serial, found))
	if missing:
		lines = ["{}: its camera (serial {}) isn't connected, or isn't found.".format(name, serial)
			for name, _, serial, _ in missing]
		for make in sorted(set(m for _, m, _, _ in missing)):
			found = next(f for _, m, _, f in missing if m == make)
			lines.append("{} {} camera{} found: {}.".format(len(found), make, "" if len(found) == 1 else "s",
				", ".join(found) if found else "none"))
		lines.append("Check the camera's cable and USB port. If a camera was swapped for another, put the new "
			"one's serial in cameraSerials. Nothing was recorded.")
		raise CameraMissing("\n".join(lines))
	params["cameraSelection"] = selection
	return params


def GetMakeList(params):
	if type(params["cameraMake"]) is list:
		cameraMakes = [params["cameraMake"][m] for m in range(len(params["cameraMake"]))]
	elif type(params["cameraMake"]) is str:
		cameraMakes = [params["cameraMake"]]
	makes = list(set(cameraMakes))
	return makes


def GrabData(cam_params):
	grabdata = {}
	# Each written frame's number, camera timestamp, and when it reached the
	# computer (campy-align): frametimes.npy and hosttimes.npy, written as the
	# recording goes, about every second
	grabdata["frameTimes"] = frametimes.FrameTimes(CameraFolder(cam_params), cam_params["frameRate"],
		name=cam_params["cameraName"])
	grabdata["cameraName"] = cam_params["cameraName"]

	# Calculate number of images and chunk length
	grabdata["numImagesToGrab"] = int(round(cam_params["recTimeInSec"]*cam_params["frameRate"]))
	# At least a frame: a chunkLengthInSec shorter than half a frame rounds to 0,
	# and CountFPS divides by it at every frame
	grabdata["chunkLengthInFrames"] = max(1, int(round(cam_params["chunkLengthInSec"]*cam_params["frameRate"])))

	# Frames dropped because the encoder fell behind (see DropFrame)
	grabdata["framesDropped"] = 0

	# A trigger that pauses on purpose (alertTriggerPauseSec): its pauses, left out of the rate CountFPS says
	grabdata["planned"] = PlannedPauses(cam_params) is not None
	grabdata["pausedSec"] = 0.0
	grabdata["pauses"] = 0
	grabdata["lastStamp"] = None
	grabdata["framePeriod"] = 1.0 / float(cam_params["frameRate"])
	# The first and the last grabbed frame's camera timestamps, dropped frames too: the time the frames grabbed
	# took, for the rate at the end (EndLine) and totalTime
	grabdata["firstStamp"] = None
	grabdata["lastGrabStamp"] = None
	grabdata["lastDropReport"] = float("-inf")
	# cameraReconnect: each time the camera was unplugged and came back, [its last frame before, its first after,
	# seconds gone, the offset added to its timestamps since]; the frame numbers between, and the seconds, in all
	grabdata["reconnects"] = []
	grabdata["framesWhileGone"] = 0
	grabdata["secondsGone"] = 0.0
	grabdata["firstHostNs"] = None

	return grabdata


def Triggered(cam_params):
	# A camera that takes a frame at each trigger pulse (a RatCam runs on its own clock)
	return str(cam_params.get("cameraTrigger", "None")) != "None" and cam_params.get("cameraMake") != "ratcam"


def MaxQueueFrames(cam_params):
	# writeQueueMaxSec as a number of frames; None = unbounded
	max_sec = cam_params.get("writeQueueMaxSec", "None")
	if max_sec is None or str(max_sec) == "None":
		return None
	return max(1, int(round(float(max_sec) * cam_params["frameRate"])))


def DropFrame(grabdata, frameNumber, maxQueue):
	# The writer is maxQueue frames behind, so this frame is counted instead of
	# queued. Its frame number is left out of frametimes.npy, which keeps one
	# row per video frame: the gap in its frame numbers marks the drop.
	grabdata["framesDropped"] += 1
	status.This().Dropped(grabdata["framesDropped"])
	now = time.perf_counter()
	if now - grabdata["lastDropReport"] >= 5:
		grabdata["lastDropReport"] = now
		Say("{}: the encoder is {} frames behind. Dropped {} frames so far (latest: frame {})."\
			.format(grabdata["cameraName"], maxQueue, grabdata["framesDropped"], frameNumber))


def StartGrabbing(camera, cam_params, cam):
	grabbing = cam.StartGrabbing(camera)
	if grabbing:
		# The camera's settings into metadata.csv before campy is told it is
		# ready: campy's main process reads that file for session.json as soon
		# as every camera is ready, and an external trigger starts at once.
		# Also so that a recording that stops without closing (SaveMetadata)
		# still has them; SaveMetadata adds the totals.
		try:
			WriteMetadata(cam_params)
		except Exception as e:
			SignalReady(cam_params, False, "could not write metadata.csv: {}".format(e))
			raise
		# One line: the camera, its settings and its video (campy/startup.py)
		startup.Ready(cam_params)
	else:
		Say("{} did not start grabbing, so it records nothing.".format(cam_params["cameraName"]))
	SignalReady(cam_params, bool(grabbing))
	return grabbing


def CountFPS(grabdata, frameNumber, timeStamp):
	# With alertTriggerPauseSec (a trigger that pauses on purpose), the gaps of NO_FRAME_SEC or more between two
	# frames (by the camera's timestamps) are left out of the rate, and said apart: over the whole time, the
	# rate at 10 s of frames every 5 minutes was a fifth of the trigger's, as if frames were missing
	if grabdata.get("planned"):
		last = grabdata.get("lastStamp")
		if last is not None and timeStamp - last >= NO_FRAME_SEC:
			grabdata["pausedSec"] += timeStamp - last - grabdata.get("framePeriod", 0.0) # the pulses missed only
			grabdata["pauses"] += 1
		grabdata["lastStamp"] = timeStamp
	# With campy's status line on the console (statusLine), the main process shows every camera's count in it
	if frameNumber % grabdata["chunkLengthInFrames"] == 0 and not statusline.Shown():
		timeElapsed = timeStamp - grabdata["frameTimes"].first
		if timeElapsed <= 0:
			return # the first frame, at a chunk of one frame: no rate yet
		paused = grabdata.get("pausedSec", 0.0)
		if paused > 0 and timeElapsed - paused > 0:
			fpsCount = round((frameNumber - 1) / (timeElapsed - paused), 1)
			Say("{} collected {} frames at {} fps for {} sec of frames ({} sec in all, with {} trigger pause{} "
				"of {} sec).".format(grabdata["cameraName"], frameNumber, fpsCount, round(timeElapsed - paused),
				round(timeElapsed), grabdata["pauses"], "" if grabdata["pauses"] == 1 else "s", round(paused)))
			return
		fpsCount = round((frameNumber - 1) / timeElapsed, 1)
		Say('{} collected {} frames at {} fps for {} sec.'\
			.format(grabdata["cameraName"], frameNumber, fpsCount, round(timeElapsed)))


# cameraReconnect: how often campy looks for a camera that was unplugged, and says it is still gone
RECONNECT_EVERY_S = 1.0
RECONNECT_REPORT_S = 30.0
# A camera back numbers its first frame as the other cameras' frame of the same trigger pulse: by their newest
# frame's host time, when it is within MATCH_S of its own; waiting up to MATCH_WAIT_S for one that is (after a
# trigger pause, the others' frames of the first pulse may come a moment after its own)
MATCH_S = 1.0
MATCH_WAIT_S = 0.25


def Reconnects(cam_params, cam):
	# Whether this camera is waited for when it is unplugged, and taken back: cameraReconnect, with a camera module
	# that can open its camera again (FLIR, emulated)
	value = cam_params.get("cameraReconnect", True)
	return str(value) not in ("False", "false", "0", "None") and hasattr(cam, "Reopen")


def FrameNumberBack(hostNs, period, others, lastFrame, lastHostNs):
	"""
	The frame number of a camera's first frame after it was unplugged and
	plugged back in: the trigger kept going, so it is the pulse the frame
	came on, as the other cameras number it. others: their newest frames
	[(number, host time ns)] (status.Camera.Others). By the others whose
	newest frame is within MATCH_S of this one (the median: (host time
	apart) / period added to their number), else by this camera's own last
	frame before it was gone (a guess across a trigger pause). Never at or
	below its last frame. Returns (number, True when by the others).
	"""
	near = [n + int(round((hostNs - h) / 1e9 / period)) for n, h in others if abs(hostNs - h) <= MATCH_S * 1e9]
	if near:
		number, matched = int(sorted(near)[len(near) // 2]), True
	elif lastHostNs:
		number, matched = lastFrame + int(round((hostNs - lastHostNs) / 1e9 / period)), False
	else:
		number, matched = lastFrame + 1, False
	return max(number, lastFrame + 1), matched


class Resume:
	"""
	A camera plugged back in, until its first frame (GrabFrames): that
	frame's number (FrameNumberBack), and the offset that carries its
	timestamps on from the camera's clock before it was gone (a FLIR
	camera's clock starts again at 0 as it powers up): its first frame's
	timestamp becomes the last frame's plus the pulses between them, so the
	camera's timestamps in frametimes.npy step over the time it was gone as
	over frames campy dropped, and campy-align puts every later frame on its
	trigger pulse.
	"""

	def __init__(self, lastFrame, lastHostNs, lastStamp, period, lostAt):
		self.lastFrame, self.lastHostNs, self.lastStamp = lastFrame, lastHostNs, lastStamp
		self.period, self.lostAt = period, lostAt

	def Number(self, hostNs, others=None):
		# others: a function giving the other cameras' newest frames, asked again for up to MATCH_WAIT_S
		# Asked again until one matches, for up to MATCH_WAIT_S: none may have matched yet because the others' frames
		# of this pulse are not in, or because a camera's counters changed as they were read (status.Camera.Others
		# leaves those out; on a busy computer that can be every camera at once). With no other camera grabbing, that
		# wait is all it costs, once
		deadline = time.monotonic() + MATCH_WAIT_S
		while True:
			found = others() if others is not None else []
			number, matched = FrameNumberBack(hostNs, self.period, found, self.lastFrame, self.lastHostNs)
			if matched or others is None or time.monotonic() >= deadline:
				return number, matched
			time.sleep(0.002)

	def Offset(self, number, stamp):
		# What to add to the camera's timestamps from now on (s)
		if self.lastStamp is None:
			return 0.0
		return self.lastStamp + (number - self.lastFrame) * self.period - stamp


def WaitForCamera(cam, cam_params, camera, grabdata, stopReadQueue):
	"""
	cameraReconnect: a camera that is gone (CameraLost), looked for every
	RECONNECT_EVERY_S until it is back (its module's Reopen opens it again,
	with campy's settings) and grabbing: returns it. Returns None when the
	recording ends first: Ctrl-C or a stop file, its writer stopped, no other
	camera recording any more, or this camera's recTimeInSec over (from its
	first frame).
	"""
	name = cam_params["cameraName"]
	lost = time.monotonic()
	said = lost
	first = grabdata.get("firstHostNs")
	end = first + float(cam_params["recTimeInSec"]) * 1e9 if first else None
	error = None
	while not StopRequested() and not stopReadQueue:
		if end is not None and clock.HostNs() >= end:
			Say("{}: its recording's time is up, and the camera did not come back.".format(name))
			return None
		if not status.This().OthersGoOn():
			Say("{}: the other cameras have ended, and the camera did not come back.".format(name))
			return None
		try:
			back = cam.Reopen(cam_params, camera)
		except Exception as e:
			back, error = None, e
		if back is not None:
			camera = back
			try:
				if cam.StartGrabbing(back):
					return back
				error = "it opened again, but did not start grabbing"
			except Exception as e:
				error = e
			CloseCamera(cam, cam_params, back)
		now = time.monotonic()
		if now - said >= RECONNECT_REPORT_S:
			said = now
			Say("{}: still gone after {:.0f} s; waiting for it to be plugged back in{}.".format(name, now - lost,
				" (last try: {})".format(ErrorText(error)) if error else ""))
		time.sleep(RECONNECT_EVERY_S)
	return None


def GrabFrames(cam_params, writeQueue, stopReadQueue, stopWriteQueue, openQueue=None, recording=None):
	# recording: what this thread and the writer tell each other (Recording),
	# when a writer runs alongside (campy.AcquireOneCamera). Metadata is then
	# saved once the writer is done, with the frames it gave ffmpeg.
	withWriter = recording is not None
	if recording is None:
		recording = Recording()

	# Open the camera object, then tell the writer whether it opened
	# (openQueue): the writer opens its video after that, since opening a
	# camera can set frameWidth and frameHeight (writer.WriteFrames)
	camera = None
	try:
		cam, camera, cam_params = OpenCamera(cam_params, stopWriteQueue, recording)
	finally:
		if openQueue is not None:
			openQueue.append(camera is not None)
	if camera is None:
		return # OpenCamera has said why, and stopped the writer

	# Create dictionary for appending frame number and timestamp information
	grabdata = GrabData(cam_params)
	maxQueue = MaxQueueFrames(cam_params)
	watch = FrameWatch(cam_params)
	mine = status.This() # this camera's counters, for campy's recording watch (campy/watch.py)
	gaps = frametimes.TriggerGaps() if Triggered(cam_params) else None # trigger pulses without a frame
	feed = livefeed.Feed(cam_params, say=Say) # liveFeed: every frame into shared memory too (campy-live-view shows it)
	frameNumber = 0
	resume = None # a camera back after it was unplugged, until its first frame (cameraReconnect)
	stampOffset = 0.0 # added to its timestamps since (Resume.Offset)
	lastHostNs = 0 # the last frame's host time

	# Whatever happens from here on, the camera is closed, the writer is told
	# to stop, and metadata is saved (finally, below). Without
	# its stop, the writer waits forever and the video is never finished.
	try:
		# The live feed's shared memory first (liveFeed), before campy is told
		# this camera is ready and starts the trigger
		feed.Prepare(cam_params)

		# Start grabbing frames from the camera (and save its settings: StartGrabbing)
		grabbing = StartGrabbing(camera, cam_params, cam)
		mine.State(status.GRABBING if grabbing else status.NOT_STARTED)
		if not grabbing:
			recording.cameraError = "did not start grabbing"

		while grabbing and not stopReadQueue and not StopRequested():
			try:
				# Grab image from camera buffer if available
				grabResult = cam.GrabFrame(camera, frameNumber)
			except CameraLost as e:
				gone = "the camera is gone, after frame {}: {}".format(frameNumber, ErrorText(e))
				mine.State(status.LOST)
				if not Reconnects(cam_params, cam):
					recording.cameraError = gone
					Say("{}: {}. Its recording ends there.".format(cam_params["cameraName"], gone))
					break
				# cameraReconnect: waited for, while the other cameras record on, and taken back when it is plugged
				# in again; its video, frametimes.npy and live feed go on from there
				Say("{}: {}. campy waits for it to be plugged back in (cameraReconnect); the other cameras record "
					"on.".format(cam_params["cameraName"], gone))
				feed.Lost()
				lostAt = time.monotonic()
				back = WaitForCamera(cam, cam_params, camera, grabdata, stopReadQueue)
				if back is None:
					recording.cameraError = gone + "; it was not plugged back in before the recording ended"
					break
				camera = back
				# (lastGrabStamp: as frametimes.npy has it, any earlier offset in)
				resume = Resume(frameNumber, lastHostNs, grabdata["lastGrabStamp"], grabdata["framePeriod"], lostAt)
				if gaps is not None:
					gaps.Restart()
				continue
			except Exception as e:
				# No frame yet. Said when it lasts (FrameWatch).
				if cam_params["cameraDebug"]:
					logging.error('Caught exception at cameras/unicam.py GrabFrames: {}'.format(e))
				watch.NoFrame(e)
				time.sleep(0.001)
				continue

			hostTime = clock.HostNs() # hosttimes.npy's clock, the same in every process
			# The camera's timestamp of this frame, read once: the offset that carries it on (Resume.Offset) is made
			# from the same value frametimes.npy gets. A FLIR camera's is the frame's own, read any number of times;
			# an emulated camera's is its clock as it is read, so read again after Resume.Number (which can wait up
			# to MATCH_WAIT_S) it was later by that wait, and the step across the gap off by it: by 1.5 ms on a busy
			# GitHub runner, 2026-11-13
			stamp = None
			if resume is not None:
				# Back after it was unplugged: its first frame numbered as the other cameras' of the same pulse, and
				# its timestamps carried on (Resume)
				number, matched = resume.Number(hostTime, mine.Others)
				try:
					stamp = cam.GetTimeStamp(grabResult)
					stampOffset = resume.Offset(number, stamp)
				except Exception:
					stamp, stampOffset = None, 0.0
				missed = number - resume.lastFrame - 1
				seconds = time.monotonic() - resume.lostAt
				grabdata["reconnects"].append([resume.lastFrame, number, round(seconds, 3), round(stampOffset, 9)])
				grabdata["framesWhileGone"] += missed
				grabdata["secondsGone"] += seconds
				mine.Reconnected(len(grabdata["reconnects"]), grabdata["framesWhileGone"])
				Say("{}: back, plugged in again after {:.1f} s: its frames go on from frame {} ({} frames while it was "
					"gone){}.".format(cam_params["cameraName"], seconds, number, missed, "" if matched else
					", numbered by its own last frame's time, as no other camera had a frame then: off if the trigger "
					"paused meanwhile"))
				frameNumber = number - 1
				resume = None
				mine.State(status.GRABBING)
			watch.Frame(frameNumber + 1)
			try:
				img = cam.GetImageArray(grabResult)
				frameNumber += 1 # first frame = 1
				timeStamp = (cam.GetTimeStamp(grabResult) if stamp is None else stamp) + stampOffset
				lastHostNs = hostTime
				if grabdata.get("firstHostNs") is None:
					grabdata["firstHostNs"] = hostTime
				if grabdata["firstStamp"] is None:
					grabdata["firstStamp"] = timeStamp
				grabdata["lastGrabStamp"] = timeStamp
				if gaps is not None and gaps.Add(timeStamp, frameNumber):
					mine.Missed(gaps.missed, gaps.frame)

				# Queue the frame for the writer, with its frameNumber and timeStamp,
				# unless the writer is writeQueueMaxSec behind
				queued = maxQueue is None or len(writeQueue) < maxQueue
				if queued:
					if withWriter:
						recording.queuedFrames.append(frameNumber) # before the frame: the writer takes both
					writeQueue.append(img)

				# Into the live feed's shared memory too: one copy, never waiting.
				# Before frametimes.npy takes the frame, which writes its batch to
				# disk about every second, and can take tens of ms doing it.
				feed.Write(img, frameNumber, hostTime, timeStamp)

				if queued:
					grabdata["frameTimes"].Add(frameNumber, timeStamp, hostTime)
				else:
					DropFrame(grabdata, frameNumber, maxQueue)
				mine.Frame(frameNumber, len(writeQueue), hostTime)

				CountFPS(grabdata, frameNumber, timeStamp)

			except Exception as e:
				if cam_params["cameraDebug"]:
					logging.error('Caught exception at cameras/unicam.py GrabFrames: {}'.format(e))

			finally:
				# Whatever happened to it: a FLIR camera holds each frame's buffer
				# until it is released, and stops sending once they are all held
				ReleaseFrame(cam, cam_params, grabResult)

			if frameNumber >= grabdata["numImagesToGrab"]:
				break

	except Exception as e:
		# Anything else that stops this thread: said, with its traceback, and
		# the camera closed as below
		logging.exception("Caught exception at cameras/unicam.py GrabFrames")
		recording.cameraError = "the camera thread stopped, after frame {}: {}".format(frameNumber, ErrorText(e))
		mine.State(status.CAMERA_FAILED)
		Say("{}: {}.".format(cam_params["cameraName"], recording.cameraError))

	finally:
		# Close the camera, tell the writer to stop, and once
		# the writer has given ffmpeg the frames still queued, save metadata
		grabdata["framesGrabbed"] = frameNumber - grabdata["framesWhileGone"] # not the numbers skipped while it was gone
		startup.Failed(cam_params, recording.cameraError) # a camera that stopped before it was ready: what it did
		mine.State(status.ENDED, only_from=(status.WAITING, status.GRABBING))
		CloseCamera(cam, cam_params, camera)
		feed.Close()
		stopWriteQueue.append('STOP')
		if withWriter:
			recording.written.wait()
		SaveMetadata(cam_params, grabdata, recording)


def ReleaseFrame(cam, cam_params, grabResult):
	try:
		cam.ReleaseFrame(grabResult)
	except Exception as e:
		if cam_params["cameraDebug"]:
			logging.error('Caught exception at cameras/unicam.py ReleaseFrame: {}'.format(e))


def CloseCamera(cam, cam_params, camera):
	# The camera modules say what fails as they close, and go on. This is for
	# anything one lets out, so that the writer is still stopped and metadata
	# still saved.
	try:
		cam.CloseCamera(cam_params, camera)
	except Exception as e:
		Say("{}: could not close the camera: {}".format(cam_params["cameraName"], ErrorText(e)))


def CameraFolder(cam_params):
	return os.path.join(cam_params["videoFolder"], cam_params["cameraName"])


def WriteMetadata(cam_params):
	# metadata.csv: the camera's parameters, one per row (not the objects)
	folder = CameraFolder(cam_params)
	os.makedirs(folder, exist_ok=True)
	with newfiles.Open(os.path.join(folder, "metadata.csv"), "w", newline="") as f:
		w = csv.writer(f, delimiter=",", quoting=csv.QUOTE_ALL)
		for row in cam_params.items():
			if isinstance(row[1], (list, str, int, float)):
				w.writerow(row)


def GrabbedTime(grabdata):
	# Seconds from the first grabbed frame to the last, by the camera's timestamps (dropped frames too)
	first, last = grabdata.get("firstStamp"), grabdata.get("lastGrabStamp")
	return last - first if first is not None and last is not None else 0


def EndLine(name, grabdata, held, recording):
	"""
	The camera's line as its recording ends: the frames it grabbed, at the
	rate they came (by their own first and last timestamps, the trigger's
	pauses left out), and, when that is not all of them, how many the video
	holds and where the others went. held: the video's frames.
	"""
	grabbed = grabdata["framesGrabbed"]
	seconds = GrabbedTime(grabdata) - grabdata.get("secondsGone", 0.0) # the time it was unplugged left out
	paused = grabdata.get("pausedSec", 0.0) if grabdata.get("pauses") else 0.0
	if paused and seconds - paused > 0:
		# A trigger that paused on purpose: the rate while frames came, and the pauses apart (CountFPS)
		rate = "{} fps over {} s of frames, with {} trigger pause{} of {} s in all".format(
			int(round((grabbed - 1) / (seconds - paused))), round(seconds - paused), grabdata["pauses"],
			"" if grabdata["pauses"] == 1 else "s", round(paused))
	else:
		rate = "{} fps".format(int(round((grabbed - 1) / seconds)) if seconds > 0 else 0)
	reconnects = grabdata.get("reconnects") or []
	if reconnects:
		rate += "; unplugged {} time{}, {:.0f} s and {} frames in all".format(len(reconnects),
			"" if len(reconnects) == 1 else "s", grabdata["secondsGone"], grabdata["framesWhileGone"])
	if held >= grabbed and not recording.videoError:
		return "{} saved {} frames at {}.".format(name, grabbed, rate)
	dropped = grabdata["framesDropped"]
	stopped = recording.restarts + (1 if recording.videoError else 0) # ffmpegs that stopped
	lost = recording.framesLost if stopped else 0
	after = max(0, grabbed - held - dropped - lost) if recording.videoError else 0
	why = []
	if dropped:
		why.append("{} dropped".format(dropped))
	if lost:
		why.append("{} lost in {}".format(lost, "a stopped ffmpeg" if stopped == 1 else "stopped ffmpegs"))
	if after:
		why.append("{} after its ffmpeg stopped".format(after))
	files = len(recording.videoFiles)
	holds = "its {} video files hold".format(files) if files > 1 else "its video holds"
	text = "{} grabbed {} frames at {}; {} {}".format(name, grabbed, rate, holds,
		"all of them" if held >= grabbed else held)
	if why:
		text += " ({})".format(", ".join(why))
	if recording.videoError and not lost and not after:
		text += ", but its ffmpeg stopped"
	return text + "."


def SaveMetadata(cam_params, grabdata, recording=None):
	"""
	As the camera's recording ends: the rest of frametimes.npy and
	hosttimes.npy, the end line, the totals into metadata.csv, then
	frametimes.mat. Frame times the files lack, or a frametimes.mat that
	could not be written, are the recording's frameTimesError (a failure:
	campy exits 1); totals that could not be written are said.
	"""
	full_folder_name = CameraFolder(cam_params)
	times = grabdata["frameTimes"]
	name = cam_params["cameraName"]
	if recording is None:
		recording = Recording()

	# The last frames into frametimes.npy and hosttimes.npy, one row per
	# frame in the video. When ffmpeg stopped, only the frames its file
	# holds: the frames queued after it stopped are left out (keepRows),
	# and so are those it was given but had not written yet (lostRows,
	# counted from the file: writer.CountPart), as when it was restarted.
	# A write that failed (FrameTimes.Flush) leaves times.error.
	keep = recording.keepRows
	if keep is None and recording.videoError:
		keep = recording.framesSent # its ffmpeg did not start, or failed as it closed: the frames it took
	try:
		times.Close(keep=keep, drop=recording.lostRows)
	except Exception as e: # Close says what went wrong in times.error; anything else, here
		times.error = times.error or "could not close its frame times: {}".format(ErrorText(e))
	if times.error:
		recording.frameTimesError = times.error
		Say("{}: {}.".format(name, times.error))

	try:
		if recording.cameraError:
			cam_params["cameraError"] = recording.cameraError
		if recording.videoError:
			cam_params["videoError"] = recording.videoError
		if recording.frameTimesError:
			cam_params["frameTimesError"] = recording.frameTimesError
		if times.failedWrites:
			cam_params["frameTimesFailedWrites"] = times.failedWrites
		if recording.ffmpegRetries:
			cam_params["ffmpegRetries"] = recording.ffmpegRetries
		if recording.restarts:
			cam_params["ffmpegRestarts"] = recording.restarts
			cam_params["videoFiles"] = list(recording.videoFiles)
		if recording.restarts or recording.videoError:
			cam_params["framesLostInEncoder"] = recording.framesLost
		if grabdata.get("reconnects"):
			# cameraReconnect: the times it was unplugged and came back. cameraReconnects: [its last frame before,
			# its first after, seconds gone, the offset added to its timestamps since (s): frametimes.npy's minus
			# that is the camera's own clock]
			cam_params["cameraReconnects"] = [list(r) for r in grabdata["reconnects"]]
			cam_params["framesWhileGone"] = grabdata["framesWhileGone"]
			cam_params["secondsGone"] = round(grabdata["secondsGone"], 3)
		if not times.count:
			Say("{} saved no frames.".format(name))
			if not recording.Failures():
				return

		# The camera's line: what it grabbed, and what its video holds
		frame_count = grabdata["framesGrabbed"]
		if times.count:
			Say(EndLine(name, grabdata, times.count, recording))
		if grabdata["framesDropped"]:
			Say('{} dropped {} of them: the encoder fell more than {} s behind (writeQueueMaxSec).'\
				.format(name, grabdata["framesDropped"], cam_params["writeQueueMaxSec"]))

		# The totals into metadata.csv, before frametimes.mat, so that a .mat
		# that fails leaves them in. With a failure, also what went wrong
		# (cameraError, videoError, frameTimesError, above). totalFrames: the
		# frames grabbed; totalTime: the seconds from the first to the last
		# (by the camera's timestamps); framesWritten: the video's frames.
		cam_params['totalFrames'] = frame_count
		cam_params['totalTime'] = GrabbedTime(grabdata)
		cam_params['framesWritten'] = times.count
		cam_params['framesDropped'] = grabdata['framesDropped']
		saved = True
		try:
			WriteMetadata(cam_params)
		except Exception as e:
			saved = False
			Say("{}: could not write its totals into {}: {}".format(name,
				os.path.join(full_folder_name, "metadata.csv"), ErrorText(e)))

		# Also save frame data to MATLAB file: the rows frametimes.npy has
		if times.saved[0]:
			mat_filename = os.path.join(full_folder_name, 'frametimes.mat')
			made = False
			try:
				x = np.load(os.path.join(full_folder_name, 'frametimes.npy'))
				matdata = {}
				matdata['frameNumber'] = x[0].astype(int) # as integers, as they were
				matdata['timeStamp'] = x[1]
				with newfiles.Open(mat_filename, "wb") as f:
					made = True
					sio.savemat(f, matdata, do_compression=True)
			except Exception as e:
				Say("{}: could not write {}: {}".format(name, mat_filename, ErrorText(e)))
				if made: # this process's own, part-written: it would read as the whole
					try:
						os.remove(mat_filename)
					except OSError:
						pass
				if not recording.frameTimesError:
					recording.frameTimesError = "could not write frametimes.mat: {} (frametimes.npy has every " \
						"frame)".format(ErrorText(e))
					cam_params["frameTimesError"] = recording.frameTimesError
					if saved:
						try:
							WriteMetadata(cam_params)
						except Exception as again:
							Say("{}: could not write frameTimesError into metadata.csv: {}".format(name, ErrorText(again)))

		if saved:
			Say('Saved metadata for {}.'.format(name))

	except Exception as e:
		logging.exception("{}: could not save its metadata".format(name))


def CloseSystems(systems, params):
	Say('Closing systems...')
	makes = GetMakeList(params)
	for m in range(len(makes)):
		cam = ImportCam(makes[m])
		cam.CloseSystem(systems[makes[m]]["system"], systems[makes[m]]["deviceList"])
	Say('Exiting campy...')
