"""
Alerts: messages to a person, on their phone, when something goes wrong in a
recording nobody is watching, and when it is put right.

A long recording runs unattended overnight. campy prints what goes wrong (a
camera that stops sending frames, frames dropped because the encoder fell
behind, ffmpeg stopping), but nobody reads the console until the morning.
Notifier sends those problems as Slack direct messages instead, within a
minute, and says when each one is resolved.

Where to send them: a file of its own on each computer
-----------------------------------------------------
Never in campy's config: metadata.csv copies every config parameter into
each recording, and configs get committed. The file is ~/.campy/alerts.yaml
(on Windows %USERPROFILE%\\.campy\\alerts.yaml), or the file
CAMPY_ALERTS_FILE names. Without it, nothing is sent. YAML, or JSON:

    rig: Dell Pro Max Slim FCS1250   # says which computer each message is from
    slack:
      token: xoxb-...                # a Slack app's bot token (chat:write)
      user: U0123ABCDEF              # your member ID: the app messages you directly
      # channel: C0123ABCDEF         # or a channel the app is in, instead of user
      # webhook: https://hooks.slack.com/services/...   # or an incoming webhook, instead of a token
      min_severity: warning          # info, warning (the default) or critical
    healthchecks:                    # a dead-man switch (Heartbeat), optional
      ping_url: https://hc-ping.com/<uuid>
      api_key: ...                   # the project's API key: to pause the check between recordings

slack can be a list, for several people or channels. On Linux and macOS the
file should be readable by its owner only (chmod 600): campy says when it is
not.

What reaches the phone
----------------------
Each problem has a key (the computer, the camera and the kind of problem):
  - Problem(key, text): sent at once the first time; while it lasts, the same
    key again sends nothing, but a reminder every REMIND_S ("Still: ...,
    for 1 h 30 min").
  - Resolved(key): one message saying it is over and how long it lasted,
    only for a problem that was sent; the problem's first message is edited
    to say it is resolved too (with a bot token).
  - A problem that keeps coming back (FLAP_TIMES times in FLAP_S) is said
    once as coming and going, and resolved only once it has stayed away for
    FLAP_S.
  - At most CAP_PER_HOUR messages an hour; beyond that, what would have been
    sent goes into one digest every DIGEST_S.
Messages go out from a thread of their own, each request with a 10 s timeout
and a few tries: a slow or dead network never holds up the caller. What was
sent, and what failed to send, is said (say) and written to a log file
(SetLog: alerts.log in a recording's folder), one JSON object a line: time,
key, severity, event (problem, worse, flapping, still, resolved, note, ...),
state (open, resolved), status (queued, sent to ..., FAILED on ...), text.

Heartbeat (healthchecks.io)
---------------------------
No program on a computer can say that the computer itself has died (a power
cut, an update's restart, campy killed). With healthchecks set, Heartbeat()
pings the check's URL every HEARTBEAT_S while campy records; healthchecks.io
alerts when the pings stop. Pause() pauses the check as a recording ends
cleanly, so the time between recordings is not an alert; healthchecks.io
starts it again at the next ping.

Only the standard library (and PyYAML when there): campy-live's environment
imports this too.
"""

import argparse, collections, datetime, json, os, re, sys, threading, time, queue
import urllib.error, urllib.request

INFO, WARNING, CRITICAL = "info", "warning", "critical"
LEVELS = {INFO: 0, WARNING: 1, CRITICAL: 2}

REMIND_S = 1800 # a problem still there is said again this often
FLAP_S, FLAP_TIMES = 600, 3 # a problem that starts this many times in this many seconds comes and goes
CAP_PER_HOUR = 20 # messages an hour, at most; then a digest
DIGEST_S = 900 # how often the digest goes out
HEARTBEAT_S = 60 # how often Heartbeat pings
TIMEOUT_S = 10 # each request
TRIES = 3
RETRY_S = (1, 4, 10) # seconds before each next try

SLACK_API = "https://slack.com/api/"
HEALTHCHECKS_API = "https://healthchecks.io/api/v3/"
# Slack errors that trying again does not fix
SLACK_FINAL = ("not_authed", "invalid_auth", "account_inactive", "token_revoked", "token_expired", "no_permission",
	"missing_scope", "channel_not_found", "user_not_found", "not_in_channel", "is_archived", "invalid_arguments",
	"no_service", "no_service_id", "no_team", "team_disabled", "invalid_token")

STATES = {"problem": "open", "worse": "open", "flapping": "open", "still": "open", "resolved": "resolved"}

ICONS = {INFO: ":information_source:", WARNING: ":large_orange_circle:", CRITICAL: ":red_circle:",
	"resolved": ":white_check_mark:"}


class SendError(Exception):
	def __init__(self, text, final=False, wait=None):
		super().__init__(text)
		self.final = final # trying again won't help
		self.wait = wait # seconds the service asked for before the next try


def AlertsFile():
	"""The alerts file: CAMPY_ALERTS_FILE, or ~/.campy/alerts.yaml."""
	return os.environ.get("CAMPY_ALERTS_FILE") or os.path.join(os.path.expanduser("~"), ".campy", "alerts.yaml")


def LoadSettings(path=None, say=None):
	"""
	The alerts file's settings (a dict), or None when there is no file. A file
	that can't be read raises ValueError, saying why.
	"""
	path = path or AlertsFile()
	if not os.path.isfile(path):
		return None
	with open(path, encoding="utf-8") as f:
		text = f.read()
	try:
		import yaml
		settings = yaml.safe_load(text)
	except ImportError:
		settings = json.loads(text) if text.strip() else None
	except Exception as e:
		raise ValueError("{}: not YAML: {}".format(path, e))
	if settings is None:
		settings = {}
	if not isinstance(settings, dict):
		raise ValueError("{}: should be a mapping (rig:, slack:, healthchecks:)".format(path))
	if say and os.name == "posix":
		mode = os.stat(path).st_mode
		if mode & 0o077:
			say("{} can be read by other users, and it holds the alerts' tokens: chmod 600 it.".format(path))
	return settings


def Post(url, body=None, headers=None, timeout=TIMEOUT_S):
	# One HTTP request: POST JSON (or form-less POST with body None); returns (status, text). An error status
	# raises SendError, with Retry-After when the service gave one
	data = json.dumps(body).encode() if body is not None else b""
	request = urllib.request.Request(url, data=data, method="POST", headers=dict(headers or {}))
	if body is not None:
		request.add_header("Content-Type", "application/json; charset=utf-8")
	try:
		with urllib.request.urlopen(request, timeout=timeout) as response:
			return response.status, response.read().decode("utf-8", "replace")
	except urllib.error.HTTPError as e:
		text = e.read().decode("utf-8", "replace")[:200]
		wait = e.headers.get("Retry-After") if e.headers else None
		raise SendError("HTTP {}: {}".format(e.code, text or e.reason), final=e.code in (400, 401, 403, 404, 410),
			wait=float(wait) if wait and wait.isdigit() else None)
	except (urllib.error.URLError, OSError) as e:
		raise SendError(str(getattr(e, "reason", e)))


class SlackBot:
	"""A Slack app's bot token, messaging a person (their member ID) or a channel the app is in."""

	def __init__(self, token, to, min_severity=WARNING, api=SLACK_API):
		self.token, self.to, self.min, self.api = token, to, min_severity, api
		self.name = "Slack {}".format(to)

	def Call(self, method, body):
		_, text = Post(self.api + method, body, {"Authorization": "Bearer " + self.token})
		try:
			answer = json.loads(text)
		except ValueError:
			raise SendError("Slack's {} answered: {}".format(method, text[:200]))
		if not answer.get("ok"):
			error = answer.get("error", "unknown error")
			raise SendError("Slack's {}: {}".format(method, error), final=error in SLACK_FINAL)
		return answer

	def Send(self, text):
		# Returns what Update needs to edit it: (the conversation's id, the message's ts)
		answer = self.Call("chat.postMessage", {"channel": self.to, "text": text, "unfurl_links": False})
		return [answer.get("channel"), answer.get("ts")]

	def Update(self, handle, text):
		if handle and handle[0] and handle[1]:
			self.Call("chat.update", {"channel": handle[0], "ts": handle[1], "text": text})


class SlackWebhook:
	"""A Slack incoming webhook: posts to the one channel it was made for (it can't edit what it sent)."""

	def __init__(self, url, min_severity=WARNING):
		self.url, self.min = url, min_severity
		self.name = "Slack webhook"

	def Send(self, text):
		_, answer = Post(self.url, {"text": text})
		if answer.strip() != "ok":
			raise SendError("Slack's webhook answered: {}".format(answer[:200]), final=answer.strip() in (
				"invalid_token", "no_service", "channel_not_found", "channel_is_archived", "action_prohibited"))
		return None

	def Update(self, handle, text):
		pass


class Healthchecks:
	"""A healthchecks.io check: Ping while recording, Pause between recordings."""

	def __init__(self, ping_url, api_key=None, check=None, api=HEALTHCHECKS_API):
		self.ping_url, self.api_key, self.api = ping_url.rstrip("/"), api_key, api
		self.check = check or CheckId(ping_url)
		self.name = "healthchecks.io"

	def Ping(self):
		Post(self.ping_url)

	def Pause(self):
		if not (self.api_key and self.check):
			return False
		Post(self.api.rstrip("/") + "/checks/{}/pause".format(self.check), headers={"X-Api-Key": self.api_key})
		return True


def CheckId(ping_url):
	# A check's UUID from its ping URL (https://hc-ping.com/<uuid>), or None (a slug URL: give check: too)
	m = re.search(r"/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/?$", ping_url or "")
	return m.group(1) if m else None


def Channels(settings, say=None):
	"""The channels the settings name: (Slack channels, a Healthchecks or None). Bad entries are said and left out."""
	say = say or (lambda text: None)
	slack = settings.get("slack") or []
	if isinstance(slack, dict):
		slack = [slack]
	out = []
	for n, s in enumerate(slack):
		if not isinstance(s, dict):
			say("alerts file: slack entry {} is not a mapping; left out".format(n + 1))
			continue
		level = str(s.get("min_severity", WARNING)).lower()
		if level not in LEVELS:
			say("alerts file: slack min_severity {!r} is not info, warning or critical; using warning".format(level))
			level = WARNING
		if s.get("webhook"):
			out.append(SlackWebhook(str(s["webhook"]), level))
		elif s.get("token") and (s.get("user") or s.get("channel")):
			out.append(SlackBot(str(s["token"]), str(s.get("user") or s.get("channel")), level,
				str(s.get("api", SLACK_API))))
		else:
			say("alerts file: slack entry {} needs webhook:, or token: with user: or channel:; left out".format(n + 1))
	hc = settings.get("healthchecks")
	heartbeat = None
	if isinstance(hc, dict) and hc.get("ping_url"):
		heartbeat = Healthchecks(str(hc["ping_url"]), hc.get("api_key"), hc.get("check"),
			str(hc.get("api_url", HEALTHCHECKS_API)))
		if not heartbeat.check and heartbeat.api_key:
			say("alerts file: healthchecks ping_url has no check UUID in it: add check: <uuid> so it can be paused")
	elif hc:
		say("alerts file: healthchecks needs ping_url:; left out")
	return out, heartbeat


def Duration(seconds):
	s = int(round(max(0, seconds)))
	if s < 60:
		return "{} s".format(s)
	if s < 3600:
		return "{} min {} s".format(s // 60, s % 60) if s < 600 and s % 60 else "{} min".format(s // 60)
	return "{} h {} min".format(s // 3600, s // 60 % 60)


class Alert:
	def __init__(self, key, text, severity, now):
		self.key, self.text, self.severity = key, text, severity
		self.since = now # when it began (this time)
		self.said = None # when its first message went out (None: not sent: below the channels' level, or capped)
		self.reminded = now
		self.handles = {} # channel index: what its Update needs
		self.resolving = None # (when Resolved was called, its text): sent after hold
		self.first = None # its first message, as sent (the edit at its resolution strikes it through)
		self.flapping = False
		self.remind = True


class Notifier:
	"""
	Problems, resolutions and notes, to every channel the alerts file names
	(see above). channels: instead of the file's (tests). clock: time.time.
	background False: send in the caller's thread (tests, campy-notify).
	state: a JSON file that keeps the open problems between runs, for a job
	that runs once a night (campy-notify --problem/--resolved).
	"""

	def __init__(self, source=None, settings=None, channels=None, heartbeat=None, clock=time.time,
			background=True, say=None, state=None, hold=0.0):
		self.say = say or (lambda text: print(text, file=sys.stderr, flush=True))
		if settings is None and channels is None:
			try:
				settings = LoadSettings(say=self.say)
			except Exception as e:
				self.say("Alerts are off: {}".format(e))
				settings = None
		settings = settings or {}
		if channels is None:
			channels, heartbeat = Channels(settings, self.say)
		self.channels = list(channels)
		self.heartbeat = heartbeat
		self.source = source or settings.get("rig") or ""
		self.clock = clock
		self.hold = float(hold) # seconds a problem has to stay resolved before it is said to be
		self.alerts = {} # key: Alert, open (or resolving)
		self.starts = collections.defaultdict(collections.deque) # key: when it began, the last FLAP_S
		self.sent = collections.deque() # when each message went out, the last hour
		self.digest = [] # (severity, text) capped
		self.digestAt = None
		self.beatAt = None
		self.log = None
		self.lock = threading.RLock()
		self.state = state
		self.LoadState()
		self.jobs = None
		if background and (self.channels or self.heartbeat):
			self.jobs = queue.SimpleQueue()
			threading.Thread(target=self.Run, name="campy alerts", daemon=True).start()

	@property
	def on(self):
		return bool(self.channels or self.heartbeat)

	def SetLog(self, path):
		"""Write what is sent, and what fails to send, into this file too (a recording's alerts.log)."""
		self.log = path

	# ------------------------------------------------------------------ what callers say

	def Problem(self, key, text, severity=WARNING, remind=True):
		"""
		A problem, now. Sent once while it lasts (reminders aside); its text may
		change meanwhile. remind False: no reminders (a camera that has stopped
		for good: once is enough).
		"""
		severity = severity if severity in LEVELS else WARNING
		with self.lock:
			now = self.clock()
			alert = self.alerts.get(key)
			if alert is not None:
				alert.text, alert.resolving = text, None
				if LEVELS[severity] > LEVELS[alert.severity]: # worse: said again, as it is now
					alert.severity = severity
					self.Say(alert, "worse", text)
				self.SaveState()
				self.Tick()
				return
			starts = self.starts[key]
			starts.append(now)
			while starts and starts[0] < now - FLAP_S:
				starts.popleft()
			alert = self.alerts[key] = Alert(key, text, severity, now)
			alert.remind = remind
			if len(starts) >= FLAP_TIMES:
				alert.flapping = True
				self.Say(alert, "flapping", text)
			else:
				self.Say(alert, "problem", text)
			self.SaveState()
			self.Tick()

	def Resolved(self, key, text=None):
		"""The problem is over: said (after hold, or FLAP_S when it came and went), if it was said."""
		with self.lock:
			alert = self.alerts.get(key)
			if alert is None:
				return
			if alert.resolving is None:
				alert.resolving = (self.clock(), text)
			self.Tick()

	def Note(self, text, severity=INFO):
		"""A message of its own: a recording started or ended, a nightly summary."""
		severity = severity if severity in LEVELS else INFO
		with self.lock:
			self.Deliver(severity, "{} {}{}".format(ICONS[severity], self.Where(None), text), None)

	def Open(self):
		"""The keys of the problems open now."""
		with self.lock:
			return [k for k, a in self.alerts.items() if a.resolving is None]

	def Tick(self):
		"""Reminders, resolutions due, the digest. Call it every few seconds."""
		with self.lock:
			now = self.clock()
			for key, alert in list(self.alerts.items()):
				if alert.resolving is not None:
					at, text = alert.resolving
					hold = FLAP_S if alert.flapping else self.hold
					if now - at >= hold:
						del self.alerts[key]
						if alert.said is not None:
							self.Say(alert, "resolved", text, now=at)
						self.SaveState()
			# Reminders: of the problems still there, said REMIND_S ago or longer; several in one message
			due = [a for a in self.alerts.values() if a.resolving is None and a.said is not None and a.remind
				and now - a.reminded >= REMIND_S]
			if len(due) == 1:
				due[0].reminded = now
				self.Say(due[0], "still", due[0].text)
			elif due:
				for a in due:
					a.reminded = now
				worst = max((a.severity for a in due), key=LEVELS.get)
				lines = ["{} {}{} problems still there:".format(ICONS[worst], self.Where(None), len(due))] + [
					"- {} (for {})".format(a.text, Duration(now - a.since)) for a in sorted(due, key=lambda a: a.since)]
				self.Deliver(worst, "\n".join(lines), None, kind="still")
			if self.digest and (self.digestAt is None or now - self.digestAt >= DIGEST_S):
				self.digestAt = now
				worst = max((s for s, _ in self.digest), key=LEVELS.get)
				lines = ["{} more alert{} this hour, past the limit of {} an hour:".format(len(self.digest),
					"" if len(self.digest) == 1 else "s", CAP_PER_HOUR)] + ["- " + t for _, t in self.digest[:30]]
				if len(self.digest) > 30:
					lines.append("- and {} more".format(len(self.digest) - 30))
				self.digest = []
				self.Deliver(worst, "{} {}".format(ICONS[worst], "\n".join(lines)), None, capped=False)

	# ------------------------------------------------------------------ heartbeat

	def Heartbeat(self):
		"""Pings the healthchecks.io check, at most every HEARTBEAT_S. Call it as often as you like."""
		if self.heartbeat is None:
			return
		now = self.clock()
		if self.beatAt is not None and now - self.beatAt < HEARTBEAT_S:
			return
		self.beatAt = now
		self.Queue(lambda: self.Try(self.heartbeat, "ping", self.heartbeat.Ping))

	def Pause(self):
		"""Pauses the healthchecks.io check (the recording ended): no alert until the next ping."""
		if self.heartbeat is not None:
			self.beatAt = None
			self.Queue(lambda: self.Try(self.heartbeat, "pause", self.heartbeat.Pause))

	# ------------------------------------------------------------------ sending

	def Say(self, alert, kind, text, now=None):
		# One of an alert's messages: problem, worse, flapping, still, resolved
		now = self.clock() if now is None else now
		where = self.Where(alert.key)
		if kind == "problem":
			body = "{} {}{}".format(ICONS[alert.severity], where, text)
		elif kind == "worse":
			body = "{} {}worse: {}".format(ICONS[alert.severity], where, text)
		elif kind == "flapping":
			body = "{} {}keeps coming and going ({} times in {}): {}. I'll say when it has stayed away for {}.".format(
				ICONS[alert.severity], where, FLAP_TIMES, Duration(FLAP_S), text, Duration(FLAP_S))
		elif kind == "still":
			body = "{} {}still, for {}: {}".format(ICONS[alert.severity], where, Duration(now - alert.since), text)
		else:
			body = "{} {}resolved, after {}{}".format(ICONS["resolved"], where, Duration(now - alert.since),
				": " + text if text else ": " + alert.text)
		first = kind in ("problem", "flapping")
		if kind == "resolved":
			# The first message, edited to say so, where the channel can (a bot token)
			original = alert.first or "{} {}{}".format(ICONS[alert.severity], where, alert.text)
			for index, handle in list(alert.handles.items()):
				channel = self.channels[index]
				edited = "~{}~ {} resolved after {}".format(original, ICONS["resolved"], Duration(now - alert.since))
				self.Queue(lambda c=channel, h=handle, t=edited: self.Try(c, "update", lambda: c.Update(h, t)))
		if first:
			alert.first = body
		if self.Deliver(alert.severity, body, alert if first else None, key=alert.key, kind=kind) and first:
			alert.said = now

	def Where(self, key):
		return "*{}*: ".format(self.source) if self.source else ""

	def Deliver(self, severity, body, alert, key=None, kind="note", capped=True):
		# To every channel that takes this severity; True if any will be sent to. Over the hour's cap: into the digest
		targets = [i for i, c in enumerate(self.channels) if LEVELS[severity] >= LEVELS[c.min]]
		self.Log(severity, key, kind, "queued" if targets else "below every channel's min_severity", body)
		if not targets:
			return False
		now = self.clock()
		while self.sent and self.sent[0] < now - 3600:
			self.sent.popleft()
		if capped and len(self.sent) >= CAP_PER_HOUR:
			self.digest.append((severity, re.sub(r"^:[a-z_]+: ", "", body)))
			if self.digestAt is None:
				self.digestAt = now # the first digest a DIGEST_S from now
			self.Log(severity, key, kind, "held for the digest", body)
			if alert is not None:
				alert.said = now # it is said, in the digest: its resolution is said too
			return False
		self.sent.append(now)
		for i in targets:
			channel = self.channels[i]

			def Send(c=channel, i=i):
				handle = self.Try(c, kind, lambda: c.Send(body))
				if alert is not None and handle:
					with self.lock:
						alert.handles[i] = handle
						self.SaveState()
			self.Queue(Send)
		return True

	def Queue(self, job):
		if self.jobs is not None:
			self.jobs.put(job)
		else:
			job()

	def Run(self):
		while True:
			job = self.jobs.get()
			if job is None:
				continue
			try:
				job()
			except Exception as e: # never let one message stop the rest
				self.say("Alerts: {}".format(e))

	def Flush(self, timeout=30.0):
		"""Waits (up to timeout s) until every message queued so far has gone, or failed. True if they have."""
		if self.jobs is None:
			return True
		done = threading.Event()
		self.jobs.put(done.set)
		return done.wait(timeout)

	def Try(self, channel, kind, call):
		# A request, tried TRIES times; what it returned, or None when it failed (said and logged)
		error = None
		for attempt in range(TRIES):
			try:
				result = call()
				self.Log(None, None, kind, "sent to {}".format(channel.name), None)
				return result if result is not None else True
			except SendError as e:
				error = e
				if e.final or attempt == TRIES - 1:
					break
				time.sleep(min(30.0, e.wait if e.wait is not None else RETRY_S[attempt]))
			except Exception as e:
				error = e
				break
		self.say("Alerts: could not {} {}: {}".format("send to" if kind not in ("ping", "pause", "update") else kind
			+ " on", channel.name, error))
		self.Log(None, None, kind, "FAILED on {}: {}".format(channel.name, error), None)
		return None

	def Log(self, severity, key, kind, status, body):
		# One JSON line: time, key, severity, event (problem, worse, flapping, still, resolved, note, ping, ...),
		# state (open, resolved; none for a note), status (queued, sent to ..., FAILED on ...), text
		if not self.log:
			return
		line = {"time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "key": key,
			"severity": severity, "event": kind, "state": STATES.get(kind), "status": status,
			"text": re.sub(r"^:[a-z_]+: (\*[^*]*\*: )?", "", body) if body else None}
		try:
			with open(self.log, "a", encoding="utf-8") as f:
				f.write(json.dumps(line) + "\n")
		except OSError:
			pass

	# ------------------------------------------------------------------ state, between runs

	def LoadState(self):
		if not self.state or not os.path.isfile(self.state):
			return
		try:
			with open(self.state, encoding="utf-8") as f:
				saved = json.load(f)
			for key, a in saved.get("alerts", {}).items():
				alert = Alert(key, a["text"], a["severity"], a["since"])
				alert.said, alert.reminded = a.get("said"), a.get("reminded", a["since"])
				alert.remind = a.get("remind", True)
				alert.handles = {int(i): h for i, h in a.get("handles", {}).items()}
				self.alerts[key] = alert
			self.starts.update({k: collections.deque(v) for k, v in saved.get("starts", {}).items()})
		except Exception as e:
			self.say("Alerts: could not read {}: {}".format(self.state, e))

	def SaveState(self):
		if not self.state:
			return
		saved = {"alerts": {k: {"text": a.text, "severity": a.severity, "since": a.since, "said": a.said,
			"reminded": a.reminded, "remind": a.remind, "handles": a.handles} for k, a in self.alerts.items()},
			"starts": {k: list(v) for k, v in self.starts.items() if v}}
		try:
			os.makedirs(os.path.dirname(os.path.abspath(self.state)), exist_ok=True)
			with open(self.state + ".tmp", "w", encoding="utf-8") as f:
				json.dump(saved, f)
			os.replace(self.state + ".tmp", self.state)
		except OSError as e:
			self.say("Alerts: could not save {}: {}".format(self.state, e))


# ---------------------------------------------------------------------- campy-notify

def Test(settings, path, say=print):
	"""Sends a test message to each channel, and pings then pauses the heartbeat. True if all worked."""
	channels, heartbeat = Channels(settings, say)
	rig = settings.get("rig") or ""
	if not channels and heartbeat is None:
		say("{} names no channel (slack:) and no heartbeat (healthchecks:).".format(path))
		return False
	ok = True
	for channel in channels:
		try:
			channel.Send("{} {}campy test message: alerts from this computer reach you here.".format(
				ICONS[INFO], "*{}*: ".format(rig) if rig else ""))
			say("{}: sent.".format(channel.name))
		except Exception as e:
			say("{}: FAILED: {}".format(channel.name, e))
			ok = False
	if heartbeat is not None:
		try:
			heartbeat.Ping()
			paused = heartbeat.Pause()
			say("healthchecks.io: pinged{}.".format(", then paused" if paused else
				" (not paused: give api_key:, and check: for a slug URL; the check alerts once its pings stop)"))
		except Exception as e:
			say("healthchecks.io: FAILED: {}".format(e))
			ok = False
	return ok


def Main(argv=None):
	p = argparse.ArgumentParser(prog="campy-notify", description="Send campy's alerts (Slack direct messages, "
		"a healthchecks.io heartbeat) from the alerts file, {}.".format(AlertsFile()))
	p.add_argument("--file", help="The alerts file (default: CAMPY_ALERTS_FILE, or ~/.campy/alerts.yaml)")
	p.add_argument("--test", action="store_true", help="Send a test message to each channel, and ping then pause "
		"the heartbeat")
	p.add_argument("--note", metavar="TEXT", help="Send TEXT as a message of its own")
	p.add_argument("--problem", nargs=2, metavar=("KEY", "TEXT"), help="Say a problem, once while it lasts "
		"(kept in --state between runs)")
	p.add_argument("--resolved", nargs="+", metavar=("KEY", "TEXT"), help="Say that problem KEY is over, if it "
		"was said")
	p.add_argument("--severity", default=WARNING, choices=list(LEVELS))
	p.add_argument("--source", help="Which computer it is from (default: the file's rig:)")
	p.add_argument("--state", default=os.path.join(os.path.expanduser("~"), ".campy", "alerts-state.json"),
		help="Where the open problems are kept between runs (default: ~/.campy/alerts-state.json)")
	args = p.parse_args(argv)
	path = args.file or AlertsFile()
	try:
		settings = LoadSettings(path, say=print)
	except Exception as e:
		print(e)
		return 1
	if settings is None:
		print("No alerts file at {}. See docs/alerts.md for what to put in it.".format(path))
		return 1
	if args.test:
		return 0 if Test(settings, path) else 1
	if not (args.note or args.problem or args.resolved):
		p.error("say what to send: --test, --note, --problem or --resolved")
	n = Notifier(args.source, settings=settings, background=False, say=print, state=args.state)
	if args.note:
		n.Note(args.note, args.severity)
	if args.problem:
		n.Problem(args.problem[0], args.problem[1], args.severity)
	if args.resolved:
		n.Resolved(args.resolved[0], " ".join(args.resolved[1:]) or None)
	n.Tick()
	return 0


if __name__ == "__main__":
	sys.exit(Main())
