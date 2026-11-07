#!/usr/bin/env python3
"""How much do the centroids and keypoints move with sleap-nn 0.3.5's peak rule?

The centroids of animals are the local peaks of the centroid model's
confidence map. In fp16 (campy-live's default) the two top pixels of a peak
sometimes round to the same value (fp32 never ties). Three rules for those:

  strict  sleap-nn before 0.3.5: a peak is one pixel greater than all 8
          neighbours, so a tied top gives no peak and the animal is lost
          (about 0.7% of centroids on a 5-mouse replay, PR #127)
  first   campy-live before it needed sleap-nn 0.3.5 (pull request 132):
          the first tied pixel in reading order is the peak
  flat    sleap-nn 0.3.5 on (talmolab/sleap-nn#782), what campy-live uses
          now: a flat top that fits in a 2x2 block is one peak at the mean
          of its pixels (half a pixel off the grid for a pair), and the
          sub-pixel refinement is centred on the pixel that point floors to;
          a wider flat top, or one next to a higher pixel, gives no peak

This runs the trained top-down pair the way campy-live --model sleap does
(sleap-nn's TopDownLayer, fp16 autocast in its TorchBackend) on sets of
frames from the replay videos (--videos, one per camera), with sleap-nn
0.3.5's own rule and with the two older ones put in its place by this script
(sleap-nn's find_local_peaks_rough, which find_local_peaks calls):

  fp32-flat     the reference
  fp32-first    fp32, campy's old rule: should equal the reference
  fp16-strict   fp16, sleap-nn's rule before 0.3.5
  fp16-first    fp16, campy-live before this change
  fp16-flat     fp16, campy-live now

and compares each with the reference, frame by frame (each camera's image):
centroids matched to the nearest reference centroid within --match-px
full-frame pixels (missed: a reference centroid with none; extra: one with no
reference centroid), the centroid and keypoint shifts of matched animals.
Then fp16-flat against fp16-first directly (what campy-live's results do on
this change): animals found by each, and how far the keypoints of the
animals both found moved (median, over 1 px, largest); and fp16-flat run
twice, for how much fp16 keypoints move from one run to the next anyway
(some GPU kernels are not deterministic, and the instance model's batch is
every camera's crops at once).

Run it under the rig lock (rig.sh / rig.py) like every GPU job on the rigs.
Needs torch, sleap-nn 0.3.5 or later and sleap_io, campy on the path.
"""

import argparse
import json
import platform
import sys
import time
import warnings

import numpy as np
import torch

MODES = ["fp32-flat", "fp32-first", "fp16-strict", "fp16-first", "fp16-flat"]


def Rough(rule):
    """find_local_peaks_rough with an older peak rule: "strict" (sleap-nn
    before 0.3.5) or "first" (campy's, pull request 132). Integer points, so
    sleap-nn 0.3.5's refinement (around the floored point) refines them where
    the older code did."""
    import torch.nn.functional as F

    def rough(cms, threshold=0.2):
        p = F.pad(cms, (1, 1, 1, 1), mode="constant", value=float("-inf"))
        h, w = cms.shape[-2:]
        n = [p[..., 1 + dr:1 + dr + h, 1 + dc:1 + dc + w] for dr in (-1, 0, 1) for dc in (-1, 0, 1) if dr or dc]
        before = torch.stack(n[:4]).amax(0)
        after = torch.stack(n[4:]).amax(0)
        mask = (cms > before) & (cms > threshold)
        mask &= (cms > after) if rule == "strict" else (cms >= after)
        subs = torch.stack(torch.where(mask.permute(0, 2, 3, 1)), dim=-1)
        vals = cms[subs[:, 0], subs[:, 3], subs[:, 1], subs[:, 2]]
        return subs[:, [2, 1]].to(torch.float32), vals, subs[:, 0].to(torch.int32), subs[:, 3].to(torch.int32)
    return rough


def read_sets(videos, first, count):
    import sleap_io as sio
    vids = [sio.load_video(v) for v in videos]
    total = min(len(v) for v in vids)
    for i in range(first, min(first + count, total) if count else total):
        frames = []
        for v in vids:
            f = np.asarray(v[i])
            if f.ndim == 3 and f.shape[-1] == 3:
                f = f[..., :1]
            frames.append(f.reshape(f.shape[0], f.shape[1], 1))
        yield i, np.stack(frames).astype(np.uint8)


def match(ref, out, px):
    """Greedy nearest matching of out's centroids to ref's (each (k, 2), no NaN): pairs (i_ref, i_out)."""
    pairs, used = [], set()
    if len(ref) == 0 or len(out) == 0:
        return pairs
    d = np.linalg.norm(ref[:, None] - out[None], axis=-1)
    for flat in np.argsort(d, axis=None):
        i, j = divmod(int(flat), d.shape[1])
        if d[i, j] > px:
            break
        if i in {p[0] for p in pairs} or j in used:
            continue
        pairs.append((i, j))
        used.add(j)
    return pairs


class Compare:
    def __init__(self, px):
        self.px = px
        self.found = self.missed = self.extra = 0
        self.cen, self.kp = [], []
        self.missed_frames = []

    def add(self, key, ref, out):
        """Returns, per image, the reference centroids matched: {i_ref: (centroid shift, keypoint shifts)}."""
        rc, oc = ref["centroids"], out["centroids"]
        per_image = []
        for b in range(rc.shape[0]):
            ri = np.flatnonzero(~np.isnan(rc[b, :, 0]))
            oi = np.flatnonzero(~np.isnan(oc[b, :, 0]))
            pairs = match(rc[b, ri], oc[b, oi], self.px)
            self.found += len(pairs)
            self.missed += len(ri) - len(pairs)
            self.extra += len(oi) - len(pairs)
            if len(ri) > len(pairs):
                self.missed_frames.append([key, b])
            got = {}
            for i, j in pairs:
                c = float(np.linalg.norm(rc[b, ri[i]] - oc[b, oi[j]]))
                d = np.linalg.norm(ref["keypoints"][b, ri[i]] - out["keypoints"][b, oi[j]], axis=-1)
                d = d[~np.isnan(d)].tolist()
                self.cen.append(c)
                self.kp.extend(d)
                got[int(ri[i])] = (c, d)
            per_image.append(got)
        return per_image

    def summary(self):
        def pct(a, q):
            return float(np.percentile(a, q)) if len(a) else None
        return {"centroids_matched": self.found, "centroids_missed": self.missed, "centroids_extra": self.extra,
                "centroid_shift_px_p50": pct(self.cen, 50), "centroid_shift_px_p99": pct(self.cen, 99),
                "centroid_shift_px_max": max(self.cen) if self.cen else None,
                "keypoint_shift_px_p50": pct(self.kp, 50), "keypoint_shift_px_p99": pct(self.kp, 99),
                "keypoint_shift_px_max": max(self.kp) if self.kp else None,
                "frames_with_misses": self.missed_frames[:200]}


def Moved(a, b, px):
    """Animals found by a and by b (per image, matched by centroid within
    px), and the keypoint moves of those both found."""
    out = {"a": 0, "b": 0, "both": 0, "kp": []}
    for i in range(a["centroids"].shape[0]):
        ai = np.flatnonzero(~np.isnan(a["centroids"][i, :, 0]))
        bi = np.flatnonzero(~np.isnan(b["centroids"][i, :, 0]))
        pairs = match(a["centroids"][i, ai], b["centroids"][i, bi], px)
        out["a"] += len(ai)
        out["b"] += len(bi)
        out["both"] += len(pairs)
        for j, k in pairs:
            d = np.linalg.norm(a["keypoints"][i, ai[j]] - b["keypoints"][i, bi[k]], axis=-1)
            out["kp"].extend(d[~np.isnan(d)].tolist())
    return out


def MoveSummary(m):
    kp = np.asarray(m.pop("kp"))
    return m | {"keypoints": len(kp), "keypoint_move_px_p50": float(np.median(kp)) if len(kp) else None,
                "keypoint_move_px_p99": float(np.percentile(kp, 99)) if len(kp) else None,
                "keypoint_move_px_max": float(kp.max()) if len(kp) else None,
                "keypoints_moved": int((kp > 0).sum()), "keypoints_moved_over_1px": int((kp > 1).sum())}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--centroid", required=True, help="trained sleap-nn centroid model folder")
    p.add_argument("--instance", required=True, help="trained sleap-nn centered-instance model folder")
    p.add_argument("--videos", nargs="+", required=True, help="replay videos, one per camera")
    p.add_argument("--first", type=int, default=0, help="first frame index")
    p.add_argument("--sets", type=int, default=0, help="sets to check (0: every frame of the videos)")
    p.add_argument("--max-instances", type=int, default=5)
    p.add_argument("--peak-threshold", type=float, default=0.2)
    p.add_argument("--centroid-threshold", type=float, default=0.2)
    p.add_argument("--match-px", type=float, default=20.0, help="farther from every reference centroid: another animal")
    p.add_argument("--json", default="tied_peaks_check.json")
    args = p.parse_args()

    if not torch.cuda.is_available():
        sys.exit("needs a CUDA or ROCm GPU (fp16 autocast)")
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cuda.matmul.allow_tf32 = False
    import sleap_nn
    from sleap_nn.inference.ops import peaks as ops
    from sleap_nn.inference.predictor import Predictor
    from campy import live
    if live.SleapProblem():
        sys.exit(live.SleapProblem())
    rules = {"flat": ops.find_local_peaks_rough, "first": Rough("first"), "strict": Rough("strict")}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        predictor = Predictor.from_model_paths([args.centroid, args.instance], device="cuda",
            max_instances=args.max_instances, peak_threshold=[args.centroid_threshold, args.peak_threshold])
    layer = predictor.layer
    backends = [layer.centroid_layer.backend, layer.centered_instance_layer.backend]

    def run(frames, mode):
        precision, rule = mode.split("-")
        for backend in backends:
            backend.use_fp16 = precision == "fp16"
        ops.find_local_peaks_rough = rules[rule]
        try:
            with torch.inference_mode():
                out = layer.predict(frames)
        finally:
            ops.find_local_peaks_rough = rules["flat"]
        return {k: getattr(out, f).detach().float().cpu().numpy() for k, f in (("centroids", "pred_centroids"),
            ("keypoints", "pred_keypoints"))}

    cmp = {m: Compare(args.match_px) for m in MODES[1:]}
    change = {"a": 0, "b": 0, "both": 0, "kp": []} # a: fp16-first (before), b: fp16-flat (now)
    again = {"a": 0, "b": 0, "both": 0, "kp": []} # fp16-flat run twice
    reference_total, sets, t0 = 0, 0, time.perf_counter()
    for i, host in read_sets(args.videos, args.first, args.sets):
        frames = torch.from_numpy(host).to("cuda")
        r = {m: run(frames, m) for m in MODES}
        ref = r["fp32-flat"]
        reference_total += int((~np.isnan(ref["centroids"][..., 0])).sum())
        for m in MODES[1:]:
            cmp[m].add(i, ref, r[m])
        for acc, m in ((change, Moved(r["fp16-first"], r["fp16-flat"], args.match_px)),
                       (again, Moved(r["fp16-flat"], run(frames, "fp16-flat"), args.match_px))):
            for k in acc:
                acc[k] += m[k]
        sets += 1
        if sets % 500 == 0:
            print("{} sets, {:.0f} s: fp16 missed: strict {}, first {}, flat {}".format(sets, time.perf_counter() - t0,
                cmp["fp16-strict"].missed, cmp["fp16-first"].missed, cmp["fp16-flat"].missed), flush=True)

    results = {"host": platform.node(), "device": torch.cuda.get_device_name(0), "torch": torch.__version__,
               "sleap_nn": sleap_nn.__version__, "args": vars(args), "sets": sets, "images": sets * len(args.videos),
               "reference_centroids": reference_total,
               "fp16_first_to_flat": MoveSummary(change), "fp16_flat_run_twice": MoveSummary(again),
               "modes": {m: c.summary() for m, c in cmp.items()}}
    from campy import strictjson
    with open(args.json, "w") as f:
        strictjson.dump(results, f, indent=1)
    print("{} sets ({} images), {} centroids in fp32".format(sets, results["images"], reference_total))
    for m, c in results["modes"].items():
        print("  {:12s} missed {:4d}  extra {:4d}  centroid shift p50/p99 {}/{} px  keypoint shift p50/p99 {}/{} px".format(
            m, c["centroids_missed"], c["centroids_extra"], *("-" if v is None else "{:.3f}".format(v) for v in (
                c["centroid_shift_px_p50"], c["centroid_shift_px_p99"], c["keypoint_shift_px_p50"], c["keypoint_shift_px_p99"]))))
    for name, v in (("fp16 first -> flat (this change)", results["fp16_first_to_flat"]),
                    ("fp16 flat run twice", results["fp16_flat_run_twice"])):
        print("{}: animals {} / {}, both {}; {} keypoints, moved {}, over 1 px {}, median {} max {} px".format(
            name, v["a"], v["b"], v["both"], v["keypoints"], v["keypoints_moved"], v["keypoints_moved_over_1px"],
            *("-" if x is None else "{:.3g}".format(x) for x in (v["keypoint_move_px_p50"], v["keypoint_move_px_max"]))))


if __name__ == "__main__":
    main()
