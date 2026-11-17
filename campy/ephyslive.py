"""
Live electrophysiology from SpikeGLX: a Neuropixels probe's AP stream, read
while SpikeGLX records, filtered, and its spikes found, for a live view
(the campy-session panel's, next) and campy-ephys, which measures what it
costs.

The pieces, in the order the data goes through them:

- A source: SpikeGLXSource reads the probe's newest samples from SpikeGLX's
  command server (fetch, from where the last read ended, so no sample is
  missed or read twice), with read-only calls only (READ_ONLY): it never
  starts, stops or changes anything in SpikeGLX. ReplaySource reads a saved
  .ap.bin (with its .meta) instead, at the speed it was recorded, or as fast
  as it can.
- Filter: Kilosort 4's high-pass, live: 3rd-order Butterworth at 300 Hz,
  then the median across channels taken off each sample. Kilosort runs its
  filter forwards and backwards (no phase shift), which a live filter can't
  (it would need the future); this one runs forwards only, so a spike's shape
  and trough come out a little different from Kilosort's (the trough up to 3
  samples early). Kilosort takes the median off first, after each channel's
  mean over its 2 s batch; live there is no batch, and the median of raw
  samples jumps by tens of uV from sample to sample with each channel's own
  offset and slow swings, which the high-pass then passes into every
  channel. So here the median comes after the high-pass.
- Detector: a spike is a sample below -threshold x the channel's noise (its
  median absolute value / 0.6745), the lowest on the sites within 50 um of
  it from 1.5 ms before it to 0.5 ms after. (Filtered forwards, a spike swings
  below zero again about 1 ms after its trough, at a third of its depth or
  more: a big unit's would count as a second spike. A smaller spike within
  1.5 ms after a bigger one nearby is lost with it: a live sorter, matching
  templates, can tell them apart.)
  Each spike keeps its waveform on its NEIGHBORS nearest sites on its shank,
  however far (its own first; up to about 60 um on a Neuropixels 1.0 or 2.0
  probe), in Kilosort 4's window: 61 samples, the trough at sample 20.
- A sorter gives each spike its unit. Unsorted, for now, leaves every unit
  at -1 (a threshold crossing). A live sorter takes its place later, with the
  same two methods: templates from a Kilosort 4 run, curated in phy, matched
  to these waveforms.
- Live keeps what a view shows: an envelope (each millisecond's lowest and
  highest value) of a few channels, the last seconds' spikes, rates by
  channel and by unit, and the latest waveforms of each.

Channels are numbered as SpikeGLX numbers the probe's AP channels (0 to 383
on a Neuropixels 2.0 probe), as Kilosort's channel_map is. Spikes carry their
sample number on the probe's stream (SpikeGLX's count since its run started;
a file's first sample is its .meta's firstSample).

The same samples give the same spikes however they come in pieces (the
source's reads are whatever arrived): the filter carries its state from one
piece to the next, the noise is measured on fixed blocks of samples, and a
sample is judged only once its 61-sample window has arrived. So a saved
recording replayed through this gives the spikes the live view showed, which
is what training a live sorter against Kilosort's spikes needs.
"""

import argparse, os, sys, threading, time
from collections import deque

import numpy as np

BEFORE, AFTER = 20, 41 # Kilosort 4's window: nt = 61 samples, the trough at nt0min = 20
NEIGHBORS = 12 # sites kept with each spike, its own first
HIGHPASS_HZ, ORDER = 300.0, 3 # Kilosort 4's highpass_cutoff, and its Butterworth's order
NOISE_BLOCK_S = 0.5 # the noise is measured on blocks of this many seconds, every 8th sample
NOISE_STEP = 8
NOISE_TAU_S = 10.0 # and follows the blocks over about this long

READ_ONLY = frozenset(( # the only SpikeGLX calls SpikeGLXSource makes
	"c_sglx_createHandle", "c_sglx_destroyHandle", "c_sglx_connect", "c_sglx_close", "c_sglx_getError",
	"c_sglx_isRunning", "c_sglx_getStreamSampleRate", "c_sglx_getStreamSampleCount", "c_sglx_getStreamAcqChans",
	"c_sglx_getint", "c_sglx_getstr", "c_sglx_getStreamI16ToVolts", "c_sglx_getGeomMap", "c_sglx_fetch",
	"c_sglx_fetchLatest", "c_sglx_mapSample"))


class EphysError(Exception):
	pass


# ---------------------------------------------------------------- the probe

class Geometry:
	"""Where each channel's site is, in um (x across the probe, shanks side by side; z up from the tip), and which
	are used (a reference site isn't)."""

	def __init__(self, x, z, shank=None, used=None):
		self.x, self.z = np.asarray(x, np.float64), np.asarray(z, np.float64)
		self.shank = np.zeros(len(self.x), int) if shank is None else np.asarray(shank, int)
		self.used = np.ones(len(self.x), bool) if used is None else np.asarray(used, bool)

	def __len__(self):
		return len(self.x)

	@classmethod
	def Linear(cls, n, pitch_um=15.0, columns=(27.0, 59.0)):
		"""A single-shank Neuropixels 2.0 probe's sites: two columns, a row every 15 um."""
		return cls([columns[i % len(columns)] for i in range(n)], [pitch_um * (i // len(columns)) for i in range(n)])

	@classmethod
	def FromPairs(cls, pairs, n):
		"""From SpikeGLX's geomMap (getGeomMap: head_shankPitch, ch5_s, ch5_x, ch5_z, ch5_u, ...)."""
		pitch = float(pairs.get("head_shankPitch", 0) or 0)
		s = [int(float(pairs.get("ch{}_s".format(i), 0))) for i in range(n)]
		x = [float(pairs.get("ch{}_x".format(i), 0)) + pitch * s[i] for i in range(n)]
		z = [float(pairs.get("ch{}_z".format(i), 0)) for i in range(n)]
		u = [int(float(pairs.get("ch{}_u".format(i), 1))) for i in range(n)]
		return cls(x, z, s, u)

	@classmethod
	def FromMeta(cls, meta, n):
		"""From a .meta's snsGeomMap, (NP2014,4,250,70)(0:27:0:1)...: (part, shanks, shank pitch, shank width), then
		(shank:x:z:used) per channel. Without one, a Neuropixels 2.0 single shank's."""
		import re
		entries = re.findall(r"\(([^)]*)\)", meta.get("snsGeomMap", ""))
		if len(entries) < n + 1:
			return cls.Linear(n)
		head = entries[0].split(",")
		pitch = float(head[2]) if len(head) > 2 else 0.0
		rows = [[float(v) for v in e.split(":")] for e in entries[1:n + 1]]
		return cls([r[1] + pitch * r[0] for r in rows], [r[2] for r in rows], [int(r[0]) for r in rows],
			[int(r[3]) if len(r) > 3 else 1 for r in rows])

	def Neighbors(self, radius_um=50.0, count=NEIGHBORS):
		"""[channels, count]: each channel's nearest sites on its shank within radius_um, nearest first (itself
		first), -1 where there are fewer."""
		d = np.hypot(self.x[:, None] - self.x[None, :], self.z[:, None] - self.z[None, :])
		d[self.shank[:, None] != self.shank[None, :]] = np.inf
		d[:, ~self.used] = np.inf
		np.fill_diagonal(d, -1.0) # itself first, even if unused
		order = np.argsort(d, axis=1, kind="stable")[:, :count]
		near = np.take_along_axis(d, order, axis=1) <= radius_um
		out = np.where(near, order, -1)
		if out.shape[1] < count:
			out = np.hstack([out, -np.ones((len(self), count - out.shape[1]), int)])
		return out.astype(np.int32)


class Stream:
	"""What a source gives: its rate, its channels' SpikeGLX numbers, uV per int16 count of each, their geometry."""

	def __init__(self, rate, uv, geometry, channels=None, filtered=False):
		self.rate = float(rate)
		self.uv = np.asarray(uv, np.float32)
		self.geometry = geometry
		self.channels = np.arange(len(self.uv)) if channels is None else np.asarray(channels)
		self.filtered = filtered # SpikeGLX filtered it already (its filtered AP buffer, js=-2)

	def Describe(self):
		return {"rate": self.rate, "channels": len(self.uv), "uv_per_count": float(np.median(self.uv)),
			"x": self.geometry.x.tolist(), "z": self.geometry.z.tolist(), "used": self.geometry.used.tolist()}


# ---------------------------------------------------------------- sources

def ReadMeta(path):
	meta = {}
	with open(path) as f:
		for line in f:
			key, _, value = line.rstrip("\n").partition("=")
			meta[key.lstrip("~")] = value
	return meta


def MetaUv(meta, n):
	"""uV per int16 count of each AP channel, from a probe's .meta: imAiRangeMax / imMaxInt / the AP gain. The gain:
	each channel's from imroTbl where it has one (Neuropixels 1.0: chan bank ref apgain lfgain apfilt), else
	imChan0apGain (a probe whose channels share one gain: 100 on an NP2013, 2048 counts), else 80 (the first
	Neuropixels 2.0 probes, 8192 counts)."""
	import re
	volts = float(meta.get("imAiRangeMax", 0.62)) / float(meta.get("imMaxInt", 8192))
	shared = float(meta.get("imChan0apGain", 80))
	gains = []
	for e in re.findall(r"\(([^)]*)\)", meta.get("imroTbl", ""))[1:]:
		fields = e.split()
		gains.append(float(fields[3]) if len(fields) == 6 else shared)
	gains = (gains + [shared] * n)[:n]
	return np.array([volts / g * 1e6 for g in gains], np.float32)


class ReplaySource:
	"""A saved probe recording (.ap.bin and its .meta), read as if it were arriving: at speed x the rate it was
	recorded at (1: as recorded), or as fast as it can (speed 0: a chunk_s piece at each read)."""

	def __init__(self, bin_path, speed=1.0, chunk_s=0.05, start_s=0.0, loop=False):
		meta = ReadMeta(os.path.splitext(bin_path)[0] + ".meta")
		saved = int(meta["nSavedChans"])
		n = int(meta.get("snsApLfSy", "{},0,1".format(saved - 1)).split(",")[0])
		data = np.memmap(bin_path, np.int16, "r")
		data = data[:len(data) // saved * saved].reshape(-1, saved)
		self.Init(data[:, :n], float(meta.get("imSampRate", 30000)), MetaUv(meta, n), Geometry.FromMeta(meta, n),
			int(meta.get("firstSample", 0)), speed, chunk_s, start_s, loop)

	@classmethod
	def FromArray(cls, data, rate, uv, geometry, first_sample=0, speed=0.0, chunk_s=0.05, loop=False):
		self = cls.__new__(cls)
		self.Init(data, rate, uv, geometry, first_sample, speed, chunk_s, 0.0, loop)
		return self

	def Init(self, data, rate, uv, geometry, first_sample, speed, chunk_s, start_s, loop):
		self.data, self.first, self.speed, self.loop = data, int(first_sample), float(speed), loop
		self.stream = Stream(rate, uv, geometry)
		self.chunk = max(1, int(round(rate * chunk_s)))
		self.at = int(round(start_s * rate)) # the next sample to give, in the file
		self.t0, self.at0 = None, self.at
		self.state = "replaying"
		self.skipped = 0

	def Open(self):
		return self.stream

	def Read(self):
		"""(first sample, int16 [samples, channels]) of what has arrived since the last read, or None."""
		if self.at >= len(self.data):
			if not self.loop:
				self.state = "ended"
				return None
			self.at = self.at0 = 0
			self.first += len(self.data) # the stream's count goes on
			self.t0 = None
		if self.speed <= 0:
			end = min(len(self.data), self.at + self.chunk)
		else:
			now = time.monotonic()
			if self.t0 is None:
				self.t0 = now
			end = min(len(self.data), self.at0 + int((now - self.t0) * self.stream.rate * self.speed))
			if end <= self.at:
				return None
		piece = (self.first + self.at, np.asarray(self.data[self.at:end]))
		self.at = end
		return piece

	def Lag(self):
		return 0.0

	def Close(self):
		pass


class ReadOnly:
	"""SpikeGLX's SDK module, with only the calls in READ_ONLY: anything else raises EphysError."""

	def __init__(self, sdk):
		self._sdk = sdk

	def __getattr__(self, name):
		if name not in READ_ONLY:
			raise EphysError("{} is not a read-only SpikeGLX call: the live view never changes SpikeGLX".format(name))
		return getattr(self._sdk, name)


class SpikeGLXSource:
	"""A probe's AP stream (js 2, ip), read live from SpikeGLX's command server with read-only calls: from where the
	last read ended, up to max_s at a time, at most every every_s (asked as soon as samples come, a reader would ask
	about 400 times a second, each costing SpikeGLX some CPU). More than max_lag_s behind (this computer busy), it skips to the newest
	samples and counts what it skipped, rather than falling further behind. filtered: from SpikeGLX's filtered AP
	buffer (js -2; on only if it is turned on in SpikeGLX's IM Setup tab), so Filter skips its high-pass."""

	def __init__(self, sdk_folder, host="127.0.0.1", port=4142, ip=0, filtered=False, max_s=0.5, max_lag_s=1.0,
		every_s=0.05):
		from campy import spikeglx
		self.sdk = ReadOnly(spikeglx.Sdk(sdk_folder))
		self.host, self.port, self.ip, self.filtered = host, port, ip, filtered
		self.max_s, self.max_lag_s = max_s, max_lag_s
		self.every_s, self.fetched_at = every_s, 0.0 # a read at most every every_s: SpikeGLX answers each one
		self.conn, self.stream, self.next = None, None, None
		self.state, self.skipped, self.lag = "not connected", 0, None
		self.lag_at = 0.0

	def Connect(self):
		from campy import spikeglx
		if self.conn is None:
			conn = spikeglx.Connection(self.sdk, self.host, self.port)
			try:
				conn.__enter__()
			except spikeglx.SpikeGLXError as e:
				self.state = str(e)
				return None
			self.conn = conn
		return self.conn

	def Open(self):
		"""The stream (rate, channels, gains, geometry) of SpikeGLX's run, or None while it runs none."""
		from ctypes import byref, c_double, c_int
		conn = self.Connect()
		if conn is None:
			return None
		sglx, h, ip = self.sdk, conn.h, self.ip
		if not conn.Running():
			self.state, self.stream, self.next = "SpikeGLX runs no run", None, None
			return None
		rate = sglx.c_sglx_getStreamSampleRate(h, 2, ip)
		n = c_int()
		conn.Ok(sglx.c_sglx_getStreamAcqChans(byref(n), h, 2, ip), "getStreamAcqChans")
		counts = [sglx.c_sglx_getint(h, i) for i in range(n.value)] # AP, LF, SY
		ap = counts[0] if counts else 0
		if rate <= 0 or ap <= 0:
			raise EphysError("SpikeGLX's probe stream {} has no AP channels (rate {}, channels {}).".format(ip, rate,
				counts))
		uv = []
		for ch in range(ap):
			mult = c_double()
			conn.Ok(sglx.c_sglx_getStreamI16ToVolts(byref(mult), h, 2, ip, ch), "getStreamI16ToVolts")
			uv.append(mult.value * 1e6)
		geometry = Geometry.Linear(ap)
		if sglx.c_sglx_getGeomMap(byref(n), h, ip):
			pairs = {}
			for i in range(n.value):
				length = c_int()
				k, _, v = sglx.c_sglx_getstr(byref(length), h, i).decode(errors="replace").partition("=")
				pairs[k.strip()] = v.strip()
			geometry = Geometry.FromPairs(pairs, ap)
		self.stream = Stream(rate, uv, geometry, filtered=self.filtered)
		self.chans = (c_int * ap)(*range(ap))
		self.state = "reading probe {}: {} channels at {:g} Hz".format(ip, ap, rate)
		return self.stream

	def Count(self):
		return self.sdk.c_sglx_getStreamSampleCount(self.conn.h, 2, self.ip)

	def Read(self):
		"""(first sample, int16 [samples, channels]) since the last read, or None (nothing new, or no run)."""
		from ctypes import POINTER, byref, c_int, c_short
		if self.stream is None and self.Open() is None:
			return None
		if time.monotonic() - self.fetched_at < self.every_s:
			return None
		self.fetched_at = time.monotonic()
		sglx, h, rate = self.sdk, self.conn.h, self.stream.rate
		if self.next is None: # from 1 at the least: SpikeGLX's fetch says 0 for an error, so not from sample 0
			self.next = max(1, self.Count() - int(rate * 0.05))
		data, nd = POINTER(c_short)(), c_int()
		nc = len(self.chans)
		head = sglx.c_sglx_fetch(byref(data), byref(nd), h, -2 if self.filtered else 2, self.ip, self.next,
			int(rate * self.max_s), self.chans, nc, 1)
		if head == 0:
			if not self.conn.Running():
				self.state, self.stream, self.next = "SpikeGLX runs no run", None, None
				return None
			count = self.Count()
			if count < self.next: # a new run: its count starts again
				self.stream, self.next = None, None
			return None
		samples = nd.value // nc
		if samples == 0:
			return None
		if head > self.next: # SpikeGLX no longer had them
			self.skipped += head - self.next
		block = np.ctypeslib.as_array(data, shape=(samples * nc,)).reshape(samples, nc).copy()
		self.next = head + samples
		now = time.monotonic()
		if samples >= int(rate * self.max_s) or now - self.lag_at > 1.0:
			self.lag_at = now
			count = self.Count()
			self.lag = (count - self.next) / rate
			if self.lag > self.max_lag_s: # behind: on from the newest samples
				jump = count - int(rate * 0.05)
				self.skipped += jump - self.next
				self.next = jump
				self.lag = (count - self.next) / rate
		return int(head), block

	def Lag(self):
		return self.lag

	def Close(self):
		if self.conn is not None:
			try:
				self.conn.__exit__(None, None, None)
			finally:
				self.conn = None


# ---------------------------------------------------------------- filtering and spikes

class Filter:
	"""A Butterworth high-pass, forwards only (unless SpikeGLX filtered the stream already), then the median across
	used channels off each sample (car). uV out, float32."""

	def __init__(self, stream, highpass_hz=HIGHPASS_HZ, order=ORDER, car=True):
		from scipy.signal import butter
		self.stream, self.car = stream, car
		self.used = np.flatnonzero(stream.geometry.used)
		self.sos = None if stream.filtered else butter(order, highpass_hz, btype="highpass", fs=stream.rate,
			output="sos")
		self.zi = None

	def Reset(self):
		self.zi = None

	def Apply(self, raw):
		from scipy.signal import sosfilt, sosfilt_zi
		x = raw.astype(np.float32)
		x *= self.stream.uv
		if self.sos is not None:
			if self.zi is None: # as if the first sample had always been there: no step at the start
				self.zi = sosfilt_zi(self.sos)[:, :, None] * x[0][None, None, :]
			x, self.zi = sosfilt(self.sos, x, axis=0, zi=self.zi)
			x = x.astype(np.float32, copy=False)
		if self.car and len(self.used):
			x -= np.median(x[:, self.used], axis=1, keepdims=True)
		return x


class Spikes:
	"""Spikes found: their sample (on the probe's stream), channel, amplitude (uV, the trough: negative), unit (-1:
	not sorted), and waveforms [spikes, 61, NEIGHBORS] on the sites in neighbors [spikes, NEIGHBORS] (-1: none,
	its waveform zeros), the spike's own channel first."""

	def __init__(self, sample=(), channel=(), amplitude=(), unit=None, waveforms=None, neighbors=None):
		self.sample = np.asarray(sample, np.int64)
		self.channel = np.asarray(channel, np.int32)
		self.amplitude = np.asarray(amplitude, np.float32)
		n = len(self.sample)
		self.unit = -np.ones(n, np.int32) if unit is None else np.asarray(unit, np.int32)
		self.waveforms = np.zeros((n, BEFORE + AFTER, NEIGHBORS), np.float32) if waveforms is None else waveforms
		self.neighbors = -np.ones((n, NEIGHBORS), np.int32) if neighbors is None else neighbors

	def __len__(self):
		return len(self.sample)


class Detector:
	"""Threshold crossings, one per spike: see the module's notes. threshold: x each channel's noise, one number or
	one per channel (np.inf: that channel off)."""

	def __init__(self, stream, threshold=4.5, radius_um=50.0, before_ms=1.5, after_ms=0.5):
		self.stream = stream
		self.threshold = np.broadcast_to(np.asarray(threshold, np.float32), (len(stream.uv),)).copy()
		self.threshold[~stream.geometry.used] = np.inf
		self.neighbors = stream.geometry.Neighbors(radius_um) # one spike among these
		self.sites = stream.geometry.Neighbors(np.inf) # its waveform on these
		self.pre = max(1, int(round(stream.rate * before_ms / 1000))) # a deeper sample this long before: not a spike
		self.post = max(1, int(round(stream.rate * after_ms / 1000))) # or this long after
		self.block = max(NOISE_STEP, int(round(stream.rate * NOISE_BLOCK_S)) // NOISE_STEP * NOISE_STEP)
		self.alpha = min(1.0, NOISE_BLOCK_S / NOISE_TAU_S)
		self.Reset()

	def Reset(self):
		self.tail, self.tail_first, self.next = None, None, None # unjudged samples kept, their first; next to judge
		self.noise = None # the noise as of the last whole block (uV)
		self.noise_by_block = {} # block number -> the noise its samples are judged with
		self.blk, self.blk_values = None, [] # the block being measured, its samples so far

	def SetThreshold(self, threshold, channel=None):
		if channel is None:
			self.threshold[:] = threshold
		else:
			self.threshold[channel] = threshold
		self.threshold[~self.stream.geometry.used] = np.inf

	def Noise(self, first, x):
		"""Measure the noise on fixed blocks of samples (every NOISE_STEP-th, counted from 0 on the stream): a
		block's spikes are judged with the noise as of the block before it."""
		offset = (-first) % NOISE_STEP
		at = np.arange(first + offset, first + len(x), NOISE_STEP)
		values = x[offset::NOISE_STEP]
		blocks = at // self.block
		for b in np.unique(blocks):
			if self.blk is not None and b != self.blk:
				self.EndBlock()
			self.blk = int(b)
			self.blk_values.append(values[blocks == b])

	def EndBlock(self):
		v = np.concatenate(self.blk_values) if self.blk_values else None
		if v is not None and len(v) * NOISE_STEP >= self.block // 2:
			level = np.median(np.abs(v), axis=0) / 0.6745
			self.noise = level if self.noise is None else self.alpha * level + (1 - self.alpha) * self.noise
			self.noise_by_block[self.blk + 1] = self.noise.astype(np.float32)
		self.blk, self.blk_values = None, []

	def Feed(self, first, x):
		"""Spikes judged with the samples given (x: filtered uV [samples, channels], its first sample on the
		stream's count); a spike near the end waits for its window's samples, in the next Feed."""
		if self.tail is not None and first != self.tail_first + len(self.tail):
			self.Reset() # a gap: start again
		self.Noise(first, x)
		if self.tail is None:
			buf, bfirst, self.next = x, first, first
		else:
			buf, bfirst = np.concatenate([self.tail, x]), self.tail_first
		margin_lo, margin_hi = max(BEFORE, self.pre + 1), max(AFTER, self.post + 1)
		lo = max(self.next - bfirst, margin_lo)
		hi = len(buf) - margin_hi
		found = self.Judge(buf, bfirst, lo, hi) if hi > lo else Spikes()
		self.next = bfirst + max(hi, lo)
		for done in [k for k in self.noise_by_block if k < self.next // self.block]: # blocks judged
			del self.noise_by_block[done]
		keep = max(0, self.next - bfirst - margin_lo)
		self.tail, self.tail_first = buf[keep:], bfirst + keep
		return found

	def Judge(self, buf, bfirst, lo, hi):
		if not self.noise_by_block:
			return Spikes()
		# Each sample's threshold: from its block's noise (a block not yet measured has none: not judged)
		at = np.arange(lo, hi) + bfirst
		blocks = at // self.block
		thr = np.full((hi - lo, buf.shape[1]), -np.inf, np.float32)
		for b in np.unique(blocks):
			noise = self.noise_by_block.get(int(b))
			if noise is not None:
				with np.errstate(invalid="ignore"): # a channel off (inf) with no noise (0): nan, never below
					thr[blocks == b] = -self.threshold * noise
		core = buf[lo:hi]
		cand = (core < thr) & (core <= buf[lo - 1:hi - 1]) & (core < buf[lo + 1:hi + 1])
		t, c = np.nonzero(cand)
		if len(t) == 0:
			return Spikes()
		t = t + lo
		# The lowest on its neighbors, from pre samples before to post after
		nb = self.neighbors[c] # [n, K]
		rows = t[:, None] + np.arange(-self.pre, self.post + 1)[None, :] # [n, pre+post+1]
		window = buf[rows[:, :, None], np.maximum(nb, 0)[:, None, :]] # [n, pre+post+1, K]
		window = np.where(nb[:, None, :] >= 0, window, np.inf)
		keep = buf[t, c] <= window.min(axis=(1, 2))
		t, c = t[keep], c[keep]
		nb = self.sites[c]
		rows = t[:, None] + np.arange(-BEFORE, AFTER)[None, :]
		waves = buf[rows[:, :, None], np.maximum(nb, 0)[:, None, :]]
		waves = np.where(nb[:, None, :] >= 0, waves, 0.0).astype(np.float32)
		return Spikes(bfirst + t, self.stream.channels[c], buf[t, c], None, waves, nb)


class Unsorted:
	"""No sorting: every spike's unit stays -1 (a threshold crossing). A live sorter has the same two methods:
	Assign gives each spike its unit (from its waveforms on its neighbors), Units says what the units are."""
	name = "threshold crossings"

	def Assign(self, spikes):
		return spikes

	def Units(self):
		return [] # [{"unit": id, "channel": its peak channel, "label": "good"/"mua", ...}]


# ---------------------------------------------------------------- what a view shows

class Live:
	"""What a view shows, kept as the data comes: an envelope of scope channels (each bin_ms's lowest and highest,
	filtered uV), the last keep_s of spikes, and the latest snippets waveforms of each channel and unit (on the
	spike's own channel). Snapshot gives what came since a view's last one. Thread-safe: Add from the reading
	loop, Snapshot from a web server's."""

	def __init__(self, stream, scope=None, scope_s=5.0, bin_ms=1.0, keep_s=10.0, snippets=30):
		self.stream, self.lock = stream, threading.Lock()
		n = len(stream.uv)
		self.scope = list(range(0, n, max(1, n // 4)))[:4] if scope is None else list(scope)
		self.bin = max(1, int(round(stream.rate * bin_ms / 1000)))
		self.max_bins = int(scope_s * 1000 / bin_ms)
		self.keep = int(keep_s * stream.rate)
		self.snippets = snippets
		self.Clear()

	def Clear(self):
		k = len(self.scope)
		self.lo, self.hi = np.zeros((0, k), np.float32), np.zeros((0, k), np.float32)
		self.first_bin = None # the bin number of lo[0] (sample // bin)
		self.pending, self.pending_first = None, None # samples of a bin not yet whole
		self.spikes = Spikes()
		self.by_channel, self.by_unit = {}, {}
		self.newest = None # the newest sample seen

	def SetScope(self, channels):
		with self.lock:
			self.scope = list(channels)
			self.lo, self.hi = np.zeros((0, len(self.scope)), np.float32), np.zeros((0, len(self.scope)), np.float32)
			self.first_bin, self.pending = None, None

	def Add(self, first, filtered, spikes):
		with self.lock:
			self.newest = first + len(filtered)
			self.Envelope(first, filtered[:, self.scope])
			self.Spikes(spikes)

	def Envelope(self, first, x):
		if self.pending is not None and self.pending_first + len(self.pending) == first:
			x, first = np.concatenate([self.pending, x]), self.pending_first
		start = (-first) % self.bin # from the first whole bin
		whole = (len(x) - start) // self.bin
		if whole > 0:
			b = x[start:start + whole * self.bin].reshape(whole, self.bin, -1)
			b0 = (first + start) // self.bin
			if self.first_bin is None or b0 != self.first_bin + len(self.lo): # a gap: start again
				self.lo, self.hi, self.first_bin = b.min(axis=1), b.max(axis=1), b0
			else:
				self.lo, self.hi = np.concatenate([self.lo, b.min(axis=1)]), np.concatenate([self.hi, b.max(axis=1)])
			if len(self.lo) > self.max_bins:
				cut = len(self.lo) - self.max_bins
				self.lo, self.hi, self.first_bin = self.lo[cut:], self.hi[cut:], self.first_bin + cut
		rest = start + max(0, whole) * self.bin
		self.pending, self.pending_first = x[rest:], first + rest

	def Spikes(self, new):
		if len(new):
			s = self.spikes
			self.spikes = Spikes(np.concatenate([s.sample, new.sample]), np.concatenate([s.channel, new.channel]),
				np.concatenate([s.amplitude, new.amplitude]), np.concatenate([s.unit, new.unit]))
			for i in range(len(new)):
				wave = new.waveforms[i, :, 0]
				self.by_channel.setdefault(int(new.channel[i]), deque(maxlen=self.snippets)).append(wave)
				if new.unit[i] >= 0:
					self.by_unit.setdefault(int(new.unit[i]), deque(maxlen=self.snippets)).append(wave)
		if self.newest is not None and len(self.spikes) and self.spikes.sample[0] < self.newest - self.keep:
			k = np.searchsorted(self.spikes.sample, self.newest - self.keep)
			s = self.spikes
			self.spikes = Spikes(s.sample[k:], s.channel[k:], s.amplitude[k:], s.unit[k:])

	def Snapshot(self, since_sample=None, since_bin=None, rate_s=2.0, snippet_channels=(), snippet_units=()):
		"""For a view: the scope's bins after since_bin, the spikes after since_sample, each channel's and unit's
		rate over the last rate_s (spikes a second), and the snippets of the channels and units asked for. Lists,
		for JSON."""
		with self.lock:
			out = {"newest": self.newest, "scope": {"channels": [int(self.stream.channels[c]) for c in self.scope],
				"bin_samples": self.bin, "first_bin": None, "min": [], "max": []}}
			if self.first_bin is not None and len(self.lo):
				k = 0 if since_bin is None else max(0, since_bin + 1 - self.first_bin)
				out["scope"].update(first_bin=int(self.first_bin + k), min=np.round(self.lo[k:].T, 1).tolist(),
					max=np.round(self.hi[k:].T, 1).tolist())
			s = self.spikes
			k = 0 if since_sample is None else np.searchsorted(s.sample, since_sample, side="right")
			out["spikes"] = {"sample": s.sample[k:].tolist(), "channel": s.channel[k:].tolist(),
				"amplitude": np.round(s.amplitude[k:], 1).tolist(), "unit": s.unit[k:].tolist()}
			recent = s.sample > (self.newest or 0) - rate_s * self.stream.rate
			n = len(self.stream.uv)
			out["rates"] = {"channel": np.round(np.bincount(np.searchsorted(self.stream.channels, s.channel[recent]),
				minlength=n) / rate_s, 2).tolist(), "unit": {int(u): round(float(c) / rate_s, 2) for u, c in
				zip(*np.unique(s.unit[recent & (s.unit >= 0)], return_counts=True))}}
			out["snippets"] = {"channel": {int(c): np.round(np.array(self.by_channel[c]), 1).tolist()
				for c in snippet_channels if c in self.by_channel}, "unit": {int(u): np.round(np.array(
				self.by_unit[u]), 1).tolist() for u in snippet_units if u in self.by_unit}}
			return out


# ---------------------------------------------------------------- running it

class Engine:
	"""A source through the filter, the detector and the sorter into Live, piece by piece, timing each step."""

	def __init__(self, source, threshold=4.5, sorter=None, scope=None):
		self.source, self.threshold, self.sorter = source, threshold, sorter or Unsorted()
		self.scope = scope
		self.stream = None
		self.stats = {"fetch_s": 0.0, "process_s": 0.0, "reads": 0, "samples": 0, "spikes": 0, "gaps": 0}
		self.expected = None

	def Start(self):
		stream = self.source.Open()
		if stream is None:
			return False
		if self.stream is None or len(stream.uv) != len(self.stream.uv):
			self.live = Live(stream, self.scope)
		self.stream, self.filter = stream, Filter(stream)
		self.detector = Detector(stream, self.threshold)
		self.expected = None
		return True

	def Step(self):
		"""One read and what follows: the number of samples it brought (0: nothing new)."""
		if self.stream is None and not self.Start():
			return 0
		t0 = time.perf_counter()
		piece = self.source.Read()
		t1 = time.perf_counter()
		self.stats["fetch_s"] += t1 - t0
		if piece is None:
			if getattr(self.source, "stream", self.stream) is None: # the run stopped
				self.stream = None
			return 0
		first, raw = piece
		if self.expected is not None and first != self.expected:
			self.stats["gaps"] += 1
			self.filter.Reset()
		self.expected = first + len(raw)
		filtered = self.filter.Apply(raw)
		spikes = self.sorter.Assign(self.detector.Feed(first, filtered))
		self.live.Add(first, filtered, spikes)
		self.stats["process_s"] += time.perf_counter() - t1
		self.stats["reads"] += 1
		self.stats["samples"] += len(raw)
		self.stats["spikes"] += len(spikes)
		return len(raw)


def LowerPriority():
	"""This process below SpikeGLX's and campy's: below normal on Windows, nice 10 elsewhere."""
	try:
		import psutil
		p = psutil.Process()
		p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else 10)
	except Exception:
		pass


def SpikeGLXProcess():
	try:
		import psutil
		for p in psutil.process_iter(["name"]):
			if (p.info["name"] or "").lower().startswith("spikeglx"):
				return p
	except Exception:
		pass
	return None


def Clock(t=None):
	return time.strftime("%I:%M:%S %p", time.localtime(t)).lstrip("0")


def Bench(args, out=print):
	"""campy-ephys bench: run the engine for --seconds, a line a second, a summary (and --json) at the end."""
	import psutil
	if args.priority == "low":
		LowerPriority()
	if args.replay:
		source = ReplaySource(args.replay, speed=args.speed, chunk_s=args.chunk_ms / 1000)
	else:
		source = SpikeGLXSource(args.sdk, args.host, args.port, args.ip, filtered=args.spikeglx_filter)
	engine = Engine(source, args.threshold)
	me, sglx = psutil.Process(), SpikeGLXProcess()
	me.cpu_percent(None)
	if sglx:
		sglx.cpu_percent(None)
	lines, t_start = [], time.monotonic()
	last, last_stats = t_start, dict(engine.stats)
	try:
		while time.monotonic() - t_start < args.seconds:
			got = engine.Step()
			if source.__class__ is ReplaySource and source.state == "ended":
				break
			now = time.monotonic()
			if now - last >= 1.0:
				d = {k: engine.stats[k] - last_stats[k] for k in engine.stats}
				line = {"t": round(now - t_start, 1), "samples_per_s": d["samples"] / (now - last),
					"fetch_ms": 1000 * d["fetch_s"] / max(1, d["reads"]), "process_s_per_s": d["process_s"] / (now - last),
					"spikes_per_s": d["spikes"] / (now - last), "lag_s": source.Lag(), "skipped": source.skipped,
					"cpu_this": me.cpu_percent(None), "cpu_spikeglx": sglx.cpu_percent(None) if sglx else None}
				lines.append(line)
				out("{}  {:,.0f} samples/s, fetch {:.1f} ms, filter+detect {:.0f}% of a core, {:,.0f} spikes/s, lag {}, "
					"skipped {:,}, CPU: this {:.0f}%{}".format(Clock(), line["samples_per_s"], line["fetch_ms"],
					100 * line["process_s_per_s"], line["spikes_per_s"], "{:.2f} s".format(line["lag_s"]) if line["lag_s"]
					is not None else "?", line["skipped"], line["cpu_this"], ", SpikeGLX {:.0f}%".format(line[
					"cpu_spikeglx"]) if sglx else ""))
				last, last_stats = now, dict(engine.stats)
			if not got:
				time.sleep(0.005 if args.replay and args.speed <= 0 else 0.02)
	finally:
		source.Close()
	took = time.monotonic() - t_start
	s = engine.stats
	summary = {"seconds": round(took, 2), "source": args.replay or "spikeglx", "channels": len(engine.stream.uv) if
		engine.stream else None, "samples": s["samples"], "data_seconds": s["samples"] / engine.stream.rate if
		engine.stream else 0, "reads": s["reads"], "fetch_ms_mean": 1000 * s["fetch_s"] / max(1, s["reads"]),
		"process_s_per_data_s": s["process_s"] / max(1e-9, s["samples"] / engine.stream.rate) if engine.stream and
		s["samples"] else None, "spikes": s["spikes"], "gaps": s["gaps"], "skipped": source.skipped,
		"state": source.state, "lines": lines}
	out("{}  done: {:,} samples ({:.1f} s of data) in {:.1f} s, {} reads (fetch {:.1f} ms each), filter+detect {} "
		"of a core per second of data, {:,} spikes, {} gaps, {:,} samples skipped. {}".format(Clock(), s["samples"],
		summary["data_seconds"], took, s["reads"], summary["fetch_ms_mean"], "{:.0%}".format(summary[
		"process_s_per_data_s"]) if summary["process_s_per_data_s"] is not None else "?", s["spikes"], s["gaps"],
		source.skipped, source.state))
	if args.json:
		from campy import strictjson
		with open(args.json, "w") as f:
			strictjson.dump(summary, f, indent=1)
	return summary


def Main(argv=None):
	parser = argparse.ArgumentParser(prog="campy-ephys", description="Live ephys from SpikeGLX (read-only): measure "
		"what reading, filtering and finding spikes costs.")
	sub = parser.add_subparsers(dest="command", required=True)
	b = sub.add_parser("bench", help="read a probe's AP stream (SpikeGLX, or a saved .ap.bin) through the filter and "
		"the spike detector, a line a second")
	src = b.add_mutually_exclusive_group(required=True)
	src.add_argument("--sdk", help="the folder holding SpikeGLX's SDK (sglx_pkg): read live from SpikeGLX")
	src.add_argument("--replay", help="a saved .ap.bin (its .meta beside it)")
	b.add_argument("--host", default="127.0.0.1")
	b.add_argument("--port", type=int, default=4142)
	b.add_argument("--ip", type=int, default=0, help="which probe (SpikeGLX's ip)")
	b.add_argument("--spikeglx-filter", action="store_true", help="read SpikeGLX's filtered AP buffer (js -2; it "
		"must be turned on in SpikeGLX's IM Setup tab)")
	b.add_argument("--speed", type=float, default=1.0, help="--replay: x the recorded rate (0: as fast as it can)")
	b.add_argument("--chunk-ms", type=float, default=50.0, help="--replay at speed 0: each read's length")
	b.add_argument("--seconds", type=float, default=60.0)
	b.add_argument("--threshold", type=float, default=4.5, help="x each channel's noise")
	b.add_argument("--priority", choices=("low", "normal"), default="low")
	b.add_argument("--json", help="write the summary and the lines here")
	args = parser.parse_args(argv)
	try:
		Bench(args)
	except (EphysError, OSError, KeyError) as e:
		print("campy-ephys: {}".format(e), file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	sys.exit(Main())
