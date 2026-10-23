"""
CamPy: Python-based multi-camera recording software.
Integrates machine vision camera APIs with ffmpeg real-time compression.
Outputs one MP4 video file and metadata files for each camera

"campy" is the main console. 
User inputs are loaded from config yaml file using a command line interface (CLI) 
configurator parses the config arguments (and default params) into "params" dictionary.
configurator assigns params to each camera stream in the "cam_params" dictionary.
	* Camera index is set by "cameraSelection".
	* If param is string, it is applied to all cameras.
	* If param is list of strings, it is assigned to each camera, ordered by camera index.
Camera streams are acquired and encoded in parallel using multiprocessing.

Usage: 
campy-acquire ./configs/campy_config.yaml
"""

import os, time, sys, copy, logging, threading, queue
from collections import deque
import multiprocessing as mp
from campy import writer, configurator, preflight, newfiles, keepawake, defaults, manifest
from campy.trigger import trigger
from campy.cameras import unicam, emu
from campy.utils.utils import HandleKeyboardInterrupt, Say, ErrorText, StartPrinter, StopPrinter

# What session.json is made from, in the main process (OpenSystems): the
# params as the config made them, where each came from, the defaults that
# changed which this run relies on, and the computer's details, read meanwhile
configured = sources = None
defaultWarnings = []
probe = None


def DefaultWarnings(params, sources):
	# The changed defaults this run relies on (campy/defaults.py), as lines
	cams = [configurator.CameraParams(params, n) for n in range(params["numCams"])]
	return defaults.Warnings(params, sources, {"cameras": cams}, known=params.get("defaultsVersion"),
		enabled=params.get("defaultWarnings", True))


def OpenSystems():
	global configured, sources, defaultWarnings, probe
	# Configure parameters
	params = configurator.ConfigureParams()
	if mp.current_process().name == "MainProcess":
		probe = manifest.Probe()
		configured, sources = copy.deepcopy(params), configurator.Sources()
		defaultWarnings = DefaultWarnings(params, sources)
		if defaultWarnings:
			print(defaults.Message(defaultWarnings), flush=True)

	# A recording never replaces one: stop now, before anything opens, if any
	# file it would write is already there, whatever preflight is set to. Each
	# run records into a new folder of its own (sessionFolder), made in Main;
	# the camera processes are told which.
	if mp.current_process().name == "MainProcess":
		try:
			if newfiles.SessionFolders(params):
				newfiles.CheckNames(params)
			else:
				newfiles.Check(params)
		except newfiles.WouldOverwrite as e:
			print(e, flush=True)
			sys.exit(1)
	elif os.environ.get(newfiles.SESSION_ENV):
		params = newfiles.UseSession(params, os.environ[newfiles.SESSION_ENV])

	# Load Camera Systems and Devices
	systems = unicam.LoadSystems(params)
	systems = unicam.GetDeviceList(systems, params)

	# Check that this configuration fits this computer, before anything starts.
	# Every camera process re-imports this module, so only the parent checks.
	if mp.current_process().name == "MainProcess":
		try:
			preflight.Run(systems, params)
		except preflight.PreflightError as e:
			print(e, flush=True)
			unicam.CloseSystems(systems, params)
			sys.exit(1)

	# The trigger starts in Main, in this process only, once every camera is
	# grabbing. (Each camera process re-imports this module, so anything here
	# runs once per camera too.)
	return systems, params


def CloseSystems(systems, params):
	# Each step whatever the one before did: the trigger stops even when a
	# camera system does not close, and the other way round
	try:
		trigger.StopTriggers(systems, params)
	except Exception as e:
		Say("Could not stop the trigger: {}".format(ErrorText(e)))
	try:
		unicam.CloseSystems(systems, params)
	except Exception as e:
		Say("Could not close the camera systems: {}".format(ErrorText(e)))


def AcquireOneCamera(n_cam):
	# Initialize param dictionary for this camera stream
	cam_params = configurator.ConfigureCamParams(systems, params, n_cam)

	# Initialize queues for display, video writer, and stop messages, and for
	# whether the camera opened; and what the camera thread and the writer tell
	# each other, and what went wrong (unicam.Recording)
	recording = unicam.Recording()

	# Its files are campy's own, empty (Main made them, newfiles.Claim): if
	# any holds something, this camera records nothing, and opens nothing
	try:
		newfiles.CheckCamera(cam_params)
	except newfiles.WouldOverwrite as e:
		recording.cameraError = str(e)
		Say("{}: {}".format(cam_params["cameraName"], e))
		unicam.SignalReady(cam_params, False, "its files are already there")
		return recording

	dispQueue = deque([], 2)
	writeQueue = deque()
	stopReadQueue = deque([],1)
	stopWriteQueue = deque([],1)
	openQueue = deque([],1)

	# Start image window display thread, unless running headless
	# (displayFrameRate 0)
	if cam_params["displayFrameRate"] > 0:
		threading.Thread(
			target = ShowPreview,
			daemon = True,
			args = (cam_params, dispQueue,),
			).start()

	# Start grabbing frames ("producer" thread)
	grab = threading.Thread(
		target = unicam.GrabFrames,
		daemon = True,
		args = (cam_params, writeQueue, dispQueue, stopReadQueue, stopWriteQueue, openQueue, recording,),
		)
	grab.start()

	# Start video file writer (main "consumer" process), once the camera is
	# open: opening it can change the frame size
	writer.WriteFrames(cam_params, writeQueue, stopReadQueue, stopWriteQueue, openQueue, recording=recording)

	# The camera thread saves metadata once the writer is done, with the frames
	# ffmpeg took: the process waits for it
	grab.join()
	return recording


def ShowPreview(cam_params, dispQueue):
	# The display thread. Qt is imported here, not in the camera process's main
	# thread: Qt takes the first thread that loads it as its main thread, and
	# the preview's QApplication has to be made in that one ("QApplication was
	# not created in the main() thread" otherwise). Headless runs load no Qt.
	from campy import display
	display.DisplayFrames(cam_params, dispQueue)


def InitCameraProcess(readyQueue):
	# Each camera process reports here when its camera is grabbing
	unicam.SetReadyQueue(readyQueue)


def RunCamera(n_cam, readyQueue, done, start=None):
	# One camera process: records its camera, then tells Main what went wrong,
	# if anything (done: unicam.TellMain). One that ends without saying died,
	# and Main says so. start: when an emulated camera's frame 0 is due, which
	# Main sets once every camera is ready (cameras/emu.py StartClock).
	# What it says is written by a thread of its own, so that a slow console
	# or log file never holds up its camera; all of it, before it ends
	StartPrinter()
	try:
		InitCameraProcess(readyQueue)
		unicam.SetMainConnection(done)
		emu.SetStartClock(start)
		# Not throttled should standby start anyway (Windows); released as it ends
		keepawake.KeepAwake(params.get("keepAwake", True), whole=False).Start()
		recording = AcquireOneCamera(n_cam)
		readyQueue.close()
		readyQueue.join_thread()
		unicam.TellMain(("done", recording.Failures()))
		done.close()
	finally:
		StopPrinter()
	# Then it ends at once, as campy's pool workers did (they were terminated):
	# without running the camera drivers' own cleanup at exit, which campy has
	# never run, and which could otherwise crash or hang after a good recording
	os._exit(0)


def Main():
	global systems
	# Ctrl-C stops every camera, each closing its video, and Main waits for
	# them; a second Ctrl-C stops the ones that hang as they close
	with HandleKeyboardInterrupt() as interrupts:
		# The trigger Arduino answers, or nothing starts: a dead one used to
		# leave the cameras waiting for frames that never came. Ctrl-C stops
		# the check, and campy, at once.
		try:
			systems = trigger.OpenTriggers(systems, params, interrupted=lambda: interrupts.count > 0)
		except trigger.TriggerError as e:
			print(e, flush=True)
			CloseSystems(systems, params)
			sys.exit(1)

		# This run's own folder, inside videoFolder, named by the date and time
		# (sessionFolder): the camera processes, which read the config again,
		# find it in their environment
		if newfiles.SessionFolders(params):
			folder = newfiles.MakeSessionFolder(params)
			newfiles.UseSession(params, folder)
			os.environ[newfiles.SESSION_ENV] = folder
			Say("Recording into {}".format(folder))
		calibration = newfiles.CalibrationCameras(params)
		if calibration:
			Say("Calibration video (\"{}\" in videoFilename) for {}: each goes into <camera>{}{}{}.".format(
				newfiles.CALIBRATION_WORD, ", ".join(calibration), os.sep, newfiles.CALIBRATION_FOLDER, os.sep))

		# Every file of the recording, made empty now, each only if it is not
		# there: of two campys started into one folder, only one records
		try:
			claimed = newfiles.Claim(params)
		except newfiles.WouldOverwrite as e:
			print(e, flush=True)
			CloseSystems(systems, params)
			sys.exit(1)

		# session.json: how this recording is made (campy/manifest.py)
		session = manifest.Session(params, sources or {}, configurator.ConfigPath(), systems, defaultWarnings,
			probe, say=Say)
		session.Start(configured)
		failures, error = None, None

		# The computer stays awake until the cameras have finished: a PC with
		# Modern Standby would otherwise go into standby when its screen times
		# out, mid-recording (keepAwake)
		awake = keepawake.KeepAwake(params.get("keepAwake", True)).Start()
		if awake.how:
			Say("Keeping this computer awake while recording (keepAwake): {}.".format(awake.how))
		elif awake.problem:
			Say("Could not keep this computer awake while recording: {}. Set its screen and sleep \
timeouts to Never, or it may go into standby mid-recording.".format(awake.problem))

		# One process per camera, started the same way on Windows and Linux
		# (spawn), and watched: one that dies is said at once, and the others
		# go on recording
		ctx = mp.get_context("spawn")
		readyQueue = ctx.Queue()
		# Emulated cameras (cameraMake "emu") time their frames from one start,
		# set below once every camera is ready, as a hardware trigger starts them
		start = ctx.Value("d", 0.0, lock=False)
		cameras = unicam.CameraProcesses(ctx, RunCamera, params, readyQueue, start=start)

		# The trigger stops and the systems close whatever happens here
		try:
			# Start the trigger only once every camera is grabbing, so that every
			# camera's first frame is the trigger's first pulse, and once the
			# Arduino is done with the check's command (a few seconds after it)
			ready, missing = unicam.WaitForCameras(readyQueue, params, cameras,
				interrupted=lambda: interrupts.count > 0)
			if ready and trigger.WaitToStart(systems, params, interrupted=lambda: interrupts.count > 0) \
				and not cameras.ready() and not interrupts.count: # not if the cameras are stopping (Ctrl-C)
				systems = trigger.StartTriggers(systems, params)
				emu.StartClock(start)
			trigger.SaveStart(params, ready, missing, systems.get("triggerStartedNs"))
			session.Recording(ready, missing)
			failures = cameras.Wait(interrupts)
		except BaseException as e:
			error = ErrorText(e)
			raise
		finally:
			awake.Stop()
			CloseSystems(systems, params)
			session.End(failures, interrupted=interrupts.count > 0, error=error)
			# The files still empty go: a camera that saved no frames leaves
			# what it always left. Not a stopped camera's, whose ffmpeg may still
			# be finishing its video.
			newfiles.Release([(path, n) for path, n in claimed if n is None or n in cameras.done])

	# A camera that did not record to the end makes campy's exit code 1
	if failures:
		Say("{} of {} cameras did not record to the end:".format(len(failures), params["numCams"]))
		for name, reasons in failures.items():
			Say("  {}: {}".format(name, "; ".join(reasons)))
		sys.exit(1)

# Open systems, creates global 'systems' and 'params' variables
systems, params = OpenSystems()