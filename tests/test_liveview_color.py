"""
campy-live-view's colours: a colour camera's live feed holds its raw Bayer
mosaic (one channel), which the view demosaics by the pattern the feed's
description says (pixelFormatInput), as the per-camera preview windows did
before campy had only this one window. Checked on made-up mosaics of one
colour, whose demosaiced middle must come back as that colour.
"""

import json, time

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from campy import livefeed, liveview
from test_livefeed import name, rings # noqa: F401 (rings: a fixture)

RED, GREEN, BLUE = 200, 100, 30 # the scene's colour, as R, G, B


def mosaic(pattern, shape=(16, 20), rgb=(RED, GREEN, BLUE)):
	# A camera's raw frame of one colour: pattern names the top left 2 x 2, row by row (RGGB: red at the top left)
	m = np.zeros(shape, np.uint8)
	channel = {"R": 0, "G": 1, "B": 2}
	for dy in range(2):
		for dx in range(2):
			m[dy::2, dx::2] = rgb[channel[pattern[2 * dy + dx]]]
	return m


@pytest.mark.parametrize("pixel_format", ["bayer_rggb8", "bayer_bggr8", "bayer_gbrg8", "bayer_grbg8"])
def test_every_bayer_pattern_comes_back_in_its_colours(pixel_format):
	raw = mosaic(pixel_format[6:10].upper())
	for frame in (raw, raw[:, :, np.newaxis]): # as a camera sends it, and as the feed's ring holds it (1 channel)
		image, bgr = liveview.Colours(frame, pixel_format)
		assert bgr and image.shape == (16, 20, 3)
		assert tuple(int(v) for v in image[8, 10]) == (BLUE, GREEN, RED) # BGR, as the view draws


def test_opencvs_names_are_not_the_cameras():
	# OpenCV's BayerRG is a camera's BGGR: the view's table says which is which (red and blue swapped otherwise)
	raw = mosaic("RGGB")
	assert tuple(int(v) for v in cv2.cvtColor(raw, cv2.COLOR_BayerRG2BGR)[8, 10]) == (RED, GREEN, BLUE) # wrong
	assert liveview.BAYER["bayer_rggb8"] == cv2.COLOR_BayerBG2BGR


def test_gray_rgb_and_bgr_frames():
	gray = np.full((16, 20), 77, np.uint8)
	image, bgr = liveview.Colours(gray, "gray")
	assert image is gray and not bgr
	assert tuple(liveview.Gray(image, (8, 10), bgr=bgr)[4, 5]) == (77, 77, 77)
	rgb = np.zeros((16, 20, 3), np.uint8)
	rgb[:] = (RED, GREEN, BLUE)
	image, bgr = liveview.Colours(rgb, "rgb24")
	assert not bgr and tuple(liveview.Gray(image, (8, 10), bgr=bgr)[4, 5]) == (BLUE, GREEN, RED)
	bgr24 = rgb[:, :, ::-1].copy() # a bgr24 camera's frame: blue first
	image, bgr = liveview.Colours(bgr24, "bgr24")
	assert bgr
	tile = liveview.Gray(image, (8, 10), bgr=bgr)
	assert tuple(tile[4, 5]) == (BLUE, GREEN, RED)
	tile[:] = 0 # its own copy: drawing on it leaves the frame as it was
	assert tuple(bgr24[0, 0]) == (BLUE, GREEN, RED)
	# A Bayer frame of a feed that does not say its pattern (a campy from before): gray, as before
	raw = mosaic("RGGB")
	image, bgr = liveview.Colours(raw, "")
	assert image is raw and not bgr


def test_the_feed_says_its_pixel_format():
	cam_params = {"liveFeed": True, "cameraName": "Camera0", "n_cam": 0, "liveFeedName": name(),
		"pixelFormatInput": "bayer_gbrg8", "frameHeight": 16, "frameWidth": 20}
	feed = livefeed.Feed(cam_params, say=lambda text: None)
	try:
		feed.Prepare(cam_params)
		assert feed.writer.ring.shape == (16, 20, 1)
		assert json.loads(feed.Description())["pixelFormat"] == "bayer_gbrg8"
		reader = livefeed.RingReader(feed.name)
		try:
			assert livefeed.PixelFormat(reader) == "bayer_gbrg8"
		finally:
			reader.Close()
	finally:
		feed.Close()


def test_the_view_shows_a_colour_camera_in_colour(rings):
	# Two cameras, one gray and one bayer_rggb8, in one view: the colour one's tile in its colours
	prefix = name()
	formats = ["gray", "bayer_rggb8"]
	writers = [rings(livefeed.RingName(prefix, c), (64, 80), np.uint8, slots=8, cameraName="Camera{}".format(c),
		description=json.dumps({"kind": livefeed.CAMERA_KIND, "pixelFormat": f})) for c, f in enumerate(formats)]
	frames = [np.full((64, 80), 90, np.uint8), mosaic("RGGB", (64, 80))]
	for n in range(1, 4):
		for w, f in zip(writers, frames):
			w.Write(f, n, 1000 * n)
	viewer = liveview.Viewer(liveview.ParseArgs(["--name", prefix, "--cameras", "2", "--no-window", "--priority",
		"normal"]))
	try:
		viewer.Check()
		assert viewer.pixelFormats == formats
		viewer.lastRow = time.monotonic() - 1 # no row: the frames alone
		image, frame, row = viewer.Step()
		assert frame == 3 and row is None
		th, tw = viewer.layout.tile
		# Pixels inside each tile's 4 px border, clear of the text (as test_liveview's frames-alone test)
		assert tuple(int(v) for v in image[th - 10, tw - 10]) == (90, 90, 90) # camera 0, gray
		b, g, r = (int(v) for v in image[th - 10, 2 * tw - 10]) # camera 1, red-orange (interpolated: within a step)
		assert abs(r - RED) <= 2 and abs(g - GREEN) <= 2 and abs(b - BLUE) <= 2, (b, g, r)
	finally:
		viewer.Close()
