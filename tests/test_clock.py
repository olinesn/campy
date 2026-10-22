"""campy's host clock (campy/clock.py): precise on Windows too, and the one clock of every host time."""

import csv, importlib, itertools, os, re, sys, time

import numpy as np
import pytest

from campy import clock

import test_frametimes

CAMPY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "campy")


@pytest.mark.parametrize("platform, resolution, expected", [
	("win32", 0.015625, time.perf_counter_ns), # Python up to 3.12: GetTickCount64
	("win32", 1e-7, time.monotonic_ns),        # Python 3.13: QueryPerformanceCounter already
	("linux", 1e-9, time.monotonic_ns),
	("linux", 0.015625, time.monotonic_ns),    # only Windows' is replaced
	("darwin", 4.2e-8, time.monotonic_ns),
	])
def test_pick(platform, resolution, expected):
	assert clock.Pick(platform, resolution) is expected


@pytest.fixture
def reloaded(monkeypatch):
	# campy.clock as imported under monkeypatch's sys.platform and clock info; as it was again after
	yield lambda: importlib.reload(clock)
	monkeypatch.undo()
	importlib.reload(clock)


def test_windows_with_a_coarse_monotonic_clock_uses_perf_counter(monkeypatch, reloaded):
	info = time.get_clock_info("monotonic")
	monkeypatch.setattr(sys, "platform", "win32")
	monkeypatch.setattr(time, "get_clock_info", lambda name: type(info)(implementation="GetTickCount64()",
		monotonic=True, adjustable=False, resolution=0.015625) if name == "monotonic" else info)
	reloaded()
	assert clock.HostNs is time.perf_counter_ns and clock.NAME == "time.perf_counter_ns"
	# Steps far finer than 15.6 ms
	t = [clock.HostNs() for _ in range(1000)]
	assert np.all(np.diff(t) >= 0) and min(d for d in np.diff(t) if d > 0) < 1_000_000


def test_elsewhere_it_is_monotonic(monkeypatch, reloaded):
	monkeypatch.setattr(sys, "platform", "linux")
	reloaded()
	assert clock.HostNs is time.monotonic_ns and clock.NAME == "time.monotonic_ns"


def test_this_computers_host_clock_is_fine():
	# Wherever the tests run, the host clock moves in steps under 1 ms
	t = [clock.HostNs() for _ in range(100000)]
	steps = [d for d in np.diff(t) if d > 0]
	assert steps and min(steps) < 1_000_000


def test_no_host_time_is_taken_from_another_clock():
	# Every host time compared across processes (hosttimes.npy, the live feed,
	# campy-live's rows, bookmarks, trigger.json, ratcam_*.csv) comes from
	# clock.HostNs: time.monotonic_ns, which is not it on Windows, appears
	# nowhere else in campy
	found = []
	for folder, _, files in os.walk(CAMPY):
		for name in files:
			path = os.path.join(folder, name)
			if name.endswith(".py") and os.path.basename(path) != "clock.py":
				with open(path, encoding="utf-8") as f:
					for n, line in enumerate(f, 1):
						if re.search(r"time\.monotonic_ns\(", line):
							found.append("{}:{}".format(os.path.relpath(path, CAMPY), n))
	assert found == []


def fake_host_clock(monkeypatch, start=7 * 10**15, step=16_666_667):
	# A host clock unlike time.monotonic_ns: far from it, and in 1/60 s steps, one per call
	ticks = itertools.count(start, step)
	monkeypatch.setattr(clock, "HostNs", lambda: next(ticks))
	return start, step


def test_hosttimes_are_on_the_host_clock(tmp_path, monkeypatch):
	start, step = fake_host_clock(monkeypatch)
	folder, meta = test_frametimes.record(tmp_path, monkeypatch)
	hosts = np.load(folder / "hosttimes.npy")
	assert len(hosts) == 300
	assert hosts[0] >= start and set(np.diff(hosts)) == {step}


def test_a_library_stamping_with_monotonic_ns_gets_the_host_clock(monkeypatch):
	start, step = fake_host_clock(monkeypatch)
	t = clock.TimeOnHostClock()
	a, b = t.monotonic_ns(), t.monotonic_ns()
	assert a >= start and b - a == step
	assert t.sleep is time.sleep and t.perf_counter is time.perf_counter


def test_ratcam_stamps_on_the_host_clock(tmp_path, monkeypatch):
	# nireyecam stamps its packets with time.monotonic_ns: on campy's host
	# clock instead, so ratcam_*.csv's host_ns and hosttimes.npy agree
	pytest.importorskip("nireyecam")
	from nireyecam import device
	import test_ratcam
	from campy.cameras import ratcam
	monkeypatch.setattr(device, "time", device.time) # put back after
	offset = 7 * 10**15
	monkeypatch.setattr(clock, "HostNs", lambda: time.perf_counter_ns() + offset)
	ratcam.StampOnHostClock()
	assert isinstance(device.time, clock.TimeOnHostClock)
	_, frametimes, meta, frames, sync = test_ratcam.record(tmp_path)
	hosts = np.load(tmp_path / "Camera0" / "hosttimes.npy")
	stamped = np.array([int(f["host_ns"]) for f in frames], np.int64)
	assert len(stamped) == len(hosts) > 0 and np.all(stamped >= offset)
	# Each frame reached nireyecam's reader thread before campy took it from the queue
	assert np.all(stamped <= hosts) and np.all(hosts - stamped < 1e9)
