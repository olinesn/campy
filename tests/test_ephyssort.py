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
	assert sorter.templates.shape[1:] == (61, es.TEMPLATE_SITES) # a spike's whole footprint, for the matcher
	assert (sorter.near_sites == sorter.sites[:, :el.NEIGHBORS]).all() # the threshold's spikes: the nearest 12
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
		# Unit by unit (its Kilosort spikes and those given to it, paired one to one): no more than either count,
		# and here about as above (one unit's spike at a time)
		assert per[u]["own_paired"] <= min(per[u]["kilosort_spikes"], per[u]["given"])
		assert abs(per[u]["own_sensitivity"] - per[u]["sensitivity"]) <= 0.05, per[u]
	assert summary["sensitivity_median"] >= 0.9 and summary["precision_median"] >= 0.95
	assert summary["own_sensitivity_median"] >= 0.9 and summary["own_precision_median"] >= 0.95
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


def test_the_templates_find_a_unit_below_the_threshold(tmp_path):
	# A small unit (45 uV, about 25 uV once filtered: under the threshold's 4.5 x 8 uV) beside the others: the
	# threshold's spikes miss most of it, the templates matched find it
	global UNITS
	saved = UNITS
	UNITS = saved[:5] + [(88, 45, 0.15, 0.30, "good")]
	try:
		data, geometry, times, clusters = Recording(seconds=12.0)
	finally:
		UNITS = saved
	bin_path = WriteBin(str(tmp_path), data, geometry, first_sample=0)
	phy = WritePhy(str(tmp_path / "kilosort4"), geometry, times, clusters)
	with open(os.path.join(phy, "cluster_group.tsv"), "w") as f:
		f.write("cluster_id\tgroup\n" + "".join("{}\tgood\n".format(u) for u in range(6)))
	out = str(tmp_path / "templates.npz")
	assert el.Main(["train", "--replay", bin_path, "--phy", phy, "--out", out, "--span", ":8"]) == 0
	assert 5 in es.TemplateSorter(out).units.tolist() # a template for it too, from Kilosort's times
	results = {}
	for name, extra in (("threshold", []), ("matched", ["--match", "7"])):
		report = tmp_path / "{}.json".format(name)
		assert el.Main(["evaluate", "--replay", bin_path, "--phy", phy, "--templates", out, "--span", "8:", "--json",
			str(report)] + extra) == 0
		results[name] = {r["unit"]: r for r in json.loads(report.read_text())["per_unit"]}
	small_thr, small = results["threshold"][5], results["matched"][5]
	assert small_thr["sensitivity"] < 0.5, small_thr # mostly under the threshold
	assert small["sensitivity"] >= 0.8 and small["precision"] >= 0.9, small
	for u in range(5): # and the others as well as before
		assert results["matched"][u]["sensitivity"] >= 0.85 and results["matched"][u]["precision"] >= 0.9, results["matched"][u]


def test_the_templates_matched_give_the_same_spikes_however_the_samples_come(sorted_recording):
	tmp, bin_path, phy, times, clusters = sorted_recording
	out = str(tmp / "templates_pieces.npz")
	assert el.Main(["train", "--replay", bin_path, "--phy", phy, "--out", out, "--span", ":5"]) == 0
	source = el.ReplaySource(bin_path, speed=0)
	runs = []
	for piece in (0.1, 0.037, 0.5):
		spikes, _ = es.ReplayMatched(source.data, source.stream, 0, len(source.data), es.MatchingSorter(out, 7), piece)
		runs.append(sorted(zip(spikes.sample.tolist(), spikes.unit.tolist(), spikes.channel.tolist())))
	assert runs[0] and any(u >= 0 for _, u, _ in runs[0])
	assert runs[1] == runs[0] and runs[2] == runs[0]


def test_a_spike_taken_away_changes_the_scores_by_the_cross_correlations(sorted_recording):
	# The scores are worked out once. Taking a spike away lowers them by the templates' cross-correlations, and a
	# group's fit comes from the scores: both as if the data less the spike were matched again, or fitted directly
	tmp, bin_path, phy, times, clusters = sorted_recording
	out = str(tmp / "templates_cross.npz")
	assert el.Main(["train", "--replay", bin_path, "--phy", phy, "--out", out, "--span", ":5"]) == 0
	m = es.MatchingSorter(out, 7).matcher
	rng = np.random.default_rng(3)
	x = rng.normal(0, 8.0, (3000, 96)).astype(np.float32)

	def Put(data, t, u, a):
		data[t - el.BEFORE:t + el.AFTER][:, m.sites[u]] += a * m.Tn[u] * m.noise[m.sites[u]]

	Put(x, 1000, 0, 1.0)
	Put(x, 1004, 1, 0.9) # units 0 and 1 a site apart, at once
	Put(x, 1500, 2, 1.2)
	a0, b0 = 100, 2900
	before = m.Scores(x, a0, b0)
	R, y = before.copy(), x.copy()
	for t, u, a in ((1000, 0, 1.0), (1004, 1, 0.9), (1500, 2, 1.2), (1520, 3, 0.7)):
		Put(y, t, u, -a)
		m.Subtract(R, a0, t, u, a)
	again = m.Scores(y, a0, b0)
	assert np.abs(R - again).max() < 1e-4 * np.abs(before).max()
	members = [(1000, 0), (1004, 1)]
	sizes, explained = m.JointFit(before, a0, members)
	G = np.zeros((len(members), x.size), np.float64)
	for i, (t, u) in enumerate(members):
		g = np.zeros_like(x)
		g[t - el.BEFORE:t + el.AFTER][:, m.sites[u]] = m.Tn[u]
		G[i] = g.ravel()
	direct = np.linalg.lstsq(G.T, (x / m.noise).ravel(), rcond=None)[0]
	assert np.allclose(sizes, direct, rtol=1e-3, atol=1e-3), (sizes, direct)
	assert abs(sizes[0] - 1.0) < 0.1 and abs(sizes[1] - 0.9) < 0.1, sizes


def test_two_units_at_once_on_shared_sites_are_both_found(tmp_path):
	# Units 0 and 1 a site apart (10 of their 12 sites shared), with a third of unit 1's spikes at one of unit 0's,
	# 0 to 3 samples after it. A nearby unit peaking at once is fitted together with the first (both templates'
	# sizes by least squares, at the lag that fits best), and both are taken away: both units' spikes at once are
	# found and named. Fitted alone, the first unit's template takes in the second's spike
	rng = np.random.default_rng(11)
	geometry = el.Geometry.Linear(96)
	T = int(12.0 * RATE)
	x = rng.normal(0, 8.0, (T, 96)).astype(np.float32)
	pair = [(40, 200, 0.12, 0.35), (42, 150, 0.25, 0.15)]
	spread = [np.exp(-np.hypot(geometry.x - geometry.x[ch], geometry.z - geometry.z[ch]) / 35.0).astype(np.float32)
		for ch, _, _, _ in pair]
	t0 = np.cumsum(rng.integers(int(0.03 * RATE), int(0.12 * RATE), 200))
	t0 = t0[t0 < T - 100]
	alone1 = np.cumsum(rng.integers(int(0.05 * RATE), int(0.2 * RATE), 120)) + 7
	together = t0[::3] + rng.integers(0, 4, len(t0[::3])) # a third of unit 1's spikes at unit 0's
	t1 = np.sort(np.concatenate([alone1[alone1 < T - 100], together]))
	t1 = t1[np.concatenate([[True], np.diff(t1) > 60])] # no unit 1 spike within 2 ms of its last
	for (ch, amp, width, bump), sp, ts in zip(pair, spread, (t0, t1)):
		shape = Shape(width, bump)
		for at in ts:
			x[at - 20:at + 41] += amp * shape[:, None] * sp[None, :]
	data = np.clip(np.round(x / UV), -32768, 32767).astype(np.int16)
	times = np.concatenate([t0, t1])
	clusters = np.concatenate([np.zeros(len(t0), int), np.ones(len(t1), int)])
	order = np.argsort(times)
	bin_path = WriteBin(str(tmp_path), data, geometry, first_sample=0)
	phy = WritePhy(str(tmp_path / "kilosort4"), geometry, times[order], clusters[order])
	with open(os.path.join(phy, "cluster_group.tsv"), "w") as f:
		f.write("cluster_id\tgroup\n0\tgood\n1\tgood\n")
	out = str(tmp_path / "templates.npz")
	assert el.Main(["train", "--replay", bin_path, "--phy", phy, "--out", out, "--span", ":8"]) == 0
	source = el.ReplaySource(bin_path, speed=0)
	start = int(8 * RATE)
	both = together[(together > start + 1000) & (together < T - 1000)]
	assert len(both) > 15
	got = {}
	for passes in (1, es.PASSES):
		sorter = es.MatchingSorter(out, 7)
		sorter.matcher.passes = passes
		spikes, _ = es.ReplayMatched(source.data, source.stream, start - int(RATE), T, sorter)
		s0, s1 = spikes.sample[spikes.unit == 0], spikes.sample[spikes.unit == 1]
		# (a unit's live trough comes up to a few samples before Kilosort's time: more for a broad one)
		found_both = sum(1 for t in both if len(s0) and len(s1) and np.abs(s0 - t).min() <= 8 and
			np.abs(s1 - t).min() <= 8)
		got[passes] = found_both / len(both)
	assert got[es.PASSES] >= 0.9, got # both units' spikes, at once, nearly always
	# Without fitting the two together, the first unit's fit takes in the second's spike and leaves too little of it
	sorter = es.MatchingSorter(out, 7)
	sorter.matcher.Group = lambda *a: None
	spikes, _ = es.ReplayMatched(source.data, source.stream, start - int(RATE), T, sorter)
	s0, s1 = spikes.sample[spikes.unit == 0], spikes.sample[spikes.unit == 1]
	alone = sum(1 for t in both if len(s1) and np.abs(s0 - t).min() <= 8 and np.abs(s1 - t).min() <= 8) / len(both)
	assert alone <= 0.3, alone


def test_three_units_at_once_on_shared_sites_are_all_found(tmp_path):
	# Units 0, 1 and 2 on neighbouring sites (peaks 15 um apart, each among the others' 12 sites), with 40 moments
	# where all three spike within 3 samples. The three are fitted together: all three found and named nearly
	# always. With pairs only, the third is mostly lost
	rng = np.random.default_rng(23)
	geometry = el.Geometry.Linear(96)
	T = int(14.0 * RATE)
	x = rng.normal(0, 8.0, (T, 96)).astype(np.float32)
	trio = [(40, 200, 0.12, 0.35), (42, 160, 0.25, 0.15), (44, 180, 0.10, 0.55)]
	own = [np.cumsum(rng.integers(int(0.06 * RATE), int(0.2 * RATE), 140)) + 31 * k for k in range(3)]
	moments = np.sort(rng.choice(np.arange(int(8.5 * RATE), T - 2000, 900), 40, replace=False))
	ts = []
	for k in range(3):
		t = np.concatenate([own[k][own[k] < T - 100], moments + rng.integers(0, 4, len(moments))])
		t = np.sort(t)
		keep = np.concatenate([[True], np.diff(t) > 90]) # no spike of a unit within 3 ms of its last
		ts.append(t[keep])
	for (ch, amp, width, bump), t in zip(trio, ts):
		sp = np.exp(-np.hypot(geometry.x - geometry.x[ch], geometry.z - geometry.z[ch]) / 35.0).astype(np.float32)
		shape = Shape(width, bump)
		for at in t:
			x[at - 20:at + 41] += amp * shape[:, None] * sp[None, :]
	data = np.clip(np.round(x / UV), -32768, 32767).astype(np.int16)
	times = np.concatenate(ts)
	clusters = np.concatenate([np.full(len(t), k) for k, t in enumerate(ts)])
	order = np.argsort(times)
	bin_path = WriteBin(str(tmp_path), data, geometry, first_sample=0)
	phy = WritePhy(str(tmp_path / "kilosort4"), geometry, times[order], clusters[order])
	with open(os.path.join(phy, "cluster_group.tsv"), "w") as f:
		f.write("cluster_id\tgroup\n0\tgood\n1\tgood\n2\tgood\n")
	out = str(tmp_path / "templates.npz")
	assert el.Main(["train", "--replay", bin_path, "--phy", phy, "--out", out, "--span", ":8"]) == 0
	source = el.ReplaySource(bin_path, speed=0)
	# The moments where all three really spiked (none dropped by its unit's 3 ms rule)
	sets = [m for m in moments if all(np.abs(t - m).min() <= 3 for t in ts)]
	assert len(sets) >= 30
	got = {}
	for group in (3, 2):
		saved = es.GROUP
		es.GROUP = group
		try:
			spikes, _ = es.ReplayMatched(source.data, source.stream, int(7.5 * RATE), T, es.MatchingSorter(out, 7))
		finally:
			es.GROUP = saved
		mine = [spikes.sample[spikes.unit == k] for k in range(3)]
		got[group] = sum(1 for m in sets if all(len(s) and np.abs(s - m).min() <= 8 for s in mine)) / len(sets)
	assert got[3] >= 0.85, got
	assert got[2] <= got[3] - 0.3, got
