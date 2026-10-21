"""bench/rig.py on Windows: its lock file (gpu.lock), its view of the processes (rig_windows.py),
the GPU clock log and rig_guard.ps1.

Three kinds of test:
- pure functions and the lock in-process, with the Windows parts stubbed: on any OS;
- whole runs of rig.py holding gpu.lock (RIG_LOCK=file on Linux): on Linux and Windows;
- the Windows API calls and rig_guard.ps1 themselves: on Windows only (the CI's Windows runner).
"""

import os, re, subprocess, sys, time

import pytest

BENCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench")
RIG = os.path.join(BENCH, "rig.py")
sys.path.insert(0, BENCH)
import rig, rig_windows  # noqa: E402

WINDOWS = sys.platform == "win32"
RUNS = WINDOWS or sys.platform.startswith("linux")  # where rig.py can see processes
runs = pytest.mark.skipif(not RUNS, reason="rig.py reads processes on Linux and Windows only")
windows = pytest.mark.skipif(not WINDOWS, reason="Windows API")


def last(log):
	# the log's last line, without its time
	return log.strip().splitlines()[-1].split(" ", 1)[1]


def dead_pid():
	p = subprocess.Popen([sys.executable, "-c", "pass"])
	p.wait()
	return p.pid


# Pure functions

def test_classify_names_dlls_ports_and_jobs(monkeypatch):
	monkeypatch.setattr(rig_windows, "GPU_NAMES", ["python*.exe"])
	monkeypatch.setattr(rig_windows, "ENCODER_NAMES", ["ffmpeg*.exe"])
	table = [  # pid, ppid, name, start, cpu, io
		(10, 1, "python.exe", 100, 0.0, 0),
		(11, 1, "Python.EXE", 101, 0.0, 0),       # another rig.py
		(12, 1, "ffmpeg.exe", 102, 0.0, 0),
		(13, 1, "SpinView_WPF.exe", 103, 0.0, 0),
		(14, 1, "putty.exe", 104, 0.0, 0),
		(15, 1, "blender.exe", 105, 0.0, 0),      # listed by nvidia-smi
		(16, 1, "explorer.exe", 106, 0.0, 0),
		(17, 10, "python.exe", 107, 0.0, 0),      # in the job
	]
	cmds = {11: r'C:\py\python.exe C:\campy_data\live\rig.py run s1 t1 -- python x.py', 10: "python campy-live"}
	recs = rig_windows.classify(table, gpu_pids={15}, camera_pids={13}, ports={14: {"trigger"}},
		jobs={"t-1": {17}}, cmdline=lambda pid, start: cmds.get(pid), desktop_pids={16, 10})
	kinds = {r[0]: r[5] for r in recs}
	assert kinds == {10: {"gpu"}, 11: set(), 12: {"encoder"}, 13: {"camera"}, 14: {"trigger"}, 15: {"gpu"},
		16: {"desktop"}, 17: {"gpu"}}
	assert {r[0]: r[7] for r in recs}[17] == "t-1"


def test_nvidia_smi_parsing():
	pids = """
==============NVSMI LOG==============
Attached GPUs                             : 1
GPU 00000000:01:00.0
    Processes
        Process ID                        : 2100
            Type                          : C+G
            Name                          : C:\\Windows\\System32\\dwm.exe
            Used GPU Memory               : Not available in WDDM driver model
        Process ID                        : 23336
            Type                          : C
            Name                          : C:\\x\\python.exe
"""
	assert rig.parse_gpu_processes(pids) == {2100: "C+G", 23336: "C"}
	assert rig.parse_gpu_processes(None) == {}
	rows = rig.parse_clocks("0, 780, 0x0000000000000004, P2\n1, [N/A], 0x0000000000000001, P8\n")
	assert rows == [(0, 780, 4, "P2"), (1, None, 1, "P8")]
	assert rig.parse_clocks("0, 210, P8\n", reasons=False) == [(0, 210, None, "P8")]
	assert rig.throttle_text(0) == "none"
	assert rig.throttle_text(0x5) == "Idle, SW Power Cap"
	assert rig.clock_text((0, 780, 4, "P2")) == "GPU 0: SM clock 780 MHz, P2, held down by: SW Power Cap"


def test_clock_log_and_summary(monkeypatch):
	rows = iter([[(0, 210, 1, "P8")], [(0, 215, 1, "P8")], [(0, 780, 4, "P2")], [(0, 780, 4, "P2")]])
	monkeypatch.setattr(rig, "gpu_clocks", lambda: next(rows))
	lines = []
	c = rig.Clocks(lines.append)
	c.sample()
	c.sample()  # 5 MHz, same reasons: not logged
	c.logged[0] = c.logged[0][:2] + (time.monotonic() - 20,)
	c.sample()
	c.sample()
	assert lines == ["GPU 0: SM clock 210 MHz, P8, held down by: Idle", "GPU 0: SM clock 780 MHz, P2, held down by: SW Power Cap"]
	summary, note = c.summary()
	assert summary == ["GPU 0 over the run: SM clock median 780 MHz, 210-780 MHz in 4 looks; held down by: Idle 50%, SW Power Cap 50%"]
	assert note == "GPU 0 SM median 780 MHz"


def test_finish_puts_the_gpu_clock_on_the_last_line():
	lines = []
	counts = dict.fromkeys(rig.KINDS + ("desktop", "load", "lock"), 0)
	counts["desktops"], counts["clocks"] = "none", (["GPU 0 over the run: ..."], "GPU 0 SM median 780 MHz")
	rig.finish(dict(counts), lines.append)
	assert lines == ["GPU 0 over the run: ...", "RIG_CLEAN (GPU 0 SM median 780 MHz)"]
	lines.clear()
	counts["gpu"] = 2
	rig.finish(dict(counts), lines.append)
	assert lines[-1].startswith("RIG_CONTAMINATED (2 sightings: gpu 2, encoder 0") and lines[-1].endswith("; GPU 0 SM median 780 MHz)")


# The lock, in-process

@pytest.fixture
def root(tmp_path, monkeypatch):
	monkeypatch.setattr(rig, "ROOT", str(tmp_path))
	monkeypatch.setattr(rig, "PERIOD", 0.05)
	monkeypatch.setenv("RIG_LOCK", "file")
	return tmp_path


def test_lock_is_taken_recorded_and_released(root):
	a = rig.FileLock()
	a.acquire("s1", "t1", no_wait=True)
	text = (root / "gpu.lock").read_text()
	assert re.match(r"s1 t1 since \d{4}-\d\d-\d\d \d\d:\d\d:\d\d rigpid=%d rigstart=-?\d+\n$" % os.getpid(), text), text
	assert a.state()[0] == "held" and a.ours()
	b = rig.FileLock()
	with pytest.raises(SystemExit) as e:
		b.acquire("s2", "t2", no_wait=True)
	assert e.value.code == 75
	a.record("t1-5", os.getpid())
	assert " job=t1-5 pid=%d pidstart=" % os.getpid() in (root / "gpu.lock").read_text()
	a.release()
	assert not (root / "gpu.lock").exists()
	b.acquire("s2", "t2", no_wait=True)
	b.release()


def test_a_lock_whose_processes_ended_is_free(root, capsys):
	(root / "gpu.lock").write_text("ghost t since 2026-10-21 05:33:00 rigpid=%d rigstart=1 job=t-1 pid=%d pidstart=1\n" % (dead_pid(), dead_pid()))
	lock = rig.FileLock()
	assert lock.state()[0] == "stale"
	lock.acquire("s1", "t1", no_wait=True)
	assert "stale gpu.lock removed" in capsys.readouterr().out
	assert (root / "gpu.lock").read_text().startswith("s1 t1 ")
	lock.release()


@pytest.mark.skipif(not RUNS, reason="needs process start times (Linux, Windows)")
def test_a_reused_pid_does_not_hold_the_lock(root):
	# The pid runs (it's this test), but it started at another time: a different process
	(root / "gpu.lock").write_text("ghost t since 2026-10-21 05:33:00 rigpid=%d rigstart=12345\n" % os.getpid())
	assert rig.FileLock().state()[0] == "stale"


def test_a_hand_written_lock_holds_until_broken(root, capsys):
	(root / "gpu.lock").write_text("cuda-graphs-session 2026-10-21 05:33\n")
	lock = rig.FileLock()
	assert lock.state()[0] == "hand"
	with pytest.raises(SystemExit):
		lock.acquire("s1", "t1", no_wait=True)
	out = capsys.readouterr().out
	assert "rig busy: 'cuda-graphs-session 2026-10-21 05:33' (gpu.lock written by hand" in out, out
	rig.cmd_break(None)
	assert "removed a hand-written gpu.lock: cuda-graphs-session" in capsys.readouterr().out
	assert not (root / "gpu.lock").exists()


def test_break_refuses_a_live_lock(root):
	lock = rig.FileLock()
	lock.acquire("s1", "t1", no_wait=True)
	try:
		with pytest.raises(SystemExit) as e:
			rig.cmd_break(None)
		assert "stop it instead" in str(e.value.code)
		assert (root / "gpu.lock").exists()
	finally:
		lock.release()


def test_a_lock_overwritten_by_someone_else_is_left_to_them(root, capsys):
	lock = rig.FileLock()
	lock.acquire("s1", "t1", no_wait=True)
	(root / "gpu.lock").write_text("intruder 06:00\n")
	assert not lock.ours()
	lock.release()
	assert (root / "gpu.lock").read_text() == "intruder 06:00\n"
	assert "no longer this run's" in capsys.readouterr().out


# Waiting for the devices, with the processes stubbed (as Windows would list them)

def fake(pid, kinds=(), name="python.exe", ppid=1, start=None, cpu=0.0, job=None):
	p = rig.Proc()
	p.pid, p.ppid, p.sid, p.start, p.cpu, p.io = pid, ppid, None, start if start is not None else pid, cpu, 0
	p.kinds, p.cmd, p.job, p.user = set(kinds), name, job, None
	return p


def test_run_waits_for_a_python_on_the_gpu(monkeypatch, root, capsys):
	looks = []
	def scan(own=()):
		looks.append(1)
		procs = {os.getpid(): fake(os.getpid(), ppid=0, start=0)}
		if len(looks) < 4:
			procs[500] = fake(500, {"gpu"}, "python.exe")  # leaves after 3 looks
		return procs
	monkeypatch.setattr(rig, "scan", scan)
	class Args:
		cpu, io, no_wait = 2.0, 50.0, False
	rig.wait_quiet(rig.Watch(), Args)
	assert len(looks) == 4
	assert "500 gpu python.exe" in capsys.readouterr().out


def test_the_jobs_own_processes_and_reused_parent_pids(monkeypatch):
	w = rig.Watch(job="t-1", root_pid=200)
	procs = {
		200: fake(200, start=10),
		201: fake(201, ppid=200, start=11),      # the job's child
		202: fake(202, ppid=300, start=12),      # a new process whose parent pid 300 ...
		300: fake(300, ppid=200, start=50),      # ... was reused later by one of the job's
		400: fake(400, job="t-1", start=13),     # in the job object
	}
	assert w.own(procs[201], procs) and w.own(procs[400], procs)
	assert not w.own(procs[202], procs)


# Whole runs, holding gpu.lock

@pytest.fixture
def run(tmp_path):
	env = dict(os.environ, RIG_ROOT=str(tmp_path / "root"), RIG_LOCK="file", RIG_PERIOD="0.3",
		RIG_CPU="1000", RIG_IO="100000")
	for k in ("RIG_JOB", "RIG_EXTRA"):
		env.pop(k, None)
	os.makedirs(env["RIG_ROOT"])

	class Run:
		root = tmp_path / "root"
		lock = tmp_path / "root" / "gpu.lock"

		def __call__(self, *args, **kw):
			return subprocess.run([sys.executable, RIG] + list(args), env=dict(env, **kw.pop("env", {})),
				cwd=str(tmp_path), capture_output=True, text=True, timeout=120, **kw)

		def popen(self, *args, **kw):
			return subprocess.Popen([sys.executable, RIG] + list(args), env=dict(env, **kw.pop("env", {})),
				cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)

		def log(self, tag):
			return (tmp_path / (tag + ".rig.log")).read_text()

		def until_held(self, p):
			# rig.py's output up to the line saying the job runs
			out = []
			for line in p.stdout:
				out.append(line)
				if " rig held by " in line:
					return out
			raise AssertionError("".join(out))

	r = Run()
	r.env, r.tmp = env, tmp_path
	return r


@runs
def test_run_holds_gpu_lock_and_removes_it(run):
	job = "import os, sys, time; time.sleep(0.5); print(open(sys.argv[1]).read().strip()); print('job', os.environ['RIG_JOB'])"
	r = run("run", "s1", "held", "--", sys.executable, "-c", job, str(run.lock))
	assert r.returncode == 0, r.stdout + r.stderr
	held = [l for l in r.stdout.splitlines() if l.startswith("s1 held since")]
	jobname = [l for l in r.stdout.splitlines() if l.startswith("job ")][0].split()[1]
	assert held and " job=%s pid=" % jobname in held[0], r.stdout
	assert not run.lock.exists()
	assert last(run.log("held")).startswith("RIG_CLEAN"), run.log("held")


@runs
def test_failed_job_releases_the_lock_and_returns_its_code(run):
	r = run("run", "s1", "fails", "--", sys.executable, "-c", "import sys; sys.exit(3)")
	assert r.returncode == 3
	assert not run.lock.exists()
	assert "job exit 3" in run.log("fails")


@runs
def test_one_run_at_a_time(run):
	out = run.tmp / "order.txt"
	job = ("import sys, time\nf = open(sys.argv[1], 'a')\nf.write('start %s\\n' % sys.argv[2]); f.flush()\n"
		"time.sleep(0.6)\nf.write('end %s\\n' % sys.argv[2]); f.close()")
	ps = [run.popen("run", "s%d" % i, "t%d" % i, "--", sys.executable, "-c", job, str(out), str(i)) for i in range(3)]
	for p in ps:
		assert p.wait(120) == 0, p.stdout.read()
	order = out.read_text().splitlines()
	assert len(order) == 6, order
	for i in range(0, 6, 2):  # no run started inside another
		assert order[i].split()[1] == order[i + 1].split()[1] and order[i].startswith("start"), order
	assert not run.lock.exists()


@runs
def test_stale_and_hand_written_locks(run):
	run.lock.write_text("ghost t since 2026-10-21 05:33:00 rigpid=%d rigstart=1\n" % dead_pid())
	s = run("status")
	assert s.stdout.startswith("rig free (stale gpu.lock cleared: ghost t"), s.stdout
	assert not run.lock.exists()
	run.lock.write_text("campy-live-speed 2026-10-21 04:00\n")
	r = run("run", "--no-wait", "s1", "t", "--", sys.executable, "-c", "pass")
	assert r.returncode == 75 and "campy-live-speed 2026-10-21 04:00" in r.stdout, r.stdout
	s = run("status")
	assert s.stdout.startswith("rig held: 'campy-live-speed 2026-10-21 04:00' (gpu.lock written by hand"), s.stdout
	assert "removed a hand-written gpu.lock" in run("break").stdout
	assert run("run", "--no-wait", "s1", "t", "--", sys.executable, "-c", "pass").returncode == 0


@runs
def test_a_lock_taken_over_mid_run_contaminates_it(run):
	job = "import sys, time; time.sleep(0.8); open(sys.argv[1], 'w').write('intruder 06:00\\n'); time.sleep(1.5)"
	r = run("run", "s1", "taken", "--", sys.executable, "-c", job, str(run.lock))
	assert r.returncode == 0, r.stdout + r.stderr
	log = run.log("taken")
	assert "LOCK changed by someone else during the run: intruder 06:00" in log, log
	assert "RIG_CONTAMINATED" in last(log) and re.search(r"\block [1-9]", last(log)), log
	assert run.lock.read_text() == "intruder 06:00\n"  # theirs now: left alone


@runs
def test_status_while_held(run):
	held = run.popen("run", "holder", "long", "--", sys.executable, "-c", "import time; time.sleep(3)")
	run.until_held(held)
	s = run("status")
	assert s.stdout.startswith("rig held: holder long since"), s.stdout
	r = run("run", "--no-wait", "s2", "second", "--", sys.executable, "-c", "pass")
	assert r.returncode == 75 and "holder long" in r.stdout, r.stdout
	assert held.wait(60) == 0
	assert run("status").stdout.startswith("rig free")


# The Windows API itself

@windows
def test_process_table_finds_this_process():
	table = {row[0]: row for row in rig_windows.process_table()}
	pid, ppid, name, start, cpu, io = table[os.getpid()]
	assert ppid == os.getppid() and name.lower().startswith("python") and cpu > 0
	assert start == rig_windows.process_start(os.getpid())
	assert rig_windows.process_start(dead_pid()) is None
	assert "pytest" in rig_windows.command_line(os.getpid(), start)


@windows
def test_cpu_and_io_of_another_process(tmp_path):
	big = tmp_path / "big"
	child = subprocess.Popen([sys.executable, "-c",
		"import sys, time\nt = time.time() + 1.5\nwhile time.time() < t: pass\n"
		"f = open(sys.argv[1], 'wb', buffering=0)\nfor i in range(40): f.write(b'x' * 1000000)\nf.close()\ntime.sleep(2)", str(big)])
	try:
		time.sleep(0.3)
		a = {r[0]: r for r in rig_windows.process_table()}[child.pid]
		time.sleep(2.5)
		b = {r[0]: r for r in rig_windows.process_table()}[child.pid]
		assert b[4] - a[4] > 0.8, (a, b)  # it spun about 1.2 s of CPU
		assert b[5] - a[5] >= 40e6, (a, b)  # and wrote 40 MB
	finally:
		child.wait()


@windows
def test_dlls_and_open_devices_of_another_process(monkeypatch):
	# Stand-ins: the DLL of Python's ctypes for Spinnaker, the NUL device for a COM port
	monkeypatch.setattr(rig_windows, "CAMERA_MODULES", ["_ctypes*.pyd"])
	child = subprocess.Popen([sys.executable, "-c",
		"import ctypes, os, sys, time\nf = open(os.devnull, 'r+b')\nprint('ready', flush=True)\ntime.sleep(20)"],
		stdout=subprocess.PIPE, text=True)
	try:
		assert child.stdout.readline().strip() == "ready"
		table = rig_windows.process_table()
		assert child.pid in rig_windows.camera_pids(table)
		users = rig_windows.port_users(table, own=(), extra={"\\Device\\Null": "trigger"})
		assert users.get(child.pid) == {"trigger"}, users
		assert child.pid not in rig_windows.port_users(table, own={child.pid}, extra={"\\Device\\Null": "trigger"})
	finally:
		child.kill()
		child.wait()


@windows
def test_job_object_holds_what_the_job_starts():
	code = "import subprocess, sys, time; c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)']); print(c.pid, flush=True); time.sleep(5)"
	p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, creationflags=0x4)
	job = rig_windows.Job()
	try:
		assert job.add(p.pid)
		rig_windows.resume(p.pid)
		grandchild = int(p.stdout.readline())
		assert {p.pid, grandchild} <= job.pids()
		job.terminate()
		assert p.wait(10) != 0
		t = time.monotonic() + 5
		while rig_windows.process_start(grandchild) is not None and time.monotonic() < t:
			time.sleep(0.1)
		assert rig_windows.process_start(grandchild) is None
	finally:
		job.close()


@windows
def test_overlap_is_caught_on_windows(run):
	held = run.popen("run", "s1", "overlap", "--", sys.executable, "-c", "import time; time.sleep(4)")
	run.until_held(held)
	time.sleep(0.5)
	other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
	other.wait()
	assert held.wait(60) == 0, held.stdout.read()
	log = run.log("overlap")
	assert re.search(r"FOREIGN %d gpu .*python" % other.pid, log, re.I), log
	assert last(log).startswith("RIG_CONTAMINATED") and re.search(r"\bgpu [1-9]", last(log)), log


@windows
def test_guarded_powershell_script_takes_the_rig_once(run):
	for f in ("rig.py", "rig_windows.py", "rig_guard.ps1"):
		(run.root / f).write_text(open(os.path.join(BENCH, f)).read())
	first = '. "%s" @args; if ($null -ne $RigExit) { exit $RigExit }\n' % (run.root / "rig_guard.ps1")
	inner = run.tmp / "inner.ps1"
	inner.write_text(first + '"inner $env:RIG_JOB"\nexit 4\n')
	outer = run.tmp / "outer.ps1"
	outer.write_text(first + '"outer $env:RIG_JOB $($args -join \'+\')"\n'
		'& powershell -NoProfile -ExecutionPolicy Bypass -File "%s"\nexit $LASTEXITCODE\n' % inner)
	env = dict(run.env, RIG_OWNER="tester", RIG_PYTHON=sys.executable)
	ps = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass"]
	# Run as powershell -File, and from a command (the guard can't end that PowerShell itself)
	for how, args in (("file", ["-File", str(outer), "t1", "two words"]),
			("command", ["-Command", "& '%s' t1 'two words'; 'after ' + $LASTEXITCODE" % outer])):
		r = subprocess.run(ps + args, env=env, capture_output=True, text=True, timeout=120)
		lines = [l for l in r.stdout.splitlines() if l.startswith(("outer", "inner"))]
		assert len(lines) == 2 and lines[0].split()[1] == lines[1].split()[1] != "", (how, r.stdout + r.stderr)
		assert lines[0].endswith("t1+two words"), lines
		if how == "file":
			assert r.returncode == 4, r.stdout + r.stderr
		else:
			assert "after 4" in r.stdout, r.stdout + r.stderr
		log = (run.tmp / "t1.rig.log").read_text()
		assert log.count("rig held by tester for t1") == (1 if how == "file" else 2) and "job exit 4" in log, log
		assert not run.lock.exists()


@windows
def test_guard_ends_a_file_script_that_lacks_the_exit(run):
	for f in ("rig.py", "rig_windows.py", "rig_guard.ps1"):
		(run.root / f).write_text(open(os.path.join(BENCH, f)).read())
	script = run.tmp / "bare.ps1"
	script.write_text('. "%s" @args\n"body $env:RIG_JOB"\nexit 5\n' % (run.root / "rig_guard.ps1"))
	r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "t2"],
		env=dict(run.env, RIG_OWNER="tester", RIG_PYTHON=sys.executable), capture_output=True, text=True, timeout=120)
	body = [l for l in r.stdout.splitlines() if l.startswith("body")]
	assert r.returncode == 5 and len(body) == 1 and body[0].startswith("body t2-"), r.stdout + r.stderr  # in the job, once
