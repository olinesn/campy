"""campy-live-flag (campy/liveflag.py) on a made-up campy-live log with known mistakes.

Three cameras see 3 animals walking slow circles for 200 sets; campy-live's
3D matched them perfectly, except where a mistake is planted:

  sets 20-29   Camera1 missed animal 0 (the other two cameras still see it)
  set 40       Camera2 put animal 1's skeleton 40 px off
  set 60       animal 2's 3D jumped 200 mm for one set (and back at 61)
  sets 80-     animals 0 and 1 swapped identities
  set 100      Camera0's detection of animal 2 scored 0.2
  set 120      Camera0 found a 4th, extra, animal (--max-instances 5)
  set 140      Camera2 saw animal 0, but the 3D matched it to no animal
"""

import csv, json, os

import numpy as np
import pytest

from scipy.spatial.transform import Rotation

from campy import live, liveflag
import test_pose3d as scene3d

CAMERAS = ["Camera0", "Camera1", "Camera2"]
CALIBRATION_NAMES = ["top", "left", "right"]
ANIMALS, SETS, NODES = 3, 200, 15
PERIOD = 16666667


def write_calibration(path, cams):
	# anipose's calibration.toml (as test_liveexport's, which needs sleap-io)
	with open(path, "w") as f:
		for i, cam in enumerate(cams):
			f.write("[cam_{}]\nname = \"{}\"\nsize = [ 1280, 1024,]\n".format(i, cam.name))
			f.write("matrix = {}\ndistortions = {}\n".format(cam.K.tolist(), cam.dist.tolist()))
			f.write("rotation = {}\ntranslation = {}\n\n".format(Rotation.from_matrix(cam.R).as_rotvec().tolist(),
				cam.t.tolist()))


def rig():
	return [scene3d.look_at(n, *where) for n, where in zip(CALIBRATION_NAMES,
		[((0, 30, 700), (0, 0, 0), (0, 1, 0)), ((-550, -300, 450),), ((550, -300, 450),)])]


def make_rows(cams, truth, plant=True):
	rows = np.zeros(SETS, live.RowType(3, 5, NODES, ANIMALS))
	rng = np.random.default_rng(1)
	for r in range(SETS):
		row = rows[r]
		row["frame"] = r + 1
		row["host_ns"] = [10 ** 12 + r * PERIOD + c for c in range(3)]
		for key in ("centroids", "centroid_vals", "keypoints", "peak_vals", "reproj_px"):
			row[key] = np.nan
		row["members"] = -1
		X = truth[r].copy()
		for c, cam in enumerate(cams):
			order = rng.permutation(ANIMALS)
			for slot, k in enumerate(order):
				row["keypoints"][c, slot] = cam.Project(X[k])
				row["peak_vals"][c, slot] = 0.9
				row["centroids"][c, slot] = row["keypoints"][c, slot, 6]
				row["centroid_vals"][c, slot] = 0.8
				row["members"][k, c] = slot
			row["found"][c] = ANIMALS
		row["points3d"] = X
		row["n_views"] = 3
		row["reproj_px"] = 0.5
	if not plant:
		return rows
	for r in range(19, 29): # Camera1 missed animal 0
		row = rows[r]
		slot = row["members"][0, 1]
		row["keypoints"][1, slot] = row["centroids"][1, slot] = np.nan
		row["peak_vals"][1, slot] = row["centroid_vals"][1, slot] = np.nan
		row["members"][0, 1] = -1
		row["n_views"][0] = 2
		row["found"][1] = 2
	row = rows[39] # Camera2 puts animal 1 40 px off
	row["keypoints"][2, row["members"][1, 2]] += [40.0, 0.0]
	row = rows[59] # animal 2 jumps for one set: each camera's detection of it with it
	row["points3d"][2] += [200.0, 0.0, 0.0]
	for c, cam in enumerate(cams):
		slot = row["members"][2, c]
		row["keypoints"][c, slot] = cam.Project(row["points3d"][2])
		row["centroids"][c, slot] = row["keypoints"][c, slot, 6]
	for r in range(79, SETS): # 0 and 1 swap identities
		row = rows[r]
		row["points3d"][[0, 1]] = row["points3d"][[1, 0]]
		row["members"][[0, 1]] = row["members"][[1, 0]].copy()
	row = rows[99] # a low score
	row["peak_vals"][0, row["members"][2, 0]] = 0.2
	row = rows[119] # an extra detection
	row["keypoints"][0, 3] = row["keypoints"][0, 0] + [300.0, 200.0]
	row["centroids"][0, 3] = row["keypoints"][0, 3, 6]
	row["peak_vals"][0, 3] = 0.9
	row["found"][0] = 4
	row = rows[139] # a detection matched to no animal
	row["members"][0 if 139 < 79 else 1, 2] = -1
	row["n_views"][1] = 2
	return rows


@pytest.fixture
def log(tmp_path):
	cams = rig()
	write_calibration(str(tmp_path / "calibration.toml"), cams)
	truth = scene3d.scene(SETS, n=ANIMALS)
	path = str(tmp_path / "campy-live_test")
	np.save(path + ".npy", make_rows(cams, truth))
	with open(path + ".json", "w") as f:
		json.dump({"cameras": CAMERAS, "calibration": str(tmp_path / "calibration.toml"),
			"calibration_names": CALIBRATION_NAMES, "animals": ANIMALS, "frame_rate": 60.0}, f)
	return path + ".npy", cams, truth


def flags_of(path, **settings):
	s = liveflag.Settings(logs=[path], **settings)
	lg = liveflag.Log(path, s)
	return lg, liveflag.FlagLog(lg, s)


def where(flags, reason):
	f = flags[flags["reason"] == reason]
	return f, sorted(set(f["frame"].tolist()))


def test_a_clean_log_has_no_flags(tmp_path):
	cams = rig()
	write_calibration(str(tmp_path / "calibration.toml"), cams)
	path = str(tmp_path / "clean")
	np.save(path + ".npy", make_rows(cams, scene3d.scene(SETS, n=ANIMALS), plant=False))
	with open(path + ".json", "w") as f:
		json.dump({"cameras": CAMERAS, "calibration": str(tmp_path / "calibration.toml"),
			"calibration_names": CALIBRATION_NAMES, "animals": ANIMALS}, f)
	_, flags = flags_of(path + ".npy")
	assert len(flags) == 0


def test_each_planted_mistake_is_flagged_where_it_is(log):
	path, cams, truth = log
	lg, flags = flags_of(path)
	missed, frames = where(flags, "missed")
	assert frames == list(range(20, 30))
	assert set(missed["camera"].tolist()) == {1} and set(missed["animal"].tolist()) == {0}
	# Where Camera1 should have seen it: its 3D, projected
	for f in missed:
		want = np.median(cams[1].Project(truth[f["frame"] - 1, 0]), axis=0)
		assert np.hypot(f["x"] - want[0], f["y"] - want[1]) < 1.0
		assert f["value"] > 60 # no detection near it
	disagree, frames = where(flags, "disagree")
	assert frames == [40]
	assert disagree["camera"].tolist() == [2] and disagree["animal"].tolist() == [1]
	assert abs(disagree["value"][0] - 40) < 1
	jump, frames = where(flags, "jump")
	assert frames == [60, 61] and set(jump["animal"].tolist()) == {2}
	swap, frames = where(flags, "swap")
	assert frames == [80] and sorted(swap["animal"].tolist()) == [0, 1]
	low, frames = where(flags, "low_score")
	assert frames == [100] and low["camera"].tolist() == [0] and low["animal"].tolist() == [2]
	extra, frames = where(flags, "extra")
	assert frames == [120] and extra["camera"].tolist() == [0] and extra["value"].tolist() == [1]
	unmatched, frames = where(flags, "unmatched")
	assert frames == [140] and unmatched["camera"].tolist() == [2]
	assert unmatched["value"][0] < 60 # right where animal 1 (truly animal 0, since the swap) projects
	# A camera that missed an animal found fewer, and no other set did
	fewer, frames = where(flags, "fewer")
	assert frames == list(range(20, 30)) and set(fewer["camera"].tolist()) == {1}
	# The unmatched detection is not also a missed animal: it is right there
	assert 140 not in where(flags, "missed")[1]


def test_without_the_calibration_file_disagree_uses_the_logs_reprojection(log, tmp_path):
	path, _, _ = log
	os.remove(str(tmp_path / "calibration.toml"))
	rows = np.load(path)
	rows[39]["reproj_px"][1] = 20.0
	np.save(path, rows)
	lg, flags = flags_of(path)
	assert lg.calibration is None
	assert where(flags, "missed")[1] == []
	disagree, frames = where(flags, "disagree")
	assert frames == [40] and disagree["camera"].tolist() == [-1]
	assert where(flags, "swap")[1] == [80] # 3D motion needs no calibration


def test_a_2d_log_flags_counts_and_scores_only(tmp_path):
	cams = rig()
	rows3d = make_rows(cams, scene3d.scene(SETS, n=ANIMALS))
	rows = np.zeros(SETS, live.RowType(3, 5, NODES))
	for name in rows.dtype.names:
		rows[name] = rows3d[name]
	path = str(tmp_path / "flat.npy")
	np.save(path, rows)
	lg, flags = flags_of(path, animals=3)
	assert set(flags["reason"].tolist()) == {"fewer", "low_score", "extra"}


def test_flags_of_a_moment_make_one_event_and_one_pick(log):
	path, _, _ = log
	s = liveflag.Settings(logs=[path])
	lg = liveflag.Log(path, s)
	flags = liveflag.FlagLog(lg, s)
	events = liveflag.Events(flags, s)
	missed = [e for e in events if e["reason"] == "missed"]
	assert len(missed) == 1 and len(missed[0]["rows"]) == 10
	jump = [e for e in events if e["reason"] == "jump"]
	assert len(jump) == 1 # sets 60 and 61, 1/60 s apart


def test_the_sample_takes_every_reason_and_keeps_its_gap(log):
	path, _, _ = log
	s = liveflag.Settings(logs=[path], budget=10, min_gap_s=0.2, random=0.1)
	lg = liveflag.Log(path, s)
	flags = liveflag.FlagLog(lg, s)
	events = liveflag.Events(flags, s)
	picked = liveflag.Sample([lg], flags, events, s)
	assert len(picked) == 10
	why = [p["why"] for p in picked]
	# 7 moments to pick from (Camera1's fewer is its missed animal's sets): the other 3 at random
	assert why.count("random") == 3
	assert {"missed", "swap", "jump", "unmatched", "disagree", "extra", "low_score"} <= set(why)
	t = np.sort(lg.time_s[[p["row"] for p in picked]])
	assert np.all(np.diff(t) >= 0.2 - 1e-9)


def test_a_small_budget_goes_to_the_first_reasons(log):
	path, _, _ = log
	s = liveflag.Settings(logs=[path], budget=2, random=0.0, min_gap_s=0.2)
	lg = liveflag.Log(path, s)
	flags = liveflag.FlagLog(lg, s)
	picked = liveflag.Sample([lg], flags, liveflag.Events(flags, s), s)
	assert [p["why"] for p in picked] == ["missed", "swap"]


def test_main_writes_flags_events_and_the_sample(log, tmp_path, capsys):
	path, _, _ = log
	out = str(tmp_path / "out")
	liveflag.Main([path, "--out", out, "--budget", "8", "--min-gap-s", "0.2"])
	with open(os.path.join(out, "flags.csv")) as f:
		rows = list(csv.DictReader(f))
	assert {r["reason"] for r in rows} >= {"missed", "swap", "jump", "disagree", "unmatched", "extra"}
	assert {r["camera"] for r in rows if r["reason"] == "missed"} == {"Camera1"}
	with open(os.path.join(out, "events.csv")) as f:
		events = list(csv.DictReader(f))
	assert sum(int(e["picked"]) for e in events) == 7
	with open(os.path.join(out, "sample.json")) as f:
		sample = json.load(f)
	assert sample["kind"] == "campy-live-flag sample" and len(sample["sets"]) == 8
	assert sample["logs"][0]["calibration"] is True
	missed = [x for x in sample["sets"] if x["why"] == "missed"][0]
	assert any(f["reason"] == "missed" and f["camera"] == "Camera1" for f in missed["flags"])
	text = capsys.readouterr().out
	assert "Picked 8 sets to label: 7 for their flags, 1 at random." in text


def test_a_long_log_is_quick(tmp_path):
	# An hour at 60 fps, 3 cameras, 5 animals, every set flagged somewhere
	import time
	cams = rig()
	write_calibration(str(tmp_path / "calibration.toml"), cams)
	base = make_rows(cams, scene3d.scene(SETS, n=ANIMALS))
	rows = np.tile(base, 60 * 60 * 60 // SETS)
	rows["frame"] = np.arange(1, len(rows) + 1)
	rows["host_ns"] = 10 ** 12 + rows["frame"][:, None] * PERIOD
	path = str(tmp_path / "hour")
	np.save(path + ".npy", rows)
	with open(path + ".json", "w") as f:
		json.dump({"cameras": CAMERAS, "calibration": str(tmp_path / "calibration.toml"),
			"calibration_names": CALIBRATION_NAMES, "animals": ANIMALS}, f)
	t = time.perf_counter()
	liveflag.Main([path + ".npy", "--out", str(tmp_path / "out")])
	assert time.perf_counter() - t < 120
