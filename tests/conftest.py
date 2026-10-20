import os, shutil, subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SKETCH = os.path.join(os.path.dirname(HERE), "campy", "trigger", "trigger.ino")
TIMER_SKETCH = os.path.join(os.path.dirname(HERE), "chronicrecord", "chronic_timer", "chronic_timer.ino")
CXX = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")


def Compile(sketch, exe):
	# A sketch, compiled for this computer against a simulated Arduino (tests/arduino_sim)
	if CXX is None:
		pytest.skip("needs a C++ compiler")
	sim_dir = os.path.join(HERE, "arduino_sim")
	subprocess.run([CXX, "-std=c++11", "-O2", "-I", sim_dir, "-include", "Arduino.h", "-x", "c++", sketch,
		"-x", "none", os.path.join(sim_dir, "sim.cpp"), "-o", exe], check=True)
	return exe


@pytest.fixture(scope="session")
def sim(tmp_path_factory):
	# campy/trigger/trigger.ino
	return Compile(SKETCH, str(tmp_path_factory.mktemp("sketch") / "sim"))


@pytest.fixture(scope="session")
def timer_sim(tmp_path_factory):
	# chronicrecord/chronic_timer/chronic_timer.ino: the ChronicRecord rig's timer
	return Compile(TIMER_SKETCH, str(tmp_path_factory.mktemp("timer") / "sim"))
