"""
gpuMake "jetson": an NVIDIA Jetson's hardware encoder, through GStreamer
(writer.JetsonEncoderArgs, GstPipeline, GstWriteFrames). No Jetson here: a
stand-in gst-launch-1.0 (FAKE_GST below) reads the gray frames the way the
real pipeline does and encodes them with ffmpeg's libx264, losslessly, into
an H.264 stream marked limited range, as the Orin's encoder marks its own,
on the file descriptor fdsink names. Everything after that is campy's own:
the pipe between the two processes, the muxing ffmpeg and its full-range
mark, the writer's errors, restarts and the launch check.
"""

import os, re, sys, subprocess, textwrap

import imageio_ffmpeg
import numpy as np
import pytest

from campy import configurator, configschema, preflight, startup, sweep, writer

from test_failures import CameraProcess, FakeCamera, params
from test_ffmpeg_restart import Killer, Levels, Near, ReadSegments


posix = pytest.mark.skipif(os.name == "nt", reason="gpuMake jetson runs on Linux (pass_fds)")

FRAGMENTED = ["-movflags", "+frag_keyframe+empty_moov+delay_moov+default_base_moof",
	"-frag_duration", "1000000", "-flush_packets", "1"]


def cam(**overrides):
	p = configurator.DefaultParams()
	p.update(cameraName="Camera0", frameWidth=1280, frameHeight=1024, frameRate=60, pixelFormatInput="gray",
		pixelFormatOutput="yuv420p", gpuID=0, gpuMake="jetson", codec="h265", quality=21, videoFilename="0.mp4")
	p.update(overrides)
	return p


# ------------------------------------------------------------------ settings

def test_h265_pipeline():
	enc = writer.EncoderArgs(cam(keyframeInterval=0.5))
	assert enc["codec"] == "nvv4l2h265enc"
	assert enc["key"] == "nvv4l2h265enc/ultrafast"
	assert enc["device"] == "gpu"
	assert enc["gst"] == ["-q", "-e",
		"fdsrc", "fd=0", "blocksize=1310720", "!",
		"rawvideoparse", "format=gray8", "width=1280", "height=1024", "framerate=60/1", "plane-strides=<1280>", "!",
		"nvvidconv", "!", "video/x-raw(memory:NVMM),format=I420", "!",
		"nvv4l2h265enc", "control-rate=variable_bitrate", "bitrate=4000000000",
		"qp-range=21,21:21,21:21,21", "preset-level=1", "insert-sps-pps=1", "iframeinterval=30", "idrinterval=30",
		"!", "h265parse", "!"]
	assert enc["mux_input"] == ["-f", "hevc", "-framerate", "60", "-r", "60"]
	# A copy, marked full range, in campy's fragmented MP4, tagged hvc1
	assert enc["output_params"] == ["-c", "copy", "-bsf:v", "hevc_metadata=video_full_range_flag=1"] \
		+ FRAGMENTED + ["-tag:v", "hvc1"]


def test_h264_preset_and_frame_rate():
	enc = writer.EncoderArgs(cam(codec="h264", preset="medium", quality=30, frameRate=59.94, videoFilename="0.mkv"))
	assert enc["key"] == "nvv4l2h264enc/medium"
	gst = enc["gst"]
	assert gst[gst.index("nvv4l2h264enc"):] == ["nvv4l2h264enc", "control-rate=variable_bitrate",
		"bitrate=4000000000", "qp-range=30,30:30,30:30,30", "preset-level=3",
		"insert-sps-pps=1", "!", "h264parse", "!"] # keyframeInterval None: the encoder's own
	assert "framerate=2997/50" in gst
	assert enc["mux_input"] == ["-f", "h264", "-framerate", "2997/50", "-r", "2997/50"]
	assert enc["output_params"] == ["-c", "copy", "-bsf:v", "h264_metadata=video_full_range_flag=1"] # .mkv: no MP4 options


def test_the_cpu_path_is_unchanged():
	# gpuID -1 is the CPU whatever gpuMake says
	assert writer.EncoderArgs(cam(gpuID=-1))["codec"] == "libx265"


@pytest.mark.parametrize("change, words", [
	(dict(codec="av1"), 'codec is "h264" or "h265", not "av1"'),
	(dict(pixelFormatInput="rgb24"), 'takes gray frames only'),
	(dict(pixelFormatInput="bayer_rggb8"), 'takes gray frames only'),
	(dict(denoise="atadenoise"), 'denoise "atadenoise" is an ffmpeg filter'),
	(dict(frameWidth=1281), 'both sides must be even'),
	(dict(quality=88), 'quality is the QP, 0-51, not 88'),
	(dict(preset="veryfast"), 'preset is one of ultrafast, fast, medium, slow, not "veryfast"'),
	])
def test_settings_it_cannot_take(change, words):
	with pytest.raises(ValueError, match=re.escape(words)):
		writer.EncoderArgs(cam(**change))


def test_the_config_and_the_sweep_know_it():
	assert "jetson" in configschema.BY_NAME["gpuMake"].choices
	params = sweep.EncoderParams(dict(cam(), gpuID=-1), "jetson:fast")
	assert (params["gpuID"], params["gpuMake"], params["preset"]) == (0, "jetson", "fast")
	assert writer.EncoderArgs(params)["key"] == "nvv4l2h265enc/fast"


# ------------------------------------------------------------------ the stand-in encoder

FAKE_GST = textwrap.dedent('''
	# Stands in for gst-launch-1.0 with campy's Jetson pipeline: reads gray frames on stdin (fdsrc fd=0) and
	# writes the encoded stream to fdsink's fd. FAKE_GST_FAIL: says what gst-launch-1.0 says when the encoder
	# cannot open, after that many frames, and exits 1.
	import os, subprocess, sys
	args = sys.argv[1:]
	value = lambda name: [a.split("=", 1)[1] for a in args if a.startswith(name + "=")]
	width, height, rate = value("width")[0], value("height")[0], value("framerate")[0]
	out = int(value("fd")[-1]) # fdsink's; fdsrc's is fd=0
	print("Opening in BLOCKING MODE ") # NVIDIA's, on stdout
	sys.stderr.write("NvMMLiteOpen : Block : BlockType = 8 \\n")
	sys.stderr.flush()
	fail = os.environ.get("FAKE_GST_FAIL")
	if fail is not None:
		sys.stdin.buffer.read(int(fail) * int(width) * int(height))
		sys.stderr.write("ERROR: from element /GstPipeline:pipeline0/nvv4l2h265enc:nvv4l2h265enc0: Could not open "
			"device '/dev/v4l2-nvenc' for reading and writing.\\n")
		sys.exit(1)
	# Lossless H.264 of the gray levels as they are (full range), marked limited range as the Orin's encoder does
	code = subprocess.call([os.environ["FAKE_FFMPEG"], "-v", "error", "-f", "rawvideo", "-pix_fmt", "gray",
		"-s", width + "x" + height, "-framerate", rate, "-i", "pipe:0", "-vf", "scale=out_range=full",
		"-pix_fmt", "yuv420p", "-c:v", "libx264", "-qp", "0", "-bf", "0", "-g", "30",
		"-bsf:v", "h264_metadata=video_full_range_flag=0", "-f", "h264", "pipe:1"], stdout=out)
	sys.exit(code)
	''')


@pytest.fixture
def fake_gst(tmp_path, monkeypatch):
	path = tmp_path / "fake_gst_launch.py"
	path.write_text(FAKE_GST)
	monkeypatch.setattr(writer, "GST_LAUNCH", [sys.executable, str(path)])
	monkeypatch.setenv("FAKE_FFMPEG", imageio_ffmpeg.get_ffmpeg_exe())
	monkeypatch.delenv("FAKE_GST_FAIL", raising=False)
	return path


def jetson_params(tmp_path, **values):
	return params(tmp_path, gpuID=0, gpuMake="jetson", codec="h264", **values)


def Probe(path):
	return subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
		capture_output=True, text=True).stderr


def GrayFrames(path, width, height):
	# The video's frames, decoded to gray as they are stored (no range conversion: the luma plane)
	raw = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-i", str(path), "-f", "rawvideo",
		"-pix_fmt", "yuv420p", "pipe:1"], capture_output=True, check=True).stdout
	size = width * height * 3 // 2
	return [np.frombuffer(raw[i:i + width * height], np.uint8).reshape(height, width)
		for i in range(0, len(raw), size)]


@posix
def test_frames_go_through_both_processes_into_a_full_range_mp4(tmp_path, fake_gst, capsys):
	p = cam(codec="h264", frameWidth=64, frameHeight=48, frameRate=30, videoFolder=str(tmp_path),
		ffmpegLogLevel="warning")
	w, _, _ = writer.OpenWriter(p, [], new=True)
	pipeline = writer.FfmpegProcess(w)
	assert isinstance(pipeline, writer.GstPipeline) # what ExitWatch and the restarts look after
	rng = np.random.default_rng(0)
	frames = [rng.integers(0, 256, (48, 64), dtype=np.uint8) for _ in range(75)]
	frames[0][:] = 0
	frames[1][:] = 255
	for frame in frames:
		w.send(frame)
	w.close()
	assert pipeline.returncode == 0 and pipeline.gst.returncode == 0 and pipeline.mux.returncode == 0
	out = capsys.readouterr()
	assert "using the Jetson's video encoder (nvv4l2h264enc, through GStreamer)" in out.out
	assert "NvMMLiteOpen" not in out.err and "BLOCKING" not in out.out # NVIDIA's chatter is not said

	video = os.path.join(str(tmp_path), "Camera0", "0.mp4")
	info = Probe(video)
	assert re.search(r"Video: h264 .*yuvj?420p\(pc", info), info # marked full range, as the frames are
	assert "Duration: 00:00:02.50" in info # 75 frames at 30 fps: each frame's time from the frame rate
	got = GrayFrames(video, 64, 48)
	assert len(got) == len(frames) # every frame, in order, each gray level as it was sent
	assert all(np.array_equal(a, b) for a, b in zip(got, frames))


@posix
def test_an_encoder_that_will_not_open_is_said(tmp_path, fake_gst, monkeypatch):
	monkeypatch.setenv("FAKE_GST_FAIL", "2")
	p = cam(codec="h264", frameWidth=64, frameHeight=48, videoFolder=str(tmp_path))
	w, _, _ = writer.OpenWriter(p, [], new=True)
	pipeline = writer.FfmpegProcess(w)
	with pytest.raises(IOError) as error:
		for _ in range(500):
			w.send(np.zeros((48, 64), np.uint8))
	first = str(error.value).splitlines()[0] # what campy says of it (FfmpegStopped)
	assert "gst-launch-1.0 said: ERROR: from element /GstPipeline:pipeline0/nvv4l2h265enc:nvv4l2h265enc0: Could " \
		"not open device '/dev/v4l2-nvenc'" in first
	pipeline.wait(10)
	assert pipeline.returncode == 1


@posix
def test_a_recording_holds_every_frame(tmp_path, fake_gst, monkeypatch, capsys):
	run = CameraProcess(monkeypatch, jetson_params(tmp_path, recTimeInSec=2), FakeCamera())
	run.Run(timeout=40)
	assert run.recording.Failures() == []
	meta = run.Metadata()
	assert meta["totalFrames"] == "200" and meta["framesWritten"] == "200" and "videoError" not in meta
	levels = Levels(os.path.join(run.Folder(), "0.mp4"))
	assert len(levels) == 200 and all(Near(level, n) for n, level in zip(range(1, 201), levels))


@posix
def test_a_killed_pipeline_is_restarted(tmp_path, fake_gst, monkeypatch, capsys):
	# restartFfmpeg with the Jetson's two processes: killed (both), a new pair goes on in 0.part2.mp4
	ffmpeg_process, on_frame, found = Killer({150})
	monkeypatch.setattr(writer, "FfmpegProcess", ffmpeg_process)
	run = CameraProcess(monkeypatch, jetson_params(tmp_path, recTimeInSec=4, restartFfmpeg=True,
		keyframeInterval=0.5), FakeCamera(on_frame=on_frame))
	run.Run(timeout=60)
	assert len(found) == 2 and found[1].returncode == 0
	assert run.recording.Failures() == [] and run.recording.restarts == 1
	first, second = ReadSegments(run.Folder())
	assert second["last_frame"] == "400" and second["lost"] == "0"
	assert int(first["frames"]) + int(first["lost"]) == int(first["last_frame"])
	held = int(first["frames"]) + int(second["frames"])
	assert run.Metadata()["framesWritten"] == str(held)
	levels = Levels(os.path.join(run.Folder(), "0.mp4")) + Levels(os.path.join(run.Folder(), "0.part2.mp4"))
	assert len(levels) == held


# ------------------------------------------------------------------ launch check

@posix
def test_the_probe_encodes_test_frames(tmp_path, fake_gst, monkeypatch):
	p = cam(codec="h264", frameWidth=64, frameHeight=48)
	assert writer.JetsonProbe(p, str(tmp_path)) is None
	assert os.path.getsize(tmp_path / "jetson_test.mp4") > 0
	monkeypatch.setenv("FAKE_GST_FAIL", "1")
	said = writer.JetsonProbe(p, str(tmp_path))
	assert said.startswith("gst-launch-1.0 exited with exit code 1") and "Could not open device '/dev/v4l2-nvenc'" in said


def test_the_probe_without_gstreamer(tmp_path, monkeypatch):
	monkeypatch.setattr(writer, "GST_LAUNCH", [str(tmp_path / "no-such-gst-launch-1.0")])
	said = writer.JetsonProbe(cam(codec="h264", frameWidth=64, frameHeight=48), str(tmp_path))
	assert "is GStreamer installed (on a Jetson, nvidia-l4t-gstreamer)?" in said


def test_launch_check(monkeypatch):
	cams = [cam(cameraName="Camera{}".format(i)) for i in range(3)] + [cam(cameraName="Camera3", codec="av1"),
		cam(cameraName="Camera4", gpuID=-1)]
	tried = []

	def probe(c, folder):
		tried.append(c["cameraName"])
		return None
	report = preflight.Report()
	kept = preflight.CheckJetson(cams, report, probe=probe)
	assert tried == ["Camera0"] # one probe for the three with the same settings
	assert [c["cameraName"] for c in kept] == ["Camera0", "Camera1", "Camera2", "Camera4"]
	lines = [(status, text) for status, _, text in report.lines]
	assert (preflight.OK, "nvv4l2h265enc/ultrafast (Camera0, Camera1, Camera2) encoded 10 test frames with campy's "
		"settings, through GStreamer") in lines
	assert (preflight.FAIL, 'Camera3: gpuMake "jetson": codec is "h264" or "h265", not "av1" (the Orin\'s AV1 '
		'encoder ignores the quality setting).') in lines

	report = preflight.Report()
	preflight.CheckJetson(cams[:1], report, probe=lambda c, folder: "wrote no video")
	[(status, _, text)] = report.lines
	assert status == preflight.FAIL and text.startswith("nvv4l2h265enc/ultrafast (Camera0) could not encode test "
		"frames with campy's settings: wrote no video. ")
	assert "To run it by hand: gst-launch-1.0 -q -e fdsrc fd=0 " in text and "| " in text

	# The ffmpeg encoder check leaves Jetson cameras to this one
	report = preflight.Report()
	preflight.CheckEncoders(cams[:3], report, exe="ffmpeg", encoders={"libx264"})
	assert report.lines == []


def test_launch_check_at_most_8_cameras():
	cams = [cam(cameraName="Camera{}".format(i)) for i in range(9)]
	report = preflight.Report()
	preflight.CheckJetson(cams, report, probe=lambda c, folder: None)
	fails = [text for status, _, text in report.lines if status == preflight.FAIL]
	assert len(fails) == 1 and fails[0].startswith("9 cameras on the Jetson's encoder (Camera0, ")
	assert "it encodes at most 8 at once" in fails[0]
	report = preflight.Report()
	preflight.CheckJetson(cams[:8] + [cam(cameraName="Camera8", gpuID=-1)], report, probe=lambda c, folder: None)
	assert [status for status, _, _ in report.lines] == [preflight.OK]


def test_the_ready_line_names_the_jetsons_encoder(tmp_path):
	p = cam(videoFolder=str(tmp_path))
	assert startup.VideoText(p).startswith("h265 on the Jetson's encoder -> ")
	assert startup.VideoText(dict(p, gpuID=-1)).startswith("h265 on CPU -> ")
