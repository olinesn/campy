"""bench/rig.py and an ephys acquisition program (SpikeGLX, Open Ephys) running on the machine.

It may be recording Neuropixels unattended, through drivers rig.py has no names for (an imec PCIe base station,
a OneBox, NI DAQ cards), so the program running at all makes the machine busy: status shows it, run waits for
it, an experiment machine refuses a test at once, and a test running when one starts is stopped. --allow ephys
(RIG_ALLOW, which campy-session's panel sets) is for a recording that runs beside it.

The rules run on any OS with made-up processes; whole runs need Linux (/proc), where a symlink to sleep
named open-ephys stands in for the program (its comm is the link's name).
"""

import datetime, json, os, shutil, subprocess, sys, time, types

import pytest

BENCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench")
RIG = os.path.join(BENCH, "rig.py")
sys.path.insert(0, BENCH)
import rig  # noqa: E402
import rig_windows  # noqa: E402

# rig.py looks at the whole machine (every process on it, its CPU and disk load): these tests run with no
# other test beside them (conftest.py, 'alone')
pytestmark = pytest.mark.alone

linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="rig.py reads Linux's /proc")


def fake(pid, kinds=(), name="python.exe", ppid=1, start=None, job=None):
	p = rig.Proc()
	p.pid, p.ppid, p.sid, p.start, p.cpu, p.io = pid, ppid, None, start if start is not None else pid, 0.0, 0
	p.kinds, p.cmd, p.job, p.user = set(kinds), name, job, None
	return p


def args(**kw):
	a = dict(cpu=2.0, io=50.0, load_looks=2, load_share=0.2, strict=False, no_wait=False, allow=set())
	a.update(kw)
	return types.SimpleNamespace(**a)


@pytest.fixture
def machine(monkeypatch):
	"""rig.scan gives this process and whatever is in procs (pid: Proc), on any OS."""
	procs = {}
	monkeypatch.setattr(rig, "scan", lambda own=(): {**procs, os.getpid(): fake(os.getpid(), ppid=0, start=0)})
	monkeypatch.setattr(rig, "disk_bytes", lambda: 0)
	monkeypatch.setattr(rig, "cmdline", lambda p: p.cmd)
	monkeypatch.setattr(rig, "NVSMI", None)
	monkeypatch.setattr(rig, "PERIOD", 0.05)
	return procs


def test_windows_knows_spikeglx_and_open_ephys_by_image_name():
	# The image names as installed on a Dell Precision 3660 that runs SpikeGLX (v20230905-phase30) and Open
	# Ephys 0.6.6; the Open Ephys installer is not the program
	table = [(pid, 1, name, pid, 0.0, 0) for pid, name in enumerate(
		["SpikeGLX.exe", "SpikeGLX_NISIM.exe", "open-ephys.exe", "Install-Open-Ephys-GUI-v0.6.6.exe", "notepad.exe"], 10)]
	kinds = {r[6]: r[5] for r in rig_windows.classify(table)}
	assert kinds == {"SpikeGLX.exe": {"ephys"}, "SpikeGLX_NISIM.exe": {"ephys"}, "open-ephys.exe": {"ephys"},
		"Install-Open-Ephys-GUI-v0.6.6.exe": set(), "notepad.exe": set()}
	# SpikeGLX draws its graphs with OpenGL: on a machine with an NVIDIA GPU it is listed as drawing (G), and
	# stays an ephys program, not a desktop
	assert rig_windows.classify([(20, 1, "SpikeGLX.exe", 20, 0.0, 0)], desktop_pids={20})[0][5] == {"ephys"}


def test_linux_knows_them_by_comm():
	assert rig.is_ephys("open-ephys") and rig.is_ephys("SpikeGLX.exe") and rig.is_ephys("spikeglx")
	assert not rig.is_ephys("python3") and not rig.is_ephys("ephys-viewer")
	assert "ephys" in rig.KINDS


def test_run_waits_for_spikeglx_and_no_wait_exits_75(machine, capsys):
	machine[500] = fake(500, {"ephys"}, "SpikeGLX.exe")
	with pytest.raises(SystemExit) as e:
		rig.wait_quiet(rig.Watch(), args(no_wait=True))
	assert e.value.code == 75
	assert "500 ephys SpikeGLX.exe" in capsys.readouterr().out
	looks = []
	real = rig.scan
	def scan(own=()):
		looks.append(1)
		if len(looks) >= 4:
			machine.pop(500, None)  # SpikeGLX closed after 3 looks
		return real(own)
	rig.scan = scan
	try:
		rig.wait_quiet(rig.Watch(), args())
	finally:
		rig.scan = real
	assert len(looks) == 4


def test_allow_ephys_leaves_it_out_but_not_what_else_it_uses(machine):
	machine[500] = fake(500, {"ephys"}, "SpikeGLX.exe")
	machine[501] = fake(501, {"ephys", "trigger"}, "open-ephys.exe")  # with a COM port open too
	w = rig.Watch(allow={"ephys"})
	users, _ = w.sample()
	assert [p.pid for p in users] == [501] and w.foreign(users[0]) == {"trigger"}
	assert [p.pid for p in w.allowed] == [500]
	w = rig.Watch()
	assert sorted(p.pid for p in w.sample()[0]) == [500, 501]


def test_allow_takes_only_known_kinds():
	assert rig.parse_allow("ephys") == {"ephys"} and rig.parse_allow(" Ephys, trigger ") == {"ephys", "trigger"}
	assert rig.parse_allow("") == set()
	with pytest.raises(Exception, match="not a kind: spikeglx"):
		rig.parse_allow("spikeglx")


class Lock:
	def ours(self):
		return True

	def text(self):
		return ""


def watched(machine, w, looks=4):
	"""The counts and log lines of a run watched for a few looks, SpikeGLX starting after the first."""
	n, lines = [0], []
	def alive():
		n[0] += 1
		if n[0] == 2:
			machine[500] = fake(500, {"ephys"}, "SpikeGLX.exe")
		return n[0] <= looks
	counts = rig.watch(w, alive, lines.append, args(allow=w.allow), [None], Lock())
	return counts, lines


def test_spikeglx_starting_during_a_run_contaminates_it(machine):
	counts, lines = watched(machine, rig.Watch(job="t-1"))
	assert counts["ephys"] >= 2 and any(line.startswith("FOREIGN 500 ephys SpikeGLX.exe") for line in lines), lines
	out = []
	rig.finish(counts, out.append)
	assert out[-1].startswith("RIG_CONTAMINATED (") and "ephys " in out[-1], out


def test_allowed_spikeglx_is_logged_once_and_the_run_stays_clean(machine):
	counts, lines = watched(machine, rig.Watch(job="t-1", allow={"ephys"}))
	assert counts["ephys"] == 0
	assert [line for line in lines if line.startswith("ALLOWED")] == ["ALLOWED (--allow) 500 ephys SpikeGLX.exe"], lines
	out = []
	rig.finish(counts, out.append)
	assert out[-1].startswith("RIG_CLEAN"), out


def test_an_experiment_test_stops_when_spikeglx_starts():
	cfg = dict(rig.EXPERIMENT_DEFAULTS, session_every=3600)
	s = rig.Stopper(cfg, [])
	assert s.check([fake(500, {"ephys"}, r"C:\SpikeGLX\SpikeGLX.exe")])
	assert s.why == "SpikeGLX.exe started outside the test, an ephys acquisition program (pid 500)"
	s = rig.Stopper(cfg, [])
	assert s.check([fake(501, {"ephys", "trigger"}, "open-ephys.exe")])
	assert s.why == "open-ephys.exe started outside the test, using the trigger, an ephys acquisition program (pid 501)"
	s = rig.Stopper(cfg, [])
	assert s.check([fake(502, {"camera"}, "SpinView_WPF.exe")])
	assert s.why == "SpinView_WPF.exe started outside the test, using the camera (pid 502)"  # as before


def test_an_experiment_machine_refuses_a_test_while_spikeglx_runs(tmp_path, monkeypatch):
	monkeypatch.setattr(rig, "ROOT", str(tmp_path))
	q = tmp_path / "quser.txt"
	q.write_text("No User exists for *\n")  # nobody logged on
	monkeypatch.setenv("RIG_QUSER", str(q))
	cfg = dict(rig.EXPERIMENT_DEFAULTS, banner=False)
	night = datetime.datetime(2026, 11, 18, 23, 0)
	running = [(4242, "SpikeGLX.exe")]
	monkeypatch.setattr(rig, "ephys_programs", lambda: list(running))
	assert rig.experiment_blockers(cfg, now=night)[0] == [
		"an ephys acquisition program is running (it may be recording): SpikeGLX.exe (pid 4242)"]
	assert rig.experiment_blockers(cfg, now=night, allow={"ephys"})[0] == []
	running.clear()
	assert rig.experiment_blockers(cfg, now=night)[0] == []


# Whole runs (Linux)

@pytest.fixture
def ephys(tmp_path):
	"""A program named open-ephys: a symlink to sleep (its comm is the link's name)."""
	link = tmp_path / "bin" / "open-ephys"
	link.parent.mkdir()
	os.symlink(shutil.which("sleep"), str(link))
	procs = []
	def start(seconds=30):
		p = subprocess.Popen([str(link), str(seconds)])
		procs.append(p)
		time.sleep(0.3)
		return p
	yield start
	for p in procs:
		p.kill()
		p.wait()


@pytest.fixture
def run(tmp_path):
	root = tmp_path / "root"
	root.mkdir()
	env = dict(os.environ, RIG_ROOT=str(root), RIG_PERIOD="0.3", RIG_CPU="1000", RIG_IO="100000")
	for k in ("RIG_JOB", "RIG_TAG", "RIG_LOGDIR", "RIG_EXTRA", "RIG_ALLOW", "RIG_EPHYS_NAMES"):
		env.pop(k, None)

	class Run:
		def __call__(self, *a, **kw):
			return subprocess.run([sys.executable, RIG] + list(a), env=dict(env, **kw.pop("env", {})), cwd=str(tmp_path),
				capture_output=True, text=True, timeout=60)

		def popen(self, *a, **kw):
			return subprocess.Popen([sys.executable, RIG] + list(a), env=dict(env, **kw.pop("env", {})), cwd=str(tmp_path),
				stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

		def log(self, tag):
			return (tmp_path / (tag + ".rig.log")).read_text()

	r = Run()
	r.root, r.env, r.tmp = root, env, tmp_path
	return r


@linux
def test_status_and_run_see_open_ephys(run, ephys):
	p = ephys(30)
	s = run("status")
	assert "ephys acquisition (spikeglx*, open-ephys*):" in s.stdout, s.stdout
	assert "    %d ephys " % p.pid in s.stdout and "/bin/open-ephys" in s.stdout, s.stdout
	r = run("run", "--no-wait", "tester", "busy", "--", "true")
	assert r.returncode == 75 and "rig in use outside rig.py" in r.stdout and "%d ephys" % p.pid in r.stdout, r.stdout
	# --allow ephys (the panel's RIG_ALLOW): it runs, clean, the program logged once
	r = run("run", "--no-wait", "tester", "beside", "--", "sleep", "1", env={"RIG_ALLOW": "ephys"})
	assert r.returncode == 0, r.stdout + r.stderr
	log = run.log("beside")
	assert "allowed outside the job, not waited for and not counted (--allow): ephys" in log
	assert log.count("ALLOWED (--allow) %d ephys" % p.pid) == 1, log
	assert log.strip().splitlines()[-1].split(" ", 1)[1].startswith("RIG_CLEAN"), log


@linux
def test_run_waits_until_open_ephys_closes(run, ephys):
	p = ephys(2)
	t = time.monotonic()
	r = run("run", "tester", "after", "--", "true")
	assert r.returncode == 0 and "in use outside rig.py, waiting" in r.stdout, r.stdout
	assert time.monotonic() - t > 1.0
	p.wait()


@linux
def test_experiment_machine_refuses_then_stops_a_test_for_open_ephys(run, ephys, tmp_path):
	(run.root / "experiment.json").write_text(json.dumps({"hours": "any", "banner": False, "session_every": 0.3}))
	q = tmp_path / "quser.txt"
	q.write_text("No User exists for *\n")
	env = {"RIG_QUSER": str(q), "RIG_LOCK": "file"}
	p = ephys(30)
	r = run("run", "tester", "refused", "--", "true", env=env)
	assert r.returncode == 75, r.stdout + r.stderr
	assert "not now on this experiment machine: an ephys acquisition program is running (it may be recording): " \
		"open-ephys (pid %d)" % p.pid in r.stdout, r.stdout
	assert not (run.tmp / "refused.rig.log").exists()
	p.kill()
	p.wait()
	# A test running when it starts is stopped, and tests pause
	t = run.popen("run", "tester", "stopped", "--", "sleep", "60", env=env)
	for line in t.stdout:
		if " rig held by " in line:
			break
	p = ephys(30)
	out, _ = t.communicate(timeout=60)
	assert t.returncode == 75, out
	log = run.log("stopped")
	assert "RIG_STOPPED at the machine by open-ephys started outside the test, an ephys acquisition program " \
		"(pid %d)" % p.pid in log, log
	assert "an ephys acquisition program" in (run.root / "PAUSE").read_text()
