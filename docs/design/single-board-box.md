# One Nano ESP32 for the cameras and the box: design

> **Status: agreed on 2026-11-13; being built.** Piece 1, the sketch (version 4), is pull request 459, tested on the
> simulated board and compiled for the Nano ESP32; piece 2, `campy-box` driving it, is pull request 460; piece 3,
> `triggerController: box`, is pull request NNN. Nothing has run on a real board yet: piece 4 is to come, run by run
> with the user's OK. It adds a mode beside the two-board setup, which stays: each rig picks one.

Today a rig with an [operant box](../operant-boxes.md) has two Arduino Nano ESP32s. One runs `trigger.ino` and pulses
the cameras; campy-acquire drives it. The other runs `campy_outputs` for the box; `campy-box` (or `campy-rules`) drives
it, and a wire takes the camera pulse into its D10 so it can count frames. This page is how one Nano ESP32 can do both.

## Why it needs a design: one USB port, one owner

- **A serial port belongs to one program.** On Windows a second program can't open it at all. On Linux and macOS it
  can, but both then read the same stream of bytes and each steals the other's lines. campy-acquire holds the trigger
  board's port for the whole recording (it checks the board before any camera opens, sends the frame rate once every
  camera is ready, and the stop at the end); `campy-box` talks to its board all the time (a heartbeat every 50 ms,
  every sensor edge, every frame's report).
- **The Nano ESP32 can't be two serial ports.** Arduino's ESP32 core, version 2.0.18 (the one the rigs and CI use), has
  room in its USB code for two serial ports, but its USB library is built with one (`CFG_TUD_CDC` is 1). A second one
  would need a rebuilt core.
- **The two sketches speak differently.** `trigger.ino` reads comma-separated numbers; the box sketch reads commands,
  one per line.

So one program must own the board: `campy-box`. campy-acquire then treats the trigger as external, as it already does
for the ChronicRecord rig's Raspberry Pi Pico (`triggerController: pico`): it opens no trigger port, the cameras wait
for pulses, and it stops after `recTimeInSec × frameRate` frames.

## The design

**The camera pulses come from hardware.** The ESP32-S3 has two motor-control PWM units (MCPWM). One timer, counting at
1 MHz (the 160 MHz clock divided by 16, then by 10), with a period of P µs and one comparator at P/2, makes the pulses:
low for the first half of each period, high for the second, as `trigger.ino` does. Its output is routed to every
camera pin.

- **The same timing as `trigger.ino`.** P is `floor(1e6 / int(fps))` µs, as `trigger.ino` rounds it
  (`syncmatch.TriggerPeriodUs`), and frame k (from 0) rises at k·P + P/2 after time 0. So `campy-syncmatch`,
  `sync.json` and `campy-align` work unchanged.
- **No CPU in the edges.** Once started, the timer runs by itself: servo moves, sensor edges, USB traffic and the box
  engine's work can't move a camera edge, and a stalled sketch doesn't stop the pulses.
- **The period is at most 65,535 µs** (the timer's 16 bits): 16 fps or more. A slower rate is refused, with that said.
- **The frame count is exact by design.** The board made the pulse, so it knows when frame k rose: time 0 plus
  k·P + P/2, on the board's clock (both clocks come from the same crystal). No wire, no missed edge. It still says
  `t <k> <time>` for each frame, so `campy-box` and `campy-rules` read it as before, and trains locked to the frames
  (photometry) work as before.
- **An exact number of pulses.** An interrupt at each rise counts them. At the planned count it tells the timer to stop
  when it next reaches zero, so the last pulse ends whole and the pin stays low. The count is recTimeInSec × frameRate,
  as campy-acquire expects. A stop asked for mid-recording ends at a period boundary the same way, worked out so that
  no pulse is cut and none is added (below).

**Two watchdogs.** The box's watchdog (250 ms of silence from the computer: outputs low, links disarmed, servos to
safe) never touches the camera pulses: if `campy-box` crashes, the pulses go on to the planned count and the recording
ends normally. A second, long one (`camwatch`, off unless set) stops the pulses after that long a silence, for
recordings with no planned count.

**The sync output for other recorders** (ephys, photometry) is `trigger.ino`'s: the same seeded random train
(SplitMix64 gaps, from the same time 0), made by the board's timed-pulse engine, exact to the microsecond.
`trigger.ino` holds a sync edge that would fall within 50 µs before a camera edge, because one loop makes both; here the
camera edges are hardware, so nothing needs holding, and `sync.json` says so (`hold_guard_us: 0`, `hold_after_us: 0`,
which `campy-syncmatch` already reads as no hold).

**Start and stop.**

- **Start**: once every camera is ready. `campy-box` reads that from the recording's `session.json` (each camera's
  `ready`), as the panel does for the Pico, and starts the pulses. Nothing to press.
- **Stop**: the planned count; or a stop from the panel or Ctrl-C (`campy-box` asks the board to stop at the next
  period boundary, campy-acquire's stop file stops the cameras); or the recording's end.

**Config.** A new `triggerController: box`, refused with `startArduino: True` or a `pico:` section, and needing a
`box:` section. campy-acquire treats it as external (no port). The panel's trigger check gets a box branch.

## What stays a risk

A reset of the board (a power dip from a stalling servo or a valve's kick, a crash of the sketch) stops the camera
pulses; with two boards it wouldn't. To keep it rare:

- servos on their own 5–6 V supply, never the board's;
- valves and pumps through MOSFET or relay driver boards with flyback diodes, on their own supply;
- grounds joined at one point.

If it happens, `campy-box` hears the board start again (`ready`) and says so (an alert), and campy-acquire's watch says
the frames stopped. The pulses don't start again on their own mid-recording: frame numbers would no longer match.

## Commands (sketch version 4)

Added to the box sketch's commands (one per line; every earlier command unchanged):

| Command | What it does |
|---|---|
| `cam <period_us> <pin> [pin …]` | the camera generator: P (100–65,535 µs) and up to 4 camera pins; the board's frames now come from it, not from D10. `cam 0`: none |
| `camsync <pin> <seed> <width_us> <min_ms> <max_ms>` | the sync output, as `trigger.ino`'s. `camsync -1`: none |
| `camgo <n>` | start now: n pulses (0: until stopped). Answers `ok camgo <n> <t0_us> <period_us>`; time 0 is t0 |
| `camstop` | stop at the next period boundary: answers `ok camstop <frames>`, the number of pulses there will have been |
| `camwatch <ms>` | stop the pulses after that long without a word from the computer (0: never, the default) |
| `camstatus` | `ok camstatus running=… frames=… planned=… hw=… t0=… period=…` (hw: the rises the timer's interrupt counted) |

What it says on its own: `t <k> <t_us>` per frame, as before; `camend <frames> <t_us>` when the pulses have ended;
`camwd <t_us>` when `camwatch` stopped them; `camerr …` if the timer's count ever differs from the frames reported.

**Stopping without cutting or adding a pulse.** Rises come at P/2 in each period, the timer's stop at its zero (the
period's end). A stop asked for in the first half of period i keeps that period's pulse (the interrupt at its rise
stops the timer); in the second half, the pulse has risen and the timer is told to stop at the coming zero, unless
that zero is under 50 µs away, when one more period is let through (the next rise's interrupt stops it). Interrupts
are off while this is decided, so the rise's interrupt can't come between the decision and the order.

## Pieces

1. **The sketch (version 4)**: the camera generator, the exact count and stop, the sync train, `camwatch`; on the
   simulated board, a stand-in for the timer with the same behaviour. Tests: rises at exactly k·P + P/2 from t0, the
   exact count, stops mid-period in both halves and next to a boundary, the sync edges where `campy-syncmatch` works
   them out, frame-locked trains and sensor stamps on the generated frames, and box work (servos moving, inputs firing
   fast, serial traffic) moving no camera edge.
2. **`campy-box`**: starts the pulses when every camera is ready, stops them, writes `sync.json` (and the start, for
   `campy-align`), keeps the two watchdogs, says a board restart.
3. **`triggerController: box`**: the config checks, the panel's trigger check, `docs/sync.md` and
   `docs/operant-boxes.md`.
4. **On a real board**, only with the user's OK for each run: one Nano ESP32 triggering FLIR cameras, the frame
   intervals from the cameras' own timestamps with the box idle and busy (as the sync output was checked,
   [Triggering and synchronization](../sync.md)), and the timer's interrupt and start on the rigs' core (2.0.18).
