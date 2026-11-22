"""bench/rig.py, one GPU at a time (--gpu, RIG_GPU): runs holding single GPUs side by side, a run for the
whole machine waiting for them in line, what counts against each, and what a reader of gpu.lock that knows
nothing of GPUs (an older rig.py, rig_hold.ps1, a GPU queue, a guarded script) makes of it.

A fake nvidia-smi on the PATH lists two GPUs, and the processes a test puts on them (a file, FAKE_NVSMI_PIDS:
'<GPU> <pid> <type>' lines); the lock is the file lock (RIG_LOCK=file), as on Windows. Pure functions and the
lock in-process run anywhere; whole runs where rig.py sees processes (Linux, macOS, Windows).
"""

import os, re, shutil, signal, subprocess, sys, time

import pytest

BENCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench")
RIG = os.path.join(BENCH, "rig.py")
sys.path.insert(0, BENCH)
import rig  # noqa: E402

# rig.py looks at the whole machine (every process on it, its CPU and disk load): these tests run with no
# other test beside them (conftest.py, 'alone')
pytestmark = pytest.mark.alone

WINDOWS = sys.platform == "win32"
RUNS = WINDOWS or sys.platform.startswith(("linux", "darwin"))
runs = pytest.mark.skipif(not RUNS, reason="rig.py reads processes on Linux, macOS and Windows only")
windows = pytest.mark.skipif(not WINDOWS, reason="PowerShell")
posix = pytest.mark.skipif(os.name != "posix", reason="kill -9 and bash")

UUID0, UUID1 = "GPU-11111111-2222-3333-4444-555555555555", "GPU-66666666-7777-8888-9999-000000000000"
FAKE_NVSMI = r'''
import os, sys
GPUS = [("0", %r, "00000000:01:00.0", "Fake RTX 6000 Ada"), ("1", %r, "00000000:C1:00.0", "Fake RTX 4000 Ada")]
GPUS = GPUS[:int(os.environ.get("FAKE_NVSMI_GPUS", "2"))]
args = sys.argv[1:]
if args[:1] == ["--query-gpu=index,uuid,pci.bus_id,name"]:
	for g in GPUS:
		print(", ".join(g))
	sys.exit(0)
if args == ["-q", "-d", "PIDS"]:
	try:
		with open(os.environ["FAKE_NVSMI_PIDS"]) as f:
			rows = [line.split() for line in f if len(line.split()) == 3]
	except (KeyError, OSError):
		rows = []
	print("==============NVSMI LOG==============\n\nAttached GPUs                             : %%d" %% len(GPUS))
	for g in GPUS:
		print("GPU " + g[2])
		mine = [r for r in rows if r[0] == g[0]]
		print("    Processes" + ("" if mine else "                             : None"))
		for r in mine:
			print("        GPU instance ID                   : N/A\n        Compute instance ID               : N/A")
			print("        Process ID                        : %%s\n            Type                          : %%s" %% (r[1], r[2]))
			print("            Name                          : python\n            Used GPU Memory               : 100 MiB")
	sys.exit(0)
sys.exit(9)  # the clock queries: no such field, as an old driver says
''' % (UUID0, UUID1)

# A job (or a stranger) that runs CUDA on a GPU: it says so in FAKE_NVSMI_PIDS, writes '<name> start' and its
# CUDA_VISIBLE_DEVICES to a file, waits until the file <go> exists (or for <seconds>), then writes '<name> end'
ON_GPU = r'''
import os, sys, time
pids, gpu, name, out, go = sys.argv[1:6]
if gpu != "-":
	with open(pids, "a") as f:
		f.write("%s %d C\n" % (gpu, os.getpid()))
with open(out, "a") as f:
	f.write("%s start %s\n" % (name, os.environ.get("CUDA_VISIBLE_DEVICES", "-")))
t0 = time.monotonic()
while time.monotonic() - t0 < 60:
	if (go.replace(".", "", 1).isdigit() and time.monotonic() - t0 > float(go)) or os.path.exists(go):
		break
	time.sleep(0.05)
with open(out, "a") as f:
	f.write("%s end\n" % name)
'''


def last(log):
	return log.strip().splitlines()[-1].split(" ", 1)[1]


def until(check, timeout=30, what=""):
	t0 = time.monotonic()
	while not check():
		assert time.monotonic() - t0 < timeout, "timed out waiting for " + what
		time.sleep(0.05)


def text(path):
	try:
		return path.read_text()
	except OSError:
		return ""


@pytest.fixture
def gpus(tmp_path):
	"""A machine with two fake GPUs and the file lock; rig.py's whole runs on it."""
	bin_ = tmp_path / "bin"
	bin_.mkdir()
	fake = bin_ / "fake_nvsmi.py"
	fake.write_text(FAKE_NVSMI)
	if WINDOWS:
		(bin_ / "nvidia-smi.bat").write_text('@"%s" "%s" %%*\r\n' % (sys.executable, fake))
	else:
		smi = bin_ / "nvidia-smi"
		smi.write_text("#!%s\n%s" % (sys.executable, FAKE_NVSMI))
		smi.chmod(0o755)
	env = dict(os.environ, RIG_ROOT=str(tmp_path / "root"), RIG_LOCK="file", RIG_PERIOD="0.3", RIG_CPU="1000",
		RIG_IO="100000", PATH=str(bin_) + os.pathsep + os.environ.get("PATH", ""), FAKE_NVSMI_PIDS=str(tmp_path / "on_gpu.txt"))
	for name in ("RIG_JOB", "RIG_TAG", "RIG_LOGDIR", "RIG_EXTRA", "RIG_GPU", "RIG_WHOLE", "RIG_STRICT", "RIG_ALLOW",
			"CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER"):
		env.pop(name, None)
	os.makedirs(env["RIG_ROOT"])

	class Gpus:
		root = tmp_path / "root"
		lock = tmp_path / "root" / "gpu.lock"
		out = tmp_path / "out.txt"
		pids = tmp_path / "on_gpu.txt"
		tmp = tmp_path

		def __call__(self, *args, **kw):
			return subprocess.run([sys.executable, RIG] + list(args), env=dict(env, **kw.pop("env", {})),
				cwd=str(tmp_path), capture_output=True, text=True, timeout=120, **kw)

		def popen(self, *args, **kw):
			return subprocess.Popen([sys.executable, RIG] + list(args), env=dict(env, **kw.pop("env", {})),
				cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

		def job(self, gpu, name, go):
			"""The command of a job that runs on GPU gpu ('-': none) until go exists (or go seconds)."""
			return [sys.executable, "-c", ON_GPU, str(self.pids), str(gpu), name, str(self.out), str(go)]

		def run(self, owner, tag, gpu, job_gpu, go, *more):
			return self.popen("run", *(("--gpu", str(gpu)) if gpu is not None else ()), *more, owner, tag, "--",
				*self.job(job_gpu, tag, go))

		def lines(self):
			return text(self.out).splitlines()

		def started(self, name):
			return any(l.startswith(name + " start") for l in self.lines())

		def log(self, tag):
			return (tmp_path / (tag + ".rig.log")).read_text()

	g = Gpus()
	g.env = env
	return g


def done(p, timeout=60):
	try:
		out = p.communicate(timeout=timeout)[0]
	except subprocess.TimeoutExpired:
		p.kill()
		raise AssertionError("rig.py didn't end: " + (p.communicate()[0] or ""))
	assert p.returncode == 0, out
	return out


# Pure functions

PRECISION_PIDS = """Attached GPUs                                          : 2
GPU 00000000:01:00.0
    Processes
        GPU instance ID                   : N/A
        Compute instance ID               : N/A
        Process ID                        : 2656
            Type                          : C+G
            Name                          : C:\\Windows\\System32\\dwm.exe
            Used GPU Memory               : Not available in WDDM driver model
        GPU instance ID                   : N/A
        Compute instance ID               : N/A
        Process ID                        : 17664
            Type                          : C
            Name                          : C:\\Users\\Lab\\python.exe
            Used GPU Memory               : Not available in WDDM driver model
GPU 00000000:C1:00.0
    Processes
        GPU instance ID                   : N/A
        Compute instance ID               : N/A
        Process ID                        : 2656
            Type                          : C+G
            Name                          : C:\\Windows\\System32\\dwm.exe
            Used GPU Memory               : Not available in WDDM driver model
        GPU instance ID                   : N/A
        Compute instance ID               : N/A
        Process ID                        : 5100
            Type                          : G
            Name                          : C:\\Windows\\explorer.exe
            Used GPU Memory               : Not available in WDDM driver model
"""


def test_which_gpu_each_process_runs_cuda_on():
	# nvidia-smi -q -d PIDS lists the processes GPU by GPU (a two-GPU workstation's output, Windows' display driver)
	gpus = rig.parse_gpu_list("0, %s, 00000000:01:00.0, NVIDIA RTX 6000 Ada Generation\n"
		"1, %s, 00000000:C1:00.0, NVIDIA RTX 4000 Ada Generation\n" % (UUID0, UUID1))
	assert [(g.index, g.uuid, g.name) for g in gpus] == [(0, UUID0, "NVIDIA RTX 6000 Ada Generation"),
		(1, UUID1, "NVIDIA RTX 4000 Ada Generation")]
	assert rig.parse_gpu_placement(PRECISION_PIDS, gpus) == {2656: {0, 1}, 17664: {0}}  # explorer only draws (G)
	assert rig.parse_gpu_placement(PRECISION_PIDS) == {2656: {0, 1}, 17664: {0}}  # by their order, without the list
	# The bus id as another driver writes it, and the GPUs listed in another order: by bus, not by place
	swapped = rig.parse_gpu_list("0, a, 0000:c1:00.0, x\n1, b, 0000:01:00.0, y\n")
	assert rig.parse_gpu_placement(PRECISION_PIDS, swapped) == {2656: {0, 1}, 17664: {1}}
	assert rig.parse_gpu_placement(None) == {} and rig.parse_gpu_list(None) == []


def test_whole_machine_and_single_gpus_in_line():
	assert rig.conflicts("whole", "gpu1") and rig.conflicts("gpu0", "whole") and rig.conflicts("gpu1", "gpu1")
	assert not rig.conflicts("gpu0", "gpu1")
	assert rig.want_text("whole") == "the whole machine" and rig.want_text("gpu1") == "GPU 1"


def test_parse_gpu():
	assert rig.parse_gpu("") is None and rig.parse_gpu(None) is None and rig.parse_gpu(" 1 ") == 1
	with pytest.raises(Exception):
		rig.parse_gpu("any")


# The lock in-process: the GPUs' locks, gpu.lock's per-gpu line, and the line of waiters

@pytest.fixture
def root(tmp_path, monkeypatch):
	monkeypatch.setattr(rig, "ROOT", str(tmp_path))
	monkeypatch.setattr(rig, "PERIOD", 0.05)
	monkeypatch.setenv("RIG_LOCK", "file")
	return tmp_path


def sleeper():
	"""A process that runs (a rig.py elsewhere, as far as a lock line or a ticket says) until killed."""
	return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def old_reader(text):
	"""gpu.lock as everything that knows nothing of GPUs reads it: an older rig.py (FileLock.state), rig_hold.ps1
	(LockState) and the GPU queue (Rig-LockState): held while the first rigpid= or the first pid= runs."""
	m = re.search(r"\brigpid=(\d+) rigstart=(-?\d+)", text)
	if not m:
		return "hand"
	pids = [(int(m.group(1)), int(m.group(2)))]
	j = re.search(r"\bpid=(\d+) pidstart=(-?\d+)", text)
	if j:
		pids.append((int(j.group(1)), int(j.group(2))))
	return "held" if any(rig.alive(*p) for p in pids) else "stale"


@runs
def test_two_gpus_held_at_once_and_gpu_lock_says_so(root):
	a, b = rig.FileLock(0), rig.FileLock(1)
	a.acquire("alice", "render", no_wait=True)
	b.acquire("bob", "train", no_wait=True)
	try:
		a.record("render-1", None)
		b.record("train-2", os.getpid())
		line = (root / "gpu.lock").read_text().strip()
		assert line.startswith("per-gpu 0+1 since ") and " job=render-1 " in line and " job=train-2 " in line, line
		assert a.state() == ("shared", line) and old_reader(line) == "held"
		assert (root / "gpu0.lock").read_text().startswith("alice render since ")
		# The whole machine waits for both; so does another run for GPU 1; GPU 0's is taken
		for lock in (rig.FileLock(), rig.FileLock(1), rig.FileLock(0)):
			with pytest.raises(SystemExit) as e:
				lock.acquire("carol", "bench", no_wait=True)
			assert e.value.code == 75
	finally:
		a.release()
	line = (root / "gpu.lock").read_text().strip()
	assert line.startswith("per-gpu 1 since ") and "render" not in line and old_reader(line) == "held", line
	b.release()
	assert not [n for n in os.listdir(root) if n.endswith((".lock", ".pids"))], os.listdir(root)
	w = rig.FileLock()
	w.acquire("carol", "bench", no_wait=True)
	assert (root / "gpu.lock").read_text().startswith("carol bench since ")
	w.release()


@runs
def test_the_per_gpu_line_holds_for_old_readers_while_any_holder_runs(root):
	# Two holders, each a separate process: the line names one live process of each, so it holds while either runs
	ra, rb, ja = sleeper(), sleeper(), sleeper()
	try:
		for i, (owner, rigp, job) in enumerate((("alice", ra, ja), ("bob", rb, None))):
			line = "%s t%d since 2026-11-22 18:00:0%d rigpid=%d rigstart=%d" % (owner, i, i, rigp.pid, rig.proc_start(rigp.pid))
			if job:
				line += " job=t%d-1 pid=%d pidstart=%d" % (i, job.pid, rig.proc_start(job.pid))
			(root / ("gpu%d.lock" % i)).write_text(line + "\n")
		lock = rig.FileLock()
		with rig.guarded(lock.path):
			lock.sync()
		line = lock.text()
		assert line.startswith("per-gpu 0+1 since 2026-11-22 18:00:00 rigpid=%d " % ra.pid) and " pid=%d " % rb.pid in line, line
		assert rig.lock_holder(line)["owner"] == "per-gpu" and rig.named_job(line)[0] == rb.pid
		ra.kill(), ra.wait()
		assert old_reader(line) == "held"  # bob's rig.py still runs
		with rig.guarded(lock.path):
			lock.sync()  # alice's rig.py ended, but her job runs: it holds GPU 0, and the line names it now
		line = lock.text()
		assert (root / "gpu0.lock").exists() and " rigpid=%d " % ja.pid in line, line
		rb.kill(), rb.wait(), ja.kill(), ja.wait()
		assert old_reader(line) == "stale"
		with rig.guarded(lock.path):
			notes = lock.sync()
		assert len(notes) == 2 and not os.path.exists(lock.path), notes
		assert lock.state() == ("free", "")
	finally:
		for p in (ra, rb, ja):
			p.kill()
			p.wait()


@runs
def test_a_gpu_run_waits_behind_the_whole_machine_in_line_and_one_for_the_other_gpu_does_not(root):
	other = sleeper()
	try:
		q = root / "rig-queue"
		q.mkdir()
		def ticket(n, want):
			(q / ("%020d-%d" % (n, other.pid))).write_text("want=%s; owner=w; tag=t%d; since=2026-11-22 18:00:00; "
				"rigpid=%d; rigstart=%d\n" % (want, n, other.pid, rig.proc_start(other.pid)))
		ticket(1, "gpu0")
		lock = rig.FileLock(1)
		lock.acquire("bob", "quick", no_wait=True)  # one waiting for GPU 0 is no reason to wait for GPU 1
		lock.release()
		ticket(2, "whole")
		with pytest.raises(SystemExit):
			lock.acquire("bob", "quick", no_wait=True)
		w = rig.FileLock()
		with pytest.raises(SystemExit):
			w.acquire("carol", "bench", no_wait=True)  # behind the earlier tickets too
		other.kill()
		other.wait()
		lock.acquire("bob", "quick", no_wait=True)  # the waiter has ended: its tickets are gone
		assert not os.listdir(q)
		lock.release()
	finally:
		other.kill()
		other.wait()


@runs
def test_a_waiting_rig_py_stands_in_line_and_leaves_it_once_it_has_the_lock(root, capsys):
	(root / "gpu.lock").write_text("hand-written 06:00\n")
	lock = rig.FileLock(1)
	t0 = time.monotonic()
	import threading
	def free():
		until(lambda: os.listdir(root / "rig-queue") if (root / "rig-queue").exists() else False, what="a ticket")
		f = rig.fields((root / "rig-queue" / os.listdir(root / "rig-queue")[0]).read_text())
		assert f["want"] == "gpu1" and f["owner"] == "bob" and f["rigpid"] == str(os.getpid())
		os.remove(root / "gpu.lock")
	t = threading.Thread(target=free)
	t.start()
	lock.acquire("bob", "quick", no_wait=False)
	t.join()
	assert time.monotonic() - t0 < 30 and not os.listdir(root / "rig-queue")
	assert "rig busy: 'hand-written 06:00' (gpu.lock written by hand" in capsys.readouterr().out
	lock.release()


def fake(pid, kinds=(), name="python.exe", ppid=1, start=None, job=None, gpus=()):
	p = rig.Proc()
	p.pid, p.ppid, p.sid, p.start, p.cpu, p.io = pid, ppid, None, start if start is not None else pid, 0.0, 0
	p.kinds, p.cmd, p.job, p.user, p.gpus = set(kinds), name, job, None, set(gpus)
	return p


def test_a_gpu_runs_list_names_its_job_not_what_started_its_rig_py(monkeypatch, root):
	# On Windows what started rig.py (here the desktop's explorer.exe, then a PowerShell) counts as the run's own;
	# listed in gpu<N>.pids it would let off, for the other GPU's run, everything started from the desktop
	me = os.getpid()
	procs = {1: fake(1, name="explorer.exe", ppid=0, start=1), 50: fake(50, name="powershell.exe", ppid=1, start=2),
		me: fake(me, ppid=50, start=3), 60: fake(60, {"gpu"}, ppid=me, start=4, job="j-1", gpus={1}),
		70: fake(70, {"gpu"}, ppid=1, start=5, gpus={0})}
	monkeypatch.setattr(rig, "WINDOWS", True)
	monkeypatch.setattr(rig, "scan", lambda own=(), gpus=False: procs)
	monkeypatch.setattr(rig, "disk_bytes", lambda: None)
	monkeypatch.setattr(rig, "cmdline", lambda p: p.cmd)
	w = rig.Watch(job="j-1", root_pid=60, gpu=1, partners=lambda: [])
	users, _ = w.sample()
	assert w.own_keys == {(me, 3), (60, 4)} and [p.pid for p in users] == [70], (w.own_keys, users)
	h = dict(rig.lock_holder("bob train since 2026-11-22 18:00:00 rigpid=%d rigstart=3 job=j-1 pid=60 pidstart=4" % me),
		gpu=1, state="held", pids=w.own_keys)
	sides = rig.Sides(procs, [h])
	assert sides.side(procs[60]) == 1 and sides.side(procs[me]) == 1
	assert sides.side(procs[70]) is None and sides.side(procs[1]) is None and sides.side(procs[50]) is None
	# For GPU 0's run, the GPU 1 job's process is let off; one of it on GPU 0 is not
	w0 = rig.Watch(gpu=0, partners=lambda: [h])
	monkeypatch.setattr(rig, "scan", lambda own=(), gpus=False: {k: v for k, v in procs.items() if k in (60, 70)})
	users, _ = w0.sample()
	assert [p.pid for p in users] == [70] and set(w0.shared) == {1}
	procs[60].gpus = {0, 1}
	users, _ = w0.sample()
	assert sorted(p.pid for p in users) == [60, 70] and w0.foreign(users[0]) == {"gpu"}


# Whole runs

@runs
def test_two_runs_on_two_gpus_run_side_by_side_and_end_shared(gpus):
	# Each job waits for a file the test writes only once both jobs run: with one lock for the machine, the
	# second would wait for the first, which never ends
	a = gpus.run("alice", "render", 0, 0, gpus.tmp / "go")
	until(lambda: gpus.started("render"), what="render's job")
	assert text(gpus.lock).startswith("per-gpu 0 since ")
	b = gpus.run("bob", "train", 1, 1, gpus.tmp / "go")
	until(lambda: gpus.started("train"), what="train's job, beside render's")
	time.sleep(1.5)  # a few looks with both
	both = text(gpus.lock)
	assert both.startswith("per-gpu 0+1 since ") and " job=render-" in both and " job=train-" in both, both
	s = gpus("status")
	assert s.stdout.startswith("rig held: per-gpu 0+1 since "), s.stdout
	assert re.search(r"GPU 0 \(Fake RTX 6000 Ada\): held alone: alice render since ", s.stdout), s.stdout
	assert re.search(r"GPU 1 \(Fake RTX 4000 Ada\): held alone: bob train since ", s.stdout), s.stdout
	assert re.search(r"\[GPU 1's job\] \d+ gpu .*\[on GPU 1\]", s.stdout), s.stdout
	part = re.search(r"the jobs on single GPUs themselves: cpu ([\d.]+) cores, disk (\d+) MB/s", s.stdout)
	assert part and float(part.group(1)) < 50 and int(part.group(2)) < 1000, s.stdout  # their own load, measured
	(gpus.tmp / "go").write_text("")
	done(a), done(b)
	assert "render start %s" % UUID0 in gpus.lines() and "train start %s" % UUID1 in gpus.lines()  # each its own GPU
	for tag, other in (("render", 1), ("train", 0)):
		log = gpus.log(tag)
		assert "SHARED from now: the job of the run holding GPU %d" % other in log, log
		assert last(log).startswith("RIG_SHARED (shared the machine with "), log
		assert "FOREIGN" not in log and "WRONG GPU" not in log, log
	assert not [n for n in os.listdir(gpus.root) if n.endswith((".lock", ".pids"))], os.listdir(gpus.root)
	s = gpus("status")
	assert s.stdout.startswith("rig free"), s.stdout
	assert "GPU 0 (Fake RTX 6000 Ada): free" in s.stdout and "GPU 1 (Fake RTX 4000 Ada): free" in s.stdout, s.stdout


@runs
def test_a_gpu_run_alone_is_clean_and_says_which_gpu(gpus):
	r = gpus("run", "--gpu", "1", "bob", "alone", "--", *gpus.job(1, "alone", 0.5))
	assert r.returncode == 0, r.stdout + r.stderr
	log = gpus.log("alone")
	assert "one GPU at a time: this run holds GPU 1 (Fake RTX 4000 Ada) alone; the job sees only GPU 1" in log, log
	assert re.search(r"rig held by bob for alone \(job alone-\d+, pid \d+\): ", log), log  # the line others parse
	assert last(log).startswith("RIG_CLEAN (held GPU 1 only; no job on another GPU ran beside it"), log


@runs
def test_the_whole_machine_waits_for_the_gpu_runs_and_later_gpu_runs_wait_behind_it(gpus):
	a = gpus.run("alice", "render", 0, 0, gpus.tmp / "release-a")
	until(lambda: gpus.started("render"), what="render's job")
	w = gpus.run("carol", "bench", None, "-", 0.5)
	until(lambda: os.path.isdir(gpus.root / "rig-queue") and os.listdir(gpus.root / "rig-queue"), what="bench's ticket")
	b = gpus.run("bob", "train", 1, 1, 0.3)  # GPU 1 is free, but the whole machine was waited for first
	time.sleep(2)
	assert not gpus.started("bench") and not gpus.started("train"), gpus.lines()
	s = gpus("status")
	queue = s.stdout.split("in line, oldest first:")[1]
	assert re.search(r"1\. carol bench for the whole machine.*\n\s+2\. bob train for GPU 1", queue), s.stdout
	r = gpus("run", "--no-wait", "--gpu", "1", "dave", "quick", "--", sys.executable, "-c", "pass")
	assert r.returncode == 75 and "GPU 1 is free, but in line before this run: carol bench" in r.stdout, r.stdout
	(gpus.tmp / "release-a").write_text("")
	done(a), done(w), done(b)
	order = [l.split()[:2] for l in gpus.lines()]
	assert order == [["render", "start"], ["render", "end"], ["bench", "start"], ["bench", "end"],
		["train", "start"], ["train", "end"]], order
	assert last(gpus.log("bench")).startswith("RIG_CLEAN"), gpus.log("bench")
	assert last(gpus.log("train")).startswith("RIG_CLEAN (held GPU 1 only"), gpus.log("train")


@runs
def test_a_gpu_run_waits_for_the_whole_machine_and_the_whole_machine_for_a_gpu_run(gpus):
	w = gpus.run("carol", "bench", None, "-", gpus.tmp / "go")
	until(lambda: gpus.started("bench"), what="bench's job")
	r = gpus("run", "--no-wait", "--gpu", "1", "bob", "quick", "--", sys.executable, "-c", "pass")
	assert r.returncode == 75 and "rig busy: carol bench since " in r.stdout, r.stdout
	(gpus.tmp / "go").write_text("")
	done(w)
	(gpus.tmp / "go").unlink()
	a = gpus.run("alice", "render", 0, 0, gpus.tmp / "go")
	until(lambda: gpus.started("render"), what="render's job")
	r = gpus("run", "--no-wait", "carol", "bench2", "--", sys.executable, "-c", "pass")
	assert r.returncode == 75 and "rig busy: per-gpu 0 since " in r.stdout, r.stdout
	r = gpus("break")
	assert r.returncode != 0 and "runs hold single GPUs" in (r.stdout + r.stderr) and gpus.lock.exists()
	(gpus.tmp / "go").write_text("")
	done(a)


@runs
def test_what_counts_against_a_run_holding_one_gpu(gpus):
	# alice holds GPU 0. bob's job, holding GPU 1, runs CUDA on GPU 0 too: it counts against alice's run (a
	# FOREIGN gpu, said to be bob's job) and against bob's (WRONG GPU). A program outside both, on GPU 1, counts
	# against alice's too: only another GPU's run's job is let off
	a = gpus.run("alice", "render", 0, 0, gpus.tmp / "go")
	until(lambda: gpus.started("render"), what="render's job")
	b = gpus.run("bob", "train", 1, 0, 1.5)
	until(lambda: any(l.startswith("train end") for l in gpus.lines()), what="train's job")
	done(b)
	stranger = subprocess.Popen(gpus.job(1, "stranger", 1.5))
	stranger.wait(60)
	(gpus.tmp / "go").write_text("")
	done(a)
	log = gpus.log("render")
	assert re.search(r"FOREIGN \d+ gpu .*\[on GPU 0\] \[the job of the run holding GPU 1\]", log), log
	assert re.search(r"FOREIGN %d gpu .*\[on GPU 1\]$" % stranger.pid, log, re.M), log
	assert last(log).startswith("RIG_CONTAMINATED (") and int(re.search(r"\bgpu (\d+)", last(log)).group(1)) >= 2, log
	blog = gpus.log("train")
	assert "WRONG GPU the job runs CUDA on GPU 0, which this run doesn't hold (it holds GPU 1)" in blog, blog
	assert last(blog).startswith("RIG_CONTAMINATED (") and int(re.search(r"wrong-gpu (\d+)", last(blog)).group(1)) >= 1, blog


SAYS = "import os, sys\nwith open(sys.argv[1], 'w') as f:\n\tf.write(open(sys.argv[2]).read() + os.environ['CUDA_VISIBLE_DEVICES'] " \
	"+ ' ' + os.environ.get('CUDA_DEVICE_ORDER', '-'))"


@runs
def test_gpu_without_two_gpus_or_with_whole_holds_the_whole_machine(gpus):
	said = gpus.tmp / "said.txt"
	for env, more, why in (({"FAKE_NVSMI_GPUS": "1"}, (), "nvidia-smi lists only one GPU"),
			({}, ("--whole",), "--whole"), ({}, ("--strict",), "--strict"), ({"RIG_WHOLE": "1"}, (), "--whole")):
		r = gpus("run", "--gpu", "0", *more, "s", "w", "--", sys.executable, "-c", SAYS, str(said), str(gpus.lock), env=env)
		assert r.returncode == 0, r.stdout + r.stderr
		assert re.match(r"s w since .*\n%s -$" % UUID0, said.read_text()), said.read_text()  # a whole-machine gpu.lock
		assert "the job sees only GPU 0 (Fake RTX 6000 Ada) (CUDA_VISIBLE_DEVICES=%s); this run holds the whole " \
			"machine all the same (%s)" % (UUID0, why) in gpus.log("w"), gpus.log("w")
	# No nvidia-smi: the index, in nvidia-smi's order
	path = os.pathsep.join([os.path.dirname(sys.executable)] + (["/usr/bin", "/bin"] if os.name == "posix" else
		[os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")]))
	if not shutil.which("nvidia-smi", path=path):
		r = gpus("run", "--gpu", "1", "s", "n", "--", sys.executable, "-c", SAYS, str(said), str(gpus.lock), env={"PATH": path})
		assert r.returncode == 0 and said.read_text().endswith("\n1 PCI_BUS_ID"), r.stdout + r.stderr + said.read_text()
		assert "this run holds the whole machine all the same (nvidia-smi lists no GPU)" in gpus.log("n")
	# No such GPU, or not a number: refused
	r = gpus("run", "--gpu", "5", "s", "x", "--", sys.executable, "-c", "pass")
	assert r.returncode != 0 and "there is no GPU 5 here; nvidia-smi lists GPU 0 Fake RTX 6000 Ada; GPU 1" in r.stderr, r.stderr
	r = gpus("run", "s", "x", "--", sys.executable, "-c", "pass", env={"RIG_GPU": "one"})
	assert r.returncode == 2 and "not a GPU's index" in r.stderr, r.stderr


@runs
def test_rig_gpu_in_the_environment_holds_one_gpu_and_a_run_inside_its_job_runs_at_once(gpus):
	inner = "import os, subprocess, sys; sys.exit(subprocess.call([sys.executable, %r, 'run', 'bob', 'inner', '--', " \
		"sys.executable, '-c', 'import os; print(\"inner\", os.environ[\"RIG_JOB\"])']))" % RIG
	r = gpus("run", "bob", "outer", "--", sys.executable, "-c", inner, env={"RIG_GPU": "1"})
	assert r.returncode == 0, r.stdout + r.stderr
	assert "already inside the rig's job outer-" in r.stdout and re.search(r"^inner outer-\d+$", r.stdout, re.M), r.stdout
	assert "holds GPU 1 (Fake RTX 4000 Ada) alone" in gpus.log("outer")


@posix
@runs
def test_a_killed_rig_py_of_a_gpu_run_keeps_its_gpu_until_its_job_ends(gpus):
	a = gpus.run("alice", "render", 0, 0, gpus.tmp / "go")
	until(lambda: gpus.started("render") and " pid=" in text(gpus.root / "gpu0.lock"), what="render's job")
	job = rig.named_job(text(gpus.root / "gpu0.lock"))
	os.kill(a.pid, signal.SIGKILL)
	a.wait()
	s = gpus("status")
	assert s.stdout.startswith("rig held: per-gpu 0 since ") and old_reader(text(gpus.lock)) == "held", s.stdout
	r = gpus("run", "--no-wait", "--gpu", "0", "bob", "x", "--", sys.executable, "-c", "pass")
	assert r.returncode == 75, r.stdout
	(gpus.tmp / "go").write_text("")
	until(lambda: not rig.alive(*job), what="the job to end")
	s = gpus("status")
	assert s.stdout.startswith("rig free") and "stale gpu0.lock removed" in s.stdout, s.stdout
	assert not gpus.lock.exists() and not (gpus.root / "gpu0.lock").exists()


@posix
def test_a_guarded_script_with_rig_gpu_takes_one_gpu_once(gpus):
	for f in ("rig.py", "rig_macos.py", "rig_windows.py", "rig_guard.sh"):
		(gpus.root / f).write_text(open(os.path.join(BENCH, f)).read())
	inner = gpus.tmp / "inner.sh"
	inner.write_text('. "$RIG_ROOT/rig_guard.sh" "$@"\necho "inner $RIG_JOB $CUDA_VISIBLE_DEVICES"\n')
	outer = gpus.tmp / "outer.sh"
	outer.write_text('. "$RIG_ROOT/rig_guard.sh" "$@"\necho "outer $RIG_JOB"\nbash %s\n' % inner)
	r = subprocess.run(["bash", str(outer), "t1"], env=dict(gpus.env, RIG_OWNER="tester", RIG_GPU="1"),
		capture_output=True, text=True, timeout=60)
	assert r.returncode == 0, r.stdout + r.stderr
	lines = [l.split() for l in r.stdout.splitlines() if l.startswith(("outer", "inner"))]
	assert len(lines) == 2 and lines[0][1] == lines[1][1] and lines[1][2] == UUID1, r.stdout
	assert "holds GPU 1 (Fake RTX 4000 Ada) alone" in (gpus.tmp / "t1.rig.log").read_text()


@windows
def test_rig_hold_ps1_stands_in_line_while_a_gpu_is_held(gpus):
	for f in ("rig.py", "rig_windows.py", "rig_hold.ps1"):
		(gpus.root / f).write_text(open(os.path.join(BENCH, f)).read())
	a = gpus.run("alice", "render", 0, 0, gpus.tmp / "go")
	until(lambda: gpus.started("render"), what="render's job")
	hold = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(gpus.root / "rig_hold.ps1"),
		"-Owner", "tester", "-Tag", "training", "-Root", str(gpus.root), "-Seconds", "3"], env=gpus.env,
		stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
	queue = gpus.root / "rig-queue"
	until(lambda: queue.exists() and os.listdir(queue), timeout=60, what="rig_hold.ps1's ticket")
	f = rig.fields((queue / os.listdir(queue)[0]).read_text(encoding="utf-8-sig"))
	assert f["want"] == "whole" and f["owner"] == "tester" and f["rigpid"] == str(hold.pid), f
	r = gpus("run", "--no-wait", "--gpu", "1", "bob", "quick", "--", sys.executable, "-c", "pass")
	assert r.returncode == 75 and "in line before this run: tester training for the whole machine" in r.stdout, r.stdout
	assert gpus("status").stdout.startswith("rig held: per-gpu 0 since ")
	(gpus.tmp / "go").write_text("")
	done(a)
	assert hold.wait(60) == 0, hold.stdout.read()
	assert "HELD" in (gpus.root / "rig_hold.log").read_text() and not os.listdir(queue)
