"""denoise: a temporal denoiser (ffmpeg's atadenoise) before the encoder (writer.EncoderArgs)."""

import os
from argparse import ArgumentParser

import numpy as np
import pytest
import imageio_ffmpeg

from campy import configurator, sweep, quality, writer
from test_writer import cam, CapturedCommand

ATADENOISE = "atadenoise=s=5"
ENCODERS = {
	"cpu": {},
	"nvidia": {"gpuID": 0, "gpuMake": "nvidia"},
	"amd": {"gpuID": 0, "gpuMake": "amd"},
	"intel": {"gpuID": 0, "gpuMake": "intel"},
	"vaapi": {"gpuID": 0, "gpuMake": "vaapi"},
	}


def Vf(enc):
	params = enc["output_params"]
	assert params.count("-vf") == 1 # ffmpeg keeps only the last -vf: every filter must be in one chain
	return params[params.index("-vf") + 1]


def WithoutVf(params):
	if "-vf" not in params:
		return list(params)
	i = params.index("-vf")
	return params[:i] + params[i + 2:]


@pytest.fixture(autouse=True)
def ffmpeg_8(monkeypatch):
	# colorRange "full" adds a yuv420p detour before ffmpeg 8 (test_writer.py)
	monkeypatch.setattr(writer, "FfmpegVersion", lambda exe=None: (8, 1))


@pytest.mark.parametrize("make", ENCODERS)
@pytest.mark.parametrize("codec", ["h264", "h265", "av1"])
def test_off_by_default_and_unchanged(make, codec):
	default = writer.EncoderArgs(cam(codec=codec, **ENCODERS[make]))
	assert configurator.DefaultParams()["denoise"] == "None"
	assert writer.EncoderArgs(cam(codec=codec, denoise="None", **ENCODERS[make])) == default
	assert "-filter_threads" not in default["input_params"]
	assert "atadenoise" not in " ".join(default["output_params"])


@pytest.mark.parametrize("make", ENCODERS)
@pytest.mark.parametrize("codec", ["h264", "h265", "av1"])
def test_on_for_every_encoder(make, codec):
	off = writer.EncoderArgs(cam(codec=codec, **ENCODERS[make]))
	on = writer.EncoderArgs(cam(codec=codec, denoise="atadenoise", **ENCODERS[make]))
	# one filter thread per camera, not one per CPU core
	assert on["input_params"] == off["input_params"] + ["-filter_threads", "1"]
	# the filter first, on the camera's frames, before VA-API's conversion and upload
	assert Vf(on) == (ATADENOISE + ",format=nv12,hwupload" if make == "vaapi" else ATADENOISE)
	# everything else as it was: encoder, its pixel format, its options
	assert WithoutVf(on["output_params"]) == WithoutVf(off["output_params"])
	for key in ("codec", "pix_fmt_out", "key", "device", "env"):
		assert on[key] == off[key]
	# 320x240 needs no padding; imageio-ffmpeg's padding -vf would replace this one
	assert on["macro_block_size"] == 1


def test_keeps_pixel_format_output():
	# NVENC given RGB, Quick Sync's NV12, the CPU's yuv420p: the filter changes none of them
	nv = writer.EncoderArgs(cam(gpuID=0, gpuMake="nvidia", pixelFormatInput="rgb24", pixelFormatOutput="rgb0",
		denoise="atadenoise"))
	assert nv["pix_fmt_out"] == "rgb0" and Vf(nv) == ATADENOISE
	assert writer.EncoderArgs(cam(gpuID=0, gpuMake="intel", denoise="atadenoise"))["pix_fmt_out"] == "nv12"
	assert writer.EncoderArgs(cam(pixelFormatOutput="rgb0", denoise="atadenoise"))["pix_fmt_out"] == "yuv420p"


def test_pads_as_imageio_would():
	# imageio-ffmpeg scales 330x250 up to 336x256 with a -vf of its own, which
	# campy's would replace: campy pads, after the filter
	enc = writer.EncoderArgs(cam(denoise="atadenoise", frameWidth=330, frameHeight=250))
	assert Vf(enc) == ATADENOISE + ",scale=336:256" and enc["macro_block_size"] == 1
	# A RatCam's sides only need to be even
	ratcam = writer.EncoderArgs(cam(denoise="atadenoise", cameraMake="ratcam", frameWidth=160, frameHeight=120))
	assert Vf(ratcam) == ATADENOISE
	# VA-API pads nothing
	assert Vf(writer.EncoderArgs(cam(gpuID=0, gpuMake="vaapi", denoise="atadenoise", frameWidth=330, frameHeight=250))) \
		== ATADENOISE + ",format=nv12,hwupload"


def test_with_full_range(monkeypatch):
	def vf(**values):
		return Vf(writer.EncoderArgs(cam(colorRange="full", denoise="atadenoise", **values)))
	for make in ("cpu", "nvidia", "amd", "intel"):
		assert vf(**ENCODERS[make]) == ATADENOISE + ",scale=out_range=full"
	assert vf(frameWidth=330, frameHeight=250) == ATADENOISE + ",scale=336:256:out_range=full"
	assert vf(gpuID=0, gpuMake="vaapi") == ATADENOISE + ",scale=out_range=full,format=nv12,hwupload"
	# ffmpeg before 8: the gray frames' detour through yuv420p comes after the filter too
	monkeypatch.setattr(writer, "FfmpegVersion", lambda exe=None: (7, 1))
	assert vf(gpuID=0, gpuMake="intel") == \
		ATADENOISE + ",scale=out_range=full,format=yuv420p,scale=in_range=full:out_range=full"


def test_command(tmp_path, monkeypatch):
	params = cam(denoise="atadenoise", frameWidth=330, frameHeight=250, videoFolder=str(tmp_path), videoFilename="v.mp4")
	cmd = CapturedCommand(params, monkeypatch)
	assert cmd.count("-vf") == 1 # campy's, which pads; not imageio's as well
	assert cmd[cmd.index("-vf") + 1] == ATADENOISE + ",scale=336:256"
	assert cmd[cmd.index("-filter_threads") + 1] == "1" and cmd.index("-filter_threads") < cmd.index("-i")


def test_unknown_values_refused(tmp_path):
	for value in ("atadenoise=s=9", "hqdn3d", "Atadenoise", "'on'", "on"):
		path = tmp_path / "c.yaml"
		path.write_text("denoise: {}\n".format(value))
		clargs = configurator.ParseClargs(ArgumentParser(prog="campy-acquire"), [str(path)])
		with pytest.raises(configurator.ConfigError) as e:
			configurator.ResolveParams(clargs)
		# unquoted on is YAML's True, refused as not text
		said = "is not text" if value == "on" else '"None" or "atadenoise"'
		assert any(said in p for p in e.value.problems), e.value.problems
		value = value.strip("'")
		with pytest.raises(ValueError, match='denoise is "None" or "atadenoise"'):
			writer.EncoderArgs(cam(denoise=value))
	# one per camera
	path = tmp_path / "c.yaml"
	path.write_text("numCams: 2\ndenoise: [atadenoise, None]\n")
	params = configurator.ResolveParams(configurator.ParseClargs(ArgumentParser(prog="campy-acquire"), [str(path)]))
	assert params["denoise"] == ["atadenoise", "None"]


def test_encode_tools_take_it():
	# campy-encode-sweep and campy-encode-quality measure the same ffmpeg command a recording runs
	assert sweep.BaseParams(sweep.ParseArgs(["--denoise", "atadenoise"]))["denoise"] == "atadenoise"
	assert sweep.BaseParams(sweep.ParseArgs([]))["denoise"] == "None"
	assert sweep.BaseParams(quality.ParseArgs(["--denoise", "atadenoise"]))["denoise"] == "atadenoise"
	with pytest.raises(SystemExit):
		sweep.ParseArgs(["--denoise", "hqdn3d"])


def EncodeLossless(tmp_path, frames, denoise):
	# Gray frames through campy's CPU writer, losslessly: full range keeps
	# every gray level and quality 0 is libx264's CRF 0
	name = "{}.mp4".format(denoise)
	params = cam(videoFolder=str(tmp_path), videoFilename=name, frameRate=30, quality=0,
		frameWidth=frames.shape[2], frameHeight=frames.shape[1], colorRange="full", denoise=denoise)
	ffmpeg, _, _ = writer.OpenWriter(params, [])
	for frame in frames:
		ffmpeg.send(frame)
	ffmpeg.close()
	reader = imageio_ffmpeg.read_frames(str(tmp_path / "Camera0" / name), pix_fmt="gray", bits_per_pixel=8)
	next(reader)
	out = np.stack([np.frombuffer(f, np.uint8).reshape(frames.shape[1:]) for f in reader])
	reader.close()
	return out


def test_real_ffmpeg_run(tmp_path):
	# A noisy still background and a bright square moving 8 px a frame, as an
	# emulated camera sends them, through this machine's ffmpeg
	rng = np.random.default_rng(0)
	frames = np.clip(rng.normal(60, 2, (12, 64, 128)), 0, 255).astype(np.uint8)
	for i, frame in enumerate(frames):
		frame[20:40, 8 * i:8 * i + 20] = 200
	plain = EncodeLossless(tmp_path, frames, "None")
	filtered = EncodeLossless(tmp_path, frames, "atadenoise")
	assert len(plain) == len(filtered) == len(frames)
	assert (plain == frames).all() # lossless without the filter
	change = np.abs(filtered.astype(int) - frames.astype(int))
	# No pixel moves by more than 4 gray levels (ffmpeg's default thresholds)
	assert change.max() <= 4
	# The still background's frame-to-frame noise goes down
	background = (slice(None), slice(48, 64))
	assert np.diff(filtered[background].astype(int), axis=0).std() < 0.8 * np.diff(frames[background].astype(int), axis=0).std()
	# The moving square's inside (200 against 60 the frame before and after) is left alone
	for i in range(2, len(frames) - 2):
		assert (filtered[i, 22:38, 8 * i + 9:8 * i + 11] == 200).all()
