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

run waits until the rig is free and nothing else uses the GPU, the cameras,
the video encoder or the trigger, and nothing else loads the CPU or the disk;
then it runs cmd. Every 2 s while it runs, anything outside the job that uses
one of those, or a CPU or disk load outside the job above --cpu cores or --io
MB/s, is logged to <tag>.rig.log, whose last line says RIG_CLEAN or
RIG_CONTAMINATED. A contaminated run's timings are not to be trusted: run it
again. With an NVIDIA GPU (nvidia-smi), the log also follows the GPU's clock
and why it is held down (its throttle reasons): the same GPU can run a test at
210 MHz or at 2,500.

On Linux (every process the user can see; other users' processes show only their CPU use):
  gpu      /dev/kfd                 GPU compute (ROCm: torch, sleap-nn)
  encoder  /dev/dri/renderD*        VA-API encoding (campy's ffmpegs); ROCm opens it too
  camera   /dev/bus/usb/... of a FLIR camera (USB vendor 1e10), or libSpinnaker
           loaded (a process that can open, reset or enumerate the cameras)
  trigger  /dev/ttyACM*, /dev/ttyUSB*  the trigger Arduino
  load     the CPU (cores) and disk (MB/s) used by everything outside the job
  desktop  a desktop drawing on the GPU (gnome-shell, Xwayland, Xorg, ...; any user,
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
current job.

The lock. On Linux, a flock on RIG_ROOT/.rig.lock (~/sleap-bench), which the
kernel releases if rig.py dies, and RIG_BUSY saying who holds it. On Windows,
the file C:\\campy_data\\live\\gpu.lock (RIG_ROOT), created only if it is
absent and deleted when the run ends. It says who holds the rig, since when,
and the process ids of rig.py and of the job: a lock whose rig.py and job have
both ended counts as free, so a crash can't leave the rig locked. A gpu.lock
written by hand ("<who> <time>", as sessions did before rig.py ran on Windows)
is respected until whoever wrote it deletes it, or `rig.py break`. RIG_LOCK=file
uses the Windows lock on Linux too.

The Python standard library only. On Linux run it with /usr/bin/python3; on
Windows with any 64-bit Python 3.9 or newer (campy's own environment will do).
"""

import argparse, contextlib, os, re, shlex, shutil, signal, subprocess, sys, time

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
# Desktops drawing on the GPU, by /proc/<pid>/comm
DESKTOP = {"gnome-shell", "Xwayland", "Xorg", "Xvnc", "mutter", "kwin_wayland", "kwin_x11", "plasmashell",
	"sway", "weston", "Hyprland", "xfwm4", "TeamViewer"}
FLIR_VENDOR = "1e10"
HZ = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
NO_WINDOW = 0x08000000 if WINDOWS else 0  # CREATE_NO_WINDOW: no console flashing up for nvidia-smi
JOBS = {}  # Windows: {job name: rig_windows.Job} of the job rig.py runs

def device_kind(path, cameras):
	if path in EXTRA:
		return EXTRA[path]
	if path == "/dev/kfd":
		return "gpu"
	if path.startswith("/dev/dri/renderD"):
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
		t, procs = time.monotonic(), scan(self.own_pids)
		disk = disk_bytes()
		users, load, cpu, io, own_io = [], [], 0.0, 0.0, 0.0
		self.desktops = {(p.pid, p.start): p for p in procs.values() if "desktop" in p.kinds}
		self.ancestors, q = set(), procs.get(os.getpid())
		while q and q.ppid in procs and procs[q.ppid].start <= q.start and q.ppid not in self.ancestors:
			self.ancestors.add(q.ppid)
			q = procs[q.ppid]
		own = set()
		for p in procs.values():
			key, c, b = (p.pid, p.start), 0.0, 0.0
			if self.prev_t is not None and key in self.prev:
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
		measured, disks = self.prev_t is not None, None
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

class Flock:
	"""Linux: a flock on LOCK while a job holds the rig (released by the kernel if rig.py dies),
	and RIG_BUSY saying who, which status shows only while the flock is really held."""

	def __init__(self):
		self.fd = None

	def text(self):
		try:
			with open(INFO) as f:
				return f.read().strip()
		except OSError:
			return ""

	def state(self):
		"""('held' or 'free', what RIG_BUSY says)."""
		os.makedirs(ROOT, exist_ok=True)
		fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o664)
		try:
			fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
			held = False
		except OSError:
			held = True
		finally:
			os.close(fd)
		return ("held" if held else "free"), self.text()

	def acquire(self, owner, tag, no_wait):
		os.makedirs(ROOT, exist_ok=True)
		self.owner, self.tag = owner, tag
		self.fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o664)
		try:
			fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except OSError:
			if no_wait:
				print("rig busy: %s" % (self.text() or "(no RIG_BUSY)"), flush=True)
				sys.exit(75)
			print("rig busy: %s; waiting..." % (self.text() or "(no RIG_BUSY)"), flush=True)
			fcntl.flock(self.fd, fcntl.LOCK_EX)

	def record(self, job, pid, note=""):
		tmp = INFO + ".tmp"
		with open(tmp, "w") as f:
			f.write("%s %s since %s rigpid=%d job=%s pid=%d%s\n" % (
				self.owner, self.tag, time.strftime("%F %T"), os.getpid(), job, pid, note))
		os.replace(tmp, INFO)

	def ours(self):
		return True

	def clear(self):
		try:
			with open(INFO, "w"):
				pass
		except OSError:
			pass

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
		pids = [(int(m.group(1)), int(m.group(2)))]
		m = re.search(r"\bpid=(\d+) pidstart=(-?\d+)", text)
		if m:
			pids.append((int(m.group(1)), int(m.group(2))))
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
					print("%s stale gpu.lock removed (its rig.py and job have ended): %s" % (time.strftime("%T"), text), flush=True)
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
				print("rig busy: %s" % self.describe(state, text), flush=True)
				sys.exit(75)
			if text != last or time.monotonic() - said > 300:
				print("rig busy: %s; waiting..." % self.describe(state, text), flush=True)
				said, last = time.monotonic(), text
			time.sleep(PERIOD)

	def record(self, job=None, pid=None, note=""):
		line = "%s %s since %s rigpid=%d rigstart=%d" % (self.owner, self.tag, self.since, os.getpid(), proc_start(os.getpid()) or 0)
		if job:
			line += " job=%s pid=%d pidstart=%d" % (job, pid, proc_start(pid) or 0)
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
				print("gpu.lock is no longer this run's, so it was left as it is: %s" % text, flush=True)
		self.line = None

def make_lock():
	return FileLock() if WINDOWS or os.environ.get("RIG_LOCK") == "file" else Flock()

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
			print("rig in use outside rig.py:", flush=True)
			for p in users:
				print("  " + describe(p))
			print("  load: " + load_text(load))
			sys.exit(75)
		if time.monotonic() - said > 30:
			said = time.monotonic()
			print("%s in use outside rig.py, waiting:" % time.strftime("%H:%M:%S"), flush=True)
			for p in users:
				print("  " + describe(p), flush=True)
			if overloaded(load, args):
				print("  load: " + load_text(load), flush=True)

def watch(w, alive, log, args, on_signal, lock):
	"""Log what is outside the job every PERIOD s until alive() is False; returns the sighting counts."""
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
		time.sleep(PERIOD)
	counts["desktops"] = desktop_text(w.desktops, short=True)
	counts["clocks"] = clocks.summary()
	return counts

def finish(counts, log, aborted=None):
	desktops = counts.pop("desktops", "none")
	lines, gpu = counts.pop("clocks", ([], ""))
	for line in lines:
		log(line)
	n = sum(counts.values())
	if aborted:
		log("RIG_ABORTED by %s" % aborted)
	notes = ([] if desktops == "none" else ["desktop on the GPU: " + desktops]) + ([gpu] if gpu else [])
	if n == 0:
		log("RIG_CLEAN" + (" (%s)" % "; ".join(notes) if notes else ""))
	else:
		log("RIG_CONTAMINATED (%d sightings: %s)" % (n, "; ".join([", ".join("%s %d" % kv for kv in counts.items())] + notes)))

def logger(path):
	def log(text):
		line = "%s %s" % (time.strftime("%H:%M:%S"), text)
		with open(path, "a", encoding="utf-8") as f:
			f.write(line + "\n")
		print(line, flush=True)
	return log

def log_path(args, tag):
	return os.path.join(args.log_dir or os.getcwd(), re.sub(r"[^\w.+-]", "_", tag) + ".rig.log")

def catch_signals():
	"""got[0] becomes the name of the first stopping signal (Ctrl-C, Ctrl-Break, SIGTERM, SIGHUP)."""
	got = [None]
	def on(sig, frame):
		got[0] = signal.Signals(sig).name
	for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
		if hasattr(signal, name):
			signal.signal(getattr(signal, name), on)
	return got

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

def cmd_run(args):
	if not args.cmd:
		sys.exit("rig.py run: no command (rig.py run <owner> <tag> -- cmd ...)")
	lock = make_lock()
	lock.acquire(args.owner, args.tag, args.no_wait)
	try:
		wait_quiet(Watch(), args)
		job = "%s-%d" % (re.sub(r"\W", "_", args.tag), os.getpid())
		log = logger(log_path(args, args.tag))
		got = catch_signals()
		env = dict(os.environ, RIG_JOB=job, RIG_OWNER=args.owner, RIG_TAG=args.tag)
		proc = start_job(args.cmd, env, log)
		lock.record(job, proc.pid)
		log("rig held by %s for %s (job %s, pid %d): %s" % (args.owner, args.tag, job, proc.pid, " ".join(shlex.join(args.cmd).split())))
		w = Watch(job=job, sid=None if WINDOWS else proc.pid, root_pid=proc.pid)
		counts = watch(w, lambda: proc.poll() is None, log, args, got, lock)
		if got[0]:
			stop_job(proc, got[0])
		code = proc.wait()
		# Whatever the job left running on a device keeps the rig until it ends (at most 60 s)
		def leftovers():
			return [p for p in scan().values() if p.job == job and p.kinds]
		t0 = time.monotonic()
		while leftovers() and time.monotonic() - t0 < 60:
			time.sleep(PERIOD)
		for p in leftovers():
			log("LEFTOVER still running after the job: %s" % describe(p))
		log("job exit %s" % code)
		finish(counts, log, got[0])
	finally:
		lock.release()
		for j in JOBS.values():
			j.close()
		JOBS.clear()
	sys.exit(code if code >= 0 else 128 - code)

def cmd_hold(args):
	start = proc_start(args.pid)
	if start is None:
		sys.exit("rig.py hold: pid %d is not running" % args.pid)
	lock = make_lock()
	lock.acquire(args.owner, args.tag, args.no_wait)
	try:
		def running():
			return alive(args.pid, start)
		wait_quiet(Watch(root_pid=args.pid), args)
		if not running():
			sys.exit("rig.py hold: pid %d ended while waiting for the rig" % args.pid)
		p = scan().get(args.pid)
		job = (p and p.job) or "hold-%d" % args.pid
		log = logger(log_path(args, args.tag))
		got = catch_signals()
		lock.record(job, args.pid, " (held for a running pid)")
		log("rig held by %s for %s (running pid %d): %s" % (args.owner, args.tag, args.pid, cmdline(p) if p else "?"))
		w = Watch(job=p and p.job, sid=p and p.sid, root_pid=args.pid)
		counts = watch(w, running, log, args, got, lock)
		finish(counts, log, got[0] and "%s (the held pid was left running)" % got[0])
	finally:
		lock.release()

def cmd_status(args):
	lock = make_lock()
	state, info = lock.state()
	if state == "held":
		print("rig held: %s" % (info or "(no RIG_BUSY)"))
	elif state == "hand":
		print("rig held: %s" % lock.describe(state, info))
	else:
		print("rig free" + (" (stale %s cleared: %s)" % ("gpu.lock" if isinstance(lock, FileLock) else "RIG_BUSY", info) if info else ""))
		if info:
			if isinstance(lock, FileLock):
				with guarded(lock.path):
					if lock.state()[0] == "stale":
						retry(os.remove, lock.path)
			else:
				lock.clear()
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
		names = {"gpu": "GPU compute (/dev/kfd)", "encoder": "video encoder (/dev/dri/renderD*)",
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
			sys.exit("rig.py break: a running rig.py holds the rig (%s); stop it instead" % (info or "no RIG_BUSY"))
		lock.clear()
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
	{"status": cmd_status, "run": cmd_run, "hold": cmd_hold, "break": cmd_break}[args.action](args)

if __name__ == "__main__":
	main()
