"""bench/rig_macos.py, rig.py's view of the processes on macOS: its parsers run on any OS; what it reads from
macOS itself (ps, libproc, sysctl) only on a Mac.
"""

import os, struct, subprocess, sys, time

import pytest

BENCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench")
sys.path.insert(0, BENCH)
import rig_macos  # noqa: E402

mac = pytest.mark.skipif(sys.platform != "darwin", reason="reads macOS itself")


def test_cpu_time_in_every_form_ps_gives():
	assert rig_macos.parse_cputime("0:00.96") == pytest.approx(0.96)
	assert rig_macos.parse_cputime("198:44.50") == pytest.approx(198 * 60 + 44.5)
	assert rig_macos.parse_cputime("02:03:04") == 2 * 3600 + 3 * 60 + 4
	assert rig_macos.parse_cputime("1-02:03:04.5") == pytest.approx(86400 + 2 * 3600 + 3 * 60 + 4.5)
	assert rig_macos.parse_cputime("-") is None


def test_ps_rows_keep_spaces_in_program_paths():
	text = """    1     0     0  27:26.44 Ss   Wed Nov 18 00:35:21 2026     /sbin/launchd
  995   774   503   0:24.81 S    Wed Nov  5 09:05:07 2026     /Applications/Brave Browser.app/Contents/MacOS/Brave Browser Helper (Renderer)
  996     1   503   0:00.00 Z    Wed Nov 18 00:35:57 2026     (sleep)
garbage
"""
	rows = rig_macos.parse_ps(text)
	assert [r[0] for r in rows] == [1, 995, 996]
	pid, ppid, uid, cpu, start, stat, exe = rows[1]
	assert (ppid, uid, stat) == (774, 503, "S") and cpu == pytest.approx(24.81)
	assert exe == "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser Helper (Renderer)"
	assert start == int(time.mktime((2026, 11, 5, 9, 5, 7, 0, 0, -1)))
	assert rows[2][5] == "Z"


def test_procargs_buffer():
	# argc, the program's path, NUL padding, the arguments, the environment, then whatever follows
	data = struct.pack("i", 3) + b"/usr/bin/python3\0\0\0\0python3\0rig.py\0run\0HOME=/Users/x\0RIG_JOB=t1-42\0\0\0junk\0"
	argv, env = rig_macos.parse_procargs(data)
	assert argv == ["python3", "rig.py", "run"]
	assert env == [b"HOME=/Users/x", b"RIG_JOB=t1-42"]
	assert rig_macos.job_of(env) == "t1-42"
	assert rig_macos.job_of([b"RIG_JOBS=x"]) is None
	assert rig_macos.parse_procargs(b"") == ([], [])


def test_names():
	assert rig_macos.program_name("/Applications/SpikeGLX.app/Contents/MacOS/SpikeGLX") == "SpikeGLX"
	assert rig_macos.program_name("C:\\SpikeGLX\\SpikeGLX.exe") == "SpikeGLX.exe"  # under Wine
	assert rig_macos.matches("SpikeGLX.exe", ["spikeglx*"]) and not rig_macos.matches("wine64-preloader", ["spikeglx*"])
	assert rig_macos.SERIAL.match("/dev/cu.usbmodem14101") and rig_macos.SERIAL.match("/dev/tty.usbserial-A10K")
	assert not rig_macos.SERIAL.match("/dev/cu.Bluetooth-Incoming-Port") and not rig_macos.SERIAL.match("/dev/ttys001")


@mac
def test_what_macos_shows_of_a_process_of_this_user(tmp_path):
	f = tmp_path / "held-open"
	f.write_text("x" * 100000)
	p = subprocess.Popen([sys.executable, "-c", "import sys, time; f = open(sys.argv[1]); f.read(); time.sleep(30)", str(f)],
		env=dict(os.environ, RIG_JOB="mac-test-1"))
	try:
		t = time.monotonic() + 10
		while os.path.realpath(str(f)) not in rig_macos.open_paths(p.pid):
			assert time.monotonic() < t, rig_macos.open_paths(p.pid)
			time.sleep(0.05)
		argv, env = rig_macos.procargs(p.pid)
		assert argv[-1] == str(f) and rig_macos.job_of(env) == "mac-test-1"
		assert rig_macos.disk_io(p.pid) is not None
		row = [r for r in rig_macos.process_table() if r[0] == p.pid][0]
		assert row[1] == os.getpid() and row[2] == os.getuid()
		assert rig_macos.process_start(p.pid) == row[4] > 0
	finally:
		p.kill()
		p.wait()
	assert rig_macos.process_start(p.pid) is None


@mac
def test_another_users_process_shows_its_cpu_but_not_its_files():
	rows = {r[0]: r for r in rig_macos.process_table()}
	assert rows[1][2] == 0 and rows[1][3] is not None  # launchd, root's: ps shows its CPU time
	assert rig_macos.procargs(1) is None and rig_macos.open_paths(1) == [] and rig_macos.disk_io(1) is None
