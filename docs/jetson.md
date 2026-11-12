# Jetson AGX Orin

An **NVIDIA Jetson AGX Orin 64GB Developer Kit** joined the test machines on 11 November 2026, next to the GMKtec EVO-X2, the Dell Precision 7875, the Dell Pro Max Slim FCS1250, the Dell Pro Tower Plus and the Windows 11 virtual machine. It is a small stand-alone rig: 12 Arm Cortex-A78AE cores, an Ampere GPU (compute capability 8.7) that shares the 64 GB of memory, and a video encoder of its own. This page is how it was set up, what works, what doesn't yet, and what it measured.

**In short:**

- **Live tracking works.** With campy's defaults (`campy-live --engine auto` picks TensorRT here), the trained 5-mouse pair tracks 3 cameras at 60 fps with 3D, every set, with the same accuracy as the other machines, in about twice the EVO-X2's time per set.
- **Recording uses the Orin's own video encoder** (`gpuMake: "jetson"`, since pull request 403). ffmpeg can't reach it there, so campy hands each camera's frames to GStreamer, which encodes them, and ffmpeg only writes them into campy's usual MP4. At 1280 × 1024 mono and 60 fps it keeps up with 8 cameras in H.264 and 7 in H.265 (8 at quality 25), at about a fifth of a CPU core per camera; at most 8 at once. Recording and tracking 3 cameras kept every frame. The CPU path (`gpuID: -1`, libx264 preset `ultrafast`) still works, at three quarters of a core per camera.
- **FLIR cameras work**: Spinnaker 4.4 has an Arm64 build, and a Blackfly S on the Orin's USB 3 port is found at SuperSpeed.
- **It needs JetPack 7.2 or newer.** JetPack 7.1 installs on an AGX Orin but leaves its GPU without a driver.

## What it measured (11 November 2026)

Every number here: JetPack 7.2.1 (Jetson Linux 39.2.1), power mode MAXN, clocks held by `jetson_clocks` (CPU 2.2 GHz, GPU 1.3 GHz), fan at full speed, under the rig lock ([`bench/rig.py`](development.md#timing-tests-on-a-shared-machine-benchrigpy)), each run `RIG_CLEAN` unless said otherwise. PyTorch 2.15.0+cu132, sleap-nn 0.3.5, TensorRT 10.16.2, campy dev `6cdc979` unless said otherwise. No monitor was attached.

### Live tracking

The trained 5-mouse pair the other machines use for their rig checks (centroid at scale 0.25, output stride 2; centered instance 320 px crop at scale 0.5, max stride 32, 24 filters), fp16, fixed crops and CUDA graphs. Time per set, p50 / p99:

| `bench/sleap_infer_bench.py --trained ... --pace 60` | 3 cameras | 8 cameras |
|---|---|---|
| **TensorRT** (`--engine auto` picks it on the Orin) | **9.6 / 9.7 ms** | **22.7 / 22.9 ms** |
| `torch.compile` | 16.3 / 16.4 ms | 40.0 / 40.3 ms |
| plain PyTorch (`--engine off`) | 23.3 / 23.5 ms | 59.8 / 60.1 ms |
| for comparison, compiled: RTX A2000 / Intel Arc Pro B50 / RTX 2000 Ada | 6.9 / 9.9 / 5.3–5.5 ms | 20.9 / 23.5 / 15.4 ms |

So, with TensorRT, 3 cameras at 60 fps fit with room to spare, and 8 cameras fit at 30 fps, not 60: about the Arc B50, a little behind the RTX A2000. `torch.compile` is much slower than TensorRT here (on the other NVIDIA cards the two are closer); campy's default picks TensorRT as long as `tensorrt` and `onnx` import in the environment (below). Building the two TensorRT engines took about 30–45 s the first time; they are kept in `~/.cache/campy/engines`, and later starts took 9 s.

The rig checks, the same as the EVO-X2's, with campy-live's defaults:

| Check | Sets | Inference p50 / p99 | To 3D p50 / p99 (max) | Frames recorded |
|---|---|---|---|---|
| replay3: the 60 s 5-mouse replay, 3 cameras, 3D | 3,600 of 3,600, 0 skipped | 10.1 / 10.4 ms | 14.5 / 15.8 (29.8) ms | (a replay) |
| smoke1: 1 emulated camera recorded and tracked, 30 s | 1,800, 0 skipped | 4.0 / 4.2 ms | | 1,800 |
| smoke3: 3 emulated cameras recorded (libx264 `ultrafast` on the CPU) and tracked in 3D, 60 s | 3,600, 0 skipped | 10.2 / 10.7 ms | 14.7 / 16.5 (24.1) ms | 3 × 3,600 |
| smoke3 recorded on the Orin's encoder (`gpuMake: "jetson"`, H.265 quality 21, pull request 403) | 3,600, 0 skipped | 10.2 / 10.6 ms | 14.5 / 15.8 (23.7) ms | 3 × 3,600 |
| **Since 11 November** (campy dev `5b3aefb`): replay3 | 3,600 of 3,600, 0 skipped | 10.1 / 10.3 ms | 14.5 / 15.7 (24.3) ms | (a replay) |
| smoke1, on the Orin's encoder | 1,800, 0 skipped | 3.9 / 4.1 ms | | 1,800 |
| smoke3, on the Orin's encoder | 3,600, 0 skipped | 10.2 / 10.7 ms | 14.6 / 16.1 (25.3) ms | 3 × 3,600 |
| **12 November** (campy dev `7952d90`): replay3 | 3,600 of 3,600, 0 skipped | 10.1 / 10.3 ms | 14.5 / 15.7 (29.7) ms | (a replay) |
| smoke1, on the Orin's encoder | 1,800, 0 skipped | 3.9 / 4.1 ms | | 1,800 |
| smoke3, on the Orin's encoder | 3,600, 0 skipped | 10.2 / 10.6 ms | 14.6 / 16.0 (22.0) ms | 3 × 3,600 |

The replay tracked as the other machines do: against the offline SLEAP predictions, 2D median 3.8 px, 3D median 4.2 mm, 99.93% the same identity, 2 identity switches (the EVO-X2: 3.8 px, 4.2 mm, 2 switches). The EVO-X2 takes about half the time (inference 5.5 / 5.6 ms in the same replay). The 3D step alone took 2.8 ms here against about 1.1 ms there: it runs on the CPU (2.3 ms here since pull request 420: [The CPU's share](#the-cpus-share-which-cores-and-numpys-threads-12-november)). While smoke3 ran, the CPU was about half busy, the three libx264 encoders included. Recorded on the Orin's encoder instead, tracking took the same time; the CPU was about 38% busy at mid-run (one sample, `top`), most of it the emulated cameras decoding their clips.

Since 11 November the rig check records on the Orin's encoder, as a recording on the Orin would: smoke1 and smoke3 use `gpuMake: "jetson"`, H.265 at quality 21 (`ENC=cpu` still runs the CPU path, libx264 `ultrafast`). Its first run, the three rows from "Since 11 November", came after the Orin's campy moved to dev `5b3aefb` (the Jetson writer, pull request 403): the replay tracked as before (2D median 3.8 px, 3D median 4.2 mm, 2 identity switches), each recorded video held every frame (4.1–5.0 Mbit/s per camera in smoke3), and the CPU was about 39% busy at smoke3's mid-run (one sample).

On 12 November, after the Orin's campy moved to dev `7952d90` (pull requests 406, 408, 410 and 411; none changes what the rig check runs: 408 is for TensorRT engines, which it does not use), the rig check ran again, `RIG_CLEAN`, and gave the same times to within 0.1 ms: the last three rows above. The replay tracked as before (2D median 3.8 px, 3D median 4.2 mm, 2 identity switches), and every frame was recorded.

Not checked: how far TensorRT's fp16 moves the keypoints from fp32 on the Orin (the replay's accuracy above already includes it), and a real camera feeding `campy-live`.

#### The CPU's share: which cores, and numpy's threads (12 November)

The Orin's cores are at their fastest already (MAXN, 2.2 GHz held), and each is slower than an x86 desktop core, so the 3D step, which runs on the CPU, takes about 2.5 times as long as on the EVO-X2. Its 12 cores are three clusters of four (0–3, 4–7, 8–11), each sharing its own cache, and every hardware interrupt (USB, the NVMe disk, the network) is handled on CPU 0. What changing how the CPU is used does, measured with the replay check (60 s, 3 cameras, 5 mice, 3D), each setting run twice, in alternating order, all under one lock, `RIG_CLEAN` (campy dev `7952d90`). Times in ms, the two runs' p50 / p99:

| Setting | 3D step alone | Frame to 3D | Inference |
|---|---|---|---|
| Defaults (`campy-live` on all 12 cores) | 2.76–2.78 / 3.88–3.90 | 14.5 / 15.7 | 10.1 / 10.3 |
| `--cpus 4-7` (one cluster, off CPU 0) | 2.65–2.68 / 3.75–3.78 | 14.2–14.3 / 15.5–15.6 | 9.9 / 10.3 |
| `--cpus 4-7`, and the replay's feeder on 8–11 (`taskset -c 8-11`) | 2.55–2.57 / 3.66–3.73 | 14.1–14.2 / 15.4 | 10.0 / 10.3 |
| `OPENBLAS_NUM_THREADS=1` alone | 2.73–2.76 / 3.86 | 14.5 / 15.7 | 10.1 / 10.3 |
| All three | 2.49–2.54 / 3.63–3.70 | 14.1 / 15.4 | 10.0 / 10.2 |

- **Keeping `campy-live` on one cluster and what feeds it on another helps a little**: about 0.25 ms off the 3D step and 0.4 ms off frame to 3D at the median (about 3%), 0.3 ms at p99. Every set was tracked in every run, and the accuracy didn't change at all (2D median 3.8 px, 3D median 4.2 mm, 2 identity switches, in each of the ten).
- **numpy's thread count doesn't matter.** numpy's OpenBLAS starts 12 threads, but the 3D step's matrices are too small for it to use them: one thread gave the same times.
- **The desktop costs nothing measurable.** GNOME, X and the VNC server used 80–150 ms of CPU over each 70 s run, about 0.2% of one core, so turning the desktop off while tracking (which needs `sudo`) wasn't tried.
- **For a recording**, the same split would be `campy-live --cpus 4-7 --campy-cpus 8-11`, which moves `campy-acquire`, its camera processes and their encoders onto the third cluster when the feed comes. Not measured with cameras.
- The worst set of each run (the max, 21–31 ms frame to 3D) moved about as much from run to run as between settings.

**The GPU doesn't help, as the step is written.** The same 3,600 sets, run offline through `campy/pose3d.py`'s tracker on the Orin's CPU, reproduced the run's 3D exactly (the same missing points, within the log's float32 rounding), at 2.15 / 3.20 ms per set (p50 / p99), under the rig lock. That is 0.6 ms less than inside `campy-live`, where it shares Python with the thread running the models. Its time is spread over hundreds of small numpy operations on 3 × 5 × 12 points: undistorting the keypoints 0.50 ms a set, scoring each camera's detections against the animals 0.59, the solves 1.1. The arithmetic itself is tiny; most of the time is the cost of starting each operation. On the GPU, through PyTorch, starting one operation cost 25 µs of CPU time, and one with its result read back 77 µs. So each piece took longer there, timed with its result back on the CPU:

| One call | numpy on the CPU | PyTorch on the GPU, fp64 | PyTorch on the GPU, fp32 |
|---|---|---|---|
| Undistorting a set's keypoints (`_Normalize`) | 430 µs | 8,383 µs | 8,236 µs |
| The 3D solve of 5 animals × 12 nodes (`Triangulate`) | 314 µs | 1,767 µs | 1,627 µs |

The GPU results matched numpy's: identical points missing, and the 3D within 3 × 10⁻¹² mm in fp64 and 0.002 mm in fp32. Recording the solve as one CUDA graph, which would replace its launches with one, failed: the eigenvalue solver and the masking of missing points need the CPU partway through. And the tracker has to read results back at least four times a set: once per camera, to match its detections to the animals, and once for the 3D. At 77 µs each that is 0.3 ms before any work. To pay off on the GPU, the step would need its own fused kernels, not PyTorch's operations. Measured 12 November (2:40 AM, `RIG_CLEAN`), replaying the replay check's logged keypoints through the tracker.

**Fewer, larger numpy operations on the CPU did help** (pull request 420). Most of numpy's time on arrays this small is spent starting each operation, and it takes about three times as long over a strided or broadcast array as over a flat one. Since that pull request, the step undistorts the keypoints on flat arrays, with the lens constants made once per tracker; once all 5 animals are being followed, it scores every camera's detections in one call instead of one call per camera; and it computes every view's reprojection errors at once. Each number is computed with the same arithmetic, in the same order, as before, so the results are identical to the last bit: the 3,600 sets' 3D, the reprojection errors and the identities matched the old code's exactly, offline on the Orin, the EVO-X2 and a Mac. Times per set, p50 / p99, all `RIG_CLEAN` (12 November):

| | Before | After |
|---|---|---|
| Orin, inside `campy-live` (the replay check, two runs each, alternating) | 2.77 / 3.86–3.88 ms | **2.27–2.30 / 3.32–3.38 ms** |
| Orin, frame to 3D in the same runs | 14.5 / 15.7 ms | **14.0 / 15.1–15.3 ms** |
| Orin, offline (all 3,600 sets, four passes each) | 2.12 / 3.14 ms | **1.65 / 2.59 ms** |
| EVO-X2, offline (the same) | 0.58 / 0.85 ms | **0.44 / 0.67 ms** |

The replay's accuracy didn't change (2D median 3.8 px, 3D median 4.2 mm, 2 identity switches). Inside `campy-live` the step gains less than offline because it shares Python with the thread running the models. What is left is mostly numpy's eigenvalue solver (the 3D solve) and the undistortion's ten rounds; going further would take compiled code.

### Recording

campy records through ffmpeg, and **ffmpeg can't use the Orin's encoder**. Ubuntu's ffmpeg finds no NVENC driver for it. NVIDIA's own ffmpeg build for JetPack 7.2.1 (8.0.1, in its apt repository) has `hevc_nvenc`, but opening it on the Orin fails with `OpenEncodeSessionEx failed: unsupported device`: that route is for the Jetson Thor. On the Orin, NVIDIA's encoder is reached through GStreamer (`nvv4l2h265enc`, `nvv4l2h264enc`).

**campy's Jetson writer** (`gpuMake: "jetson"`, pull request 403) does that. Each camera's video is two processes: GStreamer's `gst-launch-1.0` takes the camera's gray frames, one write each as ffmpeg does on the other machines, and encodes them; ffmpeg copies the encoded stream, untouched, into the same fragmented MP4 campy writes everywhere else (hvc1 for H.265, a keyframe every `keyframeInterval`). A config for it:

```yaml
gpuID: 0                 # anything but -1
gpuMake: "jetson"
codec: "h265"            # or "h264"
quality: 21              # the QP of every frame, 0-51, as on NVENC
pixelFormatInput: "gray" # mono frames only
keyframeInterval: 0.5
ffmpegPath: "/usr/bin/ffmpeg"
```

The pipeline, for one camera:

```
gst-launch-1.0 fdsrc ! rawvideoparse format=gray8 width=1280 height=1024 framerate=60/1
  ! nvvidconv ! video/x-raw(memory:NVMM),format=I420
  ! nvv4l2h265enc control-rate=variable_bitrate bitrate=4000000000 qp-range=21,21:21,21:21,21
      preset-level=1 insert-sps-pps=1 iframeinterval=30 idrinterval=30
  ! h265parse ! fdsink --pipe--> ffmpeg -f hevc -framerate 60 -r 60 -i pipe:0 -c copy
      -bsf:v hevc_metadata=video_full_range_flag=1 (campy's MP4 options) 0.mp4
```

What it had to get right, each found on the Orin (JetPack 7.2.1):

- **Gray frames go straight in.** `nvvidconv`, on the Orin's video converter rather than the CPU, turns them into I420 in the encoder's memory with the gray levels unchanged: a ramp of all 256 levels came back as 0–255. It refuses gray to NV12 ("Transform not supported"), and the route the encoder benchmark took, I420 built by the sender and then converted to NV12, clipped the levels to 16–235 (a ramp came back as 15–236). GStreamer's `videoconvert` would do it on the CPU, at 85% of a core per camera.
- **Constant QP through `qp-range`.** The encoder's own switch for it (`ratecontrol-enable=0` with `quant-i-frames` and `quant-p-frames`) is ignored on JetPack 7.2.1: QP 15, 25 and 35 gave the same file, at the encoder's default 4 Mbit/s, with every preset. So campy leaves rate control on (variable bitrate, a 4 Gbit/s ceiling) and sets the range of QPs to the one `quality`. The file size then depended only on the QP, the same with ceilings from 200 Mbit/s to 4 Gbit/s. `peak-bitrate` stays at its default: set equal to the ceiling, every file came out at the ceiling whatever the QP.
- **Marked full range.** The video holds the camera's own levels, 0–255, but the encoder marks it limited range (its `extended-colorformat` property didn't change that), and a reader would then stretch the contrast. ffmpeg marks it full range as it writes the file (`hevc_metadata`, `h264_metadata`), without touching the frames. This writer is always full range, whatever `colorRange` says.
- **Frame times from the frame rate.** The encoded stream carries none, and with `-framerate` alone ffmpeg gave the frames times that stopped advancing: a 4 s video said 0.2 s. `-r` fixes it (4.000 s). With `ffmpegLogLevel: "warning"` ffmpeg 6.1 still says `Timestamps are unset in a packet` once per camera: harmless, the times in the file are right.
- **NVIDIA's messages aren't shown.** Its libraries print a few lines as every encoder opens (`NvMMLiteOpen`, `Opening in BLOCKING MODE`, on stdout, which is why the stream has a pipe of its own). campy keeps `gst-launch-1.0`'s other lines: when it stops, its error is in what campy says (`gst-launch-1.0 said: ERROR: from element ...`).

Not supported: AV1 (`nvv4l2av1enc` runs on the Orin, but ignores the QP range: the same file at every QP), colour frames (`nvvidconv` refuses RGB on the converter, "use GPU instead", and the GPU tracks), and `denoise` (an ffmpeg filter, and ffmpeg no longer sees the frames). campy refuses those settings at the launch check. H.264 stays in NVIDIA's default profile, Baseline: High made files 11% smaller at QP 21, but 8 cameras then reached 53 fps each instead of 87.

**Checked on the Orin** (campy dev `c733a7e` plus pull request 403, every run `RIG_CLEAN`):

- 600 synthetic 1280 × 1024 frames through campy's writer, at QP 15, 21 and 28 in each codec: every frame, in order, 10.0 s long at 60 fps, a keyframe every 0.5 s, marked full range, all 256 gray levels back (within 0–2). H.265 at QP 21: 36 Mbit/s, 40.0 dB PSNR against what was sent, 34.0 dB had the video stayed marked limited range. H.264 at QP 21: 62 Mbit/s, 40.6 dB. QP 28: about 1 Mbit/s in either (the sensor noise is gone).
- The rig check with live tracking (smoke3 in the table above): 3 emulated cameras recorded for 60 s in H.265 at quality 21 while `campy-live` tracked them in 3D. Each video holds all 3,600 frames (60.00 s, 4–5 Mbit/s), and against the clips the cameras played they scored 46.3–47.8 dB (35.6–36.4 dB one frame off, so no frame is out of place; 27–28 dB had they been marked limited range).
- CPU, 3 cameras at 60 fps recorded for 40 s: `gst-launch-1.0` took 20% of a core per camera and the muxing ffmpeg under 3%, against 73% per camera for libx264 `ultrafast`.
- The launch check: `OK encoders nvv4l2h265enc/ultrafast (...) encoded 10 test frames with campy's settings, through GStreamer`, then capacity and disk from the sweep below.

**How many cameras** (`campy-encode-sweep --encoder jetson`, 1280 × 1024 mono at 60 fps, a keyframe every 0.5 s, the sweep's noisy synthetic frames; a camera count passes at 75 fps each, 60 with 25% to spare):

| Codec, quality | Bitrate per camera | 6 cameras | 7 cameras | 8 cameras | 9 cameras | Keeps up with |
|---|---|---|---|---|---|---|
| H.264, 21 | 63 Mbit/s | 116 fps | 99 fps | 87 fps | 35 fps | **8** |
| H.265, 21 | 37 Mbit/s | 96 fps | 82 fps | 72 fps | 36 fps | **7** |
| H.265, 25 | 6 Mbit/s | | 109 fps | 95 fps | | **8** |

The encoder's speed falls as the bitrate rises: about 910 megapixels a second in all for H.264 at quality 21, 760 for H.265 at 21 and 1,000 for H.265 at 25 (the sweep's frames are noisier than a cage: the rig check's recorded mouse clips came to 4–5 Mbit/s at quality 21). One camera alone reached 340 fps. **8 is the most at once**, whatever the speed: the Orin's video converter and encoder have memory for 8. With 9 open the kernel logs `all memory contexts are busy`, the encoders take turns (35 fps each), and `gst-launch-1.0` sometimes crashed (`Caught SIGSEGV`), even with 9 small, slow streams. So the launch check FAILs more than 8 Jetson cameras. The capacity table is `~/.campy/capacity.json`, as on every machine.

**The CPU path**, for comparison (`campy-encode-sweep`, the same frames, H.264 quality 21): libx264 preset `ultrafast` kept up with 6 cameras or more (the most tried), `veryfast` with 1. At campy's default CPU preset (`fast`) a recording falls behind with more than one camera, so a config for the CPU sets `gpuID: -1` and `preset: "ultrafast"`.

**What the encoder benchmark measured** (`bench/jetson_encode_bench.py`, before the writer and before pull request 404 made the benchmark run the writer's pipeline: H.265 asked for at constant QP 21 with the properties that turned out to be ignored, so in fact at the encoder's default 4 Mbit/s, which its files came to; a keyframe every second, 30 s per run):

| Cameras | Frame rate each | Kept up? |
|---|---|---|
| 3, 6 | 60 fps | yes: every frame, 1.4–1.6 ms behind the clock at the end, about 25–30% of a core per encoder process |
| 6, 8 | 75 fps (60 + 25% headroom) | yes: every frame, at most 2.4 ms behind |
| 9 | 60 fps | yes (write p99 26 ms) |
| 9 | 75 fps | no: 68 fps |
| 10, 11, 12 | 60 fps | no: 25, 19 and 16 fps each |

At 4 Mbit/s the encoder handled about 600 frames/s of 1280 × 1024; campy's writer, at a constant QP, gets 580–760 depending on the bitrate. Its clock isn't held by `jetson_clocks`: it idles at 115 MHz and runs at up to 998 MHz under load. Two runs: the first (3, 6, 9 and 12 cameras at 60 fps, the GRAY8 conversion, and the CPU sweep above) ended `RIG_CONTAMINATED`, because Ubuntu's backup monitor started by itself and touched the GPU for 10 s at the start of the 6-camera run, which still kept up; the second (6, 8 and 9 cameras at 75 fps, 10 and 11 at 60), after it was turned off (step 8 below), was `RIG_CLEAN`. The benchmark also measured PSNR "as stored" at 41.8 dB and "by the label" at 31.7: its synthetic frames stayed within 16–235, so the clipping above didn't show there.

### Cameras

A FLIR Blackfly S BFS-U3-28S5C (colour) on the developer kit's USB 3 port was found by Spinnaker 4.4.0.246 at SuperSpeed.

**A real camera, recorded and tracked** (11 November, campy dev `5b3aefb`, started from campy's panel on the Orin's desktop, `RIG_CLEAN`): that camera, sending Mono8 at 1280 × 960 and 60 fps, free-running (no trigger board), exposure 7 ms, gain 32 dB; recorded on the Orin's encoder (`gpuMake: "jetson"`, H.265 quality 21) while `campy-live` tracked it in 2D with the trained 5-mouse pair, for 2 minutes.

| | |
|---|---|
| Frames | 7,200 of 7,200, none dropped; the camera's frame numbers without a gap, 16.666–16.668 ms apart |
| The video | 7,200 frames, 120.000 s, H.265 (hvc1) marked full range, 78 Mbit/s |
| `campy-live` | 7,195 sets of 7,200 (5 skipped as the feed started), inference p50 / p99 4.8 / 6.6 ms, latency from the frame 5.6 / 7.5 ms (max 8.6) |

It was a check of the chain, not of the tracking: the room was dark and the lens out of focus, with nothing to track, so the gain is high and the detections (about 2 a frame, from noise) mean nothing. The 78 Mbit/s is that gain's noise, which quality 21 keeps: the rig checks' clips of a lit cage came to 4–5 Mbit/s. The computer's own screen was shared over VNC throughout (step 9 below).

This colour camera tops out below 60 fps at larger frames when it sends Mono8: it makes the gray frames in its own image processor, which handles about 117 million pixels a second of full-width rows (1,936 pixels), whatever width campy asks for. So the number of rows sets the top frame rate: 42.4 fps at 1,440 rows, 58.4 at 1,024 and 62.0 at 960, with the exposure making no difference (7–15 ms). campy's launch check read these from the camera and refused 60 fps at 1,440 and 1,024 rows (`FAIL camera ... can run at only 58.4 fps with campy's settings`), and recorded at 960 with a warning that 60 is close to the camera's limit. A mono Blackfly S has no such step.

## Setting it up

What was done on 10–11 November 2026, in order. Steps with `sudo` are run at the Orin, or over `ssh -t` (sudo needs a terminal for its password).

### 1. JetPack 7.2.1, not 7.1

Check the release first: `head -1 /etc/nv_tegra_release`. **R38.x (JetPack 7.1) doesn't support the Orin family**, though it installs and boots: the display works, but nothing drives the GPU (the device tree's `gpu@17000000` has no driver, `tegrastats` shows no `GR3D` field, and CUDA finds no device). This kit had JetPack 7.1 flashed for the Jetson AGX Thor. JetPack 6 doesn't do either: its Python is 3.10, and sleap-nn needs 3.11 or newer. **JetPack 7.2 (Jetson Linux 39.2)** is the first JetPack 7 for the Orin.

Flash it from an x86-64 Ubuntu 22.04 or 24.04 PC over the USB-C port beside the 40-pin header (the one by the power jack doesn't flash). Put the Orin in recovery mode: with it on, hold the middle button, press and release the right one (reset), and let go of the middle one after 2 s; the PC's `lsusb` then lists `0955:7023 NVIDIA Corp. APX`. Then either:

- **NVIDIA SDK Manager**, which needs an NVIDIA developer login: target "Jetson AGX Orin (64GB developer kit version)", JetPack 7.2.1, with Host Machine unticked.
- **NVIDIA's public files and flash script**, with no login. This is what was done, from the GMKtec EVO-X2, which already had every host package the flash needs (on another PC, run `sudo ./tools/l4t_flash_prerequisites.sh` in `Linux_for_Tegra` first). From [Jetson Linux 39.2.1](https://developer.nvidia.com/embedded/jetpack/downloads/archive-7.2.1), download `Jetson_Linux_R39.2.1_aarch64.tbz2` (1.3 GB) and `Tegra_Linux_Sample-Root-Filesystem_R39.2.1_aarch64.tbz2` (2.0 GB) into one folder, then:

  ```
  sudo tar xpf Jetson_Linux_R39.2.1_aarch64.tbz2
  sudo tar xpf Tegra_Linux_Sample-Root-Filesystem_R39.2.1_aarch64.tbz2 -C Linux_for_Tegra/rootfs
  cd Linux_for_Tegra
  sudo ./apply_binaries.sh
  sudo ./tools/l4t_create_default_user.sh -u <user> -p <password> -n <hostname> -a --accept-license
  sudo ADDITIONAL_DTB_OVERLAY_OPT="BootOrderNvme.dtbo" ./tools/kernel_flash/l4t_initrd_flash.sh \
    --external-device nvme0n1p1 -c ./tools/kernel_flash/flash_l4t_t234_nvme.xml \
    --showlogs jetson-agx-orin-devkit external
  ```

  That is NVIDIA's command for an AGX Orin booting from an NVMe drive (`tools/kernel_flash/README_initrd_flash.txt`). It writes the boot firmware and the NVMe drive, which it erases; it took 12 minutes. `-a` logs the user in automatically, so a monitor plugged in later shows the desktop. While it flashes, the Orin appears on the PC as a USB network adapter, and Ubuntu may report that "Activation of network connection failed": that is harmless.

Connect the Orin to the network by Ethernet (Wi-Fi settings don't survive a flash). Then install CUDA, cuDNN, TensorRT and NVIDIA's GStreamer plugins:

```
sudo apt update && sudo apt install nvidia-jetpack
```

This brought CUDA 13.2.2, cuDNN 9.20, TensorRT 10.16.2 and VPI 4.1, 161 packages. Right after it, NVIDIA's one-time Docker install service (`nv-install-docker`) holds the package lock for a few minutes: an `apt` or `apt-mark` started then fails with "dpkg frontend lock was locked". Wait for `systemctl is-active nv-install-docker` to say `inactive`.

### 2. The video disk

This kit boots from a 2 TB NVMe drive, so recordings go on the system disk; its 64 GB eMMC is too small for video, and is unused. The kit has one M.2 Key-M slot, which the boot drive takes, so keeping recordings off the system disk, as on the other rigs, needs a USB 3 drive.

### 3. Full power, clocks held

```
sudo nvpmodel -m 0
```

selects MAXN (all 12 cores; the kit came up in the 30 W mode with 4 cores off). It asks to reboot: type `YES`. Given no answer, as from a script, it changes nothing. The mode stays set from then on. `sudo jetson_clocks --fan` holds the CPU, GPU and memory clocks at their maximum, and the fan at full speed, until the next boot; a small systemd unit runs it at every boot, as `/etc/systemd/system/jetson-clocks.service`:

```
[Unit]
Description=Hold Jetson clocks at maximum (jetson_clocks --fan)
After=nvpmodel.service

[Service]
Type=oneshot
ExecStart=/usr/bin/jetson_clocks --fan
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

then `sudo systemctl enable jetson-clocks.service`. It logs one harmless `write error: Invalid argument` (a fan node that doesn't take a value). The video encoder's clock is not held.

### 4. USB for the cameras

In `/boot/extlinux/extlinux.conf`, add `usbcore.usbfs_memory_mb=1000 usbcore.autosuspend=-1` to the end of the `APPEND` line (keep a copy of the file first), then reboot. The first gives the FLIR cameras the USB buffer memory they need (the default 16 MB drops frames); the second stops Linux from suspending the cameras and the trigger. `cat /sys/module/usbcore/parameters/usbfs_memory_mb` then says 1000. For the trigger's serial port: `sudo usermod -aG dialout $USER`, then log out and in.

### 5. Tools

```
sudo apt install python3-venv python3-dev python3-pip python3-pyqt5 ffmpeg \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
sudo apt-mark hold ffmpeg
```

`apt-mark hold` keeps Ubuntu's ffmpeg 6.1, which has libx264 and libx265. NVIDIA's repository has a newer ffmpeg (8.0.1: a minimal build without them, whose NVENC encoders don't work on the Orin) that an upgrade would otherwise put in its place. campy's own recordings use imageio-ffmpeg's ffmpeg, from pip, unless `ffmpegPath` says otherwise.

### 6. FLIR Spinnaker

Spinnaker 4.4.0.246 for Arm64 / Ubuntu 24.04 (`spinnaker-4.4.0.246-noble-arm64-pkg.tar.gz`) and its Python wheel for Python 3.12 (`spinnaker_python-4.4.0.246-cp312-cp312-linux_aarch64.tar.gz`), from Teledyne FLIR's site (it needs their login). The wheel must match Spinnaker's version exactly. First `sudo apt install libusb-1.0-0 qtbase5-dev qtchooser qt5-qmake qtbase5-dev-tools` (SpinView's Qt libraries).

FLIR's `install_spinnaker_arm.sh` shows its license in a text box that, over SSH, may not take the keyboard (`^[[C` printed for the arrow keys, Enter doing nothing). Installing its packages in the same order with Debian's plain-text prompts avoids that:

```
cd spinnaker-4.4.0.246-noble-arm64
for p in libgentl libspinnaker libspinnaker-dev libspinnaker-c libspinnaker-c-dev libspinvideo libspinvideo-dev \
         libspinvideo-c libspinvideo-c-dev spinview-qt spinview-qt-dev spinupdate spinupdate-dev spinnaker spinnaker-doc; do
  sudo env DEBIAN_FRONTEND=readline dpkg -i ${p}_4.4.0.246_arm64.deb
done
sudo sh configure_spinnaker.sh     # the udev rule: FLIR cameras belong to the flirimaging group; add your user to it
```

Leave out FLIR's USB-FS step (`configure_usbfs.sh`, through `/etc/rc.local`): step 4 sets it. Log out and in (or reboot) for the `flirimaging` group to apply.

### 7. campy and its two environments

campy is a git checkout, installed `-e` into two venvs made with the system Python 3.12 and `--system-site-packages`, so they see Ubuntu's PyQt5 (PyPI has no PyQt5 wheel for Arm64, and `pip install -e` would try to build it and fail). The catch with `--system-site-packages`: Ubuntu's matplotlib, pandas, Pillow and their helpers were built for numpy 1.x, and with pip's numpy 2.x they fail to import (`numpy.core.multiarray failed to import`, as soon as sleap-nn imports matplotlib). Installing PyPI's builds into each venv puts them ahead of Ubuntu's:

```
git clone https://github.com/olinesn/campy ~/campy && cd ~/campy && git checkout dev
python3 -m venv --system-site-packages ~/campy/.venv
~/campy/.venv/bin/pip install -e ~/campy
~/campy/.venv/bin/pip install spinnaker_python-4.4.0.246-cp312-cp312-linux_aarch64.whl
~/campy/.venv/bin/pip install --ignore-installed matplotlib contourpy kiwisolver pandas pillow fonttools pyparsing cycler python-dateutil pytz six packaging

python3 -m venv --system-site-packages ~/venvs/sleap
~/venvs/sleap/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cu132
~/venvs/sleap/bin/pip install sleap-nn==0.3.5 "torch==2.15.0" "torchvision==0.29.2" --extra-index-url https://download.pytorch.org/whl/cu132
~/venvs/sleap/bin/pip install -e ~/campy onnx
~/venvs/sleap/bin/pip install --ignore-installed matplotlib contourpy kiwisolver pandas pillow fonttools pyparsing cycler python-dateutil pytz six packaging
```

PyTorch's own CUDA 13.2 wheels for Arm64 work on the Orin: they carry no kernels built for its compute capability 8.7, but their 8.0 kernels run on it. Pinning torch when installing sleap-nn keeps pip from replacing it. `onnx` is what campy's TensorRT engines need besides `tensorrt`, which the venv sees from JetPack's system Python packages.

### 8. The desktop and the rig lock

With the developer kit's GNOME desktop (an X11 session, logged in automatically), a few desktop programs keep the GPU open: the desktop portals, GNOME Software's background service and, until it is done, the first-login wizard. `rig.py` counts them as the desktop (noted, not waited for) since pull request 401; before that it waited on them for ever, and so did its own tests. On a rig with no screen they do nothing useful, so they were also turned off for the user: `echo yes > ~/.config/gnome-initial-setup-done`, and autostart overrides with `Hidden=true` in `~/.config/autostart/` for `org.gnome.Software.desktop` and for the backup monitor's entry, which started by itself during a timing test and used the GPU for 10 seconds. On the Orin the lock watches `/dev/nvgpu/*` and `/dev/nvhost-*gpu` for CUDA, and `/dev/v4l2-nvenc` for the encoder (pull request 401).

### 9. Its desktop from another computer

The kit has no monitor, and recordings start from campy's panel on the computer's own desktop ([Recording a session from the desktop](recording-a-session.md)), so its desktop is shared over VNC, through SSH:

- **x11vnc** (Ubuntu's package) as a user service, on `127.0.0.1:5900` only, with a VNC password (`x11vnc -storepasswd`; only its first 8 characters count). From a Mac: `ssh -N -o ServerAliveInterval=15 -L 5901:localhost:5900 <the Orin>`, then `open vnc://localhost:5901` (Screen Sharing). Through SSH it works from anywhere SSH does, VPN included.
- **A monitor that isn't there.** With nothing on its DisplayPort, X runs on a 640 × 480 screen with no monitor, and GNOME draws nothing: VNC showed a black screen with a mouse pointer. Enlarging the screen (`xrandr --fb`), a RandR monitor of its own (`xrandr --setmonitor`) and NVIDIA's documented headless setting (`Virtual 1920 1080` in a `Screen` section of `/etc/X11/xorg.conf`) left GNOME with no monitor, and the kernel has no virtual display driver (`CONFIG_DRM_VKMS` is off). What works: telling NVIDIA's X driver that the DisplayPort has a monitor, with that monitor's description (EDID) in a file, in the `Device` section of `/etc/X11/xorg.conf`:

  ```
  Option "ConnectedMonitor" "DFP-0"
  Option "CustomEDID" "DFP-0:/etc/X11/edid-1920x1080.bin"
  ```

  The EDID is a standard one for 1920 × 1080 at 60 Hz (CEA timing, 148.5 MHz). After `sudo systemctl restart gdm`, `xrandr` shows `DP-0 connected primary 1920x1080` and GNOME draws its desktop. A real monitor plugged into that port would get the same mode.
- **Not Sunshine/Moonlight**, as on the EVO-X2: Sunshine passes the mouse and keyboard through `uinput`, and NVIDIA's kernel for the Orin (6.8.12-tegra) is built without it (`CONFIG_INPUT_UINPUT` is not set), so it would be view-only.
- **Firefox** for the panel: Ubuntu's `firefox` package is only a stub for the snap, and snaps have known problems under NVIDIA's kernels. Mozilla's own package (`packages.mozilla.org`, arm64 included, its key checked against fingerprint `35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3`) is pinned above it.

## Checking the setup: `bench/jetson_setup_check.py`

A read-only checklist. It prints PASS, WARN or FAIL for each item, with a one-line fix for anything that isn't right, and changes nothing: it only reads files and runs commands that report.

```
python3 bench/jetson_setup_check.py
```

It checks, in order: the Jetson Linux version (`/etc/nv_tegra_release`, R39.2 wanted) and the `nvidia-jetpack` packages; the power mode (MAXN, mode 0) and whether `jetson_clocks` is holding the clocks; `usbfs_memory_mb` (1000 or more) and USB autosuspend (-1); the `cdc_acm` serial driver (loaded, or in the kernel's config) and a `/dev/ttyACM*` port you can open; GStreamer's `nvv4l2h265enc`, `nvvidconv`, `rawvideoparse` and `h265parse`, and the encoder properties the benchmark sets; ffmpeg, and its `libx264` and `libx265`; FLIR cameras on USB (`lsusb`); PySpin in the campy venv, Spinnaker's version and how many cameras it sees; campy and PyQt5 in the campy venv; torch with CUDA working and sleap-nn in the sleap venv; and the video disk: that `/data` (or the folder `--video-dir` names) exists, has room, and is not on the system disk.

On our kit the video disk item FAILs, as expected: it boots from its only NVMe slot, so there is no `/data` and recordings go on the system disk ([step 2](#2-the-video-disk)). With the defaults the check says `FAIL video disk: no /data`. With `--video-dir` set to the folder recordings use, it gives the free space (1,817 GB of 1,966 GB), then FAILs because that folder is on the system disk. Either FAIL clears once a USB 3 drive is mounted at `/data`. Both ways, on 11 November (JetPack 7.2.1, `--no-cameras`, under the rig lock): 17 items passed, the camera count was skipped (`--no-cameras`), and one WARN said one camera was plugged in, not 3.

Options: `--campy-venv` (default `~/campy/.venv`), `--sleap-venv` (default `~/venvs/sleap`), `--video-dir` (default `/data`; where recordings go), `--min-free-gb` (default 200), `--cameras` (default 3), `--json` to keep the results. Counting cameras opens Spinnaker, so pass `--no-cameras` while a recording is running. It exits 1 if anything failed. Needs only Python's standard library, so it runs with the system `python3`.

## The encoder benchmark: `bench/jetson_encode_bench.py`

Starts one encoder per simulated camera (default 3) and feeds each synthetic 1280×1024 mono frames (bright blobs moving over a noisy gradient, with a ramp of all 256 gray levels across the top 16 rows), paced at 60 fps, the way campy's writer feeds its encoder: one pipe per camera, one frame per write, and a write waits when the encoder falls behind. Since pull request 404 each stream is what campy's writer runs for a `gpuMake: "jetson"` camera ([Recording](#recording)), at the same settings:

```
gst-launch-1.0 fdsrc ! rawvideoparse format=gray8 width=1280 height=1024 framerate=60/1 plane-strides=<1280>
  ! nvvidconv ! video/x-raw(memory:NVMM),format=I420
  ! nvv4l2h265enc control-rate=variable_bitrate bitrate=4000000000 qp-range=21,21:21,21:21,21
      preset-level=1 insert-sps-pps=1 iframeinterval=60 idrinterval=60
  ! h265parse ! fdsink   →   ffmpeg -f hevc -framerate 60 -r 60 -i pipe:0 -c copy
      -bsf:v hevc_metadata=video_full_range_flag=1 <campy's fragmented-MP4 flags> -tag:v hvc1 streamN.mp4
```

so the files are laid out and marked like the ones campy writes, and the file size follows `--qp`. Before, the benchmark asked for its QP with `ratecontrol-enable=0` and `quant-i-frames`/`quant-p-frames`, which JetPack 7.2.1 ignores (every QP came out at the encoder's default 4 Mbit/s), and sent I420 that `nvvidconv` turned into NV12, which clips the gray levels to 16–235. A test checks that its pipeline and muxer options are the writer's. `--input gray8-convert` adds GStreamer's `videoconvert` (gray to I420 on the CPU) before `nvvidconv`, to see what a CPU conversion costs. The property names are the ones in NVIDIA's Jetson Linux r39.2 Accelerated GStreamer guide, and the script checks them with `gst-inspect-1.0` before it starts.

```
python3 bench/jetson_encode_bench.py                    # 3 streams x 60 fps for 30 s, QP 21, keyframe every second
python3 bench/jetson_encode_bench.py --qp 28            # the same at QP 28: a smaller file
python3 bench/jetson_encode_bench.py --streams 8 --fps 75   # 8 cameras at 60 fps with campy-encode-sweep's 25% headroom
python3 bench/jetson_encode_bench.py --seconds 1800     # a 30-minute run
python3 bench/jetson_encode_bench.py --mux raw          # write .h265 files directly, without the ffmpeg step
python3 bench/jetson_encode_bench.py --cpu-fallback libx264 --cpu-preset ultrafast    # the same on the CPU
python3 bench/jetson_encode_bench.py --dry-run          # print the commands only
```

For each stream it reports the frames written, the frame rate achieved, how long each pipe write took (p50, p99, max), how far the writes fell behind the 60 fps clock (the most, and over the last second: a lag that keeps growing means the encoder can't keep up, and a real recording's write queue would grow), the CPU used by the `gst-launch-1.0` and `ffmpeg` processes (100% = one core), the file size and bitrate, the frames counted back in the file, and, for the first stream, the PSNR of its first 300 frames against what was sent and the lowest and highest gray level its ramp came back as in the first frame. Both are measured two ways: as stored, and by the video's range. The ramp reads 0 and 255 when the levels survive, about 16 and 235 when something on the way clipped them; read by the range, it also shows whether the video is marked full range. For the whole machine: CPU busy (mean, and the highest 1-second sample), the script's own CPU, and every `tegrastats` line during the run (encoder clock, power, temperatures). Everything goes to a JSON file (`--json`, otherwise a timestamped file in the current folder).

The videos go to a new folder under `/data` if there is one, else in the current folder (`--out-dir` to choose; it should be the disk recordings will use), and are deleted at the end unless `--keep`. Other options: `--streams`, `--fps`, `--seconds`, `--qp` (0–51, campy's `quality`), `--keyframe-interval` (seconds, like campy's `keyframeInterval`), `--preset-level` (1, campy's `"ultrafast"`, by default), `--maxperf` (adds `maxperf-enable=1`, a property older JetPacks list), `--cpu-preset`, `--psnr-frames` (0 skips the PSNR). Needs numpy (the campy venv has it). Run it through [`bench/rig.py`](development.md#timing-tests-on-a-shared-machine-benchrigpy), like every timing test.

**Checked on the Orin** after the change (JetPack 7.2.1, `RIG_CLEAN`, one stream at 60 fps for 5 s, a keyframe every 0.5 s):

| `--qp` | Bitrate | PSNR against what was sent | Ramp back (sent 0–255) |
|---|---|---|---|
| 15 | 68.5 Mbit/s | 45.3 dB | 0–255 |
| 21 | 7.6 Mbit/s | 42.1 dB | 0–255 |
| 28 | 0.26 Mbit/s | 41.9 dB | 0–255 |

Each file held all 300 frames, said 5.000 s and was marked full range, so the PSNR and the ramp read the same as stored and by the range. The frames' sensor-like noise (2 gray levels, standard deviation) alone puts a video that drops it at about 42 dB: at QP 21 and 28 the encoder drops most of it, so their PSNRs sit near 42 dB while the file shrinks 30-fold, and QP 15 keeps some of it. These frames are calmer than `campy-encode-sweep`'s, so the bitrates are lower than its (37 Mbit/s at QP 21). 8 streams at 75 fps (60 with 25% to spare) at QP 21 for 10 s kept up: every frame, at most 1.6 ms behind the clock at the end, 9.4 Mbit/s and 25–29% of a core per encoder.

## Updating it with the other machines

At each release, as on the EVO-X2: in `~/campy`, `git fetch` and `git merge --ff-only` to the tag or to `origin/dev` (the Orin reaches GitHub itself); `pip install -e ~/campy` again in both venvs (new entry points, the version); `python3 ~/campy/bench/rig.py install` when `rig.py` changed; then the rig checks under the lock.

## Still to do

- A recording with real cameras of a lit cage with mice, several cameras, triggered, to check the tracking as well as the timing (one camera of a dark room is in [Cameras](#cameras)).
- How far TensorRT's fp16 moves the keypoints from fp32 here, as measured on the other NVIDIA cards.
