# campy docs

Guides for recording with campy and tracking animals live while it records: what hardware to use, how to set up the live pipeline, how to train and choose pose models, and how to calibrate and fix a rig. The [main README](../README.md) describes every command, and the [settings reference](config-reference.md) every config key; these pages are the how-to around them.

| Page | What it covers |
|---|---|
| [Configs](configs.md) | Rig profiles (`extends:`, kept outside campy), what campy checks before any camera opens, and `campy-config show`, `diff` and `check` |
| [Settings reference](config-reference.md) | Every config key: its default, what it takes, and what it does (generated from `campy/configschema.py`) |
| [Hardware](hardware.md) | What to buy for live recording + SLEAP + 3D, and the measurements behind it |
| [Live tracking](live-tracking.md) | Setting up and running `campy-acquire`, `campy-live` and `campy-live-view` on Linux (AMD/ROCm) and Windows (NVIDIA/CUDA), and opening the results in LUC3D (`campy-live-export`) |
| [Models](models.md) | Training lightweight sleap-nn models for live use, choosing one that keeps up, finding the frames a model gets wrong on a rig (`campy-live-flag`), and labelling them in LUC3D for the next model (`campy-label-pack`, `campy-label-merge`) |
| [Offline inference](offline-inference.md) | Running SLEAP on recorded videos 6–15× faster than `sleap-nn track`: where its time goes, what helps on cluster GPUs and benchtop PCs, and the accuracy |
| [Recording folders](recordings.md) | What a recording folder holds, `session.json` (how the recording was made), the experiment's details and notes, and the warnings for defaults that changed |
| [Calibration and troubleshooting](calibration-and-troubleshooting.md) | Camera placement, exposure and calibration, and fixes for the problems seen so far |

## Keeping these pages current

These pages change in the same pull request as the code or the measurement they describe. A pull request that changes a default, a command, a measured number or a recommendation stated here also updates the page that states it, so the two can't drift apart. Measurements made outside a code change (a new test on a rig) get a docs pull request of their own.

The hardware guide is the one HTML page here, [hardware-guide.html](hardware-guide.html), because it has a chart and styled tables. It is the guide's only source: the published copy is made from it. Its footer says which campy pull request it is current up to.
