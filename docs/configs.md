# Configs

A campy config is a YAML file of `key: value` lines, one per setting, and optionally an `extends:` line naming a [rig profile](#rig-profiles-settings-that-belong-to-the-computer-outside-campy) it builds on. Every key it can have, with its default and what it takes, is in the [settings reference](config-reference.md). A key the file leaves out takes campy's default. Any key can also be changed for one run on the command line, `--key value`, which means what `key: value` means in the file (see the [main README](../README.md#start-recording)).

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

- **Unknown keys**, in each file of an `extends:` chain. campy suggests the key you probably meant: the same name in other capitals (`framerate`), one a few letters away (`cameraExposureTimeUs`), or the only key that contains the word (`exposure` is `cameraExposureTimeInUs`). An unknown command-line option stops it the same way, `unknown option --framerate. Did you mean --frameRate?`. A shortened option is not taken: `--qual 20` used to mean `--quality 20`.
- **Kinds.** A number, True or False, or text, as the reference says. This used to be checked only on the command line: `quality: "26"` in the file got through and failed later. YAML reads `yes`, `no`, `on` and `off` as True or False, and an empty value as nothing, and the message says so when that is the cause.
- **Ranges and words.** For example, `frameRate` above 0, `numCams` 1 or more, `cameraGain` 0 or more, `quality` 0 to 51 (0 to 255 for `codec: av1`). Settings with a fixed list of words take only those: `codec`, `gpuMake`, `cameraMake`, `bufferMode`, `colorRange`, `ffmpegLogLevel`, `preflight`, `triggerController`. Limits that depend on the camera model are left to the camera: for example, the highest gain a camera takes, and the gamma it clamps to.
- **A key given twice in one file.** YAML would keep the last value without a word.
- **Per-camera lists** of the wrong length (above), and the checks across settings that were there before: one frame rate for the cameras a trigger drives, `colorRange`, `cameraGamma`.
- **Renamed keys.** A config that still says `disableGamma` is read as `cameraGamma`: True as `"Off"`, False as 0.6. campy says so as it starts. A config that gives both is refused.

**In campy 2.1.0 and older**, a value out of range was replaced by the default, with a message among the startup lines. `frameRate: 0` recorded at 100 fps. `cameraGain: 0` and `quality: 0` counted as out of range, so they recorded at gain 1 and quality 21. Now an out-of-range value stops campy, and `cameraGain: 0` and `quality: 0` mean 0. Every example config in `configs/` reads as it did, and a test checks that.

## Rig profiles: settings that belong to the computer, outside campy

Most of a config describes the rig: its cameras, its encoder, its ffmpeg, its trigger. That part rarely changes. What changes from one experiment to the next is a few lines: the frame rate, the folder, how long to record. A config can say only those few lines and take the rest from a **rig profile**:

```yaml
# C:\campy_data\configs\one_camera_80fps.yaml
extends: chronicrecord
numCams: 1
frameRate: 80
cameraExposureTimeInUs: 200
cameraGain: 7
```

```yaml
# ~/.campy/configs/chronicrecord.yaml: this computer's settings, written once
cameraMake: "flir"
frameWidth: 1280
frameHeight: 1024
gpuMake: "intel"
codec: "h265"
ffmpegPath: 'C:\ffmpeg-btbn-8.1\bin\ffmpeg.exe'
...
```

**Where profiles are.** In `~/.campy/configs/` (on Windows, `C:\Users\<you>\.campy\configs\`), next to the encoder capacity table campy keeps in `~/.campy/`. That folder is outside every campy checkout and environment, so updating, reinstalling or cloning campy again never touches a profile. The environment variable `CAMPY_CONFIG_DIR` points campy at another folder. `campy-config where` prints the folder and the profiles in it, and `campy-config new <name> --from <config>` makes a profile from an existing config (it never replaces one):

```
campy-config new evox2 --from configs/campy_config_evox2.yaml
```

Keep the rig's own settings in a profile, not as an edit of a config in `configs/`. Those are examples, which a campy update may change: an edited one makes every update stop to ask what to do with the edit.

**What `extends:` names**
- A bare name (`extends: chronicrecord`) is a profile: `<profile folder>/chronicrecord.yaml`.
- Anything with a folder or an extension is a file, from the folder of the config that names it, not the folder campy runs in: `extends: ../rigs/base.yaml`, `extends: base.yaml`, `extends: C:\campy_data\configs\base.yaml`.
- A profile can extend another file in turn, to any depth. A file that ends up extending itself is refused, and so is a name with no file, with where campy looked.

**How the files combine**
- campy's defaults come first, then the root profile, then each file that extends it, then the command line. Each one replaces the one before it, key by key.
- **A list is replaced whole.** `cameraGain: [5, 5, 5]` in the experiment replaces the profile's `[4, 3, 7]`, and `cameraGain: 6` replaces it with one gain for every camera. Lists are never merged item by item.
- **`numCams` is a setting like the others.** The list checks run on the combined result. If an experiment records fewer cameras than the profile's lists are written for, campy uses the first items and says which file the lists came from (`campy-config show --cameras` shows each camera's values). If it records more, campy stops and names the file of the list and the file of `numCams`.
- Each file is checked on its own lines, and its renamed keys are read in that file. A profile's `disableGamma: True` is its `cameraGamma: "Off"`, so an experiment's `cameraGamma: 0.6` replaces it.
- Relative paths in a setting (`videoFolder: ./test`, `cameraSettings`) are still from the folder campy runs in, as before.

## Where each setting came from: campy-config show

```
$ campy-config show one_camera_80fps.yaml --recTimeInSec 60 --changed
chain: one_camera_80fps.yaml -> ~/.campy/configs/chronicrecord.yaml -> campy's defaults

numCams                 1                                  one_camera_80fps.yaml:3
videoFolder             C:\campy_data\chronicRecording     chronicrecord.yaml:9
frameRate               80                                 one_camera_80fps.yaml:4
recTimeInSec            60                                 command line
cameraExposureTimeInUs  200                                one_camera_80fps.yaml:5
cameraGamma             Off                                chronicrecord.yaml:23
codec                   h265                               chronicrecord.yaml:28
...
```

`campy-config show <config> [--key value ...]` takes the same options as `campy-acquire`, and prints every setting it would record with and where the value came from: a file and line, `command line`, or `default`. Options:
- `--changed`: only the settings that are not campy's defaults.
- `--cameras`: one column per camera, for the per-camera settings.
- `--yaml`: the whole config as one file with no `extends:`. Each line says where its value came from, and `campy-acquire` records with it exactly as with the original.
- `--json`: the same values and sources, for programs.

**Moving to profiles without changing a recording: campy-config diff.** `campy-config diff old.yaml new.yaml` lists every setting whose value differs between two configs, each with where it came from. It also takes campy-acquire's options, applied to both. When a standalone config is rewritten as a profile plus a short experiment file, "No difference" proves they record the same.

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
