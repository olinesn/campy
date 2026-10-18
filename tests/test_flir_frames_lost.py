"""The frames a FLIR camera loses before they reach campy, and its bufferSize, against stand-ins for PySpin and the camera."""

import csv
import importlib
import sys
import types
from collections import deque

import numpy as np
import pytest

from campy import configurator
from campy.cameras import unicam


class SpinError(Exception):
	pass


class Node:
	"""
	One node: values are what its reads give, one after another, the last
	one from then on (a counter read as the acquisition starts, then as it
	closes). error: what reading it raises.
	"""
	def __init__(self, *values, readable=True, error=None):
		self.values, self.readable, self.error = list(values), readable, error
		self.writable = False

	def GetValue(self):
		if self.error is not None:
			raise self.error
		return self.values.pop(0) if len(self.values) > 1 else self.values[0]


class NodeMap:
	def __init__(self, nodes):
		self.nodes = nodes

	def GetNode(self, name):
		return self.nodes.get(name)


class Image:
	"""
	A frame as GetNextImage gives it, with the camera's FrameID in its chunk
	data. frame_id None: a chunk that cannot be read.
	"""
	def __init__(self, frame_id, status=0):
		self.frame_id, self.status, self.released = frame_id, status, False

	def IsIncomplete(self):
		return self.status != 0

	def GetImageStatus(self):
		return self.status

	def GetChunkData(self):
		return self

	def GetFrameID(self):
		if self.frame_id is None:
			raise SpinError("Spinnaker: ChunkFrameID is not available. [-1006]")
		return self.frame_id

	def GetTimestamp(self):
		return (self.frame_id or 0) * 12_500_000 # ns, 80 fps

	def GetNDArray(self):
		return np.zeros((4, 4), dtype=np.uint8)

	def Release(self):
		self.released = True


def frames(*frame_ids):
	return [Image(i) for i in frame_ids]


def incomplete(frame_id, status=3):
	return Image(frame_id, status)


class FakeCamera:
	"""
	A FLIR camera as PySpin's CameraPtr: it sends the frames given, then
	times out. Its TL stream nodemap has Spinnaker's counters, and its device
	nodemap its own (name: Node). stream_error: what GetTLStreamNodeMap
	raises, from the read stream_error_at on (0: as the acquisition starts,
	1: as it closes). device_error: what GetNodeMap raises.
	"""
	def __init__(self, images=(), counters=None, camera_counters=None, stream_error=None, stream_error_at=0,
			device_error=None):
		self.images = list(images)
		self.stream, self.device = NodeMap(counters or {}), NodeMap(camera_counters or {})
		self.stream_error, self.stream_error_at, self.stream_reads = stream_error, stream_error_at, 0
		self.device_error = device_error
		self.calls = []

	def Init(self):
		self.calls.append("Init")

	def GetTLDeviceNodeMap(self):
		return NodeMap({"DeviceModelName": Node("Blackfly S BFS-U3-13Y3M")})

	def GetNodeMap(self):
		if self.device_error is not None:
			raise self.device_error
		return self.device

	def GetTLStreamNodeMap(self):
		self.stream_reads += 1
		if self.stream_error is not None and self.stream_reads > self.stream_error_at:
			raise self.stream_error
		return self.stream

	def BeginAcquisition(self):
		self.calls.append("BeginAcquisition")

	def GetNextImage(self, timeout):
		if not self.images:
			raise SpinError("Spinnaker: Failed waiting for EventData on NEW_BUFFER_DATA event. [-1011]")
		return self.images.pop(0)

	def IsValid(self):
		return True

	def EndAcquisition(self):
		self.calls.append("EndAcquisition")

	def DeInit(self):
		self.calls.append("DeInit")


def FakePySpin():
	spin = types.ModuleType("PySpin")
	for kind in ("CBooleanPtr", "CFloatPtr", "CIntegerPtr", "CEnumerationPtr", "CEnumEntryPtr", "CStringPtr"):
		setattr(spin, kind, lambda node: node)
	spin.IsAvailable = lambda node: node is not None
	spin.IsReadable = lambda node: node is not None and node.readable
	spin.IsWritable = lambda node: node is not None and node.writable
	spin.SpinnakerException = SpinError
	# PySpin 4.4's names for some image statuses
	spin.SPINNAKER_IMAGE_STATUS_NO_ERROR = 0
	spin.SPINNAKER_IMAGE_STATUS_MISSING_PACKETS = 3
	spin.SPINNAKER_IMAGE_STATUS_DATA_INCOMPLETE = 9
	return spin


@pytest.fixture
def flir(monkeypatch, tmp_path):
	monkeypatch.setitem(sys.modules, "PySpin", FakePySpin())
	monkeypatch.delitem(sys.modules, "campy.cameras.flir", raising=False)
	module = importlib.import_module("campy.cameras.flir")
	# The camera's settings are not what these tests are about
	monkeypatch.setattr(module, "LoadSettings", lambda camera, cam_params: cam_params)
	monkeypatch.setattr(module, "NOTE_FOLDER", str(tmp_path / ".campy"))
	yield module
	sys.modules.pop("campy.cameras.flir", None)


def params(camera, **values):
	cam = {"cameraName": "Camera0", "camera": camera, "cameraSelection": 0, "cameraDebug": False}
	cam.update(values)
	return cam


def record(flir, camera):
	# Opens the camera, grabs every frame it sends as unicam.GrabFrames does, and closes it
	camera, cam = flir.OpenCamera(params(camera))
	assert flir.StartGrabbing(camera)
	frameNumber = 0
	while camera.images:
		try:
			image = flir.GrabFrame(camera, frameNumber)
		except flir.ImageNotCompleteException:
			continue
		frameNumber += 1
		flir.ReleaseFrame(image)
	flir.CloseCamera(cam, camera)
	return cam


def counter_keys(cam):
	return {key for key in cam if key.startswith("flir") and key.endswith("Count")}


CLOSING = "Closing Camera0... Please wait.\n"


# ------------------------------------------------------------------ FrameID

def test_frame_id_gaps_are_frames_lost(flir, capsys):
	# The first FrameID need not be 0 or 1: counting starts from it
	cam = record(flir, FakeCamera(frames(1000, 1001, 1004, 1005, 1010, 1011)))
	assert cam["flirFramesLost"] == 6 and cam["flirFramesIncomplete"] == 0
	assert capsys.readouterr().out == CLOSING + "Camera0: the camera's frame ID skipped 6 frames: they were lost " \
		"before reaching campy (USB too slow for this frame size and rate?).\n"


def test_no_gaps_is_silent(flir, capsys):
	cam = record(flir, FakeCamera(frames(*range(37, 137))))
	assert cam["flirFramesLost"] == 0 and cam["flirFramesIncomplete"] == 0
	assert capsys.readouterr().out == CLOSING


@pytest.mark.parametrize("frame_ids, lost", [
	((7, 8, 9, 0, 1, 2), 0), # reset
	((65534, 65535, 0, 1, 3), 1), # a 16-bit counter wrapping, then a frame lost
	((5, 5, 6), 0), # the same twice
	])
def test_frame_id_starting_over_is_not_frames_lost(flir, frame_ids, lost):
	assert record(flir, FakeCamera(frames(*frame_ids)))["flirFramesLost"] == lost


def test_a_new_acquisition_starts_the_count_over(flir):
	camera = FakeCamera(frames(10, 11))
	camera, cam = flir.OpenCamera(params(camera))
	assert flir.StartGrabbing(camera)
	for frameNumber in (0, 1):
		flir.GrabFrame(camera, frameNumber)
	camera.images = frames(20, 21)
	assert flir.StartGrabbing(camera)
	for frameNumber in (2, 3):
		flir.GrabFrame(camera, frameNumber)
	flir.CloseCamera(cam, camera)
	assert cam["flirFramesLost"] == 0


@pytest.mark.parametrize("frame_ids", [
	(None, None, None), # no FrameID chunk
	(0, 0, 0, 0), # a chunk read as 0 every time
	(9,), # one frame: no step to count
	])
def test_a_frame_id_that_cannot_be_read_is_left_out(flir, frame_ids, capsys):
	cam = record(flir, FakeCamera(frames(*frame_ids)))
	assert "flirFramesLost" not in cam and cam["flirFramesIncomplete"] == 0
	assert capsys.readouterr().out == CLOSING


def test_nothing_is_counted_across_a_frame_id_that_cannot_be_read(flir):
	# Frame 3 came, its FrameID did not: 2 to 10 is not 7 frames lost
	cam = record(flir, FakeCamera(frames(1, 2, None, 10, 11, 13)))
	assert cam["flirFramesLost"] == 1


# ------------------------------------------------------------------ incomplete frames

def test_incomplete_frames_are_counted_and_said_every_5_s(flir, monkeypatch, capsys):
	# One line per incomplete frame was printed before
	clock = [100.0, 101.0, 104.9, 105.0, 106.0, 111.0]
	monkeypatch.setattr(flir.time, "perf_counter", lambda: clock.pop(0) if len(clock) > 1 else clock[0])
	images = frames(1) + [incomplete(i) for i in (2, 3, 4, 5, 6)] + frames(7) + [incomplete(8, status=9)] + frames(9)
	cam = record(flir, FakeCamera(images))
	assert cam["flirFramesIncomplete"] == 6 and cam["flirFramesLost"] == 6
	assert capsys.readouterr().out == \
		"Camera0: a frame arrived incomplete (image status 3: missing packets). Left out 1 incomplete frames so far " \
		"(latest: after frame 1).\n" \
		"Camera0: a frame arrived incomplete (image status 3: missing packets). Left out 4 incomplete frames so far " \
		"(latest: after frame 1).\n" \
		"Camera0: a frame arrived incomplete (image status 9: data incomplete). Left out 6 incomplete frames so far " \
		"(latest: after frame 2).\n" + CLOSING + \
		"Camera0: the camera's frame ID skipped 6 frames: they were lost before reaching campy. 6 arrived " \
		"incomplete and were left out (USB link errors?).\n"


def test_an_incomplete_frame_is_still_released(flir):
	image = incomplete(2)
	camera = FakeCamera([image])
	with pytest.raises(flir.ImageNotCompleteException):
		flir.GrabFrame(camera, 0)
	assert image.released


def test_an_image_status_without_a_name(flir, capsys):
	with pytest.raises(flir.ImageNotCompleteException):
		flir.GrabFrame(FakeCamera([incomplete(2, status=42)]), 0)
	assert "(image status 42)" in capsys.readouterr().out


# ------------------------------------------------------------------ Spinnaker's stream counters

def counters(**values):
	# Each counter as read when the acquisition starts, then as it closes
	return {name: Node(*value) for name, value in values.items()}


def test_stream_counters_are_saved_as_they_rose(flir, capsys):
	# What a run that did not close the camera left on them is not this
	# recording's (RecoverAcquisition starts and stops an acquisition); a
	# counter that went down was reset, and all of it is
	camera = FakeCamera(frames(1, 2, 3, 4, 5, 14, 15), counters(
		StreamStartedFrameCount=(4, 1019),
		StreamDeliveredFrameCount=(2, 1004),
		StreamIncompleteFrameCount=(0, 0),
		StreamLostFrameCount=(1, 9),
		StreamDroppedFrameCount=(50, 0),
		)) # no StreamReceivedFrameCount, as on USB
	cam = record(flir, camera)
	assert {key: cam[key] for key in counter_keys(cam)} == {
		"flirStreamStartedFrameCount": 1015,
		"flirStreamDeliveredFrameCount": 1002,
		"flirStreamIncompleteFrameCount": 0,
		"flirStreamLostFrameCount": 8,
		"flirStreamDroppedFrameCount": 0,
		}
	assert capsys.readouterr().out == CLOSING + "Camera0: the camera's frame ID skipped 8 frames: they were lost " \
		"before reaching campy. Spinnaker lost 8 (no free buffer, or data lost on the USB link).\n"


def test_frames_spinnaker_dropped(flir, capsys):
	camera = FakeCamera(frames(1, 2, 6), counters(StreamDroppedFrameCount=(0, 3), StreamLostFrameCount=(0, 0)))
	camera, cam = flir.OpenCamera(params(camera, bufferMode="Newest Only"))
	flir.StartGrabbing(camera)
	for frameNumber in range(3):
		flir.GrabFrame(camera, frameNumber)
	flir.CloseCamera(cam, camera)
	assert capsys.readouterr().out == CLOSING + "Camera0: the camera's frame ID skipped 3 frames: they were lost " \
		"before reaching campy. Spinnaker dropped 3 older frames that campy had not taken yet (bufferMode Newest " \
		"Only).\n"


def test_the_camera_s_own_counters(flir, capsys):
	# As the EVO-X2's Blackfly S, its camera thread held for 3 s at 80 fps:
	# 99 frames dropped on the camera, none in Spinnaker. They count from the
	# camera's power-up: only what this recording added is saved.
	camera = FakeCamera(frames(*range(1, 801), *range(900, 1000)),
		counters(StreamLostFrameCount=(0, 0), StreamDroppedFrameCount=(0, 0)),
		counters(TransferQueueOverflowCount=(270807, 270906), LinkErrorCount=(132, 132)))
	cam = record(flir, camera)
	assert cam["flirFramesLost"] == cam["flirTransferQueueOverflowCount"] == 99 and cam["flirLinkErrorCount"] == 0
	assert capsys.readouterr().out == CLOSING + "Camera0: the camera's frame ID skipped 99 frames: they were lost " \
		"before reaching campy. The camera dropped 99 it could not send in time (campy took frames too slowly, or " \
		"USB too slow for this frame size and rate?).\n"


def test_link_errors_are_said_with_frames_lost(flir, capsys):
	camera = FakeCamera(frames(1) + [incomplete(2)] + frames(3), camera_counters=counters(LinkErrorCount=(132, 135)))
	record(flir, camera)
	assert capsys.readouterr().out.endswith("Camera0: the camera's frame ID skipped 1 frames: they were lost before "
		"reaching campy. 1 arrived incomplete and were left out (USB link errors?); its USB link had 3 errors.\n")


def test_link_errors_alone_are_silent(flir, capsys):
	# The link recovered, and no frame was lost
	cam = record(flir, FakeCamera(frames(1, 2, 3), camera_counters=counters(LinkErrorCount=(132, 133))))
	assert cam["flirLinkErrorCount"] == 1 and capsys.readouterr().out == CLOSING


def test_counters_say_so_without_frame_id(flir, capsys):
	camera = FakeCamera(frames(None, None), counters(StreamLostFrameCount=(0, 2)))
	cam = record(flir, camera)
	assert "flirFramesLost" not in cam and cam["flirStreamLostFrameCount"] == 2
	assert capsys.readouterr().out == CLOSING + "Camera0: frames were lost before reaching campy. Spinnaker lost 2 " \
		"(no free buffer, or data lost on the USB link).\n"


def test_the_counters_are_read_before_end_acquisition(flir, monkeypatch):
	# Ending the acquisition can reset them
	camera = FakeCamera(frames(1, 2), counters(StreamLostFrameCount=(0, 5)))
	camera, cam = flir.OpenCamera(params(camera))
	flir.StartGrabbing(camera)
	read = []
	read_counters = flir.ReadCounters
	monkeypatch.setattr(flir, "ReadCounters", lambda camera: read.append(list(camera.calls)) or read_counters(camera))
	flir.CloseCamera(cam, camera)
	assert read == [["Init", "BeginAcquisition"]] and cam["flirStreamLostFrameCount"] == 5


NOT_READABLE = {
	"no such counters": lambda: FakeCamera(frames(1, 2)),
	"not readable": lambda: FakeCamera(frames(1, 2), {name: Node(3, readable=False) for name in COUNTERS},
		{name: Node(3, readable=False) for name in CAMERA_COUNTERS}),
	"reading raises": lambda: FakeCamera(frames(1, 2), {name: Node(error=READ_FAILED) for name in COUNTERS},
		{name: Node(error=READ_FAILED) for name in CAMERA_COUNTERS}),
	"no nodemaps": lambda: FakeCamera(frames(1, 2), stream_error=SpinError("Spinnaker: not available. [-1006]"),
		device_error=SpinError("Spinnaker: not available. [-1006]")),
	}
COUNTERS = ("StreamStartedFrameCount", "StreamDeliveredFrameCount", "StreamReceivedFrameCount",
	"StreamIncompleteFrameCount", "StreamLostFrameCount", "StreamDroppedFrameCount")
CAMERA_COUNTERS = ("TransferQueueOverflowCount", "LinkErrorCount")
READ_FAILED = SpinError("Spinnaker: read failed. [-1010]")


@pytest.mark.parametrize("case", list(NOT_READABLE))
def test_counters_that_cannot_be_read_are_left_out(flir, case, capsys):
	camera = NOT_READABLE[case]()
	cam = record(flir, camera)
	assert counter_keys(cam) == set() # left out, not 0
	assert cam["flirFramesLost"] == 0
	assert camera.calls == ["Init", "BeginAcquisition", "EndAcquisition", "DeInit"]
	assert capsys.readouterr().out == CLOSING


def test_only_the_counters_it_can_read(flir):
	camera = FakeCamera(frames(1, 2), {"StreamLostFrameCount": Node(0), "StreamDroppedFrameCount": Node(0, readable=False)})
	assert counter_keys(record(flir, camera)) == {"flirStreamLostFrameCount"}


def test_a_counter_read_that_raises_at_close_still_closes(flir, capsys):
	# Read as the acquisition started; the camera unplugged before it closed
	camera = FakeCamera(frames(1, 2, 3), counters(StreamLostFrameCount=(0, 0)),
		stream_error=SpinError("Spinnaker: Camera is not valid. [-1015]"), stream_error_at=1)
	cam = record(flir, camera)
	assert camera.calls == ["Init", "BeginAcquisition", "EndAcquisition", "DeInit"]
	assert counter_keys(cam) == set() and cam["flirFramesLost"] == 0
	assert capsys.readouterr().out == CLOSING


def test_close_goes_on_when_saving_the_frames_lost_fails(flir, monkeypatch, capsys):
	# Whatever goes wrong there: the camera is still closed
	def fail(cam_params, camera):
		raise RuntimeError("something unforeseen")

	monkeypatch.setattr(flir, "SaveFramesLost", fail)
	camera = FakeCamera(frames(1, 2))
	record(flir, camera)
	assert camera.calls == ["Init", "BeginAcquisition", "EndAcquisition", "DeInit"]
	assert capsys.readouterr().out == CLOSING + "Camera0: SaveFramesLost failed as it closed: something unforeseen\n"


def test_a_counter_that_cannot_be_read_at_the_start_does_not_stop_grabbing(flir):
	camera = FakeCamera(frames(1, 2), stream_error=SpinError("Spinnaker: not available. [-1006]"))
	camera, cam = flir.OpenCamera(params(camera))
	assert flir.StartGrabbing(camera) is True


# ------------------------------------------------------------------ in metadata.csv

def grab_frames(flir, tmp_path, camera, frames_to_grab):
	# A recording through unicam.GrabFrames, as a camera process runs one; its metadata.csv
	p = configurator.DefaultParams()
	p.update(cameraMake="flir", cameraName="Camera0", camera=camera, cameraSerialNo="24243505", cameraSelection=0,
		videoFolder=str(tmp_path), frameRate=100, recTimeInSec=frames_to_grab / 100, chunkLengthInSec=10,
		displayFrameRate=0, writeQueueMaxSec="None")
	unicam.GrabFrames(p, deque(), deque([], 2), deque([], 1), deque([], 1))
	with open(tmp_path / "Camera0" / "metadata.csv") as f:
		return dict(csv.reader(f))


def test_metadata_csv_has_the_frames_lost(flir, tmp_path, capsys):
	camera = FakeCamera(frames(1, 2, 3, 4, 5) + [incomplete(6)] + frames(7, 8, 9, 12),
		counters(StreamDeliveredFrameCount=(0, 10), StreamIncompleteFrameCount=(0, 1), StreamLostFrameCount=(0, 2)))
	meta = grab_frames(flir, tmp_path, camera, 9)
	assert meta["totalFrames"] == "9"
	assert meta["flirFramesLost"] == "3" and meta["flirFramesIncomplete"] == "1"
	assert meta["flirStreamDeliveredFrameCount"] == "10" and meta["flirStreamIncompleteFrameCount"] == "1"
	assert meta["flirStreamLostFrameCount"] == "2" and "flirStreamDroppedFrameCount" not in meta
	assert "Camera0: the camera's frame ID skipped 3 frames: they were lost before reaching campy. 1 arrived " \
		"incomplete and were left out (USB link errors?); Spinnaker lost 2 (no free buffer, or data lost on the USB " \
		"link).\n" in capsys.readouterr().out


def test_metadata_csv_when_the_counters_fail_at_close(flir, tmp_path):
	camera = FakeCamera(frames(1, 2, 3), counters(StreamLostFrameCount=(0, 0)),
		counters(TransferQueueOverflowCount=(7, 7)),
		stream_error=SpinError("Spinnaker: Camera is not valid. [-1015]"), stream_error_at=1)
	meta = grab_frames(flir, tmp_path, camera, 3)
	assert meta["totalFrames"] == "3" and meta["flirFramesLost"] == "0"
	assert not any(key.startswith("flirStream") for key in meta) and meta["flirTransferQueueOverflowCount"] == "0"
	assert camera.calls[-2:] == ["EndAcquisition", "DeInit"]


# ------------------------------------------------------------------ bufferSize

class Entry:
	def __init__(self, symbolic, value):
		self.symbolic, self.value = symbolic, value
		self.readable = True

	def GetValue(self):
		return self.value

	def GetDisplayName(self):
		return self.symbolic

	def GetSymbolic(self):
		return self.symbolic


class Enumeration:
	# An enumeration node, with PySpin 4.4's entries and values
	def __init__(self, entries, current):
		self.entries = {name: Entry(name, value) for name, value in entries.items()}
		self.value = self.entries[current].value
		self.readable = self.writable = True

	def GetCurrentEntry(self):
		return next(e for e in self.entries.values() if e.value == self.value)

	def GetEntryByName(self, name):
		return self.entries.get(name)

	def SetIntValue(self, value):
		self.value = value


class Integer:
	def __init__(self, value):
		self.value = value
		self.readable = self.writable = True

	def GetValue(self):
		return self.value

	def SetValue(self, value):
		self.value = value


@pytest.mark.parametrize("buffers", [100, 37])
def test_buffer_size_is_the_count_set(flir, buffers):
	# It was the "Manual" entry's value, 0
	stream = NodeMap({
		"StreamBufferHandlingMode": Enumeration(
			{"OldestFirst": 0, "OldestFirstOverwrite": 1, "NewestOnly": 2, "NewestFirst": 3}, "NewestFirst"),
		"StreamBufferCountMode": Enumeration({"Manual": 0, "Auto": 1}, "Auto"),
		"StreamBufferCountManual": Integer(10),
		})
	camera = types.SimpleNamespace(GetTLStreamNodeMap=lambda: stream)
	cam = flir.ConfigureBuffer(camera, {"bufferSize": buffers, "bufferMode": "OldestFirst"})
	assert cam["bufferSize"] == buffers == stream.nodes["StreamBufferCountManual"].value
	assert cam["bufferMode"] == "OldestFirst" and stream.nodes["StreamBufferHandlingMode"].value == 0
	assert stream.nodes["StreamBufferCountMode"].value == 0 # Manual
