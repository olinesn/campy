# campy rig fleet: design (session rig-fleet, 2026-10-23)

> **Status: a proposal, on hold since 2026-10-23.** Nothing in it has been built or installed. It is kept, with its open
> questions, for when the work is picked up. The diagrams are SVG files in `rig-fleet/` next to this page.

Goal: releases, health checks and rig checks on the rigs (EVO-X2, Dell Pro Max Slim FCS1250, Precision 7875, and since
2026-10-28 the Dell Pro Tower Plus, an experiment machine: section 5) stop being hand work, without ever disturbing a
recording or another session's locked run.

Nothing has been changed on any rig. I only copied the existing rig-check scripts, READMEs and update scripts to my
scratchpad with scp (a file read). On 2026-10-23 at 06:45 the EVO-X2 was held by another session (a long batch job); both Windows rigs were free.

---

## 0. One fact first: the repository is public, not private

The brief says olinesn/campy is private. GitHub says `"visibility": "PUBLIC"` (CLAUDE.md agrees: "the repo is public").
Its fork setting is "require approval for first-time contributors" only. This changes the CI answer:

- GitHub's own guidance is to use self-hosted runners **only with private repositories**: on a public repo, a pull
  request can carry a workflow that runs on your machine.
- **Actions logs on a public repo are public.** The runner prints its runner name and the machine's host name in every
  job's "Set up job" step, and every path a step echoes (`C:\Users\<user>\...`). That breaks the docs rule (no host
  names, user names, paths) the moment the first job runs, and it can't be switched off.

So I recommend **not** registering the rigs as GitHub runners, and doing hardware CI from the Mac instead (below,
option A). Option B (real self-hosted runners) is written out too, with what it would take.

---

![Option A: the Mac reads the PR, sends code to the rig over SSH and posts only numbers back; GitHub and the rigs never connect. Option B: a runner service on the rig takes jobs straight from GitHub and its job logs are public.](rig-fleet/01-ci-options.svg)

*What you're choosing between: in A the only links to GitHub go through the Mac, and only numbers leave it; in B a GitHub service connects straight to a lab machine and its logs are public.*

## 1. Hardware-in-the-loop CI (priority 1)

### Option A (recommended): the Mac runs the rig checks for PRs, rigs never talk to GitHub

The Mac already has everything CI needs: your GitHub login (gh), SSH to all three rigs, and the release routine
(bundle + scp). A command on the Mac, `campy-fleet ci`, does what a runner would:

1. **Which PRs.** A PR is tested only when all of these hold, checked through the GitHub API each time:
   - it has the label **`rig-check`**, and the label was added by `olinesn` (the PR's timeline says who added it);
   - its author is `olinesn` and its branch lives in olinesn/campy itself (no forks, ever);
   - it changes campy-live or recording code: `campy/live*.py`, `campy/cameras/**`, `campy/writer*`, `campy/campy.py`,
     `campy/livefeed.py`, `campy/clock.py`, `bench/rig*` (list adjustable). Docs-only PRs are skipped even when labelled;
   - the commit tested is the PR's head **at the moment the label was added**. A push after that needs the label again,
     so nothing you didn't look at runs on a rig.
   Note: Claude sessions use your GitHub login too, so to GitHub a label added by a session looks like yours. If you want
   the label to be a human-only gate, sessions must be told never to add it (I'd put that in CLAUDE.md).
2. **Getting the code there.** A git bundle of exactly that commit, over the SSH you already use. The rigs keep having no
   GitHub credentials.
3. **Running it without touching the rig's release install.** The PR's code goes into its own folder on the rig
   (`<rig root>/ci/pr<N>-<sha>`), and the rig checks run with that folder first on PYTHONPATH (it wins over the editable
   install; this is how benchmark sessions already test their copies). The release checkout and both envs are untouched.
   A PR that changes setup.py's dependencies is reported as "needs new packages: not run" instead of installing anything.
4. **Under the lock, and yielding to recordings.** The checks run as one `rig.py run` job with `--no-wait`: if the rig
   is busy, CI says "busy, will retry" and tries again later, it never queues behind or interrupts anyone. New in
   rig.py: **`--yield`**: if a camera, encoder or trigger user appears outside the job while it runs (you start a
   recording), rig.py stops the job within 2 s and the result says "yielded to a recording". This matters because a
   recording you start by hand doesn't take the lock: today nothing stops a test from running underneath it.
5. **The result.** One PR comment per run (edited in place on reruns) plus a commit status per rig
   (`rig-check/precision-7875` etc.), so the PR page shows green/red. Example:

   > **Rig checks for 3f2a1c9** (vs each rig's baseline, v2.1.0 / dev 46fc4d7)
   > | Rig | Check | Sets skipped | Inference p50 / p99 | Frame to 3D p50 / p99 | Verdict |
   > |---|---|---|---|---|---|
   > | Precision 7875 (RTX 6000 Ada) | replay3 | 0 (0) | 1.6 / 5.7 ms (1.6 / 5.7) | 4.7 / 8.3 (4.7 / 8.3) | same |
   > | Dell Pro Max Slim (RTX 2000 Ada) | replay3 | 0 (0) | 6.1 / 6.9 (5.8 / 6.6) | ... | +5% p50, within limits |
   > | EVO-X2 (Radeon 8060S) | — | | | | busy (another session's run); retrying |

   Rigs named by make and model only; no paths, host names or user names; raw logs stay on the rig and the Mac.
6. **When it runs.** Either on demand (`campy-fleet ci 152`, by you or a session), or every 10 min by a small background
   job on the Mac (a launchd agent) that looks for newly labelled PRs. The Mac must be awake and on the lab
   network/VPN; when it isn't, checks just wait.

![A PR runs on the rigs only after four gates: label added by olinesn, author olinesn with a branch in this repo, live/camera/writer code changed, dependencies unchanged.](rig-fleet/02-pr-gates.svg)

*The gates a PR passes before any of its code reaches a rig. The last box is why a push after labelling never runs unseen.*

What A needs on the rigs: nothing new running. Only a `ci\` folder next to the rigcheck folder, and the updated rig-check
scripts (stage 1). What it needs from GitHub: nothing beyond your existing gh login (comments, statuses).

Downsides: depends on the Mac being up; PR checks are not a "required check" GitHub can enforce on its own (statuses can
still be required in branch protection if you want).

### Option B: rigs as self-hosted GitHub Actions runners (what it would take)

If you prefer real runners, the safe version is:
- **Make the repo private first**, or run CI from a private mirror. On the public repo I would not install it.
- Repo-level runners labelled `rig-<model>`; a workflow triggered only by `pull_request_target` + the label (it runs the
  workflow file from dev, never the PR's), checking author = olinesn and same-repo branch before checking out the PR.
  Fork PR setting raised to "require approval for all outside collaborators".
- Windows: the runner as a service (`config.cmd --runasservice`). For GPU, conda envs and rig.py it has to run as the
  rig's own user account, so anything that escapes the checks runs as that user. Linux: a systemd service.
- Each runner holds a long-lived registration on the rig and keeps an outbound connection to GitHub open; the antivirus
  on both Windows rigs and the IT department may object to that, which is worth asking before trying.
- Every job still goes through rig.py (`--no-wait --yield`) and posts the same comment.

Residual risks even then: any workflow on any branch you or a session push with `runs-on: rig-*` runs on lab machines;
the runner auto-updates itself from GitHub. I think A gets the same result with none of this.

---

## Every unattended job on a rig: the same safety steps

CI, updates and nightly checks all start a rig job the same way, so the rules that protect recordings and other sessions
live in one place.

![Look first with file reads; busy means retry later. If free, launch rig.py run --no-wait --yield. Taken meanwhile: exit 75, retry. A recording starting stops the job within 2 s. Contaminated: rerun once. Clean: result vs baseline.](rig-fleet/03-rig-job-safety.svg)

*The path every unattended rig job takes. The two exits at the top keep it off recordings and other sessions' runs: it never waits in line, and it gives way the moment a camera or encoder is opened.*

---

## 2. One-command release updates (priority 2): `campy-fleet update v2.1.1`

From the Mac, folding in today's `update_v2.1.0.sh` / `.ps1`:

1. `git fetch origin --tags`; check the tag exists (and is a GitHub release, or `--allow-dev` for a dev commit as with
   46fc4d7); build **one** bundle; note its SHA-256.
2. For each rig, in parallel:
   - **Look before touching** (no python on the rig, see "Probes" below): lock held? camera/encoder processes running?
   - **Copy the bundle** (scp; safe even when the rig is busy).
   - **Wait for a free rig** without disturbing it: if held, the Mac re-reads only the lock file every 5 min (an scp-sized
     read, no PowerShell, no WMI: the 30-s PowerShell polling of 2026-10-22 is what spoiled smoke3).
   - **Launch one detached job** (setsid on Linux; `Invoke-CimMethod Win32_Process Create` on Windows, so it survives the
     SSH drop) under `rig.py run fleet update-<tag>`. Waiting rig.py processes count as nothing in someone else's run,
     so even a race is harmless.
   - The job (`bench/fleet/rig_update.sh` / `.ps1`, in the repo): `git fetch` the bundle into origin refs + tags;
     `git merge --ff-only <tag>` (refuses anything that isn't a fast-forward; your uncommitted
     config edit on the Dell Pro Max Slim stays); `pip install --no-deps -e` in **both** envs;
     `pip show campy` in both must give the new version; entry points answer `--help`; then the rig checks
     (smoke1, smoke3, replay3), each rerun once if RIG_CONTAMINATED; a JSON result file.
3. The Mac picks up each rig's result once it's there and prints one report (versions before/after, each check against
   the old baseline). Clean results become the new baseline (`rigcheck/baseline.json` for the tool, and the same
   human-readable block appended to `README.txt` as today). A result worse than the limits (section 4) is **not** made
   the baseline; the report says "needs review".
4. It also writes a ready-to-paste numbers block for the docs PR and the memory notes. The docs PR itself stays a
   session's job, per CLAUDE.md.
5. `--rigs precision,slim` to do only some; `--dry-run` to show what it would do; `--rollback v2.1.0` (checks out the
   older tag instead of fast-forwarding, only when asked).

![The Mac builds one bundle, copies it to each rig, re-reads the lock file every 5 minutes and launches the job when free. On the rig one locked rig.py job fast-forwards, reinstalls both envs, checks versions, runs smoke1, smoke3, replay3 and updates the baseline; the result goes back to the Mac.](rig-fleet/04-update-sequence.svg)

*A release update for one rig. Everything that starts python on the rig happens inside the single locked job; before it, the Mac only copies a file and reads the lock file.*

Rig details (ssh alias, checkout path, env pythons, rigcheck folder, lock root, EVO-X2's jump-host fallback) live in
**`~/.campy/fleet.toml` on the Mac, outside the repo** (the repo is public). The repo gets an example with placeholders.

---

## 3. Rig status and a dashboard (priority 3)

### What each rig reports

| Field | Linux (EVO-X2) | Windows | Cost |
|---|---|---|---|
| campy version / tag | `git describe` in the checkout; pip versions from the last fleet job | same | file reads / git |
| lock state and owner | RIG_BUSY + `flock -n` | gpu.lock contents | file read |
| recording running? | `pgrep campy\|ffmpeg` | process names (Get-Process, no WMI) | light |
| disk free | `df` | Get-PSDrive | light |
| cameras: FLIR serials, USB speed | /sys/bus/usb (vendor 1e10: serial, speed) | PnP device list (serial); USB speed only from Spinnaker, so from the nightly job | light / in-lock |
| GPU clock, throttle reasons | sysfs | nvidia-smi | light |
| last rig check, vs baseline | the latest result JSON | same | file read |
| pending update | rig tag vs newest GitHub release (asked on the Mac) | same | none on rig |
| recording (state, elapsed, per-camera counters, disk projection, last error) | `~/.campy/status/recording.json`, written by campy-acquire's watch thread (alerts session) | same | file read |
| launcher (parts, live inference/skipped, post-steps) | `~/.campy/status/launcher.json`, written by campy-session (session-launcher); see rig_status_files.md | same | file read |

**Probes in two weights.** The *light* probe reads files and runs git/df/pgrep only; it never starts python. When a rig
is held by someone else, the probe reads **only the lock file** and shows the rest from the last snapshot (marked stale),
so looking at the dashboard can never spoil a run. The *full* snapshot (camera USB speed via Spinnaker, pip versions) is
taken inside fleet jobs, which already hold the lock.

![The light probe always reads the lock file; it reads git, disk, process names, the status files, the last result and the fleet snapshot only when no other session holds the rig. The Mac makes an HTML page with stale parts marked.](rig-fleet/05-dashboard-probe.svg)

*Why looking at the dashboard can't spoil a run: when another session holds a rig, the probe reads its lock file and nothing else.*

### Where to show it

- **A. Static page, regenerated on demand (recommended to start).** `campy-fleet status` prints a table; `--html` writes
  one self-contained page on the Mac. No service, nothing listening anywhere, works offline from the last snapshot.
- **B. A small always-on service** (on a rig or the Mac): live, but it's a process running next to recordings, polling
  rigs, with an open port. Not worth it for three machines.
- **C. A claude.ai artifact with live data.** Evaluated with the artifact-capabilities skill: a private page can keep
  rows in its database (`db`), but only Claude can write them (ArtifactData), so the Mac's CLI can't push to it directly.
  It works as "a Claude scheduled task runs `campy-fleet status --json` and writes the rows" (or a page that calls a
  local `campy-fleet` MCP server, which only answers inside the Claude app on this Mac). Its plus: you can see it on
  your phone. Its cost: a Claude task every N minutes, and rig data on claude.ai (private, but I'd still name rigs by
  model and leave out paths). I'd add it after A if you want the phone view.

---

## 4. Nightly rig checks with drift detection (priority 4)

`campy-fleet nightly`, started by the same Mac background job at a set hour (e.g. 02:30), takes the same safety path as
every unattended job (diagram 3):
- per rig: skip unless the light probe says idle **and** the hour is inside that rig's allowed window (you set it per
  rig; e.g. never on the Dell Pro Max Slim during an experiment block);
- runs smoke1 + replay3 (about 3 min per rig; smoke3 weekly) as `rig.py run --no-wait --yield`: busy rig = skipped
  tonight, a recording starting = the check stops at once;
- compares with the rig's baseline. Proposed limits: inference or frame-to-3D p50 more than 15% **and** 0.3 ms worse;
  p99 more than 25% and 0.5 ms worse; skipped sets more than baseline + 1% of sets; replay accuracy 2D +0.3 px or 3D
  +0.3 mm; GPU median clock under 70% of the baseline's (catches the Slim's ~900 MHz slow state); RIG_CONTAMINATED twice
  in a row; disk under 100 GB; a FLIR camera missing or below USB 3 (once cameras are attached);
- a drift becomes an alert through the alerts/watchdog session's **`campy/notify.py`** (PR #154; Slack DM + a
  healthchecks.io heartbeat, secrets in `~/.campy/alerts.yaml` outside the repo). From the Mac, one
  `Notifier(source="<rig make/model>", state=~/.campy/fleet/alerts-<rig>.json, background=False)` per rig
  (its state isn't keyed by source, so one state file per rig); `Problem("rigcheck/<check>/<metric>", text, severity)`,
  `Resolved(key)`, `Note("nightly: all clean")`. Each nightly re-report of an open problem sends one reminder (its
  30-min rule, a day apart). Tests without sending: `Notifier(channels=[], say=print)`. I write no notifier of my own, and a line on the dashboard. Nightly results never replace the baseline; only releases do.

---

## 5. An experiment machine in the fleet: the Dell Pro Tower Plus

Added 2026-10-28: a Dell Pro Tower Plus with an Intel Core Ultra 9 285, an NVIDIA RTX A2000 and 8 FLIR Blackfly S
Mono 1.3 MP cameras. It is used mainly for experiments, so it gets stricter rules than the other rigs:
- tests run there only when nobody is using it;
- anyone at the machine can see a test is running and stop it with one click;
- after a stop, the fleet leaves the machine alone until they hand it back.

![On the Dell Pro Tower Plus the Mac starts a test only when nobody is active, nothing records, the hour is allowed and tests aren't paused. A banner in every session shows the running test with a Stop button; one click writes STOP and PAUSE files, rig.py ends the job within 2 s, and PAUSE keeps new tests away. Starting campy or SpinView, or opening a camera, also stops the job.](rig-fleet/06-experiment-machine-stop.svg)

*How a person at the machine takes it back. Every path to "stopped" takes about 2 s and needs no rights over the test's
processes: the STOP file is the only thing the banner touches.*

### When a test may start there

The Mac starts a test on the Tower only when all of these hold:
- **Nobody is using it.** Every console and Remote Desktop session is locked, logged off, or has had no keyboard or
  mouse input for 15 minutes. `quser` gives each session's idle time; it's a small program, not PowerShell or WMI.
- **No camera or recording in use**, checked the same way as on the other rigs.
- **The hour is inside the machine's allowed window**, e.g. nights and weekends only. You set it.
- **Tests aren't paused** (see below).

### Seeing that a test is running

- **A banner at the top of the screen in every logged-in session**, console and Remote Desktop alike, in a colour that
  stands out:

  `campy test running: nightly rig check (replay3), about 3 min left. [Stop test]`

  It stays on top of other windows and hides itself when the test ends. A locked screen can't show it, but it is the
  first thing anyone sees on unlocking or logging in.
- **The same notice in the campy-session panel** (the browser page lab members use to record), with the same Stop
  button, so it's there even when the banner is hidden behind a full-screen program.
- **A tray icon** while a test runs, and a short history of the last tests (what, when, who stopped them).

How the banner works: a small PowerShell + Windows Forms script started at each user's logon by a Task Scheduler task
("at log on of any user", running as that user). It watches the rig's lock file with a file watcher, so it doesn't
poll. It is **not Python** on purpose: rig.py counts every python.exe as a GPU user, so a Python banner would block
every test and spoil every timing.

### Stopping a test and taking over

- **One click on "Stop test"** (banner, panel, or a desktop shortcut "Stop campy test" for when neither is in view).
  It writes a STOP file in the rig folder, which every user may write. rig.py looks for it every 2 s alongside its
  other checks, ends **every process of the job** (it already runs each job in a Windows job object), frees the lock,
  and logs `RIG_STOPPED by <user>`. The banner then says "Stopped. The machine is yours." No admin rights are needed,
  and the user never has to find or kill a process.
- **Automatically, when someone starts working:** opening a camera, or starting campy-acquire, campy-session or SpinView,
  stops the test the same way (rig.py's `--yield`, section 1). Optionally, any keyboard or mouse input at the
  console stops it too (question 9).
- **Taking control lasts.** A stop also writes a PAUSE file: no new tests on that machine until the user clicks "Resume
  campy tests" or the pause runs out. The banner offers 2 hours, until tomorrow morning (the default), or until I
  resume. The Mac reads PAUSE before every start, and the dashboard shows "paused by <user> until <time>".
- A stopped test is reported to you (Slack, through campy.notify) as "stopped by <user>". It's not a failure and not a
  drift.

### What tests do on an experiment machine

- **Never the real cameras.** Rig checks there use emulated cameras and recorded replays only. The status probe reads
  which FLIR cameras are plugged in from Windows' device list, never through Spinnaker, so it can't reset or claim a
  camera. The cameras' USB speed is read from Spinnaker only when you ask, with nobody recording.
- Test recordings go to their own folder and are deleted afterwards, as now. The rig check prints the free disk space
  before it starts.
- Everything that changes the machine needs your OK for that machine: the logon task, the banner script, the shortcuts,
  and the rig.py with STOP/PAUSE and `--yield`.

### The GPU: measure 8 cameras before deciding on an upgrade

The Tower has the same CPU as the Dell Pro Max Slim (Core Ultra 9 285), so the Slim's settings are the starting point:
Quick Sync (`hevc_qsv`) on the CPU's built-in GPU records, and the NVIDIA card tracks. Its RTX A2000 (Ampere, 70 W) is
an older and slower card than the Slim's RTX 2000 Ada. docs/hardware.md says "what 8 cameras need hasn't been measured
since the models were compiled", and the Slim's 4-camera run (30 fps, 3 mice) had its 3D poses 8.3 ms after the frames at the median. 8 cameras is
about twice the work per set, on a slower card. So 30 fps looks likely, and 60 fps with 5 mice is in doubt. The first
job on the Tower, once you allow it, is a rig check with **8 emulated cameras** (recording + campy-live, 3D) at 30 and
60 fps. If it doesn't keep up, the numbers say how much faster a card needs to be, and the Tower's power supply and slot
space decide which one fits. Also worth checking there: 8 USB 3 cameras need enough separate USB controllers. The
dashboard's per-camera USB speed and campy's own USB check show it.

---

## Stages (each a PR against dev, with docs)

| # | PR | Verified how |
|---|---|---|
| 1 | **Rig checks in the repo**: `bench/rigcheck/` with smoke and replay3 scripts for Linux and Windows (from today's on-rig kits, models/clips/calibration stay on the rigs), a result JSON per run (campy-live's own `<out>.json` + the rig.log verdict + clock + versions), baseline.json + a compare tool, `--src <folder>` to test another tree. Plus rig.py `--yield`. | Each rig runs the new scripts under the lock; numbers must match its README baseline (dev 46fc4d7) |
| 2 | **`campy-fleet update` + `status`** (Mac CLI, standard library only, `bench/fleet/`), rig_update.sh/.ps1, fleet.toml example | Dry run, then the next release or a dev update applied to all 3 rigs with it |
| 3 | **`campy-fleet ci`** (label-gated PR checks, comment + statuses) | A test PR labelled `rig-check`; a fork/unlabelled PR must be ignored |
| 4 | **Dashboard** (`status --html`, snapshot in fleet jobs) | Page from all 3 rigs, incl. one held by another session (must show "held", touch nothing) |
| 5 | **Nightly + drift alerts** (campy.notify) | One night on each rig you allow; a forced drift (lowered limit) must alert |
| 6 | **Experiment-machine mode** (section 5): idle and pause checks before every start, STOP/PAUSE in rig.py, the banner and its logon task, shortcuts, the panel notice | On the Dell Pro Tower Plus with your OK: a test stopped from the banner, from the shortcut and by opening a camera, each within 2 s, from both the console and Remote Desktop; no new test while paused |

docs/: a new page `docs/rig-fleet.md` (how to update, run checks, read the dashboard), linked from docs/README.md;
live-tracking.md's "after each update" steps point to it. Nothing public about hosts/paths.

---

## What I need from you (per rig changes need your OK each time)

1. **CI: option A (Mac-driven, recommended) or B (runners; only with the repo made private)?**
2. **Is the `rig-check` label a human-only gate** (sessions never add it), or may sessions label their own PRs?
3. **A background job on this Mac** (launchd agent, every 10 min for labelled PRs + the nightly at 02:30)? Or on demand
   only, or a Claude scheduled task instead?
4. **Per rig: may CI and nightly checks run there unattended, and when?** In particular the Dell Pro Max Slim: does it
   record real experiments, and at what hours must nothing run?
5. **New rig.py option `--yield`** (stop a check when a recording starts): OK to build? (Installing a new rig.py on each
   rig follows the usual free-rig steps, and I'd ask you per rig.)
6. **Dashboard: static page first (A), artifact later (C)?**
7. The drift limits in section 4: fine as a start?
8. **Dell Pro Tower Plus:** which Windows version, and which accounts log in there (one shared lab account, or one per
   person)? Is SSH set up on it yet? Its allowed test hours?
9. **Should any keyboard or mouse input at the Tower stop a test by itself**, or only the Stop button, opening a
   camera and starting campy?
10. **Default pause after a stop:** until the next morning, or something else?

Once you OK the design, stage 1 is the first PR. Nothing gets installed on any rig without your OK for that rig.
