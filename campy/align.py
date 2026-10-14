"""
campy-align: put every camera of a recording on one timeline.

	campy-align ./test        # the recording's videoFolder

The timeline is the trigger. A triggered camera (cameraTrigger set) makes one
frame per trigger pulse. A RatCam (cameraMake "ratcam") runs on its own
clock, but with the trigger line wired to its SYNC input it timestamps every
pulse, so each of its frames lands between two pulses. Trigger k is at
t_s = (k - 1) x the trigger period.

What it works out, for every camera:
- the trigger behind each frame, from the camera's own timestamps. A step of
  n periods between two frames means n - 1 triggers left no frame: campy
  dropped some itself (a gap in frametimes.npy's frame numbers, when the
  encoder fell behind) and the camera missed the rest;
- for a RatCam, each frame's fractional trigger, between the pulses around it;
- where the camera started. If the trigger was already running when a camera
  started (startArduino starts it before the cameras do), that camera's first
  frame is not the first trigger. campy saves when each frame and each RatCam
  pulse reached the computer, all on one clock (hosttimes.npy,
  ratcam_sync.csv), and comparing those says how many triggers later each
  camera began. The earliest camera's first trigger is trigger 1.

Writes aligned.csv in each camera folder, alignment.json in the recording
folder, and prints a summary.
"""

import argparse, ast, csv, json, os, sys
import numpy as np


IRREGULAR = 0.25   # a step this far from a whole number of periods is not a clean trigger step
NOT_PERIODIC = 0.2 # more irregular steps than this share: not a periodic trigger
UNSURE = 0.3       # a start offset this far from a whole number of triggers could be off by one
FRAME_LAG = 0.2    # see Starts: frames reach the computer after their trigger's pulse does
WINDOW = 50        # arrivals per window when fitting a clock to the computer's (ClockMap)


class AlignError(Exception):
	pass


# ------------------------------------------------------------------ reading

def ReadMetadata(path):
	meta = {}
	with open(path, newline="") as f:
		for row in csv.reader(f):
			if len(row) == 2:
				try:
					meta[row[0]] = ast.literal_eval(row[1])
				except (ValueError, SyntaxError):
					meta[row[0]] = row[1]
	return meta


def ReadColumns(path):
	# A CSV with a header row, as {column: int64 array}
	with open(path, newline="") as f:
		rows = list(csv.reader(f))
	if not rows:
		return {}
	header, data = rows[0], rows[1:]
	return {name: np.array([int(r[i]) for r in data], dtype=np.int64) for i, name in enumerate(header)}


def LoadCamera(folder):
	meta = ReadMetadata(os.path.join(folder, "metadata.csv"))
	cam = {
		"folder": folder,
		"name": str(meta.get("cameraName", os.path.basename(folder))),
		"make": str(meta.get("cameraMake", "")),
		"meta": meta,
		"warnings": [],
		}
	frametimes = np.load(os.path.join(folder, "frametimes.npy"))
	cam["frame"] = frametimes[0].astype(np.int64)
	cam["t"] = frametimes[1].astype(float) # seconds on the camera's clock, from its first frame
	path = os.path.join(folder, "hosttimes.npy")
	cam["host_ns"] = np.load(path).astype(np.int64) if os.path.exists(path) else None
	if cam["host_ns"] is not None and len(cam["host_ns"]) != len(cam["frame"]):
		cam["host_ns"] = None
	cam["ratcam"] = cam["make"] == "ratcam"
	cam["triggered"] = not cam["ratcam"] and str(meta.get("cameraTrigger", "None")) != "None"

	if cam["ratcam"]:
		frames = ReadColumns(os.path.join(folder, "ratcam_frames.csv"))
		sync = ReadColumns(os.path.join(folder, "ratcam_sync.csv"))
		# The frames campy wrote, on the camera's own clock (frametimes is zeroed)
		t_us = dict(zip(frames.get("frame", []).tolist(), frames.get("t_us", []).tolist()))
		cam["frame_t"] = np.array([t_us[n] for n in cam["frame"].tolist()], dtype=float) / 1e6
		cam["pulse"] = sync.get("pulse", np.zeros(0, np.int64))
		cam["pulse_t"] = sync.get("t_us", np.zeros(0, np.int64)) / 1e6
		cam["pulse_host_ns"] = sync.get("host_ns", np.zeros(0, np.int64))
	return cam


def LoadRecording(folder):
	cams = []
	for name in sorted(os.listdir(folder)):
		path = os.path.join(folder, name)
		if os.path.isfile(os.path.join(path, "metadata.csv")) and os.path.isfile(os.path.join(path, "frametimes.npy")):
			cams.append(LoadCamera(path))
	if not cams:
		raise AlignError("no camera folders (with metadata.csv and frametimes.npy) in {}".format(folder))
	return cams


# ------------------------------------------------------------------ triggers

def OwnPeriod(t, nominal):
	# The trigger period on one camera's clock: the median single step
	dt = np.diff(t)
	single = dt[np.abs(dt / nominal - 1) < IRREGULAR]
	return float(np.median(single)) if len(single) else nominal


def Steps(t, period):
	"""
	Trigger number of each timestamp, 0 for the first, and how many steps
	between timestamps were not a whole number of periods.
	"""
	ratio = np.diff(t) / period
	steps = np.rint(ratio).astype(np.int64)
	irregular = int(np.sum((np.abs(ratio - steps) > IRREGULAR) | (steps < 1)))
	return np.concatenate([[0], np.cumsum(steps)]), steps, irregular


def NominalPeriod(cams):
	rates = sorted(set(float(c["meta"]["frameRate"]) for c in cams if c["triggered"]))
	if len(rates) > 1:
		raise AlignError("the triggered cameras have different frame rates ({}): one trigger cannot "
			"have made them".format(", ".join("{:g}".format(r) for r in rates)))
	if rates:
		return 1.0 / rates[0]
	# No triggered camera: the pulses the RatCams recorded are the trigger
	for c in cams:
		if c["ratcam"] and len(c["pulse_t"]) > 2:
			return float(np.median(np.diff(c["pulse_t"])))
	raise AlignError("nothing to align to: no triggered camera, and no RatCam recorded SYNC pulses")


def CountTriggers(cam, nominal):
	# Each frame's trigger, from 0, by the camera's own clock
	if len(cam["t"]) < 3:
		cam["warnings"].append("only {} frames".format(len(cam["t"])))
		return False
	cam["period"] = OwnPeriod(cam["t"], nominal)
	cam["k"], steps, cam["irregular"] = Steps(cam["t"], cam["period"])
	cam["without_frame"] = int(np.sum(steps[steps > 1] - 1))
	cam["dropped"] = int(np.sum(np.diff(cam["frame"]) - 1))
	cam["missed"] = cam["without_frame"] - cam["dropped"]
	if cam["irregular"]:
		cam["warnings"].append("{} steps between frames are not a whole number of trigger periods"\
			.format(cam["irregular"]))
	return True


def CountPulses(cam, nominal):
	# Each SYNC pulse's trigger, from 0, by the RatCam's clock
	p = cam["pulse_t"]
	if len(p) < 3:
		cam["warnings"].append("only {} SYNC pulses: is the trigger line wired to its SYNC input?".format(len(p)))
		return False
	cam["period"] = OwnPeriod(p, nominal)
	cam["q"], steps, irregular = Steps(p, cam["period"])
	if irregular > NOT_PERIODIC * len(steps):
		cam["warnings"].append("its SYNC pulses are not periodic ({} of {} steps irregular): a random sync "
			"train? campy-align needs the trigger line".format(irregular, len(steps)))
		return False
	cam["pulse_gaps"] = int(np.sum(steps[steps > 1] - 1))
	counted = np.diff(cam["pulse"])
	if np.any(counted != steps):
		cam["warnings"].append("its pulse counter disagrees with the pulse times at {} steps"\
			.format(int(np.sum(counted != steps))))
	return True


# ------------------------------------------------------------------ starts

def ClockMap(dev_s, host_ns):
	"""
	The computer's clock (ns) for a time on a camera's clock (s). Arrivals are
	only ever delayed (USB, queues, a busy process), so the fit goes through
	the earliest arrival in each window of WINDOW: the delay that is always
	there, without the ones that come and go. It is a line, for clock drift.
	"""
	d = np.asarray(dev_s, float)
	lag = np.asarray(host_ns, float) - d * 1e9
	low = [i + int(np.argmin(lag[i:i + WINDOW])) for i in range(0, len(d), WINDOW)]
	if len(low) >= 2 and np.ptp(d[low]) > 0:
		slope, offset = np.polyfit(d[low], lag[low], 1)
	else:
		slope, offset = 0.0, float(np.min(lag))
	return lambda x: np.asarray(x, float) * (1e9 + slope) + offset


def Starts(cams, period):
	"""
	Each camera's first trigger, as an offset from the earliest camera's. From
	when the camera's first frame (or a RatCam's first pulse) reached the
	computer.

	A RatCam's pulse arrives a moment after its trigger; a triggered camera's
	frame arrives after its exposure, readout and transfer, a good part of a
	period later. So against a RatCam, a camera's start is rounded down after
	allowing FRAME_LAG periods early; between cameras of one kind, to nearest.
	"""
	for c in cams:
		c["start"], c["start_how"], c["unsure"] = 0, "assumed", False
		if c["ratcam"] and "q" in c and len(c["pulse_host_ns"]) == len(c["pulse_t"]):
			c["anchor_ns"] = float(ClockMap(c["pulse_t"], c["pulse_host_ns"])(c["pulse_t"][0]))
		elif not c["ratcam"] and c.get("host_ns") is not None and len(c["t"]):
			c["anchor_ns"] = float(ClockMap(c["t"], c["host_ns"])(c["t"][0]))

	anchored = [c for c in cams if "anchor_ns" in c]
	if not anchored:
		return None
	pulses = [c for c in anchored if c["ratcam"]]
	ref = min(pulses or anchored, key=lambda c: c["anchor_ns"])
	for c in anchored:
		delta = (c["anchor_ns"] - ref["anchor_ns"]) / (period * 1e9)
		if ref["ratcam"] and not c["ratcam"]:
			start = int(np.floor(delta + FRAME_LAG))
			c["lag_ms"] = round((delta - start) * period * 1e3, 2)
			c["unsure"] = delta - start > 1 - UNSURE
		else:
			start = int(np.rint(delta))
			c["unsure"] = abs(delta - start) > UNSURE
		c["start"], c["start_how"] = start, "arrival times"
	# Trigger 1 is the earliest camera's first trigger
	first = min(c["start"] for c in anchored)
	for c in anchored:
		c["start"] -= first
	return ref


# ------------------------------------------------------------------ align

def Align(cams):
	nominal = NominalPeriod(cams)
	for c in cams:
		if c["ratcam"]:
			c["usable"] = CountPulses(c, nominal)
		else:
			c["usable"] = c["triggered"] and CountTriggers(c, nominal)
			if not c["triggered"]:
				c["warnings"].append("not triggered, and not a RatCam: nothing ties it to the trigger")

	periods = [c["period"] for c in cams if c["usable"] and "period" in c]
	period = float(np.median(periods)) if periods else nominal
	ref = Starts([c for c in cams if c["usable"]], period)

	for c in cams:
		if not c["usable"]:
			continue
		if c["ratcam"]:
			pulse_trigger = c["q"] + c["start"] + 1
			f, p = c["frame_t"], c["pulse_t"]
			trigger = np.interp(f, p, pulse_trigger)
			before, after = f < p[0], f > p[-1]
			trigger[before] = pulse_trigger[0] - (p[0] - f[before]) / c["period"]
			trigger[after] = pulse_trigger[-1] + (f[after] - p[-1]) / c["period"]
			c["trigger"] = trigger
			c["between_pulses"] = ~(before | after)
		else:
			c["trigger"] = c["k"] + c["start"] + 1
		c["t_s"] = (c["trigger"] - 1) * period
	return period, ref


# ------------------------------------------------------------------ output

def CameraSummary(c):
	s = {"make": c["make"], "frames": int(len(c["frame"])), "warnings": c["warnings"]}
	if not c["usable"]:
		s["aligned"] = False
		return s
	s.update({
		"aligned": True,
		"first_trigger": round(float(c["trigger"][0]), 3) if len(c["trigger"]) else None,
		"last_trigger": round(float(c["trigger"][-1]), 3) if len(c["trigger"]) else None,
		"start_trigger": int(c["start"]) + 1,
		"start_from": c["start_how"],
		"start_unsure": bool(c["unsure"]),
		"period_ms_own_clock": round(c["period"] * 1e3, 5),
		})
	if c["ratcam"]:
		s.update({
			"pulses": int(len(c["pulse_t"])),
			"triggers_without_pulse": c["pulse_gaps"],
			"frames_outside_pulses": int(np.sum(~c["between_pulses"])),
			})
	else:
		s.update({
			"triggers_without_frame": c["without_frame"],
			"dropped_by_campy": c["dropped"],
			"missed_by_camera": c["missed"],
			})
		if "lag_ms" in c:
			s["frame_lag_after_pulse_ms"] = c["lag_ms"]
	return s


def WriteAligned(c):
	path = os.path.join(c["folder"], "aligned.csv")
	with open(path, "w", newline="") as f:
		w = csv.writer(f)
		if c["ratcam"]:
			w.writerow(["frame", "trigger", "t_s", "between_pulses"])
			for row in zip(c["frame"], c["trigger"], c["t_s"], c["between_pulses"]):
				w.writerow([int(row[0]), "{:.4f}".format(row[1]), "{:.6f}".format(row[2]), int(row[3])])
		else:
			w.writerow(["frame", "trigger", "t_s"])
			for row in zip(c["frame"], c["trigger"], c["t_s"]):
				w.writerow([int(row[0]), int(row[1]), "{:.6f}".format(row[2])])
	return path


def Describe(c):
	if not c["usable"]:
		return "not aligned"
	if c["ratcam"]:
		text = "{} frames between triggers {:.1f} and {:.1f}; {} pulses".format(
			len(c["frame"]), c["trigger"][0], c["trigger"][-1], len(c["pulse_t"]))
	else:
		text = "{} frames, triggers {}-{}".format(len(c["frame"]), c["trigger"][0], c["trigger"][-1])
		if c["without_frame"]:
			text += ": {} triggers without a frame ({} dropped by campy, {} missed by the camera)".format(
				c["without_frame"], c["dropped"], c["missed"])
	if c["start_how"] == "assumed":
		text += "; started at trigger {} (assumed: no arrival times)".format(c["start"] + 1)
	else:
		text += "; started at trigger {}{}".format(c["start"] + 1, " (could be off by one)" if c["unsure"] else "")
	return text


def Run(folder, log=print):
	cams = LoadRecording(folder)
	period, ref = Align(cams)
	summary = {
		"trigger_period_s": period,
		"trigger_rate_hz": 1.0 / period,
		"starts_from": ref["name"] if ref else None,
		"cameras": {c["name"]: CameraSummary(c) for c in cams},
		}
	for c in cams:
		if c["usable"]:
			WriteAligned(c)
	with open(os.path.join(folder, "alignment.json"), "w") as f:
		json.dump(summary, f, indent=2)

	if any(c["usable"] for c in cams):
		log("Trigger period {:.4f} ms ({:.4f} Hz). Trigger 1 is the first trigger any camera recorded{}."\
			.format(period * 1e3, 1.0 / period, ", by arrival times" if ref else " (assumed: no arrival times)"))
	else:
		log("No camera could be put on a trigger timeline:")
	width = max(len(c["name"]) for c in cams)
	for c in cams:
		log("  {:<{w}}  {:<6}  {}".format(c["name"], c["make"], Describe(c), w=width))
		for warning in c["warnings"]:
			log("  {:<{w}}  WARN    {}".format("", warning, w=width))
	log("Wrote {}{}.".format("aligned.csv in each aligned camera's folder, and "
		if any(c["usable"] for c in cams) else "", os.path.join(folder, "alignment.json")))
	return summary


def Main(argv=None):
	parser = argparse.ArgumentParser(prog="campy-align",
		description="Put every camera of a campy recording on the trigger's timeline.")
	parser.add_argument("folder", help="the recording's videoFolder")
	args = parser.parse_args(argv)
	try:
		summary = Run(args.folder)
	except AlignError as e:
		print("campy-align: {}".format(e), file=sys.stderr)
		return 1
	return 0 if any(c["aligned"] for c in summary["cameras"].values()) else 1


if __name__ == "__main__":
	sys.exit(Main())
