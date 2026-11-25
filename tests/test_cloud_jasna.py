"""cloud_jasna: presets, quote and the server-side refusals, the Jasna-in-the-cloud
lifecycle, progress parsing, release lookup. Fake Vast + scripted fake machine: nothing
is rented and no real time passes."""

import base64
import dataclasses
import json
import math
import os
import re
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cloudfakes as cf              # noqa: E402
from cloudfakes import KEY, LICENCE, UPSCALE, Resp, market, raw_offer   # noqa: E402
import hireling                       # noqa: E402
from hireling import ssh             # noqa: E402
from hireling.providers.vast import parse_offer   # noqa: E402
import cloud_jasna as cj             # noqa: E402
import core                          # noqa: E402

SCENE = {"id": "5", "title": "My Scene [1080p]", "duration": 600.0, "size": 50_000_000,
         "height": 1080, "width": 1920}
BASE = "https://github.com/Kruk2/jasna/releases/download/v0.12.4/"
PARTS = ["jasna-linux-nvidia-0.12.4.tar.zst.part00%d" % i for i in range(3)]
RELEASE = {"tag": "v0.12.4", "gb": 4.6,
           "assets": [{"name": n, "url": BASE + n, "size": 1_500_000_000} for n in PARTS]}


class Cancelled(Exception):
    """Same name as core.Cancelled: the ledger recognises it by name."""


def script_of(cmd):
    """The bash script inside a run_script command (it travels base64-encoded)."""
    m = re.search(r"echo ([A-Za-z0-9+/=]+) \| base64 -d", cmd)
    return base64.b64decode(m.group(1)).decode() if m else cmd


class World:
    """What the rented machine does, scripted per test and installed into the transport."""

    def __init__(self, tp, clock):
        self.tp, self.clock = tp, clock
        self.encoder_ok, self.install_code = {}, {}
        self.activation = (0, ["installation verified"])
        self.activation_stdin = []
        self.run_code = 0
        self.run_seconds = 600
        self.remote_size = 16                       # what `stat` says; FakeTransport.get writes 16 bytes
        self.jasna_cmds, self.answers = [], []
        self.run_lines = [
            "LTX model files missing in /root/jasna-config/x: a.safetensors.\nDownload (15.1 GB)? [y/N]: ",
            "LTX download:   5%|▌         | 0.75G/15.1G [00:30<09:00, 26.0MB/s]",
            "LTX download:  50%|█████     | 7.5G/15.1G [05:00<05:00, 26.0MB/s]",
            "LTX download: 100%|██████████| 15.1G/15.1G [10:00<00:00, 26.0MB/s]",
            "Processing video:  10%|█         | Processed: 01:00 (600f) | Remaining: 09:00 | Speed: 10.0fps",
            "Processing video:  40%|████      | Processed: 04:00 (2,400f) | Remaining: 06:00 | Speed: 10.0fps",
            "Processing video:  30%|███       | Processed: 03:00 (1,800f) | Remaining: 07:00 | Speed: 10.0fps",
            "Processing video: 100%|██████████| Processed: 10:00 (6,000f) | Remaining: 00:00 | Speed: 10.0fps",
        ]
        tp.handlers.insert(0, (re.compile(r"."), self.handle))

    def handle(self, tag, cmd, kw):
        script = script_of(cmd)
        if "nvidia-smi" in script:
            return ssh.RunResult(0, ["NVIDIA GeForce RTX 4090, 24564 MiB, 580.95.05"])
        if "apt-get install" in script and "jasna-linux-nvidia" in script:
            code = self.install_code.get(tag, 0)
            return ssh.RunResult(code, ["[stashify] step: installed"] if code == 0 else ["E: boom"])
        if "hevc_nvenc" in script:
            mark = cj.OK_MARK if self.encoder_ok.get(tag, True) else cj.FAIL_MARK
            return ssh.RunResult(0, [mark])
        if "read -r JASNA_LICENSE_CODE" in script:
            self.activation_stdin.append(kw["stdin_data"])
            code, lines = self.activation
            return ssh.RunResult(code, lines)
        if "./jasna --input" in script:
            return self._run(script, kw)
        if "stat -c %s" in script:
            return ssh.RunResult(0, [str(self.remote_size)])
        return ssh.RunResult(0, [])

    def _run(self, script, kw):
        self.jasna_cmds.append(script)
        self.tty_run = kw["tty"]
        step = self.run_seconds / len(self.run_lines)
        for line in self.run_lines:
            self.tp.simulate(step, kw["cancel"])
            tail = line.rsplit("\n", 1)[-1]
            for rx, reply in kw["prompts"]:
                if rx.search(tail):
                    self.answers.append(reply)
                    break
            if kw["on_line"]:
                kw["on_line"](line)
        return ssh.RunResult(self.run_code, self.run_lines[-2:] if self.run_code == 0 else ["Error: boom"])


class JasnaCase(cf.CloudCase):
    def setUp(self):
        super().setUp()
        cj._release_cache.update(at=0.0, data=None)
        self.world = World(self.tp, self.clock)
        self.src = os.path.join(self.dir, "My Scene [1080p].mkv")
        with open(self.src, "wb") as fh:
            fh.write(b"x" * 1000)
        self.out = os.path.join(self.dir, "out")
        os.makedirs(self.out)
        self.status, self.logs, self.stamps = [], [], []

    def cfg(self, preset="detail", confirm=None, scene=None):
        scene = scene or SCENE
        est = cj.build_quote(self.cl, scene, preset, release_gb=4.6)["est_usd"] or 0.0
        return {"cloud": {"preset": preset, "scene": scene, "est_usd": est,
                          "confirm_usd": est if confirm is None else confirm, "job_id": "job1"}}

    def run_job(self, cfg=None, release_fn=None, **kw):
        return cj.run_decensor(cfg or self.cfg(), self.src, self.out, cl=self.cl, log_cb=self.logs.append,
                               status_cb=self.status.append, stamp=lambda **f: self.stamps.append(f),
                               release_fn=release_fn or (lambda: RELEASE), **kw)

    def scripts(self):
        return [script_of(c) for c in self.tp.commands()]

    def assertNothingRented(self):
        self.assertEqual(self.vast.count("PUT", "/asks/"), 0)
        self.assertEqual(self.tp.runs, [])
        self.assertEqual(self.vast.live(), [])

    def assertClean(self):
        self.assertEqual(self.vast.live(), [], "a rented box is still alive")


class PresetTest(unittest.TestCase):
    def test_exactly_the_three_allowed_presets(self):
        self.assertEqual(set(cj.PRESETS), {"close-ups", "detail", "detail-close-ups"})
        self.assertEqual({p["label"] for p in cj.PRESETS.values()},
                         {"Sharper close-ups", "High detail", "High detail + sharper close-ups"})
        self.assertEqual(cj.preset_args("close-ups"), ["--restoration-model-name", "ltx", "--ltx-large-canvas"])
        self.assertEqual(cj.preset_args("detail"), ["--restoration-model-name", "ltx-8step"])
        self.assertEqual(cj.preset_args("detail-close-ups"),
                         ["--restoration-model-name", "ltx-8step", "--ltx-large-canvas"])

    def test_never_fast_never_normal_never_plain_standard(self):
        for key in cj.PRESETS:
            args = cj.preset_args(key)
            self.assertNotIn("--ltx-fast", args)
            model = args[args.index("--restoration-model-name") + 1]
            self.assertIn(model, ("ltx", "ltx-8step"))              # no basicvsrpp / ltx-basic / ltx-4step
            if model == "ltx":
                self.assertIn("--ltx-large-canvas", args)           # plain LTX standard is not offered

    def test_gpu_rules_follow_upstream(self):
        self.assertTrue(all(g.startswith("RTX") for g in cj.GPUS))   # data-centre cards do not work
        self.assertEqual(cj.GPUS[:2], ("RTX 5090", "RTX 4090"))       # prefer 5090, then 4090
        self.assertGreaterEqual(cj.MIN_VRAM_GB, 16)
        self.assertGreaterEqual(cj.MIN_DRIVER, 580)
        spec = cj.rent_spec(hireling.settings.Config(), 5_000_000_000)
        self.assertEqual(spec.env, {"NVIDIA_DRIVER_CAPABILITIES": "all"})
        self.assertEqual((spec.min_vram_gb, spec.min_driver), (16, 580))
        self.assertGreaterEqual(spec.disk_gb, 80 + 15)               # upstream: >= 80 GB, plus the video x3


class EstimateTest(cf.CloudCase):
    def offer(self, gpu="RTX 4090", price=0.40, down=900.0):
        return parse_offer(raw_offer(1, gpu, price, inet_down=down))

    def test_known_scene_known_numbers(self):
        est = cj.estimate(SCENE, "detail", self.offer(), 4.6)
        self.assertAlmostEqual(est["processing_min"], 45.0, places=1)     # 10 min x 1.5 x 2.5 x 1.2
        self.assertAlmostEqual(est["total_min"], 66.1, delta=0.3)
        self.assertEqual(est["est_usd"], 0.45)                            # rounded UP to the cent

    def test_faster_card_costs_less_time(self):
        mins = [cj.estimate(SCENE, "detail", self.offer(g, 0.4), 4.6)["processing_min"]
                for g in ("RTX 5090", "RTX 4090", "RTX 5080", "RTX 3090")]
        self.assertEqual(mins, sorted(mins))

    def test_presets_order_by_cost(self):
        by = {k: cj.estimate(SCENE, k, self.offer(), 4.6)["processing_min"] for k in cj.PRESETS}
        self.assertGreater(by["detail-close-ups"], by["detail"])
        self.assertGreater(by["detail-close-ups"], by["close-ups"])

    def test_longer_and_taller_video_costs_more(self):
        base = cj.estimate(SCENE, "detail", self.offer(), 4.6)["est_usd"]
        self.assertGreater(cj.estimate(dict(SCENE, duration=1200.0), "detail", self.offer(), 4.6)["est_usd"], base)
        self.assertGreater(cj.estimate(dict(SCENE, height=2160), "detail", self.offer(), 4.6)["est_usd"], base)

    def test_dollars_scale_with_the_offer_price(self):
        long_scene = dict(SCENE, duration=1800.0)                      # big enough that cent rounding is noise
        cheap = cj.estimate(long_scene, "detail", self.offer(price=0.20), 4.6)["est_usd"]
        dear = cj.estimate(long_scene, "detail", self.offer(price=0.80), 4.6)["est_usd"]
        self.assertAlmostEqual(dear / cheap, 4.0, delta=0.1)


class QuoteTest(cf.CloudCase):
    """The same function the server uses to refuse, so these ARE the refusal tests."""

    def quote(self, scene=None, preset="detail", **kw):
        return cj.build_quote(self.cl, scene or SCENE, preset, release_gb=4.6, **kw)

    def test_a_good_quote(self):
        q = self.quote()
        self.assertTrue(q["ok"])
        self.assertIsNone(q["refusal"])
        self.assertTrue(q["estimate"])
        self.assertEqual((q["gpu"], q["price_per_hour"], q["est_usd"]), ("RTX 4090", 0.4, 0.45))
        self.assertEqual((q["job_cap"], q["month_cap"], q["month_to_date"], q["month_remaining"]),
                         (3.0, 10.0, 0.0, 10.0))
        self.assertEqual(q["label"], "High detail")
        for key in ("setup_min", "processing_min", "transfer_min", "total_min"):
            self.assertGreater(q[key], 0)
        self.assertNotIn(KEY, json.dumps(q))

    def test_refused_without_a_vast_key(self):
        self.settings.update({"vast_api_key": None})
        q = self.quote()
        self.assertEqual(q["refusal"]["code"], "no_key")
        self.assertFalse(q["ok"])
        self.assertEqual(self.vast.calls, [])                          # not even a search

    def test_refused_without_a_jasna_licence(self):
        self.settings.update({"jasna_license": None})
        self.assertEqual(self.quote()["refusal"]["code"], "no_licence")
        self.assertEqual(self.vast.calls, [])

    def test_refused_when_the_estimate_is_over_the_per_job_cap(self):
        q = self.quote(dict(SCENE, duration=3 * 3600.0))
        self.assertEqual(q["refusal"]["code"], "over_job_cap")
        self.assertGreater(q["est_usd"], 3.0)                          # the number is still shown
        self.assertIn("$3.00", q["refusal"]["message"])

    def test_the_cap_is_the_users_setting(self):
        self.settings.update({"job_cap": 0.30})
        self.assertEqual(self.quote()["refusal"]["code"], "over_job_cap")       # est $0.45 > $0.30
        self.settings.update({"job_cap": 0.50})
        self.assertIsNone(self.quote()["refusal"])

    def test_refused_when_month_to_date_plus_estimate_is_over_the_monthly_cap(self):
        self.cl.ledger.append({"ended": hireling.util.iso(self.clock.time()), "usd": 9.80, "status": "ok"})
        q = self.quote()
        self.assertEqual(q["refusal"]["code"], "over_month_cap")
        self.assertEqual(q["month_to_date"], 9.8)
        self.assertEqual(q["month_remaining"], 0.2)

    def test_queued_jobs_count_against_the_month_too(self):
        self.assertIsNone(self.quote(queued_usd=5.0)["refusal"])
        self.assertEqual(self.quote(queued_usd=9.70)["refusal"]["code"], "over_month_cap")

    def test_a_running_rental_counts_against_the_month(self):
        self.settings.update({"month_cap": 0.80})
        with self.cl.rent(self.spec()):
            self.clock.advance(3600)                                    # $0.40 accrued, not yet in the ledger
            q = self.quote()
            self.assertEqual(q["month_to_date"], 0.4)
            self.assertEqual(q["refusal"]["code"], "over_month_cap")    # 0.40 running + 0.45 estimate > 0.80

    def test_last_months_ledger_does_not_block(self):
        self.cl.ledger.append({"ended": "2026-10-31T23:00:00Z", "usd": 50.0, "status": "ok"})
        self.assertIsNone(self.quote()["refusal"])

    def test_no_machine_available(self):
        self.vast.offers = []
        self.assertEqual(self.quote()["refusal"]["code"], "no_offer")
        self.assertEqual(self.vast.count("PUT", "/asks/"), 0)

    def test_search_quota_and_api_errors_are_reported_not_raised(self):
        self.vast.once.append(["POST", r"/bundles/", Resp(429, {"error": "quota", "msg": "daily search quota"})])
        self.assertEqual(self.quote()["refusal"]["code"], "search_quota")
        self.cl._quote_cache.clear()
        self.vast.once.append(["POST", r"/bundles/", Resp(400, {"error": "bad", "msg": "key %s is odd" % KEY})])
        q = self.quote()
        self.assertEqual(q["refusal"]["code"], "offers_unavailable")
        self.assertNotIn(KEY, q["refusal"]["message"])

    def test_unknown_length_cannot_be_estimated(self):
        self.assertEqual(self.quote(dict(SCENE, duration=0))["refusal"]["code"], "no_duration")

    def test_unknown_preset_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            self.quote(preset="normal")
        with self.assertRaises(ValueError):
            self.quote(preset="ltx-fast")

    def test_price_change_tolerance(self):
        self.assertFalse(cj.price_changed(1.00, 1.05))
        self.assertFalse(cj.price_changed(0.45, 0.59))
        self.assertTrue(cj.price_changed(1.00, 1.25))
        self.assertTrue(cj.price_changed(0.45, 0.70))


class LifecycleTest(JasnaCase):
    def test_happy_path(self):
        produced = self.run_job()
        self.assertEqual(os.path.dirname(produced), self.out)
        self.assertTrue(produced.endswith("_decensored.mp4"))
        with open(produced, "rb") as fh:
            self.assertEqual(fh.read(), b"decensored-bytes")
        self.assertClean()
        (row,) = self.ledger()
        self.assertEqual((row["status"], row["job_id"], row["preset"], row["scene_id"], row["scene"]),
                         ("ok", "job1", "detail", "5", "My Scene [1080p]"))
        self.assertGreater(row["usd"], 0)
        self.assertEqual(self.cl.registry.all(), {})
        # order of work: install -> encoder check -> activate -> upload -> run -> result
        cmds = self.scripts()
        order = [next(i for i, c in enumerate(cmds) if needle in c)
                 for needle in ("jasna-linux-nvidia-*.part*", "hevc_nvenc", "read -r JASNA_LICENSE_CODE",
                                "./jasna --input", "stat -c %s")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(len(self.tp.puts), 1)
        self.assertEqual(len(self.tp.gets), 1)

    def test_install_follows_upstream_recipe(self):
        self.run_job()
        install = next(c for c in self.scripts() if "jasna-linux-nvidia-*.part*" in c)
        for part in PARTS:
            self.assertIn(BASE + part, install)
        for needle in ("set -euo pipefail", "--no-install-recommends", "ca-certificates", "zstd", "libgl1",
                       "cd /root", "zstd -dc | tar -xpf - --same-owner", "wget -nv -c"):
            self.assertIn(needle, install)
        check = next(c for c in self.scripts() if "hevc_nvenc" in c)
        self.assertIn("./tools/ffmpeg", check)

    def test_remote_names_do_not_leak_the_title(self):
        produced = self.run_job()
        self.assertEqual(self.tp.puts[0][1:], (self.src, "/root/in/source.mkv"))
        self.assertEqual(self.tp.gets[0][1], "/root/out/result.mp4")
        for cmd in self.scripts():
            self.assertNotIn("My Scene", cmd)
        self.assertEqual(os.path.basename(produced), "My_Scene_1080p__decensored.mp4")

    def test_each_preset_runs_exactly_its_flags(self):
        for key in cj.PRESETS:
            self.world.jasna_cmds.clear()
            self.world.run_seconds = 60
            self.run_job(self.cfg(key))
            (cmd,) = self.world.jasna_cmds
            expect = " ".join(cj.preset_args(key))
            self.assertIn(expect, cmd)
            self.assertNotIn("--ltx-fast", cmd)
            model, large = {"close-ups": ("ltx", True), "detail": ("ltx-8step", False),
                            "detail-close-ups": ("ltx-8step", True)}[key]
            self.assertIn("--restoration-model-name %s " % model, cmd)
            self.assertEqual("--ltx-large-canvas" in cmd, large)
            for needle in ("--input /root/in/source.mkv", "--output /root/out/result.mp4",
                           "XDG_CONFIG_HOME=/root/jasna-config", "--log-level info"):
                self.assertIn(needle, cmd)
            for other in ("basicvsrpp", "ltx-basic", "ltx-4step", "ltx-undistilled"):
                self.assertNotIn(other, cmd)

    def test_the_licence_travels_on_stdin_only(self):
        self.run_job()
        self.assertEqual(self.world.activation_stdin, [(LICENCE + "\n").encode()])
        for tag, cmd, kw in self.tp.runs:
            self.assertNotIn(LICENCE, cmd)
            self.assertNotIn(LICENCE, script_of(cmd))
            self.assertNotIn(LICENCE, json.dumps(kw["env"] or {}))
            if kw["stdin_data"] is not None:
                self.assertIn("read -r JASNA_LICENSE_CODE", script_of(cmd))
        for blob in (json.dumps(self.status), "\n".join(self.logs), json.dumps(self.stamps),
                     self.read("cloud-ledger.jsonl"), self.read("cloud-instances.json")):
            self.assertNotIn(LICENCE, blob)
            self.assertNotIn(KEY, blob)

    def test_activation_uses_upstreams_environment(self):
        self.run_job()
        act = next(c for c in self.scripts() if "read -r JASNA_LICENSE_CODE" in c)
        self.assertIn("export XDG_CONFIG_HOME=/root/jasna-config", act)
        self.assertIn("./jasna --online", act)
        self.assertIn("export JASNA_LICENSE_CODE", act)

    def test_the_download_prompt_is_answered_yes_and_the_run_has_a_terminal(self):
        self.run_job()
        self.assertEqual(self.world.answers, [b"y\n"])
        self.assertTrue(self.world.tty_run)
        run = next(kw for _t, c, kw in self.tp.runs if "./jasna --input" in script_of(c))
        self.assertGreater(run["grace"], 0)                    # Ctrl-C first, so Jasna frees its licence slot

    def test_prompt_regexes(self):
        yes, no = cj.PROMPTS
        self.assertTrue(yes[0].search("Download (15.1 GB)? [y/N]: "))
        self.assertEqual(yes[1], b"y\n")
        self.assertFalse(yes[0].search("Download it (3 MB) into /x? [y/N]: "))
        self.assertTrue(no[0].search("Download it (3 MB) into /x? [y/N]: "))
        self.assertEqual(no[1], b"n\n")

    def test_the_machine_ledger_and_live_stamps(self):
        self.run_job()
        gpus = {s.get("runner") for s in self.stamps}
        self.assertIn("Vast.ai · RTX 4090", gpus)
        last = [s for s in self.stamps if "cloud_usd" in s][-1]
        self.assertEqual((last["engine"], last["cloud"], last["cloud_cap"]), ("jasna", True, 3.0))
        self.assertGreater(last["cloud_usd"], 0)

    def test_progress_only_moves_forward_on_the_dashboard_bar(self):
        self.run_job()
        fracs = [core._map_runner_status(jb, 0.0, 0.97)[0] for jb in self.status]
        self.assertTrue(fracs)
        self.assertEqual(fracs, sorted(fracs), "the bar went backwards")
        self.assertLessEqual(max(fracs), 0.97)
        stages = [jb["stage"] for jb in self.status]
        self.assertEqual(stages[0], "copy")
        self.assertEqual(stages[-1], "decensor")
        self.assertIn("scan", stages)
        messages = " | ".join(str(jb["message"]) for jb in self.status)
        for needle in ("Installing Jasna", "Activating", "Uploading", "Downloading the LTX model", "Downloading the result"):
            self.assertIn(needle, messages)
        frames = [jb for jb in self.status if jb.get("frame")]
        self.assertTrue(frames and frames[-1].get("fps") == 10.0)

    # -- every way the job can end must end in a destroy -------------------------- #

    def test_encoder_check_failure_tries_the_next_offer(self):
        self.world.encoder_ok = {"vast-7001": False}
        self.run_job()
        self.assertEqual([e["status"] for e in self.ledger()], ["failed-offer", "ok"])
        self.assertEqual(len(self.world.activation_stdin), 1)             # only the good machine was activated
        self.assertEqual(sum("jasna-linux-nvidia-*.part*" in s for s in self.scripts()), 2)
        self.assertClean()

    def test_a_failed_install_also_tries_the_next_offer(self):
        self.world.install_code = {"vast-7001": 100}
        self.run_job()
        self.assertEqual([e["status"] for e in self.ledger()], ["failed-offer", "ok"])
        self.assertClean()

    def test_three_bad_machines_then_stop(self):
        self.world.encoder_ok = {"vast-7001": False, "vast-7002": False, "vast-7003": False, "vast-7004": False}
        with self.assertRaises(hireling.BoxFailed):
            self.run_job()
        self.assertEqual(self.vast.count("PUT", "/asks/"), 3)
        self.assertEqual(self.world.activation_stdin, [])                  # the licence never left home
        self.assertEqual(self.tp.puts, [])                                 # nor did the video
        self.assertClean()

    def test_activation_failure_is_final_not_retried_elsewhere(self):
        self.world.activation = (1, ["Error: this licence code is not valid"])
        with self.assertRaises(cj.ActivationFailed) as cm:
            self.run_job()
        self.assertIn("not valid", str(cm.exception))
        self.assertEqual(self.vast.count("PUT", "/asks/"), 1)
        self.assertEqual(self.tp.puts, [])
        self.assertClean()
        self.assertEqual(self.ledger()[0]["status"], "error")

    def test_three_devices_message_says_wait(self):
        self.world.activation = (1, ["Error: licence is in use on three devices"])
        with self.assertRaises(cj.ActivationFailed) as cm:
            self.run_job()
        self.assertIn("15 minutes", str(cm.exception))
        self.assertClean()

    def test_a_jasna_crash_downloads_nothing(self):
        self.world.run_code = 1
        with self.assertRaises(cj.CloudJobFailed) as cm:
            self.run_job()
        self.assertIn("exit 1", str(cm.exception))
        self.assertEqual(self.tp.gets, [])
        self.assertEqual(os.listdir(self.out), [])
        self.assertClean()
        self.assertEqual(self.ledger()[0]["reason"], "jasna exited 1")

    def test_the_runtime_cap_stops_the_job_and_keeps_nothing(self):
        self.settings.update({"job_cap": 1.00})
        self.world.run_seconds = 5 * 3600
        cfg = self.cfg()
        with self.assertRaises(cj.CloudJobFailed) as cm:
            self.run_job(cfg)
        self.assertIn("Stopped", str(cm.exception))
        self.assertIn("destroyed", str(cm.exception))
        self.assertEqual(self.tp.gets, [])
        self.assertEqual(os.listdir(self.out), [])
        self.assertClean()
        (row,) = self.ledger()
        self.assertEqual(row["status"], "cap")
        self.assertLess(row["usd"], 1.00)
        self.assertGreater(row["usd"], 0.9)

    def test_cancel_stops_the_job_and_destroys(self):
        def cancel():
            if self.tp.puts:
                raise Cancelled("cancelled by user")
        with self.assertRaises(Cancelled):
            self.run_job(check_cancel=cancel)
        self.assertEqual(self.tp.gets, [])
        self.assertClean()
        self.assertEqual([e["status"] for e in self.ledger()], ["cancelled"])

    def test_an_incomplete_download_is_rejected_and_removed(self):
        self.world.remote_size = 99
        with self.assertRaises(cj.CloudJobFailed):
            self.run_job()
        self.assertEqual(os.listdir(self.out), [])
        self.assertClean()

    def test_a_failed_download_leaves_no_partial_file(self):
        def partial(tag, remote, local, **kw):
            with open(local, "wb") as fh:
                fh.write(b"half")
            raise ssh.SshError("connection lost")
        self.tp.get = lambda ep, tag, remote, local, **kw: partial(tag, remote, local)
        with self.assertRaises(ssh.SshError):
            self.run_job()
        self.assertEqual(os.listdir(self.out), [])
        self.assertClean()

    # -- refusals at start: nothing is rented ------------------------------------- #

    def test_start_is_refused_without_a_key_or_licence(self):
        cfg = self.cfg()
        self.settings.update({"vast_api_key": None})
        with self.assertRaises(cj.CloudRefused) as cm:
            self.run_job(cfg)
        self.assertEqual(cm.exception.code, "no_key")
        self.assertNothingRented()
        self.settings.update({"vast_api_key": KEY, "jasna_license": None})
        with self.assertRaises(cj.CloudRefused) as cm:
            self.run_job(cfg)
        self.assertEqual(cm.exception.code, "no_licence")
        self.assertNothingRented()

    def test_start_is_refused_when_the_caps_changed_since_confirming(self):
        cfg = self.cfg()
        self.settings.update({"job_cap": 0.30})
        with self.assertRaises(cj.CloudRefused) as cm:
            self.run_job(cfg)
        self.assertEqual(cm.exception.code, "over_job_cap")
        self.settings.update({"job_cap": 3.00})
        self.cl.ledger.append({"ended": hireling.util.iso(self.clock.time()), "usd": 9.90, "status": "ok"})
        with self.assertRaises(cj.CloudRefused) as cm:
            self.run_job(cfg)
        self.assertEqual(cm.exception.code, "over_month_cap")
        self.assertNothingRented()

    def test_start_is_refused_when_the_price_moved_after_confirming(self):
        cfg = self.cfg(confirm=0.20)                       # the user confirmed $0.20; it is now ~$0.45
        with self.assertRaises(cj.CloudRefused) as cm:
            self.run_job(cfg)
        self.assertEqual(cm.exception.code, "price_changed")
        self.assertNothingRented()

    def test_a_release_lookup_failure_rents_nothing(self):
        def broken():
            raise cj.CloudJobFailed("could not look up the latest Jasna release on GitHub: offline")
        with self.assertRaises(cj.CloudJobFailed):
            self.run_job(release_fn=broken)
        self.assertNothingRented()

    def test_no_machine_at_start_is_a_refusal_and_costs_nothing(self):
        cfg = self.cfg()
        self.vast.offers = []
        self.cl._quote_cache.clear()                       # the dashboard's price lookup is cached for a while
        with self.assertRaises(cj.CloudRefused) as cm:
            self.run_job(cfg)
        self.assertEqual(cm.exception.code, "no_offer")
        self.assertNothingRented()
        self.assertEqual(self.ledger(), [])

    def test_a_machine_that_vanishes_after_the_quote_is_also_a_refusal(self):
        cfg = self.cfg()
        stale = self.cl.best_offer(cj.rent_spec(self.cl.config(), SCENE["size"]))    # what a price check saw ...
        self.vast.offers = []                              # ... and the market empties out before the rental
        with mock.patch.object(self.cl, "best_offer", return_value=stale):
            with self.assertRaises(cj.CloudRefused) as cm:
                self.run_job(cfg)
        self.assertEqual(cm.exception.code, "no_offer")
        self.assertIn("Nothing was rented", str(cm.exception))
        self.assertNothingRented()
        self.assertEqual(self.ledger(), [])

    def test_the_run_looks_the_market_up_again_instead_of_trusting_the_cached_quote(self):
        cfg = self.cfg()                                   # this lookup is now cached ...
        self.vast.offers = []                              # ... and the market empties out
        with self.assertRaises(cj.CloudRefused) as cm:
            self.run_job(cfg)
        self.assertEqual(cm.exception.code, "no_offer")
        self.assertNothingRented()
        self.assertEqual(self.ledger(), [])


class ProgressTest(unittest.TestCase):
    def test_processing_lines(self):
        p = cj.RunProgress()
        u = p.parse("Processing video:  42%|████▏     | Processed: 00:12 (1,234f) | Remaining: 01:23 | Speed: 12.3fps")
        self.assertAlmostEqual(u["progress"], 0.42)
        self.assertEqual((u["frame"], u["fps"], u["eta"]), (1234, 12.3, 83))
        self.assertEqual(u["total_frames"], 2938)
        self.assertFalse(u["indeterminate"])

    def test_hours_in_the_remaining_time(self):
        u = cj.RunProgress().parse("Processing video:  5%|▌ | Processed: 00:12 (100f) | Remaining: 1:02:03 | Speed: 3.0fps")
        self.assertEqual(u["eta"], 3723)

    def test_never_backwards_and_never_100_before_exit(self):
        p = cj.RunProgress()
        a = p.parse("Processing video:  50%|x| Processed: 00:12 (10f) | Remaining: 01:00 | Speed: 1.0fps")
        b = p.parse("Processing video:  30%|x| Processed: 00:13 (11f) | Remaining: 01:00 | Speed: 1.0fps")
        c = p.parse("Processing video: 100%|x| Processed: 00:14 (12f) | Remaining: 00:00 | Speed: 1.0fps")
        self.assertEqual((a["progress"], b["progress"], c["progress"]), (0.5, 0.5, 0.99))

    def test_model_download_lines(self):
        p = cj.RunProgress()
        u = p.parse("LTX download:  12%|█▏        | 1.8G/15.1G [01:10<08:30, 25.9MB/s]")
        self.assertEqual(u["message"], "Downloading the LTX model (12%)")
        self.assertEqual(u["log"], "LTX model download: 12%")
        again = p.parse("LTX download:  13%|█▏        | 1.9G/15.1G [01:12<08:30, 25.9MB/s]")
        self.assertNotIn("log", again)                                      # logged once per 10%
        self.assertIn("log", p.parse("LTX download:  20%|██        | 3G/15.1G [02:00<08:00, 25.9MB/s]"))

    def test_other_lines_are_not_progress(self):
        p = cj.RunProgress()
        for line in ("", "12:00:01 jasna.pipeline INFO Starting", "installation verified",
                     "Warning: 50% of something |", "Error: boom"):
            self.assertIsNone(p.parse(line), line)


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        cj._release_cache.update(at=0.0, data=None)

    def gh(self, parts=3, base=BASE, extra=()):
        assets = [{"name": "jasna-linux-nvidia-0.12.4.tar.zst.part%03d" % i,
                   "browser_download_url": base + "jasna-linux-nvidia-0.12.4.tar.zst.part%03d" % i,
                   "size": 1_000_000_000} for i in range(parts)]
        assets.append({"name": "jasna-windows-nvidia-0.12.4.zip", "browser_download_url": base + "jasna-windows-nvidia-0.12.4.zip", "size": 5})
        return {"tag_name": "v0.12.4", "assets": list(extra) + assets}

    def test_picks_the_linux_parts_in_order(self):
        data = self.gh()
        data["assets"].reverse()
        rel = cj.release_assets(http_get=lambda url: data, now=1000.0)
        self.assertEqual([a["name"] for a in rel["assets"]], PARTS)
        self.assertEqual(rel["tag"], "v0.12.4")
        self.assertAlmostEqual(rel["gb"], 3.0)

    def test_asks_github_for_the_latest_release_of_the_right_repo(self):
        seen = []
        cj.release_assets(http_get=lambda url: seen.append(url) or self.gh(), now=1.0)
        self.assertEqual(seen, ["https://api.github.com/repos/Kruk2/jasna/releases/latest"])

    def test_cached_for_an_hour(self):
        cj.release_assets(http_get=lambda url: self.gh(), now=1000.0)

        def boom(url):
            raise OSError("offline")
        self.assertEqual(len(cj.release_assets(http_get=boom, now=1500.0)["assets"]), 3)
        with self.assertRaises(cj.CloudJobFailed):
            cj.release_assets(http_get=boom, now=1000.0 + cj.RELEASE_CACHE_S + 1)

    def test_incomplete_parts_are_refused(self):
        data = self.gh()
        del data["assets"][1]
        with self.assertRaises(cj.CloudJobFailed) as cm:
            cj.release_assets(http_get=lambda url: data, now=1.0)
        self.assertIn("not complete", str(cm.exception))

    def test_downloads_from_other_hosts_are_refused(self):
        with self.assertRaises(cj.CloudJobFailed):
            cj.release_assets(http_get=lambda url: self.gh(base="https://evil.example/"), now=1.0)

    def test_no_linux_build_at_all(self):
        with self.assertRaises(cj.CloudJobFailed):
            cj.release_assets(http_get=lambda url: {"tag_name": "v1", "assets": []}, now=1.0)

    def test_github_errors_become_a_clear_failure(self):
        def boom(url):
            raise OSError("rate limit exceeded for token %s" % KEY)
        hireling.register_secret(KEY)
        with self.assertRaises(cj.CloudJobFailed) as cm:
            cj.release_assets(http_get=boom, now=1.0)
        self.assertIn("GitHub", str(cm.exception))
        self.assertNotIn(KEY, str(cm.exception))

    def test_scene_meta(self):
        m = cj.scene_meta({"id": "7", "title": None, "files": [{"path": "/media/a/b/clip.mkv", "width": 1920, "height": 1080,
                                                                "duration": 61.5, "size": 123}]})
        self.assertEqual((m["id"], m["title"], m["duration"], m["size"], m["height"]), ("7", "clip.mkv", 61.5, 123, 1080))
        self.assertEqual(cj.scene_meta({"id": "8", "title": "T", "files": []})["duration"], 0)


class PlanTest(unittest.TestCase):
    """What a request may ask for, checked before anything is searched or rented."""

    def code(self, body, backend=None):
        with self.assertRaises(cj.PlanError) as cm:
            cj.plan_from_body(body, backend)
        return cm.exception.code

    def test_the_default_is_the_plain_automatic_decensor(self):
        plan = cj.plan_from_body({})
        self.assertEqual((plan.op, plan.upscale, plan.upscale_height), ("decensor", "", 0))
        self.assertEqual((plan.offer_id, plan.provider, plan.gpu, plan.hardware, plan.filters), ("", "", "", "", {}))
        self.assertFalse(plan.chosen)
        self.assertFalse(plan.steered)
        self.assertEqual(cj.plan_from_body(None), plan)
        self.assertEqual(cj.plan_from_body({}, "lada"), plan)
        self.assertEqual(cj.plan_from_body({}, "decensor"), plan)

    def test_the_operation_comes_from_op_then_the_backend_then_post_upscale(self):
        self.assertEqual(cj.plan_from_body({"op": "upscale"}).op, "upscale")
        self.assertEqual(cj.plan_from_body({}, "upscale").op, "upscale")
        self.assertEqual(cj.plan_from_body({"post_upscale": True}).op, "decensor+upscale")
        self.assertEqual(cj.plan_from_body({"post_upscale": "true"}).op, "decensor+upscale")
        self.assertEqual(cj.plan_from_body({"post_upscale": "false"}).op, "decensor")
        self.assertEqual(cj.plan_from_body({"post_upscale": 0}).op, "decensor")
        self.assertEqual(cj.plan_from_body({"op": "decensor+upscale"}).op, "decensor+upscale")
        self.assertEqual(cj.plan_from_body({"op": "decensor upscale"}).op, "decensor+upscale")   # a query string's '+'
        self.assertEqual(cj.plan_from_body({"op": "decensor"}, "upscale").op, "decensor")        # op beats the backend

    def test_other_operations_are_refused(self):
        for body, backend in (({"op": "transcode"}, None), ({"op": "chain"}, None), ({"op": "UPSCALE"}, None),
                              ({}, "transcode"), ({}, "command"), ({}, "span")):
            self.assertEqual(self.code(body, backend), "bad_op", (body, backend))

    def test_the_upscale_settings(self):
        plan = cj.plan_from_body({"op": "upscale"})
        self.assertEqual((plan.upscale, plan.upscale_height), ("span", 2160))                  # the defaults
        plan = cj.plan_from_body({"op": "upscale", "upscale": "rtx-super-res", "upscale_height": "1440"})
        self.assertEqual((plan.upscale, plan.upscale_height), ("rtx-super-res", 1440))
        self.assertEqual(cj.plan_from_body(dict(UPSCALE, upscale="RealPLKSR")).upscale, "realplksr")
        for model in ("dlss5", "topaz", "none", "span; rm -rf /"):
            self.assertEqual(self.code(dict(UPSCALE, upscale=model)), "bad_upscale", model)
        for height in (720, "tall", True, 1081, -1, 4320):
            self.assertEqual(self.code(dict(UPSCALE, upscale_height=height)), "bad_upscale_height", height)
        plain = cj.plan_from_body({"upscale": "bogus", "upscale_height": 7})                  # not upscaling: not looked at
        self.assertEqual((plain.upscale, plain.upscale_height), ("", 0))

    def test_the_machine_fields(self):
        plan = cj.plan_from_body({"offer_id": 7001, "provider": "VAST", "gpu": "RTX 4090"})
        self.assertEqual((plan.offer_id, plan.provider, plan.gpu), ("7001", "vast", "RTX 4090"))
        self.assertTrue(plan.chosen and plan.steered)
        for bad in ("1; rm", "a b", "../x", "x" * 65, "-1"):
            self.assertEqual(self.code({"offer_id": bad}), "bad_offer", bad)
        self.assertEqual(self.code({"provider": "nope"}), "bad_provider")
        self.assertEqual(self.code({"gpu": "RTX;4090"}), "bad_gpu")
        self.assertEqual(self.code({"hardware": "nope"}), "bad_hardware")
        self.assertEqual(self.code({"hardware": "cheapest", "offer_id": "12"}), "bad_hardware")
        for hardware in hireling.PRESET_IDS:
            plan = cj.plan_from_body({"hardware": hardware})
            self.assertEqual(plan.hardware, hardware)
            self.assertTrue(plan.chosen)
        self.assertFalse(cj.plan_from_body({"gpu": "RTX 3090"}).chosen)        # a model is a preference, not a machine
        self.assertTrue(cj.plan_from_body({"gpu": "RTX 3090"}).steered)

    def test_filters_are_checked_when_they_arrive(self):
        plan = cj.plan_from_body({"cloud_filters": {"min_vram_gb": 24, "countries": ["DE"]}})
        self.assertEqual(plan.filters, {"min_vram_gb": 24, "countries": ["DE"]})
        self.assertTrue(plan.steered)
        self.assertEqual(cj.plan_from_body({"cloud_filters": '{"min_vram_gb": 24}'}).filters, {"min_vram_gb": 24})
        self.assertEqual(cj.plan_from_body({"cloud_filters": ""}).filters, {})
        self.assertEqual(cj.plan_from_body({"cloud_filters": None}).filters, {})
        for bad in ("{nope", "[1]", 7, ["min_vram_gb"], {"nope": 1}, {"min_vram_gb": "lots"}, {"disk_gb": 500},
                    {"max_price": -3}):
            self.assertEqual(self.code({"cloud_filters": bad}), "bad_filters", bad)
        with self.assertRaises(cj.PlanError) as cm:
            cj.plan_from_body({"cloud_filters": {"nope": 1}})
        self.assertIn("nope", str(cm.exception))

    def test_a_plan_makes_itself_again(self):
        plan = cj.plan_from_body(dict(UPSCALE, offer_id="12", provider="vast", gpu="RTX 4090",
                                      cloud_filters={"min_vram_gb": 24}))
        self.assertEqual(cj.plan_from_body(plan.as_dict()), plan)
        self.assertEqual(plan.as_dict()["cloud_filters"], {"min_vram_gb": 24})
        hardware = cj.plan_from_body({"hardware": "fastest"})
        self.assertEqual(cj.plan_from_body(hardware.as_dict()), hardware)
        self.assertEqual(cj.plan_from_body(cj.Plan().as_dict()), cj.Plan())

    def test_flags(self):
        for value in (True, 1, "1", "true", "TRUE", " yes ", "on"):
            self.assertTrue(cj.truthy(value), value)
        for value in (False, 0, None, "", "0", "false", "no", "off", "maybe", 2, [], {}):
            self.assertFalse(cj.truthy(value), value)


class NarrowTest(cf.CloudCase):
    """The owner's own search filters can only tighten what Jasna and the caps require."""

    def base(self, **settings):
        if settings:
            self.settings.update(settings)
        return cj.requirements(self.cl.config())

    def test_no_filters_changes_nothing(self):
        base = self.base()
        self.assertIs(cj.narrow(base, {}), base)
        self.assertIs(cj.narrow(base, None), base)

    def test_minimums_only_go_up(self):
        base = self.base()
        for key, low, high in (("min_vram_gb", 8, 24), ("min_driver", 570, 590), ("min_inet_down", 100, 500),
                               ("min_reliability", 0.9, 0.995), ("min_cpu_cores", 0, 8)):
            have = getattr(base, key)
            self.assertEqual(getattr(cj.narrow(base, {key: low}), key), max(have, low), key)
            self.assertEqual(getattr(cj.narrow(base, {key: high}), key), max(have, high), key)
        self.assertEqual(cj.narrow(base, {"min_vram_gb": 8}).min_vram_gb, cj.MIN_VRAM_GB)      # Jasna's floor stays
        self.assertEqual(cj.narrow(base, {"min_driver": 1}).min_driver, cj.MIN_DRIVER)

    def test_price_limits_only_go_down(self):
        base = self.base()
        self.assertEqual(cj.narrow(base, {"max_price": 5}).max_price, 1.0)                    # the owner's cap holds
        self.assertEqual(cj.narrow(base, {"max_price": 0.5}).max_price, 0.5)
        self.assertEqual(cj.narrow(base, {"max_bw_cost": 1}).max_bw_cost, 0.02)
        self.assertEqual(cj.narrow(base, {"max_bw_cost": 0.01}).max_bw_cost, 0.01)

    def test_datacenter_only_can_be_switched_on_but_not_off(self):
        self.assertTrue(cj.narrow(self.base(), {"datacenter_only": True}).datacenter_only)
        self.assertFalse(cj.narrow(self.base(), {"datacenter_only": False}).datacenter_only)
        self.assertTrue(cj.narrow(self.base(datacenter_only=True), {"datacenter_only": False}).datacenter_only)

    def test_the_vram_ceiling_takes_the_smaller_limit(self):
        base = self.base()
        self.assertEqual(cj.narrow(base, {"max_vram_gb": 24}).max_vram_gb, 24)

    def test_what_stashify_decides_stays(self):
        base = self.base()
        spec = cj.narrow(base, {"num_gpus": 4, "max_gpus": 8, "verified": False})
        self.assertEqual((spec.num_gpus, spec.max_gpus, spec.verified), (base.num_gpus, base.max_gpus, base.verified))

    def test_restrictions_apply(self):
        base = self.base()
        spec = cj.narrow(base, {"gpu": "RTX 3090", "countries": ["de", "us"], "provider": "vast",
                                "offer_id": "12", "prefer": "community"})
        self.assertEqual((spec.gpu, spec.countries, spec.provider, spec.offer_id, spec.prefer),
                         ("RTX 3090", ("DE", "US"), "vast", "12", "community"))
        self.assertEqual(cj.narrow(base, {"gpus": ["RTX 3090", "RTX 4090"]}).gpus, ("RTX 3090", "RTX 4090"))

    def test_the_rental_itself_is_never_touched(self):
        base = self.base()
        before = dataclasses.replace(base)
        spec = cj.narrow(base, {"min_vram_gb": 24, "gpu": "RTX 4090"})
        self.assertEqual(base, before)                                                         # no side effects
        for key in ("disk_gb", "image", "env", "gpu_pattern", "max_hours", "boot_timeout"):
            self.assertEqual(getattr(spec, key), getattr(base, key), key)

    def test_a_bad_filter_raises(self):
        with self.assertRaises(ValueError):
            cj.narrow(self.base(), {"nope": 1})
        with self.assertRaises(ValueError):
            cj.narrow(self.base(), {"min_vram_gb": "lots"})


class PlanSpecTest(cf.CloudCase):
    def test_the_automatic_choice_is_unchanged(self):
        cfg = self.cl.config()
        spec = cj.plan_spec(cj.Plan(), cfg, 5_000_000_000)
        self.assertEqual(spec, cj.rent_spec(cfg, 5_000_000_000))
        self.assertEqual(spec.gpus, cj.GPUS)
        self.assertEqual(spec.max_hours, cfg.max_job_hours)

    def test_a_chosen_gpu_drops_the_preference_list(self):
        cfg = self.cl.config()
        spec = cj.plan_spec(cj.Plan(gpu="RTX 3090"), cfg)
        self.assertEqual((spec.gpus, spec.gpu_names()), ((), ("RTX 3090",)))
        spec = cj.plan_spec(cj.Plan(hardware="fastest"), cfg)
        self.assertEqual(spec.gpus, ())                                  # the whole compatible market
        self.assertEqual(spec.gpu_pattern, "^RTX")
        spec = cj.plan_spec(cj.Plan(offer_id="12", provider="vast"), cfg)
        self.assertEqual((spec.offer_id, spec.provider), ("12", "vast"))

    def test_the_disk_grows_with_the_result(self):
        cfg = self.cl.config()
        self.assertEqual(cj.disk_needed(10_000_000_000), 110)             # 80 + the video three times over
        self.assertEqual(cj.disk_needed(10_000_000_000, ratio=4.0), 140)  # ... and a result four times bigger
        self.assertEqual(cj.plan_spec(cj.Plan(), cfg, 10_000_000_000).disk_gb, 110)
        self.assertEqual(cj.plan_spec(cj.Plan(), cfg, 10_000_000_000, ratio=4.0).disk_gb, 140)
        self.assertEqual(cj.plan_spec(cj.Plan(), cfg, 1_000_000).disk_gb, cfg.disk_gb)   # a small video: the setting's floor
        self.assertEqual(cj.requirements(cfg, 0).disk_gb, max(cfg.disk_gb, 80))

    def test_the_result_is_as_many_times_bigger_as_the_pixels(self):
        up = cj.plan_from_body(dict(UPSCALE, upscale_height=2160))
        self.assertEqual(cj.result_ratio(dict(SCENE, height=1080), up), 4.0)
        self.assertEqual(cj.result_ratio(dict(SCENE, height=720), up), 9.0)
        self.assertEqual(cj.result_ratio(dict(SCENE, height=0), up), 4.0)     # unknown height: taken as 1080p
        self.assertEqual(cj.result_ratio(dict(SCENE, height=2160), up), 1.0)
        self.assertEqual(cj.result_ratio(SCENE, cj.Plan()), 1.0)


class PlanEstimateTest(cf.CloudCase):
    """What a run costs: the old Decensor numbers, the new ones for upscaling, and the breakdown."""
    BIG = dict(SCENE, size=2_000_000_000, height=720)        # a 10 minute 720p clip of 2 GB

    def offer(self, gpu="RTX 4090", price=0.40, **more):
        return parse_offer(raw_offer(1, gpu, price, **more))

    def test_the_breakdown_counts_the_disk_and_the_traffic(self):
        offer = self.offer(storage_cost=0.72, inet_up_cost=0.01, inet_down_cost=0.004)   # $0.001 per GB-hour
        est = cj.estimate_job(self.BIG, "detail", offer, 4.6)
        b = est.as_dict()["breakdown"]
        self.assertEqual(est.disk_gb, 86)
        self.assertAlmostEqual(b["storage"], 86 * 0.001 * est.hours, places=3)
        self.assertAlmostEqual(b["compute"], 0.40 * est.hours, places=3)
        self.assertAlmostEqual(b["gb_down"], 2.0 + 4.6 + 28 + 0.2, places=2)     # video + Jasna + model + apt
        self.assertAlmostEqual(b["gb_up"], 2.0, places=2)                         # the result, same size
        self.assertAlmostEqual(b["bw_down"], b["gb_down"] * 0.004, places=3)
        self.assertAlmostEqual(b["bw_up"], b["gb_up"] * 0.01, places=3)
        self.assertAlmostEqual(b["total"], b["compute"] + b["storage"] + b["bandwidth"], places=3)
        self.assertGreaterEqual(est.est_usd, b["total"])                         # rounded UP to the cent
        self.assertLess(est.est_usd - b["total"], 0.01 + 1e-9)

    def test_unreported_prices_count_as_zero_and_say_so(self):
        est = cj.estimate_job(self.BIG, "detail", self.offer(), 4.6)
        b = est.as_dict()["breakdown"]
        self.assertEqual((b["storage"], b["bandwidth"]), (0.0, 0.0))
        self.assertTrue(b["notes"])
        self.assertEqual(est.est_usd, math.ceil(round(b["total"] * 100, 6)) / 100)

    def test_the_dict_has_everything_a_dashboard_needs(self):
        out = cj.estimate(self.BIG, "detail", self.offer(), 4.6)
        self.assertEqual(set(out), {"hours", "minutes", "breakdown", "est_usd", "assumptions", "setup_min",
                                    "transfer_min", "processing_min", "total_min", "disk_gb"})
        self.assertEqual(out["total_min"], out["minutes"])
        self.assertAlmostEqual(out["setup_min"] + out["transfer_min"] + out["processing_min"], out["total_min"],
                               delta=0.2)
        self.assertTrue(all(isinstance(a, str) for a in out["assumptions"]))
        json.dumps(out)                                                         # plain data

    def test_decensoring_does_not_mention_upscaling(self):
        out = cj.estimate(self.BIG, "detail", self.offer(), 4.6)
        self.assertFalse(any("Upscal" in a or "upscal" in a for a in out["assumptions"]))

    def test_upscaling_adds_time_a_bigger_download_and_a_bigger_disk(self):
        plain = cj.estimate_job(self.BIG, "detail", self.offer(), 4.6)
        both = cj.estimate_job(self.BIG, "detail", self.offer(), 4.6, cj.plan_from_body(UPSCALE))
        self.assertAlmostEqual(plain.processing_min, 45.0, places=1)
        self.assertAlmostEqual(both.processing_min, 45.0 + 25.2, places=1)    # 10 min x 0.35 x 4 x 1.5 x 1.2
        self.assertGreater(both.transfer_min, plain.transfer_min)             # the result is 9x the pixels
        self.assertEqual((plain.disk_gb, both.disk_gb), (86, 80 + 22))        # 80 + (2 + 9 x ...) ceil'd
        self.assertAlmostEqual(both.as_dict()["breakdown"]["gb_up"], 18.0, places=1)
        self.assertGreater(both.est_usd, plain.est_usd)
        self.assertTrue(any("rough guess" in a for a in both.assumptions))

    def test_the_models_are_ranked_by_how_heavy_they_are(self):
        mins = {m: cj.estimate_job(self.BIG, "detail", self.offer(), 4.6,
                                   cj.plan_from_body(dict(UPSCALE, upscale=m))).processing_min
                for m in cj.UPSCALE_MODELS}
        self.assertLess(mins["span"], mins["rtx-super-res"])
        self.assertLess(mins["rtx-super-res"], mins["realplksr"])

    def test_a_taller_target_costs_more(self):
        by = {h: cj.estimate_job(self.BIG, "detail", self.offer(), 4.6,
                                 cj.plan_from_body(dict(UPSCALE, upscale_height=h))).est_usd
              for h in cj.UPSCALE_HEIGHTS}
        self.assertLess(by[1080], by[1440])
        self.assertLess(by[1440], by[2160])

    def test_upscaling_alone_skips_the_restoration_model(self):
        up = cj.plan_from_body({"op": "upscale", "upscale": "rtx-super-res", "upscale_height": 1440})
        alone = cj.estimate_job(self.BIG, None, self.offer(), 4.6, up)
        both = cj.estimate_job(self.BIG, "detail", self.offer(), 4.6, cj.plan_from_body(
            {"op": "decensor+upscale", "upscale": "rtx-super-res", "upscale_height": 1440}))
        self.assertAlmostEqual(alone.processing_min, 4.5 + 16.0, delta=0.1)   # a pass-through share + the upscaler
        self.assertLess(alone.est_usd, both.est_usd)
        self.assertLess(alone.setup_min, both.setup_min)                      # no 28 GB model to download
        self.assertAlmostEqual(alone.as_dict()["breakdown"]["gb_down"], 2.0 + 4.6 + 0.2, places=2)
        self.assertTrue(any("scans every frame" in a for a in alone.assumptions))

    def test_a_restoring_run_needs_its_preset(self):
        with self.assertRaises(KeyError):
            cj.estimate_job(self.BIG, None, self.offer(), 4.6)

    def test_the_estimator_gives_the_quotes_numbers(self):
        offer = self.offer(storage_cost=0.72, inet_up_cost=0.01, inet_down_cost=0.004)
        cfg = self.cl.config()
        plan = cj.plan_from_body(UPSCALE)
        disk = cj.requirements(cfg, self.BIG["size"], ratio=cj.result_ratio(self.BIG, plan)).disk_gb
        est = cj.make_estimator(cfg, self.BIG, "detail", plan, 4.6)(offer)
        self.assertEqual(est.as_dict(), cj.estimate_job(self.BIG, "detail", offer, 4.6, plan, disk).as_dict())
        self.assertEqual(est.disk_gb, 102)


class PlanQuoteTest(cf.CloudCase):
    """The same function the server refuses with, asked about the new operations and machine choices."""

    def quote(self, plan=None, preset="detail", scene=None, **kw):
        return cj.quote_with_spec(self.cl, scene or SCENE, preset, plan, release_gb=4.6, **kw)

    def market(self, extra=()):
        self.vast.offers = market(extra)

    def test_a_quote_carries_the_machine_and_the_breakdown(self):
        q, spec = self.quote()
        self.assertTrue(q["ok"])
        self.assertEqual((q["op"], q["machine"], q["preset"]), ("decensor", "auto", "detail"))
        self.assertEqual(q["plan"], cj.Plan().as_dict())
        self.assertEqual((q["offer"]["provider"], q["offer"]["gpu"], q["offer"]["price_per_hour"]),
                         ("vast", "RTX 4090", 0.4))
        self.assertEqual(set(q["offer"]), set(cj._BRIEF))
        self.assertEqual((q["max_job_hours"], q["disk_gb"]), (3.0, 100))    # 100 GB is the settings' floor
        for key in ("hours", "minutes", "breakdown", "assumptions", "setup_min", "processing_min", "transfer_min",
                    "total_min"):
            self.assertIn(key, q)
        self.assertEqual(spec.gpus, cj.GPUS)
        self.assertEqual(spec.disk_gb, q["disk_gb"])
        self.assertNotIn(KEY, json.dumps(q))

    def test_a_refusal_has_no_spec(self):
        self.vast.offers = []
        q, spec = self.quote()
        self.assertEqual(q["refusal"]["code"], "no_offer")
        self.assertIsNone(spec)
        self.assertIn("under $1.00/hour", q["refusal"]["message"])

    def test_upscale_quotes_have_their_own_words(self):
        up = cj.plan_from_body({"op": "upscale", "upscale": "span", "upscale_height": 2160})
        q, _ = self.quote(up, preset=None)
        self.assertEqual((q["op"], q["preset"], q["label"]), ("upscale", None, "Upscale to 2160p \u00b7 SPAN"))
        self.assertTrue(q["ok"])
        both, _ = self.quote(cj.plan_from_body({"op": "decensor+upscale"}))
        plain, _ = self.quote()
        self.assertEqual((both["op"], both["preset"], both["label"]), ("decensor+upscale", "detail",
                                                                       "High detail + upscale to 2160p"))
        self.assertGreater(both["est_usd"], plain["est_usd"])
        self.assertGreater(both["est_usd"], q["est_usd"])                 # upscaling alone is the cheapest
        with self.assertRaises(ValueError):
            self.quote(cj.Plan(), preset=None)                            # restoring needs a preset

    def test_upscaling_what_is_already_that_tall_is_refused(self):
        for height, ok in ((1080, False), (1440, True), (2160, True)):
            q, _ = self.quote(cj.plan_from_body(dict(UPSCALE, upscale_height=height)))
            self.assertEqual(q["ok"], ok, height)
            if not ok:
                self.assertEqual(q["refusal"]["code"], "upscale_not_needed")
                self.assertIn("1080p", q["refusal"]["message"])
        q, _ = self.quote(cj.plan_from_body(dict(UPSCALE, upscale_height=1080)), scene=dict(SCENE, height=720))
        self.assertTrue(q["ok"])
        q, _ = self.quote(cj.plan_from_body(dict(UPSCALE, upscale_height=1080)), scene=dict(SCENE, height=0))
        self.assertTrue(q["ok"])                                          # height unknown: nothing to compare

    def test_a_job_longer_than_the_rental_limit_is_refused(self):
        self.settings.update({"job_cap": 1000, "month_cap": 10000})
        long = dict(SCENE, duration=2 * 3600.0)                           # about nine hours of GPU time
        q, spec = self.quote(scene=long)
        self.assertEqual(q["refusal"]["code"], "over_job_hours")
        self.assertIsNone(spec)
        self.assertGreater(q["hours"], 3.0)
        self.assertIn("3.0 hour", q["refusal"]["message"])
        self.settings.update({"max_job_hours": 12})
        q, spec = self.quote(scene=long)
        self.assertIsNone(q["refusal"])
        self.assertEqual(spec.max_hours, 12)                              # the rental may run that long

    def test_the_dollar_cap_is_checked_before_the_hour_limit(self):
        q, _ = self.quote(scene=dict(SCENE, duration=2 * 3600.0))
        self.assertEqual(q["refusal"]["code"], "over_job_cap")

    def test_each_hardware_choice_picks_one_machine(self):
        self.market()
        for hardware, oid in (("cheapest", 13), ("fastest", 11), ("most_reliable", 14)):
            q, spec = self.quote(cj.Plan(hardware=hardware))
            self.assertEqual((q["ok"], q["machine"], q["offer"]["id"]), (True, "chosen", oid), hardware)
            self.assertEqual((spec.offer_id, spec.provider), (str(oid), "vast"), hardware)
        q, spec = self.quote(cj.Plan(hardware="best_value"))
        cost = {o["id"]: cj.estimate_job(SCENE, "detail", parse_offer(o), 4.6).est_usd for o in self.vast.offers}
        self.assertEqual(q["est_usd"], min(cost.values()))                # the least money for the whole job
        self.assertEqual(spec.offer_id, str(q["offer"]["id"]))

    def test_the_cheapest_choice_follows_the_market(self):
        self.market([(15, "RTX 3090", 0.05, {"vram_mb": 24576, "reliability": 0.99})])     # a very cheap card
        q, _ = self.quote(cj.Plan(hardware="cheapest"))
        self.assertEqual(q["offer"]["id"], 15)

    def test_a_hardware_choice_with_nothing_to_pick_is_a_refusal(self):
        self.vast.offers = []
        q, spec = self.quote(cj.Plan(hardware="fastest"))
        self.assertEqual(q["refusal"]["code"], "no_offer")
        self.assertIn("hardware", q["refusal"]["message"])
        self.assertIsNone(spec)

    def test_a_picked_machine_is_that_machine_or_nothing(self):
        self.market()
        plan = cj.Plan(offer_id="14", provider="vast")
        q, spec = self.quote(plan)
        self.assertEqual((q["offer"]["id"], q["price_per_hour"], q["machine"]), (14, 0.55, "chosen"))
        self.assertEqual((spec.offer_id, spec.provider), ("14", "vast"))
        self.vast.offers = [o for o in self.vast.offers if o["id"] != 14]
        q, spec = self.quote(plan, fresh=True)                            # the run looks again
        self.assertEqual(q["refusal"]["code"], "offer_gone")
        self.assertIn("Nothing was rented", q["refusal"]["message"])
        self.assertIsNone(spec)
        self.assertEqual(self.vast.count("PUT", "/asks/"), 0)

    def test_a_picked_machine_over_the_price_cap_is_gone_too(self):
        self.market([(17, "RTX 4090", 1.50, {})])
        q, _ = self.quote(cj.Plan(offer_id="17"))
        self.assertEqual(q["refusal"]["code"], "offer_gone")

    def test_a_picked_machine_that_jasna_cannot_use_is_gone_too(self):
        self.market([(18, "RTX 4090", 0.30, {"driver": "550.54.14"})])     # driver older than Jasna needs
        q, _ = self.quote(cj.Plan(offer_id="18"))
        self.assertEqual(q["refusal"]["code"], "offer_gone")

    def test_a_chosen_gpu_model(self):
        self.market()
        q, _ = self.quote(cj.Plan(gpu="RTX 3090"))
        self.assertEqual((q["offer"]["id"], q["machine"]), (13, "auto"))
        q, _ = self.quote(cj.Plan(gpu="RTX 4090"))
        self.assertIn(q["offer"]["id"], (12, 14))
        self.assertEqual(q["offer"]["gpu"], "RTX 4090")

    def test_filters_tighten_but_never_loosen(self):
        self.market([(15, "RTX 4090", 0.10, {"reliability": 0.90}), (16, "RTX 4090", 1.50, {})])
        q, _ = self.quote(cj.Plan(filters={"min_reliability": 0.5, "max_price": 5}))
        self.assertNotIn(q["offer"]["id"], (15, 16))                      # still reliable and under the owner's cap
        self.assertGreaterEqual(q["offer"]["reliability"], 0.98)
        q, _ = self.quote(cj.Plan(filters={"min_reliability": 0.995}))
        self.assertEqual(q["offer"]["id"], 14)
        q, _ = self.quote(cj.Plan(filters={"min_vram_gb": 30}))
        self.assertEqual(q["offer"]["id"], 11)

    def test_filters_nothing_matches(self):
        self.market()
        q, spec = self.quote(cj.Plan(filters={"countries": ["DE"]}))
        self.assertEqual(q["refusal"]["code"], "no_offer")
        self.assertIn("your choices", q["refusal"]["message"])
        self.assertIsNone(spec)

    def test_datacenter_only_and_the_bandwidth_ceiling_are_the_owners_settings(self):
        self.market()
        self.settings.update({"datacenter_only": True})
        q, _ = self.quote(cj.Plan(gpu="RTX 4090"))
        self.assertEqual(q["offer"]["id"], 14)                            # the only datacenter 4090
        self.assertTrue(q["offer"]["datacenter"])
        self.settings.update({"datacenter_only": False})
        self.vast.offers = [raw_offer(21, "RTX 4090", 0.30, inet_up_cost=0.50),     # costs $0.50 per GB uploaded
                            raw_offer(22, "RTX 4090", 0.45, inet_up_cost=0.005)]
        self.cl._quote_cache.clear()
        q, _ = self.quote()
        self.assertEqual(q["offer"]["id"], 22)                            # 21 is over the default $0.02/GB
        self.settings.update({"max_bw_cost": 1.0})
        self.cl._quote_cache.clear()
        q, _ = self.quote()
        self.assertEqual(q["offer"]["id"], 21)                            # allowed now, and cheaper

    def test_every_search_after_a_choice_is_one_lookup(self):
        self.market()
        self.vast.calls.clear()
        self.quote(cj.Plan(hardware="fastest"))
        self.assertEqual(self.vast.count("POST", "/bundles/"), 1)

    def test_a_quote_may_reuse_the_lookup_but_a_run_never_does(self):
        self.market()
        self.quote()
        self.vast.calls.clear()
        self.quote()
        self.assertEqual(self.vast.count("POST", "/bundles/"), 0)         # cached for a couple of minutes
        self.quote(fresh=True)
        self.assertEqual(self.vast.count("POST", "/bundles/"), 1)


class PlanArgsTest(unittest.TestCase):
    """The exact Jasna options for each operation."""

    def test_job_args(self):
        self.assertEqual(cj.job_args("detail"), ["--restoration-model-name", "ltx-8step"])
        self.assertEqual(cj.job_args("detail", cj.Plan()), cj.preset_args("detail"))
        self.assertEqual(cj.job_args("close-ups", cj.plan_from_body(UPSCALE)),
                         ["--restoration-model-name", "ltx", "--ltx-large-canvas",
                          "--upscale", "span", "--upscale-height", "2160"])
        self.assertEqual(cj.job_args(None, cj.plan_from_body({"op": "upscale", "upscale": "rtx-super-res",
                                                              "upscale_height": 1440})),
                         ["--no-compile-basicvsrpp", "--upscale", "rtx-super-res", "--upscale-height", "1440"])

    def test_preset_args_are_not_shared(self):
        cj.job_args("detail", cj.plan_from_body(UPSCALE))
        self.assertEqual(cj.preset_args("detail"), ["--restoration-model-name", "ltx-8step"])

    def test_the_command(self):
        cmd = cj.run_command("/root/in/source.mkv", "detail", cj.plan_from_body(UPSCALE))
        self.assertIn("./jasna --input /root/in/source.mkv --output %s" % cj.REMOTE_OUT, cmd)
        self.assertIn("--restoration-model-name ltx-8step --upscale span --upscale-height 2160 --log-level info", cmd)
        plain = cj.run_command("/root/in/source.mkv", "detail")
        self.assertNotIn("--upscale", plain)
        self.assertEqual(plain, cj.run_command("/root/in/source.mkv", "detail", cj.Plan()))
        alone = cj.run_command("/root/in/source.mkv", None, cj.plan_from_body({"op": "upscale"}))
        self.assertNotIn("--restoration-model-name", alone)
        self.assertIn("--no-compile-basicvsrpp --upscale span --upscale-height 2160", alone)

    def test_the_file_suffix_follows_core(self):
        for op in cj.OPS:
            plan = cj.plan_from_body(dict(UPSCALE, op=op))
            self.assertEqual(cj.output_suffix(plan), core.op_suffix("upscale" if op == "upscale" else "decensor"), op)

    def test_the_labels(self):
        self.assertEqual(cj.plan_label(cj.Plan(), "close-ups"), "Sharper close-ups")
        self.assertEqual(cj.plan_label(cj.plan_from_body({"op": "decensor+upscale", "upscale": "realplksr",
                                                          "upscale_height": 1440}), "detail"),
                         "High detail + upscale to 1440p")
        self.assertEqual(cj.plan_label(cj.plan_from_body({"op": "upscale", "upscale": "rtx-super-res"}), None),
                         "Upscale to 2160p \u00b7 RTX Super Resolution")


class PlanRunTest(JasnaCase):
    """Decensor + upscale and Upscale on the rented machine, and which machine gets rented."""

    def cfg_for(self, plan, preset="detail", scene=None):
        scene = scene or SCENE
        key = None if plan.op == "upscale" else preset
        est = cj.build_quote(self.cl, scene, key, plan=plan, release_gb=4.6)["est_usd"]
        return {"cloud": {"preset": key, "scene": scene, "plan": plan.as_dict(), "est_usd": est,
                          "confirm_usd": est, "job_id": "job1"}}

    def rented(self):
        return [re.search(r"/asks/(\d+)/", p).group(1) for m, p, _b in self.vast.calls
                if m == "PUT" and "/asks/" in p]

    def test_decensor_then_upscale_in_one_run(self):
        produced = self.run_job(self.cfg_for(cj.plan_from_body(UPSCALE)))
        (cmd,) = self.world.jasna_cmds
        self.assertIn("--restoration-model-name ltx-8step", cmd)
        self.assertIn("--upscale span --upscale-height 2160", cmd)
        self.assertNotIn("--no-compile-basicvsrpp", cmd)
        self.assertTrue(produced.endswith("_decensored.mp4"))
        self.assertClean()
        (row,) = self.ledger()
        self.assertEqual((row["status"], row["op"], row["preset"]), ("ok", "decensor+upscale", "detail"))
        self.assertGreater(row["usd"], 0)

    def test_upscale_alone(self):
        plan = cj.plan_from_body({"op": "upscale", "upscale": "rtx-super-res", "upscale_height": 1440})
        produced = self.run_job(self.cfg_for(plan))
        (cmd,) = self.world.jasna_cmds
        self.assertIn("--no-compile-basicvsrpp --upscale rtx-super-res --upscale-height 1440", cmd)
        self.assertNotIn("--restoration-model-name", cmd)
        self.assertTrue(produced.endswith("_upscaled.mp4"))
        self.assertEqual(os.path.basename(produced), "My_Scene_1080p__upscaled.mp4")
        self.assertClean()
        (row,) = self.ledger()
        self.assertEqual((row["status"], row["op"], row["preset"]), ("ok", "upscale", None))
        messages = [s["message"] for s in self.status]
        self.assertIn("Starting Jasna (the first run prepares the GPU)", messages)       # no model to download

    def test_decensoring_still_says_it_may_download_the_model(self):
        self.run_job()
        self.assertIn("Starting Jasna (the first run prepares the GPU and may download the model)",
                      [s["message"] for s in self.status])

    def test_a_plan_with_no_upscale_runs_plain_jasna(self):
        self.run_job()
        (cmd,) = self.world.jasna_cmds
        self.assertNotIn("--upscale", cmd)

    def test_the_run_names_the_machine_it_rented(self):
        self.run_job()
        (rented,) = self.rented()
        last = [s for s in self.stamps if "cloud_usd" in s][-1]
        self.assertEqual((last["runner"], last["engine"], last["cloud"]), ("Vast.ai \u00b7 RTX 4090", "jasna", True))
        self.assertEqual((last["provider"], last["cloud_tier"]), ("Vast.ai", "dc"))
        phases = [s["cloud_phase"] for s in self.stamps if "cloud_phase" in s]
        order = [p for i, p in enumerate(phases) if p not in phases[:i]]
        self.assertEqual(order, ["boot", "upload", "run", "download", "cleanup"])
        self.assertEqual((last["cloud_gpu"], last["cloud_provider"], last["cloud_offer_id"]),
                         ("RTX 4090", "vast", rented))
        self.assertGreater(last["cloud_cap"], 0)

    def test_a_picked_machine_is_the_one_rented(self):
        self.vast.offers = market()
        plan = cj.plan_from_body({"offer_id": "14", "provider": "vast"})
        self.run_job(self.cfg_for(plan))
        self.assertEqual(self.rented(), ["14"])
        self.assertEqual([s for s in self.stamps if "cloud_offer_id" in s][-1]["cloud_offer_id"], "14")
        self.assertClean()

    def test_a_chosen_gpu_is_the_one_rented(self):
        self.vast.offers = market()
        self.run_job(self.cfg_for(cj.plan_from_body({"gpu": "RTX 3090"})))
        self.assertEqual(self.rented(), ["13"])
        self.assertEqual([s for s in self.stamps if "cloud_gpu" in s][-1]["cloud_gpu"], "RTX 3090")

    def test_a_picked_machine_that_was_taken_is_never_replaced(self):
        self.vast.offers = market()
        cfg = self.cfg_for(cj.plan_from_body({"offer_id": "14", "provider": "vast"}))
        self.vast.create_modes = ["gone", "ok", "ok"]
        with self.assertRaises(cj.CloudRefused) as cm:
            self.run_job(cfg)
        self.assertEqual(cm.exception.code, "offer_gone")
        self.assertEqual(self.rented(), ["14"])                            # one try, no other machine
        self.assertClean()
        self.assertEqual(self.ledger(), [])

    def test_a_picked_machine_that_vanished_before_the_run_is_a_refusal(self):
        self.vast.offers = market()
        cfg = self.cfg_for(cj.plan_from_body({"offer_id": "14", "provider": "vast"}))
        self.vast.offers = [o for o in self.vast.offers if o["id"] != 14]
        with self.assertRaises(cj.CloudRefused) as cm:
            self.run_job(cfg)
        self.assertEqual(cm.exception.code, "offer_gone")
        self.assertNothingRented()

    def test_a_run_for_an_upscale_that_is_not_needed_is_refused(self):
        cfg = self.cfg_for(cj.plan_from_body(UPSCALE))
        cfg["cloud"]["plan"] = cj.plan_from_body(dict(UPSCALE, upscale_height=1080)).as_dict()
        with self.assertRaises(cj.CloudRefused) as cm:
            self.run_job(cfg)
        self.assertEqual(cm.exception.code, "upscale_not_needed")
        self.assertNothingRented()

    def test_core_sends_upscale_jobs_to_the_rented_gpu_like_decensor_jobs(self):
        cloud_cfg = {"cloud": {"preset": None}}
        for backend in (core.backend_upscale, core.backend_decensor):
            with mock.patch.object(cj, "run_decensor", return_value="/out/x.mp4") as run, \
                    mock.patch.object(core, "_runner_dispatch") as dispatch:
                self.assertEqual(backend(cloud_cfg, "/in/x.mp4", "/out"), "/out/x.mp4")
            run.assert_called_once()
            dispatch.assert_not_called()
        with mock.patch.object(core, "_runner_dispatch", return_value="/r.mp4") as dispatch:
            self.assertEqual(core.backend_upscale({}, "/in/x.mp4", "/out"), "/r.mp4")
        self.assertEqual(dispatch.call_args.args[3], "upscale")


if __name__ == "__main__":
    unittest.main()
