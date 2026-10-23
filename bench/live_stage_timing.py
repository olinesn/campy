#!/usr/bin/env python3
"""Where campy-live's time per set goes, stage by stage.

campy-live times a set as one number (inference_ns: upload, both models,
results back). This splits it, to decide which ways of getting frames into
the model and results out are worth building. Nothing in campy changes: the
script runs campy-live itself, with its stages wrapped by timers.

    python bench/live_stage_timing.py run --stages <out> [--timing stages|coarse|sync|off] -- <campy-live arguments>
    python bench/live_stage_timing.py kernels --centroid <dir> --instance <dir> --video a.mp4 --video b.mp4 ... --out <json>

run
---
campy-live as usual (same arguments; feed it from campy-acquire or a replay),
with each stage of each set timed: a timer synchronizes the GPU before and
after its stage (its stream's synchronize, not the whole device's: that
would wait for --hold-gpu-clock's spinners too), so a stage's time is its own launch
and GPU work, and the GPU work of the stages before it is not counted. A CUDA
event pair inside each stage also gives the GPU's own time for it. Those
synchronizations take the overlap of host and GPU away, so the stages add up
to more than campy-live's untimed inference time; --timing off runs the same
script without them, for that comparison. The stages, in order:

  read          the set copied out of the shared-memory rings into the pinned
                host buffer (livefeed RingReader.Read: np.copyto), per camera
  upload        TopDown.Upload: host.to(device, non_blocking=True)
  cen_pre       the centroid layer's preprocessing: grayscale, size matching,
                resize to the model's scale, padding (sleap-nn's
                _apply_full_preprocess, on uint8 frames)
  cen_norm      uint8 to float / 255 (normalize_on_gpu, in the model's forward;
                inside the graph with --cuda-graphs, so not timed apart then)
  cen_copyin    --cuda-graphs: the graph's static input copy (static_in.copy_)
  cen_fwd       the centroid model's forward pass (with --cuda-graphs, the replay)
  cen_peaks     local peak finding and the per-frame top-k (postprocess); it
                ends with results the host needs, so the host waits here
  sizematch     TopDownLayer's second size match of the full frames, for cropping
  crop          cropping each found animal out of the full frames
  ins_pre ins_norm ins_copyin ins_fwd ins_peaks
                as above, for the centered-instance model on the crops
  between       the rest of TopDownLayer.predict: NaN masks, nonzero (the
                crop count, a host wait), the scatter of keypoints back into
                (frames, animals, nodes) arrays
  dl_centroids dl_centroid_vals dl_keypoints dl_peak_vals
                TopDown.Predict's four .float().cpu().numpy() downloads
  predict_total upload to downloads, as campy-live's inference_ns
  pose3d        the 3D step (Pose3DThread, from campy-live's own log)

That is --timing sync, for sleap-nn's own layer (--fixed-crops off). For
campy-live's default path (--fixed-crops, one CUDA graph for the whole set,
the networks compiled by --engine auto), --timing stages (the default) cuts
that graph at its steps: each is recorded as a graph of its own (Staged), so
it can be waited for alone, timed on the model's stream as above:

  read          as above
  upload        the host buffer into the graph's input (static_in.copy_)
  cen_pre       resize to the centroid model's scale, padding (sleap-nn's
                preprocessing, on uint8 frames: torchvision converts them to
                float32 to shrink them and back)
  cen_net       the centroid network: the engine, with the / 255 (inside the
                compiled region with torch.compile; in torch just before the
                plan with TensorRT)
  cen_peaks     peak finding, the top max_instances, integral refinement
  crop          the crops around them, max_instances per frame
  ins_pre ins_net
                as above, for the centered-instance network on the crops
  ins_peaks     its peaks, refined, back into the frame, packed into one tensor
  download      that tensor into pinned memory, one wait, split into arrays
  predict_total upload to download, as campy-live's inference_ns

Every --check sets campy-live's own graph runs on the same frames as well,
and the two outputs must be the same (staged_vs_campy_live in the summary).
--timing coarse times campy-live's own graph as one stage (replay), between
the upload and the download; --timing off adds no waits at all.

Writes <out>.stages.npz (one row per set, ns; plus crops, the set's start
and its frame number) and <out>.stages.json (p50 / p99 / max per stage, in
ms; and the first second apart from the rest).

kernels
-------
The two models' forward passes on real frames (decoded from --video, one per
camera), in sleap-nn's layers as campy-live builds them (fp16 autocast), under
torch.profiler: the GPU time of each layer (convolution, pooling, upsampling,
...) against the bytes it must read and write and the arithmetic it does.
Achieved GB/s and TFLOP/s per layer, beside this GPU's measured copy bandwidth
and fp16 matrix-multiply throughput, say which layers are limited by memory
bandwidth and which by arithmetic. Also the top kernels of each model.

Needs sleap-nn and a CUDA or ROCm GPU. On the EVO-X2 run it under the rig lock.
"""
import argparse, json, os, sys, time, atexit

import numpy as np

from campy import live

ORDER = ["read", "upload", "cen_pre", "cen_norm", "cen_copyin", "cen_fwd", "cen_net", "cen_peaks", "sizematch", "crop",
	"ins_pre", "ins_norm", "ins_copyin", "ins_fwd", "ins_net", "ins_peaks", "between", "replay",
	"dl_centroids", "dl_centroid_vals", "dl_keypoints", "dl_peak_vals", "download", "predict_total"]


def Stats(values):
	v = np.asarray(values, float) / 1e6
	if not len(v):
		return None
	return {"p50": round(float(np.percentile(v, 50)), 3), "p99": round(float(np.percentile(v, 99)), 3),
		"max": round(float(v.max()), 3), "mean": round(float(v.mean()), 3)}


# ---------------------------------------------------------------- run

class Stages:
	"""Per-set stage times: wall (host, synchronized) and GPU (CUDA events)."""

	def __init__(self, torch, sync):
		self.torch, self.sync = torch, sync
		self.rows, self.gpu_rows, self.crops = [], [], []
		self.wall, self.gpu = {}, {}
		self.pending = [] # (name, start event, end event) until the set's end
		self.started = time.perf_counter_ns()
		self.start, self.frame = 0, -1 # this set's: upload start (ns since started), frame number
		self.starts, self.frames = [], []
		self.checked, self.checks = False, [] # Staged's check ran in this set (its times are left out)

	def Wait(self):
		# The model's stream only: torch.cuda.synchronize() would also wait
		# for --hold-gpu-clock's spinners, on streams of their own
		self.torch.cuda.current_stream().synchronize()

	def Time(self, name, fn, *a, **k):
		if not self.sync:
			return fn(*a, **k)
		torch = self.torch
		self.Wait()
		start, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
		t = time.perf_counter_ns()
		start.record()
		out = fn(*a, **k)
		end.record()
		self.Wait()
		self.wall[name] = self.wall.get(name, 0) + time.perf_counter_ns() - t
		self.pending.append((name, start, end))
		return out

	def Host(self, name, ns):
		self.wall[name] = self.wall.get(name, 0) + ns

	def EndSet(self, total_ns, crops):
		gpu = {}
		for name, s, e in self.pending:
			gpu[name] = gpu.get(name, 0) + int(s.elapsed_time(e) * 1e6)
		self.wall["predict_total"] = total_ns
		self.rows.append(self.wall)
		self.gpu_rows.append(gpu)
		self.crops.append(crops)
		self.starts.append(self.start)
		self.frames.append(self.frame)
		self.checks.append(self.checked)
		self.checked = False
		self.wall, self.gpu, self.pending = {}, {}, []


def Instrument(model, st, torch):
	"""Wraps campy-live's TopDown model's stages with st's timers."""
	from sleap_nn.inference.layers import topdown as td
	from sleap_nn.training import lightning_modules as lm
	layer = model.layer
	current = {"layer": None} # which model is running: cen or ins
	graphed = bool(getattr(model, "graphs", None))

	# Preprocessing and peak finding, per inner layer (instance attributes)
	for tag, inner in (("cen", layer.centroid_layer), ("ins", layer.centered_instance_layer)):
		def Pre(image, _orig=inner.preprocess, _tag=tag):
			current["layer"] = _tag
			return st.Time(_tag + "_pre", _orig, image)
		def Post(raw, info, _orig=inner.postprocess, _tag=tag):
			return st.Time(_tag + "_peaks", _orig, raw, info)
		inner.preprocess, inner.postprocess = Pre, Post
		backend = inner.backend
		model_ = backend.model
		if isinstance(model_, torch.nn.Module) and hasattr(model_, "graphs") and hasattr(model_, "Capture"):
			# campy's CUDAGraphed: its static input copy and its replay apart
			def Forward(x, _g=model_, _tag=tag):
				key = (tuple(x.shape), x.dtype, x.stride())
				if key not in _g.graphs:
					_g.graphs[key] = _g.Capture(x)
				graph, static_in, static_out = _g.graphs[key]
				st.Time(_tag + "_copyin", static_in.copy_, x)
				st.Time(_tag + "_fwd", graph.replay)
				return static_out
			model_.forward = Forward
		else:
			# Eager: the forward, with normalize_on_gpu timed apart inside it
			inner_net = model_.model
			def Net(x, _orig=inner_net.forward, _tag=tag):
				return st.Time(_tag + "_fwd", _orig, x)
			inner_net.forward = Net
	if not graphed: # inside the graph otherwise (and a graph recording could not synchronize)
		norm = lm.normalize_on_gpu
		lm.normalize_on_gpu = lambda img: st.Time((current["layer"] or "cen") + "_norm", norm, img)

	# TopDownLayer's own steps between the models
	sm = layer._sizematch_like_centroid_layer
	layer._sizematch_like_centroid_layer = lambda x: st.Time("sizematch", sm, x)
	crop = td.crop_bboxes
	def Crop(images, bboxes, inds, **kwargs): # crop_size=, from talmolab/sleap-nn#780 on
		out = st.Time("crop", crop, images, bboxes, inds, **kwargs)
		st.last_crops = int(bboxes.shape[0])
		return out
	td.crop_bboxes = Crop

	# Upload, the layer, and the four downloads (TopDown.Predict, written out
	# to time each)
	upload = model.Upload
	model.Upload = lambda host: st.Time("upload", upload, host)
	def Predict(frames):
		st.last_crops = 0
		if st.sync:
			st.Wait()
		t = time.perf_counter_ns()
		out = layer.predict(frames)
		if st.sync:
			st.Wait()
		layer_ns = time.perf_counter_ns() - t
		result = {}
		for key, field in (("centroids", "pred_centroids"), ("centroid_vals", "pred_centroid_values"),
				("keypoints", "pred_keypoints"), ("peak_vals", "pred_peak_values")):
			value = getattr(out, field, None)
			result[key] = None if value is None else st.Time("dl_" + key, lambda v: v.detach().float().cpu().numpy(), value)
		if result["keypoints"] is not None and model.nodes is None and result["keypoints"].size:
			model.nodes = result["keypoints"].shape[2]
		inner = sum(st.wall.get(k, 0) for k in ("cen_pre", "cen_norm", "cen_copyin", "cen_fwd", "cen_peaks",
			"sizematch", "crop", "ins_pre", "ins_norm", "ins_copyin", "ins_fwd", "ins_peaks"))
		st.Host("between", layer_ns - inner)
		return result
	model.Predict = Predict


def InstrumentFixed(model, st, torch, a):
	"""
	--timing stages / coarse: campy-live's --fixed-crops path. The upload
	(the host buffer copied into the graph's input, static_in), the set's
	GPU work (stages: Staged's steps one by one; coarse: campy-live's own
	graph, one replay), and the download (the packed result into pinned
	memory, one wait, and its split into arrays).
	"""
	f = model.fixed
	upload = model.Upload
	model.Upload = lambda host: st.Time("upload", upload, host)
	if a.timing == "stages":
		st.staged = Staged(f, st, a.check)
		if f.graph:
			st.staged.Record()
		f.Run = st.staged.Run
	else:
		run = f.Run
		f.Run = lambda frames: st.Time("replay", run, frames)
	predict = f.Predict
	def Predict(frames):
		st.Wait()
		before = sum(st.wall.values())
		t = time.perf_counter_ns()
		out = predict(frames)
		st.Host("download", time.perf_counter_ns() - t - (sum(st.wall.values()) - before))
		st.last_crops = f.B * f.K
		return out
	f.Predict = Predict


class Staged:
	"""
	--timing stages: campy-live's default path (--fixed-crops, with
	--cuda-graphs one graph for the whole set, the networks compiled by
	--engine), cut at its stages. FixedCrops.Pipeline's steps, in its order
	and with its code, each recorded as a CUDA graph of its own (all in one
	memory pool, each reading the one before's output), so a stage can be
	waited for alone and still runs as it does inside campy-live's single
	graph. Without --cuda-graphs, the same steps run eagerly. Every
	--check sets, campy-live's own graph runs on the same frames as well and
	its output is compared with the stages': they must be the same.
	"""

	NAMES = ["cen_pre", "cen_net", "cen_peaks", "crop", "ins_pre", "ins_net", "ins_peaks"]

	def __init__(self, fixed, st, check):
		import torch
		self.f, self.st, self.torch, self.check = fixed, st, torch, check
		self.graphs = None
		self.diffs = [] # (set, largest difference from campy-live's graph)
		self.sets = 0
		f = fixed
		c, ci = f.c, f.ci
		cfg = c.preprocess_config
		self.sizematch = cfg.max_height is not None or cfg.max_width is not None

		def CenPre(frames):
			raw = c._to_4d_tensor(frames)
			x, eff = f.Preprocess(c, raw)
			self.eff = eff
			return raw, x

		def CenNet(x):
			return (f.Confmaps(c, x),)

		def CenPeaks(cms):
			return f.Centroids(cms, cfg.scale)

		def Crop(raw, centroids, found):
			sized = raw
			if self.sizematch:
				from sleap_nn.data.resizing import apply_sizematcher
				sized, _ = apply_sizematcher(raw, cfg.max_height, cfg.max_width)
			if self.eff != 1.0:
				centroids = centroids / self.eff
				crops, topleft = f.Crops(sized, centroids * self.eff, found)
			else:
				crops, topleft = f.Crops(sized, centroids, found)
			return crops, topleft, centroids

		def InsPre(crops):
			return (f.Preprocess(ci, crops)[0],)

		def InsNet(xi):
			return (f.Confmaps(ci, xi),)

		def InsPeaks(icms, topleft, centroids, cvals, found):
			keypoints, vals = f.Keypoints(icms)
			keypoints = keypoints + topleft.view(-1, 1, 2)
			if self.eff != 1.0:
				keypoints = keypoints / self.eff
			B, K = found.shape
			n = keypoints.shape[1]
			keypoints = torch.where(found.view(B * K, 1, 1), keypoints, torch.full_like(keypoints, float("nan")))
			vals = torch.where(found.view(B * K, 1), vals, torch.full_like(vals, float("nan")))
			return (torch.cat([centroids, cvals[..., None], found[..., None].to(torch.float32),
				keypoints.reshape(B, K, 2 * n), vals.reshape(B, K, n)], dim=-1),)

		# (name, step, its inputs, its outputs), by name in one environment
		self.steps = [
			("cen_pre", CenPre, ["frames"], ["raw", "x"]),
			("cen_net", CenNet, ["x"], ["cms"]),
			("cen_peaks", CenPeaks, ["cms"], ["centroids", "cvals", "found"]),
			("crop", Crop, ["raw", "centroids", "found"], ["crops", "topleft", "centroids"]),
			("ins_pre", InsPre, ["crops"], ["xi"]),
			("ins_net", InsNet, ["xi"], ["icms"]),
			("ins_peaks", InsPeaks, ["icms", "topleft", "centroids", "cvals", "found"], ["out"]),
		]

	def Eager(self, frames, timed=False):
		env = {"frames": frames}
		for name, step, ins, outs in self.steps:
			args = [env[k] for k in ins]
			res = self.st.Time(name, step, *args) if timed else step(*args)
			env.update(zip(outs, res))
		return env["out"]

	def Record(self):
		# Each step a graph, in order, in one pool, from campy-live's graph's
		# input (which Upload fills); after FixedCrops recorded its own
		torch, f = self.torch, self.f
		side = live.FixedStream("capture warm-up")
		side.wait_stream(torch.cuda.current_stream())
		with torch.cuda.stream(side), torch.inference_mode():
			for _ in range(3):
				warm = self.Eager(f.static_in)
		torch.cuda.current_stream().wait_stream(side)
		del warm
		pool = torch.cuda.graph_pool_handle()
		env = {"frames": f.static_in}
		self.graphs = []
		for name, step, ins, outs in self.steps:
			graph = torch.cuda.CUDAGraph()
			with torch.cuda.graph(graph, pool=pool, stream=live.FixedStream("capture"),
					capture_error_mode="thread_local"), torch.inference_mode():
				res = step(*[env[k] for k in ins])
			env.update(zip(outs, res))
			self.graphs.append((name, graph))
		self.env = env
		for _, graph in self.graphs:
			graph.replay()
		f.graph.replay()
		self.diffs.append((-1, live.MaxAbsDiff(f.static_out, env["out"])))

	def Run(self, frames):
		# FixedCrops.Run, stage by stage: the set's tensor, on the device
		torch, f = self.torch, self.f
		if f.graph:
			if frames is not f.static_in:
				f.static_in.copy_(frames, non_blocking=True)
			if self.graphs is None:
				self.Record()
			for name, graph in self.graphs:
				self.st.Time(name, graph.replay)
			out = self.env["out"]
			if self.check and self.sets % self.check == 0:
				# campy-live's graph on the same frames: its output, untimed
				# (after this set's times; the next set's are its own)
				f.graph.replay()
				self.diffs.append((self.sets, live.MaxAbsDiff(f.static_out, out)))
				self.st.checked = True
		else:
			with torch.inference_mode():
				out = self.Eager(frames, timed=True)
				if self.check and self.sets % self.check == 0:
					self.diffs.append((self.sets, live.MaxAbsDiff(f.Pipeline(frames), out)))
					self.st.checked = True
		self.sets += 1
		return out


def Run(a, rest):
	import torch
	st = Stages(torch, a.timing != "off")
	prepare = live.Prepare

	def Prepare(args, n_frames, shape, dtype):
		model, host = prepare(args, n_frames, shape, dtype) # warmed up untimed
		fixed = getattr(model, "fixed", None)
		if a.timing == "sync" and fixed is not None:
			raise SystemExit("--timing sync times sleap-nn's own layer's stages: give campy-live --fixed-crops off "
				"(or --timing stages, for --fixed-crops)")
		if a.timing in ("stages", "coarse") and fixed is None:
			raise SystemExit("--timing {}: campy-live's --fixed-crops path (its default on a GPU)".format(a.timing))
		if a.timing == "sync" and hasattr(model, "layer"):
			Instrument(model, st, torch)
		if a.timing in ("stages", "coarse"):
			InstrumentFixed(model, st, torch, a)
		upload, predict = model.Upload, model.Predict
		def Timed(h):
			# upload start to results on the host, as campy-live's inference_ns
			st.t0 = time.perf_counter_ns()
			st.start = st.t0 - st.started
			return upload(h)
		def Done(frames):
			out = predict(frames)
			st.EndSet(time.perf_counter_ns() - st.t0, getattr(st, "last_crops", -1))
			return out
		model.Upload, model.Predict = Timed, Done
		return model, host

	read = live.Cameras.ReadSet
	def ReadSet(self, n, out):
		t = time.perf_counter_ns()
		frames = read(self, n, out)
		st.Host("read", time.perf_counter_ns() - t)
		st.frame = n
		return frames

	live.Prepare = Prepare
	live.Cameras.ReadSet = ReadSet
	atexit.register(Save, a, st)
	try:
		live.Main(rest)
	except SystemExit as e:
		Save(a, st)
		raise


def Save(a, st):
	if getattr(st, "saved", False):
		return
	st.saved = True
	# The sets Staged checked ran campy-live's graph too: left out
	keep = [not c for c in st.checks]
	rows = [r for r, k in zip(st.rows, keep) if k]
	st.gpu_rows = [r for r, k in zip(st.gpu_rows, keep) if k]
	st.crops = [r for r, k in zip(st.crops, keep) if k]
	st.starts = [r for r, k in zip(st.starts, keep) if k]
	st.frames = [r for r, k in zip(st.frames, keep) if k]
	names = [n for n in ORDER if any(n in r for r in rows)]
	wall = np.array([[r.get(n, 0) for n in names] for r in rows], np.int64).reshape(len(rows), len(names))
	gnames = [n for n in ORDER if any(n in r for r in st.gpu_rows)]
	gpu = np.array([[r.get(n, 0) for n in gnames] for r in st.gpu_rows], np.int64).reshape(len(rows), len(gnames))
	np.savez(a.stages + ".stages.npz", names=np.array(names), wall_ns=wall, gpu_names=np.array(gnames), gpu_ns=gpu,
		crops=np.array(st.crops), start_ns=np.array(st.starts, np.int64), frame=np.array(st.frames, np.int64))
	summary = {"timing": a.timing, "sets": len(rows), "stages_ms": {n: Stats(wall[:, i]) for i, n in enumerate(names)},
		"gpu_ms": {n: Stats(gpu[:, i]) for i, n in enumerate(gnames)},
		"crops": {str(int(c)): int(k) for c, k in zip(*np.unique(st.crops, return_counts=True))} if st.crops else {}}
	staged = getattr(st, "staged", None)
	if staged is not None:
		d = [x for _, x in staged.diffs]
		summary["staged_vs_campy_live"] = {"checks": len(d), "largest_difference": max(d) if d else None,
			"sets_left_out": len(keep) - sum(keep),
			"graphs": staged.graphs is not None}
	if len(rows) > 1:
		# The first second (the start: kernels' first calls, caches) apart
		starts = np.asarray(st.starts)
		first = starts - starts[0] < 1e9
		summary["first_second"] = {"sets": int(first.sum()), "predict_total_ms": Stats(wall[first, names.index(
			"predict_total")]) if first.any() else None}
		summary["after_first_second_ms"] = {n: Stats(wall[~first, i]) for i, n in enumerate(names)} if (~first).any() else {}
	log = a.stages + ".json" # campy-live's summary, when --out is the same
	try:
		with open(log) as f:
			s = json.load(f)
		summary["campy_live"] = {k: s.get(k) for k in ("sets", "skipped", "inference_ms", "pose3d_ms",
			"latency_from_last_frame_ms", "latency_to_3d_from_last_frame_ms", "cuda_graphs")}
	except (OSError, ValueError):
		pass
	with open(a.stages + ".stages.json", "w") as f:
		json.dump(summary, f, indent=1)
	print("stage times in {}.stages.json".format(a.stages), file=sys.stderr)


# ---------------------------------------------------------------- kernels

def Frames(videos, n, H=1024, W=1280):
	# n sets of gray frames, one per video, decoded with ffmpeg
	import subprocess
	out = np.empty((n, len(videos), H, W, 1), np.uint8)
	for c, v in enumerate(videos):
		p = subprocess.run(["ffmpeg", "-v", "error", "-i", v, "-frames:v", str(n), "-f", "rawvideo",
			"-pix_fmt", "gray", "-"], capture_output=True, check=True)
		out[:, c, :, :, 0] = np.frombuffer(p.stdout, np.uint8)[:n * H * W].reshape(n, H, W)
	return out


def Peaks(torch):
	# This GPU's copy bandwidth (read + write, GB/s) and fp16 matmul (TFLOP/s)
	x = torch.empty(256 * 2 ** 20, dtype=torch.float16, device="cuda")
	y = torch.empty_like(x)
	for _ in range(3):
		y.copy_(x)
	torch.cuda.synchronize()
	s, e = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
	s.record()
	for _ in range(20):
		y.copy_(x)
	e.record()
	torch.cuda.synchronize()
	bw = 20 * 2 * x.numel() * 2 / (s.elapsed_time(e) / 1e3) / 1e9
	m = torch.randn(8192, 8192, dtype=torch.float16, device="cuda")
	for _ in range(3):
		m @ m
	torch.cuda.synchronize()
	s.record()
	for _ in range(10):
		m @ m
	e.record()
	torch.cuda.synchronize()
	tf = 10 * 2 * 8192 ** 3 / (s.elapsed_time(e) / 1e3) / 1e12
	del x, y, m
	return bw, tf


def LayerCost(module, inp, out):
	# (FLOPs, bytes that must move) for one call of a leaf module
	import torch
	x = inp[0] if inp else None
	if not isinstance(x, torch.Tensor) or not isinstance(out, torch.Tensor):
		return 0, 0
	el = 2 # fp16 under autocast
	moved = x.numel() * el + out.numel() * el
	flops = 0
	if isinstance(module, (torch.nn.Conv2d, torch.nn.ConvTranspose2d)):
		w = module.weight
		moved += w.numel() * el
		k = w.shape[2] * w.shape[3]
		if isinstance(module, torch.nn.Conv2d):
			flops = 2 * out.numel() * (w.shape[1]) * k # out elements x (Cin/groups x kh x kw) MACs
		else:
			flops = 2 * x.numel() * w.shape[1] * k
	return flops, moved


def Kernels(a):
	import torch, warnings
	from torch.profiler import profile, ProfilerActivity, record_function
	sys.argv = ["campy-live"]
	args = live.ParseArgs(["--model", "sleap", "--centroid", a.centroid, "--instance", a.instance,
		"--max-instances", str(a.max_instances), "--fixed-crops", "off", "--cuda-graphs", "off"])
	frames = Frames(a.video, a.sets)
	n_frames = frames.shape[1]
	with warnings.catch_warnings():
		warnings.simplefilter("ignore")
		model = live.TrainedTopDown(args, n_frames, frames.shape[2:])
	host = live.HostBuffer(True, frames.shape[1:], np.uint8)
	model.Warm(host.tensor, crops=range(1, n_frames * a.max_instances + 1))
	bw, tf = Peaks(torch)
	print("GPU copy bandwidth {:.0f} GB/s (read + write), fp16 matmul {:.1f} TFLOP/s".format(bw, tf))

	# Leaf modules: a profiler range each, and their FLOPs and bytes
	layer = model.layer
	costs = {}
	hooks = []
	for tag, inner in (("cen", layer.centroid_layer), ("ins", layer.centered_instance_layer)):
		net = inner.backend.model
		for name, m in net.named_modules():
			if list(m.children()):
				continue
			label = "{}:{}:{}".format(tag, name, type(m).__name__)
			def Pre(mod, inp, _label=label):
				mod._range = record_function(_label)
				mod._range.__enter__()
			def Post(mod, inp, out, _label=label):
				mod._range.__exit__(None, None, None)
				f, b = LayerCost(mod, inp, out)
				c = costs.setdefault(_label, [0, 0, 0, None, None])
				c[0] += f; c[1] += b; c[2] += 1
				c[3] = tuple(inp[0].shape) if inp and isinstance(inp[0], torch.Tensor) else None
				c[4] = tuple(out.shape) if isinstance(out, torch.Tensor) else None
			hooks.append(m.register_forward_pre_hook(Pre))
			hooks.append(m.register_forward_hook(Post))
		def Whole(mod, inp, _tag=tag):
			mod._whole = record_function(_tag + ":forward")
			mod._whole.__enter__()
		def WholeEnd(mod, inp, out):
			mod._whole.__exit__(None, None, None)
		hooks.append(net.register_forward_pre_hook(Whole))
		hooks.append(net.register_forward_hook(WholeEnd))

	sets = [torch.from_numpy(f).pin_memory() for f in frames]
	for s in sets[:5]: # the hooks' first calls
		model.Predict(model.Upload(s))
	costs.clear()
	torch.cuda.synchronize()
	with profile(activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA]) as prof:
		for s in sets:
			model.Predict(model.Upload(s))
		torch.cuda.synchronize()
	for h in hooks:
		h.remove()

	def DeviceUs(e):
		for k in ("device_time_total", "cuda_time_total"):
			if hasattr(e, k):
				return getattr(e, k)
		return 0
	avg = {e.key: e for e in prof.key_averages()}
	rows = []
	for label, (f, b, calls, ishape, oshape) in costs.items():
		e = avg.get(label)
		us = DeviceUs(e) if e is not None else 0
		if not us:
			continue
		sec = us / 1e6
		rows.append({"layer": label, "calls": calls, "in": ishape, "out": oshape, "gpu_ms_per_set": us / 1e3 / a.sets,
			"MB_per_set": b / 1e6 / a.sets, "GFLOP_per_set": f / 1e9 / a.sets,
			"GB_s": b / sec / 1e9, "TFLOP_s": f / sec / 1e12, "flop_per_byte": f / b if b else 0,
			"bw_frac": b / sec / 1e9 / bw, "flop_frac": f / sec / 1e12 / tf})
	rows.sort(key=lambda r: -r["gpu_ms_per_set"])
	whole = {t: DeviceUs(avg[t + ":forward"]) / 1e3 / a.sets for t in ("cen", "ins") if t + ":forward" in avg}
	kernels = []
	for e in prof.key_averages():
		if "CUDA" in str(getattr(e, "device_type", "")): # a GPU kernel or copy
			kernels.append({"kernel": e.key[:120], "calls": e.count, "gpu_ms_per_set": DeviceUs(e) / 1e3 / a.sets})
	kernels.sort(key=lambda r: -r["gpu_ms_per_set"])
	result = {"gpu_copy_GB_s": bw, "gpu_fp16_matmul_TFLOP_s": tf, "sets": a.sets, "forward_gpu_ms_per_set": whole,
		"layers": rows, "top_kernels": kernels[:40]}
	with open(a.out, "w") as f:
		json.dump(result, f, indent=1)
	print("forward GPU ms per set:", {k: round(v, 3) for k, v in whole.items()})
	print("{:<58} {:>7} {:>7} {:>8} {:>7} {:>6} {:>6}".format("layer", "ms/set", "MB/set", "GFLOP", "GB/s", "TF/s", "F/B"))
	for r in rows[:30]:
		print("{:<58} {:7.3f} {:7.1f} {:8.3f} {:7.0f} {:6.2f} {:6.1f}".format(r["layer"][:58], r["gpu_ms_per_set"],
			r["MB_per_set"], r["GFLOP_per_set"], r["GB_s"], r["TFLOP_s"], r["flop_per_byte"]))
	print("top kernels:")
	for k in kernels[:20]:
		print("  {:8.3f} ms/set  {:5d} calls  {}".format(k["gpu_ms_per_set"], k["calls"], k["kernel"]))
	if a.trace:
		prof.export_chrome_trace(a.trace)


def Main():
	p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
	sub = p.add_subparsers(dest="cmd", required=True)
	r = sub.add_parser("run", help="campy-live with its stages timed; campy-live's arguments after --")
	r.add_argument("--stages", required=True, help="Output prefix: <stages>.stages.npz and .stages.json "
		"(give campy-live the same --out to fold its summary in).")
	r.add_argument("--timing", default="stages", choices=["stages", "coarse", "sync", "off"],
		help="stages: --fixed-crops (the default path), step by step; coarse: --fixed-crops, upload / campy-live's "
		"own graph / download; sync: sleap-nn's layer (--fixed-crops off), step by step; off: untimed.")
	r.add_argument("--check", type=int, default=100, help="stages: every this many sets, campy-live's own graph "
		"runs on the same frames too, and its output is compared with the stages' (0: never).")
	k = sub.add_parser("kernels", help="per-layer GPU time vs bytes and arithmetic")
	k.add_argument("--centroid", required=True)
	k.add_argument("--instance", required=True)
	k.add_argument("--video", action="append", required=True, help="One per camera.")
	k.add_argument("--sets", type=int, default=100)
	k.add_argument("--max-instances", type=int, default=5)
	k.add_argument("--out", required=True)
	k.add_argument("--trace", default="", help="Also a Chrome trace here.")
	argv = sys.argv[1:]
	rest = []
	if "--" in argv:
		i = argv.index("--")
		argv, rest = argv[:i], argv[i + 1:]
	a = p.parse_args(argv)
	if a.cmd == "run":
		Run(a, rest)
	else:
		Kernels(a)


if __name__ == "__main__":
	Main()
