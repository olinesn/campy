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
2. Create and activate a new Python 3.7+ Anaconda environment:
```
conda create -n campy python=3.7 imageio-ffmpeg matplotlib ffmpeg==4.2.2 -c conda-forge
conda activate campy
pip install -U setuptools
```
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
  - Manually install binary wheel for PySpin (included in the Spinnaker download)
    E.g. for Python 3.7 on Windows amd64 system, install "spinnaker_python-2.3.0.77-cp37-cp37m-win_amd64.whl"
  ```
  pip3 install <wheel>
  ```
4. Create a folder to clone campy:
```
cd ~\Documents\
mkdir campy
cd campy
```
5. Clone or download campy to local folder:
```
git clone https://github.com/ksseverson57/campy.git
```
6. Navigate to the cloned campy folder
```
cd campy
```
7. Finally, install campy and its dependencies (see setup.py):
```
pip install -e .
```

## Usage

### Configuration
- For Basler cameras, use the Pylon Viewer to configure and save your '.pfs' camera settings file. Examples are included in campy/configs.
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
digitalPins: [<pin IDs>] # e.g., [0,1,2]
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

### Start Recording:
```
campy-acquire ./configs/campy_config.yaml
```

### Stop Recording:
- Campy can be configured to stop automatically after set recording time (e.g., 1 hour):
```
recTimeInSeconds: 3600
```
- To manually end, press Ctrl^C. Please wait until campy exits!
- When campy starts the trigger itself (`startArduino: True`), it writes "trigger.json" in the videoFolder: whether the trigger started, which cameras were already grabbing (and why any were not), when the start command went out, on the clock of "hosttimes.npy", and the test LED's pin and rate if one is set. `campy-align` and `campy-synctest` read it.
- Three files, "frametimes.mat", "frametimes.npy", and "metadata.csv", will be saved along with the video file in each camera folder containing timestamps, frame numbers, and other recording metadata. "hosttimes.npy" holds when each of those frames reached the computer, on one clock for every camera (`time.monotonic_ns`); `campy-align` uses it.
- If a camera's encoder falls more than `writeQueueMaxSec` (default 5 s) behind, new frames are dropped instead of filling memory. "frametimes.npy" still has one row per video frame, so a gap in its frame numbers marks a drop, and "metadata.csv" records `totalFrames` (grabbed), `framesWritten` and `framesDropped`.

### Scaling the number of cameras
Change `numCams`; nothing else in a config has to depend on it. A setting can be one value for every camera, or a list with one value per camera. A list longer than `numCams` uses its first items, so a config written for 8 cameras also runs 4. `configs/campy_config_flir_usb3.yaml` is an example.

Whether a computer keeps up with the cameras depends on its encoders and its USB buses. Measure the encoders once per computer:
```
campy-encode-sweep --config ./configs/campy_config_flir_usb3.yaml --encoder vaapi --encoder cpu:veryfast --streams 1,2,4,6,8
```
It runs 1, 2, 4... encoders at once, with campy's own ffmpeg settings, on synthetic frames, and saves how many each one keeps up with to `~/.campy/capacity.json`. The bitrates it reports come from those synthetic frames and are only a rough guide.

`quality` means different things to different encoders. libx264 and libx265 take it as a CRF, which adapts to the picture. NVENC, VA-API and AMF take it as a constant quantizer, which spends bits on everything, sensor noise included. On a GMKtec EVO-X2 (1280x1024 mono, 60 fps), quality 21 gave about 1.5 Mbit/s per camera on libx264 and 36.7 Mbit/s on VA-API: 132 GB per hour for 8 cameras. Measure what each setting costs before recording:
```
campy-encode-quality --config ./configs/campy_config_flir_usb3.yaml --input clip.mkv --quality 21,24,27,30
```
It encodes the clip with campy's own settings at each quality and reports the bitrate, GB per camera-hour, and PSNR and SSIM against the clip. Use a few seconds of the real scene, as close to lossless as you have (FFV1, or H.264 at `-qp 0`). Without `--input` it uses the sweep's synthetic frames, which show what sensor noise costs and little else. When cameras use different encoders, give `quality` one value per camera.

Every `campy-acquire` then starts with a launch check, printed before recording:
- **encoders**: ffmpeg has each camera's encoder, and the VA-API device can be opened.
- **capacity**: the cameras on each encoder, against the measured table.
- **usb** (Linux): each camera's USB bus and controller, read from sysfs, and the camera data on each bus against a budget (300 MB/s for a 5 Gb/s bus, 600 MB/s for 10 Gb/s; set `usbBudgetMBps` once you have measured your own). A camera running at USB 2 speed fails.
- **disk**: free space for the recording.

With `preflight: "error"` (the default), a failed check stops campy before recording; `"warn"` records anyway; `"off"` skips the check. `displayFrameRate: 0` runs without display windows, for example over SSH.

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
  - `ratcam_frames.csv`: every frame grabbed, with campy's frame number, the camera's frame counter (a gap is a frame lost before campy saw it, e.g. over a slow USB link), and its timestamp in µs;
  - `ratcam_sync.csv`: every pulse on the camera's SYNC input, timestamped on the same clock.

  `metadata.csv` adds the frame rate and exposure reached, and the frames lost.
- The launch check checks each RatCam's own USB link: the RP2040 build is USB full speed, about 1 MB/s of frames.

**On the same timeline as triggered cameras:** wire the trigger line to each RatCam's SYNC input too (3.3 V: divide a 5 V trigger, e.g. 10 kΩ over 20 kΩ). Each trigger pulse, and so each triggered frame, then has a time on the RatCam's clock in `ratcam_sync.csv`, and RatCam frames fall between them. `campy-align` (below) puts them together.

### Aligning cameras after recording
```
campy-align ./test        # the recording's videoFolder
```
puts every camera on the trigger's timeline: trigger *k* is at `t_s` = (*k* − 1) × the trigger period. It writes `aligned.csv` in each camera folder (`frame`, `trigger`, `t_s`; a RatCam's `trigger` is fractional, between the pulses around the frame's start) and `alignment.json`, and prints a summary:
- **The trigger behind each frame**, from the camera's own timestamps: a step of *n* periods means *n* − 1 triggers left no frame. Those campy dropped itself (gaps in `frametimes.npy`'s frame numbers) are counted apart from those the camera missed.
- **Where each camera started.** With `startArduino: True`, campy starts the trigger once every camera is grabbing and says which were in `trigger.json`; `campy-align` starts those cameras at trigger 1. It checks each against the other cameras of its kind, and a camera whose first frame came a whole period or more after theirs is reported as having missed its first triggers. Cameras can still start on different triggers: a camera named as not ready, an external trigger started too early, or a recording without `trigger.json` (from a campy that started the trigger first). For those, the arrival times (`hosttimes.npy`, and `ratcam_sync.csv` for a RatCam's pulses) say by how many triggers, and the earliest camera's first trigger becomes trigger 1. A camera's frames reach the computer a few ms after its trigger; when that comes near a whole period the start could be off by one, and the summary says so. Recordings without `hosttimes.npy` (from before this version) assume every camera started on the same trigger.
- A RatCam needs a periodic trigger on its SYNC input. A random sync train (`ratcamSyncOut`) cannot be lined up this way.

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
It reports each camera's exposure offset from the first camera's (or `--reference`), with its standard error: from frames in which both cameras caught an edge of the blink (a few µs), or from the blink's phase when they are more than one exposure apart (about 10 µs). It also counts the triggers on which the LED was clearly on in one camera and off in the other (0 when the cameras are locked). It passes when every 2-second stretch is within `--tolerance-us` (default 100), and writes `synctest.json`. It pairs frames by trigger with `campy-align`, so it writes that tool's files too. A better `quality` (a lower number) makes the edge measurement finer, since the LED's brightness goes through the video encoder.

### Running the tests
```
pip install -e . pytest
pytest tests
```
GitHub Actions runs them on every push to master and every pull request, on Python 3.9 and 3.12 (`.github/workflows/tests.yml`). The RatCam tests need nir-eye-cam's `nireyecam`, from a private repository, and are skipped without it. To run them in CI too, add a repository secret `NIREYECAM_TOKEN`: a fine-grained personal access token with read-only access to the contents of `olinesn/nir-eye-cam`.

The tests run `trigger.ino` as plain C++ against a simulated Arduino. A second workflow (`.github/workflows/sketch.yml`) compiles it for real boards (an Arduino Uno, a Teensy 4.0 and a Teensy 3.2) whenever the sketch changes.

### Helpful tips
- To debug broken ffmpeg pipe error, include this in config.yaml:
```
ffmpegLogLevel: "warning"
```
- Use the command "ffmpeg" to check enabled packages. Hardware encoder support must be enabled in your ffmpeg binary.
- Windows ffmpeg binary installed by Anaconda should have hardware encoder support enabled by default.
- On Linux, you may need to compile your own ffmpeg binary to enable encoders:
- Nvidia:
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

## Authors
Written by Kyle Severson with contributions from Diego Aldarondo and Iris Odstrcil.

## Credits
Special thanks to Stefan Oline, Tim Dunn, David Hildebrand, Vincent Prevosto, Manuel Levy, and Paul Thompson for helpful comments.

## License
MIT License
