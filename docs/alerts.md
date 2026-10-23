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
| A camera's recording ends early | ffmpeg stopped; the camera is gone (unplugged, or its USB link lost); its camera thread or process died; it did not start grabbing | critical, sent once, no reminders | (it has ended) |
| Disk space | less than `alertDiskFreeGB` (20 GB) free on a video folder's disk | warning | more than 10 % above it |
| Disk running out | at the rate it filled over the last 5 minutes, it would be down to `stopAtFreeGB` before `recTimeInSec` ends | critical | it will last |
| Disk nearly full | below `stopAtFreeGB` (2 GB): campy **stops the recording, cleanly**, as at Ctrl-C | critical | (stopped) |
| The trigger's port | the trigger Arduino's serial port is gone (unplugged, or it reset), checked every 5 s without sending it anything | critical, once | the port is back |
| USB 2 | Linux: a USB 3 camera is now on a USB 2 link (read from sysfs every minute, as the launch check does) | critical | back on USB 3 |

A message also says when a recording starts (its cameras, frame rate, length and free space: `info`, so only channels with `min_severity: info` get it) and how it ended (each camera's frames, dropped frames and what went wrong; `warning` with dropped frames, `critical` when a camera did not record to the end). Ctrl-C is not an alert: cameras that end because you stopped them are what you asked for.

### The one thing it does: a clean stop before the disk is full

A disk that fills stops every camera's ffmpeg at the same moment, and leaves no room for the end of each video, `frametimes.npy` or `metadata.csv`. So below `stopAtFreeGB` (2 GB) of free space, campy stops the recording itself, as Ctrl-C does: every camera stops grabbing, its ffmpeg finishes the frames it has, and its files are saved whole. campy then exits with code 1, saying each camera "stopped early: only 1.95 GB free on the disk with ...". `stopAtFreeGB: 0` turns this off.

Apart from that stop, the watch only reads. It never restarts a camera, ffmpeg or the trigger, never deletes anything and never changes a setting mid-recording: a camera unplugged mid-recording needs a replug and a new recording, and a restarted trigger could resume on the wrong device. An error inside one of its checks is said once, and the other checks go on.

### Settings

| Setting | Default | |
|---|---|---|
| `alerts` | `True` | send the watch's alerts to the alerts file's channels (`False`: the console only) |
| `alertNoFramesSec` | `10` | seconds without a frame before a camera is an alert |
| `alertDiskFreeGB` | `20` | free space below which a disk is an alert |
| `stopAtFreeGB` | `2` | free space below which the recording stops, cleanly; `0` = never |

Each can also be given on the command line (`--stopAtFreeGB 5`).

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
