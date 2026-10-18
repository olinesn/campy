"""
A recording never replaces files that are already there.

A recording writes, for each camera, into <videoFolder>/<cameraName>/: its
video (videoFilename), frametimes.npy, hosttimes.npy, frametimes.mat and
metadata.csv, and for a RatCam ratcam_frames.csv and ratcam_sync.csv; and
trigger.json into videoFolder. imageio-ffmpeg starts ffmpeg with -y, and
campy opened the others with "w", so a second recording into the same
videoFolder replaced the first one's files without a word (the ChronicRecord
PC, 2026-10-18). Now:

- campy-acquire stops before it looks for cameras when any of those files is
  there, whatever preflight is set to, and says which and what to do (Check).
- Before the camera processes start, it makes every one of them, empty, each
  only if it is not there (Claim, O_EXCL). Of two campys started into the
  same folder, only one gets them; the other stops, and removes only the
  empty files it made itself.
- A camera process opens each file through Open, which refuses a file that
  holds anything the process did not write itself. Claim's empty files it
  takes: they hold nothing to lose. A camera process that finds its files
  taken (CheckCamera) records nothing, and says why.
- When the recording ends, the files still empty are removed (Release), so a
  camera that saved no frames leaves only what it always left.

There is no setting to turn this off: a new recording goes into a new
videoFolder, or the old one is moved away first.
"""

import os

TRIGGER_FILE = "trigger.json" # trigger.SaveStart
RATCAM_FILES = ("ratcam_frames.csv", "ratcam_sync.csv") # ratcam.Logs


class WouldOverwrite(Exception):
	"""A recording would replace files already there. Its text says which, and what to do."""


def CameraFolder(cam_params):
	# As unicam.CameraFolder
	return os.path.join(cam_params["videoFolder"], cam_params["cameraName"])


def CameraFiles(cam_params):
	# Every file one camera's recording writes
	names = [str(cam_params["videoFilename"]), "frametimes.npy", "hosttimes.npy", "frametimes.mat", "metadata.csv"]
	if cam_params.get("cameraMake") == "ratcam":
		names += list(RATCAM_FILES)
	return [os.path.join(CameraFolder(cam_params), name) for name in names]


def RecordingFiles(params):
	# Every file a recording with these params writes, as (path, n_cam), n_cam
	# None for trigger.json
	from campy import configurator # it imports the camera modules, which import this one
	files = [(os.path.join(params["videoFolder"], TRIGGER_FILE), None)]
	for n in range(params["numCams"]):
		files += [(path, n) for path in CameraFiles(configurator.CameraParams(params, n))]
	return files


def Key(path):
	return os.path.normcase(os.path.abspath(path))


def Size(path):
	try:
		return os.path.getsize(path)
	except OSError:
		return None


def Listing(paths, root):
	# The files, one line per folder, relative to root; an empty one marked so
	# (left by a campy that stopped before it wrote it)
	folders = {}
	for path in paths:
		folder, name = os.path.split(os.path.relpath(os.path.abspath(path), root))
		folders.setdefault(folder, []).append(name + (" (empty)" if Size(path) == 0 else ""))
	return "\n".join("  {}{}".format(folder + os.sep if folder else "", ", ".join(names))
		for folder, names in folders.items())


def Message(params, existing):
	root = os.path.abspath(params["videoFolder"])
	return "Stopped before recording: {} already holds a recording, and campy never replaces one. " \
		"These files are there:\n{}\nRecord into a new folder (--videoFolder, or videoFolder in the config), " \
		"or move this one away first.".format(root, Listing(existing, root))


def Check(params):
	"""
	Raises WouldOverwrite, saying why, when a recording with these params
	would replace a file: one already there, or one two cameras would both
	write (the same cameraNames). Opens nothing: campy-acquire runs it first.
	"""
	owners = {}
	names = params["cameraNames"]
	for path, n in RecordingFiles(params):
		other = owners.setdefault(Key(path), n)
		if other != n:
			who = ["trigger.json" if m is None else names[m] for m in (other, n)]
			raise WouldOverwrite("Stopped before recording: {} and {} would both write {}. Give each camera "
				"its own name (cameraNames).".format(who[0], who[1], path))
	existing = [path for path, n in RecordingFiles(params) if os.path.lexists(path)]
	if existing:
		raise WouldOverwrite(Message(params, existing))


def Claim(params):
	"""
	Makes every file of the recording, empty, each only if it is not there
	(O_EXCL), before the camera processes start. Returns them as
	RecordingFiles does. Raises WouldOverwrite if one is there, after
	removing the ones it made.
	"""
	made = []
	try:
		for path, n in RecordingFiles(params):
			os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
			os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)))
			made.append((path, n))
	except FileExistsError:
		Release(made)
		Check(params) # says what is there
		raise WouldOverwrite(Message(params, [path for path, n in RecordingFiles(params) if os.path.lexists(path)]))
	return made


def Release(files):
	# Removes the files that are still empty: made by Claim, never written
	for path, n in files:
		try:
			if os.path.getsize(path) == 0:
				os.remove(path)
		except OSError:
			pass


# The files this process has written: it may write them again (metadata.csv
# is written as the camera starts, and again with the totals)
_written = set()


def Taken(path):
	# Holds something this process did not write
	return Key(path) not in _written and bool(Size(path))


def CheckCamera(cam_params):
	# In the camera process, before its camera opens: raises WouldOverwrite
	# if any of its files holds something already
	taken = [path for path in CameraFiles(cam_params) if Taken(path)]
	if taken:
		root = os.path.abspath(cam_params["videoFolder"])
		raise WouldOverwrite("not recording: {} already holds a recording, and campy never replaces one:\n{}"
			.format(root, Listing(taken, root)))


def Open(path, mode="w", **kwargs):
	"""
	open(path, mode), for a file a recording writes, but refusing one that
	holds anything this process did not write itself (WouldOverwrite). An
	empty one, Claim's, is taken.
	"""
	if Key(path) in _written:
		return open(path, mode, **kwargs)
	try:
		f = open(path, mode.replace("w", "x"), **kwargs)
	except FileExistsError:
		if Size(path):
			raise WouldOverwrite("{} is already there, and campy never replaces a recording's files.".format(
				os.path.abspath(path)))
		f = open(path, mode, **kwargs)
	_written.add(Key(path))
	return f


def Take(path):
	# For a file another program writes (ffmpeg's video, which it opens with
	# -y): made empty here first, so that it replaces nothing
	Open(path, "wb").close()
