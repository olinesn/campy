#!/usr/bin/env python3
"""GPU memory and time per set of campy-live's CUDA graphs, with one memory pool per model or one per graph.

campy-live --cuda-graphs records the instance model as a graph for each count
of animals found (campy/live.py: CUDAGraphed), up to frames x max-instances
graphs, and each graph keeps the memory it was recorded in. This builds
campy-live's random-weights model as campy-live does with a trained one
(--cuda-graphs, the warm-up running every count of crops), says how much GPU
memory the graphs hold, then runs --sets sets, at --fps as the cameras would
send them, each frame with a random 0 to max-instances animals (FakeCentroids),
and times them from upload to keypoints on the host, as campy-live's
inference time.

    python bench/live_graph_memory.py --pools shared|separate|eager [--frames 3] [--sets 3000] [--json out.json]

shared: all of a model's graphs in one pool (campy-live's own); separate: a
pool per graph (campy-live before); eager: no graphs. Run each in its own
process: torch keeps the memory it was given until the process ends. Needs a
GPU (CUDA or ROCm) and sleap-nn.
"""
import argparse, json, sys, time

from campy import live

p = argparse.ArgumentParser()
p.add_argument("--pools", required=True, choices=["shared", "separate", "eager"])
p.add_argument("--frames", type=int, default=3, help="Cameras: frames per set.")
p.add_argument("--max-instances", type=int, default=5, help="Most animals per frame.")
p.add_argument("--sets", type=int, default=3000)
p.add_argument("--fps", type=float, default=60, help="Sets per second (0: back to back).")
p.add_argument("--frame-shape", default="1024x1280x1")
p.add_argument("--hold-gpu-clock", default="on", choices=["on", "off"])
p.add_argument("--seed", type=int, default=1)
p.add_argument("--json", default="", help="Also write the result here.")
a = p.parse_args()

import numpy as np  # noqa: E402
import torch  # noqa: E402

if not torch.cuda.is_available():
    sys.exit("Needs a CUDA (or ROCm) GPU.")

live.CUDAGraphed.SHARE_POOL = a.pools == "shared"
argv = ["--model", "random", "--frame-shape", a.frame_shape, "--max-instances", str(a.max_instances),
    "--animals", str(a.max_instances)]
args = live.ParseArgs(argv + (["--cuda-graphs"] if a.pools != "eager" else []))
shape = live.FrameShape(a.frame_shape)
host = live.HostBuffer(True, (a.frames,) + tuple(shape), np.uint8)
host.array[:] = np.random.default_rng(0).integers(0, 256, host.array.shape, np.uint8)
GB = 1e9

reserved0 = torch.cuda.memory_reserved()
model = live.MODELS["random"](args, a.frames, shape)
torch.cuda.synchronize()
reserved_built = torch.cuda.memory_reserved()
t = time.perf_counter()
model.Warm(host.tensor, crops=range(1, a.frames * a.max_instances + 1))
warm_s = time.perf_counter() - t
reserved_warm = torch.cuda.memory_reserved()
free, total = torch.cuda.mem_get_info()
pools = set().union(*(g.Pools() for g in model.graphs)) if model.graphs else set()
pooled = live.PoolBytes(pools) if pools else 0

rng = np.random.default_rng(a.seed)
counts = rng.integers(0, a.max_instances + 1, (a.sets, a.frames))
clock = live.GpuClock(model.device) if a.hold_gpu_clock == "on" else None
if clock:
    clock.Start()
ms = []
try:
    t0 = time.perf_counter()
    for n, c in enumerate(counts):
        if a.fps:
            due = t0 + n / a.fps
            while time.perf_counter() < due:
                time.sleep(0.0002)
        if clock:
            clock.Busy()
        t = time.perf_counter_ns()
        with live.FakeCentroids(model.layer.centroid_layer, list(c)):
            model.Predict(model.Upload(host.tensor))
        ms.append((time.perf_counter_ns() - t) / 1e6)
finally:
    if clock:
        clock.Stop()
ms = np.array(ms[min(100, len(ms) // 10):])  # the first sets: the clock coming up

result = dict(
    pools=a.pools, gpu=torch.cuda.get_device_name(), torch=torch.__version__,
    hip=getattr(torch.version, "hip", None), frames=a.frames, max_instances=a.max_instances,
    frame_shape=a.frame_shape, fps=a.fps, hold_gpu_clock=a.hold_gpu_clock,
    graphs=sum(len(g.graphs) for g in model.graphs), memory_pools=len(pools),
    graph_pools_gb=None if pooled is None else round(pooled / GB, 3),
    reserved_built_gb=round((reserved_built - reserved0) / GB, 3),
    reserved_after_warm_gb=round(reserved_warm / GB, 3),
    gpu_used_gb=round((total - free) / GB, 3), warm_s=round(warm_s, 1),
    max_abs_diff=max((g.max_abs_diff for g in model.graphs), default=None),
    crops_per_set_mean=round(float(counts.sum(1).mean()), 2), sets_timed=len(ms),
    ms_p50=round(float(np.percentile(ms, 50)), 3), ms_p99=round(float(np.percentile(ms, 99)), 3),
    ms_mean=round(float(ms.mean()), 3), ms_max=round(float(ms.max()), 3),
    gpu_clock=clock.Describe() if clock else None)
print(json.dumps(result, indent=1))
if a.json:
    with open(a.json, "w") as f:
        json.dump(result, f, indent=1)
