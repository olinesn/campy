"""
Unicam unifies camera APIs with common syntax to simplify multi-camera acquisition and 
reduce redundancy in campy code.
"""

import os, sys, time, csv, queue, logging
import numpy as np
from collections import deque
from scipy import io as sio
from campy import frametimes
from campy.utils.utils import Say


# Set in each camera process (campy.InitCameraProcess): where it reports that its
# camera is ready for triggers, so campy starts the trigger only after all are.
_readyQueue = None


def SetReadyQueue(readyQueue):
	global _readyQueue
	_readyQueue = readyQueue


def SignalReady(cam_params, ok=True, error=""):
	# Tell campy's main process this camera is grabbing, or that it never will
	if _readyQueue is not None:
		_readyQueue.put((cam_params["n_cam"], cam_params["cameraName"], ok, error))


def WaitForCameras(readyQueue, params, result=None, log=Say):
	"""
	Waits until every camera has reported ready for triggers, or failed, for at
	most readyTimeoutSec, and stops early if the camera processes end (result
	is the pool's AsyncResult). Returns the cameras that are ready, and a reason
	for each that is not.
	"""
	names = params["cameraNames"]
	pending = {n: names[n] for n in range(params["numCams"])}
	ready, missing = [], {}
	timeout = float(params["readyTimeoutSec"])
	deadline = time.monotonic() + timeout
	while pending:
		try:
			n_cam, name, ok, error = readyQueue.get(timeout=0.2)
		except queue.Empty:
			if result is not None and result.ready():
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
		missing[name] = "stopped before it was ready" if stopped else \
			"not ready after {:g} s (readyTimeoutSec)".format(timeout)

	if not missing:
		log("{} ready.".format("The camera is" if len(ready) == 1 else "All {} cameras are".format(len(ready))))
	else:
		log("{} of {} cameras are ready. Not ready: {}.".format(len(ready), params["numCams"],
			"; ".join("{} ({})".format(name, why) for name, why in missing.items())))
	return ready, missing


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


def OpenCamera(cam_params, stopWriteQueue):
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
		stopWriteQueue.append('STOP')

	return cam, camera, cam_params


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
		grabdata["frameRatio"] = cam_params["frameRate"]

	# Calculate number of images and chunk length
	grabdata["numImagesToGrab"] = int(round(cam_params["recTimeInSec"]*cam_params["frameRate"]))
	grabdata["chunkLengthInFrames"] = int(round(cam_params["chunkLengthInSec"]*cam_params["frameRate"]))

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
	SignalReady(cam_params, bool(grabbing))
	return grabbing


def CountFPS(grabdata, frameNumber, timeStamp):
	if frameNumber % grabdata["chunkLengthInFrames"] == 0:
		timeElapsed = timeStamp - grabdata["frameTimes"].first
		fpsCount = round((frameNumber - 1) / timeElapsed, 1)
		Say('{} collected {} frames at {} fps for {} sec.'\
			.format(grabdata["cameraName"], frameNumber, fpsCount, round(timeElapsed)))


def GrabFrames(cam_params, writeQueue, dispQueue, stopReadQueue, stopWriteQueue):
	# Open the camera object
	cam, camera, cam_params = OpenCamera(cam_params, stopWriteQueue)

	# Use Basler's default display window on Windows. Not supported on Linux
	if sys.platform=='win32' and cam_params["cameraMake"] == 'basler':
		dispQueue = cam.OpenPylonImageWindow(cam_params)

	# Create dictionary for appending frame number and timestamp information
	grabdata = GrabData(cam_params)
	maxQueue = MaxQueueFrames(cam_params)

	# Start grabbing frames from the camera
	grabbing = StartGrabbing(camera, cam_params, cam)

	# The camera's settings, now, so that a recording that stops without
	# closing (SaveMetadata) still has them; SaveMetadata adds the totals
	if grabbing:
		WriteMetadata(cam_params)

	frameNumber = 0
	while(not stopReadQueue):
		try:
			# Grab image from camera buffer if available
			grabResult = cam.GrabFrame(camera, frameNumber)
			hostTime = time.monotonic_ns()
			img = cam.GetImageArray(grabResult)
			frameNumber += 1 # first frame = 1
			timeStamp = cam.GetTimeStamp(grabResult)

			# Queue the frame for the writer, with its frameNumber and timeStamp,
			# unless the writer is writeQueueMaxSec behind
			if maxQueue is None or len(writeQueue) < maxQueue:
				writeQueue.append(img)
				grabdata["frameTimes"].Add(frameNumber, timeStamp, hostTime)
			else:
				DropFrame(grabdata, frameNumber, maxQueue)

			# Display converted, downsampled image in the Window
			if frameNumber % grabdata["frameRatio"] == 0:
				img = cam.DisplayImage(cam_params, dispQueue, grabResult)

			CountFPS(grabdata, frameNumber, timeStamp)

			cam.ReleaseFrame(grabResult)

			if frameNumber >= grabdata["numImagesToGrab"]:
				break

		except Exception as e:
			if cam_params["cameraDebug"]:
				logging.error('Caught exception at cameras/unicam.py GrabFrames: {}'.format(e))
			time.sleep(0.001)

	# Close the camaera, save metadata, and tell writer and display to close
	grabdata["framesGrabbed"] = frameNumber
	cam.CloseCamera(cam_params, camera)
	SaveMetadata(cam_params, grabdata)
	if not sys.platform=='win32' or not cam_params['cameraMake'] == 'basler':
		dispQueue.append('STOP')
	stopWriteQueue.append('STOP')


def CameraFolder(cam_params):
	return os.path.join(cam_params["videoFolder"], cam_params["cameraName"])


def WriteMetadata(cam_params):
	# metadata.csv: the camera's parameters, one per row (not the objects)
	folder = CameraFolder(cam_params)
	os.makedirs(folder, exist_ok=True)
	with open(os.path.join(folder, "metadata.csv"), "w", newline="") as f:
		w = csv.writer(f, delimiter=",", quoting=csv.QUOTE_ALL)
		for row in cam_params.items():
			if isinstance(row[1], (list, str, int, float)):
				w.writerow(row)


def SaveMetadata(cam_params, grabdata):
	full_folder_name = CameraFolder(cam_params)
	times = grabdata["frameTimes"]

	try:
		# The last frames into frametimes.npy and hosttimes.npy
		times.Close()
		if not times.count:
			Say("{} saved no frames.".format(cam_params["cameraName"]))
			return

		# Get the frame and time counts to save into metadata
		frame_count = grabdata["framesGrabbed"]
		time_count = times.last
		fps_count = int(round(frame_count/time_count)) if time_count > 0 else 0
		Say('{} saved {} frames at {} fps.'.format(cam_params["cameraName"], frame_count, fps_count))
		if grabdata["framesDropped"]:
			Say('{} dropped {} of them: the encoder fell more than {} s behind (writeQueueMaxSec).'\
				.format(cam_params["cameraName"], grabdata["framesDropped"], cam_params["writeQueueMaxSec"]))

		# Also save frame data to MATLAB file
		x = np.load(os.path.join(full_folder_name, 'frametimes.npy'))
		mat_filename = os.path.join(full_folder_name, 'frametimes.mat')
		matdata = {}
		matdata['frameNumber'] = x[0].astype(int) # as integers, as they were
		matdata['timeStamp'] = x[1]
		sio.savemat(mat_filename, matdata, do_compression=True)

		# Save parameters and recording metadata to csv spreadsheet
		cam_params['totalFrames'] = frame_count
		cam_params['totalTime'] = time_count
		cam_params['framesWritten'] = times.count
		cam_params['framesDropped'] = grabdata['framesDropped']
		WriteMetadata(cam_params)

		Say('Saved metadata for {}.'.format(cam_params['cameraName']))

	except Exception as e:
		logging.error('Caught exception: {}'.format(e))


def CloseSystems(systems, params):
	Say('Closing systems...')
	makes = GetMakeList(params)
	for m in range(len(makes)):
		cam = ImportCam(makes[m])
		cam.CloseSystem(systems[makes[m]]["system"], systems[makes[m]]["deviceList"])
	Say('Exiting campy...')
