# Closed-loop outputs with campy-rules

`campy-rules` lets an experiment act on the animals' live poses. Examples: switch on a laser while mouse 2 is in the nest, give a reward pulse when two mice touch noses, or send event marks to the recording system. It also makes photometry excitation pulses, locked to the camera frames, for the whole session or only while a rule holds.

It works on top of [live tracking](live-tracking.md):

- **`campy-live`** puts each set's poses in shared memory, the *results feed*.
- **`campy-rules`** reads that feed and checks each set against your rules. It is a program of its own: `campy-live` never waits for it, so it can't slow tracking down.
- **An Arduino Nano ESP32**, the *outputs board*, makes the TTL pulses on up to 8 outputs. It runs the sketch `campy/trigger/campy_outputs`. It watches the camera trigger, so it knows exactly when each frame was taken.

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
arduino-cli compile --fqbn arduino:esp32:nano_nora campy/trigger/campy_outputs
arduino-cli upload --fqbn arduino:esp32:nano_nora -p <port> campy/trigger/campy_outputs
```
The port is `/dev/ttyACM1` or similar on Linux, `COM5` or similar on Windows. The board answers `ok campy_outputs 1` to `id` at 115200 baud.

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
- **`all`, `any`, `not`** combine conditions.
- **`animal:`** an identity (0 to animals − 1, from `campy-live --calibration`), a list of them, or `any`.
- **`node:`** a name, an index, a list (their mean), or `all`.

**Missing data.** An animal that fewer than 2 cameras see has no 3D point. Its rule then keeps its last value for `missing_ms` (default 100), then counts as false. So missing data can switch an output off, never on.

**Identities** come from `campy-live`'s tracker, which can swap two animals. Each command's log line has the frame, so a swap can be checked afterwards in LUC3D.

### Actions

| `action` | What the output does |
|---|---|
| `level` | On while the rule holds (the default for an output without `train` or `pulse_ms`) |
| `pulse` | One pulse of `pulse_ms` each time the rule starts holding |
| `train` | Its train runs while the rule holds |
| `burst` | `pulses: N` of its train each time the rule starts holding |

- Several rules on one output: it is on, or its train runs, while any of them holds.
- `refractory_s`: a rule doesn't start holding again until that long after it last did.

## Running it

Start the outputs board and `campy-rules` before the recording:
```
campy-rules rules.yaml --port /dev/ttyACM1
campy-live --calibration calibration.toml ...
campy-acquire config.yaml
```
- **Order matters.** The board counts camera triggers from 1 when the trigger starts after a pause, as campy counts its frames, so frame k is the same frame for both. If the board starts counting mid-recording, `campy-rules` matches the frames by time instead, to within a frame, and says so.
- It runs in campy's Python environment or in `campy-live`'s (it needs only numpy, pyyaml and pyserial).
- It ends when `campy-live` does, or at Ctrl-C, and switches everything off as it ends.

Options:
- `--dry-run` opens no port: it reads the feed, checks the rules and logs what it would send.
- `--replay campy-live_<date_time>.npy` runs the rules over a finished `campy-live` log, with no board. It says how often each rule fired and for how long it held. Use it to tune regions, distances and debounce times on real data before an experiment.
- `--bound-ms`, `--stale-ms` and `--poll-ms`: see the latency and safety sections below.

## Safety

- **Off by default.** The outputs are low from power-up and after every reset.
- **Watchdog.** `campy-rules` sends the board a heartbeat every 50 ms. If 250 ms pass without one while anything is on, the board switches everything off by itself. That covers a crash, a hang, a kill or an unplugged cable.
- **Clean exit.** `campy-rules` switches everything off when it ends, whatever the reason.
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

To check the whole chain with your own eyes:
1. Put a test rule (`test: {every: 30}`, `action: pulse`) on an output that drives an LED all the cameras see.
2. The first frame showing the LED lit must match the board's pin time, as `campy-synctest` checks the trigger with its LED.

## What it logs

All host times are on campy's host clock (`hosttimes.npy`'s), so everything lines up with the frames.

- **`<out>.events.jsonl`**: one line per command, with the rule, the output, the frame, and its times:
  - the set's host times, when `campy-rules` read it, decided and wrote, and heard the answer;
  - the board's pin time (on the board's clock, and on the host clock);
  - `after_us`: the pin's change after the frame's trigger.

  Also one line per pulse the board made, with both edges.
- **`<out>.board.txt`**: every line to and from the board. That includes every camera trigger's rise, `t <frame> <board µs>`.
- **`<out>.npy`**: per set, its frame, times, each rule's value (−1 unknown) and whether it held.
- **`<out>.json`**: the summary. Counts, the latency of each step, and the map from the board's clock to the host clock. That map is good to half a USB round trip, about 1 ms.

## Tests

- `tests/test_outputs_sketch.py` runs the sketch on a simulated clock. It checks:
  - interleaved 3 ms trains exact to the microsecond;
  - offsets, bursts, and stopping mid-pulse;
  - groups never on together;
  - refusing late commands;
  - the watchdog and `max_on_s`;
  - counting from 1 after a pause, and the 71-minute wrap of the board's clock.
- `tests/test_rules.py` checks every condition, debouncing, hysteresis, missing data, refractory periods and file errors. It also checks `--dry-run` and `--replay`. And it runs the whole path in real time: a results feed, `campy-rules`, and the sketch on a simulated serial port with a 60 Hz trigger.
