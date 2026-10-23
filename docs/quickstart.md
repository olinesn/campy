# Quickstart: a tracked recording in 15 minutes

This page takes you from nothing installed to a recording of five mice, tracked in 3D while it recorded, open in LUC3D. It needs no camera, no trigger and no GPU. Three *emulated cameras* play short videos as if they were live cameras, and campy records them exactly as it records real ones. A small pose model, a calibration and the videos come in one download.

You'll run three programs:

- **`campy-acquire`** records the cameras and copies every frame into shared memory, the *live feed*.
- **`campy-live`** reads the live feed, finds each mouse's body parts in every camera, and puts them together in 3D.
- **`campy-live-export`** turns the result into files that [LUC3D](https://luc3d.sleap.ai) opens.

Each step below is one section, so you can skip the ones you've done.

Tested on a Mac with Apple silicon on 2026-10-23: the Mac's commands in steps 1 to 5, as written, in new conda environments, with campy 2.1.0 (`master`). LUC3D opened the export (a local copy of LUC3D, driven by its own browser tests). The Windows and Linux install commands are the ones the test rigs were set up with ([Live tracking](live-tracking.md#two-python-environments)), and the Windows download commands haven't been run yet.

## 1. Install campy (about 5 minutes)

campy uses two Python environments: one records, with the camera drivers, and one tracks, with PyTorch and sleap-nn. They are kept apart so that neither's packages can break the other's.

You need [conda](https://conda-forge.org/download/) (Miniforge, Miniconda or Anaconda) and [git](https://git-scm.com/downloads).

**The recording environment**, on every system:
```
conda create -n campy python=3.12 imageio-ffmpeg ffmpeg=8.1 -c conda-forge
conda activate campy
git clone -b master https://github.com/olinesn/campy.git
cd campy
pip install -e .
```
That's all the quickstart needs. For real cameras you'll also install the camera maker's software ([Installation](install.md#installation)).

**The tracking environment.** In a new terminal, from the same `campy` folder:
```
conda create -n campy-live python=3.12 -c conda-forge
conda activate campy-live
```
Then install PyTorch for your computer, sleap-nn and the viewer's packages:

- **Mac** (Apple silicon; PyTorch uses the Mac's GPU):
  ```
  pip install torch "sleap-nn==0.3.3" opencv-python PyQt5
  ```
- **Windows, NVIDIA GPU**: PyTorch's CUDA build first, then the rest:
  ```
  pip install torch --index-url https://download.pytorch.org/whl/cu128
  pip install "sleap-nn==0.3.3" opencv-python PyQt5
  ```
- **Linux, NVIDIA GPU**: PyTorch from PyPI is already the CUDA build:
  ```
  pip install torch "sleap-nn==0.3.3" opencv-python PyQt5
  ```
- **Linux, AMD GPU**: a ROCm build of PyTorch for your GPU first ([Live tracking](live-tracking.md#two-python-environments)), then `pip install "sleap-nn==0.3.3" opencv-python PyQt5`.
- **No GPU**: as on Linux. It works, but tracks only a few sets of frames a second.

Last, install campy into it without its dependencies, so it doesn't need the camera drivers:
```
pip install --no-deps -e .
```

On the Mac, installing both took 2–4 minutes, most of it downloading PyTorch.

## 2. Get the quickstart data (83 MB)

From the folder that holds your `campy` folder (not inside it):

- **Mac and Linux**:
  ```
  curl -LO https://github.com/olinesn/campy/releases/download/v2.1.0/campy-quickstart-1.zip
  unzip campy-quickstart-1.zip
  cd campy-quickstart
  ```
- **Windows** (PowerShell):
  ```
  curl.exe -LO https://github.com/olinesn/campy/releases/download/v2.1.0/campy-quickstart-1.zip
  tar -xf campy-quickstart-1.zip
  cd campy-quickstart
  ```

The folder holds:

| | |
|---|---|
| `Camera0/clip.mp4`, `Camera1/clip.mp4`, `Camera2/clip.mp4` | 20 s of five mice, filmed by three cameras at once (1280 × 1024, grey, 30 fps) |
| `calibration.toml` | where the three cameras are, so the mice can be put together in 3D |
| `models/centroid`, `models/centered_instance` | a small two-part sleap-nn model fast enough to track live ([Models](models.md)) |
| `campy_config_quickstart.yaml` | campy's settings for the three emulated cameras |
| `README.txt` | what's in the folder |

Run everything below from inside `campy-quickstart`.

## 3. Start campy-live

In the terminal with the **`campy-live`** environment active:
```
campy-live --cameras 3 --model sleap --centroid models/centroid --instance models/centered_instance --animals 5 --calibration calibration.toml
```
It loads the model, warms it up, and prints:
```
Waiting for the live feed of campy-live-0, campy-live-1, campy-live-2 (campy-acquire with liveFeed True)...
```
Leave it waiting. It took 12 s on the Mac. On a computer with an NVIDIA or AMD GPU, the first start also builds a faster, compiled copy of the model, which takes up to about 40 s once ([Live tracking](live-tracking.md#compiled-networks---engine-on-by-default-with-a-gpu)).

Starting it first means it tracks the recording from its first frame.

## 4. Record

In the terminal with the **`campy`** environment active, also inside `campy-quickstart`:
```
campy-acquire campy_config_quickstart.yaml
```
campy first runs its launch check (one `WARN` about a missing encoder capacity table is expected: it only matters for real rigs), then prints `Recording into …/campy-quickstart/2026-10-23_14-05-09`, a new folder named by the date and time it started. It opens the three cameras, starts them together once all are ready, records for 20 seconds, and closes each camera's video. You should see `saved 600 frames at 30 fps` for each camera.

Meanwhile `campy-live` reports every 10 seconds, and when the recording ends:
```
Done, the recording ended: 285 sets (14.2/s), 312 skipped, 0 misaligned; latency p50 84.9 p99 116.0 max 256.6 ms; inference p50 66.7 p99 88.7 ms; to 3D p50 86.3 p99 117.5 ...
```
Those are the Mac's numbers. A *set* is one frame from each camera, taken at the same moment. campy-live always works on the newest set and skips any that arrive while it is busy, so it never falls behind. The Mac needs about 67 ms per set, so it tracked about half of the 30 sets a second. With this model and five mice, the test machines' NVIDIA and AMD GPUs tracked every set, or all but a handful, at 60 fps ([Hardware](hardware.md) has the measurements). `0 misaligned` means every set's three frames were taken together.

The recording's folder now holds one folder per camera with its video (`emuclip.mp4`), its frame times and its settings. campy-live wrote its results into the folder it was started from, `campy-quickstart`: `campy-live_<date_time>.npy` and a `.json` summary.

**Optional, watch it live:** in a third terminal with the `campy-live` environment active, run `campy-live-view` before step 4. It shows each camera with the tracked mice drawn on it, and a 3D view ([live view](live-view.md#watching-it-live-campy-live-view)).

## 5. Open it in LUC3D

In the `campy-live` terminal, export the results into the recording's folder (put in your recording's folder name and campy-live's file name):
```
campy-live-export 2026-10-23_14-05-09 --log campy-live_2026-10-23_14-05-11.npy
```
It takes about a second here, and writes `campy-live.slp` into the recording's folder.

Then open [luc3d.sleap.ai](https://luc3d.sleap.ai) in Chrome or Edge (LUC3D needs their file access; in Brave it says how to turn it on):

1. **File ▸ Load SLP**, and pick `campy-live.slp` in the recording's folder.
2. LUC3D asks for the videos: click **Select Session Folder** and pick the recording's folder.

You'll see the three cameras with each mouse's body parts and its identity, the mice in 3D, and a timeline with one row per mouse and camera. LUC3D's reprojection error of campy-live's 3D poses was 2.1 px on the Mac's run. Frames that campy-live skipped have no poses; they show as gaps in the timeline.

## Your own camera

To record a real camera instead, install its maker's software into the `campy` environment ([Installation](install.md#installation), step 3), then copy `campy_config_quickstart.yaml` and change the camera lines. For one FLIR camera, running at its own pace (no trigger):
```
numCams: 1
cameraMake: "flir"
cameraNames: ["Camera0"]
cameraTrigger: "None"
cameraExposureTimeInUs: 1500
cameraGain: 1
```
Delete `videoFilename`, or set it to the video's name (`0.mp4`). Set `videoFolder` to where recordings should go. For a Basler camera, `cameraMake: "basler"` with `cameraSettings` pointing to a `.pfs` file saved from Pylon Viewer (`configs/mono8.pfs` is an example). [Configuration](configs.md#configuration) and `campy-acquire --help` list every setting, and `configs/` has examples for more cameras, GPU encoders and triggers.

The quickstart model was trained on about 1,000 frames of three mice from several camera angles ([Models](models.md#how-they-were-trained)), so expect it to do worse on a rig with other lighting, cameras or animals. The same page explains how to train a small model that keeps up live. With one camera, run `campy-live --cameras 1 --model sleap --centroid <folder> --instance <folder>` without `--calibration`: you get 2D poses, and LUC3D opens them with File ▸ Load Session Folder.

## When something goes wrong

- **`campy-live` keeps waiting** after the recording started: `liveFeed: True` must be in the config (or `--liveFeed True` on the command line), and both programs must run on the same computer.
- **Every set "misaligned"**: the cameras' frames came at different moments. With emulated cameras this means a very busy computer; close other programs.
- **`campy-live-export` says "the log's frames are not this recording's"**: the log is from another recording. Use the `.npy` that campy-live wrote during this one.

[Troubleshooting](calibration-and-troubleshooting.md#troubleshooting) covers more.
