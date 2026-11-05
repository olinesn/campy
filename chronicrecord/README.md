# ChronicRecord timer

The ChronicRecord rig's four TTL outputs, made by a Raspberry Pi Pico instead of the Raspberry Pi 3B's own GPIO pins:

| Output | What it drives | Pi 3B before (chronicControlv13.py) | Pico now |
|---|---|---|---|
| A | OneBox / SpikeGLX record gate (level) | GPIO 5 | GP2 (board pin 4) |
| B | FLIR camera trigger (Line3), 3 ms high | GPIO 12 | GP3 (board pin 5), through a 50 Ω resistor |
| C | Blue LED controller TTL (465 nm) | GPIO 13 | GP4 (board pin 6) |
| D | Yellow LED controller TTL (560 nm) | GPIO 19 | GP5 (board pin 7) |
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
- The LEDs are 30 ms. Blue switches on 1 ms before camera pulse 300 (5 s into the camera) and off 30 ms later; yellow follows on the same microsecond. With 200 µs exposures, blue lights frames 300–301 and yellow 302–303, each frame wholly lit or wholly dark. `python3 chronic_control.py plan` lists this for every bout, and warns if a setting would put an edge inside an exposure.
- Edges due together are written in one register write: blue off and yellow on, or any edge that lands on a camera pulse.
- On the Pico, each edge is at its scheduled microsecond exactly, on the state machine's clock (simulated tests, to the tick; on the real board, see above). On the Nano, the sketch busy-waits the last 300 µs with interrupts off: expected within ~1–2 µs, not measured.
- At the end of a session the Pico reports `rises=` (the camera pin's rises, counted by an interrupt) and `self_check_us=` (the time from first rise to last by its microsecond timer, minus the schedule's). Expect `rises=` equal to `pulses=` and a few microseconds at most. `bounces=` counts rises too soon after the last one to be a pulse (a ringing cable); expect 0 with the 50 Ω resistor on GP3.
- The board runs the loaded session by itself. If the computer's program dies, or the USB link drops (as Arduinos on the lab's rigs often do), the edges don't move. The program reconnects and carries on logging. The board reports each edge with its board time and how late it was, and each camera train's exact pulse count. `timestamps.txt` records those, not Python's guesses.
- No `sudo` and no GPIO access: the computer needs only the Pico's serial port.

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

## Checking the wiring

```bash
python3 chronic_control.py wiring
```

Each output blinks on its own, its letter's number of times: A (gate) once, B (camera) twice, C (blue) three times, D (yellow) four times, half a second on and half off, with a pause after each. It goes round until `stop`. Watch for them in SpikeGLX, on a meter or with an LED probe.
- `--channels AB` blinks only those outputs.
- `--pause 60` waits a minute between rounds.

The gate starts a SpikeGLX file each time, if SpikeGLX is armed: about 23 MB a second with the probe recording, so mind the disk when leaving it running.

## Setting it up

1. **Flash the Pico.** Hold BOOTSEL while plugging it in; it appears as a drive named `RPI-RP2`. Copy `chronic_timer.ino.uf2` onto it (built with the [arduino-pico](https://github.com/earlephilhower/arduino-pico) core, board `rp2040:rp2040:rpipico`). It restarts as a serial port, and `id` should answer `ok chronic_timer 2`.
2. **Wire it** as in the table at the top (in place of the Pi 3B's GPIO pins, on this rig). Don't connect anything above 3.3 V to a Pico pin: they aren't 5 V tolerant.
3. **Pull-down resistors.** Fit a 10 kΩ resistor from each of GP2–GP5 to GND, so that nothing reads high while the Pico resets or is being flashed.
4. **A 50 Ω resistor in series with GP3** (the camera output). Without it, the camera cable rings as each pulse falls, and the Pico's own input counts a second rise on every pulse (`bounces=` equal to `pulses=`). The cameras didn't see that ring, but the resistor stops it: see "The 50 Ω resistor on the camera pin" near the top.
5. **Voltage.** The Pico's outputs are 3.3 V, the same as the Pi 3B's GPIO that drove these inputs before. The Blackfly S Line3 counts 2.6 V or more as high, so 3.3 V works, with a small margin. With the cameras and the 50 Ω resistor on it, the camera line reads 2.97 V high on the OneBox.
6. **Plug the Pico into the recording PC by USB.** On Windows it is a COM port ("USB Serial Device"), which `chronic_control.py` finds by the Pico's USB IDs; on Linux it shows up in `/dev/serial/by-id/`, which it finds by name.
7. **On the PC,** record with `cameraTrigger: "Line3"`, `startArduino: False`, and `frameRate` matching `CAMERA_FPS`. For bouts:
   - `alertNoFramesSec` above the longest gap between bouts (about 300 s), or campy reports "the trigger has stopped" in every gap.
   - `alertTriggerPauseSec` below the shortest gap between bouts (60 s, after a 240 s bout), or campy sends a "trigger paused" warning for every gap.
   - `alertExternalWaitSec` above how long campy waits before the Pico's `start` (default 10 minutes), or campy sends "No frame yet ... is it running?" while it waits.
   - `recTimeInSec` equal to the session's total camera time (580 s by default), since campy stops after `recTimeInSec` x `frameRate` frames.
   - [`chronic_pico.yaml`](chronic_pico.yaml) is the profile for multi-day sessions; see [Recording a long session](#recording-a-long-session).
8. **Don't run chronicControlv13.py once the cables are on the Pico:** it would drive GPIO pins that nothing is wired to.

## Recording a long session

[`chronic_pico.yaml`](chronic_pico.yaml) is the campy profile the rig uses for bout sessions of several days. It extends the rig's own profile (`extends: chronicrecord`: four cameras, Quick Sync H.265) and changes only what the Pico needs. Each line's comment says why.

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
| `stopFile` | a file | creating it stops the recording cleanly, from SSH or a script |

**`recTimeInSec` for another length.** campy counts camera time, not the wall clock. `python chronic_control.py plan --set TOTAL_DURATION=<seconds>` ends with the session's camera pulses; `recTimeInSec` is that number divided by 60. By hand, with the default settings: bouts = `TOTAL_DURATION` / 300, of which every 6th (bouts 1, 7, 13, ...) is 240 s of camera and the rest 10 s.

| Session | `TOTAL_DURATION` | Bouts (long) | Camera pulses | `recTimeInSec` |
|---|---|---|---|---|
| 1 hour (the default) | 3600 | 12 (2) | 34,800 | 580 |
| 4 days | 345600 | 1152 (192) | 3,340,800 | 55680 |
| 6 days | 518400 | 1728 (288) | 5,011,200 | 83520 |

**Order.** Start campy first and wait for "All 4 cameras are ready.", then the Pico: `python chronic_control.py start --set TOTAL_DURATION=518400` on the PC, or from the launcher that started campy (see [On the recording PC](#on-the-recording-pc)). A camera that isn't grabbing when the first pulse comes misses frames the others have. Start the Pi within an hour of campy, or campy warns that it has had no frame yet (`alertExternalWaitSec: 3600`; 10 minutes with a campy before pull request 267, which refuses that line).

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

**Disk: a 6-day session does not fit on the recording PC today.** The videos are the small part: about 140 to 200 GB for the 6-day plan at 60 fps (worked out from 30 fps recordings of mice; not yet measured at 60 fps with animals). SpikeGLX writes to the same C: drive while the gate is high, plus its trigger margin (1 s before and after each bout on this rig), at about 24 MB/s (23.1 MB/s for the Neuropixels probe, 0.85 MB/s for the OneBox, both measured on 4 November): about 2.7 TB for the 6-day plan's 109,440 s of gate plus 3,456 s of margin. In all about 2.9 TB, roughly 475 GB a day, against about 500 GB free on the PC's only disk (1 TB). Before a multi-day session, add a data disk (or move data off during the session); see [Recording many days](../docs/hardware.md#recording-many-days) in the hardware notes. campy's launch check only counts its own videos (about 430 GB here, from the encoder sweep's synthetic frames) and warns above 80% of the free space; it doesn't know about SpikeGLX. campy stops its recording cleanly below 2 GB free, but SpikeGLX would fill the disk first. The profile's `alertDiskFreeGB: 500` sends a disk warning to Slack once less than about a day's room is left; on today's disk (about 490 GB free) that is from the start of every session, test runs included, until a data disk is added.

## On the recording PC

Since 5 November 2026 the Pico is on one of the recording PC's USB ports, and the PC starts and stops sessions; the Raspberry Pi 3B isn't used. `chronic_control.py` runs there as it did on the Pi. Checked on the rig on 5 November (below); a multi-day session hasn't run this way yet. What differs from the Pi:

- **Python:** the recording's own environment (it has pyserial), with `python` for `python3`.
- **Finding the Pico:** on Windows (and on a Linux PC without `/dev/serial/by-id`), the one Pico among the serial ports, by its USB IDs. It never takes an Arduino there, since campy's trigger Arduino may be on the same PC. With two Picos, or to be sure, name it: `--port COM7`, or by its USB serial number, `--port E665485457699D30` (as `/dev/serial/by-id` shows it on the Pi), which stays the same whatever COM number Windows gives it.
- **Starting it:** from a window on the PC's desktop (Remote Desktop is fine), best from the recording's launcher after campy says all cameras are ready, so the session is part of the recording's rig job. Keep that window open for the whole session: once the launcher ends, `bench/rig.py` sees the session program (which holds a COM port) as a foreign GPU and trigger user. Not over SSH: Windows' SSH server ends what an SSH session started when it disconnects. The session runs on in its own process, without a window, so closing the window doesn't end it. `status` and `stop` work over SSH.
- **Ending it:** campy stops right after the last bout's last frame, 10 s before the Pico lowers the gate (the post-baseline). A launcher that stops the Pico when campy ends cuts the last bout's SpikeGLX file short (it did once in the check below): let the Pico finish by itself, and stop it only after an error or Ctrl-C. (A PowerShell launcher that starts campy with `Start-Process -PassThru` must read the process's `Handle` straight away, or it never gets campy's exit code: a normal end then looks like a failure. That was the cause in the check below.)
- **Stopping it:** `stop` now asks through a file in the state folder (`stop.request`), which the session reads within a fraction of a second; that works on Windows, which has no `SIGUSR1`. On the Pi, `stop` still works the same.
- **Logs:** `~/.chronic_control` and `~/Documents/<date>/timestamps.txt` in the user's folder, as on the Pi; `--state-dir` and `--log-dir` put them elsewhere (next to the recording, say). The log's times are the PC's clock, the same clock as campy's frame times.
- **One folder per session:** on the rig, the launcher puts everything a session writes into one folder it makes, `C:\data\<date_time>\` (gate tests in `C:\data\gatetest\<date_time>\`). campy records there (its `sessionPath` setting), the Pico's log goes to `pico/` (`--log-dir`), and SpikeGLX writes its run into `spikeglx/`. For SpikeGLX the launcher uses SpikeGLX's own remote-control interface (its command server, turned on once under Options > Command Server Settings, and the official SDK, [SpikeGLX-CPP-SDK](https://github.com/billkarsh/SpikeGLX-CPP-SDK)). It checks that SpikeGLX is idle and that its trigger is TTL (so it records only while the gate is high), sets SpikeGLX's data folder and run name, starts the run, and turns recording on. It then blinks the gate once and checks that SpikeGLX opened a file before campy starts, and stops SpikeGLX's run after the Pico's last gate. SpikeGLX reports itself running a few seconds before the OneBox's samples flow (6 s on this rig); a gate edge in that time opens no file, so the launcher waits until the OneBox stream holds more than the trigger margin plus 2 s.

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

`tests/test_chronic_control.py` checks the program's plan against chronicControlv13.py's timeline. It also runs `start`, `status`, `stop`, `stop --now` and `attach` (after the session program is killed) end to end against the sketch running live on a pseudo-terminal. Where there is no pseudo-terminal (Windows), the same commands run against a stand-in board on a network socket, which answers as the sketch does but reports only the session's end. It also checks that the Pico is found by its USB IDs, never an Arduino, off the Pi.
