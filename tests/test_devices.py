"""
The devices around a recording (campy/devices.py, pico.py, spikeglx.py):
the Pico's plan and campy's recTimeInSec, the config's sections, SpikeGLX
through its SDK, and whole ChronicRecord sessions from the panel's page, with
emulated cameras, a stand-in Pico on a socket (tests/test_chronic_control.py's
FakeBoard, which here also opens a SpikeGLX file at each rise of the gate) and
a stand-in SpikeGLX SDK (tests/fake_sglx).
"""

import json, os, re, signal, subprocess, sys, threading, time

import pytest
import yaml

from campy import devices, pico, session, spikeglx
from test_chronic_control import FakeBoard
from test_panel import call, wait
from test_session import emu_session, home, the_folder, write_config # noqa: F401 (home: a fixture)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_SDK = os.path.join(REPO, "tests", "fake_sglx")
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="the panel's tests run on Linux and macOS")

# A short session at 20 frames a second: 2 bouts, 6 s apart; the first 3 s of camera, the second 1 s: 80 pulses
SHORT = {"TOTAL_BASELINE": 1, "CAMERA_LEAD_IN": 0.5, "STANDARD_STIM_DURATION": 1, "EXTENDED_STIM_DURATION": 3,
	"INTERVAL": 6, "CAMERA_FPS": 20}


def section(name, given):
	return session.Section(name, given, session.SECTIONS[name])


def a_pico(fps=60, **given):
	return pico.Pico(section("pico", given), fps=fps)


# ------------------------------------------------------------------ the plan

def test_the_plan_gives_campys_recording_time():
	# As chronicrecord/README.md's table: recTimeInSec = camera pulses / 60
	p = a_pico()
	six = p.Plan(518400)
	assert (six["bouts"], six["pulses"], six["camera_s"]) == (1728, 5011200, 83520)
	assert (p.Plan(3600)["pulses"], p.Plan(3600)["camera_s"]) == (34800, 580)
	gate = p.Plan(900)
	assert (gate["bouts"], gate["pulses"], gate["camera_s"]) == (3, 15600, 260)
	assert gate["text"] == "3 bouts over 15 min, 15,600 camera pulses (4 min 20 s of camera)"
	assert a_pico(fps=20, set=SHORT).Plan(12)["camera_s"] == 4


def test_the_picos_rate_must_be_campys():
	with pytest.raises(pico.PicoError, match="CAMERA_FPS"):
		a_pico(fps=30)
	assert a_pico(fps=30, set={"CAMERA_FPS": 30}).Plan(3600)["pulses"] == 17400


def test_lengths_and_settings_are_checked():
	with pytest.raises(pico.PicoError, match="pico.lengths"):
		a_pico(lengths=[{"name": "x", "seconds": "900"}])
	with pytest.raises(pico.PicoError, match="TOTAL_DURATION"):
		a_pico(set={"TOTAL_DURATION": 900})
	with pytest.raises(pico.PicoError, match="NOPE"):
		a_pico(set={"NOPE": 1})
	p = a_pico(lengths=[{"seconds": 900, "videoFolder": "D:/gate"}, {"name": "6 days", "seconds": 518400}])
	assert [(l["name"], l["videoFolder"]) for l in p.lengths] == [("15 min", "D:/gate"), ("6 days", None)]


def test_a_clock_time_for_the_picos_start():
	at = devices.When("7:20 PM")
	assert time.localtime(at)[3:5] == (19, 20) and 0 < at - time.time() <= 86400
	assert time.localtime(devices.When("07:05"))[3:5] == (7, 5)
	with pytest.raises(session.SessionError):
		devices.When("soon")


# ------------------------------------------------------------------ the config

def test_the_device_sections_are_read_and_checked(tmp_path):
	config = write_config(tmp_path / "c.yaml", {"numCams": 2, "videoFolder": "x", "frameRate": 60},
		pico={"port": "E665485457699D30", "lengths": [{"name": "6 days", "seconds": 518400}]},
		spikeglx={"sdk": FAKE_SDK, "run_prefix": "chronic_"}, windows={"usb_suspend": False})
	c = session.Config(str(config))
	assert c.pico["port"] == "E665485457699D30" and c.pico["gate_check"] is True and c.pico["last_gate_s"] == 120
	assert c.spikeglx["port"] == 4142 and c.spikeglx["stream"] == [1, 0]
	assert c.windows == {"update_paused": True, "usb_suspend": False}
	assert c.live is None
	bad = write_config(tmp_path / "bad.yaml", {"numCams": 2}, pico={"prot": "COM3"})
	with pytest.raises(session.SessionError, match="pico.prot: campy-session has no such key. Did you mean \"port\""):
		session.Config(str(bad))
	# campy-session run leaves a Pico to the panel, which asks before starting and stopping it
	out = subprocess.run([sys.executable, "-m", "campy.session", "run", str(config)], capture_output=True, text=True,
		env=dict(os.environ, PYTHONPATH=REPO), timeout=60)
	assert out.returncode == 1 and "record it from the panel" in out.stderr


def test_a_profiles_stop_file_gives_way_to_campy_sessions(tmp_path):
	# chronic_pico.yaml's stopFile is for its PowerShell launcher; a panel config extending it gets campy-session's own
	profile = write_config(tmp_path / "profile.yaml", {"numCams": 2, "videoFolder": "x", "stopFile": "C:/x.stop"})
	config = tmp_path / "c.yaml"
	config.write_text("extends: {}\nframeRate: 60\n".format(profile.as_posix()))
	c = session.Config(str(config))
	assert c.replaced and "C:/x.stop" in c.replaced[0]
	own = write_config(tmp_path / "own.yaml", {"numCams": 2, "stopFile": "C:/x.stop"})
	with pytest.raises(session.SessionError, match="stopFile: campy-session sets it itself"):
		session.Config(str(own))


# ------------------------------------------------------------------ SpikeGLX, through the SDK

@pytest.fixture
def sglx_state(tmp_path, monkeypatch):
	path = tmp_path / "sglx.json"
	monkeypatch.setenv("FAKE_SGLX_STATE", str(path))
	path.write_text(json.dumps({}))
	return path


def state(path):
	return json.loads(path.read_text())


def set_state(path, **changes):
	s = state(path)
	s.update(changes)
	path.write_text(json.dumps(s))


def a_sglx(**given):
	return spikeglx.SpikeGLX(section("spikeglx", dict({"sdk": FAKE_SDK, "start_timeout_s": 20}, **given)),
		say=lambda text: None)


def test_spikeglx_is_checked_started_and_stopped(tmp_path, sglx_state):
	s = a_sglx()
	assert s.Check().startswith("SpikeGLX: idle, TTL trigger")
	set_state(sglx_state, trigMode="0")
	with pytest.raises(spikeglx.SpikeGLXError, match="isn't TTL"):
		s.Check()
	set_state(sglx_state, trigMode="2", validated=False)
	with pytest.raises(spikeglx.SpikeGLXError, match="Detect > Verify"):
		s.Check() # no devices to verify with
	a_sglx(devices="(20,1,1)(20,obx)").Check()
	assert "selectDevices (20,1,1)(20,obx)" in state(sglx_state)["calls"]
	said = s.Start(str(tmp_path / "rec" / "spikeglx"), "chronic_2026-11-06_09-14-02")
	now = state(sglx_state)
	assert now["running"] and now["enabled"] and now["run"] == "chronic_2026-11-06_09-14-02"
	assert now["dir"] == str(tmp_path / "rec" / "spikeglx") and (tmp_path / "rec" / "spikeglx").is_dir()
	# Recording enabled only once the stream has more than the trigger's margin plus 2 s of samples
	assert time.time() - now["t_run"] > 3.0 and "recording enabled" in said
	with pytest.raises(spikeglx.SpikeGLXError, match="running a run already"):
		s.Check()
	assert s.Status()["running"] is True
	assert s.Stop() == "SpikeGLX: run stopped, its files closed." and not state(sglx_state)["running"]
	assert s.Stop() == "SpikeGLX: no run to stop."
	set_state(sglx_state, open=False)
	assert s.Status()["answers"] is False
	with pytest.raises(spikeglx.SpikeGLXError, match="doesn't answer"):
		s.Open() # and no spikeglx.exe to open it with


# ------------------------------------------------------------------ whole sessions from the page

class GateBoard(FakeBoard):
	"""
	The stand-in Pico, which also reports each edge as it happens (a wiring check waits for them), and opens a
	SpikeGLX file at each rise of its gate, while SpikeGLX records.
	"""

	def __init__(self, sglx_path):
		self.sglx_path, self.gates, self.opened = str(sglx_path), [], 0
		super().__init__()

	def Command(self, words):
		if words[0] in ("pin", "clear", "edge", "train", "go") and self.t0 is not None:
			self.Say("err running: stop first") # as the sketch: nothing loads while a session runs
			return
		if words[0] == "end" and self.t0 is not None:
			# As the sketch: the session ends then, later than its last edge too (a wiring check's round and its pause)
			self.end = max(self.Now(), int(words[1]))
			self.Say("ok end {}".format(self.end))
			return
		super().Command(words)
		if words[0] == "go":
			self.gates = sorted(self.edges)

	def Clock(self):
		while not self.closed:
			with self.lock:
				if self.t0 is not None:
					now = self.Now()
					while self.gates and self.gates[0][0] <= now:
						t, ch, level = self.gates.pop(0)
						self.Say("ev {} {} {} 0".format(t, ch, level)) # each edge, as the sketch reports it
						if ch == 0 and level == 1:
							self.Gate()
					if now >= self.end:
						self.Say("done {} pulses={} late_max=0 lost=0 stalls=0".format(self.end, self.Pulses(self.end)))
						self.t0 = None
			time.sleep(0.01)

	def Gate(self):
		with open(self.sglx_path) as f:
			s = json.load(f)
		if s.get("running") and s.get("enabled") and not s.get("no_files"):
			self.opened += 1
			with open(os.path.join(s["dir"], "{}_g0_t{}.obx0.obx.bin".format(s["run"], self.opened - 1)), "wb") as f:
				f.write(b"\0" * 16)


@pytest.fixture
def rig(tmp_path, sglx_state):
	board = GateBoard(sglx_state)
	yield board
	board.close()


def chronic_config(tmp_path, home, board, lengths=None, **pico_given):
	config, clips = emu_session(tmp_path, home, 1, feed="campy-test-devices", post={"remux": False, "export": False})
	text = config.read_text()
	text += yaml.safe_dump({"pico": dict({"port": board.url, "state_dir": str(tmp_path / "pico-state"),
		"lengths": lengths or [{"name": "Short", "seconds": 12}], "set": SHORT, "last_gate_s": 30}, **pico_given),
		"spikeglx": {"sdk": FAKE_SDK, "run_prefix": "chronic_", "start_timeout_s": 30}}, sort_keys=False)
	config.write_text(text)
	return config, clips


def start_panel(tmp_path, home, config, **more_env):
	env = dict(os.environ, PYTHONPATH=REPO, PYTHONUNBUFFERED="1", HOME=str(home), USERPROFILE=str(home),
		CAMPY_STATUS_DIR=str(home / "status"), **more_env)
	env.pop("IMAGEIO_FFMPEG_EXE", None)
	env.pop("CAMPY_LIVE_PYTHON", None)
	proc = subprocess.Popen([sys.executable, "-m", "campy.session", "panel", str(config), "--no-browser", "--port",
		str(18800 + os.getpid() % 1000)], cwd=str(tmp_path), env=env, stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT, text=True, start_new_session=True)
	lines = []
	threading.Thread(target=lambda: lines.extend(proc.stdout), daemon=True).start()
	t0 = time.monotonic()
	while time.monotonic() - t0 < 60:
		found = [m for m in (re.search(r"The panel: (http://\S+)", line) for line in list(lines)) if m]
		if found:
			return proc, lines, found[0].group(1)
		assert proc.poll() is None, "".join(lines)
		time.sleep(0.1)
	raise AssertionError("no panel address: " + "".join(lines))


def close_panel(proc, url, lines):
	# After a failed test too: whatever records is stopped (campy-acquire runs apart from the panel), then the panel closed
	if proc.poll() is None:
		try:
			if call(url, "/status.json")[1]["state"] != "ready":
				call(url, "/stop", {"mode": "now"})
				wait(url, lambda s: s["state"] == "ready", timeout=90)
		except Exception:
			pass
		call(url, "/close", {})
	try:
		proc.wait(timeout=30)
	except subprocess.TimeoutExpired:
		os.killpg(proc.pid, signal.SIGKILL)
		raise AssertionError("the panel didn't close: " + "".join(lines))


def steps(status):
	return {s["name"]: s["state"] for s in (status["devices"] or {}).get("steps") or []}


def recording_with_cameras_ready(url):
	return wait(url, lambda s: s["state"] == "recording" and s["devices"] and not s["devices"]["can_start_pico"],
		timeout=90)


@POSIX
def test_a_bout_session_from_the_page(tmp_path, home, rig, sglx_state):
	config, clips = chronic_config(tmp_path, home, rig)
	# The cameras pause 6 s after their first frames (as if waiting for the Pico's pulses), so that campy's 4 s of
	# frames outlast the press of Start the Pico
	proc, lines, url = start_panel(tmp_path, home, config, FAKE_SGLX_STATE=str(sglx_state), CAMPY_EMU_FAULTS="*:stall@2+6")
	try:
		status = call(url, "/status.json")[1]
		assert status["state"] == "ready" and status["length"] == 12
		assert status["lengths"] == [{"name": "Short", "seconds": 12, "videoFolder": None,
			"text": "2 bouts over 12 s, 80 camera pulses (4 s of camera)"}]
		texts = [c["text"] for c in status["checks"]]
		assert all(c["level"] == "ok" for c in status["checks"]), texts
		assert any(t.startswith("The Pico answers, idle") for t in texts) and any(t.startswith("SpikeGLX: idle") for t in texts)
		assert call(url, "/pico-start", {})[0] == 409 # nothing records

		assert call(url, "/start", {"experiment": "Gate test", "length": 12}) == (200, {"ok": True})
		status = recording_with_cameras_ready(url)
		folder = the_folder(clips)
		assert steps(status) == {"SpikeGLX": "done", "Gate check": "done", "Pico": "waiting"}
		sglx = state(sglx_state)
		assert sglx["running"] and sglx["dir"] == str(folder / "spikeglx") and sglx["run"] == "chronic_" + folder.name
		assert len(list((folder / "spikeglx").glob("*.bin"))) == 1 # the gate check's blink
		assert status["launcher"]["recording"]["planned_s"] == 4 # recTimeInSec, from the Pico's plan

		assert call(url, "/pico-start", {}) == (200, {"ok": True})
		status = wait(url, lambda s: s["state"] == "ready" and s["launcher"] and s["launcher"]["state"] == "done",
			timeout=90)
		assert steps(status)["Pico"] == "done" and steps(status)["End"] == "done"
		assert status["launcher"]["parts"]["acquire"]["exit_code"] == 0
		# campy ended after its last frame; the Pico's last gate came later, then SpikeGLX's run stopped
		assert not state(sglx_state)["running"] and state(sglx_state)["calls"][-1] == "stopRun"
		assert len(list((folder / "spikeglx").glob("*.bin"))) == 3 # the blink, and one file per bout
		pico_status = json.loads((tmp_path / "pico-state" / "status.json").read_text())
		assert pico_status["state"] == "done" and "pulses=80" in pico_status["done"]
		assert "Session started" in (folder / "pico" / "session.out").read_text()
		assert status["devices"]["pico"]["state"] == "done"
	finally:
		close_panel(proc, url, lines)
	assert proc.returncode == 0, "".join(lines)
	assert "[panel] Closed from the page." in "".join(lines)


@POSIX
def test_stop_after_this_bout(tmp_path, home, rig, sglx_state):
	# A long plan: campy would record for a minute; Stop after this bout lets the Pico finish bout 1, then stops campy
	config, clips = chronic_config(tmp_path, home, rig, lengths=[{"name": "Long", "seconds": 60}])
	proc, lines, url = start_panel(tmp_path, home, config, FAKE_SGLX_STATE=str(sglx_state))
	try:
		assert call(url, "/start", {}) == (200, {"ok": True})
		recording_with_cameras_ready(url)
		assert call(url, "/pico-start", {}) == (200, {"ok": True})
		wait(url, lambda s: (s["devices"]["pico"] or {}).get("alive") and (s["devices"]["pico"] or {}).get("bout") == 1)
		assert call(url, "/stop", {"mode": "sideways"})[0] == 409
		assert call(url, "/stop", {"mode": "bout"}) == (200, {"ok": True})
		status = wait(url, lambda s: s["state"] == "ready" and s["launcher"] and s["launcher"]["state"] in ("done",
			"failed"), timeout=90)
		folder = the_folder(clips)
		pico_status = json.loads((tmp_path / "pico-state" / "status.json").read_text())
		assert pico_status["state"] == "done" and "pulses=60" in pico_status["done"] # bout 1's, all of them
		assert len(list((folder / "spikeglx").glob("*.bin"))) == 2 # the blink and bout 1
		assert not state(sglx_state)["running"]
		assert status["devices"]["stop_mode"] == "bout"
	finally:
		close_panel(proc, url, lines)


@POSIX
def test_no_recording_when_spikeglx_misses_the_gate(tmp_path, home, rig, sglx_state):
	set_state(sglx_state, no_files=True) # the gate's cable unplugged: SpikeGLX opens no file
	config, clips = chronic_config(tmp_path, home, rig)
	proc, lines, url = start_panel(tmp_path, home, config, FAKE_SGLX_STATE=str(sglx_state))
	try:
		assert call(url, "/start", {}) == (200, {"ok": True})
		status = wait(url, lambda s: s["state"] == "ready" and s["launcher"] and s["launcher"]["state"] == "failed",
			timeout=90)
		assert steps(status)["Gate check"] == "failed"
		assert "SpikeGLX opened no file" in status["launcher"]["problems"][-1]["text"]
		assert "acquire" not in status["launcher"]["parts"] # campy never started
		assert not state(sglx_state)["running"] # SpikeGLX's run stopped again
	finally:
		close_panel(proc, url, lines)


@POSIX
def test_campy_failing_leaves_the_pico_and_spikeglx_running(tmp_path, home, rig, sglx_state):
	# A camera lost mid-recording: campy-acquire ends with exit code 1, but the Pico's session (an animal's LEDs) and
	# SpikeGLX's run go on; the page says so, and stops each when asked
	config, clips = chronic_config(tmp_path, home, rig, lengths=[{"name": "Long", "seconds": 60}])
	proc, lines, url = start_panel(tmp_path, home, config, FAKE_SGLX_STATE=str(sglx_state), CAMPY_EMU_FAULTS="1:lost@100")
	try:
		assert call(url, "/start", {}) == (200, {"ok": True})
		recording_with_cameras_ready(url)
		assert call(url, "/pico-start", {}) == (200, {"ok": True})
		status = wait(url, lambda s: s["state"] == "ready" and s["launcher"] and s["launcher"]["state"] == "failed",
			timeout=90)
		assert "still running" in status["devices"]["left_running"]
		status = wait(url, lambda s: s["devices"]["pico"] and s["devices"]["pico"]["alive"])
		assert state(sglx_state)["running"]
		code, said = call(url, "/stop-left", {"what": "spikeglx"})
		assert code == 409 and "stop it first" in said["error"] # the Pico first
		assert call(url, "/stop-left", {"what": "pico"}) == (200, {"ok": True})
		wait(url, lambda s: s["devices"]["pico"] and not s["devices"]["pico"]["alive"])
		assert call(url, "/stop-left", {"what": "spikeglx"}) == (200, {"ok": True})
		assert not state(sglx_state)["running"]
	finally:
		close_panel(proc, url, lines)


def test_spikeglx_open_without_its_command_server_is_not_opened_again(tmp_path, sglx_state, monkeypatch):
	# SpikeGLX running, its command server off: a second SpikeGLX would fight the first for the probe
	set_state(sglx_state, open=False)
	exe = tmp_path / "SpikeGLX_NISIM.exe"
	exe.write_text("")
	launched = []
	monkeypatch.setattr(spikeglx, "Launch", launched.append)
	monkeypatch.setattr(spikeglx, "ProgramRunning", lambda exe: True)
	with pytest.raises(spikeglx.SpikeGLXError, match="command server doesn't answer"):
		a_sglx(exe=str(exe)).Open()
	assert launched == []
