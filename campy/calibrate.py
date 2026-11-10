"""
campy-calibrate: the cameras' calibration from a calibration recording, made
by calibrat3 (talmolab/calibrat3, the browser ChArUco calibration app) run
headless in this computer's own Chrome, with no one at the page.

    campy-calibrate <recording folder> --calibrat3 <calibrat3 checkout>

The folder is a campy calibration recording: each camera's video is in
<camera>/calibration_images/ (a videoFilename with "calibration" in it, as
docs/calibration.md says), so calibrat3 opens it as it opens a session
folder chosen on its page. Then it does what a person does on the page:
detect the board in --target sampled frames, each camera's intrinsics, the
cameras' positions (extrinsics), and the bundle adjustment (SBA). It writes
into the folder:

  board.toml               the board (unless one is there), which calibrat3
                           reads from beside the videos
  calibration.toml         anipose's format, as calibrat3's Export writes it;
                           campy-live --calibration reads it
  calibration_summary.json each camera's reprojection error (median, p95, in
                           pixels), the frames where it saw the board, its
                           intrinsics' RMS; ok: whether every camera made it
                           into calibration.toml
  logs/calibrate.log       what calibrat3 did, step by step

campy-session runs it as the post step "calibrate" (post: calibrate:
{calibrat3: <checkout>}), after remux and align. A live: calibration: that
names a folder of such recordings uses the newest one's calibration.toml
(Newest).

Needs: a calibrat3 checkout; Playwright's Python package in this environment
(pip install playwright; it drives the installed Chrome, --browser chrome, so
no browser download); Chrome, which decodes the recordings' H.265 and AV1
(Playwright's own Chromium has no H.265).
"""

import argparse, datetime, functools, importlib.util, os, sys, threading, time

from campy import newfiles, strictjson

SUMMARY = "calibration_summary.json"
CALIBRATION = "calibration.toml"
BOARD = "board.toml"
LOG = "calibrate.log"

# The lab's board (calib.io's 300 x 200 mm ChArUco, docs/calibration.md): 14 x 9 squares of 20 mm, 15 mm markers,
# DICT_5X5_1000. calibrat3 reads board.toml's keys, as sleap-anipose writes them
DEFAULT_BOARD = {"board_x": 14, "board_y": 9, "square_length": 20.0, "marker_length": 15.0, "marker_bits": 5,
	"dict_size": 1000}
DEFAULT_TARGET = 300 # frames sampled for the board: of a 60 s recording at 10 fps, every other one


class CalibrateError(Exception):
	pass


def Stamp():
	return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def CalibrationVideos(folder):
	"""{camera: its calibration video} in a recording folder (newfiles' layout)."""
	found = {}
	try:
		names = sorted(os.listdir(folder))
	except OSError:
		return found
	for name in names:
		sub = os.path.join(folder, name, newfiles.CALIBRATION_FOLDER)
		if not os.path.isdir(sub):
			continue
		videos = sorted(v for v in os.listdir(sub) if newfiles.CALIBRATION_WORD in v.lower() and
			v.lower().endswith((".mp4", ".mov", ".m4v")))
		if videos:
			found[name] = os.path.join(sub, videos[0])
	return found


# Where Playwright's "chrome" channel finds Google Chrome, as Playwright itself looks for it
CHROME = {
	"win32": [os.path.join(os.environ.get(v, ""), "Google", "Chrome", "Application", "chrome.exe")
		for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")],
	"darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
	"linux": ["/opt/google/chrome/chrome"],
	}


def ChromeFound():
	"""Google Chrome's path, as Playwright's chrome channel finds it, or None."""
	for path in CHROME.get(sys.platform if sys.platform in CHROME else "linux", []):
		if path and os.path.isfile(path):
			return path
	return None


def Missing(calibrat3, browser="chrome", playwright=None, chrome=ChromeFound):
	"""
	What this computer lacks to make a calibration (Calibrate): [text], empty when nothing; before a recording
	starts, so that a calibration recording isn't made only to find out afterwards (the panel's checks).
	"""
	missing = []
	if not calibrat3 or not os.path.isfile(os.path.join(str(calibrat3), "index.html")):
		missing.append("calibrat3: no checkout at {} (git clone https://github.com/talmolab/calibrat3, and give its "
			"folder as post: calibrate: calibrat3:)".format(calibrat3 or "(none given)"))
	if playwright is None:
		playwright = importlib.util.find_spec("playwright") is not None
	if not playwright:
		missing.append("Playwright's Python package, in this environment ({}): pip install playwright".format(
			sys.executable))
	if browser == "chrome" and chrome() is None:
		missing.append("Google Chrome (Playwright drives the installed Chrome: it decodes the recordings' H.265 and AV1)")
	return missing


def BoardToml(board):
	b = dict(DEFAULT_BOARD, **(board or {}))
	return "".join("{} = {}\n".format(k, b[k]) for k in ("board_x", "board_y", "square_length", "marker_length",
		"marker_bits", "dict_size"))


def Newest(where):
	"""
	The calibration.toml to use for where: where itself when it is a file; for a folder, the newest of its
	recordings (by folder name, which starts with the date and time) whose calibration_summary.json says ok, with
	that summary. (path, summary or None), or (None, None) when there is none.
	"""
	where = str(where or "")
	if os.path.isfile(where):
		summary = None
		try:
			with open(os.path.join(os.path.dirname(where), SUMMARY)) as f:
				summary = strictjson.load(f)
		except (OSError, ValueError):
			pass
		return where, summary
	try:
		names = sorted(os.listdir(where), reverse=True)
	except OSError:
		return None, None
	for name in names:
		path = os.path.join(where, name, CALIBRATION)
		try:
			with open(os.path.join(where, name, SUMMARY)) as f:
				summary = strictjson.load(f)
		except (OSError, ValueError):
			continue
		if summary.get("ok") and os.path.isfile(path):
			return path, summary
	return None, None


def Describe(summary):
	"""One line on a calibration: when, and each camera's median reprojection error, the worst named."""
	if not summary:
		return "no calibration_summary.json beside it"
	cams = summary.get("cameras") or {}
	errors = {n: c.get("median_px") for n, c in cams.items() if c.get("median_px") is not None}
	text = "made {}".format(When(summary.get("made")))
	if errors:
		worst = max(errors, key=errors.get)
		text += ", {} cameras, reprojection median {:.2f}-{:.2f} px (worst {})".format(len(errors),
			min(errors.values()), max(errors.values()), worst)
	return text


def When(stamp):
	# An ISO time as people read it on the panel: 9 Nov 5:05 PM
	try:
		t = datetime.datetime.fromisoformat(str(stamp))
	except ValueError:
		return str(stamp or "?")
	return "{} {} {}:{:02d} {}".format(t.day, t.strftime("%b"), t.hour % 12 or 12, t.minute, "AM" if t.hour < 12 else "PM")


def AgeHours(summary, now=None):
	try:
		made = datetime.datetime.fromisoformat(summary["made"])
	except (KeyError, TypeError, ValueError):
		return None
	now = now or datetime.datetime.now().astimezone()
	return (now - made).total_seconds() / 3600


# ------------------------------------------------------------------ calibrat3, served here

def Server(root):
	"""calibrat3's own static server (its server.py: Range requests, which its video reader needs) on a free port."""
	spec = importlib.util.spec_from_file_location("calibrat3_server", os.path.join(root, "server.py"))
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)

	class Quiet(module.Handler):
		def log_message(self, *args):
			pass

	from http.server import ThreadingHTTPServer
	server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=root))
	server.daemon_threads = True
	threading.Thread(target=server.serve_forever, name="calibrat3 server", daemon=True).start()
	return server, "http://127.0.0.1:{}".format(server.server_address[1])


def Calibrate(folder, calibrat3, board=None, target=DEFAULT_TARGET, browser="chrome", headful=False, say=print,
		force=False):
	"""
	Calibrates the cameras of the calibration recording in folder (the module's doc). Returns the summary;
	raises CalibrateError when it can't, saying why.
	"""
	folder = os.path.abspath(folder)
	videos = CalibrationVideos(folder)
	if len(videos) < 2:
		raise CalibrateError("{} has {} calibration video{} (<camera>/{}/*calibration*.mp4): at least 2 cameras are "
			"needed".format(folder, len(videos), "" if len(videos) == 1 else "s", newfiles.CALIBRATION_FOLDER))
	out = os.path.join(folder, CALIBRATION)
	if os.path.exists(out) and not force:
		raise CalibrateError("{} is there already: --force makes it again".format(out))
	if not calibrat3 or not os.path.isfile(os.path.join(calibrat3, "index.html")):
		raise CalibrateError("no calibrat3 checkout at {!r} (post: calibrate: calibrat3: <folder with its "
			"index.html>)".format(calibrat3))
	try:
		from playwright.sync_api import sync_playwright
	except ImportError:
		raise CalibrateError("Playwright's Python package isn't in this environment ({}): pip install playwright "
			"(it drives the installed Chrome; no browser download needed)".format(sys.executable))
	if not os.path.exists(os.path.join(folder, BOARD)):
		with open(os.path.join(folder, BOARD), "w") as f:
			f.write(BoardToml(board))
	server, base = Server(calibrat3)
	t0 = time.monotonic()

	def step(text):
		say("[{:.0f} s] {}".format(time.monotonic() - t0, text))

	summary = {"schema": 1, "made": None, "folder": folder, "calibrat3": os.path.abspath(calibrat3), "target": target,
		"board": dict(DEFAULT_BOARD, **(board or {})), "cameras": {}, "ok": False}
	try:
		with sync_playwright() as p:
			kind = p.chromium
			try:
				browser_ = kind.launch(channel=browser or None, headless=not headful)
			except Exception as e:
				raise CalibrateError("could not start {} for calibrat3: {}".format(browser or "Playwright's Chromium",
					str(e).splitlines()[0]))
			try:
				page = browser_.new_page(viewport={"width": 1700, "height": 1050})
				errors = []
				page.on("pageerror", lambda e: errors.append(str(e)))

				def wait(predicate, timeout_s, what):
					# Until predicate holds on the page; calibrat3's error banner, or the time, ends it
					deadline = time.monotonic() + timeout_s
					while time.monotonic() < deadline:
						banner = page.evaluate("() => { const e = document.getElementById('errorMsg'); return e && "
							"e.style.display === 'block' ? e.textContent : null; }")
						if banner:
							raise CalibrateError("calibrat3, {}: {}".format(what, banner.strip()))
						if page.evaluate(predicate):
							return
						time.sleep(0.5)
					raise CalibrateError("calibrat3, {}: not done after {:.0f} min".format(what, timeout_s / 60))

				page.goto(base + "/index.html", wait_until="load")
				wait("() => document.getElementById('workerStatus').classList.contains('ok')", 180, "starting")
				page.set_input_files("#folderInput", folder)
				wait("() => window.__calibrat3.state.views.length >= 2 && window.__calibrat3.state.totalFrames > 0",
					600, "opening the videos")
				info = page.evaluate("() => { const s = window.__calibrat3.state; return {views: s.views.map(v => "
					"v.name), frames: s.totalFrames, fps: s.fps, board: s.board}; }")
				step("{} cameras ({}), {} frames at {} fps".format(len(info["views"]), ", ".join(info["views"]),
					info["frames"], info["fps"]))
				missing = sorted(set(videos) - set(info["views"]))
				if missing:
					raise CalibrateError("calibrat3 didn't open {}'s video".format(", ".join(missing)))
				page.fill("#targetSamples", str(int(target)))
				page.dispatch_event("#targetSamples", "input")
				# Done once detection has run and left a new detections store (empty too: no board anywhere)
				page.evaluate("() => { window.__campyBefore = window.__calibrat3.state.detections; }")
				page.click("#runDetectionBtn")
				wait("() => !window.__calibrat3.state.detectionRunning && window.__calibrat3.state.detections && "
					"window.__calibrat3.state.detections !== window.__campyBefore", 3600, "finding the board")
				found = page.evaluate("() => window.__calibrat3.state.detections.summary(6)")
				step("board found: {}".format(", ".join("{} {}".format(n, k) for n, k in zip(info["views"],
					found.get("perView") or []))))
				blind = [name for name, n in zip(info["views"], found.get("perView") or []) if not n]
				if blind:
					raise CalibrateError("the board was found in no frame of {}: was it in view, and is the board "
						"right ({})?".format(", ".join(blind), BoardToml(board).replace("\n", ", ").rstrip(", ")))
				page.click("#computeIntrinsicsBtn")
				wait("() => document.getElementById('computeIntrinsicsBtn').disabled === false && "
					"window.__calibrat3.state.intrinsics.length > 0", 3600, "intrinsics")
				intrinsics = page.evaluate("() => window.__calibrat3.state.intrinsics.map(r => r ? {rms: r.rmsError, "
					"used: r.framesUsed} : null)")
				page.click("#computeExtrinsicsBtn")
				wait("() => window.__calibrat3.state.reproj !== null", 3600, "the cameras' positions")
				extrinsics = page.evaluate("() => window.__calibrat3.state.extrinsics.map(e => e && e.error ? "
					"e.error : null)")
				step("positions: median reprojection {:.2f} px".format(page.evaluate(
					"() => window.__calibrat3.state.reproj.summary.overall.median")))
				page.wait_for_selector("#runSbaBtn:not([disabled])", timeout=60000)
				page.click("#runSbaBtn")
				wait("() => window.__calibrat3.state.sbaResult !== null && "
					"!document.getElementById('runSbaBtn').disabled", 3600, "the bundle adjustment")
				reproj = page.evaluate("() => window.__calibrat3.state.reproj.summary")
				toml = page.evaluate("async () => (await import('./ui/stage-export.js')).buildToml()")
				log_tail = page.evaluate("() => Array.from(document.querySelectorAll('#logBody .log-entry'))"
					".map(x => x.textContent)")
			finally:
				browser_.close()
	finally:
		server.shutdown()
		server.server_close()
	per_view = found.get("perView") or []
	for i, name in enumerate(info["views"]):
		r = (reproj.get("perView") or [None] * len(info["views"]))[i] or {}
		summary["cameras"][name] = {"median_px": r.get("median"), "p95_px": r.get("p95"),
			"board_frames": per_view[i] if i < len(per_view) else None,
			"intrinsics_rms_px": (intrinsics[i] or {}).get("rms") if i < len(intrinsics) else None,
			"error": extrinsics[i] if i < len(extrinsics) else None}
	overall = reproj.get("overall") or {}
	summary.update(made=Stamp(), seconds=round(time.monotonic() - t0, 1), median_px=overall.get("median"),
		p95_px=overall.get("p95"))
	failed = [n for n, c in summary["cameras"].items() if c["error"] or c["median_px"] is None]
	with open(os.path.join(folder, "logs", LOG) if os.path.isdir(os.path.join(folder, "logs")) else
			os.path.join(folder, LOG), "a", encoding="utf-8") as f:
		f.write("\n".join(log_tail) + "\n")
	if not toml:
		summary["why"] = "calibrat3 calibrated no camera"
	elif failed:
		summary["why"] = "not calibrated: {}".format(", ".join(failed))
	else:
		summary["ok"] = True
	if toml:
		with open(out + ".tmp", "w", encoding="utf-8") as f:
			f.write(toml)
		os.replace(out + ".tmp", out)
	with open(os.path.join(folder, SUMMARY), "w", encoding="utf-8") as f:
		strictjson.dump(summary, f, indent=1)
	step(Describe(summary) + ("" if summary["ok"] else "; " + summary["why"]))
	return summary


# ------------------------------------------------------------------ the command

def Main(argv=None):
	p = argparse.ArgumentParser(prog="campy-calibrate", description="The cameras' calibration (calibration.toml) "
		"from a campy calibration recording, by calibrat3 in this computer's Chrome, headless.")
	p.add_argument("folder", help="the calibration recording's folder")
	p.add_argument("--calibrat3", required=True, help="the calibrat3 checkout (the folder with its index.html)")
	p.add_argument("--target", type=int, default=DEFAULT_TARGET, help="frames to sample for the board")
	for key, value in DEFAULT_BOARD.items():
		p.add_argument("--" + key.replace("_", "-"), type=type(value), default=value, help="the board's " + key)
	p.add_argument("--browser", default="chrome", help="Playwright's channel: chrome (the installed Chrome, the "
		"default), msedge, or '' for Playwright's own Chromium")
	p.add_argument("--headful", action="store_true", help="show the page while it works")
	p.add_argument("--force", action="store_true", help="make calibration.toml again when there is one")
	args = p.parse_args(argv)
	board = {k: getattr(args, k) for k in DEFAULT_BOARD}
	try:
		summary = Calibrate(args.folder, args.calibrat3, board=board, target=args.target, browser=args.browser,
			headful=args.headful, force=args.force)
	except CalibrateError as e:
		print("campy-calibrate: {}".format(e), file=sys.stderr)
		return 1
	return 0 if summary["ok"] else 1


if __name__ == "__main__":
	sys.exit(Main())
