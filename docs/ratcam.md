# RatCam (nir-eye-cam) cameras

Recording the NIR eye cameras of nir-eye-cam alongside the other cameras, and putting them on the same timeline.

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

**On the same timeline as triggered cameras:** wire the trigger line to each RatCam's SYNC input too (3.3 V: divide a 5 V trigger, e.g. 10 kΩ over 20 kΩ). Each trigger pulse, and so each triggered frame, then has a time on the RatCam's clock in `ratcam_sync.csv`, and RatCam frames fall between them. `campy-align` ([Aligning cameras after recording](sync.md#aligning-cameras-after-recording)) puts them together.
