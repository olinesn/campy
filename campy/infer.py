"""
campy-infer: SLEAP top-down predictions on recorded videos, written as .slp
files, several times faster than `sleap-nn track` (docs/offline-inference.md).

	campy-infer <video or session folder> ... --centroid <model folder> --instance <model folder> \\
		--max-instances 5 [--out <folder>] [--name "{stem}.predictions.slp"]

Each video gets its own .slp, as sleap-nn track writes it: next to the video
as <video name>.predictions.slp, or in --out, named by --name ({stem}: the
video's name without its extension; {folder}: the folder it is in). A trial
run on the first N frames (--frames N) is named <video name>.firstN.
predictions.slp instead, so that a full run never takes it for its own. A
folder stands for its videos and its sub-folders' videos (a campy session:
Camera1/Camera1.mp4, Camera2/...). A video whose .slp is already there is
skipped (--overwrite runs it again), so a batch job that stopped can be
started again as it was; but not when that .slp says (its provenance) it was
made by other models or from another number of frames: it is then left as
it is, the video is not run, and campy-infer ends with exit status 1.

What makes it fast (each measured in docs/offline-inference.md):

- the frames are decoded on the GPU (NVDEC, PyNvVideoCodec) where it can,
  else by ffmpeg on the CPU, a batch ahead of the models;
- the two models run as campy-live runs them (campy/live.py FixedCrops):
  the whole batch with fixed shapes, every frame giving --max-instances
  crops, on the GPU from the frames to the results, which come back in one
  copy;
- the networks are compiled: TensorRT on an NVIDIA GPU, torch.compile on
  AMD (as campy-live's --engine auto), else run in PyTorch;
- fp16 (sleap-nn 0.3.5 keeps the animals whose peak is two equal pixels,
  which fp16 makes; 0.3.3 lost them), channels-last on NVIDIA;
- a quick start on a cluster whose software and videos are on network
  storage (--read-ahead, campy/prefetch.py): the files the last run started
  with are read many at a time at the start, and each video is read whole
  ahead of its decoder (a campy recording's ~1,200 fragment headers, read
  one by one by ffmpeg, cost 15 s on a cold file server).

--exact runs sleap-nn's own top-down layer instead, fp32 with no TF32, the
frames copied to the GPU first: about 2x sleap-nn track, for reference runs
that the fast runs are compared with (bench/offline_infer_bench.py compare).

Needs sleap-nn 0.3.5 or later and PyTorch (campy-live's environment); TensorRT (pip install
tensorrt onnx) on NVIDIA or Triton on AMD for the compiled networks;
PyNvVideoCodec for NVDEC.
"""

import argparse, os, sys, time, traceback
from datetime import datetime

import numpy as np

from campy import prefetch, videoread


def Say(text):
	print("[{}] {}".format(datetime.now().strftime("%H:%M:%S"), text), flush=True)


def SamePadInPython():
	"""
	sleap-nn's MaxPool2dWithSamePadding (0.3.3 to 0.3.5) pads by
	torch.ceil(torch.tensor(i / s)).item(), which torch.compile on PyTorch 2.6
	can't trace (newer PyTorch can); math.ceil gives the same number.
	"""
	import math
	try:
		from sleap_nn.architectures import common
	except ImportError:
		return
	pool = getattr(common, "MaxPool2dWithSamePadding", None)
	if pool is None or getattr(pool, "campyPad", False):
		return

	def _calc_same_pad(self, i, k, s, d):
		return int(max((math.ceil(i / s) - 1) * s + (k - 1) * d + 1 - i, 0))
	pool._calc_same_pad = _calc_same_pad
	pool.campyPad = True


class Model:
	"""
	The trained centroid and centered-instance models on a device, set up
	for speed (or, with --exact, for reference). Run turns a batch of frames
	into sleap-nn Outputs, on the host, for Labels to make.
	"""

	def __init__(self, args, predictor=None):
		t0 = time.perf_counter()
		import torch, warnings
		from campy import live
		self.torch, self.live, self.args = torch, live, args
		live.PROGRAM = "campy-infer"
		problem = live.SleapProblem()
		if problem:
			raise SystemExit(problem.replace("campy-live", "campy-infer", 1))
		self.device = live.ResolveDevice(args.device)
		if self.device.startswith("cuda:"):
			torch.cuda.set_device(torch.device(self.device))
		gpu = self.device.startswith("cuda")
		self.nvidia = gpu and not getattr(torch.version, "hip", None)
		self.exact = args.exact
		self.B = args.batch
		self.notes = []
		if self.exact:
			# fp32 as fp32: no TF32 in the convolutions or matrix products
			torch.backends.cudnn.allow_tf32 = False
			torch.backends.cuda.matmul.allow_tf32 = False
			self.fp16, self.channelsLast, self.engine = False, False, "eager"
		else:
			self.fp16 = gpu and args.precision == "fp16"
			self.channelsLast = self.nvidia if args.memory_format == "auto" else args.memory_format == "channels_last"
			if args.engine == "auto":
				self.engine, why = live.FixedEngine.Pick(self.device)
				if why:
					self.notes.append("no compiled engine: {}".format(why))
			else:
				self.engine = live.FixedEngine.Choose(args.engine, self.device)
		if self.engine == "compile":
			SamePadInPython()
		if predictor is None: # (the tests give one of random models)
			from sleap_nn.inference.predictor import Predictor
			t1 = time.perf_counter()
			with warnings.catch_warnings():
				warnings.simplefilter("ignore", UserWarning)
				predictor = Predictor.from_model_paths([args.centroid, args.instance], device=self.device,
					batch_size=self.B, max_instances=args.max_instances, peak_threshold=args.peak_threshold)
		else:
			t1 = time.perf_counter()
		# start-up, s: PyTorch, sleap-nn and the GPU; reading the two models
		self.timing = {"imports": t1 - t0, "models": time.perf_counter() - t1}
		self.predictor = predictor
		layer = self.layer = self.predictor.layer
		self.backends = (layer.centroid_layer.backend, layer.centered_instance_layer.backend)
		for backend in self.backends:
			backend.use_fp16 = self.fp16
			if self.channelsLast:
				backend.model.to(memory_format=torch.channels_last)
		self.pipeline = self.predictor.filter_pipeline
		self.fixed = {} # (H, W): FixedCrops, for that frame size
		self.host = {} # (H, W): pinned (B, H, W, 1) upload buffer
		self.plain = self.exact # sleap-nn's own layer
		self.plainWhy = "--exact" if self.exact else None
		self.nodes = len(self.predictor.skeleton.nodes) if self.predictor.skeleton is not None else None

	def Describe(self):
		if self.plain:
			how = "sleap-nn's own top-down layer ({}), frames copied to the GPU first".format(self.plainWhy)
		else:
			engines = [f.Describe() for f in self.fixed.values()]
			how = "campy's fixed crops ({}), networks: {}".format(
				"; ".join(engines) if engines else "{} crops per batch".format(self.B * self.args.max_instances),
				{"tensorrt": "TensorRT", "compile": "torch.compile", "eager": "PyTorch"}[self.engine])
		gpu = self.torch.cuda.get_device_name(self.torch.device(self.device)) if self.device.startswith("cuda") \
			else self.device
		return "{}; {}, {}, batches of {}, at most {} animals, peak threshold {:g}; on {}".format(how,
			"fp16" if self.fp16 else "fp32",
			"channels-last" if self.channelsLast else "NCHW", self.B, self.args.max_instances,
			self.args.peak_threshold, gpu)

	def Prepare(self, height, width):
		"""
		Ready for frames of this size: the fixed crops made and run once on
		blank frames, so their engines are built (or read from the cache)
		before any video's clock starts. With --engine auto, an engine that
		fails is replaced by the networks in PyTorch; fixed crops that can't
		be made (a model they don't support) by sleap-nn's own layer.
		"""
		torch = self.torch
		key = (height, width)
		if key in self.host or (self.plain and key in self.fixed):
			return
		pin = self.device.startswith("cuda")
		self.host[key] = torch.zeros((self.B, height, width, 1), dtype=torch.uint8, pin_memory=pin)
		if self.plain:
			self.fixed[key] = None
			return
		try:
			fixed = self.live.FixedCrops(self.layer, self.B, self.device, self.args.max_instances, graph=False,
				engine=self.engine, cache=self.args.engine_cache)
		except SystemExit as e:
			self.plain, self.plainWhy = True, "{}".format(e)
			self.notes.append("fixed crops not used: {}; so sleap-nn's own layer".format(e))
			self.fixed[key] = None
			return
		self.fixed[key] = fixed
		t = time.perf_counter()
		self.Predict(fixed, fixed.Upload(self.host[key]))
		Say("models ready for {}x{} frames in {:.1f} s: {}".format(width, height, time.perf_counter() - t,
			fixed.Describe()))

	def Predict(self, fixed, x):
		try:
			return fixed.Predict(x)
		except (Exception, SystemExit) as e:
			if self.args.engine != "auto" or fixed.engine == "eager":
				raise
			if self.device.startswith("cuda"):
				self.torch.cuda.synchronize()
			why = str(e).strip().splitlines()[0][:300] if str(e).strip() else type(e).__name__
			self.notes.append("the {} engine failed ({}); so the networks run in PyTorch".format(
				{"compile": "torch.compile", "tensorrt": "TensorRT"}.get(fixed.engine, fixed.engine), why))
			Say(self.notes[-1])
			fixed.Plain()
			self.engine = "eager"
			return fixed.Predict(x)

	def Run(self, start, frames):
		"""
		Frames start .. start + b - 1 ((b, H, W) uint8: numpy on the host or
		a tensor on the GPU) to sleap-nn Outputs on the host (NaN where no
		animal), their frame indices set.
		"""
		torch = self.torch
		b, H, W = frames.shape[:3]
		self.Prepare(H, W)
		fixed = self.fixed[(H, W)]
		onGpu = not isinstance(frames, np.ndarray)
		if fixed is None:
			return self.RunPlain(start, frames, onGpu)
		if onGpu:
			x = frames.reshape(b, H, W, 1)
			if b < self.B: # the last batch: filled up with its last frame, those results dropped
				x = torch.cat([x, x[-1:].expand(self.B - b, H, W, 1)])
		else:
			host = self.host[(H, W)]
			a = host.numpy()
			a[:b, ..., 0] = frames
			if b < self.B:
				a[b:] = a[b - 1]
			x = fixed.Upload(host)
		r = self.Predict(fixed, x)
		return self.Outputs(start, r["centroids"][:b], r["centroid_vals"][:b], r["keypoints"][:b], r["peak_vals"][:b])

	def RunPlain(self, start, frames, onGpu):
		torch = self.torch
		b, H, W = frames.shape[:3]
		if onGpu:
			x = frames.reshape(b, H, W, 1)
		else:
			host = self.host[(H, W)]
			host.numpy()[:b, ..., 0] = frames
			x = host[:b].to(self.device, non_blocking=True)
		with torch.inference_mode():
			out = self.pipeline(self.layer.predict(x))
		get = lambda name: getattr(out, name, None)
		arrays = [None if get(n) is None else get(n).detach().float().cpu().numpy() for n in
			("pred_centroids", "pred_centroid_values", "pred_keypoints", "pred_peak_values")]
		if arrays[0] is None or arrays[2] is None:
			raise RuntimeError("sleap-nn's layer gave no centroids or keypoints")
		return self.Outputs(start, *arrays, filtered=True)

	def Outputs(self, start, c, cv, k, kv, filtered=False):
		# sleap-nn's Outputs for these frames, as its predictor stamps them
		import torch
		from sleap_nn.inference.outputs import Outputs
		b = c.shape[0]
		out = Outputs(pred_centroids=torch.from_numpy(np.ascontiguousarray(c)),
			pred_centroid_values=torch.from_numpy(np.ascontiguousarray(cv)),
			pred_keypoints=torch.from_numpy(np.ascontiguousarray(k)),
			pred_peak_values=torch.from_numpy(np.ascontiguousarray(kv)),
			frame_indices=torch.arange(start, start + b, dtype=torch.int64),
			video_indices=torch.zeros(b, dtype=torch.int64))
		return out if filtered else self.pipeline(out)

	def Labels(self, outputs, path, started, ended):
		"""The video's Labels, as sleap-nn track makes them: every frame read,
		with its predicted instances (none where no animal), the provenance."""
		labels = self.predictor.to_labels(outputs, videos=[SlpVideo(path)], keep_empty_frames=True)
		params = {"peak_threshold": self.args.peak_threshold, "max_instances": self.args.max_instances,
			"batch_size": self.B}
		try:
			params.update(self.predictor._preprocess_provenance_params())
		except Exception:
			pass
		try:
			prov = self.Provenance(path, started, ended, len(labels.labeled_frames), params)
		except Exception:
			prov = {}
		from campy import manifest
		prov = dict(prov or {})
		version = manifest.SetupVersion(manifest.CampyRoot()) or manifest.PackageVersion("campy")
		prov["campy_infer"] = {"campy_version": version, "how": self.Describe(), "frames_limit": int(self.args.frames or 0),
			"exact": bool(self.exact), "fp16": bool(self.fp16),
			"engine": self.engine, "notes": list(self.notes)}
		labels.provenance = prov
		return labels

	def Provenance(self, path, started, ended, frames, params):
		"""
		The provenance sleap-nn's predictor gives its Labels, the same fields.
		Its system_info is made here, once per run, from the 7 values sleap-nn
		keeps: sleap-nn's own way first reads 8 packages' details, which it
		then drops, importing each package to find its folder. On a cluster
		that was about 750 modules (wandb, PyTorch Lightning) read from
		network storage: 3.5 s, or 14 s on a node that hadn't read them yet,
		on the first video of every job.
		"""
		from sleap_nn.inference.provenance import build_inference_provenance
		torch = self.torch
		prov = build_inference_provenance(model_paths=self.predictor.model_paths, model_type=type(self.layer).__name__,
			device=self.predictor.device, start_time=started, end_time=ended, input_path=path, frames_processed=frames,
			inference_params=params, include_system_info=False)
		if not hasattr(self, "systemInfo"):
			import platform
			cuda = torch.cuda.is_available()
			mps = not cuda and hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
			info = {"python_version": sys.version.split()[0], "platform": platform.platform(),
				"pytorch_version": torch.__version__, "cuda_version": torch.version.cuda if cuda else None,
				"accelerator": "cuda" if cuda else "mps" if mps else "cpu",
				"gpu_count": torch.cuda.device_count() if cuda else 1 if mps else 0}
			if cuda and info["gpu_count"]:
				info["gpus"] = [torch.cuda.get_device_name(i) for i in range(info["gpu_count"])]
			self.systemInfo = info
		prov["system_info"] = dict(self.systemInfo)
		return prov


def SlpVideo(path):
	"""
	The video for the .slp, read with OpenCV where it is installed (a campy
	recording's fragmented MP4: sleap-io's PyAV reads it as 0 frames long,
	its imageio decodes all of it to count them), as sleap-nn track's would be.
	"""
	import sleap_io as sio
	try:
		import cv2 # noqa: F401
		return sio.load_video(path, plugin="opencv")
	except Exception:
		pass
	try:
		return sio.load_video(path)
	except Exception:
		return sio.Video(filename=path, open_backend=False)


DEFAULT_NAME = "{stem}.predictions.slp"


def OutputPath(video, args):
	stem = os.path.splitext(os.path.basename(video))[0]
	folder = os.path.basename(os.path.dirname(os.path.abspath(video)))
	pattern = args.name
	if not pattern:
		# A trial on the first N frames is named for them, never as a full run
		pattern = "{{stem}}.first{}.predictions.slp".format(args.frames) if args.frames else DEFAULT_NAME
	name = pattern.format(stem=stem, folder=folder)
	return os.path.join(args.out or os.path.dirname(os.path.abspath(video)), name)


def ReadProvenance(path):
	"""
	A .slp's provenance (a dict), read without its frames, as sleap-io
	reads it (io/slp.py read_provenance): newer sleap-io writes it as the
	HDF5 dataset /provenance_json, older sleap-io and SLEAP in /metadata's
	json attribute. Raises an exception when it cannot be read.
	"""
	import json
	import h5py

	def Text(raw):
		if hasattr(raw, "shape") and getattr(raw, "shape", None) == (1,):
			raw = raw[0]
		if hasattr(raw, "item") and not isinstance(raw, (bytes, str)):
			raw = raw.item()
		return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

	with h5py.File(path, "r") as f:
		if "provenance_json" in f:
			return json.loads(Text(f["provenance_json"][()])) or {}
		return json.loads(Text(f["metadata"].attrs["json"])).get("provenance") or {}


def ModelNames(paths):
	# The model folders' own names: a .slp made on another computer has other paths to the same models
	return sorted(os.path.basename(os.path.normpath(str(p))) for p in paths or [])


def Differs(video, out, args):
	"""
	Why the .slp already at out is not what this run would write (other
	models, another number of frames), from its provenance; None when it is
	(or that cannot be told: then said).
	"""
	try:
		prov = ReadProvenance(out)
	except Exception as e:
		Say("{}: could not read what made it ({}: {}), so it is taken to be this run's".format(out, type(e).__name__, e))
		return None
	why = []
	models = prov.get("model_paths")
	if models and ModelNames(models) != ModelNames([args.centroid, args.instance]):
		why.append("it was made by other models ({})".format(", ".join(str(m) for m in models)))
	mine = prov.get("campy_infer") or {}
	if "frames_limit" in mine: # campy-infer's own .slp: the --frames it was run with (0: all)
		if int(mine["frames_limit"] or 0) != int(args.frames or 0):
			why.append("it holds the first {} frames".format(mine["frames_limit"]) if mine["frames_limit"] else
				"it holds every frame")
	elif (prov.get("frame_selection") or {}).get("frames_processed") is not None: # sleap-nn's, an older campy-infer's
		done = int(prov["frame_selection"]["frames_processed"])
		try:
			info = videoread.VideoInfo(video)
		except Exception:
			info = None
		if info is not None and info.frames:
			want = min(info.frames, args.frames) if args.frames else info.frames
			if done != want if info.exact else done < 0.9 * want:
				why.append("it holds {:,} frames, where this run reads {}{:,}".format(done, "" if info.exact else "about ", want))
	return " and ".join(why) or None


def Hms(seconds):
	seconds = int(round(seconds))
	return "{}:{:02d}:{:02d}".format(seconds // 3600, seconds // 60 % 60, seconds % 60) if seconds >= 3600 else \
		"{}:{:02d}".format(seconds // 60, seconds % 60)


def RunVideo(model, video, out, args):
	"""One video to its .slp; returns a dict of what happened."""
	t0 = time.perf_counter()
	info = videoread.VideoInfo(video)
	reader, note = videoread.Open(video, args.batch, args.decoder, args.hwaccel, args.frames, info)
	opened = time.perf_counter() - t0
	if note:
		Say(note)
	t0 = time.perf_counter()
	model.Prepare(info.height, info.width)
	ready = time.perf_counter() - t0
	total = min(info.frames, args.frames) if args.frames else info.frames
	Say("{}: {}, {}, read with {} (opened in {:.1f} s)".format(video, info, "all frames" if not args.frames else
		"the first {} frames".format(args.frames), reader.Describe(), opened))
	started = datetime.now()
	t0 = time.perf_counter()
	outputs, found, last = [], 0, t0
	for start, frames in reader:
		out_ = model.Run(start, frames)
		outputs.append(out_)
		found += int(np.isfinite(out_.pred_centroids[..., 0].numpy()).sum())
		now = time.perf_counter()
		if now - last >= args.progress > 0:
			last = now
			done = reader.read
			rate = done / (now - t0)
			left = ", about {} left".format(Hms((total - done) / rate)) if total > done and rate else ""
			Say("  {:,} of {}{:,} frames, {:.0f} frames/s{}".format(done, "" if info.exact else "about ", total, rate,
				left))
	if reader.error:
		raise RuntimeError(reader.error)
	n = reader.read
	if n == 0:
		raise RuntimeError("no frames read")
	t1 = time.perf_counter()
	labels = model.Labels(outputs, video, started, datetime.now())
	t2 = time.perf_counter()
	os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
	part = os.path.join(os.path.dirname(out), "." + os.path.basename(out)[:-4] + ".partial.slp")
	labels.save(part)
	os.replace(part, out)
	t3 = time.perf_counter()
	for v in labels.videos:
		try:
			v.close()
		except Exception:
			pass
	result = {"video": video, "slp": out, "frames": n, "animals": found, "seconds": t3 - t0, "fps": n / (t3 - t0),
		"predict_s": t1 - t0, "labels_s": t2 - t1, "save_s": t3 - t2, "ready_s": ready, "open_s": opened,
		"decoder": reader.kind}
	short = ""
	if not args.frames and info.exact and n != info.frames:
		short = "; the video says {:,} frames".format(info.frames)
	Say("{}: {:,} frames in {:.1f} s, {:.0f} frames/s (models {:.1f} s, making the labels {:.1f} s, saving {:.1f} s), "
		"{:.2f} animals a frame{}; wrote {}".format(os.path.basename(video), n, t3 - t0, n / (t3 - t0), t1 - t0, t2 - t1,
		t3 - t2, found / n, short, out))
	return result


def Parser():
	p = argparse.ArgumentParser(prog="campy-infer", description=" ".join(__doc__.strip().split("\n\n")[0].split()),
		formatter_class=argparse.RawDescriptionHelpFormatter, epilog="See docs/offline-inference.md.")
	p.add_argument("inputs", nargs="+", help="videos, or folders: their videos and their sub-folders' videos "
		"(a campy session's Camera1/Camera1.mp4, ...)")
	p.add_argument("--centroid", required=True, help="the trained centroid model's folder")
	p.add_argument("--instance", required=True, help="the trained centered-instance model's folder")
	p.add_argument("--max-instances", type=int, required=True, help="the most animals in a frame")
	p.add_argument("--peak-threshold", type=float, default=0.2, help="both models' peak threshold (default 0.2)")
	p.add_argument("--out", default="", help="the folder for the .slp files (default: each next to its video)")
	p.add_argument("--name", default="", help="each .slp's name: {stem} the video's name without its extension, "
		"{folder} the folder it is in (default: {stem}.predictions.slp; with --frames N, {stem}.firstN.predictions.slp)")
	p.add_argument("--overwrite", action="store_true", help="run videos whose .slp is already there again (also one "
		"made by other models or from another number of frames, which is otherwise left, with exit status 1)")
	p.add_argument("--batch", type=int, default=16, help="frames per batch (default 16: on an A100 and the EVO-X2 "
		"as fast as 32 or 64, or faster, with less GPU memory)")
	p.add_argument("--exact", action="store_true", help="a reference run: sleap-nn's own layer, fp32 without TF32 "
		"(about 2x sleap-nn track, not 10x)")
	p.add_argument("--device", default="auto", help="auto (the GPU: cuda, or ROCm's), cuda:N, cpu")
	p.add_argument("--engine", default="auto", choices=["auto", "tensorrt", "compile", "off"],
		help="the networks compiled: auto (TensorRT on NVIDIA, torch.compile on AMD, else PyTorch), or off")
	p.add_argument("--engine-cache", default="", help="where TensorRT plans and torch.compile kernels are kept "
		"(default ~/.cache/campy/engines)")
	p.add_argument("--precision", default="fp16", choices=["fp16", "fp32"], help="fp16 (default) or fp32")
	p.add_argument("--strict-peaks", action="store_true", help=argparse.SUPPRESS) # gone with sleap-nn 0.3.5 (Main)
	p.add_argument("--memory-format", default="auto", choices=["auto", "nchw", "channels_last"],
		help="auto: channels-last on NVIDIA, where it is faster")
	p.add_argument("--decoder", default="auto", choices=["auto", "nvdec", "ffmpeg", "pyav"],
		help="auto: NVDEC on an NVIDIA GPU with PyNvVideoCodec, else ffmpeg")
	p.add_argument("--hwaccel", default="none", choices=["none", "vaapi", "cuda", "qsv", "d3d11va"],
		help="--decoder ffmpeg: a GPU decoder for ffmpeg to use (default none: the CPU, which was faster on the "
		"EVO-X2, whose GPU also runs the models)")
	p.add_argument("--frames", type=int, default=0, help="only each video's first N frames (to try settings)")
	p.add_argument("--progress", type=float, default=30, help="seconds between progress lines (0: none)")
	p.add_argument("--read-ahead", default="on", choices=["on", "off"], help="on (default): read the files the last "
		"run started with (Python's modules, PyTorch's, cuDNN's and TensorRT's libraries, the TensorRT plans) and the "
		"models' checkpoints with many threads at the start, so that a cluster node that hasn't read them lately "
		"doesn't wait on network storage for each one in turn. The list is kept in --engine-cache's prefetch folder")
	return p


def EngineCache(args):
	# --engine-cache's folder, as campy.live's FixedEngine.CacheDir has it (not imported yet: it brings numpy)
	return os.path.expanduser(args.engine_cache or os.path.join("~", ".cache", "campy", "engines"))


def ReadAheadNote(reading):
	n, size, seconds = reading.Done()
	return "read ahead {:,} of {:,} files, {:.1f} GB, {}".format(n, len(reading.files), size / 1e9,
		"in {:.1f} s".format(seconds) if seconds is not None else "still reading")


def Main(argv=None):
	started = time.perf_counter()
	args = Parser().parse_args(argv)
	# First of all, before the big imports: those files read many at a time
	reading = prefetch.Start(EngineCache(args), prefetch.ModelFiles([args.centroid, args.instance])) \
		if args.read_ahead == "on" else None
	if args.strict_peaks:
		raise SystemExit("--strict-peaks is gone: it was the old way sleap-nn found centroids, which lost a centroid "
			"whose top is two equal pixels (fp16). sleap-nn 0.3.5, which campy-infer needs, keeps those itself")
	if args.engine == "off":
		args.engine = "eager"
	if args.batch < 1 or args.max_instances < 1:
		raise SystemExit("--batch and --max-instances: 1 or more")
	videos = videoread.FindVideos(args.inputs)
	jobs, skipped = [], []
	for v in videos:
		out = OutputPath(v, args)
		if not out.endswith(".slp"):
			raise SystemExit("--name: a .slp file name, not {}".format(os.path.basename(out)))
		jobs.append((v, out))
	outs = [o for _, o in jobs]
	twice = sorted({o for o in outs if outs.count(o) > 1})
	if twice:
		raise SystemExit("two videos would write {}: give --name a {{folder}} (e.g. \"{{folder}}_{{stem}}.slp\")".format(
			twice[0]))
	todo, other = [], []
	for v, out in jobs:
		if os.path.exists(out) and not args.overwrite:
			why = Differs(v, out, args)
			if why:
				other.append(out)
				Say("{} is already there, but {}: NOT run, and left as it is. --overwrite replaces it; --name "
					"gives this run's .slp another name".format(out, why))
			else:
				skipped.append(out)
		else:
			todo.append((v, out))
	for out in skipped:
		Say("{} is already there: skipped (--overwrite runs it again)".format(out))
	if not todo:
		Say("nothing to do{}".format("; {} .slp made otherwise left as they are (exit status 1)".format(len(other))
			if other else ""))
		return 1 if other else 0
	Say("{} video{} to run".format(len(todo), "" if len(todo) == 1 else "s"))
	videoAhead = prefetch.Video(todo[0][0]) if args.read_ahead == "on" else None
	t0 = time.perf_counter()
	model = Model(args)
	Say("models loaded in {:.1f} s{}".format(time.perf_counter() - t0, "; " + ReadAheadNote(reading) if reading else
		""))
	for note in model.notes:
		Say(note)
	results, failed = [], []
	for k, (v, out) in enumerate(todo):
		if args.read_ahead == "on" and k + 1 < len(todo):
			videoAhead = prefetch.Video(todo[k + 1][0]) # the next one, while this one runs
		try:
			results.append(RunVideo(model, v, out, args))
			if len(results) == 1:
				r = results[0]
				Say("start-up: {:.1f} s to the first frame (PyTorch, sleap-nn and the GPU {:.1f} s, reading the models "
					"{:.1f} s, the engines {:.1f} s, opening the video {:.1f} s){}".format(time.perf_counter() - started -
					r["seconds"], model.timing["imports"], model.timing["models"], r["ready_s"], r["open_s"],
					"; " + ReadAheadNote(reading) if reading else ""))
			if len(results) == 1 and args.read_ahead == "on":
				# what this run read to get here, for the next run to read ahead
				from campy import live
				listed = prefetch.Record(EngineCache(args), list(live.FixedEngine.PLANS) + [videoread.FfmpegExe()])
				if listed is None:
					Say("could not write the list of files to read ahead in {}".format(EngineCache(args)))
		except KeyboardInterrupt:
			raise
		except Exception as e:
			traceback.print_exc()
			Say("{}: FAILED: {}: {}".format(v, type(e).__name__, e))
			failed.append(v)
	Say("how: {}".format(model.Describe()))
	if results:
		frames = sum(r["frames"] for r in results)
		seconds = time.perf_counter() - t0
		Say("done: {} video{}, {:,} frames in {} ({:.0f} frames/s with loading the models){}".format(len(results),
			"" if len(results) == 1 else "s", frames, Hms(seconds), frames / seconds, "; {} FAILED: {}".format(
			len(failed), ", ".join(failed)) if failed else ""))
	if other:
		Say("{} video{} NOT run: {} .slp, made otherwise, is there ({})".format(len(other), "" if len(other) == 1 else
			"s", "its" if len(other) == 1 else "each one's", ", ".join(other)))
	return 1 if failed or other else 0


if __name__ == "__main__":
	sys.exit(Main())
