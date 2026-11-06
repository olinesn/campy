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
"""

import json, os, time

RATE = 30303.0
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


def c_sglx_getStreamSampleRate(h, js, ip):
	return RATE if Load()["running"] else 0.0


def c_sglx_getStreamSampleCount(h, js, ip):
	state = Load()
	return int(RATE * (time.time() - state["t_run"])) if state["running"] and state["t_run"] else 0


def c_sglx_setRecordingEnable(h, enable):
	Call("setRecordingEnable", enabled=bool(enable))
	return True


def c_sglx_stopRun(h):
	Call("stopRun", running=False, enabled=False)
	return True
