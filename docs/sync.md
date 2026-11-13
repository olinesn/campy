# Triggering and synchronization

Triggering cameras together from an Arduino or Teensy (or an external trigger), putting every camera on the trigger's timeline after recording (`campy-align`), sending a sync line to another recorder and putting its clock on the trigger's (`syncPin`, `campy-syncmatch`), and checking that triggered cameras expose at the same moment (`campy-synctest`).

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
A dead or hung Arduino used to leave the cameras waiting for triggers that never came. Ctrl-C during the check stops campy at once, also with code 1 (`Stopped (Ctrl-C) while checking the trigger Arduino on /dev/ttyACM0, before any camera started: nothing was recorded.`). The answer replaces the 3 s campy used to wait blindly after opening the port. trigger.ino reads a command until a second has passed with nothing more (the last number has nothing after it), then waits 4 s, then throws away whatever came meanwhile. The sketch older campys flashed (`campy_v03`'s, still on some rigs' Arduinos) waits 6 s there, and takes about 7 s in all after its answer. So campy sends the start command no sooner than 8 s after the check's answer: if the cameras are grabbing sooner, it waits the rest. A run's trigger therefore starts about 8 s after `campy-acquire` opens the port, or when the last camera is grabbing, whichever is later. The first pulse comes about 5 s after that (7 s with the older sketch).

Until pull request 284, campy sent the start 5.5 s after the answer. That worked with its own sketch, but the older sketch threw the start away, so no camera ever got a frame: the cameras sat ready, and after 10 s the recording watch said "No camera has had a frame". That is what happened on the DoublePoke rig on 5 November 2026, with the Arduino still running `campy_v03`'s sketch. `tests/test_trigger_sketch.py` now runs a copy of that sketch on its simulated clock and checks that the start reaches it. Checked on the same rig with this change, the same evening:
- 8 cameras, 2 minutes at 60 fps, the Arduino still on `campy_v03`'s sketch;
- 7,200 frames on every camera, none dropped or missed, and every camera from the trigger's first pulse (campy-align). There is no check without `startArduino: True` and `triggerController: "arduino"` (external triggers, below).

**Upstream campy** ([ksseverson57/campy](https://github.com/ksseverson57/campy)) and this one work with each other's trigger sketch, in both directions. Upstream's `trigger.ino` is this one before the test LED, with the settling delay at 4 s.
- **Upstream campy with this `trigger.ino`:** upstream sends the pin count, the pins and the frame rate, and stops with frame rate −1. This sketch then triggers the same pins at the same rate, from the same moment as upstream's own sketch (5.5 s after the command at power-up, at 25 fps), and stops at once. The test LED and the sync output stay off. Until pull request 443 the first pulse came 2 s later, because the sketch waited for the test LED's two numbers.
- **This campy with upstream's `trigger.ino`:** campy's check is answered, and the trigger starts and stops. That sketch has no test LED and no sync output. It throws their settings away and never says `Test LED off.` or `Sync output off.` after its answer to the check, so campy says so at the start, records no `sync.json`, and doesn't claim a blinking LED. Upload this `trigger.ino` to have them.

`tests/test_trigger_sketch.py` runs upstream's sketch on its simulated clock and checks both directions.

Alternatively, to configure campy for external triggering:
Start campy before triggering. Campy will wait for external triggers to initialize; start them once it prints "All N cameras are ready.", and every camera's first frame is the first trigger. To stop the recording, either configure a set recording time in campy or stop triggers before manually exiting campy (see [Stop Recording](acquire.md#stop-recording)). 
```
startArduino: False
cameraTrigger: "Line0" # or "Line3"
```

## One trigger board per rig

A rig's cameras get their pulses from one board: campy's Arduino, or the ChronicRecord Pico. The rig profile says which, with `triggerController`, and every config that extends it follows:

```yaml
# ~/.campy/configs/chronicrecord.yaml
triggerController: "pico"   # the Pico pulses Line3; campy starts no Arduino
startArduino: False
cameraTrigger: "Line3"
```

campy refuses a config that would use the other board, before any camera opens (campy-acquire, `campy-config check`, and the panel's list of experiments):
- a config that names the other board in `triggerController` (it may still say `None`, for no trigger: free-running cameras);
- `startArduino: True` on a rig whose profile says `pico`;
- a `pico:` section (campy-session's Pico) with `startArduino: True`, or on a rig whose profile says `arduino`: both boards would pulse the cameras.

A profile without `triggerController` works as before: a Pico config only needs `startArduino: False`, and `pico:` with `startArduino: True` is the one thing refused.

**The panel's checks list** has a **Trigger** line before Start. With the Arduino, campy's `serialPort` must be one of the computer's serial ports. Otherwise the line is red, names the ports there are, and the start is stopped, rather than campy-acquire stopping 2 s into the recording. With the Pico (`triggerController: pico`), the config needs a `pico:` section to start the Pico's pulses. Without one the cameras would wait for pulses nobody makes, so the start is stopped. A camera-only session is enough (`pico: {set: {CAMERA_ONLY: 60, CAMERA_FPS: 10}}`, the length and `frameRate` of the recording; [camera-only sessions](../chronicrecord/README.md#camera-only-sessions)).

Why: on 10 November 2026 every calibration from the ChronicRecord rig's panel stopped 2 s in. The Pico had pulsed the rig's cameras since 5 November, and the bout sessions' configs said so. But the rig profile, which the calibration, look and live-tracking configs extend, still started the Arduino on COM9, which had not been plugged in since 2 November.

## A sync output for other recorders

An ephys, photometry or other recording system has a clock of its own. To put its samples and the camera frames on one timeline, campy's trigger board can send it a **sync line**: 10 ms pulses with random gaps of 200 to 1000 ms, made from the same clock as the camera triggers. Each gap is a whole number of milliseconds, one of 801 values, so any four gaps in a row (about 2.4 s of pulses) say where in the recording they are. A recorder that started late, missed a stretch or dropped pulses still lines up. It is the same pulse train as the ChronicRecord Pico's (pull request 419), now from `trigger.ino` on any board.

Turn it on in the config, wire the pin to one of the recorder's inputs (and the grounds together), and upload the current `trigger.ino`:

```yaml
syncPin: 7            # the board's pin; None (the default): off
syncSeed: None        # None: a new random seed for each recording
syncPulseMs: 10       # how long each pulse is high
syncGapMs: [200, 1000] # shortest and longest gap
```

The pin must be the sync output's own (not one of `digitalPins`, nor `blinkPin`): campy checks this before it starts. Mind the voltages: the Nano ESP32 and Teensy 4.x put out 3.3 V, an Uno 5 V. campy sends the settings after the test LED's. An older sketch, or upstream campy's, has no sync output: campy hears that from its answer to the check, says so, and records no `sync.json`. The sketch says `Sync output on pin 7, seed ..., 10000 us pulses, gaps 200 to 1000 ms.`, or `Sync output off: <why>` when it can't (`pin triggers a camera`, `frame rate too high for this board`, `pulse too short, or too long for the gaps`).

Each recording keeps the settings in **`sync.json`**, next to `trigger.json` (which also gets `syncPin`, `syncSeed` and `syncFile`): the seed, pulse length and gaps, and the frame period. That is all it takes to work out every pulse. Time 0 is the start of the trigger's first frame period: pulse *k* (from 0) rises at the sum of gaps 1 to *k* + 1, and frame *k*'s trigger rises at *k* × period + period / 2. Gap *j* is 1000 × (shortest + SplitMix64(seed, *j*) mod (longest − shortest + 1)) µs: `campy/syncmatch.py` and the sketch compute it the same way.

### Keeping the camera edges where they were

`trigger.ino` makes every edge in one loop that waits on the board's microsecond clock. A sync edge written just before a camera edge would delay that camera edge by the time the write takes (a few µs on an Uno). So a sync edge due within 50 µs before a camera edge, or at the same moment, waits until 10 µs after it. The rule is deterministic, so `campy-syncmatch` works out the moved edges exactly. How many edges move depends on the frame rate, because whole-millisecond gaps often line up with a whole-millisecond frame period. Over a day of pulses: 20% at 100 and 200 fps (every edge that falls on a camera edge), 5% at 25 fps, 3% at 333 fps, under 1% at 30 and 60 fps. Each moves by 10 to 60 µs, which the matcher allows for. The sketch works out the next sync edge (64-bit arithmetic, slow on an 8-bit board) only while the next camera edge is more than 200 µs away, or 1500 µs on an AVR board such as the Uno. That caps the frame rate with a sync output at about 1,900 fps on 32-bit boards, and 320 fps on an Uno or Mega.

In the tests' simulated board (`tests/test_trigger_sketch.py`), the camera edges came at exactly the same microsecond with and without the sync output, at 25, 100 and 333 fps (2,700 to 36,000 edges each). Every sync edge came 0 to 2 µs after the time the matcher works out for it, across the 32-bit wrap of `micros()` too. On a real board, the cameras saw no difference either. The test (13 November 2026) used the Arduino Nano ESP32 that triggers a test rig's three FLIR Blackfly S cameras, with the sync output on a pin wired to nothing. It made four 60 s recordings, at 60 and 100 fps, each with the sync output off and on. The cameras' own timestamps measured the time between frames, so the figures include the cameras' own jitter:

| | 60 fps, off | 60 fps, on | 100 fps, off | 100 fps, on |
|---|---|---|---|---|
| Frames per camera (none missed) | 3,600 | 3,600 | 6,000 | 6,000 |
| Time between frames: standard deviation | 1.00–1.05 µs | 1.00–1.01 µs | 0.53–0.57 µs | 0.54–0.59 µs |
| 99th percentile of the deviation | 4.4–4.6 µs | 3.8–4.4 µs | 1.32–1.42 µs | 1.36–1.46 µs |
| Largest deviation | 8.2–9.0 µs | 8.6–8.9 µs | 1.6–1.7 µs | 1.8–2.4 µs |

The ranges are over the three cameras. At 100 fps, where one sync edge in five is held back from a camera edge, the largest single deviation was 0.2 to 0.7 µs larger with the sync output. Whether that is the sync output or chance would take longer runs; the spread and the 99th percentile didn't change. The sketch answered campy as in the simulator: sync on, off with campy's stop command, and refused on the camera's pin. The pulses on the sync pin weren't recorded, since nothing was wired to it. On an Uno, whose loop is slower, a camera edge may come 1 to 2 µs later than without the sync output; that is not measured.

### Which board: the clock

What decides how well the recorder's clock can be put on the board's is the clock behind the board's main chip:

| Clock | Boards | How far off nominal | Its sync line |
|---|---|---|---|
| Crystal | Teensy 3.2 and 4.x, Nano ESP32, Pico and Pico 2 (and the Nano RP2040 Connect), Leonardo and Micro, Due, Zero and the MKR boards, Nano 33 IoT, Giga R1 | tens of ppm, and drifts a few ppm with temperature. The test rig's Nano ESP32 was 22 to 30 ppm off against its three cameras' clocks, which differ by 8 ppm among themselves | matched at any rate the search covers; one line when it fits, else 30 s lines |
| Ceramic resonator | Uno R3 and the classic Nano (ATmega328P; the shiny 16 MHz crystal on an Uno drives its USB chip, not the 328P); the Mega 2560's part varies, so measure yours | about 0.5%, and more with temperature | matched the same way; its rate wanders more, so expect the 30 s lines |
| Internal oscillator | Uno R4 Minima (about 1% off on one rig), Nano Every and Uno WiFi Rev2 (ATmega4809, factory-calibrated to about 2%) | 1 to 3% | matched the same way, within the search's 3% |

A resonator or internal-oscillator board also triggers the cameras at the wrong rate: a "100 fps" Uno R4 Minima runs at about 99 or 101 fps. `campy-align` and `campy-syncmatch` don't mind, but anything that assumes the nominal rate will. So use a board with a crystal: the Nano ESP32 in [the hardware list](hardware.md), or a Teensy. How far an edge moves in the loop matters little here: a recorder sampling at 30 kHz only sees 33 µs steps.

### Matching the recorder's pulses: `campy-syncmatch`

Export the sync line's rises from the recorder (and its falls, if you can), then:

```bash
campy-syncmatch edges.csv --sync <recording>/sync.json [--sample-rate 30000] [--frames 72000]
```

The edges file is a `.npy` array or a `.csv`/`.txt` file: one pulse per row, its rise time and optionally its fall time, with an optional header line. The times are in seconds, or sample numbers with `--sample-rate`. The ChronicRecord Pico's `sync_*.json` works as `--sync` too (it has no frame period, so no edge is held back).

What it does:
- works out every planned edge from the seed, with the held ones where the sketch put them;
- finds the board's clock rate first. It reads every stretch of four gaps at each rate within `--max-rate-error` of nominal (3% by default, in steps of 0.01%), and keeps the rate at which the most stretches name a place in the plan. A gap must read within 0.3 ms of a whole millisecond. At a fixed nominal rate, a clock more than about 300 ppm off would fail that check, so resonator and internal-oscillator boards could not be matched at all;
- fits recorder time = *a* + *b* × board time through every matched pulse. That is one line if it fits to `--line-us` (100 µs); otherwise one line per 30 s of board time (`--segment-s`), which follows a clock whose rate wanders with temperature. On made-up pulses whose rate swung by 100 ppm over an hour, one line was off by tens of ms, and the 30 s lines by under 35 µs, at 30 kHz sampling;
- reports where board time 0 falls on the recorder's clock, the rate in ppm, the residuals, and every pulse missing from the recorder or extra on it. A pulse of the wrong width, when falls are given, is reported as extra too.

It writes `<edges>_syncmatch.csv` (one row per pulse: recorder time, pulse number, board time, residual, or why it is missing or extra) and `<edges>_syncmatch.json` (the fit and the counts). With `--frames N`, it also writes `<edges>_frames.csv`: frame *k*'s trigger on the recorder's clock, for the first *N* frames. A camera that missed triggers, or a trigger that paused, still needs `campy-align`'s trigger numbers to say which trigger each saved frame came from.

Tested on made-up pulses (`tests/test_syncmatch.py`): crystal-like (3 and 40 ppm), resonator-like (0.5%) and internal-oscillator-like (1% and 2.5%) clocks, sampled at 25 or 30 kHz, with 1 to 2% of pulses lost, stray pulses added, and the recorder starting late. Every pulse was matched, with residuals within about half a sample (under 20 µs at 30 kHz). The test also runs the simulated sketch's own sync edges through a recorder clock running 0.4% fast, and checks every frame's trigger time to within 30 µs. Not yet checked: a real board's sync line recorded by a real recorder (the test above had nothing on the sync pin).

## Aligning cameras after recording
```
campy-align ./test/2026-10-18_17-30-05        # the recording's folder (its videoFolder with sessionFolder False)
```
puts every camera on the trigger's timeline (`campy-session` runs it after each recording, its `align` step): trigger *k* is at `t_s` = (*k* − 1) × the trigger period. It writes `aligned.csv` in each aligned camera's folder (`frame`, `trigger`, `t_s`; a RatCam's `trigger` is fractional, between the pulses around the frame's start, and its `between_pulses` is 0 for a frame from before its first pulse or after its last, placed from its own clock) and `alignment.json`, and prints a summary:
- **The trigger behind each frame**, from the camera's own timestamps: a step of *n* periods means *n* − 1 triggers left no frame. Those campy dropped itself (gaps in `frametimes.npy`'s frame numbers) are counted apart from those the camera missed.
- **Where the trigger paused.** Triggers that no triggered camera has a frame for, with two cameras or more, are a pause of the trigger, not misses: `The trigger paused 2 times, 68 triggers (2.27 s) in all, with no frame in any camera: 11 triggers (0.37 s) from trigger 13025 (434.12 s in); 57 triggers (1.90 s) from trigger 13037 (434.52 s in).` (a four-camera recording on the Dell Pro Max Slim FCS1250, 26 October 2026). No frame is lost there, but every frame after is later than its number says, so use `t_s`, not the frame number, to line the video up with anything else. `alignment.json` lists them in `trigger_pauses` (`first_trigger`, `triggers`, `seconds`, `at_s`), and each camera's `in_trigger_pauses`; its `missed_by_camera` leaves them out. A camera's frames campy dropped in every camera at once are no pause: the cameras sent them. With one triggered camera, a pause and triggers the camera missed look the same, and are counted as missed.
- **Where each camera started.** With `startArduino: True`, campy starts the trigger once every camera is grabbing and says which were in `trigger.json`; `campy-align` starts those cameras at trigger 1. If the start command can't be sent (the trigger Arduino unplugged since its check), campy says so on the console, and `trigger.json` says the trigger did not start, with the error (`startError`). No camera gets a frame then, and the recording watch raises its critical "no frame" alert 10 s later. It checks each against the other cameras of its kind, and a camera whose first frame came a whole period or more after theirs is reported as having missed its first triggers. Cameras can still start on different triggers: a camera named as not ready, an external trigger started too early, or a recording without `trigger.json` (from a campy that started the trigger first). For those, the arrival times (`hosttimes.npy`, and `ratcam_sync.csv` for a RatCam's pulses) say by how many triggers, against the cameras that were grabbing, or else the earliest camera's first trigger becomes trigger 1. A RatCam's pulses reach the computer within a moment of their trigger, a triggered camera's frames a few ms after it; when a triggered camera's frames take near a whole period, its place against a RatCam (or a RatCam's against it) could be off by one, and the summary says so. Recordings without `hosttimes.npy` (from before this version) assume every camera started on the same trigger.
- A RatCam needs a periodic trigger on its SYNC input. A random sync train (`ratcamSyncOut`) cannot be lined up this way.
- A recording that stopped without closing has each file up to its own last write, about every second:
  - frames in a RatCam's `frametimes.npy` that `ratcam_frames.csv` had not written yet: `campy-align` aligns the frames both have, and says how many it left out. A RatCam with none yet is reported with no frames, and its pulses still place the other cameras;
  - frames in `frametimes.npy` without arrival times in `hosttimes.npy` (written just after it): every frame is aligned, the start is worked out from the arrival times there are, and the summary says so;
  - `ratcam_sync.csv` not yet in order of time (campy sorts it when the recording closes): `campy-align` sorts it.

## Checking that cameras expose together
`campy-synctest` checks from a recording that triggered cameras really expose at the same moment, and says how far apart they are, by filming one LED that the trigger controller blinks from the same clock as the trigger.
1. Wire an LED and a 330 Ω resistor in series from a spare pin of the trigger controller to ground, where every camera can see it. A red LED suits visible-light cameras; behind an IR-pass filter, use an IR LED.
2. Upload the current `trigger.ino`: it reads the test LED's pin and rate after the frame rate. (An older sketch ignores them and never blinks; campy says so at the start.)
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
