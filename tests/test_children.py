"""
campy/children.py: a child started with children.Child dies with its parent,
even when the parent is killed (as campy-live's nvidia-smi now does). Each
test starts a parent Python that starts a long sleep as its child, kills the
parent hard (TerminateProcess on Windows, SIGKILL on Linux), and looks for
the child.
"""

import os, signal, subprocess, sys, tempfile, time

import pytest

from campy import children

TIED = sys.platform == "win32" or sys.platform.startswith("linux")
SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]

# The parent: starts the sleep, says its pid and how it is tied, and waits to be killed
PARENT = """
import subprocess, sys, time
from campy import children
plain = sys.argv[1] == "plain"
sleep = [sys.executable, "-c", "import time; time.sleep(60)"]
child = subprocess.Popen(sleep) if plain else children.Child(sleep)
print(child.pid, None if plain else child.tied, flush=True)
time.sleep(60)
"""


def Alive(pid):
	if sys.platform == "win32":
		import ctypes
		k32 = ctypes.WinDLL("kernel32", use_last_error=True)
		k32.OpenProcess.restype = ctypes.c_void_p
		handle = k32.OpenProcess(0x00100000, False, pid) # SYNCHRONIZE
		if not handle:
			return False
		try:
			return k32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102 # WAIT_TIMEOUT: still running
		finally:
			k32.CloseHandle(ctypes.c_void_p(handle))
	try:
		with open("/proc/{}/stat".format(pid)) as f:
			return f.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X") # not a zombie
	except FileNotFoundError:
		return False


def Gone(pid, within=10):
	deadline = time.monotonic() + within
	while time.monotonic() < deadline:
		if not Alive(pid):
			return True
		time.sleep(0.05)
	return False


def Kill(pid):
	try:
		os.kill(pid, signal.SIGTERM if sys.platform == "win32" else signal.SIGKILL)
	except OSError:
		pass


def ChildOfKilledParent(how):
	# The pid of the parent's child, and how it was tied, after the parent is killed
	env = dict(os.environ, PYTHONPATH=os.pathsep.join(
		[os.path.dirname(os.path.dirname(os.path.abspath(children.__file__)))] +
		[p for p in [os.environ.get("PYTHONPATH")] if p]))
	parent = subprocess.Popen([sys.executable, "-c", PARENT, how], stdout=subprocess.PIPE, text=True, env=env)
	try:
		pid, tied = parent.stdout.readline().strip().split(None, 1)
	finally:
		parent.kill() # TerminateProcess / SIGKILL: no clean-up in the parent
		parent.wait()
		parent.stdout.close()
	return int(pid), tied


@pytest.mark.skipif(not TIED, reason="children are tied to their parent on Windows and Linux only")
def test_child_dies_with_its_killed_parent():
	pid, tied = ChildOfKilledParent("tied")
	try:
		assert tied == ("a job object" if sys.platform == "win32" else "PR_SET_PDEATHSIG")
		assert Gone(pid), "the child outlived its killed parent"
	finally:
		Kill(pid)


@pytest.mark.skipif(not TIED, reason="Alive() reads Windows' and Linux's processes")
def test_a_plain_popen_child_outlives_its_killed_parent():
	# What the test above guards against, and that Alive() sees it
	pid, _ = ChildOfKilledParent("plain")
	try:
		time.sleep(0.5)
		assert Alive(pid)
	finally:
		Kill(pid)
	assert Gone(pid)


def test_close_ends_the_child_and_it_starts_in_the_temporary_folder():
	child = children.Child(SLEEP[:2] + ["import os, sys, time; print(os.getcwd(), flush=True); time.sleep(60)"],
		stdout=subprocess.PIPE, text=True)
	try:
		where = child.stdout.readline().strip()
		assert os.path.samefile(where, tempfile.gettempdir())
		assert child.poll() is None
		child.Close()
		assert child.poll() is not None
		child.Close() # again: nothing to do
	finally:
		child.kill()
		child.wait()
		child.stdout.close()


def test_cwd_given_is_kept(tmp_path):
	child = children.Child([sys.executable, "-c", "import os; print(os.getcwd())"], stdout=subprocess.PIPE,
		text=True, cwd=str(tmp_path))
	out, _ = child.communicate(timeout=30)
	child.Close()
	assert os.path.samefile(out.strip(), tmp_path)
