# One experiment on two computers: cameras on one, SpikeGLX on the other

A design, being built (branch `claude/dp-two-pc-experiment`). It is not yet tested on a rig.

Some rigs record the cameras on one computer and Neuropixels with SpikeGLX on another: here, a PXIe chassis with an imec base station and an NI PXIe-6341. Today each computer is started by hand, and the recordings are lined up afterwards, if at all. This design makes one run of the two:
- one run name;
- one recording folder;
- a start and stop in a fixed order;
- a check afterwards that every frame has its trigger pulse in SpikeGLX's file.

## The idea

The camera computer's `campy-session` (its panel) runs the experiment. It drives SpikeGLX over the network through **SpikeGLX's own command server**, with SpikeGLX's SDK, exactly as it already drives a SpikeGLX on the same computer (`spikeglx:` section, `campy/spikeglx.py`). The SpikeGLX computer needs nothing new installed.

The two clocks are tied together in hardware, not over the network: **the trigger board's camera line also goes into one of SpikeGLX's digital inputs** (an NI-DAQ's XD line). Every camera frame comes from one pulse, and SpikeGLX records every pulse with its own sample clock. The two computers' wall clocks are never compared: every time campy-session writes comes from the camera computer's clock, and SpikeGLX's side is in its own samples.

## A run

```yaml
# the experiment's config, on the camera computer
extends: my_rig_daily.yaml
spikeglx:
  sdk: 'C:\campy_data\live\sglx-sdk'   # SpikeGLX-CPP-SDK's sglx_pkg and SglxApi.dll
  host: <the SpikeGLX computer's address>
  data_dir: 'D:/SGL_DATA'                # SpikeGLX's data folder, on ITS disk
  copy_from: '\\<that computer>\SGL_DATA'  # the same folder as this computer reaches it (a read-only share)
  trigger: immediate                     # no gate line: SpikeGLX records from "enabled" to "stop"
  stream: [0, 0]                         # the NI stream
  camera_bit: 2                          # the XD line the camera pulses are on
  run_prefix: 'dp_'
```

On the SpikeGLX computer, once:
- SpikeGLX's command server is enabled and listens on that computer's own address (Options > Command Server Settings), not 127.0.0.1.
- A firewall rule lets only the camera computer reach its port (4142). The command server has no password, so anyone who can reach it can stop a recording.

1. **Checks (the panel's list).** SpikeGLX answers, runs no run, and its trigger is Immediate (Configure > Triggers). Otherwise Start is blocked.
2. **Start.** campy-session sets SpikeGLX's data folder and run name (`<run_prefix><the recording's date_time>`) and starts its run. Once the stream holds 2 s of samples, it enables recording. It writes the stream's sample count at that moment, with the time on campy's host clock and the call's round trip, to `spikeglx.json` in the recording's folder.
3. **The cameras.** campy-acquire opens them, and starts the trigger only once every camera is grabbing. So SpikeGLX is always recording before the first pulse.
4. **Stop.** campy stops the trigger and the cameras. 3 s later campy-session records the sample count again and stops SpikeGLX's run. With `copy_from`, it copies SpikeGLX's run folder into `<recording>/spikeglx/`, each file checked by size. The recording's own copy step (`post.copy`) then takes cameras and SpikeGLX together.

If SpikeGLX doesn't start, the cameras don't either. If campy fails after SpikeGLX started, campy-session stops SpikeGLX's run. If the network drops mid-run, both keep recording, and at the end the step says SpikeGLX's run didn't stop (`stopError` in `spikeglx.json`): stop it in SpikeGLX.

## The check: `campy-ephyssync`

```bash
campy-ephyssync <recording>           # --bit N, --nidq <file>, --ap <file>, --no-probe
```

It reads SpikeGLX's NI file (the digital word's `camera_bit`), campy-align's trigger numbers for every frame (`aligned.csv`; it runs campy-align if needed) and each camera's own timestamps. It reports:
- **pulses**: SpikeGLX's count against the cameras' triggers; each camera's frames, the pulses it has no frame for, and any frame whose pulse SpikeGLX lacks (a problem);
- **the trigger board's clock on SpikeGLX's**: pulse time = a + b × trigger number, its period, how far it is from the nominal rate (ppm), and the residuals;
- **each camera's clock on SpikeGLX's**: its rate difference (ppm) and residuals;
- **margins**: how long SpikeGLX recorded before the first pulse and after the last (a margin of one period or less is a problem: pulses may be lost); from `spikeglx.json`, the time from enabling SpikeGLX's recording to the trigger's start, and to its stop, on the camera computer's clock;
- **the probe's clock**, when SpikeGLX's sync is on (sync source: the imec base station, its 1 Hz wave wired to an NI digital line): the NI clock fitted to the probe's through the wave's edges in both files (the probe's SY word, bit 6), so every frame gets a time on the probe's clock.

It writes `ephyssync.json` in the recording's folder, and `ephys_times.csv` in each camera's (frame, trigger, `ni_s`, and `probe_s` when the probe's clock is known). Exit code 0 when every frame has its pulse.

Tested on made-up recordings (`tests/test_ephyssync.py`), with these results:
- a 60 fps board 35 ppm fast at 25 kHz NI sampling: every pulse found, residuals within one sample;
- cameras 8 and −4 ppm off: measured within 0.5 ppm;
- missed frames counted;
- a SpikeGLX that started after the first pulses reported as a problem;
- a probe clock 12 ppm off and 0.2 s offset: recovered.

## Not done yet

- A test on a real rig: in stages, emulated cameras with SpikeGLX's simulated NI first, then real cameras with the NI-DAQ, then the probe.
- A gate line: the trigger board holds a pin high while it triggers, and SpikeGLX records only then (`trigger: ttl`, as with the ChronicRecord Pico). With it, a network drop could not leave SpikeGLX recording. `trigger.ino` has no gate output yet.
- An alert when SpikeGLX stops answering mid-run (today the panel's status shows it, and the stop says it).
- campy-session post running `campy-ephyssync` by itself.
