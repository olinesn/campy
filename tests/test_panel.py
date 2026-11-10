"""
campy-session panel (campy/panel.py): a recording run from a page in the
browser. Its pieces, and the whole thing over HTTP with emulated cameras and
campy-live --model none.
"""

import csv
import http.client
import json
import os
import re
import signal
import struct
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.request
import zlib

import numpy as np
import pytest

from campy import bookmarks, panel
from test_session import NAMES, emu_session, frames, home, recorded, the_folder # noqa: F401 (home: a fixture)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="Ctrl-C to the panel, as on Linux and macOS")


# ------------------------------------------------------------------ thumbnails

def read_png(data):
	# The pixels of a grey 8-bit PNG with filter 0 rows, as Png writes it
	assert data[:8] == b"\x89PNG\r\n\x1a\n"
	at, chunks = 8, {}
	while at < len(data):
		n, kind = struct.unpack(">I4s", data[at:at + 8])
		body = data[at + 8:at + 8 + n]
		assert struct.unpack(">I", data[at + 8 + n:at + 12 + n])[0] == zlib.crc32(kind + body) & 0xFFFFFFFF
		chunks[kind] = chunks.get(kind, b"") + body
		at += 12 + n
	w, h, depth, kind = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
	assert (depth, kind) == (8, 0)
	rows = np.frombuffer(zlib.decompress(chunks[b"IDAT"]), np.uint8).reshape(h, w + 1)
	assert not rows[:, 0].any()
	return rows[:, 1:]


def test_a_grey_png_of_the_pixels():
	img = (np.arange(48 * 64) % 251).astype(np.uint8).reshape(48, 64)
	assert np.array_equal(read_png(panel.Png(img)), img)


def test_a_thumbnail_is_grey_and_about_192_px_wide():
	frame = np.zeros((1024, 1280, 1), np.uint8)
	frame[:, 640:] = 200
	small = read_png(panel.Thumb(frame))
	assert small.shape == (147, 183) # every 7th pixel: 1024 and 1280 / 7, rounded up
	assert small[0, 0] == 0 and small[0, -1] == 200
	rgb = np.zeros((48, 64, 3), np.uint8)
	rgb[..., 0] = 90
	assert read_png(panel.Thumb(rgb))[0, 0] == 30 # the mean of the channels


# ------------------------------------------------------------------ bookmarks from two programs

def test_the_page_has_the_luc3d_logo_inline_and_fetches_nothing_from_elsewhere():
	import base64
	from campy import panellogo, panelpage
	page = panelpage.PAGE
	assert '<img class="logo" src="' + panellogo.LOGO in page and 'rel="icon" type="image/png" href="' + panellogo.ICON in page
	for uri in (panellogo.LOGO, panellogo.ICON):
		assert base64.b64decode(uri.split(",", 1)[1])[:8] == b"\x89PNG\r\n\x1a\n"
	assert not re.search(r"""(src|href)=["']?(https?:)?//""", page) # a rig may have no internet
	assert "Previous recording: " in page and "@LOGO@" not in page


def test_the_page_says_which_campy_it_runs(monkeypatch):
	# Version, branch and commit in the page's top right corner, as LUC3D shows its own
	from campy import manifest, panelpage
	assert 'id="version"' in panelpage.PAGE and "s.version" in panelpage.PAGE
	monkeypatch.setattr(manifest, "CampyInfo", lambda: {"version": "2.7.0", "commit": "874060ba82e8b4e27c120330934dc802e5780aa6",
		"branch": "pr358", "dirty": False, "describe": "v2.7.0-57-g874060b", "path": "/campy"})
	label = panel.VersionLabel()
	assert label["text"] == "v2.7.0 +57 \u00b7 pr358 \u00b7 874060ba" # 57 commits past the release: not the release
	assert "874060ba82e8b4e27c120330934dc802e5780aa6" in label["title"] and "pr358" in label["title"]
	assert "57 commits after the v2.7.0 release" in label["title"]
	assert panel.SinceRelease("v2.7.0-1-g874060b", "2.7.0") == ("+1", "1 commit after the v2.7.0 release")
	assert panel.SinceRelease("v2.6.0-12-g874060b", "2.7.0")[0] == "(v2.6.0 +12)" # the tag isn't setup.py's version
	assert panel.SinceRelease("v2.7.0-0-g874060b", "2.7.0") is None and panel.SinceRelease("v2.7.0", "2.7.0") is None
	assert panel.SinceRelease(None, "2.7.0") is None and panel.SinceRelease("874060b", "2.7.0") is None
	monkeypatch.setattr(manifest, "CampyInfo", lambda: {"version": "2.7.0", "commit": "874060ba82e8", "branch": "HEAD",
		"dirty": True, "path": "/campy"})
	assert panel.VersionLabel()["text"] == "v2.7.0 \u00b7 874060ba \u00b7 modified" # a detached checkout: no branch
	monkeypatch.setattr(manifest, "CampyInfo", lambda: {"version": "2.7.0", "installedFrom": "package", "path": "/campy"})
	assert panel.VersionLabel()["text"] == "v2.7.0"
	def broken():
		raise OSError("no git")
	monkeypatch.setattr(manifest, "CampyInfo", broken)
	assert panel.VersionLabel()["text"] == "version unknown"


def mark(note, cameras=2):
	return {"time": "2026-10-23 10:00:00.000", "host_ns": 1, "event": "mark", "note": note, "frame": 5,
		"cameras": [(5, 1)] * cameras, "recording": ""}


def test_two_programs_bookmarking_one_file_number_on_from_each_other(tmp_path):
	# campy-live-view and the panel both write the recording's bookmarks.csv
	path = str(tmp_path / bookmarks.BOOKMARKS)
	view, page = bookmarks.Bookmarks(path), bookmarks.Bookmarks(path)
	for who, note in ((view, "a"), (page, "b"), (page, "c"), (view, "d")):
		who.Add(mark(note))
	with open(path, newline="") as f:
		rows = list(csv.reader(f))
	assert [r[0] for r in rows[1:]] == ["1", "2", "3", "4"]
	assert [r[4] for r in rows[1:]] == ["a", "b", "c", "d"]


# ------------------------------------------------------------------ what other programs wrote

def test_the_alerts_logs_give_what_is_open_and_what_was_resolved(tmp_path):
	def line(key, state, text):
		return json.dumps({"time": "2026-10-23T10:00:00-04:00", "key": key, "severity": "warning", "event": "problem",
			"state": state, "text": text}) + "\n"
	(tmp_path / "alerts.log").write_text(line("Camera1/no-frames", "open", "no frames") +
		line("Camera1/no-frames", "resolved", "frames again") + line("disk/low", "open", "disk low") + "not json\n")
	(tmp_path / "alerts-live.log").write_text(line("live/campy-live", "open", "no result for 30 s"))
	open_, resolved = panel.Alerts(str(tmp_path))
	assert [a["key"] for a in open_] == ["disk/low", "live/campy-live"]
	assert [a["text"] for a in resolved] == ["frames again"]
	assert panel.Alerts(str(tmp_path / "missing")) == ([], [])


def test_recording_json_only_when_it_is_this_recordings_and_fresh(tmp_path, monkeypatch):
	monkeypatch.setenv("CAMPY_STATUS_DIR", str(tmp_path))
	path = tmp_path / "recording.json"
	path.write_text(json.dumps({"schema": 1, "session": {"folder": str(tmp_path / "rec")}, "cameras": []}))
	assert panel.RecordingStatus(str(tmp_path / "rec"))["schema"] == 1
	assert panel.RecordingStatus(str(tmp_path / "other")) is None
	old = time.time() - 60
	os.utime(str(path), (old, old))
	assert panel.RecordingStatus(str(tmp_path / "rec")) is None


# ------------------------------------------------------------------ the page's requests, and a second launch

def stub_panel():
	# The real Serve's handler, on a Panel whose actions are stubs: nothing starts
	calls = []
	p = object.__new__(panel.Panel)
	p.token = "T0kenT0kenT0kenT0kenT0ke"
	p.port = 19700 + os.getpid() % 1000
	p.health = types.SimpleNamespace(thumbs={})
	p.Status = lambda: {"state": "ready"}
	p.Start = lambda body: calls.append(("start", body)) or None
	p.Stop = lambda level: calls.append(("stop", level)) or None
	p.Checks = lambda: []
	p.Serve()
	return p, calls


def test_a_start_refused_for_a_copy_folder_not_there_yet_offers_to_make_it(tmp_path):
	from campy import copyqueue
	p, calls = stub_panel()
	try:
		key = "?t=" + p.token
		new = tmp_path / "luc3d-live"
		p.Start = lambda body: copyqueue.Problem(body.get("copy_to"))
		p.MakeCopyFolder = lambda path: copyqueue.Create(path)
		code, said = raw(p, "POST", "/start" + key, {"Content-Type": "application/json"}, json.dumps({"copy_to": str(new)}))
		assert code == 409 and said["create"] == str(new) and "doesn't exist yet" in said["error"]
		code, said = raw(p, "POST", "/start" + key, {"Content-Type": "application/json"},
			json.dumps({"copy_to": str(tmp_path / "no" / "where")}))
		assert code == 409 and "create" not in said # nothing to offer: the folder it goes in isn't there
		assert raw(p, "POST", "/copy-folder" + key, {"Content-Type": "application/json"}, json.dumps({"path": str(new)})) == \
			(200, {"ok": True})
		assert new.is_dir()
		assert raw(p, "POST", "/start" + key, {"Content-Type": "application/json"}, json.dumps({"copy_to": str(new)})) == \
			(200, {"ok": True})
	finally:
		p.server.shutdown()
		p.server.server_close()


def raw(p, method, path, headers=None, body=None):
	c = http.client.HTTPConnection("127.0.0.1", p.port, timeout=10)
	try:
		c.request(method, path, body=body, headers=headers or {})
		r = c.getresponse()
		data = r.read()
		if r.getheader("Content-Type", "").startswith("text/html"):
			return r.status, data.decode("utf-8") # a page (the old tab's), not JSON
		return r.status, json.loads(data or b"{}")
	finally:
		c.close()


def test_the_page_answers_only_its_key_from_this_computer():
	p, calls = stub_panel()
	try:
		key = "?t=" + p.token
		assert raw(p, "GET", "/status.json" + key) == (200, {"state": "ready"})
		assert raw(p, "GET", "/status.json", {"X-Campy-Token": p.token}) == (200, {"state": "ready"})
		assert raw(p, "GET", "/status.json" + key, {"Host": "localhost:{}".format(p.port)})[0] == 200
		assert raw(p, "GET", "/status.json?t=wrong")[0] == 403
		# A key that isn't ASCII is a wrong key (it made compare_digest raise: a traceback, the connection dropped)
		code, said = raw(p, "GET", "/status.json?t=%C3%A9")
		assert code == 403 and "wrong key" in said["error"]
		assert raw(p, "GET", "/", {"X-Campy-Token": "\u00e9"})[0] == 403
		# The key alone isn't enough from a page that isn't this computer's: another name (DNS rebinding), or a POST
		# from another site
		code, said = raw(p, "GET", "/status.json" + key, {"Host": "evil.example:{}".format(p.port)})
		assert code == 403 and "not from this computer's panel page" in said["error"]
		for origin in ("https://evil.example", "null", "http://evil.example:{}".format(p.port)):
			code, said = raw(p, "POST", "/start" + key, {"Origin": origin, "Content-Type": "text/plain"}, '{"notes": "x"}')
			assert code == 403, origin
		assert calls == []
		code, said = raw(p, "POST", "/start" + key, {"Origin": "http://127.0.0.1:{}".format(p.port),
			"Content-Type": "application/json"}, '{"notes": "x"}')
		assert code == 200 and calls == [("start", {"notes": "x"})]
	finally:
		p.server.shutdown()
		p.server.server_close()


def test_the_pages_key_is_kept_for_the_next_panel(home):
	# A tab left open, or restored by the browser, works with the next panel; --new-key, or a file others can read, not
	key = panel.PanelKey()
	assert panel.PanelKey() == key and len(key) >= 20
	if sys.platform != "win32":
		assert os.stat(panel.KeyFile()).st_mode & 0o077 == 0 # its owner's only
		os.chmod(panel.KeyFile(), 0o644)
		other = panel.PanelKey()
		assert other != key and os.stat(panel.KeyFile()).st_mode & 0o077 == 0
		key = other
	assert panel.PanelKey(new=True) != key
	with open(panel.KeyFile(), "w") as f:
		f.write("not json")
	assert len(panel.PanelKey()) >= 20


def test_an_old_tab_gets_a_page_that_says_so(tmp_path):
	p, calls = stub_panel()
	try:
		code, page = raw(p, "GET", "/?t=OldKeyOldKeyOldKeyOldKey")
		assert code == 403 and "This tab belongs to a panel that was closed" in page
		code, said = raw(p, "GET", "/status.json?t=OldKeyOldKeyOldKeyOldKey")
		assert code == 403 and "wrong key" in said["error"] # the page's own polls: JSON, which the page shows as a banner
		assert calls == []
	finally:
		p.server.shutdown()
		p.server.server_close()


def test_the_page_says_when_its_panel_is_closed_or_its_key_is_old():
	from campy import panelpage
	assert 'id="gone"' in panelpage.PAGE and "The panel is closed" in panelpage.PAGE
	assert "This page reconnects by itself" in panelpage.PAGE and "This tab belongs to a panel that was closed" in panelpage.PAGE


def campy_standin():
	# A live process whose command line says campy, as campy-session's does
	return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "campy-session-standin"])


def second_launch(home, config):
	env = dict(os.environ, PYTHONPATH=REPO, HOME=str(home), USERPROFILE=str(home))
	return subprocess.run([sys.executable, "-m", "campy.session", "panel", str(config), "--no-browser"], env=env,
		capture_output=True, text=True, timeout=60)


def test_a_second_launch_opens_the_panel_already_open(tmp_path, home):
	# The desktop icon double-clicked while the panel is open: its page, not "Another campy-session is running"
	config, clips = emu_session(tmp_path, home, 5)
	p, calls = stub_panel()
	first = campy_standin()
	try:
		status = home / ".campy" / "status"
		status.mkdir(parents=True)
		(status / "launcher.lock").write_text("{} x\n".format(first.pid))
		(status / "panel.json").write_text(json.dumps({"pid": first.pid, "url": p.url, "config": str(tmp_path / "a.yaml")}))
		out = second_launch(home, config)
		assert out.returncode == 0, out.stdout + out.stderr
		assert "The panel is already open (pid {}): {}".format(first.pid, p.url) in out.stdout
		assert "It was opened with {}".format(tmp_path / "a.yaml") in out.stdout # another config's: said
		# A panel.json whose page doesn't answer (that panel was killed) is passed by: the lock says why not
		p.server.shutdown()
		p.server.server_close()
		out = second_launch(home, config)
		assert out.returncode == 1 and "Another campy-session is running on this computer (pid {})".format(first.pid) \
			in out.stderr
	finally:
		first.kill()
		first.wait()
		if p.server.socket.fileno() != -1:
			p.server.shutdown()
			p.server.server_close()


# ------------------------------------------------------------------ the panel, over HTTP

def start_panel(tmp_path, home, config):
	env = dict(os.environ, PYTHONPATH=REPO, PYTHONUNBUFFERED="1", HOME=str(home), USERPROFILE=str(home),
		CAMPY_STATUS_DIR=str(home / "status"))
	env.pop("IMAGEIO_FFMPEG_EXE", None)
	env.pop("CAMPY_LIVE_PYTHON", None)
	proc = subprocess.Popen([sys.executable, "-m", "campy.session", "panel", str(config), "--no-browser", "--port",
		str(18700 + os.getpid() % 1000)], cwd=str(tmp_path), env=env, stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT, text=True, start_new_session=sys.platform != "win32")
	lines = []
	threading.Thread(target=lambda: lines.extend(proc.stdout), daemon=True).start()
	t0 = time.monotonic()
	while time.monotonic() - t0 < 60:
		found = [re.search(r"The panel: (http://\S+)", line) for line in list(lines)]
		found = [m for m in found if m]
		if found:
			return proc, lines, found[0].group(1)
		assert proc.poll() is None, "".join(lines)
		time.sleep(0.1)
	raise AssertionError("no panel address: " + "".join(lines))


def call(url, path, body=None):
	base, key = url.split("/?t=")
	request = urllib.request.Request("{}{}{}t={}".format(base, path, "&" if "?" in path else "?", key),
		data=None if body is None else json.dumps(body).encode(), method="GET" if body is None else "POST",
		headers={"Content-Type": "application/json"})
	try:
		with urllib.request.urlopen(request, timeout=10) as r:
			data = r.read()
			return r.status, (json.loads(data) if r.headers.get_content_type() == "application/json" else data)
	except urllib.error.HTTPError as e:
		return e.code, json.loads(e.read() or b"{}")


def wait(url, test, timeout=60):
	t0 = time.monotonic()
	while time.monotonic() - t0 < timeout:
		status = call(url, "/status.json")[1]
		if test(status):
			return status
		time.sleep(0.3)
	raise AssertionError("waited in vain; the last status: {}".format(json.dumps(status)[:2000]))


@POSIX
def test_a_recording_from_the_page(tmp_path, home):
	config, clips = emu_session(tmp_path, home, 60, live={"model": "none"}, feed="campy-test-panel",
		post={"remux": True, "export": False})
	proc, lines, url = start_panel(tmp_path, home, config)
	try:
		# The key: nothing without it
		with pytest.raises(urllib.error.HTTPError) as e:
			urllib.request.urlopen(url.split("?")[0] + "status.json", timeout=10)
		assert e.value.code == 403
		code, page = call(url, "/")
		assert code == 200 and b"campy session" in page
		status = call(url, "/status.json")[1]
		assert status["state"] == "ready" and status["launcher"] is None
		assert [c["level"] for c in status["checks"]] == ["ok", "ok", "ok"]
		assert call(url, "/stop", {})[0] == 409 # nothing to stop

		# Start, with the experiment's fields
		assert call(url, "/start", {"experiment": "Panel test", "animal_ids": "M1, M2", "notes": ""}) == (200, {"ok": True})
		assert call(url, "/start", {})[0] == 409 # one at a time
		status = wait(url, lambda s: s["state"] == "recording" and s["health"].get("feed") and
			all(c["frame"] > 20 for c in s["health"]["cameras"]))
		assert [c["name"] for c in status["health"]["cameras"]] == NAMES
		assert all(c["problems"] == [] and abs(c["fps"] - 20) < 2 for c in status["health"]["cameras"])
		assert status["launcher"]["recording"]["planned_s"] == 60
		folder = status["launcher"]["folder"]
		time.sleep(1.2)
		for c in range(len(NAMES)):
			code, png = call(url, "/thumb/{}.png".format(c))
			assert code == 200 and read_png(png).shape == (48, 64)

		# A bookmark, into the recording's bookmarks.csv
		assert call(url, "/bookmark", {"note": "  rears   at the feeder "}) == (200, {"ok": True})
		with open(os.path.join(folder, bookmarks.BOOKMARKS), newline="") as f:
			rows = list(csv.reader(f))
		assert rows[1][0] == "1" and rows[1][4] == "rears at the feeder" and int(rows[1][5]) > 20
		assert call(url, "/status.json")[1]["bookmarks"][0]["note"] == "rears at the feeder"

		# Stop: the recording stops as at Ctrl-C, its post-steps run, and the panel is ready for the next
		assert call(url, "/stop", {}) == (200, {"ok": True})
		status = wait(url, lambda s: s["state"] == "ready" and s["launcher"] and s["launcher"]["state"] == "done")
		assert status["launcher"]["parts"]["acquire"]["exit_code"] == 0
		assert status["launcher"]["post"]["remux"]["state"] == "done"
		# What went wrong in it (campy/endsummary.py): with the emulated cameras, nothing
		assert status["summary"]["lines"][0]["text"].startswith("All {} cameras: every frame saved".format(len(NAMES)))
		counts = recorded(the_folder(clips))
		assert min(counts) > 20 and max(counts) < 60 * 20, counts
		manifest = json.load(open(os.path.join(folder, "session.json")))
		assert manifest["experiment"]["name"] == "Panel test" and manifest["experiment"]["animalIds"] == ["M1", "M2"]
	finally:
		if proc.poll() is None:
			os.killpg(proc.pid, signal.SIGINT)
		try:
			proc.wait(timeout=30)
		except subprocess.TimeoutExpired:
			os.killpg(proc.pid, signal.SIGKILL)
	assert proc.returncode == 0, "".join(lines)
	assert "[panel] Closed." in "".join(lines)
	assert not (home / ".campy" / "status" / "launcher.lock").exists()

	# A panel opened again shows that recording as the last one, with its steps and its summary, at the same address:
	# a tab left open from the first panel works with it
	first_url = url
	proc, lines, url = start_panel(tmp_path, home, config)
	try:
		assert url == first_url
		status = call(url, "/status.json")[1]
		assert status["state"] == "ready" and status["launcher"]["folder"] == folder
		assert status["launcher"]["post"]["remux"]["state"] == "done" and status["launcher"]["clean"]["clean"]
		assert status["summary"]["lines"][0]["text"].startswith("All {} cameras".format(len(NAMES)))
	finally:
		os.killpg(proc.pid, signal.SIGINT)
		proc.wait(timeout=30)


@POSIX
def test_each_recording_is_copied_into_the_pages_folder(tmp_path, home):
	# The page's "Copy each recording to": checked at Start, remembered for the next panel, and each recording copied
	# in the background once it has ended (not by the post copy step, which would keep the next recording waiting)
	config, clips = emu_session(tmp_path, home, 3, post={"remux": True, "export": False})
	share = tmp_path / "share"
	share.mkdir()
	proc, lines, url = start_panel(tmp_path, home, config)
	try:
		assert call(url, "/status.json")[1]["fields"]["copy_to"] == ""
		code, said = call(url, "/start", {"copy_to": str(tmp_path / "no" / "where")})
		assert code == 409 and said["error"].startswith("Can't reach ") and "create" not in said
		# A folder not there yet, in one that is: refused, with the offer to make it (the page's Create this folder)
		code, said = call(url, "/start", {"copy_to": str(share / "new")})
		assert code == 409 and said["create"] == str(share / "new") and "doesn't exist yet" in said["error"]
		assert call(url, "/copy-folder", {"path": str(share / "new")}) == (200, {"ok": True}) and (share / "new").is_dir()
		(share / "new").rmdir()
		assert call(url, "/start", {"experiment": "Copy test", "copy_to": str(share)}) == (200, {"ok": True})
		status = wait(url, lambda s: s["state"] == "ready" and s["launcher"] and s["launcher"]["state"] == "done" and
			s["copies"]["jobs"] and s["copies"]["jobs"][0]["state"] == "done")
		folder = status["launcher"]["folder"]
		assert status["launcher"]["post"]["copy"]["state"] == "skipped" # the panel's copy, not post's
		name = os.path.basename(folder)
		for cam in NAMES:
			assert os.path.getsize(str(share / name / cam / "emuclip.mp4")) == os.path.getsize(
				os.path.join(folder, cam, "emuclip.mp4"))
		assert json.load(open(os.path.join(folder, "logs", "post.json")))["steps"]["copy"]["to"] == str(share)
		assert status["fields"]["copy_to"] == str(share) # remembered for this config
	finally:
		if proc.poll() is None:
			os.killpg(proc.pid, signal.SIGINT)
		proc.wait(timeout=30)
	assert proc.returncode == 0, "".join(lines)


@POSIX
def test_a_start_that_a_check_stops(tmp_path, home):
	config, clips = emu_session(tmp_path, home, 5, live={"model": "none", "python": str(tmp_path / "no-python")})
	proc, lines, url = start_panel(tmp_path, home, config)
	try:
		# Its address is kept for a second launch (the desktop icon double-clicked again), readable by its user only
		info = json.loads((home / ".campy" / "status" / "panel.json").read_text())
		assert info["url"] == url and info["pid"] == proc.pid
		assert (home / ".campy" / "status" / "panel.json").stat().st_mode & 0o777 == 0o600
		second = second_launch(home, config)
		assert second.returncode == 0 and "The panel is already open (pid {}): {}".format(proc.pid, url) in \
			second.stdout, second.stdout + second.stderr
		status = call(url, "/status.json")[1]
		assert status["checks"][0]["level"] == "block"
		code, said = call(url, "/start", {})
		assert code == 409 and said["error"].startswith("Can't start: Could not run campy-live's python")
		assert call(url, "/status.json")[1]["state"] == "ready"
	finally:
		os.killpg(proc.pid, signal.SIGINT)
		proc.wait(timeout=30)
	assert not any(d.is_dir() and re.match(r"\d{4}-", d.name) for d in clips.iterdir())
	assert not (home / ".campy" / "status" / "panel.json").exists() # closed: a next launch opens a new panel


@POSIX
def test_a_recording_outlives_its_panel_and_the_next_panel_finds_it(tmp_path, home):
	# The panel's window closed (here: the panel killed) mid-recording: the recording goes on, apart from it; a
	# panel opened again refuses to start another, says where it is, and can stop it
	config, clips = emu_session(tmp_path, home, 120, live={"model": "none"}, feed="campy-test-orphan",
		post={"remux": False, "export": False})
	proc, lines, url = start_panel(tmp_path, home, config)
	try:
		assert call(url, "/start", {}) == (200, {"ok": True})
		status = wait(url, lambda s: s["state"] == "recording" and s["health"].get("feed") and
			all(c["frame"] > 20 for c in s["health"]["cameras"]))
		acquire = status["launcher"]["parts"]["acquire"]["pid"]
		assert os.getsid(acquire) != os.getsid(proc.pid) # apart from the panel's terminal: no hang-up reaches it
		os.killpg(proc.pid, signal.SIGKILL) # the panel's whole terminal session, as closing its window would
		proc.wait(timeout=30)
		time.sleep(2)
		assert panel.children.Alive(acquire)
	finally:
		if proc.poll() is None:
			os.killpg(proc.pid, signal.SIGKILL)

	proc, lines, url = start_panel(tmp_path, home, config)
	try:
		status = call(url, "/status.json")[1]
		assert status["orphan"]["pid"] == acquire
		assert os.path.samefile(status["orphan"]["folder"], str(the_folder(clips)))
		code, said = call(url, "/start", {})
		assert code == 409 and "still running" in said["error"]
		assert call(url, "/stop-orphan", {}) == (200, {"ok": True})
		t0 = time.monotonic()
		while panel.children.Alive(acquire) and time.monotonic() - t0 < 60:
			time.sleep(0.3)
		assert not panel.children.Alive(acquire)
		wait(url, lambda s: s["orphan"] is None)
		counts = recorded(the_folder(clips))
		assert 20 < min(counts) and max(counts) < 120 * 20, counts
	finally:
		os.killpg(proc.pid, signal.SIGINT)
		proc.wait(timeout=30)


# ------------------------------------------------------------------ the disk check before the start

FAKE_SDK = os.path.join(REPO, "tests", "fake_sglx")


def disk_panel(tmp_path, monkeypatch, free_gb, devices=True, video_folder=None, six_days_into=None):
	# A panel for 4 cameras at 60 fps: with a Pico (8 h and 6 days, its board and SpikeGLX answering) and SpikeGLX, or
	# with neither; its disk holding free_gb. Nothing records: Start only hands the main thread its fields.
	from campy import pico, spikeglx
	from test_session import write_config
	sections = {"pico": {"port": "E665485457699D30", "lengths": [{"name": "8 h", "seconds": 28800},
		dict({"name": "6 days", "seconds": 518400}, **({"videoFolder": str(six_days_into)} if six_days_into else {}))]},
		"spikeglx": {"sdk": FAKE_SDK}} if devices else {}
	config = write_config(tmp_path / "c.yaml", {"numCams": 4, "frameRate": 60, "videoFolder": str(video_folder or tmp_path / "data"),
		"recTimeInSec": 3600}, **sections)
	monkeypatch.setattr(pico.Pico, "Check", lambda self: "COM3: idle")
	def answers(self):
		self.margin = 1.0 # trgTTLMarginS, as SpikeGLX's check reads it
		return "SpikeGLX: idle, TTL trigger"
	monkeypatch.setattr(spikeglx.SpikeGLX, "Check", answers)
	monkeypatch.setattr(panel.Panel, "Free", lambda self, folder=None: free_gb * 1e9)
	return panel.Panel(str(config), browser=False)


def disk_check(p):
	return [c for c in p.checks if c[1].startswith("Disk")]


def test_a_length_that_fits_on_the_disk(tmp_path, home, monkeypatch):
	# The 8-hour rehearsal: SpikeGLX 151 GB (6,272 s of gate with its margin, at 24 MB/s; 150.3 GB measured on
	# 6 November) and 4 cameras' 4,640 s at 0.6 MB/s, 11.1 GB: about 162 GB of 370 GB free
	p = disk_panel(tmp_path, monkeypatch, 370)
	assert p.length == 28800
	[(level, text, action)] = disk_check(p)
	assert (level, action) == ("ok", "disk")
	assert text == "Disk: this length needs about 162 GB of the 370 GB free, an estimate (SpikeGLX 151 GB: 6,272 s of " \
		"open gate with its trigger margin, at 24 MB/s; videos about 11 GB, at 0.6 MB/s a camera; plus 2 GB kept free, " \
		"stopAtFreeGB)", text
	assert not any(c[1].endswith("GB free where it records") for c in p.checks) # the need, in its place
	assert p.Start({"length": 28800}) is None and p.wanted == {"length": 28800}


def test_a_length_that_does_not_fit_is_refused(tmp_path, home, monkeypatch):
	# 6 days: about 2,910 GB, against 378 GB free (the ChronicRecord PC's one disk, the day it nearly started)
	p = disk_panel(tmp_path, monkeypatch, 378)
	assert p.Start({"length": 518400}).startswith("Can't start: Disk: this length needs about 2,910 GB of the 378 GB "
		"free: 2,534 GB short, an estimate")
	assert p.wanted is None and p.length == 518400
	[(level, text, action)] = disk_check(p)
	assert (level, action) == ("block", "disk") and text.endswith("Pick a shorter length, or add a data disk.")
	# The page's confirm, naming the shortfall, passes over the disk check alone
	assert p.Start({"length": 518400, "disk_override": True}) is None and p.wanted["length"] == 518400
	p.wanted = None
	# ... never another check's block
	from campy import pico
	monkeypatch.setattr(pico.Pico, "Check", lambda self: (_ for _ in ()).throw(pico.PicoError("The Pico doesn't answer")))
	said = p.Start({"length": 518400, "disk_override": True})
	assert said == "Can't start: The Pico doesn't answer"
	assert p.wanted is None
	# The shorter length fits: chosen on the page, the check follows it
	p.length = 28800
	monkeypatch.setattr(pico.Pico, "Check", lambda self: "COM3: idle")
	p.checks = p.Checks()
	assert disk_check(p)[0][0] == "ok"
	# Near the edge (more than 80% of the free space): a warning
	monkeypatch.setattr(panel.Panel, "Free", lambda self, folder=None: 190e9)
	assert p.Checks()[-1][0] == "warn"


def test_the_copy_folder_cant_be_where_the_chosen_length_records(tmp_path, home, monkeypatch):
	# A Pico length with a videoFolder of its own (the 6-day session's): the copy box can't name that folder, though
	# the config's videoFolder is elsewhere; the config's own folder is fine for a length that records elsewhere
	chronic, data = tmp_path / "chronic", tmp_path / "data"
	chronic.mkdir()
	data.mkdir()
	p = disk_panel(tmp_path, monkeypatch, 100000, six_days_into=chronic)
	assert p.RecordsInto() == str(data) # 8 h, the first length
	said = p.Start({"length": 518400, "copy_to": str(chronic)})
	assert said and "where the recordings are made" in said and p.wanted is None
	assert p.Start({"length": 518400, "copy_to": str(data)}) is None and p.wanted["length"] == 518400


def test_without_a_pico_the_disk_check_is_as_before(tmp_path, home, monkeypatch):
	p = disk_panel(tmp_path, monkeypatch, 15, devices=False)
	assert p.devices is None and p.lengths == [] and not disk_check(p)
	assert p.checks[-1] == ("warn", "15 GB free where it records") # below alertDiskFreeGB (20)
	assert p.Start({}) is None
	# The tile: hours at the rate it fills
	assert p.Disk(15e9, 1e6, None) == {"free_gb": 15.0, "hours_left": 4.2, "plan_left_gb": None, "drives": []}


def test_the_disk_tile_counts_the_rest_of_a_picos_plan(tmp_path, home, monkeypatch):
	# Not hours at the rate so far (none before the Pico starts: "about 65 h"), but what the plan still needs
	p = disk_panel(tmp_path, monkeypatch, 378)
	s = types.SimpleNamespace(folder=str(tmp_path / "rec"))
	p.devices.Plan(28800)
	assert p.Disk(378e9, 1.0e6, s) == {"free_gb": 378.0, "hours_left": None, "plan_left_gb": 161.7, "drives": []}
	p.devices.pico_started = time.time()
	p.devices.cached = {"pico": {"alive": True, "session_s": 14400}} # half way: 48 of 96 bouts left
	assert p.Disk(378e9, 1.0e6, s)["plan_left_gb"] == round(p.devices.Need(28800, 4, 14400)["total_b"] / 1e9, 1) == 80.8
	p.devices.cached = {"pico": {"alive": False}} # its session has ended
	assert p.Disk(378e9, 1.0e6, s) == {"free_gb": 378.0, "hours_left": None, "plan_left_gb": None, "drives": []}


# ------------------------------------------------------------------ the disk tile's drives (its pies)

def fake_drives(monkeypatch, tmp_path, sizes):
	# Drives of made-up sizes: sizes {"C:": (total GB, free GB), ...}, each the folder tmp_path/<letter> and what's in it
	mounts = {}
	for name, (total, free) in sizes.items():
		folder = tmp_path / name[0]
		folder.mkdir(exist_ok=True)
		mounts[str(folder)] = (name + "\\", total, free)
	def mount(path):
		path = os.path.abspath(str(path))
		return next((m for f, (m, _, _) in mounts.items() if path == f or path.startswith(f + os.sep)), None)
	def usage(where):
		_, total, free = next(v for v in mounts.values() if v[0] == where)
		return types.SimpleNamespace(total=total * 1e9, used=(total - free) * 1e9, free=free * 1e9)
	monkeypatch.setattr(panel, "Mount", mount)
	monkeypatch.setattr(panel, "DiskUsage", usage)


def test_a_drives_name_on_windows_and_elsewhere():
	import ntpath
	import posixpath
	assert panel.DriveName("D:\\", ntpath) == "D:"
	assert panel.DriveName("\\\\server\\share\\", ntpath) == "\\\\server\\share"
	assert panel.DriveName("C:\\mounts\\big\\", ntpath) == "C:\\mounts\\big" # a drive mounted in a folder
	assert panel.DriveName("/", posixpath) == "/" and panel.DriveName("/mnt/data/", posixpath) == "/mnt/data"


def test_a_drive_is_measured_where_a_folder_is(tmp_path, monkeypatch):
	# The 4 TB data drive: a folder on it not made yet still counts as on it (its nearest existing folder)
	fake_drives(monkeypatch, tmp_path, {"D:": (4000.8, 3998.1)})
	assert panel.Drive(tmp_path / "D" / "recordings" / "2026-11-10") == {"name": "D:", "mount": "D:\\",
		"total_gb": 4000.8, "used_gb": 2.7, "free_gb": 3998.1}
	assert panel.Drive(tmp_path / "elsewhere") is None # no drive answers for it


def test_the_real_mount_of_a_folder(tmp_path):
	# Walked up from a folder not made yet to the mount point it is on; the real drive measured there
	mount = panel.Mount(tmp_path / "not" / "made")
	assert os.path.ismount(mount) and os.path.normcase(str(tmp_path)).startswith(os.path.normcase(mount))
	drive = panel.Drive(tmp_path / "not" / "made")
	assert drive["mount"] == mount and 0 < drive["free_gb"] <= drive["total_gb"]
	assert drive["used_gb"] == pytest.approx(drive["total_gb"] - drive["free_gb"], abs=0.11)


def test_the_tile_shows_the_recordings_drive_with_the_plan_and_the_copies_drive(tmp_path, home, monkeypatch):
	# Recording onto the new 4 TB D:, copies onto C:. The plan's estimate is D:'s (SpikeGLX writes into the
	# recording's folder too); C: shows only its own space.
	p = disk_panel(tmp_path, monkeypatch, 3998)
	fake_drives(monkeypatch, tmp_path, {"C:": (1022.4, 460.2), "D:": (4000.8, 3998.1)})
	p.devices.Plan(28800)
	s = types.SimpleNamespace(folder=str(tmp_path / "D" / "rec"), state="recording")
	p.session, p.copy_to = s, str(tmp_path / "C")
	disk = p.Disk(3998.1e9, 1.0e6, s, s.folder)
	assert disk["plan_left_gb"] == 161.7
	assert disk["drives"] == [
		{"name": "D:", "mount": "D:\\", "total_gb": 4000.8, "used_gb": 2.7, "free_gb": 3998.1, "recording": True,
			"copies": False, "plan_gb": 161.7},
		{"name": "C:", "mount": "C:\\", "total_gb": 1022.4, "used_gb": 562.2, "free_gb": 460.2, "recording": False,
			"copies": True, "plan_gb": None}]
	# Copies onto the same drive: one pie, marked as the copies' too
	p.copy_to = str(tmp_path / "D" / "copies")
	os.makedirs(p.copy_to)
	assert [(d["name"], d["recording"], d["copies"]) for d in p.Drives(s.folder, 161.7)] == [("D:", True, True)]
	# An rsync address has no drive here; a destination that isn't there, none either
	for to in ("me@server:/data", str(tmp_path / "C" / "gone")):
		p.copy_to = to
		assert [d["name"] for d in p.Drives(s.folder)] == ["D:"]
	# After it: the copies still going are shown, not the page's box
	p.session, p.copy_to = None, None
	monkeypatch.setattr(p.copies, "Status", lambda: {"jobs": [{"to": str(tmp_path / "C"), "state": "copying"}]})
	monkeypatch.setattr(panel.Panel, "CopyTo", lambda self: str(tmp_path / "D" / "copies"))
	assert [(d["name"], d["copies"]) for d in p.Drives(s.folder)] == [("D:", False), ("C:", True)]
	# ... and with none going, the box's
	monkeypatch.setattr(p.copies, "Status", lambda: {"jobs": [{"to": str(tmp_path / "C"), "state": "done"}]})
	assert [(d["name"], d["copies"]) for d in p.Drives(s.folder)] == [("D:", True)]
	# Without a drive to measure (the folder's drive gone): no pies, the free space as before
	assert p.Disk(None, None, None, str(tmp_path / "E" / "rec"))["drives"] == [
		dict(panel.Drive(tmp_path / "D"), recording=False, copies=True, plan_gb=None)]


def test_a_copies_drive_measured_at_most_every_10_s(tmp_path, home, monkeypatch):
	# A network share can be slow to answer: the page asks every second, its drive is measured every 10 s
	p = disk_panel(tmp_path, monkeypatch, 3998, devices=False)
	fake_drives(monkeypatch, tmp_path, {"C:": (1022.4, 460.2), "D:": (4000.8, 3998.1)})
	asked = []
	usage = panel.DiskUsage
	monkeypatch.setattr(panel, "DiskUsage", lambda where: asked.append(where) or usage(where))
	p.session, p.copy_to = types.SimpleNamespace(folder=str(tmp_path / "D")), str(tmp_path / "C")
	now = [1000.0]
	monkeypatch.setattr(panel.time, "monotonic", lambda: now[0])
	for _ in range(5):
		p.Drives(str(tmp_path / "D"))
	assert asked.count("C:\\") == 1 and asked.count("D:\\") == 5
	now[0] += panel.DRIVE_FRESH_S + 1
	p.Drives(str(tmp_path / "D"))
	assert asked.count("C:\\") == 2


def test_the_copies_drive_has_room_for_the_recording(tmp_path, home, monkeypatch):
	# Before the start, with a Pico's plan: a warning when the copy destination can't hold this length's recording
	# (162 GB for 8 hours), never a block
	fake_drives(monkeypatch, tmp_path, {"C:": (1022.4, 100.0), "D:": (4000.8, 3998.1)})
	monkeypatch.setattr(panel.Panel, "CopyTo", lambda self: str(tmp_path / "C"))
	p = disk_panel(tmp_path, monkeypatch, 3998, video_folder=tmp_path / "D" / "data")
	[(level, text, action)] = [c for c in p.checks if c[2:] == ("copy-disk",)]
	assert (level, action) == ("warn", "copy-disk")
	assert text == "Copies: into {} (C:), 100 GB free; this length's recording is about 162 GB: it doesn't fit, " \
		"62 GB short. Make room there, or copy somewhere else.".format(tmp_path / "C"), text
	assert p.Start({"length": 28800}) is None # a warning: it starts
	p.wanted = None
	# Room enough: ok (measured again: a copy destination's drive is kept for up to 10 s)
	fake_drives(monkeypatch, tmp_path, {"C:": (1022.4, 460.2), "D:": (4000.8, 3998.1)})
	p.drives.clear()
	assert [c[0] for c in p.Checks() if c[2:] == ("copy-disk",)] == ["ok"]
	# On the recordings' own drive: room for both
	monkeypatch.setattr(panel.Panel, "CopyTo", lambda self: str(tmp_path / "D"))
	fake_drives(monkeypatch, tmp_path, {"D:": (4000.8, 300.0)})
	p.drives.clear()
	[(level, text, _)] = [c for c in p.Checks() if c[2:] == ("copy-disk",)]
	assert level == "warn" and "on the same drive as the recordings (D:): room for both, about 323 GB of the 300 GB" in text
	# No destination, or an rsync one: no line
	for to in ("", "me@server:/data"):
		monkeypatch.setattr(panel.Panel, "CopyTo", lambda self, to=to: to)
		assert not [c for c in p.Checks() if c[2:] == ("copy-disk",)]


def test_the_copy_room_is_the_folder_typed_in_the_box(tmp_path, home, monkeypatch):
	# The box starts from the experiment's usual folder, on C: with room. The user types another share instead, one that
	# is nearly full: Start copies there, so the check and the Disk free tile measure it, not the usual folder.
	fake_drives(monkeypatch, tmp_path, {"C:": (1022.4, 460.2), "D:": (4000.8, 3998.1), "S:": (8000.0, 50.0)})
	monkeypatch.setattr(panel.Panel, "CopyTo", lambda self: str(tmp_path / "C"))
	p = disk_panel(tmp_path, monkeypatch, 3998, video_folder=tmp_path / "D" / "data")
	room = lambda: [c[:2] for c in p.checks if c[2:] == ("copy-disk",)]
	copies = lambda: [d["name"] for d in p.Drives(str(tmp_path / "D")) if d["copies"]]
	assert [level for level, _ in room()] == ["ok"] and copies() == ["C:"] # before the page has said: the usual folder
	assert p.Status()["fields"]["copy_box"] is None
	p.Serve()
	try:
		def box(to):
			return raw(p, "POST", "/check?t=" + p.token, {"Content-Type": "application/json"}, json.dumps({"copy_to": to}))
		share = str(tmp_path / "S" / "lab" / "mouse 12")
		os.makedirs(share)
		assert box(share) == (200, {"ok": True})
		assert room() == [("warn", "Copies: into {} (S:), 50 GB free; this length's recording is about 162 GB: it doesn't "
			"fit, 112 GB short. Make room there, or copy somewhere else.".format(share))]
		assert copies() == ["S:"] and p.Status()["fields"]["copy_box"] == share
		order = [c[2] if len(c) > 2 else None for c in p.checks]
		assert order.index("copy-disk") == order.index("disk") + 1 # under the recording's own disk line, as before
		assert p.Start({"length": 28800, "copy_to": share}) is None # only ever a warning
		p.wanted = None
		# Emptied: no copies, nothing checked; an rsync address has no drive here, as before
		for to in ("", "  ", "me@server:/data"):
			assert box(to) == (200, {"ok": True})
			assert room() == [] and copies() == []
		# Back to the usual folder
		assert box(str(tmp_path / "C")) == (200, {"ok": True}) and [level for level, _ in room()] == ["ok"]
		# Rechecked later (another length picked, the end of a recording): still the box's folder
		box(share)
		p.checks = p.Checks()
		assert [level for level, _ in room()] == ["warn"]
		# Without copy_to, /check runs every check again, as before
		assert raw(p, "POST", "/check?t=" + p.token, {"Content-Type": "application/json"}, "{}") == (200, {"ok": True})
		assert p.copy_box == share
	finally:
		p.server.shutdown()


def test_a_typed_share_is_measured_at_most_every_10_s(tmp_path, home, monkeypatch):
	# The page tells the server of the box's folder; a slow network share is measured no more often for it
	fake_drives(monkeypatch, tmp_path, {"C:": (1022.4, 460.2), "D:": (4000.8, 3998.1), "S:": (8000.0, 50.0)})
	monkeypatch.setattr(panel.Panel, "CopyTo", lambda self: str(tmp_path / "C"))
	p = disk_panel(tmp_path, monkeypatch, 3998, video_folder=tmp_path / "D" / "data")
	asked = []
	usage = panel.DiskUsage
	monkeypatch.setattr(panel, "DiskUsage", lambda where: asked.append(where) or usage(where))
	now = [1000.0]
	monkeypatch.setattr(panel.time, "monotonic", lambda: now[0])
	p.SetCopyBox(str(tmp_path / "S"))
	for _ in range(5):
		p.Drives(str(tmp_path / "D"))
		p.checks = p.Checks()
	assert asked.count("S:\\") == 1
	now[0] += panel.DRIVE_FRESH_S + 1
	p.Drives(str(tmp_path / "D"))
	assert asked.count("S:\\") == 2


def test_the_page_tells_the_server_the_copy_box_folder():
	# Each time the box holds another folder, and not while it is typed in (half a share's name isn't looked for)
	from campy import panelpage
	page = panelpage.PAGE
	assert 'document.activeElement != $("copyto") && box !== s.fields.copy_box && box !== copyAsked' in page
	assert 'post("/check", {copy_to: box})' in page


# ------------------------------------------------------------------ the LEDs' power, asked for at Start

LEDS = ["Blue LED (465 nm)", "Yellow LED (560 nm)"]


def led_panel(tmp_path, leds=LEDS, **acquire):
	from test_session import write_config
	config = write_config(tmp_path / "c.yaml", dict({"numCams": 1, "frameRate": 60, "videoFolder": str(tmp_path / "data"),
		"recTimeInSec": 60}, **acquire), panel={"led_power": leds})
	return panel.Panel(str(config), browser=False)


def test_the_led_powers_are_checked():
	got, why = panel.LedPower(LEDS, [{"led": LEDS[1], "driver_mA": "90", "fiber_tip_uW": 21},
		{"led": LEDS[0], "driver_mA": 17, "fiber_tip_uW": "215.5"}])
	assert why is None and got["leds"] == [{"led": LEDS[0], "driver_mA": 17, "fiber_tip_uW": 215.5},
		{"led": LEDS[1], "driver_mA": 90, "fiber_tip_uW": 21}] # in the config's order
	assert "before the animal was connected" in got["measured"] and got["at"][:4].isdigit()
	assert panel.LedPower(LEDS, [{"led": LEDS[0], "driver_mA": 0, "fiber_tip_uW": 0},
		{"led": LEDS[1], "driver_mA": 90, "fiber_tip_uW": 21}])[1] is None # an LED that is off: 0
	for given, why in (
			(None, "Blue LED (465 nm) has none"),
			([{"led": LEDS[0], "driver_mA": 17, "fiber_tip_uW": 215}], "Yellow LED (560 nm) has none"),
			([{"led": LEDS[0], "driver_mA": "", "fiber_tip_uW": 215}], "driver current (mA) is not a number"),
			([{"led": LEDS[0], "driver_mA": 17, "fiber_tip_uW": -1}], "power at the fiber tip (µW) must be 0 or more"),
			([{"led": LEDS[0], "driver_mA": "nan", "fiber_tip_uW": 1}], "must be 0 or more")):
		assert why in panel.LedPower(LEDS, given)[1]


def test_a_start_asks_for_the_led_powers_and_keeps_them_with_the_recording(tmp_path, home):
	p = led_panel(tmp_path)
	status = p.Status()["led_power"]
	assert status == {"leds": LEDS, "last": None}
	assert p.Start({}).startswith("LED power: the page asks for each LED's driver current") and p.wanted is None
	leds = [{"led": LEDS[0], "driver_mA": 17, "fiber_tip_uW": 215}, {"led": LEDS[1], "driver_mA": 90, "fiber_tip_uW": 21}]
	assert p.Start({"notes": "cage 2", "led_power": leds}) is None
	power = p.wanted["led_power"]
	assert power["leds"] == leds
	# Into session.json's fields (sessionFields.ledPower), as campy-acquire's option
	args = p.Args(p.wanted)
	i = args.index("--sessionFields")
	assert json.loads(args[i + 1]) == {"ledPower": power}
	# Kept for the next Start (the page fills them in) and the next panel with this config
	p.wanted = None
	assert p.Status()["led_power"]["last"] == power
	assert led_panel(tmp_path).Status()["led_power"]["last"] == power
	# The config's own fields are kept beside them
	q = led_panel(tmp_path, sessionFields={"cage": "B3"})
	assert q.Start({"led_power": leds}) is None
	args = q.Args(q.wanted)
	assert json.loads(args[args.index("--sessionFields") + 1]) == {"cage": "B3", "ledPower": q.wanted["led_power"]}


def test_without_led_power_nothing_is_asked(tmp_path, home):
	p = led_panel(tmp_path, leds=[])
	assert p.Status()["led_power"] is None and p.Start({}) is None and "--sessionFields" not in p.Args(p.wanted)


def test_the_page_asks_for_the_led_powers_at_start():
	from campy import panelpage
	page = panelpage.PAGE
	# Each function once: a second "function when" would replace the first for every caller ("Ended at ...")
	names = re.findall(r"^function (\w+)\(", page, re.M)
	assert len(names) == len(set(names)), sorted(n for n in names if names.count(n) > 1)
	# The bout drawing's LED rows show the last confirmed power
	assert "pw = led ? ledPowerOf(led) : null" in page and "Under each LED: its driver current and power" in page
	assert 'id="leddlg"' in page and "LED power, before connecting the animal" in page and "Doric LED driver" in page
	assert "if((last||{}).led_power) return askLeds(start);" in page and 'id="ledlast"' in page
	# Each LED's colour from the wavelength in its name, beside it in the dialog
	assert "function nmColour(name)" in page and "'<b>'+swatch(n)+esc(n)+'</b>" in page
	# ... and in the bout drawing's LED rows, with the wavelength by the pin
	assert "full && nmColour(full) ? swatch(full)" in page and "ledNameOf(led)" in page


def test_a_length_can_have_its_own_copy_folder(tmp_path, home, monkeypatch):
	# pico.lengths' copyTo: the page's "Copy each recording to" box filled in when that length is picked; the copy check
	# and the drive rings follow it
	from campy import pico, spikeglx
	from test_session import write_config
	share = tmp_path / "share"
	share.mkdir()
	config = write_config(tmp_path / "c.yaml", {"numCams": 4, "frameRate": 60, "videoFolder": str(tmp_path / "data"),
		"recTimeInSec": 3600}, pico={"port": "E665485457699D30", "lengths": [
			{"name": "6 days", "seconds": 518400, "copyTo": str(share)},
			{"name": "1 hour", "seconds": 3600}]}, spikeglx={"sdk": FAKE_SDK})
	monkeypatch.setattr(pico.Pico, "Check", lambda self: "COM3: idle")
	monkeypatch.setattr(spikeglx.SpikeGLX, "Check", lambda self: "SpikeGLX: idle")
	monkeypatch.setattr(panel.Panel, "Free", lambda self, folder=None: 4000e9)
	p = panel.Panel(str(config), browser=False)
	lengths = p.Status()["lengths"]
	assert [(l["name"], l["copyTo"]) for l in lengths] == [("6 days", str(share)), ("1 hour", None)]
	assert p.length == 518400 and p.CopyDefault() == str(share)
	assert p.Status()["fields"]["copy_to"] == "" # the config's own (none): what a length without one gives back
	p.length = 3600
	assert p.CopyDefault() == ""
	# A copyTo of the wrong kind of entry is refused, naming the keys
	bad = write_config(tmp_path / "bad.yaml", {"numCams": 1, "videoFolder": str(tmp_path / "data")},
		pico={"port": "X", "lengths": [{"name": "x", "seconds": 60, "copy": "y"}]})
	with pytest.raises(Exception, match="copyTo: <optional>"):
		panel.Panel(str(bad), browser=False)


def test_the_page_fills_the_copy_box_from_the_length():
	from campy import panelpage
	page = panelpage.PAGE
	assert "if(L0.copyTo) $(\"copyto\").value = L0.copyTo; else if(copyLen) $(\"copyto\").value = s.fields.copy_to||\"\";" in page


def test_the_pages_text_is_in_rem_and_can_be_made_larger():
	# Sizes in rem, so the browser's own font size and the page's Text size buttons scale every text; none in px, and
	# nothing below 15 px at the browser's default (0.9375rem)
	from campy import panelpage
	page = panelpage.PAGE
	assert not re.search(r"font-size:\s*\d+(\.\d+)?px", page) and not re.search(r"font:\s*\d+px", page)
	sizes = [float(v) for v in re.findall(r"font-size:\s*([\d.]+)rem", page)]
	assert sizes and min(sizes) >= 0.9375
	assert 'id="larger"' in page and 'id="smaller"' in page and "campy-panel-text-size" in page
	assert "font:1.0625rem" in panelpage.OLD_KEY # the page an old tab gets


def test_the_page_draws_a_bout_and_follows_its_settings():
	# Under Start recording: a row for each of the Pico's outputs, drawn from the bout box's diagram; the settings sent
	# after a pause in typing too, so the drawing follows them
	from campy import panelpage
	page = panelpage.PAGE
	assert 'id="boutpicbox"' in page and "boutPicture(B.error ? null : B.diagram)" in page
	assert "el.oninput" in page and "setTimeout(() => setBout(f.key, v), 700)" in page


def test_the_page_draws_each_drive_as_a_pie():
	from campy import panelpage
	page = panelpage.PAGE
	assert 'id="diskpie"' in page and 'id="drives"' in page and "renderDrives(D_)" in page
	assert "<svg" not in page.split("<script>")[0] # drawn from the status, not fixed in the page
	assert "stroke-dasharray" in page and "the rest of the plan (an estimate)" in page


# ------------------------------------------------------------------ one panel for a folder of configs

def configs_folder(tmp_path):
	# A rig's folder of configs: two experiments with a title, one without, one campy-session can't use, a note
	from test_session import write_config
	folder = tmp_path / "configs"
	folder.mkdir()
	data = str(tmp_path / "data")
	write_config(folder / "look.yaml", {"numCams": 2, "videoFolder": data, "recTimeInSec": 300},
		panel={"title": "Look at the cameras", "rig_lock": "auto", "owner": "panel-look"})
	write_config(folder / "live.yaml", {"numCams": 3, "videoFolder": data, "recTimeInSec": 3600},
		panel={"title": "  Live tracking,\n 3 mice "})
	write_config(folder / "old_v02.yml", {"numCams": 4, "videoFolder": data, "recTimeInSec": 60})
	(folder / "broken.yaml").write_text("numCams: 2\npanel: {nonsense: 1}\n")
	(folder / "notes.txt").write_text("not a config")
	return folder


def test_a_folders_configs_titles_and_favourites(tmp_path, home):
	folder = configs_folder(tmp_path)
	t = panel.Types(str(folder))
	assert [c["file"] for c in t.configs] == ["broken.yaml", "live.yaml", "look.yaml", "old_v02.yml"]
	broken, live, look, old = t.configs
	assert broken["error"] and "nonsense" in broken["error"]
	assert (live["title"], live["titled"]) == ("Live tracking, 3 mice", True) # its spaces and line break: one line
	assert (old["title"], old["titled"], old["error"]) == ("old_v02", False, None) # untitled: its file's name
	# Before any star, the configs with a title; the panel opens on the first that can be used
	assert [c["file"] for c in t.Favorites()] == ["live.yaml", "look.yaml"]
	assert t.First() == live["path"]
	assert t.RigLock() == ("auto", "panel-look")
	# Stars, kept for the next panel (a new Types reads them back), in the order they were given
	assert t.Star(old["path"], True) is None and t.Star(live["path"], False) is None
	t2 = panel.Types(str(folder))
	assert [c["file"] for c in t2.Favorites()] == ["look.yaml", "old_v02.yml"]
	assert t2.First() == look["path"]
	t2.Save(last=old["path"])
	assert panel.Types(str(folder)).First() == old["path"]
	# A favourite, or the last pick, that has gone from the folder is passed over
	os.rename(folder / "old_v02.yml", tmp_path / "old_v02.yml")
	t3 = panel.Types(str(folder))
	assert [c["file"] for c in t3.Favorites()] == ["look.yaml"] and t3.First() == look["path"]
	assert t3.Star(str(tmp_path / "old_v02.yml"), True).startswith("No config")
	status = t3.Status(look["path"])
	assert status["chosen"] == look["path"] and [x["favorite"] for x in status["all"]] == [False, False, True]


def test_a_folder_with_nothing_usable(tmp_path, home):
	empty = tmp_path / "empty"
	empty.mkdir()
	with pytest.raises(panel.session.SessionError, match="No config in"):
		panel.Types(str(empty)).First()
	(empty / "bad.yaml").write_text("panel: {nonsense: 1}\n")
	with pytest.raises(panel.session.SessionError, match="can be used: bad.yaml"):
		panel.Types(str(empty)).First()


def test_one_panel_picks_among_a_folders_experiments(tmp_path, home):
	folder = configs_folder(tmp_path)
	p = panel.Panel(str(folder), browser=False)
	assert p.opened == str(folder) and p.config.path == str(folder / "live.yaml")
	assert len(p.health.names) == 3
	status = p.Status()
	assert status["types"]["chosen"] == str(folder / "live.yaml")
	assert [x["title"] for x in status["types"]["favorites"]] == ["Live tracking, 3 mice", "Look at the cameras"]
	# Another experiment: its cameras, its checks; remembered as the folder's last
	assert p.Choose(str(folder / "look.yaml")) is None
	assert p.config.path == str(folder / "look.yaml") and len(p.health.names) == 2
	assert any("2 cameras" in c[1] for c in p.checks)
	assert panel.Types(str(folder)).First() == str(folder / "look.yaml")
	# One that isn't in the folder, or can't be used: refused, and the panel stays as it was
	assert p.Choose(str(tmp_path / "elsewhere.yaml")).startswith("No config")
	assert "can't be used" in p.Choose(str(folder / "broken.yaml"))
	assert p.config.path == str(folder / "look.yaml")
	# Not while a recording starts or runs
	assert p.Start({}) is None
	assert "A recording is running" in p.Choose(str(folder / "old_v02.yml"))
	p.wanted = None
	assert p.Choose(str(folder / "old_v02.yml")) is None and len(p.health.names) == 4
	# A panel opened with one config has no list
	one = panel.Panel(str(folder / "look.yaml"), browser=False)
	assert one.Status()["types"] is None and one.Choose(str(folder / "live.yaml")).startswith("This panel was opened")


def test_a_switch_while_the_panel_is_open_follows_the_new_cameras(tmp_path, home):
	# The health thread of the old config stops; the new config's starts
	folder = configs_folder(tmp_path)
	p = panel.Panel(str(folder), browser=False)
	p.health.Start()
	old = p.health
	try:
		assert p.Choose(str(folder / "look.yaml")) is None
		assert old.done.is_set() and p.health is not old and p.health.thread.is_alive()
	finally:
		p.health.Stop()


def test_the_page_picks_and_stars_over_http(tmp_path, home):
	folder = configs_folder(tmp_path)
	p = panel.Panel(str(folder), browser=False)
	p.Serve()
	try:
		url = p.url
		assert call(url, "/type", {"path": str(folder / "look.yaml")}) == (200, {"ok": True})
		assert call(url, "/favorite", {"path": str(folder / "old_v02.yml"), "on": True}) == (200, {"ok": True})
		(folder / "new.yaml").write_text("numCams: 1\nvideoFolder: x\npanel: {title: New}\n")
		assert call(url, "/types-scan", {}) == (200, {"ok": True})
		T = call(url, "/status.json")[1]["types"]
		assert T["chosen"] == str(folder / "look.yaml")
		assert [x["file"] for x in T["favorites"]] == ["live.yaml", "look.yaml", "old_v02.yml"] # stars saved: new's
		# title doesn't star it
		assert "new.yaml" in [x["file"] for x in T["all"]]
		code, said = call(url, "/type", {"path": str(folder / "broken.yaml")})
		assert code == 409 and "can't be used" in said["error"]
	finally:
		p.server.shutdown()
		p.server.server_close()


def test_a_folder_panel_takes_the_rig_lock_when_a_config_asks(tmp_path, home, monkeypatch):
	folder = configs_folder(tmp_path)
	ran = []
	monkeypatch.setattr(panel, "UnderRig", lambda *a: ran.append(a[:3]) or 0)
	monkeypatch.setattr(panel, "Running", lambda: None)
	assert panel.Main([str(folder), "--no-browser"]) == 0
	assert ran == [("auto", "panel-look", "configs")]


@POSIX
def test_a_folder_panel_records_with_the_experiment_picked_on_its_page(tmp_path, home):
	# Opened on one experiment (3 cameras that never take a frame), switched on the page to the emulated cameras'
	# config: its cameras' health and its recording, with no stale reader of the first
	config, clips = emu_session(tmp_path, home, 60, live={"model": "none"}, feed="campy-test-folder")
	folder = tmp_path / "configs"
	folder.mkdir()
	os.rename(config, folder / "emu.yaml")
	from test_session import write_config
	write_config(folder / "first.yaml", {"numCams": 3, "videoFolder": str(tmp_path / "x"), "recTimeInSec": 60},
		panel={"title": "First"})
	proc, lines, url = start_panel(tmp_path, home, folder)
	try:
		status = call(url, "/status.json")[1]
		assert status["types"]["chosen"] == str(folder / "first.yaml") # the only title: the favourite
		assert call(url, "/type", {"path": str(folder / "emu.yaml")}) == (200, {"ok": True})
		assert call(url, "/start", {"experiment": "Folder test"}) == (200, {"ok": True})
		assert call(url, "/type", {"path": str(folder / "first.yaml")})[0] == 409 # not while it records
		status = wait(url, lambda s: s["state"] == "recording" and s["health"].get("feed") and
			all(c["frame"] > 10 for c in s["health"]["cameras"]))
		assert [c["name"] for c in status["health"]["cameras"]] == NAMES
		assert call(url, "/stop", {}) == (200, {"ok": True})
		# A clean stop, on a CI runner busy with 3 other workers, can take more than a minute (the ARM runner's did)
		try:
			status = wait(url, lambda s: s["state"] == "ready" and s["launcher"] and s["launcher"]["state"] == "done",
				timeout=180)
		except AssertionError as e:
			raise AssertionError("{}\nThe panel's output:\n{}".format(e, "".join(lines)[-6000:]))
		assert status["launcher"]["parts"]["acquire"]["exit_code"] == 0
		assert min(recorded(the_folder(clips))) > 10
		# Back to the first, once it has ended; the next panel opens on it
		assert call(url, "/type", {"path": str(folder / "first.yaml")}) == (200, {"ok": True})
		assert panel.Types(str(folder)).First() == str(folder / "first.yaml")
	finally:
		if proc.poll() is None:
			os.killpg(proc.pid, signal.SIGINT)
		try:
			proc.wait(timeout=30)
		except subprocess.TimeoutExpired:
			os.killpg(proc.pid, signal.SIGKILL)
	assert proc.returncode == 0, "".join(lines)


def test_a_title_is_only_the_file_that_gives_it(tmp_path, home):
	# A trial built on the daily config with extends: is not a second "Daily" on the list
	from test_session import write_config
	folder = tmp_path / "configs"
	folder.mkdir()
	write_config(folder / "daily.yaml", {"numCams": 2, "videoFolder": str(tmp_path / "d"), "recTimeInSec": 60},
		panel={"title": "Daily"})
	(folder / "trial.yaml").write_text("extends: daily.yaml\nrecTimeInSec: 120\n")
	t = panel.Types(str(folder))
	daily, trial = t.Find(str(folder / "daily.yaml")), t.Find(str(folder / "trial.yaml"))
	assert (daily["title"], daily["titled"]) == ("Daily", True)
	assert (trial["title"], trial["titled"], trial["error"]) == ("trial", False, None)
	assert [c["file"] for c in t.Favorites()] == ["daily.yaml"]
	# Its own title makes it one
	(folder / "trial.yaml").write_text("extends: daily.yaml\nrecTimeInSec: 120\npanel: {title: Trial}\n")
	t.Scan()
	assert [c["title"] for c in t.Favorites()] == ["Daily", "Trial"]


# ------------------------------------------------------------------ the trigger board, before Start

def test_trigger_check(monkeypatch):
	# The Arduino campy starts must be plugged in: without it campy-acquire stopped 2 s into every recording
	arduino = {"startArduino": True, "triggerController": "arduino", "serialPort": "COM9", "cameraTrigger": "Line3"}
	monkeypatch.setattr(panel.sys, "platform", "win32")
	level, text = panel.TriggerCheck(arduino, None, ports=lambda: ["COM1", "COM3", "COM12"])
	assert level == "block" and "the Arduino campy starts (startArduino) is on COM9 (serialPort), and there is no COM9 " \
		"on this computer (its serial ports: COM1, COM3, COM12)" in text
	assert panel.TriggerCheck(arduino, None, ports=lambda: ["com9"]) == ("ok", "Trigger: the Arduino on COM9")
	assert panel.TriggerCheck(arduino, None, ports=lambda: None) is None # ports can't be listed: say nothing
	assert panel.TriggerCheck(dict(arduino, triggerController="None"), None, ports=lambda: []) is None
	# A Pico rig: the config must start the Pico (its pico: section), unless no camera is triggered
	pico = {"startArduino": False, "triggerController": "pico", "cameraTrigger": ["Line3", "Line3"]}
	level, text = panel.TriggerCheck(pico, None, ports=lambda: [])
	assert level == "block" and "no pico: section to start it" in text
	assert panel.TriggerCheck(pico, object(), ports=lambda: []) == ("ok", "Trigger: the Pico")
	assert panel.TriggerCheck(dict(pico, cameraTrigger="None"), None, ports=lambda: []) is None
	# An external trigger on a rig that names no board (startArduino False): as before, no line
	assert panel.TriggerCheck({"startArduino": False, "cameraTrigger": "Line3"}, None, ports=lambda: []) is None
