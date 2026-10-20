"""
Unicam unifies camera APIs with common syntax to simplify multi-camera acquisition and 
reduce redundancy in campy code.
"""

import os, sys, time, csv, queue, logging, threading
import multiprocessing.connection
import numpy as np
from collections import deque
from scipy import io as sio
from campy import frametimes, newfiles, livefeed
from campy.utils.utils import Say, ExitCodeText, ErrorText


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


def TellMain(message):
	# A (kind, value) message for campy's main process: ("ffmpeg", pid or
	# None), ("done", what went wrong). Nothing outside a camera process.
	if _mainConnection is not None:
		try:
			_mainConnection.send(message)
		except Exception as e:
			logging.error("Caught exception at cameras/unicam.py TellMain: {}".format(e))


def SignalReady(cam_params, ok=True, error=""):
	# Tell campy's main process this camera is grabbing, or that it never will
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

	def __init__(self, ctx, target, params, readyQueue, log=Say):
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
			process = ctx.Process(target=target, args=(n, readyQueue, send), name=self.names[n], daemon=True)
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
		self.written = threading.Event() # the writer is done: framesSent and videoError are final

	def Failures(self):
		return [why for why in (self.cameraError, self.videoError) if why]


# A camera that has sent frames and then sends none for this long (or for
# three frame periods, at a slower frame rate) is said to have stopped, and
# again this often while it lasts
NO_FRAME_SEC = 3
NO_FRAME_REPORT_SEC = 30


class FrameWatch:
	"""
	Says when a camera stops sending frames, and when they come again. Only
	once it has sent its first: until then a triggered camera waits for its
	trigger, for as long as that takes.
	"""

	def __init__(self, cam_params):
		self.name = cam_params["cameraName"]
		self.after = max(NO_FRAME_SEC, 3.0 / float(cam_params["frameRate"]))
		self.last = None # when the last frame came (time.monotonic)
		self.frameNumber = 0 # and its number
		self.reported = None # when the gap was last said

	def Frame(self, frameNumber):
		now = time.monotonic()
		if self.reported is not None:
			Say("{}: frames again, after {:.1f} s without (frame {}).".format(self.name, now - self.last, frameNumber))
			self.reported = None
		self.last, self.frameNumber = now, frameNumber

	def NoFrame(self, error):
		if self.last is None:
			return
		now = time.monotonic()
		if now - self.last < self.after:
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

	try:
		camera, cam_params = cam.OpenCamera(cam_params)

		Say("Opened {}: {} {} serial# {}".format( \
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
	grabdata["frameTimes"] = frametimes.FrameTimes(CameraFolder(cam_params), cam_params["frameRate"])
	grabdata["cameraName"] = cam_params["cameraName"]

	# Calculate display rate
	if cam_params["displayFrameRate"] <= 0:
		grabdata["frameRatio"] = float('inf')
	elif cam_params["displayFrameRate"] > 0 and cam_params["displayFrameRate"] <= cam_params['frameRate']:
		grabdata["frameRatio"] = int(round(cam_params["frameRate"]/cam_params["displayFrameRate"]))
	else:
		# A preview asked to be faster than the camera shows every frame
		grabdata["frameRatio"] = 1

	# Calculate number of images and chunk length
	grabdata["numImagesToGrab"] = int(round(cam_params["recTimeInSec"]*cam_params["frameRate"]))
	# At least a frame: a chunkLengthInSec shorter than half a frame rounds to 0,
	# and CountFPS divides by it at every frame
	grabdata["chunkLengthInFrames"] = max(1, int(round(cam_params["chunkLengthInSec"]*cam_params["frameRate"])))

	# Frames dropped because the encoder fell behind (see DropFrame)
	grabdata["framesDropped"] = 0
	grabdata["lastDropReport"] = float("-inf")

	return grabdata


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
	now = time.perf_counter()
	if now - grabdata["lastDropReport"] >= 5:
		grabdata["lastDropReport"] = now
		Say("{}: the encoder is {} frames behind. Dropped {} frames so far (latest: frame {})."\
			.format(grabdata["cameraName"], maxQueue, grabdata["framesDropped"], frameNumber))


def StartGrabbing(camera, cam_params, cam):
	grabbing = cam.StartGrabbing(camera)
	if grabbing:
		Say("{} ready to trigger.".format(cam_params["cameraName"]))
	else:
		Say("{} did not start grabbing, so it records nothing.".format(cam_params["cameraName"]))
	SignalReady(cam_params, bool(grabbing))
	return grabbing


def CountFPS(grabdata, frameNumber, timeStamp):
	if frameNumber % grabdata["chunkLengthInFrames"] == 0:
		timeElapsed = timeStamp - grabdata["frameTimes"].first
		if timeElapsed <= 0:
			return # the first frame, at a chunk of one frame: no rate yet
		fpsCount = round((frameNumber - 1) / timeElapsed, 1)
		Say('{} collected {} frames at {} fps for {} sec.'\
			.format(grabdata["cameraName"], frameNumber, fpsCount, round(timeElapsed)))


def GrabFrames(cam_params, writeQueue, dispQueue, stopReadQueue, stopWriteQueue, openQueue=None, recording=None):
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
		dispQueue.append('STOP') # OpenCamera has said why, and stopped the writer
		return

	# Use Basler's default display window on Windows. Not supported on Linux
	if sys.platform=='win32' and cam_params["cameraMake"] == 'basler':
		dispQueue = cam.OpenPylonImageWindow(cam_params)

	# Create dictionary for appending frame number and timestamp information
	grabdata = GrabData(cam_params)
	maxQueue = MaxQueueFrames(cam_params)
	watch = FrameWatch(cam_params)
	feed = livefeed.Feed(cam_params, say=Say) # liveFeed: every frame into shared memory too
	frameNumber = 0

	# Whatever happens from here on, the camera is closed, the writer and the
	# display are told to stop, and metadata is saved (finally, below). Without
	# its stop, the writer waits forever and the video is never finished.
	try:
		# The live feed's shared memory first (liveFeed), before campy is told
		# this camera is ready and starts the trigger
		feed.Prepare(cam_params)

		# Start grabbing frames from the camera
		grabbing = StartGrabbing(camera, cam_params, cam)

		# The camera's settings, now, so that a recording that stops without
		# closing (SaveMetadata) still has them; SaveMetadata adds the totals
		if grabbing:
			WriteMetadata(cam_params)
		else:
			recording.cameraError = "did not start grabbing"

		while grabbing and not stopReadQueue:
			try:
				# Grab image from camera buffer if available
				grabResult = cam.GrabFrame(camera, frameNumber)
			except CameraLost as e:
				recording.cameraError = "the camera is gone, after frame {}: {}".format(frameNumber, ErrorText(e))
				Say("{}: {}. Its recording ends there.".format(cam_params["cameraName"], recording.cameraError))
				break
			except Exception as e:
				# No frame yet. Said when it lasts (FrameWatch).
				if cam_params["cameraDebug"]:
					logging.error('Caught exception at cameras/unicam.py GrabFrames: {}'.format(e))
				watch.NoFrame(e)
				time.sleep(0.001)
				continue

			hostTime = time.monotonic_ns()
			watch.Frame(frameNumber + 1)
			try:
				img = cam.GetImageArray(grabResult)
				frameNumber += 1 # first frame = 1
				timeStamp = cam.GetTimeStamp(grabResult)

				# Queue the frame for the writer, with its frameNumber and timeStamp,
				# unless the writer is writeQueueMaxSec behind
				queued = maxQueue is None or len(writeQueue) < maxQueue
				if queued:
					writeQueue.append(img)

				# Into the live feed's shared memory too: one copy, never waiting.
				# Before frametimes.npy takes the frame, which writes its batch to
				# disk about every second, and can take tens of ms doing it.
				feed.Write(img, frameNumber, hostTime, timeStamp)

				if queued:
					grabdata["frameTimes"].Add(frameNumber, timeStamp, hostTime)
				else:
					DropFrame(grabdata, frameNumber, maxQueue)

				# Display converted, downsampled image in the Window
				if frameNumber % grabdata["frameRatio"] == 0:
					img = cam.DisplayImage(cam_params, dispQueue, grabResult)

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
		Say("{}: {}.".format(cam_params["cameraName"], recording.cameraError))

	finally:
		# Close the camera, tell the display and the writer to stop, and once
		# the writer has given ffmpeg the frames still queued, save metadata
		grabdata["framesGrabbed"] = frameNumber
		CloseCamera(cam, cam_params, camera)
		feed.Close()
		if not sys.platform=='win32' or not cam_params['cameraMake'] == 'basler':
			dispQueue.append('STOP')
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


def SaveMetadata(cam_params, grabdata, recording=None):
	full_folder_name = CameraFolder(cam_params)
	times = grabdata["frameTimes"]
	name = cam_params["cameraName"]
	if recording is None:
		recording = Recording()

	try:
		# The last frames into frametimes.npy and hosttimes.npy. When ffmpeg
		# stopped, only the frames it was given: frametimes.npy keeps one row
		# per frame sent to the video, and the frames queued after are left out.
		times.Close(keep=recording.framesSent if recording.videoError else None)
		if recording.cameraError:
			cam_params["cameraError"] = recording.cameraError
		if recording.videoError:
			cam_params["videoError"] = recording.videoError
		if not times.count:
			Say("{} saved no frames.".format(name))
			if not recording.Failures():
				return

		# Get the frame and time counts to save into metadata
		frame_count = grabdata["framesGrabbed"]
		time_count = times.last or 0
		fps_count = int(round(frame_count/time_count)) if time_count > 0 else 0
		if recording.videoError and times.count:
			Say("{} grabbed {} frames at {} fps. ffmpeg was given {} of them before it stopped.".format(
				name, frame_count, fps_count, times.count))
		elif times.count:
			Say('{} saved {} frames at {} fps.'.format(name, frame_count, fps_count))
		if grabdata["framesDropped"]:
			Say('{} dropped {} of them: the encoder fell more than {} s behind (writeQueueMaxSec).'\
				.format(name, grabdata["framesDropped"], cam_params["writeQueueMaxSec"]))

		# Also save frame data to MATLAB file
		if times.count:
			x = np.load(os.path.join(full_folder_name, 'frametimes.npy'))
			mat_filename = os.path.join(full_folder_name, 'frametimes.mat')
			matdata = {}
			matdata['frameNumber'] = x[0].astype(int) # as integers, as they were
			matdata['timeStamp'] = x[1]
			with newfiles.Open(mat_filename, "wb") as f:
				sio.savemat(f, matdata, do_compression=True)

		# Save parameters and recording metadata to csv spreadsheet. With a
		# failure, also what went wrong (cameraError, videoError, above).
		cam_params['totalFrames'] = frame_count
		cam_params['totalTime'] = time_count
		cam_params['framesWritten'] = times.count
		cam_params['framesDropped'] = grabdata['framesDropped']
		WriteMetadata(cam_params)

		Say('Saved metadata for {}.'.format(name))

	except Exception as e:
		logging.error('Caught exception: {}'.format(e))


def CloseSystems(systems, params):
	Say('Closing systems...')
	makes = GetMakeList(params)
	for m in range(len(makes)):
		cam = ImportCam(makes[m])
		cam.CloseSystem(systems[makes[m]]["system"], systems[makes[m]]["deviceList"])
	Say('Exiting campy...')
