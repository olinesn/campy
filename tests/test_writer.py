"""Encoder settings (writer.EncoderArgs) and the ffmpeg command they make."""

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

	def fake_popen(cmd, *args, **kwargs):
		if "-vcodec" not in cmd: # imageio's own ffmpeg -version probe
			return real_popen(cmd, *args, **kwargs)
		seen["cmd"] = cmd
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


def test_cpu_writer_encodes(tmp_path):
	params = cam(videoFolder=str(tmp_path), videoFilename="v.mp4", frameRate=30)
	ffmpeg, writing, _ = writer.OpenWriter(params, [])
	assert writing
	for i in range(30):
		ffmpeg.send(np.full((240, 320), i * 8, dtype=np.uint8))
	ffmpeg.close()
	frames, _ = imageio_ffmpeg.count_frames_and_secs(str(tmp_path / "Camera0" / "v.mp4"))
	assert frames == 30
