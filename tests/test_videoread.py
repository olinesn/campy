"""
campy/videoread.py: every reader gives each frame once, in order, as gray
levels, in batches. Checked on made-up videos whose frames carry their own
index (8 black or white blocks, its bits), encoded with B-frames, so a
decoder that gave frames in decoding order rather than display order, or
dropped or doubled one, would show it. ffmpeg always; PyAV where it is
installed; NVDEC with an NVIDIA GPU and PyNvVideoCodec.
"""

import os, subprocess

import numpy as np
import pytest

from campy import videoread

W, H, N, B = 256, 192, 75, 16 # 75 frames: 4 batches of 16 and one of 11


def Frame(i):
	img = np.full((H, W), 128, np.uint8)
	for bit in range(8):
		x0 = 16 + bit * 28
		img[64:128, x0:x0 + 24] = 255 if (i >> bit) & 1 else 0
	# something that changes smoothly too, so the encoder makes B-frames
	img[150:170, (i * 3) % (W - 20):(i * 3) % (W - 20) + 20] = 200
	return img


def Index(img):
	img = np.asarray(img)
	return sum(1 << bit for bit in range(8) if img[96, 16 + bit * 28 + 12] > 128)


def Encode(path, codec="libx264", full=False, fragmented=False, n=N):
	cmd = [videoread.FfmpegExe(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", "{}x{}".format(W, H),
		"-r", "30", "-i", "-", "-c:v", codec]
	if codec == "libx264":
		cmd += ["-bf", "3", "-g", "10", "-crf", "12"]
	else:
		cmd += ["-x265-params", "bframes=3:keyint=10:log-level=error", "-crf", "12"]
	if full:
		cmd += ["-vf", "scale=out_range=pc", "-pix_fmt", "yuvj420p" if codec == "libx264" else "yuv420p",
			"-color_range", "pc"]
	else:
		cmd += ["-pix_fmt", "yuv420p"]
	if fragmented:
		cmd += ["-movflags", "+frag_keyframe+empty_moov+default_base_moof"]
	cmd.append(str(path))
	frames = np.stack([Frame(i) for i in range(n)])
	r = subprocess.run(cmd, input=frames.tobytes(), capture_output=True)
	if r.returncode != 0:
		pytest.skip("this ffmpeg can't encode {}: {}".format(codec, r.stderr.decode(errors="replace")[-200:]))
	return str(path)


@pytest.fixture(scope="module", params=["h264", "h264-full-fragmented", "hevc"])
def video(request, tmp_path_factory):
	d = tmp_path_factory.mktemp("videos")
	if request.param == "h264":
		return Encode(d / "a.mp4")
	if request.param == "h264-full-fragmented":
		return Encode(d / "b.mp4", full=True, fragmented=True)
	return Encode(d / "c.mp4", codec="libx265")


def Check(reader, n=N):
	seen, starts = [], []
	for start, frames in reader:
		frames = frames.cpu().numpy() if hasattr(frames, "cpu") else np.asarray(frames)
		assert frames.dtype == np.uint8 and frames.shape[1:] == (H, W)
		assert len(frames) <= B
		starts.append(start)
		seen += [Index(f) for f in frames]
		# the gray background: 128 in the made-up frames, whatever the range
		assert abs(int(np.median(frames[:, 10:50, 10:50])) - 128) <= 2
	assert seen == list(range(n))
	assert starts == list(range(0, n, B))
	assert reader.read == n and reader.error is None


def test_video_info(video):
	info = videoread.VideoInfo(video)
	assert (info.width, info.height) == (W, H)
	assert info.rate == 30
	assert abs(info.frames - N) <= 1
	assert info.fullRange == ("b.mp4" in video)


def test_ffmpeg_reads_every_frame_in_order(video):
	Check(videoread.FfmpegReader(videoread.VideoInfo(video), B))


def test_ffmpeg_first_frames_only(video):
	Check(videoread.FfmpegReader(videoread.VideoInfo(video), B, frames=40), 40)


def test_ffmpeg_stops_cleanly_when_the_caller_does(video):
	reader = videoread.FfmpegReader(videoread.VideoInfo(video), B)
	for start, frames in reader:
		break
	assert start == 0 and Index(frames[3]) == 3


def test_pyav_reads_every_frame_in_order(video):
	pytest.importorskip("av")
	Check(videoread.PyAVReader(videoread.VideoInfo(video), B))


def test_nvdec_reads_every_frame_in_order(video):
	info = videoread.VideoInfo(video)
	why = videoread.NvdecWhyNot(info)
	if why:
		pytest.skip(why)
	Check(videoread.NvdecReader(info, B))


def test_nvdec_gray_levels_match_ffmpeg(video):
	# the luma plane, stretched for a limited-range video, against ffmpeg's gray
	info = videoread.VideoInfo(video)
	why = videoread.NvdecWhyNot(info)
	if why:
		pytest.skip(why)
	a = np.concatenate([f.cpu().numpy() for _, f in videoread.NvdecReader(info, B, frames=32)])
	b = np.concatenate([np.array(f) for _, f in videoread.FfmpegReader(info, B, frames=32)])
	assert np.abs(a.astype(int) - b).max() <= 2


def test_limited_to_full_matches_ffmpeg(tmp_path):
	# a limited-range video's luma, stretched, against ffmpeg's own gray
	path = Encode(tmp_path / "ramp.mp4")
	ffmpeg = videoread.FfmpegExe()
	y = subprocess.run([ffmpeg, "-v", "error", "-i", path, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
		capture_output=True, check=True).stdout[:W * H]
	gray = subprocess.run([ffmpeg, "-v", "error", "-i", path, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray",
		"-"], capture_output=True, check=True).stdout
	y = np.frombuffer(y, np.uint8).reshape(H, W)
	gray = np.frombuffer(gray, np.uint8).reshape(H, W)
	assert np.abs(videoread.LimitedToFull(y).astype(int) - gray).max() <= 1
	ramp = np.arange(16, 236, dtype=np.uint8)
	full = videoread.LimitedToFull(ramp)
	assert full[0] == 0 and full[-1] == 255 and (np.diff(full.astype(int)) > 0).all()


def test_auto_picks_ffmpeg_without_an_nvidia_gpu(video, monkeypatch):
	monkeypatch.setattr(videoread, "NvdecWhyNot", lambda info=None: "no NVIDIA GPU")
	reader, note = videoread.Open(video, B)
	assert reader.kind == "ffmpeg" and note is None
	monkeypatch.setattr(videoread, "NvdecWhyNot", lambda info=None: "PyNvVideoCodec isn't installed")
	reader, note = videoread.Open(video, B)
	assert reader.kind == "ffmpeg" and "PyNvVideoCodec" in note
	with pytest.raises(SystemExit):
		videoread.Open(video, B, decoder="nvdec")


def test_find_videos_in_a_session_folder(tmp_path):
	for cam in ("Camera10", "Camera2", "Camera1"):
		(tmp_path / cam).mkdir()
		(tmp_path / cam / (cam + ".mp4")).write_bytes(b"")
	(tmp_path / "predictions").mkdir()
	(tmp_path / "predictions" / "x.slp").write_bytes(b"")
	(tmp_path / "trigger.json").write_text("{}")
	found = videoread.FindVideos([str(tmp_path)])
	assert [os.path.basename(v) for v in found] == ["Camera1.mp4", "Camera2.mp4", "Camera10.mp4"]
	# a video named twice is run once
	assert videoread.FindVideos([found[0], str(tmp_path)]) == found
	with pytest.raises(SystemExit):
		videoread.FindVideos([str(tmp_path / "nothing")])
