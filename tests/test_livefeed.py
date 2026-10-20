"""
The live feed (liveFeed): each camera's frames copied into a ring of shared
memory, which campy never waits on, and campy-live's reading of them. No
camera, torch or sleap-nn needed (campy-live runs here with --model none).
"""

import csv, itertools, json, multiprocessing as mp, os, threading, time, types
from argparse import ArgumentParser
from collections import deque

import numpy as np
import pytest

from campy import configurator, livefeed, live
from campy.cameras import unicam

_count = itertools.count()


def name():
	# Short and unique: macOS allows 30 characters, and a test's ring must
	# not meet another's (or an earlier run's)
	return "cl{}x{}".format(os.getpid() % 100000, next(_count))


@pytest.fixture
def rings():
	# Writers made through it are closed at the end, whatever the test did
	made = []

	def Make(*args, **kwargs):
		writer = livefeed.RingWriter(*args, **kwargs)
		made.append(writer)
		return writer

	yield Make
	for writer in made:
		writer.Close()


def frame(n, shape=(16, 20), dtype=np.uint8):
	return np.full(shape, n % 251, dtype)


def test_layout_reads_with_plain_numpy(rings):
	# The module's doc says where everything is; a reader needs nothing else
	ring = name()
	w = rings(ring, (32, 40), np.uint8, slots=4, frameRate=60, camera=2, cameraName="Camera3_sideC")
	w.Write(frame(7, (32, 40)), 7, 123456789, 0.5)
	shm = livefeed.Attach(ring)
	try:
		h = np.frombuffer(shm.buf, np.uint8, 4096)
		assert bytes(h[:8]) == b"CAMPYLV1"
		assert h[8:12].view("<u4")[0] == 1 and h[12:16].view("<u4")[0] == 4096
		slots, height, width, channels = (int(x) for x in h[16:32].view("<u4"))
		assert (slots, height, width, channels) == (4, 32, 40, 1)
		assert bytes(h[32:40]).rstrip(b"\0") == b"|u1"
		slot_bytes = int(h[40:48].view("<u8")[0])
		assert slot_bytes % 4096 == 0 and slot_bytes >= 64 + 32 * 40
		assert h[48:56].view("<u8")[0] == 32 * 40
		assert h[64:72].view("<i8")[0] == livefeed.RECORDING
		n = int(h[72:80].view("<i8")[0])
		assert n == 7
		assert h[80:88].view("<f8")[0] == 60 and h[88:96].view("<i8")[0] == 2
		assert bytes(h[96:160]).rstrip(b"\0") == b"Camera3_sideC"
		assert h[160:168].view("<i8")[0] == 1
		start = 4096 + (n % slots) * slot_bytes
		fields = np.frombuffer(shm.buf, "<i8", 6, start)
		assert list(fields[[0, 1, 2, 3]]) == [7, 7, 7, 123456789]
		assert np.frombuffer(shm.buf, "<f8", 1, start + 32)[0] == 0.5
		image = np.frombuffer(shm.buf, np.uint8, height * width * channels, start + 64).reshape(height, width, channels)
		assert np.all(image == 7)
		del h, fields, image
	finally:
		shm.close()


def test_frames_and_fields_read_back(rings):
	ring = name()
	w = rings(ring, (16, 20), np.uint8, slots=4, frameRate=60, cameraName="Camera0")
	r = livefeed.RingReader(ring)
	try:
		assert (r.shape, r.dtype, r.slots, r.cameraName) == ((16, 20, 1), np.uint8, 4, "Camera0")
		assert r.Latest() == 0 and r.Read(1) is None # nothing yet
		image = np.arange(320, dtype=np.uint8).reshape(16, 20)
		assert w.Write(image, 1, 1000, 0.25)
		f = r.Read(1)
		assert (f.frameNumber, f.hostTime, f.timeStamp) == (1, 1000, 0.25)
		assert f.written > 0
		assert np.array_equal(f.image[..., 0], image)
		assert r.Latest() == 1 and r.Info("written") == 1
	finally:
		r.Close()


def test_color_frames(rings):
	ring = name()
	w = rings(ring, (8, 10, 3), np.uint8, slots=2)
	r = livefeed.RingReader(ring)
	try:
		image = np.random.default_rng(0).integers(0, 255, (8, 10, 3), dtype=np.uint8)
		w.Write(image, 3, 0)
		assert np.array_equal(r.Read(3).image, image)
	finally:
		r.Close()


def test_older_frames_are_overwritten_never_waited_for(rings):
	ring = name()
	w = rings(ring, (16, 20), np.uint8, slots=4)
	r = livefeed.RingReader(ring)
	try:
		for n in range(1, 11):
			w.Write(frame(n), n, n)
		assert r.Latest() == 10
		assert r.Read(6) is None # replaced by frame 10
		for n in (7, 8, 9, 10):
			assert np.all(r.Read(n).image == n)
		assert r.Read(11) is None # not yet
	finally:
		r.Close()


def test_a_frame_replaced_while_copied_is_refused(rings):
	# The writer sets begin before it copies: a reader that finds begin
	# changed after its copy got part of the next frame
	ring = name()
	w = rings(ring, (16, 20), np.uint8, slots=4)
	r = livefeed.RingReader(ring)
	try:
		w.Write(frame(5), 5, 0)
		w.ring.slotFields[5 % 4, livefeed.BEGIN] = 9 # frame 9 started into the slot
		assert r.Read(5) is None
		w.ring.slotFields[5 % 4, livefeed.BEGIN] = 5
		assert r.Read(5) is not None
	finally:
		r.Close()


def test_a_frame_of_another_size_is_counted_not_written(rings):
	ring = name()
	w = rings(ring, (16, 20), np.uint8, slots=4)
	assert not w.Write(np.zeros((16, 21), np.uint8), 1, 0)
	assert not w.Write(np.zeros((16, 20), np.uint16), 2, 0)
	assert w.ring.Get("latest") == 0 and w.ring.Get("notWritten") == 2


def test_close_ends_the_ring_and_removes_its_name(rings):
	ring = name()
	w = rings(ring, (16, 20), np.uint8, slots=4)
	r = livefeed.RingReader(ring)
	w.Write(frame(1), 1, 0)
	w.Close()
	try:
		assert r.Ended()
		assert np.all(r.Read(1).image == 1) # what it holds stays readable
	finally:
		r.Close()
	with pytest.raises(FileNotFoundError):
		livefeed.RingReader(ring)


def test_a_ring_left_behind_is_replaced(rings):
	# A campy that was killed leaves its ring (on POSIX, until its resource
	# tracker removes it): the next recording's camera takes the name over,
	# and readers of the old ring are told it ended
	ring = name()
	old = rings(ring, (16, 20), np.uint8, slots=4)
	r = livefeed.RingReader(ring)
	try:
		new = rings(ring, (16, 20), np.uint8, slots=4)
		# The old one's camera is gone: it never closes its ring. (Here, in
		# one process, its close would also unregister the name new holds.)
		old.ring.Release()
		old.ring = None
		assert r.Ended() or r.Replaced()
		new.Write(frame(1), 1, 0)
		r2 = livefeed.RingReader(ring)
		try:
			assert r2.ringId == new.ring.ringId and not r2.Ended()
			assert np.all(r2.Read(1).image == 1)
		finally:
			r2.Close()
	finally:
		r.Close()


def test_a_reader_ending_leaves_the_ring_to_campy(rings):
	# Python before 3.13 would remove shared memory a reader opened when that
	# reader ends (its resource tracker): Attach must not let it
	ring = name()
	w = rings(ring, (16, 20), np.uint8, slots=4)
	w.Write(frame(1), 1, 0)
	ctx = mp.get_context("spawn")
	p = ctx.Process(target=_read_once, args=(ring,))
	p.start()
	p.join(60)
	assert p.exitcode == 0
	time.sleep(0.5) # the tracker would have removed it by now
	r = livefeed.RingReader(ring)
	try:
		assert np.all(r.Read(1).image == 1)
	finally:
		r.Close()


def _read_once(ring):
	r = livefeed.RingReader(ring)
	assert r.Read(1) is not None
	r.Close()


def _write_fast(ring, frames, ready):
	w = livefeed.RingWriter(ring, (128, 160), np.uint8, slots=3)
	ready.set()
	try:
		for n in range(1, frames + 1):
			w.Write(np.full((128, 160), n % 251, np.uint8), n, n)
	finally:
		time.sleep(0.2)
		w.Close()


def test_another_process_never_reads_a_torn_frame():
	# A writer in its own process, as fast as it can, into 3 slots, and a
	# reader taking its newest frames: every frame read whole is all one value
	ring = name()
	ctx = mp.get_context("spawn")
	ready = ctx.Event()
	p = ctx.Process(target=_write_fast, args=(ring, 20000, ready))
	p.start()
	try:
		assert ready.wait(60)
		r = None
		deadline = time.monotonic() + 30
		while r is None and time.monotonic() < deadline:
			try:
				r = livefeed.RingReader(ring)
			except (FileNotFoundError, ValueError):
				time.sleep(0.01)
		assert r is not None
		read = refused = 0
		out = np.empty(r.shape, r.dtype)
		try:
			while not r.Ended():
				n = r.Latest()
				if n == 0:
					continue
				for k in (n, n - 1, n - 2):
					f = r.Read(k, out) if k > 0 else None
					if f is None:
						refused += 1
						continue
					read += 1
					assert f.frameNumber == k
					assert out.min() == out.max() == k % 251, "frame {} read torn".format(k)
		finally:
			r.Close()
		assert read > 100
	finally:
		p.join(60)
	assert p.exitcode == 0


# -------------------------------------------------------------- in campy

def fake_camera_module(seen, at=10, shape=(16, 20)):
	# A camera of 20 frames; at frame `at`, a reader looks at the live feed
	def OpenCamera(cam_params):
		cam_params["cameraModel"] = "fake"
		cam_params["cameraSerialNo"] = "0"
		return object(), cam_params

	def GrabFrame(camera, n):
		if n == at and seen.get("ring"):
			try:
				r = livefeed.RingReader(seen["ring"])
			except FileNotFoundError:
				seen["latest"] = None
			else:
				seen["latest"] = r.Latest()
				f = r.Read(r.Latest())
				seen["frame"] = (f.frameNumber, int(f.image.max()), f.hostTime, f.timeStamp)
				r.Close()
		return n

	return types.SimpleNamespace(
		OpenCamera=OpenCamera,
		StartGrabbing=lambda camera: True,
		GrabFrame=GrabFrame,
		GetImageArray=lambda grab: np.full(shape, (grab + 1) % 256, dtype=np.uint8),
		GetTimeStamp=lambda grab: (grab + 1) * 0.01,
		DisplayImage=lambda cam_params, q, grab: None,
		ReleaseFrame=lambda grab: None,
		CloseCamera=lambda cam_params, camera: None,
		)


def grab(tmp_path, monkeypatch, seen, **settings):
	monkeypatch.setattr(unicam, "ImportCam", lambda make: fake_camera_module(seen))
	p = configurator.DefaultParams()
	p.update(cameraMake="fake", cameraName="Camera0", n_cam=0, videoFolder=str(tmp_path), frameRate=100,
		recTimeInSec=0.2, chunkLengthInSec=1, displayFrameRate=0, writeQueueMaxSec="None",
		pixelFormatInput="gray", frameWidth=20, frameHeight=16)
	p.update(settings)
	(tmp_path / "Camera0").mkdir()
	writeQueue = deque()
	unicam.GrabFrames(p, writeQueue, deque([], 2), deque([], 1), deque([], 1))
	with open(tmp_path / "Camera0" / "metadata.csv") as f:
		meta = dict(csv.reader(f))
	return writeQueue, meta


def test_the_live_feed_is_off_by_default(tmp_path, monkeypatch, capsys):
	assert configurator.DefaultParams()["liveFeed"] is False
	prefix = name()
	seen = {"ring": livefeed.RingName(prefix, 0)}
	writeQueue, meta = grab(tmp_path, monkeypatch, seen, liveFeedName=prefix)
	assert seen["latest"] is None
	assert len(writeQueue) == 20
	assert "live feed" not in capsys.readouterr().out


def test_each_frame_goes_into_the_feed_as_it_is_recorded(tmp_path, monkeypatch, capsys):
	prefix = name()
	ring = livefeed.RingName(prefix, 0)
	seen = {"ring": ring}
	writeQueue, meta = grab(tmp_path, monkeypatch, seen, liveFeed=True, liveFeedName=prefix, liveFeedSlots=4)
	# At its 11th grab, frames 1-10 were in the ring, the newest frame 10
	assert seen["latest"] == 10
	number, value, host, stamp = seen["frame"]
	assert (number, value, stamp) == (10, 10, 0.1)
	host_times = np.load(tmp_path / "Camera0" / "hosttimes.npy")
	assert host == host_times[9] # the same time as hosttimes.npy's
	# The recording is the same as without it, and the ring went with it
	assert len(writeQueue) == 20 and meta["framesWritten"] == "20"
	assert "live feed in shared memory {} (4 slots of 16x20x1 uint8)".format(ring) in capsys.readouterr().out
	with pytest.raises(FileNotFoundError):
		livefeed.RingReader(ring)


def test_a_failing_feed_stops_and_the_recording_goes_on(tmp_path, monkeypatch, capsys):
	def Broken(self, *args):
		raise OSError("no room")

	monkeypatch.setattr(livefeed.RingWriter, "Write", Broken)
	prefix = name()
	writeQueue, meta = grab(tmp_path, monkeypatch, {}, liveFeed=True, liveFeedName=prefix)
	assert len(writeQueue) == 20 and meta["framesWritten"] == "20"
	out = capsys.readouterr().out
	assert out.count("the live feed stopped") == 1
	assert "at frame 1: no room. The recording goes on." in out
	with pytest.raises(FileNotFoundError):
		livefeed.RingReader(livefeed.RingName(prefix, 0))


def test_frames_of_another_size_than_said_make_a_new_ring(tmp_path, monkeypatch, capsys):
	# The ring is made as the camera starts, from frameWidth x frameHeight; a
	# camera that sends another size gets a ring of its frames' size
	prefix = name()
	seen = {"ring": livefeed.RingName(prefix, 0)}
	grab(tmp_path, monkeypatch, seen, liveFeed=True, liveFeedName=prefix, frameWidth=64)
	assert seen["latest"] == 10 and seen["frame"][1] == 10
	out = capsys.readouterr().out
	assert "16x64x1 uint8" in out and "16x20x1 uint8" in out


def test_options_on_the_command_line():
	clargs = configurator.ParseClargs(ArgumentParser(prog="campy-acquire"),
		["c.yaml", "--liveFeed", "True", "--liveFeedName", "rig2", "--liveFeedSlots", "16"])
	assert (clargs.liveFeed, clargs.liveFeedName, clargs.liveFeedSlots) == (True, "rig2", 16)


# -------------------------------------------------------------- campy-live

def test_the_newest_set_all_cameras_hold(rings):
	prefix = name()
	writers = [rings(livefeed.RingName(prefix, c), (16, 20), np.uint8, slots=4, frameRate=60) for c in range(3)]
	cams = live.Cameras([livefeed.RingName(prefix, c) for c in range(3)], poll=0.0001)
	try:
		assert cams.Attach(wait=5)
		out = np.empty((3, 16, 20, 1), np.uint8)
		for c, w in enumerate(writers):
			for n in range(1, 6 if c < 2 else 5): # the third camera is a frame behind
				w.Write(frame(n), n, 1000 * n + c)
		frames, skipped = cams.NextSet(out)
		assert [f.frameNumber for f in frames] == [4, 4, 4] and skipped == 0
		assert np.all(out == 4)
		assert [f.hostTime for f in frames] == [4000, 4001, 4002]
		for w in writers:
			for n in range(5, 10):
				if w.ring.Get("latest") < n:
					w.Write(frame(n), n, 1000 * n)
		frames, skipped = cams.NextSet(out)
		assert frames[0].frameNumber == 9 and skipped == 4 # 5-8 skipped, never queued
		for w in writers:
			w.Close()
		assert cams.NextSet(out) is None # the recording ended
	finally:
		cams.Close()


def _record(prefix, cameras, frames, rate, delay):
	time.sleep(delay)
	writers = [livefeed.RingWriter(livefeed.RingName(prefix, c), (16, 20), np.uint8, slots=4, frameRate=rate,
		camera=c, cameraName="Camera{}".format(c)) for c in range(cameras)]
	try:
		start = time.perf_counter()
		for n in range(1, frames + 1):
			while time.perf_counter() < start + n / rate:
				pass
			for w in writers:
				w.Write(frame(n), n, time.monotonic_ns(), n / rate)
	finally:
		for w in writers:
			w.Close()


def test_campy_live_reads_a_recording_to_its_end(tmp_path, capsys):
	# Started before campy, it waits for the feed, logs every set it reads,
	# and ends with the recording, with a summary
	prefix = name()
	t = threading.Thread(target=_record, args=(prefix, 3, 120, 200, 0.5))
	t.start()
	try:
		out = str(tmp_path / "live")
		code = live.Run(live.ParseArgs(["--model", "none", "--cameras", "3", "--name", prefix,
			"--out", out, "--report-sec", "0", "--wait-sec", "30"]))
	finally:
		t.join()
	assert code == 0
	rows = np.load(out + ".npy")
	summary = json.load(open(out + ".json"))
	assert len(rows) == summary["sets"] > 0
	assert np.all(np.diff(rows["frame"]) > 0)
	assert rows["frame"][-1] == 120
	# Each set is every camera's frame of that number, and skipped sets are counted
	assert summary["sets"] + summary["skipped"] == summary["frames_spanned"] == 120 - rows["frame"][0] + 1
	assert np.all(rows["host_ns"].max(1) <= rows["read_ns"]) and np.all(rows["read_ns"] <= rows["done_ns"])
	assert summary["cameras"] == ["Camera0", "Camera1", "Camera2"] and summary["misaligned"] == 0
	assert summary["ended"] == "the recording ended"
	text = capsys.readouterr().out
	assert "Waiting for the live feed" in text and "Done, the recording ended" in text


def test_campy_live_without_a_feed_gives_up(tmp_path):
	code = live.Run(live.ParseArgs(["--model", "none", "--cameras", "1", "--name", name(),
		"--out", str(tmp_path / "x"), "--wait-sec", "0.3"]))
	assert code == 1
