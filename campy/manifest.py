"""
session.json: how a recording was made, in its folder.

Every recording folder holds the videos and their times, and metadata.csv
has each camera's parameters. What it did not hold: which campy made it
(version and commit), on what computer, GPU, driver and ffmpeg, with which
config and where each value came from (campy's default, the config file or
the command line), the cameras' serial numbers and firmware, the trigger,
the experiment (animals, notes), and how it ended. A default that changes
between versions changes the recording (campy/defaults.py), so a folder that
does not say what made it cannot be told apart from one that a different
campy made from the same config.

campy-acquire writes <recording>/session.json (schema "campy-session/1"),
three times, each time adding to it, never changing what it said before
(but "status"):

  start      before the cameras start: "campy", "host", "config", "experiment",
             "cameras" (name, make, serial, files), "trigger" (its settings).
             status "recording".
  recording  once the trigger has started (or campy has given up waiting for
             the cameras): "recording": trigger.json's record, and each
             camera's model, firmware and frame size, from its metadata.csv.
  end        when the cameras have stopped: "end": how each camera ended
             (frames grabbed, written, dropped, lost, seconds, errors), the
             exit code. status "complete", "interrupted" (Ctrl-C) or "failed".

A campy that is killed, or a computer that loses power, leaves status
"recording": the recording did not end as campy meant it to.

campy-live writes <recording>/campy-live-session.json (schema
"campy-live-session/1"): its own environment (torch, sleap-nn, CUDA or
ROCm, the GPU), the models (folder; "id", the first 12 hex digits of
best.ckpt's SHA-256; the SHA-256s of best.ckpt and training_config.yaml;
the training settings that describe them; model.json when the folder has
one), the calibration (path, SHA-256), every option with where it came from,
and at its end what it tracked. It finds the recording's folder from the
live feed (livefeed.RecordingFolder); without one (a feed from an older
campy, or a test feed) the file goes beside campy-live's log, as
<log>.session.json. One program per file: two that rewrite the same file
can lose each other's writes.

Notes after the recording: campy-session note <folder> "text" (or python -m
campy.manifest note <folder> "text") adds {"at", "text"} to session.json's
"annotations", which is the only part anything but campy-acquire writes.

Each write replaces the whole file at once (a new file, renamed over the
old), so a reader never finds half of one; a lock file (session.json.lock)
keeps campy-acquire and a note from writing at the same moment.

Readers: campy-live-export copies both into the .slp's provenance and names
the animals by animalIds. Read(folder) gives both.

This module needs only Python's standard library (and PyYAML, where there
is one, for a model's training config): campy-live, in an environment of
its own, uses it too.
"""

import os, sys, json, time, socket, shlex, hashlib, platform, subprocess, threading, shutil, csv, ast, argparse
from datetime import datetime

SCHEMA = "campy-session/1"
LIVE_SCHEMA = "campy-live-session/1"
SESSION_FILE = "session.json"
LIVE_FILE = "campy-live-session.json"

STATUS_RECORDING, STATUS_COMPLETE, STATUS_INTERRUPTED, STATUS_FAILED = "recording", "complete", "interrupted", "failed"

# The packages whose versions say how a recording was made, by program
ACQUIRE_PACKAGES = ("numpy", "imageio", "imageio-ffmpeg", "PyYAML", "scipy", "pyserial", "psutil", "PyQt5",
	"spinnaker-python", "pypylon", "nireyecam")
LIVE_PACKAGES = ("numpy", "torch", "sleap-nn", "sleap-io", "tensorrt", "tensorrt-cu12", "tensorrt-cu13", "onnx",
	"triton", "triton-windows", "pytorch-triton-rocm", "scipy", "PyYAML")

LAUNCHER_ENV = "CAMPY_LAUNCHER" # set by campy-session, the launcher: "campy-session/<version>"


def Now():
	# Local time, with its offset from UTC: 2026-10-23T14:05:11-04:00
	return datetime.now().astimezone().isoformat(timespec="seconds")


def Sha256(path, chunk=1 << 20):
	# A file's SHA-256, in hex; None when it cannot be read
	try:
		h = hashlib.sha256()
		with open(path, "rb") as f:
			while True:
				block = f.read(chunk)
				if not block:
					break
				h.update(block)
		return h.hexdigest()
	except OSError:
		return None


def FileRecord(path, text=False):
	# {"path", "sha256"} for a file, and its text when asked (a config); None for no file
	if path is None or str(path) in ("", "None"):
		return None
	record = {"path": os.path.abspath(str(path)), "sha256": Sha256(str(path))}
	if record["sha256"] is None:
		record["missing"] = True
	elif text:
		try:
			with open(str(path), encoding="utf-8", errors="replace") as f:
				record["text"] = f.read()
		except OSError:
			pass
	return record


def Run(command, timeout=10):
	# A command's output, or None when it is not there or fails
	try:
		done = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
			creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
	except (OSError, subprocess.SubprocessError):
		return None
	return done.stdout if done.returncode == 0 else None


# ------------------------------------------------------------------ campy itself

def CampyRoot():
	# The folder campy's package is in: a git checkout's, or site-packages
	return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def SetupVersion(root):
	# version= in a checkout's setup.py, which an editable install's metadata
	# says only as it was when installed
	try:
		with open(os.path.join(root, "setup.py"), encoding="utf-8") as f:
			for line in f:
				line = line.strip()
				if line.startswith("version=") or line.startswith("version ="):
					return line.split("=", 1)[1].strip().strip(",").strip("'\"")
	except OSError:
		pass
	return None


def PackageVersion(name):
	try:
		from importlib import metadata
		return metadata.version(name)
	except Exception:
		return None


def GitDir(root):
	# The checkout's git folder: .git, or the folder a worktree's .git file names
	dot = os.path.join(root, ".git")
	if os.path.isdir(dot):
		return dot
	try:
		with open(dot, encoding="utf-8") as f:
			line = f.read().strip()
		if line.startswith("gitdir:"):
			path = line.split(":", 1)[1].strip()
			return path if os.path.isabs(path) else os.path.normpath(os.path.join(root, path))
	except OSError:
		pass
	return None


def ReadHead(gitdir):
	# (commit, branch) from the git folder's files, for a computer without git
	try:
		with open(os.path.join(gitdir, "HEAD"), encoding="utf-8") as f:
			head = f.read().strip()
		if not head.startswith("ref:"):
			return head, None
		ref = head.split(":", 1)[1].strip()
		# A worktree's refs are in the main repository's folder (commondir)
		common = gitdir
		try:
			with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as f:
				common = os.path.normpath(os.path.join(gitdir, f.read().strip()))
		except OSError:
			pass
		branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
		for folder in (gitdir, common):
			try:
				with open(os.path.join(folder, ref), encoding="utf-8") as f:
					return f.read().strip(), branch
			except OSError:
				pass
		with open(os.path.join(common, "packed-refs"), encoding="utf-8") as f:
			for line in f:
				parts = line.split()
				if len(parts) == 2 and parts[1] == ref:
					return parts[0], branch
		return None, branch
	except OSError:
		return None, None


def CampyInfo():
	"""
	Which campy this is: its version, and for a git checkout the commit, the
	branch and whether its tracked files differ from that commit (dirty).
	Without git, the commit from the checkout's files, and dirty None (not
	known).
	"""
	root = CampyRoot()
	info = {"version": SetupVersion(root) or PackageVersion("campy"), "path": os.path.dirname(os.path.abspath(__file__))}
	gitdir = GitDir(root)
	if gitdir is None:
		info["installedFrom"] = "package"
		return info
	info["installedFrom"] = "git checkout"
	git = shutil.which("git")
	commit = branch = dirty = describe = None
	if git:
		commit = (Run([git, "-C", root, "rev-parse", "HEAD"]) or "").strip() or None
		branch = (Run([git, "-C", root, "rev-parse", "--abbrev-ref", "HEAD"]) or "").strip() or None
		status = Run([git, "-C", root, "status", "--porcelain", "--untracked-files=no"])
		dirty = None if status is None else bool(status.strip())
		describe = (Run([git, "-C", root, "describe", "--tags", "--always"]) or "").strip() or None
	if commit is None:
		commit, branch = ReadHead(gitdir)
	info.update(commit=commit, branch=branch, dirty=dirty)
	if describe:
		info["describe"] = describe
	return info


# ------------------------------------------------------------------ the computer

def CpuName():
	try:
		if sys.platform.startswith("linux"):
			with open("/proc/cpuinfo") as f:
				for line in f:
					if line.startswith("model name"):
						return line.split(":", 1)[1].strip()
		elif sys.platform == "darwin":
			name = (Run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "").strip()
			if name:
				return name
		elif sys.platform == "win32":
			import winreg
			key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
			return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
	except Exception:
		pass
	return platform.processor() or platform.machine()


PCI_VENDORS = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}


def LspciName(line):
	"""
	The vendor and device names in a line of lspci -mm: quoted fields, with
	options such as -rc1 -p00 between them, unquoted:
	  c6:00.0 "Display controller" "Advanced Micro Devices, Inc. [AMD/ATI]" "Device 1586" -rc1 -p00 ...
	None when it cannot be read.
	"""
	try:
		fields = [f for f in shlex.split((line or "").strip()) if not f.startswith("-")]
	except ValueError:
		return None
	# slot, class, vendor, device
	return "{} {}".format(fields[2], fields[3]) if len(fields) >= 4 else None


def Gpus():
	"""
	The computer's GPUs, each {"name", "driver", ...} as far as it can tell:
	nvidia-smi for NVIDIA's, the Windows display adapters (name and driver
	version), Linux's /sys/class/drm (vendor, PCI ID, kernel driver and, for
	amdgpu, the kernel), macOS's system_profiler.
	"""
	gpus = []
	smi = shutil.which("nvidia-smi")
	if smi:
		out = Run([smi, "--query-gpu=index,name,driver_version,pci.bus_id,memory.total", "--format=csv,noheader"])
		for line in (out or "").splitlines():
			parts = [p.strip() for p in line.split(",")]
			if len(parts) >= 5:
				gpus.append({"vendor": "NVIDIA", "index": int(parts[0]) if parts[0].isdigit() else parts[0],
					"name": parts[1], "driver": parts[2], "pciBus": parts[3], "memory": parts[4]})
	if sys.platform == "win32":
		out = Run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
			"Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,AdapterCompatibility,"
			"PNPDeviceID | ConvertTo-Json -Compress"], timeout=20)
		try:
			adapters = json.loads(out) if out else []
		except ValueError:
			adapters = []
		for a in adapters if isinstance(adapters, list) else [adapters]:
			name = a.get("Name")
			if any(g.get("name") == name for g in gpus):
				for g in gpus:
					if g.get("name") == name:
						g["windowsDriver"] = a.get("DriverVersion")
				continue
			gpus.append({"vendor": a.get("AdapterCompatibility"), "name": name, "driver": a.get("DriverVersion"),
				"pnpId": a.get("PNPDeviceID")})
	elif sys.platform.startswith("linux"):
		drm = "/sys/class/drm"
		try:
			cards = sorted(c for c in os.listdir(drm) if c.startswith("card") and "-" not in c)
		except OSError:
			cards = []
		lspci = shutil.which("lspci")
		for card in cards:
			device = os.path.join(drm, card, "device")
			def read(name):
				try:
					with open(os.path.join(device, name)) as f:
						return f.read().strip()
				except OSError:
					return None
			vendor = read("vendor")
			if vendor == "0x10de" and any(g.get("vendor") == "NVIDIA" for g in gpus):
				continue # nvidia-smi has said
			gpu = {"vendor": PCI_VENDORS.get(vendor, vendor), "pciId": "{}:{}".format((vendor or "")[2:],
				(read("device") or "")[2:]), "card": card}
			try:
				gpu["driver"] = os.path.basename(os.readlink(os.path.join(device, "driver")))
			except OSError:
				pass
			slot = os.path.basename(os.path.realpath(device))
			if lspci:
				name = LspciName(Run([lspci, "-s", slot, "-mm"]))
				if name:
					gpu["name"] = name
			if read("vbios_version"):
				gpu["vbios"] = read("vbios_version")
			gpu["kernel"] = platform.release() # amdgpu and i915 are the kernel's: its version is theirs
			gpus.append(gpu)
		try:
			with open("/opt/rocm/.info/version") as f:
				rocm = f.read().strip()
			for gpu in gpus:
				if gpu.get("vendor") == "AMD":
					gpu["rocm"] = rocm
		except OSError:
			pass
	elif sys.platform == "darwin":
		out = Run(["system_profiler", "SPDisplaysDataType", "-json"], timeout=20)
		try:
			for d in json.loads(out).get("SPDisplaysDataType", []) if out else []:
				gpus.append({"name": d.get("sppci_model") or d.get("_name"), "vendor": d.get("spdisplays_vendor"),
					"cores": d.get("sppci_cores")})
		except ValueError:
			pass
	return gpus


def Ffmpeg():
	# The ffmpeg campy encodes with (imageio-ffmpeg's, or ffmpegPath), and its version line
	try:
		import imageio_ffmpeg
		exe = imageio_ffmpeg.get_ffmpeg_exe()
	except Exception as e:
		return {"path": None, "error": str(e)}
	out = Run([exe, "-version"], timeout=30)
	return {"path": exe, "version": out.splitlines()[0] if out else None}


def Environment():
	# The Python environment: conda's or a venv's
	env = {"prefix": sys.prefix, "executable": sys.executable}
	for name, key in (("CONDA_DEFAULT_ENV", "conda"), ("VIRTUAL_ENV", "venv")):
		if os.environ.get(name):
			env[key] = os.environ[name]
	return env


def Packages(names):
	# The versions of those installed; the others are left out
	out = {}
	for name in names:
		version = PackageVersion(name)
		if version is not None:
			out[name] = version
	return out


def HostInfo(packages=ACQUIRE_PACKAGES, ffmpeg=True):
	info = {
		"name": socket.gethostname(),
		"os": platform.platform(),
		"cpu": CpuName(),
		"cores": os.cpu_count(),
		"python": platform.python_version(),
		"environment": Environment(),
		"gpus": Gpus(),
		"packages": Packages(packages),
		}
	if ffmpeg:
		info["ffmpeg"] = Ffmpeg()
	return info


class Probe:
	"""
	HostInfo and CampyInfo, on a thread of their own, started as campy starts:
	nvidia-smi, PowerShell and ffmpeg -version take a second or two, which
	the recording should not wait for. Get waits at most timeout; what is not
	there by then is added to session.json at its next write.
	"""

	def __init__(self, packages=ACQUIRE_PACKAGES, ffmpeg=True):
		self.result = None
		self.thread = threading.Thread(target=self.Run, args=(packages, ffmpeg), daemon=True, name="campy host info")
		self.thread.start()

	def Run(self, packages, ffmpeg):
		try:
			self.result = {"campy": CampyInfo(), "host": HostInfo(packages, ffmpeg)}
		except Exception as e: # never in the recording's way
			self.result = {"campy": {"error": str(e)}, "host": {"error": str(e)}}

	def Get(self, timeout=None):
		self.thread.join(timeout)
		return self.result


# ------------------------------------------------------------------ the file

class WouldReplace(Exception):
	"""The manifest's file holds something this program did not write."""


class JsonFile:
	"""
	One JSON file, written whole each time: into a new file beside it, then
	renamed over it, so a reader finds either the old one or the new one.
	Windows cannot rename over a file a reader has open: Replace tries for a
	few seconds, then writes the file in place. Lock keeps two writers (campy-
	acquire, a note) from writing at once.
	"""

	LOCK_WAIT_S, LOCK_STALE_S = 10, 60

	def __init__(self, path):
		self.path = path
		self.written = False

	def Read(self):
		try:
			with open(self.path, encoding="utf-8") as f:
				text = f.read()
		except OSError:
			return None
		if not text.strip():
			return None
		try:
			return json.loads(text)
		except ValueError:
			return None

	def Lock(self):
		lock = self.path + ".lock"
		deadline = time.monotonic() + self.LOCK_WAIT_S
		while True:
			try:
				os.close(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL))
				return lock
			except FileExistsError:
				try:
					if time.time() - os.path.getmtime(lock) > self.LOCK_STALE_S:
						os.remove(lock) # left by a writer that died
						continue
				except OSError:
					continue
				if time.monotonic() > deadline:
					return None # write anyway: a recording's manifest matters more than a race with a note
				time.sleep(0.05)

	def Unlock(self, lock):
		if lock:
			try:
				os.remove(lock)
			except OSError:
				pass

	REPLACE_TRIES, REPLACE_WAIT_S = 50, 0.1

	def Replace(self, data):
		text = json.dumps(data, indent=2, default=str, ensure_ascii=False) + "\n"
		temp = "{}.{}.partial".format(self.path, os.getpid())
		try:
			with open(temp, "w", encoding="utf-8") as f:
				f.write(text)
				f.flush()
				os.fsync(f.fileno())
			for _ in range(self.REPLACE_TRIES):
				try:
					os.replace(temp, self.path)
					self.written = True
					return
				except PermissionError: # Windows: a reader has it open
					time.sleep(self.REPLACE_WAIT_S)
			try:
				self.WriteInPlace(text)
			except PermissionError as e:
				# A reader that lets no one else write (some editors, Excel)
				raise PermissionError(e.errno, "another program has {} open and lets no one write to it: "
					"close it and try again. The file was not changed".format(os.path.basename(self.path)),
					self.path) from None
			self.written = True
		finally:
			try:
				os.remove(temp)
			except OSError:
				pass # renamed: it is the file now

	def WriteInPlace(self, text):
		# Windows, a reader holding the file open: write into it, as it is not renamed over
		with open(self.path, "w", encoding="utf-8") as f:
			f.write(text)

	def Update(self, change):
		"""
		Under the lock: change(the file's data, or None) gives the new data,
		which is written. The first write refuses a file that holds anything
		(campy-acquire's is empty then: newfiles.Claim made it).
		"""
		lock = self.Lock()
		try:
			if not self.written:
				try:
					size = os.path.getsize(self.path)
				except OSError:
					size = 0
				if size and change is not None and getattr(change, "fresh", False):
					raise WouldReplace("{} is already there, and campy never replaces a recording's files."
						.format(os.path.abspath(self.path)))
			data = change(self.Read())
			self.Replace(data)
			return data
		finally:
			self.Unlock(lock)


def Fresh(function):
	# A change that starts the file: it must not replace one (JsonFile.Update)
	function.fresh = True
	return function


# ------------------------------------------------------------------ campy-acquire

def EffectiveConfig(params, sources, program="campy-acquire", skip=()):
	"""
	Every key's value and where it came from: {key: {"value", "source"}},
	source "default", "file" or "command line"; a default that changed
	(campy/defaults.py) also says when, and what it was ("defaultChanged").
	"""
	from campy import defaults
	out = {}
	for key in sorted(params):
		if key in skip:
			continue
		source = sources.get(key, "default")
		entry = {"value": params[key], "source": source}
		if source == "default":
			changed = defaults.Annotate(key, program)
			if changed:
				entry["defaultChanged"] = changed
		out[key] = entry
	return out


def ReadMetadata(path):
	# A camera's metadata.csv as {key: value}, values as Python reads them where it can
	out = {}
	try:
		with open(path, newline="", encoding="utf-8", errors="replace") as f:
			for row in csv.reader(f):
				if len(row) >= 2:
					try:
						out[row[0]] = ast.literal_eval(row[1])
					except (ValueError, SyntaxError):
						out[row[0]] = row[1]
	except OSError:
		return None
	return out


def Rel(path, folder):
	try:
		return os.path.relpath(os.path.abspath(path), os.path.abspath(folder)).replace(os.sep, "/")
	except ValueError: # another drive, on Windows
		return os.path.abspath(path)


# What session.json takes from each camera's metadata.csv, as it starts and at its end
OPENED_KEYS = ("cameraModel", "cameraFirmware", "cameraSerialNo", "frameWidth", "frameHeight", "offsetX", "offsetY",
	"pixelFormatInput", "exposureTimeInUs", "gain", "gamma", "resultingFrameRate", "ratcamFirmware")
TOTAL_KEYS = (("framesGrabbed", "totalFrames"), ("seconds", "totalTime"), ("framesWritten", "framesWritten"),
	("framesDropped", "framesDropped"), ("framesLost", "flirFramesLost"), ("framesLost", "ratcamFramesLost"),
	("framesIncomplete", "flirFramesIncomplete"),
	("cameraError", "cameraError"), ("videoError", "videoError"))


def SdkVersions(systems):
	# The camera SDKs' own versions (Spinnaker's library, pylon), for the makes in use
	out = {}
	for make, system in (systems or {}).items():
		if not isinstance(system, dict):
			continue
		try:
			if make == "flir":
				v = system["system"].GetLibraryVersion()
				out["spinnaker"] = "{}.{}.{}.{}".format(v.major, v.minor, v.type, v.build)
			elif make == "basler":
				from pypylon import pylon
				out["pylon"] = pylon.GetPylonVersionString()
		except Exception:
			pass
	return out


class Session:
	"""
	campy-acquire's session.json (see the module's doc): Start, Recording and
	End, from campy.py's Main. Each says what goes wrong, and goes on: the
	manifest never stops a recording.
	"""

	def __init__(self, params, sources, config_path, systems=None, warnings=(), probe=None, say=print):
		from campy import configurator, newfiles
		self.folder = params["videoFolder"]
		self.file = JsonFile(os.path.join(self.folder, SESSION_FILE))
		self.params, self.sources, self.configPath = params, sources, config_path
		self.systems, self.warnings, self.probe, self.say = systems, list(warnings), probe, say
		self.cams = [configurator.CameraParams(params, n) for n in range(params["numCams"])]
		self.newfiles, self.configurator = newfiles, configurator
		self.data = None
		self.failed = False

	def Write(self, change, fresh=False):
		if fresh:
			change = Fresh(change)
		try:
			self.data = self.file.Update(change)
		except Exception as e:
			if not self.failed:
				self.say("Could not write {}: {}. The recording goes on.".format(self.file.path, e))
			self.failed = True

	def Probed(self, data, timeout=0):
		# The computer and campy, once Probe has them
		if self.probe is None:
			return
		result = self.probe.Get(timeout)
		if result:
			for key in ("campy", "host"):
				if not data.get(key):
					data[key] = result[key]
			if self.systems and "sdk" not in data["host"]:
				data["host"]["sdk"] = SdkVersions(self.systems)

	def Cameras(self):
		cams = []
		for n, cam in enumerate(self.cams):
			make = str(cam["cameraMake"])
			serial = None
			try:
				serial = self.systems[make]["serials"][cam["cameraSelection"]]
			except Exception:
				pass
			record = {"index": n, "name": cam["cameraName"], "make": make, "selection": cam["cameraSelection"],
				"serial": None if serial in (None, []) else str(serial),
				"folder": Rel(self.newfiles.CameraFolder(cam), self.folder),
				"video": Rel(self.newfiles.VideoPath(cam), self.folder),
				"calibrationVideo": self.newfiles.Calibration(cam)}
			settings = cam.get("cameraSettings", "None")
			if str(settings) != "None":
				record["settingsFile"] = FileRecord(settings)
			cams.append(record)
		return cams

	def Trigger(self):
		from campy.trigger import trigger
		p = self.params
		record = {"controller": p["triggerController"], "drivenByCampy": trigger.DrivesTrigger(p),
			"startArduino": p["startArduino"]}
		if record["drivenByCampy"]:
			record.update(serialPort=p["serialPort"], digitalPins=p["digitalPins"],
				readyTimeoutSec=p["readyTimeoutSec"])
			try:
				record["frameRate"] = self.configurator.TriggerFrameRate(p)
			except Exception:
				pass
			pin, hz = self.configurator.BlinkSettings(p)
			if pin >= 0:
				record.update(blinkPin=pin, blinkHz=hz)
		record["cameraTriggers"] = {cam["cameraName"]: str(cam["cameraTrigger"]) for cam in self.cams}
		return record

	def Start(self, configured=None):
		"""
		The start: everything known before the cameras open. configured is the
		params as the config made them, before campy put this run's folder in
		videoFolder (videoFolderRoot) and the camera processes' own keys.
		"""
		configured = configured or self.params
		skip = ("n_cam", "baseFolder", "cameraName", "device", "camera", "cameraSerialNo", "videoFolderRoot")
		p = self.params

		def start(old):
			effective = EffectiveConfig(configured, self.sources, skip=skip)
			data = {
				"schema": SCHEMA,
				"status": STATUS_RECORDING,
				"written": {"start": Now()},
				"folder": os.path.abspath(self.folder),
				"campy": None,
				"host": None,
				"config": {
					"file": FileRecord(self.configPath, text=True),
					"commandLine": sys.argv[1:],
					"launcher": os.environ.get(LAUNCHER_ENV),
					"videoFolder": p.get("videoFolderRoot", p["videoFolder"]),
					"effective": effective,
					"defaultsVersion": p.get("defaultsVersion", "None"),
					"defaultWarnings": self.warnings,
					},
				"experiment": {"name": p.get("experiment", "None"), "animalIds": p.get("animalIds", []),
					"notes": p.get("notes", "None"), "fields": p.get("sessionFields", {})},
				"annotations": [],
				"cameras": self.Cameras(),
				"trigger": self.Trigger(),
				}
			self.Probed(data, timeout=3)
			return data
		self.Write(start, fresh=True)

	def CameraMetadata(self, n):
		return ReadMetadata(os.path.join(self.newfiles.CameraFolder(self.cams[n]), "metadata.csv")) or {}

	def Recording(self, ready=None, missing=None):
		# The trigger has started, or campy gave up waiting for the cameras
		def recording(old):
			data = self.Merge(old)
			if "recording" in data:
				return data
			record = {"at": Now()}
			trigger_path = os.path.join(self.folder, self.newfiles.TRIGGER_FILE)
			try:
				with open(trigger_path, encoding="utf-8") as f:
					record["trigger"] = json.load(f)
			except (OSError, ValueError):
				record["trigger"] = None
			cams = []
			for n, cam in enumerate(self.cams):
				meta = self.CameraMetadata(n)
				opened = {key: meta[key] for key in OPENED_KEYS if key in meta}
				opened["name"] = cam["cameraName"]
				opened["ready"] = None if ready is None else cam["cameraName"] in ready
				if missing and cam["cameraName"] in dict(missing):
					opened["notReady"] = dict(missing)[cam["cameraName"]]
				cams.append(opened)
			record["cameras"] = cams
			data["recording"] = record
			data["written"]["recording"] = record["at"]
			self.Probed(data)
			return data
		self.Write(recording)

	def End(self, failures=None, interrupted=False, error=None, exit_code=None):
		failures = failures or {}
		if error is not None:
			status = STATUS_FAILED
		elif interrupted:
			status = STATUS_INTERRUPTED
		else:
			status = STATUS_FAILED if failures else STATUS_COMPLETE
		def end(old):
			data = self.Merge(old)
			record = {"at": Now(), "exitCode": exit_code if exit_code is not None else (1 if failures or error else 0),
				"failures": failures}
			if error is not None:
				record["error"] = error
			cams = []
			for n, cam in enumerate(self.cams):
				meta = self.CameraMetadata(n)
				totals = {"name": cam["cameraName"]}
				for name, key in TOTAL_KEYS:
					if key in meta:
						totals[name] = meta[key]
				video = os.path.join(self.folder, data["cameras"][n]["video"]) if data.get("cameras") else None
				if video and os.path.exists(video):
					totals["videoBytes"] = os.path.getsize(video)
				# opened late, after the recording write: its model and firmware
				if data.get("recording") and n < len(data["recording"]["cameras"]):
					opened = data["recording"]["cameras"][n]
					for key in OPENED_KEYS:
						if key in meta and key not in opened:
							totals[key] = meta[key]
				cams.append(totals)
			record["cameras"] = cams
			data["end"] = record
			data["status"] = status
			data["written"]["end"] = record["at"]
			self.Probed(data, timeout=5)
			return data
		self.Write(end)
		return status

	def Merge(self, old):
		# This run's data, with the annotations the file has gained (notes)
		data = dict(self.data) if self.data else {}
		if old and isinstance(old.get("annotations"), list):
			data["annotations"] = old["annotations"]
		return data


# ------------------------------------------------------------------ notes

def SessionPath(folder):
	# A recording's session.json, from its folder or the file's own path
	return folder if os.path.basename(os.path.normpath(folder)) == SESSION_FILE else os.path.join(folder, SESSION_FILE)


def AddNote(folder, text, at=None):
	"""
	Adds {"at", "text"} to <folder>/session.json's annotations, under its
	lock: what is already there is kept as it is. Returns the note.
	"""
	path = SessionPath(folder)
	if not os.path.isfile(path):
		raise FileNotFoundError("{} has no {}: is it a recording's folder, from campy 2.1.1 or later?".format(
			os.path.dirname(os.path.abspath(path)), SESSION_FILE))
	note = {"at": at or Now(), "text": str(text)}
	f = JsonFile(path)
	f.written = True # adding to it, not making it
	def add(old):
		if old is None:
			raise ValueError("{} cannot be read as JSON: not changed.".format(path))
		old.setdefault("annotations", []).append(note)
		return old
	f.Update(add)
	return note


def NoteMain(argv=None):
	# campy-session note <folder> "text" (campy-session routes here)
	p = argparse.ArgumentParser(prog="campy-session note", description="Add a dated note to a recording's "
		"session.json (its annotations). What is there is kept as it is.")
	p.add_argument("folder", help="The recording's folder (or its session.json).")
	p.add_argument("text", help="The note.")
	args = p.parse_args(argv)
	try:
		note = AddNote(args.folder, args.text)
	except (OSError, ValueError) as e:
		print(e, file=sys.stderr)
		return 1
	print("Added to {}: {} {}".format(SessionPath(args.folder), note["at"], note["text"]))
	return 0


def Read(folder):
	"""
	A recording's manifests: {"session": session.json, "live": [each
	campy-live-session*.json]}, each None or [] when not there.
	"""
	session = JsonFile(os.path.join(folder, SESSION_FILE)).Read()
	live = []
	try:
		names = sorted(n for n in os.listdir(folder) if n.startswith("campy-live-session") and n.endswith(".json"))
	except OSError:
		names = []
	for name in names:
		data = JsonFile(os.path.join(folder, name)).Read()
		if data is not None:
			data.setdefault("file", name)
			live.append(data)
	return {"session": session, "live": live}


# ------------------------------------------------------------------ campy-live

# What a model's training_config says about it, where sleap-nn's keeps it:
# (path in the config, name in the manifest)
MODEL_SETTINGS = (
	(("data_config", "preprocessing", "scale"), "scale"),
	(("data_config", "preprocessing", "crop_size"), "crop_size"),
	(("data_config", "preprocessing", "crop_hw"), "crop_hw"),
	(("data_config", "preprocessing", "max_height"), "max_height"),
	(("data_config", "preprocessing", "max_width"), "max_width"),
	(("data_config", "preprocessing", "ensure_grayscale"), "ensure_grayscale"),
	(("data_config", "preprocessing", "ensure_rgb"), "ensure_rgb"),
	)
BACKBONE_SETTINGS = ("filters", "filters_rate", "max_stride", "output_stride", "stem_stride", "convs_per_block")
HEAD_SETTINGS = ("anchor_part", "sigma", "output_stride")


def Dig(config, path):
	for key in path:
		if not isinstance(config, dict) or key not in config:
			return None, False
		config = config[key]
	return config, True


def ModelSettings(config):
	# The settings that describe a trained model: its input scale and crop, and its network's size
	out = {}
	for path, name in MODEL_SETTINGS:
		value, found = Dig(config, path)
		if found:
			out[name] = value
	backbones, found = Dig(config, ("model_config", "backbone_config"))
	if found and isinstance(backbones, dict):
		for kind, backbone in backbones.items():
			if isinstance(backbone, dict):
				out["backbone"] = kind
				for key in BACKBONE_SETTINGS:
					if key in backbone:
						out[key] = backbone[key]
				break
	heads, found = Dig(config, ("model_config", "head_configs"))
	if found and isinstance(heads, dict):
		for kind, head in heads.items():
			if isinstance(head, dict):
				out["head"] = kind
				confmaps = head.get("confmaps") if isinstance(head.get("confmaps"), dict) else head
				for key in HEAD_SETTINGS:
					if key in confmaps:
						out["head_" + key] = confmaps[key]
				break
	return out


def LoadConfigFile(path):
	with open(path, encoding="utf-8", errors="replace") as f:
		text = f.read()
	if path.endswith(".json"):
		return text, json.loads(text)
	try:
		import yaml
		return text, yaml.safe_load(text)
	except ImportError:
		return text, None


def ModelRecord(folder):
	"""
	A trained model, as campy-live-session.json says it: its folder, "id" (the
	first 12 hex digits of best.ckpt's SHA-256, as the model loop names
	models), the SHA-256s, the settings that describe it, its training
	config's text, and model.json when the folder has one (its "id" is
	kept, and flagged when it is not best.ckpt's).
	"""
	if not folder:
		return None
	folder = os.path.abspath(folder)
	record = {"folder": folder}
	ckpt = os.path.join(folder, "best.ckpt")
	record["ckptSha256"] = Sha256(ckpt)
	record["id"] = record["ckptSha256"][:12] if record["ckptSha256"] else None
	for name in ("training_config.yaml", "training_config.json"):
		path = os.path.join(folder, name)
		if os.path.isfile(path):
			record["trainingConfig"] = name
			record["trainingConfigSha256"] = Sha256(path)
			try:
				text, config = LoadConfigFile(path)
				record["trainingConfigText"] = text
				if config:
					record["settings"] = ModelSettings(config)
			except Exception as e:
				record["trainingConfigError"] = str(e)
			break
	model_json = os.path.join(folder, "model.json")
	record["modelJson"] = None
	if os.path.isfile(model_json):
		try:
			with open(model_json, encoding="utf-8") as f:
				record["modelJson"] = json.load(f)
			given = record["modelJson"].get("id") if isinstance(record["modelJson"], dict) else None
			if given and record["id"] and given != record["id"]:
				record["idMismatch"] = "model.json says id {}, but best.ckpt's is {}".format(given, record["id"])
		except (OSError, ValueError) as e:
			record["modelJson"] = {"error": str(e)}
	return record


def TorchInfo():
	# torch's build and the GPU it sees, for campy-live's environment
	try:
		import torch
	except ImportError:
		return None
	info = {"version": torch.__version__, "cuda": getattr(torch.version, "cuda", None),
		"hip": getattr(torch.version, "hip", None)}
	try:
		if torch.cuda.is_available():
			info["devices"] = [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())]
			if info["cuda"] and not info["hip"]:
				info["cudnn"] = torch.backends.cudnn.version()
	except Exception:
		pass
	return info


def GivenOptions(parser, argv):
	"""
	The options argv sets, by dest: a parse with every default left out, so
	only what is given is there (abbreviations too, as argparse takes them).
	"""
	import copy
	quiet = copy.deepcopy(parser)
	for action in quiet._actions:
		action.default = argparse.SUPPRESS
		action.required = False
	try:
		given, _ = quiet.parse_known_args(argv)
	except SystemExit:
		return set()
	return set(vars(given))


def OptionSources(parser, argv):
	# {option: "command line" or "default"}, by its "--name", and {dest: option}
	given = GivenOptions(parser, argv)
	sources, names = {}, {}
	for action in parser._actions:
		if not action.option_strings or action.dest == "help":
			continue
		name = max(action.option_strings, key=len)
		names[action.dest] = name
		sources[name] = "command line" if action.dest in given else "default"
	return sources, names


class LiveSession:
	"""
	campy-live's campy-live-session.json (see the module's doc): Start once
	the models are ready and the feed found, End when campy-live is done.
	"""

	def __init__(self, path, say=print):
		self.file = JsonFile(path)
		self.path, self.say = path, say
		self.data = None
		self.failed = False

	@staticmethod
	def Place(recording, out):
		# Where it goes: the recording's folder (campy-live-session.json, _2 ... for
		# a second campy-live), else beside the log
		if recording:
			for n in range(1, 100):
				path = os.path.join(recording, LIVE_FILE if n == 1 else "campy-live-session_{}.json".format(n))
				try:
					os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL))
					return path
				except FileExistsError:
					continue
				except OSError:
					break
		return out + ".session.json"

	def Write(self, change, fresh=False):
		if fresh:
			change = Fresh(change)
		try:
			self.data = self.file.Update(change)
		except Exception as e:
			if not self.failed:
				self.say("Could not write {}: {}.".format(self.path, e))
			self.failed = True

	def Start(self, args, parser, argv, recording, models, calibration, warnings, extra=None, facts=None):
		# facts: "campy" and "host" as CampyInfo and HostInfo give them, read
		# earlier (campy-live, while its models load); else read here
		facts = facts or {}
		sources, names = OptionSources(parser, argv)
		options = {names[dest]: {"value": value, "source": sources.get(names[dest], "default")}
			for dest, value in sorted(vars(args).items()) if dest in names}
		from campy import defaults
		for name, entry in options.items():
			if entry["source"] == "default":
				changed = defaults.Annotate(name, "campy-live")
				if changed:
					entry["defaultChanged"] = changed
		def start(old):
			data = {
				"schema": LIVE_SCHEMA,
				"status": STATUS_RECORDING,
				"written": {"start": Now()},
				"recording": os.path.abspath(recording) if recording else None,
				"session": SESSION_FILE if recording and os.path.isfile(os.path.join(recording, SESSION_FILE)) else None,
				"campy": facts["campy"] if "campy" in facts else CampyInfo(),
				"host": facts["host"] if "host" in facts else HostInfo(LIVE_PACKAGES, ffmpeg=False),
				"torch": TorchInfo(),
				"commandLine": list(argv) if argv is not None else sys.argv[1:],
				"options": options,
				"defaultsVersion": getattr(args, "defaults_version", None),
				"defaultWarnings": list(warnings),
				"models": models,
				"calibration": calibration,
				}
			data.update(extra or {})
			return data
		self.Write(start, fresh=True)

	def End(self, summary, log, stopped=False, error=None):
		def end(old):
			data = dict(self.data or old or {})
			keys = ("sets", "skipped", "fps", "latency_ms", "inference_ms", "ended", "model", "cuda_graphs",
				"fast_upsample", "engine", "gpu_clock")
			record = {"at": Now(), "log": log, "summary": os.path.splitext(log)[0] + ".json" if log else None}
			if summary:
				record.update({k: summary[k] for k in keys if k in summary})
			if error is not None:
				record["error"] = error
			data["end"] = record
			data["status"] = STATUS_FAILED if error is not None else STATUS_INTERRUPTED if stopped else STATUS_COMPLETE
			data.setdefault("written", {})["end"] = record["at"]
			return data
		self.Write(end)


def Main(argv=None):
	# python -m campy.manifest note <folder> "text"
	argv = sys.argv[1:] if argv is None else list(argv)
	if argv and argv[0] == "note":
		return NoteMain(argv[1:])
	print("usage: python -m campy.manifest note <folder> \"text\"", file=sys.stderr)
	return 2


if __name__ == "__main__":
	sys.exit(Main())
