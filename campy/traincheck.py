"""
campy-train-check: read a sleap-nn training config before a long run, and say
which of its settings will cost hours or change what the model sees.

	campy-train-check centroid.yaml centered_instance.yaml
	campy-train-check centered_instance.yaml --labels project.pkg.slp --like models/<an earlier run>

The SLEAP GUI's Run Training dialog doesn't show most of these, and a config
reused from an earlier run carries them along (docs/models.md, "A large
package from the SLEAP GUI"). It checks:

  workers         no training or validation workers (one process prepares
                  every image); fewer validation workers than training ones
                  when the images are read as they are (torch_dataset), which
                  made validation 175 of each 285 s on a 30,000-frame package
  data pipeline   "Cache in Memory" that won't fit: sleap-nn then switches to a
                  JPEG disk cache, with only a line in its log, and the model
                  trains on compressed copies (on Windows every worker holds
                  its own copy of the cache); a disk cache is JPEG too
  frame size      max_height / max_width left empty: they come from the largest
                  frame, and smaller frames are enlarged to it, so a crop
                  covers less of the animal and augmentation costs more
  steps           train_steps_per_epoch left empty: an epoch is every training
                  sample, which on a large package is hours per epoch; and
                  validation that takes more steps than training
  early stopping  off, or a min_delta so small that any change counts as
                  progress (the run then goes on for hours after it stopped
                  getting better)
  crop            with --labels, for a centered instance model: how many
                  labelled animals reach outside the crop, as the GUI's
                  "clipped" warning counts them
  run folder      already there (training would refuse or mix two runs)

--labels reads the labelled frames' sizes and points (not the images): a few
seconds and about 150 MB for a 30,000-frame package. Without it, the checks
that need the labels say so. --like reads an earlier run's training_log.csv
and suggests a min_delta of 0.5% of its best validation loss: the loss's scale
depends on the model type, sigma and output stride, so there's no one number.

Prints one line per check (ok, WARN, FAIL) and exits 0 when all are ok, 1 when
something warns, 2 when something fails (training would fail or waste most of
its time), 3 when a config can't be read.
"""

import argparse, collections, csv, math, os, sys

import yaml

OK, WARN, FAIL = "ok", "WARN", "FAIL"

# Instance-level models: a training sample is one animal, not one frame.
PER_INSTANCE = ("centered_instance", "multi_class_topdown", "centered_instance_segmentation")
BIG_EPOCH = 5000 # steps
TINY_DELTA = 1e-7
LIKE_SHARE = 0.005 # suggested min_delta, as a share of an earlier run's best validation loss


class CheckError(Exception):
	pass


def Get(cfg, path, default=None):
	node = cfg
	for key in path.split("."):
		if not isinstance(node, dict) or key not in node:
			return default
		node = node[key]
	return default if node is None else node


def ReadConfig(path):
	try:
		with open(path) as f:
			cfg = yaml.safe_load(f)
	except (OSError, yaml.YAMLError) as e:
		raise CheckError("can't read {}: {}".format(path, e))
	if not isinstance(cfg, dict) or "trainer_config" not in cfg:
		raise CheckError("{} isn't a sleap-nn training config (no trainer_config)".format(path))
	return cfg


def ModelType(cfg):
	heads = Get(cfg, "model_config.head_configs", {}) or {}
	used = [name for name, head in heads.items() if head]
	return used[0] if len(used) == 1 else None


def LabelStats(path):
	"""What the checks need from a labels file: each user-labelled frame's size, and each user instance's points."""
	import sleap_io as sio
	labels = sio.load_slp(path, open_videos=False)
	shapes = {}
	for video in labels.videos:
		try:
			shape = video.shape
		except Exception:
			shape = None
		shapes[id(video)] = tuple(shape[1:3]) if shape else None
	sizes = collections.Counter()
	points = []
	frames = 0
	for lf in labels.labeled_frames:
		users = lf.user_instances
		if not users:
			continue
		frames += 1
		size = shapes.get(id(lf.video))
		if size:
			sizes[size] += 1
		for inst in users:
			points.append((size, inst.numpy()))
	nodes = list(labels.skeletons[0].node_names) if labels.skeletons else []
	return dict(frames=frames, instances=len(points), sizes=sizes, points=points, nodes=nodes)


def BestLoss(run_folder):
	path = os.path.join(run_folder, "training_log.csv") if os.path.isdir(run_folder) else run_folder
	try:
		with open(path, newline="") as f:
			losses = [float(r["val/loss"]) for r in csv.DictReader(f) if r.get("val/loss")]
	except (OSError, ValueError, KeyError) as e:
		raise CheckError("can't read the validation loss from {}: {}".format(path, e))
	losses = [v for v in losses if math.isfinite(v)]
	if not losses:
		raise CheckError("{} has no validation loss".format(path))
	return min(losses)


def Fit(size, max_hw):
	"""How much sleap-nn resizes a frame of this size to fit max_height x max_width (1 = not at all)."""
	(h, w), (mh, mw) = size, max_hw
	return min(mh / h, mw / w)


def Clipped(stats, crop, max_hw, anchor):
	"""Labelled animals with a visible point outside a crop centred on the anchor (or the points' middle)."""
	import numpy as np
	a = stats["nodes"].index(anchor) if anchor in stats["nodes"] else None
	clipped, per_node, counted = 0, collections.Counter(), 0
	for size, pts in stats["points"]:
		if size is None:
			continue
		seen = np.all(np.isfinite(pts), axis=1)
		if not seen.any():
			continue
		counted += 1
		centre = pts[a] if a is not None and seen[a] else pts[seen].mean(axis=0)
		half = crop / 2 / (Fit(size, max_hw) if max_hw else 1.0)
		out = seen & np.any(np.abs(pts - centre) > half, axis=1)
		if out.any():
			clipped += 1
			for i in np.flatnonzero(out):
				per_node[stats["nodes"][i] if i < len(stats["nodes"]) else str(i)] += 1
	return clipped, counted, per_node


def Check(cfg, stats=None, windows=None, mem_free=None, like_loss=None):
	"""[(level, text)] for one config."""
	windows = os.name == "nt" if windows is None else windows
	out = []
	add = lambda level, text: out.append((level, text))
	kind = ModelType(cfg)
	tw = int(Get(cfg, "trainer_config.train_data_loader.num_workers", 0))
	vw = int(Get(cfg, "trainer_config.val_data_loader.num_workers", 0))
	tb = int(Get(cfg, "trainer_config.train_data_loader.batch_size", 1))
	vb = int(Get(cfg, "trainer_config.val_data_loader.batch_size", 1))
	pipeline = Get(cfg, "data_config.data_pipeline_fw", "torch_dataset")
	vf = float(Get(cfg, "data_config.validation_fraction", 0.1))
	has_val_file = bool(Get(cfg, "data_config.val_labels_path"))
	mh, mw = Get(cfg, "data_config.preprocessing.max_height"), Get(cfg, "data_config.preprocessing.max_width")
	need = "(needs --labels to check)"

	# Workers
	if tw == 0:
		add(FAIL, "train_data_loader.num_workers is 0: one process prepares every image and the GPU waits "
			"(an epoch took 8.5 times as long with 0 as with 12). Set it to 8-12.")
	if vw == 0:
		add(FAIL, "val_data_loader.num_workers is 0: validation runs in one process. Set it like the training workers.")
	if tw and vw and pipeline == "torch_dataset" and vw < tw:
		add(WARN, "val_data_loader.num_workers is {} against {} for training, with the images read as they are "
			"(torch_dataset): validation will be slow (4 workers took 175 of each 285 s on a 30,000-frame package). "
			"Set it to {}.".format(vw, tw, tw))
	elif tw and vw:
		add(OK, "workers: {} training, {} validation".format(tw, vw))

	# Data pipeline
	if pipeline == "torch_dataset_cache_img_disk":
		add(WARN, "data_pipeline_fw is torch_dataset_cache_img_disk: sleap-nn writes the frames as JPEG files, so the "
			"model trains on compressed copies. torch_dataset reads the original images.")
	elif pipeline == "torch_dataset_cache_img_memory":
		copies = 1 + max(tw, vw) if windows else 1
		if stats and stats["sizes"]:
			chans = 3 if Get(cfg, "data_config.preprocessing.ensure_rgb", False) else 1
			raw = sum(h * w * chans * n for (h, w), n in stats["sizes"].items())
			if mem_free is None:
				import psutil
				mem_free = psutil.virtual_memory().available
			gb = lambda b: "{:.0f} GB".format(b / 1e9)
			if raw * copies > mem_free:
				add(WARN, "Cache in Memory needs about {} ({} of images{}) against {} free: sleap-nn will switch to a "
					"JPEG disk cache, with only a line in its log, and the model will train on compressed copies. "
					"Set data_pipeline_fw: torch_dataset to read the original images.".format(gb(raw * copies), gb(raw),
					", once per worker on Windows" if windows else "", gb(mem_free)))
			else:
				add(OK, "Cache in Memory: about {} needed, {} free".format(gb(raw * copies), gb(mem_free)))
		else:
			add(WARN, "Cache in Memory: if the images don't fit{}, sleap-nn switches to a lossy JPEG disk cache "
				"with only a line in its log {}".format(" (once per worker on Windows)" if windows else "", need))
	else:
		add(OK, "data pipeline: {}".format(pipeline))

	# Frame size
	common = stats["sizes"].most_common(1)[0] if stats and stats["sizes"] else None
	if mh is None or mw is None:
		if common:
			biggest = max(stats["sizes"], key=lambda s: s[0] * s[1])
			k = Fit(common[0], biggest)
			if k > 1.0001:
				add(WARN, "max_height / max_width are empty, so they come from the largest frame, {}x{}: the most "
					"common frames, {}x{} ({:.0%}), are enlarged {:.2f} times, and a crop covers only {:.0%} as much "
					"of the animal. Set max_height: {}, max_width: {}.".format(biggest[0], biggest[1], common[0][0],
					common[0][1], common[1] / stats["frames"], k, 1 / k, common[0][0], common[0][1]))
			else:
				add(OK, "frame size: every frame {}x{}".format(*common[0]))
		else:
			add(WARN, "max_height / max_width are empty: they come from the largest frame, and smaller frames are "
				"enlarged to it {}".format(need))
	elif common:
		bigger = sum(n for (h, w), n in stats["sizes"].items() if h > mh or w > mw)
		add(OK, "frame size: max {}x{}{}".format(mh, mw, ", {} labelled frames ({:.1%}) are larger and get shrunk"
			.format(bigger, bigger / stats["frames"]) if bigger else ""))
	else:
		add(OK, "frame size: max {}x{}".format(mh, mw))

	# Steps per epoch, and validation's share
	steps = Get(cfg, "trainer_config.train_steps_per_epoch")
	epochs = int(Get(cfg, "trainer_config.max_epochs", 100))
	samples = None
	if stats:
		samples = stats["instances"] if kind in PER_INSTANCE else stats["frames"]
	if steps is None:
		if samples is not None:
			full = math.ceil(samples * (1 - (0 if has_val_file else vf)) / tb)
			full = max(full, int(Get(cfg, "trainer_config.min_train_steps_per_epoch", 0)))
			add(WARN if full > BIG_EPOCH else OK, "train_steps_per_epoch is empty: an epoch is every training sample, "
				"{:,} steps; up to {} epochs is {:,} steps{}".format(full, epochs, full * epochs,
				". Set train_steps_per_epoch (2000 worked on a 30,000-frame package)" if full > BIG_EPOCH else ""))
			steps = full
		else:
			add(WARN, "train_steps_per_epoch is empty: an epoch is every training sample, hours on a large package "
				"{}".format(need))
	else:
		add(OK, "train_steps_per_epoch: {:,}, up to {} epochs".format(int(steps), epochs))
	if samples is not None and steps and not has_val_file:
		vsteps = math.ceil(samples * vf / vb)
		if vsteps > steps:
			add(WARN, "validation is {:,} steps against {:,} training steps per epoch: most of each epoch will be "
				"validation. More validation workers help; a smaller validation_fraction makes the validation loss, "
				"which steers the learning rate and the stopping, noisier.".format(vsteps, int(steps)))

	# Early stopping
	es = Get(cfg, "trainer_config.early_stopping", {}) or {}
	delta, patience = float(es.get("min_delta") or 0), es.get("patience")
	suggest = " For this model, about {:.1g} (0.5% of the earlier run's best validation loss, {:.3g}).".format(
		like_loss * LIKE_SHARE, like_loss) if like_loss else " --like <an earlier run of this model> suggests a value."
	if not es.get("stop_training_on_plateau"):
		add(WARN, "early stopping is off: every one of the {} epochs runs.".format(epochs))
	elif delta < TINY_DELTA:
		add(WARN, "early_stopping.min_delta is {:g}: any change in the validation loss counts as progress, so training "
			"can go on for hours after it stopped getting better (a centered instance model trained 5 more hours "
			"for mOKS 0.840 -> 0.841). Set it near 0.5% of the validation loss you expect, with patience 10.{}"
			.format(delta, suggest))
	else:
		add(OK, "early stopping: min_delta {:g}, patience {}".format(delta, patience))

	# Crop
	crop = Get(cfg, "data_config.preprocessing.crop_size")
	if kind in PER_INSTANCE:
		scale = float(Get(cfg, "data_config.preprocessing.scale", 1.0))
		if not crop:
			add(OK, "crop size: worked out from the labels by sleap-nn")
		elif not stats:
			add(OK, "crop {} px: clipping not checked {}".format(crop, need))
		elif abs(scale - 1) > 1e-6:
			add(OK, "crop {} px: clipping not checked (input scale {:g}, only scale 1 is)".format(crop, scale))
		else:
			max_hw = (mh, mw) if mh and mw else max(stats["sizes"], key=lambda s: s[0] * s[1]) if stats["sizes"] else None
			anchor = Get(cfg, "model_config.head_configs.{}.confmaps.anchor_part".format(kind))
			n, counted, nodes = Clipped(stats, crop, max_hw, anchor)
			share = n / counted if counted else 0
			text = "crop {} px: {:,} of {:,} labelled animals ({:.1%}) reach outside it".format(crop, n, counted, share)
			if nodes:
				text += ", most often " + ", ".join("{} ({:,})".format(k, v) for k, v in nodes.most_common(3))
			add(WARN if share > 0.05 else OK, text)

	# Run folder
	ckpt, run = Get(cfg, "trainer_config.ckpt_dir"), Get(cfg, "trainer_config.run_name")
	folder = os.path.normpath(os.path.join(ckpt, run)) if ckpt and run else None
	if folder and os.path.exists(folder):
		add(FAIL, "the run folder already exists: {}".format(folder))
	return out


def Main(argv=None):
	p = argparse.ArgumentParser(prog="campy-train-check", description="Check sleap-nn training configs before a long "
		"run (python -m pydoc campy.traincheck).")
	p.add_argument("configs", nargs="+", help="sleap-nn training config(s) (.yaml).")
	p.add_argument("--labels", default="", help="The labels (.slp or .pkg.slp) to check against. Default: the first "
		"config's train_labels_path, if it exists.")
	p.add_argument("--no-labels", action="store_true", help="Don't read the labels.")
	p.add_argument("--like", default="", help="An earlier run's folder (or its training_log.csv), to suggest an "
		"early-stopping min_delta from. Give one per config, comma-separated, in the same order, or one for all.")
	args = p.parse_args(argv)
	try:
		cfgs = [ReadConfig(c) for c in args.configs]
		likes = [s for s in args.like.split(",") if s] if args.like else []
		if len(likes) not in (0, 1, len(cfgs)):
			raise CheckError("--like: give one earlier run, or one per config")
		like_loss = [BestLoss(l) for l in likes]
		labels = args.labels
		if not labels and not args.no_labels:
			paths = Get(cfgs[0], "data_config.train_labels_path") or []
			paths = [paths] if isinstance(paths, str) else paths
			labels = paths[0] if len(paths) == 1 and os.path.isfile(paths[0]) else ""
		stats = None
		if labels and not args.no_labels:
			print("Reading {} ...".format(labels), flush=True)
			stats = LabelStats(labels)
			print("  {:,} labelled frames, {:,} labelled animals".format(stats["frames"], stats["instances"]))
	except CheckError as e:
		print("campy-train-check: {}".format(e), file=sys.stderr)
		return 3
	worst = 0
	for i, (path, cfg) in enumerate(zip(args.configs, cfgs)):
		loss = like_loss[0] if len(like_loss) == 1 else like_loss[i] if like_loss else None
		print("\n{} ({})".format(path, ModelType(cfg) or "model type?"))
		for level, text in Check(cfg, stats, like_loss=loss):
			print("  {:4}  {}".format(level, text))
			worst = max(worst, {OK: 0, WARN: 1, FAIL: 2}[level])
	print("\n" + ["All clear.", "Warnings above: training would run, but check them first.",
		"Failures above: fix them before training."][worst])
	return worst


if __name__ == "__main__":
	sys.exit(Main())
