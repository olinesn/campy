# Hardware for live tracking

The full guide, with every measurement, is [hardware-guide.html](hardware-guide.html). GitHub shows that file as source code; to read it formatted, open the [published copy](https://claude.ai/artifact/1iFm1x157Wfw4rrHQf2FTk) or download the file and open it in a browser. It compares three machines running campy and sleap-nn together:

- **GMKtec EVO-X2**: mini-PC, AMD Ryzen AI Max+ 395 with its Radeon 8060S integrated GPU, Ubuntu 24.04 and ROCm.
- **Dell Pro Max Slim FCS1250**: Intel Core Ultra 9 285 (8 performance + 16 efficiency cores), NVIDIA RTX 2000 Ada, Windows 11 and CUDA.
- **Dell Precision 7875**: AMD Threadripper PRO 7965WX, NVIDIA RTX 6000 Ada (plus an RTX 4000 Ada for the monitor), Windows 11 and CUDA. Since 21 October 2026 it is also set up for live tracking (campy 2.1.0rc7 since 22 October), and has run the 5-mouse replay with 3D, and recorded three emulated cameras while tracking them.

## The short version

This list leaves the numbers to the guide, so it can't fall out of date with it.

1. **The inference GPU decides most of it.** A large NVIDIA card with no monitor attached kept up with every model size tried. The small 70 W card didn't, unless its CPU threads were pinned and its clock held up.
2. **A GPU slows its own clock** when the work comes in short bursts, as live tracking's does. The NVIDIA cards tested did, the AMD one less, though the Pro Max Slim's card stopped after a restart (the Precision's RTX 6000 Ada didn't). `campy-live` now keeps the GPU at full speed by default, NVIDIA or AMD (`--hold-gpu-clock`), which halved the time per set on the RTX 2000 Ada, and spins nothing when the clock stays up by itself. Benchmark at the cameras' pace with the clock held (`--pace 60 --hold-clock on`), as `campy-live` runs; sets run back to back flatter the GPU.
3. **Fast CPU cores matter more than many cores**, especially on Windows, where the work around each model costs more CPU time. Leave a core for `campy-live-view` if you'll watch the tracking.
4. **On an Intel hybrid CPU, run inference on the performance cores** at High priority, with the recording on the efficiency cores. `campy-live` does both by default.
5. **Keep Windows awake.** campy asks Windows to stay awake while it records (`keepAwake`), but a PC with nobody signed in still needs its screen timeout set to Never.
6. **Leave about 2 ms of headroom.** The live pipeline runs a little slower than the inference-only benchmark, so pick a model whose benchmark p99 is about 2 ms under the frame interval ([Models](models.md)).
7. **Dual-channel memory is enough.** Memory bandwidth didn't limit anything. 64 GB covers 3 cameras, including campy's buffer for disk stalls.
8. **Record videos to a disk of their own.** Some SSDs stop completing writes for many seconds. campy's write buffer (`writeQueueMaxSec`) absorbs that, but a separate drive avoids it.
9. **Linux gave the steadiest timing**: no set took twice the median. On Windows only the largest GPU, or pinned inference with the GPU's clock held, came close.
10. **The EVO-X2 handles 3 cameras at 60 fps with 5 mice, but not 8 cameras.** The Pro Max Slim keeps up with 3 cameras at 60 fps with 3 mice, but, with both off, not with 5 on the same models: its GPU needs about as long as one frame per set, so it skips a few percent of the sets and its 3D poses come about 28 ms after the frames, against the EVO-X2's 17. With fixed crops and CUDA graphs, both on by default with a GPU now, it keeps up with 5: 12.6 ms per set, nothing skipped, 3D poses 15.8 ms after the frames (see live-tracking.md, "Fixed crops"). With `--fixed-crops off`, give it a lighter centered-instance model for 5 mice at 60 fps.
11. **3D tracking is cheap**, and it runs alongside inference, so it doesn't add to the time per set.
12. **A compiled engine could make the models several times faster** (measured, not in `campy-live` yet): `torch.compile` on the EVO-X2's AMD GPU, TensorRT on NVIDIA cards, where it most helps the smaller ones. int8 isn't worth it ([Models](models.md)).

## Measuring a new machine

`bench/sleap_infer_bench.py` times the models alone, untrained, so you can test a machine before any labelling. Its docstring explains the options; `--sweep` runs the 32 model sizes the guide reports. Then run the whole pipeline on real cameras ([Live tracking](live-tracking.md)), because that is what has to keep up. To test the same thing with real animals but no cameras, campy's emulated cameras (`cameraMake: "emu"`) play recorded videos as if they were cameras: `campy-acquire` records them with your encoder settings and copies every frame into the live feed, with frame n reaching the computer at the same moment on every camera, as with a hardware trigger ([Live tracking](live-tracking.md#testing-without-cameras), and the [README](../README.md#emulated-cameras-cameramake-emu)). The scripts that write recorded video straight into the live feed, without recording it, aren't in campy yet.

When the machine is shared, run every timing test through `bench/rig.py` (on Linux or Windows): it runs one test at a time, says whether anything else used the GPU, the cameras, the encoder, the trigger, the CPU or the disk during it (`RIG_CLEAN` or `RIG_CONTAMINATED`), and logs the GPU's clock. Compare only clean runs, at the same GPU clock: on the Dell Pro Max Slim FCS1250, overlapping runs took a model pair from 27.6 to 42.7 ms per set, and the GPU's clock alone made a factor of two (above). The [README](../README.md#timing-tests-on-a-shared-machine-benchrigpy) explains it.
