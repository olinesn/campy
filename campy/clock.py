"""
campy's host clock: when each frame reached the computer (hosttimes.npy, the
live feed's host times, ratcam_*.csv, trigger.json), and campy-live's and
campy-live-view's times that are compared with those. Every process that
makes such a time takes it from HostNs, so they are on one clock.

HostNs is time.monotonic_ns, except on Windows when that clock is coarse:
there, up to Python 3.12, time.monotonic is GetTickCount64 and moves in steps
of 15.6 ms (time.get_clock_info("monotonic").resolution == 0.015625), so
hosttimes.npy at 60 fps held only 0, 15, 16, 31 and 32 ms differences, and
campy-live's latencies came as 15, 16, 31, 32 or 47 ms. HostNs is then
time.perf_counter_ns: QueryPerformanceCounter, to 0.1 us, monotonic too, and
one clock in every process on the computer (two processes on the Pro Max Slim
agreed to their start gap). Python 3.13 makes time.monotonic that same clock
on Windows, so HostNs gives the same times from 3.12 and 3.13 there.

Durations within one process (inference, 3D) are timed with
time.perf_counter_ns everywhere, and the live feed's written times are
time.perf_counter_ns too: those are compared only with each other, never with
host times. On Windows with a coarse time.monotonic the two would be one
clock; elsewhere they need not be.
"""
import sys, time, types

COARSE_S = 1e-3 # a monotonic clock that moves in steps longer than this is coarse


def Pick(platform=None, resolution=None):
	"""
	The host clock (a function returning ns) for platform (default
	sys.platform) when time.monotonic moves in steps of resolution seconds
	(default: this Python's).
	"""
	platform = sys.platform if platform is None else platform
	if resolution is None:
		resolution = time.get_clock_info("monotonic").resolution
	if platform == "win32" and resolution > COARSE_S:
		return time.perf_counter_ns
	return time.monotonic_ns


HostNs = Pick()
NAME = "time.perf_counter_ns" if HostNs is time.perf_counter_ns else "time.monotonic_ns"


class TimeOnHostClock(types.ModuleType):
	"""
	The time module, with monotonic_ns and monotonic on campy's host clock
	(HostNs): for a library that stamps times with time.monotonic_ns, set as
	its module's time (nireyecam, campy/cameras/ratcam.py).
	"""

	def __init__(self):
		super().__init__("time")
		self.monotonic_ns = HostNs
		self.monotonic = (lambda: HostNs() / 1e9) if HostNs is not time.monotonic_ns else time.monotonic

	def __getattr__(self, name):
		return getattr(time, name)
