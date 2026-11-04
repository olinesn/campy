"""
campy/runstats.py: the numbers campy-live and campy-rules keep for their
summaries' percentiles, in bounded memory over a run of days.
"""

import threading
import types

import numpy as np
import pytest

from campy import live, runstats


@pytest.fixture
def small(monkeypatch):
	# Blocks of 64 and 1024 values kept: the folding seen in thousands of values, not millions
	monkeypatch.setattr(runstats, "BLOCK", 64)
	monkeypatch.setattr(runstats, "EXACT", 1024)


def test_up_to_exact_every_value_is_kept_as_it_is():
	rng = np.random.default_rng(1)
	values = [int(v) for v in rng.integers(1, 50_000_000, 10_000)]
	kept = runstats.Values()
	for v in values:
		kept.Add(v)
	assert len(kept) == 10_000
	assert np.array_equal(kept.Exact(), np.array(values, np.int64))
	assert np.array_equal(kept.Since(9_990), np.array(values[9_990:], np.int64))
	assert runstats.Values().Exact().size == 0 and runstats.Values().Approx((50,)) is None


def test_past_exact_the_memory_stops_growing_and_the_percentiles_stay_close(small):
	rng = np.random.default_rng(2)
	values = np.concatenate([rng.lognormal(np.log(8e6), 0.4, 20_000), [0, 3, 999, 2e12]]).astype(np.int64)
	kept = runstats.Values()
	for v in values:
		kept.Add(v)
	assert kept.Exact() is None and kept.folded > 0
	assert len(kept.blocks) * runstats.BLOCK <= runstats.EXACT # the rest is in the histogram
	a = kept.Approx((50, 90, 99))
	assert a["n"] == len(values) and a["max"] == values.max() and a["min"] == values.min()
	assert a["mean"] == pytest.approx(values.mean(), rel=1e-12)
	for q in (50, 90, 99):
		assert a[q] == pytest.approx(np.percentile(values.astype(np.float64), q), rel=runstats.RATIO - 1)
	# A progress line's window is recent: still as it was
	assert np.array_equal(kept.Since(len(values) - 100), values[-100:])


def test_negative_and_tiny_values_in_float(small):
	# campy-rules' trigger delays: ms, a little below 0 now and then (the clock map's error)
	rng = np.random.default_rng(3)
	values = rng.normal(0.4, 0.3, 30_000)
	kept = runstats.Values(np.float64, floor=0.001)
	for v in values:
		kept.Add(v)
	a = kept.Approx((1, 50, 99))
	for q in (1, 50, 99):
		exact = np.percentile(values, q)
		assert abs(a[q] - exact) <= max(abs(exact) * (runstats.RATIO - 1), 0.001 / runstats.LINEAR), q
	assert a["min"] == values.min() and a["max"] == values.max()


def test_one_thread_adds_while_another_reads(small):
	kept, errors, done = runstats.Values(), [], threading.Event()
	def read():
		while not done.is_set():
			try:
				kept.Since(max(0, len(kept) - 50))
				kept.Approx((50,))
			except Exception as e: # pragma: no cover: what this test looks for
				errors.append(e)
	t = threading.Thread(target=read)
	t.start()
	for v in range(20_000):
		kept.Add(v)
	done.set()
	t.join()
	assert errors == [] and len(kept) == 20_000 and kept.Approx((50,))["max"] == 19_999


def log_sets(log, first, count, gap=lambda n: 0):
	for n in range(first, first + count):
		frames = [types.SimpleNamespace(frameNumber=n, hostTime=10**6 * n + 7 * c + gap(n), written=10**6 * n + 5,
			timeStamp=n / 60) for c in range(3)]
		log.Add(log.Row(frames, 0, 10**6 * n + 1000 + n % 7, 10**6 * n + 900_000 - 37 * (n % 101), {}), frames)


def test_campy_lives_summary_is_the_same_as_from_its_log(tmp_path):
	# Up to runstats.EXACT sets, the numbers are numpy's over every set, as they were from lists: here, checked
	# against the rows campy-live wrote
	log = live.Log(str(tmp_path / "live.npy"), 3, 5, 15, 60)
	log_sets(log, 1, 3000, gap=lambda n: (n % 13) * 1000)
	log.Close()
	rows = np.load(tmp_path / "live.npy")
	host = rows["host_ns"].astype(np.int64)
	latency = (rows["done_ns"] - host.max(1)).astype(np.float64) / 1e6
	summary = log.Summary()
	for q in (50, 90, 99):
		assert summary["latency_from_last_frame_ms"]["p{}".format(q)] == float(np.percentile(latency, q))
	assert summary["latency_from_last_frame_ms"]["mean"] == float(latency.mean())
	spread = (host.max(1) - host.min(1)).astype(np.float64) / 1e6
	assert summary["host_time_spread_ms"]["max"] == float(spread.max())
	# The progress line's window: the sets since the last line
	window = log.Summary(since=2900)["latency_from_last_frame_ms"]
	assert window["p50"] == float(np.percentile(latency[2900:], 50))


def test_campy_lives_summary_past_exact(tmp_path, small):
	log = live.Log(str(tmp_path / "live.npy"), 3, 5, 15, 60)
	log_sets(log, 1, 5000)
	log.Close()
	rows = np.load(tmp_path / "live.npy")
	latency = (rows["done_ns"] - rows["host_ns"].max(1)).astype(np.float64) / 1e6
	got = log.Summary()["latency_from_last_frame_ms"]
	assert got["max"] == latency.max() and got["mean"] == pytest.approx(latency.mean(), rel=1e-12)
	for q in (50, 90, 99):
		assert got["p{}".format(q)] == pytest.approx(np.percentile(latency, q), rel=runstats.RATIO - 1)
