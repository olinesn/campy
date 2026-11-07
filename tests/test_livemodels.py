"""
The panel's model picker (campy/livemodels.py): model folders found and
described from their own files, pairs, checks, the last pair remembered,
and the pick carried into the recording's config (campy/session.py's
Config, live=) and the panel (campy/panel.py).
"""

import json
import os
import sys
import time
import types

import pytest
import yaml

from campy import livemodels, panel, session
from test_session import home, write_config # noqa: F401 (home: a fixture)

NODES = ["Nose", "Head", "Trunk", "TailBase"]


def model(folder, head, nodes=NODES, scale=1.0, crop=None, hw=(1024, 1280), log=None, crop_mode=None, ckpt=True,
		anchor="Trunk", max_stride=16, filters=16):
	"""A trained model's folder, as sleap-nn 0.3.5 leaves it: training_config.yaml and best.ckpt (empty here)."""
	os.makedirs(folder, exist_ok=True)
	heads = {k: None for k in ("single_instance", "centroid", "centered_instance", "bottomup", "multi_class_topdown")}
	heads[head] = {"confmaps": {"anchor_part": anchor, "sigma": 2.5, "output_stride": 2}}
	pre = {"ensure_grayscale": True, "ensure_rgb": False, "max_height": hw[0] if hw else None,
		"max_width": hw[1] if hw else None, "scale": scale, "crop_size": crop}
	if crop_mode:
		pre["crop_mode"] = crop_mode
	config = {"data_config": {"train_labels_path": ["labels.slp"], "preprocessing": pre,
		"skeletons": [{"name": "mouse", "nodes": [{"name": n} for n in nodes], "edges": []}]},
		"model_config": {"backbone_config": {"unet": {"filters": filters, "max_stride": max_stride, "output_stride": 2}},
		"head_configs": heads}}
	with open(os.path.join(folder, "training_config.yaml"), "w") as f:
		yaml.safe_dump(config, f)
	if ckpt:
		open(os.path.join(folder, "best.ckpt"), "wb").close()
	if log:
		with open(os.path.join(folder, "training_log.csv"), "w") as f:
			f.write(",".join(log[0]) + "\n")
			for row in log[1:]:
				f.write(",".join(str(v) for v in row) + "\n")
	return str(folder)


def models_dir(tmp_path):
	root = tmp_path / "models"
	c = model(root / "rig_centroid_os2_s25", "centroid", scale=0.25)
	i = model(root / "rig_i320_s050_ms32", "centered_instance", crop=320, max_stride=32, filters=24)
	return root, c, i


def live_config(tmp_path, **live):
	path = write_config(tmp_path / "c.yaml", {"numCams": 2, "frameHeight": 1024, "frameWidth": 1280,
		"pixelFormatInput": "gray", "videoFolder": str(tmp_path / "data")}, live=dict({"python": sys.executable}, **live))
	return session.Config(str(path))


# ------------------------------------------------------------------ finding and describing models

def test_a_folder_of_models_is_looked_through_and_each_described(tmp_path):
	root, c, i = models_dir(tmp_path)
	# A training job's folders, a level down, with sleap-nn's log; a bottom-up model, which campy-live can't run; a
	# folder with no best.ckpt yet (training)
	job = root / "job.training_job" / "models"
	log = [["epoch", "val/loss", "eval/val/centroid_f1", "eval/val/centroid_precision"],
		[1, 0.5, 0.80, 0.81], [2, 0.3, 0.94, 0.95], [3, 0.4, 0.90, 0.91]]
	model(job / "261030_201359.centroid.n=30869", "centroid", scale=0.25, log=log)
	model(job / "261030_210618.centered_instance.n=30869", "centered_instance", crop=450,
		log=[["epoch", "val/loss", "eval/val/oks_voc_mAP", "eval/val/mOKS"], [1, 0.2, "", ""], [2, 0.1, "", ""],
		[3, 0.15, 0.72, 0.84]])
	model(job / "bottomup.n=10", "bottomup")
	model(job / "training", "centroid", ckpt=False)
	found, notes = livemodels.Scan([str(root), str(tmp_path / "nowhere")])
	names = sorted(m["name"] for m in found)
	assert names == ["job.training_job/models/261030_201359.centroid.n=30869",
		"job.training_job/models/261030_210618.centered_instance.n=30869", "rig_centroid_os2_s25", "rig_i320_s050_ms32"]
	assert any("nowhere isn't there" in n for n in notes)
	assert any(n.startswith("1 model folder of other kinds") for n in notes)
	by = {m["name"].split("/")[-1]: m for m in found}
	cm = by["261030_201359.centroid.n=30869"]
	assert cm["role"] == "centroid" and cm["scale"] == 0.25 and cm["nodes"] == 4 and cm["anchor"] == "Trunk"
	assert cm["trained_hw"] == [1024, 1280] and cm["max_stride"] == 16 and cm["output_stride"] == 2
	# The score at best.ckpt's epoch (the lowest val/loss)
	assert cm["score"]["epoch"] == 2 and cm["score"]["values"]["F1"] == 0.94
	assert "F1 0.940 (epoch 2)" in cm["text"] and cm["text"].startswith("centroid · scale 0.25")
	# The instance model was evaluated only at its last epoch: that one's
	im = by["261030_210618.centered_instance.n=30869"]
	assert im["role"] == "instance" and im["crop"] == 450 and im["score"]["epoch"] == 3
	assert "mAP 0.720, mOKS 0.840" in im["text"]
	assert by["rig_i320_s050_ms32"]["score"] is None and "crop 320" in by["rig_i320_s050_ms32"]["text"]


def test_a_look_stops_at_its_depth_and_its_folder_count(tmp_path):
	deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "f"
	model(deep, "centroid")
	assert livemodels.Scan([str(tmp_path)])[0] == []
	assert len(livemodels.Scan([str(tmp_path)], depth=6)[0]) == 1
	found, notes = livemodels.Scan([str(tmp_path)], depth=6, max_dirs=3)
	assert found == [] and "stopped looking after 3 folders" in notes[0]


def test_a_missing_folder_or_file_is_said(tmp_path):
	gone = livemodels.Describe(str(tmp_path / "gone"))
	assert gone["problems"] == ["{} isn't there".format(tmp_path / "gone")]
	model(tmp_path / "noconfig", "centroid")
	os.remove(tmp_path / "noconfig" / "training_config.yaml")
	assert "no training_config.yaml or .json" in livemodels.Describe(str(tmp_path / "noconfig"))["problems"][0]


# ------------------------------------------------------------------ the picker

def test_the_config_pair_alone_without_the_new_keys(tmp_path, home):
	root, c, i = models_dir(tmp_path)
	p = livemodels.Picker(live_config(tmp_path, centroid=c, instance=i))
	status = p.Status()
	assert status["pick"] == {"centroid": c, "instance": i} and status["source"] == "the config's"
	assert status["set"] == "config" and status["roots"] == [] and status["scanned"] == 0
	assert [m["folder"] for m in status["centroids"]] == [c] and [m["folder"] for m in status["instances"]] == [i]
	checks = p.Checks()
	assert checks[0][0] == "ok" and checks[0][1].startswith("Live models (the config's): centroid rig_centroid_os2_s25, "
		"instance rig_i320_s050_ms32") and livemodels.ENGINE_NOTE in checks[0][1]
	assert len(checks) == 1


def test_sets_and_folders_and_a_centroid_picked_alone(tmp_path, home):
	root, c, i = models_dir(tmp_path)
	other = model(tmp_path / "other" / "fly_instance", "centered_instance", nodes=["Head", "Thorax", "Wing"],
		anchor="Thorax", crop=96)
	newc = model(root / "new_centroid", "centroid", scale=0.5)
	newi = model(root / "new_instance", "centered_instance", crop=256)
	t = time.time()
	for folder, at in ((c, t - 9000), (i, t - 8900), (newc, t - 100), (newi, t - 50)):
		os.utime(os.path.join(folder, "best.ckpt"), (at, at))
	config = live_config(tmp_path, models=[str(root)], sets={"fly": {"centroid": c, "instance": other}})
	p = livemodels.Picker(config)
	# No pair in the config: the first set's
	assert p.Status()["pick"] == {"centroid": c, "instance": other} and p.Status()["set"] == "fly"
	# The centroid's skeleton first, newest first; the other skeleton's last
	assert [(m["name"], m["same"]) for m in p.Status()["instances"]] == [("new_instance", True),
		("rig_i320_s050_ms32", True), ("fly_instance", False)]
	assert p.Checks()[1][0] == "block" and "anchor part, Trunk, isn't in the instance model's skeleton" in p.Checks()[1][1]
	# A centroid picked alone gets the instance model with its skeleton trained nearest to it
	assert p.Choose(centroid=newc) is None
	assert p.pick == {"centroid": newc, "instance": newi} and p.source == "picked" and p.SetName() is None
	assert p.Choose(centroid=c) is None and p.pick["instance"] == newi # the one picked has the same skeleton: kept
	assert p.Choose(centroid=c, instance=i) is None and p.pick == {"centroid": c, "instance": i}
	# Only the picker's own models
	assert p.Choose(centroid=str(tmp_path / "elsewhere")).startswith("Not one of the picker's models")
	assert p.Choose(set_name="nope") == "No set 'nope'."
	assert p.Choose(set_name="config") == "The config names no pair."
	assert p.Choose(set_name="fly") is None and p.pick["instance"] == other
	# A centroid model in the instance's place
	assert p.Choose(centroid=c, instance=newc) is None
	assert any(level == "block" and "is a centroid model, not a centered-instance model" in text
		for level, text in p.Checks())


def test_checks_that_only_warn_and_one_the_config_makes_a_stop(tmp_path, home):
	c = model(tmp_path / "c", "centroid", nodes=NODES + ["TailTip"])
	i = model(tmp_path / "i", "multi_class_topdown", crop=320, hw=(1200, 1920))
	p = livemodels.Picker(live_config(tmp_path, centroid=c, instance=i))
	levels = [(level, text) for level, text in p.Checks()[1:]]
	assert [lv for lv, _ in levels] == ["warn", "warn", "warn"]
	assert "skeletons differ (centroid 5 nodes, instance 4); the centroid model only finds the anchor (Trunk)" in levels[0][1]
	assert "--fixed-crops can't run this instance model (a multi-class model)" in levels[1][1]
	assert "the instance model was trained on 1200x1920 frames; the cameras give 1024x1280" in levels[2][1]
	p = livemodels.Picker(live_config(tmp_path, centroid=c, instance=i, args=["--fixed-crops", "on"]))
	assert [lv for lv, t in p.Checks() if "--fixed-crops" in t] == ["block"]
	box = model(tmp_path / "box", "centered_instance", crop=320, crop_mode="bbox")
	assert livemodels.Describe(box)["fixed"] == "box-scaled crops (crop_mode bbox)"


def test_the_last_pair_started_is_where_the_next_panel_opens(tmp_path, home):
	root, c, i = models_dir(tmp_path)
	newi = model(root / "new_instance", "centered_instance", crop=256)
	config = live_config(tmp_path, centroid=c, instance=i, models=str(root))
	assert config.live["models"] == [str(root)] # one folder, as a list
	p = livemodels.Picker(config)
	assert p.Choose(centroid=c, instance=newi) is None and p.New()
	p.Remember()
	again = livemodels.Picker(live_config(tmp_path, centroid=c, instance=i, models=str(root)))
	assert again.pick == {"centroid": c, "instance": newi} and again.source == "last used" and not again.New()
	assert livemodels.ENGINE_NOTE not in again.Checks()[0][1]
	saved = json.load(open(os.path.join(session.StatusDir(), livemodels.REMEMBER)))
	assert list(saved.values())[0]["instance"] == newi
	# Gone since: the config's pair again
	os.remove(os.path.join(newi, "best.ckpt"))
	os.remove(os.path.join(newi, "training_config.yaml"))
	os.rmdir(newi)
	assert livemodels.Picker(live_config(tmp_path, centroid=c, instance=i)).source == "the config's"


# ------------------------------------------------------------------ the pick in the recording

def test_the_pick_goes_into_the_recordings_config_and_campy_live(tmp_path, home):
	root, c, i = models_dir(tmp_path)
	newi = model(root / "new_instance", "centered_instance", crop=256)
	path = live_config(tmp_path, centroid=c, instance=i).path
	config = session.Config(path, live={"centroid": c, "instance": newi})
	assert config.live["instance"] == newi and config.live["centroid"] == c
	text = config.resolved.ToYaml()
	assert "  instance: {}  # panel (picked on campy-session panel's page)".format(yaml.safe_dump([newi],
		default_flow_style=True).strip()[1:-1]) in text
	assert newi in config.AcquireYaml() # campy-acquire's config (session.json's), resolved again with liveFeed on
	s = session.Session(config, live_python=sys.executable)
	s.folder, s.logs = str(tmp_path / "rec"), str(tmp_path / "rec" / "logs")
	command = s.LiveCommand(1)
	assert command[command.index("--instance") + 1] == newi
	assert s.StatusDict()["live_models"] == {"centroid": c, "instance": newi, "picked": True}
	# The config itself is as it was
	assert newi not in open(path).read()


def test_a_config_with_sets_but_no_pair_is_the_panels_alone(tmp_path, home):
	root, c, i = models_dir(tmp_path)
	config = live_config(tmp_path, sets={"rig": {"centroid": c, "instance": i}})
	with pytest.raises(session.SessionError, match="campy-session run needs the two folders"):
		session.Session(config, live_python=sys.executable).Steps()
	with pytest.raises(session.SessionError, match="live.sets.rig: give it centroid: and instance:"):
		live_config(tmp_path, sets={"rig": {"centroid": c}})
	with pytest.raises(session.SessionError, match="give the trained models' folders"):
		live_config(tmp_path)


def picker_panel(tmp_path, monkeypatch, **live):
	# A panel whose live environment's check passes; nothing records
	monkeypatch.setattr(session, "CheckLiveEnv", lambda python, env=None: {"torch": True, "sleap_nn": True,
		"sleap_nn_version": session.LIVE_SLEAP_NN})
	path = live_config(tmp_path, **live).path
	return panel.Panel(path, browser=False)


def test_the_panel_offers_the_models_and_hands_the_pick_to_the_recording(tmp_path, home, monkeypatch):
	root, c, i = models_dir(tmp_path)
	newi = model(root / "new_instance", "centered_instance", crop=256)
	p = picker_panel(tmp_path, monkeypatch, centroid=c, instance=i, models=[str(root)])
	status = p.Status()
	assert status["models"]["pick"] == {"centroid": c, "instance": i} and status["models"]["scanned"] == 3
	assert [x["text"] for x in status["checks"]][1].startswith("Live models (the config's)")
	assert p.Models({"centroid": c, "instance": newi}) is None
	assert p.Status()["models"]["set"] is None and "instance new_instance" in p.Status()["checks"][1]["text"]
	assert p.Models({"centroid": "/etc"}).startswith("Not one of the picker's models")
	# Start: the recording's config has the pick, and the next panel opens on it
	made = []
	class FakeSession:
		def __init__(self, config, **kw):
			made.append(config)
			self.folder = None
		def Run(self):
			return 0
	monkeypatch.setattr(session, "Session", FakeSession)
	p.wanted = {}
	p.Record()
	assert made[0].live["instance"] == newi and made[0].picked == {"centroid": c, "instance": newi}
	assert picker_panel(tmp_path, monkeypatch, centroid=c, instance=i, models=[str(root)]).picker.source == "last used"
	# While it records, no change
	p.session = types.SimpleNamespace(state="recording")
	assert p.Models({"set": "config"}).startswith("A recording is running")


def test_no_picker_without_trained_models(tmp_path, home, monkeypatch):
	p = picker_panel(tmp_path, monkeypatch, model="random")
	assert p.picker is None and p.Status()["models"] is None
	assert p.Models({}) == "This config has no trained models to pick (live: model: sleap)."
