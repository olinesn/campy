"""
campy-session panel (campy/panel.py): a recording run from a page in the
browser. Its pieces, and the whole thing over HTTP with emulated cameras and
campy-live --model none.
"""

import csv
import json
import os
import re
import signal
import struct
import subprocess
import sys
import threading
import time
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


@POSIX
def test_a_start_that_a_check_stops(tmp_path, home):
	config, clips = emu_session(tmp_path, home, 5, live={"model": "none", "python": str(tmp_path / "no-python")})
	proc, lines, url = start_panel(tmp_path, home, config)
	try:
		status = call(url, "/status.json")[1]
		assert status["checks"][0]["level"] == "block"
		code, said = call(url, "/start", {})
		assert code == 409 and said["error"].startswith("Can't start: Could not run campy-live's python")
		assert call(url, "/status.json")[1]["state"] == "ready"
	finally:
		os.killpg(proc.pid, signal.SIGINT)
		proc.wait(timeout=30)
	assert not any(d.is_dir() and re.match(r"\d{4}-", d.name) for d in clips.iterdir())
