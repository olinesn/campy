"""
Launch check: does this configuration fit this computer?

Runs once, in the parent process, before any camera starts. When a rig is
scaled up (more cameras, a higher frame rate, larger frames), an overloaded
USB bus or encoder does not stop the recording: it loses frames, and nothing
says so until the views are out of step. Each finding is one line:

  OK    fits, with the margin
  WARN  could not be checked, or is close to its limit
  FAIL  will not fit

preflight "error" (the default) stops campy before recording when a check
FAILs; "warn" reports and records anyway; "off" skips the check.

  encoders  the ffmpeg binary has each camera's encoder, a VA-API device can
            be opened, and NVENC encodes a few test frames with campy's settings
  capacity  cameras per encoder, against the table campy-encode-sweep measured
            on this computer (capacityFile)
  usb       camera data per USB bus, against a budget (Linux: read from sysfs)
  disk      free space for the recording, when the sweep measured a bitrate
  camera    each camera can run at frameRate with campy's settings (FLIR:
            each is opened, set up as a recording sets it up, and read)
"""

import os, re, sys, json, shlex, shutil, socket, tempfile, subprocess
from collections import OrderedDict

from campy import writer, configurator
from campy.cameras import ratcam, unicam


class PreflightError(Exception):
	pass


OK, WARN, FAIL = "OK", "WARN", "FAIL"

# A load above this share of its limit is reported as a WARN
WARN_FRACTION = 0.8

# USB vendor IDs of the camera makes campy drives
USB_VENDOR_IDS = {
	"flir": ("1e10",), # FLIR / Point Grey
	"basler": ("2676",),
	"ratcam": ("2e8a", "303a"), # nir-eye-cam: RP2040 (Raspberry Pi's ID), ESP32-P4 (Espressif's)
}

# Makes that need a USB 3 link. A RatCam is a USB 2 device: the RP2040 build
# runs at full speed (12 Mb/s), the ESP32-P4 build at high speed (480 Mb/s).
USB3_MAKES = ("flir", "basler")

# Camera data one USB bus may carry, in MB/s, by the bus speed in Mb/s: about
# 75% of what a bus moves in practice, so a camera's bursts have room. VERIFY
# on each rig by streaming every camera at once at the full rate (SpinView,
# pylon Viewer). usbBudgetMBps replaces the SuperSpeed values.
USB_BUDGET_MBPS = OrderedDict([
	(12, 1),
	(480, 35),
	(5000, 300),
	(10000, 600),
	(20000, 1200),
	])

# Bytes per pixel of the frames a camera sends, by its pixelFormatInput
BYTES_PER_PIXEL = {
	"gray": 1,
	"gray10le": 2, "gray12le": 2, "gray16le": 2, "gray16be": 2,
	"rgb24": 3, "bgr24": 3,
	"rgb0": 4, "bgr0": 4, "0rgb": 4, "0bgr": 4,
	"rgba": 4, "bgra": 4, "argb": 4, "abgr": 4,
	"yuv420p": 1.5, "nv12": 1.5,
	"yuyv422": 2, "uyvy422": 2,
	}


def BytesPerPixel(pix_fmt):
	if pix_fmt in BYTES_PER_PIXEL:
		return BYTES_PER_PIXEL[pix_fmt]
	if pix_fmt.startswith("bayer_"):
		return 2 if pix_fmt.endswith(("16le", "16be")) else 1
	return None


def CameraBytesPerSec(cam):
	# Camera data rate: what crosses the USB bus, and what the encoder reads
	bpp = BytesPerPixel(str(cam["pixelFormatInput"]))
	if bpp is None:
		return None
	return cam["frameWidth"] * cam["frameHeight"] * bpp * float(cam["frameRate"])


def PixelRate(cam):
	return cam["frameWidth"] * cam["frameHeight"] * float(cam["frameRate"])


def Names(cams):
	return ", ".join(cam["cameraName"] for cam in cams)


# ------------------------------------------------------------------ report

class Report:
	def __init__(self):
		self.lines = []

	def add(self, status, check, text):
		self.lines.append((status, check, text))

	def count(self, status):
		return sum(1 for line in self.lines if line[0] == status)

	def text(self, header):
		out = [header]
		for status, check, text in self.lines:
			# A finding's further lines (ffmpeg's own output) under its text
			text = text.replace("\n", "\n" + " " * 18)
			out.append("  {:<4}  {:<8}  {}".format(status, check, text))
		return "\n".join(out)


# ------------------------------------------------------------------ cameras

def CameraList(systems, params):
	# Each camera's parameters, as its process will see them, plus the serial
	# number of the device it will open
	cams = []
	for n in range(params["numCams"]):
		cam = configurator.CameraParams(params, n)
		if cam["cameraMake"] == "ratcam":
			cam = ratcam.FixedParams(cam)
		serials = systems.get(cam["cameraMake"], {}).get("serials", [])
		sel = cam["cameraSelection"]
		ok = isinstance(sel, int) and 0 <= sel < len(serials)
		cam["cameraSerialNo"] = serials[sel] if ok else None
		cams.append(cam)
	return cams


# ------------------------------------------------------------------ encoders

def FfmpegExe():
	# The binary campy records with: imageio-ffmpeg's, or ffmpegPath
	import imageio_ffmpeg
	return imageio_ffmpeg.get_ffmpeg_exe()


def FfmpegEncoders(exe):
	# Encoder names this ffmpeg was built with; None if it would not run
	try:
		out = subprocess.run([exe, "-hide_banner", "-encoders"], capture_output=True,
			text=True, timeout=30).stdout
	except Exception:
		return None
	names = set()
	for line in out.splitlines():
		m = re.match(r"^\s*[VAS][\w.]{5}\s+(\S+)", line)
		if m:
			names.add(m.group(1))
	return names


def CheckEncoders(cams, report, exe=None, encoders=None):
	exe = exe or FfmpegExe()
	if encoders is None:
		encoders = FfmpegEncoders(exe)
	if encoders is None:
		report.add(FAIL, "encoders", "could not run ffmpeg at {}. Set ffmpegPath.".format(exe))
		return
	by_codec = OrderedDict()
	for cam in cams:
		by_codec.setdefault(writer.EncoderArgs(cam)["codec"], []).append(cam)
	for codec, group in by_codec.items():
		if codec in encoders:
			report.add(OK, "encoders", "{} for {}".format(codec, Names(group)))
		else:
			report.add(FAIL, "encoders", "{} has no {} encoder ({}). Set ffmpegPath to an ffmpeg "
				"built with it, e.g. Ubuntu's /usr/bin/ffmpeg.".format(exe, codec, Names(group)))

	devices = OrderedDict()
	for cam in cams:
		if cam["gpuID"] != -1 and cam["gpuMake"] == "vaapi":
			devices.setdefault(str(cam["vaapiDevice"]), []).append(cam)
	for device, group in devices.items():
		if not os.path.exists(device):
			report.add(FAIL, "encoders", "VA-API device {} does not exist ({}). Set vaapiDevice; "
				"ls /dev/dri lists the render nodes.".format(device, Names(group)))
		elif not os.access(device, os.R_OK | os.W_OK):
			report.add(FAIL, "encoders", "cannot open VA-API device {} ({}): add this user to the "
				"device's group (usually 'render') and log in again.".format(device, Names(group)))


# ------------------------------------------------------------------ NVENC

# Test frames per NVENC probe, and how long one may take
NVENC_PROBE_FRAMES = 10
NVENC_PROBE_TIMEOUT = 60
# Lines of ffmpeg's error output a failed probe quotes (its last ones)
NVENC_PROBE_LINES = 8

# What a failed NVENC probe means, by a phrase in ffmpeg's error output
NVENC_HINTS = [
	("Cannot get the preset configuration",
		"ffmpeg 4.3 and older ask the driver for NVENC presets with a call that NVIDIA drivers "
		"from 591.44 (December 2025) reject. Set ffmpegPath to an ffmpeg 4.4 or newer that is "
		"built for an NVENC version this driver supports (README: NVIDIA GPU encoding)."),
	("Driver does not support the required nvenc API version",
		"This ffmpeg is built for a newer NVENC version than the driver supports (Required, "
		"Found). Update the NVIDIA driver to the version ffmpeg names, or set ffmpegPath to an "
		"ffmpeg built for this driver's NVENC version, e.g. BtbN's 8.1 build (NVENC 13.0, driver "
		"570 or newer)."),
	("Cannot load",
		"ffmpeg could not load the NVIDIA driver's libraries. Is the NVIDIA driver installed? "
		"nvidia-smi lists the GPUs it drives."),
	("Requested GPU",
		"No GPU has this gpuID. nvidia-smi lists the GPUs, numbered from 0."),
	("capable devices found",
		"No NVIDIA GPU here encodes this codec at this frame size and pixel format. NVIDIA's "
		"support matrix lists each GPU's encoders: "
		"https://developer.nvidia.com/video-encode-decode-gpu-support-matrix"),
	]


def EncodedSize(cam, enc):
	# imageio-ffmpeg scales a frame up to the next multiple of macro_block_size
	width, height = cam["frameWidth"], cam["frameHeight"]
	block = enc["macro_block_size"]
	if block > 1:
		width += -width % block
		height += -height % block
	return width, height


def NvencProbeCommand(exe, cam):
	"""
	The ffmpeg command that encodes a few test frames (lavfi's testsrc2) as
	campy's writer encodes this camera: the same codec, pixel format, input
	and output options (preset, qp, -gpu, ...), at the size and rate the
	encoder gets. It writes nvenc_test.<the video's extension> in the working
	directory.
	"""
	enc = writer.EncoderArgs(cam)
	width, height = EncodedSize(cam, enc)
	source = "testsrc2=size={}x{}:rate={:.02f}".format(width, height, float(cam["frameRate"]))
	extension = os.path.splitext(str(cam["videoFilename"]))[1] or ".mp4"
	return ([exe, "-hide_banner", "-nostdin", "-v", "error"] + enc["input_params"]
		+ ["-f", "lavfi", "-i", source, "-frames:v", str(NVENC_PROBE_FRAMES)]
		+ ["-vcodec", enc["codec"], "-pix_fmt", enc["pix_fmt_out"]] + enc["output_params"]
		+ ["-y", "nvenc_test" + extension])


def CommandText(cmd):
	return subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)


def RunProbe(cmd, folder):
	# ffmpeg's exit code and error output. The code is None when ffmpeg would
	# not start or did not finish.
	try:
		done = subprocess.run(cmd, cwd=folder, capture_output=True, encoding="utf-8",
			errors="replace", timeout=NVENC_PROBE_TIMEOUT)
	except subprocess.TimeoutExpired:
		return None, "(did not finish in {} s)".format(NVENC_PROBE_TIMEOUT)
	except OSError as e:
		return None, str(e)
	return done.returncode, done.stderr or ""


def NvencHint(said):
	for phrase, hint in NVENC_HINTS:
		if phrase in said:
			return hint
	return "Run the command below with -v info instead of -v error for ffmpeg's whole output."


def CheckNvenc(cams, report, exe=None, encoders=None):
	"""
	Encodes a few test frames with each NVENC camera's settings, before any
	camera starts. Without it, an ffmpeg that cannot use NVENC on this driver
	stops every camera's writer with a BrokenPipeError and nothing from ffmpeg
	to say why (ffmpegLogLevel is "quiet" by default). Cameras with the same
	settings share one probe.
	"""
	nvenc = [cam for cam in cams if cam["gpuID"] != -1 and cam["gpuMake"] == "nvidia"]
	if not nvenc:
		return
	exe = exe or FfmpegExe()
	if encoders is None:
		encoders = FfmpegEncoders(exe)
	if encoders is None:
		return # ffmpeg would not run: CheckEncoders FAILs that

	probes = OrderedDict()
	for cam in nvenc:
		if writer.EncoderArgs(cam)["codec"] in encoders: # else CheckEncoders FAILs it
			probes.setdefault(tuple(NvencProbeCommand(exe, cam)), []).append(cam)
	if not probes:
		return

	with tempfile.TemporaryDirectory(prefix="campy-nvenc-") as folder:
		for cmd, group in probes.items():
			code, said = RunProbe(list(cmd), folder)
			where = "{} on GPU {} ({})".format(writer.EncoderArgs(group[0])["codec"],
				group[0]["gpuID"], Names(group))
			if code == 0:
				report.add(OK, "encoders", "{} encoded {} test frames with campy's settings"\
					.format(where, NVENC_PROBE_FRAMES))
				continue
			quoted = [line.strip() for line in said.splitlines() if line.strip()][-NVENC_PROBE_LINES:]
			if not quoted:
				quoted = ["(nothing; exit code {})".format(code)]
			report.add(FAIL, "encoders", "\n".join(
				["{} could not encode test frames with campy's settings. ffmpeg said:".format(where)]
				+ ["  " + line for line in quoted]
				+ [NvencHint(said), "To run it by hand: " + CommandText(list(cmd))]))


# ------------------------------------------------------------------ capacity

def CapacityPath(params):
	path = str(params.get("capacityFile", "None"))
	if path == "None":
		path = os.path.join(os.path.expanduser("~"), ".campy", "capacity.json")
	return path


def LoadCapacity(path):
	try:
		with open(path) as f:
			return json.load(f)
	except (OSError, ValueError):
		return None


def Shape(cam):
	return (cam["frameWidth"], cam["frameHeight"], float(cam["frameRate"]), str(cam["pixelFormatInput"]))


def EntryShape(entry):
	return (entry["width"], entry["height"], float(entry["fps"]), entry["pix_fmt_in"])


def FindEntry(table, cam):
	# The measurement for this camera's encoder and frame shape, if there is one
	if not table:
		return None
	key = writer.EncoderArgs(cam)["key"]
	for entry in table.get("entries", []):
		if entry["key"] == key and EntryShape(entry) == Shape(cam):
			return entry
	return None


def CheckCapacity(cams, table, report, path):
	if table is None:
		report.add(WARN, "capacity", "no encoder capacity table at {}. Run campy-encode-sweep "
			"on this computer to measure how many cameras each encoder keeps up with.".format(path))
		return
	measured_on = table.get("host", {}).get("hostname")
	if measured_on and measured_on != socket.gethostname():
		report.add(WARN, "capacity", "{} was measured on {}, not on this computer ({})."\
			.format(path, measured_on, socket.gethostname()))

	groups = OrderedDict()
	for cam in cams:
		groups.setdefault(writer.EncoderArgs(cam)["key"], []).append(cam)

	for key, group in groups.items():
		entries = [e for e in table.get("entries", []) if e["key"] == key]
		if not entries:
			report.add(WARN, "capacity", "{} was not measured ({}). Run campy-encode-sweep with "
				"this encoder.".format(key, Names(group)))
			continue

		n = len(group)
		shapes = set(Shape(cam) for cam in group)
		exact = [e for e in entries if len(shapes) == 1 and EntryShape(e) == next(iter(shapes))]
		if exact:
			entry = exact[0]
			cap, tested = entry["max_streams"], entry["max_tested"]
			w, h, fps, _ = EntryShape(entry)
			measured = "{}{}".format(cap, "+" if cap >= tested else "")
			text = "{}: {} camera{} at {}x{} {:g} fps; measured to keep up with {} (x{:g} headroom)"\
				.format(key, n, "s" if n > 1 else "", w, h, fps, measured, entry["headroom"])
			if n > cap:
				report.add(FAIL, "capacity", text + ". Move cameras to another encoder or lower the load.")
			else:
				report.add(OK, "capacity", text)
		else:
			# Another frame size or rate than measured: compare pixel rates, from
			# the measurement closest in pixel rate per stream
			need = sum(PixelRate(cam) for cam in group) / 1e6
			per_stream = need / n
			entry = min(entries, key=lambda e: abs(e["width"] * e["height"] * e["fps"] / 1e6 - per_stream))
			have = entry["max_mpx_s"]
			text = "{}: {} cameras need {:.0f} Mpx/s; the {}x{} {:g} fps measurement kept up with "\
				"{:.0f} Mpx/s (estimate: a different frame size or rate)"\
				.format(key, n, need, entry["width"], entry["height"], entry["fps"], have)
			if need > have:
				report.add(FAIL, "capacity", text)
			elif need > WARN_FRACTION * have:
				report.add(WARN, "capacity", text)
			else:
				report.add(OK, "capacity", text)

	if len(groups) > 1:
		report.add(WARN, "capacity", "encoders were measured one at a time, and {} share this "
			"computer's CPU (VA-API converts every frame on the CPU too). Check the mix with a test "
			"recording.".format(" and ".join(groups)))


# ------------------------------------------------------------------ USB

def ReadAttr(path, name):
	try:
		with open(os.path.join(path, name)) as f:
			return f.read().strip()
	except OSError:
		return None


def ListUsbDevices(sysfs_root="/sys"):
	"""
	Every USB device, as a dict: name ("4-1.2"), vendor, product, serial,
	speed (Mb/s of its own link), bus (root hub number), bus_speed, and
	controller (the host controller's PCI address, e.g. "0000:c4:00.3"). One
	controller usually has two buses, a USB 2 one and a USB 3 one, which carry
	data separately.
	"""
	base = os.path.join(sysfs_root, "bus", "usb", "devices")
	devices, bus_speeds = [], {}
	for name in sorted(os.listdir(base)):
		path = os.path.join(base, name)
		m = re.fullmatch(r"usb(\d+)", name)
		if m:
			bus_speeds[int(m.group(1))] = float(ReadAttr(path, "speed") or 0)
			continue
		if ":" in name: # an interface, not a device
			continue
		vendor = ReadAttr(path, "idVendor")
		if vendor is None:
			continue
		parts = os.path.realpath(path).split(os.sep)
		roots = [i for i, part in enumerate(parts) if re.fullmatch(r"usb\d+", part)]
		controller = parts[roots[-1] - 1] if roots and roots[-1] > 0 else "unknown"
		devices.append({
			"name": name,
			"vendor": vendor.lower(),
			"product": ReadAttr(path, "idProduct"),
			"serial": ReadAttr(path, "serial"),
			"speed": float(ReadAttr(path, "speed") or 0),
			"bus": int(ReadAttr(path, "busnum") or 0),
			"controller": controller,
			})
	for device in devices:
		device["bus_speed"] = bus_speeds.get(device["bus"], device["speed"])
	return devices


def SerialsMatch(camera_serial, usb_serial):
	a = str(camera_serial or "").strip().lower()
	b = str(usb_serial or "").strip().lower()
	if not a or not b:
		return False
	if a == b or a.lstrip("0") == b.lstrip("0"):
		return True
	# A camera may give its USB serial in hex. VERIFY with the rig's cameras.
	try:
		return int(a, 10) == int(b, 16)
	except ValueError:
		return False


def FindCameraDevice(devices, cam):
	vendors = USB_VENDOR_IDS.get(cam["cameraMake"], ())
	for device in devices:
		if device["vendor"] in vendors and SerialsMatch(cam.get("cameraSerialNo"), device["serial"]):
			return device
	return None


def UsbBudget(bus_speed, params):
	override = str(params.get("usbBudgetMBps", "None"))
	if override != "None" and bus_speed >= 5000:
		return float(override)
	budget = 0
	for speed, mbps in USB_BUDGET_MBPS.items():
		if bus_speed >= speed:
			budget = mbps
	return budget


def CheckUsb(cams, params, report, sysfs_root="/sys"):
	usb_cams = [cam for cam in cams if cam["cameraMake"] in USB_VENDOR_IDS]
	if not usb_cams:
		return
	base = os.path.join(sysfs_root, "bus", "usb", "devices")
	if not os.path.isdir(base):
		report.add(WARN, "usb", "USB bandwidth not checked: it is read from Linux's sysfs.")
		return

	devices = ListUsbDevices(sysfs_root)
	buses = OrderedDict()
	for cam in usb_cams:
		device = FindCameraDevice(devices, cam)
		if device is None:
			report.add(WARN, "usb", "{} (serial {}) is not on the USB bus: a GigE camera, or its USB "
				"serial is written differently.".format(cam["cameraName"], cam.get("cameraSerialNo")))
			continue
		rate = CameraBytesPerSec(cam)
		if rate is None:
			report.add(WARN, "usb", "{}: unknown bytes per pixel for pixelFormatInput {}; left out "
				"of its bus's total.".format(cam["cameraName"], cam["pixelFormatInput"]))
			rate = 0
		if cam["cameraMake"] in USB3_MAKES and device["speed"] < 5000:
			report.add(FAIL, "usb", "{} is connected at {:g} Mb/s, not USB 3, and needs {:.0f} MB/s. "
				"Check its cable and port.".format(cam["cameraName"], device["speed"], rate / 1e6))
		elif cam["cameraMake"] not in USB3_MAKES:
			CheckUsbLink(cam, device, rate, params, report)
		bus = buses.setdefault((device["controller"], device["bus"]),
			{"speed": device["bus_speed"], "cams": [], "rate": 0.0})
		bus["cams"].append(cam)
		bus["rate"] += rate

	for (controller, busnum), bus in buses.items():
		if bus["speed"] < 5000 and all(cam["cameraMake"] in USB3_MAKES for cam in bus["cams"]):
			continue # a USB 2 bus: each camera on it has already FAILed above
		budget = UsbBudget(bus["speed"], params)
		load = bus["rate"] / 1e6
		share = load / budget if budget else float("inf")
		text = "bus {} ({:g} Gb/s, controller {}): {} send {:.{p}f} of {:.0f} MB/s ({:.0%})".format(
			busnum, bus["speed"] / 1000, controller, Names(bus["cams"]), load, budget, share,
			p=0 if load >= 10 else 1)
		if share > 1:
			report.add(FAIL, "usb", text + ". Move cameras to another controller.")
		elif share > WARN_FRACTION:
			report.add(WARN, "usb", text)
		else:
			report.add(OK, "usb", text)


def CheckUsbLink(cam, device, rate, params, report):
	# A USB 2 camera's own link (a RatCam): full speed carries about 1 MB/s of
	# frames, whatever bus it is on
	budget = UsbBudget(device["speed"], params)
	need = rate / 1e6
	share = need / budget if budget else float("inf")
	text = "{} is connected at {:g} Mb/s, which carries about {:g} MB/s; it needs {:.2f} MB/s ({:.0%})"\
		.format(cam["cameraName"], device["speed"], budget, need, share)
	if share > 1:
		report.add(FAIL, "usb", text + ". Lower its frame rate or frame size" + (
			", or use the ESP32-P4 build (USB high speed)." if device["speed"] <= 12 else "."))
	elif share > WARN_FRACTION:
		report.add(WARN, "usb", text)
	else:
		report.add(OK, "usb", text)


# ------------------------------------------------------------------ disk

def CheckDisk(cams, params, table, report):
	folder = os.path.abspath(params["videoFolder"])
	probe = folder
	while not os.path.exists(probe) and os.path.dirname(probe) != probe:
		probe = os.path.dirname(probe)
	free = shutil.disk_usage(probe).free

	rates = [FindEntry(table, cam) for cam in cams]
	known = [entry["bitrate_mbps"] for entry in rates if entry and entry.get("bitrate_mbps")]
	if len(known) < len(cams):
		report.add(OK, "disk", "{:.0f} GB free at {}; recording size not estimated (no measured "
			"bitrate for every camera).".format(free / 1e9, folder))
		return
	need = sum(known) * 1e6 / 8 * float(params["recTimeInSec"])
	text = "{:.0f} GB free at {}; {:.0f} s from {} cameras is about {:.1f} GB at the sweep's "\
		"bitrates".format(free / 1e9, folder, float(params["recTimeInSec"]), len(cams), need / 1e9)
	if need > free:
		report.add(FAIL, "disk", text)
	elif need > WARN_FRACTION * free:
		report.add(WARN, "disk", text)
	else:
		report.add(OK, "disk", text)


# ------------------------------------------------------------------ run

# ------------------------------------------------------------------ frame rate

def CameraLimits(systems, cam):
	# What limits this camera's frame rate (flir.FrameRateLimits), or None for a
	# make that cannot say. Opens the camera briefly, before its own process does.
	make = cam["cameraMake"]
	if "deviceList" not in systems.get(make, {}) or cam["cameraSerialNo"] is None:
		return None
	module = unicam.ImportCam(make)
	if not hasattr(module, "FrameRateLimits"):
		return None
	return module.FrameRateLimits(systems[make]["deviceList"][cam["cameraSelection"]], cam)


def FrameRateReason(cam, limits, rate):
	# The tightest of the limits the camera reported, in words
	frame, link = limits.get("frameBytes"), limits.get("throughputLimit")
	exposure, cap = limits.get("exposureUs"), limits.get("frameRateCap")
	causes = []
	if frame and link and link / frame < rate:
		causes.append((link / frame, "its USB link is limited to {:.0f} MB/s (DeviceLinkThroughputLimit), "
			"and {:.1f} MB frames at {:g} fps need {:.0f} MB/s. Unplugging and replugging the camera "
			"resets the limit to its saved settings, or raise it in SpinView".format(
			link / 1e6, frame / 1e6, rate, frame * rate / 1e6)))
	if exposure and 1e6 / exposure < rate:
		causes.append((1e6 / exposure, "its exposure, {:.0f} us (cameraExposureTimeInUs), is longer "
			"than a frame at {:g} fps ({:.0f} us)".format(exposure, rate, 1e6 / rate)))
	if cap and cap < rate:
		causes.append((cap, "AcquisitionFrameRate caps it at {:.1f} fps "
			"(AcquisitionFrameRateEnable is on)".format(cap)))
	if not causes:
		return "its sensor reads out {}x{} frames no faster".format(cam["frameWidth"], cam["frameHeight"])
	return min(causes)[1]


def FrameRateFinding(cam, limits):
	# (status, text) for one camera
	rate = float(cam["frameRate"])
	fps = limits.get("fps")
	who = "{} ({})".format(cam["cameraName"], cam["cameraSerialNo"])
	if not fps:
		return WARN, "{}: could not read the highest frame rate it can run at".format(who)
	if fps < rate:
		return FAIL, "{} can run at only {:.1f} fps with campy's settings, below frameRate {:g}: {}.".format(
			who, fps, rate, FrameRateReason(cam, limits, rate))
	close = rate > WARN_FRACTION * fps
	return (WARN if close else OK), "{}: up to {:.1f} fps with campy's settings; frameRate {:g}{}".format(
		who, fps, rate, " is close to that" if close else "")


def CheckFrameRate(cams, report, limits):
	"""
	Each camera can run at frameRate with campy's settings. One that cannot
	does not stop: free-running, it records more slowly than its video says;
	triggered, it misses triggers. limits(cam) returns what limits a camera
	(CameraLimits), or None to skip it.
	"""
	for cam in cams:
		try:
			found = limits(cam)
		except Exception as e:
			report.add(WARN, "camera", "{}: could not read its frame rate limits ({})".format(cam["cameraName"], e))
			continue
		if found is not None:
			status, text = FrameRateFinding(cam, found)
			report.add(status, "camera", text)


def Run(systems, params, sysfs_root="/sys"):
	mode = str(params.get("preflight", "error")).lower()
	if mode == "off":
		return None

	cams = CameraList(systems, params)
	path = CapacityPath(params)
	table = LoadCapacity(path)

	report = Report()
	CheckEncoders(cams, report)
	CheckNvenc(cams, report)
	CheckCapacity(cams, table, report, path)
	CheckUsb(cams, params, report, sysfs_root)
	CheckFrameRate(cams, report, lambda cam: CameraLimits(systems, cam))
	CheckDisk(cams, params, table, report)

	print(report.text("Launch check ({} cameras on {}):".format(len(cams), socket.gethostname())), flush=True)
	failed = report.count(FAIL)
	if failed and mode == "error":
		raise PreflightError("Stopped before recording: {} check{} failed. Fix {}, or set preflight: "
			"\"warn\" to record anyway.".format(failed, "s" if failed > 1 else "",
			"them" if failed > 1 else "it"))
	return report
