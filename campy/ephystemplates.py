"""
A live sorter's templates from a recording session (campy-ephys templates):
the probe's bouts joined into one file, Kilosort 4 run on it in an
environment of its own, then campy-ephys train on the result. On the
ChronicRecord rig a session's SpikeGLX run has one file per bout (the
Pico's gate: <run>_g0_t0, _t1, ...), and Kilosort and the trainer take one.

  campy-ephys templates --session <recording folder> --out <folder> --kilosort-python <its python>

Each step is skipped when its output is already there, so a run that stopped
goes on from where it was. It reads the session's files and writes only in
--out. Run it when nothing records: Kilosort holds the GPU for minutes, and
the training span is held in memory (about 46 MB a second of a 384-channel
probe).
"""

import csv, glob, os, re, shutil, subprocess, sys, time

import numpy as np

from campy import ephyslive as el

MINUTES = 20.0 # of the session joined for Kilosort, from its first bout
TRAIN_S = 240.0 # the training span: the joined file's last seconds (the newest, nearest the sessions that follow)
SAME = ("nSavedChans", "imSampRate", "snsApLfSy", "snsGeomMap", "imroTbl") # the same in every bout joined
BOUT = re.compile(r"_g(\d+)_t(\d+)\.([^.]+)\.ap\.bin$")
COPY_BLOCK = 64 << 20 # bytes at a time


class Bout:
	def __init__(self, path, meta):
		self.path, self.meta = path, meta
		m = BOUT.search(os.path.basename(path))
		self.g, self.t, self.probe = int(m.group(1)), int(m.group(2)), m.group(3)
		self.saved = int(meta["nSavedChans"])
		self.samples = os.path.getsize(path) // (2 * self.saved)
		self.first = int(meta.get("firstSample", 0) or 0)


def Bouts(folder, probe=None, say=print):
	"""A session's bout files of one probe (its stream: imec0, imec1000, ...), in SpikeGLX's order (g, then t).
	folder: the recording's folder, its spikeglx folder, or a run folder. A file whose .meta SpikeGLX didn't finish
	(no fileSizeBytes, or another size) is left out, and said."""
	found = {}
	for path in glob.glob(os.path.join(folder, "**", "*.ap.bin"), recursive=True):
		if not BOUT.search(os.path.basename(path)):
			continue
		meta_path = path[:-4] + ".meta"
		if not os.path.isfile(meta_path):
			say("{}: no .meta beside it: left out".format(path))
			continue
		meta = el.ReadMeta(meta_path)
		size = os.path.getsize(path)
		if str(meta.get("fileSizeBytes", "")) != str(size):
			say("{}: its .meta doesn't give its size ({} bytes; SpikeGLX didn't finish it?): left out".format(path, size))
			continue
		bout = Bout(path, meta)
		found.setdefault(bout.probe, []).append(bout)
	if not found:
		raise el.EphysError("No probe's bouts (<run>_g0_tN.<probe>.ap.bin with its .meta) under {}".format(folder))
	if probe is None:
		if len(found) > 1:
			raise el.EphysError("Bouts of {} probes under {} ({}): give --probe".format(len(found), folder,
				", ".join(sorted(found))))
		probe = next(iter(found))
	if probe not in found:
		raise el.EphysError("No bouts of {} under {} (there: {})".format(probe, folder, ", ".join(sorted(found))))
	return sorted(found[probe], key=lambda b: (b.g, b.t))


def Join(bouts, out_bin, seconds=None, say=print):
	"""The bouts, one after the other, into out_bin (up to seconds of samples, whole samples; all of them with
	None), its .meta (the first bout's, with the joined file's size and length) and <out>.bouts.csv (each bout's
	file, firstSample, samples, and where it starts in the joined file). Stops at a bout whose probe settings differ
	from the first's. Returns the samples joined."""
	first = bouts[0]
	rate = float(first.meta.get("imSampRate", 30000))
	most = None if seconds is None else int(round(seconds * rate))
	plan, total = [], 0
	for b in bouts:
		differ = [k for k in SAME if b.meta.get(k) != first.meta.get(k)]
		if differ:
			say("{}: its {} differ from the first bout's: joined up to it".format(os.path.basename(b.path),
				", ".join(differ)))
			break
		take = b.samples if most is None else min(b.samples, most - total)
		if take <= 0:
			break
		plan.append((b, take, total))
		total += take
	row = 2 * first.saved
	tmp = out_bin + ".part"
	with open(tmp, "wb") as out:
		for b, take, _ in plan:
			left = take * row
			with open(b.path, "rb") as f:
				while left:
					block = f.read(min(COPY_BLOCK, left))
					if not block:
						raise el.EphysError("{} ended early".format(b.path))
					out.write(block)
					left -= len(block)
	meta = dict(first.meta, fileName=out_bin.replace("\\", "/"), fileSizeBytes=str(total * row),
		fileTimeSecs=repr(total / rate), campyJoinedBouts=str(len(plan)))
	meta.pop("fileSHA1", None)
	with open(os.path.splitext(out_bin)[0] + ".meta", "w") as f:
		for line in open(os.path.splitext(first.path)[0] + ".meta"): # its lines in their order, ~ kept
			key = line.rstrip("\n").partition("=")[0]
			if key.lstrip("~") in meta:
				f.write("{}={}\n".format(key, meta.pop(key.lstrip("~"))))
		for key, value in meta.items():
			f.write("{}={}\n".format(key, value))
	with open(os.path.splitext(out_bin)[0] + ".bouts.csv", "w", newline="") as f:
		w = csv.writer(f)
		w.writerow(["file", "first_sample", "samples", "joined_start"])
		for b, take, at in plan:
			w.writerow([b.path, b.first, take, at])
	os.replace(tmp, out_bin)
	say("Joined {} bouts of {}: {:.1f} s ({:.2f} GB) in {}".format(len(plan), first.probe, total / rate,
		total * row / 1e9, out_bin))
	return total


def Probe(meta):
	"""Kilosort 4's probe from a .meta: its AP channels, their positions (x across the shanks, z) and shanks."""
	n = int(meta.get("snsApLfSy", "{},0,1".format(int(meta["nSavedChans"]) - 1)).split(",")[0])
	g = el.Geometry.FromMeta(meta, n)
	return {"chanMap": np.arange(n), "xc": g.x.astype(np.float32), "yc": g.z.astype(np.float32),
		"kcoords": g.shank.astype(np.float32), "n_chan": n}


def Sort(bin_path, out):
	"""Kilosort 4 on bin_path, its output (phy's files) in out: run in Kilosort's environment (Kilosort's defaults;
	the probe from the .meta)."""
	from kilosort import run_kilosort
	meta = el.ReadMeta(os.path.splitext(bin_path)[0] + ".meta")
	run_kilosort(settings={"n_chan_bin": int(meta["nSavedChans"]), "filename": bin_path,
		"fs": float(meta.get("imSampRate", 30000))}, probe=Probe(meta), results_dir=out)


def Kilosort(bin_path, out, python, say=print):
	"""Sort() in Kilosort's own environment (python: its python), at low priority; its lines said as they come."""
	here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
	env = dict(os.environ, PYTHONPATH=os.pathsep.join([here] + [v for v in [os.environ.get("PYTHONPATH")] if v]))
	kwargs = {"creationflags": 0x00004000} if sys.platform == "win32" else {} # BELOW_NORMAL_PRIORITY_CLASS
	cmd = [python, "-m", "campy.ephystemplates", bin_path, out]
	say("Kilosort 4: {}".format(" ".join(cmd)))
	t0 = time.monotonic()
	proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kwargs)
	for line in proc.stdout:
		say("  " + line.rstrip())
	if proc.wait():
		raise el.EphysError("Kilosort 4 ended with {} ({})".format(proc.returncode, python))
	say("Kilosort 4: done in {:.0f} s".format(time.monotonic() - t0))


def Templates(session, out, python, probe=None, minutes=MINUTES, span=None, labels=None, say=print):
	"""The whole of it: Bouts, Join, Kilosort, then campy-ephys train. Writes in out: <probe>.joined.ap.bin (+
	.meta, .bouts.csv), kilosort4/, templates.npz, templates.json (what it did). Returns templates.npz's path."""
	from campy import ephyssort
	os.makedirs(out, exist_ok=True)
	t0 = time.monotonic()
	bouts = Bouts(session, probe, say)
	joined = os.path.join(out, "{}.joined.ap.bin".format(bouts[0].probe))
	ks = os.path.join(out, "kilosort4")
	npz = os.path.join(out, "templates.npz")
	if os.path.isfile(joined) and os.path.isfile(os.path.splitext(joined)[0] + ".meta"):
		say("Already joined: {}".format(joined))
	else:
		Join(bouts, joined, None if minutes is None else minutes * 60.0, say)
	if os.path.isfile(os.path.join(ks, "spike_times.npy")):
		say("Already sorted: {}".format(ks))
	else:
		if os.path.isdir(ks): # a sort that stopped: from the start again
			shutil.rmtree(ks)
		Kilosort(joined, ks, python, say)
	source = el.ReplaySource(joined, speed=0)
	length = len(source.data) / source.stream.rate
	if span is None:
		span = "{:g}:".format(max(0.0, length - TRAIN_S))
	summary = ephyssort.Train(source.data, source.stream, ks, npz, span, labels or ephyssort.LABELS, say=say)
	record = {"session": os.path.abspath(session), "probe": bouts[0].probe, "bouts": len(bouts),
		"joined": joined, "joined_s": round(length, 3), "kilosort": ks, "templates": npz, "span": span,
		"minutes": round((time.monotonic() - t0) / 60.0, 1), "train": summary}
	from campy import strictjson
	with open(os.path.join(out, "templates.json"), "w") as f:
		strictjson.dump(record, f, indent=1)
	say("Templates: {}\nFor the panel's config:\n  spikeglx:\n    templates: {}\n    match: learned".format(npz,
		os.path.abspath(npz).replace("\\", "/")))
	return npz


if __name__ == "__main__": # Kilosort's environment: python -m campy.ephystemplates <bin> <out>
	Sort(sys.argv[1], sys.argv[2])
