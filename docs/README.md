# campy docs

Guides for recording with campy and tracking animals live while it records. The [main README](../README.md) is the short version, with every command and the page that explains it; the [settings reference](config-reference.md) lists every config key.

| Page | What it covers |
|---|---|
| [Quickstart](quickstart.md) | A tracked recording in 15 minutes, with no camera: install, record three emulated cameras, track five mice in 3D with campy-live, open the result in LUC3D |
| [Installation](install.md) | campy and the camera makers' software (Basler Pylon, FLIR Spinnaker and PySpin), and Spinnaker 4.4 on Windows when its installer fails |
| [Configs](configs.md) | Rig profiles (`extends:`, kept outside campy), what campy checks before any camera opens, and `campy-config show`, `diff` and `check` |
| [Settings reference](config-reference.md) | Every config key: its default, what it takes, and what it does (generated from `campy/configschema.py`) |
| [Recording with campy-acquire](acquire.md) | Starting and stopping a recording, what happens when a camera or its encoder fails, video that survives a crash (`fragmentedVideo`), keeping the computer awake (`keepAwake`), and emulated cameras (`cameraMake: "emu"`) |
| [Recording folders](recordings.md) | What a recording folder holds, `session.json` (how the recording was made), the experiment's details and notes, and the warnings for defaults that changed |
| [Alerts](alerts.md) | Slack direct messages and a healthchecks.io heartbeat for unattended recordings: setting them up, and how the messages behave |
| [Video encoding](encoding.md) | NVIDIA NVENC, Intel Quick Sync, VA-API on Linux, full-range video, H.265 on a Mac, how many cameras an encoder keeps up with (`campy-encode-sweep`, `campy-encode-quality`, the launch check), and building ffmpeg |
| [FLIR cameras](flir.md) | Cameras left at 43 MB/s by a restart (`campy-flir-reset`), Spinnaker's search for GigE cameras, gamma, and the frame rate, frame size and USB limit campy sets |
| [Triggering and synchronization](sync.md) | Arduino, Teensy and external triggers, `campy-align` and `campy-synctest` |
| [RatCam cameras](ratcam.md) | nir-eye-cam's NIR eye cameras, alongside the others and on the same timeline |
| [Hardware](hardware.md) | What to buy for live recording + SLEAP + 3D, and the measurements behind it |
| [Live tracking](live-tracking.md) | Setting up and running `campy-acquire`, `campy-live` and `campy-live-view` (in one command with `campy-session`) on Linux (AMD/ROCm) and Windows (NVIDIA/CUDA), and opening the results in LUC3D (`campy-live-export`) |
| [Watching it live](live-view.md) | `campy-live-view`: the cameras with the tracking drawn on them, a 3D view, warnings at a glance, bookmarks |
| [3D poses](pose3d.md) | How `campy/pose3d.py` matches animals across cameras, keeps their identities and triangulates them, and how it was validated |
| [Models](models.md) | Training lightweight sleap-nn models for live use, choosing one that keeps up, finding the frames a model gets wrong on a rig (`campy-live-flag`), labelling them in LUC3D for the next model (`campy-label-pack`, `campy-label-merge`), and measuring models against a rig's labelled test sets (`campy-model-eval`, `campy-model-fit`) |
| [Closed-loop outputs](closed-loop.md) | `campy-rules`: TTL outputs from the live poses (regions, distances between animals, identities), photometry pulse trains locked to the frames, the outputs board, safety and latency |
| [Offline inference](offline-inference.md) | Running SLEAP on recorded videos 6–15× faster than `sleap-nn track`: where its time goes, what helps on cluster GPUs and benchtop PCs, and the accuracy |
| [Calibration](calibration.md) | Camera placement, exposure and calibration |
| [Troubleshooting](troubleshooting.md) | Problems seen so far, by what you see: the message campy prints, or what is wrong with the recording, with what to do and where to read more |
| [Development](development.md) | Running the tests, timing tests on a shared machine (`bench/rig.py`), branches and releases |

## Keeping these pages current

These pages change in the same pull request as the code or the measurement they describe. A pull request that changes a default, a command, a measured number or a recommendation stated here also updates the page that states it, so the two can't drift apart. Measurements made outside a code change (a new test on a rig) get a docs pull request of their own.

The hardware guide is the one HTML page here, [hardware-guide.html](hardware-guide.html), because it has a chart and styled tables. It is the guide's only source: the published copy is made from it. Its footer says which campy pull request it is current up to.
