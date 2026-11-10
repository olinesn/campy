"""
campy/prefetch.py: the list of files a run read, written for the next run,
and read back many at a time at its start (campy-infer --read-ahead).
"""

import os
import time

from campy import infer, prefetch


def Wait(reading):
	for _ in range(500):
		n, size, seconds = reading.Done()
		if seconds is not None:
			return n, size
		time.sleep(0.01)
	raise AssertionError("still reading")


def test_the_list_names_this_runs_modules_and_the_extra_files(tmp_path):
	plan = tmp_path / "instance_0123.plan"
	plan.write_bytes(b"x" * 1000)
	out = prefetch.Record(str(tmp_path / "cache"), [str(plan), str(tmp_path / "gone.plan")])
	assert out and os.path.dirname(out) == str(tmp_path / "cache" / "prefetch")
	listed = [line.rstrip("\n") for line in open(out) if not line.startswith("#")]
	assert os.path.realpath(prefetch.__file__) in listed # a module this process imported
	assert os.path.join(os.path.realpath(os.path.dirname(prefetch.__file__)), "") in listed # its folder
	assert os.path.realpath(str(plan)) in listed
	assert not any(p.endswith("gone.plan") for p in listed) # a file that isn't there is left out
	assert len(listed) == len(set(listed))


def test_the_next_run_reads_them_and_passes_over_what_is_gone(tmp_path):
	files = []
	for i in range(40):
		f = tmp_path / "m{}.py".format(i)
		f.write_bytes(b"#" * (1000 + i))
		files.append(str(f))
	cache = str(tmp_path / "cache")
	out = prefetch.ListPath(cache)
	os.makedirs(os.path.dirname(out))
	with open(out, "w") as f:
		f.write("# a comment\n")
		f.writelines(p + "\n" for p in files + [str(tmp_path / "gone.py"), str(tmp_path) + os.sep])
	ckpt = tmp_path / "best.ckpt"
	ckpt.write_bytes(b"w" * 5000)
	reading = prefetch.Start(cache, [str(ckpt), files[0]], threads=4)
	n, size = Wait(reading)
	assert len(reading.files) == 43 # the checkpoint, the 40 modules once each, the gone one, the folder
	assert n == 42 # all but the gone one
	assert size == 5000 + sum(1000 + i for i in range(40))


def test_no_list_and_nothing_else_is_nothing_to_read(tmp_path):
	assert prefetch.Start(str(tmp_path)) is None
	reading = prefetch.Start(str(tmp_path), [str(tmp_path / "gone.ckpt")])
	assert Wait(reading) == (0, 0)


def test_a_big_library_the_run_used_little_of_is_not_read_ahead(tmp_path, monkeypatch):
	libs = {}
	for name, size, used in (("libbuilder.so", 40 << 20, 0), ("libused.so", 40 << 20, 30 << 20),
			("libsmall.so", 1 << 20, 0)):
		f = tmp_path / name
		with open(str(f), "wb") as h:
			h.truncate(size)
		libs[os.path.realpath(str(f))] = (used, size)
	monkeypatch.setattr(prefetch, "Mapped", lambda: dict(libs))
	out = prefetch.Record(str(tmp_path / "cache"))
	text = open(out).read()
	listed = [line for line in text.splitlines() if not line.startswith("#")]
	assert os.path.realpath(str(tmp_path / "libused.so")) in listed
	assert os.path.realpath(str(tmp_path / "libsmall.so")) in listed # small: read whole either way
	assert os.path.realpath(str(tmp_path / "libbuilder.so")) not in listed
	assert "# not read ahead: {} (42 MB, 0 MB of it used)".format(os.path.realpath(str(tmp_path / "libbuilder.so"))) \
		in text


def test_an_unchanged_list_is_not_written_again(tmp_path):
	cache = str(tmp_path / "cache")
	out = prefetch.Record(cache)
	before = os.stat(out).st_mtime_ns
	os.utime(out, ns=(before - 10 ** 9, before - 10 ** 9))
	assert prefetch.Record(cache) == out
	assert os.stat(out).st_mtime_ns == before - 10 ** 9


def test_model_files_are_the_checkpoints_and_configs_not_the_labels(tmp_path):
	for name in ("best.ckpt", "training_config.yaml", "initial_config.yaml", "labels_gt.train.0.slp",
			"metrics.val.0.json", "training_log.csv"):
		(tmp_path / name).write_bytes(b"")
	names = sorted(os.path.basename(p) for p in prefetch.ModelFiles([str(tmp_path), str(tmp_path / "gone")]))
	assert names == ["best.ckpt", "initial_config.yaml", "training_config.yaml"]


def test_campy_infer_reads_ahead_from_its_engine_cache_unless_told_not_to(tmp_path, monkeypatch):
	started = []
	monkeypatch.setattr(prefetch, "Start", lambda folder, also=(), threads=32: started.append((folder, list(also))))
	(tmp_path / "a.mp4").write_bytes(b"")
	(tmp_path / "a.predictions.slp").write_bytes(b"") # skipped: nothing to run
	argv = [str(tmp_path / "a.mp4"), "--centroid", str(tmp_path), "--instance", "i", "--max-instances", "5",
		"--engine-cache", str(tmp_path / "engines")]
	infer.Main(argv)
	assert started == [(str(tmp_path / "engines"), [])]
	infer.Main(argv + ["--read-ahead", "off"])
	assert len(started) == 1
	assert infer.EngineCache(infer.Parser().parse_args(["v", "--centroid", "c", "--instance", "i",
		"--max-instances", "1"])) == os.path.expanduser(os.path.join("~", ".cache", "campy", "engines"))


def test_a_video_is_read_from_start_to_end_up_to_the_cap(tmp_path, monkeypatch):
	video = tmp_path / "0.mp4"
	video.write_bytes(b"v" * (20 << 20))
	assert Wait(prefetch.Video(str(video))) == (1, 20 << 20)
	monkeypatch.setattr(prefetch, "VIDEO_BYTES", 9 << 20)
	n, size = Wait(prefetch.Video(str(video)))
	assert n == 1 and 9 << 20 <= size < (9 << 20) + (8 << 20) # whole 8 MB reads, stopping once past the cap
