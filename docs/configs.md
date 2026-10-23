# Configs

A campy config is a YAML file of `key: value` lines, one per setting. Every key it can have, with its default and what it takes, is in the [settings reference](config-reference.md). A key the file leaves out takes campy's default. Any key can also be changed for one run on the command line, `--key value`, which means what `key: value` means in the file (see the [Recording with campy-acquire](acquire.md#start-recording)).

## Configuration
- For Basler cameras, use the Pylon Viewer to configure and save your '.pfs' camera settings file. Examples are included in campy/configs.
- A Basler camera's video has the frame size its '.pfs' file gives the camera, and an emulated camera's (`cameraMake: "emu"`) its clip's. campy says so when that is not `frameWidth` x `frameHeight` (`Camera0: frameWidth x frameHeight 1152x1024 changed to 1280x1024, the camera's frame size with its cameraSettings.`). Each video opens once its camera is open: it used to open first, at `frameWidth` x `frameHeight`, and frames of another size scrambled it. A camera that fails to open gets no video.
- Edit the config.yaml file to fit your system and recording configuration.
- Several example config files are located in campy/configs.
- Every key, its default and what it takes: [docs/config-reference.md](config-reference.md), or `campy-acquire --help`.
- campy checks the whole config before any camera opens, and lists every problem with its file and line: an unknown key (with the key you probably meant), a value of the wrong kind or out of range, a key given twice, a per-camera list of the wrong length. To check configs without recording:
```
campy-config check configs/campy_config_chronicrecord.yaml my_rig.yaml
```
- [docs/configs.md](configs.md) explains the checks. In campy 2.1.0 and older, a value out of range was replaced by its default (`frameRate: 0` recorded at 100 fps, `cameraGain: 0` at gain 1); now it stops campy, and 0 is a gain and a quality campy takes.
- The experiment's details go into the recording's "session.json" (below): `experiment` (its name), `animalIds` (the animals recorded, one list for the whole rig: `animalIds: [M1, M2, M3]`), `notes`, and `sessionFields`, a mapping for anything else (`sessionFields: {cage: B3, condition: saline}`). Each is also a command-line option (`--animalIds "[M4, M7]" --notes "after surgery"`). campy does not ask for them at startup. A note after the recording: `campy-session note <recording folder> "text"` (or `python -m campy.manifest note <folder> "text"`).
- When a config leaves out a setting whose default changed in a newer campy, `campy-acquire` says so once at startup, in plain words: `cameraGamma not set: since 2.1.0 the default is 0.6 (was Off). ...`. Each is said only where it applies (cameraGamma with a FLIR camera). Setting the key stops it; `defaultsVersion: "2.1.0"` says the config was written for 2.1.0's defaults, so only later changes are said; `defaultWarnings: False` stops them all. The table of changed defaults is `campy/defaults.py`, listed in [Recording folders](recordings.md#defaults-that-changed-the-startup-warnings).

## One value, or one per camera

Most settings take one value for every camera, or a list with one value per camera, in camera order:

```yaml
numCams: 3
cameraGain: [4, 3, 7]   # Camera0 4 dB, Camera1 3 dB, Camera2 7 dB
quality: 26             # every camera
```

A one-item list is the same as one value. A list longer than `numCams` uses its first `numCams` items, so a config written for 8 cameras also runs 4, and campy says which lists it shortened. A list with 2 or more items but fewer than `numCams` stops campy. `cameraNames` and `cameraSelection` need a value for every camera.

A few settings take one value for the whole rig and refuse a list: `numCams`, `recTimeInSec`, `keepAwake`, `liveFeedSlots`, the launch check's, and the trigger's. The reference's "Per camera" column says which.

## What campy checks before any camera opens

`campy-acquire` reads the whole config and the command line first. If anything is wrong, it lists every problem, each with the file and line, and stops before it opens a camera, starts ffmpeg or makes a folder:

```
$ campy-acquire my_rig.yaml
The config has 3 problems:
  my_rig.yaml:12: unknown key "framerate". Did you mean "frameRate"?
  my_rig.yaml:20: quality: '26' is not a whole number, or a list of them
  my_rig.yaml:31: codec: 'x265' is not "h264", "h265" or "av1". Did you mean "h265"?
```

It checks:

- **Unknown keys.** campy suggests the key you probably meant: the same name in other capitals (`framerate`), one a few letters away (`cameraExposureTimeUs`), or the only key that contains the word (`exposure` is `cameraExposureTimeInUs`). An unknown command-line option stops it the same way, `unknown option --framerate. Did you mean --frameRate?`. A shortened option is not taken: `--qual 20` used to mean `--quality 20`.
- **Kinds.** A number, True or False, or text, as the reference says. This used to be checked only on the command line: `quality: "26"` in the file got through and failed later. YAML reads `yes`, `no`, `on` and `off` as True or False, and an empty value as nothing, and the message says so when that is the cause.
- **Ranges and words.** For example, `frameRate` above 0, `numCams` 1 or more, `cameraGain` 0 or more, `quality` 0 to 51 (0 to 255 for `codec: av1`). Settings with a fixed list of words take only those: `codec`, `gpuMake`, `cameraMake`, `bufferMode`, `colorRange`, `ffmpegLogLevel`, `preflight`, `triggerController`. Limits that depend on the camera model are left to the camera: for example, the highest gain a camera takes, and the gamma it clamps to.
- **A key given twice in one file.** YAML would keep the last value without a word.
- **Per-camera lists** of the wrong length (above), and the checks across settings that were there before: one frame rate for the cameras a trigger drives, `colorRange`, `cameraGamma`.
- **Renamed keys.** A config that still says `disableGamma` is read as `cameraGamma`: True as `"Off"`, False as 0.6. campy says so as it starts. A config that gives both is refused.

**In campy 2.1.0 and older**, a value out of range was replaced by the default, with a message among the startup lines. `frameRate: 0` recorded at 100 fps. `cameraGain: 0` and `quality: 0` counted as out of range, so they recorded at gain 1 and quality 21. Now an out-of-range value stops campy, and `cameraGain: 0` and `quality: 0` mean 0. Every example config in `configs/` reads as it did, and a test checks that.

## Checking a config without recording: campy-config check

```
campy-config check configs/campy_config_evox2.yaml my_rig.yaml
```

This reads each config as `campy-acquire` would and prints `OK` or `FAIL` for each one, with every problem and every note, such as a renamed key or a shortened list. It opens no camera, encoder or trigger. Its exit status is 1 if any config has a problem, for scripts and release checks.

`campy-encode-sweep --config` and `campy-encode-quality --config` check their config the same way.

## The settings reference is generated

Every setting is described once, in `campy/configschema.py`: its default, its kind, its range or words, whether it takes a list, and its help. `DefaultParams()`, `campy-acquire`'s options and their `--help`, and [config-reference.md](config-reference.md) are all made from that table, so they can't disagree. After changing a setting there, regenerate the page:

```
campy-config reference > docs/config-reference.md
```

A test fails when the page and the table differ.
