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

run waits until the rig is free and nothing else uses the GPU, the cameras,
the video encoder or the trigger, and nothing else loads the CPU or the disk;
then it runs cmd. Every 2 s while it runs, anything outside the job that uses
one of those, or a CPU or disk load outside the job above --cpu cores or --io
MB/s, is logged to <tag>.rig.log, whose last line says RIG_CLEAN or
RIG_CONTAMINATED. A contaminated run's timings are not to be trusted: run it
again. Nor are those of a run stopped before its job ended (Ctrl-C, SIGTERM, a
hangup, an error in rig.py), whose last line says RIG_ABORTED, or of one whose
log has no RIG_ line at all (its rig.py was killed): trust only RIG_CLEAN. A
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
  load     the CPU (cores) and disk (MB/s) used by everything outside the job
  desktop  a desktop drawing on the GPU (gnome-shell, Xwayland, Xorg, GNOME's gsd-xsettings,
           mutter-x11-frames and portal helper xdg-desktop-portal-gnome, ...; any user,
           the login screen too). run does not wait for it (it would not go away),
           but says in the log which was up, and a desktop that starts or ends
           during the run contaminates it.

On Windows (rig_windows.py beside this file says how each is seen):
  gpu      a CUDA program (type C in nvidia-smi -q -d PIDS), or any python*.exe
  encoder  ffmpeg*.exe
  camera   a Spinnaker DLL loaded (PySpin, SpinView)
  trigger  a COM port open
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

On an experiment machine (RIG_ROOT/experiment.json; docs/design/rig-fleet.md, section 5), where people
record experiments, run starts a test only when every Windows session (quser) has been idle 15+ min,
nothing outside it uses a device, the hour is allowed ("hours", default 22:00-06:00) and RIG_ROOT/PAUSE
isn't there (or its time has passed); otherwise it exits 75 at once, never waiting in line. On Windows the
banner (rig_banner.ps1, started in the session by a Task Scheduler task; rig_experiment_setup.ps1 sets it
up) must show the test first. The test ends within 2 s, every process of it, when RIG_ROOT/STOP appears
(the banner's Stop button, the "Stop campy test" shortcut, keyboard or mouse input at the machine, an
unlock, a Remote Desktop connect, a logon), when anything outside it starts using the GPU, a camera, the
encoder or the trigger (campy, SpinView, a Python), or when quser shows a session in use; the log's last
line is then RIG_STOPPED, run exits 75, and PAUSE keeps new tests away until 9 AM ("pause_until").

Installing and versions. Each machine runs one installed copy, in RIG_ROOT, which every
script calls (a campy checkout can be on any branch or commit, and every lock user on a
machine must run the same lock). Its version is a short hash of the six files (rig.py,
rig_windows.py, rig_guard.sh, rig_guard.ps1, rig_hold.ps1, rig_banner.ps1) with line endings ignored, so the
same commit shows the same version on every machine; status (its second line) and every
rig.log say it. To
update, run the checkout's own copy: python <checkout>/bench/rig.py install. It takes the rig
with --no-wait (exit 75 if busy, nothing changed), refuses while any other rig.py run, hold
or install, or rig_hold.ps1, is running or waiting (exit 75), backs the old files up to
RIG_ROOT/rig-backup/<old version>-<time>/, writes each new file beside the old one, checks
it, renames it into place, and records what it installed in RIG_ROOT/RIG_INSTALLED and
rig-install.log. Inside the rig's own job (an update script run under rig.py) it doesn't
wait for the rig that job holds. --check only says whether the installed copy differs
(exit 0 the same, 3 different). It refuses a checkout with uncommitted changes to these
files (--allow-dirty), or one older than what is installed (--allow-older). Files copied
from a checkout (not a git checkout themselves) can name their commit with --commit.

The Python standard library only. On Linux run it with /usr/bin/python3; on
Windows with any 64-bit Python 3.9 or newer (campy's own environment will do).
"""

import argparse, contextlib, datetime, hashlib, json, os, re, shlex, shutil, signal, subprocess, sys, time

WINDOWS = os.name == "nt"
if WINDOWS:
	import msvcrt
	sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
	import rig_windows
else:
	import fcntl, pwd

ROOT = os.path.expanduser(os.environ.get("RIG_ROOT", r"C:\campy_data\live" if WINDOWS else "~/sleap-bench"))
LOCK = os.path.join(ROOT, ".rig.lock")
INFO = os.path.join(ROOT, "RIG_BUSY")
PERIOD = float(os.environ.get("RIG_PERIOD", 2.0))  # s between looks
# Other files to watch as one of the kinds, e.g. RIG_EXTRA="trigger=/dev/ttyS0,gpu=/dev/nvidia0"
# (on Windows, NT device names: RIG_EXTRA="trigger=\Device\Serial0")
EXTRA = dict(reversed(kv.split("=", 1)) for kv in os.environ.get("RIG_EXTRA", "").split(",") if "=" in kv)
KINDS = ("gpu", "encoder", "camera", "trigger")
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
FILES = ("rig.py", "rig_windows.py", "rig_guard.sh", "rig_guard.ps1", "rig_hold.ps1", "rig_banner.ps1")
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
	if re.match(r"/dev/tty(ACM|USB)\d+$", path):
		return "trigger"
	if path in cameras:
		return "camera"
	return None

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
	__slots__ = ("pid", "ppid", "sid", "start", "cpu", "io", "kinds", "cmd", "job", "user")

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
	if stat[stat.index("(") + 1:stat.rindex(")")] in DESKTOP:
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

def scan(own=()):
	"""{pid: Proc} for every process. own: pids known to be the job's (Windows looks less at them)."""
	if WINDOWS:
		procs = {}
		on_gpu = gpu_processes()  # first, so nvidia-smi has ended before the processes are listed
		compute = {pid for pid, t in on_gpu.items() if "G" not in t}
		for pid, ppid, start, cpu, io, kinds, name, job in rig_windows.scan(JOBS, compute, EXTRA, own, set(on_gpu) - compute):
			p = Proc()
			p.pid, p.ppid, p.sid, p.start, p.cpu, p.io = pid, ppid, None, start, cpu, io
			p.kinds, p.cmd, p.job, p.user = kinds, name, job, None
			procs[pid] = p
		return procs
	cameras = flir_cameras()
	procs = {}
	for name in os.listdir("/proc"):
		if name.isdigit():
			p = read_proc(int(name), cameras)
			if p:
				procs[p.pid] = p
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

class Watch:
	"""Which processes are outside the job, and the CPU and disk they use."""

	def __init__(self, job=None, sid=None, root_pid=None):
		self.job, self.sid, self.root_pid = job, sid, root_pid
		self.me = {os.getpid()}
		self.prev, self.prev_t, self.prev_disk = {}, None, None
		self.desktops = {}  # (pid, start): Proc, every desktop up at the last sample
		self.ancestors = set()
		self.own_pids = set()

	def own(self, p, procs):
		if p.pid in self.me or (self.job and p.job == self.job) or (self.sid and p.sid == self.sid):
			return True
		if WINDOWS and p.pid in self.ancestors:  # what started rig.py waits for it, and may be a python.exe
			return True
		q, seen = p, 0
		while q and seen < 64:
			if q.pid == self.root_pid or q.pid in self.me:
				return True
			parent = procs.get(q.ppid)
			if parent and parent.start > q.start:  # a newer process that reused the parent's pid
				return False
			q, seen = parent, seen + 1
		return False

	def sample(self):
		"""(foreign device users, foreign load) now: [(Proc, kinds)], (cores, MB/s, [(cores, MB/s, Proc)],
		MB/s all disks moved or None)."""
		t, procs = time.perf_counter(), scan(self.own_pids)  # Windows' monotonic() ticks every 15.6 ms
		disk = disk_bytes()
		users, load, cpu, io, own_io = [], [], 0.0, 0.0, 0.0
		self.desktops = {(p.pid, p.start): p for p in procs.values() if "desktop" in p.kinds}
		self.ancestors, q = set(), procs.get(os.getpid())
		while q and q.ppid in procs and procs[q.ppid].start <= q.start and q.ppid not in self.ancestors:
			self.ancestors.add(q.ppid)
			q = procs[q.ppid]
		own, measured = set(), self.prev_t is not None and t > self.prev_t
		for p in procs.values():
			key, c, b = (p.pid, p.start), 0.0, 0.0
			if measured and key in self.prev:
				dt = t - self.prev_t
				pcpu, pio = self.prev[key]
				c = (p.cpu - pcpu) / dt
				b = (p.io - pio) / 1e6 / dt if p.io is not None and pio is not None else 0.0
			if self.own(p, procs):
				own.add(p.pid)
				own_io += b
				continue
			if p.kinds - {"desktop"}:
				users.append(p)
			cpu, io = cpu + c, io + b
			if c > 0.05 or b > 1:
				load.append((c, b, p))
		self.own_pids = own
		self.prev = {(p.pid, p.start): (p.cpu, p.io) for p in procs.values()}
		disks = None
		if measured and disk is not None and self.prev_disk is not None:
			# What outside programs read and wrote, as far as it reached a disk; and at least what the
			# disks moved beyond the job's own reads and writes, which catches writes that Windows'
			# file cache took and the kernel wrote out later, after their program had gone
			disks = max(0.0, (disk - self.prev_disk) / 1e6 / (t - self.prev_t))
			io = max(min(io, disks), disks - own_io)
		self.prev_disk = disk
		self.prev_t = t
		load.sort(key=lambda x: -(x[0] + x[1] / 50))
		return users, (cpu, io, load, disks) if measured else None

def describe(p, width=150):
	return "%d %s %s" % (p.pid, ",".join(sorted(p.kinds)) or "-", cmdline(p)[:width])

def desktop_text(desktops, short=False):
	if WINDOWS:
		names = sorted(set(p.cmd for p in desktops.values()), key=str.lower)
		return "none" if not names else "%d processes" % len(desktops) if short else "%d processes: %s" % (
			len(desktops), ", ".join(names))
	return ", ".join(sorted(set("%s (%s%s)" % (p.cmd.split()[0].rsplit("/", 1)[-1], p.user or "?",
		", the login screen" if p.user == "gdm" else "") for p in desktops.values()))) or "none"

def load_text(load, n=3):
	cpu, io, top, disks = load
	return "cpu %.1f cores, disk %.0f MB/s%s; %s" % (cpu, io, "" if disks is None else " (all disks %.0f)" % disks, "; ".join(
		"%d %.1f cores %.0f MB/s %s" % (p.pid, c, b, cmdline(p)[:80]) for c, b, p in top[:n]) or "nothing")

def overloaded(load, args):
	return load is not None and (load[0] > args.cpu or load[1] > args.io)

# The locks. Each has acquire (wait for it, or exit 75), record (say who holds it and for which job),
# ours (still held by this rig.py), release, and state/describe for status and break.

ENDED = " (its rig.py has ended, but its job runs on"

class Flock:
	"""Linux: a flock on LOCK while a run holds the rig (released by the kernel if rig.py dies), and
	RIG_BUSY saying who, from the moment its rig.py has the flock, and the pid and start of its job
	('<owner> <tag> since <date time> rigpid=<pid> [job=<job> pid=<pid> pidstart=<t>]'). A rig.py that
	was killed (SIGKILL, the OOM killer) loses its flock but leaves its job running, so the rig stays
	held, by RIG_BUSY, until that job ends (as FileLock does)."""

	def __init__(self):
		self.fd, self.orphan = None, False

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

class FileLock:
	"""Windows (or RIG_LOCK=file): ROOT/gpu.lock, created only if it is absent (O_EXCL, CreateNew),
	one line long: '<owner> <tag> since <date time> rigpid=<pid> rigstart=<t> [job=<job> pid=<pid> pidstart=<t>]'.
	It counts as free once both pids have ended, so a crash leaves nothing to clean up. A line
	without rigpid was written by hand, and holds the rig until its writer deletes it (or break)."""

	def __init__(self):
		self.path = os.path.join(ROOT, "gpu.lock")
		self.line = None

	def text(self):
		"""The lock's line; None if there is no lock."""
		try:
			with open(self.path, encoding="utf-8", errors="replace") as f:
				return f.read().strip()
		except FileNotFoundError:
			return None
		except OSError:
			return ""  # being written or deleted this moment

	def age(self):
		try:
			return time.time() - os.path.getmtime(self.path)
		except OSError:
			return 0.0

	def state(self):
		"""(state, line): 'free', 'held' (a live rig.py or job), 'stale' (both ended) or 'hand' (written by hand)."""
		text = self.text()
		if text is None:
			return "free", ""
		m = re.search(r"\brigpid=(\d+) rigstart=(-?\d+)", text)
		if not m:
			return ("stale" if text == "" and self.age() > 60 else "hand"), text
		pids = [(int(m.group(1)), int(m.group(2)))] + ([named_job(text)] if named_job(text) else [])
		return ("held" if any(alive(pid, start) for pid, start in pids) else "stale"), text

	def describe(self, state, text):
		if state == "hand":
			return "%s (gpu.lock written by hand %s, %.1f h ago: it holds the rig until whoever wrote it " \
				"deletes it, or rig.py break)" % (repr(text), time.strftime("%F %T", time.localtime(time.time() - self.age())),
				self.age() / 3600)
		return text

	def acquire(self, owner, tag, no_wait):
		os.makedirs(ROOT, exist_ok=True)
		self.owner, self.tag, said, last = owner, tag, None, None
		while True:
			with guarded(self.path):
				state, text = self.state()
				if state == "stale":
					say("%s stale gpu.lock removed (its rig.py and job have ended): %s" % (time.strftime("%T"), text))
					retry(os.remove, self.path)
					state = "free"
				if state == "free":
					try:
						fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o664)
					except FileExistsError:  # written this moment by something that doesn't use the guard
						state, text = "hand", self.text() or ""
					else:
						os.close(fd)
						self.since = time.strftime("%F %T")
						self.record()
						return
			if no_wait:
				say("rig busy: %s" % self.describe(state, text))
				sys.exit(75)
			if text != last or time.monotonic() - said > 300:
				say("rig busy: %s; waiting..." % self.describe(state, text))
				said, last = time.monotonic(), text
			time.sleep(PERIOD)

	def record(self, job=None, pid=None, note=""):
		line = "%s %s since %s rigpid=%d rigstart=%d" % (self.owner, self.tag, self.since, os.getpid(), proc_start(os.getpid()) or 0)
		if job:  # pid None: the job is about to start
			line += " job=%s %s" % (job, "starting" if pid is None else "pid=%d pidstart=%d" % (pid, proc_start(pid) or 0))
		def write():
			with open(self.path, "w", encoding="utf-8") as f:
				f.write(line + note + "\n")
		retry(write)
		self.line = line + note

	def ours(self):
		return self.line is None or self.text() in (self.line, "")

	def release(self):
		if self.line is None:
			return
		with guarded(self.path):
			text = self.text()
			if text == self.line:
				retry(os.remove, self.path)
			elif text is not None:
				say("gpu.lock is no longer this run's, so it was left as it is: %s" % text)
		self.line = None

def make_lock():
	return FileLock() if WINDOWS or os.environ.get("RIG_LOCK") == "file" else Flock()

WAITING = " (waiting for the rig to be quiet)"

def wait_quiet(w, args):
	"""Wait until nothing outside w's job uses a device or loads the CPU or disk; exit 75 instead with --no-wait.
	The rig is ours by then, but something started without rig.py may still be at work."""
	said = 0
	w.sample()
	while True:
		time.sleep(PERIOD)
		users, load = w.sample()
		if not users and not overloaded(load, args):
			return
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
			if overloaded(load, args):
				say("  load: " + load_text(load))

def watch(w, alive, log, args, on_signal, lock, stopper=None):
	"""Log what is outside the job every PERIOD s until alive() is False; returns the sighting counts.
	With a stopper (an experiment machine), it ends as soon as the stopper says why, its STOP file
	looked for every 0.2 s."""
	counts = dict.fromkeys(KINDS + ("desktop", "load", "lock"), 0)
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
		clocks.sample()
		for key in set(before) ^ set(w.desktops):
			if not WINDOWS:  # Windows starts and ends programs that draw (WebView, search) by itself
				counts["desktop"] += 1
			p = w.desktops.get(key) or before[key]
			log("DESKTOP %s: %s" % ("started" if key in w.desktops else "ended", describe(p)))
		before = dict(w.desktops)
		for p in users:
			for k in p.kinds:
				counts[k] += 1
			note(("dev", p.pid, p.start), "FOREIGN %s" % describe(p))
		if overloaded(load, args):
			counts["load"] += 1
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
	counts["desktops"] = desktop_text(w.desktops, short=True)
	counts["clocks"] = clocks.summary()
	return counts

def finish(counts, log, aborted=None, stopped=None):
	"""The run's last line: RIG_CLEAN or RIG_CONTAMINATED; or, for a run stopped before its job ended,
	RIG_ABORTED with what was seen until then, never RIG_CLEAN; or RIG_STOPPED for one stopped at an
	experiment machine (stopped says how)."""
	desktops = counts.pop("desktops", "none")
	lines, gpu = counts.pop("clocks", ([], ""))
	for line in lines:
		log(line)
	n = sum(counts.values())
	notes = ([] if desktops == "none" else ["desktop on the GPU: " + desktops]) + ([gpu] if gpu else [])
	if n:
		notes.insert(0, "%d sightings: %s" % (n, ", ".join("%s %d" % kv for kv in counts.items())))
	if stopped:
		log("RIG_STOPPED at the machine by %s (until then: %s)" % (stopped, "; ".join(notes if n else ["clean"] + notes)))
	elif aborted:
		log("RIG_ABORTED by %s (until then: %s)" % (aborted, "; ".join(notes if n else ["clean"] + notes)))
	elif n == 0:
		log("RIG_CLEAN" + (" (%s)" % "; ".join(notes) if notes else ""))
	else:
		log("RIG_CONTAMINATED (%s)" % "; ".join(notes))

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

def rig_processes(procs=None):
	"""[(pid, command line)] of every other rig.py run, hold or install, and rig_hold.ps1, running or
	waiting; this rig.py and the processes it runs under (an update script's rig.py run) left out."""
	procs = scan() if procs is None else procs
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
		text = cmdline(p)
		if text and RIG_PROC.search(text):
			found.append((p.pid, text))
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

def experiment_blockers(cfg, banner=True, now=None):
	"""(why a test may not start now, [] if it may; the sessions seen)."""
	now = now or datetime.datetime.now()
	why = []
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

	def __init__(self, cfg, found):
		self.cfg, self.why = cfg, None
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
			self.why = "%s started outside the test, using the %s (pid %d)" % (name, ",".join(sorted(p.kinds - {"desktop"})), p.pid)
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
	return state == "held" and ("job=%s " % job) in (text + " ")

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
	if cfg:  # an experiment machine: a test starts only now or not at all, never waits in line
		args.no_wait = True
		why, _ = experiment_blockers(cfg)
		if why:
			say("rig.py run: not now on this experiment machine: %s" % "; ".join(why))
			sys.exit(75)
	lock.acquire(args.owner, args.tag, args.no_wait)
	got = [None]
	closing, done = catch_console_close(got, lambda: abandon_run(lock))
	proc, found = None, []
	try:
		lock.record(note=WAITING)
		wait_quiet(Watch(), args)
		if cfg:  # again, now that the rig is ours; a STOP left from before is not for this run
			why, found = experiment_blockers(cfg)
			if why:
				say("rig.py run: not now on this experiment machine: %s" % "; ".join(why))
				sys.exit(75)
			remove_marker("STOP")
		job = "%s-%d" % (re.sub(r"\W", "_", args.tag), os.getpid())
		log = logger(log_path(args, args.tag))
		# The log's first line before the job starts: a run whose job ran has a log, even when rig.py
		# is killed (SIGKILL) the moment RIG_BUSY names the job, and the log says nothing more then
		log("rig.py " + version_text())
		catch_signals(got)
		env = dict(os.environ, RIG_JOB=job, RIG_OWNER=args.owner, RIG_TAG=args.tag)
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
		w = Watch(job=job, sid=None if WINDOWS else proc.pid, root_pid=proc.pid)
		stopper = Stopper(cfg, found) if cfg else None
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
	lock = make_lock()
	lock.acquire(args.owner, args.tag, args.no_wait)
	got = [None]
	closing, done = catch_console_close(got, lock.release)  # the held pid is left running, as on a signal
	held = False
	def running():
		return alive(args.pid, start)
	try:
		lock.record(note=WAITING)
		wait_quiet(Watch(root_pid=args.pid), args)
		if not running():
			sys.exit("rig.py hold: pid %d ended while waiting for the rig" % args.pid)
		p = scan().get(args.pid)
		job = (p and p.job) or "hold-%d" % args.pid
		log = logger(log_path(args, args.tag))
		log("rig.py " + version_text())  # before RIG_BUSY names the pid, as in run
		catch_signals(got)
		lock.record(job, args.pid, " (held for a running pid)")
		held = True
		log("rig held by %s for %s (running pid %d): %s" % (args.owner, args.tag, args.pid, cmdline(p) if p else "?"))
		w = Watch(job=p and p.job, sid=p and p.sid, root_pid=args.pid)
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

def cmd_status(args):
	lock = make_lock()
	state, info = lock.state()
	if state in ("held", "hand"):
		print("rig held: %s" % lock.describe(state, info))
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
	m = re.search(r"\bpid=(\d+)", info) if state == "held" else None
	w = Watch(job=job, root_pid=m and int(m.group(1)))  # the job's processes (on Windows, those it started)
	w.sample()
	time.sleep(1.0)
	_, load = w.sample()
	procs = scan()
	if WINDOWS:
		ports = sorted(rig_windows.serial_devices().values())
		names = {"gpu": "GPU (nvidia-smi%s, python*.exe)" % ("" if NVSMI else ": none"), "encoder": "video encoder (ffmpeg*.exe)",
			"camera": "FLIR cameras (Spinnaker loaded)", "trigger": "trigger (%s)" % (" ".join(ports) or "no COM port")}
	else:
		cams = flir_cameras()
		jetson = os.path.isdir("/dev/nvgpu")  # a Jetson's integrated GPU (see device_kind)
		names = {"gpu": "GPU compute (%s)" % ("/dev/nvgpu/*, /dev/nvhost-*gpu" if jetson else "/dev/kfd"),
			"encoder": "video encoder (%s)" % ("/dev/v4l2-nvenc, /dev/dri/renderD*" if jetson else "/dev/dri/renderD*"),
			"camera": "FLIR cameras (%d on USB: %s)" % (len(cams), " ".join(sorted(cams.values())) or "none"),
			"trigger": "trigger (%s)" % (" ".join(sorted(
				"/dev/" + d for d in os.listdir("/dev") if re.match(r"tty(ACM|USB)\d+$", d))) or "none plugged in")}
	for k in KINDS:
		users = [p for p in procs.values() if k in p.kinds and p.pid != os.getpid()]
		print("%-48s %s" % (names[k] + ":", "free" if not users else ""))
		for p in users:
			print("    %s%s" % ("[job] " if job and p.job == job else "", describe(p, 120)))
	print("%-48s %s" % ("desktop on the GPU (not waited for):", desktop_text(
		{(p.pid, p.start): p for p in procs.values() if "desktop" in p.kinds})))
	for row in gpu_clocks():
		print(clock_text(row))
	if load:
		cpu, io, top, disks = load
		print("load outside the job (1 s): cpu %.1f cores, disk %.0f MB/s%s   (run waits above %.1f cores or %.0f MB/s)" % (
			cpu, io, "" if disks is None else " (all disks %.0f)" % disks, args.cpu, args.io))
		for c, b, p in top[:5]:
			print("    %d %.1f cores %.0f MB/s %s" % (p.pid, c, b, cmdline(p)[:110]))

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
		state, info = lock.state()
		if state == "held":
			sys.exit("rig.py break: a running rig.py or its job holds the rig (%s); stop it instead" % info)
		if state == "free":
			print("rig free: there is no gpu.lock")
			return
		retry(os.remove, lock.path)
	print("removed %s gpu.lock: %s" % ("a hand-written" if state == "hand" else "a stale", info))

def main(argv=None):
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
