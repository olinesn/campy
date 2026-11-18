"""
campy/ephystemplates.py: a live sorter's templates from a recording session.
The synthetic units of test_ephyssort.py, recorded as a ChronicRecord
session is (one file per bout, <run>_g0_tN.imec1000.ap.bin), joined, sorted
by a stand-in Kilosort (in a process of its own, as Kilosort's environment
is) that gives the units' true spikes, then trained.
"""

import csv, json, os, sys, textwrap

import numpy as np
import pytest

from campy import ephyslive as el, ephystemplates as et
from test_ephyssort import Recording, WritePhy

RUN = "chronic_2026-11-17_15-08-09"


def WriteBout(folder, t, data, geometry, first_sample, probe="imec1000", shanks=1, imro=None, finished=True):
	os.makedirs(folder, exist_ok=True)
	path = os.path.join(folder, "{}_g0_t{}.{}.ap.bin".format(RUN, t, probe))
	np.hstack([data, np.zeros((len(data), 1), np.int16)]).tofile(path)
	n = data.shape[1]
	geom = "(NP2013,{},250,70)".format(shanks) + "".join("({}:{:g}:{:g}:1)".format(i % shanks, geometry.x[i],
		geometry.z[i]) for i in range(n))
	imro = imro or "(2013,{})".format(n) + "".join("({} 0 0 0 {})".format(i, i) for i in range(n))
	with open(path[:-4] + ".meta", "w") as f:
		f.write("acqApLfSy={n},0,1\nfileName={p}\n{size}fileSHA1=ABC\nfirstSample={first}\nimAiRangeMax=0.62\n"
			"imMaxInt=2048\nimChan0apGain=100\nimSampRate=30000\nnSavedChans={s}\nsnsApLfSy={n},0,1\n"
			"~imroTbl={imro}\n~snsGeomMap={geom}\n".format(n=n, s=n + 1, p=path.replace("\\", "/"), size="fileSizeBytes={}\n"
			"fileTimeSecs={}\n".format(os.path.getsize(path), len(data) / 30000.0) if finished else "",
			first=first_sample, imro=imro, geom=geom))
	return path


@pytest.fixture(scope="module")
def session(tmp_path_factory):
	# A session of four bouts (t0, t1, t2 and t10: SpikeGLX counts on past 9), the probe's files in its run folder,
	# with the orphan imec0 .meta SpikeGLX leaves beside each
	tmp = tmp_path_factory.mktemp("session")
	data, geometry, times, clusters = Recording(seconds=8.0)
	folder = os.path.join(str(tmp), "rec", "spikeglx", RUN + "_g0", RUN + "_g0_imec0")
	cuts = [0, 30000, 120000, 180000, len(data)]
	paths = []
	for i, t in enumerate([0, 1, 2, 10]):
		paths.append(WriteBout(folder, t, data[cuts[i]:cuts[i + 1]], geometry, 100000 + 1000000 * i))
		with open(os.path.join(folder, "{}_g0_t{}.imec0.ap.meta".format(RUN, t)), "w") as f:
			f.write("imSampRate=30000\nnSavedChans={}\n".format(data.shape[1] + 1))
	return os.path.join(str(tmp), "rec"), data, geometry, times, clusters, paths, cuts


def test_a_session_s_bouts_in_spikeglx_s_order(session):
	rec, data, geometry, times, clusters, paths, cuts = session
	said = []
	bouts = et.Bouts(rec, say=said.append)
	assert [b.t for b in bouts] == [0, 1, 2, 10] and [b.path for b in bouts] == paths
	assert [b.samples for b in bouts] == [cuts[i + 1] - cuts[i] for i in range(4)] and bouts[0].probe == "imec1000"
	assert said == []
	assert [b.t for b in et.Bouts(os.path.join(rec, "spikeglx"))] == [0, 1, 2, 10]


def test_the_bouts_joined_one_after_the_other(session, tmp_path):
	rec, data, geometry, times, clusters, paths, cuts = session
	out = str(tmp_path / "imec1000.joined.ap.bin")
	said = []
	assert et.Join(et.Bouts(rec), out, say=said.append) == len(data)
	joined = np.fromfile(out, np.int16).reshape(-1, data.shape[1] + 1)
	assert np.array_equal(joined[:, :-1], data)
	meta = el.ReadMeta(str(tmp_path / "imec1000.joined.ap.meta"))
	assert meta["firstSample"] == "100000" and meta["fileSizeBytes"] == str(os.path.getsize(out))
	assert float(meta["fileTimeSecs"]) == len(data) / 30000.0 and meta["campyJoinedBouts"] == "4"
	assert "fileSHA1" not in meta and meta["fileName"] == out.replace("\\", "/")
	assert meta["snsGeomMap"] == el.ReadMeta(paths[0][:-4] + ".meta")["snsGeomMap"]
	with open(str(tmp_path / "imec1000.joined.ap.bouts.csv")) as f:
		rows = list(csv.DictReader(f))
	assert [(r["file"], int(r["first_sample"]), int(r["samples"]), int(r["joined_start"])) for r in rows] == [
		(paths[i], 100000 + 1000000 * i, cuts[i + 1] - cuts[i], cuts[i]) for i in range(4)]
	# The replay reads it as one recording, the first bout's first sample on
	source = el.ReplaySource(out, speed=0)
	assert source.first == 100000 and len(source.data) == len(data)
	assert said and "Joined 4 bouts of imec1000" in said[-1]


def test_join_stops_at_its_length_or_at_another_probe_setting(session, tmp_path):
	rec, data, geometry, times, clusters, paths, cuts = session
	# Up to 5 s: the first three bouts (6 s) cut at 150,000 samples
	out = str(tmp_path / "five.ap.bin")
	assert et.Join(et.Bouts(rec), out, seconds=5.0, say=lambda s: None) == 150000
	assert np.array_equal(np.fromfile(out, np.int16).reshape(-1, data.shape[1] + 1)[:, :-1], data[:150000])
	# A bout whose imroTbl changed (another bank of sites): joined up to it
	other = str(tmp_path / "other")
	n = data.shape[1]
	for i, t in enumerate([0, 1, 2]):
		WriteBout(other, t, data[cuts[i]:cuts[i + 1]], geometry, 0, imro="(2013,{})".format(n) + "".join(
			"({} 0 0 0 {})".format(c, c + (96 if t == 2 else 0)) for c in range(n)))
	said = []
	assert et.Join(et.Bouts(other), str(tmp_path / "o.ap.bin"), say=said.append) == cuts[2]
	assert any("imroTbl differ from the first bout's" in s for s in said)


def test_unfinished_bouts_left_out_and_several_probes_need_naming(session, tmp_path):
	rec, data, geometry, times, clusters, paths, cuts = session
	folder = str(tmp_path / "two")
	WriteBout(folder, 0, data[:30000], geometry, 0, probe="imec0")
	WriteBout(folder, 0, data[:30000], geometry, 0, probe="imec1")
	WriteBout(folder, 1, data[:30000], geometry, 0, probe="imec1", finished=False) # SpikeGLX stopped mid-bout
	with pytest.raises(el.EphysError, match=r"Bouts of 2 probes .* \(imec0, imec1\): give --probe"):
		et.Bouts(folder)
	said = []
	assert [b.t for b in et.Bouts(folder, "imec1", say=said.append)] == [0]
	assert len(said) == 1 and "didn't finish it" in said[0]
	with pytest.raises(el.EphysError, match="No bouts of imec2"):
		et.Bouts(folder, "imec2")
	with pytest.raises(el.EphysError, match="No probe's bouts"):
		et.Bouts(str(tmp_path / "empty"))


def test_kilosort_s_probe_from_the_meta(tmp_path):
	# A 4-shank probe (NP2013): each channel's shank, and x across the shanks (250 um apart)
	data, geometry, _, _ = Recording(seconds=0.2)
	meta = el.ReadMeta(WriteBout(str(tmp_path), 0, data, geometry, 0, shanks=4)[:-4] + ".meta")
	probe = et.Probe(meta)
	n = data.shape[1]
	assert probe["n_chan"] == n and np.array_equal(probe["chanMap"], np.arange(n))
	assert np.allclose(probe["kcoords"], np.arange(n) % 4) and np.allclose(probe["yc"], geometry.z)
	assert np.allclose(probe["xc"], geometry.x + 250 * (np.arange(n) % 4))


def test_templates_from_a_session_joined_sorted_and_trained(session, tmp_path, monkeypatch):
	# Kilosort's environment stood in for by a kilosort module whose run_kilosort writes the units' true spikes as
	# Kilosort 4 would (phy's files), and says what it was given
	rec, data, geometry, times, clusters, paths, cuts = session
	fake = tmp_path / "fake_kilosort" / "kilosort"
	fake.mkdir(parents=True)
	np.savez(str(tmp_path / "truth.npz"), times=times, clusters=clusters, x=geometry.x, z=geometry.z)
	(fake / "__init__.py").write_text(textwrap.dedent("""
		import json, os, sys
		import numpy as np
		sys.path.insert(0, {tests!r})
		from campy import ephyslive as el
		from test_ephyssort import WritePhy

		def run_kilosort(settings, probe, results_dir):
			with open({calls!r}, "a") as f:
				f.write(json.dumps({{"settings": settings, "n_chan": probe["n_chan"], "kcoords": sorted(set(
					probe["kcoords"].tolist())), "yc_max": float(probe["yc"].max())}}) + "\\n")
			t = np.load({truth!r})
			WritePhy(results_dir, el.Geometry(t["x"], t["z"]), t["times"], t["clusters"])
	""").format(tests=os.path.dirname(os.path.abspath(__file__)), calls=str(tmp_path / "calls.jsonl"),
		truth=str(tmp_path / "truth.npz")))
	monkeypatch.setenv("PYTHONPATH", str(tmp_path / "fake_kilosort"))
	out = str(tmp_path / "templates")
	said = []
	npz = et.Templates(rec, out, sys.executable, say=said.append)
	assert npz == os.path.join(out, "templates.npz") and os.path.isfile(npz)
	calls = [json.loads(l) for l in open(str(tmp_path / "calls.jsonl"))]
	assert len(calls) == 1 and calls[0]["settings"] == {"n_chan_bin": data.shape[1] + 1, "filename": os.path.join(
		out, "imec1000.joined.ap.bin"), "fs": 30000.0}
	assert calls[0]["n_chan"] == data.shape[1] and calls[0]["yc_max"] == float(geometry.z.max())
	with np.load(npz) as f:
		assert sorted(f["units"][~f["compete"]].tolist()) == [0, 1, 2, 3, 4] and "match_z" in f
	record = json.load(open(os.path.join(out, "templates.json")))
	assert record["bouts"] == 4 and record["probe"] == "imec1000" and record["span"] == "0:"
	assert record["train"]["units"] == 5 and record["joined_s"] == len(data) / 30000.0
	assert "    templates: {}".format(os.path.abspath(npz).replace("\\", "/")) in said[-1] and \
		said[-1].endswith("    match: learned")
	# Again: joined and sorted already, so only trained again (Kilosort not run twice)
	said = []
	et.Templates(rec, out, sys.executable, say=said.append)
	assert len(open(str(tmp_path / "calls.jsonl")).readlines()) == 1
	assert any(s.startswith("Already joined") for s in said) and any(s.startswith("Already sorted") for s in said)


def test_kilosort_failing_stops_it_and_says_so(session, tmp_path, monkeypatch):
	rec = session[0]
	fake = tmp_path / "broken" / "kilosort"
	fake.mkdir(parents=True)
	(fake / "__init__.py").write_text("def run_kilosort(settings, probe, results_dir):\n\traise RuntimeError('CUDA out "
		"of memory')\n")
	monkeypatch.setenv("PYTHONPATH", str(tmp_path / "broken"))
	said = []
	with pytest.raises(el.EphysError, match="Kilosort 4 ended with 1"):
		et.Templates(rec, str(tmp_path / "t"), sys.executable, minutes=0.05, say=said.append)
	assert any("CUDA out of memory" in s for s in said)
	assert not os.path.exists(str(tmp_path / "t" / "templates.npz"))
	# The command line says it too, and ends 1
	assert el.Main(["templates", "--session", rec, "--out", str(tmp_path / "t"), "--kilosort-python",
		sys.executable, "--minutes", "0.05"]) == 1
