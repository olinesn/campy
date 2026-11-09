"""
campy/endsummary.py: what went wrong in a recording that has ended, for the
panel's After the recording card, from fake recording folders shaped like
campy's own (metadata.csv, logs/campy-acquire.log, alerts.log).
"""

import csv
import json
import os

from campy import endsummary, panel

NAMES = ["Camera0_topB", "Camera1_topC", "Camera2_mid", "Camera3_sideC", "Camera4_topR", "Camera5_topL",
	"Camera6_sideR", "Camera7_sideL"]
# The 8-camera recording of 9 November 2026 (Dell Pro Tower Plus, NVENC H.265 quality 5): 3,285 frames grabbed by
# each camera, and these dropped
DROPPED = {"Camera1_topC": 506, "Camera2_mid": 501, "Camera3_sideC": 556, "Camera5_topL": 563, "Camera6_sideR": 536,
	"Camera7_sideL": 539}


def metadata(folder, name, **totals):
	os.makedirs(os.path.join(folder, name), exist_ok=True)
	rows = dict({"cameraName": name, "writeQueueMaxSec": 5.0, "frameRate": 60}, **totals)
	with open(os.path.join(folder, name, "metadata.csv"), "w", newline="") as f:
		w = csv.writer(f, quoting=csv.QUOTE_ALL)
		for row in rows.items():
			w.writerow(row)


def closed(folder, name, grabbed, dropped=0, **more):
	metadata(folder, name, totalFrames=grabbed, totalTime=grabbed / 60.0, framesWritten=grabbed - dropped,
		framesDropped=dropped, **more)


def alert(key, severity, event, state, text, time="2026-11-09T14:34:51-05:00"):
	return json.dumps({"time": time, "key": key, "severity": severity, "event": event, "state": state,
		"status": "below every channel's min_severity", "text": text}) + "\n"


def recording(tmp_path, dropped=DROPPED, alerts=""):
	folder = str(tmp_path / "2026-11-09_14-34-22")
	for name in NAMES:
		closed(folder, name, 3285, dropped.get(name, 0))
	with open(os.path.join(folder, "alerts.log"), "w") as f:
		f.write(alerts)
	return folder


def texts(summary, level=None):
	return [x["text"] for x in summary["lines"] if level is None or x["level"] == level]


def test_a_clean_recording_is_one_line(tmp_path):
	folder = recording(tmp_path, dropped={}, alerts=alert(None, "info", "note", None, "Recording started: 8 cameras"))
	s = endsummary.Summary(folder)
	assert s["level"] == "ok" and s["cameras"] == 8
	assert texts(s) == ["All 8 cameras: every frame saved (3,285 frames each)"]


def test_dropped_frames_and_the_critical_alerts_still_open(tmp_path):
	# As on 9 November: frames waiting for ffmpeg, then dropped on 6 of the 8 cameras, still open as it ended
	log = (alert("cameras/queue", "warning", "problem", "open", "6 cameras at once: frames waiting for ffmpeg. The disk "
		"or the encoder has stalled.\n- Camera1_topC: 2.9 s of frames waiting") +
		alert("cameras/queue", "critical", "worse", "open", "worse: 6 cameras at once: frames waiting for ffmpeg. The "
			"disk or the encoder has stalled.\n- Camera1_topC: 3.4 s", time="2026-11-09T14:34:53-05:00") +
		alert("cameras/drops", "critical", "problem", "open", "2 cameras at once: frames dropped: more than "
			"writeQueueMaxSec (5 s) of frames were waiting for ffmpeg.\n- Camera2_mid: dropped 18 frames") +
		alert(None, "critical", "note", None, "Recording ended after 1 min 8 s, with problems still open"))
	s = endsummary.Summary(recording(tmp_path, alerts=log))
	assert s["level"] == "bad"
	assert texts(s)[0] == ("6 of 8 cameras dropped frames: Camera1_topC 506 of 3,285, Camera2_mid 501 of 3,285, "
		"Camera3_sideC 556 of 3,285, Camera5_topL 563 of 3,285, Camera6_sideR 536 of 3,285, Camera7_sideL 539 of 3,285 "
		"(the encoder fell more than 5 s behind: writeQueueMaxSec)")
	assert texts(s)[1:] == [
		"Critical at 14:34:53, still open at the end: 6 cameras at once: frames waiting for ffmpeg. The disk or the encoder "
		"has stalled.",
		"Critical at 14:34:51, still open at the end: 2 cameras at once: frames dropped: more than writeQueueMaxSec (5 s) of "
		"frames were waiting for ffmpeg."]


def test_a_critical_alert_resolved_while_it_recorded_is_kept_and_warnings_only_when_open(tmp_path):
	log = (alert("Camera3_sideC/no-frames", "critical", "problem", "open", "Camera3_sideC: no frames for 10 s",
		time="2026-11-09T14:34:40-05:00") +
		alert("Camera3_sideC/no-frames", "critical", "resolved", "resolved", "Camera3_sideC: frames again",
			time="2026-11-09T14:34:52-05:00") +
		alert("disk/low", "warning", "problem", "open", "Disk: 40 GB free") +
		alert("Camera1_topC/late", "warning", "problem", "open", "Camera1_topC: late frames") +
		alert("Camera1_topC/late", "warning", "resolved", "resolved", "Camera1_topC: on time again") +
		"not json\n")
	s = endsummary.Summary(recording(tmp_path, dropped={}, alerts=log))
	assert s["level"] == "bad"
	assert texts(s) == ["All 8 cameras: every frame saved (3,285 frames each)",
		"Critical at 14:34:40, resolved at 14:34:52: Camera3_sideC: no frames for 10 s",
		"Warning at 14:34:51, still open at the end: Disk: 40 GB free"]
	assert [x["level"] for x in s["lines"]] == ["ok", "bad", "warn"]


def test_a_camera_without_totals_takes_them_from_the_acquire_log(tmp_path):
	folder = recording(tmp_path, dropped={})
	metadata(folder, "Camera2_mid") # its metadata.csv could not take its totals
	metadata(folder, "Camera4_topR") # never closed, and said nothing
	os.makedirs(os.path.join(folder, "logs"))
	with open(os.path.join(folder, "logs", "campy-acquire.log"), "w") as f:
		f.write("[acquire] Camera0_topB saved 3285 frames at 60 fps.\n"
			"[acquire] Camera2_mid grabbed 3,285 frames at 60 fps; its 2 video files hold 2700 (501 dropped, 84 lost in "
			"a stopped ffmpeg).\n")
	s = endsummary.Summary(folder)
	assert texts(s) == [
		"1 of 8 cameras dropped frames: Camera2_mid 501 of 3,285 (the encoder fell more than 5 s behind: "
		"writeQueueMaxSec)",
		"1 of 8 cameras lost frames in a stopped ffmpeg: Camera2_mid 84 of 3,285",
		"Camera4_topR stopped without closing (no totals in its metadata.csv)"]


def test_errors_lost_frames_and_no_cameras(tmp_path):
	folder = str(tmp_path / "rec")
	closed(folder, "Camera0", 600, flirFramesLost=99)
	closed(folder, "Camera1", 600, videoError="its ffmpeg exited with code 1")
	closed(folder, "Camera2", 0)
	s = endsummary.Summary(folder)
	assert texts(s) == ["1 of 3 cameras lost frames before they reached campy (flirFramesLost): Camera0 99",
		"Camera1: its ffmpeg exited with code 1", "Camera2 saved no frames"]
	empty = tmp_path / "empty"
	empty.mkdir()
	assert texts(endsummary.Summary(str(empty))) == ["No camera saved anything: no camera folder with a metadata.csv"]
	assert endsummary.Summary(str(tmp_path / "missing")) is None


def test_the_cache_reads_again_only_when_a_file_changes(tmp_path):
	folder = recording(tmp_path, dropped={})
	cache = endsummary.Cache()
	first = cache.Get(folder)
	assert cache.Get(folder) is first
	with open(os.path.join(folder, "alerts.log"), "a") as f:
		f.write(alert("disk/low", "critical", "problem", "open", "Disk: 2 GB free"))
	assert cache.Get(folder)["level"] == "bad"
	assert cache.Get(None) is None


def test_a_panel_opened_again_shows_the_last_recording_that_ended(tmp_path, monkeypatch):
	home = tmp_path / "home"
	monkeypatch.setenv("HOME", str(home))
	monkeypatch.setenv("USERPROFILE", str(home))
	folder = recording(tmp_path)
	status_dir = home / ".campy" / "status"
	status_dir.mkdir(parents=True)

	def launcher(state, acquire):
		(status_dir / "launcher.json").write_text(json.dumps({"schema": 1, "state": state, "folder": folder,
			"parts": {"acquire": {"state": acquire, "exit_code": 0}}, "post": {}}))
		with open(os.path.join(folder, "launcher.json"), "w") as f:
			f.write((status_dir / "launcher.json").read_text())

	assert panel.Previous() is None # none yet
	launcher("recording", "running")
	assert panel.Previous() is None # still recording: Orphan's
	launcher("done", "ended")
	last = panel.Previous()
	assert last["folder"] == folder and last["clean"] == {"clean": True, "why": ""}
	os.rename(folder, folder + ".moved")
	assert panel.Previous() is None # its folder has gone


def test_the_page_shows_the_summary():
	from campy import panelpage
	assert 'id="summary"' in panelpage.PAGE and "s.summary" in panelpage.PAGE


def test_the_pages_copy_step_says_copying_with_a_bar_and_the_time_left():
	# A copy under way was "waiting" on the After the recording card; now "copying", with a bar and an estimate
	from campy import panelpage
	assert '["done","failed","copying"].includes(CJ.state)' in panelpage.PAGE
	assert "function copyBar(j)" in panelpage.PAGE and "function copyEta(j)" in panelpage.PAGE
	assert "copyBar(j)+" in panelpage.PAGE # in the Copies card too


def test_copies_still_to_do_show_at_the_top_of_the_page_in_every_state():
	# A copy still running when the next recording starts stays obvious: a strip under the page's header, not only the
	# Copies card below the cameras
	from campy import panelpage
	page = panelpage.PAGE
	assert page.index('id="bgcopy"') < page.index('id="typebox"') < page.index('id="during"')
	assert '$("bgcopy").hidden = !now.length && !queued.length' in page
	assert "You can start a new recording now" in page # and it says so
