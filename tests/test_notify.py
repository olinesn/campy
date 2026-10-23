"""
campy's alerts (campy/notify.py): Slack direct messages and a healthchecks.io
heartbeat, sent to a stand-in for both services on this computer. A problem
is said once while it lasts, reminded every half hour, said when resolved
(its first message edited to say so); one that comes and goes is said once;
past 20 an hour, the rest go into a digest. A service that fails is tried
again, a token it refuses is not, and the caller never waits for either.
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from campy import notify

CHECK = "0c5e9a8e-1234-4abc-9def-0123456789ab"


class Service:
	# Slack's Web API and webhooks, and healthchecks.io's pings and API, as a local HTTP server
	def __init__(self):
		self.requests = [] # (path, headers, body)
		self.fail = [] # statuses (or Slack error names) the next requests get, in order
		self.delay = 0.0
		self.ts = 0
		service = self

		class Handler(BaseHTTPRequestHandler):
			def log_message(self, *args):
				pass

			def do_POST(self):
				size = int(self.headers.get("Content-Length") or 0)
				raw = self.rfile.read(size) if size else b""
				body = json.loads(raw) if raw else None
				service.requests.append((self.path, dict(self.headers), body))
				if service.delay:
					time.sleep(service.delay)
				failure = service.fail.pop(0) if service.fail else None
				if isinstance(failure, int):
					self.send_response(failure)
					if failure == 429:
						self.send_header("Retry-After", "0")
					self.end_headers()
					return
				if self.path.startswith("/api/chat."):
					service.ts += 1
					answer = {"ok": False, "error": failure} if failure else {"ok": True, "channel": "D042",
						"ts": "1700000000.{:06d}".format(service.ts)}
					text = json.dumps(answer)
				elif self.path.startswith("/hook"):
					text = failure or "ok"
				else:
					text = "OK"
				self.send_response(200)
				self.end_headers()
				self.wfile.write(text.encode())

		self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
		self.url = "http://127.0.0.1:{}".format(self.server.server_address[1])
		threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

	def Paths(self):
		return [r[0] for r in self.requests]

	def Texts(self, method="chat.postMessage"):
		return [r[2]["text"] for r in self.requests if r[0] == "/api/" + method]


@pytest.fixture
def service():
	s = Service()
	yield s
	s.server.shutdown()


@pytest.fixture(autouse=True)
def quick(monkeypatch):
	monkeypatch.setattr(notify, "RETRY_S", (0, 0, 0))


class Clock:
	def __init__(self):
		self.now = 1000.0

	def __call__(self):
		return self.now


def Bot(service, level=notify.WARNING):
	return notify.SlackBot("xoxb-secret-token", "U0123ABCDEF", level, api=service.url + "/api/")


def Make(service, clock=None, channels=None, **kwargs):
	said = []
	n = notify.Notifier("Test rig", channels=channels if channels is not None else [Bot(service)],
		clock=clock or Clock(), background=False, say=said.append, **kwargs)
	n.said = said
	return n


# ---------------------------------------------------------------------- the alerts file

def test_no_file_no_alerts(tmp_path, monkeypatch):
	monkeypatch.setenv("CAMPY_ALERTS_FILE", str(tmp_path / "none.yaml"))
	assert notify.LoadSettings() is None
	n = notify.Notifier("rig", background=False, say=lambda t: None)
	assert not n.on
	n.Problem("k", "nothing listens") # does nothing, and doesn't fail


def test_file_settings_and_channels(tmp_path, monkeypatch):
	path = tmp_path / "alerts.yaml"
	path.write_text("rig: Dell Pro Max Slim FCS1250\n"
		"slack:\n"
		"  - token: xoxb-1\n    user: U1\n"
		"  - webhook: https://hooks.slack.com/services/T/B/x\n    min_severity: critical\n"
		"  - token: xoxb-2\n"
		"healthchecks:\n  ping_url: https://hc-ping.com/{}\n  api_key: k\n".format(CHECK))
	said = []
	settings = notify.LoadSettings(str(path), say=said.append)
	channels, heartbeat = notify.Channels(settings, said.append)
	assert [type(c).__name__ for c in channels] == ["SlackBot", "SlackWebhook"]
	assert channels[0].to == "U1" and channels[0].min == notify.WARNING and channels[1].min == notify.CRITICAL
	assert heartbeat.check == CHECK
	assert any("slack entry 3" in s for s in said) # a token with no user or channel: left out, and said
	if os.name == "posix":
		os.chmod(path, 0o644)
		said.clear()
		notify.LoadSettings(str(path), say=said.append)
		assert any("chmod 600" in s for s in said)
		os.chmod(path, 0o600)
		said.clear()
		notify.LoadSettings(str(path), say=said.append)
		assert not said
	monkeypatch.setenv("CAMPY_ALERTS_FILE", str(path))
	assert notify.Notifier(background=False, say=lambda t: None).source == "Dell Pro Max Slim FCS1250"


def test_bad_file(tmp_path):
	path = tmp_path / "alerts.yaml"
	path.write_text("- just\n- a list\n")
	with pytest.raises(ValueError):
		notify.LoadSettings(str(path))


def test_check_id():
	assert notify.CheckId("https://hc-ping.com/{}".format(CHECK)) == CHECK
	assert notify.CheckId("https://hc-ping.com/{}/".format(CHECK)) == CHECK
	assert notify.CheckId("https://hc-ping.com/pingkey/campy-evo") is None


# ---------------------------------------------------------------------- problems and resolutions

def test_problem_once_reminded_resolved(service):
	clock = Clock()
	n = Make(service, clock)
	n.Problem("cam1/no-frames", "Camera 1: no frame for 10 s", notify.CRITICAL)
	clock.now += 5
	n.Problem("cam1/no-frames", "Camera 1: no frame for 15 s", notify.CRITICAL) # the same: nothing sent
	assert service.Texts() == [":red_circle: *Test rig*: Camera 1: no frame for 10 s"]
	clock.now += notify.REMIND_S
	n.Tick()
	assert service.Texts()[-1] == ":red_circle: *Test rig*: still, for 30 min: Camera 1: no frame for 15 s"
	clock.now += 100
	n.Tick()
	assert len(service.Texts()) == 2 # one reminder a REMIND_S
	n.Resolved("cam1/no-frames", "Camera 1: frames again")
	assert service.Texts()[-1] == ":white_check_mark: *Test rig*: resolved, after 31 min: Camera 1: frames again"
	# The first message, edited to say it is resolved
	(update,) = [r[2] for r in service.requests if r[0] == "/api/chat.update"]
	assert update["ts"] == "1700000000.000001" and update["channel"] == "D042"
	assert update["text"].startswith("~:red_circle: *Test rig*: Camera 1: no frame for 10 s~ :white_check_mark:")
	assert n.Open() == []
	n.Resolved("cam1/no-frames") # again: nothing
	n.Resolved("never-said")
	assert len(service.Texts()) == 3
	# Bearer token in the header, never in the text
	assert all(r[1]["Authorization"] == "Bearer xoxb-secret-token" for r in service.requests)
	assert not any("xoxb" in t for t in service.Texts())


def test_hold_and_return(service):
	clock = Clock()
	n = Make(service, clock, hold=60)
	n.Problem("k", "write queue at 40 %")
	n.Resolved("k")
	clock.now += 30
	n.Tick()
	n.Problem("k", "write queue at 50 %") # back within the hold: still the same problem, nothing new
	clock.now += 100
	n.Tick()
	assert len(service.Texts()) == 1
	n.Resolved("k")
	clock.now += 61
	n.Tick()
	assert service.Texts()[-1].endswith("resolved, after 2 min 10 s: write queue at 50 %")


def test_worse_is_said_again(service):
	n = Make(service)
	n.Problem("q", "write queue at 30 %", notify.WARNING)
	n.Problem("q", "write queue at 80 %", notify.CRITICAL)
	n.Problem("q", "write queue at 70 %", notify.WARNING) # not worse than critical
	assert service.Texts() == [":large_orange_circle: *Test rig*: write queue at 30 %",
		":red_circle: *Test rig*: worse: write queue at 80 %"]


def test_severity_per_channel(service):
	critical = notify.SlackBot("xoxb-c", "U-critical", notify.CRITICAL, api=service.url + "/api/")
	everything = notify.SlackBot("xoxb-i", "U-info", notify.INFO, api=service.url + "/api/")
	n = Make(service, channels=[critical, everything])
	n.Note("Recording started: 3 cameras at 30 fps")
	n.Problem("d", "Camera 2 dropped 40 frames", notify.WARNING)
	n.Problem("f", "Camera 2: ffmpeg stopped", notify.CRITICAL)
	to = [(r[2]["channel"], r[2]["text"].split("*Test rig*: ", 1)[1]) for r in service.requests]
	assert to == [("U-info", "Recording started: 3 cameras at 30 fps"), ("U-info", "Camera 2 dropped 40 frames"),
		("U-critical", "Camera 2: ffmpeg stopped"), ("U-info", "Camera 2: ffmpeg stopped")]
	assert service.Texts()[0].startswith(":information_source: ")


def test_below_every_channel_not_resolved(service):
	n = Make(service, channels=[Bot(service, notify.CRITICAL)])
	n.Problem("w", "a warning", notify.WARNING)
	n.Resolved("w")
	assert service.requests == [] # never said, so its resolution isn't either


def test_flapping(service):
	clock = Clock()
	n = Make(service, clock)
	for _ in range(notify.FLAP_TIMES):
		n.Problem("u", "Camera 3 at USB 2")
		clock.now += 60
		n.Resolved("u")
		clock.now += 60
		n.Tick()
	texts = service.Texts()
	assert len([t for t in texts if t.endswith("*: Camera 3 at USB 2")]) == 2 # the first two times
	assert "keeps coming and going (3 times in 10 min): Camera 3 at USB 2" in texts[-1]
	assert len([t for t in texts if "resolved" in t]) == 2 # the third resolution waits for FLAP_S
	clock.now += notify.FLAP_S
	n.Tick()
	assert "resolved" in service.Texts()[-1] and len(service.Texts()) == len(texts) + 1


def test_cap_and_digest(service):
	clock = Clock()
	n = Make(service, clock)
	for i in range(notify.CAP_PER_HOUR + 5):
		n.Problem("k{}".format(i), "problem {}".format(i))
	assert len(service.Texts()) == notify.CAP_PER_HOUR
	clock.now += notify.DIGEST_S
	n.Tick()
	digest = service.Texts()[-1]
	assert digest.startswith(":large_orange_circle: 5 more alerts this hour, past the limit of 20 an hour:")
	assert "- *Test rig*: problem 24" in digest
	# Problems held for the digest are resolved like any other: past the cap, in the next digest
	n.Resolved("k24")
	assert len(service.Texts()) == notify.CAP_PER_HOUR + 1
	assert n.digest == [(notify.WARNING, "*Test rig*: resolved, after 15 min: problem 24")]
	clock.now += 3600
	n.Resolved("k3")
	resolved, still, digest = service.Texts()[-3:]
	assert resolved == ":white_check_mark: *Test rig*: resolved, after 1 h 15 min: problem 3"
	# The other 23, reminded in one message
	assert still.startswith(":large_orange_circle: *Test rig*: 23 problems still there:\n- problem 0 (for 1 h 15 min)")
	assert digest.startswith(":large_orange_circle: 1 more alert this hour")


# ---------------------------------------------------------------------- failures to send

def test_retry_then_sent(service):
	n = Make(service)
	service.fail = [500, 429]
	n.Problem("k", "Camera 1: no frames")
	assert service.Paths() == ["/api/chat.postMessage"] * 3
	assert n.said == []


def test_final_error_not_retried(service, tmp_path):
	n = Make(service)
	n.SetLog(str(tmp_path / "alerts.log"))
	service.fail = ["invalid_auth"]
	n.Problem("k", "Camera 1: no frames")
	assert service.Paths() == ["/api/chat.postMessage"]
	assert n.said == ["Alerts: could not send to Slack U0123ABCDEF: Slack's chat.postMessage: invalid_auth"]
	log = (tmp_path / "alerts.log").read_text()
	assert "FAILED on Slack U0123ABCDEF" in log and "Camera 1: no frames" in log and "xoxb" not in log


def test_unreachable_said(service):
	n = Make(service, channels=[notify.SlackBot("xoxb-x", "U1", api="http://127.0.0.1:9/api/")])
	n.Problem("k", "text")
	assert len(n.said) == 1 and n.said[0].startswith("Alerts: could not send to Slack U1: ")


def test_webhook(service):
	n = Make(service, channels=[notify.SlackWebhook(service.url + "/hook")])
	n.Problem("k", "Camera 1: no frames")
	n.Resolved("k")
	assert [r[2]["text"] for r in service.requests] == [":large_orange_circle: *Test rig*: Camera 1: no frames",
		":white_check_mark: *Test rig*: resolved, after 0 s: Camera 1: no frames"] # nothing to edit
	service.fail = ["no_service"]
	n.Note("x", notify.WARNING)
	assert n.said[-1].startswith("Alerts: could not send to Slack webhook")


def test_background_never_waits(service):
	service.delay = 1.0
	n = notify.Notifier("rig", channels=[Bot(service)], say=lambda t: None)
	t0 = time.monotonic()
	n.Problem("k", "slow network")
	n.Note("and a note", notify.WARNING)
	assert time.monotonic() - t0 < 0.3
	assert n.Flush(10)
	assert len(service.Texts()) == 2


# ---------------------------------------------------------------------- heartbeat

def test_heartbeat(service):
	clock = Clock()
	hc = notify.Healthchecks(service.url + "/" + CHECK, "api-key", api=service.url + "/api/v3/")
	n = Make(service, clock, channels=[], heartbeat=hc)
	assert n.on
	n.Heartbeat()
	clock.now += 30
	n.Heartbeat() # too soon
	clock.now += 31
	n.Heartbeat()
	n.Pause()
	assert service.Paths() == ["/" + CHECK, "/" + CHECK, "/api/v3/checks/{}/pause".format(CHECK)]
	assert service.requests[-1][1]["X-Api-Key"] == "api-key"
	n.Heartbeat() # after a pause, at once: the next recording
	assert service.Paths()[-1] == "/" + CHECK


def test_heartbeat_without_key_not_paused(service):
	hc = notify.Healthchecks(service.url + "/pingkey/campy-evo")
	assert hc.Pause() is False and service.requests == []


# ---------------------------------------------------------------------- between runs, and campy-notify

def test_state_between_runs(service, tmp_path):
	state = str(tmp_path / "state.json")
	clock = Clock()
	Make(service, clock, state=state).Problem("drift/evo", "EVO-X2's rig check: 3 % slower")
	clock.now += 86400
	again = Make(service, clock, state=state)
	again.Problem("drift/evo", "EVO-X2's rig check: 3 % slower") # still: not said again (a reminder is due)
	assert service.Texts()[0].endswith("3 % slower") and "still, for 24 h 0 min" in service.Texts()[-1]
	clock.now += 86400
	Make(service, clock, state=state).Resolved("drift/evo", "EVO-X2's rig check: back to normal")
	assert service.Texts()[-1].endswith("resolved, after 48 h 0 min: EVO-X2's rig check: back to normal")
	(update,) = [r[2] for r in service.requests if r[0] == "/api/chat.update"] # the first night's message, edited
	assert update["ts"] == "1700000000.000001"
	assert json.load(open(state))["alerts"] == {}


def test_campy_notify(service, tmp_path, capsys):
	path = tmp_path / "alerts.yaml"
	path.write_text(json.dumps({"rig": "Precision 7875", "slack": {"token": "xoxb-t", "user": "U9",
		"api": service.url + "/api/"}, "healthchecks": {"ping_url": service.url + "/" + CHECK, "api_key": "k",
		"api_url": service.url + "/api/v3/"}}))
	os.chmod(path, 0o600)
	assert notify.Main(["--file", str(path), "--test"]) == 0
	out = capsys.readouterr().out
	assert "Slack U9: sent." in out and "healthchecks.io: pinged, then paused." in out
	assert service.Texts() == [":information_source: *Precision 7875*: campy test message: alerts from this "
		"computer reach you here."]
	state = str(tmp_path / "state.json")
	assert notify.Main(["--file", str(path), "--state", state, "--problem", "k", "rig check failed"]) == 0
	assert notify.Main(["--file", str(path), "--state", state, "--problem", "k", "rig check failed"]) == 0
	assert notify.Main(["--file", str(path), "--state", state, "--resolved", "k", "rig", "check", "passed"]) == 0
	assert service.Texts()[1:] == [":large_orange_circle: *Precision 7875*: rig check failed",
		":white_check_mark: *Precision 7875*: resolved, after 0 s: rig check passed"]
	service.fail = ["invalid_auth"]
	assert notify.Main(["--file", str(path), "--test"]) == 1
	assert notify.Main(["--file", str(tmp_path / "none.yaml"), "--test"]) == 1
	assert "No alerts file" in capsys.readouterr().out
