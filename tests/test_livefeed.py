"""
The live feed (liveFeed): each camera's frames copied into a ring of shared
memory, which campy never waits on, and campy-live's reading of them. No
camera, torch or sleap-nn needed (campy-live runs here with --model none).
"""

import csv, itertools, json, multiprocessing as mp, os, subprocess, sys, threading, time, types
from argparse import ArgumentParser
from collections import deque

import numpy as np
import pytest

from campy import clock, configurator, frametimes, livefeed, live, strictjson
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


def _write_fast(ring, frames, ready, shape=(128, 160)):
	w = livefeed.RingWriter(ring, shape, np.uint8, slots=3)
	ready.set()
	try:
		for n in range(1, frames + 1):
			w.Write(np.full(shape, n % 251, np.uint8), n, n)
	finally:
		time.sleep(0.2)
		w.Close()


def _read_against_a_fast_writer(frames, shape=(128, 160)):
	# A writer in its own process (so on another core), as fast as it can,
	# into 3 slots, and a reader here taking its newest frames. Returns the
	# frames read whole, the ones refused, and the torn ones: read whole but
	# not all one value, or with another frame's fields
	ring = name()
	ctx = mp.get_context("spawn")
	ready = ctx.Event()
	p = ctx.Process(target=_write_fast, args=(ring, frames, ready, shape))
	p.start()
	torn = []
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
					if not (f.frameNumber == f.hostTime == k and out.min() == out.max() == k % 251):
						torn.append(k)
		finally:
			r.Close()
	finally:
		p.join(600)
	assert p.exitcode == 0
	return read, refused, torn


def test_another_process_never_reads_a_torn_frame():
	read, refused, torn = _read_against_a_fast_writer(20000)
	assert not torn, "frames read torn: {}".format(torn[:10])
	assert read > 100


def test_the_feed_fences_memory_where_the_cpu_may_reorder_it():
	# x86 keeps stores and loads in order; ARM does not, so there the writer
	# and reader call C's atomic_thread_fence (macOS, and Linux with libatomic)
	import platform
	machine = platform.machine().lower()
	if machine in ("x86_64", "amd64", "i386", "i686"):
		assert livefeed.FENCE == "none needed (x86)"
	elif machine in ("arm64", "aarch64") and sys.platform in ("darwin", "linux"):
		assert livefeed.FENCE.startswith("atomic_thread_fence"), livefeed.FENCE
	livefeed._Release()
	livefeed._Acquire()


@pytest.mark.skipif(not os.environ.get("CAMPY_LIVEFEED_STRESS"),
	reason="long: set CAMPY_LIVEFEED_STRESS to a number of frames (e.g. 2000000)")
@pytest.mark.parametrize("shape", [(4, 16), (128, 160)], ids=["64B", "20kB"])
def test_another_process_never_reads_a_torn_frame_stress(shape):
	# The same, for millions of frames: small frames give the reader more
	# chances to catch one as it is replaced
	frames = int(os.environ["CAMPY_LIVEFEED_STRESS"])
	t = time.monotonic()
	read, refused, torn = _read_against_a_fast_writer(frames, shape)
	print("{} frames of {}x{} in {:.0f} s: {} read whole, {} refused, {} torn".format(
		frames, *shape, time.monotonic() - t, read, refused, len(torn)))
	assert not torn, "{} frames read torn, of {} read: {}".format(len(torn), read, torn[:10])
	assert read > 100


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
	unicam.GrabFrames(p, writeQueue, deque([], 1), deque([], 1))
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


def test_sets_none_could_be_read_whole_are_counted_as_skipped(rings, monkeypatch):
	# campy-live held up long enough that every set its rings still held was replaced as it copied it: it looks
	# again from the next frame, and the sets it passed are counted as skipped with the next set it reads. Before,
	# they were never counted (a busy Windows CI runner: 113 sets + 4 skipped of a recording's 120 frames)
	prefix = name()
	writers = [rings(livefeed.RingName(prefix, c), (16, 20), np.uint8, slots=4, frameRate=60) for c in range(3)]
	cams = live.Cameras([livefeed.RingName(prefix, c) for c in range(3)], poll=0.0001)
	try:
		assert cams.Attach(wait=5)
		out = np.empty((3, 16, 20, 1), np.uint8)
		for n in range(1, 5):
			for c, w in enumerate(writers):
				w.Write(frame(n), n, 1000 * n + c)
		frames, skipped = cams.NextSet(out)
		assert frames.number == 4 and skipped == 0
		for n in range(5, 10):
			for c, w in enumerate(writers):
				w.Write(frame(n), n, 1000 * n + c)
		real, tried = cams.ReadSet, []

		def ReadSet(n, out):
			# Sets 9, 8 and 7 (all the rings hold) replaced as they are copied; frame 10 comes meanwhile
			if len(tried) < 3:
				tried.append(n)
				if len(tried) == 3:
					for c, w in enumerate(writers):
						w.Write(frame(10), 10, 10000 + c)
				return None
			return real(n, out)
		monkeypatch.setattr(cams, "ReadSet", ReadSet)
		frames, skipped = cams.NextSet(out)
		assert tried == [9, 8, 7]
		assert frames.number == 10 and skipped == 5 # 5-9
		monkeypatch.setattr(cams, "ReadSet", real)
		for c, w in enumerate(writers):
			w.Write(frame(11), 11, 11000 + c)
		assert cams.NextSet(out)[1] == 0 # counted once
	finally:
		cams.Close()


def test_no_set_before_the_first_one_read_is_counted_as_skipped(rings, monkeypatch):
	# The same before campy-live's first set: the sets it passed came before the first one it read, and a
	# recording's summary counts from that first one (frames_spanned), so none are skipped
	prefix = name()
	writers = [rings(livefeed.RingName(prefix, c), (16, 20), np.uint8, slots=4, frameRate=60) for c in range(3)]
	cams = live.Cameras([livefeed.RingName(prefix, c) for c in range(3)], poll=0.0001)
	try:
		assert cams.Attach(wait=5)
		out = np.empty((3, 16, 20, 1), np.uint8)
		for n in range(1, 5):
			for c, w in enumerate(writers):
				w.Write(frame(n), n, 1000 * n + c)
		real, tried = cams.ReadSet, []

		def ReadSet(n, out):
			if len(tried) < 3:
				tried.append(n)
				if len(tried) == 3:
					for c, w in enumerate(writers):
						w.Write(frame(5), 5, 5000 + c)
				return None
			return real(n, out)
		monkeypatch.setattr(cams, "ReadSet", ReadSet)
		frames, skipped = cams.NextSet(out)
		assert tried == [4, 3, 2] and frames.number == 5 and skipped == 0
	finally:
		cams.Close()


def test_rings_left_by_a_killed_recording_are_passed_by(rings, capsys):
	# A campy-acquire killed mid-recording left its rings RECORDING: their camera processes (the descriptions' pid)
	# are gone. campy-live waits for the next recording's, rather than track old frames and then take the new
	# rings for the recording's end
	prefix = name()
	dead = subprocess.Popen([sys.executable, "-c", "pass"])
	dead.wait()
	left = strictjson.dumps({"kind": livefeed.CAMERA_KIND, "pid": dead.pid, "parent_pid": dead.pid})
	old = [livefeed.RingWriter(livefeed.RingName(prefix, c), (16, 20), np.uint8, 4, 60.0, c, "Camera{}".format(c),
		left) for c in range(2)]
	for w in old:
		w.Write(frame(500), 500, 500_000)
	cams = live.Cameras([livefeed.RingName(prefix, c) for c in range(2)], poll=0.0001)
	try:
		assert not cams.Attach(wait=0.5)
		said = capsys.readouterr().out
		assert said.count("was left by a recording that ended without closing it (its camera process {} is "
			"gone)".format(dead.pid)) == 2 # once a ring
		for w in old:
			w.Close()
		new = [rings(livefeed.RingName(prefix, c), (16, 20), np.uint8, slots=4, frameRate=60) for c in range(2)]
		assert cams.Attach(wait=5) # the next recording's: this process writes them
		for w in new:
			w.Write(frame(1), 1, 1000)
		out = np.empty((2, 16, 20, 1), np.uint8)
		assert [f.frameNumber for f in cams.NextSet(out)[0]] == [1, 1]
	finally:
		cams.Close()


def half_made(writer):
	# The writer's ring as campy-acquire leaves it for a moment: the shared memory made,
	# its header not written yet. Returns what makes it whole again.
	header = writer.ring.header
	magic = header[:8].copy()
	header[:8] = 0
	def Finish():
		header[:8] = magic
	return Finish


def test_a_ring_still_being_made_is_refused_and_let_go_of(rings, monkeypatch):
	# The reader closes the memory it opened: left open, it could not be closed
	# later (views of it were made), and on Windows it would keep the ring's memory
	opened = []
	attach = livefeed.Attach
	monkeypatch.setattr(livefeed, "Attach", lambda n: opened.append(attach(n)) or opened[-1])
	w = rings(name(), (16, 20), np.uint8, slots=4)
	finish = half_made(w)
	for _ in range(3):
		with pytest.raises(ValueError, match="not a campy live feed ring"):
			livefeed.RingReader(w.name)
	assert len(opened) == 3 and all(shm.buf is None for shm in opened)
	finish()
	reader = livefeed.RingReader(w.name)
	reader.Close()


def test_campy_live_waits_for_a_ring_still_being_made(rings):
	# Started before campy-acquire, campy-live can look at a camera's ring
	# between its making and its header: it waits, as for a ring not there yet
	# (it stopped with "is not a campy live feed ring")
	prefix = name()
	writers = [rings(livefeed.RingName(prefix, c), (16, 20), np.uint8, slots=4) for c in range(2)]
	finish = half_made(writers[1])
	cams = live.Cameras([w.name for w in writers], poll=0.0001)
	try:
		threading.Timer(0.5, finish).start()
		start = time.monotonic()
		assert cams.Attach(wait=10)
		assert time.monotonic() - start >= 0.4
		# The next recording's ring, being made as the others still record: not an
		# error, and the recording counts as ended once it is made
		writers[0].Close()
		w = rings(livefeed.RingName(prefix, 0), (16, 20), np.uint8, slots=4)
		finish = half_made(w)
		cams.Ended() # does not raise
		finish()
		assert cams.Ended()
	finally:
		cams.Close()


def _record(prefix, cameras, frames, rate, delay, trigger=None, lose=None):
	# campy-acquire's rings, made after delay, and frames at rate from when
	# trigger is set: campy makes its rings a few seconds before the trigger,
	# and a campy-live already waiting attaches meanwhile. A short recording
	# started at once could end between campy-live's looks for the feed
	# (every 20 ms), and it would wait for the next one.
	time.sleep(delay)
	writers = [livefeed.RingWriter(livefeed.RingName(prefix, c), (16, 20), np.uint8, slots=4, frameRate=rate,
		camera=c, cameraName="Camera{}".format(c)) for c in range(cameras)]
	try:
		if trigger is not None:
			trigger.wait(30)
		start = time.perf_counter()
		for n in range(1, frames + 1):
			# Waited for asleep: this thread spinning would hold the GIL, and every
			# sleep or system call of campy-live's would take 5 ms (Python's thread
			# switch interval) to get it back, a few sets a second. campy-acquire
			# is a process of its own.
			while time.perf_counter() < start + n / rate:
				time.sleep(max(0, start + n / rate - time.perf_counter()))
			# One host time for the set, as for cameras on one trigger: stamped
			# one by one, a thread switch between them (Python's is every 5 ms)
			# could put the set more than half a frame apart, misaligned
			host = clock.HostNs()
			for c, w in enumerate(writers):
				if lose is not None and c == lose[0] and n > lose[1]:
					if n == lose[1] + 1:
						w.Close() # the camera lost: its process closes its ring, the others record on
					continue
				w.Write(frame(n), n, host, n / rate)
	finally:
		for w in writers:
			w.Close()


def test_campy_live_reads_a_recording_to_its_end(tmp_path, capsys):
	# Started before campy, it waits for the feed, logs every set it reads,
	# and ends with the recording, with a summary
	out = str(tmp_path / "live")
	code = run_live(name(), out, ["--model", "none", "--report-sec", "0", "--wait-sec", "30"], frames=120,
		delay=0.5)
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


def test_campy_live_looks_for_the_feed_every_20_ms(monkeypatch):
	# Started first, it looks for the feed every 20 ms: a camera without a
	# trigger sends its first frame as its ring is made, at 60 fps one every
	# 17 ms; it looked every 0.2 s, and missed up to 12. Checked here by the
	# waits themselves, which no busy computer changes
	slept = []

	class Clock:
		def __getattr__(self, name):
			return getattr(time, name)

		def sleep(self, seconds):
			slept.append(seconds)
	monkeypatch.setattr(live, "time", Clock())
	cameras = live.Cameras(["campy-test-none-{}-{}".format(os.getpid(), c) for c in range(3)], say=lambda text: None)
	assert cameras.Attach(stop=lambda: len(slept) >= 5) is False
	assert len(slept) == 5 and max(slept) <= 0.02, slept


def test_campy_live_waiting_tracks_from_the_first_frames(tmp_path):
	# Started first, it reads the recording from its start. The first frame it
	# reads is the newest when its setup after attaching is done: frame 1 on a
	# Mac (setup 3-19 ms), 7 and 10 on a CI runner so busy it skipped 24 of 54
	# sets with no model. How often it looks is checked above, without timing
	out = str(tmp_path / "live")
	assert run_live(name(), out, ["--model", "none", "--report-sec", "0", "--wait-sec", "30"], frames=60, rate=60,
		delay=0.5, triggered=False) == 0
	rows = np.load(out + ".npy")
	assert rows["frame"][0] <= 30 and rows["frame"][-1] == 60


def log_sets(log, first, count):
	# count sets of 3 cameras' frames, as campy-live's loop gives them to Log.Row and Log.Add
	for n in range(first, first + count):
		frames = [types.SimpleNamespace(frameNumber=n, hostTime=1000 * n + c, written=1000 * n + 5, timeStamp=n / 60)
			for c in range(3)]
		log.Add(log.Row(frames, 0, 1000 * n + 10, 1000 * n + 20, {}), frames)


def test_campy_live_log_writes_do_not_hold_up_the_sets(tmp_path, monkeypatch):
	# A write the disk makes wait holds up only the log's writer thread, not
	# the next set; Close waits for it, and the file has every row
	go, waiting = threading.Event(), threading.Event()
	append = frametimes.GrowingNpy.Append
	def Append(npy, values):
		waiting.set()
		assert go.wait(30)
		append(npy, values)
	monkeypatch.setattr(frametimes.GrowingNpy, "Append", Append)
	log = live.Log(str(tmp_path / "live.npy"), 3, 5, 15, 60)
	start = time.monotonic()
	log_sets(log, 1, 300) # 5 seconds' rows, at 60 sets a second
	assert waiting.wait(30)
	assert time.monotonic() - start < 5 and log.sets == 300
	assert len(np.load(tmp_path / "live.npy")) == 0 # the first write still waiting
	threading.Timer(0.3, go.set).start()
	log_sets(log, 301, 20)
	log.Close()
	rows = np.load(tmp_path / "live.npy")
	assert list(rows["frame"]) == list(range(1, 321))
	assert list(rows["host_ns"][:, 2]) == [1000 * n + 2 for n in range(1, 321)]


def test_campy_live_log_says_a_write_that_fails(tmp_path, monkeypatch, caplog, capsys):
	# The sets go on, and the summary is still made, saying what failed
	def Append(npy, values):
		raise OSError(28, "No space left on device")
	monkeypatch.setattr(frametimes.GrowingNpy, "Append", Append)
	log = live.Log(str(tmp_path / "live.npy"), 3, 5, 15, 60)
	log_sets(log, 1, 200) # never raises
	log.Close()
	log.Close() # once is enough
	assert caplog.text.count("Could not write") == 1
	assert capsys.readouterr().out.count("No space left on device. Its rows end where the writes stopped.") == 1
	summary = log.Summary()
	assert summary["sets"] == 200 and summary["log_error"] == "OSError: [Errno 28] No space left on device"


def test_campy_live_stopped_by_an_error_keeps_its_rows(tmp_path, monkeypatch):
	# The rows already handed to the log's writer thread are written, though
	# the loop never got to close the log
	add = live.Log.Add
	def Add(log, *a, **k):
		if log.sets == 50:
			raise RuntimeError("the model failed")
		add(log, *a, **k)
	monkeypatch.setattr(live.Log, "Add", Add)
	out = str(tmp_path / "live")
	with pytest.raises(RuntimeError, match="the model failed"):
		run_live(name(), out, ["--model", "none", "--report-sec", "0", "--wait-sec", "30"], frames=400, delay=0.5)
	assert len(np.load(out + ".npy")) == 50


def test_campy_live_without_a_feed_gives_up(tmp_path):
	code = live.Run(live.ParseArgs(["--model", "none", "--cameras", "1", "--name", name(),
		"--out", str(tmp_path / "x"), "--wait-sec", "0.3"]))
	assert code == 1


# -------------------------------------------------------------- campy-live in 3D

def seeing(tmp_path, monkeypatch):
	# A model that "sees" 3 known mice through 3 calibrated cameras, as
	# --model seeing; returns campy-live's 3D arguments, the mice, and which
	# detection is which mouse in each camera
	import test_pose3d as scene3d
	cams = scene3d.rig()
	names = ["top", "left", "right"]
	with open(tmp_path / "calibration.toml", "w") as f:
		for i, (cam, label) in enumerate(zip(cams, names)):
			f.write('[cam_{}]\nname = "{}"\nsize = [1280, 1024]\nmatrix = {}\ndistortions = {}\nrotation = {}\n'
				'translation = {}\n\n'.format(i, label, cam.K.tolist(), cam.dist.tolist(), cam.R.tolist(), cam.t.tolist()))
	truth = scene3d.scene(1, n=3)[0] # (3 mice, 15 nodes, 3)
	rng = np.random.default_rng(0)
	keypoints, scores, order = scene3d.observe(cams, truth, rng, noise=0.2, drop_node=0.0, instances=5)

	class Seeing(live.NoModel):
		instances, nodes = 5, truth.shape[1]

		def __init__(self, args, n_frames, shape):
			pass

		def Predict(self, frames):
			return {"keypoints": keypoints, "peak_vals": scores,
				"centroids": keypoints[:, :, 0]} # where each mouse is: its first node

	monkeypatch.setitem(live.MODELS, "seeing", Seeing)
	args = ["--model", "seeing", "--report-sec", "0", "--wait-sec", "30", "--animals", "3",
		"--calibration", str(tmp_path / "calibration.toml"), "--calibration-names", ",".join(names)]
	return args, truth, order


def run_live(prefix, out, args, frames=60, rate=200, delay=0.3, triggered=True, lose=None):
	# campy-live on a recording of 3 cameras' frames, from a thread: its rings
	# made after delay, its frames from when campy-live has attached to them
	# (triggered), or else from when the rings are made, as cameras without a
	# trigger send them
	trigger = threading.Event()
	t = threading.Thread(target=_record, args=(prefix, 3, frames, rate, delay, trigger if triggered else None, lose))
	attach = live.Cameras.Attach

	def Attach(cameras, *a, **k):
		try:
			return attach(cameras, *a, **k)
		finally:
			trigger.set()

	t.start()
	try:
		with pytest.MonkeyPatch.context() as m:
			m.setattr(live.Cameras, "Attach", Attach)
			# --cpus none: the tests' own process stays where it is, on any CPU
			return live.Run(live.ParseArgs(["--cameras", "3", "--name", prefix, "--out", out, "--cpus", "none"] + args))
	finally:
		trigger.set()
		t.join()


def test_campy_live_matches_and_triangulates_the_animals(tmp_path, monkeypatch):
	# campy-live hands the model's keypoints to pose3d, and logs each mouse's
	# 3D under one identity
	args, truth, order = seeing(tmp_path, monkeypatch)
	out = str(tmp_path / "live3d")
	code = run_live(name(), out, args)
	assert code == 0
	rows = np.load(out + ".npy")
	summary = json.load(open(out + ".json"))
	last = rows[-1]
	assert list(last["n_views"]) == [3, 3, 3]
	# Each identity is one true mouse, seen as the right detection in every camera, and in the right place
	for identity in range(3):
		mouse = order[0][last["members"][identity][0]]
		assert all(order[c][last["members"][identity][c]] == mouse for c in range(3))
		error = np.linalg.norm(last["points3d"][identity] - truth[mouse], axis=-1)
		assert np.nanmax(error) < 3 # mm
	# The same identities in every set (the same frames each time)
	assert np.all(rows["members"] == rows["members"][0])
	assert np.all(rows["done_ns"] <= rows["pose3d_ns"])
	assert rows["frame"][-1] == 60 and np.all(np.diff(rows["frame"]) > 0)
	assert summary["pose3d"].startswith("3 animals, cameras top, left, right")
	assert summary["pose3d_ms"]["p50"] > 0
	# What campy-live-export needs to write LUC3D's files from the log alone
	assert summary["calibration"] == str(tmp_path / "calibration.toml")
	assert summary["calibration_names"] == ["top", "left", "right"] and summary["animals"] == 3
	assert "edges" in summary


def test_campy_live_makes_the_3d_while_the_model_runs_on_the_next_set(tmp_path, monkeypatch):
	# The 3D of a set is made on a thread of its own: the first set's 3D
	# waits until the model has run on a later set, which, made in turn,
	# would never come. The tracker still has every set in order. 3 s of
	# frames, for a campy-live that comes late to them (a slow runner).
	from campy import pose3d
	args, truth, order = seeing(tmp_path, monkeypatch)
	predicted = [] # one per set the model ran on
	predict = live.MODELS["seeing"].Predict
	def Predict(model, frames):
		predicted.append(1)
		return predict(model, frames)
	monkeypatch.setattr(live.MODELS["seeing"], "Predict", Predict)
	update = pose3d.Tracker.Update
	seen = []
	def Update(tracker, keypoints, scores=None, frame=None):
		if not seen:
			deadline = time.monotonic() + 10
			while len(predicted) < 2:
				assert time.monotonic() < deadline, "the model did not go on to the next set"
				time.sleep(0.001)
		seen.append(frame)
		return update(tracker, keypoints, scores, frame)
	monkeypatch.setattr(pose3d.Tracker, "Update", Update)
	out = str(tmp_path / "live3d")
	assert run_live(name(), out, args, frames=600) == 0
	rows = np.load(out + ".npy")
	assert list(rows["frame"]) == seen and seen == sorted(seen) and seen[-1] == 600
	assert np.all(rows["done_ns"] <= rows["pose3d_ns"]) and np.all(rows["n_views"] == 3)


def test_campy_live_stops_at_a_3d_error(tmp_path, monkeypatch):
	from campy import pose3d
	args, truth, order = seeing(tmp_path, monkeypatch)
	def Update(tracker, keypoints, scores=None, frame=None):
		raise ValueError("the 3D failed")
	monkeypatch.setattr(pose3d.Tracker, "Update", Update)
	with pytest.raises(ValueError, match="the 3D failed"):
		run_live(name(), str(tmp_path / "live3d"), args, frames=200)


# -------------------------------------------- a camera that stops, or loses a frame

PERIOD = 16_666_667 # ns, 60 fps


def three(rings, slots=8):
	prefix = name()
	writers = [rings(livefeed.RingName(prefix, c), (16, 20), np.uint8, slots=slots, frameRate=60, camera=c,
		cameraName="Camera{}".format(c)) for c in range(3)]
	said = []
	cams = live.Cameras([w.name for w in writers], poll=0.0001, say=said.append)
	assert cams.Attach(wait=5)
	return writers, cams, said


def pulse(writers, p, t0=10**12, numbers=None, skip=()):
	# Trigger pulse p: each camera's frame (its pixels say the pulse), numbered as that camera numbers it
	for c, w in enumerate(writers):
		if c not in skip:
			w.Write(frame(p), p if numbers is None else numbers[c], t0 + p * PERIOD + 100_000 * c, p / 60)


def pulses(out):
	return [int(out[c, 0, 0, 0]) for c in range(len(out))]


def test_a_ring_that_ends_mid_recording_is_left_out_and_the_sets_go_on(rings):
	# Camera 2 is lost at frame 20 (its process closes its ring) while cameras 0 and 1 record on: the sets
	# went on waiting for camera 2, silently, for the rest of the recording
	writers, cams, said = three(rings)
	out = np.empty((3, 16, 20, 1), np.uint8)
	try:
		for p in range(1, 21):
			pulse(writers, p)
		assert cams.NextSet(out)[0].number == 20
		writers[2].Close()
		for p in range(21, 41): # under half a second of the others' frames: it could be the recording's end
			pulse(writers, p, skip=(2,))
		with pytest.raises(TimeoutError):
			cams.NextSet(out, stop=Timeout(0.3))
		assert said == []
		for p in range(41, 81):
			pulse(writers, p, skip=(2,))
		frames, skipped = cams.NextSet(out)
		assert frames.number == 80 and frames[2] is None and frames.own == [80, 80, -1]
		assert pulses(out)[:2] == [80, 80] and np.all(out[2] == 0) # a blank frame for the model
		assert len(said) == 1 and "Camera2's feed ended at its frame 20" in said[0] and "Camera0, Camera1" in said[0]
		report = cams.Report()
		assert report["sets_without"] == {"Camera2": 1} and report["left_out_at_end"] == {"Camera2": "ended"}
		assert report["events"][0]["event"] == "left out" and report["events"][0]["why"] == "ended"
		for w in writers[:2]:
			w.Close()
		assert cams.NextSet(out) is None # the recording ended
	finally:
		cams.Close()


class Timeout:
	def __init__(self, seconds):
		self.at = time.monotonic() + seconds

	def __call__(self):
		if time.monotonic() > self.at:
			raise TimeoutError
		return False


def test_a_camera_that_stalls_is_left_out_and_taken_back_when_it_resumes(rings, monkeypatch):
	# Camera 1 sends nothing for 1 s (STALL_S 0.3 here) while the others record on: it is left out, the sets
	# go on without it; its frames come again, numbered on from where it stopped (it lost the pulses in
	# between), and it is taken back, its frames found by host time
	monkeypatch.setattr(live, "STALL_S", 0.3)
	writers, cams, said = three(rings)
	out = np.empty((3, 16, 20, 1), np.uint8)
	got = []
	done = threading.Event()

	def Read():
		while not done.is_set():
			r = cams.NextSet(out, stop=done.is_set)
			if r is None:
				return
			got.append((r[0].number, list(r[0].own), pulses(out)))

	reader = threading.Thread(target=Read)
	reader.start()
	try:
		t0 = clock.HostNs()
		number1 = 0
		for p in range(1, 151): # 2.5 s at 60 fps
			stalled = 30 < p <= 90
			if not stalled:
				number1 += 1
			numbers = [p, number1, p]
			pulse(writers, p, t0, numbers, skip=(1,) if stalled else ())
			time.sleep(PERIOD / 1e9)
		time.sleep(0.2)
	finally:
		done.set()
		reader.join()
		cams.Close()
	assert any("No frame from Camera1" in s for s in said), said
	assert any("Camera1's frames came again" in s for s in said), said
	without = [g for g in got if g[1][1] == -1]
	assert without and all(g[2][0] == g[2][2] for g in without) # the others still paired
	# Once taken back, every set pairs one pulse's frames, camera 1's numbered 60 lower
	back = [g for g in got if g[0] > 90 and g[1][1] != -1]
	assert back, got
	for number, own, seen in back:
		assert seen[0] == seen[1] == seen[2] == number % 251 and own[1] == number - 60, (number, own, seen)
	assert cams.offset == [0, -60, 0]


def test_a_dropped_frame_is_paired_again_by_host_time(rings):
	# Camera 2 loses pulse 10's frame and numbers every later frame one lower: its frame of each number is
	# of the next pulse. After MISALIGNED_SETS sets in a row more than half a frame apart, its frames are
	# found again by host time, and each set pairs one pulse's frames from then on
	writers, cams, said = three(rings)
	out = np.empty((3, 16, 20, 1), np.uint8)
	try:
		def Pulse(p):
			if p == 10:
				pulse(writers, p, skip=(2,))
			else:
				pulse(writers, p, numbers=[p, p, p - 1 if p > 10 else p])
		for p in range(1, 21):
			Pulse(p)
		seen = []
		for p in range(21, 21 + live.MISALIGNED_SETS):
			frames, _ = cams.NextSet(out)
			seen.append(pulses(out))
			Pulse(p)
		assert seen[0] == [19, 19, 20] # paired by number: the wrong pulse
		assert cams.offset == [0, 0, -1]
		frames, _ = cams.NextSet(out)
		assert pulses(out) == [frames.number] * 3 and frames.own[2] == frames.number - 1
		assert len(said) == 1 and "Camera2's frames paired again by host time" in said[0] and "-1" in said[0]
		report = cams.Report()
		assert report["offsets"] == {"Camera2": -1} and report["offset_changes"] == 1
		event = report["events"][0]
		assert event["event"] == "offset" and event["offset"] == -1 and event["before"] == 0
		# More sets: the offset stays, and nothing more is said
		for p in range(21 + live.MISALIGNED_SETS, 40):
			Pulse(p)
			frames, _ = cams.NextSet(out)
			assert pulses(out) == [frames.number] * 3
		assert len(said) == 1
	finally:
		cams.Close()


def test_a_camera_that_lost_more_frames_than_its_ring_holds_is_paired_again(rings):
	# Camera 2 loses 12 frames at once (more than its 8 slots): no set number is held by every ring any more,
	# so no set came at all. Its frames are found again by host time
	writers, cams, said = three(rings)
	out = np.empty((3, 16, 20, 1), np.uint8)
	got = []
	done = threading.Event()

	def Read():
		while not done.is_set():
			r = cams.NextSet(out, stop=done.is_set)
			if r is None:
				return
			got.append((r[0].number, pulses(out)))

	reader = threading.Thread(target=Read)
	reader.start()
	try:
		t0 = clock.HostNs()
		for p in range(1, 61):
			if not 20 <= p < 32:
				pulse(writers, p, t0, numbers=[p, p, p - 12 if p >= 32 else p])
			else:
				pulse(writers, p, t0, skip=(2,))
			time.sleep(PERIOD / 1e9)
		time.sleep(0.1)
	finally:
		done.set()
		reader.join()
		cams.Close()
	assert cams.offset == [0, 0, -12], (cams.offset, said)
	late = [g for g in got if g[0] >= 40]
	assert late and all(seen == [number % 251] * 3 for number, seen in late), late


def test_a_camera_catching_up_on_held_frames_is_not_paired_by_their_host_times(rings):
	# A camera whose process was stopped comes back with the frames its driver held meanwhile, all at once:
	# their host times are those of their coming, not of their pulses. Paired by them, it got an offset 97
	# frames off on the EVO-X2. Now it is left as it is until its frames come on time again, then paired
	writers, cams, said = three(rings)
	try:
		t0 = 10**12
		for p in range(1, 31):
			pulse(writers, p, t0, skip=(2,) if p > 10 else ())
		# Camera 2's frames 11-30 (no frame lost, so numbered as the others'), all here within 2 ms, now
		now = t0 + 30 * PERIOD + 1_000_000
		for p in range(11, 31):
			writers[2].Write(frame(p), p, now + (p - 11) * 100_000, p / 60)
		cams.Leave(2, "stalled", time.monotonic())
		assert cams.Repair([0, 1, 2]) == {0, 1} and cams.offset == [0, 0, 0] # not placed: catching up
		# Then on time again: its frames paired with the same pulses' (offset 0)
		for p in range(31, 35):
			pulse(writers, p, t0, numbers=[p, p, p])
		assert 2 in cams.Repair([0, 1, 2]) and cams.offset == [0, 0, 0]
		assert not any("paired again" in s for s in said)
	finally:
		cams.Close()

def test_every_camera_pausing_at_once_is_no_stall_and_no_offset(rings, monkeypatch):
	# The trigger pauses (between a Pico's bouts): every camera stops at once for longer than STALL_S, then
	# goes on. No camera is left out, no offset taken, and the sets after the pause pair the same pulses
	monkeypatch.setattr(live, "STALL_S", 0.2)
	writers, cams, said = three(rings)
	out = np.empty((3, 16, 20, 1), np.uint8)
	got = []
	done = threading.Event()

	def Read():
		while not done.is_set():
			r = cams.NextSet(out, stop=done.is_set)
			if r is None:
				return
			got.append((r[0].number, list(r[0].own), pulses(out)))

	reader = threading.Thread(target=Read)
	reader.start()
	try:
		for p in range(1, 31):
			pulse(writers, p, clock.HostNs() - p * PERIOD) # each frame's host time: now
			time.sleep(PERIOD / 1e9)
		time.sleep(0.6) # the pause: 3 STALL_S
		for p in range(31, 61):
			# The cameras' frames come back one at a time: camera 2's first a few ms after the others'. One
			# host time for the pulse (a thread switch between the writes could put them half a frame apart)
			host = clock.HostNs()
			for c, w in enumerate(writers):
				w.Write(frame(p), p, host, p / 60)
				if c == 1 and p == 31:
					time.sleep(0.005)
			time.sleep(PERIOD / 1e9)
		time.sleep(0.1)
	finally:
		done.set()
		reader.join()
		cams.Close()
	assert said == [] and cams.offset == [0, 0, 0] and cams.left == {}
	assert all(-1 not in own for _, own, _ in got)
	assert any(number > 31 for number, _, _ in got)
	assert all(seen == [number % 251] * 3 for number, _, seen in got)


def test_the_log_row_of_a_set_without_a_camera(tmp_path):
	# A camera left out of the set: no host time, its own frame -1, and what the model made of its blank
	# frame dropped; its host time is not in the set's spread or latency
	log = live.Log(str(tmp_path / "log.npy"), 3, 2, 4, 60)
	frames = live.FrameSet([livefeed.Frame(None, 41, 1000, 0.5, 7), livefeed.Frame(None, 40, 1100, 0.5, 8), None])
	frames.number, frames.own = 41, [41, 40, -1]
	result = {"keypoints": np.ones((3, 2, 4, 2)), "peak_vals": np.ones((3, 2, 4)), "centroids": np.ones((3, 2, 2)),
		"centroid_vals": np.ones((3, 2))}
	row = log.Row(frames, 0, 2000, 3000, result)
	assert row["frame"] == 41 and list(row["cam_frame"]) == [41, 40, -1]
	assert list(row["host_ns"]) == [1000, 1100, 0] and np.isnan(row["stamp"][2])
	assert list(row["found"]) == [2, 2, 0] and np.isnan(row["keypoints"][2]).all()
	log.Add(row, frames)
	log.Close()
	summary = log.Summary()
	assert summary["misaligned"] == 0 and summary["host_time_spread_ms"]["max"] == pytest.approx(0.0001)
	assert np.load(tmp_path / "log.npy")["cam_frame"].tolist() == [[41, 40, -1]]


def test_campy_live_goes_on_with_the_other_cameras_when_one_is_lost(tmp_path, monkeypatch, capsys):
	# Camera 2 ("right") is lost at frame 60 of 300, at 200 fps: campy-live says so, and its later sets have
	# the other two cameras' keypoints and 3D, camera 2's columns empty; the summary says how many
	args, truth, order = seeing(tmp_path, monkeypatch)
	out = str(tmp_path / "lost")
	assert run_live(name(), out, args, frames=300, lose=(2, 60)) == 0
	said = capsys.readouterr().out
	assert "Camera2's feed ended at its frame 60 while the other cameras record on" in said, said
	rows = np.load(out + ".npy")
	summary = json.load(open(out + ".json"))
	without = rows[rows["cam_frame"][:, 2] == -1]
	assert len(without) and rows["frame"][-1] == 300
	assert summary["feed"]["sets_without"] == {"Camera2": len(without)}
	assert np.all(without["host_ns"][:, 2] == 0) and np.isnan(without["keypoints"][:, 2]).all()
	assert np.all(without["members"][:, :, 2] == -1) and np.all(without["n_views"] == 2)
	for identity in range(3):
		mouse = order[0][without[-1]["members"][identity][0]]
		error = np.linalg.norm(without[-1]["points3d"][identity] - truth[mouse], axis=-1)
		assert np.nanmax(error) < 3 # mm: the 3D from the two cameras left
	assert np.all(rows["cam_frame"][:, :2] == rows["frame"][:, None])


def test_campy_live_makes_its_model_before_the_feed_comes(tmp_path, monkeypatch, capsys):
	# For --frame-shape's frames, while it waits; again for a feed of
	# another size (16x20 here), which is then logged as any
	made = []

	class Model(live.NoModel):
		def __init__(self, args, n_frames, shape):
			made.append((n_frames, tuple(shape)))
			self.instances = 0

	monkeypatch.setitem(live.MODELS, "made", Model)
	out = str(tmp_path / "live")
	assert run_live(name(), out, ["--model", "made", "--report-sec", "0", "--wait-sec", "30",
		"--frame-shape", "32x40"], delay=0.5) == 0
	assert made == [(3, (32, 40, 1)), (3, (16, 20, 1))]
	text = capsys.readouterr().out
	assert text.index("Model: ") < text.index("Waiting for the live feed") < text.index("making it again")
	rows = np.load(out + ".npy")
	assert rows["frame"][-1] == 60


def test_campy_live_frame_shape():
	assert live.FrameShape("1024x1280x1") == (1024, 1280, 1)
	assert live.FrameShape("1024X1280") == (1024, 1280, 1)
	assert live.FrameShape("480x640x3") == (480, 640, 3)
	for bad in ("1024", "1024x0x1", "ax2", "1x2x3x4"):
		with pytest.raises(SystemExit):
			live.FrameShape(bad)


# -------------------------------------------------------------- campy-live's warm-up

def test_warm_up_covers_every_count_of_crops():
	# 3 cameras, at most 5 animals each: 1 to 15 crops, spread over the frames,
	# and a frame with more animals than are kept
	counts = live.WarmCounts(3, 5, range(1, 16))
	assert [sum(c) for c in counts[:-1]] == list(range(1, 16))
	assert all(len(c) == 3 and max(c) <= 5 for c in counts[:-1])
	assert counts[-1] == [6, 0, 0]
	assert live.WarmCounts(3, 5, ()) == [] # the random model: its own peaks


def test_warm_up_runs_the_whole_layer_with_animals_found(monkeypatch):
	# Each count goes through the model's Predict (the path a real set takes),
	# with the fake peaks in place only meanwhile
	backend = object()
	seen = []

	class Layer:
		centroid_layer = types.SimpleNamespace(backend=backend)

	model = object.__new__(live.TopDown)
	model.n_frames, model.instances, model.layer = 3, 5, Layer()
	model.fixed = None # without --fixed-crops, as __init__ leaves it
	monkeypatch.setattr(model, "Upload", lambda host: host, raising=False)
	monkeypatch.setattr(live, "Synchronize", lambda device: None)
	model.device = "cpu"

	def Predict(frames):
		b = model.layer.centroid_layer.backend
		seen.append(b.counts if isinstance(b, live.FakeCentroids) else None)
	model.Predict = Predict
	model.Warm("frames", crops=range(1, 16))
	assert seen[:3] == [None] * 3 # the frames as they are
	assert seen[3:] == live.WarmCounts(3, 5, range(1, 16))
	assert model.layer.centroid_layer.backend is backend


def test_fake_centroids_give_that_many_peaks():
	torch = pytest.importorskip("torch")

	class Backend:
		device = "cpu"

		def __call__(self, x):
			return {"output": torch.zeros(len(x), 1, 64, 80)}

	layer = types.SimpleNamespace(backend=Backend())
	with live.FakeCentroids(layer, [2, 0, 6]) as fake:
		assert layer.backend is fake and fake.device == "cpu"
		cm = layer.backend(torch.zeros(3, 1, 256, 320))["output"]
	assert isinstance(layer.backend, Backend)
	peaks = (cm == torch.nn.functional.max_pool2d(cm, 3, 1, 1)) & (cm > 0.2)
	assert peaks.sum((1, 2, 3)).tolist() == [2, 0, 6]


# -------------------------------------------------------------- keeping the GPU warm, and the CPUs

def test_the_gpu_clock_is_held_with_a_gpu(monkeypatch):
	# auto: on with CUDA and ROCm (not MPS or the CPU); on and off as asked
	torch = types.SimpleNamespace(version=types.SimpleNamespace(hip=None))
	monkeypatch.setitem(__import__("sys").modules, "torch", torch)
	model = types.SimpleNamespace(device="cuda")
	assert live.HoldsClock(live.ParseArgs([]), model)
	assert not live.HoldsClock(live.ParseArgs(["--hold-gpu-clock", "off"]), model)
	assert not live.HoldsClock(live.ParseArgs([]), types.SimpleNamespace(device="mps"))
	assert not live.HoldsClock(live.ParseArgs(["--hold-gpu-clock", "on"]), types.SimpleNamespace(device="cpu"))
	assert not live.HoldsClock(live.ParseArgs([]), live.NoModel)
	torch.version.hip = "6.4"
	assert live.HoldsClock(live.ParseArgs([]), model)
	assert not live.HoldsClock(live.ParseArgs(["--hold-gpu-clock", "off"]), model)
	assert live.ParseArgs([]).hold_gpu_clock_blocks == "auto"
	assert live.ParseArgs(["--hold-gpu-clock-blocks", "2"]).hold_gpu_clock_blocks == "2"
	with pytest.raises(SystemExit):
		live.ParseArgs(["--hold-gpu-clock-blocks", "many"])


def auto_clock(monkeypatch):
	# A GpuClock on auto, with its clock read from a list (no GPU)
	monkeypatch.setitem(__import__("sys").modules, "torch", types.SimpleNamespace())
	clock = live.GpuClock("cuda", "auto", reader=types.SimpleNamespace(name="fake"))
	clock.fastest = 2500.0
	return clock


class Looks:
	# Adjust on one look every LOOK_S, its state kept from call to call:
	# the blocks spinning after each look
	def __init__(self, clock):
		self.clock, self.state = clock, 0

	def __call__(self, mhzs, limited=False):
		blocks = []
		for mhz in mhzs:
			self.state = self.clock.Adjust(mhz, limited, self.state)
			blocks.append(self.clock.busy)
		return blocks


def test_auto_spins_a_block_more_while_the_clock_is_slow(monkeypatch):
	# LOW_LOOKS slow looks in a row: one block more, up to MOST_BUSY; a
	# single slow look is not enough
	clock = auto_clock(monkeypatch)
	looks = Looks(clock)
	assert clock.busy == clock.FIRST_BUSY == 1 and clock.MOST_BUSY == 2 and clock.LOW_LOOKS == 2
	assert looks([900, 2500, 900, 2490]) == [1, 1, 1, 1]
	assert looks([1635, 1635]) == [1, 2]
	assert looks([1635] * 6) == [2] * 6
	assert clock.changes == 1


def test_auto_does_not_fight_a_power_or_heat_limit(monkeypatch):
	clock = auto_clock(monkeypatch)
	assert Looks(clock)([1800] * 6, limited=True) == [1] * 6


def test_auto_never_spins_a_block_fewer_while_sets_come(monkeypatch):
	# Until #152 auto tried one fewer after every 10 s at full clock, and
	# until this, once per restart: on an RTX 6000 Ada each try at none
	# slowed every set 2-3 times for about 1 s. Now never, however long it's calm
	clock = auto_clock(monkeypatch)
	looks = Looks(clock)
	minutes = int(60 / clock.LOOK_S)
	assert looks([2475] * 30 * minutes) == [1] * 30 * minutes and clock.changes == 0
	# a slow clock adds the second block, and it stays, at full clock or not
	assert looks([1635, 1635]) == [1, 2] and clock.fewest == 2
	assert looks([2475] * 30 * minutes) == [2] * 30 * minutes and clock.changes == 1


def test_auto_remembers_a_gpus_fewest_until_its_computer_restarts(monkeypatch, tmp_path):
	monkeypatch.setitem(__import__("sys").modules, "torch", types.SimpleNamespace())
	monkeypatch.setattr(live.GpuClock, "GpuId", staticmethod(lambda torch, device: "GPU-abc"))
	boot = [1000.0]
	monkeypatch.setattr(live.GpuClock, "Boot", staticmethod(lambda: boot[0]))
	memory = str(tmp_path / "engines" / "gpu_clock.json")
	def Clock():
		c = live.GpuClock("cuda", "auto", reader=types.SimpleNamespace(name="fake"), fewest=0, memory=memory)
		c.fastest = 2500.0
		return c
	first = Clock()
	assert first.fewest == 1 and first.busy == 1 and first.remembered == 0
	looks = Looks(first)
	looks([2475] * 100 + [1000, 1000]) # 1 too few
	assert first.busy == first.fewest == 2
	first.Remember() # as Watch does when fewest grows
	assert json.load(open(memory))["GPU-abc"]["fewest"] == 2
	assert first.Describe()["fewest_learned"] == 2
	# the next run on this GPU starts at 2 and stays there
	second = Clock()
	assert second.fewest == 2 and second.remembered == 2 and second.busy == 2
	assert Looks(second)([2475] * 100) == [2] * 100
	assert second.Describe()["fewest_remembered"] == 2 and "fewest_learned" not in second.Describe()
	# after a restart: forgotten, 1 again
	boot[0] = 90000.0
	assert Clock().fewest == 1
	# another GPU: its own; an unreadable file: nothing remembered
	boot[0] = 1000.0
	monkeypatch.setattr(live.GpuClock, "GpuId", staticmethod(lambda torch, device: "GPU-other"))
	assert Clock().fewest == 1
	open(memory, "w").write("not json")
	monkeypatch.setattr(live.GpuClock, "GpuId", staticmethod(lambda torch, device: "GPU-abc"))
	assert Clock().fewest == 1
	# a fewest remembered by an older campy (1, from a try at none) is the floor anyway
	json.dump({"GPU-abc": {"fewest": 1, "boot": 1000.0}}, open(memory, "w"))
	assert Clock().fewest == 1
	# a fixed number of blocks neither reads nor writes it
	assert live.GpuClock("cuda", "2", memory=memory).memory is None


def test_auto_says_how_it_went(monkeypatch):
	clock = auto_clock(monkeypatch)
	clock.time, clock.clocks, clock.changes, clock.mhz = {1: 1.0, 0: 3.0}, [2400.0, 2475.0, 2475.0], 1, 2475.0
	assert clock.Describe() == {"blocks_while_sets_come": "auto", "fewest_blocks": 1, "clock_read_by": "fake",
		"blocks_share": {"0": 0.75, "1": 0.25}, "block_changes": 1, "clock_mhz_p50": 2475.0,
		"clock_mhz_min": 2400.0, "fastest_mhz": 2500.0}
	assert clock.Now() == "; GPU clock 2475 MHz, 1 block spinning"
	fixed = live.GpuClock("cuda", "2")
	assert fixed.busy == 2 and fixed.reader is None and fixed.Describe() == {"blocks_while_sets_come": 2}
	assert fixed.Now() == ""


def test_the_clock_readers_parse_what_the_drivers_give(tmp_path):
	sclk = tmp_path / "pp_dpm_sclk"
	sclk.write_text("0: 600Mhz \n1: 2117Mhz *\n2: 2900Mhz \n")
	reader = live.SysfsClock(None, "cuda", paths=[str(sclk)])
	assert reader.Read() == (2117.0, False)
	sclk.write_text("0: 600Mhz \n1: 1100Mhz \n2: 2900Mhz *\n")
	assert reader.Read() == (2900.0, False)
	sclk.write_text("")
	assert reader.Read() is None
	parse = live.SmiClock.Parse
	assert parse(live.SmiClock, "2475, 0x0000000000000000") == (2475.0, False)
	assert parse(live.SmiClock, "900, 0x0000000000000001") == (900.0, False) # idle: more blocks help
	assert parse(live.SmiClock, "1900, 0x0000000000000004") == (1900.0, True) # SW power cap
	assert parse(live.SmiClock, "[N/A], [N/A]") is None


def test_the_gpu_clock_spins_until_stopped():
	torch = pytest.importorskip("torch")
	if not torch.cuda.is_available():
		pytest.skip("no CUDA GPU here")
	clock = live.GpuClock("cuda")
	clock.CYCLES = 100000
	clock.Start()
	try:
		time.sleep(0.3)
		# the spinners (as many as the waiting needs), and with auto the
		# clock's reader too, where the GPU's clock can be read (#104)
		names = [thread.name for thread in clock.threads]
		spinners = [n for n in names if n.startswith("campy-live GPU clock ") and n[-1].isdigit()]
		assert len(spinners) == max(clock.WAITING_BLOCKS, clock.MOST_BUSY)
		assert names.count("campy-live GPU clock reader") == (clock.reader is not None)
		assert len(names) == len(spinners) + (clock.reader is not None)
	finally:
		# even when an assert fails: spinners left running at the
		# interpreter's exit abort it (terminate called without an active
		# exception, exit 134)
		clock.Stop()
	spins = clock.spins
	assert spins > 0 and clock.threads == []
	time.sleep(0.1)
	assert clock.spins == spins


def test_each_row_has_its_inference_time(tmp_path):
	out = str(tmp_path / "timed")
	assert run_live(name(), out, ["--model", "none", "--report-sec", "0", "--wait-sec", "30"]) == 0
	rows = np.load(out + ".npy")
	assert (rows["inference_ns"] > 0).all()


def test_campy_live_goes_on_the_performance_cores_by_default(monkeypatch, capsys):
	# --cpus auto on a hybrid CPU: the performance cores, at high priority;
	# campy-acquire's processes (its feeds say them) onto the efficiency cores
	from campy import cpus
	pinned, priority = [], []
	monkeypatch.setattr(cpus, "Hybrid", lambda: {"performance": [0, 1, 10], "efficiency": [2, 3]})
	monkeypatch.setattr(cpus, "Cores", lambda: {"performance": [0, 1, 10], "efficiency": [2, 3]})
	monkeypatch.setattr(cpus, "Pin", lambda c, pid=None: pinned.append((pid, list(c))))
	monkeypatch.setattr(cpus, "SetPriority", lambda name, pid=None: priority.append((pid, name)))
	monkeypatch.setattr(cpus, "Tree", lambda pids: sorted(set(pids) | {99}))
	monkeypatch.setattr(live, "GpuInterrupts", lambda args: None) # tests/test_live_cpus.py has those
	args = live.ParseArgs([])
	assert live.PlaceSelf(args)[0] == "CPUs 0-1,10 (the performance cores) at high priority"
	assert pinned == [(None, [0, 1, 10])] and priority == [(None, "high")]
	readers = [types.SimpleNamespace(Description=lambda pid=pid: json.dumps({"kind": livefeed.CAMERA_KIND,
		"pid": pid, "parent_pid": 7})) for pid in (11, 12)]
	monkeypatch.setattr(cpus, "Affinity", lambda pid=None: [0, 1, 2, 3, 10] if pid != 12 else [1, 2])
	moved = live.PlaceCampy(args, readers)
	assert pinned[1:] == [(7, [2, 3]), (11, [2, 3]), (12, [2, 3]), (99, [2, 3])]
	assert "campy-acquire (4 processes" in capsys.readouterr().out
	# campy-live ending (stopped, or crashed mid-recording): campy-acquire back on the CPUs it had; an encoder
	# started since (100) on the widest of them
	pinned.clear()
	monkeypatch.setattr(cpus, "Tree", lambda pids: sorted(set(pids) | {99, 100}))
	live.UnplaceCampy(moved)
	assert pinned == [(7, [0, 1, 2, 3, 10]), (11, [0, 1, 2, 3, 10]), (12, [1, 2]), (99, [0, 1, 2, 3, 10]),
		(100, [0, 1, 2, 3, 10])]
	assert "campy-acquire (5 processes) back on the CPUs it had" in capsys.readouterr().out
	monkeypatch.setattr(cpus, "Tree", lambda pids: sorted(set(pids) | {99}))
	# Asked not to: as it is
	pinned.clear(), priority.clear()
	args = live.ParseArgs(["--cpus", "none", "--campy-cpus", "none"])
	assert live.PlaceSelf(args) == (None, None)
	assert live.PlaceCampy(args, readers) is None
	live.UnplaceCampy(None)
	assert pinned == [] and priority == []
	# A list, at the priority asked
	args = live.ParseArgs(["--cpus", "4-5", "--priority", "above_normal"])
	assert live.PlaceSelf(args)[0] == "CPUs 4-5 at above normal priority"


def test_a_cpu_of_one_kind_is_left_as_it_is(monkeypatch):
	from campy import cpus
	monkeypatch.setattr(cpus, "Hybrid", lambda: None)
	monkeypatch.setattr(cpus, "Pin", lambda *a, **k: pytest.fail("pinned"))
	monkeypatch.setattr(cpus, "SetPriority", lambda *a, **k: pytest.fail("priority changed"))
	monkeypatch.setattr(live, "GpuInterrupts", lambda args: None) # not Windows, or no CPU stood out
	args = live.ParseArgs([])
	assert live.PlaceSelf(args) == (None, None)
	live.PlaceCampy(args, [])


def test_a_cameras_feed_says_its_processes(tmp_path):
	feed = livefeed.Feed({"liveFeed": True, "cameraName": "Camera0", "n_cam": 0, "liveFeedName": name(),
		"videoFolder": str(tmp_path)})
	d = json.loads(feed.Description())
	assert d == {"kind": livefeed.CAMERA_KIND, "pid": os.getpid(), "parent_pid": os.getppid(),
		"folder": os.path.join(str(tmp_path), "Camera0")}
	readers = [types.SimpleNamespace(Description=feed.Description), types.SimpleNamespace(Description=lambda: "")]
	assert livefeed.Processes(readers) == sorted({os.getpid(), os.getppid()})


def test_a_ring_says_its_camera_is_lost_until_its_next_frame(rings):
	# cameraReconnect: LOST while campy waits for the camera, RECORDING again with its next frame; never ended
	ring = name()
	w = rings(ring, (4, 4), np.uint8)
	w.Write(frame(1, (4, 4)), 1, 1000)
	r = livefeed.RingReader(ring)
	try:
		w.Lost()
		assert r.Lost() and not r.Ended()
		w.Write(frame(70, (4, 4)), 70, 2000)
		assert not r.Lost() and r.State() == livefeed.RECORDING and r.Latest() == 70
	finally:
		r.Close()


def test_campy_live_takes_back_a_camera_plugged_back_in(rings, monkeypatch):
	# cameraReconnect: camera 1 unplugged after pulse 30 (its ring LOST, not ended), plugged back in at pulse 91, its
	# frames numbered as the other cameras' again: left out meanwhile, then taken back, every set pairing one pulse's
	# frames, with no offset. Left out at once, as its feed says it is lost: not after STALL_S (1 s, here the
	# whole time it is gone), which held every camera's sets back that long, untracked
	writers, cams, said = three(rings)
	out = np.empty((3, 16, 20, 1), np.uint8)
	got = []
	done = threading.Event()

	def Read():
		while not done.is_set():
			r = cams.NextSet(out, stop=done.is_set)
			if r is None:
				return
			got.append((r[0].number, list(r[0].own), pulses(out)))

	reader = threading.Thread(target=Read)
	reader.start()
	try:
		t0 = clock.HostNs()
		for p in range(1, 151): # 2.5 s at 60 fps
			if p == 31:
				writers[1].Lost()
			pulse(writers, p, t0, skip=(1,) if 30 < p <= 90 else ())
			time.sleep(PERIOD / 1e9)
		time.sleep(0.2)
	finally:
		done.set()
		reader.join()
		cams.Close()
	assert any("Camera1 unplugged after its frame 30" in s for s in said), said
	assert not any("No frame from Camera1" in s for s in said), said
	assert any("Camera1's frames came again" in s for s in said), said
	# The sets without it start within a few pulses of the unplug (a loaded runner: up to 0.25 s), not 1 s later
	without = [g[0] for g in got if g[1][1] == -1]
	assert without and min(without) <= 45, without
	assert cams.Report()["events"][0]["why"] == "lost"
	back = [g for g in got if g[0] > 95 and g[1][1] != -1]
	assert back, got
	for number, own, seen in back:
		assert seen[0] == seen[1] == seen[2] == number % 251 and own[1] == number, (number, own, seen)
	assert cams.offset == [0, 0, 0]
