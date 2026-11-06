"""
Windows settings that can end a long recording, for campy-session's checks
(the config's windows: section):

- Windows Update, unpaused, restarts the PC by itself outside its active
  hours, which ends campy, SpikeGLX and a Pico powered by USB. The check
  warns unless updates are paused until a day after the recording's planned
  end; the panel's button pauses them, with tools/windows/pause_windows_update.ps1,
  which asks for administrator rights itself (Windows' prompt: Yes).
- USB selective suspend, on, lets Windows power down a USB device it thinks
  is idle (tools/windows/disable_usb_selective_suspend.ps1 turns it off; run
  as administrator).

Elsewhere than Windows these checks say nothing.
"""

import math, os, subprocess, sys
from datetime import datetime, timedelta, timezone

KEYS = {
	"update_paused": ((bool,), True, "warn unless Windows Update is paused until a day after the recording's planned end"),
	"usb_suspend": ((bool,), True, "warn while USB selective suspend is on (the power plan's USB settings)"),
	}

WINDOWS = sys.platform == "win32"
UX = r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
USB_SUBGROUP = "2a737441-1930-4402-8d77-b2bebba308a3"
USB_SUSPEND = "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"


def Tool(name):
	import campy
	return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(campy.__file__))), "tools", "windows", name)


def PausedUntil():
	"""When Windows Update's pause ends (a local datetime), or None (not paused, or not Windows)."""
	if not WINDOWS:
		return None
	try:
		import winreg
		with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, UX) as key:
			value = winreg.QueryValueEx(key, "PauseUpdatesExpiryTime")[0]
	except OSError:
		return None
	try:
		return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone()
	except ValueError:
		return None


def UsbSuspendOn():
	"""True when USB selective suspend is on (plugged in) in the active power plan; None when it can't be told."""
	if not WINDOWS:
		return None
	try:
		out = subprocess.run(["powercfg", "/query", "SCHEME_CURRENT", USB_SUBGROUP, USB_SUSPEND], capture_output=True,
			text=True, timeout=30)
	except (OSError, subprocess.TimeoutExpired):
		return None
	for line in out.stdout.splitlines():
		if "Current AC Power Setting Index" in line:
			return int(line.split(":")[-1].strip(), 16) != 0
	return None


def Days(seconds):
	# How long to pause for a recording of seconds: its days, and one more
	return int(math.ceil(seconds / 86400.0)) + 1


def Checks(section, seconds):
	"""[(level, text, action)] for a recording of seconds: "warn" or "ok"; action "pause-updates" for the button."""
	if not WINDOWS:
		return []
	out = []
	if section["update_paused"]:
		until = PausedUntil()
		need = datetime.now().astimezone() + timedelta(seconds=seconds or 0, days=1)
		if until is not None and until >= need:
			out.append(("ok", "Windows Update paused until {}".format(When(until)), None))
		else:
			out.append(("warn", "Windows Update isn't paused until a day after the recording's end{}: it can restart this "
				"PC mid-recording, which ends campy, SpikeGLX and the Pico's session. Pause it for {} days (Windows asks "
				"for administrator rights: Yes).".format(" (paused until {})".format(When(until)) if until else "",
				Days(seconds or 0)), "pause-updates"))
	if section["usb_suspend"]:
		on = UsbSuspendOn()
		if on:
			out.append(("warn", "USB selective suspend is on: Windows may power down an idle USB device. Turn it off: "
				"{} (as administrator).".format(Tool("disable_usb_selective_suspend.ps1")), None))
		elif on is False:
			out.append(("ok", "USB selective suspend is off", None))
	return out


def When(t):
	return t.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def PauseUpdates(days):
	"""Run pause_windows_update.ps1 -Days days (Windows asks for administrator rights): (ok, what it said)."""
	if not WINDOWS:
		return False, "Not Windows."
	try:
		out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
			Tool("pause_windows_update.ps1"), "-Days", str(max(1, min(int(days), 35)))], capture_output=True, text=True,
			timeout=300)
	except (OSError, subprocess.TimeoutExpired) as e:
		return False, str(e)
	return out.returncode in (0, 3), (out.stdout + out.stderr).strip()[-600:]
