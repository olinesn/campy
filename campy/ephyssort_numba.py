"""
campy/ephyssort.py's template matching, its busiest loop compiled with numba when numba is installed
(pip install numba, or campy[fast3d]): each pass's spikes, the highest first, each looked at again
with those before it taken away, fitted alone or with other units' spikes at once, and taken away.
ephyssort.py imports this only through ephyssort.StartFast, in a thread of its own, and uses it once
it is ready; without numba it does the same in Python and numpy (TemplateMatcher.Judge, Group,
PairFits, JointFits, Solve3, Subtract).

The loop does the same arithmetic as ephyssort.py's, in the same order, so it gives the same spikes
and leaves the same scores, to the last bit (tests/test_ephyssort.py holds it to that):
- no fastmath: numba then neither reorders nor fuses (no fused multiply-add) floating-point operations
- error_model="numpy": a division by zero gives inf or nan, as numpy's does, rather than an error
- a spike is taken away in float32, its size first made float32, as ephyssort.Subtract does
The loop releases Python's lock while it runs (nogil). cache=True keeps the compiled code beside this
file (numba's __pycache__), so it is compiled once per numba version, not at each start.
"""

import math

import numba
import numpy as np

VERSION = numba.__version__

_JIT = dict(cache=True, nogil=True, error_model="numpy")


@numba.njit(**_JIT)
def _Within(times, n, t, w):
	# A spike of the unit (times[:n]) within w samples of t
	for i in range(n):
		if abs(times[i] - t) <= w:
			return True
	return False


@numba.njit(**_JIT)
def _Paired(paired, n, tv, v):
	for i in range(n):
		if paired[i, 0] == tv and paired[i, 1] == v:
			return True
	return False


@numba.njit(**_JIT)
def _Cross(cross, cross_at, pos, ui, uj, lag, L):
	# Unit uj's score lowered at lag by a spike of ui of size 1 (ephyssort's cross[ui][pos[ui, uj], lag + L - 1]); 0
	# where they share no site, or further apart than a template
	p = pos[ui, uj]
	if p >= 0 and abs(lag) < L:
		return np.float64(cross[cross_at[ui] + p, lag + L - 1])
	return 0.0


@numba.njit(**_JIT)
def _Subtract(R, a0, t, u, a, cross, cross_at, runs, runs_at, L):
	# ephyssort.TemplateMatcher.Subtract
	i0 = t - (L - 1) - a0
	lo = max(0, i0)
	hi = min(R.shape[1], i0 + 2 * L - 1)
	if hi <= lo:
		return
	a32 = np.float32(a)
	base = cross_at[u]
	for r in range(runs_at[u], runs_at[u + 1]):
		r0, r1, k0 = runs[r, 0], runs[r, 1], runs[r, 2]
		for i in range(r1 - r0):
			row = r0 + i
			c = base + k0 + i
			for j in range(lo, hi):
				R[row, j] -= a32 * cross[c, j - i0]


@numba.njit(**_JIT)
def _Group(R, a0, t, u, peaks_t, peaks_u, paired, n_paired, taken_t, taken_n, norm2, zu, nearmat, pos, cross,
		cross_at, w, pair_search, group_most, size_lo, size_hi, L, g_t, g_u, g_a):
	# ephyssort.TemplateMatcher.Group: the group's members into g_t, g_u, g_a; how many (0: u alone)
	alone = np.float64(R[u, t - a0]) / norm2[u]
	explained = alone * np.float64(R[u, t - a0])
	i = np.searchsorted(peaks_t, t - w)
	j = np.searchsorted(peaks_t, t + w + 1)
	g_t[0] = t
	g_u[0] = u
	g_a[0] = alone
	n = 1
	most = (j - i) * (2 * pair_search + 1)
	trial_t = np.empty(most, np.int64)
	trial_v = np.empty(most, np.int64)
	size = np.empty((most, 3))
	gain = np.empty(most)
	total = np.empty(most)
	while n < group_most:
		k = 0
		for q in range(i, j):
			v = peaks_u[q]
			near = False
			for m in range(n):
				if nearmat[g_u[m], v]:
					near = True
			for m in range(n):
				if v == g_u[m]:
					near = False
			if not near:
				continue
			peak = peaks_t[q]
			if _Within(taken_t[v], taken_n[v], peak, w):
				continue
			for tv in range(max(t - w, peak - pair_search), min(t + w, peak + pair_search) + 1):
				if not _Paired(paired, n_paired, tv, v):
					trial_t[k] = tv
					trial_v[k] = v
					k += 1
		if k == 0:
			break
		best = -1
		for q in range(k):
			tv, v = trial_t[q], trial_v[q]
			bv = np.float64(R[v, tv - a0])
			if n == 1: # ephyssort.TemplateMatcher.PairFits
				b0 = np.float64(R[u, t - a0])
				m00, m11 = norm2[u], norm2[v]
				m01 = _Cross(cross, cross_at, pos, u, v, tv - t, L)
				det = m00 * m11 - m01 * m01
				half = 0.5 * (m00 + m11)
				root = math.sqrt(max(half * half - det, 0.0))
				small = half - root
				bad = not (small > 0) or (half + root) > 1e6 * small
				if bad:
					det = 1.0
				x0 = (m11 * b0 - m01 * bv) / det
				x1 = (m00 * bv - m01 * b0) / det
				together = x0 * b0 + x1 * bv
				size[q, 0], size[q, 1] = x0, x1
			else: # ephyssort.Solve3
				t0, u0, t1, u1 = g_t[0], g_u[0], g_t[1], g_u[1]
				b0 = np.float64(R[u0, t0 - a0])
				b1 = np.float64(R[u1, t1 - a0])
				m00, m11, m22 = norm2[u0], norm2[u1], norm2[v]
				m01 = _Cross(cross, cross_at, pos, u0, u1, t1 - t0, L)
				m02 = _Cross(cross, cross_at, pos, u0, v, tv - t0, L)
				m12 = _Cross(cross, cross_at, pos, u1, v, tv - t1, L)
				d0 = m00
				l10 = m01 / d0
				l20 = m02 / d0
				d1 = m11 - l10 * m01
				e = m12 - l20 * m01
				l21 = e / d1
				d2 = m22 - l20 * m02 - l21 * e
				bad = not (d0 > 0 and d1 > 0 and d2 > 0) # (as numpy's minimum: a nan pivot is bad)
				if not bad:
					bad = max(max(d0, d1), d2) > 1e6 * min(min(d0, d1), d2)
				if bad:
					d0, d1, d2 = 1.0, 1.0, 1.0
					l10, l20, l21 = 0.0, 0.0, 0.0
				y1 = b1 - l10 * b0
				y2 = bv - l20 * b0 - l21 * y1
				x2 = y2 / d2
				x1 = y1 / d1 - l21 * x2
				x0 = b0 / d0 - l10 * x1 - l20 * x2
				together = x0 * b0 + x1 * b1 + x2 * bv
				size[q, 0], size[q, 1], size[q, 2] = x0, x1, x2
			if bad:
				together = -np.inf
			g = together - explained
			gain[q] = g
			total[q] = together
			ok = g > zu[v] * zu[v]
			for m in range(n + 1):
				if not (size[q, m] >= size_lo and size[q, m] <= size_hi):
					ok = False
			if ok and (best < 0 or g > gain[best]):
				best = q
		if best < 0:
			break
		g_t[n] = trial_t[best]
		g_u[n] = trial_v[best]
		explained = total[best]
		for m in range(n + 1):
			g_a[m] = size[best, m]
		n += 1
	if n == 1:
		return 0
	return n


@numba.njit(**_JIT)
def Pass(R, a0, new_t, new_u, new_near, peaks_t, peaks_u, taken_t, taken_n, norm2, root, zu, nearmat, pos, cross,
		cross_at, runs, runs_at, w, pair_search, group_most, size_lo, size_hi, L, out_t, out_u, out_a, out_g):
	"""One pass of ephyssort.TemplateMatcher.Judge: its spikes (new_*, the highest first), each looked at again with
	those before it taken away (R, the scores, whose first sample is a0), fitted alone or with other units' at once,
	and taken away. Each spike found into out_* (sample, unit, size, in a group); how many."""
	n_out = 0
	paired = np.empty((len(new_t) * group_most + 1, 2), np.int64)
	n_paired = 0
	g_t = np.empty(group_most, np.int64)
	g_u = np.empty(group_most, np.int64)
	g_a = np.empty(group_most)
	for i in range(len(new_t)):
		t, u = new_t[i], new_u[i]
		if _Within(taken_t[u], taken_n[u], t, w):
			continue # found already, with another unit's spike at once
		a = np.float64(R[u, t - a0]) / norm2[u]
		if a * root[u] <= zu[u] or a < size_lo:
			continue # explained by the spikes found before it
		n = 0
		if new_near[i]:
			n = _Group(R, a0, t, u, peaks_t, peaks_u, paired, n_paired, taken_t, taken_n, norm2, zu, nearmat, pos,
				cross, cross_at, w, pair_search, group_most, size_lo, size_hi, L, g_t, g_u, g_a)
		if n == 0 and a > size_hi:
			continue # too big for one spike of it, and no other unit's spike at once explains the rest
		if n == 0:
			g_t[0], g_u[0], g_a[0] = t, u, a
			fits = 1
		else:
			fits = n
			for m in range(1, n):
				paired[n_paired, 0], paired[n_paired, 1] = g_t[m], g_u[m]
				n_paired += 1
		for m in range(fits):
			tt, uu, aa = g_t[m], g_u[m], g_a[m]
			_Subtract(R, a0, tt, uu, aa, cross, cross_at, runs, runs_at, L)
			taken_t[uu, taken_n[uu]] = tt
			taken_n[uu] += 1
			out_t[n_out], out_u[n_out], out_a[n_out], out_g[n_out] = tt, uu, aa, n > 0
			n_out += 1
	return n_out


def WarmUp():
	"""Compiles the loop (or loads it from numba's cache) on small arrays of the types ephyssort passes."""
	U, W, L = 2, 300, 61
	R = np.zeros((U, W), np.float32)
	R[0, 150] = 100.0
	nearmat = np.ones((U, U), bool)
	pos = np.zeros((U, U), np.int32)
	pos[0, 1] = 1
	pos[1, 0] = 1
	pos[1, 1] = 0
	cross = np.ones((4, 2 * L - 1), np.float32)
	Pass(R, 0, np.array([150, 152]), np.array([0, 1]), np.array([True, True]), np.array([150, 152]),
		np.array([0, 1]), np.zeros((U, 20), np.int64), np.zeros(U, np.int64), np.full(U, 10.0), np.full(U, 3.0),
		np.full(U, 1.0), nearmat, pos, cross, np.array([0, 2]), np.array([[0, 2, 0, 2], [0, 2, 0, 2]]),
		np.array([0, 1, 2]), 20, 6, 3, 0.5, 2.0, L, np.empty(6, np.int64), np.empty(6, np.int64), np.empty(6),
		np.empty(6, np.bool_))
