"""
Each USB device's link speed on Windows, from its hub, for the launch check
(campy/preflight.py) and the recording watch (campy/watch.py). Linux's sysfs
says the same in files; Windows says it to whoever asks the hub driver, as
Microsoft's USBView does:

  1. Configuration Manager (cfgmgr32) lists the USB devices plugged in now:
     USB\\VID_1E10&PID_4000\\01524401, the last part being the device's USB
     serial (a FLIR camera's serial number, in hex), and each one's hub and
     port on it
  2. the hub driver says what is on that port
     (IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX and _EX_V2): low, full or
     high speed (USB 2), or SuperSpeed (USB 3: 5 Gb/s) and SuperSpeedPlus
     (10 Gb/s)

Only asks: nothing is opened but the hub (with no access rights: none are
needed to ask), and no camera is touched. All of it takes about 1.5 ms on
the Dell Pro Max Slim FCS1250 (12 USB devices). Tests give ListUsbDevices
a made-up Api.
"""

import re

# What the hub's _EX answer says in its Speed byte. A SuperSpeed device says 2 (high speed) there, for
# programs older than USB 3: _EX_V2's flags say SuperSpeed.
SPEEDS = {0: 1.5, 1: 12.0, 2: 480.0, 3: 5000.0}
SUPERSPEED, SUPERSPEED_PLUS = 0x1, 0x4 # _EX_V2 flags: DeviceIsOperatingAtSuperSpeed(Plus)OrHigher


class WindowsApi:
	"""Configuration Manager and the hub driver, through ctypes: device instance IDs in, plain values out."""

	CR_SUCCESS, CR_BUFFER_SMALL = 0, 0x1A
	FILTER_ENUMERATOR, FILTER_PRESENT = 0x1, 0x100 # CM_GETIDLIST_FILTER_*
	CM_DRP_ADDRESS = 0x1D # a USB device's port number on its hub
	IOCTL_EX = 0x220448 # IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX
	IOCTL_EX_V2 = 0x22045C # IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2
	SHARE_READ_WRITE, OPEN_EXISTING = 0x3, 3

	def __init__(self):
		import ctypes
		from ctypes import wintypes as wt
		self.ctypes, self.wt = ctypes, wt

		class GUID(ctypes.Structure):
			_fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]
		# GUID_DEVINTERFACE_USB_HUB
		self.hubGuid = GUID(0xf18a0e88, 0xc30c, 0x11d0, (ctypes.c_ubyte * 8)(0x88, 0x15, 0x00, 0xa0, 0xc9, 0x06, 0xbe,
			0xd8))
		cm = ctypes.WinDLL("cfgmgr32")
		k32 = ctypes.WinDLL("kernel32", use_last_error=True)
		P = ctypes.POINTER
		for name, args in (
				("CM_Get_Device_ID_List_SizeW", [P(wt.ULONG), wt.LPCWSTR, wt.ULONG]),
				("CM_Get_Device_ID_ListW", [wt.LPCWSTR, wt.LPWSTR, wt.ULONG, wt.ULONG]),
				("CM_Locate_DevNodeW", [P(wt.DWORD), wt.LPCWSTR, wt.ULONG]),
				("CM_Get_Parent", [P(wt.DWORD), wt.DWORD, wt.ULONG]),
				("CM_Get_Device_IDW", [wt.DWORD, wt.LPWSTR, wt.ULONG, wt.ULONG]),
				("CM_Get_DevNode_Registry_PropertyW", [wt.DWORD, wt.ULONG, P(wt.ULONG), wt.LPVOID, P(wt.ULONG),
					wt.ULONG]),
				("CM_Get_Device_Interface_List_SizeW", [P(wt.ULONG), P(GUID), wt.LPCWSTR, wt.ULONG]),
				("CM_Get_Device_Interface_ListW", [P(GUID), wt.LPCWSTR, wt.LPWSTR, wt.ULONG, wt.ULONG])):
			f = getattr(cm, name)
			f.argtypes, f.restype = args, wt.DWORD
		k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE]
		k32.CreateFileW.restype = wt.HANDLE
		k32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, wt.LPVOID, wt.DWORD, wt.LPVOID, wt.DWORD, P(wt.DWORD),
			wt.LPVOID]
		k32.DeviceIoControl.restype = wt.BOOL
		k32.CloseHandle.argtypes = [wt.HANDLE]
		self.cm, self.k32 = cm, k32
		self.invalid = ctypes.c_void_p(-1).value

	def Check(self, result, what):
		if result != self.CR_SUCCESS:
			raise OSError("{} failed (CONFIGRET 0x{:X})".format(what, result))

	def DeviceIds(self):
		"""The instance ID of every USB device (and USB interface, and root hub) plugged in now."""
		ctypes, wt = self.ctypes, self.wt
		flags = self.FILTER_ENUMERATOR | self.FILTER_PRESENT
		for _ in range(5): # a device plugged in between the two calls makes the list longer
			n = wt.ULONG()
			self.Check(self.cm.CM_Get_Device_ID_List_SizeW(ctypes.byref(n), "USB", flags), "Listing USB devices")
			buf = ctypes.create_unicode_buffer(n.value)
			result = self.cm.CM_Get_Device_ID_ListW("USB", buf, n.value, flags)
			if result != self.CR_BUFFER_SMALL:
				self.Check(result, "Listing USB devices")
				return [s for s in ctypes.wstring_at(ctypes.addressof(buf), n.value).split("\0") if s]
		raise OSError("Listing USB devices: the list kept changing")

	def Node(self, iid):
		node = self.wt.DWORD()
		self.Check(self.cm.CM_Locate_DevNodeW(self.ctypes.byref(node), iid, 0), "Finding {}".format(iid))
		return node.value

	def Parent(self, iid):
		"""The instance ID of the device this one is plugged into (its hub; a root hub's: its controller)."""
		ctypes = self.ctypes
		parent = self.wt.DWORD()
		self.Check(self.cm.CM_Get_Parent(ctypes.byref(parent), self.Node(iid), 0), "Finding {}'s hub".format(iid))
		buf = ctypes.create_unicode_buffer(512)
		self.Check(self.cm.CM_Get_Device_IDW(parent.value, buf, 512, 0), "Naming {}'s hub".format(iid))
		return buf.value

	def Port(self, iid):
		"""Its port number on its hub."""
		ctypes, wt = self.ctypes, self.wt
		value, size = wt.DWORD(), wt.ULONG(4)
		self.Check(self.cm.CM_Get_DevNode_Registry_PropertyW(self.Node(iid), self.CM_DRP_ADDRESS, None,
			ctypes.byref(value), ctypes.byref(size), 0), "Reading {}'s port".format(iid))
		return value.value

	def HubPath(self, hub):
		ctypes, wt = self.ctypes, self.wt
		n = wt.ULONG()
		self.Check(self.cm.CM_Get_Device_Interface_List_SizeW(ctypes.byref(n), ctypes.byref(self.hubGuid), hub, 0),
			"Finding hub {}".format(hub))
		buf = ctypes.create_unicode_buffer(max(n.value, 1))
		self.Check(self.cm.CM_Get_Device_Interface_ListW(ctypes.byref(self.hubGuid), hub, buf, n.value, 0),
			"Finding hub {}".format(hub))
		if not buf.value:
			raise OSError("Hub {} has no device interface".format(hub))
		return buf.value

	def Link(self, hub, port):
		"""What the hub says of its port: (the _EX answer's Speed byte, the _EX_V2 answer's flags or None)."""
		ctypes, wt = self.ctypes, self.wt
		handle = self.k32.CreateFileW(self.HubPath(hub), 0, self.SHARE_READ_WRITE, None, self.OPEN_EXISTING, 0, None)
		if handle is None or handle == self.invalid:
			raise OSError("Opening hub {}: Windows error {}".format(hub, ctypes.get_last_error()))
		try:
			got = wt.DWORD()
			ex = ctypes.create_string_buffer(port.to_bytes(4, "little"), 2048)
			if not self.k32.DeviceIoControl(handle, self.IOCTL_EX, ex, len(ex), ex, len(ex), ctypes.byref(got), None):
				raise OSError("Asking hub {} about port {}: Windows error {}".format(hub, port,
					ctypes.get_last_error()))
			speed = ex.raw[23] # after ConnectionIndex (4 bytes), the device descriptor (18), its configuration (1)
			# _EX_V2: ConnectionIndex, Length 16, the USB versions asked about (1.1, 2.0, 3.0), its flags back
			v2 = ctypes.create_string_buffer(port.to_bytes(4, "little") + (16).to_bytes(4, "little") +
				(7).to_bytes(4, "little") + bytes(4), 16)
			flags = None
			if self.k32.DeviceIoControl(handle, self.IOCTL_EX_V2, v2, 16, v2, 16, ctypes.byref(got), None):
				flags = int.from_bytes(v2.raw[12:16], "little")
			return speed, flags
		finally:
			self.k32.CloseHandle(handle)


Api = WindowsApi # (tests replace it: a made-up device tree)


def LinkSpeed(speed, flags):
	"""Mb/s of a link, as Linux's sysfs says it, from what the hub said."""
	if flags is not None and flags & SUPERSPEED_PLUS:
		return 10000.0
	if flags is not None and flags & SUPERSPEED:
		return 5000.0
	return SPEEDS.get(speed)


def ControllerName(iid):
	# PCI\VEN_8086&DEV_7F6E&SUBSYS_0CFD1028&REV_10\3&36cb97a3&0&A0 -> 8086:7f6e, as lspci names it
	m = re.match(r"PCI\\VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})", iid, re.I)
	return "{}:{}".format(m.group(1), m.group(2)).lower() if m else iid


def ListUsbDevices(api=None):
	"""
	Every USB device plugged in now, as preflight.ListUsbDevices gives
	Linux's: name (its instance ID), vendor, product, serial (None when it
	has none: Windows then makes up the instance ID's last part), speed (Mb/s
	of its own link; None, with error, when its hub could not say), and the
	data path it shares with others: controller (its host controller's
	instance ID), controller_name ("8086:7f6e"), bus ("usb3" or "usb2": an
	xHCI controller carries USB 3 and USB 2 links apart) and bus_speed.
	Windows doesn't say how fast a controller is, only each link:
	bus_speed is the fastest link on that side of it (bus_speed_known
	False). Raises OSError when Windows can't list the devices.
	"""
	api = api or Api()
	devices = []
	for iid in api.DeviceIds():
		m = re.match(r"USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})\\(.+)$", iid, re.I)
		if not m: # a root hub (USB\ROOT_HUB30\...), or an interface of a device (USB\VID_...&MI_00\...)
			continue
		serial = m.group(3)
		device = {"name": iid, "vendor": m.group(1).lower(), "product": m.group(2).lower(),
			"serial": None if "&" in serial else serial, "speed": None, "controller": "unknown",
			"bus_speed_known": False}
		try:
			hub = api.Parent(iid)
			device["speed"] = LinkSpeed(*api.Link(hub, api.Port(iid)))
			if device["speed"] is None:
				device["error"] = "its hub gave an unknown speed"
			for _ in range(16): # up through any hubs to the root hub, whose parent is the controller
				if hub.upper().startswith("USB\\ROOT_HUB"):
					device["controller"] = api.Parent(hub)
					break
				hub = api.Parent(hub)
		except OSError as e:
			device["error"] = str(e)
		devices.append(device)
	for device in devices:
		device["controller_name"] = ControllerName(device["controller"])
		device["bus"] = "usb3" if (device["speed"] or 0) >= 5000 else "usb2"
	for device in devices:
		side = [d["speed"] for d in devices if d["controller"] == device["controller"] and d["bus"] == device["bus"]
			and d["speed"] is not None]
		device["bus_speed"] = max(side) if device["bus"] == "usb3" and side else 480.0
	return devices
