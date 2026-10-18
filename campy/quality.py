"""
campy-encode-quality: what each quality setting costs in file size and image quality.

Encodes the same clip at several quality settings, through the writer a
recording uses (writer.OpenWriter, so the same ffmpeg arguments), decodes each
result and reports per setting: the bitrate, GB per camera-hour, how fast one
stream encoded, and PSNR and SSIM against the clip.

The quality number means different things to different encoders. libx264 and
libx265 take it as a CRF, which adapts to the picture. NVENC, VA-API and AMF
take it as a constant quantizer, which spends bits on everything, sensor noise
included, so the same number can give files tens of times larger. Pick it per
encoder, here, before recording.

Pick it on real footage: pass --input with a short recording of the real scene
that is as close to lossless as you have (FFV1, or H.264 at -qp 0). A lossy
clip has already lost what you are trying to measure. Without --input the
sweep's synthetic frames are used and scored against the scene without its
sensor noise. They are smooth apart from that noise, so they show what the
noise costs and little else.

The video is measured in gray (luma): a mono camera exactly, a color camera
by its brightness only. Even a lossless setting scores about 57 dB rather
than infinity: yuv420p output stores brightness in video range (16-235), so
the 256 gray levels are rounded to 220. colorRange "full" keeps all 256.
A frame whose sides are not multiples of 16 (1000x750) is stretched to ones
that are (1008x752) by the writer, in a recording as here, and is scored
against the clip stretched the same way: lossless then scores a little lower.

Examples:
  campy-encode-quality --config configs/campy_config_flir_usb3.yaml --quality 18,21,24,27,30
  campy-encode-quality --config configs/campy_config_flir_usb3.yaml --input clip.mkv \\
      --encoder vaapi --encoder cpu:veryfast --quality 21,24,27
"""

import os, re, sys, json, time, shutil, argparse, tempfile, subprocess, contextlib

import numpy as np

from campy import writer, sweep


# ------------------------------------------------------------------ the clip

def InputFrames(path, start, seconds, ffmpeg):
	"""Gray frames from a video file, with its frame size and rate."""
	import imageio_ffmpeg
	reader = imageio_ffmpeg.read_frames(path, pix_fmt="gray", bits_per_pixel=8, # not the RGB default
		input_params=["-ss", str(start)], output_params=["-t", str(seconds)])
	meta = next(reader)
	width, height = meta["size"]
	frames = [np.frombuffer(b, np.uint8).reshape(height, width) for b in reader]
	if not frames:
		raise ValueError("no frames read from {} at {} s".format(path, start))
	return frames, width, height, float(meta["fps"])


def WriteRaw(path, frames):
	with open(path, "wb") as f:
		for fr in frames:
			f.write(np.ascontiguousarray(fr).tobytes())


# ------------------------------------------------------------------ scoring

class ScoreError(Exception):
	pass


def VideoSize(path):
	"""A video's frame size, as ffmpeg reads it; None if it cannot (Score then says why)."""
	import imageio_ffmpeg
	try:
		reader = imageio_ffmpeg.read_frames(path)
		try:
			return tuple(next(reader)["size"])
		finally:
			reader.close()
	except Exception:
		return None


def Score(ffmpeg, reference, video, width, height, fps, metric, video_size):
	"""
	Mean PSNR (dB) or SSIM of a video against raw gray reference frames of
	width x height. Raises ScoreError, with ffmpeg's reason, when it gives none.
	"""
	# ffmpeg's psnr and ssim need two inputs of one size. A video of another
	# size (video_size) was stretched by the writer (imageio-ffmpeg scales a
	# frame up to multiples of macro_block_size), so the reference is stretched
	# the same way: cropping the video would compare shifted pixels, and
	# shrinking it back would score the stretch along with the encoder.
	stretch, ref = "", "[0:v]"
	if video_size is not None and tuple(video_size) != (width, height):
		stretch, ref = "[0:v]scale={}:{}[r];".format(*video_size), "[r]"
	out = subprocess.run([ffmpeg, "-hide_banner", "-f", "rawvideo", "-pix_fmt", "gray",
		"-s", "{}x{}".format(width, height), "-r", str(fps), "-i", reference, "-i", video,
		"-lavfi", "{}[1:v]format=gray[d];{}[d]{}".format(stretch, ref, metric), "-f", "null", "-"],
		capture_output=True, text=True).stderr
	pattern = r"average:(\d+(?:\.\d+)?|inf)" if metric == "psnr" else r"All:(\d+(?:\.\d+)?)"
	m = re.search(pattern, out)
	if not m:
		raise ScoreError(FfmpegReason(out))
	return float(m.group(1))


def FfmpegReason(stderr):
	# ffmpeg's first complaint, without its "[name @ 0x...]" prefix
	lines = [line.strip() for line in stderr.splitlines() if line.strip()]
	said = [line for line in lines if line.startswith("[Parsed_") or "rror" in line] or lines[-1:]
	return re.sub(r"^\[[^\]]*\]\s*", "", said[0]) if said else "ffmpeg said nothing"


def Encode(params, frames):
	"""Encodes frames as a recording would. Returns (path, seconds taken)."""
	with contextlib.redirect_stdout(open(os.devnull, "w")): # OpenWriter's "Opened: ..." line
		ffmpeg, _, _ = writer.OpenWriter(params, [])
		t0 = time.perf_counter()
		for fr in frames:
			ffmpeg.send(fr)
		ffmpeg.close()
		took = time.perf_counter() - t0
	return os.path.join(params["videoFolder"], params["cameraName"], params["videoFilename"]), took


def Measure(base, encoders, qualities, frames, references, log=print):
	"""
	One row per encoder and quality. references maps a label ("input", or
	"clean" and "input" for synthetic frames) to a raw gray file of the frames.
	"""
	ffmpeg = FfmpegExe(base)
	w, h, fps = base["frameWidth"], base["frameHeight"], float(base["frameRate"])
	work = base["videoFolder"]
	rows, stretched = [], set()
	for spec in encoders:
		for q in qualities:
			params = sweep.EncoderParams(base, spec)
			params["quality"] = q
			params["cameraName"] = re.sub(r"\W", "_", "{}-{}".format(spec, q))
			path, took = Encode(params, frames)
			key = writer.EncoderArgs(params)["key"]
			mbps = os.path.getsize(path) * 8 / (len(frames) / fps) / 1e6
			row = {"encoder": key, "quality": q, "mbps": mbps, "gb_per_hour": mbps * 3600 / 8 / 1000,
				"encode_fps": len(frames) / took if took > 0 else float("inf")}
			size = VideoSize(path)
			if size not in (None, (w, h)) and key not in stretched:
				stretched.add(key)
				log("{} stretches {}x{} frames to {}x{}, as in a recording; scored against the clip "
					"stretched the same way.".format(key, w, h, *size))
			for label, ref in references.items():
				for metric in ("psnr", "ssim"):
					try:
						row[metric + "_" + label] = Score(ffmpeg, ref, path, w, h, fps, metric, size)
					except ScoreError as e:
						row[metric + "_" + label] = float("nan")
						log("{} at quality {}: no {} against the {} frames. ffmpeg: {}".format(
							key, q, metric.upper(), label, e))
			rows.append(row)
			log(FormatRow(row, list(references)))
			shutil.rmtree(os.path.join(work, params["cameraName"]), ignore_errors=True)
	return rows


def FfmpegExe(params):
	if str(params.get("ffmpegPath", "None")) != "None":
		return params["ffmpegPath"]
	import imageio_ffmpeg
	return imageio_ffmpeg.get_ffmpeg_exe()


# ------------------------------------------------------------------ output

def Header(labels):
	cols = "{:20} {:>7} {:>8} {:>10} {:>10}".format("encoder", "quality", "Mbit/s", "GB/cam-h", "enc fps")
	for label in labels:
		cols += " {:>12} {:>12}".format("PSNR " + label, "SSIM " + label)
	return cols


def FormatRow(row, labels):
	text = "{:20} {:>7} {:>8.2f} {:>10.2f} {:>10.0f}".format(row["encoder"], row["quality"], row["mbps"],
		row["gb_per_hour"], row["encode_fps"])
	for label in labels:
		text += " {:>9.2f} dB {:>12.4f}".format(row["psnr_" + label], row["ssim_" + label])
	return text


# ------------------------------------------------------------------ command line

def ParseArgs(argv=None):
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("--config", help="campy config .yaml: frame shape and encoder settings "
		"are taken from it (its first camera), then overridden by the options below.")
	parser.add_argument("--input", help="video to encode (default: synthetic frames); "
		"its size and frame rate replace the config's")
	parser.add_argument("--start", type=float, default=0, help="seconds into --input (default: 0)")
	parser.add_argument("--seconds", type=float, default=5, help="length of the clip (default: 5)")
	parser.add_argument("--quality", dest="qualities",
		help="settings to try, comma-separated (default: the config's, and 3 and 6 either side)")
	parser.add_argument("--encoder", action="append", dest="encoders",
		help="cpu[:preset], vaapi, nvidia[:preset], intel[:preset] or amd. Repeat to compare. "
		"Default: the config's encoder, else cpu.")
	parser.add_argument("--width", type=int, dest="frameWidth")
	parser.add_argument("--height", type=int, dest="frameHeight")
	parser.add_argument("--frameRate", type=float)
	parser.add_argument("--codec")
	parser.add_argument("--keyframeInterval", type=float)
	parser.add_argument("--ffmpegPath")
	parser.add_argument("--vaapiDevice")
	parser.add_argument("--noise", type=float, default=3.0, help="synthetic sensor noise, gray levels (default: 3)")
	parser.add_argument("--json", help="also write the results here")
	args = parser.parse_args(argv)
	# sweep.BaseParams reads these; quality is set per row instead.
	args.quality = args.pixelFormatInput = args.pixelFormatOutput = None
	return args


def Qualities(text, default):
	if text:
		return [int(q) for q in text.split(",")]
	q = int(default)
	return sorted({max(0, v) for v in (q - 6, q - 3, q, q + 3, q + 6)})


def Main(argv=None):
	args = ParseArgs(argv)
	base = sweep.BaseParams(args)
	base["pixelFormatInput"] = "gray"
	base["videoFilename"] = "quality.mp4"
	encoders = args.encoders or [sweep.DefaultEncoder(base)]
	qualities = Qualities(args.qualities, base["quality"])

	work = tempfile.mkdtemp(prefix="campy-quality-")
	base["videoFolder"] = work
	try:
		references = {}
		if args.input:
			frames, w, h, fps = InputFrames(args.input, args.start, args.seconds, FfmpegExe(base))
			base.update(frameWidth=w, frameHeight=h, frameRate=fps)
			references["input"] = os.path.join(work, "input.gray")
			WriteRaw(references["input"], frames)
			source = "{}, {:g} s from {:g} s".format(args.input, len(frames) / fps, args.start)
		else:
			w, h, fps = base["frameWidth"], base["frameHeight"], float(base["frameRate"])
			n = max(1, int(round(args.seconds * fps)))
			frames = sweep.SyntheticFrames(w, h, "gray", n, args.noise, seed=0)
			# Same seed, no noise: the same scene and motion without the sensor noise.
			references["clean"] = os.path.join(work, "clean.gray")
			WriteRaw(references["clean"], sweep.SyntheticFrames(w, h, "gray", n, 0.0, seed=0))
			references["input"] = os.path.join(work, "input.gray")
			WriteRaw(references["input"], frames)
			source = "synthetic frames, sensor noise {:g} gray levels".format(args.noise)

		print("{} frames of {}x{} at {:g} fps: {}".format(len(frames), w, h, fps, source))
		print(Header(list(references)))
		rows = Measure(base, encoders, qualities, frames, references)
	finally:
		shutil.rmtree(work, ignore_errors=True)

	print("\nenc fps is one stream on its own; campy-encode-sweep measures streams running together.")
	if args.json:
		with open(args.json, "w") as f:
			json.dump({"source": source, "width": w, "height": h, "fps": fps, "frames": len(frames),
				"rows": rows}, f, indent=1)
		print("Saved to {}.".format(args.json))


if __name__ == "__main__":
	Main()
