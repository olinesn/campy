"""
"""
import PySpin
from campy.cameras import unicam
from campy.utils.utils import Say
import os, io, sys, json, time, logging, contextlib


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
	return FixedParams(cam_params)


# campy sets a FLIR camera's frame size in steps of 16 (a Blackfly S's Width
# and Height step by 16)
FRAME_SIZE_STEP = 16


def FrameSide(value):
	# frameWidth or frameHeight as campy sets it: rounded up to a multiple of 16
	value = int(value)
	return value + -value % FRAME_SIZE_STEP


def FixedParams(cam_params):
	"""
	frameWidth and frameHeight rounded up to multiples of 16, as campy sets
	them on the camera. Before the camera opens (LoadDevice): the writer opens
	its video at frameWidth x frameHeight as soon as the camera thread starts,
	before that thread has set the camera up, and takes every frame as that
	size. Rounded only in the camera thread, 1000 wide gave 1008-wide frames
	in a video of 1000: scrambled.
	"""
	for key in ("frameWidth", "frameHeight"):
		rounded = FrameSide(cam_params[key])
		if rounded != cam_params[key]:
			Say("{}: {} {} rounded up to {}: campy sets FLIR frame sizes in steps of 16.".format(
				cam_params["cameraName"], key, cam_params[key], rounded))
			cam_params[key] = rounded
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


def GigeDiscovery(system):
	# Whether Spinnaker searches the network interfaces for GigE cameras each
	# time it starts (EnumerateGEVInterfaces; campy-flir-reset --gige turns it
	# off), or None where this Spinnaker cannot say
	node = system.GetTLNodeMap().GetNode("EnumerateGEVInterfaces")
	if node is None:
		return None
	node = PySpin.CBooleanPtr(node)
	if PySpin.IsAvailable(node) and PySpin.IsReadable(node):
		return node.GetValue()
	return None


def DeviceType(device):
	# How a camera is connected, read without opening it: "USB3Vision",
	# "GigEVision", or None where Spinnaker cannot say
	node = device.GetTLDeviceNodeMap().GetNode("DeviceType")
	if node is None:
		return None
	node = PySpin.CEnumerationPtr(node)
	if PySpin.IsAvailable(node) and PySpin.IsReadable(node):
		return node.GetCurrentEntry().GetSymbolic()
	return None


def OpenCamera(cam_params):

	# Load camera
	camera = cam_params["camera"]

	# Initialize camera object
	camera.Init()

	# What it loses before its frames reach campy, from here to CloseCamera
	_frameCounts[id(camera)] = (camera, FrameCount(cam_params["cameraName"]))

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

	# First, what an earlier run that did not close the camera left on it
	RecoverAcquisition(camera, cam_params)
	RecoverFrameRate(camera, cam_params)

	try:
		# Configure trigger
		cam_params = ConfigureTrigger(camera, cam_params)

		# Configure custom image settings (acquisition, white balance, frame height/width/offsets, 
		# pixel format, exposure, gain, gamma, buffer and chunkdata mode (e.g. timestamp/frame info))
		cam_params = ConfigureCustomImageSettings(camera, cam_params)
		cam_params = CheckFrameSize(camera, cam_params)

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
	  frameSize, wantedFrameSize: the frames it would send, (width, height),
	       and frameWidth x frameHeight as campy rounds them; maxFrameSize:
	       its full frame (WidthMax, HeightMax); frameSizeLocked: its frame
	       size cannot be changed now (FormatLocked). None where unreadable.
	A free-running camera is read without the cap campy gives it (frameRate,
	ConfigureFrameRate), so fps is the most it can do. It is read with the USB
	limit campy gives it (ConfigureLinkLimit), and closed with the frame rate
	settings and USB limit it had. Before anything else it puts back what an
	earlier run that did not close the camera left on it (RecoverAcquisition,
	RecoverFrameRate), where their messages are not hidden with the progress
	lines.
	"""
	camera = device
	camera.Init()
	settings = None
	try:
		RecoverAcquisition(camera, cam_params)
		RecoverFrameRate(camera, cam_params)
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
			"frameSize": FrameSize(nodemap),
			"wantedFrameSize": (int(settings["frameWidth"]), int(settings["frameHeight"])),
			"maxFrameSize": FrameSize(nodemap, "WidthMax", "HeightMax"),
			"frameSizeLocked": FormatLocked(nodemap),
			}
	finally:
		if settings is not None:
			RestoreFrameRate(camera, settings)
			RestoreLinkLimit(camera, settings)
		camera.DeInit()


def StartGrabbing(camera):
	try:
		camera.BeginAcquisition()
	except PySpin.SpinnakerException as e:
		Say("Error: {}".format(e))
		return False
	except Exception as e:
		Say("Exception in cameras/flir.py function StartGrabbing(camera): {}".format(e))
		return False
	# Spinnaker's stream counters as the acquisition starts (outside the try:
	# a counter that cannot be read is left out, and stops nothing)
	Counts(camera).Start(camera)
	return True


def GrabFrame(camera, frameNumber):
	try:
		image_result = camera.GetNextImage(50) # timeout to wait for next frame, in ms
	except PySpin.SpinnakerException as e:
		if Gone(camera):
			raise unicam.CameraLost("Spinnaker says the camera is no longer valid (unplugged?): {}".format(e))
		raise

	#  Ensure image completion. An incomplete image is released here: its
	#  buffer is the camera's, and one never released is one it cannot fill.
	#  It is counted, and said at most every few seconds.
	counts = Counts(camera)
	if image_result.IsIncomplete():
		image_status = image_result.GetImageStatus()
		image_result.Release()
		counts.Incomplete(image_status, frameNumber)
		raise ImageNotCompleteException("Image not complete", image_status)

	counts.Frame(image_result)
	return image_result


def Gone(camera):
	"""
	True when Spinnaker says the camera can no longer be used (IsValid False:
	unplugged), asked after GetNextImage fails. Nothing else counts, an error
	code included: a triggered camera waits for its first trigger with
	GetNextImage timing out, for as long as that takes, and which errors an
	unplugged camera gives has not been seen on a rig. A camera that is gone
	but still valid is said to send no frames (unicam.FrameWatch) until Ctrl-C.
	"""
	try:
		return camera.IsValid() is False
	except Exception:
		return False


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

		# Downsample image (the display shows a mono image as gray as it is)
		img = img[::cam_params["displayDownsample"], ::cam_params["displayDownsample"]]

		# Send to display queue
		dispQueue.append(img)

	except Exception as e:
		logging.error('Caught exception at cameras/flir.py DisplayImage: {}.'.format(e))


def ReleaseFrame(grabResult):

	grabResult.Release()


def CloseCamera(cam_params, camera):
	"""
	Saves the frames the camera lost before they reached campy, stops the
	acquisition, puts back the frame rate settings, and lets the camera go.
	The frames lost first: ending the acquisition can reset Spinnaker's
	counters. Each step is tried whatever the one before did, and what fails
	is said, not raised: EndAcquisition fails on a camera that was unplugged,
	or that never started, and campy's camera thread still has the writer to
	stop and metadata to save. A camera left acquiring is stopped at its next
	open (RecoverAcquisition), and its frame rate cap is put back then
	(RecoverFrameRate).
	"""
	name = cam_params["cameraName"]
	Say("Closing {}... Please wait.".format(name))
	for step, close in (("SaveFramesLost", lambda: SaveFramesLost(cam_params, camera)),
			("EndAcquisition", camera.EndAcquisition),
			("RestoreFrameRate", lambda: RestoreFrameRate(camera, cam_params)),
			("DeInit", camera.DeInit)):
		try:
			close()
		except Exception as e:
			Say("{}: {} failed as it closed: {}".format(name, step, e))


def CloseSystem(system, device_list):
	try:
		device_list.Clear()
		system.ReleaseInstance()
	except PySpin.SpinnakerException as e:
		Say("SpinnakerException at cameras/flir.py CloseSystem: {}".format(e))
		Say("passing from {}".format(__name__))
		raise
	except Exception as e:
		Say("Exception at cameras/flir.py CloseSystem: {}".format(e))
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
			Say("\n*** CONFIGURING CUSTOM IMAGE SETTINGS *** \n")

		settingsConfig = True

		camera.AcquisitionMode.SetValue(PySpin.AcquisitionMode_Continuous)

		cam_params = ConfigureWhiteBalance(camera, cam_params)
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


def ConfigureWhiteBalance(camera, cam_params):
	"""
	Turns a colour camera's automatic white balance off. A mono camera has no
	white balance: its BalanceWhiteAuto node is there but not available (a
	Blackfly S BFS-U3-13Y3M on PySpin 4.4), so it is left alone, with nothing
	printed.
	"""
	if GetNode(camera.GetNodeMap(), "BalanceWhiteAuto", PySpin.CEnumerationPtr) is None:
		return cam_params
	try:
		camera.BalanceWhiteAuto.SetValue(PySpin.BalanceWhiteAuto_Off)
	except Exception as e:
		Say("{}: could not turn off automatic white balance: {}".format(cam_params["cameraName"], e))
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
			Say("Unable to set Buffer Handling mode (node retrieval). Aborting...")
			return cam_params

		handling_mode_entry = PySpin.CEnumEntryPtr(handling_mode.GetCurrentEntry())
		if not PySpin.IsAvailable(handling_mode_entry) or not PySpin.IsReadable(handling_mode_entry):
			Say("Unable to set Buffer Handling mode (Entry retrieval). Aborting...")
			return cam_params

		# Set stream buffer Count Mode to manual
		stream_buffer_count_mode = PySpin.CEnumerationPtr(s_node_map.GetNode("StreamBufferCountMode"))
		if not PySpin.IsAvailable(stream_buffer_count_mode) or not PySpin.IsWritable(stream_buffer_count_mode):
			Say("Unable to set Buffer Count Mode (node retrieval). Aborting...")
			return cam_params

		stream_buffer_count_mode_manual = PySpin.CEnumEntryPtr(stream_buffer_count_mode.GetEntryByName("Manual"))
		if not PySpin.IsAvailable(stream_buffer_count_mode_manual) or not PySpin.IsReadable(
				stream_buffer_count_mode_manual):
			Say("Unable to set Buffer Count Mode entry (Entry retrieval). Aborting...")
			return cam_params

		stream_buffer_count_mode.SetIntValue(stream_buffer_count_mode_manual.GetValue())

		# Retrieve and modify Stream Buffer Count
		buffer_count = PySpin.CIntegerPtr(s_node_map.GetNode("StreamBufferCountManual"))
		if not PySpin.IsAvailable(buffer_count) or not PySpin.IsWritable(buffer_count):
			Say("Unable to set Buffer Count (Integer node retrieval). Aborting...")
			return cam_params

		# Display Buffer Info
		buffer_count.SetValue(cam_params["bufferSize"])
		Say("Buffer count now set to: %d" % buffer_count.GetValue())
		# For metadata.csv: the count the camera took. (This was the "Manual"
		# entry's value, 0, so every recording said bufferSize 0.)
		cam_params["bufferSize"] = buffer_count.GetValue()

		if cam_params["bufferMode"] == "OldestFirst":
			handling_mode_entry = handling_mode.GetEntryByName("OldestFirst")
		elif cam_params["bufferMode"] == "NewestFirst":
			handling_mode_entry = handling_mode.GetEntryByName("NewestFirst")
		elif cam_params["bufferMode"] == "NewestOnly":
			handling_mode_entry = handling_mode.GetEntryByName("NewestOnly")
		elif cam_params["bufferMode"] == "OldestFirstOverwrite":
			handling_mode_entry = handling_mode.GetEntryByName("OldestFirstOverwrite")
		else:
			Say("BufferMode should be 'OldestFirst', 'NewestFirst', 'NewestOnly' or 'OldestFirstOverwrite'")
			return cam_params

		handling_mode.SetIntValue(handling_mode_entry.GetValue())
		Say("BufferMode has been set to {}".format(handling_mode_entry.GetDisplayName()))

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
			Say("\n*** CONFIGURING CHUNK DATA ***\n")

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
			Say("Unable to retrieve chunk selector. Aborting...")
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
						Say("{} enabled".format(chunk_str))
					elif not chunk_enable.GetValue() and PySpin.IsWritable(chunk_enable):
						Say("{} disabled".format(chunk_str))
					else:
						Say("{} not available".format(chunk_str))

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
			Say("*** CONFIGURING EXPOSURE ***\n")
		# Turn off automatic exposure mode
		#
		# *** NOTES *** Automatic exposure prevents the manual configuration of exposure times and needs to be turned
		# off. Enumerations representing entry nodes have been added to QuickSpin. This allows for the much easier
		# setting of enumeration nodes to new values.
		#
		# The naming convention of QuickSpin enums is the name of the enumeration node followed by an underscore and
		# the symbolic of the entry node. Selecting "Off" on the "ExposureAuto" node is thus named "ExposureAuto_Off".

		if camera.ExposureAuto.GetAccessMode() != PySpin.RW:
			Say("Unable to disable automatic exposure. Aborting...")
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
			Say("Unable to set exposure time. Aborting...")
			return cam_params

		# Ensure desired exposure time does not exceed the maximum
		exposure_time_to_set = cam_params["cameraExposureTimeInUs"]
		exposure_time_to_set = min(camera.ExposureTime.GetMax(), exposure_time_to_set)
		camera.ExposureTime.SetValue(exposure_time_to_set)
		Say("Shutter time set to {} us...".format(exposure_time_to_set))
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

		# Rounded already in a recording (FixedParams); not in the launch check
		height_to_set = cam_params["frameHeight"] = FrameSide(cam_params["frameHeight"])

		# Get max offset values from the camera
		max_h = height_to_set
		try:
			ZeroOffset(nodemap, "OffsetY", "Height")
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
				Say("offset_y is set to {}".format(offset_y))
			node_offset_y = PySpin.CIntegerPtr(nodemap.GetNode("OffsetY"))
			if PySpin.IsAvailable(node_offset_y) and PySpin.IsWritable(node_offset_y):
				node_offset_y.SetValue(offset_y)
				cam_params["offsetY"] = offset_y
			else:
				Say("OffsetY cannot be set!")
		else:
			Say("Height not available...")
		Say("Height set to {}...".format(node_height.GetValue()))

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
		# Rounded already in a recording (FixedParams); not in the launch check
		width_to_set = cam_params["frameWidth"] = FrameSide(cam_params["frameWidth"])

		# Get max offset values from the camera
		max_w = width_to_set
		try:
			ZeroOffset(nodemap, "OffsetX", "Width")
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
			Say("Width set to {}...".format(node_width.GetValue()))
			offset_x = int((max_w-width_to_set)/2)
			if offset_x % 4 != 0:
				while offset_x % 4 != 0:
					offset_x += 1
				Say("offset_x is set to {}".format(offset_x))
			node_offset_x = PySpin.CIntegerPtr(nodemap.GetNode("OffsetX"))
			if PySpin.IsAvailable(node_offset_x) and PySpin.IsWritable(node_offset_x):
				node_offset_x.SetValue(offset_x)
				cam_params["offsetX"] = offset_x
			else:
				Say("OffsetX cannot be set!")
		else:
			Say("Width not available...")

	except Exception as e:
		logging.error("Caught exception at cameras/flir.py ConfigureFrameWidth: {}".format(e))

	return cam_params


def ZeroOffset(nodemap, offset, size):
	"""
	Moves the frame to the sensor's edge (OffsetX or OffsetY 0) before
	ConfigureFrameWidth or ConfigureFrameHeight sets its size and centres it.
	Width's maximum is what is left of the sensor right of OffsetX (Height's,
	below OffsetY), so with the offset a smaller centred frame left on the
	camera (640x512 of 1280x1024, at offsets 160 and 128), a full frame could
	not be set ("Value = 1280 must be equal or smaller than Max = 1120"), and
	a frame of the same size was centred on the wrong maximum. Left alone when
	the size cannot be set, so the frame it has is not moved.
	"""
	try:
		size_node = PySpin.CIntegerPtr(nodemap.GetNode(size))
		offset_node = PySpin.CIntegerPtr(nodemap.GetNode(offset))
		if PySpin.IsAvailable(size_node) and PySpin.IsWritable(size_node) \
		and PySpin.IsAvailable(offset_node) and PySpin.IsWritable(offset_node):
			offset_node.SetValue(offset_node.GetMin())
	except Exception as e:
		logging.error("Caught exception at cameras/flir.py ZeroOffset {}: {}".format(offset, e))


def FrameSize(nodemap, width="Width", height="Height"):
	# (width, height) read from the camera, or None where it cannot be read
	size = (ReadNode(nodemap, width, PySpin.CIntegerPtr), ReadNode(nodemap, height, PySpin.CIntegerPtr))
	return size if None not in size else None


def CheckFrameSize(camera, cam_params):
	"""
	Says when the camera's frame size is not frameWidth x frameHeight (a
	setting it refused, logged above): the writer takes its frames as that
	size, so its video would be scrambled. The launch check stops campy
	before that (preflight.FrameSizeFinding); this is for a run without it.
	"""
	size = FrameSize(camera.GetNodeMap())
	wanted = (int(cam_params["frameWidth"]), int(cam_params["frameHeight"]))
	if size is not None and size != wanted:
		Say("{}: its frames are {}x{}, not frameWidth x frameHeight {}x{}, so its video will be scrambled.".format(
			cam_params["cameraName"], *(size + wanted)))
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
			Say("*** CONFIGURING GAIN ***\n")
	
		# Retrieve GenICam nodemap (nodemap)
		nodemap = camera.GetNodeMap()

		# Retrieve node
		node_gainauto_mode = PySpin.CEnumerationPtr(nodemap.GetNode("GainAuto"))
		if not PySpin.IsAvailable(node_gainauto_mode) or not PySpin.IsWritable(node_gainauto_mode):
			Say("Unable to configure gain (enum retrieval). Aborting...")
			return cam_params

		# EnumEntry node (always associated with an Enumeration node)
		node_gainauto_mode_off = node_gainauto_mode.GetEntryByName("Off")
		if not PySpin.IsAvailable(node_gainauto_mode_off):
			Say("Unable to configure gain (entry retrieval). Aborting...")
			return cam_params

		# Turn off Auto Gain
		node_gainauto_mode.SetIntValue(node_gainauto_mode_off.GetValue())

		# Retrieve gain node (float)
		node_gain = PySpin.CFloatPtr(nodemap.GetNode("Gain"))
		if not PySpin.IsAvailable(node_gain) or not PySpin.IsWritable(node_gain):
			Say("Unable to configure gain (float retrieval). Aborting...")
			return cam_params

		max_gain = camera.Gain.GetMax()

		if gain > camera.Gain.GetMax():
			Say("Max. gain is {}dB!".format(max_gain))
			gain = max_gain
		elif gain <= 0:
			gain = 0.0

		# Set gain
		node_gain.SetValue(float(gain))
		Say("Gain set to {} dB.".format(gain))
		cam_params["gain"] = gain

	except Exception as e:
		logging.error("Caught error at cameras/flir.py ConfigureGain: {}".format(e))

	return cam_params


# The switch that turns a camera's gamma on: GammaEnable on a Blackfly S and
# other recent models, GammaEnabled on older Point Grey ones
GAMMA_SWITCHES = ("GammaEnable", "GammaEnabled")


def ConfigureGamma(camera, cam_params):
	"""
	The gamma the camera applies to each frame, from cameraGamma: a number
	turns the camera's gamma on (GammaEnable) at that value (Gamma), and "Off"
	turns it off, so that brightness is in proportion to the light. The camera
	applies brightness out = brightness in ^ Gamma, on a 0-1 scale: below 1
	brightens the dark and middle grays and leaves black and white where they
	are. A value outside what the camera takes (Gamma's minimum and maximum)
	is set to the nearest of them, and said. The gamma the camera has after
	this goes into metadata.csv as gamma ("Off" when off). Like gain and
	exposure, it is not put back when the camera closes: every open sets it.
	"""
	name = cam_params["cameraName"]
	try:
		setting = cam_params["cameraGamma"]
		if cam_params["cameraDebug"]:
			Say("*** CONFIGURING GAMMA ***\n")
		nodemap = camera.GetNodeMap()
		switch = None
		for switch_name in GAMMA_SWITCHES:
			switch = GetNode(nodemap, switch_name, PySpin.CBooleanPtr)
			if switch is not None:
				break

		if str(setting).lower() == "off":
			if switch is None or not PySpin.IsWritable(switch):
				Say("{}: could not turn its gamma off (cameraGamma \"Off\"): it has no GammaEnable it can "
					"change.".format(name))
				return cam_params
			switch.SetValue(False)
			cam_params["gamma"] = "Off"
			Say("Gamma off.")
			return cam_params

		# On first: Gamma can be set only while it is on
		if switch is not None and PySpin.IsWritable(switch):
			switch.SetValue(True)
		node = GetNode(nodemap, "Gamma", PySpin.CFloatPtr)
		if node is None or not PySpin.IsWritable(node):
			Say("{}: could not set its gamma to {} (cameraGamma): it has no Gamma it can change.".format(
				name, setting))
			return cam_params
		lowest, highest = node.GetMin(), node.GetMax()
		gamma = min(max(float(setting), lowest), highest)
		if gamma != float(setting):
			Say("{}: cameraGamma {} is outside what this camera takes, {:g} to {:g}. Gamma set to {:g}.".format(
				name, setting, lowest, highest, gamma))
		node.SetValue(gamma)
		cam_params["gamma"] = round(node.GetValue(), 4)
		Say("Gamma set to {:g}.".format(cam_params["gamma"]))

	except Exception as e:
		logging.error("Caught error at cameras/flir.py ConfigureGamma for {}: {}".format(name, e))

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
			Say("*** CONFIGURING PIXEL FORMAT ***\n")

		if pixelFormat == "bayer_rggb8" or pixelFormat == "bayer_bggr8":
			camera.PixelFormat.SetValue(PySpin.PixelFormat_BayerRG8)
			camera.IspEnable.SetValue(False)
		elif pixelFormat == "rgb24":
			camera.PixelFormat.SetValue(PySpin.PixelFormat_RGB8Packed)
		elif pixelFormat == "gray":
			camera.PixelFormat.SetValue(PySpin.PixelFormat_Mono8)
		else:
			# pixelFormatInput is also ffmpeg's name for the frames (writer.py),
			# so only the four above name a format that both ffmpeg and the
			# camera have. (This branch looked up PySpin.PixelFormat_<name>, with
			# a call never closed, so it always failed, and silently.)
			Say("{}: campy does not set pixelFormatInput \"{}\" on a FLIR camera, only \"gray\", \"rgb24\", "
				"\"bayer_rggb8\" or \"bayer_bggr8\". The camera keeps its own pixel format.".format(
				cam_params["cameraName"], pixelFormat))

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
				Say("*** CONFIGURING TRIGGER ***")

		cameraTrigger = cam_params["cameraTrigger"]

		# Ensure trigger mode off
		# The trigger must be disabled in order to configure whether the source
		# is software or hardware.
		if camera.TriggerMode.GetAccessMode() != PySpin.RW:
			Say("Unable to disable trigger mode (node retrieval). Aborting...")
			return cam_params
		camera.TriggerMode.SetValue(PySpin.TriggerMode_Off)

		# Switch on the TriggerOverlap (important for high frame rates/exposures)
		camera.TriggerOverlap.SetValue(PySpin.TriggerOverlap_ReadOut)

		# Select trigger source
		# The trigger source must be set to hardware or software while trigger
		# mode is off.
		if camera.TriggerSource.GetAccessMode() != PySpin.RW:
			Say("Unable to get trigger source (node retrieval). Aborting...")
			return cam_params

		# Configure trigger source
		if cameraTrigger == 'software' or cameraTrigger == 'Software' or cameraTrigger == 'SOFTWARE':
			camera.TriggerSource.SetValue(PySpin.TriggerSource_Software)
			Say("Trigger source set to software...")
		elif cameraTrigger == 'none' or cameraTrigger == 'None' or cameraTrigger == 'NONE':
			camera.TriggerMode.SetValue(PySpin.TriggerMode_Off)
			Say("Trigger source set to None...")
			return cam_params
		else:
			# A line, e.g. "Line3": PySpin.TriggerSource_Line3
			source = getattr(PySpin, "TriggerSource_{}".format(cameraTrigger), None)
			if source is None:
				Say("{}: cameraTrigger \"{}\" is not a FLIR trigger source (\"Line0\" to \"Line3\", \"Software\", "
					"or \"None\" to free-run). Trigger mode is left off.".format(cam_params["cameraName"], cameraTrigger))
				return cam_params
			camera.TriggerSource.SetValue(source)
			Say("Trigger source set to {}...".format(cameraTrigger))

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
	enough. The limit found is not put back when a recording closes the
	camera: unlike a frame rate cap, a higher limit holds no later recording
	back. The launch check does put it back (RestoreLinkLimit), so the camera's
	own process makes the change and says so.
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
				Say("USB throughput limit {:.0f} MB/s (frameRate needs {:.0f})...".format(found / 1e6, needed / 1e6))
				return cam_params
			want = highest
		else:
			want = min(max(int(float(setting) * 1e6), lowest), highest)
			step = node.GetInc()
			want = lowest + (want - lowest) // step * step
			if want == found:
				Say("USB throughput limit {:.0f} MB/s...".format(found / 1e6))
				return cam_params
		node.SetValue(want)
		cam_params["linkLimitFound"] = found # for RestoreLinkLimit
		Say("USB throughput limit set to {:.0f} MB/s (it was {:.0f})...".format(node.GetValue() / 1e6, found / 1e6))
	except Exception as e:
		logging.error("Caught error at cameras/flir.py ConfigureLinkLimit for {}: {}".format(name, e))
	return cam_params


def RestoreLinkLimit(camera, cam_params):
	# Puts back the USB throughput limit ConfigureLinkLimit found (the launch check)
	found = cam_params.get("linkLimitFound")
	if found is None:
		return
	try:
		GetNode(camera.GetNodeMap(), "DeviceLinkThroughputLimit", PySpin.CIntegerPtr).SetValue(found)
	except Exception as e:
		logging.error("Caught error at cameras/flir.py RestoreLinkLimit: {}".format(e))


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
	is put back when campy closes it (RestoreFrameRate), and is noted in a file
	first, for the next run to put back if this one never closes it
	(RecoverFrameRate).

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
			Say("{}: this camera's frame rate cannot be set (AcquisitionFrameRate), so it free-runs at its "
				"own rate, not at frameRate {:g}.".format(name, rate))
			return cam_params

		# What the camera had, for RestoreFrameRate. A note still there is one
		# RecoverFrameRate could not put back: the camera still has an earlier
		# run's settings, and the note has what it had before them.
		path = FrameRateNotePath(cam_params)
		found = ReadFrameRateNote(path)
		if found is None:
			found = FrameRateState(nodemap)
			WriteFrameRateNote(path, cam_params, found)
		cam_params["frameRateFound"] = found

		if auto is not None and PySpin.IsWritable(auto):
			off = auto.GetEntryByName("Off")
			if off is not None and PySpin.IsAvailable(off):
				auto.SetIntValue(off.GetValue())
		switch.SetValue(True)
		if not PySpin.IsWritable(node):
			Say("{}: AcquisitionFrameRate cannot be set, so it free-runs at its own rate, not at "
				"frameRate {:g}.".format(name, rate))
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
			Say("Frame rate set to {:.3f} fps (free-running)...".format(fps))
		else:
			Say(FrameRateShortfall(cam_params, nodemap, fps, lowest))

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


def FrameRateState(nodemap):
	# A camera's frame rate settings, as ConfigureFrameRate finds them
	switch = FrameRateSwitch(nodemap)
	node = GetNode(nodemap, "AcquisitionFrameRate", PySpin.CFloatPtr)
	auto = GetNode(nodemap, FRAME_RATE_AUTO, PySpin.CEnumerationPtr)
	return {
		"enabled": switch.GetValue(),
		"rate": node.GetValue(),
		"auto": auto.GetCurrentEntry().GetSymbolic() if auto is not None else None,
		}


def SetFrameRateState(nodemap, state):
	# Gives a camera the frame rate settings FrameRateState read
	# The switch first: the rate can be set only while it is on, and turning
	# it off resets the rate (to the most the camera can do)
	FrameRateSwitch(nodemap).SetValue(state["enabled"])
	node = GetNode(nodemap, "AcquisitionFrameRate", PySpin.CFloatPtr)
	if state["enabled"] and node is not None and PySpin.IsWritable(node):
		node.SetValue(min(max(state["rate"], node.GetMin()), node.GetMax()))
	auto = GetNode(nodemap, FRAME_RATE_AUTO, PySpin.CEnumerationPtr)
	if state["auto"] is not None and auto is not None and PySpin.IsWritable(auto):
		auto.SetIntValue(auto.GetEntryByName(state["auto"]).GetValue())


def Cap(state):
	# The rate frame rate settings cap a camera at, to 0.1 fps; None when they do not
	if state["enabled"] and state["auto"] in (None, "Off"):
		return round(state["rate"], 1)
	return None


def RestoreFrameRate(camera, cam_params):
	"""
	Puts back the frame rate settings ConfigureFrameRate found on a
	free-running camera. A camera keeps them until it is unplugged, and with
	the cap on it drops triggers that come faster than AcquisitionFrameRate (a
	Blackfly S capped at 10 fps kept 21 of 100 software triggers at 50 Hz), so
	a triggered recording after this one would be held to this one's frameRate.
	Once they are back, their note (WriteFrameRateNote) is removed.
	"""
	found = cam_params.get("frameRateFound")
	if not found:
		return
	try:
		SetFrameRateState(camera.GetNodeMap(), found)
	except Exception as e:
		logging.error("Caught error at cameras/flir.py RestoreFrameRate: {}".format(e))
		return # the note stays, for the next run to try again
	RemoveFrameRateNote(FrameRateNotePath(cam_params))


# ------------------------------------------------------------------ the frame rate note
#
# A camera keeps campy's frame rate cap until it is unplugged. When campy's
# camera process stops without closing the camera (a crash, a kill, a
# restart of the computer, which leaves the camera powered), RestoreFrameRate
# never runs, and the next run would find the cap and put it back again at
# its own close. So ConfigureFrameRate notes what it found in a file, one per camera,
# before it changes anything; RestoreFrameRate removes the note once it has
# put those settings back; and every later open of the camera, triggered or
# not, puts back a note it finds first (RecoverFrameRate).

# Where the notes are kept, with campy-encode-sweep's capacity.json
NOTE_FOLDER = os.path.join(os.path.expanduser("~"), ".campy")


def FrameRateNotePath(cam_params):
	# This camera's note, by serial number; None when it has none
	serial = "".join(c for c in str(cam_params.get("cameraSerialNo") or "") if c.isalnum())
	return os.path.join(NOTE_FOLDER, "flir-frame-rate-{}.json".format(serial)) if serial else None


def ReadFrameRateNote(path):
	# The settings a note holds, or None when there is no note (or it cannot be read)
	if path is None or not os.path.exists(path):
		return None
	try:
		with open(path) as f:
			note = json.load(f)
		return {key: note[key] for key in ("enabled", "rate", "auto")}
	except (OSError, ValueError, KeyError, TypeError) as e:
		logging.error("Caught error at cameras/flir.py ReadFrameRateNote, reading {}: {}".format(path, e))
		return None


def WriteFrameRateNote(path, cam_params, found):
	if path is None:
		return
	note = dict(found, cameraName=cam_params["cameraName"], cameraSerialNo=str(cam_params["cameraSerialNo"]),
		written=time.strftime("%Y-%m-%d %H:%M:%S"), pid=os.getpid())
	try:
		os.makedirs(NOTE_FOLDER, exist_ok=True)
		# Whole or not at all: a note cut short by a crash would be no note
		with open(path + ".tmp", "w") as f:
			json.dump(note, f, indent=1)
		os.replace(path + ".tmp", path)
	except OSError as e:
		Say("{}: could not write {} ({}). If campy stops without closing this camera, it keeps its frame "
			"rate cap until it is unplugged.".format(cam_params["cameraName"], path, e))


def RemoveFrameRateNote(path):
	if path is None:
		return
	try:
		os.remove(path)
	except FileNotFoundError:
		pass
	except OSError as e:
		logging.error("Caught error at cameras/flir.py RemoveFrameRateNote: {}".format(e))


def RecoverFrameRate(camera, cam_params):
	"""
	When campy last ran this camera free-running and did not close it, the
	camera still has that run's frame rate settings: puts back the ones that
	run found (its note), and says so. Done at every open, triggered or not,
	since a triggered camera drops triggers above a cap. Silent when the
	camera already has them, as after it has been unplugged. A cap with no
	note was set by another program, or by a campy without these notes, and
	is left alone: the launch check reports one that is below a triggered
	camera's frameRate.
	"""
	path = FrameRateNotePath(cam_params)
	if path is None or not os.path.exists(path):
		return
	found = ReadFrameRateNote(path)
	if found is None: # unreadable: nothing to put back
		RemoveFrameRateNote(path)
		return
	try:
		nodemap = camera.GetNodeMap()
		left = FrameRateState(nodemap)
		SetFrameRateState(nodemap, found)
	except Exception as e:
		logging.error("Caught error at cameras/flir.py RecoverFrameRate: {}".format(e))
		return # the note stays, for this run's close or the next run to try again
	RemoveFrameRateNote(path)
	if Cap(left) == Cap(found):
		return
	said = "{}: campy did not close it last time".format(cam_params["cameraName"])
	if Cap(left) is not None:
		said += " and left it capped at {:.1f} fps".format(Cap(left))
	if Cap(found) is not None:
		Say("{}. Cap set back to {:.1f} fps, as before.".format(said, Cap(found)))
	else:
		Say("{}. Cap removed, as before.".format(said))


# ------------------------------------------------------------------ an acquisition left running
#
# CloseCamera's EndAcquisition stops the camera's acquisition. When campy's
# camera process stops without closing the camera (a kill, a crash), the
# camera goes on acquiring, and while it acquires, its frame size and pixel
# format are read only to every process that opens it: ConfigureFrameWidth,
# ConfigureFrameHeight and ConfigurePixelFormat only log an error, and the
# camera sends frames of the size the killed run set into a video of the size
# asked for, which scrambles it when they differ. TLParamsLocked does not
# show it: a new process reads 0, and setting it changes nothing. What
# ends it is the camera's own AcquisitionStop, or Spinnaker's own
# BeginAcquisition and EndAcquisition, from the new process (two Blackfly S
# on PySpin 4.4, one on Linux and one on Windows, 2026-10-18). So every open
# stops such an acquisition first (RecoverAcquisition).

# Read only while the camera acquires
FORMAT_NODES = (("PixelFormat", "CEnumerationPtr"), ("Width", "CIntegerPtr"), ("Height", "CIntegerPtr"))


def FormatLocked(nodemap):
	# True when the camera's frame size or pixel format cannot be set now
	nodes = [GetNode(nodemap, name, getattr(PySpin, kind)) for name, kind in FORMAT_NODES]
	return any(node is not None and not PySpin.IsWritable(node) for node in nodes)


def StopAcquisition(camera, nodemap):
	# Stops an acquisition another process started: the camera's own command,
	# then, where that does not do it, Spinnaker's own start and stop
	node = nodemap.GetNode("AcquisitionStop")
	stop = PySpin.CCommandPtr(node) if node is not None else None
	if stop is not None and PySpin.IsAvailable(stop) and PySpin.IsWritable(stop):
		try:
			stop.Execute()
		except PySpin.SpinnakerException as e:
			logging.error("Caught error at cameras/flir.py StopAcquisition, AcquisitionStop: {}".format(e))
	if FormatLocked(nodemap):
		camera.BeginAcquisition()
		camera.EndAcquisition()


def RecoverAcquisition(camera, cam_params):
	"""
	When an earlier run did not close this camera, it is still acquiring and
	its frame size and pixel format cannot be set (above): stops that
	acquisition, and says so. Silent when they can be set.
	"""
	name = cam_params["cameraName"]
	try:
		nodemap = camera.GetNodeMap()
		if not FormatLocked(nodemap):
			return
		StopAcquisition(camera, nodemap)
		stuck = FormatLocked(nodemap)
	except Exception as e:
		logging.error("Caught error at cameras/flir.py RecoverAcquisition: {}".format(e))
		stuck = True
	if stuck:
		Say("{}: its frame size and pixel format cannot be set, even after stopping its acquisition, so it keeps "
			"the ones it has. Unplug it and plug it back in.".format(name))
	else:
		Say("{}: still acquiring from a run that did not close it. Stopped it, so its frame size and pixel format "
			"can be set.".format(name))


# ------------------------------------------------------------------ frames lost before campy
#
# campy numbers the frames it grabs itself (unicam.GrabFrames), so a frame the
# camera made that never reached campy, lost on the camera, the USB link or in
# Spinnaker, leaves frametimes.npy's frame numbers whole: only a longer step
# between its timestamps shows it. Three things count those frames. The
# camera numbers each frame it makes in its FrameID chunk (ConfigureChunkData
# turns it on), so a step of more than 1 between the frames campy gets is
# frames lost (FrameCount). The camera counts the frames it dropped because it
# could not send them (CAMERA_COUNTERS). And Spinnaker counts the frames its
# stream lost, dropped or got incomplete (STREAM_COUNTERS). CloseCamera saves
# them in metadata.csv (SaveFramesLost), and says so when frames were lost.
#
# Over USB a camera sends a frame only when Spinnaker has a buffer for it, so
# a camera thread that falls behind loses its frames on the camera, not in
# Spinnaker. A BFS-U3-13Y3M on the EVO-X2 (PySpin 4.4, 2026-10-18), free-running
# at 80 fps, its camera thread held for 3 s: of the 240 frames it made, 100
# waited in Spinnaker's buffers (bufferSize) and 41 in the camera's transfer
# queue (TransferQueueMaxBlockCount), and the other 99 were dropped on the
# camera. FrameID skipped 99, as did its timestamps, while Spinnaker's
# StreamLostFrameCount and StreamDroppedFrameCount stayed 0.

# The camera's own counters, in its device nodemap. They count from its power-up.
CAMERA_COUNTERS = (
	"TransferQueueOverflowCount", # frames dropped before they were sent: its transfer queue was full
	"LinkErrorCount", # errors on its USB link
	)

# Spinnaker's counters of the camera's stream, in its TL stream nodemap
# (TransportLayerStream.h). A Blackfly S on PySpin 4.4 (Linux) reads all of
# them but StreamReceivedFrameCount (GigE Vision only).
STREAM_COUNTERS = (
	"StreamStartedFrameCount", # frames it began to receive
	"StreamDeliveredFrameCount", # frames it gave campy
	"StreamReceivedFrameCount", # frames received whole (GigE Vision)
	"StreamIncompleteFrameCount", # frames with data missing (campy leaves them out: flirFramesIncomplete)
	"StreamLostFrameCount", # frames it had no free buffer for, or never received whole
	"StreamDroppedFrameCount", # frames it dropped before campy took them (bufferMode NewestOnly, NewestFirst)
	)

# An incomplete frame is said at most this often, with the count so far (as
# unicam.DropFrame says the frames it drops)
INCOMPLETE_REPORT_SEC = 5

# Each open camera and its FrameCount, by id(camera): GrabFrame is given only
# the camera. The camera is kept with it, so that no other object takes its id
# while it is here.
_frameCounts = {}


def Counts(camera):
	# The camera's FrameCount; made here for a camera OpenCamera did not open
	entry = _frameCounts.get(id(camera))
	if entry is None:
		entry = _frameCounts[id(camera)] = (camera, FrameCount("The FLIR camera"))
	return entry[1]


class FrameCount:
	"""
	What a camera loses before its frames reach campy, counted as it records:
	the frames its FrameID skipped, the incomplete frames GrabFrame left out,
	and the camera's and Spinnaker's counters as its acquisition started.
	"""

	def __init__(self, name):
		self.name = name
		self.frameID = None # the last frame's FrameID: None before the first, or after one it could not read
		self.counted = False # FrameID has stepped forward once, so lost counts something
		self.lost = 0 # frames FrameID skipped
		self.incomplete = 0 # incomplete frames left out
		self.reported = float("-inf") # when they were last said
		self.started = {} # CAMERA_COUNTERS and STREAM_COUNTERS as the acquisition started

	def Start(self, camera):
		# A new acquisition, where FrameID can start over
		self.frameID = None
		self.started = ReadCounters(camera)

	def Frame(self, image):
		"""
		Each frame GrabFrame returns. FrameID goes up by 1 from one frame the
		camera makes to the next, so a bigger step is frames that never
		reached campy. A step back, or none, is FrameID starting over (a
		counter that wraps, or was reset), not frames lost: counting goes on
		from there. A frame whose FrameID cannot be read breaks the count, so
		nothing is counted across it. A FrameID that never steps forward (a
		camera that sends no FrameID chunk, read as 0) counts nothing, and
		leaves flirFramesLost out.
		"""
		try:
			frameID = int(GetFrameID(image.GetChunkData()))
		except Exception:
			self.frameID = None
			return
		if self.frameID is not None and frameID > self.frameID:
			self.lost += frameID - self.frameID - 1
			self.counted = True
		self.frameID = frameID

	def Incomplete(self, status, frameNumber):
		# An incomplete frame left out: counted, and said at most every
		# INCOMPLETE_REPORT_SEC. Said for every one, a camera on a failing USB
		# link printed a line per frame.
		self.incomplete += 1
		now = time.perf_counter()
		if now - self.reported >= INCOMPLETE_REPORT_SEC:
			self.reported = now
			Say("{}: a frame arrived incomplete (image status {}). Left out {} incomplete frames so far (latest: "
				"after frame {}).".format(self.name, ImageStatusText(status), self.incomplete, frameNumber))


def ImageStatusText(status):
	# An image status and its name in PySpin, in words: "3: missing packets"
	prefix = "SPINNAKER_IMAGE_STATUS_"
	for key in dir(PySpin):
		if key.startswith(prefix) and getattr(PySpin, key) == status:
			return "{}: {}".format(status, key[len(prefix):].lower().replace("_", " "))
	return str(status)


def ReadCounters(camera):
	# The CAMERA_COUNTERS and STREAM_COUNTERS the camera can read now, by name. Never raises.
	counters = {}
	for nodemap, names in ((lambda: camera.GetNodeMap(), CAMERA_COUNTERS),
			(lambda: camera.GetTLStreamNodeMap(), STREAM_COUNTERS)):
		try:
			nodemap = nodemap()
		except Exception as e:
			logging.error("Caught error at cameras/flir.py ReadCounters: {}".format(e))
			continue
		for name in names:
			try:
				value = ReadNode(nodemap, name, PySpin.CIntegerPtr)
			except Exception as e:
				logging.error("Caught error at cameras/flir.py ReadCounters, {}: {}".format(name, e))
				continue
			if value is not None:
				counters[name] = int(value)
	return counters


def SaveFramesLost(cam_params, camera):
	"""
	The frames the camera lost before they reached campy, into cam_params
	for metadata.csv, and a line when it lost any (FramesLostText):
	  flirFramesLost        the frames its FrameID skipped; left out when
	                        FrameID could not be read
	  flirFramesIncomplete  the incomplete frames GrabFrame left out
	  flir<counter>         each of the CAMERA_COUNTERS and STREAM_COUNTERS
	                        it can read, as much as it rose while the camera
	                        acquired
	Read before EndAcquisition (CloseCamera).
	"""
	entry = _frameCounts.pop(id(camera), None)
	counts = entry[1] if entry is not None else FrameCount(cam_params["cameraName"])
	if counts.counted:
		cam_params["flirFramesLost"] = counts.lost
	cam_params["flirFramesIncomplete"] = counts.incomplete
	for name, value in ReadCounters(camera).items():
		# A counter lower than at the start was reset since: all of it is this recording's
		before = counts.started.get(name, 0)
		cam_params["flir" + name] = value - before if value >= before else value
	text = FramesLostText(cam_params)
	if text:
		Say(text)


def FramesLostText(cam_params):
	# One line on the frames a camera lost before they reached campy, and
	# where, as far as the counters say; None when it lost none
	causes = []
	incomplete = cam_params.get("flirFramesIncomplete", 0)
	if incomplete:
		causes.append("{} arrived incomplete and were left out (USB link errors?)".format(incomplete))
	overflow = cam_params.get("flirTransferQueueOverflowCount", 0)
	if overflow:
		causes.append("the camera dropped {} it could not send in time (campy took frames too slowly, or USB too "
			"slow for this frame size and rate?)".format(overflow))
	dropped = cam_params.get("flirStreamDroppedFrameCount", 0)
	if dropped:
		causes.append("Spinnaker dropped {} older frames that campy had not taken yet (bufferMode {})".format(
			dropped, cam_params.get("bufferMode")))
	lost = cam_params.get("flirStreamLostFrameCount", 0)
	if lost:
		causes.append("Spinnaker lost {} (no free buffer, or data lost on the USB link)".format(lost))
	skipped = cam_params.get("flirFramesLost", 0)
	if not skipped and not causes:
		return None

	errors = cam_params.get("flirLinkErrorCount", 0)
	if errors:
		causes.append("its USB link had {} errors".format(errors))
	name = cam_params["cameraName"]
	if skipped:
		text = "{}: the camera's frame ID skipped {} frames: they were lost before reaching campy".format(name, skipped)
	else:
		text = "{}: frames were lost before reaching campy".format(name)
	if not causes:
		return text + " (USB too slow for this frame size and rate?)."
	causes = "; ".join(causes)
	return "{}. {}.".format(text, causes[0].upper() + causes[1:])


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
			Say("Printing device information for camera %d... \n" % cam_num)

			node_device_information = PySpin.CCategoryPtr(nodemap.GetNode("DeviceInformation"))
			if PySpin.IsAvailable(node_device_information) and PySpin.IsReadable(node_device_information):
				features = node_device_information.GetFeatures()
				for feature in features:
					node_feature = PySpin.CValuePtr(feature)
					try:
						Say("%s: %s" % (node_feature.GetName(), node_feature.ToString() if PySpin.IsReadable(
							node_feature) else "Node not readable"))
					except:
						pass
			else:
				Say("Device control information not available.")

	except Exception as e:
		logging.error("Caught error at cameras/flir.py PrintDeviceInfo: {}".format(e))

