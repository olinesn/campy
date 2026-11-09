"""campy-train-check (campy/traincheck.py) on made-up sleap-nn configs and label stats."""

import collections, os

import numpy as np
import pytest
import yaml

from campy import traincheck

NODES = ["Nose", "Trunk", "TailTip"]


def config(**over):
	cfg = {
		"data_config": {"data_pipeline_fw": "torch_dataset", "validation_fraction": 0.1,
			"preprocessing": {"max_height": 1024, "max_width": 1280, "scale": 1.0, "crop_size": 450}},
		"model_config": {"head_configs": {"centroid": None, "centered_instance": {"confmaps": {"anchor_part": "Trunk"}}}},
		"trainer_config": {"train_data_loader": {"batch_size": 4, "num_workers": 12},
			"val_data_loader": {"batch_size": 4, "num_workers": 12}, "train_steps_per_epoch": 2000, "max_epochs": 200,
			"early_stopping": {"stop_training_on_plateau": True, "min_delta": 3e-7, "patience": 10}},
	}
	for path, value in over.items():
		node = cfg
		keys = path.split("__")
		for k in keys[:-1]:
			node = node[k]
		node[keys[-1]] = value
	return cfg


def mouse(x, y, tail=60):
	return np.array([[x, y - 40], [x, y], [x, y + tail]], float)


def stats(sizes, animals):
	"""sizes: {(h, w): frames}; animals: [(size, points)]."""
	return dict(frames=sum(sizes.values()), instances=len(animals), sizes=collections.Counter(sizes),
		points=animals, nodes=NODES)


def levels(out):
	return {text.split(" ")[0].split(":")[0]: level for level, text in out}


def warned(out, word):
	return [t for l, t in out if l != traincheck.OK and word in t]


def test_a_good_config_is_all_clear():
	s = stats({(1024, 1280): 100}, [((1024, 1280), mouse(500, 500))] * 120)
	out = traincheck.Check(config(), s, windows=True, mem_free=100e9)
	assert all(level == traincheck.OK for level, _ in out), out


def test_no_workers_fails_and_few_validation_workers_warn():
	out = traincheck.Check(config(trainer_config__train_data_loader__num_workers=0), windows=False)
	assert any(l == traincheck.FAIL and "train_data_loader.num_workers is 0" in t for l, t in out)
	out = traincheck.Check(config(trainer_config__val_data_loader__num_workers=4), windows=False)
	assert warned(out, "val_data_loader.num_workers is 4 against 12")


def test_memory_cache_that_wont_fit_warns_about_the_jpeg_switch():
	s = stats({(1024, 1280): 30000}, [])
	cfg = config(data_config__data_pipeline_fw="torch_dataset_cache_img_memory")
	out = traincheck.Check(cfg, s, windows=True, mem_free=113e9)
	(text,) = warned(out, "Cache in Memory needs")
	assert "JPEG" in text and "once per worker on Windows" in text and "511 GB" in text # 39.3 GB x 13 copies
	assert not warned(traincheck.Check(cfg, s, windows=False, mem_free=113e9), "Cache in Memory")
	disk = traincheck.Check(config(data_config__data_pipeline_fw="torch_dataset_cache_img_disk"), windows=False)
	assert warned(disk, "JPEG files")


def test_empty_max_size_warns_when_frames_get_enlarged():
	s = stats({(1024, 1280): 97, (1536, 2048): 3}, [((1024, 1280), mouse(500, 500))])
	cfg = config(data_config__preprocessing__max_height=None, data_config__preprocessing__max_width=None)
	(text,) = warned(traincheck.Check(cfg, s, windows=False), "max_height")
	assert "1536x2048" in text and "1024x1280 (97%)" in text and "enlarged 1.50 times" in text
	assert "max_height: 1024, max_width: 1280" in text
	same = stats({(1024, 1280): 10}, [])
	assert not warned(traincheck.Check(cfg, same, windows=False), "max_height")


def test_empty_steps_per_epoch_counts_animals_for_a_centered_instance_model():
	s = stats({(1024, 1280): 30000}, [((1024, 1280), mouse(500, 500))] * 60000)
	out = traincheck.Check(config(trainer_config__train_steps_per_epoch=None), s, windows=False)
	(text,) = warned(out, "train_steps_per_epoch is empty")
	assert "13,500 steps" in text # 60,000 animals x 0.9 / batch 4
	centroid = config(trainer_config__train_steps_per_epoch=None)
	centroid["model_config"]["head_configs"] = {"centroid": {"confmaps": {}}, "centered_instance": None}
	ok = [t for l, t in traincheck.Check(centroid, s, windows=False) if "6,750 steps" in t] # 30,000 frames
	assert ok


def test_validation_longer_than_training_warns():
	s = stats({(1024, 1280): 30000}, [])
	cfg = config(trainer_config__train_steps_per_epoch=200)
	cfg["model_config"]["head_configs"] = {"centroid": {"confmaps": {}}}
	assert warned(traincheck.Check(cfg, s, windows=False), "validation is 750 steps against 200")


def test_early_stopping_that_counts_any_change_warns_and_suggests_from_an_earlier_run(tmp_path):
	cfg = config(trainer_config__early_stopping={"stop_training_on_plateau": True, "min_delta": 1e-8, "patience": 20})
	(text,) = warned(traincheck.Check(cfg, windows=False), "min_delta is 1e-08")
	assert "--like" in text
	log = tmp_path / "training_log.csv"
	log.write_text("epoch,val/loss\n0,1e-3\n1,7e-5\n2,6.4e-5\n3,nan\n")
	loss = traincheck.BestLoss(str(tmp_path))
	assert loss == pytest.approx(6.4e-5)
	(text,) = warned(traincheck.Check(cfg, windows=False, like_loss=loss), "min_delta is 1e-08")
	assert "about 3e-07" in text
	off = config(trainer_config__early_stopping={"stop_training_on_plateau": False})
	assert warned(traincheck.Check(off, windows=False), "early stopping is off")


def test_crop_clipping_counts_animals_reaching_outside_the_crop():
	inside = ((1024, 1280), mouse(500, 500, tail=100))
	long_tail = ((1024, 1280), mouse(500, 500, tail=300)) # tip 300 px from the trunk, past a 450 px crop's 225
	s = stats({(1024, 1280): 10}, [inside] * 9 + [long_tail])
	(text,) = [t for l, t in traincheck.Check(config(), s, windows=False) if t.startswith("crop 450")]
	assert "1 of 10 labelled animals (10.0%)" in text and "TailTip (1)" in text
	# Enlarged 1.5 times (max size from a bigger frame), the crop covers 300 px: a 160 px tail reaches outside it
	enlarged = stats({(1024, 1280): 10}, [((1024, 1280), mouse(500, 500, tail=160))] * 10)
	cfg = config(data_config__preprocessing__max_height=1536, data_config__preprocessing__max_width=2048)
	(text,) = [t for l, t in traincheck.Check(cfg, enlarged, windows=False) if t.startswith("crop 450")]
	assert "10 of 10" in text
	# A hidden anchor: the crop centres on the points' middle instead
	hidden = mouse(500, 500, tail=300)
	hidden[1] = np.nan
	s = stats({(1024, 1280): 1}, [((1024, 1280), hidden)])
	(text,) = [t for l, t in traincheck.Check(config(), s, windows=False) if t.startswith("crop 450")]
	assert "0 of 1" in text # middle at y 630: nose 170 px above, tail 170 below


def test_existing_run_folder_fails(tmp_path):
	(tmp_path / "run1").mkdir()
	cfg = config(trainer_config__ckpt_dir=str(tmp_path), trainer_config__run_name="run1")
	assert any(l == traincheck.FAIL and "already exists" in t for l, t in traincheck.Check(cfg, windows=False))


def test_main_exit_codes(tmp_path, capsys):
	good, bad = tmp_path / "good.yaml", tmp_path / "bad.yaml"
	good.write_text(yaml.safe_dump(config()))
	bad.write_text(yaml.safe_dump(config(trainer_config__train_data_loader__num_workers=0)))
	assert traincheck.Main([str(good), "--no-labels"]) == 0
	assert traincheck.Main([str(good), str(bad), "--no-labels"]) == 2
	(tmp_path / "x.yaml").write_text("not: a config\n")
	assert traincheck.Main([str(tmp_path / "x.yaml")]) == 3
	out = capsys.readouterr().out
	assert "All clear." in out and "Failures above" in out
