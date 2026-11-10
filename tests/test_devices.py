"""
The devices around a recording (campy/devices.py, pico.py, spikeglx.py):
the Pico's plan and campy's recTimeInSec, the config's sections, SpikeGLX
through its SDK, and whole ChronicRecord sessions from the panel's page, with
emulated cameras, a stand-in Pico on a socket (tests/test_chronic_control.py's
FakeBoard, which here also opens a SpikeGLX file at each rise of the gate) and
a stand-in SpikeGLX SDK (tests/fake_sglx).
"""

import json, os, re, signal, subprocess, sys, threading, time, types

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


def test_the_pages_bout_settings():
	# The page's settings, in its units (ms for the LEDs), over pico.set, for the panel's recordings
	p = a_pico(set={"INTERVAL": 600})
	shown = {f["key"]: f for f in p.PageStatus()["fields"]}
	assert (shown["INTERVAL"]["value"], shown["PULSE_WIDTH_C"]["value"], shown["PULSE_WIDTH_C"]["unit"]) == (600, 30, "ms")
	assert not any(f["changed"] for f in shown.values()) and p.Plan(518400)["bouts"] == 864
	assert p.Choose({"PULSE_WIDTH_C": "50", "PULSE_WIDTH_D": 40, "INTERVAL": 300}) is None
	assert p.page == {"PULSE_WIDTH_C": 0.05, "PULSE_WIDTH_D": 0.04, "INTERVAL": 300}
	assert p.Sets(900) == ["INTERVAL=300", "PULSE_WIDTH_C=0.05", "PULSE_WIDTH_D=0.04", "TOTAL_DURATION=900"]
	assert p.Plan(518400)["bouts"] == 1728
	status = p.PageStatus()
	assert status["changed"] == ["INTERVAL", "PULSE_WIDTH_C", "PULSE_WIDTH_D"]
	assert {f["key"]: f["config"] for f in status["fields"]}["INTERVAL"] == 600
	# What one bout does: the 50 ms blue LED lights frames 300-302, the 40 ms yellow 303-305
	assert "blue LED at 9.999 s for 50 ms (frames 300-302), then yellow for 40 ms (frames 303-305)" in status["bout"]["text"]
	assert "camera off at 245 s and gate closed at 255 s in a long bout, at 15 s and 25 s in a normal one" in \
		status["bout"]["text"]
	# The config's value again, or none: the config's
	assert p.Choose({"INTERVAL": 600, "PULSE_WIDTH_D": ""}) is None and p.page == {"PULSE_WIDTH_C": 0.05}
	# Refused, and nothing changed
	for values, why in (({"LED_LEAD_US": 2}, "not a bout setting"), ({"INTERVAL": "soon"}, "not a number"),
			({"INTERVAL": 0}, "more than 0"), ({"PULSE_WIDTH_C": -30}, "more than 0"),
			({"EXTENDED_EVERY": 2.5}, "whole number"), ({"PULSE_WIDTH_C": 0.0001}, "a microsecond")):
		assert why in p.Choose(dict(values, PULSE_WIDTH_D=99)) and p.page == {"PULSE_WIDTH_C": 0.05}
	# The LEDs' order and the dark between them: 0 is a setting for these two
	assert p.Choose({"YELLOW_FIRST": 1, "LED_GAP": 150, "PULSE_WIDTH_D": 50}) is None
	assert p.page == {"PULSE_WIDTH_C": 0.05, "PULSE_WIDTH_D": 0.05, "YELLOW_FIRST": 1, "LED_GAP": 0.15}
	assert "yellow LED at 9.999 s for 50 ms (frames 300-302), then 150 ms dark, then blue for 50 ms (frames 312-314)" \
		in p.PageStatus()["bout"]["text"]
	assert p.Choose({"YELLOW_FIRST": 0, "LED_GAP": 0, "PULSE_WIDTH_D": ""}) is None and p.page == {"PULSE_WIDTH_C": 0.05}
	for values, why in (({"YELLOW_FIRST": 2}, "1 or 0"), ({"YELLOW_FIRST": 0.5}, "1 or 0"), ({"LED_GAP": -1}, "more than 0")):
		assert why in p.Choose(values) and p.page == {"PULSE_WIDTH_C": 0.05}
	# Settings a bout can't have: each plan says why (and stops the start), as does the bout's description
	assert p.Choose({"CAMERA_LEAD_IN": 20}) is None
	assert "before the gate opens" in p.PageStatus()["bout"]["error"]
	with pytest.raises(pico.PicoError, match="before the gate opens"):
		p.Plan(900)


def test_the_pico_holds_2048_bouts():
	# Refused before planning each bout, rather than by the board once the session starts
	p = a_pico()
	assert p.Plan(2048 * 300)["bouts"] == 2048
	p.Choose({"INTERVAL": 200})
	t0 = time.monotonic()
	with pytest.raises(pico.PicoError, match="2,592 bouts, one every 200 s, and it holds 2,048"):
		p.Plan(518400)
	with pytest.raises(pico.PicoError, match="it holds 2,048"):
		p.Spans(518400)
	p.Choose({"INTERVAL": 0.001}) # a billion bouts: refused at once
	with pytest.raises(pico.PicoError, match="it holds 2,048"):
		p.Plan(518400)
	assert time.monotonic() - t0 < 1


def test_the_last_bout_settings_are_remembered(tmp_path):
	# For each config, the page's settings as the last panel left them; the next panel starts from them
	where = str(tmp_path / "status" / pico.REMEMBER)
	assert pico.Remembered(where, "a.yaml") is None
	pico.Remember(where, "a.yaml", {"PULSE_WIDTH_C": 0.05, "INTERVAL": 600})
	pico.Remember(where, "b.yaml", {"EXTENDED_EVERY": 3})
	last = pico.Remembered(where, "a.yaml")
	assert last["page"] == {"PULSE_WIDTH_C": 0.05, "INTERVAL": 600} and last["at"]
	p = a_pico(set={"INTERVAL": 600})
	p.Restore(last)
	assert p.page == {"PULSE_WIDTH_C": 0.05} # the config says 600 itself now
	status = p.PageStatus()
	assert status["changed"] == ["PULSE_WIDTH_C"] and status["remembered"]
	assert "blue LED at 9.999 s for 50 ms" in status["bout"]["text"]
	p.Choose({"PULSE_WIDTH_D": 40}) # changed on this page again
	assert p.PageStatus()["remembered"] is None and p.page == {"PULSE_WIDTH_C": 0.05, "PULSE_WIDTH_D": 0.04}
	# What a bout setting can't be is left out: another setting, not a number, 0 or less, a part of a bout
	p.Restore({"page": {"LED_LEAD_US": 5, "INTERVAL": "soon", "PULSE_WIDTH_D": 0, "EXTENDED_EVERY": 2.5,
		"CAMERA_FPS": 30, "PULSE_WIDTH_C": 0.07, "YELLOW_FIRST": 2, "LED_GAP": -0.1}, "at": "2026-11-09T15:12:00-05:00"})
	assert p.page == {"PULSE_WIDTH_C": 0.07} and p.PageStatus()["remembered"] == "Mon 9 Nov, 3:12 PM"
	# The order and the dark between the LEDs are remembered, 0 included where the config says otherwise
	p.Restore({"page": {"YELLOW_FIRST": 1, "LED_GAP": 0.15}, "at": "2026-11-09T15:12:00-05:00"})
	assert p.page == {"YELLOW_FIRST": 1, "LED_GAP": 0.15}
	for nothing in (None, {}, {"page": None}, {"page": [1]}):
		p.Restore(nothing)
		assert p.page == {} and p.remembered is None
	# Back to the config's: nothing to remember
	pico.Remember(where, "a.yaml", {})
	assert pico.Remembered(where, "a.yaml") is None and pico.Remembered(where, "b.yaml")["page"] == {"EXTENDED_EVERY": 3}


def test_a_setting_as_chronic_control_reads_it():
	assert [pico.SetText(v) for v in (300, 300.0, 0.05, 0.00001, 1.5, 6)] == ["300", "300", "0.05", "0.00001", "1.5", "6"]


def test_the_recording_keeps_the_settings_it_planned():
	# The page's later changes don't reach a recording's Pico: it starts with what was planned
	d = a_devices()
	d.pico.Choose({"PULSE_WIDTH_C": 50})
	d.Plan(900)
	assert "PULSE_WIDTH_C=0.05" in d.sets and d.settings["from_page"] == ["PULSE_WIDTH_C"]
	assert d.settings["settings"]["PULSE_WIDTH_C"] == 0.05 and d.settings["settings"]["TOTAL_DURATION"] == 900
	d.pico.Choose({"PULSE_WIDTH_C": 70})
	started = []
	d.pico.Start = lambda seconds, folder, sets=None: started.append(sets) or (True, "Session started")
	d.folder, d.lock, d.say = "/nowhere", threading.Lock(), lambda text: None
	d.CanStartPico = lambda s=None: None
	d.StartPico(types.SimpleNamespace(state="recording"))
	assert "PULSE_WIDTH_C=0.05" in started[0]


def test_spikeglx_files_counted_once_per_gate_opening(tmp_path):
	# One opening of the gate: a file per stream, the probe's and the OneBox's
	for name in ("run_g0_t0.imec0.ap.bin", "run_g0_t0.obx0.obx.bin", "run_g0_t1.imec0.ap.bin", "run_g0_t1.obx0.obx.bin",
			"run_g0_t2.obx0.obx.bin"):
		(tmp_path / name).write_bytes(b"")
	assert (spikeglx.Files(str(tmp_path)), spikeglx.Openings(str(tmp_path))) == (5, 3)


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
	# The page says so itself (no need to watch campy's console): every camera ready, in its own line and step
	return wait(url, lambda s: s["state"] == "recording" and s["devices"] and not s["devices"]["can_start_pico"]
		and s["devices"]["cameras_ready"] and s["devices"]["cameras_ready"][0] == s["devices"]["cameras_ready"][1],
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
		assert status["lengths"] == [{"name": "Short", "seconds": 12, "videoFolder": None, "copyTo": None,
			"text": "2 bouts over 12 s, 80 camera pulses (4 s of camera)"}]
		texts = [c["text"] for c in status["checks"]]
		assert all(c["level"] == "ok" for c in status["checks"]), texts
		assert any(t.startswith("The Pico answers, idle") for t in texts) and any(t.startswith("SpikeGLX: idle") for t in texts)
		assert call(url, "/pico-start", {})[0] == 409 # nothing records
		# The bout settings, on the page: the LEDs longer (SHORT's 20 fps: blue from pulse 10, 50 ms = 1 frame more)
		bouts = status["bouts"]
		assert {f["key"]: f["value"] for f in bouts["fields"]}["PULSE_WIDTH_C"] == 30 and bouts["changed"] == []
		assert call(url, "/bouts", {"values": {"PULSE_WIDTH_C": "nope"}})[0] == 409
		assert call(url, "/bouts", {"values": {"PULSE_WIDTH_C": 80, "PULSE_WIDTH_D": 60}}) == (200, {"ok": True})
		status = call(url, "/status.json")[1]
		assert status["bouts"]["changed"] == ["PULSE_WIDTH_C", "PULSE_WIDTH_D"]
		assert "blue LED at 0.999 s for 80 ms" in status["bouts"]["bout"]["text"]
		assert any(c["text"].startswith("Bout settings changed on this page") and "Blue LED on 80 ms (the config's 30)"
			in c["text"] for c in status["checks"])

		assert call(url, "/start", {"experiment": "Gate test", "length": 12}) == (200, {"ok": True})
		status = recording_with_cameras_ready(url)
		folder = the_folder(clips)
		assert steps(status) == {"SpikeGLX": "done", "Gate check": "done", "Cameras": "done", "Pico": "waiting"}
		cams = status["devices"]["cameras_ready"][1]
		assert cams >= 1 and any(s["text"] == "all {} cameras are ready".format(cams) for s in status["devices"]["steps"])
		sglx = state(sglx_state)
		assert sglx["running"] and sglx["dir"] == str(folder / "spikeglx") and sglx["run"] == "chronic_" + folder.name
		assert len(list((folder / "spikeglx").glob("*.bin"))) == 1 # the gate check's blink
		assert status["launcher"]["recording"]["planned_s"] == 4 # recTimeInSec, from the Pico's plan
		assert call(url, "/bouts", {"reset": True})[0] == 409 # not while it records
		# The recording says which settings came from the page; the config file is unchanged
		used = status["launcher"]["devices"]["settings"]
		assert used["from_page"] == ["PULSE_WIDTH_C", "PULSE_WIDTH_D"] and used["settings"]["PULSE_WIDTH_C"] == 0.08
		recorded = (folder / "logs" / "session-config.yaml").read_text()
		assert re.search(r"set: .*PULSE_WIDTH_C: 0.08.*# panel \(bout settings set on campy-session panel's page: "
			r"PULSE_WIDTH_C, PULSE_WIDTH_D\)", recorded), recorded
		assert "PULSE_WIDTH_C" not in config.read_text()

		assert call(url, "/pico-start", {}) == (200, {"ok": True})
		status = wait(url, lambda s: s["state"] == "ready" and s["launcher"] and s["launcher"]["state"] == "done",
			timeout=90)
		assert steps(status)["Pico"] == "done" and steps(status)["End"] == "done"
		assert status["launcher"]["parts"]["acquire"]["exit_code"] == 0
		assert status["launcher"]["clean"]["clean"] is True # the page's "every camera recorded to the end"
		# campy ended after its last frame; the Pico's last gate came later, then SpikeGLX's run stopped
		assert not state(sglx_state)["running"] and state(sglx_state)["calls"][-1] == "stopRun"
		assert len(list((folder / "spikeglx").glob("*.bin"))) == 3 # the blink, and one file per bout
		pico_status = json.loads((tmp_path / "pico-state" / "status.json").read_text())
		assert pico_status["state"] == "done" and "pulses=80" in pico_status["done"]
		assert "Session started" in (folder / "pico" / "session.out").read_text()
		# The Pico ran the page's LEDs: 80 ms of blue, then 60 ms of yellow
		ran = json.loads((tmp_path / "pico-state" / "session.json").read_text())["settings"]
		assert (ran["PULSE_WIDTH_C"], ran["PULSE_WIDTH_D"]) == (0.08, 0.06)
		assert status["devices"]["pico"]["state"] == "done"
	finally:
		close_panel(proc, url, lines)
	assert proc.returncode == 0, "".join(lines)
	assert "[panel] Closed from the page." in "".join(lines)
	# The next panel with this config starts from the page's last bout settings
	assert json.loads((home / "status" / pico.REMEMBER).read_text())[pico.ConfigKey(config)]["page"] == \
		{"PULSE_WIDTH_C": 0.08, "PULSE_WIDTH_D": 0.06}
	proc, lines, url = start_panel(tmp_path, home, config, FAKE_SGLX_STATE=str(sglx_state))
	try:
		bouts = call(url, "/status.json")[1]["bouts"]
		assert bouts["changed"] == ["PULSE_WIDTH_C", "PULSE_WIDTH_D"] and bouts["remembered"]
		assert {f["key"]: f["value"] for f in bouts["fields"]}["PULSE_WIDTH_C"] == 80
		assert call(url, "/bouts", {"reset": True}) == (200, {"ok": True})
		assert call(url, "/status.json")[1]["bouts"]["changed"] == []
	finally:
		close_panel(proc, url, lines)
	assert pico.ConfigKey(config) not in json.loads((home / "status" / pico.REMEMBER).read_text())


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
		assert status["launcher"]["clean"]["clean"] is False # nothing recorded: not "every camera recorded to the end"
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


# ------------------------------------------------------------------ the disk a session needs

class NeedConfig:
	def __init__(self, **panel_given):
		self.panel = section("panel", panel_given)


def a_devices(sglx=True, margin=1.0, **panel_given):
	d = object.__new__(devices.Devices)
	d.pico, d.config = a_pico(), NeedConfig(**panel_given)
	d.sglx = types.SimpleNamespace(section=section("spikeglx", {"sdk": FAKE_SDK}), margin=margin) if sglx else None
	d.Reset()
	d.cached = {}
	return d


def test_the_disk_a_session_needs():
	d = a_devices()
	# The 6-day plan: 288 bouts with 255 s of gate and 1,440 with 25 s, each with SpikeGLX's 1 s margin before and
	# after (112,896 s at 24 MB/s: 2,709.5 GB); 83,520 s of camera for 4 cameras at 0.6 MB/s (200.4 GB). As
	# chronicrecord/README.md works it out: about 2.9 TB
	six = d.Need(518400, 4)
	assert (six["gate_s"], six["camera_s"]) == (112896, 83520)
	assert (round(six["spikeglx_b"] / 1e9, 1), round(six["videos_b"] / 1e9, 1), round(six["total_b"] / 1e12, 2)) == \
		(2709.5, 200.4, 2.91)
	# The 8-hour rehearsal of 6 November: 96 bouts, 6,272 s of gate: 150.5 GB (SpikeGLX wrote 150.3)
	eight = d.Need(28800, 4)
	assert (eight["gate_s"], round(eight["spikeglx_b"] / 1e9, 1)) == (6272, 150.5)
	# The rates are the config's
	d2 = a_devices(video_mb_s=0.1)
	d2.sglx.section["write_mb_s"] = 12
	assert (d2.Need(28800, 4)["spikeglx_b"], d2.Need(28800, 4)["videos_b"]) == (6272 * 12e6, 4640 * 4 * 0.1e6)
	# SpikeGLX's margin as it is set (2 s: 4 s more per bout)
	assert a_devices(margin=2.0).Need(28800, 4)["gate_s"] == 6272 + 96 * 2
	# Without SpikeGLX: the videos alone; without a Pico: no plan to count from
	alone = a_devices(sglx=False).Need(28800, 4)
	assert alone["spikeglx_b"] == 0 and alone["total_b"] == alone["videos_b"]
	d.pico = None
	assert d.Need(28800, 4) is None


def test_the_rest_of_a_session_from_where_the_pico_is():
	d = a_devices()
	assert d.NeedLeft(4) is None # nothing planned
	d.Plan(900) # the gate test: 3 bouts at 0, 300 and 600 s (255, 25 and 25 s of gate)
	assert d.NeedLeft(4)["gate_s"] == 311 # the Pico not started yet: all of it
	d.pico_started = time.time()
	d.cached = {"pico": {"alive": True, "session_s": 610}} # 10 s into the last bout, whose gate is open 1 s earlier
	assert d.NeedLeft(4)["gate_s"] == 25 + 1 - 10
	assert d.NeedLeft(4)["camera_s"] == 10 - 5 # its camera starts 5 s in, for 10 s
	d.cached = {"pico": {"alive": False}}
	assert d.NeedLeft(4) is None
	# A camera-only session: the gate never opens
	assert a_pico(set={"CAMERA_ONLY": 60}).Spans(3600) == ([], [(0.0, 60.0)])


def test_the_pages_drawing_of_a_bout():
	# One long bout and one normal one, in seconds from the gate's opening, from the Pico's own plan: the page draws
	# them (and redraws them as the bout settings change)
	p = a_pico(set={"INTERVAL": 300})
	d = p.PageStatus()["diagram"]
	assert [(r["pin"], r["name"]) for r in d["rows"]] == [("GP2", "Record Gate"), ("GP3", "Camera"), ("GP4", "Blue LED"),
		("GP5", "Yellow LED")]
	assert (d["fps"], d["interval_s"], d["pulse_s"]) == (60.0, 300.0, 0.003)
	long, normal = d["bouts"]
	assert (long["long"], long["gate"], long["camera"][0], long["pulses"]) == (True, [0.0, 255.0], 5.0, 14400)
	assert (normal["long"], normal["gate"], normal["pulses"]) == (False, [0.0, 25.0], 600)
	# The LEDs in the order they light: blue 30 ms, then yellow at once, 1 ms before pulse 300
	assert [(l["name"], l["on"], l["off"]) for l in long["leds"]] == [("blue", 9.999, 10.029), ("yellow", 10.029, 10.059)]
	# The page's settings change it
	assert p.Choose({"PULSE_WIDTH_C": 50, "STANDARD_STIM_DURATION": 20}) is None
	d = p.PageStatus()["diagram"]
	assert d["bouts"][0]["leds"][0]["off"] == 10.049 and d["bouts"][1]["gate"] == [0.0, 35.0]
	# Every bout long: one bout drawn; settings that can't run: none
	assert p.Choose({"EXTENDED_EVERY": 1}) is None and len(p.PageStatus()["diagram"]["bouts"]) == 1
	assert p.Choose({"CAMERA_LEAD_IN": 20}) is None and p.PageStatus()["diagram"] is None
