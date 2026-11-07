"""
campy-acquire's one window (campy/acquireview.py): every camera in one
campy-live-view, at displayFrameRate, fed by the live feed it turns on for
every camera; none with displayFrameRate 0, nor under campy-session, which
opens its own.
"""

import os, re, subprocess, sys, types

import pytest

from campy import acquireview, configurator, livefeed, manifest
from test_emu import write_clip
from test_livefeed import name

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
	# Neither switch from the computer running the tests, nor left behind by one
	monkeypatch.delenv(manifest.LAUNCHER_ENV, raising=False)
	monkeypatch.delenv(livefeed.FEED_ENV, raising=False)


def params(**values):
	p = configurator.DefaultParams()
	p.update(numCams=3, liveFeedName="rigfeed", displayFrameRate=10)
	p.update(values)
	return p


class FakeProcess:
	def __init__(self, code=None):
		self.code, self.closed = code, False

	def poll(self):
		return self.code

	def Close(self):
		self.closed = True


class Popen:
	# Stands in for children.Child: what was started, and how
	def __init__(self, code=None):
		self.calls, self.code = [], code

	def __call__(self, command, **kwargs):
		self.calls.append((command, kwargs))
		self.process = FakeProcess(self.code)
		return self.process


def test_one_view_for_every_camera_at_display_frame_rate(tmp_path, monkeypatch):
	monkeypatch.setattr(acquireview, "Python", lambda: ("py", None))
	said, popen = [], Popen()
	p = params(alertTriggerPauseSec=60, alertNoFramesSec=900)
	view = acquireview.View(p, str(tmp_path), say=said.append).Prepare()
	# The camera processes, spawned, read the config again: the feed is on in their environment
	assert os.environ[livefeed.FEED_ENV] == "1" and p["liveFeed"] is True
	view.Start(popen=popen)
	[(command, kwargs)] = popen.calls
	assert command == ["py", "-m", "campy.liveview", "--cameras", "3", "--name", "rigfeed", "--fps", "10",
		"--pause-sec", "60.0", "--stop-sec", "900.0"]
	assert livefeed.FEED_ENV not in kwargs["env"] # the view's own feed is campy-acquire's, not one it would make
	assert kwargs.get("start_new_session") or kwargs.get("creationflags") # out of the console's Ctrl-C
	assert said == ["All 3 cameras in one campy-live-view window, at 10 fps (displayFrameRate; 0 for none). Its "
		"output: {}".format(tmp_path / "campy-live-view.log")]
	view.Stop()
	assert popen.process.closed and livefeed.FEED_ENV not in os.environ


@pytest.mark.parametrize("why", ["displayFrameRate 0", "campy-session"])
def test_no_view_at_rate_0_or_under_campy_session(tmp_path, monkeypatch, why):
	if why == "campy-session":
		monkeypatch.setenv(manifest.LAUNCHER_ENV, "campy-launcher/1")
		p = params()
	else:
		p = params(displayFrameRate=0)
	monkeypatch.setattr(acquireview, "Python", lambda: ("py", None))
	said, popen = [], Popen()
	view = acquireview.View(p, str(tmp_path), say=said.append).Prepare().Start(popen=popen)
	view.Stop()
	assert popen.calls == [] and said == [] and not view.on
	assert livefeed.FEED_ENV not in os.environ and p["liveFeed"] is False


def test_a_view_that_cannot_start_is_said_once_and_the_recording_goes_on(tmp_path, monkeypatch):
	monkeypatch.setattr(acquireview, "Python", lambda: (None, "this environment has no OpenCV"))
	said = []
	view = acquireview.View(params(), str(tmp_path), say=said.append).Prepare().Start(popen=Popen())
	view.Check()
	view.Stop()
	assert said == ["No campy-live-view window: this environment has no OpenCV. Recording without it."]


def test_a_view_that_stops_early_is_said_once(tmp_path, monkeypatch):
	monkeypatch.setattr(acquireview, "Python", lambda: ("py", None))
	(tmp_path / "campy-live-view.log").write_text("Cannot open a window (no desktop).\n")
	said, popen = [], Popen(code=1)
	view = acquireview.View(params(), str(tmp_path), say=said.append).Prepare()
	view.Start(popen=popen)
	view.Check()
	view.Check()
	view.Stop()
	assert said[1:] == ["campy-live-view stopped (exit code 1): Cannot open a window (no desktop).. Recording goes "
		"on without it; see {}.".format(tmp_path / "campy-live-view.log")]


def test_the_view_closed_by_hand_or_at_the_end_is_not_a_problem(tmp_path, monkeypatch):
	monkeypatch.setattr(acquireview, "Python", lambda: ("py", None))
	said, popen = [], Popen(code=0)
	view = acquireview.View(params(), str(tmp_path), say=said.append).Prepare()
	view.Start(popen=popen)
	view.Check()
	assert len(said) == 1 # its start only
	popen.code = 1
	view.Stop()
	view.Check()
	assert len(said) == 1


def test_the_environment_switch_turns_the_feed_on(monkeypatch):
	cam = {"liveFeed": False, "cameraName": "Camera0", "n_cam": 0, "liveFeedName": name(), "pixelFormatInput": "gray",
		"frameHeight": 4, "frameWidth": 6}
	assert not livefeed.Feed(cam).on
	monkeypatch.setenv(livefeed.FEED_ENV, "1")
	feed = livefeed.Feed(cam, say=lambda text: None)
	try:
		assert feed.on
		feed.Prepare(cam)
		assert feed.writer.ring.shape == (4, 6, 1)
	finally:
		feed.Close()


def test_its_python_has_opencv(monkeypatch):
	# This environment's, with OpenCV; else campy-live's registered one; else none, and why
	from campy import session
	monkeypatch.setattr(acquireview.importlib.util, "find_spec", lambda m: object())
	assert acquireview.Python()[0]
	monkeypatch.setattr(acquireview.importlib.util, "find_spec", lambda m: None)
	monkeypatch.setattr(session, "LivePython", lambda live: (sys.executable, "registered"))
	assert acquireview.Python() == (sys.executable, None)
	def Unknown(live):
		raise session.SessionError("not registered")
	monkeypatch.setattr(session, "LivePython", Unknown)
	python, why = acquireview.Python()
	assert python is None and "no OpenCV" in why


def test_campy_acquire_opens_one_view_that_reads_every_cameras_feed(tmp_path):
	# A real recording: 2 emulated cameras, liveFeed left False, displayFrameRate 5. Its campy-live-view, in Qt's
	# window (on Qt's offscreen platform here) drawn with this environment's OpenCV (headless in the tests), reads
	# both cameras' feeds, which the camera processes made from the environment.
	pytest.importorskip("cv2")
	pytest.importorskip("PyQt5")
	from test_liveviewqt import OFFSCREEN, QtStarts
	if not QtStarts():
		pytest.skip("Qt's offscreen platform does not start here")
	names = ["Camera0", "Camera1"]
	for camera in names:
		(tmp_path / camera).mkdir()
		write_clip(str(tmp_path / camera / "clip.mp4"), frames=7)
	feed = "cv{}".format(os.getpid() % 100000)
	config = tmp_path / "config.yaml"
	config.write_text("\n".join([
		"numCams: 2", 'cameraMake: "emu"', 'videoFolder: "{}"'.format(tmp_path.as_posix()), 'videoFilename: "emuclip.mp4"',
		"frameRate: 20", "recTimeInSec: 4", "chunkLengthInSec: 1", "displayFrameRate: 5",
		'pixelFormatInput: "gray"', 'pixelFormatOutput: "yuv420p"', "gpuID: -1", 'codec: "h264"',
		'preflight: "off"', 'liveFeedName: "{}"'.format(feed), ""]))
	env = dict(OFFSCREEN, PYTHONPATH=REPO, PYTHONUNBUFFERED="1")
	for key in (manifest.LAUNCHER_ENV, livefeed.FEED_ENV, "IMAGEIO_FFMPEG_EXE"):
		env.pop(key, None)
	code = ("import sys; sys.argv = ['campy-acquire', sys.argv[1]]\n"
		"from campy import acquireview\n"
		"args = acquireview.Args\n"
		"acquireview.Args = lambda p: args(p) + ['--gui', 'qt', '--priority', 'normal']\n"
		"from campy.campy import Main; Main()")
	proc = subprocess.run([sys.executable, "-c", code, str(config)], cwd=str(tmp_path), env=env,
		capture_output=True, text=True, timeout=120)
	out = proc.stdout
	assert proc.returncode == 0, out + proc.stderr
	session, = [d for d in tmp_path.iterdir() if re.fullmatch(r"\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d", d.name)]
	assert "All 2 cameras in one campy-live-view window, at 5 fps" in out, out
	for camera in names:
		assert re.search(r"{}: live feed in shared memory {}-\d".format(camera, feed), out), out
	log = (session / "campy-live-view.log").read_text()
	assert re.search(r"Live feed: Camera0 64x48, Camera1 64x48", log), log
	assert "No Qt here" not in log and "Cannot open a window" not in log, log
	assert "campy-live-view stopped" not in out
