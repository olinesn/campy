#!/usr/bin/env python3
"""Check CUDA graphs when the number of animals changes from set to set.

sleap_infer_bench.py times one crop count per row. Live, each frame has however
many animals were detected, so the centered-instance model sees a different
batch size from one set to the next, and a CUDA graph only replays the shape it
was captured with. This builds the default top-down pair as
sleap_infer_bench.py --cuda-graphs does, then for each set puts a random
0..max_instances synthetic peaks in each frame (written in place into the
centroid wrapper's buffer, which the captured graph reads), runs the pipeline
with graphs, runs the same set again eagerly (same modules, graph bypassed),
and compares the keypoints. It reports how many graphs were captured, what a
first call of a new count costs, the steady replay time, the largest keypoint
difference and the GPU memory reserved at the end.

    python bench/cuda_graph_crop_check.py [--frames 8] [--sets 400] [--json out.json]

Needs an Nvidia GPU (cuda).
"""
import argparse, json, math, sys, time
import numpy as np
import torch

sys.argv, cli = [sys.argv[0]], sys.argv[1:]
import sleap_infer_bench as sib  # noqa: E402  (same folder)

p = argparse.ArgumentParser()
p.add_argument("--frames", type=int, default=8)
p.add_argument("--sets", type=int, default=400)
p.add_argument("--seed", type=int, default=1)
p.add_argument("--json", default="cuda_graph_crop_check.json")
a = p.parse_args(cli)

sys.argv = [sys.argv[0], "--frames", str(a.frames), "--cuda-graphs"]
args = sib.parse_args()
device = "cuda"
torch.manual_seed(0)
c = dict(scale=0.25, filters=16, max_stride=16, output_stride=4)
ci = dict(crop=192, filters=16, max_stride=16, output_stride=2)
K = args.max_instances
topdown, cl, il, _, _ = sib.make_layers(c, ci, args, device, "fp16", K, "channels_last")
cnet, inet = cl.backend.model, il.backend.model
assert isinstance(cnet, sib.CUDAGraphed) and isinstance(inet, sib.CUDAGraphed)
synth = cnet.inner
cm_h, cm_w = synth.blobs.shape[-2:]

# Per-frame blob images for every count 0..K (same layout as SyntheticCentroidPeaks).
yy, xx = torch.meshgrid(torch.arange(cm_h), torch.arange(cm_w), indexing="ij")
per = torch.zeros(K + 1, a.frames, cm_h, cm_w)
for k in range(1, K + 1):
    for b in range(a.frames):
        for i in range(k):
            cx = cm_w * (i + 1) / (k + 1)
            cy = cm_h * (0.3 + 0.4 * ((i + b) % 2))
            per[k, b] += torch.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 2.0**2))
per = per.to(device)

EAGER = [False]
orig_forward = sib.CUDAGraphed.forward
sib.CUDAGraphed.forward = lambda self, x: self._eager(x) if EAGER[0] else orig_forward(self, x)

frames = torch.randint(0, 256, (a.frames, sib.FRAME_H, sib.FRAME_W, 1), dtype=torch.uint8)
rng = np.random.default_rng(a.seed)


def run(counts):
    with torch.no_grad():
        for b, k in enumerate(counts):
            synth.blobs[b, 0].copy_(per[k, b])
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    out = topdown.predict(frames.to(device, non_blocking=True))
    torch.cuda.synchronize()
    return time.perf_counter() - t0, out


def kp(out):
    k = out.pred_keypoints
    return (k if torch.is_tensor(k) else torch.as_tensor(np.asarray(k))).float().cpu()


rows, new_shape_ms, steady_ms, max_diff, mismatches, errors = [], [], [], 0.0, 0, []
for s in range(a.sets):
    counts = rng.integers(0, K + 1, a.frames)
    if s == 0:
        counts[:] = 0  # the empty set first: no crops at all
    n = int(counts.sum())
    seen = len(inet.graphs) + len(cnet.graphs)
    try:
        EAGER[0] = False
        t_g, out_g = run(counts)
        kg = kp(out_g)
        EAGER[0] = True
        t_e, out_e = run(counts)
        ke = kp(out_e)
    except Exception as e:  # report and carry on
        errors.append(f"set {s} crops {n}: {type(e).__name__}: {e}")
        EAGER[0] = False
        continue
    EAGER[0] = False
    new = len(inet.graphs) + len(cnet.graphs) > seen  # a capture happened in this set
    (new_shape_ms if new else steady_ms).append(t_g * 1e3)
    same_shape = tuple(kg.shape) == tuple(ke.shape)
    if same_shape:
        both = ~(torch.isnan(kg) | torch.isnan(ke))
        nan_same = bool((torch.isnan(kg) == torch.isnan(ke)).all())
        d = float((kg[both] - ke[both]).abs().max()) if both.any() else 0.0
        max_diff = max(max_diff, d)
        if not nan_same or d > 1e-3:
            mismatches += 1
    else:
        mismatches += 1
    found = int((~torch.isnan(out_g.pred_centroids).any(-1)).sum())
    rows.append(dict(set=s, crops=n, found=found, graph_ms=t_g * 1e3, eager_ms=t_e * 1e3,
                     new_capture=new, kp_shape=list(kg.shape), same_shape=same_shape))

res = dict(
    frames=a.frames, sets=a.sets, max_instances=K,
    instance_graphs_captured=len(inet.graphs),
    instance_graph_crops=" ".join(str(n) for n in sorted(k[0][0] for k in inet.graphs)),
    centroid_graphs_captured=len(cnet.graphs),
    first_call_new_shape_ms=dict(p50=float(np.median(new_shape_ms)) if new_shape_ms else None,
                                 max=float(max(new_shape_ms)) if new_shape_ms else None,
                                 n=len(new_shape_ms)),
    steady_graph_ms=dict(p50=float(np.median(steady_ms)), p99=float(np.percentile(steady_ms, 99)),
                         max=float(max(steady_ms)), n=len(steady_ms)) if steady_ms else None,
    eager_ms_p50=float(np.median([r["eager_ms"] for r in rows])) if rows else None,
    max_keypoint_diff_px=max_diff, mismatched_sets=mismatches,
    found_equals_crops=all(r["found"] == r["crops"] for r in rows),
    cuda_mem_reserved_mb=torch.cuda.memory_reserved() / 2**20,
    cuda_graph_capture_internal_diff=max(cnet.max_abs_diff, inet.max_abs_diff),
    errors=errors, rows=rows,
)
json.dump(res, open(a.json, "w"), indent=1)
print(json.dumps({k: v for k, v in res.items() if k != "rows"}, indent=1))
