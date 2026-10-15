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
from campy.utils.utils import HandleKeyboardInterrupt

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
	trigger.StopTriggers(systems, params)
	unicam.CloseSystems(systems, params)


def AcquireOneCamera(n_cam):
	# Initialize param dictionary for this camera stream
	cam_params = configurator.ConfigureCamParams(systems, params, n_cam)

	# Initialize queues for display, video writer, and stop messages
	dispQueue = deque([], 2)
	writeQueue = deque()
	stopReadQueue = deque([],1)
	stopWriteQueue = deque([],1)

	# Start image window display thread, unless running headless
	# (displayFrameRate 0). Imported here so headless runs need no Qt.
	if cam_params["displayFrameRate"] > 0:
		from campy import display
		threading.Thread(
			target = display.DisplayFrames,
			daemon = True,
			args = (cam_params, dispQueue,),
			).start()

	# Start grabbing frames ("producer" thread)
	threading.Thread(
		target = unicam.GrabFrames,
		daemon = True,
		args = (cam_params, writeQueue, dispQueue, stopReadQueue, stopWriteQueue,),
		).start()

	# Start video file writer (main "consumer" process)
	writer.WriteFrames(cam_params, writeQueue, stopReadQueue, stopWriteQueue)


def InitCameraProcess(readyQueue):
	# Each camera process reports here when its camera is grabbing
	unicam.SetReadyQueue(readyQueue)


def Main():
	global systems
	with HandleKeyboardInterrupt():
		# Acquire cameras in parallel with Windows- and Linux-compatible pool
		ctx = mp.get_context("spawn")
		readyQueue = ctx.Queue()
		p = ctx.Pool(params["numCams"], initializer=InitCameraProcess, initargs=(readyQueue,))
		result = p.map_async(AcquireOneCamera, range(params["numCams"]))

		# Start the trigger only once every camera is grabbing, so that every
		# camera's first frame is the trigger's first pulse. The serial port
		# opens meanwhile: the Arduino needs a few seconds after it opens.
		systems = trigger.OpenTriggers(systems, params)
		ready, missing = unicam.WaitForCameras(readyQueue, params, result)
		if ready and not result.ready(): # not if the cameras already stopped (Ctrl-C)
			systems = trigger.StartTriggers(systems, params)
		trigger.SaveStart(params, ready, missing, systems.get("triggerStartedNs"))
		result.get()

	CloseSystems(systems, params)

# Open systems, creates global 'systems' and 'params' variables
systems, params = OpenSystems()