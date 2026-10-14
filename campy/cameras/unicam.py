"""
Unicam unifies camera APIs with common syntax to simplify multi-camera acquisition and 
reduce redundancy in campy code.
"""

import os, sys, time, csv, logging
import numpy as np
from collections import deque
from scipy import io as sio


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
		print('Camera make is not supported by CamPy. Check config.', flush=True)
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

		print("Opened {}: {} {} serial# {}".format( \
			cam_params["cameraName"],
			cam_params["cameraMake"], 
			cam_params["cameraModel"],
			cam_params["cameraSerialNo"]))

	except Exception as e:
		logging.error("Caught error at cameras/unicam.py OpenCamera: {}".format(e))
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
	grabdata["timeStamp"] = []
	grabdata["hostTime"] = [] # the computer's clock when each frame arrived (campy-align)
	grabdata["frameNumber"] = []
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
	# queued. Its frame number is left out of grabdata: frametimes.npy keeps one
	# row per video frame, and the gap in frameNumber marks the drop.
	grabdata["framesDropped"] += 1
	now = time.perf_counter()
	if now - grabdata["lastDropReport"] >= 5:
		grabdata["lastDropReport"] = now
		print("{}: the encoder is {} frames behind. Dropped {} frames so far (latest: frame {})."\
			.format(grabdata["cameraName"], maxQueue, grabdata["framesDropped"], frameNumber), flush=True)


def StartGrabbing(camera, cam_params, cam):
	grabbing = cam.StartGrabbing(camera)
	if grabbing:
		print(cam_params["cameraName"], "ready to trigger.")
	return grabbing


def CountFPS(grabdata, frameNumber, timeStamp):
	if frameNumber % grabdata["chunkLengthInFrames"] == 0:
		timeElapsed = timeStamp - grabdata["timeStamp"][0]
		fpsCount = round((frameNumber - 1) / timeElapsed, 1)
		print('{} collected {} frames at {} fps for {} sec.'\
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
				grabdata['frameNumber'].append(frameNumber)
				grabdata['timeStamp'].append(timeStamp)
				grabdata['hostTime'].append(hostTime)
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


def SaveMetadata(cam_params, grabdata):
	full_folder_name = os.path.join(cam_params["videoFolder"], cam_params["cameraName"])

	try:
		# Zero timeStamps
		timeFirstGrab = grabdata["timeStamp"][0]
		grabdata["timeStamp"] = [i - timeFirstGrab for i in grabdata["timeStamp"]]

		# Get the frame and time counts to save into metadata
		frame_count = grabdata.get('framesGrabbed', grabdata['frameNumber'][-1])
		time_count = grabdata['timeStamp'][-1]
		fps_count = int(round(frame_count/time_count))
		print('{} saved {} frames at {} fps.'.format(cam_params["cameraName"], frame_count, fps_count))
		if grabdata["framesDropped"]:
			print('{} dropped {} of them: the encoder fell more than {} s behind (writeQueueMaxSec).'\
				.format(cam_params["cameraName"], grabdata["framesDropped"], cam_params["writeQueueMaxSec"]))

		meta = cam_params

		# Save frame data to numpy file
		npy_filename = os.path.join(full_folder_name, 'frametimes.npy')
		x = np.array([grabdata['frameNumber'], grabdata['timeStamp']])
		np.save(npy_filename,x)

		# When each of those frames reached the computer (time.monotonic_ns, one
		# clock for every camera process): campy-align compares cameras with it
		np.save(os.path.join(full_folder_name, 'hosttimes.npy'), np.array(grabdata['hostTime'], dtype=np.int64))

		# Also save frame data to MATLAB file
		mat_filename = os.path.join(full_folder_name, 'frametimes.mat')
		matdata = {};
		matdata['frameNumber'] = grabdata['frameNumber']
		matdata['timeStamp'] = grabdata['timeStamp']
		sio.savemat(mat_filename, matdata, do_compression=True)

		# Save parameters and recording metadata to csv spreadsheet
		csv_filename = os.path.join(full_folder_name, 'metadata.csv')
		meta['totalFrames'] = frame_count
		meta['totalTime'] = grabdata['timeStamp'][-1]
		meta['framesWritten'] = len(grabdata['frameNumber'])
		meta['framesDropped'] = grabdata['framesDropped']
		
		with open(csv_filename, 'w', newline='') as f:
			w = csv.writer(f, delimiter=',', quoting=csv.QUOTE_ALL)
			for row in meta.items():
				# Print items that are not objects or dicts
				if isinstance(row[1],(list,str,int,float)):
					w.writerow(row)

		print('Saved metadata for {}.'.format(cam_params['cameraName']))

	except Exception as e:
		logging.error('Caught exception: {}'.format(e))


def CloseSystems(systems, params):
	print('Closing systems...')
	makes = GetMakeList(params)
	for m in range(len(makes)):
		cam = ImportCam(makes[m])
		cam.CloseSystem(systems[makes[m]]["system"], systems[makes[m]]["deviceList"])
	print('Exiting campy...')
