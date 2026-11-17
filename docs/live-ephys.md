# Live electrophysiology from SpikeGLX

*Being built. This page says what exists so far: the engine (`campy/ephyslive.py`) and `campy-ephys bench`, which measures what it costs. The campy-session panel's Ephys section, which will show it, comes next.*

A Neuropixels probe recorded by SpikeGLX can be watched live: a few channels scrolling like an oscilloscope, and its spikes, found by a threshold on every channel, as snippets, firing rates and a map of the probe. campy reads the probe's samples from SpikeGLX while SpikeGLX records them, and changes nothing in SpikeGLX.

It is built for live-sorted single units later. Spikes keep their waveforms in Kilosort 4's window, each spike has a unit (none yet), and a recording replayed through it gives exactly the spikes the live view showed. So a live sorter can be trained on a saved recording against its Kilosort 4 sort, curated in phy, and then run live.

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
5. **Keep each spike's waveform** on its 12 nearest sites (its own first) in Kilosort 4's window: 61 samples, the trough at sample 20.
6. **Give each spike its unit.** For now none: every spike is −1, a threshold crossing.
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

**Not measured yet:**
- what reading costs SpikeGLX, and how long a read takes from a running SpikeGLX: on a Windows computer with SpikeGLX's simulated probe playing a saved recording first, then on the ChronicRecord rig with no recording running;
- the filter and spike finder on the rig's own processor.

## Lining spikes up with camera frames

On the ChronicRecord rig the Pico's camera pulses are recorded by the OneBox (XD0), so live a spike's frame is:
1. its probe sample, turned into a OneBox sample with `mapSample`;
2. then the camera pulses counted up to it.

That is exact to the frame unless a camera dropped frames. The exact timeline stays the job of `chronicrecord/sync_match.py` and `campy-align` after the recording. This part comes with the panel's Ephys section.

## Next

- **The panel's Ephys section:**
  - the engine in its own process, at below-normal priority, only while the section is open;
  - a scrolling oscilloscope of a few channels;
  - spike snippets per channel, rates, and a heatmap of the probe;
  - per-channel thresholds;
  - each spike's camera frame.
- **A live sorter:** templates from a Kilosort 4 sort of an earlier recording of the same probe, curated in phy, matched to each spike's waveforms.
  - Trained on that recording replayed through this engine, so that the templates have the live filter's shape.
  - Checked against the Kilosort sort before it is used live.
  - It won't follow the probe's drift between sorts, and it finds only units the earlier sort found.
