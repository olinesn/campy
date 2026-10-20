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
imports only numpy and this package's livefeed and frametimes, which need
nothing else, so campy's camera drivers need not be installed there. In that
environment, either install campy without its dependencies

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
also while campy-live runs), and <out>.json, the summary. Fields of each row:

  frame          the set's frame number
  host_ns        per camera: when campy got the frame (time.monotonic_ns,
                 as hosttimes.npy)
  written_ns     per camera: when it was in shared memory (time.perf_counter_ns)
  stamp          per camera: the camera's timestamp, seconds
  read_ns        when campy-live had copied the set (time.monotonic_ns)
  done_ns        when its keypoints were back on the host (time.monotonic_ns)
  skipped        sets skipped just before this one
  found          per camera: animals found
  centroids      per camera, per animal: x, y in the frame (NaN: none)
  centroid_vals  per camera, per animal: the centroid's confidence
  keypoints      per camera, per animal, per node: x, y in the frame
  peak_vals      per camera, per animal, per node: confidence

and with --calibration:

  pose3d_ns      when the 3D poses were made (time.monotonic_ns)
  points3d       per animal (identity), per node: x, y, z in the calibration's
                 units (mm); NaN when fewer than 2 cameras saw it
  members        per animal, per camera: which of that camera's animals it
                 is (an index into keypoints), -1 none
  n_views        per animal: how many cameras saw it
  reproj_px      per animal, per node: mean reprojection error, pixels

Latency, from a frame reaching the computer to its keypoints, is done_ns
minus host_ns (on Linux and macOS, one clock in every process). On Windows
before Python 3.13 that clock moves in steps of 15.6 ms, so latencies from
host times are that coarse there; the summary's inference and 3D times are
timed with time.perf_counter_ns, precise everywhere. The summary
gives it from the set's last frame (the earliest the set could be read) and
its first.
"""

import argparse, json, math, os, queue, signal, sys, threading, time
from datetime import datetime
import numpy as np
from campy import livefeed
from campy.frametimes import GrowingNpy, Writer

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
					except FileNotFoundError:
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
				except FileNotFoundError:
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
		self.nodes, self.nodeNames = None, None
		self.layer, self.text = self.Build(args)

	def Fp16(self, backends):
		# fp16 autocast inside sleap-nn's TorchBackend (cuda and ROCm only)
		if self.precision == "fp16" and self.device.startswith("cuda"):
			for backend in backends:
				backend.use_fp16 = True

	def Upload(self, host):
		return host.to(self.device, non_blocking=True)

	def Predict(self, frames):
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
		# the centroid model's output (FakeCentroids).
		frames = self.Upload(host)
		for _ in range(3):
			self.Predict(frames)
		for counts in WarmCounts(self.n_frames, self.instances, crops):
			with FakeCentroids(self.layer.centroid_layer, counts):
				self.Predict(frames)
		Synchronize(self.device)


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
		return "{} ({}, {}, {}, max {} animals)".format(self.text, self.device, self.precision, self.memoryFormat,
			self.instances)


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
		self.Fp16([layer.centroid_layer.backend, layer.centered_instance_layer.backend])
		if self.memoryFormat == "channels_last":
			for backend in (layer.centroid_layer.backend, layer.centered_instance_layer.backend):
				backend.model.to(memory_format=self.torch.channels_last)
		self.crop = tuple(int(x) for x in layer.crop_size)
		text = "trained: centroid {}, instance {}".format(args.centroid, args.instance)
		return layer, text

	def Describe(self):
		return "{} ({}, {}, {}, max {} animals)".format(self.text, self.device, self.precision, self.memoryFormat,
			self.instances)


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
				pose3d_ns, pose3d_pc = time.monotonic_ns(), time.perf_counter_ns()
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
		("read_ns", "<i8"), ("done_ns", "<i8"), ("skipped", "<i4"), ("found", "<i2", (c,)),
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
		self.rows = []
		self.every = max(1, int(round(frameRate or 60)))
		self.frameRate = frameRate
		self.latency_last, self.latency_first, self.read, self.predict, self.spread = [], [], [], [], []
		self.sets, self.skipped, self.misaligned = 0, 0, 0
		self.first = self.lastFrame = None
		self.started = None # when the first set was read

	def Row(self, frames, skipped, read_ns, done_ns, result):
		# One set's row: its frames, and the model's results
		row = np.zeros((), self.rows_type)
		host = [f.hostTime for f in frames]
		row["frame"] = frames[0].frameNumber
		row["host_ns"] = host
		row["written_ns"] = [f.written for f in frames]
		row["stamp"] = [f.timeStamp for f in frames]
		row["read_ns"], row["done_ns"], row["skipped"] = read_ns, done_ns, skipped
		for key in ("centroids", "centroid_vals", "keypoints", "peak_vals"):
			row[key] = Fit(result.get(key), row[key].shape)
		row["found"] = (~np.isnan(row["centroids"]).any(-1)).sum(-1)
		return row

	def Add(self, row, frames, poses=None, pose3d_ns=None, durations=None):
		# The row, with its 3D poses (pose3d.Poses) when they were made.
		# durations: (inference, 3D) in ns, timed with time.perf_counter_ns:
		# on Windows before Python 3.13, time.monotonic_ns moves in 15.6 ms steps
		if poses is not None:
			row["pose3d_ns"] = pose3d_ns
			row["points3d"] = poses.points3d
			row["members"] = poses.members
			row["n_views"] = poses.n_views
			row["reproj_px"] = poses.reproj_px
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
	if args.nice:
		try:
			os.nice(args.nice)
		except (AttributeError, OSError) as e:
			Say("Could not lower this program's priority (--nice): {}".format(e))

	cameras = Cameras(names, poll=args.poll_ms / 1e3)
	log = worker = None
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
		if args.calibration:
			from campy import pose3d
		if not cameras.Attach(wait=args.wait_sec, stop=stop):
			Say("No live feed: stopped." if stop() else "No live feed after {:g} s.".format(args.wait_sec))
			return 1
		Say("Live feed: {}.".format(cameras.Describe()))
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
		log = Log(out + ".npy", len(names), model.instances, model.nodes or args.nodes, frameRate,
			tracker.animals if tracker else 0)
		worker = Pose3DThread(tracker.tracker, log) if tracker else None
		Say("Logging to {}.npy.".format(out))
		reported, since = time.monotonic(), 0
		while True:
			got = cameras.NextSet(host.array, stop=stop)
			if got is None:
				break
			frames, skipped = got
			read_ns, read_pc = time.monotonic_ns(), time.perf_counter_ns()
			result = model.Predict(model.Upload(host.tensor)) # ends with the results on the host
			done_ns, done_pc = time.monotonic_ns(), time.perf_counter_ns()
			row = log.Row(frames, skipped, read_ns, done_ns, result)
			if worker is None:
				log.Add(row, frames, durations=(done_pc - read_pc, None))
			else:
				worker.Put(row, frames, done_pc - read_pc) # its 3D while the model runs on the next set
			if args.report_sec and time.monotonic() - reported >= args.report_sec:
				printer.Do(Say, Line(log.Summary(since), log))
				reported, since = time.monotonic(), len(log.latency_last)
		if worker is not None:
			worker.Close()
		log.Close()
		printer.Close()
		summary = log.Summary()
		summary.update(model=model.Describe(), nodes=getattr(model, "nodeNames", None),
			pose3d=tracker.Describe() if tracker else None, cameras=[r.cameraName for r in cameras.readers],
			rings=names, frame_shape=list(shape), frame_rate=frameRate, log=out + ".npy",
			ended="stopped (Ctrl-C)" if stop() else "the recording ended")
		with open(out + ".json", "x") as f:
			json.dump(summary, f, indent=2)
		Say("Done, {}: {}".format(summary["ended"], Line(summary, log)))
		Say("Summary in {}.json.".format(out))
		return 0
	finally:
		# Stopped by an error too: the rows and lines still queued are written
		try:
			if worker is not None:
				worker.Stop()
			if log is not None:
				log.Close()
			printer.Close()
		finally:
			cameras.Close()
			signal.signal(signal.SIGINT, previous)


def Prepare(args, n_frames, shape, dtype):
	# The model, for sets of n_frames frames of shape and dtype, warmed up, and
	# the host buffer the sets are copied into
	model = MODELS[args.model](args, n_frames, shape)
	host = HostBuffer(getattr(model, "torch", None) is not None, (n_frames,) + tuple(shape), dtype)
	if hasattr(model, "Warm"):
		t = time.perf_counter()
		model.Warm(host.tensor, crops=range(1, n_frames * args.max_instances + 1) if args.model == "sleap" else ())
		Say("Model: {}. Warmed up in {:.1f} s.".format(model.Describe(), time.perf_counter() - t))
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


def ParseArgs(argv=None):
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
	p.add_argument("--wait-sec", type=float, default=float("inf"), help="How long to wait for the feed.")
	p.add_argument("--poll-ms", type=float, default=0.5, help="How often to look for a new set.")
	p.add_argument("--report-sec", type=float, default=10, help="Say how it goes this often; 0: only at the end.")
	p.add_argument("--nice", type=int, default=0, help="Lower this program's CPU priority by this much (POSIX).")
	return p.parse_args(argv)


def Main(argv=None):
	sys.exit(Run(ParseArgs(argv)))


if __name__ == "__main__":
	Main()
