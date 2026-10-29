# Development

Running campy's tests, timing tests on a shared machine without spoiling each other's numbers, and how campy's branches and releases work.

## Running the tests
```
pip install -e . pytest
pytest tests
```
GitHub Actions runs them on every push to master or dev and every pull request, on Python 3.9, 3.10 and 3.12 on Linux and Python 3.12 on Windows (`.github/workflows/tests.yml`). The RatCam tests need nir-eye-cam's `nireyecam`, from a private repository, and are skipped without it. To run them in CI too, add a repository secret `NIREYECAM_TOKEN`: a fine-grained personal access token with read-only access to the contents of `olinesn/nir-eye-cam`.

The tests run `trigger.ino` as plain C++ against a simulated Arduino. A second workflow (`.github/workflows/sketch.yml`) compiles it for real boards (an Arduino Uno, a Teensy 4.0 and a Teensy 3.2) whenever the sketch changes.

## Timing tests on a shared machine (bench/rig.py)
When several people (or several Claude sessions) run recordings, campy-live, replays and benchmarks on one machine, two at once spoil each other's numbers. `bench/rig.py` is a lock they all take, on Linux and on Windows: `run` waits until nobody else holds it and nothing else is on the GPU, the video encoder, the FLIR cameras or the trigger, and the CPU and disk are quiet; then it runs the command, and every 2 s logs anything outside it that touches those to `<tag>.rig.log`, which ends `RIG_CLEAN` or `RIG_CONTAMINATED`. Only numbers from a clean run are worth comparing.
```
python3 bench/rig.py status
python3 bench/rig.py run <your name> <tag> -- bash my_test.sh <tag> ...
python3 bench/rig.py hold <your name> <tag> <pid>     # for a job already running
python3 bench/rig.py break                            # remove a lock nobody holds any more
```
With an NVIDIA GPU the log also follows the GPU's clock and what holds it down (nvidia-smi's throttle reasons), and the last line gives the median clock. That matters on Windows: the RTX 2000 Ada sat at 780 MHz of its 2,500 during live tracking, and at 210 MHz while waiting for it, until `campy-live` held its clock up.

What it watches:

| | Linux | Windows |
|---|---|---|
| GPU | `/dev/kfd` open | a CUDA program (type C in `nvidia-smi -q -d PIDS`), or any `python*.exe` |
| Video encoder | `/dev/dri/renderD*` open | `ffmpeg*.exe` |
| FLIR cameras | their USB devices open, or Spinnaker loaded | a Spinnaker DLL loaded |
| Trigger | `/dev/ttyACM*`, `/dev/ttyUSB*` open | a COM port open |
| Desktop on the GPU (noted, not waited for) | gnome-shell, Xwayland, ...; one starting or ending contaminates the run | programs that draw on the GPU (dwm.exe, explorer.exe, ...); Windows starts and ends some by itself, so this is only logged |

On Windows, every Python process counts as a GPU user, because any of them may start CUDA (`nvidia-smi` lists a CUDA program only once it has). The disk load there starts from what all the physical disks actually read and wrote (the `PhysicalDisk(_Total)` counter): what processes outside the job read and wrote, but no more than the disks moved, since Windows counts a process's pipes, devices, network and cached files alike; or, if more, what the disks moved beyond the job's own reads and writes, which catches a file copy that went into Windows' memory cache and was written out after the copy had ended. The kernel's own count ("System", pid 4) is left out: Windows' display driver charges it with every frame a job copies to the GPU, 400-800 MB/s on the Precision 7875 with nothing read from or written to a disk, which made every GPU benchmark there end `RIG_CONTAMINATED` before. The lock there is a file, `C:\campy_data\live\gpu.lock`, created only if it is absent, and deleted when the run ends. It names who holds the rig and since when, and the process ids of `rig.py` and of the job: once both have ended, the lock counts as free, so a crash can't leave the rig locked. A `gpu.lock` written by hand (a name and a time) holds the rig until whoever wrote it deletes it, or `rig.py break`.

A script takes the lock by itself when its first command is `. <folder of rig.py>/rig_guard.sh "$@"` (bash), or, in PowerShell, `. <folder of rig.py>\rig_guard.ps1 @args; if ($null -ne $RigExit) { exit $RigExit }`. `RIG_OWNER` names who runs it; on Windows `RIG_PYTHON` names the `python.exe` to run `rig.py` with. It needs only Python's standard library, and is installed by copying, not with campy: the machines run the copy in their lock's folder, not the one in a campy checkout. On the lab's EVO-X2 that is `~/sleap-bench` (`RIG_ROOT`), holding `rig.py` and `rig_guard.sh`, where `rig.sh` runs `rig.py` with `/usr/bin/python3`; on the Windows machines (the Dell Pro Max Slim FCS1250 and the Dell Precision 7875), `C:\campy_data\live`, holding `rig.py`, `rig_windows.py` and `rig_guard.ps1`, with `RIG_PYTHON` set as a user environment variable to a 64-bit Python 3.9 or newer. When a change to them is merged into `dev`, copy the new files over the old ones on each machine (a run already going keeps the version it started with), then check with `rig.py status`.

**Holding a Windows machine for your own work.** Training a model, or anything else long that uses the GPU, would spoil a timing test, and a test would slow down your work. To keep tests off the machine meanwhile, run `bench/rig_hold.ps1` at the machine (or over Remote Desktop) and leave its window open:
```
powershell -NoProfile -ExecutionPolicy Bypass -File C:\campy_data\live\rig_hold.ps1 -Owner <you> -Tag <what, e.g. sleap-training> -Why "<one line>"
```
A `.bat` file holding that line makes it a double-click. It writes `gpu.lock` as `rig.py` does, naming its own window as the holder, so `rig.py status` says `rig held: <you> <what> since <time> ... (held at the screen with rig_hold.ps1: <one line>; ...)`, and every `rig.py run`, guarded script and `rig.py hold` waits; `rig.py break` refuses to remove it. If a test holds the machine when you start it, it waits for that test to end and says so. It starts and stops nothing else, and needs no Python, so it can't count as a GPU user itself. To end the hold, press R then Y in its window, which deletes the lock and says RELEASED; or close the window: `rig.py` counts a lock whose holder has ended as free. The window's title says WAITING, HELD (with the time held) or RELEASED, and what it prints goes to a `.log` beside the script. It is for Windows only; on Linux, `rig.py hold <you> <what> <pid>` holds the machine for as long as a program you started runs.

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
