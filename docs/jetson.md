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

The replay tracked as the other machines do: against the offline SLEAP predictions, 2D median 3.8 px, 3D median 4.2 mm, 99.93% the same identity, 2 identity switches (the EVO-X2: 3.8 px, 4.2 mm, 2 switches). The EVO-X2 takes about half the time (inference 5.5 / 5.6 ms in the same replay). The 3D step alone takes 2.8 ms here against about 1.1 ms there: it runs on the CPU. While smoke3 ran, the CPU was about half busy, the three libx264 encoders included. Recorded on the Orin's encoder instead, tracking took the same time; the CPU was about 38% busy at mid-run (one sample, `top`), most of it the emulated cameras decoding their clips.

Since 11 November the rig check records on the Orin's encoder, as a recording on the Orin would: smoke1 and smoke3 use `gpuMake: "jetson"`, H.265 at quality 21 (`ENC=cpu` still runs the CPU path, libx264 `ultrafast`). Its first run, the last three rows above, came after the Orin's campy moved to dev `5b3aefb` (the Jetson writer, pull request 403): the replay tracked as before (2D median 3.8 px, 3D median 4.2 mm, 2 identity switches), each recorded video held every frame (4.1–5.0 Mbit/s per camera in smoke3), and the CPU was about 39% busy at smoke3's mid-run (one sample).

Not checked: how far TensorRT's fp16 moves the keypoints from fp32 on the Orin (the replay's accuracy above already includes it), and a real camera feeding `campy-live`.

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

A FLIR Blackfly S BFS-U3-28S5C (colour) on the developer kit's USB 3 port was found by Spinnaker 4.4.0.246 at SuperSpeed. No recording with it has been timed yet.

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

- A recording with real cameras, timed, with `campy-live` tracking them.
- How far TensorRT's fp16 moves the keypoints from fp32 here, as measured on the other NVIDIA cards.
