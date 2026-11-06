import os, shutil, subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SKETCH = os.path.join(os.path.dirname(HERE), "campy", "trigger", "trigger.ino")
TIMER_SKETCH = os.path.join(os.path.dirname(HERE), "chronicrecord", "chronic_timer", "chronic_timer.ino")
CXX = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")


def Compile(sketch, exe, *flags):
	# A sketch, compiled for this computer against a simulated Arduino (tests/arduino_sim)
	if CXX is None:
		pytest.skip("needs a C++ compiler")
	sim_dir = os.path.join(HERE, "arduino_sim")
	subprocess.run([CXX, "-std=c++11", "-O2", *flags, "-I", sim_dir, "-include", "Arduino.h", "-x", "c++", sketch,
		"-x", "none", os.path.join(sim_dir, "sim.cpp"), "-o", exe], check=True)
	return exe


def pytest_configure(config):
	config.addinivalue_line("markers", "alone: uses the whole machine (bench/rig.py looks at every process on it, and "
		"its CPU and disk load), so it runs with no other test beside it: not on pytest-xdist's workers")


def pytest_collection_modifyitems(config, items):
	# Run in parallel (pytest -n, pytest-xdist), the other workers are Python processes, some with ffmpegs, that the
	# 'alone' tests' rig.py would see as foreign GPU and encoder users. There they are skipped, and run on their own
	# after: pytest -m alone tests (.github/workflows/tests.yml, docs/development.md)
	if not os.environ.get("PYTEST_XDIST_WORKER"):
		return
	skip = pytest.mark.skip(reason="runs alone, not beside other tests: pytest -m alone tests")
	for item in items:
		if item.get_closest_marker("alone"):
			item.add_marker(skip)
	# The files of long real-time tests first (each worker sorts the same way), so that none of them starts last and
	# runs on alone after the others are done: test_session.py's frozen campy-live alone takes 2 minutes
	first = {name: k for k, name in enumerate(LONG_FILES)}
	items.sort(key=lambda item: first.get(os.path.basename(str(item.fspath)), len(first)))


# The files whose tests take longest, longest first (by --durations in CI): run first in parallel
LONG_FILES = ["test_session.py", "test_devices.py", "test_watch_acquire.py", "test_chronic_control.py",
	"test_ffmpeg_restart.py", "test_failures.py", "test_newfiles.py", "test_trigger_start.py"]


@pytest.fixture(scope="session")
def sim(tmp_path_factory):
	# campy/trigger/trigger.ino
	return Compile(SKETCH, str(tmp_path_factory.mktemp("sketch") / "sim"))


@pytest.fixture(scope="session")
def old_sim(tmp_path_factory):
	# tests/old_trigger/trigger.ino: the trigger sketch an older campy flashed (6 s settling delay, no test LED)
	old = os.path.join(HERE, "old_trigger")
	return Compile(os.path.join(old, "trigger.ino"), str(tmp_path_factory.mktemp("old_sketch") / "sim"), "-include",
		os.path.join(old, "prototypes.h"))


@pytest.fixture(scope="session", params=["pico", "nano"])
def timer_sim(request, tmp_path_factory):
	# chronicrecord/chronic_timer/chronic_timer.ino: the ChronicRecord rig's timer,
	# as the Pico runs it (a PIO state machine makes the outputs) and as the Nano ESP32 does
	flags = ["-DSIM_PIO"] if request.param == "pico" else []
	return Compile(TIMER_SKETCH, str(tmp_path_factory.mktemp("timer") / "sim"), *flags)


@pytest.fixture(autouse=True)
def no_real_alerts(monkeypatch, tmp_path_factory):
	# No test sends to the alerts file of the computer it runs on (Slack, healthchecks.io), or writes its
	# ~/.campy/status/recording.json: campy-acquires started by tests inherit these too. A test that wants
	# alerts names its own file.
	folder = tmp_path_factory.mktemp("campy-home")
	monkeypatch.setenv("CAMPY_ALERTS_FILE", str(folder / "no-alerts.yaml"))
	monkeypatch.setenv("CAMPY_STATUS_DIR", str(folder / "status"))


@pytest.fixture(autouse=True)
def no_camera_opening():
	# campy/startup.py holds what a camera says as it opens, per process: each test starts with none
	from campy import startup
	startup.End()
	yield
	startup.End()


OUTPUTS_SKETCH = os.path.join(os.path.dirname(HERE), "campy", "outputs_board", "campy_outputs", "campy_outputs.ino")


@pytest.fixture(scope="session")
def outputs_sim(tmp_path_factory):
	# campy/outputs_board/campy_outputs: the board campy-rules switches
	return Compile(OUTPUTS_SKETCH, str(tmp_path_factory.mktemp("outputs") / "sim"))
