"""
The steps after a recording (campy/post.py): remux, align, export, copy, run, after
a clean stop only, and again by hand (campy-session post).
"""

import csv
import json
import os
import shutil
import subprocess
import sys
import time

import numpy as np
import pytest

from campy import post
from test_emu import write_clip

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAMERAS = ["Camera0", "Camera1"]


def ffmpeg():
	import imageio_ffmpeg
	return imageio_ffmpeg.get_ffmpeg_exe()


def fragmented_clip(path, frames=12):
	# As campy writes them (writer.EncoderArgs): fragmented, an empty moov
	plain = str(path) + ".plain.mp4"
	write_clip(plain, frames=frames)
	subprocess.run([ffmpeg(), "-v", "error", "-y", "-i", plain, "-c", "copy", "-movflags",
		"+frag_keyframe+empty_moov+delay_moov+default_base_moof", "-frag_duration", "200000", str(path)], check=True)
	os.remove(plain)


def write_metadata(path, **values):
	with open(str(path), "w", newline="") as f:
		w = csv.writer(f, quoting=csv.QUOTE_ALL)
		for row in dict({"cameraName": path.parent.name, "frameRate": 20}, **values).items():
			w.writerow(row)


def recording(tmp_path, clean=True, launcher_code=0):
	folder = tmp_path / "2026-10-23_10-00-00"
	for name in CAMERAS:
		(folder / name).mkdir(parents=True)
		fragmented_clip(folder / name / "0.mp4")
		write_metadata(folder / name / "metadata.csv", **({"framesWritten": 12} if clean else {}))
	if launcher_code is not None:
		(folder / "launcher.json").write_text(json.dumps({"parts": {"acquire": {"state": "ended",
			"exit_code": launcher_code}}}))
	return folder


def lines(out):
	return out.getvalue() if hasattr(out, "getvalue") else ""


# ------------------------------------------------------------------ remux

def test_a_fragmented_video_is_told_from_a_plain_one(tmp_path):
	fragmented_clip(tmp_path / "f.mp4")
	write_clip(str(tmp_path / "p.mp4"))
	assert post.Fragmented(str(tmp_path / "f.mp4"))
	assert not post.Fragmented(str(tmp_path / "p.mp4"))
	assert not post.Fragmented(str(tmp_path / "missing.mp4"))


def test_remux_gives_the_same_packets_in_a_plain_mp4(tmp_path):
	path = tmp_path / "0.mp4"
	fragmented_clip(path)
	before = post.Packets(ffmpeg(), str(path))
	said = post.Remux(str(path), ffmpeg())
	assert "12 packets, the same bit for bit" in said
	assert not post.Fragmented(str(path))
	assert post.Packets(ffmpeg(), str(path)) == before
	assert os.listdir(str(tmp_path)) == ["0.mp4"]


def test_remux_keep_leaves_the_fragmented_one_beside_it(tmp_path):
	path = tmp_path / "0.mp4"
	fragmented_clip(path)
	post.Remux(str(path), ffmpeg(), keep=True)
	assert sorted(os.listdir(str(tmp_path))) == ["0.fragmented.mp4", "0.mp4"]
	assert post.Fragmented(str(tmp_path / "0.fragmented.mp4")) and not post.Fragmented(str(path))


def test_a_copy_that_differs_is_never_put_in_place(tmp_path, monkeypatch):
	path = tmp_path / "0.mp4"
	fragmented_clip(path)
	original = path.read_bytes()
	real = post.Packets
	monkeypatch.setattr(post, "Packets", lambda ff, p: real(ff, p) + (["0, 99, 99, 1, 1, deadbeef"] if "remux-tmp" in p else []))
	with pytest.raises(post.PostError, match="is not the same video .* the fragmented video is kept as it was"):
		post.Remux(str(path), ffmpeg())
	assert path.read_bytes() == original and os.listdir(str(tmp_path)) == ["0.mp4"]


def test_no_room_for_the_copy_skips_the_video(tmp_path, monkeypatch):
	path = tmp_path / "0.mp4"
	fragmented_clip(path)
	original = path.read_bytes()
	monkeypatch.setattr(post.shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(100, 100, 1000))
	with pytest.raises(post.PostError, match="not enough free space"):
		post.Remux(str(path), ffmpeg())
	assert path.read_bytes() == original


def test_a_failed_ffmpeg_leaves_no_copy(tmp_path):
	path = tmp_path / "0.mp4"
	path.write_bytes(b"not a video")
	with pytest.raises(post.PostError, match="could not remux"):
		post.Remux(str(path), ffmpeg())
	assert os.listdir(str(tmp_path)) == ["0.mp4"]


def refused(monkeypatch, refuse, times=None):
	# os.replace as Windows does when another program (an antivirus scanner, an indexer, a viewer) holds a file:
	# PermissionError for the renames refuse(src, dst) picks, every time or the first `times` tries
	real, tries = os.replace, {"n": 0}
	def replace(src, dst):
		if refuse(str(src), str(dst)) and (times is None or tries["n"] < times):
			tries["n"] += 1
			raise PermissionError(32, "The process cannot access the file because it is being used by another process",
				str(src))
		return real(src, dst)
	monkeypatch.setattr(post.os, "replace", replace)
	monkeypatch.setattr(post, "REPLACE_WAIT_S", 0)
	return tries


def test_a_rename_refused_for_a_moment_is_tried_again(tmp_path, monkeypatch):
	path = tmp_path / "0.mp4"
	fragmented_clip(path)
	tries = refused(monkeypatch, lambda src, dst: ".remux-tmp" in src, times=3)
	post.Remux(str(path), ffmpeg(), keep=True)
	assert tries["n"] == 3
	assert sorted(os.listdir(str(tmp_path))) == ["0.fragmented.mp4", "0.mp4"] and not post.Fragmented(str(path))


@pytest.mark.parametrize("keep", [True, False])
def test_a_rename_always_refused_leaves_the_original_under_its_name(tmp_path, monkeypatch, keep):
	# remux: keep moved the original aside first: it is moved back, so the camera never ends up with no 0.mp4
	path = tmp_path / "0.mp4"
	fragmented_clip(path)
	original = path.read_bytes()
	refused(monkeypatch, lambda src, dst: ".remux-tmp" in src)
	with pytest.raises(post.PostError, match="could not put the remuxed copy .* the original is kept as it was"):
		post.Remux(str(path), ffmpeg(), keep=keep)
	assert os.listdir(str(tmp_path)) == ["0.mp4"] and path.read_bytes() == original


def test_one_camera_refused_does_not_stop_the_others(tmp_path, monkeypatch):
	folder = recording(tmp_path)
	refused(monkeypatch, lambda src, dst: "Camera0" in dst)
	said = []
	class Log:
		def Say(self, text):
			said.append(text)
	state, why = post.StepRemux(str(folder), {"remux": True}, Log(), ffmpeg=ffmpeg())
	assert state == "failed" and "1 of 2 videos not remuxed (Camera0)" in why
	assert post.Fragmented(str(folder / "Camera0" / "0.mp4")) and not post.Fragmented(str(folder / "Camera1" / "0.mp4"))
	assert any("Camera0" in line and "could not put the remuxed copy" in line for line in said)


def test_every_camera_video_is_found_calibration_ones_too(tmp_path):
	folder = recording(tmp_path)
	(folder / "Camera0" / "calibration_images").mkdir()
	fragmented_clip(folder / "Camera0" / "calibration_images" / "0-calibration.mp4")
	(folder / "Camera1" / "0.fragmented.mp4").write_bytes(b"kept from before")
	videos = [os.path.relpath(v, str(folder)) for v in post.Videos(str(folder))]
	assert videos == [os.path.join("Camera0", "0.mp4"), os.path.join("Camera0", "calibration_images", "0-calibration.mp4"),
		os.path.join("Camera1", "0.mp4")]


# ------------------------------------------------------------------ a clean stop

def test_what_a_clean_stop_is(tmp_path):
	assert post.CleanStop(str(recording(tmp_path / "a"))) == (True, "")
	assert post.CleanStop(str(recording(tmp_path / "b", launcher_code=None))) == (True, "") # campy-acquire alone
	ok, why = post.CleanStop(str(recording(tmp_path / "c", launcher_code=1)))
	assert not ok and why == "campy-acquire ended with exit code 1"
	ok, why = post.CleanStop(str(recording(tmp_path / "d", clean=False, launcher_code=None)))
	assert not ok and "Camera0 stopped without closing" in why
	folder = recording(tmp_path / "e", launcher_code=None)
	write_metadata(folder / "Camera1" / "metadata.csv", framesWritten=5, videoError="ffmpeg stopped after taking 5 frames")
	ok, why = post.CleanStop(str(folder))
	assert not ok and why == "Camera1: ffmpeg stopped after taking 5 frames"
	folder = recording(tmp_path / "g", launcher_code=None)
	write_metadata(folder / "Camera0" / "metadata.csv", framesWritten=12, frameTimesError="could not write all its "
		"frame times: frametimes.npy and hosttimes.npy have 8 of the video's 12 frames")
	ok, why = post.CleanStop(str(folder))
	assert not ok and why.startswith("Camera0: could not write all its frame times")
	(tmp_path / "f").mkdir()
	assert post.CleanStop(str(tmp_path / "f"))[0] is False


def test_after_a_crash_nothing_runs_and_the_command_is_said(tmp_path, capsys):
	folder = recording(tmp_path, launcher_code=1)
	results = post.Run(str(folder), {"remux": True, "export": True})
	assert {s: r[0] for s, r in results.items()} == {s: "skipped" for s in post.STEPS}
	out = capsys.readouterr().out
	assert "Post-steps skipped: the recording did not stop cleanly: campy-acquire ended with exit code 1." in out
	assert "campy-session post {} --force".format(folder) in out
	assert all(post.Fragmented(v) for v in post.Videos(str(folder))) # readable as they are, up to their last second
	results = post.Run(str(folder), {"remux": True, "export": False}, force=True, only=["remux"])
	assert results["remux"][0] == "done" and not any(post.Fragmented(v) for v in post.Videos(str(folder)))


def test_running_again_does_only_what_is_not_done(tmp_path, capsys):
	folder = recording(tmp_path)
	first = post.Run(str(folder), {"remux": True, "export": True, "run": ["exit 3"]})
	assert first["remux"][0] == "done" and first["export"] == ("skipped", "campy-live did not run (no campy-live.npy)")
	assert first["run"] == ("failed", "1 of 1 commands failed")
	again = post.Run(str(folder), {"remux": True, "export": True, "run": ["exit 0"]})
	assert again["remux"][0] == "done" and again["remux"][1].startswith("done already")
	assert again["run"] == ("done", "1 commands")
	state = json.loads((folder / "logs" / "post.json").read_text())
	assert state["steps"]["run"]["state"] == "done"
	log = (folder / "logs" / "post.log").read_text()
	assert "remux: done already" in log and "ended with exit code 3" in log


# ------------------------------------------------------------------ align

def frame_times(folder, missed=(), frames=12, rate=20):
	# frametimes.npy for frames triggers apart from the missed ones, and arrival times, as campy writes them
	k = np.array([k for k in range(1, frames + 1 + len(missed)) if k not in missed])
	np.save(str(folder / "frametimes.npy"), np.array([np.arange(1, len(k) + 1), (k - 1) / rate]))
	np.save(str(folder / "hosttimes.npy"), (10 ** 12 + (k - 1) * 10 ** 9 // rate + 5 * 10 ** 6).astype(np.int64))


def test_align_says_where_the_trigger_paused(tmp_path):
	folder = recording(tmp_path)
	for name in CAMERAS:
		write_metadata(folder / name / "metadata.csv", framesWritten=40, cameraTrigger="Line3", cameraMake="flir")
		frame_times(folder / name, missed=range(20, 25), frames=40)
	(folder / "trigger.json").write_text(json.dumps({"triggerStarted": True, "readyBeforeTrigger": CAMERAS}))
	results = post.Run(str(folder), {"remux": False}, only=["align"])
	assert results["align"] == ("done", "2 of 2 cameras on the trigger's timeline (alignment.json, aligned.csv); the "
		"trigger paused 1 time, 5 pulses (0.25 s) with no frame in any camera")
	summary = json.loads((folder / "alignment.json").read_text())
	assert summary["trigger_pauses"] == [{"first_trigger": 20, "triggers": 5, "seconds": 0.25, "at_s": 0.95}]
	assert (folder / "Camera0" / "aligned.csv").is_file()
	assert "The trigger paused 1 time" in (folder / "logs" / "post.log").read_text()


def test_align_with_nothing_wrong_and_with_nothing_to_align(tmp_path):
	folder = recording(tmp_path)
	for name in CAMERAS:
		write_metadata(folder / name / "metadata.csv", framesWritten=40, cameraTrigger="Line3", cameraMake="flir")
		frame_times(folder / name, frames=40)
	assert post.Run(str(folder), {}, only=["align"])["align"] == ("done", "2 of 2 cameras on the trigger's timeline "
		"(alignment.json, aligned.csv); no trigger pulse without a frame")
	free = recording(tmp_path / "free") # free-running cameras: no trigger to align to
	for name in CAMERAS:
		frame_times(free / name)
	state, message = post.Run(str(free), {}, only=["align"])["align"]
	assert state == "skipped" and message.startswith("nothing to align: ")
	assert post.Run(str(free), {"align": False}, only=["align"])["align"] == ("skipped", "align is off")


# ------------------------------------------------------------------ export

def live_log(path, frames, with_summary=True, cameras=2):
	dtype = np.dtype([("frame", "<i8"), ("keypoints", "<f4", (cameras, 2, 3, 2))])
	rows = np.zeros(len(frames), dtype)
	rows["frame"] = frames
	np.save(str(path), rows)
	if with_summary:
		with open(str(path)[:-4] + ".json", "w") as f:
			json.dump({"sets": len(frames), "cameras": CAMERAS, "nodes": ["a", "b", "c"]}, f)
	return rows


def test_campy_lives_runs_are_joined_in_order(tmp_path):
	live_log(tmp_path / "campy-live.npy", [1, 3, 5], with_summary=False) # killed: no summary
	live_log(tmp_path / "campy-live.part2.npy", [40, 41])
	live_log(tmp_path / "campy-live.part10.npy", [90])
	logs = post.LivePartLogs(str(tmp_path))
	assert [os.path.basename(p) for p in logs] == ["campy-live.npy", "campy-live.part2.npy", "campy-live.part10.npy"]
	out = str(tmp_path / "joined.npy")
	assert post.JoinLogs(logs, out) == (6, True)
	assert list(np.load(out)["frame"]) == [1, 3, 5, 40, 41, 90]
	summary = json.loads(open(out[:-4] + ".json").read())
	assert summary["sets"] == 6 and summary["cameras"] == CAMERAS
	assert summary["parts"] == ["campy-live.npy", "campy-live.part2.npy", "campy-live.part10.npy"]


def test_runs_that_do_not_fit_together_are_refused(tmp_path):
	a = live_log(tmp_path / "a.npy", [1, 50])
	live_log(tmp_path / "b.npy", [40])
	with pytest.raises(post.PostError, match="starts at frame 40, not after the frame 50"):
		post.JoinLogs([str(tmp_path / "a.npy"), str(tmp_path / "b.npy")], str(tmp_path / "j.npy"))
	live_log(tmp_path / "c.npy", [60], cameras=3)
	with pytest.raises(post.PostError, match="another model, or another camera count"):
		post.JoinLogs([str(tmp_path / "a.npy"), str(tmp_path / "c.npy")], str(tmp_path / "j.npy"))


def test_export_runs_campy_live_export_in_its_environment(tmp_path, monkeypatch):
	folder = recording(tmp_path)
	live_log(folder / "campy-live.npy", [1, 2])
	live_log(folder / "campy-live.part2.npy", [9])
	(folder / "logs").mkdir()
	ran = []
	def run(command, **kwargs):
		ran.append(command)
		log = command[command.index("--log") + 1]
		assert list(np.load(log)["frame"]) == [1, 2, 9] # the joined log, while it is there
		return subprocess.CompletedProcess(command, 0, "Wrote campy-live.slp\n", "")
	monkeypatch.setattr(post.subprocess, "run", run)
	log = post.Log(str(folder))
	assert post.StepExport(str(folder), {"export": True}, log, live_python="/live/python") == \
		("done", "LUC3D's files written (campy-live.slp)")
	command, = ran
	assert command[:4] == ["/live/python", "-m", "campy.liveexport", str(folder)]
	assert not (folder / "logs" / "campy-live.joined.npy").exists() # the joined copy removed
	assert post.StepExport(str(folder), {"export": True}, log, live_python=None)[0] == "failed"


# ------------------------------------------------------------------ cage

def calibrated(tmp_path):
	# A calibration of test_cagegrid's made-up box, in a calibration recording's folder as campy-calibrate leaves it
	import test_cagegrid
	path = tmp_path / "calibrations" / "2026-11-20_09-00-00" / "calibration.toml"
	path.parent.mkdir(parents=True)
	cams = test_cagegrid.rig()
	test_cagegrid.write_calibration(str(path), cams)
	return path, cams


def tracked(tmp_path, calibration, cage=True, name="rec"):
	# A recording campy-live tracked in 3D with calibration (campy-live-session.json, as it writes it)
	from campy import manifest, pose3d
	folder = recording(tmp_path / name)
	pose = {"animals": 2, "cage": pose3d.CagePath(str(calibration)) if cage else None}
	(folder / manifest.LIVE_FILE).write_text(json.dumps({"calibration": manifest.FileRecord(str(calibration)),
		"pose3d": pose}))
	return folder


def clicked_cage(path, cams, off_px=0.0):
	# A cage drawn by hand: its corners clicked where each camera sees them (off_px: as if on another calibration)
	from campy import pose3d
	import test_cagegrid
	corners = test_cagegrid.TRUE_CORNERS
	clicks = {c.name: [(c.Project(X) + off_px).tolist() for X in corners] for c in cams}
	pose3d.Cage(corners, [0, 0, 1], 200.0, 30.0, clicks, 0.0).Save(str(path))
	return open(str(path)).read()


def fake_campy_cage(code=0, lines=(), meanwhile=None):
	# campy-cage as RunCage runs it: its --out written (a cage made from the grid) when it ends with 0
	from campy import pose3d
	import test_cagegrid
	ran = []

	def run(command, log, stop=None):
		ran.append(command)
		out = command[command.index("--out") + 1]
		if code == 0:
			pose3d.Cage(test_cagegrid.TRUE_CORNERS, [0, 0, 1], 305.0, 30.0, {}, 0.0).Save(out, made="grid", grid={},
				fit={"floor_mm": [458.4, 254.2], "squares": [18.05, 10.01]}, identity={"swaps": {}, "weak": []})
		if meanwhile:
			meanwhile()
		for line in lines:
			log.Say(line)
		return code, list(lines)
	return run, ran


def test_the_cage_steps_options():
	assert post.CageOptions(None) is None and post.CageOptions({}) is None and post.CageOptions(False) is None
	assert post.CageOptions(True) == {"size": None, "height": None, "percentile": 50.0}
	assert post.CageOptions({"size": [18, 10], "height": 305, "percentile": 25}) == {"size": [18.0, 10.0],
		"height": 305.0, "percentile": 25.0}
	assert post.CageOptions({"size": "18x10"})["size"] == [18.0, 10.0]
	for bad, said in (({"size": [18]}, "size: \\[18\\] is not the floor's width and depth"),
			({"size": [18, True]}, "is not the floor's"), ({"height": -1}, "height: -1"),
			({"percentile": 0}, "percentile: 0"), ({"sise": [18, 10]}, "only, not sise"), ("yes", "or true")):
		with pytest.raises(ValueError, match=said):
			post.CageOptions(bad)


def test_the_cage_step_finds_the_calibration_campy_live_used(tmp_path):
	from campy import manifest
	calibration, _ = calibrated(tmp_path)
	folder = tracked(tmp_path, calibration)
	used = post.UsedCalibration(str(folder))
	assert used["calibration"] == str(calibration) and used["sha256"] == manifest.Sha256(str(calibration))
	assert used["cage"] == str(calibration)[:-5] + ".cage.json"
	assert post.UsedCalibration(str(tracked(tmp_path, calibration, cage=False, name="off")))["cage"] is False
	# Only campy-live's summary (no campy-live-session.json): its calibration, and the cage it looked at
	summary = recording(tmp_path / "summary")
	live_log(summary / "campy-live.npy", [1, 2])
	(summary / "campy-live.json").write_text(json.dumps({"calibration": str(calibration), "cage": [
		{"from_frame": None, "path": "/x/c.cage.json", "cage": None, "refused": None}]}))
	assert post.UsedCalibration(str(summary)) == {"calibration": str(calibration), "sha256": None,
		"cage": "/x/c.cage.json", "known": True}
	os.remove(str(summary / "campy-live.json"))
	assert post.UsedCalibration(str(summary))["known"] is False # campy-live killed before its summary
	assert post.UsedCalibration(str(recording(tmp_path / "none"))) is None


def test_the_cage_step_skips_what_it_must_not_touch(tmp_path):
	from campy import pose3d
	calibration, cams = calibrated(tmp_path)
	folder = tracked(tmp_path, calibration)
	log = post.Log(str(folder))
	on = {"cage": {"size": [12, 8]}}
	run, ran = fake_campy_cage()
	step = lambda where, settings=on: post.StepCage(str(where), settings, log, run=run)
	assert step(folder, {}) == ("skipped", "cage is off (post: cage: {size: [W, D]} turns it on, for a box with a "
		"1-inch grid)")
	assert step(recording(tmp_path / "plain")) == ("skipped", "campy-live did not run (no campy-live.npy)")
	assert step(tracked(tmp_path, calibration, cage=False, name="off")) == ("skipped", "campy-live ran with --cage off")
	two_d = recording(tmp_path / "2d")
	live_log(two_d / "campy-live.npy", [1])
	(two_d / "campy-live.json").write_text(json.dumps({"calibration": None, "cage": None}))
	assert step(two_d) == ("skipped", "campy-live tracked in 2D only (no calibration)")
	calib_rec = tracked(tmp_path, calibration, name="calib")
	(calib_rec / "Camera0" / "calibration_images").mkdir()
	write_clip(str(calib_rec / "Camera0" / "calibration_images" / "0-calibration.mp4"))
	assert step(calib_rec) == ("skipped", "a calibration recording: its board would stay in the pictures")
	# A cage drawn by hand, or made from the grid: never replaced
	cage = pose3d.CagePath(str(calibration))
	drawn = clicked_cage(cage, cams)
	assert step(folder) == ("skipped", "the calibration has a cage already (drawn by hand: {})".format(cage))
	assert open(cage).read() == drawn
	# One drawn on another calibration: not replaced either, but said, as campy-live doesn't use it
	clicked_cage(cage, cams, off_px=40.0)
	state, said = step(folder)
	assert state == "skipped" and "isn't used: its corners land 57 px from where they were clicked" in said
	assert "Not replaced" in said
	os.remove(cage)
	# The calibration changed since the recording was tracked with it
	with open(str(calibration), "a") as f:
		f.write("\n")
	state, said = step(folder)
	assert state == "skipped" and "changed since this recording was tracked with it" in said
	assert not ran and not os.path.exists(cage)


def test_the_cage_step_makes_the_cage_beside_the_calibration(tmp_path):
	from campy import cagegrid, pose3d
	calibration, cams = calibrated(tmp_path)
	folder = tracked(tmp_path, calibration)
	run, ran = fake_campy_cage()
	state, said = post.StepCage(str(folder), {"cage": {"size": [18, 10], "height": 300}}, post.Log(str(folder)),
		run=run)
	assert state == "done", said
	cage = pose3d.CagePath(str(calibration))
	assert said.startswith("the cage from the box's grid, floor 458 x 254 mm (18.05 x 10.01 squares), 305 mm high, "
		"beside {}: every recording with this calibration uses it; every camera's picture matches its own "
		"calibration. The check's pictures: {} (".format(calibration, os.path.join(cagegrid.CheckFolder(
		str(calibration)), "sheet.jpg")))
	command, = ran
	out = command[command.index("--out") + 1]
	assert command[:5] == [sys.executable, "-m", "campy.cagegrid", str(calibration), str(folder)]
	assert command[7:] == ["--size", "18,10", "--height", "300"] # the median: no --percentile
	assert out != cage and not os.path.exists(out) and os.path.dirname(out) == os.path.dirname(cage)
	assert json.load(open(cage))["made"] == "grid" and cagegrid.LoadGrid(cage) == {}
	# The next recording with this calibration has its cage
	assert post.StepCage(str(tracked(tmp_path, calibration, name="next")), {"cage": True}, post.Log(str(folder)),
		run=run) == ("skipped", "the calibration has a cage already (made from the grid: {})".format(cage))
	assert len(ran) == 1


def test_a_cage_drawn_while_campy_cage_ran_is_kept(tmp_path):
	# campy-live-view, kept open after the recording, saved a cage drawn by hand while campy-cage ran
	from campy import pose3d
	calibration, cams = calibrated(tmp_path)
	folder = tracked(tmp_path, calibration)
	cage = pose3d.CagePath(str(calibration))
	drawn = []
	run, _ = fake_campy_cage(meanwhile=lambda: drawn.append(clicked_cage(cage, cams)))
	state, said = post.StepCage(str(folder), {"cage": True}, post.Log(str(folder)), run=run)
	assert (state, said) == ("skipped", "a cage was drawn while campy-cage ran ({}): that one is kept, and "
		"campy-cage's not saved".format(cage))
	assert open(cage).read() == drawn[0]
	assert sorted(os.listdir(os.path.dirname(cage))) == ["calibration.cage.json", "calibration.toml"]


def test_campy_cages_refusals_are_the_steps_result_and_the_steps_after_run(tmp_path):
	from campy import cagegrid, pose3d
	calibration, _ = calibrated(tmp_path)
	folder = tracked(tmp_path, calibration)
	sheet = os.path.join(cagegrid.CheckFolder(str(calibration)), "sheet.jpg")
	swapped = "CAMERA NAMES SWAPPED? Each of these pictures matches another camera's calibration better than its own " \
		"(picture -> calibration): left -> top, top -> left. Check the cameras' order (cameraSerials) before tracking."
	run, _ = fake_campy_cage(3, ["ridge maps (1.0 s)", swapped, "Not saved: the pictures don't match their cameras' "
		"calibrations (--force saves it anyway)."])
	state, said = post.StepCage(str(folder), {"cage": True}, post.Log(str(folder)), run=run)
	assert state == "skipped" and said == "not saved: {} This recording's 3D is wrong for those cameras. The check's " \
		"pictures: {}. The next recording with this calibration tries again.".format(swapped, sheet)
	run, _ = fake_campy_cage(4, ["Not saved: the grid lands on most cameras' lines by less than x2"])
	state, said = post.StepCage(str(folder), {"cage": True}, post.Log(str(folder)), run=run)
	assert state == "skipped" and said.startswith("not saved: the grid lands on most cameras' lines by less than x2, "
		"so the fit found something else") and sheet in said
	run, _ = fake_campy_cage(1, ["3 cameras' pictures", "No cage: no wall with a grid found", ""])
	assert post.StepCage(str(folder), {"cage": True}, post.Log(str(folder)), run=run) == ("failed",
		"campy-cage ended with exit code 1: No cage: no wall with a grid found")
	assert sorted(os.listdir(os.path.dirname(str(calibration)))) == ["calibration.toml"] # nothing left behind
	# In post.Run: the step's result, and the steps after it run
	run, _ = fake_campy_cage(4)
	real = post.StepCage
	post_step = lambda *a, **k: real(*a, run=run, **k)
	import unittest.mock
	with unittest.mock.patch.object(post, "StepCage", post_step):
		results = post.Run(str(folder), {"cage": True, "run": ["exit 0"]}, only=["cage", "run"],
			out=open(os.devnull, "w"))
	assert results["cage"][0] == "skipped" and results["run"] == ("done", "1 commands")
	state = json.loads((folder / "logs" / "post.json").read_text())
	assert state["steps"]["cage"]["state"] == "skipped" and "not saved" in state["steps"]["cage"]["message"]


def test_campy_cages_lines_go_into_post_log_with_how_far_it_has_got(tmp_path):
	folder = recording(tmp_path)
	log = post.Log(str(folder), out=open(os.devnull, "w"))
	seen = []
	log.on_line = lambda text, fraction: seen.append((text, fraction))
	py = "import sys; print('5 cameras\\' pictures from x (1.0 s)'); print('ridge maps (0.5 s)'); print('Saved'); " \
		"sys.exit(0)"
	code, lines = post.RunCage([sys.executable, "-c", py], log)
	assert code == 0 and lines == ["5 cameras' pictures from x (1.0 s)", "ridge maps (0.5 s)", "Saved"]
	assert [f for _, f in seen if f is not None] == [0.35, 0.4]
	assert "  ridge maps (0.5 s)" in (folder / "logs" / "post.log").read_text()
	code, _ = post.RunCage([sys.executable, "-c", "print('a'); print('b')"], log, stop=lambda: True)
	assert code is None # stopped (Ctrl-C) at its first line


def test_the_cage_step_end_to_end_with_campy_cage(tmp_path, monkeypatch):
	# campy-cage itself, on a recording of the made-up box: each camera's video, a bright mouse going round the floor
	# (gone in the median of its frames); the cage made once, then used by the next recording
	import cv2
	from campy import pose3d, videoread
	import test_cagegrid
	monkeypatch.setenv("PYTHONPATH", REPO)
	calibration, cams = calibrated(tmp_path)
	folder = tmp_path / "2026-11-20_10-00-00"
	n = 40
	for i, cam in enumerate(cams):
		camera = folder / "Camera{}_{}".format(i, cam.name)
		camera.mkdir(parents=True)
		write_metadata(camera / "metadata.csv", framesWritten=n)
		still = test_cagegrid.render(cam, seed=i)
		frames = []
		for k in range(n):
			X = np.array([100 * np.cos(2 * np.pi * k / n), 60 * np.sin(2 * np.pi * k / n), 20.0])
			frame = still.copy()
			u, v = cam.Project(X)
			cv2.circle(frame, (int(u), int(v)), 18, 250, -1)
			frames.append(frame)
		h, w = still.shape
		subprocess.run([videoread.FfmpegExe(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s",
			"{}x{}".format(w, h), "-r", "20", "-i", "-", "-c:v", "libx264", "-g", "10", "-crf", "8", "-pix_fmt",
			"yuv420p", str(camera / "0.mp4")], input=np.stack(frames).tobytes(), check=True)
	from campy import manifest
	(folder / manifest.LIVE_FILE).write_text(json.dumps({"calibration": manifest.FileRecord(str(calibration)),
		"pose3d": {"animals": 1, "cage": pose3d.CagePath(str(calibration))}}))
	results = post.Run(str(folder), {"cage": {"size": [12, 8]}}, only=["cage"], out=open(os.devnull, "w"))
	state, said = results["cage"]
	assert state == "done", (folder / "logs" / "post.log").read_text()
	assert said.startswith("the cage from the box's grid, floor ") and "mm high, beside {}".format(calibration) in said
	cage = pose3d.Cage.Load(pose3d.CagePath(str(calibration)))
	for X in test_cagegrid.TRUE_CORNERS:
		assert test_cagegrid.nearest(cage.corners, X) < 3.0
	assert cage.Check(cams) is None # campy-live uses it on this calibration
	log = (folder / "logs" / "post.log").read_text()
	assert "campy-cage: " in log and "  ridge maps (" in log and "Every camera's picture matches its own calibration" \
		in log
	assert os.path.isfile(os.path.join(str(calibration)[:-5] + ".cage_check", "sheet.jpg"))

def test_copy_puts_the_whole_folder_under_copy_to(tmp_path):
	folder = recording(tmp_path / "rec")
	(folder / "logs").mkdir()
	(folder / "logs" / "campy-acquire.log").write_text("its log\n")
	dest = tmp_path / "nas"
	state, message = post.StepCopy(str(folder), {"copy": {"to": str(dest)}}, post.Log(str(folder)))
	assert state == "done", message
	copied = dest / folder.name
	for name in CAMERAS:
		assert (copied / name / "0.mp4").read_bytes() == (folder / name / "0.mp4").read_bytes()
	assert (copied / "logs" / "campy-acquire.log").read_text() == "its log\n"
	assert (folder / "Camera0" / "0.mp4").exists() # nothing deleted


@pytest.mark.skipif(sys.platform != "win32" and not shutil.which("rsync"), reason="no copy program: shutil copies")
def test_copy_says_which_process_copies(tmp_path):
	# The panel follows how much it has written (copyqueue.Written)
	folder = recording(tmp_path / "rec")
	seen = []
	state, message = post.StepCopy(str(folder), {"copy": {"to": str(tmp_path / "nas")}}, post.Log(str(folder)),
		started=seen.append)
	assert state == "done", message
	proc, = seen
	assert proc.pid > 0 and proc.returncode is not None # ended by the time the step returns


def test_a_copy_that_takes_a_while_is_done_though_post_json_changed_after_it(tmp_path, monkeypatch):
	# As on Windows, where robocopy takes over the half second between post.json's saves: the copy step's own lines
	# after the copy save post.json again, so the copied one is older; only post's own files may differ like that
	folder = recording(tmp_path / "rec")
	real = post.RunCopier

	def slow(command, *args, **kwargs):
		time.sleep(0.7)
		return real(command, *args, **kwargs)
	monkeypatch.setattr(post, "RunCopier", slow)
	dest = tmp_path / "nas"
	results = post.Run(str(folder), {"copy": {"to": str(dest)}}, only=["copy"], out=open(os.devnull, "w"))
	assert results["copy"][0] == "done", results
	assert (dest / folder.name / "Camera0" / "0.mp4").read_bytes() == (folder / "Camera0" / "0.mp4").read_bytes()


def test_run_replaces_folder_and_goes_on_after_a_failure(tmp_path):
	folder = recording(tmp_path)
	(folder / "logs").mkdir()
	marker = tmp_path / "marker.txt"
	py = '"{}"'.format(sys.executable) if sys.platform == "win32" else sys.executable
	commands = ["{} -c \"import sys; open(sys.argv[1], 'w').write(sys.argv[2])\" {} {{folder}}".format(py, marker),
		"{} -c \"raise SystemExit(4)\"".format(py),
		"{} -c \"print('third')\"".format(py)]
	state, message = post.StepRun(str(folder), {"run": commands}, post.Log(str(folder)))
	assert (state, message) == ("failed", "1 of 3 commands failed")
	assert marker.read_text() == str(folder)
	assert "third" in (folder / "logs" / "post.log").read_text()


# ------------------------------------------------------------------ campy-session post, by hand

def test_campy_session_post_by_hand(tmp_path):
	folder = recording(tmp_path, launcher_code=None) # recorded by campy-acquire alone
	env = dict(os.environ, PYTHONPATH=REPO, HOME=str(tmp_path), USERPROFILE=str(tmp_path))
	env.pop("CAMPY_LIVE_PYTHON", None)
	def campy_session(*args):
		return subprocess.run([sys.executable, "-m", "campy.session", "post", str(folder)] + list(args),
			capture_output=True, text=True, env=env, timeout=120)
	out = campy_session("--only", "remux")
	assert out.returncode == 0, out.stdout + out.stderr
	assert "remux: done, 2 videos remuxed to plain MP4" in out.stdout
	assert not any(post.Fragmented(v) for v in post.Videos(str(folder)))
	out = campy_session("--only", "remux")
	assert "remux: done already" in out.stdout
	out = campy_session("--only", "rmux")
	assert out.returncode == 1 and "--only takes remux, join, align, calibrate, cage, export, copy, run" in out.stderr

	crashed = recording(tmp_path / "crashed", clean=False, launcher_code=None)
	out = subprocess.run([sys.executable, "-m", "campy.session", "post", str(crashed)], capture_output=True, text=True,
		env=env, timeout=120)
	assert out.returncode == 1 and "did not stop cleanly" in out.stdout


def test_while_the_steps_run_post_json_says_which_and_how_far(tmp_path, monkeypatch):
	# For the panel's progress bar: the plan, the running step, its latest line and progress, written as it goes
	folder = recording(tmp_path)
	seen = {}

	def align(folder_, settings, log, stop=None):
		log.Say("lining up the cameras")
		seen["during"] = json.load(open(os.path.join(folder_, "logs", post.POST_JSON)))
		log.Progress("half way", 0.5)
		seen["progress"] = json.load(open(os.path.join(folder_, "logs", post.POST_JSON)))
		return "done", "aligned"
	monkeypatch.setattr(post, "StepAlign", align)
	post.Run(str(folder), {"remux": False, "join": False, "export": False}, only=["remux", "align", "run"])
	during = seen["during"]
	assert during["plan"] == ["remux", "align", "run"] and during["running"] == "align"
	assert during["steps"]["align"]["state"] == "running" and during["steps"]["remux"]["state"] == "skipped"
	assert seen["progress"]["steps"]["align"]["fraction"] == 0.5
	assert seen["progress"]["steps"]["align"]["message"] == "half way"
	after = json.load(open(os.path.join(str(folder), "logs", post.POST_JSON)))
	assert after["running"] is None and after["steps"]["align"] == dict(after["steps"]["align"], state="done",
		message="aligned")
	assert "fraction" not in after["steps"]["align"]
