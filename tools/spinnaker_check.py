"""Check FLIR Spinnaker and PySpin downloads against the SHA-256s campy's rigs use.

campy can't ship Spinnaker or PySpin (FLIR's licence forbids passing them on),
so each computer gets them from FLIR's site or from a copy already on one of
your own computers. This checks that a file is the exact one the rigs run:

    python spinnaker_check.py SpinnakerSDK_FULL_4.4.0.246_x64.exe
    python spinnaker_check.py ~/Downloads          # every Spinnaker file in a folder

Each file is matched by its name: OK when its SHA-256 is the one below,
MISMATCH when it differs (a damaged or changed file: don't install it), and
"not listed" for a Spinnaker file of a version or platform not in the list.
Exits 1 on any MISMATCH; otherwise 2 when no file checked was listed or a
file named doesn't exist, and 0 when at least one listed file was OK. Needs only Python's standard library (3.9 or newer).

To add a new Spinnaker version, add its files' SHA-256s to KNOWN and change
PINNED, in the same pull request as docs/install.md's table.
"""
import argparse
import hashlib
import os
import sys

PINNED = "4.4.0.246"

# File name -> SHA-256, worked out on the rigs' own copies (13 November 2026).
# The arm64 package was the same on two computers.
KNOWN = {
    # Windows x64
    "SpinnakerSDK_FULL_4.4.0.246_x64.exe":
        "ca842792d522cbd4ca3549554ca11995108ec3b641ab3267d1f4bec319cd7e98",
    "spinnaker_python-4.4.0.246-cp312-cp312-win_amd64.zip":
        "153773d0bc039b9709305c0a448f06a12718f16a385c336b0effe82d9163e996",
    "spinnaker_python-4.4.0.246-cp312-cp312-win_amd64.whl":
        "5705935b45dcd3a71fae02f9afebed4c383549e4642098b3eca43fa6748c52b7",
    # Ubuntu 24.04, x86-64
    "spinnaker-4.4.0.246-noble-amd64-pkg.tar.gz":
        "4a5df47c647fca519c27b83b1a75ad9a86872373ac384844eb9af9701f7dc2a4",
    "spinnaker_python-4.4.0.246-cp312-cp312-linux_x86_64.tar.gz":
        "ce69b1e8fd2195f31d7ef19b38e34d81f47ab3be176a301b2dd042ba150cb518",
    "spinnaker_python-4.4.0.246-cp312-cp312-linux_x86_64.whl":
        "b60c7db0619a25e1fe1189526443f74d324d066a1e527924dffbf12d2d1daa04",
    # Ubuntu 24.04, Arm64 (Jetson AGX Orin)
    "spinnaker-4.4.0.246-noble-arm64-pkg.tar.gz":
        "9c4aef63cb070a4734e7e93051898b70aaeac17c13b86ef7cbee8c787554e261",
    "spinnaker_python-4.4.0.246-cp312-cp312-linux_aarch64.tar.gz":
        "37b0586ffcbce59f8f4ee7f6d43692cf4dd667a1c75717640c76422e3cbeafa3",
    "spinnaker_python-4.4.0.246-cp312-cp312-linux_aarch64.whl":
        "19c84b4ee675913d6f6780267399a18f07bb7d7c8962390e07701f6d8d04c132",
}

SUFFIXES = (".exe", ".whl", ".zip", ".tar.gz")


def sha256(path, block=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(block), b""):
            h.update(chunk)
    return h.hexdigest()


def is_spinnaker_file(name):
    low = name.lower()
    return low.startswith("spinnaker") and low.endswith(SUFFIXES)


def files_to_check(paths):
    """Files named on the command line, and Spinnaker files in folders named there."""
    out = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                full = os.path.join(p, name)
                if os.path.isfile(full) and is_spinnaker_file(name):
                    out.append(full)
        else:
            out.append(p)
    return out


def check(path, known=None):
    """Return (status, message) for one file: status is OK, MISMATCH, UNLISTED or MISSING."""
    known = KNOWN if known is None else known
    name = os.path.basename(path)
    if not os.path.isfile(path):
        return "MISSING", f"{name}: no such file"
    want = known.get(name)
    if want is None:
        return "UNLISTED", (f"{name}: not listed (campy's rigs use Spinnaker {PINNED}; "
                            "a renamed file is matched by its original name only)")
    got = sha256(path)
    if got == want:
        return "OK", f"{name}: OK"
    return "MISMATCH", f"{name}: MISMATCH, SHA-256 {got}, expected {want}. Don't install it."


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="*", help="Spinnaker or PySpin files, or folders holding them")
    ap.add_argument("--list", action="store_true", help="print the pinned files and their SHA-256s")
    args = ap.parse_args(argv)
    if args.list or not args.paths:
        print(f"Spinnaker {PINNED}, as campy's rigs run it:")
        for name, h in KNOWN.items():
            print(f"{h}  {name}")
        return 0
    files = files_to_check(args.paths)
    if not files:
        print("No Spinnaker files found.")
        return 2
    statuses = []
    for f in files:
        status, msg = check(f)
        statuses.append(status)
        print(msg)
    if "MISMATCH" in statuses:
        return 1
    if "OK" not in statuses or "MISSING" in statuses:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
