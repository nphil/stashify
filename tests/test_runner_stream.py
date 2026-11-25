"""The runner pushes its state over GET /events and the coordinator keeps one reader per runner:
SSE framing, snapshot then changes, a slow reader that cannot hold up the runner, the live
state replacing polling, the fall-back to polling (an older runner, a dropped link) and the
reconnect. Everything runs on loopback with the standard library: no real runner, no requests."""

import atexit
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "winrunner"))

_scratch = tempfile.mkdtemp(prefix="stashify-stream-")
atexit.register(shutil.rmtree, _scratch, ignore_errors=True)
os.environ.setdefault("STASHIFY_RUNNER_STREAM", "0")
os.environ["LOCALAPPDATA"] = _scratch

import core                          # noqa: E402
import runner                        # noqa: E402
import runner_stream as rs           # noqa: E402
import test_dashboard as td          # noqa: E402,F401  (a stand-in for `requests` when it is missing)


def wait_for(cond, secs=5.0):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _line(it):
    try:
        return next(it) + b"\n"
    except StopIteration:
        return b""


def parse(chunks):
    it = iter(b"".join(chunks).split(b"\n"))
    return list(rs.parse_sse(lambda: _line(it)))


class Providers:
    """Mutable stand-ins for what a runner knows."""

    def __init__(self):
        self.health = {"ok": True, "busy": False, "events": True}
        self.gpu = {"util": 1}
        self.jobs = {}
        self.lines = {}

    def jobs_list(self):
        return [dict(j, log_cursor=len(self.lines.get(j["id"], []))) for j in self.jobs.values()]

    def log_since(self, jid, after):
        ls = self.lines.get(jid, [])
        return len(ls), [{"seq": i + 1, "text": t} for i, t in enumerate(ls) if i + 1 > after]


def run_stream(p, write, **kw):
    stop = threading.Event()
    kw.setdefault("tick", 0.01)
    t = threading.Thread(target=runner.stream_events, daemon=True,
                         args=(write, lambda: dict(p.health), lambda: dict(p.gpu), p.jobs_list, p.log_since),
                         kwargs=dict(stop=stop, **kw))
    t.start()
    return stop, t


class FramingTest(unittest.TestCase):
    def test_frame_round_trips_through_the_reader(self):
        data = {"id": "a", "message": "two\nlines"}
        got = parse([runner.sse_frame("job", data, 3), b": ping\n\n", runner.sse_frame("gpu", {"util": 5}, 4)])
        self.assertEqual(got, [("job", data), ("ping", None), ("gpu", {"util": 5})])

    def test_frame_shape_and_unparsable_data(self):
        self.assertEqual(runner.sse_frame("gpu", {"util": 5}, 7), b'id: 7\nevent: gpu\ndata: {"util":5}\n\n')
        self.assertEqual(parse([b"event: job\ndata: {nope\n\n", b"event: gpu\ndata: {}\n\n"]), [("gpu", {})])

    def test_runner_and_nas_runner_share_one_stream_block(self):
        def block(path):
            with open(path, encoding="utf-8") as fh:
                s = fh.read()
            return s.split("# >>> event stream", 1)[1].split("# <<< event stream <<<", 1)[0].split("\n", 1)[1]
        self.assertEqual(block(os.path.join(ROOT, "winrunner", "runner.py")),
                         block(os.path.join(ROOT, "lada_runner.py")))


class StreamTest(unittest.TestCase):
    def test_snapshot_first_then_only_changes(self):
        p = Providers()
        p.jobs["j1"] = {"id": "j1", "state": "running", "progress": 0.1}
        p.lines["j1"] = ["one", "two"]
        out = []
        stop, t = run_stream(p, out.append, gpu_every=0.02)
        self.addCleanup(stop.set)
        self.assertTrue(wait_for(lambda: len([f for f in parse(out) if f[0] == "job"]) >= 1))
        first = [e for e, _ in parse(out)]
        self.assertEqual(first[:4], ["health", "gpu", "log", "job"])          # the whole snapshot, log before state
        log = [d for e, d in parse(out) if e == "log"][0]
        self.assertEqual((log["id"], log["cursor"], [x["text"] for x in log["lines"]]), ("j1", 2, ["one", "two"]))
        time.sleep(0.15)
        n = len(parse(out))
        self.assertEqual(n, 4)                                                # nothing changed: nothing sent
        p.jobs["j1"]["progress"] = 0.5
        p.lines["j1"].append("three")
        p.gpu["util"] = 90
        self.assertTrue(wait_for(lambda: len(parse(out)) >= 7))
        time.sleep(0.1)
        later = parse(out)[4:]
        self.assertEqual(sorted(e for e, _ in later), ["gpu", "job", "log"])
        self.assertEqual([d["lines"][0]["text"] for e, d in later if e == "log"], ["three"])    # only the new line
        stop.set()

    def test_gpu_is_rate_limited_and_a_ping_goes_out_when_quiet(self):
        p = Providers()
        out = []
        stop, t = run_stream(p, out.append, gpu_every=10, ping=0.05)
        self.addCleanup(stop.set)
        time.sleep(0.1)
        p.gpu["util"] = 50
        self.assertTrue(wait_for(lambda: b": ping" in b"".join(out)))
        time.sleep(0.1)
        self.assertEqual([e for e, d in parse(out) if e == "gpu"], ["gpu"])   # the second reading waits for gpu_every
        stop.set()

    def test_the_stream_ends_when_the_client_goes_away(self):
        p = Providers()

        def write(b):
            raise BrokenPipeError()
        ended = []

        def go():
            try:
                runner.stream_events(write, dict, dict, list, p.log_since)
            except OSError:
                ended.append(True)                         # what Handler._events catches
        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(2)
        self.assertEqual((t.is_alive(), ended), (False, [True]))

    def test_a_slow_reader_does_not_hold_up_the_runner_or_other_readers(self):
        release = threading.Event()
        slow_got = []

        def slow_write(b):
            slow_got.append(b)
            release.wait(5)                                   # takes one frame and then never reads again
        jid = "abc123"
        with runner._jobs_lock:
            runner._jobs[jid] = {"id": jid, "state": "running", "progress": 0.0}
        self.addCleanup(runner._jobs.pop, jid, None)
        p = runner
        stop = threading.Event()
        self.addCleanup(stop.set)
        self.addCleanup(release.set)
        threading.Thread(target=runner.stream_events, daemon=True, args=(
            slow_write, lambda: {"ok": True}, lambda: {}, runner._jobs_public, runner.log_since_cursor),
            kwargs=dict(stop=stop, tick=0.01)).start()
        fast = []
        threading.Thread(target=runner.stream_events, daemon=True, args=(
            fast.append, lambda: {"ok": True}, lambda: {}, runner._jobs_public, runner.log_since_cursor),
            kwargs=dict(stop=stop, tick=0.01)).start()
        self.assertTrue(wait_for(lambda: slow_got))
        t0 = time.monotonic()
        for i in range(500):                                  # the job's own work goes on at full speed
            runner.push_log(jid, "line %d" % i)
            runner.set_job(jid, progress=i / 500)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertTrue(wait_for(lambda: any(e == "job" and d["progress"] > 0.9 for e, d in parse(fast))))
        self.assertEqual(len(slow_got), 1)                    # and the slow one is simply still waiting
        runner._logs.pop(jid, None)
        runner._logseq.pop(jid, None)


class FakeRunner:
    """A loopback runner whose /events does what `script(handler, n)` says (n = connection number)."""

    def __init__(self, script, token="tok"):
        self.connections = 0
        self.polls = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.headers.get("X-Runner-Token") != token:
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/events":
                    outer.connections += 1
                    script(self, outer.connections)
                else:
                    outer.polls += 1
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def open_stream(h):
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Connection", "close")
    h.end_headers()


def send(h, event, data, eid=1):
    h.wfile.write(runner.sse_frame(event, data, eid))
    h.wfile.flush()


class ReaderTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"STASHIFY_RUNNER_STREAM": "1"})
        env.start()
        self.addCleanup(env.stop)
        for name, val in (("BACKOFF_MIN", 0.05), ("BACKOFF_MAX", 0.2)):
            p = mock.patch.object(rs, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.links = []

    def link(self, fake, token="tok"):
        self.addCleanup(fake.close)
        lk = rs.ensure(fake.url, token)
        self.links.append(lk)
        self.addCleanup(lk.close)
        return lk

    def test_live_state_replaces_polling(self):
        def script(h, n):
            open_stream(h)
            send(h, "health", {"ok": True, "busy": True, "ops": ["decensor"]}, 1)
            send(h, "gpu", {"util": 42, "mem_used": 1}, 2)
            send(h, "log", {"id": "r1", "cursor": 2, "lines": [{"seq": 1, "text": "a"}, {"seq": 2, "text": "b"}]}, 3)
            send(h, "job", {"id": "r1", "state": "running", "progress": 0.3}, 4)
            time.sleep(30)
        fake = FakeRunner(script)
        lk = self.link(fake)
        self.assertTrue(wait_for(lambda: lk.live_gpu() and lk.job_and_lines("r1")))
        self.assertEqual(lk.state(), "live")
        job, lines, cursor = lk.job_and_lines("r1", after=1)
        self.assertEqual((job["progress"], [x["text"] for x in lines], cursor), (0.3, ["b"], 2))
        with mock.patch.object(core.http(), "get", side_effect=AssertionError("polled")):
            self.assertEqual(core.runner_health(fake.url, "tok")["busy"], True)     # from memory
            import server
            self.assertEqual(server._read_runner_gpu(fake.url, "tok")["util"], 42.0)
        self.assertEqual(fake.polls, 0)
        self.assertEqual(fake.connections, 1)

    def test_an_older_runner_without_events_is_polled_as_before(self):
        def script(h, n):
            h.send_response(404)
            h.send_header("Content-Length", "0")
            h.end_headers()
        fake = FakeRunner(script)
        lk = self.link(fake)
        self.assertTrue(wait_for(lambda: lk.state() == "unsupported"))
        reply = mock.Mock(status_code=200, json=lambda: {"ok": True, "busy": False})
        with mock.patch.object(core.http(), "get", return_value=reply) as get:
            self.assertEqual(core.runner_health(fake.url, "tok"), {"ok": True, "busy": False})
        self.assertEqual(get.call_count, 1)                                          # the old /health request
        self.assertIsNone(lk.live_health())

    def test_a_dropped_link_falls_back_to_polling_then_reconnects(self):
        gate = threading.Event()

        def script(h, n):
            open_stream(h)
            send(h, "health", {"ok": True, "n": n}, 1)
            if n == 1:
                return                                    # closes: the link drops
            gate.wait(30)
        fake = FakeRunner(script)
        lk = self.link(fake)
        self.assertTrue(wait_for(lambda: fake.connections >= 2 and lk.live_health() == {"ok": True, "n": 2}))
        self.assertEqual(lk.state(), "live")
        self.assertGreaterEqual(fake.connections, 2)
        gate.set()

    def test_while_down_callers_see_reconnecting_not_offline(self):
        drop = threading.Event()

        def script(h, n):
            open_stream(h)
            send(h, "health", {"ok": True}, 1)
            if n == 1:
                drop.wait(10)
                return
            time.sleep(30)
        fake = FakeRunner(script)
        with mock.patch.object(rs, "BACKOFF_MIN", 5), mock.patch.object(rs, "BACKOFF_MAX", 5):
            lk = self.link(fake)
            self.assertTrue(wait_for(lambda: lk.live_health() is not None))
            drop.set()
            self.assertTrue(wait_for(lambda: lk.state() == "reconnecting"))
            self.assertIsNone(lk.live_health())                    # callers poll (and keep their sticky copy)
            import server
            self.assertEqual(server._stream_state(fake.url), "reconnecting")

    def test_silence_longer_than_the_read_timeout_means_gone(self):
        def script(h, n):
            open_stream(h)
            send(h, "health", {"ok": True}, 1)
            time.sleep(30)                                  # alive socket, but no pings
        fake = FakeRunner(script)
        with mock.patch.object(rs, "READ_TIMEOUT", 0.3):
            lk = self.link(fake)
            self.assertTrue(wait_for(lambda: lk.live_health() is not None))
            self.assertTrue(wait_for(lambda: lk.state() in ("reconnecting", "connecting") and lk.live_health() is None))

    def test_wrong_token_is_refused_and_not_live(self):
        fake = FakeRunner(lambda h, n: None, token="right")
        lk = self.link(fake, token="wrong")
        self.assertTrue(wait_for(lambda: lk.state() == "refused"))

    def test_dispatch_follows_the_pushed_job_without_polling(self):
        def script(h, n):
            open_stream(h)
            send(h, "health", {"ok": True}, 1)
            send(h, "log", {"id": "r1", "cursor": 1, "lines": [{"seq": 1, "text": "hello"}]}, 2)
            send(h, "job", {"id": "r1", "state": "running", "progress": 0.2}, 3)
            time.sleep(0.4)
            send(h, "log", {"id": "r1", "cursor": 2, "lines": [{"seq": 2, "text": "bye"}]}, 4)
            send(h, "job", {"id": "r1", "state": "done", "progress": 1.0}, 5)
            time.sleep(30)
        fake = FakeRunner(script)
        self.addCleanup(fake.close)
        lk = rs.ensure(fake.url, "tok")
        self.addCleanup(lk.close)
        self.assertTrue(wait_for(lambda: lk.live_health() is not None))
        out_dir = tempfile.mkdtemp(prefix="dispatch-", dir=_scratch)
        runner_cfg = {"name": "pc", "url": fake.url, "token": "tok", "engine": "jasna"}
        seen, lines = [], []
        with mock.patch.object(core, "resolve_runner", return_value=runner_cfg), \
                mock.patch.object(core, "newest_video", return_value=os.path.join(out_dir, "o.mp4")), \
                mock.patch.object(core, "_chown_like"), mock.patch.object(core.shutil, "move"), \
                mock.patch.object(core.shutil, "rmtree"), mock.patch.object(core.os, "makedirs"), \
                mock.patch.object(core.os, "chmod"), \
                mock.patch.object(core.http(), "post", return_value=mock.Mock(json=lambda: {"id": "r1"}, raise_for_status=lambda: None)), \
                mock.patch.object(core.http(), "get", side_effect=AssertionError("polled")):
            core._runner_dispatch({"runnerScratch": out_dir}, "/in.mp4", out_dir, "decensor",
                                  status_cb=seen.append, log_cb=lines.append)
        self.assertEqual([s["state"] for s in seen][-1], "done")
        self.assertEqual(lines, ["hello", "bye"])                   # every line, once, before "done"


if __name__ == "__main__":
    unittest.main()
