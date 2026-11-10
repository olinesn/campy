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


def write_obx(folder, t, seconds, first=0, bout=None, green=(0.02, 1.0, 0.03), red=(-0.01, 0.06, 0.5), hum=0.05):
	"""
	One OneBox file. bout: the camera starting 1 s in at 60 Hz for 600 pulses,
	yellow 50 ms from 1 ms before pulse 300, 150 ms dark, blue 50 ms. green and
	red: (dark, in blue, in yellow) in volts, plus 60 Hz hum and a little noise.
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
		y_on = cam + 300 * 10**6 // 60 - 1000
		spans = {"yellow": (us(y_on), us(y_on + 50000)), "blue": (us(y_on + 200000), us(y_on + 250000))}
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
