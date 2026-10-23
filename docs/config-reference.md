# campy-acquire settings

<!-- Generated from campy/configschema.py by `campy-config reference > docs/config-reference.md`. Edit the table there, not this page: a test fails when the two differ. -->

Every key a campy config file can have, and its command-line option (`--key value` means what `key: value` means in the file). "Per camera" says how a list is read: *one, or one per camera* takes one value for every camera or a list with a value for each, in camera order; *one for the rig* refuses a list. [Configs](configs.md) explains how campy checks a config, and `campy-config check` checks one without starting any camera.

## Recording

| Key | Default | Takes | Per camera | What it does |
|---|---|---|---|---|
| `numCams` | `1` | a whole number, 1 or more | one for the rig | Number of cameras. |
| `cameraNames` | from numCams | text | one per camera | Names assigned to the cameras in the order of cameraSelection. Default: Camera0, Camera1, ... |
| `cameraSelection` | from numCams | a whole number, 0 or more | one per camera | Selects and orders camera indices to include in the recording. List length must be equal to numCams. Default: 0, 1, ... |
| `videoFolder` | `"./test"` | text, as typed | one, or one per camera | Folder in which to save videos. Each run records into a new folder inside it, named by the date and time it started (sessionFolder). |
| `sessionFolder` | `True` | True or False | one, or one per camera | Record each run into a new folder inside videoFolder, named by the date and time it started, e.g. 2026-10-18_17-30-05 (default: True). False records into videoFolder itself, and stops if a recording is already there. |
| `sessionPath` | `"None"` | text, as typed | one for the rig | Record into this folder instead of a new one inside videoFolder (default: None). It is made if it is not there, and campy stops if a recording is already in it. For a program that starts campy-acquire and chose the folder itself (campy-session). |
| `stopFile` | `"None"` | text, as typed | one for the rig | Stop the recording, as Ctrl-C does, once this file is there (default: None); "now" in it is a second Ctrl-C. For a program that started campy-acquire, or a run started over SSH. campy stops at once if it is there already, and removes it once it has stopped. |
| `videoFilename` | `"0.mp4"` | text, as typed | one, or one per camera | Name for video output file. A name with "calibration" in it (0-calibration.mp4) is a calibration video: it goes into &lt;camera&gt;/calibration_images/. |
| `frameRate` | `100` | a number above 0 | one, or one per camera | Frame rate equal to trigger frequency. |
| `recTimeInSec` | `10` | a number above 0 | one for the rig | Recording time in seconds. |
| `keepAwake` | `True` | True or False | one for the rig | Keep the computer out of sleep and standby while recording, screen included (default: True). A Windows PC with Modern Standby otherwise goes into standby when its screen times out, and the recording slows to a crawl. |

## Cameras

| Key | Default | Takes | Per camera | What it does |
|---|---|---|---|---|
| `cameraMake` | `"basler"` | "basler", "flir", "ratcam" or "emu" | one, or one per camera | Company that produced the camera. Currently supported: 'basler', 'flir', 'ratcam' (nir-eye-cam), 'emu' (plays a video file). |
| `cameraSettings` | `"None"` | text, as typed | one, or one per camera | Path to camera settings file. |
| `frameWidth` | `1152` | a whole number, 1 or more | one, or one per camera | Frame width in pixels. |
| `frameHeight` | `1024` | a whole number, 1 or more | one, or one per camera | Frame height in pixels. |
| `cameraDebug` | `False` | True or False | one, or one per camera | Flag to turn on camera debug mode. |
| `cameraTrigger` | `"None"` | text | one, or one per camera | String indicating trigger input to camera (e.g. 'Line3'). |
| `cameraOut` | `2` | a whole number, 0 or more | one, or one per camera | Integer indicating camera output line for exposure active signal (e.g. 2). |
| `bufferMode` | `"OldestFirst"` | "OldestFirst", "NewestFirst", "NewestOnly" or "OldestFirstOverwrite" | one, or one per camera | Type of buffer to use in camera (default: 'OldestFirst'). |
| `bufferSize` | `100` | a whole number, 1 or more | one, or one per camera | Size of buffer to use in camera in frames (default: 100). |
| `cameraExposureTimeInUs` | `1500` | a whole number above 0 | one, or one per camera | Exposure time (in microseconds) for each camera frame. |
| `cameraGain` | `1` | a number, 0 or more | one, or one per camera | Intensity gain applied to each camera frame. The highest gain depends on the camera (18 dB on a Blackfly S BFS-U3-13Y3M). |

## FLIR cameras

| Key | Default | Takes | Per camera | What it does |
|---|---|---|---|---|
| `cameraGamma` | `0.6` | a number above 0, or "Off" | one, or one per camera | FLIR cameras: the gamma the camera applies to each frame (Gamma, with GammaEnable on): brightness out = brightness in ^ cameraGamma, so below 1 brightens the dark and middle grays and leaves black and white where they are. Default 0.6. 'Off' turns the camera's gamma off (GammaEnable False: brightness in proportion to the light, what disableGamma True did). A value the camera cannot take is set to its nearest limit (a Blackfly S takes 0.25 to 4), and metadata.csv's gamma says what the camera has. Basler cameras take gamma from their .pfs file. |
| `disableGamma` | replaced by `cameraGamma` | True or False | one, or one per camera | Replaced by cameraGamma, and read as it: True is cameraGamma 'Off', False is the default, 0.6. |
| `cameraLinkLimitMBps` | `"auto"` | a number above 0, or "auto" or "None" | one, or one per camera | FLIR USB3 cameras: the camera's USB throughput limit (DeviceLinkThroughputLimit). 'auto' (default) raises it to the camera's maximum when it is too low for frameRate; a number sets it in MB/s; 'None' leaves the camera's own limit. |

## RatCam (nir-eye-cam) cameras

| Key | Default | Takes | Per camera | What it does |
|---|---|---|---|---|
| `ratcamPorts` | `"None"` | text | a list | RatCam serial ports, e.g. ['/dev/ttyACM1']; 'sim' is a simulated camera. Default: every RatCam found by USB, in order of serial number. |
| `ratcamGain` | `0` | a whole number from 0 to 28 | one, or one per camera | RatCam analog gain code (the firmware's AGAIN): RP2040 0-3 (1x-8x), ESP32-P4 0-28. |
| `ratcamLed` | `0` | a whole number from 0 to 1000 | one, or one per camera | RatCam IR LED level, 0-1000 (ESP32-P4: on or off). 0 (the default) = off. |
| `ratcamSyncOut` | `False` | True or False | one, or one per camera | Drive the random sync pulse train on this RatCam's SYNC_OUT (one camera per rig). |

## Compression

| Key | Default | Takes | Per camera | What it does |
|---|---|---|---|---|
| `ffmpegLogLevel` | `"quiet"` | "quiet", "panic", "fatal", "error", "warning", "info", "verbose", "debug" or "trace" | one, or one per camera | Sets verbosity level for ffmpeg logging. ('quiet' (no warnings), 'warning', 'info' (real-time stats)). |
| `ffmpegPath` | `"None"` | text, as typed | one, or one per camera | Location of ffmpeg binary for imageio. |
| `pixelFormatInput` | `"rgb24"` | text | one, or one per camera | Pixel format input. Use 'rgb24' for RGB or 'bayer_bggr8' for 8-bit bayer pattern. |
| `pixelFormatOutput` | `"rgb0"` | text | one, or one per camera | Pixel format output. Use 'rgb0' for best results. |
| `colorRange` | `"limited"` | "limited" or "full" | one, or one per camera | Video color range: 'limited' (16-235, the default) or 'full' (0-255: every gray level of the camera, and less CPU for ffmpeg; raise quality by about 2 for the same file size). |
| `gpuID` | `-1` | a whole number, -1 or more | one, or one per camera | List of integers assigning the gpu index to stream each camera. Set to -1 to stream with CPU. |
| `gpuMake` | `"nvidia"` | "nvidia", "amd", "intel" or "vaapi" | one, or one per camera | Company that produced the GPU. Currently supported: 'nvidia', 'amd', 'intel' (QuickSync), 'vaapi' (Linux VA-API: AMD through Mesa, or Intel). |
| `codec` | `"h264"` | "h264", "h265" or "av1" | one, or one per camera | Video codec for compression. Currently supported: 'h264', 'h265' (hevc), and 'av1' with gpuMake 'vaapi', 'nvidia' or 'intel' (quality is then AV1's quantizer index, 0-255). |
| `quality` | `21` | a whole number from 0 to 255 | one, or one per camera | Compression quality: lower is less compression and larger files. libx264/libx265 take it as a CRF, which adapts to the picture; NVENC, VA-API and AMF as a constant quantizer, which spends bits on sensor noise too, so the same number gives much larger files there. One value, or one per camera. campy-encode-quality measures each setting. 0-51 for h264 and h265, 0-255 for av1. |
| `preset` | `"None"` | text | one, or one per camera | Compression preset (e.g. 'slow', 'fast', 'veryfast'). Incorrect settings may break the pipe. Test with ffmpegLogLevel 'warning' or 'info'. |
| `keyframeInterval` | `"None"` | a number above 0, or "None" | one, or one per camera | Seconds between keyframes (I-frames), e.g. 0.5. Shorter makes seeking and sparse frame sampling cheaper (LUCID's image ID-switch check needs 0.5) but files larger. Default: the encoder's own (NVENC: every 250 frames). |
| `fragmentedVideo` | `True` | True or False | one, or one per camera | Write .mp4 and .mov videos in fragments of at most a second (default: True), so that a video whose ffmpeg is stopped before it closes the file (a power cut, an OS crash, ffmpeg killed) is still readable up to its last fragment. A plain MP4 is then unreadable. False writes a plain MP4. |
| `vaapiDevice` | `"/dev/dri/renderD128"` | text, as typed | one, or one per camera | VA-API render device for gpuMake 'vaapi' (default: /dev/dri/renderD128). |
| `vaapiLinearSurfaces` | `True` | True or False | one, or one per camera | gpuMake 'vaapi' on AMD GPUs: keep video surfaces linear (Mesa's AMD_DEBUG=novideotiling), so each frame goes straight from the CPU into GPU memory instead of being copied there by the GPU's compute engine (default: True). |
| `writeQueueMaxSec` | `5` | a number above 0, or "None" | one, or one per camera | Most frames held waiting for the encoder, in seconds of video. When the encoder falls this far behind, new frames are dropped and counted (frametimes.npy shows the gaps, metadata.csv the count) instead of filling memory. 'None' = unbounded. |

## Live feed

| Key | Default | Takes | Per camera | What it does |
|---|---|---|---|---|
| `liveFeed` | `False` | True or False | one, or one per camera | Also copy every frame each camera grabs into shared memory, for a program that reads them live, such as campy-live (default: False). One copy per frame: campy never waits for the reader. |
| `liveFeedName` | `"campy-live"` | text, as typed | one, or one per camera | Name of the live feed's shared memory: camera n's is &lt;liveFeedName&gt;-&lt;n&gt; (default: campy-live). Two campys running at once each need their own. |
| `liveFeedSlots` | `8` | a whole number, 1 or more | one for the rig | Frames each camera's live feed holds (default: 8): how far behind a reader can be and still read a frame. |

## Launch check

| Key | Default | Takes | Per camera | What it does |
|---|---|---|---|---|
| `preflight` | `"error"` | "error", "warn" or "off" | one for the rig | Launch check of USB bandwidth, encoder capacity and disk: 'error' (stop if the configuration does not fit), 'warn' (report only) or 'off'. |
| `capacityFile` | `"None"` | text, as typed | one for the rig | Encoder capacity table written by campy-encode-sweep (default: ~/.campy/capacity.json). |
| `usbBudgetMBps` | `"None"` | a number above 0, or "None" | one for the rig | Most camera data per USB bus, in MB/s, for the launch check. Default: by bus speed. |

## Display and progress

| Key | Default | Takes | Per camera | What it does |
|---|---|---|---|---|
| `chunkLengthInSec` | `5` | a number above 0 | one, or one per camera | Length of video chunks in seconds for reporting recording progress. |
| `displayFrameRate` | `10` | a number, 0 or more | one, or one per camera | Display frame rate in Hz. Max ~30. 0 = no display window (headless). |
| `displayDownsample` | `2` | a whole number, 1 or more | one, or one per camera | Downsampling factor for displaying images. |

## Trigger

| Key | Default | Takes | Per camera | What it does |
|---|---|---|---|---|
| `triggerController` | `"arduino"` | "arduino", "Arduino", "None" or "none" | one for the rig | Microntroller make for camera triggering. Currently supported: 'arduino'. |
| `startArduino` | `False` | True or False | one for the rig | If True, start Arduino after initializing cameras. |
| `serialPort` | `"COM3"` | text | one for the rig | Serial port for communicating with Arduino. |
| `digitalPins` | `[0, 1, 2, 3, 4, 5, 6]` | a whole number, 0 or more | a list | Digital pins on microcontroller board for sending TTL camera triggers. |
| `readyTimeoutSec` | `30` | a number, 0 or more | one for the rig | Longest wait for every camera to be grabbing before campy starts the trigger anyway (and says which cameras were not ready). |
| `blinkPin` | `"None"` | a whole number, 0 or more, or "None" | one for the rig | Arduino pin driving a test LED that every camera can see, for campy-synctest. |
| `blinkHz` | `"None"` | a number, 0 or more, or "None" | one for the rig | Test LED blink rate. Pick one that is not near a whole or half multiple of the frame rate (e.g. 43 Hz at 25 or 60 fps). |

