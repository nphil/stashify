"""cloud_jasna: presets, quote and the server-side refusals, the Jasna-in-the-cloud
lifecycle, progress parsing, release lookup. Fake Vast + scripted fake machine: nothing
is rented and no real time passes."""

import base64
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cloudfakes as cf              # noqa: E402
from cloudfakes import KEY, LICENCE, Resp, raw_offer   # noqa: E402
import cloud_ssh                     # noqa: E402
import cloud_vast                    # noqa: E402
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
            return cloud_ssh.RunResult(0, ["NVIDIA GeForce RTX 4090, 24564 MiB, 580.95.05"])
        if "apt-get install" in script and "jasna-linux-nvidia" in script:
            code = self.install_code.get(tag, 0)
            return cloud_ssh.RunResult(code, ["[stashify] step: installed"] if code == 0 else ["E: boom"])
        if "hevc_nvenc" in script:
            mark = cj.OK_MARK if self.encoder_ok.get(tag, True) else cj.FAIL_MARK
            return cloud_ssh.RunResult(0, [mark])
        if "read -r JASNA_LICENSE_CODE" in script:
            self.activation_stdin.append(kw["stdin_data"])
            code, lines = self.activation
            return cloud_ssh.RunResult(code, lines)
        if "./jasna --input" in script:
            return self._run(script, kw)
        if "stat -c %s" in script:
            return cloud_ssh.RunResult(0, [str(self.remote_size)])
        return cloud_ssh.RunResult(0, [])

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
        return cloud_ssh.RunResult(self.run_code, self.run_lines[-2:] if self.run_code == 0 else ["Error: boom"])


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
        spec = cj.rent_spec(cloud_vast.Config(), 5_000_000_000)
        self.assertEqual(spec.env, {"NVIDIA_DRIVER_CAPABILITIES": "all"})
        self.assertEqual((spec.min_vram_gb, spec.min_driver), (16, 580))
        self.assertGreaterEqual(spec.disk_gb, 80 + 15)               # upstream: >= 80 GB, plus the video x3


class EstimateTest(cf.CloudCase):
    def offer(self, gpu="RTX 4090", price=0.40, down=900.0):
        return cloud_vast._parse_offer(raw_offer(1, gpu, price, inet_down=down))

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
        self.cl.ledger.append({"ended": cloud_vast._iso(self.clock.time()), "usd": 9.80, "status": "ok"})
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
        self.world.encoder_ok = {7001: False}
        self.run_job()
        self.assertEqual([e["status"] for e in self.ledger()], ["failed-offer", "ok"])
        self.assertEqual(len(self.world.activation_stdin), 1)             # only the good machine was activated
        self.assertEqual(sum("jasna-linux-nvidia-*.part*" in s for s in self.scripts()), 2)
        self.assertClean()

    def test_a_failed_install_also_tries_the_next_offer(self):
        self.world.install_code = {7001: 100}
        self.run_job()
        self.assertEqual([e["status"] for e in self.ledger()], ["failed-offer", "ok"])
        self.assertClean()

    def test_three_bad_machines_then_stop(self):
        self.world.encoder_ok = {7001: False, 7002: False, 7003: False, 7004: False}
        with self.assertRaises(cloud_vast.BoxFailed):
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
            raise cloud_ssh.SshError("connection lost")
        self.tp.get = lambda ep, tag, remote, local, **kw: partial(tag, remote, local)
        with self.assertRaises(cloud_ssh.SshError):
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
        self.cl.ledger.append({"ended": cloud_vast._iso(self.clock.time()), "usd": 9.90, "status": "ok"})
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
        cfg = self.cfg()                                   # this lookup is now cached ...
        self.vast.offers = []                              # ... and the market empties out
        with self.assertRaises(cj.CloudRefused) as cm:
            self.run_job(cfg)
        self.assertEqual(cm.exception.code, "no_offer")
        self.assertIn("Nothing was rented", str(cm.exception))
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
        cloud_vast.register_secret(KEY)
        with self.assertRaises(cj.CloudJobFailed) as cm:
            cj.release_assets(http_get=boom, now=1.0)
        self.assertIn("GitHub", str(cm.exception))
        self.assertNotIn(KEY, str(cm.exception))

    def test_scene_meta(self):
        m = cj.scene_meta({"id": "7", "title": None, "files": [{"path": "/media/a/b/clip.mkv", "width": 1920, "height": 1080,
                                                                "duration": 61.5, "size": 123}]})
        self.assertEqual((m["id"], m["title"], m["duration"], m["size"], m["height"]), ("7", "clip.mkv", 61.5, 123, 1080))
        self.assertEqual(cj.scene_meta({"id": "8", "title": "T", "files": []})["duration"], 0)


if __name__ == "__main__":
    unittest.main()
