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

Problems and their fixes are in [Troubleshooting](troubleshooting.md).
