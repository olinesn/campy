"""
campy-flir-reset: reset FLIR USB3 cameras that a computer's restart left at
their USB 2 throughput limit.

	campy-flir-reset

A Blackfly S sets its USB throughput limit (DeviceLinkThroughputLimit) to 43
MB/s whenever it connects at USB 2 speed (FLIR's release notes for firmware
1808, item F-300). A computer's restart leaves its USB ports powered, so a
camera left plugged in keeps running, and the computer's startup connects it
at USB 2 and then moves it to USB 3: the camera keeps the USB 2 limit until
it is reset or unplugged. Every program then sees 43 MB/s, which holds a
1280x1024 Mono8 camera to 32.8 fps instead of 170, although all of its saved
settings say 380. campy raises such a limit itself (cameraLinkLimitMBps), but
SpinView and other programs do not. Shutting down and switching on did not
leave it on the computer where it was measured, which cut the ports' power.

This checks every FLIR camera and resets each one that is on a USB 3 link
with a limit of at most USB 2's 480 Mbit/s (60 MB/s). A reset (DeviceReset) is
the same as unplugging the camera: it restarts with its saved settings, limit
included. A camera that saved a limit that low is reset too, and comes back
with it; that costs a few seconds and changes nothing. Cameras on a USB 2 link,
and models without the setting, are left alone.

Run it once after the computer starts, before anything opens the cameras: on
Linux, from a systemd service (see docs/flir.md). Exits 1 if a camera did not
come back on USB 3 after its reset.

	campy-flir-reset --gige off|on

turns Spinnaker's search for GigE cameras off or on (for the user who runs
it; on Windows, for the whole computer), and touches no camera. Each time a program starts Spinnaker
(System.GetInstance), Spinnaker broadcasts for GigE Vision cameras on every
network interface and waits about 0.25 s on each, one after another: 0.95 s
on the EVO-X2, with three, in campy-acquire and again in each camera process.
A computer whose FLIR cameras are all USB3 spends that on cameras it does not
have; with it off, campy-acquire was ready to trigger in 0.99 s instead of
2.86. It is Spinnaker's own setting (EnumerateGEVInterfaces), which Spinnaker
4.0 and newer keep: on Linux for the user, in
~/.config/spinnaker/GenTLSysReg.ini, and on Windows for every user of the
computer, in C:\\ProgramData\\Spinnaker\\Shared\\GenTLSysReg.ini. So while it is
off, no program that user runs (on Windows, no program on the computer) finds
a GigE camera, SpinView included.
"""

import argparse, getpass, os, sys, time
import PySpin

from campy.utils.utils import Say

USB2_MAX = 60_000_000 # bytes/s: USB 2's 480 Mbit/s. No limit a camera sets for USB 2 is above it.
WAIT_S = 30           # for cameras to show up after the computer starts, and after a reset
SETTLE_S = 2          # after the resets, before looking for the cameras again
FLIR_VENDORS = ("1e10", "1724") # USB vendor IDs: Point Grey / FLIR, and FLIR's newer one
GIGE_NODE = "EnumerateGEVInterfaces" # on the System's node map: Spinnaker 3.2 and newer


def ConnectedFlirDevices(root="/sys/bus/usb/devices"):
	# How many FLIR USB devices the (Linux) kernel has connected; 0 elsewhere
	count = 0
	try:
		devices = os.listdir(root)
	except OSError:
		return 0
	for dev in devices:
		try:
			with open(os.path.join(root, dev, "idVendor")) as f:
				count += f.read().strip() in FLIR_VENDORS
		except OSError:
			pass
	return count


def Read(nodemap, name, kind):
	# The named node's value, or None where the camera has no such node or cannot read it
	node = nodemap.GetNode(name)
	if node is None:
		return None
	node = kind(node)
	if not PySpin.IsAvailable(node) or not PySpin.IsReadable(node):
		return None
	if kind is PySpin.CEnumerationPtr:
		return node.GetCurrentEntry().GetSymbolic()
	return node.GetValue()


def Link(cam):
	# (serial number, link speed, throughput limit) of a camera; the limit is
	# read only on a USB 3 link, and is None where the camera has none
	tl = cam.GetTLDeviceNodeMap()
	serial = Read(tl, "DeviceSerialNumber", PySpin.CStringPtr)
	speed = Read(tl, "DeviceCurrentSpeed", PySpin.CEnumerationPtr) or "unknown"
	if not speed.startswith("SuperSpeed"):
		return serial, speed, None
	cam.Init()
	try:
		limit = Read(cam.GetNodeMap(), "DeviceLinkThroughputLimit", PySpin.CIntegerPtr)
	finally:
		cam.DeInit()
	return serial, speed, limit


def Cameras(system, want):
	# The camera list, once it has at least `want` cameras or WAIT_S has passed
	deadline = time.time() + WAIT_S
	while True:
		system.UpdateCameras()
		cams = system.GetCameras()
		if cams.GetSize() >= want or time.time() > deadline:
			return cams
		cams.Clear()
		time.sleep(1)


def Reset(cam):
	cam.Init()
	PySpin.CCommandPtr(cam.GetNodeMap().GetNode("DeviceReset")).Execute()
	try:
		cam.DeInit()
	except PySpin.SpinnakerException:
		pass # the camera has already gone


def WaitBack(system, serial):
	# (link speed, throughput limit) of a camera once it is back from its
	# reset, or (None, None) if it is not back within WAIT_S
	deadline = time.time() + WAIT_S
	while time.time() < deadline:
		system.UpdateCameras()
		cams = system.GetCameras()
		cam = cams.GetBySerial(serial)
		try:
			if cam.IsValid():
				return Link(cam)[1:]
		except PySpin.SpinnakerException:
			pass # still starting up
		finally:
			del cam
			cams.Clear()
		time.sleep(1)
	return None, None


def MB(limit):
	return "{:.0f} MB/s".format(limit / 1e6)


def Run(system):
	connected = ConnectedFlirDevices()
	cams = Cameras(system, connected)
	found = cams.GetSize()
	if found < connected:
		Say("{} FLIR USB device(s) connected, but Spinnaker sees {}: is the udev rule that gives "
			"them to group flirimaging in place?".format(connected, found))
	if found == 0:
		Say("No FLIR cameras.")
		cams.Clear()
		return 0
	stuck = {}
	for i in range(found):
		cam = cams[i]
		try:
			serial, speed, limit = Link(cam)
			if limit is None:
				Say("{}: {} link, left alone.".format(serial, speed))
			elif limit > USB2_MAX:
				Say("{}: {} link, throughput limit {}.".format(serial, speed, MB(limit)))
			else:
				Say("{}: {} link, but throughput limit {}, as on USB 2: resetting it.".format(serial, speed, MB(limit)))
				Reset(cam)
				stuck[serial] = limit
		except PySpin.SpinnakerException as e:
			Say("Camera {}: {}".format(i, e))
		del cam
	cams.Clear()
	if not stuck:
		return 0

	time.sleep(SETTLE_S) # until the reset cameras have left the list
	failed = 0
	for serial, before in stuck.items():
		speed, limit = WaitBack(system, serial)
		if speed is None:
			Say("{}: not back {} s after its reset.".format(serial, WAIT_S))
			failed += 1
		elif limit is None:
			Say("{}: back on a {} link.".format(serial, speed))
			failed += 1
		elif limit == before:
			Say("{}: back on a {} link, throughput limit {}: the limit it saved.".format(serial, speed, MB(limit)))
		else:
			Say("{}: back on a {} link, throughput limit {}.".format(serial, speed, MB(limit)))
	return 1 if failed else 0


def GigeScope(user):
	# Whom the GigE search setting is kept for, and who then finds no GigE
	# camera: Spinnaker keeps it for the user on Linux, and on Windows for the
	# whole computer (C:\ProgramData\Spinnaker\Shared\GenTLSysReg.ini)
	if sys.platform == "win32":
		return "for every user of this computer", "no program on this computer"
	return "for user {}".format(user), "no program this user runs"


def SetGige(system, on):
	# Turns Spinnaker's search for GigE cameras on or off, for this user (on
	# Windows, this computer): the System's node map holds it, and Spinnaker
	# 4.0 and newer keep it
	scope, nobody = GigeScope(getpass.getuser())
	node = system.GetTLNodeMap().GetNode(GIGE_NODE)
	if node is None:
		Say("This Spinnaker has no {} setting: 3.2 and newer have it.".format(GIGE_NODE))
		return 1
	node = PySpin.CBooleanPtr(node)
	if PySpin.IsReadable(node) and node.GetValue() == on:
		Say("Spinnaker's search for GigE cameras was already {} {}.".format("on" if on else "off", scope))
		return 0
	if not PySpin.IsReadable(node) or not PySpin.IsWritable(node):
		Say("Spinnaker's {} setting cannot be changed here.".format(GIGE_NODE))
		return 1
	node.SetValue(on)
	if node.GetValue() != on:
		Say("Spinnaker did not take {} = {}.".format(GIGE_NODE, on))
		return 1
	if on:
		Say("Spinnaker's search for GigE cameras is on again {}: programs find GigE cameras, and "
			"Spinnaker takes about 0.25 s per network interface to start.".format(scope))
	else:
		Say("Spinnaker's search for GigE cameras is off {}: Spinnaker starts without waiting on each "
			"network interface, and {} finds a GigE camera, SpinView included, until "
			"campy-flir-reset --gige on.".format(scope, nobody))
	return 0


def Main(argv=None):
	parser = argparse.ArgumentParser(prog="campy-flir-reset",
		description="Reset FLIR USB3 cameras that a computer's restart left at their USB 2 throughput limit (43 MB/s).")
	parser.add_argument("--gige", choices=("off", "on"),
		help="instead, turn Spinnaker's search for GigE cameras off or on for this user, or on Windows for the "
		"whole computer (it costs about 0.25 s per network interface each time a program starts Spinnaker), "
		"and touch no camera")
	args = parser.parse_args(argv)
	system = PySpin.System.GetInstance()
	try:
		if args.gige:
			return SetGige(system, args.gige == "on")
		return Run(system)
	finally:
		system.ReleaseInstance()


if __name__ == "__main__":
	sys.exit(Main())
