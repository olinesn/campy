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


def test_a_ratcam_log_cut_short_aligns_the_frames_it_has(tmp_path):
	# A recording that stopped without closing: frametimes.npy holds frames
	# that ratcam_frames.csv had not written yet (each writes about every second)
	triggered_camera(tmp_path, "CamA", 1, 600)
	eye = ratcam(tmp_path, "Eye", 1, 600)
	path = tmp_path / "Eye" / "ratcam_frames.csv"
	with open(path, newline="") as f:
		rows = list(csv.reader(f))
	assert len(rows) == 1 + 198
	write_csv(path, rows[0], rows[1:151])
	summary, text = run(tmp_path)

	rows = aligned(tmp_path, "Eye")
	assert [int(r["frame"]) for r in rows] == list(range(1, 151))
	got = np.array([float(r["trigger"]) for r in rows])
	want = np.array([eye[int(r["frame"])] for r in rows])
	assert np.max(np.abs(got - want)) < 1e-3
	assert summary["cameras"]["Eye"]["frames"] == 150
	assert "its last 48 frames in frametimes.npy are not in ratcam_frames.csv" in text
	assert summary["cameras"]["CamA"]["warnings"] == []


@pytest.mark.parametrize("log", ["header only", "empty", "no frametimes"])
def test_a_ratcam_with_no_frames_logged_yet(tmp_path, log):
	# Cut off before ratcam_frames.csv got its first batch of rows (empty: its
	# header was still in campy's buffer), or before frametimes.npy did (it is
	# made, empty, with the first batch), while ratcam_sync.csv had pulses
	a = triggered_camera(tmp_path, "CamA", 5, 600)
	ratcam(tmp_path, "Eye", 1, 600)
	path = tmp_path / "Eye" / "ratcam_frames.csv"
	if log == "no frametimes":
		np.save(tmp_path / "Eye" / "frametimes.npy", np.zeros((2, 0)))
		np.save(tmp_path / "Eye" / "hosttimes.npy", np.zeros(0, np.int64))
	else:
		path.write_text(path.read_text().splitlines()[0] + "\n" if log == "header only" else "")
	summary, text = run(tmp_path)

	s = summary["cameras"]["Eye"]
	assert (s["aligned"], s["frames"], s["first_trigger"], s["pulses"]) == (True, 0, None, 600)
	assert "Eye   ratcam  no frames; 600 pulses" in text
	assert s["warnings"] == ["{} (the recording stopped without closing?): no frames to align".format(
		"frametimes.npy has no frames" if log == "no frametimes" else
		"none of its 198 frames in frametimes.npy are in ratcam_frames.csv")]
	# Its pulses still place the other camera
	assert summary["starts_from"] == "Eye"
	assert {int(r["frame"]): int(r["trigger"]) for r in aligned(tmp_path, "CamA")} == a
	assert aligned(tmp_path, "Eye") == []


@pytest.mark.parametrize("hosttimes", ["a batch short", "longer"])
def test_arrival_times_that_do_not_match_the_frames(tmp_path, hosttimes):
	# frametimes.FrameTimes writes frametimes.npy, then hosttimes.npy: killed
	# between the two, hosttimes.npy is a batch (about a second of frames)
	# short. Killed as Close(keep) cuts the two to what the video took, longer
	triggered_camera(tmp_path, "CamA", 1, 600)
	b = triggered_camera(tmp_path, "CamB", 31, 600, seed=2)
	path = tmp_path / "CamB" / "hosttimes.npy"
	host = np.load(path)
	np.save(path, host[:-60] if hosttimes == "a batch short" else np.concatenate([host, host[-5:] + 10**9]))
	summary, text = run(tmp_path)

	s = summary["cameras"]["CamB"]
	assert (s["start_trigger"], s["start_from"], s["start_unsure"]) == (31, "arrival times", False)
	assert {int(r["frame"]): int(r["trigger"]) for r in aligned(tmp_path, "CamB")} == b # every frame
	if hosttimes == "a batch short":
		assert s["warnings"] == ["hosttimes.npy has arrival times for 510 of its 570 frames (the recording "
			"stopped without closing?): its start is worked out from those"]
	else:
		assert s["warnings"] == ["hosttimes.npy has 5 more arrival times than frametimes.npy has frames, "
			"and they are left out"]
	assert "assumed" not in text


def test_no_arrival_times_yet(tmp_path):
	# Killed after frametimes.npy got its first batch, before hosttimes.npy did
	triggered_camera(tmp_path, "CamA", 1, 600)
	triggered_camera(tmp_path, "CamB", 31, 600, seed=2)
	np.save(tmp_path / "CamB" / "hosttimes.npy", np.zeros(0, np.int64))
	summary, text = run(tmp_path)
	s = summary["cameras"]["CamB"]
	assert (s["start_from"], s["warnings"]) == ("assumed", ["hosttimes.npy has arrival times for 0 of its 570 "
		"frames (the recording stopped without closing?)"])
	assert "CamB  flir    570 frames, triggers 1-570; started at trigger 1 (assumed: no arrival times)" in text


def test_sync_pulses_out_of_order(tmp_path):
	# ratcam_sync.csv is put in order of time only when the recording closes.
	# Out of order, each step back counted as triggers without a pulse, and the
	# first row as the RatCam's first pulse
	a = triggered_camera(tmp_path, "CamA", 1, 600)
	eye = ratcam(tmp_path, "Eye", 1, 600)
	path = tmp_path / "Eye" / "ratcam_sync.csv"
	with open(path, newline="") as f:
		header, *rows = list(csv.reader(f))
	rows[0], rows[1] = rows[1], rows[0]
	for i in range(30, len(rows) - 4, 60): # one pulse in 60 written three later
		rows.insert(i + 3, rows.pop(i))
	write_csv(path, header, rows)
	summary, text = run(tmp_path)

	s = summary["cameras"]["Eye"]
	assert (s["start_trigger"], s["triggers_without_pulse"], s["frames_outside_pulses"], s["warnings"]) == \
		(1, 0, 0, [])
	rows = aligned(tmp_path, "Eye")
	got = np.array([float(r["trigger"]) for r in rows])
	want = np.array([eye[int(r["frame"])] for r in rows])
	assert np.max(np.abs(got - want)) < 1e-3
	assert {int(r["frame"]): int(r["trigger"]) for r in aligned(tmp_path, "CamA")} == a


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


def test_a_short_last_window_does_not_tilt_the_clock():
	# 1151 pulses: the last window of WINDOW arrivals has one, and it waited
	# 95 ms behind a frame on USB
	dev = 123.4 + np.arange(1151) * T_TRUE
	host = HOST0 + (dev - 123.4) * 1e9 + 0.3e6
	host[-1] += 95e6
	assert align.ClockMap(dev, host)(dev[0]) == pytest.approx(HOST0 + 0.3e6, abs=1e3) # within 1 us


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


# ------------------------------------------------------------------ trigger.json

def trigger_record(root, ready, started=True):
	# What campy writes when it starts the trigger itself (trigger.SaveStart)
	(root / "trigger.json").write_text(json.dumps({"triggerStarted": started, "startedBy": "campy",
		"readyBeforeTrigger": sorted(ready) if started else [], "notReady": {}, "startedHostNs": HOST0 - 2_000_000}))


def test_ready_cameras_start_at_one_where_arrival_times_would_round_wrong(tmp_path):
	# Both cameras saw trigger 1, but CamB's frames take 9 ms (0.54 periods) longer to arrive
	a = triggered_camera(tmp_path, "CamA", 1, 300, lag_ms=6)
	b = triggered_camera(tmp_path, "CamB", 1, 300, lag_ms=15, seed=5)
	eye = ratcam(tmp_path, "Eye", 1, 300, duration_s=4.9)

	# Arrival times alone put CamB a trigger late, and are sure of it: against the RatCam's
	# pulses, 0.9 periods from pulse to arrival looks like a frame of the next trigger
	summary, _ = run(tmp_path)
	assert summary["cameras"]["CamB"]["start_trigger"] == 2

	trigger_record(tmp_path, {"CamA", "CamB", "Eye"})
	summary, text = run(tmp_path)
	assert summary["ready_before_trigger"] == ["CamA", "CamB", "Eye"]
	for name in ("CamA", "CamB", "Eye"):
		s = summary["cameras"][name]
		assert (s["start_trigger"], s["start_from"], s["start_unsure"]) == (1, "ready before the trigger", False)
	for name, truth in (("CamA", a), ("CamB", b)):
		assert {int(r["frame"]): int(r["trigger"]) for r in aligned(tmp_path, name)} == truth
	rows = aligned(tmp_path, "Eye")
	assert max(abs(float(r["trigger"]) - eye[int(r["frame"])]) for r in rows) < 1e-3
	assert "Trigger 1 is the trigger's first pulse" in text
	assert "grabbing before the trigger started" in text and "off by one" not in text


def test_a_ready_camera_that_clearly_missed_its_first_triggers(tmp_path):
	triggered_camera(tmp_path, "CamA", 1, 300)
	b = triggered_camera(tmp_path, "CamB", 3, 300, seed=6) # its first frame is trigger 3
	trigger_record(tmp_path, {"CamA", "CamB"})
	summary, text = run(tmp_path)
	s = summary["cameras"]["CamB"]
	assert (s["start_trigger"], s["start_from"]) == (3, "arrival times")
	assert {int(r["frame"]): int(r["trigger"]) for r in aligned(tmp_path, "CamB")} == b
	assert "it missed its first 2" in text


def test_a_camera_that_was_not_ready_is_placed_by_arrival_times(tmp_path):
	triggered_camera(tmp_path, "CamA", 1, 300)
	triggered_camera(tmp_path, "CamB", 40, 300, seed=7) # opened late: campy started without it
	trigger_record(tmp_path, {"CamA"})
	summary, _ = run(tmp_path)
	assert summary["cameras"]["CamA"]["start_from"] == "ready before the trigger"
	assert (summary["cameras"]["CamB"]["start_trigger"], summary["cameras"]["CamB"]["start_from"]) == \
		(40, "arrival times")


def test_ready_cameras_without_arrival_times(tmp_path):
	triggered_camera(tmp_path, "CamA", 1, 300, host=False)
	triggered_camera(tmp_path, "CamB", 1, 300, host=False)
	trigger_record(tmp_path, {"CamA", "CamB"})
	summary, text = run(tmp_path)
	assert summary["cameras"]["CamB"]["start_from"] == "ready before the trigger"
	assert "assumed" not in text


def test_a_trigger_campy_did_not_start_changes_nothing(tmp_path):
	triggered_camera(tmp_path, "CamA", 1, 300)
	triggered_camera(tmp_path, "CamB", 1, 300, seed=8)
	trigger_record(tmp_path, {"CamA", "CamB"}, started=False) # an external trigger
	summary, _ = run(tmp_path)
	assert summary["ready_before_trigger"] is None
	assert summary["cameras"]["CamB"]["start_from"] == "arrival times"


@pytest.mark.parametrize("lag_ms, unsure", [(3, False), (9, False), (13, True)])
def test_a_ratcam_that_was_not_ready_against_ready_triggered_cameras(tmp_path, lag_ms, unsure):
	# The triggered cameras' frames arrive lag_ms after their trigger (0.18, 0.54
	# and 0.78 of a period at 60 Hz), its pulses 0.3 ms after. Rounded to
	# nearest, the RatCam came out a trigger early from 0.5 of a period on, and
	# from 0.7 on was sure of it
	a = triggered_camera(tmp_path, "CamA", 1, 1200, lag_ms=lag_ms)
	triggered_camera(tmp_path, "CamB", 1, 1200, lag_ms=lag_ms, seed=3)
	eye = ratcam(tmp_path, "Eye", 50, 1199, start_s=trigger_s(50) + 0.003, duration_s=15)
	trigger_record(tmp_path, {"CamA", "CamB"})
	summary, text = run(tmp_path)

	s = summary["cameras"]["Eye"]
	assert (s["start_trigger"], s["start_from"], s["start_unsure"]) == (50, "arrival times", unsure)
	rows = aligned(tmp_path, "Eye")
	assert max(abs(float(r["trigger"]) - eye[int(r["frame"])]) for r in rows) < 1e-3
	assert {int(r["frame"]): int(r["trigger"]) for r in aligned(tmp_path, "CamA")} == a
	# What placed it: the reference's frames, this long after the RatCam's pulses
	ref = summary["cameras"][summary["starts_from"]]
	assert ref["frame_lag_after_pulse_ms"] == pytest.approx(lag_ms - 0.3, abs=0.2)
	assert ("off by one" in text) == unsure


def test_placements_the_rounding_already_got_right(tmp_path):
	# A ready RatCam places the triggered cameras that were not, rounding down
	# (CamC's frames take 0.78 of a period: could be off by one); between
	# triggered cameras, to nearest. Both as before
	with_ratcam, triggered = tmp_path / "with_ratcam", tmp_path / "triggered"
	with_ratcam.mkdir()
	triggered.mkdir()
	triggered_camera(with_ratcam, "CamA", 20, 900, lag_ms=3)
	b = triggered_camera(with_ratcam, "CamB", 300, 900, lag_ms=9, seed=9)
	c = triggered_camera(with_ratcam, "CamC", 41, 900, lag_ms=13, seed=10)
	ratcam(with_ratcam, "Eye", 1, 900, duration_s=14.5)
	trigger_record(with_ratcam, {"Eye"})
	summary, _ = run(with_ratcam)
	got = {name: (s["start_trigger"], s["start_unsure"]) for name, s in summary["cameras"].items()}
	assert got == {"CamA": (20, False), "CamB": (300, False), "CamC": (41, True), "Eye": (1, False)}
	for name, truth in (("CamB", b), ("CamC", c)):
		assert {int(r["frame"]): int(r["trigger"]) for r in aligned(with_ratcam, name)} == truth

	triggered_camera(triggered, "CamA", 1, 900, lag_ms=9, seed=11)
	b = triggered_camera(triggered, "CamB", 300, 900, lag_ms=9, seed=9)
	trigger_record(triggered, {"CamA"})
	summary, _ = run(triggered)
	s = summary["cameras"]["CamB"]
	assert (s["start_trigger"], s["start_from"], s["start_unsure"]) == (300, "arrival times", False)
	assert {int(r["frame"]): int(r["trigger"]) for r in aligned(triggered, "CamB")} == b


def test_a_trigger_pause_is_told_apart_from_a_camera_missing_pulses(tmp_path):
	pause = set(range(300, 368)) # 68 pulses no camera got a frame for
	a = triggered_camera(tmp_path, "CamA", 1, 900, missed=pause | {500}, dropped={600, 601})
	b = triggered_camera(tmp_path, "CamB", 1, 900, missed=pause, ppm=-40, lag_ms=7, seed=2)
	c = triggered_camera(tmp_path, "CamC", 1, 900, missed=pause | {350, 700}, seed=3) # 350: inside the pause anyway
	summary, text = run(tmp_path)
	for name, truth in (("CamA", a), ("CamB", b), ("CamC", c)):
		assert {int(r["frame"]): int(r["trigger"]) for r in aligned(tmp_path, name)} == truth
	(p,) = summary["trigger_pauses"]
	assert (p["first_trigger"], p["triggers"]) == (300, 68)
	assert p["seconds"] == pytest.approx(68 * summary["trigger_period_s"])
	assert p["at_s"] == pytest.approx(299 * summary["trigger_period_s"])
	got = {n: (s["triggers_without_frame"], s["dropped_by_campy"], s["in_trigger_pauses"], s["missed_by_camera"])
		for n, s in summary["cameras"].items()}
	assert got == {"CamA": (71, 2, 68, 1), "CamB": (68, 0, 68, 0), "CamC": (69, 0, 68, 1)}
	assert "The trigger paused 1 time, 68 triggers (1.13 s) in all, with no frame in any camera: 68 triggers " \
		"(1.13 s) from trigger 300 (4.98 s in)." in text
	assert "71 triggers without a frame (2 dropped by campy, 68 while the trigger paused, 1 missed by the camera)" in text


def test_frames_campy_dropped_in_every_camera_are_no_pause(tmp_path):
	# The encoders fell behind together: the cameras sent those frames, so the trigger did not pause
	for n, name in enumerate(("CamA", "CamB")):
		triggered_camera(tmp_path, name, 1, 600, dropped=set(range(200, 230)), seed=n)
	summary, text = run(tmp_path)
	assert summary["trigger_pauses"] == [] and "paused" not in text
	assert all(s["dropped_by_campy"] == 30 and s["missed_by_camera"] == 0 for s in summary["cameras"].values())


def test_one_camera_can_not_tell_a_pause(tmp_path):
	triggered_camera(tmp_path, "CamA", 1, 600, missed=set(range(300, 368)))
	summary, text = run(tmp_path)
	assert summary["trigger_pauses"] == []
	assert summary["cameras"]["CamA"]["missed_by_camera"] == 68 and "paused" not in text
