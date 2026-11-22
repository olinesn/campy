"""
One test at a time on a shared test machine: the lab's EVO-X2 (Linux) or the Dell Pro Max Slim
FCS1250 (Windows).

Several people, or several Claude sessions, run timing tests on the same
machine: campy recordings, campy-live, replays of recorded videos, sleap-nn
benchmarks and training. Two of them at once spoil each other's numbers, and
nobody notices unless something says so. rig.py is that something: a lock
every test takes, and a watch over everything a test can be spoiled by.

  rig.py status                                 who holds the rig, and who uses each device
  rig.py run [options] <owner> <tag> -- cmd...  wait for the rig, then run cmd holding it
  rig.py hold [options] <owner> <tag> <pid>     hold the rig for a job already running
  rig.py break                                  remove a lock nobody holds any more
  rig.py version                                this copy's version, and the installed one's
  rig.py install [--check] [--from <checkout>]  install this copy (a campy checkout's) as the rig's

run waits until the rig is free, nothing else uses the GPU, the cameras, the
video encoder or the trigger, no ephys acquisition program (SpikeGLX, Open
Ephys) runs, and nothing else loads the CPU or the disk; then it runs cmd.
Every 2 s while it runs, anything outside the job that uses one of those, or a
CPU or disk load outside the job above --cpu cores or --io MB/s, is logged to
<tag>.rig.log, whose last line says RIG_CLEAN, RIG_CLEAN_BACKGROUND or
RIG_CONTAMINATED. One look at a device user contaminates
a run; load has to last (--load-looks looks in a row, 2, or more than
--load-share of the run's looks, 0.2), and a machine's known background load
(RIG_ROOT/background.json: an IT security agent, each program with a CPU and
disk ceiling) makes it RIG_CLEAN_BACKGROUND instead, unless it covers more of the
run than that file allows. --strict (RIG_STRICT=1): every look above the limits
counts, background or not. A contaminated run's timings are not to be trusted:
run it again. Nor are those of a run stopped before its job ended (Ctrl-C,
SIGTERM, a hangup, an error in rig.py), whose last line says RIG_ABORTED, or of
one whose log has no RIG_ line at all (its rig.py was killed): trust only a last
line starting RIG_CLEAN, and only RIG_CLEAN itself under --strict. A
signal ignored when rig.py starts stays ignored: under nohup, a hangup (the
terminal or the ssh closing) stops neither rig.py nor its job. A closed output
(rig.py run ... | head, a cut ssh) doesn't stop it either: the log still gets
every line. With an NVIDIA GPU (nvidia-smi), the log also follows the GPU's clock
and why it is held down (its throttle reasons): the same GPU can run a test at
210 MHz or at 2,500.

On Linux (every process the user can see; other users' processes show only their CPU use):
  gpu      /dev/kfd                 GPU compute (ROCm: torch, sleap-nn)
           /dev/nvgpu/*, /dev/nvhost-*gpu   a Jetson's integrated GPU (CUDA on an AGX Orin)
  encoder  /dev/dri/renderD*        VA-API encoding (campy's ffmpegs); ROCm opens it too, and
                                    CUDA on a Jetson
           /dev/v4l2-nvenc          a Jetson's video encoder (GStreamer's nvv4l2h264enc/h265enc)
  camera   /dev/bus/usb/... of a FLIR camera (USB vendor 1e10), or libSpinnaker
           loaded (a process that can open, reset or enumerate the cameras)
  trigger  /dev/ttyACM*, /dev/ttyUSB*  the trigger Arduino
  ephys    a process named open-ephys* or spikeglx* (RIG_EPHYS_NAMES; by /proc/<pid>/comm, any user's)
  load     the CPU (cores) and disk (MB/s) used by everything outside the job
  desktop  a desktop drawing on the GPU (gnome-shell, Xwayland, Xorg, GNOME's gsd-xsettings,
           mutter-x11-frames and portal helper xdg-desktop-portal-gnome, ...; any user,
           the login screen too). run does not wait for it (it would not go away),
           but says in the log which was up, and a desktop that starts or ends
           during the run contaminates it.

On macOS (rig_macos.py beside this file says how each is seen; every user's CPU, through ps, but only this
user's processes' open files, disk and environment):
  trigger  /dev/cu.usbmodem*, /dev/cu.usbserial*, ... (and their /dev/tty.* names): a USB serial port
  ephys    a program named spikeglx* or open-ephys* (RIG_EPHYS_NAMES), a Windows one under Wine too
  load     the CPU (cores) and disk (MB/s) used by everything outside the job (disk: this user's processes)
  desktop  WindowServer, noted and never waited for
  gpu, encoder and camera are not watched (Metal, VideoToolbox and the cameras have no device files, and
  every app with a window draws on the GPU): status says so. The lock is the Linux one, a flock.

On Windows (rig_windows.py beside this file says how each is seen):
  gpu      a CUDA program (type C in nvidia-smi -q -d PIDS), or any python*.exe but another rig.py
           (see "Other rig.py processes" below)
  encoder  ffmpeg*.exe
  camera   a Spinnaker DLL loaded (PySpin, SpinView)
  trigger  a COM port open
  ephys    SpikeGLX*.exe or open-ephys*.exe running (RIG_EPHYS_NAMES): SpikeGLX (SpikeGLX_NISIM
           too) or the Open Ephys GUI. Running at all counts, recording or not: an unattended
           Neuropixels recording (an imec PCIe base station, a OneBox, NI DAQ cards) opens drivers
           rig.py can't name, so the program is what it looks for
  load     the CPU as on Linux. The disk: what all disks read and wrote (PhysicalDisk(_Total))
           less what the job's processes read and wrote, or what processes outside the job
           read and wrote if more, but never more than the disks moved (a program's reads and
           writes also count pipes, devices, the network and the file cache). The kernel's
           (System's) count no I/O of its own: the display driver charges it with every frame a
           job copies to the GPU.
  desktop  programs that draw on the GPU (type G or C+G: dwm.exe, explorer.exe, Task
           Manager, ...). Noted, not waited for; the log says when one starts or
           ends, but that doesn't contaminate the run, since Windows starts and
           ends some (WebView, search) by itself.

Other rig.py processes on Windows (status, a run waiting in line, hold, break, install, version) use
nothing, and never count. rig.py knows one by its command line, which it can't read for a python.exe
started over ssh (elevated) or by another user; so every rig.py, as it starts, also writes a marker,
RIG_ROOT/rig-processes/<pid>-<start> (and one for a venv's python.exe that started it), saying what it
was started to do (rig.py run, status, ...). A watching rig.py exempts a python.exe with a marker. One
with neither, started under RIG_MARK_WAIT s (5) ago, is held back for those seconds before it counts
(it may be a rig.py yet to write its marker); if it ends in them without a marker, it counts then.
install, which refuses while another run, hold or install runs or waits, reads the markers too. A
marker stays when its rig.py ends; each rig.py removes those of rig.py processes that ended over 10
min ago.

--allow ephys (RIG_ALLOW=ephys) on run or hold: ephys acquisition programs outside the job are
neither waited for nor counted, only logged (ALLOWED). For a real recording that pairs cameras with
SpikeGLX, such as campy-session's panel, which sets RIG_ALLOW=ephys when it takes the rig: the
lock still keeps tests away while it records, but SpikeGLX, started by hand beside it, mustn't keep
the panel from opening. --allow takes any of the kinds above, comma-separated.

A process belongs to the job when it carries the job's RIG_JOB in its
environment (everything the job starts does, even with setsid or nohup), is in
the job's session, or descends from the job; on Windows, when it is in the
job's Windows job object (everything the job starts is). A script can take
the rig itself by sourcing rig_guard.sh (bash) or rig_guard.ps1 (PowerShell)
first, which re-runs it under rig.py unless it is already part of the rig's
current job. Likewise rig.py run, inside the rig's current job, runs its command
at once, in that job, instead of waiting for the rig its own job holds.

The lock. On Linux, a flock on RIG_ROOT/.rig.lock (~/sleap-bench), which the
kernel releases if rig.py dies, and RIG_BUSY saying who holds it (from the
moment its rig.py has the flock, while it waits for quiet too) and the process
id of its job: a rig.py that was killed (SIGKILL, the OOM killer) loses its
flock, but its job runs on, and the rig stays held until that ends. On Windows,
the file C:\\campy_data\\live\\gpu.lock (RIG_ROOT), created only if it is
absent and deleted when the run ends. It says who holds the rig, since when,
and the process ids of rig.py and of the job: a lock whose rig.py and job have
both ended counts as free, so a crash can't leave the rig locked. A gpu.lock
written by hand ("<who> <time>", as sessions did before rig.py ran on Windows)
is respected until whoever wrote it deletes it, or `rig.py break`. RIG_LOCK=file
uses the Windows lock on Linux too.

One GPU at a time (--gpu N, or RIG_GPU=N; the file lock only, so Windows, or RIG_LOCK=file). On a machine
with two or more NVIDIA GPUs, a run can hold one GPU alone, nvidia-smi's GPU N (rig.py status lists them),
instead of the whole machine. Its job sees only that GPU (CUDA_VISIBLE_DEVICES, set to the GPU's UUID), and a
run holding another GPU may run beside it. Its lock is RIG_ROOT/gpu<N>.lock (the same line as gpu.lock's),
and while any GPU is held alone, gpu.lock says so with a line of its own ('per-gpu 0+1 since ...', which
names every such job's job= and the rig.py or job processes that keep it held), so that whatever only knows
gpu.lock (rig_hold.ps1, the GPU queue of a Windows VM, an older rig.py, a guarded script's check for its
own job) sees the machine held, as before. A run without --gpu holds the whole machine, as always, and so
does one with --gpu and --whole (RIG_WHOLE=1) or --strict, which are for timings that must have the
machine to themselves; and so does --gpu where there is only one GPU, no nvidia-smi, the flock, or an
experiment machine (the job still sees only that GPU). What counts against a run holding GPU N: anything on
GPU N outside its job, the job of another GPU's run among them; anything outside both jobs, as always
(device users, and CPU and disk load as LoadTrack says); the other GPU's job using a camera, the trigger or
an ephys program (those are the machine's, not a GPU's); and its own job running CUDA on a GPU it doesn't
hold (WRONG GPU in the log, wrong-gpu on the last line). The other GPU's job, everything it starts
(by its RIG_JOB, by the processes its rig.py lists in gpu<N>.pids every look, or by descending from them or
from its rig.py) and its CPU and disk load don't count; which GPU a process runs CUDA on is nvidia-smi's
(-q -d PIDS, by GPU). A run that had such a job beside it at any look ends RIG_SHARED, not RIG_CLEAN: its
results stand, but the two shared the CPU, memory, disks and PCIe, so its timings are not a benchmark's.
Who waits, waits in line (RIG_ROOT/rig-queue, a ticket per waiting rig.py or rig_hold.ps1, oldest first):
a run takes what it waits for only once nobody earlier in line waits for the same GPU or for the whole
machine, so runs for single GPUs, one after another, can't keep a run for the whole machine waiting for
ever, and one for GPU 1 passes one waiting for GPU 0 (a free GPU stays idle while the whole machine is
waited for).

On an experiment machine (RIG_ROOT/experiment.json; docs/design/rig-fleet.md, section 5), where people
record experiments, run starts a test only when every Windows session (quser) has been idle 15+ min,
nothing outside it uses a device, the hour is allowed ("hours", default 22:00-06:00) and RIG_ROOT/PAUSE
isn't there (or its time has passed), and no ephys acquisition program runs; otherwise it exits 75 at once, never waiting in line. On Windows the
banner (rig_banner.ps1, started in the session by a Task Scheduler task; rig_experiment_setup.ps1 sets it
up) must show the test first. The test ends within 2 s, every process of it, when RIG_ROOT/STOP appears
(the banner's Stop button, the "Stop campy test" shortcut, keyboard or mouse input at the machine, an
unlock, a Remote Desktop connect, a logon), when anything outside it starts using the GPU, a camera, the
encoder or the trigger (campy, SpinView, a Python) or an ephys acquisition program starts (SpikeGLX, Open
Ephys), or when quser shows a session in use; the log's last
line is then RIG_STOPPED, run exits 75, and PAUSE keeps new tests away until 9 AM ("pause_until").

Installing and versions. Each machine runs one installed copy, in RIG_ROOT, which every
script calls (a campy checkout can be on any branch or commit, and every lock user on a
machine must run the same lock). Its version is a short hash of the seven files (rig.py,
rig_windows.py, rig_macos.py, rig_guard.sh, rig_guard.ps1, rig_hold.ps1, rig_banner.ps1) with line endings ignored, so the
same commit shows the same version on every machine; status (its second line) and every
rig.log say it. To
update, run the checkout's own copy: python <checkout>/bench/rig.py install. It takes the rig
with --no-wait (exit 75 if busy, nothing changed), refuses while any other rig.py run, hold
or install, or rig_hold.ps1, is running or waiting (exit 75; on Windows a rig.py started over ssh is
known by its marker, see "Other rig.py processes"), backs the old files up to
RIG_ROOT/rig-backup/<old version>-<time>/, writes each new file beside the old one, checks
it, renames it into place, and records what it installed in RIG_ROOT/RIG_INSTALLED and
rig-install.log. Inside the rig's own job (an update script run under rig.py) it doesn't
wait for the rig that job holds. --check only says whether the installed copy differs
(exit 0 the same, 3 different). It refuses a checkout with uncommitted changes to these
files (--allow-dirty), or one older than what is installed (--allow-older). Files copied
from a checkout (not a git checkout themselves) can name their commit with --commit.

The Python standard library only. On Linux and macOS run it with /usr/bin/python3; on
Windows with any 64-bit Python 3.9 or newer (campy's own environment will do).
"""

import argparse, collections, contextlib, datetime, fnmatch, hashlib, json, os, re, shlex, shutil, signal, subprocess, sys, time

WINDOWS = os.name == "nt"
MACOS = sys.platform == "darwin"
if WINDOWS:
	import msvcrt
	sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
	import rig_windows
else:
	import fcntl, pwd
if MACOS:
	sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
	import rig_macos

ROOT = os.path.expanduser(os.environ.get("RIG_ROOT", r"C:\campy_data\live" if WINDOWS else "~/sleap-bench"))
LOCK = os.path.join(ROOT, ".rig.lock")
INFO = os.path.join(ROOT, "RIG_BUSY")
PERIOD = float(os.environ.get("RIG_PERIOD", 2.0))  # s between looks
# Other files to watch as one of the kinds, e.g. RIG_EXTRA="trigger=/dev/ttyS0,gpu=/dev/nvidia0"
# (on Windows, NT device names: RIG_EXTRA="trigger=\Device\Serial0")
EXTRA = dict(reversed(kv.split("=", 1)) for kv in os.environ.get("RIG_EXTRA", "").split(",") if "=" in kv)
if MACOS:  # macOS gives an open file's real path (/private/var/..., not /var/...)
	EXTRA.update({os.path.realpath(path): kind for path, kind in list(EXTRA.items())})
KINDS = ("gpu", "encoder", "camera", "trigger", "ephys")
# Ephys acquisition programs (the "ephys" kind), by name: on Linux /proc/<pid>/comm (its first 15
# characters), on Windows the image name (rig_windows.EPHYS_NAMES, the same variable). Their PCIe and
# USB acquisition hardware (an imec base station, a OneBox, NI DAQ cards) opens devices rig.py has no
# names for, so the program running at all is what counts. RIG_EPHYS_NAMES (comma-separated
# patterns) replaces the list on a machine
EPHYS_NAMES = [s.strip().lower() for s in os.environ.get("RIG_EPHYS_NAMES", "spikeglx*,open-ephys*").split(",") if s.strip()]
# Desktops drawing on the GPU, by /proc/<pid>/comm (the kernel keeps its first 15 characters). A logged-in
# GNOME session also has two helpers that keep the GPU's render node open for as long as it runs:
# gsd-xsettings and mutter-x11-frames (comm "mutter-x11-fram"). At the login screen they run as gdm, whose
# open files rig.py can not see; logged in, they are the user's and would otherwise look like encoder users
# that never go away. So would GNOME's portal helper, xdg-desktop-portal-gnome, on any GNOME desktop: it
# draws the dialogs apps ask the desktop for (a file chooser, the screen-sharing picker; the capture itself
# runs in gnome-shell), opens the render node with the first one it draws, and keeps it until it ends. On
# the EVO-X2 (GNOME on Wayland) it did so partway through a run, and the next run waited until it was
# stopped. Its comm, "xdg-desktop-por", is also that of the other portals (xdg-desktop-portal itself and
# -gtk). Ubuntu's GNOME on a Jetson (an X11 session) adds two more that draw with the GPU: GNOME Software's
# background service, and the first-login wizard (comm "gnome-initial-s")
DESKTOP = {"gnome-shell", "Xwayland", "Xorg", "Xvnc", "mutter", "kwin_wayland", "kwin_x11", "plasmashell",
	"sway", "weston", "Hyprland", "xfwm4", "TeamViewer", "gsd-xsettings", "mutter-x11-frames"[:15],
	"xdg-desktop-portal-gnome"[:15], "gnome-software", "gnome-initial-setup"[:15]}
FLIR_VENDOR = "1e10"
HZ = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
NO_WINDOW = 0x08000000 if WINDOWS else 0  # CREATE_NO_WINDOW: no console flashing up for nvidia-smi
JOBS = {}  # Windows: {job name: rig_windows.Job} of the job rig.py runs
HERE = os.path.dirname(os.path.abspath(__file__))
# The lock's files: installed together, and hashed together into its version
FILES = ("rig.py", "rig_windows.py", "rig_macos.py", "rig_guard.sh", "rig_guard.ps1", "rig_hold.ps1", "rig_banner.ps1")
# The kinds macOS gives rig.py no way to see (rig_macos.py), unless RIG_EXTRA names a file as one
UNWATCHED = ({"gpu", "encoder", "camera"} - set(EXTRA.values())) if MACOS else set()
STAMP = "RIG_INSTALLED"

def say(text):
	"""print, which a closed stdout can't stop. With rig.py run ... | head, or an ssh whose local end was
	cut, a print raises, and rig.py would end mid-run, its job still running. From then on what it says
	goes nowhere; its log still gets every line."""
	try:
		print(text, flush=True)
	except (OSError, ValueError):
		try:
			fd = os.open(os.devnull, os.O_WRONLY)
			os.dup2(fd, sys.stdout.fileno())
			os.close(fd)
		except (OSError, ValueError):
			sys.stdout = open(os.devnull, "w")

def device_kind(path, cameras):
	if path in EXTRA:
		return EXTRA[path]
	if path == "/dev/kfd":
		return "gpu"
	# A Jetson (AGX Orin, JetPack 7.2): CUDA opens /dev/nvgpu/igpu0/ctrl (and /dev/nvmap, renderD128);
	# the GPU's other nodes are /dev/nvhost-*-gpu. The video encoder is /dev/v4l2-nvenc, which
	# NVIDIA's V4L2 plugin opens (the node itself is a stand-in: the plugin does the work)
	if path.startswith("/dev/nvgpu/") or (path.startswith("/dev/nvhost-") and path.endswith("gpu")):
		return "gpu"
	if path.startswith("/dev/dri/renderD") or path == "/dev/v4l2-nvenc":
		return "encoder"
	if re.match(r"/dev/tty(ACM|USB)\d+$", path) or (MACOS and rig_macos.SERIAL.match(path)):
		return "trigger"
	if path in cameras:
		return "camera"
	return None

def is_ephys(comm):
	"""Whether a Linux process's comm names an ephys acquisition program."""
	comm = comm.lower()
	return any(fnmatch.fnmatchcase(comm, pat[:15]) for pat in EPHYS_NAMES)

def ephys_programs():
	"""[(pid, name)] of every ephys acquisition program running, anyone's, found by name alone: cheap
	enough to ask before anything else (no nvidia-smi, no handle table)."""
	if WINDOWS:
		return sorted((row[0], row[2]) for row in rig_windows.process_table()
			if rig_windows.matches(row[2], rig_windows.EPHYS_NAMES))
	if MACOS:
		return sorted((p.pid, rig_macos.program_name(getattr(p, "exe", None) or first_word(p.cmd)))
			for p in scan().values() if "ephys" in p.kinds)
	out = []
	for name in os.listdir("/proc") if os.path.isdir("/proc") else ():
		if name.isdigit():
			try:
				with open("/proc/%s/comm" % name) as f:
					comm = f.read().strip()
			except OSError:
				continue
			if is_ephys(comm):
				out.append((int(name), comm))
	return sorted(out)

def flir_cameras():
	"""{'/dev/bus/usb/BBB/DDD': 'bus-port'} for every FLIR camera on USB."""
	out = {}
	base = "/sys/bus/usb/devices"
	for d in os.listdir(base) if os.path.isdir(base) else ():
		try:
			with open(os.path.join(base, d, "idVendor")) as f:
				if f.read().strip() != FLIR_VENDOR:
					continue
			bus = int(open(os.path.join(base, d, "busnum")).read())
			dev = int(open(os.path.join(base, d, "devnum")).read())
		except (OSError, ValueError):
			continue
		out["/dev/bus/usb/%03d/%03d" % (bus, dev)] = d
	return out

class Proc:
	# unsure (Windows): a python.exe whose command line can't be read and that has no rig.py marker (classify)
	# gpus: the GPUs (nvidia-smi's indexes) it runs CUDA on, when known; partner: the GPU whose run's job it is
	# in, for a run holding another GPU (Watch). Read both with getattr: they are set only where known
	__slots__ = ("pid", "ppid", "sid", "start", "cpu", "io", "kinds", "cmd", "job", "user", "exe", "unsure", "gpus", "partner")

def read_proc(pid, cameras):
	"""A Proc for pid, or None if it is gone or a kernel thread (Linux)."""
	p = Proc()
	p.pid = pid
	try:
		with open("/proc/%d/stat" % pid) as f:
			stat = f.read()
	except OSError:
		return None
	fields = stat[stat.rindex(")") + 2:].split()
	p.ppid, p.sid = int(fields[1]), int(fields[3])
	p.cpu = (int(fields[11]) + int(fields[12])) / HZ
	p.start = int(fields[19])
	if pid == 2 or p.ppid == 2 or fields[0] == "Z":  # kernel threads; zombies have ended
		return None
	try:
		with open("/proc/%d/cmdline" % pid, "rb") as f:
			p.cmd = " ".join(f.read().decode(errors="replace").replace("\0", " ").split())
	except OSError:
		p.cmd = ""
	if not p.cmd:
		p.cmd = "[" + stat[stat.index("(") + 1:stat.rindex(")")] + "]"
	p.kinds, p.io, p.job, p.user = set(), None, None, None
	comm = stat[stat.index("(") + 1:stat.rindex(")")]
	if is_ephys(comm):  # by name: seen whoever runs it, before its open files (another user's are hidden)
		p.kinds.add("ephys")
	if comm in DESKTOP:
		p.kinds.add("desktop")
		try:
			p.user = pwd.getpwuid(os.stat("/proc/%d" % pid).st_uid).pw_name
		except (OSError, KeyError):
			pass
		return p
	try:
		fds = os.listdir("/proc/%d/fd" % pid)
	except OSError:
		return p  # another user's process: only its CPU use is visible
	for fd in fds:
		try:
			k = device_kind(os.readlink("/proc/%d/fd/%s" % (pid, fd)), cameras)
		except OSError:
			continue
		if k:
			p.kinds.add(k)
	if "camera" not in p.kinds:
		try:
			with open("/proc/%d/maps" % pid) as f:
				if "libSpinnaker" in f.read():
					p.kinds.add("camera")
		except OSError:
			pass
	try:
		with open("/proc/%d/environ" % pid, "rb") as f:
			m = re.search(rb"(?:^|\0)RIG_JOB=([^\0]*)", f.read())
			p.job = m.group(1).decode() if m else None
	except OSError:
		pass
	try:
		with open("/proc/%d/io" % pid) as f:
			io = dict(line.split(": ") for line in f.read().splitlines())
			p.io = int(io["read_bytes"]) + int(io["write_bytes"])
	except (OSError, KeyError, ValueError):
		pass
	return p

# macOS: a process's command line and RIG_JOB ({(pid, start, program): (argv, job)}), read once per process:
# they don't change while the program runs (an exec changes its program)
MAC_ARGS = {}

def scan_macos():
	"""{pid: Proc} for every process on macOS (rig_macos.py): ps for all of them (pid, parent, CPU time,
	start, program); this user's own also show their open files, disk bytes and environment."""
	procs, me, args = {}, os.getuid(), {}
	for pid, ppid, uid, cpu, start, stat, exe in rig_macos.process_table():
		if pid == 0 or "Z" in stat:  # kernel_task (the kernel, as Linux's kernel threads); zombies have ended
			continue
		p = Proc()
		p.pid, p.ppid, p.start, p.cpu, p.exe = pid, ppid, start or 0, cpu or 0.0, exe
		p.sid, p.kinds, p.io, p.job, p.user, p.cmd = rig_macos.getsid(pid), set(), None, None, None, exe
		argv = []
		if uid == me:
			key = (pid, start, exe)
			info = MAC_ARGS.get(key) or rig_macos.procargs(pid)
			if info:
				args[key] = info
				argv, env = info[:2]
				p.cmd = " ".join(" ".join(argv).split()) or exe
				p.job = rig_macos.job_of(env)
			p.io = rig_macos.disk_io(pid)
		name = rig_macos.program_name(exe)
		if rig_macos.matches(name, EPHYS_NAMES) or (argv and rig_macos.matches(rig_macos.program_name(argv[0]), EPHYS_NAMES)):
			p.kinds.add("ephys")  # a Windows program under Wine: its process is Wine's, its first word the program
		if name in rig_macos.DESKTOP:
			p.kinds.add("desktop")
			try:
				p.user = pwd.getpwuid(uid).pw_name
			except KeyError:
				p.user = str(uid)
		elif uid == me:
			for path in rig_macos.open_paths(pid):
				k = device_kind(path, ())
				if k:
					p.kinds.add(k)
		procs[pid] = p
	MAC_ARGS.clear()
	MAC_ARGS.update(args)
	return procs

def scan(own=(), gpus=False):
	"""{pid: Proc} for every process. own: pids known to be the job's (Windows looks less at them). gpus: also
	which GPU each process runs CUDA on (Proc.gpus), from nvidia-smi; on Windows always (it asks anyway), on
	Linux and macOS only when asked (a run holding one GPU), and there a process nvidia-smi lists uses the GPU."""
	if MACOS:
		procs = scan_macos()
	elif WINDOWS:
		procs = {}
		on_gpu, placed = gpu_snapshot()  # first, so nvidia-smi has ended before the processes are listed
		compute = {pid for pid, t in on_gpu.items() if "G" not in t}
		for pid, ppid, start, cpu, io, kinds, name, job, unsure in rig_windows.scan(JOBS, compute, EXTRA, own,
				set(on_gpu) - compute, rig_marks()):
			p = Proc()
			p.pid, p.ppid, p.sid, p.start, p.cpu, p.io = pid, ppid, None, start, cpu, io
			p.kinds, p.cmd, p.job, p.user, p.unsure = kinds, name, job, None, unsure
			p.gpus = placed.get(pid, set())
			procs[pid] = p
		return procs
	else:
		cameras = flir_cameras()
		procs = {}
		for name in os.listdir("/proc"):
			if name.isdigit():
				p = read_proc(int(name), cameras)
				if p:
					procs[p.pid] = p
	if gpus and NVSMI:
		_, placed = gpu_snapshot()
		for pid, where in placed.items():
			p = procs.get(pid)
			if p is not None:
				p.gpus = where
				if "desktop" not in p.kinds:
					p.kinds.add("gpu")
	return procs

def disk_bytes():
	"""The bytes all physical disks have read and written (a running count), or None where the
	processes' own counts are disk bytes already (Linux: /proc/<pid>/io's read_bytes, write_bytes).
	On Windows a process's count is all its I/O, disk or not, and what Windows' file cache writes
	out later is the kernel's, so the load outside the job comes from what the disks moved too
	(Watch.sample)."""
	return rig_windows.disk_bytes() if WINDOWS else None

def proc_start(pid):
	"""When pid started (an opaque number), None if it isn't running; -1 or 0 if it runs but that is unknown."""
	if WINDOWS:
		return rig_windows.process_start(pid)
	if MACOS:
		return rig_macos.process_start(pid)
	try:
		with open("/proc/%d/stat" % pid) as f:
			fields = f.read().rsplit(")", 1)[1].split()
		return None if fields[0] == "Z" else int(fields[19])
	except (OSError, IndexError, ValueError):
		if os.path.isdir("/proc/self"):
			return None
	try:  # no /proc (macOS)
		os.kill(pid, 0)
		return 0
	except PermissionError:
		return 0
	except OSError:
		return None

def alive(pid, start):
	s = proc_start(pid)
	return s is not None and (s in (-1, 0) or not start or s == start)

def named_job(text):
	"""(pid, start) of the job a lock's line names ('job=<job> pid=<pid> pidstart=<start>'), or None."""
	m = re.search(r"\bpid=(\d+) pidstart=(-?\d+)", text or "")
	return (int(m.group(1)), int(m.group(2))) if m else None

def job_runs(text):
	"""Whether the job a lock's line names still runs."""
	job = named_job(text)
	return bool(job) and alive(*job)

def cmdline(p):
	if WINDOWS:
		return rig_windows.command_line(p.pid, p.start) or p.cmd
	return p.cmd

# Other rig.py processes, by their markers (Windows). A python.exe counts as on the GPU unless it is another
# rig.py, which rig_windows.classify knows by its command line; but a rig.py at the desktop can't read the
# command line of a python.exe started over ssh (elevated) or by another user. On the ChronicRecord PC on
# 2026-11-20, a `rig.py status` run over ssh every 10 min was logged FOREIGN 11 times against one 2 h run.
# So every rig.py marks itself as it starts: a file ROOT/rig-processes/<pid>-<start> (start as the process
# table gives it, so a reused pid has no marker), which a watching rig.py lists at every look. A marker
# stays after its rig.py ends (a watcher may judge a python.exe it held back only at its next look: Watch.holds)
# and each rig.py removes those whose process ended over MARK_KEEP s ago.
MARKS = "rig-processes"
MARK_KEEP = 600.0
# s a python.exe whose command line can't be read and that has no marker is held back, before it counts, to
# show it is a rig.py by marking itself: one marks itself as it starts, a fraction of a second in
MARK_WAIT = float(os.environ.get("RIG_MARK_WAIT", 5.0))
FILETIME_EPOCH = 11644473600  # s from 1601-01-01, where Windows counts a process's start from, to 1970

def filetime_now():
	"""Now as Windows gives a process's start time: 100 ns units since 1601."""
	return int((time.time() + FILETIME_EPOCH) * 1e7)

def marks_folder():
	return os.path.join(ROOT, MARKS)

def rig_marks():
	"""{(pid, start)} of the rig.py processes that marked themselves (running, or ended lately)."""
	try:
		names = os.listdir(marks_folder())
	except OSError:
		return set()
	return {(int(m.group(1)), int(m.group(2))) for m in (re.match(r"(\d+)-(\d+)$", n) for n in names) if m}

def self_and_launchers(table=None):
	"""[(pid, start)] of this process and of each python.exe above it whose command line runs rig.py
	(a venv's python.exe starts the real one and waits for it), from the process table."""
	rows = {row[0]: row for row in (rig_windows.process_table() if table is None else table)}
	out, row = [], rows.get(os.getpid())
	while row and len(out) < 8:
		out.append((row[0], row[3]))
		parent = rows.get(row[1])
		if not parent or parent[3] > row[3] or not rig_windows.matches(parent[2], rig_windows.GPU_NAMES) or \
				not rig_windows.RIG_PY.search(rig_windows.command_line(parent[0], parent[3]) or ""):
			break
		row = parent
	return out

def mark_self(action):
	"""On Windows, mark this rig.py (and a launcher of it: self_and_launchers) in ROOT/rig-processes, then
	remove old markers (prune_marks). Only where ROOT exists; on any error rig.py goes unmarked, known by its
	command line only, as before markers: marking must never keep a rig.py (a panel's, a run's) from starting."""
	if not WINDOWS or not os.path.isdir(ROOT):
		return
	try:
		with contextlib.suppress(FileExistsError):
			os.mkdir(marks_folder())
		for pid, start in self_and_launchers():
			with open(os.path.join(marks_folder(), "%d-%d" % (pid, start)), "w", encoding="utf-8") as f:
				f.write("rig.py %s (pid %d) since %s\n" % (action, pid, time.strftime("%Y-%m-%d %H:%M:%S")))
		prune_marks()
	except Exception:  # noqa: BLE001 (see above)
		pass

def prune_marks(now=None):
	"""Remove the markers of rig.py processes that have ended, once a marker is MARK_KEEP s old."""
	now = time.time() if now is None else now
	for pid, start in rig_marks():
		path = os.path.join(marks_folder(), "%d-%d" % (pid, start))
		try:
			if now - os.stat(path).st_mtime >= MARK_KEEP and not alive(pid, start):
				os.remove(path)
		except OSError:
			pass

# The GPU, from nvidia-smi where there is one

NVSMI = shutil.which("nvidia-smi")
THROTTLE = ((0x1, "Idle"), (0x2, "Applications Clocks Setting"), (0x4, "SW Power Cap"), (0x8, "HW Slowdown"),
	(0x10, "Sync Boost"), (0x20, "SW Thermal Slowdown"), (0x40, "HW Thermal Slowdown"),
	(0x80, "HW Power Brake Slowdown"), (0x100, "Display Clock Setting"))
# Newer drivers call the reasons "event reasons"; the last query is for drivers with neither
CLOCK_QUERIES = ["index,clocks.sm,clocks_throttle_reasons.active,pstate",
	"index,clocks.sm,clocks_event_reasons.active,pstate", "index,clocks.sm,pstate"]

def nvsmi(*args):
	if not NVSMI:
		return None
	try:
		r = subprocess.run([NVSMI] + list(args), capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
	except (OSError, subprocess.SubprocessError):
		return None
	return r.stdout if r.returncode == 0 else None

def parse_gpu_processes(text):
	"""{pid: type} from nvidia-smi -q -d PIDS; type C (compute: CUDA), G (graphics) or C+G."""
	out, pid = {}, None
	for line in (text or "").splitlines():
		k, _, v = line.partition(":")
		k, v = k.strip(), v.strip()
		if k == "Process ID" and v.isdigit():
			pid = int(v)
		elif k == "Type" and pid is not None:
			out[pid], pid = v, None
	return out

def gpu_processes():
	"""{pid: type} of every process on an NVIDIA GPU. Under Windows' display driver this lists every
	program that draws (dwm.exe, explorer.exe, Task Manager: C+G) besides CUDA programs (C)."""
	return parse_gpu_processes(nvsmi("-q", "-d", "PIDS"))

# The GPUs one by one (one GPU at a time: --gpu). nvidia-smi numbers them in PCI bus order, which is also
# CUDA's with CUDA_DEVICE_ORDER=PCI_BUS_ID (CUDA's own default puts the fastest first); a job is shown its
# GPU by UUID, which means the same GPU whatever the order.
GPU = collections.namedtuple("GPU", "index uuid bus name")
_GPUS = []  # [the list], once nvidia-smi has been asked

def bus_key(text):
	"""A PCI bus id as numbers, the same however nvidia-smi writes it ('00000000:C1:00.0', '0000:c1:00.0')."""
	try:
		return tuple(int(x, 16) for x in re.split(r"[:.]", text.strip()))
	except ValueError:
		return text.strip().lower()

def parse_gpu_list(text):
	"""[GPU] from nvidia-smi --query-gpu=index,uuid,pci.bus_id,name --format=csv,noheader, by index."""
	out = []
	for line in (text or "").splitlines():
		f = [x.strip() for x in line.split(",", 3)]
		if len(f) == 4 and number(f[0]) is not None:
			out.append(GPU(number(f[0]), f[1], f[2], f[3]))
	return sorted(out)

def gpu_list():
	"""[GPU] of this machine's NVIDIA GPUs; [] without nvidia-smi. Asked once: they don't change while rig.py runs."""
	if not _GPUS:
		_GPUS.append(parse_gpu_list(nvsmi("--query-gpu=index,uuid,pci.bus_id,name", "--format=csv,noheader")))
	return _GPUS[0]

def parse_gpu_placement(text, gpus=()):
	"""{pid: {GPU index}} from nvidia-smi -q -d PIDS, which lists the processes GPU by GPU: each GPU a process runs
	CUDA on (type C or C+G; a program that only draws, G, is not on it for this). A GPU's section starts 'GPU
	<bus id>'; its index is gpus' (gpu_list()) for that bus id, or else its place in the output (by index)."""
	index = {bus_key(g.bus): g.index for g in gpus}
	out, gpu, pid, n = {}, None, None, -1
	for line in (text or "").splitlines():
		if line.startswith("GPU "):
			n += 1
			gpu, pid = index.get(bus_key(line[4:]), n), None
			continue
		k, _, v = line.partition(":")
		k, v = k.strip(), v.strip()
		if k == "Process ID" and v.isdigit():
			pid = int(v)
		elif k == "Type" and pid is not None:
			if "C" in v and gpu is not None:
				out.setdefault(pid, set()).add(gpu)
			pid = None
	return out

def gpu_snapshot():
	"""({pid: type}, {pid: {GPU index}}) from one nvidia-smi -q -d PIDS ({} and {} without nvidia-smi)."""
	text = nvsmi("-q", "-d", "PIDS")
	return parse_gpu_processes(text), parse_gpu_placement(text, gpu_list() if text else ())

def number(s, base=10):
	try:
		return int(s.strip(), base)
	except ValueError:
		return None

def parse_clocks(text, reasons=True):
	"""[(gpu index, SM MHz, throttle reasons bitmask, pstate)] from nvidia-smi's csv (noheader, nounits)."""
	out = []
	for line in (text or "").splitlines():
		f = [x.strip() for x in line.split(",")]
		if len(f) < 3 or number(f[0]) is None:
			continue
		mask = number(f[2], 16) if reasons and len(f) >= 4 else None
		out.append((number(f[0]), number(f[1]), mask, f[-1]))
	return out

def gpu_clocks():
	"""parse_clocks for every NVIDIA GPU now; [] without nvidia-smi."""
	while CLOCK_QUERIES:
		text = nvsmi("--query-gpu=" + CLOCK_QUERIES[0], "--format=csv,noheader,nounits")
		if text is not None:
			return parse_clocks(text, "reasons" in CLOCK_QUERIES[0])
		if not NVSMI or len(CLOCK_QUERIES) == 1:
			return []
		CLOCK_QUERIES.pop(0)  # this driver doesn't know a field: ask for less
	return []

def throttle_text(mask):
	if mask is None:
		return ""
	names = [name for bit, name in THROTTLE if mask & bit] + (["0x%x" % (mask & ~0x1FF)] if mask & ~0x1FF else [])
	return ", ".join(names) or "none"

def clock_text(row):
	i, mhz, mask, pstate = row
	return "GPU %s: SM clock %s MHz, %s%s" % (i, "?" if mhz is None else mhz, pstate,
		"" if mask is None else ", held down by: " + throttle_text(mask))

class Clocks:
	"""The GPUs' SM clock and throttle reasons through a run: a line when they change, a summary at the end."""

	def __init__(self, log):
		self.log, self.rows, self.logged = log, {}, {}

	def sample(self):
		now = time.monotonic()
		for row in gpu_clocks():
			i, mhz, mask, pstate = row
			self.rows.setdefault(i, []).append((mhz, mask))
			last = self.logged.get(i)
			if last is None or ((mask != last[1] or abs((mhz or 0) - (last[0] or 0)) >= 100) and now - last[2] >= 10):
				self.logged[i] = (mhz, mask, now)
				self.log(clock_text(row))

	def summary(self):
		"""(lines for the log, a short note for the last line)."""
		lines, notes = [], []
		for i, rows in sorted(self.rows.items()):
			mhz = sorted(m for m, _ in rows if m is not None)
			if not mhz:
				continue
			median = mhz[len(mhz) // 2]
			seen = {}
			for _, mask in rows:
				for bit, name in THROTTLE:
					if mask is not None and mask & bit:
						seen[name] = seen.get(name, 0) + 1
			held = ", ".join("%s %d%%" % (n, round(100.0 * c / len(rows))) for n, c in sorted(seen.items(), key=lambda x: -x[1]))
			lines.append("GPU %s over the run: SM clock median %d MHz, %d-%d MHz in %d looks; held down by: %s" % (
				i, median, mhz[0], mhz[-1], len(rows), held or ("none" if any(m is not None for _, m in rows) else "?")))
			notes.append("GPU %s SM median %d MHz" % (i, median))
		return lines, "; ".join(notes)

# A machine's known background load (ROOT/background.json): programs that always run there and that nobody
# can stop, above all an IT security agent, which on the Windows PCs reads 70-110 MB/s or uses 1-3 cores for a
# few seconds as a test starts. Each entry names the program (its file name or its full path, * and ? allowed,
# letter case ignored) and the most CPU (cores) and disk (MB/s) it may use:
#   {"processes": [{"name": "agent.exe", "cpu": 4, "io": 200, "why": "IT security agent"},
#                  {"path": "C:/Program Files/Vendor/*/scanner.exe", "cpu": 4, "io": 200}],
#    "share": 0.25, "looks": 3}
# Load from listed programs within their ceilings doesn't contaminate a run, for at most "share" of its looks
# or "looks" looks, whichever is more: a scan through a third of a run did slow it (the Pro Max Slim's p99
# 7.8 ms against 2.4 in the 2.5.0 round), so a run that background covers that much is still contaminated.
# Such a run ends RIG_CLEAN_BACKGROUND, saying what was seen. Load from anything else, or from a listed
# program above its ceiling, counts as before; device users always do. --strict ignores the file.

BACKGROUND = "background.json"
BACKGROUND_SHARE, BACKGROUND_LOOKS = 0.25, 3

def norm(text):
	"""A program's name or path, or a pattern for one, as compared: / for \\, lower case."""
	return (text or "").replace("\\", "/").strip().lower()

def first_word(cmd):
	"""The program a command line runs: its first word, or what its opening quotes hold."""
	cmd = (cmd or "").strip()
	if cmd.startswith('"'):
		return cmd[1:].split('"', 1)[0]
	return cmd.split(None, 1)[0] if cmd else ""

def program(p):
	"""(file name, full path or '') of p's program, normalised (norm). On Windows the name is the image's and
	the path is its command line's first word; on Linux the path is /proc/<pid>/exe (another user's process
	hides it: then the command line's first word)."""
	if WINDOWS:
		path = first_word(cmdline(p))
		return norm(p.cmd), norm(path if ("\\" in path or "/" in path) else "")
	if MACOS:  # ps's program path (every user's)
		path = getattr(p, "exe", None) or first_word(p.cmd)
		return norm(rig_macos.program_name(path)), norm(path if "/" in path else "")
	if p.cmd.startswith("[") and p.cmd.endswith("]"):  # no command line: [comm]
		return norm(p.cmd[1:-1]), ""
	try:
		path = os.readlink("/proc/%d/exe" % p.pid)
	except OSError:
		path = first_word(p.cmd)
	path = path[:-len(" (deleted)")] if path.endswith(" (deleted)") else path
	return norm(first_word(p.cmd).rsplit("/", 1)[-1]), norm(path if "/" in path else "")

class Background:
	"""The programs of background.json (entries: dicts with label, name, path, cpu, io), how much of a run
	they may cover (share of its looks, or looks), and what was wrong with the file (problems)."""

	def __init__(self, entries=(), share=BACKGROUND_SHARE, looks=BACKGROUND_LOOKS, problems=(), path=None):
		self.entries, self.share, self.looks = list(entries), share, looks
		self.problems, self.path = list(problems), path
		self.cache = {}  # (pid, start): entry index or None

	@classmethod
	def read(cls, root=None):
		"""ROOT/background.json; an empty list if there is none. A file that can't be read, or an entry
		without a program or a ceiling, excuses nothing (problems says why): a typo must not hide load."""
		path = os.path.join(root or ROOT, BACKGROUND)
		try:
			with open(path, encoding="utf-8") as f:
				cfg = json.load(f)
		except FileNotFoundError:
			return cls(path=path)
		except (OSError, ValueError) as e:
			return cls(problems=["%s can't be read (%s): no background excused" % (path, e)], path=path)
		if isinstance(cfg, list):
			cfg = {"processes": cfg}
		if not isinstance(cfg, dict) or not isinstance(cfg.get("processes", []), list):
			return cls(problems=["%s: not {\"processes\": [...]}: no background excused" % path], path=path)
		entries, problems = [], []
		for i, e in enumerate(cfg.get("processes", [])):
			try:
				if not isinstance(e, dict) or not (e.get("name") or e.get("path")):
					raise ValueError("no name or path")
				cpu, io = float(e["cpu"]), float(e["io"])
			except (KeyError, TypeError, ValueError) as err:
				problems.append("%s: entry %d (%s) left out: it needs a name or path, and cpu and io ceilings (%s)" % (
					path, i + 1, json.dumps(e)[:80], err))
				continue
			label = str(e.get("name") or e.get("path"))
			entries.append({"label": label, "name": norm(e.get("name")), "path": norm(e.get("path")), "cpu": cpu, "io": io,
				"why": str(e.get("why") or "")})
		try:
			share, looks = float(cfg.get("share", BACKGROUND_SHARE)), int(cfg.get("looks", BACKGROUND_LOOKS))
		except (TypeError, ValueError):
			problems.append("%s: share or looks isn't a number: the defaults (%s, %d) used" % (path, BACKGROUND_SHARE, BACKGROUND_LOOKS))
			share, looks = BACKGROUND_SHARE, BACKGROUND_LOOKS
		return cls(entries, share, looks, problems, path)

	def match(self, p):
		"""The index of the entry p's program is, or None."""
		key = (p.pid, p.start)
		if key not in self.cache:
			if len(self.cache) > 5000:
				self.cache.clear()
			name, path = program(p)
			self.cache[key] = next((i for i, e in enumerate(self.entries) if
				(e["name"] and fnmatch.fnmatchcase(name, e["name"])) or (e["path"] and path and fnmatch.fnmatchcase(path, e["path"]))), None)
		return self.cache[key]

	def allowed(self, looks):
		"""How many of a run's looks background load may cover."""
		return max(self.looks, int(self.share * looks))

	def text(self):
		"""What the log's start says about the list."""
		if not self.entries:
			return "none"
		return "%s (each within its ceiling, for up to %d%% of the run's looks or %d looks)" % ("; ".join(
			"%s up to %g cores, %g MB/s%s" % (e["label"], e["cpu"], e["io"], " (%s)" % e["why"] if e["why"] else "")
			for e in self.entries), round(100 * self.share), self.looks)

# One look's load outside the job: all of it (cpu cores, io MB/s), the processes with most ([(cores, MB/s,
# Proc)]), what all disks moved (MB/s, or None), what is left once listed background within its ceiling is
# taken out (rest_cpu, rest_io), and that background ([(label, cores, MB/s, within its ceiling)]). For a run
# holding one GPU, the jobs of runs holding other GPUs are not outside: their load is partner (cores, MB/s)
Load = collections.namedtuple("Load", "cpu io top disks rest_cpu rest_io background partner", defaults=(None,))

# The kinds that are the machine's, not a GPU's: a job holding one GPU uses none of them beside another
MACHINE_KINDS = {"camera", "trigger", "ephys"}

class Sides:
	"""Which run's job a process is in, among the runs holding single GPUs (holders, gpu_holders()): the GPU it
	holds, or None. A job's process carries its RIG_JOB (seen on Linux and macOS), or is one of those its rig.py
	lists in gpu<N>.pids (its Windows job object's, every look), or descends from one of them, from that
	rig.py or from the job's first process (a process started since the last list)."""

	def __init__(self, procs, holders):
		self.procs, self.holders, self.roots = procs, holders, {}
		for h in holders:
			keys = set(h["pids"]) | {k for k in (h["rig"], h["jobpid"]) if k}
			self.roots[h["gpu"]] = {pid for pid, start in keys if pid in procs and (start in (0, -1) or procs[pid].start == start)}
		self.rigs = {h["rig"][0]: h["gpu"] for h in holders if h["rig"]}

	def side(self, p):
		for h in self.holders:
			if h["job"] and p.job == h["job"]:
				return h["gpu"]
		for h in self.holders:
			if self.roots[h["gpu"]] and Watch.below(p, self.procs, self.roots[h["gpu"]]):
				return h["gpu"]
		return None

class Watch:
	"""Which processes are outside the job, and the CPU and disk they use. gpu: the GPU a run holding one GPU
	holds; partners: a function giving the runs holding other GPUs (gpu_holders()), whose jobs are not outside."""

	def __init__(self, job=None, sid=None, root_pid=None, background=None, allow=(), gpu=None, partners=None):
		self.job, self.sid, self.root_pid = job, sid, root_pid
		self.background = background
		self.allow = set(allow)  # kinds neither waited for nor counted (--allow): only noted, in self.allowed
		self.allowed = []  # outside the job at the last sample, using only allowed kinds
		self.me = {os.getpid()}
		self.prev, self.prev_t, self.prev_disk = {}, None, None
		self.desktops = {}  # (pid, start): Proc, every desktop up at the last sample
		self.ancestors = set()
		self.own_pids = set()
		self.own_keys = set()  # (pid, start) of the job's processes at the last sample (gpu<N>.pids), not rig.py's ancestors
		self.pending = {}  # (pid, start): (Proc, first seen): held back at the last sample (holds), not judged yet
		self.gpu, self.partners = gpu, partners
		self.off = []  # the job's processes on a GPU this run doesn't hold, at the last sample
		self.shared = {}  # {GPU: holder} of the runs on other GPUs whose job ran at the last sample

	def own(self, p, procs):
		if p.pid in self.me or (self.job and p.job == self.job) or (self.sid and p.sid == self.sid):
			return True
		if WINDOWS and p.pid in self.ancestors:  # what started rig.py waits for it, and may be a python.exe
			return True
		return self.below(p, procs, self.me | {self.root_pid})

	@staticmethod
	def below(p, procs, roots):
		"""Whether p is one of the pids in roots or descends from one."""
		q, seen = p, 0
		while q and seen < 64:
			if q.pid in roots:
				return True
			parent = procs.get(q.ppid)
			if parent and parent.start > q.start:  # a newer process that reused the parent's pid
				return False
			q, seen = parent, seen + 1
		return False

	def foreign(self, p):
		"""The kinds p uses that count against a run: all but desktop and the allowed ones. For a process of another
		GPU's run's job (Proc.partner), only this run's GPU and the machine's own devices (MACHINE_KINDS)."""
		if getattr(p, "partner", None) is not None:
			on_mine = self.gpu is not None and self.gpu in (getattr(p, "gpus", None) or ())
			return ((p.kinds & MACHINE_KINDS) | ({"gpu"} if on_mine else set())) - self.allow
		return p.kinds - {"desktop"} - self.allow

	def holds(self, p, t):
		"""Whether p, a python.exe whose command line can't be read and that has no rig.py marker (unsure), is
		held back from counting at this look (t: perf_counter): it may be a rig.py that started a moment ago,
		yet to mark itself. Held while it is under MARK_WAIT s old, and first seen under MARK_WAIT s ago (should
		the clock jump back); then it counts as any other. One that ends while held counts at the next look,
		unless it marked itself (sample). Never exempts it."""
		if not getattr(p, "unsure", False):
			return False
		first = self.pending.get((p.pid, p.start), (p, t))[1]
		return t - first < MARK_WAIT and filetime_now() - p.start < MARK_WAIT * 1e7

	def sample(self):
		"""(foreign device users, foreign load) now: [(Proc, kinds)], (cores, MB/s, [(cores, MB/s, Proc)],
		MB/s all disks moved or None)."""
		holders = self.partners() if self.partners else []
		# Windows' monotonic() ticks every 15.6 ms
		t = time.perf_counter()
		procs = scan(self.own_pids, gpus=True) if self.gpu is not None or holders else scan(self.own_pids)
		disk = disk_bytes()
		users, load, cpu, io, own_io = [], [], 0.0, 0.0, 0.0
		sides = Sides(procs, holders) if holders else None
		off, own_keys, shared, part_cpu, part_io = [], set(), {}, 0.0, 0.0
		self.allowed = []
		self.desktops = {(p.pid, p.start): p for p in procs.values() if "desktop" in p.kinds}
		self.ancestors, q = set(), procs.get(os.getpid())
		while q and q.ppid in procs and procs[q.ppid].start <= q.start and q.ppid not in self.ancestors:
			self.ancestors.add(q.ppid)
			q = procs[q.ppid]
		own, measured = set(), self.prev_t is not None and t > self.prev_t
		held = {}
		for p in procs.values():
			key, c, b = (p.pid, p.start), 0.0, 0.0
			if measured and key in self.prev:
				dt = t - self.prev_t
				pcpu, pio = self.prev[key]
				c = (p.cpu - pcpu) / dt
				b = (p.io - pio) / 1e6 / dt if p.io is not None and pio is not None else 0.0
			if self.own(p, procs):
				own.add(p.pid)
				if p.pid not in self.ancestors:  # what started rig.py (a shell, explorer.exe) is no job's to list
					own_keys.add(key)
				own_io += b
				if self.gpu is not None and set(getattr(p, "gpus", None) or ()) - {self.gpu}:
					off.append(p)
				continue
			side = sides.side(p) if sides else None
			if side is not None:  # another GPU's run's job: its load is its own, and only some uses count
				p.partner = side
				part_cpu, part_io = part_cpu + c, part_io + b
				if p.pid not in sides.rigs:
					shared[side] = next(h for h in holders if h["gpu"] == side)
				if self.foreign(p):
					users.append(p)
				continue
			if self.foreign(p):
				if self.holds(p, t):  # its load still counts
					held[key] = (p, self.pending.get(key, (p, t))[1])
				else:
					users.append(p)
			elif p.kinds & self.allow:
				self.allowed.append(p)
			cpu, io = cpu + c, io + b
			if c > 0.05 or b > 1:
				load.append((c, b, p))
		# One held back at the last look that has ended since: another rig.py if it marked itself, else it counts
		ended = [p for key, (p, first) in self.pending.items()
			if key not in held and (key[0] not in procs or procs[key[0]].start != key[1])]
		if ended:
			marked = rig_marks()
			users += [p for p in ended if (p.pid, p.start) not in marked]
		self.pending = held
		self.own_pids, self.own_keys, self.off, self.shared = own, own_keys, off, shared
		self.prev = {(p.pid, p.start): (p.cpu, p.io) for p in procs.values()}
		# Known background (background.json), each entry's processes together: within its ceilings it is
		# taken out of what is left (rest); above them it stays in, as anything else's load would. Only
		# processes in load (over 0.05 cores or 1 MB/s) are looked at: the rest of a listed program stays in
		bg, seen = self.background, {}
		for c, b, p in load if bg and bg.entries else ():
			i = bg.match(p)
			if i is not None:
				s = seen.setdefault(i, [0.0, 0.0])
				s[0], s[1] = s[0] + c, s[1] + b
		disks = None
		if measured and disk is not None and self.prev_disk is not None:
			disks = max(0.0, (disk - self.prev_disk) / 1e6 / (t - self.prev_t))
		# On Windows a program's count is all its I/O, disk or not (a security agent's reached 6,102 MB/s with
		# the disks at a few): against its ceiling goes at most what the disks moved
		background, bg_cpu, bg_io = [], 0.0, 0.0
		for i, (c, b) in sorted(seen.items()):
			e, disk_b = bg.entries[i], b if disks is None else min(b, disks)
			ok = c <= e["cpu"] and disk_b <= e["io"]
			background.append((e["label"], c, disk_b, ok))
			if ok:  # taken out of the load: all its CPU, and all its I/O as counted
				bg_cpu, bg_io = bg_cpu + c, bg_io + b
		rest_cpu, rest_io = cpu - bg_cpu, io - bg_io
		if disks is not None:
			# What outside programs read and wrote, as far as it reached a disk; and at least what the
			# disks moved beyond the job's own reads and writes, which catches writes that Windows'
			# file cache took and the kernel wrote out later, after their program had gone. What is left
			# once the background is out: the same, with its reads and writes taken off the disks' too. Another
			# GPU's run's job's reads and writes are its own, as the job's are
			io = max(min(io, disks), disks - own_io - part_io)
			rest_io = max(min(rest_io, disks), disks - own_io - part_io - bg_io)
			part_io = min(part_io, disks)
		self.prev_disk = disk
		self.prev_t = t
		load.sort(key=lambda x: -(x[0] + x[1] / 50))
		return users, Load(cpu, io, load, disks, max(0.0, rest_cpu), max(0.0, rest_io), background,
			(part_cpu, part_io) if holders else None) if measured else None

def describe(p, width=150):
	gpus, side = getattr(p, "gpus", None), getattr(p, "partner", None)
	return "%d %s %s%s%s%s" % (p.pid, ",".join(sorted(p.kinds)) or "-", cmdline(p)[:width],
		" (command line unreadable, no rig.py marker)" if getattr(p, "unsure", False) else "",
		" [on GPU %s]" % ",".join(str(i) for i in sorted(gpus)) if gpus else "",
		" [the job of the run holding GPU %d]" % side if side is not None else "")

def desktop_text(desktops, short=False):
	if WINDOWS:
		names = sorted(set(p.cmd for p in desktops.values()), key=str.lower)
		return "none" if not names else "%d processes" % len(desktops) if short else "%d processes: %s" % (
			len(desktops), ", ".join(names))
	return ", ".join(sorted(set("%s (%s%s)" % (p.cmd.split()[0].rsplit("/", 1)[-1], p.user or "?",
		", the login screen" if p.user == "gdm" else "") for p in desktops.values()))) or "none"

def background_text(background):
	return "; ".join("%s %.1f cores %.0f MB/s%s" % (label, c, b, "" if ok else " ABOVE its ceiling")
		for label, c, b, ok in background)

def load_text(load, n=3):
	cpu, io, top, disks = load[:4]
	text = "cpu %.1f cores, disk %.0f MB/s%s; %s" % (cpu, io, "" if disks is None else " (all disks %.0f)" % disks, "; ".join(
		"%d %.1f cores %.0f MB/s %s" % (p.pid, c, b, cmdline(p)[:80]) for c, b, p in top[:n]) or "nothing")
	background = getattr(load, "background", None)
	if background:
		text += "; background (background.json): %s; the rest: cpu %.1f cores, disk %.0f MB/s" % (
			background_text(background), load.rest_cpu, load.rest_io)
	return text

def overloaded(load, args):
	"""All the load outside the job is above --cpu or --io (a LOAD line in the log)."""
	return load is not None and (load[0] > args.cpu or load[1] > args.io)

def foreign_load(load, args):
	"""What is left of the load once listed background within its ceiling is out is above --cpu or --io; with
	--strict, all of it."""
	if getattr(args, "strict", False) or not hasattr(load, "rest_cpu"):
		return overloaded(load, args)
	return load is not None and (load.rest_cpu > args.cpu or load.rest_io > args.io)

def streak(flags):
	"""The most True in a row."""
	best = run = 0
	for f in flags:
		run = run + 1 if f else 0
		best = max(best, run)
	return best

def looks_text(n, period=None):
	period = PERIOD if period is None else period
	return "%d look%s (%s s)" % (n, "" if n == 1 else "s", "%g" % round(n * period, 1))

class LoadTrack:
	"""The CPU and disk load outside the job, look by look through a run, for its last line.

	A look is 'load' when what is left once listed background is out is above --cpu or --io, 'background'
	when only listed background (within its ceilings) took the load above them. Load counts (contaminates)
	only when it lasts: --load-looks looks in a row (2), or more than --load-share of the run's looks (0.2);
	a moment of it is logged and named on the last line, but the run stays clean. Background contaminates
	only when it covers more of the run than background.json allows. --strict: every look above the limits
	counts at once, background or not, as before 13 November 2026."""

	def __init__(self, args, background=None):
		self.args, self.bg = args, background or Background()
		self.strict = getattr(args, "strict", False)
		self.need, self.share = getattr(args, "load_looks", 2), getattr(args, "load_share", 0.2)
		self.states = []  # per look: None, "load" or "background"
		self.peak = [0.0, 0.0]  # the most cores and MB/s of 'load' looks (what is left)
		self.bg_peak = {}  # label: [cores, MB/s] at most, within its ceiling

	def add(self, load):
		if load is None:
			return None
		state = None
		if foreign_load(load, self.args):
			state = "load"
			cpu, io = (load.cpu, load.io) if self.strict else (load.rest_cpu, load.rest_io)
			self.peak = [max(self.peak[0], cpu), max(self.peak[1], io)]
		elif overloaded(load, self.args):
			state = "background"
			for label, c, b, ok in load.background:
				if ok:
					p = self.bg_peak.setdefault(label, [0.0, 0.0])
					p[0], p[1] = max(p[0], c), max(p[1], b)
		self.states.append(state)
		return state

	def verdict(self):
		"""(load sightings that count, background contaminates, notes for the last line)."""
		looks = len(self.states)
		n = self.states.count("load")
		longest = streak(s == "load" for s in self.states)
		notes, counted = [], 0
		if n:
			lasts = self.strict or longest >= self.need or n > self.share * looks
			counted = n if lasts else 0
			notes.append("%sload outside the job in %d of %d looks, longest %s in a row, peak %.1f cores, %.0f MB/s%s" % (
				"" if lasts else "brief ", n, looks, looks_text(longest), self.peak[0], self.peak[1],
				"" if lasts else " (not counted: under %d looks in a row and %d%% of the run)" % (
					self.need, round(100 * self.share))))
		b = self.states.count("background")
		too_much = False
		if b:
			allowed = self.bg.allowed(looks)
			too_much = b > allowed
			notes.append("background (background.json) above the limits in %d of %d looks, longest %s in a row%s: %s" % (
				b, looks, looks_text(streak(s == "background" for s in self.states)),
				", more than the %d allowed" % allowed if too_much else "",
				", ".join("%s peak %.1f cores, %.0f MB/s" % (k, c, io) for k, (c, io) in self.bg_peak.items())))
		return counted, too_much, notes

# The locks. Each has acquire (wait for it, or exit 75), record (say who holds it and for which job),
# ours (still held by this rig.py), release, and state/describe for status and break.

ENDED = " (its rig.py has ended, but its job runs on"

class Flock:
	"""Linux: a flock on LOCK while a run holds the rig (released by the kernel if rig.py dies), and
	RIG_BUSY saying who, from the moment its rig.py has the flock, and the pid and start of its job
	('<owner> <tag> since <date time> rigpid=<pid> [job=<job> pid=<pid> pidstart=<t>]'). A rig.py that
	was killed (SIGKILL, the OOM killer) loses its flock but leaves its job running, so the rig stays
	held, by RIG_BUSY, until that job ends (as FileLock does). It holds the whole machine only: one GPU at a
	time needs the file lock."""

	gpu = None

	def __init__(self):
		self.fd, self.orphan = None, False

	def tick(self, own):
		pass

	def text(self):
		try:
			with open(INFO) as f:
				return f.read().strip()
		except OSError:
			return ""

	def state(self):
		"""('held' or 'free', what RIG_BUSY says). Held while a rig.py holds the flock, or while the job
		RIG_BUSY names runs on after its rig.py ended (self.orphan)."""
		os.makedirs(ROOT, exist_ok=True)
		fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o664)
		try:
			fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
			held = False
		except OSError:
			held = True
		finally:
			os.close(fd)
		text = self.text()
		self.orphan = not held and job_runs(text)
		return ("held" if held or self.orphan else "free"), text

	def describe(self, state, text):
		text = text or "(no RIG_BUSY)"
		if state == "held" and self.orphan and ENDED not in text:
			text += ENDED + "; the rig is free once it ends)"
		return text

	def acquire(self, owner, tag, no_wait):
		os.makedirs(ROOT, exist_ok=True)
		self.owner, self.tag = owner, tag
		self.fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o664)
		try:
			fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except OSError:
			if no_wait:
				say("rig busy: %s" % (self.text() or "(no RIG_BUSY)"))
				sys.exit(75)
			say("rig busy: %s; waiting..." % (self.text() or "(no RIG_BUSY)"))
			fcntl.flock(self.fd, fcntl.LOCK_EX)
		# The flock is ours. But a rig.py that was killed lost its flock and left its job running: the rig
		# is that job's until it ends. RIG_BUSY goes on naming it (a guarded script it starts looks for its
		# job there), and says who waits for it
		text, said, self.orphan = self.text(), None, True
		while job_runs(text):
			if no_wait:
				say("rig busy: %s" % self.describe("held", text))
				os.close(self.fd)
				self.fd = None
				sys.exit(75)
			if said is None:
				self.write(text.split(ENDED)[0] + ENDED + ", and %s %s waits for it since %s rigpid=%d)" % (
					owner, tag, time.strftime("%F %T"), os.getpid()))
			if said is None or time.monotonic() - said > 300:
				say("rig busy: %s; waiting..." % self.describe("held", text))
				said = time.monotonic()
			time.sleep(PERIOD)
			text = self.text()
		self.orphan = False
		self.since = time.strftime("%F %T")
		self.record()

	def record(self, job=None, pid=None, note=""):
		"""pid None: the job is about to start (its name is known before its pid)."""
		line = "%s %s since %s rigpid=%d" % (self.owner, self.tag, self.since, os.getpid())
		if job:
			line += " job=%s %s" % (job, "starting" if pid is None else "pid=%d pidstart=%d" % (pid, proc_start(pid) or 0))
		self.write(line + note)

	def write(self, line):
		tmp = INFO + ".tmp"
		with open(tmp, "w") as f:
			f.write(line + "\n")
		os.replace(tmp, INFO)

	def ours(self):
		return True

	def clear(self):
		try:
			with open(INFO, "w"):
				pass
		except OSError:
			pass

	def clear_stale(self):
		"""Empty RIG_BUSY if nothing holds the rig (status, break): under the flock, so that the line of a
		rig.py taking the rig this moment stays. False if the rig is held."""
		fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o664)
		try:
			try:
				fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
			except OSError:
				return False
			if job_runs(self.text()):
				return False
			self.clear()
			return True
		finally:
			os.close(fd)

	def release(self):
		if self.fd is not None:
			self.clear()
			os.close(self.fd)
			self.fd = None

@contextlib.contextmanager
def guarded(path):
	"""Only one rig.py at a time between reading the lock file and replacing it: an OS lock on
	<path>.guard, which the OS releases if rig.py dies."""
	fd = os.open(path + ".guard", os.O_RDWR | os.O_CREAT, 0o664)
	try:
		if WINDOWS:
			while True:
				try:
					msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
					break
				except OSError:
					time.sleep(0.05)
		else:
			fcntl.flock(fd, fcntl.LOCK_EX)
		yield
	finally:
		if WINDOWS:
			try:
				os.lseek(fd, 0, 0)
				msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
			except OSError:
				pass
		os.close(fd)

def retry(f, *args):
	"""f(*args), again for up to 2 s while Windows says another process has the file open."""
	for i in range(40):
		try:
			return f(*args)
		except PermissionError:
			if i == 39:
				raise
			time.sleep(0.05)

# The file lock's other files, in ROOT beside gpu.lock (one GPU at a time, and who waits; see the docstring)
SHARED = "per-gpu"  # the first word of gpu.lock's line while runs hold single GPUs
QUEUE = "rig-queue"  # one ticket per waiting rig.py (or rig_hold.ps1): '<time ns since 1970, 20 digits>-<pid>'

def read_text(path):
	"""A lock file's line; None if there is no file, '' if it can't be read this moment (being written or deleted)."""
	try:
		with open(path, encoding="utf-8", errors="replace") as f:
			return f.read().strip()
	except FileNotFoundError:
		return None
	except OSError:
		return ""

def file_age(path):
	try:
		return time.time() - os.path.getmtime(path)
	except OSError:
		return 0.0

def write_line(path, line):
	def write():
		with open(path, "w", encoding="utf-8") as f:
			f.write(line + "\n")
	retry(write)

def line_state(text, age):
	"""A lock line's state, as rig.py has always read gpu.lock: 'free' (no file), 'held' (its rig.py or its job
	runs), 'stale' (both have ended), 'hand' (no rigpid: written by hand; an empty one over 60 s old is stale)."""
	if text is None:
		return "free"
	m = re.search(r"\brigpid=(\d+) rigstart=(-?\d+)", text)
	if not m:
		return "stale" if text == "" and age > 60 else "hand"
	pids = [(int(m.group(1)), int(m.group(2)))] + ([named_job(text)] if named_job(text) else [])
	return "held" if any(alive(pid, start) for pid, start in pids) else "stale"

def is_shared(text):
	return bool(text) and text.split(" ", 1)[0] == SHARED

def lock_holder(text):
	"""What a lock line says: owner, tag, since, job, rig (rig.py's (pid, start)) and jobpid (the job's, or None)."""
	text = text or ""
	words = text.split()
	m = re.search(r"\brigpid=(\d+) rigstart=(-?\d+)", text)
	job, since = re.search(r"\bjob=(\S+)", text), re.search(r"\bsince (\S+ \S+)", text)
	return {"owner": words[0] if words else "?", "tag": words[1] if len(words) > 1 else "?",
		"since": since.group(1) if since else "?", "job": job.group(1) if job else None,
		"rig": (int(m.group(1)), int(m.group(2))) if m else None, "jobpid": named_job(text)}

def gpu_lock_path(i):
	return os.path.join(ROOT, "gpu%d.lock" % i)

def gpu_pids_path(i):
	return os.path.join(ROOT, "gpu%d.pids" % i)

def gpu_locks():
	"""[(GPU, path, state, line)] of every gpu<N>.lock (line_state), by GPU."""
	try:
		names = os.listdir(ROOT)
	except OSError:
		return []
	out = []
	for name in names:
		m = re.match(r"gpu(\d+)\.lock$", name)
		if m:
			path = os.path.join(ROOT, name)
			text = read_text(path)
			if text is not None:
				out.append((int(m.group(1)), path, line_state(text, file_age(path)), text))
	return sorted(out)

def published(i):
	"""{(pid, start)} of the job's processes that the run holding GPU i lists in gpu<i>.pids."""
	out = set()
	for line in (read_text(gpu_pids_path(i)) or "").splitlines():
		f = line.split()
		if len(f) == 2 and f[0].isdigit() and re.match(r"-?\d+$", f[1]):
			out.add((int(f[0]), int(f[1])))
	return out

def gpu_holders():
	"""The runs holding single GPUs: [lock_holder(line) plus gpu, line, state ('held', or 'hand' for a line being
	written) and pids (published)], by GPU."""
	out = []
	for i, path, state, text in gpu_locks():
		if state in ("held", "hand"):
			h = lock_holder(text)
			h.update(gpu=i, line=text, state=state, pids=published(i))
			out.append(h)
	return out

def holders_but(gpu):
	"""A function giving the runs holding GPUs other than gpu (Watch's partners)."""
	return lambda: [h for h in gpu_holders() if h["gpu"] != gpu and h["state"] == "held"]

def shared_line(holders):
	"""gpu.lock's line while runs hold single GPUs (holders, all 'held'), or None without one. Read as rig.py has
	always read gpu.lock (by rig_hold.ps1, a GPU queue, an older rig.py, rig_guard's check for its job=), it holds
	the machine while a holder runs: its rigpid and pid are the one holder's rig.py and job, or a live process of
	each of the first two (no more fit; each gpu<N>.lock still holds its own GPU), and it names every holder's job=."""
	if not holders:
		return None
	if len(holders) == 1:
		pair = [holders[0]["rig"], holders[0]["jobpid"]]
	else:
		pair = [h["rig"] if h["rig"] and alive(*h["rig"]) else (h["jobpid"] or h["rig"]) for h in holders[:2]]
	pair = [x for x in pair if x]
	if not pair:
		return None
	def clean(text):
		return str(text).replace("=", "-")  # nothing in the list that a reader would take for a pid=
	line = "%s %s since %s rigpid=%d rigstart=%d" % (SHARED, "+".join(str(h["gpu"]) for h in holders),
		min(h["since"] for h in holders), pair[0][0], pair[0][1])
	if len(pair) > 1:
		line += " pid=%d pidstart=%d" % pair[1]
	return line + " (GPUs held one at a time, each in its gpu<N>.lock: %s)" % "; ".join("GPU %d %s %s%s" % (
		h["gpu"], clean(h["owner"]), clean(h["tag"]), " job=%s " % h["job"] if h["job"] else "") for h in holders)

def queue_folder():
	return os.path.join(ROOT, QUEUE)

def tickets(prune=False):
	"""[(name, fields)] of who waits in line, oldest first: the tickets whose rig.py (or rig_hold.ps1) still runs.
	prune (under the guard): remove the others."""
	try:
		names = sorted(n for n in os.listdir(queue_folder()) if re.match(r"\d+-\d+$", n))
	except OSError:
		return []
	out = []
	for name in names:
		path = os.path.join(queue_folder(), name)
		f = fields(read_text(path) or "")
		try:
			who = (int(f["rigpid"]), int(f["rigstart"]))
		except (KeyError, ValueError):
			who = None
		if (who and alive(*who)) or (who is None and file_age(path) < 60):
			out.append((name, f))
		elif prune:
			with contextlib.suppress(OSError):
				os.remove(path)
	return out

def want_text(want):
	return "the whole machine" if want in ("whole", None) else "GPU %s" % want[3:]

def conflicts(a, b):
	"""Whether two waiters want the same: the whole machine wants every GPU."""
	return a == "whole" or b == "whole" or a == b

def ticket_text(f):
	return "%s %s for %s, waiting since %s" % (f.get("owner", "?"), f.get("tag", "?"), want_text(f.get("want", "whole")),
		f.get("since", "?"))

class FileLock:
	"""Windows (or RIG_LOCK=file): ROOT/gpu.lock, created only if it is absent (O_EXCL, CreateNew),
	one line long: '<owner> <tag> since <date time> rigpid=<pid> rigstart=<t> [job=<job> pid=<pid> pidstart=<t>]'.
	It counts as free once both pids have ended, so a crash leaves nothing to clean up. A line
	without rigpid was written by hand, and holds the rig until its writer deletes it (or break).

	gpu: hold that GPU alone, in ROOT/gpu<N>.lock (the same line), while gpu.lock carries the per-gpu line
	(shared_line), kept current under the guard by every rig.py that looks (sync). Every lock file changes under
	one guard, gpu.lock.guard's (as rig_hold.ps1 and the GPU queue take it). A waiting rig.py has a ticket in
	ROOT/rig-queue, and takes what it waits for only when no earlier ticket wants the same (conflicts)."""

	def __init__(self, gpu=None):
		self.path = os.path.join(ROOT, "gpu.lock")
		self.gpu = gpu
		self.mine = self.path if gpu is None else gpu_lock_path(gpu)  # the file this lock holds
		self.want = "whole" if gpu is None else "gpu%d" % gpu
		self.line = self.ticket = self.published = None

	def text(self):
		"""gpu.lock's line; None if there is no lock."""
		return read_text(self.path)

	def age(self):
		return file_age(self.path)

	def state(self):
		"""(state, line) of the machine: 'free', 'held' (a live rig.py or job), 'stale' (both ended), 'hand' (written
		by hand), or 'shared': runs hold single GPUs, and line is what gpu.lock says of them (shared_line)."""
		text = self.text()
		if text is not None and not is_shared(text):
			return line_state(text, self.age()), text
		holders = gpu_holders()
		if holders:
			return "shared", shared_line([h for h in holders if h["state"] == "held"]) or text or holders[0]["line"]
		return ("free", "") if text is None else ("stale", text)

	def describe(self, state, text):
		if state == "hand":
			return "%s (gpu.lock written by hand %s, %.1f h ago: it holds the rig until whoever wrote it " \
				"deletes it, or rig.py break)" % (repr(text), time.strftime("%F %T", time.localtime(time.time() - self.age())),
				self.age() / 3600)
		return text

	def sync(self):
		"""Under the guard: remove the GPUs' locks whose rig.py and job have ended (and their gpu<N>.pids), and the
		tickets of rig.py processes that have ended; then make gpu.lock say what the GPUs' locks hold (shared_line),
		or remove its per-gpu line once none is held. A whole-machine line in gpu.lock is left alone. Returns what
		it removed, to say."""
		notes = []
		for i, path, state, text in gpu_locks():
			if state == "stale":
				notes.append("%s stale gpu%d.lock removed (its rig.py and job have ended): %s" % (time.strftime("%T"), i, text))
				retry(os.remove, path)
				with contextlib.suppress(OSError):
					os.remove(gpu_pids_path(i))
		tickets(prune=True)
		want = shared_line([h for h in gpu_holders() if h["state"] == "held"])
		text = self.text()
		if want and text is None:
			try:
				os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o664))
			except FileExistsError:  # written this moment by something that doesn't use the guard
				return notes
			write_line(self.path, want)
		elif want and is_shared(text) and text != want:
			write_line(self.path, want)
		elif not want and is_shared(text) and not gpu_holders():
			retry(os.remove, self.path)
		return notes

	def ahead(self):
		"""The tickets before this rig.py's (all of them, if it has none) that want what it wants, or the whole machine."""
		me = str(os.getpid())
		return [f for name, f in tickets() if (self.ticket is None or name < self.ticket) and f.get("rigpid") != me
			and conflicts(self.want, f.get("want", "whole"))]

	def try_take(self):
		"""Under the guard: take what this lock is for, if it is free and nobody earlier in line wants it too. None
		once taken; else what keeps it, for 'rig busy: ...'."""
		text = self.text()
		if text is not None and not is_shared(text) and line_state(text, self.age()) == "stale":
			say("%s stale gpu.lock removed (its rig.py and job have ended): %s" % (time.strftime("%T"), text))
			retry(os.remove, self.path)
		for note in self.sync():
			say(note)
		text = self.text()
		if text is not None and not is_shared(text):  # the whole machine is held, or gpu.lock was written by hand
			return self.describe(line_state(text, self.age()), text)
		holders = gpu_holders()
		if self.gpu is None and holders:
			return self.state()[1]
		mine = [h for h in holders if h["gpu"] == self.gpu]
		if mine:
			return "GPU %d: %s" % (self.gpu, mine[0]["line"])
		ahead = self.ahead()
		if ahead:
			return "%s is free, but in line before this run: %s" % (want_text(self.want), "; ".join(ticket_text(f) for f in ahead))
		try:
			fd = os.open(self.mine, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o664)
		except FileExistsError:  # written this moment by something that doesn't use the guard
			return self.describe("hand", read_text(self.mine) or "")
		os.close(fd)
		self.since = time.strftime("%F %T")
		self._record()
		if self.gpu is not None:
			self.sync()
			if not is_shared(self.text()):  # gpu.lock written this moment by something without the guard
				retry(os.remove, self.mine)
				self.line = None
				return self.describe("hand", self.text() or "")
		return None

	def write_ticket(self):
		"""Stand in line: ROOT/rig-queue/<now in ns>-<pid>, saying what this rig.py waits for."""
		os.makedirs(queue_folder(), exist_ok=True)
		name = "%020d-%d" % (time.time_ns(), os.getpid())
		path = os.path.join(queue_folder(), name)
		def clean(text):
			return re.sub(r"[;=\s]", "-", str(text))
		with open(path + ".tmp", "w", encoding="utf-8") as f:
			f.write("want=%s; owner=%s; tag=%s; since=%s; rigpid=%d; rigstart=%d\n" % (self.want, clean(self.owner),
				clean(self.tag), time.strftime("%F %T"), os.getpid(), proc_start(os.getpid()) or 0))
		retry(os.replace, path + ".tmp", path)
		self.ticket = name

	def drop_ticket(self):
		if self.ticket:
			with contextlib.suppress(OSError):
				os.remove(os.path.join(queue_folder(), self.ticket))
			self.ticket = None

	def acquire(self, owner, tag, no_wait):
		os.makedirs(ROOT, exist_ok=True)
		self.owner, self.tag, said, last = owner, tag, None, None
		try:
			while True:
				with guarded(self.path):
					busy = self.try_take()
					if busy is None:
						self.drop_ticket()
						return
					if not no_wait and self.ticket is None:
						self.write_ticket()
				if no_wait:
					say("rig busy: %s" % busy)
					sys.exit(75)
				if busy != last or time.monotonic() - said > 300:
					say("rig busy: %s; waiting..." % busy)
					said, last = time.monotonic(), busy
				time.sleep(PERIOD)
		except BaseException:
			self.drop_ticket()
			raise

	def _record(self, job=None, pid=None, note=""):
		line = "%s %s since %s rigpid=%d rigstart=%d" % (self.owner, self.tag, self.since, os.getpid(), proc_start(os.getpid()) or 0)
		if job:  # pid None: the job is about to start
			line += " job=%s %s" % (job, "starting" if pid is None else "pid=%d pidstart=%d" % (pid, proc_start(pid) or 0))
		write_line(self.mine, line + note)
		self.line = line + note

	def record(self, job=None, pid=None, note=""):
		if self.gpu is None:
			self._record(job, pid, note)
			return
		with guarded(self.path):  # gpu.lock's per-gpu line names the job too
			self._record(job, pid, note)
			self.sync()

	def tick(self, own):
		"""Every look of a run holding one GPU: list its job's processes (own: {(pid, start)}) in gpu<N>.pids, for the
		runs on other GPUs to know them by, and keep gpu.lock's line current (a holder whose rig.py was killed)."""
		if self.gpu is None or self.line is None:
			return
		with contextlib.suppress(OSError):
			text = "".join("%d %d\n" % k for k in sorted(own))
			if text != self.published:
				path = gpu_pids_path(self.gpu)
				tmp = "%s.%d.tmp" % (path, os.getpid())
				with open(tmp, "w", encoding="utf-8") as f:
					f.write(text)
				retry(os.replace, tmp, path)
				self.published = text
			with guarded(self.path):
				self.sync()

	def ours(self):
		"""Whether the lock is still this run's; for one GPU, also whether gpu.lock still says GPUs are held one at
		a time (a whole-machine line written over it means someone took the machine meanwhile)."""
		if self.line is None:
			return True
		if read_text(self.mine) not in (self.line, ""):
			return False
		text = self.text() if self.gpu is not None else None
		return text is None or text == "" or is_shared(text)

	def release(self):
		self.drop_ticket()
		if self.line is None:
			return
		with guarded(self.path):
			text = read_text(self.mine)
			if text == self.line:
				retry(os.remove, self.mine)
			elif text is not None:
				say("%s is no longer this run's, so it was left as it is: %s" % (os.path.basename(self.mine), text))
			if self.gpu is not None:
				with contextlib.suppress(OSError):
					os.remove(gpu_pids_path(self.gpu))
				self.sync()
		self.line = None

def file_lock():
	"""Whether this machine's lock is the file lock (gpu.lock), not the flock."""
	return WINDOWS or os.environ.get("RIG_LOCK") == "file"

def make_lock(gpu=None):
	"""The machine's lock; gpu: the file lock for that GPU alone."""
	return FileLock(gpu) if file_lock() else Flock()

WAITING = " (waiting for the rig to be quiet)"

def background_for(args):
	"""The machine's background list (background.json), or none with --strict."""
	return Background() if getattr(args, "strict", False) else Background.read()

def allowed_kinds(args):
	"""The kinds --allow (RIG_ALLOW) leaves out: neither waited for nor counted."""
	return set(getattr(args, "allow", None) or ())

def parse_allow(text):
	kinds = {k.strip().lower() for k in (text or "").split(",") if k.strip()}
	bad = kinds - set(KINDS)
	if bad:
		raise argparse.ArgumentTypeError("not a kind: %s (the kinds: %s)" % (", ".join(sorted(bad)), ", ".join(KINDS)))
	return kinds

def log_rules(log, args, bg):
	"""The log says, before the job starts, what will count against the run."""
	if allowed_kinds(args):
		log("allowed outside the job, not waited for and not counted (--allow): %s" % ", ".join(sorted(allowed_kinds(args))))
	if getattr(args, "strict", False):
		log("--strict: any look with load outside the job above %g cores or %g MB/s contaminates the run; "
			"background.json is not used" % (args.cpu, args.io))
		return
	log("load outside the job above %g cores or %g MB/s counts when it lasts %d looks in a row or more than %d%% "
		"of the run's looks; known background (%s): %s" % (args.cpu, args.io, getattr(args, "load_looks", 2),
		round(100 * getattr(args, "load_share", 0.2)), BACKGROUND, bg.text()))
	for problem in bg.problems:
		log(problem)

def wait_quiet(w, args):
	"""Wait until nothing outside w's job uses a device or loads the CPU or disk; exit 75 instead with --no-wait.
	The rig is ours by then, but something started without rig.py may still be at work."""
	said = 0
	w.sample()
	while True:
		time.sleep(PERIOD)
		users, load = w.sample()
		if not users and not foreign_load(load, args):
			if not w.pending:
				return
			continue  # a python.exe that may be a rig.py yet to mark itself: judged within MARK_WAIT s, --no-wait too
		if args.no_wait:
			say("rig in use outside rig.py:")
			for p in users:
				say("  " + describe(p))
			say("  load: " + load_text(load))
			sys.exit(75)
		if time.monotonic() - said > 30:
			said = time.monotonic()
			say("%s in use outside rig.py, waiting:" % time.strftime("%H:%M:%S"))
			for p in users:
				say("  " + describe(p))
			if foreign_load(load, args):
				say("  load: " + load_text(load))

def watch(w, alive, log, args, on_signal, lock, stopper=None):
	"""Log what is outside the job every PERIOD s until alive() is False; returns the sighting counts.
	With a stopper (an experiment machine), it ends as soon as the stopper says why, its STOP file
	looked for every 0.2 s. Load outside the job counts as LoadTrack says."""
	counts = dict.fromkeys(KINDS + ("desktop", "load", "lock") + (("wrong-gpu",) if w.gpu is not None else ()), 0)
	track = LoadTrack(args, w.background)
	# A run holding one GPU: the looks at which another GPU's run's job ran beside it, who, and its load at most
	shared, looks, beside = {"looks": 0, "who": {}, "peak": [0.0, 0.0]}, 0, {}
	last = {}  # what was logged when, so a process that stays is logged every 30 s, not every 2 s
	def note(key, text):
		now = time.monotonic()
		if now - last.get(key, -1e9) >= 30:
			last[key] = now
			log(text)
	clocks = Clocks(log)
	w.sample()
	before = dict(w.desktops)
	log("desktop on the GPU: %s" % desktop_text(before))
	while alive():
		users, load = w.sample()
		looks += 1
		clocks.sample()
		if w.gpu is not None:
			lock.tick(w.own_keys)
			for p in w.off:
				counts["wrong-gpu"] += 1
				note(("off", p.pid, p.start), "WRONG GPU the job runs CUDA on GPU %s, which this run doesn't hold (it holds GPU %d): %s" % (
					",".join(str(i) for i in sorted(p.gpus)), w.gpu, describe(p)))
			for side in sorted(set(beside) ^ set(w.shared)):
				h = w.shared.get(side) or beside[side]
				log("SHARED %s: the job of the run holding GPU %d (%s %s, job %s)" % (
					"from now" if side in w.shared else "no more", side, h["owner"], h["tag"], h["job"]))
			beside = dict(w.shared)
			if beside:
				shared["looks"] += 1
				for side, h in beside.items():
					shared["who"][side] = "%s %s on GPU %d" % (h["owner"], h["tag"], side)
				part = getattr(load, "partner", None) if load is not None else None
				if part:
					shared["peak"] = [max(shared["peak"][0], part[0]), max(shared["peak"][1], part[1])]
		for key in set(before) ^ set(w.desktops):
			if not WINDOWS:  # Windows starts and ends programs that draw (WebView, search) by itself
				counts["desktop"] += 1
			p = w.desktops.get(key) or before[key]
			log("DESKTOP %s: %s" % ("started" if key in w.desktops else "ended", describe(p)))
		before = dict(w.desktops)
		for p in users:
			for k in w.foreign(p):
				counts[k] += 1
			note(("dev", p.pid, p.start), "FOREIGN %s" % describe(p))
		for p in w.allowed:
			if ("allowed", p.pid, p.start) not in last:  # once each: it is no contamination
				last[("allowed", p.pid, p.start)] = time.monotonic()
				log("ALLOWED (--allow) %s" % describe(p))
		if track.add(load):
			note(("load",), "LOAD %s" % load_text(load))
		if not lock.ours():
			counts["lock"] += 1
			note(("lock",), "LOCK changed by someone else during the run: %s" % lock.text())
		if on_signal[0]:
			break
		if stopper is None:  # a signal (a console closing: seconds to go) ends the wait at once
			t = time.monotonic() + PERIOD
			while time.monotonic() < t and not on_signal[0]:
				time.sleep(0.2)
			continue
		if stopper.check(users):
			break
		t = time.monotonic() + PERIOD
		while time.monotonic() < t and not on_signal[0] and not stopper.stop_file():
			time.sleep(0.2)
		if stopper.why:
			break
	# A python.exe held back at the last look (Watch.holds) ran during the run: it is judged before the
	# verdict, within MARK_WAIT s, so that the run's end exempts nothing. What starts after the end doesn't count
	held = set(w.pending)
	while held and not on_signal[0] and not (stopper and stopper.why):
		time.sleep(min(PERIOD, 0.5))
		users, _ = w.sample()
		for p in users:
			if (p.pid, p.start) in held:
				for k in w.foreign(p):
					counts[k] += 1
				note(("dev", p.pid, p.start), "FOREIGN %s" % describe(p))
		held &= set(w.pending)
	counts["desktops"] = desktop_text(w.desktops, short=True)
	if w.gpu is not None:
		counts["shared"] = (shared["looks"], looks, w.gpu, [shared["who"][k] for k in sorted(shared["who"])], shared["peak"])
	counts["clocks"] = clocks.summary()
	counts["load"], counts["background"], counts["loadnotes"] = track.verdict()
	return counts

def finish(counts, log, aborted=None, stopped=None):
	"""The run's last line: RIG_CLEAN, RIG_CLEAN_BACKGROUND (clean but for a machine's listed background
	load, within what background.json allows), RIG_SHARED (a run holding one GPU, clean but for another GPU's
	run's job beside it: not RIG_CLEAN, as its timings aren't a benchmark's) or RIG_CONTAMINATED; or, for a run stopped before its job
	ended, RIG_ABORTED with what was seen until then, never RIG_CLEAN; or RIG_STOPPED for one stopped at an
	experiment machine (stopped says how). Load notes (LoadTrack) give how long and how much."""
	desktops = counts.pop("desktops", "none")
	lines, gpu = counts.pop("clocks", ([], ""))
	too_much = counts.pop("background", False)
	load_notes = counts.pop("loadnotes", [])
	shared = counts.pop("shared", None)  # a run holding one GPU: (looks shared, looks, its GPU, who, their peak load)
	for line in lines:
		log(line)
	n = sum(counts.values())
	background = any(t.startswith("background") for t in load_notes)
	notes = load_notes + ([] if desktops == "none" else ["desktop on the GPU: " + desktops]) + ([gpu] if gpu else [])
	if shared and shared[0]:
		notes.insert(0, "shared the machine with %s in %d of %d looks (their load: up to %.1f cores, %.0f MB/s); "
			"this run held GPU %d only, so its results stand but its timings are not a benchmark's" % (
			" and ".join(shared[3]), shared[0], shared[1], shared[4][0], shared[4][1], shared[2]))
	elif shared:
		notes.insert(0, "held GPU %d only; no job on another GPU ran beside it" % shared[2])
	if n:
		notes.insert(0, "%d sightings: %s" % (n, ", ".join("%s %d" % kv for kv in counts.items())))
	clean = "clean but for background" if background else "clean"
	if stopped:
		log("RIG_STOPPED at the machine by %s (until then: %s)" % (stopped, "; ".join(notes if n or too_much else [clean] + notes)))
	elif aborted:
		log("RIG_ABORTED by %s (until then: %s)" % (aborted, "; ".join(notes if n or too_much else [clean] + notes)))
	elif n or too_much:
		log("RIG_CONTAMINATED (%s)" % "; ".join(notes))
	elif shared and shared[0]:
		log("RIG_SHARED (%s)" % "; ".join(notes))
	elif background:
		log("RIG_CLEAN_BACKGROUND (%s)" % "; ".join(notes))
	else:
		log("RIG_CLEAN" + (" (%s)" % "; ".join(notes) if notes else ""))

def logger(path):
	def log(text):
		line = "%s %s" % (time.strftime("%H:%M:%S"), text)
		with open(path, "a", encoding="utf-8") as f:
			f.write(line + "\n")
		say(line)
	return log

def log_path(args, tag):
	return os.path.join(args.log_dir or os.getcwd(), re.sub(r"[^\w.+-]", "_", tag) + ".rig.log")

def catch_signals(got=None):
	"""got[0] becomes the name of the first stopping signal (Ctrl-C, Ctrl-Break, SIGTERM, SIGHUP). One
	ignored when rig.py started stays ignored, by rig.py and by its job: under nohup a hangup (the
	terminal or the ssh closing) stops neither, and a script's background job (cmd &) keeps out of the
	script's Ctrl-C, as any program would."""
	got = [None] if got is None else got
	def on(sig, frame):
		got[0] = got[0] or signal.Signals(sig).name
	for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
		sig = getattr(signal, name, None)
		if sig is not None and signal.getsignal(sig) is not signal.SIG_IGN:
			signal.signal(sig, on)
	return got

# Windows: rig.py's console window closing (its X, or Task Manager's End task on it), a logoff or a shutdown.
# Windows ends every process in that console about 5 s after the event, and Python has no signal for it
# (only Ctrl-C and Ctrl-Break), so rig.py used to end there and then: no last log line, and gpu.lock left
# behind. That file counts as free once both its pids have ended, but it stays until the next rig.py clears
# it, and anything that only looks for the file takes the rig for busy (2026-11-09, a panel's window closed).
CLOSE_EVENTS = {2: "the console window closing", 5: "a logoff", 6: "a shutdown"}  # CTRL_CLOSE, _LOGOFF, _SHUTDOWN_EVENT
CLOSE_WAIT = 4.0  # s rig.py holds Windows off for, inside its 5 s, to end the job and release the rig
CONSOLE_HANDLERS = []  # the ctypes callbacks, kept alive while rig.py runs

def console_event(event, got, closing, done, abandon, wait=CLOSE_WAIT):
	"""A console control event. Ctrl-C and Ctrl-Break: False (Python's own handlers take them, as
	catch_signals). A close, logoff or shutdown: got[0] says which, closing is set (the run ends its job
	at once, not after 30 s, and releases the rig), and this waits up to wait s for done; if the run hasn't
	finished by then (it was still waiting for the rig to be quiet, say), abandon() ends the job and
	releases the rig from here. True: handled, and Windows ends rig.py when this returns."""
	why = CLOSE_EVENTS.get(event)
	if why is None:
		return False
	got[0] = got[0] or why
	closing.set()
	if not done.wait(wait):
		with contextlib.suppress(Exception):
			abandon()
	return True

def catch_console_close(got, abandon):
	"""(closing, done), two threading.Events: on Windows, a close, logoff or shutdown of rig.py's console
	goes to console_event. The caller sets done once the rig is released."""
	import threading
	closing, done = threading.Event(), threading.Event()
	if WINDOWS:
		import ctypes
		from ctypes import wintypes
		routine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
		k32 = ctypes.WinDLL("kernel32", use_last_error=True)
		k32.SetConsoleCtrlHandler.argtypes = (routine, wintypes.BOOL)
		k32.SetConsoleCtrlHandler.restype = wintypes.BOOL
		handler = routine(lambda event: bool(console_event(event, got, closing, done, abandon)))
		if k32.SetConsoleCtrlHandler(handler, True):
			CONSOLE_HANDLERS.append(handler)
	return closing, done

def abandon_run(lock):
	"""The console is closing and the run didn't finish in time: end the job (its job object) and release
	the rig, from the console handler's thread."""
	for job in list(JOBS.values()):
		with contextlib.suppress(Exception):
			job.terminate()
	lock.release()

def outlive(error, running, log, got, what, stop=None):
	"""rig.py failed while the job runs (an error in its watch, a log it can't write). Releasing the rig
	now would let the next run start beside the job, so the job goes on, unwatched, and the rig stays
	held until it ends. A stopping signal still stops it (stop) or ends a hold, as usual."""
	def note(text):
		try:
			log(text)
		except Exception:
			say(text)
	error = "%s: %s" % (type(error).__name__, error)
	note("rig.py failed (%s): %s runs on unwatched, and the rig stays held until it ends" % (error, what))
	sig = None
	while running():
		if got[0]:
			sig = got[0]
			if stop:
				stop(sig)
			break
		time.sleep(PERIOD)
	return note, "RIG_ABORTED by an error in rig.py (%s)%s" % (error, ", then by %s" % sig if sig else
		": %s ran on, unwatched, to its end" % what)

def start_job(cmd, env, log):
	"""Start cmd: on Linux in a session of its own, on Windows in a job object of its own (suspended
	until it is in it, so nothing it starts escapes)."""
	if not WINDOWS:
		return subprocess.Popen(cmd, env=env, start_new_session=True)
	proc = subprocess.Popen(cmd, env=env, creationflags=0x4)  # CREATE_SUSPENDED
	try:
		job = rig_windows.Job()
		if job.add(proc.pid):
			JOBS[env["RIG_JOB"]] = job
		else:
			job.close()
			raise OSError("AssignProcessToJobObject failed")
	except OSError as e:
		log("the job's processes are found by their parent pids (no Windows job object: %s)" % e)
	finally:
		rig_windows.resume(proc.pid)
	return proc

def stop_job(proc, sig):
	"""The run was stopped: stop the job too, or it would go on without the rig."""
	if WINDOWS:  # Ctrl-C reached the job too; give it 30 s to end, then end it
		try:
			proc.wait(30)
		except subprocess.TimeoutExpired:
			for job in JOBS.values():
				job.terminate()
			proc.kill()
		return
	try:
		os.killpg(proc.pid, getattr(signal, sig))
	except OSError:
		pass
	try:
		proc.wait(30)
	except subprocess.TimeoutExpired:
		os.killpg(proc.pid, signal.SIGKILL)
		proc.wait()

def end_job(proc):
	"""Stopped at an experiment machine: end every process of the job now, not after a grace period."""
	if WINDOWS:
		for job in JOBS.values():
			job.terminate()
		with contextlib.suppress(OSError):
			proc.kill()
		return
	with contextlib.suppress(OSError):
		os.killpg(proc.pid, signal.SIGTERM)
	try:
		proc.wait(3)
	except subprocess.TimeoutExpired:
		with contextlib.suppress(OSError):
			os.killpg(proc.pid, signal.SIGKILL)

# Versions and installing (rig.py version, install; the first line of status and of every rig.log)

def file_bytes(path):
	"""A file's bytes with line endings made \n (a Windows checkout's are \r\n), or None if it is missing."""
	try:
		with open(path, "rb") as f:
			return f.read().replace(b"\r\n", b"\n")
	except FileNotFoundError:
		return None

def version(folder):
	"""The lock's version in folder: the first 10 hex digits of a sha256 of FILES there, line endings ignored."""
	h = hashlib.sha256()
	for name in FILES:
		data = file_bytes(os.path.join(folder, name))
		h.update(name.encode() + (b"\0-\0" if data is None else b"\0+%d\0" % len(data) + data))
	return h.hexdigest()[:10]

def same_folder(a, b):
	return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))

def read_stamp(root=None):
	"""What RIG_INSTALLED says ({key: value}), {} if there is none."""
	try:
		with open(os.path.join(root or ROOT, STAMP), encoding="utf-8", errors="replace") as f:
			return dict(line.rstrip("\n").split(": ", 1) for line in f if ": " in line)
	except OSError:
		return {}

def installed_text():
	"""'<version> in <ROOT>, installed <when> from campy <commit> by <who>', or what is known instead."""
	v, stamp = version(ROOT), read_stamp()
	text = "%s in %s" % (v, ROOT)
	if not stamp:
		return text + " (not installed with rig.py install)"
	how = "%s from campy %s by %s" % (stamp.get("installed", "?"), stamp.get("commit", "?"), stamp.get("by", "?"))
	if stamp.get("version") != v:
		return text + " (changed since rig.py install put %s there on %s)" % (stamp.get("version", "?"), how)
	return text + ", installed " + how

def version_text():
	"""This copy's version; and, run from somewhere else than ROOT, the installed one's too."""
	if same_folder(HERE, ROOT):
		return installed_text()
	mine, theirs = version(HERE), version(ROOT)
	return "%s in %s, %s the installed one: %s" % (mine, HERE, "the same as" if mine == theirs else "NOT", installed_text())

def git(folder, *args):
	"""git's output in folder (stripped), or None if git isn't there or fails."""
	try:
		r = subprocess.run(["git", "-C", folder] + list(args), capture_output=True, text=True, timeout=30,
			creationflags=NO_WINDOW)
	except (OSError, subprocess.SubprocessError):
		return None
	return r.stdout.strip() if r.returncode == 0 else None

def source_folder(path):
	"""The folder holding rig.py: path itself, or path/bench (a campy checkout)."""
	for folder in (os.path.join(path, "bench"), path):
		if os.path.isfile(os.path.join(folder, "rig.py")):
			return os.path.abspath(folder)
	sys.exit("rig.py install: no rig.py in %s or its bench folder" % path)

RIG_PROC = re.compile(r"""rig\.py["']?\s+(run|hold|install)\b|rig_hold\.ps1""", re.I)

def mark_texts():
	"""{(pid, start): its first line} of every marker (rig_marks): what each marked rig.py was started to do,
	e.g. 'rig.py run (pid 4242) since 2026-11-20 19:31:59'."""
	out = {}
	for pid, start in rig_marks():
		try:
			with open(os.path.join(marks_folder(), "%d-%d" % (pid, start)), encoding="utf-8-sig", errors="replace") as f:
				out[(pid, start)] = f.readline().strip()
		except OSError:
			pass
	return out

def rig_processes(procs=None):
	"""[(pid, command line)] of every other rig.py run, hold or install, and rig_hold.ps1, running or
	waiting; this rig.py and the processes it runs under (an update script's rig.py run) left out. On Windows
	a rig.py whose command line can't be read (started over ssh, elevated, or by another user) is known by its
	marker instead (mark_self)."""
	procs = scan() if procs is None else procs
	marks = mark_texts() if WINDOWS else {}
	mine, pid = set(), os.getpid()
	while pid in procs and pid not in mine:
		mine.add(pid)
		pid = procs[pid].ppid
	found = []
	for p in procs.values():
		if p.pid in mine or p.pid == os.getpid():
			continue
		if WINDOWS and not re.match(r"(python|pythonw|py|powershell|pwsh)\d*(\.\d+)*(\.exe)?$", p.cmd, re.I):
			continue
		# macOS: a Python (rig.py runs under one), not a shell or an ssh whose command text names rig.py
		if MACOS and not re.match(r"python\d*(\.\d+)*$", rig_macos.program_name(getattr(p, "exe", None) or ""), re.I):
			continue
		text = cmdline(p)
		if text and RIG_PROC.search(text):
			found.append((p.pid, text))
			continue
		mark = marks.get((p.pid, p.start))  # the same pid and start time: not a marker of an earlier process
		if mark and RIG_PROC.search(mark):
			found.append((p.pid, "%s (command line unreadable; its marker: %s)" % (p.cmd, mark)))
	return found

def compare(src):
	"""[(file, 'same' | 'differs' | 'new' | 'not in the source')] between src and ROOT."""
	rows = []
	for name in FILES:
		a, b = file_bytes(os.path.join(src, name)), file_bytes(os.path.join(ROOT, name))
		rows.append((name, "not in the source" if a is None else "new" if b is None else "same" if a == b else "differs"))
	return rows

def cmd_version(args):
	print("rig.py " + version_text())

def cmd_install(args):
	src = source_folder(args.source) if args.source else HERE
	if same_folder(src, ROOT):
		sys.exit("rig.py install: %s is the installed copy itself; run a campy checkout's bench/rig.py install, "
			"or give --from <checkout>" % src)
	new, old = version(src), version(ROOT)
	commit = git(src, "rev-parse", "--short=9", "HEAD")
	if not commit and args.commit:
		commit = "%s (said)" % args.commit
	print("installed: rig.py " + installed_text())
	print("source:    rig.py %s in %s (campy %s)" % (new, src, commit or "?, not a git checkout"))
	rows = compare(src)
	for name, how in rows:
		print("    %-16s %s" % (name, how))
	missing = [name for name, how in rows if how == "not in the source"]
	if missing:
		sys.exit("rig.py install: %s has no %s; give a campy checkout" % (src, ", ".join(missing)))
	if new == old:
		print("the installed copy is this one: nothing to install")
		return
	if args.check:
		print("the installed copy differs: run rig.py install from this checkout (when the rig is free)")
		sys.exit(3)
	if commit and not args.commit:
		dirty = git(src, "status", "--porcelain", "--", *FILES)
		if dirty and not args.allow_dirty:
			sys.exit("rig.py install: the checkout has uncommitted changes to the lock's files (--allow-dirty to "
				"install them anyway):\n" + dirty)
		was = read_stamp().get("commit", "?").split("+")[0]
		if re.match(r"^[0-9a-f]{7,40}$", was) and not commit.startswith(was) and not was.startswith(commit) \
				and git(src, "merge-base", "--is-ancestor", commit, was) is not None and not args.allow_older:
			sys.exit("rig.py install: campy %s is older than the installed %s (--allow-older to install it anyway)" % (commit, was))
		if dirty:
			commit += "+changes"
	owner = args.owner or os.environ.get("RIG_OWNER") or "unnamed"
	lock = make_lock()
	inside = in_current_job(lock)
	if not inside:
		lock.acquire(owner, "rig-install", True)  # busy: exit 75
	try:
		if not inside:
			lock.record(note=" (installing rig.py %s)" % new)
		others = rig_processes()
		if others:
			say("rig.py install: not now, other rig.py processes run or wait (a run that has started keeps its "
				"own copy, but one waiting would start on a mix):")
			for pid, text in others:
				say("    %d %s" % (pid, text[:150]))
			sys.exit(75)
		backup = None
		if any(os.path.exists(os.path.join(ROOT, name)) for name in FILES):
			backup = os.path.join(ROOT, "rig-backup", "%s-%s" % (old, time.strftime("%Y%m%d-%H%M%S")))
			os.makedirs(backup)
			for name in FILES:
				if os.path.exists(os.path.join(ROOT, name)):
					shutil.copy2(os.path.join(ROOT, name), os.path.join(backup, name))
			stamp = os.path.join(ROOT, STAMP)
			if os.path.exists(stamp):
				shutil.copy2(stamp, os.path.join(backup, STAMP))
		for name in FILES:
			with open(os.path.join(src, name), "rb") as f:
				data = f.read()
			dest = os.path.join(ROOT, name)
			tmp = dest + ".new"
			with open(tmp, "wb") as f:
				f.write(data)
			with open(tmp, "rb") as f:
				if hashlib.sha256(f.read()).digest() != hashlib.sha256(data).digest():
					sys.exit("rig.py install: %s didn't read back as written; nothing more replaced" % tmp)
			retry(os.replace, tmp, dest)
		if version(ROOT) != new:
			sys.exit("rig.py install: the installed copy is %s, not %s; the old files are in %s" % (version(ROOT), new, backup))
		when = time.strftime("%Y-%m-%d %H:%M:%S")
		with open(os.path.join(ROOT, STAMP), "w", encoding="utf-8") as f:
			f.write("version: %s\ncommit: %s\nfrom: %s\ninstalled: %s\nby: %s\nprevious: %s\nbackup: %s\n" % (
				new, commit or "?", src, when, owner, old, backup or "none"))
		with open(os.path.join(ROOT, "rig-install.log"), "a", encoding="utf-8") as f:
			f.write("%s %s installed rig.py %s (campy %s, from %s) over %s; backup %s\n" % (
				when, owner, new, commit or "?", src, old, backup or "none"))
		say("installed rig.py %s over %s%s" % (new, old, "; the old files are in " + backup if backup else ""))
	finally:
		if not inside:
			lock.release()

# Experiment machines (docs/design/rig-fleet.md, section 5). A machine people record experiments on has
# ROOT/experiment.json. There run starts a test only when nobody uses the machine (every Windows session
# idle 15+ minutes), nothing uses a camera, the GPU, the encoder or the trigger, the hour is allowed and
# tests aren't paused (ROOT/PAUSE); it never waits in line. On Windows the banner (rig_banner.ps1) must
# be running in the session and show the test before it starts. The test stops within 2 s when ROOT/STOP
# appears (the banner's Stop button, its desktop shortcut, input at the machine, unlocking, a Remote
# Desktop connect, a logon), when anything outside the job starts using a device (campy, SpinView, a
# camera opened), or when quser shows a session in use; PAUSE then keeps new tests away.

EXPERIMENT_DEFAULTS = {
	"hours": "22:00-06:00",       # when tests may start: "HH:MM-HH:MM[,...]", "any" for always
	"idle_minutes": 15,           # every session idle at least this long
	"pause_until": "09:00",       # a stop pauses tests until this time (the next morning)
	"banner": WINDOWS,            # need the banner showing the test (Windows)
	"banner_task": "campy test banner",  # the Task Scheduler task that starts it in the session
	"session_every": 10,          # s between looks at the sessions (quser) during a test
}

def experiment_config():
	"""The experiment machine's settings, or None on any other machine. A file that can't be read
	still makes this an experiment machine (with the defaults): a typo mustn't turn the safeguards off."""
	path = os.path.join(ROOT, "experiment.json")
	try:
		with open(path, encoding="utf-8") as f:
			cfg = json.load(f)
		if not isinstance(cfg, dict):
			raise ValueError("not a JSON object")
	except FileNotFoundError:
		return None
	except (OSError, ValueError) as e:
		say("%s can't be read (%s): using the experiment-machine defaults" % (path, e))
		cfg = {}
	return dict(EXPERIMENT_DEFAULTS, **cfg)

def minutes(hhmm):
	h, m = hhmm.strip().split(":")
	return int(h) * 60 + int(m)

def in_hours(spec, now):
	"""Whether now (a datetime) is inside spec: 'HH:MM-HH:MM', several comma-separated; a range may
	wrap past midnight ('22:00-06:00'). 'any' (or empty) is always."""
	if not spec or str(spec).strip().lower() in ("any", "always"):
		return True
	t = now.hour * 60 + now.minute
	for part in str(spec).split(","):
		a, b = (minutes(x) for x in part.split("-"))
		if (a <= t < b) if a <= b else (t >= a or t < b):
			return True
	return False

def fields(text):
	"""'key=value; key=value' as a dict (what STOP, PAUSE and BANNER hold)."""
	return {k.strip(): v.strip() for k, v in (p.split("=", 1) for p in (text or "").split(";") if "=" in p)}

def marker(name):
	"""ROOT/<name>'s text; None if there is none. Read so that the writer can still replace it."""
	try:
		with open(os.path.join(ROOT, name), encoding="utf-8", errors="replace") as f:
			return f.read().strip()
	except FileNotFoundError:
		return None
	except OSError:
		return ""  # being replaced this moment

def write_marker(name, **kv):
	"""Write ROOT/<name> as one 'key=value; ...' line, whole or not at all."""
	path = os.path.join(ROOT, name)
	tmp = "%s.%d.tmp" % (path, os.getpid())
	with open(tmp, "w", encoding="utf-8") as f:
		f.write("; ".join("%s=%s" % (k, str(v).replace(";", ",")) for k, v in kv.items()) + "\n")
	retry(os.replace, tmp, path)

def remove_marker(name):
	with contextlib.suppress(FileNotFoundError):
		retry(os.remove, os.path.join(ROOT, name))

def pause_until(cfg, now):
	"""The next pause_until time after now, as 'YYYY-MM-DD HH:MM' (a stop at 11 PM pauses until 9 AM)."""
	t = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(minutes=minutes(cfg["pause_until"]))
	if t <= now:
		t += datetime.timedelta(days=1)
	return t.strftime("%Y-%m-%d %H:%M")

def read_pause(now):
	"""PAUSE's text while tests are paused, else None. A pause whose 'until' has passed is removed;
	'until=resume', or an 'until' that can't be read, pauses until someone removes the file."""
	text = marker("PAUSE")
	if text is None:
		return None
	until = fields(text).get("until", "resume")
	try:
		ends = datetime.datetime.strptime(until, "%Y-%m-%d %H:%M")
	except ValueError:
		return text or "PAUSE"
	if now < ends:
		return text
	say("the pause has ended (%s): removed" % text)
	remove_marker("PAUSE")
	return None

def write_pause(cfg, how, now=None):
	"""Pause tests until the next morning, unless a pause is in place already (it may be longer)."""
	now = now or datetime.datetime.now()
	if read_pause(now) is None:
		write_marker("PAUSE", until=pause_until(cfg, now), how=how, at=now.strftime("%Y-%m-%d %H:%M:%S"))

QUSER = re.compile(r"^[ >](\S+)\s+(?:(\S+)\s+)?(\d+)\s+(\S+)\s+(\S+)\s+(\S.*?)\s*$")

def idle_minutes(text):
	"""quser's IDLE TIME in minutes: '.' or 'none' (under a minute) 0, '7', '1:05', '2+03:04'; None unknown."""
	text = text.strip()
	if text in (".", "none"):
		return 0
	m = re.match(r"^(?:(\d+)\+)?(?:(\d+):)?(\d+)$", text)
	if not m:
		return None
	d, h, mi = m.groups()
	return int(d or 0) * 1440 + int(h or 0) * 60 + int(mi)

def parse_quser(text):
	"""[{user, session, id, state, idle}] from quser's table (a disconnected session has no SESSIONNAME)."""
	out = []
	for line in (text or "").splitlines()[1:]:
		m = QUSER.match(line)
		if m:
			out.append({"user": m.group(1), "session": m.group(2) or "", "id": int(m.group(3)),
				"state": m.group(4), "idle": idle_minutes(m.group(5))})
	return out

def sessions():
	"""Every Windows session (quser), [] if none is logged on, None if that can't be told. RIG_QUSER names a
	file holding quser's output instead (tests). Elsewhere there are none to see."""
	fake = os.environ.get("RIG_QUSER")
	if fake:
		try:
			with open(fake, encoding="utf-8") as f:
				return parse_quser(f.read())
		except OSError:
			return None
	if not WINDOWS:
		return []
	try:
		r = subprocess.run(["quser"], capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
	except (OSError, subprocess.TimeoutExpired):
		return None
	if r.returncode != 0 and "No User exists" in (r.stdout + r.stderr):
		return []
	return parse_quser(r.stdout) if r.returncode == 0 else None

def session_text(s):
	return "session %d (%s%s, %s, idle %s min)" % (s["id"], s["user"], " " + s["session"] if s["session"] else "",
		s["state"], "?" if s["idle"] is None else s["idle"])

def banner_pid():
	"""The banner's process id if it runs (ROOT/BANNER names it), else None."""
	pid = fields(marker("BANNER")).get("pid", "")
	return int(pid) if pid.isdigit() and proc_start(int(pid)) is not None else None

def start_banner(cfg, wait=20.0):
	"""Start the banner in the logged-on session by its Task Scheduler task; whether it runs."""
	if banner_pid():
		return True
	with contextlib.suppress(OSError, subprocess.TimeoutExpired):
		subprocess.run(["schtasks", "/run", "/tn", cfg["banner_task"]], capture_output=True, timeout=20,
			creationflags=NO_WINDOW)
	t0 = time.monotonic()
	while time.monotonic() - t0 < wait:
		if banner_pid():
			return True
		time.sleep(0.5)
	return False

def banner_shows(job, wait=15.0):
	"""Wait until the banner says it shows job; whether it did."""
	t0 = time.monotonic()
	while time.monotonic() - t0 < wait:
		if fields(marker("BANNER")).get("shown") == job and banner_pid():
			return True
		time.sleep(0.25)
	return False

def experiment_blockers(cfg, banner=True, now=None, allow=()):
	"""(why a test may not start now, [] if it may; the sessions seen). allow: kinds --allow leaves out."""
	now = now or datetime.datetime.now()
	why = []
	if "ephys" not in allow:
		programs = ephys_programs()
		if programs:
			why.append("an ephys acquisition program is running (it may be recording): %s" % ", ".join(
				"%s (pid %d)" % (name, pid) for pid, name in programs))
	if not in_hours(cfg["hours"], now):
		why.append("outside the hours tests may start (%s)" % cfg["hours"])
	paused = read_pause(now)
	if paused is not None:
		why.append("tests are paused (%s)" % paused)
	found = sessions()
	if found is None:
		why.append("can't tell whether anyone is using the machine (quser failed)")
	else:
		for s in found:
			if s["idle"] is None or s["idle"] < cfg["idle_minutes"]:
				why.append("someone may be using it: %s, under %d min" % (session_text(s), cfg["idle_minutes"]))
	if banner and cfg["banner"] and found and not why and not start_banner(cfg):
		why.append("the banner isn't running in the session (Task Scheduler task %r)" % cfg["banner_task"])
	return why, found or []

class Stopper:
	"""During a test on an experiment machine: why to stop it now (self.why), or None."""

	def __init__(self, cfg, found, allow=()):
		self.cfg, self.why, self.allow = cfg, None, set(allow)
		self.base = {s["id"]: s for s in found}
		self.next = time.monotonic() + cfg["session_every"]

	def stop_file(self):
		text = marker("STOP")
		if text is None:
			return False
		self.why = fields(text).get("how") or "the STOP file"
		return True

	def check(self, users):
		"""Called every PERIOD with the foreign device users."""
		if self.why or self.stop_file():
			return True
		if users:
			p = users[0]
			name = re.split(r"[\\/]", (p.cmd or "?").split()[0].strip('"'))[-1]
			kinds = p.kinds - {"desktop"} - self.allow
			uses = sorted(kinds - {"ephys"})
			what = (["using the %s" % ",".join(uses)] if uses else []) + (
				["an ephys acquisition program"] if "ephys" in kinds else [])
			self.why = "%s started outside the test, %s (pid %d)" % (name, ", ".join(what), p.pid)
			return True
		if time.monotonic() >= self.next:
			self.next = time.monotonic() + self.cfg["session_every"]
			self.why = session_change(self.base, sessions())
		return self.why is not None

def session_change(base, now):
	"""Why the sessions show someone at the machine since the test started, or None."""
	for s in now or ():
		b = base.get(s["id"])
		if b is None:
			return "a new session: %s" % session_text(s)
		if s["state"] != b["state"]:
			return "%s was %s" % (session_text(s), b["state"])
		if s["idle"] is not None and b["idle"] is not None and s["idle"] < b["idle"] - 1:
			return "keyboard or mouse input: %s (it was idle %s min)" % (session_text(s), b["idle"])
	return None

def in_current_job(lock):
	"""Whether this rig.py runs inside the job that holds the rig (its RIG_JOB is the lock's job)."""
	job = os.environ.get("RIG_JOB")
	if not job:
		return False
	state, text = lock.state()
	return state in ("held", "shared") and ("job=%s " % job) in (text + " ")

def parse_gpu(text):
	"""--gpu: nvidia-smi's index of a GPU; '' (RIG_GPU unset or empty) is none."""
	text = (text or "").strip()
	if not text:
		return None
	if not text.isdigit():
		raise argparse.ArgumentTypeError("not a GPU's index (nvidia-smi's: 0, 1, ...): %s" % text)
	return int(text)

def gpu_plan(args, experiment, job=True):
	"""For --gpu: (the GPU this run holds alone, or None for the whole machine; what the job's environment gets;
	lines for the log). Without --gpu: (None, {}, []). job: a run's job, which is shown only that GPU (not hold's)."""
	want = getattr(args, "gpu", None)
	if want is None:
		return None, {}, []
	gpus = gpu_list()
	g = next((x for x in gpus if x.index == want), None)
	if gpus and g is None:
		sys.exit("rig.py: there is no GPU %d here; nvidia-smi lists %s" % (want, "; ".join("GPU %d %s" % (x.index, x.name) for x in gpus)))
	env = {"RIG_GPU": str(want)}
	if g and g.uuid.startswith("GPU-"):
		env["CUDA_VISIBLE_DEVICES"] = g.uuid
	else:  # no UUID to go by: the index, in nvidia-smi's order
		env.update(CUDA_VISIBLE_DEVICES=str(want), CUDA_DEVICE_ORDER="PCI_BUS_ID")
	name = "GPU %d%s" % (want, " (%s)" % g.name if g else "")
	pin = "; the job sees only %s (CUDA_VISIBLE_DEVICES=%s)" % (name, env["CUDA_VISIBLE_DEVICES"]) if job else ""
	why = ("--whole" if getattr(args, "whole", False) else "--strict" if getattr(args, "strict", False) else
		"the lock here is a flock; one GPU at a time needs the file lock" if not file_lock() else
		"an experiment machine" if experiment else
		"nvidia-smi lists %s" % ("no GPU" if not gpus else "only one GPU") if len(gpus) < 2 else None)
	if why:
		return None, env if job else {}, ["--gpu %d%s; this run holds the whole machine all the same (%s)" % (want, pin, why)]
	return want, env if job else {}, ["one GPU at a time: this run holds %s alone%s. A run holding another GPU may run "
		"beside it: that run's job, and its CPU and disk load, don't count against this one, which then ends "
		"RIG_SHARED (its results stand, but not its timings: the two share the CPU, memory, disks and PCIe); "
		"anything on %s outside this job does count" % (name, pin, name)]

def cmd_run(args):
	if not args.cmd:
		sys.exit("rig.py run: no command (rig.py run <owner> <tag> -- cmd ...)")
	lock = make_lock()
	if in_current_job(lock):
		# The job holding the rig asked for it again: waiting would wait on itself forever
		say("rig.py run: already inside the rig's job %s; running %s in it" % (os.environ["RIG_JOB"], args.tag))
		code = subprocess.call(args.cmd)
		sys.exit(code if code >= 0 else 128 - code)
	cfg = experiment_config()
	gpu, gpu_env, gpu_notes = gpu_plan(args, cfg is not None)
	lock = make_lock(gpu)
	if cfg:  # an experiment machine: a test starts only now or not at all, never waits in line
		args.no_wait = True
		why, _ = experiment_blockers(cfg, allow=allowed_kinds(args))
		if why:
			say("rig.py run: not now on this experiment machine: %s" % "; ".join(why))
			sys.exit(75)
	lock.acquire(args.owner, args.tag, args.no_wait)
	got = [None]
	closing, done = catch_console_close(got, lambda: abandon_run(lock))
	proc, found = None, []
	try:
		lock.record(note=WAITING)
		bg = background_for(args)
		wait_quiet(Watch(background=bg, allow=allowed_kinds(args), gpu=gpu,
			partners=holders_but(gpu) if gpu is not None else None), args)
		if cfg:  # again, now that the rig is ours; a STOP left from before is not for this run
			why, found = experiment_blockers(cfg, allow=allowed_kinds(args))
			if why:
				say("rig.py run: not now on this experiment machine: %s" % "; ".join(why))
				sys.exit(75)
			remove_marker("STOP")
		job = "%s-%d" % (re.sub(r"\W", "_", args.tag), os.getpid())
		log = logger(log_path(args, args.tag))
		# The log's first line before the job starts: a run whose job ran has a log, even when rig.py
		# is killed (SIGKILL) the moment RIG_BUSY names the job, and the log says nothing more then
		log("rig.py " + version_text())
		log_rules(log, args, bg)
		for line in gpu_notes:
			log(line)
		catch_signals(got)
		env = dict(os.environ, RIG_JOB=job, RIG_OWNER=args.owner, RIG_TAG=args.tag, **gpu_env)
		# The lock names the job before the job starts: a guarded script (rig_guard.sh, .ps1) checks
		# at once whether it runs in the rig's job, and if it looked before the lock named its job, it
		# would re-run itself under rig.py and wait forever on the rig its own rig.py holds
		lock.record(job, None)
		if cfg and cfg["banner"] and found:
			if not banner_shows(job):
				log("the banner didn't show the test within 15 s, so it was not started")
				log("RIG_STOPPED at the machine by no banner showing (the job never started)")
				sys.exit(75)
			log("the banner shows the test (pid %s)" % banner_pid())
		if cfg:
			log("experiment machine: %s" % ("; ".join(session_text(s) for s in found) or "nobody logged on"))
		proc = start_job(args.cmd, env, log)
		lock.record(job, proc.pid)
		log("rig held by %s for %s (job %s, pid %d): %s" % (args.owner, args.tag, job, proc.pid, " ".join(shlex.join(args.cmd).split())))
		w = Watch(job=job, sid=None if WINDOWS else proc.pid, root_pid=proc.pid, background=bg, allow=allowed_kinds(args),
			gpu=gpu, partners=holders_but(gpu) if gpu is not None else None)
		stopper = Stopper(cfg, found, allowed_kinds(args)) if cfg else None
		counts = watch(w, lambda: proc.poll() is None, log, args, got, lock, stopper)
		stopped = stopper and stopper.why
		if stopped:
			log("STOP: %s; ending the job" % stopped)
			end_job(proc)
			write_pause(cfg, stopped)
			remove_marker("STOP")
			log("tests paused: %s" % read_pause(datetime.datetime.now()))
		elif got[0]:
			if closing.is_set():  # the console is closing: Windows ends rig.py in seconds, so no 30 s for the job
				end_job(proc)
			else:
				stop_job(proc, got[0])
		code = proc.wait()
		# Whatever the job left running on a device keeps the rig until it ends (at most 60 s; not
		# while the console closes, when the job object has ended all of it)
		def leftovers():
			return [p for p in scan().values() if p.job == job and p.kinds]
		t0 = time.monotonic()
		while not closing.is_set() and leftovers() and time.monotonic() - t0 < 60:
			time.sleep(PERIOD)
		for p in leftovers():
			log("LEFTOVER still running after the job: %s" % describe(p))
		log("job exit %s" % code)
		finish(counts, log, got[0], stopped)
		if stopped:
			code = 75
	except BaseException as e:
		if proc is None or proc.poll() is not None:
			raise
		note, verdict = outlive(e, lambda: proc.poll() is None, log, got, "the job", lambda sig: stop_job(proc, sig))
		note("job exit %s" % proc.wait())
		note(verdict)
		raise
	finally:
		lock.release()
		for j in JOBS.values():
			j.close()
		JOBS.clear()
		done.set()
	sys.exit(code if code >= 0 else 128 - code)

def cmd_hold(args):
	start = proc_start(args.pid)
	if start is None:
		sys.exit("rig.py hold: pid %d is not running" % args.pid)
	gpu, _, gpu_notes = gpu_plan(args, experiment_config() is not None, job=False)
	lock = make_lock(gpu)
	lock.acquire(args.owner, args.tag, args.no_wait)
	got = [None]
	closing, done = catch_console_close(got, lock.release)  # the held pid is left running, as on a signal
	held = False
	def running():
		return alive(args.pid, start)
	try:
		lock.record(note=WAITING)
		bg = background_for(args)
		partners = holders_but(gpu) if gpu is not None else None
		wait_quiet(Watch(root_pid=args.pid, background=bg, allow=allowed_kinds(args), gpu=gpu, partners=partners), args)
		if not running():
			sys.exit("rig.py hold: pid %d ended while waiting for the rig" % args.pid)
		p = scan().get(args.pid)
		job = (p and p.job) or "hold-%d" % args.pid
		log = logger(log_path(args, args.tag))
		log("rig.py " + version_text())  # before RIG_BUSY names the pid, as in run
		log_rules(log, args, bg)
		for line in gpu_notes:
			log(line)
		catch_signals(got)
		lock.record(job, args.pid, " (held for a running pid)")
		held = True
		log("rig held by %s for %s (running pid %d): %s" % (args.owner, args.tag, args.pid, cmdline(p) if p else "?"))
		w = Watch(job=p and p.job, sid=p and p.sid, root_pid=args.pid, background=bg, allow=allowed_kinds(args),
			gpu=gpu, partners=partners)
		counts = watch(w, running, log, args, got, lock)
		finish(counts, log, got[0] and "%s; the held pid was left running" % got[0])
	except BaseException as e:
		if not held or not running():
			raise
		note, verdict = outlive(e, running, log, got, "the held pid")
		note(verdict + ("; the held pid was left running" if got[0] else ""))
		raise
	finally:
		lock.release()
		done.set()

def gpu_status(state):
	"""status' lines on the GPUs one by one (a machine with two or more, or GPU locks), and on who waits in line."""
	gpus, holders = gpu_list(), {h["gpu"]: h for h in gpu_holders()}
	if len(gpus) >= 2 or holders:
		names = {g.index: g.name for g in gpus}
		print("one GPU at a time (rig.py run --gpu <N>, or RIG_GPU=<N>):")
		for i in sorted(set(names) | set(holders)):
			h = holders.get(i)
			print("    GPU %d%s: %s" % (i, " (%s)" % names[i] if i in names else "", "held alone: " + h["line"] if h else
				"held with the whole machine" if state in ("held", "hand") else "free"))
	queue = tickets()
	if queue:
		print("in line, oldest first:")
		for n, (name, f) in enumerate(queue, 1):
			print("    %d. %s (%s pid %s)" % (n, ticket_text(f), f.get("by", "rig.py"), f.get("rigpid", "?")))

def cmd_status(args):
	lock = make_lock()
	notes = []
	if isinstance(lock, FileLock) and os.path.isdir(ROOT):
		with guarded(lock.path):
			notes = lock.sync()  # what has ended, on the GPUs one by one and in line
	state, info = lock.state()
	if state in ("held", "hand"):
		print("rig held: %s" % lock.describe(state, info))
	elif state == "shared":
		print("rig held: %s" % info)
	else:
		print("rig free" + (" (stale %s cleared: %s)" % ("gpu.lock" if isinstance(lock, FileLock) else "RIG_BUSY", info) if info else ""))
		if info:
			if isinstance(lock, FileLock):
				with guarded(lock.path):
					if lock.state()[0] == "stale":
						retry(os.remove, lock.path)
			else:
				lock.clear_stale()
	print("rig.py " + version_text())  # second: scripts look for "rig held" or "rig free" first
	for note in notes:
		print(note)
	if isinstance(lock, FileLock):
		gpu_status(state)
	cfg = experiment_config()
	if cfg:
		now = datetime.datetime.now()
		found = sessions()
		paused, banner = read_pause(now), banner_pid()
		print("experiment machine: tests may start %s (now %s); %s; banner %s" % (cfg["hours"],
			"inside" if in_hours(cfg["hours"], now) else "outside", "paused: %s" % paused if paused else "not paused",
			"running (pid %d)" % banner if banner else "not running"))
		print("    sessions: %s" % ("can't tell (quser failed)" if found is None else
			"; ".join(session_text(s) for s in found) or "nobody logged on"))
	m = re.search(r"\bjob=(\S+)", info) if state == "held" else None
	job = m and m.group(1)
	named = named_job(info) if state == "held" else None
	root = named[0] if named and alive(*named) else None  # the job's first process: still that one, not its pid reused
	bg = background_for(args)
	# The job's processes (on Windows, those it started); with GPUs held one at a time, those of every such run
	holders = [h for h in gpu_holders() if h["state"] == "held"] if isinstance(lock, FileLock) else []
	w = Watch(job=job, root_pid=root, background=bg, partners=(lambda: holders) if holders else None)
	w.sample()
	time.sleep(1.0)
	_, load = w.sample()
	procs = scan(gpus=bool(holders)) if holders else scan()
	sides = Sides(procs, holders) if holders else None
	if WINDOWS:
		ports = sorted(rig_windows.serial_devices().values())
		names = {"gpu": "GPU (nvidia-smi%s, python*.exe)" % ("" if NVSMI else ": none"), "encoder": "video encoder (ffmpeg*.exe)",
			"camera": "FLIR cameras (Spinnaker loaded)", "trigger": "trigger (%s)" % (" ".join(ports) or "no COM port"),
			"ephys": "ephys acquisition (%s)" % ", ".join(rig_windows.EPHYS_NAMES)}
	elif MACOS:
		ports = rig_macos.serial_ports()
		names = {"gpu": "GPU", "encoder": "video encoder", "camera": "FLIR cameras",
			"trigger": "trigger (%s)" % (" ".join(ports) or "no USB serial port plugged in"),
			"ephys": "ephys acquisition (%s)" % ", ".join(EPHYS_NAMES)}
	else:
		cams = flir_cameras()
		jetson = os.path.isdir("/dev/nvgpu")  # a Jetson's integrated GPU (see device_kind)
		names = {"gpu": "GPU compute (%s)" % ("/dev/nvgpu/*, /dev/nvhost-*gpu" if jetson else "/dev/kfd"),
			"encoder": "video encoder (%s)" % ("/dev/v4l2-nvenc, /dev/dri/renderD*" if jetson else "/dev/dri/renderD*"),
			"camera": "FLIR cameras (%d on USB: %s)" % (len(cams), " ".join(sorted(cams.values())) or "none"),
			"trigger": "trigger (%s)" % (" ".join(sorted(
				"/dev/" + d for d in os.listdir("/dev") if re.match(r"tty(ACM|USB)\d+$", d))) or "none plugged in"),
			"ephys": "ephys acquisition (%s)" % ", ".join(EPHYS_NAMES)}
	for k in KINDS:
		users = [p for p in procs.values() if k in p.kinds and p.pid != os.getpid()]
		if k in UNWATCHED and not (k == "gpu" and holders):  # with GPUs held one at a time, nvidia-smi's
			print("%-48s %s" % (names[k] + ":", "not watched on macOS"))
			continue
		print("%-48s %s" % (names[k] + ":", "free" if not users else ""))
		for p in users:
			# the job's: in it (RIG_JOB, its Windows job object), or below the line's pid=. vm-oline's GPU queue writes its
			# own line, with pid= but no job=, and runs each job in a job object of its own
			mine = (job and p.job == job) or (root and Watch.below(p, procs, {root}))
			side = sides.side(p) if sides else None
			print("    %s%s" % ("[job] " if mine else "[GPU %d's job] " % side if side is not None else "", describe(p, 120)))
	print("%-48s %s" % ("desktop on the GPU (not waited for):", desktop_text(
		{(p.pid, p.start): p for p in procs.values() if "desktop" in p.kinds})))
	for row in gpu_clocks():
		print(clock_text(row))
	if load:
		cpu, io, top, disks = load[:4]
		print("load outside the job%s (1 s): cpu %.1f cores, disk %.0f MB/s%s   (run waits above %.1f cores or %.0f MB/s)" % (
			"s on single GPUs" if holders else "", cpu, io, "" if disks is None else " (all disks %.0f)" % disks, args.cpu, args.io))
		if holders and load.partner:
			print("    the jobs on single GPUs themselves: cpu %.1f cores, disk %.0f MB/s" % load.partner)
		for c, b, p in top[:5]:
			i = bg.match(p) if bg.entries else None
			print("    %d %.1f cores %.0f MB/s %s%s" % (p.pid, c, b, cmdline(p)[:110],
				"" if i is None else "   [background: %s]" % bg.entries[i]["label"]))
		if load.background:
			print("    background (background.json): %s; the rest: cpu %.1f cores, disk %.0f MB/s" % (
				background_text(load.background), load.rest_cpu, load.rest_io))
	if bg.entries or bg.problems:
		print("background (%s): %s" % (bg.path, bg.text()))
		for problem in bg.problems:
			print("    " + problem)

def cmd_break(args):
	lock = make_lock()
	if not isinstance(lock, FileLock):
		state, info = lock.state()
		if state == "held":
			sys.exit("rig.py break: %s holds the rig (%s); stop it instead" % (
				"the job of a rig.py that has ended" if lock.orphan else "a running rig.py", info or "no RIG_BUSY"))
		if info and not lock.clear_stale():
			sys.exit("rig.py break: the rig was taken this moment (%s)" % lock.text())
		print("rig free" + (" (RIG_BUSY cleared: %s)" % info if info else ""))
		return
	with guarded(lock.path):
		for note in lock.sync():  # the GPUs' locks, and tickets, whose rig.py has ended
			print(note)
		state, info = lock.state()
		if state == "held":
			sys.exit("rig.py break: a running rig.py or its job holds the rig (%s); stop it instead" % info)
		if state == "shared":
			sys.exit("rig.py break: runs hold single GPUs (%s); stop them instead" % info)
		if state == "free":
			print("rig free: there is no gpu.lock")
			return
		retry(os.remove, lock.path)
	print("removed %s gpu.lock: %s" % ("a hand-written" if state == "hand" else "a stale", info))

def main(argv=None):
	# First, so that a rig.py watching a run can tell this one is a rig.py however it was started (over ssh)
	mark_self(((sys.argv[1:] if argv is None else argv) or ["?"])[0])
	for stream in (sys.stdout, sys.stderr):
		try:
			stream.reconfigure(errors="replace")
		except AttributeError:
			pass
	ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip(),
		formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
	sub = ap.add_subparsers(dest="action", required=True)
	def limits(p):
		p.add_argument("--cpu", type=float, default=float(os.environ.get("RIG_CPU", 2.0)),
			help="CPU outside the job above which run waits and the run is contaminated, in cores (default 2, or RIG_CPU)")
		p.add_argument("--io", type=float, default=float(os.environ.get("RIG_IO", 50)),
			help="the same for disk reads and writes, MB/s (default 50, or RIG_IO)")
		p.add_argument("--load-looks", type=int, default=int(os.environ.get("RIG_LOAD_LOOKS", 2)),
			help="load outside the job contaminates a run when it lasts this many looks in a row (default 2, or RIG_LOAD_LOOKS) ...")
		p.add_argument("--load-share", type=float, default=float(os.environ.get("RIG_LOAD_SHARE", 0.2)),
			help="... or comes in more than this share of the run's looks (default 0.2, or RIG_LOAD_SHARE)")
		p.add_argument("--strict", action="store_true", default=os.environ.get("RIG_STRICT") == "1",
			help="every look with load outside the job above --cpu or --io contaminates the run, and background.json "
			"is not used (or RIG_STRICT=1)")
	limits(sub.add_parser("status", help="who holds the rig, and who uses each device"))
	sub.add_parser("break", help="remove a lock that nothing holds any more, or one written by hand")
	sub.add_parser("version", help="this copy's version, and the installed one's")
	p = sub.add_parser("install", help="install this rig.py (a campy checkout's) as the rig's, when the rig is free")
	p.add_argument("--from", dest="source", help="the campy checkout (or its bench folder) to install from; "
		"default: this rig.py's own folder")
	p.add_argument("--check", action="store_true", help="only say whether the installed copy differs (exit 3 if it does)")
	p.add_argument("--owner", help="who installs it (default RIG_OWNER)")
	p.add_argument("--allow-dirty", action="store_true", help="install uncommitted changes to the lock's files")
	p.add_argument("--allow-older", action="store_true", help="install a commit older than the installed one")
	p.add_argument("--commit", help="the campy commit the files are from, when --from is a copy, not a git checkout")
	for name in ("run", "hold"):
		p = sub.add_parser(name)
		limits(p)
		p.add_argument("--no-wait", action="store_true", default=os.environ.get("RIG_NOWAIT") == "1",
			help="exit 75 instead of waiting when the rig is busy (or RIG_NOWAIT=1)")
		p.add_argument("--allow", type=parse_allow, default=os.environ.get("RIG_ALLOW", ""),
			help="kinds outside the job not waited for and not counted, only logged, comma-separated: ephys for a "
			"recording run beside SpikeGLX (or RIG_ALLOW)")
		p.add_argument("--gpu", type=parse_gpu, default=os.environ.get("RIG_GPU"),
			help="hold only this GPU (nvidia-smi's index; rig.py status lists them), so that a run holding another GPU "
			"can run beside it; run's job sees only it (CUDA_VISIBLE_DEVICES). Needs two or more GPUs and the file lock "
			"(Windows); elsewhere it holds the whole machine (or RIG_GPU)")
		p.add_argument("--whole", action="store_true", default=os.environ.get("RIG_WHOLE") == "1",
			help="with --gpu: hold the whole machine all the same, for timings (--strict does too) (or RIG_WHOLE=1)")
		p.add_argument("--log-dir", default=os.environ.get("RIG_LOGDIR"),
			help="where <tag>.rig.log goes (default: the current folder, or RIG_LOGDIR)")
		p.add_argument("owner", help="who runs it: the session's name")
		p.add_argument("tag", help="the run's name; its log is <tag>.rig.log")
		if name == "run":
			p.add_argument("cmd", nargs=argparse.REMAINDER, help="-- then the command")
		else:
			p.add_argument("pid", type=int)
	args = ap.parse_args(argv)
	if args.action == "run" and args.cmd[:1] == ["--"]:
		args.cmd = args.cmd[1:]
	{"status": cmd_status, "run": cmd_run, "hold": cmd_hold, "break": cmd_break, "version": cmd_version,
		"install": cmd_install}[args.action](args)

if __name__ == "__main__":
	main()
