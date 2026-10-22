#!/usr/bin/env python3
"""Does a compiled inference engine make campy-live's two models faster?

campy-live runs a sleap-nn top-down pair on every synchronized set of camera
frames: a centroid model on the whole frames, then a centered-instance model
on a crop around each animal. In torch, each layer of a network is its own
GPU kernel (or several): a convolution writes its result to memory, and the
bias, activation and next step read it back. A compiled engine (TensorRT,
MIGraphX, torch.compile's inductor) "fuses" those steps into the convolution's
kernel, so the intermediate results never make the round trip, and picks its
kernels for the exact input shape. CUDA graphs (campy-live's --cuda-graphs)
only remove the cost of launching the kernels; this measures the other half.

Only the networks are compiled (backbone + head, fixed input shapes);
preprocessing, cropping and peak finding stay in torch, as in campy-live. Each
engine reads its input from, and writes its output to, torch tensors' GPU
memory (MIGraphX argument_from_pointer, TensorRT tensor addresses on
data_ptr), on torch's current stream: no copies through the host. Each call's
time includes handing the input over and the fp32 output that peak finding
reads.

Engines (--engines; one that can't be built here is reported and skipped):

  eager-fp32       torch, fp32 (the accuracy reference)
  eager-fp16       torch, fp16 autocast: campy-live's default
  eager-fp16-cl    eager-fp16 in channels-last layout everywhere (campy-live uses it on
                   NVIDIA only); inductor picks channels-last for convolutions itself
  graph-fp16       eager-fp16 replayed as a CUDA graph (campy-live --cuda-graphs)
  compile-default  torch.compile (inductor), fp16 autocast
  compile-ro       torch.compile mode="reduce-overhead" (inductor + CUDA graphs)
  compile-ma       torch.compile mode="max-autotune" (tries kernel variants, + CUDA graphs)
  migraphx-fp16    AMD MIGraphX, fp16 (ROCm)
  migraphx-int8    MIGraphX, int8, calibrated on real frames (quantize_int8)
  trt-fp16         NVIDIA TensorRT, fp16 (ONNX exported in fp16: TensorRT 11
                   builds strongly typed networks only)
  trt-int8         TensorRT, int8: the ONNX carries Q/DQ (quantize/dequantize)
                   pairs at every convolution, with scales calibrated here on
                   real frames (TensorRT 11 has no implicit int8 calibrator)

What it does
------------
1. Loads the trained centroid and instance models (--centroid, --instance:
   sleap-nn model folders) with sleap-nn's Predictor, as campy-live
   --model sleap does. --weights random re-initialises the same networks
   (same shapes, same cost) for timing without trained weights.
2. Reads sets of frames from the replay videos (--videos, one per camera)
   and runs sleap-nn's whole top-down layer on them in fp32, recording the
   exact inputs its two networks see (the frames scaled and normalised; the
   crops around each animal it found). Some sets calibrate int8 (--calib-sets),
   others, interleaved with them, check accuracy (--accuracy-sets).
3. For each engine: builds it for each shape (centroid: the frames of one
   set; instance: --batches crops), times each model and each set (centroid
   + instance at each batch, one GPU sync per set, as campy-live waits) with
   sets started --pace times a second and the GPU clock held
   (--hold-clock-blocks spinning blocks on a lowest-priority stream, as
   sleap_infer_bench.py --hold-clock on), and compares its confidence maps
   and peaks with torch fp32's on the accuracy sets.

Accuracy: largest absolute confidence-map difference from torch fp32, and
peak positions after sleap-nn's own peak finding (integral refinement): the
instance model's keypoints (nodes whose fp32 peak is above --peak-threshold)
and the centroids (local peaks above --centroid-threshold, matched to the
nearest), both in full-frame pixels. Crop batches are padded to the engine's
batch (15) with zeros, as an engine with fixed shapes needs.

--profile also counts each engine's GPU kernels per call and the time they
take (torch.profiler): fusion shows as fewer, longer kernels.

Interop notes, learned the hard way:
* Every engine runs on a torch stream of the bench's own (not the default
  stream): on ROCm torch's default stream is the null stream, and MIGraphX's
  run_async given it ran on another stream the timer never waited for;
  TensorRT adds synchronizations of its own on the default stream.
* MIGraphX 2.15 (ROCm 7.2) can't compile its kernels inside a process that
  has loaded ROCm 10.1 torch wheels (torch's hiprtc headers clash with its
  kernels'), so programs are compiled by a separate Python with ROCm's
  migraphx and numpy only (--migraphx-python) and loaded back as JSON.
* torch.compile on Windows needs Triton: triton-windows, with CC pointing at
  the tcc.exe it bundles (triton/runtime/tcc/tcc.exe).

Run it under the rig lock (rig.sh / rig.py), like every GPU job on the rigs.
Needs: torch + sleap-nn (+ sleap_io for the videos), onnx for the exports,
migraphx (ROCm's Python bindings, /opt/rocm/lib) or tensorrt for those
engines. bench/sleap_infer_bench.py lends its clock hold, clock log and timer.
"""

import argparse
import copy
import json
import math
import os
import platform
import subprocess
import sys
import time
import traceback
import warnings
from datetime import datetime

import numpy as np


def mgx_compile_main(argv):
    """mgx-compile ONNX SHAPE PRECISION OUT.mxr [CALIB.npy]: compile a MIGraphX program, without torch.

    Run in its own process with ROCm's Python bindings (--migraphx-python). In
    a process with ROCm torch, MIGraphX's kernel compiler (hiprtc) is torch's
    newer one, whose headers MIGraphX 2.15's kernels don't compile with
    (ROCm 10.1's against ROCm 7.2's). The saved program holds compiled code
    objects, so the torch process only loads and runs it. Saved as JSON:
    MIGraphX's default (msgpack) files don't load in a process that has
    imported torch ("Not an object for field: version").
    """
    import migraphx
    onnx_path, shape, precision, out = argv[:4]
    shape = [int(v) for v in shape.split("x")]
    target = migraphx.get_target("gpu")
    prog = migraphx.parse_onnx(onnx_path, map_input_dims={"x": shape})
    if precision == "int8":
        calib = np.load(argv[4])
        migraphx.quantize_int8(prog, target, [{"x": migraphx.argument(np.ascontiguousarray(b))} for b in calib])
        migraphx.quantize_fp16(prog)  # what isn't int8 runs in fp16
    elif precision == "fp16":
        migraphx.quantize_fp16(prog)
    prog.compile(target, offload_copy=False)
    migraphx.save(prog, out, format="json")


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "mgx-compile":
    mgx_compile_main(sys.argv[2:])
    sys.exit(0)

import torch  # noqa: E402
import torch._dynamo  # noqa: E402
import torch.nn.functional as F  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sleap_infer_bench as sib  # noqa: E402  (ClockKeeper, clock logs, time_calls, CUDAGraphed)

ALL_ENGINES = ["eager-fp32", "eager-fp16", "eager-fp16-cl", "graph-fp16", "compile-default", "compile-ro", "compile-ma",
               "migraphx-fp16", "migraphx-int8", "trt-fp16", "trt-int8"]
HEADS = {"centroid": "CentroidConfmapsHead", "instance": "CenteredInstanceConfmapsHead"}


# --------------------------------------------------------------------------
# The networks
# --------------------------------------------------------------------------

class Net(torch.nn.Module):
    """Backbone + head of one sleap-nn model: normalised image in, confidence maps out."""

    def __init__(self, lightning_module, head):
        super().__init__()
        self.body = lightning_module.model
        self.head = head

    def forward(self, x):
        return self.body(x)[self.head]


class Half(torch.nn.Module):
    """fp32 in, fp16 network, fp32 out: what the fp16 ONNX for TensorRT holds."""

    def __init__(self, net):
        super().__init__()
        self.net = copy.deepcopy(net).half()

    def forward(self, x):
        return self.net(x.half()).float()


def randomize(net, seed=0):
    """The same network with fresh random weights (the timing doesn't depend on them)."""
    net = copy.deepcopy(net)
    torch.manual_seed(seed)
    for m in net.modules():
        if hasattr(m, "reset_parameters") and m is not net:
            m.reset_parameters()
    return net


class QDQConv(torch.nn.Module):
    """A Conv2d with int8 fake quantisation of its input (per tensor) and weight (per channel).

    Exported to ONNX, each becomes a QuantizeLinear + DequantizeLinear pair,
    which TensorRT reads as "run this convolution in int8 with these scales".
    """

    def __init__(self, conv, amax_in):
        super().__init__()
        self.conv = conv
        self.scale_in = max(float(amax_in), 1e-8) / 127.0
        w_amax = conv.weight.detach().abs().amax(dim=(1, 2, 3)).clamp(min=1e-8)
        self.register_buffer("scale_w", (w_amax / 127.0).float())
        self.register_buffer("zero_w", torch.zeros(conv.out_channels, dtype=torch.int32, device=conv.weight.device))

    def forward(self, x):
        c = self.conv
        x = torch.fake_quantize_per_tensor_affine(x, self.scale_in, 0, -128, 127)
        w = torch.fake_quantize_per_channel_affine(c.weight, self.scale_w, self.zero_w, 0, -128, 127)
        return F.conv2d(x, w, c.bias, c.stride, c.padding, c.dilation, c.groups)


def conv_input_amax(net, batches, percentile):
    """Each Conv2d's input range over the calibration batches: the max, or a percentile of |x|."""
    convs = [m for m in net.modules() if isinstance(m, torch.nn.Conv2d)]
    samples = {id(c): [] for c in convs}
    peaks = {id(c): 0.0 for c in convs}
    gen = torch.Generator(device=batches[0].device).manual_seed(0)

    def hook(module, inputs):
        a = inputs[0].detach().abs().flatten().float()
        peaks[id(module)] = max(peaks[id(module)], a.max().item())
        if percentile < 100:
            n = min(a.numel(), 200_000)
            idx = torch.randint(0, a.numel(), (n,), device=a.device, generator=gen)
            samples[id(module)].append(a[idx])

    handles = [c.register_forward_pre_hook(hook) for c in convs]
    try:
        with torch.no_grad():
            for x in batches:
                net(x)
    finally:
        for h in handles:
            h.remove()
    amax = {}
    for c in convs:
        if percentile < 100:
            s = torch.cat(samples[id(c)])
            if s.numel() > 8_000_000:
                s = s[torch.randperm(s.numel(), device=s.device)[:8_000_000]]
            amax[id(c)] = torch.quantile(s, percentile / 100).item()
        else:
            amax[id(c)] = peaks[id(c)]
    return convs, amax


def qdq_net(net, batches, percentile):
    """A copy of net (fp32) whose every Conv2d carries calibrated int8 Q/DQ."""
    net = copy.deepcopy(net).float()
    convs, amax = conv_input_amax(net, batches, percentile)
    replace = {id(c): QDQConv(c, amax[id(c)]) for c in convs}
    for parent in list(net.modules()):
        for name, child in list(parent.named_children()):
            if id(child) in replace:
                setattr(parent, name, replace[id(child)])
    return net


def export_onnx(module, shape, path, device):
    """Export module at one fixed input shape (input "x", output "y")."""
    import onnx  # noqa: F401  (torch.onnx needs it; fail early with its name)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    x = torch.zeros(shape, device=device)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        torch.onnx.export(module.eval(), (x,), path, input_names=["x"], output_names=["y"],
                          opset_version=17, dynamo=False, do_constant_folding=True)
    return path


# --------------------------------------------------------------------------
# Engines. Each is built for one network at one input shape, and called with
# a contiguous fp32 tensor on the GPU; it returns fp32 confidence maps on the
# GPU, valid until its next call.
# --------------------------------------------------------------------------

def autocast(enabled):
    return torch.autocast(device_type="cuda", dtype=torch.float16, enabled=enabled)


class Eager:
    def __init__(self, net, shape, fp16):
        self.net, self.fp16 = net, fp16

    def __call__(self, x):
        with autocast(self.fp16):
            return self.net(x).float()


class Graphed:
    """sleap_infer_bench's CUDAGraphed (one graph per shape, copy in, replay)."""

    def __init__(self, net, shape, fp16):
        self.graphed = sib.CUDAGraphed(net, fp16)

    def __call__(self, x):
        return self.graphed(x)


class Compiled:
    """torch.compile'd forward. Every engine and shape compiles the same code object
    (run), so dynamo's per-code cache needs room for them all (main() resets it
    between engines); past its limit dynamo silently runs eager."""

    def __init__(self, net, shape, fp16, mode):
        for key in ("cache_size_limit", "recompile_limit", "accumulated_cache_size_limit",
                    "accumulated_recompile_limit"):
            if hasattr(torch._dynamo.config, key):
                setattr(torch._dynamo.config, key, max(getattr(torch._dynamo.config, key), 256))
        kwargs = {} if mode == "default" else {"mode": mode}

        def run(x):
            with autocast(fp16):
                return net(x).float()
        self.fn = torch.compile(run, dynamic=False, fullgraph=True, **kwargs)

    def __call__(self, x):
        out = self.fn(x)
        return out


class MIGraphX:
    """A MIGraphX program on torch's memory and stream (offload_copy off)."""

    def __init__(self, onnx_path, shape, precision, calib=None, cache=None, python=None):
        import migraphx
        self.mgx = migraphx
        mxr = cache or os.path.splitext(onnx_path)[0] + "_{}.mxr.json".format(precision)
        if not (cache and os.path.exists(cache)):
            cmd = [python, os.path.abspath(__file__), "mgx-compile", onnx_path, "x".join(map(str, shape)),
                   precision, mxr]
            if calib is not None:
                npy = os.path.splitext(mxr)[0] + "_calib.npy"
                np.save(npy, torch.stack(calib).cpu().numpy().astype(np.float32))
                cmd.append(npy)
            if python:
                env = dict(os.environ)
                env.pop("LD_PRELOAD", None)
                subprocess.run(cmd, check=True, env=env)
            else:
                mgx_compile_main(cmd[3:])
        self.prog = migraphx.load(mxr, format="json")
        shapes = self.prog.get_parameter_shapes()
        self.in_shape = shapes["x"]
        outs = sorted(n for n in shapes.keys() if "#output" in n)
        if len(outs) != 1:
            raise RuntimeError("expected one output parameter, got {}".format(outs))
        self.out_name = outs[0]
        out_shape = shapes[self.out_name]
        dtype = {"float_type": torch.float32, "half_type": torch.float16}[out_shape.type_string()]
        self.out = torch.empty(out_shape.lens(), dtype=dtype, device="cuda")
        self.args = {self.out_name: migraphx.argument_from_pointer(out_shape, self.out.data_ptr())}
        # Scratch the program asks for, if any, also from torch
        self.scratch = []
        for name in shapes.keys():
            if name not in ("x", self.out_name):
                s = shapes[name]
                buf = torch.empty(max(s.bytes(), 1), dtype=torch.uint8, device="cuda")
                self.scratch.append(buf)
                self.args[name] = migraphx.argument_from_pointer(s, buf.data_ptr())
        self.run_async = hasattr(self.prog, "run_async")

    def __call__(self, x):
        self.args["x"] = self.mgx.argument_from_pointer(self.in_shape, x.data_ptr())
        if self.run_async:
            self.prog.run_async(self.args, torch.cuda.current_stream().cuda_stream, "ihipStream_t")
        else:
            torch.cuda.current_stream().synchronize()
            self.prog.run(self.args)
        return self.out if self.out.dtype == torch.float32 else self.out.float()


class TensorRT:
    """A TensorRT engine on torch's memory and stream."""

    def __init__(self, onnx_path, shape, cache=None, opt_level=3):
        import tensorrt as trt
        logger = trt.Logger(trt.Logger.WARNING)
        runtime = trt.Runtime(logger)
        if cache and os.path.exists(cache):
            with open(cache, "rb") as f:
                plan = f.read()
        else:
            flags = 0
            if hasattr(trt.NetworkDefinitionCreationFlag, "STRONGLY_TYPED"):
                flags |= 1 << int(trt.NetworkDefinitionCreationFlag.STRONGLY_TYPED)
            builder = trt.Builder(logger)
            network = builder.create_network(flags)
            parser = trt.OnnxParser(network, logger)
            if not parser.parse_from_file(onnx_path):
                raise RuntimeError("TensorRT can't parse {}: {}".format(
                    onnx_path, "; ".join(str(parser.get_error(i)) for i in range(parser.num_errors))))
            config = builder.create_builder_config()
            config.builder_optimization_level = opt_level
            plan = builder.build_serialized_network(network, config)
            if plan is None:
                raise RuntimeError("TensorRT build failed for {}".format(onnx_path))
            plan = bytes(plan)
            if cache:
                with open(cache, "wb") as f:
                    f.write(plan)
        self.engine = runtime.deserialize_cuda_engine(plan)
        self.ctx = self.engine.create_execution_context()
        out_shape = tuple(self.engine.get_tensor_shape("y"))
        self.out = torch.empty(out_shape, dtype=torch.float32, device="cuda")
        self.ctx.set_tensor_address("y", self.out.data_ptr())

    def __call__(self, x):
        self.ctx.set_tensor_address("x", x.data_ptr())
        self.ctx.execute_async_v3(torch.cuda.current_stream().cuda_stream)
        return self.out


def build_engine(name, net, shape, model, ctx):
    """One engine for net at shape. ctx: device, workdir, calibration batches, options."""
    nvidia = torch.version.hip is None
    tag = "{}_{}_{}".format(ctx["weights"], model, "x".join(map(str, shape)))
    work = ctx["workdir"]
    if name in ("eager-fp32", "eager-fp16", "eager-fp16-cl", "graph-fp16", "compile-default", "compile-ro",
                "compile-ma"):
        fp16 = name != "eager-fp32"
        if nvidia or name == "eager-fp16-cl":  # campy-live's default on NVIDIA (cuDNN's layout)
            net = copy.deepcopy(net).to(memory_format=torch.channels_last)
        if name.startswith("eager"):
            return Eager(net, shape, fp16)
        if name == "graph-fp16":
            return Graphed(net, shape, fp16)
        mode = {"compile-default": "default", "compile-ro": "reduce-overhead",
                "compile-ma": "max-autotune"}[name]
        return Compiled(net, shape, fp16, mode)
    if name.startswith("migraphx"):
        if nvidia:
            raise RuntimeError("MIGraphX: AMD GPUs only")
        precision = name.split("-")[1]
        onnx_path = export_onnx(net, shape, os.path.join(work, tag + "_fp32.onnx"), ctx["device"])
        calib = None
        if precision == "int8":
            calib = calibration_batches(ctx["calib"][model], shape[0])
        return MIGraphX(onnx_path, shape, precision, calib, python=ctx["migraphx_python"],
                        cache=os.path.join(work, "{}_{}.mxr.json".format(tag, name)) if ctx["cache"] else None)
    if name.startswith("trt"):
        if not nvidia:
            raise RuntimeError("TensorRT: NVIDIA GPUs only")
        if name == "trt-fp16":
            module = Half(net)
        else:
            batches = calibration_batches(ctx["calib"][model], shape[0])
            module = qdq_net(net, batches, ctx["calib_percentile"])
        onnx_path = export_onnx(module, shape, os.path.join(work, "{}_{}.onnx".format(tag, name)), ctx["device"])
        return TensorRT(onnx_path, shape, opt_level=ctx["trt_opt_level"],
                        cache=os.path.join(work, "{}_{}.plan".format(tag, name)) if ctx["cache"] else None)
    raise ValueError(name)


def calibration_batches(pool, n):
    """Calibration inputs at batch n: whole sets for the centroid, crops regrouped for the instance."""
    if isinstance(pool, list):  # centroid: one (frames, 1, H, W) tensor per set
        if pool[0].shape[0] == n:
            return pool
        pool = torch.cat(pool)
    k = pool.shape[0] // n
    return [pool[i * n:(i + 1) * n] for i in range(k)]


# --------------------------------------------------------------------------
# Real inputs: sleap-nn's top-down layer on the replay videos, in fp32
# --------------------------------------------------------------------------

class Recorder(torch.nn.Module):
    """Stands in for a backend's Lightning module: records the normalised input, then runs it."""

    def __init__(self, lm):
        super().__init__()
        self.lm = lm
        self.seen = []

    def forward(self, img):
        from sleap_nn.data.normalization import normalize_on_gpu
        x = img.squeeze(1) if img.ndim == 5 else img
        self.seen.append(normalize_on_gpu(x.to("cuda")).float().contiguous())
        return self.lm(img)


def read_sets(videos, indices):
    """Frames index i of every video, as (cameras, H, W, 1) uint8 arrays."""
    import sleap_io as sio
    vids = [sio.load_video(v) for v in videos]
    for i in indices:
        frames = []
        for v in vids:
            f = np.asarray(v[int(i)])
            if f.ndim == 3 and f.shape[-1] == 3:
                f = f[..., :1]
            frames.append(f.reshape(f.shape[0], f.shape[1], 1))
        yield np.stack(frames).astype(np.uint8)
    for v in vids:
        try:
            v.close()
        except Exception:
            pass


def video_length(path):
    import sleap_io as sio
    v = sio.load_video(path)
    n = len(v)
    try:
        v.close()
    except Exception:
        pass
    return n


def capture_inputs(layer, videos, n_sets, offset):
    """Run the fp32 layer on n_sets sets spread through the videos; return each network's inputs per set."""
    total = min(video_length(v) for v in videos)
    step = total / n_sets
    indices = [int(offset * step + i * step) % total for i in range(n_sets)]
    cl, il = layer.centroid_layer.backend, layer.centered_instance_layer.backend
    rec_c, rec_i = Recorder(cl.model), Recorder(il.model)
    saved = (cl.model, il.model, cl.use_fp16, il.use_fp16)
    cl.model, il.model, cl.use_fp16, il.use_fp16 = rec_c, rec_i, False, False
    sets = []
    try:
        for frames in read_sets(videos, indices):
            rec_c.seen.clear()
            rec_i.seen.clear()
            with torch.inference_mode():
                layer.predict(torch.from_numpy(frames).to("cuda"))
            # clone: out of inference mode, so torch.compile and graphs can take them
            c = rec_c.seen[0].clone()
            i = torch.cat(rec_i.seen).clone() if rec_i.seen else c.new_zeros((0,) + c.shape[1:2] + (0, 0))
            sets.append((c, i))
    finally:
        cl.model, il.model, cl.use_fp16, il.use_fp16 = saved
    return indices, sets


# --------------------------------------------------------------------------
# Accuracy
# --------------------------------------------------------------------------

def global_peaks(cm, threshold):
    from sleap_nn.inference.ops.peaks import find_global_peaks
    pts, vals = find_global_peaks(cm, threshold=-math.inf, refinement="integral")
    return pts, vals


def tied_peaks(cm, threshold):
    """Pixels above threshold that equal their largest neighbour: a plateau top, which
    find_local_peaks (strictly greater than all 8 neighbours) drops, animal and all."""
    from sleap_nn.inference.ops.peaks import morphological_dilation
    neighbours = morphological_dilation(cm, None)
    return int(((cm == neighbours) & (cm > threshold)).sum())


def local_peaks(cm, threshold):
    from sleap_nn.inference.ops.peaks import find_local_peaks
    pts, vals, sample, _ = find_local_peaks(cm, threshold=threshold, refinement="integral")
    return pts, vals, sample


class Accuracy:
    """Collects one engine's differences from torch fp32 on the accuracy sets."""

    def __init__(self, c_px, i_px, peak_threshold, centroid_threshold):
        self.c_px, self.i_px = c_px, i_px  # confmap pixel -> full-frame pixels
        self.pt, self.ct = peak_threshold, centroid_threshold
        self.cm_c, self.cm_i, self.kp, self.cen = [], [], [], []
        self.flips = self.nodes = self.missed = self.extra = self.centroids = 0
        self.ties_ref = self.ties_out = 0

    def instance(self, ref, out):
        if ref.shape[0] == 0:
            return
        self.cm_i.append((out - ref).abs().max().item())
        rp, rv = global_peaks(ref, self.pt)
        op, ov = global_peaks(out, self.pt)
        on_r, on_o = rv >= self.pt, ov >= self.pt
        self.nodes += int(on_r.sum())
        self.flips += int((on_r != on_o).sum())
        both = on_r & on_o
        d = (op - rp).norm(dim=-1)[both] * self.i_px
        self.kp.append(d.cpu().numpy())

    def centroid(self, ref, out):
        self.cm_c.append((out - ref).abs().max().item())
        self.ties_ref += tied_peaks(ref, self.ct)
        self.ties_out += tied_peaks(out, self.ct)
        rp, _, rs = local_peaks(ref, self.ct)
        op, _, os_ = local_peaks(out, self.ct)
        self.centroids += len(rp)
        used = set()
        for j in range(len(rp)):
            cand = (os_ == rs[j]).nonzero().flatten().tolist()
            cand = [c for c in cand if c not in used]
            if not cand:
                self.missed += 1
                continue
            dist = (op[cand] - rp[j]).norm(dim=-1)
            k = int(dist.argmin())
            if dist[k].item() * self.c_px > 20:  # 20 full-frame px: a different animal
                self.missed += 1
                continue
            used.add(cand[k])
            self.cen.append(dist[k].item() * self.c_px)
        self.extra += len(op) - len(used)

    def summary(self):
        kp = np.concatenate(self.kp) if self.kp else np.zeros(0)
        cen = np.asarray(self.cen)

        def pct(a, q):
            return float(np.percentile(a, q)) if len(a) else None
        return {
            "centroid_cm_maxabs": max(self.cm_c) if self.cm_c else None,
            "instance_cm_maxabs": max(self.cm_i) if self.cm_i else None,
            "instance_cm_maxabs_p50": pct(self.cm_i, 50),
            "keypoint_shift_px_p50": pct(kp, 50), "keypoint_shift_px_p95": pct(kp, 95),
            "keypoint_shift_px_p99": pct(kp, 99),
            "keypoints_shifted_over_1px": float((kp > 1).mean()) if len(kp) else None,
            "keypoint_shift_px_max": float(kp.max()) if len(kp) else None,
            "keypoints_compared": int(len(kp)), "keypoint_nodes_visible_fp32": self.nodes,
            "keypoint_visibility_flips": self.flips,
            "centroid_shift_px_p50": pct(cen, 50), "centroid_shift_px_p99": pct(cen, 99),
            "centroid_shift_px_max": float(cen.max()) if len(cen) else None,
            "centroids_fp32": self.centroids, "centroids_missed": self.missed, "centroids_extra": self.extra,
            "centroid_plateau_tops_fp32": self.ties_ref, "centroid_plateau_tops": self.ties_out,
        }


def check_accuracy(engines, nets_fp32, sets, batch, args):
    """Run the engine on every accuracy set; crops padded to its batch."""
    acc = Accuracy(args.c_px, args.i_px, args.peak_threshold, args.centroid_threshold)
    with torch.no_grad():
        for c, crops in sets:
            ref = nets_fp32["centroid"](c).float()
            out = engines["centroid"](c).clone()
            acc.centroid(ref, out)
            for s in range(0, crops.shape[0], batch):
                chunk = crops[s:s + batch]
                n = chunk.shape[0]
                if n < batch:
                    chunk = torch.cat([chunk, chunk.new_zeros((batch - n,) + chunk.shape[1:])])
                ref = nets_fp32["instance"](chunk[:n]).float()
                out = engines["instance"](chunk.contiguous())[:n].clone()
                acc.instance(ref, out)
    return acc.summary()


# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------

def timed(fn, args, keeper, clock_log):
    if keeper is not None:
        keeper.start()
    try:
        first, second, lat, _, overruns, clock = sib.time_calls(
            fn, "cuda", args.iters, args.warmup, pace=args.pace, clock_log=clock_log)
    finally:
        spins = keeper.end() if keeper is not None else None
    row = sib.summarize(first, second, lat)
    row["overruns"] = overruns
    row["clock"] = clock
    row["hold_clock_spins"] = spins
    return row


def profile_kernels(fn, calls=30, top=8):
    """GPU kernels fn() launches per call and their time (torch.profiler), the busiest first."""
    from torch.autograd import DeviceType
    from torch.profiler import ProfilerActivity, profile
    for _ in range(5):
        fn()
    torch.cuda.synchronize()
    with profile(activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA]) as prof:
        for _ in range(calls):
            fn()
        torch.cuda.synchronize()
    kernels = [e for e in prof.key_averages() if getattr(e, "device_type", None) == DeviceType.CUDA]

    def us(e):
        return getattr(e, "self_device_time_total", None) or getattr(e, "self_cuda_time_total", 0.0)
    kernels.sort(key=us, reverse=True)
    total = sum(us(e) for e in kernels)
    return {"kernels_per_call": sum(e.count for e in kernels) / calls,
            "kernel_ms_per_call": total / calls / 1e3,
            "top": [{"name": e.key[:120], "per_call": e.count / calls, "ms_per_call": us(e) / calls / 1e3}
                    for e in kernels[:top]]}


def fmt(v, nd=2):
    return "-" if v is None else "{:.{}f}".format(v, nd)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--centroid", required=True, help="trained sleap-nn centroid model folder")
    p.add_argument("--instance", required=True, help="trained sleap-nn centered-instance model folder")
    p.add_argument("--videos", nargs="+", required=True, help="replay videos, one per camera")
    p.add_argument("--engines", default="eager-fp32,eager-fp16,graph-fp16,compile-default,compile-ro,"
                   "compile-ma,migraphx-fp16,migraphx-int8,trt-fp16,trt-int8",
                   help="comma-separated, from: " + ",".join(ALL_ENGINES))
    p.add_argument("--weights", default="trained", choices=["trained", "random"],
                   help="random: the same networks re-initialised (timing only; accuracy is skipped)")
    p.add_argument("--batches", default="3,5,10,15", help="instance-model crop batches to build and time")
    p.add_argument("--accuracy-batch", type=int, default=15, help="the instance batch accuracy is checked at")
    p.add_argument("--accuracy-sets", type=int, default=500, help="sets (frames from every camera) to compare")
    p.add_argument("--calib-sets", type=int, default=100, help="sets the int8 engines calibrate on")
    p.add_argument("--calib-percentile", type=float, default=99.99,
                   help="trt-int8: activation range = this percentile of |x| per layer (100 = max)")
    p.add_argument("--max-instances", type=int, default=5)
    p.add_argument("--peak-threshold", type=float, default=0.2)
    p.add_argument("--centroid-threshold", type=float, default=0.2)
    p.add_argument("--iters", type=int, default=2000)
    p.add_argument("--warmup", type=int, default=100)
    p.add_argument("--pace", type=float, default=60.0, help="sets started per second (0: back to back)")
    p.add_argument("--hold-clock-blocks", type=int, default=1, help="GPU clock hold: spinning blocks (0: off)")
    p.add_argument("--trt-opt-level", type=int, default=3, help="TensorRT builder optimisation level (0-5)")
    p.add_argument("--migraphx-python", default="python3",
                   help="Python with ROCm's migraphx (and numpy, not torch) that compiles the MIGraphX programs; "
                   "'' compiles in this process (fails with torch's ROCm 10.1 wheels and MIGraphX 2.15)")
    p.add_argument("--workdir", default="compiled_work", help="ONNX files and engine caches")
    p.add_argument("--cache", action="store_true", help="reuse built TensorRT plans / MIGraphX programs")
    p.add_argument("--profile", action="store_true",
                   help="also count each engine's GPU kernels per call (torch.profiler) and where the time goes")
    p.add_argument("--json", default=None)
    args = p.parse_args()

    if not torch.cuda.is_available():
        sys.exit("needs a CUDA or ROCm GPU")
    # fp32 means fp32: no TF32 in the reference (torch's default lets cuDNN use it for convolutions)
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cuda.matmul.allow_tf32 = False
    engines = [e for e in args.engines.split(",") if e]
    for e in engines:
        if e not in ALL_ENGINES:
            sys.exit("unknown engine {}".format(e))
    batches = [int(b) for b in args.batches.split(",")]
    if args.accuracy_batch not in batches:
        batches.append(args.accuracy_batch)
    json_path = args.json or datetime.now().strftime("compiled_%Y%m%d_%H%M%S.json")
    os.makedirs(args.workdir, exist_ok=True)

    from sleap_nn.inference.predictor import Predictor
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        predictor = Predictor.from_model_paths(
            [args.centroid, args.instance], device="cuda", max_instances=args.max_instances,
            peak_threshold=[args.centroid_threshold, args.peak_threshold])
    layer = predictor.layer
    cl, il = layer.centroid_layer, layer.centered_instance_layer
    nets = {"centroid": Net(cl.backend.model, HEADS["centroid"]).eval(),
            "instance": Net(il.backend.model, HEADS["instance"]).eval()}
    c_scale = float(cl.preprocess_config.scale)
    i_scale = float(il.preprocess_config.scale)
    args.c_px = cl.output_stride / c_scale
    args.i_px = il.output_stride / i_scale

    device = sib.device_description("cuda")
    print("device:", device, "| torch", torch.__version__, "| sleap-nn", sib.sleap_nn.__version__, flush=True)

    t0 = time.perf_counter()
    acc_idx, acc_sets = capture_inputs(layer, args.videos, args.accuracy_sets, 0.0)
    cal_idx, cal_sets = capture_inputs(layer, args.videos, args.calib_sets, 0.5)
    crops_seen = [s[1].shape[0] for s in acc_sets]
    print("captured {} accuracy + {} calibration sets in {:.0f} s; crops per set: mean {:.1f}, max {}".format(
        len(acc_sets), len(cal_sets), time.perf_counter() - t0, np.mean(crops_seen), max(crops_seen)), flush=True)
    c_shape = tuple(acc_sets[0][0].shape)
    crop_hw = None
    for _, crops in acc_sets + cal_sets:
        if crops.shape[0]:
            crop_hw = tuple(crops.shape[1:])
            break
    if crop_hw is None:
        sys.exit("the centroid model found no animals in the replay frames")
    shapes = {"centroid": [c_shape], "instance": [(b,) + crop_hw for b in batches]}
    print("shapes: centroid {}, instance {}".format(c_shape, ["x".join(map(str, s)) for s in shapes["instance"]]))
    calib = {"centroid": [s[0] for s in cal_sets],
             "instance": torch.cat([s[1] for s in cal_sets if s[1].shape[0]])}

    fp32_ref = {k: Eager(n, None, False) for k, n in nets.items()}
    if args.weights == "random":
        nets = {k: randomize(n) for k, n in nets.items()}

    # Timing inputs: real ones (the first set's frames; its crops repeated to each batch)
    pool = calib["instance"]
    x_in = {("centroid", c_shape): acc_sets[0][0].contiguous()}
    for s in shapes["instance"]:
        reps = math.ceil(s[0] / pool.shape[0])
        x_in[("instance", s)] = pool.repeat(reps, 1, 1, 1)[:s[0]].contiguous()

    keeper = sib.ClockKeeper(args.hold_clock_blocks) if args.hold_clock_blocks > 0 else None
    clock_log = sib.make_clock_log("cuda")
    ctx = {"device": "cuda", "workdir": args.workdir, "calib": calib, "weights": args.weights,
           "calib_percentile": args.calib_percentile, "cache": args.cache, "trt_opt_level": args.trt_opt_level,
           "migraphx_python": args.migraphx_python or None}

    results = {"device": device, "host": platform.node(), "torch": torch.__version__,
               "sleap_nn": sib.sleap_nn.__version__, "weights": args.weights, "args": vars(args),
               "accuracy_frame_indices": acc_idx, "calibration_frame_indices": cal_idx,
               "shapes": {k: [list(s) for s in v] for k, v in shapes.items()}, "engines": {}}

    def save():
        with open(json_path, "w") as f:
            json.dump(results, f, indent=1, default=str)

    # Every engine runs on one stream of torch's own, which the timer waits on. Not the
    # default stream: on ROCm that is the null stream (handle 0), and MIGraphX's
    # run_async given 0 ran on a stream of its own, unseen by the timer
    torch.cuda.synchronize()
    torch.cuda.set_stream(torch.cuda.Stream())
    for name in engines:
        print("\n== {}".format(name), flush=True)
        res = {"rows": [], "error": None}
        results["engines"][name] = res
        built = {}
        if name.startswith("compile"):
            torch._dynamo.reset()
        try:
            t0 = time.perf_counter()
            with torch.no_grad():
                for model in ("centroid", "instance"):
                    for s in shapes[model]:
                        tb = time.perf_counter()
                        built[(model, s)] = build_engine(name, nets[model], s, model, ctx)
                        # The first call (compiles / autotunes for some engines)
                        built[(model, s)](x_in[(model, s)])
                        torch.cuda.synchronize()
                        print("  built {} {} in {:.1f} s".format(model, "x".join(map(str, s)),
                                                                time.perf_counter() - tb), flush=True)
            res["build_s"] = time.perf_counter() - t0
        except Exception as e:
            res["error"] = "{}: {}".format(type(e).__name__, e)
            res["traceback"] = traceback.format_exc()
            print("  can't build: " + res["error"], flush=True)
            built.clear()
            torch.cuda.empty_cache()
            save()
            continue

        try:
            with torch.no_grad():
                rows = []
                for (model, s), eng in built.items():
                    x = x_in[(model, s)]
                    rows.append(("model", model, s, lambda eng=eng, x=x: eng(x)))
                ce, cx = built[("centroid", c_shape)], x_in[("centroid", c_shape)]
                for s in shapes["instance"]:
                    ie, ix = built[("instance", s)], x_in[("instance", s)]
                    rows.append(("set", "centroid+instance", s,
                                 lambda ce=ce, cx=cx, ie=ie, ix=ix: (ce(cx), ie(ix))))
                for kind, model, s, fn in rows:
                    row = timed(fn, args, keeper, clock_log)
                    row.update(kind=kind, model=model, shape=list(s))
                    res["rows"].append(row)
                    clk = row["clock"]["sm_mhz_p50"] if row.get("clock") else None
                    print("  {:5s} {:18s} {:14s} p50 {:6.2f}  p99 {:6.2f}  max {:6.2f} ms  overruns {:4d}  "
                          "clock {}".format(kind, model, "x".join(map(str, s)), row["p50_ms"], row["p99_ms"],
                                            row["max_ms"], row["overruns"], fmt(clk, 0)), flush=True)
            if args.profile:
                res["profile"] = {}
                for key in (("centroid", c_shape), ("instance", (args.accuracy_batch,) + crop_hw)):
                    eng, x = built[key], x_in[key]
                    with torch.no_grad():
                        prof = profile_kernels(lambda eng=eng, x=x: eng(x))
                    res["profile"][key[0]] = prof
                    print("  profile {:8s} {:5.0f} kernels per call, {:.2f} ms of GPU time; busiest: {}".format(
                        key[0], prof["kernels_per_call"], prof["kernel_ms_per_call"], "; ".join(
                            "{:.2f} ms x{:g} {}".format(t["ms_per_call"], t["per_call"], t["name"][:60])
                            for t in prof["top"][:3])), flush=True)
            save()
            if args.weights == "trained":
                ta = time.perf_counter()
                res["accuracy"] = check_accuracy(
                    {"centroid": built[("centroid", c_shape)],
                     "instance": built[("instance", (args.accuracy_batch,) + crop_hw)]},
                    fp32_ref, acc_sets, args.accuracy_batch, args)
                a = res["accuracy"]
                print("  accuracy ({:.0f} s): confmap max|diff| centroid {} instance {}; keypoints shift p50 {} "
                      "p99 {} max {} px ({} compared, {} visibility flips); centroids shift p99 {} px, "
                      "{} missed, {} extra of {} (plateau tops {})".format(
                          time.perf_counter() - ta, fmt(a["centroid_cm_maxabs"], 4), fmt(a["instance_cm_maxabs"], 4),
                          fmt(a["keypoint_shift_px_p50"]), fmt(a["keypoint_shift_px_p99"]),
                          fmt(a["keypoint_shift_px_max"]), a["keypoints_compared"], a["keypoint_visibility_flips"],
                          fmt(a["centroid_shift_px_p99"]), a["centroids_missed"], a["centroids_extra"],
                          a["centroids_fp32"], a["centroid_plateau_tops"]), flush=True)
        except Exception as e:
            res["error"] = "{}: {}".format(type(e).__name__, e)
            res["traceback"] = traceback.format_exc()
            print("  failed: " + res["error"], flush=True)
        built.clear()
        torch.cuda.empty_cache()
        save()

    print("\nSummary (ms per call, paced {} /s, clock hold {} blocks) -> {}".format(
        args.pace, args.hold_clock_blocks, json_path))
    print("{:16s} {:5s} {:18s} {:14s} {:>7s} {:>7s}".format("engine", "kind", "model", "shape", "p50", "p99"))
    for name, res in results["engines"].items():
        if res["error"] and not res["rows"]:
            print("{:16s} {}".format(name, res["error"][:100]))
        for r in res["rows"]:
            print("{:16s} {:5s} {:18s} {:14s} {:7.2f} {:7.2f}".format(
                name, r["kind"], r["model"], "x".join(map(str, r["shape"])), r["p50_ms"], r["p99_ms"]))
    save()


if __name__ == "__main__":
    main()
