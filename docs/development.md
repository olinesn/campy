# Development

Running campy's tests, timing tests on a shared machine without spoiling each other's numbers, and how campy's branches and releases work.

## Running the tests
```
pip install -e . pytest pytest-xdist
pytest -n 6 -m "not alone" tests
pytest -m alone tests
```
`-n 6` runs them on 6 workers at once, which takes the suite from about 17 minutes to about 5: most tests wait on real-time recordings (emulated cameras), timers and child processes rather than on the CPU, so 6 workers beat 4 even on a 4-core computer. Pinned to 4 cores of an NVIDIA Jetson AGX Orin (24 November 2026), the suite took 423 s on 4 workers, 300 s on 6 and 303 s on 8, where a test timed against the clock failed once from the load. The second line runs the test marked `alone`, on its own: a `campy-live-view` test that writes frames at 100 fps and checks that the view warns of nothing but the problem it plants. Beside 3 other workers on GitHub's 2-core Windows runner its frame writer was held up past a frame, so its frames really came late. Run in parallel, it skips itself. (The rig lock's tests, `alone` too, moved with it to [rig-lock](https://github.com/olinesn/rig-lock).) Plain `pytest tests` still runs everything, on one worker. Each test makes its own files in its own temporary folder and names its own live feed (with its process id), so tests don't depend on each other or on their order; a new test should do the same. `--durations=25` lists the slowest tests.

A few of campy's waits can be shortened through the environment, for tests only, so that a test needn't record for minutes to see them run out: `CAMPY_TEST_LIVE_STALL_S` (campy-watch's 30 s with no result from campy-live before it says campy-live hangs), `CAMPY_TEST_LIVE_STOP_S` (the 30 s campy-session then gives campy-live to stop at its stop file) and `CAMPY_TEST_LIVE_RESTART_S` (how long it waits before starting campy-live again: `10,60,300` s). Nothing else sets them; `tests/test_session.py` does.

GitHub Actions runs them on every push to master or dev and every pull request, on Python 3.9, 3.10 and 3.12 on Linux, Python 3.12 on Windows and Python 3.12 on ARM Linux (`.github/workflows/tests.yml`), each on 6 workers. The RatCam tests need nir-eye-cam's `nireyecam`, from a private repository, and are skipped without it. To run them in CI too, add a repository secret `NIREYECAM_TOKEN`: a fine-grained personal access token with read-only access to the contents of `olinesn/nir-eye-cam`.

The tests run `trigger.ino` as plain C++ against a simulated Arduino. A second workflow (`.github/workflows/sketch.yml`) compiles it for real boards (an Arduino Uno, an Uno R4 Minima, a Nano ESP32, a Teensy 4.0 and a Teensy 3.2) whenever the sketch changes.

## Timing tests on a shared machine
When several people (or several Claude sessions) run recordings, campy-live, replays and benchmarks on one machine, two at once spoil each other's numbers. campy doesn't prevent that itself. A machine shared for timing tests runs each of them under a lock: an external program that waits until nothing else uses the GPU, the video encoder, the cameras or the trigger, runs the test, and logs whatever else touched them meanwhile. The lab's machines use `rig.py`, which was campy's `bench/rig.py` until 22 November 2026 and is now a repository of its own, [rig-lock](https://github.com/olinesn/rig-lock). Its README has what was here: how the lock works, [which load counts](https://github.com/olinesn/rig-lock#which-load-counts-and-background-programs), installing and updating it on a machine, holding a Windows machine for your own work, and experiment machines. The timing numbers in these docs were measured under it, and a number counts only from a run whose log ends `RIG_CLEAN` ([Troubleshooting](troubleshooting.md#timing-tests) has the other endings).

campy needs none of it, and works the same without it. What campy has for such a lock:
- **`panel: {rig_lock: auto}`** (off by default) runs campy-session's panel under the rig's lock program, as `<program> run --log-dir <logs> <owner> <tag> -- <the panel>`, so the panel waits for the rig to be free and holds it while it is open ([Live tracking](live-tracking.md)). `auto` is `rig.py` in the lock's folder (`RIG_ROOT`, else `C:\campy_data\live` on Windows and `~/sleap-bench` elsewhere); a path names another program. The panel passes `RIG_ALLOW=ephys`, so SpikeGLX or Open Ephys running beside it doesn't count against it, and it ignores Ctrl-C in its window, which would reach the lock program too and end the whole job at once; close it from its page.
- **Benchmarks and checks** in `bench/` and `tools/windows/` say when to run them under the lock, as every timing test should be on a shared machine. None of them needs it to run.

## Development and releases
Everything lands on `dev` first. `master` is the released campy, and changes only when `dev` is promoted to it.

- **Pull requests** go to `dev`, the repository's default branch.
- **Trying `dev` on a rig:** in its clone, `git fetch`, `git checkout dev` and `git pull`; `git checkout master` goes back. The code is current as soon as the branch is checked out, because `pip install -e .` runs it from the clone. But `pip show campy` keeps the version from the last `pip install -e .`, so to have it report the checked-out version, run `pip install -e . --no-deps` with the campy environment active (`--no-deps` leaves the other packages alone; without it, pip may also update dependencies). Run a full `pip install -e .` when `setup.py`'s dependencies changed.
- **A pre-release** marks a state of `dev` to test, for example on the rigs. Set `version` in `setup.py` to it (`2.1.0rc1`) in a pull request to `dev`, so a rig reports that version once it has run `pip install -e . --no-deps` on it, then:
  ```
  gh release create v2.1.0rc1 --target dev --prerelease --generate-notes
  ```
  Pre-release tags take Python's form, `v2.1.0rc1`, `v2.1.0rc2`, ..., which pip orders before `2.1.0`. A rig checks one out with `git checkout v2.1.0rc1`.
- **A release:** set `version` in `setup.py` to it (`2.1.0`) in a pull request to `dev`. Then run **Actions ▸ Promote dev to master ▸ Run workflow** with the tag `v2.1.0`, or:
  ```
  gh workflow run promote.yml -f tag=v2.1.0
  ```
  It moves `master` forward to `dev` and publishes release `v2.1.0` on that commit, with notes listing the pull requests merged since the last release. With no tag it only promotes. It changes nothing if `setup.py`'s version is not the tag's, or if `master` has a commit that `dev` lacks: promotion only ever fast-forwards, because merging or squashing `dev` into `master` would leave the two histories apart, and every later promotion would conflict.
