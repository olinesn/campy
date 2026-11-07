"""
campy-session panel's model picker: which trained models campy-live tracks
with, chosen on the panel's page before the start, not by editing the config.

Where the choices come from:

  live.centroid / live.instance   the config's own pair, always offered
  panel.models                    folders looked through for trained sleap-nn
                                  model folders (best.ckpt beside
                                  training_config.yaml or .json), down to
                                  SCAN_DEPTH folders below each
  panel.model_sets                named pairs, {name: {centroid: folder,
                                  instance: folder}}, the pairs that go
                                  together

The two lists are panel: keys, not live: ones, so that a rig profile can
hold them: a live: section in a profile would turn on live tracking for
every experiment built on it, a panel: section turns on nothing.

Each model folder is described from its own files, without torch or sleap-nn
(the panel runs in the recording environment): its training config says what
it is (centroid, centered instance, multi-class top-down, or another kind,
which campy-live can't run and the picker leaves out), its input (scale, the
frame size it was trained on, grey or colour, the crop), its network (max
stride, output stride, filters), its skeleton, and, when its folder has
sleap-nn's training_log.csv, its validation score at its best epoch (lowest
val/loss): F1 for a centroid model, mAP and mOKS for an instance model.
The rigs' own model folders hold only best.ckpt and training_config.yaml,
so they have no score.

Folder names don't say which centroid model goes with which instance model
(realtime_centroid_os2_s25 with realtime_i320_s050_ms32_sg5_f24; a training
job's 261030_201359.centroid with 261030_210618.centered_instance), so the
picker pairs them by their skeleton and their time: the instance list puts
the models with the centroid's skeleton first, and a centroid picked alone
gets the one of those trained nearest to it in time (an instance model
picked after it stays as picked).

The checks before a start (Picker.Checks, the panel's checks list): a
missing folder, best.ckpt or training config, a model in the wrong role, or
a centroid model whose anchor part the instance model's skeleton lacks stop
the start; different skeletons that share the anchor, a pair that
--fixed-crops can't run (multi-class, box-scaled crops: campy-live then runs
sleap-nn's own top-down layer, slower; a stop when the config's args force
--fixed-crops on), and a model trained on another frame size than the
cameras' only warn. A pair new to the rig builds its compiled engines at its
first start (--engine: about 26-28 s on the Windows rigs' NVIDIA GPUs).

A pick holds for the recordings the panel starts: it goes into each one's
live: section (campy/session.py's Config, live=), so session-config.yaml,
campy-acquire.yaml (session.json) and campy-live-session.json all say which
models it used. The config file is never changed. The last pair started
from each config is kept in ~/.campy/status/panel-models.json, and the panel
opens on it the next time, while both folders are still there.
"""

import csv, os, time
from datetime import datetime

SCAN_DEPTH = 4 # folders below each panel.models folder
SCAN_DIRS = 5000 # at most this many folders looked at, in all
SCAN_S = 10.0 # and for at most this long
CONFIG_NAMES = ("training_config.yaml", "training_config.json")
# sleap-nn's heads, as the picker uses them
ROLES = {"centroid": "centroid", "centered_instance": "instance", "multi_class_topdown": "instance"}
KINDS = {"centroid": "centroid", "centered_instance": "centered instance", "multi_class_topdown": "multi-class top-down",
	"single_instance": "single instance", "bottomup": "bottom-up", "multi_class_bottomup": "multi-class bottom-up"}
ENGINE_NOTE = "a pair's first start builds its compiled engines (--engine; about 26-28 s on the Windows rigs' " \
	"NVIDIA GPUs), then they load from the cache"
REMEMBER = "panel-models.json"


def Key(path):
	return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


def Dig(config, *path):
	for key in path:
		if not isinstance(config, dict) or key not in config:
			return None
		config = config[key]
	return config


def ReadConfig(folder):
	# (its training config's file name, the config), or (None, None)
	for name in CONFIG_NAMES:
		path = os.path.join(folder, name)
		if os.path.isfile(path):
			with open(path, encoding="utf-8", errors="replace") as f:
				if name.endswith(".json"):
					import json
					return name, json.load(f)
				import yaml
				return name, yaml.safe_load(f)
	return None, None


def Head(config):
	# The model's kind: its first head that is set (sleap-nn's head_configs, the others null)
	heads = Dig(config, "model_config", "head_configs")
	if isinstance(heads, dict):
		for kind, head in heads.items():
			if head:
				return kind, head
	return None, None


def Nodes(config):
	# Its skeleton's node names, or None
	skeletons = Dig(config, "data_config", "skeletons")
	if isinstance(skeletons, dict): # {name: {nodes, edges}}
		skeletons = list(skeletons.values())
	if not skeletons or not isinstance(skeletons[0], dict):
		return None
	nodes = skeletons[0].get("nodes") or []
	return [n["name"] if isinstance(n, dict) else str(n) for n in nodes]


def Number(value):
	try:
		value = float(value)
	except (TypeError, ValueError):
		return None
	return value if value == value else None # NaN: none


SCORES = { # training_log.csv's columns, and how the picker says them
	"centroid": (("eval/val/centroid_f1", "F1"), ("eval/val/centroid_precision", "precision"),
		("eval/val/centroid_recall", "recall"), ("eval/val/centroid_dist_median", "median error px")),
	"instance": (("eval/val/oks_voc_mAP", "mAP"), ("eval/val/mOKS", "mOKS"), ("eval/val/distance/p50", "median error px")),
	}


def Score(folder, role):
	"""
	Its validation score, from sleap-nn's training_log.csv: the evaluation's
	values at the epoch with the lowest val/loss (best.ckpt's), or at the
	last epoch evaluated if that one wasn't: {"epoch", "values": {name:
	value}, "text"}, or None.
	"""
	path = os.path.join(folder, "training_log.csv")
	try:
		with open(path, newline="", encoding="utf-8", errors="replace") as f:
			rows = list(csv.DictReader(f))
	except OSError:
		return None
	columns = SCORES.get(role, ())
	scored = [r for r in rows if columns and Number(r.get(columns[0][0])) is not None]
	best = [r for r in rows if Number(r.get("val/loss")) is not None]
	if not best:
		return None
	row = min(best, key=lambda r: Number(r["val/loss"]))
	if scored and row not in scored:
		row = scored[-1]
	values = {name: Number(row.get(column)) for column, name in columns if Number(row.get(column)) is not None}
	if values:
		first = [n for n in ("F1", "mAP", "mOKS") if n in values]
		text = ", ".join("{} {:.3f}".format(n, values[n]) for n in first) or ", ".join(
			"{} {:.3g}".format(n, v) for n, v in values.items())
	else:
		values = {"val/loss": Number(row["val/loss"])}
		text = "val loss {:.3g}".format(values["val/loss"])
	epoch = Number(row.get("epoch"))
	return {"epoch": int(epoch) if epoch is not None else None, "values": values, "text": text}


def Describe(folder, root=None):
	"""
	One model folder, for the picker: {folder, name, date, kind, role,
	settings..., nodes, score, problems (what stops its use), fixed (why
	--fixed-crops can't run it, or None), text}.
	"""
	folder = os.path.abspath(os.path.expanduser(str(folder)))
	name = os.path.relpath(folder, root) if root and Key(folder).startswith(Key(root) + os.sep) else \
		os.path.basename(folder.rstrip("\\/")) or folder
	model = {"folder": folder, "name": name.replace("\\", "/"), "date": None, "kind": None, "role": None,
		"problems": [], "fixed": None, "score": None, "nodes": None, "node_names": None}
	if not os.path.isdir(folder):
		model["problems"].append("{} isn't there".format(folder))
		model["text"] = "not there"
		return model
	ckpt = os.path.join(folder, "best.ckpt")
	if os.path.isfile(ckpt):
		model["date"] = datetime.fromtimestamp(os.path.getmtime(ckpt)).strftime("%Y-%m-%d")
	else:
		model["problems"].append("no best.ckpt in {}".format(folder))
	try:
		file, config = ReadConfig(folder)
	except Exception as e:
		file, config = "training_config", None
		model["problems"].append("its training config can't be read ({}: {})".format(type(e).__name__, e))
	if file is None:
		model["problems"].append("no training_config.yaml or .json in {}".format(folder))
	if not isinstance(config, dict):
		model["text"] = "; ".join(model["problems"])
		return model
	kind, head = Head(config)
	model["kind"], model["role"] = kind, ROLES.get(kind)
	pre = Dig(config, "data_config", "preprocessing") or {}
	backbone = next((b for b in (Dig(config, "model_config", "backbone_config") or {}).values() if isinstance(b, dict)), {})
	confmaps = head.get("confmaps") if isinstance(head, dict) and isinstance(head.get("confmaps"), dict) else head
	confmaps = confmaps if isinstance(confmaps, dict) else {}
	crop = pre.get("crop_size") or pre.get("crop_hw")
	model.update({"scale": Number(pre.get("scale")), "crop": crop if model["role"] == "instance" else None,
		"trained_hw": [pre.get("max_height"), pre.get("max_width")] if pre.get("max_height") and pre.get("max_width")
			else None, "grey": bool(pre.get("ensure_grayscale")), "rgb": bool(pre.get("ensure_rgb")),
		"max_stride": backbone.get("max_stride"), "output_stride": confmaps.get("output_stride",
			backbone.get("output_stride")), "filters": backbone.get("filters"), "anchor": confmaps.get("anchor_part")})
	names = Nodes(config)
	model["node_names"], model["nodes"] = names, len(names) if names else None
	if kind == "multi_class_topdown":
		model["fixed"] = "a multi-class model"
	elif model["role"] == "instance" and pre.get("crop_mode") not in (None, "fixed"):
		model["fixed"] = "box-scaled crops (crop_mode {})".format(pre.get("crop_mode"))
	if model["role"]:
		try:
			model["score"] = Score(folder, model["role"])
		except Exception:
			model["score"] = None
	model["text"] = Summary(model)
	return model


def Summary(m):
	# "centroid · scale 0.25 · max stride 16 · output stride 2 · 16 filters · 15 nodes · F1 0.941"
	parts = [KINDS.get(m["kind"], m["kind"] or "?")]
	if m.get("crop"):
		crop = m["crop"]
		parts.append("crop {}".format("x".join(str(c) for c in crop) if isinstance(crop, (list, tuple)) else crop))
	if m.get("scale") is not None:
		parts.append("scale {:g}".format(m["scale"]))
	for key, said in (("max_stride", "max stride {}"), ("output_stride", "output stride {}"), ("filters", "{} filters")):
		if m.get(key) is not None:
			parts.append(said.format(m[key]))
	if m.get("nodes"):
		parts.append("{} nodes".format(m["nodes"]))
	if m.get("trained_hw"):
		parts.append("trained on {}x{}".format(*m["trained_hw"]))
	if m.get("score"):
		parts.append(m["score"]["text"] + (" (epoch {})".format(m["score"]["epoch"]) if m["score"]["epoch"] is not None
			else ""))
	return " · ".join(parts)


def Scan(roots, depth=SCAN_DEPTH, max_dirs=SCAN_DIRS, max_s=SCAN_S):
	"""
	The model folders under roots: ([model descriptions], [what to say]):
	a folder that isn't there, a look cut short, the models of other kinds
	left out.
	"""
	models, notes, seen, looked = [], [], set(), 0
	t0 = time.monotonic()
	other = 0
	for root in roots:
		root = os.path.abspath(os.path.expanduser(str(root)))
		if not os.path.isdir(root):
			notes.append("panel.models: {} isn't there".format(root))
			continue
		stack = [(root, 0)]
		while stack:
			if looked >= max_dirs or time.monotonic() - t0 > max_s:
				notes.append("panel.models: stopped looking after {} folders ({:.0f} s): give the models' own folders"
					.format(looked, time.monotonic() - t0))
				stack = []
				break
			folder, level = stack.pop()
			looked += 1
			try:
				entries = list(os.scandir(folder))
			except OSError:
				continue
			names = {e.name for e in entries if e.is_file()}
			if "best.ckpt" in names and names & set(CONFIG_NAMES):
				if Key(folder) not in seen:
					seen.add(Key(folder))
					model = Describe(folder, root)
					if model["role"]:
						models.append(model)
					else:
						other += 1
				continue # a model's own folder: nothing below it
			if level < depth:
				stack += [(e.path, level + 1) for e in sorted(entries, key=lambda e: e.name, reverse=True)
					if e.is_dir() and not e.name.startswith((".", "__"))]
	if other:
		notes.append("{} model folder{} of other kinds (bottom-up, single instance...) left out: campy-live runs "
			"top-down models".format(other, "" if other == 1 else "s"))
	return models, notes


def Sameish(a, b):
	return Key(a) == Key(b) if a and b else False


class Picker:
	"""
	The panel's choice of models: what it offers (the config's pair,
	panel.model_sets, panel.models' folders), the pick, and its checks.
	"""

	def __init__(self, config, remember=None):
		self.config = config
		self.live = config.live
		self.folders = config.panel["models"]
		self.remember = remember if remember is not None else RememberFile()
		self.configured = {"centroid": self.live["centroid"], "instance": self.live["instance"]}
		self.sets = [{"name": str(name), "centroid": str(pair["centroid"]), "instance": str(pair["instance"])}
			for name, pair in config.panel["model_sets"].items()]
		self.Scan()
		self.pick, self.source = self.Default()

	def Scan(self):
		t0 = time.monotonic()
		models, self.notes = Scan(self.folders)
		self.scan_s = time.monotonic() - t0
		self.scanned = len(models)
		# The config's pair and the sets' models, wherever they are
		known = {Key(m["folder"]) for m in models}
		for folder in [self.configured["centroid"], self.configured["instance"]] + \
				[s[k] for s in self.sets for k in ("centroid", "instance")]:
			if folder and Key(folder) not in known:
				known.add(Key(folder))
				models.append(Describe(folder))
		self.models = models
		self.byKey = {Key(m["folder"]): m for m in models}

	def Model(self, folder):
		return self.byKey.get(Key(folder)) if folder else None

	def Default(self):
		# The pair last started from this config, while both are there; else the config's; else the first set's
		last = Remembered(self.remember, self.config.path)
		if last and all(os.path.isdir(last.get(k) or "") for k in ("centroid", "instance")):
			for k in ("centroid", "instance"):
				if Key(last[k]) not in self.byKey:
					self.models.append(Describe(last[k]))
					self.byKey[Key(last[k])] = self.models[-1]
			return {"centroid": last["centroid"], "instance": last["instance"]}, "last used"
		if self.configured["centroid"] and self.configured["instance"]:
			return dict(self.configured), "the config's"
		if self.sets:
			return {"centroid": self.sets[0]["centroid"], "instance": self.sets[0]["instance"]}, \
				"set {}".format(self.sets[0]["name"])
		return {"centroid": "", "instance": ""}, None

	def Choose(self, centroid=None, instance=None, set_name=None):
		"""
		The page's pick: a set by its name, or the two folders (an instance
		model of None: the one that goes best with the centroid). Only the
		models it offers. An error text, or None.
		"""
		if set_name is not None:
			if set_name == "config":
				if not (self.configured["centroid"] and self.configured["instance"]):
					return "The config names no pair."
				self.pick, self.source = dict(self.configured), "the config's"
				return None
			found = [s for s in self.sets if s["name"] == set_name]
			if not found:
				return "No set {!r}.".format(set_name)
			self.pick, self.source = {"centroid": found[0]["centroid"], "instance": found[0]["instance"]}, \
				"set {}".format(set_name)
			return None
		centroid = centroid if centroid is not None else self.pick["centroid"]
		if self.Model(centroid) is None:
			return "Not one of the picker's models: {}".format(centroid)
		if instance is None:
			instance = self.Partner(centroid)
		elif self.Model(instance) is None:
			return "Not one of the picker's models: {}".format(instance)
		self.pick, self.source = {"centroid": self.Model(centroid)["folder"],
			"instance": self.Model(instance)["folder"] if instance else ""}, "picked"
		return None

	def Partner(self, centroid):
		# The instance model for a centroid picked alone: of those with its skeleton, the one trained nearest to it in
		# time (a training job's two halves; one skeleton is often every model's, so not merely the one picked before);
		# without any, the one picked
		c = self.Model(centroid)
		now = self.Model(self.pick["instance"])
		same = [m for m in self.Instances() if m["node_names"] and m["node_names"] == c["node_names"]]
		if not same:
			return now["folder"] if now is not None else ""
		def apart(m):
			try:
				return abs(os.path.getmtime(os.path.join(m["folder"], "best.ckpt")) -
					os.path.getmtime(os.path.join(c["folder"], "best.ckpt")))
			except OSError:
				return float("inf")
		return min(same, key=apart)["folder"]

	def Centroids(self):
		return sorted([m for m in self.models if m["role"] == "centroid" or (m["role"] is None and m["folder"] in
			(self.configured["centroid"], self.pick["centroid"] if hasattr(self, "pick") else ""))],
			key=lambda m: m["date"] or "", reverse=True)

	def Instances(self):
		c = self.Model(self.pick["centroid"]) if hasattr(self, "pick") else None
		nodes = c["node_names"] if c else None
		wanted = [m for m in self.models if m["role"] == "instance" or (m["role"] is None and m["folder"] in
			(self.configured["instance"], self.pick["instance"] if hasattr(self, "pick") else ""))]
		# The centroid's skeleton first, the newest first
		return sorted(sorted(wanted, key=lambda m: m["date"] or "", reverse=True),
			key=lambda m: 0 if nodes and m["node_names"] == nodes else 1)

	def SetName(self):
		# The set the pick is: "config", a set's name, or None
		p = self.pick
		if Sameish(p["centroid"], self.configured["centroid"]) and Sameish(p["instance"], self.configured["instance"]):
			return "config"
		for s in self.sets:
			if Sameish(p["centroid"], s["centroid"]) and Sameish(p["instance"], s["instance"]):
				return s["name"]
		return None

	def Pick(self):
		return dict(self.pick) if self.pick["centroid"] and self.pick["instance"] else None

	def Remember(self):
		# This config's pair from now on, for the next panel: after a start
		pick = self.Pick()
		if pick:
			Remember(self.remember, self.config.path, pick)

	def New(self):
		# Not the pair last started here: its engines may have to be built
		last = Remembered(self.remember, self.config.path)
		return not (last and Sameish(last.get("centroid"), self.pick["centroid"]) and
			Sameish(last.get("instance"), self.pick["instance"]))

	def FrameShape(self):
		# The cameras' (height, width, channels), as campy-session gives campy-live --frame-shape
		shape = self.live.get("frame_shape") or ""
		if shape:
			try:
				h, w, *ch = (int(v) for v in shape.lower().split("x"))
				return h, w, ch[0] if ch else 1
			except ValueError:
				return None
		try:
			h, w = int(self.config.Value("frameHeight")), int(self.config.Value("frameWidth"))
		except (TypeError, ValueError):
			return None
		return h, w, 1 if str(self.config.Value("pixelFormatInput", "")) in ("gray", "mono8") else 3

	def FixedForced(self):
		args = [str(a) for a in self.live["args"]]
		for i, a in enumerate(args):
			if a == "--fixed-crops" and i + 1 < len(args):
				return args[i + 1] == "on"
			if a == "--fixed-crops=on":
				return True
		return False

	def Checks(self):
		"""The pick's checks for the panel: [(level, text)], "block" stops a start, "warn" only warns."""
		c, i = self.Model(self.pick["centroid"]), self.Model(self.pick["instance"])
		if c is None or i is None:
			return [("block", "Live models: pick a centroid model and an instance model (panel.models, panel.model_sets).")]
		out = []
		for slot, m, role in (("centroid", c, "centroid"), ("instance", i, "instance")):
			for problem in m["problems"]:
				out.append(("block", "Live models: the {} model: {}.".format(slot, problem)))
			if not m["problems"] and m["role"] != role:
				out.append(("block", "Live models: the {} model ({}) is a {} model, not a {} model.".format(slot,
					m["name"], KINDS.get(m["kind"], m["kind"] or "model of no kind"),
					"centroid" if role == "centroid" else "centered-instance")))
		if not any(level == "block" for level, _ in out):
			cn, inn = c["node_names"], i["node_names"]
			if cn and inn and cn != inn:
				anchor = c.get("anchor")
				if anchor and anchor not in inn:
					out.append(("block", "Live models: the centroid model's anchor part, {}, isn't in the instance "
						"model's skeleton ({} nodes): they weren't trained on the same skeleton.".format(anchor, len(inn))))
				else:
					out.append(("warn", "Live models: the two models' skeletons differ (centroid {} nodes, instance {}); "
						"the centroid model only finds the anchor{}, which both have.".format(len(cn), len(inn),
						" ({})".format(anchor) if anchor else "")))
			if i.get("fixed"):
				text = "--fixed-crops can't run this instance model ({}): campy-live runs sleap-nn's own top-down " \
					"layer, slower".format(i["fixed"])
				out.append(("block" if self.FixedForced() else "warn", "Live models: " + text +
					(" (the config's args force --fixed-crops on)" if self.FixedForced() else "") + "."))
			shape = self.FrameShape()
			for slot, m in (("centroid", c), ("instance", i)):
				if shape and m.get("trained_hw") and list(m["trained_hw"]) != [shape[0], shape[1]]:
					out.append(("warn", "Live models: the {} model was trained on {}x{} frames; the cameras give {}x{} "
						"(sleap-nn fits each frame to {}x{} first).".format(slot, m["trained_hw"][0], m["trained_hw"][1],
						shape[0], shape[1], m["trained_hw"][0], m["trained_hw"][1])))
			crop = i.get("crop")
			side = max(crop) if isinstance(crop, (list, tuple)) else Number(crop)
			if shape and side and side > min(shape[0], shape[1]):
				out.append(("warn", "Live models: the instance model's crop ({}) is larger than the cameras' frames "
					"({}x{}).".format(crop, shape[0], shape[1])))
		said = "Live models ({}): centroid {}, instance {}".format(self.source or "picked", c["name"], i["name"])
		if self.New() and not any(level == "block" for level, _ in out):
			said += "; " + ENGINE_NOTE
		return [("ok", said)] + out

	def Status(self):
		def Shown(m):
			return {k: m.get(k) for k in ("folder", "name", "date", "kind", "role", "text", "problems", "nodes",
				"anchor", "fixed")}
		return {"pick": dict(self.pick), "source": self.source, "set": self.SetName(),
			"config": dict(self.configured) if self.configured["centroid"] and self.configured["instance"] else None,
			"sets": self.sets, "centroids": [Shown(m) for m in self.Centroids()],
			"instances": [dict(Shown(m), same=bool(self.Model(self.pick["centroid"]) and m["node_names"] and
				m["node_names"] == self.Model(self.pick["centroid"])["node_names"])) for m in self.Instances()],
			"roots": [os.path.abspath(os.path.expanduser(str(r))) for r in self.folders],
			"scanned": self.scanned, "scan_s": round(self.scan_s, 2), "notes": self.notes,
			"new": self.New()}


# ------------------------------------------------------------------ the last pair, per config

def RememberFile():
	from campy import session
	return os.path.join(session.StatusDir(), REMEMBER)


def Remembered(path, config):
	from campy import strictjson
	try:
		with open(path) as f:
			got = strictjson.load(f).get(Key(config))
		return got if isinstance(got, dict) else None
	except (OSError, ValueError, AttributeError):
		return None


def Remember(path, config, pick):
	from campy import session, strictjson
	try:
		with open(path) as f:
			everything = strictjson.load(f)
		if not isinstance(everything, dict):
			everything = {}
	except (OSError, ValueError):
		everything = {}
	everything[Key(config)] = {"centroid": pick["centroid"], "instance": pick["instance"],
		"at": datetime.now().astimezone().isoformat(timespec="seconds")}
	try:
		os.makedirs(os.path.dirname(path), exist_ok=True)
		session.WriteJson(path, everything)
	except OSError:
		pass
