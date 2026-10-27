# 3D poses from live 2D predictions (campy/pose3d.py)

How campy-live turns each set of 2D predictions into 3D poses with identities, how it differs from LUC3D's cross-view tracker, and how it was validated.

`campy/pose3d.py` turns one synchronized set of 2D top-down predictions (one per camera) into 3D poses with identities: it decides which detection in each camera is the same animal, triangulates each animal's nodes, and keeps each animal's identity from one set to the next. It needs only numpy and scipy, so it runs inside the live inference process, once per set:
```
from campy import pose3d
cameras = pose3d.LoadCalibration("calibration.toml", names=["Camera0_mid", "Camera1_topB", "Camera2_topC"])
tracker = pose3d.Tracker(cameras, n_animals=5)
poses = tracker.Update(keypoints, scores, frame=n)    # once per set, in order
```
- In: `keypoints` (cameras, instances, nodes, 2), pixels in each camera's own image as the model gives them, NaN for a missing node (a row of NaN is no detection, so padding is fine); `scores` (cameras, instances, nodes) or None; `frame`, the set's frame number (sets may be skipped).
- Out: `poses.points3d` (n_animals, nodes, 3) in the calibration's units (mm), one row per identity, NaN where the animal was seen by fewer than 2 cameras this set; `poses.members` (n_animals, cameras), the instance used in each camera or -1; `poses.n_views`; `poses.reproj_px`, each node's mean reprojection error.

The matching is LUC3D's cross-view tracker (Track All): camera by camera, a Hungarian assignment of the animals (each with its 3D from the sets before) to that camera's detections, scored per node by the distance from the animal's 3D to the detection's rays and to its projection; new animals from leftover detections of two cameras, by epipolar distance, until there are `n_animals`. Then each animal is triangulated (DLT on undistorted points) from this set's detections only. A node whose cameras disagree by more than 50 px (`reproj_threshold`) loses the camera that disagrees, or is left out of the 3D when only 2 cameras saw it, so one camera's misplaced node or false detection does not pull the mouse's 3D off; `reproj_threshold=0` keeps every view, as LUC3D. `Tracker`'s docstring lists the other options (score-weighted DLT, minimum nodes or node score).

It differs from LUC3D in `stale` (60 frames, not 20) and in three ways that keep a misplaced node from deciding a match, which a model trained on another rig makes often: a node more than about a mouse's length (125 mm) from a detection counts against it the same however far it is (`max_penalty`; LUC3D's penalty has no floor, so one node that triangulated metres away outweighed all the others); an animal's own 3D, which the next set is matched against, keeps only the nodes whose cameras agree within 20 px (`match_reproj`); and new animals are paired by the median epipolar distance of their nodes, not the mean. `max_penalty=None, match_reproj=0` gives LUC3D's scoring back. What they changed:
- **Dell Pro Max Slim FCS1250, live** (3 cameras, 3 white mice, a model trained on another rig; 120 s, 3595 sets): with LUC3D's scoring, campy-live grouped 56% of the mice right across the cameras, and re-triangulating its groups gave a median reprojection error of 42 px (p90 208); with these changes, replayed on the same log, 97% and 5.2 px (p90 199: that tail is the 2D's own, the best grouping has it too). Judged against the best grouping each set's own 2D allows, on the mice it groups cleanly (every camera within 15 px), by `bench/pose3d_live_log.py`.
- **22 proofread sessions** (the SLAP rig, 2–4 mice, 3 of its 8 cameras at a time: 66 runs of ~18,000 sets, scored against the hand-corrected tracks by `bench/pose3d_proofread.py`): detections with the right identity, mean 86.4% → 93.7% (runs at 98% or more: 33 → 48 of 66); groups of one animal 99.66% → 99.90%; lasting identity switches 274 → 111. 30 runs are better by more than a point and 3 worse, all three already poor (55–69% after), in sessions where LUC3D's own Track All also swaps mice in close contacts.

## The cage

`Tracker(cameras, n_animals, cage=pose3d.Cage.Load(path))` (or `tracker.cage = ...` between sets) sets aside any animal whose 3D lands outside the cage. A `Cage` is the cage floor's corners in the calibration's world, the up direction (towards the cameras), a height and a margin (30 mm). `campy-live-view` makes it from the floor's corners clicked in 2 or more cameras: each corner is triangulated, and up is the normal of their plane on the cameras' side. An animal is outside when its middle (the median of its nodes) is more than the margin past a wall, below the floor or above the top. Then that set it has no 3D (NaN), its detections are not grouped (`members` -1, `n_views` 0) and `poses.outside` is True for it. A pair of detections whose 3D is outside starts no identity. A target whose own 3D (what the next set is matched against) is outside keeps the 3D it had. A node outside, of an animal inside, is dropped. For a convex floor, as a box's, the test is one matrix product for every point (11 µs for 85 points on an M-series Mac). At a corner it is a square's corner rather than a round one, so up to about 12 mm more lenient there. Animals inside the cage are tracked exactly as without it (a test checks 120 sets of 5 walking animals).

Why it is needed: when one camera's detection is grouped with another animal's in the other cameras, the rays can still meet within the reprojection threshold, far along the cameras' line of sight. With every camera on one side of the cage, as on the Dell Pro Max Slim FCS1250's 4-camera rig, that place is below the floor or above the cage, often by tens of centimetres, sometimes by metres. A cage drawn in each camera's image (a 2D outline per camera) would not catch it: replayed on the same recordings, outlines around where the animals went caught only 21–41% of the positions more than 25 cm from the middle, because those positions project inside every camera's outline.

Replayed (`bench/pose3d_cage_replay.py`) on six recordings from the Dell Pro Max Slim FCS1250's 4 cameras, 26 October 2026, 10 minutes each at 30 fps, with the cage clicked in `campy-live-view` on one of them (all six share one calibration) (the logged 2D through the same code, with and without the cage, set by set; without it, the replay gives the logged 3D back exactly). *Far* is animal-sets more than 25 cm from the middle of where the animals seen by all 4 cameras were. *Set aside* is what the cage made not found. *All in 3D* is the share of sets with every animal in 3D. *Swaps* are sets where two identities' positions exchange by more than 30 mm. *Filter only* is the replay without a cage, with the cage's test applied to its output afterwards (the same positions set aside, the tracking untouched). *ms* is the 3D step per set on an M-series Mac, mean.

| run | mice | far: none → cage | set aside | all in 3D: none → cage | swaps: none / filter only / cage | ms: none → cage |
|---|---|---|---|---|---|---|
| 14-38-10 | 3 | 2 → 0 | 19 (0.04%) | 99.47 → 99.36% | 3 / 3 / 3 | 0.78 → 0.82 |
| 14-49-00 | 3 | 24 → 0 | 36 (0.07%) | 99.73 → 99.53% | 2 / 2 / 2 | 0.84 → 0.89 |
| 15-17-41 | 5 | 207 → 0 | 458 (0.51%) | 92.41 → 90.01% | 19 / 7 / 4 | 1.19 → 1.25 |
| 16-10-59 | 5 | 142 → 0 | 322 (0.36%) | 92.87 → 91.18% | 16 / 13 / 11 | 1.22 → 1.26 |
| 16-22-10 | 5 | 128 → 0 | 243 (0.27%) | 94.61 → 93.30% | 10 / 6 / 3 | 1.15 → 1.19 |
| 16-53-12 | 5 | 665 → 0 | 1,530 (1.70%) | 91.77 → 83.66% | 90 / 37 / 34 | 1.38 → 1.42 |

- **What it sets aside is not a mouse.** Of the positions outside, 78–95% were below the floor and the rest above the top; one, in all six runs, was past a wall. In the 5-mouse runs, those below the floor were a median 10–13 cm below it, and the furthest tenth 0.5–1 m. The middles of mice seen by all 4 cameras were above the floor in 99% of sets: the lowest 1% were 13–27 mm above it, and 3 mm below in 16-53-12. 31–66% of the positions set aside were made from 2 cameras (0–3% of those kept), and their median reprojection error was 17–21 px (4–7 px for those kept).
- **All in 3D drops** by the sets whose "every animal in 3D" counted a position no mouse was at. Most in 16-53-12, where the 5 mice were piled against one wall much of the run and 3 of the 4 cameras often saw one merged mouse. There even some groupings of all 4 cameras landed up to 25 cm below the floor.
- **Identities.** Most of the "none" column's swaps are the far positions themselves: a mouse's 3D jumping metres away and back counts as an exchange. *Filter only* is the fair comparison. Against it, the cage's two changes inside the tracker (no identity started from a pair outside, a target outside keeping its 3D) gave as many swaps or fewer in every run, 57 against 68 in all. Their identities are the same in both 3-mouse runs. With 5 mice, the two sometimes resolve a pile differently, after which the same mice carry other numbers (lasting relabellings between them: 1, 4, 2 and 9 in the four runs), as with any change to the tracker. These are swap counts, not checked against hand-labelled identities, which these recordings don't have.
- **Time**: 0.04–0.05 ms more per set (p99 +0.05–0.09 ms). In `campy-live` the 3D step runs beside the next set's inference, so it delays only the 3D, by that much, and not the frame rate.

`bench/pose3d_validate.py` checks it on recorded sessions in LUC3D's layout: it emulates a 3-camera rig with 3 of a session's cameras and compares with LUC3D's own tracking of every camera (run in node by `bench/pose3d_luc3d_ref.mjs` from a LUC3D clone). It was checked on two 8-camera sessions: 5 mice over 3600 sets, and 3 fighting mice over 7201 sets. The reference animal is a LUC3D target with detections in at least 2 of the triple's cameras; its reference 3D is a DLT over all 8 cameras. *assoc* is the share of reference animals whose 3-camera group pose3d made exactly. *sw* counts identity switches against LUC3D. *2-view* is the share of pose3d's animals that were triangulated from only 2 cameras. The errors are per node, in mm.

| cameras | 5 mice: assoc | sw | 2-view | mm p50 / p95 | 3 fighting: assoc | sw | 2-view | mm p50 / p95 |
|---|---|---|---|---|---|---|---|---|
| topB, topL, topR | 100.0% | 0 | 2% | 2.7 / 9.5 | 100.0% | 0 | 4% | 3.6 / 19 |
| mid, topB, topC | 100.0% | 0 | 3% | 1.7 / 6.6 | 100.0% | 0 | 7% | 2.0 / 13 |
| mid, topL, sideR | 99.9% | 0 | 5% | 1.9 / 7.2 | 99.8% | 2 | 9% | 2.0 / 13 |
| topC, sideL, sideR | 100.0% | 0 | 6% | 1.4 / 5.1 | 99.9% | 0 | 7% | 4.2 / 23 |
| sideL, sideC, sideR | 99.8% | 2 | 35% | 3.1 / 12 | 98.8% | 2 | 19% | 3.6 / 21 |
| all 56 triples, median (worst) | 99.9% (99.8%) | 0 (2) | 5% | 1.8 / 7.2 | 99.8% (98.6%) | 0 (4) | 9% | 2.1 / 13 |

Before the changes for misplaced nodes the worst triples were 94.4% (5 mice) and 94.7% (3 fighting), and the switches 20 and 62 in all; now 14 and 66. On the EVO-X2, on one core (`taskset`), a set took 0.53 ms (p99 0.69 ms) for 5 mice and 0.46 ms (p99 0.62 ms) for 3 before those changes, which add about a fifth (on a Mac: 0.62 to 0.73 ms for 5 mice, 0.52 to 0.64 ms for 3).
```
python bench/pose3d_validate.py --session <session folder> --animals 5 --out <cache folder> \
    --triples topB,topL,topR mid,topL,sideR [--all-triples]
```
It needs sleap-io (to read the `.slp` files, once) and node. The other two:
```
python bench/pose3d_live_log.py <campy-live log>.npy calibration.toml [--replay --options '{...}']
python bench/pose3d_proofread.py --sessions <folder> [<folder> ...] --out <cache folder> \
    --triples top,mid,back topL,midL,backL top,topL,midL --ref top [--options '{...}']
```
`pose3d_live_log.py` needs only numpy and scipy, so it runs where campy-live does; with `--replay` it runs a fresh Tracker over the log's 2D, to compare settings on one recording. `pose3d_proofread.py` needs sleap-io.
