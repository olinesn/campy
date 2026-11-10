"""A recording's copy checked byte for byte, and the recording deleted here only once every file of it is the same in
the copy (campy/copycheck.py; post.StepCopy and FinishCopy; the panel's copies, campy/copyqueue.py)."""

import hashlib
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from campy import copycheck, copyqueue, panel, post, session


@pytest.fixture(autouse=True)
def status_dir(tmp_path, monkeypatch):
	monkeypatch.setenv("CAMPY_STATUS_DIR", str(tmp_path / "status"))


def recording(tmp_path, name, size=300000, exit_code=0):
	# A recording's folder as the rig makes them: cameras, SpikeGLX's and the Pico's folders, logs; stopped cleanly
	# (campy-session's launcher.json, each camera's totals in its metadata.csv) unless exit_code says otherwise
	folder = tmp_path / "rec" / name
	for sub in ("Camera0", "Camera1", "spikeglx", "pico", "logs"):
		(folder / sub).mkdir(parents=True)
	(folder / "Camera0" / "0.mp4").write_bytes(os.urandom(size))
	(folder / "Camera1" / "1.mp4").write_bytes(os.urandom(size // 3))
	for cam in ("Camera0", "Camera1"):
		(folder / cam / "metadata.csv").write_text("framesWritten,120\n" if exit_code == 0 else "")
	(folder / "launcher.json").write_text(json.dumps({"parts": {"acquire": {"state": "ended", "exit_code": exit_code}}}))
	(folder / "spikeglx" / "run_g0_t0.imec0.ap.bin").write_bytes(os.urandom(size // 2))
	(folder / "pico" / "bouts.csv").write_text("bout,start\n1,0\n")
	(folder / "logs" / "campy-acquire.log").write_text("its log\n")
	(folder / "session.json").write_text("{}")
	return str(folder)


def contents(folder):
	# Every file's bytes, by its path in the folder (post's own logs aside: they say what happened)
	return {rel: open(os.path.join(folder, rel), "rb").read() for rel in copycheck.Files(folder, post.POST_OWN)}


def queue_with(sent, busy=None):
	return copyqueue.CopyQueue(say=lambda text: None, out=open(os.devnull, "w"), busy=busy,
		alert=lambda key, text, severity: sent.append((key, text, severity)))


def finished(queue, timeout=60):
	t0 = time.monotonic()
	while queue.Pending():
		assert time.monotonic() - t0 < timeout, queue.Status()
		time.sleep(0.05)
	return queue.Status()["jobs"][0]


def spoil_after_copy(monkeypatch, spoil):
	# The copy made as usual (robocopy, rsync), then spoiled before it is checked: what a bad share or a cut-off
	# copy would leave
	real = post.RunCopier

	def run(command, *args, **kwargs):
		out = real(command, *args, **kwargs)
		spoil(command[2].rstrip("/") if command[0] == "robocopy" else command[-1].rstrip("/"))
		return out
	monkeypatch.setattr(post, "RunCopier", run)


def flip_a_bit(dest, rel="Camera0/0.mp4", at=123457):
	path = os.path.join(dest, *rel.split("/"))
	with open(path, "r+b") as f:
		f.seek(at)
		byte = f.read(1)
		f.seek(at)
		f.write(bytes([byte[0] ^ 0x10]))


# ------------------------------------------------------------------ the check

def test_a_clean_copy_is_checked_then_deleted_here(tmp_path):
	share = tmp_path / "share"
	share.mkdir()
	folder = recording(tmp_path, "2026-11-10_09-00-00")
	before = contents(folder)
	sent = []
	queue = queue_with(sent)
	queue.Add(folder, str(share), delete=True)
	job = finished(queue)
	assert job["state"] == "done", job
	assert job["verified"] == len(before) and job["mismatched"] == 0
	assert "deleted from this computer" in job["message"]
	assert not os.path.exists(folder) and not os.path.exists(folder + copycheck.DELETING)
	dest = str(share / "2026-11-10_09-00-00")
	assert contents(dest) == before # every byte there
	step = post.Load(dest)["steps"]["copy"] # the copy's own post.json says it all, the deletion too
	assert step["state"] == "done" and step["verified"] == len(before) and step["mismatched"] == 0
	assert "deleted from this computer" in step["local"]
	for f in step["files"]:
		assert f["result"] == "verified" and f["sha256"] == hashlib.sha256(before[f["file"]]).hexdigest()
	assert sorted(f["file"] for f in step["files"]) == sorted(before) # SpikeGLX's and the Pico's files included
	log = open(os.path.join(dest, "logs", "post.log"), encoding="utf-8").read()
	assert "verified  spikeglx/run_g0_t0.imec0.ap.bin" in log and "deleted from this computer" in log
	assert sent == []


def test_a_recording_that_didnt_stop_cleanly_is_copied_and_checked_but_kept_here(tmp_path):
	# campy-acquire failed two seconds in: its folder is copied and checked as any, but stays here, box ticked or not,
	# with an alert (the disk isn't freed), as well as the page saying why
	share = tmp_path / "share"
	share.mkdir()
	folder = recording(tmp_path, "2026-11-10_13-01-57", exit_code=1)
	before = contents(folder)
	sent = []
	queue = queue_with(sent)
	queue.Add(folder, str(share), delete=True)
	job = finished(queue)
	assert job["state"] == "done" and job["verified"] == len(before) and job["mismatched"] == 0, job
	assert "didn't stop cleanly: campy-acquire ended with exit code 1" in job["message"]
	assert contents(folder) == before # all of it still here
	dest = str(share / "2026-11-10_13-01-57")
	assert contents(dest) == before
	assert "didn't stop cleanly" in post.Load(folder)["steps"]["copy"]["local"]
	assert "didn't stop cleanly" in post.Load(dest)["steps"]["copy"]["local"] # the copy's logs say it too
	assert len(sent) == 1 and sent[0][0] == "copy/2026-11-10_13-01-57" and sent[0][2] == "warning", sent
	assert "didn't stop cleanly" in sent[0][1] and "campy-acquire ended with exit code 1" in sent[0][1]
	assert "kept on this computer" in sent[0][1] and sent[0][1].count("delete it by hand") == 1


def test_post_by_hand_never_deletes_a_recording_that_didnt_stop_cleanly(tmp_path):
	share = tmp_path / "share"
	share.mkdir()
	folder = recording(tmp_path, "2026-11-10_13-05-00", exit_code=1)
	code = post.Main([folder, "--only", "copy", "--to", str(share), "--delete-after-verify", "--force"])
	assert code == 1 and os.path.isdir(folder) # asked to delete, kept: not a success
	assert "didn't stop cleanly" in post.Load(folder)["steps"]["copy"]["local"]
	assert post.Load(folder)["steps"]["copy"]["state"] == "done"


def test_an_unclean_recording_whose_logs_dont_copy_still_says_why_it_is_kept(tmp_path, monkeypatch):
	# The second copy of post's logs failing doesn't hide the reason: still kept for the unclean stop, the alert says so
	share = tmp_path / "share"
	share.mkdir()
	folder = recording(tmp_path, "2026-11-10_13-10-00", exit_code=1)
	real = copycheck.CopyFiles
	monkeypatch.setattr(copycheck, "CopyFiles", lambda folder, dest, names: (False, "the share went away", {})
		if any(n.endswith("post.log") for n in names) else real(folder, dest, names))
	sent = []
	queue = queue_with(sent)
	queue.Add(folder, str(share), delete=True)
	job = finished(queue)
	assert job["state"] == "done" and os.path.isdir(folder), job
	assert "didn't stop cleanly" in job["message"] and "the share went away" in job["message"]
	assert "didn't stop cleanly" in post.Load(folder)["steps"]["copy"]["local"]
	assert len(sent) == 1 and "didn't stop cleanly" in sent[0][1] and "the share went away" in sent[0][1]


def test_a_flipped_bit_is_caught_and_nothing_is_deleted(tmp_path, monkeypatch):
	share = tmp_path / "share"
	share.mkdir()
	folder = recording(tmp_path, "2026-11-10_09-10-00")
	before = contents(folder)
	spoil_after_copy(monkeypatch, flip_a_bit)
	sent = []
	queue = queue_with(sent)
	queue.Add(folder, str(share), delete=True)
	job = finished(queue)
	assert job["state"] == "failed" and job["mismatched"] == 1, job
	assert "Camera0/0.mp4 differs from byte 123,457" in job["message"]
	assert contents(folder) == before # all of it still here
	step = post.Load(folder)["steps"]["copy"]
	assert step["state"] == "failed"
	bad = [f for f in step["files"] if f["result"] != "verified"]
	assert [(f["file"], f["result"]) for f in bad] == [("Camera0/0.mp4", "mismatch")]
	assert len(sent) == 1 and sent[0][0] == "copy/2026-11-10_09-10-00" and "kept on this computer" in sent[0][1]


def test_a_truncated_file_is_caught_and_nothing_is_deleted(tmp_path, monkeypatch):
	share = tmp_path / "share"
	share.mkdir()
	folder = recording(tmp_path, "2026-11-10_09-20-00")
	before = contents(folder)

	def truncate(dest):
		with open(os.path.join(dest, "spikeglx", "run_g0_t0.imec0.ap.bin"), "r+b") as f:
			f.truncate(1000)
	spoil_after_copy(monkeypatch, truncate)
	sent = []
	queue = queue_with(sent)
	queue.Add(folder, str(share), delete=True)
	job = finished(queue)
	assert job["state"] == "failed", job
	assert "spikeglx/run_g0_t0.imec0.ap.bin 150,000 bytes here, 1,000 in the copy" in job["message"]
	assert contents(folder) == before
	assert len(sent) == 1


def test_a_file_missing_from_the_copy_is_caught(tmp_path, monkeypatch):
	share = tmp_path / "share"
	share.mkdir()
	folder = recording(tmp_path, "2026-11-10_09-25-00")
	spoil_after_copy(monkeypatch, lambda dest: os.remove(os.path.join(dest, "pico", "bouts.csv")))
	results = post.Run(folder, {"copy": {"to": str(share)}}, only=["copy"], force=True, out=open(os.devnull, "w"),
		delete=True)
	assert results["copy"][0] == "failed" and "pico/bouts.csv not in the copy" in results["copy"][1]
	assert "local" not in results and os.path.isfile(os.path.join(folder, "pico", "bouts.csv"))


def test_a_file_open_in_another_program_is_not_deleted(tmp_path):
	# As SpikeGLX, still writing its run into the recording's folder
	share = tmp_path / "share"
	share.mkdir()
	folder = recording(tmp_path, "2026-11-10_09-30-00")
	held = os.path.join(folder, "spikeglx", "run_g0_t0.imec0.ap.bin")
	holder = subprocess.Popen([sys.executable, "-c", "import sys, time\nf = open(sys.argv[1], 'rb')\nprint('open', "
		"flush=True)\ntime.sleep(120)", held], stdout=subprocess.PIPE, text=True)
	try:
		assert holder.stdout.readline().strip() == "open"
		sent = []
		queue = queue_with(sent)
		queue.Add(folder, str(share), delete=True)
		job = finished(queue)
	finally:
		holder.kill()
		holder.wait()
	assert job["state"] == "done" and job["verified"] and not job["mismatched"], job
	assert "kept here" in job["message"] and "run_g0_t0.imec0.ap.bin" in job["message"], job
	assert os.path.isfile(held) and os.path.isfile(os.path.join(folder, "Camera0", "0.mp4"))
	assert "kept here" in post.Load(folder)["steps"]["copy"]["local"]
	assert len(sent) == 1 and "not deleted from this computer" in sent[0][1]


def test_a_folder_the_panel_still_wants_is_not_deleted(tmp_path):
	share = tmp_path / "share"
	share.mkdir()
	folder = recording(tmp_path, "2026-11-10_09-40-00")
	queue = queue_with([], busy=lambda f: "a recording is being made into it" if f == folder else "")
	queue.Add(folder, str(share), delete=True)
	job = finished(queue)
	assert job["state"] == "done" and "kept here: a recording is being made into it" in job["message"]
	assert os.path.isdir(folder)


def test_a_file_changed_after_the_check_keeps_the_folder(tmp_path):
	folder = recording(tmp_path, "2026-11-10_09-50-00")
	snapshot = copycheck.Snapshot(folder)
	with open(os.path.join(folder, "pico", "bouts.csv"), "a") as f: # written to after the check
		f.write("2,600\n")
	os.utime(os.path.join(folder, "pico", "bouts.csv"), ns=(1, 1))
	deleted, why = copycheck.Delete(folder, snapshot)
	assert not deleted and "written to since the check: pico/bouts.csv" in why
	(tmp_path / "rec" / "2026-11-10_09-50-00" / "new.txt").write_text("x")
	deleted, why = copycheck.Delete(folder, copycheck.Snapshot(folder, skip=("new.txt",)))
	assert not deleted and "new since the check: new.txt" in why
	assert os.path.isdir(folder)
	deleted, why = copycheck.Delete(folder, copycheck.Snapshot(folder))
	assert deleted and not os.path.exists(folder)


def test_without_the_box_ticked_the_copy_is_checked_and_kept_here(tmp_path):
	share = tmp_path / "share"
	share.mkdir()
	folder = recording(tmp_path, "2026-11-10_10-00-00")
	queue = queue_with([])
	queue.Add(folder, str(share))
	job = finished(queue)
	assert job["state"] == "done" and job["verified"] == 9 and job["mismatched"] == 0 # 6 files + 2 metadata.csv + launcher.json
	assert "every file checked byte for byte (9 files" in job["message"] and os.path.isdir(folder)
	dest = os.path.join(str(share), "2026-11-10_10-00-00")
	# Post's logs, written after the check, copied again: the copy's are the same as here, the result in them
	for name in ("post.json", "post.log"):
		assert open(os.path.join(dest, "logs", name), "rb").read() == open(os.path.join(folder, "logs", name), "rb").read()
	assert post.Load(dest)["steps"]["copy"]["verified"] == 9


def test_post_by_hand_deletes_after_the_check(tmp_path):
	share = tmp_path / "share"
	share.mkdir()
	folder = recording(tmp_path, "2026-11-10_10-10-00")
	before = contents(folder)
	(tmp_path / "rec" / "2026-11-10_10-10-00" / "logs" / "launcher.json").write_text(json.dumps(
		{"parts": {"acquire": {"exit_code": 0}}}))
	before["logs/launcher.json"] = open(os.path.join(folder, "logs", "launcher.json"), "rb").read()
	code = post.Main([folder, "--only", "copy", "--to", str(share), "--delete-after-verify", "--force"])
	assert code == 0 and not os.path.exists(folder)
	dest = os.path.join(str(share), "2026-11-10_10-10-00")
	assert contents(dest) == before
	assert "deleted from this computer" in post.Load(dest)["steps"]["copy"]["local"]


def test_the_check_reads_the_copy_past_this_computers_cache_where_it_can(tmp_path):
	path = tmp_path / "f.bin"
	path.write_bytes(os.urandom(copycheck.CHUNK + 5000)) # not a whole number of sectors: the last read is short
	f, how = copycheck.ReadBack(str(path))
	try:
		assert copycheck.ReadAll(f, copycheck.CHUNK) + copycheck.ReadAll(f, copycheck.CHUNK) == path.read_bytes()
		assert copycheck.ReadAll(f, copycheck.CHUNK) == b""
	finally:
		f.close()
	assert how == {"win32": "uncached", "linux": "cache dropped"}.get(sys.platform, "maybe cached")


# ------------------------------------------------------------------ the page's box

def test_the_box_is_remembered_for_each_config_and_starts_from_the_configs(tmp_path):
	path = copyqueue.RememberFile()
	assert copyqueue.RememberedDelete(path, str(tmp_path / "a.yaml")) is None
	copyqueue.Remember(path, str(tmp_path / "a.yaml"), r"\\server\share\me", delete=True)
	copyqueue.Remember(path, str(tmp_path / "b.yaml"), r"\\server\share\me", delete=False)
	assert copyqueue.RememberedDelete(path, str(tmp_path / "a.yaml")) is True
	assert copyqueue.RememberedDelete(path, str(tmp_path / "b.yaml")) is False
	p = object.__new__(panel.Panel)
	p.config = type("C", (), {"path": str(tmp_path / "c.yaml"), "post": {"copy": {"to": "x", "delete_after_verify": True}}})()
	assert p.CopyDelete() is True # never set on a page: the config's
	p.config.post = {"copy": {}}
	assert p.CopyDelete() is False # off by default
	p.config.path = str(tmp_path / "a.yaml")
	assert p.CopyDelete() is True # as the page left it


def test_the_config_takes_delete_after_verify(tmp_path):
	import yaml
	path = tmp_path / "c.yaml"
	path.write_text(yaml.safe_dump({"numCams": 1, "post": {"copy": {"to": "x", "delete_after_verify": True}}}))
	assert session.Config(str(path)).post["copy"] == {"to": "x", "delete_after_verify": True}
	path.write_text(yaml.safe_dump({"numCams": 1, "post": {"copy": {"to": "x", "delete_after_verify": "yes"}}}))
	with pytest.raises(session.SessionError, match="delete_after_verify: 'yes' is not true or false"):
		session.Config(str(path))


# ------------------------------------------------------------------ out of tracking's way

def test_the_check_reads_no_faster_than_its_limit(tmp_path):
	# However fast the copy's side answers (here: the same disk), the check keeps to its rate: 6 MB at 3 MB/s
	for side in ("here", "there"):
		(tmp_path / side).mkdir()
	data = os.urandom(6 * 1000 * 1000)
	(tmp_path / "here" / "0.mp4").write_bytes(data)
	(tmp_path / "there" / "0.mp4").write_bytes(data)
	t0 = time.monotonic()
	files, _, total = copycheck.Check(str(tmp_path / "here"), str(tmp_path / "there"), rate=3e6)
	assert files[0]["result"] == "verified" and total == len(data)
	assert time.monotonic() - t0 >= 1.8
	assert copycheck.MAX_BYTES_PER_S == 100e6 # the default


def test_the_check_reads_and_compares_only_in_its_own_lowest_priority_threads(tmp_path, monkeypatch):
	import threading
	lowered, read_in = set(), set()
	real_background, real_read = copycheck.Background, copycheck.ReadAll

	def background():
		lowered.add(threading.get_ident())
		return real_background()

	def read(f, n):
		read_in.add(threading.get_ident())
		return real_read(f, n)
	monkeypatch.setattr(copycheck, "Background", background)
	monkeypatch.setattr(copycheck, "ReadAll", read)
	folder = recording(tmp_path, "2026-11-10_11-00-00")
	share = tmp_path / "share"
	(share / "2026-11-10_11-00-00").mkdir(parents=True)
	import shutil
	shutil.copytree(folder, str(share / "2026-11-10_11-00-00"), dirs_exist_ok=True)
	files, _, _ = copycheck.Check(folder, str(share / "2026-11-10_11-00-00"))
	assert all(f["result"] == "verified" for f in files)
	assert read_in and read_in <= lowered and threading.get_ident() not in read_in


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS: nothing is lowered")
def test_a_check_thread_is_lowered_as_far_as_the_system_lets_it(tmp_path):
	import threading
	got = {}

	def lower():
		got["lowered"] = copycheck.Background()
		if sys.platform.startswith("linux"):
			got["nice"] = os.getpriority(os.PRIO_PROCESS, threading.get_native_id())
		elif sys.platform == "win32":
			import ctypes
			k32 = ctypes.WinDLL("kernel32")
			k32.GetCurrentThread.restype = ctypes.c_void_p
			k32.GetThreadPriority.argtypes = [ctypes.c_void_p]
			got["priority"] = k32.GetThreadPriority(k32.GetCurrentThread())
	t = threading.Thread(target=lower)
	t.start()
	t.join()
	assert got["lowered"]
	if "nice" in got:
		assert got["nice"] == 19
	if "priority" in got:
		assert got["priority"] < 0 # below normal: idle (-15), or background mode's own


# ------------------------------------------------------------------ review fixes (10 November)

def test_a_copy_onto_the_recording_itself_is_refused_and_nothing_deleted(tmp_path):
	# A recording D:\data\rec1 copied --to D:\data: the copy's folder is the recording itself, and every file would be
	# compared with itself and pass. Refused, before anything is copied or deleted
	folder = recording(tmp_path, "2026-11-10_14-00-00")
	before = contents(folder)
	parent = os.path.dirname(folder)
	code = post.Main([folder, "--only", "copy", "--to", parent, "--delete-after-verify"])
	assert code == 1 and contents(folder) == before
	step = post.Load(folder)["steps"]["copy"]
	assert step["state"] == "failed" and "is the recording's own folder" in step["message"], step
	assert "nothing was copied, and nothing deleted" in step["message"] and "files" not in step
	# Into the recording itself (a copy inside it): refused too, nothing made in it
	code = post.Main([folder, "--only", "copy", "--to", folder, "--delete-after-verify", "--force"])
	assert code == 1 and contents(folder) == before
	assert not os.path.exists(os.path.join(folder, "2026-11-10_14-00-00"))
	assert "is the recording's own folder, or inside it" in post.Load(folder)["steps"]["copy"]["message"]


def test_a_copy_onto_the_recording_through_another_path_is_refused(tmp_path, monkeypatch):
	# The same folder by another name: a link (realpath sees through it), or a network share of this disk (it can't:
	# os.path.samefile compares the folders themselves)
	folder = recording(tmp_path, "2026-11-10_14-10-00")
	alias = tmp_path / "alias"
	try:
		os.symlink(os.path.dirname(folder), str(alias), target_is_directory=True)
	except (OSError, NotImplementedError):
		pytest.skip("this system can't make a link here")
	assert post.SelfCopy(folder, str(alias / "2026-11-10_14-10-00"))
	monkeypatch.setattr(post.os.path, "realpath", lambda p: os.path.abspath(p)) # as a share would look
	assert post.Within(str(alias / "2026-11-10_14-10-00"), folder)
	assert post.Within(str(alias / "2026-11-10_14-10-00" / "logs" / "deeper"), folder) # inside it, not made yet
	assert "holds the recording's own folder" in post.SelfCopy(folder, str(tmp_path / "rec"))
	assert post.SelfCopy(folder, str(tmp_path / "share" / "2026-11-10_14-10-00")) is None
	assert not post.Within(str(tmp_path / "rec2"), str(tmp_path / "rec")) # a name that only starts the same
	sent = []
	queue = queue_with(sent)
	queue.Add(folder, str(alias), delete=True) # the panel's copies get it too
	job = finished(queue)
	assert job["state"] == "failed" and "recording's own folder" in job["message"], job
	assert os.path.isdir(folder) and len(sent) == 1 and "Its folder is kept" in sent[0][1]


def test_the_panel_refuses_the_folder_the_chosen_length_records_into(tmp_path):
	# A Pico length's own videoFolder (the panel's RecordsInto), followed through a link too
	into = tmp_path / "chronic"
	into.mkdir()
	assert "where the recordings are made" in copyqueue.Problem(str(into), str(into))
	try:
		os.symlink(str(into), str(tmp_path / "link"), target_is_directory=True)
	except (OSError, NotImplementedError):
		return
	assert "where the recordings are made" in copyqueue.Problem(str(tmp_path / "link"), str(into))


def fake_rsync(monkeypatch):
	# An rsync address: rsync "runs" and succeeds; nothing reads the copy back
	import subprocess
	monkeypatch.setattr(post.shutil, "which", lambda name: "/usr/bin/rsync" if name == "rsync" else None)
	monkeypatch.setattr(post, "RunCopier", lambda command, started=None: subprocess.CompletedProcess(command, 0, "", ""))


def test_an_rsync_copy_with_the_box_ticked_says_it_wasnt_checked(tmp_path, monkeypatch):
	fake_rsync(monkeypatch)
	folder = recording(tmp_path, "2026-11-10_14-20-00")
	before = contents(folder)
	sent = []
	queue = queue_with(sent)
	queue.Add(folder, "me@server:/data", delete=True)
	job = finished(queue)
	assert job["state"] == "done" and contents(folder) == before, job
	assert "the copy wasn't checked (an rsync address isn't read back), so nothing was deleted" in job["local"]
	assert len(sent) == 1, sent
	assert "wasn't checked" in sent[0][1] and "kept on this computer" in sent[0][1]
	assert "and checked" not in sent[0][1] and "by hand" not in sent[0][1]


def test_post_by_hand_to_an_rsync_address_says_the_copy_wasnt_checked(tmp_path, monkeypatch, capsys):
	fake_rsync(monkeypatch)
	folder = recording(tmp_path, "2026-11-10_14-30-00")
	post.Main([folder, "--only", "copy", "--to", "me@server:/data", "--delete-after-verify"])
	err = capsys.readouterr().err
	assert os.path.isdir(folder)
	assert "nothing deleted: the copy wasn't checked (an rsync address isn't read back)" in err
	assert "done already" not in err


def test_closing_while_a_checked_copy_deletes_lets_it_end(tmp_path, monkeypatch):
	# The panel closes (Close now, Ctrl-C) while a copy already checked deletes its folder: not marked left (that
	# made the deleted folder again, with a post.json saying it wasn't copied, and kept its alert from going out)
	share = tmp_path / "share"
	share.mkdir()
	folder = recording(tmp_path, "2026-11-10_14-40-00")
	entered, release = threading.Event(), threading.Event()
	real = post.FinishCopy

	def slow(*args, **kwargs):
		entered.set()
		release.wait(20)
		return real(*args, **kwargs)
	monkeypatch.setattr(post, "FinishCopy", slow)
	sent = []
	queue = queue_with(sent)
	queue.Add(folder, str(share), delete=True)
	assert entered.wait(30)
	assert queue.Leave() == [] and len(queue.Pending()) == 1
	from test_copyqueue import bare_panel
	p = bare_panel(queue)
	assert p.Close(leave=True) is None and p.close_when_copied and not p.closing # closes once it has ended
	release.set()
	job = finished(queue)
	assert job["state"] == "done" and "deleted from this computer" in job["message"], job
	assert not os.path.exists(folder) # not made again
	assert contents(str(share / "2026-11-10_14-40-00"))
	assert sent == []


def test_a_result_is_never_written_into_a_folder_that_is_gone(tmp_path):
	gone = tmp_path / "rec" / "2026-11-10_14-50-00"
	job = copyqueue.Job(str(gone), str(tmp_path / "share"))
	copyqueue.CopyQueue.Record(job, "skipped", "the panel closed")
	assert not gone.exists()
