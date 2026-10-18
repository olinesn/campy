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

import os, time, sys, logging, threading, queue
from collections import deque
import multiprocessing as mp
from campy import writer, configurator, preflight
from campy.trigger import trigger
from campy.cameras import unicam
from campy.utils.utils import HandleKeyboardInterrupt, Say, ErrorText

def OpenSystems():
	# Configure parameters
	params = configurator.ConfigureParams()

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


def RunCamera(n_cam, readyQueue, done):
	# One camera process: records its camera, then tells Main what went wrong,
	# if anything (done: unicam.TellMain). One that ends without saying died,
	# and Main says so.
	InitCameraProcess(readyQueue)
	unicam.SetMainConnection(done)
	recording = AcquireOneCamera(n_cam)
	readyQueue.close()
	readyQueue.join_thread()
	unicam.TellMain(("done", recording.Failures()))
	done.close()
	# Then it ends at once, as campy's pool workers did (they were terminated):
	# without running the camera drivers' own cleanup at exit, which campy has
	# never run, and which could otherwise crash or hang after a good recording
	os._exit(0)


def Main():
	global systems
	# Ctrl-C stops every camera, each closing its video, and Main waits for
	# them; a second Ctrl-C stops the ones that hang as they close
	with HandleKeyboardInterrupt() as interrupts:
		# One process per camera, started the same way on Windows and Linux
		# (spawn), and watched: one that dies is said at once, and the others
		# go on recording
		ctx = mp.get_context("spawn")
		readyQueue = ctx.Queue()
		cameras = unicam.CameraProcesses(ctx, RunCamera, params, readyQueue)

		# The trigger stops and the systems close whatever happens here
		try:
			# Start the trigger only once every camera is grabbing, so that every
			# camera's first frame is the trigger's first pulse. The serial port
			# opens meanwhile: the Arduino needs a few seconds after it opens.
			systems = trigger.OpenTriggers(systems, params)
			ready, missing = unicam.WaitForCameras(readyQueue, params, cameras,
				interrupted=lambda: interrupts.count > 0)
			if ready and not cameras.ready() and not interrupts.count: # not if the cameras are stopping (Ctrl-C)
				systems = trigger.StartTriggers(systems, params)
			trigger.SaveStart(params, ready, missing, systems.get("triggerStartedNs"))
			failures = cameras.Wait(interrupts)
		finally:
			CloseSystems(systems, params)

	# A camera that did not record to the end makes campy's exit code 1
	if failures:
		Say("{} of {} cameras did not record to the end:".format(len(failures), params["numCams"]))
		for name, reasons in failures.items():
			Say("  {}: {}".format(name, "; ".join(reasons)))
		sys.exit(1)

# Open systems, creates global 'systems' and 'params' variables
systems, params = OpenSystems()