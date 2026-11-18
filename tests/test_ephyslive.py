"""
campy/ephyslive.py: live ephys from SpikeGLX. A synthetic Neuropixels 2.0
recording (noise, slow drifts, spikes planted at known samples and sites)
through the filter and the detector, from arrays, from a saved .bin/.meta,
and from a stand-in SpikeGLX (tests/fake_sglx).
"""

import json, os, sys, time

import numpy as np
import pytest

from campy import ephyslive as el

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_SDK = os.path.join(HERE, "fake_sglx")
RATE = 30000.0
UV = 0.62 / 8192 / 80 * 1e6 # uV per count: a Neuropixels 2.0 probe's


def Shape():
	"""An extracellular spike's shape, 61 samples: the trough at sample 20 (0.12 ms wide), a bump a third as big
	0.45 ms later (0.25 ms wide)."""
	t = np.arange(61) / 30.0 # ms
	return (-np.exp(-0.5 * ((t - 20 / 30.0) / 0.12) ** 2) + 0.35 * np.exp(-0.5 * ((t - 1.1) / 0.25) ** 2)).astype(
		np.float32)


def Synthetic(seconds=2.0, n=96, units=4, rate_hz=15.0, noise_uv=8.0, amp_uv=180.0, seed=1):
	"""int16 [samples, n], the geometry, and the planted spikes: [(trough sample, channel, unit)]."""
	rng = np.random.default_rng(seed)
	geometry = el.Geometry.Linear(n)
	T = int(seconds * RATE)
	t = np.arange(T) / RATE
	x = rng.normal(0, noise_uv, (T, n)).astype(np.float32)
	x += (300 * np.sin(2 * np.pi * 4 * t))[:, None] # common to every channel: the median takes it off
	x += 150 * np.sin(2 * np.pi * 2 * t[:, None] + rng.uniform(0, 6.28, n)[None, :]).astype(np.float32) # the high-pass
	shape = Shape()
	truth = []
	for u in range(units):
		ch = int((u + 0.5) * n / units) # spread along the probe
		d = np.hypot(geometry.x - geometry.x[ch], geometry.z - geometry.z[ch])
		spread = np.exp(-d / 35.0).astype(np.float32) # a unit seen on 10 to 20 sites, as on a Neuropixels probe
		at = 0
		while True:
			at += int(RATE * (0.004 + rng.exponential(1 / rate_hz)))
			if at + 41 >= T:
				break
			x[at - 20:at + 41] += amp_uv * shape[:, None] * spread[None, :]
			truth.append((at, ch, u))
	return np.clip(np.round(x / UV), -32768, 32767).astype(np.int16), geometry, sorted(truth)


def Engine(data, geometry, chunk_s=0.05, **kwargs):
	source = el.ReplaySource.FromArray(data, RATE, np.full(data.shape[1], UV, np.float32), geometry, speed=0,
		chunk_s=chunk_s)
	return el.Engine(source, **kwargs)


def RunAll(engine):
	"""Every spike, as Spikes, the source read to its end."""
	found = []
	engine.Start()
	add = engine.live.Add

	def Keep(first, filtered, spikes):
		found.append(spikes)
		add(first, filtered, spikes)
	engine.live.Add = Keep
	while engine.source.state != "ended":
		engine.Step()
	return el.Spikes(*(np.concatenate([getattr(s, k) for s in found]) for k in ("sample", "channel", "amplitude",
		"unit")), np.concatenate([s.waveforms for s in found]), np.concatenate([s.neighbors for s in found]))


@pytest.fixture(scope="module")
def recording():
	return Synthetic()


def Matched(spikes, truth, start, tolerance=4, radius_um=40.0, geometry=None):
	"""How many planted spikes after start were found (within tolerance samples, on a site within radius_um).
	Filtered forwards, a spike's trough comes up to 3 samples early."""
	hit = 0
	planted = [p for p in truth if p[0] >= start]
	for at, ch, _ in planted:
		near = np.abs(spikes.sample - at) <= tolerance
		if near.any():
			d = np.hypot(geometry.x[spikes.channel[near]] - geometry.x[ch], geometry.z[spikes.channel[near]] -
				geometry.z[ch])
			hit += bool((d <= radius_um).any())
	return hit, len(planted)


def test_finds_the_planted_spikes(recording):
	data, geometry, truth = recording
	spikes = RunAll(Engine(data, geometry))
	start = int(RATE * el.NOISE_BLOCK_S) + 100 # the first block measures the noise
	hit, planted = Matched(spikes, truth, start, geometry=geometry)
	assert planted > 50
	assert hit >= 0.95 * planted, (hit, planted)
	# One spike each: no second one from a spike's swing after its trough, nor on sites far from it. Beyond the
	# planted ones, only noise crossing 4.5 x its level: Gaussian noise does, about 0.1 to 0.2 times a second on
	# each channel
	planted_at = np.array([at for at, _, _ in truth])
	alone = [s for s in spikes.sample if np.abs(planted_at - s).min() > 60]
	assert len(alone) / (data.shape[1] * (len(data) - start) / RATE) < 0.3
	near = len(spikes) - len(alone)
	assert near <= 1.1 * len(truth), (near, len(truth))
	assert (spikes.unit == -1).all() # not sorted
	assert (spikes.amplitude < 0).all()


def test_same_spikes_however_the_samples_come(recording):
	"""Pieces of 50 ms, of 13 ms and of 0.5 s give the same spikes, to the sample and the waveform: so a recording
	replayed gives the spikes the live view showed."""
	data, geometry, _ = recording
	runs = [RunAll(Engine(data, geometry, chunk_s=c)) for c in (0.05, 0.013, 0.5)]
	for other in runs[1:]:
		assert np.array_equal(runs[0].sample, other.sample)
		assert np.array_equal(runs[0].channel, other.channel)
		assert np.allclose(runs[0].waveforms, other.waveforms, atol=1e-3)


def test_waveforms_in_kilosorts_window(recording):
	"""61 samples, the trough at sample 20 on the spike's own channel (its neighbors' column 0)."""
	data, geometry, truth = recording
	spikes = RunAll(Engine(data, geometry))
	assert spikes.waveforms.shape[1:] == (61, el.NEIGHBORS)
	assert (spikes.neighbors[:, 0] == spikes.channel).all()
	mean = spikes.waveforms[:, :, 0].mean(axis=0)
	assert int(np.argmin(mean)) == el.BEFORE
	assert np.allclose(spikes.waveforms[np.arange(len(spikes)), el.BEFORE, 0], spikes.amplitude)
	# Its 12 nearest sites, nearest first, all of them (a Neuropixels 2.0 probe's: within 90 um)
	nb = spikes.neighbors[0]
	assert (nb >= 0).all()
	d = np.hypot(geometry.x[nb] - geometry.x[nb[0]], geometry.z[nb] - geometry.z[nb[0]])
	assert d[0] == 0 and (np.diff(d) >= 0).all() and d.max() <= 90


def test_threshold_per_channel(recording):
	data, geometry, truth = recording
	engine = Engine(data, geometry)
	engine.Start()
	off = sorted({ch for _, ch, _ in truth})[0]
	engine.detector.SetThreshold(np.inf, channel=off)
	spikes = RunAll_from_started(engine)
	assert off not in set(spikes.channel.tolist())
	assert len(spikes) > 0


def RunAll_from_started(engine):
	found = []
	while engine.source.state != "ended":
		first, raw = engine.source.Read() or (None, None)
		if raw is None:
			break
		found.append(engine.detector.Feed(first, engine.filter.Apply(raw)))
	return el.Spikes(*(np.concatenate([getattr(s, k) for s in found]) for k in ("sample", "channel", "amplitude")))


class ByChannel:
	"""A stand-in live sorter: unit = channel // 32."""
	name = "test"

	def Assign(self, spikes):
		spikes.unit = (spikes.channel // 32).astype(np.int32)
		return spikes

	def Units(self):
		return []


def test_a_sorter_s_units_reach_the_view(recording):
	data, geometry, truth = recording
	engine = Engine(data, geometry, sorter=ByChannel())
	spikes = RunAll(engine)
	snap = engine.live.Snapshot(snippet_units=[0, 1, 2])
	assert set(snap["spikes"]["unit"]) <= {0, 1, 2}
	assert snap["rates"]["unit"] and all(r > 0 for r in snap["rates"]["unit"].values())
	for u, waves in snap["snippets"]["unit"].items():
		assert len(waves[0]) == 61


def test_the_view_s_envelope_and_spikes_since(recording):
	"""A view asking for what came since its last snapshot gets every millisecond bin and every spike once."""
	data, geometry, _ = recording
	engine = Engine(data, geometry, scope=[3, 50])
	engine.Start()
	live = engine.live
	bins, spikes = {}, []
	last_bin = last_sample = None
	while engine.source.state != "ended":
		engine.Step()
		snap = live.Snapshot(since_sample=last_sample, since_bin=last_bin)
		scope = snap["scope"]
		if scope["first_bin"] is not None and scope["min"][0]:
			for i in range(len(scope["min"][0])):
				assert scope["first_bin"] + i not in bins
				bins[scope["first_bin"] + i] = (scope["min"][0][i], scope["max"][1][i])
			last_bin = scope["first_bin"] + len(scope["min"][0]) - 1
		got = list(zip(snap["spikes"]["sample"], snap["spikes"]["channel"]))
		spikes += got
		if got:
			last_sample = got[-1][0]
	json.dumps(snap) # for the page
	# The bins are each millisecond's lowest (channel 3) and highest (channel 50) of the filtered samples
	filtered = el.Filter(engine.stream).Apply(data)
	keys = sorted(bins)
	assert keys == list(range(keys[0], keys[-1] + 1)) and len(keys) >= len(data) // live.bin - 1
	for b in keys[:50]:
		assert abs(bins[b][0] - filtered[b * 30:(b + 1) * 30, 3].min()) < 0.1
		assert abs(bins[b][1] - filtered[b * 30:(b + 1) * 30, 50].max()) < 0.1
	# Each spike once, in order (spikes at the same sample on two channels may both come)
	assert len(spikes) == len(set(spikes)) and [t for t, _ in spikes] == sorted(t for t, _ in spikes)
	assert len(spikes) == engine.stats["spikes"]


def WriteBin(folder, data, geometry, first_sample=1234, imro_np1=False):
	path = os.path.join(folder, "run_g0_t0.imec0.ap.bin")
	sync = np.zeros((len(data), 1), np.int16)
	np.hstack([data, sync]).tofile(path)
	n = data.shape[1]
	geom = "(NP2000,1,0,70)" + "".join("(0:{:g}:{:g}:1)".format(geometry.x[i], geometry.z[i]) for i in range(n))
	imro = "(0,{})".format(n) + "".join("({} 0 0 500 250 1)".format(i) for i in range(n)) if imro_np1 else \
		"(24,{})".format(n) + "".join("({} 0 0 0)".format(i) for i in range(n))
	with open(path[:-4] + ".meta", "w") as f:
		f.write("imSampRate=30000\nnSavedChans={}\nsnsApLfSy={},0,1\nimAiRangeMax=0.62\nimMaxInt=8192\n"
			"firstSample={}\n~snsGeomMap={}\n~imroTbl={}\n".format(n + 1, n, first_sample, geom, imro))
	return path


def test_replay_of_a_saved_recording(tmp_path, recording):
	data, geometry, _ = recording
	path = WriteBin(str(tmp_path), data[:15000], geometry)
	source = el.ReplaySource(path, speed=0, chunk_s=0.1)
	stream = source.Open()
	assert stream.rate == 30000 and len(stream.uv) == data.shape[1]
	assert np.allclose(stream.uv, UV)
	assert np.allclose(stream.geometry.x, geometry.x) and np.allclose(stream.geometry.z, geometry.z)
	got, firsts = [], []
	while True:
		piece = source.Read()
		if piece is None:
			break
		firsts.append(piece[0])
		got.append(piece[1])
	assert firsts[0] == 1234 and np.array_equal(np.concatenate(got), data[:15000])
	# A Neuropixels 1.0 probe's gain (500) from its imroTbl (in a folder of its own: Windows won't rewrite a file
	# still mapped)
	(tmp_path / "np1").mkdir()
	path = WriteBin(str(tmp_path / "np1"), data[:3000], geometry, imro_np1=True)
	assert np.allclose(el.ReplaySource(path).Open().uv, 0.62 / 8192 / 500 * 1e6)
	# An NP2013 (Neuropixels 2.0, 4 shanks): one gain for all (imChan0apGain 100), 2048 counts: 3.03 uV a count
	meta = path[:-4] + ".meta"
	text = open(meta).read().replace("imMaxInt=8192", "imMaxInt=2048") + "imChan0apGain=100\n"
	text = text.replace("~imroTbl=(0,", "~imroTbl=(2013,").replace(" 500 250 1)", " 0)")
	open(meta, "w").write(text)
	assert np.allclose(el.ReplaySource(path).Open().uv, 0.62 / 2048 / 100 * 1e6)


def test_replay_at_the_recorded_speed(tmp_path, recording):
	data, geometry, _ = recording
	source = el.ReplaySource.FromArray(data, RATE, np.full(data.shape[1], UV, np.float32), geometry, speed=1.0)
	source.Open()
	t0, total = time.monotonic(), 0
	while time.monotonic() - t0 < 0.3:
		piece = source.Read()
		total += 0 if piece is None else len(piece[1])
		time.sleep(0.01)
	assert 0.2 * RATE < total < 0.45 * RATE


def FakeState(tmp_path, data, **state):
	np.save(str(tmp_path / "ap.npy"), data)
	path = tmp_path / "sglx.json"
	path.write_text(json.dumps(dict({"open": True, "validated": True, "trigMode": "2", "running": True,
		"t_run": time.time() - 2.0, "ap_npy": str(tmp_path / "ap.npy"), "calls": []}, **state)))
	return path


def test_reads_spikeglx_live_with_read_only_calls(tmp_path, monkeypatch, recording):
	data, geometry, truth = recording
	state = FakeState(tmp_path, data[:, :32])
	monkeypatch.setenv("FAKE_SGLX_STATE", str(state))
	source = el.SpikeGLXSource(FAKE_SDK)
	engine = el.Engine(source)
	firsts = []
	t0 = time.monotonic()
	while time.monotonic() - t0 < 1.0:
		if engine.stream is None:
			engine.Start()
		piece_n = engine.Step()
		if piece_n:
			firsts.append(engine.expected - piece_n)
		time.sleep(0.02)
	source.Close()
	assert engine.stream is not None and len(engine.stream.uv) == 32 and engine.stream.rate == 30000
	assert np.allclose(engine.stream.uv, UV)
	assert np.allclose(engine.stream.geometry.z, geometry.z[:32]) # from its geomMap, given in alphanumeric order
	assert engine.stats["samples"] > 0.8 * RATE and engine.stats["gaps"] == 0 and source.skipped == 0
	assert engine.stats["spikes"] > 0
	assert engine.stats["reads"] <= 1.0 / 0.05 + 2 # at most a read every 50 ms, not one per 2 ms of samples
	# Only fetches: nothing set, started or stopped
	calls = json.loads(state.read_text())["calls"]
	assert set(calls) == {"fetch"}
	# A call that changes SpikeGLX can't be made through it
	with pytest.raises(el.EphysError):
		source.sdk.c_sglx_setRecordingEnable


def test_no_run_then_a_run(tmp_path, monkeypatch, recording):
	data, geometry, _ = recording
	state = FakeState(tmp_path, data[:, :16], running=False, t_run=None)
	monkeypatch.setenv("FAKE_SGLX_STATE", str(state))
	source = el.SpikeGLXSource(FAKE_SDK)
	engine = el.Engine(source)
	assert engine.Step() == 0 and engine.stream is None
	assert source.state == "SpikeGLX runs no run"
	s = json.loads(state.read_text())
	s.update(running=True, t_run=time.time() - 1.0)
	state.write_text(json.dumps(s))
	got = 0
	t0 = time.monotonic()
	while not got and time.monotonic() - t0 < 2:
		got = engine.Step()
		time.sleep(0.02)
	assert got > 0 and engine.stream is not None
	source.Close()


def test_falls_behind_then_skips_ahead(tmp_path, monkeypatch, recording):
	data, geometry, _ = recording
	state = FakeState(tmp_path, data[:, :8])
	monkeypatch.setenv("FAKE_SGLX_STATE", str(state))
	source = el.SpikeGLXSource(FAKE_SDK, max_lag_s=0.5)
	engine = el.Engine(source)
	assert engine.Start()
	engine.Step()
	time.sleep(1.2) # this computer busy: 1.2 s behind
	t0 = time.monotonic()
	while time.monotonic() - t0 < 0.5:
		engine.Step()
	assert source.skipped > 0.4 * RATE
	assert source.Lag() is not None and source.Lag() < 0.5
	source.Close()


def test_bench_on_a_replay(tmp_path, recording, capsys):
	data, geometry, _ = recording
	path = WriteBin(str(tmp_path), data, geometry)
	out = tmp_path / "bench.json"
	assert el.Main(["bench", "--replay", path, "--speed", "0", "--seconds", "20", "--json", str(out),
		"--priority", "normal"]) == 0
	summary = json.loads(out.read_text())
	assert summary["samples"] == len(data) and summary["spikes"] > 50 and summary["gaps"] == 0
	assert "done:" in capsys.readouterr().out
