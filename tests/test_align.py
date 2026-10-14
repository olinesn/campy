"""campy-align, on made-up recordings whose true trigger for every frame is known."""

import csv
import json

import numpy as np
import pytest

from campy import align


T_TRUE = 1 / 60 * (1 + 150e-6) # the trigger's real period: a trigger controller is a little off
HOST0 = 5_000_000_000_000      # the computer's clock (ns) at the first trigger


def trigger_s(k):
	# Seconds from trigger 1 to trigger k
	return (k - 1) * T_TRUE


def write_meta(folder, **meta):
	with open(folder / "metadata.csv", "w", newline="") as f:
		w = csv.writer(f, delimiter=",", quoting=csv.QUOTE_ALL)
		for row in meta.items():
			w.writerow(row)


def write_csv(path, header, rows):
	with open(path, "w", newline="") as f:
		w = csv.writer(f)
		w.writerow(header)
		w.writerows(rows)


def triggered_camera(root, name, first, last, missed=(), dropped=(), ppm=20.0, lag_ms=6.0,
		host=True, frame_rate=60, seed=0):
	"""
	A camera that made a frame for each of triggers first..last, except the
	missed ones. campy grabbed the dropped ones but did not write them. Returns
	{campy frame number: true trigger}.
	"""
	rng = np.random.default_rng(seed)
	folder = root / name
	folder.mkdir()
	grabbed = [k for k in range(first, last + 1) if k not in missed]
	written = [(n, k) for n, k in enumerate(grabbed, start=1) if k not in dropped]
	t = np.array([trigger_s(k) * (1 + ppm * 1e-6) for _, k in written])
	np.save(folder / "frametimes.npy", np.array([[n for n, _ in written], t - t[0]]))
	if host:
		arrive = [HOST0 + trigger_s(k) * 1e9 + lag_ms * 1e6 + rng.exponential(1.5e6) for _, k in written]
		np.save(folder / "hosttimes.npy", np.array(arrive, dtype=np.int64))
	write_meta(folder, cameraName=name, cameraMake="flir", frameRate=frame_rate, cameraTrigger="Line0")
	return dict(written)


def ratcam(root, name, first_pulse, last_pulse, fps=20.0, start_s=0.01, duration_s=9.9, ppm=-30.0,
		pulse_times=None, seed=1):
	"""
	A RatCam with the trigger on its SYNC input. Its clock runs ppm off and
	from its own zero; pulses can wait behind a frame on USB. Returns
	{campy frame number: true fractional trigger of the frame's start}.
	"""
	rng = np.random.default_rng(seed)
	folder = root / name
	folder.mkdir()

	def dev_us(s): # the camera's clock, for seconds from trigger 1
		return int(round((123.4 + s * (1 + ppm * 1e-6)) * 1e6))

	if pulse_times is None:
		pulse_times = [trigger_s(k) for k in range(first_pulse, last_pulse + 1)]
	pulses = []
	for n, s in enumerate(pulse_times):
		queued = rng.uniform(0, 100e6) if rng.random() < 0.7 else 0 # behind a frame on a full-speed link
		pulses.append([1000 + n, dev_us(s), int(HOST0 + s * 1e9 + 0.3e6 + queued)])
	write_csv(folder / "ratcam_sync.csv", ["pulse", "t_us", "host_ns"], pulses)

	starts = start_s + np.arange(int(duration_s * fps)) / fps
	frames = [[n, 500 + n, dev_us(s), int(HOST0 + (s + 1 / fps + 0.05) * 1e9)] for n, s in enumerate(starts, start=1)]
	write_csv(folder / "ratcam_frames.csv", ["frame", "camera_frame", "t_us", "host_ns"], frames)
	t_us = np.array([f[2] for f in frames])
	np.save(folder / "frametimes.npy", np.array([[f[0] for f in frames], (t_us - t_us[0]) / 1e6]))
	np.save(folder / "hosttimes.npy", np.array([f[3] for f in frames], dtype=np.int64))
	write_meta(folder, cameraName=name, cameraMake="ratcam", frameRate=fps, cameraTrigger="None")
	return {n: 1 + s / T_TRUE for n, s in enumerate(starts, start=1)}


def aligned(root, name):
	with open(root / name / "aligned.csv") as f:
		return list(csv.DictReader(f))


def run(root):
	out = []
	summary = align.Run(str(root), log=out.append)
	return summary, "\n".join(out)


def test_every_camera_from_the_first_trigger(tmp_path):
	a = triggered_camera(tmp_path, "CamA", 1, 600, missed={100}, dropped={200, 201})
	b = triggered_camera(tmp_path, "CamB", 1, 600, ppm=-40, lag_ms=7, seed=2)
	eye = ratcam(tmp_path, "Eye", 1, 600)
	summary, text = run(tmp_path)

	assert abs(summary["trigger_period_s"] / T_TRUE - 1) < 1e-4
	for name, truth in (("CamA", a), ("CamB", b)):
		rows = aligned(tmp_path, name)
		assert {int(r["frame"]): int(r["trigger"]) for r in rows} == truth
		assert float(rows[-1]["t_s"]) == pytest.approx(599 * summary["trigger_period_s"])
	s = summary["cameras"]["CamA"]
	assert (s["triggers_without_frame"], s["dropped_by_campy"], s["missed_by_camera"]) == (3, 2, 1)
	assert (s["start_trigger"], s["start_from"], s["start_unsure"]) == (1, "arrival times", False)
	assert 5 < s["frame_lag_after_pulse_ms"] < 7 # what the made-up camera's frames took to arrive

	rows = aligned(tmp_path, "Eye")
	got = np.array([float(r["trigger"]) for r in rows])
	want = np.array([eye[int(r["frame"])] for r in rows])
	assert np.max(np.abs(got - want)) < 1e-3 # a thousandth of a frame period
	assert all(r["between_pulses"] == "1" for r in rows)
	assert "CamA" in text and "1 missed by the camera" in text


def test_cameras_that_started_after_the_trigger(tmp_path):
	# The trigger was running before anyone listened: CamA saw trigger 5 first,
	# the RatCam trigger 31, CamB trigger 417. Trigger 1 becomes CamA's first.
	a = triggered_camera(tmp_path, "CamA", 5, 900)
	# Two thirds of a period from trigger to arrival: rounding would put it a trigger late
	b = triggered_camera(tmp_path, "CamB", 417, 900, lag_ms=11, seed=3)
	eye = ratcam(tmp_path, "Eye", 31, 900, duration_s=14.5)
	summary, _ = run(tmp_path)

	starts = {name: s["start_trigger"] for name, s in summary["cameras"].items()}
	assert starts == {"CamA": 1, "CamB": 413, "Eye": 27}
	for name, truth in (("CamA", a), ("CamB", b)):
		assert {int(r["frame"]): int(r["trigger"]) for r in aligned(tmp_path, name)} == \
			{n: k - 4 for n, k in truth.items()}

	rows = aligned(tmp_path, "Eye")
	got = np.array([float(r["trigger"]) for r in rows])
	want = np.array([eye[int(r["frame"])] - 4 for r in rows])
	assert np.max(np.abs(got - want)) < 1e-3
	# Frames before the RatCam heard its first pulse still get a place, flagged
	early = [r for r in rows if r["between_pulses"] == "0"]
	assert early and all(float(r["trigger"]) < 27 for r in early)


def test_without_arrival_times_starts_are_assumed(tmp_path):
	# Recordings from before campy saved hosttimes.npy
	triggered_camera(tmp_path, "CamA", 1, 300, host=False)
	triggered_camera(tmp_path, "CamB", 1, 300, missed={50}, host=False)
	summary, text = run(tmp_path)
	assert summary["starts_from"] is None
	assert summary["cameras"]["CamB"]["start_from"] == "assumed"
	assert summary["cameras"]["CamB"]["missed_by_camera"] == 1
	assert "assumed: no arrival times" in text


def test_a_random_sync_train_is_not_a_trigger(tmp_path):
	rng = np.random.default_rng(4)
	times = np.cumsum(rng.uniform(0.4, 1.0, 20))
	ratcam(tmp_path, "Eye", 0, 0, pulse_times=list(times))
	summary, text = run(tmp_path)
	assert summary["cameras"]["Eye"]["aligned"] is False
	assert "not periodic" in text and "No camera could be put on a trigger timeline" in text
	assert "Trigger period" not in text
	assert not (tmp_path / "Eye" / "aligned.csv").exists()
	assert align.Main([str(tmp_path)]) == 1 # nothing aligned


def test_triggered_cameras_must_share_a_rate(tmp_path):
	triggered_camera(tmp_path, "CamA", 1, 100)
	triggered_camera(tmp_path, "CamB", 1, 100, frame_rate=30)
	with pytest.raises(align.AlignError, match="different frame rates"):
		run(tmp_path)


def test_command_line(tmp_path, capsys):
	triggered_camera(tmp_path, "CamA", 1, 100)
	assert align.Main([str(tmp_path)]) == 0
	assert json.loads((tmp_path / "alignment.json").read_text())["cameras"]["CamA"]["aligned"]
	assert "CamA" in capsys.readouterr().out
	assert align.Main([str(tmp_path / "CamA")]) == 1 # not a recording folder
	assert "no camera folders" in capsys.readouterr().err
