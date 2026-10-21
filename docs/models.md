# Pose models for live tracking

`campy-live` runs a sleap-nn **top-down** model in two parts:

- a **centroid model** looks at the whole frame, scaled down, and finds one point on each animal;
- a **centered-instance model** looks at a square crop around each of those points and finds the body parts.

For live use both have to be much smaller than the usual SLEAP models. The standard SLEAP 1.x model for these mice took about 106 ms per set of 3 frames on the EVO-X2, over six times the 16.7 ms there is at 60 fps. The models below were trained to fit.

## How they were trained

- sleap-nn 0.3.3, one data-centre GPU (A100 or L40S) per model, about 2 minutes per epoch.
- About 1,000 labelled frames of 3 mice from several camera angles, 15 body parts.
- The centered-instance models were trained on 12 body parts, without the three tail points, because a crop that holds the tail tip has to be much larger. sleap-nn 0.3.3 won't train on a subset of a skeleton's parts, so the tail points were removed from a copy of the labels file.
- Batch size 4, the same augmentation for every model, a fixed random seed, so models differ only in the settings being compared.

## Settings that worked

**Centroid**: 0.25 input scale, 16 filters, max stride 16, **output stride 2**, a 10 px confidence-map spread (sigma, in full-size pixels), anchored on the trunk. Output stride 2 with sigma 10 found 97.6% of the mice in the validation frames, against 94.8% at output stride 4 with sigma 20. Most of the remaining misses were mice touching each other.

**Centered-instance**: a 320 px crop scaled to half size, so the model sees **160 px**, with **24 filters**, **max stride 32** and sigma 5 px.

- *Crop size* comes from the animals. Measured on 3,000 labelled mice, a trunk-centred crop needed 286 px for 99% of them and 369 px for the largest (rearing, seen from the side). A 320 px crop holds all but 10 of the 3,000.
- *Scale the crop down* rather than cropping smaller. Half scale was as accurate as 0.6–0.8 scale with max stride 32.
- *Max stride 32*, not 16. With max stride 16, at inputs of 256 px and up, the model sees too little of the crop to tell which mouse is the centred one, and puts the whole skeleton on a neighbour more often. Across all the crop sizes tried, max stride 32 cut wrong-mouse skeletons from about 11 to 4 per 293.
- *24 filters* beat 16. A filter rate above 2.0 helped the small models catch up, but didn't improve this one.

Accuracy on the same 293 validation mice (11 body parts, median / 90th / 99th percentile error in pixels, and how many skeletons landed on the wrong mouse):

| Centered-instance model | Error p50 / p90 / p99 | Wrong mouse |
|---|---|---|
| 160 px (320 px at half scale), 24 filters, max stride 32 | 3.5 / 12.7 / 51.4 px | 2 |
| 320 px at full scale, 24 filters, max stride 32 | 3.1 / 11.4 / 52.6 px | 4 |
| 192 px (384 px at half scale), 24 filters, max stride 16 | 3.9 / 13.4 / 66.5 px | 7 |
| 192 px (384 px at half scale), 16 filters, max stride 16 | 4.3 / 16.1 / 80.6 px | 8 |
| Standard SLEAP 1.x model, for reference (its own validation split) | 3.7 / 12.9 / 56.9 px | 3 |

## Choosing one that keeps up

1. **Benchmark the sizes first**, untrained, on the machine that will run them. Run time doesn't depend on the weights:
   ```
   python bench/sleap_infer_bench.py --precision fp16 --crops-per-frame 5 \
       --centroid-output-stride 2 --crop 160 --instance-filters 24 --instance-max-stride 32 \
       --pace 60 --hold-clock on
   ```
   `--crop` is the size the centered-instance model sees, `--crops-per-frame` is the number of animals, and `--frames` is the number of cameras (3 by default). `--sweep` runs a preset grid of 32 sizes.
   `--pace 60` starts the sets 60 times a second, as the cameras deliver them, and `--hold-clock on` keeps the GPU's clock up as `campy-live` does. Without them the numbers mislead: a GPU lowers its clock between paced sets, and a heavier model, which keeps it busier, can then look as fast as a light one. Each row records the clock the GPU ran at.
2. **Leave about 2 ms.** The live pipeline ran 1–1.7 ms slower than the benchmark at the median and 2–2.5 ms at p99, because it also copies frames in, reads results back and logs them. At 60 fps, pick a model whose benchmark p99 is about 14–14.5 ms or less. At 30 fps there is far more room.
3. **Check it live**: the summary's skipped sets should be zero ([Live tracking](live-tracking.md)).

On the EVO-X2 with 5 mice and 3 cameras, the benchmark p99s were 14.0 ms for the 160 px model, 15.3 ms for the 24-filter 192 px model and 10.6 ms for the 16-filter one. In the full live pipeline the 160 px model skipped nothing. The 24-filter 192 px model, with less room, still skipped a set now and then, and the 16-filter one kept up easily but was less accurate. The [hardware guide](hardware.md) has the live measurements.

## Recording for a model

- **Match the training images' brightness.** The training frames averaged about 24 out of 255. Frames about 8 times brighter, with many saturated pixels, lost most mice and produced hundreds of false detections, and frames half as bright lost about half. Set aperture, exposure and gain so live frames look like the training frames.
- **Gamma**: since October 2026 campy sets FLIR cameras to gamma 0.6 by default (`cameraGamma`). Before that its default turned the camera's gamma off, so older training videos were probably recorded that way. Gamma 0.6 makes dark and middle greys brighter. To match a model trained on older videos, set `cameraGamma: "Off"`, or check that the model still finds every animal.
- **Video quality**: H.265 at quality 26 (Quick Sync, 30 fps) cost the models 1.6% of the animals they found in lossless video, against 3.7% at quality 30 and 7.7% at 34.
