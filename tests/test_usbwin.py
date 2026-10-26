"""
Each USB device's link speed on Windows (campy/usbwin.py), on a made-up
device tree shaped as the Dell Pro Max Slim FCS1250's: FLIR cameras on the
chipset controller's root hub, the trigger Arduino and a keyboard beside
them. The launch check and the recording watch read it too
(tests/test_preflight.py, tests/test_watch.py use FakeWindows). The real
calls run only on Windows.
"""

import sys

import pytest

from campy import preflight, usbwin

CONTROLLER = r"PCI\VEN_8086&DEV_7F6E&SUBSYS_0CFD1028&REV_10\3&36CB97A3&0&A0"
ROOT = r"USB\ROOT_HUB30\4&361F12FD&0&0"
OTHER = r"PCI\VEN_1022&DEV_15B6&SUBSYS_0CFD1028&REV_00\4&1A2B3C4D&0&0341"
OTHER_ROOT = r"USB\ROOT_HUB30\5&2C3D4E5F&0&0"
HS, SS, SS_PLUS = (2, 0), (2, 3), (2, 7) # what the hub says: _EX's Speed byte, _EX_V2's flags


def camera(serial):
	return r"USB\VID_1E10&PID_4000" + "\\" + serial


class FakeWindows:
	"""
	A device tree: devices maps an instance ID to (its hub, its port, what
	the hub says of that port: (speed byte, flags), or an OSError). Hubs and
	root hubs are devices too; a root hub's hub is its controller. Link()
	answers from it as the hub driver would; set gone to make DeviceIds fail.
	"""

	def __init__(self, devices, gone=None):
		self.devices, self.gone = dict(devices), gone
		self.asked = []

	def DeviceIds(self):
		if self.gone:
			raise OSError(self.gone)
		return list(self.devices)

	def Parent(self, iid):
		return self.devices[iid][0]

	def Port(self, iid):
		return self.devices[iid][1]

	def Link(self, hub, port):
		self.asked.append((hub, port))
		for iid, (h, p, link) in self.devices.items():
			if h == hub and p == port:
				if isinstance(link, Exception):
					raise link
				return link
		raise OSError("Asking hub {} about port {}: Windows error 87".format(hub, port))

	def Plug(self, iid, hub, port, link):
		self.devices[iid] = (hub, port, link)


def slim(**links):
	# The Pro Max Slim's tree; links: serial -> (speed byte, flags), to change a camera's
	devices = {
		ROOT: (CONTROLLER, 0, None),
		camera("01524401"): (ROOT, 24, links.get("01524401", SS)), # 22168577
		camera("0171ED30"): (ROOT, 20, links.get("0171ED30", SS)), # 24243504
		r"USB\VID_2341&PID_0070\ECDA3B60A290": (ROOT, 5, (1, 0)), # the trigger: full speed
		r"USB\VID_2341&PID_0070&MI_01\6&142CAE2E&0&0001": (r"USB\VID_2341&PID_0070\ECDA3B60A290", 6, None),
		r"USB\VID_413C&PID_2113\5&33176d27&0&3": (ROOT, 3, (0, 0)), # a keyboard: low speed, no serial
		}
	return FakeWindows(devices)


def test_link_speeds():
	assert usbwin.LinkSpeed(*SS) == 5000
	assert usbwin.LinkSpeed(*SS_PLUS) == 10000
	assert usbwin.LinkSpeed(*HS) == 480
	assert usbwin.LinkSpeed(1, 0) == 12
	assert usbwin.LinkSpeed(0, 0) == 1.5
	assert usbwin.LinkSpeed(3, None) == 5000 # Windows 7: no _EX_V2, but _EX says SuperSpeed
	assert usbwin.LinkSpeed(2, None) == 480
	assert usbwin.LinkSpeed(9, 0) is None


def test_list_usb_devices():
	devices = {d["name"]: d for d in usbwin.ListUsbDevices(slim())}
	# No root hubs, no interfaces (&MI_)
	assert set(devices) == {camera("01524401"), camera("0171ED30"), r"USB\VID_2341&PID_0070\ECDA3B60A290",
		r"USB\VID_413C&PID_2113\5&33176d27&0&3"}
	cam = devices[camera("01524401")]
	assert (cam["vendor"], cam["product"], cam["serial"]) == ("1e10", "4000", "01524401")
	assert (cam["speed"], cam["bus"], cam["bus_speed"], cam["bus_speed_known"]) == (5000, "usb3", 5000, False)
	assert (cam["controller"], cam["controller_name"]) == (CONTROLLER, "8086:7f6e")
	assert devices[r"USB\VID_2341&PID_0070\ECDA3B60A290"]["speed"] == 12
	keyboard = devices[r"USB\VID_413C&PID_2113\5&33176d27&0&3"]
	assert (keyboard["speed"], keyboard["serial"], keyboard["bus"], keyboard["bus_speed"]) == (1.5, None, "usb2", 480)
	# A FLIR camera's USB serial is its serial number in hex
	assert preflight.FindCameraDevice(list(devices.values()), {"cameraMake": "flir", "cameraSerialNo": "22168577"}) \
		is cam


def test_a_hub_between_and_a_faster_link():
	# A camera behind a USB 3 hub, and one at 10 Gb/s on another controller: each found its controller
	hub = r"USB\VID_2109&PID_0817\6&ABCDEF&0&2"
	windows = FakeWindows({
		ROOT: (CONTROLLER, 0, None),
		hub: (ROOT, 21, SS),
		camera("0000000B"): (hub, 1, SS),
		OTHER_ROOT: (OTHER, 0, None),
		camera("0000000C"): (OTHER_ROOT, 2, SS_PLUS),
		camera("0000000D"): (OTHER_ROOT, 3, SS),
		})
	devices = {d["serial"]: d for d in usbwin.ListUsbDevices(windows)}
	assert devices["0000000B"]["controller"] == CONTROLLER
	assert devices["0000000C"]["controller_name"] == "1022:15b6"
	assert (devices["0000000C"]["speed"], devices["0000000D"]["speed"]) == (10000, 5000)
	assert devices["0000000D"]["bus_speed"] == 10000 # the fastest link on that side of its controller
	assert (hub, 1) in windows.asked # the camera's own hub was asked, not the root hub


def test_a_hub_that_cannot_say():
	windows = slim(**{"0171ED30": OSError("Asking hub USB\\ROOT_HUB30 about port 20: Windows error 31")})
	devices = {d["serial"]: d for d in usbwin.ListUsbDevices(windows)}
	assert devices["0171ED30"]["speed"] is None
	assert "Windows error 31" in devices["0171ED30"]["error"]
	assert devices["01524401"]["speed"] == 5000 # the others are read


def test_no_device_list():
	with pytest.raises(OSError, match="Listing USB devices failed"):
		usbwin.ListUsbDevices(FakeWindows({}, gone="Listing USB devices failed (CONFIGRET 0x3)"))


def test_controller_names():
	assert usbwin.ControllerName(CONTROLLER) == "8086:7f6e"
	assert usbwin.ControllerName("unknown") == "unknown"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' Configuration Manager and USB hubs")
def test_this_computers_usb_devices():
	# Whatever is plugged in (perhaps nothing, on a CI machine): read without an error, each with a speed or why not
	for device in usbwin.ListUsbDevices():
		assert device["speed"] in (1.5, 12.0, 480.0, 5000.0, 10000.0) or device.get("error")
