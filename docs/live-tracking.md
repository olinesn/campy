# Live tracking with campy-live

Three programs work together on the computer that records:

- **`campy-acquire`** records the cameras as usual. With `liveFeed: True` it also copies each frame into shared memory, the *live feed*.
- **`campy-live`** reads the feed and runs a sleap-nn top-down model on each synchronized set of frames, one frame per camera. With a calibration, it also matches the animals across cameras, keeps their identities and triangulates them in 3D. It is never more than one set behind: a set that arrives while it is busy is skipped and counted, never queued.
- **`campy-live-view`** (optional) shows the camera frames with the tracking drawn on them, plus a 3D view.

campy never waits for `campy-live`, so tracking can't slow down or break a recording. The [main README](../README.md#live-pose-estimation-livefeed-campy-live) lists every option.

## Two Python environments

The recording and the inference run in separate environments, because one needs camera drivers and the other needs torch:

- **Recording**: campy's usual environment ([Installation](../README.md#installation)), with Spinnaker/PySpin for FLIR cameras.
- **Inference**: Python 3.12 with a GPU build of torch, `sleap-nn` 0.3.3, OpenCV (`opencv-python`) for the view, and optionally `PyQt5` for the view's Qt window. Install campy into it without its dependencies, so the camera drivers aren't needed there:
  ```
  pip install --no-deps -e <campy folder>
  ```
  `campy-live` imports only numpy and campy's own feed modules, besides torch and sleap-nn.

Which torch build:
- **Linux, AMD GPU (ROCm)**: a ROCm build for your GPU. The EVO-X2's Radeon 8060S (gfx1151) used AMD's own torch wheels (torch 2.14.0+rocm10.1.0).
- **Windows, NVIDIA GPU (CUDA)**: a CUDA build, for example `pip install torch --index-url https://download.pytorch.org/whl/cu128`. The Pro Max Slim uses torch 2.11.0+cu128; the Precision 7875 (RTX 6000 Ada and RTX 4000 Ada, driver 596) uses 2.14.1+cu130 (`--index-url https://download.pytorch.org/whl/cu130`). Any recent CUDA build runs on Ada-generation cards.

## Recording with live tracking

1. **Start `campy-live` first**, in the inference environment:
   ```
   campy-live --cameras 3 --model sleap --centroid <centroid model folder> --instance <centered-instance model folder> \
       --animals 5 --calibration calibration.toml
   ```
   It loads the models and warms them up before any frame exists, then prints `Waiting for the live feed`. On a computer with performance and efficiency cores (Intel Core Ultra and 12th-generation Core or later) it first moves itself onto the performance cores at High priority, and once the feed appears it moves campy-acquire, its camera processes and their encoders onto the efficiency cores (see below, On Windows). The wait is 5–8 s on the EVO-X2 and up to about 18 s on Windows, mostly imports. It assumes 1280 × 1024 grey frames. For other cameras, give `--frame-shape <height>x<width>x<channels>`, or it builds the model again when the frames arrive and misses the first seconds.
2. **Start the recording** with the live feed on:
   ```
   campy-acquire <config>.yaml --liveFeed True
   ```
   Because `campy-live` is already waiting, it analyses the recording from the first frame.
3. **Optionally, watch it** in the inference environment:
   ```
   campy-live-view
   ```
   It runs as a separate program at low priority and stays open from one recording to the next. Its `c` key, or the "Fast CPUs" box in its panel, moves `campy-live` on and off the performance cores while both run, and the panel shows `campy-live`'s inference time over the last 2 s, so you can see the difference. On a computer without a screen (over SSH), `campy-live-view --no-window --png <folder>` saves pictures instead.

`campy-live` ends with the recording, or at Ctrl-C. It writes `campy-live_<date_time>.npy`, with one row per set processed (keypoints, confidences, and the 3D poses and identities with `--calibration`), and a `.json` summary.

`campy-live` runs the models in fp16 (`--precision fp16`, the default on NVIDIA and AMD GPUs). sleap-nn's own centroid peak finder lost about 0.7% of the mice per frame in fp16, wherever the two top pixels of a centroid rounded to the same value. `campy-live` keeps such a peak once instead, so fp16 finds the mice fp32 finds; `--strict-peaks` goes back to sleap-nn's own (see [Models](models.md)).

## Checking that it keeps up

The summary, and the reports it prints every 10 s, give the sets processed and skipped, the inference time per set and the latency from frames to keypoints and to 3D. At 60 fps a set must take less than 16.7 ms, and at 30 fps less than 33.3 ms.

- **Sets skipped through the whole recording**: the model is too slow for this machine and frame rate. Choose a lighter one ([Models](models.md)).
- **A few skipped in the first fraction of a second**: the GPU was still at its idle clock when the frames came. `campy-live` holds the GPU's clock up from the moment the feed appears (`--hold-gpu-clock`, on by default with NVIDIA and AMD GPUs), so this should no longer happen.
- **"misaligned" sets**: one camera's frames reached the computer late, or a camera lost a frame (`flirFramesLost` in its `metadata.csv`). See the README on frame numbers.

For a test without animals or without a trained model, `--model random` runs untrained models of the benchmark's default sizes, and `--model none` only reads the feed.

## Fixed crops (`--fixed-crops`, off by default)

Normally `campy-live` runs the centroid model, waits for the GPU to say how many animals it found, and only then cuts that many crops for the instance model. That wait in the middle of every set costs time, and it is why `--cuda-graphs` needs a separate graph for every number of animals. With `--fixed-crops`, every frame always gets `--max-instances` crops. When fewer animals are there, the missing ones get a padding crop whose keypoints are thrown away. Nothing in a set then depends on how many animals were found, so:

- the GPU runs the whole set without the CPU waiting for it in the middle;
- the results come back in one copy at the end, instead of four;
- with `--cuda-graphs` as well, the whole set (both models and everything between them) is a single recorded graph.

The animals found and the keypoints are sleap-nn's own. On the 5-mouse replay (3 cameras, 3,600 sets, the trained models below), `campy-live --fixed-crops` found the same animals as the default in every set, with identical centroids. In fp32 the keypoints were identical in every set too. In fp16 (the default precision), they were identical in every set where all 15 crops were real animals. In sets with fewer animals the instance model runs on a different number of crops, so fp16 rounds a little differently (99% of those sets within 3.3 px; a handful of keypoints jumped to a second, nearly equally bright spot).

sleap-nn 0.3.3 itself sometimes cuts every crop of a set one pixel too narrow or too short (it works out the crop size from a rounded subtraction): in 1,065 of the 3,600 sets of that replay. That changed some keypoints by up to tens of pixels. `--fixed-crops` always cuts crops at their real size. So its keypoints differ from the default's in those sets, and the comparison above is against sleap-nn with only that size corrected. `bench/fixed_crops_check.py` runs this comparison on any recorded videos.

On the EVO-X2, 5 mice, 3 cameras, 60 fps, trained models (centroid at 0.25 scale with output stride 2, instance on 320-px crops at 0.5 scale), three replays of each:

| | inference per set, p50 / p99 | frame to keypoints, p99 | frame to 3D, p50 / p99 | sets skipped (of 3,600) | graph memory |
|---|---|---|---|---|---|
| default | 15.1 / 17.1–17.5 ms | 28–29 ms | 17.1–17.5 / 30–31 ms | 4–8 | – |
| `--cuda-graphs` | 15.0 / 16.4–16.8 ms | 19–24 ms | 17.0–17.1 / 21–26 ms | 0–3 | 549 MB (16 graphs) |
| `--fixed-crops --cuda-graphs` | 13.6 / 14.2–14.3 ms | 15.3–15.6 ms | 16.0 / 18.0–18.8 ms | 1–3 | 210 MB (1 graph) |

So about 1.4 ms less per set, and a much steadier p99.

On the Precision 7875's RTX 6000 Ada (Windows), the same models and mice, one 30 s replay of each, it did far more:

| | inference per set, p50 / p99 | frame to keypoints, p50 / p99 | frame to 3D, p50 / p99 | sets skipped | graph memory |
|---|---|---|---|---|---|
| default | 10.3 / 12.8 ms | 12.0 / 14.5 ms | 13.3 / 16.0 ms | 0 | – |
| `--cuda-graphs` | 7.2 / 9.3 ms | 8.8 / 11.0 ms | 10.1 / 12.3 ms | 0 | 549 MB (16 graphs) |
| `--fixed-crops` | 8.7 / 11.1 ms | 10.3 / 12.8 ms | 11.7 / 14.3 ms | 0 | – |
| `--fixed-crops --cuda-graphs` | 2.7 / 4.5 ms | 4.3 / 6.2 ms | 5.7 / 7.4 ms | 0 | 229 MB (1 graph) |

Run alone, a set took 8.9 ms by default and 2.6 ms as one graph. On Windows, each wait for the GPU and each operation launched cost the CPU far more than the fast GPU needs to compute, so a set that runs as one graph with one wait is most of the gain. There, missing animals cost nothing measurable: 2.6 ms with 11 to 15 of 15. In fp16 the keypoints differed from sleap-nn's (crop size corrected) in only 5 of 3,600 sets. On the EVO-X2's AMD GPU the gain comes from removing the wait, not from the graph: run alone, a set took 14.3 ms by default, 12.8 ms with `--fixed-crops` and no graph, and 12.9 ms with the graph. `--fixed-crops` without `--cuda-graphs` skipped more sets live (8), because launching every operation without a pause kept the 3D thread waiting. Use the two together.

On the Pro Max Slim's RTX 2000 Ada (Windows), the same models and mice, three 30 s replays of each (about 1,790 sets in each), GPU clock held as `campy-live` does by default:

| | inference per set, p50 / p99 | frame to keypoints, p50 / p99 | frame to 3D, p50 / p99 | sets skipped | graph memory |
|---|---|---|---|---|---|
| default | 16.7–16.9 / 17.9–18.1 ms | 27.2–27.6 / 35.4–35.7 ms | 28.4–28.8 / 36.8–37.0 ms | 97–109 (5.4–6.1%) | – |
| `--cuda-graphs` | 16.2–16.3 / 17.0–17.2 ms | 26.3–26.8 / 34.8–34.9 ms | 27.4–27.9 / 36.0–36.3 ms | 42–49 | 531 MB (16 graphs) |
| `--fixed-crops` | 13.8–13.9 / 15.0–15.2 ms | 15.9–16.0 / 18.2–20.0 ms | 16.9 / 19.3–21.5 ms | 0–1 | – |
| `--fixed-crops --cuda-graphs` | 12.6 / 12.8 ms | 14.8 / 15.2–15.3 ms | 15.7 / 16.4 ms | 0 | 214 MB (1 graph) |

So 5 mice at 60 fps now fit on the Pro Max Slim with these models, with 4 ms to spare. By default each set took a little longer than the 16.7 ms between frames, so about one set in 17 was skipped and each 3D pose came a whole frame late. As one graph, a set takes 12.6 ms, nothing is skipped, and each 3D pose is ready 15.7 ms after its frames (p99 16.4 ms), a little sooner than on the EVO-X2. Run alone (`bench/fixed_crops_check.py`, 3,600 sets), a set took 18.2 ms by default, 14.4 ms with `--fixed-crops` and no graph, and 12.6 ms as one graph. Unlike on the EVO-X2, fewer animals didn't make the default faster: with 11 to 15 of 15 animals in view, the graph took 12.6 ms every time and the default 15.3–18.2 ms. The animals found and the centroids were the same as the default's in every set. With the crop size corrected, the fp16 keypoints were identical in every set with 14 or 15 animals and differed (by up to 3.3 px) in 24 sets with 11 or 13.

The catch, on a GPU that is itself the limit, like the EVO-X2's: every missing animal still costs a crop's worth of instance model. With 14 of 15 animals in view a set took 12.9 ms against the default's 13.7, about the same with 13, and with 12 the default was faster (12.2 against 12.9 ms). The benchmark puts 15 crops at 13.2 ms against 9.6 ms for 9, so with only 3 animals per frame the default would be about 3.5 ms faster. Set `--max-instances` to the number of animals you really have; then `--fixed-crops` pays.

## Testing without cameras

campy's emulated cameras (`cameraMake: "emu"`) stand in for real ones: each plays a recorded video in a loop, at `frameRate`, and `campy-acquire` records it and copies every frame into the live feed as it would a real camera's. This tests the whole pipeline, encoder included, with real animals and no hardware. Camera n plays `<videoFolder>/<cameraNames[n]>/<videoFilename without its first 3 characters>`, so with `videoFilename: "emuclip.mp4"` each camera plays the `clip.mp4` in its folder:
```
cameraMake: "emu"
numCams: 3
cameraNames: ["Camera1", "Camera2", "Camera3"]
videoFolder: "C:\\campy_data\\replay"     # Camera1\clip.mp4, Camera2\clip.mp4, Camera3\clip.mp4
videoFilename: "emuclip.mp4"
frameRate: 60
pixelFormatInput: "gray"                 # one channel per frame, as a Mono8 FLIR camera's
liveFeed: True
```
Each run's recording goes into a new folder inside `videoFolder`, as usual, and the clips stay where they are. As with a hardware trigger, campy starts every emulated camera at the same moment once all are ready, so frame n of each camera reaches the computer within a few milliseconds of the others', as with real cameras on a trigger. Each emulated camera used to start on its own, and `campy-live` counted every set as misaligned. Use videos of the same length, recorded together, so that each set of frames shows one moment. With campy 2.1.0rc6, three emulated cameras playing a 5-mouse session at 60 fps, recorded while `campy-live` tracked them in 3D, gave 0 skipped sets and frame to 3D pose in 13.3 ms on the Precision 7875, and kept all 3,600 frames per camera on each test machine (the hardware guide has all three). The README explains the details ([Emulated cameras](../README.md#emulated-cameras-cameramake-emu)).

## On Linux (AMD, ROCm)

- **First use of a model**: AMD's MIOpen library tunes every new convolution shape the first time it sees it, so the first warm-up of a new model can take about 10 s. The tuning is cached for later runs.
- **Disk stalls**: some SSDs stop completing writes for 10–16 s. The EVO-X2's config (`configs/campy_config_evox2.yaml`) holds up to 60 s of frames in memory (`writeQueueMaxSec: 60`) so a stall drops nothing. Better still, record to a second drive.
- **Encoding**: VA-API runs on the same GPU as inference. On the EVO-X2 that cost inference only a few percent.

## On Windows (NVIDIA, CUDA)

- **Sleep**: campy keeps Windows awake while it records (`keepAwake`, on by default). If campy is started over SSH while nobody is signed in, Windows refuses the screen part of that request: set the screen timeout to Never as well (Settings > System > Power).
- **Latencies in 15–16 ms steps (fixed)**: on Windows, up to Python 3.12, Python's `time.monotonic` moves in steps of 15.6 ms. campy used to take its host times from it (`hosttimes.npy`, the live feed, and `campy-live`'s timestamps), so the latencies `campy-live` reported there came in 15–16 ms steps (15, 16, 31, 32, 47 ms...). Since pull request 118, campy takes every host time from Windows' precise clock instead (`time.perf_counter_ns`, QueryPerformanceCounter, which Python 3.13 uses for `time.monotonic` anyway): one clock in every process, so the camera processes, `campy-live` and `campy-live-view` still compare their times directly, and latencies are exact to well under a millisecond (on the Pro Max Slim, a 60 fps recording's `hosttimes.npy` then showed frames 16.6 ms apart, and `campy-live` read each set 0.9 ms after its last frame arrived, where it used to say 0, 15 or 16 ms). Inference times were always precise. Latencies measured on Windows before this, such as the hardware guide's 31 / 32 ms, are rounded to those steps. A program that writes frames into the live feed itself (a replay script, say) must stamp them with the same clock, `campy.clock.HostNs`: with `time.monotonic_ns`, a different clock on Windows, the latencies `campy-live` reports mean nothing (40–56 ms in a replay whose real latency was 13 ms).
- **Encoding on the Intel iGPU** (Quick Sync, `gpuMake: "intel"`) keeps the encoder off the inference GPU.
- **The GPU's clock**: an NVIDIA GPU chooses its own clock speed from how busy it looks. Live tracking gives it a short burst of work for each frame, and that didn't look busy: on the Pro Max Slim's RTX 2000 Ada the clock sat at 780 MHz of its 2,500 while tracking, and fell to 210 MHz while `campy-live` waited for the recording to start. So `campy-live` keeps a tiny piece of the GPU busy, at the lowest priority, from the moment the feed appears until it ends (`--hold-gpu-clock`, on by default with NVIDIA and AMD GPUs). The models' work goes first, so it isn't slowed. That took the time per set from 27.6 to 13.2 ms with 3 mice in view, so the same models now fit 60 fps, and the first sets of a recording are no longer slow. The cost is about 20 W more while waiting. `--hold-gpu-clock off` turns it off. While the frames come, `campy-live` reads the GPU's clock every half second and spins 0, 1 or 2 blocks, as many as keep the clock up (`--hold-gpu-clock-blocks auto`, or a fixed number): one block costs the models about 0.3 ms per set, so none spin when the GPU keeps its clock by itself, as the Pro Max Slim's card did right after a restart. It reads the clock through NVIDIA's NVML (the `nvidia-ml-py` package, if installed in campy-live's environment), else `nvidia-smi` (a small program that `campy-live` starts and ends; it ends with `campy-live` even when `campy-live` is killed or crashes, so it never keeps polling the GPU or holds a folder open after), and on AMD from the driver. AMD's GPUs slow down too, less: the EVO-X2's ran the default models at 2,117 of its 2,900 MHz, 9.9 ms per set instead of 8.9, so the hold is on there by default as well.
- **Hybrid Intel CPUs**: `campy-live` runs itself on the performance cores at High priority, and moves campy-acquire's camera processes and encoders onto the efficiency cores when their feed appears (`--cpus auto`, `--priority auto` and `--campy-cpus auto`, the defaults). With 3 cameras recording, that saved about 2 ms per set. `python -m campy.cpus` lists which cores are which. `--cpus none --campy-cpus none` leaves both programs where Windows puts them. The move needs a campy-acquire from this version on, whose live feed reports its processes; with an older one, `campy-live` says so and leaves campy-acquire alone.
- **CUDA graphs** (`--cuda-graphs`, off by default): each model's GPU work is recorded once and then replayed in one go. The keypoints are identical; on the RTX 2000 Ada it saved about 0.8 ms per set (13.2 to 12.4 ms with 3 mice). Each number of animals found gets its own graph (up to 15 at 3 cameras with up to 5 animals each, 40 at 8), and all of a model's graphs share one block of GPU memory. On the RTX 6000 Ada, `campy-live`'s graphs hold 0.26 GB at 3 cameras and 0.65 GB at 8; with a block per graph, as before, they held 1.7 and 11.2 GB, for the same time per set. `campy-live` says how much its graphs hold when it starts.
- **channels-last** memory layout is the default on NVIDIA GPUs (`--memory-format auto`), and is faster there.
- **A monitor** plugged into the inference GPU slowed it noticeably. If the machine has two GPUs, plug the monitor into the other one.
- **Two GPUs**: run `campy-live` on one and let campy's encoders use the other. torch numbers GPUs fastest first unless told otherwise, and `nvidia-smi` by their slot, so start `campy-live` with `CUDA_DEVICE_ORDER=PCI_BUS_ID` and `CUDA_VISIBLE_DEVICES=<the GPU's nvidia-smi index>`. Give the cameras the other GPU's `gpuID` for NVENC. On the Precision 7875 that is `CUDA_VISIBLE_DEVICES=0` (RTX 6000 Ada) for `campy-live` and `gpuID: 1` (RTX 4000 Ada, which also drives the monitor) for the encoders; while it recorded, `nvidia-smi` listed `campy-live` only on the first and ffmpeg only on the second.
