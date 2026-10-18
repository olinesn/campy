# campy
- Python package for acquiring synchronized video from multiple cameras with real-time compression

## Hardware/Software Recommendations
- Basler and/or FLIR machine vision camera(s)
- Windows, IOS, or Linux system
- (Recommended) Server/workstation class CPUs with >=4 memory channels (e.g., AMD Threadripper 3955WX-9955WX, Intel i9-10900X, Xeon) can increase bandwidth over consumer CPUs with 2 memory channels (more cameras, higher resolution/frame rate).
- (Recommended) High core count CPUs are not necessary, but generally >1 core per camera is recommended, as campy leverages one process per camera using multiprocessing for parallelism.
- (Optional) USB expansion card with 1 host controller per camera (e.g., Basler USB 3.0, 4X PCIe, 4X HC, 4 Ports PN# 2000036233).
- (Optional) Hardware encoder using AMD, Intel, or Nvidia GPU (see https://developer.nvidia.com/video-encode-decode-gpu-support-matrix).
- (Optional) Arduino/Teensy/Pi microcontroller for syncing cameras and other devices.

## Installation
1. Update graphics drivers
2. Create and activate a new Anaconda environment with Python 3.12. campy needs Python 3.9 or newer, and 3.12 is the version it is set up for: FLIR's PySpin (Spinnaker 4.4) is built for Python 3.12 on Windows, macOS and Ubuntu 24.04. On Ubuntu 22.04, PySpin is built for Python 3.10 only, so use `python=3.10` there.
```
conda create -n campy python=3.12 imageio-ffmpeg ffmpeg=8.1 -c conda-forge
conda activate campy
pip install -U setuptools
```
To encode on an NVIDIA GPU, check this ffmpeg first: see [NVIDIA GPU encoding (NVENC)](#nvidia-gpu-encoding-nvenc) below.

3. Install camera software
- If using Basler cameras, install Pylon software:
  - Install Basler Pylon with Developer options
  - Install pypylon:
  Windows:
  ```
  pip install pypylon
  ```
  Linux:
  ```
  conda install swig
  git clone https://github.com/basler/pypylon.git
  python ./pypylon/setup.py install
  ```
  
- If using FLIR cameras:
  - Download and install Spinnaker SDK and SpinView software from FLIR's website: 
    https://www.flir.com/support-center/iis/machine-vision/downloads/spinnaker-sdk-and-firmware-download/
    On Windows, choose the "Application Development" profile: it installs the camera drivers and Visual C++ runtimes PySpin needs. If the installer fails, see [Spinnaker 4.4 on Windows: when the installer fails](#spinnaker-44-on-windows-when-the-installer-fails).
  - Manually install binary wheel for PySpin (included in the Spinnaker download). It must match your Python, by its cpXY tag (cp312 for Python 3.12), and the Spinnaker SDK you installed, by its version: a mismatch fails at `import PySpin`. E.g. for Spinnaker 4.4.0.246 and Python 3.12, install "spinnaker_python-4.4.0.246-cp312-cp312-win_amd64.whl" on Windows, or "spinnaker_python-4.4.0.246-cp312-cp312-linux_x86_64.whl" on Ubuntu 24.04 (x86_64). PySpin for Python 3.12 needs NumPy 2.x, and for Python 3.10 NumPy 1.x (`pip install "numpy<2"`); see the README in the PySpin download. On Windows and macOS, PySpin for Python 3.12 needs Spinnaker 4.4 or newer: 4.4.0.246 added it, so an older Spinnaker (e.g. 4.2) has to be replaced by 4.4, SpinView included.
  ```
  pip3 install <wheel>
  ```
  - An environment with an older PySpin (e.g. Spinnaker 2.x's, for Python 3.7) works only with that older SDK, so upgrade a computer's Spinnaker and its campy environment together.
  - On Linux, have `campy-flir-reset` run at startup, so that a camera left plugged in through a restart does not stay at 43 MB/s: see [FLIR cameras at 43 MB/s after a restart](#flir-cameras-at-43-mbs-after-a-restart).
4. Create a folder to clone campy:
```
cd ~\Documents\
mkdir campy
cd campy
```
5. Clone campy to that folder. `master` is the released campy; `dev`, the repository's default branch, holds work not released yet (see [Development and releases](#development-and-releases)):
```
git clone -b master https://github.com/olinesn/campy.git
```
6. Navigate to the cloned campy folder
```
cd campy
```
7. Finally, install campy and its dependencies (see setup.py):
```
pip install -e .
```

### NVIDIA GPU encoding (NVENC)
`gpuMake: "nvidia"` needs an ffmpeg that is 4.4 or newer and is built for an NVENC version the NVIDIA driver supports.
- ffmpeg 4.3 and older ask the driver for NVENC presets with a call that NVIDIA drivers from 591.44 (December 2025) reject for H.264 and HEVC. Every camera's writer then stops with a broken pipe, and ffmpeg's error is `Cannot get the preset configuration: unsupported param (12)`. campy's install instructions used to pin `ffmpeg==4.2.2`, so environments made with them have this problem. A standalone ffmpeg set with `ffmpegPath` (below) fixes it and leaves the environment as it is.
- An ffmpeg built for a newer NVENC version than the driver supports fails the other way, and says what it needs: `Driver does not support the required nvenc API version. Required: 13.1 Found: 13.0`, then `The minimum required Nvidia driver for nvenc is 610.00 or newer`. Update the driver, or use an ffmpeg built for the NVENC version the driver has ("Found").

conda-forge's ffmpeg comes in builds for several NVENC versions, and conda picks one the driver supports when it creates the environment, by the CUDA version `nvidia-smi` shows: 12.8 or newer (driver 570 or newer) gets NVENC 13.0, and 12.2 to 12.7 an older NVENC version. A driver older than 535 (CUDA below 12.2) gets an ffmpeg without NVENC, and campy's launch check stops with "has no hevc_nvenc encoder": update the driver, or set `ffmpegPath` to an ffmpeg built for an older NVENC version.

The command above pins ffmpeg 8.1. Against 4.2.2, campy's tests pass, and libx264 writes the same video from the same frames, except that a frame size padded to a multiple of 16 is rescaled slightly differently (by at most one gray level). On NVENC, `-preset fast` now means P1, NVENC's fastest preset, and video from RGB input is tagged with the conversion NVENC applies (BT.601, limited range), which 4.2.2 left untagged.

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

`codec: "av1"` uses `av1_nvenc`, on GPUs whose NVENC encodes AV1: the Ada generation (RTX 40-series, RTX 2000-6000 Ada) and newer. Its `quality` is AV1's quantizer index, 0-255, not the 0-51 QP of H.264 and H.265. On an RTX 2000 Ada (1280x1024 mono, 80 fps, with `campy-encode-sweep`'s 1.25x headroom), NVENC kept up with 12 cameras in H.264 or H.265, and with 16 or more in AV1 at quality 88.

### Intel GPU encoding (Quick Sync)
`gpuMake: "intel"` encodes on an Intel GPU with Quick Sync: `h264_qsv`, `hevc_qsv`, and for `codec: "av1"` `av1_qsv` (Arc GPUs, and the integrated GPUs of Core Ultra processors). It needs an ffmpeg built with Intel's oneVPL (libvpl). conda-forge's ffmpeg for Windows is not: 8.1.2 has no `_qsv` encoders. Set `ffmpegPath` to one that is, such as BtbN's builds above, which also have NVENC; `ffmpeg -hide_banner -encoders | findstr qsv` lists them. On Linux, `gpuMake: "vaapi"` (below) uses Intel GPUs through their media driver instead.

`quality` is Quick Sync's constant quantizer: campy passes `quality` + 1 for H.264 and H.265, and AV1's quantizer index, 0-255, as it is. Quick Sync takes NV12 frames (4:2:0, like yuv420p), so campy gives it NV12 for `pixelFormatOutput: "yuv420p"`; the video is the same.

On a Core Ultra 9 285's integrated GPU (1280x1024 mono, 80 fps, 1.25x headroom), H.264, H.265 and AV1 (quality 88) each kept up with 16 or more cameras, more than the RTX 2000 Ada beside it. Quick Sync and NVENC are separate encoders, so a computer with both can split its cameras between them (`gpuMake` and `gpuID` per camera, with an ffmpeg that has both). The launch check notes that the sweep measured each encoder alone. `configs/campy_config_chronicrecord.yaml` records FLIR cameras with Quick Sync H.265 on that computer.

AV1 needed 10-20% fewer bits than H.265 and H.264 for the same detail on a moving test scene, but it is harder to read back. SLEAP 1.6 reads video through OpenCV unless told otherwise. OpenCV's Linux wheels (4.13 and 5.0) cannot decode AV1 at all. Its Windows wheel reads AV1 at 60-100 frames/s, against about 400 for H.264 and H.265. SLEAP's PyAV reader decodes AV1 everywhere (`sleap-label --video-backend pyav`). LUCID reads it in Chrome and Firefox, while Safari plays AV1 only on Macs with Apple M3 or newer.

### Full-range video (colorRange)
`colorRange: "full"` puts a camera's gray levels, 0-255, into the video as they are. With the default, `"limited"`, ffmpeg first squeezes them into video's usual 16-235, rescaling every pixel. Full range keeps all 256 levels and is marked in the file, so readers decode the camera's own levels back. Every reader tried gave each level of a 0-255 ramp back to within 0.2: OpenCV 4.6 and 4.12, PyAV, imageio-ffmpeg, sleap-io 0.5.3 (its OpenCV, FFMPEG and PyAV backends) and Chrome as LUCID decodes video, for H.264 and H.265 from Quick Sync and NVENC, and H.264, H.265 and AV1 from VA-API. Chrome was checked on a Mac, and on Windows with H.264 only: there it decodes H.265 on the GPU, which an SSH session cannot reach. The same ramp in limited range came back up to 2 levels darker through OpenCV and sleap-io.

It also spares ffmpeg most of its CPU work for a GPU encoder, which was that rescaling. Per 1280x1024 mono frame, Quick Sync H.265 took 1.7 ms instead of 3.3 (ChronicRecord PC, ffmpeg 8.1), and VA-API H.265 1.4 ms instead of 5.8 (EVO-X2, Ubuntu's ffmpeg 6.1): for 16 cameras at 80 fps, about two cores fewer on the first and five on the second.

At the same `quality` a full-range video is about 20% larger, since the extra levels are detail the encoder keeps. At the same size its picture is slightly better (+0.3 dB PSNR on a moving scene, Quick Sync H.265), so raise `quality` by about 2 to keep file sizes as they were: quality 26 in full range made a 9% smaller file than 24 in limited range, and scored 0.2 dB higher.

It applies to every encoder, except NVENC given `pixelFormatOutput: "rgb0"`, which converts RGB to limited range itself (campy says so: use `"yuv420p"`). campy converts the range rather than only marking the file: before ffmpeg 7.1, ffmpeg marked such a file full range but left its levels squeezed, so readers decoded black as 16 and white as 235.

### Spinnaker 4.4 on Windows: when the installer fails
On one Windows 11 PC (October 2026), Spinnaker 4.4.0.246's installer failed the same way every time. Teledyne Common Components installed, then the installer's administrator process went away and nothing else installed: no SpinView, and PySpin found 0 cameras, although Settings ▸ Apps listed "Teledyne Spinnaker SDK 4.4.0.246". Its log in `%TEMP%` (`Teledyne_Spinnaker_SDK_4.4.0.246_(x64)_*.log`) shows `Applied execute package: TeledyneCommonComponentsSetup.exe, result: 0x0`, then `Error 0x800700e8: Failed to write message type to pipe`. No crash or Microsoft Defender event explained it. By then the installer has saved its packages under `C:\ProgramData\Package Cache`, so they can be installed directly, in PowerShell run as administrator:

1. Install the packages with their features. Without `ADDLOCAL` each package installs no features at all and still exits with 0, so the loop prints what each one installs (Binaries' list should include `SpinView_v140`, Drivers' `USBDrivers`). `USBDrivers,USBDriversLegacy` leaves out the GigE drivers; use `ALL` for GigE cameras.
```powershell
$cache = "C:\ProgramData\Package Cache"
$parts = [ordered]@{ VCRedist_v140_x64 = "ALL"; Spinnaker_GenTL_v140_x64 = "ALL"; Spinnaker_Binaries_v140_x64 = "ALL"; Spinnaker_GenICam_v140_x64 = "ALL"; Spinnaker_Drivers_x64 = "USBDrivers,USBDriversLegacy" }
foreach ($n in $parts.Keys) {
  $msi = Get-ChildItem $cache -Recurse -Filter "$n.msi" | Where-Object FullName -like "*v4.4.0.246*" | Select-Object -First 1
  $log = "$env:TEMP\spin440-$n.log"
  $p = Start-Process msiexec.exe -ArgumentList "/i `"$($msi.FullName)`" ADDLOCAL=$($parts[$n]) /qb /norestart /l*v `"$log`"" -Wait -PassThru
  $local = (Select-String -LiteralPath $log -Pattern 'Feature: (\S+);.*Action: Local' | ForEach-Object { $_.Matches[0].Groups[1].Value }) -join ', '
  "$n -> exit code $($p.ExitCode); installing: $local"
}
```
2. Register the USB3 camera driver with Windows. The drivers package copies it to `C:\Program Files\Spinnaker\driver64`, but outside the full installer nothing registers it, so the camera stays a "USB3 Vision Device" with an error. Older USB 2 cameras use `PGRUsb\pgrusbcam.inf` instead.
```powershell
pnputil /add-driver "C:\Program Files\Spinnaker\driver64\PGRUsb3\PGRUSBCam3.inf" /install
pnputil /scan-devices
Get-PnpDevice -PresentOnly | Where-Object InstanceId -match 'VID_1E10' | Format-Table Status, Class, FriendlyName
```
   The camera should then be `OK`, as a "USB3 Vision Camera" of class `PGRDevices`, and PySpin should find it:
```powershell
python -c "import PySpin; s = PySpin.System.GetInstance(); c = s.GetCameras(); print('cameras', c.GetSize()); c.Clear(); s.ReleaseInstance()"
```
3. SpinView is then in the Start menu (`C:\Program Files\Spinnaker\bin64\vs2015\SpinView_WPF_v140.exe`). The half-installed "Teledyne Spinnaker SDK 4.4.0.246" may reopen its wizard at the next login: close it with Exit, since running it again fails the same way. Leave its Settings ▸ Apps entry alone too, as its uninstaller may remove the packages installed above.

### FLIR cameras at 43 MB/s after a restart
A Blackfly S sets its USB throughput limit (`DeviceLinkThroughputLimit`) to 43 MB/s whenever it connects at USB 2 speed (FLIR's release notes for firmware 1808, item F-300). A computer's restart leaves its USB ports powered, so a camera that stays plugged in keeps running through it, and the computer's startup connects it at USB 2 and then moves it to USB 3. The camera never restarts, so it keeps the USB 2 limit on its USB 3 link until it is reset or unplugged, although every one of its saved settings says 380 MB/s. A 1280x1024 Mono8 camera then runs at 32.8 fps instead of 170. campy raises such a limit itself (`cameraLinkLimitMBps`), but SpinView and other programs get 43 MB/s.

Measured on a GMKtec EVO-X2 (Ubuntu 24.04, AMI BIOS 1.09) with a BFS-U3-13Y3M on firmware 1808.0.120.0, the newest FLIR lists for it, in October 2026. Whether the camera restarted comes from its `DeviceUptime`:

| | Limit afterwards | Camera restarted |
|---|---|---|
| Restart (`sudo reboot`) | 43 MB/s, all 5 times | no: its uptime ran on through the restart, and it was connected twice before Linux started |
| Its USB 3 port turned off (`usb2-port1/disable`), then on again | 43 MB/s | no: it moved to USB 2 and back |
| Shut down, then switched on | 380 MB/s | yes, at power-on |
| Shut down, power cord out for 10 s, switched on | 380 MB/s | yes, at power-on |
| `DeviceReset`, or unplugging the camera | 380 MB/s | yes |

So on this computer, shutting down and switching on clears it, and a restart does not. A computer that keeps its USB ports powered while shut down (a BIOS setting on some) would keep the camera running through that too.

`campy-flir-reset` resets every FLIR camera that is on a USB 3 link with a limit no higher than USB 2's 480 Mbit/s (60 MB/s). A reset (`DeviceReset`) is the same as unplugging the camera: it restarts with its saved settings, and is back about 8 s later. Cameras on a USB 2 link, and cameras with a higher limit, are left alone. A camera that saved a limit that low is reset too, and comes back with it. On Linux, a systemd service can run it at every startup, before anything opens the cameras, so every program finds the saved limit. Put the campy environment's `bin` folder (`dirname $(which campy-flir-reset)` with the environment active) and your user name in it:
```
sudo tee /etc/systemd/system/campy-flir-reset.service <<'EOF'
[Unit]
Description=Reset FLIR USB3 cameras a restart left at their USB 2 throughput limit
After=systemd-udev-trigger.service

[Service]
Type=oneshot
User=<your user name>
ExecStart=<campy environment>/bin/campy-flir-reset
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now campy-flir-reset.service
journalctl -b -u campy-flir-reset.service    # what it found and did at this startup
```
The user needs the camera access that Spinnaker's installer sets up on Linux (group `flirimaging`); if a FLIR camera is plugged in but Spinnaker does not see it, the service says so. On the EVO-X2, after each of two restarts, it found the camera at 43 MB/s 7 s into startup and had it back at 380 MB/s by 14.4 s; after both shutdowns it found 380 MB/s and left the camera alone. The login screen came up no later than without it (22.5 s after the kernel started, against 23.3 s). `campy-flir-reset` also runs by hand, on Windows too, but the restart behaviour above was not measured on Windows.

## Usage

### Configuration
- For Basler cameras, use the Pylon Viewer to configure and save your '.pfs' camera settings file. Examples are included in campy/configs.
- A Basler camera's video has the frame size its '.pfs' file gives the camera, and an emulated camera's (`cameraMake: "emu"`) its clip's. campy says so when that is not `frameWidth` x `frameHeight` (`Camera0: frameWidth x frameHeight 1152x1024 changed to 1280x1024, the camera's frame size with its cameraSettings.`). Each video opens once its camera is open: it used to open first, at `frameWidth` x `frameHeight`, and frames of another size scrambled it. A camera that fails to open gets no video.
- Edit the config.yaml file to fit your system and recording configuration.
- Several example config files are located in campy/configs.
- For help setting config parameters:
```
campy-acquire --help
```

### Camera Triggering
Campy's trigger module supports Python integration with Arduino and Teensy microcontrollers:
1. Download Arduino IDE (https://www.arduino.cc/en/software). If using Teensy, install Teensyduino (https://www.pjrc.com/teensy/teensyduino.html).
3. Connect your microcontroller via USB and note the serial port number (e.g., "COM3" in Windows Device Manager or "/dev/ttyACM0" on Linux).
4. Connect the camera GPIO wires to the microcontroller digital outputs. Note the digital pin numbers (e.g., DO1, DO2, etc.) for each camera. For best results, securely solder the pins. Be careful to use the correct voltage recommended by the camera manufacturer.
5. In your campy config.yaml, configure:
```
startArduino: True
cameraTrigger: "Line0" # or "Line3", whichever color wire you used, but be consistent!
digitalPins: [<pin IDs>] # e.g., [0,1,2], or 2 for one pin
serialPort: "<port>" # e.g., "COM3" or "/dev/ttyACM0"
triggerController: "arduino"
```
4. Open and upload the "trigger.ino" file (in campy/trigger folder) to your board. Make sure the baudrate is set to 115200, and the serial monitor is closed while using pyserial connection.
5. Campy can now communicate with the microcontroller to synchronously trigger the cameras once acquisition has initialized: it opens every camera first, and starts the trigger when all of them are grabbing (it prints "All N cameras are ready."), so every camera's first frame is the trigger's first pulse. A camera that is not grabbing within `readyTimeoutSec` (default 30 s) is named, and the trigger starts for the others.

Alternatively, to configure campy for external triggering:
Start campy before triggering. Campy will wait for external triggers to initialize; start them once it prints "All N cameras are ready.", and every camera's first frame is the first trigger. To stop the recording, either configure a set recording time in campy or stop triggers before manually exiting campy (see Stop Recording below). 
```
startArduino: False
cameraTrigger: "Line0" # or "Line3"
```

Without a trigger (`cameraTrigger: "None"`), a FLIR camera free-runs: it times its own frames, and campy sets its `AcquisitionFrameRate` to `frameRate`. Left alone, it would run at whatever rate its saved settings say (a Blackfly S at full frame: 170 fps), while campy still stops after `frameRate` × `recTimeInSec` frames and stamps the video at `frameRate`. campy turns on `AcquisitionFrameRateEnable` (on older Point Grey models such as the Grasshopper3 and Chameleon3, `AcquisitionFrameRateEnabled`, with `AcquisitionFrameRateAuto` Off), sets the rate, reads back the rate the camera will run at, and records it in "metadata.csv" as `resultingFrameRate`. `AcquisitionFrameRate` only caps the rate: an exposure longer than a frame, the camera's USB throughput limit, or its sensor's readout can hold it below `frameRate`, and campy then says which, and at what rate it will record (the launch check stops before that; see below). When it closes the camera, campy puts back the frame rate settings it found: a camera keeps them until it is unplugged, and with the cap on it drops triggers that come faster, so a triggered recording after a free-running one would be held to its `frameRate`. Before it changes them, campy notes what it found in `~/.campy/flir-frame-rate-<serial>.json`, and removes the note once they are back. If campy stops without closing a camera (a crash, a kill, a restart of the computer, which leaves the camera powered), the next run that opens it, triggered or not, puts them back first and says so (`Camera0: campy did not close it last time and left it capped at 80.0 fps. Cap removed, as before.`); the launch check does this too. A cap with no note was set by another program, or by a campy without these notes, and is left as it is (unplug the camera to clear it): on a triggered camera, the launch check reports it if it is below `frameRate`. Triggered cameras are otherwise left as they are: the trigger sets their rate.

A run that stops without closing a FLIR camera, triggered or not, also leaves it acquiring, and while it acquires, its frame size and pixel format cannot be changed. A later run with a different `frameWidth`, `frameHeight` or `pixelFormatInput` then wrote frames of the old size into a video of the new one, which scrambles it (640x512 asked for after a 1280x1024 run: 10 s at 80 fps came out as 3,200 frames instead of 800). So every open, the launch check's too, stops such an acquisition first and says so (`Camera0: still acquiring from a run that did not close it. Stopped it, so its frame size and pixel format can be set.`).

A FLIR frame smaller than the sensor (`frameWidth`, `frameHeight`) is centred on it. campy moves the frame to the sensor's edge before it sets the size, since a camera's width can reach no further than the sensor's edge from its offset: a smaller centred frame an earlier run left on it (640x512 of 1280x1024) would otherwise hold a full frame to 1120x896. If the camera's frame size still is not `frameWidth` x `frameHeight` (larger than the sensor, or a camera that cannot be resized now), the video would be scrambled, so the launch check stops campy and says what to change.

campy sets a FLIR camera's frame size in steps of 16: it rounds `frameWidth` and `frameHeight` up to multiples of 16, and says so (`Camera0: frameWidth 1000 rounded up to 1008: campy sets FLIR frame sizes in steps of 16.`). It does this before the camera and its video open. It used to happen only as the camera was set up, after the video had opened at 1000 wide, so 1008-wide frames went into it, scrambled.

Every FLIR camera's USB throughput limit (`DeviceLinkThroughputLimit`), triggered or not, is set by `cameraLinkLimitMBps`. A camera's live limit is not always the one it saved: a Blackfly S left plugged in through a computer's restart came up at 43 MB/s instead of its saved 380, which holds full-frame Mono8 to 32.8 fps (see [FLIR cameras at 43 MB/s after a restart](#flir-cameras-at-43-mbs-after-a-restart)). With `"auto"` (the default), campy raises a limit that is less than the camera's frames need at `frameRate` (frame size × frame rate, plus 10%) to the camera's maximum, and says so; a limit that is enough is kept, since a lower one can be on purpose, so that cameras can share a USB bus. A number sets the limit in MB/s, and `"None"` leaves the camera's own limit.

### Start Recording:
```
campy-acquire ./configs/campy_config.yaml
```

Any config key can be changed for one run on the command line: `--key value` means what `key: value` means in the config file. Text needs no quotes, `False` is false, `None` is the text "None", and a key that takes one value per camera takes a list, in quotes for the shell:
```
campy-acquire ./configs/campy_config_chronicrecord.yaml --cameraTrigger Line3 --codec h265 --startArduino False
campy-acquire ./configs/campy_config_3cams_gpu.yaml --quality "[24, 26, 26]" --gpuID "[0, 0, 1]"
```
campy 2.1.0rc2 and older needed text quoted twice (`--codec "'h265'"`, which still works), read `--startArduino False` as True, and took no lists. Paths are taken as typed (`--ffmpegPath C:\ffmpeg-btbn-8.1\bin\ffmpeg.exe`). As in the config, `yes`, `no`, `on` and `off` are True or False: the True/False keys take them, and a text key refuses them, so quote such text: `"'off'"`. `--preflight off` works as it is. A value campy cannot use stops it before any camera opens, naming the key (`argument --quality: 'high' is not a whole number, or a list of them`).

### Stop Recording:
- Campy can be configured to stop automatically after set recording time (e.g., 1 hour):
```
recTimeInSec: 3600
```
- To manually end, press Ctrl^C. Please wait until campy exits! Each camera stops, closes its video and saves its metadata. If one hangs as it closes (in a camera driver, or waiting for an ffmpeg that does not exit), press Ctrl^C again: campy then stops that camera's process at once, without closing it, and names it. Its files are those of a recording that stopped without closing (below), and its ffmpeg is left to finish the video from the frames it has; campy gives its pid, to end it if it hangs. Two presses within a second count as one.
- When campy starts the trigger itself (`startArduino: True`), it writes "trigger.json" in the videoFolder: whether the trigger started, which cameras were already grabbing (and why any were not), when the start command went out, on the clock of "hosttimes.npy", and the test LED's pin and rate if one is set. `campy-align` and `campy-synctest` read it.
- Three files, "frametimes.mat", "frametimes.npy", and "metadata.csv", will be saved along with the video file in each camera folder containing timestamps, frame numbers, and other recording metadata. "hosttimes.npy" holds when each of those frames reached the computer, on one clock for every camera (`time.monotonic_ns`); `campy-align` uses it.
- campy writes "frametimes.npy" and "hosttimes.npy" while it records, a batch about every second, and "metadata.csv" when the camera starts grabbing. When the recording closes, it adds the totals to "metadata.csv" (`totalFrames`, `framesWritten`, `framesDropped`, `totalTime`) and writes "frametimes.mat". So a recording that stops without closing, because campy crashed or was killed, keeps its settings and its frame times up to the last second, and memory does not grow with the length of a recording. ffmpeg still finishes the video, which then has up to a second more frames than "frametimes.npy". "frametimes.npy" is stored column by column (the .npy format's `fortran_order`); `np.load` gives the same 2 x frames array as before.
- If a camera's encoder falls more than `writeQueueMaxSec` (default 5 s) behind, new frames are dropped instead of filling memory. "frametimes.npy" still has one row per video frame, so a gap in its frame numbers marks a drop, and "metadata.csv" records `totalFrames` (grabbed), `framesWritten` and `framesDropped`.
- When a camera fails during a recording, campy says so at once, ends that camera's recording as cleanly as it can, and goes on with the other cameras. At the end it lists the cameras that did not record to the end, and exits with code 1 (0 when every camera did).
  - A camera process that dies (killed, or a crash in a camera driver) is named, with its exit code. Its files are those of a recording that stopped without closing (above): its video has the frames its ffmpeg had, and "metadata.csv" has no totals.
  - When ffmpeg stops (killed, crashed, or an encoder error), its camera is stopped and closed. "metadata.csv" gets `videoError` (why, with ffmpeg's exit code), and `framesWritten` counts only the frames ffmpeg was given: "frametimes.npy" and "hosttimes.npy" have one row for each of those, and the frames grabbed after them are in neither (`totalFrames` − `framesDropped` − `framesWritten` of them). Whether the video holds the frames ffmpeg was given depends on how it stopped: a killed ffmpeg leaves an MP4 that cannot be read (its index is written as it closes), while one that stops on an error of its own usually finishes it. ffmpeg's own messages show with `ffmpegLogLevel: "warning"`.
  - A camera that stops sending frames is reported after 3 s without one, every 30 s while it lasts, and when frames come again; Ctrl-C ends it as usual. A camera that is gone for good ends its recording there, with `cameraError` in "metadata.csv": a RatCam that lost its USB link, a FLIR camera Spinnaker no longer counts as valid, a Basler pylon says was removed. So does a camera that did not start grabbing.
  - A camera that fails to close is reported, and its video and "metadata.csv" are still finished.

### Scaling the number of cameras
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
- **usb** (Linux): each camera's USB bus and controller, read from sysfs, and the camera data on each bus against a budget (300 MB/s for a 5 Gb/s bus, 600 MB/s for 10 Gb/s; set `usbBudgetMBps` once you have measured your own). A camera running at USB 2 speed fails.
- **camera**: each camera's `cameraSelection` picks a camera that was found (with three FLIR cameras connected, `cameraSelection` 3 FAILs). FLIR: each camera can run at `frameRate` with campy's settings. campy opens it, sets it up as a recording does (trigger, frame size, pixel format, exposure), reads the highest frame rate the camera says it can reach, and names what limits it when that is too low: the camera's USB throughput limit (`DeviceLinkThroughputLimit`), an exposure longer than a frame, or, on a triggered camera, `AcquisitionFrameRate` (a free-running camera is read without the `frameRate` cap campy gives it). A camera that cannot keep up does not stop by itself: free-running, it records more slowly than its video says; triggered, it misses triggers. It also checks that each camera sends frames of `frameWidth` x `frameHeight`, and FAILs with the ways out when it does not:
```
  FAIL  camera    Camera0 (24243505) would send 640x512 frames, not frameWidth x frameHeight 1280x1024, so its video would be scrambled. Either:
                    - unplug it and plug it back in: its frame size cannot be changed now
                    - set frameWidth: 640 and frameHeight: 512, the frames it sends now
```
- **disk**: free space for the recording.

With `preflight: "error"` (the default), a failed check stops campy before recording; `"warn"` records anyway; `"off"` (quoted or not) skips the check. `displayFrameRate: 0` runs without display windows, for example over SSH.

### RatCam (nir-eye-cam) cameras
`cameraMake: "ratcam"` records the NIR eye cameras of [nir-eye-cam](https://github.com/olinesn/nir-eye-cam) (an HM01B0 on an RP2040, or a VD55G1 on an ESP32-P4) alongside the other cameras. Each is a USB serial device. Install its host package, `nireyecam` (Python 3.9 or newer), into campy's environment:
```
pip install -e '.[ratcam]'         # or: pip install -e <nir-eye-cam>/host
campy-acquire ./configs/campy_config_ratcam_sim.yaml   # two simulated RatCams
```
`configs/campy_config_flir_ratcam.yaml` puts one next to FLIR cameras.

- `cameraSelection` counts RatCams in the order of their USB serial numbers, so each keeps its index when the ports come up in another order. `ratcamPorts` lists serial ports to use instead; `"sim"` is a simulated RatCam.
- A RatCam is not triggered: it runs at its `frameRate`, which campy reaches by setting the sensor's frame length. A rate the sensor cannot reach at that frame size stops the camera, with the highest rate it can. `cameraExposureTimeInUs` is converted to sensor lines.
- `ratcamGain` is the firmware's analog gain code (RP2040 0-3 for 1x-8x, ESP32-P4 0-28). `ratcamLed` sets the IR LEDs (0-1000; the ESP32-P4's are on or off). They are off unless set, and campy turns them off when it closes the camera. `ratcamSyncOut: True` makes that camera drive the random sync pulse train on its SYNC_OUT.
- Frames are 8-bit mono. They are encoded at their own size, never rescaled to a multiple of 16.
- `frametimes.npy` holds the camera's own timestamps. Each RatCam folder also gets:
  - `ratcam_frames.csv`: every frame grabbed, with campy's frame number, the camera's frame counter (a gap is a frame lost before campy saw it, e.g. over a slow USB link), its timestamp in µs, and when it reached the computer (`host_ns`, the clock of `hosttimes.npy`);
  - `ratcam_sync.csv`: every pulse on the camera's SYNC input, timestamped on the same clock, and when it reached the computer, in order of time.

  campy writes both while it records, like `frametimes.npy`: it opens them when the camera starts grabbing and adds rows about every second. So a recording that stops without closing keeps its rows up to the last second, and memory does not grow with the length of a recording (kept until the end, they took about 170 bytes a row: 1.8 GB a day of frames at 120 fps). `metadata.csv` adds the frame rate and exposure reached, and, when the recording closes, the frames lost (`ratcamFramesLost`) and the sync pulses (`ratcamSyncPulses`).
- The launch check checks each RatCam's own USB link: the RP2040 build is USB full speed, about 1 MB/s of frames.

**On the same timeline as triggered cameras:** wire the trigger line to each RatCam's SYNC input too (3.3 V: divide a 5 V trigger, e.g. 10 kΩ over 20 kΩ). Each trigger pulse, and so each triggered frame, then has a time on the RatCam's clock in `ratcam_sync.csv`, and RatCam frames fall between them. `campy-align` (below) puts them together.

### Aligning cameras after recording
```
campy-align ./test        # the recording's videoFolder
```
puts every camera on the trigger's timeline: trigger *k* is at `t_s` = (*k* − 1) × the trigger period. It writes `aligned.csv` in each aligned camera's folder (`frame`, `trigger`, `t_s`; a RatCam's `trigger` is fractional, between the pulses around the frame's start, and its `between_pulses` is 0 for a frame from before its first pulse or after its last, placed from its own clock) and `alignment.json`, and prints a summary:
- **The trigger behind each frame**, from the camera's own timestamps: a step of *n* periods means *n* − 1 triggers left no frame. Those campy dropped itself (gaps in `frametimes.npy`'s frame numbers) are counted apart from those the camera missed.
- **Where each camera started.** With `startArduino: True`, campy starts the trigger once every camera is grabbing and says which were in `trigger.json`; `campy-align` starts those cameras at trigger 1. It checks each against the other cameras of its kind, and a camera whose first frame came a whole period or more after theirs is reported as having missed its first triggers. Cameras can still start on different triggers: a camera named as not ready, an external trigger started too early, or a recording without `trigger.json` (from a campy that started the trigger first). For those, the arrival times (`hosttimes.npy`, and `ratcam_sync.csv` for a RatCam's pulses) say by how many triggers, against the cameras that were grabbing, or else the earliest camera's first trigger becomes trigger 1. A RatCam's pulses reach the computer within a moment of their trigger, a triggered camera's frames a few ms after it; when a triggered camera's frames take near a whole period, its place against a RatCam (or a RatCam's against it) could be off by one, and the summary says so. Recordings without `hosttimes.npy` (from before this version) assume every camera started on the same trigger.
- A RatCam needs a periodic trigger on its SYNC input. A random sync train (`ratcamSyncOut`) cannot be lined up this way.
- A recording that stopped without closing has each file up to its own last write, about every second:
  - frames in a RatCam's `frametimes.npy` that `ratcam_frames.csv` had not written yet: `campy-align` aligns the frames both have, and says how many it left out. A RatCam with none yet is reported with no frames, and its pulses still place the other cameras;
  - frames in `frametimes.npy` without arrival times in `hosttimes.npy` (written just after it): every frame is aligned, the start is worked out from the arrival times there are, and the summary says so;
  - `ratcam_sync.csv` not yet in order of time (campy sorts it when the recording closes): `campy-align` sorts it.

### Checking that cameras expose together
`campy-synctest` checks from a recording that triggered cameras really expose at the same moment, and says how far apart they are, by filming one LED that the trigger controller blinks from the same clock as the trigger.
1. Wire an LED and a 330 Ω resistor in series from a spare pin of the trigger controller to ground, where every camera can see it. A red LED suits visible-light cameras; behind an IR-pass filter, use an IR LED.
2. Upload the current `trigger.ino`: it reads the test LED's pin and rate after the frame rate. (An older sketch ignores them and never blinks.)
3. In the config, set the LED's pin and a blink rate that is not near a whole or half multiple of `frameRate` (43 Hz suits 25 and 60 fps), and keep the exposure under a quarter of the blink period (5.8 ms at 43 Hz; about 2 ms is good):
```
blinkPin: 13
blinkHz: 43
cameraExposureTimeInUs: 2000
```
4. Record about a minute, then:
```
campy-synctest ./test        # the recording's videoFolder
```
It reports each camera's exposure offset from the first camera's (or `--reference`), with its standard error: from frames in which both cameras caught an edge of the blink (a few µs), or from the blink's phase (about 10 µs) when they are more than half an exposure apart, their exposures differ, the LED saturates, or too few frames caught an edge in both. It also counts the triggers on which the LED was clearly on in one camera and off in the other (0 when the cameras are locked). It passes when every 2-second stretch is within `--tolerance-us` (default 100), and writes `synctest.json`. It pairs frames by trigger with `campy-align`, so it writes that tool's files too. A better `quality` (a lower number) makes the edge measurement finer, since the LED's brightness goes through the video encoder.

### Running the tests
```
pip install -e . pytest
pytest tests
```
GitHub Actions runs them on every push to master or dev and every pull request, on Python 3.9, 3.10 and 3.12 on Linux and Python 3.12 on Windows (`.github/workflows/tests.yml`). The RatCam tests need nir-eye-cam's `nireyecam`, from a private repository, and are skipped without it. To run them in CI too, add a repository secret `NIREYECAM_TOKEN`: a fine-grained personal access token with read-only access to the contents of `olinesn/nir-eye-cam`.

The tests run `trigger.ino` as plain C++ against a simulated Arduino. A second workflow (`.github/workflows/sketch.yml`) compiles it for real boards (an Arduino Uno, a Teensy 4.0 and a Teensy 3.2) whenever the sketch changes.

### Development and releases
Everything lands on `dev` first. `master` is the released campy, and changes only when `dev` is promoted to it.

- **Pull requests** go to `dev`, the repository's default branch.
- **Trying `dev` on a rig:** in its clone, `git fetch`, `git checkout dev` and `git pull`; `git checkout master` goes back. `pip install -e .` follows whichever branch is checked out, so nothing needs reinstalling unless `setup.py`'s dependencies changed.
- **A pre-release** marks a state of `dev` to test, for example on the rigs. Set `version` in `setup.py` to it (`2.1.0rc1`) in a pull request to `dev`, so a rig running it reports that version, then:
  ```
  gh release create v2.1.0rc1 --target dev --prerelease --generate-notes
  ```
  Pre-release tags take Python's form, `v2.1.0rc1`, `v2.1.0rc2`, ..., which pip orders before `2.1.0`. A rig checks one out with `git checkout v2.1.0rc1`.
- **A release:** set `version` in `setup.py` to it (`2.1.0`) in a pull request to `dev`. Then run **Actions ▸ Promote dev to master ▸ Run workflow** with the tag `v2.1.0`, or:
  ```
  gh workflow run promote.yml -f tag=v2.1.0
  ```
  It moves `master` forward to `dev` and publishes release `v2.1.0` on that commit, with notes listing the pull requests merged since the last release. With no tag it only promotes. It changes nothing if `setup.py`'s version is not the tag's, or if `master` has a commit that `dev` lacks: promotion only ever fast-forwards, because merging or squashing `dev` into `master` would leave the two histories apart, and every later promotion would conflict.

### Helpful tips
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

## Authors
Written by Kyle Severson with contributions from Diego Aldarondo and Iris Odstrcil.

## Credits
Special thanks to Stefan Oline, Tim Dunn, David Hildebrand, Vincent Prevosto, Manuel Levy, and Paul Thompson for helpful comments.

## License
MIT License
