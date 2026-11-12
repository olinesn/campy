# Jetson AGX Orin

An **NVIDIA Jetson AGX Orin 64GB Developer Kit** joined the test machines on 11 November 2026, next to the GMKtec EVO-X2, the Dell Precision 7875, the Dell Pro Max Slim FCS1250, the Dell Pro Tower Plus and the Windows 11 virtual machine. It is a small stand-alone rig: 12 Arm Cortex-A78AE cores, an Ampere GPU (compute capability 8.7) that shares the 64 GB of memory, and a video encoder of its own. This page is how it was set up, what works, what doesn't yet, and what it measured.

**In short:**

- **Live tracking works.** With campy's defaults (`campy-live --engine auto` picks TensorRT here), the trained 5-mouse pair tracks 3 cameras at 60 fps with 3D, every set, with the same accuracy as the other machines, in about twice the EVO-X2's time per set.
- **Recording works, on the CPU.** campy can't drive the Orin's hardware encoder yet. Its CPU path (`gpuID: -1`, libx264 preset `ultrafast`) kept up with 3 emulated cameras at 60 fps while they were tracked, every frame recorded.
- **The hardware encoder works through GStreamer** and keeps up with 8 cameras at 60 fps (1280 × 1024 mono, with 25% headroom). ffmpeg can't use it on the Orin, so campy would need a GStreamer writer for it.
- **FLIR cameras work**: Spinnaker 4.4 has an Arm64 build, and a Blackfly S on the Orin's USB 3 port is found at SuperSpeed.
- **It needs JetPack 7.2 or newer.** JetPack 7.1 installs on an AGX Orin but leaves its GPU without a driver.

## What it measured (11 November 2026)

Every number here: JetPack 7.2.1 (Jetson Linux 39.2.1), power mode MAXN, clocks held by `jetson_clocks` (CPU 2.2 GHz, GPU 1.3 GHz), fan at full speed, under the rig lock ([`bench/rig.py`](development.md#timing-tests-on-a-shared-machine-benchrigpy)), each run `RIG_CLEAN` unless said otherwise. PyTorch 2.15.0+cu132, sleap-nn 0.3.5, TensorRT 10.16.2, campy dev `6cdc979`. No monitor was attached.

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

The replay tracked as the other machines do: against the offline SLEAP predictions, 2D median 3.8 px, 3D median 4.2 mm, 99.93% the same identity, 2 identity switches (the EVO-X2: 3.8 px, 4.2 mm, 2 switches). The EVO-X2 takes about half the time (inference 5.5 / 5.6 ms in the same replay). The 3D step alone takes 2.8 ms here against about 1.1 ms there: it runs on the CPU. While smoke3 ran, the CPU was about half busy, the three libx264 encoders included.

Not checked: how far TensorRT's fp16 moves the keypoints from fp32 on the Orin (the replay's accuracy above already includes it), and a real camera feeding `campy-live`.

### Recording

campy records through ffmpeg, and **ffmpeg can't use the Orin's encoder**. Ubuntu's ffmpeg finds no NVENC driver for it. NVIDIA's own ffmpeg build for JetPack 7.2.1 (8.0.1, in its apt repository) has `hevc_nvenc`, but opening it on the Orin fails with `OpenEncodeSessionEx failed: unsupported device`: that route is for the Jetson Thor. On the Orin, NVIDIA's encoder is reached through GStreamer (`nvv4l2h265enc`, `nvv4l2h264enc`, `nvv4l2av1enc`).

**campy's CPU encoder** (`campy-encode-sweep`, 1280 × 1024 mono at 60 fps with its 25% headroom, H.264 quality 21): libx264 preset `ultrafast` kept up with 6 cameras or more (the most tried), `veryfast` with 1. `campy-acquire`'s launch check reads this from `~/.campy/capacity.json`. At campy's default CPU preset (`fast`) a recording would fall behind with more than one camera, so an Orin config sets `gpuID: -1` and `preset: "ultrafast"`.

**The hardware encoder through GStreamer** (`bench/jetson_encode_bench.py`, 1280 × 1024 mono, H.265 at constant QP 21, a keyframe every second, 30 s per run, one pipe per camera as campy's writer uses):

| Cameras | Frame rate each | Kept up? |
|---|---|---|
| 3, 6 | 60 fps | yes: every frame, 1.4–1.6 ms behind the clock at the end, about 25–30% of a core per encoder process |
| 6, 8 | 75 fps (60 + 25% headroom) | yes: every frame, at most 2.4 ms behind |
| 9 | 60 fps | yes (write p99 26 ms) |
| 9 | 75 fps | no: 68 fps |
| 10, 11, 12 | 60 fps | no: 25, 19 and 16 fps each |

So the encoder handles about 600 frames/s of 1280 × 1024 in all, with no more than 9 encoders at once: past that it doesn't slow down evenly, it falls to a fraction of its speed. Its clock isn't held by `jetson_clocks`: it idles at 115 MHz and runs at up to 998 MHz under load. The benchmark's synthetic frames came to 4.1 Mbit/s per camera at 60 fps. Two runs: the first (3, 6, 9 and 12 cameras at 60 fps, the GRAY8 conversion below, and the CPU sweep above) ended `RIG_CONTAMINATED`, because Ubuntu's backup monitor started by itself and touched the GPU for 10 s at the start of the 6-camera run, which still kept up; the second (6, 8 and 9 cameras at 75 fps, 10 and 11 at 60), after it was turned off (step 8 below), was `RIG_CLEAN`.

Two things a GStreamer writer in campy would have to do:

- **Send I420, not GRAY8.** On JetPack 7.2.1 `nvvidconv` lists GRAY8 but refuses to turn it into the encoder's NV12 ("Transform not supported"). The benchmark sends the mono image as I420's brightness plane with the two colour planes at 128 (`--input i420`, the default since pull request 401), which costs the encoder nothing. Letting GStreamer's `videoconvert` convert on the CPU (`--input gray8-convert`) took 85% of a core per camera, and 3 cameras fell to 54 fps.
- **Mark the video full range.** The encoder keeps the gray levels 0–255 but labels the stream limited range (16–235): the first 300 frames came back at 41.8 dB PSNR as stored but 31.7 dB when decoded by the label, which stretches the contrast. campy's other encoders write full range and say so.

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

It checks, in order: the Jetson Linux version (`/etc/nv_tegra_release`, R39.2 wanted) and the `nvidia-jetpack` packages; the power mode (MAXN, mode 0) and whether `jetson_clocks` is holding the clocks; `usbfs_memory_mb` (1000 or more) and USB autosuspend (-1); the `cdc_acm` serial driver (loaded, or in the kernel's config) and a `/dev/ttyACM*` port you can open; GStreamer's `nvv4l2h265enc`, `nvvidconv`, `rawvideoparse` and `h265parse`, and the encoder properties the benchmark sets; ffmpeg, and its `libx264` and `libx265`; FLIR cameras on USB (`lsusb`); PySpin in the campy venv, Spinnaker's version and how many cameras it sees; campy and PyQt5 in the campy venv; torch with CUDA working and sleap-nn in the sleap venv; and the free space on `/data`, and that `/data` is not on the system disk.

Options: `--campy-venv` (default `~/campy/.venv`), `--sleap-venv` (default `~/venvs/sleap`), `--video-dir` (default `/data`), `--min-free-gb` (default 200), `--cameras` (default 3), `--json` to keep the results. Counting cameras opens Spinnaker, so pass `--no-cameras` while a recording is running. It exits 1 if anything failed. Needs only Python's standard library, so it runs with the system `python3`.

## The encoder benchmark: `bench/jetson_encode_bench.py`

Starts one encoder per simulated camera (default 3) and feeds each synthetic 1280×1024 mono frames (bright blobs moving over a noisy gradient), paced at 60 fps, the way campy's writer feeds ffmpeg: one pipe per camera, one frame per write, and a write waits when the encoder falls behind. Each stream is

```
gst-launch-1.0 fdsrc ! rawvideoparse format=i420 width=1280 height=1024 framerate=60/1
  ! nvvidconv ! video/x-raw(memory:NVMM),format=NV12
  ! nvv4l2h265enc ratecontrol-enable=0 quant-i-frames=21 quant-p-frames=21
      iframeinterval=60 idrinterval=60 insert-sps-pps=1 num-B-Frames=0
  ! h265parse ! fdsink   →   ffmpeg -f hevc -i pipe: -c copy <campy's fragmented-MP4 flags> -tag:v hvc1 streamN.mp4
```

so the files are laid out like the MP4s campy writes on the other rigs. The frames go in as I420: the mono image as the brightness plane, the two colour planes at 128 ([Recording](#recording) says why not GRAY8). `--input gray8-convert` sends GRAY8 and adds GStreamer's `videoconvert` (on the CPU) before `nvvidconv`; `--input gray8` is the direct GRAY8 path, which JetPack 7.2.1 refuses. The property names are the ones in NVIDIA's Jetson Linux r39.2 Accelerated GStreamer guide, and the script checks them with `gst-inspect-1.0` before it starts.

```
python3 bench/jetson_encode_bench.py                    # 3 streams x 60 fps for 30 s, QP 21, keyframe every second
python3 bench/jetson_encode_bench.py --streams 8 --fps 75   # 8 cameras at 60 fps with campy-encode-sweep's 25% headroom
python3 bench/jetson_encode_bench.py --seconds 1800     # a 30-minute run
python3 bench/jetson_encode_bench.py --mux raw          # write .h265 files directly, without the ffmpeg step
python3 bench/jetson_encode_bench.py --cpu-fallback libx264 --cpu-preset ultrafast    # the same on the CPU
python3 bench/jetson_encode_bench.py --dry-run          # print the commands only
```

For each stream it reports the frames written, the frame rate achieved, how long each pipe write took (p50, p99, max), how far the writes fell behind the 60 fps clock (the most, and over the last second: a lag that keeps growing means the encoder can't keep up, and a real recording's write queue would grow), the CPU used by the `gst-launch-1.0` and `ffmpeg` processes (100% = one core), the file size and bitrate, the frames counted back in the file, and, for the first stream, the PSNR of its first 300 frames against what was sent (two ways: as stored, and by the video's range, which tells whether the encoder kept gray levels 0–255 or squeezed them to 16–235). For the whole machine: CPU busy (mean, and the highest 1-second sample), the script's own CPU, and every `tegrastats` line during the run (encoder clock, power, temperatures). Everything goes to a JSON file (`--json`, otherwise a timestamped file in the current folder).

The videos go to a new folder under `/data` (`--out-dir` to choose; it should be the disk recordings will use) and are deleted at the end unless `--keep`. Other options: `--streams`, `--fps`, `--seconds`, `--qp`, `--keyframe-interval` (seconds, like campy's `keyframeInterval`), `--preset-level`, `--maxperf` (adds `maxperf-enable=1`, a property older JetPacks list), `--cpu-preset`, `--psnr-frames` (0 skips the PSNR). Needs numpy (the campy venv has it). Run it through [`bench/rig.py`](development.md#timing-tests-on-a-shared-machine-benchrigpy), like every timing test.

## Updating it with the other machines

At each release, as on the EVO-X2: in `~/campy`, `git fetch` and `git merge --ff-only` to the tag or to `origin/dev` (the Orin reaches GitHub itself); `pip install -e ~/campy` again in both venvs (new entry points, the version); `python3 ~/campy/bench/rig.py install` when `rig.py` changed; then the rig checks under the lock.

## Still to do

- A GStreamer writer in campy for the hardware encoder (I420 in, full range marked), so recordings don't use the CPU.
- A recording with real cameras, timed, with `campy-live` tracking them.
- How far TensorRT's fp16 moves the keypoints from fp32 here, as measured on the other NVIDIA cards.
