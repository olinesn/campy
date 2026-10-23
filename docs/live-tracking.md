# Live tracking with campy-live

Three programs work together on the computer that records:

- **`campy-acquire`** records the cameras as usual. With `liveFeed: True` it also copies each frame into shared memory, the *live feed*.
- **`campy-live`** reads the feed and runs a sleap-nn top-down model on each synchronized set of frames, one frame per camera. With a calibration, it also matches the animals across cameras, keeps their identities and triangulates them in 3D. It is never more than one set behind: a set that arrives while it is busy is skipped and counted, never queued.
- **`campy-live-view`** (optional) shows the camera frames with the tracking drawn on them, plus a 3D view.
- **`campy-rules`** (optional) switches TTL outputs from the poses, for closed-loop experiments: see [Closed-loop outputs](closed-loop.md).

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

### In one command: `campy-session`

`campy-session` starts all three programs, each in its own environment, from one config, and stops them together. Add a `live:` section to the recording's config, and a `view:` section if you like:

```yaml
# ... the recording's keys, as for campy-acquire ...
live:
  python: "C:\\Users\\<you>\\miniconda3\\envs\\campyLive\\python.exe"   # optional, see below
  centroid: "C:\\campy_data\\live\\models\\centroid"
  instance: "C:\\campy_data\\live\\models\\instance"
  animals: 5
  calibration: "C:\\campy_data\\calib\\calibration.toml"
  env: {CUDA_DEVICE_ORDER: PCI_BUS_ID, CUDA_VISIBLE_DEVICES: "0"}   # two GPUs: campy-live on the first
  args: ["--report-sec", "30"]   # any other campy-live option, as a list
view: true                       # or: view: {args: ["--no-3d"], keep_open: true}
```

Then, in the **recording** environment:

```
campy-session run <config>.yaml
```

Options after the config are campy-acquire's, for this run only (`campy-session run <config>.yaml --recTimeInSec 3600`).

**Telling it where the inference environment is.** `campy-session` runs `campy-live` with that environment's python, so neither environment needs activating and nothing needs to be on PATH. It looks in three places, in order:

1. `live.python` in the config;
2. the `CAMPY_LIVE_PYTHON` environment variable;
3. the python registered once per computer: in the inference environment, run `campy-session register-live` (or `python -m campy.session register-live`). It writes that python to `~/.campy/envs.json`.

Both environments must have the same campy installed, or `campy-session` stops before anything starts and says so. It compares their live feed code.

**What happens:**

1. It reads the config exactly as campy-acquire does ([Configs](configs.md)): the same checks, before anything starts, and a config built on a rig profile (`extends:`) works too, its `live:`, `view:` and `post:` sections combined with the profile's one level deep. It also checks those sections' own keys, and suggests the key you meant for a typo. `view: true` and `view: false` are short for `view: {show: true}` and `view: {show: false}`.
2. It makes the recording's folder (`<videoFolder>/<date and time>/`, as campy-acquire would). Into its `logs/` folder it writes:
   - the config as given (`session-config.original.yaml`);
   - the same combined into one file (`session-config.yaml`): every setting, with the profile and the command-line options in it, each line saying where its value came from. `campy-session post` reads it again;
   - the config campy-acquire runs (`campy-acquire.yaml`): the same, with `liveFeed: True` when something reads the feed.
3. It starts `campy-live` and waits until its models are warmed up: 5–40 s, longest the first time an engine is built (`live.ready_timeout_s`, default 120 s).
4. It starts `campy-live-view` (with `view`), then `campy-acquire`, which records into that folder. `campy-live` tracks from its first frame.
5. The recording ends at `recTimeInSec`, or at Ctrl-C. `campy-live` tracks to the last frame and ends by itself. The view closes, unless `keep_open` is set.

Each program's output is shown in the terminal, each line marked `[acquire]`, `[live]` or `[view]`, and kept in `<folder>/logs/<program>.log`. `campy-live`'s results go into the recording's folder (`campy-live.npy` and `.json`), ready for `campy-live-export`. `<folder>/launcher.json` records when each program started and ended, and its exit code. The exit code is campy-acquire's: 0 when every camera recorded to the end.

**The recording comes first.**

- `campy-acquire` runs as it does when started by hand. Ctrl-C reaches it directly, and it is never killed. If `campy-session` itself is closed or crashes, the recording goes on to `recTimeInSec`, and `campy-live` keeps tracking it.
- If `campy-live` can't start (a wrong model folder, say), the recording goes on without tracking. With `live.required: true` instead, nothing is recorded.
- If `campy-live` stops during the recording, the cameras carry on, and `campy-session` starts it again after 10 s, then 1 and 5 minutes, at most 5 times an hour. Each restart writes its own log (`campy-live.part2.npy`, ...). `live.restart: false` turns this off.
- One `campy-session` runs at a time on a computer.

**After the recording.** When every camera has recorded to the end, `campy-session` runs the steps of the config's `post:` section, and logs each one in `<folder>/logs/post.log`. Without a `post:` section, it runs `remux` and `export`:

```yaml
post:
  remux: true          # true (the default), keep, or false
  export: true         # campy-live-export, when campy-live ran (the default)
  copy: "\\\\nas\\share\\recordings"   # optional: copies the recording's folder into this one
  run: ["python C:\\tools\\notify.py {folder}"]   # optional: your own commands
```

- **remux** turns each fragmented video (campy's default, readable after a crash) into a plain MP4 without re-encoding. Some readers have trouble with fragmented files (an older calibrat3 did), and a fragmented file's header has no frame count.
  - Before anything is replaced, ffmpeg's packet checksums of the two files are compared line by line: every packet, timestamp and the codec's header must be identical. Only then does the plain file replace the fragmented one.
  - With `remux: keep`, the fragmented file stays beside it as `<name>.fragmented.mp4`.
  - If the check fails, or the disk has no room for one more copy of the video, the original stays as it was, and the log says why.
  - A 30 s video of 3 cameras takes a fraction of a second.
- **export** runs `campy-live-export` in the inference environment, so the recording opens in LUC3D with the tracking on it (below). If `campy-live` was started again during the recording, its logs are joined first, in order.
- **copy** copies the whole recording folder into `copy.to`: with robocopy on Windows, with rsync on Linux and macOS (rsync also takes `user@host:/path`). Then it checks every file's size. **Nothing is deleted**, on either side.
- **run** runs your own commands in a shell, with `{folder}` replaced by the recording's folder. One that fails is logged, and the next one still runs.

After a crash, or when a camera didn't record to the end, none of them run, and `campy-session` says why. The recording's fragmented videos stay readable up to their last second. To run the steps again by hand, or after looking at a crashed recording:

```
campy-session post <recording folder>                 # the steps not done yet
campy-session post <recording folder> --only remux    # one step
campy-session post <recording folder> --force         # after a crash, or again
```

`campy-session post` also works on a recording made with `campy-acquire` alone. There, every camera's `metadata.csv` must have its totals and no `cameraError` or `videoError`, unless you give `--force`. What each step did is kept in `<folder>/logs/post.json`, so running `post` again repeats nothing that is done. A Ctrl-C during the steps stops them after the current one.

### From a page in the browser: `campy-session panel`

For people who'd rather not use a terminal, the same recording can be run from a page in the browser:

```
campy-session panel <config>.yaml
```

It opens the page in this computer's browser, and prints its address. A recording starts only when someone presses **Start recording** on the page, never on its own. Its length is the config's `recTimeInSec`. After each recording the page is ready for the next one, into a new folder. Ctrl-C in the panel's console closes it, when nothing is recording.

What the page shows, refreshed every second:

- **Before the start:**
  - the experiment's name, the animal IDs and notes, which go into the recording's `session.json`;
  - the checks. Some stop a start: `campy-live`'s environment not found or not the same campy, or another `campy-session` running. Others only warn: less free disk space than `alertDiskFreeGB`.
- **While it records:**
  - **time:** elapsed, against the planned length;
  - **each camera:** a grey thumbnail, refreshed once a second; its frames per second and frame number; and its health, from the same checks `campy-live-view` draws in red: `NO FRAMES`, behind, `LOST`, `GAP`, `LATE`, frames ahead or behind. When campy-acquire's watch is running, also its dropped frames and write queue;
  - **disk:** free space, and roughly how long it lasts at the rate it's filling;
  - **tracking:** how many animals `campy-live` tracked in 3D over the last second;
  - **warnings:** from the alerts' logs in the recording's folder, and anything `campy-session` reported;
  - **bookmarks:** a note box and an Add button. They write the same `bookmarks.csv` in the recording's folder as `campy-live-view`'s `m` key, and the two never give two bookmarks the same number;
  - **programs:** each one's state, and a button that opens `campy-live-view` again if it was closed.
- **After it:** each step of `post:`, as it runs.

**Stop** stops the recording exactly as Ctrl-C does: each camera closes its video. If a camera hangs while closing, the button turns into **Stop now, without closing**, for a few seconds. Closing the page stops nothing, and the page can be opened again at any time.

The page is served only to this computer (`127.0.0.1`):

- **Over Remote Desktop** (Windows), open it in the rig's own browser.
- **From another computer**, go through SSH: run `ssh -L 8765:127.0.0.1:8765 <rig>`, then open the printed address on your own computer.

Every request needs the random key in that address, so no other program on the computer can press Start or Stop. `--port` picks another port, and `--no-browser` only prints the address.

**Closing the panel's window never ends a recording.** The panel starts `campy-acquire` and `campy-live` apart from its own console: on Windows in a hidden console of their own, on Linux in a session of their own. So closing the window, or the terminal, or the panel crashing, leaves the recording going to its end, with `campy-live` tracking it. Stop and Ctrl-C in the panel's window reach `campy-acquire` through its stop file. A panel opened again while such a recording is still running says so, refuses to start another (two recordings can't share the cameras), and offers **Stop that recording**. `campy-session run` in a terminal keeps its own behaviour: there, Ctrl-C reaches `campy-acquire` directly.

**A desktop icon** for labmates: `campy-session shortcut <config>.yaml --name "Record: 3 cameras"` makes one. It's a shortcut on Windows, a `.desktop` file on Linux and a `.command` file on macOS. Double-clicking it opens the panel with that config. [Recording a session from the desktop](recording-a-session.md) is the page to give them.

### By hand

The three programs can also be started separately, as `campy-session` does:

1. **Start `campy-live` first**, in the inference environment:
   ```
   campy-live --cameras 3 --model sleap --centroid <centroid model folder> --instance <centered-instance model folder> \
       --animals 5 --calibration calibration.toml
   ```
   It loads the models and warms them up before any frame exists, compiling the networks if it can ([Compiled networks](#compiled-networks---engine-on-by-default-with-a-gpu)), then prints `Waiting for the live feed`. On a computer with performance and efficiency cores (Intel Core Ultra and 12th-generation Core or later) it first moves itself onto the performance cores at High priority, and once the feed appears it moves campy-acquire, its camera processes and their encoders onto the efficiency cores (see below, On Windows). The wait is 5–8 s on the EVO-X2 and up to about 18 s on Windows, mostly imports. The first start with a new model, GPU or update takes 13–35 s longer while the compiled networks are made; they are kept, and later starts take 0–4 s longer than without. It assumes 1280 × 1024 grey frames. For other cameras, give `--frame-shape <height>x<width>x<channels>`, or it builds the model again when the frames arrive and misses the first seconds.
2. **Start the recording** with the live feed on:
   ```
   campy-acquire <config>.yaml --liveFeed True
   ```
   Because `campy-live` is already waiting, it analyses the recording from the first frame. It looks for the feed every 20 ms and takes its first set at once, writing `campy-live-session.json` in the background. On the Precision 7875, with three emulated cameras at 60 fps and the trained models with 3D, it tracked from frame 1 both ways, by hand and through `campy-session`, with all 1,200 sets tracked and none skipped. On dev between pull requests 163 and 183 (never in a release), writing that file came first, and the first 64–73 frames, 1.1–1.2 s, went untracked.
3. **Optionally, watch it** in the inference environment:
   ```
   campy-live-view
   ```
   It runs as a separate program at low priority and stays open from one recording to the next. Its `c` key, or the "Fast CPUs" box in its panel, moves `campy-live` on and off the performance cores while both run, and the panel shows `campy-live`'s inference time over the last 2 s, so you can see the difference. On a computer without a screen (over SSH), `campy-live-view --no-window --png <folder>` saves pictures instead.
4. **For an unattended recording, also start `campy-watch`**, in either environment:
   ```
   campy-watch --cameras 3
   ```
   It runs campy-live-view's checks without a window, and sends what lasts as a Slack message ([Alerts](alerts.md)): a camera a frame ahead or behind the others, frames lost or late, good 3D for fewer animals than `--animals` for most of 3 minutes, and `campy-live` stopping while the cameras record. It waits for the feed and ends with the recording.

To start the recording from a script once `campy-live` is ready, give `campy-live --ready-file <path>`: it writes that file as it prints `Waiting for the live feed`. To stop a recording that was started without a console, or detached over SSH, give `campy-acquire` a stop file (`--stopFile <path>`) and create that file: campy stops as at Ctrl-C, and `campy-live` analyses the recording to its last frame, as it does when a recording ends by itself ([Stop Recording](../README.md#stop-recording)).

`campy-live` ends with the recording, or at Ctrl-C. It writes `campy-live_<date_time>.npy`, with one row per set processed (keypoints, confidences, and the 3D poses and identities with `--calibration`), and a `.json` summary. It also writes `campy-live-session.json` into the recording's folder: which models it ran (named by the SHA-256 of `best.ckpt`), with which calibration, options and environment ([Recording folders](recordings.md#campy-live-sessionjson)). To go through the results frame by frame, open the recording in LUC3D (below).

`campy-live` runs the models in fp16 (`--precision fp16`, the default on NVIDIA and AMD GPUs). sleap-nn's own centroid peak finder lost about 0.7% of the mice per frame in fp16, wherever the two top pixels of a centroid rounded to the same value. `campy-live` keeps such a peak once instead, so fp16 finds the mice fp32 finds; `--strict-peaks` goes back to sleap-nn's own (see [Models](models.md)).

## Checking that it keeps up

The summary, and the reports it prints every 10 s, give the sets processed and skipped, the inference time per set and the latency from frames to keypoints and to 3D. At 60 fps a set must take less than 16.7 ms, and at 30 fps less than 33.3 ms.

- **Sets skipped through the whole recording**: the model is too slow for this machine and frame rate. Choose a lighter one ([Models](models.md)).
- **A few skipped in the first fraction of a second**: the GPU was still at its idle clock when the frames came. `campy-live` holds the GPU's clock up from the moment the feed appears (`--hold-gpu-clock`, on by default with NVIDIA and AMD GPUs), so this should no longer happen.
- **"misaligned" sets**: one camera's frames reached the computer late, or a camera lost a frame (`flirFramesLost` in its `metadata.csv`). See the README on frame numbers.

For a test without animals or without a trained model, `--model random` runs untrained models of the benchmark's default sizes, and `--model none` only reads the feed.

## Before and after the speed-ups of 22–23 October 2026

The three test machines' rig checks, run at each step with `campy-live`'s defaults and no options: the same scripts, models and 5-mouse clips every time. A set is one frame from each camera.

| Code | What it added |
|---|---|
| 2.1.0rc6 (before) | `campy-live` as it was on the morning of 22 October: sleap-nn's own top-down layer, the networks in plain PyTorch fp16 |
| 2.1.0 | fixed crops and one CUDA graph per set (pull requests 126 and 134), faster upsampling on AMD (138), one block of clock hold on the EVO-X2 (141) |
| pull request 147 | the two networks compiled by default (`--engine auto`: TensorRT on NVIDIA, `torch.compile` on the EVO-X2) |
| now | the crops cut with one gather (pull request 151), and the clock hold's two hiccups fixed (152); dev at acdf7b6, measured on 23 October |

In the tables below:

- **Time per set**: from the upload of the frames to the GPU to the 2D results being back on the CPU (`inference_ms` in `campy-live`'s summary). At 60 fps it has to stay under 16.7 ms, or sets are skipped.
- **Frame to 2D** and **frame to 3D**: from the moment the last camera's frame reached the computer (campy-acquire's arrival stamp, or the replay feeder's) until the set's 2D results, or its 3D poses, were ready. These add noticing the set (up to 0.5 ms), the copy out of shared memory (0.2–0.3 ms), any wait while the GPU was still busy with the set before, and the 3D step.
- **3D step**: the 3D calculation alone, which runs on its own thread while the GPU starts the next set.
- p50 is the median, p99 the time 99% of sets stayed under, max the slowest set of the run. Times in ms.

### Recording 3 emulated cameras while tracking 5 mice in 3D (60 s, 3 × 3,600 frames recorded every time)

The EVO-X2 encodes with VA-API on the same GPU that runs the models, the Precision 7875 with NVENC on its RTX 4000 Ada (the models run on the RTX 6000 Ada), and the Pro Max Slim with Quick Sync.

| Machine | Code | Skipped | Time per set p50 / p90 / p99 / max | Frame to 2D p50 / p99 / max | Frame to 3D p50 / p99 / max | 3D step |
|---|---|---|---|---|---|---|
| EVO-X2 | 2.1.0rc6 | 3 | 15.4 / 16.2 / 17.3 / 64.3 | 16.9 / 21.5 / 65.7 | 18.6 / 23.1 / 68.6 | 1.36 |
| | 2.1.0 | 3 | 10.8 / 10.9 / 11.1 / 61.1 | 11.6 / 12.2 / 71.6 | 12.8 / 13.7 / 74.1 | 1.10 |
| | pull request 147 | 2 | 6.1 / 6.2 / 6.3 / 56.5 | 6.8 / 7.6 / 64.5 | 8.1 / 9.1 / 66.6 | 1.07 |
| | **now** | **0** | **5.6 / 5.7 / 5.8 / 7.6** | **6.3 / 7.0 / 9.1** | **7.5 / 8.4 / 10.9** | 1.06 |
| Pro Max Slim (RTX 2000 Ada) | 2.1.0rc6 | 212 (5.9%) | 16.8 / 17.2 / 17.8 / 20.7 | 26.4 / 34.5 / 36.2 | 27.6 / 35.8 / 37.9 | 0.98 |
| | 2.1.0 | 0 | 12.6 / 12.7 / 12.8 / 13.9 | 13.8 / 14.7 / 21.2 | 14.8 / 15.7 / 23.5 | 0.90 |
| | pull request 147 | 0 | 5.9 / 6.1 / 6.5 / 11.2 | 7.1 / 7.9 / 13.4 | 8.2 / 9.3 / 14.5 | 1.03 |
| | **now** ¹ | **0** | **5.5 / 5.6 / 5.7 / 6.4** | **6.7 / 7.1 / 14.2** | **7.7 / 8.4 / 16.0** | 0.98 |
| Precision 7875 (RTX 6000 Ada) | 2.1.0rc6 | 0 | 10.8 / 12.2 / 14.5 / 18.5 | 11.9 / 15.8 / 33.3 | 13.3 / 17.4 / 37.2 | 1.28 |
| | 2.1.0 | 0 | 2.7 / 2.8 / 4.8 / 5.1 | 3.9 / 6.0 / 20.3 | 5.3 / 7.3 / 23.8 | 1.31 |
| | pull request 147 | 0 | 1.6 / 1.7 / 3.7 / 6.0 | 2.8 / 4.9 / 7.5 | 4.1 / 6.2 / 10.2 | 1.24 |
| | **now** | **0** | **1.6 / 1.7 / 1.8 / 3.0** | **2.7 / 3.4 / 18.3** | **4.1 / 5.3 / 21.6** | 1.28 |

### Replaying the 5-mouse recording, 3 cameras, 3D (EVO-X2 60 s, the Windows machines 30 s)

| Machine | Code | Sets (skipped) | Time per set p50 / p90 / p99 / max | Frame to 2D p50 / p99 / max | Frame to 3D p50 / p99 / max | 3D step |
|---|---|---|---|---|---|---|
| EVO-X2 | 2.1.0rc6 | 3,600 (0) | 15.0 / 15.6 / 16.6 / 24.5 | 15.7 / 18.2 / 29.4 | 16.9 / 19.8 / 30.5 | 1.15 |
| | 2.1.0 | 3,597 (3) | 10.6 / 10.7 / 10.8 / 60.8 | 11.2 / 11.6 / 73.0 | 12.2 / 13.0 / 75.3 | 0.91 |
| | pull request 147 | 3,598 (2) | 5.9 / 6.0 / 6.1 / 53.8 | 6.5 / 7.0 / 57.5 | 7.5 / 8.5 / 59.8 | 0.86 |
| | **now** | **3,600 (0)** | **5.5 / 5.5 / 5.6 / 9.6** | **6.1 / 6.5 / 11.6** | **7.1 / 8.1 / 14.2** | 0.89 |
| Pro Max Slim | 2.1.0rc6 | 1,689 (97, 5.4%) | 16.7 / 17.2 / 17.8 / 19.0 | 27.4 / 35.5 / 37.2 | 28.5 / 36.8 / 39.6 | 1.00 |
| | 2.1.0 | 1,788 (0) | 12.6 / 12.7 / 12.8 / 14.1 | 14.8 / 15.4 / 27.3 | 15.7 / 16.5 / 29.6 | 0.86 |
| | pull request 147 | 1,783 (0) | 5.8 / 5.9 / 6.6 / 6.8 | 8.1 / 8.9 / 12.7 | 9.1 / 10.0 / 15.0 | 0.91 |
| | **now** ¹ | **1,787 (0)** | **5.5 / 5.6 / 5.7 / 6.5** | **7.7 / 8.2 / 24.8** | **8.7 / 9.4 / 27.1** | 0.91 |
| Precision 7875 | 2.1.0rc6 | 1,788 (0) | 10.0 / 11.1 / 12.4 / 16.0 | 11.6 / 14.1 / 19.0 | 12.9 / 15.5 / 21.8 | 1.15 |
| | 2.1.0 | 1,787 (0) | 2.7 / 2.8 / 4.7 / 4.8 | 4.2 / 6.5 / 19.9 | 5.7 / 7.7 / 22.9 | 1.31 |
| | pull request 147 | 1,789 (0) | 1.6 / 1.7 / 5.7 / 5.8 | 3.3 / 7.1 / 9.6 | 4.7 / 8.3 / 11.9 | 1.32 |
| | **now** | **1,788 (0)** | **1.6 / 1.7 / 1.7 / 2.0** | **3.1 / 4.1 / 10.9** | **4.7 / 5.8 / 13.1** | 1.34 |

### Recording 1 emulated camera while tracking (no 3D; EVO-X2 30 s, the Windows machines 20 s)

| Machine | Time per set, 2.1.0rc6, p50 / p99 / max | Time per set, now | Frame to 2D, 2.1.0rc6, p50 / p99 | Frame to 2D, now |
|---|---|---|---|---|
| EVO-X2 | 6.4 / 7.5 / 9.4 | **3.0 / 3.1 / 5.2** | 6.9 / 8.0 | **3.4 / 3.8** |
| Pro Max Slim ¹ | 9.1 / 10.1 / 10.8 | **2.3 / 2.4 / 2.8** | 10.0 / 11.1 | **3.3 / 3.5** |
| Precision 7875 | 9.0 / 12.5 / 21.3 | **0.9 / 1.0 / 1.1** | 9.8 / 13.7 | **1.9 / 2.3** |

### Accuracy (EVO-X2 replay, live against offline SLEAP on the same frames)

| Code | 2D error, median / p90 | 3D error, median / p90 | Same identity | Identity switches |
|---|---|---|---|---|
| 2.1.0rc6 | 3.8 / 9.6 px | 4.3 / 10.6 mm | 99.93% | 2 |
| 2.1.0 | 3.8 / 9.3 px | 4.2 / 10.6 mm | 99.93% | 2 |
| pull request 147 | 3.8 / 9.3 px | 4.2 / 10.6 mm | 99.93% | 2 |
| now | 3.8 / 9.3 px | 4.2 / 10.6 mm | 99.93% | 2 |

- **A set takes 2.7 times less time on the EVO-X2, 3 times less on the Pro Max Slim and 6 times less on the RTX 6000 Ada**, with the same tracking accuracy.
- **The Pro Max Slim now keeps up with 5 mice at 60 fps.** Before, each set took just over the 16.7 ms between frames, so the next frame waited and about 1 set in 18 was skipped, and each 3D pose came 28 ms after its frames. Now nothing is skipped and 3D comes 7.7–8.7 ms after.
- **The slow outliers are gone.** On the EVO-X2 the first set of every run used to take 54–64 ms and the frame after it was skipped; now the slowest set is 7.6–9.6 ms and nothing is skipped. On the RTX 6000 Ada, p99 went from 3.7–5.7 to 1.7–1.8 ms.
- **The machines are busy about a third of the time or less.** While recording 3 cameras, the GPU work and the copy out of shared memory take about 6 ms of each 16.7 ms on the EVO-X2 and the Pro Max Slim, and about 2 ms on the RTX 6000 Ada.
- **On the Windows machines one or two sets per run still take 13–27 ms from frame to 3D** (the "max" column), while their time per set stayed normal: the delay is outside the GPU work, and not measured yet.

Notes on the measurements:

- Every run was `RIG_CLEAN` except ¹. The Pro Max Slim's "now" runs were marked `RIG_CONTAMINATED` by one sighting, in the job's first second, before its first check started: 164 MB/s of disk from another test that had just ended. A first attempt shortly before, during which Windows Update and the app-store service were busy (up to 3.6 cores), gave the same times (5.5 / 5.7 ms per set while recording), and so did that PR's own clean runs (5.49 / 5.63 ms). The Pro Max Slim's own rig checks after its update to dev df15047 (pull request 158; 23 October, campy installed from the rig's checkout) were all `RIG_CLEAN` and gave the same times: 5.5 / 5.7 ms per set recording 3 cameras (frame to 3D 7.7 / 8.4 ms), 5.5 / 5.7 ms replaying (8.7 / 9.4 ms), and 2.3 / 2.4 ms with 1 camera, with nothing skipped or misaligned and every camera keeping all its frames.
- The Windows machines' 2.1.0rc6 replays are the reruns made after the replay feeder was changed to stamp its frames with campy's own host clock. The first runs' frame-to-pose times (41–57 ms) compared two different clocks and mean nothing; their time per set was the same.
- The "now" runs used a copy of dev's code next to each rig's installed campy; the earlier rows are the rigs' own checks after each update.

## Fixed crops (`--fixed-crops`, on by default with a GPU)

Normally `campy-live` runs the centroid model, waits for the GPU to say how many animals it found, and only then cuts that many crops for the instance model. That wait in the middle of every set costs time, and it is why `--cuda-graphs` needs a separate graph for every number of animals. With `--fixed-crops`, every frame always gets `--max-instances` crops. When fewer animals are there, the missing ones get a padding crop whose keypoints are thrown away. Nothing in a set then depends on how many animals were found, so:

- the GPU runs the whole set without the CPU waiting for it in the middle;
- the results come back in one copy at the end, instead of four;
- with `--cuda-graphs` as well, the whole set (both models and everything between them) is a single recorded graph.

Both are on by default with a GPU (NVIDIA's CUDA or AMD's ROCm): `--fixed-crops` and `--cuda-graphs` are `auto`, which means on with a GPU and off on the CPU or a Mac's MPS. A model that fixed crops can't run (sleap-nn's centroid NMS, ground-truth centroids, multi-class models, or refinement other than integral or none) falls back to sleap-nn's own top-down layer, with a graph for each number of animals, and `campy-live` says why when it starts. `--fixed-crops off --cuda-graphs off` gives the old behaviour, and `--fixed-crops on` refuses to start with a model it can't run. In the tables below, "both off" was the default before.

Checked with no options at all, on the 5-mouse replay with 3D (22 October): the Pro Max Slim took 12.6 / 12.9 ms per set, skipped nothing, and had each 3D pose 15.8 ms after its frames (with both off: 16.9 / 18.1 ms, 110 of 1,785 skipped, 28.5 ms); the Precision 7875's RTX 6000 Ada 2.7 / 4.9 ms, 3D after 5.7 ms (both off: 10.2 / 12.6 ms, 13.2 ms); the EVO-X2 13.6 / 14.1 ms, 3D after 15.8 / 18.4 ms at p50 / p99 (both off: 15.2 / 17.2 ms, 17.5 / 30.7 ms), 3 of 3,600 skipped either way.

The animals found and the keypoints are sleap-nn's own. On the 5-mouse replay (3 cameras, 3,600 sets, the trained models below), `campy-live --fixed-crops` found the same animals as sleap-nn's own layer (`--fixed-crops off`) in every set, with identical centroids. In fp32 the keypoints were identical in every set too. In fp16 (the default precision), they were identical in every set where all 15 crops were real animals. In sets with fewer animals the instance model runs on a different number of crops, so fp16 rounds a little differently (99% of those sets within 3.3 px; a handful of keypoints jumped to a second, nearly equally bright spot).

sleap-nn 0.3.3 itself sometimes cuts every crop of a set one pixel too narrow or too short (it works out the crop size from a rounded subtraction): in 1,065 of the 3,600 sets of that replay. That changed some keypoints by up to tens of pixels. `--fixed-crops` always cuts crops at their real size, so the comparison above is against sleap-nn with only that size corrected. Since pull request 131 sleap-nn's own layer, as `campy-live --fixed-crops off` runs it, cuts them at their real size too (next section). `bench/fixed_crops_check.py` runs this comparison on any recorded videos, and counts the sets where sleap-nn alone would have cut its crops short.

On the EVO-X2, 5 mice, 3 cameras, 60 fps, trained models (centroid at 0.25 scale with output stride 2, instance on 320-px crops at 0.5 scale), three replays of each:

| | inference per set, p50 / p99 | frame to keypoints, p99 | frame to 3D, p50 / p99 | sets skipped (of 3,600) | graph memory |
|---|---|---|---|---|---|
| both off (the old default) | 15.1 / 17.1–17.5 ms | 28–29 ms | 17.1–17.5 / 30–31 ms | 4–8 | – |
| `--fixed-crops off` (graphs per count) | 15.0 / 16.4–16.8 ms | 19–24 ms | 17.0–17.1 / 21–26 ms | 0–3 | 549 MB (16 graphs) |
| both on (the default now) | 13.6 / 14.2–14.3 ms | 15.3–15.6 ms | 16.0 / 18.0–18.8 ms | 1–3 | 210 MB (1 graph) |

So about 1.4 ms less per set, and a much steadier p99.

On the Precision 7875's RTX 6000 Ada (Windows), the same models and mice, one 30 s replay of each, it did far more:

| | inference per set, p50 / p99 | frame to keypoints, p50 / p99 | frame to 3D, p50 / p99 | sets skipped | graph memory |
|---|---|---|---|---|---|
| both off (the old default) | 10.3 / 12.8 ms | 12.0 / 14.5 ms | 13.3 / 16.0 ms | 0 | – |
| `--fixed-crops off` (graphs per count) | 7.2 / 9.3 ms | 8.8 / 11.0 ms | 10.1 / 12.3 ms | 0 | 549 MB (16 graphs) |
| `--cuda-graphs off` (fixed crops, no graph) | 8.7 / 11.1 ms | 10.3 / 12.8 ms | 11.7 / 14.3 ms | 0 | – |
| both on (the default now) | 2.7 / 4.5 ms | 4.3 / 6.2 ms | 5.7 / 7.4 ms | 0 | 229 MB (1 graph) |

Run alone, a set took 8.9 ms with both off and 2.6 ms as one graph. On Windows, each wait for the GPU and each operation launched cost the CPU far more than the fast GPU needs to compute, so a set that runs as one graph with one wait is most of the gain. There, missing animals cost nothing measurable: 2.6 ms with 11 to 15 of 15. In fp16 the keypoints differed from sleap-nn's (crop size corrected) in only 5 of 3,600 sets. On the EVO-X2's AMD GPU the gain comes from removing the wait, not from the graph: run alone, a set took 14.3 ms with both off, 12.8 ms with `--fixed-crops` and no graph, and 12.9 ms with the graph. `--fixed-crops` without `--cuda-graphs` skipped more sets live (8), because launching every operation without a pause kept the 3D thread waiting. Use the two together.

On the Pro Max Slim's RTX 2000 Ada (Windows), the same models and mice, three 30 s replays of each (about 1,790 sets in each), GPU clock held as `campy-live` does by default:

| | inference per set, p50 / p99 | frame to keypoints, p50 / p99 | frame to 3D, p50 / p99 | sets skipped | graph memory |
|---|---|---|---|---|---|
| both off (the old default) | 16.7–16.9 / 17.9–18.1 ms | 27.2–27.6 / 35.4–35.7 ms | 28.4–28.8 / 36.8–37.0 ms | 97–109 (5.4–6.1%) | – |
| `--fixed-crops off` (graphs per count) | 16.2–16.3 / 17.0–17.2 ms | 26.3–26.8 / 34.8–34.9 ms | 27.4–27.9 / 36.0–36.3 ms | 42–49 | 531 MB (16 graphs) |
| `--cuda-graphs off` (fixed crops, no graph) | 13.8–13.9 / 15.0–15.2 ms | 15.9–16.0 / 18.2–20.0 ms | 16.9 / 19.3–21.5 ms | 0–1 | – |
| both on (the default now) | 12.6 / 12.8 ms | 14.8 / 15.2–15.3 ms | 15.7 / 16.4 ms | 0 | 214 MB (1 graph) |

So 5 mice at 60 fps now fit on the Pro Max Slim with these models, with 4 ms to spare. With both off, the old default, each set took a little longer than the 16.7 ms between frames, so about one set in 17 was skipped and each 3D pose came a whole frame late. As one graph, a set takes 12.6 ms, nothing is skipped, and each 3D pose is ready 15.7 ms after its frames (p99 16.4 ms), a little sooner than on the EVO-X2. Run alone (`bench/fixed_crops_check.py`, 3,600 sets), a set took 18.2 ms with both off, 14.4 ms with `--fixed-crops` and no graph, and 12.6 ms as one graph. Unlike on the EVO-X2, fewer animals didn't make sleap-nn's own layer faster: with 11 to 15 of 15 animals in view, the graph took 12.6 ms every time and sleap-nn's layer 15.3–18.2 ms. The animals found and the centroids were the same as sleap-nn's layer's in every set. With the crop size corrected, the fp16 keypoints were identical in every set with 14 or 15 animals and differed (by up to 3.3 px) in 24 sets with 11 or 13.

The catch, on a GPU that is itself the limit, like the EVO-X2's: every missing animal still costs a crop's worth of instance model. With 14 of 15 animals in view a set took 12.9 ms against 13.7 with both off, about the same with 13, and with 12 both off was faster (12.2 against 12.9 ms). The benchmark puts 15 crops at 13.2 ms against 9.6 ms for 9, so with only 3 animals per frame both off would be about 3.5 ms faster. Set `--max-instances` to the number of animals you really have; then `--fixed-crops` pays. On such a GPU, with far fewer animals in view than `--max-instances`, `--fixed-crops off` can be faster; on the NVIDIA cards measured (RTX 6000 Ada, RTX 2000 Ada) fixed crops were faster with any number of animals.

## Crops cut at their real size (a sleap-nn bug, worked around)

The top-down model finds each animal's center, cuts a square crop around it (192 px for the default untrained model, 320 px for the trained 5-mouse model), and finds the body parts in that crop. sleap-nn 0.3.3 works out how big to cut the crops from the first animal's box, with a small rounding error: for some positions of that animal it cuts every crop of the set one pixel too narrow or too short, 319 px instead of 320. The model then sees each animal slightly shifted, and some body parts move by a few pixels, now and then by much more. In the 5-mouse replay above, almost a third of the sets were affected.

`campy-live` now corrects this for its own models, without changing sleap-nn's installed files (`ExactCropSize` in `campy/live.py`): the crops are always cut at the model's crop size, in the same place as before. On the EVO-X2, replaying 60 s of 5 mice from 3 cameras with the trained models (3 runs each, alternating), 1,059 of the 3,588 sets came out different from before. In those sets, compared with offline SLEAP, the median body-part error went from 3.92 to 3.79 px and the share within 5 px from 62.9% to 64.1%; over all sets the 3D median went from 4.3 to 4.2 mm, and identities were kept as before. The time per set didn't change (inference p50 14.9–15.2 ms before, 15.1–15.2 ms after). `--fixed-crops` cuts its crops at the real size too.

sleap-nn has since fixed the bug ([talmolab/sleap-nn#780](https://github.com/talmolab/sleap-nn/pull/780), which fixes [talmolab/sleap-nn#778](https://github.com/talmolab/sleap-nn/issues/778), reported from campy). The fix is on sleap-nn's main branch since 22 October 2026 and will be in its first release after 0.3.4; 0.3.3 and 0.3.4 still cut short. A sleap-nn with the fix tells its crop step the crop size, and `campy-live`'s correction, written for the old step, stopped with an error on the first animal it cropped. That would have hit every run that uses sleap-nn's own top-down step: on the CPU or a Mac, with `--fixed-crops off`, and with models `--fixed-crops` can't run. Since pull request 166, `campy-live` checks the installed sleap-nn when it loads the models: with the fix, it leaves sleap-nn's crops as they are; without it, it corrects them as above. Checked on a Mac with sleap-nn 0.3.3, 0.3.4 and main: campy's tests pass on all three, and `campy-live` on the CPU tracked an emulated camera on sleap-nn main (before this change, it stopped with that error while warming up). On a sleap-nn with the fix, one test in `tests/test_live_exact_crops.py` is skipped, with a note that the correction can be removed once campy needs that sleap-nn.

## Compiled networks (`--engine`, on by default with a GPU)

Normally PyTorch runs each layer of the two networks as its own small GPU program. A convolution writes its result to memory, then the next step (bias, activation, upsampling) reads it back, and so on. A compiled engine fuses those steps into fewer, bigger programs made for the exact image size, so far less data travels to and from memory (`bench/compiled_bench.py` measured the networks alone; see the hardware guide). Because an engine is made for one input size, it needs `--fixed-crops`, whose sizes never change. With `--cuda-graphs` too, the engines' programs are recorded into the set's single graph like everything else.

`--engine auto`, the default since pull request 147, picks an engine by itself:

- **TensorRT** (NVIDIA's own) on an NVIDIA GPU, where the `tensorrt` and `onnx` packages are installed;
- otherwise **`torch.compile`**, where Triton is installed. On Linux it comes with PyTorch, for AMD (ROCm) and NVIDIA alike. On Windows it is the `triton-windows` package (see below);
- otherwise **none**. The networks run in plain PyTorch, as before, and `campy-live` says so once as it starts: `--engine auto: neither TensorRT (pip install tensorrt onnx) nor Triton (...) is installed; so the networks run in torch`.

If the engine it picked can't be made, `campy-live` doesn't stop. A Triton that doesn't match PyTorch, a TensorRT build that fails, or any other error while the engine is made or first run: it says why in one line (`--engine auto: the torch.compile engine failed (...); so the networks run in torch`), and warms up again without the engine. All of this happens during the warm-up, before `Waiting for the live feed`, so it can never stall or slow a recording. Tested on the three machines with a deliberately broken `tensorrt` or `triton` package: one line, then exactly the default's output and speed (12.6 ms per set on the Pro Max Slim, 2.7 ms on the RTX 6000 Ada, 10.6 ms on the EVO-X2, where `--fast-upsample` comes back on), ready in 5.0–6.7 s. Without a GPU, or with `--fixed-crops off`, there is nothing to compile, and `auto` stays quiet.

The other choices: `--engine compile` or `--engine tensorrt` asks for that engine, and `--engine on` for `auto`'s choice. All three stop `campy-live` with the reason if the engine can't be made. `--engine off` runs the networks in plain PyTorch, the default before.

### Why it is on by default

Measured on 22 October 2026 on the three test machines, with campy 2.1.0. The same 5-mouse replay as above, 3 cameras at 60 fps with 3D, against 2.1.0's defaults (fixed crops, CUDA graphs, and `--fast-upsample` on the EVO-X2). EVO-X2: 3,600 sets per replay. Windows machines: 30 s, about 1,790 sets.

| | inference per set, p50 / p99 | frame to 3D, p50 / p99 | sets skipped | GPU memory (`campy-live`) |
|---|---|---|---|---|
| EVO-X2, 2.1.0 default | 10.6 / 10.7–10.8 ms | 12.2 / 13.0 ms | 3 | 1,055 MB |
| EVO-X2, `--engine auto` (`torch.compile`) | **5.9 / 6.1 ms** | **7.5 / 8.4 ms** | 1–2 | 820 MB |
| Pro Max Slim (RTX 2000 Ada), 2.1.0 default | 12.6 / 12.8 ms | 15.6–15.7 / 16.4 ms | 0 | 615–625 MB |
| Pro Max Slim, `--engine auto` (TensorRT) | **5.8 / 6.5 ms** | **9.1 / 9.8 ms** | 0 | 490–515 MB |
| Pro Max Slim, `--engine compile` | 9.6 / 9.8–10.2 ms | 12.8 / 13.5–13.7 ms | 0 | 545–555 MB |
| RTX 6000 Ada, 2.1.0 default | 2.7 / 4.8–4.9 ms | 5.7–5.8 / 7.9 ms | 0 | 1,000 MB |
| RTX 6000 Ada, `--engine auto` (TensorRT) | **1.6** / 3.9–6.4 ms | **4.7–4.8** / 7.0–9.2 ms | 0 | 870–945 MB |
| RTX 6000 Ada, `--engine compile` | 2.2 / 4.3–6.8 ms | 5.3 / 7.3–9.9 ms | 0–4 | 900–970 MB |
| RTX 4000 Ada, 2.1.0 default | 6.4 / 6.7 ms | 9.3 / 10.6 ms | 0 | 720 MB |
| RTX 4000 Ada, `--engine auto` (TensorRT) | **3.4 / 5.1–5.2 ms** | **6.4–6.5 / 8.1–8.4 ms** | 0 | 670 MB |
| RTX 4000 Ada, `--engine compile` | 5.1 / 6.0 ms | 8.0–8.1 / 9.1–9.4 ms | 0 | |

Every run was `RIG_CLEAN`, except the two first TensorRT builds (the Pro Max Slim's and the RTX 6000 Ada's), during which the security agent briefly used 2–4.5 cores; their timings match the clean runs. GPU memory is the most the GPU's used memory rose while `campy-live` ran. A first TensorRT build needs about 360 MB more for a moment. The Precision 7875's p99 swings from run to run with or without an engine, as before: it comes from the clock hold's tries at one block fewer, during which the RTX 6000 Ada's clock falls for about 1.3 s ([models.md](models.md#where-a-sets-time-goes)). Since pull request 152 a count found too few isn't tried again on that GPU, and its p99 is 1.7–2.0 ms. Run to run, each setting repeated its p50 to 0.1 ms.

**Accuracy.** Against the offline SLEAP predictions of the same frames (the rig check's comparison), every setting tracked exactly as well as 2.1.0's default, to the digits it reports:

| | 2D error, median / p90 | within 5 px | 3D error, median / p90 | same identity | identity switches |
|---|---|---|---|---|---|
| EVO-X2, default and `--engine auto` | 3.8 / 9.3 px | 64% | 4.2 / 10.6 mm | 99.93% | 2 |
| Pro Max Slim, default, `auto` and `compile` | 3.8 / 9.8 px | 63% | 4.4 / 11.2–11.3 mm | 99.85% | 2 |
| RTX 6000 Ada, default, `auto` and `compile` | 3.8 / 9.8 px | 63% | 4.4 / 11.2 mm | 99.85% | 2 |
| RTX 4000 Ada, default, `auto` and `compile` | 3.8 / 9.8 px | 63% | 4.4 / 11.2–11.3 mm | 99.85% | 2 |

Set by set against the default's own output on the same machine, `torch.compile` moved 0.7% of the keypoints by more than a pixel (p99 0.012–0.013 px), and TensorRT moved 2.0–2.3% (p99 2.9 px). TensorRT runs the whole network in half precision, so now and then a keypoint lands on a second, nearly as bright spot. In about 1,790 sets of 3 cameras, each found the same animals as the default in all but 1 or 2 of the cameras' frames. With this pull request's code, `--engine off`, and `auto` with a deliberately broken TensorRT or Triton installed, gave the default's output to the last bit. The default itself repeated its output exactly from run to run. Those moved keypoints don't show in the comparison with offline SLEAP above, so `auto` takes TensorRT on NVIDIA, where it is clearly the fastest. On the Pro Max Slim it takes 6.8 ms per set off the default, against 3.0 ms for `torch.compile`. For the exact keypoints of plain fp16 PyTorch, use `--engine compile` (on Windows it needs `triton-windows`) or `--engine off`.

**`--fast-upsample` adds nothing on top.** It only applies to the networks run in plain PyTorch, so with an engine it stays off. The engine already fuses the slow upsampling into the layers around it: on the EVO-X2 a set took 12.8 ms with neither, 10.6 ms with `--fast-upsample` and 5.9 ms with `torch.compile`. On NVIDIA, `--fast-upsample on` changed nothing (12.6 and 2.7 ms per set with or without it, the keypoints identical), which is why its `auto` already left it off there. Its default stays `auto`: on with ROCm when the networks run in plain PyTorch, which is when `--engine auto` found no engine or one failed.

### Start-up time, and where the engines are kept

An engine is made while `campy-live` warms up, before it prints `Waiting for the live feed`. It is made once and kept, so only the first start takes longer. Seconds from starting `campy-live` to `Waiting for the live feed`:

| | 2.1.0 default | `--engine auto`, first start | `--engine auto`, later starts |
|---|---|---|---|
| EVO-X2 (`torch.compile`) | 4.4–4.5 s | 17.3–17.5 s (19.8 s with MIOpen's cache empty too) | 6.9–7.0 s |
| Pro Max Slim (TensorRT) | 5.0 s | 26.4–26.9 s | 5.0 s |
| RTX 6000 Ada (TensorRT) | 6.4–6.6 s | 26.8–37.2 s | 6.6–10.8 s |
| RTX 4000 Ada (TensorRT) | 6.4 s | 28.3 s | 6.5 s |
| `--engine compile` on Windows | | 40.7–41.7 s | 7.4–9.8 s |

- **TensorRT** keeps its engines (*plans*) in `--engine-cache`, by default `~/.cache/campy/engines` (on Windows `C:\Users\<you>\.cache\campy\engines`). A plan's name comes from the network's weights, the input size, the precision, the GPU, and the TensorRT and PyTorch versions. So another model, a retrained one, another GPU or an update gets a new plan, built once, and an old plan is never used by mistake. Old plans stay in the folder; delete them to free the space (35–45 MB per model pair).
- **`torch.compile`** keeps its compiled kernels in the same folder, under `inductor/`. On Linux its own default is the temp folder, which is emptied at every restart. On the EVO-X2 that would mean a 17 s start after every reboot, so `campy-live` keeps them with the TensorRT plans. If `TORCHINDUCTOR_CACHE_DIR` is set, that folder is used instead. PyTorch names what it keeps by the traced network, its own version and the GPU. The weights are inputs, so a retrained model of the same size reuses the kernels, and another architecture or a PyTorch update compiles anew.
- `campy-live` says when it is making them: `building the centroid network's TensorRT engine ... once (kept in ...)`, or `compiling the networks with torch.compile (their kernels kept in ...)`.

So after installing or updating campy, PyTorch or TensorRT, or with a new model, the first start takes up to about 40 s. Start `campy-live` before the recording, as always, and wait for `Waiting for the live feed`; then it analyses the recording from the first frame. Or run a short replay once ([Testing without cameras](#testing-without-cameras)), and every start after that is quick.

### Installing the engine packages (Windows)

The engines need packages that a plain campy-live environment doesn't have. Install them into the same conda environment `campy-live` runs from (the one with PyTorch and sleap-nn), not into the recording environment:

```
<env>\python.exe -m pip install tensorrt-cu13==11.4.0.106 onnx==1.23.2 triton-windows==<version>
```

- `tensorrt-cu13` and `onnx` are for `--engine tensorrt`. 11.4.0.106 is the build that was tested. It brings its own copy of NVIDIA's libraries and works next to PyTorch builds for CUDA 12.8 as well as 13.0. The NVIDIA driver must support CUDA 13.
- `triton-windows` is for `--engine compile`. Each PyTorch minor version works with one Triton minor version only, so pick the one that matches `torch.__version__`. PyTorch 2.11 needs Triton 3.6 (`triton-windows==3.6.0.post26`), and PyTorch 2.14 needs Triton 3.8 (`triton-windows==3.8.0.post29`). (On Linux the matching `triton` comes with PyTorch; `triton-windows` uses the same version numbers.) A mismatched Triton fails when the networks compile, during the warm-up.
- None of the three changes PyTorch, NumPy or anything else already installed. Check afterwards with `<env>\python.exe -c "import torch, tensorrt, onnx, triton; print(torch.__version__, tensorrt.__version__, triton.__version__)"`.
- When you upgrade PyTorch in that environment, upgrade `triton-windows` to the matching version too. When you change TensorRT version, the next run rebuilds its engines on its own (about 10–20 s, once).

Then check it with a short replay (see [Testing without cameras](#testing-without-cameras)) with `--engine tensorrt` (or the default, `auto`). The first run prints `building the centroid network's TensorRT engine ... once`, and the report should say `networks: centroid TensorRT fp16`. After installing this way, the same 30 s, 5-mouse, 3-camera replay as in the tables gave:

| | `--engine tensorrt`, inference per set p50 / p99 | `--engine compile` |
|---|---|---|
| RTX 6000 Ada (PyTorch 2.14, Triton 3.8) | 1.6 / 5.9–6.3 ms, 0 skipped | 2.2 / 7.4 ms, 5 skipped in the first seconds |
| Pro Max Slim, RTX 2000 Ada (PyTorch 2.11, Triton 3.6) | 5.8 / 6.5–6.6 ms, 0 skipped | 9.6 / 9.8 ms, 0 skipped |

These match the earlier measurements below, which used the same packages from a separate folder. On the Pro Max Slim `torch.compile` is clearly slower than TensorRT (9.6 against 5.8 ms), so use `--engine tensorrt` (or `auto`) there.

### Earlier measurements (when `--engine` came, pull request 133)

These came before `--fast-upsample` and the clock hold's one block on the EVO-X2, so its default was slower then (13.7 ms per set; 10.6 ms with both). Live, 5 mice, 3 cameras, 60 fps, 3D, the same trained models as above, compared with `--fixed-crops --cuda-graphs`:

| | inference per set, p50 / p99 | frame to 3D, p50 / p99 | sets skipped | graph memory |
|---|---|---|---|---|
| EVO-X2, `--fixed-crops --cuda-graphs` | 13.7 / 14.2–14.3 ms | 17.4–17.6 / 20.4–20.9 ms | 0–3 of 3,600 | 210 MB |
| EVO-X2, `+ --engine compile` | **5.9–6.0** / 6.9–7.0 ms | **7.7** / 9.0–9.2 ms | 3–10 of 3,600 | 126 MB |
| RTX 6000 Ada, `--fixed-crops --cuda-graphs` | 2.7 / 4.6–4.9 ms | 5.7–5.8 / 7.6–8.0 ms | 0 of 1,800 | 229 MB |
| RTX 6000 Ada, `+ --engine compile` | 2.2–2.3 / 4.2–7.2 ms | 5.4 / 7.1–10.0 ms | 0 of 1,800 | |
| RTX 6000 Ada, `+ --engine tensorrt` | **1.6** / 5.6–5.8 ms | **4.7** / 8.1–8.7 ms | 0 of 1,800 | 59 MB |
| Pro Max Slim (RTX 2000 Ada), `--fixed-crops --cuda-graphs` | 12.6 / 12.8–12.9 ms | 15.7 / 16.6–16.8 ms | 0 of 1,800 | |
| Pro Max Slim, `+ --engine tensorrt` | **5.8** / 6.5–6.6 ms | **9.1** / 10.0–10.1 ms | 0 of 1,800 | |

Three replays of each (EVO-X2: the whole 3,600-set replay; Precision 7875 and Pro Max Slim: 30 s each). Every run was `RIG_CLEAN`, except that the Pro Max Slim's job logged 4 sightings of its security agent during the tests that ran before, minutes before the replays; none came during them. On the EVO-X2, whose GPU is itself the limit, `torch.compile` more than halves the time: a set takes 6 ms instead of 13.7, so 10 ms of each 16.7 ms frame are now free. The tracking matched the offline SLEAP predictions exactly as well as before (2D median 3.8 px, 3D median 4.2 mm, the same identities 99.93% of the time). The Pro Max Slim's small RTX 2000 Ada gains as much with TensorRT: 12.6 to 5.8 ms per set, so 5 mice at 60 fps now leave it 10 ms of each frame (`torch.compile`, measured live there later, takes 9.6 ms per set; on the networks alone it is also much slower than TensorRT, 7.4 against 4.5 ms). On the RTX 6000 Ada, where the graph had already made a set short, TensorRT saves another 1.1 ms. The Precision 7875's p99 is set by one stretch of about 60–100 sets (from set ~730) that is slow in every run with or without an engine. Outside it, TensorRT's p95 is 1.7 ms against 2.9–3.3 ms.

Accuracy, set by set (`bench/fixed_crops_check.py --engine … --reference fp32`): 3,600 sets of the same replay, against sleap-nn's own layer in fp32 (crop size corrected), about 575,000 keypoints:

| networks | a set alone (RTX 6000 Ada) | keypoints moved over 1 px | p99 moved | sets where the animals found differ |
|---|---|---|---|---|
| PyTorch fp16 (today's default) | 2.6 ms | 0.71% | 0.055 px | 388 |
| `torch.compile` fp16 | 2.2 ms | 0.66% | 0.024 px | 369 |
| TensorRT fp16 | 1.55 ms | 1.77% | 2.9 px | 393 |
| TensorRT fp32 (`--precision fp32`) | 3.2 ms | 0.49% | 0.017 px | 1 |

On the EVO-X2 the same comparison gave `torch.compile` fp16 and PyTorch fp16 the same accuracy: 1.78% of keypoints moved over 1 px (p99 2.9 px), and the animals found differ in 382 and 381 sets. Half precision on that GPU moves keypoints more than on the RTX 6000 Ada, with or without the engine. On the Pro Max Slim, TensorRT fp16 moved 1.89% of keypoints over 1 px (p99 2.9 px), against 0.69% for PyTorch fp16, as on the RTX 6000 Ada.

So `torch.compile` is as exact as today's fp16 PyTorch. TensorRT fp16 is the fastest, but it runs the whole network in half precision, so about 1 keypoint in 60 moves more than a pixel (often to a second, nearly as bright spot), against 1 in 140 today. In fp32, TensorRT is exact but slower than fp16 PyTorch. The roughly 380 sets where any fp16 run finds different animals than fp32 aren't caused by the engines. In fp16 the two brightest pixels of a peak sometimes round to the same value, and sleap-nn's peak finder then drops that animal (about 0.7% of centroids; see the hardware guide).

## Faster upsampling on AMD (`--fast-upsample`, on by default with ROCm when no engine runs)

The two models make their images bigger again with *bilinear upsampling* layers. PyTorch has two ways to run them, depending on how the image is laid out in memory: NCHW (each channel a whole image after the other, the default on AMD) or channels-last (all channels of one pixel side by side, the default on NVIDIA). In NCHW, PyTorch's upsampling gives each output pixel one GPU thread, which then loops over every crop and every channel. On the deep, small layers (5×5 pixels with 768 channels) that is only a few hundred threads for a GPU built to run tens of thousands, so it sits nearly idle: on the EVO-X2 those layers took 1.45 and 0.98 ms of a set's 9 ms of instance model ([Where a set's time goes](models.md#where-a-sets-time-goes)). In channels-last the work is spread over the channels too, but on the big, shallow layers channels-last is slower.

With `--fast-upsample`, `campy-live` times each upsampling layer both ways while it warms up, for every number of crops it can get, and uses whichever is faster: the input is turned into channels-last, upsampled and turned back, so the layer after it sees no difference. Each output pixel is the same sum of the same four input pixels either way, so the results are identical. `campy-live` says what it chose when it starts (for example "3 of 7 upsampling layer shapes run channels_last ... 2.41 ms less GPU time"). With `--cuda-graphs` the graphs record the faster way. `auto` (the default) turns it on when the models run in NCHW on a GPU, which `--memory-format auto` picks on AMD. On NVIDIA the models already run channels-last, and there every upsampling layer was already at its fastest (RTX 6000 Ada: nothing switched, same time), so it does nothing there. `--fast-upsample off` turns it off. With a compiled engine (`--engine`, above, on by default since pull request 147), which fuses the upsampling into the layers around it and is faster still on the EVO-X2 (5.9 ms per set), it stays off: `--fast-upsample` is for PyTorch's own way of running the networks, which `campy-live` falls back to when no engine can be made, or with `--engine off`.

On the EVO-X2, 5 mice, 3 cameras, 60 fps, the trained models of the table above, two replays of each (3,600 sets each), all on the same day:

| | inference per set, p50 / p99 | sets skipped | 3D step alone, p50 |
|---|---|---|---|
| `--fixed-crops --cuda-graphs` | 13.6 / 14.2 ms | 0, 2 | 1.2–1.7 ms |
| `--fixed-crops --cuda-graphs --fast-upsample on` | **10.6 / 11.7 ms** | 0, 3 | 0.9–1.0 ms |
| `--fixed-crops --cuda-graphs --hold-gpu-clock-blocks 1` | 12.8 / 13.0 ms | 0, 2 | 0.9 ms |
| `--fixed-crops --cuda-graphs --hold-gpu-clock-blocks 1 --fast-upsample on` | **10.6 / 10.8 ms** | 2, 2 | 1.0 ms |
| `--hold-gpu-clock-blocks 1` (no fixed crops, no graphs) | 14.4 / 16.0–16.2 ms | 0, 3 | 1.0 ms |
| `--hold-gpu-clock-blocks 1 --fast-upsample on` | 12.3 / 13.8–14.7 ms | 0, 3 | 1.1 ms |

2D and 3D accuracy against the offline SLEAP predictions were the same in every run (2D median 3.8 px; 3D median 4.2 mm). Run in one process on the same 400 sets, the keypoints were identical to the last bit, with and without it, on the EVO-X2 and on the RTX 6000 Ada (with `--memory-format nchw` forced there: 11.6 to 9.5 ms per set, still slower than channels-last's 8.6).

**The GPU clock hold matters on the EVO-X2.** With `--hold-gpu-clock-blocks auto`, `campy-live` stops its spinning block once the GPU's clock reads above 90% of its fastest, about 10 s in. On the EVO-X2 the clock does read high after that, but every set gets about 1 ms slower, and so does the 3D step, which runs on the CPU. So the whole chip seems to drop to a lower power state between sets, not just the GPU's clock. That hid most of `--fast-upsample`'s gain in runs without fixed crops (about 15.2 ms with or without it once the block stopped, against 12.1 and 14.3 ms while it spun). Keeping one block spinning (`--hold-gpu-clock-blocks 1`) avoids it, as the table shows. With `--fast-upsample` the GPU has more idle time, the clock dips, and `auto` kept its block spinning 95% of the time anyway. Changing `auto` to keep one block on the EVO-X2 is a separate change, not made yet.

## Opening a tracked recording in LUC3D

`campy-session` runs `campy-live-export` itself after a clean stop (`post: export`, above). By hand: after the recording, `campy-live-export` turns `campy-live`'s log into files that [LUC3D](https://github.com/talmolab/luc3d) opens with the results already on the videos: each camera's 2D predictions and, if `campy-live` ran with `--calibration`, each animal's 3D pose and identity. Run it in the inference environment, which has the `sleap-io` package it needs (it comes with sleap-nn):
```
campy-live-export <recording folder> --log campy-live_<date_time>.npy
```
The recording folder is the one `campy-acquire` made, with one folder per camera. The export writes into it:

- **`campy-live.slp`**: everything in one file. In LUC3D, use File > Load SLP and pick it. When LUC3D asks where the videos are, pick the recording folder; it finds each camera's video by the folder's name. Each camera shows its 2D predictions. With 3D, each animal on each frame is grouped across the cameras with its identity ("animal 0", "animal 1", ...) and its 3D pose, as LUC3D's own triangulation would group it, so the 3D view, the reprojections and the identity timeline work at once.
- **`<camera>/campy-live.slp`, `calibration.toml` and `skeleton.json`**: the same 2D in LUC3D's session-folder layout. Use File > Load Session Folder and pick the recording folder. This gives the 2D only, which LUC3D can triangulate itself. It is also how to open a recording tracked without `--calibration`. `calibration.toml` holds only the recorded cameras, under campy's camera names.
- **`points3d.h5`**: the 3D alone, as plain arrays (`points_3d` is frames × animals × nodes × xyz in mm, NaN where unseen, with `frame_indices`, `campy_frames`, `track_names` and `node_names`), for analysis outside LUC3D. LUC3D's File > Load 3D points also reads it, as 3D poses not linked to the 2D.

How the frames line up: `campy-live` skips the sets that arrive while it is busy, so those frames have no predictions. Every prediction goes on the video frame that holds it, found through each camera's `frametimes.npy`. If campy's writer dropped frames from one camera's video, that video runs behind the others in LUC3D from there on. Its 2D still land on its own correct frames, and `campy-live-export` says from which frame. The export also checks that the log belongs to the recording: each row's host times must match the recording's `hosttimes.npy`.

The summary tells the export the skeleton, the calibration and the number of animals. Logs from campy 2.1.0rc6 and earlier lack the skeleton's edges: add `--skeleton <the instance model's folder>`. If the calibration is not at the path `campy-live` used, give `--calibration`. If the summary is missing because `campy-live` was stopped by an error, also give `--cameras`. The `.slp`'s provenance keeps the recording's `session.json` and `campy-live-session.json`, with the animal IDs. The identities stay "animal 1", "animal 2", …, because campy-live doesn't know which animal ID each one is. With `--calibration`, the export says so if that file isn't the one `campy-live` tracked with.

Checked on the EVO-X2: three emulated cameras played the 5-mouse clips for 30 s while `campy-live` tracked them in 3D (1,791 sets, 3 skipped). In LUC3D, every 2D point and 3D pose sat on the frame its set came from, and LUC3D's reprojection error of `campy-live`'s 3D was 2.1 px.

Long recordings: the export no longer needs much memory, however long the recording. It used to let sleap-io build the whole file in memory first, which took 5.6 minutes and 20.7 GB for an hour of 3 cameras at 60 fps with 5 mice, so a day's recording could not be exported at all. It now writes the files a piece at a time, and the files come out the same, value for value. Measured on the EVO-X2 (Linux) with copies of a real 30-second recording strung together: an hour takes 9 seconds and 0.2 GB of memory (13 seconds and 0.3 GB on an M-series Mac), and a whole day (5,158,080 sets, a 17.8 GB log) takes 3.2 minutes and 0.95 GB, the same when the log has to be read from disk rather than from memory. Tools that report a process's memory show much more for the day, 18.9 GB, because they count the parts of the log the export has read; those are only a copy of the file, which the computer drops and reads again when it needs the memory. Held to 3 GB of memory in all, the day still exported, in 4.6 minutes instead of 3.2, so a computer with 4 GB to spare is enough. The files take about 3 GB per hour of recording, 73 GB for the day: `campy-live.slp` 37 GB, each camera's own `.slp` 9.5 GB, `points3d.h5` 7.5 GB. Leave room for them on the recording's disk.

Opening a long recording in LUC3D is a different matter, because the browser has to hold what it opens. File > Load SLP reads the whole file at once: 10 minutes (257 MB) opened fine, but a whole hour (1.5 GB) ran the browser tab out of memory. LUC3D reads big files a frame at a time in two other places, and both opened the hour: File > Load Project, picking `campy-live.slp`, shows the 3D groups but labels the animals only by their tracks ("animal 0", "animal 1", ...), not as LUC3D identities; File > Load Session Folder shows the 2D. To see a stretch of a long recording with everything, identities included, export just that part with `--frames FIRST:LAST`, given in campy's frame numbers, and open it with Load SLP. It lands on the right frames of the full videos (campy's frame numbers are LUC3D's too, unless campy's writer dropped frames). `--force` replaces the previous part's files.

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
Each run's recording goes into a new folder inside `videoFolder`, as usual, and the clips stay where they are. As with a hardware trigger, campy starts every emulated camera at the same moment once all are ready, so frame n of each camera reaches the computer within a few milliseconds of the others', as with real cameras on a trigger. Each emulated camera used to start on its own, and `campy-live` counted every set as misaligned. Use videos of the same length, recorded together, so that each set of frames shows one moment. Each test machine recorded three emulated cameras playing a 5-mouse session at 60 fps for 60 s while `campy-live` tracked them in 3D, and kept all 3,600 frames per camera. With campy's defaults since pull request 147, which run the networks as compiled engines, the Precision 7875 and the Pro Max Slim skipped no set and the EVO-X2 skipped 2. Frame to 3D pose took 4.1 ms on the Precision 7875, 8.1 ms on the EVO-X2 and 8.2 ms on the Pro Max Slim. With 2.1.0rc7's defaults it took 5.2, 12.8 and 14.8 ms, and with 2.1.0rc6 13.3, 18.6 and 27.6 ms. The hardware guide has all of them. The README explains the details ([Emulated cameras](../README.md#emulated-cameras-cameramake-emu)).

## On Linux (AMD, ROCm)

- **First use of a model**: AMD's MIOpen library tunes every new convolution shape the first time it sees it, so the first warm-up of a new model can take about 10 s. The tuning is cached for later runs.
- **Disk stalls**: some SSDs stop completing writes for 10–16 s. The EVO-X2's config (`configs/campy_config_evox2.yaml`) holds up to 60 s of frames in memory (`writeQueueMaxSec: 60`) so a stall drops nothing. Better still, record to a second drive.
- **Encoding**: VA-API runs on the same GPU as inference. On the EVO-X2 that cost inference only a few percent.

## On Windows (NVIDIA, CUDA)

- **Sleep**: campy keeps Windows awake while it records (`keepAwake`, on by default). If campy is started over SSH while nobody is signed in, Windows refuses the screen part of that request: set the screen timeout to Never as well (Settings > System > Power).
- **Latencies in 15–16 ms steps (fixed)**: on Windows, up to Python 3.12, Python's `time.monotonic` moves in steps of 15.6 ms. campy used to take its host times from it (`hosttimes.npy`, the live feed, and `campy-live`'s timestamps), so the latencies `campy-live` reported there came in 15–16 ms steps (15, 16, 31, 32, 47 ms...). Since pull request 118, campy takes every host time from Windows' precise clock instead (`time.perf_counter_ns`, QueryPerformanceCounter, which Python 3.13 uses for `time.monotonic` anyway): one clock in every process, so the camera processes, `campy-live` and `campy-live-view` still compare their times directly, and latencies are exact to well under a millisecond (on the Pro Max Slim, a 60 fps recording's `hosttimes.npy` then showed frames 16.6 ms apart, and `campy-live` read each set 0.9 ms after its last frame arrived, where it used to say 0, 15 or 16 ms). Inference times were always precise. Latencies measured on Windows before this, such as the hardware guide's 31 / 32 ms, are rounded to those steps. A program that writes frames into the live feed itself (a replay script, say) must stamp them with the same clock, `campy.clock.HostNs`: with `time.monotonic_ns`, a different clock on Windows, the latencies `campy-live` reports mean nothing (40–56 ms in a replay whose real latency was 13 ms).
- **Encoding on the Intel iGPU** (Quick Sync, `gpuMake: "intel"`) keeps the encoder off the inference GPU.
- **The GPU's clock**: an NVIDIA GPU chooses its own clock speed from how busy it looks. Live tracking gives it a short burst of work for each frame, and that didn't look busy: on the Pro Max Slim's RTX 2000 Ada the clock sat at 780 MHz of its 2,500 while tracking, and fell to 210 MHz while `campy-live` waited for the recording to start. So `campy-live` keeps a tiny piece of the GPU busy, at the lowest priority, from the moment the feed appears until it ends (`--hold-gpu-clock`, on by default with NVIDIA and AMD GPUs). The models' work goes first, so it isn't slowed. That took the time per set from 27.6 to 13.2 ms with 3 mice in view, so the same models now fit 60 fps, and the first sets of a recording are no longer slow. The cost is about 20 W more while waiting. `--hold-gpu-clock off` turns it off. While the frames come, `campy-live` reads the GPU's clock every half second and spins 0, 1 or 2 blocks, as many as keep the clock up (`--hold-gpu-clock-blocks auto`, or a fixed number): one block costs the models about 0.3 ms per set, so none spin when the GPU keeps its clock by itself, as the Pro Max Slim's card did right after a restart. When a try at one block fewer turns out too few (the clock falls again within 10 s), `auto` puts the block back and doesn't try that count again on that GPU until the computer restarts: it remembers it in `gpu_clock.json` in `--engine-cache`, by the GPU's UUID. Before, it kept retrying, and on the RTX 6000 Ada, which needs 1 block, each try cost 1.3 s of sets 3–4 times slower (p99 5.1–5.7 ms; now 1.7–2.0 ms, pull request 152). It reads the clock through NVIDIA's NVML (the `nvidia-ml-py` package, if installed in campy-live's environment), else `nvidia-smi` (a small program that `campy-live` starts and ends; it ends with `campy-live` even when `campy-live` is killed or crashes, so it never keeps polling the GPU or holds a folder open after), and on AMD from the driver. AMD's GPUs slow down too, less: the EVO-X2's ran the default models at 2,117 of its 2,900 MHz, 9.9 ms per set instead of 8.9, so the hold is on there by default as well. On the EVO-X2, whose AMD GPU shares one chip with the CPU, `auto` keeps at least 1 block spinning while the frames come. With none, its GPU clock still read 2,650–2,850 MHz, yet every set took about 1 ms longer and the CPU's 3D step too. So the whole chip slows down between sets in a way the GPU clock doesn't show. With 5 mice, 3 cameras, `--fixed-crops --cuda-graphs`, two replays each: 0 blocks 13.7 / 14.2 ms per set (p50 / p99), 3D step 1.3–1.5 ms; 1 block, which `auto` now keeps, 12.8 / 13.0 ms, 3D step 0.85 ms (with `--fast-upsample`, 10.6 / 10.7–10.8 ms). The chip ran at its power limit, about 85 W, in every one of those replays (the replay's own video decoding included), so the block cost no measurable power there. NVIDIA GPUs and AMD's own graphics cards are unchanged: there `auto` still goes down to none when the clock holds by itself. A number (`--hold-gpu-clock-blocks 0`, `1`, `2`) still overrides `auto` everywhere.
- **Hybrid Intel CPUs**: `campy-live` runs itself on the performance cores at High priority, and moves campy-acquire's camera processes and encoders onto the efficiency cores when their feed appears (`--cpus auto`, `--priority auto` and `--campy-cpus auto`, the defaults). With 3 cameras recording, that saved about 2 ms per set. `python -m campy.cpus` lists which cores are which. `--cpus none --campy-cpus none` leaves both programs where Windows puts them. The move needs a campy-acquire from this version on, whose live feed reports its processes; with an older one, `campy-live` says so and leaves campy-acquire alone.
- **CUDA graphs** (`--cuda-graphs`, on by default with a GPU): each model's GPU work is recorded once and then replayed in one go. The keypoints are identical; on the RTX 2000 Ada it saved about 0.8 ms per set (13.2 to 12.4 ms with 3 mice). Each number of animals found gets its own graph (up to 15 at 3 cameras with up to 5 animals each, 40 at 8), and all of a model's graphs share one block of GPU memory. On the RTX 6000 Ada, `campy-live`'s graphs hold 0.26 GB at 3 cameras and 0.65 GB at 8; with a block per graph, as before, they held 1.7 and 11.2 GB, for the same time per set. `campy-live` says how much its graphs hold when it starts.
- **channels-last** memory layout is the default on NVIDIA GPUs (`--memory-format auto`), and is faster there.
- **A monitor** plugged into the inference GPU slowed it noticeably. If the machine has two GPUs, plug the monitor into the other one.
- **Two GPUs**: run `campy-live` on one and let campy's encoders use the other. torch numbers GPUs fastest first unless told otherwise, and `nvidia-smi` by their slot, so start `campy-live` with `CUDA_DEVICE_ORDER=PCI_BUS_ID` and `CUDA_VISIBLE_DEVICES=<the GPU's nvidia-smi index>`. Give the cameras the other GPU's `gpuID` for NVENC. On the Precision 7875 that is `CUDA_VISIBLE_DEVICES=0` (RTX 6000 Ada) for `campy-live` and `gpuID: 1` (RTX 4000 Ada, which also drives the monitor) for the encoders; while it recorded, `nvidia-smi` listed `campy-live` only on the first and ffmpeg only on the second.
