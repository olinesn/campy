# Recording a session from the desktop

This page is for anyone who records on a rig that someone has already set up: no terminal needed. Every campy recording on the rigs starts this way, live tracking (`campy-live`) included, and so do the ChronicRecord rig's sessions with its Pico and SpikeGLX ([below](#on-the-chronicrecord-rig-the-pico-and-spikeglx)). You double-click the rig's one icon, pick the experiment, check that the cameras look right, press **Start recording**, and press **Stop** at the end (or let it stop at the set length). The person who set up the rig makes the icon once; how is at the end of this page.

## Before you start

- **The animals and the cage are ready**, and the room is as it should be for the recording (lights, doors).
- **Nobody else is recording on this computer.** Only one recording runs at a time; the page tells you if another one is still running.

## 1. Open the panel

Double-click **campy recording** on the desktop. It is the rig's one icon, for every kind of experiment. (A rig not yet changed over may still have one icon per experiment, named something like **Record with campy (rig)**: double-click the one for your experiment, and skip the Experiment list below.)

- A black window opens and fills with lines of text. Leave it open: it is the panel itself. You don't need to read it.
- Your web browser opens the panel's page. If it doesn't, the black window's first lines show its address (`http://127.0.0.1:8765/?t=...`): copy the whole address, key included, into the browser.

The page's top right corner says which version of the software it runs, for example `v2.7.0 · dev · 874060ba`: give it when you report a problem. The page is only for this computer. Working over Remote Desktop, open it in the rig's own browser, inside the Remote Desktop window.

On a rig that other tests share (most of them), the panel first waits for the rig to be free, then holds it for as long as it is open, so that no test runs while you record. While it holds the rig, Ctrl-C in the black window does nothing: close the panel with **Close the panel** at the top of the page once nothing is recording.

### Pick the experiment

The first box on the page, **Experiment**, lists the rig's kinds of recording, for example *ChronicRecord session (Pico + SpikeGLX)*, *Live tracking, 3 mice* or *Look at the cameras (5 min)*. The panel opens on the one picked last on this computer. Pick yours: the rest of the page changes to it (its cameras, its models, its session lengths and bout settings, its checks), and says which config file it is.

- **You can only change it while nothing is recording.** That includes a recording an earlier panel left running, and a Pico or SpikeGLX still running (the page's **Still running** box). Stop those first.
- **Each experiment keeps its own settings.** Bout settings and the live-model pair you change on the page are remembered for that experiment only, as before.
- **Missing from the list?** Press **Show all configs**. It lists every config in the rig's configs folder, and says why any of them can't be used. The star (☆/★) adds a config to the Experiment list or takes it off. **Use** records with one that isn't on the list, without starring it. **Look again** reads the folder again after someone adds or edits a config. The stars are kept for this computer, in `~/.campy/status/panel-types.json`.

## 2. Fill in the experiment and check the list

At the top of the page, under **Next recording** and **Saved with the recording**:

- **Experiment name**: for example `Social hierarchy, week 3`.
- **Animal IDs**: the animals in the cage, separated by commas: `M12, M14, M15`.
- **Notes**: anything worth knowing later: `cage 2, new bedding`.
- **Copy each recording to** (optional): a folder for the copies, for example the lab's network share, `\\server\share\your folder`. After each recording ends, the computer copies its folder there, in the background, while the next recording goes on. Nothing is deleted, here or there. The page remembers the folder for next time; empty the box for no copies.

These are saved with the recording, so you can find it again. If the page can't reach the copy folder when you press Start, it says so and doesn't start: check the address, or open the share once in File Explorer. If only the last folder is missing (for example a new `\\server\share\your folder\new-project` in a folder that is there), it says the folder doesn't exist yet and shows **Create this folder**: press it, then **Start recording** again. It makes only that last folder, so a mistyped share or parent folder makes nothing.

Next to them, under **Checks**, is a list of checks:

- **ok** (green): fine.
- **warning** (yellow): you can still record, but read it. The usual one: less free disk space than usual.
- On the ChronicRecord rig the disk line is what the chosen length needs instead, and a length that doesn't fit stops the start ([below](#on-the-chronicrecord-rig-the-pico-and-spikeglx)).
- **stops the start** (red): the recording can't start until it is fixed. Ask the person who set up the rig.

The recording's length is fixed by the rig's settings, and the page shows it (for example `2:00:00 each`).

## 3. Start

Press **Start recording**. Within about a minute, depending on the rig, the page shows **Recording** at the top, and:

- **Each camera's picture**, refreshed every second. Check that every camera shows the cage. Before a camera's first frame it says "No picture yet" instead (on the ChronicRecord rig, until you start the Pico, since the cameras wait for its pulses). Under each one, a green **OK**, or a yellow label saying what's wrong (see below), and its frames per second, which should be steady.
- **Disk free**, and about how long it lasts. On the ChronicRecord rig, what the rest of the session's plan still needs instead (an estimate), with how many GB it is short in red if it doesn't fit.
- **Tracked in 3D**: how many animals the live tracking sees, for example `5 of 5`.
- **Programs**: each should say **running**.
- The time recorded so far, out of the recording's length.

## 4. While it records

- **Bookmarks**: when something happens you'll want to find later (a fight, a rearing, a disturbance in the room), type a few words in the box under **Bookmark** and press **Add** (or Enter). It notes the exact moment, with your words, in the recording's `bookmarks.csv`.
- **The viewer**: a separate window may show the cameras with the tracking drawn on them. If someone closed it, **Open the viewer again** brings it back. Closing it never affects the recording.
- **Warnings** lists anything that went wrong, and things that went wrong and were then fixed. Yellow labels on a camera mean:

| Label | What it means | What to do |
|---|---|---|
| `NO FRAMES` | The camera stopped sending pictures | Tell the person in charge of the rig; the other cameras keep recording |
| `PAUSED 4:30` (grey, on every camera at once) | The trigger is pausing as planned, as between the bouts of a ChronicRecord session: no camera gets pictures until the next bout | Nothing. If it lasts longer than the config's `alertNoFramesSec`, it turns into `NO FRAMES`: the trigger has stopped |
| `LOST`, `GAP`, `LATE` | The camera lost or delayed a few frames in the last 10 seconds | Nothing, unless it keeps coming back: then note it in a bookmark |
| `1 FRAME AHEAD` or `BEHIND` | That camera is out of step with the others | Stop, and start a new recording |
| `40.0 FPS` (lower than the others) | The camera is falling behind | Tell the person in charge of the rig |

**You can close the page** at any time: the recording goes on. Open it again from the black window's address, or by double-clicking the icon, which opens the same page again. A tab left open, or one the browser brings back when it starts again, keeps working with the next panel too: the page's address stays the same from one panel to the next. While the panel is closed, the page says so at the top (**The panel is closed**) and comes back by itself once the panel is open again. A tab from before this (its address has an older key) says **This tab belongs to a panel that was closed**: close it, and use the other tab. **Don't close the black window** while recording if you can help it. If you do by mistake, the recording still goes on to its end. When you open the panel again, it shows that recording as still running, with a button to stop it.

## 5. Stop

The recording stops by itself at its set length. To stop earlier, press **Stop** at the top. Each camera then finishes its video, which takes a few seconds. If a camera hangs, the button turns into **Stop now, without closing** for a few seconds: press it only if the page has been stuck on **Stopping** for more than a minute.

After the recording, the page shows **After the recording** (at the top: **Finishing the recording's files**, with a progress bar and which step of how many is running, for example `Step 4 of 7 · Calibration (calibrat3) · 0:47`). Under **Steps after the recording**, the running step says what it is doing and for how long. Once those steps are done, the box is called **Previous recording**, with when it ended and how long it lasted. Under **How it went**, a few lines say what went wrong while it recorded, so the problems shown under **Warnings** aren't lost when the recording ends:

- the cameras that dropped frames, each with how many of the frames it took: `6 of 8 cameras dropped frames: Camera2_mid 501 of 3,285, ... (the video encoder fell more than 5 s behind: writeQueueMaxSec)`;
- frames lost in a stopped video encoder (ffmpeg) or before they reached the computer, a camera's error, a camera that stopped without closing its video;
- each critical alert, with the time it turned critical and whether it was resolved or still open at the end, and the warnings still open at the end. An alert's text is as it was at that time, so its counts can be lower than the totals above.

A recording without a problem says so in one line: `All 8 cameras: every frame saved (3,285 frames each)`. The lines come from the files in the recording's folder (each camera's `metadata.csv`, `logs/campy-acquire.log` and `alerts.log`). They stay on the page until the next recording starts, also when you open the page again, or close the panel and open it again. If a line is red, tell the person in charge of the rig.

Below them, under **Steps after the recording**, the computer:

- **Video files**: turns the videos into ordinary video files (checked to be identical to what was recorded);
- **Tracking files for LUC3D**: writes the tracking in the files LUC3D opens;
- **Copy**: with a folder in **Copy each recording to** (below);
- **Extra commands**: any the rig's settings add.

Each step turns green when done (grey **skipped** when it has nothing to do). The top of the page then says **Ready to record**: you can start the next recording.

**Copies.** With a folder in **Copy each recording to**, the page then lists the recording under **Copies**, and as the **Copy** step under **Steps after the recording**: waiting, copying (how many GB so far, with a progress bar and about how long is left, at the rate so far, with the time it should be done), done, or failed with the reason. Copies go one at a time, so a recording waits while the one before it copies. You can start the next recording right away: copying doesn't disturb it. While a copy is running or waiting, a **Copying in the background** strip at the top of the page shows it, with its progress bar and time left, and says you can start a new recording; it stays during the next recording, until the last copy is done. Leave the computer on and the panel's black window open until then. On the 8-camera rig, a 2.1 GB recording (about 15 minutes) took under 2 minutes to copy to the network share.

When you are done for the day, press **Close the panel** at the top of the page (or, on a rig that doesn't hold the rig's lock, Ctrl-C in the black window), then close the browser tab. If recordings are still being copied, the panel stays open until the copies are done, then closes by itself; leave the computer on until then. **Close now, without the copies** closes it at once: the recordings not copied stay on this computer, and each one's `logs/post.log` gives the command that copies it by hand.

## On the ChronicRecord rig: the Pico and SpikeGLX

On the ChronicRecord rig the same page also runs the Pico (which makes the camera pulses, SpikeGLX's record gate and the two LED drivers' TTLs) and SpikeGLX, which records the probe while the gate is high. It replaces the PowerShell launcher (`chronic6day.ps1`). What changes:

**Before the start.** Pick the **session length**: 6 days, a 15-minute gate test (3 bouts), an hour, or an 8-hour overnight rehearsal (96 bouts; on 6 November it took 152 GB: SpikeGLX 150 GB, the videos only 2 GB with no animal in the cage, so it fits on the PC's own disk where 6 days don't). The page shows what that plan holds (bouts, camera pulses, camera time). campy records exactly the plan's camera time, so the two always agree. A gate test goes into `C:\data\gatetest\`, a rehearsal into `C:\data\rehearsal\`, the others into `C:\data\`. The checks add:

- the Pico answers and isn't running anything;
- SpikeGLX answers, isn't running a run, and its trigger is TTL (so it writes files only while the gate is high). If SpikeGLX isn't open, the page opens it at Start;
- Windows Update is paused until a day after the session ends. If not, it's a warning with a **Pause Windows Update** button: Windows asks for administrator rights, click Yes;
- USB selective suspend is off (a warning if not);
- **the disk:** how much the chosen length needs, against the free space where it records, for example `Disk: this length needs about 162 GB of the 370 GB free` for the overnight rehearsal. It counts SpikeGLX (24 MB/s while the gate is open, plus its 1 s margin before and after each bout) and the videos (0.6 MB/s a camera while the cameras take frames, more than a cage with mice usually writes), so it is an estimate, and it keeps `stopAtFreeGB` (2 GB) free on top. A length that doesn't fit is red and stops the start: 6 days need about 2,910 GB, so on the PC's own disk the page says how many GB short it is and asks for a shorter length or a data disk. If you really mean to start it anyway (data will be moved off the disk during the session), **Start recording** asks first, naming the shortfall; nothing else that stops the start can be passed over. Above 80% of the free space it is a warning.

**The bouts.** Under the length, **Bout settings** shows what each bout does, and each setting can be changed there:

| On the page | The Pico's setting | The config's (chronic_control.py's default) |
|---|---|---|
| A bout every (s) | `INTERVAL` | 300 |
| Baseline: gate open to the LEDs, and camera off to gate closed (s) | `TOTAL_BASELINE` | 10 |
| Camera on before the LEDs (s) | `CAMERA_LEAD_IN` | 5 |
| Camera time, normal bout (s) | `STANDARD_STIM_DURATION` | 10 |
| Camera time, long bout (s) | `EXTENDED_STIM_DURATION` | 240 |
| A long bout every (bouts; the first is long) | `EXTENDED_EVERY` | 6 |
| Blue LED on (ms) | `PULSE_WIDTH_C` | 30 |
| Yellow LED on, as blue ends (ms) | `PULSE_WIDTH_D` | 30 |

Below them a line says what one bout does from the gate's opening, for example *camera on at 5 s; blue LED at 9.999 s for 30 ms (frames 300-301), then yellow for 30 ms (frames 302-303); camera off at 245 s and gate closed at 255 s in a long bout, at 15 s and 25 s in a normal one*, with a warning if an LED would switch during a frame's exposure. Each LED lights once a bout. The length's plan (number of bouts, camera time), the disk check and campy's recording time follow at once.

- **The config file isn't changed.** The page's settings are for the recordings started from it. A changed setting is outlined, with the config's value under it. **Back to the config's** undoes every change; an emptied field undoes its own.
- **The page remembers them.** The next panel opened with the same config starts from the last settings its page had, and says so (`2 changed, as the last panel left them (Mon 9 Nov, 3:12 PM)`). They're kept in `~/.campy/status/panel-bouts.json`, one entry for each config; **Back to the config's** removes that config's.
- **The checks list says what was changed**, and a setting a bout can't have stops the start with the reason: an LED outside the camera's time, a bout that runs into the next, the camera starting before the gate opens, or more bouts than the Pico holds (2,048: 7.1 days at one bout every 300 s).
- **The recording says which settings it ran with.** Its `logs\session-config.yaml` has them in `pico:`'s `set:`, the line marked `# panel (bout settings set on campy-session panel's page: ...)`, and its `launcher.json` has every setting the Pico ran with. The Pico's own log, in the session's `pico\` folder, lists each bout's times, as always.
- **They can't change while a recording runs.** The Pico starts with the settings the recording was started with.
- **To keep settings for every session,** put them in the config's `pico:` section, in `set:` (for example `set: {INTERVAL: 600}`; the LEDs' in seconds there, `PULSE_WIDTH_C: 0.05`). The camera's rate (`CAMERA_FPS`, which must match campy's `frameRate`) and the finer timing settings are in `set:` only.

You can also give a time for the Pico to start (`7:20 PM`); otherwise you start it yourself.

**Start.** The page shows each step as it goes:

1. **SpikeGLX:** its run starts into the session's folder (`spikeglx\`), with recording on.
2. **Gate check:** the gate blinks once, and SpikeGLX must open a file for it. If it doesn't, nothing else starts and the page says what to check (SpikeGLX's trigger, the gate's cable).
3. **The cameras:** campy starts, and the cameras' window opens. They wait for the Pico's pulses. The **Pico** card counts them as they get ready (`Cameras getting ready: 2 of 4`), then says **All 4 cameras are ready** in green, with a short chime, and the browser tab's title changes to *Cameras ready*: no need to watch campy's console window.
4. **The Pico:** once every camera is ready, press **Start the Pico** (it asks first), or it starts by itself at the time you gave. If a camera isn't ready, the button says so: stop, and start again.

While it records, the **Pico** card shows the bout (for example `bout 212 of 1728`, or `between bouts`), the camera pulses so far, and when the session ends. The **SpikeGLX** card shows its run, whether a file is open (the gate is high), and how many files this session has.

**Stop.** **Stop** asks first, and offers two ways:

- **Stop after this bout:** the Pico finishes the bout it is in, so its gate falls on time and SpikeGLX's file closes as planned; then campy and SpikeGLX stop.
- **Stop now:** the Pico stops at once (every output low), then campy, then SpikeGLX.

**The end.** campy stops by itself after the last bout's last frame. The page then waits for the Pico's last gate (10 s later) and stops SpikeGLX's run.

**If campy fails on its own** (a camera lost, say), the Pico and SpikeGLX are left running: the animal's LED schedule goes on, and SpikeGLX keeps recording its bouts. The page says so under **Still running**, with **Stop the Pico**, then **Stop SpikeGLX's run**, for when you decide. The same buttons appear if the panel's window was closed during a session and the panel was opened again.

The session's folder, `C:\data\<date_time>\`, also holds `spikeglx\` (SpikeGLX's run, named `chronic_<date_time>`) and `pico\` (the Pico's log, `<date>\timestamps.txt`, and its program's output, `session.out`).

## Where the recording is

At the top of the page, under the rig's name, is the recording's folder, for example `D:\data\2026-10-23_14-02-11`. It holds:

- one folder per camera, with its video;
- `bookmarks.csv`, your bookmarks;
- `session.json`, which records the experiment, the animal IDs, your notes and how everything was set;
- `logs\`, which the person who looks after the rig may ask for if something went wrong.

## For the person who sets up the rig

Make the icon once, in the recording environment (the one with `campy-acquire`), for the rig's folder of configs:

```
campy-session shortcut <folder of configs>
```

It is named **campy recording** (`--name` gives another name), and opens one panel for every config in the folder, picked on the page's Experiment list. Before anyone stars a config on the page, the list shows the configs that have a title, `panel: {title: "Live tracking, 3 mice"}`, by that title; so give a title to each config people should record with, and leave it out of old or test configs. A config without a title shows under **Show all configs** by its file name. A title belongs to the file that gives it: a config built on a titled one with `extends:` (a short trial of the daily config, say) doesn't take that title, so it stays off the list until it gets a title of its own or a star. The panel holds the rig's lock (below) when any config in the folder asks for it.

An icon for one config still works: `campy-session shortcut <config>.yaml --name "Record: 3 cameras"` opens the panel with that config alone, with no Experiment list.

- **Windows**: a shortcut on the desktop (OneDrive's desktop too).
- **Linux**: a `.desktop` icon on the desktop, and an entry in the applications menu. GNOME may ask once to **Allow Launching** it.
- **macOS**: a `.command` file on the desktop.

The icon runs `campy-session panel <folder or config>` with that environment's Python, by its full path. So make it again after moving the environment, the folder or the config. Options after the folder or config are `campy-acquire`'s, for every recording the icon starts. For example, `--recTimeInSec 3600` records an hour.

The config needs everything the recording needs: a `live:` section for the tracking, `post:` for what happens after, and `campy-session register-live` run once in the tracking environment (see [Live tracking](live-tracking.md#in-one-command-campy-session)). The view (`campy-live-view`) runs in that registered environment too, or in `view.python`. On a rig shared with tests, add `panel: {rig_lock: auto}`: the panel then holds the rig's lock (`rig.py`) while it is open. A Pico, SpikeGLX and the Windows checks are the `pico:`, `spikeglx:` and `windows:` sections; the ChronicRecord rig's config, [`chronicrecord/chronic_session.yaml`](../chronicrecord/chronic_session.yaml), has each, with a comment on every line. A key campy-session doesn't know is refused, with the list of the keys that section takes. The panel starts `campy-acquire` and `campy-live` apart from its own window, so closing that window, or the terminal on Linux, never ends a recording. Its Stop reaches them through `campy-acquire`'s stop file.
