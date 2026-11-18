"""
SpikeGLX, for campy-session (the config's spikeglx: section): a recording's
SpikeGLX run, started and stopped with the cameras.

campy-session talks to SpikeGLX through its remote command server (turned on
once in SpikeGLX: Options > Command Server Settings > Enable; 127.0.0.1, port
4142) and SpikeGLX's official SDK (billkarsh/SpikeGLX-CPP-SDK: sglx_pkg, a
Python module and SglxApi.dll), which is not part of campy: spikeglx.sdk is
the folder that holds sglx_pkg.

Before a recording: SpikeGLX answers (it is opened if it isn't running and
spikeglx.exe says where it is), runs no run, and its trigger is TTL
(trigMode 2), so that it writes files only while the gate is high, not all
the time. A SpikeGLX freshly opened has settings "never validated": its
devices (spikeglx.devices) are then detected and verified, as Configure >
Detect > Verify | Save does. At each Start, a SpikeGLX whose devices aren't
the config's (its probes and OneBoxes) is given the config's the same way:
an experiment's config decides whether the probe is recorded, whatever
SpikeGLX was last set to (the OneBox alone, "(20,obx)", for fiber photometry
without a probe).

At the start: its data folder is <recording>/spikeglx, its run name
<run_prefix><date_time> (as the recording's folder), the run starts, and
once its stream (spikeglx.stream: js, ip; the OneBox's on the ChronicRecord
rig) holds more samples than the trigger's margin plus 2 s, recording is
enabled. (SpikeGLX says it is running about 6 s before the OneBox's samples
flow, and a gate edge then would open no file.) At the end: the run stops,
which closes its files.
"""

import glob, math, os, subprocess, sys, tempfile, threading, time

KEYS = {
	"sdk": ((str,), "", "the folder holding SpikeGLX's SDK (sglx_pkg, from SpikeGLX-CPP-SDK)"),
	"exe": ((str,), "", "SpikeGLX's program, opened if SpikeGLX isn't running (default: SpikeGLX must be open)"),
	"host": ((str,), "127.0.0.1", "SpikeGLX's command server"),
	"port": ((int,), 4142, "its port"),
	"devices": ((str,), "", "SpikeGLX's devices, by physical address (selectDevices): \"(20,1,1)(20,obx)\" the "
		"probe and the OneBox, \"(20,obx)\" the OneBox alone; given to a SpikeGLX freshly opened, and at each Start to "
		"one whose probes and OneBoxes differ"),
	"stream": ((list,), [1, 0], "the stream the gate's TTL is on, [js, ip] (1, 0: the first OneBox)"),
	"run_prefix": ((str,), "", "the run's name before the recording's date_time: \"chronic_\""),
	"write_mb_s": ((int, float), 24, "what SpikeGLX writes while the gate is open, in MB/s, for the panel's disk check "
		"before the start (24: a Neuropixels 2.0 probe's 23.1 and a OneBox's 0.85, measured on the ChronicRecord rig)"),
	"start_timeout_s": ((int, float), 120, "how long its run may take to start acquiring"),
	"templates": ((str,), "", "the panel's Live ephys card sorts spikes into units with these templates (campy-ephys "
		"train, from a Kilosort 4 sort of this probe; docs/live-ephys.md); default: none, threshold crossings only"),
	"match": ((str, int, float), "", "with templates: the card finds spikes with them too, below the threshold, in a "
		"thread of its own: \"learned\", each unit at the threshold learned in training; a number, every unit at that "
		"score; default: off, the threshold's spikes sorted"),
	"open_timeout_s":((int, float), 120, "how long SpikeGLX may take to open and answer"),
	}

TTL_TRIGGER = "2" # [DAQSettings] trigMode: 0 immediate, 1 timed, 2 TTL, ...
FOLDER = "spikeglx" # in the recording's folder


class SpikeGLXError(Exception):
	pass


def Entries(text):
	"""The parenthesized entries of a SpikeGLX devices string, each split at its commas: [[str, ...]]."""
	import re
	return [[v.strip() for v in e.split(",")] for e in re.findall(r"\(([^()]*)\)", text or "") if e.strip()]


def Wanted(devices):
	"""
	A spikeglx.devices string's probes {(slot, port, dock)} and OneBoxes
	{slot}, or None when it names anything else (an NI-DAQ, a passive probe),
	which campy leaves to SpikeGLX as it is.
	"""
	probes, boxes = set(), set()
	for e in Entries(devices):
		if len(e) == 2 and e[0].isdigit() and e[1].lower() == "obx":
			boxes.add(int(e[0]))
		elif len(e) == 3 and all(v.isdigit() for v in e):
			probes.add(tuple(int(v) for v in e))
		else:
			return None
	return probes, boxes


def Shown(probes, boxes):
	"""Probes and OneBoxes as a devices string: "(20,1,1)(20,obx)", "()" for none."""
	text = "".join("({},{},{})".format(*p) for p in sorted(probes)) + "".join("({},obx)".format(b) for b in sorted(boxes))
	return text or "()"


def Match(section):
	"""spikeglx.match as campy-ephys serve's --match: "learned", a score above 0, or None (off: "", 0)."""
	value = section.get("match", "")
	if isinstance(value, str):
		value = value.strip()
		if value.lower() in ("", "0", "off", "none"):
			return None
		if value.lower() == "learned":
			return "learned"
		try:
			value = float(value)
		except ValueError:
			value = None
	if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
		return "{:g}".format(value) if value else None
	raise SpikeGLXError("spikeglx.match: \"learned\" (each unit at the threshold learned in training), a score above "
		"0 (every unit at it), or 0 (off); not {!r}.".format(section.get("match")))


_sdk = {}


def Sdk(folder):
	"""SpikeGLX's SDK module (sglx_pkg.sglx), from its folder."""
	folder = os.path.abspath(folder)
	if folder not in _sdk:
		if not os.path.isdir(os.path.join(folder, "sglx_pkg")):
			raise SpikeGLXError("SpikeGLX's SDK not found: no sglx_pkg in {} (spikeglx.sdk in the config).".format(folder))
		if hasattr(os, "add_dll_directory") and os.path.isdir(os.path.join(folder, "sglx_pkg")):
			os.add_dll_directory(os.path.join(folder, "sglx_pkg")) # SglxApi.dll lives beside sglx.py
		sys.path.insert(0, folder)
		try:
			from sglx_pkg import sglx
		except Exception as e:
			raise SpikeGLXError("Could not load SpikeGLX's SDK from {}: {}".format(folder, e))
		finally:
			sys.path.remove(folder)
		_sdk[folder] = sglx
	return _sdk[folder]


class Connection:
	"""One connection to SpikeGLX's command server (a with block)."""

	def __init__(self, sdk, host, port):
		self.sglx, self.host, self.port = sdk, host, port

	def __enter__(self):
		self.h = self.sglx.c_sglx_createHandle()
		if not self.sglx.c_sglx_connect(self.h, self.host.encode(), self.port):
			error = self.Error()
			self.sglx.c_sglx_destroyHandle(self.h)
			raise SpikeGLXError("SpikeGLX doesn't answer on {}:{} ({}). Is it open, with its command server on "
				"(Options > Command Server Settings > Enable)?".format(self.host, self.port, error))
		return self

	def __exit__(self, *a):
		self.sglx.c_sglx_close(self.h)
		self.sglx.c_sglx_destroyHandle(self.h)

	def Error(self):
		e = self.sglx.c_sglx_getError(self.h)
		return e.decode(errors="replace") if e else "?"

	def Ok(self, ok, what):
		if not ok:
			raise SpikeGLXError("SpikeGLX: {} failed: {}".format(what, self.Error()))

	def Flag(self, fn, what):
		from ctypes import byref, c_bool
		v = c_bool()
		self.Ok(fn(byref(v), self.h), what)
		return bool(v.value)

	def Running(self):
		return self.Flag(self.sglx.c_sglx_isRunning, "isRunning")

	def Saving(self):
		return self.Flag(self.sglx.c_sglx_isSaving, "isSaving")

	def DataDir(self):
		from ctypes import byref, c_char_p
		d = c_char_p()
		self.Ok(self.sglx.c_sglx_getDataDir(byref(d), self.h, 0), "getDataDir")
		return d.value.decode(errors="replace") if d.value else ""

	def Text(self, fn, what):
		from ctypes import byref, c_char_p
		d = c_char_p()
		self.Ok(fn(byref(d), self.h), what)
		return d.value.decode(errors="replace") if d.value else ""

	def Selected(self):
		"""
		SpikeGLX's devices as last verified: probes {(slot, port, dock)} (its
		getProbeAddrs, "(probeID,slot,port,dock)...") and OneBoxes {slot}
		(getOneBoxAddrs, "(OneBoxID,slot)..."); None when its answers can't be read.
		"""
		try:
			probes = {tuple(int(v) for v in e[1:4]) for e in Entries(self.Text(self.sglx.c_sglx_getProbeAddrs,
				"getProbeAddrs"))}
			boxes = {int(e[1]) for e in Entries(self.Text(self.sglx.c_sglx_getOneBoxAddrs, "getOneBoxAddrs"))}
		except (SpikeGLXError, AttributeError, ValueError, IndexError):
			return None # an SDK without these calls, or answers in another form: SpikeGLX's devices left as they are
		return probes, boxes

	def Differs(self, devices):
		"""SpikeGLX's devices as a string when they aren't devices' probes and OneBoxes, else None."""
		wanted = Wanted(devices)
		now = self.Selected() if wanted is not None else None
		if wanted is None or now is None or now == wanted:
			return None
		return Shown(*now)

	def Select(self, devices, say=print):
		"""Detect and verify devices, as Configure > Detect > Verify | Save does."""
		# Verify refuses a run name already used in the data folder (the last session's): a fresh pair first
		fresh = os.path.join(tempfile.gettempdir(), "sglx_verify")
		os.makedirs(fresh, exist_ok=True)
		if not self.sglx.c_sglx_setDataDir(self.h, 0, fresh.encode()):
			say("SpikeGLX: setDataDir before verifying: {} (going on)".format(self.Error()))
		if not self.sglx.c_sglx_setRunName(self.h, "verify_{}".format(int(time.time())).encode()):
			say("SpikeGLX: setRunName before verifying: {} (going on)".format(self.Error()))
		self.Ok(self.sglx.c_sglx_selectDevices(self.h, devices.encode(), 1), "selectDevices {}".format(devices))

	def Samples(self, js, ip):
		return self.sglx.c_sglx_getStreamSampleRate(self.h, js, ip), self.sglx.c_sglx_getStreamSampleCount(self.h, js, ip)

	def Params(self, devices="", say=print, select=False):
		"""
		SpikeGLX's run parameters {key: value}. Freshly opened (never verified),
		devices are detected and verified first; with select, also when SpikeGLX's
		probes and OneBoxes aren't devices'.
		"""
		from ctypes import byref, c_int
		n = c_int()
		if not self.sglx.c_sglx_getParams(byref(n), self.h):
			if "never validated" not in self.Error():
				self.Ok(False, "getParams")
			if not devices:
				raise SpikeGLXError("SpikeGLX's settings haven't been validated since it opened: in SpikeGLX, Configure > "
					"Detect > Verify | Save (or give spikeglx.devices in the config, to do it from here).")
			say("SpikeGLX: its settings haven't been validated since it opened: detecting {} and verifying.".format(devices))
			self.Select(devices, say)
			self.Ok(self.sglx.c_sglx_getParams(byref(n), self.h), "getParams")
		elif select and devices:
			now = self.Differs(devices)
			if now is not None:
				say("SpikeGLX: its devices are {}, the config's {} (spikeglx.devices): detecting those and verifying."
					.format(now, devices))
				self.Select(devices, say)
				self.Ok(self.sglx.c_sglx_getParams(byref(n), self.h), "getParams")
				after = self.Differs(devices)
				if after is not None:
					raise SpikeGLXError("SpikeGLX: after selecting {} (spikeglx.devices), its devices are {}: in SpikeGLX, "
						"Configure > Devices, tick those and Detect > Verify | Save.".format(devices, after))
		out = {}
		for i in range(n.value):
			length = c_int()
			k, _, v = self.sglx.c_sglx_getstr(byref(length), self.h, i).decode(errors="replace").partition("=")
			out[k.strip()] = v.strip()
		return out


class SpikeGLX:
	"""The spikeglx: section, checked, and what campy-session asks of SpikeGLX."""

	def __init__(self, section, say=print):
		self.section, self.say = section, say
		if not section["sdk"]:
			raise SpikeGLXError("spikeglx.sdk: give the folder that holds SpikeGLX's SDK (sglx_pkg).")
		stream = section["stream"]
		if len(stream) != 2 or not all(isinstance(v, int) and not isinstance(v, bool) for v in stream):
			raise SpikeGLXError("spikeglx.stream: [js, ip], two whole numbers ([1, 0]: the first OneBox); not {!r}."
				.format(stream))
		Match(section) # the card's, checked with the rest
		self.lock = threading.Lock() # one connection at a time: the panel's status, and the session's steps

	def Connect(self):
		return Connection(Sdk(self.section["sdk"]), self.section["host"], self.section["port"])

	def Answers(self):
		try:
			with self.lock, self.Connect():
				return True
		except SpikeGLXError:
			return False

	def Check(self, conn=None, select=False):
		"""
		SpikeGLX answers, runs no run, and its trigger is TTL: a line saying so.
		SpikeGLXError otherwise. With select (at Start), devices that aren't the
		config's are selected; without, the line says they will be.
		"""
		if conn is None:
			with self.lock, self.Connect() as conn:
				return self.Check(conn, select)
		if conn.Running():
			raise SpikeGLXError("SpikeGLX is running a run already: stop it in SpikeGLX (campy-session starts its own, "
				"into the recording's folder).")
		devices = self.section["devices"]
		p = conn.Params(devices, self.say, select)
		if p.get("trigMode") != TTL_TRIGGER:
			raise SpikeGLXError("SpikeGLX's trigger isn't TTL (trigMode={}): it would record all the time, not only while "
				"the gate is high. Set its trigger to TTL in SpikeGLX and check again.".format(p.get("trigMode")))
		self.margin = float(p.get("trgTTLMarginS") or 1)
		line = "SpikeGLX: idle, TTL trigger (stream {}, bit {}, margin {} s, {})".format(p.get("trgTTLStream"),
			p.get("trgTTLBit"), p.get("trgTTLMarginS"), p.get("trgTTLMode"))
		now = conn.Differs(devices) if devices else None
		if now is not None:
			line += "; its devices are {}: Start selects the config's, {}".format(now, devices)
		elif devices and Wanted(devices) is not None:
			line += "; devices {}".format(devices)
		return line

	def Open(self):
		"""Open SpikeGLX (spikeglx.exe) if it doesn't answer, and wait until it does: True when it answers."""
		if self.Answers():
			return True
		exe = self.section["exe"]
		if ProgramRunning(exe):
			raise SpikeGLXError("SpikeGLX is open, but its command server doesn't answer: in SpikeGLX, Options > Command "
				"Server Settings > Enable (127.0.0.1, port {}); or close SpikeGLX, and it is opened at Start.".format(
				self.section["port"]))
		if not exe:
			raise SpikeGLXError("SpikeGLX doesn't answer: open it, with its command server on (or give spikeglx.exe, to "
				"have it opened from here).")
		if not os.path.isfile(exe):
			raise SpikeGLXError("SpikeGLX doesn't answer, and {} isn't there to open it (spikeglx.exe).".format(exe))
		self.say("Opening SpikeGLX ({}).".format(exe))
		Launch(exe)
		deadline = time.monotonic() + self.section["open_timeout_s"]
		while time.monotonic() < deadline:
			time.sleep(3)
			if self.Answers():
				return True
		raise SpikeGLXError("SpikeGLX was opened, but didn't answer within {:g} s (its command server on?).".format(
			self.section["open_timeout_s"]))

	def Start(self, folder, run):
		"""Its run, into folder (made if missing), named run; once it acquires, recording enabled."""
		js, ip = self.section["stream"]
		with self.lock, self.Connect() as conn:
			self.Check(conn, select=True)
			os.makedirs(folder, exist_ok=True)
			conn.Ok(conn.sglx.c_sglx_setDataDir(conn.h, 0, os.path.abspath(folder).encode()), "setDataDir")
			conn.Ok(conn.sglx.c_sglx_setRunName(conn.h, run.encode()), "setRunName")
			conn.Ok(conn.sglx.c_sglx_startRun(conn.h, run.encode()), "startRun")
			deadline = time.monotonic() + self.section["start_timeout_s"]
			while True:
				if time.monotonic() > deadline:
					raise SpikeGLXError("SpikeGLX's run didn't start acquiring within {:g} s ({}).".format(
						self.section["start_timeout_s"], conn.Error()))
				if conn.Running():
					rate, count = conn.Samples(js, ip)
					if rate > 0 and count > rate * (self.margin + 2.0):
						break
				time.sleep(0.5)
			conn.Ok(conn.sglx.c_sglx_setRecordingEnable(conn.h, True), "setRecordingEnable")
			time.sleep(1.0)
			return "SpikeGLX: run {} acquiring ({:.0f} samples on stream {},{}), recording enabled, into {}.".format(run,
				conn.Samples(js, ip)[1], js, ip, conn.DataDir())

	def Stop(self):
		"""Stop its run (its files close): a line saying what happened."""
		with self.lock, self.Connect() as conn:
			if not conn.Running():
				return "SpikeGLX: no run to stop."
			conn.Ok(conn.sglx.c_sglx_stopRun(conn.h), "stopRun")
			return "SpikeGLX: run stopped, its files closed."

	def Status(self):
		"""{answers, running, saving, folder, samples}, for the page, or None while a step uses SpikeGLX; never raises."""
		js, ip = self.section["stream"]
		if not self.lock.acquire(timeout=0.5):
			return None # a step is talking to SpikeGLX (its run starting: up to 2 minutes): the last status stands
		try:
			with self.Connect() as conn:
				running = conn.Running()
				return {"answers": True, "running": running, "saving": conn.Saving() if running else False,
					"folder": conn.DataDir(), "samples": conn.Samples(js, ip)[1] if running else None}
		except Exception as e:
			return {"answers": False, "error": str(e)}
		finally:
			self.lock.release()


def ProgramRunning(exe):
	"""True when SpikeGLX's program (exe's name, else any SpikeGLX*) is running on this computer; False elsewhere than
	Windows, or when that can't be told."""
	if sys.platform != "win32":
		return False
	name = os.path.basename(exe) if exe else "SpikeGLX*"
	try:
		out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq {}".format(name), "/FO", "CSV", "/NH"],
			capture_output=True, text=True, timeout=30)
	except (OSError, subprocess.TimeoutExpired):
		return False
	return "spikeglx" in out.stdout.lower()


def Files(folder):
	"""How many .bin files SpikeGLX has written in folder (each gate opening writes one per stream: probe, OneBox)."""
	return len(glob.glob(os.path.join(folder, "**", "*.bin"), recursive=True))


def Openings(folder):
	"""How many times SpikeGLX opened files in folder: its files' run_g<g>_t<t> trigger indices, each counted once."""
	import re
	found = set()
	for path in glob.glob(os.path.join(folder, "**", "*.bin"), recursive=True):
		m = re.search(r"_g(\d+)_t(\d+)\.", os.path.basename(path))
		found.add(m.groups() if m else path)
	return len(found)


def Launch(exe):
	"""
	SpikeGLX, opened as if by hand. On Windows through a shortcut whose
	Start-in is its own folder (where it finds its settings, _Configs: opened
	from elsewhere it came up with default settings) and through Explorer, so
	that it runs outside this program's job (the rig's lock): if campy-session
	ends, SpikeGLX keeps recording what the gate opens.
	"""
	folder = os.path.dirname(os.path.abspath(exe))
	if sys.platform == "win32":
		lnk = os.path.join(tempfile.gettempdir(), "campy-SpikeGLX.lnk")
		script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:CAMPY_LNK); $s.TargetPath = "
			"$env:CAMPY_EXE; $s.WorkingDirectory = $env:CAMPY_DIR; $s.Save()")
		subprocess.run(["powershell", "-NoProfile", "-Command", script], env=dict(os.environ, CAMPY_LNK=lnk,
			CAMPY_EXE=os.path.abspath(exe), CAMPY_DIR=folder), capture_output=True, timeout=60)
		subprocess.Popen(["explorer.exe", lnk])
	else:
		subprocess.Popen([exe], cwd=folder, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL, start_new_session=True)
