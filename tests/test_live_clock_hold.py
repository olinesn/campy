"""
campy-live --hold-gpu-clock-blocks auto on an AMD integrated GPU (the
EVO-X2's): at least one block keeps spinning while sets come (GpuClock's
fewest), where elsewhere auto goes down to none at full clock. No GPU needed:
torch is a stand-in.
"""
import types

from campy import live


def fake_torch(hip, integrated):
	props = types.SimpleNamespace(is_integrated=integrated)
	return types.SimpleNamespace(version=types.SimpleNamespace(hip=hip),
		device=lambda name: name, cuda=types.SimpleNamespace(get_device_properties=lambda device: props))


def test_fewest_is_one_on_an_amd_integrated_gpu_only():
	assert live.GpuClock.Fewest(fake_torch("6.4", True), "cuda") == 1 # EVO-X2: ROCm, integrated
	assert live.GpuClock.Fewest(fake_torch("6.4", False), "cuda") == 0 # an AMD card
	assert live.GpuClock.Fewest(fake_torch(None, True), "cuda") == 0 # NVIDIA (integrated, as a Jetson)
	assert live.GpuClock.Fewest(fake_torch(None, False), "cuda") == 0 # NVIDIA
	assert live.GpuClock.Fewest(types.SimpleNamespace(), "cuda") == 0 # can't tell: as before


def clock(monkeypatch, hip, integrated, blocks="auto"):
	monkeypatch.setitem(__import__("sys").modules, "torch", fake_torch(hip, integrated))
	c = live.GpuClock("cuda", blocks, reader=types.SimpleNamespace(name="fake"))
	c.fastest = 2900.0
	return c


def calm_looks(c, seconds):
	# Adjust on a full clock, one look every LOOK_S: the blocks after each
	state, t, blocks = (0, 0.0, None), 0.0, []
	for _ in range(int(seconds / c.LOOK_S)):
		t += c.LOOK_S
		state = c.Adjust(2850.0, False, *state, t)
		blocks.append(c.busy)
	return blocks


def test_auto_keeps_one_block_on_an_amd_integrated_gpu(monkeypatch):
	c = clock(monkeypatch, "6.4", True)
	assert c.fewest == 1 and c.busy == 1
	assert calm_looks(c, 10 * c.CALM_S) == [1] * int(10 * c.CALM_S / c.LOOK_S) # never 0, never a change
	assert c.changes == 0
	# a slow clock still adds the second block, and calm takes it away again, down to 1
	state = c.Adjust(1500.0, False, 0, 0.0, None, 0.5)
	c.Adjust(1500.0, False, *state, 1.0)
	assert c.busy == 2
	assert calm_looks(c, 2 * c.CALM_S)[-1] == 1
	assert c.Describe()["fewest_blocks"] == 1


def test_auto_still_goes_down_to_none_elsewhere(monkeypatch):
	for hip, integrated in (("6.4", False), (None, False)):
		c = clock(monkeypatch, hip, integrated)
		assert c.fewest == 0 and calm_looks(c, c.CALM_S)[-1] == 0
		assert "fewest_blocks" not in c.Describe()


def test_a_number_still_decides(monkeypatch):
	for n in ("0", "2"):
		c = clock(monkeypatch, "6.4", True, blocks=n)
		assert c.fewest == 0 and c.busy == int(n) and not c.auto


def test_a_spin_is_2_ms_on_rocm_too(monkeypatch):
	# ROCm's _sleep counts a fixed 100 MHz clock: CYCLES there would spin
	# 50 ms, and the feed's first set waited for one to end
	assert clock(monkeypatch, "6.4", True).CYCLES == live.GpuClock.ROCM_CYCLES == 200000
	assert clock(monkeypatch, None, False).CYCLES == live.GpuClock.CYCLES == 5000000


def test_the_waiting_blocks_spin_from_start_till_the_wait_is_too_long(monkeypatch):
	# Start comes as the warm-up ends, and the feed may be late or never come
	c = clock(monkeypatch, None, False)
	now = [1000.0]
	monkeypatch.setattr(live.time, "monotonic", lambda: now[0])
	c.started = now[0]
	assert c.Spinning() == c.WAITING_BLOCKS
	now[0] += c.WAITING_MOST_S - 1
	assert c.Spinning() == c.WAITING_BLOCKS
	now[0] += 1
	assert c.Spinning() == 0
	assert c.FeedCame() is True # said: nothing was spinning when it came
	assert c.Spinning() == c.WAITING_BLOCKS # and they spin again, for the first frames
	c.Busy()
	assert c.Spinning() == c.busy
	now[0] += c.WAITING_S
	assert c.Spinning() == c.WAITING_BLOCKS # a pause in the frames: as before
	assert c.Describe()["held_before_the_feed_s"] == c.WAITING_MOST_S


def test_a_feed_in_time_keeps_the_blocks_spinning_past_the_limit(monkeypatch):
	c = clock(monkeypatch, None, False)
	now = [1000.0]
	monkeypatch.setattr(live.time, "monotonic", lambda: now[0])
	c.started = now[0]
	now[0] += 5
	assert c.FeedCame() is False
	now[0] += 10 * c.WAITING_MOST_S
	assert c.Spinning() == c.WAITING_BLOCKS
	assert c.Describe()["held_before_the_feed_s"] == 5.0
