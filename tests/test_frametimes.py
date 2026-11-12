"""frametimes.npy and hosttimes.npy, written while the recording runs (campy/frametimes.py)."""

import csv
import os
import subprocess
import sys
import textwrap
import threading
import time
import types
from collections import deque

import numpy as np
import pytest
from scipy import io as sio

from campy import configurator, frametimes
from campy.cameras import unicam


def camera_times(n, drop=()):
	# What a camera gives: frame numbers with gaps where frames were dropped,
	# timestamps in seconds on its own clock, arrival times in ns
	numbers = [k for k in range(1, n + 1) if k not in drop]
	stamps = [1726543.210987654 + k * 0.0125 + 1e-7 * np.sin(k) for k in numbers]
	hosts = [123456789012345 + k * 12_500_000 + k % 7 for k in numbers]
	return numbers, stamps, hosts


def old_arrays(numbers, stamps, hosts):
	# How SaveMetadata made them before
	return np.array([numbers, [t - stamps[0] for t in stamps]]), np.array(hosts, dtype=np.int64)


def test_growing_npy_is_a_valid_file_after_every_append(tmp_path):
	one = frametimes.GrowingNpy(tmp_path / "one.npy", np.int64)
	two = frametimes.GrowingNpy(tmp_path / "two.npy", np.float64, rows=2)
	assert np.load(tmp_path / "one.npy").shape == (0,) and np.load(tmp_path / "two.npy").shape == (2, 0)
	expect_one, expect_two = [], []
	for batch in range(1, 5):
		values = list(range(10 * batch, 10 * batch + batch))
		one.Append(values)
		two.Append([(v, v / 3) for v in values])
		expect_one += values
		expect_two += [(v, v / 3) for v in values]
		assert np.array_equal(np.load(tmp_path / "one.npy"), expect_one)
		assert np.array_equal(np.load(tmp_path / "two.npy"), np.array(expect_two).T)
		# The header never changes size: the frames always start at byte 128
		assert os.path.getsize(tmp_path / "two.npy") == 128 + 16 * len(expect_two)
	one.Close()
	two.Close()


def test_same_arrays_as_before(tmp_path):
	numbers, stamps, hosts = camera_times(1000, drop=(17, 18, 400))
	times = frametimes.FrameTimes(str(tmp_path), 80)
	for n, t, h in zip(numbers, stamps, hosts):
		times.Add(n, t, h)
	times.Close()
	old_frames, old_hosts = old_arrays(numbers, stamps, hosts)
	frames, hosts_now = np.load(tmp_path / "frametimes.npy"), np.load(tmp_path / "hosttimes.npy")
	# The same values, bit for bit, in the same shape and type
	assert frames.shape == old_frames.shape and frames.dtype == old_frames.dtype
	assert np.array_equal(frames, old_frames) and np.array_equal(hosts_now, old_hosts) and hosts_now.dtype == np.int64
	assert times.count == len(numbers) and times.last == old_frames[1][-1]


def test_memory_holds_only_the_batch_not_yet_written(tmp_path):
	times = frametimes.FrameTimes(str(tmp_path), 80)
	numbers, stamps, hosts = camera_times(1000)
	for n, t, h in zip(numbers, stamps, hosts):
		times.Add(n, t, h)
		assert len(times.hosts) < 80 and len(times.frames) < 80
	times.Wait()
	assert np.load(tmp_path / "frametimes.npy").shape == (2, 960) # 12 batches of 80
	times.Close()


def test_a_process_that_dies_keeps_the_frames_it_wrote(tmp_path):
	# No Close, no Python cleanup: os._exit, as a crash would end it, once the
	# writer has written the batches handed to it
	script = textwrap.dedent("""
		import os, sys
		from campy import frametimes
		times = frametimes.FrameTimes(sys.argv[1], 10)
		for k in range(1, 36):
			times.Add(k, 100.0 + k / 80, 1000 + k)
		times.Wait()
		os._exit(3)
		""")
	done = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=60)
	assert done.returncode == 3, done.stderr
	frames, hosts = np.load(tmp_path / "frametimes.npy"), np.load(tmp_path / "hosttimes.npy")
	assert frames.shape == (2, 30) and list(frames[0]) == list(range(1, 31)) # 3 batches of 10; 5 not yet written
	assert list(hosts) == list(range(1001, 1031))


def test_a_process_that_ends_without_closing_is_not_held_open(tmp_path):
	# The writer thread is a daemon: waiting for batches, it does not keep the
	# process from ending
	script = textwrap.dedent("""
		import sys
		from campy import frametimes
		times = frametimes.FrameTimes(sys.argv[1], 10)
		for k in range(1, 36):
			times.Add(k, 100.0 + k / 80, 1000 + k)
		times.Wait()
		""")
	done = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=60)
	assert done.returncode == 0, done.stderr
	assert np.load(tmp_path / "frametimes.npy").shape == (2, 30)


class SlowDisk:
	"""
	GrowingNpy.Append held until go is set: a write the disk makes wait (ext4's
	journal, while ffmpeg writes the videos).
	"""
	def __init__(self, monkeypatch):
		self.go, self.waiting = threading.Event(), threading.Event()
		append = frametimes.GrowingNpy.Append
		def Append(npy, values):
			self.waiting.set()
			assert self.go.wait(30)
			append(npy, values)
		monkeypatch.setattr(frametimes.GrowingNpy, "Append", Append)


def test_a_slow_write_does_not_hold_up_add(tmp_path, monkeypatch):
	disk = SlowDisk(monkeypatch)
	numbers, stamps, hosts = camera_times(400)
	times = frametimes.FrameTimes(str(tmp_path), 80)
	start = time.monotonic()
	for n, t, h in zip(numbers, stamps, hosts):
		times.Add(n, t, h)
	assert disk.waiting.wait(30)
	# Five batches handed to the writer, which is stuck on the first: Add went on
	assert time.monotonic() - start < 5 and times.count == 400
	assert np.load(tmp_path / "frametimes.npy").shape == (2, 0)
	disk.go.set()
	times.Close()
	old_frames, old_hosts = old_arrays(numbers, stamps, hosts)
	assert np.array_equal(np.load(tmp_path / "frametimes.npy"), old_frames)
	assert np.array_equal(np.load(tmp_path / "hosttimes.npy"), old_hosts)


def test_close_waits_for_the_batches_still_queued_before_it_cuts(tmp_path, monkeypatch):
	disk = SlowDisk(monkeypatch)
	times = frametimes.FrameTimes(str(tmp_path), 10)
	for k in range(1, 36):
		times.Add(k, 100.0 + k / 80, 1000 + k)
	assert disk.waiting.wait(30)
	threading.Timer(0.3, disk.go.set).start()
	times.Close(keep=23)
	frames, hosts = np.load(tmp_path / "frametimes.npy"), np.load(tmp_path / "hosttimes.npy")
	assert list(frames[0]) == list(range(1, 24)) and list(hosts) == list(range(1001, 1024))


class FlakyDisk:
	"""
	GrowingNpy.Append raising OSError for the writes `fails` says: fails(file
	name, the write's number for that file, from 1) is True for one that fails.
	"""
	def __init__(self, monkeypatch, fails, error=OSError(5, "Input/output error")):
		self.tries = {}
		append = frametimes.GrowingNpy.Append
		def Append(npy, values):
			name = os.path.basename(npy.path)
			self.tries[name] = self.tries.get(name, 0) + 1
			if fails(name, self.tries[name]):
				raise error
			append(npy, values)
		monkeypatch.setattr(frametimes.GrowingNpy, "Append", Append)
		monkeypatch.setattr(frametimes, "CLOSE_WAIT_S", 0.01)


def add(times, numbers, stamps, hosts):
	for n, t, h in zip(numbers, stamps, hosts):
		times.Add(n, t, h) # never raises: the camera goes on


def test_a_failed_write_goes_in_with_the_next(tmp_path, monkeypatch, capsys):
	# The 2nd batch's writes fail (a disk that dropped out for a moment, a
	# file an antivirus held): its frames wait, and the 3rd batch's write puts
	# them in first. The files come out whole, as if nothing had failed.
	FlakyDisk(monkeypatch, lambda name, n: n == 2)
	numbers, stamps, hosts = camera_times(400, drop=(17, 300))
	times = frametimes.FrameTimes(str(tmp_path), 80, name="Camera0")
	add(times, numbers, stamps, hosts)
	times.Close()
	old_frames, old_hosts = old_arrays(numbers, stamps, hosts)
	assert np.array_equal(np.load(tmp_path / "frametimes.npy"), old_frames)
	assert np.array_equal(np.load(tmp_path / "hosttimes.npy"), old_hosts)
	assert times.error is None and times.failedWrites == 1 and times.saved == (len(numbers),) * 2
	out = capsys.readouterr().out
	assert out.count("Camera0: could not write frametimes.npy ({}): [Errno 5] Input/output error. Its frame times wait "
		"in memory, and go in with the next write that works.\n".format(tmp_path)) == 1
	assert "Camera0: frame times written again, after 1 failed write: frametimes.npy and hosttimes.npy have every " \
		"frame so far.\n" in out


def test_a_failed_last_write_is_tried_again_as_it_closes(tmp_path, monkeypatch):
	# The last batch, and the first try again as it closes, fail; the second try works
	FlakyDisk(monkeypatch, lambda name, n: n in (5, 6))
	numbers, stamps, hosts = camera_times(330)
	times = frametimes.FrameTimes(str(tmp_path), 80)
	add(times, numbers, stamps, hosts) # 4 batches of 80; Close writes the last 10
	times.Close()
	old_frames, old_hosts = old_arrays(numbers, stamps, hosts)
	assert np.array_equal(np.load(tmp_path / "frametimes.npy"), old_frames)
	assert np.array_equal(np.load(tmp_path / "hosttimes.npy"), old_hosts)
	assert times.error is None and times.failedWrites == 2


def test_writes_that_keep_failing_are_the_error_close_gives(tmp_path, monkeypatch, capsys):
	# From the 3rd batch on, nothing goes in. Close tries again CLOSE_TRIES
	# times, then says what the files lack; they keep the frames they have,
	# the video's first, one for one. Said once while it records, not at
	# every batch.
	disk = FlakyDisk(monkeypatch, lambda name, n: n >= 3, OSError(28, "No space left on device"))
	numbers, stamps, hosts = camera_times(500)
	times = frametimes.FrameTimes(str(tmp_path), 80, name="Camera0")
	add(times, numbers, stamps, hosts)
	times.Close() # never raises
	frames, hosts_now = np.load(tmp_path / "frametimes.npy"), np.load(tmp_path / "hosttimes.npy")
	old_frames, old_hosts = old_arrays(numbers, stamps, hosts)
	assert np.array_equal(frames, old_frames[:, :160]) and np.array_equal(hosts_now, old_hosts[:160])
	assert times.saved == (160, 160) and times.count == 500
	failed = 5 + frametimes.CLOSE_TRIES # batches 3 to 7 (the 7th, of 20 frames, as it closed), then the tries again
	assert disk.tries == {"frametimes.npy": 2 + failed, "hosttimes.npy": 2 + failed}
	assert times.error == "could not write all its frame times: frametimes.npy and hosttimes.npy have 160 of the " \
		"video's 500 frames (frametimes.npy: [Errno 28] No space left on device, {} failed writes)".format(failed)
	assert capsys.readouterr().out.count("could not write") == 1


def test_a_file_that_keeps_failing_alone(tmp_path, monkeypatch):
	# Each file on its own: hosttimes.npy's writes go on while frametimes.npy's fail
	FlakyDisk(monkeypatch, lambda name, n: name == "frametimes.npy" and n >= 2)
	numbers, stamps, hosts = camera_times(200)
	times = frametimes.FrameTimes(str(tmp_path), 80)
	add(times, numbers, stamps, hosts)
	times.Close()
	assert times.saved == (80, 200) and len(np.load(tmp_path / "hosttimes.npy")) == 200
	assert times.error.startswith("could not write all its frame times: frametimes.npy has 80 of the video's 200 "
		"frames (frametimes.npy: [Errno 5] Input/output error")


def test_files_short_of_frames_are_cut_as_the_video_is(tmp_path, monkeypatch):
	# Frame times that never went in are the last ones: the frames the video
	# lacks besides (keep, drop: its ffmpeg stopped) are left out of what the
	# files have, as of whole files, so that their rows are still the
	# video's first frames, one for one
	FlakyDisk(monkeypatch, lambda name, n: n >= 3)
	times = frametimes.FrameTimes(str(tmp_path), 10)
	for k in range(1, 36):
		times.Add(k, k / 80, 1000 + k)
	times.Close(keep=30, drop=[4, 25, 25, 31]) # frames 5 and 26 lost in a stopped ffmpeg; 31-35 never in a video
	video = [k for k in range(1, 31) if k not in (5, 26)]
	assert times.count == len(video) == 28 and times.saved == (19, 19)
	assert list(np.load(tmp_path / "frametimes.npy")[0]) == video[:19]
	assert list(np.load(tmp_path / "hosttimes.npy")) == [1000 + k for k in video[:19]]
	assert "have 19 of the video's 28 frames" in times.error


def test_a_file_that_could_not_be_made_is_made_with_the_next_batch(tmp_path, monkeypatch):
	made = frametimes.GrowingNpy.__init__
	calls = []
	def Init(npy, path, *a, **k):
		calls.append(path)
		if len(calls) == 1:
			raise PermissionError(13, "Permission denied")
		made(npy, path, *a, **k)
	monkeypatch.setattr(frametimes.GrowingNpy, "__init__", Init)
	numbers, stamps, hosts = camera_times(100)
	times = frametimes.FrameTimes(str(tmp_path), 40)
	add(times, numbers, stamps, hosts)
	times.Close()
	old_frames, old_hosts = old_arrays(numbers, stamps, hosts)
	assert np.array_equal(np.load(tmp_path / "frametimes.npy"), old_frames)
	assert np.array_equal(np.load(tmp_path / "hosttimes.npy"), old_hosts)
	assert times.error is None and times.failedWrites == 1


class CutFile:
	"""
	A GrowingNpy's file, whose next write puts in only its first `part` bytes
	and raises (a disk that dropped out part-way), or whose next flush raises
	with the bytes still held in the file object's buffer.
	"""
	def __init__(self, file, part=None, flush=False):
		self.file, self.part, self.flush_fails = file, part, flush

	def write(self, data):
		if self.part is not None:
			self.file.write(data[:self.part])
			self.file.flush()
			self.part = None
			raise OSError(5, "Input/output error")
		return self.file.write(data)

	def flush(self):
		if self.flush_fails:
			self.flush_fails = False
			raise OSError(5, "Input/output error")
		self.file.flush()

	def __getattr__(self, name):
		return getattr(self.file, name)


@pytest.mark.parametrize("cut", [dict(part=13), dict(part=200), dict(flush=True)])
def test_a_write_cut_part_way_is_mended_before_the_next(tmp_path, cut):
	# What the failed write left in the file, or in its file object, is left
	# out: the same values written again come out once, after the ones before
	two = frametimes.GrowingNpy(str(tmp_path / "two.npy"), np.float64, rows=2)
	two.Append([(1, 0.1), (2, 0.2)])
	two.file = CutFile(two.file, **cut)
	with pytest.raises(OSError):
		two.Append([(3, 0.3), (4, 0.4), (5, 0.5), (6, 0.6), (7, 0.7), (8, 0.8), (9, 0.9), (10, 1.0)])
	assert two.count == 2 and two.broken
	two.Append([(3, 0.3), (4, 0.4), (5, 0.5), (6, 0.6), (7, 0.7), (8, 0.8), (9, 0.9), (10, 1.0)])
	two.Append([(11, 1.1)])
	two.Close()
	frames = np.load(tmp_path / "two.npy")
	assert list(frames[0]) == list(range(1, 12)) and np.allclose(frames[1], np.arange(1, 12) / 10)
	assert os.path.getsize(tmp_path / "two.npy") == frametimes.HEADER_BYTES + 11 * 16


class Hold:
	"""
	Holds the camera thread as it grabs frame `at` + 1, until go is set: what
	is on disk then is what a recording killed there (kill -9, a crash)
	leaves. (An exception no longer stands in for that: GrabFrames closes the
	camera and saves metadata whatever it raises.)
	"""
	def __init__(self, at):
		self.at, self.reached, self.go = at, threading.Event(), threading.Event()


def written(path, count):
	# The file, once it holds count frames (the writer thread writes them while
	# the camera goes on; until its first batch, the file is there but empty)
	deadline = time.monotonic() + 30
	while time.monotonic() < deadline:
		try:
			if np.load(path).shape[-1] >= count:
				break
		except (OSError, EOFError, ValueError):
			pass
		time.sleep(0.01)
	return np.load(path)


def fake_camera_module(hold=None):
	def OpenCamera(cam_params):
		cam_params["cameraModel"] = "fake"
		cam_params["cameraSerialNo"] = "0"
		return object(), cam_params

	def GrabFrame(camera, n):
		if hold is not None and n == hold.at:
			hold.reached.set()
			hold.go.wait(30)
		return n

	return types.SimpleNamespace(
		OpenCamera=OpenCamera,
		StartGrabbing=lambda camera: True,
		GrabFrame=GrabFrame,
		GetImageArray=lambda grab: np.full((4, 4), grab % 256, dtype=np.uint8),
		GetTimeStamp=lambda grab: 5000.0 + grab * 0.01,
		ReleaseFrame=lambda grab: None,
		CloseCamera=lambda cam_params, camera: None,
		)


def record(tmp_path, monkeypatch, hold=None):
	monkeypatch.setattr(unicam, "ImportCam", lambda make: fake_camera_module(hold))
	p = configurator.DefaultParams()
	p.update(cameraMake="fake", cameraName="Camera0", videoFolder=str(tmp_path), frameRate=100,
		recTimeInSec=3, chunkLengthInSec=1, displayFrameRate=0, writeQueueMaxSec="None")
	unicam.GrabFrames(p, deque(), deque([], 1), deque([], 1))
	folder = tmp_path / "Camera0"
	with open(folder / "metadata.csv") as f:
		meta = dict(csv.reader(f))
	return folder, meta


def test_a_recording_cut_short_keeps_its_settings_and_frames(tmp_path, monkeypatch):
	hold = Hold(250)
	thread = threading.Thread(target=record, args=(tmp_path, monkeypatch, hold), daemon=True)
	thread.start()
	try:
		assert hold.reached.wait(30)
		folder = tmp_path / "Camera0"
		with open(folder / "metadata.csv") as f:
			meta = dict(csv.reader(f))
		# The settings, from when the camera started grabbing; no totals, which only a closed recording has
		assert meta["cameraName"] == "Camera0" and meta["cameraModel"] == "fake" and meta["frameRate"] == "100"
		assert "totalFrames" not in meta
		# Every frame up to the last batch (one a second at 100 fps): 200 of the 250,
		# once the writer thread has written the second batch
		frames = written(folder / "frametimes.npy", 200)
		assert list(frames[0]) == list(range(1, 201))
		assert np.allclose(frames[1], np.arange(200) * 0.01)
		# hosttimes.npy too, waited for: the writer thread writes it just after frametimes.npy, and a busy CI runner
		# (Python 3.9, 4 workers) now and then read it between the two, with only the first batch's 100
		assert len(written(folder / "hosttimes.npy", 200)) == 200
		assert not (folder / "frametimes.mat").exists() # written when the recording closes
	finally:
		hold.go.set()
		thread.join(30)


def test_a_closed_recording_has_every_file(tmp_path, monkeypatch):
	folder, meta = record(tmp_path, monkeypatch)
	frames = np.load(folder / "frametimes.npy")
	assert list(frames[0]) == list(range(1, 301))
	assert meta["totalFrames"] == "300" and meta["framesWritten"] == "300" and meta["framesDropped"] == "0"
	assert float(meta["totalTime"]) == frames[1][-1]
	mat = sio.loadmat(folder / "frametimes.mat")
	assert np.issubdtype(mat["frameNumber"].dtype, np.integer) # integers, as before
	assert mat["frameNumber"].shape == mat["timeStamp"].shape == (1, 300)
	assert np.array_equal(mat["frameNumber"][0], frames[0]) and np.array_equal(mat["timeStamp"][0], frames[1])


def test_trigger_gaps_counts_pulses_without_a_frame():
	gaps = frametimes.TriggerGaps()
	period = 1 / 30 * (1 + 50e-6) # the camera's clock, a little off the trigger's
	t = [k * period for k in range(1, 401) if k not in (5, 200, 201, 202) and not 300 <= k < 368]
	new, frames = [], []
	for n, x in enumerate(t, start=1):
		new.append(gaps.Add(x, n))
		frames.append(gaps.frame)
	# A gap before the period was known (pulse 5) is counted once it is, with the frame just after it: frame 5
	assert gaps.period == pytest.approx(period)
	assert new[30] == 1 and frames[30] == 5 and sum(new[:30]) == 0
	assert sum(new) == gaps.missed == 1 + 3 + 68
	assert new[t.index(203 * period)] == 3 and new[t.index(368 * period)] == 68
	assert frames[-1] == t.index(368 * period) + 1


def test_trigger_gaps_learns_a_trigger_at_another_rate():
	# frameRate 60 in the config, the trigger at 30: the camera's own period, so no gap at every step
	gaps = frametimes.TriggerGaps()
	for k in range(100):
		assert gaps.Add(k / 30, k + 1) == 0
	assert gaps.Add(102 / 30, 101) == 2 and gaps.missed == 2 and gaps.frame == 101


def test_the_step_over_a_camera_unplugged_is_no_missed_pulses():
	# cameraReconnect: the camera's timestamps step over the time it was gone; that step is not counted as trigger
	# pulses without a frame (the recording watch says the time it was gone apart), and counting goes on after it
	gaps = frametimes.TriggerGaps()
	t = [n / 30 for n in range(1, 61)]
	for n, s in enumerate(t, 1):
		gaps.Add(s, n)
	gaps.Restart()
	assert gaps.Add(130 / 30, 130) == 0 # back at frame 130: 69 numbers skipped, not counted
	assert gaps.Add(131 / 30, 131) == 0
	assert gaps.Add(134 / 30, 134) == 2 and gaps.missed == 2 # and pulses missed after it still are


def test_a_camera_back_is_numbered_as_the_other_cameras():
	# unicam.FrameNumberBack: by the other cameras' newest frames within a second (the median), else its own last
	period = 1 / 30
	ns = lambda s: int(s * 1e9)
	at = 10.0 # its first frame back came then
	# The others' newest frames: their frame 300 at 10.0 s (the same pulse), and one a frame behind (299 at 9.967 s)
	assert unicam.FrameNumberBack(ns(at + 0.002), period, [(300, ns(at)), (299, ns(at - period)), (300, ns(at))],
		60, ns(2.0)) == (300, True)
	# The others' frame of this pulse not in yet: theirs before it, a period earlier
	assert unicam.FrameNumberBack(ns(at), period, [(299, ns(at - period - 0.001))], 60, ns(2.0)) == (300, True)
	# No other camera within a second (one camera, or all paused): by its own last frame (frame 60 at 2.0 s)
	assert unicam.FrameNumberBack(ns(at), period, [(100, ns(5.0))], 60, ns(2.0)) == (300, False)
	assert unicam.FrameNumberBack(ns(at), period, [], 60, ns(2.0)) == (300, False)
	# Never at or below its last frame
	assert unicam.FrameNumberBack(ns(at), period, [(10, ns(at))], 60, ns(2.0)) == (61, True)
	# The offset carries its timestamps on from its last frame's: its clock started again at 0
	resume = unicam.Resume(60, ns(2.0), 2.0, period, 0.0)
	assert abs(resume.Offset(300, 0.25) - (2.0 + 240 * period - 0.25)) < 1e-12


def test_a_camera_back_asks_the_others_again_when_their_counters_were_changing(monkeypatch):
	# status.Camera.Others leaves out a camera whose counters changed between its two reads; on a busy computer that
	# can be every other camera at once (seen on GitHub's runner, 2026-11-12: numbered by its own last frame instead).
	# Resume.Number asks again, rather than taking an empty answer for no other camera
	period = 1 / 30
	answers = iter([[], [], [(300, int(10.0 * 1e9))]])
	resume = unicam.Resume(60, int(2.0 * 1e9), 2.0, period, 0.0)
	assert resume.Number(int(10.001 * 1e9), lambda: next(answers)) == (300, True)
	# None grabbing at all: given up after MATCH_WAIT_S, by its own last frame
	monkeypatch.setattr(unicam, "MATCH_WAIT_S", 0.02)
	assert resume.Number(int(10.0 * 1e9), lambda: []) == (300, False)
