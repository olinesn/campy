# Operant boxes

campy can run an operant box's hardware: read its sensors (IR beam breaks, nose pokes, touch or lick sensors) and drive its actuators (hobby servos for doors and levers, solenoid valves and pumps, cue lights and tones, event marks to a recording system). Every sensor edge is stamped with the camera frame it happened in, so the box's events line up with the video.

It is the [outputs board](closed-loop.md#the-board) grown by a few features, not a new board. The same Arduino Nano ESP32 and the same sketch, `campy/outputs_board/campy_outputs` (version 3), already count the camera's frames, make exact pulses, and switch everything off when the computer goes away. Version 3 adds inputs, servos and *links* (reflexes on the board). One board per box does all of it, so a rule can combine a poke with the animals' live poses.

**What exists today, and what comes next.**

1. **The board** (pull request 449): inputs, servos, links, the safe state; tested on a simulated board.
2. **`campy-box`** (pull request 451): runs a task from a YAML file (states such as *wait for a poke, open the door, give a reward*), and logs every event with the board's time, the computer's time and the camera frame. [Running a task](#running-a-task-campy-box) below.
3. **Recordings** (next): `campy-session` starts the task with the recording and keeps its log in the recording's folder; the panel shows the counts. Until then, start `campy-box` by hand before the recording.
4. **Poses in the task** (after that): `campy-rules`' conditions (an animal in a region, two animals close) usable in a task, and the box's inputs usable in `campy-rules`.

## How the work is divided

- **The board** reads the sensors and times everything to the microsecond on its own clock. *Links* react to a sensor on the board itself, in microseconds: a poke opens the reward valve, a beam break moves a door. The computer only arms and disarms them.
- **The computer** runs the task: which state the box is in, what is armed, when a trial ends. Its decisions take a few milliseconds over USB, which is fine for task logic but not for a reward that must follow a poke at once. That is what links are for.
- **The camera trigger** goes into the board too (as for `campy-rules`). The board counts its pulses as frames 1, 2, 3, … and says, for each sensor edge, which frame it fell in and how long after that frame's trigger.

## Running a task: campy-box

`campy-box` sets the board up from a task file, runs the task's states, and logs everything. Start it before the recording, so that the board's frame 1 is the recording's first frame:

```
campy-box task.yaml --port COM7          # /dev/ttyACM1 or similar on Linux
campy-box task.yaml --check              # check the file, show the board's setup and the states; no board
```

A task file names the box's parts, the reflexes, and the states. For example, a door that opens for each trial and a reward for a poke in the left port:

```yaml
board:
  port: COM7
  trigger_pin: 10           # the camera trigger; none: no camera
  watchdog_ms: 250          # the board makes the box safe if campy-box goes quiet this long
inputs:
  poke_left:  {input: 0, pin: A0, pull: up}          # pull-up: active low, as most beam breaks
  poke_right: {input: 1, pin: A1, pull: up}
  lick:       {input: 2, pin: A2, pull: down, debounce_ms: 0.5}
outputs:
  valve: {channel: 0, pin: D2, pulse_ms: 40}
  cue:   {channel: 1, pin: D3, max_on_s: 60}
  mark:  {channel: 2, pin: D4, pulse_ms: 5}           # event marks to the recording system
servos:
  door: {servo: 0, pin: D11, positions: {closed: 1000, open: 2000}, safe: closed, speed_us_per_s: 2000}
links:
  mark_pokes:  {when: poke_left, do: {output: mark}, armed: always}
  reward_left: {when: poke_left, do: {output: valve}}     # armed by the states that name it
task:
  start: iti
  end: {trials: 100, minutes: 60}
  states:
    iti:
      do: [{servo: door, to: closed}, {output: cue, action: off}]
      after_s: [8, 12]      # a random time between the two
      next: ready
    ready:
      trial: true           # entering it starts a trial
      do: [{servo: door, to: open}, {output: cue, action: on}]
      arm: {reward_left: 1} # armed while in this state, for one firing
      on: {poke_left: reward, poke_right: iti}
      after_s: 30
      next: iti
    reward:
      do: [{output: cue, action: off}]
      after_s: 2
      next: iti
```

- **States** do their actions on entering, arm their links while they last, and move on when an input has an edge (`on`) or when their time is up (`after_s`, then `next`). `on` can wait for an inactive edge or for the n-th edge (`{lever: {to: reward, count: 5}}`, a fixed ratio of 5). `next` can be a random choice (`[left_trial, right_trial]`, or with weights `{left_trial: 3, right_trial: 1}`). `after_s: [8, 12]` is a random time. `trial: true` starts a trial; `end: true` ends the task. `python -m pydoc campy.box` describes every key.
- **The reward is the board's.** The poke rewards through a link, which the board fires within microseconds of the poke. The task's move to `reward` comes a few milliseconds later, when the computer hears of the poke. Leaving `ready` disarms the link.
- **Outputs and servos stay as they are** until a state changes them: `iti` closes the door and switches the cue off itself.
- **Random times and choices** come from a seed, which is logged; `seed:` under `task:` makes a run repeat the same ones.
- **The task ends** after `end: trials` (instead of starting one more), after `end: minutes`, in a state with `end: true`, on Ctrl-C, or with `--duration-s`. Then, and if campy-box fails, the box is made safe.
- **The watchdog.** If campy-box is held up long enough for the board's watchdog to make the box safe, campy-box says so, arms its always-armed links again and goes on from `task: after_watchdog` (default: the start state).
- `on` and `off` can go without quotes: YAML reads them as true and false, and `campy-box` takes them back as the words.

**What it logs** (`--out`, default `campy-box_<date_time>`):

- `<out>.events.jsonl`, a line per event: every input edge (`kind: input`, with `board_us`, `host_ns` on campy's host clock as in `hosttimes.npy`, `frame` and `after_us`), every link firing, each state entered (with what caused it: the input and its times, or the timer), each pulse with its edges, levels switched by the board, servo arrivals, every command and its answer, trigger pauses and the watchdog;
- `<out>.board.txt`, every line to and from the board;
- `<out>.json`, the summary: trials, why it ended, counts per input, link and state, the seed and the board-to-host clock map.

## Wiring (Arduino Nano ESP32)

| Board pin | What | Notes |
|---|---|---|
| A0 to A7 | Inputs 0 to 7: beam breaks, nose pokes, lick or touch sensors | Suggested; any free pin works |
| D11, D12 (D13, D0, D1 if needed) | Servos 0 to 3: the servo's signal wire | Suggested; D13 also lights the board's LED |
| D2 to D9 | Outputs (channels 0 to 7): valves, pumps, cue lights, tone generators, event marks | The sketch's defaults |
| D10 | Camera trigger in: a wire from the trigger board's camera output | The sketch's default |
| GND | Shared with the trigger, every sensor, every servo supply and every driver | |

The host sets every pin when it starts (`in`, `servo`, `pin`, `trig`), so other pins work too. No pin can be both an input or a servo and anything else; the board refuses such a command.

**Two Nano ESP32s.** A rig with a box has two: one runs `trigger.ino` and pulses the cameras (campy-acquire drives it), the other runs this sketch for the box ([hardware list](hardware.md)). The trigger board's pulse is 3.3 V too, so its camera output goes straight to the box board's D10, with the two boards' grounds joined. A 5 V trigger board (an Uno) needs a divider there instead.

**3.3 V logic.** The ESP32's pins take at most 3.3 V.

- *Beam breaks and nose pokes* with an open-collector output (most IR break-beam receivers): set the input to pull-up and active low. The board's own pull-up (about 45 kΩ) holds the line at 3.3 V and the sensor pulls it down when the beam is broken. For long cables, add a 10 kΩ pull-up to 3.3 V at the board.
- *Sensors with a 5 V push-pull output* need a divider (for example 10 kΩ over 20 kΩ) or a level shifter before the pin.
- *Touch or lick sensors (capacitance)*: use a module that does the sensing and gives a digital line (a lickometer, or a TTP223-type touch board), wired like any other input. The board doesn't read capacitance itself.

**Servos.** Power hobby servos from their own 5–6 V supply, never from the board's 5 V pin, and join that supply's ground to the board's. Most hobby servos take the 3.3 V signal directly.

**Valves and pumps.** Never drive a solenoid from a pin. Use a driver (a logic-level MOSFET module or a valve driver board) with a flyback diode across the coil, and its own supply.

**Pull-downs on outputs.** As for `campy-rules`, a 10 kΩ resistor from each output to ground keeps a valve or laser driver off while the board resets.

## Inputs

`in <i> <pin> <pull> <active> <debounce_us>` sets input i (0 to 7): pull 0 none, 1 up, 2 down; active 1 if the sensor's "on" is high, 0 if it is low. The board answers with the input's pin and its level now (`ok in 0 <pin> 0` when inactive). `in <i> -1` frees it. A pin is a number (0 to 48), or 100 to 107 for A0 to A7.

Each change of an input is said as

```
i <input> <1 active, 0 inactive> <t_us> <frame> <us after that frame's trigger>
```

`t_us` is when the pin changed, on the board's clock (microseconds since power-up). 1 always means *active* (a beam broken, a nose in the port, a tongue on the spout), whichever level that is on the wire. The board reads its inputs in every timing loop too, including while it watches for a camera trigger, so the time is that of the change to within a few microseconds. The frame is 0 when no trigger has come yet.

**Debouncing** is a lockout: the first edge is taken at once (no delay for a link to wait on), then the input is ignored for `debounce_us`. A change during the lockout is said when the lockout ends, with the time it happened. So a lever's bouncing contact is one press, at its first contact, and a pulse shorter than the lockout still has both its edges. Clean TTL sensors need little or none (0.5 ms for a lick sensor); a mechanical switch needs a few milliseconds.

## Servos

`servo <s> <pin> <min_us> <max_us> <safe_us> <speed>` sets servo s (0 to 3). The board sends it 50 pulses a second on the ESP32's PWM hardware, so the pulses never jitter whatever else the board is doing. It starts at `safe_us`: the position that is safe when nobody is in charge, such as a door closed or a lever retracted. `speed` is how fast it moves, in microseconds of pulse per second: 2000 takes a 1000–2000 µs servo from one end to the other in half a second. 0 moves it at once (as fast as the servo goes). `min_us` and `max_us` bound every move (500 to 2500 µs).

`move <s> <us>` sends it to a position. The board says `m <s> <us> <t_us>` once its pulse is there; the servo itself takes a little longer to follow. `servo <s> -1` stops its pulses, which leaves a hobby servo limp.

## Links: reflexes on the board

`link <l> <input> <edge> <what> <a> <b>` sets link l (0 to 7): when the input goes active (edge 1) or inactive (edge 0), do *what*:

| what | Does | a | b |
|---|---|---|---|
| 1 | a pulse | channel | width, µs |
| 2 | switch a level on | channel | (0) |
| 3 | switch a level off | channel | (0) |
| 4 | move a servo | servo | position, µs |
| 5 | run a channel's train | channel | pulses (-1 until stopped) |

A link starts disarmed. `arm <l> <n>` lets it fire n times (1: the next poke only), -1 until disarmed, 0 to disarm. Each firing is said as `x <l> <input> <t_us> <frame> <1 done, 0 refused>`, followed by what it did (a pulse's `p` line, a level's `e … link`, a servo's `m`). It is refused, and the box goes on, when the channel is already high, would clash with its group, or has no pin. On the simulated board, a valve's pulse starts within 10 µs of the poke that fired it.

For example, a reward for the next poke in port 0, a 40 ms valve opening on channel 3:

```
in 0 100 1 0 1000        poke port on A0: pull-up, active low, 1 ms lockout
link 0 0 1 1 3 40000     its poke: pulse channel 3 for 40 ms
arm 0 1                  once
```

## Safety

The outputs board's watchdog covers the box too. If the computer says nothing for the watchdog's time (250 ms by default: a crash, a hang, an unplugged cable) while anything is active, or when it sends `alloff`:

- every output goes low and every train stops (as before);
- every link is disarmed, so no reward is given without the task;
- every servo goes back to its safe position, at its own speed.

An armed link and a servo sent away from its safe position count as active, so the watchdog guards them. Once everything is back, there is nothing left to guard and the board stays quiet. `box` says the state: `ok box t=… in=<active inputs, a bit each> armed=<armed links> s0=<position>/<target> …`.

## What has been tested

- **On a simulated board** (`tests/test_outputs_sketch.py`, run in CI on Linux (x86 and Arm) and Windows, and on a Mac): input edges stamped within 3 µs of the change and in the right frame, also while the board watches for a trigger and while a train's pulse is due; pull-up beam breaks; bouncing contacts as one press; a pulse shorter than the lockout keeping both edges; licks at 7 Hz beside two photometry trains, whose pulses stay exact to the microsecond; a link firing once; a servo moving at its speed in 20 ms steps; the watchdog and `alloff` disarming links and sending servos back; pins never shared. The sketch's earlier tests all pass unchanged, so nothing `campy-rules` relies on moved.
- **Compiled for the Nano ESP32** (Arduino ESP32 core 2.0.18, as in CI). The simulated board stands in for the ESP32's input registers and PWM hardware, so the real board's timing is not measured yet.
- **`campy-box`** (`tests/test_box.py`): the file's checks, a task run against a scripted board (pokes, trials, the end after N trials, a fixed ratio, the watchdog, the seed), and the whole path on the board's sketch running in real time on a pseudo-terminal: pokes every 0.4 s on a pull-up input, each rewarded by the board's link in the right frame, three trials, the door servo's PWM.
- **Not yet on a real board, sensor or servo.** That needs a spare Nano ESP32: every one on the test machines today is a camera trigger.
