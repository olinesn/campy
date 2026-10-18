"""
campy-encode-sweep: how many cameras can each encoder keep up with here?

For each encoder and each camera count N, runs N encoders at once, one process
per stream as campy-acquire does, each fed synthetic camera frames through the
writer a recording uses (writer.OpenWriter) as fast as ffmpeg takes them. A
count passes when every stream sustains frameRate x headroom. The largest
passing count, per encoder and frame shape, goes into the capacity table that
campy-acquire's launch check reads (default ~/.campy/capacity.json).

The frames are a lit background with moving bright blobs and sensor noise.
Encoding speed depends on content (noise costs the most), so treat the
bitrate as a rough guide and re-check the speed with a real recording.
campy-encode-quality measures what each quality setting costs in file size
and image quality.

Examples:
  campy-encode-sweep --width 1280 --height 1024 --frameRate 60 --encoder cpu:veryfast --encoder vaapi
  campy-encode-sweep --config configs/campy_config_8cams_flir_usb3.yaml --streams 1,2,4,6,8,10,12
"""

import os, sys, json, time, queue, socket, shutil, platform, argparse, datetime, statistics, subprocess, tempfile
import multiprocessing as mp
import numpy as np

from campy import writer, configurator


ENCODER_MAKES = ("cpu", "vaapi", "nvidia", "intel", "amd")


# ------------------------------------------------------------------ frames

def SyntheticFrames(width, height, pix_fmt, count, noise=3.0, seed=0, clean=False):
	"""
	count frames of a lit scene: a smooth background with low-frequency
	texture, two bright blobs moving across it, and Gaussian sensor noise of
	standard deviation noise (in gray levels), fresh in every frame.
	clean True: (frames, the same frames without the noise), made in one pass.
	"""
	rng = np.random.default_rng(seed)
	y, x = np.mgrid[0:height, 0:width].astype(np.float32)
	phase = rng.uniform(0, 2 * np.pi, 4)
	background = (70 + 25 * x / width + 15 * y / height
		+ 8 * np.sin(x / 37.0 + phase[0]) * np.sin(y / 23.0 + phase[1]))
	frames, clean_frames = [], []
	for i in range(count):
		t = 2 * np.pi * i / count
		img = background.copy()
		for k, (ax, ay) in enumerate(((0.30, 0.25), (0.20, 0.35))):
			cx = width * (0.5 + ax * np.sin(t * (k + 1) + phase[2]))
			cy = height * (0.5 + ay * np.sin(t * (k + 2) + phase[3]))
			rx, ry = 0.06 * width, 0.04 * height
			img += 120 * np.exp(-(((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2))
		if clean: # the frame noise 0 gives; the noise is still drawn, so the noisy frames do not change
			clean_frames.append(ToPixelFormat(np.clip(img, 0, 255).astype(np.uint8), pix_fmt))
		img += rng.standard_normal((height, width), dtype=np.float32) * noise
		gray = np.clip(img, 0, 255).astype(np.uint8)
		frames.append(ToPixelFormat(gray, pix_fmt))
	return (frames, clean_frames) if clean else frames


def ToPixelFormat(gray, pix_fmt):
	if pix_fmt == "gray" or (pix_fmt.startswith("bayer_") and pix_fmt.endswith("8")):
		return gray
	if pix_fmt in ("rgb24", "bgr24"):
		return np.repeat(gray[:, :, None], 3, axis=2)
	if pix_fmt in ("rgb0", "bgr0", "rgba", "bgra"):
		return np.repeat(gray[:, :, None], 4, axis=2)
	if pix_fmt == "gray16le":
		return gray.astype(np.uint16) << 8
	raise ValueError("no synthetic frames for pixelFormatInput {}".format(pix_fmt))


# ------------------------------------------------------------------ one stream

def EncodeWorker(index, cam_params, settings, ready, go, results):
	# One stream, in its own process. Reports frames encoded after the warmup
	# and the time they took, plus the output size for a bitrate.
	sys.stdout = open(os.devnull, "w") # OpenWriter's per-stream messages
	try:
		frames = SyntheticFrames(cam_params["frameWidth"], cam_params["frameHeight"],
			cam_params["pixelFormatInput"], settings["frames"], settings["noise"], seed=index)
		cam_params["cameraName"] = "stream{}".format(index)
		ffmpeg, _, _ = writer.OpenWriter(cam_params, [])
	except Exception as e:
		ready.put((index, repr(e)))
		return
	ready.put((index, None))
	go.wait()

	sent, warm_frames = 0, None
	t0 = time.perf_counter()
	t_warm = t0 + settings["warmup"]
	t_end = t_warm + settings["seconds"]
	error = None
	try:
		while True:
			now = time.perf_counter()
			if warm_frames is None and now >= t_warm:
				warm_frames, t_warm = sent, now
			if now >= t_end:
				break
			ffmpeg.send(frames[sent % len(frames)])
			sent += 1
	except Exception as e:
		error = repr(e)
	t_stop = time.perf_counter()
	try:
		ffmpeg.close()
	except Exception as e:
		error = error or repr(e)

	path = os.path.join(cam_params["videoFolder"], cam_params["cameraName"], cam_params["videoFilename"])
	size = os.path.getsize(path) if os.path.exists(path) else 0
	measured = sent - (warm_frames or 0)
	results.put({
		"index": index,
		"error": error,
		"fps": measured / (t_stop - t_warm) if warm_frames is not None and t_stop > t_warm else 0.0,
		"bitrate_mbps": size * 8 / (sent / float(cam_params["frameRate"])) / 1e6 if sent else 0.0,
		})


# How long a stream may take to open its ffmpeg, and how often RunStreams
# looks for a stream that died while it waits for the others
READY_TIMEOUT = 300
POLL_SEC = 0.5


def Gather(messages, procs, index_of, timeout, before, late):
	"""
	One message from each stream's process, by stream index, and an error for
	each that sent none. A process that died without sending (a crash in
	ffmpeg's or numpy's native code, an OOM kill) never will: waiting out the
	whole timeout for it hung the sweep for 5 minutes, and queue.Empty then
	stopped it with nothing saved. So this stops at the first process found
	dead, or at the timeout. before, late: the error's words for a process
	that died ("before it was ready") and for one still running at the
	timeout ("was not ready after 300 s").
	"""
	got = {}
	deadline = time.monotonic() + timeout
	timed_out = False
	while len(got) < len(procs):
		try:
			message = messages.get(timeout=POLL_SEC)
			got[index_of(message)] = message
			continue
		except queue.Empty:
			pass
		if any(i not in got and not p.is_alive() for i, p in enumerate(procs)):
			# A process sends its message before it exits, so one that has just
			# exited may have left it after the last read
			while True:
				try:
					message = messages.get(timeout=POLL_SEC)
				except queue.Empty:
					break
				got[index_of(message)] = message
			break
		if time.monotonic() > deadline:
			timed_out = True
			break

	errors = []
	for i, p in enumerate(procs):
		if i in got:
			continue
		if not p.is_alive():
			code = p.exitcode
			how = "was killed (signal {})".format(-code) if code < 0 else "exited with code {}".format(code)
			errors.append("stream {}: {} {}".format(i, how, before))
		elif timed_out:
			errors.append("stream {}: {}".format(i, late))
	return got, errors


def RunStreams(cam_params, n, settings, out_dir, worker=None):
	"""
	Runs n streams at once. Returns one result dict per stream, the errors,
	and the computer's average CPU use while they ran (None without psutil,
	or when a stream failed before they started). worker: what each stream's
	process runs, EncodeWorker unless a test gives another.
	"""
	ctx = mp.get_context("spawn")
	ready, results, go = ctx.Queue(), ctx.Queue(), ctx.Event()
	params = dict(cam_params, videoFolder=out_dir)
	procs = [ctx.Process(target=worker or EncodeWorker, args=(i, params, settings, ready, go, results))
		for i in range(n)]
	for p in procs:
		p.start()
	opened, errors = Gather(ready, procs, lambda message: message[0], READY_TIMEOUT,
		"before it was ready", "was not ready after {} s".format(READY_TIMEOUT))
	errors += ["stream {}: {}".format(i, error) for i, (_, error) in sorted(opened.items()) if error]
	out, cpu = [], None
	if not errors:
		CpuPercent() # starts the CPU average here, once every stream is ready
		go.set()
		timeout = settings["warmup"] + settings["seconds"] + 120
		done, errors = Gather(results, procs, lambda result: result["index"], timeout,
			"before it sent its result", "sent no result after {:g} s".format(timeout))
		out = [done[i] for i in sorted(done)]
		cpu = CpuPercent()
	for p in procs:
		if errors:
			# Waiting for go, or encoding for a count that has already failed.
			# Its ffmpeg sees its input close, and exits.
			p.terminate()
		p.join(timeout=30)
		if p.is_alive():
			p.terminate()
	errors += ["stream {}: {}".format(r["index"], r["error"]) for r in out if r["error"]]
	return out, errors, cpu


# ------------------------------------------------------------------ host

def HostInfo(cam_params):
	import imageio_ffmpeg
	exe = imageio_ffmpeg.get_ffmpeg_exe()
	info = {
		"hostname": socket.gethostname(),
		"platform": platform.platform(),
		"cpu": platform.processor() or platform.machine(),
		"cores": os.cpu_count(),
		"ffmpeg": exe,
		}
	try:
		if sys.platform.startswith("linux"):
			with open("/proc/cpuinfo") as f:
				for line in f:
					if line.startswith("model name"):
						info["cpu"] = line.split(":", 1)[1].strip()
						break
		elif sys.platform == "darwin":
			info["cpu"] = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
				capture_output=True, text=True).stdout.strip()
		version = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=30)
		info["ffmpeg_version"] = version.stdout.splitlines()[0]
	except Exception:
		pass
	return info


# ------------------------------------------------------------------ sweep

def EncoderParams(base, spec):
	"""Camera parameters for an encoder spec: "cpu[:preset]", "vaapi", "nvidia[:preset]", ..."""
	make, _, preset = spec.partition(":")
	if make not in ENCODER_MAKES:
		raise ValueError("unknown encoder {}: use one of {}".format(spec, ", ".join(ENCODER_MAKES)))
	params = dict(base)
	if make == "cpu":
		params["gpuID"] = -1
	else:
		params["gpuID"] = 0 if base["gpuID"] == -1 else base["gpuID"]
		params["gpuMake"] = make
	if preset:
		params["preset"] = preset
	return params


def CpuPercent():
	try:
		import psutil
		return psutil.cpu_percent(interval=None)
	except ImportError:
		return None


def Sweep(cam_params, encoders, counts, settings, keep_going=False, log=print):
	"""Returns one capacity-table entry per encoder."""
	w, h, fps = cam_params["frameWidth"], cam_params["frameHeight"], float(cam_params["frameRate"])
	log("{}x{} {} at {:g} fps; each stream must reach {:g} fps (x{:g} headroom).".format(
		w, h, cam_params["pixelFormatInput"], fps, fps * settings["headroom"], settings["headroom"]))
	log("{:<22} {:>7} {:>8} {:>8} {:>9} {:>6} {:>7}  {}".format(
		"encoder", "streams", "fps min", "fps med", "Mpx/s", "cpu %", "Mbit/s", "result"))
	entries = []
	for spec in encoders:
		params = EncoderParams(cam_params, spec)
		enc = writer.EncoderArgs(params)
		rows, max_streams, failed = [], 0, False
		for n in counts:
			out_dir = tempfile.mkdtemp(prefix="campy-sweep-")
			try:
				results, errors, cpu = RunStreams(params, n, settings, out_dir)
			finally:
				shutil.rmtree(out_dir, ignore_errors=True)
			if errors:
				log("{:<22} {:>7}  error: {}".format(enc["key"], n, "; ".join(errors)))
				rows.append({"streams": n, "error": "; ".join(errors), "pass": False})
				break
			rates = [r["fps"] for r in results]
			ok = min(rates) >= fps * settings["headroom"]
			row = {
				"streams": n,
				"fps_min": round(min(rates), 1),
				"fps_median": round(statistics.median(rates), 1),
				"mpx_s": round(sum(rates) * w * h / 1e6, 1),
				"cpu_percent": cpu,
				"bitrate_mbps": round(statistics.median(r["bitrate_mbps"] for r in results), 2),
				"pass": ok,
				}
			rows.append(row)
			log("{:<22} {:>7} {:>8.1f} {:>8.1f} {:>9.1f} {:>6} {:>7.2f}  {}".format(
				enc["key"], n, row["fps_min"], row["fps_median"], row["mpx_s"],
				"-" if cpu is None else "{:.0f}".format(cpu), row["bitrate_mbps"],
				"pass" if ok else "too slow"))
			if ok and not failed:
				max_streams = n
			if not ok:
				failed = True
				if not keep_going:
					break
		passing = [r for r in rows if r.get("pass") and r["streams"] == max_streams]
		# An encoder that stopped with an error before it measured any count (one
		# this computer does not have, say) measured nothing: it is saved as an
		# error, not as keeping up with 0 streams
		error = rows[-1]["error"] if rows and all("error" in r for r in rows) else None
		entries.append({
			"key": enc["key"],
			"codec": enc["codec"],
			"width": w,
			"height": h,
			"fps": fps,
			"pix_fmt_in": cam_params["pixelFormatInput"],
			"pix_fmt_out": cam_params["pixelFormatOutput"],
			"quality": cam_params["quality"],
			"keyframeInterval": cam_params.get("keyframeInterval", "None"),
			"headroom": settings["headroom"],
			"max_streams": max_streams,
			"max_tested": max(r["streams"] for r in rows),
			"max_mpx_s": round(max_streams * w * h * fps / 1e6, 1),
			"bitrate_mbps": passing[0]["bitrate_mbps"] if passing else None,
			"error": error,
			"measured":datetime.datetime.now().isoformat(timespec="seconds"),
			"results": rows,
			})
	return entries


def SaveEntries(path, entries, host):
	# Merge into the capacity table: a new measurement replaces the one for the
	# same encoder and frame shape. An encoder that stopped with an error
	# replaces only an earlier error, never a measurement: an encoder that
	# failed to start once still keeps up with what it was measured to. A
	# table from another computer is replaced.
	table = None
	try:
		with open(path) as f:
			table = json.load(f)
	except (OSError, ValueError):
		pass
	if not table or table.get("host", {}).get("hostname") != host["hostname"]:
		table = {"version": 1, "entries": []}
	table["host"] = host

	def same(a, b):
		fields = ("key", "width", "height", "fps", "pix_fmt_in", "pix_fmt_out")
		return all(a[f] == b[f] for f in fields)

	measured = [e for e in table["entries"] if not e.get("error")]
	entries = [n for n in entries if not (n.get("error") and any(same(e, n) for e in measured))]
	kept = [e for e in table["entries"] if not any(same(e, n) for n in entries)]
	table["entries"] = kept + entries
	os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
	with open(path, "w") as f:
		json.dump(table, f, indent=1)
	return table


# ------------------------------------------------------------------ CLI

def ParseArgs(argv=None):
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("--config", help="campy config .yaml: frame shape and encoder settings "
		"are taken from it (its first camera), then overridden by the options below.")
	parser.add_argument("--width", type=int, dest="frameWidth")
	parser.add_argument("--height", type=int, dest="frameHeight")
	parser.add_argument("--frameRate", type=float)
	parser.add_argument("--pixelFormatInput", help="default: gray")
	parser.add_argument("--pixelFormatOutput", help="default: yuv420p")
	parser.add_argument("--codec")
	parser.add_argument("--quality", type=int)
	parser.add_argument("--keyframeInterval", type=float)
	parser.add_argument("--ffmpegPath")
	parser.add_argument("--vaapiDevice")
	parser.add_argument("--encoder", action="append", dest="encoders",
		help="cpu[:preset], vaapi, nvidia[:preset], intel[:preset] or amd. Repeat to compare. "
		"Default: the config's encoder, else cpu.")
	parser.add_argument("--streams", default="1,2,4,6,8",
		help="camera counts to try, in increasing order (default: 1,2,4,6,8)")
	parser.add_argument("--seconds", type=float, default=8, help="measured time per count (default: 8)")
	parser.add_argument("--warmup", type=float, default=2, help="unmeasured start (default: 2)")
	parser.add_argument("--headroom", type=float, default=1.25,
		help="a stream passes at frameRate x this (default: 1.25)")
	parser.add_argument("--frames", type=int, default=120, help="distinct test frames per stream (default: 120)")
	parser.add_argument("--noise", type=float, default=3.0, help="sensor noise, gray levels (default: 3)")
	parser.add_argument("--keep-going", action="store_true",
		help="try every count, not stopping at the first that is too slow")
	parser.add_argument("--out", help="capacity table to update (default: ~/.campy/capacity.json, "
		"or the config's capacityFile)")
	parser.add_argument("--no-save", action="store_true", help="print only")
	return parser.parse_args(argv)


def BaseParams(args):
	params = configurator.DefaultParams()
	params["pixelFormatInput"] = "gray"
	params["pixelFormatOutput"] = "yuv420p"
	if args.config:
		params.update(configurator.LoadConfig(args.config) or {})
		params = configurator.FillCameraLists(params)
		params = configurator.CameraParams(params, 0)
	for key in ("frameWidth", "frameHeight", "frameRate", "pixelFormatInput", "pixelFormatOutput",
			"codec", "quality", "keyframeInterval", "ffmpegPath", "vaapiDevice"):
		value = getattr(args, key)
		if value is not None:
			params[key] = value
	params["videoFilename"] = "sweep.mp4"
	params["ffmpegLogLevel"] = "error"
	if str(params.get("ffmpegPath", "None")) != "None":
		os.environ["IMAGEIO_FFMPEG_EXE"] = params["ffmpegPath"]
	return params


def DefaultEncoder(params):
	if params["gpuID"] == -1:
		return "cpu" if str(params["preset"]) == "None" else "cpu:{}".format(params["preset"])
	return params["gpuMake"]


def Main(argv=None):
	args = ParseArgs(argv)
	params = BaseParams(args)
	encoders = args.encoders or [DefaultEncoder(params)]
	counts = [int(n) for n in args.streams.split(",")]
	settings = {"seconds": args.seconds, "warmup": args.warmup, "headroom": args.headroom,
		"frames": args.frames, "noise": args.noise}

	entries = Sweep(params, encoders, counts, settings, keep_going=args.keep_going)

	print()
	for e in entries:
		if e["error"]:
			print("{}: stopped with an error (above), so nothing was measured.".format(e["key"]))
			continue
		more = "+" if e["max_streams"] >= e["max_tested"] else ""
		print("{}: keeps up with {}{} camera{} at {}x{} {:g} fps.".format(e["key"], e["max_streams"],
			more, "" if e["max_streams"] == 1 else "s", e["width"], e["height"], e["fps"]))
	if not args.no_save:
		from campy import preflight
		path = args.out or preflight.CapacityPath(params)
		table = SaveEntries(path, entries, HostInfo(params))
		for e in entries:
			if e not in table["entries"]:
				print("{}: kept its earlier measurement.".format(e["key"]))
		print("Saved to {}.".format(path))


if __name__ == "__main__":
	Main()
