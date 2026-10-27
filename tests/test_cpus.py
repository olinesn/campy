"""
campy/cpus.py: which cores are which, and pinning (campy-live's --cpus, and
campy-live-view's c key).
"""

import os, struct, sys

import pytest

from campy import cpus


def test_cpu_lists_read_and_write():
	assert cpus.ParseList("0,1,10-13,22,23") == [0, 1, 10, 11, 12, 13, 22, 23]
	assert cpus.ParseList(" 3, 1-2 ,1") == [1, 2, 3]
	assert cpus.Text([0, 1, 10, 11, 12, 13, 22, 23]) == "0-1,10-13,22-23"
	assert cpus.Text([5]) == "5"
	assert cpus.Mask([0, 1, 10, 11, 12, 13, 22, 23]) == "C03C03"
	for bad in ("", "a", "3-1", "-1", "1-x"):
		with pytest.raises(ValueError):
			cpus.ParseList(bad)


def Entry(efficiency, mask, group=0):
	# A SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX of RelationProcessorCore, one group
	body = struct.pack("<BB20xH", 0, efficiency, 1) + struct.pack("<QH6x", mask, group)
	return struct.pack("<II", 0, 8 + len(body)) + body


def test_windows_cores_by_their_efficiency_class():
	# A Core Ultra 9 285's numbering: the P-cores (class 1) are 0, 1, 10-13, 22, 23
	p = [0, 1, 10, 11, 12, 13, 22, 23]
	data = b"".join(Entry(1 if c in p else 0, 1 << c) for c in range(24))
	assert cpus._ByClass(cpus._ParseCores(data)) == {"performance": p,
		"efficiency": [c for c in range(24) if c not in p]}
	# One kind: all of them performance
	data = b"".join(Entry(0, 3 << (2 * c)) for c in range(4)) # 4 cores of 2 threads
	assert cpus._ByClass(cpus._ParseCores(data)) == {"performance": list(range(8)), "efficiency": []}


def test_resolve(monkeypatch):
	monkeypatch.setattr(cpus, "Cores", lambda: {"performance": [0, 1], "efficiency": [2, 3]})
	assert cpus.Resolve("performance") == cpus.Resolve("P") == [0, 1]
	assert cpus.Resolve("e-cores") == [2, 3]
	assert cpus.Resolve("all") == [0, 1, 2, 3]
	assert cpus.Resolve("0,3") == [0, 3]
	monkeypatch.setattr(cpus, "Cores", lambda: {"performance": [0, 1], "efficiency": []})
	with pytest.raises(ValueError):
		cpus.Resolve("efficiency")


@pytest.mark.skipif(not hasattr(os, "sched_setaffinity") and sys.platform != "win32", reason="no CPU affinity here")
def test_pin_this_process_and_back():
	before = cpus.Affinity()
	try:
		cpus.Pin(before[:1])
		assert cpus.Affinity() == before[:1]
	finally:
		cpus.Pin(before)
	assert cpus.Affinity() == before


def test_the_process_tree():
	# This process, and what it started
	import subprocess
	child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
	try:
		if not cpus.Processes():
			pytest.skip("no process list here")
		assert child.pid in cpus.Tree([os.getpid()])
	finally:
		child.kill()
		child.wait()


def test_placement_moves_campy_live_and_campy_acquire(monkeypatch):
	affinity = {}
	priority = {}
	monkeypatch.setattr(cpus, "Hybrid", lambda: {"performance": [0, 1], "efficiency": [2, 3]})
	monkeypatch.setattr(cpus, "Cores", lambda: {"performance": [0, 1], "efficiency": [2, 3]})
	monkeypatch.setattr(cpus, "Pin", lambda c, pid=None: affinity.__setitem__(pid, list(c)))
	monkeypatch.setattr(cpus, "Affinity", lambda pid=None: affinity.get(pid, [0, 1, 2, 3]))
	monkeypatch.setattr(cpus, "SetPriority", lambda name, pid=None: priority.__setitem__(pid, name))
	monkeypatch.setattr(cpus, "Tree", lambda pids: sorted(set(pids) | {30}))
	placement = cpus.Placement(live=10, campy=[20, 21])
	assert placement.Available() and placement.IsOn() is False
	text = placement.Set(True)
	assert placement.IsOn() is True
	assert affinity == {10: [0, 1], 20: [2, 3], 21: [2, 3], 30: [2, 3]} and priority == {10: "high"}
	assert text == "campy-live on CPUs 0-1 at high priority; campy-acquire (3 processes) on CPUs 2-3."
	placement.Set(False)
	assert placement.IsOn() is False and set(map(tuple, affinity.values())) == {(0, 1, 2, 3)}
	assert priority == {10: "normal"}
	assert not cpus.Placement(live=None).Available()


# ---------------------------------------------------------------- a GPU's interrupt CPU

def _Entries(values, size, at):
	# NtQuerySystemInformation's per-CPU entries, with values at byte at
	data = bytearray(size * len(values))
	for i, v in enumerate(values):
		struct.pack_into("<I", data, size * i + at, v)
	return bytes(data)


def test_interrupt_counts_parse_both_classes():
	performance = _Entries([10, 20, 30], 48, 40) # InterruptCount
	interrupts = _Entries([1, 2, 3], 24, 4) # DpcCount
	assert cpus._ParseCounts(performance, interrupts) == [11, 22, 33]
	assert cpus._Since([2 ** 32 - 5, 7], [3, 9]) == [8, 2] # the counts wrap


def test_the_cpu_that_took_the_burst_is_found():
	# The Precision 7875's RTX 4000 Ada: ~1 interrupt and ~1 DPC per launch on CPU 2
	idle = [40, 10, 12, 8, 9, 7]
	during = [42, 11, 8000 + 12, 9, 10, 8]
	found = cpus.InterruptCpus(during, idle, 4000)
	assert found["cpus"] == [2] and found["share"] > 0.99 and found["extra"] == 2.0 and found["launches"] == 4000
	assert "reason" not in found


def test_two_cpus_sharing_the_interrupts_are_both_left_out():
	found = cpus.InterruptCpus([5, 3000, 3000, 5], [5, 0, 0, 5], 3000)
	assert found["cpus"] == [1, 2] and found["share"] == 1.0


def test_no_cpu_when_the_burst_made_too_few_or_spread_them():
	few = cpus.InterruptCpus([10, 30, 10], [10, 10, 10], 4000)
	assert few["cpus"] == [] and "only 20 more" in few["reason"]
	spread = cpus.InterruptCpus([1000] * 8, [0] * 8, 4000) # each 1/8: none stands out
	assert spread["cpus"] == [] and "spread" in spread["reason"]
	# Below the idle count: no negative share
	assert cpus.InterruptCpus([0, 5000], [100, 0], 4000)["cpus"] == [1]


def test_burst_counts_launches_and_waits(monkeypatch):
	monkeypatch.setattr(cpus.sys, "platform", "win32")
	counts = iter([[0, 0, 0], [5, 4100, 5], [10, 4110, 10]])
	monkeypatch.setattr(cpus, "InterruptCounts", lambda: next(counts))
	monkeypatch.setattr(cpus.time, "sleep", lambda s: None)
	calls = {"launch": 0, "wait": 0}
	launch = lambda: calls.__setitem__("launch", calls["launch"] + 1)
	wait = lambda: calls.__setitem__("wait", calls["wait"] + 1)
	found = cpus.GpuInterruptCpus(launch, wait, launches=200, every=50)
	assert calls == {"launch": 201, "wait": 6} # one to start, 4 every 50, one at the end
	assert found["cpus"] == [1]
	monkeypatch.setattr(cpus.sys, "platform", "linux")
	assert cpus.GpuInterruptCpus(launch, wait) is None


def test_avoiding_never_leaves_none():
	assert cpus.Avoiding([0, 1, 2, 3], [2]) == [0, 1, 3]
	assert cpus.Avoiding([0, 1], [5]) == [0, 1]
	assert cpus.Avoiding([2], [2]) == [2]
	assert cpus.Avoiding([0, 1], None) == [0, 1]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' counters")
def test_interrupt_counts_on_windows():
	counts = cpus.InterruptCounts()
	assert len(counts) == min(os.cpu_count(), 64) and sum(counts) > 0


def test_placement_keeps_campy_live_off_the_interrupt_cpu(monkeypatch):
	affinity = {}
	monkeypatch.setattr(cpus, "Hybrid", lambda: {"performance": [0, 1, 2], "efficiency": [3, 4]})
	monkeypatch.setattr(cpus, "Cores", lambda: {"performance": [0, 1, 2], "efficiency": [3, 4]})
	monkeypatch.setattr(cpus, "Pin", lambda c, pid=None: affinity.__setitem__(pid, list(c)))
	monkeypatch.setattr(cpus, "Affinity", lambda pid=None: affinity.get(pid, [0, 1, 2, 3, 4]))
	monkeypatch.setattr(cpus, "SetPriority", lambda name, pid=None: None)
	monkeypatch.setattr(cpus, "Tree", lambda pids: sorted(pids))
	placement = cpus.Placement(live=10, campy=[20], avoid=[1])
	placement.Set(True)
	assert affinity == {10: [0, 2], 20: [3, 4]} and placement.IsOn() is True
	placement.Set(False)
	assert affinity == {10: [0, 2, 3, 4], 20: [0, 1, 2, 3, 4]} and placement.IsOn() is False
