"""Encoder settings (writer.EncoderArgs) and the ffmpeg command they make."""

import os

import numpy as np
import pytest
import imageio_ffmpeg

from campy import configurator, writer


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
	assert enc["output_params"] == ["-preset", "fast", "-tune", "fastdecode", "-crf", "21",
		"-bufsize", "20M", "-maxrate", "10M", "-bf:v", "4", "-x264-params", "nal-hrd=cbr"]
	assert enc["input_params"] == ["-an"]
	assert enc["macro_block_size"] == 16
	assert enc["key"] == "libx264/fast"
	assert enc["device"] == "cpu"


def test_nvidia_amd_intel_args_unchanged():
	nv = writer.EncoderArgs(cam(gpuID=1, gpuMake="nvidia", codec="h265"))
	assert nv["codec"] == "hevc_nvenc"
	assert nv["output_params"] == ["-preset", "fast", "-qp", "21", "-bf:v", "0", "-gpu", "1"]
	assert nv["key"] == "hevc_nvenc/fast"

	amd = writer.EncoderArgs(cam(gpuID=0, gpuMake="amd"))
	assert amd["codec"] == "h264_amf"
	assert amd["output_params"] == ["-usage", "lowlatency", "-rc", "cqp", "-qp_i", "21",
		"-qp_p", "21", "-qp_b", "21", "-bf:v", "0", "-hwaccel_device", "0"]
	assert amd["key"] == "h264_amf"

	qsv = writer.EncoderArgs(cam(gpuID=0, gpuMake="intel", pixelFormatOutput="rgb0"))
	assert qsv["codec"] == "h264_qsv"
	assert qsv["pix_fmt_out"] == "nv12"
	assert qsv["output_params"] == ["-bf:v", "0", "-preset", "faster", "-q", "22"]


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
	assert enc["output_params"] == ["-vf", "format=nv12,hwupload", "-rc_mode", "CQP", "-qp", "21", "-bf", "0"]
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
	assert nv["output_params"] == ["-preset", "fast", "-qp", "88", "-bf:v", "0", "-gpu", "0"]
	assert nv["key"] == "av1_nvenc/fast"

	qsv = writer.EncoderArgs(cam(gpuID=0, gpuMake="intel", codec="av1", quality=88))
	assert qsv["codec"] == "av1_qsv"
	assert qsv["output_params"] == ["-bf:v", "0", "-preset", "faster", "-q", "88"]
	assert qsv["key"] == "av1_qsv/faster"

	# H.264 and H.265 on Quick Sync keep quality + 1
	for codec, name in (("h264", "h264_qsv"), ("h265", "hevc_qsv")):
		enc = writer.EncoderArgs(cam(gpuID=0, gpuMake="intel", codec=codec, quality=24))
		assert enc["codec"] == name and enc["output_params"] == ["-bf:v", "0", "-preset", "faster", "-q", "25"]


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
