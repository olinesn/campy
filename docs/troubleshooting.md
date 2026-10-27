# Troubleshooting

Problems seen so far, by what you see: the message campy prints, or what is wrong with the recording. Each entry says what causes it and what to do, in a few lines, and links to the page that explains it in full. Search this page for the words of the message you got.

Before recording, campy checks the config and the computer, and most problems show up there as a `FAIL` or `WARN` line of the launch check ([Scaling the number of cameras](encoding.md#scaling-the-number-of-cameras)) or as a config error ([Configs](configs.md#what-campy-checks-before-any-camera-opens)). `ffmpegLogLevel: "warning"` in the config shows ffmpeg's own messages, which campy hides by default.

## Installing

**`import PySpin` fails.** The PySpin wheel must match both the Python it is installed into (its `cp312` tag for Python 3.12) and the Spinnaker SDK installed on the computer (its version, e.g. 4.4.0.246). PySpin for Python 3.12 needs NumPy 2.x, and on Windows and macOS Spinnaker 4.4 or newer. An environment with an older PySpin works only with that older SDK, so upgrade a computer's Spinnaker and its campy environment together ([Installation](install.md#installation)).

**PySpin finds 0 cameras, or SpinView is missing, after installing Spinnaker 4.4 on Windows.** Its installer can fail halfway and still list the SDK in Settings ▸ Apps. The packages it saved can be installed directly, and the USB3 camera driver registered with `pnputil` ([Spinnaker 4.4 on Windows: when the installer fails](install.md#spinnaker-44-on-windows-when-the-installer-fails)).

**Spinnaker doesn't see a FLIR camera that is plugged in, on Linux.** The user who records needs the camera access Spinnaker's installer sets up (group `flirimaging`); log out and in after joining it ([FLIR cameras](flir.md#flir-cameras-at-43-mbs-after-a-restart)).

**`campy-align` or `campy-synctest` stops without a word on Windows, exit code `-1066598273` (`0xC06D007F`).** NumPy's line fit crashed Python. In that environment NumPy does its linear algebra with Intel's MKL, and conda-forge's MKL 2026.1 calls two functions (`__atomic_compare_exchange`, `__atomic_load`) that the OpenMP library it comes with (`llvm-openmp` 23) doesn't provide. Recording isn't affected: only the fits after it. To check: `python -c "import numpy as np; print(np.polyfit([0, 1, 2], [1, 3, 5], 1))"` prints nothing. To fix it, switch the environment's NumPy to OpenBLAS: `conda install -n campy -c conda-forge "libblas=*=*openblas" "libcblas=*=*openblas" "liblapack=*=*openblas"`. This swaps three small packages that only tell NumPy which library to use, and adds OpenBLAS (5 MB). NumPy and everything else stay as they are. Seen on the Dell Pro Max Slim FCS1250 in October 2026. After the switch, `campy-align` ran there and wrote exactly the same files as in an environment that never had the problem, and recording with live tracking was as fast as before.

**The launch check FAILs a camera's `cameraSelection`.** It picks a camera that wasn't found: with three FLIR cameras connected, they are 0, 1 and 2, so `cameraSelection` 3 fails ([Scaling the number of cameras](encoding.md#scaling-the-number-of-cameras)).

## Encoding and ffmpeg

**Every camera's writer stops with a broken pipe, and ffmpeg says `Cannot get the preset configuration: unsupported param (12)`.** ffmpeg 4.3 and older ask the NVIDIA driver for NVENC presets in a way drivers from 591.44 (December 2025) refuse. Use ffmpeg 4.4 or newer: the environment's ffmpeg 8.1 from the install command, or another one set with `ffmpegPath` ([NVIDIA GPU encoding](encoding.md#nvidia-gpu-encoding-nvenc)).

**A broken pipe from ffmpeg, for any other reason.** ffmpeg stopped, and campy says so and ends that camera's recording (`videoError` in its `metadata.csv`). Set `ffmpegLogLevel: "warning"` to see ffmpeg's own error ([Helpful tips](encoding.md#helpful-tips)). For NVENC, the launch check encodes test frames with each camera's settings first and prints ffmpeg's error with what it means.

**`Driver does not support the required nvenc API version. Required: 13.1 Found: 13.0`.** That ffmpeg was built for a newer NVENC than the driver has. Update the driver, or use an ffmpeg built for the NVENC version the driver has: BtbN's 8.0 and 8.1 builds are NVENC 13.0 (driver 570 or newer) ([NVIDIA GPU encoding](encoding.md#nvidia-gpu-encoding-nvenc)).

**The launch check says ffmpeg `has no hevc_nvenc encoder`.** conda picked an ffmpeg without NVENC, because the driver is older than 535. Update the driver, or set `ffmpegPath` to an ffmpeg built for an older NVENC ([NVIDIA GPU encoding](encoding.md#nvidia-gpu-encoding-nvenc)).

**The Quick Sync encoder is missing: the launch check says ffmpeg `has no hevc_qsv encoder` (or `h264_qsv`, `av1_qsv`).** conda-forge's ffmpeg for Windows has no Quick Sync. Set `ffmpegPath` to an ffmpeg built with Intel's oneVPL, such as BtbN's builds; `ffmpeg -hide_banner -encoders | findstr qsv` lists what one has ([Intel GPU encoding](encoding.md#intel-gpu-encoding-quick-sync)).

**VA-API: the device can't be opened, or `vainfo` lists no encoder.** Install `mesa-va-drivers` (AMD) or Intel's media driver, add the user to the `render` group and log out and in, and set `vaapiDevice` if the GPU isn't `renderD128` ([Helpful tips](encoding.md#helpful-tips)).

**On an AMD Ryzen AI Max, a camera's encoder stalls for half a second at a time, and `journalctl -k` shows `amdgpu: Fence fallback timer expired on ring comp_...`.** Mesa's tiled video surfaces use the GPU's compute engine, which misses interrupts on that chip. campy's default, `vaapiLinearSurfaces: True`, avoids it; check that a config doesn't turn it off ([Helpful tips](encoding.md#helpful-tips)). `campy-acquire` sends an alert when they come back: 3 within 10 minutes ([Alerts](alerts.md#the-gpus-kernel-log-amd-on-linux)).

**The launch check's capacity line FAILs or WARNs.** The cameras on an encoder are more than `campy-encode-sweep` measured it keeping up with on this computer, or more than it tried. A WARN says which `--streams` to measure; a missing table (`no encoder capacity table`) only means the sweep hasn't been run here ([Scaling the number of cameras](encoding.md#scaling-the-number-of-cameras)).

## Cameras

**A FLIR camera records at about 33 fps, or its throughput is 43 MB/s.** A camera that stays plugged in through a computer restart comes back with a USB 2 throughput limit. Shutting down and switching on clears it, and so does `campy-flir-reset` ([FLIR cameras](flir.md#flir-cameras-at-43-mbs-after-a-restart)).

**The launch check says a camera is connected at USB 2 speed (`not USB 3`).** Its cable, hub or port runs at USB 2. Plug it straight into a USB 3 port, with a USB 3 cable ([Scaling the number of cameras](encoding.md#scaling-the-number-of-cameras)).

**A free-running FLIR camera records slower than `frameRate`, or the launch check FAILs its frame rate.** Something holds the camera below `frameRate`: an exposure longer than a frame, its USB throughput limit, or its sensor's readout. campy names which; shorten the exposure, or lower the frame rate or frame size ([Frame rate, frame size and USB limit](flir.md#frame-rate-frame-size-and-usb-limit)).

**`Camera0: campy did not close it last time and left it capped at 80.0 fps. Cap removed, as before.`** A run that stopped without closing the camera (a crash, a kill, a restart) left its frame-rate cap on. campy put back the settings it had found. Nothing to do ([Frame rate, frame size and USB limit](flir.md#frame-rate-frame-size-and-usb-limit)).

**`Camera0: still acquiring from a run that did not close it. Stopped it, so its frame size and pixel format can be set.`** The same, for an acquisition a run left going. campy stopped it. Nothing to do. With a trigger campy doesn't start itself, also check `campy-align`'s report, or unplug the camera first ([Frame rate, frame size and USB limit](flir.md#frame-rate-frame-size-and-usb-limit)).

**A video is scrambled, or the launch check says a camera `would send 640x512 frames, not frameWidth x frameHeight 1280x1024`.** The camera can't take the frame size the config asks for right now. Unplug it and plug it back in, or set `frameWidth` and `frameHeight` to the frames it sends ([Frame rate, frame size and USB limit](flir.md#frame-rate-frame-size-and-usb-limit)).

**`Camera0: frameWidth 1000 rounded up to 1008`.** campy sets FLIR frame sizes in steps of 16. To avoid it, use a multiple of 16 ([Frame rate, frame size and USB limit](flir.md#frame-rate-frame-size-and-usb-limit)).

**`Camera0: frameWidth x frameHeight 1152x1024 changed to 1280x1024`.** A Basler camera's video takes the frame size its `.pfs` file gives the camera, and an emulated camera's the clip's. Set `frameWidth` and `frameHeight` to match to make the message go ([Configuration](configs.md#configuration)).

**campy takes about a second longer to start than it should, and the launch check WARNs on `gige`.** Spinnaker searches every network interface for GigE cameras each time it starts. On a computer whose FLIR cameras are all USB3, turn the search off with `campy-flir-reset --gige off` ([Faster startup](flir.md#faster-startup-spinnakers-search-for-gige-cameras)).

**A camera fails to open with Spinnaker's "Could not read the XML URL [-1008]".** Seen once, on one of three cameras; campy then recorded the other two. It opened normally on the next run.

**A FLIR camera's frames look darker or brighter than before.** Since 2.1.0, campy sets gamma 0.6 on FLIR cameras unless the config says otherwise (`cameraGamma`); before, it turned gamma off. `cameraGamma: "Off"` gives the old frames ([FLIR gamma](flir.md#flir-gamma-cameragamma)).

## Starting and stopping a recording

**The config is refused before any camera opens** (an unknown key, a value of the wrong kind or out of range, a key given twice, a per-camera list of the wrong length). campy lists every problem with its file and line, and suggests the key you probably meant. `campy-config check <config>` checks a config without recording ([Configs](configs.md#what-campy-checks-before-any-camera-opens)).

**`... not set: since 2.1.0 the default is ...` at startup.** The config leaves out a setting whose default changed, so it records differently than it did with an older campy. Set the setting to keep either behaviour ([Recording folders](recordings.md#defaults-that-changed-the-startup-warnings)).

**`Stopped before recording: ... already holds a recording, and campy never replaces one.`** With `sessionFolder: False`, campy records into `videoFolder` itself, and a recording is already there. Record into a new folder, or move that one away first ([Stop Recording](acquire.md#stop-recording)).

**campy stops with "The trigger Arduino … did not answer".** Before any camera starts, campy checks that the trigger Arduino responds. Unplug its USB cable and plug it back in (or power-cycle it), then run campy again. The Arduino sometimes stays busy with a command from a run that just stopped.

**campy doesn't exit after Ctrl-C.** A camera is hanging as it closes, in its driver or waiting for its ffmpeg. Press Ctrl-C again: campy stops that camera's process at once and names it ([Stop Recording](acquire.md#stop-recording)).

**A camera is reported after 3 s without frames.** It stopped sending them: a trigger that stopped, a loose cable, or a camera that is gone. campy says so every 30 s while it lasts, and ends that camera's recording when the camera is gone for good ([Stop Recording](acquire.md#stop-recording)).

**A Windows recording slows to a crawl after about 10 minutes.** Windows went into Modern Standby when the screen timed out. campy's `keepAwake` (on by default) asks Windows to stay awake, but if campy was started over SSH with nobody signed in, Windows refuses the screen part. Set the screen timeout to Never.

## What a recording holds

**Frames dropped in the middle of a recording** (`framesDropped` in a camera's `metadata.csv`). The encoder fell behind by more than `writeQueueMaxSec`, usually because the disk stopped taking writes. Some SSDs stall for 10–16 s with nothing in the system logs, even with their power saving off. Raise `writeQueueMaxSec` (60 s for 3 cameras takes up to about 14 GB of memory, used only during a stall), or record to a second drive. campy-acquire says so as it happens: once frames waiting for ffmpeg fill a quarter of `writeQueueMaxSec`, and again when any is dropped, on the console and, with [alerts](alerts.md) set up, as a Slack message.

**`the camera's frame ID skipped 99 frames: they were lost before reaching campy`** (`flirFramesLost` in `metadata.csv`). Frames were lost on the camera, its USB link or in Spinnaker: campy took frames too slowly, or the USB link is too slow for this frame size and rate. Such a camera numbers its later frames one lower than the others ([Stop Recording](acquire.md#stop-recording)).

**A video can't be opened after a crash: `moov atom not found`.** It was written as a plain MP4 (`fragmentedVideo: False`), whose index is written only as ffmpeg closes it. Keep the default, `fragmentedVideo: True`, so a video stays readable up to its last second ([Video that survives a crash](acquire.md#video-that-survives-a-crash-fragmentedvideo)).

**A video cut off by a power cut won't open in MATLAB on a Mac.** Copy it into a new file: `ffmpeg -fflags +discardcorrupt -i 0.mp4 -c copy 0-repaired.mp4` ([Video that survives a crash](acquire.md#video-that-survives-a-crash-fragmentedvideo)).

**A mono camera's full-range video looks green, or too dark and flat.** campy 2.1.0rc1 and rc2 wrote such Quick Sync and VA-API videos with no colour when ffmpeg was older than 8. Check with ffmpeg's `signalstats` (`UAVG=0` is affected) and re-encode a copy ([Full-range video](encoding.md#full-range-video-colorrange)).

**An H.265 video won't open in MATLAB, QuickTime or Safari on a Mac ("Cannot Decode").** Apple's decoder needs the video tagged `hvc1`, which campy now does. A copy of an older video: `ffmpeg -i 0.mp4 -c copy -tag:v hvc1 0-hvc1.mp4` ([H.265 on a Mac](encoding.md#h265-on-a-mac-matlab-quicktime-safari)).

**An AV1 video won't open in OpenCV (or SLEAP) on Linux.** OpenCV's Linux wheels can't decode AV1. Use SLEAP's PyAV reader (`sleap-label --video-backend pyav`), or record H.264 or H.265 ([Intel GPU encoding](encoding.md#intel-gpu-encoding-quick-sync)).

**Checking that cameras expose together.** `campy-synctest` measures it from a blinking LED that all cameras see ([Triggering and synchronization](sync.md#checking-that-cameras-expose-together)). `campy-align` puts every camera's frames on the trigger's timeline after recording.

## campy-live

**`campy-live` keeps printing `Waiting for the live feed` after the recording started.** The recording has no live feed: add `liveFeed: True` to the config, or `--liveFeed True` to `campy-acquire`. Both programs must run on the same computer, and `campy-live --name` must match the config's `liveFeedName` if that was changed ([Live pose estimation](live-tracking.md#live-pose-estimation-livefeed-campy-live)).

**`campy-live` misses the start of the recording.** Start it before `campy-acquire`, and wait for `Waiting for the live feed`. If the cameras' frames aren't 1280 × 1024 grey, give `--frame-shape` so the model is ready for them.

**`campy-live` takes up to 40 s to start.** The first start with a new model, GPU or update builds compiled copies of the networks, and keeps them for later starts. Start it before the recording and wait for `Waiting for the live feed` ([Compiled networks](live-tracking.md#compiled-networks---engine-on-by-default-with-a-gpu)).

**`--engine auto: ... so the networks run in torch`.** No compiled engine could be made: TensorRT and Triton aren't installed, or one failed (the line says why). campy-live runs anyway, only slower. To get the engines on Windows, install `tensorrt-cu13`, `onnx` and the `triton-windows` that matches PyTorch ([Installing the engine packages](live-tracking.md#installing-the-engine-packages-windows)).

**`campy-live` skips sets.** The model is too slow for this machine and frame rate. A few skipped in the first fraction of a second on CUDA are the first sets warming up. Skips throughout mean you need a lighter model ([Models](models.md#choosing-one-that-keeps-up)). On a Windows PC with an Intel hybrid CPU, see [Hardware](hardware.md) on pinning.

**`campy-live` is slower than the measurements say.** Check that the GPU keeps its clock up (`--hold-gpu-clock`, on by default with NVIDIA and AMD GPUs), that no monitor is plugged into the inference GPU, and that nothing else uses the GPU ([On Windows](live-tracking.md#on-windows-nvidia-cuda)).

**"misaligned" sets.** A set's frames reached the computer more than half a frame apart: one camera's frames came late, or a camera lost a frame (`flirFramesLost` in its `metadata.csv`) and numbers its frames one lower than the others ([Live pose estimation](live-tracking.md#live-pose-estimation-livefeed-campy-live)).

**Latencies in steps of 15–16 ms on Windows.** That recording was made before campy took its host times from Windows' precise clock (pull request 118). The steps are the old clock's. A program that writes frames into the live feed itself must stamp them with `campy.clock.HostNs` ([On Windows](live-tracking.md#on-windows-nvidia-cuda)).

**3D poses are wrong, or mice swap between cameras.** Check the calibration first (the lines in `campy-live-view`: [Calibration](calibration.md#calibrating)). If it is good, look at the 2D. False detections outside the cage, or a camera that often misses a mouse, pull wrong pairings into the 3D. A model trained on another rig does this more often, and retraining on the rig's own frames helps most.

## campy-live-view's warnings

The view puts a red label on a camera's tile when something is wrong with it ([Watching it live](live-view.md)):

- **`12.0 FPS`, `NO FRAMES`**: the camera is behind the others or has stopped. Look at `campy-acquire`'s messages for that camera.
- **`LOST 2`**: the camera's own timestamps skipped frames in the last 10 seconds (lost before campy got them).
- **`GAP 1`**: frame numbers are missing from its live feed.
- **`LATE 1`**: frames reached the computer a frame late.
- **`1 FRAME AHEAD`**: the camera's frames of a given number were taken a trigger pulse before or after the others'. A camera kept grabbing after a run that didn't close it. Stop the recording and start it again.
- **`#2  bad 3D: 34 px`** on a mouse: its 3D is far from the cameras' 2D (above `--bad-px`). Usually a false detection or a mouse one camera misses.

**No window over SSH.** Without a desktop, use `campy-live-view --no-window --png <folder>`, which saves every 30th view.

## Exporting and LUC3D

**`campy-live-export` says "the log's frames are not this recording's".** The log is from another recording. Give the `.npy` that campy-live wrote during this one ([Opening a tracked recording in LUC3D](live-tracking.md#opening-a-tracked-recording-in-luc3d)).

**`campy-live-export` says `no node names: the log has no summary; give --skeleton <model folder>`.** The log's summary has no skeleton: a log from campy 2.1.0rc6 or earlier, a campy-live stopped by an error, or one that ran untrained models (`--model random`, which have no body parts to name). Give `--skeleton <the instance model's folder>`, and `--cameras` if the summary is missing ([Opening a tracked recording in LUC3D](live-tracking.md#opening-a-tracked-recording-in-luc3d)).

**LUC3D's tab runs out of memory opening a long recording.** File ▸ Load SLP reads the whole file at once; an hour was too much. Export the stretch you want with `campy-live-export --frames FIRST:LAST`, or open the whole recording with File ▸ Load Project or Load Session Folder ([Opening a tracked recording in LUC3D](live-tracking.md#opening-a-tracked-recording-in-luc3d)).

**LUC3D can't open folders, in Brave.** Brave turns off the file access LUC3D uses. Use Chrome or Edge, or turn on brave://flags/#file-system-access-api.

## Timing tests

**A timing run's log ends `RIG_CONTAMINATED`.** Something outside the run used the GPU, the encoder, the cameras, the trigger, or a lot of CPU or disk while it ran. Its numbers can't be compared; the log names what it saw. Run it again when the machine is quiet ([Timing tests on a shared machine](development.md#timing-tests-on-a-shared-machine-benchrigpy)).
