"""
campy keeps the computer awake while it records (keepAwake): a Windows power
request, systemd-inhibit on Linux, caffeinate on macOS. Turned off, it does
nothing; where it cannot be made, campy records anyway and says why. The
command it starts ends with campy, even when campy is killed.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from argparse import ArgumentParser

import pytest

from campy import configurator, keepawake

COMMAND = sys.platform != "win32" # Windows makes a power request, with no command to outlive campy

# A campy that keeps the computer awake, says how and its command's pid, and waits to be killed
CAMPY = """
import json, time
from campy import keepawake
awake = keepawake.KeepAwake().Start()
process = awake._process
print(json.dumps({"how": awake.how, "problem": awake.problem, "pid": process and process.pid,
	"tied": process and process.tied}), flush=True)
time.sleep(60)
"""


def Alive(pid):
	if sys.platform.startswith("linux"):
		try:
			with open("/proc/{}/stat".format(pid)) as f:
				return f.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X") # not a zombie
		except FileNotFoundError:
			return False
	try:
		os.kill(pid, 0)
	except ProcessLookupError:
		return False
	except PermissionError:
		pass
	return True


def Gone(pid, within=10):
	deadline = time.monotonic() + within
	while time.monotonic() < deadline:
		if not Alive(pid):
			return True
		time.sleep(0.05)
	return False


def Cwd(pid):
	if sys.platform.startswith("linux"):
		return os.readlink("/proc/{}/cwd".format(pid))
	out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True).stdout
	return next(line[1:] for line in out.splitlines() if line.startswith("n"))


def test_on_by_default_and_off_from_the_command_line():
	assert configurator.DefaultParams()["keepAwake"] is True
	clargs = configurator.ParseClargs(ArgumentParser(prog="campy-acquire"), ["c.yaml", "--keepAwake", "False"])
	assert clargs.keepAwake is False


def test_off_does_nothing():
	awake = keepawake.KeepAwake(False).Start()
	assert awake.how is None and awake.problem is None
	awake.Stop()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows needs no command")
def test_missing_command_is_said_not_raised(monkeypatch):
	monkeypatch.setattr(shutil, "which", lambda name: None)
	with keepawake.KeepAwake() as awake:
		assert awake.how is None
		assert "not found" in awake.problem


@pytest.mark.skipif(sys.platform != "win32", reason="Windows power requests")
def test_windows_power_request():
	with keepawake.KeepAwake() as awake:
		assert awake.problem is None and awake.how.startswith("a Windows power request")
		# System and execution always; the screen only with someone signed in
		assert {1, 3} <= set(awake._types) and set(awake._types) | {0} == {0, 1, 3}
		assert awake.refused in ([], ["screen"])
	assert awake._handle is None
	# A camera process asks only not to be throttled
	with keepawake.KeepAwake(whole=False) as camera:
		assert camera.problem is None and camera._types == [3]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS caffeinate")
def test_macos_caffeinate():
	with keepawake.KeepAwake() as awake:
		assert awake.how == "caffeinate"
		process = awake._process
		time.sleep(0.2)
		assert process.poll() is None
	assert process.poll() is not None


@pytest.mark.skipif(not sys.platform.startswith("linux") or shutil.which("systemd-inhibit") is None,
	reason="Linux with systemd-inhibit")
def test_linux_inhibitor():
	with keepawake.KeepAwake() as awake:
		if awake.problem: # no logind to talk to (a container, a CI runner)
			pytest.skip(awake.problem)
		time.sleep(0.5)
		# Idle and sleep, or idle only where polkit refuses sleep (over SSH)
		assert awake.how in ("systemd-inhibit, blocking idle and sleep", "systemd-inhibit, blocking idle; sleep refused")
		assert awake.refused == (["sleep"] if awake.how.endswith("sleep refused") else [])
		listed = subprocess.run(["systemd-inhibit", "--list"], capture_output=True, text=True).stdout
		assert keepawake.REASON in listed
	listed = subprocess.run(["systemd-inhibit", "--list"], capture_output=True, text=True).stdout
	assert keepawake.REASON not in listed


@pytest.mark.skipif(sys.platform == "win32", reason="the camera processes' request is Windows only")
def test_camera_process_request_is_windows_only():
	with keepawake.KeepAwake(whole=False) as camera:
		assert camera.how is None and camera.problem is None


@pytest.mark.skipif(not COMMAND, reason="Windows starts no command")
def test_the_command_ends_with_a_killed_campy(tmp_path):
	# The campy starts in a folder of its own, as campy does in a recording's
	root = os.path.dirname(os.path.dirname(os.path.abspath(keepawake.__file__)))
	env = dict(os.environ, PYTHONPATH=os.pathsep.join([root] + [p for p in [os.environ.get("PYTHONPATH")] if p]))
	campy = subprocess.Popen([sys.executable, "-c", CAMPY], stdout=subprocess.PIPE, text=True, env=env,
		cwd=str(tmp_path))
	try:
		said = json.loads(campy.stdout.readline())
		if said["problem"]: # no caffeinate or logind here (a container, a CI runner)
			pytest.skip(said["problem"])
		pid = said["pid"]
		assert Alive(pid)
		# It doesn't hold the folder campy was started in
		assert os.path.samefile(Cwd(pid), tempfile.gettempdir())
		if sys.platform.startswith("linux"):
			assert said["tied"] == "PR_SET_PDEATHSIG"
	finally:
		campy.kill() # SIGKILL: no clean-up in campy
		campy.wait()
		campy.stdout.close()
	try:
		assert Gone(pid), "{} outlived the campy that started it".format(said["how"])
	finally:
		try:
			os.kill(pid, signal.SIGKILL)
		except OSError:
			pass
