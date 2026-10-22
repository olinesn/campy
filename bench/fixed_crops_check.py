"""
campy-live --fixed-crops against campy-live's default (sleap-nn's own
TopDownLayer), set by set, on recorded videos: the animals found per camera,
and how far apart the keypoints are. Also each way's time per set (upload,
model, results back), with nothing else running: the --fixed-crops graph
(--cuda-graphs), the same without a graph, and the default.

sleap-nn 0.3.3 takes the crop size from the first crop's corners in float32,
int(abs(right - left)) + 1, which for some centroids gives a pixel less (319
for 320 px crops), and then cuts every crop of the set that size: the
instance model sees those crops resized a little differently. --fixed-crops
always cuts the crop size. So the keypoints are compared with the default
twice: as it is, and with that size made exact (ExactCropSize), which is
sleap-nn's layer otherwise unchanged; and the sets where sleap-nn's crops
came out short are counted.

    python bench/fixed_crops_check.py --centroid <model> --instance <model> \
        Camera1.mp4 Camera3.mp4 Camera4.mp4 [--sets 3600] [--out check.json]

The videos are decoded with ffmpeg, in gray, frame k of each making set k,
as campy's cameras would give them. --engine compile (or tensorrt) runs the
--fixed-crops graph's networks as compiled engines: then the graph is
compared with the default, and with --fixed-crops in torch without a graph
(graph_vs_eager_fixed_max_abs_diff_px: the engines' own difference).
Run it on the rig through its lock.
"""
import argparse, json, subprocess, sys, time
import numpy as np
from campy import live


def Frames(videos, shape):
	# The sets, one array (cameras, H, W, 1) at a time
	h, w = shape
	procs = [subprocess.Popen(["ffmpeg", "-v", "error", "-i", v, "-f", "rawvideo", "-pix_fmt", "gray", "-"],
		stdout=subprocess.PIPE, bufsize=h * w * 4) for v in videos]
	try:
		while True:
			out = np.empty((len(videos), h, w, 1), np.uint8)
			for c, p in enumerate(procs):
				b = p.stdout.read(h * w)
				if len(b) < h * w:
					return
				out[c, ..., 0] = np.frombuffer(b, np.uint8).reshape(h, w)
			yield out
	finally:
		for p in procs:
			p.kill()


class ExactCropSize:
	"""
	sleap-nn's crop_bboxes (in its top-down layer) with the crop size made
	exact, while on: the first box's far corners moved half a pixel further,
	so its size rounds right (the top-lefts, which place the crops, as they
	were). Counts the calls where sleap-nn's own size was short.
	"""

	def __init__(self):
		import sleap_nn.inference.layers.topdown as topdown
		self.topdown, self.original = topdown, topdown.crop_bboxes
		self.on, self.short = False, 0
		topdown.crop_bboxes = self

	def __call__(self, images, bboxes, sample_inds):
		import torch
		if len(bboxes):
			fixed = bboxes.clone()
			fixed[0, 3, 1] = bboxes[0, 0, 1] + (torch.round(bboxes[0, 3, 1] - bboxes[0, 0, 1]) + 0.5)
			fixed[0, 1, 0] = bboxes[0, 0, 0] + (torch.round(bboxes[0, 1, 0] - bboxes[0, 0, 0]) + 0.5)
			if not self.on:
				size = (bboxes[0, 3, 1] - bboxes[0, 0, 1]).abs().to(torch.long), (bboxes[0, 1, 0] - bboxes[0, 0, 0]).abs().to(torch.long)
				exact = torch.round(bboxes[0, 3, 1] - bboxes[0, 0, 1]).to(torch.long), torch.round(bboxes[0, 1, 0] - bboxes[0, 0, 0]).to(torch.long)
				self.short += int(any(bool(a != b) for a, b in zip(size, exact)))
			else:
				bboxes = fixed
		return self.original(images, bboxes, sample_inds)


def Compare(g, d):
	# (animals found differ, keypoint max abs diff, keypoints missing in one only)
	fg = (~np.isnan(g["centroids"]).any(-1)).sum(-1)
	fd = (~np.isnan(d["centroids"]).any(-1)).sum(-1)
	if (fg != fd).any():
		return True, None, None
	kg, kd = g["keypoints"], d["keypoints"]
	both = np.isfinite(kg) & np.isfinite(kd)
	return False, float(np.abs(kg - kd)[both].max(initial=0)), int(np.count_nonzero(np.isnan(kg) != np.isnan(kd)))


class Keypoints:
	"""
	One way's keypoints against the reference's, keypoint by keypoint, in
	the sets where both found the same animals per camera: how far each
	moved (the distance, px), and the sets where the animals found differ.
	"""

	def __init__(self):
		self.moved, self.found_differs, self.sets = [], 0, 0

	def Add(self, a, ref):
		self.sets += 1
		if (Found(a) != Found(ref)).any():
			self.found_differs += 1
			return
		if not Found(ref).sum(): # no animals: sleap-nn guesses the node count
			return
		d = np.linalg.norm(a["keypoints"] - ref["keypoints"], axis=-1)
		self.moved.append(d[np.isfinite(d)])

	def Summary(self):
		d = np.concatenate(self.moved) if self.moved else np.zeros(0)
		return {"sets": self.sets, "sets_where_found_differs": self.found_differs, "keypoints_compared": int(d.size),
			"moved_px_p50": float(np.percentile(d, 50)) if d.size else 0.0,
			"moved_px_p99": float(np.percentile(d, 99)) if d.size else 0.0,
			"moved_px_max": float(d.max()) if d.size else 0.0,
			"moved_over_0.1px": float(np.mean(d > 0.1)) if d.size else 0.0,
			"moved_over_1px": float(np.mean(d > 1)) if d.size else 0.0,
			"moved_over_5px": float(np.mean(d > 5)) if d.size else 0.0}


def Found(r):
	return (~np.isnan(r["centroids"]).any(-1)).sum(-1)


def Ms(values):
	v = np.asarray(values, np.float64) / 1e6
	return {"p50": float(np.percentile(v, 50)), "p99": float(np.percentile(v, 99)), "max": float(v.max()),
		"mean": float(v.mean())} if len(v) else {}


def Main(argv=None):
	p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
	p.add_argument("videos", nargs="+")
	p.add_argument("--centroid", required=True)
	p.add_argument("--instance", required=True)
	p.add_argument("--frame-shape", default="1024x1280")
	p.add_argument("--max-instances", type=int, default=5)
	p.add_argument("--precision", default="fp16")
	p.add_argument("--sets", type=int, default=0, help="0: every set of the videos")
	p.add_argument("--engine", default="eager", help="the --fixed-crops graph's networks: campy-live --engine (eager, "
		"compile, tensorrt, auto); the --fixed-crops run without a graph stays in torch, as a reference")
	p.add_argument("--engine-cache", default="")
	p.add_argument("--reference", default="same", choices=["same", "fp32"], help="the default (sleap-nn's layer) it "
		"is compared with: at --precision, or in fp32, the most exact (then keypoint_moved also says how far fp16 "
		"itself moves them, with --fixed-crops in torch without a graph)")
	p.add_argument("--out", default="")
	a = p.parse_args(argv)
	h, w = [int(x) for x in a.frame_shape.split("x")][:2]
	n = len(a.videos)
	args = live.ParseArgs(["--model", "sleap", "--centroid", a.centroid, "--instance", a.instance, "--fixed-crops",
		"--cuda-graphs", "--max-instances", str(a.max_instances), "--precision", a.precision,
		"--frame-shape", "{}x{}x1".format(h, w), "--engine", a.engine, "--engine-cache", a.engine_cache])
	model = live.TrainedTopDown(args, n, (h, w, 1))
	host = live.HostBuffer(True, (n, h, w, 1), np.uint8)
	graphed = model.fixed
	eager = live.FixedCrops(model.layer, n, model.device, a.max_instances, graph=False)

	backends = [model.layer.centroid_layer.backend, model.layer.centered_instance_layer.backend]
	fp16 = [b.use_fp16 for b in backends]

	def Default(frames):
		model.fixed = None
		if a.reference == "fp32":
			for b in backends:
				b.use_fp16 = False
		try:
			return model.Predict(frames)
		finally:
			model.fixed = graphed
			for b, f in zip(backends, fp16):
				b.use_fp16 = f

	t = time.perf_counter()
	model.Warm(host.tensor) # records the graph
	model.fixed = None
	if a.reference == "fp32":
		for b in backends:
			b.use_fp16 = False
	model.Warm(host.tensor, crops=range(1, n * a.max_instances + 1)) # the default's shapes
	for b, f in zip(backends, fp16):
		b.use_fp16 = f
	model.fixed = graphed
	for _ in range(3):
		eager.Predict(eager.Upload(host.tensor))
	print("warmed up in {:.1f} s; {}".format(time.perf_counter() - t, model.DescribeGraphs()), flush=True)

	exact = ExactCropSize()
	times = {"fixed_graph": [], "fixed_eager": [], "default": []}
	by_found = {}
	diffs = {"as_is": [], "exact_crop_size": []}
	diff_found = {"as_is": [], "exact_crop_size": []} # animals found in the sets of diffs
	found_diff = {"as_is": 0, "exact_crop_size": 0}
	missing = {"as_is": 0, "exact_crop_size": 0}
	cen_diff, eager_diff, found_total = [], [], []
	moved = {"fixed_graph": Keypoints(), "fixed_eager_torch": Keypoints()} # vs the exact-crop-size reference
	sets = 0
	for frames in Frames(a.videos, (h, w)):
		host.array[:] = frames
		t0 = time.perf_counter_ns()
		g = model.Predict(model.Upload(host.tensor))
		t1 = time.perf_counter_ns()
		e = eager.Predict(eager.Upload(host.tensor))
		t2 = time.perf_counter_ns()
		on_gpu = host.tensor.to(model.device, non_blocking=True)
		d = Default(on_gpu)
		t3 = time.perf_counter_ns()
		exact.on = True
		x = Default(on_gpu)
		exact.on = False
		moved["fixed_graph"].Add(g, x)
		moved["fixed_eager_torch"].Add(e, x)
		times["fixed_graph"].append(t1 - t0)
		times["fixed_eager"].append(t2 - t1)
		times["default"].append(t3 - t2)
		total = int((~np.isnan(d["centroids"]).any(-1)).sum())
		found_total.append(total)
		by_found.setdefault(total, {"default": [], "fixed_graph": []})
		by_found[total]["default"].append(t3 - t2)
		by_found[total]["fixed_graph"].append(t1 - t0)
		for name, ref in (("as_is", d), ("exact_crop_size", x)):
			differs, kp, miss = Compare(g, ref)
			found_diff[name] += int(differs)
			if not differs:
				if total:
					diffs[name].append(kp)
					diff_found[name].append(total)
				missing[name] += miss
		if not Compare(g, d)[0]:
			cen_diff.append(float(np.nanmax(np.abs(g["centroids"] - d["centroids"]), initial=0)))
		eager_diff.append(float(np.nanmax(np.abs(g["keypoints"] - e["keypoints"]), initial=0)))
		sets += 1
		if sets % 600 == 0:
			print("{} sets: found differs in {} / {}; keypoints max diff {:.4g} px as is, {:.4g} with the exact crop "
				"size; sleap-nn's crops short in {} sets".format(sets, found_diff["as_is"], found_diff["exact_crop_size"],
				max(diffs["as_is"], default=0), max(diffs["exact_crop_size"], default=0), exact.short), flush=True)
		if a.sets and sets >= a.sets:
			break

	def Diffs(name):
		kp, f = np.asarray(diffs[name]), np.asarray(diff_found[name])
		return {"sets_where_found_differs": found_diff[name],
			"keypoint_max_abs_diff_px": float(kp.max()) if len(kp) else 0.0,
			"keypoint_p99_of_set_max_diff_px": float(np.percentile(kp, 99)) if len(kp) else 0.0,
			"keypoint_median_of_set_max_diff_px": float(np.median(kp)) if len(kp) else 0.0,
			"sets_with_keypoint_diff_over_0.01px": int(np.count_nonzero(kp > 0.01)),
			"keypoints_missing_in_one_only": missing[name],
			"by_animals_found": {str(k): {"sets": int(np.count_nonzero(f == k)),
				"sets_with_keypoint_diff_over_0.01px": int(np.count_nonzero(kp[f == k] > 0.01)),
				"keypoint_max_abs_diff_px": float(kp[f == k].max(initial=0))} for k in np.unique(f)}}

	summary = {
		"sets": sets, "videos": a.videos, "centroid": a.centroid, "instance": a.instance, "precision": a.precision,
		"engine": a.engine, "reference": a.reference if a.reference == "fp32" else a.precision,
		"keypoint_moved_vs_reference_exact_crop_size": {k: v.Summary() for k, v in moved.items()},
		"graph": model.DescribeGraphs(),
		"found_per_set_mean": float(np.mean(found_total)) if found_total else 0.0,
		"centroid_max_abs_diff_px": max(cen_diff, default=0.0),
		"vs_default_as_is": Diffs("as_is"),
		"vs_default_exact_crop_size": Diffs("exact_crop_size"),
		"sets_where_sleap_nn_crops_were_short": exact.short,
		"graph_vs_eager_fixed_max_abs_diff_px": max(eager_diff, default=0.0),
		"time_ms": {k: Ms(v) for k, v in times.items()},
		"time_ms_by_animals_found": {str(k): {m: Ms(v) for m, v in d.items()} | {"sets": len(d["default"])}
			for k, d in sorted(by_found.items())},
	}
	text = json.dumps(summary, indent=2)
	print(text)
	if a.out:
		with open(a.out, "w") as f:
			f.write(text)
	return 0 if found_diff["exact_crop_size"] == 0 else 1


if __name__ == "__main__":
	sys.exit(Main())
