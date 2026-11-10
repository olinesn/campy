"""
campy/calibrate.py (campy-calibrate, the post step calibrate) and what uses its calibrations: the live config's
folder of calibrations, and the panel's check. The headless calibrat3 run itself needs Chrome, Playwright and a
calibrat3 checkout: it runs only with CAMPY_CALIBRAT3 (the checkout) and CAMPY_CALIBRATION_SESSION (a calibration
recording) set.
"""

import datetime
import json
import os
import shutil

import pytest

from campy import calibrate, panel, post, session


def calibration_recording(folder, cameras=("Camera0_topB", "Camera1_topC"), made=None, ok=True, toml=True):
	for name in cameras:
		sub = os.path.join(folder, name, "calibration_images")
		os.makedirs(sub, exist_ok=True)
		open(os.path.join(sub, "calibration.mp4"), "wb").close()
	if made is not None:
		with open(os.path.join(folder, calibrate.SUMMARY), "w") as f:
			json.dump({"schema": 1, "made": made, "ok": ok, "median_px": 0.1, "cameras": {name: {"median_px": 0.1 +
				0.1 * i} for i, name in enumerate(cameras)}}, f)
	if toml:
		with open(os.path.join(folder, calibrate.CALIBRATION), "w") as f:
			f.write('[cam_0]\nname = "Camera0_topB"\n')
	return folder


def test_a_calibration_recordings_videos_and_its_board(tmp_path):
	folder = calibration_recording(str(tmp_path / "rec"), toml=False)
	os.makedirs(os.path.join(folder, "logs"))
	assert sorted(calibrate.CalibrationVideos(folder)) == ["Camera0_topB", "Camera1_topC"]
	assert calibrate.CalibrationVideos(str(tmp_path / "missing")) == {}
	assert calibrate.BoardToml(None) == ("board_x = 14\nboard_y = 9\nsquare_length = 20.0\nmarker_length = 15.0\n"
		"marker_bits = 5\ndict_size = 1000\n")
	assert "board_x = 8\n" in calibrate.BoardToml({"board_x": 8})


def test_the_newest_whole_calibration_of_a_folder(tmp_path):
	root = tmp_path / "calibrations"
	calibration_recording(str(root / "2026-11-08_09-00-00"), made="2026-11-08T09:01:00-05:00")
	calibration_recording(str(root / "2026-11-09_09-00-00"), made="2026-11-09T09:01:00-05:00")
	calibration_recording(str(root / "2026-11-09_17-00-00"), made="2026-11-09T17:01:00-05:00", ok=False) # incomplete
	calibration_recording(str(root / "2026-11-10_09-00-00"), toml=False) # not calibrated (yet)
	path, summary = calibrate.Newest(str(root))
	assert path == str(root / "2026-11-09_09-00-00" / "calibration.toml") and summary["ok"]
	assert session.LiveCalibration(str(root)) == path
	assert session.LiveCalibration(path) == path # a file, as it is
	assert calibrate.Newest(str(tmp_path / "nothing")) == (None, None)
	(tmp_path / "empty").mkdir()
	with pytest.raises(session.SessionError, match="holds no calibration campy-calibrate made whole"):
		session.LiveCalibration(str(tmp_path / "empty"))
	assert calibrate.Describe(summary) == ("made 9 Nov 9:01 AM, 2 cameras, reprojection median 0.10-0.20 px "
		"(worst Camera1_topC)")
	now = datetime.datetime.fromisoformat("2026-11-10T09:01:00-05:00")
	assert calibrate.AgeHours(summary, now) == 24.0


def test_the_panels_check_says_which_calibration_and_wants_one_a_day(tmp_path):
	root = tmp_path / "calibrations"
	calibration_recording(str(root / "2026-11-09_09-00-00"), made="2026-11-09T09:01:00-05:00")
	at = lambda text: datetime.datetime.fromisoformat(text)
	level, text = panel.CalibrationCheck(str(root), now=at("2026-11-09T16:00:00-05:00"))
	assert level == "ok" and "2026-11-09_09-00-00" in text and "made 9 Nov 9:01 AM" in text
	assert calibrate.When("2026-11-09T17:05:00-05:00") == "9 Nov 5:05 PM"
	level, text = panel.CalibrationCheck(str(root), now=at("2026-11-10T12:00:00-05:00"))
	assert level == "warn" and "27 hours old. Record a calibration today" in text
	level, text = panel.CalibrationCheck(str(root), now=at("2026-11-13T12:00:00-05:00"))
	assert level == "warn" and "4 days old" in text
	(tmp_path / "empty").mkdir()
	assert panel.CalibrationCheck(str(tmp_path / "empty"))[0] == "block"
	assert panel.CalibrationCheck(str(tmp_path / "gone.toml")) == ("block", "3D calibration: {} isn't there".format(
		tmp_path / "gone.toml"))


def test_the_post_step_only_for_a_calibration_recording_and_when_asked(tmp_path, monkeypatch):
	class Log:
		def Say(self, text):
			pass
	plain = tmp_path / "plain"
	(plain / "Camera0").mkdir(parents=True)
	assert post.StepCalibrate(str(plain), {"calibrate": {"calibrat3": "x"}}, Log()) == ("skipped",
		"not a calibration recording")
	rec = calibration_recording(str(tmp_path / "rec"), toml=False)
	state, text = post.StepCalibrate(rec, {}, Log())
	assert state == "skipped" and "calibrate is off" in text
	state, text = post.StepCalibrate(rec, {"calibrate": {"calibrat3": str(tmp_path / "no-checkout")}}, Log())
	assert state == "failed" and "no calibrat3 checkout" in text
	# What calibrat3 gave: every camera calibrated, or not
	def fake(folder, calibrat3, **kw):
		return {"ok": True, "median_px": 0.08, "seconds": 37.2, "cameras": {"Camera0_topB": {"median_px": 0.07},
			"Camera1_topC": {"median_px": 0.09}}}
	monkeypatch.setattr(calibrate, "Calibrate", fake)
	assert post.StepCalibrate(rec, {"calibrate": {"calibrat3": "c3"}}, Log()) == ("done", "calibration.toml for 2 "
		"cameras, by calibrat3: reprojection median 0.08 px, worst Camera1_topC 0.09 px (37 s)")
	monkeypatch.setattr(calibrate, "Calibrate", lambda folder, calibrat3, **kw: {"ok": False, "why": "not calibrated: "
		"Camera1_topC"})
	assert post.StepCalibrate(rec, {"calibrate": {"calibrat3": "c3"}}, Log()) == ("failed", "calibration.toml "
		"incomplete: not calibrated: Camera1_topC")
	assert post.STEPS.index("calibrate") == post.STEPS.index("align") + 1


def test_the_config_takes_the_calibrate_step():
	assert session.POST["calibrate"][1] == {}


@pytest.mark.skipif(not (os.environ.get("CAMPY_CALIBRAT3") and os.environ.get("CAMPY_CALIBRATION_SESSION")),
	reason="needs CAMPY_CALIBRAT3 (a calibrat3 checkout), CAMPY_CALIBRATION_SESSION (a calibration recording), Chrome "
	"and Playwright")
def test_calibrat3_headless_on_a_real_calibration_recording(tmp_path):
	folder = tmp_path / "rec"
	shutil.copytree(os.environ["CAMPY_CALIBRATION_SESSION"], str(folder),
		ignore=shutil.ignore_patterns("calibration*.toml", calibrate.SUMMARY, "calibrat3_*"))
	summary = calibrate.Calibrate(str(folder), os.environ["CAMPY_CALIBRAT3"], say=lambda text: None)
	assert summary["ok"] and (folder / "calibration.toml").is_file()
	assert all(c["median_px"] < 1 for c in summary["cameras"].values()), summary["cameras"]
	with pytest.raises(calibrate.CalibrateError, match="is there already"):
		calibrate.Calibrate(str(folder), os.environ["CAMPY_CALIBRAT3"], say=lambda text: None)


# ------------------------------------------------------------------ before a calibration recording starts

def test_what_this_computer_lacks_for_calibrat3(tmp_path):
	checkout = tmp_path / "calibrat3"
	checkout.mkdir()
	(checkout / "index.html").write_text("<html></html>")
	assert calibrate.Missing(str(checkout), playwright=True, chrome=lambda: "chrome.exe") == []
	missing = calibrate.Missing(str(tmp_path / "nowhere"), playwright=False, chrome=lambda: None)
	assert len(missing) == 3 and missing[0].startswith("calibrat3: no checkout at ")
	assert "pip install playwright" in missing[1] and missing[2].startswith("Google Chrome")
	# Playwright's own Chromium (browser: chromium) needs no installed Chrome
	assert calibrate.Missing(str(checkout), browser="chromium", playwright=True, chrome=lambda: None) == []


def a_config(tmp_path, video="calibration.mp4", calibrate_=None):
	text = "numCams: 2\ncameraMake: emu\nvideoFilename: {}\n".format(video)
	if calibrate_ is not None:
		text += "post:\n  calibrate: {}\n".format(json.dumps(calibrate_))
	(tmp_path / "c.yaml").write_text(text)
	return session.Config(str(tmp_path / "c.yaml"))


def test_the_panel_checks_a_calibration_recording_can_be_calibrated(tmp_path):
	# Before 10 Nov 2026 a rig without calibrat3 or Playwright recorded the calibration, and its calibration.toml
	# failed only after it (the ChronicRecord rig had neither)
	assert panel.CalibrateCheck(a_config(tmp_path, video="0.mp4")) is None # not a calibration recording
	level, text = panel.CalibrateCheck(a_config(tmp_path))
	assert level == "warn" and "post: calibrate: isn't set" in text
	on = a_config(tmp_path, calibrate_={"calibrat3": "C:/campy_data/live/calibrat3"})
	level, text = panel.CalibrateCheck(on, missing=lambda checkout, browser: ["Playwright's Python package"])
	assert level == "block" and text.endswith("Missing: Playwright's Python package")
	assert panel.CalibrateCheck(on, missing=lambda checkout, browser: []) == ("ok", "Calibration: calibrat3 makes "
		"calibration.toml after the recording (C:/campy_data/live/calibrat3)")
