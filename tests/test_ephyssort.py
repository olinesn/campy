"""
campy/ephyssort.py: the live sorter from a Kilosort sort. Synthetic units
(some on nearly the same sites, told apart by their shapes or footprints),
a phy folder as Kilosort writes one (their spike times and labels), trained
on the first seconds replayed as live and evaluated on the rest.
"""

import json, os

import numpy as np
import pytest

from campy import ephyslive as el, ephyssort as es
from test_ephyslive import RATE, UV, WriteBin

# (peak channel, trough uV, trough width ms, bump after it as a share of the trough, label)
UNITS = [(12, 200, 0.12, 0.35, "good"), (14, 140, 0.30, 0.15, "good"), # one site apart, different shapes
	(40, 180, 0.10, 0.50, "good"), (47, 180, 0.10, 0.50, "good"), # the same shape, 3 rows apart (45 um)
	(70, 160, 0.15, 0.30, "good"), (88, 220, 0.12, 0.35, "noise")]


def Shape(width_ms, bump):
	t = np.arange(61) / 30.0
	return (-np.exp(-0.5 * ((t - 20 / 30.0) / width_ms) ** 2) + bump * np.exp(-0.5 * ((t - 1.1) / 0.25) ** 2)).astype(
		np.float32)


def Recording(seconds=8.0, n=96, rate_hz=12.0, noise_uv=8.0, seed=5):
	rng = np.random.default_rng(seed)
	geometry = el.Geometry.Linear(n)
	T = int(seconds * RATE)
	x = rng.normal(0, noise_uv, (T, n)).astype(np.float32)
	times, clusters = [], []
	for u, (ch, amp, width, bump, _) in enumerate(UNITS):
		d = np.hypot(geometry.x - geometry.x[ch], geometry.z - geometry.z[ch])
		spread = np.exp(-d / 35.0).astype(np.float32)
		shape = Shape(width, bump)
		at = 0
		while True:
			at += int(RATE * (0.004 + rng.exponential(1 / rate_hz)))
			if at + 41 >= T:
				break
			x[at - 20:at + 41] += amp * shape[:, None] * spread[None, :]
			times.append(at)
			clusters.append(u)
	order = np.argsort(times)
	return (np.clip(np.round(x / UV), -32768, 32767).astype(np.int16), geometry, np.array(times)[order],
		np.array(clusters)[order])


def WritePhy(folder, geometry, times, clusters):
	os.makedirs(folder, exist_ok=True)
	np.save(os.path.join(folder, "spike_times.npy"), times.astype(np.uint64)[:, None])
	np.save(os.path.join(folder, "spike_clusters.npy"), clusters.astype(np.int32))
	np.save(os.path.join(folder, "channel_map.npy"), np.arange(len(geometry), dtype=np.int32))
	np.save(os.path.join(folder, "channel_positions.npy"), np.stack([geometry.x, geometry.z], axis=1))
	with open(os.path.join(folder, "cluster_KSLabel.tsv"), "w") as f:
		f.write("cluster_id\tKSLabel\n" + "".join("{}\tgood\n".format(u) for u in range(len(UNITS))))
	with open(os.path.join(folder, "cluster_group.tsv"), "w") as f: # curated in phy: one unit called noise
		f.write("cluster_id\tgroup\n" + "".join("{}\t{}\n".format(u, UNITS[u][4]) for u in range(len(UNITS))))
	return folder


@pytest.fixture(scope="module")
def sorted_recording(tmp_path_factory):
	tmp = tmp_path_factory.mktemp("sort")
	data, geometry, times, clusters = Recording()
	bin_path = WriteBin(str(tmp), data, geometry, first_sample=0)
	phy = WritePhy(str(tmp / "kilosort4"), geometry, times, clusters)
	return tmp, bin_path, phy, times, clusters


def test_phy_s_labels_curated_or_kilosort_s(sorted_recording):
	tmp, bin_path, phy, times, clusters = sorted_recording
	sort = es.ReadPhy(phy)
	assert sort["label_file"] == "cluster_group.tsv" and sort["kept"] == [0, 1, 2, 3, 4] # phy's: unit 5 is noise
	assert es.ReadPhy(phy, ("good", "noise"))["kept"] == [0, 1, 2, 3, 4, 5]
	os.rename(os.path.join(phy, "cluster_group.tsv"), os.path.join(phy, "cluster_group.tsv.off"))
	try:
		sort = es.ReadPhy(phy)
		assert sort["label_file"] == "cluster_KSLabel.tsv" and sort["kept"] == [0, 1, 2, 3, 4, 5] # Kilosort's: all good
	finally:
		os.rename(os.path.join(phy, "cluster_group.tsv.off"), os.path.join(phy, "cluster_group.tsv"))


def test_trained_on_the_first_seconds_it_sorts_the_rest(sorted_recording, capsys):
	tmp, bin_path, phy, times, clusters = sorted_recording
	out = str(tmp / "templates.npz")
	assert el.Main(["train", "--replay", bin_path, "--phy", phy, "--out", out, "--span", ":5"]) == 0
	sorter = es.TemplateSorter(out)
	assert sorted(sorter.units.tolist()) == [0, 1, 2, 3, 4] # not the unit phy calls noise
	assert sorter.templates.shape[1:] == (61, el.NEIGHBORS)
	assert [int(np.argmin(t[:, 0])) for t in sorter.templates] == [el.BEFORE] * 5 # each trough at 20, on its peak site
	assert {int(u): int(p) for u, p in zip(sorter.units, sorter.peaks)} == {0: 12, 1: 14, 2: 40, 3: 47, 4: 70}
	report = tmp / "evaluate.json"
	assert el.Main(["evaluate", "--replay", bin_path, "--phy", phy, "--templates", out, "--span", "5:",
		"--json", str(report)]) == 0
	summary = json.loads(report.read_text())
	per = {r["unit"]: r for r in summary["per_unit"]}
	for u in range(5):
		assert per[u]["kilosort_spikes"] > 20
		# The sorter: of each unit's spikes the spike finder found, nearly all given to it; and few others given to it
		assert per[u]["found_and_given"] >= 0.9 * per[u]["found_live"], per[u]
		assert per[u]["precision"] >= 0.85, per[u]
		# Overall: also what the spike finder missed (unit 1's broad trough is small once high-passed, and one site
		# from unit 0's)
		assert per[u]["sensitivity"] >= 0.75, per[u]
	assert summary["sensitivity_median"] >= 0.9 and summary["precision_median"] >= 0.95
	assert summary["live_spikes_of_a_unit_given_it"] >= 0.9
	# The noise unit's spikes (phy says noise): left unsorted, not taken for a kept unit
	assert summary["live_spikes_of_no_unit_given_one"] <= 0.1
	assert "Sorted" in capsys.readouterr().out


def test_the_live_engine_sorts_with_it(sorted_recording):
	# The sorter in the live engine (pieces of 50 ms, as from SpikeGLX): units on the spikes, their rates and
	# snippets in the view
	tmp, bin_path, phy, times, clusters = sorted_recording
	out = str(tmp / "templates_live.npz")
	assert el.Main(["train", "--replay", bin_path, "--phy", phy, "--out", out, "--span", ":5"]) == 0
	source = el.ReplaySource(bin_path, speed=0, chunk_s=0.05, start_s=5.0)
	engine = el.Engine(source, sorter=es.TemplateSorter(out))
	engine.Start()
	while source.state != "ended":
		engine.Step()
	snap = engine.live.Snapshot(snippet_units=[0, 1, 2, 3, 4])
	units = np.array(snap["spikes"]["unit"])
	assert set(units[units >= 0].tolist()) == {0, 1, 2, 3, 4}
	assert all(snap["rates"]["unit"][u] > 3 for u in range(5)) # about 12 a second each
	assert all(len(snap["snippets"]["unit"][u][0]) == 61 for u in range(5))
	assert [u["unit"] for u in engine.sorter.Units()] == [0, 1, 2, 3, 4]


def test_serve_sorts_with_the_templates_and_says_its_units(sorted_recording):
	# campy-ephys serve --templates (what the panel starts with spikeglx.templates): the page gets the units, and
	# the spikes' units
	import subprocess, sys, time, urllib.request
	tmp, bin_path, phy, times, clusters = sorted_recording
	out = str(tmp / "templates_serve.npz")
	assert el.Main(["train", "--replay", bin_path, "--phy", phy, "--out", out, "--span", ":5"]) == 0
	env = dict(os.environ, CAMPY_EPHYS_KEY="k3y", PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(el.__file__))))
	proc = subprocess.Popen([sys.executable, "-m", "campy.ephyslive", "serve", "--replay", bin_path, "--speed", "4",
		"--templates", out, "--idle-s", "20", "--parent", str(os.getpid())], env=env, stdout=subprocess.PIPE, text=True)
	try:
		port = int(proc.stdout.readline().split()[1])
		ask = lambda path: json.loads(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:{}{}".format(port,
			path), headers={"X-Ephys-Key": "k3y"}), timeout=10).read())
		deadline, snap = time.monotonic() + 30, {}
		while time.monotonic() < deadline:
			snap = ask("/snapshot?stream=1")
			if snap.get("live") and any(u >= 0 for u in snap["live"]["spikes"]["unit"]):
				break
			time.sleep(0.3)
		assert [u["unit"] for u in snap["stream"]["units"]] == [0, 1, 2, 3, 4]
		assert snap["stream"]["sorter"].startswith("Kilosort 4 templates")
		assert set(u for u in snap["live"]["spikes"]["unit"] if u >= 0) <= {0, 1, 2, 3, 4}
		assert snap["live"]["rates"]["unit"]
	finally:
		proc.terminate()
		proc.wait(timeout=10)
