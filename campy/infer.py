"""
campy-infer: SLEAP top-down predictions on recorded videos, written as .slp
files, several times faster than `sleap-nn track` (docs/offline-inference.md).

	campy-infer <video or session folder> ... --centroid <model folder> --instance <model folder> \\
		--max-instances 5 [--out <folder>] [--name "{stem}.predictions.slp"]

Each video gets its own .slp, as sleap-nn track writes it: next to the video
as <video name>.predictions.slp, or in --out, named by --name ({stem}: the
video's name without its extension; {folder}: the folder it is in). A folder
stands for its videos and its sub-folders' videos (a campy session:
Camera1/Camera1.mp4, Camera2/...). A video whose .slp is already there is
skipped (--overwrite runs it again), so a batch job that stopped can be
started again as it was.

What makes it fast (each measured in docs/offline-inference.md):

- the frames are decoded on the GPU (NVDEC, PyNvVideoCodec) where it can,
  else by ffmpeg (with VA-API on an AMD GPU), a batch ahead of the models;
- the two models run as campy-live runs them (campy/live.py FixedCrops):
  the whole batch with fixed shapes, every frame giving --max-instances
  crops, on the GPU from the frames to the results, which come back in one
  copy;
- the networks are compiled: TensorRT on an NVIDIA GPU, torch.compile on
  AMD (as campy-live's --engine auto), else run in PyTorch;
- fp16, with campy's tied-peaks fix (fp16 otherwise loses animals whose
  peak is two equal pixels), channels-last on NVIDIA.

--exact runs sleap-nn's own top-down layer instead, fp32 with no TF32, its
own (strict) peak finding, the frames copied to the GPU first and each crop
cut at its true size: about 2x sleap-nn track, for reference runs that the
fast runs are compared with (bench/offline_infer_bench.py compare).

Needs sleap-nn and PyTorch (campy-live's environment); TensorRT (pip install
tensorrt onnx) on NVIDIA or Triton on AMD for the compiled networks;
PyNvVideoCodec for NVDEC.
"""

import argparse, os, sys, time, traceback
from datetime import datetime

import numpy as np

from campy import videoread


def Say(text):
	print("[{}] {}".format(datetime.now().strftime("%H:%M:%S"), text), flush=True)


def SamePadInPython():
	"""
	sleap-nn 0.3.3's MaxPool2dWithSamePadding pads by
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
		import torch, warnings
		from campy import live, peaks
		self.torch, self.live, self.args = torch, live, args
		live.PROGRAM = "campy-infer"
		self.device = live.ResolveDevice(args.device)
		if self.device.startswith("cuda"):
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
			self.fp16, self.tied, self.channelsLast, self.engine = False, False, False, "eager"
		else:
			self.fp16 = gpu and args.precision == "fp16"
			self.tied = not args.strict_peaks
			self.channelsLast = self.nvidia if args.memory_format == "auto" else args.memory_format == "channels_last"
			if args.engine == "auto":
				self.engine, why = live.FixedEngine.Pick(self.device)
				if why:
					self.notes.append("no compiled engine: {}".format(why))
			else:
				self.engine = live.FixedEngine.Choose(args.engine, self.device)
		peaks.UseTiedPeaks(self.tied)
		if self.engine == "compile":
			SamePadInPython()
		if predictor is None: # (the tests give one of random models)
			from sleap_nn.inference.predictor import Predictor
			with warnings.catch_warnings():
				warnings.simplefilter("ignore", UserWarning)
				predictor = Predictor.from_model_paths([args.centroid, args.instance], device=self.device,
					batch_size=self.B, max_instances=args.max_instances, peak_threshold=args.peak_threshold)
		self.predictor = predictor
		layer = self.layer = self.predictor.layer
		self.backends = (layer.centroid_layer.backend, layer.centered_instance_layer.backend)
		for backend in self.backends:
			backend.use_fp16 = self.fp16
			if self.channelsLast:
				backend.model.to(memory_format=torch.channels_last)
		live.ExactCropSize(layer)
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
		return "{}; {}, {} peaks, {}, batches of {}, at most {} animals, peak threshold {:g}; on {}".format(how,
			"fp16" if self.fp16 else "fp32", "tied" if self.tied else "strict",
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
				tiedPeaks=self.tied, engine=self.engine, cache=self.args.engine_cache)
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
		try:
			prov = self.predictor._build_inference_provenance(source=path, start_time=started, end_time=ended,
				n_frames=len(labels.labeled_frames), inference_params={"peak_threshold": self.args.peak_threshold,
				"max_instances": self.args.max_instances, "batch_size": self.B,
				**self.predictor._preprocess_provenance_params()})
		except Exception:
			prov = {}
		from campy import manifest
		prov = dict(prov or {})
		version = manifest.SetupVersion(manifest.CampyRoot()) or manifest.PackageVersion("campy")
		prov["campy_infer"] = {"campy_version": version, "how": self.Describe(),
			"exact": bool(self.exact), "fp16": bool(self.fp16), "tied_peaks": bool(self.tied),
			"engine": self.engine, "notes": list(self.notes)}
		labels.provenance = prov
		return labels


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


def OutputPath(video, args):
	stem = os.path.splitext(os.path.basename(video))[0]
	folder = os.path.basename(os.path.dirname(os.path.abspath(video)))
	name = args.name.format(stem=stem, folder=folder)
	return os.path.join(args.out or os.path.dirname(os.path.abspath(video)), name)


def Hms(seconds):
	seconds = int(round(seconds))
	return "{}:{:02d}:{:02d}".format(seconds // 3600, seconds // 60 % 60, seconds % 60) if seconds >= 3600 else \
		"{}:{:02d}".format(seconds // 60, seconds % 60)


def RunVideo(model, video, out, args):
	"""One video to its .slp; returns a dict of what happened."""
	info = videoread.VideoInfo(video)
	reader, note = videoread.Open(video, args.batch, args.decoder, args.hwaccel, args.frames, info)
	if note:
		Say(note)
	t0 = time.perf_counter()
	model.Prepare(info.height, info.width)
	ready = time.perf_counter() - t0
	total = min(info.frames, args.frames) if args.frames else info.frames
	Say("{}: {}, {}, read with {}".format(video, info, "all frames" if not args.frames else
		"the first {} frames".format(args.frames), reader.Describe()))
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
		"predict_s": t1 - t0, "labels_s": t2 - t1, "save_s": t3 - t2, "ready_s": ready, "decoder": reader.kind}
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
	p.add_argument("--name", default="{stem}.predictions.slp", help="each .slp's name: {stem} the video's name "
		"without its extension, {folder} the folder it is in (default: {stem}.predictions.slp)")
	p.add_argument("--overwrite", action="store_true", help="run videos whose .slp is already there again")
	p.add_argument("--batch", type=int, default=64, help="frames per batch (default 64; 16-64 measured; lower it if "
		"the GPU runs out of memory)")
	p.add_argument("--exact", action="store_true", help="a reference run: sleap-nn's own layer, fp32 without TF32, "
		"strict peaks, every crop at its true size (about 2x sleap-nn track, not 10x)")
	p.add_argument("--device", default="auto", help="auto (the GPU: cuda, or ROCm's), cuda:N, cpu")
	p.add_argument("--engine", default="auto", choices=["auto", "tensorrt", "compile", "off"],
		help="the networks compiled: auto (TensorRT on NVIDIA, torch.compile on AMD, else PyTorch), or off")
	p.add_argument("--engine-cache", default="", help="where TensorRT plans and torch.compile kernels are kept "
		"(default ~/.cache/campy/engines)")
	p.add_argument("--precision", default="fp16", choices=["fp16", "fp32"], help="fp16 (default) or fp32")
	p.add_argument("--strict-peaks", action="store_true", help="sleap-nn's own peak finding (fp16 then loses "
		"animals whose peak is two equal pixels)")
	p.add_argument("--memory-format", default="auto", choices=["auto", "nchw", "channels_last"],
		help="auto: channels-last on NVIDIA, where it is faster")
	p.add_argument("--decoder", default="auto", choices=["auto", "nvdec", "ffmpeg", "pyav"],
		help="auto: NVDEC on an NVIDIA GPU with PyNvVideoCodec, else ffmpeg")
	p.add_argument("--hwaccel", default="auto", choices=["auto", "none", "vaapi", "cuda", "qsv", "d3d11va"],
		help="ffmpeg's GPU decoder: auto (VA-API on Linux with an AMD or Intel GPU, else the CPU)")
	p.add_argument("--frames", type=int, default=0, help="only each video's first N frames (to try settings)")
	p.add_argument("--progress", type=float, default=30, help="seconds between progress lines (0: none)")
	return p


def Main(argv=None):
	args = Parser().parse_args(argv)
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
	todo = []
	for v, out in jobs:
		if os.path.exists(out) and not args.overwrite:
			skipped.append(out)
		else:
			todo.append((v, out))
	for out in skipped:
		Say("{} is already there: skipped (--overwrite runs it again)".format(out))
	if not todo:
		Say("nothing to do")
		return 0
	Say("{} video{} to run".format(len(todo), "" if len(todo) == 1 else "s"))
	t0 = time.perf_counter()
	model = Model(args)
	Say("models loaded in {:.1f} s".format(time.perf_counter() - t0))
	for note in model.notes:
		Say(note)
	results, failed = [], []
	for v, out in todo:
		try:
			results.append(RunVideo(model, v, out, args))
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
	return 1 if failed else 0


if __name__ == "__main__":
	sys.exit(Main())
