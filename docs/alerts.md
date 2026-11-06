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

Every recording is watched, however it was started: a thread in `campy-acquire`'s main process reads each camera's counters (frames grabbed, waiting for ffmpeg, given to ffmpeg, dropped) every 2 seconds. That process otherwise only waits for the camera processes, so the cameras never wait for it. Each alert is also said on the console (`Alert: ...`, `Alert resolved: ...`) and written to `alerts.log` in the recording's folder, with or without the alerts file. Without the file, nothing is sent, but the console lines, `alerts.log` and the disk stop below still happen.

| What | When it is an alert | How bad | Over when |
|---|---|---|---|
| A camera sends no frames | it has sent frames, then none for `alertNoFramesSec` (10 s) | critical | frames again |
| Every camera at once | all stopped together (their last frames within 1 s): one message, "the trigger has stopped", not one per camera | critical | frames again |
| The trigger paused | every triggered camera got no frame for the same trigger pulses, by its own timestamps (each frame's camera clock: a step of *n* frame periods is *n* − 1 pulses without a frame), their next frames within 1 s of each other: one message, with the pulses, how long, and the frame just after. No frame is lost, but from there on the frames are later than their numbers say. A pause of 10 s or more is also "the trigger has stopped" above. A trigger that pauses on purpose: see [planned pauses](#a-trigger-that-pauses-on-purpose-alerttriggerpausesec) | warning | no pause for 2 minutes |
| A camera misses trigger pulses | a triggered camera got no frame for pulses the other cameras got: counted from the start, its frames are then out of step with theirs; `campy-align` puts them right. With one triggered camera, a pause and a miss look the same, and the message says so | warning | none missed for 2 minutes |
| No first frame | none 10 s after the trigger Arduino started; with an external trigger (`startArduino: False`), after `alertExternalWaitSec` (10 minutes) of waiting, or never with `None` | critical; warning for an external trigger | the first frames |
| Dropped frames | frames dropped because more than `writeQueueMaxSec` of frames were waiting for ffmpeg | warning; critical above 1 % of the last minute's frames | none dropped for 2 minutes |
| Writing stalls | the frames waiting for ffmpeg reach a quarter of `writeQueueMaxSec` (critical at three quarters): the disk or the encoder has stopped keeping up, and frames are dropped once it is full. The EVO-X2's SSD stopped taking writes for 10 to 16 s at a time. A camera's first 10 s of frames, and a restarted ffmpeg's, get no warning, only critical: ffmpeg opens its encoder when its first frame comes, and frames pile up meanwhile ([below](#frames-waiting-as-the-encoders-open)) | warning, then critical | below a tenth, for 30 s |
| A camera's recording ends early | ffmpeg stopped (and was not restarted); the camera is gone (unplugged, or its USB link lost: a FLIR camera says so within a second); its camera thread or process died; it did not start grabbing | critical, sent once, no reminders | (it has ended) |
| ffmpeg restarted | with `restartFfmpeg: True`, a camera's ffmpeg stopped and a new one took over: said once, then again with the frames lost once the stopped ffmpeg's file is counted ([below](#restarting-a-stopped-ffmpeg-restartffmpeg)) | warning, sent once each, no reminders | (nothing stays open) |
| Disk space | less than `alertDiskFreeGB` (20 GB) free on a video folder's disk | warning | more than 10 % above it |
| Disk running out | at the bytes per frame it filled by over the last 5 minutes, the frames still to record (`recTimeInSec` × `frameRate` per camera, less those grabbed) would take it down to `stopAtFreeGB`. Counted in frames, not wall-clock time, so a trigger that runs the cameras only part of the time is counted right. With planned pauses (`alertTriggerPauseSec`, bouts), the bytes per frame are counted over the whole recording instead of the last 5 minutes: other programs fill the same disk around the frames (SpikeGLX while the gate is open), and over 5 minutes that share swings with the kind of bout (on the ChronicRecord rig, 150 to 346 MB/s against about 60 on average, so false alerts all night) | critical | it will last |
| Disk nearly full | below `stopAtFreeGB` (2 GB): campy **stops the recording, cleanly**, as at Ctrl-C | critical | (stopped) |
| The trigger's port | the trigger Arduino's serial port is gone (unplugged, or it reset), checked every 5 s without sending it anything | critical, once | the port is back. campy lets go of the lost port, so a replug brings it back under the same name, but it does not restart the trigger: the cameras get frames again in the next recording |
| USB 2 | a USB 3 camera is now on a USB 2 link, checked every minute as the launch check does: on Linux in sysfs, on Windows by asking the camera's USB hub ([below](#a-camera-on-a-usb-2-link)) | critical | back on USB 3 |
| GPU firmware stalls | Linux with an AMD GPU: `alertGpuStalls` (3) or more `Fence fallback timer expired on ring ...` in the kernel log within 10 minutes ([below](#the-gpus-kernel-log-amd-on-linux)) | warning | none for 10 minutes |
| A GPU job hung | a ring timeout, the GPU's firmware scheduler not answering, a queue that would not stop, a GPU page fault | warning | none for 10 minutes |
| GPU reset | the driver reset a ring of the GPU, or the whole GPU | critical | none for 10 minutes |

A message also says when a recording starts (its cameras, frame rate, length and free space: `info`, so only channels with `min_severity: info` get it) and how it ended (each camera's frames, dropped frames, ffmpeg restarts and the frames they lost, trigger pulses it alone missed, the trigger's pauses, what went wrong, and the problems still open as it ended; `warning` with dropped frames, an ffmpeg restart, a trigger pause or missed pulses, `critical` when a camera did not record to the end or a critical problem was still open). Ctrl-C is not an alert: cameras that end because you stopped them are what you asked for. But a recording stopped while a camera had no frames, or with the trigger unplugged, did not record what it should have, and its summary says so.

### A trigger started well after campy (`alertExternalWaitSec`)

With an external trigger (`startArduino: False`), campy starts its cameras and waits for pulses from a device it doesn't control. If no camera has had a frame 10 minutes after campy started waiting, the watch sends a warning: `No frame yet, 10 min after campy started waiting for the external trigger: is it running?`. It is resolved, with "The first frames came", once they do. Some sessions start campy well ahead of the trigger on purpose, so that every camera is grabbing before the first pulse. In a 1-hour rehearsal of the ChronicRecord rig (a Raspberry Pi Pico makes the pulses), the Pico's session started 16 minutes after campy, and this warning was the only false alarm sent. Set `alertExternalWaitSec` above the longest wait you plan, or to `None` to never send it. With `None`, a trigger that is never started is not reported: campy waits until it is stopped. Once the frames have come, a trigger that stops is still caught by `alertNoFramesSec`. The trigger Arduino (`startArduino: True`) is not affected: no frame 10 s after it started is critical. Default: `600` (10 minutes).

### A trigger that pauses on purpose (`alertTriggerPauseSec`)

Some sessions run the cameras in bouts: the ChronicRecord rig's Pico pulses them for 10 s every 5 minutes, for days. To campy each gap between bouts is a trigger pause, so without this setting every bout after a gap is a "trigger paused" warning, from the third one on it is "coming and going", and Slack gets a reminder every 30 minutes for the whole session.

With `alertTriggerPauseSec` set, a pause at least that many seconds long is planned. It is said on the console (`Trigger: The trigger paused as planned: ...`) and written to `alerts.log` (event `planned pause`, status `logged only`), but not sent. `recording.json` lists it in `trigger_pauses` with `planned: true`, and the message as the recording ends counts them in a line of its own ("The trigger paused as planned 1728 times ...") without making it a warning. With one triggered camera, a gap that long is taken as the trigger's planned pause too.

On the console, each camera's own lines stay quiet through a gap every camera shares (their last frames within 1 s of each other): no "CameraN: no frame for 3 s, since frame 14400 ..." when the gap starts, every 30 s while it lasts, and "frames again" when it ends, which at 4 cameras and 1,727 gaps in 6 days came to some 76,000 lines. The watch's one `Trigger:` line says each gap instead. A camera that stops while the others still get frames, or is still without frames once they have them again, is said on its own as before. The running `CameraN collected N frames at X fps` line leaves the gaps out of its rate and says them apart (`... at 60.0 fps for 250 sec of frames (1810 sec in all, with 6 trigger pauses of 1560 sec)`), and so does the line as each camera ends (`saved N frames at 60 fps over ... s of frames, with ... trigger pauses of ... s in all`); before, they averaged over the gaps, as if frames were missing. Without `alertTriggerPauseSec`, or for a camera without a trigger, these lines are as before. On a console, the running line is replaced by campy's one status line, which says `trigger paused 0:45` during a gap and counts the gaps after it ([The status line](acquire.md#the-status-line-statusline)); the `collected` lines are still printed when the output goes to a log. On every recording, a FLIR camera's grab that timed out (Spinnaker error -1011, `Failed waiting for EventData on NEW_BUFFER_DATA event`) is said as `timed out waiting for a frame (no trigger pulse?) [Spinnaker -1011]`.

Shorter pauses are still a warning, and a camera that misses pulses the others got is still its own alert. The "frames later than their numbers say" in a pause's warning counts the planned pauses before it too. Set it below the shortest planned gap and above any pause that would be a fault, and set `alertNoFramesSec` above the longest planned gap, or each gap is also "the trigger has stopped". A trigger that stops for good is then caught by `alertNoFramesSec`. Default: `None`, every pause is a warning.

### A camera on a USB 2 link

A USB 3 camera on a USB 2 link gets less than a sixth of the bandwidth it needs, so it loses frames. Its link can't slow down while it stays connected, though: when a cable or port fails, the camera leaves and comes back on the port's USB 2 half. campy has lost it by then, so this alert comes right after "the camera is gone", and tells you why it went and what to replace: the cable, a hub, or the port. It also catches a camera that was on USB 2 from the start, when `preflight` is `"warn"` or `"off"`. Otherwise the launch check stops such a recording before it starts.

On Linux the watch reads each link's speed in sysfs, as `lsusb -t` does. On Windows it asks each camera's USB hub what speed its port runs at, as Microsoft's USBView does: it opens nothing but the hub, needs no administrator rights, and reads every USB device in about 1 ms (the Dell Pro Max Slim FCS1250, 12 devices). Both run in `campy-acquire`'s main process, never in a camera's. Windows names a FLIR camera by its serial number in hex (`USB\VID_1E10&PID_4000\01524401` is camera 22168577), which campy matches. On macOS the link isn't checked.

Checked on 26 October 2026 on the Dell Pro Max Slim FCS1250 (Windows 11), with its four FLIR Blackfly S (BFS-U3-13Y3M) at 30 fps on Line3, the rig's own profile and Quick Sync H.265. Its four cameras, the trigger Arduino and the keyboard all hang off the chipset's USB controller (`8086:7f6e`). Windows read the cameras at 5 Gb/s (SuperSpeed), the Arduino Nano ESP32 at 12 Mb/s (full speed) and the keyboard at 1.5 Mb/s (low speed), as they are. A whole reading took 1.1 ms (median of 100; at most 2.9 ms) with the cameras idle, and the same from a normal PowerShell window, not run as administrator (2.6 ms). During a 3-minute recording, the watch was set to read every 5 s instead of every minute, for more readings: 31 readings, median 2.5 ms, at most 13 ms, each one 5 Gb/s for all four cameras, and no alert. Every camera grabbed 5400 frames of 5400, none dropped, and each video decoded whole, with 5400 frame times. `recording.json` ended as `ended`, with `usb_speed` 5000 for each camera. The launch check said `OK usb controller 8086:7f6e, its USB 3 links: Camera0, Camera1, Camera2, Camera3 send 157 of 300 MB/s (52%)`. A camera moved to a USB 2 link was not part of this check: the alert's message is the one Linux sends, and the tests change a made-up camera's link.

### Frames waiting as the encoders open

ffmpeg opens its encoder only when its first frame comes, and takes no more frames until the encoder is open. Meanwhile frames wait in campy's queue. On the Dell Pro Max Slim FCS1250 (Intel Quick Sync H.265), four ffmpegs opening together at 60 fps, 1280x1024, each had 0.82-0.88 s of frames waiting, then caught up within 3 s (measured without cameras, as campy opens them, twice). A real 4-camera recording there, after 30 s of waiting for an external trigger, had 1.8 s of frames waiting at the start, and then wrote every frame, with none dropped. So for a camera's first 10 s of frames, and a restarted ffmpeg's, the watch doesn't warn about frames waiting. It still sends critical (frames about to be dropped), and it still reports dropped frames. A stall that's still there after those 10 s is reported then.

A trigger that pauses doesn't do this: after a 290 s pause without frames, the same four ffmpegs took the next frames at once, with no backlog. Bouts within one recording start without a backlog. Only a new recording, or a restarted ffmpeg, does.

### The one thing it does: a clean stop before the disk is full

A disk that fills stops every camera's ffmpeg at the same moment, and leaves no room for the end of each video, `frametimes.npy` or `metadata.csv`. So below `stopAtFreeGB` (2 GB) of free space, campy stops the recording itself, as Ctrl-C does: every camera stops grabbing, its ffmpeg finishes the frames it has, and its files are saved whole. campy then exits with code 1, saying each camera "stopped early: only 1.95 GB free on the disk with ...". `stopAtFreeGB: 0` turns this off.

Apart from that stop, the watch only reads. It never restarts a camera or the trigger, never deletes anything and never changes a setting mid-recording: a camera unplugged mid-recording needs a replug and a new recording, and a restarted trigger could resume on the wrong device. A stopped ffmpeg is restarted only when you turn that on (`restartFfmpeg`, below); the camera's own process does it, and the watch says so. An error inside one of its checks is said once, and the other checks go on.

### Restarting a stopped ffmpeg (`restartFfmpeg`)

Off by default. Each camera's frames go to an ffmpeg of its own, which encodes them and writes the video. When that ffmpeg stops mid-recording (killed, crashed, or an encoder error), campy normally stops that camera: its video ends there, and the other cameras go on. With `restartFfmpeg: True`, campy starts a new ffmpeg instead, and the camera never stops:

1. The camera keeps grabbing. Its frames wait in memory, as they do whenever the encoder is slow (up to `writeQueueMaxSec`, past which they are dropped and counted). A new ffmpeg takes a fraction of a second to start.
2. campy waits for the stopped ffmpeg to exit, so that its file is closed. That normally takes a fraction of a second. An ffmpeg that has stopped taking frames but hasn't exited 3 s later (stuck on its way out, as in a GPU driver call) is killed, and campy says "Camera1: ffmpeg stopped taking frames and did not exit within 3 s, stuck on its way out: killed it (pid ...)". Its file is already closed by then, so the kill costs the video nothing ([below](#an-ffmpeg-that-does-not-exit)). This step is the same without `restartFfmpeg`, where the camera then stops.
3. The new ffmpeg writes a new file next to the first, with the same settings: `0.mp4`, then `0.part2.mp4`, `0.part3.mp4`. Frame numbers go on from one file to the next.
4. The frames that were inside the stopped ffmpeg, given to it but not yet in its file, are lost: about a second (its last fragment, and the frames inside the encoder). In the background, campy reads which frames the old file holds, from its packets' timestamps (without decoding it, at low priority), and names the lost ones.
5. `segments.csv`, in the camera's folder, says which frames each file holds, and which were lost ([Recording folders](recordings.md#what-is-in-the-folder)). "frametimes.npy" and "hosttimes.npy" have no row for a lost frame, as for a dropped one: their rows are the files' frames, read one after the other.
6. After the recording, `campy-session post`'s `join` step joins the files into one, `0.joined.mp4`, without re-encoding, for the tools that read one video per camera ([Live tracking](live-tracking.md#in-one-command-campy-session)). For a recording made with `campy-acquire` alone: `campy-session post <recording folder> --only join`.

Two messages say it: as it happens, "Camera1: ffmpeg stopped, and campy started a new one (restartFfmpeg; restart 1 of the recording). The camera never stopped: its video goes on in a new file, 0.part2.mp4. ...", and seconds later, once the old file is counted, "Camera1: the stopped ffmpeg's file is counted: 27 frames given to it are not in it, and lost." Both are warnings. The end's summary says "Camera1: 14400 frames, in 2 video files: 1 ffmpeg restart, 27 frames lost". The recording counts as whole: campy exits with 0, and `campy-session`'s post steps run.

It does not restart, and the camera stops as without it (a critical alert, which says why it was not restarted):

- after 3 restarts of that camera within the last hour;
- when the disk is the cause: less free space than `stopAtFreeGB` (or than 1 GB, if that is less), or ffmpeg's error says the disk is full, failed or read-only. A new ffmpeg would only fail again, and the disk stop above ends the recording cleanly;
- for a calibration video, which is short and watched, and which calibration tools read as one file;
- when campy is stopping the recording (its stop file, or the disk stop).

A new ffmpeg that does not start counts as one of the 3 and is tried again a second later. The first ffmpeg not starting at all is a settings or driver problem, and is not retried.

#### Tested on the GMKtec EVO-X2

On 26 October 2026 the GMKtec EVO-X2 (Ubuntu 24.04) recorded with its own config and `restartFfmpeg: True`: three FLIR Blackfly S (BFS-U3-13Y3M) at 60 fps, triggered by its Arduino Nano ESP32, VA-API H.264 with `keyframeInterval: 0.5`, and real Slack direct messages. A script stopped the cameras' ffmpegs on a schedule. Afterwards every file was decoded and counted, and `campy-session post --only join` joined each camera's files.

| Recording | What was done | Result |
|---|---|---|
| 5 min, 18000 frames per camera | Camera0's ffmpeg killed (kill -9) at 60 s and 180 s, Camera1's at 120 s; Camera2's stopped with SIGTERM at 150 s | Every camera recorded to the end, and campy exited with 0. Each new ffmpeg was writing within about a second. Camera0 ended in 3 files with 17945 frames, Camera1 and Camera2 in 2 files with 17977 and 17998. Each killed ffmpeg lost 23 to 32 frames (0.4 to 0.5 s); the one stopped with SIGTERM finished its file, and lost 2 |
| 2.5 min, 9000 frames per camera | Camera2's ffmpeg killed at 20, 40, 60 and 80 s | Restarted 3 times. At the fourth kill Camera2 stopped, with a critical alert saying it had been restarted 3 times. Camera0 and Camera1 recorded all 9000 frames. Camera2's 4 files held 4530 of its 4608 frames, and 78 were lost (77 inside the stopped ffmpegs, and the one it failed on) |

In both recordings:

- Every file decoded to exactly the frames `segments.csv` gives it.
- "frametimes.npy" and "hosttimes.npy" had one row per frame of the files, and none for a lost frame. Frames grabbed equalled frames written plus frames lost.
- The joined videos, made in under a second each, decoded to the files' frames, frame for frame (decoded-frame checksums). `campy-live-export`'s reader found each frame number at its place in them.
- Slack accepted all 18 messages.

The second recording's join needed `--force`, since a camera did not record to the end.

#### On Windows

On 26 October 2026 the same two recordings ran on the Dell Pro Max Slim FCS1250 (Windows 11), with its own config: four FLIR Blackfly S (BFS-U3-13Y3M) at 30 fps, triggered on Line3 by its Arduino Nano ESP32 on COM9, Quick Sync H.265 in full range with `keyframeInterval: 0.5`, and real Slack direct messages. Windows has no gentle stop for ffmpeg like Linux's SIGTERM, so every stop was a kill (`Stop-Process -Force`, which Windows reports as exit code 0xFFFFFFFF). ffmpeg's pipe then failed with `[Errno 32] Broken pipe` or `[Errno 22] Invalid argument`, and both were restarted.

| Recording | What was done | Result |
|---|---|---|
| 5 min, 9000 frames per camera | Camera0's ffmpeg killed at 60 s and 180 s, Camera1's at 120 s, Camera3's at 150 s; Camera2's left alone | Every camera recorded to the end, and campy exited with 0. Camera0 ended in 3 files with 8978 frames, Camera1 and Camera3 in 2 files with 8980 and 8992; Camera2 had all 9000 in one. Each kill lost 8 to 20 frames (0.3 to 0.7 s) |
| 2.5 min, 4500 frames per camera | Camera2's ffmpeg killed at 20, 40, 60 and 80 s | Restarted 3 times. At the fourth kill Camera2 stopped, with the critical alert. The other three cameras recorded all 4500 frames. Camera2's 4 files held 2265 of its 2326 frames |

Every check above passed here too:

- Every file decoded to its `segments.csv` count.
- The frame times matched the files.
- Frames grabbed equalled frames written plus frames lost.
- The joins took 0.2 s each and were identical to the files, frame for frame. Quick Sync's restarted ffmpeg writes the same codec header, so its files join without re-encoding.
- Slack accepted all 18 messages.

On 27 October 2026 the same two recordings ran on the Dell Precision 7875 (Windows 11) for NVIDIA's encoder. No camera is attached there, so three emulated cameras played recorded clips at 60 fps, through the same campy-acquire. They were recorded with NVENC H.265 on its RTX 4000 Ada and `keyframeInterval: 0.5`, with real Slack direct messages.

- **The 5-minute recording.** It had 4 restarts. Every camera recorded all 18000 frames, campy exited with 0, and each kill lost 6 to 26 frames.
- **The 2.5-minute recording.** It had 3 restarts, then the critical stop at the fourth kill. The other two cameras recorded all 9000 frames.
- **The checks.** Every check above passed, and the joins were identical to the files, frame for frame. A restarted NVENC ffmpeg writes the same codec header too.

What it does not cover: an ffmpeg that hangs while it still takes frames, or waits for the next one. The real stalls seen so far were of that kind (the EVO-X2's SSD not taking writes for 10 to 16 s, a GPU's video firmware stalling), and they show as frames waiting for ffmpeg, then dropped frames (the table above). Restarting such an ffmpeg would mean killing it on a guess, so it is alert-only. No ffmpeg has exited mid-recording on its own on the rigs campy has been tested on so far, so this is a safety net for something rare. Besides the rig test above, it is tested with emulated cameras (ffmpeg killed mid-recording; every frame checked to be in its file or named as lost).

#### An ffmpeg that does not exit

An ffmpeg that stops (an encoder error) finishes its video file, closes its input, the pipe campy gives it frames through, and exits. ffmpeg 8.1's own log shows that order. campy notices the closed pipe at its next frame, and then waits for ffmpeg to exit. In campy 2.3.0 and older it waited with no limit. An ffmpeg stuck on its way out, in a GPU driver call that doesn't return, would hold up the restart, or the camera's stop, for as long as it lived. The camera's frames would fill `writeQueueMaxSec` meanwhile, and be dropped, with no message naming the cause.

So now, an ffmpeg that has stopped taking frames but hasn't exited 3 s later is killed. A healthy one exits 0.05 to 0.2 s after the frame it refused (ffmpeg 8.1 on a Mac, libx264 and the Mac's own encoder, 1280×1024 frames). 3 s, plus the second a new ffmpeg takes to start, fit in `writeQueueMaxSec`'s default 5 s, so the restart can come before the first frame is dropped. Its file was closed before its input, so the kill can't cut it. `segments.csv`'s `ended` and the camera's `videoError` say it, for example "ffmpeg stopped after taking 4210 frames, and did not exit within 3 s, so campy killed it: [Errno 32] Broken pipe". A process stuck inside the operating system can't be killed until the driver lets go. If one is still there 10 s after the kill, campy says so, and waits for it.

Two kinds of ffmpeg are never killed this way. One that still has its input open, taking frames slowly or not at all for a while, is the stall above, and alert-only. One finishing its video as the recording ends, however long that takes, is the other.

Tested on 4 November 2026 on a Mac, with emulated cameras and a stand-in ffmpeg. The stand-in passed 100 frames to the real ffmpeg (libx264), which wrote them to the file, then closed its input and didn't exit for 30 s. It was killed 3 to 3.5 s after it stopped, and the restart came at once. Every frame was in a file or named as lost. Without `restartFfmpeg`, the camera stopped. Before this change campy waited out the stand-in: with one that hung for 20 s, the restart came 21.6 s into an 8 s recording, and 107 frames were dropped. With it, the restart came at 5.6 s, and none were dropped. Neither a stand-in that took no frame for 5 s, its input still open, nor one that took 5 s to exit after the recording's end was killed. Not tested: a real ffmpeg stuck this way (none has been seen), or a rig.

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

**Tested on the EVO-X2** (26 October 2026, Ubuntu 24.04, kernel 6.17). The rig check's smoke test was used: 3 emulated cameras recorded at 60 fps with VA-API H.264, and `campy-live` tracked them in 3D for 60 s. It ran once with the reading on and once with it off.

- With it on, journalctl ran as campy-acquire's child and used 0.00 s of CPU in the minute. campy-acquire's own process used 2.97 s, against 2.94 s with the reading off.
- `campy-live` kept up the same way in both runs: 3600 of 3600 sets, none skipped, inference p50 5.6 / p99 5.8 ms, to 3D 7.6 / 8.5 and 7.6 / 8.4 ms, as in the rig check's baseline.
- No journalctl was left running after either recording.
- A third run swapped journalctl for a script that printed made-up stall, hang and ring-reset lines in the driver's formats. Each alert was said and sent within the watch's 2-second check, and reached the phone as a Slack direct message: the stall warning at the third stall, then the hang warning and the critical reset.

### The recording's state now: `recording.json`

Every 4 seconds, and as it ends, the watch also writes the recording's state to `~/.campy/status/recording.json` (`%USERPROFILE%\.campy\status\recording.json` on Windows), for a control panel to show: the state (`recording`, `stopping`, `ended`, `failed`), the session folder, its start, how long it has run and is planned to; for each camera its frame rate (set and measured), frames, dropped frames, seconds of frames waiting for ffmpeg, USB link speed (Linux and Windows) and state; each disk's free space and when it would be full at the present rate; each camera's trigger pulses without a frame (`trigger_pulses_without_frame`) and the trigger's pauses so far (`trigger_pauses`, each with `planned`: whether it was at least `alertTriggerPauseSec`); the problems open now; the last critical one; and, on Linux with an AMD GPU, `gpu_log`: how the kernel log is read (`journalctl`, `/dev/kmsg`, or why it isn't), and the firmware stalls, hung jobs and resets so far, with each ring's count. Only campy-acquire writes it, and whole each time. `python -m pydoc campy.watch` lists the fields.

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
| `alertExternalWaitSec` | `600` | with an external trigger, seconds of waiting for the first frame before "No frame yet" is a warning; `None` = never ([above](#a-trigger-started-well-after-campy-alertexternalwaitsec)) |
| `alertDiskFreeGB` | `20` | free space below which a disk is an alert |
| `stopAtFreeGB` | `2` | free space below which the recording stops, cleanly; `0` = never |
| `restartFfmpeg` | `False` | restart a camera's ffmpeg that stops mid-recording, into a new file ([above](#restarting-a-stopped-ffmpeg-restartffmpeg)) |
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

Every frame each camera grabbed was in its video, which decoded without an error (3771, 3771 and 2253 frames), with `frametimes.npy`, `hosttimes.npy` and `metadata.csv` beside it and one host time per frame. `alerts.log` had one JSON line for each problem and one for each message sent, Slack accepted every message, and the file holds no token. `recording.json` ended as `failed`, with each camera's state (`ended`, `ended`, `lost`) and the two problems still open. Its `usb_speed` was empty on Windows then, as the USB link check was Linux only; it is read on Windows too now ([A camera on a USB 2 link](#a-camera-on-a-usb-2-link)). The recording's start message, `info`, went only to the log, as the alerts file asks for `warning` and up.

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
