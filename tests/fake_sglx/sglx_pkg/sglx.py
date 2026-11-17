"""
A stand-in for SpikeGLX's SDK module (sglx_pkg.sglx, SpikeGLX-CPP-SDK), for
campy's tests (tests/test_devices.py): the c_sglx_* calls campy/spikeglx.py
makes, answered from a JSON file (the environment's FAKE_SGLX_STATE), which
the tests read and change. Its SpikeGLX:

  open        answers on its command server (else connect fails)
  validated   its settings verified (else getParams fails "never validated"
              until selectDevices)
  trigMode    "2": TTL
  running     a run started (startRun), its OneBox stream at 30,303 samples a
              second from then; dir, run: its data folder and run name
  enabled     recording enabled; the tests' stand-in Pico writes a .bin file
              into dir at each rise of the gate while it is
  ap_npy      a probe's AP samples (.npy, int16 [samples, channels]), for
              campy/ephyslive.py: its stream (js 2, ip 0) at 30,000 samples
              a second from the run's start, the file over and over; fetch
              gives the last BUFFER_S seconds of it
  camera      the OneBox's camera pulses (js 1: XA0-11, XD0, SY0, at 30,303 a
              second), on XD0's bit "bit": {"fps": 60, "bit": 2, "bouts":
              [[start s from the run's start, pulses], ...]}, each pulse 3 ms
              from the sample at or after its time; mapSample: the same
              moment on another stream, both counted from the run's start
"""

import ctypes, json, os, time

import numpy as np

RATE = 30303.0
IMEC_RATE = 30000.0
UV = 0.62 / 8192 / 80 # volts per count: a Neuropixels 2.0 probe's
BUFFER_S = 4.0
DEFAULT = {"open": True, "validated": True, "trigMode": "2", "running": False, "enabled": False, "dir": "", "run": "",
	"t_run": None, "calls": []}


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
	Call("selectDevices " + devices.decode(), validated=True)
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


def CameraWord(state, samples):
	"""XD0 at these OneBox samples: the camera bit high for 3 ms from each pulse's first sample."""
	cam = state.get("camera") or {}
	word = np.zeros(len(samples), np.int16)
	fps, bit = float(cam.get("fps", 60)), int(cam.get("bit", 2))
	for start, n in cam.get("bouts", []):
		rises = np.ceil((start + np.arange(int(n)) / fps) * RATE).astype(np.int64)
		k = np.searchsorted(rises, samples, side="right") - 1
		on = (k >= 0) & (samples - rises[np.maximum(k, 0)] < int(0.003 * RATE))
		word[on] |= 1 << bit
	return word


def c_sglx_mapSample(h, dst_js, dst_ip, src_sample, src_js, src_ip):
	return int(round(src_sample * Rate(dst_js) / Rate(src_js)))


def c_sglx_fetch(data_ref, n_ref, h, js, ip, start, max_samps, channels, n_cs, downsample):
	state = Load()
	state["calls"].append("fetch")
	Save(state)
	if abs(js) == 1 and state["running"]: # the OneBox: its XD0 (channel 12) alone
		count = c_sglx_getStreamSampleCount(h, js, ip)
		start = max(int(start), count - int(BUFFER_S * RATE))
		end = min(count, start + max_samps)
		if end <= start:
			Set(n_ref, 0)
			return start
		samples = np.arange(start, end)
		cols = [CameraWord(state, samples) if c == 12 else np.zeros(len(samples), np.int16) for c in channels[:n_cs]]
		h.block = np.ascontiguousarray(np.stack(cols, axis=1), np.int16).ravel()
		data_ref._obj.contents = ctypes.c_short.from_buffer(h.block)
		Set(n_ref, len(h.block))
		return start
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
