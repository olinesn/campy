"""The panel's copies (campy/copyqueue.py): each finished recording copied into the page's folder, in the background,
one at a time; the result in the recording's post.json; the folder remembered per config; Close waits for them."""

import json
import os
import threading
import time
import types

import pytest

from campy import copyqueue, panel, post


@pytest.fixture(autouse=True)
def status_dir(tmp_path, monkeypatch):
	monkeypatch.setenv("CAMPY_STATUS_DIR", str(tmp_path / "status"))


def recording(tmp_path, name, size=1000):
	folder = tmp_path / "rec" / name
	(folder / "Camera0").mkdir(parents=True)
	(folder / "Camera0" / "0.mp4").write_bytes(os.urandom(size))
	(folder / "session.json").write_text("{}")
	return str(folder)


def finished(queue, timeout=30):
	t0 = time.monotonic()
	while queue.Pending():
		assert time.monotonic() - t0 < timeout, queue.Status()
		time.sleep(0.05)
	return queue.Status()


def quiet():
	return copyqueue.CopyQueue(say=lambda text: None, out=open(os.devnull, "w"))


def test_recordings_are_copied_one_after_another(tmp_path):
	to = tmp_path / "share"
	to.mkdir()
	queue = quiet()
	first, second = recording(tmp_path, "2026-11-09_10-27-20", 5000), recording(tmp_path, "2026-11-09_10-39-49")
	queue.Add(first, str(to))
	queue.Add(second, str(to))
	status = finished(queue)
	assert status["pending"] == 0
	assert [j["state"] for j in status["jobs"]] == ["done", "done"]
	for folder in (first, second):
		name = os.path.basename(folder)
		copied = to / name / "Camera0" / "0.mp4"
		assert copied.read_bytes() == open(os.path.join(folder, "Camera0", "0.mp4"), "rb").read()
		step = post.Load(folder)["steps"]["copy"]
		assert step["state"] == "done" and step["to"] == str(to), step
	job = [j for j in status["jobs"] if j["name"] == "2026-11-09_10-27-20"][0]
	assert job["bytes"] == job["done_bytes"] >= 5000


def test_a_copy_that_fails_says_so_in_post_json(tmp_path):
	not_a_folder = tmp_path / "a file"
	not_a_folder.write_text("x")
	queue = quiet()
	folder = recording(tmp_path, "2026-11-09_11-00-00")
	queue.Add(folder, str(not_a_folder))
	status = finished(queue)
	assert status["jobs"][0]["state"] == "failed"
	assert post.Load(folder)["steps"]["copy"]["state"] == "failed"


def test_closing_leaves_the_copies_not_done_and_says_how_to_make_them(tmp_path, monkeypatch):
	release = threading.Event()

	def slow(folder, settings, log, stop=None):
		release.wait(10)
		return "done", "copied"
	monkeypatch.setattr(post, "StepCopy", slow)
	queue = quiet()
	to = tmp_path / "share"
	to.mkdir()
	a, b = recording(tmp_path, "2026-11-09_12-00-00"), recording(tmp_path, "2026-11-09_12-20-00")
	queue.Add(a, str(to))
	queue.Add(b, str(to))
	t0 = time.monotonic()
	while queue.Status()["jobs"][-1]["state"] != "copying":
		assert time.monotonic() - t0 < 10
		time.sleep(0.02)
	left = queue.Leave()
	assert [j.name for j in left] == ["2026-11-09_12-00-00", "2026-11-09_12-20-00"]
	release.set()
	time.sleep(0.3)
	assert queue.Pending() == [] and [j["state"] for j in queue.Status()["jobs"]] == ["left", "left"]
	for folder in (a, b):
		step = post.Load(folder)["steps"]["copy"]
		assert step["state"] == "skipped"
		assert 'campy-session post "{}" --only copy --to "{}"'.format(folder, to) in step["message"]
	assert "may still finish" in post.Load(a)["steps"]["copy"]["message"] # it was copying


def test_the_folder_is_checked_before_a_start(tmp_path):
	videos = tmp_path / "videos"
	videos.mkdir()
	assert copyqueue.Problem("", str(videos)) is None
	assert copyqueue.Problem(str(tmp_path), str(videos)) is None
	assert copyqueue.Problem(str(tmp_path / "nowhere"), str(videos)).startswith("Can't reach ")
	assert "where the recordings are made" in copyqueue.Problem(str(videos), str(videos))
	(videos / "inside").mkdir()
	assert "where the recordings are made" in copyqueue.Problem(str(videos / "inside"), str(videos))


def test_the_folder_is_remembered_for_each_config(tmp_path):
	path = copyqueue.RememberFile()
	assert copyqueue.Remembered(path, str(tmp_path / "a.yaml")) is None
	copyqueue.Remember(path, str(tmp_path / "a.yaml"), r"\\server\share\me")
	copyqueue.Remember(path, str(tmp_path / "b.yaml"), "")
	assert copyqueue.Remembered(path, str(tmp_path / "a.yaml")) == r"\\server\share\me"
	assert copyqueue.Remembered(path, str(tmp_path / "b.yaml")) == "" # emptied on purpose: no copy, not the config's


def bare_panel(queue):
	p = object.__new__(panel.Panel)
	p.copies, p.close_when_copied, p.closing = queue, False, False
	p.session = p.wanted = None
	return p


def test_close_waits_for_the_copies_or_leaves_them(tmp_path, monkeypatch):
	release = threading.Event()
	monkeypatch.setattr(post, "StepCopy", lambda folder, settings, log, stop=None: (release.wait(10), ("done", "ok"))[1])
	queue = quiet()
	queue.Add(recording(tmp_path, "2026-11-09_13-00-00"), str(tmp_path))
	p = bare_panel(queue)
	assert p.Close() is None and p.close_when_copied and not p.closing # closes by itself once they're done
	assert p.Close(leave=True) is None and p.closing # now, leaving them
	assert queue.Status()["jobs"][0]["state"] == "left"
	release.set()
	q2 = quiet()
	p = bare_panel(q2)
	assert p.Close() is None and p.closing # nothing to copy: closes at once


def test_the_box_starts_from_the_remembered_folder_or_the_configs(tmp_path):
	p = bare_panel(quiet())
	p.config = types.SimpleNamespace(path=str(tmp_path / "daily.yaml"), post={"copy": {"to": r"\\server\from-config"}})
	assert p.CopyTo() == r"\\server\from-config"
	copyqueue.Remember(copyqueue.RememberFile(), p.config.path, r"\\server\from-page")
	assert p.CopyTo() == r"\\server\from-page"
	p.config.post = {"copy": {}}
	p.config.path = str(tmp_path / "other.yaml")
	assert p.CopyTo() == ""


def test_post_by_hand_copies_into_the_folder_given(tmp_path):
	to = tmp_path / "share"
	to.mkdir()
	folder = recording(tmp_path, "2026-11-09_14-00-00")
	(tmp_path / "rec" / "2026-11-09_14-00-00" / "logs").mkdir(exist_ok=True)
	# A clean stop, as campy-session's launcher.json says it
	(tmp_path / "rec" / "2026-11-09_14-00-00" / "logs" / "launcher.json").write_text(json.dumps(
		{"parts": {"acquire": {"exit_code": 0}}}))
	code = post.Main([folder, "--only", "copy", "--to", str(to), "--force"])
	assert code == 0
	assert (to / "2026-11-09_14-00-00" / "Camera0" / "0.mp4").exists()
