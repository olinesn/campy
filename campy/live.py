"""
campy-live: SLEAP pose estimation on the frames campy is recording.

campy-acquire, with liveFeed True, copies each camera's frames into shared
memory (campy/livefeed.py). campy-live reads them, as a separate program that
campy never waits for: it takes the newest set of frames that every camera
has (the same frame number on each), runs a sleap-nn top-down model on the
set as one batch, logs the keypoints, and goes straight to the newest set
again. Sets that came while it was busy are skipped, and counted, never
queued, so it is never further behind than one set.

It runs in its own Python environment, the one with torch and sleap-nn: it
imports only numpy and this package's livefeed, liveresults, frametimes, clock,
children, manifest and defaults, which need nothing else, so campy's camera
drivers need not be installed there. In that environment, either install
campy without its dependencies

    pip install --no-deps -e <campy folder>
    campy-live --cameras 3

or run it from campy's folder:

    PYTHONPATH=<campy folder> python -m campy.live --cameras 3

Start it before or after campy-acquire; it waits for the cameras' feeds, and
ends when the recording does (or at Ctrl-C), with a summary. Best before: it
loads and warms up the model first (5-8 s with trained models), for frames of
--frame-shape (1024x1280x1, campy's FLIR cameras), and only then waits for the
feed, so it is ready for the recording's first frame. A feed of another frame
size has the model built again (and its first seconds missed).

Models (--model)
----------------
random   (default) random-weight models of the sizes the live benchmark
         picked (bench/sleap_infer_bench.py): centroid at 0.25 scale, 16
         filters, max stride 16, output stride 4; centered instance on 192 px
         crops, 16 filters, max stride 16, output stride 2; 15 nodes. The
         centroid model's output is replaced by --animals fixed peaks per frame,
         as in the benchmark, so the time is that of a trained model finding
         that many animals. The keypoints mean nothing.
sleap    trained sleap-nn models: --centroid <model folder> and --instance
         <model folder> (each with training_config.yaml and best.ckpt).
none     no model: reads and logs the sets only, to check the feed.

3D (--calibration)
------------------
With --calibration <anipose calibration.toml>, each set's keypoints also go
to campy/pose3d.py's Tracker: it decides which detection in each camera is the
same animal, keeps each animal's identity (0 to --animals - 1) from set to
set, and triangulates the ones that 2 or more cameras see. The cameras are
found in the calibration by campy's camera names (the feed's), or by
--calibration-names, in feed order. It takes about 1 ms a set on one core for
5 animals. It runs in a thread of its own, on each set in turn, while the
model runs on the next set: a set takes the longer of the two, not their sum.
A set's row is logged once its 3D is made.

Output
------
<out>.npy, one row per set processed (a structured array; np.load reads it,
also while campy-live runs), and <out>.json, the summary, which also says the
model's nodes and edges, and with --calibration the calibration's path, its
cameras' names and the number of animals: what campy-live-export
(campy/liveexport.py) needs to write the results as files LUC3D opens.
And campy-live-session.json in the recording's folder (beside the log,
<out>.session.json, when the feed does not say where the recording is): how
this run was made, for the recording to keep (campy/manifest.py): campy-
live's environment, the models' folders and SHA-256s and the settings they
were trained with, the calibration's SHA-256, every option and where it came
from, and how the run ended. At startup campy-live says each default that
changed in a newer campy and that this run relies on (campy/defaults.py):
--defaults-version and --default-warnings stop them.
Fields of each row:

  frame          the set's frame number
  host_ns        per camera: when campy got the frame (as hosttimes.npy, on
                 campy's host clock, campy/clock.py)
  written_ns     per camera: when it was in shared memory (time.perf_counter_ns)
  stamp          per camera: the camera's timestamp, seconds
  read_ns        when campy-live had copied the set (host clock)
  done_ns        when its keypoints were back on the host (host clock)
  inference_ns   upload, model and results back, timed with time.perf_counter_ns
  skipped        sets skipped just before this one
  found          per camera: animals found
  centroids      per camera, per animal: x, y in the frame (NaN: none)
  centroid_vals  per camera, per animal: the centroid's confidence
  keypoints      per camera, per animal, per node: x, y in the frame
  peak_vals      per camera, per animal, per node: confidence

and with --calibration:

  pose3d_ns      when the 3D poses were made (host clock)
  points3d       per animal (identity), per node: x, y, z in the calibration's
                 units (mm); NaN when fewer than 2 cameras saw it
  members        per animal, per camera: which of that camera's animals it
                 is (an index into keypoints), -1 none
  n_views        per animal: how many cameras saw it
  reproj_px      per animal, per node: mean reprojection error, pixels

Speed
-----
GPU clock (--hold-gpu-clock): a GPU sets its own clock by how busy it
looks, and campy-live's work, a burst every frame, didn't look busy enough.
Waiting for the feed, an RTX 2000 Ada dropped to its idle clock (210 MHz),
so the first sets of a recording took 110-130 ms and the next ones were
skipped; then it settled at 780 MHz of its 2500, and each set took 27 ms
where 13 would do. The EVO-X2's AMD GPU ran light models at 2100 of its
2900 MHz, 10% slower. So from the feed's coming to the end, campy-live
keeps a few of the GPU's blocks spinning (torch.cuda._sleep), on streams of
the lowest priority: 4 while it waits for frames, and while they come, 0 to
2 (--hold-gpu-clock-blocks auto): it reads the GPU's clock every half
second (NVML, through the nvidia-ml-py package's pynvml if installed, else
nvidia-smi; on ROCm the driver's pp_dpm_sclk) and spins one block more
when the clock falls below 90% of the fastest it has seen, one fewer after
10 s at full clock. The GPU then looks busy and stays at its full clock,
for about 20 W while waiting (against 2.5 W idle). The model's work goes
first, on the GPU's other blocks, but a spinning block still costs it
about 0.3 ms a set (2, 0.55 ms), so none spin when the clock stays up
without them (an RTX 2000 Ada did so after a restart, not after two days up). A clock that the
GPU's power or heat limit holds down is left alone. On an AMD integrated
GPU (the EVO-X2's, which shares its chip with the CPU) at least 1 spins
while sets come: with none, its clock still read 2650-2850 MHz, yet each
set took about 1 ms longer (13.7 against 12.8 ms, 5 mice, --fixed-crops
--cuda-graphs) and the CPU's 3D step too, so the whole chip slows down
between sets, which its GPU clock doesn't show. auto: on with a GPU
(NVIDIA's CUDA and AMD's ROCm); off with MPS and the CPU.

CPUs (--cpus, --priority, --campy-cpus): on a CPU with performance cores
(P-cores) and efficiency cores (E-cores), as Intel's Core Ultra, campy-live
puts itself on the performance cores at high priority, and campy-acquire
(its camera processes and their encoders, which its feed says) on the
efficiency cores, once its feed comes: the CPU work of launching the model
then never waits for campy's. python -m campy.cpus says which cores are
which; --cpus none --campy-cpus none leaves both as they were started, and
campy-live-view's c key moves them back and forth while they run.

--cuda-graphs records each model's GPU work once per input shape and then
replays it in one go (a CUDA graph), instead of launching its hundred or so
GPU operations one by one, each costing the CPU some microseconds: the
results are the same, and campy-live says how far from the same they were
when it made the graphs (0 is the same). Each count of animals found is
another shape, so another graph: a model's graphs share one pool of GPU
memory (CUDAGraphed), and campy-live says how much they hold.

--fixed-crops (FixedCrops) crops --max-instances animals in every frame,
the missing ones padded and their keypoints dropped, and finds the peaks
with shapes that never depend on the frames. So the CPU launches a whole
set without waiting for the GPU to say how many animals it found, the
results come back in one copy, and with --cuda-graphs the whole set, both
models and everything around them, is one graph. The animals found and the
keypoints are sleap-nn's own (to the last bit in fp32, as long as sleap-nn
cuts its crops at their size: sleap-nn 0.3.3 cuts some sets' crops a pixel
short, FixedCrops never does; in fp16 the instance model rounds differently
with another batch size, so sets with fewer animals than --max-instances can
differ). It pays when every frame has about --max-instances animals: each
missing one still costs a crop's worth of instance model.

Both are on by default with a GPU (auto), off on the CPU and MPS: with 5
mice, 3 cameras and the trained models, a set took 16.8 -> 12.6 ms on an
RTX 2000 Ada, 10.3 -> 2.7 on an RTX 6000 Ada and 15.0 -> 13.6 on the
EVO-X2's Radeon (docs/live-tracking.md). A model --fixed-crops can't run
(centroid NMS, ground truth, classes, other refinement) gets sleap-nn's
own layer, with graphs per count, and campy-live says why. --fixed-crops
off and --cuda-graphs off give the old way.

--engine (with --fixed-crops, whose shapes never change) runs the two
networks as compiled engines (FixedEngine) instead of torch's layer by layer:
compile is torch.compile (inductor, Triton kernels: ROCm, and NVIDIA where
Triton is installed), tensorrt is NVIDIA's TensorRT (an fp16 or fp32 engine
built from the network's ONNX). A compiled engine fuses each convolution with
what follows it (bias, activation, upsampling), so far less goes to and from
GPU memory (bench/compiled_bench.py measured the networks alone). Only the
networks change: crops, peaks and everything else are FixedCrops' own, and
with --cuda-graphs the engines' kernels are recorded into the set's one
graph like the rest.

auto, the default (FixedEngine.Pick): tensorrt on an NVIDIA GPU where
tensorrt and onnx import, else compile where Triton does, else none, said
once. An engine that fails while warming up (a Triton that doesn't match
torch, a TensorRT build error) leaves the networks in torch, as --engine off,
with one line saying why, and the warm-up runs again: all before the feed is
waited for, so never during a recording. on: auto's choice, stopping if it
can't be made; compile, tensorrt: that one, stopping likewise; off (eager):
none. Live, 5 mice on 3 cameras at 60 fps, against the defaults before it (5
to 9 replays each, docs/live-tracking.md): EVO-X2 10.6 -> 5.9 ms a set
(compile); RTX 2000 Ada 12.6 -> 5.8, RTX 4000 Ada 6.4 -> 3.4, RTX 6000 Ada
2.7 -> 1.6 (tensorrt), tracking as close to the offline SLEAP predictions as
before, and no more GPU memory. TensorRT's fp16 runs the whole network in
half precision and moves 2.0-2.3% of the keypoints by over a pixel from torch's
fp16 (torch.compile 0.7%): the same animals, and the same 2D and 3D errors
against offline SLEAP. The warm-up waits for the engines: the first time
(another model, GPU, PyTorch or TensorRT) 13-35 s more, then 0-4 s. Both
keep what they made in --engine-cache (~/.cache/campy/engines): TensorRT's
plans, named by the weights, shape, precision, GPU and the versions, and
torch.compile's kernels in its inductor folder (TORCHINDUCTOR_CACHE_DIR,
unless set; inductor's own default, the temp folder, is emptied at each
restart on Linux).

--fast-upsample runs each of the models' bilinear upsampling layers in the
other memory layout (channels_last instead of NCHW) where that is faster
for its input, as timed while warming up: PyTorch's NCHW upsampling kernel
leaves the GPU nearly idle on the small, deep layers (5x5 pixels by 768
channels), and its channels_last one is slower on the big ones. The results
are the same. auto: on with a GPU (CUDA or ROCm) and --memory-format nchw,
which auto picks on ROCm (the EVO-X2, 5 mice, 3 cameras, --fixed-crops
--cuda-graphs: 13.6 to 10.6 ms a set). With channels_last models (auto's on
NVIDIA GPUs) every layer was faster as it was, so it does nothing there. With
a compiled engine (--engine auto's, on every GPU with the packages) it is off:
the engine fuses the upsampling itself (EVO-X2: 5.9 ms a set, against 10.6
with --fast-upsample and no engine); it is for the networks run in torch.

With --results-feed on (the default), each row also goes into shared memory,
campy-live-results (campy/liveresults.py), as it is logged: campy-live-view
(campy/liveview.py) shows them on the cameras' frames.

Latency, from a frame reaching the computer to its keypoints, is done_ns
minus host_ns, both on campy's host clock (campy/clock.py), one clock in
every process. That is time.monotonic_ns, except on Windows before Python
3.13, where time.monotonic moves in steps of 15.6 ms and the host clock is
time.perf_counter_ns instead: precise there too. The summary's inference and
3D times are durations, timed with time.perf_counter_ns. The summary
gives latency from the set's last frame (the earliest the set could be read)
and its first.
"""

import argparse, json, math, os, queue, signal, sys, threading, time
from datetime import datetime
import numpy as np
from campy import children, cpus, defaults, livefeed, liveresults, manifest
from campy import clock as hostclock # not clock: Run's clock is the GPU's
from campy.frametimes import GrowingNpy, Writer

# TORCHINDUCTOR_CACHE_DIR as campy-live was started with it: torch's inductor
# writes its own default into it the first time it looks (cache_dir_utils),
# so later it can't say whether the user set it (FixedEngine.Compile)
_INDUCTOR_DIR = os.environ.get("TORCHINDUCTOR_CACHE_DIR")

# The random models: what bench/sleap_infer_bench.py's default pair is
RANDOM_CENTROID = dict(scale=0.25, filters=16, max_stride=16, output_stride=4)
RANDOM_INSTANCE = dict(crop=192, filters=16, max_stride=16, output_stride=2)


def Say(text):
	print("[{}] {}".format(datetime.now().strftime("%H:%M:%S"), text), flush=True)


# ---------------------------------------------------------------- the feed

class Cameras:
	"""
	The rings of every camera, and the newest set of frames that all hold.
	"""

	def __init__(self, names, poll=0.0005):
		self.names = list(names)
		self.poll = poll
		self.readers = [None] * len(self.names)
		self.buffer = None # (cameras, height, width, channels): the set, copied out
		self.last = 0 # the last set's frame number

	def Attach(self, wait=float("inf"), stop=lambda: False):
		# Waits until every camera's ring is there and recording
		deadline = time.monotonic() + wait
		said = False
		partly = None # since when some cameras' feeds are there, and not all
		while True:
			for i, name in enumerate(self.names):
				if self.readers[i] is None:
					try:
						reader = livefeed.RingReader(name)
					except (OSError, ValueError): # not there yet, or being made (its header not written yet)
						continue
					if reader.Ended():
						reader.Close()
						continue
					self.readers[i] = reader
			if all(self.readers):
				break
			if stop() or time.monotonic() > deadline:
				return False
			missing = [n for n, r in zip(self.names, self.readers) if r is None]
			if not said:
				Say("Waiting for the live feed of {} (campy-acquire with liveFeed True)...".format(", ".join(missing)))
				said = True
			if len(missing) < len(self.names):
				partly = partly or time.monotonic()
				if time.monotonic() - partly > 10:
					Say("Still no live feed from {}, while the others' are there: did that camera open? "
						"campy-live waits for every camera's (--cameras, --rings).".format(", ".join(missing)))
					partly = float("inf")
			# A ring that ended while the others never came: wait for the next recording's
			for i, reader in enumerate(self.readers):
				if reader is not None and reader.Ended():
					reader.Close()
					self.readers[i] = None
			time.sleep(0.2)
		shapes = {(r.shape, r.dtype.str) for r in self.readers}
		if len(shapes) > 1:
			raise ValueError("the cameras' frames differ in size or type: {}".format(
				", ".join("{} {}x{}x{} {}".format(r.cameraName, *(r.shape + (r.dtype,))) for r in self.readers)))
		return True

	def Describe(self):
		r = self.readers[0]
		return "{} cameras ({}), {}x{}x{} {}, {} slots each".format(len(self.readers),
			", ".join(r.cameraName or n for r, n in zip(self.readers, self.names)), *(r.shape + (r.dtype, r.slots)))

	def Ended(self):
		# The recording ended: every camera's ring did, or one was made again
		# (a new recording, or a frame of another size)
		ended = [r.Ended() for r in self.readers]
		if all(ended) or any(r.Replaced() for r in self.readers):
			return True
		for i, name in enumerate(self.names):
			if ended[i]:
				try:
					reader = livefeed.RingReader(name)
				except (OSError, ValueError): # not there, or the next one still being made
					continue
				replaced = reader.ringId != self.readers[i].ringId
				reader.Close()
				if replaced:
					return True
		return False

	def Newest(self):
		# The newest frame number every camera has written
		return min(r.Latest() for r in self.readers)

	def ReadSet(self, n, out):
		# Frame n of every camera into out; None if a ring no longer holds it
		frames = []
		for i, reader in enumerate(self.readers):
			frame = reader.Read(n, out[i])
			if frame is None:
				return None
			frames.append(frame)
		return frames

	def NextSet(self, out, stop=lambda: False, check_every=0.25):
		"""
		Waits for a set newer than the last one, and copies the newest that
		every ring still holds whole into out. Returns (frames, skipped), or
		None when the recording ends or stop() says to.
		"""
		checked = time.monotonic()
		while True:
			newest = self.Newest()
			if newest > self.last:
				# The newest first; if a ring replaced it as it was copied (a
				# reader slower than the slots), the next older ones
				oldest = max(self.last + 1, max(r.Latest() for r in self.readers) - self.readers[0].slots + 2)
				for n in range(newest, oldest - 1, -1):
					frames = self.ReadSet(n, out)
					if frames is not None:
						skipped = n - self.last - 1 if self.last else 0
						self.last = n
						return frames, skipped
				self.last = newest # none whole: look again at the next frame
				continue
			now = time.monotonic()
			if now - checked > check_every:
				checked = now
				if stop() or self.Ended():
					return None
			time.sleep(self.poll)

	def Close(self):
		for r in self.readers:
			if r is not None:
				r.Close()
		self.readers = [None] * len(self.names)


# ---------------------------------------------------------------- the models

def ResolveDevice(name):
	import torch
	if name == "auto":
		if torch.cuda.is_available():
			return "cuda"
		if torch.backends.mps.is_available():
			return "mps"
		return "cpu"
	return name


def Synchronize(device):
	import torch
	if device.startswith("cuda"):
		torch.cuda.synchronize()
	elif device == "mps":
		torch.mps.synchronize()


class NoModel:
	"""--model none: no inference, the feed alone."""
	device = "cpu"
	nodes = 0
	edges = None

	def __init__(self, args, n_frames, shape):
		self.instances = 0

	def Upload(self, host):
		return host

	def Predict(self, frames):
		return {}

	def Describe(self):
		return "no model (--model none): reading the feed only"


class TopDown:
	"""
	A sleap-nn TopDownLayer, on a device: Upload sends a set of frames to it,
	Predict runs the layer and brings the results back as numpy arrays.
	"""

	upsamples = () # --fast-upsample's layers (LayoutUpsample); none in a TopDown made without __init__
	engineAuto, engineNote = False, None # --engine auto, and what it says (a TopDown made without __init__: off)

	def __init__(self, args, n_frames, shape):
		import torch
		self.torch = torch
		self.device = ResolveDevice(args.device)
		self.precision = args.precision
		self.instances = args.max_instances
		self.n_frames, self.shape = n_frames, shape
		self.memoryFormat = args.memory_format
		if self.memoryFormat == "auto":
			nvidia = self.device.startswith("cuda") and not getattr(torch.version, "hip", None)
			self.memoryFormat = "channels_last" if nvidia else "nchw"
		self.nodes, self.nodeNames, self.edges = None, None, None
		# A centroid whose top is two equal pixels (fp16 rounding) is kept
		# once, not dropped as sleap-nn does (campy/peaks.py)
		from campy import peaks
		tied = not getattr(args, "strict_peaks", False)
		self.tiedPeaks = peaks.UseTiedPeaks(tied) and tied
		self.layer, self.text = self.Build(args)
		ExactCropSize(self.layer)
		# --engine: auto (the default) is FixedEngine.Pick's choice, and if
		# that can't be made, the networks in torch, as off, said once
		# (engineNote); any other choice that can't be made stops campy-live
		engine = getattr(args, "engine", "eager")
		self.engineAuto = engine == "auto"
		self.engineNote = None
		if self.engineAuto:
			engine, why = FixedEngine.Pick(self.device)
			if why:
				self.engineNote = "--engine auto: {}; so the networks run in torch".format(why)
		else:
			engine = FixedEngine.Choose(engine, self.device)
		self.args = args
		self.upsamples = []
		if FastUpsample(args, self.device, self.memoryFormat, engine):
			self.AddUpsamples()
		self.graphs = []
		self.fixed = None
		self.fixedNote = None # why --fixed-crops auto left them off
		gpu = self.device.startswith("cuda")
		graphs, fixed = OnOff(args.cuda_graphs), OnOff(getattr(args, "fixed_crops", "off"))
		if graphs == "on" and not gpu:
			raise SystemExit("--cuda-graphs: CUDA (or ROCm) only, not {}".format(self.device))
		graphs = graphs == "on" or (graphs == "auto" and gpu)
		explicit = engine != "eager" and not self.engineAuto
		if fixed == "on" or (fixed == "auto" and gpu):
			try:
				self.fixed = FixedCrops(self.layer, n_frames, self.device, args.max_instances, graph=graphs,
					tiedPeaks=self.tiedPeaks, engine=engine, cache=getattr(args, "engine_cache", ""))
			except SystemExit as e:
				if fixed == "on" or explicit:
					raise
				self.fixedNote = "{}; so sleap-nn's own top-down layer".format(e)
		if self.fixed is None and explicit:
			raise SystemExit("--engine: with --fixed-crops only (a compiled engine is made for one input shape)")
		if self.fixed is None and engine != "eager":
			self.engineNote = None # auto, without fixed crops: nothing to compile, nothing to say
			if FastUpsample(args, self.device, self.memoryFormat, "eager") and not self.upsamples:
				self.AddUpsamples()
		if self.fixed is None and graphs:
			for backend in (self.layer.centroid_layer.backend, self.layer.centered_instance_layer.backend):
				graphed = CUDAGraphed(backend.model, backend.use_fp16)
				backend.model, backend.use_fp16 = graphed, False # fp16 now inside the graph
				self.graphs.append(graphed)

	def AddUpsamples(self):
		# --fast-upsample's layers, before CUDAGraphed wraps the models
		for backend in (self.layer.centroid_layer.backend, self.layer.centered_instance_layer.backend):
			self.upsamples += LayoutUpsample.Into(backend.model)

	def EngineFallback(self, error):
		# --engine auto: the engine couldn't be made (or run) while warming
		# up, so the networks in torch, with --fast-upsample as it would be
		# with --engine off
		self.engineNote = "--engine auto: the {} engine failed ({}: {}); so the networks run in torch".format(
			{"compile": "torch.compile", "tensorrt": "TensorRT"}.get(self.fixed.engine, self.fixed.engine),
			type(error).__name__, str(error).strip().splitlines()[0][:300] if str(error).strip() else "no message")
		self.fixed.Plain()
		if FastUpsample(self.args, self.device, self.memoryFormat, "eager") and not self.upsamples:
			self.AddUpsamples()

	def Fp16(self, backends):
		# fp16 autocast inside sleap-nn's TorchBackend (cuda and ROCm only)
		if self.precision == "fp16" and self.device.startswith("cuda"):
			for backend in backends:
				backend.use_fp16 = True

	def Upload(self, host):
		if self.fixed is not None:
			return self.fixed.Upload(host)
		return host.to(self.device, non_blocking=True)

	def Predict(self, frames):
		if self.fixed is not None:
			result = self.fixed.Predict(frames)
			if self.nodes is None:
				self.nodes = result["keypoints"].shape[2]
			return result
		out = self.layer.predict(frames)
		result = {}
		for key, field in (("centroids", "pred_centroids"), ("centroid_vals", "pred_centroid_values"),
				("keypoints", "pred_keypoints"), ("peak_vals", "pred_peak_values")):
			value = getattr(out, field, None)
			result[key] = None if value is None else value.detach().float().cpu().numpy()
		if result["keypoints"] is not None and self.nodes is None and result["keypoints"].size:
			self.nodes = result["keypoints"].shape[2]
		return result

	def Warm(self, host, crops=()):
		# The first call of each shape is slow (MIOpen finds its kernels on
		# ROCm), and so is the first of each GPU operation: made before the
		# recording's frames come. Blank frames have no animals, and a set
		# with animals goes on to crop around each, run the instance model on
		# the crops and put their keypoints back in the frame, operations a
		# set with none never runs (the first set with animals took 200 ms
		# when only the instance model had been warmed). So the whole layer
		# runs again for each count of crops, with that many peaks put into
		# the centroid model's output (FakeCentroids). With --fixed-crops,
		# every set runs all of it, on the same shapes: blank frames warm it.
		# --fast-upsample: each upsampling layer times its two layouts on the
		# first input of each shape it gets, here (LayoutUpsample).
		# --engine auto: the engines are made here, at the first set; if that
		# fails, the networks run in torch instead and the warm-up starts again
		try:
			self.WarmOnce(host, crops)
		except (Exception, SystemExit) as e:
			if not (self.engineAuto and self.fixed is not None and self.fixed.engine != "eager"):
				raise
			if self.device.startswith("cuda"):
				self.torch.cuda.synchronize()
			self.EngineFallback(e)
			self.WarmOnce(host, crops)

	def WarmOnce(self, host, crops):
		frames = self.Upload(host)
		for up in self.upsamples:
			up.measuring = True
		try:
			for _ in range(3):
				self.Predict(frames)
			for counts in WarmCounts(self.n_frames, self.instances, crops) if self.fixed is None else ():
				with FakeCentroids(self.layer.centroid_layer, counts):
					self.Predict(frames)
		finally:
			for up in self.upsamples:
				up.measuring = False
				up.Settle()
		Synchronize(self.device)

	def DescribeEngine(self):
		return self.engineNote

	def DescribeUpsamples(self):
		if not self.upsamples:
			return None
		timed = [t for up in self.upsamples for t in up.timed.values()]
		# the GPU time it saves on the largest input each layer got (most animals)
		most = [up.timed[max(up.timed, key=lambda k: k[0][0])] for up in self.upsamples if up.timed]
		return "--fast-upsample: {} of {} upsampling layer shapes run {} ({} of {} layers at the most animals, " \
			"{:.2f} ms less GPU time there)".format(sum(t[2] for t in timed), len(timed),
			self.upsamples[0].otherName or "in the other layout", sum(t[2] for t in most), len(most),
			sum(t[0] - t[1] for t in most if t[2]))

	def DescribeGraphs(self):
		if self.fixed is not None:
			return self.fixed.Describe()
		if not self.graphs:
			return self.fixedNote
		diff = max(g.max_abs_diff for g in self.graphs)
		pooled = PoolBytes(set().union(*(g.Pools() for g in self.graphs)))
		return ("" if self.fixedNote is None else self.fixedNote + ". ") + \
			"CUDA graphs: {} made{}; their output {} (largest difference {:g})".format(
			sum(len(g.graphs) for g in self.graphs),
			"" if pooled is None else ", holding {:.0f} MB of GPU memory".format(pooled / 1e6),
			"the same as without" if diff == 0 else "differs from without", diff)


_exactCrop = threading.local() # the crop size, while one of campy-live's layers crops


def ExactCropSize(layer):
	"""
	Has this sleap-nn TopDownLayer cut its crops at its crop size. sleap-nn
	0.3.3's crop_bboxes takes the size from the first box's corners,
	int(abs(right - left)) + 1, in float32: for some centroids (x 221.547989
	with 192 px crops) right - left comes out 190.99998, not 191, and every
	crop of the set is cut a pixel short (191, or 319 of 320), moving
	keypoints by up to tens of pixels. So while this layer crops, the first
	box's far corners are put half a pixel past the crop size's, a copy of
	the boxes, and the size rounds right; the top-lefts, which place the
	crops and the keypoints, are untouched. sleap-nn's other layers, and
	its files, are left as they are.
	"""
	import torch
	from sleap_nn.inference.layers import topdown
	if not getattr(topdown.crop_bboxes, "campyExact", False):
		original = topdown.crop_bboxes

		def crop_bboxes(images, bboxes, sample_inds):
			size = getattr(_exactCrop, "size", None)
			if size is not None and len(bboxes):
				bboxes = bboxes.clone()
				bboxes[0, 3, 1] = bboxes[0, 0, 1] + (size[0] - 0.5)
				bboxes[0, 1, 0] = bboxes[0, 0, 0] + (size[1] - 0.5)
			return original(images, bboxes, sample_inds)

		crop_bboxes.campyExact = True
		topdown.crop_bboxes = crop_bboxes
	stage2 = layer._run_stage_2

	def RunStage2(*args, **kwargs):
		_exactCrop.size = tuple(layer.crop_size)
		try:
			return stage2(*args, **kwargs)
		finally:
			_exactCrop.size = None

	layer._run_stage_2 = RunStage2


class CUDAGraphed:
	"""
	A model's forward pass recorded as a CUDA graph for each shape of input,
	and replayed (--cuda-graphs; bench/sleap_infer_bench.py's): one launch
	instead of the hundred or so of its operations. With fp16, the autocast
	is inside the graph, with its weight cache off, as recording needs.
	max_abs_diff: the largest difference from the forward pass run without a
	graph, on the inputs recorded.

	Memory: a graph keeps the GPU memory its operations used while it was
	recorded, for replays to use again. The instance model gets a graph for
	each count of animals found, up to cameras x --max-instances (15 at 3
	cameras, 40 at 8), and in the benchmark 28 graphs with a pool each held
	8.1 GB of an RTX 6000 Ada's memory. So all of a model's graphs record into
	one pool (SHARE_POOL), as the benchmark's do (1.4 GB): the graphs run one
	at a time, on one stream, so their working memory can be the same.

	The output: each graph writes its output into a tensor of its own, made
	before the recording and so outside the shared pool, and returns it,
	valid until that graph's next call. In the pool, a graph recorded later
	could have been given, for its output, memory that an earlier graph works
	in, and that graph's next replay would overwrite it, even though only the
	count that came next was replayed. Writing the output is the cast to
	float32 the layers want, done anyway with fp16, so it adds no work there.
	"""

	SHARE_POOL = True # False: a pool per graph, as before (to measure)

	def __new__(cls, inner, fp16):
		import torch

		class Graphed(torch.nn.Module):
			def __init__(self):
				super().__init__()
				self.inner = inner
				self.fp16 = fp16
				self.graphs = {} # (shape, dtype, strides) -> (graph, input, output)
				self.pool = torch.cuda.graph_pool_handle() if cls.SHARE_POOL else None
				self.max_abs_diff = 0.0

			def Raw(self, x):
				with torch.autocast(device_type="cuda", dtype=torch.float16, enabled=self.fp16, cache_enabled=False):
					return self.inner(x)

			def Eager(self, x):
				return self.Raw(x).float()

			def Pools(self):
				# the memory pools the graphs recorded into
				return {self.pool} if self.pool is not None else {g.pool() for g, _, _ in self.graphs.values()}

			def Capture(self, x):
				static_in = x.clone()
				side = FixedStream("capture warm-up")
				side.wait_stream(torch.cuda.current_stream())
				with torch.cuda.stream(side):
					for _ in range(3): # cuDNN's and cuBLAS's first calls, outside the recording
						warm = self.Eager(static_in)
				torch.cuda.current_stream().wait_stream(side)
				static_out = torch.empty_like(warm) # outside the pool (see the class's doc)
				del warm
				graph = torch.cuda.CUDAGraph()
				# thread_local: the GPU clock's threads (GpuClock) may launch
				# meanwhile, on streams of their own (FixedStream)
				with torch.cuda.graph(graph, pool=self.pool, stream=FixedStream("capture"),
						capture_error_mode="thread_local"):
					static_out.copy_(self.Raw(static_in))
				graph.replay()
				diff = (static_out - self.Eager(static_in)).abs().max().item() if static_out.numel() else 0.0
				self.max_abs_diff = max(self.max_abs_diff, diff)
				return graph, static_in, static_out

			def Settle(self):
				# After the warm-up: a layer faster as is for every shape goes
				# back into its model, as it was (this one costs some
				# microseconds a call)
				if not any(use for _, _, use in self.timed.values()):
					setattr(*self.place, self.inner)

			def forward(self, x):
				key = (tuple(x.shape), x.dtype, x.stride())
				if key not in self.graphs:
					self.graphs[key] = self.Capture(x)
				graph, static_in, static_out = self.graphs[key]
				static_in.copy_(x)
				graph.replay()
				return static_out

		return Graphed()


def FastUpsample(args, device, memoryFormat, engine=None):
	# --fast-upsample: auto is on where it measured faster, models in nchw (on
	# ROCm, auto's there) on a GPU. Models in channels_last were faster as
	# they are, at every upsampling layer (RTX 6000 Ada). engine: the
	# networks' engine as chosen (FixedEngine.Pick or Choose), else --engine's
	if args.fast_upsample == "off" or not device.startswith("cuda"):
		return False
	if (engine or getattr(args, "engine", "eager")) not in ("eager", "off"):
		return False # a compiled engine fuses the upsampling itself, and can't compile this layer's timing
	return args.fast_upsample == "on" or memoryFormat == "nchw"


class LayoutUpsample:
	"""
	One of a model's bilinear upsampling layers (nn.Upsample), run in the
	model's memory layout or the other one, whichever is faster for each
	shape of input (--fast-upsample). The other layout: the input converted
	to it, upsampled there and converted back, the next layer getting the
	layout it always gets. PyTorch's upsampling kernel for NCHW gives each
	output pixel a GPU thread that loops over the batch and the channels, so
	on the small, deep layers (5x5 pixels by 768 channels) a few hundred
	threads do all the work and the GPU waits: on the EVO-X2 those layers took
	1.45 and 0.98 ms of a set's 9.9 ms of instance model; its channels_last
	kernel spreads the channels over threads, but is slower than NCHW's on
	the big, shallow layers. So each layer times both, the first time it gets
	an input of a shape, while the model warms up (measuring), outside any
	CUDA graph being recorded: the graph then records the faster one. A shape
	first seen later runs as sleap-nn has it. The results are the same:
	each output pixel is the same sum of the same four inputs.
	"""

	MARGIN = 0.9 # the other layout only if it takes at most this much of the time
	REPEATS, ROUNDS = 10, 5

	@staticmethod
	def Into(model):
		# Each bilinear nn.Upsample in model replaced by one of these
		import torch
		made = []
		for name, module in list(model.named_modules()):
			for child, inner in list(module.named_children()):
				if type(inner) is torch.nn.Upsample and inner.mode == "bilinear":
					up = LayoutUpsample(inner, module, child)
					setattr(module, child, up)
					made.append(up)
		return made

	def __new__(cls, inner, parent, name):
		import torch
		import torch.nn.functional as F

		class Upsample(torch.nn.Module):
			def __init__(self):
				super().__init__()
				self.inner = inner
				self.place = (parent, name)
				self.measuring = False
				self.timed = {} # input key -> (ms as is, ms other layout, use the other)
				self.otherName = None

			def Other(self, x):
				cl = x.is_contiguous(memory_format=torch.channels_last) and not x.is_contiguous()
				into, back = (torch.contiguous_format, torch.channels_last) if cl else \
					(torch.channels_last, torch.contiguous_format)
				y = F.interpolate(x.contiguous(memory_format=into), scale_factor=inner.scale_factor,
					mode=inner.mode, align_corners=inner.align_corners)
				return y.contiguous(memory_format=back)

			def Time(self, x):
				# GPU ms per call, as is and in the other layout: the best of
				# ROUNDS of REPEATS calls each, taking turns
				best = [float("inf"), float("inf")]
				ways = (self.inner, self.Other)
				for way in ways:
					way(x)
				for _ in range(cls.ROUNDS):
					for i, way in enumerate(ways):
						start, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
						start.record()
						for _ in range(cls.REPEATS):
							way(x)
						end.record()
						end.synchronize()
						best[i] = min(best[i], start.elapsed_time(end) / cls.REPEATS)
				return best

			def Settle(self):
				# After the warm-up: a layer faster as is for every shape goes
				# back into its model, as it was (this one costs some
				# microseconds a call)
				if not any(use for _, _, use in self.timed.values()):
					setattr(*self.place, self.inner)

			def forward(self, x):
				key = (tuple(x.shape), x.dtype, x.stride(), torch.is_autocast_enabled())
				timed = self.timed.get(key)
				if timed is None:
					if not self.measuring or torch.cuda.is_current_stream_capturing():
						return self.inner(x)
					if self.otherName is None:
						self.otherName = "nchw" if x.is_contiguous(memory_format=torch.channels_last) and \
							not x.is_contiguous() else "channels_last"
					asis, other = self.Time(x)
					timed = self.timed[key] = (asis, other, other <= asis * cls.MARGIN)
				return self.Other(x) if timed[2] else self.inner(x)

		return Upsample()


def PoolBytes(pools):
	"""
	The GPU memory torch holds in these graph memory pools, in bytes (from
	torch.cuda.memory_snapshot()); None if it can't say.
	"""
	import torch
	pools = {tuple(p) for p in pools}
	try:
		return sum(s["total_size"] for s in torch.cuda.memory_snapshot() if tuple(s["segment_pool_id"]) in pools)
	except (KeyError, TypeError, RuntimeError):
		return None


class FixedCrops:
	"""
	--fixed-crops: sleap-nn's TopDownLayer done again with no shape that
	depends on the data, so the GPU runs a whole set without the CPU waiting
	for it in the middle, and its results come back in one copy.

	sleap-nn's layer finds the centroids (nonzero), brings their count to
	the CPU, and only then crops that many animals and runs the instance
	model on them: the CPU waits for the centroid model before it can
	launch the rest, and --cuda-graphs needs a graph per count. Here every
	frame gives max_instances crops, frames x max_instances in all, the
	missing animals' crops padded (cut at the frame's corner, their
	keypoints thrown away), and the peaks are found with fixed shapes:

	- centroids: the confidence map's local maxima above the threshold
	  (sleap-nn's 8-neighbour test, or with tiedPeaks campy/peaks.py's,
	  which keeps a top of two equal pixels once, as campy-live's layer), the max_instances most confident by
	  topk, in sleap-nn's order (by position, or by confidence when there
	  were more than max_instances), and a mask of the real ones;
	- keypoints: sleap-nn's global peak per node (find_global_peaks_rough
	  has fixed shapes already);
	- integral refinement on every peak, real or not, with the same 5x5
	  patches and the same arithmetic as sleap-nn, so the same sub-pixel
	  result; the padded ones are dropped after.

	The crops, the coordinates and their order follow sleap-nn's own code
	step by step (make_centered_bboxes, crop_bboxes, the coordinate ladder),
	so the results are those of the layer: the same animals found, the same
	keypoints, up to the instance model's own rounding (its batch is now
	always frames x max_instances).

	Everything goes into one tensor (per frame, per animal: centroid x, y,
	its confidence, found, then keypoints x, y and their confidences), which
	comes back into pinned memory in one non_blocking copy and one wait.

	With --cuda-graphs (CUDA, ROCm), all of it, from the frames' upload
	buffer to that tensor, is ONE graph, recorded once (it has one shape):
	the frames are uploaded into its input, it is replayed, its output is
	copied back. Its memory is one pool; its output is outside the pool
	(as CUDAGraphed's). Without --cuda-graphs, the same runs eagerly: no
	wait in the middle, but every operation still launched one by one.

	Not supported: sleap-nn's centroid NMS, ground-truth centroids or
	keypoints, multi-class models, and refinement other than integral or
	none. The frames' sizematcher (max_height, max_width) is the same for
	every frame, so it is a number here: the trained models' 1024x1280 for
	1024x1280 frames is none.
	"""

	def __init__(self, layer, n_frames, device, instances, graph=False, tiedPeaks=None, engine="eager", cache=""):
		import torch
		from campy import peaks
		self.torch, self.layer, self.device = torch, layer, device
		# None: as sleap-nn's peak finding in this process (campy-live's layer)
		self.tiedPeaks = peaks.TiedPeaksInUse() if tiedPeaks is None else tiedPeaks
		self.c, self.ci = layer.centroid_layer, layer.centered_instance_layer
		if getattr(self.c, "use_gt_centroids", False) or getattr(self.ci, "use_gt_peaks", False) or \
				getattr(layer, "centroid_nms", False) or type(layer).__name__ != "TopDownLayer":
			raise SystemExit("--fixed-crops: a plain sleap-nn top-down model only (no centroid NMS, ground truth "
				"or classes)")
		for part in (self.c, self.ci):
			if part.postprocess_config.effective_refinement not in (None, "integral"):
				raise SystemExit("--fixed-crops: integral refinement or none, not {}".format(
					part.postprocess_config.refinement))
		self.B = n_frames
		self.K = int(self.c.postprocess_config.max_instances or self.c.max_instances or instances)
		self.cropH, self.cropW = (int(v) for v in layer.crop_size)
		self.cuda = str(device).startswith("cuda")
		if graph and not self.cuda:
			raise SystemExit("--cuda-graphs: CUDA (or ROCm) only, not {}".format(device))
		self.graph = None if graph else False # None: to record at the first set
		self.graphWanted = graph
		self.static_in = self.static_out = self.pool = None
		self.host = self.done = None
		self.max_abs_diff = 0.0
		self.nodes = None
		self.engine = FixedEngine.Choose(engine, device)
		self.cache = cache
		self.engines = {} # id(layer): FixedEngine, made at its first input

	# ------------------------------------------------ the pipeline, fixed shapes

	def Preprocess(self, layer, x):
		"""
		sleap-nn's InferenceLayer preprocessing (channels, sizematcher, input
		scale, padding to the stride, the n_samples axis) on a (N, C, H, W)
		batch; the sizematcher on the whole batch at once (its frames are all
		the same size), its scale a number. Returns (x, sizematcher scale).
		"""
		from sleap_nn.data.normalization import convert_to_grayscale, convert_to_rgb
		from sleap_nn.data.resizing import apply_pad_to_stride, apply_sizematcher, resize_image
		cfg = layer.preprocess_config
		if cfg.ensure_rgb and x.shape[-3] != 3:
			x = convert_to_rgb(x)
		elif cfg.ensure_grayscale and x.shape[-3] != 1:
			x = convert_to_grayscale(x)
		eff = 1.0
		if cfg.max_height is not None or cfg.max_width is not None:
			x, eff = apply_sizematcher(x, cfg.max_height, cfg.max_width)
		if cfg.scale != 1.0:
			x = resize_image(x, cfg.scale)
		if layer.max_stride != 1:
			x = apply_pad_to_stride(x, layer.max_stride)
		return x.unsqueeze(1), float(eff)

	def Confmaps(self, layer, x):
		# The model, as sleap-nn's TorchBackend runs it, with autocast's weight
		# cache off (a graph can't record it)
		torch = self.torch
		backend = layer.backend
		if self.engine != "eager":
			if id(layer) not in self.engines:
				self.engines[id(layer)] = FixedEngine(self.engine, backend, x, bool(backend.use_fp16 and self.cuda),
					self.cache, "centroid" if layer is self.c else "instance")
			return self.engines[id(layer)](x)
		with torch.autocast(device_type="cuda", dtype=torch.float16, enabled=bool(backend.use_fp16 and self.cuda),
				cache_enabled=False):
			out = backend.model(x)
		if isinstance(out, dict):
			out = layer._extract_confmaps(out)
		return out.float()

	def Integral(self, maps, sample, x, y, patch):
		"""
		sleap-nn's integral refinement (find_*_peaks with refinement
		"integral") of peaks at whole pixels x, y (long, any shape) of
		maps[sample] (maps: (S, 1, h, w)), all of them: the offsets, (..., 2).
		The patch and the sums are sleap-nn's (make_centered_bboxes,
		crop_bboxes, integral_regression), done the same way.
		"""
		torch = self.torch
		F = torch.nn.functional
		shape = x.shape
		x, y, sample = x.reshape(-1), y.reshape(-1), sample.reshape(-1)
		half = patch // 2
		# make_centered_bboxes' top-left, then crop_bboxes' trunc(top-left + half) - half
		left = ((x.float() - patch / 2 + 0.5) + half).long() - half
		top = ((y.float() - patch / 2 + 0.5) + half).long() - half
		padded = F.pad(maps, (patch, patch, patch, patch), mode="constant", value=0)
		r = torch.arange(patch, device=maps.device)
		ax = (left[:, None, None] + r[None, None, :] + patch).clamp(0, padded.shape[-1] - 1)
		ay = (top[:, None, None] + r[None, :, None] + patch).clamp(0, padded.shape[-2] - 1)
		cms = padded[sample[:, None, None], 0, ay, ax].unsqueeze(1) # (n, 1, patch, patch)
		gv = torch.arange(patch, dtype=torch.float32, device=maps.device) - ((patch - 1) / 2)
		z = torch.sum(cms, dim=[2, 3])
		dx = torch.sum(gv.view(1, 1, 1, -1) * cms, dim=[2, 3]) / z
		dy = torch.sum(gv.view(1, 1, -1, 1) * cms, dim=[2, 3]) / z
		return torch.cat([dx, dy], dim=1).reshape(shape + (2,))

	def Centroids(self, cms, scale):
		"""
		CentroidLayer.postprocess with fixed shapes: (centroids (B, K, 2),
		their values (B, K), found (B, K)), in the input image's pixels (before
		the sizematcher's scale is undone), NaN where none.
		"""
		torch = self.torch
		from sleap_nn.inference.ops.peaks import morphological_dilation
		cfg = self.c.postprocess_config
		B, C, h, w = cms.shape
		if C != 1:
			raise SystemExit("--fixed-crops: a centroid model with one confidence map, not {}".format(C))
		K = self.K
		flat = cms.reshape(B, h * w)
		if self.tiedPeaks: # a top of two equal pixels kept once (campy/peaks.py), as the layer does
			from campy.peaks import PeakMask
			peak = PeakMask(cms, cfg.peak_threshold).reshape(B, h * w)
		else:
			around = morphological_dilation(cms.reshape(-1, 1, h, w), None).reshape(B, h * w)
			peak = (flat > around) & (flat > cfg.peak_threshold)
		vals, idx = torch.topk(torch.where(peak, flat, torch.full_like(flat, float("-inf"))), K, dim=1)
		found = vals > float("-inf")
		# sleap-nn keeps them in raster order, or, with more than K, the topk's
		raster = peak.sum(1, keepdim=True) <= K
		order = torch.where(found, idx, h * w + torch.arange(K, device=cms.device)).argsort(dim=1)
		idx = torch.where(raster, idx.gather(1, order), idx)
		vals = torch.where(raster, vals.gather(1, order), vals)
		found = torch.where(raster, found.gather(1, order), found)
		x, y = idx % w, torch.div(idx, w, rounding_mode="floor")
		peaks = torch.stack([x, y], dim=-1).to(torch.float32)
		if cfg.effective_refinement == "integral":
			sample = torch.arange(B, device=cms.device)[:, None].expand(B, K)
			peaks = peaks + self.Integral(cms, sample, x, y, cfg.integral_patch_size)
		peaks = peaks * self.c.output_stride if self.c.output_stride != 1 else peaks
		peaks = peaks / scale if scale != 1.0 else peaks
		nan = torch.full_like(peaks, float("nan"))
		return torch.where(found[..., None], peaks, nan), torch.where(found, vals, nan[..., 0]), found

	def Crops(self, image, centroids, found):
		# make_centered_bboxes and crop_bboxes, for every slot: (crops
		# (B*K, C, cropH, cropW), the boxes' top-left (B*K, 2) as sleap-nn adds it).
		# sleap-nn's crop_bboxes pads the frames with 0 and indexes them by
		# (frame, row, column); here each pixel's place in the unpadded frames
		# is one number, read with one gather, and 0 where it is outside them:
		# the same pixels, without writing the padded frames and with one index
		# per pixel rather than three (0.69 to 0.18 ms per set on the EVO-X2,
		# 0.44 to 0.12 ms on the RTX 2000 Ada: bench/live_stage_timing.py)
		torch = self.torch
		B, K = found.shape
		ch, cw = self.cropH, self.cropW
		C, h, w = image.shape[-3:]
		c = torch.where(found[..., None], centroids, torch.zeros_like(centroids)).reshape(B * K, 2)
		topleft = torch.stack([c[:, 0] - cw / 2, c[:, 1] - ch / 2], dim=-1) + 0.5
		corner = torch.stack([(topleft[:, 0] + cw // 2).to(torch.long) - cw // 2,
			(topleft[:, 1] + ch // 2).to(torch.long) - ch // 2], dim=-1)
		x = corner[:, 0:1, None] + torch.arange(cw, device=image.device)[None, None, :] # (B*K, 1, cw)
		y = corner[:, 1:2, None] + torch.arange(ch, device=image.device)[None, :, None] # (B*K, ch, 1)
		inside = (x >= 0) & (x < w) & (y >= 0) & (y < h)
		sample = torch.arange(B, device=image.device).repeat_interleave(K)[:, None, None]
		at = sample * (h * w) + y.clamp(0, h - 1) * w + x.clamp(0, w - 1) # (B*K, ch, cw)
		if C == 1:
			crops = image.reshape(-1)[at]
			crops = torch.where(inside, crops, torch.zeros_like(crops)).unsqueeze(1)
		else:
			crops = image.permute(0, 2, 3, 1).reshape(B * h * w, C)[at] # (B*K, ch, cw, C)
			crops = torch.where(inside[..., None], crops, torch.zeros_like(crops)).permute(0, 3, 1, 2).contiguous()
		return crops, topleft

	def Keypoints(self, cms):
		# CenteredInstanceLayer.postprocess: each node's global peak, refined
		torch = self.torch
		from sleap_nn.inference.ops.peaks import find_global_peaks_rough
		cfg = self.ci.postprocess_config
		rough, vals = find_global_peaks_rough(cms, threshold=cfg.peak_threshold)
		peaks = rough
		if cfg.effective_refinement == "integral":
			N, C, h, w = cms.shape
			missing = torch.isnan(rough)
			at = torch.where(missing, torch.zeros_like(rough), rough).to(torch.long)
			sample = torch.arange(N * C, device=cms.device).view(N, C)
			offsets = self.Integral(cms.reshape(N * C, 1, h, w), sample, at[..., 0], at[..., 1],
				cfg.integral_patch_size)
			peaks = torch.where(missing, rough, rough + offsets)
		peaks = peaks * self.ci.output_stride if self.ci.output_stride != 1 else peaks
		scale = self.ci.preprocess_config.scale
		return (peaks / scale if scale != 1.0 else peaks), vals

	def Pipeline(self, frames):
		"""
		The whole set, frames (B, H, W, C) or (B, C, H, W) on the device, to
		one tensor (B, K, 4 + 3 x nodes): centroid x, y, its value, found (1
		or 0), keypoints x, y per node, peak value per node. NaN where none.
		"""
		torch = self.torch
		raw = self.c._to_4d_tensor(frames)
		x, eff = self.Preprocess(self.c, raw)
		centroids, cvals, found = self.Centroids(self.Confmaps(self.c, x), self.c.preprocess_config.scale)
		# Crops from the sized frames (TopDownLayer's sizematcher, without
		# the centroid layer's channel change), at the centroids there
		sized = raw
		cfg = self.c.preprocess_config
		if cfg.max_height is not None or cfg.max_width is not None:
			from sleap_nn.data.resizing import apply_sizematcher
			sized, _ = apply_sizematcher(raw, cfg.max_height, cfg.max_width)
		if eff != 1.0:
			centroids = centroids / eff # undo_eff_scale, then TopDownLayer's * eff
			crops, topleft = self.Crops(sized, centroids * eff, found)
		else:
			crops, topleft = self.Crops(sized, centroids, found)
		xi, _ = self.Preprocess(self.ci, crops)
		keypoints, vals = self.Keypoints(self.Confmaps(self.ci, xi))
		keypoints = keypoints + topleft.view(-1, 1, 2) # add_crop_offset
		if eff != 1.0:
			keypoints = keypoints / eff
		B, K = found.shape
		n = keypoints.shape[1]
		keypoints = torch.where(found.view(B * K, 1, 1), keypoints, torch.full_like(keypoints, float("nan")))
		vals = torch.where(found.view(B * K, 1), vals, torch.full_like(vals, float("nan")))
		return torch.cat([centroids, cvals[..., None], found[..., None].to(torch.float32),
			keypoints.reshape(B, K, 2 * n), vals.reshape(B, K, n)], dim=-1)

	# ------------------------------------------------ running it

	def Upload(self, host):
		if self.static_in is not None:
			self.static_in.copy_(host, non_blocking=True)
			return self.static_in
		return host.to(self.device, non_blocking=True)

	def Record(self, frames):
		# The graph, recorded once, as CUDAGraphed.Capture records a model's
		torch = self.torch
		self.static_in = torch.empty_like(frames)
		self.static_in.copy_(frames)
		side = FixedStream("capture warm-up")
		side.wait_stream(torch.cuda.current_stream())
		with torch.cuda.stream(side), torch.inference_mode():
			for _ in range(3): # cuDNN's (MIOpen's) and cuBLAS's first calls, outside the recording
				warm = self.Pipeline(self.static_in)
		torch.cuda.current_stream().wait_stream(side)
		self.static_out = torch.empty_like(warm) # outside the pool
		del warm
		self.pool = torch.cuda.graph_pool_handle()
		graph = torch.cuda.CUDAGraph()
		with torch.cuda.graph(graph, pool=self.pool, stream=FixedStream("capture"), capture_error_mode="thread_local"), \
				torch.inference_mode():
			self.static_out.copy_(self.Pipeline(self.static_in))
		graph.replay()
		with torch.inference_mode():
			self.max_abs_diff = max(self.max_abs_diff, MaxAbsDiff(self.static_out, self.Pipeline(self.static_in)))
		self.graph = graph

	def Run(self, frames):
		# The set's tensor, on the device
		torch = self.torch
		if self.graph is None:
			self.Record(frames)
		if self.graph:
			if frames is not self.static_in:
				self.static_in.copy_(frames, non_blocking=True)
			self.graph.replay()
			return self.static_out
		with torch.inference_mode():
			return self.Pipeline(frames)

	def Predict(self, frames):
		torch = self.torch
		out = self.Run(frames)
		if self.cuda:
			# One copy back, into pinned memory, and one wait
			if self.host is None:
				self.host = torch.empty(out.shape, dtype=out.dtype, pin_memory=True)
				self.done = torch.cuda.Event()
			self.host.copy_(out, non_blocking=True)
			self.done.record()
			self.done.synchronize()
			a = self.host.numpy()
		else:
			a = out.cpu().numpy()
		n = (a.shape[-1] - 4) // 3
		self.nodes = n
		B, K = a.shape[:2]
		return {"centroids": a[..., 0:2].copy(), "centroid_vals": a[..., 2].copy(), "found_mask": a[..., 3] > 0.5,
			"keypoints": a[..., 4:4 + 2 * n].reshape(B, K, n, 2).copy(), "peak_vals": a[..., 4 + 2 * n:].copy()}

	def Plain(self):
		# The networks in torch from now on (--engine auto, when an engine
		# failed while warming up): nothing recorded or made yet
		self.engine, self.engines = "eager", {}
		self.graph = None if self.graphWanted else False
		self.static_in = self.static_out = self.pool = None
		self.host = self.done = None
		self.max_abs_diff = 0.0

	def Describe(self):
		engines = "" if not self.engines else "; networks: " + ", ".join(e.text for e in self.engines.values())
		if not self.graph:
			return "fixed crops: {} per set, without a graph{}".format(self.B * self.K, engines)
		pooled = PoolBytes({self.pool})
		return "fixed crops: one CUDA graph for the whole set ({} crops){}; its output {} (largest difference " \
			"{:g}){}".format(self.B * self.K, "" if pooled is None else ", holding {:.0f} MB of GPU memory".format(
			pooled / 1e6), "the same as without" if self.max_abs_diff == 0 else "differs from without",
			self.max_abs_diff, engines)


class FixedEngine:
	"""
	--engine: one of FixedCrops' two networks as a compiled engine, made for
	the one input it will ever get (FixedCrops' shapes never change), on its
	first call. Called as the model is (the preprocessed batch, (N, 1, C, H,
	W), uint8), it gives the confidence maps, float32, on the device.

	compile: torch.compile of the model's forward (sleap-nn's Lightning
	module: normalization, backbone, head), fp16 autocast as campy-live's
	default, no dynamic shapes, one whole graph (fullgraph: a break would
	stop with its reason rather than quietly run part of it in torch).
	inductor's kernels are plain launches, so --cuda-graphs records them.

	tensorrt: the backbone and head exported to ONNX at that shape (in fp16
	for --precision fp16: TensorRT 10 and later build strongly typed
	networks, so the ONNX's types are the engine's), built into a plan, kept
	in --engine-cache under a hash of the weights, the shape, the precision,
	TensorRT's version and the GPU, and read from there the next time. It
	reads its input from, and writes its output to, torch tensors' memory, on
	torch's current stream, so --cuda-graphs records it too (TensorRT allows
	its enqueue in a graph once it has run once, which the warm-up does).
	The normalization (/ 255) stays in torch, before it.
	"""

	HEADS = ("CentroidConfmapsHead", "CenteredInstanceConfmapsHead")

	TRITON = "Triton (Linux: it comes with PyTorch; Windows: pip install triton-windows, the version that " \
		"matches your PyTorch: docs/live-tracking.md)"

	@staticmethod
	def Pick(device):
		"""
		--engine auto's choice on this device: (kind, None), kind tensorrt
		where the GPU is NVIDIA's and tensorrt and onnx are installed, else
		compile where Triton is; or ("eager", why not) with a GPU and
		neither; ("eager", None) without a GPU.
		"""
		import importlib.util
		if not str(device).startswith("cuda"):
			return "eager", None
		import torch
		nvidia = not getattr(torch.version, "hip", None)
		if nvidia and importlib.util.find_spec("tensorrt") and importlib.util.find_spec("onnx"):
			return "tensorrt", None
		if importlib.util.find_spec("triton"):
			return "compile", None
		if nvidia:
			return "eager", "neither TensorRT (pip install tensorrt onnx) nor {} is installed".format(FixedEngine.TRITON)
		return "eager", "{} isn't installed".format(FixedEngine.TRITON)

	@staticmethod
	def Choose(engine, device):
		# eager, compile or tensorrt, for --engine on this device; one that
		# can't be had here stops campy-live (auto: Pick's, or eager)
		import importlib.util
		if engine in ("eager", "off", "", None):
			return "eager"
		if engine == "auto":
			return FixedEngine.Pick(device)[0]
		import torch
		if not str(device).startswith("cuda"):
			raise SystemExit("--engine {}: CUDA or ROCm only, not {}".format(engine, device))
		nvidia = not getattr(torch.version, "hip", None)
		if engine == "on":
			kind, why = FixedEngine.Pick(device)
			if kind == "eager":
				raise SystemExit("--engine on: {}".format(why))
			return kind
		if engine == "tensorrt":
			if not nvidia:
				raise SystemExit("--engine tensorrt: NVIDIA GPUs only (on ROCm, --engine compile)")
			for module in ("tensorrt", "onnx"):
				if not importlib.util.find_spec(module):
					raise SystemExit("--engine tensorrt needs the {0} package (pip install {0})".format(module))
		elif engine == "compile":
			if not importlib.util.find_spec("triton"):
				raise SystemExit("--engine compile needs {}".format(FixedEngine.TRITON))
		else:
			raise SystemExit("--engine: auto, on, off, compile or tensorrt, not {}".format(engine))
		return engine

	def __init__(self, kind, backend, x, fp16, cache, name):
		import torch
		self.torch, self.kind, self.fp16, self.name = torch, kind, fp16, name
		self.shape = tuple(x.shape)
		t = time.perf_counter()
		if kind == "compile":
			self.Compile(backend.model, cache)
		else:
			self.TensorRT(backend.model, cache, x)
		self.text = "{} {} {} {}".format(name, {"compile": "torch.compile", "tensorrt": "TensorRT"}[kind],
			"fp16" if fp16 else "fp32", "x".join(str(v) for v in self.shape[:1] + self.shape[2:]))
		self.seconds = time.perf_counter() - t

	@staticmethod
	def PlainConfigs(model):
		# sleap-nn's Model.forward reads backbone_config.in_channels from an
		# OmegaConf config, which dynamo can't trace (typed configs, as the
		# random model's, fail in OmegaConf's own type handling): a plain
		# copy of the same values instead
		import types
		try:
			from omegaconf import DictConfig, OmegaConf
		except ImportError:
			return

		def Plain(v):
			return types.SimpleNamespace(**{k: Plain(x) for k, x in v.items()}) if isinstance(v, dict) else v
		for module in model.modules():
			cfg = getattr(module, "backbone_config", None)
			if isinstance(cfg, DictConfig):
				module.backbone_config = Plain(OmegaConf.to_container(cfg, resolve=True))

	@staticmethod
	def CacheDir(cache):
		# --engine-cache, or ~/.cache/campy/engines: TensorRT's plans, and
		# torch.compile's kernels in its inductor folder
		return os.path.expanduser(cache or os.path.join("~", ".cache", "campy", "engines"))

	PLAN_FORMAT = 2 # FixedEngine's ONNX export, as the plan names count it: a change here makes new plans

	@staticmethod
	def PlanName(name, state, shape, fp16, trtVersion, gpu):
		# A plan's file name: the network (its state_dict's names and
		# values), the input's shape, the precision, TensorRT's and PyTorch's
		# versions (PyTorch exports the ONNX), the GPU and PLAN_FORMAT. Another
		# model, campy's export or package version is another file, built anew
		import hashlib, torch
		digest = hashlib.sha1()
		for key, value in state.items():
			digest.update(key.encode())
			digest.update(value.detach().cpu().contiguous().numpy().tobytes())
		digest.update("{} {} {} {} {} {}".format(tuple(shape), fp16, trtVersion, gpu, torch.__version__,
			FixedEngine.PLAN_FORMAT).encode())
		return "{}_{}".format(name, digest.hexdigest()[:16])

	def Compile(self, model, cache):
		torch = self.torch
		fp16 = self.fp16
		self.PlainConfigs(model)
		# inductor's own cache is the temp folder's torchinductor_<user>,
		# which Linux empties at each restart (about 15 s more to start on the
		# EVO-X2): kept with the TensorRT plans instead, unless campy-live was
		# started with TORCHINDUCTOR_CACHE_DIR set. inductor reads it at each
		# compile. Its keys are the traced graph, PyTorch's version and the
		# GPU (the weights are inputs), so another model or update compiles anew
		if not _INDUCTOR_DIR:
			os.environ["TORCHINDUCTOR_CACHE_DIR"] = os.path.join(self.CacheDir(cache), "inductor")
		if self.name == "centroid":
			print("campy-live: compiling the networks with torch.compile (their kernels kept in {}: up to about "
				"40 s the first time, a few seconds after)".format(os.environ["TORCHINDUCTOR_CACHE_DIR"]), flush=True)

		def run(x):
			with torch.autocast(device_type="cuda", dtype=torch.float16, enabled=fp16, cache_enabled=False):
				out = model(x)
			if isinstance(out, dict):
				raise RuntimeError("--engine compile: the model gives a dict, not one confidence map")
			return out.float()
		self.fn = torch.compile(run, dynamic=False, fullgraph=True)

	def TensorRT(self, lightning, cache, x):
		import copy, warnings
		import tensorrt as trt
		torch = self.torch
		body = getattr(lightning, "model", None)
		if body is None:
			raise SystemExit("--engine tensorrt: a trained sleap-nn model only (--model sleap), not {}".format(
				type(lightning).__name__))
		n, _, c, h, w = self.shape
		device = x.device
		with torch.inference_mode(False), torch.no_grad():
			probe = body(torch.zeros((1, c, h, w), device=device))
			heads = [k for k in probe if k in self.HEADS] if isinstance(probe, dict) else []
			if len(heads) != 1:
				raise SystemExit("--engine tensorrt: no confidence-map head in the {} model".format(self.name))
			head = heads[0]
			fp16 = self.fp16

			class Net(torch.nn.Module):
				# backbone + head: the normalized image in, float32 confidence maps out
				def __init__(self):
					super().__init__()
					self.body = copy.deepcopy(body).to(memory_format=torch.contiguous_format)
					if fp16:
						self.body = self.body.half()

				def forward(self, x):
					return self.body(x.half() if fp16 else x)[head].float()
			net = Net().eval()
			cache = self.CacheDir(cache)
			os.makedirs(cache, exist_ok=True)
			stem = os.path.join(cache, self.PlanName(self.name, net.state_dict(), self.shape, fp16, trt.__version__,
				torch.cuda.get_device_name(device)))
			logger = trt.Logger(trt.Logger.WARNING)
			runtime = trt.Runtime(logger)
			if os.path.exists(stem + ".plan"):
				with open(stem + ".plan", "rb") as f:
					plan = f.read()
			else:
				print("campy-live: building the {} network's TensorRT engine ({}, {}), once (kept in "
					"{})".format(self.name, "x".join(str(v) for v in (n, c, h, w)), "fp16" if fp16 else "fp32",
					cache), flush=True)
				with warnings.catch_warnings():
					warnings.simplefilter("ignore")
					torch.onnx.export(net, (torch.zeros((n, c, h, w), device=device),), stem + ".onnx",
						input_names=["x"], output_names=["y"], opset_version=17, dynamo=False, do_constant_folding=True)
				flags = 0
				if hasattr(trt.NetworkDefinitionCreationFlag, "STRONGLY_TYPED"):
					flags |= 1 << int(trt.NetworkDefinitionCreationFlag.STRONGLY_TYPED)
				builder = trt.Builder(logger)
				network = builder.create_network(flags)
				parser = trt.OnnxParser(network, logger)
				if not parser.parse_from_file(stem + ".onnx"):
					raise SystemExit("--engine tensorrt: TensorRT can't read the {} network: {}".format(self.name,
						"; ".join(str(parser.get_error(i)) for i in range(parser.num_errors))))
				plan = builder.build_serialized_network(network, builder.create_builder_config())
				if plan is None:
					raise SystemExit("--engine tensorrt: TensorRT couldn't build the {} network".format(self.name))
				plan = bytes(plan)
				with open(stem + ".plan.part", "wb") as f:
					f.write(plan)
				os.replace(stem + ".plan.part", stem + ".plan")
				os.remove(stem + ".onnx")
			del net, probe
		self.runtime = runtime
		self.engine = runtime.deserialize_cuda_engine(plan)
		if self.engine is None:
			raise SystemExit("--engine tensorrt: {}.plan doesn't load (another TensorRT?); delete it".format(stem))
		self.ctx = self.engine.create_execution_context()
		self.out = torch.empty(tuple(self.engine.get_tensor_shape("y")), dtype=torch.float32, device=device)
		self.ctx.set_tensor_address("y", self.out.data_ptr())
		self.fn = self.Enqueue

	def Enqueue(self, x):
		# sleap-nn's normalize_on_gpu for uint8 (float32 / 255), then the
		# engine on torch's stream; its output buffer is the same every call
		torch = self.torch
		x = x.squeeze(1)
		if torch.is_floating_point(x):
			from sleap_nn.data.normalization import normalize_on_gpu
			x = normalize_on_gpu(x)
		else:
			x = x.float() / 255.0
		x = x.contiguous()
		self.ctx.set_tensor_address("x", x.data_ptr())
		if not self.ctx.execute_async_v3(torch.cuda.current_stream().cuda_stream):
			raise RuntimeError("TensorRT: the {} network didn't run".format(self.name))
		return self.out

	def __call__(self, x):
		if tuple(x.shape) != self.shape:
			raise RuntimeError("--engine: the {} network was made for {}, not {}".format(self.name, self.shape,
				tuple(x.shape)))
		return self.fn(x)


def MaxAbsDiff(a, b):
	# The largest difference between two tensors with NaNs; inf if the NaNs differ
	import torch
	na, nb = torch.isnan(a), torch.isnan(b)
	if not torch.equal(na, nb):
		return float("inf")
	d = (a - b).abs()[~na]
	return float(d.max().item()) if d.numel() else 0.0


_STREAMS, _STREAMS_LOCK = {}, threading.Lock()


def FixedStream(name, priority=0):
	"""
	One CUDA stream per name (and device), made the first time and kept:
	never torch.cuda.Stream() per use. torch hands out its pool of 32 streams
	per priority in turn, so streams made per graph and per GpuClock.Start
	came round to the same one: a clock spinner's stream was the stream a
	graph was being recorded on, its event sync failed
	(CUDA_ERROR_CAPTURED_EVENT) and the recording with it. The streams kept
	here are also made distinct from each other.
	"""
	import torch
	key = (name, priority, torch.cuda.current_device())
	with _STREAMS_LOCK:
		if key not in _STREAMS:
			taken = {s.cuda_stream for s in _STREAMS.values()}
			for _ in range(64): # a pool's worth, twice
				stream = torch.cuda.Stream(priority=priority)
				if stream.cuda_stream not in taken:
					break
			_STREAMS[key] = stream
		return _STREAMS[key]


class GpuClock:
	"""
	--hold-gpu-clock (see the module's doc): blocks of the GPU spinning, on
	streams of the lowest priority, from Start to Stop, so that an NVIDIA
	GPU keeps its full clock. While no set has come for WAITING_S (before
	the recording's first frames), WAITING_BLOCKS spin: with one alone, an
	RTX 2000 Ada still judged itself idle and fell to 1000 MHz, and the first
	second's sets took 20 ms instead of 13. While sets come (Busy), blocks
	spin (--hold-gpu-clock-blocks): a number, or auto, from fewest to
	MOST_BUSY as the GPU's clock needs (Adjust), read by a ClockReader:
	fewest is 1 on an AMD integrated GPU (Fewest), else 0. Each spinner's
	thread waits on an event that lets it sleep, so they cost no CPU core.
	Each spins on a stream of its own, the same at every Start (FixedStream).
	"""

	CYCLES = 5000000 # one spin: 2 ms at 2.5 GHz, 24 ms at the idle clock
	WAITING_BLOCKS, WAITING_S = 4, 0.5
	# auto, while sets come: at most 2 (in the benchmark, an RTX 2000 Ada that
	# had fallen into its slow clock reached 1635 MHz with 1, 2295 with 2, and
	# 8 left the model waiting, 37 ms a set instead of 12); 1 to begin with
	MOST_BUSY, FIRST_BUSY = 2, 1
	LOOK_S = 0.5 # how often auto reads the clock
	LOW = 0.9 # a clock below this share of the fastest seen is too slow
	LOW_LOOKS = 2 # this many slow looks in a row: one block more
	CALM_S = 10.0 # at full clock this long: one block fewer...
	MOST_CALM_S = 320.0 # ...or twice as long as last time, up to this, when the last one fewer was too few

	def __init__(self, device, blocks="auto", reader=None, fewest=None):
		import torch
		self.torch, self.device = torch, device
		self.stop = threading.Event()
		self.threads = []
		self.spins = 0
		self.lastSet = 0.0 # time.monotonic() of the last set (Busy)
		self.auto = blocks == "auto"
		self.reader = (reader or ClockReader.For(device)) if self.auto else None
		# auto's fewest while sets come; without the clock to read, it spins FIRST_BUSY
		self.fewest = (self.Fewest(torch, device) if fewest is None else fewest) if self.auto else 0
		self.busy = max(self.FIRST_BUSY, self.fewest) if self.auto else int(blocks)
		self.fastest = self.mhz = 0.0 # MHz
		self.clocks = [] # MHz, read while sets came
		self.time = {} # blocks spinning while sets came: seconds
		self.changes = 0
		self.calm = self.CALM_S

	@staticmethod
	def Fewest(torch, device):
		# 1 on an AMD integrated GPU (ROCm, is_integrated: the EVO-X2's
		# Radeon 8060S): with no block spinning between sets, the chip, CPU
		# and GPU, slows down though its GPU clock reads high (see the
		# module's doc). 0 elsewhere: NVIDIA GPUs and AMD's own cards
		try:
			integrated = torch.cuda.get_device_properties(torch.device(device)).is_integrated
		except Exception:
			return 0
		return 1 if getattr(torch.version, "hip", None) and integrated else 0

	def Busy(self):
		self.lastSet = time.monotonic()

	def Waiting(self):
		return time.monotonic() - self.lastSet >= self.WAITING_S

	def Start(self):
		if not self.threads:
			self.stop.clear()
			n = max(self.WAITING_BLOCKS, self.MOST_BUSY if self.auto else self.busy)
			with self.torch.cuda.device(self.device):
				streams = [FixedStream("gpu clock {}".format(i)) for i in range(n)] # 0: the lowest priority
			self.threads = [threading.Thread(target=self.Run, args=(i, streams[i]), daemon=True,
				name="campy-live GPU clock {}".format(i)) for i in range(n)]
			if self.reader is not None:
				self.threads.append(threading.Thread(target=self.Watch, daemon=True, name="campy-live GPU clock reader"))
			for thread in self.threads:
				thread.start()

	def Run(self, i, stream):
		torch = self.torch
		with torch.cuda.device(self.device):
			done = torch.cuda.Event(blocking=True)
			while not self.stop.is_set():
				if i >= (self.WAITING_BLOCKS if self.Waiting() else self.busy):
					self.stop.wait(0.05)
					continue
				with torch.cuda.stream(stream):
					torch.cuda._sleep(self.CYCLES)
				done.record(stream)
				done.synchronize()
				self.spins += 1

	def Watch(self):
		low, calmSince, fewerAt = 0, time.monotonic(), None
		while not self.stop.wait(self.LOOK_S):
			look = self.reader.Read()
			if look is None:
				continue
			mhz, limited = look
			self.fastest = max(self.fastest, mhz)
			if self.Waiting():
				low, calmSince = 0, time.monotonic()
				continue
			self.clocks.append(mhz)
			self.mhz = mhz
			self.time[self.busy] = self.time.get(self.busy, 0.0) + self.LOOK_S
			low, calmSince, fewerAt = self.Adjust(mhz, limited, low, calmSince, fewerAt, time.monotonic())

	def Adjust(self, mhz, limited, low, calmSince, fewerAt, now):
		"""
		One look at the clock while sets come: LOW_LOOKS in a row below LOW
		of the fastest seen (which the WAITING_BLOCKS reach), one block more;
		calm (at full clock) for self.calm, one fewer. A clock held down by
		the GPU's power or heat (limited) is not raised: more blocks would
		only take power from the model. When one fewer turned out too few
		within self.calm, the next try waits twice as long.
		"""
		if mhz < self.LOW * self.fastest and not limited:
			low += 1
			if low >= self.LOW_LOOKS and self.busy < self.MOST_BUSY:
				self.busy += 1
				self.changes += 1
				if fewerAt is not None and now - fewerAt < self.calm:
					self.calm = min(2 * self.calm, self.MOST_CALM_S)
				return 0, now, None
			return low, now, fewerAt
		if now - calmSince >= self.calm and self.busy > self.fewest:
			self.busy -= 1
			self.changes += 1
			return 0, now, now
		return 0, calmSince, fewerAt

	def Now(self):
		# for the progress line
		if not self.auto:
			return ""
		blocks = "{} block{} spinning".format(self.busy, "" if self.busy == 1 else "s")
		return "; GPU clock {:.0f} MHz, {}".format(self.mhz, blocks) if self.mhz else "; GPU clock not read, " + blocks

	def Describe(self):
		d = {"blocks_while_sets_come": "auto" if self.auto else self.busy}
		if self.fewest:
			d["fewest_blocks"] = self.fewest
		if self.auto:
			total = sum(self.time.values())
			d.update(clock_read_by=self.reader.name if self.reader else None,
				blocks_share={str(b): round(t / total, 3) for b, t in sorted(self.time.items())} if total else None,
				block_changes=self.changes)
			if self.clocks:
				d.update(clock_mhz_p50=float(np.median(self.clocks)), clock_mhz_min=float(min(self.clocks)),
					fastest_mhz=self.fastest)
		return d

	def Stop(self):
		if self.threads:
			self.stop.set()
			for thread in self.threads:
				thread.join()
			self.threads = []
			if self.reader is not None:
				self.reader.Close()


class ClockReader:
	"""
	The GPU's clock, for GpuClock's auto: Read() gives (MHz, limited) or
	None, limited when the GPU says its power or heat holds the clock down.
	NVIDIA: through NVML (the nvidia-ml-py package's pynvml) if it is
	installed, else from nvidia-smi, run once for the whole run, reading
	every half second. ROCm (amdgpu): the clock the driver's pp_dpm_sclk
	marks. For(device) gives None where none of them works.
	"""

	# NVML's clock event reasons that more blocks spinning would not lift:
	# SW power cap, HW slowdown, SW thermal, HW thermal, HW power brake
	LIMITED = 0x4 | 0x8 | 0x20 | 0x40 | 0x80
	name = None

	@staticmethod
	def For(device):
		import torch
		if not str(device).startswith("cuda"):
			return None
		if getattr(torch.version, "hip", None):
			kinds = [SysfsClock]
		else:
			kinds = [NvmlClock, SmiClock]
		for kind in kinds:
			try:
				reader = kind(torch, device)
			except Exception: # not installed, no driver, no such GPU: the next kind
				continue
			if reader.Read() is not None:
				return reader
			reader.Close()
		return None

	@staticmethod
	def Uuid(torch, device):
		# nvidia-smi's and NVML's numbering can differ from CUDA's: the GPU's UUID
		props = torch.cuda.get_device_properties(torch.device(device))
		uuid = str(getattr(props, "uuid", "") or "")
		if not uuid:
			raise RuntimeError("no UUID for {}".format(device))
		return uuid if uuid.startswith("GPU-") else "GPU-" + uuid

	def Close(self):
		pass


class NvmlClock(ClockReader):
	name = "NVML"

	def __init__(self, torch, device):
		import pynvml
		self.nvml = pynvml
		pynvml.nvmlInit()
		self.handle = pynvml.nvmlDeviceGetHandleByUUID(self.Uuid(torch, device))
		self.reasons = getattr(pynvml, "nvmlDeviceGetCurrentClocksEventReasons", None) or \
			pynvml.nvmlDeviceGetCurrentClocksThrottleReasons

	def Read(self):
		try:
			mhz = self.nvml.nvmlDeviceGetClockInfo(self.handle, self.nvml.NVML_CLOCK_SM)
			return float(mhz), bool(self.reasons(self.handle) & self.LIMITED)
		except self.nvml.NVMLError:
			return None

	def Close(self):
		try:
			self.nvml.nvmlShutdown()
		except Exception:
			pass


class SmiClock(ClockReader):
	name = "nvidia-smi"
	PERIOD_MS = 500

	def __init__(self, torch, device):
		import shutil, subprocess
		smi = shutil.which("nvidia-smi")
		if not smi:
			raise RuntimeError("no nvidia-smi")
		uuid = self.Uuid(torch, device)
		# Older drivers name the reasons throttle, newer event
		for reasons in ("clocks_event_reasons.active", "clocks_throttle_reasons.active"):
			query = [smi, "-i", uuid, "--query-gpu=clocks.sm," + reasons, "--format=csv,noheader,nounits"]
			try:
				out = subprocess.run(query, capture_output=True, text=True, timeout=20)
			except (OSError, subprocess.TimeoutExpired):
				raise RuntimeError("nvidia-smi did not answer")
			if out.returncode == 0 and out.stdout.strip() and self.Parse(out.stdout.strip().splitlines()[-1]):
				break
		else:
			raise RuntimeError("nvidia-smi gave no clock")
		self.last = None
		# Dies with campy-live, even when it is killed (campy/children.py)
		self.proc = children.Child(query + ["-lms", str(self.PERIOD_MS)], stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL, text=True)
		self.thread = threading.Thread(target=self.Follow, daemon=True, name="campy-live nvidia-smi")
		self.thread.start()
		deadline = time.monotonic() + 5
		while self.last is None and self.proc.poll() is None and time.monotonic() < deadline:
			time.sleep(0.05)

	def Parse(self, line):
		try:
			mhz, mask = (c.strip() for c in line.split(","))
			return float(mhz), bool(int(mask, 16) & self.LIMITED)
		except ValueError:
			return None

	def Follow(self):
		for line in self.proc.stdout:
			look = self.Parse(line)
			if look is not None:
				self.last = look

	def Read(self):
		return self.last

	def Close(self):
		self.proc.Close()


class SysfsClock(ClockReader):
	name = "sysfs pp_dpm_sclk"

	def __init__(self, torch, device, paths=None):
		import glob
		paths = paths if paths is not None else sorted(glob.glob("/sys/class/drm/card*/device/pp_dpm_sclk"))
		if not paths:
			raise RuntimeError("no pp_dpm_sclk")
		self.path = paths[0]

	def Read(self):
		try:
			with open(self.path) as f:
				for line in f:
					if line.rstrip().endswith("*"):
						return float(line.split()[1].lower().rstrip("mhz")), False
		except (OSError, ValueError, IndexError):
			pass
		return None


def OnOff(value):
	"""
	--cuda-graphs, --fixed-crops: on, off or auto (on with a GPU, CUDA or
	ROCm). The bare option is on; True and False (from older callers) too.
	"""
	if value is True or value is False or value is None:
		return "on" if value else "off"
	if value not in ("on", "off", "auto"):
		raise SystemExit("on, off or auto, not {}".format(value))
	return value


def HoldsClock(args, model):
	# --hold-gpu-clock: auto is on with a GPU (NVIDIA's CUDA or AMD's ROCm)
	return args.hold_gpu_clock != "off" and getattr(model, "device", "cpu").startswith("cuda")


def WarmCounts(n_frames, most, crops):
	"""
	The animals per frame to warm up with: for each count of crops, that many
	spread over the frames, and one frame with more than most (the centroid
	layer keeps its most confident most).
	"""
	counts = [[n // n_frames + (b < n % n_frames) for b in range(n_frames)] for n in crops]
	if crops:
		counts.append([most + 1] + [0] * (n_frames - 1))
	return counts


class FakeCentroids:
	"""
	For warm-up, around the centroid layer's model: adds counts[b] peaks to
	frame b's confidence maps, as many animals found (as the random model's
	SyntheticCentroidPeaks), while in a with block.
	"""

	def __init__(self, layer, counts):
		self.layer, self.counts = layer, counts

	def __enter__(self):
		self.backend = self.layer.backend
		self.layer.backend = self
		return self

	def __exit__(self, *exc):
		self.layer.backend = self.backend

	def __getattr__(self, name):
		return getattr(self.backend, name)

	def __call__(self, x):
		import torch
		out = self.backend(x)
		return {k: self.Peaks(v) if isinstance(v, torch.Tensor) and v.ndim == 4 else v for k, v in out.items()}

	def Peaks(self, cm):
		# Gaussians of height 1 on whole pixels (one local maximum each),
		# across the middle of the map, over the model's own output, made small
		import torch
		b, c, h, w = cm.shape
		yy, xx = torch.meshgrid(torch.arange(h, device=cm.device), torch.arange(w, device=cm.device), indexing="ij")
		blobs = torch.zeros_like(cm)
		for f, k in enumerate(self.counts[:b]):
			for i in range(k):
				cx, cy = (w * (i + 1)) // (k + 1), (h * (3 + 4 * (i % 2))) // 10
				blobs[f, :] += torch.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / 8.0).to(cm.dtype)
		return cm.clamp(-1, 1) * 1e-3 + blobs


class RandomTopDown(TopDown):
	"""--model random: random weights, of the benchmark's sizes (see the module's doc)."""

	def Build(self, args):
		import torch, warnings
		from omegaconf import OmegaConf
		from sleap_nn.config.get_config import get_model_config
		from sleap_nn.inference.layers.backends.torch_backend import TorchBackend
		from sleap_nn.inference.layers.centered_instance import CenteredInstanceLayer
		from sleap_nn.inference.layers.centroid import CentroidLayer
		from sleap_nn.inference.layers.configs import PostprocessConfig, PreprocessConfig
		from sleap_nn.inference.layers.topdown import TopDownLayer
		from sleap_nn.training.lightning_modules import (CentroidLightningModule,
			TopDownCenteredInstanceLightningModule)

		c = dict(scale=args.centroid_scale, filters=args.centroid_filters,
			max_stride=args.centroid_max_stride, output_stride=args.centroid_output_stride)
		ci = dict(crop=args.crop, filters=args.instance_filters, max_stride=args.instance_max_stride,
			output_stride=args.instance_output_stride)
		channels = self.shape[2]
		height, width = self.shape[:2]

		def Unet(filters, max_stride, output_stride):
			return {"unet": {"in_channels": channels, "filters": filters, "filters_rate": args.filters_rate,
				"max_stride": max_stride, "output_stride": output_stride}}

		cfg = OmegaConf.structured(get_model_config(
			backbone_config=Unet(c["filters"], c["max_stride"], c["output_stride"]),
			head_configs={"centroid": {"confmaps": {"sigma": 2.5, "output_stride": c["output_stride"]}}}))
		centroid_module = CentroidLightningModule(model_type="centroid", backbone_type="unet",
			backbone_config=cfg.backbone_config, head_configs=cfg.head_configs).eval()
		cfg = OmegaConf.structured(get_model_config(
			backbone_config=Unet(ci["filters"], ci["max_stride"], ci["output_stride"]),
			head_configs={"centered_instance": {"confmaps": {
				"part_names": ["node{}".format(i) for i in range(args.nodes)], "sigma": 2.5,
				"output_stride": ci["output_stride"]}}}))
		instance_module = TopDownCenteredInstanceLightningModule(model_type="centered_instance",
			backbone_type="unet", backbone_config=cfg.backbone_config, head_configs=cfg.head_configs).eval()
		# The Lightning modules themselves: their forward sends its input to
		# self.device, which only their own .to() updates
		centroid_module.to(self.device)
		instance_module.to(self.device)
		if self.memoryFormat == "channels_last":
			centroid_module.to(memory_format=torch.channels_last)
			instance_module.to(memory_format=torch.channels_last)
		cm_h = math.ceil(height * c["scale"] / c["max_stride"]) * c["max_stride"] // c["output_stride"]
		cm_w = math.ceil(width * c["scale"] / c["max_stride"]) * c["max_stride"] // c["output_stride"]
		centroid_net = SyntheticCentroidPeaks(centroid_module, self.n_frames, cm_h, cm_w, args.animals)
		with warnings.catch_warnings():
			warnings.simplefilter("ignore", UserWarning) # TorchBackend's fp16 notice
			centroid_backend = TorchBackend(model=centroid_net, device=self.device)
			instance_backend = TorchBackend(model=instance_module, device=self.device)
		self.Fp16([centroid_backend, instance_backend])
		layer = TopDownLayer(
			centroid_layer=CentroidLayer(backend=centroid_backend, output_stride=c["output_stride"],
				max_instances=args.max_instances, max_stride=c["max_stride"],
				preprocess_config=PreprocessConfig(scale=c["scale"]),
				postprocess_config=PostprocessConfig(peak_threshold=0.2, refinement="integral",
					max_instances=args.max_instances)),
			centered_instance_layer=CenteredInstanceLayer(backend=instance_backend,
				output_stride=ci["output_stride"], max_stride=ci["max_stride"],
				preprocess_config=PreprocessConfig(scale=1.0),
				postprocess_config=PostprocessConfig(peak_threshold=float("-inf"), refinement="integral")),
			crop_size=(ci["crop"], ci["crop"]))
		self.crop = (ci["crop"], ci["crop"])
		self.nodes = args.nodes
		text = "random weights: centroid s{:g} f{} ms{} os{}, instance crop{} f{} ms{} os{}, {} nodes, " \
			"{} animals per frame".format(c["scale"], c["filters"], c["max_stride"], c["output_stride"],
			ci["crop"], ci["filters"], ci["max_stride"], ci["output_stride"], args.nodes, args.animals)
		return layer, text

	def Describe(self):
		return "{} ({}, {}, {}, max {} animals{})".format(self.text, self.device, self.precision, self.memoryFormat,
			self.instances, "" if self.tiedPeaks else ", strict peaks")


def SyntheticCentroidPeaks(inner, n_frames, cm_h, cm_w, k, sigma=2.0):
	"""
	The random centroid model, run in full, with its output replaced by k
	peaks per frame (bench/sleap_infer_bench.py's SyntheticCentroidPeaks): a
	random model finds hundreds, a trained one about as many as there are
	animals.
	"""
	import torch

	class Peaks(torch.nn.Module):
		def __init__(self):
			super().__init__()
			self.inner = inner
			yy, xx = torch.meshgrid(torch.arange(cm_h), torch.arange(cm_w), indexing="ij")
			blobs = torch.zeros(n_frames, 1, cm_h, cm_w)
			for b in range(n_frames):
				for i in range(k):
					cx = cm_w * (i + 1) / (k + 1)
					cy = cm_h * (0.3 + 0.4 * ((i + b) % 2))
					blobs[b, 0] += torch.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma ** 2))
			self.register_buffer("blobs", blobs)

		def forward(self, img):
			cm = self.inner(img)
			return cm.clamp(-1, 1) * 1e-3 + self.blobs.to(cm.dtype)

	return Peaks()


class TrainedTopDown(TopDown):
	"""--model sleap: trained sleap-nn models (--centroid and --instance model folders)."""

	def Build(self, args):
		if not args.centroid or not args.instance:
			raise SystemExit("--model sleap needs --centroid and --instance: the trained model folders")
		import warnings
		from sleap_nn.inference.predictor import Predictor
		with warnings.catch_warnings():
			warnings.simplefilter("ignore", UserWarning)
			predictor = Predictor.from_model_paths([args.centroid, args.instance], device=self.device,
				max_instances=args.max_instances, peak_threshold=[args.centroid_threshold, args.peak_threshold])
		layer = predictor.layer
		# Its nodes, from its skeleton: with no animal found, sleap-nn's own
		# guess at the node count is wrong
		skeleton = getattr(predictor, "skeleton", None)
		if skeleton is not None:
			self.nodeNames = [node.name for node in skeleton.nodes]
			self.nodes = len(self.nodeNames)
			try: # for campy-live-view: pairs of node indices
				self.edges = [[int(a), int(b)] for a, b in skeleton.edge_inds]
			except Exception:
				self.edges = None
		self.Fp16([layer.centroid_layer.backend, layer.centered_instance_layer.backend])
		if self.memoryFormat == "channels_last":
			for backend in (layer.centroid_layer.backend, layer.centered_instance_layer.backend):
				backend.model.to(memory_format=self.torch.channels_last)
		self.crop = tuple(int(x) for x in layer.crop_size)
		text = "trained: centroid {}, instance {}".format(args.centroid, args.instance)
		return layer, text

	def Describe(self):
		return "{} ({}, {}, {}, max {} animals{})".format(self.text, self.device, self.precision, self.memoryFormat,
			self.instances, "" if self.tiedPeaks else ", strict peaks")


MODELS = {"random": RandomTopDown, "sleap": TrainedTopDown, "none": NoModel}


# ---------------------------------------------------------------- 3D

class Pose3D:
	"""
	campy/pose3d.py's Tracker on each set's keypoints (--calibration): which
	detection in each camera is which animal, and each animal's 3D pose.
	"""

	def __init__(self, args, cameraNames):
		from campy import pose3d
		names = args.calibration_names.split(",") if args.calibration_names else list(cameraNames)
		if len(names) != len(cameraNames):
			raise SystemExit("--calibration-names gives {} cameras; the feed has {}".format(len(names), len(cameraNames)))
		self.names = names
		self.cameras = pose3d.LoadCalibration(args.calibration, names=names)
		self.animals = args.animals
		self.tracker = pose3d.Tracker(self.cameras, n_animals=self.animals)
		self.text = "{} animals, cameras {} of {}".format(self.animals, ", ".join(names), args.calibration)
		Say("3D: {}.".format(self.text))

	def Update(self, keypoints, scores, frame):
		return self.tracker.Update(keypoints, scores, frame=frame)

	def Describe(self):
		return self.text


class Pose3DThread:
	"""
	The Tracker on a thread of its own, so the model can go on to the next
	set meanwhile: it takes the sets in the order they are put, makes their
	3D poses and adds their rows to the log. An error stops it, and is raised
	by the next Put, or by Close.
	"""

	def __init__(self, tracker, log, queued=8):
		self.tracker, self.log = tracker, log
		self.sets = queue.Queue(maxsize=queued) # (row, frames, inference ns); None stops it
		self.error = None
		self.thread = threading.Thread(target=self.Run, daemon=True, name="campy-live 3D")
		self.thread.start()

	def Put(self, row, frames, inference):
		# Waits only if the 3D is that many sets behind
		if self.error is not None:
			raise self.error
		self.sets.put((row, frames, inference))

	def Run(self):
		while True:
			item = self.sets.get()
			if item is None:
				return
			if self.error is not None:
				continue
			row, frames, inference = item
			try:
				# Which animal is which across the cameras, and its 3D pose
				start = time.perf_counter_ns()
				poses = self.tracker.Update(row["keypoints"], row["peak_vals"], int(row["frame"]))
				pose3d_ns, pose3d_pc = hostclock.HostNs(), time.perf_counter_ns()
				self.log.Add(row, frames, poses, pose3d_ns, durations=(inference, pose3d_pc - start))
			except BaseException as e:
				self.error = e

	def Stop(self):
		# After the sets put so far
		if self.thread.is_alive():
			self.sets.put(None)
			self.thread.join()

	def Close(self):
		self.Stop()
		if self.error is not None:
			raise self.error


# ---------------------------------------------------------------- the log

def RowType(n_cameras, instances, nodes, animals3d=0):
	c, i, n = n_cameras, max(1, instances), max(1, nodes)
	fields = [
		("frame", "<i8"), ("host_ns", "<i8", (c,)), ("written_ns", "<i8", (c,)), ("stamp", "<f8", (c,)),
		("read_ns", "<i8"), ("done_ns", "<i8"), ("inference_ns", "<i8"), ("skipped", "<i4"), ("found", "<i2", (c,)),
		("centroids", "<f4", (c, i, 2)), ("centroid_vals", "<f4", (c, i)),
		("keypoints", "<f4", (c, i, n, 2)), ("peak_vals", "<f4", (c, i, n))]
	if animals3d:
		a = animals3d
		fields += [("pose3d_ns", "<i8"), ("points3d", "<f4", (a, n, 3)), ("members", "<i2", (a, c)),
			("n_views", "<i1", (a,)), ("reproj_px", "<f4", (a, n))]
	return np.dtype(fields)


def Fit(value, shape):
	# value (cameras, animals, ...) into an array of shape, NaN where it has none
	out = np.full(shape, np.nan, np.float32)
	if value is not None:
		value = np.asarray(value)
		cut = tuple(slice(0, min(a, b)) for a, b in zip(value.shape, shape))
		out[cut] = value[cut]
	return out


class Log:
	"""
	The rows, written about every second, and the numbers for the summary.
	Each second's rows are written by a thread of their own (Writer, as
	campy's frametimes.npy): written here, between two sets, a write the disk
	made wait (tens of ms, while campy's ffmpegs write the videos) held up
	the next set, and was most of campy-live's latency over 20 ms.
	"""

	def __init__(self, path, n_cameras, instances, nodes, frameRate, animals3d=0):
		self.path = path
		self.rows_type = RowType(n_cameras, instances, nodes, animals3d)
		self.file = GrowingNpy(path, self.rows_type, header_bytes=1024)
		self.latency_3d, self.pose3d = [], []
		self.writer = Writer("campy-live log", path)
		self.error = None # a write that failed, said when closing and in the summary (log_error)
		self.closed = False
		self.results = None # a liveresults.Publisher: each row also into shared memory
		self.rows = []
		self.every = max(1, int(round(frameRate or 60)))
		self.frameRate = frameRate
		self.latency_last, self.latency_first, self.read, self.predict, self.spread = [], [], [], [], []
		self.sets, self.skipped, self.misaligned = 0, 0, 0
		self.first = self.lastFrame = None
		self.started = None # when the first set was read

	def Row(self, frames, skipped, read_ns, done_ns, result, inference_ns=0):
		# One set's row: its frames, and the model's results
		row = np.zeros((), self.rows_type)
		host = [f.hostTime for f in frames]
		row["frame"] = frames[0].frameNumber
		row["host_ns"] = host
		row["written_ns"] = [f.written for f in frames]
		row["stamp"] = [f.timeStamp for f in frames]
		row["read_ns"], row["done_ns"], row["skipped"] = read_ns, done_ns, skipped
		row["inference_ns"] = inference_ns
		for key in ("centroids", "centroid_vals", "keypoints", "peak_vals"):
			row[key] = Fit(result.get(key), row[key].shape)
		row["found"] = (~np.isnan(row["centroids"]).any(-1)).sum(-1)
		return row

	def Add(self, row, frames, poses=None, pose3d_ns=None, durations=None):
		# The row, with its 3D poses (pose3d.Poses) when they were made.
		# durations: (inference, 3D) in ns, timed with time.perf_counter_ns;
		# the row's *_ns times are on the host clock (campy/clock.py)
		if poses is not None:
			row["pose3d_ns"] = pose3d_ns
			row["points3d"] = poses.points3d
			row["members"] = poses.members
			row["n_views"] = poses.n_views
			row["reproj_px"] = poses.reproj_px
		if self.results is not None:
			self.results.Write(row)
		read_ns, done_ns, skipped = int(row["read_ns"]), int(row["done_ns"]), int(row["skipped"])
		host = np.array([f.hostTime for f in frames], np.int64)
		self.rows.append(row)
		if len(self.rows) >= self.every:
			self.Write()

		if self.first is None:
			self.first = frames[0].frameNumber
			self.started = time.monotonic()
		self.lastFrame = frames[0].frameNumber
		self.sets += 1
		self.skipped += skipped
		spread = int(host.max() - host.min())
		self.spread.append(spread)
		if self.frameRate and spread > 0.5e9 / self.frameRate:
			self.misaligned += 1
		self.latency_last.append(done_ns - host.max())
		self.latency_first.append(done_ns - host.min())
		self.read.append(read_ns - host.max())
		inference, pose3d = durations if durations is not None else (done_ns - read_ns, None)
		self.predict.append(inference)
		if poses is not None:
			self.latency_3d.append(pose3d_ns - host.max())
			self.pose3d.append(pose3d_ns - done_ns if pose3d is None else pose3d)

	def Write(self):
		# Hands the rows to the writer thread
		if self.rows:
			self.writer.Do(self.file.Append, np.array(self.rows, self.rows_type))
			self.rows = []

	def Summary(self, since=0):
		def Ms(values):
			v = np.asarray(values[since:], np.float64) / 1e6
			if not len(v):
				return {}
			return {"p50": float(np.percentile(v, 50)), "p90": float(np.percentile(v, 90)),
				"p99": float(np.percentile(v, 99)), "max": float(v.max()), "mean": float(v.mean())}
		total = (self.lastFrame - self.first + 1) if self.first is not None else 0
		return {"sets": self.sets, "frames_spanned": total, "skipped": self.skipped,
			"misaligned": self.misaligned,
			"sets_per_s": (self.sets - 1) / max(1e-9, time.monotonic() - self.started) if self.sets > 1 else 0.0,
			"latency_from_last_frame_ms": Ms(self.latency_last),
			"latency_from_first_frame_ms": Ms(self.latency_first),
			"wait_and_read_ms": Ms(self.read), "inference_ms": Ms(self.predict), # upload, model, results back
			"host_time_spread_ms": Ms(self.spread),
			"latency_to_3d_from_last_frame_ms": Ms(self.latency_3d), "pose3d_ms": Ms(self.pose3d),
			**({"log_error": self.error} if self.error else {})}

	def Close(self):
		# The rows still queued written first. A write that failed is said,
		# and goes in the summary: the sets went on without it, and the
		# summary is still written.
		if self.closed:
			return
		self.closed = True
		if self.results is not None:
			self.results.Close()
		self.Write()
		try:
			self.writer.Close()
		except Exception as e:
			self.error = "{}: {}".format(type(e).__name__, e)
			Say("Could not write {}: {}. Its rows end where the writes stopped.".format(self.path, self.error))
		finally:
			self.file.Close()


# ---------------------------------------------------------------- the program

def Run(args):
	names = args.rings.split(",") if args.rings else [livefeed.RingName(args.name, n) for n in range(args.cameras)]
	stopping = []
	previous = signal.signal(signal.SIGINT, lambda *a: stopping.append(1))
	stop = lambda: bool(stopping)
	placed = PlaceSelf(args)
	if args.nice:
		try:
			os.nice(args.nice)
		except (AttributeError, OSError) as e:
			Say("Could not lower this program's priority (--nice): {}".format(e))

	cameras = Cameras(names, poll=args.poll_ms / 1e3)
	log = worker = clock = session = None
	models = ModelRecords(args) # the models' SHA-256s, read meanwhile
	# The progress lines, from a thread of their own: printed in the loop,
	# into a log file on the disk the videos go to, one could wait as long as
	# a log write
	printer = Writer("campy-live printer", "the progress line")
	try:
		# The model first, for the frames expected, so it is ready when the
		# feed's first frame comes (campy makes its rings only a few seconds
		# before the trigger); pose3d's imports too
		expected = (FrameShape(args.frame_shape), np.dtype(np.uint8))
		model, host = Prepare(args, len(names), *expected)
		warnings = DefaultWarnings(args, model) # said now, before the wait for the feed
		if args.calibration:
			from campy import pose3d
		if not cameras.Attach(wait=args.wait_sec, stop=stop):
			Say("No live feed: stopped." if stop() else "No live feed after {:g} s.".format(args.wait_sec))
			return 1
		Say("Live feed: {}.".format(cameras.Describe()))
		if HoldsClock(args, model):
			clock = GpuClock(model.device, args.hold_gpu_clock_blocks)
			clock.Start()
		if placed:
			PlaceCampy(args, cameras.readers)
		shape, dtype = cameras.readers[0].shape, cameras.readers[0].dtype
		frameRate = cameras.readers[0].Info("frameRate")
		if (shape, dtype) != expected:
			if args.model == "none":
				host = HostBuffer(False, (len(names),) + shape, dtype)
			else:
				Say("The feed's frames are {}x{}x{} {}, not the {}x{}x{} {} the model was made for (--frame-shape): "
					"making it again, for these.".format(*(shape + (dtype,) + expected[0] + (expected[1],))))
				del model, host
				model, host = Prepare(args, len(names), shape, dtype)

		tracker = Pose3D(args, [r.cameraName for r in cameras.readers]) if args.calibration else None

		out = args.out or "campy-live_{}".format(datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
		session = StartSession(args, model, tracker, cameras, models, out, warnings)
		log = Log(out + ".npy", len(names), model.instances, model.nodes or args.nodes, frameRate,
			tracker.animals if tracker else 0)
		if args.results_feed == "on":
			log.results = Publish(args, log, names, cameras, model, tracker, frameRate)
		worker = Pose3DThread(tracker.tracker, log) if tracker else None
		Say("Logging to {}.npy.".format(out))
		reported, since = time.monotonic(), 0
		while True:
			got = cameras.NextSet(host.array, stop=stop)
			if got is None:
				break
			frames, skipped = got
			if clock is not None:
				clock.Busy()
			read_ns, read_pc = hostclock.HostNs(), time.perf_counter_ns()
			result = model.Predict(model.Upload(host.tensor)) # ends with the results on the host
			done_ns, done_pc = hostclock.HostNs(), time.perf_counter_ns()
			row = log.Row(frames, skipped, read_ns, done_ns, result, done_pc - read_pc)
			if worker is None:
				log.Add(row, frames, durations=(done_pc - read_pc, None))
			else:
				worker.Put(row, frames, done_pc - read_pc) # its 3D while the model runs on the next set
			if args.report_sec and time.monotonic() - reported >= args.report_sec:
				printer.Do(Say, Line(log.Summary(since), log) + (clock.Now() if clock is not None else ""))
				reported, since = time.monotonic(), len(log.latency_last)
		if worker is not None:
			worker.Close()
		if clock is not None:
			clock.Stop()
		log.Close()
		printer.Close()
		summary = log.Summary()
		summary.update(model=model.Describe(), nodes=getattr(model, "nodeNames", None),
			edges=getattr(model, "edges", None), pose3d=tracker.Describe() if tracker else None,
			calibration=os.path.abspath(args.calibration) if tracker else None,
			calibration_names=tracker.names if tracker else None, animals=tracker.animals if tracker else 0,
			cameras=[r.cameraName for r in cameras.readers],
			rings=names, frame_shape=list(shape), frame_rate=frameRate, log=out + ".npy", cpus=placed,
			cuda_graphs=model.DescribeGraphs() if hasattr(model, "DescribeGraphs") else None,
			fast_upsample=model.DescribeUpsamples() if hasattr(model, "DescribeUpsamples") else None,
			results_feed=log.results.name if log.results else None,
			gpu_clock_held=clock is not None, gpu_clock=clock.Describe() if clock is not None else None,
			ended="stopped (Ctrl-C)" if stop() else "the recording ended")
		summary["session"] = session.path if session else None
		with open(out + ".json", "x") as f:
			json.dump(summary, f, indent=2)
		Say("Done, {}: {}".format(summary["ended"], Line(summary, log)))
		Say("Summary in {}.json.".format(out))
		if session is not None:
			session.End(summary, out + ".npy", stopped=stop())
			session = None
		return 0
	except BaseException as e:
		if session is not None:
			try:
				summary = log.Summary()
			except Exception:
				summary = None
			session.End(summary, log.path if log is not None else None, error="{}: {}".format(type(e).__name__, e))
			session = None
		raise
	finally:
		# Stopped by an error too: the rows and lines still queued are written
		try:
			if clock is not None:
				clock.Stop()
			if worker is not None:
				worker.Stop()
			if log is not None:
				log.Close()
			printer.Close()
		finally:
			cameras.Close()
			signal.signal(signal.SIGINT, previous)


class ModelRecords:
	"""
	The trained models' records for campy-live-session.json (manifest.
	ModelRecord), made on a thread of their own as campy-live starts: a
	best.ckpt can be several hundred MB to hash.
	"""

	def __init__(self, args):
		self.records = {}
		folders = {"centroid": args.centroid, "instance": args.instance} if args.model == "sleap" else {}
		self.thread = threading.Thread(target=self.Run, args=(folders,), daemon=True, name="campy-live model hashes")
		self.thread.start()

	def Run(self, folders):
		for kind, folder in folders.items():
			try:
				self.records[kind] = manifest.ModelRecord(folder)
			except Exception as e:
				self.records[kind] = {"folder": folder, "error": str(e)}

	def Get(self):
		self.thread.join()
		return self.records


def RunContext(model):
	# What the default changes apply to (defaults.Relied): a GPU, and ROCm
	device = str(getattr(model, "device", "cpu"))
	gpu = device.startswith("cuda") and not isinstance(model, NoModel)
	rocm = False
	if gpu:
		try:
			import torch
			rocm = bool(getattr(torch.version, "hip", None))
		except ImportError:
			pass
	return {"gpu": gpu, "rocm": rocm}


def DefaultWarnings(args, model):
	"""
	Says the changed defaults this run relies on (campy/defaults.py), once,
	and returns them as lines, for campy-live-session.json.
	"""
	try:
		sources, _ = manifest.OptionSources(Parser(), getattr(args, "argv", None) or [])
		known = None if args.defaults_version in ("", "None") else args.defaults_version
		warnings = defaults.Warnings(vars(args), sources, RunContext(model), program="campy-live", known=known,
			enabled=args.default_warnings == "on")
	except ValueError as e: # a --defaults-version that is not a version
		Say(str(e))
		return []
	if warnings:
		Say(defaults.Message(warnings, "campy-live"))
	return warnings


def StartSession(args, model, tracker, cameras, models, out, warnings):
	"""
	Writes campy-live-session.json (manifest.LiveSession): into the
	recording's folder, which the feed says, else beside the log. Returns it,
	or None when it could not be made: it never stops campy-live.
	"""
	try:
		recording = livefeed.RecordingFolder(cameras.readers)
		session = manifest.LiveSession(manifest.LiveSession.Place(recording, out), say=Say)
		calibration = None
		if tracker is not None:
			calibration = manifest.FileRecord(args.calibration)
			calibration["cameraNames"] = tracker.names
		describe = lambda name: getattr(model, name)() if hasattr(model, name) else None
		reader = cameras.readers[0]
		session.Start(args, Parser(), getattr(args, "argv", None) or [], recording, models.Get(), calibration,
			warnings, extra={
				"feed": {"rings": list(cameras.names), "cameras": [r.cameraName for r in cameras.readers],
					"frameShape": list(reader.shape), "frameRate": reader.Info("frameRate")},
				"model": {"kind": args.model, "description": model.Describe(), "device": str(getattr(model, "device", "")),
					"engine": describe("DescribeEngine"), "nodes": getattr(model, "nodeNames", None)},
				"pose3d": {"animals": tracker.animals, "description": tracker.Describe()} if tracker else None,
				"log": os.path.abspath(out + ".npy"),
				})
	except Exception as e:
		Say("Could not write campy-live-session.json: {}. Tracking goes on.".format(e))
		return None
	Say("How this run is made: {}.".format(session.path))
	return session


def Publish(args, log, names, cameras, model, tracker, frameRate):
	# The rows into shared memory too, with what campy-live-view needs to draw them
	name = liveresults.RingName(args.name)
	results = liveresults.Publisher(name, log.rows_type, say=Say, cameras=[r.cameraName for r in cameras.readers],
		rings=names, nodes=getattr(model, "nodeNames", None), edges=getattr(model, "edges", None),
		calibration=os.path.abspath(args.calibration) if tracker else None,
		calibration_names=tracker.names if tracker else None, animals=tracker.animals if tracker else 0,
		frame_rate=frameRate, pid=os.getpid())
	if results.writer is not None:
		Say("Results in shared memory {} too, for campy-live-view.".format(name))
	return results


def PlaceSelf(args):
	"""
	campy-live onto --cpus at --priority. auto: on a CPU with performance and
	efficiency cores, the performance ones, at high priority; elsewhere as it
	is. Returns what it did, as text, or None.
	"""
	choice, priority = args.cpus.strip().lower(), args.priority
	if choice == "none":
		chosen = None
	elif choice == "auto":
		cores = cpus.Hybrid()
		chosen = cores["performance"] if cores else None
	else:
		try:
			chosen = cpus.Resolve(args.cpus)
		except ValueError as e:
			raise SystemExit("--cpus {}: {}".format(args.cpus, e))
	if priority == "auto":
		priority = "high" if chosen is not None and choice != "none" else "normal"
	done = []
	if chosen is not None:
		try:
			cpus.Pin(chosen)
			done.append("CPUs {}".format(cpus.Text(chosen)) + (" (the performance cores)" if choice == "auto" else ""))
		except OSError as e:
			Say("Could not pin campy-live to CPUs {} (--cpus): {}".format(cpus.Text(chosen), e))
	if priority != "normal":
		try:
			cpus.SetPriority(priority)
			done.append("{} priority".format(priority.replace("_", " ")))
		except OSError as e:
			Say("Could not give campy-live {} priority (--priority): {}".format(priority, e))
	if not done:
		return None
	Say("campy-live on {}.".format(" at ".join(done)))
	return " at ".join(done)


def PlaceCampy(args, readers):
	# campy-acquire onto --campy-cpus (auto: the efficiency cores, when campy-live is on the performance ones):
	# its main process and camera processes, as their feeds say them, and the encoders they started
	choice = args.campy_cpus.strip().lower()
	if choice == "none" or (choice == "auto" and (args.cpus.strip().lower() != "auto" or cpus.Hybrid() is None)):
		return
	try:
		chosen = cpus.Resolve("efficiency" if choice == "auto" else args.campy_cpus)
	except ValueError as e:
		Say("--campy-cpus {}: {}".format(args.campy_cpus, e))
		return
	pids = livefeed.Processes(readers)
	if not pids:
		Say("campy-acquire's feed doesn't say its processes (a campy from before October 2026): to put it on CPUs "
			"{} start it so (python -m campy.cpus says how).".format(cpus.Text(chosen)))
		return
	moved, failed = 0, []
	try:
		tree = cpus.Tree(pids)
	except OSError as e:
		tree, failed = [], [str(e)]
	for pid in tree:
		if pid == os.getpid():
			continue
		try:
			cpus.Pin(chosen, pid)
			moved += 1
		except OSError as e:
			failed.append("process {}: {}".format(pid, e))
	if moved:
		Say("campy-acquire ({} processes: camera processes and encoders) on CPUs {}{}.".format(moved,
			cpus.Text(chosen), " (the efficiency cores)" if choice == "auto" else ""))
	if failed:
		Say("Could not move campy-acquire onto CPUs {} (--campy-cpus): {}".format(cpus.Text(chosen), "; ".join(failed)))


def Prepare(args, n_frames, shape, dtype):
	# The model, for sets of n_frames frames of shape and dtype, warmed up, and
	# the host buffer the sets are copied into
	model = MODELS[args.model](args, n_frames, shape)
	host = HostBuffer(getattr(model, "torch", None) is not None, (n_frames,) + tuple(shape), dtype)
	if hasattr(model, "Warm"):
		t = time.perf_counter()
		model.Warm(host.tensor, crops=range(1, n_frames * args.max_instances + 1) if args.model == "sleap" else ())
		Say("Model: {}. Warmed up in {:.1f} s.".format(model.Describe(), time.perf_counter() - t))
		for describe in ("DescribeEngine", "DescribeUpsamples", "DescribeGraphs"):
			text = getattr(model, describe)() if hasattr(model, describe) else None
			if text:
				Say(text + ".")
	else:
		Say("Model: {}.".format(model.Describe()))
	return model, host


def FrameShape(text):
	# "1024x1280x1" (height x width x channels; channels 1 if left out)
	try:
		shape = tuple(int(x) for x in text.lower().split("x"))
	except ValueError:
		shape = ()
	if len(shape) == 2:
		shape += (1,)
	if len(shape) != 3 or min(shape) < 1:
		raise SystemExit("--frame-shape {}: give it as height x width x channels, e.g. 1024x1280x1".format(text))
	return shape


def Line(s, log):
	lat = s.get("latency_from_last_frame_ms", {})
	inf = s.get("inference_ms", {})
	text = "{} sets ({:.1f}/s), {} skipped, {} misaligned; latency p50 {:.1f} p99 {:.1f} max {:.1f} ms; " \
		"inference p50 {:.1f} p99 {:.1f} ms".format(s["sets"] if "sets" in s else 0, s.get("sets_per_s", 0),
		s.get("skipped", 0), s.get("misaligned", 0), lat.get("p50", float("nan")), lat.get("p99", float("nan")),
		lat.get("max", float("nan")), inf.get("p50", float("nan")), inf.get("p99", float("nan")))
	lat3, p3 = s.get("latency_to_3d_from_last_frame_ms", {}), s.get("pose3d_ms", {})
	if lat3:
		text += "; to 3D p50 {:.1f} p99 {:.1f} max {:.1f} ms (3D alone p50 {:.2f} p99 {:.2f} ms)".format(
			lat3["p50"], lat3["p99"], lat3["max"], p3["p50"], p3["p99"])
	return text


class HostBuffer:
	"""
	Where a set is copied out of shared memory: a numpy array, and with a
	model, a torch tensor on the same memory, pinned when the device can use
	that (a faster upload to a discrete GPU).
	"""

	def __init__(self, torch, shape, dtype):
		if not torch:
			self.array = np.empty(shape, dtype)
			self.tensor = self.array
			return
		import torch
		if np.dtype(dtype) != np.uint8:
			self.array = np.empty(shape, dtype)
			self.tensor = torch.from_numpy(self.array)
			return
		try:
			self.tensor = torch.empty(shape, dtype=torch.uint8, pin_memory=torch.cuda.is_available())
		except RuntimeError:
			self.tensor = torch.empty(shape, dtype=torch.uint8)
		self.array = self.tensor.numpy()


def Parser():
	p = argparse.ArgumentParser(prog="campy-live", description="SLEAP pose estimation on the frames campy is "
		"recording (campy-acquire with liveFeed True).", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
	p.add_argument("--cameras", type=int, default=3, help="How many cameras: rings <name>-0 to <name>-<cameras - 1>.")
	p.add_argument("--name", default=livefeed.DEFAULT_NAME, help="campy's liveFeedName.")
	p.add_argument("--rings", default="", help="Or the rings' names, comma-separated, in camera order.")
	p.add_argument("--model", default="random", choices=sorted(MODELS), help="See campy-live's help (python -m pydoc campy.live).")
	p.add_argument("--frame-shape", default="1024x1280x1", help="The frames' height x width x channels the model \
		is made and warmed up for before the feed comes; a feed of another size has it made again.")
	p.add_argument("--centroid", default="", help="--model sleap: the trained centroid model's folder.")
	p.add_argument("--instance", default="", help="--model sleap: the trained centered-instance model's folder.")
	p.add_argument("--centroid-threshold", type=float, default=0.2, help="--model sleap: centroid peak threshold.")
	p.add_argument("--peak-threshold", type=float, default=0.2, help="--model sleap: keypoint peak threshold.")
	p.add_argument("--device", default="auto", help="auto, cuda (also ROCm), mps or cpu.")
	p.add_argument("--precision", default="fp16", choices=["fp16", "fp32"], help="fp16: autocast, on cuda and ROCm only.")
	p.add_argument("--memory-format", default="auto", choices=["auto", "nchw", "channels_last"],
		help="auto: channels_last on Nvidia GPUs, where it is faster, and nchw elsewhere (ROCm, MPS, CPU), \
			as bench/sleap_infer_bench.py.")
	p.add_argument("--max-instances", type=int, default=5, help="Most animals per frame.")
	p.add_argument("--strict-peaks", action="store_true", help="sleap-nn's own centroid peak finding: a centroid \
		whose top is two equal pixels (fp16 rounds about 0.7%% of them so) is lost. Default: kept once (campy/peaks.py).")
	p.add_argument("--animals", type=int, default=5, help="How many animals there are: with --calibration, the 3D \
		identities 0 .. animals-1; with --model random, the animals it finds per frame.")
	p.add_argument("--calibration", default="", help="anipose calibration.toml: match the animals across the \
		cameras and triangulate them each set (campy/pose3d.py). Default: 2D only.")
	p.add_argument("--calibration-names", default="", help="With --calibration: the cameras' names in it, \
		comma-separated, in feed order (default: campy's cameraNames, as the feed gives them).")
	p.add_argument("--nodes", type=int, default=15, help="--model random: nodes per animal.")
	p.add_argument("--centroid-scale", type=float, default=RANDOM_CENTROID["scale"])
	p.add_argument("--centroid-filters", type=int, default=RANDOM_CENTROID["filters"])
	p.add_argument("--centroid-max-stride", type=int, default=RANDOM_CENTROID["max_stride"])
	p.add_argument("--centroid-output-stride", type=int, default=RANDOM_CENTROID["output_stride"])
	p.add_argument("--crop", type=int, default=RANDOM_INSTANCE["crop"])
	p.add_argument("--instance-filters", type=int, default=RANDOM_INSTANCE["filters"])
	p.add_argument("--instance-max-stride", type=int, default=RANDOM_INSTANCE["max_stride"])
	p.add_argument("--instance-output-stride", type=int, default=RANDOM_INSTANCE["output_stride"])
	p.add_argument("--filters-rate", type=float, default=2.0)
	p.add_argument("--out", default="", help="Log file name, without .npy (default: campy-live_<date_time>).")
	p.add_argument("--results-feed", default="on", choices=["on", "off"], help="on: each set's row also into \
		shared memory <name>-results, as it is logged, for campy-live-view to show on the frames.")
	p.add_argument("--wait-sec", type=float, default=float("inf"), help="How long to wait for the feed.")
	p.add_argument("--poll-ms", type=float, default=0.5, help="How often to look for a new set.")
	p.add_argument("--report-sec", type=float, default=10, help="Say how it goes this often; 0: only at the end.")
	p.add_argument("--nice", type=int, default=0, help="Lower this program's CPU priority by this much (POSIX).")
	p.add_argument("--cpus", default="auto", help="The CPUs campy-live runs on: auto (on a CPU with performance and \
		efficiency cores, the performance cores; elsewhere all), performance, efficiency, all, none (as it is \
		started), or a list such as 0,1,10-13. python -m campy.cpus says which are which.")
	p.add_argument("--priority", default="auto", choices=("auto",) + cpus.PRIORITIES, help="campy-live's priority: \
		auto is high when --cpus auto put it on the performance cores, else normal. Above normal needs root on Linux.")
	p.add_argument("--campy-cpus", default="auto", help="The CPUs campy-acquire (its camera processes and \
		encoders) is moved onto when its feed comes: auto (the efficiency cores, when --cpus auto put campy-live on \
		the performance ones), efficiency, all, none, or a list.")
	p.add_argument("--cuda-graphs", nargs="?", const="on", default="auto", choices=["auto", "on", "off"],
		help="Record each model's GPU work as a CUDA graph per input shape and replay it (CUDA and ROCm): the same \
		results, with less CPU time to launch it. auto (the default): on with a GPU.")
	p.add_argument("--fixed-crops", nargs="?", const="on", default="auto", choices=["auto", "on", "off"],
		help="Crop --max-instances animals in every frame, padding the missing ones, so the GPU runs a whole set \
		without the CPU waiting for the animals found, and its results come back in one copy (FixedCrops). With \
		--cuda-graphs, the whole set is one graph. auto (the default): on with a GPU, when the models allow it \
		(else sleap-nn's own layer, and campy-live says why). Set --max-instances to the animals there are.")
	p.add_argument("--engine", default="auto", choices=["auto", "on", "off", "compile", "tensorrt", "eager"],
		help="With --fixed-crops (on by default with a GPU): run the two networks as compiled engines \
		(FixedEngine), made while warming up, before the feed is waited for. compile: torch.compile (ROCm; NVIDIA \
		with Triton); tensorrt: NVIDIA TensorRT. auto (the default): tensorrt on NVIDIA where tensorrt and onnx \
		are installed, else compile where Triton is, else none; if the engine can't be made, the networks run in \
		torch, and campy-live says why. on: auto's choice, and stop if it can't be made. off (or eager): none.")
	p.add_argument("--engine-cache", default="", help="--engine: the folder its built engines are kept in and \
		read back from: TensorRT's plans, and torch.compile's kernels in inductor/ unless TORCHINDUCTOR_CACHE_DIR \
		is set (default: ~/.cache/campy/engines).")
	p.add_argument("--fast-upsample", default="auto", choices=["auto", "on", "off"], help="Time each bilinear \
		upsampling layer of the models in both memory layouts (NCHW and channels_last) while warming up, for each \
		shape of input, and run it in the faster one: the same results (see campy-live's help). auto: on with \
		a GPU and --memory-format nchw (auto's on ROCm), where it measured faster.")
	p.add_argument("--hold-gpu-clock", default="auto", choices=["auto", "on", "off"], help="Keep a few blocks of \
		the GPU spinning at the lowest priority from the feed's coming to the end, so the GPU keeps its full clock \
		(see campy-live's help). auto: on with a GPU (NVIDIA's CUDA or AMD's ROCm).")
	p.add_argument("--hold-gpu-clock-blocks", default="auto", type=lambda v: v if v == "auto" else str(int(v)),
		help="How many blocks spin while sets come: auto (0 to 2, as the GPU's clock needs, read through NVML, \
		nvidia-smi or sysfs; 1 when it can't be read; at least 1 on an AMD integrated GPU, as the EVO-X2's) or a \
		number.")
	p.add_argument("--defaults-version", default="None", help="The campy version whose defaults the command \
		that starts campy-live was written for, e.g. 2.1.0: campy-live then says only the defaults changed after \
		it that this run relies on (campy/defaults.py). None: every one.")
	p.add_argument("--default-warnings", default="on", choices=["on", "off"], help="Say, at startup, each \
		default that changed in a newer campy and that this run relies on.")
	return p


def ParseArgs(argv=None):
	argv = sys.argv[1:] if argv is None else list(argv)
	args = Parser().parse_args(argv)
	args.argv = argv # which options were given (manifest.OptionSources)
	return args


def Main(argv=None):
	sys.exit(Run(ParseArgs(argv)))


if __name__ == "__main__":
	Main()
