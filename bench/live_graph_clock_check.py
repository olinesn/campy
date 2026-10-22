#!/usr/bin/env python3
"""Check campy-live's CUDA graphs while its GPU clock hold runs.

campy-live --cuda-graphs records a graph for each new count of crops (mice),
and --hold-gpu-clock keeps GPU blocks spinning on streams of their own
(campy/live.py: CUDAGraphed, GpuClock). torch.cuda.Stream() hands out a pool
of 32 streams per priority in turn, so streams made per recording and per
GpuClock.Start came round to the same one: a spinner then spun on the stream
a graph was being recorded on, its event sync failed
(CUDA_ERROR_CAPTURED_EVENT) and the recording with it
(cudaErrorStreamCaptureInvalidated). campy/live.py's FixedStream now makes
each of those streams once.

This builds campy-live's random-weights model with --cuda-graphs, as
campy-live does, and for --rounds rounds: restarts the clock hold (all its
spinners spinning, as while campy-live waits for frames), builds the model
again (as campy-live does when the feed's frames are not the shape it
expected) and warms it up with every crop count from 1 to
frames x max-instances, each recorded as a graph with the clock spinning.
The default, 12 rounds of 15 counts, takes more than 200 streams' worth.
It passes if every graph was recorded, the spinners kept spinning, and the
graphs' output equals the model's run without a graph.

    python bench/live_graph_clock_check.py [--frames 3] [--max-instances 5] [--rounds 12]

Needs an NVIDIA GPU (cuda) and sleap-nn. Exit status 0: passed.
"""
import argparse, json, sys, threading, time

from campy import live

p = argparse.ArgumentParser()
p.add_argument("--frames", type=int, default=3, help="Cameras: frames per set.")
p.add_argument("--max-instances", type=int, default=5, help="Most mice per frame.")
p.add_argument("--rounds", type=int, default=12)
p.add_argument("--frame-shape", default="1024x1280x1")
p.add_argument("--json", default="", help="Also write the result here.")
a = p.parse_args()

import torch  # noqa: E402

if not torch.cuda.is_available() or getattr(torch.version, "hip", None):
    sys.exit("Needs an NVIDIA GPU (cuda).")

thread_errors = []
threading.excepthook = lambda e: thread_errors.append("{}: {}: {}".format(
    e.thread.name if e.thread else "?", e.exc_type.__name__, e.exc_value))

args = live.ParseArgs(["--model", "random", "--cuda-graphs", "--fixed-crops", "off", "--frame-shape", a.frame_shape,
    "--max-instances", str(a.max_instances), "--animals", str(a.max_instances)])
shape = live.FrameShape(a.frame_shape)
host = live.HostBuffer(True, (a.frames,) + tuple(shape), live.np.uint8)
crops = range(1, a.frames * a.max_instances + 1)

clock = live.GpuClock(live.ResolveDevice(args.device))
clock.lastSet = 0.0  # no sets: all its spinners spin
rounds, error = [], None
t0 = time.perf_counter()
try:
    for r in range(a.rounds):
        clock.Stop()
        clock.Start()
        spins = clock.spins
        t = time.perf_counter()
        model = live.MODELS["random"](args, a.frames, shape)
        model.Warm(host.tensor, crops=crops)
        graphs = sum(len(g.graphs) for g in model.graphs)
        diff = max(g.max_abs_diff for g in model.graphs)
        rounds.append(dict(round=r, graphs=graphs, max_abs_diff=diff, spins=clock.spins - spins,
            seconds=round(time.perf_counter() - t, 2)))
        print("round {}: {} graphs, largest difference {:g}, {} spins, {:.1f} s".format(
            r, graphs, diff, clock.spins - spins, time.perf_counter() - t), flush=True)
        if thread_errors:
            break
        del model
except Exception as e:
    error = "round {}: {}: {}".format(len(rounds), type(e).__name__, e)
finally:
    clock.Stop()

streams = getattr(live, "_STREAMS", {})
result = dict(
    torch=torch.__version__, gpu=torch.cuda.get_device_name(), frames=a.frames,
    max_instances=a.max_instances, crop_counts=len(crops), rounds_asked=a.rounds,
    rounds_done=len(rounds), graphs_recorded=sum(r["graphs"] for r in rounds),
    clock_restarts=len(rounds), spinners=clock.WAITING_BLOCKS,
    fixed_streams={"{} (device {})".format(k[0], k[2]): hex(s.cuda_stream) for k, s in streams.items()},
    max_abs_diff=max((r["max_abs_diff"] for r in rounds), default=None),
    error=error, thread_errors=thread_errors, seconds=round(time.perf_counter() - t0, 1), rounds=rounds)
passed = (error is None and not thread_errors and len(rounds) == a.rounds
    and all(r["spins"] > 0 and r["max_abs_diff"] <= 1e-2 for r in rounds))
result["passed"] = passed
print(json.dumps({k: v for k, v in result.items() if k != "rounds"}, indent=1))
if a.json:
    with open(a.json, "w") as f:
        json.dump(result, f, indent=1)
print("PASSED" if passed else "FAILED")
sys.exit(0 if passed else 1)
