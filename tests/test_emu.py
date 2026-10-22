"""
Emulated cameras (cameraMake "emu", campy/cameras/emu.py), as stand-ins for
real ones in live tests: gray frames with one channel, one start shared by
every camera of a run, and a clip that loops without a pause.
"""

import multiprocessing as mp
import os
import re
import subprocess
import sys
import time
from collections import deque

import numpy as np
import pytest

from campy import livefeed
from campy.cameras import emu

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write_clip(path, frames=10, size=(64, 48)):
	# Frame i: red 10 * i, green and blue 0 (in gray, its luma), so each frame
	# says which it is
	import imageio_ffmpeg
	w = imageio_ffmpeg.write_frames(path, size, fps=20, pix_fmt_in="rgb24", macro_block_size=16,
		ffmpeg_log_level="error")
	w.send(None)
	for i in range(frames):
		img = np.zeros((size[1], size[0], 3), np.uint8)
		img[:, :, 0] = 20 * i
		w.send(img)
	w.close()


@pytest.fixture
def clip(tmp_path):
	path = str(tmp_path / "clip.mp4")
	write_clip(path)
	return path


def which(img, frames=10):
	# The frame's number in write_clip's clip, from its red
	red = img[:, :, 0] if img.ndim == 3 else None
	if red is None:
		raise ValueError("a gray frame")
	return int(round(float(red.mean()) / 20)) % frames


# ------------------------------------------------------------------ gray

def test_a_gray_emu_frame_has_one_channel(clip):
	# As a Mono8 FLIR camera's: height x width, so the writer (pixelFormatInput
	# gray) and the live feed (a height x width x 1 ring) take it. imageio's
	# reader gave 3 channels whatever the config said.
	gray = emu.Clip(clip, gray=True)
	rgb = emu.Clip(clip)
	try:
		for i in range(3):
			g, c = gray.get_data(i), rgb.get_data(i)
			assert g.shape == (48, 64) and g.dtype == np.uint8 and g.flags.c_contiguous and g.flags.writeable
			assert c.shape == (48, 64, 3) and c.flags.writeable
			# ffmpeg's gray is the frame's luma: about 0.3 of its red
			assert abs(float(g.mean()) - 0.299 * float(c[:, :, 0].mean())) < 3
	finally:
		gray.close()
		rgb.close()


def test_a_gray_emu_camera_opens_at_its_clips_size_and_fills_the_live_feed(tmp_path):
	(tmp_path / "Camera0").mkdir()
	write_clip(str(tmp_path / "Camera0" / "clip.mp4"))
	cam_params = {"videoFilename": "emuclip.mp4", "videoFolder": str(tmp_path), "cameraName": "Camera0",
		"frameRate": 100, "pixelFormatInput": "gray", "frameWidth": 1280, "frameHeight": 1024, "n_cam": 0,
		"liveFeed": True, "liveFeedName": "campy-test-emu-{}".format(os.getpid()), "displayDownsample": 2}
	camera, cam_params = emu.OpenCamera(cam_params)
	feed = livefeed.Feed(cam_params, say=lambda text: None)
	try:
		assert (cam_params["frameWidth"], cam_params["frameHeight"]) == (64, 48)
		feed.Prepare(cam_params) # the ring of a gray camera: 48 x 64 x 1
		ring = feed.writer.ring
		assert ring.shape == (48, 64, 1)
		for n in range(3):
			img = emu.GrabFrame(camera, n)
			assert img.shape == (48, 64)
			feed.Write(img, n + 1, time.monotonic_ns(), time.perf_counter())
			# The same ring: a frame of 3 channels made a new one, of 48 x 64 x 3,
			# which campy-live rejects
			assert feed.writer is not None and feed.writer.ring is ring
		assert ring.Get("written") == 3 and ring.Get("notWritten") == 0
		shown = deque()
		emu.DisplayImage(cam_params, shown, img) # the preview's downsampling, of a frame without channels
		assert shown[0].shape == (24, 32)
	finally:
		feed.Close()
		emu.CloseCamera(cam_params, camera)


# ------------------------------------------------------------------ the loop

def test_the_clip_loops_without_starting_ffmpeg_again(clip, monkeypatch):
	# Frame 10 is the clip's frame 0 again, from the same ffmpeg (-stream_loop):
	# imageio's reader went back to frame 0 by starting a new one, and the frame
	# after the clip's last came 110-150 ms late
	opened = []
	read_frames = emu.imageio_ffmpeg.read_frames
	monkeypatch.setattr(emu.imageio_ffmpeg, "read_frames", lambda *a, **k: opened.append(a) or read_frames(*a, **k))
	c = emu.Clip(clip)
	try:
		seen = [which(c.get_data(i)) for i in range(35)]
		assert seen == [i % 10 for i in range(35)]
		assert len(opened) == 1
		# The same frame again (a grab that did not count) is the same frame
		assert which(c.get_data(34)) == 4
		# Back to an earlier frame (nothing in campy asks for one): the clip again, from its start
		assert which(c.get_data(3)) == 3 and len(opened) == 2
	finally:
		c.close()


def test_a_clip_that_cannot_be_read_fails_as_its_camera_opens(tmp_path):
	with pytest.raises(Exception):
		emu.Clip(str(tmp_path / "no such clip.mp4"))


# ------------------------------------------------------------------ the shared start

class Value:
	# multiprocessing's Value, in this process
	value = 0.0


def test_an_emu_camera_waits_for_the_shared_start(clip):
	start = Value()
	camera = emu.EmulatedCamera(emu.Clip(clip), 20, start)
	try:
		# Until campy's main process says go: no frame, as a triggered camera waits for its trigger
		t = time.perf_counter()
		with pytest.raises(emu.NotStarted):
			emu.GrabFrame(camera, 0)
		assert 0.04 < time.perf_counter() - t < 0.5
		emu.StartClock(start, lead=0.1)
		assert start.value > time.perf_counter()
		for n in range(4):
			emu.GrabFrame(camera, n)
			# Frame n at the start plus n frame periods, not before
			assert time.perf_counter() >= start.value + n / 20.0
		assert time.perf_counter() - (start.value + 3 / 20.0) < 0.03
	finally:
		camera.close()


def test_without_a_shared_start_an_emu_camera_starts_its_own_clock(clip):
	# Outside campy-acquire (and as before): frame 0 at once, then on time
	camera = emu.EmulatedCamera(emu.Clip(clip), 20)
	try:
		t = time.perf_counter()
		emu.GrabFrame(camera, 0)
		assert time.perf_counter() - t < 0.04
		emu.GrabFrame(camera, 2)
		assert time.perf_counter() >= camera.startTime + 0.1
	finally:
		camera.close()


def grab(clip, start, ready, times, delay):
	# One camera process: it opens later than the others (delay), and says when each frame came
	time.sleep(delay)
	camera = emu.EmulatedCamera(emu.Clip(clip), 50, start)
	ready.put(True)
	got = []
	n = 0
	while n < 10:
		try:
			emu.GrabFrame(camera, n)
		except emu.NotStarted:
			continue
		got.append(time.perf_counter())
		n += 1
	camera.close()
	times.put((delay, got))


def test_the_same_frame_comes_at_the_same_time_from_every_camera(clip):
	# Three camera processes that open 0, 0.3 and 0.6 s apart. Each timed its
	# frames from its own first one, so frame n came up to 0.6 s apart, and
	# campy-live took every set as misaligned. With the start that campy's
	# main process sets once all are ready, it comes at the same moment.
	ctx = mp.get_context("spawn")
	start = ctx.Value("d", 0.0, lock=False)
	ready, times = ctx.Queue(), ctx.Queue()
	processes = [ctx.Process(target=grab, args=(clip, start, ready, times, d), daemon=True) for d in (0, 0.3, 0.6)]
	for p in processes:
		p.start()
	try:
		for p in processes:
			ready.get(timeout=60)
		emu.StartClock(start)
		results = dict(times.get(timeout=60) for p in processes)
	finally:
		for p in processes:
			p.join(10)
	got = np.array([results[d] for d in (0, 0.3, 0.6)]) # camera x frame
	assert got.shape == (3, 10)
	spread = got.max(axis=0) - got.min(axis=0)
	assert np.median(spread) < 0.01 and spread.max() < 0.03, spread # 0.6-2.4 ms and at most 4 ms, on a Mac
	# On the shared schedule: frame n at the start plus n / 50 s
	late = got - (start.value + np.arange(10) / 50.0)
	assert late.min() >= 0 and np.median(late) < 0.01, late


# ------------------------------------------------------------------ campy-acquire

# Loaded first by every Python that campy-acquire starts: camera n opens 0.3 s
# after camera n - 1, as camera processes started one after another on
# Windows did (here they open within a few ms of each other)
STAGGER = """
import importlib.abc, importlib.util, sys, time

class Stagger(importlib.abc.MetaPathFinder):
	def find_spec(self, name, path, target=None):
		if name != "campy.cameras.emu":
			return None
		sys.meta_path.remove(self)
		spec = importlib.util.find_spec(name)
		run = spec.loader.exec_module
		def exec_module(module):
			run(module)
			open_camera = module.OpenCamera
			def OpenCamera(cam_params):
				time.sleep(0.3 * cam_params["n_cam"])
				return open_camera(cam_params)
			module.OpenCamera = OpenCamera
		spec.loader.exec_module = exec_module
		return spec

sys.meta_path.insert(0, Stagger())
"""


def test_campy_acquire_with_emulated_cameras_for_a_live_test(tmp_path):
	# What a live test without hardware runs: 3 emulated gray cameras with the
	# live feed on. Every camera's frame n reaches the computer at the same
	# time (hosttimes.npy), and each camera's live feed is the one ring of
	# 48 x 64 x 1 it made as it started, as a Mono8 camera's. The cameras
	# open 0.3 s apart (STAGGER): each used to time its frames from its own
	# first one, so frame n came 0.3 and 0.6 s later on the second and third.
	names = ["Camera0", "Camera1", "Camera2"]
	for name in names:
		(tmp_path / name).mkdir()
		write_clip(str(tmp_path / name / "clip.mp4"), frames=7) # loops 8 times in 3 s
	config = tmp_path / "config.yaml"
	config.write_text("\n".join([
		"numCams: 3", 'cameraMake: "emu"', 'videoFolder: "{}"'.format(tmp_path.as_posix()), 'videoFilename: "emuclip.mp4"',
		"frameRate: 20", "recTimeInSec: 3", "chunkLengthInSec: 1", "displayFrameRate: 0",
		'pixelFormatInput: "gray"', 'pixelFormatOutput: "yuv420p"', "gpuID: -1", 'codec: "h264"',
		'preflight: "off"', "liveFeed: True", 'liveFeedName: "campy-test-emu-acquire-{}"'.format(os.getpid()), ""]))
	(tmp_path / "site").mkdir()
	(tmp_path / "site" / "sitecustomize.py").write_text(STAGGER)
	env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path / "site"), REPO]), PYTHONUNBUFFERED="1")
	env.pop("IMAGEIO_FFMPEG_EXE", None)
	code = "import sys; sys.argv = ['campy-acquire', sys.argv[1]]; from campy.campy import Main; Main()"
	proc = subprocess.run([sys.executable, "-c", code, str(config)], cwd=str(tmp_path), env=env,
		capture_output=True, text=True, timeout=120)
	out = proc.stdout
	assert proc.returncode == 0, out + proc.stderr

	session, = [d for d in tmp_path.iterdir() if re.fullmatch(r"\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d", d.name)]
	for name in names:
		rings = re.findall(r"{}: live feed in shared memory \S+ \(\d+ slots of (\S+) uint8\)".format(name), out)
		assert rings == ["48x64x1"], out
	host = np.array([np.load(str(session / name / "hosttimes.npy")) for name in names]) / 1e9
	assert host.shape == (3, 60) # 3 s at 20 fps, every frame
	spread = host.max(axis=0) - host.min(axis=0)
	assert np.median(spread) < 0.01 and spread.max() < 0.04, spread # 1.5-1.8 ms and at most 5 ms, on a Mac; 0.63 s before
	import imageio
	for name in names:
		video = imageio.get_reader(str(session / name / "emuclip.mp4"))
		try:
			assert video.count_frames() == 60
		finally:
			video.close()
