"""
RatCam: the NIR eye cameras of nir-eye-cam (github.com/olinesn/nir-eye-cam),
an HM01B0 on an RP2040 or a VD55G1 on an ESP32-P4, recorded alongside
campy's other cameras.

A RatCam is a USB serial device. Each frame is 8-bit mono and carries the
camera's own frame counter and microsecond timestamp, and the camera also
timestamps every pulse on its SYNC input, on the same clock. The nireyecam
package (nir-eye-cam's host/ folder) speaks its protocol:
	pip install -e <nir-eye-cam>/host     # or: pip install -e '.[ratcam]' in campy

Config (each can be one value, or one per camera):
	cameraMake: "ratcam"
	cameraSelection: index among the RatCams, in order of USB serial number
	frameWidth, frameHeight: a resolution the firmware supports
	frameRate: reached by setting the sensor's frame length; a rate the
		sensor cannot reach at that size stops the camera with the highest it can
	cameraExposureTimeInUs: converted to sensor lines
	ratcamGain: analog gain code, firmware's AGAIN (RP2040 0-3, ESP32-P4 0-28)
	ratcamLed: IR LED level 0-1000 (ESP32-P4: on or off); 0, the default, = off
	ratcamSyncOut: drive the random sync pulse train on SYNC_OUT (one camera)
	ratcamPorts: "None" finds RatCams by USB; or a list of serial ports to use
		instead, where "sim" is a simulated camera (for trying campy without one)

Besides campy's frametimes and metadata, each RatCam folder gets:
	ratcam_frames.csv  every frame grabbed: campy's frame number, the camera's
	                   frame counter (a gap = a frame lost before campy saw it),
	                   its timestamp in us, and the computer's arrival time in ns
	ratcam_sync.csv    every SYNC input pulse: its counter, its timestamp in us,
	                   and the computer's arrival time in ns, in order of time
Both are written while the recording runs: opened when the camera starts
grabbing, a batch of rows about every second, closed with the camera. So
memory does not grow with the recording, and one that stops without closing
(campy crashed or was killed) keeps its rows up to the last second.
"""

import csv, os, queue, time
from campy import newfiles
from campy.cameras import unicam
from campy.utils.utils import Say


PIXEL_FORMAT = "gray" # every RatCam frame is 8-bit mono
GRAB_TIMEOUT_S = 0.05 # like flir.GrabFrame: wait this long for the next frame
WRITE_EVERY_NS = 1_000_000_000 # the logs' rows go to disk about this often


class RatCamError(Exception):
	pass


class CsvLog:
	"""
	A CSV file that grows as rows arrive. They wait in memory until Write,
	and then go to disk together and are flushed, so the file on disk ends
	with a whole row.
	"""

	def __init__(self, path, header):
		self.path, self.header = path, header
		self.file = None # opened by Open, when the camera starts grabbing
		self.rows = []   # not yet on disk
		self.count = 0   # rows in all, besides the header

	def Add(self, row):
		self.rows.append(row)
		self.count += 1

	def Open(self):
		if self.file is None:
			self.file = newfiles.Open(self.path, "w", newline="") # never over a recording's log
			csv.writer(self.file).writerow(self.header)
			self.Write() # rows from before it opened

	def Write(self):
		if self.file is not None and self.rows:
			csv.writer(self.file).writerows(self.rows)
			self.file.flush()
			self.rows = []

	def Close(self):
		self.Open()
		self.Write()
		self.file.close()


def SortByTime(path):
	# Rewrites a log in order of t_us, keeping the order of rows with equal
	# times. Through a new file renamed over the old, so a crash here leaves
	# the log as it was.
	with open(path, newline="") as f:
		rows = list(csv.reader(f))
	t = rows[0].index("t_us")
	rows[1:] = sorted(rows[1:], key=lambda row: int(row[t]))
	with open(path + ".sorting", "w", newline="") as f:
		csv.writer(f).writerows(rows)
	os.replace(path + ".sorting", path)


class Logs:
	"""ratcam_frames.csv and ratcam_sync.csv of one RatCam."""

	def __init__(self, folder):
		self.folder = folder
		self.frames = CsvLog(os.path.join(folder, "ratcam_frames.csv"), ["frame", "camera_frame", "t_us", "host_ns"])
		self.sync = CsvLog(os.path.join(folder, "ratcam_sync.csv"), ["pulse", "t_us", "host_ns"])
		self.counter = None # the camera's frame counter at the last frame
		self.lost = 0       # frames it skipped: lost before they reached campy
		self.pulse_us = None # the last pulse's time
		self.in_order = True # every pulse so far after the one before
		self.written_ns = time.monotonic_ns()

	def Frame(self, counter, t_us, host_ns):
		# Each grabbed frame, in campy's order: frame is campy's frame number
		if self.counter is not None:
			self.lost += max(counter - self.counter - 1, 0)
		self.counter = counter
		self.frames.Add((self.frames.count + 1, counter, t_us, host_ns))

	def Pulse(self, counter, t_us, host_ns):
		# Pulses come in order, from one queue; one that does not puts the
		# log in order of time when it closes
		if self.pulse_us is not None and t_us < self.pulse_us:
			self.in_order = False
		self.pulse_us = t_us
		self.sync.Add((counter, t_us, host_ns))

	def Open(self):
		os.makedirs(self.folder, exist_ok=True)
		self.frames.Open()
		self.sync.Open()
		self.written_ns = time.monotonic_ns()

	def WriteIfDue(self):
		now = time.monotonic_ns()
		if now - self.written_ns >= WRITE_EVERY_NS:
			self.written_ns = now
			self.frames.Write()
			self.sync.Write()

	def Close(self):
		os.makedirs(self.folder, exist_ok=True)
		self.frames.Close()
		self.sync.Close()
		if not self.in_order:
			SortByTime(self.sync.path)


class RatCam:
	"""One open RatCam, and the logs of the frames and sync pulses it delivers."""

	def __init__(self, cam, folder):
		self.cam = cam    # nireyecam.device.Camera
		self.logs = Logs(folder)


def FixedParams(cam_params):
	# What every RatCam sends, whatever the config says. Before the camera opens:
	# the writer (and the launch check) read it from cam_params.
	cam_params["pixelFormatInput"] = PIXEL_FORMAT
	return cam_params


def PortSerials():
	# USB serial number of each serial port, where the OS reports one
	try:
		from serial.tools import list_ports
		return {p.device: p.serial_number for p in list_ports.comports()}
	except Exception:
		return {}


def LoadSystem(params):
	"""
	The RatCams to choose from, as {"port", "serial"} dicts. Found by USB and
	sorted by serial number, so cameraSelection picks the same camera after a
	reboot, when the ports may come up in another order.
	"""
	ports = params.get("ratcamPorts", "None")
	if ports is None or str(ports) == "None":
		from nireyecam.device import discover
		found = sorted(discover(), key=lambda c: (str(c.serial_number or ""), c.port))
		return [{"port": c.port, "serial": c.serial_number or c.port} for c in found]

	if not isinstance(ports, list):
		ports = [ports]
	serials = PortSerials()
	devices = []
	for n, port in enumerate(str(p) for p in ports):
		if port == "sim":
			devices.append({"port": "sim:{}".format(n), "serial": "sim{}".format(n)})
		elif port.startswith("sim:"):
			devices.append({"port": port, "serial": "sim{}".format(port[4:])})
		else:
			devices.append({"port": port, "serial": serials.get(port) or port})
	return devices


def GetDeviceList(system):

	return system


def LoadDevice(systems, params, cam_params):

	return FixedParams(cam_params)


def GetSerialNumber(device):

	return device["serial"]


def Reply(cam, line):
	# First number of a reply: "OK FLL 344 EXP 107" -> 344
	return int(cam.command(line).split()[0])


def Drain(camera):
	# Empty the receive queue, keeping any sync pulses
	while True:
		try:
			packet, host_ns = camera.cam.data.get_nowait()
		except queue.Empty:
			return
		Keep(camera, packet, host_ns)


def Keep(camera, packet, host_ns):
	from nireyecam import protocol
	if packet.type == protocol.SYNC:
		camera.logs.Pulse(packet.seq, packet.t_us, host_ns)


def MeasureLineTimeNs(camera, frame_length, frames=8, timeout_s=3.0):
	"""
	Sensor line time, from the camera's own frame timestamps at a known frame
	length. The RP2040 build needs this: the Arducam module's oscillator, and
	so its line time, is not specified.
	"""
	from nireyecam import protocol
	cam = camera.cam
	Drain(camera)
	cam.command("START")
	got, skipped = [], 0
	deadline = time.monotonic() + timeout_s
	try:
		while len(got) < frames and time.monotonic() < deadline:
			try:
				packet, host_ns = cam.data.get(timeout=0.1)
			except queue.Empty:
				continue
			if packet.type != protocol.FRAME:
				Keep(camera, packet, host_ns)
			elif skipped < 2: # settling after START
				skipped += 1
			else:
				got.append((packet.seq, packet.t_us))
	finally:
		cam.command("STOP")
	time.sleep(0.05)
	Drain(camera)
	if len(got) < 3:
		raise RatCamError("only {} frames in {:g} s while measuring the frame rate".format(len(got), timeout_s))
	periods = sorted((b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(got, got[1:]) if b[0] > a[0])
	return periods[len(periods) // 2] * 1000.0 / frame_length


def Configure(camera, cam_params):
	"""Apply the config to the camera; raise RatCamError with the reason if it cannot."""
	cam, name = camera.cam, cam_params["cameraName"]
	cam.command("STOP")
	info = cam.info()
	if not info.get("cam_ok"):
		raise RatCamError("{}: the sensor did not start: {}".format(name, info.get("cam_error") or "no reason given"))

	w, h = int(cam_params["frameWidth"]), int(cam_params["frameHeight"])
	cam.command("AE 0")
	cam.command("RES {}x{}".format(w, h))
	info = cam.info()

	# Frame rate, through the frame length (lines per frame)
	line_ns = float(info.get("line_time_ns") or 0) # the ESP32-P4 reports it
	if line_ns <= 0:
		line_ns = MeasureLineTimeNs(camera, int(info["frame_length"]))
	rate = float(cam_params["frameRate"])
	want = int(round(1e9 / (rate * line_ns)))
	if want > 0xFFFF:
		raise RatCamError("{}: frameRate {:g} is below the slowest this sensor runs at {}x{} ({:.2f} fps)."\
			.format(name, rate, w, h, 1e9 / (0xFFFF * line_ns)))
	fll = Reply(cam, "FLL {}".format(want))
	if fll > want: # the firmware clamps to the shortest frame the sensor allows
		raise RatCamError("{}: {}x{} runs at up to {:.1f} fps on this camera; frameRate is {:g}."\
			.format(name, w, h, 1e9 / (fll * line_ns), rate))

	# Exposure, in lines; the firmware keeps it inside the frame
	want_exp = max(1, int(round(float(cam_params["cameraExposureTimeInUs"]) * 1000.0 / line_ns)))
	exposure = Reply(cam, "EXP {}".format(want_exp))
	if exposure < want_exp:
		Say("{}: exposure shortened to {:.0f} us, the longest a {:g} fps frame allows."\
			.format(name, exposure * line_ns / 1000.0, rate))

	gain = Reply(cam, "AGAIN {}".format(int(cam_params.get("ratcamGain", 0))))
	led = Reply(cam, "LED {}".format(int(cam_params.get("ratcamLed", 0))))
	sync_out = Reply(cam, "SYNCGEN {}".format(1 if cam_params.get("ratcamSyncOut") else 0))

	cam_params["cameraModel"] = "{} {}".format(info.get("board", "?"), info.get("sensor", "hm01b0"))
	cam_params["ratcamFirmware"] = str(info.get("fw", "?"))
	cam_params["ratcamLineTimeNs"] = round(line_ns, 3)
	cam_params["ratcamFrameLength"] = fll
	cam_params["ratcamFrameRate"] = round(1e9 / (fll * line_ns), 4)
	cam_params["ratcamExposureLines"] = exposure
	cam_params["ratcamExposureUs"] = round(exposure * line_ns / 1000.0, 1)
	cam_params["ratcamGainApplied"] = gain
	cam_params["ratcamLedApplied"] = led
	cam_params["ratcamSyncOutApplied"] = sync_out
	return cam_params


def OpenCamera(cam_params):
	from nireyecam.device import open_camera
	device = cam_params["device"]
	camera = RatCam(open_camera(device["port"], cam_params["cameraName"]),
		os.path.join(cam_params["videoFolder"], cam_params["cameraName"]))
	try:
		cam_params = Configure(camera, FixedParams(cam_params))
	except Exception:
		CloseQuietly(camera)
		raise
	Say("{}: {}x{} at {:.2f} fps (frame length {}), exposure {:.0f} us, gain code {}, LED {}{}."\
		.format(cam_params["cameraName"], cam_params["frameWidth"], cam_params["frameHeight"],
		cam_params["ratcamFrameRate"], cam_params["ratcamFrameLength"], cam_params["ratcamExposureUs"],
		cam_params["ratcamGainApplied"], cam_params["ratcamLedApplied"],
		", driving SYNC_OUT" if cam_params["ratcamSyncOutApplied"] else ""))
	return camera, cam_params


def StartGrabbing(camera):
	try:
		Drain(camera)
		camera.logs.Open() # with any sync pulses from while it was configured
		camera.cam.command("START")
		return True
	except Exception as e:
		Say("RatCam did not start: {}".format(e))
		return False


def GrabFrame(camera, frameNumber):
	from nireyecam import protocol
	cam = camera.cam
	camera.logs.WriteIfDue() # here, so they are written while no frames come too
	deadline = time.monotonic() + GRAB_TIMEOUT_S
	while True:
		if cam.error is not None:
			# Its reader thread has ended (the USB link failed): no frame will
			# come again, so this camera's recording ends (unicam.GrabFrames)
			raise unicam.CameraLost("lost the RatCam: {}".format(cam.error))
		try:
			packet, host_ns = cam.data.get(timeout=max(0.0, deadline - time.monotonic()))
		except queue.Empty:
			raise RatCamError("no frame within {:g} ms".format(GRAB_TIMEOUT_S * 1000))
		if packet.type == protocol.FRAME:
			camera.logs.Frame(packet.seq, packet.t_us, host_ns)
			return packet
		Keep(camera, packet, host_ns)


def GetImageArray(grabResult):

	return grabResult.image()


def GetTimeStamp(grabResult):

	return grabResult.t_us * 1e-6


def DisplayImage(cam_params, dispQueue, grabResult):
	# Mono: the display shows it as gray
	step = cam_params["displayDownsample"]
	dispQueue.append(grabResult.image()[::step, ::step])


def ReleaseFrame(grabResult):

	del grabResult


def CloseQuietly(camera):
	for command in ("STOP", "SYNCGEN 0", "LED 0"):
		try:
			camera.cam.command(command)
		except Exception:
			pass
	try:
		camera.cam.close()
	except Exception:
		pass


def CloseCamera(cam_params, camera):
	Say("Closing {}... Please wait.".format(cam_params["cameraName"]))
	try:
		try:
			camera.cam.command("STOP")
		except Exception:
			pass
		time.sleep(0.05)
		Drain(camera) # sync pulses that came after the last frame
	finally:
		CloseQuietly(camera) # the IR LEDs go off with it
	try:
		SaveLogs(cam_params, camera)
	except Exception as e:
		Say("{}: could not save ratcam_frames.csv and ratcam_sync.csv: {}".format(cam_params["cameraName"], e))


def SaveLogs(cam_params, camera):
	# The last rows, and the totals for metadata.csv
	logs = camera.logs
	logs.Close()
	lost = logs.lost
	cam_params["ratcamFramesLost"] = lost
	cam_params["ratcamSyncPulses"] = logs.sync.count
	if lost:
		Say("{}: the camera's frame counter skipped {} frames: they were lost before reaching campy "
			"(USB too slow for this frame size and rate?).".format(cam_params["cameraName"], lost))


def CloseSystem(system, device_list):
	pass
