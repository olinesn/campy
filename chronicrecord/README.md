# ChronicRecord timer

The ChronicRecord rig's four TTL outputs, made by a Raspberry Pi Pico instead of the Raspberry Pi 3B's own GPIO pins:

| Output | What it drives | Pi 3B before (chronicControlv13.py) | Pico now |
|---|---|---|---|
| A | OneBox / SpikeGLX record gate (level) | GPIO 5 | GP2 (board pin 4) |
| B | FLIR camera trigger (Line3), 3 ms high | GPIO 12 | GP3 (board pin 5) |
| C | Blue LED controller TTL (465 nm) | GPIO 13 | GP4 (board pin 6) |
| D | Yellow LED controller TTL (560 nm) | GPIO 19 | GP5 (board pin 7) |
| | Every cable's ground | GND | GND (board pin 3 or 8) |

The Pi 3B still runs the experiment, started and stopped over SSH. It plans the whole session and loads it onto the Pico over USB. Then the Pico makes every edge from its own crystal clock. campy doesn't take part: on this setup the campy configs need `startArduino: False`, and the cameras just record the pulses on Line3. Start campy first, so that every camera is grabbing before the Pi's `start`.

- `chronic_timer/chronic_timer.ino` is the Pico's program (the same file also runs on an Arduino Nano ESP32).
- `chronic_control.py` is the Pi's program (needs only Python 3 and pyserial).

**Why a Pico:** its PIO hardware runs the outputs by itself from a queue of "these levels, for this many clock ticks". All four pins change in the same clock tick (about 7.5 ns at 133 MHz), and neither USB nor the rest of its program can move an edge unless the queue runs dry, which the Pico counts and reports (`stalls=`). The official Pico design has a 12 MHz crystal. An UNO R4 has no crystal in use (about ±1%), and a Pi's Linux can delay anything done in software.

**Checked on a real Pico (2 November 2026, on the Pi 3B's USB, nothing wired to its outputs).** At 60 Hz every session made exactly the planned pulses with no stalls. `self_check_us` was −1 to −3 µs in sessions of 10 s, 30 s and 120 s (7 runs); it doesn't grow with length, so the state machine keeps its schedule. One 60 s run read −17 µs, which didn't recur: the check is an interrupt, and one can be delayed.

What that doesn't show: the check shares the Pico's crystal, so it can't see how far the crystal itself is from true time (probably tens of parts per million, a few seconds a day, not measured). It only watches the camera pin's rises, not their width or the other three pins. The OneBox's recording of the edges is still the independent check.

**Long recordings:** the Pico holds 8192 edges and 2048 camera trains, 1365 bouts: 4.7 days at one bout per 300 s. A longer session is refused at `start` ("too many edges"). Camera-only sessions have no such limit (2^32 pulses).

## Why

On the Pi, everything was timed in software:

- **Camera clock.** `lgpio.tx_pwm` is software-timed PWM (lgpio's docs say so; the class name in the script says "hardware", but it isn't). lgpio's own thread switches the pin. Each frame's trigger is late by however long Linux takes to wake that thread: typically tens of microseconds, sometimes much more when the Pi is busy (SSH, the SD card, the status bar). *Expected, not measured.*
- **LED pulses were ~100 ms, not 30 ms.** `sleep_with_status()` sleeps in 0.1 s steps (so it can redraw the status bar). Every phase is rounded up to the next 100 ms, so a "30 ms" pulse lasted at least ~100 ms. The yellow LED came on 100+ ms after the blue one, not 30 ms. Each switch then also waited for a log line to be written to the SD card.
- **LEDs weren't locked to frames.** The LED schedule and the camera clock ran on separate timers. So which frame an LED came on in shifted from bout to bout, and an LED could switch partway through a frame's exposure (a half-lit frame).
- **The log's times are Python's guesses.** The `timestamps.txt` times are `datetime.now()` after each switch, and "Est. Frames" is computed, not counted.

SpikeGLX/OneBox records the real TTL edges, so the true LED widths and gaps can be checked in an existing recording.

On the Pico (or Nano):

- The camera trigger is a pulse train on its crystal. Pulse *k* rises at exactly `start + floor(k × 10⁶ / 60)` µs: 16666 or 16667 µs apart, exactly 60 Hz on average. It stops after an exact count (600 pulses in a standard bout, 14400 in an extended one).
- The LEDs are 30 ms. Blue switches on 1 ms before camera pulse 300 (5 s into the camera) and off 30 ms later; yellow follows on the same microsecond. With 200 µs exposures, blue lights frames 300–301 and yellow 302–303, each frame wholly lit or wholly dark. `python3 chronic_control.py plan` lists this for every bout, and warns if a setting would put an edge inside an exposure.
- Edges due together are written in one register write: blue off and yellow on, or any edge that lands on a camera pulse.
- On the Pico, each edge is at its scheduled microsecond exactly, on the state machine's clock (simulated tests, to the tick; on the real board, see above). On the Nano, the sketch busy-waits the last 300 µs with interrupts off: expected within ~1–2 µs, not measured.
- At the end of a session the Pico reports `rises=` (the camera pin's rises, counted by an interrupt) and `self_check_us=` (the time from first rise to last by its microsecond timer, minus the schedule's). Expect `rises=` equal to `pulses=` and a few microseconds at most.
- The board runs the loaded session by itself. If the Pi's program dies, or the USB link drops (as Arduinos on the lab's rigs often do), the edges don't move. The program reconnects and carries on logging. The board reports each edge with its board time and how late it was, and each camera train's exact pulse count. `timestamps.txt` records those, not Python's guesses.
- No `sudo`: the Pi needs no GPIO access, only its serial port.

## The protocol

The same session as chronicControlv13.py, with the settings at the top of `chronic_control.py`. Every bout, every `INTERVAL` (300 s), for `TOTAL_DURATION` (3600 s, 12 bouts):

1. Gate high.
2. 5 s pre-baseline.
3. Camera on at 60 Hz.
4. 5 s later, blue LED 30 ms, then yellow LED 30 ms.
5. Camera off after 10 s of camera (240 s in bouts 1, 7, ...).
6. 10 s post-baseline.
7. Gate low.

To change a setting for one session without editing the file:

```bash
python3 chronic_control.py start --set TOTAL_DURATION=1800 --set PULSE_WIDTH_C=0.050
```

## Running it

From any computer that can reach the Pi over SSH (put in the Pi's user name and address):

```bash
ssh <user>@<pi-address> python3 chronic_control.py start
```

```bash
ssh <user>@<pi-address> python3 chronic_control.py status
```

```bash
ssh <user>@<pi-address> python3 chronic_control.py stop
```

- `start` checks that the board answers, loads the session, starts it, and returns. The session keeps running in the background after SSH disconnects.
- `stop` ends the session after the current bout, as typing `cancel` did. The bout's gate still goes low on time.
- `stop --now` ends it at once. The camera finishes the pulse it is on, then every output goes low. With no session program running, `stop --now` tells the board itself to stop.
- `plan` prints the session's timeline without touching the board.
- `run` runs the session in the terminal. Ctrl-C there is `stop --now`.

The log is `~/Documents/<date>/timestamps.txt`, as before. The program's own output goes to `~/.chronic_control/session.out`. `status` reads `~/.chronic_control/status.json`, which the session rewrites as it runs. Like every JSON file campy writes, it is strict JSON: an infinite or NaN number would be the text `"Infinity"` or `"NaN"` ([Recording folders](../docs/recordings.md#infinity-and-nan-in-campys-json-files)).

## Camera-only sessions

No bouts, no LEDs, and the gate stays low: only the camera pulses, for as long as you say. Use it for calibration or plain recordings:

```bash
python3 chronic_control.py start --set CAMERA_ONLY=3600 --set CAMERA_FPS=30
```

`stop` ends one at once, on a whole pulse (a few ms to 20 ms later, what's already queued).

## Setting it up

1. **Flash the Pico.** Hold BOOTSEL while plugging it in; it appears as a drive named `RPI-RP2`. Copy `chronic_timer.ino.uf2` onto it (built with the [arduino-pico](https://github.com/earlephilhower/arduino-pico) core, board `rp2040:rp2040:rpipico`). It restarts as a serial port, and `id` should answer `ok chronic_timer 2`.
2. **Wire it** as in the table at the top, in place of the Pi's GPIO pins. Don't connect anything above 3.3 V to a Pico pin: they aren't 5 V tolerant.
3. **Pull-down resistors.** Fit a 10 kΩ resistor from each of GP2–GP5 to GND, so that nothing reads high while the Pico resets or is being flashed.
4. **Voltage.** The Pico's outputs are 3.3 V, the same as the Pi's GPIO that drives these inputs today. The Blackfly S Line3 counts 2.6 V or more as high, so 3.3 V works, with a small margin.
5. **Plug the Pico into the Pi by USB.** It shows up in `/dev/serial/by-id/`, which `chronic_control.py` finds by itself.
6. **On the PC,** record with `cameraTrigger: "Line3"`, `startArduino: False`, and `frameRate` matching `CAMERA_FPS`. For bouts:
   - `alertNoFramesSec` above the longest gap between bouts (about 300 s), or campy reports "the trigger has stopped" in every gap.
   - `recTimeInSec` equal to the session's total camera time (580 s by default), since campy stops after `recTimeInSec` x `frameRate` frames.
7. **Don't run chronicControlv13.py once the cables are on the Pico:** it would drive GPIO pins that nothing is wired to.

## Commands the board takes

These are what `chronic_control.py` sends. Each answer comes at once: `ok ...` or `err ...`.

- `pin <ch> <pin>`, `clear`: set up the outputs and clear the old schedule.
- `train <ch> <start_us> <count> <rate_mHz> <width_us>`: one camera train.
- `edge <ch> <t_us> <0|1>`: one edge, in order of time.
- `go`: start; session time 0 is 20 ms later.
- `end <t_us>`: the session ends at that time.
- `stop`: end now.
- `status`, `id`: what it's doing; which sketch it runs.

It reports by itself:

- `ev <t_us> <ch> <level> <late_us>`: an edge happened.
- `camon <train> <first_rise_us>`: a camera train started.
- `cam <train> <pulses> <first_rise_us> <last_fall_us>`: a camera train ended.
- `done ...`: the session is over and every output is low.
- `ready ...`: it just powered up.

The full description is at the top of the sketch.

## Tests

`tests/test_chronic_timer.py` compiles the sketch for the computer and runs it on a simulated clock (`tests/arduino_sim`).

`tests/test_chronic_control.py` checks the Pi program's plan against chronicControlv13.py's timeline. It also runs `start`, `status`, `stop` and `stop --now` end to end against the sketch running live on a pseudo-terminal.
