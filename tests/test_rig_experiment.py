"""bench/rig.py on an experiment machine (RIG_ROOT/experiment.json): tests start only when nobody uses the
machine, at the allowed hours and not paused, and stop within 2 s on STOP, input or a device user.

quser's table comes from a file (RIG_QUSER); the banner is off (it needs a Windows session), so the
whole runs need only a machine where rig.py sees processes (Linux, Windows)."""

import datetime, json, os, subprocess, sys, time

import pytest

BENCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench")
RIG = os.path.join(BENCH, "rig.py")
sys.path.insert(0, BENCH)
import rig  # noqa: E402

# rig.py looks at the whole machine (every process on it, its CPU and disk load): these tests run with no
# other test beside them (conftest.py, 'alone')
pytestmark = pytest.mark.alone

RUNS = os.name == "nt" or sys.platform.startswith("linux")
runs = pytest.mark.skipif(not RUNS, reason="rig.py reads processes on Linux and Windows only")
linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="a fake GPU device file (RIG_EXTRA) is Linux's")

QUSER = """ USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME
>lab                   console             1  Active      {idle}  11/4/2026 9:00 PM
"""
QUSER_RDP = """ USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME
 lab                   rdp-tcp#0           2  Active          8  11/3/2026 2:36 AM
 other                                     3  Disc      1+02:05  11/2/2026 9:15 AM
 third                 console             4  Active       1:05  11/2/2026 9:15 AM
 fourth                console             5  Active          .  11/2/2026 9:15 AM
"""


def test_quser_table():
	s = rig.parse_quser(QUSER_RDP)
	assert [(x["user"], x["session"], x["id"], x["state"], x["idle"]) for x in s] == [
		("lab", "rdp-tcp#0", 2, "Active", 8), ("other", "", 3, "Disc", 1565),
		("third", "console", 4, "Active", 65), ("fourth", "console", 5, "Active", 0)]
	assert rig.parse_quser(QUSER.format(idle="none"))[0]["idle"] == 0
	assert rig.parse_quser("No User exists for *") == []


def test_allowed_hours():
	at = lambda h, m: datetime.datetime(2026, 11, 4, h, m)
	assert rig.in_hours("22:00-06:00", at(23, 30)) and rig.in_hours("22:00-06:00", at(5, 59))
	assert not rig.in_hours("22:00-06:00", at(6, 0)) and not rig.in_hours("22:00-06:00", at(12, 0))
	assert rig.in_hours("09:00-10:00,13:00-14:00", at(13, 10)) and not rig.in_hours("09:00-10:00", at(10, 0))
	assert rig.in_hours("any", at(12, 0)) and rig.in_hours("", at(12, 0))


@pytest.fixture
def root(tmp_path, monkeypatch):
	monkeypatch.setattr(rig, "ROOT", str(tmp_path))
	return tmp_path


def test_pause_until_the_next_morning_then_it_ends(root):
	cfg = dict(rig.EXPERIMENT_DEFAULTS)
	night = datetime.datetime(2026, 11, 4, 23, 40)
	assert rig.pause_until(cfg, night) == "2026-11-05 09:00"
	assert rig.pause_until(cfg, datetime.datetime(2026, 11, 5, 3, 0)) == "2026-11-05 09:00"
	rig.write_pause(cfg, "the Stop button on the banner", night)
	text = rig.read_pause(night)
	assert "until=2026-11-05 09:00" in text and "how=the Stop button on the banner" in text
	rig.write_pause(cfg, "again", night)  # a pause in place stays as it is
	assert "again" not in rig.read_pause(night)
	assert rig.read_pause(datetime.datetime(2026, 11, 5, 8, 59)) is not None  # not over yet
	assert rig.read_pause(datetime.datetime(2026, 11, 5, 9, 0)) is None  # over: removed
	assert not (root / "PAUSE").exists()
	(root / "PAUSE").write_text("until=resume; how=the banner (until resumed)\n")
	assert rig.read_pause(datetime.datetime(2030, 1, 1)) is not None
	(root / "PAUSE").write_text("garbage\n")  # unreadable: paused until removed
	assert rig.read_pause(night) == "garbage"


def test_blockers(root, monkeypatch, tmp_path):
	q = tmp_path / "quser.txt"
	monkeypatch.setenv("RIG_QUSER", str(q))
	cfg = dict(rig.EXPERIMENT_DEFAULTS, banner=False)
	night, noon = datetime.datetime(2026, 11, 4, 23, 0), datetime.datetime(2026, 11, 4, 12, 0)
	q.write_text(QUSER.format(idle="20"))
	assert rig.experiment_blockers(cfg, now=night)[0] == []
	assert "outside the hours" in rig.experiment_blockers(cfg, now=noon)[0][0]
	q.write_text(QUSER.format(idle="3"))
	assert "someone may be using it: session 1 (lab console, Active, idle 3 min), under 15 min" in rig.experiment_blockers(cfg, now=night)[0]
	q.write_text("No User exists for *\n")  # nobody logged on: nobody to disturb
	assert rig.experiment_blockers(cfg, now=night)[0] == []
	q.unlink()  # quser failed: can't tell, so no
	assert "quser failed" in rig.experiment_blockers(cfg, now=night)[0][0]
	q.write_text(QUSER.format(idle="20"))
	rig.write_pause(cfg, "the desktop shortcut", night)
	assert "tests are paused" in rig.experiment_blockers(cfg, now=night)[0][0]


def test_session_change():
	base = {s["id"]: s for s in rig.parse_quser(QUSER.format(idle="20"))}
	assert rig.session_change(base, rig.parse_quser(QUSER.format(idle="21"))) is None
	assert "keyboard or mouse input" in rig.session_change(base, rig.parse_quser(QUSER.format(idle=".")))
	assert "a new session" in rig.session_change(base, rig.parse_quser(QUSER_RDP))
	assert rig.session_change(base, None) is None  # quser failed this once


def test_a_broken_experiment_json_keeps_the_rules(root):
	(root / "experiment.json").write_text("{not json")
	assert rig.experiment_config() == rig.EXPERIMENT_DEFAULTS
	(root / "experiment.json").write_text(json.dumps({"hours": "any"}))
	assert rig.experiment_config()["hours"] == "any"
	(root / "experiment.json").unlink()
	assert rig.experiment_config() is None


# Whole runs

@pytest.fixture
def run(tmp_path):
	root = tmp_path / "root"
	root.mkdir()
	(root / "experiment.json").write_text(json.dumps({"hours": "any", "banner": False, "session_every": 0.3}))
	q = tmp_path / "quser.txt"
	q.write_text(QUSER.format(idle="20"))
	env = dict(os.environ, RIG_ROOT=str(root), RIG_LOCK="file", RIG_PERIOD="0.5", RIG_CPU="1000",
		RIG_IO="100000", RIG_QUSER=str(q))
	for k in ("RIG_JOB", "RIG_EXTRA"):
		env.pop(k, None)

	class Run:
		def popen(self, *args, **kw):
			return subprocess.Popen([sys.executable, RIG] + list(args), env=dict(env, **kw.pop("env", {})),
				cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

		def __call__(self, *args):
			return subprocess.run([sys.executable, RIG] + list(args), env=env, cwd=str(tmp_path),
				capture_output=True, text=True, timeout=60)

		def until_held(self, p):
			out = []
			for line in p.stdout:
				out.append(line)
				if " rig held by " in line:
					return out
			raise AssertionError("".join(out))

		def log(self, tag):
			return (tmp_path / (tag + ".rig.log")).read_text()

	r = Run()
	r.root, r.quser, r.env, r.tmp = root, q, env, tmp_path
	return r


SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]


def write_stop(root, text):
	# As the banner's Stop button writes it (rig_banner.ps1): whole or not at all, through a
	# temporary file. A plain write_text leaves the file empty for a moment, and rig.py,
	# reading it then, logs "the STOP file" instead of its reason (seen on Windows CI)
	tmp = root / "STOP.tmp"
	tmp.write_text(text)
	os.replace(str(tmp), str(root / "STOP"))


@runs
def test_stop_file_ends_the_test_within_2_s_and_pauses(run):
	p = run.popen("run", "tester", "stopme", "--", *SLEEP)
	run.until_held(p)
	time.sleep(1.0)
	write_stop(run.root, "how=the Stop button on the banner; at=now\n")
	t0 = time.monotonic()
	out, _ = p.communicate(timeout=60)
	took = time.monotonic() - t0
	assert p.returncode == 75, out
	log = run.log("stopme")
	assert log.strip().splitlines()[-1].split(" ", 1)[1].startswith(
		"RIG_STOPPED at the machine by the Stop button on the banner (until then: clean"), log
	assert "experiment machine: session 1 (lab console, Active, idle 20 min)" in log
	assert took < 2.5, (took, log)  # within 2 s of the click, plus the job's own exit
	assert not (run.root / "STOP").exists() and not (run.root / "gpu.lock").exists()
	assert "how=the Stop button on the banner" in (run.root / "PAUSE").read_text()
	# Paused: the next test doesn't start, and says why
	r = run("run", "tester", "next", "--", sys.executable, "-c", "pass")
	assert r.returncode == 75 and "tests are paused" in r.stdout, r.stdout + r.stderr
	assert not (run.tmp / "next.rig.log").exists()


@runs
def test_input_at_the_machine_stops_the_test(run):
	p = run.popen("run", "tester", "input", "--", *SLEEP)
	run.until_held(p)
	run.quser.write_text(QUSER.format(idle="."))  # someone moved the mouse
	out, _ = p.communicate(timeout=60)
	assert p.returncode == 75, out
	assert "RIG_STOPPED at the machine by keyboard or mouse input: session 1" in run.log("input")
	assert "keyboard or mouse input" in (run.root / "PAUSE").read_text()


@runs
def test_no_test_while_someone_uses_the_machine_or_outside_the_hours(run):
	run.quser.write_text(QUSER.format(idle="2"))
	r = run("run", "tester", "busy", "--", sys.executable, "-c", "pass")
	assert r.returncode == 75 and "someone may be using it" in r.stdout, r.stdout + r.stderr
	assert not (run.root / "gpu.lock").exists() and not (run.tmp / "busy.rig.log").exists()
	run.quser.write_text(QUSER.format(idle="20"))
	now = datetime.datetime.now()
	h = (now.hour + 2) % 24
	(run.root / "experiment.json").write_text(json.dumps({"hours": "%02d:00-%02d:30" % (h, h), "banner": False}))
	r = run("run", "tester", "late", "--", sys.executable, "-c", "pass")
	assert r.returncode == 75 and "outside the hours" in r.stdout, r.stdout + r.stderr


@runs
def test_a_clean_test_runs_and_leaves_no_pause(run):
	(run.root / "STOP").write_text("how=an old stop\n")  # left from before: not for this run
	r = run("run", "tester", "ok", "--", sys.executable, "-c", "pass")
	assert r.returncode == 0, r.stdout + r.stderr
	assert run.log("ok").strip().splitlines()[-1].split(" ", 1)[1].startswith("RIG_CLEAN"), run.log("ok")
	assert not (run.root / "PAUSE").exists() and not (run.root / "STOP").exists()


@linux
def test_a_device_opened_outside_the_test_stops_it(run, tmp_path):
	gpu = tmp_path / "fake-kfd"
	gpu.write_text("")
	env = {"RIG_EXTRA": "camera=%s" % gpu}
	p = run.popen("run", "tester", "cam", "--", *SLEEP, env=env)
	run.until_held(p)
	other = subprocess.Popen([sys.executable, "-c", "import sys, time; f = open(sys.argv[1]); time.sleep(5)", str(gpu)])
	try:
		out, _ = p.communicate(timeout=60)
	finally:
		other.kill()
	assert p.returncode == 75, out
	log = run.log("cam")
	assert "RIG_STOPPED at the machine by python" in log and "started outside the test, using the camera (pid %d)" % other.pid in log, log
	assert "using the camera" in (run.root / "PAUSE").read_text()
