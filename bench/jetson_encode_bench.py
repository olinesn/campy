#!/usr/bin/env python3
"""Benchmark B2 for the Jetson AGX Orin: can its hardware encoder keep up with 3 cameras?

Stock ffmpeg can't use the Orin's video encoder (there is no libnvidia-encode
on Orin, so hevc_nvenc fails). NVIDIA's route is GStreamer's nvv4l2h265enc.
This script starts one encoder per simulated camera and feeds it synthetic
8-bit mono frames (moving bright blobs on a noisy gradient), paced at --fps
for --seconds, the way campy's writer feeds ffmpeg: one pipe per camera, one
frame per write, and a write blocks when the encoder falls behind.

Each stream is (the default, --mux ffmpeg):

    this script --pipe--> gst-launch-1.0 fdsrc ! rawvideoparse format=gray8 ...
        ! nvvidconv ! video/x-raw(memory:NVMM),format=NV12
        ! nvv4l2h265enc ratecontrol-enable=0 quant-i-frames=QP quant-p-frames=QP
            iframeinterval=N idrinterval=N insert-sps-pps=1 num-B-Frames=0
        ! h265parse ! fdsink --pipe--> ffmpeg -f hevc -i pipe: -c copy
            (campy's fragmented-MP4 flags and hvc1 tag) streamN.mp4

so the file is laid out like one campy writes today. --mux raw writes the
H.265 stream straight to a .h265 file instead (no ffmpeg), to see what the
second process costs. --cpu-fallback libx264|libx265 replaces the whole chain
with one stock ffmpeg encoding on the CPU, for comparison.

What it reports, per stream:
  frames written, achieved fps, pipe write time (p50/p99/max), how far the
  writes fell behind the --fps clock (max lag and lag at the end: a lag that
  keeps growing means the encoder can't keep up and a real recording's write
  queue would grow), CPU % of the gst-launch and ffmpeg processes (100 = one
  core), output size and bitrate, frames counted back in the file, and for
  stream 0 the PSNR of the first --psnr-frames frames against what was sent.
And for the whole machine: CPU busy % (mean and max over 1 s samples, Linux),
this script's own CPU, and the tegrastats lines logged during the run (if
tegrastats is installed; it shows the encoder clock NVENC, power and heat).

Results go to a JSON file (--json), like bench/sleap_infer_bench.py.

Examples, on the Orin (as campy's rig tests do, through bench/rig.py when
other people use the machine):

    python3 bench/jetson_encode_bench.py                       # 3 x 60 fps, 30 s, QP 21
    python3 bench/jetson_encode_bench.py --seconds 1800        # the 30-minute pass/fail run
    python3 bench/jetson_encode_bench.py --mux raw             # without the ffmpeg muxer
    python3 bench/jetson_encode_bench.py --cpu-fallback libx264 --cpu-preset ultrafast
    python3 bench/jetson_encode_bench.py --dry-run             # print the commands only

Pass for B2: 3 x 60 fps for 30 minutes with the lag at the end under one
frame (no queue growth). Needs numpy; nothing from campy is imported.
"""

import argparse
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime

# campy/writer.py's fragmented-MP4 flags (EncoderArgs, fragmentedVideo): a file
# stays readable up to its last fragment if the muxer is stopped early.
# tests/test_jetson_encode_bench.py checks that these match writer.py.
FRAGMENTED_MP4 = [
    "-movflags", "+frag_keyframe+empty_moov+delay_moov+default_base_moof",
    "-frag_duration", "1000000",
    "-flush_packets", "1",
]
# H.265 in an MP4 is tagged hvc1 (writer.py: Apple's decoder refuses hev1)
HVC1 = ["-tag:v", "hvc1"]

# nvv4l2h265enc properties used, as named in NVIDIA's Jetson Linux r39.2
# "Accelerated GStreamer" guide. The script checks them against
# gst-inspect-1.0 before it starts, so a renamed property stops it with a
# clear message rather than a gst-launch error.
#   ratecontrol-enable=0       constant QP (1, the default, is variable)
#   quant-i-frames / quant-p-frames   the QP of I and P frames
#   iframeinterval / idrinterval      frames between I frames / IDR frames
#   insert-sps-pps=1           parameter sets before every IDR (a reader can
#                              start at any keyframe)
#   num-B-Frames=0             no B frames, like campy's GPU paths
#   preset-level               hardware preset (only with --preset-level)
#   maxperf-enable             encoder at full clock (only with --maxperf; in
#                              older guides, not in r39.2's)
ENCODER = "nvv4l2h265enc"


def frame_bytes(width, height):
    return width * height


def keyframe_frames(fps, keyframe_interval):
    # campy's keyframeInterval is in seconds; encoders count frames
    return max(1, int(round(fps * keyframe_interval)))


def gst_encoder_props(qp, iframe_interval, preset_level=None, maxperf=False):
    props = [
        "ratecontrol-enable=0",
        "quant-i-frames=%d" % qp,
        "quant-p-frames=%d" % qp,
        "iframeinterval=%d" % iframe_interval,
        "idrinterval=%d" % iframe_interval,
        "insert-sps-pps=1",
        "num-B-Frames=0",
    ]
    if preset_level is not None:
        props.append("preset-level=%d" % preset_level)
    if maxperf:
        props.append("maxperf-enable=1")
    return props


def gst_args(width, height, fps, qp, iframe_interval, out, preset_level=None,
             maxperf=False, gst="gst-launch-1.0"):
    """gst-launch-1.0 arguments for one stream: raw GRAY8 frames on stdin, H.265 out.

    out is a file path (written by filesink, the raw .h265 stream) or an int,
    a file descriptor the H.265 byte-stream is written to (fdsink) for ffmpeg
    to read. Each word is one argument, so nothing is parsed by a shell.
    """
    size = frame_bytes(width, height)
    args = [gst, "-q", "-e",
            "fdsrc", "fd=0", "blocksize=%d" % size, "!",
            "rawvideoparse", "format=gray8", "width=%d" % width, "height=%d" % height,
            "framerate=%d/1" % fps, "!",
            "nvvidconv", "!",
            "video/x-raw(memory:NVMM),format=NV12", "!",
            ENCODER] + gst_encoder_props(qp, iframe_interval, preset_level, maxperf) + [
            "!", "h265parse", "!"]
    if isinstance(out, int):
        args += ["fdsink", "fd=%d" % out, "sync=false"]
    else:
        args += ["filesink", "location=%s" % out, "sync=false"]
    return args


def ffmpeg_mux_args(fps, out, ffmpeg="ffmpeg"):
    """ffmpeg arguments that copy an H.265 byte-stream on stdin into a campy-style MP4."""
    return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-f", "hevc", "-framerate", str(fps), "-i", "pipe:0",
            "-c", "copy"] + FRAGMENTED_MP4 + HVC1 + [out]


def ffmpeg_cpu_args(codec, width, height, fps, qp, iframe_interval, preset, out, ffmpeg="ffmpeg"):
    """ffmpeg arguments for the CPU fallback: raw gray frames on stdin, libx264/libx265 at a fixed QP.

    Full-range 4:2:0 like campy's GPU paths (scale out_range=full, -color_range
    pc), so the gray levels come back as they went in; no B frames, like the
    Orin encoder, so the two are compared on the same terms.
    """
    if codec not in ("libx264", "libx265"):
        raise ValueError("codec is libx264 or libx265, not %r" % codec)
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "gray", "-s", "%dx%d" % (width, height),
            "-framerate", str(fps), "-i", "pipe:0",
            "-vf", "scale=out_range=full", "-pix_fmt", "yuv420p", "-color_range", "pc",
            "-c:v", codec, "-preset", preset, "-g", str(iframe_interval), "-bf", "0"]
    if codec == "libx264":
        args += ["-qp", str(qp)]
    else:
        # closed GOP, as campy's libx265 (writer.py)
        args += ["-x265-params", "qp=%d:keyint=%d:open-gop=0:log-level=error" % (qp, iframe_interval)]
    args += FRAGMENTED_MP4
    if codec == "libx265":
        args += HVC1
    return args + [out]


def stream_commands(args, index, out_dir, gst_fd=None):
    """The commands for stream `index`: (encoder argv, muxer argv or None, output path)."""
    kf = keyframe_frames(args.fps, args.keyframe_interval)
    if args.cpu_fallback:
        out = os.path.join(out_dir, "stream%d_%s.mp4" % (index, args.cpu_fallback))
        return (ffmpeg_cpu_args(args.cpu_fallback, args.width, args.height, args.fps, args.qp, kf,
                                args.cpu_preset, out, ffmpeg=args.ffmpeg), None, out)
    if args.mux == "raw":
        out = os.path.join(out_dir, "stream%d.h265" % index)
        return (gst_args(args.width, args.height, args.fps, args.qp, kf, out,
                         args.preset_level, args.maxperf, gst=args.gst_launch), None, out)
    out = os.path.join(out_dir, "stream%d.mp4" % index)
    fd = 3 if gst_fd is None else gst_fd
    return (gst_args(args.width, args.height, args.fps, args.qp, kf, fd,
                     args.preset_level, args.maxperf, gst=args.gst_launch),
            ffmpeg_mux_args(args.fps, out, ffmpeg=args.ffmpeg), out)


def missing_properties(inspect_text, props):
    """Property names in props ("name=value") that gst-inspect-1.0's output doesn't list."""
    listed = set(re.findall(r"^\s{2}([A-Za-z][\w-]*)\s*:", inspect_text, re.M))
    return [p.split("=", 1)[0] for p in props if p.split("=", 1)[0] not in listed]


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------

def make_frames(width, height, count, seed=0):
    """`count` gray frames: a gradient, sensor-like noise, and 5 bright blobs moving around.

    Real camera video compresses differently (texture, lighting), so file sizes
    are indicative; the encoder's speed hardly depends on the content.
    """
    import numpy as np
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    base = 40 + 60 * (xx / width) + 20 * (yy / height)
    frames = []
    blobs = [(rng.uniform(0.2, 0.8) * width, rng.uniform(0.2, 0.8) * height,
              rng.uniform(-6, 6), rng.uniform(-6, 6)) for _ in range(5)]
    for i in range(count):
        img = base.copy()
        for (x0, y0, vx, vy) in blobs:
            # bounce inside the frame
            x = abs((x0 + vx * i) % (2 * width) - width) if vx else x0
            y = abs((y0 + vy * i) % (2 * height) - height) if vy else y0
            r2 = ((xx - x) / 60.0) ** 2 + ((yy - y) / 30.0) ** 2
            img += 150 * np.exp(-r2)
        img += rng.normal(0, 2.0, size=img.shape).astype(np.float32)
        frames.append(np.clip(img, 0, 255).astype(np.uint8).tobytes())
    return frames


# --------------------------------------------------------------------------
# CPU accounting
# --------------------------------------------------------------------------

def read_proc_stat():
    # (busy, total) jiffies over all CPUs, Linux only
    try:
        with open("/proc/stat") as f:
            vals = [int(v) for v in f.readline().split()[1:]]
    except OSError:
        return None
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    total = sum(vals[:8])
    return total - idle, total


class SystemCPU(threading.Thread):
    """Samples whole-machine CPU busy % once a second (Linux /proc/stat)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.samples = []
        self.stop = threading.Event()
        self.first = read_proc_stat()

    def run(self):
        prev = self.first
        while not self.stop.wait(1.0):
            cur = read_proc_stat()
            if prev and cur and cur[1] > prev[1]:
                self.samples.append(100.0 * (cur[0] - prev[0]) / (cur[1] - prev[1]))
            prev = cur

    def summary(self):
        last = read_proc_stat()
        out = {"cores": os.cpu_count()}
        if self.first and last and last[1] > self.first[1]:
            out["busy_pct_mean"] = 100.0 * (last[0] - self.first[0]) / (last[1] - self.first[1])
        if self.samples:
            out["busy_pct_max_1s"] = max(self.samples)
        return out


class Tegrastats(threading.Thread):
    """Keeps every line tegrastats prints during the run (encoder clock, power, heat)."""

    def __init__(self, exe, interval_ms=1000):
        super().__init__(daemon=True)
        self.lines = []
        self.proc = subprocess.Popen([exe, "--interval", str(interval_ms)], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True)

    def run(self):
        for line in self.proc.stdout:
            self.lines.append("%.1f %s" % (time.time(), line.rstrip()))

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def wait_with_rusage(proc, timeout):
    """Wait for a Popen, and return the CPU seconds (user + system) it used, or None.

    os.wait4 gives the child's own resource use, on Linux and macOS.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            pid, status, ru = os.wait4(proc.pid, os.WNOHANG)
        except ChildProcessError:
            return None
        if pid:
            proc.returncode = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status
            return ru.ru_utime + ru.ru_stime
        if time.monotonic() > deadline:
            proc.kill()
            timeout = 5
            deadline = time.monotonic() + 5
        time.sleep(0.05)


# --------------------------------------------------------------------------
# One stream
# --------------------------------------------------------------------------

def finite(data):
    # JSON any program reads: an infinite or NaN number (a PSNR of identical
    # frames is inf) becomes null, as campy/strictjson.py does
    if isinstance(data, dict):
        return {k: finite(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [finite(v) for v in data]
    if isinstance(data, float) and not math.isfinite(data):
        return None
    return data


def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(math.ceil(q / 100.0 * len(s))) - 1)]


class Stream:
    def __init__(self, args, index, out_dir, frames):
        self.index = index
        self.args = args
        self.frames = frames
        self.offset = (index * 17) % len(frames)  # streams don't send identical frames
        self.mux = None
        self.err_files = []
        if args.cpu_fallback or args.mux == "raw":
            enc_argv, _, self.out = stream_commands(args, index, out_dir)
            self.enc = self._popen(enc_argv, "enc", stdout=subprocess.DEVNULL)
        else:
            r, w = os.pipe()
            enc_argv, mux_argv, self.out = stream_commands(args, index, out_dir, gst_fd=w)
            self.mux = self._popen(mux_argv, "mux", stdin=r, stdout=subprocess.DEVNULL)
            self.enc = self._popen(enc_argv, "enc", stdout=subprocess.DEVNULL, pass_fds=(w,))
            os.close(r)
            os.close(w)
        self.commands = {"encoder": enc_argv, "muxer": mux_argv if self.mux else None}
        self.write_ms = []
        self.lags = []
        self.sent = 0
        self.error = None

    def _popen(self, argv, tag, **kw):
        err = tempfile.TemporaryFile()
        self.err_files.append((tag, err))
        kw.setdefault("stdin", subprocess.PIPE)
        return subprocess.Popen(argv, stderr=err, **kw)

    def feed(self, start_at, n_frames):
        period = 1.0 / self.args.fps
        pipe = self.enc.stdin
        frames, k = self.frames, len(self.frames)
        try:
            # the first frame waits for the pipeline to start; the clock starts after it
            while time.monotonic() < start_at:
                time.sleep(0.001)
            pipe.write(frames[self.offset % k])
            self.sent = 1
            t0 = time.monotonic()
            for i in range(1, n_frames):
                due = t0 + i * period
                now = time.monotonic()
                if due > now:
                    time.sleep(due - now)
                a = time.monotonic()
                pipe.write(frames[(self.offset + i) % k])
                b = time.monotonic()
                self.sent += 1
                self.write_ms.append(1000.0 * (b - a))
                self.lags.append(1000.0 * (b - due))
            self.t_first, self.t_last = t0, time.monotonic()
        except (BrokenPipeError, OSError) as e:
            self.error = "pipe closed after %d frames: %s" % (self.sent, e)
            self.t_first = getattr(self, "t_first", None) or time.monotonic()
            self.t_last = time.monotonic()
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    def finish(self, elapsed_wall, timeout):
        enc_cpu = wait_with_rusage(self.enc, timeout)
        mux_cpu = wait_with_rusage(self.mux, timeout) if self.mux else None
        errs = {}
        for tag, f in self.err_files:
            f.seek(0)
            text = f.read().decode(errors="replace").strip()
            f.close()
            if text:
                errs[tag] = text[-2000:]
        span = max(1e-9, self.t_last - self.t_first)
        period_ms = 1000.0 / self.args.fps
        r = {
            "stream": self.index,
            "output": self.out,
            "frames_written": self.sent,
            "achieved_fps": (self.sent - 1) / span if self.sent > 1 else None,
            "write_ms_p50": pct(self.write_ms, 50),
            "write_ms_p99": pct(self.write_ms, 99),
            "write_ms_max": max(self.write_ms) if self.write_ms else None,
            "lag_ms_max": max(self.lags) if self.lags else None,
            "lag_ms_end": statistics.mean(self.lags[-int(self.args.fps):]) if self.lags else None,
            "frames_late": sum(1 for x in self.lags if x > period_ms),
            "encoder_exit": self.enc.returncode,
            "encoder_cpu_pct": 100.0 * enc_cpu / elapsed_wall if enc_cpu is not None else None,
            "muxer_exit": self.mux.returncode if self.mux else None,
            "muxer_cpu_pct": 100.0 * mux_cpu / elapsed_wall if mux_cpu is not None else None,
            "bytes": os.path.getsize(self.out) if os.path.exists(self.out) else None,
            "error": self.error,
            "stderr": errs or None,
        }
        if r["bytes"] and self.sent:
            r["mbit_per_s"] = r["bytes"] * 8 / 1e6 / (self.sent / self.args.fps)
        return r


# --------------------------------------------------------------------------
# Reading the files back
# --------------------------------------------------------------------------

def count_frames(ffmpeg, path):
    """Frames in a video file: its packets, one line each in ffmpeg's framecrc listing (no ffprobe needed)."""
    fmt = ["-f", "hevc"] if path.endswith(".h265") else []
    p = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"] + fmt + ["-i", path,
                        "-map", "0:v:0", "-c", "copy", "-f", "framecrc", "-"], capture_output=True, text=True)
    if p.returncode:
        return None
    return sum(1 for line in p.stdout.splitlines() if line and not line.startswith("#"))


def psnr_args(ffmpeg, path, ref, width, height, fps, n_frames, raw_luma):
    """ffmpeg arguments for the PSNR of a video's first n_frames against the frames sent.

    raw_luma: compare the decoded Y plane as stored (extractplanes), rather
    than converted to gray by the video's range. The two tell whether the
    encoder kept the full 0-255 range (raw matches) or squeezed it to 16-235.
    """
    fmt = ["-f", "hevc", "-framerate", str(fps)] if path.endswith(".h265") else []
    # frames are paired by their index, not their timestamps (an encoder's
    # start offset would otherwise pair each frame with its neighbour)
    n = "setpts=N/(%d*TB)" % fps
    a = ("[0:v]extractplanes=y," if raw_luma else "[0:v]format=gray,") + n + "[a]"
    return [ffmpeg, "-hide_banner", "-nostdin"] + fmt + ["-i", path,
            "-f", "rawvideo", "-pix_fmt", "gray", "-s", "%dx%d" % (width, height),
            "-framerate", str(fps), "-i", ref,
            "-lavfi", a + ";[1:v]format=gray," + n + "[b];[a][b]psnr", "-frames:v", str(n_frames), "-f", "null", "-"]


def parse_psnr(stderr):
    m = re.findall(r"PSNR .*?average:(inf|[\d.]+)", stderr)
    if not m:
        return None
    return float("inf") if m[-1] == "inf" else float(m[-1])


def measure_psnr(args, path, frames, offset, n_sent, tmp):
    # the reference is the frames sent, written out in full (ffmpeg's
    # -stream_loop on a raw file paired them wrongly after the first loop)
    ref = os.path.join(tmp, "reference.gray")
    k = len(frames)
    n = min(args.psnr_frames, n_sent)
    with open(ref, "wb") as f:
        for i in range(n):
            f.write(frames[(offset + i) % k])
    out = {}
    for raw in (True, False):
        p = subprocess.run(psnr_args(args.ffmpeg, path, ref, args.width, args.height, args.fps,
                                     n, raw), capture_output=True, text=True)
        out["psnr_db_stored_luma" if raw else "psnr_db_by_range"] = parse_psnr(p.stderr)
    os.remove(ref)
    return out


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

class Help(argparse.ArgumentDefaultsHelpFormatter):
    # shows a default only where there is one (not for flags and None)
    def _get_help_string(self, action):
        if action.default in (None, False) or "default" in (action.help or ""):
            return action.help
        return super()._get_help_string(action)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Benchmark B2: N paced streams of synthetic 8-bit mono frames into the Jetson's "
                    "hardware H.265 encoder (GStreamer nvv4l2h265enc), or into ffmpeg on the CPU "
                    "with --cpu-fallback. Reports per-stream fps, pipe back-pressure, CPU and file "
                    "size, as JSON.",
        epilog="Pass for the Orin: 3 x 60 fps for 30 minutes (--seconds 1800) with lag_ms_end under "
               "one frame (16.7 ms) on every stream. --dry-run prints the commands without running them.",
        formatter_class=Help,
    )
    p.add_argument("--streams", type=int, default=3, help="encoders running at once (cameras)")
    p.add_argument("--fps", type=int, default=60, help="frames per second fed to each stream")
    p.add_argument("--seconds", type=float, default=30.0, help="how long to feed them")
    p.add_argument("--width", type=int, default=1280, help="frame width (Blackfly S 1.3 MP)")
    p.add_argument("--height", type=int, default=1024, help="frame height")
    p.add_argument("--qp", type=int, default=21,
                   help="fixed quantizer for I and P frames (campy's default quality)")
    p.add_argument("--keyframe-interval", type=float, default=1.0,
                   help="seconds between keyframes (campy's keyframeInterval)")
    g = p.add_argument_group("Jetson encoder (GStreamer)")
    g.add_argument("--mux", choices=["ffmpeg", "raw"], default="ffmpeg",
                   help="ffmpeg: pipe the H.265 into stock ffmpeg -c copy, making a fragmented MP4 "
                        "like campy's; raw: write a .h265 file with filesink, no second process")
    g.add_argument("--preset-level", type=int, default=None,
                   help="nvv4l2h265enc preset-level (default: the encoder's own)")
    g.add_argument("--maxperf", action="store_true",
                   help="add maxperf-enable=1 (only if this JetPack's encoder has it)")
    g.add_argument("--gst-launch", default="gst-launch-1.0", help="gst-launch-1.0 to run")
    g.add_argument("--no-property-check", action="store_true",
                   help="don't check the encoder's property names with gst-inspect-1.0 first")
    g = p.add_argument_group("CPU fallback")
    g.add_argument("--cpu-fallback", choices=["libx264", "libx265"], default=None,
                   help="encode with stock ffmpeg on the CPU instead of the Jetson encoder")
    g.add_argument("--cpu-preset", default="fast",
                   help="x264/x265 preset (campy's CPU default is fast; try ultrafast or superfast)")
    p.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg to run (muxer, CPU fallback, reading back)")
    p.add_argument("--out-dir", default=None,
                   help="where the videos go (default: a new folder under /data if it exists, else cwd); "
                        "put it on the disk recordings will use")
    p.add_argument("--keep", action="store_true", help="keep the videos (they are deleted at the end otherwise)")
    p.add_argument("--pattern-frames", type=int, default=120,
                   help="distinct synthetic frames, sent in a loop")
    p.add_argument("--psnr-frames", type=int, default=300,
                   help="frames of stream 0 compared with what was sent (0: skip)")
    p.add_argument("--no-tegrastats", action="store_true", help="don't log tegrastats during the run")
    p.add_argument("--json", default=None, help="output JSON path (default: timestamped in cwd)")
    p.add_argument("--dry-run", action="store_true", help="print each stream's commands and exit")
    args = p.parse_args(argv)
    if args.streams < 1 or args.fps < 1 or args.seconds <= 0:
        p.error("--streams, --fps and --seconds must be positive")
    return args


def tool_versions(args):
    out = {}
    for name, argv in (("gst", [args.gst_launch, "--version"]), ("ffmpeg", [args.ffmpeg, "-version"])):
        try:
            out[name] = subprocess.run(argv, capture_output=True, text=True, timeout=10).stdout.splitlines()[:2]
        except (OSError, subprocess.TimeoutExpired):
            out[name] = None
    try:
        with open("/etc/nv_tegra_release") as f:
            out["l4t"] = f.readline().strip()
    except OSError:
        out["l4t"] = None
    try:
        out["nvpmodel"] = subprocess.run(["nvpmodel", "-q"], capture_output=True, text=True,
                                         timeout=10).stdout.strip().splitlines()
    except (OSError, subprocess.TimeoutExpired):
        out["nvpmodel"] = None
    return out


def main(argv=None):
    args = parse_args(argv)
    total = int(round(args.fps * args.seconds))

    if args.dry_run:
        for i in range(args.streams):
            enc, mux, out = stream_commands(args, i, args.out_dir or ".")
            print("stream %d -> %s" % (i, out))
            print("  encoder: " + " ".join(enc))
            if mux:
                print("  muxer:   " + " ".join(mux))
        return 0

    exe = args.ffmpeg if args.cpu_fallback else args.gst_launch
    if not shutil.which(exe):
        sys.exit("%s not found. %s" % (exe, "Install ffmpeg (sudo apt install ffmpeg)." if args.cpu_fallback else
                 "On the Jetson: sudo apt install nvidia-jetpack gstreamer1.0-tools "
                 "gstreamer1.0-plugins-base gstreamer1.0-plugins-bad; or use --cpu-fallback."))
    if not args.cpu_fallback and args.mux == "ffmpeg" and not shutil.which(args.ffmpeg):
        sys.exit("%s not found (needed for --mux ffmpeg): sudo apt install ffmpeg, or use --mux raw" % args.ffmpeg)
    if not args.cpu_fallback and not args.no_property_check:
        inspect = shutil.which("gst-inspect-1.0") or "gst-inspect-1.0"
        try:
            text = subprocess.run([inspect, ENCODER], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired) as e:
            sys.exit("gst-inspect-1.0 %s failed (%s); --no-property-check skips this" % (ENCODER, e))
        if not text:
            sys.exit("gst-inspect-1.0 finds no %s: is this a Jetson with JetPack's multimedia packages "
                     "(sudo apt install nvidia-jetpack)?" % ENCODER)
        props = gst_encoder_props(args.qp, 1, args.preset_level, args.maxperf)
        missing = missing_properties(text, props)
        if missing:
            sys.exit("%s has no propert%s %s on this JetPack (see gst-inspect-1.0 %s); "
                     "--no-property-check runs anyway" % (ENCODER, "y" if len(missing) == 1 else "ies",
                                                           ", ".join(missing), ENCODER))

    out_dir = args.out_dir
    if out_dir is None:
        base = "/data" if os.path.isdir("/data") and os.access("/data", os.W_OK) else os.getcwd()
        out_dir = os.path.join(base, "jetson_encode_bench_%s" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)

    print("making %d synthetic %dx%d frames..." % (args.pattern_frames, args.width, args.height), flush=True)
    frames = make_frames(args.width, args.height, args.pattern_frames)

    tegra = None
    if not args.no_tegrastats and shutil.which("tegrastats"):
        tegra = Tegrastats(shutil.which("tegrastats"))
        tegra.start()
    import resource  # POSIX only, like os.wait4: this runs on Linux (and macOS for the CPU fallback)
    syscpu = SystemCPU()
    self0 = resource.getrusage(resource.RUSAGE_SELF)
    wall0 = time.monotonic()

    streams = [Stream(args, i, out_dir, frames) for i in range(args.streams)]
    start_at = time.monotonic() + 0.5
    syscpu.start()
    threads = [threading.Thread(target=s.feed, args=(start_at, total), daemon=True) for s in streams]
    label = args.cpu_fallback or ("%s, mux %s" % (ENCODER, args.mux))
    print("feeding %d streams x %d fps for %g s (%s) into %s" % (args.streams, args.fps, args.seconds,
                                                                 label, out_dir), flush=True)
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    results = [s.finish(time.monotonic() - wall0, timeout=60) for s in streams]
    elapsed = time.monotonic() - wall0
    syscpu.stop.set()
    self1 = resource.getrusage(resource.RUSAGE_SELF)
    if tegra:
        tegra.close()

    for r in results:
        r["frames_in_file"] = count_frames(args.ffmpeg, r["output"]) if r["bytes"] and shutil.which(args.ffmpeg) else None
    if args.psnr_frames and results[0]["bytes"] and shutil.which(args.ffmpeg):
        results[0].update(measure_psnr(args, results[0]["output"], frames, streams[0].offset,
                                       results[0]["frames_written"], out_dir))

    header = {
        "bench": "jetson_encode_bench",
        "date": datetime.now().isoformat(timespec="seconds"),
        "argv": sys.argv[1:] if argv is None else list(argv),
        "settings": {k: v for k, v in vars(args).items() if k not in ("json", "dry_run")},
        "frames_per_stream": total,
        "encoder": label,
        "commands": [s.commands for s in streams],
        "versions": tool_versions(args),
    }
    system = syscpu.summary()
    system["elapsed_s"] = elapsed
    system["feeder_cpu_pct"] = 100.0 * ((self1.ru_utime - self0.ru_utime) + (self1.ru_stime - self0.ru_stime)) / elapsed
    if tegra:
        system["tegrastats"] = tegra.lines

    if not args.keep:
        for r in results:
            if os.path.exists(r["output"]):
                os.remove(r["output"])
        try:
            os.rmdir(out_dir)
        except OSError:
            pass

    print()
    print("%-7s %8s %8s %9s %9s %9s %8s %8s %9s %8s" % ("stream", "frames", "fps", "write p99", "write max",
                                                         "lag end", "enc CPU", "mux CPU", "Mbit/s", "in file"))
    f = lambda v, fmt: "-" if v is None else fmt % v
    for r in results:
        print("%-7d %8d %8s %9s %9s %9s %8s %8s %9s %8s" % (
            r["stream"], r["frames_written"], f(r["achieved_fps"], "%.2f"), f(r["write_ms_p99"], "%.1f ms"),
            f(r["write_ms_max"], "%.1f ms"), f(r["lag_ms_end"], "%.1f ms"), f(r["encoder_cpu_pct"], "%.0f%%"),
            f(r["muxer_cpu_pct"], "%.0f%%"), f(r.get("mbit_per_s"), "%.1f"), f(r["frames_in_file"], "%d")))
        if r["error"] or r["encoder_exit"] or r["muxer_exit"]:
            print("  stream %d failed: %s %s" % (r["stream"], r["error"] or "", r["stderr"] or ""))
    print("machine CPU busy: mean %s, max %s over 1 s (of %d cores); this script %.0f%%" % (
        f(system.get("busy_pct_mean"), "%.0f%%"), f(system.get("busy_pct_max_1s"), "%.0f%%"),
        system["cores"], system["feeder_cpu_pct"]))
    if "psnr_db_stored_luma" in results[0]:
        print("stream 0 PSNR, first %d frames: %s dB as stored, %s dB by the video's range" % (
            min(args.psnr_frames, results[0]["frames_written"]), f(results[0]["psnr_db_stored_luma"], "%.2f"), f(results[0]["psnr_db_by_range"], "%.2f")))

    out_path = args.json or "jetson_encode_bench_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(out_path, "w") as fh:
        json.dump(finite({"header": header, "system": system, "results": results}), fh, indent=2,
                  allow_nan=False)
    print("wrote %s" % out_path)
    ok = all(r["frames_written"] == total and not r["error"] and not r["encoder_exit"] and not r["muxer_exit"]
             for r in results)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
