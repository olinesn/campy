#!/usr/bin/env python3
"""Does campy's tie-tolerant peak finding (campy/peaks.py) find, in fp16, the centroids fp32 finds?

sleap-nn keeps a centroid only where one pixel of the centroid model's
confidence map is strictly greater than its 8 neighbours. In fp16 the two top
pixels sometimes round to the same value and the animal is lost (about 0.7%
of centroids, bench/compiled_bench.py, PR #127). campy-live now keeps such a
peak once (campy/peaks.py; --strict-peaks for sleap-nn's own).

This runs the trained top-down pair the way campy-live --model sleap does
(sleap-nn's TopDownLayer, fp16 autocast in its TorchBackend) on sets of
frames from the replay videos (--videos, one per camera), four ways:

  fp32-strict   fp32, sleap-nn's peaks: the reference
  fp32-tied     fp32, campy's peaks: should equal the reference (fp32 doesn't tie)
  fp16-strict   fp16, sleap-nn's peaks: campy-live before this change
  fp16-tied     fp16, campy's peaks: campy-live now

and compares each with the reference, frame by frame (each camera's image):
centroids matched to the nearest reference centroid within --match-px
full-frame pixels (missed: a reference centroid with none; extra: one with no
reference centroid), the centroid and keypoint shifts of matched animals, and,
between fp16-strict and fp16-tied, whether the animals both found have the
same keypoints: in images whose centroids are all the same they should be
identical; where fp16-tied found one more animal, the instance model ran on
one more crop, and fp16 results can differ with the batch. Recovered: the
reference centroids fp16-strict missed and fp16-tied found, with their
shifts from fp32.

Run it under the rig lock (rig.sh / rig.py) like every GPU job on the rigs.
Needs torch + sleap-nn + sleap_io, campy on the path (for campy.peaks).
"""

import argparse
import json
import platform
import sys
import time
import warnings

import numpy as np
import torch

MODES = ["fp32-strict", "fp32-tied", "fp16-strict", "fp16-tied"]


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
    from sleap_nn.inference.predictor import Predictor
    from campy import peaks
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        predictor = Predictor.from_model_paths([args.centroid, args.instance], device="cuda",
            max_instances=args.max_instances, peak_threshold=[args.centroid_threshold, args.peak_threshold])
    layer = predictor.layer
    backends = [layer.centroid_layer.backend, layer.centered_instance_layer.backend]

    def run(frames, mode):
        precision, how = mode.split("-")
        for backend in backends:
            backend.use_fp16 = precision == "fp16"
        peaks.UseTiedPeaks(how == "tied")
        with torch.inference_mode():
            out = layer.predict(frames)
        return {k: getattr(out, f).detach().float().cpu().numpy() for k, f in (("centroids", "pred_centroids"),
            ("keypoints", "pred_keypoints"))}

    cmp = {m: Compare(args.match_px) for m in MODES[1:]}
    reference_total, sets, t0 = 0, 0, time.perf_counter()
    recovered_cen, recovered_kp = [], [] # animals fp16-strict lost and fp16-tied found, against fp32
    # fp16-strict vs fp16-tied, animals both found at the same centroid: keypoints
    # in images whose centroids are all the same (the instance model got the
    # same crops), and in images where fp16-tied found one more (one crop more
    # in the instance model's batch: fp16 kernels may differ with the batch)
    same = {"same_crops": [0, 0, 0.0], "one_more_crop": [0, 0, 0.0]} # animals, keypoints differ, largest px
    for i, host in read_sets(args.videos, args.first, args.sets):
        frames = torch.from_numpy(host).to("cuda")
        r = {m: run(frames, m) for m in MODES}
        ref = r["fp32-strict"]
        reference_total += int((~np.isnan(ref["centroids"][..., 0])).sum())
        got = {m: cmp[m].add(i, ref, r[m]) for m in MODES[1:]}
        for b, (gs, gt) in enumerate(zip(got["fp16-strict"], got["fp16-tied"])):
            for k in set(gt) - set(gs):
                recovered_cen.append(gt[k][0])
                recovered_kp.extend(gt[k][1])
        s, t = r["fp16-strict"], r["fp16-tied"]
        for b in range(s["centroids"].shape[0]):
            sc, tc = s["centroids"][b], t["centroids"][b]
            ns, nt = int((~np.isnan(sc[:, 0])).sum()), int((~np.isnan(tc[:, 0])).sum())
            group = "same_crops" if np.array_equal(sc, tc, equal_nan=True) else "one_more_crop" if nt == ns + 1 else None
            for a in range(sc.shape[0]):
                if group is None or np.isnan(sc[a, 0]):
                    continue
                hit = np.flatnonzero((tc == sc[a]).all(-1))
                if len(hit):
                    d = np.abs(np.nan_to_num(s["keypoints"][b, a], nan=-1e9) - np.nan_to_num(t["keypoints"][b, hit[0]], nan=-1e9))
                    same[group][0] += 1
                    same[group][1] += int(d.max() > 0)
                    same[group][2] = max(same[group][2], float(d.max()))
        sets += 1
        if sets % 500 == 0:
            print("{} sets, {:.0f} s: fp16 strict missed {}, tied missed {}".format(
                sets, time.perf_counter() - t0, cmp["fp16-strict"].missed, cmp["fp16-tied"].missed), flush=True)
    peaks.UseTiedPeaks(False)

    def pct(a, q):
        return float(np.percentile(a, q)) if len(a) else None
    results = {"host": platform.node(), "device": torch.cuda.get_device_name(0), "torch": torch.__version__,
               "sleap_nn": sleap_nn.__version__, "args": vars(args), "sets": sets, "images": sets * len(args.videos),
               "reference_centroids": reference_total, "recovered": len(recovered_cen),
               "recovered_centroid_shift_px_p50": pct(recovered_cen, 50),
               "recovered_centroid_shift_px_max": max(recovered_cen) if recovered_cen else None,
               "recovered_keypoint_shift_px_p50": pct(recovered_kp, 50),
               "recovered_keypoint_shift_px_p99": pct(recovered_kp, 99),
               "fp16_strict_vs_tied": {g: {"animals": v[0], "keypoints_differ": v[1], "largest_px": v[2]}
                                       for g, v in same.items()},
               "modes": {m: c.summary() for m, c in cmp.items()}}
    with open(args.json, "w") as f:
        json.dump(results, f, indent=1)
    print("{} sets ({} images), {} centroids in fp32 (sleap-nn's peaks)".format(sets, results["images"], reference_total))
    for m, c in results["modes"].items():
        print("  {:12s} missed {:4d}  extra {:4d}  centroid shift p50/p99 {}/{} px  keypoint shift p50/p99 {}/{} px".format(
            m, c["centroids_missed"], c["centroids_extra"], *("-" if v is None else "{:.3f}".format(v) for v in (
                c["centroid_shift_px_p50"], c["centroid_shift_px_p99"], c["keypoint_shift_px_p50"], c["keypoint_shift_px_p99"]))))
    print("recovered (fp16-strict lost, fp16-tied found): {}, centroid shift from fp32 p50 {} max {} px, "
          "keypoint shift p50/p99 {}/{} px".format(results["recovered"], *("-" if v is None else "{:.2f}".format(v) for v in (
              results["recovered_centroid_shift_px_p50"], results["recovered_centroid_shift_px_max"],
              results["recovered_keypoint_shift_px_p50"], results["recovered_keypoint_shift_px_p99"]))))
    for g, v in results["fp16_strict_vs_tied"].items():
        print("fp16 strict vs tied, {}: {} animals, keypoints differ in {} (largest {:g} px)".format(
            g.replace("_", " "), v["animals"], v["keypoints_differ"], v["largest_px"]))

if __name__ == "__main__":
    main()
