# ChronicRecord timer

The ChronicRecord rig's four TTL outputs, made by an Arduino Nano ESP32 instead of the Raspberry Pi 3B:

| Output | What it drives | Pi 3B before (chronicControlv13.py) | Nano ESP32 now |
|---|---|---|---|
| A | OneBox / SpikeGLX record gate (level) | GPIO 5 | D2 |
| B | FLIR camera trigger (Line3), 60 Hz, 3 ms high | GPIO 12 | D3 |
| C | Blue LED controller TTL (465 nm) | GPIO 13 | D4 |
| D | Yellow LED controller TTL (560 nm) | GPIO 19 | D5 |

The Pi still runs the experiment, started and stopped over SSH. It plans the whole session and loads it onto the Nano over USB. Then the Nano makes every edge from its own crystal clock. campy doesn't take part: the ChronicRecord campy configs keep `startArduino: False`, and the cameras just record the pulses on Line3.

- `chronic_timer/chronic_timer.ino` is the Nano's sketch.
- `chronic_control.py` is the Pi's program (needs only Python 3 and pyserial).

## Why

On the Pi, everything was timed in software:

- **Camera clock.** `lgpio.tx_pwm` is software-timed PWM (lgpio's docs say so; the class name in the script says "hardware", but it isn't). lgpio's own thread switches the pin. Each frame's trigger is late by however long Linux takes to wake that thread: typically tens of microseconds, sometimes much more when the Pi is busy (SSH, the SD card, the status bar). *Expected, not measured.*
- **LED pulses were ~100 ms, not 30 ms.** `sleep_with_status()` sleeps in 0.1 s steps (so it can redraw the status bar). Every phase is rounded up to the next 100 ms, so a "30 ms" pulse lasted at least ~100 ms. The yellow LED came on 100+ ms after the blue one, not 30 ms. Each switch then also waited for a log line to be written to the SD card.
- **LEDs weren't locked to frames.** The LED schedule and the camera clock ran on separate timers. So which frame an LED came on in shifted from bout to bout, and an LED could switch partway through a frame's exposure (a half-lit frame).
- **The log's times are Python's guesses.** The `timestamps.txt` times are `datetime.now()` after each switch, and "Est. Frames" is computed, not counted.

SpikeGLX/OneBox records the real TTL edges, so the true LED widths and gaps can be checked in an existing recording.

On the Nano:

- The camera trigger is a pulse train on its crystal (±10–20 ppm). Pulse *k* rises at exactly `start + floor(k × 10⁶ / 60)` µs: 16666 or 16667 µs apart, exactly 60 Hz on average. It stops after an exact count (600 pulses in a standard bout, 14400 in an extended one).
- The LEDs are 30 ms. Blue switches on 1 ms before camera pulse 300 (5 s into the camera) and off 30 ms later; yellow follows on the same microsecond. With 200 µs exposures, blue lights frames 300–301 and yellow 302–303, each frame wholly lit or wholly dark. `python3 chronic_control.py plan` lists this for every bout, and warns if a setting would put an edge inside an exposure.
- Edges due together are written in one register write: blue off and yellow on, or any edge that lands on a camera pulse.
- Expected on the board: within ~1–2 µs of schedule. The sketch busy-waits the last 300 µs before each change with interrupts off; the clock reads in whole microseconds. *Not measured on the rig.* The tests show the schedule logic is exact on a simulated clock: every edge within 2 µs, and 0 µs late in the simulation.
- The Nano runs the loaded session by itself. If the Pi's program dies, or the USB link drops (as Arduinos on the lab's rigs often do), the edges don't move. The program reconnects and carries on logging. The Nano reports each edge with its board time and how late it was, and each camera train's exact pulse count. `timestamps.txt` records those, not Python's guesses.
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

From any computer:

```bash
ssh falknerlab@10.241.0.176 python3 chronic_control.py start
```

```bash
ssh falknerlab@10.241.0.176 python3 chronic_control.py status
```

```bash
ssh falknerlab@10.241.0.176 python3 chronic_control.py stop
```

- `start` checks that the Nano answers, loads the session, starts it, and returns. The session keeps running in the background after SSH disconnects.
- `stop` ends the session after the current bout, as typing `cancel` did. The bout's gate still goes low on time.
- `stop --now` ends it at once. The camera finishes the pulse it is on, then every output goes low. With no session program running, `stop --now` tells the Nano itself to stop.
- `plan` prints the session's timeline without touching the board.
- `run` runs the session in the terminal. Ctrl-C there is `stop --now`.

The log is `~/Documents/<date>/timestamps.txt`, as before. The program's own output goes to `~/.chronic_control/session.out`.

## Setting it up (not done yet)

1. **Flash the Nano.** In the Arduino IDE, choose board "Arduino Nano ESP32", open `chronic_timer/chronic_timer.ino`, and upload. In the Serial Monitor (newline line ending), `id` should answer `ok chronic_timer 1`.
2. **Wire it.**
   - Connect D2, D3, D4 and D5 on the screw-terminal shield to the BNC breakout's rows for outputs A–D, in place of the Pi's GPIO 5, 12, 13 and 19.
   - Connect the Nano's GND to the breakout's ground.
   - Fit a 10 kΩ resistor from each of the four outputs to GND. While the Nano resets or is being flashed, its pins float. A floating gate or LED input could otherwise read high and start a SpikeGLX file or flash an LED.
3. **Voltage.** The Nano's outputs are 3.3 V, the same as the Pi's GPIO that drives these inputs today. The Blackfly S Line3 counts 2.6 V or more as high, so 3.3 V works, with a small margin. For more margin over long BNC cables, a 74AHCT125 buffer powered from 5 V would turn the 3.3 V outputs into 5 V ones. That's optional: today's 3.3 V works on this rig.
4. **Plug the Nano into the Pi by USB.** It shows up as `/dev/serial/by-id/usb-Arduino_Nano_ESP32_...`, which `chronic_control.py` finds by itself. The ChronicRecord PC's COM9 is a Nano ESP32 used by `campy_config_chronicrecord_v02c_arduinoTrigger.yaml`. If that is the same board, that config won't find it once it's on the Pi.
5. **Set up the Pi.**
   - `sudo apt install python3-serial`.
   - Copy `chronic_control.py` to `~`.
   - Check that the user is in the `dialout` group (`groups`).
   - Run `python3 chronic_control.py plan` and check the timeline.

## Commands the Nano takes

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
