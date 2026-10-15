"""campy-synctest, on made-up recordings whose cameras' exposure offsets are known."""

import csv, json

import imageio_ffmpeg
import numpy as np
import pytest

from campy import synctest

FPS = 25
T = 1 / FPS * (1 + 30e-6)       # the trigger's real period
HZ = 43                         # the test LED, from the same clock
HOST0 = 5_000_000_000_000


def lit_seconds(a, b, hz):
	# Seconds a 50% square wave at hz (same clock as the trigger) is on within [a, b]
	period = 1 / (hz * (1 + 30e-6))
	def until(t):
		x = np.maximum(t - 0.0123, 0)
		n = np.floor(x / period)
		return n * period / 2 + np.minimum(x - n * period, period / 2)
	return until(b) - until(a)


def camera(root, name, latency_us, first=1, last=500, exposure_us=2000, hz=HZ, led=150.0, codec="ffv1", seed=0):
	"""A triggered camera whose exposure starts latency_us after each of triggers first..last."""
	rng = np.random.default_rng(seed)
	folder = root / name
	folder.mkdir()
	k = np.arange(first, last + 1)
	start = (k - 1) * T + latency_us * 1e-6
	lit = lit_seconds(start, start + exposure_us * 1e-6, hz) / (exposure_us * 1e-6)
	h, w = 64, 80
	yy, xx = np.mgrid[0:h, 0:w]
	spot = (xx - 60) ** 2 + (yy - 16) ** 2 < 6 ** 2
	video = "rec.mkv" if codec == "ffv1" else "rec.mp4"
	params = ["-crf", "12"] if codec == "libx264" else []
	writer = imageio_ffmpeg.write_frames(str(folder / video), (w, h), fps=FPS, codec=codec, pix_fmt_in="gray",
		pix_fmt_out="gray" if codec == "ffv1" else "yuv420p", macro_block_size=16, output_params=params)
	writer.send(None)
	for i in range(len(k)):
		img = rng.normal(30, 2, (h, w))
		img[spot] += led * lit[i]
		writer.send(np.clip(np.round(img), 0, 255).astype(np.uint8))
	writer.close()
	t = (k - 1) * T * (1 + 20e-6) # the camera's own clock
	np.save(folder / "frametimes.npy", np.array([np.arange(1, len(k) + 1), t - t[0]]))
	arrive = HOST0 + start * 1e9 + 6e6 + rng.exponential(1e6, len(k))
	np.save(folder / "hosttimes.npy", arrive.astype(np.int64))
	with open(folder / "metadata.csv", "w", newline="") as f:
		out = csv.writer(f, quoting=csv.QUOTE_ALL)
		out.writerows([("cameraName", name), ("cameraMake", "flir"), ("frameRate", FPS), ("cameraTrigger", "Line0"),
			("videoFilename", video), ("cameraExposureTimeInUs", exposure_us)])


def record(root, names, hz=HZ):
	(root / "trigger.json").write_text(json.dumps({"triggerStarted": True, "startedBy": "campy",
		"readyBeforeTrigger": sorted(names), "notReady": {}, "startedHostNs": HOST0 - 4_000_000_000,
		"blinkPin": 13, "blinkHz": hz}))


@pytest.mark.parametrize("offset_us", [30.0, -45.0])
def test_locked_cameras_pass_with_their_offset(tmp_path, offset_us):
	camera(tmp_path, "CamA", 50)
	camera(tmp_path, "CamB", 50 + offset_us, seed=1)
	record(tmp_path, ["CamA", "CamB"])
	r = synctest.Analyze(str(tmp_path))
	p = r["pairs"]["CamB"]
	assert r["verdict"] == p["verdict"] == synctest.PASS
	assert (r["blink_hz"], r["reference"], p["method"]) == (HZ, "CamA", "edges")
	assert p["offset_us"] == pytest.approx(offset_us, abs=3)
	assert p["offset_se_us"] < 2 and p["contradictions"] == 0 and p["pairs"] == 500
	assert all(c["led_seen"] and not c["saturated"] for c in r["cameras"].values())
	assert not r["warnings"]
	assert json.loads((tmp_path / "synctest.json").read_text())["verdict"] == synctest.PASS


def test_lossy_video_still_measures_the_offset(tmp_path):
	# What campy records: H.264 (here at crf 12), not lossless
	camera(tmp_path, "CamA", 50, codec="libx264")
	camera(tmp_path, "CamB", 70, codec="libx264", seed=2)
	record(tmp_path, ["CamA", "CamB"])
	p = synctest.Analyze(str(tmp_path))["pairs"]["CamB"]
	assert p["verdict"] == synctest.PASS and p["offset_us"] == pytest.approx(20, abs=8)


def test_cameras_out_of_step_fail(tmp_path):
	# CamB exposes 3 ms late on every trigger: more than an exposure, so from the phase
	camera(tmp_path, "CamA", 50)
	camera(tmp_path, "CamB", 3050, seed=3)
	record(tmp_path, ["CamA", "CamB"])
	p = synctest.Analyze(str(tmp_path))["pairs"]["CamB"]
	assert p["verdict"] == synctest.FAIL and "steady offset" in p["reason"]
	assert p["method"] == "phase" and p["offset_us"] == pytest.approx(3000, abs=50)


def test_pairs_by_trigger_even_when_a_camera_starts_late(tmp_path):
	camera(tmp_path, "CamA", 50)
	camera(tmp_path, "CamB", 60, first=26, seed=4) # opened a second late
	record(tmp_path, ["CamA"])
	p = synctest.Analyze(str(tmp_path))["pairs"]["CamB"]
	assert p["pairs"] == 475 and p["offset_us"] == pytest.approx(10, abs=3)


def test_no_led_is_inconclusive(tmp_path, capsys):
	camera(tmp_path, "CamA", 50, led=0)
	camera(tmp_path, "CamB", 50, led=0, seed=5)
	record(tmp_path, ["CamA", "CamB"])
	assert synctest.Main([str(tmp_path)]) == 1
	out = capsys.readouterr().out
	assert "does not see the blinking LED" in out and "Verdict: INCONCLUSIVE" in out


def test_warns_about_the_rates_and_exposure(tmp_path):
	camera(tmp_path, "CamA", 50, hz=50, exposure_us=8000)
	camera(tmp_path, "CamB", 50, hz=50, exposure_us=8000, seed=6)
	record(tmp_path, ["CamA", "CamB"], hz=50)
	text = " ".join(synctest.Analyze(str(tmp_path))["warnings"])
	assert "close to a whole or half number" in text and "is long for a 50 Hz blink" in text


def test_needs_the_blink_rate(tmp_path, capsys):
	camera(tmp_path, "CamA", 50)
	camera(tmp_path, "CamB", 50, seed=7)
	assert synctest.Main([str(tmp_path)]) == 2 # no trigger.json
	assert "no blink rate" in capsys.readouterr().err
	assert synctest.Main([str(tmp_path), "--blink-hz", "43", "--roi", "CamB=54,10,12,12"]) == 0
	assert json.loads((tmp_path / "synctest.json").read_text())["cameras"]["CamB"]["roi"] == [54, 10, 12, 12]
