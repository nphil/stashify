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
import urllib.parse
from http.server import ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cloudfakes as cf              # noqa: E402
from cloudfakes import KEY, LICENCE, UPSCALE, Resp, market, raw_offer   # noqa: E402
import cloud_api                     # noqa: E402
import cloud_jasna as cj             # noqa: E402
import hireling                       # noqa: E402
import core                          # noqa: E402
import server                        # noqa: E402

SCENE = {"id": "5", "title": "My Scene [1080p]", "duration": 600.0, "size": 50_000_000,
         "height": 1080, "width": 1920}
# A host that charges for its disk and for traffic both ways (Vast's prices: $/GB/month and $/GB).
PRICED = (15, "RTX 4090", 0.45, {"storage_cost": 0.72, "inet_up_cost": 0.01, "inet_down_cost": 0.004})


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

    def test_bandwidth_ceiling_job_hours_and_datacenter_only_are_settings(self):
        status, view = cloud_api.settings_save(self.cl, {"max_bw_cost": 0.05, "max_job_hours": 6, "datacenter_only": True})
        self.assertEqual(status, 200)
        shown = cloud_api.settings_get(self.cl)[1]
        for each in (view, shown):
            self.assertEqual((each["max_bw_cost"], each["max_job_hours"], each["datacenter_only"]), (0.05, 6.0, True))

    def test_their_bad_values_are_a_400_and_change_nothing(self):
        before = cloud_api.settings_get(self.cl)[1]
        for bad in ({"max_job_hours": 0}, {"max_job_hours": 169}, {"max_job_hours": "abc"},
                    {"max_bw_cost": -1}, {"max_bw_cost": 11}):
            status, payload = cloud_api.settings_save(self.cl, bad)
            self.assertEqual(status, 400, bad)
            self.assertIn(next(iter(bad)), payload["error"])
        self.assertEqual(cloud_api.settings_get(self.cl)[1], before)

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


class StatusApiTest(cf.CloudCase):
    def test_every_provider_is_listed_with_its_credit(self):
        status, payload = cloud_api.status(self.cl)
        self.assertEqual(status, 200)
        by_name = {p["name"]: p for p in payload["providers"]}
        self.assertEqual(set(by_name), {"vast", "runpod"})
        vast = by_name["vast"]
        self.assertEqual((vast["display_name"], vast["enabled"], vast["credit"], vast["credit_error"]),
                         ("Vast.ai", True, 12.34, None))
        self.assertFalse(by_name["runpod"]["enabled"])
        self.assertNotIn("credit", by_name["runpod"])                  # no key: nothing to look up
        self.assertNotIn(KEY, json.dumps(payload))

    def test_a_credit_lookup_that_fails_is_reported_not_raised(self):
        self.clock.sleep(61)                                     # past the one-minute credit cache
        self.vast.once.append(["GET", r"/users/current", Resp(401, {"error": "invalid key " + KEY})])
        payload = cloud_api.status(self.cl)[1]
        vast = next(p for p in payload["providers"] if p["name"] == "vast")
        self.assertIsNone(vast["credit"])
        self.assertTrue(vast["credit_error"])
        self.assertNotIn(KEY, json.dumps(payload))


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

    def test_a_scene_gives_every_machine_its_own_estimate_with_the_breakdown(self):
        self.vast.offers = market([PRICED])
        status, payload = cloud_api.offers(self.cl, {"sort": "price_asc"}, SCENE)
        self.assertEqual(status, 200)
        self.assertEqual(payload["for"], {"scene_id": "5", "title": "My Scene [1080p]", "op": "decensor",
                                          "preset": "detail", "upscale": None, "upscale_height": None,
                                          "label": "High detail", "estimated": True})
        self.assertEqual([o["id"] for o in payload["offers"]][:2], [13, 12])         # cheapest hour first
        priced = next(o for o in payload["offers"] if o["id"] == 15)
        est = priced["estimate"]
        b = est["breakdown"]
        self.assertTrue(b["storage"] > 0 and b["bw_down"] > 0 and b["bw_up"] > 0, b)
        self.assertAlmostEqual(b["total"], b["compute"] + b["storage"] + b["bandwidth"], delta=0.01)
        self.assertAlmostEqual(b["bandwidth"], b["bw_down"] + b["bw_up"], delta=0.01)
        self.assertGreaterEqual(est["est_usd"], b["total"])
        self.assertLess(est["est_usd"], b["total"] + 0.0101)                         # rounded UP to the cent
        self.assertAlmostEqual(est["total_min"], est["setup_min"] + est["transfer_min"] + est["processing_min"],
                               delta=0.2)
        self.assertAlmostEqual(est["hours"], est["total_min"] / 60, delta=0.01)
        self.assertTrue(est["disk_gb"] > 0 and est["assumptions"])
        self.assertNotIn(KEY, json.dumps(payload))

    def test_the_estimate_a_machine_shows_is_the_price_the_quote_asks_for(self):
        self.vast.offers = market([PRICED])
        offers = cloud_api.offers(self.cl, {}, SCENE)[1]["offers"]
        self.assertEqual(len(offers), 5)
        for offer in offers:
            quote = cj.build_quote(self.cl, SCENE, "detail", plan=cj.plan_from_body({"offer_id": offer["id"]}))
            self.assertEqual((quote["est_usd"], quote["hours"]),
                             (offer["estimate"]["est_usd"], offer["estimate"]["hours"]), offer["id"])

    def test_the_upscale_job_is_costed_as_an_upscale(self):
        self.vast.offers = market()
        plain = cloud_api.offers(self.cl, {"sort": "price_asc"}, SCENE)[1]
        status, up = cloud_api.offers(self.cl, {"sort": "price_asc", "op": "upscale", "upscale": "rtx-super-res",
                                                "upscale_height": "1440", "preset": "close-ups"}, SCENE)
        self.assertEqual(status, 200)
        self.assertEqual((up["for"]["op"], up["for"]["preset"], up["for"]["upscale"], up["for"]["upscale_height"]),
                         ("upscale", None, "rtx-super-res", 1440))           # an upscale has no restoration preset
        self.assertLess(up["offers"][0]["estimate"]["processing_min"], plain["offers"][0]["estimate"]["processing_min"])

    def test_hardware_presets_each_name_one_machine_and_what_the_job_costs_on_it(self):
        self.vast.offers = market()
        payload = cloud_api.offers(self.cl, {}, SCENE)[1]
        by_id = {p["id"]: p for p in payload["hardware_presets"]}
        self.assertEqual(set(by_id), {"cheapest", "fastest", "best_value", "most_reliable"})
        self.assertEqual((by_id["cheapest"]["offer_id"], by_id["cheapest"]["gpu"]), (13, "RTX 3090"))
        self.assertEqual(by_id["most_reliable"]["offer_id"], 14)
        self.assertEqual(by_id["fastest"]["offer_id"], 11)
        listed = {o["id"]: o for o in payload["offers"]}
        for pick in by_id.values():
            self.assertEqual(pick["provider"], "vast")
            self.assertEqual(pick["estimate"], listed[pick["offer_id"]]["estimate"])

    def test_without_a_scene_there_is_no_estimate_and_no_cost_sort(self):
        payload = cloud_api.offers(self.cl, {})[1]
        self.assertIsNone(payload["for"])
        self.assertTrue(all(o["estimate"] is None for o in payload["offers"]))
        sorts = {s["id"]: s["available"] for s in payload["sorts"]}
        self.assertEqual((sorts["price_asc"], sorts["est_total"], sorts["est_time"]), (True, False, False))

    def test_a_plain_rental_can_be_costed_by_the_hour(self):
        self.vast.offers = market()
        first = cloud_api.offers(self.cl, {"hours": "2", "sort": "price_asc"})[1]["offers"][0]
        self.assertEqual((first["id"], first["estimate"]["hours"], first["estimate"]["est_usd"]), (13, 2.0, 0.44))
        for bad in ("abc", "0", "-1", "169", "nan"):
            status, payload = cloud_api.offers(self.cl, {"hours": bad})
            self.assertEqual((status, payload["code"]), (400, "bad_query"), bad)

    def test_filters_sort_and_paging_pass_through(self):
        self.vast.offers = market()
        payload = cloud_api.offers(self.cl, {"gpu": "RTX 4090", "sort": "price_desc"})[1]
        self.assertEqual(([o["id"] for o in payload["offers"]], payload["total"], payload["sort"]),
                         ([14, 12], 2, "price_desc"))
        page = cloud_api.offers(self.cl, {"sort": "price_asc", "limit": "1", "offset": "1"})[1]
        self.assertEqual(([o["id"] for o in page["offers"]], page["total"], page["count"]), ([12], 4, 1))
        for sort_id in hireling.SORT_IDS:                                    # every sort works with a scene
            status, payload = cloud_api.offers(self.cl, {"sort": sort_id}, SCENE)
            self.assertEqual((status, payload["sort"], payload["count"]), (200, sort_id, 4), sort_id)

    def test_bad_queries_are_a_400(self):
        for query, meta, code in (({"sort": "nope"}, None, "bad_query"), ({"limit": "0"}, None, "bad_query"),
                                  ({"nope": "1"}, None, "bad_query"), ({"op": "transcode"}, SCENE, "bad_op"),
                                  ({"preset": "normal"}, SCENE, "bad_preset"),
                                  ({"op": "upscale", "upscale": "dlss5"}, SCENE, "bad_upscale"),
                                  ({"op": "upscale", "upscale_height": "720"}, SCENE, "bad_upscale_height")):
            status, payload = cloud_api.offers(self.cl, query, meta)
            self.assertEqual((status, payload["code"]), (400, code), query)

    def test_the_datacenter_only_setting_hides_community_hosts(self):
        self.vast.offers = market()
        self.assertEqual({o["host_type"] for o in cloud_api.offers(self.cl, {})[1]["offers"]},
                         {"datacenter", "community"})
        cloud_api.settings_save(self.cl, {"datacenter_only": True})
        self.assertEqual({o["host_type"] for o in cloud_api.offers(self.cl, {})[1]["offers"]}, {"datacenter"})

    def test_the_bandwidth_ceiling_setting_hides_machines_that_charge_more(self):
        self.vast.offers = [raw_offer(22, "RTX 4090", 0.30, inet_up_cost=0.50), raw_offer(23, "RTX 4090", 0.40)]
        self.assertEqual([o["id"] for o in cloud_api.offers(self.cl, {})[1]["offers"]], [23])
        cloud_api.settings_save(self.cl, {"max_bw_cost": 1.0})
        self.assertEqual(sorted(o["id"] for o in cloud_api.offers(self.cl, {})[1]["offers"]), [22, 23])


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

    def test_a_plan_changes_the_quote_and_names_the_machine(self):
        self.vast.offers = market()
        payload = cloud_api.quotes(self.cl, [("5", SCENE)], "detail", params={"hardware": "cheapest"})[1]
        quote = payload["quotes"][0]
        self.assertEqual((payload["op"], quote["machine"], quote["gpu"]), ("decensor", "chosen", "RTX 3090"))
        self.assertEqual(payload["plan"]["hardware"], "cheapest")
        plain = cloud_api.quotes(self.cl, [("5", SCENE)], "detail")[1]
        self.assertEqual(plain["quotes"][0]["machine"], "auto")
        upscaled = cloud_api.quotes(self.cl, [("5", SCENE)], "detail", params=UPSCALE)[1]
        self.assertEqual(upscaled["op"], "decensor+upscale")
        self.assertGreater(upscaled["quotes"][0]["est_usd"], plain["quotes"][0]["est_usd"])

    def test_the_upscale_op_ignores_the_preset(self):
        status, payload = cloud_api.quotes(self.cl, [("5", SCENE)], "ultra", params={"op": "upscale"})
        self.assertEqual(status, 200)                              # "ultra" is no preset, and an upscale needs none
        self.assertTrue(payload["quotes"][0]["ok"])
        self.assertEqual(payload["op"], "upscale")

    def test_a_bad_plan_is_a_400(self):
        for params, code in (({"op": "transcode"}, "bad_op"), ({"op": "upscale", "upscale": "dlss5"}, "bad_upscale"),
                             ({"hardware": "turbo"}, "bad_hardware")):
            status, payload = cloud_api.quotes(self.cl, [("5", SCENE)], "detail", params=params)
            self.assertEqual((status, payload["code"]), (400, code), params)

    def test_the_datacenter_only_setting_decides_whether_a_community_host_may_run_the_job(self):
        self.vast.offers = [raw_offer(21, "RTX 4090", 0.30, datacenter=False)]
        self.assertTrue(cloud_api.quotes(self.cl, [("5", SCENE)], "detail")[1]["quotes"][0]["ok"])
        cloud_api.settings_save(self.cl, {"datacenter_only": True})
        quote = cloud_api.quotes(self.cl, [("5", SCENE)], "detail")[1]["quotes"][0]
        self.assertEqual(quote["refusal"]["code"], "no_offer")

    def test_the_bandwidth_ceiling_setting_decides_whether_a_pricey_host_may_run_the_job(self):
        self.vast.offers = [raw_offer(22, "RTX 4090", 0.30, inet_up_cost=0.50)]
        self.assertEqual(cloud_api.quotes(self.cl, [("5", SCENE)], "detail")[1]["quotes"][0]["refusal"]["code"],
                         "no_offer")
        cloud_api.settings_save(self.cl, {"max_bw_cost": 1.0})
        self.assertTrue(cloud_api.quotes(self.cl, [("5", SCENE)], "detail")[1]["quotes"][0]["ok"])

    def test_the_job_hours_setting_limits_how_long_a_rental_may_run(self):
        self.settings.update({"job_cap": 100.0, "month_cap": 1000.0})
        long_scene = dict(SCENE, duration=7200.0)                   # two hours of video: far over 3 hours of work
        quote = cloud_api.quotes(self.cl, [("5", long_scene)], "detail")[1]["quotes"][0]
        self.assertEqual(quote["refusal"]["code"], "over_job_hours")
        cloud_api.settings_save(self.cl, {"max_job_hours": 48})
        quote = cloud_api.quotes(self.cl, [("5", long_scene)], "detail")[1]["quotes"][0]
        self.assertTrue(quote["ok"])
        self.assertEqual(quote["max_job_hours"], 48.0)


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

    def test_only_decensor_and_upscale_runs_go_to_the_cloud(self):
        for backend in ("transcode", "command"):
            self.assertEqual(self.admit(backend=backend)[1]["code"], "bad_op", backend)
        self.assertEqual(self.admit(self.body(op="transcode"))[1]["code"], "bad_op")

    def test_an_unknown_scene_is_a_404(self):
        self.assertEqual(self.admit(meta=None)[0], 404)

    def test_no_refusal_rents_anything(self):
        self.settings.update({"job_cap": 0.20})
        self.admit()
        self.assertEqual(self.vast.count("PUT", "/asks/"), 0)
        self.assertEqual(self.vast.live(), [])

    def plan_body(self, plan, meta=SCENE, **kw):
        """A request body for `plan` carrying the price the user would have seen for it."""
        quote = cj.build_quote(self.cl, meta, "detail", plan=cj.plan_from_body(plan))
        body = {"cloud_preset": "detail", "confirm_usd": quote["est_usd"] if quote.get("ok") else 1.0}
        body.update(plan)
        body.update(kw)
        return body

    def test_a_decensor_with_an_upscale_after_it_is_admitted(self):
        status, rec = self.admit(self.plan_body(UPSCALE))
        self.assertEqual(status, 202)
        self.assertEqual((rec["op"], rec["plan"]["upscale"], rec["plan"]["upscale_height"]),
                         ("decensor+upscale", "span", 2160))
        self.assertGreater(rec["est_usd"], self.est())                   # the extra pass costs more

    def test_an_upscale_alone_is_admitted_without_a_preset(self):
        plan = {"op": "upscale", "upscale": "rtx-super-res", "upscale_height": 1440}
        body = self.plan_body(plan)
        del body["cloud_preset"]
        status, rec = self.admit(body)
        self.assertEqual((status, rec["op"]), (202, "upscale"))

    def test_the_upscale_backend_alone_means_an_upscale(self):
        quote = cj.build_quote(self.cl, SCENE, "detail", plan=cj.plan_from_body({}, "upscale"))
        status, rec = self.admit({"confirm_usd": quote["est_usd"]}, backend="upscale")
        self.assertEqual((status, rec["op"], rec["plan"]["upscale"], rec["plan"]["upscale_height"]),
                         (202, "upscale", "span", 2160))

    def test_a_hardware_choice_is_pinned_to_one_machine_at_admission(self):
        self.vast.offers = market()
        status, rec = self.admit(self.plan_body({"hardware": "cheapest"}))
        self.assertEqual(status, 202)
        self.assertEqual((str(rec["offer_id"]), rec["provider"], rec["gpu"]), ("13", "vast", "RTX 3090"))
        self.assertEqual((rec["hardware"], rec["plan"]["hardware"]), ("cheapest", ""))   # shown, but the run is pinned
        again = cj.plan_from_body(rec["plan"])                           # the stored plan is a valid request
        self.assertEqual((again.offer_id, again.hardware), ("13", ""))

    def test_a_picked_machine_that_is_gone_is_refused(self):
        self.vast.offers = market()
        self.vast.calls.clear()
        status, payload = self.admit(self.plan_body({"offer_id": "999"}))
        self.assertEqual((status, payload["code"]), (409, "offer_gone"))
        self.assertEqual(self.vast.count("PUT", "/asks/"), 0)

    def test_a_bad_plan_is_a_400_and_asks_the_market_nothing(self):
        self.vast.calls.clear()
        for plan, code in (({"op": "transcode"}, "bad_op"),
                           ({"op": "decensor+upscale", "upscale": "dlss5"}, "bad_upscale"),
                           ({"op": "decensor+upscale", "upscale_height": 720}, "bad_upscale_height"),
                           ({"hardware": "turbo"}, "bad_hardware"),
                           ({"hardware": "cheapest", "offer_id": "13"}, "bad_hardware"),
                           ({"offer_id": "no way!"}, "bad_offer"), ({"provider": "a/b"}, "bad_provider"),
                           ({"gpu": "<script>"}, "bad_gpu"), ({"cloud_filters": {"nope": 1}}, "bad_filters")):
            status, payload = self.admit(dict(plan, cloud_preset="detail", confirm_usd=1.0))
            self.assertEqual((status, payload["code"]), (400, code), plan)
        self.assertEqual(self.vast.calls, [])

    def test_a_run_longer_than_the_job_hours_setting_is_refused(self):
        self.settings.update({"job_cap": 100.0, "month_cap": 1000.0})
        long_scene = dict(SCENE, duration=7200.0)
        status, payload = self.admit(self.body(confirm_usd=1.0), meta=long_scene)
        self.assertEqual((status, payload["code"]), (409, "over_job_hours"))
        self.settings.update({"max_job_hours": 48.0})
        quote = cj.build_quote(self.cl, long_scene, "detail")
        self.assertEqual(self.admit(self.body(confirm_usd=quote["est_usd"]), meta=long_scene)[0], 202)

    def test_an_upscale_that_would_change_nothing_is_refused(self):
        already = dict(SCENE, height=2160, width=3840)
        plan = {"op": "upscale", "upscale": "span", "upscale_height": 2160}
        status, payload = self.admit(dict(plan, confirm_usd=1.0), meta=already)
        self.assertEqual((status, payload["code"]), (409, "upscale_not_needed"))

    def test_filters_narrow_the_machine_the_job_runs_on(self):
        self.vast.offers = market()
        status, rec = self.admit(self.plan_body({"gpu": "RTX 3090"}))
        self.assertEqual((status, rec["gpu"]), (202, "RTX 3090"))
        status, rec = self.admit(self.plan_body({"cloud_filters": {"max_price": 0.25}}))
        self.assertEqual((status, rec["gpu"]), (202, "RTX 3090"))


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
        self.assertEqual(self.submit(backend="transcode")[1]["code"], "bad_op")
        self.assertEqual(self.submit(op="transcode")[1]["code"], "bad_op")
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

    # -- plans, machines and status ---------------------------------------------- #

    def plan_submit(self, plan, **extra):
        """Quote `plan` over HTTP, then confirm exactly that price (what the dashboard does)."""
        status, payload = self.call("GET", "/api/cloud/quote?scenes=5&preset=detail&" + urllib.parse.urlencode(plan))
        self.assertEqual(status, 200, payload)
        body = {"scene_id": "5", "backend": "decensor", "cloud_preset": "detail",
                "confirm_usd": payload["quotes"][0]["est_usd"]}
        body.update(plan)
        body.update(extra)
        return self.call("POST", "/api/decensor", body)

    def test_the_quote_route_takes_the_plan_in_the_query_string(self):
        plain = self.quote()["est_usd"]
        quotes = [self.call("GET", "/api/cloud/quote?scenes=5&preset=detail&op=" + spelling)[1]
                  for spelling in ("decensor%2Bupscale", "decensor+upscale")]          # a '+' arrives as a space
        self.assertEqual([q["op"] for q in quotes], ["decensor+upscale"] * 2)
        self.assertEqual(quotes[0]["quotes"][0]["est_usd"], quotes[1]["quotes"][0]["est_usd"])
        self.assertGreater(quotes[0]["quotes"][0]["est_usd"], plain)

    def test_the_offers_route_prices_the_scene(self):
        self.vast.offers = market()
        status, body = self.call("GET", "/api/cloud/offers?scene=5&op=upscale&upscale_height=2160&sort=est_total&limit=3")
        self.assertEqual(status, 200)
        self.assertEqual((body["for"]["scene_id"], body["for"]["op"], body["for"]["upscale_height"]), ("5", "upscale", 2160))
        prices = [o["estimate"]["est_usd"] for o in body["offers"]]
        self.assertEqual((len(prices), prices), (3, sorted(prices)))               # cheapest job first
        self.assertNotIn(KEY, json.dumps(body))

    def test_the_offers_route_has_its_own_errors(self):
        self.assertEqual(self.call("GET", "/api/cloud/offers?sort=nope")[1]["code"], "bad_query")
        self.assertEqual(self.call("GET", "/api/cloud/offers?scene=5&op=transcode")[1]["code"], "bad_op")
        self.gql_scene = None
        status, body = self.call("GET", "/api/cloud/offers?scene=5")
        self.assertEqual((status, body["code"]), (404, "no_scene"))

    def test_the_status_route_lists_the_providers_with_their_credit(self):
        status, body = self.call("GET", "/api/cloud/status")
        self.assertEqual(status, 200)
        vast = next(p for p in body["providers"] if p["name"] == "vast")
        self.assertEqual((vast["enabled"], vast["credit"]), (True, 12.34))
        self.assertNotIn(KEY, json.dumps(body))

    def test_a_job_records_the_machine_it_was_promised(self):
        self.vast.offers = market()
        status, job = self.plan_submit({"offer_id": "14"})
        self.assertEqual(status, 202, job)
        self.assertEqual((job["cloud_op"], job["cloud_gpu"], str(job["cloud_offer_id"]), job["cloud_provider"]),
                         ("decensor", "RTX 4090", "14", "vast"))
        self.assertNotIn("_cloud", job)
        self.assertEqual(server._jobs[job["id"]]["_cloud"]["plan"]["offer_id"], "14")      # the run reads it from here

    def test_a_hardware_choice_is_pinned_when_the_job_is_queued(self):
        self.vast.offers = market()
        status, job = self.plan_submit({"hardware": "most_reliable"})
        self.assertEqual(status, 202, job)
        self.assertEqual((str(job["cloud_offer_id"]), job["cloud_hardware"]), ("14", "most_reliable"))
        plan = server._jobs[job["id"]]["_cloud"]["plan"]
        self.assertEqual((plan["offer_id"], plan["hardware"]), ("14", ""))

    def test_the_operation_decides_what_the_worker_runs(self):
        cases = ((UPSCALE, "decensor", True, "decensor+upscale"),
                 ({"op": "upscale"}, "upscale", False, "upscale"),
                 ({}, "decensor", False, "decensor"))
        for plan, backend, post_upscale, op in cases:
            status, job = self.plan_submit(plan)
            self.assertEqual((status, job["cloud_op"]), (202, op), plan)
            cfg = server.job_config(server._jobs[job["id"]])
            self.assertEqual((cfg["backend"], cfg["postUpscale"], cfg["cloud"]["op"]), (backend, post_upscale, op), plan)

    def test_a_stray_post_upscale_setting_cannot_turn_a_plain_decensor_into_two_steps(self):
        status, job = self.plan_submit({})
        with mock.patch.dict(os.environ, {"POST_UPSCALE": "true"}):
            self.assertFalse(server.job_config(server._jobs[job["id"]])["postUpscale"])

    def test_an_upscale_can_go_to_the_cloud_without_a_preset(self):
        plan = {"op": "upscale", "upscale": "span", "upscale_height": "1440"}      # the scene is 1080p
        quote = self.call("GET", "/api/cloud/quote?scenes=5&" + urllib.parse.urlencode(plan))[1]["quotes"][0]
        status, job = self.call("POST", "/api/decensor", dict(plan, scene_id="5", cloud=True,
                                                              confirm_usd=quote["est_usd"]))
        self.assertEqual(status, 202, job)
        self.assertEqual((job["cloud"], job["cloud_op"]), (True, "upscale"))

    def test_a_bad_plan_or_a_vanished_machine_queues_nothing(self):
        self.assertEqual(self.submit(op="upscale", upscale="dlss5")[1]["code"], "bad_upscale")
        self.assertEqual(self.submit(hardware="turbo")[1]["code"], "bad_hardware")
        status, body = self.submit(offer_id="999", confirm_usd=1.0)
        self.assertEqual((status, body["code"]), (409, "offer_gone"))
        self.assertNothingQueuedOrRented()


if __name__ == "__main__":
    unittest.main()
