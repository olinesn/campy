"""bench/rig.py, the one-test-at-a-time lock for a shared Linux test machine.

A temporary file stands in for the GPU (RIG_EXTRA), so no device is needed. Whole runs need Linux
(/proc); the lock in-process (a flock) runs on macOS too, and what rig.py prints on any OS.
"""

import os, re, shlex, shutil, signal, subprocess, sys, threading, time

import pytest

BENCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench")
RIG = os.path.join(BENCH, "rig.py")
sys.path.insert(0, BENCH)
import rig as riglib  # noqa: E402

linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="rig.py reads Linux's /proc")
posix = pytest.mark.skipif(os.name != "posix", reason="the Linux lock is a flock")
HOLD_OPEN = "import sys, time; f = open(sys.argv[1]); time.sleep(float(sys.argv[2]))"
WAIT_FOR = "import os, sys, time\nwhile not os.path.exists(sys.argv[1]): time.sleep(0.05)"  # a job that ends when told


def last(log):
	# the log's last line, without its time
	return log.strip().splitlines()[-1].split(" ", 1)[1]


@pytest.fixture
def rig(tmp_path):
	gpu = tmp_path / "fake-kfd"
	gpu.write_text("")
	env = dict(os.environ, RIG_ROOT=str(tmp_path / "root"), RIG_EXTRA="gpu=%s" % gpu,
		RIG_PERIOD="0.3", RIG_CPU="1000", RIG_IO="100000")
	env.pop("RIG_JOB", None)
	os.makedirs(env["RIG_ROOT"])

	class Rig:
		root = tmp_path / "root"

		def __call__(self, *args, **kw):
			return subprocess.run([sys.executable, RIG] + list(args), env=dict(env, **kw.pop("env", {})),
				cwd=str(tmp_path), capture_output=True, text=True, timeout=60, **kw)

		def popen(self, *args):
			return subprocess.Popen([sys.executable] + list(args), env=env, cwd=str(tmp_path))

		def log(self, tag):
			return (tmp_path / (tag + ".rig.log")).read_text()

	r = Rig()
	r.gpu, r.env = str(gpu), env
	return r


@linux
def test_clean_run_and_its_own_device_use(rig):
	# The job opens the GPU itself, even from a process in a new session: not foreign
	job = "import subprocess, sys; subprocess.run(['setsid', sys.executable, '-c', %r, %r, '1.5'])" % (HOLD_OPEN, rig.gpu)
	r = rig("run", "tester", "clean", "--", sys.executable, "-c", job)
	assert r.returncode == 0, r.stdout + r.stderr
	log = rig.log("clean")
	assert "FOREIGN" not in log and last(log).startswith("RIG_CLEAN"), log
	assert (rig.root / "RIG_BUSY").read_text() == ""


@linux
def test_overlap_is_caught(rig):
	r = rig.popen(RIG, "run", "tester", "overlap", "--", "sleep", "3")
	time.sleep(1.5)  # the rig is taken; then another job opens the GPU without it
	other = rig.popen("-c", HOLD_OPEN, rig.gpu, "1")
	other.wait()
	assert r.wait(30) == 0
	log = rig.log("overlap")
	assert "FOREIGN %d gpu" % other.pid in log, log
	assert last(log).startswith("RIG_CONTAMINATED") and re.search(r"\bgpu [1-9]", last(log)), log


@linux
def test_cpu_load_outside_the_job_is_caught(rig):
	spin = rig.popen("-c", "import time; t = time.time() + 4\nwhile time.time() < t: pass")
	try:
		r = rig("run", "--cpu", "0.1", "--no-wait", "tester", "busy", "--", "true")
		assert r.returncode == 75 and "load" in r.stdout, r.stdout + r.stderr
	finally:
		spin.wait()


@linux
def test_no_wait_while_held_and_status(rig):
	held = rig.popen(RIG, "run", "holder", "long", "--", "sleep", "3")
	time.sleep(1.5)
	r = rig("run", "--no-wait", "tester", "second", "--", "true")
	assert r.returncode == 75 and "holder long" in r.stdout, r.stdout + r.stderr
	s = rig("status")
	assert s.stdout.startswith("rig held: holder long"), s.stdout
	assert held.wait(30) == 0
	assert rig("status").stdout.startswith("rig free")


@linux
def test_a_device_user_without_the_rig_makes_run_wait(rig):
	other = rig.popen("-c", HOLD_OPEN, rig.gpu, "2")
	time.sleep(0.5)
	t = time.monotonic()
	r = rig("run", "tester", "after", "--", "true")
	assert r.returncode == 0 and "in use outside rig.py" in r.stdout, r.stdout
	assert time.monotonic() - t > 1.0
	other.wait()


@linux
def test_stale_busy_note_is_cleared(rig):
	(rig.root / "RIG_BUSY").write_text("ghost old since 2026-01-01 rigpid=1 job=old-1 pid=1\n")
	s = rig("status")
	assert s.stdout.startswith("rig free (stale RIG_BUSY cleared"), s.stdout
	assert (rig.root / "RIG_BUSY").read_text() == ""


@linux
def test_hold_of_an_ended_pid_is_refused(rig):
	p = subprocess.Popen(["true"])
	p.wait()
	r = rig("hold", "tester", "gone", str(p.pid))
	assert r.returncode != 0 and "not running" in r.stderr


@linux
def test_hold_lasts_until_the_pid_ends(rig):
	job = subprocess.Popen(["sleep", "1.5"])
	r = rig("hold", "tester", "held", str(job.pid))
	assert r.returncode == 0, r.stderr
	assert last(rig.log("held")).startswith("RIG_CLEAN")


@linux
def test_guarded_script_takes_the_rig_once(rig, tmp_path):
	# rig_guard.sh re-runs a script under rig.py; a guarded script it calls does not wait on itself
	root = rig.root
	for f in ("rig.py", "rig_guard.sh"):
		(root / f).write_text(open(os.path.join(BENCH, f)).read())
	inner = tmp_path / "inner.sh"
	inner.write_text('. "$RIG_ROOT/rig_guard.sh" "$@"\necho "inner $RIG_JOB"\n')
	outer = tmp_path / "outer.sh"
	outer.write_text('. "$RIG_ROOT/rig_guard.sh" "$@"\necho "outer $RIG_JOB"\nbash %s\n' % inner)
	r = subprocess.run(["bash", str(outer), "t1"], env=dict(rig.env, RIG_OWNER="tester"),
		capture_output=True, text=True, timeout=60)
	assert r.returncode == 0, r.stdout + r.stderr
	lines = [l for l in r.stdout.splitlines() if l.startswith(("outer", "inner"))]
	assert len(lines) == 2 and lines[0].split()[1] == lines[1].split()[1] != "", r.stdout
	assert last((tmp_path / "t1.rig.log").read_text()).startswith("RIG_CLEAN")
	assert "rig held by tester for t1" in (tmp_path / "t1.rig.log").read_text()


@linux
def test_a_desktop_is_noted_not_waited_for_and_one_starting_mid_run_is_caught(rig, tmp_path):
	# A desktop drawing on the GPU (here a stand-in named Xwayland) never goes away, so run does not
	# wait for it; but one that starts during a run changes what the run measured
	xwayland = tmp_path / "Xwayland"
	shutil.copy(shutil.which("sleep"), str(xwayland))
	up = subprocess.Popen([str(xwayland), "6"])
	try:
		r = rig("run", "tester", "desk", "--", "sleep", "1")
		assert r.returncode == 0 and "in use outside" not in r.stdout, r.stdout
		assert "desktop on the GPU: Xwayland" in rig.log("desk"), rig.log("desk")
		assert last(rig.log("desk")).startswith("RIG_CLEAN (desktop on the GPU: Xwayland"), rig.log("desk")
	finally:
		up.kill()
		up.wait()
	run = rig.popen(RIG, "run", "tester", "desk2", "--", "sleep", "2.5")
	time.sleep(1.2)
	late = subprocess.Popen([str(xwayland), "0.6"])
	late.wait()
	assert run.wait(30) == 0
	log = rig.log("desk2")
	assert "DESKTOP started: %d" % late.pid in log and "RIG_CONTAMINATED" in last(log), log


@linux
def test_a_logged_in_gnome_desktops_helpers_are_desktop_not_device_users(rig, tmp_path):
	# Logged in, GNOME's gsd-xsettings and mutter-x11-frames keep the render node open for the whole
	# session; stand-ins named like them hold the fake GPU open, and run neither waits nor is contaminated.
	# mutter-x11-frames is longer than the 15 characters the kernel keeps of a name, so this checks that too
	helpers = []
	with open(rig.gpu) as dev:
		for name in ("gsd-xsettings", "mutter-x11-frames"):
			exe = tmp_path / name
			shutil.copy(shutil.which("sleep"), str(exe))
			helpers.append(subprocess.Popen([str(exe), "30"], stdin=dev))
	try:
		r = rig("run", "tester", "gnome", "--", "sleep", "1", env={"RIG_NOWAIT": "1"})
		log = rig.log("gnome")
		assert r.returncode == 0 and "in use outside" not in r.stdout, r.stdout
		assert last(log).startswith("RIG_CLEAN (desktop on the GPU:"), log
		assert "gsd-xsettings" in log and "mutter-x11-frames" in log, log
	finally:
		for h in helpers:
			h.kill()
			h.wait()


# rig.py, slowed down between starting the job and going on: as when the machine is busy and
# rig.py gets its next time slice only after the job has run its first lines
SLOW_START = """import sys, time
sys.path.insert(0, sys.argv.pop(1))
import rig
start_job = rig.start_job
def slow_start_job(*args):
	proc = start_job(*args)
	time.sleep(1.0)
	return proc
rig.start_job = slow_start_job
rig.main()
"""


@linux
def test_queued_guarded_jobs_each_start_when_rig_py_is_slow(rig, tmp_path):
	# Several guarded scripts wait for the rig while another job holds it. Each, once it has the
	# rig, must run, even when it checks the lock before its rig.py has gone on past starting it:
	# the lock has to name its job already, or the script re-runs itself under rig.py and waits
	# forever on the rig its own rig.py holds
	root = rig.root
	for f in ("rig.py", "rig_guard.sh"):
		(root / f).write_text(open(os.path.join(BENCH, f)).read())
	slow = tmp_path / "slow_rig.py"
	slow.write_text(SLOW_START)
	job = tmp_path / "job.sh"
	job.write_text('. "$RIG_ROOT/rig_guard.sh" "$@"\necho "ran $1 $RIG_JOB"\n')
	holder = rig.popen(RIG, "run", "holder", "first", "--", "sleep", "2")
	time.sleep(1.0)
	waiters = []
	try:
		for i in range(3):
			waiters.append(subprocess.Popen([sys.executable, str(slow), str(root), "run", "waiter", "w%d" % i,
				"--", "bash", str(job), "w%d" % i], env=rig.env, cwd=str(tmp_path),
				stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
			time.sleep(0.2)
		assert holder.wait(30) == 0
		for i, w in enumerate(waiters):
			try:
				out = w.communicate(timeout=30)[0]
			except subprocess.TimeoutExpired:
				pytest.fail("w%d never ran its job: %s" % (i, (root / "RIG_BUSY").read_text()))
			assert w.returncode == 0 and "ran w%d w%d-%d" % (i, i, w.pid) in out, out
			assert "waiting" not in out.split("rig held by", 1)[1], out
			assert last(rig.log("w%d" % i)).startswith("RIG_CLEAN"), rig.log("w%d" % i)
	finally:
		for w in waiters:
			if w.poll() is None:
				w.terminate()
				w.communicate(timeout=60)
		holder.wait(30)
	assert (root / "RIG_BUSY").read_text() == ""


@linux
def test_run_inside_the_rigs_job_runs_at_once(rig):
	# rig.py run called by the job that holds the rig runs its command in that job, without waiting
	inner = "%s %s run tester inner -- %s -c \"import os; print('inner', os.environ['RIG_JOB'])\"" % (
		shlex.quote(sys.executable), shlex.quote(RIG), shlex.quote(sys.executable))
	r = rig("run", "tester", "outer", "--", "bash", "-c", 'echo "outer $RIG_JOB"; ' + inner)
	assert r.returncode == 0, r.stdout + r.stderr
	jobs = [l.split()[1] for l in r.stdout.splitlines() if l.startswith(("outer ", "inner "))]
	assert len(jobs) == 2 and jobs[0] == jobs[1] != "", r.stdout
	assert "already inside the rig's job" in r.stdout, r.stdout
	assert last(rig.log("outer")).startswith("RIG_CLEAN"), rig.log("outer")


# rig.py ending, or being stopped, while its job runs: the rig must never read free while the job
# runs, and a run not watched to its end must never end RIG_CLEAN

def text(path):
	try:
		return path.read_text()
	except OSError:
		return ""


def busy(rig):
	return text(rig.root / "RIG_BUSY")


def job_pid(rig, timeout=30):
	# the job's pid, once RIG_BUSY names it
	t = time.monotonic() + timeout
	while time.monotonic() < t:
		m = re.search(r"\bpid=(\d+) pidstart=", busy(rig))
		if m:
			return int(m.group(1))
		time.sleep(0.05)
	raise AssertionError("RIG_BUSY never named a job: %r" % busy(rig))


def runs(pid):
	try:
		with open("/proc/%d/stat" % pid) as f:
			return f.read().rsplit(")", 1)[1].split()[0] != "Z"
	except OSError:
		return False


def until(check, timeout=30):
	t = time.monotonic() + timeout
	while not check():
		if time.monotonic() > t:
			raise AssertionError("timed out")
		time.sleep(0.05)


@linux
def test_a_killed_rig_py_keeps_the_rig_until_its_job_ends(rig, tmp_path):
	# SIGKILL (the OOM killer, a tool's timeout): the kernel drops rig.py's flock, but its job runs on
	go = tmp_path / "go"
	r = rig.popen(RIG, "run", "tester", "killed", "--", sys.executable, "-c", WAIT_FOR, str(go))
	job = job_pid(rig)
	r.kill()
	r.wait()
	assert runs(job)
	s = rig("status")
	assert s.stdout.startswith("rig held: tester killed since") and "its rig.py has ended" in s.stdout, s.stdout
	r2 = rig("run", "--no-wait", "tester", "early", "--", "true")
	assert r2.returncode == 75 and "its rig.py has ended" in r2.stdout, r2.stdout + r2.stderr
	b = rig("break")
	assert b.returncode != 0 and "stop it instead" in b.stderr, b.stdout + b.stderr
	# A run that waits for the job says so in RIG_BUSY, which still names the job
	second = subprocess.Popen([sys.executable, RIG, "run", "tester2", "second", "--", "true"], env=rig.env,
		cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
	try:
		until(lambda: "tester2 second waits for it" in busy(rig))
		assert "job=killed-%d pid=%d " % (r.pid, job) in busy(rig), busy(rig)
		assert rig("status").stdout.startswith("rig held: tester killed")
		time.sleep(1.0)
		assert second.poll() is None and not (tmp_path / "second.rig.log").exists()
		go.touch()
		out = second.communicate(timeout=60)[0]
	finally:
		go.touch()
		if second.poll() is None:
			second.kill()
	assert second.returncode == 0, out
	assert "its rig.py has ended" in out, out
	assert last(rig.log("second")).startswith("RIG_CLEAN")
	# A log from the start (rig.py writes its first line before the job starts, so even a rig.py killed
	# the moment RIG_BUSY named the job left one), and no verdict: its numbers can't be trusted
	killed = rig.log("killed")
	assert killed.splitlines()[0].split(" ", 1)[1].startswith("rig.py ") and "RIG_" not in killed, killed
	assert busy(rig) == "" and rig("status").stdout.startswith("rig free")


@linux
def test_a_killed_run_whose_job_ended_leaves_a_free_rig(rig, tmp_path):
	go = tmp_path / "go"
	r = rig.popen(RIG, "run", "tester", "killed", "--", sys.executable, "-c", WAIT_FOR, str(go))
	job = job_pid(rig)
	r.kill()
	r.wait()
	go.touch()
	until(lambda: not runs(job))
	s = rig("status")
	assert s.stdout.startswith("rig free (stale RIG_BUSY cleared: tester killed"), s.stdout
	assert rig("run", "--no-wait", "tester", "after", "--", "true").returncode == 0


@linux
def test_a_closed_output_does_not_end_the_run(rig, tmp_path):
	# rig.py run ... | head, or an ssh whose local end was cut: what rig.py prints goes nowhere, and the
	# run goes on holding the rig, logging, to the job's end
	go = tmp_path / "go"
	r = subprocess.Popen([sys.executable, RIG, "run", "tester", "piped", "--", sys.executable, "-c", WAIT_FOR, str(go)],
		env=rig.env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
	r.stdout.close()
	r.stdout = None  # communicate() must not read it (Windows reads it in a thread)
	try:
		job_pid(rig)
		other = rig.popen("-c", HOLD_OPEN, rig.gpu, "1")  # a FOREIGN line for rig.py to log, and print
		other.wait()
		until(lambda: "FOREIGN %d" % other.pid in text(tmp_path / "piped.rig.log"))
		time.sleep(0.5)
		assert r.poll() is None, r.stderr.read()
		assert rig("status").stdout.startswith("rig held: tester piped")
		assert rig("run", "--no-wait", "tester", "early", "--", "true").returncode == 75
		go.touch()
		err = r.communicate(timeout=60)[1]
	finally:
		go.touch()
		if r.poll() is None:
			r.kill()
	assert r.returncode == 0 and not err, err
	log = rig.log("piped")
	assert "job exit 0" in log and last(log).startswith("RIG_CONTAMINATED"), log


FAILING_WATCH = """import sys
sys.path.insert(0, sys.argv.pop(1))
import rig
def broken(self):
	raise RuntimeError("a bug in the watch")
rig.Clocks.sample = broken
rig.main()
"""


@linux
def test_an_error_in_rig_py_keeps_the_rig_until_the_job_ends(rig, tmp_path):
	failing = tmp_path / "failing_rig.py"
	failing.write_text(FAILING_WATCH)
	go = tmp_path / "go"
	r = subprocess.Popen([sys.executable, str(failing), BENCH, "run", "tester", "broken", "--", sys.executable, "-c",
		WAIT_FOR, str(go)], env=rig.env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
	try:
		until(lambda: "rig.py failed" in text(tmp_path / "broken.rig.log"))
		assert r.poll() is None
		assert rig("status").stdout.startswith("rig held: tester broken")
		assert rig("run", "--no-wait", "tester", "early", "--", "true").returncode == 75
		go.touch()
		err = r.communicate(timeout=60)[1]
	finally:
		go.touch()
		if r.poll() is None:
			r.kill()
	assert r.returncode == 1 and "RuntimeError: a bug in the watch" in err, err
	log = rig.log("broken")
	assert "job exit 0" in log, log
	assert last(log) == "RIG_ABORTED by an error in rig.py (RuntimeError: a bug in the watch): the job ran on, unwatched, to its end", log
	assert busy(rig) == ""


@linux
def test_a_stopped_run_ends_aborted_not_clean(rig):
	r = rig.popen(RIG, "run", "tester", "stopped", "--", "sleep", "30")
	job = job_pid(rig)
	r.terminate()
	assert r.wait(60) == 128 + signal.SIGTERM
	assert not runs(job)
	log = rig.log("stopped")
	assert "job exit -%d" % signal.SIGTERM in log and last(log).startswith("RIG_ABORTED by SIGTERM (until then: clean"), log


@linux
@pytest.mark.skipif(not shutil.which("nohup"), reason="no nohup")
def test_a_nohup_run_survives_a_hangup(rig, tmp_path):
	go = tmp_path / "go"
	r = subprocess.Popen(["nohup", sys.executable, RIG, "run", "tester", "nohup", "--", sys.executable, "-c", WAIT_FOR, str(go)],
		env=rig.env, cwd=str(tmp_path), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
	try:
		job = job_pid(rig)
		r.send_signal(signal.SIGHUP)  # the terminal or the ssh closing
		time.sleep(1.0)
		assert r.poll() is None and runs(job)
		go.touch()
		out = r.communicate(timeout=60)[0]
	finally:
		go.touch()
		if r.poll() is None:
			r.kill()
	assert r.returncode == 0, out
	assert last(rig.log("nohup")).startswith("RIG_CLEAN"), rig.log("nohup")


@linux
def test_a_stopped_hold_ends_aborted_and_a_killed_one_keeps_the_rig(rig):
	held = subprocess.Popen(["sleep", "60"])
	try:
		h = rig.popen(RIG, "hold", "tester", "held", str(held.pid))
		until(lambda: "pid=%d pidstart=" % held.pid in busy(rig))
		h.terminate()
		h.wait(30)
		assert held.poll() is None
		assert last(rig.log("held")).startswith("RIG_ABORTED by SIGTERM; the held pid was left running (until then: clean"), rig.log("held")
		assert rig("status").stdout.startswith("rig free")
		# rig.py hold killed: the held pid keeps the rig until it ends
		h = rig.popen(RIG, "hold", "tester", "held2", str(held.pid))
		until(lambda: "held2" in busy(rig) and "pid=%d pidstart=" % held.pid in busy(rig))
		h.kill()
		h.wait()
		s = rig("status")
		assert s.stdout.startswith("rig held: tester held2") and "its rig.py has ended" in s.stdout, s.stdout
	finally:
		held.kill()
		held.wait()
	assert rig("status").stdout.startswith("rig free")


@linux
def test_rig_busy_names_a_run_while_it_waits_for_quiet(rig):
	other = rig.popen("-c", HOLD_OPEN, rig.gpu, "4")
	time.sleep(0.5)
	r = rig.popen(RIG, "run", "tester", "queued", "--", "true")
	try:
		until(lambda: "tester queued" in busy(rig))
		s = rig("status")
		assert s.stdout.startswith("rig held: tester queued since") and "waiting for the rig to be quiet" in s.stdout, s.stdout
	finally:
		other.wait()
	assert r.wait(30) == 0


# The same in-process (macOS too)

@pytest.fixture
def flock_root(tmp_path, monkeypatch):
	monkeypatch.setattr(riglib, "ROOT", str(tmp_path))
	monkeypatch.setattr(riglib, "LOCK", str(tmp_path / ".rig.lock"))
	monkeypatch.setattr(riglib, "INFO", str(tmp_path / "RIG_BUSY"))
	monkeypatch.setattr(riglib, "PERIOD", 0.05)
	monkeypatch.delenv("RIG_LOCK", raising=False)
	return tmp_path


@posix
def test_the_flock_names_its_run_at_once_and_a_killed_runs_job_holds_it(flock_root, capsys):
	job = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
	try:
		a = riglib.Flock()
		a.acquire("s1", "t1", no_wait=True)
		assert re.match(r"s1 t1 since \d{4}-\d\d-\d\d \d\d:\d\d:\d\d rigpid=%d$" % os.getpid(), a.text()), a.text()
		a.record("t1-1", job.pid)
		assert " job=t1-1 pid=%d pidstart=" % job.pid in a.text()
		os.close(a.fd)  # its rig.py killed: the kernel drops the flock, RIG_BUSY stays
		b = riglib.Flock()
		state, text = b.state()
		assert state == "held" and "its rig.py has ended" in b.describe(state, text)
		with pytest.raises(SystemExit) as e:
			b.acquire("s2", "t2", no_wait=True)
		assert e.value.code == 75 and "its rig.py has ended" in capsys.readouterr().out
		assert not b.clear_stale() and " job=t1-1 " in b.text()
		t = threading.Thread(target=b.acquire, args=("s2", "t2", False))
		t.start()
		until(lambda: "s2 t2 waits for it" in b.text())
		assert " job=t1-1 pid=%d pidstart=" % job.pid in b.text()  # a guarded script of the job finds it
		job.kill()
		job.wait()
		t.join(10)
		assert not t.is_alive() and b.text().startswith("s2 t2 since ") and "job=" not in b.text(), b.text()
		b.release()
		assert riglib.Flock().state() == ("free", "")
	finally:
		job.kill()
		job.wait()


@posix
def test_a_rig_busy_whose_job_ended_is_stale(flock_root):
	gone = subprocess.Popen([sys.executable, "-c", "pass"])
	gone.wait()
	(flock_root / "RIG_BUSY").write_text("ghost t since 2026-11-04 01:00:00 rigpid=1 job=t-1 pid=%d pidstart=5\n" % gone.pid)
	lock = riglib.Flock()
	assert lock.state()[0] == "free" and lock.clear_stale() and lock.text() == ""


SIGNALS = """import signal, sys
sys.path.insert(0, sys.argv[1])
import rig
rig.catch_signals()
print(*[signal.getsignal(getattr(signal, n)) is signal.SIG_IGN for n in ("SIGHUP", "SIGTERM", "SIGINT")])
"""


@posix
def test_a_signal_ignored_at_start_stays_ignored():
	# nohup ignores SIGHUP for rig.py; catching it would let a hangup stop the run after all
	def start(hup):
		return lambda: signal.signal(signal.SIGHUP, hup)
	plain = subprocess.run([sys.executable, "-c", SIGNALS, BENCH], capture_output=True, text=True, preexec_fn=start(signal.SIG_DFL))
	nohup = subprocess.run([sys.executable, "-c", SIGNALS, BENCH], capture_output=True, text=True, preexec_fn=start(signal.SIG_IGN))
	assert plain.stdout.split() == ["False", "False", "False"], plain.stdout + plain.stderr
	assert nohup.stdout.split() == ["True", "False", "False"], nohup.stdout + nohup.stderr


SAYS = """import sys, time
sys.path.insert(0, sys.argv[1])
import rig
time.sleep(0.5)
for i in range(3):
	rig.say("x" * 100000)
open(sys.argv[2], "w").write("went on")
"""


def test_what_rig_py_says_can_not_stop_it(tmp_path):
	# its output closed (rig.py run ... | head, a cut ssh): say goes on, and so does rig.py, any OS
	p = subprocess.Popen([sys.executable, "-c", SAYS, BENCH, str(tmp_path / "out")], stdout=subprocess.PIPE,
		stderr=subprocess.PIPE)
	p.stdout.close()
	p.stdout = None  # communicate() must not read it (Windows reads it in a thread)
	err = p.communicate(timeout=60)[1]
	assert p.returncode == 0 and (tmp_path / "out").read_text() == "went on", err
	assert not err, err
