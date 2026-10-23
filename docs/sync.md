# Triggering and synchronization

Triggering cameras together from an Arduino or Teensy (or an external trigger), putting every camera on the trigger's timeline after recording (`campy-align`), and checking that triggered cameras expose at the same moment (`campy-synctest`).

## Camera Triggering
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

Every run, before any camera starts, campy checks that the Arduino answers: it opens `serialPort`, sends the stop command (frame rate -1, test LED off) and waits for trigger.ino's reply (`Frame rate set to: 0.00 fps.`; 51 ms after the command on the EVO-X2's Arduino Nano ESP32). An Arduino that is still busy with a command from a run that stopped a moment ago throws the check's away unanswered, so campy sends it again every second, for up to 8 s. If the port does not open, or the Arduino does not answer, campy stops there, before any camera opens, and exits with code 1:
```
The trigger Arduino on /dev/ttyACM0 did not answer (no reply to the stop command 3,0,1,2,-1,-1,0 within 8 s). Unplug its USB cable and plug it back in (or power-cycle it), then run campy again.
```
A dead or hung Arduino used to leave the cameras waiting for triggers that never came. Ctrl-C during the check stops campy at once, also with code 1 (`Stopped (Ctrl-C) while checking the trigger Arduino on /dev/ttyACM0, before any camera started: nothing was recorded.`). The answer replaces the 3 s campy used to wait blindly after opening the port. trigger.ino reads a command until a second has passed with nothing more (the last number has nothing after it), then waits 4 s, then throws away whatever came meanwhile, so campy sends the start command no sooner than 5.5 s after the check's answer: if the cameras are grabbing sooner, it waits the rest. A run's trigger therefore starts about 5.5 s after `campy-acquire` opens the port, or when the last camera is grabbing, whichever is later, and the first pulse comes about 5 s after that, as before. There is no check without `startArduino: True` and `triggerController: "arduino"` (external triggers, below).

Alternatively, to configure campy for external triggering:
Start campy before triggering. Campy will wait for external triggers to initialize; start them once it prints "All N cameras are ready.", and every camera's first frame is the first trigger. To stop the recording, either configure a set recording time in campy or stop triggers before manually exiting campy (see [Stop Recording](acquire.md#stop-recording)). 
```
startArduino: False
cameraTrigger: "Line0" # or "Line3"
```

## Aligning cameras after recording
```
campy-align ./test/2026-10-18_17-30-05        # the recording's folder (its videoFolder with sessionFolder False)
```
puts every camera on the trigger's timeline: trigger *k* is at `t_s` = (*k* − 1) × the trigger period. It writes `aligned.csv` in each aligned camera's folder (`frame`, `trigger`, `t_s`; a RatCam's `trigger` is fractional, between the pulses around the frame's start, and its `between_pulses` is 0 for a frame from before its first pulse or after its last, placed from its own clock) and `alignment.json`, and prints a summary:
- **The trigger behind each frame**, from the camera's own timestamps: a step of *n* periods means *n* − 1 triggers left no frame. Those campy dropped itself (gaps in `frametimes.npy`'s frame numbers) are counted apart from those the camera missed.
- **Where each camera started.** With `startArduino: True`, campy starts the trigger once every camera is grabbing and says which were in `trigger.json`; `campy-align` starts those cameras at trigger 1. It checks each against the other cameras of its kind, and a camera whose first frame came a whole period or more after theirs is reported as having missed its first triggers. Cameras can still start on different triggers: a camera named as not ready, an external trigger started too early, or a recording without `trigger.json` (from a campy that started the trigger first). For those, the arrival times (`hosttimes.npy`, and `ratcam_sync.csv` for a RatCam's pulses) say by how many triggers, against the cameras that were grabbing, or else the earliest camera's first trigger becomes trigger 1. A RatCam's pulses reach the computer within a moment of their trigger, a triggered camera's frames a few ms after it; when a triggered camera's frames take near a whole period, its place against a RatCam (or a RatCam's against it) could be off by one, and the summary says so. Recordings without `hosttimes.npy` (from before this version) assume every camera started on the same trigger.
- A RatCam needs a periodic trigger on its SYNC input. A random sync train (`ratcamSyncOut`) cannot be lined up this way.
- A recording that stopped without closing has each file up to its own last write, about every second:
  - frames in a RatCam's `frametimes.npy` that `ratcam_frames.csv` had not written yet: `campy-align` aligns the frames both have, and says how many it left out. A RatCam with none yet is reported with no frames, and its pulses still place the other cameras;
  - frames in `frametimes.npy` without arrival times in `hosttimes.npy` (written just after it): every frame is aligned, the start is worked out from the arrival times there are, and the summary says so;
  - `ratcam_sync.csv` not yet in order of time (campy sorts it when the recording closes): `campy-align` sorts it.

## Checking that cameras expose together
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
campy-synctest ./test/2026-10-18_17-30-05        # the recording's folder
```
It reports each camera's exposure offset from the first camera's (or `--reference`), with its standard error: from frames in which both cameras caught an edge of the blink (a few µs), or from the blink's phase (about 10 µs) when they are more than half an exposure apart, their exposures differ, the LED saturates, or too few frames caught an edge in both. It also counts the triggers on which the LED was clearly on in one camera and off in the other (0 when the cameras are locked). It passes when every 2-second stretch is within `--tolerance-us` (default 100), and writes `synctest.json`. It pairs frames by trigger with `campy-align`, so it writes that tool's files too. A better `quality` (a lower number) makes the edge measurement finer, since the LED's brightness goes through the video encoder.
