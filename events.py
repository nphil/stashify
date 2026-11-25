"""Server-Sent Events for the dashboard (GET /api/events).

One hub per process. While at least one browser is connected it runs a small thread per
topic (jobs, GPU meters, runners, cloud status) that takes a snapshot every few seconds and
publishes it only when it changed, plus one thread that batches the new log lines of running
jobs. A connecting client is first sent the latest snapshot of every topic, so it is never
waiting for a change. When the last client leaves the threads stop and the snapshots are
dropped, so an idle server does no polling for the dashboard.

Frames:   id: <n>\\nevent: <name>\\ndata: <one line of JSON>\\n\\n
Events:   jobs, gpus, runners, cloud (the same JSON as the matching GET), log
          ({job_id, cursor, lines}, at most 4 frames a second per running job).
A client that was disconnected gets full snapshots again on reconnect, so Last-Event-ID
is not needed and is ignored. Comment lines (`: ping`) are sent when nothing else is, so a
proxy or the browser can tell a quiet stream from a dead one.
"""

import json
import logging
import queue
import threading

HEARTBEAT_S = 15            # the client drops a stream that is silent for 45 s
QUEUE_MAX = 256             # a client this far behind is dropped; it reconnects and resyncs
PING = b": ping\n\n"


def sse_frame(event, data, eid=None):
    """One SSE frame as bytes. `data` is JSON-encoded on a single line."""
    head = "id: %d\n" % eid if eid is not None else ""
    return ("%sevent: %s\ndata: %s\n\n" % (head, event, json.dumps(data, separators=(",", ":")))).encode()


class Subscriber:
    def __init__(self):
        self.q = queue.Queue(maxsize=QUEUE_MAX)
        self.dead = threading.Event()

    def offer(self, frame):
        try:
            self.q.put_nowait(frame)
        except queue.Full:
            self.dead.set()


class EventHub:
    """topics: [(name, fn, interval_s)]; fn() returns the JSON-able snapshot (an exception skips
    that round). logs: (batches, snapshot) or None; batches(cursors) returns the new
    {job_id, cursor, lines} payloads and advances `cursors`; snapshot() returns every
    running job's buffered lines, for a client that has just connected."""

    LOG_TICK = 0.25

    def __init__(self, topics, logs=None):
        self.topics = list(topics)
        self.logs = logs
        self._lock = threading.Lock()
        self._subs = set()
        self._stop = None
        self._last = {}                     # topic -> (json text, frame)
        self._eid = 0

    def subscribers(self):
        with self._lock:
            return len(self._subs)

    def subscribe(self):
        sub = Subscriber()
        with self._lock:
            for name, _fn, _every in self.topics:
                if name in self._last:
                    sub.offer(self._last[name][1])
            if self.logs:
                try:
                    for payload in self.logs[1]():
                        self._eid += 1
                        sub.offer(sse_frame("log", payload, self._eid))
                except Exception:  # noqa: BLE001 - a log hiccup must not refuse the stream
                    logging.debug("event stream: log snapshot failed", exc_info=True)
            self._subs.add(sub)
            if self._stop is None or self._stop.is_set():
                self._start()
        return sub

    def unsubscribe(self, sub):
        with self._lock:
            self._subs.discard(sub)
            if not self._subs and self._stop is not None:
                self._stop.set()
                self._last.clear()

    # -- internals ------------------------------------------------------------------ #

    def _start(self):
        stop = self._stop = threading.Event()
        for name, fn, every in self.topics:
            threading.Thread(target=self._topic_loop, args=(stop, name, fn, every), daemon=True,
                             name="events-" + name).start()
        if self.logs:
            threading.Thread(target=self._log_loop, args=(stop,), daemon=True, name="events-log").start()

    def _broadcast(self, stop, frame_for):
        """Hand a frame to every client, unless this round of threads has been stopped."""
        with self._lock:
            if stop.is_set():
                return False
            self._eid += 1
            frame = frame_for(self._eid)
            for sub in list(self._subs):
                sub.offer(frame)
            return True

    def _topic_loop(self, stop, name, fn, every):
        last_text = None
        while not stop.is_set():
            try:
                data = fn()
                text = json.dumps(data, separators=(",", ":"))
                if text != last_text:
                    def make(eid, name=name, data=data, text=text):
                        frame = sse_frame(name, data, eid)
                        self._last[name] = (text, frame)
                        return frame
                    if not self._broadcast(stop, make):
                        return
                    last_text = text
            except Exception:  # noqa: BLE001 - one bad snapshot must not end the stream
                logging.debug("event stream: %s snapshot failed", name, exc_info=True)
            stop.wait(every)

    def _log_loop(self, stop):
        cursors = {}
        while not stop.is_set():
            try:
                for payload in self.logs[0](cursors):
                    if not self._broadcast(stop, lambda eid, p=payload: sse_frame("log", p, eid)):
                        return
            except Exception:  # noqa: BLE001
                logging.debug("event stream: log batch failed", exc_info=True)
            stop.wait(self.LOG_TICK)


def serve(handler, hub):
    """Stream `hub` to one HTTP client until it goes away. `handler` is a
    BaseHTTPRequestHandler that has not sent a response yet."""
    sub = hub.subscribe()
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Accel-Buffering", "no")        # nginx-style proxies: do not buffer
        handler.send_header("Connection", "close")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()
        handler.wfile.write(b"retry: 3000\n\n")
        while not sub.dead.is_set():
            try:
                frame = sub.q.get(timeout=HEARTBEAT_S)
            except queue.Empty:
                frame = PING
            handler.wfile.write(frame)
            handler.wfile.flush()
    except OSError:
        pass                                                   # the client went away
    finally:
        hub.unsubscribe(sub)
