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
5. Campy can now communicate with the microcontroller to synchronously trigger the cameras once acquisition has initialized.

Alternatively, to configure campy for external triggering:
Start campy before triggering. Campy will wait for external triggers to initialize. To stop the recording, either configure a set recording time in campy or stop triggers before manually exiting campy (see Stop Recording below). 
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
- Three files, "frametimes.mat", "frametimes.npy", and "metadata.csv", will be saved along with the video file in each camera folder containing timestamps, frame numbers, and other recording metadata.
- If a camera's encoder falls more than `writeQueueMaxSec` (default 5 s) behind, new frames are dropped instead of filling memory. "frametimes.npy" still has one row per video frame, so a gap in its frame numbers marks a drop, and "metadata.csv" records `totalFrames` (grabbed), `framesWritten` and `framesDropped`.

### Scaling the number of cameras
Change `numCams`; nothing else in a config has to depend on it. A setting can be one value for every camera, or a list with one value per camera. A list longer than `numCams` uses its first items, so a config written for 8 cameras also runs 4. `configs/campy_config_flir_usb3.yaml` is an example.

Whether a computer keeps up with the cameras depends on its encoders and its USB buses. Measure the encoders once per computer:
```
campy-encode-sweep --config ./configs/campy_config_flir_usb3.yaml --encoder vaapi --encoder cpu:veryfast --streams 1,2,4,6,8
```
It runs 1, 2, 4... encoders at once, with campy's own ffmpeg settings, on synthetic frames, and saves how many each one keeps up with to `~/.campy/capacity.json`. The bitrates it reports come from those synthetic frames and are only a rough guide.

Every `campy-acquire` then starts with a launch check, printed before recording:
- **encoders**: ffmpeg has each camera's encoder, and the VA-API device can be opened.
- **capacity**: the cameras on each encoder, against the measured table.
- **usb** (Linux): each camera's USB bus and controller, read from sysfs, and the camera data on each bus against a budget (300 MB/s for a 5 Gb/s bus, 600 MB/s for 10 Gb/s; set `usbBudgetMBps` once you have measured your own). A camera running at USB 2 speed fails.
- **disk**: free space for the recording.

With `preflight: "error"` (the default), a failed check stops campy before recording; `"warn"` records anyway; `"off"` skips the check. `displayFrameRate: 0` runs without display windows, for example over SSH.

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
