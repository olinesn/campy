"""campy-encode-quality: the settings it tries, synthetic frames, and a real (lossless) clip."""

import json, math

import imageio_ffmpeg
import numpy as np

from campy import quality, sweep


def test_default_settings_bracket_the_configs():
	assert quality.Qualities(None, 21) == [15, 18, 21, 24, 27]
	assert quality.Qualities(None, 3) == [0, 3, 6, 9] # never below 0
	assert quality.Qualities("30,21", 21) == [30, 21] # as given


def test_synthetic_frames(tmp_path, capsys):
	out = tmp_path / "q.json"
	quality.Main(["--width", "160", "--height", "128", "--frameRate", "30", "--seconds", "1",
		"--encoder", "cpu:ultrafast", "--quality", "15,35", "--json", str(out)])
	result = json.load(open(out))
	assert (result["width"], result["height"], result["fps"], result["frames"]) == (160, 128, 30.0, 30)
	fine, coarse = result["rows"]
	assert (fine["encoder"], fine["quality"], coarse["quality"]) == ("libx264/ultrafast", 15, 35)
	assert fine["mbps"] > coarse["mbps"] > 0
	assert math.isclose(fine["gb_per_hour"], fine["mbps"] * 3600 / 8 / 1000)
	for row in result["rows"]:
		for key in ("psnr_clean", "psnr_input"):
			assert 20 < row[key] < 99
		for key in ("ssim_clean", "ssim_input"):
			assert 0.5 < row[key] <= 1
	# The finer setting keeps more of what it was given: the noise included.
	assert fine["psnr_input"] > coarse["psnr_input"]
	assert "PSNR clean" in capsys.readouterr().out


def test_real_clip_is_read_at_its_own_size_and_rate(tmp_path):
	clip = str(tmp_path / "clip.mkv")
	frames = sweep.SyntheticFrames(96, 64, "gray", 50, noise=3.0, seed=4)
	w = imageio_ffmpeg.write_frames(clip, (96, 64), fps=25, codec="ffv1", pix_fmt_in="gray",
		pix_fmt_out="gray", macro_block_size=1, output_params=["-level", "3"])
	w.send(None)
	for fr in frames:
		w.send(fr)
	w.close()

	out = tmp_path / "q.json"
	quality.Main(["--input", clip, "--seconds", "1", "--encoder", "cpu:ultrafast", "--quality", "0,30",
		"--json", str(out)])
	result = json.load(open(out))
	assert (result["width"], result["height"], result["fps"], result["frames"]) == (96, 64, 25.0, 25)
	lossless, lossy = result["rows"]
	# CRF 0 is lossless but for yuv420p's video range (256 gray levels -> 220), ~57 dB.
	# Frames scored out of step, or at the wrong size, would land near 20-35 dB.
	assert 52 < lossless["psnr_input"] < 62
	assert lossless["ssim_input"] > 0.998
	assert lossy["psnr_input"] < 50 and lossy["ssim_input"] < lossless["ssim_input"]
	assert not any(k.endswith("_clean") for k in lossless) # no clean scene for a real clip
