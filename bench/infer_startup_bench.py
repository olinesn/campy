"""
How long campy-infer takes to get ready, step by step, before its first
frame: Python itself, the imports (numpy, PyTorch, sleap-nn, TensorRT,
PyNvVideoCodec), the GPU's CUDA context, loading the two models, making the
networks' engines (built, or read from --engine-cache) and opening the
video's decoder with its first batch. docs/offline-inference.md, "Start-up".

	CAMPY_T0=$(date +%s.%N) python bench/infer_startup_bench.py --video <video> \\
		--centroid <model folder> --instance <model folder> --max-instances 5 \\
		[--engine-cache <folder>] [--json out.json]

CAMPY_T0 (the shell's clock just before python starts) lets the first step
count Python's own start-up and the site-packages it reads. Each step is
timed alone, in the order campy-infer does them, so a step's time includes
the libraries it is the first to load (the CUDA context loads the CUDA
runtime, the first prediction cuDNN's kernels).
"""

import argparse, json, os, sys, time

T0 = float(os.environ.get("CAMPY_T0") or 0) or None
START = time.time()


def Main():
	p = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0])
	p.add_argument("--video", required=True)
	p.add_argument("--centroid", required=True)
	p.add_argument("--instance", required=True)
	p.add_argument("--max-instances", type=int, default=5)
	p.add_argument("--engine-cache", default="")
	p.add_argument("--batch", type=int, default=16)
	p.add_argument("--decoder", default="auto")
	p.add_argument("--json", default="")
	p.add_argument("--label", default="")
	p.add_argument("--read-ahead", default="off", choices=["on", "off"], help="as campy-infer's --read-ahead (the "
		"list read from and written to --engine-cache's prefetch folder)")
	a = p.parse_args()
	steps = []
	if T0:
		steps.append(("python start", START - T0))
	t = [time.perf_counter()]
	from campy import prefetch
	cache = os.path.expanduser(a.engine_cache or os.path.join("~", ".cache", "campy", "engines"))
	reading = prefetch.Start(cache, prefetch.ModelFiles([a.centroid, a.instance])) if a.read_ahead == "on" else None

	def Step(name):
		now = time.perf_counter()
		steps.append((name, now - t[0]))
		print("{:<28s} {:7.2f} s".format(name, now - t[0]), flush=True)
		t[0] = now
	if T0:
		print("{:<28s} {:7.2f} s".format("python start", START - T0), flush=True)
	import numpy # noqa: F401
	from campy import infer, videoread, live
	Step("numpy + campy")
	import torch
	Step("import torch")
	torch.cuda.init()
	torch.zeros(1, device="cuda")
	torch.cuda.synchronize()
	Step("CUDA context")
	import sleap_nn.inference.predictor # noqa: F401
	Step("import sleap-nn")
	import tensorrt # noqa: F401
	import onnx # noqa: F401
	Step("import tensorrt + onnx")
	try:
		import PyNvVideoCodec # noqa: F401
	except ImportError:
		pass
	Step("import PyNvVideoCodec")
	args = infer.Parser().parse_args([a.video, "--centroid", a.centroid, "--instance", a.instance, "--max-instances",
		str(a.max_instances), "--batch", str(a.batch), "--engine-cache", a.engine_cache, "--decoder", a.decoder])
	model = infer.Model(args)
	Step("load the models")
	info = videoread.VideoInfo(a.video)
	model.Prepare(info.height, info.width)
	torch.cuda.synchronize()
	Step("engines ready")
	reader, note = videoread.Open(a.video, args.batch, args.decoder, args.hwaccel, 0, info)
	it = iter(reader)
	start, frames = next(it)
	Step("decoder + first batch")
	model.Run(start, frames)
	torch.cuda.synchronize()
	Step("first batch predicted")
	total = sum(s for _, s in steps)
	ahead = None
	if reading:
		n, size, seconds = reading.Done()
		ahead = {"files": n, "listed": len(reading.files), "bytes": size, "seconds": seconds}
		print("read ahead {:,} of {:,} files, {:.2f} GB, {}".format(n, len(reading.files), size / 1e9,
			"in {:.1f} s".format(seconds) if seconds is not None else "still reading"), flush=True)
	if a.read_ahead == "on":
		print("list:", prefetch.Record(cache, list(live.FixedEngine.PLANS) + [videoread.FfmpegExe()]), flush=True)
	print("{:<28s} {:7.2f} s  ({})".format("total", total, model.Describe()), flush=True)
	if a.json:
		with open(a.json, "w") as f:
			json.dump({"label": a.label, "host": os.uname().nodename, "python": sys.executable,
				"gpu": torch.cuda.get_device_name(0), "steps": steps, "total": total, "how": model.Describe(),
				"notes": model.notes, "read_ahead": ahead}, f, indent=1)
	os._exit(0) # the reader's threads and processes: not timed


if __name__ == "__main__":
	Main()
