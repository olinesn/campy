# Hardware for live tracking

The full guide, with every measurement, is [hardware-guide.html](hardware-guide.html). GitHub shows that file as source code; to read it formatted, open the [published copy](https://claude.ai/artifact/1iFm1x157Wfw4rrHQf2FTk) or download the file and open it in a browser. It compares three machines running campy and sleap-nn together:

- **GMKtec EVO-X2**: mini-PC, AMD Ryzen AI Max+ 395 with its Radeon 8060S integrated GPU, Ubuntu 24.04 and ROCm.
- **Dell Pro Max Slim FCS1250**: Intel Core Ultra 9 285 (8 performance + 16 efficiency cores), NVIDIA RTX 2000 Ada, Windows 11 and CUDA.
- **Dell Precision 7875**: AMD Threadripper PRO 7965WX, NVIDIA RTX 6000 Ada (plus an RTX 4000 Ada for the monitor), Windows 11 and CUDA.

## The short version

This list leaves the numbers to the guide, so it can't fall out of date with it.

1. **The inference GPU decides most of it.** A large NVIDIA card with no monitor attached kept up with every model size tried. The small 70 W card didn't, unless its CPU threads were pinned.
2. **Fast CPU cores matter more than many cores**, especially on Windows, where the work around each model costs more CPU time. Leave a core for `campy-live-view` if you'll watch the tracking.
3. **On an Intel hybrid CPU, pin inference to the performance cores** at High priority and put the recording on the efficiency cores. `campy-live` doesn't do this by itself yet.
4. **Keep Windows awake.** campy asks Windows to stay awake while it records (`keepAwake`), but a PC with nobody signed in still needs its screen timeout set to Never.
5. **Leave about 2 ms of headroom.** The live pipeline runs a little slower than the inference-only benchmark, so pick a model whose benchmark p99 is about 2 ms under the frame interval ([Models](models.md)).
6. **Dual-channel memory is enough.** Memory bandwidth didn't limit anything. 64 GB covers 3 cameras, including campy's buffer for disk stalls.
7. **Record videos to a disk of their own.** Some SSDs stop completing writes for many seconds. campy's write buffer (`writeQueueMaxSec`) absorbs that, but a separate drive avoids it.
8. **Linux gave the steadiest timing**: no set took twice the median. On Windows only the largest GPU, or pinned inference, came close.
9. **The EVO-X2 handles 3 cameras at 60 fps with 5 mice, but not 8 cameras.**
10. **3D tracking is cheap**, and it runs alongside inference, so it doesn't add to the time per set.

## Measuring a new machine

`bench/sleap_infer_bench.py` times the models alone, untrained, so you can test a machine before any labelling. Its docstring explains the options; `--sweep` runs the 32 model sizes the guide reports. Then run the whole pipeline on real cameras ([Live tracking](live-tracking.md)), because that is what has to keep up. Feeding recorded video into the live feed tests the same thing with real animals, but the scripts used for that aren't in campy yet.
