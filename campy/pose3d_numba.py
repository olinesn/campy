"""
campy/pose3d.py's two busiest loops compiled with numba, when numba is installed (pip install
numba, or campy[fast3d]): the lens undistortion of each set's keypoints, and the scoring of each
camera's detections against the animals followed. pose3d.py imports this only through
pose3d.StartFast, in a thread of its own, and uses it once it is ready; without numba it does the
same in numpy.

Each loop does the same arithmetic as pose3d.py's numpy, in the same order, so it gives the same
numbers to the last bit (tests/test_pose3d.py holds it to that):
- no fastmath: numba then neither reorders nor fuses (no fused multiply-add) floating-point operations
- the scores' sum over a skeleton's nodes copies numpy's own order for a sum (_PairwiseSum)
- what numpy does with BLAS (the matrix products), its exp and the eigen-solve stay in numpy: numba's
  versions can differ in the last bit (numba's eigh did, on the Jetson AGX Orin)
The loops release Python's lock while they run (nogil), so campy-live's thread running the models
isn't held up by them. cache=True keeps the compiled code beside this file (numba's __pycache__),
so it is compiled once per numba version, not at each start.
"""

import math

import numba
import numpy as np

VERSION = numba.__version__


@numba.njit(cache=True, nogil=True)
def Undistort(px, py, cx, cy, fx, fy, k1, k2, p1, p2, k3, p1x2, p2x2, iterations, check_px, distorted, out):
	# pose3d._Undistorter.__call__, one point at a time: flat (n,) arrays in, out (n, 2)
	for i in range(px.shape[0]):
		x0 = (px[i] - cx[i]) / fx[i]
		y0 = (py[i] - cy[i]) / fy[i]
		x, y = x0, y0
		if distorted:
			for _ in range(iterations):
				r2 = x * x + y * y
				radial = 1.0 + r2 * (k1[i] + r2 * (k2[i] + r2 * k3[i]))
				xn = (x0 - p1x2[i] * x * y - p2[i] * (r2 + 2.0 * x * x)) / radial
				yn = (y0 - p1[i] * (r2 + 2.0 * y * y) - p2x2[i] * x * y) / radial
				x, y = xn, yn
			if check_px > 0:
				r2 = x * x + y * y
				radial = 1.0 + r2 * (k1[i] + r2 * (k2[i] + r2 * k3[i]))
				ex = (x * radial + p1x2[i] * x * y + p2[i] * (r2 + 2.0 * x * x) - x0) * fx[i]
				ey = (y * radial + p1[i] * (r2 + 2.0 * y * y) + p2x2[i] * x * y - y0) * fy[i]
				if ex * ex + ey * ey > check_px * check_px:
					x = np.nan
					y = np.nan
		out[i, 0] = x
		out[i, 1] = y


@numba.njit(cache=True, nogil=True)
def _PairwiseSum(a, n):
	# numpy's sum of n contiguous float64 (its pairwise_sum, for n up to 128: 8 running sums
	# from 8 values on), in its order, so to the same last bit
	if n < 8:
		r = -0.0
		for j in range(n):
			r += a[j]
		return r
	r0, r1, r2, r3, r4, r5, r6, r7 = a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]
	m = n - n % 8
	for j in range(8, m, 8):
		r0 += a[j]
		r1 += a[j + 1]
		r2 += a[j + 2]
		r3 += a[j + 3]
		r4 += a[j + 4]
		r5 += a[j + 5]
		r6 += a[j + 6]
		r7 += a[j + 7]
	r = ((r0 + r1) + (r2 + r3)) + ((r4 + r5) + (r6 + r7))
	for j in range(m, n):
		r += a[j]
	return r


@numba.njit(cache=True, nogil=True)
def Score(proj, dxy, vf, E, v, rays, nw, corr2d, corr3d, dist_thr, max_penalty, has_max, out):
	# pose3d.Tracker._ScoreCameras after its matrix products and exp, out (K, T, D):
	# proj (K, T, N, 2) the targets projected, dxy (K, D, N, 2) the detections, vf and E (T,)
	# vel * (1 + dt) and exp(-time_penalty * dt), v (K, T, N, 3) the targets from each camera's
	# centre, rays (K, D, N, 3) the detections' rays (unit), nw (N,) the node weights
	K, T, N = proj.shape[0], proj.shape[1], proj.shape[2]
	D = dxy.shape[1]
	row = np.empty(N)
	for k in range(K):
		for t in range(T):
			for d in range(D):
				for n in range(N):
					dx = proj[k, t, n, 0] - dxy[k, d, n, 0]
					dy = proj[k, t, n, 1] - dxy[k, d, n, 1]
					s2 = corr2d * (1.0 - math.sqrt(dx * dx + dy * dy) / vf[t]) * E[t]
					v0, v1, v2 = v[k, t, n, 0], v[k, t, n, 1], v[k, t, n, 2]
					r0, r1, r2 = rays[k, d, n, 0], rays[k, d, n, 1], rays[k, d, n, 2]
					cx = v1 * r2 - v2 * r1
					cy = v2 * r0 - v0 * r2
					cz = v0 * r1 - v1 * r0
					s3 = 1.0 - math.sqrt(cx * cx + cy * cy + cz * cz) / dist_thr
					if has_max and s3 < -max_penalty:   # np.maximum: a NaN stays NaN
						s3 = -max_penalty
					s3 = corr3d * s3
					row[n] = ((s2 if math.isfinite(s2) else 0.0) + (s3 if math.isfinite(s3) else 0.0)) * nw[n]
				out[k, t, d] = _PairwiseSum(row, N)


def WarmUp():
	# Compiles both (or loads them from numba's cache) on small arrays of the types pose3d passes
	n = np.ones(4)
	Undistort(n, n, n, n, n, n, n, n, n, n, n, n, n, 2, 1.0, True, np.empty((4, 2)))
	a4 = np.ones((1, 1, 2, 2))
	a3 = np.ones((1, 1, 2, 3))
	t1 = np.ones(1)
	Score(a4, a4, t1, t1, a3, a3, np.ones(2), 1.0, 6.0, 25.0, 4.0, True, np.empty((1, 1, 1)))
