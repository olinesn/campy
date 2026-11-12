"""chronicrecord/photometry_snapshots.py: the photometry readout from made-up OneBox files."""

import os, sys

import numpy as np
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "chronicrecord"))
import photometry_snapshots as ps

RATE = 30303.0
NCH = 14 # XA0-XA11, XD0, SY0, as the rig's OneBox
META = """acqXaDwSy=12,1,1
nSavedChans=14
obAiRangeMax=5
obAiRangeMin=-5
obMaxInt=32768
obSampRate=30303
firstSample={first}
typeThis=obx
~snsChanMap=(12,1,1)(XA0;0:0)(XA1;1:1)(XA2;2:2)(XA3;3:3)(XA4;4:4)(XA5;5:5)(XA6;6:6)(XA7;7:7)(XA8;8:8)(XA9;9:9)(XA10;10:10)(XA11;11:11)(XD0;12:12)(SY0;13:13)
"""


def write_obx(folder, t, seconds, first=0, bout=None, green=(0.02, 1.0, 0.03), red=(-0.01, 0.06, 0.5), hum=0.05,
		width_us=50000, gap_us=150000, yellow_first=True):
	"""
	One OneBox file. bout: the camera starting 1 s in at 60 Hz for 600 pulses,
	yellow 50 ms from 1 ms before pulse 300, 150 ms dark, blue 50 ms (width_us,
	gap_us and yellow_first change that). green and red: (dark, in blue, in
	yellow) in volts, plus 60 Hz hum and a little noise.
	"""
	n = int(seconds * RATE)
	rng = np.random.default_rng(t)
	data = np.zeros((n, NCH), np.int16)
	xd = np.zeros(n, np.int32)
	us = lambda u: int(np.ceil(u * 1e-6 * RATE))
	xd[us(0.5e6):us(seconds * 1e6 - 0.5e6)] |= 1 # gate
	analog = {}
	if bout:
		cam = 1e6
		for k in range(600):
			r = cam + k * 10**6 // 60
			xd[us(r):us(r + 3000)] |= 1 << 2
		on = cam + 300 * 10**6 // 60 - 1000
		a, b = ("yellow", "blue") if yellow_first else ("blue", "yellow")
		spans = {a: (us(on), us(on + width_us)), b: (us(on + width_us + gap_us), us(on + 2 * width_us + gap_us))}
		xd[slice(*spans["blue"])] |= 1 << 3
		xd[slice(*spans["yellow"])] |= 1 << 4
		tt = np.arange(n) / RATE
		for col, (dark, in_blue, in_yellow) in ((5, green), (6, red)):
			x = dark + hum * np.sin(2 * np.pi * 60 * tt + col) + rng.normal(0, 0.002, n)
			x[slice(*spans["blue"])] += in_blue - dark
			x[slice(*spans["yellow"])] += in_yellow - dark
			analog[col] = x
	for col, x in analog.items():
		data[:, col] = np.clip(np.round(x / 5 * 32768), -32768, 32767).astype(np.int16)
	data[:, 12] = xd.astype(np.int16)
	name = os.path.join(folder, "chronic_test_g0_t{}.obx0.obx".format(t))
	data.tofile(name + ".bin")
	with open(name + ".meta", "w") as f:
		f.write(META.format(first=first))


def test_the_snapshot_readout(tmp_path, capsys):
	run = tmp_path / "spikeglx" / "chronic_test_g0"
	run.mkdir(parents=True)
	write_obx(str(run), 0, 2.0) # the gate check's blink: no camera
	write_obx(str(run), 1, 12.0, first=10 * 30303, bout=True)
	write_obx(str(run), 2, 12.0, first=400 * 30303, bout=True, green=(0.02, 0.8, 0.03))
	rows = ps.Main([str(tmp_path)])
	assert (tmp_path / "photometry_snapshots.csv").is_file()
	assert rows[0]["camera_pulses"] == 0 and "gate check" in rows[0]["note"]
	b1, b2 = rows[1], rows[2]
	assert (b1["bout"], b2["bout"]) == (1, 2)
	for b in (b1, b2):
		assert b["camera_pulses"] == 600 and b["order"] == "yellow first"
		assert (b["yellow_frames"], b["blue_frames"]) == ("300-302", "312-314")
		assert abs(b["yellow_ms"] - 50) <= 0.04 and abs(b["blue_ms"] - 50) <= 0.04 and abs(b["gap_ms"] - 150) <= 0.04
		assert abs(b["yellow_lead_us"] - 1000) <= 34 and abs(b["blue_lead_us"] - 1000) <= 34
		# The 50 mV hum cancels over a whole cycle: the levels to about a millivolt
		assert b["red_V"] == pytest.approx(0.5 - (-0.01), abs=0.002)
		assert b["green_in_yellow_V"] == pytest.approx(0.01, abs=0.002)
		assert b["red_in_blue_V"] == pytest.approx(0.07, abs=0.002)
		assert b["green_in_blue_half_rise_ms"] <= 0.034
		assert "clipped" not in b.get("note", "")
	assert b1["green_V"] == pytest.approx(0.98, abs=0.002) and b2["green_V"] == pytest.approx(0.78, abs=0.002)
	assert b2["gate_on_s"] - b1["gate_on_s"] == pytest.approx(390, abs=1e-4) # on the run's clock
	assert "300-302" in capsys.readouterr().out


def test_clipping_and_campys_frames(tmp_path):
	write_obx(str(tmp_path), 1, 12.0, bout=True, green=(0.0, 6.0, 0.0))
	cam = tmp_path / "Camera0"
	cam.mkdir()
	# Two bouts of frames, 600 then 600, 300 s apart; the OneBox has the first only
	t = np.r_[np.arange(600) / 60, 300 + np.arange(600) / 60]
	np.save(str(cam / "frametimes.npy"), np.vstack([np.arange(1200), t]))
	rows = ps.Main([str(tmp_path), "--campy", str(tmp_path), "--csv", str(tmp_path / "out.csv")])
	assert "green clipped in blue flash" in rows[0]["note"]
	assert rows[0]["Camera0_frames"] == 600
	assert (rows[0]["yellow_campy_frames"], rows[0]["blue_campy_frames"]) == ("300-302", "312-314")
	assert ps.CampyBouts(str(tmp_path)) == {"Camera0": [(0, 600), (600, 600)]}


def test_no_files(tmp_path):
	with pytest.raises(SystemExit, match="No OneBox files"):
		ps.Main([str(tmp_path)])


def one_bout(tmp_path, **kw):
	write_obx(str(tmp_path), 1, 12.0, bout=True, **kw)
	return ps.Main([str(tmp_path), "--csv", str(tmp_path / "out.csv")])[0]


@pytest.mark.parametrize("width_us, gap_us, yellow_first", [(50000, 150000, True), (30000, 0, False)])
def test_normal_flashes_read_as_before(tmp_path, width_us, gap_us, yellow_first):
	"""
	The panel's 50 ms snapshot pattern and chronic_control's own 30 ms one: a
	whole cycle ending 1 ms before each flash's end, the dark the cycle ending
	1 ms before the first flash, no note: the same numbers as before the cut.
	"""
	b = one_bout(tmp_path, width_us=width_us, gap_us=gap_us, yellow_first=yellow_first)
	assert "note" not in b
	meta, data = ps.ReadObx(str(tmp_path / "chronic_test_g0_t1.obx0.obx.bin"))
	xd = data[:, 12].astype(np.int32)
	cycle, guard = int(round(RATE / 60)), int(round(0.001 * RATE))
	for label, col in (("green", 5), ("red", 6)):
		x = data[:, col].astype(np.float64) * 5 / 32768
		flashes = {}
		for name, bit in (("blue", 3), ("yellow", 4)):
			on, off = ps.Edges((xd >> bit) & 1)
			flashes[name] = (int(on[0]), int(off[0]))
		start = min(on for on, _ in flashes.values())
		dark = x[start - guard - cycle:start - guard].mean()
		assert b["dark_{}_V".format(label)] == round(float(dark), 5)
		for name, (on, off) in flashes.items():
			level = x[off - guard - cycle:off - guard].mean()
			assert b["{}_in_{}_V".format(label, name)] == round(float(level) - float(dark), 5)
	assert b["green_V"] == pytest.approx(0.98, abs=0.002) and b["red_V"] == pytest.approx(0.51, abs=0.002)


def test_a_10_ms_flash_is_read_inside_the_flash(tmp_path):
	"""
	Blue on for 10 ms: a whole cycle back from its end would take ~7.7 ms of the
	dark before it (green_V about half the step). The window starts 1 ms after
	the LED's on edge instead, and the note says it was shorter than a cycle.
	"""
	b = one_bout(tmp_path, width_us=10000, hum=0.0)
	assert b["green_V"] == pytest.approx(0.98, abs=0.002)
	assert b["red_V"] == pytest.approx(0.51, abs=0.002)
	assert "blue flash: read over 8.0 ms, less than one 60 Hz cycle" in b["note"]
	assert "yellow flash: read over 8.0 ms" in b["note"]
	assert "dark" not in b["note"]


def test_a_10_ms_flash_right_after_the_other_takes_nothing_from_it(tmp_path):
	"""No dark between the LEDs: the second flash's window doesn't reach into the first's light."""
	b = one_bout(tmp_path, width_us=10000, gap_us=0, hum=0.0, green=(0.02, 1.0, 3.0), red=(-0.01, 2.0, 0.5))
	assert b["gap_ms"] == pytest.approx(0, abs=0.04)
	assert b["green_V"] == pytest.approx(0.98, abs=0.002) # blue second: none of green's 3 V in yellow
	assert b["red_V"] == pytest.approx(0.51, abs=0.002)
	assert b["red_in_blue_V"] == pytest.approx(2.01, abs=0.002)


def test_a_flash_one_cycle_long_is_read_and_flagged(tmp_path):
	"""
	16.7 ms, exactly one cycle: less a guard at each end, 14.7 ms are left. The
	value is given (the hum's leftover is within a few tens of millivolts here,
	50 mV of hum) and the note says the hum wasn't averaged out.
	"""
	b = one_bout(tmp_path, width_us=10**6 // 60)
	assert b["blue_ms"] == pytest.approx(1000 / 60, abs=0.04)
	assert b["green_V"] == pytest.approx(0.98, abs=0.03)
	assert "blue flash: read over 14.7 ms, less than one 60 Hz cycle (hum not averaged out)" in b["note"]
	# Without the hum it is exact
	b = one_bout(tmp_path, width_us=10**6 // 60, hum=0.0)
	assert b["green_V"] == pytest.approx(0.98, abs=0.002)


def test_a_flash_a_cycle_and_two_guards_long_is_a_whole_cycle(tmp_path):
	b = one_bout(tmp_path, width_us=19000)
	assert "note" not in b
	assert b["green_V"] == pytest.approx(0.98, abs=0.002)


def test_a_flash_too_short_to_read_is_blank(tmp_path, capsys):
	b = one_bout(tmp_path, width_us=1000)
	assert b["blue_ms"] == pytest.approx(1, abs=0.04)
	assert (b["green_V"], b["red_V"], b["green_in_blue_half_rise_ms"]) == ("", "", "")
	assert "blue flash: nothing to read 1 ms clear of the edges" in b["note"]
	assert isinstance(b["dark_green_V"], float)
	assert "green blank V, red blank V" in capsys.readouterr().out
	assert ",," in (tmp_path / "out.csv").read_text().splitlines()[1]


def write_aligned(folder, cam, triggers):
	d = folder / cam
	d.mkdir(exist_ok=True)
	with open(str(d / "aligned.csv"), "w") as f:
		f.write("frame,trigger,t_s\n")
		for i, k in enumerate(triggers):
			f.write("{},{},{:.6f}\n".format(i + 1, k, (k - 1) / 60))


def three_bouts(tmp_path):
	"""Three bouts 300 s apart on the run's clock, as the panel's gate test (each 600 pulses here)."""
	for t, at in ((1, 0), (2, 300), (3, 600)):
		write_obx(str(tmp_path), t, 12.0, first=int(round(at * RATE)), bout=True)


def test_campys_frames_come_from_aligned_csv_and_a_stray_frame_moves_nothing(tmp_path, capsys):
	three_bouts(tmp_path)
	bout = lambda first: list(range(first, first + 600))
	# Camera0: a frame between bouts 2 and 3 that the Pico didn't send, and campy stopping at the planned total, so
	# bout 3 has no frame for its last pulse (as on 10 November). Camera1: every pulse, nothing else, and its grid
	# puts bout 2 one trigger later than the OneBox's times do (the clocks' small difference)
	write_aligned(tmp_path, "Camera0", bout(1) + bout(18001) + [20000] + bout(36001)[:-1])
	write_aligned(tmp_path, "Camera1", bout(1) + bout(18002) + bout(36001))
	rows = ps.Main([str(tmp_path), "--campy", str(tmp_path), "--csv", str(tmp_path / "out.csv")])
	b = [r for r in rows if r.get("bout")]
	assert [(r["Camera0_frames"], r["Camera1_frames"]) for r in b] == [(600, 600), (600, 600), (599, 600)]
	assert [r["yellow_campy_frames"] for r in b[:2]] == ["300-302", "900-902"]
	assert [r["blue_campy_frames"] for r in b[:2]] == ["312-314", "912-914"]
	# Bout 3: after the stray frame, Camera0's video is one frame later than Camera1's
	assert b[2]["yellow_campy_frames"] == "Camera0 1501-1503; Camera1 1500-1502"
	assert b[2]["blue_campy_frames"] == "Camera0 1513-1515; Camera1 1512-1514"
	assert "Camera0: 1 frame outside the Pico's bouts (video frame 1200)" in capsys.readouterr().out


def test_without_aligned_csv_frametimes_as_before(tmp_path, capsys):
	three_bouts(tmp_path)
	cam = tmp_path / "Camera0"
	cam.mkdir()
	t = np.r_[np.arange(600) / 60, 300 + np.arange(600) / 60, 600 + np.arange(600) / 60]
	np.save(str(cam / "frametimes.npy"), np.vstack([np.arange(1800), t]))
	rows = ps.Main([str(tmp_path), "--campy", str(tmp_path), "--csv", str(tmp_path / "out.csv")])
	b = [r for r in rows if r.get("bout")]
	assert [r["yellow_campy_frames"] for r in b] == ["300-302", "900-902", "1500-1502"]
	assert "No aligned.csv" in capsys.readouterr().out
