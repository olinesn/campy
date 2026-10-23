"""
Reading a recorded video's frames, in order, in batches, fast enough to keep
a GPU's pose models busy (campy-infer).

Every reader gives the video's frames as gray levels (H, W), uint8, the way
ffmpeg converts them to gray: a limited-range video (16-235) is stretched to
0-255, a full-range one is read as it is. They come in batches of `batch`
frames, (first frame's index, frames (b, H, W)), b = batch except perhaps the
last, from the first frame to the last, never skipping or repeating one. The
next batches are decoded while the caller works on this one; a batch is only
valid until the caller asks for the next.

  nvdec   the NVIDIA GPU's video decoder (PyNvVideoCodec): the frames stay on
          the GPU, as torch tensors on cuda; under one CPU core.
  ffmpeg  an ffmpeg process writing gray frames into a pipe: ffmpeg's own
          threads on the CPU, or with hwaccel (vaapi, cuda, qsv, d3d11va) the
          GPU's decoder through ffmpeg. numpy arrays.
  pyav    PyAV (ffmpeg's libraries in Python), in a process of its own: with
          PyTorch and OpenCV in the same process it hung on its first frames
          holding Python's interpreter lock. Its frames come through shared
          memory, without copies through pipes. numpy arrays.

Open picks one (auto: nvdec where PyNvVideoCodec and an NVIDIA GPU are, else
ffmpeg, with VA-API on a Linux PC with an AMD or Intel GPU), and says why.
Measured in docs/offline-inference.md.
"""

import os, queue, re, shutil, subprocess, sys, threading

import numpy as np

VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".avi")


def FfmpegExe():
	# ffmpeg on the PATH, else imageio-ffmpeg's own (campy's dependency)
	found = shutil.which("ffmpeg")
	if found:
		return found
	try:
		import imageio_ffmpeg
		return imageio_ffmpeg.get_ffmpeg_exe()
	except Exception:
		return "ffmpeg"


class VideoInfo:
	"""
	A video's size, rate, frame count and colour range, from ffmpeg's own
	description of it (and OpenCV's frame count, where OpenCV is installed).
	frames is a guess (the duration times the rate) without OpenCV, and
	readers don't rely on it: they read to the end.
	"""

	def __init__(self, path, ffmpeg=None):
		self.path = path
		r = subprocess.run([ffmpeg or FfmpegExe(), "-hide_banner", "-nostdin", "-i", path], capture_output=True,
			text=True, errors="replace")
		text = r.stderr
		stream = re.search(r"Stream #\S+.*?: Video: (.*)", text)
		if not stream:
			raise ValueError("{}: no video stream ({})".format(path, text.strip().splitlines()[-1] if text.strip()
				else "ffmpeg said nothing"))
		line = stream.group(1)
		self.codec = line.split()[0]
		size = re.search(r", (\d{2,5})x(\d{2,5})", line)
		self.width, self.height = int(size.group(1)), int(size.group(2))
		rate = re.search(r", ([\d.]+) fps", line) or re.search(r", ([\d.]+) tbr", line)
		self.rate = float(rate.group(1)) if rate else 0.0
		# the pixel format, the field after the codec (fields split at the
		# commas outside parentheses), and its details: full range for yuvj
		# formats or (pc), gray formats too
		fields, depth, part = [], 0, ""
		for ch in line:
			depth += (ch in "([") - (ch in ")]")
			if ch == "," and depth == 0:
				fields.append(part.strip())
				part = ""
			else:
				part += ch
		fields.append(part.strip())
		fmt = re.match(r"(\w+)(\((.*)\))?", fields[1] if len(fields) > 1 else "")
		pixfmt = fmt.group(1) if fmt else ""
		details = [d.strip() for d in (fmt.group(3) or "").split(",")] if fmt else []
		self.pixfmt = pixfmt
		self.fullRange = pixfmt.startswith("yuvj") or pixfmt.startswith("gray") or "pc" in details
		self.bitDepth = 10 if re.search(r"p10|p12|p16", pixfmt) else 8
		duration = re.search(r"Duration: (\d+):(\d+):([\d.]+)", text)
		seconds = int(duration.group(1)) * 3600 + int(duration.group(2)) * 60 + float(duration.group(3)) \
			if duration else 0.0
		self.frames = int(round(seconds * self.rate)) if self.rate else 0
		self.exact = False # whether frames is a count rather than a guess
		try:
			import cv2
			cap = cv2.VideoCapture(path)
			n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
			cap.release()
			if n > 0:
				self.frames, self.exact = n, True
		except Exception:
			pass

	def __repr__(self):
		return "{}x{} {} {}, {:g} fps, {}{} frames, {} range".format(self.width, self.height, self.codec, self.pixfmt,
			self.rate, "" if self.exact else "about ", self.frames, "full" if self.fullRange else "limited")


def LimitedToFull(y):
	"""
	A limited-range video's luma (16-235) as gray levels 0-255, as ffmpeg's
	gray conversion gives them (to within a level): what the GPU decoder's
	luma plane needs to match the other readers. numpy or torch, uint8.
	"""
	if isinstance(y, np.ndarray):
		f = (y.astype(np.float32) - 16) * (255 / 219)
		return np.clip(np.rint(f), 0, 255).astype(np.uint8)
	return ((y.float() - 16) * (255 / 219)).round_().clamp_(0, 255).to(y.dtype)


# ---------------------------------------------------------------- ffmpeg

def HwaccelAuto():
	"""
	The GPU decoder to ask ffmpeg for when none was named: VA-API on Linux
	with a render node and no NVIDIA driver (the EVO-X2's AMD GPU: 1,826
	frames/s against 1,487 on its CPU), else none: on the Pro Max Slim,
	NVDEC through ffmpeg was no faster than its CPU (686 against 702), and
	an NVIDIA GPU is better read with nvdec. ffmpeg decodes on the CPU by
	itself where the hardware decoder can't be opened.
	"""
	if sys.platform.startswith("linux") and os.path.exists("/dev/dri/renderD128") and \
			not os.path.exists("/proc/driver/nvidia"):
		return "vaapi"
	return "none"


def FfmpegCommand(path, hwaccel="none", frames=0, ffmpeg=None, vaapiDevice="/dev/dri/renderD128"):
	cmd = [ffmpeg or FfmpegExe(), "-v", "error", "-nostdin"]
	if hwaccel not in ("none", "", None):
		cmd += ["-hwaccel", hwaccel]
		if hwaccel == "vaapi":
			cmd += ["-hwaccel_device", vaapiDevice]
	cmd += ["-i", path, "-map", "0:v:0"]
	if frames:
		cmd += ["-frames:v", str(frames)]
	# every frame once, in order: no frames dropped or doubled to keep a rate
	cmd += ["-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
	return cmd


class FfmpegReader:
	"""An ffmpeg process decoding the video to gray frames in a pipe, read by a
	thread into a ring of batch arrays (no array made per frame)."""

	kind = "ffmpeg"

	def __init__(self, info, batch, hwaccel="none", frames=0, ffmpeg=None, ahead=4):
		self.info, self.B, self.hwaccel, self.limit = info, batch, hwaccel, frames
		self.ffmpeg, self.ahead = ffmpeg, ahead
		self.read = 0 # frames read so far
		self.error = None # what ffmpeg said, if it stopped early

	def Describe(self):
		return "ffmpeg ({})".format("on the CPU" if self.hwaccel in ("none", "", None) else
			"-hwaccel " + self.hwaccel)

	def __iter__(self):
		H, W, B = self.info.height, self.info.width, self.B
		cmd = FfmpegCommand(self.info.path, self.hwaccel, self.limit, self.ffmpeg)
		if "-fps_mode" in cmd and not FfmpegHas(cmd[0], "fps_mode"):
			i = cmd.index("-fps_mode") # ffmpeg before 5.1: the same as -vsync passthrough
			cmd[i:i + 2] = ["-vsync", "passthrough"]
		proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
		ring = [np.empty((B, H, W), np.uint8) for _ in range(self.ahead + 2)]
		free, full = queue.Queue(), queue.Queue(maxsize=self.ahead + 2)
		for k in range(len(ring)):
			free.put(k)
		stop = threading.Event()
		err = []
		threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True).start()

		def Read():
			try:
				i = 0
				while not stop.is_set():
					k = free.get()
					if k is None:
						break
					n = 0
					while n < B:
						view = memoryview(ring[k][n]).cast("B")
						got = 0
						while got < H * W:
							m = proc.stdout.readinto(view[got:])
							if not m:
								break
							got += m
						if got < H * W:
							break
						n += 1
					if n:
						full.put((i, k, n))
						i += n
					if n < B:
						break
				full.put(None)
			except BaseException as e:
				full.put(e)
		reader = threading.Thread(target=Read, daemon=True)
		reader.start()
		last = None
		try:
			while True:
				item = full.get()
				if last is not None:
					free.put(last) # the caller is done with the batch before
					last = None
				if item is None:
					break
				if isinstance(item, BaseException):
					raise item
				i, k, n = item
				last = k
				self.read = i + n
				yield i, ring[k][:n]
		finally:
			stop.set()
			free.put(None)
			if proc.poll() is None:
				proc.kill()
			proc.wait()
			reader.join(timeout=5)
		code = proc.returncode
		if code not in (0, None) and not self.limit:
			self.error = "ffmpeg stopped (exit {}): {}".format(code, (err[0] if err else b"").decode(
				errors="replace").strip()[-400:])


_HAS = {}


def FfmpegHas(exe, option):
	# whether this ffmpeg knows an option (-fps_mode came in ffmpeg 5.1)
	key = (exe, option)
	if key not in _HAS:
		try:
			r = subprocess.run([exe, "-hide_banner", "-h", "long"], capture_output=True, text=True, errors="replace",
				timeout=30)
			_HAS[key] = ("-" + option) in r.stdout
		except Exception:
			_HAS[key] = False
	return _HAS[key]


# ---------------------------------------------------------------- PyAV, in its own process

def _PyAVWorker(path, frames, B, name, shape, free, full, threads):
	# decodes into the shared ring's free slots, a batch per slot, in order
	from multiprocessing import shared_memory
	shm = shared_memory.SharedMemory(name=name)
	ring = np.ndarray(shape, np.uint8, buffer=shm.buf)
	try:
		import av
		container = av.open(path)
		stream = container.streams.video[0]
		stream.thread_type = "AUTO"
		stream.thread_count = threads
		slot, n, i0, i = free.get(), 0, 0, 0
		for frame in container.decode(stream):
			if frames and i >= frames:
				break
			ring[slot, n] = frame.to_ndarray(format="gray")
			n += 1
			i += 1
			if n == B:
				full.put((slot, i0, n))
				i0 += n
				n = 0
				slot = free.get()
				if slot is None:
					break
		if n:
			full.put((slot, i0, n))
		container.close()
		full.put(None)
	except BaseException as e:
		full.put(repr(e))
	finally:
		del ring
		shm.close()


class PyAVReader:
	"""PyAV in a process of its own (spawned), handing whole batches over
	through a shared-memory ring of ahead + 2 batches."""

	kind = "pyav"

	def __init__(self, info, batch, frames=0, threads=0, ahead=4):
		self.info, self.B, self.limit, self.threads, self.ahead = info, batch, frames, threads, ahead
		self.read = 0
		self.error = None

	def Describe(self):
		return "PyAV (in its own process, on the CPU)"

	def __iter__(self):
		import multiprocessing as mp
		from multiprocessing import shared_memory
		slots = self.ahead + 2
		shape = (slots, self.B, self.info.height, self.info.width)
		shm = shared_memory.SharedMemory(create=True, size=int(np.prod(shape)))
		ring = np.ndarray(shape, np.uint8, buffer=shm.buf)
		ctx = mp.get_context("spawn")
		free, full = ctx.Queue(), ctx.Queue()
		for k in range(slots):
			free.put(k)
		proc = ctx.Process(target=_PyAVWorker, args=(self.info.path, self.limit, self.B, shm.name, shape, free, full,
			self.threads), daemon=True)
		proc.start()
		try:
			last = None
			while True:
				try:
					item = full.get(timeout=120)
				except queue.Empty:
					if not proc.is_alive():
						raise RuntimeError("the PyAV decoder process died (exit {})".format(proc.exitcode))
					continue
				if last is not None:
					free.put(last)
					last = None
				if item is None:
					break
				if isinstance(item, str):
					raise RuntimeError("PyAV decoder process: " + item)
				slot, i, n = item
				last = slot
				self.read = i + n
				yield i, ring[slot, :n]
		finally:
			free.put(None)
			proc.join(timeout=10)
			if proc.is_alive():
				proc.terminate()
				proc.join(timeout=5)
			del ring
			shm.close()
			shm.unlink()


# ---------------------------------------------------------------- NVDEC

def NvdecWhyNot(info=None):
	"""None if the NVIDIA GPU's decoder can be used (for this video), else why not."""
	import importlib.util
	try:
		import torch
	except ImportError:
		return "PyTorch isn't installed"
	if not torch.cuda.is_available() or getattr(torch.version, "hip", None):
		return "no NVIDIA GPU"
	if importlib.util.find_spec("PyNvVideoCodec") is None:
		return "PyNvVideoCodec isn't installed (pip install PyNvVideoCodec)"
	if info is not None:
		if info.bitDepth != 8:
			return "{}: 8-bit video only".format(info.pixfmt)
		if info.codec not in ("h264", "hevc", "av1", "vp9", "mpeg4", "mpeg2video", "vp8", "mjpeg"):
			return "{} video".format(info.codec)
		if not re.match(r"(yuvj?420p|nv12)$", info.pixfmt or ""):
			return "{}: 4:2:0 video only".format(info.pixfmt)
	return None


class NvdecReader:
	"""
	PyNvVideoCodec's demuxer and NVDEC, in a thread: each frame's luma plane
	(NV12's first H rows) copied out of the decoder's surface into a batch on
	the GPU (the decoder reuses its surfaces), stretched to 0-255 for a
	limited-range video. Frames come in display order (the decoder's NATIVE
	latency reorders them).
	"""

	kind = "nvdec"

	def __init__(self, info, batch, frames=0, gpu=0, ahead=4):
		self.info, self.B, self.limit, self.gpu, self.ahead = info, batch, frames, gpu, ahead
		self.read = 0
		self.error = None

	def Describe(self):
		return "NVDEC (the GPU's video decoder, PyNvVideoCodec)"

	def Frames(self):
		import PyNvVideoCodec as nvc
		import torch
		torch.cuda.set_device(self.gpu)
		torch.cuda.init()
		demuxer = nvc.CreateDemuxer(filename=self.info.path)
		decoder = nvc.CreateDecoder(gpuid=self.gpu, codec=demuxer.GetNvCodecId(), cudacontext=0, cudastream=0,
			usedevicememory=True)
		H = demuxer.Height()

		def Luma(frame):
			return torch.from_dlpack(frame)[:H]
		for packet in demuxer:
			for frame in decoder.Decode(packet):
				yield Luma(frame)
		# what the decoder still holds (an empty packet: the end of the stream)
		flush = getattr(nvc, "PacketData", None)
		if flush is not None:
			for frame in decoder.Decode(flush()):
				yield Luma(frame)

	def __iter__(self):
		import torch
		q = queue.Queue(maxsize=self.ahead)
		stop = threading.Event()
		B, limit, full = self.B, self.limit, self.info.fullRange
		stream = torch.cuda.Stream(device=self.gpu)

		def Run():
			try:
				with torch.cuda.stream(stream):
					i = 0
					batch = None
					n = 0
					for y in self.Frames():
						if stop.is_set() or (limit and i + n >= limit):
							break
						if batch is None:
							batch = torch.empty((B,) + tuple(y.shape), dtype=torch.uint8, device=y.device)
						batch[n].copy_(y)
						n += 1
						if n == B:
							done = torch.cuda.Event()
							done.record(stream)
							q.put((i, batch, n, done))
							i += n
							batch, n = None, 0
					if n and not stop.is_set():
						done = torch.cuda.Event()
						done.record(stream)
						q.put((i, batch, n, done))
				q.put(None)
			except BaseException as e:
				q.put(e)
		t = threading.Thread(target=Run, daemon=True)
		t.start()
		try:
			while True:
				item = q.get()
				if item is None:
					break
				if isinstance(item, BaseException):
					raise item
				i, batch, n, done = item
				torch.cuda.current_stream().wait_event(done)
				# made on the decoder's stream, used on the caller's: not given
				# back to the allocator before the caller's work on it is done
				batch.record_stream(torch.cuda.current_stream())
				frames = batch[:n]
				if not full:
					frames = LimitedToFull(frames)
				self.read = i + n
				yield i, frames
		finally:
			stop.set()
			while t.is_alive():
				try:
					q.get(timeout=0.1)
				except queue.Empty:
					pass
			t.join(timeout=5)


# ---------------------------------------------------------------- choosing

def Open(path, batch, decoder="auto", hwaccel="auto", frames=0, info=None, ffmpeg=None):
	"""
	A reader for the video: (reader, note), note saying why a GPU decoder
	wasn't used where auto would have liked one (or None). decoder: auto,
	nvdec, ffmpeg or pyav; hwaccel (ffmpeg's): auto, none, vaapi, cuda, qsv,
	d3d11va. frames: only the first so many (0: all).
	"""
	info = info or VideoInfo(path, ffmpeg)
	note = None
	if decoder in ("auto", "nvdec"):
		why = NvdecWhyNot(info)
		if why is None:
			return NvdecReader(info, batch, frames), None
		if decoder == "nvdec":
			raise SystemExit("--decoder nvdec: {}".format(why))
		if why not in ("no NVIDIA GPU", "PyTorch isn't installed"):
			note = "NVDEC not used: {}".format(why)
		decoder = "ffmpeg"
	if decoder == "pyav":
		import importlib.util
		if importlib.util.find_spec("av") is None:
			raise SystemExit("--decoder pyav needs PyAV (pip install av)")
		return PyAVReader(info, batch, frames), note
	if decoder != "ffmpeg":
		raise SystemExit("--decoder: auto, nvdec, ffmpeg or pyav, not {}".format(decoder))
	if hwaccel == "auto":
		hwaccel = HwaccelAuto()
	return FfmpegReader(info, batch, hwaccel, frames, ffmpeg), note


def FindVideos(inputs):
	"""
	The videos to run, in order: each input a video file, or a folder: a
	session folder's camera folders' videos (Camera1/Camera1.mp4, ...,
	Camera10/..., in the cameras' number order), or the videos in it.
	"""
	def Natural(name):
		return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]

	def VideosIn(folder):
		return [os.path.join(folder, f) for f in sorted(os.listdir(folder), key=Natural)
			if f.lower().endswith(VIDEO_EXTENSIONS) and not f.startswith(".") and
			os.path.isfile(os.path.join(folder, f))]
	videos = []
	for item in inputs:
		if os.path.isdir(item):
			found = VideosIn(item)
			for sub in sorted(os.listdir(item), key=Natural):
				full = os.path.join(item, sub)
				if os.path.isdir(full) and not sub.startswith("."):
					found += VideosIn(full)
			if not found:
				raise SystemExit("{}: no videos in it or in its folders ({})".format(item, ", ".join(VIDEO_EXTENSIONS)))
			videos += found
		elif os.path.isfile(item):
			videos.append(item)
		else:
			raise SystemExit("{}: no such video or folder".format(item))
	seen, unique = set(), []
	for v in videos:
		key = os.path.abspath(v)
		if key not in seen:
			seen.add(key)
			unique.append(v)
	return unique
