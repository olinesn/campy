"""
One test at a time on a shared Linux test machine (the lab's EVO-X2).

Several people, or several Claude sessions, run timing tests on the same
machine: campy recordings, campy-live, replays of recorded videos, sleap-nn
benchmarks and training. Two of them at once spoil each other's numbers, and
nobody notices unless something says so. rig.py is that something: a lock
every test takes, and a watch over everything a test can be spoiled by.

  rig.py status                                 who holds the rig, and who uses each device
  rig.py run [options] <owner> <tag> -- cmd...  wait for the rig, then run cmd holding it
  rig.py hold [options] <owner> <tag> <pid>     hold the rig for a job already running

run waits until the rig is free and nothing else uses the GPU, the cameras,
the video encoder or the trigger, and nothing else loads the CPU or the disk;
then it runs cmd in its own session. Every 2 s while it runs, anything outside
the job that uses one of those, or a CPU or disk load outside the job above
--cpu cores or --io MB/s, is logged to <tag>.rig.log, whose last line says
RIG_CLEAN or RIG_CONTAMINATED. A contaminated run's timings are not to be
trusted: run it again.

What is watched (every process the user can see; other users' processes show
only their CPU use):
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

A process belongs to the job when it carries the job's RIG_JOB in its
environment (everything the job starts does, even with setsid or nohup), is in
the job's session, or descends from the job. A script can take the rig itself
by sourcing rig_guard.sh first, which re-runs it under rig.py unless it is
already part of the rig's current job.

Linux only; the Python standard library only (run it with /usr/bin/python3).
"""

import argparse, fcntl, os, pwd, re, shlex, signal, subprocess, sys, time

ROOT = os.path.expanduser(os.environ.get("RIG_ROOT", "~/sleap-bench"))
LOCK = os.path.join(ROOT, ".rig.lock")
INFO = os.path.join(ROOT, "RIG_BUSY")
PERIOD = float(os.environ.get("RIG_PERIOD", 2.0))  # s between looks
# Other files to watch as one of the kinds, e.g. RIG_EXTRA="trigger=/dev/ttyS0,gpu=/dev/nvidia0"
EXTRA = dict(reversed(kv.split("=", 1)) for kv in os.environ.get("RIG_EXTRA", "").split(",") if "=" in kv)
KINDS = ("gpu", "encoder", "camera", "trigger")
# Desktops drawing on the GPU, by /proc/<pid>/comm
DESKTOP = {"gnome-shell", "Xwayland", "Xorg", "Xvnc", "mutter", "kwin_wayland", "kwin_x11", "plasmashell",
	"sway", "weston", "Hyprland", "xfwm4", "TeamViewer"}
FLIR_VENDOR = "1e10"
HZ = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

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
	__slots__ = ("pid", "ppid", "sid", "start", "ticks", "io", "kinds", "cmd", "job", "user")

def read_proc(pid, cameras):
	"""A Proc for pid, or None if it is gone or a kernel thread."""
	p = Proc()
	p.pid = pid
	try:
		with open("/proc/%d/stat" % pid) as f:
			stat = f.read()
	except OSError:
		return None
	fields = stat[stat.rindex(")") + 2:].split()
	p.ppid, p.sid = int(fields[1]), int(fields[3])
	p.ticks = int(fields[11]) + int(fields[12])
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

def scan():
	cameras = flir_cameras()
	procs = {}
	for name in os.listdir("/proc"):
		if name.isdigit():
			p = read_proc(int(name), cameras)
			if p:
				procs[p.pid] = p
	return procs

class Watch:
	"""Which processes are outside the job, and the CPU and disk they use."""

	def __init__(self, job=None, sid=None, root_pid=None):
		self.job, self.sid, self.root_pid = job, sid, root_pid
		self.me = {os.getpid()}
		self.prev, self.prev_t = {}, None
		self.desktops = {}  # (pid, start): Proc, every desktop up at the last sample

	def own(self, p, procs):
		if p.pid in self.me or (self.job and p.job == self.job) or (self.sid and p.sid == self.sid):
			return True
		q, seen = p, 0
		while self.root_pid and q and seen < 64:
			if q.pid == self.root_pid:
				return True
			q, seen = procs.get(q.ppid), seen + 1
		return False

	def sample(self):
		"""(foreign device users, foreign load) now: [(Proc, kinds)], (cores, MB/s, [(cores, MB/s, Proc)])."""
		t, procs = time.monotonic(), scan()
		users, load, cpu, io = [], [], 0.0, 0.0
		self.desktops = {(p.pid, p.start): p for p in procs.values() if "desktop" in p.kinds}
		for p in procs.values():
			if self.own(p, procs):
				continue
			if p.kinds - {"desktop"}:
				users.append(p)
			key = (p.pid, p.start)
			if self.prev_t is not None and key in self.prev:
				dt = t - self.prev_t
				ticks, pio = self.prev[key]
				c = (p.ticks - ticks) / HZ / dt
				b = (p.io - pio) / 1e6 / dt if p.io is not None and pio is not None else 0.0
				cpu, io = cpu + c, io + b
				if c > 0.05 or b > 1:
					load.append((c, b, p))
		self.prev = {(p.pid, p.start): (p.ticks, p.io) for p in procs.values()}
		measured = self.prev_t is not None
		self.prev_t = t
		load.sort(key=lambda x: -(x[0] + x[1] / 50))
		return users, (cpu, io, load) if measured else None

def describe(p, width=150):
	return "%d %s %s" % (p.pid, ",".join(sorted(p.kinds)) or "-", p.cmd[:width])

def desktop_text(desktops):
	return ", ".join(sorted(set("%s (%s%s)" % (p.cmd.split()[0].rsplit("/", 1)[-1], p.user or "?",
		", the login screen" if p.user == "gdm" else "") for p in desktops.values()))) or "none"

def load_text(load, n=3):
	cpu, io, top = load
	return "cpu %.1f cores, disk %.0f MB/s; %s" % (cpu, io, "; ".join(
		"%d %.1f cores %.0f MB/s %s" % (p.pid, c, b, p.cmd[:80]) for c, b, p in top[:n]) or "nothing")

def overloaded(load, args):
	return load is not None and (load[0] > args.cpu or load[1] > args.io)

# The lock: a flock on LOCK while a job holds the rig (released by the kernel if rig.py dies),
# and RIG_BUSY saying who, which status shows only while the flock is really held.

def lock_held():
	os.makedirs(ROOT, exist_ok=True)
	fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o664)
	try:
		fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
		return False
	except OSError:
		return True
	finally:
		os.close(fd)

def holder():
	try:
		with open(INFO) as f:
			return f.read().strip()
	except OSError:
		return ""

def holder_job():
	m = re.search(r"\bjob=(\S+)", holder())
	return m.group(1) if m else None

def take(owner, tag, args):
	os.makedirs(ROOT, exist_ok=True)
	fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o664)
	try:
		fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
	except OSError:
		if args.no_wait:
			print("rig busy: %s" % (holder() or "(no RIG_BUSY)"), flush=True)
			sys.exit(75)
		print("rig busy: %s; waiting..." % (holder() or "(no RIG_BUSY)"), flush=True)
		fcntl.flock(fd, fcntl.LOCK_EX)
	# The rig is ours, but something started without rig.py may still be on the devices or the CPU
	w, said = Watch(), 0
	w.sample()
	while True:
		time.sleep(PERIOD)
		users, load = w.sample()
		if not users and not overloaded(load, args):
			break
		if args.no_wait:
			print("rig in use outside rig.py:", flush=True)
			for p in users:
				print("  " + describe(p))
			print("  load: " + load_text(load))
			sys.exit(75)
		if time.monotonic() - said > 30:
			said = time.monotonic()
			print("%s in use outside rig.py, waiting:" % time.strftime("%T"), flush=True)
			for p in users:
				print("  " + describe(p), flush=True)
			if overloaded(load, args):
				print("  load: " + load_text(load), flush=True)
	return fd

def write_info(owner, tag, job, pid, note=""):
	tmp = INFO + ".tmp"
	with open(tmp, "w") as f:
		f.write("%s %s since %s rigpid=%d job=%s pid=%d%s\n" % (
			owner, tag, time.strftime("%F %T"), os.getpid(), job, pid, note))
	os.replace(tmp, INFO)

def clear_info():
	try:
		with open(INFO, "w"):
			pass
	except OSError:
		pass

def watch(w, alive, log, args, on_signal):
	"""Log what is outside the job every PERIOD s until alive() is False; returns the sighting counts."""
	counts = dict.fromkeys(KINDS + ("desktop", "load"), 0)
	last = {}  # what was logged when, so a process that stays is logged every 30 s, not every 2 s
	def note(key, text):
		now = time.monotonic()
		if now - last.get(key, -1e9) >= 30:
			last[key] = now
			log(text)
	w.sample()
	before = dict(w.desktops)
	log("desktop on the GPU: %s" % desktop_text(before))
	while alive():
		users, load = w.sample()
		for key in set(before) ^ set(w.desktops):
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
		if on_signal[0]:
			break
		time.sleep(PERIOD)
	counts["desktops"] = desktop_text(w.desktops)
	return counts

def finish(counts, log, aborted=None):
	desktops = counts.pop("desktops", "none")
	n = sum(counts.values())
	if aborted:
		log("RIG_ABORTED by %s" % aborted)
	note = "" if desktops == "none" else "; desktop on the GPU: " + desktops
	if n == 0:
		log("RIG_CLEAN" + (" (%s)" % note[2:] if note else ""))
	else:
		log("RIG_CONTAMINATED (%d sightings: %s%s)" % (n, ", ".join("%s %d" % kv for kv in counts.items()), note))

def logger(path):
	def log(text):
		line = "%s %s" % (time.strftime("%T"), text)
		with open(path, "a") as f:
			f.write(line + "\n")
		print(line, flush=True)
	return log

def log_path(args, tag):
	return os.path.join(args.log_dir or os.getcwd(), re.sub(r"[^\w.+-]", "_", tag) + ".rig.log")

def cmd_run(args):
	if not args.cmd:
		sys.exit("rig.py run: no command (rig.py run <owner> <tag> -- cmd ...)")
	fd = take(args.owner, args.tag, args)
	job = "%s-%d" % (re.sub(r"\W", "_", args.tag), os.getpid())
	log = logger(log_path(args, args.tag))
	got = [None]
	def on(sig, frame):
		got[0] = signal.Signals(sig).name
	for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
		signal.signal(s, on)
	env = dict(os.environ, RIG_JOB=job, RIG_OWNER=args.owner, RIG_TAG=args.tag)
	proc = subprocess.Popen(args.cmd, env=env, start_new_session=True)
	write_info(args.owner, args.tag, job, proc.pid)
	log("rig held by %s for %s (job %s, pid %d): %s" % (args.owner, args.tag, job, proc.pid, " ".join(shlex.join(args.cmd).split())))
	w = Watch(job=job, sid=proc.pid, root_pid=proc.pid)
	counts = watch(w, lambda: proc.poll() is None, log, args, got)
	if got[0]:  # stopped: stop the job too, or it would go on without the rig
		try:
			os.killpg(proc.pid, getattr(signal, got[0]))
		except OSError:
			pass
		try:
			proc.wait(30)
		except subprocess.TimeoutExpired:
			os.killpg(proc.pid, signal.SIGKILL)
			proc.wait()
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
	clear_info()
	os.close(fd)
	sys.exit(code if code >= 0 else 128 - code)

def cmd_hold(args):
	try:
		start = read_proc(args.pid, {}).start
	except AttributeError:
		sys.exit("rig.py hold: pid %d is not running" % args.pid)
	fd = take(args.owner, args.tag, args)
	def alive():
		p = read_proc(args.pid, {})
		return p is not None and p.start == start
	if not alive():
		clear_info()
		sys.exit("rig.py hold: pid %d ended while waiting for the rig" % args.pid)
	p = read_proc(args.pid, {})
	job = p.job or "hold-%d" % args.pid
	log = logger(log_path(args, args.tag))
	got = [None]
	def on(sig, frame):
		got[0] = signal.Signals(sig).name
	for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
		signal.signal(s, on)
	write_info(args.owner, args.tag, job, args.pid, " (held for a running pid)")
	log("rig held by %s for %s (running pid %d): %s" % (args.owner, args.tag, args.pid, p.cmd))
	w = Watch(job=p.job, sid=p.sid, root_pid=args.pid)
	counts = watch(w, alive, log, args, got)
	finish(counts, log, got[0] and "%s (the held pid was left running)" % got[0])
	clear_info()
	os.close(fd)

def cmd_status(args):
	held = lock_held()
	info = holder()
	if held:
		print("rig held: %s" % (info or "(no RIG_BUSY)"))
	else:
		print("rig free" + (" (stale RIG_BUSY cleared: %s)" % info if info else ""))
		if info:
			clear_info()
	job = holder_job() if held else None
	w = Watch()
	w.sample()
	time.sleep(1.0)
	_, load = w.sample()
	procs = scan()
	cams = flir_cameras()
	names = {"gpu": "GPU compute (/dev/kfd)", "encoder": "video encoder (/dev/dri/renderD*)",
		"camera": "FLIR cameras (%d on USB: %s)" % (len(cams), " ".join(sorted(cams.values())) or "none"),
		"trigger": "trigger (%s)" % (" ".join(sorted(
			"/dev/" + d for d in os.listdir("/dev") if re.match(r"tty(ACM|USB)\d+$", d))) or "none plugged in")}
	for k in KINDS:
		users = [p for p in procs.values() if k in p.kinds]
		print("%-48s %s" % (names[k] + ":", "free" if not users else ""))
		for p in users:
			print("    %s%s" % ("[job] " if job and p.job == job else "", describe(p, 120)))
	print("%-48s %s" % ("desktop on the GPU (not waited for):", desktop_text(
		{(p.pid, p.start): p for p in procs.values() if "desktop" in p.kinds})))
	if load:
		mine = [x for x in load[2] if not (job and x[2].job == job)]
		print("load outside the job (1 s): cpu %.1f cores, disk %.0f MB/s%s" % (
			sum(c for c, b, p in mine), sum(b for c, b, p in mine),
			"   (run waits above %.1f cores or %.0f MB/s)" % (args.cpu, args.io)))
		for c, b, p in mine[:5]:
			print("    %d %.1f cores %.0f MB/s %s" % (p.pid, c, b, p.cmd[:110]))

def main(argv=None):
	ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip(),
		formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
	sub = ap.add_subparsers(dest="action", required=True)
	def limits(p):
		p.add_argument("--cpu", type=float, default=float(os.environ.get("RIG_CPU", 2.0)),
			help="CPU outside the job above which run waits and the run is contaminated, in cores (default 2, or RIG_CPU)")
		p.add_argument("--io", type=float, default=float(os.environ.get("RIG_IO", 50)),
			help="the same for disk reads and writes, MB/s (default 50, or RIG_IO)")
	limits(sub.add_parser("status", help="who holds the rig, and who uses each device"))
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
	{"status": cmd_status, "run": cmd_run, "hold": cmd_hold}[args.action](args)

if __name__ == "__main__":
	main()
