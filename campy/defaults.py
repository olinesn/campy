"""
Defaults that changed, and which version changed them, so that a config
written for an older campy is told when it relies on one.

A config that leaves a key out gets campy's default for it. When that
default changes, the same config makes different recordings, without a
word: in 2.1.0 FLIR cameras went from no gamma to gamma 0.6, and the same
config's videos came out brighter in the dark and middle grays. So at
startup campy-acquire and campy-live say, once, each changed default that
this run relies on (Warnings):

  cameraGamma not set: since 2.1.0 the default is 0.6 (was Off). ...

Each is said only where it applies (cameraGamma with a FLIR camera, campy-
live's --fast-upsample with ROCm), and only for a key the config and the
command line leave out. They go into the recording's session.json too
(campy/manifest.py). To stop them:

- set the key, in the config or on the command line: then it is chosen,
  not a default;
- defaultsVersion: "2.1.0" in the config (campy-live: --defaults-version
  2.1.0): the config was written knowing 2.1.0's defaults, so only defaults
  changed after 2.1.0 are said;
- defaultWarnings: False (campy-live: --default-warnings off): none.

A change to a default that changes what a recording holds or how it is
made goes into CHANGES, in the same pull request. An entry for a release not
made yet gives the version it is planned for (NEXT); the release's version
pull request checks it.

This module needs only Python's standard library: campy-live, in an
environment of its own, uses it too.
"""

import re

# The version the next release is planned as, for changes made since 2.2.0
NEXT = "2.2.1"


class Change:
	"""
	A default that changed: key (a config key, or a campy-live option as
	"--option"), the version that changed it (since), what it was and is
	now, as the config would say them, what that changes (effect), and
	whether it applies to this run: applies(context), context being what
	Warnings is given.
	"""

	def __init__(self, key, since, was, now, effect, applies=lambda context: True, program="campy-acquire",
			pr=None):
		self.key, self.since, self.was, self.now, self.effect = key, since, was, now, effect
		self.applies, self.program, self.pr = applies, program, pr

	def Text(self):
		return "{} not set: since {} the default is {} (was {}). {}".format(self.key, self.since, self.now,
			self.was, self.effect)

	def Record(self):
		# For session.json: what changed, and when
		record = {"in": self.since, "was": self.was, "now": self.now}
		if self.pr:
			record["pullRequest"] = self.pr
		return record


def AnyCamera(test):
	# applies(context) for campy-acquire: any camera's params pass test
	return lambda context: any(test(cam) for cam in context.get("cameras", []))


def Make(cam):
	return str(cam.get("cameraMake", "")).lower()


def Video(cam):
	return str(cam.get("videoFilename", "")).lower().endswith((".mp4", ".mov"))


def Gpu(context):
	return bool(context.get("gpu"))


CHANGES = [
	# campy-acquire: config keys
	Change("cameraGamma", "2.1.0", "Off", 0.6,
		"FLIR frames are brighter in the dark and middle grays than with the camera's gamma off "
		"(brightness in proportion to the light). cameraGamma: \"Off\" keeps the old frames.",
		AnyCamera(lambda cam: Make(cam) == "flir"), pr=80),
	Change("cameraLinkLimitMBps", "2.1.0", "None", "auto",
		"\"None\" left the camera's own USB throughput limit; now campy raises it to the camera's maximum "
		"when it is too low for frameRate.",
		AnyCamera(lambda cam: Make(cam) == "flir"), pr=24),
	Change("writeQueueMaxSec", "2.1.0", "None", 5,
		"\"None\" queued every frame for the encoder, without limit; now, when it falls 5 s behind, new "
		"frames are dropped and counted (metadata.csv's framesDropped) instead of filling memory.", pr=2),
	Change("fragmentedVideo", "2.1.0", False, True,
		"Videos are fragmented MP4s, readable up to their last second after a crash. False writes a plain "
		"MP4.", AnyCamera(Video), pr=51),
	Change("sessionFolder", "2.1.0", False, True,
		"Each run records into a new <videoFolder>/<date_time>/ folder, not into videoFolder itself.", pr=63),

	# campy-live: options
	Change("--fixed-crops", "2.1.0", "off", "auto",
		"With a GPU, every frame's --max-instances crops run at once, as one CUDA graph with --cuda-graphs.",
		Gpu, program="campy-live", pr=134),
	Change("--cuda-graphs", "2.1.0", "off", "auto",
		"With a GPU, each model's work is recorded as a CUDA graph and replayed.", Gpu, program="campy-live",
		pr=134),
	Change("--fast-upsample", "2.1.0", "off", "auto",
		"On ROCm, each upsampling layer runs in the memory layout it measured faster in.",
		lambda context: bool(context.get("rocm")), program="campy-live", pr=138),
	Change("--engine", "2.2.0", "eager", "auto",
		"With --fixed-crops, the networks run as compiled engines: TensorRT on NVIDIA where it is installed "
		"(fp16, keypoints a little less accurate), else torch.compile.", Gpu, program="campy-live", pr=147),
	Change("--cage", NEXT, "none", "auto",
		"A cage drawn in campy-live-view (<calibration>.cage.json, beside the calibration) is used: an animal whose 3D "
		"lands outside it is not found (no 3D, no detections grouped, outside_cage 1). --cage off keeps every 3D "
		"position.", lambda context: bool(context.get("cage")), program="campy-live", pr=None),
	]


def VersionKey(text):
	"""
	A version as a tuple that sorts in release order: 2.1.0rc7 before
	2.1.0, before 2.1.1. None for text that is not one.
	"""
	m = re.fullmatch(r"\s*v?(\d+(?:\.\d+)*)(?:\s*(?:rc|c)\s*(\d+))?\s*", str(text))
	if not m:
		return None
	numbers = tuple(int(x) for x in m.group(1).split("."))
	numbers += (0,) * (3 - len(numbers))
	return numbers + ((0, int(m.group(2))) if m.group(2) else (1, 0))


def Since(since, known):
	# Whether a change made in version since is newer than known
	if known is None or str(known) == "None":
		return True
	k = VersionKey(known)
	if k is None:
		raise ValueError('defaultsVersion is a campy version, e.g. "2.1.0", not {!r}.'.format(known))
	return VersionKey(since) > k


def Relied(values, sources, context=None, program="campy-acquire", known=None):
	"""
	The changes this run relies on: the program's, that apply (context), and
	whose key is not set (sources[key] is "default", or the key is not
	there), changed after known (defaultsVersion; None: any).
	"""
	context = context or {}
	out = []
	for change in CHANGES:
		if change.program != program or not Since(change.since, known):
			continue
		if sources.get(change.key, "default") != "default":
			continue
		if not change.applies(context):
			continue
		out.append(change)
	return out


def Warnings(values, sources, context=None, program="campy-acquire", known=None, enabled=True):
	# Each relied-on change as a line; none when silenced (defaultWarnings False)
	if not enabled or str(enabled).lower() in ("false", "off", "0", "no"):
		return []
	return [change.Text() for change in Relied(values, sources, context, program, known)]


def Message(lines, program="campy-acquire"):
	# What is said at startup: the lines, and how to stop them
	if not lines:
		return ""
	how = 'set each in the config, or defaultsVersion: "<version>" for the defaults it was written for; ' \
		"defaultWarnings: False silences these" if program == "campy-acquire" else \
		"pass each option, or --defaults-version <version>; --default-warnings off silences these"
	return "This run relies on {} that changed ({}):\n{}".format(
		"a default" if len(lines) == 1 else "{} defaults".format(len(lines)), how,
		"\n".join("  " + line for line in lines))


def Annotate(key, program="campy-acquire"):
	# session.json's note on a key whose default changed: {"in", "was", "now"}, or None
	for change in CHANGES:
		if change.key == key and change.program == program:
			return change.Record()
	return None
