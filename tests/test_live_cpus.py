"""
campy-live --cpus auto: on Windows it leaves out the CPU that handles its
GPU's interrupts (cpus.GpuInterruptCpus), and says so. No GPU needed.
"""
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
