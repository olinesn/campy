#!/usr/bin/env python3
"""Read-only checklist for a Jetson AGX Orin set up as a campy rig.

Prints PASS, WARN or FAIL for each item, with a one-line fix for anything
that isn't right. It changes nothing: it only reads files under /etc, /proc
and /sys, and runs commands that report (nvpmodel -q, gst-inspect-1.0,
ffmpeg -version, lsusb, and short Python one-liners in the two venvs).
Run it as your normal user, after every step below and after each JetPack
update:

    python3 bench/jetson_setup_check.py
    python3 bench/jetson_setup_check.py --campy-venv ~/campy/.venv --sleap-venv ~/venvs/sleap --json check.json

The PySpin item opens Spinnaker to count the cameras, so run it when no
recording is going (or pass --no-cameras). Exit status: 0 if nothing
failed, 1 otherwise. Needs only Python's standard library.

How to set up the Orin (the full version is docs/jetson.md)
------------------------------------------------------------
1. Flash JetPack 7.2.1 (Jetson Linux 39.2.1: Ubuntu 24.04, Python 3.12,
   CUDA 13.2, TensorRT 10.16) with NVIDIA SDK Manager from an Ubuntu PC
   (or the USB installer image; if it loops, SDK Manager or
   l4t_initrd_flash). Then: sudo apt update && sudo apt install nvidia-jetpack
2. Put an NVMe drive in the M.2 Key-M slot for video (the 64 GB eMMC is too
   small), format it ext4 and mount it at /data from /etc/fstab (by UUID),
   owned by your user.
3. Full power: sudo nvpmodel -m 0 (MAXN; it may ask to reboot), then
   sudo jetson_clocks --fan after every boot (a systemd unit can do it).
4. USB for the cameras: add usbcore.usbfs_memory_mb=1000 usbcore.autosuspend=-1
   to the end of the APPEND line in /boot/extlinux/extlinux.conf, reboot.
   Add yourself to the dialout group for the trigger's /dev/ttyACM0.
5. Tools: sudo apt install ffmpeg gstreamer1.0-tools gstreamer1.0-plugins-base
   gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
6. FLIR: Spinnaker 4.4 for ARM64 / Ubuntu 24.04 (install_spinnaker_arm.sh)
   and the matching PySpin wheel for Python 3.12 (cp312, aarch64).
7. Two venvs, with the system Python 3.12:
   - campy: python3 -m venv ~/campy/.venv; pip install -e ~/campy; the PySpin
     wheel; PyQt5 (if pip has no aarch64 wheel: sudo apt install python3-pyqt5
     and make the venv with --system-site-packages)
   - sleap: python3 -m venv ~/venvs/sleap;
     pip install torch torchvision --index-url https://download.pytorch.org/whl/cu132;
     then pip install sleap-nn; then pip install -e ~/campy
     (torch first: sleap-nn's own CUDA torch source is x86 only. A warning
     that sm_87 isn't in torch's build list is harmless.)
"""

import argparse
import glob
import gzip
import json
import os
import re
import shutil
import subprocess
import sys

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"

# what this rig wants
WANT_L4T = (39, 2)          # JetPack 7.2.x = Jetson Linux R39.2.x
WANT_USBFS_MB = 1000
FLIR_USB_VENDOR = "1e10"    # FLIR (Point Grey) USB vendor id


class Report:
    def __init__(self):
        self.items = []

    def add(self, status, name, detail, fix=None):
        self.items.append({"status": status, "item": name, "detail": detail,
                           "fix": fix if status in (WARN, FAIL) else None})

    def print(self):
        for it in self.items:
            print("%-4s  %-34s %s" % (it["status"], it["item"], it["detail"]))
            if it["fix"]:
                print("      %-34s fix: %s" % ("", it["fix"]))
        n = {s: sum(1 for it in self.items if it["status"] == s) for s in (PASS, WARN, FAIL, SKIP)}
        print("\n%d pass, %d warn, %d fail, %d skipped" % (n[PASS], n[WARN], n[FAIL], n[SKIP]))

    def failed(self):
        return any(it["status"] == FAIL for it in self.items)


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def run(argv, timeout=30, **kw):
    """(exit code, stdout + stderr), or (None, why) if it couldn't run."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **kw)
    except FileNotFoundError:
        return None, "%s not found" % argv[0]
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    return p.returncode, (p.stdout + p.stderr)


# --------------------------------------------------------------------------
# Parsers (pure, unit-tested)
# --------------------------------------------------------------------------

def parse_tegra_release(text):
    """'# R39 (release), REVISION: 2.1, ...' -> (39, 2, 1), or None."""
    m = re.search(r"R(\d+)\s*\(release\),\s*REVISION:\s*(\d+)(?:\.(\d+))?", text or "")
    if not m:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def parse_nvpmodel(text):
    """nvpmodel -q output -> (mode name, mode number), either None if absent."""
    name = re.search(r"NV Power Mode:\s*(\S+)", text or "")
    num = re.findall(r"^\s*(\d+)\s*$", text or "", re.M)
    return (name.group(1) if name else None, int(num[-1]) if num else None)


def kernel_config_value(config_text, option):
    """'y', 'm' or None ('is not set' or missing) for CONFIG_<option> in a kernel config."""
    m = re.search(r"^CONFIG_%s=(\w+)" % re.escape(option), config_text or "", re.M)
    return m.group(1) if m else None


def count_usb_vendor(lsusb_text, vendor):
    return len(re.findall(r"ID %s:[0-9a-f]{4}" % re.escape(vendor), lsusb_text or "", re.I))


def clocks_locked(pairs):
    """jetson_clocks sets each CPU's and the GPU's minimum clock to its maximum.

    pairs: [(name, min, max)] -> (all locked, [names not locked]).
    """
    loose = [name for name, lo, hi in pairs if lo is None or hi is None or lo < hi]
    return (bool(pairs) and not loose), loose


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def check_jetpack(rep):
    text = read("/etc/nv_tegra_release")
    if text is None:
        rep.add(FAIL, "Jetson Linux (L4T) version", "no /etc/nv_tegra_release: not a Jetson, or not flashed with JetPack",
                "flash JetPack 7.2.1 with SDK Manager (docs/jetson.md)")
        return False
    v = parse_tegra_release(text)
    code, out = run(["dpkg-query", "-W", "-f", "${Version}", "nvidia-jetpack"])
    jp = " (nvidia-jetpack %s)" % out.strip() if code == 0 and out.strip() else ""
    if v is None:
        rep.add(WARN, "Jetson Linux (L4T) version", "can't read %r" % text.splitlines()[0][:80], "check /etc/nv_tegra_release")
    elif v[:2] >= WANT_L4T:
        rep.add(PASS, "Jetson Linux (L4T) version", "R%s%s" % (".".join(map(str, v)), jp))
    else:
        rep.add(FAIL, "Jetson Linux (L4T) version", "R%s%s, want R39.2 (JetPack 7.2.1)" % (".".join(map(str, v)), jp),
                "reflash with JetPack 7.2.1: JetPack 6's torch wheels are Python 3.10, sleap-nn needs 3.11+")
    if not jp:
        rep.add(WARN, "nvidia-jetpack packages", "not installed (CUDA, TensorRT, multimedia)",
                "sudo apt update && sudo apt install nvidia-jetpack")
    return True


def check_power(rep):
    code, out = run(["nvpmodel", "-q"])
    if code is None:
        rep.add(FAIL, "power mode (nvpmodel)", out, "nvpmodel comes with JetPack (sudo apt install nvidia-l4t-tools)")
    else:
        name, num = parse_nvpmodel(out)
        if num == 0:
            rep.add(PASS, "power mode (nvpmodel)", "%s (mode 0)" % name)
        else:
            rep.add(FAIL, "power mode (nvpmodel)", "%s (mode %s), want MAXN (mode 0)" % (name, num),
                    "sudo nvpmodel -m 0 (reboot if it asks); 50 W is the fallback if MAXN passes 65 W and throttles")

    pairs = []
    for cpu in sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq")):
        lo, hi = read(cpu + "/scaling_min_freq"), read(cpu + "/scaling_max_freq")
        pairs.append((cpu.split("/")[-2], int(lo) if lo else None, int(hi) if hi else None))
    for dev in sorted(glob.glob("/sys/class/devfreq/*")):
        if "gpu" in os.path.basename(dev) or "ga10b" in os.path.basename(dev):
            lo, hi = read(dev + "/min_freq"), read(dev + "/max_freq")
            pairs.append(("gpu", int(lo) if lo else None, int(hi) if hi else None))
    if not pairs:
        rep.add(SKIP, "jetson_clocks", "no cpufreq/devfreq in /sys")
        return
    ok, loose = clocks_locked(pairs)
    if ok:
        rep.add(PASS, "jetson_clocks", "CPU and GPU clocks held at their maximum")
    else:
        rep.add(FAIL, "jetson_clocks", "not active: %s can still clock down" % ", ".join(loose[:4] + (["..."] if len(loose) > 4 else [])),
                "sudo jetson_clocks --fan (again after every boot)")


def check_usb(rep):
    mb = read("/sys/module/usbcore/parameters/usbfs_memory_mb")
    if mb is None:
        rep.add(FAIL, "usbfs memory", "no /sys/module/usbcore/parameters/usbfs_memory_mb", "is this Linux with USB core loaded?")
    elif int(mb) >= WANT_USBFS_MB:
        rep.add(PASS, "usbfs memory", "%s MB" % mb.strip())
    else:
        rep.add(FAIL, "usbfs memory", "%s MB, want >= %d (FLIR cameras drop frames)" % (mb.strip(), WANT_USBFS_MB),
                "add usbcore.usbfs_memory_mb=1000 to APPEND in /boot/extlinux/extlinux.conf, reboot")
    a = read("/sys/module/usbcore/parameters/autosuspend")
    if a is None:
        rep.add(FAIL, "USB autosuspend", "no /sys/module/usbcore/parameters/autosuspend", "is this Linux?")
    elif a.strip() == "-1":
        rep.add(PASS, "USB autosuspend", "-1 (off)")
    else:
        rep.add(FAIL, "USB autosuspend", "%s s, want -1 (cameras and the trigger can be suspended)" % a.strip(),
                "add usbcore.autosuspend=-1 to APPEND in /boot/extlinux/extlinux.conf, reboot")


def check_serial(rep):
    loaded = os.path.isdir("/sys/module/cdc_acm")
    cfg = None
    try:
        with gzip.open("/proc/config.gz", "rt") as f:
            cfg = kernel_config_value(f.read(), "USB_ACM")
    except OSError:
        pass
    if loaded or cfg in ("y", "m"):
        how = "loaded" if loaded else ("built in" if cfg == "y" else "module, loads when a board is plugged in")
        rep.add(PASS, "cdc_acm driver (trigger serial)", how)
    else:
        rep.add(FAIL, "cdc_acm driver (trigger serial)",
                "not in this kernel (CONFIG_USB_ACM %s)" % ("not set" if cfg is None and os.path.exists("/proc/config.gz") else "unknown"),
                "sudo modprobe cdc_acm; if it isn't there, build it as a module for this kernel (JetPack 6+ dropped some USB serial drivers)")
    ports = sorted(glob.glob("/dev/ttyACM*"))
    if ports:
        rep.add(PASS, "trigger port", ", ".join(ports))
        if not os.access(ports[0], os.R_OK | os.W_OK):
            rep.add(FAIL, "trigger port access", "%s isn't readable and writable by you" % ports[0],
                    "sudo usermod -aG dialout $USER, then log out and in")
    else:
        rep.add(FAIL, "trigger port", "no /dev/ttyACM*", "plug in the Nano ESP32 (a data USB cable); dmesg | tail shows whether it was seen")


def check_gstreamer(rep):
    for element, why in (("nvv4l2h265enc", "the hardware H.265 encoder"), ("nvvidconv", "GRAY8 -> NV12 on the VIC"),
                         ("rawvideoparse", "raw frames from a pipe"), ("h265parse", "the encoder's output")):
        code, out = run(["gst-inspect-1.0", element])
        if code == 0:
            rep.add(PASS, "GStreamer %s" % element, why)
        elif code is None:
            rep.add(FAIL, "GStreamer %s" % element, out,
                    "sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-bad nvidia-jetpack")
            return
        else:
            rep.add(FAIL, "GStreamer %s" % element, "not found (%s)" % why,
                    "sudo apt install nvidia-jetpack" if element.startswith("nv") else
                    "sudo apt install gstreamer1.0-plugins-base gstreamer1.0-plugins-bad")
        if element == "nvv4l2h265enc" and code == 0:
            # the properties bench/jetson_encode_bench.py sets
            here = os.path.dirname(os.path.abspath(__file__))
            sys.path.insert(0, here)
            try:
                import jetson_encode_bench as b
                missing = b.missing_properties(out, b.gst_encoder_props(21, 60))
            except ImportError:
                missing = None
            finally:
                sys.path.remove(here)
            if missing:
                rep.add(WARN, "nvv4l2h265enc properties", "missing: %s" % ", ".join(missing),
                        "the encoder's properties changed in this JetPack: update gst_encoder_props in bench/jetson_encode_bench.py")


def check_ffmpeg(rep):
    exe = shutil.which("ffmpeg")
    if not exe:
        rep.add(FAIL, "ffmpeg", "not found (campy muxes the encoder's H.265 into MP4 with it)", "sudo apt install ffmpeg")
        return
    code, out = run([exe, "-hide_banner", "-version"])
    first = out.splitlines()[0] if out else ""
    rep.add(PASS, "ffmpeg", first[:70])
    code, enc = run([exe, "-hide_banner", "-encoders"])
    missing = [c for c in ("libx264", "libx265") if c not in (enc or "")]
    if missing:
        rep.add(WARN, "ffmpeg CPU encoders", "no %s (only the --cpu-fallback comparison needs them)" % ", ".join(missing),
                "sudo apt install ffmpeg (Ubuntu's build has both)")


def venv_python(venv):
    return os.path.join(os.path.expanduser(venv), "bin", "python")


def check_pyspin(rep, venv, cameras, expected, lsusb_flir):
    py = venv_python(venv)
    if not os.path.exists(py):
        rep.add(FAIL, "PySpin (campy venv)", "no %s" % py,
                "python3 -m venv %s, then pip install -e ~/campy and the PySpin cp312 aarch64 wheel" % venv)
        return
    code_ = ("import PySpin, json; s = PySpin.System.GetInstance(); v = s.GetLibraryVersion(); "
             "n = None\n"
             "if %r:\n"
             "    c = s.GetCameras(); n = c.GetSize(); c.Clear()\n"
             "s.ReleaseInstance(); print(json.dumps(['%%d.%%d.%%d.%%d' %% (v.major, v.minor, v.type, v.build), n]))"
             % bool(cameras))
    code, out = run([py, "-c", code_], timeout=60)
    if code != 0:
        rep.add(FAIL, "PySpin (campy venv)", "won't import: %s" % (out.strip().splitlines() or ["?"])[-1][:90],
                "install Spinnaker 4.4 ARM64 for Ubuntu 24.04, then its PySpin wheel (cp312, aarch64) into %s" % venv)
        return
    version, n = json.loads(out.strip().splitlines()[-1])
    rep.add(PASS, "PySpin (campy venv)", "Spinnaker %s" % version)
    if not cameras:
        rep.add(SKIP, "FLIR cameras (Spinnaker)", "--no-cameras")
    elif n is not None and n >= expected:
        rep.add(PASS, "FLIR cameras (Spinnaker)", "%d seen" % n)
    else:
        usb = "" if lsusb_flir is None else "; lsusb lists %d FLIR device%s" % (lsusb_flir, "" if lsusb_flir == 1 else "s")
        rep.add(FAIL, "FLIR cameras (Spinnaker)", "%s seen, want %d%s" % (n, expected, usb),
                "check cables and power (2 A shared on the USB ports), usbfs memory, and that you're in the flirimaging group")


def check_campy_venv(rep, venv):
    py = venv_python(venv)
    if not os.path.exists(py):
        return
    code, out = run([py, "-c", "import sys, campy; from importlib.metadata import version; "
                               "print(sys.version.split()[0], version('campy'))"])
    if code == 0:
        rep.add(PASS, "campy (campy venv)", "Python %s, campy %s" % tuple(out.split()[:2]))
    else:
        rep.add(FAIL, "campy (campy venv)", "won't import", "%s -m pip install -e ~/campy" % py)
    code, out = run([py, "-c", "from PyQt5.QtCore import QT_VERSION_STR, PYQT_VERSION_STR; "
                               "print(PYQT_VERSION_STR, QT_VERSION_STR)"])
    if code == 0:
        rep.add(PASS, "PyQt5 (campy venv)", "PyQt %s, Qt %s" % tuple(out.split()[:2]))
    else:
        rep.add(FAIL, "PyQt5 (campy venv)", "won't import (campy-live-view needs it)",
                "pip install PyQt5; if there's no aarch64 wheel: sudo apt install python3-pyqt5 and remake the venv with --system-site-packages")


def check_sleap_venv(rep, venv):
    py = venv_python(venv)
    if not os.path.exists(py):
        rep.add(FAIL, "torch + sleap-nn (sleap venv)", "no %s" % py,
                "python3 -m venv %s; pip install torch torchvision --index-url https://download.pytorch.org/whl/cu132; pip install sleap-nn" % venv)
        return
    code_ = ("import json, warnings; warnings.simplefilter('ignore'); import torch\n"
             "from importlib.metadata import version\n"
             "try:\n    sn = version('sleap-nn')\nexcept Exception:\n    sn = None\n"
             "ok = torch.cuda.is_available()\n"
             "print(json.dumps([torch.__version__, torch.version.cuda, ok, torch.cuda.get_device_name(0) if ok else None, sn]))")
    code, out = run([py, "-c", code_], timeout=120)
    if code != 0:
        rep.add(FAIL, "torch (sleap venv)", "won't import: %s" % (out.strip().splitlines() or ["?"])[-1][:90],
                "%s -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu132" % py)
        return
    tv, cuda, ok, name, sn = json.loads(out.strip().splitlines()[-1])
    if ok:
        rep.add(PASS, "torch.cuda (sleap venv)", "torch %s, CUDA %s, %s" % (tv, cuda, name))
    else:
        rep.add(FAIL, "torch.cuda (sleap venv)", "torch %s (CUDA %s) sees no GPU" % (tv, cuda),
                "reinstall torch from https://download.pytorch.org/whl/cu132 (a CPU-only wheel came from PyPI?)")
    if sn:
        rep.add(PASS, "sleap-nn (sleap venv)", sn)
    else:
        rep.add(FAIL, "sleap-nn (sleap venv)", "not installed", "%s -m pip install sleap-nn (after torch)" % py)


def check_disk(rep, folder, min_free_gb):
    if not os.path.isdir(folder):
        rep.add(FAIL, "video disk", "no %s" % folder,
                "put an NVMe drive in the M.2 Key-M slot, format it ext4, mount it at %s from /etc/fstab" % folder)
        return
    du = shutil.disk_usage(folder)
    free = du.free / 1e9
    same_as_root = os.stat(folder).st_dev == os.stat("/").st_dev
    source = None
    for line in (read("/proc/mounts") or "").splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[1] == folder:
            source = parts[0]
    detail = "%.0f GB free of %.0f GB%s" % (free, du.total / 1e9, " on %s" % source if source else "")
    if same_as_root:
        rep.add(FAIL, "video disk", detail + ", but on the system disk (eMMC)",
                "mount the NVMe drive at %s (see /etc/fstab)" % folder)
    elif free < min_free_gb:
        rep.add(FAIL, "video disk", detail + ", want >= %g GB" % min_free_gb, "free space or use a bigger drive")
    else:
        rep.add(PASS, "video disk", detail)
    if not os.access(folder, os.W_OK):
        rep.add(FAIL, "video disk access", "%s isn't writable by you" % folder, "sudo chown $USER %s" % folder)


def lsusb_flir_count():
    code, out = run(["lsusb"])
    return count_usb_vendor(out, FLIR_USB_VENDOR) if code == 0 else None


class Help(argparse.ArgumentDefaultsHelpFormatter):
    # shows a default only where there is one (not for flags and None)
    def _get_help_string(self, action):
        if action.default in (None, False) or "default" in (action.help or ""):
            return action.help
        return super()._get_help_string(action)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Read-only checklist for a Jetson AGX Orin campy rig: prints PASS/WARN/FAIL with a "
                    "one-line fix for each item, and changes nothing. Setup steps: the top of this "
                    "script, or docs/jetson.md.",
        formatter_class=Help)
    p.add_argument("--campy-venv", default="~/campy/.venv", help="the venv campy-acquire runs in (PySpin, PyQt5)")
    p.add_argument("--sleap-venv", default="~/venvs/sleap", help="the venv campy-live runs in (torch, sleap-nn)")
    p.add_argument("--video-dir", default="/data", help="where recordings go (the NVMe drive)")
    p.add_argument("--min-free-gb", type=float, default=200, help="free space wanted on --video-dir")
    p.add_argument("--cameras", type=int, default=3, help="FLIR cameras that should be plugged in")
    p.add_argument("--no-cameras", action="store_true",
                   help="don't open Spinnaker to count cameras (use while a recording is running)")
    p.add_argument("--json", default=None, help="also write the results to this JSON file")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    rep = Report()
    check_jetpack(rep)
    check_power(rep)
    check_usb(rep)
    check_serial(rep)
    check_gstreamer(rep)
    check_ffmpeg(rep)
    flir = lsusb_flir_count()
    if flir is not None:
        rep.add(PASS if flir >= args.cameras else WARN, "FLIR cameras on USB (lsusb)", "%d" % flir,
                "plug in all %d cameras, on the USB 3.2 Gen2 ports" % args.cameras)
    check_pyspin(rep, args.campy_venv, not args.no_cameras, args.cameras, flir)
    check_campy_venv(rep, args.campy_venv)
    check_sleap_venv(rep, args.sleap_venv)
    check_disk(rep, args.video_dir, args.min_free_gb)
    rep.print()
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"items": rep.items}, f, indent=2)
    return 1 if rep.failed() else 0


if __name__ == "__main__":
    sys.exit(main())
