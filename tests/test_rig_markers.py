"""bench/rig.py on Windows never counts another rig.py as a GPU user, however it was started.

A python.exe counts as on the GPU unless it is another rig.py, which rig.py knew only by its command
line; but a rig.py at the desktop can't read the command line of a python.exe started over ssh
(elevated) or by another user. On the ChronicRecord PC (2026-11-20) a `rig.py status` run over ssh
every 10 min was counted against a 2 h run 11 times. Now every rig.py marks itself in
RIG_ROOT/rig-processes as it starts (mark_self), a watching rig.py exempts a marked python.exe
(rig_windows.classify), and one it can't tell yet is held back a few seconds, never exempted
(Watch.holds).

Pure tests with the Windows parts stubbed run on any OS; the last two use the Windows API itself.
"""

import os, subprocess, sys, time, types

import pytest

BENCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench")
RIG = os.path.join(BENCH, "rig.py")
sys.path.insert(0, BENCH)
import rig, rig_windows  # noqa: E402

# rig.py looks at the whole machine: these tests run with no other test beside them (conftest.py, 'alone')
pytestmark = pytest.mark.alone

WINDOWS = sys.platform == "win32"
windows = pytest.mark.skipif(not WINDOWS, reason="Windows API")
S = 10 ** 7  # one second in a Windows process start time (100 ns units)
T0 = 133_000_000_000_000_000  # a start time: 2022


def fake(pid, kinds=("gpu",), name="python.exe", start=T0, unsure=True):
	# a process as rig.py's scan gives it on Windows
	p = rig.Proc()
	p.pid, p.ppid, p.sid, p.start, p.cpu, p.io = pid, 1, None, start, 0.0, 0
	p.kinds, p.cmd, p.job, p.user, p.unsure = set(kinds), name, None, None, unsure
	return p


@pytest.fixture
def root(tmp_path, monkeypatch):
	monkeypatch.setattr(rig, "ROOT", str(tmp_path))
	monkeypatch.setattr(rig, "PERIOD", 0.05)
	monkeypatch.setattr(rig, "disk_bytes", lambda: None)
	monkeypatch.setattr(rig, "cmdline", lambda p: p.cmd)
	return tmp_path


def mark(root, pid, start):
	(root / rig.MARKS).mkdir(exist_ok=True)
	(root / rig.MARKS / ("%d-%d" % (pid, start))).write_text("rig.py status\n")


# Which python.exe is another rig.py

def test_classify_exempts_a_marked_python_and_flags_one_it_cant_tell(monkeypatch):
	monkeypatch.setattr(rig_windows, "GPU_NAMES", ["python*.exe"])
	table = [  # pid, ppid, name, start, cpu, io
		(10, 1, "python.exe", 100, 0.0, 0),  # rig.py status over ssh: command line unreadable, marked
		(11, 1, "python.exe", 101, 0.0, 0),  # unreadable, no marker
		(12, 1, "python.exe", 102, 0.0, 0),  # the marker is an earlier process's that had pid 12
		(13, 1, "python.exe", 103, 0.0, 0),  # marked, but has a camera open
		(14, 1, "python.exe", 104, 0.0, 0),  # marked, but a CUDA program (nvidia-smi)
		(15, 1, "python.exe", 105, 0.0, 0),  # readable: rig.py install
		(16, 1, "python.exe", 106, 0.0, 0),  # readable: not rig.py
		(17, 1, "pythonw.exe", 107, 0.0, 0),  # readable: rig.py version
	]
	cmds = {15: r'"C:\py\python.exe" C:\campy_data\live\rig.py install --check', 16: "python -m campy.live",
		17: r"pythonw C:\campy_data\live\rig.py version"}
	asked = []
	def cmdline(pid, start):
		asked.append(pid)
		return cmds.get(pid)
	recs = {r[0]: r for r in rig_windows.classify(table, gpu_pids={14}, camera_pids={13}, cmdline=cmdline,
		marks={(10, 100), (12, 50), (13, 103), (14, 104)})}
	assert {pid: r[5] for pid, r in recs.items()} == {10: set(), 11: {"gpu"}, 12: {"gpu"}, 13: {"camera", "gpu"},
		14: {"gpu"}, 15: set(), 16: {"gpu"}, 17: set()}
	# unsure: a python.exe that may be a rig.py yet to mark itself (rig.py's Watch holds it back a few seconds)
	assert {pid for pid, r in recs.items() if r[8]} == {11, 12}
	assert 10 not in asked  # a marker needs no command line


def test_rig_marks_lists_the_markers_and_nothing_else(root):
	assert rig.rig_marks() == set()  # no folder yet
	mark(root, 500, T0)
	mark(root, 501, T0 + 5)
	(root / rig.MARKS / "500-1.tmp").write_text("")
	(root / rig.MARKS / "notes.txt").write_text("")
	assert rig.rig_marks() == {(500, T0), (501, T0 + 5)}


# Marking itself

def test_a_rig_py_marks_itself_and_a_venv_launcher_of_it(root, monkeypatch):
	me = os.getpid()
	table = [  # pid, ppid, name, start, cpu, io
		(me, 700, "python.exe", T0 + 30, 0.0, 0),  # this rig.py
		(700, 600, "python.exe", T0 + 20, 0.0, 0),  # a venv's python.exe, which started it and waits for it
		(600, 1, "python.exe", T0 + 10, 0.0, 0),  # a Python script that ran it: no rig.py in its command line
	]
	cmds = {700: r"C:\venv\Scripts\python.exe C:\campy_data\live\rig.py status", 600: "python poll_rigs.py"}
	monkeypatch.setattr(rig, "WINDOWS", True)
	monkeypatch.setattr(rig, "rig_windows", rig_windows, raising=False)  # rig.py imports it on Windows only
	monkeypatch.setattr(rig_windows, "process_table", lambda: table)
	monkeypatch.setattr(rig_windows, "command_line", lambda pid, start: cmds.get(pid))
	assert rig.self_and_launchers() == [(me, T0 + 30), (700, T0 + 20)]
	rig.mark_self("status")
	assert rig.rig_marks() == {(me, T0 + 30), (700, T0 + 20)}
	assert (root / rig.MARKS / ("%d-%d" % (me, T0 + 30))).read_text().startswith("rig.py status (pid %d) since " % me)
	# a parent that started after this process is another process that reused its parent's pid
	table[1] = (700, 600, "python.exe", T0 + 40, 0.0, 0)
	assert rig.self_and_launchers() == [(me, T0 + 30)]


def test_no_marker_where_there_is_no_lock_folder(tmp_path, monkeypatch):
	monkeypatch.setattr(rig, "ROOT", str(tmp_path / "none"))
	monkeypatch.setattr(rig, "WINDOWS", True)
	monkeypatch.setattr(rig, "rig_windows", rig_windows, raising=False)
	monkeypatch.setattr(rig_windows, "process_table", lambda: [(os.getpid(), 1, "python.exe", T0, 0.0, 0)])
	rig.mark_self("status")
	assert not (tmp_path / "none").exists()


def test_old_markers_of_ended_rig_py_processes_are_removed(root, monkeypatch):
	now = time.time()
	running = {(501, T0 + 1)}
	monkeypatch.setattr(rig, "proc_start", lambda pid: next((s for p, s in running if p == pid), None))
	for pid, start, age in ((500, T0, 3600), (501, T0 + 1, 3600), (502, T0 + 2, 60), (503, T0 + 3, 3600)):
		mark(root, pid, start)
		path = root / rig.MARKS / ("%d-%d" % (pid, start))
		os.utime(path, (now - age, now - age))
	running.add((503, T0 + 99))  # pid 503 runs again, as another process
	rig.prune_marks(now)
	# 500 ended an hour ago; 501 still runs; 502 ended a minute ago (a watcher may still look it up)
	assert rig.rig_marks() == {(501, T0 + 1), (502, T0 + 2)}


# Holding back a python.exe it can't tell yet

def looks(monkeypatch, frames, start_age=0.2):
	"""A Watch over frames (one {pid: Proc} per look, 2 s apart), whose processes started start_age s before
	the first look; the users each look found."""
	clock = {"t": 0.0}
	frames = iter(frames)
	def scan(own=()):
		procs = dict(next(frames))
		procs[os.getpid()] = fake(os.getpid(), (), unsure=False)  # rig.py itself
		return procs
	monkeypatch.setattr(rig, "scan", scan)
	monkeypatch.setattr(rig.time, "perf_counter", lambda: clock["t"])
	monkeypatch.setattr(rig, "filetime_now", lambda: T0 + int((start_age + clock["t"]) * S))
	w, found = rig.Watch(), []
	def look():
		found.append(sorted(p.pid for p in w.sample()[0]))
		clock["t"] += 2.0
	return w, look, found


def test_a_rig_py_over_ssh_that_marks_itself_is_never_counted(root, monkeypatch):
	# seen before it marked itself, then marked; then another seen before, and ended after marking itself
	late = T0 + 4 * S  # 501 starts just before the third look
	w, look, found = looks(monkeypatch, [{500: fake(500)}, {500: fake(500, (), unsure=False)}, {501: fake(501, start=late)}, {}])
	look()
	assert w.pending and found == [[]]
	look()
	assert not w.pending
	look()
	mark(root, 501, late)
	look()
	assert found == [[], [], [], []] and not w.pending


def test_a_python_it_cant_tell_counts_once_old_enough_or_when_it_ends_unmarked(root, monkeypatch):
	stays = fake(500)
	w, look, found = looks(monkeypatch, [{500: stays}] * 4 + [{501: fake(501, start=T0 + 8 * S)}, {}])
	for _ in range(6):
		look()
	# 500: 0.2 s old at the first look, 2.2, 4.2 (held), 6.2 (counted); 501: held, then ended with no marker
	assert found == [[], [], [], [500], [], [501]]
	assert "500 gpu python.exe (command line unreadable, no rig.py marker)" == rig.describe(stays)


def test_an_old_python_it_cant_tell_counts_at_once(root, monkeypatch):
	w, look, found = looks(monkeypatch, [{500: fake(500)}], start_age=60)
	look()
	assert found == [[500]] and not w.pending


def test_the_hold_lasts_at_most_mark_wait_if_the_clock_jumped_back(root, monkeypatch):
	# a start time in the future (the clock set back since): held from its first sighting for MARK_WAIT s only
	w, look, found = looks(monkeypatch, [{500: fake(500)}] * 4, start_age=-3600)
	for _ in range(4):
		look()
	assert found == [[], [], [], [500]]


def test_a_readable_python_and_any_other_user_count_at_once(root, monkeypatch):
	w, look, found = looks(monkeypatch, [{500: fake(500, unsure=False), 501: fake(501, ("encoder",), "ffmpeg.exe", unsure=False)}])
	look()
	assert found == [[500, 501]] and not w.pending


def test_run_waits_for_a_python_it_cant_tell_then_starts_once_it_is_a_rig_py(root, monkeypatch):
	# --no-wait too: it is no user yet, so the run neither starts beside it nor gives up at once
	monkeypatch.setattr(rig, "MARK_WAIT", 0.5)
	start = rig.filetime_now()
	seen = []
	def scan(own=()):
		seen.append(1)
		ssh = fake(500, (), start=start, unsure=False) if len(seen) > 4 else fake(500, start=start)
		return {os.getpid(): fake(os.getpid(), (), unsure=False), 500: ssh}
	monkeypatch.setattr(rig, "scan", scan)
	class Args:
		cpu, io, no_wait = 2.0, 50.0, True
	rig.wait_quiet(rig.Watch(), Args)
	assert len(seen) == 5


def test_no_wait_gives_up_on_a_python_that_never_shows_it_is_a_rig_py(root, monkeypatch, capsys):
	monkeypatch.setattr(rig, "MARK_WAIT", 0.3)
	start = rig.filetime_now()
	monkeypatch.setattr(rig, "scan", lambda own=(): {os.getpid(): fake(os.getpid(), (), unsure=False), 500: fake(500, start=start)})
	class Args:
		cpu, io, no_wait = 2.0, 50.0, True
	t0 = time.monotonic()
	with pytest.raises(SystemExit) as e:
		rig.wait_quiet(rig.Watch(), Args)
	assert e.value.code == 75 and time.monotonic() - t0 >= 0.25
	assert "500 gpu python.exe (command line unreadable, no rig.py marker)" in capsys.readouterr().out


class Lock:
	def ours(self):
		return True

	def text(self):
		return ""


def run_ending_beside(monkeypatch, marks_itself):
	"""The last line of a run whose last look sees pid 500 start, a python.exe it can't tell: one that marks
	itself 3 looks later (a rig.py over ssh), or never does; and its log."""
	monkeypatch.setattr(rig, "MARK_WAIT", 0.5)
	monkeypatch.setattr(rig, "NVSMI", None)
	state = {"looks": 0, "start": None}
	def scan(own=()):
		procs = {os.getpid(): fake(os.getpid(), (), unsure=False)}
		if state["start"] is not None:
			state["looks"] += 1
			marked = marks_itself and state["looks"] > 3
			procs[500] = fake(500, () if marked else ("gpu",), start=state["start"], unsure=not marked)
		return procs
	monkeypatch.setattr(rig, "scan", scan)
	n = [0]
	def alive():
		n[0] += 1
		if n[0] == 3:
			state["start"] = rig.filetime_now()
		return n[0] <= 3
	args = types.SimpleNamespace(cpu=2.0, io=50.0, load_looks=2, load_share=0.2, strict=False, no_wait=False, allow=set())
	lines = []
	counts = rig.watch(rig.Watch(job="t-1"), alive, lines.append, args, [None], Lock())
	out = []
	rig.finish(counts, out.append)
	return out[-1], lines


def test_a_python_it_cant_tell_at_a_runs_last_look_is_judged_before_the_verdict(root, monkeypatch):
	verdict, lines = run_ending_beside(monkeypatch, marks_itself=False)
	assert verdict.startswith("RIG_CONTAMINATED (") and "gpu 1" in verdict, verdict
	assert "FOREIGN 500 gpu python.exe (command line unreadable, no rig.py marker)" in lines, lines


def test_a_rig_py_starting_at_a_runs_last_look_leaves_it_clean(root, monkeypatch):
	verdict, lines = run_ending_beside(monkeypatch, marks_itself=True)
	assert verdict == "RIG_CLEAN", (verdict, lines)
	assert not any(line.startswith("FOREIGN") for line in lines), lines


# The Windows API itself

@windows
def test_a_marker_names_this_process_as_the_process_table_does(root, monkeypatch):
	monkeypatch.setattr(rig, "WINDOWS", True)
	rig.mark_self("test")
	start = {row[0]: row[3] for row in rig_windows.process_table()}[os.getpid()]
	assert (os.getpid(), start) in rig.rig_marks()


@windows
def test_a_rig_py_whose_command_line_cant_be_read_is_never_counted_and_another_python_is(tmp_path, monkeypatch):
	# Over ssh a rig.py at the desktop can't read these two python.exe's command lines: hidden here alike
	root = tmp_path / "root"
	root.mkdir()
	monkeypatch.setattr(rig, "ROOT", str(root))
	hidden, read = set(), rig_windows.command_line
	monkeypatch.setattr(rig_windows, "command_line", lambda pid, start: None if pid in hidden else read(pid, start))
	w = rig.Watch()
	w.me = set()  # the two are this process's children, which a Watch takes as its own (a job's): here they aren't
	w.sample()
	env = dict(os.environ, RIG_ROOT=str(root))
	status = subprocess.Popen([sys.executable, RIG, "status"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
	other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
	hidden |= {status.pid, other.pid}
	counted = set()
	t_end = None
	while t_end is None or time.monotonic() < t_end:
		counted |= {p.pid for p in w.sample()[0]}
		if t_end is None and status.poll() is not None and other.poll() is not None:
			t_end = time.monotonic() + rig.PERIOD + 1  # one more look after both ended
		time.sleep(0.3)
	assert status.returncode == 0
	assert any(pid == status.pid for pid, _ in rig.rig_marks())
	assert status.pid not in counted
	assert other.pid in counted  # ended unmarked within MARK_WAIT, or counted once older
