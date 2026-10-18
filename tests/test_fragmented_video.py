"""
Fragmented MP4 (fragmentedVideo, writer.EncoderArgs): a video whose ffmpeg is
stopped before it closes the file (a power cut, an OS crash, ffmpeg killed)
is readable up to its last fragment, where a plain MP4 is not readable at all.
"""

import os, sys, time, types, struct

import numpy as np
import pytest
import yaml
import imageio_ffmpeg

from campy import configurator, writer, sweep


FRAGMENTED = ["-movflags", "+frag_keyframe+empty_moov+delay_moov+default_base_moof",
	"-frag_duration", "1000000", "-flush_packets", "1"]
ENCODERS = {
	"cpu": {},
	"nvidia": {"gpuID": 0, "gpuMake": "nvidia"},
	"amd": {"gpuID": 0, "gpuMake": "amd"},
	"intel": {"gpuID": 0, "gpuMake": "intel"},
	"vaapi": {"gpuID": 0, "gpuMake": "vaapi"},
	}
W, H = 320, 240


def cam(**overrides):
	params = configurator.DefaultParams()
	params.update(cameraName="Camera0", frameWidth=W, frameHeight=H,
		pixelFormatInput="gray", pixelFormatOutput="yuv420p")
	params.update(overrides)
	return params


# ------------------------------------------------------------------ settings

def test_on_by_default():
	assert configurator.DefaultParams()["fragmentedVideo"] is True


@pytest.mark.parametrize("make", list(ENCODERS))
def test_every_encoder_gets_the_same_settings_in_fragments(make):
	for extra in ({}, {"keyframeInterval": 0.5, "colorRange": "full"}):
		params = cam(**ENCODERS[make], **extra)
		enc = writer.EncoderArgs(params)
		plain = writer.EncoderArgs(dict(params, fragmentedVideo=False))
		assert enc["output_params"] == FRAGMENTED + plain["output_params"]
		assert dict(enc, output_params=None) == dict(plain, output_params=None)


def test_only_mp4_and_mov_are_fragmented():
	for name in ("0.mp4", "0.MP4", "0.mov", "0.m4v"):
		assert writer.EncoderArgs(cam(videoFilename=name))["output_params"][:6] == FRAGMENTED
	# Matroska and the rest have no -movflags: they are left as they were
	for name in ("0.mkv", "0.avi", "0"):
		assert "-movflags" not in writer.EncoderArgs(cam(videoFilename=name))["output_params"]


def test_config_and_command_line(tmp_path, monkeypatch):
	config = tmp_path / "c.yaml"
	config.write_text(yaml.safe_dump({"numCams": 2, "fragmentedVideo": [True, False]}))
	monkeypatch.setattr(sys, "argv", ["campy-acquire", str(config)])
	p = configurator.ConfigureParams()
	assert [configurator.CameraParams(p, n)["fragmentedVideo"] for n in range(2)] == [True, False]
	monkeypatch.setattr(sys, "argv", ["campy-acquire", str(config), "--fragmentedVideo", "False"])
	assert configurator.ConfigureParams()["fragmentedVideo"] is False


class Captured(Exception):
	pass


@pytest.mark.parametrize("make", ["cpu", "vaapi"])
def test_command(tmp_path, monkeypatch, make):
	# The flags reach ffmpeg as options of the video file: after the input,
	# before the file name, and imageio-ffmpeg adds no -movflags of its own
	seen = {}
	real_popen = imageio_ffmpeg._io.subprocess.Popen

	def fake_popen(cmd, *args, **kwargs):
		if "-vcodec" not in cmd: # imageio's own ffmpeg -version probe
			return real_popen(cmd, *args, **kwargs)
		seen["cmd"] = cmd
		raise Captured()

	monkeypatch.setattr(imageio_ffmpeg._io.subprocess, "Popen", fake_popen)
	monkeypatch.delenv("AMD_DEBUG", raising=False) # vaapi sets it; restored after the test
	with pytest.raises(Captured):
		writer.OpenWriter(cam(videoFolder=str(tmp_path), videoFilename="v.mp4", **ENCODERS[make]), [])
	cmd = seen["cmd"]
	start = cmd.index("-movflags")
	assert cmd[start:start + len(FRAGMENTED)] == FRAGMENTED
	assert cmd.count("-movflags") == 1 and cmd.index("-i") < start
	assert cmd[-1].endswith("v.mp4")


# ------------------------------------------------------------------ real videos

def Frames(count):
	# sweep's synthetic scene (a background and two moving blobs), each frame
	# showing its own number in 16 black or white blocks along the top. No
	# sensor noise, so the video is small (200 frames: 48 KB), well under the
	# 256 KB ffmpeg holds without -flush_packets; and no frame repeats, so
	# libx264 adds no keyframes at scene cuts, which would cut fragments too.
	frames = sweep.SyntheticFrames(W, H, "gray", count, 0.0, seed=0)
	for n, frame in enumerate(frames):
		for b in range(16):
			frame[0:20, b * 20:(b + 1) * 20] = 255 if (n >> b) & 1 else 0
	return frames


def Number(frame):
	return sum(1 << b for b in range(16) if frame[4:16, b * 20 + 4:(b + 1) * 20 - 4].mean() > 128)


def ReadFrames(path, seek=None, fps=None):
	# Every frame, or the first frame from a seek to frame number seek
	extra = {} if seek is None else {"input_params": ["-ss", "{:.6f}".format(seek / fps)],
		"output_params": ["-frames:v", "1"]}
	reader = imageio_ffmpeg.read_frames(path, pix_fmt="gray", bits_per_pixel=8, **extra)
	try:
		next(reader)
		return [np.frombuffer(b, np.uint8).reshape(H, W) for b in reader]
	finally:
		reader.close()


def Readable(path):
	try:
		return [Number(f) for f in ReadFrames(path)]
	except (OSError, RuntimeError, StopIteration): # "Could not load meta information"
		return []


def TopBoxes(path):
	# The file's top-level boxes, as (type, offset)
	boxes, pos, end = [], 0, os.path.getsize(path)
	with open(path, "rb") as f:
		while pos + 8 <= end:
			f.seek(pos)
			size, kind = struct.unpack(">I4s", f.read(8))
			if size == 1:
				size = struct.unpack(">Q", f.read(8))[0]
			boxes.append((kind.decode("latin1"), pos))
			if size < 8:
				break
			pos += size
	return boxes


def InOrder(numbers, but_last=0):
	# Every frame from the first, in order, except perhaps the last but_last
	head = numbers[:len(numbers) - but_last]
	return head == list(range(len(head))) and max(numbers) < len(numbers)


# A fragment cut between keyframes (keyframeInterval "None", or over a second)
# can end on a frame that libx264 encoded ahead of the B-frames shown before
# it (-bf 4): those are in the next fragment. A video cut off there lacks
# them, and a reader repeats a neighbour in their place, or skips them: the
# last 5 frames read need not be the last 5 sent.
TAIL = 5


def OpenWithProcess(params, monkeypatch):
	# writer.OpenWriter, and the ffmpeg process it starts
	started = []
	real_popen = imageio_ffmpeg._io.subprocess.Popen

	def popen(cmd, *args, **kwargs):
		process = real_popen(cmd, *args, **kwargs)
		if "-vcodec" in cmd:
			started.append(process)
		return process

	monkeypatch.setattr(imageio_ffmpeg._io.subprocess, "Popen", popen)
	ffmpeg, writing, _ = writer.OpenWriter(params, [])
	assert writing
	return ffmpeg, started[0]


def test_killed_ffmpeg_leaves_the_video_up_to_its_last_fragment(tmp_path, monkeypatch, capsys):
	# 20 s of video at 10 fps, in 1 s fragments (libx264's own keyframes are
	# 250 frames apart). ffmpeg is killed once 10 s of it can be read, while
	# libx264 still holds frames: what was written stays readable.
	params = cam(videoFolder=str(tmp_path), videoFilename="v.mp4", frameRate=10)
	path = str(tmp_path / "Camera0" / "v.mp4")
	ffmpeg, process = OpenWithProcess(params, monkeypatch)
	for frame in Frames(200):
		ffmpeg.send(frame)
	deadline = time.time() + 30
	while len(Readable(path)) < 100 and time.time() < deadline:
		time.sleep(0.2)
	process.kill()
	process.wait()
	ffmpeg.close() # finds ffmpeg gone and leaves the file alone

	numbers = Readable(path)
	assert 100 <= len(numbers) <= 200
	# The frames up to a fragment's end, as they were sent
	assert InOrder(numbers, but_last=TAIL)
	assert TopBoxes(path)[-1][0] != "mfra" # ffmpeg did not get to close the file


def test_killed_ffmpeg_leaves_a_plain_mp4_unreadable(tmp_path, monkeypatch, capsys):
	# What campy wrote before: the index is written only when ffmpeg closes the file
	params = cam(videoFolder=str(tmp_path), videoFilename="v.mp4", frameRate=10, fragmentedVideo=False)
	path = str(tmp_path / "Camera0" / "v.mp4")
	ffmpeg, process = OpenWithProcess(params, monkeypatch)
	for frame in Frames(200):
		ffmpeg.send(frame)
	time.sleep(2)
	process.kill()
	process.wait()
	ffmpeg.close()
	assert Readable(path) == []


def test_closed_video_reads_back_every_frame_and_seeks_exactly(tmp_path, capsys):
	# 2 s at 20 fps with a keyframe every 0.5 s, from the CPU path's libx264
	# with B-frames (-bf 4). Without delay_moov's edit list a seek to either
	# of the 2 frames before a keyframe gave the keyframe.
	fps = 20
	params = cam(videoFolder=str(tmp_path), videoFilename="v.mp4", frameRate=fps, keyframeInterval=0.5)
	ffmpeg, writing, _ = writer.OpenWriter(params, [])
	sent = Frames(41)
	for frame in sent:
		ffmpeg.send(frame)
	ffmpeg.close()
	path = str(tmp_path / "Camera0" / "v.mp4")
	assert "moof" in [kind for kind, _ in TopBoxes(path)]

	frames = ReadFrames(path)
	assert [Number(f) for f in frames] == list(range(41))
	assert imageio_ffmpeg.count_frames_and_secs(path)[0] == 41
	for n in range(41):
		assert np.array_equal(ReadFrames(path, seek=n, fps=fps)[0], frames[n]), "seek to frame {}".format(n)


@pytest.mark.parametrize("keyframe_interval", [0.5, "None"])
def test_a_video_cut_after_any_fragment_reads_up_to_there(tmp_path, capsys, keyframe_interval):
	# What ffmpeg stopped at any moment leaves: the file up to the end of a
	# fragment. With a keyframe every 0.5 s each fragment ends at one, and the
	# frames read back are exactly those sent; with libx264's own keyframes
	# (250 frames apart) fragments end every second, between keyframes.
	params = cam(videoFolder=str(tmp_path), videoFilename="v.mp4", frameRate=10, keyframeInterval=keyframe_interval)
	ffmpeg, writing, _ = writer.OpenWriter(params, [])
	for frame in Frames(60):
		ffmpeg.send(frame)
	ffmpeg.close()
	path = str(tmp_path / "Camera0" / "v.mp4")
	with open(path, "rb") as f:
		data = f.read()
	cuts = [pos for kind, pos in TopBoxes(path) if kind == "moof"][1:] + [len(data)]
	assert len(cuts) >= 6 # fragments of 1 s or less
	cut_path = str(tmp_path / "cut.mp4")
	counts = []
	for cut in cuts:
		with open(cut_path, "wb") as f:
			f.write(data[:cut])
		numbers = Readable(cut_path)
		assert InOrder(numbers, but_last=0 if keyframe_interval == 0.5 else TAIL), numbers
		counts.append(len(numbers))
	assert counts == sorted(set(counts)) and counts[-1] == 60 # each fragment adds frames


def test_a_stopped_video_is_said_to_be_readable_to_its_last_fragment(capsys):
	# writer.VideoFailed, when ffmpeg stops mid-recording (tests/test_failures.py)
	for overrides, kept in (({}, "Its video can be read up to its last fragment,"),
			({"fragmentedVideo": False}, "Its video may be unreadable,"),
			({"videoFilename": "0.mkv"}, "Its video may be unreadable,")):
		recording, stop = types.SimpleNamespace(videoError=None), []
		writer.VideoFailed(cam(**overrides), recording, stop, "ffmpeg stopped after taking 30 frames")
		assert kept in capsys.readouterr().out
		assert stop == ["STOP"] and recording.videoError == "ffmpeg stopped after taking 30 frames"
