"""
campy-live-view's checks (campy/livehealth.py): of each camera's live feed
(its frames' numbers and times, as its ring holds them), and of campy-live's
tracking. The rings are made up here: each camera's frames, as the view reads
them every 50 ms (8 slots); tests/test_liveview.py checks the same on real
rings, and what the view draws of it.
"""

import numpy as np

from campy import live, livehealth

P = 33_333_333 # ns: 30 fps
T0 = 10 ** 12


def frames(n=300, period=P, ahead=None, lose=None, late=None, missing=None, slow=None, stop=None):
	"""
	3 cameras' frames 1..n: {camera: [(number, host ns, stamp s, written ns)]}. ahead: a camera whose
	frames are one pulse early (it started a frame ahead); lose: (camera, frame) the camera dropped there,
	numbering its later frames one lower; late: (camera, frame) reaching the computer 2 frames late;
	missing: (camera, frame) never in its feed; slow: a camera at 25 fps; stop: (camera, frame) its last.
	"""
	out = {}
	for c in range(3):
		got = []
		for k in range(1, n + 1):
			pulse = k
			if ahead == c:
				pulse = k - 1
			if lose and lose[0] == c and k >= lose[1]:
				pulse = k + 1
			step = period * 30 / 25 if slow == c else period
			written = T0 + int(pulse * step) + 1_000_000
			if late and late == (c, k):
				written += 2 * period
			if missing and missing == (c, k):
				continue
			if stop and stop[0] == c and k > stop[1]:
				break
			if got:
				written = max(written, got[-1][3] + 1000) # campy writes the frames in turn: after a late one, at once
			got.append((k, written - 1_000_000, pulse * step / 1e9, written))
		out[c] = got
	return out


def sampled(feeds, every=50_000_000, slots=8, until=None):
	# A Rig that read each camera's ring every 50 ms: the newest `slots` frames written by then
	rig = livehealth.Rig(["Camera{}".format(c) for c in feeds], 30.0, slots)
	end = until or max(f[-1][3] for f in feeds.values())
	t = T0
	while t <= end:
		for c, f in feeds.items():
			there = [x for x in f if x[3] <= t]
			if not there:
				continue
			latest = there[-1][0]
			held = [x for x in there if x[0] > latest - slots]
			rig.Add(c, *zip(*held), latest=latest)
		t += every
	rig.sampledAt = t - every
	return rig


def problems(status):
	return [[tag for tag, _ in s.problems] for s in status.cameras]


def test_cameras_that_keep_up_together_are_fine():
	status = sampled(frames()).Status()
	assert problems(status) == [[], [], []]
	assert all(abs(s.fps - 30) < 0.2 for s in status.cameras)
	assert all(abs(s.offset_ms) < 1 for s in status.cameras)
	assert abs(status.period - 1 / 30) < 1e-4
	assert abs(status.recording_s - 299 / 30) < 0.05 # frame 1 to frame 300


def test_a_camera_one_frame_ahead_is_warned_of():
	# As on 2026-10-20: a camera left grabbing started the next recording a frame ahead, 33 ms early
	status = sampled(frames(ahead=2)).Status()
	assert problems(status) == [[], [], ["1 FRAME AHEAD"]]
	assert status.cameras[2].frames_off == 1 and abs(status.cameras[2].offset_ms + 33.3) < 1
	assert "1 frame ahead" in status.cameras[2].problems[0][1]


def test_a_lost_frame_is_counted_and_the_camera_is_then_a_frame_behind():
	status = sampled(frames(lose=(1, 250))).Status()
	assert status.cameras[1].lost == 1 and status.cameras[1].totals["lost"] == 1
	assert problems(status)[1] == ["1 FRAME BEHIND", "LOST 1"]
	assert problems(status)[0] == problems(status)[2] == []


def test_a_frame_missing_from_the_feed_is_a_gap():
	status = sampled(frames(missing=(0, 200))).Status()
	assert status.cameras[0].gaps == 1 and problems(status) == [["GAP 1"], [], []]


def test_a_late_frame_is_counted():
	status = sampled(frames(late=(2, 280))).Status()
	assert status.cameras[2].late == 1 and problems(status) == [[], [], ["LATE 1"]]


def test_a_camera_behind_or_stopped_is_warned_of():
	status = sampled(frames(slow=1), until=T0 + 300 * P + 2_000_000).Status() # as the others end
	assert problems(status)[1][0].endswith("FPS") and abs(status.cameras[1].fps - 25) < 0.5
	assert problems(status)[0] == problems(status)[2] == []
	status = sampled(frames(stop=(0, 250))).Status()
	assert problems(status)[0] == ["NO FRAMES"] and status.cameras[0].quiet_s > 1.5


def test_warnings_go_after_10_s():
	feeds = frames(n=900, missing=(0, 200))
	assert sampled(feeds, until=T0 + 400 * P).Status().cameras[0].gaps == 1
	status = sampled(feeds).Status() # 23 s after it
	assert status.cameras[0].gaps == 0 and status.cameras[0].totals["gaps"] == 1 and problems(status) == [[], [], []]


def test_a_view_that_came_late_reads_the_recording_time_from_the_frame_number():
	feeds = {c: f[100:] for c, f in frames().items()}
	assert abs(sampled(feeds).Status().recording_s - 299 / 30) < 0.05


def rows(n=60, errors=(3.0, 4.0, 5.0), views=3, animals=3):
	out = []
	for k in range(1, n + 1):
		row = np.zeros((), live.RowType(3, animals, 4, animals))
		row["frame"] = k
		row["n_views"] = views
		row["reproj_px"] = np.asarray(errors, float)[:, None]
		row["points3d"] = 1.0
		out.append(row)
	return out


def test_tracking_alerts_when_too_few_identities_have_good_3d():
	tracking = livehealth.Tracking(3, 30.0, 20.0)
	for row in rows():
		tracking.Add(row)
	assert tracking.Alerts() == []
	tracking = livehealth.Tracking(3, 30.0, 20.0)
	for row in rows(errors=(3.0, 4.0, 234.0)):
		tracking.Add(row)
	alerts = tracking.Alerts()
	assert alerts[0] == "Good 3D for 2 of 3 animals: #2 error 234 px"
	assert alerts[1].startswith("#2's error, 234 px, is far above the others' (4 px)")
	tracking = livehealth.Tracking(3, 30.0, 20.0)
	for row in rows():
		row["n_views"][1] = 1 # seen by one camera: no 3D
		tracking.Add(row)
	assert tracking.Alerts() == ["Good 3D for 2 of 3 animals: #1 not found in 3D"]


def test_tracking_dims_by_the_median_over_the_last_half_second():
	# One row's error crossed --bad-px 100-180 times a minute on 2026-10-20's recordings: the view dims an
	# identity while its error's median over the last DIM_S (15 rows at 30 fps) is above it
	tracking = livehealth.Tracking(3, 30.0, 20.0)
	assert tracking.Dimmed() is None # no row yet
	def take(errors, start):
		dimmed = []
		for k, row in enumerate(rows(n=len(errors)), start):
			row["frame"] = k
			row["reproj_px"] = np.asarray(errors[k - start], float)[:, None]
			tracking.Add(row)
			dimmed.append(tracking.Dimmed())
		return dimmed
	# #2 bad a row in 3 (runs of one frame): never dimmed, though a third of its rows are above 20 px
	assert all(d == {} for d in take([(3.0, 4.0, 234.0 if k % 3 == 2 else 5.0) for k in range(60)], 1))
	# Bad from frame 91 on: dimmed once 8 of the last 15 rows are (frame 98, a quarter of a second later), with
	# the median's px for its label
	take([(3.0, 4.0, 5.0)] * 30, 61)
	dimmed = take([(3.0, 4.0, 234.0)] * 30, 91)
	assert [k for k, d in enumerate(dimmed, 91) if d] == list(range(98, 121)) and dimmed[-1] == {2: 234.0}
	# Good again from frame 121: one good row does not undim it; the 8th does
	dimmed = take([(3.0, 4.0, 5.0)] * 30, 121)
	assert [k for k, d in enumerate(dimmed, 121) if d] == list(range(121, 128))
	# At an earlier frame, the half second before it; a frame it never took: None (the row alone tells)
	assert tracking.Dimmed(100) == {2: 234.0} and tracking.Dimmed(130) == {} and tracking.Dimmed(999) is None
	# No 3D in the window: not dimmed; --bad-px 0: none
	take([(3.0, 4.0, np.nan)] * 20, 151)
	assert tracking.Dimmed() == {}
	tracking = livehealth.Tracking(3, 30.0, 0)
	take([(3.0, 4.0, 234.0)] * 30, 1)
	assert tracking.Dimmed() == {}


def test_tracking_keeps_the_last_10_s():
	tracking = livehealth.Tracking(3, 30.0, 20.0)
	for row in rows(n=600):
		tracking.Add(row)
	frames, err, views = tracking.Arrays()
	assert frames[0] == 600 - 300 and frames[-1] == 600 and err.shape == (301, 3) and (views == 3).all()
	# A new recording (frames from 1 again): the last one's rows go
	for row in rows(n=5):
		tracking.Add(row)
	assert list(tracking.Arrays()[0]) == [1, 2, 3, 4, 5]
