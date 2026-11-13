"""chronicrecord/sync_match.py: the Pico's sync pulses found in made-up OneBox files and matched to its plan."""

import json, os, re, sys

import numpy as np
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "chronicrecord"))
import chronic_control as cc
import sync_match as sm

RATE = 30303.0 # the OneBox's obSampRate
# XA0 (gate), XA1 (sync) and XD0 only: the matcher finds its columns by name
META = """nSavedChans=3
obAiRangeMax=5
obAiRangeMin=-5
obMaxInt=32768
obSampRate=30303
firstSample={first}
typeThis=obx
~snsChanMap=(2,0,1)(XA0;0:0)(XA1;1:1)(XD0;2:2)
"""
# A 3-bout session: bout 1 with 4 s of camera, the others 3 s, a bout every 20 s
SETTINGS = ["TOTAL_DURATION=60", "INTERVAL=20", "TOTAL_BASELINE=2", "CAMERA_LEAD_IN=1", "STANDARD_STIM_DURATION=3",
	"EXTENDED_STIM_DURATION=4"]
SEED = 31337


def make_session(folder, settings=SETTINGS, seed=SEED, ppm=3.2, offset_s=50.0, drop=(), stray_camera=(), glitch=(),
		starts=None, bouts=None):
	"""
	A session's OneBox files as SpikeGLX writes them on the rig: the gate
	check's blink, then one file per bout from 1 s before its gate to 1 s
	after, on a OneBox clock offset_s ahead of the board's time 0 and ppm fast.
	drop: sync pulses (k) the OneBox didn't see; stray_camera: camera rises
	(board us) the Pico didn't plan; glitch: extra sync pulses (on, off);
	starts: {bout index: board us its file starts at, instead}; bouts: the
	bouts recorded (default all). Returns the plan's sync rises.
	"""
	s = cc.Settings(settings + ["SYNC=1", "SYNC_SEED={}".format(seed)])
	plan, trains, edges = cc.Plan(s)
	end = plan[-1]["gate_off"]
	sync = sm.PlannedRises(seed, 200, 1000, end)
	lines = {1: [(r, r + 10000) for k, r in enumerate(sync) if k not in drop] + list(glitch)}
	lines[2] = [(r, r + 3000) for ch, start, count, rate, width in trains for r in
		start + np.arange(count, dtype=np.int64) * 10**9 // rate] + [(r, r + 3000) for r in stray_camera]
	for bit, ch in ((0, cc.GATE), (3, cc.BLUE), (4, cc.YELLOW)):
		on = [t for t, c, level in edges if c == ch and level == 1]
		off = [t for t, c, level in edges if c == ch and level == 0]
		lines[bit] = list(zip(on, off))
	clock = lambda t_us: offset_s * RATE + t_us * 1e-6 * RATE * (1 + ppm * 1e-6)
	run = os.path.join(str(folder), "spikeglx", "chronic_test_g0")
	os.makedirs(run, exist_ok=True)
	spans = [(-31000000, -29000000, {0: [(-30000000, -29500000)]})] # the gate check's blink, before the session
	for i, b in enumerate(plan):
		if bouts is None or i in bouts:
			spans.append(((starts or {}).get(i, b["gate_on"] - 1000000), b["gate_off"] + 1000000, lines))
	for t, (start, stop, pulses) in enumerate(spans):
		first = int(np.floor(clock(start)))
		n = int(np.floor(clock(stop))) - first
		xd = np.zeros(n, np.int32)
		for bit, pairs in pulses.items():
			for on, off in pairs:
				a, z = int(np.ceil(clock(on))) - first, int(np.ceil(clock(off))) - first
				if z > 0 and a < n:
					xd[max(a, 0):min(z, n)] |= 1 << bit
		data = np.zeros((n, 3), np.int16)
		data[:, 0] = (xd & 1) * int(3.1 / 5 * 32768)
		data[:, 1] = (xd >> 1 & 1) * int(3.0 / 5 * 32768)
		data[:, 2] = xd
		name = os.path.join(run, "chronic_test_g0_t{}.obx0.obx".format(t))
		data.tofile(name + ".bin")
		with open(name + ".meta", "w") as f:
			f.write(META.format(first=first))
	day = os.path.join(str(folder), "pico", "2026-11-12")
	os.makedirs(day, exist_ok=True)
	with open(os.path.join(day, "sync_2026-11-12_10-00-00.json"), "w") as f:
		json.dump(dict(seed=seed, width_us=10000, min_gap_ms=200, max_gap_ms=1000, ends_us=end, settings=s), f)
	return sync, plan


def test_a_clean_session_lines_up(tmp_path, capsys):
	sync, plan = make_session(tmp_path)
	summary, rows = sm.Main([str(tmp_path)])
	out = capsys.readouterr().out
	assert summary["missing"] == 0 and summary["extra"] == 0
	assert summary["matched"] == len([r for r in rows if "pulse" in r]) > 30
	# A minute of pulses, each to the nearest sample (33 us), pins the rate to a fraction of a ppm (the hour below: 0.02)
	assert abs(summary["drift_ppm"] - 3.2) < 0.5
	assert abs(summary["onebox_s_at_board_0"] - 50.0) < 2 / RATE
	assert summary["residual_us_max"] < 40 # a sample is 33 us
	assert [f["anchored"] for f in summary["per_file"]] == [False, True, True, True] # not the blink
	# Every OneBox pulse is its planned one
	for r in rows:
		assert sync[r["pulse"]] / 1e6 == r["board_s"]
	# The other lines, put on the board's clock, are the plan's: nothing odd
	for name in ("camera", "gate", "blue", "yellow"):
		assert summary[name + "_odd"] == []
	assert "camera: 600 rises on the OneBox, 600 on the plan" in out
	assert (tmp_path / "sync_match.csv").is_file() and (tmp_path / "sync_match.json").is_file()
	# The same from the analog channel, read above 1.5 V
	summary2, _ = sm.Main([str(tmp_path), "--sync-xa", "XA1", "--no-lines"]) # its own sync_match.json is no sync file
	assert summary2["matched"] == summary["matched"] and summary2["missing"] == 0


def test_a_dropped_pulse_a_glitch_and_a_stray_camera_pulse(tmp_path):
	sync, plan = make_session(tmp_path)
	inside = [k for k, r in enumerate(sync) if plan[1]["gate_on"] < r < plan[1]["gate_off"]]
	k = inside[len(inside) // 2]
	stray = plan[2]["camera_start"] + 100 * 10**6 // 60 + 8000 # between two frames of bout 3
	make_session(tmp_path, drop=[k], glitch=[(sync[k + 1] + 50000, sync[k + 1] + 51000)], stray_camera=[stray])
	summary, rows = sm.Main([str(tmp_path)])
	(missing,) = [r for r in rows if r.get("note", "").startswith("missing")]
	assert missing["pulse"] == k and missing["board_s"] == sync[k] / 1e6
	(extra,) = [r for r in rows if r.get("note", "").startswith("extra")]
	assert extra["width_ms"] == pytest.approx(1.0, abs=0.07) and "not 10" in extra["note"]
	assert summary["missing"] == 1 and summary["extra"] == 1
	assert abs(summary["drift_ppm"] - 3.2) < 0.5 # one lost pulse moves nothing
	(odd,) = summary["camera_odd"]
	assert abs(float(re.search(r"board t=([\d.]+) s", odd).group(1)) - stray / 1e6) < 40e-6 and "didn't plan" in odd


def test_files_that_start_mid_sequence_and_mid_pulse(tmp_path):
	# Only bouts 2 and 3 recorded (SpikeGLX started late), and bout 2's file starting 3 ms into a sync pulse: the pulse
	# cut by the file's start is neither a rise nor missing, and the rest still find their place
	sync, plan = make_session(tmp_path)
	k = [k for k, r in enumerate(sync) if r > plan[1]["gate_on"] - 1000000][0]
	make_session(tmp_path, bouts=[1, 2], starts={1: int(sync[k]) + 3000})
	summary, rows = sm.Main([str(tmp_path)])
	assert summary["missing"] == 0 and summary["extra"] == 0
	pulses = [r["pulse"] for r in rows if "pulse" in r]
	assert min(pulses) == k + 1 and abs(summary["drift_ppm"] - 3.2) < 0.5


def test_clock_drift_over_an_hour(tmp_path):
	# 40 ppm (0.14 s over the hour), far more than the rig's 3.2: one offset can't place every bout, the fit does. Bout
	# 1 has 60 s of camera, so its file drifts 80 samples from end to end too
	settings = ["TOTAL_DURATION=3600", "INTERVAL=600", "TOTAL_BASELINE=2", "CAMERA_LEAD_IN=1", "STANDARD_STIM_DURATION=3",
		"EXTENDED_STIM_DURATION=60"]
	sync, plan = make_session(tmp_path, settings=settings, ppm=40.0, offset_s=12.5)
	summary, rows = sm.Main([str(tmp_path)])
	assert summary["missing"] == 0 and summary["extra"] == 0 and summary["files"] == 7
	assert abs(summary["drift_ppm"] - 40.0) < 0.02
	assert abs(summary["onebox_samples_per_board_s"] - RATE * (1 + 40e-6)) < 0.001
	assert summary["residual_us_max"] < 40
	assert summary["camera_odd"] == [] and summary["gate_odd"] == []


def test_the_plan_is_the_boards(tmp_path):
	# The gaps sync_match.py works out are the sketch's (tests/test_chronic_timer.py runs the sketch against them):
	# SplitMix64's published first outputs for seed 0, and gaps within their bounds
	assert [int(x) for x in sm.SplitMix64(0, [1, 2, 3])] == [0xE220A8397B1DCDAF, 0x6E789E6AA1B965F4, 0x06C45D188009454F]
	gaps = sm.Gaps(5, 200, 1000, 100000)
	assert gaps.min() == 200 and gaps.max() == 1000 and abs(gaps.mean() - 600) < 3


def test_no_sync_pulses_says_what_to_check(tmp_path):
	make_session(tmp_path, drop=range(10000))
	with pytest.raises(SystemExit, match="Is the sync line wired to the OneBox"):
		sm.Main([str(tmp_path), "--no-lines"])
