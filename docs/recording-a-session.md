# Recording a session from the desktop

This page is for anyone who records on a rig that someone has already set up: no terminal needed. Every campy recording on the rigs starts this way, live tracking (`campy-live`) included, and so do the ChronicRecord rig's sessions with its Pico and SpikeGLX ([below](#on-the-chronicrecord-rig-the-pico-and-spikeglx)). You double-click an icon, check that the cameras look right, press **Start recording**, and press **Stop** at the end (or let it stop at the set length). The person who set up the rig makes the icon once; how is at the end of this page.

## Before you start

- **The animals and the cage are ready**, and the room is as it should be for the recording (lights, doors).
- **Nobody else is recording on this computer.** Only one recording runs at a time; the page tells you if another one is still running.

## 1. Open the panel

Double-click the icon on the desktop, named something like **Record with campy (rig)**.

- A black window opens and fills with lines of text. Leave it open: it is the panel itself. You don't need to read it.
- Your web browser opens the panel's page. If it doesn't, the black window's first lines show its address (`http://127.0.0.1:8765/?t=...`): copy the whole address, key included, into the browser.

The page is only for this computer. Working over Remote Desktop, open it in the rig's own browser, inside the Remote Desktop window.

On a rig that other tests share (most of them), the panel first waits for the rig to be free, then holds it for as long as it is open, so that no test runs while you record. While it holds the rig, Ctrl-C in the black window does nothing: close the panel with **Close the panel** at the top of the page once nothing is recording.

## 2. Fill in the experiment and check the list

At the top of the page, under **Before the start**:

- **Experiment**: the experiment's name, for example `Social hierarchy, week 3`.
- **Animal IDs**: the animals in the cage, separated by commas: `M12, M14, M15`.
- **Notes**: anything worth knowing later: `cage 2, new bedding`.

These are saved with the recording, so you can find it again.

Next to them is a list of checks:

- **ok** (green): fine.
- **warning** (yellow): you can still record, but read it. The usual one: less free disk space than usual.
- **stops the start** (red): the recording can't start until it is fixed. Ask the person who set up the rig.

The recording's length is fixed by the rig's settings, and the page shows it (for example `2:00:00 each`).

## 3. Start

Press **Start recording**. Within about a minute, depending on the rig, the page shows **Recording** at the top, and:

- **Each camera's picture**, refreshed every second. Check that every camera shows the cage. Under each one, a green **OK**, or a yellow label saying what's wrong (see below), and its frames per second, which should be steady.
- **Disk free**, and about how long it lasts.
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

**You can close the page** at any time: the recording goes on. Open it again from the black window's address, or by double-clicking the icon, which opens the same page again. **Don't close the black window** while recording if you can help it. If you do by mistake, the recording still goes on to its end. When you open the panel again, it shows that recording as still running, with a button to stop it.

## 5. Stop

The recording stops by itself at its set length. To stop earlier, press **Stop** at the top. Each camera then finishes its video, which takes a few seconds. If a camera hangs, the button turns into **Stop now, without closing** for a few seconds: press it only if the page has been stuck on **Stopping** for more than a minute.

After the recording, the page shows **After the recording** while the computer:

- turns the videos into ordinary video files (checked to be identical to what was recorded);
- writes the tracking in the files LUC3D opens;
- copies the recording elsewhere, if the rig is set up to.

Each step turns green when done. Then the page says **Ready**, for the next recording.

When you are done for the day, press **Close the panel** at the top of the page (or, on a rig that doesn't hold the rig's lock, Ctrl-C in the black window), then close the browser tab.

## On the ChronicRecord rig: the Pico and SpikeGLX

On the ChronicRecord rig the same page also runs the Pico (which makes the camera pulses, SpikeGLX's record gate and the two LED drivers' TTLs) and SpikeGLX, which records the probe while the gate is high. It replaces the PowerShell launcher (`chronic6day.ps1`). What changes:

**Before the start.** Pick the **session length**: 6 days, a 15-minute gate test (3 bouts) or an hour. The page shows what that plan holds (bouts, camera pulses, camera time). campy records exactly the plan's camera time, so the two always agree. A gate test goes into `C:\data\gatetest\`, the others into `C:\data\`. The checks add:

- the Pico answers and isn't running anything;
- SpikeGLX answers, isn't running a run, and its trigger is TTL (so it writes files only while the gate is high). If SpikeGLX isn't open, the page opens it at Start;
- Windows Update is paused until a day after the session ends. If not, it's a warning with a **Pause Windows Update** button: Windows asks for administrator rights, click Yes;
- USB selective suspend is off (a warning if not).

You can also give a time for the Pico to start (`7:20 PM`); otherwise you start it yourself.

**Start.** The page shows each step as it goes:

1. **SpikeGLX:** its run starts into the session's folder (`spikeglx\`), with recording on.
2. **Gate check:** the gate blinks once, and SpikeGLX must open a file for it. If it doesn't, nothing else starts and the page says what to check (SpikeGLX's trigger, the gate's cable).
3. **The cameras:** campy starts, and the cameras' window opens. They wait for the Pico's pulses. The **Pico** card counts them as they get ready (`Cameras getting ready: 2 of 4`), then says **All 4 cameras are ready** in green, with a short chime, and the browser tab's title changes to *Cameras ready*: no need to watch campy's console window.
4. **The Pico:** once every camera is ready, press **Start the Pico** (it asks first), or it starts by itself at the time you gave. If a camera isn't ready, the button says so: stop, and start again.

While it records, the **Pico** card shows the bout (for example `bout 212 of 1728`), the camera pulses so far, and when the session ends. The **SpikeGLX** card shows its run, whether a file is open (the gate is high), and how many files this session has.

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

Make the icon once per config, in the recording environment (the one with `campy-acquire`):

```
campy-session shortcut <config>.yaml --name "Record: 3 cameras"
```

- **Windows**: a shortcut on the desktop (OneDrive's desktop too).
- **Linux**: a `.desktop` icon on the desktop, and an entry in the applications menu. GNOME may ask once to **Allow Launching** it.
- **macOS**: a `.command` file on the desktop.

The icon runs `campy-session panel <config>` with that environment's Python, by its full path. So make it again after moving the environment or the config. Options after the config are `campy-acquire`'s, for every recording the icon starts. For example, `--recTimeInSec 3600` records an hour.

The config needs everything the recording needs: a `live:` section for the tracking, `post:` for what happens after, and `campy-session register-live` run once in the tracking environment (see [Live tracking](live-tracking.md#in-one-command-campy-session)). The view (`campy-live-view`) runs in that registered environment too, or in `view.python`. On a rig shared with tests, add `panel: {rig_lock: auto}`: the panel then holds the rig's lock (`rig.py`) while it is open. A Pico, SpikeGLX and the Windows checks are the `pico:`, `spikeglx:` and `windows:` sections; the ChronicRecord rig's config, [`chronicrecord/chronic_session.yaml`](../chronicrecord/chronic_session.yaml), has each, with a comment on every line. A key campy-session doesn't know is refused, with the list of the keys that section takes. The panel starts `campy-acquire` and `campy-live` apart from its own window, so closing that window, or the terminal on Linux, never ends a recording. Its Stop reaches them through `campy-acquire`'s stop file.
