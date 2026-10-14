"""campy-encode-sweep: test frames, a small real sweep, and the capacity table."""

import json

import numpy as np

from campy import configurator, sweep


def test_synthetic_frames():
	frames = sweep.SyntheticFrames(64, 48, "gray", 4, seed=1)
	assert len(frames) == 4
	assert frames[0].shape == (48, 64) and frames[0].dtype == np.uint8
	assert not np.array_equal(frames[0], frames[1]) # the scene moves
	assert not np.array_equal(frames[0], sweep.SyntheticFrames(64, 48, "gray", 4, seed=2)[0])
	assert sweep.SyntheticFrames(64, 48, "rgb24", 1)[0].shape == (48, 64, 3)


def test_encoder_specs():
	base = configurator.DefaultParams()
	assert sweep.EncoderParams(base, "cpu:veryfast")["gpuID"] == -1
	assert sweep.EncoderParams(base, "cpu:veryfast")["preset"] == "veryfast"
	vaapi = sweep.EncoderParams(base, "vaapi")
	assert (vaapi["gpuID"], vaapi["gpuMake"]) == (0, "vaapi")


def test_small_sweep():
	params = sweep.BaseParams(sweep.ParseArgs(["--width", "160", "--height", "128", "--frameRate", "30"]))
	settings = {"seconds": 0.6, "warmup": 0.2, "headroom": 1.0, "frames": 8, "noise": 3.0}
	logged = []
	[entry] = sweep.Sweep(params, ["cpu:ultrafast"], [1, 2], settings, log=logged.append)
	assert entry["key"] == "libx264/ultrafast"
	assert (entry["width"], entry["height"], entry["fps"], entry["pix_fmt_in"]) == (160, 128, 30.0, "gray")
	assert [r["streams"] for r in entry["results"]] == [1, 2]
	assert entry["max_streams"] == 2 and entry["max_tested"] == 2
	assert entry["results"][0]["fps_min"] > 30 # a tiny frame encodes far faster than real time
	assert entry["bitrate_mbps"] > 0


def entry(key, fps=60.0, streams=4):
	return {"key": key, "width": 1280, "height": 1024, "fps": fps, "pix_fmt_in": "gray",
		"pix_fmt_out": "yuv420p", "max_streams": streams}


def test_save_merges_by_encoder_and_shape(tmp_path):
	path = str(tmp_path / "sub" / "capacity.json")
	host = {"hostname": "evo"}
	sweep.SaveEntries(path, [entry("libx264/veryfast"), entry("h264_vaapi")], host)
	sweep.SaveEntries(path, [entry("h264_vaapi", streams=8), entry("h264_vaapi", fps=30.0)], host)
	table = json.load(open(path))
	got = sorted((e["key"], e["fps"], e["max_streams"]) for e in table["entries"])
	assert got == [("h264_vaapi", 30.0, 4), ("h264_vaapi", 60.0, 8), ("libx264/veryfast", 60.0, 4)]

	# A table from another computer is replaced, not mixed in
	sweep.SaveEntries(path, [entry("libx264/fast")], {"hostname": "ms-s1"})
	table = json.load(open(path))
	assert [e["key"] for e in table["entries"]] == ["libx264/fast"]
	assert table["host"]["hostname"] == "ms-s1"
