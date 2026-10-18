"""Encoder settings (writer.EncoderArgs) and the ffmpeg command they make."""

import os

import numpy as np
import pytest
import imageio_ffmpeg

from campy import configurator, preflight, writer


# Every .mp4 starts with these (fragmentedVideo; tests/test_fragmented_video.py)
FRAGMENTED = ["-movflags", "+frag_keyframe+empty_moov+delay_moov+default_base_moof",
	"-frag_duration", "1000000", "-flush_packets", "1"]
# and an H.265 one also this (Apple's decoders need hvc1)
HVC1 = ["-tag:v", "hvc1"]


def cam(**overrides):
	params = configurator.DefaultParams()
	params.update(cameraName="Camera0", frameWidth=320, frameHeight=240,
		pixelFormatInput="gray", pixelFormatOutput="yuv420p")
	params.update(overrides)
	return params


def test_cpu_args_unchanged():
	enc = writer.EncoderArgs(cam(pixelFormatOutput="rgb0"))
	assert enc["codec"] == "libx264"
	assert enc["pix_fmt_out"] == "yuv420p" # rgb0 is mapped for the CPU path
	assert enc["output_params"] == FRAGMENTED + ["-preset", "fast", "-tune", "fastdecode", "-crf", "21",
		"-bufsize", "20M", "-maxrate", "10M", "-bf:v", "4"]
	assert enc["input_params"] == ["-an"]
	assert enc["macro_block_size"] == 16
	assert enc["key"] == "libx264/fast"
	assert enc["device"] == "cpu"


def test_nvidia_amd_intel_args_unchanged():
	nv = writer.EncoderArgs(cam(gpuID=1, gpuMake="nvidia", codec="h265"))
	assert nv["codec"] == "hevc_nvenc"
	assert nv["output_params"] == FRAGMENTED + HVC1 + ["-preset", "fast", "-qp", "21", "-bf:v", "0", "-gpu", "1"]
	assert nv["key"] == "hevc_nvenc/fast"

	amd = writer.EncoderArgs(cam(gpuID=0, gpuMake="amd"))
	assert amd["codec"] == "h264_amf"
	assert amd["output_params"] == FRAGMENTED + ["-usage", "lowlatency", "-rc", "cqp", "-qp_i", "21",
		"-qp_p", "21", "-qp_b", "21", "-bf:v", "0", "-hwaccel_device", "0"]
	assert amd["key"] == "h264_amf"

	qsv = writer.EncoderArgs(cam(gpuID=0, gpuMake="intel", pixelFormatOutput="rgb0"))
	assert qsv["codec"] == "h264_qsv"
	assert qsv["pix_fmt_out"] == "nv12"
	assert qsv["output_params"] == FRAGMENTED + ["-bf:v", "0", "-preset", "faster", "-q", "22"]


@pytest.mark.parametrize("codec", ["h264", "h265", "av1"])
def test_quick_sync_is_given_nv12_for_yuv420p(codec):
	# Quick Sync takes 8-bit frames as NV12 only. Given -pix_fmt yuv420p,
	# ffmpeg switched to NV12 itself and printed "Incompatible pixel format
	# 'yuv420p' for codec 'hevc_qsv', auto-selecting format 'nv12'"
	# (ChronicRecord PC, 2026-10-16)
	qsv = writer.EncoderArgs(cam(gpuID=0, gpuMake="intel", codec=codec, pixelFormatOutput="yuv420p"))
	assert qsv["pix_fmt_out"] == "nv12"
	# Other encoders keep yuv420p
	for make in ("nvidia", "amd"):
		assert writer.EncoderArgs(cam(gpuID=0, gpuMake=make, pixelFormatOutput="yuv420p"))["pix_fmt_out"] == "yuv420p"
	assert writer.EncoderArgs(cam(pixelFormatOutput="yuv420p"))["pix_fmt_out"] == "yuv420p"


def test_vaapi_args():
	enc = writer.EncoderArgs(cam(gpuID=0, gpuMake="vaapi", vaapiDevice="/dev/dri/renderD129"))
	assert enc["codec"] == "h264_vaapi"
	assert enc["input_params"] == ["-an", "-init_hw_device", "vaapi=va:/dev/dri/renderD129",
		"-filter_hw_device", "va"]
	assert enc["output_params"] == FRAGMENTED + ["-vf", "format=nv12,hwupload", "-rc_mode", "CQP", "-qp", "21", "-bf", "0"]
	assert enc["pix_fmt_out"] == "vaapi"
	assert enc["macro_block_size"] == 1 # no padding filter: it would replace -vf
	assert enc["key"] == "h264_vaapi"
	assert enc["device"] == "gpu"
	assert writer.EncoderArgs(cam(gpuID=0, gpuMake="vaapi", codec="h265"))["codec"] == "hevc_vaapi"


def test_vaapi_av1_quantizer():
	# AV1's VA-API encoder ignores -qp: its quantizer index comes from -global_quality
	enc = writer.EncoderArgs(cam(gpuID=0, gpuMake="vaapi", codec="av1", quality=120))
	assert enc["codec"] == "av1_vaapi"
	params = enc["output_params"]
	assert params[params.index("-global_quality") + 1] == "120" and "-qp" not in params
	# H.264 and H.265 keep -qp
	h265 = writer.EncoderArgs(cam(gpuID=0, gpuMake="vaapi", codec="h265", quality=24))["output_params"]
	assert h265[h265.index("-qp") + 1] == "24" and "-global_quality" not in h265


def test_av1_on_nvenc_and_quick_sync():
	# As on VA-API, quality is AV1's quantizer index (0-255), passed as it is
	nv = writer.EncoderArgs(cam(gpuID=0, gpuMake="nvidia", codec="av1", quality=88))
	assert nv["codec"] == "av1_nvenc"
	assert nv["output_params"] == FRAGMENTED + ["-preset", "fast", "-qp", "88", "-bf:v", "0", "-gpu", "0"]
	assert nv["key"] == "av1_nvenc/fast"

	qsv = writer.EncoderArgs(cam(gpuID=0, gpuMake="intel", codec="av1", quality=88))
	assert qsv["codec"] == "av1_qsv"
	assert qsv["output_params"] == FRAGMENTED + ["-bf:v", "0", "-preset", "faster", "-q", "88"]
	assert qsv["key"] == "av1_qsv/faster"

	# H.264 and H.265 on Quick Sync keep quality + 1
	for codec, name in (("h264", "h264_qsv"), ("h265", "hevc_qsv")):
		enc = writer.EncoderArgs(cam(gpuID=0, gpuMake="intel", codec=codec, quality=24))
		tag = HVC1 if codec == "h265" else []
		assert enc["codec"] == name and enc["output_params"] == FRAGMENTED + tag + ["-bf:v", "0", "-preset", "faster", "-q", "25"]


def test_keyframe_interval_applies_to_vaapi():
	enc = writer.EncoderArgs(cam(gpuID=0, gpuMake="vaapi", frameRate=60, keyframeInterval=0.5))
	assert enc["output_params"][-2:] == ["-g", "30"]


class Captured(Exception):
	pass


def test_vaapi_command_order(tmp_path, monkeypatch):
	# The hardware device must be set up before the input, and the upload
	# filter must follow -pix_fmt vaapi, which imageio-ffmpeg always adds.
	seen = {}
	real_popen = imageio_ffmpeg._io.subprocess.Popen
	monkeypatch.delenv("AMD_DEBUG", raising=False) # and restored after the test

	def fake_popen(cmd, *args, **kwargs):
		if "-vcodec" not in cmd: # imageio's own ffmpeg -version probe
			return real_popen(cmd, *args, **kwargs)
		seen["cmd"] = cmd
		seen["AMD_DEBUG"] = os.environ.get("AMD_DEBUG")
		raise Captured()

	monkeypatch.setattr(imageio_ffmpeg._io.subprocess, "Popen", fake_popen)
	params = cam(gpuID=0, gpuMake="vaapi", videoFolder=str(tmp_path), videoFilename="v.mp4")
	with pytest.raises(Captured):
		writer.OpenWriter(params, [])
	cmd = seen["cmd"]
	assert cmd.index("-init_hw_device") < cmd.index("-i")
	assert cmd[cmd.index("-pix_fmt", cmd.index("-i")) + 1] == "vaapi"
	assert cmd.index("-vf") > cmd.index("-i")
	assert cmd.count("-vf") == 1
	assert cmd[cmd.index("-vcodec", cmd.index("-i")) + 1] == "h264_vaapi"
	# ffmpeg starts with linear video surfaces (see the next tests)
	assert seen["AMD_DEBUG"] == "novideotiling"


def test_vaapi_linear_surfaces():
	# On by default: frames go straight into the surface, not through a GPU copy
	assert writer.EncoderArgs(cam(gpuID=0, gpuMake="vaapi"))["env"] == {"AMD_DEBUG": "novideotiling"}
	assert writer.EncoderArgs(cam(gpuID=0, gpuMake="vaapi", vaapiLinearSurfaces=False))["env"] == {}
	# Nothing for the other encoders
	assert writer.EncoderArgs(cam())["env"] == {}
	assert writer.EncoderArgs(cam(gpuID=0, gpuMake="nvidia"))["env"] == {}


def test_set_environment_adds_to_amd_debug(monkeypatch):
	monkeypatch.delenv("AMD_DEBUG", raising=False)
	writer.SetEnvironment({"AMD_DEBUG": "novideotiling"})
	assert os.environ["AMD_DEBUG"] == "novideotiling"
	# A user's own Mesa flags are kept, and a flag already there is not repeated
	monkeypatch.setenv("AMD_DEBUG", "nodcc")
	writer.SetEnvironment({"AMD_DEBUG": "novideotiling"})
	assert os.environ["AMD_DEBUG"] == "nodcc,novideotiling"
	writer.SetEnvironment({"AMD_DEBUG": "novideotiling"})
	assert os.environ["AMD_DEBUG"] == "nodcc,novideotiling"


def test_cpu_writer_encodes(tmp_path):
	params = cam(videoFolder=str(tmp_path), videoFilename="v.mp4", frameRate=30)
	ffmpeg, writing, _ = writer.OpenWriter(params, [])
	assert writing
	for i in range(30):
		ffmpeg.send(np.full((240, 320), i * 8, dtype=np.uint8))
	ffmpeg.close()
	frames, _ = imageio_ffmpeg.count_frames_and_secs(str(tmp_path / "Camera0" / "v.mp4"))
	assert frames == 30


def test_cpu_h264_says_nothing_at_warning_level(tmp_path, capfd):
	# -x264-params nal-hrd=cbr made libx264 warn "CBR HRD requires constant
	# bitrate" on every CPU H.264 recording: CBR signalling cannot go with -crf
	params = cam(videoFolder=str(tmp_path), videoFilename="v.mp4", frameRate=30, ffmpegLogLevel="warning")
	ffmpeg, writing, _ = writer.OpenWriter(params, [])
	assert writing
	for i in range(10):
		ffmpeg.send(np.full((240, 320), i * 8, dtype=np.uint8))
	ffmpeg.close()
	assert capfd.readouterr().err == ""


# H.265 in an MP4 or MOV is tagged hvc1: Apple's decoders refuse ffmpeg's
# default, hev1 (MATLAB R2023b's VideoReader on a Mac: "Cannot Decode")

H265 = {None: "libx265", "nvidia": "hevc_nvenc", "amd": "hevc_amf", "intel": "hevc_qsv", "vaapi": "hevc_vaapi"}


@pytest.mark.parametrize("make", list(H265))
def test_h265_in_mp4_or_mov_is_tagged_hvc1(make):
	gpu = {} if make is None else {"gpuID": 0, "gpuMake": make}
	for name in ("0.mp4", "0.mov", "0.MP4"):
		for fragmented in (True, False):
			enc = writer.EncoderArgs(cam(codec="h265", videoFilename=name, fragmentedVideo=fragmented, **gpu))
			assert enc["codec"] == H265[make]
			params = enc["output_params"]
			assert params.count("-tag:v") == 1 and params[params.index("-tag:v") + 1] == "hvc1"
	# Not H.264 or AV1, nor H.265 in another container: Matroska has no such
	# tag, and .m4v (ffmpeg's ipod muxer) takes no H.265 at all
	for codec in ("h264", "av1"):
		assert "-tag:v" not in writer.EncoderArgs(cam(codec=codec, **gpu))["output_params"]
	for name in ("0.mkv", "0.avi", "0.m4v"):
		assert "-tag:v" not in writer.EncoderArgs(cam(codec="h265", videoFilename=name, **gpu))["output_params"]


def test_libx265_closes_its_gops():
	# Open-GOP keyframes, libx265's default, made Apple's decoder fail and
	# seeks to the frame before a keyframe give the keyframe
	params = writer.EncoderArgs(cam(codec="h265"))["output_params"]
	assert params[params.index("-x265-params") + 1] == "open-gop=0"
	for make in (None, "nvidia", "amd", "intel", "vaapi"):
		gpu = {} if make is None else {"gpuID": 0, "gpuMake": make}
		for codec in ("h264", "h265", "av1"):
			if (make, codec) != (None, "h265"):
				assert "-x265-params" not in writer.EncoderArgs(cam(codec=codec, **gpu))["output_params"]


def test_libx265_video_is_hvc1_and_seeks_exactly(tmp_path):
	exe = imageio_ffmpeg.get_ffmpeg_exe()
	if "libx265" not in (preflight.FfmpegEncoders(exe) or ()):
		pytest.skip("this ffmpeg has no libx265")
	params = cam(codec="h265", videoFolder=str(tmp_path), videoFilename="v.mp4", frameRate=30, keyframeInterval=0.5)
	ffmpeg, writing, _ = writer.OpenWriter(params, [])
	assert writing
	for i in range(30):
		ffmpeg.send(np.full((240, 320), i * 8, dtype=np.uint8))
	ffmpeg.close()
	path = str(tmp_path / "Camera0" / "v.mp4")
	import subprocess
	err = subprocess.run([exe, "-hide_banner", "-i", path], capture_output=True, text=True).stderr
	assert "hevc" in err and "(hvc1 / 0x31637668)" in err
	assert imageio_ffmpeg.count_frames_and_secs(path)[0] == 30
	# Keyframes at 0 and 15: a seek to 14 starts at 0, and gives frame 14
	for k in (13, 14, 15, 16):
		reader = imageio_ffmpeg.read_frames(path, pix_fmt="gray", bits_per_pixel=8,
			input_params=["-ss", "{:.4f}".format((k - 0.25) / 30)])
		next(reader)
		level = np.frombuffer(next(reader), np.uint8).mean()
		reader.close()
		assert abs(level - k * 8) <= 2, (k, level)


# colorRange: "full" keeps a camera's 0-255 levels (writer.EncoderArgs)

def test_limited_range_is_the_default_and_unchanged():
	for make in (None, "nvidia", "intel", "amd", "vaapi"):
		gpu = {} if make is None else {"gpuID": 0, "gpuMake": make}
		default = writer.EncoderArgs(cam(**gpu))
		assert writer.EncoderArgs(cam(colorRange="limited", **gpu)) == default
		assert "-color_range" not in default["output_params"]


@pytest.mark.parametrize("make", [None, "nvidia", "intel", "amd"])
def test_full_range_args(make):
	gpu = {} if make is None else {"gpuID": 0, "gpuMake": make}
	enc = writer.EncoderArgs(cam(colorRange="full", **gpu))
	assert enc["output_params"][-4:] == ["-vf", "scale=out_range=full", "-color_range", "pc"]
	assert enc["output_params"].count("-vf") == 1
	assert enc["macro_block_size"] == 1 # 320x240 needs no padding, and imageio's would replace -vf
	assert writer.EncoderArgs(cam(colorRange="FULL", **gpu)) == enc


def test_full_range_on_vaapi_scales_before_the_upload():
	enc = writer.EncoderArgs(cam(gpuID=0, gpuMake="vaapi", colorRange="full"))
	params = enc["output_params"]
	assert params[params.index("-vf") + 1] == "scale=out_range=full,format=nv12,hwupload"
	assert params.count("-vf") == 1 and params[-2:] == ["-color_range", "pc"]


def test_full_range_pads_as_imageio_would():
	# imageio-ffmpeg scales 330x250 up to 336x256 with a -vf of its own; with
	# full range that scaling is in campy's -vf, and imageio adds none
	enc = writer.EncoderArgs(cam(colorRange="full", frameWidth=330, frameHeight=250))
	assert enc["output_params"][-4:-2] == ["-vf", "scale=336:256:out_range=full"]
	assert enc["macro_block_size"] == 1
	# A RatCam's sides only need to be even
	ratcam = writer.EncoderArgs(cam(colorRange="full", cameraMake="ratcam", frameWidth=160, frameHeight=120))
	assert ratcam["output_params"][-4:-2] == ["-vf", "scale=out_range=full"]


def test_full_range_leaves_nvenc_rgb_input_alone(tmp_path, monkeypatch, capsys):
	# NVENC given RGB converts it to YUV itself, in limited range: marking that
	# video full range would be wrong, so campy leaves it and says so
	params = cam(gpuID=0, gpuMake="nvidia", pixelFormatOutput="rgb0", colorRange="full",
		videoFolder=str(tmp_path), videoFilename="v.mp4")
	enc = writer.EncoderArgs(params)
	assert enc == writer.EncoderArgs(dict(params, colorRange="limited"))
	assert "-color_range" not in enc["output_params"] and "-vf" not in enc["output_params"]
	cmd = CapturedCommand(params, monkeypatch)
	assert 'colorRange "full" does nothing with pixelFormatOutput "rgb0"' in capsys.readouterr().out
	assert "-color_range" not in cmd


def CapturedCommand(params, monkeypatch):
	# The ffmpeg command OpenWriter starts, without starting it
	seen = {}
	real_popen = imageio_ffmpeg._io.subprocess.Popen

	def fake_popen(cmd, *args, **kwargs):
		if "-vcodec" not in cmd: # imageio's own ffmpeg -version probe
			return real_popen(cmd, *args, **kwargs)
		seen["cmd"] = cmd
		raise Captured()

	monkeypatch.setattr(imageio_ffmpeg._io.subprocess, "Popen", fake_popen)
	with pytest.raises(Captured):
		writer.OpenWriter(params, [])
	return seen["cmd"]


def test_full_range_command(tmp_path, monkeypatch):
	params = cam(colorRange="full", frameWidth=330, frameHeight=250, videoFolder=str(tmp_path), videoFilename="v.mp4")
	cmd = CapturedCommand(params, monkeypatch)
	assert cmd.count("-vf") == 1 # campy's, which pads; not imageio's as well
	assert cmd[cmd.index("-vf") + 1] == "scale=336:256:out_range=full"
	assert cmd[cmd.index("-color_range") + 1] == "pc" and cmd.index("-color_range") > cmd.index("-i")


def RampVideo(tmp_path, color_range):
	# A gray ramp, every level 0-255, through campy's CPU writer losslessly (quality 0 = CRF 0)
	params = cam(videoFolder=str(tmp_path), videoFilename=color_range + ".mp4", frameRate=10, quality=0,
		frameWidth=512, frameHeight=64, colorRange=color_range)
	ramp = np.tile((np.arange(512) // 2).astype(np.uint8), (64, 1))
	ffmpeg, writing, _ = writer.OpenWriter(params, [])
	for _ in range(5):
		ffmpeg.send(ramp)
	ffmpeg.close()
	path = str(tmp_path / "Camera0" / (color_range + ".mp4"))
	reader = imageio_ffmpeg.read_frames(path, pix_fmt="gray", bits_per_pixel=8)
	next(reader)
	frame = np.frombuffer(next(reader), np.uint8).reshape(64, 512)
	reader.close()
	return ramp, frame, path


def ColorRangeTag(path):
	# What the file says, from ffmpeg's description of its video stream
	import subprocess
	err = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", path], capture_output=True, text=True).stderr
	line = next(l for l in err.splitlines() if "Video:" in l)
	return "pc" if ("(pc" in line or "yuvj" in line) else "tv"


def test_full_range_video_gives_back_every_gray_level(tmp_path):
	ramp, frame, path = RampVideo(tmp_path, "full")
	assert ColorRangeTag(path) == "pc"
	assert (frame == ramp).all()


def test_limited_range_video_rounds_the_levels(tmp_path):
	# The control: 256 levels squeezed into 220 do not all come back
	ramp, frame, path = RampVideo(tmp_path, "limited")
	assert ColorRangeTag(path) == "tv"
	assert not (frame == ramp).all() and np.abs(frame.astype(int) - ramp).max() <= 1


def test_a_bayer_pattern_goes_to_ffmpeg_as_configured(tmp_path, monkeypatch):
	# OpenWriter used to "flip blue and red" for FLIR bayer_bggr8 with a comparison
	# that changed nothing; the line is gone, and ffmpeg still gets the pattern the
	# config names, for every camera make
	for make in ("flir", "basler"):
		params = cam(cameraMake=make, pixelFormatInput="bayer_bggr8", videoFolder=str(tmp_path), videoFilename="v.mp4")
		cmd = CapturedCommand(params, monkeypatch)
		assert cmd[cmd.index("-pix_fmt") + 1] == "bayer_bggr8" and cmd.index("-pix_fmt") < cmd.index("-i")
		assert params["pixelFormatInput"] == "bayer_bggr8"
