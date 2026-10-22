"""
Emulated cameras (cameraMake: "emu"): each plays a recorded clip as if it were
a camera, at the configured frame rate, looping back to the clip's start when
it runs out. They stand in for real cameras in tests that need no hardware,
campy-live's included:
	* With pixelFormatInput "gray", each frame has one channel (height x width),
	  as a Mono8 FLIR camera's does (imageio's reader gave every clip 3).
	* In campy-acquire, every emulated camera times its frames from one start,
	  which campy's main process sets once every camera is ready (StartClock),
	  as a hardware trigger starts them all: frame n reaches the computer at
	  the same moment on every camera.
	* The loop back to the clip's start does not pause: ffmpeg loops the clip.
"""

from campy.cameras import unicam
from campy.utils.utils import Say
import os
import time
import logging
import sys
import numpy as np
from collections import deque
import csv
import imageio_ffmpeg


# How long GrabFrame waits for the shared start before it gives up, as a
# triggered camera's does for its next frame (FrameWatch says nothing about it)
WAIT_FOR_START_SEC = 0.05

# Frame 0 comes this long after campy's main process says go (StartClock), so
# that every camera process has read the start before it is due
START_LEAD_SEC = 0.1

# Set in each camera process (campy.RunCamera): the start its frames are timed
# from, shared with campy's main process (a multiprocessing Value of
# time.perf_counter(), 0 until the main process sets it). None outside
# campy-acquire: each camera then starts its own clock at its first frame.
_start = None


def SetStartClock(start):
	global _start
	_start = start


def StartClock(start, lead=START_LEAD_SEC):
	"""
	In campy's main process, once every camera is ready (where it starts the
	trigger): every emulated camera's frame 0 comes lead seconds from now, and
	frame n n frame periods after it. time.perf_counter() is the same clock in
	every process, on Windows, Linux and macOS.
	"""
	if start is not None:
		start.value = time.perf_counter() + lead


class NotStarted(Exception):
	# campy's main process has not said go yet (StartClock)
	pass


class Clip:
	"""
	A clip's frames, one after another, from its start again once they run
	out, from one ffmpeg that loops the clip itself (-stream_loop -1). gray:
	one channel per frame, ffmpeg's gray (the clip's luma), else RGB. Going
	back to frame 0 with imageio's reader started ffmpeg again, which held up
	the frame after the clip's last for 110-150 ms.
	"""

	def __init__(self, path, gray=False):
		self.path = path
		self.channels = 1 if gray else 3
		self.frames = None
		self.Open()
		# The first frame now: a clip that cannot be read fails here, as its camera opens
		self.last = self.Read()
		self.pos = 0 # the number of the frame read last, counting on through the loops

	def Open(self):
		self.frames = imageio_ffmpeg.read_frames(self.path, pix_fmt="gray" if self.channels == 1 else "rgb24",
			bits_per_pixel=8 * self.channels, input_params=["-stream_loop", "-1"])
		meta = next(self.frames)
		self.width, self.height = meta["size"]
		self.fps = meta.get("fps")

	def Read(self):
		try:
			data = next(self.frames)
		except StopIteration:
			raise IOError("{}: no frames".format(self.path))
		shape = (self.height, self.width) if self.channels == 1 else (self.height, self.width, 3)
		return np.frombuffer(data, np.uint8).reshape(shape).copy() # writable, as imageio's were

	def get_data(self, index):
		# Frame index, counting on through the loops: unicam.GrabFrames asks for
		# 0, 1, 2, ..., or the same one again
		if index < self.pos:
			self.frames.close()
			self.Open()
			self.last, self.pos = self.Read(), 0
		while self.pos < index:
			self.last = self.Read()
			self.pos += 1
		return self.last

	def close(self):
		self.frames.close()


class EmulatedCamera:
	# A clip served as a camera's frames, at the configured frame rate
	def __init__(self, clip, frameRate, start=None):
		self.clip = clip
		self.framePeriod = 1.0 / frameRate if frameRate > 0 else 0.0
		self.start = start # shared with campy's main process (SetStartClock), or None
		self.startTime = None

	def Start(self, wait=WAIT_FOR_START_SEC):
		# When frame 0 is due: the shared start, once campy's main process has
		# set it (raises NotStarted after waiting wait seconds for it), or now
		if self.startTime is None:
			if self.start is None:
				self.startTime = time.perf_counter()
			else:
				deadline = time.perf_counter() + wait
				while not self.start.value:
					if time.perf_counter() >= deadline:
						raise NotStarted("waiting for campy to start the emulated cameras")
					time.sleep(0.001)
				self.startTime = self.start.value
		return self.startTime

	def get_data(self, frameNumber):
		start = self.Start()
		# The frame first, then the wait until its time (relative to frame 0):
		# reading it does not make it late
		img = self.clip.get_data(frameNumber)
		delay = start + frameNumber * self.framePeriod - time.perf_counter()
		if delay > 0:
			time.sleep(delay)
		return img

	def close(self):
		self.clip.close()


def LoadSystem(params):
	# One emulated device per camera, named emu0, emu1, ...: cameraSelection
	# indexes into this list. (It was the string "emu", so cameras past the
	# third indexed past its last character.)
	selection = params.get("cameraSelection", [])
	if not isinstance(selection, list):
		selection = [selection]
	count = max([params["numCams"]] + [s + 1 for s in selection if isinstance(s, int)])
	return ["emu{}".format(n) for n in range(count)]


def GetDeviceList(system):

	return system


def LoadDevice(systems, params, cam_params):

	return cam_params


def GetSerialNumber(device):

	return device


def GetModelName(camera):

	return "Emulated_Camera"


def ClipPath(cam_params):
	# <videoFolder>/<cameraName>/<videoFilename without its first 3 characters>:
	# videoFilename "emuclip.mp4" plays clip.mp4. The clip is in the configured
	# videoFolder, not in this run's session folder.
	videoFileName = cam_params["videoFilename"][3:len(cam_params["videoFilename"])]
	folder = cam_params.get("videoFolderRoot", cam_params["videoFolder"])
	return os.path.join(folder, cam_params["cameraName"], videoFileName)


def OpenCamera(cam_params):
	# Open video reader for emulation
	full_file_name = ClipPath(cam_params)
	# pixelFormatInput "gray": one channel, as a Mono8 camera's frames (imageio read them as RGB)
	clip = Clip(full_file_name, gray=str(cam_params.get("pixelFormatInput")) == "gray")
	camera = EmulatedCamera(clip, cam_params["frameRate"], _start)

	# The clip's frame size, for the writer (which waits for it) and metadata
	cam_params = unicam.SetFrameSize(cam_params, clip.width, clip.height, "the clip's frame size")
	cam_params['cameraModel'] = GetModelName(camera)

	Say("Opened {} emulation.".format(cam_params["cameraName"]))
	return camera, cam_params


def LoadSettings(cam_params, camera):

	return cam_params


def StartGrabbing(camera):

	return True


def GrabFrame(camera, frameNumber):

	return camera.get_data(frameNumber)


def GetImageArray(grabResult):

	return grabResult


def GetTimeStamp(grabResult):

	return time.perf_counter()


def DisplayImage(cam_params, dispQueue, grabResult):
	# Downsample image (a gray frame has no channel axis)
	img = grabResult[::cam_params["displayDownsample"],::cam_params["displayDownsample"]]

	# Send to display queue
	dispQueue.append(img)


def ReleaseFrame(grabResult):

	del grabResult


def CloseCamera(cam_params, camera):
	Say('Closing {}... Please wait.'.format(cam_params["cameraName"]))
	# Close camera after acquisition stops; what fails is said, not raised
	try:
		camera.close()
	except Exception as e:
		Say("{}: the clip did not close: {}".format(cam_params["cameraName"], e))


def CloseSystem(system, device_list):
	del system
	del device_list
