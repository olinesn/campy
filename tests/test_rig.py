"""bench/rig.py, the one-test-at-a-time lock for a shared Linux test machine.

A temporary file stands in for the GPU (RIG_EXTRA), so no device is needed; Linux only (/proc).
"""

import os, re, shutil, subprocess, sys, time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="rig.py reads Linux's /proc")

BENCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench")
RIG = os.path.join(BENCH, "rig.py")
HOLD_OPEN = "import sys, time; f = open(sys.argv[1]); time.sleep(float(sys.argv[2]))"


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


def test_clean_run_and_its_own_device_use(rig):
	# The job opens the GPU itself, even from a process in a new session: not foreign
	job = "import subprocess, sys; subprocess.run(['setsid', sys.executable, '-c', %r, %r, '1.5'])" % (HOLD_OPEN, rig.gpu)
	r = rig("run", "tester", "clean", "--", sys.executable, "-c", job)
	assert r.returncode == 0, r.stdout + r.stderr
	log = rig.log("clean")
	assert "FOREIGN" not in log and last(log).startswith("RIG_CLEAN"), log
	assert (rig.root / "RIG_BUSY").read_text() == ""


def test_overlap_is_caught(rig):
	r = rig.popen(RIG, "run", "tester", "overlap", "--", "sleep", "3")
	time.sleep(1.5)  # the rig is taken; then another job opens the GPU without it
	other = rig.popen("-c", HOLD_OPEN, rig.gpu, "1")
	other.wait()
	assert r.wait(30) == 0
	log = rig.log("overlap")
	assert "FOREIGN %d gpu" % other.pid in log, log
	assert last(log).startswith("RIG_CONTAMINATED") and re.search(r"\bgpu [1-9]", last(log)), log


def test_cpu_load_outside_the_job_is_caught(rig):
	spin = rig.popen("-c", "import time; t = time.time() + 4\nwhile time.time() < t: pass")
	try:
		r = rig("run", "--cpu", "0.1", "--no-wait", "tester", "busy", "--", "true")
		assert r.returncode == 75 and "load" in r.stdout, r.stdout + r.stderr
	finally:
		spin.wait()


def test_no_wait_while_held_and_status(rig):
	held = rig.popen(RIG, "run", "holder", "long", "--", "sleep", "3")
	time.sleep(1.5)
	r = rig("run", "--no-wait", "tester", "second", "--", "true")
	assert r.returncode == 75 and "holder long" in r.stdout, r.stdout + r.stderr
	s = rig("status")
	assert s.stdout.startswith("rig held: holder long"), s.stdout
	assert held.wait(30) == 0
	assert rig("status").stdout.startswith("rig free")


def test_a_device_user_without_the_rig_makes_run_wait(rig):
	other = rig.popen("-c", HOLD_OPEN, rig.gpu, "2")
	time.sleep(0.5)
	t = time.monotonic()
	r = rig("run", "tester", "after", "--", "true")
	assert r.returncode == 0 and "in use outside rig.py" in r.stdout, r.stdout
	assert time.monotonic() - t > 1.0
	other.wait()


def test_stale_busy_note_is_cleared(rig):
	(rig.root / "RIG_BUSY").write_text("ghost old since 2026-01-01 rigpid=1 job=old-1 pid=1\n")
	s = rig("status")
	assert s.stdout.startswith("rig free (stale RIG_BUSY cleared"), s.stdout
	assert (rig.root / "RIG_BUSY").read_text() == ""


def test_hold_of_an_ended_pid_is_refused(rig):
	p = subprocess.Popen(["true"])
	p.wait()
	r = rig("hold", "tester", "gone", str(p.pid))
	assert r.returncode != 0 and "not running" in r.stderr


def test_hold_lasts_until_the_pid_ends(rig):
	job = subprocess.Popen(["sleep", "1.5"])
	r = rig("hold", "tester", "held", str(job.pid))
	assert r.returncode == 0, r.stderr
	assert last(rig.log("held")).startswith("RIG_CLEAN")


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
