#!/usr/bin/env python3
"""Benchmark candidate lightweight SLEAP top-down models for live inference.

Builds a centroid model and a centered-instance model with random weights from
sleap-nn's own config and model code, wraps them in sleap-nn's inference
layers (TorchBackend -> CentroidLayer + CenteredInstanceLayer -> TopDownLayer),
and times ``TopDownLayer.predict`` the way live inference would call it on a
set of synchronized camera frames:

    3 uint8 mono 1280x1024 frames on the host
      -> upload to the device as (N, H, W, 1)
      -> centroid model + local peak finding
      -> K crops per frame, cut on the device
      -> centered-instance model + global peak finding (integral refinement)

Compute time does not depend on the weights, so this can be used to pick
model sizes before labelling and training. It is standalone: campy is not
imported, and nothing here is installed with campy.

What is measured
----------------
* Per-set latency (one set = the N frames of one synchronized trigger), with
  the device synchronized after every set: p50/p95/p99/max over --iters
  iterations after --warmup untimed iterations, and sets per second.
* The first and second call of each new shape, timed separately. On ROCm,
  MIOpen searches/tunes kernels for every new convolution shape on first use,
  so the first call can take seconds; it is reported, not mixed into the
  percentiles. (On MPS the first call also builds Metal kernels.)
* fp32 and fp16 autocast. On cuda/ROCm fp16 uses sleap-nn's own
  ``TorchBackend(use_fp16=True)`` (torch.autocast inside the backend); on MPS
  the script applies ``torch.autocast("mps", float16)`` itself,
  because TorchBackend only autocasts on cuda (the model is wrapped so that
  autocast runs inside the backend and fp32 confmaps come out, as on cuda).
  fp16 is skipped on CPU.
* With --memory-format nchw,channels_last, both memory layouts. sleap-nn's
  models are channels-first (NCHW); cuDNN runs its tensor-core convolutions
  channels-last and converts every activation to and from that layout, about a
  fifth of the forward time on an RTX 2000 Ada. channels_last converts the
  models' weights (model.to(memory_format=torch.channels_last)) so activations
  stay channels-last and the conversions go. On ROCm it changed nothing, and on
  Apple MPS it was slower.
* With --cuda-graphs (cuda only), each model's forward pass is captured as a
  CUDA graph on its first call and replayed after that, so its ~100-150 kernel
  launches cost one launch. sleap-nn's peak finding and cropping stay as they
  are: they make data-dependent decisions (nonzero, .item()) that a graph
  cannot record. A graph only replays the shapes it was captured with; a new
  shape (another crop count) is captured again. fp16 then runs under the
  wrapper's own autocast with its weight cache off, as graph capture requires.
  Each row records the largest difference between the graph's output and the
  eager forward on the capture input. torch.compile's reduce-overhead mode does
  the same automatically but needs Triton, which Windows lacks.
* With --stages, each stage alone as well: CentroidLayer.predict on the
  uploaded frames, and CenteredInstanceLayer.predict on a (N*K, 1, crop, crop)
  uint8 crop batch already on the device.
* GFLOP per set for each model (torch.utils.flop_counter, 2 FLOP per MAC) and
  parameter counts.

How random weights are made to behave like a trained centroid model
-------------------------------------------------------------------
A randomly initialised centroid model produces hundreds of local maxima, so
peak finding (and its integral refinement) would do far more work than with a
trained model, and the number of crops would change from set to set. The
centroid model is therefore wrapped: it still runs in full, but its output is
squashed to below the peak threshold (clamp to +-1, times 1e-3) and K Gaussian
peaks per frame are added at fixed positions. Peak finding then finds exactly
K centroids per frame, and the centered-instance model always gets N*K crops.
The centered-instance layer uses peak_threshold=-inf so every node of every
crop gets integral refinement, like a trained model with all nodes visible.

Crop counts and fixed shapes
----------------------------
sleap-nn's TopDownLayer drops empty (NaN) centroid slots before cropping, so
in live use the crop batch is N * (animals detected), not N * max_instances,
and each distinct count is a new shape for MIOpen and for torch.compile.
Benchmark every count you expect (``--crops-per-frame 1,2,3,4,5``) to see the
first-call cost of each, and warm all of them up at start-up in the live
pipeline. ``--max-instances`` (default 5) is the centroid layer's cap.

Running on this Mac (smoke test)
--------------------------------
    uv venv -p 3.12 .venv-bench && . .venv-bench/bin/activate
    uv pip install "sleap-nn==0.3.3" torch
    python bench/sleap_infer_bench.py --device cpu --iters 20 --warmup 3
    python bench/sleap_infer_bench.py --device mps --iters 50 --sweep

Installing on the EVO-X2 (Ryzen AI Max+ 395, Radeon 8060S = gfx1151)
--------------------------------------------------------------------
sleap-nn has no ROCm extra. Its plain requirement is an unpinned torch plus
torchvision>=0.20, so install AMD's gfx1151 torch *and* torchvision first and
then sleap-nn with no extra (its torch-cuda*/gpu extras would pull CUDA
wheels), pinning torch/torchvision with a constraints file so pip cannot
swap them for PyPI's CUDA builds:

    python3.12 -m venv ~/venvs/sleap-rocm && . ~/venvs/sleap-rocm/bin/activate
    pip install --upgrade pip
    # Check AMD's "Install PyTorch for ROCm" page (Ryzen AI Max / gfx1151) for
    # the current versions; at the time of writing:
    pip install --index-url https://stable.repo.amd.com/rocm/whl-next/ \
        "torch[device-gfx1151]==2.14.0+rocm10.1.0" torchvision
    pip freeze | grep -E "^(torch|torchvision)==" > ~/venvs/rocm-constraints.txt
    pip install -c ~/venvs/rocm-constraints.txt "sleap-nn==0.3.3"
    python -c "import torch; print(torch.__version__, torch.version.hip, torch.cuda.is_available(), torch.cuda.get_device_name(0))"

The last line must print a +rocm version, a HIP version and True. If pip
reports a torch/torchvision conflict instead, it was about to replace the
ROCm build: fix the versions rather than dropping the constraints.

On ROCm, PyTorch's "cuda" device is the HIP device, so use --device cuda (or
the default --device auto).

Persist MIOpen's tuning database, so kernels tuned on the first call of each
shape are reused by the next process instead of being searched again:

    export MIOPEN_USER_DB_PATH=$HOME/.cache/miopen-sleap
    export MIOPEN_CUSTOM_CACHE_DIR=$HOME/.cache/miopen-sleap
    mkdir -p "$MIOPEN_USER_DB_PATH"

Run the script twice: the first call of each shape should drop sharply on the
second run. Use the same variables in the live pipeline.

Use fp16 only (not bf16) on gfx1151; bf16 has had NaN reports there:

    python bench/sleap_infer_bench.py --precision fp16 --sweep --iters 2000

(--precision fp32,fp16 is the default, to show what autocast buys.) The sweep
is 8 centroid + 4 centered-instance stage runs plus 32 pipeline pairs, per
precision and per crop count; at --iters 2000 and ~10-20 ms a set, budget
roughly 15-30 minutes per precision (--no-pipeline skips the 32 pairs).
torch.compile with mode="reduce-overhead" (--compile) is experimental on ROCm:
it compiles once per shape and may fail; compare it only after eager works.

Run it while campy records 3 cameras, so the GPU, memory bandwidth and CPU are
shared the way they will be live. In one terminal start the recording with the
real config, in another watch the kernel log, and in a third run the bench:

    campy-acquire ./configs/<your 3-camera config>.yaml
    sudo journalctl -k -f | grep -iE "amdgpu|fence|ring|mes|gpu reset|timeout"
    python bench/sleap_infer_bench.py --precision fp16 --iters 2000 --sweep

Any "Fence fallback timer expired on ring ...", "ring ... timeout", MES
("MES failed to respond", "failed to ... queue") or GPU reset message during
the run means the driver missed interrupts or hung; the run's p99/max will
usually show it as multi-millisecond stalls. Note the time and the line.

Optional A/B with HSA_ENABLE_INTERRUPT=0 (the ROCm runtime then polls for
completion instead of waiting for interrupts; this works around missed-fence
stalls at the cost of a busy CPU core). Same config, same load, compare p99/max
and the journal:

    python bench/sleap_infer_bench.py --precision fp16 --json a_interrupts.json
    HSA_ENABLE_INTERRUPT=0 python bench/sleap_infer_bench.py --precision fp16 --json b_polling.json

The JSON records the environment variables that matter (HSA_*, MIOPEN_*,
HIP_*, ROCR_*, AMD_*, PYTORCH_*), so the two files say which run was which.
"""

import argparse
import itertools
import json
import math
import os
import platform
import sys
import time
import warnings
from datetime import datetime

import numpy as np
import torch
from omegaconf import OmegaConf

import sleap_nn
from sleap_nn.config.get_config import get_model_config
from sleap_nn.inference.layers.backends.torch_backend import TorchBackend
from sleap_nn.inference.layers.centered_instance import CenteredInstanceLayer
from sleap_nn.inference.layers.centroid import CentroidLayer
from sleap_nn.inference.layers.configs import PostprocessConfig, PreprocessConfig
from sleap_nn.inference.layers.topdown import TopDownLayer
from sleap_nn.training.lightning_modules import (
    CentroidLightningModule,
    TopDownCenteredInstanceLightningModule,
)

FRAME_H, FRAME_W = 1024, 1280

SWEEP_CENTROID = [
    dict(scale=s, filters=16, max_stride=ms, output_stride=os_)
    for s, ms, os_ in itertools.product((0.25, 0.5), (16, 32), (2, 4))
]
SWEEP_INSTANCE = [
    dict(crop=c, filters=16, max_stride=ms) for c, ms in itertools.product((192, 256), (16, 32))
]


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

def unet_config(in_channels, filters, filters_rate, max_stride, output_stride):
    return {
        "unet": {
            "in_channels": in_channels,
            "filters": filters,
            "filters_rate": filters_rate,
            "max_stride": max_stride,
            "output_stride": output_stride,
        }
    }


def build_centroid_module(in_channels, filters, filters_rate, max_stride, output_stride):
    model_config = get_model_config(
        backbone_config=unet_config(in_channels, filters, filters_rate, max_stride, output_stride),
        head_configs={"centroid": {"confmaps": {"sigma": 2.5, "output_stride": output_stride}}},
    )
    cfg = OmegaConf.structured(model_config)
    return CentroidLightningModule(
        model_type="centroid",
        backbone_type="unet",
        backbone_config=cfg.backbone_config,
        head_configs=cfg.head_configs,
    ).eval()


def build_instance_module(in_channels, filters, filters_rate, max_stride, output_stride, n_nodes):
    model_config = get_model_config(
        backbone_config=unet_config(in_channels, filters, filters_rate, max_stride, output_stride),
        head_configs={
            "centered_instance": {
                "confmaps": {
                    "part_names": [f"node{i}" for i in range(n_nodes)],
                    "sigma": 2.5,
                    "output_stride": output_stride,
                }
            }
        },
    )
    cfg = OmegaConf.structured(model_config)
    return TopDownCenteredInstanceLightningModule(
        model_type="centered_instance",
        backbone_type="unet",
        backbone_config=cfg.backbone_config,
        head_configs=cfg.head_configs,
    ).eval()


class SyntheticCentroidPeaks(torch.nn.Module):
    """Run the centroid model, then replace its confmaps with K known peaks.

    The model still runs in full (its output feeds the result), so its cost
    is measured; peak finding then sees exactly K peaks per frame, as with a
    trained model, instead of hundreds of random local maxima.
    """

    def __init__(self, inner, n_frames, cm_h, cm_w, k, sigma=2.0):
        super().__init__()
        self.inner = inner
        yy, xx = torch.meshgrid(torch.arange(cm_h), torch.arange(cm_w), indexing="ij")
        blobs = torch.zeros(n_frames, 1, cm_h, cm_w)
        # K centres spread along a diagonal-ish grid, away from the borders.
        for b in range(n_frames):
            for i in range(k):
                cx = cm_w * (i + 1) / (k + 1)
                cy = cm_h * (0.3 + 0.4 * ((i + b) % 2))
                blobs[b, 0] += torch.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma**2))
        self.register_buffer("blobs", blobs)

    def forward(self, img):
        cm = self.inner(img)
        return cm.clamp(-1, 1) * 1e-3 + self.blobs.to(cm.dtype)


def count_gflop_and_params(module, input_shape):
    """GFLOP of one forward at input_shape (uint8 input, as live), on CPU."""
    from torch.utils.flop_counter import FlopCounterMode

    x = torch.zeros(input_shape, dtype=torch.uint8)
    counter = FlopCounterMode(display=False)
    with torch.inference_mode(), counter:
        module(x)
    params = sum(p.numel() for p in module.parameters())
    return counter.get_total_flops() / 1e9, params


# --------------------------------------------------------------------------
# Device helpers
# --------------------------------------------------------------------------

def resolve_device(name):
    if name == "auto":
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
        return "cpu"
    if name == "cuda" and not torch.cuda.is_available():
        sys.exit("--device cuda: torch.cuda.is_available() is False (on ROCm, is this a ROCm torch build?)")
    if name == "mps" and not torch.backends.mps.is_available():
        sys.exit("--device mps: MPS is not available")
    return name


def synchronize(device):
    if device.startswith("cuda"):
        torch.cuda.synchronize()
    elif device == "mps":
        torch.mps.synchronize()


def device_description(device):
    if device.startswith("cuda"):
        props = torch.cuda.get_device_properties(0)
        arch = getattr(props, "gcnArchName", "")
        backend = f"ROCm/HIP {torch.version.hip}" if torch.version.hip else f"CUDA {torch.version.cuda}"
        return f"{torch.cuda.get_device_name(0)} {arch} ({backend})".strip()
    if device == "mps":
        return f"Apple MPS ({platform.machine()}, macOS {platform.mac_ver()[0]})"
    return f"CPU {platform.processor() or platform.machine()} ({torch.get_num_threads()} threads)"


class MPSAutocast(torch.nn.Module):
    """fp16 autocast for MPS, done the way TorchBackend does it on cuda.

    TorchBackend only autocasts on cuda. Like it, run the forward under
    autocast and hand fp32 confmaps to the layer's peak finding (sleap-nn's
    scatter into fp32 output tensors fails on fp16 values).
    """

    def __init__(self, inner):
        super().__init__()
        self.inner = inner

    def forward(self, img):
        with torch.autocast(device_type="mps", dtype=torch.float16):
            out = self.inner(img)
        return out.float()


class CUDAGraphed(torch.nn.Module):
    """Replay a model's forward pass as a CUDA graph, captured per input shape.

    Runs the inner forward under fp16 autocast when asked (with autocast's
    weight cache off, which graph capture needs) and returns fp32 outputs, as
    TorchBackend does. The returned tensor is the graph's static output, valid
    until the next call: the layers read it before then.
    """

    def __init__(self, inner, fp16):
        super().__init__()
        self.inner = inner
        self.fp16 = fp16
        self.graphs = {}  # (shape, dtype) -> (graph, static_in, static_out)
        self.max_abs_diff = 0.0

    def _eager(self, x):
        with torch.autocast(device_type="cuda", dtype=torch.float16, enabled=self.fp16,
                            cache_enabled=False):
            return self.inner(x).float()

    def _capture(self, x):
        static_in = x.clone()
        side = torch.cuda.Stream()
        side.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(side):
            for _ in range(3):  # warm up cuDNN/cuBLAS outside the capture
                self._eager(static_in)
        torch.cuda.current_stream().wait_stream(side)
        graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(graph):
            static_out = self._eager(static_in)
        graph.replay()
        diff = (static_out - self._eager(static_in)).abs().max().item()
        self.max_abs_diff = max(self.max_abs_diff, diff)
        return graph, static_in, static_out

    def forward(self, x):
        key = (tuple(x.shape), x.dtype)
        if key not in self.graphs:
            self.graphs[key] = self._capture(x)
        graph, static_in, static_out = self.graphs[key]
        static_in.copy_(x)
        graph.replay()
        return static_out


# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------

def time_calls(fn, device, iters, warmup):
    """Time fn() with a device sync after each call.

    Returns first/second call times (new-shape cost) and the latency list of
    `iters` calls made after `warmup` further untimed calls.
    """
    t0 = time.perf_counter()
    fn()
    synchronize(device)
    first = time.perf_counter() - t0

    t0 = time.perf_counter()
    out = fn()
    synchronize(device)
    second = time.perf_counter() - t0

    for _ in range(warmup):
        fn()
    synchronize(device)

    lat = np.empty(iters)
    for i in range(iters):
        t0 = time.perf_counter()
        out = fn()
        synchronize(device)
        lat[i] = time.perf_counter() - t0
    return first, second, lat, out


def summarize(first, second, lat):
    ms = lat * 1e3
    return {
        "first_call_ms": first * 1e3,
        "second_call_ms": second * 1e3,
        "iters": int(len(ms)),
        "mean_ms": float(ms.mean()),
        "p50_ms": float(np.percentile(ms, 50)),
        "p95_ms": float(np.percentile(ms, 95)),
        "p99_ms": float(np.percentile(ms, 99)),
        "max_ms": float(ms.max()),
        "sets_per_s": float(1e3 / ms.mean()),
    }


# --------------------------------------------------------------------------
# One benchmark run
# --------------------------------------------------------------------------

def make_layers(c, ci, args, device, precision, k, memory_format="nchw"):
    """Build fresh backends/layers (use_fp16/use_compile are construction-time)."""
    cm_h = math.ceil(FRAME_H * c["scale"] / c["max_stride"]) * c["max_stride"] // c["output_stride"]
    cm_w = math.ceil(FRAME_W * c["scale"] / c["max_stride"]) * c["max_stride"] // c["output_stride"]

    centroid_module = build_centroid_module(
        args.in_channels, c["filters"], args.filters_rate, c["max_stride"], c["output_stride"]
    )
    instance_module = build_instance_module(
        args.in_channels, ci["filters"], args.filters_rate, ci["max_stride"],
        ci["output_stride"], args.nodes,
    )
    # Move the Lightning modules themselves: their forward sends the input to
    # self.device, which Lightning only updates when .to() is called on the
    # module, not when a parent module (the wrappers below) is moved.
    centroid_module.to(device)
    instance_module.to(device)
    if memory_format == "channels_last":
        centroid_module.to(memory_format=torch.channels_last)
        instance_module.to(memory_format=torch.channels_last)
    centroid_net = SyntheticCentroidPeaks(centroid_module, args.frames, cm_h, cm_w, k)
    instance_net = instance_module
    if precision == "fp16" and device == "mps":
        centroid_net, instance_net = MPSAutocast(centroid_net), MPSAutocast(instance_net)
    graphed = args.cuda_graphs and device.startswith("cuda")
    if graphed:
        centroid_net = CUDAGraphed(centroid_net, precision == "fp16")
        instance_net = CUDAGraphed(instance_net, precision == "fp16")

    use_fp16 = precision == "fp16" and device.startswith("cuda") and not graphed
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # TorchBackend's fp16/compile notices
        centroid_backend = TorchBackend(
            model=centroid_net, device=device, use_fp16=use_fp16,
            use_compile=args.compile, compile_mode="reduce-overhead",
        )
        instance_backend = TorchBackend(
            model=instance_net, device=device, use_fp16=use_fp16,
            use_compile=args.compile, compile_mode="reduce-overhead",
        )

    centroid_layer = CentroidLayer(
        backend=centroid_backend,
        output_stride=c["output_stride"],
        max_instances=args.max_instances,
        max_stride=c["max_stride"],
        preprocess_config=PreprocessConfig(scale=c["scale"]),
        postprocess_config=PostprocessConfig(
            peak_threshold=0.2, refinement="integral", max_instances=args.max_instances
        ),
    )
    instance_layer = CenteredInstanceLayer(
        backend=instance_backend,
        output_stride=ci["output_stride"],
        max_stride=ci["max_stride"],
        preprocess_config=PreprocessConfig(scale=1.0),
        postprocess_config=PostprocessConfig(peak_threshold=float("-inf"), refinement="integral"),
    )
    topdown = TopDownLayer(
        centroid_layer=centroid_layer,
        centered_instance_layer=instance_layer,
        crop_size=(ci["crop"], ci["crop"]),
    )
    return topdown, centroid_layer, instance_layer, centroid_module, instance_module


def run_one(c, ci, args, device, precision, k, frames_host, what, memory_format="nchw"):
    """what: 'pipeline', 'centroid' or 'instance'. Returns a result dict."""
    topdown, centroid_layer, instance_layer, centroid_module, instance_module = make_layers(
        c, ci, args, device, precision, k, memory_format
    )
    n = args.frames

    if what == "instance":
        crops = torch.randint(0, 256, (n * k, 1, ci["crop"], ci["crop"]), dtype=torch.uint8).to(device)

        def fn():
            return instance_layer.predict(crops)
    else:
        layer = topdown if what == "pipeline" else centroid_layer

        def fn():
            x = frames_host.to(device, non_blocking=True)  # (N, H, W, 1) uint8
            return layer.predict(x)

    first, second, lat, out = time_calls(fn, device, args.iters, args.warmup)
    result = {"stage": what, "precision": precision, "memory_format": memory_format,
              "cuda_graphs": False, "crops_per_frame": k}
    result.update(summarize(first, second, lat))
    graphed = [m for m in (centroid_layer.backend.model, instance_layer.backend.model)
               if isinstance(m, CUDAGraphed) and m.graphs]
    if graphed:
        result["cuda_graphs"] = True
        result["cuda_graph_max_abs_diff"] = max(m.max_abs_diff for m in graphed)

    # Check the shapes really were what we meant to time.
    if what in ("pipeline", "centroid"):
        n_centroids = int((~torch.isnan(out.pred_centroids).any(-1)).sum())
        result["centroids_found"] = n_centroids
        if n_centroids != n * k:
            result["warning"] = f"expected {n * k} centroids, found {n_centroids}"
    if what in ("pipeline", "instance"):
        result["keypoints_shape"] = list(out.pred_keypoints.shape)

    del topdown, centroid_layer, instance_layer, centroid_module, instance_module
    return result


def model_stats(c, ci, args, k):
    """GFLOP per set and params for both models, counted on CPU."""
    stats = {}
    if c is not None:
        h = math.ceil(FRAME_H * c["scale"] / c["max_stride"]) * c["max_stride"]
        w = math.ceil(FRAME_W * c["scale"] / c["max_stride"]) * c["max_stride"]
        m = build_centroid_module(
            args.in_channels, c["filters"], args.filters_rate, c["max_stride"], c["output_stride"]
        )
        g, p = count_gflop_and_params(m, (args.frames, 1, args.in_channels, h, w))
        stats.update(centroid_input_hw=[h, w], centroid_gflop_per_set=g,
                     centroid_gflop_per_frame=g / args.frames, centroid_params=p)
    if ci is not None:
        m = build_instance_module(
            args.in_channels, ci["filters"], args.filters_rate, ci["max_stride"],
            ci["output_stride"], args.nodes,
        )
        g, p = count_gflop_and_params(m, (args.frames * k, 1, args.in_channels, ci["crop"], ci["crop"]))
        stats.update(instance_gflop_per_set=g, instance_gflop_per_crop=g / (args.frames * k),
                     instance_params=p)
    return stats


def centroid_label(c):
    return f"s{c['scale']:g} f{c['filters']} ms{c['max_stride']} os{c['output_stride']}"


def instance_label(ci):
    return f"crop{ci['crop']} f{ci['filters']} ms{ci['max_stride']} os{ci['output_stride']}"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Time random-weight SLEAP top-down models (sleap-nn) for live inference.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--device", default="auto", choices=["auto", "cuda", "mps", "cpu"],
                   help="cuda is also the ROCm/HIP device")
    p.add_argument("--precision", default="fp32,fp16",
                   help="comma list of fp32, fp16 (fp16 = autocast; skipped on cpu)")
    p.add_argument("--memory-format", default="nchw",
                   help="comma list of nchw, channels_last (model weights and activations)")
    p.add_argument("--cuda-graphs", action="store_true",
                   help="cuda: replay each model's forward pass as a CUDA graph")
    p.add_argument("--compile", action="store_true",
                   help="experimental: TorchBackend(use_compile=True, compile_mode='reduce-overhead')")
    p.add_argument("--iters", type=int, default=2000, help="timed sets per measurement")
    p.add_argument("--warmup", type=int, default=50,
                   help="untimed sets after the first two calls of each shape")
    p.add_argument("--frames", type=int, default=3, help="frames per set (cameras)")
    p.add_argument("--crops-per-frame", default="5",
                   help="comma list: centroids found (= crops) per frame, e.g. 1,2,3,4,5")
    p.add_argument("--max-instances", type=int, default=5, help="centroid layer cap")
    p.add_argument("--nodes", type=int, default=15, help="skeleton nodes")
    p.add_argument("--in-channels", type=int, default=1)
    p.add_argument("--filters-rate", type=float, default=2.0, help="both models")
    g = p.add_argument_group("centroid model (single-config mode)")
    g.add_argument("--centroid-scale", type=float, default=0.25, help="input scale")
    g.add_argument("--centroid-filters", type=int, default=16)
    g.add_argument("--centroid-max-stride", type=int, default=16)
    g.add_argument("--centroid-output-stride", type=int, default=4)
    g = p.add_argument_group("centered-instance model (single-config mode)")
    g.add_argument("--crop", type=int, default=192, help="crop size (square)")
    g.add_argument("--instance-filters", type=int, default=16)
    g.add_argument("--instance-max-stride", type=int, default=16)
    g.add_argument("--instance-output-stride", type=int, default=2)
    p.add_argument("--sweep", action="store_true",
                   help="preset sweep: centroid scale {0.25,0.5} x max_stride {16,32} x "
                        "output_stride {2,4}; centered-instance crop {192,256} x max_stride {16,32} "
                        "at --instance-output-stride; filters 16")
    p.add_argument("--stages", action="store_true",
                   help="also time each stage alone (implied by --sweep)")
    p.add_argument("--no-pipeline", action="store_true",
                   help="with --sweep: time stages only, skip the full pipeline for every pair")
    p.add_argument("--json", default=None, help="output JSON path (default: timestamped in cwd)")
    p.add_argument("--seed", type=int, default=0)
    return p.parse_args()


def main():
    args = parse_args()
    device = resolve_device(args.device)
    torch.manual_seed(args.seed)
    precisions = [s.strip() for s in args.precision.split(",") if s.strip()]
    for prec in precisions:
        if prec not in ("fp32", "fp16"):
            sys.exit(f"--precision: unknown {prec!r} (use fp32 and/or fp16; bf16 is deliberately not offered)")
    memory_formats = [s.strip() for s in args.memory_format.split(",") if s.strip()]
    for mf in memory_formats:
        if mf not in ("nchw", "channels_last"):
            sys.exit(f"--memory-format: unknown {mf!r} (use nchw and/or channels_last)")
    skipped = []
    if device == "cpu" and "fp16" in precisions:
        precisions.remove("fp16")
        skipped.append("fp16 skipped on cpu")
    if args.cuda_graphs and args.compile:
        sys.exit("--cuda-graphs and --compile both capture graphs: choose one")
    if args.cuda_graphs and not device.startswith("cuda"):
        skipped.append("--cuda-graphs ignored: cuda only")
    if args.compile and device == "mps":
        skipped.append("--compile ignored on mps (sleap-nn's TorchBackend disables it)")
    ks = [int(s) for s in args.crops_per_frame.split(",")]
    if max(ks) > args.max_instances:
        sys.exit("--crops-per-frame cannot exceed --max-instances")

    if args.sweep:
        centroids = SWEEP_CENTROID
        instances = [dict(ci, output_stride=args.instance_output_stride) for ci in SWEEP_INSTANCE]
        stages, pipeline = True, not args.no_pipeline
    else:
        centroids = [dict(scale=args.centroid_scale, filters=args.centroid_filters,
                          max_stride=args.centroid_max_stride,
                          output_stride=args.centroid_output_stride)]
        instances = [dict(crop=args.crop, filters=args.instance_filters,
                          max_stride=args.instance_max_stride,
                          output_stride=args.instance_output_stride)]
        stages, pipeline = args.stages, True

    env = {k: v for k, v in os.environ.items()
           if k.startswith(("HSA_", "MIOPEN_", "HIP_", "ROCR_", "AMD_", "PYTORCH_"))}
    header = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "host": platform.node(),
        "python": sys.version.split()[0],
        "torch": torch.__version__,
        "torch_hip": torch.version.hip,
        "sleap_nn": sleap_nn.__version__,
        "device": device,
        "device_name": device_description(device),
        "env": env,
        "args": vars(args),
        "method": "sleap-nn TopDownLayer.predict (CentroidLayer + CenteredInstanceLayer on "
                  "TorchBackend) with random-weight models built from sleap-nn configs; centroid "
                  "confmaps replaced by K synthetic peaks per frame after the model runs, so the "
                  "crop count is fixed; device synchronized after every set",
        "notes": skipped,
    }
    print(f"sleap-nn {sleap_nn.__version__}, torch {torch.__version__}, device {device}: "
          f"{header['device_name']}")
    if env:
        print("env: " + " ".join(f"{k}={v}" for k, v in sorted(env.items())))
    for note in skipped:
        print(f"note: {note}")
    print(f"{args.frames} frames {FRAME_W}x{FRAME_H} uint8 per set, {args.nodes} nodes, "
          f"iters {args.iters}, warmup {args.warmup}, compile {args.compile}, "
          f"CUDA graphs {args.cuda_graphs and device.startswith('cuda')}, "
          f"memory format {','.join(memory_formats)}")
    print("timing: sleap-nn TopDownLayer.predict; centroid confmaps -> K synthetic peaks/frame "
          "(model still runs) so crops are fixed")

    rng = np.random.default_rng(args.seed)
    frames_np = rng.integers(0, 256, (args.frames, FRAME_H, FRAME_W, 1), dtype=np.uint8)
    frames_host = torch.from_numpy(frames_np)
    if device.startswith("cuda"):
        frames_host = frames_host.pin_memory()

    # (kind, centroid cfg or None, instance cfg or None)
    jobs = []
    if stages:
        jobs += [("centroid", c, instances[0]) for c in centroids]
        jobs += [("instance", centroids[0], ci) for ci in instances]
    if pipeline:
        jobs += [("pipeline", c, ci) for c in centroids for ci in instances]

    results = []
    stats_cache = {}
    t_start = time.perf_counter()
    for kind, c, ci in jobs:
        for k in ks:
            key = (centroid_label(c) if kind != "instance" else None,
                   instance_label(ci) if kind != "centroid" else None, k)
            if key not in stats_cache:
                stats_cache[key] = model_stats(
                    c if kind != "instance" else None, ci if kind != "centroid" else None, args, k
                )
            for prec in precisions:
                for mf in memory_formats:
                    r = run_one(c, ci, args, device, prec, k, frames_host, kind, mf)
                    r["centroid"] = dict(c) if kind != "instance" else None
                    r["instance"] = dict(ci) if kind != "centroid" else None
                    r.update(stats_cache[key])
                    results.append(r)
                    print_row(r, header_needed=len(results) == 1)

    print(f"\ndone in {time.perf_counter() - t_start:.1f} s")
    print_table(results)

    out_path = args.json or f"sleap_infer_bench_{device}_{datetime.now():%Y%m%d_%H%M%S}.json"
    with open(out_path, "w") as f:
        json.dump({"header": header, "results": results}, f, indent=2)
    print(f"wrote {out_path}")


COLUMNS = [
    ("stage", 8), ("centroid", 20), ("instance", 21), ("prec", 4), ("mem", 4), ("cg", 2), ("K", 2), ("GFLOP", 6),
    ("1st ms", 8), ("2nd ms", 7), ("p50", 7), ("p95", 7), ("p99", 7), ("max", 7), ("sets/s", 7),
]


def row_cells(r):
    gflop = r.get("centroid_gflop_per_set", 0.0) * (r["stage"] != "instance") + \
        r.get("instance_gflop_per_set", 0.0) * (r["stage"] != "centroid")
    return [
        r["stage"],
        centroid_label(r["centroid"]) if r["centroid"] else "-",
        instance_label(r["instance"]) if r["instance"] else "-",
        r["precision"], "cl" if r.get("memory_format") == "channels_last" else "nchw",
        "y" if r.get("cuda_graphs") else "-",
        str(r["crops_per_frame"]), f"{gflop:.1f}",
        f"{r['first_call_ms']:.0f}", f"{r['second_call_ms']:.1f}",
        f"{r['p50_ms']:.2f}", f"{r['p95_ms']:.2f}", f"{r['p99_ms']:.2f}", f"{r['max_ms']:.2f}",
        f"{r['sets_per_s']:.1f}",
    ]


def format_row(cells):
    return "  ".join(str(c).rjust(w) if i >= 3 else str(c).ljust(w)
                     for i, (c, (_, w)) in enumerate(zip(cells, COLUMNS)))


def print_row(r, header_needed):
    if header_needed:
        print()
        print(format_row([name for name, _ in COLUMNS]))
    print(format_row(row_cells(r)) + (f"  !! {r['warning']}" if "warning" in r else ""), flush=True)


def print_table(results):
    print("\nSummary (latency per set in ms; GFLOP per set; mem = memory format, cl = "
          "channels_last; cg = CUDA graphs; K = crops per frame; 1st/2nd = first calls of "
          "the shape):")
    print(format_row([name for name, _ in COLUMNS]))
    print("-" * (sum(w for _, w in COLUMNS) + 2 * (len(COLUMNS) - 1)))
    order = {"centroid": 0, "instance": 1, "pipeline": 2}
    for r in sorted(results, key=lambda r: (order[r["stage"]], r["p50_ms"])):
        print(format_row(row_cells(r)) + (f"  !! {r['warning']}" if "warning" in r else ""))


if __name__ == "__main__":
    main()
