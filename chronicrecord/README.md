# ChronicRecord timer

The ChronicRecord rig's TTL outputs, made by a Raspberry Pi Pico instead of the Raspberry Pi 3B's own GPIO pins:

| Output | What it drives | Pi 3B before (chronicControlv13.py) | Pico now |
|---|---|---|---|
| A | OneBox / SpikeGLX record gate (level) | GPIO 5 | GP2 (board pin 4) |
| B | FLIR camera trigger (Line3), 3 ms high | GPIO 12 | GP3 (board pin 5), through a 50 Ω resistor |
| C | Blue LED controller TTL (465 nm) | GPIO 13 | GP4 (board pin 6) |
| D | Yellow LED controller TTL (560 nm) | GPIO 19 | GP5 (board pin 7) |
| E | Sync pulses to the OneBox's XA1 (optional, off by default): see [Sync pulses for the OneBox](#sync-pulses-for-the-onebox) | (none) | GP6 (board pin 9) |
| | Every cable's ground | GND | GND (board pin 3 or 8) |

Since 5 November 2026 the Pico is plugged into the recording PC, which runs the experiment: `chronic_control.py` plans the whole session and loads it onto the Pico over USB. Then the Pico makes every edge from its own crystal clock. Until then a Raspberry Pi 3B did the same over USB, started over SSH; the checks dated 2 and 4 November below ran that way, and the Pi is no longer used. campy doesn't take part: on this setup the campy configs need `startArduino: False`, and the cameras just record the pulses on Line3. Start campy first, so that every camera is grabbing before the Pico's `start`.

- `chronic_timer/chronic_timer.ino` is the Pico's program (the same file also runs on an Arduino Nano ESP32).
- `chronic_control.py` runs the session from the computer the Pico is plugged into: the recording PC (Windows), or any Linux computer, as the Pi did. It needs only Python 3 and pyserial. See [On the recording PC](#on-the-recording-pc).

**Why a Pico:** its PIO hardware runs the outputs by itself from a queue of "these levels, for this many clock ticks". All four pins change in the same clock tick (about 7.5 ns at 133 MHz), and neither USB nor the rest of its program can move an edge unless the queue runs dry, which the Pico counts and reports (`stalls=`). The official Pico design has a 12 MHz crystal. An UNO R4 has no crystal in use (about ±1%), and a Pi's Linux can delay anything done in software.

**Checked on a real Pico (2 November 2026, on the Pi 3B's USB, nothing wired to its outputs).** At 60 Hz every session made exactly the planned pulses with no stalls. `self_check_us` was −1 to −3 µs in sessions of 10 s, 30 s and 120 s (7 runs); it doesn't grow with length, so the state machine keeps its schedule. One 60 s run read −17 µs, which didn't recur: the check is an interrupt, and one can be delayed.

What that doesn't show: the check shares the Pico's crystal, so it can't see how far the crystal itself is from true time. It only watches the camera pin's rises, not their width or the other three pins. The OneBox's recording of the edges is the independent check, below.

**Checked against the OneBox (4 November 2026, wired to the rig).** A 3-bout session (`--set TOTAL_DURATION=900`: bout 1 with 240 s of camera, bouts 2 and 3 with 10 s) ran with SpikeGLX's trigger following the gate: TTL trigger on the OneBox's XA0, "follow high", 1 s margin. campy recorded the 4 cameras at the same time. The OneBox samples every 33 µs (30,303 a second).

- **One SpikeGLX file per bout**, 257.0, 27.0 and 27.0 s long. That is each bout's gate (255 or 25 s), plus the 1 s margin SpikeGLX adds before and after.
- **The gate:** in each file, exactly one rise and one fall. It read 3.16 to 3.20 V high and 0.004 V low. Camera pulses moved it by 5 mV at most, and the LEDs switching not at all. The one sample on each edge that reads in between is the edge itself.
- **Against the plan:** the camera's first pulse came 5 s after the gate rose (to the sample), and the gate fell 10 s after the camera's last pulse. The camera made 14,400, 600 and 600 pulses. Both LED pulses were 30.0 ms, at their planned times within a sample once the clock difference below is taken out.
- **campy:** 15,600 frames on each camera, split 14,400, 600 and 600 between the bouts, with no frame missing.
- **Clocks:** the gate rose every 300.000990 s by the OneBox's clock (300.000967 s by the probe's), the same to the sample both times. So the Pico's crystal and SpikeGLX's clocks differ by 3.3 parts per million, about 0.3 s a day. Which one is off isn't known. It doesn't matter for lining up the data, because SpikeGLX records every edge.

**The gate stays low in a camera-only session** (checked the same evening). With SpikeGLX armed the same way, a 2-minute camera-only session at 60 fps (`--set CAMERA_ONLY=120`) made its 7,200 camera pulses and opened no SpikeGLX file. One gate blink just before it and one just after (`wiring --channels A`) each opened a file, so SpikeGLX was armed the whole time.

**The 50 Ω resistor on the camera pin** (fitted and checked the same evening). Until then the camera cable rang as each pulse fell, and the Pico's own input counted a second rise on every pulse: `bounces=` equalled the pulse count in every session (15,600 of 15,600 in the 3-bout session above). The cameras never saw the ring: campy got one frame per pulse. A 50 Ω resistor in series with GP3 stopped it:

- **The Pico's own count:** in a 1-hour, 12-bout session (the Pi's default plan, 34,800 pulses) and in a 10 s camera-only burst (600 pulses), `rises=` equalled `pulses=` and `bounces=0`. The Pico's `trace` of the burst shows only up, down, up, down: each pulse 3,000 µs wide, nothing extra at the fall.
- **On the OneBox (XA2):** the pulse is still 3.003 ms wide, 16.665 ms apart, every one where it should be, and the digital copy (XD bit 2) still within a sample of it. Each edge still completes within one sample (33 µs), as before. The levels moved: 2.97 V high and 0.19 V low, against 3.10 V and 0.08 V without the resistor. The low sits at 0.19 V even between bouts, so a small steady current flows back into the pin while it is low, probably from the cameras' Line3 inputs (*expected, not measured*). No other channel moved by more than a millivolt, and the camera pulses still shift the gate by about 4 mV.
- **SpikeGLX:** one file per bout, 257 s or 27 s, 12 in all, with 14,400 or 600 camera pulses in each, as planned.
- **campy:** 34,800 frames on each of the 4 cameras, split 14,400 / 600 / 600 / 600 / 600 / 600 twice over, with none dropped, lost or incomplete. Frame intervals 16.66 to 16.67 ms within each bout.

The OneBox samples every 33 µs, too slowly to see the ring itself: it didn't show there before the resistor either. The Pico's own count is the check that it's gone.

Not checked: a session of more than 12 bouts, an animal in the rig, and the LEDs' light (only their TTLs).

**Long recordings:** the Pico holds 12288 edges and 2048 camera trains, 2048 bouts: 7.1 days at one bout per 300 s. A longer session is refused at `start` ("too many edges"). Camera-only sessions have no such limit (2^32 pulses). For 6 days: `--set TOTAL_DURATION=518400`. Loading that many bouts takes `start` up to a minute or so.

## Why

On the Pi, everything was timed in software:

- **Camera clock.** `lgpio.tx_pwm` is software-timed PWM (lgpio's docs say so; the class name in the script says "hardware", but it isn't). lgpio's own thread switches the pin. Each frame's trigger is late by however long Linux takes to wake that thread: typically tens of microseconds, sometimes much more when the Pi is busy (SSH, the SD card, the status bar). *Expected, not measured.*
- **LED pulses were ~100 ms, not 30 ms.** `sleep_with_status()` sleeps in 0.1 s steps (so it can redraw the status bar). Every phase is rounded up to the next 100 ms, so a "30 ms" pulse lasted at least ~100 ms. The yellow LED came on 100+ ms after the blue one, not 30 ms. Each switch then also waited for a log line to be written to the SD card.
- **LEDs weren't locked to frames.** The LED schedule and the camera clock ran on separate timers. So which frame an LED came on in shifted from bout to bout, and an LED could switch partway through a frame's exposure (a half-lit frame).
- **The log's times are Python's guesses.** The `timestamps.txt` times are `datetime.now()` after each switch, and "Est. Frames" is computed, not counted.

SpikeGLX/OneBox records the real TTL edges, so the true LED widths and gaps can be checked in an existing recording.

On the Pico (or Nano):

- The camera trigger is a pulse train on its crystal. Pulse *k* rises at exactly `start + floor(k × 10⁶ / 60)` µs: 16666 or 16667 µs apart, exactly 60 Hz on average. It stops after an exact count (600 pulses in a standard bout, 14400 in an extended one).
- The LEDs are 30 ms by default. Blue switches on 1 ms before camera pulse 300 (5 s into the camera) and off 30 ms later; yellow follows on the same microsecond. `YELLOW_FIRST=1` puts yellow first, and `LED_GAP` leaves that long dark between the two (the panel's config does both: [Fiber photometry](#fiber-photometry-snapshots)). With 200 µs exposures, blue lights frames 300–301 and yellow 302–303, each frame wholly lit or wholly dark. `python3 chronic_control.py plan` lists this for every bout, and warns if a setting would put an edge inside an exposure.
- Edges due together are written in one register write: one LED off and the other on (with no `LED_GAP`), or any edge that lands on a camera pulse.
- On the Pico, each edge is at its scheduled microsecond exactly, on the state machine's clock (simulated tests, to the tick; on the real board, see above). On the Nano, the sketch busy-waits the last 300 µs with interrupts off: expected within ~1–2 µs, not measured.
- At the end of a session the Pico reports `rises=` (the camera pin's rises, counted by an interrupt) and `self_check_us=` (the time from first rise to last by its microsecond timer, minus the schedule's). Expect `rises=` equal to `pulses=` and a few microseconds at most. `bounces=` counts rises too soon after the last one to be a pulse (a ringing cable); expect 0 with the 50 Ω resistor on GP3.
- The board runs the loaded session by itself. If the computer's program dies, or the USB link drops (as Arduinos on the lab's rigs often do), the edges don't move. The program reconnects and carries on logging. The board reports each edge with its board time and how late it was, and each camera train's exact pulse count. `timestamps.txt` records those, not Python's guesses.
- No `sudo` and no GPIO access: the computer needs only the Pico's serial port.

## The protocol

The same session as chronicControlv13.py, with the settings at the top of `chronic_control.py`. Every bout, every `INTERVAL` (300 s), for `TOTAL_DURATION` (3600 s, 12 bouts):

1. Gate high.
2. 5 s pre-baseline.
3. Camera on at 60 Hz.
4. 5 s later, blue LED 30 ms, then yellow LED 30 ms. (From the panel: yellow 50 ms, 150 ms dark, then blue 50 ms; see [Fiber photometry](#fiber-photometry-snapshots).)
5. Camera off after 10 s of camera (240 s in bouts 1, 7, ...).
6. 10 s post-baseline.
7. Gate low.

`plan` and `start` refuse settings that a bout can't have: a bout that runs into the next, an LED outside the camera's time, or the camera starting before the gate opens (`CAMERA_LEAD_IN` longer than `TOTAL_BASELINE`). From campy's panel, the page's **Bout settings** change them for one session ([From the panel](#from-the-panel)).

To change a setting for one session without editing the file:

```bash
python3 chronic_control.py start --set TOTAL_DURATION=1800 --set PULSE_WIDTH_C=0.050
```

## Fiber photometry (snapshots)

The blue and yellow LEDs are a fiber photometry system's excitation light. On the rig:

- The Pico's outputs C (blue) and D (yellow) go to a two-channel Doric LED driver (stand-alone, TTL mode), which drives a 465 nm and a 560 nm Doric CLED.
- A Doric FMC5 fluorescence minicube (excitation 465–480 and 555–570 nm, emission 500–540 and 580–680 nm) sends both lights down one patch cord (400 µm core, NA 0.57) to the mouse's implanted fiber. It sends the two emission bands to two New Focus 2151 femtowatt photoreceivers.
- The receivers' outputs go to the OneBox: **XA5 green** (500–540 nm), **XA6 red** (580–680 nm). SpikeGLX records them while the gate is high, on the same clock as the gate, camera and LED lines, so every reading lines up with the camera frames to a sample (33 µs).
- **The receivers must be on DC Low** (2×10¹⁰ V/A, DC to 750 Hz). On their AC settings (30 to 750 Hz) a flash's steady level is filtered out. In a test on 5 November (no animal) they were on AC: each flash showed as a jump that sagged back within the flash and swung negative when it ended.
- **Set the LED currents with a power meter** at the patch cord's tip. In the same test, with 100 mA on each LED and no animal, the blue flash pushed both XA5 and XA6 to +5 V, the top of the OneBox's ±5 V range. With a mouse the receivers see far more light than with the bare patch cord: see [the first recordings with a mouse](#first-recordings-with-a-mouse-10-november-2026) for the levels that fit.
- **Each receiver runs on a 9 V battery.** A receiver whose battery is flat reads a steady 0.2 V or so and doesn't move when its LED flashes; that was the cause of the first gate test on 10 November.
- Between flashes the receivers' outputs carried about 70 mV rms of noise, mostly 60 Hz mains hum (same test). Averaging over whole 60 Hz cycles (16.7 ms) removes the hum: averaged over 20 ms it was 4 to 5 mV rms.

**The pattern.** The panel's config ([`chronic_session.yaml`](chronic_session.yaml)) reads the neuro-seeER reporter (EGFP and FusionRed) with the "snapshot" pattern of its paper (Neuron, 2026; [doi:10.1016/j.neuron.2026.07.031](https://doi.org/10.1016/j.neuron.2026.07.031)). Once a bout (every 5 minutes), it gives 50 ms of yellow (560 nm), then 150 ms dark, then 50 ms of blue. These are `pico.set`'s `YELLOW_FIRST: 1`, `PULSE_WIDTH_D: 0.05`, `LED_GAP: 0.15` and `PULSE_WIDTH_C: 0.05`. 50 ms is exactly 3 frames at 60 Hz and 150 ms exactly 9, so every edge still falls 1 ms before a camera pulse: yellow lights frames 300–302 and blue 312–314, each frame wholly lit or wholly dark. The paper's system (a Neurophotometrics FP3002) recorded the emission with a camera chip; here the two receivers are sampled every 33 µs, so each flash gives about 1,500 samples per channel. `chronic_control.py`'s own settings, and the PowerShell launcher's, keep chronicControlv13.py's pattern (blue 30 ms, then yellow 30 ms at once).

**Reading the snapshots.** [`photometry_snapshots.py`](photometry_snapshots.py) reads a session's OneBox files (needs numpy only; run it on a copy of the session, not on the rig while it records) and writes `photometry_snapshots.csv`, one row per bout:

```
python photometry_snapshots.py D:\data\<date_time> --campy D:\data\<date_time>
```

- **The flashes:** each LED's time on the OneBox's clock, its width, the camera frames it lit (counted from the bout's first), how long before its first frame's pulse it switched on, and the dark between the two flashes.
- **The readout:** each receiver's mean over the last 16.7 ms (one 60 Hz cycle) of each flash, less its dark level over the 16.7 ms before the first flash, 1 ms clear of every edge. `green_V` is XA5 in the blue flash and `red_V` XA6 in the yellow one. XA5 in yellow and XA6 in blue are the bleed between the channels.
- **Flashes shorter than 18.7 ms** (a cycle plus 1 ms at each edge) are read over what they have, from 1 ms after the LED comes on to 1 ms before it goes off, never the dark before the flash or the other LED's flash. The value is still given, and `note` says over how many milliseconds, less than a cycle, so the mains hum wasn't averaged out of it (for example `blue flash: read over 8.0 ms, less than one 60 Hz cycle (hum not averaged out)` for a 10 ms flash). A flash of about 2 ms or less leaves nothing 1 ms clear of its edges: its values are blank and `note` says so. The same goes for the dark level if the first flash comes within 17.7 ms of the file's start. The 30 ms and 50 ms flashes are read over a whole cycle, as before.
- **Checks:** how long each receiver took to reach half its step after its LED's edge, any sample at the OneBox's ±5 V limit (`note`), and the dark level's noise.
- **With `--campy`:** each camera's frames in each bout next to the OneBox's camera pulses, and the flashes' frames in campy's video (counted from 0, the recording's first frame), so each reading can be put next to the video. They come from each camera's `aligned.csv` (campy-align, which runs after every panel recording), which gives every frame the Pico pulse behind it. A frame the Pico didn't send (a stray edge on the trigger line) or a frame that is missing then shifts nothing else: the frames outside the bouts are listed, and if the cameras' videos no longer agree, the flash's frames are given for each camera (`Camera0 1501-1503; Camera1 1500-1502`). Without `aligned.csv` it falls back to `frametimes.npy`, a new bout where frames stop for more than 1 s, and says so; an extra or missing frame shifts every later bout's numbers there.

It ends with what should be the same in every bout: the order, the frames lit, the widths, the gap and the leads, each as a range.

### First recordings with a mouse (10 November 2026)

A fiber-implanted mouse, the receivers on DC Low, the snapshot pattern above and the panel's "Gate test (15 min, 3 bouts)": three bouts, the first with 240 s of camera and the other two with 10 s, each with one yellow and one blue flash. A Neuropixels 2.0 probe was plugged into the OneBox but not implanted.

**The rig's red camera lights reach the red receiver.** With the room's red lights on (they light the cage for the cameras), XA6 sat at the OneBox's +5 V limit for the whole recording, dark included, while XA5 read its usual 0.03 to 0.05 V in the dark. Red light passes through tissue: it reaches the implanted fiber through the head, or past the ferrule's mating sleeve, and the red receiver passes 580–680 nm. With the lights off, XA6 read 0.13 to 0.24 V in the dark. The cameras need those lights, so long recordings need the light kept out at the mouse's end instead: an opaque sleeve (black heat-shrink) over the mating sleeve and a cover over the headcap, or infrared camera lights if the cube's 580–680 nm band rejects them. Neither has been tried yet.

**21 µW is too much with a mouse.** With each LED set to 21 µW at the fiber tip before the mouse was connected (blue 115 mA, yellow 150 mA), both readings were at the +5 V limit for the whole of their flash in every bout: green in the blue flash and red in the yellow one. Both reached the limit within 0.1 ms of the LED coming on, so how far over they were isn't known.

**The levels with the mouse connected**, each LED on in the driver's continuous mode for a few seconds, read live from SpikeGLX:

| | XA5 (green) | XA6 (red) |
|---|---|---|
| dark (lights off) | 0.04 V | 0.20 V |
| blue at 20 mA (5 µW) | **2.90 V** | 4.26 V (blue light reaching the red receiver) |
| blue at 18 mA | about 2.8 V | |
| yellow at 20 mA (3 µW) | 0.05 V | **3.40 V** |
| yellow at 17 to 18 mA | | about 3.0 V |

The powers in brackets were measured at the fiber tip after the mouse was disconnected. Both LEDs flicker on and off at 17 mA and are off at 16 mA (the driver's lowest steady current). So with this light path and this mouse, both readings fit under 5 V only with the LEDs a few mA above the point where they flicker. The LEDs' power isn't in proportion to their current: blue gave 5 µW at 20 mA and 21 µW at 115 mA, yellow 3 µW at 20 mA and 21 µW at 150 mA.

**A snapshot recording at 20 mA on both LEDs** (blue 5 µW, yellow 3 µW at the tip). Nothing clipped, and the readings hardly changed between bouts:

| bout | `green_V` | `red_V` | XA6 in the blue flash | XA5 in the yellow flash |
|---|---|---|---|---|
| 1 | 2.868 V | 3.280 V | 4.063 V | 0.008 V |
| 2 | 2.873 V | 3.269 V | 4.077 V | 0.008 V |
| 3 | 2.875 V | 3.266 V | 4.076 V | 0.008 V |

- The three bouts' readings were within 0.25% (green) and 0.4% (red) of each other, so the 50 ms flashes at 20 mA were as steady as the continuous light. The dark level's noise was 8 to 9 mV rms.
- Each receiver reached half its step 0.33 to 0.36 ms after its LED's edge.
- The dark level fell after the first bout (XA5 from 0.051 to 0.008 V, XA6 from 0.22 to 0.055 V), most likely some remaining room light going off. Each reading has its own bout's dark level taken off, so the readings didn't move.
- The timing was as planned in every bout: yellow lit frames 300–302 and blue 312–314, each flash 49.995 to 50.028 ms, 149.985 to 150.018 ms of dark between them, each LED on 990 to 1023 µs before its first frame's pulse. All four cameras saved one frame per Pico pulse (15,600), and the Pico reported nothing late or lost.

**What to change for long recordings.** 20 mA leaves 2 to 3 mA between a good reading and a flickering LED, and XA6 in the blue flash at 4.1 V is close to the limit. For this mouse the readings want about 4 to 5 µW of blue and 3 µW of yellow at the fiber tip. An absorptive neutral density filter of about OD 0.5 (a third of the light) in an in-line fiber filter mount on each LED's patch cord, between the LED and the cube, with connectors that match the cords (FC or SMA), would put those powers at roughly 65 to 75 mA, well clear of the flicker. OD 1.0 (a tenth) would need 30 to 45 µW out of each LED, more than the highest powers measured here (21 µW), at currents not measured yet. These currents are interpolated between the two measured points of each LED, not yet tried, and another mouse's implant may want different powers. The 20 mA recording's `session.json` says 21 µW for both LEDs, left over from the earlier setting: the real powers were 5 µW (blue) and 3 µW (yellow).

**Two things seen once, in the same evening's recordings:**

- One recording had a camera frame between bouts that the Pico didn't send: all four cameras took a frame at the same moment, 46 s after the second bout's last pulse. campy stops at the planned number of frames, so the third bout lost its last frame. Its frames in campy's numbering are then one later than `photometry_snapshots.py --campy` gives (it counts the Pico's pulses), and `--campy` splits that bout in two. Each camera's `aligned.csv` gives every frame's own Pico pulse and is right. The other recordings that evening had no such frame.
- After a recording was stopped early, SpikeGLX started the next run without the OneBox: its console showed `385CH` (the probe alone) and repeated `obx stream not enabled`, and the panel stopped the start after 120 s ("SpikeGLX's run didn't start acquiring"). Quitting SpikeGLX and pressing Start again, so that the panel opened it afresh, brought the OneBox back. SpikeGLX's saved settings were the same as for the runs before and after.

Not checked yet: keeping the camera lights out at the mouse's end, the LEDs' currents behind a neutral density filter, a session longer than 15 minutes with a mouse, and the patch cords other than the one to the mouse.

## Running it

On the computer the Pico is plugged into, in a Python environment with pyserial (the recording's own, on the PC; `python3` on Linux):

```bash
python chronic_control.py start
```

```bash
python chronic_control.py status
```

```bash
python chronic_control.py stop
```

- `start` checks that the board answers, loads the session, starts it, and returns. The session keeps running in the background. On Windows, start it from a window on the desktop, not over SSH (see [On the recording PC](#on-the-recording-pc)); `status` and `stop` work over SSH from another computer.
- `stop` ends the session after the current bout, as typing `cancel` did. The bout's gate still goes low on time.
- `stop --now` ends it at once. The camera finishes the pulse it is on, then every output goes low. With no session program running, `stop --now` tells the board itself to stop.
- `plan` prints the session's timeline without touching the board.
- `run` runs the session in the terminal. Ctrl-C there is `stop --now`.
- `attach` takes over a session the board is still running after the program that started it stopped (the computer restarted, or the program was killed): it carries on logging to the same `timestamps.txt` until the session ends, and `stop` works again. The board's edges while nothing was attached aren't in the log; SpikeGLX has them. See [On the recording PC](#on-the-recording-pc).

The log is `~/Documents/<date>/timestamps.txt`, as before. The program's own output goes to `~/.chronic_control/session.out`. `status` reads `~/.chronic_control/status.json`, which the session rewrites as it runs. Like every JSON file campy writes, it is strict JSON: an infinite or NaN number would be the text `"Infinity"` or `"NaN"` ([Recording folders](../docs/recordings.md#infinity-and-nan-in-campys-json-files)).

## Camera-only sessions

No bouts, no LEDs, and the gate stays low, so SpikeGLX doesn't record (checked against the OneBox: see "Checked against the OneBox" above). Only the camera pulses, for as long as you say. Use it for calibration or plain recordings:

```bash
python3 chronic_control.py start --set CAMERA_ONLY=3600 --set CAMERA_FPS=30
```

`stop` ends one at once, on a whole pulse (a few ms to 20 ms later, what's already queued).

**From campy's panel**, a config's `pico:` section runs one with `set: {CAMERA_ONLY: <seconds>, CAMERA_FPS: <campy's frameRate>}` and one length (`lengths: [{name: "1 min", seconds: 60}]`), `gate_check: false` (no SpikeGLX). campy's `recTimeInSec` is `CAMERA_ONLY`, from the Pico's plan. Since 10 November 2026 the rig's calibration (60 s at 10 fps), look and live-tracking experiments are camera-only Pico sessions like this. Before that they still started the Arduino, which hadn't been on the PC since 2 November, and every one stopped 2 s in ([One trigger board per rig](../docs/sync.md#one-trigger-board-per-rig)).

## Checking the wiring

```bash
python3 chronic_control.py wiring
```

Each output blinks on its own, its letter's number of times: A (gate) once, B (camera) twice, C (blue) three times, D (yellow) four times, half a second on and half off, with a pause after each. The sync output, E, blinks five times, only when asked for (`--channels E`, version 3). It goes round until `stop`. Watch for them in SpikeGLX, on a meter or with an LED probe.
- `--channels AB` blinks only those outputs.
- `--pause 60` waits a minute between rounds.

The gate starts a SpikeGLX file each time, if SpikeGLX is armed: about 23 MB a second with the probe recording, so mind the disk when leaving it running.

## Setting it up

1. **Flash the Pico.** Hold BOOTSEL while plugging it in; it appears as a drive named `RPI-RP2`. Copy `chronic_timer.ino.uf2` onto it (built with the [arduino-pico](https://github.com/earlephilhower/arduino-pico) core, board `rp2040:rp2040:rpipico`). It restarts as a serial port, and `id` should answer `ok chronic_timer 3` (version 3 added the sync output, GP6; version 2 runs everything else the same).
2. **Wire it** as in the table at the top (in place of the Pi 3B's GPIO pins, on this rig). Don't connect anything above 3.3 V to a Pico pin: they aren't 5 V tolerant.
3. **Pull-down resistors.** Fit a 10 kΩ resistor from each of GP2–GP5 to GND (and GP6, if the sync output is wired), so that nothing reads high while the Pico resets or is being flashed.
4. **A 50 Ω resistor in series with GP3** (the camera output). Without it, the camera cable rings as each pulse falls, and the Pico's own input counts a second rise on every pulse (`bounces=` equal to `pulses=`). The cameras didn't see that ring, but the resistor stops it: see "The 50 Ω resistor on the camera pin" near the top.
5. **Voltage.** The Pico's outputs are 3.3 V, the same as the Pi 3B's GPIO that drove these inputs before. The Blackfly S Line3 counts 2.6 V or more as high, so 3.3 V works, with a small margin. With the cameras and the 50 Ω resistor on it, the camera line reads 2.97 V high on the OneBox.
6. **Plug the Pico into the recording PC by USB.** On Windows it is a COM port ("USB Serial Device"), which `chronic_control.py` finds by the Pico's USB IDs; on Linux it shows up in `/dev/serial/by-id/`, which it finds by name.
7. **On the PC,** record with `cameraTrigger: "Line3"`, `startArduino: False`, and `frameRate` matching `CAMERA_FPS`. Put `triggerController: "pico"` and `startArduino: False` in the rig profile itself, so that no config on the PC can start an Arduino on the same cameras ([One trigger board per rig](../docs/sync.md#one-trigger-board-per-rig)). For bouts:
   - `alertNoFramesSec` above the longest gap between bouts (about 300 s), or campy reports "the trigger has stopped" in every gap.
   - `alertTriggerPauseSec` below the shortest gap between bouts (60 s, after a 240 s bout), or campy sends a "trigger paused" warning for every gap.
   - `alertExternalWaitSec` above how long campy waits before the Pico's `start` (default 10 minutes), or campy sends "No frame yet ... is it running?" while it waits.
   - `recTimeInSec` equal to the session's total camera time (580 s by default), since campy stops after `recTimeInSec` x `frameRate` frames.
   - [`chronic_pico.yaml`](chronic_pico.yaml) is the profile for multi-day sessions; see [Recording a long session](#recording-a-long-session).
8. **Don't run chronicControlv13.py once the cables are on the Pico:** it would drive GPIO pins that nothing is wired to.

## Recording a long session

[`chronic_pico.yaml`](chronic_pico.yaml) is the campy profile the rig uses for bout sessions of several days. It extends the rig's own profile (`extends: chronicrecord`: four cameras, Quick Sync H.265) and changes only what the Pico needs, plus the denoise filter. Each line's comment says why.

| Setting | Value | Why |
|---|---|---|
| `frameRate` | `60` | the Pico's camera rate (`CAMERA_FPS`) |
| `startArduino` | `False` | the Pico makes the pulses; campy writes `startedBy: external` |
| `cameraTrigger` | `"Line3"` | where the Pico's camera cable goes in |
| `recTimeInSec` | `83520` | the 6-day session's total camera time, so campy stops after the last bout |
| `alertNoFramesSec` | `400` | above the longest gap without pulses, 290 s (300 s between bouts, less 10 s of camera) |
| `alertTriggerPauseSec` | `30` | gaps of 30 s or more are planned: logged, not sent. Below the shortest gap, 60 s (after a 240 s bout) |
| `alertExternalWaitSec` | `3600` | campy is started well before the Pico's session (16 minutes before it in the 1-hour rehearsal on 4 November, whose only false alarm was this warning at 10 minutes); an hour still catches a session that was never started |
| `alertDiskFreeGB` | `500` | warns with about a day's room left: SpikeGLX fills the same disk at about 475 GB a day, which campy's default (20 GB) would announce only an hour ahead |
| `denoise` | `"atadenoise"` | smaller videos: ffmpeg's temporal denoiser before the encoder changes no pixel by more than 4 gray levels and left tracking unchanged; on H.265 Quick Sync it saved 6-22% in a test, more for a still scene ([Denoising before encoding](../docs/encoding.md#denoising-before-encoding-denoise)). Turned on 9 November 2026 |
| `stopFile` | a file | creating it stops the recording cleanly, from SSH or a script |

**`recTimeInSec` for another length.** campy counts camera time, not the wall clock. `python chronic_control.py plan --set TOTAL_DURATION=<seconds>` ends with the session's camera pulses; `recTimeInSec` is that number divided by 60. By hand, with the default settings: bouts = `TOTAL_DURATION` / 300, of which every 6th (bouts 1, 7, 13, ...) is 240 s of camera and the rest 10 s.

| Session | `TOTAL_DURATION` | Bouts (long) | Camera pulses | `recTimeInSec` |
|---|---|---|---|---|
| 1 hour (the default) | 3600 | 12 (2) | 34,800 | 580 |
| 4 days | 345600 | 1152 (192) | 3,340,800 | 55680 |
| 6 days | 518400 | 1728 (288) | 5,011,200 | 83520 |

**Order.** Start campy first and wait for "All 4 cameras are ready.", then the Pico. campy's panel does this for you ([From the panel](#from-the-panel)); by hand, `python chronic_control.py start --set TOTAL_DURATION=518400` on the PC. A camera that isn't grabbing when the first pulse comes misses frames the others have. Start the Pi within an hour of campy, or campy warns that it has had no frame yet (`alertExternalWaitSec: 3600`; 10 minutes with a campy before pull request 267, which refuses that line).

**Stopping early.** `chronic_control.py stop` (on the PC, or over SSH), then Ctrl-C in campy's window (or create the `stopFile`). campy otherwise waits for frames that won't come.

**What the alerts say.** Every gap between bouts looks to campy like a trigger pause: it counts the pulses it would have had at 60 fps. With `alertTriggerPauseSec: 30`, those gaps are planned pauses: each is a `Trigger: The trigger paused as planned ...` line on campy's console and in the session's `alerts.log`, but nothing goes to Slack. The message as the recording ends counts them ("The trigger paused as planned 1727 times ..."). Slack still gets the start and end messages, a "No frame yet" warning if no pulse has come an hour after campy started (`alertExternalWaitSec: 3600`; needs campy with pull request 267 or later, which an older campy refuses: delete that line there), a real stop (no frames for 400 s) as a critical alert, a pause shorter than 30 s as a "trigger paused" warning, and a camera that misses pulses the others got. No frame is lost, and `frametimes.npy` has every frame's time. Checked on the rig on 4 November (a 3-bout test, about 10.5 minutes, campy at pull request 259 or later): both gaps (1 min and 4 min 50 s) were logged as planned pauses and not sent; campy's start sent no "frames waiting" warning (pull request 258); every camera got 15,600 frames for the Pico's 15,600 pulses. Slack got only the disk warning below and the end message, which is a warning while the disk warning is still open. This needs a campy with `alertTriggerPauseSec` (pull request 259 or later); an older campy refuses the profile's unknown setting, so update the rig first or delete that line, in which case every gap is a "trigger paused" warning, reported as coming and going from the third bout on, with a reminder every 30 minutes.

**Rehearsed for an hour** (4 November, 7:20 to 8:15 PM, campy at dev 2825003): the Pi's default plan, 12 bouts (two of 240 s of camera, ten of 10 s), started by the Pi at a set time with campy already waiting.
- **Alerts:** all 11 gaps were logged as planned pauses and none went to Slack, and campy's start sent no "frames waiting" warning. campy ended by itself after the last bout's frames, with an end message counting the 11 planned pauses.
- **The one Slack warning** was "No frame yet, 10 min after campy started waiting for the external trigger", resolved when the bouts began. campy had been started 16 minutes before the Pi; campy then warned after 10 minutes of waiting for an external trigger. Since pull request 267 the profile waits an hour (`alertExternalWaitSec: 3600`), and the rig runs that campy (dev 1078dd4, from the evening of 4 November) with that profile, so a gap like this one sends nothing.
- **SpikeGLX:** 12 files, one per bout. Checked edge by edge on the OneBox's clock, every bout matched the plan to within a sample (33 µs):
  - gate high 255.0008 s or 25.0001 s;
  - 14,400 or 600 camera pulses, the first 5.000 s after the gate;
  - blue on at 9.999 s for 30.0 ms, yellow from the same instant for 30.0 ms.
- **Clocks over the hour:** from bout 1 to bout 12 the gate rose 3,300.0105 s apart by the OneBox's clock, against 3,300 s planned: the Pico and OneBox clocks differ by 3.2 parts per million (11 ms an hour, 0.27 s a day), the same as in the 3-bout test.

**Rehearsed overnight from the panel** (6 November, 1:38 to 9:34 AM, no animal, LED drivers on, campy at dev 90699e2): the panel's **Overnight rehearsal (8 h, 96 bouts)** length, the 6-day plan cut at 8 hours (16 bouts of 240 s of camera, 80 of 10 s; 278,400 camera pulses), with the Pico on the recording PC and the panel driving SpikeGLX.
- **Pico:** 278,400 pulses, none late, lost or stalled; its session ended by itself at 9:34:22 AM.
- **Cameras:** each of the 4 got 278,400 frames, none dropped by campy and none missed by the camera. campy ended after the last frame, remuxed the videos to plain MP4, and aligned all 4 cameras on the trigger's timeline, with the 95 gaps (1,432,200 pulses' time with no frame, 6 h 38 min) counted as planned pauses.
- **SpikeGLX:** 194 files, two (probe and OneBox) for each of the 97 gate openings: the gate check's blink and the 96 bouts.
- **Clocks over 8 hours:** from bout 1 to bout 96 the gate rose 28,500.0916 s apart by the OneBox's clock (counted at its nominal 30,303 samples/s), against 28,500 s planned: the OneBox gains 3.21 parts per million on the Pico (0.28 s a day), as over the 1-hour rehearsal, and steadily: no bout's rise was more than 72 µs off a straight line (16.6 µs rms). The Pico's log against the PC's clock (each gate's rise as the PC logged it) agrees with the Pico to 0.05 ppm, so the 3.2 ppm is the OneBox's sample rate being a little off its nominal value, not the Pico. On the OneBox the gate was high 25.000 s or 255.001 s, and it counted all 278,400 camera pulses.
- **Disk:** SpikeGLX 150.3 GB, as worked out from its measured rates; the videos only 1.8 GB in all (about 0.5 GB a camera for 77 minutes at 60 fps). With no animal the cameras saw a still cage, which compresses to very little: videos with mice will be much larger. Free space went from 370.6 GB to 217.1 GB.
- **Alerts:** no camera or trigger problem. Slack got the start and end messages, the disk warning below (`alertDiskFreeGB: 500`), and red "The disk ... fills at 150 to 346 MB/s ... down to 2.00 GB free after 16 min more of recording" alerts about every 15 minutes, all false: campy measured its disk's rate over the last 5 minutes per camera frame, and SpikeGLX's writes around each bout's frames were charged to those few frames. Since pull request 297, with planned pauses (`alertTriggerPauseSec`) campy counts that rate over the whole recording.

**Disk: sessions record to a 4 TB data disk.** The videos are the small part: about 140 to 200 GB for the 6-day plan at 60 fps (worked out from 30 fps recordings of mice; not yet measured at 60 fps with animals). SpikeGLX writes to the same drive while the gate is high, plus its trigger margin (1 s before and after each bout on this rig), at about 24 MB/s (23.1 MB/s for the Neuropixels probe, 0.85 MB/s for the OneBox, both measured on 4 November): about 2.7 TB for the 6-day plan's 109,440 s of gate plus 3,456 s of margin. In all about 2.9 TB, roughly 475 GB a day. That never fit on the PC's 1 TB system disk (about 500 GB free), so on 10 November 2026 the PC got a second NVMe SSD, 4 TB (Crucial P310), as D:, and the bout sessions (`chronic_session.yaml`) record to `D:\data` since then; see [Recording many days](../docs/hardware.md#recording-many-days) in the hardware notes. Every other recording goes to D: too: the panel's calibrations to `D:\data\calibration`, its look and live-tracking recordings to `D:\data\look` and `D:\data\live_demo`, and the rig profile's default folder is `D:\data\chronicRecording`. campy's launch check only counts its own videos (about 430 GB here, from the encoder sweep's synthetic frames) and warns above 80% of the free space; it doesn't know about SpikeGLX. campy stops its recording cleanly below 2 GB free, but SpikeGLX would fill the disk first. The panel counts both before the start, and refuses a length that doesn't fit ([From the panel](#from-the-panel)). The profile's `alertDiskFreeGB: 500` sends a disk warning to Slack once less than about a day's room is left; on the 4 TB data disk it comes only once less than about a day's room is left.

## On the recording PC

Since 5 November 2026 the Pico is on one of the recording PC's USB ports, and the PC starts and stops sessions; the Raspberry Pi 3B isn't used. `chronic_control.py` runs there as it did on the Pi. Checked on the rig on 5 November (below); a multi-day session hasn't run this way yet. What differs from the Pi:

- **Python:** the recording's own environment (it has pyserial), with `python` for `python3`.
- **Finding the Pico:** on Windows (and on a Linux PC without `/dev/serial/by-id`), the one Pico among the serial ports, by its USB IDs. It never takes an Arduino there, since campy's trigger Arduino may be on the same PC. With two Picos, or to be sure, name it: `--port COM7`, or by its USB serial number, `--port E665485457699D30` (as `/dev/serial/by-id` shows it on the Pi), which stays the same whatever COM number Windows gives it.
- **Starting it:** from campy's panel ([From the panel](#from-the-panel)), or a window on the PC's desktop (Remote Desktop is fine), after campy says all cameras are ready, so the session is part of the recording's rig job. Keep that window open for the whole session: once the launcher ends, `bench/rig.py` sees the session program (which holds a COM port) as a foreign GPU and trigger user. Not over SSH: Windows' SSH server ends what an SSH session started when it disconnects. The session runs on in its own process, without a window, so closing the window doesn't end it. `status` and `stop` work over SSH.
- **Stopping early:** Ctrl-C in a PowerShell window reaches every program in it, including `bench/rig.py`, which then ends them all at once, before a launcher can stop the Pico or close campy's videos and SpikeGLX's files. The panel turns Ctrl-C off in its window and stops everything from its page instead ([From the panel](#from-the-panel)); the rig's PowerShell launcher reads Ctrl-C as a key while a session runs, asks for the `y` key, then stops the Pico, campy (through its `stopFile`) and SpikeGLX's run, in that order. (PowerShell's `Read-Host` turns Ctrl-C back into a signal, so the question reads a single key.)
- **Ending it:** campy stops right after the last bout's last frame, 10 s before the Pico lowers the gate (the post-baseline). A launcher that stops the Pico when campy ends cuts the last bout's SpikeGLX file short (it did once in the check below): let the Pico finish by itself, and stop it only after an error or Ctrl-C. (A PowerShell launcher that starts campy with `Start-Process -PassThru` must read the process's `Handle` straight away, or it never gets campy's exit code: a normal end then looks like a failure. That was the cause in the check below.)
- **Stopping it:** `stop` now asks through a file in the state folder (`stop.request`), which the session reads within a fraction of a second; that works on Windows, which has no `SIGUSR1`. On the Pi, `stop` still works the same.
- **Logs:** `~/.chronic_control` and `~/Documents/<date>/timestamps.txt` in the user's folder, as on the Pi; `--state-dir` and `--log-dir` put them elsewhere (next to the recording, say). The log's times are the PC's clock, the same clock as campy's frame times.
- **One folder per session:** on the rig, the launcher puts everything a session writes into one folder it makes, `D:\data\<date_time>\` (gate tests in `D:\data\gatetest\<date_time>\`; the earlier ones, made in `C:\data`, were moved there on 10 November 2026). campy records there (its `sessionPath` setting), the Pico's log goes to `pico/` (`--log-dir`), and SpikeGLX writes its run into `spikeglx/`. For SpikeGLX the launcher uses SpikeGLX's own remote-control interface (its command server, turned on once under Options > Command Server Settings, and the official SDK, [SpikeGLX-CPP-SDK](https://github.com/billkarsh/SpikeGLX-CPP-SDK)). If SpikeGLX isn't open, the launcher opens it (outside its own rig job, through a shortcut that starts it in its own folder, so it finds its saved settings) and does what SpikeGLX's Configure dialog does with Detect and Verify, through the same interface (`selectDevices`; a freshly opened SpikeGLX refuses to start a run until that is done once). It checks that SpikeGLX is idle and that its trigger is TTL (so it records only while the gate is high), sets SpikeGLX's data folder and run name (`chronic_<YYYY-MM-DD_HH-MM-SS>`, as the session folder), starts the run, and turns recording on. It then blinks the gate once and checks that SpikeGLX opened a file before campy starts, and stops SpikeGLX's run after the Pico's last gate. SpikeGLX reports itself running a few seconds before the OneBox's samples flow (6 s on this rig); a gate edge in that time opens no file, so the launcher waits until the OneBox stream holds more than the trigger margin plus 2 s.

### From the panel

Since pull request 283 a ChronicRecord session runs from campy's panel, a page in the rig's browser, in place of the PowerShell launcher (`chronic6day.ps1`, and `gatetest_pc.ps1` for gate tests), which stays on the rig until the panel has run real sessions. [Recording a session from the desktop](../docs/recording-a-session.md#on-the-chronicrecord-rig-the-pico-and-spikeglx) is the guide for whoever records; this is what it does, from [`chronic_session.yaml`](chronic_session.yaml) (on the rig in `C:\campy_data\configs`, beside `chronic_pico.yaml`, which it extends):

- **Its length** is picked on the page (`pico.lengths`: 6 days, a 15-minute gate test, an hour). campy's `recTimeInSec` is worked out from the Pico's plan for that length (camera pulses / 60), so they can't disagree; the launcher refused a mismatch instead.
- **Its bouts** can be changed on the page too (**Bout settings**: `INTERVAL`, `TOTAL_BASELINE`, `CAMERA_LEAD_IN`, `STANDARD_STIM_DURATION`, `EXTENDED_STIM_DURATION`, `EXTENDED_EVERY`, and the LEDs' `PULSE_WIDTH_C` and `PULSE_WIDTH_D`, `YELLOW_FIRST` (1 or 0) and `LED_GAP`, the widths and gap in ms there), over the config's `pico.set`, without changing the config; the next panel with the same config starts from the last values (`~/.campy/status/panel-bouts.json`) ([Recording a session from the desktop](../docs/recording-a-session.md#on-the-chronicrecord-rig-the-pico-and-spikeglx)). The page says what one bout does, with the frames each LED lights. Each length's plan, the disk check and `recTimeInSec` follow the settings; a plan the Pico can't run stops the start, including one of more than 2,048 bouts, which the Pico would refuse only at its `start`, after campy and SpikeGLX had started. The Pico starts with the settings the recording was started with. The recording's `logs\session-config.yaml` has them in `pico.set` (the line marked as the panel's), and `launcher.json` every setting the Pico ran with.
- **Checks before Start:** the Pico (by its USB serial number) answers and is idle; SpikeGLX answers, has no run, and its trigger is TTL (`trigMode=2`), or isn't open and will be opened; Windows Update paused until a day after the session's end (else a warning, and a button that runs `tools/windows/pause_windows_update.ps1`: Windows asks for administrator rights); USB selective suspend off (a warning); and the disk: what the chosen length needs against the free space where it records, keeping `stopAtFreeGB` free. SpikeGLX's part is the plan's open-gate seconds plus SpikeGLX's trigger margin (read from SpikeGLX, 1 s before and after each opening here) at `spikeglx.write_mb_s` (24 MB/s); the videos' part is the plan's camera seconds for every camera at `panel.video_mb_s` (0.6 MB/s a camera, an estimate on the high side). For the 8-hour rehearsal that is about 162 GB (SpikeGLX 151 GB, against 150.3 GB written on 6 November), for 6 days about 2,910 GB. A length that doesn't fit is red and stops the start; the page's Start then offers a confirm that names the shortfall, for a session whose data will be moved off during it. During the session the Disk free tile shows what the rest of the plan still needs, not hours at the rate so far (which, before the Pico's first pulse, was the cameras' rate of nothing).
- **One folder per session,** `D:\data\<date_time>\` (gate tests: `D:\data\gatetest\<date_time>\`): campy's videos and `logs\`, `spikeglx\` and `pico\`.
- **SpikeGLX** through its command server and the official SDK (`campy/spikeglx.py`, the launcher's `sglx_session.py` moved into campy): opened through a shortcut that starts it in its own folder, via Explorer, so it runs outside the rig's job; if its settings were "never validated" since it opened, the probe and OneBox are detected and verified (`(20,1,1)(20,obx)`); data folder `spikeglx\`, run name `chronic_<date_time>`; the run started; recording enabled once the OneBox's stream holds more than the trigger's margin plus 2 s of samples.
- **The gate check:** `wiring --channels A` for one blink, then `stop`; SpikeGLX must open a new file, or nothing else starts and SpikeGLX's run stops.
- **campy** with the live feed and one `campy-live-view` window (frames only), the panel's usual camera thumbnails and health.
- **The Pico's session** starts when **Start the Pico** is pressed (it asks first) once session.json says every camera is ready, or at a time given on the page: `chronic_control.py start --set TOTAL_DURATION=<length> --port E665485457699D30 --state-dir C:\campy_data\pico\state --log-dir <session>\pico`.
- **The end:** campy stops after the last frame; the panel waits for the Pico's last gate (up to `pico.last_gate_s`, 120 s), stops SpikeGLX's run 3 s later, and copies this session's part of `session.out` into `pico\`. **Stop** asks first: after this bout (the Pico's `stop`, then campy once the bout's gate has fallen, then SpikeGLX) or now (`stop --now`, then campy's stop file, then SpikeGLX). If campy fails on its own, the Pico and SpikeGLX are left running and the page offers to stop each.
- **The rig's lock:** the panel waits for the rig and holds it while it is open (`panel: {rig_lock: auto}`). Ctrl-C in its window does nothing then (it would reach `rig.py`, which ends the whole job before anything stops in order); **Close the panel** on the page ends it when nothing records. Closing its window by mistake leaves campy, the Pico and SpikeGLX running; the panel opened again shows them, with buttons to stop each.

**Gate test from the panel** (5 November 2026, 7:15 to 7:27 PM; this pull request's code, LED drivers on, no animal; SpikeGLX closed beforehand, so the panel opened it, and its settings were verified from the panel). The page ran every step in order:
- SpikeGLX's run, then the gate check: the blink opened a 2.5 s file.
- The cameras, then the Pico from **Start the Pico**.
- At the end, the Pico's last gate, then SpikeGLX stopped 12 s after campy's last frame, then remux and align.

What it recorded:
- **SpikeGLX:** files of 257.0, 27.0 and 27.0 s for the three bouts, as with the launcher.
- **The Pico:** `pulses=15600 stalls=0 bounces=0 self_check_us=-3`; it ended by itself.
- **campy:** 15,600 frames on each of the 4 cameras, none dropped or missed. campy-align put every camera on the trigger's timeline, with the two gaps between bouts as planned pauses.

**Both Stops from the page** (the same evening, 9:18 to 9:27 PM, the gate-test length):
- **Stop after this bout**, pressed in bout 1. The Pico finished bout 1 on time (14,400 pulses, its gate down at 255.0 s) and made no more bouts. campy then stopped with 14,400 frames on each camera, and SpikeGLX stopped too. SpikeGLX's files: the blink, then bout 1 whole, 257.0 s.
- **Stop now**, pressed 7 s into bout 1's camera. The Pico stopped at once (446 pulses). campy stopped with 446 frames on each camera, every one closed cleanly, and SpikeGLX stopped 3 s later. SpikeGLX's files: the blink, then the cut-short bout, 14.4 s.

Not checked yet: campy failing mid-session on the rig, and a multi-day session.

The desktop icon, made once in the recording environment: `campy-session shortcut C:\campy_data\configs\chronic_session.yaml --name "ChronicRecord session"`. `campy-session register-live`, run once in the campy-live environment, lets the view run there.

**Before a long session on the PC** (Windows; settings for the user to make, not the program):

- **Windows Update:** pause updates for longer than the session. Unpaused, Windows restarts the PC for updates by itself, outside its active hours. A restart ends campy and SpikeGLX whatever drives the Pico, and probably restarts a Pico powered by USB (below).
- **USB power saving:** set "USB selective suspend" to Disabled in the power plan, and in Device Manager untick "Allow the computer to turn off this device to save power" for the Pico's port and its USB hub. It shouldn't matter while a program holds the port open, but costs nothing.
- **Sleep:** never, on mains power.

**Through a PC restart: a 5 V supply of its own.** Powered only by USB, the Pico loses power while the PC restarts: it restarts and sets every output low, ending the session (measured on the Dell Pro Max Slim FCS1250, below). To keep it running, power it from a separate 5 V supply as well, as the Pico's datasheet describes ("Powering Pico"): the supply's + through a Schottky diode (a 1N5817 or 1N5819; the stripe towards the Pico) to **VSYS (pin 39)**, and its − to **GND (pin 38)**. The Pico's own diode on USB power and this one share the load, so either alone keeps it running and the USB link still works. Use a plug-in supply whose output floats (most phone chargers), so that it adds no connection to mains earth. Then, after a restart:

1. Start campy and SpikeGLX again, as for a new session (campy writes a new recording folder).
2. `python chronic_control.py attach`. It finds the session the board is running, from `~/.chronic_control/session.json`, and logs it from then on. If the board isn't running a session, it says so: the session ended, or the board lost power.

**Dry run on the PC** (5 November 2026, Windows 11, a spare Pico on a USB port with nothing wired to its pins, this version of `chronic_control.py` in the recording's Python environment):

- It found the Pico by itself (COM11), and by its USB serial number.
- A 30-minute camera-only session started from a PowerShell window in Remote Desktop kept running after the window was closed.
- **A restart** (Start, Restart; the PC was down about 30 s): the Pico came back idle with an empty schedule (`ok status idle edges=0 trains=0`), so it had lost power. `attach` said so: "the board is not running a session".
- `start`, `status` and `stop` in a 60 s camera-only session: `stop` ended it in about a second, on a whole pulse (316 pulses made, 316 counted by the Pico's own input, no stalls).

**On the rig, the Pico on the PC** (5 November 2026, the wired Pico moved from the Pi's USB to the PC's; LED controllers on, no animal; SpikeGLX gated by the Pico as in "Checked against the OneBox"):

- **Noise:** the same 45 s bout (gate 45 s, camera 30 s, both LEDs) from the Pi, then from the PC, then from the Pi again, a few minutes apart. The probe's noise (300 Hz to 6 kHz, median channel) and its 60 Hz hum were the same from the PC as from the Pi just after it (each channel within 1%). They were about a third lower in the first run from the Pi, before any cable moved, so something else changed between the first two runs; the second run from the Pi shows it wasn't the PC's USB. The OneBox's lines moved by a few millivolts at most: gate 3.17 V high and 0.000 V low, camera line 2.96 V and 0.18 V.
- **A 3-bout session from the PC** (`--set TOTAL_DURATION=900`) with campy recording the 4 cameras: 15,600 frames on each camera, both gaps logged as planned pauses; the Pico made 15,600 pulses with no stalls and no bounces. In SpikeGLX, bouts 1 and 2 matched the plan to the sample, as from the Pi: files of 257.0 and 27.0 s, the gate high 255.0009 or 25.0001 s, the first camera pulse 5.000 s after it rose, the gate falling 10.0137 s after the last, both LEDs 30.0 ms. Bout 3's file was 18.9 s: the launcher stopped the Pico when campy ended (see "Ending it" above). With that fixed, the same session again: all three bouts matched the plan to the sample (files of 257.0, 27.0 and 27.0 s), 15,600 frames on each camera, and the Pico ended the session by itself. Then once more with the launcher controlling SpikeGLX: the videos, SpikeGLX's run and the Pico's log all in the session's one folder, the gate check's 2.5 s file first (`t0`), then the three bouts, again matching the plan to the sample.

Not checked yet: the PC restarting with the Pico on its own supply, and a multi-day session from the PC.

## Sync pulses for the OneBox

**What it is for.** The Pico and the OneBox each keep their own clock. The gate, camera and LED lines say when the Pico switched them, but they repeat: every camera pulse looks like every other one, and every bout like the last. If something goes wrong (a stray camera frame, a missing pulse, a file that starts late), it can be hard to tell which pulse is which. The sync output (E, GP6) is a fifth line that never repeats: its pulses come at random gaps from a seed, so any few of them in a row say exactly where in the session they are. Wired to a free OneBox input, it ties every OneBox sample to the Pico's own time, so the two clocks can be compared at any point of the session, and every other line can be checked pulse by pulse against the Pico's plan.

**The pulses:** 10 ms high, and from one rise to the next a random whole number of milliseconds from 200 to 1000 (SplitMix64, from a seed; the formula is at the top of the sketch and in the session's sync file). They run from the session's time 0 to its end (the last gate's fall, or the time `stop` or `end` set), in and between the bouts; SpikeGLX keeps only what falls in its files, which is enough. Why these numbers:
- **10 ms** is 303 OneBox samples (one every 33 µs), so a pulse can't be missed or mistaken for noise. It is also unlike every other line (the camera's 3 ms, the LEDs' tens of ms with one flash a bout), so a cable on the wrong input shows straight away.
- **200 to 1000 ms**, whole milliseconds: 801 possible gaps, about 2 pulses a second. Each gap is measured on the OneBox to within two samples (about 70 µs; the clocks' 3.2 ppm difference adds 3 µs at most over a second), far less than the 1 ms between possible gaps, so each one is read without error. Four gaps in a row (about 2.4 s) have 801⁴, about 4 × 10¹¹, possible values; a 6-day session has about 864,000 runs of four, so nearly every one is unique (in a 6-day plan, one pair repeats; the matcher skips any run that isn't unique and places it by its neighbours). The shortest bout's file, 27 s, holds about 45 pulses.
- **On the Pico's state machine**, the same queue as the other four outputs: each sync edge is on its microsecond of the Pico's crystal, exactly like the others, and a sync edge due at the same microsecond as another output's goes out in the same instruction. The sync train adds about 4 queue entries a second to the camera's 120, so it doesn't change the risk of the queue running dry (`stalls=` still counts it) or the camera self check (`self_check_us`, `rises=`). The sketch's tests check that every camera and LED edge stays on its exact clock tick with the sync train on.

**Wiring.** GP6 (board pin 9) to the OneBox's XA1, the analog input between the gate (XA0) and the camera (XA2), which nothing else uses here, with its ground to the Pico's GND, and a 10 kΩ pull-down from GP6 to GND like the other outputs. SpikeGLX records XA1 as a voltage, and its digital copy as bit 1 of XD0, as it does for the gate (bit 0) and camera (bit 2); *expected from those two, not yet checked on the rig.* If XD0's bit 1 doesn't show it, the matcher reads XA1 itself (`--sync-xa XA1`, high above 1.5 V). To check the wiring: `python chronic_control.py wiring --channels E` blinks it five times, then a pause, until `stop`; watch XA1 in SpikeGLX's graphs, which show the inputs whether or not a file is being written.

**Turning it on.** It is off by default. In campy, set `picoSync: True` in the rig profile ([config reference](../docs/config-reference.md#trigger)); every Pico session from the panel then has it. Run by hand: `python chronic_control.py start --set SYNC=1`. The other settings (`SYNC_WIDTH_US`, `SYNC_MIN_GAP_MS`, `SYNC_MAX_GAP_MS`, `SYNC_SEED`) can be changed the same way, in `pico.set` or with `--set`; by default each session picks a new random seed. It needs `chronic_timer 3` on the Pico: a session with the sync train on a board still running version 2 doesn't start, and says to flash it again.

**What the session saves.** Beside `timestamps.txt` (in the recording's `pico\<date>\` folder from the panel), `sync_<date_time>.json`: the seed, the pulse settings, when the session's time 0 was by the PC's clock, its end, the board's final `done` line and all the session's settings. `timestamps.txt` gets a line with the seed, and the board's `done` line says how many sync pulses it made (`sync=`).

**Matching it with the OneBox:** [`sync_match.py`](sync_match.py) (needs numpy only; run it on a copy of the session, not on the rig while it records):

```bash
python sync_match.py D:\data\<date_time>
```

It finds the session's sync file and the OneBox files (one per gate opening, placed on the run's own sample count), works out every planned pulse from the seed, finds each file's place by its runs of four gaps, and fits one straight line, OneBox sample = a + b × board time, through every pulse. It prints:
- how many sync pulses matched, and any **missing** (planned inside a file, clear of its ends, but not on the OneBox) or **extra** (on the OneBox but not planned, or not 10 ms wide), with their times on both clocks;
- the **offset**: the OneBox's time at the board's time 0;
- the **drift**: how many OneBox samples one second of the Pico's crystal is, and how far that is from SpikeGLX's `obSampRate`, in ppm and ms a day (the rig measured about 3.2 ppm, 0.28 s a day, from the gate in earlier tests);
- the **residuals** from that line: a few tens of µs at most when everything is right (a sample is 33 µs);
- then the **camera, gate and LED lines**, each rise put on the board's clock through the same line and matched to the Pico's plan for the session's settings: how many, how far from the plan at most, and every rise the Pico didn't plan (a stray edge on that line, with its board time) or planned rise that isn't there. Files before the session (the gate check's blink) are left out.

It writes `sync_match.csv` (one row per sync pulse, seen, missing or extra: its file, OneBox sample and time, width, planned pulse and board time, residual) and `sync_match.json` (the fit, the counts, and the other lines' odd pulses) in the session folder. `--sync <file>` picks the sync file when the folder has more than one; `--no-lines` checks only the sync line.

*Not yet checked on the rig:* the wiring to XA1, SpikeGLX's XD0 bit 1, and a real session's numbers. Flashing the Pico with version 3 and a gate test are the next step.

## Commands the board takes

These are what `chronic_control.py` sends. Each answer comes at once: `ok ...` or `err ...`.

- `pin <ch> <pin>`, `clear`: set up the outputs and clear the old schedule.
- `train <ch> <start_us> <count> <rate_mHz> <width_us>`: one camera train.
- `edge <ch> <t_us> <0|1>`: one edge, in order of time.
- `sync <ch> <seed> <width_us> <min_gap_ms> <max_gap_ms>`: the sync train on channel 4 (GP6) for the session (version 3).
- `go`: start; session time 0 is 20 ms later.
- `end <t_us>`: the session ends at that time.
- `stop`: end now.
- `status`, `id`: what it's doing; which sketch it runs.

It reports by itself:

- `ev <t_us> <ch> <level> <late_us>`: an edge happened.
- `camon <train> <first_rise_us>`: a camera train started.
- `cam <train> <pulses> <first_rise_us> <last_fall_us>`: a camera train ended.
- `done ...`: the session is over and every output is low; `sync=` is the sync pulses it made.
- `ready ...`: it just powered up.

The full description is at the top of the sketch.

## Tests

`tests/test_chronic_timer.py` compiles the sketch for the computer and runs it on a simulated clock (`tests/arduino_sim`), the sync train included: its pulses are the seed's to the microsecond, and every camera and LED edge stays on its clock tick.

`tests/test_sync_match.py` runs `sync_match.py` on made-up OneBox files from a real plan, on a clock that runs fast: a clean session, a dropped and a glitched sync pulse with a stray camera pulse, files that start mid-session and mid-pulse, and 40 ppm of drift over an hour.

`tests/test_chronic_control.py` checks the program's plan against chronicControlv13.py's timeline. It also runs `start`, `status`, `stop`, `stop --now` and `attach` (after the session program is killed) end to end against the sketch running live on a pseudo-terminal. Where there is no pseudo-terminal (Windows), the same commands run against a stand-in board on a network socket, which answers as the sketch does but reports only the session's end. It also checks that the Pico is found by its USB IDs, never an Arduino, off the Pi.
