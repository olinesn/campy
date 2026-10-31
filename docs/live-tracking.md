# Live tracking with campy-live

Three programs work together on the computer that records:

- **`campy-acquire`** records the cameras as usual. With `liveFeed: True` it also copies each frame into shared memory, the *live feed*.
- **`campy-live`** reads the feed and runs a sleap-nn top-down model on each synchronized set of frames, one frame per camera. With a calibration, it also matches the animals across cameras, keeps their identities and triangulates them in 3D. It is never more than one set behind: a set that arrives while it is busy is skipped and counted, never queued.
- **`campy-live-view`** (optional) shows the camera frames with the tracking drawn on them, plus a 3D view.
- **`campy-rules`** (optional) switches TTL outputs from the poses, for closed-loop experiments: see [Closed-loop outputs](closed-loop.md).

campy never waits for `campy-live`, so tracking can't slow down or break a recording. [The reference at the end of this page](#live-pose-estimation-livefeed-campy-live) lists every option.

## Two Python environments

The recording and the inference run in separate environments, because one needs camera drivers and the other needs torch:

- **Recording**: campy's usual environment ([Installation](install.md#installation)), with Spinnaker/PySpin for FLIR cameras.
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
- `campy-session` also starts **`campy-watch`** beside `campy-live` ([Alerts](alerts.md#beside-campy-live-campy-watch)). It sends what `campy-live-view` shows in red as alerts once it lasts, and writes them into `alerts-live.log` in the recording's folder. `live.watch: false` turns it off. Like `campy-live`, it isn't tied to `campy-session`: it watches the recording to its end.
- If `campy-live` stops during the recording, or **hangs** (it runs but makes no result for 30 s while every camera records, which `campy-watch` reports; a camera that stops sending frames also stops campy-live's results, and is no reason to restart it), the cameras carry on. `campy-session` ends a hung `campy-live`, through its stop file and by force 30 s later if it must. Either way, it starts `campy-live` again after 10 s, then 1 and 5 minutes, at most 5 times an hour. Each restart writes its own log (`campy-live.part2.npy`, ...). After the fifth restart in an hour, `campy-session` gives up and sends a critical alert to the alerts' channels (`live/campy-live/restarts`). `live.restart: false` turns restarts off.
- Checked with a `campy-live` frozen mid-recording (SIGSTOP): `campy-watch` reported it 31 s later, `campy-session` ended it 30 s after that, and started it again. Every camera kept every frame.
- One `campy-session` runs at a time on a computer.

**After the recording.** When every camera has recorded to the end, `campy-session` runs the steps of the config's `post:` section, and logs each one in `<folder>/logs/post.log`. Without a `post:` section, it runs `remux`, `join`, `align` and `export`:

```yaml
post:
  remux: true          # true (the default), keep, or false
  join: true           # a camera's video files after an ffmpeg restart, joined into one (the default)
  align: true          # campy-align: every camera on the trigger's timeline, and where the trigger paused (the default)
  export: true         # campy-live-export, when campy-live ran (the default)
  copy: "\\\\nas\\share\\recordings"   # optional: copies the recording's folder into this one
  run: ["python C:\\tools\\notify.py {folder}"]   # optional: your own commands
```

- **remux** turns each fragmented video (campy's default, readable after a crash) into a plain MP4 without re-encoding. Some readers have trouble with fragmented files (an older calibrat3 did), and a fragmented file's header has no frame count.
  - Before anything is replaced, ffmpeg's packet checksums of the two files are compared line by line: every packet, timestamp and the codec's header must be identical. Only then does the plain file replace the fragmented one.
  - With `remux: keep`, the fragmented file stays beside it as `<name>.fragmented.mp4`.
  - If the check fails, or the disk has no room for one more copy of the video, the original stays as it was, and the log says why.
  - A 30 s video of 3 cameras takes a fraction of a second.
- **join** is for a camera whose ffmpeg was restarted during the recording (`restartFfmpeg`), so that its video is in more than one file (`0.mp4`, `0.part2.mp4`, ...). It joins them into one plain MP4, `0.joined.mp4`, without re-encoding, for the tools that read one video per camera: `campy-live-export` (and so LUC3D), `campy-label-pack`, `campy-model-fit` and `campy-synctest`, which say to run it when it hasn't. The files stay as they are.
  - Before it is kept, every packet of the joined video must be the files' packets, in order (ffmpeg's packet checksums), and there must be as many as "frametimes.npy" has rows. Files whose codec headers differ are not joined, since a join without re-encoding keeps only the first file's; each is whole on its own.
  - A file that holds no frame (its ffmpeg stopped before it wrote its first fragment) is left out. A recording without a restart has nothing to join.
- **align** runs `campy-align` ([Aligning cameras after recording](sync.md#aligning-cameras-after-recording)): `aligned.csv` in each camera's folder and `alignment.json` in the recording's, with each frame's trigger and its time. The log says where the trigger paused (pulses no camera has a frame for: no frame is lost, but every frame after is later than its number says) and which camera missed pulses the others got. It is skipped when no camera is triggered and none is a RatCam: there is no timeline to put them on.
- **export** runs `campy-live-export` in the inference environment, so the recording opens in LUC3D with the tracking on it (below). If `campy-live` was started again during the recording, its logs are joined first, in order.
- **copy** copies the whole recording folder into `copy.to`: with robocopy on Windows, with rsync on Linux and macOS (rsync also takes `user@host:/path`). Then it checks every file's size. **Nothing is deleted**, on either side.
- **run** runs your own commands in a shell, with `{folder}` replaced by the recording's folder. One that fails is logged, and the next one still runs.

After a crash, or when a camera didn't record to the end, none of them run, and `campy-session` says why. The recording's fragmented videos stay readable up to their last second. To run the steps again by hand, or after looking at a crashed recording:

```
campy-session post <recording folder>                 # the steps not done yet
campy-session post <recording folder> --only remux    # one step
campy-session post <recording folder> --only join     # after an ffmpeg restart, before campy-live-export by hand
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

To start the recording from a script once `campy-live` is ready, give `campy-live --ready-file <path>`: it writes that file as it prints `Waiting for the live feed`. To stop a recording that was started without a console, or detached over SSH, give `campy-acquire` a stop file (`--stopFile <path>`) and create that file: campy stops as at Ctrl-C, and `campy-live` analyses the recording to its last frame, as it does when a recording ends by itself ([Stop Recording](acquire.md#stop-recording)).

`campy-live` ends with the recording, or at Ctrl-C. It writes `campy-live_<date_time>.npy`, with one row per set processed (keypoints, confidences, and the 3D poses and identities with `--calibration`), and a `.json` summary (strict JSON: an infinite or NaN number in it is the text `"Infinity"` or `"NaN"`, as in the [recording's manifests](recordings.md#infinity-and-nan-in-campys-json-files)). It also writes `campy-live-session.json` into the recording's folder: which models it ran (named by the SHA-256 of `best.ckpt`), with which calibration, options and environment ([Recording folders](recordings.md#campy-live-sessionjson)). To go through the results frame by frame, open the recording in LUC3D (below).

`campy-live` runs the models in fp16 (`--precision fp16`, the default on NVIDIA and AMD GPUs). sleap-nn's own centroid peak finder lost about 0.7% of the mice per frame in fp16, wherever the two top pixels of a centroid rounded to the same value. `campy-live` keeps such a peak once instead, so fp16 finds the mice fp32 finds; `--strict-peaks` goes back to sleap-nn's own (see [Models](models.md)).

## Checking that it keeps up

The summary, and the reports it prints every 10 s, give the sets processed and skipped, the inference time per set and the latency from frames to keypoints and to 3D. At 60 fps a set must take less than 16.7 ms, and at 30 fps less than 33.3 ms.

- **Sets skipped through the whole recording**: the model is too slow for this machine and frame rate. Choose a lighter one ([Models](models.md)).
- **A few skipped in the first fraction of a second**: the GPU was still at its idle clock when the frames came. `campy-live` holds the GPU's clock up from the end of its warm-up (`--hold-gpu-clock`, on by default with NVIDIA and AMD GPUs), for up to 10 minutes while it waits for the feed, so this should no longer happen. If the feed takes longer than that, it says so in one line, and cameras without a trigger can still lose a few sets ([The GPU's clock](#the-first-sets-after-waiting-for-the-feed)).
- **"misaligned" sets**: one camera's frames reached the computer late, or a camera lost a frame (`flirFramesLost` in its `metadata.csv`). See [frame numbers](#live-pose-estimation-livefeed-campy-live), below.

For a test without animals or without a trained model, `--model random` runs untrained models of the benchmark's default sizes, and `--model none` only reads the feed.

### The first sets after waiting for the feed

Until pull request 247, `campy-live` began holding the GPU's clock only when the feed appeared. Cameras without a trigger send their first frame as soon as their feed exists, so a GPU that had gone idle was still at its idle clock for the first sets. It takes 150–350 ms to get up to speed. Now the hold starts at the end of the warm-up, so the GPU is already at speed when the feed comes.

The time that matters is the wait between the warm-up and the feed. On the Pro Max Slim's RTX 2000 Ada, with 1 emulated camera at 60 fps and `campy-acquire` started by hand after `campy-live` was ready (which adds its own 1–2 s), 40 runs in random order before the change gave:

| Wait before starting `campy-acquire` | Runs | Slowest first set | Skipped |
|---|---|---|---|
| None | 4 | 2.6–4.6 ms | 0 |
| 1 s | 4 | 10–13 ms | 0 |
| 2–40 s | 32 | 21–31 ms | 2–8 in every run |

All of these runs were `RIG_CLEAN` but one of the 2–40 s waits and one old-code 20 s run below, each marked for a sighting of load, and both looked like the others. Then, old and new code in random order on the same machine:

| Run | Old code: slowest set in the first second | Old: skipped | New: slowest set in the first second | New: skipped |
|---|---|---|---|---|
| No wait, 1 camera (5 runs each) | 2.6–3.0 ms | 0 | 2.4–3.1 ms | 0 |
| 3, 6 and 20 s wait, 1 camera (15 runs each) | 26–29 ms | 3–7 in every run | 2.4–2.6 ms | 0 |
| 6 s wait, 3 cameras with 3D (3 runs each) | 73–74 ms | 9–12 | 5.7–5.9 ms | 0 |
| Replay, 3 cameras with 3D, no wait (3 runs each) | 6.4–6.5 ms | 0 | 5.6–5.7 ms | 0 |

After the first second, the time per set was the same with both: 2.3 ms per set (p99 2.4) with 1 camera, 5.5 ms with 3. Tracking also starts sooner: the first frame analysed was 2–7 with old code (9–13 in the replays) and is now 1 (1–2 in the replays), because starting the hold no longer happens after the feed has appeared.

- **What a real recording loses.** With old code, at most about 0.35 s of slower sets and up to 12 sets skipped, at the very start, and only with cameras that have no trigger. Keypoints were late or missing for those sets, so `campy-rules` could not act on them. With a trigger, the feed appears well before the first frame: 3 real recordings on the Pro Max Slim (4 cameras, triggered, old code) ran at full speed from their first set, and 5 of them, 18,000 sets each, skipped 0 or 1 in total. Real cameras without a trigger were not tested.
- **The cost.** The hold's roughly 20 W now also runs during the wait. It stops after 10 minutes with no feed, so a `campy-live` left waiting doesn't keep the GPU busy. If the feed comes after that, `campy-live` says so in one line and starts the hold, and the first sets behave as with old code: 2 skipped in the one run tested (a 620 s wait). The power was not measured again.
- **Other computers.** The EVO-X2 doesn't have this problem: 15 runs of each code, with waits of 0, 6 and 20 s, skipped nothing, and its time per set stayed at 3.0 ms. The Precision 7875's older rig checks show the same slow first sets (7–22 ms where 1.0–1.7 is usual, 1 skipped in one run), but it wasn't tested with the change. The earlier tables here that list a few skips in the first seconds may have the same cause; they weren't run again.
- **Why it looked like idling.** It showed up as "the first run after the machine sat idle", in rig checks on two days. 3 runs after 30–45 minutes idle, each with one straight after it, were all clean, so idling alone doesn't do it. Those bad runs did wait longer (3–5 s from the warm-up to the feed, against about 2 s). Why the wait was longer that day wasn't found; it no longer matters with the hold running through it.

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

- Every run was `RIG_CLEAN` except ¹. The Pro Max Slim's "now" runs were marked `RIG_CONTAMINATED` by one sighting, in the job's first second, before its first check started: 164 MB/s of disk from another test that had just ended. A first attempt shortly before, during which Windows Update and the app-store service were busy (up to 3.6 cores), gave the same times (5.5 / 5.7 ms per set while recording), and so did that PR's own clean runs (5.49 / 5.63 ms). The Pro Max Slim's own rig checks after its update to dev df15047 (pull request 158; 23 October, campy installed from the rig's checkout) were all `RIG_CLEAN` and gave the same times: 5.5 / 5.7 ms per set recording 3 cameras (frame to 3D 7.7 / 8.4 ms), 5.5 / 5.7 ms replaying (8.7 / 9.4 ms), and 2.3 / 2.4 ms with 1 camera, with nothing skipped or misaligned and every camera keeping all its frames. They tracked about a second fewer sets than the frames recorded, though: 1,132 sets for 1,200 frames in smoke1, 3,536 for 3,600 in smoke3 and 1,732 in replay3, against 1,193, 3,593 and 1,783 on the previous build. `campy-live` began at frame 65–69 instead of 6–18. Since pull request 163 it wrote `campy-live-session.json` after finding the feed and before its first set, and asking git, `nvidia-smi` and PowerShell about the computer took about a second on this PC. With pull request 183's `campy-live` (a copy next to the installed campy, all `RIG_CLEAN`), the same checks tracked 1,195, 3,595 and 1,792 sets, from frame 6–9, with the same times per set. Those first 5–8 frames came before its first set because the emulated cameras and the replay send frames as soon as their feed appears; cameras on campy's trigger make their feed seconds before their first frame.
- The Windows machines' 2.1.0rc6 replays are the reruns made after the replay feeder was changed to stamp its frames with campy's own host clock. The first runs' frame-to-pose times (41–57 ms) compared two different clocks and mean nothing; their time per set was the same.
- The "now" runs used a copy of dev's code next to each rig's installed campy; the earlier rows are the rigs' own checks after each update.
- All three rigs' own checkouts were then updated to dev 7d78800, which has pull request 183 (23 October, campy installed from each rig's checkout). Their rig checks were all `RIG_CLEAN`, and none skipped a set or misaligned one. Every check tracked the whole run again, with the same times per set as before:

  | Machine | 1 camera, sets / time per set p50 / p99 | 3 cameras, sets / time per set / frame to 3D p50 / p99 | Replay, sets / time per set / frame to 3D p50 / p99 |
  |---|---|---|---|
  | EVO-X2 | 1,800 / 3.0 / 3.1 | 3,600 / 5.6 / 5.8 / 7.5 / 8.5 | 3,600 / 5.5 / 5.6 / 7.1 / 8.1 |
  | Pro Max Slim | 1,195 / 2.3 / 2.5 | 3,597 / 5.5 / 5.7 / 7.7 / 8.4 | 1,792 / 5.5 / 5.7 / 8.7 / 9.5 |
  | Precision 7875 | 1,200 / 0.9 / 1.0 | 3,600 / 1.6 / 1.8 / 4.1 / 5.2 | 1,797 / 1.6 / 1.7 / 4.7 / 5.8 |

  The runs were 20 s (1,200 frames) on the Windows machines and 30 s on the EVO-X2 with 1 camera, 60 s (3,600 frames) with 3 cameras, and 30 s (Windows) or 60 s (EVO-X2) replaying. At dev df15047, before pull request 183, the Windows machines had tracked about a second less of each run: 1,121–1,132 sets with 1 camera, 3,500–3,536 with 3 and 1,723–1,732 replaying. The EVO-X2 never had that late start: it is a Linux machine with an AMD GPU, so nothing asks PowerShell or `nvidia-smi`, and its rig checks at df15047 had already tracked every set. The replay's accuracy was unchanged: 2D median 3.8 px, 99.93% same identity, 2 switches.
- The same evening (23 October) all three rigs' checkouts moved on to dev cba7620, version 2.2.0rc1 (pull request 186; its only change to the code since 7d78800 is the version number). Both environments on each rig report campy 2.2.0rc1. The rig checks were all `RIG_CLEAN` and gave the same results as at 7d78800, with nothing skipped or misaligned and every camera keeping all its frames. Each run's log of sets shows where `campy-live` began: at frame 1 on the EVO-X2 and the Precision 7875 (frame 3 when replaying on the Precision), and at frames 3, 4 and 10 on the Pro Max Slim. At df15047 the Windows machines had begun at frames 65–69.

  | Machine | 1 camera, sets / first frame / time per set p50 / p99 | 3 cameras, sets / first frame / time per set / frame to 3D p50 / p99 | Replay, sets / first frame / time per set / frame to 3D p50 / p99 |
  |---|---|---|---|
  | EVO-X2 | 1,800 / 1 / 3.0 / 3.1 | 3,600 / 1 / 5.6 / 5.8 / 7.6 / 8.5 | 3,600 / 1 / 5.5 / 5.6 / 7.1 / 8.0 |
  | Pro Max Slim | 1,198 / 3 / 2.3 / 2.5 | 3,597 / 4 / 5.5 / 6.0 / 7.8 / 8.7 | 1,791 / 10 / 5.5 / 6.2 / 8.7 / 9.8 |
  | Precision 7875 | 1,200 / 1 / 0.9 / 1.0 | 3,600 / 1 / 1.6 / 1.7 / 4.1 / 5.2 | 1,798 / 3 / 1.6 / 1.7 / 4.7 / 5.8 |

  The replay's accuracy on the EVO-X2 was unchanged too: 2D median 3.8 px, 3D median 4.2 mm, 99.93% same identity, 2 switches.
- Later that evening the three rigs moved to the `v2.2.0rc1` pre-release itself (d094e43). It adds pull requests 187 (`campy-session` also starts `campy-watch`) and 189 (fixes from unplugging a camera and the trigger mid-recording), neither of which changes tracking. Both environments on each rig report campy 2.2.0rc1. The rig checks were all `RIG_CLEAN`, with nothing skipped or misaligned and every camera keeping all its frames, and their times matched the table above to within 0.1 ms:

  | Machine | 1 camera, sets / time per set p50 / p99 | 3 cameras, sets / time per set / frame to 3D p50 / p99 | Replay, sets / time per set / frame to 3D p50 / p99 |
  |---|---|---|---|
  | EVO-X2 | 1,800 / 3.0 / 3.1 | 3,600 / 5.6 / 5.8 / 7.6 / 8.4 | 3,600 / 5.5 / 5.6 / 7.1 / 8.0 |
  | Pro Max Slim | 1,197 / 2.3 / 2.5 | 3,597 / 5.5 / 5.7 / 7.8 / 8.6 | 1,792 / 5.5 / 5.7 / 8.7 / 9.5 |
  | Precision 7875 | 1,200 / 0.9 / 1.0 | 3,600 / 1.6 / 1.7 / 4.1 / 5.3 | 1,797 / 1.6 / 1.7 / 4.6 / 5.7 |

  The replay's accuracy on the EVO-X2 was again 2D median 3.8 px and 3D median 4.2 mm, with 2 identity switches.
- On 24 October the three rigs moved on to dev 54e981a (pull request 200). It adds pull requests 190–200: strict-JSON manifests (192, 194), a faster first video in `campy-infer` (193), the Jetson scripts (198), and benchmark and docs changes. None of them changes tracking, and the version is still 2.2.0rc1 in both environments on each rig. The rig checks were all `RIG_CLEAN`, with nothing skipped or misaligned and every camera keeping all its frames. Their times matched the v2.2.0rc1 table to within 0.1 ms at the median and 0.4 ms at the 99th percentile, and every `.json` file they wrote (`campy-live`'s summaries and `campy-live-session.json`) is strict JSON:

  | Machine | 1 camera, sets / first frame / time per set p50 / p99 | 3 cameras, sets / first frame / time per set / frame to 3D p50 / p99 | Replay, sets / first frame / time per set / frame to 3D p50 / p99 |
  |---|---|---|---|
  | EVO-X2 | 1,800 / 1 / 3.0 / 3.1 | 3,600 / 1 / 5.6 / 5.8 / 7.5 / 8.5 | 3,600 / 1 / 5.5 / 5.6 / 7.0 / 7.9 |
  | Pro Max Slim | 1,195 / 6 / 2.3 / 2.5 | 3,597 / 4 / 5.5 / 5.7 / 7.7 / 8.6 | 1,792 / 9 / 5.5 / 5.8 / 8.8 / 9.9 |
  | Precision 7875 | 1,200 / 1 / 0.9 / 1.0 | 3,600 / 1 / 1.6 / 1.7 / 4.1 / 5.2 | 1,797 / 4 / 1.6 / 1.7 / 4.7 / 5.8 |

  The replay's accuracy on the EVO-X2 was unchanged: 2D median 3.8 px, 3D median 4.2 mm, 2 switches.
- On 26 October the three rigs moved to the `v2.2.0` release (12ba9ad). It adds pull requests 197 (memory fences in the live feed for ARM processors) and 201–205 (docs, and the version). Both environments on each rig report campy 2.2.0. The rig checks were all `RIG_CLEAN` and matched the table above to within 0.1 ms, with nothing skipped or misaligned and every camera keeping all its frames. The Pro Max Slim's replay was run twice: the first run had the same times, but was marked `RIG_CONTAMINATED` because the computer's security software read 482 MB/s of disk during it. These checks still use 3 emulated cameras, on all three machines.

  | Machine | 1 camera, sets / time per set p50 / p99 | 3 cameras, sets / time per set / frame to 3D p50 / p99 | Replay, sets / time per set / frame to 3D p50 / p99 |
  |---|---|---|---|
  | EVO-X2 | 1,800 / 3.0 / 3.1 | 3,600 / 5.6 / 5.8 / 7.5 / 8.4 | 3,600 / 5.5 / 5.6 / 7.0 / 7.9 |
  | Pro Max Slim | 1,198 / 2.3 / 2.5 | 3,598 / 5.5 / 5.7 / 7.8 / 8.6 | 1,790 / 5.5 / 5.7 / 8.7 / 9.5 |
  | Precision 7875 | 1,200 / 0.9 / 1.1 | 3,600 / 1.6 / 1.7 / 4.1 / 5.2 | 1,797 / 1.6 / 1.7 / 4.7 / 5.6 |

  The replay's accuracy on the EVO-X2 was again 2D median 3.8 px, 3D median 4.2 mm, 2 switches.
- Later on 26 October the EVO-X2 alone moved on to dev 78c252a. It adds pull requests 207 (the USB 2 alert and the launch check's USB check on Windows) and 208 (alerts for the AMD GPU's firmware stalls, hung jobs and resets, from the kernel log: [Alerts](alerts.md#the-gpus-kernel-log-amd-on-linux)). Neither changes tracking, and the version is still 2.2.0 in both environments. The Windows rigs were not updated. The rig checks were all `RIG_CLEAN`, with nothing skipped or misaligned, every camera keeping all its frames and `campy-live` starting at frame 1, and they matched the v2.2.0 row above to within 0.1 ms:
  - 1 camera: 1,800 sets, 3.0 / 3.1 ms per set
  - 3 cameras: 3,600 sets, 5.6 / 5.8 ms per set, frame to 3D 7.6 / 8.5 ms
  - replay: 3,600 sets, 5.5 / 5.6 ms per set, frame to 3D 7.0 / 8.0 ms, with 2D median 3.8 px, 3D median 4.2 mm and 2 switches

  In both recordings, `campy-acquire` read the kernel log through journalctl, without sudo, and found no GPU error.
- On 27 October the three rigs moved to dev `7f6983b`. It adds pull requests 207 (Windows: an alert when a USB 3 camera comes back on a USB 2 link), 208 (the EVO-X2's GPU kernel-log alert) and 212 (Windows: `campy-live` keeps off its GPU's interrupt CPU), plus docs. Both environments on each rig report campy 2.2.0 (the version number didn't change). All nine rig checks were `RIG_CLEAN`, with nothing skipped or misaligned, and matched the v2.2.0 table above: to within 0.1 ms on the EVO-X2 and the Precision 7875, and within 0.3 ms on the Pro Max Slim (its 1-camera p99 2.8 against 2.5 ms; the rest within 0.2). On the Windows rigs `campy-live` now names its GPU's interrupt CPU when it starts and leaves it out: CPU 0 for the Precision's RTX 6000 Ada (it runs on CPUs 1–47), CPU 1 for the Pro Max Slim's RTX 2000 Ada (it runs on the performance cores but CPU 1). spock's campy copy, which `campy-infer` uses, moved to the same commit.

  | Machine | 1 camera, sets / time per set p50 / p99 | 3 cameras, sets / time per set / frame to 3D p50 / p99 | Replay, sets / time per set / frame to 3D p50 / p99 |
  |---|---|---|---|
  | EVO-X2 | 1,800 / 3.0 / 3.1 | 3,600 / 5.6 / 5.8 / 7.6 / 8.4 | 3,600 / 5.5 / 5.6 / 7.1 / 8.1 |
  | Pro Max Slim | 1,198 / 2.3 / 2.8 | 3,598 / 5.5 / 5.7 / 7.8 / 8.6 | 1,791 / 5.5 / 5.8 / 8.8 / 9.7 |
  | Precision 7875 | 1,200 / 0.9 / 1.2 | 3,600 / 1.6 / 1.8 / 4.1 / 5.3 | 1,797 / 1.6 / 1.7 / 4.6 / 5.6 |

  The replay's accuracy on the EVO-X2 was again 2D median 3.8 px, 3D median 4.2 mm, identity 0.9993, 2 switches.
- Later on 27 October the three rigs moved to dev `4cb0e3a`. It adds pull request 209 (an optional automatic restart of a camera's ffmpeg that stops mid-recording, `restartFfmpeg`, off by default: [Alerts](alerts.md#restarting-a-stopped-ffmpeg-restartffmpeg)), plus docs. Nothing in it changes live tracking. Both environments on each rig report campy 2.2.0. After the rig checks, `restartFfmpeg: True` went into each rig's profile, so their recordings now restart a camera's ffmpeg that stops (`campy-config show` names the profile as its source on each).

  All nine rig checks were `RIG_CLEAN`, with nothing skipped or misaligned, and every camera kept all its frames. They matched the 7f6983b table above, to within 0.1 ms, but for two runs:

  - The Pro Max Slim's 1-camera p99 was 2.5 ms, back to v2.2.0's, against 2.8 ms.
  - The Precision 7875's first 1-camera run had one slow stretch (p99 4.7 ms, p50 0.9). Its rerun a few minutes later gave p99 1.1 ms, which is the number below.

  spock's campy copy, which `campy-infer` uses, moved to the same commit.

  | Machine | 1 camera, sets / time per set p50 / p99 | 3 cameras, sets / time per set / frame to 3D p50 / p99 | Replay, sets / time per set / frame to 3D p50 / p99 |
  |---|---|---|---|
  | EVO-X2 | 1,800 / 3.0 / 3.1 | 3,600 / 5.6 / 5.8 / 7.6 / 8.5 | 3,600 / 5.5 / 5.6 / 7.1 / 8.1 |
  | Pro Max Slim | 1,198 / 2.3 / 2.5 | 3,595 / 5.5 / 5.7 / 7.8 / 8.5 | 1,791 / 5.5 / 5.7 / 8.7 / 9.5 |
  | Precision 7875 | 1,200 / 0.9 / 1.1 | 3,600 / 1.6 / 1.7 / 4.1 / 5.3 | 1,797 / 1.6 / 1.7 / 4.6 / 5.7 |

  The replay's accuracy on the EVO-X2 was again 2D median 3.8 px, 3D median 4.2 mm, identity 0.9993, 2 switches.

- On 30 October, before the 2.3.0 release, the three rigs ran their checks again at dev `dcb349d`. It has all of 2.3.0's code, including pull requests 217 (the encoder retry), 222 (the cage) and 224 (the trigger-pause warning). These checks use no drawn cage, so the cage itself isn't tested here. Both environments on each rig report campy 2.2.0, the version before the release's own number.

  In the runs below, every rig lock verdict was `RIG_CLEAN`, nothing was skipped or misaligned, and every camera kept all its frames. They matched the 4cb0e3a table above to within 0.3 ms, but for the Pro Max Slim's 1-camera p99 of 2.9 ms against 2.5. Three first tries were rerun:

  - The Pro Max Slim's first 1-camera run skipped 3 sets near its start (worst set 38.3 ms). The rerun skipped none, and it's the number below. Its first run of the day had also skipped sets at start-up on 29 October (7 sets, and 9 in the 3-camera run after it), so this isn't a one-off. The cause isn't known yet.
  - The Precision 7875's 1-camera and 3-camera runs were marked `RIG_CONTAMINATED` by antivirus load. Neither skipped a set, though the 1-camera run's p99 rose to 3.7 ms. The reruns were clean.

  | Machine | 1 camera, sets / time per set p50 / p99 | 3 cameras, sets / time per set / frame to 3D p50 / p99 | Replay, sets / time per set / frame to 3D p50 / p99 |
  |---|---|---|---|
  | EVO-X2 | 1,800 / 3.0 / 3.1 | 3,600 / 5.6 / 5.8 / 7.6 / 8.5 | 3,600 / 5.5 / 5.6 / 7.1 / 8.0 |
  | Pro Max Slim | 1,195 / 2.3 / 2.9 | 3,597 / 5.5 / 5.7 / 7.8 / 8.6 | 1,792 / 5.5 / 5.7 / 8.8 / 9.8 |
  | Precision 7875 | 1,200 / 0.9 / 1.1 | 3,600 / 1.6 / 1.7 / 4.0 / 5.2 | 1,797 / 1.6 / 1.7 / 4.6 / 5.8 |

  The replay's accuracy on the EVO-X2 was again 2D median 3.8 px, 3D median 4.2 mm, identity 0.9993, 2 switches.

## Four cameras on the Pro Max Slim (26 October 2026)

A fourth FLIR camera joined the Pro Max Slim's three, on the same trigger. `campy-session` recorded the four at 30 fps for 10 minutes, twice with 3 mice in the cage and four times with 5, while `campy-live` tracked them in 3D with a new 4-camera calibration and `campy-live-view` showed it. Defaults of 2.2.0rc1 (fixed crops, CUDA graphs, TensorRT); every camera kept all 18,000 of its frames each time. Times in ms, median / p99:

| 4 cameras, 30 fps, 10 min | Sets tracked | Inference | Frame to 3D | 3D alone |
|---|---|---|---|---|
| Run 1, 3 mice (`RIG_CLEAN`) | 17,999 of 18,000 (1 skipped at the start) | 5.2 / 6.7 | 8.2 / 10.3 | 1.2 / 2.1 |
| Run 2, 3 mice | 18,000 of 18,000 | 5.2 / 6.7 | 8.3 / 10.4 | 1.3 / 2.2 |
| Run 3, 5 mice (a load sighting only after the recording, in the remux) | 18,000 of 18,000 | 7.3 / 8.9 | 10.9 / 13.0 | 1.7 / 2.5 |
| Run 4, 5 mice (the same) | 18,000 of 18,000 | 7.3 / 9.3 | 11.0 / 13.2 | 1.7 / 2.5 |
| Run 5, 5 mice (another program wrote up to 442 MB/s to disk for a minute) | 18,000 of 18,000 | 7.3 / 8.8 | 10.9 / 12.9 | 1.7 / 2.5 |
| Run 6, 5 mice (other programs used 2–3 CPU cores twice) | 18,000 of 18,000 | 7.3 / 13.3 | 11.4 / 17.6 | 2.1 / 2.8 |
| 4 emulated cameras, 60 fps, 5 mice (`RIG_CLEAN`) | 3,593 of 3,600 (4 skipped in the first 10 s) | 7.2 / 7.9 | 10.0 / 11.2 | 1.2 / 1.9 |

With 5 mice, each camera found all 5 in 69–92% of its frames, and all 5 had a 3D pose in 92.7–95.4% of the sets, 4 in 4.6–7.1%, and never fewer than 3 (at most 0.3% of the sets); with 3 mice, all 3 had one in 99.6–99.7%. A mouse needs only two of the four cameras for its 3D pose, which is how 3D finds all 5 more often than any one camera does. Run 6 shows what other programs on the computer cost: its 99th percentile rose by about 4.5 ms while its median moved 0.5 ms, and nothing was skipped. One more start, between runs 5 and 6, failed in its first seconds: every camera's Quick Sync encoder refused to open, and 40 s later the next start worked. Since pull request 217, campy starts such an ffmpeg again on its own ([Troubleshooting](troubleshooting.md)). In runs 1 and 2, where the 2D allowed a sure match in all four cameras, `campy-live` grouped the same detections into each mouse 99.4% of the time (`bench/pose3d_live_log.py`). Its 3D poses reprojected within 3.3–3.8 px at the median. The 60 fps row records 4 emulated cameras with Quick Sync, playing a 5-mouse recording. With 3 cameras the same check gives 5.5 / 5.7 and 7.7 / 8.6 ms.

### How well those runs tracked, and where they failed (evaluated 27 October)

The six good runs above were copied off the Pro Max Slim and looked at set by set: `campy-live`'s log, the camera frame times, and SLEAP run again afterwards on the same videos (`campy-infer --exact`: sleap-nn's own layer in fp32, on an A100, same two models). The same 3D step (`campy/pose3d.py`) was then run over those offline predictions, so live and offline differ only in the 2D. Median / p90 where two numbers are given:

| 10 min, 4 cameras | Run 1 (3 mice) | Run 2 (3 mice) | Run 3 (5) | Run 4 (5) | Run 5 (5) | Run 6 (5) |
|---|---|---|---|---|---|---|
| Each camera found every mouse, % of frames (lowest–highest camera) | 91–99 | 93–99 | 80–90 | 75–86 | 69–92 | 70–82 |
| Every mouse with a 3D pose, live, % of sets | 99.6 | 99.7 | 92.7 | 94.1 | 95.4 | 92.9 |
| The same, not counting 3D poses far outside the cage | 99.5 | 99.6 | 91.6 | 93.1 | 94.4 | 87.6 |
| The same from offline SLEAP | 99.5 | 99.6 | 91.2 | 92.9 | 94.3 | 88.0 |
| 3D poses far outside the cage (of all the mice's 3D poses) | 0.02% | 0.04% | 0.25% | 0.23% | 0.22% | 1.1% |
| Identity switches not undone within 1 s | 4 | 0 | 13 | 21 | 9 | 31 |
| Reprojection error of the 3D poses, px (every node, as logged; the 3.3–3.8 px above counts only well-scored nodes) | 3.8 / 9.6 | 4.4 / 12.0 | 4.4 / 14.7 | 4.7 / 16.5 | 4.1 / 13.5 | 5.1 / 19.6 |

- **Tracking live lost nothing against SLEAP afterwards.** Offline, each camera found the same mice in all but 0.2–2% of its detections. The keypoints were 0.2–0.5 px apart at the median (p90 3–5 px: offline reads the H.265 video, live the camera's own frames, and live runs in fp16), the 3D poses 0.7–1.1 mm apart, and every mouse had a 3D pose in as many sets, within 0.6 points. So with these models the limits are the models and the camera views, not doing it live.
- **Most failures are mice piled together.** In this cage, about 26 × 22 cm, 5 mice are rarely apart: a mouse's nearest neighbour was 6–7 cm away at the median. When two or more lie against or on top of each other, three of the four cameras often find one mouse where there are two. The 3D step then has too few detections to go round, and identities swap. In run 6, the worst, they often lay piled along one wall: each camera found all 5 in only 70–82% of its frames, and identities switched 31 times.
- **Some 3D poses are impossible.** When a camera's detection is grouped with another mouse's detections in the other cameras, the triangulated point can land far from the cage, up to metres away, often with a reprojection error under 25 px. That happened to 0.2–1.1% of the mice's 3D poses with 5 mice, mostly ones seen by only 2 or 3 cameras (3–10% of 2-camera poses). Counting those as not found, every mouse had a 3D pose in 87.6–94.4% of the sets with 5 mice, not 92.7–95.4%. Most last 1–3 sets (up to 40 in run 6). A rule for closed-loop outputs that asks where a mouse is would take them as real.
- **Why the views can be confused.** All four cameras look into the cage from the same side, so their views of it are close: seen from the cage, the angle between two cameras is 11–41° (11° between Camera0 and Camera2). Two mice at different depths along nearly the same line of sight look alike to all four.
- **Most of the missed piled mice can be seen.** 20 sets were picked at random from the failed ones of the four 5-mouse runs (fewer than 5 mice with a 3D pose in the cage, and some camera finding fewer than 5: 6–12% of each run's sets), and all four views of each were looked at by eye on 28 October. In 9 the missing mouse was clearly visible in two cameras or more, and in 4 more partly visible (a head or half a body). In 6 it was hidden in at least three of the four views, under or behind another mouse. In 1, enough cameras found it and the 3D step lost it. So a model trained on more piles could find about two in three of them, and the rest need a camera looking from another side. Also seen: a second detection on a lone mouse (3 sets, two of them the same pile 3 s apart) and detections in a dark, empty corner of the image (1 set). This is one person's reading of 20 sets, so the shares are rough: about ±20 points.
- **The 3D step's settings barely help.** Re-running it over the live 2D of the four 5-mouse runs with 10 other settings (minimum node score 0.3, weights by node score, a birth threshold, staleness 20 or 120 frames, twice the 3D term's weight, a tighter 3D distance, a floor of 1 on the 3D term, and lower reprojection thresholds) changed the share of sets with every mouse by under 0.5 points. Only the floor of 1 (`max_penalty`) gave fewer swaps in three runs (28, 18 and 6 against 34, 27 and 13, counting every swap, even one undone at once) and as many in the fourth. Refusing a camera's detection whose match scores badly made it much worse: no new identity can start once all 5 exist, so a mouse lost that way is rarely matched again. Keeping a 3D pose far outside the cage out of the next set's matching changed nothing either.
- **A lower centroid threshold finds more of the piled mice.** Offline, with the centroid model's peak threshold at 0.1 instead of 0.2 (keypoints still at 0.2; `campy-live --centroid-threshold 0.1`), each camera found all 5 mice in 2–8 points more of its frames, and every mouse had a 3D pose (outside-the-cage poses not counted) in 94.2% instead of 91.2% of run 3's sets and 91.3% instead of 88.0% of run 6's. But there were more short identity swaps, and about 3 in 10 of the extra detections lay within 40 px of another detection, so some may be the same mouse twice. Labelled frames would tell. This was measured offline only.
- **A sharper instance model gives better keypoints, not more mice.** The same centroid model with the centered-instance model trained at full scale (320 px crops at scale 1.0 instead of 0.5) found exactly the same mice, offline in runs 5 and 6. Its 3D poses' reprojection error fell from 4.1 to 3.7 px and from 5.1 to 4.4 px at the median (p90 13.5 to 11.9 and 19.7 to 17.6 px). On its own validation labels its error is 3.5 px at the median against 3.9. Its networks took 3.5 times as long offline (fp32 on the A100). At 30 fps the Pro Max Slim may have the time, as a set takes 7.3 of the 33 ms between frames now, but it hasn't been timed live.
- **A trigger pause went unnoticed.** In run 4, 7 min 14 s in (frame 13,024), every camera's own clock shows 0.40 s and then 1.93 s between two frames, and no camera's frame counter skipped. So the trigger stopped for 2.3 s, and no frame was lost. Nothing reported it: no alert, and the recording's post-processing didn't run `campy-align`, which counts such gaps. Since pull request 224, both do: the recording watch warns about a pause as it happens, and `campy-session` runs `campy-align` after the recording ([Alerts](alerts.md), the `align` step above). Each `frametimes.npy` has the real times, but anything that counts frames at 30 fps is 2.3 s off after that point.
- **It is the only one found so far.** Every recording on the Pro Max Slim's disk was checked on 28 October for gaps that every camera shares. Of these, 34 were triggered by campy's trigger board (`startArduino`), on the same serial port each time, from 19 to 26 October: 3.8 hours and about 594,000 pulses, at 10, 30 and 60 fps, up to 70 minutes long. Run 4's pause is the only one. One pause in 3.8 hours can't tell how often it happens: with this little data, anywhere from about once an hour to once in 150 hours. In the same recordings, no single camera missed a pulse that the others got. Two recordings on 20 October had a different fault, since fixed: one camera's first frame came 4.5 s before campy started the trigger, a frame left in the camera by the run before, so counted from the start that camera's frames are one ahead of the others'. campy has discarded such frames since pull request 94 ([FLIR cameras](flir.md#frame-rate-frame-size-and-usb-limit)), and it hasn't happened in the 15 triggered recordings since.

What changes with a camera more:

- **campy's config**: `numCams: 4`, and per-camera lists such as `cameraGain` need a fourth value. campy picks cameras by their place in the FLIR driver's list (`cameraSelection`), not by serial, so check with `campy-config show <config> --cameras`, or with campy-acquire's "Opened Camera0 ... serial#" lines, that the older cameras kept their names. Their calibration is matched by name.
- **A new calibration** with all four cameras: `campy-live` matches the calibration's cameras to the feed's by name, and stops if one is missing.
- **`--cameras 4`** for `campy-live`, `campy-live-view` and `campy-watch` run by hand (3 by default). `campy-session` passes `numCams` itself.
- **New TensorRT plans**, built once at the first start (below).
- **USB**: the launch check adds up the cameras' data on each USB controller, on Windows too: four 1280 × 1024 Mono8 cameras at 30 fps are about 157 MB/s, 52 % of the 300 MB/s it gives a controller whose cameras link at 5 Gb/s. At 60 fps (315 MB/s) it warns, as Windows doesn't say whether the controller runs at 5 Gb/s or faster: stream every camera at once at the full rate (SpinView) and set `usbBudgetMBps` to what it carried.

## Eight cameras on one RTX A2000 (29 October 2026)

A Dell Pro Tower Plus (Intel Core Ultra 9 285, NVIDIA RTX A2000 12 GB, a 70 W card) records 8 FLIR cameras, and has one GPU for both jobs. To see whether it can also track all 8 live, campy 2.2.0 recorded 8 emulated cameras there, encoding H.265 on the A2000's NVENC, while `campy-live` tracked them in 3D on the same card. The cameras played all 8 views of the 60 s, 5-mouse recording whose 3 views the rig checks use, and 3D used that recording's own calibration. `campy-live` ran the rig checks' trained models with its defaults (fixed crops, CUDA graphs, TensorRT, the GPU clock held). No real camera was opened. Times in ms, median / p99:

| 8 emulated cameras, 5 mice | Sets tracked | Inference | Frame to 3D | 3D alone |
|---|---|---|---|---|
| 30 fps, 120 s, recording H.265 on the A2000 (NVENC) | 3,600 of 3,600 | 21.5 / 23.1 | 26.2 / 30.5 | 2.2 / 6.1 |
| 30 fps, 120 s, recording AV1 on the iGPU (Quick Sync) | 3,596 of 3,600 (4 skipped in the first second) | 17.7 / 19.0 | 22.3 / 26.0 | 2.1 / 5.6 |
| 60 fps, 60 s, recording H.265 on the A2000 (NVENC) | 2,173 of 3,600 (1,427 skipped) | 25.3 / 28.5 | 39.3 / 50.9 | 2.8 / 7.2 |
| 60 fps, 60 s, recording AV1 on the iGPU (Quick Sync) | 2,689 of 3,600 (911 skipped) | 20.0 / 20.6 | 33.7 / 44.3 | 2.6 / 6.7 |

- **30 fps fits; 60 fps does not.** At 30 fps a set took 21.5 ms of the 33.3 ms between frames, nothing was skipped, and every 3D pose was ready within 30.5 ms at p99 (46.7 ms at most, in the first seconds). At 60 fps a set takes about 25 ms, longer than the 16.7 ms between frames, so `campy-live` tracked 36 sets a second and skipped the rest, as it does when it falls behind (it always takes the newest set).
- **The card's power limit holds it back.** Its clock sat at about 1,450 MHz at 30 fps and 1,350 MHz at 60 (1,875 MHz while the models loaded), held down by its 70 W power cap most of the time, at up to 79 °C. Its encoder was 23% busy at 30 fps and 48% at 60, so the recording was not the limit ([Video encoding](encoding.md#nvidia-gpu-encoding-nvenc): this NVENC keeps up with 14 such cameras at 60 fps).
- **Recording on the iGPU instead frees the A2000 for tracking.** The Quick Sync rows ran later that morning with the same cameras, models and calibration, but encoded AV1 (quality 88) with Quick Sync on the processor's integrated GPU ([Video encoding](encoding.md#intel-gpu-encoding-quick-sync)), so the A2000 only ran the models. A set took 4–5 ms less: 17.7 instead of 21.5 ms at 30 fps, and 20.0 instead of 25.3 at 60, where a quarter of the sets were skipped instead of 40%. So encoding 8 streams on NVENC slowed the models, though NVENC is a separate part of the chip. 60 fps still doesn't fit with these models. The 30 fps run ended `RIG_CLEAN`. The 60 fps run was marked contaminated by other programs' CPU use as it started: Windows Search's indexer, working on the user folder where the Python environments are (not on the recording: it doesn't index campy's folder, see [Troubleshooting](troubleshooting.md#timing-tests)), and the antivirus. Each was measured once.
- **The recording kept every frame**: each of the 8 cameras saved all 3,600 frames in every run.
- **3D**: in the 30 fps run all 5 mice had a 3D pose in 99.9% of the sets (4 in the rest), each seen by 8 cameras at the median, and the poses reprojected within 3.5 px at the median. Each camera found all 5 mice in 71–99% of its frames. Not checked: whether the right detections were grouped into each mouse (`bench/pose3d_live_log.py` cannot try every grouping of 5 animals in 8 cameras), and accuracy against offline SLEAP.
- **Other programs.** Both runs ended `RIG_CONTAMINATED`, by other programs' CPU use only. At 30 fps the antivirus and Windows Search used 2–2.5 cores while the models loaded and as the cameras started; after the first 10 s, each 10 s report's p99 inference was 22.2–23.3 ms. At 60 fps Windows' app and device-enrollment services used 4–5 cores at times. Each run was measured once.
- **Played at half speed.** The clips were recorded at 60 fps, so at 30 fps the mice moved half as fast as in life; that does not change the work per set.

To track 8 cameras at 60 fps here, the A2000 would need a lighter model or a faster card: the guide's model-size sweep has what each GPU does with 8 cameras ([Hardware guide](hardware.md)).

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

- **TensorRT** keeps its engines (*plans*) in `--engine-cache`, by default `~/.cache/campy/engines` (on Windows `C:\Users\<you>\.cache\campy\engines`). A plan's name comes from the network's weights, the input size (the centroid network's batch is the number of cameras, the centered-instance network's the cameras × `--max-instances`, so another camera or animal count builds new plans), the precision, the GPU, and the TensorRT and PyTorch versions. So another model, a retrained one, another GPU or an update gets a new plan, built once, and an old plan is never used by mistake. Old plans stay in the folder; delete them to free the space (35–45 MB per model pair).
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
Each run's recording goes into a new folder inside `videoFolder`, as usual, and the clips stay where they are. As with a hardware trigger, campy starts every emulated camera at the same moment once all are ready, so frame n of each camera reaches the computer within a few milliseconds of the others', as with real cameras on a trigger. Each emulated camera used to start on its own, and `campy-live` counted every set as misaligned. Use videos of the same length, recorded together, so that each set of frames shows one moment. Each test machine recorded three emulated cameras playing a 5-mouse session at 60 fps for 60 s while `campy-live` tracked them in 3D, and kept all 3,600 frames per camera. With campy's defaults since pull request 147, which run the networks as compiled engines, the Precision 7875 and the Pro Max Slim skipped no set and the EVO-X2 skipped 2. Frame to 3D pose took 4.1 ms on the Precision 7875, 8.1 ms on the EVO-X2 and 8.2 ms on the Pro Max Slim. With 2.1.0rc7's defaults it took 5.2, 12.8 and 14.8 ms, and with 2.1.0rc6 13.3, 18.6 and 27.6 ms. The hardware guide has all of them. [Recording with campy-acquire](acquire.md#emulated-cameras-cameramake-emu) explains the details.

## On Linux (AMD, ROCm)

- **First use of a model**: AMD's MIOpen library tunes every new convolution shape the first time it sees it, so the first warm-up of a new model can take about 10 s. The tuning is cached for later runs.
- **Disk stalls**: some SSDs stop completing writes for 10–16 s. The EVO-X2's config (`configs/campy_config_evox2.yaml`) holds up to 60 s of frames in memory (`writeQueueMaxSec: 60`) so a stall drops nothing. Better still, record to a second drive.
- **Encoding**: VA-API runs on the same GPU as inference. On the EVO-X2 that cost inference only a few percent.

## On Windows (NVIDIA, CUDA)

- **Sleep**: campy keeps Windows awake while it records (`keepAwake`, on by default). If campy is started over SSH while nobody is signed in, Windows refuses the screen part of that request: set the screen timeout to Never as well (Settings > System > Power).
- **Latencies in 15–16 ms steps (fixed)**: on Windows, up to Python 3.12, Python's `time.monotonic` moves in steps of 15.6 ms. campy used to take its host times from it (`hosttimes.npy`, the live feed, and `campy-live`'s timestamps), so the latencies `campy-live` reported there came in 15–16 ms steps (15, 16, 31, 32, 47 ms...). Since pull request 118, campy takes every host time from Windows' precise clock instead (`time.perf_counter_ns`, QueryPerformanceCounter, which Python 3.13 uses for `time.monotonic` anyway): one clock in every process, so the camera processes, `campy-live` and `campy-live-view` still compare their times directly, and latencies are exact to well under a millisecond (on the Pro Max Slim, a 60 fps recording's `hosttimes.npy` then showed frames 16.6 ms apart, and `campy-live` read each set 0.9 ms after its last frame arrived, where it used to say 0, 15 or 16 ms). Inference times were always precise. Latencies measured on Windows before this, such as the hardware guide's 31 / 32 ms, are rounded to those steps. A program that writes frames into the live feed itself (a replay script, say) must stamp them with the same clock, `campy.clock.HostNs`: with `time.monotonic_ns`, a different clock on Windows, the latencies `campy-live` reports mean nothing (40–56 ms in a replay whose real latency was 13 ms).
- **Encoding on the Intel iGPU** (Quick Sync, `gpuMake: "intel"`) keeps the encoder off the inference GPU.
- **The GPU's clock**: an NVIDIA GPU chooses its own clock speed from how busy it looks. Live tracking gives it a short burst of work for each frame, and that didn't look busy: on the Pro Max Slim's RTX 2000 Ada the clock sat at 780 MHz of its 2,500 while tracking, and fell to 210 MHz while `campy-live` waited for the recording to start. So `campy-live` keeps a tiny piece of the GPU busy, at the lowest priority, from the end of its warm-up until it ends (`--hold-gpu-clock`, on by default with NVIDIA and AMD GPUs; [the first sets](#the-first-sets-after-waiting-for-the-feed) say why it starts then). The models' work goes first, so it isn't slowed. That took the time per set from 27.6 to 13.2 ms with 3 mice in view, so the same models now fit 60 fps, and the first sets of a recording are no longer slow. The cost is about 20 W more while waiting, for at most 10 minutes without a feed. `--hold-gpu-clock off` turns it off. While the frames come, `campy-live` reads the GPU's clock every half second and spins 0, 1 or 2 blocks, as many as keep the clock up (`--hold-gpu-clock-blocks auto`, or a fixed number): one block costs the models about 0.3 ms per set, so none spin when the GPU keeps its clock by itself, as the Pro Max Slim's card did right after a restart. When a try at one block fewer turns out too few (the clock falls again within 10 s), `auto` puts the block back and doesn't try that count again on that GPU until the computer restarts: it remembers it in `gpu_clock.json` in `--engine-cache`, by the GPU's UUID. Before, it kept retrying, and on the RTX 6000 Ada, which needs 1 block, each try cost 1.3 s of sets 3–4 times slower (p99 5.1–5.7 ms; now 1.7–2.0 ms, pull request 152). It reads the clock through NVIDIA's NVML (the `nvidia-ml-py` package, if installed in campy-live's environment), else `nvidia-smi` (a small program that `campy-live` starts and ends; it ends with `campy-live` even when `campy-live` is killed or crashes, so it never keeps polling the GPU or holds a folder open after), and on AMD from the driver. AMD's GPUs slow down too, less: the EVO-X2's ran the default models at 2,117 of its 2,900 MHz, 9.9 ms per set instead of 8.9, so the hold is on there by default as well. On the EVO-X2, whose AMD GPU shares one chip with the CPU, `auto` keeps at least 1 block spinning while the frames come. With none, its GPU clock still read 2,650–2,850 MHz, yet every set took about 1 ms longer and the CPU's 3D step too. So the whole chip slows down between sets in a way the GPU clock doesn't show. With 5 mice, 3 cameras, `--fixed-crops --cuda-graphs`, two replays each: 0 blocks 13.7 / 14.2 ms per set (p50 / p99), 3D step 1.3–1.5 ms; 1 block, which `auto` now keeps, 12.8 / 13.0 ms, 3D step 0.85 ms (with `--fast-upsample`, 10.6 / 10.7–10.8 ms). The chip ran at its power limit, about 85 W, in every one of those replays (the replay's own video decoding included), so the block cost no measurable power there. NVIDIA GPUs and AMD's own graphics cards are unchanged: there `auto` still goes down to none when the clock holds by itself. A number (`--hold-gpu-clock-blocks 0`, `1`, `2`) still overrides `auto` everywhere.
- **Hybrid Intel CPUs**: `campy-live` runs itself on the performance cores at High priority, and moves campy-acquire's camera processes and encoders onto the efficiency cores when their feed appears (`--cpus auto`, `--priority auto` and `--campy-cpus auto`, the defaults). With 3 cameras recording, that saved about 2 ms per set. `python -m campy.cpus` lists which cores are which. `--cpus none --campy-cpus none` leaves both programs where Windows puts them. The move needs a campy-acquire from this version on, whose live feed reports its processes; with an older one, `campy-live` says so and leaves campy-acquire alone.
- **CUDA graphs** (`--cuda-graphs`, on by default with a GPU): each model's GPU work is recorded once and then replayed in one go. The keypoints are identical; on the RTX 2000 Ada it saved about 0.8 ms per set (13.2 to 12.4 ms with 3 mice). Each number of animals found gets its own graph (up to 15 at 3 cameras with up to 5 animals each, 40 at 8), and all of a model's graphs share one block of GPU memory. On the RTX 6000 Ada, `campy-live`'s graphs hold 0.26 GB at 3 cameras and 0.65 GB at 8; with a block per graph, as before, they held 1.7 and 11.2 GB, for the same time per set. `campy-live` says how much its graphs hold when it starts.
- **channels-last** memory layout is the default on NVIDIA GPUs (`--memory-format auto`), and is faster there.
- **A monitor** plugged into the inference GPU looked like it slowed it, but on the Precision 7875 the slowdown turned out to be the interrupt core below: unplugging the RTX 4000 Ada's monitor changed nothing. With two GPUs, plugging the monitor into the other one is still tidy, but not needed for speed.
- **Windows: inference stays off its GPU's interrupt core** (`--cpus auto`, the default, since pull request 212). Nearly every GPU kernel ends with an interrupt that Windows handles on one fixed CPU, about 20 µs each. If `campy-live` runs on that CPU, every kernel launch waits for it: on the Precision 7875 the RTX 4000 Ada's interrupts go to CPU 2, and a benchmark thread there took 16.7 ms per set instead of 8.4. So before it loads the model, `campy-live` runs about 0.1 s of tiny kernels on its GPU, counts each CPU's interrupts and DPCs while they run and for as long without, and leaves the CPU that took them out of its own CPUs. It says so when it starts, for example `campy-live on CPUs 0-1,3-47 (all but CPU 2, which handles the NVIDIA RTX 4000 Ada Generation's interrupts)`, and `campy-live-session.json` keeps what it found under `cpus`. On a CPU with one kind of core it stays at normal priority; on a hybrid Intel CPU it takes the performance cores without that CPU, at High priority as before, and `campy-live-view`'s `c` key keeps it off that CPU too. When no CPU stands out it pins as before and says why; `--cpus none`, `all` or a list skip the test. `python -m campy.cpus --gpu 1` says which CPU handles GPU 1's interrupts (torch's numbering). Windows picks the interrupt CPU and may change it after a restart or a driver update, which is why `campy-live` looks each time it starts. On the Precision it found CPU 2 for the RTX 4000 Ada and CPU 0 for the RTX 6000 Ada in every one of 12 starts (78–90% of the test's extra interrupts and DPCs). Replaying 3 cameras with 5 mice and 3D on the RTX 4000 Ada for 60 s, every run `RIG_CLEAN`, inference p50 / p99:
  - without compiled engines or CUDA graphs (`--engine off --cuda-graphs off`, the most kernel launches): 10.1 / 15.9 ms anywhere (two runs, one skipped set) against 10.0 / 12.0 ms off CPU 2 (two runs, none skipped); frame to 3D p99 19.3–19.5 against 15.0 ms.
  - with the default TensorRT engines: 3.3 / 5.2 and 3.3 / 3.4 ms anywhere, against 3.3 / 3.4 ms in both runs off CPU 2. Engines launch few kernels, so only the stretches that landed on CPU 2 (one of the two runs) were slower.
  - with CUDA graphs but no engine: 6.4 / 6.5 ms either way.
  - On the RTX 6000 Ada (default engines), whose interrupt CPU is CPU 0, which Windows already keeps programs off: 1.6 / 1.7 ms in both runs anywhere and in two of three runs off CPU 0; the third, the first after its engines were built, had one 10 s stretch with p99 5.9 ms (1.6 / 5.4 over the run).
  - On the Dell Pro Max Slim's RTX 2000 Ada (27 October, same replay, two runs each, every run `RIG_CLEAN`, GPU at 2,490 MHz), the interrupt CPU is CPU 1, one of the performance cores, so `campy-live` used to run on it. Before is `--cpus performance` (the old default: all 8 performance cores at High priority), after is the new default (the performance cores but CPU 1). The gain there is small, since the GPU's own work is most of each set: with the default TensorRT engines 5.5 / 6.3 and 5.5 / 5.7 ms before against 5.5 / 5.7 ms in both runs after; without engines or CUDA graphs 14.0 / 14.9–15.1 ms against 14.0 / 14.5–14.6 ms (frame to 3D p99 18.5–18.8 against 18.2–18.5 ms; 0–1 skipped sets either way); with CUDA graphs but no engine 12.4 / 12.6 ms either way. The test found CPU 1 in all 12 starts there (83% of its extra interrupts and DPCs).
  - Linux doesn't need it, and `campy-live` doesn't look there. On the EVO-X2 (27 October, Ubuntu, ROCm, same replay, every run `RIG_CLEAN`), the GPU's interrupts all go to CPU 30 (`/proc/interrupts`, IRQ 150; irqbalance is off), but ROCm raised only about 100 a second during a replay, against about 85,000 a second for the benchmark on Windows: it doesn't interrupt for each kernel. Keeping `campy-live` off CPU 30 changed nothing: with the default compiled models 5.5 / 5.6–5.7 ms per set either way (two runs each), and uncompiled 10.2 / 11.2–11.4 ms off CPU 30 against 10.2 / 11.3–11.5 ms as today. Pinned to one CPU, CPU 30 was no slower than CPU 29: uncompiled 11.7 / 14.4 ms on CPU 30 against 11.7 / 14.5 on CPU 29, compiled 5.5 / 5.6 against 6.0 / 7.9 (one run each; CPU 29 doesn't take the GPU's interrupts, so that difference is something else). One CPU is slower than many for the uncompiled models, wherever it is.
- **Two GPUs**: run `campy-live` on one and let campy's encoders use the other. torch numbers GPUs fastest first unless told otherwise, and `nvidia-smi` by their slot, so start `campy-live` with `CUDA_DEVICE_ORDER=PCI_BUS_ID` and `CUDA_VISIBLE_DEVICES=<the GPU's nvidia-smi index>`. Give the cameras the other GPU's `gpuID` for NVENC. On the Precision 7875 that is `CUDA_VISIBLE_DEVICES=0` (RTX 6000 Ada) for `campy-live` and `gpuID: 1` (RTX 4000 Ada, which also drives the monitor) for the encoders; while it recorded, `nvidia-smi` listed `campy-live` only on the first and ffmpeg only on the second.

## A tracked recording in one command (campy-session)

`campy-session run <config>.yaml`, in the recording environment, starts `campy-live` (in the inference environment), `campy-live-view` and `campy-acquire` from one config, and ends them together. The config holds campy-acquire's keys plus a `live:` section (the trained models, `animals`, `calibration`, `env`, more campy-live `args`) and an optional `view:` section. Every program's output goes into the recording's folder (`logs/`), and so do `campy-live`'s results. `campy-session register-live`, run once in the inference environment, tells it where that environment is; `live.python` in the config does the same. After a clean stop it runs the `post:` section's steps: remux the videos to plain MP4 (each checked packet by packet), `campy-live-export`, an optional copy to another folder or computer, and your own commands. `campy-session post <folder>` runs them again by hand. `campy-session panel <config>.yaml` does the same from a page in the browser, for those who'd rather not use a terminal: Start, each camera's thumbnail and health, disk space, warnings, bookmarks, Stop. `campy-session shortcut <config>.yaml` puts an icon on the desktop that opens it ([Recording a session from the desktop](recording-a-session.md)). The recording comes first: campy-acquire is never killed, and it records on if `campy-session` or `campy-live` stops. See [Live tracking](live-tracking.md#in-one-command-campy-session).

## Live pose estimation (liveFeed, campy-live)

With `liveFeed: True` (or `--liveFeed True`), each camera process also copies every frame it grabs into a ring of shared memory, named `campy-live-0`, `campy-live-1`, ... for cameras 0, 1, ... (`liveFeedName: "campy-live"`), holding the newest `liveFeedSlots` (8) frames, each with its frame number, its camera timestamp, and when it reached the computer (as in "hosttimes.npy"). It is one copy per frame (0.05 ms for a 1280x1024 frame on the EVO-X2), and campy never waits for whatever reads it: a reader that falls behind finds the frames it missed replaced by newer ones. Each ring also says which folder its camera records into, so `campy-live-view` can put its bookmarks beside the recording. If the feed fails, the camera says so once and records on without it. Off by default.

`campy-live` reads the feed and runs a SLEAP top-down model (sleap-nn) on the cameras' frames as campy records. Each time, it takes the newest set of frames that every camera has (the same frame number on each), runs the model on the set as one batch, logs the keypoints, and goes straight back to the newest set. A set that came while it was busy is skipped and counted, never queued, so it is never more than one set behind. It runs in its own Python environment, the one with torch and sleap-nn: it imports only numpy and campy's `livefeed`, `liveresults` and `frametimes` modules, so the camera drivers are not needed there. In that environment:
```
pip install --no-deps -e <campy folder>     # or: PYTHONPATH=<campy folder> python -m campy.live ...
campy-live --cameras 3                                      # random-weight models of the benchmark's sizes
campy-live --cameras 3 --model sleap --centroid <centroid model folder> --instance <centered-instance model folder>
```
Start it before or after `campy-acquire`. It waits for the feed, and ends when the recording does (or at Ctrl-C). `--ready-file <path>` makes it write that file (JSON: its pid and the time) once its models are loaded and warmed up, as it starts waiting for the feed, so a program that starts `campy-acquire` after it knows when to. `--stop-file <path>` stops it, as Ctrl-C does, once that file is there: for a program that started it without a console. It writes `campy-live_<date_time>.npy`, one row per set (frame number, each camera's host time, the keypoints and their confidence, the centroids, and when the set was read and finished), and a summary `.json`: sets processed and skipped, the latency from frames to keypoints, and the skeleton, calibration and number of animals (for `campy-live-export`). It also writes "campy-live-session.json" into the recording's folder (found from the feed): the models' folders and SHA-256s and the settings they were trained with, the calibration's SHA-256, every option and whether it was given, its environment, and how it ended ([Recording folders](recordings.md#campy-live-sessionjson)). Like `campy-acquire`, it says at startup each changed default it relies on (`--defaults-version 2.1.0`, `--default-warnings off`). `python -m pydoc campy.live` describes the fields and the models. `--model random` (the default) times random-weight models of the sizes `bench/sleap_infer_bench.py` picked, with a fixed number of animals per frame; `--model none` only reads the feed. `--cuda-graphs` replays each model as a recorded CUDA graph. `--fixed-crops` crops `--max-instances` animals in every frame, padding the missing ones, so the GPU never stops mid-set to report how many it found and the results come back in one copy. With `--cuda-graphs` as well, the whole set is one graph. It gives the same animals and keypoints, and pays when about `--max-instances` animals are in view: with 5 mice it took each set from 15.0 to 13.6 ms on the EVO-X2, and from 10.3 to 2.7 ms on an RTX 6000 Ada ([Live tracking](live-tracking.md#fixed-crops---fixed-crops-on-by-default-with-a-gpu)). Both are on by default with a GPU (NVIDIA or AMD); `--fixed-crops off --cuda-graphs off` turns them off. With `--fixed-crops`, `--engine` runs the two networks as compiled engines, and its default, `auto`, does so with a GPU: TensorRT on NVIDIA where `tensorrt` and `onnx` are installed, otherwise `torch.compile` where Triton is (it comes with PyTorch on Linux). With 5 mice it took the EVO-X2 from 10.6 to 5.9 ms per set (`torch.compile`), the Pro Max Slim's RTX 2000 Ada from 12.6 to 5.8 ms and an RTX 6000 Ada from 2.7 to 1.6 ms (TensorRT), with tracking as good against offline SLEAP. If no engine can be made, `campy-live` says why in one line and runs the networks in plain PyTorch. The first start with a new model or update takes up to about 40 s while the engines are made; they are kept in `--engine-cache` (`~/.cache/campy/engines`), and later starts are quick. `--engine off` turns it off, and `--engine on`, `compile` or `tensorrt` stops `campy-live` if that engine can't be made ([Compiled networks](live-tracking.md#compiled-networks---engine-on-by-default-with-a-gpu)). `--fast-upsample` (default `auto`: on with AMD GPUs when no engine runs) runs PyTorch's slow upsampling layers in a faster memory layout, with the same results.

Frames of one trigger pulse have the same frame number on every camera, as long as every camera starts on the trigger's first pulse (campy starts the trigger once all are grabbing) and none loses a frame before campy gets it. A camera that loses one (`flirFramesLost` in "metadata.csv") numbers its later frames one lower than the others. `campy-live` counts the sets whose host times are more than half a frame apart (`misaligned`).

The shared memory's layout is documented in `campy/livefeed.py`, and plain numpy can read it, from any program.

With `--calibration <calibration.toml>` (anipose's), `campy-live` also turns each set into 3D, in the same loop ([3D poses from live 2D predictions](pose3d.md#3d-poses-from-live-2d-predictions-campypose3dpy)): it works out which animal in each camera is the same one, gives each animal an identity (0 to `--animals` − 1) that it keeps from set to set, and triangulates each animal that 2 or more cameras see. The cameras are found in the calibration by campy's camera names, or by `--calibration-names`, in the feed's camera order:
```
campy-live --cameras 3 --model sleap --centroid <folder> --instance <folder> --animals 3 --calibration calibration.toml
```
Each row then also has `points3d` (per animal and node, x, y, z in the calibration's units, mm), `members` (which detection in each camera is that animal), `n_views`, `reproj_px` and `outside_cage`, and the summary gives the latency from frames to 3D.

#### The cage: 3D outside it is not found (`--cage`)

Sometimes one camera's detection of a mouse is grouped with another mouse's detections in the other cameras. Their lines of sight can still meet, but far from both mice. On the Dell Pro Max Slim FCS1250's 4-camera rig, whose cameras all look at the cage from one side, that place is often below the cage floor or above the cage, sometimes metres away. On six 4-camera recordings of 3 and 5 mice (26 October 2026), 0.2 to 1.1% of the 3D positions landed more than 25 cm from the cage's middle. A closed-loop rule ("mouse in region X", [campy-rules](closed-loop.md)) would act on such a position as if a mouse were there.

So you can draw the cage, once per calibration: in `campy-live-view`, press `k` and click the cage floor's corners, in the same order, in 2 or more cameras, then press Enter ([Watching it live](live-view.md)). campy keeps it beside the calibration, as `<calibration>.cage.json` (`calibration4.toml` → `calibration4.cage.json`): the floor's corners in 3D, its height (200 mm unless you change it) and a 30 mm margin. From then on, `campy-live` (`--cage auto`, the default) treats a mouse whose 3D lands outside the cage as **not found** that frame:
- its `points3d` and `reproj_px` are NaN, its `members` -1 (none of its detections are grouped) and `n_views` 0, and the row's `outside_cage` is 1 for it. `campy-rules`, `campy-live-export` and `campy-live-view` then all treat it as not seen.
- a pair of detections whose 3D lands outside the cage doesn't start a new identity, so no identity number is taken by a wrong pair.
- a mouse whose own 3D (what the next frame is matched against) lands outside keeps the 3D it had, so a wrong grouping isn't what the next frame is matched to.
- a single body point outside the cage, of a mouse inside it, is dropped.

`campy-live` looks at the file once a second. A cage drawn, changed or removed while it runs is used from then on, and the summary (`campy-live.json`) lists each cage it used and from which frame (`cage`), and how many mouse-frames it set aside (`outside_cage`). A cage drawn for another calibration is refused, and said so: its corners no longer land where they were clicked. `--cage off` keeps every 3D position, and `--cage <file>` uses another file. In a `campy-session` config, pass it in `live.args`, for example `args: ["--cage", "off"]`.

On the six recordings, replayed through the same code with the cage clicked on one of them, every position more than 25 cm from the middle was gone. The positions set aside were 0.04–0.07% with 3 mice and 0.3–1.7% with 5, almost all below the floor or above the top, not past a wall. They were often from 2 cameras (31–66%, against 0–3% of the positions kept), with a median reprojection error of 17–21 px (4–7 px for those kept). Without a cage, the replay gives the logged 3D back exactly. The 3D step took 0.04–0.05 ms more per frame, on its own thread beside inference: that is about 0.5% of the 10 ms from frame to 3D on the Pro Max Slim, and doesn't slow the frame rate. The details, and what it did to identities, are in [3D poses](pose3d.md#the-cage). To see what a cage does to a recording of your own:
```
python bench/pose3d_cage_replay.py <recording folder> --cage <calibration>.cage.json
```

### Opening a tracked recording in LUC3D (campy-live-export)

After the recording, `campy-live-export` writes `campy-live`'s results into the recording folder as files LUC3D opens. Run it in campy-live's environment, which has sleap-io:
```
campy-live-export <recording folder> --log campy-live_<date_time>.npy
```
It writes `campy-live.slp`, which holds each camera's 2D and, with `--calibration`, each animal's identity and 3D pose, grouped across the cameras (LUC3D: File > Load SLP, then pick the recording folder for the videos). It also writes `<camera>/campy-live.slp`, `calibration.toml` and `skeleton.json`, LUC3D's session folder for the 2D (File > Load Session Folder), and `points3d.h5`, the 3D as arrays. Each prediction goes on the video frame that holds it, through each camera's `frametimes.npy`. `--frames FIRST:LAST` exports part of a long recording. `--skeleton <model folder>` gives the skeleton's edges for logs from 2.1.0rc6 and earlier, whose summary lacks them. `--calibration` and `--cameras` cover a calibration that has moved and a log with no summary. See [Live tracking](#opening-a-tracked-recording-in-luc3d) and `python -m pydoc campy.liveexport`. Its provenance keeps the recording's "session.json" and "campy-live-session.json", with the animal IDs. The identities stay "animal 1", "animal 2", ..., because campy-live doesn't know which ID each one is.

### Finding the frames to label next (campy-live-flag)

After the recording, `campy-live-flag` reads `campy-live`'s log and finds the sets the model probably got wrong, then picks a few varied ones to label for the next model. It needs only numpy and scipy, and changes nothing in `campy-live`:
```
campy-live-flag campy-live_<date_time>.npy --budget 25
```
With 3D (`campy-live --calibration`) it flags animals a camera missed while the others saw them, identity swaps and jumps, detections the cameras disagree on, and, with or without 3D, too many or too few animals and low scores. Flags close together make one event; it then picks `--budget` sets (10% at random, the rest from the events, each kind of mistake in turn, each pick the least like the ones before, at least 2 s apart), and writes `flags.csv`, `events.csv` and `sample.json` into `<log>_flags/` with a per-camera count of each kind of mistake. See [Models](models.md#finding-the-frames-a-model-gets-wrong-campy-live-flag) and `python -m pydoc campy.liveflag`.

### Labelling them (campy-label-pack, campy-label-merge)

`campy-label-pack <log>_flags/sample.json --recording <recording folder>` writes the picked sets as one SLEAP package for LUC3D: every camera's frame of each set embedded in the file, with `campy-live`'s detections, identities and 3D on them, ready to correct (File > Load SLP). After correcting and saving in LUC3D, `campy-label-merge <name>.pkg.slp <name>.labels.slp` writes the corrected camera frames as a training package and a test package. Test sets are whole minutes of a recording, kept out of training for good; `--test-into` adds them to a rig's test set, and `--train-into`/`--train-out` add the rest to a copy of a training package. A camera frame with predictions left in it isn't taken. Both need sleap-io (campy-live's environment). See [Models](models.md#labelling-them-in-luc3d-campy-label-pack-campy-label-merge).

### Measuring models, and whether one suits a rig (campy-model-eval, campy-model-fit)

`campy-model-eval <rig's test package> --pair new=<centroid>,<instance> --pair old=<centroid>,<instance>` runs each pair through campy-live's own inference code on the rig's labelled test sets and reports, per pair and camera: missed and extra animals, skeletons on the wrong mouse, the point error (median, p90, p99), and how often the 3D grouped each animal's detections across the cameras, for the random and the flagged test sets apart. `campy-model-fit stats <training package> --out <model folder>` saves the training images' brightness (and the model's id, `model.json`); `campy-model-fit check <model folder> --recording <recording>` warns when a camera's frames are darker, brighter or more saturated than the training images, its animals' brightness differs, or its gamma isn't the training videos'. See [Models](models.md#measuring-a-model-on-a-rigs-test-sets-campy-model-eval).

### Closed-loop outputs (campy-rules)

`campy-rules` reads `campy-live`'s results as they come, checks each set's poses against rules from a YAML file (an animal in a region, two animals' body parts within a distance, one identity or any), and switches TTL outputs on an Arduino Nano ESP32 of their own (the sketch `campy/outputs_board/campy_outputs`). The board watches the camera trigger. It makes trains of pulses locked to the frames itself, such as 3 ms photometry excitation on alternate frames, and says how long after its frame's trigger each output switched. It refuses commands about frames older than `bound_ms`. Everything goes off if `campy-rules` stops or goes quiet. `--dry-run` only logs, and `--replay <campy-live log>` tries the rules on a finished recording:
```
campy-rules rules.yaml --port /dev/ttyACM1
```
See [Closed-loop outputs](closed-loop.md) for the wiring, the rules file, safety and latency.
