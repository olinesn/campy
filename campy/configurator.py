"""
campy-acquire's settings: the config file, the command line and their checks.
Every setting is described once, in configschema.py: its default, the values
it takes and its help.
"""

import os, sys, copy, yaml, time, logging
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter, ArgumentTypeError
from campy import configschema
from campy.cameras import unicam
from campy.utils.utils import Say


def DefaultParams():
	"""
	Default parameters for campy config.
	Omitted parameters will revert to these default values.
	"""
	return configschema.Defaults()


def FillCameraLists(params):
	# Name and select cameras the config does not list. Runs after command-line
	# overrides, so --numCams changes how many cameras get a name.
	if "cameraNames" not in params.keys():
		params["cameraNames"] = ["Camera%s" % n for n in range(params["numCams"])]
	if "cameraSelection" not in params.keys():
		params["cameraSelection"] = [n for n in range(params["numCams"])]
	return params


class ConfigError(ValueError):
	"""Everything wrong with a config, found before any camera opens: one line each."""
	def __init__(self, problems):
		self.problems = list(problems)
		if len(self.problems) == 1:
			text = self.problems[0]
		else:
			text = "The config has {} problems:\n".format(len(self.problems)) + \
				"\n".join("  " + p for p in self.problems)
		super().__init__(text)


def ConfigureParams(resolved=False):
	"""
	campy-acquire's settings, from its command line: the config (and the files
	it extends), then the options. resolved=True also returns the Resolved, for
	what each value is and where it came from (a recording's manifest).
	"""
	parser = ArgumentParser(description="Campy CLI",
						formatter_class=ArgumentDefaultsHelpFormatter,)
	clargs = ParseClargs(parser)
	result = ResolveClargs(clargs)
	for note in result.notes:
		Say(note)
	params = result.params

	# Optionally, user can manually set path to find ffmpeg binary.
	if params["ffmpegPath"] != "None":
		os.environ["IMAGEIO_FFMPEG_EXE"] = params["ffmpegPath"]

	return (params, result) if resolved else params


def Resolve(config, argv=()):
	"""
	The settings campy-acquire config argv... would record with, and where each
	came from, without opening anything: a Resolved, or a ConfigError listing
	every problem. argv is campy-acquire's options, e.g. ["--frameRate", "60"].
	"""
	clargs = ParseClargs(ArgumentParser(prog="campy-acquire"), [config] + list(argv))
	return ResolveClargs(clargs)


def ResolveParams(clargs):
	# Resolve's params, for parsed options, with its notes said
	result = ResolveClargs(clargs)
	for note in result.notes:
		Say(note)
	return result.params


def ProfileFolder():
	# Where this computer's rig profiles are: extends: <name> is <folder>/<name>.yaml
	return os.path.expanduser(os.environ.get("CAMPY_CONFIG_DIR") or os.path.join("~", ".campy", "configs"))


def Shown(path):
	# A path as campy's messages show it, with ~ for the home folder
	home = os.path.expanduser("~")
	if os.path.isabs(path) and path.startswith(home + os.sep):
		return "~" + path[len(home):]
	return path


class Source:
	"""Where one setting's value came from: campy's default, a line of a config file, or the command line."""
	DEFAULT, FILE, COMMAND_LINE = "default", "file", "command line"

	def __init__(self, kind, path=None, line=None, note=None):
		self.kind, self.path, self.line, self.note = kind, path, line, note

	def Where(self):
		if self.kind != Source.FILE:
			return self.kind
		return "{}:{}".format(Shown(self.path), self.line) if self.line else Shown(self.path)

	def __str__(self):
		return self.Where() + (" ({})".format(self.note) if self.note else "")

	def Structured(self):
		# For programs: {"kind", "file" (absolute, or None), "line", "note"}
		return {"kind": self.kind, "file": os.path.abspath(self.path) if self.path else None,
			"line": self.line, "note": self.note}


class Resolved:
	"""
	A config as campy-acquire records with it:
	  params: every setting (what ConfigureParams returns)
	  sources: {key: Source}, where each value came from
	  chain: the config files, the one given first, then each file it extends
	  notes: what campy says about it as it starts (renamed keys, shortened lists)
	  sections: the sections other programs read ({"live": {...}, ...}), and
	  section_sources: {"live": {key: Source}}
	"""
	def __init__(self, params, sources, chain, notes, sections=None, section_sources=None):
		self.params, self.sources, self.chain, self.notes = params, sources, chain, notes
		self.sections, self.section_sources = sections or {}, section_sources or {}

	def Effective(self):
		# {key: {"value": ..., "source": {"kind", "file", "line", "note"}}}, ready for JSON. A
		# section's keys are "<section>.<key>" (live.models).
		effective = {key: {"value": copy.deepcopy(value), "source": self.sources[key].Structured()}
			for key, value in self.params.items()}
		for name, values in self.sections.items():
			for key, value in values.items():
				effective["{}.{}".format(name, key)] = {"value": copy.deepcopy(value),
					"source": self.section_sources[name][key].Structured()}
		return effective

	def Cameras(self):
		# Each camera's settings, as its process gets them
		return [CameraParams(self.params, n) for n in range(self.params["numCams"])]

	def ToYaml(self):
		# One config with every setting and no extends:, each line saying where
		# its value came from. campy-acquire records with it as with the original.
		made = " -> ".join(Shown(path) for path in self.chain)
		if any(source.kind == Source.COMMAND_LINE for source in self.sources.values()):
			made += ", and the command line"
		lines = ["# Every campy setting, as campy-acquire records with it.",
			"# From: {}. Each comment says where the value came from.".format(made), ""]
		for key, value in self.params.items():
			# The value as YAML on one line: a one-item flow list, without its brackets
			text = yaml.safe_dump([value], default_flow_style=True, width=float("inf")).strip()[1:-1]
			lines.append("{}: {}  # {}".format(key, text, self.sources[key]))
		for name, values in self.sections.items():
			lines.append("{}:".format(name))
			for key, value in values.items():
				text = yaml.safe_dump([value], default_flow_style=True, width=float("inf")).strip()[1:-1]
				lines.append("  {}: {}  # {}".format(key, text, self.section_sources[name][key]))
		return "\n".join(lines) + "\n"


def ParentPath(name, child):
	# The file extends: name means, in the config child. A bare name is a rig
	# profile; anything with a folder or an extension is a path from child's folder.
	if os.path.basename(name) == name and "\\" not in name and not os.path.splitext(name)[1] \
			and not name.startswith("~"):
		return os.path.join(ProfileFolder(), name + ".yaml")
	path = os.path.expanduser(name)
	return path if os.path.isabs(path) else os.path.join(os.path.dirname(os.path.abspath(child)), path)


MAX_EXTENDS = 20


def ReadChain(config_path):
	"""
	The config and the files it extends, the one given first: [(path, values,
	lines)], without their extends:. A missing file, an extends: that loops
	and an extends: that is not one name are refused.
	"""
	chain, seen, path = [], [], config_path
	while True:
		real = os.path.realpath(path)
		if real in seen:
			raise ConfigError(["extends: loops: {}".format(" -> ".join(
				[Shown(p) for p, _, _ in chain] + [Shown(path)]))])
		seen.append(real)
		values, lines = ReadConfig(path)
		parent = values.pop("extends", None)
		line = lines.pop("extends", None)
		chain.append((path, values, lines))
		if parent is None:
			return chain
		where = "{}:{}".format(Shown(path), line)
		if not isinstance(parent, str) or not parent.strip():
			raise ConfigError(["{}: extends: names one config file or rig profile, not {!r}".format(where, parent)])
		next_path = ParentPath(parent.strip(), path)
		if not os.path.isfile(next_path):
			hint = " (the rig profile folder; CAMPY_CONFIG_DIR sets another, and campy-config where lists " \
				"its profiles)" if os.path.dirname(next_path) == ProfileFolder() else ""
			raise ConfigError(["{}: extends: {}, but there is no {}{}".format(where, parent, Shown(next_path), hint)])
		if len(chain) >= MAX_EXTENDS:
			raise ConfigError(["{}: more than {} files extend each other".format(where, MAX_EXTENDS)])
		path = next_path


def MergeChain(chain, notes, problems, sections=None, section_sources=None):
	# The chain's values, the root profile first and each file over the one it
	# extends, key by key (a list is replaced whole), with the Source of each.
	# Each file's values are checked, and its renamed keys read as the new ones.
	# Sections (live:, post: ...) go into sections instead, combined one level
	# deep: a file's live: models replaces the profile's, its other keys stay.
	params, sources = {}, {}
	sections = {} if sections is None else sections
	section_sources = {} if section_sources is None else section_sources
	for path, values, lines in reversed(chain):
		def where(key, path=path, lines=lines):
			return "{}:{}".format(Shown(path), lines[key]) if key in lines else Shown(path)
		values, more = CheckValues(values, where)
		problems += more
		for name in [k for k in values if configschema.BY_NAME[k].kind is configschema.SECTION]:
			for key, value in values.pop(name).items():
				sections.setdefault(name, {})[key] = value
				section_sources.setdefault(name, {})[key] = Source(Source.FILE, path, lines.get(name))
		layer = {key: Source(Source.FILE, path, lines.get(key)) for key in values}
		values, renamed = Rename(values, Shown(path), notes, problems)
		for old, (new, value) in renamed.items():
			layer.pop(old, None)
			layer[new] = Source(Source.FILE, path, lines.get(old), "written as {}: {}".format(old, value))
		params.update(values)
		sources.update(layer)
	return params, sources


def Rename(values, where, notes, problems):
	# values with each renamed key (RENAMED) read as the key that replaced it,
	# and {old key: (new key, the old value)}
	renamed = {}
	for old, (new, read) in RENAMED.items():
		if old in values:
			value = values[old]
			try:
				values = read(values, where, notes)
				renamed[old] = (new, value)
			except ValueError as e:
				problems.append(str(e))
				values.pop(old, None)
	return values, renamed


def ResolveClargs(clargs):
	# Resolve, for parsed options
	notes, problems = [], []
	chain = ReadChain(clargs.config)
	sections, section_sources = {}, {}
	params, sources = MergeChain(chain, notes, problems, sections, section_sources)
	# Options are read by argparse (ConfigValue); here they get the same range
	# and word checks as the config's own values
	overrides = {key: value for key, value in clargs.__dict__.items() if value is not None and key != "config"}
	overrides, more = CheckValues(overrides, lambda key: "--" + key)
	problems += more
	overrides, renamed = Rename(overrides, "The command line", notes, problems)
	if problems:
		raise ConfigError(problems)
	for key in overrides:
		sources[key] = Source(Source.COMMAND_LINE)
	for old, (new, value) in renamed.items():
		sources[new] = Source(Source.COMMAND_LINE, note="written as {}: {}".format(old, value))
	params.update(overrides)
	for key, value in DefaultParams().items():
		if key not in params:
			params[key] = value
			sources[key] = Source(Source.DEFAULT)
	for key in ("cameraNames", "cameraSelection"):
		if key not in params:
			sources[key] = Source(Source.DEFAULT, note="one per camera, from numCams")
	params = FillCameraLists(params)

	for check in (lambda p: ValidateCameraLists(p, sources, notes), ValidateColorRange, ValidateGamma):
		try:
			check(params)
		except ValueError as e:
			problems.append(str(e))
	# These look at each camera's values, which needs lists that fit numCams
	checks = [ValidateQuality] + ([TriggerFrameRate] if params["startArduino"] else [])
	for check in checks if not problems else []:
		try:
			check(params) # TriggerFrameRate fails here, with the reason, if the rates disagree
		except ValueError as e:
			problems.append(str(e))
	if problems:
		raise ConfigError(problems)
	return Resolved(params, sources, [os.path.abspath(path) for path, _, _ in chain], notes, sections, section_sources)


def ConfigureCamParams(systems, params, n_cam):
	# Insert camera-specific metadata from parameters into cam_params dictionary
	cam_params = params
	cam_params["n_cam"] = n_cam
	cam_params["baseFolder"] = os.getcwd()
	cam_params["cameraName"] = params["cameraNames"][n_cam]

	cam_params = OptParams(cam_params)
	cam_make = cam_params["cameraMake"]
	cam_idx = cam_params["cameraSelection"]

	cam_params["device"] = systems[cam_make]["deviceList"][cam_idx]
	cam_params = unicam.LoadDevice(systems, params, cam_params)

	cam_params["cameraSerialNo"] = systems[cam_make]["serials"][cam_idx]

	return cam_params


# Lists that are not one value per camera
NON_CAMERA_LISTS = ("digitalPins", "ratcamPorts")
# Lists that need a value for every camera: one name or index cannot be shared
EVERY_CAMERA_LISTS = ("cameraNames", "cameraSelection")


def OptParams(cam_params):
	# A single value applies to every camera. A list holds one value per camera,
	# in camera order; a one-item list applies to every camera, and a list longer
	# than numCams uses its first numCams items, so a config written for more
	# cameras also runs with fewer (ValidateCameraLists checks this at startup).
	for key in cam_params:
		if type(cam_params[key]) is list and key not in NON_CAMERA_LISTS:
			values = cam_params[key]
			if len(values) == 1 and key not in EVERY_CAMERA_LISTS:
				cam_params[key] = values[0]
			else:
				cam_params[key] = values[cam_params["n_cam"]]
	return cam_params


def ValidateCameraLists(params, sources=None, notes=None):
	# Per-camera lists must cover every camera, so changing numCams fails here
	# with the key's name rather than in a camera process. With sources, the
	# messages say where a list and numCams were set; notes collects what
	# would otherwise be said.
	num_cams = params["numCams"]
	def From(key):
		if not sources or key not in sources:
			return ""
		return " ({} is from {}, numCams from {})".format(key, sources[key].Where(), sources["numCams"].Where())
	longer = []
	for key, value in params.items():
		if type(value) is not list or key in NON_CAMERA_LISTS:
			continue
		if key in EVERY_CAMERA_LISTS and len(value) < num_cams:
			raise ValueError("{} has {} values for {} cameras. It needs one per camera.{}"\
				.format(key, len(value), num_cams, From(key)))
		if 1 < len(value) < num_cams:
			raise ValueError("{} has {} values for {} cameras. Give one value per camera, "
				"or a single value for all of them.{}".format(key, len(value), num_cams, From(key)))
		if len(value) > num_cams:
			longer.append(key)
	if longer:
		note = "{} list{} more values than numCams: using the first {}.".format(
			", ".join(longer), "s" if len(longer) == 1 else "", num_cams)
		if sources and "numCams" in sources:
			# A list from the file a config extends, and numCams from the config
			# itself: say so, since the rig's list was written for its own cameras
			numcams_file = sources["numCams"].path
			elsewhere = sorted(set(sources[key].Where().split(":")[0] for key in longer
				if key in sources and sources[key].kind == Source.FILE and sources[key].path != numcams_file))
			if elsewhere:
				note += " {} from {}, numCams {} from {}: campy-config show --cameras lists each camera's " \
					"values.".format("The list is" if len(longer) == 1 else "The lists are", ", ".join(elsewhere),
					num_cams, sources["numCams"].Where())
		if notes is None:
			Say(note)
		else:
			notes.append(note)


def ValidateColorRange(params):
	# One value, or one per camera
	values = params["colorRange"] if isinstance(params["colorRange"], list) else [params["colorRange"]]
	for value in values:
		if str(value).lower() not in ("limited", "full"):
			raise ValueError('colorRange is "limited" or "full", not "{}".'.format(value))


# cameraGamma's word for the camera's gamma turned off (GammaEnable False)
GAMMA_OFF = "Off"


def GammaSetting(value):
	# One camera's cameraGamma as campy uses it: a number above 0, or "Off".
	# YAML reads off, Off and no as False, which means "Off" here too.
	if value is False or (isinstance(value, str) and value.strip().lower() == "off"):
		return GAMMA_OFF
	if not isinstance(value, bool):
		try:
			number = float(value)
		except (TypeError, ValueError):
			number = None
		if number is not None and 0 < number < float("inf"):
			return value if isinstance(value, (int, float)) else number
	raise ValueError('cameraGamma is a number above 0 (e.g. 0.6) or "Off", not {!r}.'.format(value))


def ValidateGamma(params):
	# One value, or one per camera
	value = params["cameraGamma"]
	params["cameraGamma"] = [GammaSetting(v) for v in value] if isinstance(value, list) else GammaSetting(value)


def GammaFromDisableGamma(values, where, notes=None):
	# disableGamma, the key cameraGamma replaced, read as cameraGamma: True is
	# "Off" (linear, as it was), False the default. A config with both is
	# refused, since they can disagree.
	if "disableGamma" not in values:
		return values
	old = values.pop("disableGamma")
	if "cameraGamma" in values:
		raise ValueError("{} gives both cameraGamma and disableGamma. cameraGamma replaced disableGamma: "
			"remove disableGamma.".format(where))
	default = DefaultParams()["cameraGamma"]
	new = [GAMMA_OFF if v else default for v in old] if isinstance(old, list) else (GAMMA_OFF if old else default)
	values["cameraGamma"] = new
	note = "{} says disableGamma: {}, read as cameraGamma: {}. cameraGamma replaced disableGamma; write it " \
		"instead.".format(where, old, new)
	if notes is None:
		Say(note)
	else:
		notes.append(note)
	return values


def TriggerFrameRate(params):
	# The rate the trigger controller pulses at: the frame rate of the cameras it
	# triggers (cameraTrigger set), or of every camera if none is set. With one
	# frame rate per camera (a RatCam free-runs at its own), those must agree.
	cams = [CameraParams(params, n) for n in range(params["numCams"])]
	triggered = [cam for cam in cams if str(cam["cameraTrigger"]) != "None"] or cams
	rates = sorted(set(float(cam["frameRate"]) for cam in triggered))
	if len(rates) > 1:
		raise ValueError("The trigger controller pulses at one rate, but the triggered cameras have "
			"frame rates {}. Give them one frameRate, or set cameraTrigger \"None\" on the cameras "
			"it does not trigger.".format(", ".join("{:g}".format(r) for r in rates)))
	return int(rates[0]) if rates[0].is_integer() else rates[0]


def BlinkSettings(params):
	# The test LED's (pin, Hz) for the trigger controller; (-1, 0) when there is none
	pin, hz = params.get("blinkPin", "None"), params.get("blinkHz", "None")
	if str(pin) == "None" or str(hz) == "None" or float(hz) <= 0:
		return -1, 0
	hz = float(hz)
	return int(pin), (int(hz) if hz.is_integer() else hz)


def CameraParams(params, n_cam):
	# One camera's parameters, without opening its device: what OptParams gives
	# the camera's process. For checks that run before any camera starts.
	cam_params = copy.deepcopy(params)
	cam_params["n_cam"] = n_cam
	cam_params = OptParams(cam_params)
	cam_params["cameraName"] = cam_params["cameraNames"]
	return cam_params



def ValidateQuality(params):
	# quality is H.264's and H.265's quantizer or CRF, 0-51, and AV1's quantizer index, 0-255
	for n in range(params["numCams"]):
		cam = CameraParams(params, n)
		limit = 255 if str(cam["codec"]) == "av1" else 51
		if isinstance(cam["quality"], int) and cam["quality"] > limit:
			raise ValueError("quality is 0 to {} for codec {}, not {}{}.".format(limit, cam["codec"], cam["quality"],
				"" if params["numCams"] == 1 else " (camera {})".format(n)))


# Keys campy used to have: {old key: (the key that replaced it, a function)}.
# Each function takes one layer's values (a config file, or the command line),
# where they come from and a list for its note, and returns them with the old
# key read as the new one.
RENAMED = {"disableGamma": ("cameraGamma", GammaFromDisableGamma)}


def LoadConfig(config_path):
	try:
		with open(config_path, "rb") as f:
			config = yaml.safe_load(f)
	except Exception as e:
		logging.error('Caught this error at configurator.py LoadConfig: {}. Check your config path!'.format(e))
		raise
	return config


def ReadConfig(config_path):
	"""
	A config file's values and the line each key is on. A key given twice is
	refused (YAML would keep the last one without a word), as is a file that is
	not a list of key: value lines.
	"""
	with open(config_path, "rb") as f:
		text = f.read()
	name = config_path
	try:
		values = yaml.safe_load(text)
		node = yaml.compose(text, Loader=yaml.SafeLoader)
	except yaml.YAMLError as e:
		mark = getattr(e, "problem_mark", None)
		where = "{}:{}".format(name, mark.line + 1) if mark is not None else name
		raise ConfigError(["{}: campy can't read this YAML ({})".format(where, getattr(e, "problem", e))])
	if values is None:
		return {}, {}
	if not isinstance(values, dict):
		raise ConfigError(["{}: a campy config is a list of key: value lines, not a {}".format(
			name, type(values).__name__)])
	lines, problems = {}, []
	for key_node, _ in node.value:
		key, line = key_node.value, key_node.start_mark.line + 1
		if key in lines:
			problems.append("{}:{}: {} is already set on line {}. Give each key once.".format(name, line, key, lines[key]))
		lines[key] = line
	if problems:
		raise ConfigError(problems)
	return values, lines


def Distance(a, b):
	# Edits (insert, delete, change a letter) that turn a into b
	row = list(range(len(b) + 1))
	for i, ca in enumerate(a, 1):
		prev, row[0] = row[0], i
		for j, cb in enumerate(b, 1):
			prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
	return row[-1]


def Suggest(word, names):
	# The name word was probably meant to be, or None: the same name in another
	# case, then the nearest within a few edits, then the only one containing it
	lower = str(word).lower()
	same = [n for n in names if n.lower() == lower]
	if same:
		return same[0]
	scored = sorted((Distance(lower, n.lower()), n) for n in names)
	if scored and scored[0][0] <= (3 if len(lower) > 12 else 2) and \
			(len(scored) == 1 or scored[1][0] > scored[0][0]):
		return scored[0][1]
	inside = [n for n in names if len(lower) >= 4 and lower in n.lower()]
	if len(inside) == 1:
		return inside[0]
	return None


def UnknownKey(key):
	suggestion = Suggest(key, configschema.BY_NAME)
	if suggestion:
		return 'unknown key "{}". Did you mean "{}"?'.format(key, suggestion)
	return 'unknown key "{}". docs/config-reference.md (or campy-config reference) lists every key.'.format(key)


def ItemError(item, kind, words=(), lists=True):
	# What is wrong with one value of a kind (configschema), or None. Floats
	# take whole numbers too. Returns the value as campy uses it: PyYAML reads
	# 1e3 as text, which is a number here.
	if isinstance(item, str) and item in words:
		return item, None
	if kind is float and isinstance(item, str):
		try:
			return float(item), None
		except ValueError:
			pass
	if isinstance(item, bool):
		fits = kind is bool
	else:
		fits = kind is not bool and isinstance(item, (int, float) if kind is float else kind)
	if fits:
		return item, None
	expected = " or ".join([KIND_NAMES[kind]] + ['"{}"'.format(w) for w in words])
	if lists:
		expected += ", or a list of them"
	if item is None:
		hint = " (YAML reads null, ~ and an empty value as no value: write None)"
	elif isinstance(item, bool):
		hint = " (YAML reads yes, no, on, off, true and false as True or False: quote text, e.g. \"'off'\")"
	else:
		hint = ""
	return item, "{!r} is not {}{}".format(item, expected, hint)


def SettingError(setting, item):
	# One value of a setting: (the value as campy uses it, what is wrong or None)
	if setting.kind is configschema.GAMMA:
		try:
			return GammaSetting(item), None
		except ValueError as e:
			return item, str(e)
	if setting.kind is configschema.TEXT:
		if setting.name == "preflight" and item is False:
			return item, None # preflight: off, unquoted: YAML's False, which preflight reads as off
		if not isinstance(item, str):
			return item, "{!r} is not text".format(item)
	else:
		item, error = ItemError(item, setting.kind, setting.words, setting.per_camera != configschema.ONE)
		if error:
			return item, error
	if isinstance(item, str) and item in setting.words:
		return item, None
	if setting.choices:
		folded = item.lower() if setting.fold_case else item
		if folded not in setting.choices:
			near = Suggest(item, setting.choices)
			return item, "{!r} is not {}{}".format(item, setting.Takes(),
				'. Did you mean "{}"?'.format(near) if near else "")
	if isinstance(item, (int, float)) and not isinstance(item, bool):
		if (setting.above is not None and not item > setting.above) or \
				(setting.low is not None and item < setting.low) or \
				(setting.high is not None and item > setting.high):
			return item, "{!r} is not {}".format(item, setting.Takes())
	return item, None


def CheckValues(values, where):
	"""
	One layer's values (a config file, or the command line) as campy uses them,
	and what is wrong with each: unknown keys (with the key that was probably
	meant), values of the wrong kind, out of range or not one of the words a
	setting takes, and a list where the rig takes one value. where(key) says
	where the key was set, for the messages.
	"""
	checked, problems = {}, []
	for key, value in values.items():
		setting = configschema.BY_NAME.get(key)
		if setting is None:
			problems.append("{}: {}".format(where(key), UnknownKey(key)))
			continue
		if setting.kind is configschema.SECTION:
			if not isinstance(value, dict):
				problems.append("{}: {} is a section of key: value lines (indented under it), not {!r}".format(
					where(key), key, value))
			else:
				checked[key] = value
			continue
		if isinstance(value, list):
			if setting.per_camera == configschema.ONE:
				problems.append("{}: {} takes one value for the whole rig, not a list ({!r})".format(
					where(key), key, value))
				continue
			if not value:
				problems.append("{}: {} is an empty list".format(where(key), key))
				continue
			items = [SettingError(setting, item) for item in value]
			errors = [e for _, e in items if e]
			value = [v for v, _ in items]
		else:
			value, error = SettingError(setting, value)
			errors = [error] if error else []
		if errors:
			problems.append("{}: {}: {}".format(where(key), key, errors[0]))
		checked[key] = value
	return checked, problems


def ReadCheckedConfig(config_path):
	# A config's values, with the files it extends, checked (CheckValues) and
	# with renamed keys read as their new ones, for tools that take a config
	# but not campy-acquire's options (campy-encode-sweep, campy-encode-quality)
	notes, problems = [], []
	values, _ = MergeChain(ReadChain(config_path), notes, problems)
	if problems:
		raise ConfigError(problems)
	for note in notes:
		Say(note)
	return values


# What each kind of override value is called in an error
KIND_NAMES = {int: "a whole number", float: "a number", bool: "True or False", str: "text"}


def ConfigValue(kind, words=(), lists=True):
	"""
	An argparse type that reads an override the way LoadConfig reads the config
	(yaml.safe_load), so --key value means what key: value means there: Line3 is
	text, False is False, [24, 26] is one value per camera, and None is the text
	"None" that campy compares against. Each value must be of kind (float takes
	whole numbers too) or one of words, so a typo fails here, naming the option.
	lists=False is for options read once for the whole rig (numCams, the
	trigger's, recTimeInSec in the launch check), which a list would break.
	"""
	def Check(item):
		item, error = ItemError(item, kind, words, lists)
		if error:
			raise ArgumentTypeError(error)
		return item

	def Parse(text):
		try:
			value = yaml.safe_load(text)
		except yaml.YAMLError as e:
			raise ArgumentTypeError("{!r} is not a value campy can read ({})".format(text, getattr(e, "problem", e)))
		if lists and isinstance(value, list):
			return [Check(item) for item in value]
		return Check(value)

	return Parse


def GammaValue(text):
	# --cameraGamma: a number above 0 or "Off" (GammaSetting), or a list of them
	try:
		value = yaml.safe_load(text)
	except yaml.YAMLError:
		value = text
	try:
		return [GammaSetting(v) for v in value] if isinstance(value, list) else GammaSetting(value)
	except ValueError as e:
		raise ArgumentTypeError(str(e))


def TextValue(text):
	# Paths, and words YAML would change (preflight's off is not False), are taken
	# as typed, so C:\ffmpeg\bin\ffmpeg.exe keeps its backslashes. One pair of
	# quotes around the whole value is dropped, as in the old "'./configs/rgb24.pfs'".
	if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
		return text[1:-1]
	return text




def OptionType(setting):
	# The argparse type that reads a setting's --key value
	if setting.kind is configschema.TEXT:
		return TextValue
	if setting.kind is configschema.GAMMA:
		return GammaValue
	return ConfigValue(setting.kind, setting.words, lists=setting.per_camera != configschema.ONE)


def ParseClargs(parser, argv=None):
	parser.epilog = "Each --key value means what key: value means in the config file, which is YAML: \
		Line3 and h265 are text, True and False are booleans, and None is the text None. YAML also reads \
		yes, no, on and off as booleans, which the True/False options take; text options refuse them, \
		so quote such text: \"'off'\". Options that take one value per camera also take a list, e.g. \
		--quality \"[24, 26]\" (quoted for the shell). Paths, and --preflight, are taken as typed. \
		docs/config-reference.md describes every setting."
	# --qual is not --quality: an option is its whole name
	parser.allow_abbrev = False
	parser.add_argument(
		"config", metavar="config", help="Campy configuration .yaml file.",
	)
	groups = {section: parser.add_argument_group(section) for section in configschema.SECTIONS}
	for setting in configschema.SETTINGS:
		if setting.kind is configschema.SECTION:
			continue # in the config file only
		groups[setting.section].add_argument(
			"--" + setting.name,
			dest=setting.name,
			type=OptionType(setting),
			help=setting.help,
		)

	# An option campy doesn't have stops here, with the one probably meant
	for word in (argv if argv is not None else sys.argv[1:]):
		if word.startswith("--") and len(word) > 2:
			name = word[2:].split("=", 1)[0]
			if (name not in configschema.BY_NAME or configschema.BY_NAME[name].kind is configschema.SECTION) \
					and name != "help":
				suggestion = Suggest(name, configschema.BY_NAME)
				parser.error("unknown option {}{}".format("--" + name,
					". Did you mean --{}?".format(suggestion) if suggestion else ""))

	return parser.parse_args(argv)
