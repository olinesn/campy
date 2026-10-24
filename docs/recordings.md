# Recording folders: what they hold and how they were made

Each run of `campy-acquire` records into a new folder inside `videoFolder`, named by the date and time it started (`2026-10-23_14-05-11`). This page covers what goes into that folder, the `session.json` that records how the recording was made, how to enter the experiment's details, and the warnings campy shows at startup when a config relies on a default that has changed.

## What is in the folder

```
2026-10-23_14-05-11/
  session.json                how the recording was made (below)
  trigger.json                how the trigger started (when campy starts it)
  campy-live-session.json     how campy-live tracked it (when campy-live ran)
  Camera0/
    0.mp4                     the video
    frametimes.npy            each frame's number and camera timestamp
    hosttimes.npy             when each frame reached the computer
    frametimes.mat            the same as frametimes.npy, for MATLAB
    metadata.csv              the camera's settings and totals
  Camera1/ ...
```

`campy-live-export` later adds `campy-live.slp`, `skeleton.json`, `calibration.toml` and `points3d.h5` (see [Live tracking](live-tracking.md)).

## session.json

`metadata.csv` already listed each camera's settings, but a folder did not say which campy made it, on which computer and drivers, from which config, or what the experiment was. The same config can make different recordings on different campy versions: in 2.1.0, FLIR cameras went from no gamma to gamma 0.6 by default, so the same config gave brighter videos. `session.json` records all of it, in one JSON file any program can read (strict JSON: see [Infinity and NaN](#infinity-and-nan-in-these-files)). Its schema is `"campy-session/1"`.

campy writes it three times during a recording. Each write adds sections and never changes what an earlier one said, except `status`:

1. **At the start**, before the cameras open:
   - `campy`: the version, the git commit and branch, and `dirty` (true when the checkout's files differ from that commit).
   - `host`: the computer's name, operating system, CPU, Python and its environment, each GPU with its driver version (on Linux, where amdgpu and i915 are part of the kernel, the kernel's version, plus the GPU's firmware and ROCm's version where ROCm is installed), ffmpeg's path and version, the camera SDK version (Spinnaker, pylon) and the versions of the Python packages campy uses.
   - `config`: the config file's path, its SHA-256 and its full text, and the command line. It also has `effective`: every setting's value, with where it came from (`"default"`, `"file"` or `"command line"`). A default that has changed since an earlier version also says when and what it was (`"defaultChanged": {"in": "2.1.0", "was": "Off", "now": 0.6}`). Last, the startup warnings (below).
   - `experiment`: the experiment's name, the animal IDs, the notes and any other fields (below).
   - `cameras`: each camera's name, make, serial number, folder and video, and its settings file with a SHA-256 (Basler `.pfs`).
   - `trigger`: the controller, whether campy drives it, the serial port, the pins and the frame rate.
2. **Once the trigger has started** (or campy has stopped waiting for the cameras): `recording` has `trigger.json`'s record, and each camera's model, firmware, frame size, exposure, gain and gamma as the camera reported them, and whether it was ready.
3. **When the cameras stop**: `end` has the exit code, the cameras that did not record to the end and why, and each camera's frames grabbed, written, dropped and lost, its seconds, its errors and its video's size.

`status` is `"recording"` from the start, then `"complete"`, `"interrupted"` (stopped with Ctrl-C) or `"failed"` (a camera did not record to the end, or campy stopped on an error). A `status` still `"recording"` means campy never reached its end: it was killed, or the computer lost power. The videos and frame times are still there up to their last second, but `end` is missing.

Each write replaces the whole file at once (a new file, renamed over the old), so a program reading it never finds half a file.

An abridged example:

```json
{
  "schema": "campy-session/1",
  "status": "complete",
  "written": {"start": "2026-10-23T14:05:11-04:00", "recording": "2026-10-23T14:05:14-04:00", "end": "2026-10-23T14:15:15-04:00"},
  "campy": {"version": "2.1.0", "commit": "5950afd…", "branch": "dev", "dirty": false, "installedFrom": "git checkout"},
  "host": {"os": "Windows-11-…", "cpu": "…", "gpus": [{"vendor": "NVIDIA", "name": "NVIDIA RTX 2000 Ada Generation", "driver": "…"}],
           "ffmpeg": {"path": "…", "version": "ffmpeg version 7.1 …"}, "sdk": {"spinnaker": "4.4.0.…"}, "packages": {"numpy": "…"}},
  "config": {"file": {"path": "…\\chronic.yaml", "sha256": "…", "text": "…"},
             "effective": {"frameRate": {"value": 60, "source": "file"},
                           "quality": {"value": 24, "source": "command line"},
                           "cameraGamma": {"value": 0.6, "source": "default",
                                           "defaultChanged": {"in": "2.1.0", "was": "Off", "now": 0.6}}},
             "defaultWarnings": ["cameraGamma not set: since 2.1.0 the default is 0.6 (was Off). …"]},
  "experiment": {"name": "social", "animalIds": ["M1", "M2", "M3", "M4", "M5"], "notes": "new bedding", "fields": {"cage": "B3"}},
  "annotations": [{"at": "2026-10-23T15:02:40-04:00", "text": "M3 lost its ear tag at about 5 min"}],
  "cameras": [{"index": 0, "name": "Camera0", "make": "flir", "serial": "…", "video": "Camera0/0.mp4"}],
  "trigger": {"controller": "arduino", "drivenByCampy": true, "frameRate": 60},
  "recording": {"trigger": {"triggerStarted": true}, "cameras": [{"name": "Camera0", "cameraModel": "Blackfly S BFS-U3-16S2M", "cameraFirmware": "…", "gamma": 0.6, "ready": true}]},
  "end": {"exitCode": 0, "failures": {}, "cameras": [{"name": "Camera0", "framesGrabbed": 36000, "framesWritten": 36000, "framesDropped": 0, "framesLost": 0, "seconds": 600.0}]}
}
```

Each camera's firmware is now also in its `metadata.csv` (`cameraFirmware`: FLIR's `DeviceFirmwareVersion`, Basler's device version).

`session.json` includes the computer's name and the full paths of the config and the recording. Remember that when you share a recording outside the lab.

## The experiment's details

There are four config keys, each also a command-line option:

| Key | What | Example |
|---|---|---|
| `experiment` | the experiment's name | `experiment: "social-5mice"` |
| `animalIds` | the animals recorded: one list for the whole rig, not one per camera | `animalIds: [M1, M2, M3]` |
| `notes` | anything to note before the recording | `--notes "new bedding"` |
| `sessionFields` | any other fields, as a mapping | `sessionFields: {cage: B3, condition: saline}` |

They go into `session.json`'s `experiment`, and into each camera's `metadata.csv`. campy doesn't prompt for them, so rigs started by a script or by the trigger's controller never wait at a prompt. Put the ones that stay the same in the config and give the rest on the command line, for example `campy-acquire config.yaml --animalIds "[M4, M7]" --notes "after surgery"`.

To add a note afterwards (something seen during the recording, a correction):

```bash
campy-session note C:\campy_data\chronicRecording\2026-10-23_14-05-11 "M3 lost its ear tag at about 5 min"
```

The note goes into `annotations`, with the time it was added, and nothing else in the file changes. Until the `campy-session` launcher is installed, `python -m campy.manifest note <folder> "text"` does the same. A note can be added while the recording is still running: campy keeps it when it next writes the file. On Windows, a program that has `session.json` open (a viewer, an editor) holds up a note for up to 5 seconds; a program that also stops others from writing to the file (Excel, some editors) makes the note fail with a message saying so, and the file is not changed: close that program and add the note again.

## campy-live-session.json

When `campy-live` tracks a recording, it writes `campy-live-session.json` next to `session.json` (schema `"campy-live-session/1"`). It finds the recording's folder from the live feed. A second `campy-live` on the same recording writes `campy-live-session_2.json`. If the feed doesn't say where the recording is (an older campy, or a test feed), the file goes next to campy-live's log as `<log>.session.json`. Each program writes only its own file, because two programs rewriting the same file can lose each other's changes.

It holds:

- campy-live's own environment: campy's version and commit, the computer, the GPUs and drivers, and torch with its CUDA or ROCm version, plus sleap-nn, sleap-io and TensorRT.
- `models`: for each trained model, its folder and its `id`, the first 12 hex digits of `best.ckpt`'s SHA-256, so a model is named by its weights. Also the full SHA-256s of `best.ckpt` and `training_config.yaml`, and the settings that describe the model (input scale and size, crop, backbone filters, filters rate, max stride, output stride, anchor part, sigma). It keeps the full text of the training config, and `model.json` when the model folder has one. If `model.json` gives another `id`, `idMismatch` says so (a `model.json` copied into the wrong folder).
- `calibration`: the path, its SHA-256 and the camera names used in it.
- `options`: every option's value and whether it was given on the command line or left at its default. A default that has changed says when and what it was.
- `feed`, `model` (the device, the engine that ran) and `pose3d`.
- `end`: sets tracked and skipped, inference and latency times, and the paths to its log and summary.

`campy-live`'s summary (`<log>.json`) has the path to it under `session`.

`campy-live` gathers what this file says about campy, the computer and the models while its models load: hashing `best.ckpt` and asking git, `nvidia-smi` and Windows take a second or more. It writes the file in the background once the feed appears, so tracking starts with the recording's first frames and never waits for it.

## Infinity and NaN in these files

Both files are strict JSON, so any JSON reader takes them as they are: JavaScript's `JSON.parse`, `jq`, and the JSON readers in Go, Rust or R. JSON has no way to write infinity or "not a number", so campy writes those as the text `"Infinity"`, `"-Infinity"` and `"NaN"`. Every `campy-live-session.json` has one: `--wait-sec`, whose default is to wait for the live feed as long as it takes.

```json
"--wait-sec": {"value": "Infinity", "source": "default"}
```

Others appear only when something is set that way: a campy-live option given as `inf` or `nan`, a config value written as YAML's `.inf` or `.nan` (for example in `sessionFields`), or `inf` or `nan` in a camera's `metadata.csv`. Python's `float("Infinity")` and JavaScript's `Number("Infinity")` both turn the text back into the number, so `--wait-sec` still reads as "no limit".

`campy-live`'s summary (`<log>.json`, next to its log) is written the same way: an infinite or NaN timing or frame rate in it is the same text.

campy 2.2.0rc1 and earlier wrote them as bare words (`Infinity`, without quotes), which strict JSON readers reject, refusing the whole file. Python's `json` module reads them anyway, and campy's own reader (`campy.manifest.Read`, which `campy-live-export` uses) turns them into the same text new files have, so a `.slp` made from an older recording holds valid JSON too. LUC3D reads both forms.

## In campy-live-export and LUC3D

`campy-live-export` copies `session.json` and the matching `campy-live-session.json` into the `.slp`'s provenance, with the animal IDs. It leaves the identities named "animal 1", "animal 2", …: campy-live numbers the animals in the order it first matches them, which says nothing about which animal ID each one is. When you pass `--calibration`, it checks that file against the one campy-live tracked with, and says so if their SHA-256s differ.

LUC3D reads both files when you open a campy recording's folder (Load Session Folder, or a session folder picked after File → Load SLP or Load Project):

- **Frame rate**: from `session.json`, instead of an estimate from the video. You can still type another rate.
- **Cameras**: the views appear in campy's camera order (`Camera10` after `Camera9`), each camera loads the video campy wrote for it, and without a calibration the views are named after campy's cameras.
- **A Recording section** in the Session tab: the status (with a plain explanation when it isn't complete), the experiment's name, animal IDs, notes and other fields, the notes added afterwards, the frame rate, when it started, campy's version, and each camera's serial number and video.
- **Calibration check**: it compares the calibration file it loads with the SHA-256 in `campy-live-session.json`, and warns on the status bar when campy-live's 3D was made with a different calibration.

LUC3D only shows the animal IDs and never names identities after them, for the reason above. A folder without `session.json` loads as before.

## Defaults that changed: the startup warnings

When a config leaves a setting out, campy uses its default. If a newer campy changes that default, the same config makes a different recording. So at startup, `campy-acquire` lists each changed default that this run relies on, once, as one block:

```
This run relies on 2 defaults that changed (set each in the config, or defaultsVersion: "<version>" for the defaults it was written for; defaultWarnings: False silences these):
  cameraGamma not set: since 2.1.0 the default is 0.6 (was Off). FLIR frames are brighter in the dark and middle grays than with the camera's gamma off (brightness in proportion to the light). cameraGamma: "Off" keeps the old frames.
  writeQueueMaxSec not set: since 2.1.0 the default is 5 (was None). …
```

Each warning appears only where it applies (`cameraGamma` and `cameraLinkLimitMBps` only with a FLIR camera, `fragmentedVideo` only with .mp4 or .mov videos), and goes into `session.json`'s `defaultWarnings` either way. The table is in `campy/defaults.py`:

| Setting | Changed in | Was | Now | Warned when |
|---|---|---|---|---|
| `cameraGamma` | 2.1.0 | `Off` (linear) | 0.6 | a FLIR camera |
| `cameraLinkLimitMBps` | 2.1.0 | `None` (the camera's own USB limit) | `auto` (raised to the camera's maximum when too low) | a FLIR camera |
| `writeQueueMaxSec` | 2.1.0 | `None` (no limit) | 5 s, then frames are dropped and counted | always |
| `fragmentedVideo` | 2.1.0 | `False` (plain MP4) | `True` | .mp4 or .mov videos |
| `sessionFolder` | 2.1.0 | `False` (records into `videoFolder` itself) | `True` | always |
| campy-live `--fixed-crops` | 2.1.0 | `off` | `auto` (on with a GPU) | a GPU |
| campy-live `--cuda-graphs` | 2.1.0 | `off` | `auto` (on with a GPU) | a GPU |
| campy-live `--fast-upsample` | 2.1.0 | `off` | `auto` (on with ROCm) | ROCm |
| campy-live `--engine` | the release after 2.1.0 | `eager` | `auto` (TensorRT or torch.compile) | a GPU |

There are three ways to stop them:

- **Set the setting**, in the config or on the command line. It's then your choice, not a default, and that warning stops.
- **`defaultsVersion: "2.1.0"`** in the config says it was written knowing 2.1.0's defaults, so only defaults changed after 2.1.0 are warned about. For `campy-live`, use `--defaults-version 2.1.0`.
- **`defaultWarnings: False`** in the config silences them all. For `campy-live`, use `--default-warnings off`.

A pull request that changes a default that changes what a recording holds, or how it is made, adds a row to `campy/defaults.py` and to the table above.
