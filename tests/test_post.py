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


# ------------------------------------------------------------------ copy and run

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


def test_a_copy_that_takes_a_while_is_done_though_post_json_changed_after_it(tmp_path, monkeypatch):
	# As on Windows, where robocopy takes over the half second between post.json's saves: the copy step's own lines
	# after the copy save post.json again, so the copied one is older; only post's own files may differ like that
	folder = recording(tmp_path / "rec")
	real = subprocess.run

	def slow(command, *args, **kwargs):
		if command and command[0] in ("robocopy", "rsync"):
			time.sleep(0.7)
		return real(command, *args, **kwargs)
	monkeypatch.setattr(post.subprocess, "run", slow)
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
	assert out.returncode == 1 and "--only takes remux, join, align, calibrate, export, copy, run" in out.stderr

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
