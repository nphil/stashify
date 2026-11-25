"""What the dashboard (web/) needs from the coordinator: the event stream, the built-app static
serving, the extra job / runner fields, runner wake + edit, library sorting, and the quick
retry that stops a dropped connect from reading as "runner offline". Everything is local:
a loopback HTTP server, fake Stash and fake Vast, no real runner, no network."""

import gzip
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cloudfakes as cf              # noqa: E402
import cloud_jasna as cj             # noqa: E402
import core                          # noqa: E402
import events                        # noqa: E402
import server                        # noqa: E402
from test_cloud_api import SCENE, stash_scene   # noqa: E402

try:
    import requests
except ImportError:                  # a bare Python (no pip install): enough of it to fake the network
    import types
    requests = types.ModuleType("requests")
    requests.RequestException = type("RequestException", (OSError,), {})
    requests.ConnectionError = type("ConnectionError", (requests.RequestException,), {})
    requests.Timeout = type("Timeout", (requests.RequestException,), {})
    requests.get = requests.post = lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("no network in tests"))
    sys.modules["requests"] = requests


def wait_for(cond, secs=5.0):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class FrameTest(unittest.TestCase):
    def test_a_frame_is_id_event_and_one_line_of_json(self):
        frame = events.sse_frame("jobs", [{"id": "a", "message": "two\nlines"}], 7)
        self.assertEqual(frame, b'id: 7\nevent: jobs\ndata: [{"id":"a","message":"two\\nlines"}]\n\n')

    def test_a_frame_without_an_id(self):
        self.assertEqual(events.sse_frame("ping", {}), b"event: ping\ndata: {}\n\n")


class HubTest(unittest.TestCase):
    def hub(self, value, **kw):
        return events.EventHub([("jobs", lambda: value["v"], 0.02)], **kw)

    def drain(self, sub, n, secs=3.0):
        out = []
        end = time.time() + secs
        while len(out) < n and time.time() < end:
            try:
                out.append(sub.q.get(timeout=0.1))
            except Exception:  # noqa: BLE001
                pass
        return out

    def test_a_snapshot_is_published_only_when_it_changes(self):
        value = {"v": [1]}
        hub = self.hub(value)
        sub = hub.subscribe()
        self.addCleanup(hub.unsubscribe, sub)
        (first,) = self.drain(sub, 1)
        self.assertIn(b"event: jobs", first)
        time.sleep(0.2)                                        # ten more rounds, nothing new
        self.assertTrue(sub.q.empty())
        value["v"] = [1, 2]
        (second,) = self.drain(sub, 1)
        self.assertIn(b"data: [1,2]", second)

    def test_a_new_client_is_sent_the_latest_snapshot_at_once(self):
        hub = self.hub({"v": {"a": 1}})
        one = hub.subscribe()
        self.drain(one, 1)
        two = hub.subscribe()
        self.assertIn(b'data: {"a":1}', two.q.get_nowait())
        hub.unsubscribe(one)
        hub.unsubscribe(two)

    def test_the_threads_stop_and_the_snapshots_go_with_the_last_client(self):
        calls = []
        hub = events.EventHub([("jobs", lambda: calls.append(1) or len(calls), 0.01)])
        sub = hub.subscribe()
        wait_for(lambda: len(calls) > 3)
        hub.unsubscribe(sub)
        time.sleep(0.1)
        n = len(calls)
        time.sleep(0.15)
        self.assertEqual(len(calls), n)                        # nobody listening: nothing polled
        self.assertEqual(hub.subscribers(), 0)
        self.assertTrue(hub.subscribe().q.empty())             # and no stale snapshot is replayed

    def test_a_snapshot_that_fails_is_skipped_not_fatal(self):
        state = {"n": 0}

        def flaky():
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("boom")
            return "ok"
        hub = events.EventHub([("jobs", flaky, 0.01)])
        sub = hub.subscribe()
        self.addCleanup(hub.unsubscribe, sub)
        self.assertIn(b'data: "ok"', self.drain(sub, 1)[0])

    def test_log_lines_are_batched_per_job_and_a_new_client_gets_the_buffer(self):
        lines = [{"seq": 1, "t": 0, "level": "proc", "text": "hello"}]
        sent = []

        def batches(cursors):
            if cursors.get("j1") == 1:
                return []
            cursors["j1"] = 1
            sent.append(1)
            return [{"job_id": "j1", "cursor": 1, "lines": lines}]
        snap = lambda: [{"job_id": "j1", "cursor": 1, "lines": lines}]      # noqa: E731
        hub = events.EventHub([], logs=(batches, snap))
        early = hub.subscribe()
        self.addCleanup(hub.unsubscribe, early)
        self.assertIn(b'event: log\ndata: {"job_id":"j1","cursor":1,"lines":[{"seq":1', self.drain(early, 2)[0])
        late = hub.subscribe()
        self.addCleanup(hub.unsubscribe, late)
        self.assertIn(b"hello", late.q.get_nowait())

    def test_a_client_that_falls_far_behind_is_dropped(self):
        sub = events.Subscriber()
        for i in range(events.QUEUE_MAX + 1):
            sub.offer(b"x")
        self.assertTrue(sub.dead.is_set())


class ServerCase(cf.CloudCase):
    """The real Handler on a loopback port; Stash faked."""

    TOKEN = "worker-token-1"

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gql_calls = []
        self.gql_scene = stash_scene("5")
        patches = [mock.patch.object(server, "TOKEN", self.TOKEN),
                   mock.patch.object(server, "stash_gql", self.fake_gql),
                   mock.patch.object(server, "RUNNERS_STORE", os.path.join(self.tmp.name, "runners.json")),
                   mock.patch.object(server, "WEBUI_DIR", os.path.join(self.tmp.name, "dist"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        cj.set_cloud(self.cl)
        self.addCleanup(cj.set_cloud, None)
        self._reset()
        self.addCleanup(self._reset)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @staticmethod
    def _reset():
        with server._jobs_lock:
            server._jobs.clear()
        while not server._work.empty():
            server._work.get_nowait()

    def fake_gql(self, query, variables=None):
        self.gql_calls.append(variables)
        if variables and "f" in variables:
            return {"findScenes": {"count": 0, "scenes": []}}
        return {"s%d" % i: self.gql_scene for i in range(20)}

    def raw(self, method, path, body=None, token=TOKEN, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = {"Content-Type": "application/json"}
        if token:
            h["X-Decensor-Token"] = token
        h.update(headers or {})
        conn.request(method, path, json.dumps(body) if body is not None else None, h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def call(self, method, path, body=None, token=TOKEN):
        resp, data = self.raw(method, path, body, token)
        return resp.status, (json.loads(data) if data else None)


class EventStreamTest(ServerCase):
    def test_the_stream_needs_the_token(self):
        self.assertEqual(self.call("GET", "/api/events", token="")[0], 401)

    def test_the_stream_sends_snapshots_then_pings_and_cleans_up_when_the_client_leaves(self):
        hub = events.EventHub([("jobs", lambda: [{"id": "a"}], 0.02), ("runners", lambda: [], 0.02)])
        with mock.patch.object(server, "event_hub", lambda: hub), mock.patch.object(events, "HEARTBEAT_S", 0.1):
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            sock.sendall(("GET /api/events HTTP/1.1\r\nHost: x\r\nX-Decensor-Token: %s\r\n\r\n" % self.TOKEN).encode())
            buf = b""
            end = time.time() + 5
            while time.time() < end and (b": ping" not in buf):
                buf += sock.recv(4096)
            head, _, body = buf.partition(b"\r\n\r\n")
            self.assertIn(b"200", head.split(b"\r\n")[0])
            lowered = head.lower()
            self.assertIn(b"content-type: text/event-stream", lowered)
            self.assertIn(b"cache-control: no-cache", lowered)
            self.assertIn(b"x-accel-buffering: no", lowered)
            self.assertIn(b'event: jobs\ndata: [{"id":"a"}]\n\n', body)
            self.assertIn(b"event: runners", body)
            self.assertIn(b": ping\n\n", body)                       # a quiet stream is not a dead one
            self.assertEqual(hub.subscribers(), 1)
            sock.close()
            self.assertTrue(wait_for(lambda: hub.subscribers() == 0))   # the next write fails: unsubscribed


class StaticTest(ServerCase):
    def setUp(self):
        super().setUp()
        dist = server.WEBUI_DIR
        os.makedirs(os.path.join(dist, "assets"))
        self.js = "console.log('hello');" * 200
        with open(os.path.join(dist, "index.html"), "w") as fh:
            fh.write('<script>window.__TOKEN__="__WORKER_TOKEN__"</script><div id=app></div>')
        with open(os.path.join(dist, "assets", "app-abc123.js"), "w") as fh:
            fh.write(self.js)
        with open(os.path.join(dist, "icon.png"), "wb") as fh:
            fh.write(b"\x89PNG not really")

    def test_index_carries_the_token_and_is_never_cached(self):
        resp, body = self.raw("GET", "/", token="")
        self.assertEqual(resp.status, 200)
        self.assertIn(self.TOKEN.encode(), body)
        self.assertNotIn(b"__WORKER_TOKEN__", body)
        self.assertEqual(resp.getheader("Cache-Control"), "no-cache")

    def test_a_screen_of_the_app_falls_back_to_index_but_a_missing_file_is_404(self):
        self.assertIn(b"id=app", self.raw("GET", "/runners", token="")[1])
        self.assertEqual(self.raw("GET", "/assets/gone-1.js", token="")[0].status, 404)
        self.assertNotIn(b"root:", self.raw("GET", "/../../etc/passwd", token="")[1])      # normalised, never read from disk
        self.assertEqual(self.raw("GET", "/..%2f..%2fetc%2fpasswd.txt", token="")[0].status, 404)

    def test_hashed_assets_cache_for_a_year_and_are_gzipped_on_request(self):
        resp, body = self.raw("GET", "/assets/app-abc123.js", token="", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(resp.getheader("Cache-Control"), "public, max-age=31536000, immutable")
        self.assertEqual(resp.getheader("Content-Encoding"), "gzip")
        self.assertEqual(gzip.decompress(body).decode(), self.js)
        self.assertLess(len(body), len(self.js))
        plain_resp, plain = self.raw("GET", "/assets/app-abc123.js", token="")
        self.assertIsNone(plain_resp.getheader("Content-Encoding"))
        self.assertEqual(plain.decode(), self.js)

    def test_images_are_not_compressed(self):
        resp, _ = self.raw("GET", "/icon.png", token="", headers={"Accept-Encoding": "gzip"})
        self.assertIsNone(resp.getheader("Content-Encoding"))
        self.assertEqual(resp.getheader("Content-Type"), "image/png")


class RunnerTest(ServerCase):
    def health(self, **kw):
        body = {"ok": True, "node": "pc", "kind": "windows", "ops": ["decensor", "upscale"],
                "engines": {"decensor": "jasna", "upscale": "span"}, "busy": False,
                "lanes": {"ai": {"gpu": "RTX 3080", "busy": False}}}
        body.update(kw)
        return mock.Mock(json=lambda: body)

    def test_a_dropped_connect_is_retried_once_before_the_runner_is_offline(self):
        with mock.patch.object(requests, "get", side_effect=[requests.ConnectionError("dropped"), self.health()]) as get, \
                mock.patch.object(core.time, "sleep"):
            h = core.runner_health("http://pc:8720", "t")
        self.assertEqual(h["node"], "pc")
        self.assertEqual(get.call_count, 2)

    def test_two_failed_connects_in_a_row_raise(self):
        with mock.patch.object(requests, "get", side_effect=requests.Timeout("slow")) as get, \
                mock.patch.object(core.time, "sleep"):
            with self.assertRaises(requests.Timeout):
                core.runner_health("http://pc:8720", "t")
        self.assertEqual(get.call_count, 2)

    def test_a_refusal_that_is_not_a_connect_failure_is_not_retried(self):
        bad = mock.Mock(json=mock.Mock(side_effect=ValueError("not json")))
        with mock.patch.object(requests, "get", return_value=bad) as get:
            with self.assertRaises(ValueError):
                core.runner_health("http://pc:8720", "t")
        self.assertEqual(get.call_count, 1)

    def test_probe_runner_shows_online_after_one_dropped_connect(self):
        with mock.patch.object(requests, "get", side_effect=[requests.ConnectionError("dropped"), self.health()]), \
                mock.patch.object(core.time, "sleep"):
            out = server.probe_runner({"name": "pc", "url": "http://pc:8720"})
        self.assertTrue(out["online"])
        self.assertEqual((out["gpu"], out["asleep"], out["can_wake"], out["queue_eta_s"]), ("RTX 3080", False, False, 0))
        self.assertEqual(out["qualities"], ["fast", "balanced", "high", "max"])

    def test_resolve_runner_does_not_give_up_on_one_dropped_connect(self):
        cfg = {"runners": [{"name": "pc", "url": "http://pc:8720", "token": "t", "mac": "aa:bb:cc:dd:ee:ff"}],
               "runnerUrl": "", "runnerWakeSecs": 1}
        with mock.patch.object(requests, "get", side_effect=[requests.ConnectionError("dropped"), self.health()]), \
                mock.patch.object(core.time, "sleep"), mock.patch.object(core, "wake_runner") as wake:
            runner = core.resolve_runner(cfg, "decensor", wake=True)
        self.assertEqual(runner["name"], "pc")
        wake.assert_not_called()                                # it was awake all along

    def test_a_sleeping_runner_says_so_and_can_be_woken(self):
        entry = {"name": "pc", "url": "http://pc:8720", "token": "t", "mac": "aa:bb:cc:dd:ee:ff"}
        with mock.patch.object(requests, "get", side_effect=requests.ConnectionError("asleep")), \
                mock.patch.object(core.time, "sleep"):
            out = server.probe_runner(entry)
        self.assertEqual((out["online"], out["asleep"], out["can_wake"]), (False, True, True))
        self.assertGreater(out["wake_eta_s"], 0)
        server.save_store([entry])
        with mock.patch.object(core, "wake_runner", return_value=True) as wake:
            status, payload = self.call("POST", "/api/runners/wake", {"url": "http://pc:8720/"})
        self.assertEqual((status, payload["ok"]), (200, True))
        self.assertGreater(payload["eta_s"], 0)
        self.assertEqual(wake.call_args[0][0]["mac"], "aa:bb:cc:dd:ee:ff")

    def test_waking_a_runner_with_no_mac_is_409_and_an_unknown_one_404(self):
        server.save_store([{"name": "pc", "url": "http://pc:8720", "token": "t"}])
        self.assertEqual(self.call("POST", "/api/runners/wake", {"url": "http://pc:8720"})[0], 409)
        self.assertEqual(self.call("POST", "/api/runners/wake", {"url": "http://nope:1"})[0], 404)

    def test_editing_a_runner_keeps_its_token_mac_and_ops(self):
        server.save_store([{"name": "pc", "url": "http://pc:8720", "token": "secret-tok",
                            "mac": "aa:bb:cc:dd:ee:ff", "ops": ["decensor"], "prefer": []}])
        with mock.patch.object(requests, "get", side_effect=requests.ConnectionError("x")), mock.patch.object(core.time, "sleep"):
            status, _ = self.call("POST", "/api/runners", {"name": "Desk PC", "url": "http://pc:8720", "prefer": ["decensor"]})
        self.assertEqual(status, 201)
        (stored,) = server.load_store()
        self.assertEqual((stored["name"], stored["token"], stored["mac"], stored["ops"], stored["prefer"]),
                         ("Desk PC", "secret-tok", "aa:bb:cc:dd:ee:ff", ["decensor"], ["decensor"]))
        with mock.patch.object(requests, "get", side_effect=requests.ConnectionError("x")), mock.patch.object(core.time, "sleep"):
            self.call("POST", "/api/runners", {"url": "http://pc:8720", "token": "new-tok"})
        self.assertEqual(server.load_store()[0]["token"], "new-tok")

    def test_the_connection_test_answers_ok_or_an_error(self):
        with mock.patch.object(requests, "get", return_value=self.health()):
            status, payload = self.call("POST", "/api/runners/test", {"url": "http://pc:8720"})
        self.assertEqual((status, payload["ok"], payload["error"], payload["ops"]),
                         (200, True, None, ["decensor", "upscale"]))
        with mock.patch.object(requests, "get", side_effect=requests.ConnectionError("x")), mock.patch.object(core.time, "sleep"):
            status, payload = self.call("POST", "/api/runners/test", {"url": "http://nope:1"})
        self.assertEqual((status, payload["ok"]), (200, False))
        self.assertTrue(payload["error"])

    def test_a_runner_with_a_job_running_reports_when_it_will_be_free(self):
        job = server.new_job("5", {})
        server.set_job(job["id"], state="running", runner="pc", eta=90)
        self.assertEqual(server._runner_queue_eta("pc"), 90)
        self.assertEqual(server._runner_queue_eta("other"), 0)


class SceneListTest(ServerCase):
    def scenes(self, query):
        status, _ = self.call("GET", "/api/scenes?" + query)
        return status, (self.gql_calls[-1]["f"] if self.gql_calls else None)

    def test_sort_direction_and_count_are_passed_to_stash(self):
        status, f = self.scenes("sort=file_mod_time&dir=asc&per_page=500&page=2&tag=9")
        self.assertEqual(status, 200)
        self.assertEqual((f["sort"], f["direction"], f["per_page"], f["page"]), ("file_mod_time", "ASC", 60, 2))
        self.assertEqual(self.gql_calls[-1]["sf"]["tags"]["value"], ["9"])

    def test_random_is_a_stable_shuffle_by_seed(self):
        _, f = self.scenes("sort=random&seed=12345")
        self.assertEqual(f["sort"], "random_12345")
        _, f = self.scenes("sort=random")
        self.assertEqual(f["sort"], "random_0")

    def test_the_default_is_newest_first_and_titles_run_a_to_z(self):
        _, f = self.scenes("")
        self.assertEqual((f["sort"], f["direction"]), ("date", "DESC"))
        _, f = self.scenes("sort=title")
        self.assertEqual(f["direction"], "ASC")

    def test_an_unknown_sort_or_direction_is_a_400_and_never_reaches_stash(self):
        self.gql_calls.clear()
        self.assertEqual(self.call("GET", "/api/scenes?sort=1;drop")[0], 400)
        self.assertEqual(self.call("GET", "/api/scenes?dir=sideways")[0], 400)
        self.assertEqual(self.gql_calls, [])


class JobFieldsTest(ServerCase):
    def test_a_plain_job_says_what_it_is(self):
        status, job = self.call("POST", "/api/decensor", {"scene_id": "5", "backend": "decensor", "post_upscale": True,
                                                          "restoration": "ltx", "ltx_auto_split": 40})
        self.assertEqual(status, 202)
        self.assertEqual((job["op"], job["quality"], job["scene_title"]), ("decensor_up", "balanced", "My Scene"))
        self.assertGreater(job["created"], time.time() - 60)
        self.assertEqual(job["result"], None)
        self.assertEqual(self.call("POST", "/api/decensor", {"scene_id": "5", "backend": "upscale"})[1]["op"], "upscale")
        fast = self.call("POST", "/api/decensor", {"scene_id": "5", "backend": "decensor", "restoration": "basicvsrpp"})[1]
        self.assertEqual(fast["quality"], "fast")
        high = self.call("POST", "/api/decensor", {"scene_id": "5", "backend": "decensor", "restoration": "ltx"})[1]
        self.assertEqual(high["quality"], "high")

    def test_a_job_still_submits_when_stash_cannot_name_the_scene(self):
        with mock.patch.object(server, "stash_gql", side_effect=RuntimeError("down")):
            status, job = self.call("POST", "/api/decensor", {"scene_id": "5", "backend": "decensor"})
        self.assertEqual((status, job["scene_title"]), (202, None))

    def test_a_cloud_job_carries_its_estimate_quality_and_title(self):
        quote = self.call("GET", "/api/cloud/quote?scenes=5&preset=detail")[1]["quotes"][0]
        status, job = self.call("POST", "/api/decensor", {"scene_id": "5", "backend": "decensor", "cloud_preset": "detail",
                                                          "confirm_usd": quote["est_usd"]})
        self.assertEqual(status, 202, job)
        self.assertEqual((job["op"], job["quality"], job["scene_title"]), ("decensor", "balanced", "My Scene"))
        self.assertEqual(job["cloud_est_usd"], quote["est_usd"])
        self.assertEqual(job["cloud_est"], quote["est_usd"])

    def test_queued_jobs_get_a_wait_from_the_running_job_and_the_paid_estimates_ahead(self):
        running = server.new_job("1", {})
        server.set_job(running["id"], state="running", eta=120)
        first = server.new_job("2", {}, cloud={"est_usd": 1.0, "cap": 3.0, "label": "x", "_": 0, "est_min": 10})
        second = server.new_job("3", {}, cloud={"est_usd": 1.0, "cap": 3.0, "label": "x", "est_min": 5})
        third = server.new_job("4", {})                        # a home job: its length is unknown
        etas = {j["id"]: j["eta"] for j in server.jobs_snapshot()}
        self.assertEqual(etas[first["id"]], 120)
        self.assertEqual(etas[second["id"]], 120 + 600)
        self.assertEqual(etas[third["id"]], 120 + 600 + 300)
        self.assertEqual(etas[running["id"]], 120)
        server.set_job(running["id"], eta=None)                # the running job's end is unknown: so is every wait
        self.assertTrue(all(j["eta"] is None for j in server.jobs_snapshot() if j["state"] == "queued"))

    def test_the_cloud_phase_passes_through_the_progress_callback(self):
        job = server.new_job("1", {})
        server.progress_cb(job["id"])(0.5, "Uploading the video", {"cloud_phase": "upload"})
        self.assertEqual(server._jobs[job["id"]]["cloud_phase"], "upload")

    def test_log_batches_follow_running_jobs_and_advance(self):
        job = server.new_job("1", {})
        server.set_job(job["id"], state="running")
        server.push_log(job["id"], "one")
        cursors = {}
        (batch,) = server.log_batches(cursors)
        self.assertEqual((batch["job_id"], batch["cursor"], [x["text"] for x in batch["lines"]]),
                         (job["id"], 1, ["one"]))
        self.assertEqual(server.log_batches(cursors), [])
        server.push_log(job["id"], "two")
        (batch,) = server.log_batches(cursors)
        self.assertEqual([x["seq"] for x in batch["lines"]], [2])
        self.assertEqual(len(server.log_snapshot()[0]["lines"]), 2)
        server.set_job(job["id"], state="review_ready")
        self.assertEqual(server.log_batches(cursors), [])
        self.assertEqual(cursors, {})


class CloudShapeTest(ServerCase):
    def test_offers_for_several_scenes_sum_the_estimate_and_include_the_caps(self):
        one = self.call("GET", "/api/cloud/offers?scene=5&limit=200")[1]
        status, many = self.call("GET", "/api/cloud/offers?scene_ids=5,5&limit=200")
        self.assertEqual(status, 200, many)
        self.assertEqual(many["for"]["scenes"], 2)
        by = {o["id"]: o for o in one["offers"]}
        for o in many["offers"]:
            if o["id"] in by and o.get("estimate") and by[o["id"]].get("estimate"):
                self.assertAlmostEqual(o["estimate"]["est_usd"], 2 * by[o["id"]]["estimate"]["est_usd"], places=2)
                self.assertAlmostEqual(o["estimate"]["hours"], 2 * by[o["id"]]["estimate"]["hours"], places=2)
                break
        else:
            self.fail("no priced offer to compare")
        self.assertEqual(set(one["caps"]), {"max_price", "job_cap", "month_cap", "max_job_hours", "month_to_date"})
        self.assertEqual((one["caps"]["job_cap"], one["caps"]["month_cap"]), (3.0, 10.0))

    def test_an_unknown_scene_in_the_list_is_404(self):
        with mock.patch.object(server.cloud_api, "scene_metas", return_value=[SCENE, None]):
            status, payload = self.call("GET", "/api/cloud/offers?scene_ids=5,6")
        self.assertEqual((status, payload["code"]), (404, "no_scene"))

    def test_a_running_machine_says_which_scene_it_is_for(self):
        meta = {"scene": "My Scene", "scene_id": "5", "preset": "detail"}
        with self.cl.rent(self.spec(), meta=meta):
            status, payload = self.call("GET", "/api/cloud/status")
        self.assertEqual(status, 200)
        (row,) = payload["active"]
        self.assertEqual((row["scene"], row["scene_id"], row["preset"]), ("My Scene", "5", "detail"))
        self.assertIn("usd", row)
        self.assertGreater(row["started"], 0)

    def test_settings_always_show_the_jasna_licence_row(self):
        status, view = self.call("GET", "/api/cloud/settings")
        self.assertEqual(status, 200)
        self.assertTrue(view["jasna_license"]["set"])
        for k in ("max_bw_cost", "max_job_hours", "datacenter_only"):
            self.assertIn(k, view)


if __name__ == "__main__":
    unittest.main()
