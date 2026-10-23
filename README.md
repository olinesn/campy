# campy
- Python package for acquiring synchronized video from multiple cameras with real-time compression

campy records many machine-vision cameras at once (FLIR, Basler, nir-eye-cam's RatCams, or videos standing in for cameras), each into its own compressed video, encoded on the GPU (NVIDIA, Intel, AMD) or the CPU as it records. It triggers the cameras together from an Arduino or Teensy, or waits for an external trigger, and puts every camera's frames on the trigger's timeline afterwards. Since 2.1.0 it can also track animals while it records: `campy-live` runs a SLEAP model on every set of frames, matches the animals across cameras and triangulates them in 3D, and the result opens in LUC3D.

**New to campy?** [The quickstart](docs/quickstart.md) gets you from nothing installed to a recording of five mice, tracked in 3D while it recorded and open in LUC3D, in about 15 minutes, with no camera. [docs/](docs/README.md) has the other guides.

## Install

A conda environment with Python 3.12 and ffmpeg, then campy from this repository:
```
conda create -n campy python=3.12 imageio-ffmpeg ffmpeg=8.1 -c conda-forge
conda activate campy
git clone -b master https://github.com/olinesn/campy.git
cd campy
pip install -e .
```
Each camera maker's software (Spinnaker and PySpin for FLIR, Pylon and pypylon for Basler), GPU encoding and its drivers: [Installation](docs/install.md). Live tracking runs in a second environment, with PyTorch and sleap-nn: [Live tracking](docs/live-tracking.md#two-python-environments).

## Record

```
campy-acquire ./configs/campy_config.yaml
```
Each run records into a new folder inside the config's `videoFolder`, named by the date and time it started, with one folder per camera. It stops after `recTimeInSec`, or at Ctrl-C (wait for campy to close every video). Before any camera opens, campy checks the config and the computer (encoders, USB, cameras, disk) and says what is wrong. `configs/` has example configs, `campy-acquire --help` lists every setting, and the [settings reference](docs/config-reference.md) says what each one does. [Recording with campy-acquire](docs/acquire.md) explains starting, stopping and what happens when something fails, and [Recording folders](docs/recordings.md) what a recording holds.

## Commands

| Command | What it does | Guide |
|---|---|---|
| `campy-acquire` | Record the cameras | [Recording](docs/acquire.md) |
| `campy-notify` | Send problems as Slack direct messages, and keep a healthchecks.io heartbeat for unattended recordings | [Alerts](docs/alerts.md) |
| `campy-config` | Check, show and compare configs without recording (`check`, `show`, `diff`, `where`, `new`); rig profiles | [Configs](docs/configs.md) |
| `campy-encode-sweep`, `campy-encode-quality` | Measure how many cameras each encoder keeps up with, and what each quality costs | [Video encoding](docs/encoding.md#scaling-the-number-of-cameras) |
| `campy-flir-reset` | Reset FLIR cameras a restart left at USB 2 speed; turn Spinnaker's GigE search off | [FLIR cameras](docs/flir.md) |
| `campy-align`, `campy-synctest` | Put every camera on the trigger's timeline; check that cameras expose together | [Triggering and synchronization](docs/sync.md) |
| `campy-session` | A tracked recording in one command (`run`), from a page in the browser (`panel`), and what to do after a clean stop (`post`: remux, export, copy) | [Live tracking](docs/live-tracking.md#in-one-command-campy-session) |
| `campy-live` | Track animals live, in 2D and 3D, while campy records | [Live tracking](docs/live-tracking.md) |
| `campy-live-view` | Watch the cameras and the tracking live | [Watching it live](docs/live-view.md) |
| `campy-live-export` | Open a tracked recording in LUC3D | [Live tracking](docs/live-tracking.md#opening-a-tracked-recording-in-luc3d) |
| `campy-live-flag`, `campy-label-pack`, `campy-label-merge`, `campy-model-eval`, `campy-model-fit` | Find the frames a model gets wrong, label them for the next model, and compare models on a rig | [Models](docs/models.md) |
| `campy-infer` | SLEAP predictions on recorded videos, about 10 times faster than `sleap-nn track`, as ordinary `.slp` files | [Offline inference](docs/offline-inference.md#poses-from-recorded-videos-campy-infer) |
| `campy-rules` | Switch TTL outputs from the live poses (an animal in a region, two animals close), for closed-loop experiments | [Closed-loop outputs](docs/closed-loop.md) |

## Documentation

| Page | What it covers |
|---|---|
| [Quickstart](docs/quickstart.md) | A tracked recording in 15 minutes, with no camera |
| [Recording a session from the desktop](docs/recording-a-session.md) | For labmates who don't use a terminal: the desktop icon and the panel in the browser |
| [Installation](docs/install.md) | campy, the camera makers' software, Spinnaker on Windows when its installer fails |
| [Configs](docs/configs.md) and [Settings reference](docs/config-reference.md) | How campy reads a config, and every key |
| [Recording with campy-acquire](docs/acquire.md) | Starting and stopping, failures, video that survives a crash, keeping the computer awake, emulated cameras |
| [Recording folders](docs/recordings.md) | What a recording holds, and `session.json` |
| [Alerts](docs/alerts.md) | Slack direct messages and a healthchecks.io heartbeat for unattended recordings |
| [Video encoding](docs/encoding.md) | NVENC, Quick Sync, VA-API, colour range, H.265 on a Mac, how many cameras an encoder keeps up with, building ffmpeg |
| [FLIR cameras](docs/flir.md) | 43 MB/s after a restart, GigE search, gamma, frame rate, frame size and USB limit |
| [Triggering and synchronization](docs/sync.md) | Arduino and external triggers, `campy-align`, `campy-synctest` |
| [RatCam cameras](docs/ratcam.md) | nir-eye-cam's NIR eye cameras |
| [Live tracking](docs/live-tracking.md), [Watching it live](docs/live-view.md), [3D poses](docs/pose3d.md) | `campy-live`, `campy-live-view`, `campy-live-export`, and the 3D tracker |
| [Closed-loop outputs](docs/closed-loop.md) | `campy-rules`: TTL outputs on an Arduino from the live poses, with the wiring, the rules file, safety and latency |
| [Models](docs/models.md), [Offline inference](docs/offline-inference.md) | Small models that keep up live; running SLEAP faster on recorded videos |
| [Hardware](docs/hardware.md) | What to buy, with the measurements behind it |
| [Calibration and troubleshooting](docs/calibration-and-troubleshooting.md) | Camera placement, calibration, and fixes for the problems seen so far |
| [Development](docs/development.md) | Tests, timing tests on a shared machine, branches and releases |

## This fork

This repository is a fork of [ksseverson57/campy](https://github.com/ksseverson57/campy), from its `master` of November 2025, with about 380 commits since. Several defaults changed on the way, so a config written for the original campy records differently here: each run goes into a folder of its own (`sessionFolder`), FLIR cameras get gamma 0.6 (`cameraGamma`) and a USB limit raised when it is too low (`cameraLinkLimitMBps`), videos are written in fragments that survive a crash (`fragmentedVideo`), and an encoder that falls 5 s behind drops frames instead of filling memory (`writeQueueMaxSec`). campy says so at startup for each of these that applies to the config; [Recording folders](docs/recordings.md#defaults-that-changed-the-startup-warnings) lists them, with the setting that brings back the old behaviour. campy also checks the computer before recording and stops if a check fails (`preflight: "warn"` records anyway), and never overwrites a recording.

## Authors
Written by Kyle Severson with contributions from Diego Aldarondo and Iris Odstrcil.

## Credits
Special thanks to Stefan Oline, Tim Dunn, David Hildebrand, Vincent Prevosto, Manuel Levy, and Paul Thompson for helpful comments.

## License
MIT License
