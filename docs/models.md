# Pose models for live tracking

`campy-live` runs a sleap-nn **top-down** model in two parts:

- a **centroid model** looks at the whole frame, scaled down, and finds one point on each animal;
- a **centered-instance model** looks at a square crop around each of those points and finds the body parts.

For live use both have to be much smaller than the usual SLEAP models. The standard SLEAP 1.x model for these mice took about 106 ms per set of 3 frames on the EVO-X2, over six times the 16.7 ms there is at 60 fps. The models below were trained to fit.

## How they were trained

- sleap-nn 0.3.3, one data-centre GPU (A100 or L40S) per model, about 2 minutes per epoch.
- About 1,000 labelled frames of 3 mice from several camera angles, 15 body parts.
- The centered-instance models were trained on 12 body parts, without the three tail points, because a crop that holds the tail tip has to be much larger. sleap-nn 0.3.3 won't train on a subset of a skeleton's parts, so the tail points were removed from a copy of the labels file.
- Batch size 4, the same augmentation for every model, a fixed random seed, so models differ only in the settings being compared.

## Settings that worked

**Centroid**: 0.25 input scale, 16 filters, max stride 16, **output stride 2**, a 10 px confidence-map spread (sigma, in full-size pixels), anchored on the trunk. Output stride 2 with sigma 10 found 97.6% of the mice in the validation frames, against 94.8% at output stride 4 with sigma 20. Most of the remaining misses were mice touching each other.

**Centered-instance**: a 320 px crop scaled to half size, so the model sees **160 px**, with **24 filters**, **max stride 32** and sigma 5 px.

- *Crop size* comes from the animals. Measured on 3,000 labelled mice, a trunk-centred crop needed 286 px for 99% of them and 369 px for the largest (rearing, seen from the side). A 320 px crop holds all but 10 of the 3,000.
- *Scale the crop down* rather than cropping smaller. Half scale was as accurate as 0.6–0.8 scale with max stride 32.
- *Max stride 32*, not 16. With max stride 16, at inputs of 256 px and up, the model sees too little of the crop to tell which mouse is the centred one, and puts the whole skeleton on a neighbour more often. Across all the crop sizes tried, max stride 32 cut wrong-mouse skeletons from about 11 to 4 per 293.
- *24 filters* beat 16. A filter rate above 2.0 helped the small models catch up, but didn't improve this one.

Accuracy on the same 293 validation mice (11 body parts, median / 90th / 99th percentile error in pixels, and how many skeletons landed on the wrong mouse):

| Centered-instance model | Error p50 / p90 / p99 | Wrong mouse |
|---|---|---|
| 160 px (320 px at half scale), 24 filters, max stride 32 | 3.5 / 12.7 / 51.4 px | 2 |
| 320 px at full scale, 24 filters, max stride 32 | 3.1 / 11.4 / 52.6 px | 4 |
| 192 px (384 px at half scale), 24 filters, max stride 16 | 3.9 / 13.4 / 66.5 px | 7 |
| 192 px (384 px at half scale), 16 filters, max stride 16 | 4.3 / 16.1 / 80.6 px | 8 |
| Standard SLEAP 1.x model, for reference (its own validation split) | 3.7 / 12.9 / 56.9 px | 3 |

## Choosing one that keeps up

1. **Benchmark the sizes first**, untrained, on the machine that will run them. Run time doesn't depend on the weights:
   ```
   python bench/sleap_infer_bench.py --precision fp16 --crops-per-frame 5 \
       --centroid-output-stride 2 --crop 160 --instance-filters 24 --instance-max-stride 32 \
       --pace 60 --hold-clock on
   ```
   `--crop` is the size the centered-instance model sees, `--crops-per-frame` is the number of animals, and `--frames` is the number of cameras (3 by default). `--sweep` runs a preset grid of 32 sizes.
   `--pace 60` starts the sets 60 times a second, as the cameras deliver them, and `--hold-clock on` keeps the GPU's clock up as `campy-live` does. Without them the numbers mislead: a GPU lowers its clock between paced sets, and a heavier model, which keeps it busier, can then look as fast as a light one. Each row records the clock the GPU ran at.
2. **Leave about 2 ms.** The live pipeline ran 1–1.7 ms slower than the benchmark at the median and 2–2.5 ms at p99, because it also copies frames in, reads results back and logs them. At 60 fps, pick a model whose benchmark p99 is about 14–14.5 ms or less. At 30 fps there is far more room.
3. **Check it live**: the summary's skipped sets should be zero ([Live tracking](live-tracking.md)).

On the EVO-X2 with 5 mice and 3 cameras, the benchmark p99s were 14.0 ms for the 160 px model, 15.3 ms for the 24-filter 192 px model and 10.6 ms for the 16-filter one. In the full live pipeline the 160 px model skipped nothing. The 24-filter 192 px model, with less room, still skipped a set now and then, and the 16-filter one kept up easily but was less accurate. On the Pro Max Slim (RTX 2000 Ada) the 160 px model took 16.5 ms per set with 5 mice, which is too close to the 16.7 ms between frames: it skipped 4–6% of the sets. Use the 16-filter 192 px model there, or 30 fps. The [hardware guide](hardware.md) has the live measurements.

## Where a set's time goes

`bench/live_stage_timing.py run` runs `campy-live` itself with a timer around each stage of each set. Before and after each stage it waits for the GPU to finish, so each stage counts only its own work. Its docstring lists the stages. `bench/live_stage_timing.py kernels` profiles the two models layer by layer: GPU time per layer, against the bytes each layer moves and the arithmetic it does.

On the EVO-X2, with the settings above, 3 cameras replaying 5 mice at 60 fps (3,592 sets, 15 crops in most). Times are per set, in ms:

| Stage | p50 | p99 |
|---|---|---|
| Copy the set out of shared memory | 0.64 | 1.31 |
| Upload to the GPU | 0.21 | 0.67 |
| Centroid preprocessing (shrink 1024×1280 to 256×320) | 0.53 | 1.05 |
| **Centroid model** | **2.14** | 2.94 |
| Centroid peak finding (the CPU waits for the GPU here) | 1.03 | 1.58 |
| Cutting out the crops | 0.75 | 0.84 |
| Crop preprocessing (320 to 160 px), uint8 to float for both models | 0.25 | 0.44 |
| **Centered-instance model** | **9.11** | 9.33 |
| Instance peak finding | 0.77 | 1.57 |
| sleap-nn's other steps between and after the models | 0.64 | 1.07 |
| Results back to the CPU (4 downloads) | 0.12 | 0.33 |
| **Upload to results back, timed per stage** | **15.7** | **17.5** |
| The same, untimed (as `campy-live` logs it) | 15.3 | 17.7 |
| The same, untimed, `--cuda-graphs` | 15.1 | 16.7 |
| 3D (on its own thread, alongside the next set) | 1.2 | 2.4 |

- **The models are 72% of the time, and the centered-instance model alone is 58%.** Moving data in and out (copy, upload, downloads) is about 1 ms. Making those steps faster can save a few tenths of a millisecond at most.
- **The steps hardly overlap.** Timed one at a time, the stages add up to almost the same time as the untimed run (15.7 against 15.3 ms).
- **CUDA graphs save about 0.2 ms here.** The GPU is busy computing, not waiting for work to be sent to it.
- **Memory bandwidth isn't the limit, even on the high-resolution layers.** The GPU copies 213 GB/s in this test. The largest layers (the first convolutions, at 160×160 and 256×320) reached 66–172 GB/s, and most convolutions run at 12–39 TFLOP/s, close to the GPU's 34 TFLOP/s fp16 matrix speed. Activations this size mostly stay in the chip's cache.
- **The biggest waste is the upsampling.** The models' bilinear upsampling layers took 3.7 ms of GPU time per set, a third of both models, and the casts between fp16 and fp32 around them took another 1.5 ms. Under fp16 autocast, PyTorch runs bilinear upsampling in fp32, with a kernel that handles each output pixel in a single thread and loops over all the crops and channels. On the smallest layers (5×5 and 10×10 pixels, 384–768 channels) that leaves the GPU almost idle: 1.45 and 0.98 ms, at 2–6 GB/s. Run in channels-last layout, those two layers gave identical keypoints, and the whole top-down step went from 13.8 to 12.4–12.6 ms per set. In the larger layers channels-last was slower, so it helps only the small ones. `campy-live --fast-upsample` does this by itself (on by default on AMD GPUs when the networks run in plain PyTorch, see [Faster upsampling](live-tracking.md#faster-upsampling-on-amd---fast-upsample-on-by-default-with-rocm-when-no-engine-runs)): while it warms up it times every upsampling layer both ways, for every number of crops, and keeps the faster. On the EVO-X2 that moves the 5×5, 10×10 and most 20×20 layers to channels-last (5×5×768 with 15 crops: 1.45 to 0.08 ms) and leaves the rest. In the live replay with `--fixed-crops --cuda-graphs`, a set went from 13.6 to 10.6 ms.
- **Shrinking the frames converts them to float first.** On the GPU, torchvision turns all three 1024×1280 frames from uint8 into float32, shrinks them with antialiasing, and rounds them back to uint8. The model then divides by 255 at 256×320. That costs 0.5 ms per set.

The table above is sleap-nn's own top-down layer, run in plain PyTorch: `campy-live`'s default before pull requests 126–147.

### With today's defaults (fixed crops, one CUDA graph, compiled networks)

`campy-live`'s defaults now run the whole set as one CUDA graph (`--fixed-crops`, `--cuda-graphs`), with the two networks compiled (`--engine auto`: `torch.compile` on the EVO-X2, TensorRT on NVIDIA). A graph can't be stopped halfway to time one step, so `bench/live_stage_timing.py run --timing stages` (its default) records each step of the same code as a graph of its own, in the same order and the same GPU memory, and waits for each in turn. Every 100th set it also runs `campy-live`'s own single graph on the same frames: the two outputs were identical in every check. Waiting between steps costs 0.1–0.3 ms per set, so the steps add up to a little more than `campy-live`'s own untimed time (last rows).

Measured on 22 October 2026, campy dev after pull request 148, the same 5-mouse replay (3 cameras, 60 fps, 3D), 2 runs of about 3,560 timed sets on each machine, every run `RIG_CLEAN`. Per set, p50 / p99, in ms; the GPU's own time for each step was within 0.03 ms of these:

| Step | EVO-X2 (Radeon 8060S, `torch.compile`) | Pro Max Slim (RTX 2000 Ada, TensorRT) | Precision 7875 (RTX 6000 Ada, TensorRT) |
|---|---|---|---|
| Copy the set out of shared memory (before the timer `campy-live` logs) | 0.17 / 0.33 | 0.28 / 0.46 | 0.23 / 0.86–1.06 |
| Upload to the GPU, into the graph's input | 0.14 / 0.20 | 0.37 / 0.52 | 0.22 / 0.69–0.72 |
| Shrink the frames to 256×320 | 0.17 / 0.20 | 0.20 / 0.25–0.28 | 0.06 / 0.24–0.36 |
| **Centroid network** (with the / 255) | **0.95** / 0.99–1.01 | **0.84** / 0.93–0.94 | **0.22** / 0.84 |
| Centroid peaks, top 5 per frame, refinement | 0.31 / 0.36–0.38 | 0.18 / 0.23 | 0.17 / 0.38–0.39 |
| **Cutting out the 15 crops** | **0.66–0.67** / 0.77 | **0.47** / 0.51–0.52 | 0.09 / 0.39 |
| Shrink the crops to 160×160 | 0.12 / 0.15 | 0.09 / 0.12 | 0.04 / 0.16–0.19 |
| **Centered-instance network** | **3.39–3.41** / 3.48–3.49 | **3.52–3.53** / 3.78–3.83 | **0.78** / 2.90–2.91 |
| Instance peaks, refinement, packing the results | 0.30 / 0.33 | 0.18 / 0.21 | 0.12 / 0.33 |
| Results back to the CPU (one copy, one wait) | 0.10 / 0.29–0.31 | 0.13 / 0.23 | 0.12–0.13 / 0.23–0.24 |
| Upload to results back, timed step by step | 6.20 / 6.58–6.63 | 6.02–6.03 / 6.52–6.57 | 1.92 / 6.19–6.20 |
| The same, untimed (as `campy-live` logs it) | 5.93–5.94 / 6.10–6.12 | 5.80–5.82 / 6.54–6.55 | 1.62–1.63 / 3.63–5.10 |
| **The same, with the faster crop below** (pull request 151) | **5.46–5.47 / 5.64** | **5.49–5.54 / 6.46–6.50** | 1.62 / 4.40–5.24 |

- **The networks are now 70–75% of a set.** Moving data in and out (the copy out of shared memory, the upload and the download) is 0.4–0.8 ms, and the shrinking 0.1–0.3 ms. Even made free, they would save little.
- **Cutting out the crops was the largest step outside the networks**: 0.67 ms on the EVO-X2 and 0.47 ms on the Pro Max Slim, though it only copies 1.5 MB. It followed sleap-nn's way: pad all three frames with 320 pixels of zeros on every side (writing 9.6 MB), then index them by frame, row and column, with three indexes for every pixel. PyTorch's indexing kernel is slow with three indexes, on ROCm and on the RTX 2000 Ada alike (on the RTX 6000 Ada it took 0.07 ms). Since pull request 151, `campy-live` computes one number per pixel (where it is in the unpadded frames) and reads the frames with a single gather, writing 0 where a crop runs past a frame's edge, as the padding did. The pixels are exactly the same: in a test of 400 random sets, half with crops past the edges, and in the replays, where every keypoint of every set was identical to the bit. The crop took 0.69 → 0.18 ms on the EVO-X2, 0.44 → 0.12 ms on the RTX 2000 Ada and 0.07 → 0.06 ms on the RTX 6000 Ada, measured alone in a CUDA graph; the whole set's graph also holds 25 MB less GPU memory (126 → 101 MB on the EVO-X2, 59 → 42 MB on NVIDIA). Compiled with `torch.compile`, the same crop would take 0.02 ms, another 0.1–0.2 ms, but on NVIDIA that would add Triton next to TensorRT.
- **The RTX 6000 Ada's p99 is its GPU clock, not data.** All of that machine's slow sets came in one or two stretches of about 1.3 s per minute (12–13.4 s and around 36 s into the replay), in which every step, network or copy, ran 3–4 times slower on the GPU itself. Those are the moments when `--hold-gpu-clock-blocks auto` tries spinning one block fewer: after 10 s at full clock it drops from 1 block to none, the RTX 6000 Ada's clock falls to 800–975 MHz within half a second, and 1 s later `auto` puts the block back and waits twice as long (20 s, 40 s, … up to 320 s) before trying again. In each 60-s replay it did that twice (4 block changes); in the 10-s stretches without a try, p99 was 1.7–2.0 ms. In a long recording the tries become rare (one 1.3-s stretch every 5 minutes or so), so a long recording's p99 would be close to 2 ms. The Pro Max Slim tries too, but its clock only falls to about 2,050 MHz.
- **The EVO-X2's first-second hiccup is one set.** The first set after the recording starts takes 25–57 ms (the next one is then skipped); every other set of the first second is normal. In that set the upload waits about 27 ms on the CPU for a copy the GPU does in 0.05 ms. With `--hold-gpu-clock off` it went away (the slowest set 12 ms, nothing skipped), but every set was 0.8 ms slower (6.7 ms), so the cause is the clock hold's spinners starting just before the first set (`GpuClock.Start` runs once the feed is there).
- **What's left.** The copy out of shared memory (0.17–0.28 ms) could be skipped by registering the shared-memory rings as pinned GPU memory and uploading straight from a ring's slot, but that saves less than 0.3 ms of latency. Peak finding (0.3 + 0.3 ms on the EVO-X2) could go inside the `torch.compile` region, but compiled floating-point sums may round differently, so the keypoints would no longer be exactly today's.

## Running the models faster: compiled engines (on by default in campy-live)

`campy-live` used to run both models in plain PyTorch, in fp16. In PyTorch every layer of a network is its own small GPU program: a convolution writes its result to memory, and the step after it (adding the bias, the activation, the upsampling) reads it back. A *compiled engine* fuses those steps into one program and picks the fastest one for the exact image size, so far less goes to and from memory. CUDA graphs (`campy-live --cuda-graphs`) only cut the cost of *starting* each program; fusing is the other half.

`bench/compiled_bench.py` measures this on the trained models above, with the replay videos as input. It times only the two networks (preprocessing, cropping and peak finding stay in PyTorch, as in `campy-live`), 60 sets a second with the GPU clock held, and compares the output with PyTorch in full precision on 500 sets of 3 frames. Per set of 3 frames with 5 mice (15 crops), median / p99:

| Machine | PyTorch fp16 (now) | + CUDA graphs | `torch.compile` | TensorRT fp16 | MIGraphX fp16 |
|---|---|---|---|---|---|
| EVO-X2 (Radeon 8060S) | 11.1 / 11.3 ms | 11.1 / 11.3 ms | **4.3 / 4.4 ms** | – | 11.1 / 11.2 ms |
| RTX 6000 Ada | 5.1 / 6.5 ms | 2.1 / 2.2 ms | 1.7 / 1.9 ms | **1.2 / 1.6 ms** | – |
| RTX 4000 Ada | 7.3 / 9.5 ms | 5.6 / 5.8 ms | 4.2 / 4.5 ms | **2.7 / 2.8 ms** | – |
| RTX 2000 Ada (Pro Max Slim) | 10.9 / 11.2 ms | 10.2 / 10.2 ms | 7.4 / 7.9 ms | **4.5 / 4.7 ms** | – |

- **On the EVO-X2, `torch.compile` makes the networks 2.6 times faster** (11.1 to 4.3 ms per set), with the same accuracy as fp16 PyTorch. Most of the gain is the upsampling step, which PyTorch runs slowly on this GPU (3.4 of 11.8 ms in fp32) and the compiler fuses into its neighbours. AMD's own engine, MIGraphX, gained nothing on this GPU.
- **On NVIDIA, TensorRT is fastest**, then `torch.compile` (max-autotune mode), then CUDA graphs. On the Pro Max Slim's RTX 2000 Ada it saves 6.4 ms per set (10.9 to 4.5 ms), enough to bring the 5-mouse live pipeline (16.5 ms per set by default, too close to 60 fps) well under the frame time. `campy-live --fixed-crops --cuda-graphs` alone already brings it to 12.6 ms, with nothing skipped (live-tracking.md, "Fixed crops"). On the RTX 6000 Ada the networks are already fast, so the gain in milliseconds is small.
- **int8 isn't worth it.** On the NVIDIA cards TensorRT int8 was at most 0.3 ms faster than fp16 and moved 15.6% of keypoints by more than 1 pixel (p99 4.4 px). MIGraphX's int8 gave exactly its fp16 output, so this GPU doesn't run it in int8 at all.
- The timings don't depend on the weights: random weights of the same shapes gave the same times with CUDA graphs and the compiled engines (within 0.05 ms).

A compiled engine is built for a fixed number of crops, while the number of animals found changes from set to set. `campy-live --fixed-crops` (pull request 126, on by default with a GPU) always runs `--max-instances` crops per frame, padding the missing ones, which is what an engine needs. `campy-live --engine` (pull request 133) runs the networks this way, and since pull request 147 its default, `auto`, does so on every GPU with the packages: TensorRT on NVIDIA where it is installed, otherwise `torch.compile`. Live, with 5 mice on 3 cameras at 60 fps, a set took 10.6 → 5.9 ms on the EVO-X2, 12.6 → 5.8 ms on the RTX 2000 Ada, 6.4 → 3.4 ms on the RTX 4000 Ada and 2.7 → 1.6 ms on the RTX 6000 Ada, with tracking exactly as close to the offline SLEAP predictions as before. Where no engine can be made, `campy-live` says why and runs the networks in plain PyTorch. Start-up times, caches and the accuracy comparison: [Compiled networks](live-tracking.md#compiled-networks---engine-on-by-default-with-a-gpu).

**fp16 used to lose a few animals; `campy-live` now keeps them.** sleap-nn's centroid peak finder keeps a pixel only if it is strictly higher than all 8 of its neighbours. In fp16 the two top pixels of a peak sometimes round to exactly the same value, and then that animal had no centroid in that frame. This happened to 0.6–0.9% of centroids in every fp16 engine, PyTorch included (`campy-live`'s default), always with exactly two tied pixels per lost centroid, and never in fp32.

Since pull request 132, `campy-live` breaks such a tie by position: of two equal top pixels it keeps the first in reading order (`campy/peaks.py`), so the peak is found once. A peak without a tie is found exactly as before. `--fixed-crops` finds its peaks the same way. `--strict-peaks` brings back sleap-nn's own peak finder, in both. Measured with `bench/tied_peaks_check.py` on the 5-mouse replay (3 cameras, all 3,600 sets, 53,652 centroids in fp32):

| | EVO-X2 | RTX 6000 Ada |
|---|---|---|
| fp16, sleap-nn's peak finder: centroids lost | 397 (0.74%) | 359 (0.67%) |
| fp16, `campy-live`'s: centroids lost | 0 | 1 |
| fp32, `campy-live`'s vs sleap-nn's | identical | identical |

- The one centroid still lost on the RTX 6000 Ada isn't a tie: that frame had six peaks for five places (`--max-instances 5`), the 5th and 6th scored 0.6560 and 0.6562, and fp16 kept the other one. sleap-nn's peak finder did the same.
- In sets where both found the same mice, every keypoint was bit-identical.
- A recovered mouse's centroid was up to 8.6 px from fp32's, because sleap-nn's sub-pixel refinement leans toward whichever of the two pixels is kept. Its crop moves with it, and its keypoints were 1.1–1.2 px from fp32's at the median (p99 6.6–6.8 px). Before the fix, that mouse had no keypoints at all in that frame.
- When one more mouse is found in a set, the centered-instance model runs on one more crop, and in fp16 the other mice's keypoints in that set can then change: on the EVO-X2 all of them changed a little, on the RTX 6000 Ada 3% of them did. The largest change was 64 px on the EVO-X2 and 7.2 px on the RTX 6000 Ada, a node jumping between two nearly equal spots on its confidence map, as fp16 nodes sometimes do against fp32. The same happens in fp16 whenever the number of mice found in a set changes.

To check a machine:
```
python bench/tied_peaks_check.py --centroid <centroid model folder> --instance <instance model folder> \
    --videos cam1.mp4 cam2.mp4 cam3.mp4
```

To measure a machine (see the script's help for the engines and what each needs installed):
```
python bench/compiled_bench.py --centroid <centroid model folder> --instance <instance model folder> \
    --videos cam1.mp4 cam2.mp4 cam3.mp4 --engines eager-fp16,graph-fp16,compile-ma,trt-fp16
```

## Finding the frames a model gets wrong (`campy-live-flag`)

A model trained on other rigs' videos makes its own mistakes on a new rig: a camera that keeps missing a mouse in one corner, a side view where skeletons land on the wrong mouse. The fastest fix is to label a few of those frames and train again. Labelling only nine frames from one rig removed most of the false detections there. `campy-live-flag` finds those frames for you, from `campy-live`'s log, after the recording:

```
campy-live-flag campy-live_<date_time>.npy --budget 25
```

It doesn't change `campy-live` or slow it down: everything it needs is already in each row of the log (the animals each camera found, their scores, and, with `--calibration`, which detection is which animal in each camera, the 3D pose and how well the cameras agree). For each set of frames and each camera it flags:

| Flag | What happened | Needs 3D |
|---|---|---|
| `missed` | Two or more other cameras saw an animal and its 3D pose falls inside this camera's picture, but this camera found nothing there. The model missed it, or something hid it. | yes |
| `swap` | Two animals' identities swapped from one set to the next. | yes |
| `jump` | An animal's 3D position jumped more than 50 mm in one set (a wrong match). | yes |
| `unmatched` | A detection that the 3D matched to no animal, though there were no more detections than animals: the cameras disagree on where the animals are. | yes |
| `disagree` | One camera's skeleton is more than 15 px from where the animal's 3D pose puts it. | yes |
| `extra` | More detections than animals (seen only when `--max-instances` is above the number of animals). | no |
| `fewer` | Fewer detections than animals. | no |
| `low_score` | A detection whose points scored under 0.5 (the median). | no |

A mistake usually lasts many sets in a row: a mouse missed for a second is 60 sets at 60 fps. Flags of the same kind in the same camera less than a second apart count as one *event*, and each event offers one set to label. From those it picks `--budget` sets (25 by default, about an hour of correcting 3 mice in 3 cameras):

- 10% at random from the whole recording, so the labels aren't all hard cases and mistakes no flag catches still turn up;
- the rest from the events, one kind of mistake at a time in turn (`missed` first), the cameras taking turns within each kind. Each pick is the event *least like* the sets already picked: where the animals are in each camera's picture, how many each camera found, and how close together they are. That avoids picking 25 near-identical frames of the same huddle;
- no two picks closer than 2 s.

A set is labelled whole, every camera's frame of it. That gives three training images, and lets the 3D check the corrections.

It writes `flags.csv` (every flag, with where in the picture it is: for `missed`, where the animal should have been), `events.csv` and `sample.json` (the picks and why each was picked) into `<log>_flags/`, and prints how often each kind of mistake happened in each camera, a quick health check of a model on a rig. Give several logs to pick one sample from several recordings. It needs only numpy and scipy, and takes about a second for a 2-minute log and about 20 s for an hour. Run `python -m pydoc campy.liveflag` for every option.

If the calibration file named in the log's summary isn't on the computer, give it with `--calibration`. Without it, `missed` can't be worked out, and `disagree` uses the log's own reprojection errors, without saying which camera is wrong. A log made without `--calibration` gets only the `extra`, `fewer` and `low_score` flags.

## Labelling them in LUC3D (`campy-label-pack`, `campy-label-merge`)

`campy-label-pack` turns the sets `campy-live-flag` picked into one file to correct in [LUC3D](https://github.com/talmolab/luc3d), with `campy-live`'s predictions already on them, so labelling means correcting, not starting from nothing. Run it in the inference environment (it needs `sleap-io`, which comes with sleap-nn):

```
campy-label-pack <log>_flags/sample.json --recording <recording folder> --name <rig>_<date>
```

It writes `<name>/<name>.pkg.slp` next to `sample.json`:

- **The frames are in the file**: every camera's frame of each picked set, as PNG, so exactly the video's pixels. It opens on any computer, without the recording's videos. Frame 0, 1, 2, … of the package are the picked sets in order; `sets.csv` says which recording, campy frame and video frame each one is, why it was picked, and its flags.
- **The predictions are on them**: each camera's detections, and with 3D each animal's identity and 3D pose, grouped across the cameras. So LUC3D also shows where the 3D puts an animal in a camera that missed it.
- `--skeleton <training package>` writes the labels in the training set's nodes. The live models have no tail points, so those stay empty unless you add them.

**Correcting in LUC3D.** File > Load SLP > `<name>.pkg.slp` (nothing else to pick). Shift+Right goes to the next set. Correct each set in every camera:

- double-click an animal's predictions to make them your labels, then drag the points that are wrong (dragging a predicted point converts it too);
- where a camera missed an animal, double-click its projection there to make it a label, and drag the points into place;
- press T to triangulate again, to see where the cameras still disagree;
- delete detections that aren't an animal.

**Every prediction must become a label or be deleted.** A camera's frame that still has a prediction left in it isn't used for training: the mouse left unlabelled would teach the model there is no mouse there. When done, File > Save as `<name>.labels.slp` next to the package.

LUC3D can't open an embedded frame whose frame numbers or size are stored as 64-bit numbers, as `sleap-io` writes them ("Cannot mix BigInt and other types"). `campy-label-pack` stores them as 32-bit; packages made otherwise, the training packages included, don't open in LUC3D yet.

**Making training and test sets.** `campy-label-merge` reads what LUC3D saved and takes the frames out of the package:

```
campy-label-merge <name>.pkg.slp <name>.labels.slp --test-into tests/<rig>.test.pkg.slp \
    --train-into <training package> --train-out <new training package>
```

- **Test sets are kept apart for good.** About a third of each recording's minutes are test minutes, chosen by a hash of the recording's name and the minute. A set is always on the same side, and whole minutes are kept out, so no frame a few frames from a test frame is trained on. Test frames go to `<name>.test.pkg.slp` and, with `--test-into`, into that rig's test set, which only grows. They are for measuring models, never for training.
- **The rest go to `<name>.train.pkg.slp`**, and, with `--train-into`, into a copy of the training package (`--train-out`). The labels are written in that package's nodes, so run it once for the 15-node package (centroid model) and once for the 12-node one (centered-instance model).
- **Points marked hidden are left out**, as SLEAP trains on them. That includes the points LUC3D fills in when a prediction becomes a label.
- **`<name>.merge.csv`** says what happened to each camera frame: taken, not finished (predictions left), untouched, or emptied (every prediction deleted and no label; left out unless `--keep-emptied`).

Tried on a 30-second, 3-camera recording of 5 mice: 12 picked sets were packed in 18 s (15 MB). LUC3D opened the package with the predictions, identities and 3D on it. A set corrected and saved there was merged into the 1,000-frame 12-node training package in 5 minutes, mostly reading the 550 MB package over the network. The result read back as 1,003 frames, the new ones pixel for pixel the package's.

## Measuring a model on a rig's test sets (`campy-model-eval`)

Until now, a model's accuracy on a rig was judged by consistency: how well the cameras agree in 3D, and how often the count of animals is right. `campy-model-eval` measures it against labels instead: the rig's test sets from `campy-label-merge`, which are never trained on. Give it one pair of models, or several to compare (an old and a new one, say):

```
campy-model-eval tests/<rig>.test.pkg.slp --pair new=<centroid folder>,<instance folder> \
    --pair old=<centroid folder>,<instance folder> --out eval.json
```

It runs each set through `campy-live`'s own inference code, with `campy-live`'s defaults: the same preprocessing, fixed crops, the tie-tolerant peaks, fp16 and the compiled engines on a GPU. So the numbers are the models as `campy-live` runs them. Run it in the inference environment, on the rig's own GPU (under the rig lock). Any of `campy-live`'s options can follow `--live-args`, for example `--max-instances 3`. For each pair, and for each camera, it reports:

| Measure | What it is |
|---|---|
| missed | Labelled animals with no detection: no centroid within 50 px of the label's Trunk (`--match-px`), matched one to one. |
| extra | Detections that are no labelled animal, per camera frame. |
| wrong mouse | Animals found whose skeleton sits on another animal: their points' median distance from the label is over 30 px (`--wrong-px`). |
| node error | The distance of each labelled point from the model's, in pixels: median, p90, p99, and the share within 5 and 10 px. Points the model didn't place are counted apart. |
| grouped | Of the labelled animals seen in two or more cameras, the share whose detections `campy-live`'s 3D (the same tracker, started afresh on each set) put together as one animal, with no other animal's detections. |

Identity switches over time need labelled sequences, which single test sets aren't. Everything is also reported separately for the sets picked at random (the model as it meets the rig) and for those picked for a flag (the hard cases).

Checked with stand-in labels: the predictions `campy-live` logged on a 5-mouse replay, packed and merged as if they had been corrected. Run on a laptop CPU in fp32 against those labels:

- the models that made them (a 1,000-frame training set: the centroid model with output stride 2 and a 10 px spread, the 160 px centered-instance model) scored a median point error of 0.09 px, p99 4.75 px, 0.6% missed and no wrong mouse (the rest is fp32 on a CPU against fp16 on the rig's GPU);
- a pair with the same settings trained on the 10,223-frame set (plus 9 frames from another rig) scored 5.4 px median, 17.9% missed against those labels, and 8 wrong-mouse skeletons.

That says the tool measures what it should, not which model is better: stand-in labels are the first model's own predictions. Real numbers need real corrected labels.

## Does a model suit a rig? (`campy-model-fit`)

A model loses animals on frames that look unlike its training images: brighter, more saturated, recorded with another gamma ([Recording for a model](#recording-for-a-model)). `campy-model-fit` compares them before a recording, in two steps:

```
campy-model-fit stats <training package> --out <model folder> --gamma Off
campy-model-fit check <model folder> --recording <a short recording on the rig>
```

`stats` reads 300 labelled frames of the training package and writes `image_stats.json` into the model's folder. It records the spread of each frame's mean brightness, its 95th percentile and its share of saturated pixels, and the animals' brightness (the pixels within 10 px of their labelled points). It also writes `model.json` with the model's id: the first 12 hex digits of the SHA-256 of its `best.ckpt`, so a recording can name the exact model it ran.

`check` reads the first 10 s of each camera's video and its `metadata.csv`. It warns, per camera, when:

- its frames are darker or brighter than 90% of the training frames;
- it has far more saturated pixels;
- its animals (with `--log`, from `campy-live`'s keypoints) are over 25% darker or brighter than the training animals;
- its `cameraGamma` isn't the one the training videos had.

It exits with 1 when any camera got a warning.

On the 1,000-frame training set: frame mean brightness 14–33 (5th–95th percentile), animals 127. Against it, a 30-second replay of 5 mice on three cameras: one camera matched, two were brighter (mean 33 and 35), and on the brightest the animals were too (178 against 127).

## Recording for a model

- **Match the training images' brightness.** The training frames averaged about 24 out of 255. Frames about 8 times brighter, with many saturated pixels, lost most mice and produced hundreds of false detections, and frames half as bright lost about half. Set aperture, exposure and gain so live frames look like the training frames.
- **Gamma**: since October 2026 campy sets FLIR cameras to gamma 0.6 by default (`cameraGamma`). Before that its default turned the camera's gamma off, so older training videos were probably recorded that way. Gamma 0.6 makes dark and middle greys brighter. To match a model trained on older videos, set `cameraGamma: "Off"`, or check that the model still finds every animal.
- **Video quality**: H.265 at quality 26 (Quick Sync, 30 fps) cost the models 1.6% of the animals they found in lossless video, against 3.7% at quality 30 and 7.7% at 34.
