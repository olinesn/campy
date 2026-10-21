# Calibration and troubleshooting

## Placing 3 cameras

Tested by running campy's 3D tracking on every possible 3 of the 8 cameras in two recorded sessions (5 mice; 3 fighting mice), all 56 triples each. The best layout was **one camera looking down (65–90° above the floor) and two oblique cameras 25–45° up**. The two obliques should be at least about 35° apart, and the three spread around the arena, over at least 100° (best 150–210°). Triples like that grouped a median of 99.75–99.92% of the mice correctly across cameras, with no identity switches.

- Keep every camera at least about 25° above the floor. Triples with a camera close to the floor, or one looking along a clear wall at a grazing angle, did worst.
- Three cameras all looking down from nearly the same direction left fewer mice seen by only two cameras, but doubled the 3D error.

## Before calibrating: lens and exposure

Calibration holds only as long as focus and zoom stay put, so set the lenses first.

1. **Point and focus** every camera on the arena.
2. **Set the brightness to match the pose model's training images** ([Models](models.md#recording-for-a-model)). For the models here that is an image mean of about 15–35 out of 255, with almost no saturated pixels. Close the aperture, shorten the exposure or lower the gain on any camera that is brighter.
3. **Check with animals in view**: on every camera, the number of animals found should equal the number in the arena, and should be zero when the arena is empty. A camera that is far too bright finds fewer animals and many false ones at fixed spots. A camera that finds nothing at all, with nothing wrong in the software, usually has nothing the model recognises in view.

## Calibrating

1. **Record a calibration video** with the calibration board moving through the arena. Any `videoFilename` containing "calibration" is treated as one, and goes into each camera's `calibration_images/` folder, where calibration tools look for it ([README](../README.md#stop-recording)). A slower, shorter run is enough:
   ```
   campy-acquire <config>.yaml --frameRate 10 --recTimeInSec 90 --videoFilename calibration.mp4
   ```
2. **Calibrate** with an anipose-compatible tool (sleap-anipose, or a tool that writes the same `calibration.toml`).
3. **Use it live**: `campy-live --calibration calibration.toml`. The cameras are matched by campy's camera names (`cameraNames`). If the calibration names them differently, give `--calibration-names`, in the feed's camera order.
4. **Check it** in `campy-live-view`. Each detection is joined by a line to its animal's 3D pose projected back into that camera, with the distance in pixels. Short lines on every camera mean a good calibration and good matching. To check a logged recording afterwards, `python bench/pose3d_live_log.py <campy-live log>.npy calibration.toml` scores the 3D against the best grouping the 2D allows.

## Troubleshooting

**campy stops with "The trigger Arduino … did not answer".** Before any camera starts, campy checks that the trigger Arduino responds. Unplug its USB cable and plug it back in (or power-cycle it), then run campy again. The Arduino sometimes stays busy with a command from a run that just stopped.

**A FLIR camera records at about 33 fps, or its throughput is 43 MB/s.** A camera that stays plugged in through a computer restart comes back with a USB 2 throughput limit. Shutting down and switching on clears it, and so does `campy-flir-reset` ([README](../README.md#flir-cameras-at-43-mbs-after-a-restart)).

**Frames dropped in the middle of a recording** (`framesDropped` in a camera's `metadata.csv`). The encoder fell behind by more than `writeQueueMaxSec`, usually because the disk stopped taking writes. Some SSDs stall for 10–16 s with nothing in the system logs, even with their power saving off. Raise `writeQueueMaxSec` (60 s for 3 cameras takes up to about 14 GB of memory, used only during a stall), or record to a second drive.

**A Windows recording slows to a crawl after about 10 minutes.** Windows went into Modern Standby when the screen timed out. campy's `keepAwake` (on by default) asks Windows to stay awake, but if campy was started over SSH with nobody signed in, Windows refuses the screen part. Set the screen timeout to Never.

**`campy-live` skips sets.** The model is too slow for this machine and frame rate. A few skipped in the first fraction of a second on CUDA are the first sets warming up. Skips throughout mean you need a lighter model ([Models](models.md#choosing-one-that-keeps-up)). On a Windows PC with an Intel hybrid CPU, see [Hardware](hardware.md) on pinning.

**`campy-live` misses the start of the recording.** Start it before `campy-acquire`, and wait for `Waiting for the live feed`. If the cameras' frames aren't 1280 × 1024 grey, give `--frame-shape` so the model is ready for them.

**3D poses are wrong, or mice swap between cameras.** Check the calibration first (lines in `campy-live-view`, above). If it is good, look at the 2D. False detections outside the cage, or a camera that often misses a mouse, pull wrong pairings into the 3D. A model trained on another rig does this more often, and retraining on the rig's own frames helps most.

**A camera fails to open with Spinnaker's "Could not read the XML URL [-1008]".** Seen once, on one of three cameras; campy then recorded the other two. It opened normally on the next run.

**Checking that cameras expose together.** `campy-synctest` measures it from a blinking LED that all cameras see ([README](../README.md#checking-that-cameras-expose-together)). `campy-align` puts every camera's frames on the trigger's timeline after recording.
