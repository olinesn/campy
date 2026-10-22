"""
campy-live --fixed-crops against campy-live's default (sleap-nn's own
TopDownLayer), set by set, on recorded videos: the animals found per camera,
and how far apart the keypoints are. Also each way's time per set (upload,
model, results back), with nothing else running: the --fixed-crops graph
(--cuda-graphs), the same without a graph, and the default.

sleap-nn 0.3.3 takes the crop size from the first crop's corners in float32,
int(abs(right - left)) + 1, which for some centroids gives a pixel less (319
for 320 px crops), and then cuts every crop of the set that size
(talmolab/sleap-nn#778). campy-live corrects that for its own layers
(live.ExactCropSize, pull request 131), and --fixed-crops always cuts the
crop size, so both cut the same crops; the sets where sleap-nn alone would
have cut them short are counted (ShortCrops).

    python bench/fixed_crops_check.py --centroid <model> --instance <model> \
        Camera1.mp4 Camera3.mp4 Camera4.mp4 [--sets 3600] [--out check.json]

The videos are decoded with ffmpeg, in gray, frame k of each making set k,
as campy's cameras would give them. Run it on the rig through its lock.
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


class ShortCrops:
	"""
	Counts the calls of sleap-nn's crop_bboxes (in its top-down layer) whose
	own crop size, from the boxes as sleap-nn made them, would be short. It
	goes around campy-live's ExactCropSize, which then cuts the real size.
	"""

	def __init__(self):
		import sleap_nn.inference.layers.topdown as topdown
		self.original, self.short = topdown.crop_bboxes, 0
		topdown.crop_bboxes = self

	def __call__(self, images, bboxes, sample_inds):
		import torch
		if len(bboxes):
			sides = bboxes[0, 3, 1] - bboxes[0, 0, 1], bboxes[0, 1, 0] - bboxes[0, 0, 0]
			self.short += int(any(bool(side.abs().to(torch.long) != torch.round(side).to(torch.long)) for side in sides))
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
	p.add_argument("--out", default="")
	a = p.parse_args(argv)
	h, w = [int(x) for x in a.frame_shape.split("x")][:2]
	n = len(a.videos)
	args = live.ParseArgs(["--model", "sleap", "--centroid", a.centroid, "--instance", a.instance, "--fixed-crops",
		"--cuda-graphs", "--max-instances", str(a.max_instances), "--precision", a.precision,
		"--frame-shape", "{}x{}x1".format(h, w)])
	model = live.TrainedTopDown(args, n, (h, w, 1))
	host = live.HostBuffer(True, (n, h, w, 1), np.uint8)
	graphed = model.fixed
	eager = live.FixedCrops(model.layer, n, model.device, a.max_instances, graph=False)

	def Default(frames):
		model.fixed = None
		try:
			return model.Predict(frames)
		finally:
			model.fixed = graphed

	t = time.perf_counter()
	model.Warm(host.tensor) # records the graph
	model.fixed = None
	model.Warm(host.tensor, crops=range(1, n * a.max_instances + 1)) # the default's shapes
	model.fixed = graphed
	for _ in range(3):
		eager.Predict(eager.Upload(host.tensor))
	print("warmed up in {:.1f} s; {}".format(time.perf_counter() - t, model.DescribeGraphs()), flush=True)

	short = ShortCrops()
	times = {"fixed_graph": [], "fixed_eager": [], "default": []}
	by_found = {}
	diffs, diff_found = [], [] # each set's keypoint max diff, and the animals found in it
	found_diff = missing = 0
	cen_diff, eager_diff, found_total = [], [], []
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
		times["fixed_graph"].append(t1 - t0)
		times["fixed_eager"].append(t2 - t1)
		times["default"].append(t3 - t2)
		total = int((~np.isnan(d["centroids"]).any(-1)).sum())
		found_total.append(total)
		by_found.setdefault(total, {"default": [], "fixed_graph": []})
		by_found[total]["default"].append(t3 - t2)
		by_found[total]["fixed_graph"].append(t1 - t0)
		differs, kp, miss = Compare(g, d)
		found_diff += int(differs)
		if not differs:
			if total:
				diffs.append(kp)
				diff_found.append(total)
			missing += miss
			cen_diff.append(float(np.nanmax(np.abs(g["centroids"] - d["centroids"]), initial=0)))
		eager_diff.append(float(np.nanmax(np.abs(g["keypoints"] - e["keypoints"]), initial=0)))
		sets += 1
		if sets % 600 == 0:
			print("{} sets: found differs in {}; keypoints max diff {:.4g} px; sleap-nn alone would have cut short "
				"crops in {} sets".format(sets, found_diff, max(diffs, default=0), short.short), flush=True)
		if a.sets and sets >= a.sets:
			break

	def Diffs():
		kp, f = np.asarray(diffs), np.asarray(diff_found)
		return {"sets_where_found_differs": found_diff,
			"keypoint_max_abs_diff_px": float(kp.max()) if len(kp) else 0.0,
			"keypoint_p99_of_set_max_diff_px": float(np.percentile(kp, 99)) if len(kp) else 0.0,
			"keypoint_median_of_set_max_diff_px": float(np.median(kp)) if len(kp) else 0.0,
			"sets_with_keypoint_diff_over_0.01px": int(np.count_nonzero(kp > 0.01)),
			"keypoints_missing_in_one_only": missing,
			"by_animals_found": {str(k): {"sets": int(np.count_nonzero(f == k)),
				"sets_with_keypoint_diff_over_0.01px": int(np.count_nonzero(kp[f == k] > 0.01)),
				"keypoint_max_abs_diff_px": float(kp[f == k].max(initial=0))} for k in np.unique(f)}}

	summary = {
		"sets": sets, "videos": a.videos, "centroid": a.centroid, "instance": a.instance, "precision": a.precision,
		"graph": model.DescribeGraphs(),
		"found_per_set_mean": float(np.mean(found_total)) if found_total else 0.0,
		"centroid_max_abs_diff_px": max(cen_diff, default=0.0),
		"vs_default": Diffs(),
		"sets_where_sleap_nn_alone_cuts_short": short.short,
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
	return 0 if found_diff == 0 else 1


if __name__ == "__main__":
	sys.exit(Main())
