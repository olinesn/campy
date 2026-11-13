"""
campy-live --cpus auto: on Windows it leaves out the CPU that handles its
GPU's interrupts (cpus.GpuInterruptCpus), and says so. No GPU needed.
"""
import time
import types

import pytest

from campy import cpus, live


@pytest.fixture
def placed(monkeypatch):
	# What PlaceSelf pinned and the priority it set, with GpuInterrupts and the cores stand-ins
	done = {}
	monkeypatch.setattr(cpus, "Pin", lambda c, pid=None: done.__setitem__("cpus", list(c)))
	monkeypatch.setattr(cpus, "SetPriority", lambda name, pid=None: done.__setitem__("priority", name))
	monkeypatch.setattr(cpus, "All", lambda: list(range(8)))
	monkeypatch.setattr(live, "Say", lambda text: done.setdefault("said", []).append(text))

	def Run(hybrid=None, interrupts=None, cpus_option="auto"):
		done.clear()
		monkeypatch.setattr(cpus, "Hybrid", lambda: hybrid)
		monkeypatch.setattr(live, "GpuInterrupts", lambda args: interrupts)
		text, found = live.PlaceSelf(types.SimpleNamespace(cpus=cpus_option, priority="auto"))
		return text, found, dict(done)
	return Run


RTX4000 = {"gpu": "NVIDIA RTX 4000 Ada Generation", "cpus": [2], "share": 0.97, "extra": 2.0, "launches": 4000}


def test_all_but_the_interrupt_cpu_at_normal_priority(placed):
	text, found, done = placed(interrupts=RTX4000)
	assert done["cpus"] == [0, 1, 3, 4, 5, 6, 7] and "priority" not in done
	assert text == "CPUs 0-1,3-7 (all but CPU 2, which handles the NVIDIA RTX 4000 Ada Generation's interrupts)"
	assert found is RTX4000


def test_performance_cores_less_the_interrupt_cpu(placed):
	hybrid = {"performance": [0, 1, 2, 3], "efficiency": [4, 5, 6, 7]}
	text, _, done = placed(hybrid=hybrid, interrupts=RTX4000)
	assert done == {"cpus": [0, 1, 3], "priority": "high", "said": done["said"]}
	assert "the performance cores but CPU 2" in text
	# An interrupt CPU among the efficiency cores: the performance cores as they are
	text, _, done = placed(hybrid=hybrid, interrupts=dict(RTX4000, cpus=[5]))
	assert done["cpus"] == [0, 1, 2, 3] and text == "CPUs 0-3 (the performance cores) at high priority"


def test_as_before_when_no_cpu_stands_out_or_off_windows(placed):
	assert placed(interrupts={"cpus": [], "reason": "spread"})[0] is None # nothing pinned, normal priority
	assert placed(interrupts=None)[:2] == (None, None)


def test_a_list_or_none_skips_the_measurement(placed, monkeypatch):
	text, found, done = placed(interrupts=RTX4000, cpus_option="0-3")
	assert done["cpus"] == [0, 1, 2, 3] and done["priority"] == "high" and found is None
	assert placed(interrupts=RTX4000, cpus_option="none")[:2] == (None, None)


def test_measured_only_on_windows_with_a_model(monkeypatch):
	args = types.SimpleNamespace(model="sleap", device="auto")
	monkeypatch.setattr(live.sys, "platform", "linux")
	assert live.GpuInterrupts(args) is None
	monkeypatch.setattr(live.sys, "platform", "win32")
	assert live.GpuInterrupts(types.SimpleNamespace(model="none", device="auto")) is None


# --pose3d-cpus: the 3D step's thread alone on CPUs of its own, the rest of campy-live kept off them (Linux)

def three_d(monkeypatch, option, calibration="cal.toml", platform="linux"):
	monkeypatch.setattr(live.sys, "platform", platform)
	return live.Pose3DCpus(types.SimpleNamespace(pose3d_cpus=option, calibration=calibration))


def test_pose3d_cpus_resolved_only_on_linux_with_a_3d_step(monkeypatch):
	said = []
	monkeypatch.setattr(live, "Say", said.append)
	monkeypatch.setattr(cpus, "All", lambda: list(range(12)))
	assert three_d(monkeypatch, "4-7") == [4, 5, 6, 7]
	assert three_d(monkeypatch, "none") is None and three_d(monkeypatch, "") is None and not said
	assert three_d(monkeypatch, "4-7", calibration="") is None and "no 3D step" in said[-1]
	assert three_d(monkeypatch, "4-7", platform="win32") == [4, 5, 6, 7]
	assert three_d(monkeypatch, "4-7", platform="darwin") is None and "only on Linux and Windows" in said[-1]
	with pytest.raises(SystemExit):
		three_d(monkeypatch, "4-x")


def test_the_rest_of_campy_live_kept_off_the_3d_steps_cpus(placed, monkeypatch):
	monkeypatch.setattr(cpus, "All", lambda: list(range(12)))

	def Run(cpus_option, hybrid=None):
		monkeypatch.setattr(cpus, "Hybrid", lambda: hybrid)
		monkeypatch.setattr(live, "GpuInterrupts", lambda args: None)
		done = {}
		monkeypatch.setattr(cpus, "Pin", lambda c, pid=None: done.__setitem__("cpus", list(c)))
		monkeypatch.setattr(cpus, "SetPriority", lambda name, pid=None: done.__setitem__("priority", name))
		text, _ = live.PlaceSelf(types.SimpleNamespace(cpus=cpus_option, priority="auto"), keep_off=[4, 5, 6, 7])
		return text, done

	# A CPU of one kind (the Jetson AGX Orin), --cpus auto: all but the 3D step's, at normal priority
	text, done = Run("auto")
	assert done == {"cpus": [0, 1, 2, 3, 8, 9, 10, 11]}
	assert text == "CPUs 0-3,8-11 (all but the 3D step's); the 3D step alone on CPUs 4-7"
	# --cpus 0-3: already off them
	text, done = Run("0-3")
	assert done == {"cpus": [0, 1, 2, 3], "priority": "high"}
	assert text == "CPUs 0-3 at high priority; the 3D step alone on CPUs 4-7"
	# A list that takes them in: less them
	assert Run("0-7")[1]["cpus"] == [0, 1, 2, 3]
	# The performance cores
	text, done = Run("auto", hybrid={"performance": list(range(8)), "efficiency": [8, 9, 10, 11]})
	assert done["cpus"] == [0, 1, 2, 3] and "(the performance cores, less the 3D step's)" in text
	# None left for the rest
	with pytest.raises(SystemExit):
		Run("4-5")


def test_on_windows_the_mask_takes_the_3d_cpus_in_and_default_cpu_sets_keep_the_rest_off(placed, monkeypatch):
	# A Windows thread can't leave its process's affinity mask, and a new thread takes the process's
	monkeypatch.setattr(cpus, "All", lambda: list(range(12)))
	monkeypatch.setattr(cpus, "Hybrid", lambda: None)
	monkeypatch.setattr(live, "GpuInterrupts", lambda args: None)
	monkeypatch.setattr(live.sys, "platform", "win32")
	done = {}
	monkeypatch.setattr(cpus, "Pin", lambda c, pid=None: done.__setitem__("mask", list(c)))
	monkeypatch.setattr(cpus, "KeepOff", lambda c: done.__setitem__("default", list(c)))
	monkeypatch.setattr(cpus, "SetPriority", lambda name, pid=None: done.__setitem__("priority", name))
	text, _ = live.PlaceSelf(types.SimpleNamespace(cpus="0-3", priority="auto"), keep_off=[4, 5, 6, 7])
	assert done == {"mask": list(range(8)), "default": [0, 1, 2, 3], "priority": "high"}
	assert text == "CPUs 0-3 at high priority; the 3D step alone on CPUs 4-7"


def test_cpu_set_ids_read_from_windows_entries():
	import struct
	def Entry(cid, group, index):
		# SYSTEM_CPU_SET_INFORMATION: Size 32, Type 0, Id, Group, LogicalProcessorIndex, then the rest
		return struct.pack("<IIIHB", 32, 0, cid, group, index) + bytes(32 - 15)
	data = Entry(256, 0, 0) + Entry(257, 0, 1) + Entry(300, 1, 2) + struct.pack("<II", 16, 1) + bytes(8)
	assert cpus._ParseCpuSets(data) == {0: 256, 1: 257, 66: 300}


def Where():
	# The CPU this thread runs on now
	import ctypes, os
	if live.sys.platform == "win32":
		return ctypes.windll.kernel32.GetCurrentProcessorNumber()
	return sorted(os.sched_getaffinity(0)) # Linux: the CPUs it may run on


@pytest.mark.skipif(not (hasattr(live.os, "sched_setaffinity") or live.sys.platform == "win32"),
	reason="pins threads on Linux and Windows only")
def test_the_3d_thread_runs_on_its_cpus_and_the_caller_stays_where_it_was():
	allowed = cpus.Affinity()
	mine = allowed[-1:]
	seen, before = [], Where()

	class Tracker:
		def Update(self, keypoints, scores, frame):
			end = time.perf_counter() + 0.005 # busy long enough to be moved, were it free to
			while time.perf_counter() < end:
				pass
			seen.append(Where())
			return []

	class Log:
		def Add(self, *a, **k):
			pass

	worker = live.Pose3DThread(Tracker(), Log(), cpus=mine)
	for frame in range(20):
		worker.Put({"keypoints": None, "peak_vals": None, "frame": frame}, None, 0)
	worker.Close()
	if live.sys.platform == "win32":
		assert set(seen) == set(mine) and cpus.Affinity() == allowed
	else:
		assert seen == [mine] * 20 and Where() == before == allowed


def test_all_is_every_cpu_online_not_this_process_s_own(tmp_path, monkeypatch):
	# A CPU of one kind (the Jetson AGX Orin): 'all' was the CPUs campy-live had pinned itself to, when first asked
	# after that (--cpus 0-3 --campy-cpus all)
	(tmp_path / "system" / "cpu").mkdir(parents=True)
	(tmp_path / "system" / "cpu" / "online").write_text("0-11\n")
	monkeypatch.setattr(cpus.os, "sched_getaffinity", lambda pid: {0, 1, 2, 3}, raising=False)
	assert cpus._LinuxCores(str(tmp_path)) == {"performance": list(range(12)), "efficiency": []}
	# Intel's hybrid CPUs on Linux: the two kinds
	(tmp_path / "cpu_core").mkdir(), (tmp_path / "cpu_atom").mkdir()
	(tmp_path / "cpu_core" / "cpus").write_text("0-7\n")
	(tmp_path / "cpu_atom" / "cpus").write_text("8-15\n")
	assert cpus._LinuxCores(str(tmp_path)) == {"performance": list(range(8)), "efficiency": list(range(8, 16))}
