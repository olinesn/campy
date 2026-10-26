# Video encoding

How campy compresses each camera's video: the GPU encoders (NVIDIA NVENC, Intel Quick Sync, VA-API on Linux for AMD and Intel), the ffmpeg each one needs, the video's colour range and tags, and how many cameras a computer's encoders keep up with. Every setting named here is in the [settings reference](config-reference.md).

## NVIDIA GPU encoding (NVENC)
`gpuMake: "nvidia"` needs an ffmpeg that is 4.4 or newer and is built for an NVENC version the NVIDIA driver supports.
- ffmpeg 4.3 and older ask the driver for NVENC presets with a call that NVIDIA drivers from 591.44 (December 2025) reject for H.264 and HEVC. Every camera's writer then stops with a broken pipe, and ffmpeg's error is `Cannot get the preset configuration: unsupported param (12)`. campy's install instructions used to pin `ffmpeg==4.2.2`, so environments made with them have this problem. A standalone ffmpeg set with `ffmpegPath` (below) fixes it and leaves the environment as it is.
- An ffmpeg built for a newer NVENC version than the driver supports fails the other way, and says what it needs: `Driver does not support the required nvenc API version. Required: 13.1 Found: 13.0`, then `The minimum required Nvidia driver for nvenc is 610.00 or newer`. Update the driver, or use an ffmpeg built for the NVENC version the driver has ("Found").

conda-forge's ffmpeg comes in builds for several NVENC versions, and conda picks one the driver supports when it creates the environment, by the CUDA version `nvidia-smi` shows: 12.8 or newer (driver 570 or newer) gets NVENC 13.0, and 12.2 to 12.7 an older NVENC version. A driver older than 535 (CUDA below 12.2) gets an ffmpeg without NVENC, and campy's launch check stops with "has no hevc_nvenc encoder": update the driver, or set `ffmpegPath` to an ffmpeg built for an older NVENC version.

The install command ([Installation](install.md#installation)) pins ffmpeg 8.1. Against 4.2.2, campy's tests pass, and libx264 writes the same video from the same frames, except that a frame size padded to a multiple of 16 is rescaled slightly differently (by at most one gray level). On NVENC, `-preset fast` now means P1, NVENC's fastest preset, and video from RGB input is tagged with the conversion NVENC applies (BT.601, limited range), which 4.2.2 left untagged.

To use another ffmpeg, put its path in the config; campy passes it to imageio-ffmpeg as `IMAGEIO_FFMPEG_EXE`:
```
ffmpegPath: 'C:\ffmpeg\bin\ffmpeg.exe'
```
BtbN's builds (https://github.com/BtbN/FFmpeg-Builds/releases, e.g. `ffmpeg-n8.1-latest-win64-gpl-8.1.zip` unzipped to `C:\ffmpeg`) take their NVENC version from the ffmpeg version: 8.0 and 8.1 are built for NVENC 13.0 (driver 570 or newer), 9.x and master for NVENC 13.1 (driver 610 or newer).

To check a computer before recording:
1. `nvidia-smi` prints the driver version.
2. Encode two seconds of test frames, without cameras, with the ffmpeg campy will use: the one in `ffmpegPath`, or else the environment's own (`python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"` prints its path).
```
ffmpeg -hide_banner -f lavfi -i testsrc2=size=1280x1024:rate=60 -t 2 -pix_fmt rgb0 -c:v hevc_nvenc -preset fast -qp 24 -bf:v 0 -gpu 0 -y nvenc_test.mp4
```
It passes if it writes nvenc_test.mp4 and prints no errors. These are campy's settings for `codec: "h265"`, `quality: 24` and `gpuID: 0`; `codec: "h264"` uses `h264_nvenc`.

`campy-acquire`'s launch check runs the same test with each NVENC camera's own settings before any camera starts, and stops with ffmpeg's error and what it means.

For `preset`, `"fast"` (campy's default) works in every ffmpeg, and `p1` (fastest) to `p7` (best quality) from 4.4 on. ffmpeg 9 removed the older names, such as `llhp` and `llhq`.

`codec: "av1"` uses `av1_nvenc`, on GPUs whose NVENC encodes AV1: the Ada generation (RTX 40-series, RTX 2000-6000 Ada) and newer. Its `quality` is AV1's quantizer index, 0-255, not the 0-51 QP of H.264 and H.265. On an RTX 2000 Ada (1280x1024 mono, 80 fps, with `campy-encode-sweep`'s 1.25x headroom), NVENC kept up with 12 cameras in H.264 or H.265, and with 16 or more in AV1 at quality 88. On an RTX 4000 Ada (same settings, quality 24) it kept up with 28 cameras at 80 fps and 32 at 60, in H.264 and H.265 alike. There the encoder chip itself was the limit, at about 3,100 frames/s in all: it was 93-98% busy from 12 cameras on, and at 32-36 cameras a run could pass or fail at the same count, so the lower count is the one to count on.

## Intel GPU encoding (Quick Sync)
`gpuMake: "intel"` encodes on an Intel GPU with Quick Sync: `h264_qsv`, `hevc_qsv`, and for `codec: "av1"` `av1_qsv` (Arc GPUs, and the integrated GPUs of Core Ultra processors). It needs an ffmpeg built with Intel's oneVPL (libvpl). conda-forge's ffmpeg for Windows is not: 8.1.2 has no `_qsv` encoders. Set `ffmpegPath` to one that is, such as BtbN's builds above, which also have NVENC; `ffmpeg -hide_banner -encoders | findstr qsv` lists them. On Linux, `gpuMake: "vaapi"` (below) uses Intel GPUs through their media driver instead.

`quality` is Quick Sync's constant quantizer: campy passes `quality` + 1 for H.264 and H.265, and AV1's quantizer index, 0-255, as it is. Quick Sync takes NV12 frames (4:2:0, like yuv420p), so campy gives it NV12 for `pixelFormatOutput: "yuv420p"`; the video is the same.

On a Core Ultra 9 285's integrated GPU (1280x1024 mono, 80 fps, 1.25x headroom), H.264, H.265 and AV1 (quality 88) each kept up with 16 or more cameras, more than the RTX 2000 Ada beside it. Quick Sync and NVENC are separate encoders, so a computer with both can split its cameras between them (`gpuMake` and `gpuID` per camera, with an ffmpeg that has both). The launch check notes that the sweep measured each encoder alone. `configs/campy_config_chronicrecord.yaml` records FLIR cameras with Quick Sync H.265 on that computer.

AV1 needed 10-20% fewer bits than H.265 and H.264 for the same detail on a moving test scene, but it is harder to read back. SLEAP 1.6 reads video through OpenCV unless told otherwise. OpenCV's Linux wheels (4.13 and 5.0) cannot decode AV1 at all. Its Windows wheel reads AV1 at 60-100 frames/s, against about 400 for H.264 and H.265. SLEAP's PyAV reader decodes AV1 everywhere (`sleap-label --video-backend pyav`). LUCID reads it in Chrome and Firefox, while Safari plays AV1 only on Macs with Apple M3 or newer.

## Full-range video (colorRange)
`colorRange: "full"` puts a camera's gray levels, 0-255, into the video as they are. With the default, `"limited"`, ffmpeg first squeezes them into video's usual 16-235, rescaling every pixel. Full range keeps all 256 levels and is marked in the file, so readers decode the camera's own levels back. Every reader tried gave each level of a 0-255 ramp back to within 0.2: OpenCV 4.6 and 4.12, PyAV, imageio-ffmpeg, sleap-io 0.5.3 (its OpenCV, FFMPEG and PyAV backends) and Chrome as LUCID decodes video, for H.264 and H.265 from Quick Sync and NVENC. VA-API's H.264, H.265 and AV1 were checked through ffmpeg's own gray decode only, which could not show that their colour was wrong (see below). Chrome was checked on a Mac, and on Windows with H.264 only: there it decodes H.265 on the GPU, which an SSH session cannot reach. OpenCV 4.9 and 5.0 and MATLAB R2023b's VideoReader on a Mac gave every level back exactly, for H.264 and H.265 from Quick Sync and from libx264 and libx265. MATLAB was not checked on Windows. On a Mac it opens H.265 only when the file tags it `hvc1`, as campy now does (see [H.265 on a Mac](#h265-on-a-mac-matlab-quicktime-safari)). The same ramp in limited range came back up to 2 levels darker through OpenCV and sleap-io, and up to 2 brighter through MATLAB.

It also spares ffmpeg most of its CPU work for a GPU encoder, which was that rescaling. Per 1280x1024 mono frame, Quick Sync H.265 took 1.7 ms instead of 3.3 (Dell Pro Max Slim FCS1250, ffmpeg 8.1), and VA-API H.265 1.4 ms instead of 5.8 (EVO-X2, Ubuntu's ffmpeg 6.1, before the colour fix below, which adds 0.2-0.4 ms): for 16 cameras at 80 fps, about two cores fewer on the first and five on the second.

At the same `quality` a full-range video is about 20% larger, since the extra levels are detail the encoder keeps. At the same size its picture is slightly better (+0.3 dB PSNR on a moving scene, Quick Sync H.265), so raise `quality` by about 2 to keep file sizes as they were: quality 26 in full range made a 9% smaller file than 24 in limited range, and scored 0.2 dB higher.

It applies to every encoder, except NVENC given `pixelFormatOutput: "rgb0"`, which converts RGB to limited range itself (campy says so: use `"yuv420p"`). campy converts the range rather than only marking the file: before ffmpeg 7.1, ffmpeg marked such a file full range but left its levels squeezed, so readers decoded black as 16 and white as 235.

With ffmpeg before 8, campy 2.1.0rc1 and 2.1.0rc2 wrote a mono camera's full-range Quick Sync and VA-API video with no colour: both chroma planes 0 instead of 128. Those encoders take NV12 frames, and ffmpeg 6.1, 7.0 and 7.1 convert gray frames to full-range NV12 that way. The gray levels in such a video are right, and readers that read gray get them back (imageio-ffmpeg with `pix_fmt="gray"`, PyAV with `format="gray"`). Any reader that shows the video in colour shows it green. MATLAB and QuickTime showed black as 28 and white as 109, Chrome (and so LUCID) about the same, and OpenCV's frames, converted to gray, read black as 80 and white as 176. campy now converts such frames to full-range yuv420p first, then to NV12, when its ffmpeg is older than 8, and every reader gives each level back, on the EVO-X2's VA-API too. That costs 0.2-0.5 ms of CPU a 1280x1024 frame, so with ffmpeg 8.0 and 8.1, which convert right, the filter stays as it was. Video from the CPU, NVENC and AMF (yuv420p) and from colour cameras was never affected, nor was the Dell Pro Max Slim FCS1250's Quick Sync video (ffmpeg 8.1). A full-range video from the EVO-X2's VA-API is. To check a video, look for `UAVG=0` (affected) or `UAVG=128` in what this prints:
```
ffmpeg -hide_banner -i 0.mp4 -frames:v 1 -vf signalstats,metadata=print:key=lavfi.signalstats.UAVG -f null -
```
This makes a copy that every reader shows right, re-encoded at high quality:
```
ffmpeg -i 0.mp4 -vf lutyuv=u=128:v=128 -c:v libx264 -crf 12 -color_range pc 0-fixed.mp4
```

## H.265 on a Mac (MATLAB, QuickTime, Safari)
On a Mac, MATLAB's `VideoReader`, QuickTime and Safari decode H.265 with Apple's decoder, which opens an H.265 .mp4 or .mov only when the file tags its video `hvc1`. ffmpeg's own tag is `hev1`, which campy wrote until now: Apple's decoder refused every such file ("Cannot Decode"), from every encoder, in either color range. Chrome, and so LUCID, opens either. campy now has ffmpeg tag H.265 `hvc1`, for every encoder. With libx265 (`codec: "h265"`, `gpuID: -1`) it also asks for closed GOPs (`-x265-params open-gop=0`), as libx264 and the GPU encoders write them. On libx265's default open GOPs, Apple's decoder failed even with `hvc1`, and imageio, PyAV, MATLAB and Chrome, asked for the frame just before a keyframe, gave the keyframe (frame 40 for 39). Closed GOPs make libx265's files about 1% larger with `keyframeInterval: 0.5`, and no larger with keyframes 250 frames apart.

This was checked on a Mac with 1280x1024, 80 fps video and a keyframe every 0.5 s. The clips were libx265 through campy's writer (limited and full range; .mp4 and .mov; fragmented and plain), the Dell Pro Max Slim FCS1250's Quick Sync H.265 and an NVENC clip, each copied into a file as campy now writes it, the EVO-X2's VA-API H.265 from campy's own ffmpeg command (Ubuntu's ffmpeg 6.1; limited and full range), and `hevc_videotoolbox`. MATLAB R2023b's `VideoReader`, AVFoundation (QuickTime's framework), imageio-ffmpeg (ffmpeg 7.0 and 8.1), PyAV 19 and OpenCV 4.6, 4.9 and 5.0 each read every frame in order. They, Chrome 155 and WebKit 26.6 (Safari's engine) also gave the frame asked for at every seek, to keyframes and to the frames either side of them. Quick Sync and VA-API repeat the stream's parameter sets before every keyframe, which `hvc1` expects only in the file's header; their video decodes to the same frames either way. Not checked: AMF's H.265, and MATLAB on Windows.

A copy of an older campy H.265 video, tagged `hvc1`, opens in MATLAB, QuickTime and Safari on a Mac:
```
ffmpeg -i 0.mp4 -c copy -tag:v hvc1 0-hvc1.mp4
```
A copy of a libx265 video keeps its open GOPs: QuickTime still refuses it, Safari's engine showed a broken picture for the frame before a keyframe, and MATLAB reads it in order but, asked for that frame, gives the keyframe.

## Scaling the number of cameras
Change `numCams`; nothing else in a config has to depend on it. A setting can be one value for every camera, or a list with one value per camera. A list longer than `numCams` uses its first items, so a config written for 8 cameras also runs 4. `configs/campy_config_flir_usb3.yaml` is an example.

Whether a computer keeps up with the cameras depends on its encoders and its USB buses. Measure the encoders once per computer:
```
campy-encode-sweep --config ./configs/campy_config_flir_usb3.yaml --encoder vaapi --encoder cpu:veryfast --streams 1,2,4,6,8
```
It runs 1, 2, 4... encoders at once, with campy's own ffmpeg settings, on synthetic frames, and saves how many each one keeps up with to `~/.campy/capacity.json`. The bitrates it reports come from those synthetic frames and are only a rough guide.

`quality` means different things to different encoders. libx264 and libx265 take it as a CRF, which adapts to the picture. NVENC, VA-API, AMF and Quick Sync take it as a constant quantizer (for AV1, a quantizer index from 0 to 255), which spends bits on everything, sensor noise included. On a GMKtec EVO-X2 (1280x1024 mono, 60 fps), quality 21 gave about 1.5 Mbit/s per camera on libx264 and 36.7 Mbit/s on VA-API: 132 GB per hour for 8 cameras. Measure what each setting costs before recording:
```
campy-encode-quality --config ./configs/campy_config_flir_usb3.yaml --input clip.mkv --quality 21,24,27,30
```
It encodes the clip with campy's own settings at each quality and reports the bitrate, GB per camera-hour, and PSNR and SSIM against the clip. Use a few seconds of the real scene, as close to lossless as you have (FFV1, or H.264 at `-qp 0`). Without `--input` it uses the sweep's synthetic frames, which show what sensor noise costs and little else. When cameras use different encoders, give `quality` one value per camera.

Every `campy-acquire` then starts with a launch check, printed before recording:
- **encoders**: ffmpeg has each camera's encoder, the VA-API device can be opened, and NVENC encodes a few test frames with each NVENC camera's settings ([NVIDIA GPU encoding](#nvidia-gpu-encoding-nvenc)).
- **capacity**: the cameras on each encoder, against the measured table. Cameras split over two GPUs (`gpuID` for NVENC and AMF, `vaapiDevice` for VA-API) count against each GPU's encoder, with the count the sweep measured on one GPU. It FAILs only at a camera count, or a pixel rate, the sweep found too slow. More cameras than it tried (`8+`: every count kept up), or a count between one that kept up and one that did not, is a WARN that says which `--streams` to measure. An encoder the sweep could not run at all is "not measured".
- **usb**: each camera's USB link and controller, and the camera data on each bus against a budget (300 MB/s for a 5 Gb/s bus, 600 MB/s for 10 Gb/s; set `usbBudgetMBps` once you have measured your own). A camera running at USB 2 speed fails. Linux has it all in sysfs. On Windows campy asks each camera's USB hub, which says how fast each link is but not how fast the controller is, so there a controller's budget is its fastest link's (300 MB/s with 5 Gb/s cameras), and going over it is a WARN, not a FAIL, unless `usbBudgetMBps` is set. On macOS it isn't checked.
- **camera**: each camera's `cameraSelection` picks a camera that was found (with three FLIR cameras connected, `cameraSelection` 3 FAILs). FLIR: each camera can run at `frameRate` with campy's settings. campy opens it, sets it up as a recording does (trigger, frame size, pixel format, exposure), reads the highest frame rate the camera says it can reach, and names what limits it when that is too low: the camera's USB throughput limit (`DeviceLinkThroughputLimit`), an exposure longer than a frame, or, on a triggered camera, `AcquisitionFrameRate` (a free-running camera is read without the `frameRate` cap campy gives it). A camera that cannot keep up does not stop by itself: free-running, it records more slowly than its video says; triggered, it misses triggers. It also checks that each camera sends frames of `frameWidth` x `frameHeight`, and FAILs with the ways out when it does not:
```
  FAIL  camera    Camera0 (24243505) would send 640x512 frames, not frameWidth x frameHeight 1280x1024, so its video would be scrambled. Either:
                    - unplug it and plug it back in: its frame size cannot be changed now
                    - set frameWidth: 640 and frameHeight: 512, the frames it sends now
```
- **disk**: free space for the recording.

With `preflight: "error"` (the default), a failed check stops campy before recording; `"warn"` records anyway; `"off"` (quoted or not) skips the check. `displayFrameRate: 0` runs without display windows, for example over SSH.

## Helpful tips
- To debug broken ffmpeg pipe error, include this in config.yaml:
```
ffmpegLogLevel: "warning"
```
- Use the command "ffmpeg" to check enabled packages. Hardware encoder support must be enabled in your ffmpeg binary.
- The Windows ffmpeg installed by Anaconda (conda-forge) has NVENC, but not Intel Quick Sync: see [Intel GPU encoding](#intel-gpu-encoding-quick-sync).
- On Linux, you may need to compile your own ffmpeg binary to enable encoders:
- Nvidia. nv-codec-headers' master is NVENC 13.1, which needs driver 610 or newer; for an older driver, clone a branch it supports, e.g. `git clone -b sdk/13.0 ...` for driver 570 or newer (each `sdk/` branch's README gives its minimum driver):
```
git clone https://git.videolan.org/git/ffmpeg/nv-codec-headers.git
cd nv-codec-headers && sudo make install && cd ..
```
- Intel:
```
sudo apt-get install libva-dev libmfx-dev libx264-dev libx265-dev libnuma-dev
```
- AMD:
```
git clone https://github.com/GPUOpen-LibrariesAndSDKs/AMF.git
sudo cp -r ./AMF/amf/public/include/core /usr/include/AMF
sudo cp -r ./AMF/amf/public/include/components /usr/include/AMF
```
- Compile ffmpeg:
```
git clone https://git.ffmpeg.org/ffmpeg.git
cd ffmpeg
sudo ./configure \
--enable-cuda --enable-cuvid --enable-nvdec --enable-nvenc --enable-nonfree \
--extra-cflags=-I/usr/local/cuda/include --extra-ldflags=-L/usr/local/cuda/lib64 \
--enable-gpl --enable-libx264 --enable-libx265 --enable-libmfx \
--enable-amf
sudo make -j -s
sudo cp -r ./ffmpeg /usr/bin
```
- Include in config.yaml:
```
ffmpegPath: "/usr/bin/ffmpeg"
```
- VA-API on Linux (`gpuMake: "vaapi"`: AMD GPUs through Mesa, or Intel). Ubuntu's own ffmpeg has it, so no compiling is needed:
```
sudo apt install ffmpeg mesa-va-drivers vainfo
sudo usermod -aG render $USER        # then log out and in
vainfo --display drm --device /dev/dri/renderD128   # should list VAEntrypointEncSlice for H264
```
  Then set `ffmpegPath: "/usr/bin/ffmpeg"` and, if the GPU is not `renderD128`, `vaapiDevice`.

  `codec: "av1"` uses `av1_vaapi`, on GPUs that encode AV1 (`vainfo` lists `VAProfileAV1Profile0` with `VAEntrypointEncSlice`). Its `quality` is AV1's quantizer index, 0-255, not the 0-51 QP of H.264 and H.265.

  On AMD GPUs, campy runs ffmpeg with `AMD_DEBUG=novideotiling` (`vaapiLinearSurfaces: True`, the default). Mesa otherwise keeps video surfaces tiled, so every frame is copied into one by a shader on the GPU's compute engine. On a Ryzen AI Max (Strix Halo) that engine misses interrupts: `journalctl -k` shows `amdgpu: Fence fallback timer expired on ring comp_...`, and each one stalls a camera's encoder for half a second. With linear surfaces the CPU writes each frame in directly, the compute engine does nothing, and the video is the same. Intel's driver ignores the setting.
