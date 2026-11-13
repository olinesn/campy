# Closed-loop outputs with campy-rules

`campy-rules` lets an experiment act on the animals' live poses. Examples: switch on a laser while mouse 2 is in the nest, give a reward pulse when two mice touch noses, or send event marks to the recording system. It also makes photometry excitation pulses, locked to the camera frames, for the whole session or only while a rule holds.

It works on top of [live tracking](live-tracking.md):

- **`campy-live`** puts each set's poses in shared memory, the *results feed*.
- **`campy-rules`** reads that feed and checks each set against your rules. It is a program of its own: `campy-live` never waits for it, so it can't slow tracking down.
- **An Arduino Nano ESP32**, the *outputs board*, makes the TTL pulses on up to 8 outputs. It runs the sketch `campy/outputs_board/campy_outputs`, which can also read an [operant box](operant-boxes.md)'s sensors and move its servos. It watches the camera trigger, so it knows exactly when each frame was taken.

How it divides the work: `campy-rules` decides *whether* an output should be on, and the board decides *when* each edge happens, on its own crystal clock. A 3 ms photometry pulse can't be timed precisely over USB, where a message can land anywhere in a 1 ms window. So the board makes every pulse itself, to the microsecond, and `campy-rules` only starts and stops it.

## The board

### What you need

- An Arduino Nano ESP32, on its own USB port. It is separate from the camera trigger's board, which `campy-acquire` keeps busy for the whole recording.
- Wires from its outputs to the devices: Doric LED drivers' TTL inputs, a laser driver, a pump, the recording system's digital inputs.
- One jumper from the camera trigger's output pin to the board's trigger input, plus a shared ground.
- Optional: an LED behind a 330 Ω resistor on a spare output, where all the cameras see it, to measure the latency.

### Wiring (the sketch's defaults)

| Board pin | What |
|---|---|
| D2 to D9 | Outputs, channels 0 to 7 |
| D10 | Camera trigger in: the same TTL that goes to the cameras |
| GND | Shared with the trigger controller and every device |

- **3.3 V logic.** The ESP32's pins are not 5 V tolerant. A 5 V trigger needs a divider (for example 10 kΩ over 20 kΩ) before D10. The outputs are 3.3 V, which most TTL inputs take, Doric's LED drivers included. Check each device's datasheet.
- **Pull-downs.** Put a 10 kΩ resistor from each output to ground. The outputs are low from power-up, but during a reset or an upload a pin floats for a moment. A pull-down keeps a laser driver from reading that as on.
- `campy-rules` sets the pins again from the rules file (`pin:` per output, `trigger_pin:` for the trigger), so other pins work too.

### Uploading the sketch

Use the Arduino IDE (board: Arduino Nano ESP32) or arduino-cli:
```
arduino-cli compile --fqbn arduino:esp32:nano_nora campy/outputs_board/campy_outputs
arduino-cli upload --fqbn arduino:esp32:nano_nora -p <port> campy/outputs_board/campy_outputs
```
The port is `/dev/ttyACM1` or similar on Linux, `COM5` or similar on Windows. The board answers `ok campy_outputs 3` to `id` at 115200 baud.

Upload it again after each campy update that changes the sketch. `campy-rules` checks the version when it starts and refuses a board that runs another one, saying which it found. Version 2 came with pull request 304: a board that still answers `ok campy_outputs 1` restarts its frame count after a pause in the trigger (see *Order matters* below), so `campy-rules` won't start with it. Pull request 430 changed the sketch but not its version, since nothing it says changed: a board flashed before it drops any line that doesn't fit its 64-byte USB buffer at once (`status` every time, and now and then a line when several come together). Upload it again to get every line. Version 3 (pull request 449) added inputs, servos and links for [operant boxes](operant-boxes.md); everything `campy-rules` uses works as in version 2, but `campy-rules` refuses a version 2 board, so upload the sketch again.

## The rules file

A YAML file with four parts. `python -m pydoc campy.rules` describes every key.

```yaml
board:
  port: /dev/ttyACM1        # or COM5; --port overrides it
  trigger_pin: 10
  bound_ms: 40              # never act on a set taken longer ago than this
  watchdog_ms: 250          # the board switches everything off if campy-rules goes quiet this long

outputs:
  blue:   {channel: 0, pin: 2, group: 1, run: session,       # 465 nm, odd frames
           train: {every: 2, phase: 1, offset_us: 0, width_us: 3000}}
  violet: {channel: 1, pin: 3, group: 1, run: session,       # 405 nm, even frames
           train: {every: 2, phase: 0, offset_us: 0, width_us: 3000}}
  laser:  {channel: 2, pin: 4, max_on_s: 10}
  reward: {channel: 3, pin: 5, pulse_ms: 50}
  mark:   {channel: 4, pin: 6, pulse_ms: 5}

regions:
  nest:  {box: [[0, 0, 0], [120, 80, 60]]}                  # 3D, in the calibration's mm
  port:  {sphere: {center: [200, 40, 20], radius: 25}}
  arena: {cylinder: {center: [0, 0], radius: 200, z: [-10, 100]}}
  door:  {camera: cam1, polygon: [[10, 10], [200, 10], [200, 90], [10, 90]]}   # one camera, pixels

rules:
  - name: mouse2_in_nest
    when: {inside: {region: nest, animal: 2, node: nose, margin: 10}}
    enter_ms: 50            # true for 50 ms before it switches on
    exit_ms: 100            # false for 100 ms before it switches off
    do: {output: laser}     # on while the rule holds

  - name: nose_to_nose
    when: {near: {a: {animal: any, node: nose}, b: {animal: other, node: nose}, within: 15, leave: 25}}
    refractory_s: 5
    do: {output: reward, action: pulse}
```

### Outputs

- An output is a channel (0 to 7) on a pin.
- `train:` makes the board pulse it on the camera frames: a pulse of `width_us`, `offset_us` after the trigger of every frame k where (k − `phase`) is a multiple of `every`.
- `run: session` runs the train from start to end, whatever the poses. Without it, rules start and stop the train.
- Outputs in the same `group:` are never high together. The board skips a pulse that would overlap another output of its group, and logs it.
- `max_on_s:` the board switches a level off after that long, whatever `campy-rules` says.

### Photometry excitation

The example's `blue` and `violet` outputs are two LED drivers on alternate frames, 3 ms each, from each frame's trigger. On the simulated clock, every pulse started on its trigger's microsecond and lasted 3,000 µs. The two LEDs were never on together.

To light only part of the exposure, use `offset_us` to place the pulse within the frame. A train that is stopped always finishes the pulse it is making. A rule that runs a train (`action: train`) starts it at the first frame after the decision. The time that takes is the rule's latency, described below.

### Conditions

- **`inside`**: an animal's body part in a region. `margin` makes it sticky: once inside, the animal must go that far out before the rule lets go.
  - 3D regions are `box`, `sphere` and `cylinder` (axis along z), in the calibration's units (mm).
  - A per-camera `polygon` is in pixels. It works without a calibration too, then on the detections, which have no identities.
- **`near`**: two body parts within `within` mm (pixels with `camera:`), until they are more than `leave` apart.
  - `a` and `b` each name an animal and a node. `b`'s animal can be `other` (any but a's), `same` (a's own, for example nose to tail) or an identity.
- **`test: {every: N}`**: true on every Nth frame, for measuring latency without any animal.
- **`input: <name>`**: an [operant box](operant-boxes.md)'s sensor on the same board, named in an `inputs:` section (as in a `campy-box` task: `poke: {input: 0, pin: A0, pull: up}`). True for a set while the input is active, or if it went active since the set before, so a lick shorter than a frame still counts once. With a pose under `all:`, for example a reward only when mouse 2 is the one at the port:

  ```yaml
  inputs:
    poke: {input: 0, pin: A0, pull: up}
  rules:
    - name: mouse2_pokes
      when: {all: [{input: poke}, {inside: {region: port, animal: 2, node: nose}}]}
      do: {output: reward, action: pulse}
  ```

  The board says each edge as it happens, with its frame; the rules see it with the next set, so the reward follows the poke by about a set's latency (the frame's trigger to its poses), not within microseconds as a `campy-box` link would. Without a board (`--dry-run`, `--replay`) an input is unknown, so it never switches anything on. The rules file's inputs take pins no output and not the trigger has: a channel the rules don't use is unpinned, so its default pin (D2 to D9) is free.
- **`all`, `any`, `not`** combine conditions.
- **`animal:`** an identity (0 to animals − 1, from `campy-live --calibration`), a list of them, or `any`.
- **`node:`** a name, an index, a list (their mean), or `all`.

**Missing data.** An animal that fewer than 2 cameras see has no 3D point. Neither has one whose 3D lands outside the cage, when a cage is drawn ([The cage](live-tracking.md#the-cage-3d-outside-it-is-not-found---cage)): it counts as not found, in 3D and in each camera (its `members` are -1), so no rule acts on a place where no animal can be. Its rule then keeps its last value for `missing_ms` (default 100), then counts as false. So missing data can switch an output off, never on. A camera that `campy-live` left out of a set (its feed ended or stalled: [A camera that stops](live-tracking.md#live-pose-estimation-livefeed-campy-live)) has no detections in it, so its `members` are -1 and a condition on that camera's 2D is missing data too; 3D conditions go on from the other cameras.

**Identities** come from `campy-live`'s tracker, which can swap two animals. Each command's log line has the frame, so a swap can be checked afterwards in LUC3D.

### Actions

| `action` | What the output does |
|---|---|
| `level` | On while the rule holds (the default for an output without `train` or `pulse_ms`) |
| `pulse` | One pulse of `pulse_ms` each time the rule starts holding |
| `train` | Its train runs while the rule holds |
| `burst` | `pulses: N` of its train each time the rule starts holding |

- Several rules on one output: its level is on while any of its `level` rules holds, and its train runs while any of its `train` rules holds. Each is stopped when its own last rule lets go, so a level and a train can share an output. Before pull request 304, one rule's stop could leave the other kind on: a level stayed on, or a train kept running, until `campy-rules` ended.
- A switch-on the board didn't make is sent again. If a `level` or `train` switch-on is refused (too late, a clash with its group, or an error), `campy-rules` sends it again with the next set while the rule still holds. That set is a fresh frame. Each refusal and each retry is in the events log, and the summary counts the retries. A `pulse` or `burst` is not sent again: its moment has passed.
- `refractory_s`: a rule doesn't start holding again until that long after it last did. That still counts after a stalled feed, the watchdog or a `campy-live` restart, when the rules otherwise start over. Before pull request 311, those reset it, so a reward could come again sooner.

## Running it

Start the outputs board and `campy-rules` before the recording:
```
campy-rules rules.yaml --port /dev/ttyACM1
campy-live --calibration calibration.toml ...
campy-acquire config.yaml
```
- **Order matters.** The board counts camera triggers from 1 from when `campy-rules` starts, as campy counts its frames, so frame k is the same frame for both. Both keep counting through a pause in the trigger, such as the pause between two bouts, so a train's `every` and `phase` stay on campy's frame numbers. The board reports each pause of half a second or more, and `campy-rules` logs it (`kind: gap` in the events log). Before pull request 304 (sketch version 1), the board started again at frame 1 after such a pause, while campy didn't, so with `every: 2` blue and violet could swap relative to campy's frames. If the board started counting mid-recording, `campy-rules` matches the frames by time instead, to within a frame, says so and logs it (`kind: rebase`).
- It runs in campy's Python environment or in `campy-live`'s (it needs only numpy, pyyaml and pyserial).
- It ends when the recording does, or at Ctrl-C, and switches everything off as it ends. If `campy-live` ends or restarts during the recording, `campy-rules` keeps running and waits for it: see [When campy-live ends or restarts](#when-campy-live-ends-or-restarts).
- `campy-session` doesn't start `campy-rules`, and doesn't restart it. Start it yourself, before the recording, and stop it with Ctrl-C if you need to (it switches everything off). If it crashes or is killed, the board's watchdog switches everything off, and nothing starts it again.

Options:
- `--dry-run` opens no port: it reads the feed, checks the rules and logs what it would send.
- `--replay campy-live_<date_time>.npy` runs the rules over a finished `campy-live` log, with no board. It says how often each rule fired and for how long it held. Use it to tune regions, distances and debounce times on real data before an experiment.
- `--bound-ms`, `--stale-ms` and `--poll-ms`: see the latency and safety sections below.
- `--live-wait-s`: how long to wait for `campy-live` to come back while the recording goes on. The default is no limit. `--live-wait-s 0` ends `campy-rules` with `campy-live`, as before pull request 311.

### When campy-live ends or restarts

`campy-session` restarts a `campy-live` that stops or hangs during the recording ([Live tracking](live-tracking.md)), so a restart is a normal event. Since pull request 311, `campy-rules` rides through it:

1. As soon as `campy-live`'s results feed ends, whatever the rules switched on goes off: a laser, a rule's train. The session's trains (`run: session`, such as photometry excitation) keep running, since they don't depend on the poses.
2. `campy-rules` says so once: `campy-live ended: pose rules off, session trains on (blue, violet), waiting for campy-live (the recording goes on)`. It keeps the board's heartbeat going while it waits, so the board's watchdog stays quiet and the trains go on.
3. When the next `campy-live` makes its results feed, `campy-rules` reads it and runs the rules from its next sets. They start over: no rule holds, and each must be true again for `enter_ms` before it switches anything on. `refractory_s` still counts from each rule's last start. If the new `campy-live` has other cameras, nodes or animals, the rules are made again for it. If they no longer fit (a node the rules name is gone), `campy-rules` says why, switches everything off and ends.

**How it tells a restart from the end of the recording.** `campy-live`'s results feed names the cameras' live feeds it reads. Once sets have come, `campy-rules` notes them. When the results end, the recording goes on while any of those camera feeds is still the same feed, still recording, and its camera process still running. If every one has ended, is gone, was made again by the next recording, or was left behind by a `campy-acquire` that was killed, the recording has ended. `campy-rules` then ends, and switches everything off.
- It looks every half second while it waits, so the recording's end reaches the board within about a second.
- It looks the same way whenever no set has come for `--stale-ms`. So a hung `campy-live` whose feed never ends doesn't keep `campy-rules` running after the recording.
- A pause in the trigger, such as the pause between two bouts, is not an end: the cameras' feeds stay open through it.
- When it can't tell, `campy-rules` ends with `campy-live`, as before: if no set came before the results ended, or if `campy-live` is too old to name its camera feeds.

The events log has a `live_gone` line when the results end and a `live_back` line when they come back, or when `campy-rules` stopped waiting. That one has `gap_s`, `back`, and `why` (for example "the recording ended"). The summary lists the same under `live_gaps`. Pose rules were off from one to the other.

## Safety

- **Off by default.** The outputs are low from power-up and after every reset.
- **Watchdog.** `campy-rules` sends the board a heartbeat every 50 ms. If 250 ms pass without one while anything is on, the board switches everything off by itself. That covers a crash, a kill or an unplugged cable. It also covers a stuck decision loop: the heartbeat goes out only while the loop that checks the rules has gone round in the last 100 ms. Before pull request 304, another thread sent it, so a stuck loop kept the watchdog quiet. On a real board it fires 250 ms after the board last heard anything, to within 0.01 ms ([On a real board](#on-a-real-board)).
- **After the watchdog.** `campy-rules` starts the rules over from the next sets, and starts the session's trains (`run: session`) again. A rule that still holds then switches its output on again.
- **Logs never hold up a decision.** `campy-rules` writes its logs on a thread of its own. A slow disk delays neither the decisions, nor the stale-feed check, nor the heartbeat.
- **Clean exit.** `campy-rules` switches everything off when it ends, whatever the reason, the session's photometry trains included.
- **`campy-live` gone mid-recording.** What the rules switched on goes off at once. The session's trains, and the heartbeat, go on while `campy-rules` waits for the next `campy-live` ([When campy-live ends or restarts](#when-campy-live-ends-or-restarts)). Before pull request 311, `campy-rules` ended then, and its clean exit stopped the trains. When the recording itself ends, it ends and switches everything off, as before. The heartbeat still goes only while its waiting loop goes round, so a stuck or killed `campy-rules` is still caught by the watchdog.
- **A stalled feed.** If no set comes from `campy-live` for 200 ms (`--stale-ms`), whatever the rules switched on goes off. The session's trains keep running, since they don't depend on the poses.
- **Limits.** `max_on_s` per output, and groups that are never on together, are enforced on the board itself.
- **Old poses.** Nothing ever acts on a pose taken longer ago than `bound_ms` (next section). Switching off is never refused.

## Latency: what is exact and what is bounded

- **Pulses the board makes** (trains, and the width of every pulse) are timed on the board's clock. On the simulated clock they were exact to the microsecond. On the Nano ESP32, expect within a few µs: the sketch busy-waits the last 300 µs before each edge with interrupts off, like the [ChronicRecord timer](../chronicrecord/README.md). *Not yet measured on the board.*
- **Decisions from poses** take as long as the whole pipeline. Every command names the frame its pose came from. The board answers with how long after that frame's camera trigger its pin changed, so **every event in every experiment carries its own measured latency**. The board also refuses a command that arrives more than `bound_ms` after its frame's trigger (default 40 ms). `campy-rules` checks the same bound before sending.

The steps, with estimates for the EVO-X2 at 60 fps, until they are measured on a rig:

| Step | Estimate | Measured from |
|---|---|---|
| Behavior to the start of the next exposure | 0 to 16.7 ms (one frame) | Not measurable per event; set by the frame rate |
| Exposure, readout, USB 3 transfer, campy's grab | ~3–6 ms | The board's trigger time to `host_ns` |
| Feed, inference and 3D in `campy-live` | ~12 ms median | `host_ns` to `pose3d_ns` |
| `campy-rules` (poll and rules) | < 0.5 ms | `pose3d_ns` to the decision |
| USB serial write to the pin | ~1 ms (the Nano ESP32's USB is full speed) | The write to the board's pin time |
| **Frame's trigger to the pin** | **~17–20 ms median, ~25 ms p99** | **The board itself, per event** |

`campy-rules` prints each step's p50, p99 and worst case at the end, and writes them to its summary.

`campy-rules` hears each line from the board as soon as it arrives: a trigger's report, a command's answer, and the round trips that map the board's clock. Before pull request 417, it read in blocks that waited up to 5 ms for more to come; on Windows that wait is about 15 ms, the system timer's step. Measured on a real Nano ESP32 running `campy_outputs` on a Windows PC (the Dell Pro Tower Plus, 12 November, 3,000 commands each way, alternated): an answer was heard 6.0 ms after its command at the median (p99 10.6 ms) before, and 0.66 ms (p99 1.05 ms) after. The quickest round trip, which sets how well board times map to host times, was 14–15 ms before and 0.34–0.41 ms after. So, before, a pin time given on the host clock could be up to about 7 ms off on Windows; it is now within about 0.2 ms. The board's own latency per event (frame trigger to pin) is timed on the board's clock and was never affected. On the simulated board on a Mac, a trigger's report was heard 0.5 ms after it at the median, not 1.8 ms. This changes when `campy-rules` hears of a change, not when the pin changes.

On a Dell PC, the worst case comes every 15 minutes: Dell Optimizer's TechHub agent makes the BIOS work for about 9 s and then 2 s more, which slows the sets running then. On the Precision 7875 that added up to 2.5 ms to frame-to-3D. On the Pro Tower Plus the cost comes mostly in the first quarter-hour after the agent starts: frame to 3D up to 24.8 ms, against 12.6 ms at the 99th percentile ([Hardware guide](hardware-guide.html), "On a Dell PC, Dell's TechHub agent slows live tracking every 15 minutes").

To check the whole chain with your own eyes:
1. Put a test rule (`test: {every: 30}`, `action: pulse`) on an output that drives an LED all the cameras see.
2. The first frame showing the LED lit must match the board's pin time, as `campy-synctest` checks the trigger with its LED.

## What it logs

All host times are on campy's host clock (`hosttimes.npy`'s), so everything lines up with the frames.

- **`<out>.events.jsonl`**: one line per command, with the rule, the output, the frame, and its times:
  - the set's host times, when `campy-rules` read it, decided and wrote, and heard the answer;
  - the board's pin time (on the board's clock, and on the host clock);
  - `after_us`: the pin's change after the frame's trigger.

  Also one line per pulse the board made, with both edges; one per input edge (`kind: input`, with its board time, host time and the board's frame); and `live_gone` / `live_back` when `campy-live` was gone during the recording (pose rules off between them).
- **`<out>.board.txt`**: every line to and from the board. That includes every camera trigger's rise, `t <frame> <board µs>`.
- **`<out>.npy`**: per set, its frame, times, each rule's value (−1 unknown) and whether it held.
- **`<out>.json`**: the summary. Counts, the latency of each step, and the map from the board's clock to the host clock. That map is good to half its quickest round trip: about 0.2 ms on a Windows PC with a Nano ESP32 (up to about 7 ms before pull request 417).

## Tests

- `tests/test_outputs_sketch.py` runs the sketch on a simulated clock. It checks:
  - interleaved 3 ms trains exact to the microsecond;
  - offsets, bursts, and stopping mid-pulse;
  - groups never on together;
  - refusing late commands;
  - the watchdog and `max_on_s`;
  - counting on through a pause (and a train keeping its frames across it), and the 71-minute wrap of the board's clock;
  - every line coming whole through a 64-byte USB transmit buffer, as a Nano ESP32's (the simulated board has one since pull request 430): `status` (about 110 characters), seven answers back to back, and four trains' pulse reports at once. Before, the sketch dropped a line that didn't fit at once; the simulated board had no such buffer, so no test saw it. On a real Nano ESP32 (13 November, the Dell Pro Tower Plus's camera-trigger board, flashed for the test and restored after it), `status` came whole 5 times out of 5, 30 commands sent at once were all answered in order with nothing lost, `status` read both outputs high before the watchdog and none after it (fired 249.989 ms after the board last heard anything), and a port closed while answers were still coming left nothing for the next program to open it.
- `tests/test_rules.py` checks every condition, debouncing, hysteresis, missing data, refractory periods and file errors. It also checks `--dry-run` and `--replay`, a level and a train on one output (both orders), switch-ons refused and sent again, the recovery after the watchdog, a stuck decision loop that lets the watchdog fire, and a slow log write. For `campy-live` ending mid-recording, it checks that:
  - the laser goes off at once and the session's train keeps running (no stop, no `alloff`), with the heartbeat at its usual pace and quiet for 200 ms or more at most once (one heartbeat sent late by a busy test computer, not by `campy-rules`);
  - the rules resume on the next `campy-live`'s feed, in one log;
  - `campy-rules` ends with `alloff` when the recording ends, while it waits or before (the cameras' feeds end first, as at a normal stop), or while `campy-live` is hung;
  - it ends with `campy-live` as before without camera feeds to look at, or with `--live-wait-s 0`;
  - a camera feed left by a killed recording, or made again by the next one, counts as ended;
  - `refractory_s` counts across the restart.

  For inputs, it checks a poke rewarded only with mouse 2 at the port, an input shorter than a frame counted once, an input that is unknown without a board, the board's setup, and the whole path in real time: pokes on the simulated board's A0, each rewarded once.

  One of these runs the sketch in real time with a 60 Hz trigger: `campy-live` gone for 2 s, the session train's pin kept pulsing (20 pulses or more during the gap) and the board's watchdog never fired. That test sets the watchdog to 500 ms, not 250, because a 250 ms hold-up of the simulated board or of `campy-rules` on a busy test computer could fire it. It also checks the heartbeat's pace from what `campy-rules` sent. Slowing the waiting loop to 300 ms makes that test and the heartbeat check fail. And it runs the whole path in real time: a results feed, `campy-rules`, and the sketch on a simulated serial port with a 60 Hz trigger.

### On a real board

On 12 and 13 November the camera-trigger Nano ESP32 of the Dell Pro Tower Plus was flashed with `campy_outputs` (version 2) for a few minutes at a time. Each time it was flashed back afterwards with the exact binary it had run before, and it answered its trigger commands as before. Its outputs were the board's own RGB LED (pins 14 and 15, on no header pin); no trigger was wired. Under `campy-rules`' own `Board` (`campy/trigger/outputs.py`), with the heartbeat stopped the way a stuck decision loop stops it:

- the watchdog fired 249.996 ms and 249.995 ms after the board last heard anything (with one and with two outputs on; `watchdog_ms` 250), and 999.994 ms with 1,000, timed on the board's own clock;
- it stayed quiet with nothing on, and with an output on while heartbeats came (3 s);
- once the heartbeat came back, nothing switched itself on again, and the next `on` worked.

With the sketch from before pull request 430, the board can't answer `status` on a Nano ESP32: its answer (about 110 characters) is longer than the 64 bytes its USB serial buffer holds, and that sketch drops a line that doesn't fit rather than wait. Pull request 430 sends such lines in pieces. `campy-rules` doesn't use `status`. With pull request 430's sketch on the same board, `status` was answered 5 times out of 5, 30 commands sent at once were all answered in order, and nothing was lost.

The trains need camera frames, and no trigger was wired, so on 13 November the board made its own: channel 7 drove the RGB LED's blue pin (16, also on no header pin), and that same pin was the trigger input (`trig 16`, then `pin 7 16`; in that order, since `trig` makes the pin an input only). The ESP32's Arduino core keeps a pin's input on when it is an output, so the board counts its own pulses as frames. The computer sent `pulse 7 2000` about 60 times a second. With trains on channels 0 and 1 (every 2nd frame, 1 ms after the frame's trigger, 3 ms long):

- a level and a train on one output, both orders: `off` left the train running (30 pulses in the next second, each 3,000 µs), `run 0 0` stopped it (no pulse after it), and with the train stopped first the level stayed on until `off`; `status` then said nothing was high and no train was running;
- across a 1 s pause in the frames, the board said `gap` once (1.017 s), went on counting frames (no restart from 1), and every one of the 240 frames from the first pulse to the last got exactly one pulse from the right channel: channel 0 on even frames, channel 1 on odd ones, 120 of them after the pause;
- every pulse rose 1,000 µs after its frame's trigger and lasted 3,000 µs, on the board's own clock; no clash, late command or watchdog.

The pins' levels weren't measured with an instrument; all of the above is the board's own report (its `p`, `gap` and `status` lines). The frames came from the board itself, with the computer's timing, not from a camera trigger.
