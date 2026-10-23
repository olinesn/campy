# campy docs

Guides for recording with campy and tracking animals live while it records: what hardware to use, how to set up the live pipeline, how to train and choose pose models, and how to calibrate and fix a rig. The [main README](../README.md) is the reference for every config key and command; these pages are the how-to around it.

| Page | What it covers |
|---|---|
| [Hardware](hardware.md) | What to buy for live recording + SLEAP + 3D, and the measurements behind it |
| [Live tracking](live-tracking.md) | Setting up and running `campy-acquire`, `campy-live` and `campy-live-view` on Linux (AMD/ROCm) and Windows (NVIDIA/CUDA), and opening the results in LUC3D (`campy-live-export`) |
| [Models](models.md) | Training lightweight sleap-nn models for live use, and choosing one that keeps up |
| [Offline inference](offline-inference.md) | Running SLEAP on recorded videos 6–15× faster than `sleap-nn track`: where its time goes, what helps on cluster GPUs and benchtop PCs, and the accuracy |
| [Calibration and troubleshooting](calibration-and-troubleshooting.md) | Camera placement, exposure and calibration, and fixes for the problems seen so far |

## Keeping these pages current

These pages change in the same pull request as the code or the measurement they describe. A pull request that changes a default, a command, a measured number or a recommendation stated here also updates the page that states it, so the two can't drift apart. Measurements made outside a code change (a new test on a rig) get a docs pull request of their own.

The hardware guide is the one HTML page here, [hardware-guide.html](hardware-guide.html), because it has a chart and styled tables. It is the guide's only source: the published copy is made from it. Its footer says which campy pull request it is current up to.
