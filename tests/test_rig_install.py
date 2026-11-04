"""bench/rig.py version and rig.py install: the lock's version, and installing a campy checkout's copy
as the machine's.

In-process tests (the lock a gpu.lock file, no process scan) run on any OS; whole runs of rig.py need
Linux or Windows, where it can see processes.
"""

import argparse, os, shutil, subprocess, sys

import pytest

BENCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench")
RIG = os.path.join(BENCH, "rig.py")
sys.path.insert(0, BENCH)
import rig as riglib  # noqa: E402

RUNS = sys.platform == "win32" or sys.platform.startswith("linux")
runs = pytest.mark.skipif(not RUNS, reason="rig.py reads processes on Linux and Windows only")
has_git = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def copy_lock(folder, extra=b""):
	"""The five lock files from bench/ in folder (rig_guard.ps1 with extra appended)."""
	os.makedirs(folder, exist_ok=True)
	for name in riglib.FILES:
		with open(os.path.join(BENCH, name), "rb") as f:
			data = f.read()
		with open(os.path.join(folder, name), "wb") as f:
			f.write(data + (extra if name == "rig_guard.ps1" else b""))
	return str(folder)


def args(**kw):
	a = dict(source=None, check=False, owner="tester", allow_dirty=False, allow_older=False, commit=None)
	a.update(kw)
	return argparse.Namespace(**a)


@pytest.fixture
def root(tmp_path, monkeypatch):
	# ROOT a temporary folder locked with gpu.lock, and no other rig.py processes
	r = tmp_path / "root"
	r.mkdir()
	monkeypatch.setattr(riglib, "ROOT", str(r))
	monkeypatch.setenv("RIG_LOCK", "file")
	monkeypatch.delenv("RIG_JOB", raising=False)
	monkeypatch.delenv("RIG_OWNER", raising=False)
	monkeypatch.setattr(riglib, "rig_processes", lambda procs=None: [])
	return r


# The version

def test_the_version_ignores_line_endings_and_follows_every_file(tmp_path):
	a = copy_lock(tmp_path / "a")
	b = tmp_path / "b"
	b.mkdir()
	for name in riglib.FILES:  # a Windows checkout (core.autocrlf): the same files with \r\n
		data = open(os.path.join(a, name), "rb").read()
		(b / name).write_bytes(data.replace(b"\n", b"\r\n"))
	assert riglib.version(a) == riglib.version(str(b)) and len(riglib.version(a)) == 10
	(b / "rig_hold.ps1").write_bytes(b"# changed\r\n")
	assert riglib.version(str(b)) != riglib.version(a)
	(b / "rig_hold.ps1").unlink()
	assert riglib.version(str(b)) != riglib.version(a)
	assert riglib.version(str(tmp_path / "nothing")) != riglib.version(str(b))


def test_version_text_says_whether_this_copy_is_the_installed_one(root, monkeypatch, capsys):
	monkeypatch.setattr(riglib, "HERE", copy_lock(root.parent / "checkout"))
	assert "NOT the installed one" in riglib.version_text() and "not installed with rig.py install" in riglib.version_text()
	copy_lock(root)
	assert "the same as the installed one" in riglib.version_text()
	monkeypatch.setattr(riglib, "HERE", str(root))
	riglib.cmd_version(None)
	assert capsys.readouterr().out.startswith("rig.py %s in %s" % (riglib.version(str(root)), root))


# Installing

def test_install_copies_every_file_backs_up_and_records_it(root, tmp_path, capsys):
	(root / "rig.py").write_text("# the old rig.py\n")
	old = riglib.version(str(root))
	src = copy_lock(tmp_path / "checkout" / "bench")
	riglib.cmd_install(args(source=str(tmp_path / "checkout")))  # a checkout: its bench folder
	out = capsys.readouterr().out
	new = riglib.version(src)
	assert riglib.version(str(root)) == new and "installed rig.py %s over %s" % (new, old) in out, out
	for name in riglib.FILES:
		assert (root / name).read_bytes() == open(os.path.join(src, name), "rb").read()
		assert not (root / (name + ".new")).exists()
	stamp = riglib.read_stamp()
	assert stamp["version"] == new and stamp["previous"] == old and stamp["by"] == "tester" and stamp["from"] == src
	backup = stamp["backup"]
	assert os.path.basename(backup).startswith(old + "-") and open(os.path.join(backup, "rig.py")).read() == "# the old rig.py\n"
	assert sorted(os.listdir(backup)) == ["rig.py"]
	assert "installed rig.py %s" % new in (root / "rig-install.log").read_text()
	assert not (root / "gpu.lock").exists()  # the rig is free again
	assert "installed " in riglib.installed_text() and "by tester" in riglib.installed_text()
	# The same again: nothing to do, nothing backed up
	riglib.cmd_install(args(source=src))
	assert "nothing to install" in capsys.readouterr().out
	assert len(os.listdir(root / "rig-backup")) == 1


def test_a_copy_changed_by_hand_after_install_says_so(root, tmp_path):
	riglib.cmd_install(args(source=copy_lock(tmp_path / "src"), commit="abc1234"))  # copied files: their commit, as said
	assert riglib.read_stamp()["commit"] == "abc1234 (said)" and "from campy abc1234 (said)" in riglib.installed_text()
	(root / "rig.py").write_text("# edited on the machine\n")
	assert "changed since rig.py install put" in riglib.installed_text()


def test_check_only_compares(root, tmp_path, capsys):
	src = copy_lock(tmp_path / "src")
	copy_lock(root, extra=b"# older\n")
	with pytest.raises(SystemExit) as e:
		riglib.cmd_install(args(source=src, check=True))
	out = capsys.readouterr().out
	assert e.value.code == 3 and "rig_guard.ps1    differs" in out and "rig.py           same" in out, out
	assert (root / "rig_guard.ps1").read_bytes().endswith(b"# older\n") and not (root / "rig-backup").exists()
	riglib.cmd_install(args(source=src))
	riglib.cmd_install(args(source=src, check=True))  # the same now: exit 0
	assert "nothing to install" in capsys.readouterr().out


def test_install_waits_for_nobody_a_busy_rig_changes_nothing(root, tmp_path, capsys):
	src = copy_lock(tmp_path / "src")
	(root / "rig.py").write_text("# old\n")
	(root / "gpu.lock").write_text("someone 2026-11-04 17:00\n")  # written by hand: held
	with pytest.raises(SystemExit) as e:
		riglib.cmd_install(args(source=src))
	assert e.value.code == 75 and "rig busy" in capsys.readouterr().out
	assert (root / "rig.py").read_text() == "# old\n" and (root / "gpu.lock").read_text() == "someone 2026-11-04 17:00\n"


def test_install_refuses_while_another_rig_py_runs_or_waits(root, tmp_path, monkeypatch, capsys):
	src = copy_lock(tmp_path / "src")
	(root / "rig.py").write_text("# old\n")
	monkeypatch.setattr(riglib, "rig_processes", lambda procs=None: [(4242, "python rig.py run s1 t1 -- x")])
	with pytest.raises(SystemExit) as e:
		riglib.cmd_install(args(source=src))
	out = capsys.readouterr().out
	assert e.value.code == 75 and "4242 python rig.py run s1 t1" in out, out
	assert (root / "rig.py").read_text() == "# old\n" and not (root / "gpu.lock").exists()


def test_install_inside_the_rigs_own_job_does_not_wait_for_it(root, tmp_path, monkeypatch, capsys):
	# An update script run under rig.py run installs the checkout's rig.py: the rig is its job's
	src = copy_lock(tmp_path / "src")
	held = "upd v1 since 2026-11-04 18:00:00 rigpid=%d rigstart=%d job=v1-1 pid=%d pidstart=%d\n" % (
		os.getpid(), riglib.proc_start(os.getpid()) or 0, os.getpid(), riglib.proc_start(os.getpid()) or 0)
	(root / "gpu.lock").write_text(held)
	monkeypatch.setenv("RIG_JOB", "v1-1")
	riglib.cmd_install(args(source=src))
	assert riglib.version(str(root)) == riglib.version(src)
	assert (root / "gpu.lock").read_text() == held  # left to the job's rig.py


def test_install_refuses_the_installed_copy_and_a_folder_without_rig_py(root, tmp_path, monkeypatch):
	copy_lock(root)
	monkeypatch.setattr(riglib, "HERE", str(root))
	with pytest.raises(SystemExit) as e:
		riglib.cmd_install(args())
	assert "is the installed copy itself" in str(e.value.code)
	with pytest.raises(SystemExit) as e:
		riglib.cmd_install(args(source=str(tmp_path)))
	assert "no rig.py" in str(e.value.code)
	part = tmp_path / "part"
	part.mkdir()
	(part / "rig.py").write_text("# rig.py alone\n")
	with pytest.raises(SystemExit) as e:
		riglib.cmd_install(args(source=str(part)))
	assert "has no rig_windows.py" in str(e.value.code)


def git(folder, *a):
	subprocess.run(["git", "-C", str(folder)] + list(a), check=True, capture_output=True,
		env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


@has_git
def test_install_refuses_uncommitted_changes_and_older_commits(root, tmp_path, capsys):
	repo = tmp_path / "campy"
	src = copy_lock(repo / "bench")
	git(repo, "init", "-q")
	git(repo, "add", ".")
	git(repo, "commit", "-q", "-m", "one")
	first = riglib.git(src, "rev-parse", "--short=9", "HEAD")
	with open(os.path.join(src, "rig_hold.ps1"), "a") as f:
		f.write("# two\n")
	with pytest.raises(SystemExit) as e:
		riglib.cmd_install(args(source=str(repo)))
	assert "uncommitted changes" in str(e.value.code) and not (root / "rig.py").exists()
	riglib.cmd_install(args(source=str(repo), allow_dirty=True))
	assert riglib.read_stamp()["commit"] == first + "+changes"
	git(repo, "commit", "-q", "-am", "two")
	with open(os.path.join(src, "rig_hold.ps1"), "a") as f:
		f.write("# three\n")
	git(repo, "commit", "-q", "-am", "three")
	third = riglib.git(src, "rev-parse", "--short=9", "HEAD")
	riglib.cmd_install(args(source=str(repo)))
	assert riglib.read_stamp()["commit"] == third
	git(repo, "checkout", "-q", first)  # an older commit
	with pytest.raises(SystemExit) as e:
		riglib.cmd_install(args(source=str(repo)))
	assert "is older than the installed" in str(e.value.code)
	riglib.cmd_install(args(source=str(repo), allow_older=True))
	assert riglib.read_stamp()["commit"] == first


# Finding other rig.py processes

class P:
	def __init__(self, pid, ppid, cmd):
		self.pid, self.ppid, self.cmd, self.start = pid, ppid, cmd, 0


def test_rig_processes_finds_runs_holds_and_rig_hold_but_not_its_own_parents(monkeypatch):
	monkeypatch.setattr(riglib, "WINDOWS", False)
	me = os.getpid()
	procs = {p.pid: p for p in [
		P(me, 10, "python3 rig.py install"),
		P(10, 11, "/usr/bin/python3 /home/x/sleap-bench/rig.py run upd v1 -- bash update.sh"),  # this one's parent
		P(11, 1, "bash"),
		P(20, 1, "/usr/bin/python3 /home/x/sleap-bench/rig.py run s1 t1 -- bash x.sh"),
		P(21, 1, '"C:\\py\\python.exe" "C:\\campy_data\\live\\rig.py" hold s2 t2 99'),
		P(22, 1, "powershell -File C:\\campy_data\\launchers\\rig_hold.ps1 -Owner u -Tag t"),
		P(23, 1, "python3 rig.py status"),
		P(24, 1, "vim rig.py"),
	]}
	assert sorted(pid for pid, _ in riglib.rig_processes(procs)) == [20, 21, 22]


# Whole runs

@runs
def test_status_and_every_rig_log_say_the_version(tmp_path):
	root = tmp_path / "root"
	gpu = tmp_path / "fake-gpu"
	gpu.write_text("")
	env = dict(os.environ, RIG_ROOT=str(root), RIG_LOCK="file", RIG_EXTRA="gpu=%s" % gpu, RIG_PERIOD="0.3",
		RIG_CPU="1000", RIG_IO="100000", RIG_OWNER="tester")
	env.pop("RIG_JOB", None)
	def rig(*a, script=RIG):
		return subprocess.run([sys.executable, script] + list(a), env=env, cwd=str(tmp_path), capture_output=True,
			text=True, timeout=120)
	r = rig("install", "--check")
	assert r.returncode == 3, r.stdout + r.stderr
	r = rig("install", "--allow-dirty")  # this checkout may have uncommitted changes
	assert r.returncode == 0 and "installed rig.py %s" % riglib.version(BENCH) in r.stdout, r.stdout + r.stderr
	installed = str(root / "rig.py")
	r = rig("status", script=installed)
	assert r.stdout.startswith("rig.py %s in %s, installed " % (riglib.version(BENCH), root)), r.stdout + r.stderr
	r = rig("run", "tester", "t1", "--", sys.executable, "-c", "pass", script=installed)
	assert r.returncode == 0, r.stdout + r.stderr
	log = (tmp_path / "t1.rig.log").read_text()
	assert "rig.py %s in %s, installed " % (riglib.version(BENCH), root) in log, log
	# A run from the checkout says it isn't the installed copy when they differ
	with open(root / "rig_hold.ps1", "a") as f:
		f.write("# edited\n")
	r = rig("run", "tester", "t2", "--", sys.executable, "-c", "pass")
	assert "NOT the installed one" in (tmp_path / "t2.rig.log").read_text()
