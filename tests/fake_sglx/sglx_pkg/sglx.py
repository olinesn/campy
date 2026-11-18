"""
A stand-in for SpikeGLX's SDK module (sglx_pkg.sglx, SpikeGLX-CPP-SDK), for
campy's tests (tests/test_devices.py): the c_sglx_* calls campy/spikeglx.py
makes, answered from a JSON file (the environment's FAKE_SGLX_STATE), which
the tests read and change. Its SpikeGLX:

  open        answers on its command server (else connect fails)
  validated   its settings verified (else getParams fails "never validated"
              until selectDevices)
  trigMode    "2": TTL
  devices     the devices it last verified ("(20,1,1)(20,obx)": a probe and a
              OneBox in slot 20), which getProbeAddrs and getOneBoxAddrs give
              and selectDevices sets; no_addrs: those two calls fail (an SDK
              without them); stuck: selectDevices leaves devices as they are
  running     a run started (startRun), its OneBox stream at 30,303 samples a
              second from then; dir, run: its data folder and run name
  enabled     recording enabled; the tests' stand-in Pico writes a .bin file
              into dir at each rise of the gate while it is
  ap_npy      a probe's AP samples (.npy, int16 [samples, channels]), for
              campy/ephyslive.py: its stream (js 2, ip 0) at 30,000 samples
              a second from the run's start, the file over and over; fetch
              gives the last BUFFER_S seconds of it
"""

import ctypes, json, os, time

import numpy as np

RATE = 30303.0
IMEC_RATE = 30000.0
UV = 0.62 / 8192 / 80 # volts per count: a Neuropixels 2.0 probe's
BUFFER_S = 4.0
DEFAULT = {"open": True, "validated": True, "trigMode": "2", "running": False, "enabled": False, "dir": "", "run": "",
	"t_run": None, "calls": [], "devices": "(20,1,1)(20,obx)", "no_addrs": False, "stuck": False}


def Path():
	return os.environ["FAKE_SGLX_STATE"]


def Load():
	try:
		with open(Path()) as f:
			return dict(DEFAULT, **json.load(f))
	except (OSError, ValueError):
		return dict(DEFAULT)


def Save(state):
	with open(Path() + ".tmp", "w") as f:
		json.dump(state, f)
	os.replace(Path() + ".tmp", Path())


def Call(name, **changes):
	state = Load()
	state["calls"].append(name)
	state.update(changes)
	Save(state)
	return state


class Handle:
	error = b""


def Set(ref, value):
	ref._obj.value = value


def c_sglx_createHandle():
	return Handle()


def c_sglx_destroyHandle(h):
	pass


def c_sglx_connect(h, host, port):
	if not Load()["open"]:
		h.error = b"connection refused"
		return False
	return True


def c_sglx_close(h):
	pass


def c_sglx_getError(h):
	return h.error


def c_sglx_isRunning(ref, h):
	Set(ref, Load()["running"])
	return True


def c_sglx_isSaving(ref, h):
	Set(ref, False)
	return True


def c_sglx_getDataDir(ref, h, i):
	Set(ref, Load()["dir"].encode())
	return True


def c_sglx_getParams(ref, h):
	state = Load()
	if not state["validated"]:
		h.error = b"getParams: parameters never validated"
		return False
	h.params = ["trigMode={}".format(state["trigMode"]), "trgTTLStream=obx0", "trgTTLBit=0", "trgTTLMarginS=1",
		"trgTTLMode=1"]
	Set(ref, len(h.params))
	return True


def c_sglx_getstr(ref, h, i):
	return h.params[i].encode()


def c_sglx_selectDevices(h, devices, verify):
	stuck = Load()["stuck"]
	Call("selectDevices " + devices.decode(), validated=True, **({} if stuck else {"devices": devices.decode()}))
	return True


def Entries():
	import re
	return [e.split(",") for e in re.findall(r"\(([^()]*)\)", Load()["devices"])]


def c_sglx_getProbeAddrs(ref, h):
	if Load()["no_addrs"]:
		h.error = b"getProbeAddrs: unknown command"
		return False
	probes = [e for e in Entries() if len(e) == 3]
	Set(ref, ("".join("({},{})".format(i, ",".join(e)) for i, e in enumerate(probes)) or "()").encode())
	return True


def c_sglx_getOneBoxAddrs(ref, h):
	if Load()["no_addrs"]:
		h.error = b"getOneBoxAddrs: unknown command"
		return False
	boxes = [e for e in Entries() if len(e) == 2 and e[1] == "obx"]
	Set(ref, ("".join("({},{})".format(i, e[0]) for i, e in enumerate(boxes)) or "()").encode())
	return True


def c_sglx_setDataDir(h, i, folder):
	Call("setDataDir", dir=folder.decode())
	return True


def c_sglx_setRunName(h, name):
	Call("setRunName", run=name.decode())
	return True


def c_sglx_startRun(h, name):
	Call("startRun", running=True, t_run=time.time(), enabled=False)
	return True


def Rate(js):
	return IMEC_RATE if abs(js) == 2 else RATE


def c_sglx_getStreamSampleRate(h, js, ip):
	return Rate(js) if Load()["running"] else 0.0


def c_sglx_getStreamSampleCount(h, js, ip):
	state = Load()
	return int(Rate(js) * (time.time() - state["t_run"])) if state["running"] and state["t_run"] else 0


def Ap(h):
	if getattr(h, "ap", None) is None:
		h.ap = np.load(Load()["ap_npy"], mmap_mode="r")
	return h.ap


def c_sglx_getStreamAcqChans(ref, h, js, ip):
	h.ints = [Ap(h).shape[1], 0, 1] if abs(js) == 2 else [12, 1, 1]
	Set(ref, len(h.ints))
	return True


def c_sglx_getint(h, i):
	return h.ints[i]


def c_sglx_getStreamI16ToVolts(ref, h, js, ip, chan):
	Set(ref, UV)
	return True


def c_sglx_getGeomMap(ref, h, ip):
	n = Ap(h).shape[1]
	h.params = ["head_partNumber=NP2000", "head_numShanks=1", "head_shankPitch=0", "head_shankWidth=70"]
	for i in range(n):
		h.params += ["ch{}_s=0".format(i), "ch{}_x={}".format(i, 27 + 32 * (i % 2)), "ch{}_z={}".format(i, 15 * (i // 2)),
			"ch{}_u=1".format(i)]
	h.params.sort() # as SpikeGLX gives them: in ascending alphanumeric order
	Set(ref, len(h.params))
	return True


def c_sglx_fetch(data_ref, n_ref, h, js, ip, start, max_samps, channels, n_cs, downsample):
	state = Load()
	state["calls"].append("fetch")
	Save(state)
	if not state["running"]:
		h.error = b"fetch: not running"
		return 0
	count = c_sglx_getStreamSampleCount(h, js, ip)
	start = max(int(start), count - int(BUFFER_S * IMEC_RATE)) # older samples are gone
	end = min(count, start + max_samps)
	if end <= start:
		Set(n_ref, 0)
		return start
	ap = Ap(h)
	rows = np.arange(start, end) % len(ap)
	h.block = np.ascontiguousarray(ap[rows][:, list(channels[:n_cs])], np.int16).ravel()
	data_ref._obj.contents = ctypes.c_short.from_buffer(h.block)
	Set(n_ref, len(h.block))
	return start


def c_sglx_setRecordingEnable(h, enable):
	Call("setRecordingEnable", enabled=bool(enable))
	return True


def c_sglx_stopRun(h):
	Call("stopRun", running=False, enabled=False)
	return True
