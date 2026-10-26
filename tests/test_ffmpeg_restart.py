"""
restartFfmpeg: a camera's ffmpeg that stops mid-recording is restarted, the
camera never stopping, and its video goes on in a new file (0.part2.mp4).
Every frame is accounted for: each file's frames are the ones segments.csv
says, frame for frame (each fake frame's gray level is its number), and
frametimes.npy has one row per video frame, the files read one after the
other. And when it must not restart: three times an hour already, the disk,
a calibration video.
"""

import csv
import os

import imageio_ffmpeg
import numpy as np
import pytest

from campy import segments, writer
from test_failures import CameraProcess, FakeCamera, fake_write_frames, params


def Killer(at):
	# A FakeCamera on_frame that kills the ffmpeg running at each of these frames
	found = []
	real = writer.FfmpegProcess

	def ffmpeg_process(w):
		found.append(real(w))
		return found[-1]

	def on_frame(n):
		if n in at:
			found[-1].kill()
	return ffmpeg_process, on_frame, found


def Levels(path):
	# Each frame's gray level, decoded
	reader = imageio_ffmpeg.read_frames(path)
	meta = reader.__next__()
	w, h = meta["size"]
	return [int(round(np.frombuffer(f, np.uint8).reshape(h, w, -1).mean())) for f in reader]


def Near(level, number):
	# A decoded frame's gray level is its frame number's (mod 256), to within the encoder's rounding
	d = abs(level - number % 256)
	return min(d, 256 - d) <= 3


def ReadSegments(folder):
	with open(os.path.join(folder, "segments.csv"), newline="") as f:
		return list(csv.DictReader(f))


@pytest.mark.parametrize("keyframes", ["None", 0.5]) # None: libx264's B-frames, fragments cut between keyframes
def test_a_killed_ffmpeg_is_restarted_and_every_frame_accounted_for(tmp_path, monkeypatch, capsys, keyframes):
	ffmpeg_process, on_frame, found = Killer({250})
	monkeypatch.setattr(writer, "FfmpegProcess", ffmpeg_process)
	camera = FakeCamera(on_frame=on_frame)
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=6, restartFfmpeg=True, keyframeInterval=keyframes),
		camera)
	run.Run(timeout=40)
	out = capsys.readouterr().out
	folder = run.Folder()
	assert len(found) == 2 and found[1].returncode == 0 # the second ffmpeg ended as the recording did

	# The camera never stopped, and nothing failed
	assert run.recording.Failures() == [] and run.recording.restarts == 1
	meta = run.Metadata()
	assert meta["totalFrames"] == "600" and "videoError" not in meta
	assert meta["ffmpegRestarts"] == "1" and meta["videoFiles"] == "['0.mp4', '0.part2.mp4']"
	assert "Camera0: ffmpeg restarted (restart 1 of 3 allowed in an hour). The camera never stopped; its video goes " \
		"on in 0.part2.mp4, from frame " in out

	# segments.csv: which frames each file holds
	rows = ReadSegments(folder)
	assert [r["file"] for r in rows] == ["0.mp4", "0.part2.mp4"]
	first, second = rows
	assert first["first_frame"] == "1" and first["ended"].startswith("ffmpeg stopped after taking ")
	assert int(second["first_frame"]) == int(first["last_frame"]) + 1 and second["last_frame"] == "600"
	assert second["ended"] == "the recording ended" and second["lost"] == "0"
	lost = segments.ParseRanges(first["lost_frames"])
	assert int(first["lost"]) == len(lost) > 0
	assert int(first["frames"]) + len(lost) == int(first["last_frame"]) # every frame given to it: in it, or lost
	assert meta["framesLostInEncoder"] == str(len(lost))

	# Each file holds the frames segments.csv says, in order, frame for frame
	frames = run.FrameTimes()
	numbers = [int(n) for n in frames[0]]
	expected = [n for n in range(1, 601) if n not in set(lost)]
	assert numbers == expected # one row per video frame: the lost ones left out, and only those
	assert len(np.load(os.path.join(folder, "hosttimes.npy"))) == len(expected)
	assert meta["framesWritten"] == str(len(expected))
	levels = Levels(os.path.join(folder, "0.mp4")) + Levels(os.path.join(folder, "0.part2.mp4"))
	assert len(levels) == len(expected)
	assert [n for n, level in zip(expected, levels) if not Near(level, n)] == []
	assert len(levels[:int(first["frames"])]) == int(first["frames"])


def test_at_most_three_restarts_an_hour(tmp_path, monkeypatch, capsys):
	ffmpeg_process, on_frame, found = Killer({100, 200, 300, 400})
	monkeypatch.setattr(writer, "FfmpegProcess", ffmpeg_process)
	camera = FakeCamera(on_frame=on_frame)
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=6, restartFfmpeg=True), camera)
	run.Run(timeout=40)
	out = capsys.readouterr().out
	assert len(found) == 4 and run.recording.restarts == 3
	# The fourth stop ends the camera's recording, as without restartFfmpeg
	(failure,) = run.recording.Failures()
	assert failure.endswith(". It was not restarted: it was restarted 3 times in the last hour, the most allowed")
	assert "Stopping Camera0." in out and camera.closed
	meta = run.Metadata()
	assert int(meta["totalFrames"]) < 600 and meta["videoError"] == failure
	rows = ReadSegments(run.Folder())
	assert [r["file"] for r in rows] == ["0.mp4", "0.part2.mp4", "0.part3.mp4", "0.part4.mp4"]
	assert all(r["ended"].startswith("ffmpeg stopped after taking ") for r in rows)
	# Every file counted; frametimes.npy holds their frames, in order
	held = [int(r["frames"]) for r in rows]
	expected = []
	for r in rows:
		lost = set(segments.ParseRanges(r["lost_frames"]))
		expected += [n for n in range(int(r["first_frame"]), int(r["last_frame"]) + 1) if n not in lost]
	assert [int(n) for n in run.FrameTimes()[0]] == expected and len(expected) == sum(held)
	for r in rows:
		# A file killed within a second holds no fragment yet: nothing in it can be read, and it says 0
		if int(r["frames"]):
			assert len(Levels(os.path.join(run.Folder(), r["file"]))) == int(r["frames"])


def test_not_restarted_when_the_disk_is_nearly_full(tmp_path, monkeypatch, capsys):
	ffmpeg_process, on_frame, found = Killer({150})
	monkeypatch.setattr(writer, "FfmpegProcess", ffmpeg_process)
	monkeypatch.setattr(writer.shutil, "disk_usage", lambda path: type("Usage", (), {"free": 1.5e9})())
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=6, restartFfmpeg=True), FakeCamera(on_frame=on_frame))
	run.Run(timeout=40)
	(failure,) = run.recording.Failures()
	assert failure.endswith(". It was not restarted: the disk is nearly full (1.50 GB free)"), failure
	assert len(found) == 1 and not os.path.exists(os.path.join(run.Folder(), "segments.csv"))


def test_not_restarted_when_ffmpeg_says_the_disk_is_full(tmp_path, monkeypatch, capsys):
	def write_frames(path, size, **kwargs):
		count = 0
		while True:
			frame = yield
			if frame is not None:
				count += 1
				if count > 30:
					raise IOError("[Errno 32] Broken pipe\n\nFFMPEG STDERR OUTPUT:\nav_interleaved_write_frame(): "
						"No space left on device")
	monkeypatch.setattr(writer, "write_frames", write_frames)
	monkeypatch.setattr(writer.segments, "CountFrames", lambda path, rate, given, **k: bytearray([1] * 30 + [0]))
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=6, restartFfmpeg=True), FakeCamera())
	run.Run(timeout=40)
	(failure,) = run.recording.Failures()
	assert failure.endswith(". It was not restarted: the disk is the cause (\"no space left\")"), failure
	assert run.Metadata()["framesWritten"] == "30"


def test_a_calibration_video_is_not_restarted(tmp_path, monkeypatch):
	ffmpeg_process, on_frame, found = Killer({150})
	monkeypatch.setattr(writer, "FfmpegProcess", ffmpeg_process)
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=6, restartFfmpeg=True,
		videoFilename="0-calibration.mp4"), FakeCamera(on_frame=on_frame))
	run.Run(timeout=40)
	(failure,) = run.recording.Failures()
	assert failure.endswith(". It was not restarted: a calibration video is not restarted"), failure
	assert len(found) == 1


def test_a_new_ffmpeg_that_does_not_start_is_tried_again(tmp_path, monkeypatch, capsys):
	# The first restart's ffmpeg does not start; the next one does, into the same file name
	starts = []
	fake = fake_write_frames(fail_at=None)

	def write_frames(path, size, **kwargs):
		starts.append(os.path.basename(path))
		if len(starts) == 2:
			raise FileNotFoundError("no ffmpeg this time")
		if len(starts) == 1:
			return fake_write_frames(fail_at=40)(path, size, **kwargs)
		return fake(path, size, **kwargs)
	monkeypatch.setattr(writer, "write_frames", write_frames)
	monkeypatch.setattr(writer.segments, "CountFrames", lambda path, rate, given, **k: bytearray([1] * (given - 1) + [0]))
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=2, restartFfmpeg=True), FakeCamera())
	run.Run(timeout=40)
	out = capsys.readouterr().out
	assert starts == ["0.mp4", "0.part2.mp4", "0.part2.mp4"]
	assert "Camera0: ffmpeg did not start again: no ffmpeg this time" in out
	assert run.recording.restarts == 1 and run.recording.Failures() == [] # restarts that started
	assert "ffmpeg restarted (restart 2 of 3 allowed in an hour)" in out # but each try counts
	assert [r["file"] for r in ReadSegments(run.Folder())] == ["0.mp4", "0.part2.mp4"]
	assert run.Metadata()["framesWritten"] == "199" # 200 grabbed, the one the first ffmpeg failed on lost


# ------------------------------------------------------------------ segments.py

def test_part_names():
	assert segments.PartName("0.mp4", 1) == "0.mp4"
	assert segments.PartName("0.mp4", 2) == "0.part2.mp4"
	assert segments.PartName("Camera1.mov", 12) == "Camera1.part12.mov"
	assert segments.JoinedName("0.mp4") == "0.joined.mp4"
	assert segments.Part("0.part3.mp4") == 3 and segments.Part("0.mp4") is None


def test_ranges():
	assert segments.Ranges([7, 1, 2, 3, 9, 10]) == "1-3;7;9-10"
	assert segments.Ranges([]) == ""
	assert segments.ParseRanges("1-3;7;9-10") == [1, 2, 3, 7, 9, 10]
	assert segments.ParseRanges("") == []


def test_frame_runs():
	runs = segments.FrameRuns()
	for n in [5, 6, 7, 10, 11, 20]:
		runs.Add(n)
	assert [runs.Number(i) for i in range(6)] == [5, 6, 7, 10, 11, 20]
	assert runs.first == 5 and runs.last == 20 and runs.count == 6 and len(runs.starts) == 3


def test_keep_rows():
	assert list(segments.KeepRows([1, 2, 3, 4], {2, 3, 99})) == [True, False, False, True]
	assert list(segments.KeepRows([1, 2], set())) == [True, True]


def test_video_for(tmp_path):
	assert segments.VideoFor(str(tmp_path), "0.mp4") == "0.mp4" # no segments.csv: the one file
	segments.Write(str(tmp_path), [{"file": "0.mp4"}, {"file": "0.part2.mp4"}])
	with pytest.raises(segments.NeedsJoin, match=r"its video is in 2 files \(0\.mp4, 0\.part2\.mp4\).*--only join"):
		segments.VideoFor(str(tmp_path), "0.mp4")
	(tmp_path / "0.joined.mp4").write_bytes(b"x")
	assert segments.VideoFor(str(tmp_path), "0.mp4") == "0.joined.mp4"
	assert segments.LostFrames(str(tmp_path)) == set()


def test_the_watch_says_the_same_limit():
	from campy import watch
	assert watch.RESTARTS_PER_HOUR == writer.RESTARTS_PER_HOUR


# ------------------------------------------------------------------ after the recording: the tools

@pytest.mark.parametrize("codec", ["h264", "h265"]) # H.265 tagged hvc1, its codec header in the file's header only
def test_the_tools_after_a_restart(tmp_path, monkeypatch, capsys, codec):
	from campy import align, liveexport, post, videoread
	ffmpeg_process, on_frame, found = Killer({250})
	monkeypatch.setattr(writer, "FfmpegProcess", ffmpeg_process)
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=6, restartFfmpeg=True, keyframeInterval=0.5,
		codec=codec), FakeCamera(on_frame=on_frame))
	run.Run(timeout=40)
	folder = run.Folder()
	lost = segments.LostFrames(folder)
	expected = [n for n in range(1, 601) if n not in lost]

	# Before the join: the tools that need one video say how to make it; campy-infer takes each file
	with pytest.raises(liveexport.ExportError, match="campy-session post .* --only join"):
		liveexport.Camera(str(tmp_path), "Camera0")
	assert [os.path.basename(v) for v in videoread.FindVideos([str(tmp_path)])] == ["0.mp4", "0.part2.mp4"]

	# The join: one video, the files' packets, as many as frametimes.npy has rows
	class Log:
		said = []
		def Say(self, text):
			self.said.append(text)
	state, message = post.StepJoin(str(tmp_path), {}, Log(), stop=None)
	assert (state, message) == ("done", "1 camera's video files joined into one, checked packet by packet"), Log.said
	assert Log.said[0].startswith("Camera0: 2 files joined into 0.joined.mp4, {} frames, the same packets".format(
		len(expected)))
	levels = Levels(os.path.join(folder, "0.joined.mp4"))
	assert len(levels) == len(expected) and [n for n, lv in zip(expected, levels) if not Near(lv, n)] == []
	assert not post.Fragmented(os.path.join(folder, "0.joined.mp4"))
	assert post.StepJoin(str(tmp_path), {}, Log(), stop=None)[0] == "done" # again: joined already
	assert post.StepJoin(str(tmp_path), {"join": False}, Log())[0] == "skipped"

	# campy-live-export's camera (so campy-label-pack's and campy-model-fit's): the joined video, frame for frame
	camera = liveexport.Camera(str(tmp_path), "Camera0")
	assert camera.video == "0.joined.mp4"
	assert list(camera.Index(expected)) == list(range(len(expected)))
	assert list(camera.Index(sorted(lost))) == [-1] * len(lost)
	assert [os.path.basename(v) for v in videoread.FindVideos([str(tmp_path)])] == ["0.joined.mp4"]
	assert align.LoadCamera(folder)["warnings"] == []

	# A recording cut short before campy took the lost frames' rows out: the readers leave them out
	frames, hosts = np.load(os.path.join(folder, "frametimes.npy")), np.load(os.path.join(folder, "hosttimes.npy"))
	everything = np.arange(1, 601)
	np.save(os.path.join(folder, "frametimes.npy"), np.vstack([everything, everything / 100.0]))
	np.save(os.path.join(folder, "hosttimes.npy"), everything * 1000)
	camera = liveexport.Camera(str(tmp_path), "Camera0")
	assert list(camera.frames) == expected and list(camera.host) == [n * 1000 for n in expected]
	cam = align.LoadCamera(folder)
	assert list(cam["frame"]) == expected and len(cam["host_ns"]) == len(expected)
	assert cam["warnings"] == ["{} frames lost in a stopped ffmpeg still had rows (the recording stopped without "
		"closing?), and are left out".format(len(lost))]


def test_a_join_that_does_not_match_frametimes_is_not_kept(tmp_path, monkeypatch):
	from campy import post
	ffmpeg_process, on_frame, found = Killer({250})
	monkeypatch.setattr(writer, "FfmpegProcess", ffmpeg_process)
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=4, restartFfmpeg=True),
		FakeCamera(on_frame=on_frame))
	run.Run(timeout=40)
	folder = run.Folder()
	frames = np.load(os.path.join(folder, "frametimes.npy"))
	np.save(os.path.join(folder, "frametimes.npy"), frames[:, :-5]) # 5 rows short
	with pytest.raises(post.PostError, match=r"its video files hold \d+ frames, and frametimes\.npy has \d+ rows"):
		post.Join(folder, post.Ffmpeg())
	assert sorted(f for f in os.listdir(folder) if f.endswith(".mp4")) == ["0.mp4", "0.part2.mp4"]


def test_a_plain_mp4_cut_off_holds_nothing(tmp_path, monkeypatch, capsys):
	# fragmentedVideo False: a killed ffmpeg's MP4 has no index, so none of it can be read. Every frame given to it
	# is lost, the camera goes on into the next file, and the join leaves the empty one out
	from campy import post
	ffmpeg_process, on_frame, found = Killer({150})
	monkeypatch.setattr(writer, "FfmpegProcess", ffmpeg_process)
	run = CameraProcess(monkeypatch, params(tmp_path, recTimeInSec=4, restartFfmpeg=True, fragmentedVideo=False),
		FakeCamera(on_frame=on_frame))
	run.Run(timeout=40)
	first, second = ReadSegments(run.Folder())
	assert first["frames"] == "0" and first["lost_frames"] == "1-{}".format(first["last_frame"])
	assert second["last_frame"] == "400" and second["lost"] == "0"
	assert [int(n) for n in run.FrameTimes()[0]] == list(range(int(second["first_frame"]), 401))
	assert "Camera0: 0.mp4 holds 0 of the {} frames given to its ffmpeg.".format(first["last_frame"]) in \
		capsys.readouterr().out
	assert post.StepJoin(str(tmp_path), {}, type("Log", (), {"Say": lambda self, t: None})())[0] == "done"
	assert len(Levels(os.path.join(run.Folder(), "0.joined.mp4"))) == 400 - int(first["last_frame"])
