"""
A recording cut short (campy killed, a power cut): its cameras never closed
their recordings, so metadata.csv has no totals, the fragmented MP4 ends at
its last fragment written, and frametimes.npy can have rows for frames that
never reached the video (added as each frame is queued for the writer). The
tools keep only the rows of frames the video holds (segments.HeldRows),
read from its packets: campy-live-export's (and campy-label-pack's) Camera,
and campy-session post's join, which also joins a cut last part.
"""

import csv
import os
import subprocess

import imageio_ffmpeg
import numpy as np
import pytest

from campy import liveexport, post, segments, writer
from test_ffmpeg_restart import Killer, Levels, Near
from test_failures import CameraProcess, FakeCamera, params
from test_videoread import N, Frame, Index

CUTS = [i / 20 for i in range(6, 20)] # where the file is cut: 30% to 95% of its bytes


def write_metadata(folder, closed=False, rate=30):
	with open(os.path.join(folder, "metadata.csv"), "w", newline="") as f:
		w = csv.writer(f)
		w.writerow(["videoFilename", "0.mp4"])
		if rate:
			w.writerow(["frameRate", rate])
		if closed:
			w.writerow(["framesWritten", N])


def cut(path, keep):
	with open(path, "rb") as f:
		data = f.read()
	with open(path, "wb") as f:
		f.write(data[:int(len(data) * keep)])


def decoded(path):
	# Every frame a decoder gives (none doubled or dropped for a constant rate), as their places; a cut file ends in
	# a packet it can't decode (said, not raised)
	out = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "quiet", "-i", path, "-fps_mode", "passthrough", "-f",
		"rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
	h, w = Frame(0).shape
	return [Index(f) for f in np.frombuffer(out, np.uint8).reshape(-1, h, w)]


@pytest.fixture(scope="module")
def fragmented(tmp_path_factory):
	# N frames, each carrying its place (test_videoread.Frame), B-frames, campy's fragmented MP4 (writer.py's
	# movflags), a fragment about every 9 frames
	path = str(tmp_path_factory.mktemp("cut") / "whole.mp4")
	frames = np.stack([Frame(i) for i in range(N)])
	h, w = frames.shape[1:]
	subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s",
		"{}x{}".format(w, h), "-r", "30", "-i", "-", "-c:v", "libx264", "-bf", "3", "-g", "10", "-crf", "12", "-pix_fmt",
		"yuv420p", "-movflags", "+frag_keyframe+empty_moov+delay_moov+default_base_moof", "-frag_duration", "300000",
		path], input=frames.tobytes(), check=True)
	return path


def readable(path, rate=30):
	# Whether ffmpeg opens a cut file at all: cut in some places, it does not ("error reading header")
	try:
		segments.CountFrames(path, rate, 10 ** 5)
		return True
	except segments.Uncounted:
		return False


def one_camera(tmp_path, video, keep, rows=N, closed=False, rate=30):
	folder = tmp_path / "rec" / "Camera0"
	folder.mkdir(parents=True)
	with open(video, "rb") as f:
		(folder / "0.mp4").write_bytes(f.read())
	if keep < 1:
		cut(str(folder / "0.mp4"), keep)
	frames = np.arange(1, rows + 1) # campy's frame numbers from 1: frame n is the video's place n - 1
	np.save(str(folder / "frametimes.npy"), np.vstack([frames, frames / 30.0]))
	np.save(str(folder / "hosttimes.npy"), frames * 1000)
	write_metadata(str(folder), closed=closed, rate=rate)
	return folder


def test_a_cut_video_keeps_only_the_rows_it_holds(tmp_path, fragmented, capsys):
	checked, holes = 0, 0
	for k, keep in enumerate(CUTS):
		rec = tmp_path / str(k)
		folder = one_camera(rec, fragmented, keep)
		if not readable(str(folder / "0.mp4")):
			continue
		held = decoded(str(folder / "0.mp4")) # the places of the frames it holds, as a reader gives them
		assert 0 < len(held) < N
		holes += held != list(range(len(held))) # a B-frame missing before its last frame
		camera = liveexport.Camera(str(rec / "rec"), "Camera0")
		# Each video frame is the row campy numbered it by
		assert [int(f) - 1 for f in camera.frames] == held, keep
		assert list(camera.host) == [int(f) * 1000 for f in camera.frames]
		assert list(camera.Index(camera.frames)) == list(range(len(held)))
		assert list(camera.Index([N])) == [-1] # never in the video
		assert "Camera0 did not close its recording; {} of frametimes.npy's {} rows are frames its video does not " \
			"hold".format(N - len(held), N) in capsys.readouterr().out
		checked += 1
	assert checked >= 6 and holes >= 1, (checked, holes)


def test_a_closed_camera_is_not_counted(tmp_path, fragmented, monkeypatch):
	folder = one_camera(tmp_path, fragmented, 1, closed=True)
	monkeypatch.setattr(segments, "CountFrames", lambda *a, **k: pytest.fail("counted a closed camera's video"))
	assert len(liveexport.Camera(str(tmp_path / "rec"), "Camera0").frames) == N
	assert segments.Closed(str(folder))


def test_frames_past_the_last_row_are_said_and_no_frame_rate_is_needed(tmp_path, fragmented, capsys):
	# The video got further than frametimes.npy (its last batch never written); metadata.csv without frameRate
	folder = one_camera(tmp_path, fragmented, 1, rows=N - 12, rate=None)
	held = segments.HeldRows(str(folder), np.arange(1, N - 11), video="0.mp4")
	assert held.keep.all() and held.beyond == 12 and held.files == [("0.mp4", N)]
	camera = liveexport.Camera(str(tmp_path / "rec"), "Camera0")
	assert len(camera.frames) == N - 12
	assert "its video holds 12 frames past the last row" in capsys.readouterr().out


def test_a_video_that_cannot_be_read_keeps_every_row_and_says_so(tmp_path, capsys, monkeypatch):
	def unreadable(*args, **kwargs):
		raise segments.Uncounted("a stand-in ffmpeg")
	monkeypatch.setattr(segments, "CountFrames", unreadable)
	folder = tmp_path / "rec" / "Camera0"
	folder.mkdir(parents=True)
	(folder / "0.mp4").write_bytes(b"not a video")
	np.save(str(folder / "frametimes.npy"), np.vstack([np.arange(1, 11), np.arange(10) / 30.0]))
	write_metadata(str(folder))
	assert len(liveexport.Camera(str(tmp_path / "rec"), "Camera0").frames) == 10
	assert "its video's frames could not be counted" in capsys.readouterr().out


@pytest.mark.parametrize("data", [b"", b"a plain MP4 cut before ffmpeg wrote its index"])
def test_an_empty_video_holds_nothing(tmp_path, data):
	folder = tmp_path / "Camera0"
	folder.mkdir()
	(folder / "0.mp4").write_bytes(data)
	held = segments.HeldRows(str(folder), np.arange(1, 11), video="0.mp4")
	assert not held.keep.any() and held.files == [("0.mp4", 0)] and held.left_out == 10


def test_the_video_beside_campys_copies_is_found(tmp_path, fragmented):
	folder = one_camera(tmp_path, fragmented, 1, closed=True)
	os.remove(str(folder / "metadata.csv")) # no videoFilename: the one video in the folder
	for name in ("0.fragmented.mp4", "0.remux-tmp.mp4", ".0.join-tmp.mp4"):
		(folder / name).write_bytes(b"x")
	assert liveexport.Camera(str(tmp_path / "rec"), "Camera0").video == "0.mp4"


# ------------------------------------------------------------------ with an ffmpeg restart

def crash(folder):
	"""
	What a camera with a restarted ffmpeg leaves when the recording is cut
	short: metadata.csv without its totals, frametimes.npy with a row for
	every frame grabbed (the lost frames' too), segments.csv's last row
	still open, and its last file cut at its last fragment written.
	"""
	with open(os.path.join(folder, "metadata.csv"), newline="") as f:
		rows = [r for r in csv.reader(f) if r and r[0] not in ("framesWritten", "totalFrames", "totalTime")]
	with open(os.path.join(folder, "metadata.csv"), "w", newline="") as f:
		csv.writer(f).writerows(rows)
	everything = np.arange(1, 601)
	np.save(os.path.join(folder, "frametimes.npy"), np.vstack([everything, everything / 100.0]))
	np.save(os.path.join(folder, "hosttimes.npy"), everything * 1000)
	parts = segments.Read(folder)
	parts[-1].update(last_frame=parts[-1]["first_frame"] + 10, frames=None, lost=None, lost_frames="",
		ended="")
	segments.Write(folder, parts)
	last = os.path.join(folder, parts[-1]["file"])
	with open(last, "rb") as f:
		whole = f.read()
	for keep in (0.7, 0.75, 0.65, 0.8, 0.6, 0.85): # a cut ffmpeg can open
		with open(last, "wb") as f:
			f.write(whole[:int(len(whole) * keep)])
		if readable(last, 100):
			return parts
	pytest.fail("ffmpeg opens none of the cut files")


def test_a_recording_with_a_restart_cut_short_is_joined_and_exported(tmp_path, monkeypatch):
	ffmpeg_process, on_frame, found = Killer({250})
	monkeypatch.setattr(writer, "FfmpegProcess", ffmpeg_process)
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=6, restartFfmpeg=True, keyframeInterval=0.5),
		FakeCamera(on_frame=on_frame))
	run.Run(timeout=40)
	folder = run.Folder()
	lost = segments.LostFrames(folder)
	crash(folder)
	assert not segments.Closed(folder)

	# Before the join: the tools say how, --force included (post skips an unclean stop without it)
	with pytest.raises(liveexport.ExportError, match=r"--only join --force"):
		liveexport.Camera(str(tmp_path), "Camera0")
	assert post.CleanStop(str(tmp_path))[0] is False

	said = post.Join(folder, post.Ffmpeg())
	assert said.startswith("Camera0: 2 files joined into 0.joined.mp4")
	assert "It did not close its recording; " in said and "rows are frames its video does not hold" in said
	camera = liveexport.Camera(str(tmp_path), "Camera0")
	assert camera.video == "0.joined.mp4"
	levels = Levels(os.path.join(folder, "0.joined.mp4"))
	# Every video frame is the row of the frame it holds: the lost frames and the cut tail left out
	assert len(levels) == len(camera.frames) < 600 - len(lost)
	assert [n for n, lv in zip(camera.frames, levels) if not Near(lv, int(n))] == []
	assert not set(int(n) for n in camera.frames) & lost
	assert list(camera.Index(camera.frames)) == list(range(len(levels)))
	assert list(camera.host) == [int(n) * 1000 for n in camera.frames]
