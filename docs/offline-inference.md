# Offline inference: getting poses from recorded videos faster

`campy-live` tracks animals while they are recorded. Most labs also run SLEAP afterwards on the recorded videos, with `sleap-nn track`, one video at a time, in batches. This page measures how fast that runs, where the time goes, and which of `campy-live`'s speed-ups also work there. The measurements were made on data-centre GPUs and on two benchtop PCs, with `bench/offline_infer_bench.py`. `campy-infer` runs the fastest of them for real batch jobs: see [How to run it](#how-to-run-it).

## Summary

- **`sleap-nn track` (0.3.3) leaves the GPU idle most of the time.** It passes each batch of frames to the model as a numpy array, so sleap-nn shrinks the full frames and cuts out each animal on the CPU. That is more than half of each frame's time. On an NVIDIA A100 the GPU was busy 38% of the time, and the run kept about 8 CPU cores busy. A 10-minute, 18,000-frame camera video took 3 min 51 s (78 frames/s).
- **Copying each batch to the GPU before predicting doubles the speed** (100 → 226 frames/s on the A100), with the same animals found. It is one line in sleap-nn's predict loop.
- **With channels-last memory layout, fp16 and campy's tied-peaks fix, sleap-nn's own code runs 7–8× faster** on the A100 and L40S (761–801 frames/s).
- **campy-live's fixed-shape crops with TensorRT run 11–15× faster** (1,135 frames/s on the A100, 1,524 on the L40S). The whole 10-minute video takes 25 s on the A100, including loading the models and writing the `.slp`.
- **On a benchtop PC the same approach gives 6–7×** (about 450 frames/s, a 10-minute camera video in 40 s), on both the GMKtec EVO-X2 (AMD, `torch.compile`) and the Dell Pro Max Slim FCS1250 (NVIDIA RTX 2000 Ada, TensorRT). **It runs just as fast while the PC records 3 cameras at 60 fps, and the recording dropped no frames.**
- Accuracy stays within the differences sleap-nn already has. sleap-nn's own output moves 2% of keypoints by more than a pixel because of its crop-size rounding bug, and fp16 with the tied-peaks fix moves 1.5–3.4%. fp16 *without* that fix loses 0.8% of the animals.

## The test

- **Video:** one camera of a 5-mouse recording, 1280×1024 gray, HEVC, 30 fps, 10 minutes (18,000 frames).
- **Models:** the live pair from [Models](models.md): a centroid model at 0.25 scale (output stride 2) and a centered-instance model with 320 px crops at 0.5 scale, max stride 32, 24 filters. The "larger model" rows use the same centered-instance model at full scale (4× the pixels).
- **Settings:** as the lab's batch jobs: at most 5 animals, peak threshold 0.2, batches of 16, fp32.
- **Machines:**
  - an NVIDIA A100 40 GB and an L40S 46 GB in a cluster, with sleap-nn 0.3.3 and PyTorch 2.6;
  - the GMKtec EVO-X2: AMD Ryzen AI Max+ 395 with its built-in GPU, Linux, ROCm, PyTorch 2.14;
  - the Dell Pro Max Slim FCS1250: Intel Core Ultra 9 285 and NVIDIA RTX 2000 Ada, Windows 11, PyTorch 2.11.
- Each number is frames per second over 6,000 frames after a short warm-up. It counts decoding the video, the models and copying the results back. On the two benchtop PCs every run held the rig lock, and nothing else used the GPU, encoder or cameras.

## Where the time goes

Milliseconds per frame on the A100, each stage timed separately, batches of 16:

| | Shrink frames | Cut crops | Centroid network | Instance network | Peaks, other | Total |
|---|---:|---:|---:|---:|---:|---:|
| sleap-nn as-is (frames on the CPU) | 3.79 | 1.42 | 0.51 | 3.35 | 0.92 | 10.0 |
| batch copied to the GPU first | 0.04 | 0.07 | 0.49 | 3.35 | 0.66 | 4.6 |
| + channels-last, fp16, tied peaks | 0.04 | 0.07 | 0.24 | 0.64 | 0.63 | 1.6 |
| campy fixed crops + TensorRT | one engine pass from the uploaded frames to the results ||||| 0.5 |

## One change at a time

A100, frames per second; each row adds one change to the row above:

| Change | frames/s | × as-is | What limits it next |
|---|---:|---:|---|
| sleap-nn as-is | 100 | 1.0 | the CPU (resizing and cropping) |
| copy each batch to the GPU before `layer.predict` | 226 | 2.3 | the GPU: fp32 networks |
| channels-last memory layout | 493 | 4.9 | the GPU |
| fp16 with the tied-peaks fix | 640 | 6.4 | the GPU, and the steps between the two networks |
| batches of 64 | 801 | 8.0 | decoding the video on the CPU |
| campy fixed crops + TensorRT + GPU video decoding (NVDEC) | 1,135 | 11.4 | handing frames from the decoder to the model |

Channels-last matters on NVIDIA: its fast convolution kernels want the channels last, and sleap-nn's models are channels-first, so every layer was converted back and forth. On AMD (ROCm) it made no difference. Bigger batches only help once the networks are fast: in fp32 without channels-last, batch 64 was slower (190 frames/s).

Tried, and didn't help:

| | frames/s | Why |
|---|---:|---|
| sleap-nn's own `use_compile` option | 95 | it compiles again for every new number of animals in a batch |
| `torch.compile` of the fixed crops on PyTorch 2.6 (A100) | 555 | slower than eager channels-last (691); it also needs sleap-nn's pool padding computed in plain Python (below) |
| CUDA graphs | 262 vs 257 | at these batch sizes there are few kernel launches to save |
| the video on the node's local disk | 95 vs 100 | reading the file was never the limit |
| several decoder processes | 273–366 | moving whole frames between processes costs more than decoding them |

## Every machine

Frames per second. "Small changes" means sleap-nn's own layer with the batch on the GPU, fp16 with tied peaks, and channels-last on NVIDIA. "Fastest" is campy's fixed crops with TensorRT on NVIDIA, or `torch.compile` on AMD.

| Machine | sleap-nn as-is | small changes | fastest | while recording 3 cameras (as-is / fastest) |
|---|---:|---:|---:|---|
| NVIDIA A100 40 GB | 100 | 801 | 1,135 | – |
| NVIDIA L40S 46 GB | 98 | 761 | 1,524 | – |
| A100, larger model | 98 | 317 | 540 | – |
| GMKtec EVO-X2 (AMD) | 68 | 186 | 453 | 62 / 433, all 3 × 14,400 frames saved |
| Dell Pro Max Slim FCS1250 (RTX 2000 Ada) | 67 | 199 | 447 | 61 / 447, all 3 × 14,400 frames saved |

On the benchtop PCs, `campy-acquire` recorded 3 emulated cameras at 60 fps for 4 minutes while the offline runs went: VA-API H.264 on the EVO-X2, Quick Sync H.265 on the Pro Max Slim. Each camera's file had all 14,400 frames. sleap-nn as-is kept 14–16 CPU threads busy there; the fast paths used about one.

**On AMD, a first run is much slower.** ROCm's MIOpen picks its convolution kernels the first time it sees each input shape, and saves its choices for later. In sleap-nn's own layer, the number of crops changes from batch to batch, so a first run keeps meeting new shapes. On the EVO-X2, sleap-nn as-is ran at 19 frames/s the first time (68 after), and fp16 at batch 64 at 10 frames/s (175 after). campy's fixed crops have one shape per batch size, so they pay this once. The table shows the second runs.

With the larger model the networks are the limit everywhere, and TensorRT matters most: 98 → 540 frames/s on the A100, against 317 for sleap-nn's layer with the small changes.

## Reading the video

Once the model is fast, decoding sets the pace. Gray frames per second from the HEVC video, with nothing else running:

| Machine | sleap-io (sleap-nn's reader) | OpenCV | PyAV or ffmpeg (CPU, threaded) | GPU decoder |
|---|---:|---:|---:|---:|
| A100 node | 247 | 587 | 773 | 1,506 (NVDEC) |
| L40S node | – | – | 615 | 2,702 (NVDEC) |
| GMKtec EVO-X2 | 722 | 1,296 | 1,487 | 1,826 (VA-API) |
| Dell Pro Max Slim FCS1250 | 272 | 415 | 702 | 686 (NVDEC through ffmpeg) |

sleap-io's reader varied from run to run on network storage (12–480 frames/s). It checks that the file still exists, opening it again, for every frame it reads. A GPU decoder keeps up with every model path here using under one CPU core. On the benchtop PCs, ffmpeg on the CPU is already faster than the model. PyAV could not run in the same process as PyTorch and OpenCV: it hung on its first frames while holding Python's interpreter lock. The bench script runs it in a process of its own and hands frames over through shared memory.

## Accuracy

Each run against an exact reference: fp32, every crop cut at its true size. A100, 6,000 frames, 29,322 mice. Mice are matched frame by frame by their centroids.

| Setup | Mice found in only one run | Keypoints moved > 1 px |
|---|---:|---:|
| sleap-nn as-is | 0 | 2.07% |
| campy fixed crops, fp32 | 0 | 0.01% |
| fp32 on another GPU (RTX 2000 Ada, EVO-X2), same settings | 18–23 | 0.65% |
| fp16 without the tied-peaks fix | 235 lost | 2.9% |
| fp16 + tied peaks, fixed crops | 26 | 1.5% |
| fp16 + tied peaks + channels-last, sleap-nn's layer (with its crop bug) | 26 | 3.5% |
| TensorRT fp16 (A100 / RTX 2000 Ada) | 55 / 51 | 3.3% / 3.4% |
| `torch.compile` fp16, batch 64 (EVO-X2) | 47 | 2.5% |

sleap-nn as-is moves 2% of keypoints because of its crop-size rounding bug ([Live tracking](live-tracking.md), `ExactCropSize`). The first animal in a batch sets the crop size for every crop in it, and that size can come out a pixel short, so sleap-nn's results also change with the batch size: batch 64 against batch 16 moved 3.2% of keypoints. Frames copied to the GPU first gave the same mice as sleap-nn as-is, with 90% of keypoints identical to the bit. The 0.8% of mice that fp16 loses without the tied-peaks fix are the ones whose peak is two pixels of equal value ([Live tracking](live-tracking.md)).

## What to use

1. **Copy frames to the GPU before predicting.** It's the cheapest change: 2.2× with no new packages.
2. **On NVIDIA, run channels-last and fp16, and use fp16 only with the tied-peaks fix.** 7–8× on the data-centre GPUs, in sleap-nn's own code.
3. **Fix the crop-size rounding.** It costs no time, and the results stop depending on the batch size.
4. **For the most speed, use campy's fixed crops with a compiled engine** (TensorRT on NVIDIA, `torch.compile` on AMD) and a GPU video decoder: 11–15× on data-centre GPUs, 6–7× on a benchtop PC. On AMD it also avoids slow first runs.
5. **Skip** sleap-nn's `use_compile`, CUDA graphs and extra decoder processes.

## How to run it

`campy-infer` does all of the fastest path for a lab's batch jobs: it runs a top-down model pair over recorded videos and writes one normal `.slp` per video, as `sleap-nn track` does.

```bash
campy-infer <video.mp4> --centroid <centroid model folder> --instance <instance model folder> --max-instances 5
campy-infer <session folder> --centroid ... --instance ... --max-instances 5 --out <session folder>/predictions --name "run1_{stem}.slp"
```

- **Inputs:** videos, or folders. A folder stands for its own videos and its sub-folders' videos, so a campy session folder runs every camera (`Camera1/Camera1.mp4`, `Camera2/...`, in camera order).
- **Outputs:** each video's `.slp` goes next to it as `<video name>.predictions.slp`, or into `--out`. `--name` sets the file name: `{stem}` is the video's name without its extension, and `{folder}` is the folder the video is in (for cameras whose videos are all called `0.mp4`: `--name "{folder}_{stem}.slp"`). Two videos can't write the same file: it stops before loading the models and says so.
- **A stopped job starts again where it was.** A video whose `.slp` is already there is skipped, so the same command can be run again after a job is cut off. Each `.slp` is written under a temporary name and renamed when complete. `--overwrite` runs every video again.
- **Settings:** `--max-instances` (required: every frame is given that many crops), `--peak-threshold` (0.2, for both models), `--batch` (16: on the A100 and the EVO-X2, 32 and 64 were no faster, and they need more GPU memory).
- **What it does by default:** on an NVIDIA GPU, the frames are decoded on the GPU (NVDEC, with `PyNvVideoCodec` installed), the models run as campy's fixed crops with TensorRT in fp16, with the tied-peaks fix. Elsewhere ffmpeg decodes on the CPU, and on an AMD GPU the networks run with `torch.compile`. If an engine can't be made, it says why and runs the networks in plain PyTorch; if NVDEC can't be used, it says why and ffmpeg decodes on the CPU. Each choice can be set: `--engine auto|tensorrt|compile|off`, `--decoder auto|nvdec|ffmpeg|pyav`, `--hwaccel` (ffmpeg's GPU decoder: `vaapi`, `cuda`, `qsv`, `d3d11va` or `none`), `--precision fp16|fp32`, `--strict-peaks`.
- **Reference runs:** `--exact` runs sleap-nn's own top-down layer instead: fp32 without TF32, sleap-nn's own peak finding, the frames copied to the GPU first, and every crop cut at its true size. It is about twice as fast as `sleap-nn track`, not ten times. Compare a fast run with it using `python bench/offline_infer_bench.py compare --base <exact .slp> <fast .slp>`.
- **Trying settings:** `--frames N` runs only each video's first N frames.
- **Engines:** the first run with a new model, GPU or batch size builds the TensorRT engines (about a minute) or compiles the networks, before any video's clock starts. They are kept in `--engine-cache` (`~/.cache/campy/engines`) and later runs read them in a few seconds.

Each video's line in the output gives its frames, its time, frames per second, and how much of it went to the models, to making the labels and to saving. The `.slp` records how it was made: its provenance has sleap-nn's usual fields and a `campy_infer` entry with campy's version, the engine, the precision and the peak finding.

**Install:** in the environment with sleap-nn and PyTorch (`campy-live`'s), `pip install --no-deps -e <campy folder>`. For the fast paths, also `pip install tensorrt onnx PyNvVideoCodec` on NVIDIA. Use the `tensorrt` package for the CUDA version PyTorch was built for (`tensorrt-cu12` for `+cu124`). On AMD, Triton comes with ROCm's PyTorch. ffmpeg is the one on the `PATH`, or imageio-ffmpeg's own.

**On a Slurm cluster,** one job per session, one GPU, 12 CPU cores, 64 GB of memory:

```bash
#SBATCH --gres=gpu:1
#SBATCH -c 12
#SBATCH --mem=64G
python -m campy.infer $SESSION --centroid $CENTROID --instance $INSTANCE --max-instances 5 \
  --out $SESSION/predictions --name "${TAG}_{stem}.slp" --engine-cache $SHARED/engines
```

Keep `--engine-cache` on shared storage, so the engines are built once per model and GPU type rather than once per job.

### How fast it is, and how close to the bench

The same 10-minute, 18,000-frame video as above, the whole of it, from starting the video to the `.slp` saved, with the models already loaded (that takes another 5–7 s). Every run found the same 4.72 mice a frame.

| Machine | `campy-infer` | the whole video | of which: models / making the labels / saving | `sleap-nn track` |
|---|---:|---:|---|---:|
| NVIDIA A100 40 GB (TensorRT, NVDEC) | **723–730 frames/s** | 24.7–24.9 s | 16.1 / 3.9–4.3 / 4.5–4.7 s | 78 frames/s, 3 min 51 s |
| GMKtec EVO-X2 (`torch.compile`, ffmpeg on the CPU) | **414–417 frames/s** | 43.2–43.4 s | 38.1–38.3 / 2.0 / 3.1–3.2 s | 68 frames/s (bench) |
| Dell Pro Max Slim FCS1250, RTX 2000 Ada (TensorRT, ffmpeg on the CPU), batches of 64 | **405 frames/s** (twice) | 44.5 s | 38.2 / 2.9 / 3.4 s | 67 frames/s (bench) |

The A100 ran on a cluster node; the benchtop PCs held their rig locks, and every run ended `RIG_CLEAN`. The models alone ran at 1,120 frames/s on the A100 and at 470 on the two benchtop PCs, as the bench measured (1,135, 453 and 447). The rest is making the labels and saving them, which the bench's own `--write-slp` run also spent (711 frames/s with batches of 64, against `campy-infer`'s 688–698).

On the A100, with batches of 64:

| Change | frames/s | Predictions |
|---|---:|---|
| the default | 688–698 | the bench's `--path fixed --engine tensorrt --decoder nvdec` `.slp`, to the bit: every mouse, every keypoint |
| `--decoder ffmpeg` (CPU) | 582 | the same, to the bit |
| `--engine off` (fp16 PyTorch, channels-last) | 522 | 1.0% of mice and 3.2% of keypoints (by more than a pixel) differ from the default's: TensorRT's fp16 against PyTorch's |
| `--exact` | 145 | the reference |

On the EVO-X2, ffmpeg's GPU decoder (`--hwaccel vaapi`) made the whole video slower, 349 frames/s against 411: the same GPU decodes the video and runs the models. So `campy-infer` decodes on the CPU unless asked, using about 4 cores there. The Windows campy-live environments don't have `PyNvVideoCodec`, so the Pro Max Slim decoded on the CPU too; its ffmpeg (702 frames/s) is faster than its models.

Against an `--exact` run (the first 6,000 frames, 29,320 mice): `campy-infer`'s default moved 3.1% of keypoints by more than a pixel, and `sleap-nn track` 2.1%. Matched through the `.slp` files, which keep no centroids, a mouse counts as found in one run only when the middle of its keypoints moved more than 10 px: 1.0% of mice for `campy-infer` and 0.8% for `sleap-nn track`. These are the same differences as in [Accuracy](#accuracy): fp16 and TensorRT, and sleap-nn's crop-size bug.

## Running the bench

```bash
python bench/offline_infer_bench.py run --video <video.mp4> --centroid <centroid model folder> --instance <instance model folder> --out runs/base --frames 6000
python bench/offline_infer_bench.py run ... --out runs/fast --upload gpu --precision fp16 --peaks tied --memory-format channels_last --batch 64 --decoder nvdec
python bench/offline_infer_bench.py run ... --out runs/trt --path fixed --precision fp16 --peaks tied --engine tensorrt --decoder nvdec
python bench/offline_infer_bench.py compare --base runs/base runs/fast runs/trt
```

Each run writes `<out>.json`: frames per second, stage times with `--stage-timing`, GPU use, CPU cores and decoding waits. It also writes `<out>.npz` with the predictions; `--write-slp` also saves an `.slp`. `compare` matches the mice frame by frame and reports how many differ and how far their keypoints moved. It also reads `.slp` files from `sleap-nn track`. `--decoder` chooses `sleapio`, `opencv`, `pyav` (in its own process), `ffmpeg` (with `--hwaccel vaapi`, `cuda` or `qsv`) or `nvdec` (PyNvVideoCodec). Run `--help` for the rest.

On PyTorch 2.6, `torch.compile` can't trace sleap-nn 0.3.3's max-pool padding, which computes `torch.ceil(torch.tensor(i / s)).item()`. The script replaces it with `math.ceil(i / s)` (the same number) for its compile runs. Newer PyTorch traces it as is.
