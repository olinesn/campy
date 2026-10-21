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
