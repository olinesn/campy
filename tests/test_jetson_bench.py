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

def test_gst_pipeline_feeds_gray8_to_the_hardware_encoder_at_a_fixed_qp():
	argv = enc.gst_args(1280, 1024, 60, 21, 60, 7)
	assert argv[:3] == ["gst-launch-1.0", "-q", "-e"]
	# one word per argument, elements joined by "!"
	assert argv[argv.index("fdsrc"):argv.index("fdsrc") + 4] == ["fdsrc", "fd=0", "blocksize=1310720", "!"]
	assert "format=gray8" in argv and "width=1280" in argv and "height=1024" in argv and "framerate=60/1" in argv
	order = [argv.index(e) for e in ("rawvideoparse", "nvvidconv", "video/x-raw(memory:NVMM),format=NV12",
		"nvv4l2h265enc", "h265parse", "fdsink")]
	assert order == sorted(order)
	for prop in ("ratecontrol-enable=0", "quant-i-frames=21", "quant-p-frames=21", "iframeinterval=60",
			"idrinterval=60", "insert-sps-pps=1", "num-B-Frames=0"):
		assert prop in argv
	assert "fd=7" in argv
	assert not any(a.startswith(("preset-level", "maxperf")) for a in argv)


def test_gst_optional_properties_and_raw_file_output():
	argv = enc.gst_args(640, 480, 30, 30, 15, "/data/x.h265", preset_level=1, maxperf=True)
	assert "preset-level=1" in argv and "maxperf-enable=1" in argv
	assert argv[-3:] == ["filesink", "location=/data/x.h265", "sync=false"]


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
	assert after(argv, "-f") == "hevc" and after(argv, "-c") == "copy" and after(argv, "-framerate") == "60"
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
	assert data["results"][0]["psnr_db_stored_luma"] > 30


FAKE_GST = """\
#!{python}
# stands in for gst-launch-1.0: reads the same raw frames and writes H.265
# where the pipeline would (fdsink fd=N or filesink location=...)
import os, subprocess, sys
words = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
w, h, fps = words["width"], words["height"], words["framerate"].split("/")[0]
cmd = [{ffmpeg!r}, "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "gray",
	"-s", w + "x" + h, "-framerate", fps, "-i", "pipe:0", "-c:v", "libx265", "-preset", "ultrafast",
	"-x265-params", "log-level=error", "-f", "hevc"]
if "fd" in words and "fdsink" in sys.argv:
	sys.exit(subprocess.call(cmd + ["pipe:1"], stdout=int(words["fd"])))
sys.exit(subprocess.call(cmd + ["-y", words["location"]]))
"""


@pytest.mark.skipif(not has_encoder("libx265"), reason="this ffmpeg has no libx265")
@pytest.mark.skipif(sys.platform == "win32", reason="the bench runs on Linux; the stand-in is a #! script")
@pytest.mark.parametrize("mux", ["ffmpeg", "raw"])
def test_gstreamer_path_with_a_stand_in(tmp_path, mux):
	gst = tmp_path / "gst-launch-1.0"
	gst.write_text(FAKE_GST.format(python=sys.executable, ffmpeg=FFMPEG))
	gst.chmod(gst.stat().st_mode | stat.S_IEXEC)
	out = tmp_path / "r.json"
	code = enc.main(["--gst-launch", str(gst), "--no-property-check", "--mux", mux, "--streams", "2",
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
	assert data["results"][0]["psnr_db_stored_luma"] > 30


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
