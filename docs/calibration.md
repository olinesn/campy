# Calibration

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

1. **Record a calibration video** with the calibration board moving through the arena. Any `videoFilename` containing "calibration" is treated as one, and goes into each camera's `calibration_images/` folder, where calibration tools look for it ([Recording with campy-acquire](acquire.md#stop-recording)). A slower, shorter run is enough:
   ```
   campy-acquire <config>.yaml --frameRate 10 --recTimeInSec 90 --videoFilename calibration.mp4
   ```
2. **Calibrate** with an anipose-compatible tool (sleap-anipose, or a tool that writes the same `calibration.toml`). With sleap-anipose and the lab's board (calib.io's coarse 300 × 200 mm ChArUco: 14 × 9 squares of 20 mm, 15 mm markers, `DICT_5X5_1000`; see [What to buy](hardware.md#what-to-buy)), first write the board's file (and a picture of the board), then calibrate in the recording's folder:
   ```
   slap-draw_board --board_name charuco_14x9x5.jpg --board_x 14 --board_y 9 --square_length 20 --marker_length 15 --marker_bits 5 --dict_size 1000 --img_width 1440 --img_height 1440 --save charuco_14x9x5.toml
   slap-calibrate --session . --board charuco_14x9x5.toml --calib_fname calibration.toml --metadata_fname calibration_metadata.h5 --histogram_path reprojection_histogram.png --reproj_path .
   ```
3. **Use it live**: `campy-live --calibration calibration.toml`. The cameras are matched by campy's camera names (`cameraNames`). If the calibration names them differently, give `--calibration-names`, in the feed's camera order.
4. **Check it** in `campy-live-view`. Each detection is joined by a line to its animal's 3D pose projected back into that camera, with the distance in pixels. Short lines on every camera mean a good calibration and good matching. To check a logged recording afterwards, `python bench/pose3d_live_log.py <campy-live log>.npy calibration.toml` scores the 3D against the best grouping the 2D allows.
5. **Draw the cage** in `campy-live-view` (`k`): its floor's corners clicked in 2 or more cameras. It is kept beside the calibration (`calibration.cage.json`), and campy-live sets aside any 3D outside it ([The cage](live-tracking.md#the-cage-3d-outside-it-is-not-found---cage)). After calibrating again, draw it again: a cage made for another calibration is not used.

## A calibration every day, made by itself (calibrat3)

Cameras get bumped. On 9 November 2026 the Dell Pro Tower Plus's 8-camera box was tracked live with a calibration from July. The overall reprojection error looked fine, about 7 px, but one camera was 75 px off and another 18 px: they had moved since. The overall figure, averaged over 8 cameras, hid both. So that box now records a calibration each day, and campy makes the `calibration.toml` from it by itself, with [calibrat3](https://github.com/talmolab/calibrat3) running headless in the computer's own Chrome.

1. **A calibration experiment** on the recording panel: a config with the rig's cameras and trigger, `videoFilename: "calibration.mp4"`, `frameRate: 10`, `recTimeInSec: 60`, its own `videoFolder` (for example `C:\campy_data\calibrations`), and the `calibrate` step on:
   ```yaml
   post:
     calibrate:
       calibrat3: 'C:\campy_data\live\calibrat3'    # a calibrat3 checkout (the folder with its index.html)
       # the defaults: the lab's board, 300 frames sampled, the installed Chrome
       # board: {board_x: 14, board_y: 9, square_length: 20, marker_length: 15, marker_bits: 5, dict_size: 1000}
       # target: 300
       # browser: chrome
   ```
   Start it, and move the board through the whole arena for the 60 s, turning and tilting it, so every camera sees it from many angles.
2. **After the recording**, the `calibrate` step (after `remux` and `align`) runs `campy-calibrate` on the folder. It opens the folder in calibrat3, as you would on its page, and does what you would do there: finds the board in 300 sampled frames, then each camera's intrinsics, the cameras' positions and the bundle adjustment. It writes into the folder `calibration.toml`, `board.toml`, `calibration_summary.json` (each camera's reprojection error, median and p95 in pixels, and the frames where it saw the board) and `logs/calibrate.log`. The panel shows the result under **Steps after the recording**, for example `Calibration (calibrat3): calibration.toml for 8 cameras, by calibrat3: reprojection median 0.08 px, worst Camera2_mid 0.09 px (40 s)`. A camera calibrat3 couldn't place makes the step fail, naming it, so a calibration never silently leaves out a camera. By hand: `campy-calibrate <folder> --calibrat3 <checkout>` (`--headful` shows the page while it works).
3. **The live configs** name the folder instead of a file, `live: calibration: 'C:\campy_data\calibrations'`: each recording then uses the newest calibration there that `campy-calibrate` made whole. The panel's checks say which one, and its cameras' errors, and turn yellow when it is more than a day old (`Record a calibration today`). A folder with none stops the start. A new calibration has no cage: draw it again (`k`) for the first live recording after it.

It needs Playwright's Python package in campy's environment (`pip install playwright`; it drives the installed Chrome, so no browser download) and a calibrat3 checkout. Chrome, not Playwright's own Chromium, because it decodes the recordings' H.265 and AV1. On the Mac, on the 4-camera calibration recording of 26 October (60 s at 10 fps, H.265), it took 37 s and gave the same calibration as calibrat3 run by hand: the board in 281/277/277/277 of 300 frames, each camera's error 0.07–0.09 px, and the same focal lengths and distances between cameras, to 0.1 mm.

Problems and their fixes are in [Troubleshooting](troubleshooting.md).
