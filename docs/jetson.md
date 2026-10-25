# Jetson AGX Orin (bring-up)

An NVIDIA Jetson AGX Orin 64GB developer kit is being set up as a small, stand-alone rig that records three cameras and tracks the animals live on the same box. This page is the setup, and the two scripts that check it before campy runs there. campy itself doesn't support the Orin's video encoder yet: no numbers on this page are measured, and nothing here changes how campy records.

The target: JetPack 7.2.1 (Jetson Linux 39.2.1: Ubuntu 24.04, Python 3.12, CUDA 13.2, TensorRT 10.16), three FLIR Blackfly S Mono 1.3 MP cameras (1280×1024, 8-bit mono) triggered at 60 fps by an Arduino Nano ESP32 on `/dev/ttyACM0`, and `campy-live-view` on the Orin's own monitor.

**Why the encoder is different here.** Stock ffmpeg can't use the Orin's hardware video encoder: the Orin has no `libnvidia-encode`, so `hevc_nvenc` fails. NVIDIA's route is GStreamer's `nvv4l2h265enc`, fed gray frames that `nvvidconv` turns into NV12 in the Orin's hardware memory. `bench/jetson_encode_bench.py` measures whether that keeps up with three cameras before campy is changed to use it.

## Setting up the Orin

1. **Flash JetPack 7.2.1** with NVIDIA SDK Manager from an Ubuntu PC, or with the USB installer image (if it loops, use SDK Manager or `l4t_initrd_flash`). Then install the CUDA, TensorRT and multimedia packages:
   ```
   sudo apt update && sudo apt install nvidia-jetpack
   ```
   JetPack 6 won't do: NVIDIA's torch wheels for it are Python 3.10 only, and sleap-nn needs Python 3.11 or newer.
2. **A video disk.** The 64 GB eMMC is too small for video. Put an NVMe drive in the M.2 Key-M slot, format it ext4, add it to `/etc/fstab` by its UUID (`sudo blkid` shows it) mounted at `/data`, and make it yours (`sudo chown $USER /data`).
3. **Full power.** `sudo nvpmodel -m 0` selects MAXN (it may ask to reboot; it stays set). `sudo jetson_clocks --fan` holds the CPU, GPU and memory clocks at their maximum with the fan up; it has to be run again after every boot (a small systemd unit can do it). Above 65 W of instantaneous draw the Orin halves its clocks; if that happens, the 50 W mode is the fallback.
4. **USB for the cameras.** In `/boot/extlinux/extlinux.conf`, add `usbcore.usbfs_memory_mb=1000 usbcore.autosuspend=-1` to the end of the `APPEND` line, then reboot. The first gives the FLIR cameras the USB buffer memory they need (the default 16 MB drops frames); the second stops Linux from suspending the cameras and the trigger. Add yourself to the `dialout` group for the trigger's serial port: `sudo usermod -aG dialout $USER`, then log out and in.
5. **Tools:**
   ```
   sudo apt install ffmpeg gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
   ```
6. **FLIR Spinnaker 4.4** for ARM64 / Ubuntu 24.04 (`install_spinnaker_arm.sh`, which also offers the `flirimaging` group and the USB memory setting), and the matching PySpin wheel for Python 3.12 (cp312, aarch64). The PySpin wheel must match the Spinnaker version exactly. SpinView should list all three cameras.
7. **Two venvs**, with the system Python 3.12, as on the other Linux rig:
   - **campy**, for `campy-acquire` and `campy-live-view`:
     ```
     git clone https://github.com/olinesn/campy ~/campy && cd ~/campy && git checkout dev
     python3 -m venv ~/campy/.venv && . ~/campy/.venv/bin/activate
     pip install -e ~/campy
     pip install <the PySpin cp312 aarch64 wheel>
     pip install PyQt5
     ```
     If pip has no PyQt5 wheel for aarch64, install it from Ubuntu instead (`sudo apt install python3-pyqt5`) and make the venv with `python3 -m venv --system-site-packages ~/campy/.venv`.
   - **sleap**, for `campy-live`. Install torch first, from PyTorch's CUDA 13.2 wheels (sleap-nn's own CUDA torch source is for x86 PCs only); a warning that `sm_87` isn't in torch's build list is harmless:
     ```
     python3 -m venv ~/venvs/sleap && . ~/venvs/sleap/bin/activate
     pip install torch torchvision --index-url https://download.pytorch.org/whl/cu132
     pip install sleap-nn
     pip install -e ~/campy
     ```

## Checking the setup: `bench/jetson_setup_check.py`

A read-only checklist. It prints PASS, WARN or FAIL for each item, with a one-line fix for anything that isn't right, and changes nothing: it only reads files and runs commands that report.

```
python3 bench/jetson_setup_check.py
```

It checks, in order: the Jetson Linux version (`/etc/nv_tegra_release`, R39.2 wanted) and the `nvidia-jetpack` packages; the power mode (MAXN, mode 0) and whether `jetson_clocks` is holding the clocks; `usbfs_memory_mb` (1000 or more) and USB autosuspend (-1); the `cdc_acm` serial driver (loaded, or in the kernel's config) and a `/dev/ttyACM*` port you can open; GStreamer's `nvv4l2h265enc`, `nvvidconv`, `rawvideoparse` and `h265parse`, and the encoder properties the benchmark sets; ffmpeg, and its `libx264` and `libx265`; FLIR cameras on USB (`lsusb`); PySpin in the campy venv, Spinnaker's version and how many cameras it sees; campy and PyQt5 in the campy venv; torch with CUDA working and sleap-nn in the sleap venv; and the free space on `/data`, and that `/data` is not on the system disk.

Options: `--campy-venv` (default `~/campy/.venv`), `--sleap-venv` (default `~/venvs/sleap`), `--video-dir` (default `/data`), `--min-free-gb` (default 200), `--cameras` (default 3), `--json` to keep the results. Counting cameras opens Spinnaker, so pass `--no-cameras` while a recording is running. It exits 1 if anything failed. Needs only Python's standard library, so it runs with the system `python3`.

## Can the encoder keep up? `bench/jetson_encode_bench.py` (benchmark B2)

Starts one encoder per simulated camera (default 3) and feeds each synthetic 1280×1024 mono frames (bright blobs moving over a noisy gradient), paced at 60 fps, the way campy's writer feeds ffmpeg today: one pipe per camera, one frame per write, and a write waits when the encoder falls behind. Each stream is

```
gst-launch-1.0 fdsrc ! rawvideoparse format=gray8 width=1280 height=1024 framerate=60/1
  ! nvvidconv ! video/x-raw(memory:NVMM),format=NV12
  ! nvv4l2h265enc ratecontrol-enable=0 quant-i-frames=21 quant-p-frames=21
      iframeinterval=60 idrinterval=60 insert-sps-pps=1 num-B-Frames=0
  ! h265parse ! fdsink   →   ffmpeg -f hevc -i pipe: -c copy <campy's fragmented-MP4 flags> -tag:v hvc1 streamN.mp4
```

so the files are laid out like the MP4s campy writes on the other rigs. The property names are the ones in NVIDIA's Jetson Linux r39.2 Accelerated GStreamer guide, and the script checks them with `gst-inspect-1.0` before it starts.

```
python3 bench/jetson_encode_bench.py                    # 3 streams x 60 fps for 30 s, QP 21, keyframe every second
python3 bench/jetson_encode_bench.py --seconds 1800     # the 30-minute pass/fail run
python3 bench/jetson_encode_bench.py --mux raw          # write .h265 files directly, without the ffmpeg step
python3 bench/jetson_encode_bench.py --cpu-fallback libx264 --cpu-preset ultrafast    # the same on the CPU
python3 bench/jetson_encode_bench.py --dry-run          # print the commands only
```

For each stream it reports the frames written, the frame rate achieved, how long each pipe write took (p50, p99, max), how far the writes fell behind the 60 fps clock (the most, and over the last second: a lag that keeps growing means the encoder can't keep up, and a real recording's write queue would grow), the CPU used by the `gst-launch-1.0` and `ffmpeg` processes (100% = one core), the file size and bitrate, the frames counted back in the file, and, for the first stream, the PSNR of its first 300 frames against what was sent (two ways: as stored, and by the video's range, which tells whether the encoder kept gray levels 0–255 or squeezed them to 16–235). For the whole machine: CPU busy (mean, and the highest 1-second sample), the script's own CPU, and every `tegrastats` line during the run (encoder clock, power, temperatures). Everything goes to a JSON file (`--json`, otherwise a timestamped file in the current folder).

The videos go to a new folder under `/data` (`--out-dir` to choose; it should be the disk recordings will use) and are deleted at the end unless `--keep`. Other options: `--streams`, `--fps`, `--seconds`, `--qp`, `--keyframe-interval` (seconds, like campy's `keyframeInterval`), `--preset-level`, `--maxperf` (adds `maxperf-enable=1`, a property older JetPacks list), `--cpu-preset`. Needs numpy (the campy venv has it).

**Pass:** 3 streams at 60 fps for 30 minutes with the lag over the last second under one frame (16.7 ms) on every stream, and every frame in the file. It should also be run while `bench/sleap_infer_bench.py` runs, since the encoder, the GPU and the CPU share the Orin's memory. When other people use the machine, run both through [`bench/rig.py`](development.md#timing-tests-on-a-shared-machine-benchrigpy).

Results from the Orin will be added here and to the [hardware guide](hardware.md) once it is running.
