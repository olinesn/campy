"""
"""
import PySpin
from campy.cameras import unicam
import os, io, sys, time, logging, contextlib
import numpy as np


os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"


class ImageNotCompleteException(Exception):
	"""Exception raised for errors in the input.
	Attributes:
		expression -- input expression in which the error occurred
		message -- explanation of the error
	"""

	def __init__(self, expression, message):
		self.expression = expression
		self.message = message


def LoadSystem(params):

	return PySpin.System.GetInstance()


def GetDeviceList(system):

	return system.GetCameras()


def LoadDevice(systems, params, cam_params):
	cam_params["camera"] = cam_params["device"]
	return cam_params


def GetSerialNumber(device):
	node_device_serial_number = PySpin.CStringPtr(device.GetTLDeviceNodeMap().GetNode("DeviceSerialNumber"))
	if PySpin.IsAvailable(node_device_serial_number) and PySpin.IsReadable(node_device_serial_number):
		device_serial_number = node_device_serial_number.GetValue()
	else:
		device_serial_number = []

	return device_serial_number


def GetModelName(camera):
	node_device_model_name = PySpin.CStringPtr(camera.GetTLDeviceNodeMap().GetNode("DeviceModelName"))
	if PySpin.IsAvailable(node_device_model_name) and PySpin.IsReadable(node_device_model_name):
		device_model_name = node_device_model_name.GetValue()
	else:
		device_model_name = []

	return device_model_name


def OpenCamera(cam_params):

	# Load camera
	camera = cam_params["camera"]

	# Initialize camera object
	camera.Init()

	# Reset to factory default settings
	# camera.UserSetSelector.SetValue(PySpin.UserSetSelector_Default)
	# camera.UserSetLoad()

	# Retrieve TL device nodemap
	nodemap_tldevice = camera.GetTLDeviceNodeMap()

	# Print device information
	PrintDeviceInfo(nodemap_tldevice, cam_params)
	cam_params['cameraModel'] = GetModelName(camera)

	# Load camera settings
	cam_params = LoadSettings(camera, cam_params)

	return camera, cam_params


def LoadSettings(camera, cam_params):

	try:
		# Configure trigger
		cam_params = ConfigureTrigger(camera, cam_params)

		# Configure custom image settings (acquisition, white balance, frame height/width/offsets, 
		# pixel format, exposure, gain, gamma, buffer and chunkdata mode (e.g. timestamp/frame info))
		cam_params = ConfigureCustomImageSettings(camera, cam_params)

		# The USB link's limit, for the frame size and pixel format set above
		cam_params = ConfigureLinkLimit(camera, cam_params)

		# A free-running camera's frame rate, last: the most it can reach depends
		# on the frame size, pixel format and exposure set above
		cam_params = ConfigureFrameRate(camera, cam_params)

	except Exception as e:
		logging.error("Caught error at cameras/flir.py LoadSettings: {}".format(e))

	return cam_params


def FrameRateLimits(device, cam_params):
	"""
	For the launch check (campy/preflight.py): opens the camera, applies
	campy's settings to it as a recording does (trigger, frame size, pixel
	format, exposure), reads what limits its frame rate, and closes it.
	  fps: the highest frame rate it can run at with those settings
	       (AcquisitionResultingFrameRate: the USB limit, the exposure and the
	       sensor's readout all count)
	  frameBytes, throughputLimit: bytes per frame, and the USB link's limit in
	       bytes/s (DeviceLinkThroughputLimit; None where the camera has none)
	  exposureUs, frameRateCap: the exposure, and AcquisitionFrameRate when
	       AcquisitionFrameRateEnable caps the rate (else None)
	A free-running camera is read without the cap campy gives it (frameRate,
	ConfigureFrameRate), so fps is the most it can do, and it is closed with
	the frame rate settings it had.
	"""
	camera = device
	camera.Init()
	settings = None
	try:
		with contextlib.redirect_stdout(io.StringIO()): # the Configure* progress lines
			settings = LoadSettings(camera, dict(cam_params))
		nodemap = camera.GetNodeMap()
		switch = FrameRateSwitch(nodemap)
		if FreeRuns(cam_params) and switch is not None and PySpin.IsWritable(switch):
			switch.SetValue(False)

		enabled = switch is not None and switch.GetValue()
		return {
			"fps": ReadNode(nodemap, "AcquisitionResultingFrameRate", PySpin.CFloatPtr),
			"frameBytes": ReadNode(nodemap, "PayloadSize", PySpin.CIntegerPtr),
			"throughputLimit": ReadNode(nodemap, "DeviceLinkThroughputLimit", PySpin.CIntegerPtr),
			"exposureUs": ReadNode(nodemap, "ExposureTime", PySpin.CFloatPtr),
			"frameRateCap": ReadNode(nodemap, "AcquisitionFrameRate", PySpin.CFloatPtr) if enabled else None,
			}
	finally:
		if settings is not None:
			RestoreFrameRate(camera, settings)
		camera.DeInit()


def StartGrabbing(camera):
	try:
		camera.BeginAcquisition()
		return True
	except PySpin.SpinnakerException as e:
		print("Error: {}".format(e))
		return False
	except Exception as e:
		print("Exception in cameras/flir.py function StartGrabbing(camera): {}".foramt(e))
		return False


def GrabFrame(camera, frameNumber):
	image_result = camera.GetNextImage(50) # timeout to wait for next frame, in ms

	#  Ensure image completion
	if image_result.IsIncomplete():
		image_status = image_result.GetImageStatus()
		print("Image incomplete with image status %d ..." % image_status)
		raise ImageNotCompleteException("Image not complete", image_status)

	return image_result


def GetImageArray(grabResult):

	return grabResult.GetNDArray()


def GetTimeStamp(grabResult):

	return grabResult.GetChunkData().GetTimestamp() * 1e-9


def GetFrameID(chunkData):

	return chunkData.GetFrameID()


_imageProcessor = None

def ConvertToRGB(image):
	# Spinnaker 3 and newer (e.g. PySpin 4.4, the one for Python 3.12) convert
	# with an ImageProcessor; older PySpin has only the image's own Convert.
	global _imageProcessor
	if hasattr(PySpin, "ImageProcessor"):
		if _imageProcessor is None:
			_imageProcessor = PySpin.ImageProcessor()
			_imageProcessor.SetColorProcessing(PySpin.SPINNAKER_COLOR_PROCESSING_ALGORITHM_HQ_LINEAR)
		return _imageProcessor.Convert(image, PySpin.PixelFormat_RGB8)
	return image.Convert(PySpin.PixelFormat_RGB8, PySpin.HQ_LINEAR)


def DisplayImage(cam_params, dispQueue, grabResult):
	try:
		if cam_params["pixelFormatInput"] == "bayer_rggb8" \
		or cam_params["pixelFormatInput"] == "bayer_bggr8":
			# Demosaic to RGB
			grabResult = ConvertToRGB(grabResult)

		# Convert to Numpy array
		img = GetImageArray(grabResult)

		# Downsample image
		img = img[::cam_params["displayDownsample"], ::cam_params["displayDownsample"]]

		# The display shows RGB: repeat a mono image's one channel
		if img.ndim == 2:
			img = np.repeat(img[:, :, np.newaxis], 3, axis=2)

		# Send to display queue
		dispQueue.append(img)

	except Exception as e:
		logging.error('Caught exception at cameras/flir.py DisplayImage: {}.'.format(e))


def ReleaseFrame(grabResult):

	grabResult.Release()


def CloseCamera(cam_params, camera):
	print("Closing {}... Please wait.".format(cam_params["cameraName"]))
	# Close camera after acquisition stops
	while True:
		try:
			try:
				# Close camera
				camera.EndAcquisition()
				RestoreFrameRate(camera, cam_params)
				camera.DeInit()
				del camera
				break
			except PySpin.SpinnakerException as e:
				print("Error: {}".format(e))
				raise
			except Exception as e:
				logging.error("Caught exception at cameras/flir.py CloseCamera: {}".format(e))
				raise
				break
		except KeyboardInterrupt:
			break


def CloseSystem(system, device_list):
	try:
		device_list.Clear()
		system.ReleaseInstance()
	except PySpin.SpinnakerException as e:
		print("SpinnakerException at cameras/flir.py CloseSystem: {}".format(e))
		print("passing from", __name__)
		raise
	except Exception as e:
		print("Exception at cameras/flir.py CloseSystem: {}".format(e))
		raise


# Flir-Specific Functions
def ConfigureCustomImageSettings(camera, cam_params):
	"""
	Configures a number of settings on the camera including offsets X and Y, width,
	height, and pixel format. These settings must be applied before BeginAcquisition()
	is called; otherwise, they will be read only. Also, it is important to note that
	settings are applied immediately. This means if you plan to reduce the width and
	move the x offset accordingly, you need to apply such changes in the appropriate order.
	:param nodemap: GenICam nodemap.
	:type nodemap: INodeMap
	:return: True if successful, False otherwise.
	:rtype: bool
	"""

	try:
		if cam_params["cameraDebug"]:
			print("\n*** CONFIGURING CUSTOM IMAGE SETTINGS *** \n")

		settingsConfig = True

		camera.AcquisitionMode.SetValue(PySpin.AcquisitionMode_Continuous)

		# White Balance not supported for monochrome cameras
		try:
			camera.BalanceWhiteAuto.SetValue(PySpin.BalanceWhiteAuto_Off)
		except:
			print('Could not set white balance')

		cam_params = ConfigureFrameWidth(camera, cam_params)
		cam_params = ConfigureFrameHeight(camera, cam_params)
		cam_params = ConfigurePixelFormat(camera, cam_params)
		cam_params = ConfigureExposure(camera, cam_params)
		cam_params = ConfigureGain(camera, cam_params)
		cam_params = ConfigureGamma(camera, cam_params)
		cam_params = ConfigureBuffer(camera, cam_params)
		cam_params = ConfigureChunkData(camera, cam_params)

	except Exception as e:
		logging.error("Caught error at cameras/flir.py ConfigureCustomImageSettings: {}".format(e))
		settingsConfig = False

	cam_params["settingsConfig"] = settingsConfig

	return cam_params


def ConfigureBuffer(camera, cam_params):
	"""
	This function configures the camera's frame buffer settings.
	"""

	try:
		# Retrieve Stream Parameters device nodemap
		s_node_map = camera.GetTLStreamNodeMap()

		# Retrieve Buffer Handling Mode Information
		handling_mode = PySpin.CEnumerationPtr(s_node_map.GetNode("StreamBufferHandlingMode"))
		if not PySpin.IsAvailable(handling_mode) or not PySpin.IsWritable(handling_mode):
			print("Unable to set Buffer Handling mode (node retrieval). Aborting...")
			return cam_params

		handling_mode_entry = PySpin.CEnumEntryPtr(handling_mode.GetCurrentEntry())
		if not PySpin.IsAvailable(handling_mode_entry) or not PySpin.IsReadable(handling_mode_entry):
			print("Unable to set Buffer Handling mode (Entry retrieval). Aborting...")
			return cam_params

		# Set stream buffer Count Mode to manual
		stream_buffer_count_mode = PySpin.CEnumerationPtr(s_node_map.GetNode("StreamBufferCountMode"))
		if not PySpin.IsAvailable(stream_buffer_count_mode) or not PySpin.IsWritable(stream_buffer_count_mode):
			print("Unable to set Buffer Count Mode (node retrieval). Aborting...")
			return cam_params

		stream_buffer_count_mode_manual = PySpin.CEnumEntryPtr(stream_buffer_count_mode.GetEntryByName("Manual"))
		if not PySpin.IsAvailable(stream_buffer_count_mode_manual) or not PySpin.IsReadable(
				stream_buffer_count_mode_manual):
			print("Unable to set Buffer Count Mode entry (Entry retrieval). Aborting...")
			return cam_params

		stream_buffer_count_mode.SetIntValue(stream_buffer_count_mode_manual.GetValue())

		# Retrieve and modify Stream Buffer Count
		buffer_count = PySpin.CIntegerPtr(s_node_map.GetNode("StreamBufferCountManual"))
		if not PySpin.IsAvailable(buffer_count) or not PySpin.IsWritable(buffer_count):
			print("Unable to set Buffer Count (Integer node retrieval). Aborting...")
			return cam_params

		# Display Buffer Info
		buffer_count.SetValue(cam_params["bufferSize"])
		print("Buffer count now set to: %d" % buffer_count.GetValue())

		if cam_params["bufferMode"] == "OldestFirst":
			handling_mode_entry = handling_mode.GetEntryByName("OldestFirst")
		elif cam_params["bufferMode"] == "NewestFirst":
			handling_mode_entry = handling_mode.GetEntryByName("NewestFirst")
		elif cam_params["bufferMode"] == "NewestOnly":
			handling_mode_entry = handling_mode.GetEntryByName("NewestOnly")
		elif cam_params["bufferMode"] == "OldestFirstOverwrite":
			handling_mode_entry = handling_mode.GetEntryByName("OldestFirstOverwrite")
		else:
			print("BufferMode should be 'OldestFirst', 'NewestFirst', 'NewestOnly' or 'OldestFirstOverwrite'")
			return cam_params

		handling_mode.SetIntValue(handling_mode_entry.GetValue())
		print("BufferMode has been set to {}".format(handling_mode_entry.GetDisplayName()))

		cam_params["bufferSize"] = stream_buffer_count_mode_manual.GetValue()
		cam_params["bufferMode"] = handling_mode_entry.GetDisplayName()
	
	except Exception as e:
		logging.error('Caught exeption at cameras/flir.py ConfigureBuffer: {}'.format(e))

	return cam_params


def ConfigureChunkData(camera, cam_params):
	"""
	This function configures the camera to add chunk data to each image. It does
	this by enabling each type of chunk data before enabling chunk data mode.
	When chunk data is turned on, the data is made available in both the nodemap
	and each image.
	:param nodemap: Transport layer device nodemap.
	:type nodemap: INodeMap
	:return: True if successful, False otherwise
	:rtype: bool
	"""

	# ToDo: Only enable requested chunks (eg. Timestamp and FrameID) for faster execution and lower memory print
	
	try:
		if cam_params["cameraDebug"]:
			print("\n*** CONFIGURING CHUNK DATA ***\n")

		# Activate chunk mode
		#
		# *** NOTES ***
		# Once enabled, chunk data will be available at the end of the payload
		# of every image captured until it is disabled. Chunk data can also be
		# retrieved from the nodemap.

		nodemap = camera.GetNodeMap()
		chunk_mode_active = PySpin.CBooleanPtr(nodemap.GetNode("ChunkModeActive"))

		if PySpin.IsAvailable(chunk_mode_active) and PySpin.IsWritable(chunk_mode_active):
			chunk_mode_active.SetValue(True)

		# Enable all types of chunk data
		#
		# *** NOTES ***
		# Enabling chunk data requires working with nodes: "ChunkSelector"
		# is an enumeration selector node and "ChunkEnable" is a boolean. It
		# requires retrieving the selector node (which is of enumeration node
		# type), selecting the entry of the chunk data to be enabled, retrieving
		# the corresponding boolean, and setting it to be true.
		#
		# In this example, all chunk data is enabled, so these steps are
		# performed in a loop. Once this is complete, chunk mode still needs to
		# be activated.
		chunk_selector = PySpin.CEnumerationPtr(nodemap.GetNode("ChunkSelector"))

		if not PySpin.IsAvailable(chunk_selector) or not PySpin.IsReadable(chunk_selector):
			print("Unable to retrieve chunk selector. Aborting...")
			return cam_params

		# Retrieve entries
		#
		# *** NOTES ***
		# PySpin handles mass entry retrieval in a different way than the C++
		# API. Instead of taking in a NodeList_t reference, GetEntries() takes
		# no parameters and gives us a list of INodes. Since we want these INodes
		# to be of type CEnumEntryPtr, we can use a list comprehension to
		# transform all of our collected INodes into CEnumEntryPtrs at once.
		entries = [PySpin.CEnumEntryPtr(chunk_selector_entry) for chunk_selector_entry in chunk_selector.GetEntries()]

		# Retrieve corresponding boolean
		chunk_enable = PySpin.CBooleanPtr(nodemap.GetNode("ChunkEnable"))

		# Iterate through our list and select each entry node to enable
		for chunk_selector_entry in entries:
			if PySpin.IsAvailable(chunk_selector_entry) and PySpin.IsReadable(chunk_selector_entry):
				chunk_selector.SetIntValue(chunk_selector_entry.GetValue())
				chunk_name = chunk_selector_entry.GetSymbolic()
				chunk_str = "{}:".format(chunk_name)

				if PySpin.IsWritable(chunk_enable):
					if chunk_name=="FrameID" or chunk_name=="Timestamp":
						chunk_enable.SetValue(True)
					else:
						chunk_enable.SetValue(False)

				if cam_params["cameraDebug"]:
					if chunk_enable.GetValue() and PySpin.IsWritable(chunk_enable):
						print("{} enabled".format(chunk_str))
					elif not chunk_enable.GetValue() and PySpin.IsWritable(chunk_enable):
						print("{} disabled".format(chunk_str))
					else:
						print("{} not available".format(chunk_str))

	except Exception as e:
		logging.error("Caught error at cameras/flir.py at ConfigureChunkData: {}".format(e))

	return cam_params


def ConfigureExposure(camera, cam_params):
	"""
	This function configures a custom exposure time. Automatic exposure is turned off in order to allow for the
	customization, and then the custom setting is applied.
	 :param cam: Camera to configure exposure for.
	 :type cam: CameraPtr
	 :param exposure_time: exposure time in microseconds
	 :type exposure_time: int
	 :return: True if successful, False otherwise.
	 :rtype: bool
	"""
	
	try:
		if cam_params["cameraDebug"]:
			print("*** CONFIGURING EXPOSURE ***\n")
		# Turn off automatic exposure mode
		#
		# *** NOTES *** Automatic exposure prevents the manual configuration of exposure times and needs to be turned
		# off. Enumerations representing entry nodes have been added to QuickSpin. This allows for the much easier
		# setting of enumeration nodes to new values.
		#
		# The naming convention of QuickSpin enums is the name of the enumeration node followed by an underscore and
		# the symbolic of the entry node. Selecting "Off" on the "ExposureAuto" node is thus named "ExposureAuto_Off".

		if camera.ExposureAuto.GetAccessMode() != PySpin.RW:
			print("Unable to disable automatic exposure. Aborting...")
			return cam_params

		camera.ExposureAuto.SetValue(PySpin.ExposureAuto_Off)

		# Set exposure time manually; exposure time recorded in microseconds
		#
		# *** NOTES *** Notice that the node is checked for availability and writability prior to the setting of the
		# node. In QuickSpin, availability and writability are ensured by checking the access mode.
		#
		# Further, it is ensured that the desired exposure time does not exceed the maximum. Exposure time is counted
		# in microseconds - this can be found out either by retrieving the unit with the GetUnit() method or by
		# checking SpinView.

		if camera.ExposureTime.GetAccessMode() != PySpin.RW:
			print("Unable to set exposure time. Aborting...")
			return cam_params

		# Ensure desired exposure time does not exceed the maximum
		exposure_time_to_set = cam_params["cameraExposureTimeInUs"]
		exposure_time_to_set = min(camera.ExposureTime.GetMax(), exposure_time_to_set)
		camera.ExposureTime.SetValue(exposure_time_to_set)
		print("Shutter time set to {} us...".format(exposure_time_to_set))
		cam_params["exposureTimeInUs"] = exposure_time_to_set

	except Exception as e:
		logging.error("Caught error at cameras/flir.py ConfigureExposure: {}".format(e))

	return cam_params


def ConfigureFrameHeight(camera, cam_params):
	"""
	This function configures the frame height to be captured in the region of interest. 
	Value is available to the user in the config.yaml:
	frameHeight: X
	Y offset is limited and centered automatically.
	"""
	try:
		# Set frame height
		nodemap = camera.GetNodeMap()

		height_to_set = cam_params["frameHeight"]
		if height_to_set % 16 != 0:
			while height_to_set % 16 != 0:
				height_to_set += 1
			cam_params["frameHeight"] = height_to_set

		# Get max offset values from the camera
		max_h = height_to_set
		try:
			max_h = PySpin.CIntegerPtr(nodemap.GetNode("Height")).GetMax()
		except Exception as e:
			logging.error("Caught exception at cameras/flir.py GetMax: {}".format(e))

		node_height = PySpin.CIntegerPtr(nodemap.GetNode("Height"))
		if PySpin.IsAvailable(node_height) and PySpin.IsWritable(node_height):
			height_to_set = cam_params["frameHeight"]
			node_height.SetValue(height_to_set)
			offset_y = int((max_h-height_to_set)/2)
			if offset_y % 4 != 0:
				while offset_y % 4 != 0:
					offset_y += 1
				print("offset_y is set to {}".format(offset_y))
			node_offset_y = PySpin.CIntegerPtr(nodemap.GetNode("OffsetY"))
			if PySpin.IsAvailable(node_offset_y) and PySpin.IsWritable(node_offset_y):
				node_offset_y.SetValue(offset_y)
				cam_params["offsetY"] = offset_y
			else:
				print("OffsetY cannot be set!")
		else:
			print("Height not available...")
		print("Height set to {}...".format(node_height.GetValue()))

	except Exception as e:
		logging.error("Caught exception at cameras/flir.py ConfigureFrameHeight: {}".format(e))

	return cam_params


def ConfigureFrameWidth(camera, cam_params):
	"""
	This function configures the frame width to be captured in the region of interest. 
	Value is available to the user in the config.yaml:
	frameWidth: X
	X offset is limited and centered automatically.
	"""
	try:

		nodemap = camera.GetNodeMap()
		# Set frame width
		width_to_set = cam_params["frameWidth"]
		if width_to_set % 16 != 0:
			while width_to_set % 16 != 0:
				width_to_set += 1
			cam_params["frameWidth"] = width_to_set

		# Get max offset values from the camera
		max_w = width_to_set
		try:
			max_w = PySpin.CIntegerPtr(nodemap.GetNode("Width")).GetMax()
		except Exception as e:
			logging.error("Caught exception at cameras/flir.py GetMax: {}".format(e))

		# *** NOTES ***
		# Other nodes, such as those corresponding to image width and height,
		# might have an increment other than 1. In these cases, it can be
		# important to check that the desired value is a multiple of the
		# increment. However, as these values are being set to the maximum,
		# there is no reason to check against the increment.
		node_width = PySpin.CIntegerPtr(nodemap.GetNode("Width"))
		if PySpin.IsAvailable(node_width) and PySpin.IsWritable(node_width):
			width_to_set = cam_params["frameWidth"]
			node_width.SetValue(width_to_set)
			print("Width set to {}...".format(node_width.GetValue()))
			offset_x = int((max_w-width_to_set)/2)
			if offset_x % 4 != 0:
				while offset_x % 4 != 0:
					offset_x += 1
				print("offset_x is set to {}".format(offset_x))
			node_offset_x = PySpin.CIntegerPtr(nodemap.GetNode("OffsetX"))
			if PySpin.IsAvailable(node_offset_x) and PySpin.IsWritable(node_offset_x):
				node_offset_x.SetValue(offset_x)
				cam_params["offsetX"] = offset_x
			else:
				print("OffsetX cannot be set!")
		else:
			print("Width not available...")

	except Exception as e:
		logging.error("Caught exception at cameras/flir.py ConfigureFrameWidth: {}".format(e))

	return cam_params


def ConfigureGain(camera, cam_params):
	"""
	This function configures the camera gain.
	:param cam: Camera to acquire images from.
	:type cam: CameraPtr
	:param gain: gain in dB
	:type gain: float
	:return: True if successful, False otherwise.
	:rtype: bool
	"""

	try:
		gain = cam_params["cameraGain"]
		if cam_params["cameraDebug"]:
			print("*** CONFIGURING GAIN ***\n")
	
		# Retrieve GenICam nodemap (nodemap)
		nodemap = camera.GetNodeMap()

		# Retrieve node
		node_gainauto_mode = PySpin.CEnumerationPtr(nodemap.GetNode("GainAuto"))
		if not PySpin.IsAvailable(node_gainauto_mode) or not PySpin.IsWritable(node_gainauto_mode):
			print("Unable to configure gain (enum retrieval). Aborting...")
			return cam_params

		# EnumEntry node (always associated with an Enumeration node)
		node_gainauto_mode_off = node_gainauto_mode.GetEntryByName("Off")
		if not PySpin.IsAvailable(node_gainauto_mode_off):
			print("Unable to configure gain (entry retrieval). Aborting...")
			return cam_params

		# Turn off Auto Gain
		node_gainauto_mode.SetIntValue(node_gainauto_mode_off.GetValue())

		# Retrieve gain node (float)
		node_gain = PySpin.CFloatPtr(nodemap.GetNode("Gain"))
		if not PySpin.IsAvailable(node_gain) or not PySpin.IsWritable(node_gain):
			print("Unable to configure gain (float retrieval). Aborting...")
			return cam_params

		max_gain = camera.Gain.GetMax()

		if gain > camera.Gain.GetMax():
			print("Max. gain is {}dB!".format(max_gain))
			gain = max_gain
		elif gain <= 0:
			gain = 0.0

		# Set gain
		node_gain.SetValue(float(gain))
		print("Gain set to {} dB.".format(gain))
		cam_params["gain"] = gain

	except Exception as e:
		logging.error("Caught error at cameras/flir.py ConfigureGain: {}".format(e))

	return cam_params


def ConfigureGamma(camera, cam_params):
	"""This function disables the gamma correction.
	:param cam: Camera to disable gamma correction.
	:type cam: CameraPtr
	"""

	try:
		if cam_params["disableGamma"]:
			if cam_params["cameraDebug"]:
				print("*** DISABLING GAMMA CORRECTION ***\n")

			# Retrieve GenICam nodemap (nodemap)
			nodemap = camera.GetNodeMap()

			# Retrieve node (boolean)
			node_gamma_enable_bool = PySpin.CBooleanPtr(nodemap.GetNode("GammaEnable"))

			if not PySpin.IsAvailable(node_gamma_enable_bool) or not PySpin.IsWritable(node_gamma_enable_bool):
				print("Unable to disable gamma (boolean retrieval). Aborting...")
				return cam_params

			# Set value to False (disable gamma correction)
			node_gamma_enable_bool.SetValue(False)
			print("Gamma correction disabled.")

	except Exception as e:
		logging.error("Caught error at cameras/flir.py at ConfigureGamma: {}".format(e))

	return cam_params


def ConfigurePixelFormat(camera, cam_params):
	"""
	This function configures the camera pixel format.
	:param cam: Camera to acquire images from.
	:type cam: CameraPtr
	:param pixelFormat: string 
	:type gain: str
	:return: True if successful, False otherwise.
	:rtype: bool
	"""
	
	try:
		pixelFormat = cam_params["pixelFormatInput"]

		if cam_params["cameraDebug"]:
			print("*** CONFIGURING PIXEL FORMAT ***\n")

		if pixelFormat == "bayer_rggb8" or pixelFormat == "bayer_bggr8":
			camera.PixelFormat.SetValue(PySpin.PixelFormat_BayerRG8)
			camera.IspEnable.SetValue(False)
		elif pixelFormat == "rgb24":
			camera.PixelFormat.SetValue(PySpin.PixelFormat_RGB8Packed)
		elif pixelFormat == "gray":
			camera.PixelFormat.SetValue(PySpin.PixelFormat_Mono8)
		else:
			eval("camera.PixelFormat.SetValue(PySpin.PixelFormat_{}".format(pixelFormat))

	except Exception as e:
		logging.error("Error setting pixel format to {}: {}".format(pixelFormat, e))

	return cam_params


def ConfigureTrigger(camera, cam_params):
	"""
	This function configures the camera to use a trigger. First, trigger mode is
	ensured to be off in order to select the trigger source. Trigger mode is
	then enabled, which has the camera capture only a single image upon the
	execution of the chosen trigger.
	 :param cam: Camera to configure trigger for.
	 :type cam: CameraPtr
	 :return: True if successful, False otherwise.
	 :rtype: bool
	"""
	try:
		cam_params["configTrig"] = False
		if cam_params["cameraDebug"]:
				print("*** CONFIGURING TRIGGER ***")

		cameraTrigger = cam_params["cameraTrigger"]

		# Ensure trigger mode off
		# The trigger must be disabled in order to configure whether the source
		# is software or hardware.
		if camera.TriggerMode.GetAccessMode() != PySpin.RW:
			print("Unable to disable trigger mode (node retrieval). Aborting...")
			return cam_params
		camera.TriggerMode.SetValue(PySpin.TriggerMode_Off)

		# Switch on the TriggerOverlap (important for high frame rates/exposures)
		camera.TriggerOverlap.SetValue(PySpin.TriggerOverlap_ReadOut)

		# Select trigger source
		# The trigger source must be set to hardware or software while trigger
		# mode is off.
		if camera.TriggerSource.GetAccessMode() != PySpin.RW:
			print("Unable to get trigger source (node retrieval). Aborting...")
			return cam_params

		# Configure trigger source
		if cameraTrigger == 'software' or cameraTrigger == 'Software' or cameraTrigger == 'SOFTWARE':
			camera.TriggerSource.SetValue(PySpin.TriggerSource_Software)
			print("Trigger source set to software...")
		elif cameraTrigger == 'none' or cameraTrigger == 'None' or cameraTrigger == 'NONE':
			camera.TriggerMode.SetValue(PySpin.TriggerMode_Off)
			print("Trigger source set to None...")
			return cam_params
		else:
			# eval("camera.TriggerSource.SetValue(PySpin.TriggerSource_%s)" % cameraTrigger)
			eval("camera.TriggerSource.SetValue(PySpin.TriggerSource_{})".format(cameraTrigger))
			print("Trigger source set to {}...".format(cameraTrigger))

		# Turn trigger mode on
		# Once the appropriate trigger source has been set, turn trigger mode
		# on in order to retrieve images using the trigger.
		camera.TriggerMode.SetValue(PySpin.TriggerMode_On)

		# cam_params["triggerSource"] = camera.TriggerMode.GetValue()
		cam_params["configTrig"] = True

	except Exception as e:
		logging.error("Caught error at cameras/flir.py ConfigureTrigger: {}".format(e))

	return cam_params


# "auto" raises a limit that is less than this share above what the frames need
LINK_LIMIT_HEADROOM = 1.1


def ConfigureLinkLimit(camera, cam_params):
	"""
	The camera's USB throughput limit (DeviceLinkThroughputLimit), from
	cameraLinkLimitMBps:
	  "auto" (default)  raised to the camera's maximum when it is below what
	                    frameRate needs (frame bytes x frameRate, plus 10%);
	                    a limit that is enough is left as it is
	  a number          that many MB/s
	  "None"            the camera's own limit, untouched
	A camera's live limit is not always the one it saved: a Blackfly S left
	plugged in through a computer's restart came up at 43 MB/s instead of its
	saved 380, which holds 1280x1024 Mono8 to 32.8 fps. A lower limit can also
	be on purpose, to share a USB bus, so "auto" keeps any limit that is
	enough. The limit found is not put back at close: unlike a frame rate cap,
	a higher limit holds no later recording back.
	"""
	setting = cam_params.get("cameraLinkLimitMBps", "auto")
	if str(setting).lower() == "none":
		return cam_params
	name = cam_params["cameraName"]
	try:
		nodemap = camera.GetNodeMap()
		node = GetNode(nodemap, "DeviceLinkThroughputLimit", PySpin.CIntegerPtr)
		if node is None or not PySpin.IsWritable(node):
			return cam_params # no such limit on this camera (GigE, older models)
		found = node.GetValue()
		lowest, highest = node.GetMin(), node.GetMax()
		if str(setting).lower() == "auto":
			frame = ReadNode(nodemap, "PayloadSize", PySpin.CIntegerPtr)
			needed = frame * float(cam_params["frameRate"]) if frame else 0
			if found >= needed * LINK_LIMIT_HEADROOM:
				print("USB throughput limit {:.0f} MB/s (frameRate needs {:.0f})...".format(found / 1e6, needed / 1e6))
				return cam_params
			want = highest
		else:
			want = min(max(int(float(setting) * 1e6), lowest), highest)
			step = node.GetInc()
			want = lowest + (want - lowest) // step * step
		node.SetValue(want)
		print("USB throughput limit set to {:.0f} MB/s (it was {:.0f})...".format(node.GetValue() / 1e6, found / 1e6))
	except Exception as e:
		logging.error("Caught error at cameras/flir.py ConfigureLinkLimit for {}: {}".format(name, e))
	return cam_params


# The switch that lets AcquisitionFrameRate set a free-running camera's rate:
# AcquisitionFrameRateEnable on a Blackfly S, an Oryx and other recent models;
# AcquisitionFrameRateEnabled on older Point Grey models (Grasshopper3,
# Chameleon3), which also pick the rate themselves until AcquisitionFrameRateAuto
# is Off
FRAME_RATE_SWITCHES = ("AcquisitionFrameRateEnable", "AcquisitionFrameRateEnabled")
FRAME_RATE_AUTO = "AcquisitionFrameRateAuto"

# AcquisitionFrameRate has its own step: a Blackfly S set to 80 fps reports
# 79.998 (and its timestamps say 80.00)
FRAME_RATE_TOLERANCE = 0.001


def FreeRuns(cam_params):
	# No trigger (cameraTrigger "None"): the camera times its own frames
	return str(cam_params["cameraTrigger"]).lower() == "none"


def GetNode(nodemap, name, kind):
	# The named node as kind (PySpin.CFloatPtr, ...), or None where this camera
	# has no such node or cannot read it now
	node = nodemap.GetNode(name)
	if node is None:
		return None
	node = kind(node)
	return node if PySpin.IsAvailable(node) and PySpin.IsReadable(node) else None


def ReadNode(nodemap, name, kind):
	node = GetNode(nodemap, name, kind)
	return node.GetValue() if node is not None else None


def FrameRateSwitch(nodemap):
	for name in FRAME_RATE_SWITCHES:
		node = GetNode(nodemap, name, PySpin.CBooleanPtr)
		if node is not None:
			return node
	return None


def ConfigureFrameRate(camera, cam_params):
	"""
	A free-running camera (cameraTrigger "None") runs at frameRate. campy stops
	after frameRate x recTimeInSec frames and the writer stamps the video at
	frameRate, so a camera left at its own rate records too fast (a Blackfly S
	runs at up to 170 fps) and its video plays slowed down.

	AcquisitionFrameRate only caps the rate: an exposure longer than a frame,
	the USB link's throughput limit or the sensor's readout can hold the camera
	below it. So campy reads back the rate the camera will run at
	(AcquisitionResultingFrameRate), keeps it as resultingFrameRate (in
	metadata.csv), and says why when it is not frameRate. What the camera had
	is put back when campy closes it (RestoreFrameRate).

	A triggered camera is left as it is: the trigger sets its rate.
	"""
	if not FreeRuns(cam_params):
		return cam_params

	name, rate = cam_params["cameraName"], float(cam_params["frameRate"])
	try:
		nodemap = camera.GetNodeMap()
		switch = FrameRateSwitch(nodemap)
		node = GetNode(nodemap, "AcquisitionFrameRate", PySpin.CFloatPtr)
		auto = GetNode(nodemap, FRAME_RATE_AUTO, PySpin.CEnumerationPtr)
		if switch is None or node is None or not PySpin.IsWritable(switch):
			print("{}: this camera's frame rate cannot be set (AcquisitionFrameRate), so it free-runs at its "
				"own rate, not at frameRate {:g}.".format(name, rate), flush=True)
			return cam_params

		# What the camera had, for RestoreFrameRate
		cam_params["frameRateFound"] = {
			"enabled": switch.GetValue(),
			"rate": node.GetValue(),
			"auto": auto.GetCurrentEntry().GetSymbolic() if auto is not None else None,
			}

		if auto is not None and PySpin.IsWritable(auto):
			off = auto.GetEntryByName("Off")
			if off is not None and PySpin.IsAvailable(off):
				auto.SetIntValue(off.GetValue())
		switch.SetValue(True)
		if not PySpin.IsWritable(node):
			print("{}: AcquisitionFrameRate cannot be set, so it free-runs at its own rate, not at "
				"frameRate {:g}.".format(name, rate), flush=True)
			return cam_params

		# The camera refuses a rate outside its range. Its maximum follows the
		# frame size, pixel format and USB throughput limit, not the exposure.
		lowest, highest = node.GetMin(), node.GetMax()
		node.SetValue(min(max(rate, lowest), highest))

		fps = ReadNode(nodemap, "AcquisitionResultingFrameRate", PySpin.CFloatPtr)
		if fps is None:
			fps = node.GetValue()
		cam_params["resultingFrameRate"] = round(fps, 3)
		if abs(fps - rate) <= FRAME_RATE_TOLERANCE * rate:
			print("Frame rate set to {:.3f} fps (free-running)...".format(fps))
		else:
			print(FrameRateShortfall(cam_params, nodemap, fps, lowest), flush=True)

	except Exception as e:
		logging.error("Caught error at cameras/flir.py ConfigureFrameRate: {}".format(e))

	return cam_params


def FrameRateShortfall(cam_params, nodemap, fps, lowest):
	# Why a free-running camera will not run at frameRate, in words
	name, rate = cam_params["cameraName"], float(cam_params["frameRate"])
	result = "It will record at {:.1f} fps, and its video will say {:g} fps.".format(fps, rate)
	if fps > rate:
		return "{}: frameRate {:g} is below the slowest this camera free-runs at, {:.1f} fps (AcquisitionFrameRate's "\
			"minimum). {} Trigger it to record more slowly.".format(name, rate, lowest, result)

	from campy import preflight # the launch check's words for what limits a camera
	limits = {
		"frameBytes": ReadNode(nodemap, "PayloadSize", PySpin.CIntegerPtr),
		"throughputLimit": ReadNode(nodemap, "DeviceLinkThroughputLimit", PySpin.CIntegerPtr),
		"exposureUs": ReadNode(nodemap, "ExposureTime", PySpin.CFloatPtr),
		}
	return "{} can run at only {:.1f} fps free-running with campy's settings, below frameRate {:g}: {}. {}".format(
		name, fps, rate, preflight.FrameRateReason(cam_params, limits, rate), result)


def RestoreFrameRate(camera, cam_params):
	"""
	Puts back the frame rate settings ConfigureFrameRate found on a
	free-running camera. A camera keeps them until it is unplugged, and with
	the cap on it drops triggers that come faster than AcquisitionFrameRate (a
	Blackfly S capped at 10 fps kept 21 of 100 software triggers at 50 Hz), so
	a triggered recording after this one would be held to this one's frameRate.
	"""
	found = cam_params.get("frameRateFound")
	if not found:
		return
	try:
		nodemap = camera.GetNodeMap()
		# The switch first: the rate can be set only while it is on, and turning
		# it off resets the rate (to the most the camera can do)
		FrameRateSwitch(nodemap).SetValue(found["enabled"])
		node = GetNode(nodemap, "AcquisitionFrameRate", PySpin.CFloatPtr)
		if found["enabled"] and node is not None and PySpin.IsWritable(node):
			node.SetValue(min(max(found["rate"], node.GetMin()), node.GetMax()))
		auto = GetNode(nodemap, FRAME_RATE_AUTO, PySpin.CEnumerationPtr)
		if found["auto"] is not None and auto is not None and PySpin.IsWritable(auto):
			auto.SetIntValue(auto.GetEntryByName(found["auto"]).GetValue())
	except Exception as e:
		logging.error("Caught error at cameras/flir.py RestoreFrameRate: {}".format(e))


def PrintDeviceInfo(nodemap, cam_params):
	"""
	This function prints the device information of the camera from the transport
	layer; please see NodeMapInfo example for more in-depth comments on printing
	device information from the nodemap.
	:param nodemap: Transport layer device nodemap.
	:param cam_num: Camera number.
	:type nodemap: INodeMap
	:type cam_num: int
	:returns: True if successful, False otherwise.
	:rtype: bool
	"""
	try:
		cam_num = cam_params["cameraSelection"]
		if cam_params["cameraDebug"]:
			print("Printing device information for camera %d... \n" % cam_num)

			node_device_information = PySpin.CCategoryPtr(nodemap.GetNode("DeviceInformation"))
			if PySpin.IsAvailable(node_device_information) and PySpin.IsReadable(node_device_information):
				features = node_device_information.GetFeatures()
				for feature in features:
					node_feature = PySpin.CValuePtr(feature)
					try:
						print("%s: %s" % (node_feature.GetName(), node_feature.ToString() if PySpin.IsReadable(
							node_feature) else "Node not readable"))
					except:
						pass
			else:
				print("Device control information not available.")

	except Exception as e:
		logging.error("Caught error at cameras/flir.py PrintDeviceInfo: {}".format(e))

