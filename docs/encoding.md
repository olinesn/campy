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

`codec: "av1"` uses `av1_nvenc`, on GPUs whose NVENC encodes AV1: the Ada generation (RTX 40-series, RTX 2000-6000 Ada) and newer. Its `quality` is AV1's quantizer index, 0-255, not the 0-51 QP of H.264 and H.265. On an RTX 2000 Ada (1280x1024 mono, 80 fps, with `campy-encode-sweep`'s 1.25x headroom), NVENC kept up with 12 cameras in H.264 or H.265, and with 16 or more in AV1 at quality 88. On an RTX 4000 Ada (same settings, quality 24) it kept up with 28 cameras at 80 fps and 32 at 60, in H.264 and H.265 alike. There the encoder chip itself was the limit, at about 3,100 frames/s in all: it was 93-98% busy from 12 cameras on, and at 32-36 cameras a run could pass or fail at the same count, so the lower count is the one to count on. An RTX A2000 (the generation before Ada, so no AV1) was measured on 28 October 2026 at 60 fps (1280x1024 mono, quality 24, preset fast, same headroom): it kept up with 14 cameras in H.265, only just (76 fps against the 75 required), and with 12 in H.264; 16 and 14 were too slow. That is about 1,150 frames/s in all for H.265 and 990 for H.264, about a third of the RTX 4000 Ada. Its machine, a Dell Pro Tower Plus, records 8 FLIR Blackfly S cameras at 60 fps in H.265, a bit over half of that, and in a 20 s test recording with campy 2.2.0 every camera kept all 1,200 frames. On 29 October it moved to AV1 on its processor's Quick Sync (quality 88, preset faster), to leave the A2000 to live tracking: its encoder shares the card's 70 W limit with the models. In the same 20 s test every camera again kept all 1,200 frames, 16.67 ms apart, and the files came to 2.3-3.1 Mbit/s per camera against 5-7 for H.265 at quality 24 (the picture quality wasn't compared). While the 8 Quick Sync encoders started, each camera's write queue held 1.4-1.8 s of frames, well under `writeQueueMaxSec` (5 s there), and nothing was dropped.

## Intel GPU encoding (Quick Sync)
`gpuMake: "intel"` encodes on an Intel GPU with Quick Sync: `h264_qsv`, `hevc_qsv`, and for `codec: "av1"` `av1_qsv` (Arc GPUs, and the integrated GPUs of Core Ultra processors). It needs an ffmpeg built with Intel's oneVPL (libvpl). conda-forge's ffmpeg for Windows is not: 8.1.2 has no `_qsv` encoders. Set `ffmpegPath` to one that is, such as BtbN's builds above, which also have NVENC; `ffmpeg -hide_banner -encoders | findstr qsv` lists them. On Linux, `gpuMake: "vaapi"` (below) uses Intel GPUs through their media driver instead.

`quality` is Quick Sync's constant quantizer: campy passes `quality` + 1 for H.264 and H.265, and AV1's quantizer index, 0-255, as it is. Quick Sync takes NV12 frames (4:2:0, like yuv420p), so campy gives it NV12 for `pixelFormatOutput: "yuv420p"`; the video is the same.

On a Core Ultra 9 285's integrated GPU (1280x1024 mono, 80 fps, 1.25x headroom), H.264, H.265 and AV1 (quality 88) each kept up with 16 or more cameras, more than the RTX 2000 Ada beside it. On another Core Ultra 9 285 (a Dell Pro Tower Plus, 29 October; preset faster, the same headroom), Quick Sync kept up with 40 cameras at 30 fps in AV1 (quality 88), H.265 and H.264 (quality 24), and at 60 fps with 24 in AV1, 28 in H.265 and 20 in H.264. With 48 at 30 fps, some streams never got going. The RTX A2000 beside it kept up with 14 cameras at 60 fps in H.265 and 12 in H.264, and has no AV1 encoder: it is an Ampere card, and `av1_nvenc` there reports "No capable devices found". Encoding on the integrated GPU also leaves the NVIDIA card to `campy-live`: with 8 cameras and 5 mice, its time per set fell from 21.5 to 17.7 ms at 30 fps when the recording moved from the A2000's NVENC to Quick Sync ([Hardware guide](hardware.md)). Quick Sync and NVENC are separate encoders, so a computer with both can split its cameras between them (`gpuMake` and `gpuID` per camera, with an ffmpeg that has both). The launch check notes that the sweep measured each encoder alone. `configs/campy_config_chronicrecord.yaml` records FLIR cameras with Quick Sync H.265 on that computer.

AV1 needed 10-20% fewer bits than H.265 and H.264 for the same detail on a moving test scene, but it is harder to read back. SLEAP 1.6 reads video through OpenCV unless told otherwise. OpenCV's Linux wheels (4.13 and 5.0) cannot decode AV1 at all. Its Windows wheel reads AV1 at 60-100 frames/s, against about 400 for H.264 and H.265. SLEAP's PyAV reader decodes AV1 everywhere (`sleap-label --video-backend pyav`). LUC3D reads it in Chrome and Firefox on every computer tried, while Safari plays AV1 only on Macs with Apple M3 or newer ([AV1 in LUC3D](#av1-in-luc3d)).

## AV1 in LUC3D

LUC3D plays AV1 recordings in Chrome on every computer and GPU we tried, frame-exact, and in Firefox on a Mac. Safari on a Mac with an Apple M2 Pro can't play AV1 at all: LUC3D loads no video and says that Safari decodes AV1 only on Macs with Apple M3 or newer.

The test, on 29 October with LUC3D's main branch of that day: two 8-camera AV1 recordings from a Dell Pro Tower Plus (Quick Sync on its Core Ultra 9 285, quality 88, 1280x1024 mono, a keyframe every 0.5 s; 3,600 frames per camera at 30 fps and at 60 fps), and for comparison the original H.265 videos of the same scene. A script loaded each recording into LUC3D in Chrome 156 (on the Windows PCs over Remote Desktop), once on each GPU, and checked it against ffmpeg's decode of the same files.

| Computer | GPU that decoded the AV1 | Loads all 8 cameras, plays at 30 and 60 fps | Jump to a random frame |
|---|---|---|---|
| Dell Precision 7875 | RTX 6000 Ada or RTX 4000 Ada (hardware) | yes | about 0.1 s |
| Dell Pro Max Slim FCS1250 | RTX 2000 Ada (hardware) | yes | about 0.1 s |
| Dell Pro Max Slim FCS1250 and Dell Pro Tower Plus | the Core Ultra 9 285's integrated GPU (hardware) | yes | 0.2-0.25 s |
| Dell Pro Tower Plus | RTX A2000 (hardware) | yes | about 0.1 s |
| Windows 11 virtual machine | Intel Arc Pro B50 (hardware) | yes | 0.25-0.3 s |
| Any of these, hardware video decode turned off, or no GPU | the processor (software) | yes | 0.05-0.4 s |
| Mac with an Apple M2 Pro (Chrome and Firefox) | the processor (software) | yes | 0.1-0.15 s |

What held everywhere AV1 played:

- **Every frame was the right one.** For each recording, 15 jumps (to keyframes, the frames either side of them, and the first and last frames) landed on the frame ffmpeg gives that number in all 8 cameras, judged by the picture, and so did spot checks after 60 single steps and 10 more random jumps.
- **Playback mostly showed the right frame number.** In 46 of 48 Chrome playbacks on Windows, and all of them on the Mac, the picture matched the frame number LUC3D showed. In the other two (both on an Intel GPU, one AV1 and one H.265 recording) LUC3D switched to its fallback way of drawing, the videos dropped about two thirds of their frames, and the picture was 1-4 frames off the number in 7 and 8 of 34 samples. Firefox's picture was sometimes one frame ahead of the number, with H.265 too.
- **Gray levels were ffmpeg's.** Each pixel came within 1 level (of 255) of ffmpeg's decode, expanded from video's 16-235 to 0-255 as browsers show it, and on the Intel GPUs and in Chrome on the Mac exactly.
- **Software decoding is fast enough.** Chrome's software AV1 decoder did 800-1,150 frames/s for one camera, and 1,900-5,800 for 8 cameras at once (the least on the virtual machine's 8 processor threads); 8 cameras at 60 fps need 480. A step to the next frame took 2-8 ms (about 40 ms with no GPU at all).

Things to know:

- **Chrome may pick the integrated GPU.** On the Pro Max Slim, Chrome chose the Intel GPU over the RTX 2000 Ada by itself; on the Pro Tower Plus and the Precision it chose the NVIDIA card. The RTX A2000 can't encode AV1, but it decodes it.
- **Jumps depend on the keyframes, not the codec.** The H.265 originals have a keyframe every 250 frames and took 0.3-0.5 s per jump on the same GPUs. Set `keyframeInterval` (0.5 s here) for quick jumps in any codec.
- **Without a GPU, only AV1 opens.** With Chrome's hardware video decode off, or on a computer without a GPU, the AV1 recordings played as above, but the H.265 originals didn't load at all: Chrome has no software H.265 decoder.
- **Firefox steps more slowly.** On the Mac, Firefox stepped through AV1 at about 20 ms a frame and loaded a recording in 1-1.7 s (Chrome: 5-8 ms, 0.4 s). It can't decode H.265 frame by frame, so H.265 steps took about 0.2 s there.
- **Remote Desktop shows about 30 pictures a second.** Playback over Remote Desktop kept the videos' speed, but the screen updated only ~32 times a second.

Not tested: Linux, Microsoft Edge, Firefox on Windows, Safari on an M3 or newer Mac, and recordings longer than 2 minutes.

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
It runs 1, 2, 4... encoders at once, with campy's own ffmpeg settings, on synthetic frames, and saves how many each one keeps up with to `~/.campy/capacity.json`. The bitrates it reports come from those synthetic frames and are only a rough guide. Its `--codec`, like `campy-encode-quality`'s, takes the config's names only: `h264`, `h265` or `av1`. campy 2.2.0 and older passed another name, such as `hevc`, to ffmpeg as it was, which then encoded on the CPU even with `--encoder intel` or `nvidia`. Its row then showed `hevc/...` instead of `hevc_qsv/...` or `hevc_nvenc/...`.

`quality` means different things to different encoders. libx264 and libx265 take it as a CRF, which adapts to the picture. NVENC, VA-API, AMF and Quick Sync take it as a constant quantizer (for AV1, a quantizer index from 0 to 255), which spends bits on everything, sensor noise included. On a GMKtec EVO-X2 (1280x1024 mono, 60 fps), quality 21 gave about 1.5 Mbit/s per camera on libx264 and 36.7 Mbit/s on VA-API: 132 GB per hour for 8 cameras. Measure what each setting costs before recording:
```
campy-encode-quality --config ./configs/campy_config_flir_usb3.yaml --input clip.mkv --quality 21,24,27,30
```
It encodes the clip with campy's own settings at each quality and reports the bitrate, GB per camera-hour, and PSNR and SSIM against the clip. Since campy 2.5.2 it scores PSNR and SSIM together, in one ffmpeg pass per reference clip instead of one per score. That made the scoring about 45% quicker (0.22 s to 0.12 s for 150 frames of 1280x1024 on an Apple M2 Pro), with the same scores to every digit it prints. Use a few seconds of the real scene, as close to lossless as you have (FFV1, or H.264 at `-qp 0`). Without `--input` it uses the sweep's synthetic frames, which show what sensor noise costs and little else. When cameras use different encoders, give `quality` one value per camera.

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

With `preflight: "error"` (the default), a failed check stops campy before recording; `"warn"` records anyway; `"off"` (quoted or not) skips the check. `displayFrameRate: 0` runs without the cameras' window (campy-live-view), for example over SSH.

## Denoising before encoding (atadenoise)
campy has no denoise filter. Measured on 9 November 2026: a temporal denoiser (`atadenoise`) changes mouse pixels by a few gray levels at most, a SLEAP model trained on denoised video tracks undenoised video just as well, and the filter's real effect is on file size: on near-raw frames it made campy's files 4–33% smaller at the same quality setting, more for a still scene and more on NVENC than on Quick Sync, and AV1 recordings of mice 20–28% smaller with no measurable change in 2D or 3D tracking (see "With mice", below). Older campy code carries the line `-vf atadenoise=...` commented out in its NVENC settings; a setup that turns it on (`-vf atadenoise=s=5`, ffmpeg 4.2, H.265 NVENC at constant QP 24) is what these numbers compare against.

**What atadenoise does.** For each pixel it looks at the same pixel in the 2 frames before and the 2 after (`s=5`), and averages in each of those frames only while it differs from the pixel by at most 4 gray levels (of 255), and only while the differences on that side add up to at most 9. It stops at the first frame that differs more. The thresholds come from the defaults `0a=0.02` and `0b=0.04`: 0.02 × 256 − 1 = 4 and 0.04 × 256 − 1 = 9 for 8-bit video. So **no pixel can change by more than 4 gray levels**, wherever it is, and it can't leave a trail or a smear brighter than that. A still background is averaged over all 5 frames; a mouse's moving edge, which changes by tens of levels from frame to frame, isn't averaged at all.

**What it changes, measured.** Unfiltered recordings, scored pixel by pixel against themselves after the filter: four cameras of a 5-mouse recording (white mice on dark bedding, 1280 × 1024 mono, 30 fps, H.265 on Quick Sync at quality 26, about 1.9 Mbit/s per camera; five 32-frame windows each), and a 60 fps arena recording with a spinning fan. Gray levels of 255; "averaged" = pixels averaged with at least one other frame:

| Pixels | Mean change | 99th percentile | Largest | Averaged | Frames used, mean |
|---|---|---|---|---|---|
| background (more than 15 px from any mouse) | 0.05 | 1.1 | 3 | 100% | 5.0 |
| still mouse | 0.56 | 2.1 | 3 | 92% | 4.2 |
| moving mouse | 0.33 | 2.0 | 3 | 33% | 1.5 |
| mouse edge (7 px band) | 0.39 | 2.0 | 3 | 65% | 3.1 |
| where a mouse was or will be within 2 frames | 0.38 | 2.0 | 3 | 53% | 2.1 |

- **So it doesn't only touch the background.** A mouse that holds still is averaged like the background; a moving one mostly isn't. Either way the change is under one gray level on average and 3 at most. At 60 fps a moving animal changes less between frames than at 30 fps, so more of it would be averaged, still by at most 4 levels. The fan's blades were averaged only where they were flat and even.
- **Other settings.** `s=3` or `s=9` changed this little. Half the thresholds (`0.01/0.02`) changed no pixel by more than 1 level but removed almost no noise; double (`0.04/0.08`) changed moving mice by up to 7 levels (10% of their pixels by 4 or more).
- **Other filters.** `hqdn3d` (spatial and temporal) moved the background by about 0.5 level and mice by 1.2 on average, up to 4. Quick Sync's own denoise (`vpp_qsv=denoise=10` to `100` on a Core Ultra 9 285's graphics) moved mouse pixels by 0.5–1.7 on average and up to 4–9 levels (up to 1 of that from the conversion to the encoder's NV12 and back, which alone moves pixels by 0.14 on average), more than atadenoise for about the same noise removed (0.47 to 0.36–0.40 levels, atadenoise 0.43, on the same frames). `tmedian` (a 3-frame median) wiped out moving mice (up to 209 levels) and isn't a denoiser for animals. `nlmeans` is spatial only: it blurred fur and edges (up to 19 levels on mice).
- **Noise.** In these already-encoded recordings the still background's frame-to-frame noise was only 0.41 gray levels; the filter took it to 0.36. Encoders keep a still background still by themselves. In 60 fps arena recordings from the same camera, the old videos (filter + H.265 QP 24) had 0.27–0.54 levels of background noise and the new ones (AV1 on Quick Sync, quality 88, no filter) 0.50–0.67: both under one level.

**Tracking.** The same 720 frames (4 cameras × 3 windows × 60 frames of the 5-mouse recording), unfiltered and filtered, were run through a centroid model (input scale 0.25, output stride 2) and a centered-instance model (crop 320, input scale 0.5, max stride 32, 24 filters) trained mostly on 8-camera arena videos that had been through atadenoise (sleap-nn 0.3.5 on the CPU, up to 5 animals). To show how much keypoints move from any small change to a picture, the same frames were also encoded with AV1 on Quick Sync at quality 88 (the new arena settings), with and without the filter first.

| Compared | Animals found | Keypoint shift, median / 90th / 99th percentile | Keypoints moved > 1 px | > 5 px |
|---|---|---|---|---|
| unfiltered vs filtered | 3511 vs 3510 | 0.11 / 3.0 / 6.5 px | 16% | 1.9% |
| unfiltered vs AV1 | 3511 vs 3513 | 0.27 / 3.7 / 12.4 px | 31% | 4.5% |
| AV1 vs filter + AV1 | 3513 vs 3512 | 0.23 / 3.5 / 10.9 px | 28% | 3.8% |

The filter moves keypoints less than encoding does, the models found the same animals with the same scores (mean change under 0.001), and nothing suggests they need filtered video. Filtering before AV1 made this already-encoded clip's file only 2% smaller; see near-raw frames below.

**CPU cost.** ffmpeg runs the filter on the CPU, for each camera. Eight 1280 × 1024 gray streams paced at 60 fps, as 8 cameras would send them, CPU cores used by all 8 ffmpeg processes together (every case kept 60 fps, so the filter never held an encoder back). The EVO-X2 run had the machine to itself; on the Windows PC each run saw a burst or two of other CPU use (1–2 cores, from its endpoint security software), and two runs a few minutes apart agreed within half a core, so each cell gives both:

| Computer and encoder | No filter | atadenoise | atadenoise without SIMD |
|---|---|---|---|
| Dell Pro Max Slim FCS1250 (Core Ultra 9 285), AV1 on Quick Sync, quality 88 | 1.6–2.0 | 3.0 | 6.9–7.1 |
| the same, H.265 on its RTX 2000 Ada (NVENC, QP 24) | 1.2–1.3 | 1.7–2.1 | 5.1–5.3 |
| GMKtec EVO-X2 (Ryzen AI Max+ 395), H.264 on VA-API, QP 21 | 1.4 | 3.0 | 6.9 |

- "Without SIMD" (`-cpuflags 0`) is close to ffmpeg 4.2, whose atadenoise had no SIMD code: about 5 more cores for 8 cameras. ffmpeg 8.1 (and 6.1) costs about 1.5 more.
- One thread does the filter for about 0.9 ms a frame with SIMD and 3–4.5 ms without (0.45 and 1.5–2.2 cores for 8 cameras at 60 fps). Most of the extra cost above is ffmpeg's default of one filter thread per CPU core, for each camera. With `-filter_threads 1`, 8 cameras with atadenoise used 1.8 cores on Quick Sync (no filter in the same run: 1.8) and 1.6 on NVENC (no filter: 1.2–1.3). A filter added to campy should run that way.
- The Dell Pro Tower Plus has the same CPU and integrated graphics as the Pro Max Slim. Its RTX A2000 wasn't measured; NVENC's work is the same with or without the filter, since the filter runs before it.

**On the GPU instead.** None of ffmpeg's GPU filters does what atadenoise does:
- Quick Sync's denoise (`hwupload,vpp_qsv=denoise=30`) costs no more CPU than uploading the frames (8 streams: 2.2–2.4 cores, 60 fps), but changes mice more, as above. On Windows, `hwupload=extra_hw_frames=64` fails with "Could not create the texture"; plain `hwupload` works.
- `nlmeans_vulkan` managed 17 frames a second for one stream and `nlmeans_opencl` 58 (8 cameras at 60 fps need 480 in all), and both are spatial.
- `bilateral_cuda` keeps up (1,300 fps for one stream) but is spatial too.
- NVENC's temporal filter (`hevc_nvenc -tf_level 4`) needs B-frames, which campy's GPU encoders don't use, and with them it cut one stream from 1,600 to 415 fps.
- VA-API's `denoise_vaapi` isn't implemented by AMD's Mesa driver ("the requested function is not implemented").

**On near-raw frames.** The recordings above had already been encoded once at an everyday quality, which removes most of the sensor noise before the filter sees it, so they understate what the filter does. Recordings of a spinning fan from a 1.3 MP mono camera (1280 × 1024, 120 fps, H.265 at constant QP 5, about 176 Mbit/s, which keeps nearly all the noise; three fan speeds, 2 s each, also taken every other frame as 60 fps) show what it does to frames close to what the camera sends. Gray levels of 255; "moving" = pixels that change by more than 12 levels within the 5 frames the filter looks at; file size = the same frames with and without the filter, encoded with libx265 at constant QP 24 and no B-frames (a CPU stand-in for NVENC at QP 24):

| Fan, 60 fps | Still background noise, before → after | Moving pixels: mean change / largest / averaged | File size with the filter |
|---|---|---|---|
| slow | 1.46 → 1.09 | 0.35 / 3 / 45% | −30% |
| medium | 1.19 → 0.90 | 0.34 / 3 / 44% | −17% |
| fast | 0.82 → 0.61 | 0.29 / 3 / 40% | −7% |

At 120 fps the numbers were nearly the same (−32%, −19%, −7%). Half the thresholds (`0.01/0.02`) changed no pixel by more than 1 level and saved only 2%; double (`0.04/0.08`) removed most of the noise (to 0.3–0.4) but moved pixels by up to 7 levels.

The same clips through campy's own encoders on the Dell Pro Max Slim (BtbN ffmpeg 8.1, `-filter_threads 1`, 60 fps, 3 s each), bitrate without → with the filter:

| Fan, 60 fps | AV1 on Quick Sync, quality 88, preset faster | H.265 on NVENC (RTX 2000 Ada), QP 24, preset fast | H.265 on Quick Sync, quality 26 |
|---|---|---|---|
| slow | 1.28 → 1.01 Mbit/s (−21%) | 1.95 → 1.31 (−33%) | 1.08 → 0.84 (−22%) |
| medium | 1.60 → 1.42 (−11%) | 2.23 → 1.75 (−22%) | 1.42 → 1.24 (−13%) |
| fast | 2.39 → 2.29 (−4%) | 3.06 → 2.73 (−11%) | 2.09 → 1.97 (−6%) |

At 120 fps the savings were within 2 points of these. NVENC at a constant QP spends the most bits on noise, so it gains the most; Quick Sync gains about two-thirds as much. AV1 on Quick Sync without the filter already wrote smaller files than H.265 on NVENC with it, for every fan speed.

So on real recordings the filter mainly buys smaller files: roughly a tenth to a fifth on Quick Sync and a fifth to a third on NVENC for a quiet scene, less with more movement. It costs changes of at most 3 levels on anything that moves and, in these tests, nothing in tracking. Raising `quality` saves bits too, but on every pixel.

**With mice, on near-raw frames (9 November 2026).** Two white mice in the 8-camera arena on the Dell Pro Tower Plus (no bedding, a dark floor), 4 of its cameras recorded for 65 s at 60 fps with H.265 on its RTX A2000 at constant QP 5: about 200–245 Mbit/s per camera, which keeps the sensor noise (about 1.9 gray levels in the still background, at gain 5 dB). All 8 cameras at QP 5 were too much for the A2000's encoder: 6 of them got only about 45 frames a second into their encoders and dropped 15–17% of their frames, so the test used 4 (no frame dropped).

The filter on these frames (six 40-frame windows per camera; gray levels of 255):

| Pixels, 4 cameras | Mean change | Largest | Averaged |
|---|---|---|---|
| background | 0.59–0.84 | 3 | 88–90% |
| still mouse | 0.70–0.79 | 3 | 85–88% |
| moving mouse | 0.34–0.38 | 3 | 34–37% |
| mouse edge (7 px band) | 0.56–0.64 | 3 | 61–71% |

It took the background noise from 1.8–2.0 levels to 1.4–1.5.

AV1 as the arena records it (Quick Sync on the Dell Pro Max Slim FCS1250, which has the same Core Ultra 9 285 graphics; quality 88, preset faster, no B-frames, a keyframe every 0.5 s, `-filter_threads 1`), 10 s of each camera:

| Camera | AV1 | atadenoise + AV1 | atadenoise at half thresholds + AV1 |
|---|---|---|---|
| top B | 1.63 Mbit/s | 1.17 (−28%) | −1.5% |
| mid | 2.29 | 1.77 (−23%) | −1.2% |
| side C | 2.08 | 1.64 (−21%) | −1.0% |
| top R | 2.19 | 1.75 (−20%) | −0.9% |

**A longer window** (`s=9` or `s=13`: 4 or 6 frames each side instead of 2) saved little more, because the sum limit (9 levels a side) usually stops the averaging before the extra frames. Same clips and AV1 settings, and CPU on the Dell Pro Max Slim FCS1250 with `-filter_threads 1` (one burst of other CPU use flagged; every case kept 60 fps):

| | no filter | `s=5` | `s=9` | `s=13` |
|---|---|---|---|---|
| AV1 file size, 4 cameras | – | −20 to −28% | −21 to −29% | −21 to −29% |
| filter time per frame, one thread | – | 0.50 ms | 1.09 ms | 2.54 ms |
| CPU cores, 8 cameras at 60 fps with AV1 | 1.2–1.5 | 2.0 | 2.1 | 3.3 |

`s=9` saved 1.1–1.5 points more than `s=5` and `s=13` another 0.2, so `s=5` is the one to use. What the filter did to the pixels hardly changed with the window either (the same 4 cameras, six 50-frame windows each, checked pixel for pixel against ffmpeg's own output): no pixel changed by more than 3 levels at any `s`, mice changed by the same 0.35–0.79 levels on average to within 0.01, and the background noise came out the same (1.4–1.5 levels).

AV1 at quality 88 changes pixels far more than the filter does: 1.8–2.1 gray levels on average in the background and 2.0–3.2 on mice, against the near-raw frames. With the filter first, the AV1 frames were just as close to the near-raw ones (within 0.05 level in every region, every camera). The filtered and unfiltered AV1 files differ from each other by 1–2.5 levels on average, mostly because the encoder makes different choices once the noise is gone.

Tracking (the same centroid and centered-instance models as above, sleap-nn 0.3.5 in fp32 on an NVIDIA GPU, up to 5 animals; 600 frames per camera), keypoint shift against the near-raw frames, median / 90th / 99th percentile:

| Camera | AV1 | atadenoise + AV1 |
|---|---|---|
| top B | 0.24 / 4.1 / 37 px | 0.26 / 4.3 / 39 px |
| mid | 0.36 / 6.3 / 92 px | 0.39 / 6.6 / 99 px |
| side C | 0.37 / 5.0 / 36 px | 0.41 / 5.0 / 39 px |
| top R | 0.26 / 4.4 / 16 px | 0.26 / 4.4 / 15 px |

The large 99th percentiles are frames where a mouse is hard to see in that camera, with or without the filter. The filter alone, on the near-raw frames, shifted keypoints by a median of 0.06–0.11 px.

In 3D (the 4 cameras matched and triangulated by campy's own code, `campy/pose3d.py`, with a calibration from 4 months earlier), the views agreed as well with the filter as without it: median reprojection error 10.5 px on the near-raw frames, 10.7 with AV1 and 10.8 with atadenoise + AV1 (99th percentile 29.6, 29.0 and 28.3), and 57–60% of the mouse-sets had 3D in each. The 3D points of AV1 and of atadenoise + AV1 were 1.3 mm from the near-raw ones (median; 14 mm at the 90th percentile). On the near-raw frames alone (1,200 sets, 20 s), the filter left the views' agreement where it was (median reprojection error 10.7 px unfiltered, 10.5 filtered) and moved the 3D points by 0.2 mm (median). The reprojection error itself says the calibration no longer fits these cameras exactly, so a difference under about half a pixel would not show.

So for AV1 on Quick Sync, the filter makes files about a fifth to a quarter smaller on this rig with mice, at no measurable cost to tracking in 2D or 3D, and, run on one thread, for about the CPU of no filter.

**Not yet measured.** White mice on light bedding (the hardest case for telling fur from bedding; the filter still can't move any pixel by more than 4 levels there), more than two mice, and longer stretches.

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
