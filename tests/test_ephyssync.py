"""
SpikeGLX on another computer (campy/spikeglx.py's host, data_dir, trigger
immediate, copy_from; the stand-in SDK tests/fake_sglx), and campy-ephyssync
(campy/ephyssync.py) on made-up recordings: an NI file whose digital word
carries the camera pulses (and SpikeGLX's sync wave), with cameras aligned by
campy-align's files.
"""

import csv, json, os

import numpy as np
import pytest

from campy import devices, ephyssync, spikeglx
from test_devices import FAKE_SDK, a_sglx, section, set_state, sglx_state, state # noqa: F401 (sglx_state: a fixture)


# ------------------------------------------------------------------ SpikeGLX on another computer

def test_another_computer_needs_its_data_folder():
	with pytest.raises(spikeglx.SpikeGLXError, match="data_dir"):
		a_sglx(host="192.0.2.10")
	with pytest.raises(spikeglx.SpikeGLXError, match="copy_from is for SpikeGLX on another computer"):
		a_sglx(copy_from="//pc/SGL_DATA")
	with pytest.raises(spikeglx.SpikeGLXError, match="ttl or immediate"):
		a_sglx(trigger="gate")


def test_immediate_trigger_on_another_computer(tmp_path, sglx_state):
	s = a_sglx(host="192.0.2.10", data_dir="D:/SGL_DATA", trigger="immediate", stream=[0, 0], camera_bit=2)
	with pytest.raises(spikeglx.SpikeGLXError, match="isn't Immediate"):
		s.Check() # the fake SpikeGLX's trigger is TTL
	set_state(sglx_state, trigMode="0")
	assert s.Check() == "SpikeGLX on 192.0.2.10: idle, Immediate trigger (records from recording enabled to its run's stop)"
	said = s.Start(str(tmp_path / "rec" / "spikeglx"), "dp_2026-11-24_05-30-00")
	now = state(sglx_state)
	assert now["dir"] == "D:/SGL_DATA" and now["run"] == "dp_2026-11-24_05-30-00" and now["enabled"]
	assert not (tmp_path / "rec").exists() # nothing made on this computer
	assert said.endswith("on 192.0.2.10.")
	rec = s.record
	assert rec["trigger"] == "immediate" and rec["cameraBit"] == 2 and rec["dataDir"] == "D:/SGL_DATA"
	en = rec["recordingEnabled"]
	assert en["sample"] > 2 * 30303 * 0.9 and en["roundTripNs"] >= 0 and en["hostNs"] > 0
	s.Stop()
	assert rec["beforeStop"]["sample"] >= en["sample"] and rec["beforeStop"]["hostNs"] >= en["hostNs"]
	(tmp_path / "rec").mkdir()
	assert s.Write(str(tmp_path / "rec")) is None
	written = json.loads((tmp_path / "rec" / "spikeglx.json").read_text())
	assert written["run"] == "dp_2026-11-24_05-30-00" and "stoppedEpochNs" in written


def test_its_run_is_not_opened_from_here(sglx_state):
	set_state(sglx_state, open=False)
	s = a_sglx(host="192.0.2.10", data_dir="D:/SGL_DATA", trigger="immediate", exe="C:/SpikeGLX/SpikeGLX.exe")
	with pytest.raises(spikeglx.SpikeGLXError, match="open it there, with its command server on"):
		s.Open()


def test_the_run_folder_is_copied_and_checked(tmp_path, sglx_state):
	share = tmp_path / "share"
	run = share / "dp_x_g0"
	(run / "dp_x_g0_imec0").mkdir(parents=True)
	(run / "dp_x_g0_t0.nidq.bin").write_bytes(b"\x00" * 1000)
	(run / "dp_x_g0_t0.nidq.meta").write_text("nSavedChans=1\n")
	(run / "dp_x_g0_imec0" / "dp_x_g0_t0.imec0.ap.bin").write_bytes(b"\x01" * 5000)
	set_state(sglx_state, trigMode="0")
	cfg = types_config(host="192.0.2.10", data_dir="D:/SGL_DATA", trigger="immediate", copy_from=str(share))
	d = devices.Devices(cfg, say=lambda text: None)
	rec = tmp_path / "rec"
	rec.mkdir()
	d.folder = str(rec)
	d.sglx.record = {"run": "dp_x"}
	d.CopySpikeGLX()
	assert (rec / "spikeglx" / "dp_x_g0" / "dp_x_g0_imec0" / "dp_x_g0_t0.imec0.ap.bin").stat().st_size == 5000
	written = json.loads((rec / "spikeglx.json").read_text())
	assert written["copiedFiles"] == 3 and written["copiedBytes"] == 1000 + len("nSavedChans=1\n") + 5000
	d.sglx.record = {"run": "missing"}
	d.CopySpikeGLX()
	assert "isn't there to copy" in json.loads((rec / "spikeglx.json").read_text())["copyError"]


def types_config(**given):
	import types
	sec = section("spikeglx", dict({"sdk": FAKE_SDK, "start_timeout_s": 20}, **given))
	return types.SimpleNamespace(spikeglx=sec, pico=None, windows=None, Value=lambda key, default=None: default)


# ------------------------------------------------------------------ campy-ephyssync

NI_RATE = 25000.0


def write_ni(folder, words, sync=False):
	os.makedirs(folder, exist_ok=True)
	path = os.path.join(folder, "run_g0_t0.nidq.bin")
	words.astype(np.int16).tofile(path)
	meta = {"nSavedChans": 1, "niSampRate": NI_RATE, "snsMnMaXaDw": "0,0,0,1", "snsSaveChanSubset": "all",
		"typeThis": "nidq", "firstSample": 1000, "syncSourceIdx": 3 if sync else 0, "syncNiChanType": 0, "syncNiChan": 1}
	with open(path[:-4] + ".meta", "w") as f:
		f.write("".join("{}={}\n".format(k, v) for k, v in meta.items()))
	return path


def make_recording(tmp_path, fps=60.0, seconds=20.0, ppm_board=35.0, lead=1.5, tail=1.0, cams=None, missed=(),
		cam_ppm=None, drop_first_pulses=0, sync=False):
	"""A recording: the board pulses camera triggers at fps (its clock ppm_board fast of nominal on the NI's),
	SpikeGLX's NI file from lead s before the first pulse to tail s after the last; each camera has a frame per
	trigger except those in missed[name]."""
	rec = tmp_path / "rec"
	rec.mkdir()
	n = int(seconds * fps)
	period = (1 / fps) * (1 - ppm_board * 1e-6)
	rises = lead + np.arange(n) * period
	total = int((rises[-1] + tail) * NI_RATE)
	word = np.zeros(total, np.int16)
	rng = np.random.default_rng(3)
	for i, t in enumerate(rises):
		if i < drop_first_pulses:
			continue
		s0 = int(np.ceil(t * NI_RATE))
		word[s0:s0 + int(0.002 * NI_RATE)] |= 1 << 2 # 2 ms pulses on bit 2
	if sync:
		for t in np.arange(0.3, total / NI_RATE, 0.5):
			s0 = int(np.ceil(t * NI_RATE))
			if int(t / 0.5) % 2 == 0:
				word[s0:s0 + int(0.5 * NI_RATE)] |= 1 << 1
	write_ni(str(rec / "spikeglx" / "run_g0"), word, sync)
	cams = cams or {"Camera0_topB": 8.0, "Camera1_topC": -4.0}
	summary = {}
	for name, ppm in cams.items():
		folder = rec / name
		folder.mkdir()
		trig = np.array([k for k in range(1, n + 1) if k not in set(missed.get(name, ())) if isinstance(missed, dict)]
			if isinstance(missed, dict) else range(1, n + 1))
		frames = np.arange(len(trig))
		# the camera's own clock: its first frame at 0, running ppm fast of the NI's, 1 us jitter
		t_ni = rises[trig - 1]
		t_cam = (t_ni - t_ni[0]) * (1 + ppm * 1e-6) + rng.normal(0, 1e-6, len(trig))
		np.save(folder / "frametimes.npy", np.asfortranarray(np.vstack([frames, t_cam]).astype(np.float64)))
		with open(folder / "aligned.csv", "w", newline="") as f:
			w = csv.writer(f)
			w.writerow(["frame", "trigger", "t_s"])
			for fr, k in zip(frames, trig):
				w.writerow([fr, k, "{:.6f}".format((k - 1) / fps)])
		summary[name] = {"aligned": True, "frames": len(trig)}
	(rec / "alignment.json").write_text(json.dumps({"trigger_period_s": 1 / fps, "cameras": summary}))
	(rec / "spikeglx.json").write_text(json.dumps({"cameraBit": 2, "recordingEnabled": {"hostNs": 10_000_000_000,
		"sample": 5000}, "beforeStop": {"hostNs": 33_000_000_000, "sample": 600_000}}))
	(rec / "trigger.json").write_text(json.dumps({"startedHostNs": 18_000_000_000}))
	return rec, n, rises


def test_every_frame_has_its_pulse(tmp_path):
	rec, n, rises = make_recording(tmp_path)
	out = ephyssync.Check(str(rec), log=lambda text: None)
	assert out["ok"], out["problems"]
	assert out["pulses"] == n == out["pulse_numbers"] == out["triggers_named_by_cameras"]
	b = out["board"]
	assert abs(b["ppm_from_nominal"] - (-35.0)) < 0.5 # the board's period is 35 ppm short: it runs fast
	assert b["residuals"]["max_us"] <= 1e6 / NI_RATE + 1 # each rise within one sample
	assert abs(b["pulse_ms"] - 2.0) < 0.1
	for name, ppm in (("Camera0_topB", 8.0), ("Camera1_topC", -4.0)):
		c = out["cameras"][name]
		assert c["frames"] == n and c["frames_without_pulse"] == 0 and c["triggers_without_frame"] == 0
		assert abs(c["clock"]["ppm_vs_spikeglx"] - ppm) < 0.5
		assert c["clock"]["residuals"]["rms_us"] < 30 # NI sampling (40 us steps) + camera jitter
	m = out["margins"]
	assert abs(m["spikeglx_before_first_pulse_s"] - 1.5) < 1e-3 and abs(m["spikeglx_after_last_pulse_s"] - 1.0) < 1e-3
	assert m["host_enable_to_trigger_start_s"] == 8.0 and m["stream_samples_enable_to_stop"] == 595_000
	with open(rec / "Camera0_topB" / "ephys_times.csv") as f:
		rows = list(csv.DictReader(f))
	assert len(rows) == n and abs(float(rows[10]["ni_s"]) - rises[10]) < 1 / NI_RATE + 1e-9


def test_missed_frames_are_counted_and_lost_pulses_are_problems(tmp_path):
	rec, n, _ = make_recording(tmp_path, missed={"Camera1_topC": [100, 101, 500]})
	out = ephyssync.Check(str(rec), log=lambda text: None)
	assert out["ok"] and out["cameras"]["Camera1_topC"]["triggers_without_frame"] == 3


def test_spikeglx_starting_late_is_a_problem(tmp_path):
	rec, n, _ = make_recording(tmp_path, drop_first_pulses=5, lead=0.05)
	out = ephyssync.Check(str(rec), log=lambda text: None)
	assert not out["ok"]
	assert any("missed the first or last" in p for p in out["problems"])


def test_the_probe_clock_from_spikeglxs_sync(tmp_path):
	rec, n, rises = make_recording(tmp_path, sync=True)
	# The probe's file: 30 kHz, its SY word's bit 6 the same 1 Hz wave on a clock 12 ppm fast, starting 0.2 s earlier
	ap_rate, ppm = 30000.0, 12.0
	folder = rec / "spikeglx" / "run_g0" / "run_g0_imec0"
	folder.mkdir()
	total = int(24 * ap_rate)
	sy = np.zeros((total, 2), np.int16)
	for t in np.arange(0.3, 22, 1.0):
		tp = (t + 0.2) * (1 + ppm * 1e-6)
		s0 = int(np.ceil(tp * ap_rate))
		sy[s0:s0 + int(0.5 * ap_rate), 1] |= 1 << 6
	path = folder / "run_g0_t0.imec0.ap.bin"
	sy.tofile(path)
	(folder / "run_g0_t0.imec0.ap.meta").write_text("nSavedChans=2\nimSampRate=30000\ntypeThis=imec\n")
	out = ephyssync.Check(str(rec), log=lambda text: None)
	p = out["probe_clock"]
	assert abs(p["ppm"] - ppm) < 2 and abs(p["offset_s"] - 0.2) < 1e-3
	with open(rec / "Camera0_topB" / "ephys_times.csv") as f:
		row = list(csv.DictReader(f))[30]
	assert abs(float(row["probe_s"]) - (rises[30] + 0.2) * (1 + ppm * 1e-6)) < 1e-4


def test_campy_session_run_drives_spikeglx_too(sglx_state):
	# campy-session run (not only the panel) checks SpikeGLX first, and refuses to start when it would block
	from campy import session as sessionmod
	set_state(sglx_state, open=False)
	cfg = types_config(host="192.0.2.10", data_dir="D:/SGL_TEST", trigger="immediate")
	with pytest.raises(sessionmod.SessionError, match="not started: .*doesn't answer"):
		sessionmod.RunDevices(cfg)
	set_state(sglx_state, open=True, trigMode="0")
	devs = sessionmod.RunDevices(cfg)
	assert devs is not None and devs.sglx is not None
	none = types_config()
	none.spikeglx = None
	assert sessionmod.RunDevices(none) is None


def test_a_spikeglx_too_old_to_verify_from_here_says_what_to_do(sglx_state):
	# SpikeGLX 20230905 (the DoublePoke rig's) has no SELECTDEVS: freshly opened, it must be verified by hand once
	set_state(sglx_state, validated=False, old=True, trigMode="0")
	s = a_sglx(host="192.0.2.10", data_dir="D:/SGL_DATA", trigger="immediate", devices="(nidq)")
	with pytest.raises(spikeglx.SpikeGLXError, match="SpikeGLX on 192.0.2.10 is a version that can't detect and verify"):
		s.Check()
	assert state(sglx_state)["dir"] == "D:/SGL_DATA" # verifying would have used its own data folder
