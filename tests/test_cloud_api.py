"""cloud_api (settings, key test, quotes, admission) and the /api/cloud routes plus the paid
branch of POST /api/decensor in server.py. Fake Vast, throwaway directories, a loopback HTTP
server: nothing is rented and no real time passes."""

import http.client
import json
import os
import stat
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cloudfakes as cf              # noqa: E402
from cloudfakes import KEY, LICENCE, Resp   # noqa: E402
import cloud_api                     # noqa: E402
import cloud_jasna as cj             # noqa: E402
import hireling                       # noqa: E402
import core                          # noqa: E402
import server                        # noqa: E402

SCENE = {"id": "5", "title": "My Scene [1080p]", "duration": 600.0, "size": 50_000_000,
         "height": 1080, "width": 1920}


def stash_scene(sid="5"):
    return {"id": sid, "title": "My Scene", "files": [
        {"path": "/media/My Scene.mkv", "width": 1920, "height": 1080, "duration": 600.0, "size": 50_000_000}]}


class SettingsApiTest(cf.CloudCase):
    def test_the_view_has_no_secret_values(self):
        status, view = cloud_api.settings_get(self.cl)
        self.assertEqual(status, 200)
        text = json.dumps(view)
        self.assertNotIn(KEY, text)
        self.assertNotIn(LICENCE, text)
        self.assertTrue(view["vast_api_key"]["set"])
        self.assertEqual(view["vast_api_key"]["last4"], KEY[-4:])
        self.assertEqual(view["jasna_license"]["last4"], LICENCE[-4:])
        self.assertEqual((view["max_price"], view["job_cap"], view["month_cap"]), (1.0, 3.0, 10.0))

    def test_saving_changes_the_caps(self):
        status, view = cloud_api.settings_save(self.cl, {"job_cap": 2.5, "month_cap": 7})
        self.assertEqual(status, 200)
        self.assertEqual((view["job_cap"], view["month_cap"]), (2.5, 7.0))
        self.assertNotIn(KEY, json.dumps(view))

    def test_a_bad_value_is_a_400_and_changes_nothing(self):
        status, payload = cloud_api.settings_save(self.cl, {"job_cap": "abc", "month_cap": 4})
        self.assertEqual(status, 400)
        self.assertIn("job_cap", payload["error"])
        self.assertEqual(cloud_api.settings_get(self.cl)[1]["month_cap"], 10.0)
        self.assertEqual(cloud_api.settings_save(self.cl, ["not", "an", "object"])[0], 400)

    def test_the_key_cannot_change_while_a_machine_runs(self):
        new_key = "another-vast-key-0123456789"
        with self.cl.rent(self.spec()):
            self.assertTrue(self.cl.busy())
            status, payload = cloud_api.settings_save(self.cl, {"vast_api_key": new_key})
            self.assertEqual((status, payload["code"]), (409, "busy"))
            self.assertEqual(cloud_api.settings_save(self.cl, {"vast_api_key": None})[0], 409)
            self.assertEqual(cloud_api.settings_save(self.cl, {"job_cap": 2})[0], 200)   # other fields are fine
            self.assertEqual(self.settings.get("vast_api_key"), KEY)
        self.assertFalse(self.cl.busy())
        self.assertEqual(cloud_api.settings_save(self.cl, {"vast_api_key": new_key})[0], 200)
        self.assertEqual(self.settings.get("vast_api_key"), new_key)
        self.assertEqual(self.vast.live(), [])

    def test_test_key_shows_the_balance(self):
        status, result = cloud_api.test_key(self.cl, {})
        self.assertEqual(status, 200)
        self.assertEqual((result["ok"], result["balance"], result["provider"]), (True, 12.34, "vast"))
        self.assertEqual(cloud_api.test_key(self.cl, {"vast_api_key": "typed-but-unsaved-key-123"})[1]["ok"], True)
        self.assertEqual(self.settings.get("vast_api_key"), KEY)        # testing does not save

    def test_a_rejected_key_is_reported_without_the_key(self):
        self.vast.once.append(["GET", r"/users/current", Resp(401, {"error": "invalid key " + KEY})])
        status, result = cloud_api.test_key(self.cl, {})
        self.assertEqual(status, 200)
        self.assertFalse(result["ok"])
        self.assertNotIn(KEY, json.dumps(result))


class OffersApiTest(cf.CloudCase):
    def test_needs_a_key(self):
        self.settings.update({"vast_api_key": None})
        status, payload = cloud_api.offers(self.cl, {})
        self.assertEqual((status, payload["code"]), (409, "no_key"))
        self.assertEqual(self.vast.calls, [])

    def test_lists_the_market_with_compatibility_marks(self):
        status, payload = cloud_api.offers(self.cl, {})
        self.assertEqual(status, 200)
        self.assertEqual({o["gpu"] for o in payload["offers"]} <= {"RTX 4090", "RTX 3090", "RTX 5090"}, True)
        self.assertTrue(all(o["compatible"] is True for o in payload["offers"]))
        self.assertEqual(payload["filters"]["max_price"], 1.0)
        self.assertNotIn(KEY, json.dumps(payload))

    def test_a_search_failure_is_a_502(self):
        self.vast.once.append(["POST", r"/bundles/", Resp(401, {"error": "invalid key " + KEY})])
        status, payload = cloud_api.offers(self.cl, {})
        self.assertEqual((status, payload["code"]), (502, "offers_unavailable"))
        self.assertNotIn(KEY, json.dumps(payload))


class SceneMetasTest(unittest.TestCase):
    def test_one_round_trip_digits_only(self):
        queries = []

        def gql(query):
            queries.append(query)
            return {"s0": stash_scene("5"), "s1": None}

        metas = cloud_api.scene_metas(gql, ["5", '7"){x}', "9"])
        self.assertEqual(len(queries), 1)
        self.assertNotIn("{x}", queries[0])                       # a hostile id never reaches the query
        self.assertEqual(metas[0]["id"], "5")
        self.assertEqual(metas[0]["duration"], 600.0)
        self.assertIsNone(metas[1])
        self.assertIsNone(metas[2])                               # Stash said null: unknown scene

    def test_no_valid_ids_means_no_request(self):
        def gql(query):
            raise AssertionError("must not ask Stash")
        self.assertEqual(cloud_api.scene_metas(gql, ["", "x"]), [None, None])


class QuotesApiTest(cf.CloudCase):
    def test_unknown_preset_is_a_400(self):
        self.assertEqual(cloud_api.quotes(self.cl, [("5", SCENE)], "ultra")[0], 400)

    def test_the_monthly_cap_is_cumulative_across_a_selection(self):
        self.settings.update({"month_cap": 1.0})
        status, payload = cloud_api.quotes(self.cl, [("5", SCENE), ("6", SCENE), ("7", SCENE)], "detail")
        self.assertEqual(status, 200)
        first, second, third = payload["quotes"]
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(third["refusal"]["code"], "over_month_cap")
        self.assertEqual(payload["total_usd"], round(first["est_usd"] + second["est_usd"], 2))
        self.assertEqual(payload["label"], "High detail")

    def test_an_unknown_scene_is_reported_not_quoted(self):
        payload = cloud_api.quotes(self.cl, [("404", None), ("5", SCENE)], "close-ups")[1]
        self.assertEqual(payload["quotes"][0]["refusal"]["code"], "no_scene")
        self.assertEqual(payload["quotes"][0]["scene_id"], "404")
        self.assertTrue(payload["quotes"][1]["ok"])

    def test_queued_jobs_are_counted(self):
        payload = cloud_api.quotes(self.cl, [("5", SCENE)], "detail", queued_usd=9.9)[1]
        self.assertEqual(payload["quotes"][0]["refusal"]["code"], "over_month_cap")


class AdmitTest(cf.CloudCase):
    """The server-side refusals: whatever the dashboard did, these stop a paid job."""

    def est(self, preset="detail", scene=SCENE):
        return cj.build_quote(self.cl, scene, preset)["est_usd"]

    def body(self, **kw):
        body = {"cloud_preset": "detail", "confirm_usd": self.est()}
        body.update(kw)
        return body

    def admit(self, body=None, meta=SCENE, queued=0.0, backend="decensor"):
        return cloud_api.admit(self.cl, body or self.body(), meta, queued, backend)

    def test_a_confirmed_job_is_admitted(self):
        status, rec = self.admit()
        self.assertEqual(status, 202)
        self.assertEqual((rec["preset"], rec["label"], rec["cap"], rec["gpu"]),
                         ("detail", "High detail", 3.0, "RTX 4090"))
        self.assertEqual(rec["est_usd"], self.est())
        self.assertEqual(rec["scene"]["id"], "5")
        self.assertNotIn(KEY, json.dumps(rec))
        self.assertNotIn(LICENCE, json.dumps(rec))

    def test_refused_without_a_vast_key(self):
        body = self.body()                              # the price the user saw, before the key went
        self.settings.update({"vast_api_key": None})
        self.vast.calls.clear()
        status, payload = self.admit(body)
        self.assertEqual((status, payload["code"]), (409, "no_key"))
        self.assertEqual(self.vast.calls, [])

    def test_refused_without_a_jasna_licence(self):
        body = self.body()
        self.settings.update({"jasna_license": None})
        status, payload = self.admit(body)
        self.assertEqual((status, payload["code"]), (409, "no_licence"))

    def test_refused_when_the_estimate_is_over_the_per_job_cap(self):
        self.settings.update({"job_cap": 0.20})
        status, payload = self.admit()
        self.assertEqual((status, payload["code"]), (409, "over_job_cap"))
        self.assertIn("quote", payload)

    def test_refused_when_month_to_date_plus_the_estimate_is_over_the_cap(self):
        self.cl.ledger.append({"ended": hireling.util.iso(self.clock.time()), "usd": 9.80, "status": "ok"})
        status, payload = self.admit()
        self.assertEqual((status, payload["code"]), (409, "over_month_cap"))

    def test_refused_when_queued_jobs_use_up_the_month(self):
        self.assertEqual(self.admit(queued=9.9)[1]["code"], "over_month_cap")
        self.assertEqual(self.admit(queued=1.0)[0], 202)

    def test_refused_when_the_price_moved_since_the_user_confirmed(self):
        status, payload = self.admit(self.body(confirm_usd=0.05))
        self.assertEqual((status, payload["code"]), (409, "price_changed"))
        self.assertGreater(payload["quote"]["est_usd"], 0.05)

    def test_a_job_must_carry_the_confirmed_price(self):
        for bad in (None, "", 0, -1, "free", float("nan"), float("inf")):
            body = self.body()
            body["confirm_usd"] = bad
            self.assertEqual(self.admit(body)[1]["code"], "no_confirm", bad)
        body = self.body()
        del body["confirm_usd"]
        self.assertEqual(self.admit(body)[0], 400)

    def test_only_the_three_presets_exist(self):
        for preset in ("normal", "basicvsrpp", "ltx", "ltx-8step", "", None, "Detail"):
            self.assertEqual(self.admit(self.body(cloud_preset=preset))[1]["code"], "bad_preset", preset)

    def test_cloud_is_for_plain_decensor_only(self):
        self.assertEqual(self.admit(backend="upscale")[1]["code"], "bad_op")
        self.assertEqual(self.admit(self.body(post_upscale=True))[1]["code"], "bad_op")

    def test_an_unknown_scene_is_a_404(self):
        self.assertEqual(self.admit(meta=None)[0], 404)

    def test_no_refusal_rents_anything(self):
        self.settings.update({"job_cap": 0.20})
        self.admit()
        self.assertEqual(self.vast.count("PUT", "/asks/"), 0)
        self.assertEqual(self.vast.live(), [])


class ServerRoutesTest(cf.CloudCase):
    """The real Handler on a loopback port, Stash and Vast faked."""

    TOKEN = "worker-token-1"

    def setUp(self):
        super().setUp()
        self._reset_server_state()
        self.addCleanup(self._reset_server_state)
        for patcher in (mock.patch.object(server, "TOKEN", self.TOKEN),
                        mock.patch.object(server, "stash_gql", self.fake_gql)):
            patcher.start()
            self.addCleanup(patcher.stop)
        cj.set_cloud(self.cl)
        self.addCleanup(cj.set_cloud, None)
        self.gql_scene = stash_scene("5")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @staticmethod
    def _reset_server_state():
        with server._jobs_lock:
            server._jobs.clear()
        while not server._work.empty():
            server._work.get_nowait()

    def fake_gql(self, query, variables=None):
        return {"s0": self.gql_scene}

    def call(self, method, path, body=None, token=TOKEN):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Decensor-Token"] = token
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, (json.loads(raw) if raw else None)

    def quote(self, preset="detail"):
        status, payload = self.call("GET", "/api/cloud/quote?scenes=5&preset=" + preset)
        self.assertEqual(status, 200)
        return payload["quotes"][0]

    def submit(self, **kw):
        body = {"scene_id": "5", "backend": "decensor", "cloud_preset": "detail",
                "confirm_usd": self.quote()["est_usd"]}
        body.update(kw)
        return self.call("POST", "/api/decensor", body)

    def assertNothingQueuedOrRented(self):
        self.assertEqual(dict(server._jobs), {})
        self.assertTrue(server._work.empty())
        self.assertEqual(self.vast.count("PUT", "/asks/"), 0)
        self.assertEqual(self.vast.live(), [])

    # -- auth / secrets ---------------------------------------------------------- #

    def test_every_cloud_route_needs_the_worker_token(self):
        routes = [("GET", "/api/cloud/settings"), ("GET", "/api/cloud/status"), ("GET", "/api/cloud/offers"),
                  ("GET", "/api/cloud/quote?scenes=5&preset=detail"), ("POST", "/api/cloud/settings"),
                  ("POST", "/api/cloud/test"), ("POST", "/api/decensor")]
        for method, path in routes:
            for token in (None, "wrong"):
                status, _ = self.call(method, path, {} if method == "POST" else None, token=token)
                self.assertEqual(status, 401, (method, path, token))
        self.assertNothingQueuedOrRented()

    def test_secrets_are_never_returned_and_the_file_is_private(self):
        status, body = self.call("POST", "/api/cloud/settings",
                                 {"vast_api_key": "brand-new-vast-key-abcdef", "job_cap": 2.0})
        self.assertEqual(status, 200)
        self.assertNotIn("brand-new-vast-key-abcdef", json.dumps(body))
        for path in ("/api/cloud/settings", "/api/cloud/status", "/api/cloud/offers"):
            status, body = self.call("GET", path)
            self.assertEqual(status, 200, path)
            text = json.dumps(body)
            for secret in (KEY, LICENCE, "brand-new-vast-key-abcdef"):
                self.assertNotIn(secret, text, path)
        mode = stat.S_IMODE(os.stat(os.path.join(self.dir, "cloud.json")).st_mode)
        self.assertEqual(mode, 0o600)

    def test_test_key_button(self):
        status, result = self.call("POST", "/api/cloud/test", {})
        self.assertEqual((status, result["ok"], result["balance"]), (200, True, 12.34))

    # -- the paid job ------------------------------------------------------------ #

    def test_quote_then_confirm_queues_one_paid_job(self):
        q = self.quote()
        self.assertTrue(q["ok"])
        status, job = self.submit()
        self.assertEqual(status, 202)
        self.assertTrue(job["cloud"])
        self.assertEqual((job["cloud_est"], job["cloud_cap"], job["cloud_label"]), (q["est_usd"], 3.0, "High detail"))
        self.assertNotIn("_cloud", job)
        self.assertEqual(server._work.get_nowait(), ("process", job["id"]))
        cfg = server.job_config(server._jobs[job["id"]])
        self.assertEqual((cfg["cloud"]["preset"], cfg["cloud"]["job_id"]), ("detail", job["id"]))
        self.assertEqual(job["gpu_stats"], {})                         # not this machine's GPU

    def test_a_cloud_job_needs_no_runner_but_a_normal_job_still_does(self):
        cfg = core.config_from_env()
        cfg.update(backend="decensor", runnerUrl="", runners=[])
        cfg["cloud"] = {"preset": "detail"}
        try:
            core.validate(cfg)
        except ValueError as exc:
            self.assertNotIn("runner", str(exc))
        del cfg["cloud"]
        with self.assertRaises(ValueError) as ctx:
            core.validate(cfg)
        self.assertIn("runner", str(ctx.exception))

    def test_a_normal_job_is_untouched(self):
        status, job = self.call("POST", "/api/decensor", {"scene_id": "5"})
        self.assertEqual(status, 202)
        self.assertNotIn("cloud", job)
        self.assertNotIn("cloud", server.job_config(server._jobs[job["id"]]))

    def test_refused_without_the_confirmed_price(self):
        status, body = self.submit(confirm_usd=None)
        self.assertEqual((status, body["code"]), (400, "no_confirm"))
        self.assertNothingQueuedOrRented()

    def test_refused_without_a_key_or_licence(self):
        for field, code in (("vast_api_key", "no_key"), ("jasna_license", "no_licence")):
            est = self.quote()["est_usd"]
            self.settings.update({field: None})
            status, body = self.submit(confirm_usd=est)
            self.assertEqual((status, body["code"]), (409, code))
            self.settings.update({"vast_api_key": KEY, "jasna_license": LICENCE})
        self.assertNothingQueuedOrRented()

    def test_refused_over_the_per_job_cap(self):
        est = self.quote()["est_usd"]
        self.settings.update({"job_cap": 0.10})
        status, body = self.submit(confirm_usd=est)
        self.assertEqual((status, body["code"]), (409, "over_job_cap"))
        self.assertNothingQueuedOrRented()

    def test_refused_over_the_monthly_cap(self):
        est = self.quote()["est_usd"]
        self.cl.ledger.append({"ended": hireling.util.iso(self.clock.time()), "usd": 9.80, "status": "ok"})
        status, body = self.submit(confirm_usd=est)
        self.assertEqual((status, body["code"]), (409, "over_month_cap"))
        self.assertNothingQueuedOrRented()

    def test_queued_paid_jobs_count_against_the_month(self):
        est = self.quote()["est_usd"]
        self.settings.update({"month_cap": round(est * 2.5, 2)})
        self.assertEqual(self.submit(confirm_usd=est)[0], 202)
        self.assertEqual(self.submit(confirm_usd=est)[0], 202)
        status, body = self.submit(confirm_usd=est)
        self.assertEqual((status, body["code"]), (409, "over_month_cap"))
        self.assertEqual(len(server._jobs), 2)
        self.assertEqual(server.queued_cloud_usd(), round(est * 2, 2))

    def test_refused_when_the_price_moved(self):
        status, body = self.submit(confirm_usd=0.01)
        self.assertEqual((status, body["code"]), (409, "price_changed"))
        self.assertNothingQueuedOrRented()

    def test_refused_for_other_presets_and_other_operations(self):
        self.assertEqual(self.submit(cloud_preset="normal")[1]["code"], "bad_preset")
        self.assertEqual(self.submit(cloud_preset="ltx")[1]["code"], "bad_preset")
        self.assertEqual(self.submit(backend="upscale")[1]["code"], "bad_op")
        self.assertEqual(self.submit(post_upscale=True)[1]["code"], "bad_op")
        self.assertNothingQueuedOrRented()

    def test_an_unknown_scene_is_refused(self):
        self.gql_scene = None
        status, body = self.submit(confirm_usd=1.0)
        self.assertEqual((status, body["code"]), (404, "no_scene"))
        self.assertNothingQueuedOrRented()

    def test_the_local_gpu_still_shows_for_normal_jobs(self):
        job = server.new_job("5", {})
        with mock.patch.object(server, "_gpu", {"util": 55}):
            self.assertEqual(server.public(job)["gpu_stats"], {"util": 55})


if __name__ == "__main__":
    unittest.main()
