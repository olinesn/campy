import os, shutil, subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SKETCH = os.path.join(os.path.dirname(HERE), "campy", "trigger", "trigger.ino")
CXX = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")


@pytest.fixture(scope="session")
def sim(tmp_path_factory):
	# campy/trigger/trigger.ino, compiled for this computer against a simulated
	# Arduino (tests/arduino_sim)
	if CXX is None:
		pytest.skip("needs a C++ compiler")
	exe = str(tmp_path_factory.mktemp("sketch") / "sim")
	sim_dir = os.path.join(HERE, "arduino_sim")
	subprocess.run([CXX, "-std=c++11", "-O2", "-I", sim_dir, "-include", "Arduino.h", "-x", "c++", SKETCH,
		"-x", "none", os.path.join(sim_dir, "sim.cpp"), "-o", exe], check=True)
	return exe
