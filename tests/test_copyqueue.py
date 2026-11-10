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


def test_a_copy_is_in_post_json_before_it_counts_as_done(tmp_path, monkeypatch):
	# Whatever waits for the copies (the panel closing) finds each result in post.json, even when writing it is slow
	real = post.Save

	def slow(folder, state):
		time.sleep(0.3)
		real(folder, state)
	monkeypatch.setattr(post, "Save", slow)
	to = tmp_path / "share"
	to.mkdir()
	queue = quiet()
	folder = recording(tmp_path, "2026-11-09_10-50-00")
	queue.Add(folder, str(to))
	assert finished(queue)["jobs"][0]["state"] == "done"
	assert post.Load(folder)["steps"]["copy"]["state"] == "done"


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

	def slow(folder, settings, log, stop=None, **kwargs):
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
	while [j["state"] for j in queue.Status()["jobs"]] != ["waiting", "copying"]: # newest first: b waits while a copies
		assert time.monotonic() - t0 < 10, queue.Status()
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
	assert copyqueue.Problem(str(tmp_path / "no" / "where"), str(videos)).startswith("Can't reach ")
	assert "where the recordings are made" in copyqueue.Problem(str(videos), str(videos))
	(videos / "inside").mkdir()
	assert "where the recordings are made" in copyqueue.Problem(str(videos / "inside"), str(videos))


def test_a_copy_folder_not_there_yet_can_be_made_one_level_only(tmp_path):
	# The page's Create this folder: only the last folder, in one that is there (a mistyped share or parent makes nothing)
	videos = tmp_path / "videos"
	videos.mkdir()
	new = tmp_path / "share" / "luc3d-live"
	(tmp_path / "share").mkdir()
	assert copyqueue.Creatable(str(new)) and copyqueue.Creatable(str(new) + os.sep)
	assert "doesn't exist yet. Press Create this folder" in copyqueue.Problem(str(new), str(videos))
	assert not copyqueue.Creatable(str(tmp_path / "nowhere" / "luc3d-live")) # its parent isn't there either
	assert copyqueue.Problem(str(tmp_path / "nowhere" / "x"), str(videos)).startswith("Can't reach ")
	assert not copyqueue.Creatable("") and not copyqueue.Creatable("me@host:/data/x")
	assert copyqueue.Create(str(new), str(videos)) is None and new.is_dir()
	assert not copyqueue.Creatable(str(new)) and copyqueue.Problem(str(new), str(videos)) is None
	assert copyqueue.Create(str(new), str(videos)) is None # there already: nothing to do
	assert copyqueue.Create(str(tmp_path / "nowhere" / "x"), str(videos)).startswith("Can't reach ")
	assert not (tmp_path / "nowhere").exists()
	# Inside the recordings' folder: made, but still refused as a copy folder
	assert "where the recordings are made" in copyqueue.Create(str(videos / "copies"), str(videos))


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


def test_the_page_lists_the_newest_first_even_within_one_clock_tick(tmp_path, monkeypatch):
	# Windows' time.time() moves in steps of about 16 ms: recordings queued within one step keep their order
	release = threading.Event()
	monkeypatch.setattr(post, "StepCopy", lambda folder, settings, log, stop=None, **kwargs: (release.wait(10), ("done", "ok"))[1])
	monkeypatch.setattr(copyqueue.time, "time", lambda: 1000.0)
	queue = quiet()
	names = ["2026-11-09_15-00-0{}".format(n) for n in range(4)]
	for name in names:
		queue.Add(recording(tmp_path, name), str(tmp_path))
	assert [j["name"] for j in queue.Status()["jobs"]] == names[::-1]
	release.set()


def test_close_waits_for_the_copies_or_leaves_them(tmp_path, monkeypatch):
	release = threading.Event()
	monkeypatch.setattr(post, "StepCopy", lambda folder, settings, log, stop=None, **kwargs: (release.wait(10), ("done", "ok"))[1])
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


# ------------------------------------------------------------------ how far a copy has got

BIG = 100_000


def old(folder, seconds=3600):
	# The recording's files, written an hour before their copy
	t = time.time() - seconds
	for root, _, files in os.walk(folder):
		for name in files:
			os.utime(os.path.join(root, name), (t, t))


def presize(src, dst):
	# As robocopy (Windows' CopyFileEx) does as it starts on a file: the copy is made its full size at once, its bytes
	# and its time come later
	os.makedirs(os.path.dirname(dst), exist_ok=True)
	with open(dst, "wb") as f:
		f.truncate(os.path.getsize(src))


def copying(queue, monkeypatch, name, pid=None):
	"""A StepCopy that pre-sizes the big file of recording name and waits: (began, release)."""
	began, release = threading.Event(), threading.Event()

	def step(folder, settings, log, stop=None, started=None, **kwargs):
		if os.path.basename(folder) != name: # an earlier test's queue, still copying: not this test's
			return "done", "copied"
		if pid is not None and started is not None:
			started(types.SimpleNamespace(pid=pid))
		dest = os.path.join(settings["copy"]["to"], os.path.basename(folder))
		presize(os.path.join(folder, "Camera0", "0.mp4"), os.path.join(dest, "Camera0", "0.mp4"))
		began.set()
		release.wait(10)
		return "done", "copied"
	monkeypatch.setattr(post, "StepCopy", step)
	monkeypatch.setattr(copyqueue, "LOOK_S", -1) # looked at on every Status
	return began, release


def done_bytes(queue):
	return queue.Status()["jobs"][0]["done_bytes"]


def test_a_big_file_sized_in_full_as_it_starts_counts_only_once_copied(tmp_path, monkeypatch):
	# Where the copy program's count isn't known (macOS): the copy's folder is looked at. Seen 2026-11-10 on Windows:
	# a 5.9 GB SpikeGLX file counted as copied as soon as robocopy started on it, and the page sat at 6.4 of 7.6 GB
	# ("under a minute left") for minutes
	queue = quiet()
	began, release = copying(queue, monkeypatch, "2026-11-10_11-40-00")
	folder = recording(tmp_path, "2026-11-10_11-40-00", BIG)
	old(folder)
	to = tmp_path / "share"
	to.mkdir()
	queue.Add(folder, str(to))
	assert began.wait(10)
	src = os.path.join(folder, "Camera0", "0.mp4")
	dst = to / "2026-11-10_11-40-00" / "Camera0" / "0.mp4"
	assert dst.stat().st_size == BIG
	assert done_bytes(queue) == 0 # its full size, but not copied yet
	# Half its bytes written: still its full size, its time now's
	with open(dst, "r+b") as f:
		f.write(open(src, "rb").read(BIG // 2))
	assert done_bytes(queue) == 0
	# Done with it: its bytes and its original's time
	dst.write_bytes(open(src, "rb").read())
	os.utime(dst, (os.path.getatime(src), os.path.getmtime(src)))
	assert done_bytes(queue) == BIG
	release.set()
	job = finished(queue)["jobs"][0]
	assert job["state"] == "done" and job["done_bytes"] == job["bytes"]


class FakeIo:
	def __init__(self, counts):
		self.counts = counts # the write count, read each time it is looked at

	def __call__(self, pid):
		self.pid = pid
		return self

	def children(self, recursive=False):
		return []

	def io_counters(self):
		return types.SimpleNamespace(write_bytes=self.counts[0]) # Windows': no write_chars


def test_the_copy_programs_write_count_drives_the_progress(tmp_path, monkeypatch):
	counts = [0]
	fake = FakeIo(counts)
	monkeypatch.setattr(copyqueue.psutil, "Process", fake)
	queue = quiet()
	began, release = copying(queue, monkeypatch, "2026-11-10_11-41-00", pid=4321)
	folder = recording(tmp_path, "2026-11-10_11-41-00", BIG)
	old(folder)
	to = tmp_path / "share"
	to.mkdir()
	queue.Add(folder, str(to))
	assert began.wait(10)
	total = queue.Status()["jobs"][0]["bytes"]
	assert fake.pid == 4321
	assert done_bytes(queue) == 0 # the big file there in full size: not counted
	counts[0] = 30_000
	assert done_bytes(queue) == 30_000
	counts[0] = 90_000
	assert done_bytes(queue) == 90_000
	counts[0] = 50_000 # never back
	assert done_bytes(queue) == 90_000
	counts[0] = 10 * total # its own output to the log too: never more than the recording
	assert done_bytes(queue) == total
	release.set()
	assert finished(queue)["jobs"][0]["done_bytes"] == total


def test_the_copy_programs_count_is_the_most_any_of_its_processes_wrote(monkeypatch):
	# rsync: a sender writes what its receiver writes again (on Linux, write_chars: every write, a pipe's too)
	def proc(chars, children=()):
		return types.SimpleNamespace(io_counters=lambda: types.SimpleNamespace(write_bytes=0, write_chars=chars),
			children=lambda recursive=False: list(children))
	assert copyqueue.Written(proc(700, [proc(1000), proc(20)])) == 1000
	assert copyqueue.Written(None) is None

	def gone(recursive=False):
		raise copyqueue.psutil.NoSuchProcess(4321)
	assert copyqueue.Written(types.SimpleNamespace(children=gone)) is None # ended: the copy's folder is looked at

	def mac():
		raise AttributeError("io_counters") # psutil keeps no count on macOS
	assert copyqueue.Written(types.SimpleNamespace(io_counters=mac, children=lambda recursive=False: [])) is None


def test_the_copys_folder_counts_what_is_really_there(tmp_path):
	src, dst = tmp_path / "rec", tmp_path / "copy"
	(src / "a").mkdir(parents=True)
	(dst / "a").mkdir(parents=True)
	(src / "a" / "big.bin").write_bytes(b"x" * 1000)
	(src / "small.txt").write_bytes(b"y" * 10)
	old(str(src))
	# rsync's temporary file, which grows as it writes, by its size; a file smaller than its original by its size
	(dst / "a" / ".big.bin.Xy12Ab").write_bytes(b"x" * 400)
	(dst / "small.txt").write_bytes(b"y" * 4)
	assert copyqueue.Copied(str(src), str(dst)) == 404
	# Its full size but not its original's time (robocopy, still writing): not counted
	(dst / "a" / ".big.bin.Xy12Ab").unlink()
	presize(str(src / "a" / "big.bin"), str(dst / "a" / "big.bin"))
	assert copyqueue.Copied(str(src), str(dst)) == 4
	# Both done: their original's time
	(dst / "small.txt").write_bytes(b"y" * 10)
	for name in ("a/big.bin", "small.txt"):
		s = os.stat(src / name)
		os.utime(dst / name, (s.st_atime, s.st_mtime))
	assert copyqueue.Copied(str(src), str(dst)) == 1010
