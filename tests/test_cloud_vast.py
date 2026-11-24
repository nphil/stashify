"""cloud_vast: settings, Vast client, offers, rentals, reaper. All on fakes (no network,
no rentals, no real time). The invariant under test, again and again: whatever happens,
the rented box is destroyed (or the registry keeps it for the reaper)."""

import contextlib
import json
import os
import re
import stat
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cloudfakes as cf              # noqa: E402
from cloudfakes import KEY, LICENCE, INSTANCE_KEY, Resp, raw_offer   # noqa: E402
import cloud_ssh                     # noqa: E402
import cloud_vast                    # noqa: E402
from cloud_vast import (BoxFailed, LifetimeExceeded, RentalCancelled, SpendCapReached,   # noqa: E402
                        TooManyRentals, VerifyFailed)


class Cancelled(Exception):
    """Same name as core.Cancelled: the ledger recognises it by name."""


class SettingsTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="cloudset-")
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "cloud.json")
        self.s = cloud_vast.Settings(self.path, env={})

    def test_defaults_are_the_owners_numbers(self):
        cfg = self.s.config()
        self.assertEqual((cfg.max_price, cfg.job_cap, cfg.month_cap), (1.00, 3.00, 10.00))
        self.assertEqual(cfg.api_key, "")

    def test_secrets_are_write_only_and_the_file_is_private(self):
        self.s.update({"vast_api_key": KEY, "jasna_license": LICENCE, "job_cap": 2})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        pub = self.s.public()
        blob = json.dumps(pub)
        self.assertNotIn(KEY, blob)
        self.assertNotIn(LICENCE, blob)
        self.assertEqual(pub["vast_api_key"], {"set": True, "last4": KEY[-4:], "source": "file"})
        self.assertEqual(pub["jasna_license"]["last4"], LICENCE[-4:])
        self.assertEqual(pub["job_cap"], 2.0)
        self.assertEqual(self.s.get("vast_api_key"), KEY)        # the server itself can read it

    def test_short_secrets_show_no_last4(self):
        self.s.update({"vast_api_key": "abcdefgh"})              # 8 chars: allowed, but too short to hint
        self.assertEqual(self.s.public()["vast_api_key"]["last4"], "")

    def test_blank_keeps_null_clears(self):
        self.s.update({"vast_api_key": KEY})
        self.s.update({"vast_api_key": "   ", "month_cap": 20})
        self.assertEqual(self.s.get("vast_api_key"), KEY)
        self.s.update({"vast_api_key": None})
        self.assertFalse(self.s.public()["vast_api_key"]["set"])
        self.assertEqual(self.s.get("month_cap"), 20.0)

    def test_bad_input_is_refused_and_changes_nothing(self):
        self.s.update({"job_cap": 4})
        for bad in ({"job_cap": "abc"}, {"job_cap": -1}, {"job_cap": 0}, {"month_cap": float("inf")},
                    {"max_price": 9999}, {"vast_api_key": "short"}, {"vast_api_key": "has a space 123"},
                    {"jasna_license": 12345678}, "not a dict"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.s.update(bad)
        self.assertEqual(self.s.get("job_cap"), 4.0)

    def test_environment_only_overrides(self):
        self.s.update({"vast_api_key": KEY, "job_cap": 2})
        env = cloud_vast.Settings(self.path, env={"VAST_API_KEY": "env-key-0123456789", "VAST_JOB_CAP": "7"})
        self.assertEqual(env.get("vast_api_key"), "env-key-0123456789")
        self.assertEqual(env.get("job_cap"), 7.0)
        pub = env.public()
        self.assertEqual(pub["vast_api_key"]["source"], "env")
        self.assertEqual(sorted(pub["env_overrides"]), ["job_cap", "vast_api_key"])
        self.assertNotIn("env-key-0123456789", json.dumps(pub))

    def test_unreadable_file_is_ignored_not_fatal(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        self.assertEqual(self.s.config().job_cap, 3.00)


class ApiClientTest(unittest.TestCase):
    def setUp(self):
        self.clock = cf.FakeClock()
        self.vast = cf.FakeVast(self.clock)
        self.sleeps = []
        self.key = KEY
        self.api = cloud_vast.VastApi(lambda: self.key, http=self.vast, sleep=self.sleeps.append)

    def test_paths_end_in_slash_and_only_gets_follow_redirects(self):
        self.api.search_offers({})
        iid = self.api.create_instance(1, {"label": "stashify-x"})["new_contract"]
        self.api.show_instance(iid)
        self.api.list_instances()
        self.api.attach_ssh_key(iid, "ssh-ed25519 AAAA")
        self.api.destroy_instance(iid)
        self.api.balance()
        for method, path, _ in self.vast.calls:
            if not path.endswith("/users/current"):
                self.assertTrue(path.endswith("/"), path)
        for method, follow in self.vast.follow:
            self.assertEqual(follow, method == "GET", method)

    def test_bearer_header_is_sent(self):
        self.api.balance()
        self.assertEqual(self.vast.auth, ["Bearer " + KEY])

    def test_no_key_means_no_network(self):
        self.key = ""
        with self.assertRaises(cloud_vast.NotConfigured):
            self.api.balance()
        self.assertEqual(self.vast.calls, [])

    def test_429_waits_as_told_then_succeeds(self):
        self.vast.once.append(["GET", r"/users/current", Resp(429, {"error": "rate"}, {"Retry-After": "7"})])
        self.assertEqual(self.api.balance(), 12.34)
        self.assertEqual(self.sleeps, [7.0])

    def test_search_quota_is_final(self):
        self.vast.once.append(["POST", r"/bundles/", Resp(429, {"error": "quota", "msg": "daily search quota"})])
        with self.assertRaises(cloud_vast.VastQuotaError):
            self.api.search_offers({})
        self.assertEqual(self.vast.count("POST", "/bundles/"), 1)

    def test_rejected_key(self):
        self.vast.once.append(["GET", r"/users/current", Resp(401, {"error": "invalid_user_key"})])
        with self.assertRaises(cloud_vast.VastAuthError) as cm:
            self.api.balance()
        self.assertNotIn(KEY, str(cm.exception))

    def test_a_create_is_never_retried_but_reads_are(self):
        self.vast.create_modes = ["500"]
        with self.assertRaises(cloud_vast.VastApiError):
            self.api.create_instance(1, {"label": "stashify-x"})
        self.assertEqual(self.vast.count("PUT", "/asks/"), 1)
        self.vast.once += [["GET", r"/users/current", Resp(503, {"error": "busy"})],
                           ["GET", r"/users/current", Resp(503, {"error": "busy"})]]
        self.assertEqual(self.api.balance(), 12.34)
        self.assertEqual(self.sleeps, [2, 4])

    def test_a_create_is_not_retried_after_a_network_error_either(self):
        self.vast.once.append(["PUT", r"/asks/", ConnectionError("reset by peer")])
        with self.assertRaises(cloud_vast.VastApiError):
            self.api.create_instance(1, {"label": "stashify-x"})
        self.assertEqual(self.vast.count("PUT", "/asks/"), 1)

    def test_reads_survive_a_network_blip(self):
        self.vast.once.append(["GET", r"/users/current", ConnectionError("blip")])
        self.assertEqual(self.api.balance(), 12.34)

    def test_destroying_a_missing_instance_counts_as_gone(self):
        self.assertTrue(self.api.destroy_instance(424242))

    def test_instance_list_follows_every_page(self):
        for n in range(5):
            self.vast.add_instance("stashify-p%d" % n)
        self.vast.page_size = 2
        self.assertEqual(len(self.api.list_instances()), 5)
        self.assertEqual(self.vast.count("GET", r"^/api/v1/instances/$"), 3)

    def test_a_created_instance_key_is_registered_for_redaction(self):
        self.api.create_instance(1, {"label": "stashify-x"})
        self.assertEqual(cloud_vast.redact("key=" + INSTANCE_KEY), "key=***")

    def test_error_text_never_echoes_the_key(self):
        self.vast.once.append(["GET", r"/users/current", Resp(400, {"error": "bad", "msg": "token %s is bad" % KEY})])
        with self.assertRaises(cloud_vast.VastApiError) as cm:
            self.api.balance()
        self.assertNotIn(KEY, str(cm.exception))


class OfferTest(cf.CloudCase):
    def test_search_asks_for_the_right_machines(self):
        self.cl.search(self.spec(min_inet_down=200))
        body = self.vast.search_bodies[-1]
        self.assertEqual(body["gpu_ram"], {"gte": 16000})             # MB, not GB
        self.assertEqual(body["driver_version"], {"gte": 580000000})
        self.assertEqual(body["dph_total"], {"lte": 1.0})
        self.assertEqual(body["gpu_name"], {"in": ["RTX 4090", "RTX_4090", "RTX 3090", "RTX_3090"]})
        self.assertEqual((body["num_gpus"], body["rented"], body["verified"]),
                         ({"eq": 1}, {"eq": False}, {"eq": True}))
        self.assertEqual(body["inet_down"], {"gte": 200})
        self.assertNotIn("datacenter", body)
        self.assertEqual(self.vast.calls[-1][1], "/api/v0/bundles/")

    def test_datacenter_only_is_a_real_filter(self):
        self.cl.search(self.spec(datacenter_only=True))
        self.assertEqual(self.vast.search_bodies[-1]["datacenter"], {"eq": True})

    def test_offers_are_rechecked_and_ranked(self):
        bad_price = raw_offer(5, price=1.5)
        no_price = raw_offer(6)
        del no_price["dph_total"]
        self.vast.offers = [
            raw_offer(10, "RTX 3090", 0.20), raw_offer(11, "RTX 4090", 0.50),
            raw_offer(12, "RTX 4090", 0.30, driver="570.10"),          # driver too old
            raw_offer(13, "RTX 4090", 0.30, vram_mb=12000),            # not enough VRAM
            raw_offer(14, "RTX 4080", 0.30), raw_offer(15, "RTX 4090", 0.30, reliability=0.5),
            bad_price, no_price, raw_offer(16, "RTX 4090", 0.60, datacenter=True),
            raw_offer(17, "RTX_4090", 0.55)]                           # CLI-style spelling
        ids = [o.id for o in self.cl.search(self.spec())]
        self.assertEqual(ids, [16, 11, 17, 10])        # 4090 datacenter, 4090 cheapest first, then the 3090

    def test_a_missing_driver_field_does_not_disqualify(self):
        offer = raw_offer(20)
        del offer["driver_version"]
        self.vast.offers = [offer]
        self.assertEqual([o.id for o in self.cl.search(self.spec())], [20])

    def test_quote_search_is_cached_two_minutes(self):
        spec = self.spec()
        self.cl.best_offer(spec)
        self.cl.best_offer(spec)
        self.assertEqual(self.vast.count("POST", "/bundles/"), 1)
        self.clock.advance(cloud_vast.QUOTE_CACHE_S + 1)
        self.cl.best_offer(spec)
        self.assertEqual(self.vast.count("POST", "/bundles/"), 2)

    def test_no_offer_means_nothing_is_created(self):
        self.vast.offers = []
        with self.assertRaises(cloud_vast.NoOffers):
            with self.cl.rent(self.spec()):
                self.fail("no machine, so the body must not run")
        self.assertEqual(self.vast.count("PUT", "/asks/"), 0)

    def test_offer_view_has_no_secrets(self):
        view = self.cl.offers(self.spec())
        self.assertTrue(view and "id" not in view[0])
        self.assertNotIn(KEY, json.dumps(view))


class RentTest(cf.CloudCase):
    def assertClean(self):
        self.assertEqual(self.vast.live(), [], "a rented box is still alive")

    def statuses(self):
        return [e["status"] for e in self.ledger()]

    # -- the paths that must all end in a destroy ----------------------------- #

    def test_happy_path_destroys_and_books_the_cost(self):
        meta = {"job_id": "j1", "scene_id": "5", "scene": "Title", "preset": "detail"}
        with self.cl.rent(self.spec(), meta=meta) as box:
            self.assertEqual(self.vast.live(), [box.id])
            self.assertEqual((box.gpu, box.dph), ("RTX 4090", 0.40))
            self.assertIn("/root/in", self.tp.commands()[0])             # bootstrap ran
            self.clock.advance(1800)
            box.run("echo hi")
        self.assertClean()
        (row,) = self.ledger()
        self.assertEqual((row["status"], row["job_id"], row["scene"], row["preset"], row["gpu"]),
                         ("ok", "j1", "Title", "detail", "RTX 4090"))
        self.assertAlmostEqual(row["usd"], 0.20, places=4)               # half an hour at $0.40
        self.assertEqual(self.cl.registry.all(), {})
        self.assertIn(box.id, self.tp.cleaned)
        self.assertEqual(self.cl.month_to_date(), row["usd"])

    def test_create_payload_is_what_vast_expects(self):
        with self.cl.rent(self.spec(image="ubuntu:24.04", env={"NVIDIA_DRIVER_CAPABILITIES": "all"}, disk_gb=120)):
            pass
        body = next(b for m, p, b in self.vast.calls if m == "PUT")
        self.assertEqual((body["image"], body["disk"], body["runtype"], body["client_id"]),
                         ("ubuntu:24.04", 120, "ssh_direct", "me"))
        self.assertEqual(body["env"], {"NVIDIA_DRIVER_CAPABILITIES": "all"})
        self.assertTrue(body["label"].startswith("stashify-"))
        self.assertIn("AAAATESTKEYONLY", body["onstart"])
        self.assertLessEqual(len(body["onstart"]), 4048)                 # Vast's onstart limit

    def test_unreachable_box_is_destroyed_and_the_next_offer_is_used(self):
        self.tp.probe_script = {7001: ["down"]}
        with self.cl.rent(self.spec()) as box:
            second = box.id
            self.assertGreater(box.spent_total(), 0.06)                  # the dead box still cost money
        self.assertEqual(second, 7002)
        self.assertEqual(self.vast.count("PUT", "/asks/"), 2)
        self.assertEqual(self.statuses(), ["failed-offer", "ok"])
        self.assertClean()

    def test_on_offer_is_called_before_each_machine_is_rented(self):
        seen = []
        self.tp.probe_script = {7001: ["down"]}

        def on_offer(n, offer):
            seen.append((n, offer.id, self.vast.count("PUT", "/asks/")))
        with self.cl.rent(self.spec(), on_offer=on_offer):
            pass
        self.assertEqual(seen, [(1, 1, 0), (2, 2, 1)])          # (attempt, offer id, machines rented so far)
        self.assertClean()

    def test_a_missing_ssh_key_is_attached_once(self):
        self.tp.probe_script = {7001: ["denied"] * 5 + ["ok"]}
        with self.cl.rent(self.spec()):
            pass
        self.assertEqual(self.vast.count("POST", "/ssh/"), 1)
        self.assertClean()

    def test_a_machine_that_dies_while_booting_is_replaced(self):
        self.vast.status_seq = {7001: ["loading", "exited"]}
        self.tp.probe_script = {7001: ["down"]}
        with self.cl.rent(self.spec()) as box:
            self.assertEqual(box.id, 7002)
        self.assertEqual(self.statuses(), ["failed-offer", "ok"])
        self.assertClean()

    def test_verify_failure_tries_the_next_offer(self):
        seen = []

        def verify(box):
            seen.append(box.id)
            if len(seen) == 1:
                raise VerifyFailed("no encoder")
        with self.cl.rent(self.spec(), verify=verify):
            pass
        self.assertEqual(seen, [7001, 7002])
        self.assertEqual(self.statuses(), ["failed-offer", "ok"])
        self.assertClean()

    def test_builtin_verify_rejects_an_old_driver(self):
        self.tp.handlers.insert(0, (re.compile("nvidia-smi"), lambda t, c, k: cloud_ssh.RunResult(
            0, ["NVIDIA GeForce RTX 4090, 24564 MiB, 535.10"]) if t == 7001 else cloud_ssh.RunResult(
            0, ["NVIDIA GeForce RTX 4090, 24564 MiB, 580.95"])))
        with self.cl.rent(self.spec()) as box:
            self.assertEqual(box.id, 7002)
        self.assertClean()

    def test_never_more_than_three_offers(self):
        def verify(box):
            raise VerifyFailed("nope")
        with self.assertRaises(BoxFailed):
            with self.cl.rent(self.spec(), verify=verify):
                self.fail("must not run")
        self.assertEqual(self.vast.count("PUT", "/asks/"), 3)
        self.assertEqual(self.statuses(), ["failed-offer"] * 3)
        self.assertClean()

    def test_a_taken_offer_just_moves_on(self):
        self.vast.create_modes = ["gone"]
        with self.cl.rent(self.spec()) as box:
            self.assertEqual(box.offer.id, 2)
        self.assertClean()

    def test_cancel_during_the_job(self):
        state = {"stop": False}
        with self.assertRaises(RentalCancelled):
            with self.cl.rent(self.spec(), cancel=lambda: state["stop"]) as box:
                state["stop"] = True
                box.run("work")
        self.assertClean()
        self.assertEqual(self.statuses(), ["cancelled"])

    def test_a_cancel_that_raises_its_own_error_still_destroys(self):
        state = {"stop": False}

        def cancel():
            if state["stop"]:
                raise Cancelled("cancelled by user")
        with self.assertRaises(Cancelled):
            with self.cl.rent(self.spec(), cancel=cancel) as box:
                state["stop"] = True
                box.run("work")
        self.assertClean()
        self.assertEqual(self.statuses(), ["cancelled"])

    def test_cancel_while_the_machine_is_still_booting(self):
        with self.assertRaises(RentalCancelled):
            with self.cl.rent(self.spec(), cancel=lambda: bool(self.vast.instances)):
                self.fail("never reached")
        self.assertClean()
        self.assertEqual(self.statuses(), ["cancelled"])

    def test_cancel_from_the_transport_layer(self):
        self.tp.handlers.append((re.compile("JOB"), lambda t, c, k: (_ for _ in ()).throw(cloud_ssh.Cancelled("x"))))
        with self.assertRaises(RentalCancelled):
            with self.cl.rent(self.spec()) as box:
                box.run("JOB")
        self.assertClean()

    def test_an_error_in_the_body_still_destroys(self):
        with self.assertRaises(RuntimeError):
            with self.cl.rent(self.spec()):
                raise RuntimeError("kaboom")
        self.assertClean()
        (row,) = self.ledger()
        self.assertEqual(row["status"], "error")
        self.assertIn("kaboom", row["reason"])

    def test_ctrl_c_still_destroys(self):
        with self.assertRaises(KeyboardInterrupt):
            with self.cl.rent(self.spec()):
                raise KeyboardInterrupt()
        self.assertClean()

    def test_a_caller_can_set_the_ledger_outcome(self):
        with self.cl.rent(self.spec()) as box:
            box.outcome = ("error", "jasna exited 1")
        (row,) = self.ledger()
        self.assertEqual((row["status"], row["reason"]), ("error", "jasna exited 1"))

    # -- creates that go wrong -------------------------------------------------- #

    def test_a_create_that_errors_but_rented_is_swept_before_the_next_offer(self):
        for mode in ("500-but-rented", "false-but-rented"):
            self.vast.create_modes = [mode]
            with self.cl.rent(self.spec()):
                pass
            self.assertClean()
        # two rentals, each: one swept instance (error) + one real one (ok); never a retry of the create
        self.assertEqual(self.statuses().count("error"), 2)
        self.assertEqual(self.vast.count("PUT", "/asks/"), 4)

    def test_if_the_sweep_cannot_confirm_nothing_else_is_rented(self):
        self.vast.create_modes = ["500-but-rented"]
        self.vast.destroy_fail = float("inf")
        with self.assertRaises(cloud_vast.CloudError):
            with self.cl.rent(self.spec()):
                self.fail("must not run")
        self.assertEqual(self.vast.count("PUT", "/asks/"), 1)            # no second machine on top
        self.assertEqual(len(self.vast.live()), 1)                       # still there, so ...
        self.assertEqual(len(self.cl.registry.all()), 1)                 # ... the registry keeps it
        self.vast.destroy_fail = 0
        result = self.cl.reap_once()                                     # ... and the reaper finishes the job
        self.assertEqual(len(result["destroyed"]), 1)
        self.assertClean()

    def test_a_rejected_key_stops_everything_and_leaks_nothing(self):
        self.vast.create_modes = ["auth"]
        with self.assertRaises(cloud_vast.VastAuthError) as cm:
            with self.cl.rent(self.spec()):
                self.fail("must not run")
        self.assertNotIn(KEY, str(cm.exception))
        self.assertEqual(self.vast.count("PUT", "/asks/"), 1)

    def test_price_drift_after_create_is_refused(self):
        self.vast.price_override = {7001: 0.90}                          # offer said 0.40, box says 0.90
        with self.cl.rent(self.spec(max_price=0.50)) as box:
            self.assertEqual(box.id, 7002)
        self.assertEqual(self.statuses(), ["failed-offer", "ok"])
        self.assertClean()

    # -- failed destroys -------------------------------------------------------- #

    def test_a_failed_destroy_keeps_the_record_and_the_reaper_finishes_it(self):
        with self.cl.rent(self.spec(), meta={"job_id": "j9"}) as box:
            self.vast.destroy_fail = float("inf")
        self.assertEqual(self.vast.live(), [box.id])                     # could not destroy ...
        self.assertEqual(self.ledger(), [])                              # ... so nothing is booked yet
        rec = self.cl.registry.all()
        (label,) = rec
        self.assertEqual(rec[label]["inst"]["id"], box.id)               # ... and the registry remembers
        self.vast.destroy_fail = 0
        result = self.cl.reap_once()
        self.assertEqual(result["destroyed"], [box.id])
        self.assertClean()
        (row,) = self.ledger()
        self.assertEqual((row["status"], row["job_id"]), ("orphan", "j9"))

    def test_destroy_is_idempotent_and_books_once(self):
        with self.cl.rent(self.spec()) as box:
            self.assertTrue(box.destroy("ok"))
            self.assertTrue(box.destroy("ok"))
        self.assertEqual(len(self.ledger()), 1)
        self.assertEqual(self.vast.destroyed, [box.id])

    # -- money: caps and lifetime ------------------------------------------------ #

    def _long_job(self, hours):
        self.tp.handlers.append((re.compile("LONGJOB"), lambda t, c, k: (
            self.tp.simulate(hours * 3600, k["cancel"]), cloud_ssh.RunResult(0, []))[1]))

    def test_the_runtime_per_job_cap_stops_and_destroys(self):
        self.settings.update({"job_cap": 1.00})
        self._long_job(4)
        with self.assertRaises(SpendCapReached):
            with self.cl.rent(self.spec()) as box:
                self.assertEqual(box.cap, 1.00)
                box.run("LONGJOB")
        self.assertClean()
        (row,) = self.ledger()
        self.assertEqual(row["status"], "cap")
        self.assertTrue(0.95 < row["usd"] < 1.00, row["usd"])            # stopped just under the cap

    def test_the_cap_covers_every_attempt_not_just_the_last(self):
        self.settings.update({"job_cap": 0.50})
        self.tp.probe_script = {7001: ["down"]}                          # ~$0.07 burnt on the first box
        self._long_job(4)
        with self.assertRaises(SpendCapReached):
            with self.cl.rent(self.spec()) as box:
                box.run("LONGJOB")
        self.assertTrue(sum(e["usd"] for e in self.ledger()) < 0.50)
        self.assertClean()

    def test_the_monthly_cap_is_a_hard_cap_too(self):
        self.settings.update({"month_cap": 2.00})
        self.cl.ledger.append({"ended": cloud_vast._iso(self.clock.time()), "usd": 1.50, "status": "ok"})
        self._long_job(5)
        with self.assertRaises(SpendCapReached):
            with self.cl.rent(self.spec()) as box:
                self.assertAlmostEqual(box.cap, 0.50)                    # what is left of the month
                box.run("LONGJOB")
        self.assertLessEqual(self.cl.month_to_date(), 2.00)
        self.assertClean()

    def test_an_exhausted_month_rents_nothing(self):
        self.settings.update({"month_cap": 2.00})
        self.cl.ledger.append({"ended": cloud_vast._iso(self.clock.time()), "usd": 2.00, "status": "ok"})
        with self.assertRaises(SpendCapReached):
            with self.cl.rent(self.spec()):
                self.fail("must not run")
        self.assertEqual(self.vast.calls, [])                            # not even a search

    def test_last_months_spend_does_not_count(self):
        self.settings.update({"month_cap": 2.00})
        self.cl.ledger.append({"ended": "2026-10-30T10:00:00Z", "usd": 99.0, "status": "ok"})
        with self.cl.rent(self.spec()) as box:
            self.assertEqual(box.cap, 2.00)

    def test_a_running_rental_counts_against_the_month(self):
        with self.cl.rent(self.spec()):
            self.clock.advance(3600)
            self.assertAlmostEqual(self.cl.month_spend_committed(), 0.40, places=3)

    def test_lifetime_limit(self):
        self._long_job(3)
        with self.assertRaises(LifetimeExceeded):
            with self.cl.rent(self.spec(max_hours=1)) as box:
                box.run("LONGJOB")
        self.assertClean()
        self.assertEqual(self.statuses(), ["lifetime"])

    def test_at_most_two_rentals_at_once(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(self.cl.rent(self.spec()))
            stack.enter_context(self.cl.rent(self.spec()))
            with self.assertRaises(TooManyRentals):
                with self.cl.rent(self.spec()):
                    self.fail("third rental")
            self.assertEqual(self.vast.count("PUT", "/asks/"), 2)
        self.assertClean()

    def test_on_tick_reports_cost_while_running(self):
        ticks = []
        with self.cl.rent(self.spec(), on_tick=lambda b: ticks.append(round(b.spent_total(), 2))) as box:
            self.clock.advance(3600)
            box.check()
        self.assertEqual(ticks[-1], 0.40)

    def test_a_broken_status_hook_cannot_break_the_rental(self):
        def bad(_box):
            raise RuntimeError("hook")
        with self.cl.rent(self.spec(), on_tick=bad) as box:
            self.clock.advance(10)
            box.check()
        self.assertClean()

    # -- secrecy ------------------------------------------------------------------ #

    def test_secrets_never_reach_logs_files_or_the_status_view(self):
        self.vast.once.append(["GET", r"/api/v1/instances/", Resp(400, {"error": "bad", "msg": "key %s rejected" % KEY})])
        with self.assertLogs("stashify.cloud", level="DEBUG") as logs:
            result = self.cl.reap_once()
            cloud_vast.log.warning("raw secret in a log call: %s and %s", KEY, LICENCE)
        self.assertNotIn(KEY, json.dumps(result))
        self.assertNotIn(KEY, "\n".join(logs.output))
        self.assertNotIn(LICENCE, "\n".join(logs.output))
        with self.cl.rent(self.spec(), meta={"job_id": "j", "scene": "s"}) as box:
            reg = self.read("cloud-instances.json")
            status = json.dumps(self.cl.status())
            for blob in (reg, status):
                for secret in (KEY, LICENCE, INSTANCE_KEY):
                    self.assertNotIn(secret, blob)
        ledger = self.read("cloud-ledger.jsonl")
        for secret in (KEY, LICENCE, INSTANCE_KEY):
            self.assertNotIn(secret, ledger)
        self.assertEqual(cloud_vast.redact("Authorization: Bearer abcdefghijklmnop1234"), "Authorization: Bearer ***")

    def test_an_exception_message_is_redacted_in_the_ledger(self):
        with self.assertRaises(RuntimeError):
            with self.cl.rent(self.spec()):
                raise RuntimeError("login failed for %s" % KEY)
        self.assertNotIn(KEY, json.dumps(self.ledger()))


class ReaperTest(cf.CloudCase):
    def record(self, label, iid, age_s=0.0, max_hours=12.0):
        """Pretend an earlier process rented iid under label (and crashed)."""
        now = self.clock.time()
        self.cl.registry.begin(label, now - age_s, max_hours, {"job_id": "old"})
        self.cl.registry.set_inst(label, {"id": iid, "created_at": now - age_s, "gpu": "RTX 4090",
                                          "dph": 0.40, "offer_id": 1})

    def test_a_recorded_orphan_is_destroyed_and_booked(self):
        iid = self.vast.add_instance("stashify-aaaa1111", start_age_s=1800)
        self.record("stashify-aaaa1111", iid, age_s=1800)
        result = self.cl.reap_once()
        self.assertEqual(result["destroyed"], [iid])
        self.assertEqual(self.vast.live(), [])
        (row,) = self.ledger()
        self.assertEqual((row["status"], row["job_id"]), ("orphan", "old"))
        self.assertAlmostEqual(row["usd"], 0.20, places=3)
        self.clock.advance(700)                                           # next sweeps tidy the record
        self.cl.reap_once()
        self.assertEqual(self.cl.registry.all(), {})

    def test_a_label_only_orphan_is_destroyed(self):
        iid = self.vast.add_instance("stashify-bbbb2222", start_age_s=3600)
        self.assertEqual(self.cl.reap_once()["destroyed"], [iid])
        (row,) = self.ledger()
        self.assertEqual(row["status"], "orphan")
        self.assertAlmostEqual(row["usd"], 0.40, places=3)

    def test_other_peoples_instances_are_never_touched(self):
        a = self.vast.add_instance("my-own-thing")
        b = self.vast.add_instance("")
        result = self.cl.reap_once()
        self.assertEqual(result["destroyed"], [])
        self.assertEqual(self.vast.live(), [a, b])

    def test_an_active_rental_is_left_alone(self):
        with self.cl.rent(self.spec()) as box:
            self.assertEqual(self.cl.reap_once()["destroyed"], [])
            self.assertEqual(self.vast.live(), [box.id])

    def test_an_over_lifetime_box_dies_even_though_it_looks_active(self):
        with self.assertRaises(LifetimeExceeded):
            with self.cl.rent(self.spec(max_hours=1)) as box:
                self.clock.advance(3700)
                self.assertEqual(self.cl.reap_once()["destroyed"], [box.id])
                box.run("anything")
        self.assertEqual(self.vast.live(), [])
        self.assertEqual(len(self.ledger()), 1)                           # booked once, not twice
        self.assertEqual(self.ledger()[0]["status"], "orphan")

    def test_an_unavailable_list_skips_the_sweep_and_keeps_the_registry(self):
        iid = self.vast.add_instance("stashify-cccc3333")
        self.record("stashify-cccc3333", iid)
        self.vast.list_fail = True
        result = self.cl.reap_once()
        self.assertIn("error", result)
        self.assertEqual(self.vast.live(), [iid])
        self.assertEqual(len(self.cl.registry.all()), 1)

    def test_a_rental_whose_instance_is_gone_is_forgotten(self):
        self.record("stashify-dddd4444", 99999)
        self.cl.reap_once()
        self.assertEqual(self.cl.registry.all(), {})

    def test_no_key_no_sweep(self):
        self.settings.update({"vast_api_key": None})
        self.assertEqual(self.cl.reap_once(), {"skipped": "no api key"})
        self.assertEqual(self.vast.calls, [])

    def test_a_failed_destroy_is_reported_and_retried_next_time(self):
        iid = self.vast.add_instance("stashify-eeee5555")
        self.vast.destroy_fail = float("inf")
        result = self.cl.reap_once()
        self.assertEqual((result["destroyed"], result["failed"]), ([], [iid]))
        self.vast.destroy_fail = 0
        self.assertEqual(self.cl.reap_once()["destroyed"], [iid])

    def test_the_reaper_thread_survives_a_crash(self):
        calls = []

        def boom():
            calls.append(1)
            raise RuntimeError("bug")
        self.cl.reap_once = boom
        slept = []

        def sleep(_s):
            slept.append(1)
            if len(slept) >= 3:
                raise SystemExit             # end the daemon loop for the test
        self.clock.sleep = sleep
        t = self.cl.start_reaper(every=1)
        t.join(5)
        self.assertGreaterEqual(len(calls), 3)


class StatusTest(cf.CloudCase):
    def test_status_summary(self):
        for n in range(12):
            self.cl.ledger.append({"ended": cloud_vast._iso(self.clock.time() + n), "usd": 0.10, "status": "ok", "scene": "s%d" % n})
        with self.cl.rent(self.spec()) as box:
            self.clock.advance(1800)
            st = self.cl.status()
            self.assertEqual(st["active"][0]["gpu"], "RTX 4090")
            self.assertAlmostEqual(st["active"][0]["spent"], 0.20, places=3)
        before = len(self.vast.calls)
        st = self.cl.status()
        self.assertEqual(len(self.vast.calls), before)                   # status() never touches the network
        self.assertEqual(len(st["recent"]), 10)
        self.assertEqual(st["recent"][0]["status"], "ok")
        self.assertAlmostEqual(st["month_to_date"], 1.2 + 0.2, places=3)
        self.assertEqual((st["configured"], st["job_cap"], st["month_cap"]), (True, 3.0, 10.0))
        self.assertNotIn(KEY, json.dumps(st))


class BookkeepingTest(cf.CloudCase):
    def test_ledger_ignores_garbage_lines_and_keeps_order(self):
        self.cl.ledger.append({"ended": cloud_vast._iso(self.clock.time()), "usd": 1.0, "n": 1})
        with open(self.cl.ledger.path, "a") as fh:
            fh.write("{broken\n\n[1,2]\n")
        self.cl.ledger.append({"ended": cloud_vast._iso(self.clock.time()), "usd": 2.0, "n": 2})
        self.assertEqual([e["n"] for e in self.cl.ledger.entries()], [1, 2])
        self.assertEqual([e["n"] for e in self.cl.ledger.last(10)], [2, 1])
        self.assertEqual(self.cl.ledger.month_to_date(self.clock.time()), 3.0)
        self.assertEqual(stat.S_IMODE(os.stat(self.cl.ledger.path).st_mode), 0o600)

    def test_a_corrupt_registry_is_set_aside_not_fatal(self):
        with open(self.cl.registry.path, "w") as fh:
            fh.write("garbage")
        self.assertEqual(self.cl.registry.all(), {})
        self.assertTrue([f for f in os.listdir(self.dir) if f.startswith("cloud-instances.json.corrupt-")])

    def test_one_instance_is_booked_once_whoever_gets_there_first(self):
        self.cl.registry.begin("stashify-ffff6666", self.clock.time(), 12, {})
        self.cl.registry.set_inst("stashify-ffff6666", {"id": 5, "created_at": self.clock.time(), "gpu": "g", "dph": 1.0, "offer_id": 1})
        self.clock.advance(3600)
        first = self.cl._record("stashify-ffff6666", 5, "ok", "")
        second = self.cl._record("stashify-ffff6666", 5, "orphan", "reaper")
        self.assertEqual((first, second), (1.0, 0.0))
        self.assertEqual(len(self.ledger()), 1)

    def test_registry_file_is_private(self):
        self.cl.registry.begin("stashify-x", self.clock.time(), 12, {})
        self.assertEqual(stat.S_IMODE(os.stat(self.cl.registry.path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
