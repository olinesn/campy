# Alerts: a message on your phone when a recording goes wrong

A long recording runs overnight with nobody watching. When a camera stops, the encoder falls behind or the disk fills, campy says so on its console, and nobody reads that until the morning. campy's alerts send those problems to you as a Slack direct message within a minute or two, and say when each one is over. A heartbeat on [healthchecks.io](https://healthchecks.io) covers the one thing no program on the rig can report: the computer itself dying (a power cut, a Windows Update restart, campy killed).

This page sets up both, and lists what `campy-acquire` watches as it records. `campy-notify --test` checks the setup.

## 1. A Slack app that messages you

The alerts come from a small Slack app of your own, as direct messages from it to you, so they reach your phone like any other DM. You make it once, for all the rigs:

1. Go to [api.slack.com/apps](https://api.slack.com/apps), **Create New App**, **From scratch**. Name it (say "campy") and pick your workspace.
2. **OAuth & Permissions**, **Bot Token Scopes**: add `chat:write`.
3. **Install to Workspace** (some workspaces need an admin to approve it). Copy the **Bot User OAuth Token**, which starts with `xoxb-`.
4. **App Home**, **Show Tabs**: turn on the **Messages Tab**, where its messages to you show.
5. Your member ID: in Slack, open your profile, **⋮**, **Copy member ID** (it starts with `U`).

To post to a channel instead (one the whole lab watches), invite the app to the channel and give its channel ID as `channel:` instead of `user:`. An [incoming webhook](https://api.slack.com/messaging/webhooks) URL works too (`webhook:`), but a webhook can't edit its messages, so a resolved problem's first message isn't marked as resolved.

## 2. A healthchecks.io check (optional)

1. Make an account on [healthchecks.io](https://healthchecks.io) (the free plan is enough) and **Add Check**, one per rig.
2. Set its **Period** to 1 minute and its **Grace Time** to 5 minutes: campy pings it every minute while it records, and the check alerts 5 minutes after the pings stop.
3. **Integrations**: add Slack (or email), so its alerts reach you too.
4. **Settings**, **API Access**: create an API key (not the read-only one). campy uses it to pause the check when a recording ends cleanly, so the hours between recordings aren't an alert. A paused check starts again at its next ping, the next recording's first.
5. Copy the check's **ping URL** (`https://hc-ping.com/<uuid>`).

## 3. The alerts file on each rig

The tokens go in a file of their own on each computer, never in a campy config: `metadata.csv` copies every config parameter into each recording, and configs get committed. Without the file, campy sends nothing.

The file is `~/.campy/alerts.yaml`, which is `%USERPROFILE%\.campy\alerts.yaml` on Windows. The `CAMPY_ALERTS_FILE` environment variable can name another one.

```yaml
rig: Dell Pro Max Slim FCS1250      # which computer each message says it is from
slack:
  token: xoxb-...                   # the app's Bot User OAuth Token
  user: U0123ABCDEF                 # your member ID: the app messages you directly
  min_severity: warning             # info (start/end summaries too), warning (default) or critical
healthchecks:
  ping_url: https://hc-ping.com/<uuid>
  api_key: ...                      # to pause the check between recordings
```

`slack:` can be a list, for several people or channels, each with its own `min_severity` (for example: you get critical only, a lab channel gets everything). For a ping URL in the slug form (`https://hc-ping.com/<ping key>/<slug>`), add `check: <uuid>` so campy can pause it.

On Linux and macOS, make the file readable by you only, and campy warns if it isn't:

```bash
chmod 600 ~/.campy/alerts.yaml
```

Anyone with the bot token can post as your app. If it leaks, open the app's **OAuth & Permissions** page and revoke it.

Then check it. This sends a test message to each Slack entry and pings and then pauses the check. It says what worked, and exits 1 if anything failed:

```bash
campy-notify --test
```

## What campy-acquire watches

Every recording is watched, however it was started: a thread in `campy-acquire`'s main process reads each camera's counters (frames grabbed, waiting for ffmpeg, given to ffmpeg, dropped) every 2 seconds. That process otherwise only waits for the camera processes, so the cameras never wait for it. Each alert is also said on the console (`Alert: ...`, `Alert resolved: ...`) and, when the alerts file exists, written to `alerts.log` in the recording's folder. Without the file, the console lines and the disk stop below still happen.

| What | When it is an alert | How bad | Over when |
|---|---|---|---|
| A camera sends no frames | it has sent frames, then none for `alertNoFramesSec` (10 s) | critical | frames again |
| Every camera at once | all stopped together (their last frames within 1 s): one message, "the trigger has stopped", not one per camera | critical | frames again |
| No first frame | none 10 s after the trigger Arduino started; with an external trigger (`startArduino: False`), after 10 minutes of waiting | critical; warning for an external trigger | the first frames |
| Dropped frames | frames dropped because more than `writeQueueMaxSec` of frames were waiting for ffmpeg | warning; critical above 1 % of the last minute's frames | none dropped for 2 minutes |
| Writing stalls | the frames waiting for ffmpeg reach a quarter of `writeQueueMaxSec` (critical at three quarters): the disk or the encoder has stopped keeping up, and frames are dropped once it is full. The EVO-X2's SSD stopped taking writes for 10 to 16 s at a time | warning, then critical | below a tenth, for 30 s |
| A camera's recording ends early | ffmpeg stopped; the camera is gone (unplugged, or its USB link lost: a FLIR camera says so within a second); its camera thread or process died; it did not start grabbing | critical, sent once, no reminders | (it has ended) |
| Disk space | less than `alertDiskFreeGB` (20 GB) free on a video folder's disk | warning | more than 10 % above it |
| Disk running out | at the rate it filled over the last 5 minutes, it would be down to `stopAtFreeGB` before `recTimeInSec` ends | critical | it will last |
| Disk nearly full | below `stopAtFreeGB` (2 GB): campy **stops the recording, cleanly**, as at Ctrl-C | critical | (stopped) |
| The trigger's port | the trigger Arduino's serial port is gone (unplugged, or it reset), checked every 5 s without sending it anything | critical, once | the port is back. campy lets go of the lost port, so a replug brings it back under the same name, but it does not restart the trigger: the cameras get frames again in the next recording |
| USB 2 | Linux: a USB 3 camera is now on a USB 2 link (read from sysfs every minute, as the launch check does) | critical | back on USB 3 |
| GPU firmware stalls | Linux with an AMD GPU: `alertGpuStalls` (3) or more `Fence fallback timer expired on ring ...` in the kernel log within 10 minutes ([below](#the-gpus-kernel-log-amd-on-linux)) | warning | none for 10 minutes |
| A GPU job hung | a ring timeout, the GPU's firmware scheduler not answering, a queue that would not stop, a GPU page fault | warning | none for 10 minutes |
| GPU reset | the driver reset a ring of the GPU, or the whole GPU | critical | none for 10 minutes |

A message also says when a recording starts (its cameras, frame rate, length and free space: `info`, so only channels with `min_severity: info` get it) and how it ended (each camera's frames, dropped frames, what went wrong, and the problems still open as it ended; `warning` with dropped frames, `critical` when a camera did not record to the end or a critical problem was still open). Ctrl-C is not an alert: cameras that end because you stopped them are what you asked for. But a recording stopped while a camera had no frames, or with the trigger unplugged, did not record what it should have, and its summary says so.

### The one thing it does: a clean stop before the disk is full

A disk that fills stops every camera's ffmpeg at the same moment, and leaves no room for the end of each video, `frametimes.npy` or `metadata.csv`. So below `stopAtFreeGB` (2 GB) of free space, campy stops the recording itself, as Ctrl-C does: every camera stops grabbing, its ffmpeg finishes the frames it has, and its files are saved whole. campy then exits with code 1, saying each camera "stopped early: only 1.95 GB free on the disk with ...". `stopAtFreeGB: 0` turns this off.

Apart from that stop, the watch only reads. It never restarts a camera, ffmpeg or the trigger, never deletes anything and never changes a setting mid-recording: a camera unplugged mid-recording needs a replug and a new recording, and a restarted trigger could resume on the wrong device. An error inside one of its checks is said once, and the other checks go on.

### The GPU's kernel log (AMD, on Linux)

On a computer with an AMD Ryzen AI Max (Strix Halo), such as the GMKtec EVO-X2, the GPU's engines can miss an interrupt. The driver then waits for a fallback timer, and the kernel log says `amdgpu: Fence fallback timer expired on ring comp_1.0.0`. Whatever ran on that engine stalled for about half a second: a camera's VA-API encoder, or `campy-live`, which uses the same GPU. Before `vaapiLinearSurfaces` ([Encoding](encoding.md#helpful-tips)) this happened 20 to 70 times an hour with 8 cameras. A job that hangs for longer times out (`ring ... timeout`), and the driver then resets that ring or the whole GPU, which ends what was running on it. Otherwise the watch only sees these as frames waiting for ffmpeg ("Writing stalls" above).

So on Linux with an AMD GPU, `campy-acquire` reads the kernel log while it records and sends what the GPU's driver logs:

- **Firmware stalls**: a warning at `alertGpuStalls` (3) within 10 minutes. A single stall now and then is no alert, but the old rate is. The message gives how many came in the last minute and the last 10 minutes, and each ring with its count, for example `comp_1.0.0 (compute: campy-live, and Mesa's copies into tiled video surfaces) ×4, vcn_unified_0 (video encoder: the cameras' VA-API encoders) ×1`, and the last line itself.
- **A hung GPU job** (`ring ... timeout`, `MES failed to respond`, `Queue preemption failed`, a GPU page fault): a warning at the first.
- **A GPU reset** (`Ring ... reset succeeded` or `failed`, `GPU reset(...) succeeded!`, `GPU Recovery Failed`, `VRAM is lost due to GPU reset`): critical. A reset ends the work on the GPU, so expect the cameras' VA-API encoders or `campy-live` to fail; the camera alerts say which.

Each is over once 10 minutes pass without another. The summary at the end has a line for them when there were any, such as "GPU kernel log: 5 firmware stalls, no GPU reset (comp_1.0.0 ×5)". The kernel log is the whole computer's, so stalls in `campy-live`'s work show up here too.

Some of the driver's messages are not problems and are left out. The main one is `Freeing queue vital buffer ..., queue evicted`, which it logs whenever a GPU program (`campy-live`, a benchmark) exits. On the EVO-X2, 10 days of recordings, live tracking and benchmarks (16 to 26 October 2026, kernel 6.17) logged 54 of those, and no stall, hang or reset at all: linear surfaces took the stalls away.

**How it reads the log.** It runs `journalctl -k -f -n 0 -o cat`, which prints only the kernel's new messages, as a process tied to `campy-acquire`, so it ends when campy does. A thread of its own in campy-acquire's main process reads it. It waits on journalctl without using the CPU, since the kernel logs a few lines an hour, and the cameras never wait for it. Where journalctl isn't there, it reads `/dev/kmsg`.

**Permission.** journalctl shows the kernel log to users in the `adm` or `systemd-journal` group (on Ubuntu, the first user is in `adm`). `/dev/kmsg` is root's only on Ubuntu (`kernel.dmesg_restrict` is 1). If it can read neither, campy says so once, on the console and in `alerts.log`, and records without it. `recording.json`'s `gpu_log` says why too. To let it read the log, type this in a terminal and then log out and back in:

```bash
sudo usermod -aG adm $USER
```

Check it with `journalctl -k -n 5`, which should show the kernel's last lines without sudo. `alertGpuLog: False` turns the reading off. On Windows, macOS, and Linux without an AMD GPU, it does nothing.

### The recording's state now: `recording.json`

Every 4 seconds, and as it ends, the watch also writes the recording's state to `~/.campy/status/recording.json` (`%USERPROFILE%\.campy\status\recording.json` on Windows), for a control panel to show: the state (`recording`, `stopping`, `ended`, `failed`), the session folder, its start, how long it has run and is planned to; for each camera its frame rate (set and measured), frames, dropped frames, seconds of frames waiting for ffmpeg, USB link speed (Linux) and state; each disk's free space and when it would be full at the present rate; the problems open now; the last critical one; and, on Linux with an AMD GPU, `gpu_log`: how the kernel log is read (`journalctl`, `/dev/kmsg`, or why it isn't), and the firmware stalls, hung jobs and resets so far, with each ring's count. Only campy-acquire writes it, and whole each time. `python -m pydoc campy.watch` lists the fields.

### Beside campy-live: `campy-watch`

`campy-acquire` can't see what only campy-live-view shows: whether the cameras' frames of the same number came at the same moment, frames a camera lost before campy got them, and how the tracking is doing. `campy-watch` reads what campy-live-view reads (the live feed, `liveFeed: True`, and campy-live's results), read-only, with campy-live-view's own checks, and sends them once they last. Start it beside `campy-live`, in either environment:

```bash
campy-watch --cameras 3
```

| What | When it is an alert | How bad |
|---|---|---|
| A camera a frame or more ahead or behind | for 10 s: every set campy-live makes mixes frames of different trigger pulses, so stop the recording and start it again | critical |
| Frames lost, missing from the feed, or late | in every check for 30 s (counted over the last 10 s, as campy-live-view does) | warning |
| A camera slower than the others | for 30 s | warning |
| Tracking | good 3D for fewer animals than campy-live's `--animals`, for 70 % of the last 3 minutes | warning |
| campy-live stopped | no new result for 30 s while every camera records (it has stopped or hangs); or it ended while they record | critical (warning) |

`campy-session` starts it for you beside `campy-live` (`live.watch`, on by default), and restarts a `campy-live` that `campy-watch` reports as making no results ([Live tracking](live-tracking.md#in-one-command-campy-session)). With `--stop-file` and `--launcher-pid`, as `campy-session` starts it, it also stops at its stop file, and stops waiting if `campy-session` ended before the recording began. A camera with no frames is left to `campy-acquire`, which says it already. That camera also stops campy-live's results, as campy-live waits for a frame from each camera, so campy-live is not called hung then, and `campy-session` does not restart it for it. `campy-watch` waits for the feed, and ends when the recording does. `--name` is campy's `liveFeedName` (default `campy-live`), `--bad-px` the 3D error that counts as bad, as in campy-live-view. What it finds goes into `alerts-live.log` in the recording's folder, in `alerts.log`'s format (campy-acquire writes `alerts.log`; each file has one writer), whether or not the alerts file names a channel.

### Settings

| Setting | Default | |
|---|---|---|
| `alerts` | `True` | send the watch's alerts to the alerts file's channels (`False`: the console only) |
| `alertNoFramesSec` | `10` | seconds without a frame before a camera is an alert |
| `alertDiskFreeGB` | `20` | free space below which a disk is an alert |
| `stopAtFreeGB` | `2` | free space below which the recording stops, cleanly; `0` = never |
| `alertGpuLog` | `True` | Linux with an AMD GPU: read the kernel log for the GPU's firmware stalls, hung jobs and resets |
| `alertGpuStalls` | `3` | firmware stalls within 10 minutes that are an alert |

Each can also be given on the command line (`--stopAtFreeGB 5`).

## Tested on a rig, with cables pulled by hand

On 23 October 2026 the alerts were checked on the GMKtec EVO-X2 (Ubuntu 24.04): two FLIR Blackfly S (BFS-U3-13Y3M) on USB 3 at 60 fps, triggered by its Arduino Nano ESP32, recording with the rig's own config, the live feed on, `campy-live` and `campy-watch` beside it, and the messages going to a Slack direct message on a phone. Each 5-minute recording had a camera's USB cable pulled, then the trigger's. The times are from the kernel's log (the cable) and `alerts.log` (the message sent). Every message also showed on the phone within the same minute (Slack shows minutes).

The first recording, with campy 2.2.0rc1, found four things that did not work as this page said. They are fixed, and the second recording, with the fixes, shows each one working:

| What was done | 2.2.0rc1 | With the fixes |
|---|---|---|
| A camera's cable pulled | "Camera1: no frame for 10 s", 10 s later: Spinnaker's error ("Camera has been removed from the list and is no longer valid", -1024) did not count as the camera being gone | "Camera1: the camera is gone ... after frame 2901", about 1 s later. Its video was closed, whole, at once |
| The trigger's cable pulled | "The trigger Arduino on /dev/ttyACM0 is gone", about 1 s later | the same, 2 s later (the port is checked every 5 s) |
| The camera still plugged in, without trigger pulses | "Camera0: no frame for 10 s", 10 s after its last frame | the same |
| The trigger plugged back in | no message: campy still held the lost port open, so Linux gave the Arduino the next name, /dev/ttyACM1. The next recording would not have found it on /dev/ttyACM0 either, until it was replugged once more | back as /dev/ttyACM0, and "resolved: The trigger Arduino's port /dev/ttyACM0 is back", 2 s later |
| `campy-watch` | "campy-live has made no result for 31 s, while the cameras record: it has stopped or hangs", then "resolved" a second later. campy-live was fine: it was waiting for the unplugged camera. `campy-session` would have restarted it for that | no message |
| The end | no message: the summary was sent as `info`, because the recording was stopped (its stop file), as at Ctrl-C | ":red_circle: Recording ... ended after 5 min 35 s, 1 of 2 cameras not to the end", with each camera's frames and "Still open as it ended: Camera0: no frame for 231 s" |

In both recordings, every frame each camera grabbed was in its video, which decoded without an error (7733 and 6037 frames; 5791 and 2901), with `frametimes.npy`, `hosttimes.npy` and `metadata.csv` beside it. `alerts.log` had one JSON line for each problem and one for each message sent, and no token. `recording.json` ended as `failed` (the fixes; `ended` before them), with each camera's state (`lost`, `ended`) and the problems still open.

### On Windows

On 26 October 2026 the same test ran on the Dell Pro Max Slim FCS1250 (Windows 11), with the fixes above (campy 2.2.0rc1 at dev 27830d2): three FLIR Blackfly S (BFS-U3-13Y3M) on USB 3 at 30 fps, triggered on Line3 by its Arduino Nano ESP32 on COM9, recording with the rig's own profile (Quick Sync H.265), the live feed on, `campy-live` and `campy-watch` beside it, and the messages going to the same Slack direct message. One 5-minute recording had a camera's USB cable pulled, then the trigger's, and the trigger plugged back in. The cable times are from Windows' device records (each device's last arrival and removal) and from the cameras' last frames; the message times are from `alerts.log`, which keeps whole seconds.

Everything worked as on Linux. The trigger's messages came a little later than there only because of where the 5-second port check happened to fall:

| What was done | What was sent |
|---|---|
| A camera's cable pulled | "Camera2: the camera is gone ... after frame 2253", about 2 s later. Spinnaker on Windows gives the same error as on Linux (-1024, "Camera has been removed from the list and is no longer valid"). Its video was closed, whole, at once; the other two cameras went on |
| The trigger's cable pulled | "The trigger Arduino on COM9 is gone", about 4 s later (the port is checked every 5 s). The reason Windows gives is "ClearCommError failed ... The device does not recognize the command" |
| The two cameras still plugged in, without trigger pulses | "No camera has had a frame for 10 s: all 2 stopped together, so the trigger has stopped", 10 s after their last frame |
| The trigger plugged back in | "resolved, after 1 min 12 s: The trigger Arduino's port COM9 is back", about 4 s later. Windows keeps a device's COM number, so it came back as COM9 |
| `campy-watch` | no message, although `campy-live` stopped making results once the unplugged camera's frames stopped (it waits for every camera's frame of a set) |
| The end | ":red_circle: Recording ... ended after 5 min 36 s, 1 of 3 cameras not to the end", with each camera's frames and "Still open as it ended: No camera has had a frame for 203 s ... the trigger has stopped" |

Every frame each camera grabbed was in its video, which decoded without an error (3771, 3771 and 2253 frames), with `frametimes.npy`, `hosttimes.npy` and `metadata.csv` beside it and one host time per frame. `alerts.log` had one JSON line for each problem and one for each message sent, Slack accepted every message, and the file holds no token. `recording.json` ended as `failed`, with each camera's state (`ended`, `ended`, `lost`) and the two problems still open. Its `usb_speed` is empty on Windows: the USB link check is Linux only. The recording's start message, `info`, went only to the log, as the alerts file asks for `warning` and up.

What this means for an unattended recording:

- **A recording with no trigger pulses does not end at `recTimeInSec`.** campy counts frames, and none come. It goes on until it is stopped: Ctrl-C, its stop file, or `campy-session`'s stop. The alerts say so within seconds.
- **Replugging a camera or the trigger does not resume the recording.** Stop it, and start a new one.

## How messages behave

- A problem is sent once, as it starts, marked :red_circle: (critical) or :large_orange_circle: (warning). While it lasts, nothing more is sent, apart from a reminder every 30 minutes. When several problems are due a reminder at once, they share one message.
- When it is over, one :white_check_mark: message says so and how long it lasted. The first message is also edited (struck through, "resolved after ...") so the conversation reads at a glance.
- A problem that becomes worse (warning to critical) is sent again.
- A problem that keeps coming and going (3 times in 10 minutes) gets one message saying so, and is called resolved only once it has stayed away for 10 minutes.
- At most 20 messages an hour per rig. Past that, the rest go into one digest every 15 minutes.
- Messages go out from a thread of their own, each request with a 10 s timeout and up to 3 tries. A slow or dead network never holds anything up. A message that can't be sent is said on the console and in the log.
- A recording's `alerts.log` has one JSON object a line, for programs to read: `time`, `key` (such as `Camera1/no-frames`), `severity`, `event` (`problem`, `worse`, `flapping`, `still`, `resolved`, `note`), `state` (`open` or `resolved`), `status` (`queued`, `sent to ...`, `FAILED on ...: why`) and `text`.

## From scripts: `campy-notify`

A job that runs on its own, such as a nightly rig check, can send through the same file:

```bash
campy-notify --note "Nightly rig check passed"
campy-notify --problem rigcheck/drift "Rig check: encoder 3 % slower than last week" --severity warning
campy-notify --resolved rigcheck/drift "Rig check: back to normal"
```

`--problem` and `--resolved` keep what is open in `~/.campy/alerts-state.json` (`--state`), so a problem reported every night is sent once, reminded, and resolved the night it passes. `--source` replaces the file's `rig:` name.

From Python, `campy.notify.Notifier` does the same. Its problems are kept in memory unless `state=` names a file. `python -m pydoc campy.notify` describes it.
