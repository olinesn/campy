# Live electrophysiology from SpikeGLX

*Being built. What exists so far: the engine (`campy/ephyslive.py`), `campy-ephys bench`, which measures what it costs, the campy-session panel's **Live ephys** card, which shows it ([below](#in-the-panel-the-live-ephys-card)), and a live sorter trained on a Kilosort 4 sort, which can also find spikes with the templates themselves, below the threshold ([below](#a-live-sorter-from-a-kilosort-4-sort)).*

A Neuropixels probe recorded by SpikeGLX can be watched live: a few channels scrolling like an oscilloscope, and its spikes, found by a threshold on every channel, as snippets, firing rates and a map of the probe. campy reads the probe's samples from SpikeGLX while SpikeGLX records them, and changes nothing in SpikeGLX.

It sorts spikes into units live, with templates trained on a Kilosort 4 sort of an earlier recording of the same probe (curated in phy or not). Spikes keep their waveforms in Kilosort 4's window, and a recording replayed through it gives exactly the spikes the live view showed, which is how the sorter is trained and checked.

## How SpikeGLX gives its samples

SpikeGLX's remote command server (Options > Command Server Settings > Enable; 127.0.0.1, port 4142), through its SDK ([SpikeGLX-CPP-SDK](https://github.com/billkarsh/SpikeGLX-CPP-SDK), the same one campy-session uses to start and stop SpikeGLX's runs):

- `fetch` gives a stream's samples from a sample number on, for chosen channels: a probe's AP or LF band, or a OneBox's channels. Reading on from where the last read ended misses no sample. SpikeGLX keeps the last few seconds; a reader that falls further behind than that loses samples, and is told so.
- `fetch` with stream type −2 gives SpikeGLX's own filtered AP band, if its filtered buffers are turned on (IM Setup tab). campy doesn't turn it on, and filters the samples itself unless told to read that buffer (`--spikeglx-filter`).
- `getGeomMap` says where each site is on the probe, `getStreamI16ToVolts` how many volts each count is, and `mapSample` turns a probe sample number into a OneBox one through SpikeGLX's sync.
- The API finds no spikes and sorts none: that is campy's part.

SpikeGLX's own example measures 2 to 3 ms from a signal at the probe to a reaction to it, fetching all 384 channels in a loop. A Neuropixels probe's AP band is 384 channels × 30,000 samples a second × 2 bytes = 23 MB a second, what SpikeGLX writes to disk (23.1 MB/s measured on a Neuropixels 2.0 probe).

## What campy does with them

Each piece of samples, as it arrives (`campy/ephyslive.py`):

1. **Read** them from SpikeGLX with read-only calls only. The reader can't call anything that would start, stop or change SpikeGLX: it is given only the calls that read. If it falls more than 1 s behind (the computer busy), it skips to the newest samples and counts what it skipped, rather than fall further behind.
2. **High-pass** each channel: a 3rd-order Butterworth at 300 Hz, Kilosort 4's. Kilosort runs its filter forwards and backwards, which a live filter can't, since it would need samples that haven't come yet. This one runs forwards only, so a spike's trough comes up to 3 samples (0.1 ms) earlier than Kilosort's, and its shape is a little different.
3. **Take the median across channels off each sample** (common median reference). Kilosort does this before its filter, after taking each channel's mean over a 2 s batch off. Live there is no batch: the median of unfiltered samples jumps by tens of µV from one sample to the next with each channel's own offset and slow swings, and the high-pass would pass that into every channel. So here it comes after the filter.
4. **Find spikes.** A spike is a sample below −4.5 × its channel's noise, and the lowest on the sites within 50 µm of it from 1.5 ms before to 0.5 ms after.
   - **The noise** is the channel's median absolute value / 0.6745, measured every half second and followed over about 10 s.
   - **Why 1.5 ms before.** Filtered forwards, a spike swings below zero again about 1 ms after its trough, a third as deep or more. A large unit's swing would count as a second spike.
   - **What it loses.** A smaller spike within 1.5 ms after a larger one nearby is lost; telling those apart is a sorter's work.
5. **Keep each spike's waveform** on its 12 nearest sites on its shank (its own first; up to about 60 µm away on a Neuropixels 1.0 or 2.0 probe), in Kilosort 4's window: 61 samples, the trough at sample 20.
6. **Give each spike its unit:** with templates from a Kilosort 4 sort ([below](#a-live-sorter-from-a-kilosort-4-sort)), the unit it matches; otherwise, or when it matches none, −1 (a threshold crossing).
7. **Keep what a view shows:**
   - each millisecond's lowest and highest value of a few channels, for the last 5 s;
   - the last 10 s of spikes;
   - each channel's and each unit's rate;
   - their latest 30 waveforms.

Channels are numbered as SpikeGLX numbers the probe's AP channels, as Kilosort's `channel_map` is. A spike's sample number is on the probe's stream (SpikeGLX's count since its run started). A saved file's first sample is its `.meta`'s `firstSample`.

**The same samples give the same spikes, however they come in pieces.** The filter carries its state from one piece to the next, the noise is measured on fixed blocks of samples, and a sample is judged only once all 61 samples of its window have come. The tests check this with pieces of 13 ms, 50 ms and 0.5 s.

## Measuring what it costs: `campy-ephys bench`

```bash
campy-ephys bench --sdk <the folder holding sglx_pkg> --seconds 120 --json bench.json
campy-ephys bench --replay <run>_g0_t0.imec0.ap.bin --speed 0
```

It reads the probe (from SpikeGLX, or from a saved `.ap.bin` and its `.meta`, as recorded or as fast as it can with `--speed 0`) through the filter and the spike finder. Once a second it prints a line:

- the samples read, and how long each read took;
- how much of a core the filter and the spike finder use;
- spikes a second, how far behind SpikeGLX it is, and the samples skipped;
- the CPU this program and SpikeGLX use.

It runs at below-normal priority unless told otherwise (`--priority normal`).

**Measured so far,** with no SpikeGLX: a 10 s synthetic Neuropixels 2.0 recording (384 channels at 30 kHz, Gaussian noise, slow swings, 24 units), replayed as fast as it could on an Apple M-series Mac.

| Step, per 50 ms of samples | Time |
|---|---|
| High-pass, 384 channels | 4.8 ms |
| Median off each sample | 4.9 ms |
| Finding spikes | 3.3 ms |
| All, with the conversions | about 15 ms: 31% of one core per second of samples |

On the synthetic recordings of the tests (4 units on 96 sites, 8 µV noise, 180 µV spikes), every spike planted after the first half second was found, within 4 samples and 40 µm. Beyond them it found only noise crossing the threshold, fewer than 0.3 a second per channel.

**A real recording:** Kilosort 4's sample data, 45 s from a Neuropixels 1.0 probe recorded by the International Brain Laboratory (public; the file of Kilosort 4's tutorial). It was replayed as fast as it could, in 50 ms pieces, on one core of a cluster node's CPU.

| | |
|---|---|
| Spikes found | 91,406: about 2,000 a second across the probe; 345 of the 384 sites more than once a second |
| Noise (median absolute value / 0.6745) | 13 µV median (11.7 to 14.9 µV, 10th to 90th percentile of sites) |
| A spike's trough | −70 µV median |
| Filter and spike finder | 57% of one core per second of samples (more spikes than the synthetic recording, and a slower core) |
| Samples skipped, gaps | none |

**Against Kilosort 4,** on the same 45 s. Kilosort 4.1.7 sorted the recording on a cluster GPU with its default settings: 281 units, 135 of them labelled "good" by Kilosort, 138,147 spikes. A Kilosort spike counts as found live when a live spike is within 10 samples of it, on a site within 60 µm of its unit's peak site. Recall is measured for each good unit; precision is the share of live spikes (after the first half second) near any Kilosort spike.

| | |
|---|---|
| Good units' spikes found live (median of the 135 units) | 85% (25th to 75th percentile: 59% to 97%) |
| Units with a trough deeper than −100 µV at their peak site (47) | 98% |
| −60 to −100 µV (48) | 80% |
| −40 to −60 µV (36) | 40% |
| Shallower than −40 µV (4) | 16% |
| Live spikes near some Kilosort spike | 70% |
| Live trough minus Kilosort's spike time | 0 samples median; −3 to +3 (5th to 95th percentile) |

A unit's trough here is the median of the live-filtered signal at its peak site around Kilosort's spike times. As expected of a threshold, recall falls for units near the threshold (−4.5 × the 13 µV noise, about −58 µV), where Kilosort's template matching still finds them. These are the numbers a live sorter has to improve on: matching templates finds spikes below the threshold, and tells apart the 30% of live spikes that Kilosort doesn't place on a unit near them (noise crossings, or units Kilosort didn't keep). The same comparison is the yardstick for accuracy before any speed work.

**From a running SpikeGLX (on a test VM):** SpikeGLX v20260826 (API 4.1.3), the release on the ChronicRecord rig, with no probe attached. It ran a simulated probe (below) playing the same sample recording, labelled as a Neuropixels 2.0 four-shank probe (NP2013, the rig's). The computer was a Windows 11 virtual machine with 8 cores of an Intel Core i9-14900. Numbers on the VM vary with the other VMs on its server, so take them as a first look, not the rig's numbers. 40 s of reading, in a `rig.py` job that came out clean (`RIG_CLEAN`).

| | |
|---|---|
| The live spikes against a replay of the same samples from the file | the same 55,997 spikes, none more on either side |
| One read: 50 to 60 ms of all 384 channels | 2.7 ms on average; `fetchLatest` of 50 ms alone took 1.8 ms median, 2.8 ms at the 95th percentile, 26 ms at most (4 channels: 0.9 ms) |
| Reads | about 16 a second (at most one every 50 ms); none skipped, no gap; at most 15 ms behind SpikeGLX |
| SpikeGLX's CPU | none while it ran no run; 14.5% of one core running the simulated probe (not writing files); 24.8% while campy read it, so about 10% of a core for the reading |
| campy's reader (reading, filtering, finding spikes) | 43% of one core |

Reading as soon as samples come (about 400 small reads a second) took SpikeGLX about 14% of a core and the reader 62%. So the reader asks at most every 50 ms.

**How the test ran a simulated probe,** with no hardware, by its settings files in SpikeGLX's `_Configs` folder (SpikeGLX's help: Simulated Probes):
- `imsimprobes.ini`: `[SIMPROBES]` `naddr=1`, `addr0=40, 1, 1, 1, <the file's path without .ap.bin>`;
- `improbetable.ini`: rows for slot 40 (a simulated slot), port 1 enabled;
- `mainapp.ini`: `[CmdSrv]` `enabled=true` on 127.0.0.1, port 4142;
- then `selectDevices("(40,1,1)")` and `startRun` through the API.

SpikeGLX takes the probe's part number from the file's `.meta`, so the `.meta` of a real recording with its size, length and checksum changed is enough.

**On the ChronicRecord rig** (Dell Pro Max Slim, Intel Core Ultra 9 285), 17 November 2026: SpikeGLX v20260826 running a run by hand with its real Neuropixels 2.0 probe (NP2013) and OneBox. Its trigger was on TTL with the gate low, so nothing was written. campy read it for 2 minutes, read-only, in a `rig.py` job that came out clean (`RIG_CLEAN`).

| | |
|---|---|
| Read | 118 s of all 384 channels at 30 kHz; no gap, nothing skipped, at most 35 ms behind SpikeGLX |
| One read (about 67 ms of samples) | 5.7 ms on average |
| SpikeGLX's CPU | 17.1% of one core running with no reader; 22.0% while campy read it, so about 5% of a core for the reading |
| campy's reader (reading, filtering, finding spikes) | 38% of one core |
| Written to disk | nothing: SpikeGLX was not saving before or after, and its data folder stayed empty |

**With everything running** (17 November 2026, the ChronicRecord rig): a 15-minute gate test (3 bouts, 4 cameras at 60 fps, the Pico, SpikeGLX recording its bouts) from the panel's *ChronicRecord session + live 3D (test)* experiment, which adds campy-live 3D tracking to the rig's session (3 animals, a 4-camera calibration) and draws the camera view with its 3D tile. The Live ephys card was open throughout.

| | |
|---|---|
| Cameras | 15,600 frames each: every one of the Pico's pulses (the Pico: 0 late, 0 lost, 0 stalls) |
| campy-live | every one of the 15,600 frame sets tracked, 0 skipped, 0 misaligned: 60 a second through each bout; frame to 3D 9.0 ms median, 11.4 ms at the 99th percentile, 13.1 ms at most (inference 5.3 ms) |
| The camera view | drew every result (about 60 a second); before, the session's view drew 15 frames a second with no 3D |
| The Live ephys card | 16 reads a second, 15 ms behind SpikeGLX, its reader 45% of one core; the camera frame it showed matched the view's |

Over Remote Desktop the view and the card show at most Remote Desktop's own frame rate (usually 30 a second).

## In the panel: the Live ephys card

With a `spikeglx:` section in its config (the ChronicRecord rig's), the campy-session panel's page has a **Live ephys** card. An experiment whose `spikeglx.devices` names only OneBoxes, such as the ChronicRecord rig's photometry-only one, has none: there is no probe for it to read. **Show the probe live** opens it:

- an oscilloscope of the picked site and its 3 nearest, the last 2 s (each millisecond's lowest and highest);
- the last 10 s of spikes, by depth on the probe;
- a probe map of each site's spikes a second; a click picks a site;
- the picked site's last 30 spikes, their mean and its threshold, and the threshold to set, for every site or the picked one (2 to 20 × noise);
- the camera's frame, and a tick for each frame along the oscilloscope's top ([below](#each-spikes-camera-frame)).

**How it runs:**
- **A process of its own.** Showing the card starts `campy-ephys serve` at below-normal priority: the engine above, answering on a port of its own on 127.0.0.1, only with a key the panel gives it.
- **The panel passes the page's requests on to it** (`/ephys.json`, `/ephys-scope`, `/ephys-threshold`), with the page's own key and its checks.
- **It reads only.** It opens its own connection to SpikeGLX and uses only the read calls, so it can't start, stop or change anything, nor get in the way of the panel's own steps with SpikeGLX.
- **It stops** when **Hide** is pressed, when the page hasn't asked for 30 s (its tab closed), or when the panel ends. On Windows it is in the panel's job object, so it ends with it whatever happens.
- **The page asks 10 times a second** for what came since its last ask: the scope's new bins, the new spikes, the rates, and the picked site's snippets; every 2 s also the probe's sites, noise and thresholds. That is a few kB to some tens of kB a second. Nothing else is fetched: the page works on a rig with no internet.
- **The traces scroll smoothly.** The oscilloscope and the spikes are drawn up to 30 times a second, at a time that moves at the samples' own rate, 0.3 s behind the newest sample, so there is always something to scroll into. (Drawn only when they came in, they jumped in 200 ms steps.) The probe map and the snippets are drawn with each answer. A tab out of sight draws nothing.
- **What the status line counts:** samples the reader skipped because it fell more than 1 s behind, and, apart from those, samples SpikeGLX didn't have when they were asked for: as its run starts it gives a sample count a few seconds before those samples can be read.
- **Before SpikeGLX runs** (or between runs) the card says it is waiting. A new run starts the view again.

Checked in the tests (`tests/test_panel.py`) with a stand-in SpikeGLX: the card's data comes through the panel, the scope and threshold reach the process, only read calls reach SpikeGLX, and the process ends with **Hide** and when the page stops asking.

## Each spike's camera frame

On the ChronicRecord rig the OneBox records the Pico's camera pulses (its XD0 word, bit 2), on the same clock as its other channels, and SpikeGLX keeps the probe and the OneBox in step with its own sync. So with each read of the probe, the reader also reads the OneBox's XD0 channel alone (read calls only) and:

1. **Finds the camera pulses.** A pulse after more than 1 s with none is a bout's first.
2. **Numbers them.** A bout's first pulse is frame *n*, where *n* is the Pico's count of camera pulses so far: `pulses` in `chronic_control.py`'s `status.json`, which counts every pulse of the bouts already ended. Each pulse after it in the bout is one more. That is campy's frame number, since each camera takes one frame a pulse from the recording's first.
3. **Gives each spike its frame:** the last pulse's before it, within a frame period, or none (−1) between bouts. A probe sample becomes a OneBox sample through SpikeGLX's own sync (`mapSample`, once a read).

What this means:
- **A bout already under way** when the card was opened has no frame numbers. They come from the next bout's first pulse.
- **The Live ephys card** shows the current frame and bout in its status line, and a tick for each frame along the top of its oscilloscope.
- **The panel tells the reader where to look:** the OneBox is `spikeglx.stream`'s (the one with the gate's TTL), and the Pico's `status.json` is the `pico:` section's.
- **It costs** one more read (one channel) and one `mapSample` call each read.
- **It's approximate:** a camera that drops a frame is one off from then on, live. The recording's exact timeline stays `campy-align`'s and `chronicrecord/sync_match.py`'s.

Checked in the tests with the stand-in SpikeGLX's OneBox (camera pulses in bouts) and a Pico `status.json`:
- every spike's frame equals the frame worked out from the pulses' plan;
- a bout under way at the start gets none, and spikes between bouts get none;
- the panel passes the OneBox and the Pico's status on.

Not yet tried with the rig's own OneBox and Pico.

## A live sorter from a Kilosort 4 sort

`campy/ephyssort.py`. Templates are trained on a recording of the same probe that Kilosort 4 sorted, then each live spike is matched to them.

```bash
campy-ephys train --replay <run>_g0_t0.imec0.ap.bin --phy <Kilosort's output folder> --out templates.npz [--span 0:600]
campy-ephys evaluate --replay <run>_g0_t0.imec0.ap.bin --phy <Kilosort's output folder> --templates templates.npz --span 600:900 [--match learned]
```

**Training.** The recording is replayed through the live filter and spike finder, exactly as live.
1. **Labels:** each live spike near a spike of a kept unit (within 10 samples, on a site within 60 µm of the unit's peak site) takes that unit. The kept units are those labelled `good` or `mua`: phy's labels if the sort was curated (`cluster_group.tsv`), else Kilosort's (`cluster_KSLabel.tsv`); `--labels` changes them. Units labelled `noise` in a curated sort get templates too, but only to compete for spikes: what they match stays unsorted, and the card doesn't list them (`--compete`).
2. **Templates:** each unit's template is the median of its spikes' waveforms at Kilosort's spike times, on its 32 nearest sites (a spike's whole footprint), as the live filter shapes them. Kilosort's own templates are whitened and filtered forwards and backwards, so they are not what the live view sees. A threshold's spike is compared on the 12 nearest of them, the sites a spike keeps.
3. **Acceptance:** each unit gets how far its own spikes fall from its template: the 99th percentile of their distances (`--accept-q`).
4. **For matching** ([below](#spikes-found-by-the-templates-below-the-threshold)): the whitening, the templates again in the whitened samples, and each unit's threshold, learned by replaying the training span once more with the templates matched. This replay takes longer than the first: with numba, 1.4 to 1.7 s per second of recording on one core of a cluster node (7 minutes for a 240 s span of a 384-channel probe with 437 templates). `--no-whiten` and `--no-learn` leave these out. The units are saved in their order on the probe, which the matcher's work goes by.

**Sorting live.** Each spike is compared with the units whose sites include its channel, on the sites they share. The template is scaled to the spike (0.5 to 2 times), and the distance is the residual per sample, divided by each site's noise. The spike takes the closest unit if it is within that unit's acceptance, and stays unsorted (−1) otherwise.

**Using it on the card:** give the config's `spikeglx:` section `templates: <the .npz>`. The card then colours each spike by its unit, lists the busiest units with their rates (a click shows a unit's last 30 spikes), and says what share of spikes it sorted. A `mua` unit is listed as such.

**Matching on the card:** add `match: learned` to the same section, and the card's reader finds spikes with the templates too, below the threshold, each unit at the threshold learned in training ([below](#spikes-found-by-the-templates-below-the-threshold)). A number (`match: 8`) sets one threshold for every unit instead. It's off by default (`match: 0`, or left out), and needs `templates:`; a value the panel can't use stops the config when it opens, with what it takes.

```yaml
spikeglx:
  templates: D:/sorts/<probe>/templates.npz
  match: learned
```

The status line then says how the matcher keeps up: how busy its thread was in the last second, how far behind it is, and how many seconds of samples it left unmatched (its threshold spikes went out unsorted then).

### Templates from a recording session: `campy-ephys templates`

`campy/ephystemplates.py` does the whole of it from a session's folder, on the PC that recorded it:

```bash
campy-ephys templates --session <recording folder> --out <folder> --kilosort-python <Kilosort 4's python>
```

1. **The probe's bouts, joined.** A session on the ChronicRecord rig has one SpikeGLX file per bout (the Pico's gate: `<run>_g0_t0`, `_t1`, ...). They are joined one after the other, in SpikeGLX's order, into `<probe>.joined.ap.bin`, with its `.meta` (the first bout's, with the joined file's size and length) and `.bouts.csv` (where each bout starts in it). By default it takes the session's first 20 minutes (`--minutes`; 0: all of it). A bout SpikeGLX didn't finish is left out, and the joining stops at a bout whose probe settings (its sites, `imroTbl`) differ from the first's. Each join is a step in the signal, so a few spikes may be found at each one, by Kilosort and live alike.
2. **Kilosort 4,** in an environment of its own (`--kilosort-python`), at low priority, with its defaults and the probe's layout from the `.meta` (its sites, across its shanks). Its output, phy's files, goes in `kilosort4/`.
3. **Training** (`campy-ephys train`, [above](#a-live-sorter-from-a-kilosort-4-sort)) on the joined file's last 240 s (`--span`): the newest, nearest the sessions that follow. The span is held in memory, about 46 MB a second of a 384-channel probe.

It writes only in `--out`: the joined file, `kilosort4/`, `templates.npz` and `templates.json` (what it did), then prints the lines for the panel's config. A step whose output is already there is skipped, so a run that stopped goes on from where it was; delete `--out` to start again. Run it when nothing records: Kilosort holds the GPU for minutes. To curate the units first, open `kilosort4/` in phy, then run `campy-ephys train` on the joined file, as above.

**Kilosort 4 on the ChronicRecord PC:** a conda environment of its own, `kilosort4` (Python 3.11, Kilosort 4.1.7, torch 2.11.0 with CUDA 12.8, the build campy-live's environment runs on its RTX 2000 Ada). Installed on 18 November, under the rig lock.

**Run on that PC (18 November), on a session recorded with its probe on a bench, not implanted:** 4 bouts (2.5 s, 257 s, 27 s and 27 s) of its Neuropixels 2.0 four-shank probe (384 channels), under the rig lock, with nothing recording:

| Step | Time | Note |
|---|---|---|
| Joining the 4 bouts (7.2 GB, 313 s) | 26 s | |
| Kilosort 4 | 5.8 min | 17.9 GB of memory, 2.6 GB on the GPU; drift correction and spike detection take 80% of it |
| Training on the last 240 s | 6.2 min | the replay 1.6 min, learning the thresholds 3.9 min |
| All of it | 12.2 min | |

Kilosort's time grows with the minutes joined (20 by default); the training's doesn't (its 240 s).

- **A probe that isn't in a brain still gives "units".** Kilosort found 119 in the bench recording (29 it labelled good, 90 mua; 237,047 spikes, about 760 a second), and the training made templates for 117 of them. They are noise and interference, not neurons. So make templates only from sessions with the probe implanted, and look at Kilosort's units in phy before training (`campy-ephys train --phy` on the curated folder): Kilosort's "good" label alone doesn't tell a neuron from noise.

**Checked on Kilosort 4's sample recording** (45 s, Neuropixels 1.0; Kilosort 4.1.7, its labels, not curated). Trained on the first 30 s, evaluated on the last 15 s against Kilosort's sort there, for its good units:

| | Templates for good units only | Good and mua units (the default) |
|---|---|---|
| Units with a template | 129 of 135 good | 275 of 281 (132 good ones reported) |
| A unit's Kilosort spikes found live and given to it (median per unit) | 68% (25th to 75th percentile: 31% to 91%) | 58% (26% to 86%) |
| The spikes given to a unit that are its (median per unit) | 61% (39% to 89%) | 86% (59% to 96%) |
| Live spikes of a good unit given that unit | 91% | 86% (4% left unsorted) |
| Live spikes of no good unit given one anyway | 42% | 10% |
| Time to sort, per spike (one core of a cluster node) | 151 µs | 303 µs |

The first column was measured when templates came from the live spikes Kilosort's spikes matched; they now come from Kilosort's spike times, so units that rarely cross the threshold have one too (the second column).

So the mua units' templates are what keeps their spikes, and noise crossings, from being taken for good units. With the 95th percentile as acceptance instead of the 99th, the spikes given to a unit that are its rose to 89%, and the share of its spikes found and given to it fell to 59%. On synthetic units in the tests (pairs on nearly the same sites, told apart by shape or footprint), nearly every spike the spike finder found went to its unit.

**What limits it:**
- **What the spike finder misses.** It finds 85% of good units' spikes (above), and few of the units near its threshold, where Kilosort's template matching still finds them: the lower quartile of units is under 30%. Matching the templates finds them ([below](#spikes-found-by-the-templates-below-the-threshold)).
- **No drift correction.** Train again on a recent recording when the probe has moved.
- **Only the sort's units.** Units the sort didn't keep stay unsorted.
- **Collisions.** Two spikes within 1.5 ms of each other nearby are one live spike, sorted as the bigger.
- **Speed.** At about 280 µs a spike, 2,000 spikes a second would take about half a core. Speed work comes after accuracy (below).

### Spikes found by the templates, below the threshold

With `--match learned` (`campy-ephys evaluate` and `campy-ephys serve`), the templates find spikes themselves, as Kilosort 4 does, so a unit whose trough rarely crosses the threshold is still found. `--match` with a number sets one threshold for every unit instead.

- **The samples are whitened as Kilosort 4 whitens them** (for matching only: the card's waveforms stay as filtered). Each channel is whitened with the covariance of its 32 nearest sites, measured on the training span. Activity that spreads across many sites, such as neurons further from the probe, then no longer looks like a template's spike.
- **Matching.** Each unit's template is slid along the whitened samples (a matched filter, in the frequency domain). Its score at each sample, in noise units (z), says how much of the data the template explains there.
- **Taking spikes away.** The highest score above its unit's threshold is a spike. Its template, at its fitted size, is taken away, and the rest is looked at again, up to 3 times. Nearby units that spike within 0.67 ms of each other are fitted together, up to three, so that one unit's template doesn't take in the others' spikes. Taking a spike away updates the scores through the templates' cross-correlations, as Kilosort does, so nothing is matched twice.
- **Each unit's threshold is learned in training.** The training span is replayed with the templates matched from z 6. Each unit's template spikes are paired one to one with its Kilosort spikes (within 10 samples), and its threshold is the one where F1 is highest: F1 counts both the share of its Kilosort spikes found and the share of its template spikes that are its. One threshold for every unit doesn't work: on a real recording, z 7 gave out three times Kilosort's spike count, mostly to small units.
- **The threshold's spikes** that no template spike explains (within 10 samples, on its sites) stay as unsorted crossings.
- **The same spikes however they come.** A sample is judged once all it needs has come, so a recording replayed in 50 ms pieces or 1 s pieces gives the same spikes.
- **How it's fast enough** (the same spikes as the first, slower version, on both recordings below):
  - **Scores:** each template is 8 shapes in time with a weight on each of its sites. For a group of about 12 neighbouring units at once, their weights times their channels' transforms is one matrix product, done in whole 2,048-sample blocks.
  - **Candidates:** only samples above their unit's threshold are looked at, and the search for another unit's spike at once is skipped where no unit near it peaks.
  - **The judging loop** (look again, fit, take away) is compiled with numba when numba is installed (`pip install numba`, or campy's `fast3d` extra). It gives numpy's spikes, sizes and scores to the last bit (the tests hold it to that), and `CAMPY_NUMBA=0` turns it off.
  - **Threads:** `--threads N` works the scores out in N threads (the same spikes). Four saved only about a fifth of the scores' time on a cluster node: each group's product is small.
- **Live (`campy-ephys serve`, the card), the matcher runs in a thread of its own** beside the reader, so the two use two cores: its matrix products, transforms and numba's loop let the reader's thread run meanwhile. Spikes reach the view in order, a little later (as they already did, once both finders had judged them).
  - **It never holds the reader up.** A piece that would put the matcher more than 2 s behind goes out with its threshold spikes unsorted, and the matcher starts again after the gap.
  - **The card's health** (`matcher`, and `matcher_busy`: the share of the last second its thread spent matching) says how far behind it is, how busy, and how many pieces it skipped. The card's status line shows them.

**Checked against Kilosort 4** on two recordings (each probe 384 channels), trained on one span and tested on a later one:
- Kilosort 4's sample recording: 45 s, Kilosort's own labels;
- a 63-minute recording sorted by Kilosort 4 and curated in phy (194 good, 61 mua and 282 noise clusters).

For good units, each unit's Kilosort spikes and the spikes given to it are paired one to one; the table gives the median per unit, with the 25th to 75th percentile in brackets:

| | Kilosort's sample (trained 0–30 s, tested 30–45 s) | Curated recording (trained 60–300 s, tested 700–760 s) |
|---|---|---|
| **Threshold's spikes, sorted:** found / right | 58% (26–86%) / 87% (65–98%) | 17% (5–41%) / 32% (7–75%) |
| **Matched, z 7 for every unit:** found / right | 94% / 77% | 82% / 12% |
| **Matched, each unit's threshold, not whitened:** found / right | 87% / 89% | 55% / 45% |
| **Matched, each unit's threshold, whitened (the default):** found / right | **95% (86–99%) / 96% (89–100%)** | **75% (45–92%) / 80% (56–95%)** |
| Spikes given to good units a second, whitened (Kilosort's) | 1,362 (1,389) | 2,008 (1,931) |
| One core of a cluster node, per second of recording, whitened, with numba (without) | 0.59 s (0.87 s) | 0.71 s |
| One core of the ChronicRecord PC (Intel Core Ultra 9 285), with numba (without) | 0.49 s (0.70 s) | not timed |

"Found" is the share of a unit's Kilosort spikes found and given to it; "right" is the share of the spikes given to it that are its.

The curated recording is the harder one: 38% of its good units are at 80% or more on both, against 80% of those in Kilosort's sample. Small units do worst there: of the units whose trough is under 40 µV, 11% are; of those over 100 µV, 53%. Before whitening, most of the matcher's wrong spikes there fell where Kilosort had no spike nearby, and a fifth fell on another good unit's spike nearby. That hasn't been measured again since whitening.

**What limits it:**
- **Speed.** Matching takes 0.6 to 0.7 s of one core of a cluster node per second of a 384-channel probe with numba, and about 0.9 s without. On the ChronicRecord PC it took 0.49 s with numba and 0.69 s in the panel's own environment, which had no numba (18 November, Kilosort's sample replayed as fast as it could be, under the rig lock, the same spikes every way). numba went into the panel's environment that night (numba 0.68.1 and llvmlite 0.50.0; pip changed nothing else, numpy stayed 2.5.4), and the card's matcher there then took 0.48 s, with the same 54,413 sorted spikes and the same accuracy.
- **Live, at the recording's own pace** (the same PC and recording, 20 s replayed as SpikeGLX would give it, under the rig lock):

| | Reader's thread | Matcher's thread | Behind at most | Pieces skipped |
|---|---|---|---|---|
| Matcher in the reader's thread, numba | 88% of a core | (the same) | none | none |
| Matcher in its own thread, numba | 37% | 53% | 0.06 s | 0 |
| Matcher in its own thread, the panel's environment (no numba) | 41% | 80% | 0.25 s | 0 |
| Matcher in its own thread, the panel's environment with numba (since 18 November, 9:58 PM) | 37% | 52% | 0.06 s | 0 |

  In its own thread the matcher keeps up even without numba; with numba, which the panel's environment now has, it is busy about half the time. On a cluster node's slower cores, in the reader's thread it couldn't keep up (20 s of samples took 29 s), and without numba it fell 2 s behind and skipped a fifth of the pieces.
- **Not tried on the card yet:** the panel passes `--match` with `spikeglx.match`, but the ChronicRecord probe has no templates from a sort of its own recordings yet.
- **Harder recordings.** On the curated recording, a quarter of good units have under 45% of their Kilosort spikes found, and a quarter have under 56% of what they're given right.
- **No drift correction**, as above.

## Next

- **A better sorter:** following the probe's drift.
- **Matching on the card, on the ChronicRecord PC:** once its probe is implanted, `campy-ephys templates` on its first session, then `match: learned` tried during the next; then how many probes one PC can match live at once. Until then its probe is on a bench, and its recordings have no units to make templates from.
- **Accuracy first, then speed.** Speed work waits until the live output matches an automated sort made after the recording (Kilosort 4) in precision and accuracy. After that come tests of how many probes one PC can follow live at once, and only then speedups.
- **Later, on top of live-sorted units:** each neuron's activity plotted live against the population's low-dimensional space and against the behaviour campy-live tracks: keypoint-MoSeq syllables, a PCA or UMAP space of the poses, or another map that describes the behaviour better.
