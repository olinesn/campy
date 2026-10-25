#!/usr/bin/env python3
"""Offline (batch) top-down inference on a recorded video: how fast, where
the time goes, and whether campy-live's speed-ups help there too.

campy-live made sleap-nn's top-down inference several times faster for one
set of frames at a time (fixed-shape crops in one GPU graph, compiled
networks, fp16 with tied peaks, the exact crop size, ...). Offline inference
(sleap-nn track over a whole recorded video, in batches, on a cluster GPU) is
a different job: the frames have to be decoded first, batches are bigger, and
latency doesn't matter, only frames per second. This script runs the same
video through either path, with each change on or off, and writes one JSON
per run: frames per second end to end, where the time went, the GPU's
utilization, and the predictions (an .npz) to compare with a baseline run.

    python bench/offline_infer_bench.py run --video V --centroid C --instance I --out RUN [options]
    python bench/offline_infer_bench.py compare --base RUN0 RUN1 [RUN2 ...]

run
---
--path sleapnn   sleap-nn 0.3.3's own TopDownLayer.predict on each batch, as
                 sleap-nn predict runs it (Predictor.from_model_paths, then
                 layer.predict per batch, its filter pipeline, to_labels);
                 sleap-nn track runs sleap-nn's older pipeline
  --upload host  the batch as sleap-nn's VideoProvider gives it, a numpy
                 array: sleap-nn then resizes the frames and cuts the crops on
                 the CPU, and only the models run on the GPU (sleap-nn
                 predict's behaviour)
  --upload gpu   the uint8 batch copied to the GPU first (pinned memory), so
                 resizing, cropping and peak finding run there
  --exact-crop   campy's crop-size fix (talmolab/sleap-nn#778): sleap-nn cuts
                 a whole batch's crops one pixel short when the first box's
                 float corners round down
  --sleapnn-compile  sleap-nn's own TorchBackend torch.compile option
                 (reduce-overhead, dynamic=False): one compile per crop count
--path fixed     campy-live's FixedCrops (campy/live.py): the whole batch with
                 fixed shapes (batch x max_instances crops), no host wait
                 between the two models, results in one copy
  --engine eager|compile|tensorrt   the two networks as campy-live's --engine
  --cuda-graphs on|off              the whole batch as one CUDA graph
Both paths:
  --precision fp32|fp16    fp16 autocast (sleap-nn's use_fp16)
  --peaks strict|tied      tied: campy/peaks.py's peak finding, which keeps a
                           centroid whose top is two equal pixels (fp16 drops
                           them otherwise; talmolab/sleap-nn#782)
  --memory-format nchw|channels_last
  --tf32 default|on|off    TF32 in cuDNN's convolutions and in matrix products;
                           default: PyTorch's own (convolutions yes, matmul no)
  --decoder sleapio|opencv|pyav|ffmpeg|nvdec  --decode-threads N  --decode-workers N
           sleapio: sleap-nn's own VideoProvider (sleap-io, OpenCV, one
           thread, frame by frame, RGB then channel 0); opencv: one
           cv2.VideoCapture read in order; pyav: PyAV, the luma plane
           (gray) only, ffmpeg's own threads (--decode-threads); nvdec: the
           GPU's video decoder (PyNvVideoCodec), luma plane, frames stay on
           the GPU; ffmpeg: an ffmpeg process (--ffmpeg) piping gray frames,
           --hwaccel vaapi|cuda|qsv|d3d11va for a GPU's decoder (qsv: Intel
           Quick Sync's own decoder, e.g. hevc_qsv; d3d11va: --hwaccel-device
           picks the adapter). --decode-workers splits the video into that many
           contiguous parts, each decoded by its own reader (process).
  --write-slp PATH         also build the Labels and save the .slp (timed)
  --stage-timing           synchronize the GPU around every stage and time
                           each (slower; for the breakdown, not for fps)
  --decode-only            just decode, no model (the decoder's own fps)
The first --warmup batches are run and not timed (engines are built, MIOpen /
cuDNN pick kernels); fps counts the rest.

compare
-------
Each RUN's predictions against the first (--base): the same animals found
(matched by centroid within 10 px), animals only in one, and the keypoint
differences of matched animals (p50 / p99 / max, share over 1 px).
"""

import argparse
import json
import os
import platform
import queue
import subprocess
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from campy import strictjson  # noqa: E402


# ---------------------------------------------------------------- decoding

def VideoInfo(path):
	import cv2
	cap = cv2.VideoCapture(path)
	n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
	h, w = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)), int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
	cap.release()
	return n, h, w


def VideoRate(path):
	import cv2
	cap = cv2.VideoCapture(path)
	rate = cap.get(cv2.CAP_PROP_FPS)
	cap.release()
	return rate


def QsvDecoder(path):
	# ffmpeg's Quick Sync decoder for the video's codec, from its fourcc
	import cv2
	cap = cv2.VideoCapture(path)
	tag = int(cap.get(cv2.CAP_PROP_FOURCC)).to_bytes(4, "little").decode("ascii", "replace").lower()
	cap.release()
	codecs = {"hvc1": "hevc", "hev1": "hevc", "hevc": "hevc", "avc1": "h264", "h264": "h264", "av01": "av1", "vp09": "vp9"}
	if tag not in codecs:
		raise SystemExit("--hwaccel qsv: no Quick Sync decoder known for fourcc {!r}; try --hwaccel d3d11va".format(tag))
	return codecs[tag] + "_qsv"


def DecodeRange(args, start, stop):
	"""
	Frames start..stop-1 of the video, one at a time, as (H, W) uint8 numpy
	arrays (or, for nvdec, (H, W) uint8 tensors on the GPU), in order.
	"""
	if args.decoder == "sleapio":
		import sleap_io as sio
		video = sio.load_video(args.video)
		for i in range(start, stop):
			yield video[i][..., 0]
		video.close()
	elif args.decoder == "opencv":
		import cv2
		cap = cv2.VideoCapture(args.video)
		if start:
			cap.set(cv2.CAP_PROP_POS_FRAMES, start)
		for _ in range(start, stop):
			ok, img = cap.read()
			if not ok:
				break
			yield img[..., 2] # BGR: R, as sleap-io's channel 0
		cap.release()
	elif args.decoder == "pyav":
		import av
		container = av.open(args.video)
		stream = container.streams.video[0]
		stream.thread_type = "AUTO"
		stream.thread_count = args.decode_threads
		i = 0
		if start:
			# seek to the keyframe before start, then decode up to it
			tb = stream.time_base
			rate = stream.average_rate
			container.seek(int(start / rate / tb), stream=stream, backward=True, any_frame=False)
			i = None
		for frame in container.decode(stream):
			if i is None:
				i = int(round(frame.pts * stream.time_base * stream.average_rate))
			if i >= stop:
				break
			if i >= start:
				yield frame.to_ndarray(format="gray")
			i += 1
		container.close()
	elif args.decoder == "ffmpeg":
		# an ffmpeg process (--ffmpeg, its own threads; --hwaccel vaapi, cuda,
		# qsv or d3d11va: a GPU's video decoder) writing gray frames to a pipe
		_, H, W = VideoInfo(args.video)
		cmd = [args.ffmpeg, "-v", "error", "-nostdin"]
		vf = []
		if args.hwaccel == "qsv":
			# Intel Quick Sync's own decoder (hevc_qsv, ...), which hands
			# frames back in system memory. Not -hwaccel qsv: since ffmpeg 8
			# that leaves the frames in GPU memory, which -pix_fmt gray can't
			# convert (error -22), and copying them out after is 2-3x slower
			cmd += ["-c:v", QsvDecoder(args.video)]
		elif args.hwaccel == "d3d11va":
			# Windows' own decoder API on adapter --hwaccel-device (0: the
			# first, an Intel iGPU's when the display is on it); frames copied
			# out with hwdownload, nearly twice as fast as ffmpeg's own copy
			cmd += ["-hwaccel", "d3d11va", "-hwaccel_output_format", "d3d11"]
			if args.hwaccel_device:
				cmd += ["-hwaccel_device", args.hwaccel_device]
			vf = ["-vf", "hwdownload,format=nv12"]
		elif args.hwaccel != "none":
			cmd += ["-hwaccel", args.hwaccel]
			if args.hwaccel == "vaapi":
				cmd += ["-hwaccel_device", args.vaapi_device]
		if start:
			cmd += ["-ss", "{:.6f}".format(start / VideoRate(args.video))]
		cmd += ["-i", args.video, "-frames:v", str(stop - start)] + vf + ["-f", "rawvideo", "-pix_fmt", "gray", "-"]
		proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=H * W * 8)
		try:
			for _ in range(start, stop):
				buf = proc.stdout.read(H * W)
				if len(buf) < H * W:
					break
				yield np.frombuffer(buf, np.uint8).reshape(H, W)
		finally:
			proc.kill()
			proc.wait()
	elif args.decoder == "nvdec":
		# demux + decode in order (no seeking); each frame NV12 (H * 3 / 2, W)
		# on the GPU, its luma plane copied out (the decoder reuses its surfaces)
		import PyNvVideoCodec as nvc
		import torch
		torch.cuda.init()
		demuxer = nvc.CreateDemuxer(filename=args.video)
		decoder = nvc.CreateDecoder(gpuid=0, codec=demuxer.GetNvCodecId(), cudacontext=0, cudastream=0,
			usedevicememory=True)
		h = demuxer.Height() if hasattr(demuxer, "Height") else None
		i = 0
		for packet in demuxer:
			for frame in decoder.Decode(packet):
				if i >= stop:
					return
				if i >= start:
					t = torch.from_dlpack(frame)
					yield t[:(h or t.shape[0] * 2 // 3)].clone()
				i += 1
		for frame in decoder.Decode(nvc.PacketData()) if hasattr(nvc, "PacketData") else ():
			if start <= i < stop:
				t = torch.from_dlpack(frame)
				yield t[:(h or t.shape[0] * 2 // 3)].clone()
			i += 1
	else:
		raise SystemExit("--decoder: sleapio, opencv, pyav, ffmpeg or nvdec")


def _Worker(args, start, stop, B, out):
	# one contiguous part of the video into a queue of (B, H, W) batches
	buf = []
	for img in DecodeRange(args, start, stop):
		buf.append(img)
		if len(buf) == B:
			out.put(np.stack(buf))
			buf = []
	if buf:
		out.put(np.stack(buf))
	out.put(None)


def _ShmWorker(args, start, stop, B, name, shape, free, full):
	# decodes into the shared ring's free slots, a batch per slot, in order
	from multiprocessing import shared_memory
	shm = shared_memory.SharedMemory(name=name)
	ring = np.ndarray(shape, np.uint8, buffer=shm.buf)
	try:
		slot, n, i0 = free.get(), 0, start
		for img in DecodeRange(args, start, stop):
			ring[slot, n] = img
			n += 1
			if n == B:
				full.put((slot, i0, n))
				i0 += n
				n = 0
				slot = free.get()
		if n:
			full.put((slot, i0, n))
		full.put(None)
	except BaseException as e:
		full.put(repr(e))
	finally:
		del ring
		shm.close()


class Batches:
	"""
	The frames to run, as batches of B in order: (first frame index, uint8
	array (b, H, W, 1) on the host, or a tensor on the GPU for nvdec), from a
	background thread (one reader) or --decode-workers processes (each a
	contiguous part of the video), a few batches ahead.
	"""

	def __init__(self, args, start, stop, B):
		self.args, self.start, self.stop, self.B = args, start, stop, B

	def __iter__(self):
		args, B = self.args, self.B
		if args.decoder == "sleapio" and args.decode_workers <= 1:
			yield from self.SleapNN()
			return
		if args.decode_workers <= 1 and args.decoder == "pyav":
			yield from self.Process()
			return
		if args.decode_workers <= 1 or args.decoder == "nvdec":
			q = queue.Queue(maxsize=args.queue)
			threading.Thread(target=self.Thread, args=(q,), daemon=True).start()
			i = self.start
			while True:
				b = q.get()
				if b is None:
					return
				if isinstance(b, BaseException):
					raise b
				yield i, b
				i += len(b)
			return
		import multiprocessing as mp
		ctx = mp.get_context("spawn")
		W = args.decode_workers
		# parts of whole batches, so no batch spans two parts
		n = self.stop - self.start
		per = -(-n // W)
		per = -(-per // B) * B
		parts = [(self.start + k * per, min(self.stop, self.start + (k + 1) * per)) for k in range(W)]
		parts = [p for p in parts if p[0] < p[1]]
		queues = [ctx.Queue(maxsize=args.queue) for _ in parts]
		procs = [ctx.Process(target=_Worker, args=(args, a, b, B, q), daemon=True) for (a, b), q in zip(parts, queues)]
		for p in procs:
			p.start()
		for (a, _), q in zip(parts, queues):
			i = a
			while True:
				b = q.get()
				if b is None:
					break
				yield i, b[..., None]
				i += len(b)
		for p in procs:
			p.join()

	def Process(self):
		# PyAV in its own process (in one with torch and OpenCV it hung, the
		# GIL held, after the first frames), frames handed over through a
		# shared-memory ring of --queue + 2 batches, no copies through pipes
		import multiprocessing as mp
		from multiprocessing import shared_memory
		_, H, W = VideoInfo(self.args.video)
		slots = self.args.queue + 2
		shape = (slots, self.B, H, W)
		shm = shared_memory.SharedMemory(create=True, size=int(np.prod(shape)))
		ring = np.ndarray(shape, np.uint8, buffer=shm.buf)
		ctx = mp.get_context("spawn")
		free, full = ctx.Queue(), ctx.Queue()
		for k in range(slots):
			free.put(k)
		proc = ctx.Process(target=_ShmWorker, args=(self.args, self.start, self.stop, self.B, shm.name, shape, free,
			full), daemon=True)
		proc.start()
		try:
			last = None
			while True:
				item = full.get()
				if last is not None:
					free.put(last) # the consumer is done with the batch before
				if item is None:
					break
				if isinstance(item, str):
					raise RuntimeError("decoder process: " + item)
				slot, i, n = item
				last = slot
				yield i, ring[slot, :n, :, :, None]
		finally:
			proc.join(timeout=10)
			if proc.is_alive():
				proc.terminate()
			del ring
			shm.close()
			shm.unlink()

	def Thread(self, q):
		try:
			buf = []
			for img in DecodeRange(self.args, self.start, self.stop):
				buf.append(img)
				if len(buf) == self.B:
					q.put(self.Stack(buf))
					buf = []
			if buf:
				q.put(self.Stack(buf))
			q.put(None)
		except BaseException as e:
			q.put(e)

	def Stack(self, buf):
		if self.args.decoder == "nvdec":
			import torch
			return torch.stack(buf)[..., None]
		return np.stack(buf)[..., None]

	def SleapNN(self):
		# sleap-nn's own VideoProvider (prefetch thread, queue of 4), as track
		from sleap_nn.inference.providers import VideoProvider
		provider = VideoProvider(video=self.args.video, batch_size=self.B, frames=list(range(self.start, self.stop)))
		for batch in provider:
			yield int(batch.frame_indices[0]), batch.images


# ---------------------------------------------------------------- GPU utilization

class GpuSampler:
	"""nvidia-smi every 200 ms in the background: GPU utilization and memory."""

	def __init__(self):
		self.samples, self.proc = [], None

	def Start(self):
		busy = "/sys/class/drm/card1/device/gpu_busy_percent"
		if os.path.exists(busy) and not os.path.exists("/proc/driver/nvidia"):
			# AMD (amdgpu): busy percent from sysfs, every 200 ms
			def poll():
				while self.proc == "amd":
					try:
						with open(busy) as f:
							self.samples.append([float(f.read()), 0.0, 0.0])
					except (OSError, ValueError):
						pass
					time.sleep(0.2)
			self.proc = "amd"
			threading.Thread(target=poll, daemon=True).start()
			return
		try:
			dev = os.environ.get("CUDA_VISIBLE_DEVICES", "0").split(",")[0] or "0"
			self.proc = subprocess.Popen(["nvidia-smi", "-i", dev, "--query-gpu=utilization.gpu,memory.used,clocks.sm",
				"--format=csv,noheader,nounits", "-lms", "200"], stdout=subprocess.PIPE, text=True)
			threading.Thread(target=self.Read, daemon=True).start()
		except Exception:
			self.proc = None

	def Read(self):
		for line in self.proc.stdout:
			try:
				self.samples.append([float(v) for v in line.split(",")])
			except ValueError:
				pass

	def Stop(self):
		if self.proc == "amd":
			self.proc = None
		elif self.proc is not None:
			self.proc.terminate()
		if not self.samples:
			return None
		a = np.array(self.samples)
		return {"util_mean": float(a[:, 0].mean()), "util_p50": float(np.median(a[:, 0])),
			"mem_max_mb": float(a[:, 1].max()), "sm_clock_p50": float(np.median(a[:, 2])), "samples": len(a)}


# ---------------------------------------------------------------- stage timing

class Stages:
	"""Wall time per named stage, the GPU synchronized before and after each."""

	def __init__(self, on):
		self.on, self.t = on, {}

	def Wrap(self, name, fn):
		if not self.on:
			return fn
		import torch

		def wrapped(*a, **k):
			torch.cuda.synchronize()
			t = time.perf_counter()
			out = fn(*a, **k)
			torch.cuda.synchronize()
			self.t[name] = self.t.get(name, 0.0) + time.perf_counter() - t
			return out
		return wrapped

	def Add(self, name, seconds):
		self.t[name] = self.t.get(name, 0.0) + seconds


class _Proxy:
	# a backend with its __call__ timed (attrs classes: __call__ is the type's)
	def __init__(self, inner, call):
		object.__setattr__(self, "_inner", inner)
		object.__setattr__(self, "_call", call)

	def __call__(self, *a, **k):
		return self._call(*a, **k)

	def __getattr__(self, name):
		return getattr(self._inner, name)

	def __setattr__(self, name, value):
		setattr(self._inner, name, value)


def TimeSleapNN(layer, stages):
	# sleap-nn's TopDownLayer stages, timed (--stage-timing)
	from sleap_nn.inference.layers import topdown
	c, ci = layer.centroid_layer, layer.centered_instance_layer
	c.preprocess = stages.Wrap("cen_pre", c.preprocess)
	c.postprocess = stages.Wrap("cen_peaks", c.postprocess)
	ci.preprocess = stages.Wrap("ins_pre", ci.preprocess)
	ci.postprocess = stages.Wrap("ins_peaks", ci.postprocess)
	for part, name in ((c, "cen_fwd"), (ci, "ins_fwd")):
		backend = part.backend
		part.backend = _Proxy(backend, stages.Wrap(name, backend.__call__))
	original = topdown.crop_bboxes
	topdown.crop_bboxes = stages.Wrap("crop", original)


# ---------------------------------------------------------------- the run

def SamePadInPython():
	# sleap-nn 0.3.3's MaxPool2dWithSamePadding pads with
	# torch.ceil(torch.tensor(i / s)).item(): torch.compile (dynamo) in
	# torch 2.6 can't trace .item() (newer torch folds it); math.ceil gives
	# the same integer
	import math
	from sleap_nn.architectures import common

	def _calc_same_pad(self, i, k, s, d):
		return int(max((math.ceil(i / s) - 1) * s + (k - 1) * d + 1 - i, 0))
	common.MaxPool2dWithSamePadding._calc_same_pad = _calc_same_pad


def Build(args):
	import torch
	import warnings
	if args.engine == "compile" or args.sleapnn_compile:
		SamePadInPython()
	from sleap_nn.inference.predictor import Predictor
	from campy import peaks
	peaks.UseTiedPeaks(args.peaks == "tied")
	with warnings.catch_warnings():
		warnings.simplefilter("ignore", UserWarning)
		predictor = Predictor.from_model_paths([args.centroid, args.instance], device="cuda", batch_size=args.batch,
			max_instances=args.max_instances, peak_threshold=args.peak_threshold)
	layer = predictor.layer
	for backend in (layer.centroid_layer.backend, layer.centered_instance_layer.backend):
		backend.use_fp16 = args.precision == "fp16"
		if args.memory_format == "channels_last":
			backend.model.to(memory_format=torch.channels_last)
		if args.sleapnn_compile:
			backend._compiled = torch.compile(backend.model, mode="reduce-overhead", dynamic=False)
	if args.exact_crop:
		from campy.live import ExactCropSize
		ExactCropSize(layer)
	return predictor, layer


_progress = [time.monotonic()]


def Watchdog(seconds):
	# a run that makes no progress for this long prints every thread's stack
	# and stops: faulthandler's own C thread, so it fires even when a C call
	# holds the GIL; re-armed at each batch (Progress)
	import faulthandler
	_progress.append(seconds)
	faulthandler.dump_traceback_later(seconds, exit=True)


def Progress():
	# a batch done: the watchdog's time starts again
	import faulthandler
	_progress[0] = time.monotonic()
	if len(_progress) > 1:
		faulthandler.dump_traceback_later(_progress[1], exit=True)


def Run(args):
	import torch
	torch.backends.cudnn.benchmark = args.cudnn_benchmark
	if args.tf32 != "default":
		torch.backends.cudnn.allow_tf32 = args.tf32 == "on"
		torch.backends.cuda.matmul.allow_tf32 = args.tf32 == "on"
	n, H, W = VideoInfo(args.video)
	start = args.start
	stop = n if not args.frames else min(n, start + args.frames)
	B = args.batch
	report = {"args": vars(args), "video_frames": n, "frame_hw": [H, W], "gpu": None, "host": platform.node(),
		"torch": torch.__version__, "cpus": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count(),
		"tf32_cudnn_matmul": [torch.backends.cudnn.allow_tf32, torch.backends.cuda.matmul.allow_tf32]}
	if args.decode_only:
		return DecodeOnly(args, start, stop, B, report)
	report["gpu"] = torch.cuda.get_device_name(0)
	predictor, layer = Build(args)
	stages = Stages(args.stage_timing)
	fixed = None
	if args.path == "fixed":
		from campy.live import FixedCrops
		fixed = FixedCrops(layer, B, "cuda", args.max_instances, graph=args.cuda_graphs == "on",
			tiedPeaks=args.peaks == "tied", engine=args.engine, cache=args.engine_cache)
		host = torch.empty((B, H, W, 1), dtype=torch.uint8, pin_memory=True)
	elif args.stage_timing:
		TimeSleapNN(layer, stages)
	pipeline = predictor.filter_pipeline
	K = args.max_instances
	nodes = len(predictor.skeleton.nodes)
	total = stop - start
	cen = np.full((total, K, 2), np.nan, np.float32)
	cval = np.full((total, K), np.nan, np.float32)
	kp = np.full((total, K, nodes, 2), np.nan, np.float32)
	kval = np.full((total, K, nodes), np.nan, np.float32)
	outputs_list = [] if args.write_slp else None
	pinned = None

	def Predict(frames):
		# one batch -> (centroids, centroid vals, keypoints, peak vals) numpy, b frames
		nonlocal pinned
		b = frames.shape[0]
		if fixed is not None:
			t = time.perf_counter()
			if isinstance(frames, np.ndarray):
				if b < B: # the last batch: padded with its last frame, those results dropped
					frames = np.concatenate([frames, np.repeat(frames[-1:], B - b, axis=0)])
				host.numpy()[:] = frames
				x = fixed.Upload(host)
			else:
				if b < B:
					frames = torch.cat([frames, frames[-1:].expand(B - b, *frames.shape[1:])])
				x = frames
			stages.Add("upload", time.perf_counter() - t)
			t = time.perf_counter()
			r = fixed.Predict(x)
			stages.Add("predict", time.perf_counter() - t)
			return r["centroids"][:b], r["centroid_vals"][:b], r["keypoints"][:b], r["peak_vals"][:b], None
		t = time.perf_counter()
		if args.upload == "gpu":
			if isinstance(frames, np.ndarray):
				if pinned is None or pinned.shape[0] < b:
					pinned = torch.empty((B,) + frames.shape[1:], dtype=torch.uint8, pin_memory=True)
				pinned.numpy()[:b] = frames
				frames = pinned[:b].to("cuda", non_blocking=True)
		elif not isinstance(frames, np.ndarray):
			frames = frames.cpu().numpy()
		stages.Add("upload", time.perf_counter() - t)
		t = time.perf_counter()
		out = pipeline(layer.predict(frames))
		stages.Add("predict", time.perf_counter() - t)
		t = time.perf_counter()
		res = []
		for field in ("pred_centroids", "pred_centroid_values", "pred_keypoints", "pred_peak_values"):
			v = getattr(out, field, None)
			res.append(None if v is None else v.detach().float().cpu().numpy())
		stages.Add("download", time.perf_counter() - t)
		return res + [out]

	def Store(i, res, b):
		c, cv, k, kv, out = res
		j = i - start
		if c is not None and c.size:
			m = min(K, c.shape[1])
			cen[j:j + b, :m], cval[j:j + b, :m] = c[:, :m], cv[:, :m]
			if k is not None and k.size:
				kp[j:j + b, :m], kval[j:j + b, :m] = k[:, :m], kv[:, :m]
		if outputs_list is not None:
			if out is None:
				out = FixedOutputs(c, cv, k, kv)
			outputs_list.append(predictor._stamp_metadata(out, _Meta(i, b)))

	batches = iter(Batches(args, start, stop, B))
	# warm-up: the first batches, untimed (and their results kept)
	sync = torch.cuda.synchronize
	t0 = time.perf_counter()
	done = 0
	for _ in range(args.warmup):
		try:
			i, frames = next(batches)
		except StopIteration:
			break
		b = frames.shape[0]
		Progress()
		Store(i, Predict(frames), b)
		done += b
	sync()
	warm_s = time.perf_counter() - t0
	stages.t.clear()
	gpu = GpuSampler()
	gpu.Start()
	cpu0 = os.times()
	t0 = time.perf_counter()
	timed = 0
	wait = 0.0
	while True:
		t = time.perf_counter()
		try:
			i, frames = next(batches)
		except StopIteration:
			break
		wait += time.perf_counter() - t
		b = frames.shape[0]
		Progress()
		t = time.perf_counter()
		res = Predict(frames)
		t2 = time.perf_counter()
		Store(i, res, b)
		stages.Add("store", time.perf_counter() - t2)
		timed += b
	sync()
	loop_s = time.perf_counter() - t0
	cpu1 = os.times()
	report["gpu_sampled"] = gpu.Stop()
	report.update({"frames": total, "timed_frames": timed, "warmup_s": warm_s, "loop_s": loop_s,
		"fps": timed / loop_s if loop_s else None, "decode_wait_s": wait,
		"cpu_cores_used": ((cpu1.user + cpu1.system) - (cpu0.user + cpu0.system)) / loop_s,
		"stages_s": stages.t, "nodes": nodes, "path_text": fixed.Describe() if fixed is not None else "sleap-nn TopDownLayer"})
	import faulthandler
	faulthandler.cancel_dump_traceback_later()
	if args.write_slp:
		t = time.perf_counter()
		labels = predictor.to_labels(outputs_list, videos=[_SioVideo(args.video)], keep_empty_frames=True)
		t2 = time.perf_counter()
		labels.save(args.write_slp)
		t3 = time.perf_counter()
		report.update({"to_labels_s": t2 - t, "save_s": t3 - t2,
			"instances_in_slp": sum(len(lf.instances) for lf in labels.labeled_frames)})
	report["end_to_end_fps"] = total / (warm_s + loop_s + report.get("to_labels_s", 0) + report.get("save_s", 0))
	np.savez_compressed(args.out + ".npz", centroids=cen, centroid_vals=cval, keypoints=kp, peak_vals=kval,
		start=start)
	report["found"] = int(np.isfinite(cen[..., 0]).sum())
	return report


def _SioVideo(path):
	import sleap_io as sio
	return sio.load_video(path)


class _Meta:
	def __init__(self, i, b):
		self.frame_indices = np.arange(i, i + b, dtype=np.int64)
		self.video_indices = np.zeros(b, dtype=np.int64)


def FixedOutputs(c, cv, k, kv):
	# FixedCrops' arrays as sleap-nn's Outputs (NaN where no animal), for to_labels
	import torch
	from sleap_nn.inference.outputs import Outputs
	return Outputs(pred_centroids=torch.from_numpy(c), pred_centroid_values=torch.from_numpy(cv),
		pred_keypoints=torch.from_numpy(k), pred_peak_values=torch.from_numpy(kv))


def DecodeOnly(args, start, stop, B, report):
	cpu0 = os.times()
	t0 = time.perf_counter()
	count = 0
	first = None
	for i, frames in Batches(args, start, stop, B):
		Progress()
		if first is None:
			first = np.asarray(frames[0].cpu() if hasattr(frames, "cpu") else frames[0])[..., 0].copy()
		count += frames.shape[0]
	if args.decoder == "nvdec":
		import torch
		torch.cuda.synchronize()
	s = time.perf_counter() - t0
	cpu1 = os.times()
	np.save(args.out + ".first.npy", first)
	report.update({"frames": count, "loop_s": s, "fps": count / s,
		"cpu_cores_used": ((cpu1.user + cpu1.system) - (cpu0.user + cpu0.system)) / s})
	return report


# ---------------------------------------------------------------- compare

def Predictions(path):
	"""
	(keypoints (frames, animals, nodes, 2), centroids (frames, animals, 2)
	or None, first frame) from a run's .npz or from a .slp (sleap-nn
	track's output, which has no centroids), NaN where none.
	"""
	if path.endswith(".slp"):
		import sleap_io as sio
		labels = sio.load_slp(path)
		frames = [lf for lf in labels.labeled_frames]
		n = max(lf.frame_idx for lf in frames) + 1
		K = max(len(lf.instances) for lf in frames)
		nodes = len(labels.skeleton.nodes)
		kp = np.full((n, max(K, 1), nodes, 2), np.nan, np.float32)
		for lf in frames:
			for k, inst in enumerate(lf.instances):
				kp[lf.frame_idx, k] = inst.numpy()[:, :2]
		return kp, None, 0
	d = np.load(path if path.endswith(".npz") else path + ".npz")
	return d["keypoints"], d["centroids"], int(d["start"])


def Compare(base, other, radius=10.0):
	"""
	Animals matched frame by frame by their centroids (from a .slp: the
	median of their keypoints), the nearest within radius px, greedy; then
	their keypoints compared.
	"""
	ka, ca, sa = Predictions(base)
	kb, cb, sb = Predictions(other)
	s = max(sa, sb)
	n = min(sa + len(ka), sb + len(kb)) - s
	ka, kb = ka[s - sa:s - sa + n], kb[s - sb:s - sb + n]
	import warnings
	if ca is None or cb is None:
		with warnings.catch_warnings():
			warnings.simplefilter("ignore", RuntimeWarning)
			ca, cb = np.nanmedian(ka, axis=2), np.nanmedian(kb, axis=2)
	else:
		ca, cb = ca[s - sa:s - sa + n], cb[s - sb:s - sb + n]
	matched, only_a, only_b, diffs = 0, 0, 0, []
	frames_differ = 0
	for f in range(n):
		ia = np.flatnonzero(np.isfinite(ca[f, :, 0]))
		ib = np.flatnonzero(np.isfinite(cb[f, :, 0]))
		used = set()
		m = 0
		for x in ia:
			if len(ib) == 0:
				break
			d = np.hypot(*(cb[f, ib] - ca[f, x]).T)
			for o in np.argsort(d):
				if d[o] > radius:
					break
				if ib[o] not in used:
					used.add(ib[o])
					m += 1
					diffs.append(np.hypot(*(kb[f, ib[o]] - ka[f, x]).T))
					break
		matched += m
		only_a += len(ia) - m
		only_b += len(ib) - m
		frames_differ += (len(ia) - m) > 0 or (len(ib) - m) > 0
	d = np.concatenate(diffs) if diffs else np.zeros(0)
	both = np.isfinite(d)
	d = d[both]
	return {"frames": n, "matched": matched, "only_base": only_a, "only_other": only_b, "frames_differ": frames_differ,
		"kp_compared": int(d.size), "kp_nan_mismatch": int((~both).sum()),
		"kp_p50": float(np.median(d)) if d.size else None, "kp_p99": float(np.percentile(d, 99)) if d.size else None,
		"kp_max": float(d.max()) if d.size else None, "kp_over_1px": float((d > 1).mean()) if d.size else None,
		"kp_exact": float((d == 0).mean()) if d.size else None}


# ---------------------------------------------------------------- main

def Main(argv=None):
	p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
	sub = p.add_subparsers(dest="cmd", required=True)
	r = sub.add_parser("run")
	r.add_argument("--video", required=True)
	r.add_argument("--centroid", required=True)
	r.add_argument("--instance", required=True)
	r.add_argument("--out", required=True, help="output stem: <out>.json, <out>.npz")
	r.add_argument("--path", choices=("sleapnn", "fixed"), default="sleapnn")
	r.add_argument("--batch", type=int, default=16)
	r.add_argument("--start", type=int, default=0)
	r.add_argument("--frames", type=int, default=0, help="0: to the end")
	r.add_argument("--warmup", type=int, default=5, help="batches run before timing")
	r.add_argument("--max-instances", type=int, default=5)
	r.add_argument("--peak-threshold", type=float, default=0.2)
	r.add_argument("--precision", choices=("fp32", "fp16"), default="fp32")
	r.add_argument("--peaks", choices=("strict", "tied"), default="strict")
	r.add_argument("--memory-format", choices=("nchw", "channels_last"), default="nchw")
	r.add_argument("--upload", choices=("host", "gpu"), default="host")
	r.add_argument("--exact-crop", action="store_true")
	r.add_argument("--sleapnn-compile", action="store_true")
	r.add_argument("--engine", choices=("eager", "compile", "tensorrt"), default="eager")
	r.add_argument("--engine-cache", default="")
	r.add_argument("--cuda-graphs", choices=("on", "off"), default="off")
	r.add_argument("--cudnn-benchmark", action="store_true")
	r.add_argument("--tf32", choices=("default", "on", "off"), default="default")
	r.add_argument("--decoder", choices=("sleapio", "opencv", "pyav", "ffmpeg", "nvdec"), default="sleapio")
	r.add_argument("--ffmpeg", default="ffmpeg", help="--decoder ffmpeg: the ffmpeg to run")
	r.add_argument("--hwaccel", choices=("none", "vaapi", "cuda", "qsv", "d3d11va"), default="none")
	r.add_argument("--vaapi-device", default="/dev/dri/renderD128")
	r.add_argument("--hwaccel-device", default="", help="--hwaccel d3d11va: the adapter (0, 1, ...; default ffmpeg's)")
	r.add_argument("--decode-threads", type=int, default=0, help="pyav: ffmpeg's threads (0: auto)")
	r.add_argument("--decode-workers", type=int, default=1)
	r.add_argument("--queue", type=int, default=4)
	r.add_argument("--write-slp", default="")
	r.add_argument("--stage-timing", action="store_true")
	r.add_argument("--decode-only", action="store_true")
	r.add_argument("--stall", type=float, default=300, help="seconds without progress before giving up")
	c = sub.add_parser("compare")
	c.add_argument("--base", required=True)
	c.add_argument("runs", nargs="+")
	args = p.parse_args(argv)
	if args.cmd == "compare":
		for run in args.runs:
			print(strictjson.dumps({"run": os.path.basename(run), **Compare(args.base, run)}))
		return
	os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
	Watchdog(args.stall)
	report = Run(args)
	with open(args.out + ".json", "w") as f:
		strictjson.dump(report, f, indent=1, default=str)
	short = {k: report.get(k) for k in ("fps", "end_to_end_fps", "decode_wait_s", "loop_s", "cpu_cores_used",
		"gpu_sampled", "stages_s", "found", "to_labels_s", "save_s", "path_text")}
	print("RESULT", os.path.basename(args.out), strictjson.dumps(short, default=str), flush=True)


if __name__ == "__main__":
	Main()
