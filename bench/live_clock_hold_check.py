#!/usr/bin/env python3
"""Does campy-live's GPU clock hold slow any sets down, and when?

campy-live's --hold-gpu-clock-blocks auto changes how many GPU blocks spin
while sets come (GpuClock.Adjust). A count too few lets an NVIDIA GPU's
clock fall, and every set is slower until a block is put back. This runs
campy-live as usual (nothing in campy changes) and logs, beside its own
rows, the GPU's clock and power every --sample-ms and every block change,
on campy's host clock, so slow sets can be lined up with them.

    python bench/live_clock_hold_check.py replay <recording folder> --name <feed> --fps 60 --seconds 720
    python bench/live_clock_hold_check.py run --log <prefix> --clock-memory <json> [--forget] -- <campy-live arguments>
    python bench/live_clock_hold_check.py report <prefix> [<prefix> ...]
    python bench/live_clock_hold_check.py same <a>.npy <b>.npy

replay   plays a campy recording's videos (Camera*/ *.mp4) into a live feed,
         the same frame number on every camera, at --fps, for --seconds,
         from the start again at each video's end (frame numbers go on), so
         a run can be longer than the recording.
run      campy-live with the clock logged: <prefix>.clock.npz (samples:
         host_ns, SM MHz, power W, blocks spinning; looks: each of auto's
         clock reads while sets come, with the blocks before and after it)
         and <prefix>.clock.json. --clock-memory is the gpu_clock.json the
         hold remembers each GPU's fewest blocks in (its own, so a test
         never writes the rig's); --forget empties it first, as after a
         restart. Give campy-live --out <prefix> so report finds its rows.
report   per run: sets, skipped, ms per set (p50 / p99 / p99.9 / max),
         frame to 3D, and the slow windows: --min-sets or more sets slower
         than --slow times the median, each less than --gap s from the last, each with its start,
         length, slowest set, lowest clock and the block changes around it.
same     the 2D results (centroids, keypoints, their confidences) of the
         sets with the same frame number in two runs, compared exactly.

Run it under the rig lock. Needs nvidia-ml-py (pynvml) for the samples.
"""
import argparse, glob, os, sys, threading, time, atexit

import numpy as np

from campy import strictjson
from campy.clock import HostNs


def Replay(a):
	import cv2
	from campy import livefeed
	cams = sorted(d for d in glob.glob(os.path.join(a.folder, "Camera*")) if os.path.isdir(d))
	paths = [sorted(glob.glob(os.path.join(d, "*.mp4")))[0] for d in cams]
	names = [os.path.basename(d) for d in cams]
	caps = [cv2.VideoCapture(p) for p in paths]
	def Next():
		imgs = []
		for i, cap in enumerate(caps):
			ok, img = cap.read()
			if not ok: # the video's end: from its start again
				cap.release()
				caps[i] = cap = cv2.VideoCapture(paths[i])
				ok, img = cap.read()
				if not ok:
					sys.exit("cannot read " + paths[i])
			imgs.append(img[:, :, 0] if img.ndim == 3 else img)
		return imgs
	imgs = Next()
	h, w = imgs[0].shape[:2]
	writers = [livefeed.RingWriter(livefeed.RingName(a.name, i), (h, w, 1), np.uint8, frameRate=a.fps, camera=i,
		cameraName=n) for i, n in enumerate(names)]
	print("replaying {} cameras ({}), {}x{}, at {} fps for {} s, into {}-*, looping".format(len(caps),
		", ".join(names), w, h, a.fps, a.seconds, a.name), flush=True)
	n_frames, period = int(a.seconds * a.fps), 1.0 / a.fps
	t0, late, n = time.perf_counter(), 0, 0
	try:
		for n in range(1, n_frames + 1):
			due = t0 + (n - 1) * period
			while time.perf_counter() < due:
				time.sleep(0.0005)
			if time.perf_counter() - due > period:
				late += 1
			host = HostNs()
			for writer, img in zip(writers, imgs):
				writer.Write(img, n, host, (n - 1) * period)
			imgs = Next()
	except KeyboardInterrupt:
		pass
	took = time.perf_counter() - t0
	print("wrote {} sets in {:.1f} s ({:.2f} fps), {} more than a frame late".format(n, took, n / took, late),
		flush=True)
	for writer in writers:
		writer.Close()


class Log:
	def __init__(self):
		self.samples = [] # host_ns, MHz, W, blocks spinning, busy
		self.looks = [] # host_ns, MHz, limited, busy before, busy after, fewest after
		self.clock = None
		self.saved = False


def Run(a, rest):
	from campy import live
	log = Log()
	if a.forget and os.path.exists(a.clock_memory):
		os.remove(a.clock_memory)
	base = live.GpuClock

	class Logged(base):
		def __init__(self, device, blocks="auto", reader=None, fewest=None, memory=None):
			super().__init__(device, blocks, reader=reader, fewest=fewest, memory=a.clock_memory)
			log.clock = self

		def Adjust(self, mhz, limited, *rest):
			before = self.busy
			out = super().Adjust(mhz, limited, *rest)
			log.looks.append((HostNs(), mhz, int(bool(limited)), before, self.busy, self.fewest))
			return out

		def Start(self):
			super().Start()
			threading.Thread(target=Sample, args=(self,), daemon=True, name="clock check sampler").start()

	def Sample(clock):
		import torch
		uuid = live.ClockReader.Uuid(torch, clock.device)
		try:
			import pynvml
			pynvml.nvmlInit()
			handle = pynvml.nvmlDeviceGetHandleByUUID(uuid)
		except Exception as e:
			# no nvidia-ml-py: nvidia-smi, one process for the run, a line every sample_ms
			import shutil, subprocess
			smi = shutil.which("nvidia-smi")
			if not smi:
				print("clock check: no NVML ({}) and no nvidia-smi, no samples".format(e), flush=True)
				return
			print("clock check: no NVML ({}): samples from nvidia-smi".format(e), flush=True)
			p = subprocess.Popen([smi, "--id=" + uuid, "--query-gpu=clocks.sm,power.draw", "--format=csv,noheader,nounits",
				"-lms", str(int(a.sample_ms))], stdout=subprocess.PIPE, text=True)
			threading.Thread(target=lambda: (clock.stop.wait(), p.terminate()), daemon=True).start()
			for line in p.stdout:
				try:
					mhz, watts = (float(v) for v in line.split(","))
				except ValueError:
					continue
				log.samples.append((HostNs(), mhz, watts, clock.Spinning(), clock.busy))
			return
		period = a.sample_ms / 1000.0
		while not clock.stop.wait(period):
			try:
				mhz = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM)
				watts = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0
			except pynvml.NVMLError:
				continue
			log.samples.append((HostNs(), mhz, watts, clock.Spinning(), clock.busy))

	live.GpuClock = Logged
	atexit.register(Save, a, log)
	try:
		live.Main(rest)
	finally:
		Save(a, log)


def Save(a, log):
	if log.saved:
		return
	log.saved = True
	samples = np.array(log.samples, np.float64).reshape(-1, 5)
	looks = np.array(log.looks, np.float64).reshape(-1, 6)
	np.savez(a.log + ".clock.npz", samples=samples, looks=looks)
	c = log.clock
	d = {"sample_ms": a.sample_ms, "clock_memory": a.clock_memory, "forgot": a.forget, "samples": len(samples),
		"looks": len(looks), "hold": c.Describe() if c is not None else None}
	with open(a.log + ".clock.json", "w") as f:
		strictjson.dump(d, f, indent=1)


def Pct(v, q):
	return round(float(np.percentile(v, q)), 3) if len(v) else None


def Windows(t, ms, slow, gap):
	# runs of slow sets (ms > slow), each less than gap s from the last
	idx = np.flatnonzero(ms > slow)
	out = []
	for i in idx:
		if out and t[i] - t[out[-1][-1]] < gap:
			out[-1].append(i)
		else:
			out.append([i])
	return out


def Report(a):
	rows = []
	for prefix in a.prefixes:
		r = np.load(prefix + ".npy")
		r = r[r["inference_ns"] > 0]
		t = r["read_ns"] / 1e9
		t0 = t[0]
		ms = r["inference_ns"] / 1e6
		host = r["host_ns"].astype(np.float64)
		host[host <= 0] = np.nan
		last = np.nanmax(host, axis=1)
		to3d = (r["pose3d_ns"] - last) / 1e6 if "pose3d_ns" in r.dtype.names else None
		to3d = to3d[np.isfinite(to3d) & (r["pose3d_ns"] > 0)] if to3d is not None else None
		med = float(np.median(ms))
		w = Windows(t, ms, a.slow * med, a.gap)
		lone = sum(len(x) for x in w if len(x) < a.min_sets)
		w = [x for x in w if len(x) >= a.min_sets]
		rows_extra = {}
		cl = np.load(prefix + ".clock.npz") if os.path.exists(prefix + ".clock.npz") else None
		js = strictjson.load(open(prefix + ".clock.json")) if os.path.exists(prefix + ".clock.json") else {}
		print("== {}: {} sets over {:.0f} s, {} skipped, first frame {}".format(os.path.basename(prefix), len(r),
			t[-1] - t0, int(r["skipped"].sum()), int(r["frame"][0])))
		print("   ms per set p50 {} / p99 {} / p99.9 {} / max {}".format(Pct(ms, 50), Pct(ms, 99), Pct(ms, 99.9),
			round(float(ms.max()), 3)))
		after = t - t0 >= a.skip_first
		print("   after the first {:g} s: p99 {} / max {}; outside the slow windows p99 {}".format(a.skip_first,
			Pct(ms[after], 99), round(float(ms[after].max()), 3),
			Pct(np.delete(ms, [i for x in w for i in x]), 99)))
		if to3d is not None and len(to3d):
			print("   frame to 3D p50 {} / p99 {} / max {} ms".format(Pct(to3d, 50), Pct(to3d, 99),
				round(float(to3d.max()), 3)))
			if a.loop:
				# the replay's first frames after it opens its videos again come late to campy-live
				ok = (r["pose3d_ns"] > 0) & np.isfinite(last) & ((r["frame"] - 1) % a.loop >= 10)
				inner = (r["pose3d_ns"][ok] - last[ok]) / 1e6
				print("   frame to 3D, the replay's first 10 frames of each loop left out: p50 {} / p99 {} / max {} "
					"ms".format(Pct(inner, 50), Pct(inner, 99), round(float(inner.max()), 3)))
				rows_extra = {"to3d_inner_p99": Pct(inner, 99), "to3d_inner_max": round(float(inner.max()), 3)}
		if cl is not None and len(cl["samples"]):
			s = cl["samples"]
			fed = (s[:, 0] / 1e9 >= t0) & (s[:, 0] / 1e9 <= t[-1])
			s = s[fed]
			if len(s):
				share = {int(b): round(float(np.mean(s[:, 4] == b)), 3) for b in np.unique(s[:, 4])}
				print("   clock p50 {:.0f} / min {:.0f} MHz, power mean {:.1f} W, blocks share {}".format(
					np.median(s[:, 1]), s[:, 1].min(), s[:, 2].mean(), share))
		hold = js.get("hold") or {}
		print("   hold: {}".format({k: hold.get(k) for k in ("fewest_blocks", "fewest_remembered", "fewest_learned",
			"block_changes", "blocks_share") if k in hold}))
		lengths = []
		print("   {} slow window(s) (at least {} sets over {:.2f} ms = {:g} x the median, gaps under {:g} s), and {} "
			"slow sets apart from them:".format(len(w), a.min_sets, a.slow * med, a.slow, a.gap, lone))
		for x in w:
			start, end = t[x[0]], t[x[-1]]
			lengths.append(end - start)
			extra = ""
			if cl is not None:
				lk = cl["looks"]
				near = lk[(lk[:, 0] / 1e9 >= start - a.before) & (lk[:, 0] / 1e9 <= end + 0.5) & (lk[:, 3] != lk[:, 4])]
				ch = ", ".join("{} -> {} at {:+.1f} s".format(int(c[3]), int(c[4]), c[0] / 1e9 - start) for c in near)
				s = cl["samples"]
				inw = s[(s[:, 0] / 1e9 >= start) & (s[:, 0] / 1e9 <= end + 0.05)]
				if not len(inw): # no samples: auto's own looks
					inw = lk[(lk[:, 0] / 1e9 >= start) & (lk[:, 0] / 1e9 <= end + 0.5)]
				extra = "; clock min {}".format("{:.0f} MHz".format(inw[:, 1].min()) if len(inw) else "-")
				extra += "; block changes {}".format(ch or "none within {:g} s before".format(a.before))
			print("     at {:7.1f} s for {:5.2f} s: {:4d} slow sets, slowest {:.2f} ms{}".format(start - t0,
				end - start, len(x), ms[x].max(), extra))
		rows.append({"run": os.path.basename(prefix), "sets": len(r), "skipped": int(r["skipped"].sum()),
			"p50": Pct(ms, 50), "p99": Pct(ms, 99), "p999": Pct(ms, 99.9), "max": round(float(ms.max()), 3),
			"to3d_p50": Pct(to3d, 50) if to3d is not None else None,
			"to3d_p99": Pct(to3d, 99) if to3d is not None else None,
			"to3d_max": round(float(to3d.max()), 3) if to3d is not None and len(to3d) else None,
			"slow_windows": len(w), "slow_s": round(float(sum(lengths)), 2),
			"slow_sets_in_windows": int(sum(len(x) for x in w)), "lone_slow_sets": lone, **rows_extra})
	if a.json:
		with open(a.json, "w") as f:
			strictjson.dump(rows, f, indent=1)


def Same(a):
	x, y = np.load(a.a), np.load(a.b)
	x, y = x[x["inference_ns"] > 0], y[y["inference_ns"] > 0]
	common, ix, iy = np.intersect1d(x["frame"], y["frame"], return_indices=True)
	print("{} sets in both ({} and {} in each)".format(len(common), len(x), len(y)))
	ok = True
	for field in ("found", "centroids", "centroid_vals", "keypoints", "peak_vals"):
		u, v = x[field][ix], y[field][iy]
		same = np.array([np.array_equal(p, q, equal_nan=True) for p, q in zip(u, v)])
		diff = np.nanmax(np.abs(u.astype(np.float64) - v.astype(np.float64))) if not same.all() else 0.0
		print("  {:<14} identical in {} of {} sets{}".format(field, int(same.sum()), len(same),
			"" if same.all() else ", largest difference {:g}".format(diff)))
		ok &= bool(same.all())
	sys.exit(0 if ok else 1)


def Main():
	p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
	sub = p.add_subparsers(dest="cmd", required=True)
	r = sub.add_parser("replay")
	r.add_argument("folder")
	r.add_argument("--name", required=True)
	r.add_argument("--fps", type=float, default=60.0)
	r.add_argument("--seconds", type=float, default=720.0)
	u = sub.add_parser("run")
	u.add_argument("--log", required=True)
	u.add_argument("--clock-memory", required=True)
	u.add_argument("--forget", action="store_true")
	u.add_argument("--sample-ms", type=float, default=100.0)
	o = sub.add_parser("report")
	o.add_argument("prefixes", nargs="+")
	o.add_argument("--slow", type=float, default=1.1)
	o.add_argument("--gap", type=float, default=1.0)
	o.add_argument("--min-sets", type=int, default=5, help="fewer slow sets than this together are not a window")
	o.add_argument("--before", type=float, default=15.0, help="block changes this many s before a window are listed")
	o.add_argument("--skip-first", type=float, default=2.0)
	o.add_argument("--loop", type=int, default=0, help="the replay's frames per loop (rec5: 3600): its first 10 "
		"frames of each loop are left out of a second frame-to-3D line")
	o.add_argument("--json", default="")
	s = sub.add_parser("same")
	s.add_argument("a")
	s.add_argument("b")
	argv = sys.argv[1:]
	rest = []
	if "--" in argv:
		i = argv.index("--")
		argv, rest = argv[:i], argv[i + 1:]
	a = p.parse_args(argv)
	{"replay": Replay, "run": lambda a: Run(a, rest), "report": Report, "same": Same}[a.cmd](a)


if __name__ == "__main__":
	Main()
