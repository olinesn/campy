"""bench/rig.py's verdict on load outside the job: a machine's known background (background.json), load
that has to last before it counts, and --strict.

The verdict's rules on made-up looks run on any OS; whole runs with a real background program need Linux
(/proc), as in test_rig.py.
"""

import json, os, subprocess, sys, time, types

import pytest

BENCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench")
RIG = os.path.join(BENCH, "rig.py")
sys.path.insert(0, BENCH)
import rig  # noqa: E402

# rig.py looks at the whole machine (every process on it, its CPU and disk load): these tests run with no
# other test beside them (conftest.py, 'alone')
pytestmark = pytest.mark.alone

linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="rig.py reads Linux's /proc")


def last(log):
	# the log's last line, without its time
	return log.strip().splitlines()[-1].split(" ", 1)[1]


def fake(pid, cmd="python.exe", ppid=1, start=None, job=None):
	p = rig.Proc()
	p.pid, p.ppid, p.sid, p.start, p.cpu, p.io = pid, ppid, None, start if start is not None else pid, 0.0, 0
	p.kinds, p.cmd, p.job, p.user = set(), cmd, job, None
	return p


def args(**kw):
	a = dict(cpu=2.0, io=50.0, load_looks=2, load_share=0.2, strict=False)
	a.update(kw)
	return types.SimpleNamespace(**a)


def background(tmp_path, cfg):
	(tmp_path / rig.BACKGROUND).write_text(json.dumps(cfg) if not isinstance(cfg, str) else cfg)
	return rig.Background.read(str(tmp_path))


AGENT = {"processes": [{"name": "agent.exe", "cpu": 4, "io": 200, "why": "IT security agent"}]}
QUIET = rig.Load(0.3, 5.0, [], None, 0.3, 5.0, [])
OTHER = rig.Load(3.0, 10.0, [], None, 3.0, 10.0, [])  # an unlisted program above 2 cores
SCAN = rig.Load(2.8, 110.0, [], None, 0.2, 2.0, [("agent.exe", 2.6, 108.0, True)])  # the agent, within its ceiling


def verdict(looks, bg=None, aborted=None, **kw):
	"""The last line of a run with these looks."""
	track = rig.LoadTrack(args(**kw), bg)
	for load in looks:
		track.add(load)
	counts = dict.fromkeys(rig.KINDS + ("desktop", "load", "lock"), 0)
	counts["load"], counts["background"], counts["loadnotes"] = track.verdict()
	lines = []
	rig.finish(counts, lines.append, aborted)
	return lines[-1]


# background.json

def test_reading_the_background_list(tmp_path):
	bg = rig.Background.read(str(tmp_path))  # none: nothing excused, nothing wrong
	assert bg.entries == [] and bg.problems == [] and bg.text() == "none"
	bg = background(tmp_path, AGENT)
	assert [e["label"] for e in bg.entries] == ["agent.exe"] and not bg.problems
	assert (bg.share, bg.looks) == (rig.BACKGROUND_SHARE, rig.BACKGROUND_LOOKS)
	assert bg.text().startswith("agent.exe up to 4 cores, 200 MB/s (IT security agent)")
	bg = background(tmp_path, [{"path": r"C:\Program Files\Vendor\*\scanner.exe", "cpu": 3, "io": 100}])  # a bare list
	assert bg.entries[0]["path"] == "c:/program files/vendor/*/scanner.exe"
	# A typo excuses nothing, and says so
	bg = background(tmp_path, "{not json")
	assert bg.entries == [] and "can't be read" in bg.problems[0]
	bg = background(tmp_path, {"processes": [{"name": "agent.exe", "cpu": 4}, {"cpu": 1, "io": 1}, AGENT["processes"][0]],
		"share": 0.5, "looks": 5})
	assert [e["label"] for e in bg.entries] == ["agent.exe"] and len(bg.problems) == 2
	assert (bg.share, bg.looks) == (0.5, 5) and bg.allowed(4) == 5 and bg.allowed(40) == 20


def test_background_programs_are_matched_by_name_or_path(tmp_path, monkeypatch):
	bg = background(tmp_path, {"processes": [{"name": "agent*.exe", "cpu": 4, "io": 200},
		{"path": "C:/Program Files/Vendor/*/scanner.exe", "cpu": 4, "io": 200}, {"name": "secd", "cpu": 1, "io": 50}]})
	monkeypatch.setattr(rig, "WINDOWS", True)
	lines = {10: r'"C:\Program Files\Vendor\7.1\scanner.exe" --service', 11: r"C:\Tools\scanner.exe", 12: None}
	monkeypatch.setattr(rig, "cmdline", lambda p: lines.get(p.pid) or p.cmd)
	assert bg.match(fake(9, "AgentSvc.EXE")) == 0  # letter case ignored
	assert bg.match(fake(10, "scanner.exe")) == 1
	assert bg.match(fake(11, "scanner.exe")) is None  # the same name in another folder
	assert bg.match(fake(12, "scanner.exe")) is None  # its command line can't be read: not excused
	assert bg.match(fake(13, "python.exe")) is None
	monkeypatch.setattr(rig, "WINDOWS", False)  # Linux: the program's file name, and /proc/<pid>/exe or argv[0]
	assert bg.match(fake(20, "/opt/sec/bin/secd --daemon")) == 2
	assert bg.match(fake(21, "[secd]")) == 2
	assert bg.match(fake(22, "/usr/bin/secdx")) is None


def test_background_within_its_ceiling_is_taken_out_of_the_load(tmp_path, monkeypatch):
	# pid 600 is the agent, 601 another program; each look adds their CPU seconds, and the disks' bytes
	state = {"agent": [0.0, 0], "other": [0.0, 0], "disk": 0}
	def scan(own=()):
		procs = {os.getpid(): fake(os.getpid(), ppid=0, start=0), 600: fake(600, "agent.exe"), 601: fake(601, "copy.exe")}
		procs[600].cpu, procs[600].io = state["agent"]
		procs[601].cpu, procs[601].io = state["other"]
		return procs
	clock = iter(range(100))
	monkeypatch.setattr(rig, "scan", scan)
	monkeypatch.setattr(rig, "cmdline", lambda p: p.cmd)
	monkeypatch.setattr(rig.time, "perf_counter", lambda: float(next(clock)))  # a look each second
	monkeypatch.setattr(rig, "disk_bytes", lambda: state["disk"])
	w = rig.Watch(background=background(tmp_path, AGENT))
	def look(agent=(0, 0), other=(0, 0), disk=0):  # cores and MB in the last second
		for k, (c, b) in (("agent", agent), ("other", other)):
			state[k][0] += c
			state[k][1] += b * 10 ** 6
		state["disk"] += disk * 10 ** 6
		load = w.sample()[1]
		return round(load.cpu, 1), round(load.io), round(load.rest_cpu, 1), round(load.rest_io), load.background
	w.sample()  # the first look measures nothing
	assert look(agent=(2.5, 100), disk=100) == (2.5, 100, 0, 0, [("agent.exe", 2.5, 100.0, True)])
	# Its reads from Windows' file cache reached no disk; the copy's writes did. Against its ceiling goes at
	# most what the disks moved
	assert look(agent=(1, 150), other=(0.5, 60), disk=60) == (1.5, 60, 0.5, 60, [("agent.exe", 1.0, 60.0, True)])
	# A count that is mostly not disk (the network, pipes): 6,000 MB/s with the disks at 5 is within 200
	assert look(agent=(1, 6000), disk=5) == (1, 5, 0, 0, [("agent.exe", 1.0, 5.0, True)])
	# Above its ceiling it is load like anyone's
	assert look(agent=(5, 10), disk=10) == (5, 10, 5, 10, [("agent.exe", 5.0, 10.0, False)])
	load = rig.Load(5.0, 10.0, [], None, 5.0, 10.0, [("agent.exe", 5.0, 10.0, False)])
	assert "agent.exe 5.0 cores 10 MB/s ABOVE its ceiling; the rest: cpu 5.0 cores" in rig.load_text(load)


# The verdict

def test_a_moment_of_load_is_logged_but_does_not_count():
	line = verdict([QUIET] * 9 + [OTHER])
	assert line.startswith("RIG_CLEAN (brief load outside the job in 1 of 10 looks, longest 1 look ("), line
	assert "peak 3.0 cores, 10 MB/s (not counted: under 2 looks in a row and 20% of the run)" in line
	assert verdict([QUIET] * 10) == "RIG_CLEAN"


def test_load_that_lasts_or_keeps_coming_contaminates():
	line = verdict([QUIET] * 8 + [OTHER, OTHER])  # 2 looks in a row
	assert line.startswith("RIG_CONTAMINATED (2 sightings: gpu 0, encoder 0, camera 0, trigger 0, desktop 0, load 2, lock 0; "
		"load outside the job in 2 of 10 looks, longest 2 looks ("), line
	line = verdict([QUIET, OTHER] * 3 + [QUIET] * 4)  # never 2 in a row, but 3 of 10 looks: more than 20%
	assert line.startswith("RIG_CONTAMINATED (3 sightings:") and "longest 1 look (" in line, line
	assert verdict([QUIET, OTHER] * 2 + [QUIET] * 6).startswith("RIG_CLEAN (brief load")  # 2 of 10: not more than 20%
	assert verdict([OTHER]).startswith("RIG_CONTAMINATED")  # the only look a short run had
	assert verdict([QUIET] * 8 + [OTHER, OTHER], load_looks=3).startswith("RIG_CLEAN (brief load")


def test_known_background_makes_the_run_clean_background(tmp_path):
	bg = background(tmp_path, AGENT)
	line = verdict([SCAN] * 3 + [QUIET] * 17, bg)
	assert line == ("RIG_CLEAN_BACKGROUND (background (background.json) above the limits in 3 of 20 looks, longest 3 "
		"looks (%s s) in a row: agent.exe peak 2.6 cores, 108 MB/s)" % ("%g" % round(3 * rig.PERIOD, 1))), line
	assert line.startswith("RIG_CLEAN")  # scripts that look for RIG_CLEAN trust it
	# Background and a moment of other load: still clean, and both are named
	line = verdict([SCAN] * 3 + [OTHER] + [QUIET] * 16, bg)
	assert line.startswith("RIG_CLEAN_BACKGROUND (brief load outside the job in 1 of 20 looks"), line
	# Through more of the run than background.json allows (a quarter of it, or 3 looks): contaminated
	line = verdict([SCAN] * 6 + [QUIET] * 14, bg)
	assert line.startswith("RIG_CONTAMINATED (background (background.json) above the limits in 6 of 20 looks") \
		and "more than the 5 allowed" in line, line
	assert verdict([SCAN] * 3 + [QUIET], bg).startswith("RIG_CLEAN_BACKGROUND")  # 3 looks are allowed, however short
	# A device user contaminates whatever the load
	track = rig.LoadTrack(args(), bg)
	track.add(SCAN)
	counts = dict.fromkeys(rig.KINDS + ("desktop", "load", "lock"), 0)
	counts["gpu"] = 1
	counts["load"], counts["background"], counts["loadnotes"] = track.verdict()
	lines = []
	rig.finish(counts, lines.append)
	assert lines[-1].startswith("RIG_CONTAMINATED (1 sightings: gpu 1,"), lines


def test_strict_counts_every_look_and_ignores_the_list(tmp_path):
	bg = background(tmp_path, AGENT)
	assert verdict([SCAN] + [QUIET] * 19, bg, strict=True).startswith("RIG_CONTAMINATED (1 sightings:")
	assert verdict([OTHER] + [QUIET] * 19, strict=True).startswith("RIG_CONTAMINATED (1 sightings:")
	assert verdict([QUIET] * 20, bg, strict=True) == "RIG_CLEAN"


def test_an_aborted_run_with_background_says_so(tmp_path):
	bg = background(tmp_path, AGENT)
	line = verdict([SCAN] + [QUIET] * 9, bg, aborted="SIGTERM")
	assert line.startswith("RIG_ABORTED by SIGTERM (until then: clean but for background; background (background.json)"), line


def test_waiting_ignores_background_but_not_strict(tmp_path):
	assert not rig.foreign_load(SCAN, args()) and rig.overloaded(SCAN, args())
	assert rig.foreign_load(SCAN, args(strict=True))
	assert rig.foreign_load(OTHER, args())
	assert not rig.foreign_load(None, args())


# Whole runs (Linux)

@pytest.fixture
def run(tmp_path):
	env = dict(os.environ, RIG_ROOT=str(tmp_path / "root"), RIG_PERIOD="0.3", RIG_CPU="1000", RIG_IO="100000")
	for name in ("RIG_JOB", "RIG_TAG", "RIG_LOGDIR", "RIG_STRICT", "RIG_LOAD_LOOKS", "RIG_LOAD_SHARE"):
		env.pop(name, None)
	os.makedirs(env["RIG_ROOT"])
	spin = tmp_path / "bgspin"  # a program with a name of its own: python, under another name
	os.symlink(sys.executable, str(spin))

	class Run:
		root = tmp_path / "root"

		def __call__(self, *a):
			return subprocess.run([sys.executable, RIG, "run"] + list(a), env=env, cwd=str(tmp_path),
				capture_output=True, text=True, timeout=60)

		def popen(self, *a):
			return subprocess.Popen([sys.executable, RIG, "run"] + list(a), env=env, cwd=str(tmp_path))

		def spin(self, seconds):
			# one core busy for that long
			return subprocess.Popen([str(spin), "-c", "import time; t = time.time() + %s\nwhile time.time() < t: pass" % seconds])

		def background(self, cfg):
			(self.root / rig.BACKGROUND).write_text(json.dumps(cfg))

		def log(self, tag):
			return (tmp_path / (tag + ".rig.log")).read_text()

	return Run()


@linux
def test_a_listed_program_within_its_ceiling(run):
	# Other programs' noise on the test machine must not count here: only the background verdict is tested
	run.background({"processes": [{"name": "bgspin", "cpu": 8, "io": 100000}], "share": 1.0})
	spin = run.spin(6)
	try:
		time.sleep(0.5)
		t = time.monotonic()
		r = run("--cpu", "0.5", "--load-looks", "1000", "--load-share", "1", "tester", "bg", "--", "sleep", "2")
		assert r.returncode == 0, r.stdout + r.stderr
		assert time.monotonic() - t < 5  # it didn't wait for the background program
	finally:
		spin.kill()
	log = run.log("bg")
	assert "known background (background.json): bgspin up to 8 cores" in log, log
	assert "LOAD " in log and "background (background.json): bgspin" in log, log
	assert last(log).startswith("RIG_CLEAN_BACKGROUND (") and "bgspin peak" in last(log), log


@linux
def test_a_listed_program_above_its_ceiling_and_strict(run):
	run.background({"processes": [{"name": "bgspin", "cpu": 0.2, "io": 100000}], "share": 1.0})
	for tag, extra in (("over", []), ("strict", ["--strict"])):
		if tag == "strict":
			run.background({"processes": [{"name": "bgspin", "cpu": 8, "io": 100000}], "share": 1.0})
		r = run.popen("--cpu", "0.5", *extra, "tester", tag, "--", "sleep", "3")
		time.sleep(1.0)  # the run has started; then the program starts
		spin = run.spin(1.5)
		spin.wait()
		assert r.wait(30) == 0
		log = run.log(tag)
		assert last(log).startswith("RIG_CONTAMINATED") and " load " in last(log), log
	assert "--strict: any look with load outside the job" in run.log("strict")
	assert "ABOVE its ceiling" in run.log("over")
