"""
"""
import pypylon.pylon as pylon
import pypylon.genicam as geni
from campy.cameras import unicam
from campy.utils.utils import Say
import os, sys, time, logging
import numpy as np
from collections import deque


def LoadSystem(params):

	return pylon.TlFactory.GetInstance()


def GetDeviceList(system):

	return system.EnumerateDevices()


def LoadDevice(systems, params, cam_params):
	# system = params["systems"]["basler"]["system"]
	system = systems["basler"]["system"]
	cam_params["camera"] = system.CreateDevice(cam_params["device"])
	return cam_params


def GetSerialNumber(device):

	return device.GetSerialNumber()


def GetModelName(camera):

	return camera.GetDeviceInfo().GetModelName()


def GetFirmwareVersion(camera):
	# The camera's firmware (DeviceInfo's DeviceVersion), for metadata.csv and session.json
	try:
		return str(camera.GetDeviceInfo().GetDeviceVersion()) or "None"
	except Exception:
		return "None"


def OpenCamera(cam_params):
	# Open the camera
	camera = pylon.InstantCamera(cam_params["camera"])
	camera.Open()

	# Load default camera settings
	cam_params['cameraModel'] = GetModelName(camera)
	cam_params['cameraFirmware'] = GetFirmwareVersion(camera)
	cam_params = LoadSettings(cam_params, camera)

	return camera, cam_params


def LoadSettings(cam_params, camera):
	# Load settings from Pylon features file
	pylon.FeaturePersistence.Load(cam_params['cameraSettings'], camera.GetNodeMap(), False) #Validation is false
	camera.MaxNumBuffer = cam_params["bufferSize"] # default bufferSize is ~500 frames

	# Manual override settings
	if cam_params["cameraTrigger"] == "Software" or cam_params["cameraTrigger"] == "software":
		camera.TriggerMode.SetValue('Off')
		camera.AcquisitionFrameRateEnable.SetValue(True)
		camera.AcquisitionFrameRate.SetValue(cam_params["frameRate"])
	
	# The frame size cameraSettings gave the camera, for the writer and metadata.
	# Known only once the camera is open: the writer waits for it.
	return unicam.SetFrameSize(cam_params, camera.Width.GetValue(), camera.Height.GetValue(),
		"the camera's frame size with its cameraSettings")


def StartGrabbing(camera):
	try:
		camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
		return True
	except Exception:
		return False


def GrabFrame(camera, frameNumber):
	try:
		return camera.RetrieveResult(0, pylon.TimeoutHandling_ThrowException)
	except Exception as e:
		# No frame yet, unless pylon says the camera is gone (unplugged)
		if Removed(camera):
			raise unicam.CameraLost("pylon says the camera was removed: {}".format(e))
		raise


def Removed(camera):
	try:
		return bool(camera.IsCameraDeviceRemoved())
	except Exception:
		return False


def GetImageArray(grabResult):

	return grabResult.Array


def GetTimeStamp(grabResult):

	return grabResult.TimeStamp*1e-9


def DisplayImage(cam_params, dispQueue, grabResult):
	# On Windows, Pylon's own image window; elsewhere campy's (display.py)
	if sys.platform == 'win32':
		dispQueue.SetImage(grabResult)
		dispQueue.Show()
	else:
		# If pixelformat is bayer, first convert result to RGB
		if cam_params["pixelFormatInput"].find("bayer") != -1:
			converter = pylon.ImageFormatConverter()
			converter.OutputPixelFormat = pylon.PixelType_RGB8packed
			img = converter.Convert(grabResult).GetArray()
		else:
			img = grabResult.GetArray()

		# Downsample image
		img = img[::cam_params["displayDownsample"],::cam_params["displayDownsample"]]

		# Send image to display window thru queue
		dispQueue.append(img)


def ReleaseFrame(grabResult):

	grabResult.Release()


def CloseCamera(cam_params, camera):
	Say('Closing {}... Please wait.'.format(cam_params["cameraName"]))
	# Close Basler camera after acquisition stops. Each step whatever the one
	# before did, and what fails is said, not raised (flir.CloseCamera).
	for step, close in (("StopGrabbing", camera.StopGrabbing), ("Close", camera.Close)):
		try:
			close()
		except Exception as e:
			Say("{}: {} failed as it closed: {}".format(cam_params["cameraName"], step, e))


def CloseSystem(system, device_list):
	del system
	del device_list


# Basler-Specific Functions
def OpenPylonImageWindow(cam_params):
	imageWindow = pylon.PylonImageWindow()
	imageWindow.Create(cam_params["n_cam"])
	imageWindow.Show()
	return imageWindow
