"""
campy keeps the computer awake while it records (keepAwake): a Windows power
request, systemd-inhibit on Linux, caffeinate on macOS. Turned off, it does
nothing; where it cannot be made, campy records anyway and says why.
"""

import shutil
import subprocess
import sys
import time
from argparse import ArgumentParser

import pytest

from campy import configurator, keepawake


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
		assert awake.refused == ([] if "sleep" in awake.how else ["sleep"])
		listed = subprocess.run(["systemd-inhibit", "--list"], capture_output=True, text=True).stdout
		assert keepawake.REASON in listed
	listed = subprocess.run(["systemd-inhibit", "--list"], capture_output=True, text=True).stdout
	assert keepawake.REASON not in listed


@pytest.mark.skipif(sys.platform == "win32", reason="the camera processes' request is Windows only")
def test_camera_process_request_is_windows_only():
	with keepawake.KeepAwake(whole=False) as camera:
		assert camera.how is None and camera.problem is None
