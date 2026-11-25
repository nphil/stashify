"""The coordinator's live link to each runner: GET /events, held open.

Instead of asking a runner for /health, /gpu, /jobs/<id> and the log every 1-5 seconds, the
coordinator keeps one Server-Sent Events connection per runner. The runner pushes a snapshot
when we connect and then only what changes, plus a `: ping` comment every 15 s. A reader
thread per runner keeps the newest health / gpu / job / log state here in memory; the rest of
the coordinator reads that instead of the network while the link is `live`.

  live          connected and the runner's snapshot has arrived: use the memory.
  reconnecting  the link dropped (a missed ping, an EOF, an error): callers fall back to the
                old polling code, which keeps its own STICKY_SECS memory of the last answer,
                so one drop never reads as "offline".
  unsupported   the runner answered 404 (an older runner): polling only; asked again every
                5 minutes in case it was upgraded.
  refused       the runner rejected the token.

Nothing else changes: dispatch, cancel/pause and the result download stay plain requests.
Standard library only (no `requests`), so a drop is detected by this socket's own read timeout.
"""

import http.client
import json
import logging
import os
import threading
import time
from collections import OrderedDict, deque
from urllib.parse import urlsplit

log = logging.getLogger("stashify.runner_stream")

READ_TIMEOUT = 40            # the runner pings every 15 s: this long with no bytes means the link is gone
CONNECT_TIMEOUT = 4
BACKOFF_MIN = 1
BACKOFF_MAX = 15
RECHECK_UNSUPPORTED = 300    # seconds between asking an older runner for /events again
IDLE_EXIT = 300              # a link nobody has asked about for this long is closed (runner removed)
MAX_JOBS = 32                # jobs kept per runner (newest)
MAX_LOG = 2000               # log lines kept per job


def enabled():
    return os.environ.get("STASHIFY_RUNNER_STREAM", "1") != "0"


def parse_sse(readline):
    """Yield (event, data) for every frame `readline` delivers, ("ping", None) for a comment
    line (the keep-alive), until it returns b"" (the stream ended). A frame whose data is not
    JSON is skipped."""
    name, data = None, []
    while True:
        raw = readline()
        if not raw:
            return
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line == "":
            if data:
                try:
                    yield (name or "message", json.loads("\n".join(data)))
                except ValueError:
                    pass
            name, data = None, []
        elif line.startswith(":"):
            yield ("ping", None)
        else:
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                name = value
            elif field == "data":
                data.append(value)


class _NoEvents(Exception):
    """The runner has no /events (an older runner)."""


class _Idle(Exception):
    """Nobody has asked about this runner for a long time."""


class RunnerLink:
    def __init__(self, url, token=""):
        self.url = url
        self.token = token
        self.cond = threading.Condition()
        self.wake = threading.Event()
        self.connected = False
        self.supported = None          # None until the first answer
        self.refused = False
        self.health = None
        self.gpu = None
        self.jobs = OrderedDict()
        self.logs = {}                 # job id -> deque of log lines
        self.events = 0                # counts every state change, for wait()
        self.dropped_at = None
        self.wanted_at = time.monotonic()
        self.finished = False
        self._stop = False
        self._conn = None

    # -- reading (any thread) ---------------------------------------------------- #

    def want(self):
        self.wanted_at = time.monotonic()

    def live(self):
        return self.connected and self.health is not None

    def state(self):
        if self.live():
            return "live"
        if self.refused:
            return "refused"
        if self.supported is False:
            return "unsupported"
        return "reconnecting" if self.dropped_at is not None else "connecting"

    def live_health(self):
        with self.cond:
            return dict(self.health) if self.live() else None

    def live_gpu(self):
        with self.cond:
            return dict(self.gpu) if self.live() and self.gpu is not None else None

    def job_and_lines(self, jid, after=0):
        """(job dict, new log lines after `after`, new cursor), taken together so a "done"
        never arrives without the lines before it; None when the link is not live or has not
        been told about this job yet (the caller polls instead)."""
        self.want()
        with self.cond:
            job = self.jobs.get(jid) if self.live() else None
            if job is None:
                return None
            lines = [dict(x) for x in self.logs.get(jid, ()) if x["seq"] > after]
            return dict(job), lines, (lines[-1]["seq"] if lines else after)

    def wait(self, timeout, since=None):
        """Sleep until something arrives or the link drops, at most `timeout` seconds. `since` is
        a `.events` reading taken earlier: anything that arrived after it ends the wait at once."""
        with self.cond:
            n = self.events if since is None else since
            self.cond.wait_for(lambda: self.events != n or not self.connected, timeout)

    # -- the reader thread --------------------------------------------------------- #

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="runner-link-" + self.url).start()

    def close(self):
        self._stop = True
        self.wake.set()
        conn = self._conn
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def _loop(self):
        delay = BACKOFF_MIN
        try:
            while not self._stop:
                if time.monotonic() - self.wanted_at > IDLE_EXIT:
                    break
                pause = None
                got = False
                try:
                    got = self._session()
                except _NoEvents:
                    with self.cond:
                        self.supported = False
                    pause = RECHECK_UNSUPPORTED
                except _Idle:
                    break
                except PermissionError:
                    self.refused = True
                    pause = BACKOFF_MAX
                except (OSError, http.client.HTTPException, ValueError) as exc:
                    log.debug("runner stream %s: %s", self.url, exc)
                finally:
                    self._down()
                if got:
                    delay = BACKOFF_MIN
                if pause is None:
                    pause = delay
                    delay = min(delay * 2, BACKOFF_MAX)
                self.wake.wait(pause)
                self.wake.clear()
        finally:
            self.finished = True
            _forget(self)

    def _down(self):
        with self.cond:
            if self.connected or self.health is not None:
                self.dropped_at = time.monotonic()
            self.connected = False
            self.events += 1
            self.cond.notify_all()

    def _session(self):
        u = urlsplit(self.url)
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = self._conn = cls(u.hostname, u.port, timeout=CONNECT_TIMEOUT)
        try:
            conn.connect()
            conn.sock.settimeout(READ_TIMEOUT)
            conn.request("GET", "/events", headers={
                "X-Runner-Token": self.token, "X-Lada-Token": self.token,
                "Accept": "text/event-stream"})
            resp = conn.getresponse()
            if resp.status == 404:
                raise _NoEvents
            if resp.status in (401, 403):
                raise PermissionError("runner refused the token")
            if resp.status != 200:
                raise OSError("runner answered %s" % resp.status)
            if "text/event-stream" not in (resp.getheader("Content-Type") or ""):
                raise _NoEvents
            with self.cond:
                self.connected = True
                self.supported = True
                self.refused = False
                self.health = self.gpu = None          # the snapshot that follows replaces all of it
                self.jobs.clear()
                self.logs.clear()
                self.events += 1
                self.cond.notify_all()
            got = False
            for event, data in parse_sse(resp.readline):
                if time.monotonic() - self.wanted_at > IDLE_EXIT:
                    raise _Idle
                if event != "ping":
                    got = True
                    self._apply(event, data)
            return got
        finally:
            self._conn = None
            conn.close()

    def _apply(self, event, data):
        with self.cond:
            if event == "health" and isinstance(data, dict):
                self.health = data
            elif event == "gpu" and isinstance(data, dict):
                self.gpu = data
            elif event == "job" and isinstance(data, dict) and data.get("id"):
                self.jobs[data["id"]] = data
                self.jobs.move_to_end(data["id"])
                while len(self.jobs) > MAX_JOBS:
                    old, _ = self.jobs.popitem(last=False)
                    self.logs.pop(old, None)
            elif event == "log" and isinstance(data, dict) and data.get("id"):
                dq = self.logs.get(data["id"])
                if dq is None:
                    dq = self.logs[data["id"]] = deque(maxlen=MAX_LOG)
                top = dq[-1]["seq"] if dq else 0
                for ln in data.get("lines") or ():
                    if isinstance(ln, dict) and ln.get("seq", 0) > top:
                        dq.append(ln)
                        top = ln["seq"]
            self.events += 1
            self.cond.notify_all()


_links = {}
_links_lock = threading.Lock()


def ensure(url, token=""):
    """The link to this runner (started on first use), or None when streaming is switched off."""
    url = str(url or "").rstrip("/")
    if not url or not enabled():
        return None
    with _links_lock:
        lk = _links.get(url)
        if lk is None or lk.finished:
            lk = _links[url] = RunnerLink(url, token)
            lk.start()
        elif lk.token != token:
            lk.token = token
            lk.wake.set()
        lk.want()
        return lk


def peek(url):
    """The existing link, or None: never starts one."""
    return _links.get(str(url or "").rstrip("/"))


def _forget(lk):
    with _links_lock:
        if _links.get(lk.url) is lk:
            del _links[lk.url]
