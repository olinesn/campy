"""campy-syncmatch: a recorder's sync rises matched to the trigger board's plan, at the board's rate whatever it is."""

import csv, json, os, shutil, subprocess

import numpy as np
import pytest

from campy import syncmatch

SYNC = dict(seed=987654321, width_us=10000, min_gap_ms=200, max_gap_ms=1000, frame_period_us=10000)


def recorder(sync, hours=1.0, rate=1.0, offset_s=7.5, wobble_ppm=0.0, sample_rate=30000, drop=0.0, extra=0, skip=0,
		seed=0):
	"""
	The sync line as a recorder with its own clock sees it: rise and fall times (s). rate: recorder s per board s;
	wobble_ppm: the rate wandering by that much over an hour (a temperature change); sample_rate: its sampling.
	drop: a fraction of pulses lost; extra: stray pulses added; skip: pulses before the recorder started.
	"""
	rng = np.random.default_rng(seed)
	up, down = syncmatch.SyncEdges(sync, hours * 3600e6)
	b_up, b_down = up / 1e6, down / 1e6

	def clock(b):
		return offset_s + rate * b + wobble_ppm * 1e-6 * 3600 / (2 * np.pi) * (1 - np.cos(2 * np.pi * b / 3600))

	r, f = clock(b_up), clock(b_down)
	keep = rng.random(len(r)) >= drop
	keep[:skip] = False
	r, f = r[keep], f[keep]
	if extra:
		stray = rng.uniform(r[0], r[-1], extra)
		r, f = np.r_[r, stray], np.r_[f, stray + 0.002] # 2 ms: not the sync pulse's width
	order = np.argsort(r)
	q = lambda t: np.round(t * sample_rate) / sample_rate
	return q(r[order]), q(f[order]), int(keep.sum())


def test_its_numbers_are_the_sketchs():
	# SplitMix64's first numbers from seed 0 (the reference implementation's), and the gaps from them
	assert [int(x) for x in syncmatch.SplitMix64(0, [1, 2, 3])] == [0xE220A8397B1DCDAF, 0x6E789E6AA1B965F4,
		0x06C45D188009454F]
	gaps = syncmatch.Gaps(1, 200, 1000, 1000)
	assert gaps.min() >= 200 and gaps.max() <= 1000 and len(set(gaps)) > 500
	assert np.array_equal(syncmatch.PlannedRises(1, 200, 1000, 10e6), np.cumsum(gaps)[np.cumsum(gaps) <= 10000] * 1000)


def test_the_hold_rule():
	# Camera edges at 0 and 5000 in each 10 ms period: an edge due within 50 us before one, or on it, waits until 10 us
	# after it; one just after a camera edge, or 51 us before, stays
	period = 10000
	t = np.array([5000, 4950, 4949, 5001, 10000, 9990, 20000, 123456])
	assert list(syncmatch.Hold(t, period)) == [10, 60, 0, 0, 10, 20, 10, 0]
	assert syncmatch.TriggerPeriodUs(100) == 10000 and syncmatch.TriggerPeriodUs(29.97) == 34482
	assert syncmatch.TriggerPeriodUs(0) is None


@pytest.mark.parametrize("rate, what", [
	(1 + 3e-6, "a crystal (3 ppm)"),
	(1 - 4e-5, "a crystal at its worst (40 ppm)"),
	(1.005, "a ceramic resonator (0.5%)"),
	(0.99, "an internal oscillator (1%)"),
	(1.025, "an internal oscillator (2.5%)"),
])
def test_matches_at_any_rate_within_the_search(rate, what):
	r, f, n = recorder(SYNC, rate=rate, drop=0.01, extra=5, skip=37)
	result = syncmatch.Match(r, SYNC, f)
	matched = result["pulse"] >= 0
	assert matched.sum() == n, what
	assert (~matched).sum() == 5 and not result["good"][~matched].any() # the strays: their width is wrong
	assert result["mode"] == "line"
	assert abs(result["rate"] - rate) < 1e-7 and abs(result["offset_s"] - 7.5) < 20e-6
	assert np.nanmax(np.abs(result["residual_us"])) < 20 # half a sample at 30 kHz, plus the board's few us
	# The pulses it lost: each one in the plan, inside the recording, that isn't there
	planned = result["planned_us"]
	lo = planned[result["pulse"][matched].min()]
	hi = planned[result["pulse"][matched].max()]
	inside = np.flatnonzero((planned >= lo) & (planned <= hi))
	assert len(result["missing"]) == len(inside) - n


def test_a_rate_beyond_the_search_is_not_matched():
	r, f, _ = recorder(SYNC, hours=0.2, rate=1.05)
	with pytest.raises(SystemExit, match="Too few sync pulses"):
		syncmatch.Match(r, SYNC, f)
	result = syncmatch.Match(r, SYNC, f, max_rate_error=0.06)
	assert abs(result["rate"] - 1.05) < 1e-7


def test_a_wandering_rate_gets_a_line_per_half_minute():
	# A resonator's rate moves with temperature: 100 ppm over an hour leaves one line tens of ms out
	r, f, n = recorder(SYNC, hours=2, rate=1.004, wobble_ppm=100)
	line = syncmatch.Match(r, SYNC, f, segment_s=0)
	assert np.nanmax(np.abs(line["residual_us"])) > 10000
	result = syncmatch.Match(r, SYNC, f)
	assert result["mode"] == "segments" and result["segment_s"] == 30
	assert (result["pulse"] >= 0).sum() == n and len(result["missing"]) == 0
	assert np.nanmax(np.abs(result["residual_us"])) < 35 # 17 us from the 30 kHz samples, the rest the curve
	# Board time to recorder time, between the pulses too
	b = np.linspace(100, 7000, 50)
	want = 7.5 + 1.004 * b + 100e-6 * 3600 / (2 * np.pi) * (1 - np.cos(2 * np.pi * b / 3600))
	assert np.abs(result["map"].ToRecorder(b) - want).max() < 60e-6
	assert np.abs(result["map"].ToBoard(want) - b).max() < 60e-6


def test_the_chronicrecord_picos_sync_file():
	# The Pico's sync_*.json has no frame period: no holds, edges on whole ms
	sync = dict(seed=5, width_us=10000, min_gap_ms=200, max_gap_ms=1000)
	r, f, n = recorder(sync, hours=0.5, rate=1 + 3.2e-6)
	result = syncmatch.Match(r, sync, f)
	assert (result["pulse"] >= 0).sum() == n


def test_the_command_reads_csv_and_npy_and_writes_frame_times(tmp_path, capsys):
	r, f, n = recorder(SYNC, hours=0.3, rate=1.006, sample_rate=25000, drop=0.02)
	(tmp_path / "sync.json").write_text(json.dumps(dict(SYNC, pin=7)))
	# Sample numbers, rise and fall, with a header line, as a DAQ might export them
	with open(tmp_path / "edges.csv", "w") as out:
		out.write("rise_sample,fall_sample\n")
		for a, b in zip(r, f):
			out.write("{},{}\n".format(int(round(a * 25000)), int(round(b * 25000))))
	summary, rows = syncmatch.Main([str(tmp_path / "edges.csv"), "--sync", str(tmp_path / "sync.json"),
		"--sample-rate", "25000", "--frames", "1000"])
	said = capsys.readouterr().out
	assert summary["matched"] == n and summary["extra"] == 0 and summary["fit"] == "line"
	assert abs(summary["drift_ppm"] - 6000) < 0.5
	assert "more than a crystal's error" in said
	with open(tmp_path / "edges_frames.csv") as f_:
		frames = list(csv.DictReader(f_))
	assert len(frames) == 1000
	# Frame k's trigger rises at k x 10 ms + 5 ms of board time
	assert abs(float(frames[10]["recorder_s"]) - (7.5 + 1.006 * 0.105)) < 50e-6
	written = list(csv.DictReader(open(tmp_path / "edges_syncmatch.csv")))
	assert sum(1 for row in written if row["note"].startswith("missing")) == summary["missing"] > 0
	assert json.loads((tmp_path / "edges_syncmatch.json").read_text())["matched"] == n
	# The rises alone, in seconds, as an array
	np.save(tmp_path / "rises.npy", r)
	summary, _ = syncmatch.Main([str(tmp_path / "rises.npy"), "--sync", str(tmp_path / "sync.json")])
	assert summary["matched"] == n


@pytest.mark.skipif(shutil.which("c++") is None and shutil.which("g++") is None and shutil.which("clang++") is None,
	reason="needs a C++ compiler")
def test_from_the_sketch_to_frame_times(sim):
	# trigger.ino's own sync edges (the simulated board), seen by a recorder whose clock runs 0.4% fast and started
	# 3.2 s after the board's time 0: campy-syncmatch puts each camera frame's trigger on the recorder's clock
	fps = 100
	command = "2,2,3,{},-1,0,7,{},10000,200,1000".format(fps, SYNC["seed"])
	out = subprocess.run([sim, str(int(60e6)), command], capture_output=True, text=True, check=True, timeout=60).stdout
	edges = {}
	for line in out.splitlines():
		if not line.startswith("serial "):
			t, pin, level = map(int, line.split())
			edges.setdefault(pin, []).append((t, level))
	camera = np.array([t for t, level in edges[2] if level == 1], float)
	t0 = camera[0] - 5000 # half a period before the first rise
	up = np.array([t for t, level in edges[7] if level == 1], float)
	down = np.array([t for t, level in edges[7][1:] if level == 0], float)
	n = min(len(up), len(down))
	rec = lambda board_us: np.round((-3.2 + 1.004 * board_us / 1e6) * 30000) / 30000
	result = syncmatch.Match(rec(up[:n] - t0), SYNC, rec(down[:n] - t0))
	assert (result["pulse"] >= 0).sum() == n
	_, frame_rec = syncmatch.FrameTimes(result, SYNC["frame_period_us"], len(camera))
	assert np.abs(frame_rec - (-3.2 + 1.004 * (camera - t0) / 1e6)).max() < 30e-6
