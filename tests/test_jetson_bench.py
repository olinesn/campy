"""bench/jetson_encode_bench.py and bench/jetson_setup_check.py, for the Jetson AGX Orin rig.

No Jetson and no GStreamer needed: the commands are checked as argument
lists, the CPU fallback runs for real with imageio-ffmpeg's ffmpeg, and the
GStreamer path runs with a stand-in gst-launch-1.0 (a script that encodes
with ffmpeg's libx265 and writes the H.265 stream where the real pipeline
would), so the pipes, the muxer and the report are exercised end to end.
"""

import json, os, stat, subprocess, sys, textwrap

import pytest
import imageio_ffmpeg

from campy import configurator, writer

BENCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench")
sys.path.insert(0, BENCH)
import jetson_encode_bench as enc  # noqa: E402
import jetson_setup_check as chk  # noqa: E402

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def has_encoder(name):
	out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
	return name in out


def after(argv, word):
	return argv[argv.index(word) + 1]


# ------------------------------------------------------------------ commands

def test_gst_pipeline_feeds_gray_frames_to_the_hardware_encoder_at_a_fixed_qp():
	argv = enc.gst_args(1280, 1024, 60, 21, 60, 7)
	assert argv[:3] == ["gst-launch-1.0", "-q", "-e"]
	# one word per argument, elements joined by "!"; gray frames go straight to nvvidconv, which makes
	# I420 in the encoder's memory (NV12 clipped the levels to 16-235 on JetPack 7.2.1)
	assert argv[argv.index("fdsrc"):argv.index("fdsrc") + 4] == ["fdsrc", "fd=0", "blocksize=1310720", "!"]
	for word in ("format=gray8", "width=1280", "height=1024", "framerate=60/1", "plane-strides=<1280>"):
		assert word in argv
	assert "videoconvert" not in argv and not any("NV12" in a for a in argv)
	order = [argv.index(e) for e in ("rawvideoparse", "nvvidconv", "video/x-raw(memory:NVMM),format=I420",
		"nvv4l2h265enc", "h265parse", "fdsink")]
	assert order == sorted(order)
	# constant QP through qp-range, rate control on with a ceiling it never reaches, no peak-bitrate
	for prop in ("control-rate=variable_bitrate", "bitrate=4000000000", "qp-range=21,21:21,21:21,21",
			"preset-level=1", "iframeinterval=60", "idrinterval=60", "insert-sps-pps=1"):
		assert prop in argv
	# the properties JetPack 7.2.1 ignores are gone
	assert not any(a.startswith(("ratecontrol-enable", "quant-", "peak-bitrate", "maxperf")) for a in argv)
	assert "fd=7" in argv


def test_gst_pipeline_is_campys_jetson_writer():
	# what writer.JetsonEncoderArgs gives an H.265 camera at the same settings is what the bench runs,
	# from fdsrc to the parser, and the muxer reads the stream and marks it as the writer's does
	params = configurator.DefaultParams()
	params.update(cameraName="Camera0", frameWidth=1280, frameHeight=1024, frameRate=60, pixelFormatInput="gray",
		pixelFormatOutput="yuv420p", codec="h265", gpuID=0, gpuMake="jetson", quality=21, preset="ultrafast",
		keyframeInterval=0.5, denoise="None")
	w = writer.JetsonEncoderArgs(params)
	argv = enc.gst_args(1280, 1024, 60, 21, enc.keyframe_frames(60, 0.5), 7)
	assert argv[1:argv.index("fdsink")] == w["gst"]
	assert enc.MAX_BITRATE == writer.JETSON_MAX_BITRATE
	mux = enc.ffmpeg_mux_args(60, "/data/s0.mp4", ffmpeg="ff")
	assert mux[mux.index("-f"):mux.index("-i")] == w["mux_input"]
	assert mux[mux.index("-c"):mux.index("-c") + 4] == w["output_params"][:4]


def test_gst_inputs():
	# gray8-convert: GStreamer's videoconvert makes I420 on the CPU before nvvidconv
	conv = enc.gst_args(1280, 1024, 60, 21, 60, 7, input_format="gray8-convert")
	assert "blocksize=1310720" in conv and "format=gray8" in conv
	assert conv.index("rawvideoparse") < conv.index("videoconvert") < conv.index("video/x-raw,format=I420") < conv.index("nvvidconv")
	assert "video/x-raw(memory:NVMM),format=I420" in conv
	for gone in ("i420", "nv12"):
		with pytest.raises(ValueError):
			enc.gst_args(1280, 1024, 60, 21, 60, 7, input_format=gone)
	assert enc.parse_args([]).input == "gray8"
	with pytest.raises(SystemExit):
		enc.parse_args(["--input", "i420"])
	with pytest.raises(SystemExit):
		enc.parse_args(["--qp", "52"])


def test_gst_optional_properties_and_raw_file_output():
	argv = enc.gst_args(640, 480, 30, 30, 15, "/data/x.h265", preset_level=3, maxperf=True)
	assert "preset-level=3" in argv and "maxperf-enable=1" in argv and "qp-range=30,30:30,30:30,30" in argv
	assert argv[-3:] == ["filesink", "location=/data/x.h265", "sync=false"]
	assert not any(a.startswith("preset-level") for a in enc.gst_args(640, 480, 30, 30, 15, 7, preset_level=None))


def test_ramp_frames_and_levels():
	frames = enc.make_frames(320, 240, 2)
	assert len(frames[0]) == enc.frame_bytes(320, 240) == 320 * 240
	top = frames[0][:320]
	assert top[0] == 0 and top[-1] == 255 and list(top) == sorted(top)
	assert enc.ramp_levels(frames[0], 320, 240) == (0.0, 255.0)
	# a frame squeezed to limited range reads about 16 and 235
	import numpy as np
	squeezed = (16 + np.frombuffer(frames[0], np.uint8).astype(np.float64) * 219 / 255).round().astype(np.uint8)
	assert enc.ramp_levels(squeezed.tobytes(), 320, 240) == (16.0, 235.0)
	assert enc.ramp_levels(b"short", 320, 240) is None
	argv = enc.first_frame_args("ff", "/d/s0.mp4", 320, 240, 60, raw_luma=True)
	assert after(argv, "-vf") == "extractplanes=y" and after(argv, "-frames:v") == "1" and argv[-1] == "-"
	assert after(enc.first_frame_args("ff", "/d/s0.h265", 320, 240, 60, raw_luma=False), "-f") == "hevc"


def test_keyframe_interval_is_seconds_turned_into_frames():
	assert enc.keyframe_frames(60, 1.0) == 60
	assert enc.keyframe_frames(60, 0.5) == 30
	assert enc.keyframe_frames(60, 0.001) == 1


def test_muxer_uses_campys_fragmented_mp4_flags_and_hvc1():
	params = configurator.DefaultParams()
	params.update(cameraName="Camera0", frameWidth=320, frameHeight=240, pixelFormatInput="gray",
		pixelFormatOutput="yuv420p", codec="h265", gpuID=0, gpuMake="nvidia")
	out = writer.EncoderArgs(params)["output_params"]
	# what writer.py gives a fragmented H.265 MP4 is what the bench gives its muxer
	assert out[:len(enc.FRAGMENTED_MP4)] == enc.FRAGMENTED_MP4
	assert out[len(enc.FRAGMENTED_MP4):len(enc.FRAGMENTED_MP4) + 2] == enc.HVC1
	argv = enc.ffmpeg_mux_args(60, "/data/s0.mp4", ffmpeg="ff")
	assert argv[0] == "ff" and argv[-1] == "/data/s0.mp4"
	assert after(argv, "-f") == "hevc" and after(argv, "-c") == "copy"
	# -r on the input: the stream has no frame times, and -framerate alone gave times that stopped advancing
	assert argv.index("-framerate") < argv.index("-r") < argv.index("-i")
	assert after(argv, "-framerate") == "60" and after(argv, "-r") == "60"
	assert after(argv, "-bsf:v") == "hevc_metadata=video_full_range_flag=1"
	assert enc.FRAGMENTED_MP4[1] in argv and after(argv, "-tag:v") == "hvc1"


def test_cpu_fallback_commands():
	x264 = enc.ffmpeg_cpu_args("libx264", 1280, 1024, 60, 21, 60, "fast", "o.mp4")
	assert after(x264, "-pix_fmt") == "gray" and after(x264, "-s") == "1280x1024"
	assert after(x264, "-c:v") == "libx264" and after(x264, "-qp") == "21" and after(x264, "-g") == "60"
	assert after(x264, "-bf") == "0" and after(x264, "-color_range") == "pc"
	assert "-tag:v" not in x264
	x265 = enc.ffmpeg_cpu_args("libx265", 1280, 1024, 60, 21, 60, "ultrafast", "o.mp4")
	assert "qp=21:keyint=60:open-gop=0" in after(x265, "-x265-params")
	assert after(x265, "-tag:v") == "hvc1"
	with pytest.raises(ValueError):
		enc.ffmpeg_cpu_args("hevc_nvenc", 1, 1, 1, 1, 1, "fast", "o.mp4")


def test_stream_commands_follow_the_options(tmp_path):
	args = enc.parse_args([])
	assert (args.streams, args.fps, args.seconds, args.qp, args.mux) == (3, 60, 30.0, 21, "ffmpeg")
	e, m, out = enc.stream_commands(args, 1, str(tmp_path), gst_fd=9)
	assert out.endswith("stream1.mp4") and "fd=9" in e and m[-1] == out
	e, m, out = enc.stream_commands(enc.parse_args(["--mux", "raw"]), 0, str(tmp_path))
	assert m is None and out.endswith(".h265") and "location=" + out in e
	e, m, out = enc.stream_commands(enc.parse_args(["--cpu-fallback", "libx265"]), 2, str(tmp_path))
	assert m is None and e[0] == "ffmpeg" and out.endswith("stream2_libx265.mp4")


def test_dry_run_prints_the_commands(capsys):
	assert enc.main(["--dry-run", "--streams", "2"]) == 0
	text = capsys.readouterr().out
	assert text.count("encoder: gst-launch-1.0") == 2 and text.count("muxer:   ffmpeg") == 2


def test_property_check_reads_gst_inspect():
	inspect = textwrap.dedent("""\
		Element Properties:
		  name                : The name of the object
		  iframeinterval      : Encoding Intra Frame occurance frequency
		  quant-i-frames      : Quantization parameter for I-frames
		""")
	assert enc.missing_properties(inspect, ["iframeinterval=60", "quant-i-frames=21", "maxperf-enable=1"]) == ["maxperf-enable"]


def test_psnr_parse():
	assert enc.parse_psnr("[Parsed_psnr_2 @ 0x1] PSNR y:41.75 average:41.752 min:40 max:43") == 41.752
	assert enc.parse_psnr("PSNR y:inf average:inf min:inf max:inf") == float("inf")
	assert enc.parse_psnr("nothing") is None
	assert enc.finite({"a": [float("inf"), 1.0]}) == {"a": [None, 1.0]}


# ------------------------------------------------------------------ running it

@pytest.mark.skipif(not has_encoder("libx264"), reason="this ffmpeg has no libx264")
@pytest.mark.skipif(sys.platform == "win32", reason="the bench runs on Linux (os.wait4, resource)")
def test_cpu_fallback_runs(tmp_path):
	out = tmp_path / "r.json"
	code = enc.main(["--cpu-fallback", "libx264", "--cpu-preset", "ultrafast", "--streams", "2", "--fps", "20",
		"--seconds", "1", "--width", "320", "--height", "240", "--pattern-frames", "8", "--psnr-frames", "10",
		"--ffmpeg", FFMPEG, "--out-dir", str(tmp_path / "v"), "--keep", "--json", str(out)])
	assert code == 0
	data = json.loads(out.read_text())
	assert len(data["results"]) == 2
	for r in data["results"]:
		assert r["frames_written"] == 20 and r["frames_in_file"] == 20
		assert r["encoder_exit"] == 0 and r["bytes"] > 0 and r["encoder_cpu_pct"] is not None
		assert r["achieved_fps"] == pytest.approx(20, rel=0.2)
	r0 = data["results"][0]
	assert r0["psnr_db_stored_luma"] > 30
	# full range on the CPU too: the ramp comes back 0-255
	for key in ("ramp_levels_stored_luma", "ramp_levels_by_range"):
		low, high = r0[key]
		assert low <= 3 and high >= 252, (key, low, high)


FAKE_GST = """\
#!{python}
# stands in for gst-launch-1.0: reads the same gray frames and writes H.265
# where the pipeline would (fdsink fd=N or filesink location=...), keeping the
# levels 0-255 as nvvidconv's gray to I420 does
import os, subprocess, sys
words = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
w, h, fps = words["width"], words["height"], words["framerate"].split("/")[0]
assert words["format"] == "gray8" and words["plane-strides"] == "<" + w + ">", words
cmd = [{ffmpeg!r}, "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "gray",
	"-s", w + "x" + h, "-framerate", fps, "-i", "pipe:0",
	"-vf", "scale=out_range=full", "-pix_fmt", "yuv420p", "-color_range", "pc",
	"-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error", "-f", "hevc"]
if "fd" in words and "fdsink" in sys.argv:
	sys.exit(subprocess.call(cmd + ["pipe:1"], stdout=int(words["fd"])))
sys.exit(subprocess.call(cmd + ["-y", words["location"]]))
"""


@pytest.mark.skipif(not has_encoder("libx265"), reason="this ffmpeg has no libx265")
@pytest.mark.skipif(sys.platform == "win32", reason="the bench runs on Linux; the stand-in is a #! script")
@pytest.mark.parametrize("mux,input_format", [("ffmpeg", "gray8"), ("raw", "gray8"), ("ffmpeg", "gray8-convert")])
def test_gstreamer_path_with_a_stand_in(tmp_path, mux, input_format):
	gst = tmp_path / "gst-launch-1.0"
	gst.write_text(FAKE_GST.format(python=sys.executable, ffmpeg=FFMPEG))
	gst.chmod(gst.stat().st_mode | stat.S_IEXEC)
	out = tmp_path / "r.json"
	code = enc.main(["--gst-launch", str(gst), "--no-property-check", "--mux", mux, "--input", input_format, "--streams", "2",
		"--fps", "20", "--seconds", "1", "--width", "320", "--height", "240", "--pattern-frames", "8",
		"--psnr-frames", "10", "--ffmpeg", FFMPEG, "--out-dir", str(tmp_path / "v"), "--keep", "--json", str(out)])
	data = json.loads(out.read_text())
	assert code == 0, data["results"]
	for r in data["results"]:
		assert r["frames_written"] == 20 and r["frames_in_file"] == 20 and r["encoder_exit"] == 0
		if mux == "ffmpeg":
			assert r["output"].endswith(".mp4") and r["muxer_exit"] == 0 and r["muxer_cpu_pct"] is not None
		else:
			assert r["output"].endswith(".h265") and r["muxer_exit"] is None
	r0 = data["results"][0]
	assert r0["psnr_db_stored_luma"] > 30
	# the ramp comes back 0-255, stored and read by the video's range
	for key in ("ramp_levels_stored_luma", "ramp_levels_by_range"):
		low, high = r0[key]
		assert low <= 3 and high >= 252, (key, low, high)


# ------------------------------------------------------------------ setup check

def test_setup_check_parsers():
	assert chk.parse_tegra_release("# R39 (release), REVISION: 2.1, GCID: 1, BOARD: generic, EABI: aarch64") == (39, 2, 1)
	assert chk.parse_tegra_release("# R36 (release), REVISION: 4, GCID: 1") == (36, 4)
	assert chk.parse_tegra_release("junk") is None
	assert chk.parse_nvpmodel("NV Power Mode: MAXN\n0\n") == ("MAXN", 0)
	assert chk.parse_nvpmodel("NV Power Mode: MODE_30W\n2\n") == ("MODE_30W", 2)
	assert chk.kernel_config_value("CONFIG_USB_ACM=m\nCONFIG_USB=y\n", "USB_ACM") == "m"
	assert chk.kernel_config_value("# CONFIG_USB_ACM is not set\n", "USB_ACM") is None
	lsusb = ("Bus 002 Device 003: ID 1e10:4000 Point Grey Research, Inc. Blackfly S\n"
		"Bus 002 Device 004: ID 1E10:4000 FLIR\nBus 001 Device 002: ID 2341:0070 Arduino Nano ESP32\n")
	assert chk.count_usb_vendor(lsusb, "1e10") == 2
	assert chk.clocks_locked([("cpu0", 2201600, 2201600), ("gpu", 1300500000, 1300500000)]) == (True, [])
	assert chk.clocks_locked([("cpu0", 115200, 2201600), ("gpu", 1, 1)]) == (False, ["cpu0"])
	assert chk.clocks_locked([]) == (False, [])


def test_setup_check_video_disk_says_what_to_do(tmp_path):
	# no such folder: a disk of its own, or --video-dir; on the system disk: a disk of its own, nothing
	# about mounting it at the folder given (on our Orin that folder was the home folder)
	rep = chk.Report()
	chk.check_disk(rep, str(tmp_path / "data"), 200)
	(item,) = rep.items
	assert item["status"] == chk.FAIL and item["detail"] == "no " + str(tmp_path / "data")
	assert "USB 3 drive" in item["fix"] and "--video-dir" in item["fix"]
	rep = chk.Report()
	chk.check_disk(rep, "/", 0)
	disk = rep.items[0]
	assert disk["status"] == chk.FAIL and disk["detail"].endswith("but on the system disk")
	assert "USB 3 drive" in disk["fix"] and "eMMC" not in disk["detail"] and "mounted at" not in disk["fix"]


def test_setup_check_runs_anywhere_and_changes_nothing(tmp_path):
	# off a Jetson every Jetson item fails, with a fix, and nothing is written but the JSON asked for
	before = sorted(os.listdir(tmp_path))
	out = tmp_path / "check.json"
	p = subprocess.run([sys.executable, os.path.join(BENCH, "jetson_setup_check.py"), "--no-cameras",
		"--campy-venv", str(tmp_path / "none"), "--sleap-venv", str(tmp_path / "none"),
		"--video-dir", str(tmp_path), "--json", str(out)], capture_output=True, text=True, timeout=120)
	assert sorted(os.listdir(tmp_path)) == sorted(before + ["check.json"])
	items = json.loads(out.read_text())["items"]
	if not os.path.exists("/etc/nv_tegra_release"):
		assert p.returncode == 1
		assert items[0]["status"] == "FAIL" and items[0]["fix"]
	assert all(it["fix"] for it in items if it["status"] in ("WARN", "FAIL"))
	assert "fix:" in p.stdout
