"""Cloud transcode and the one capability filter (cloud_caps): what each card may do, the quote's
honest transfer and storage lines, and the rented-box run of winrunner/transcode_cli.py. Fake Vast and a
scripted fake machine: nothing is rented and no real time passes."""

import base64
import dataclasses
import os
import re
import unittest

import cloudfakes as cf
from cloudfakes import KEY, market, raw_offer
import hireling
from hireling import ssh
from hireling.providers.vast import parse_offer
import cloud_api
import cloud_caps
import cloud_jasna as cj

SCENE = {"id": "5", "title": "My Scene [1080p]", "duration": 600.0, "size": 500_000_000,
         "height": 1080, "width": 1920}
BASE = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
FFMPEG = {"tag": "latest", "name": "ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz", "size": 154_000_000, "gb": 0.154,
          "url": BASE + "ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz"}


def script_of(cmd):
    m = re.search(r"echo ([A-Za-z0-9+/=]+) \| base64 -d", cmd)
    return base64.b64decode(m.group(1)).decode() if m else cmd


class CapsTest(unittest.TestCase):
    def test_one_filter_per_operation(self):
        for gpu in ("RTX 3080", "RTX 4090", "RTX 5090", "GTX 1660", "L4", "A10G", "Tesla T4", "Quadro RTX 6000"):
            self.assertIsNone(cloud_caps.problem("transcode", gpu, 10.0), gpu)
        for gpu in ("A100", "A100 SXM4", "H100 NVL", "H200", "Tesla V100", "Tesla P100", "B200", "A30"):
            self.assertEqual(cloud_caps.problem("transcode", gpu, 80.0), "No video encoder", gpu)
        self.assertEqual(cloud_caps.problem("transcode", "RTX 3050", 1.0), "Needs 2 GB (this has 1)")

    def test_jasna_needs_an_rtx_card_with_10_gb(self):
        self.assertIsNone(cloud_caps.problem("decensor", "RTX 3080", 10.0))      # the owner's home card
        self.assertIsNone(cloud_caps.problem("upscale", "RTX 3060", 12.0))
        self.assertEqual(cloud_caps.problem("decensor", "RTX 3070", 8.0), "Needs 10 GB (this has 8)")
        self.assertEqual(cloud_caps.problem("decensor+upscale", "A100", 80.0), "Needs an RTX card")
        self.assertEqual(cloud_caps.problem("decensor", "L4", 24.0), "Needs an RTX card")

    def test_every_operation_is_listed_and_only_transcode_skips_jasna(self):
        self.assertEqual(set(cloud_caps.OPS), {"decensor", "decensor+upscale", "upscale", "transcode"})
        self.assertEqual([op for op in cloud_caps.OPS if not cloud_caps.uses_jasna(op)], ["transcode"])

    def test_the_requirements_follow_the_operation(self):
        cfg = hireling.settings.Config()
        cfg = dataclasses.replace(cfg, disk_gb=10)
        t, d = cj.requirements(cfg, 5_000_000_000, op="transcode"), cj.requirements(cfg, 5_000_000_000)
        self.assertEqual((t.min_vram_gb, t.min_driver, t.gpus), (2, 570, ()))
        self.assertEqual((d.min_vram_gb, d.min_driver, d.gpus), (10, 580, ()))
        self.assertLess(t.disk_gb, d.disk_gb)                                      # no 80 GB of Jasna and its models
        for gpu in ("RTX 3080", "L4", "A10"):
            self.assertTrue(re.search(t.gpu_pattern, gpu, re.I), gpu)
        for gpu in ("A100", "H100", "V100"):
            self.assertFalse(re.search(t.gpu_pattern, gpu, re.I), gpu)

    def test_a_3080_is_no_longer_left_out_of_the_automatic_choice(self):
        spec = cj.plan_spec(cj.Plan(), hireling.settings.Config(), 1_000_000_000)
        self.assertEqual(spec.gpus, ())
        self.assertTrue(re.search(spec.gpu_pattern, "RTX 3080", re.I))


class PlanTest(unittest.TestCase):
    def test_a_transcode_plan(self):
        plan = cj.plan_from_body({"transcode_height": "720", "transcode_quality": 28}, "transcode")
        self.assertEqual((plan.op, plan.transcode_height, plan.transcode_quality), ("transcode", 720, 28))
        self.assertEqual((plan.upscale, plan.upscale_height, plan.jasna, plan.restoring), ("", 0, False, False))
        self.assertEqual(cj.plan_from_body(plan.as_dict()), plan)
        self.assertEqual(cj.plan_label(plan), "Transcode to 720p")
        self.assertEqual(cj.plan_label(cj.plan_from_body({"op": "transcode"})), "Transcode \u00b7 same size")

    def test_bad_transcode_numbers_are_refused(self):
        for body in ({"transcode_height": "20"}, {"transcode_height": "9000"},
                     {"transcode_quality": "99"}, {"transcode_height": "tall"}):
            with self.assertRaises(cj.PlanError) as cm:
                cj.plan_from_body(dict(body, op="transcode"))
            self.assertEqual(cm.exception.code, "bad_transcode", body)

    def test_the_file_suffix_is_the_home_one(self):
        import core
        self.assertEqual(cj.output_suffix(cj.plan_from_body({"op": "transcode"})), core.op_suffix("transcode"))


class ProgressTest(unittest.TestCase):
    def test_transcode_cli_lines(self):
        p = cj.RunProgress("transcode")
        upd = p.parse("transcode: 42%| |Processed: 00:12 (1234f) | Remaining: 01:23 | Speed: 3.2f/s 1.50x")
        self.assertEqual(upd["message"], "Transcoding in the cloud")
        self.assertAlmostEqual(upd["progress"], 0.42)
        self.assertEqual((upd["frame"], upd["fps"], upd["eta"]), (1234, 3.2, 83))
        self.assertEqual(cj.cloud_phase_of(upd["message"]), "run")
        self.assertEqual(cj.cloud_phase_of("Starting the transcode (hevc_nvenc)"), "run")
        self.assertEqual(cj.cloud_phase_of("Installing ffmpeg on the rented RTX 3080"), "boot")


class FfmpegAssetTest(unittest.TestCase):
    def setUp(self):
        cj._ffmpeg_cache.update(at=0.0, data=None)

    def release(self, *names):
        return {"tag_name": "latest", "assets": [{"name": n, "browser_download_url": BASE + n, "size": 150_000_000}
                                                 for n in names]}

    def test_the_oldest_stable_branch_wins(self):
        got = cj.ffmpeg_asset(lambda url: self.release(
            "ffmpeg-master-latest-linux64-gpl.tar.xz", "ffmpeg-n9.0-latest-linux64-gpl-9.0.tar.xz",
            "ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz", "ffmpeg-n8.1-latest-linux64-gpl-shared-8.1.tar.xz",
            "ffmpeg-n8.1-latest-win64-gpl-8.1.zip"), now=1.0)
        self.assertEqual(got["name"], "ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz")
        self.assertAlmostEqual(got["gb"], 0.15)

    def test_master_is_the_last_resort_and_nothing_means_nothing_is_rented(self):
        got = cj.ffmpeg_asset(lambda url: self.release("ffmpeg-master-latest-linux64-gpl.tar.xz"), now=1.0)
        self.assertEqual(got["name"], "ffmpeg-master-latest-linux64-gpl.tar.xz")
        cj._ffmpeg_cache.update(at=0.0, data=None)
        with self.assertRaises(cj.CloudJobFailed):
            cj.ffmpeg_asset(lambda url: self.release("ffmpeg-n8.1-latest-win64-gpl-8.1.zip"), now=1.0)
        with self.assertRaises(cj.CloudJobFailed):
            cj.ffmpeg_asset(lambda url: (_ for _ in ()).throw(OSError("offline")), now=1.0)

    def test_a_foreign_download_url_is_ignored(self):
        data = self.release("ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz")
        data["assets"][0]["browser_download_url"] = "https://evil.example/ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz"
        with self.assertRaises(cj.CloudJobFailed):
            cj.ffmpeg_asset(lambda url: data, now=1.0)


class QuoteTest(cf.CloudCase):
    """The honest bill: both transfers, the ffmpeg download and the disk, from a stated rule of thumb."""
    keys = {"vast_api_key": KEY}                      # NO Jasna licence: a transcode does not need one

    def setUp(self):
        super().setUp()
        self.vast.offers = market()
        cj._ffmpeg_cache.update(at=0.0, data=None)

    def offer(self, gpu="RTX 3080", **more):
        return parse_offer(raw_offer(1, gpu, 0.30, inet_down=900.0, storage_cost=15.0, **more))

    def test_no_licence_is_needed(self):
        q = cj.build_quote(self.cl, SCENE, None, plan=cj.Plan(op="transcode"), release_gb=0.15)
        self.assertIsNone(q["refusal"], q["refusal"])
        self.assertEqual((q["op"], q["preset"]), ("transcode", None))
        q = cj.build_quote(self.cl, SCENE, "detail", release_gb=4.6)
        self.assertEqual(q["refusal"]["code"], "no_licence")                   # Jasna still does

    def test_the_estimate_counts_both_transfers_storage_and_the_download(self):
        est = cj.estimate_job(SCENE, None, self.offer(), 0.15, cj.Plan(op="transcode"))
        self.assertGreater(est.up_min, 0)
        self.assertGreater(est.down_min, 0)
        self.assertGreater(est.up_min, est.down_min)                           # the whole file up, a smaller result back
        self.assertAlmostEqual(est.up_min, 0.5 * 8000 / cj.HOME_UPLINK_MBPS / 60, places=2)
        self.assertAlmostEqual(est.up_min + est.down_min, est.transfer_min, places=6)
        b = est.cost
        self.assertAlmostEqual(b.gb_down, 0.5 + 0.15 + cj.APT_GB, places=3)    # the source + ffmpeg + apt
        self.assertAlmostEqual(b.gb_up, 0.5 * cj.TRANSCODE_RESULT_SHARE, places=3)
        self.assertGreater(b.storage, 0)
        self.assertEqual(est.disk_gb, cj.disk_needed(SCENE["size"], cj.TRANSCODE_RESULT_SHARE, "transcode"))
        # time = setup + upload + encode + download, with the stated 8x real time (10 min video: 1.25 min + 20 % padding)
        self.assertAlmostEqual(est.processing_min, 600 / 60 / cj.NVENC_X_REALTIME_1080P * cj.SAFETY, places=3)
        self.assertAlmostEqual(est.setup_min + est.transfer_min + est.processing_min, est.hours * 60, places=3)
        self.assertTrue(any("rule of thumb" in a for a in est.assumptions))
        self.assertTrue(any("goes up from home" in a and "comes back" in a for a in est.assumptions))
        d = est.as_dict()
        self.assertEqual((d["up_min"], d["down_min"]), (round(est.up_min, 1), round(est.down_min, 1)))

    def test_a_smaller_target_height_shrinks_the_result_and_taller_video_is_slower(self):
        keep = cj.estimate_job(SCENE, None, self.offer(), 0.15, cj.Plan(op="transcode"))
        small = cj.estimate_job(SCENE, None, self.offer(), 0.15, cj.Plan(op="transcode", transcode_height=540))
        self.assertLess(small.down_min, keep.down_min)
        uhd = dict(SCENE, height=2160, width=3840)
        self.assertGreater(cj.estimate_job(uhd, None, self.offer(), 0.15, cj.Plan(op="transcode")).processing_min,
                           keep.processing_min * 3)

    def test_the_speed_does_not_depend_on_the_card_but_the_price_does(self):
        cheap = cj.estimate_job(SCENE, None, self.offer("RTX 3080"), 0.15, cj.Plan(op="transcode"))
        fast = cj.estimate_job(SCENE, None, self.offer("RTX 5090", vram_mb=32607), 0.15, cj.Plan(op="transcode"))
        self.assertEqual(cheap.processing_min, fast.processing_min)

    def test_a_cards_outside_jasnas_table_is_placed_by_its_compute(self):
        o = self.offer("RTX 3080")
        base, published = cj.gpu_min_per_min(o)
        self.assertFalse(published)
        self.assertGreaterEqual(base, cj.MIN_PER_MIN_RANGE[0])
        self.assertLessEqual(base, cj.MIN_PER_MIN_RANGE[1])
        self.assertEqual(cj.gpu_min_per_min(self.offer("RTX 4090"))[0], 1.5)
        est = cj.estimate_job(SCENE, "detail", o, 4.6)
        self.assertIn("not in Jasna's published speed table", est.assumptions[0])

    def test_the_quote_dict_carries_everything_the_run_bar_shows(self):
        q = cj.build_quote(self.cl, SCENE, None, plan=cj.Plan(op="transcode"), release_gb=0.15)
        for key in ("up_min", "down_min", "setup_min", "processing_min", "transfer_min", "total_min", "disk_gb"):
            self.assertIn(key, q)
        self.assertGreater(q["breakdown"]["gb_down"], 0.5)
        self.assertGreater(q["breakdown"]["gb_up"], 0)

    def test_a_sum_of_scenes_adds_the_transfer_minutes(self):
        one = cj.estimate_job(SCENE, None, self.offer(), 0.15, cj.Plan(op="transcode"))
        two = cj.sum_costs([one, one])
        self.assertAlmostEqual(two.up_min, 2 * one.up_min)
        self.assertAlmostEqual(two.down_min, 2 * one.down_min)


class OffersGreyedTest(cf.CloudCase):
    def setUp(self):
        super().setUp()
        self.vast.offers = market(extra=[
            (21, "RTX 3080", 0.15, {"vram_mb": 10240, "datacenter": False}),
            (22, "RTX 3070", 0.10, {"vram_mb": 8192, "datacenter": False}),
            (23, "A100", 0.90, {"vram_mb": 81920}),
            (24, "L4", 0.30, {"vram_mb": 23034}),
            (25, "RTX 3050", 0.05, {"vram_mb": 1024, "datacenter": False})])

    def why(self, op):
        status, payload = cloud_api.offers(self.cl, {"op": op, "limit": "50"})
        self.assertEqual(status, 200)
        return {o["gpu"]: (o["compatible"], o["incompatible_reason"]) for o in payload["offers"]}

    def test_transcode_says_what_is_missing(self):
        rows = self.why("transcode")
        for gpu in ("RTX 3080", "RTX 3070", "RTX 5090", "L4"):
            self.assertEqual(rows[gpu], (True, None), gpu)
        self.assertEqual(rows["A100"], (False, "No video encoder"))
        self.assertEqual(rows["RTX 3050"], (False, "Needs 2 GB (this has 1)"))

    def test_jasna_ops_say_it_too(self):
        for op in ("decensor", "upscale", "decensor+upscale"):
            rows = self.why(op)
            self.assertEqual(rows["RTX 3080"], (True, None), op)                   # the home card's twin works
            self.assertEqual(rows["RTX 3070"], (False, "Needs 10 GB (this has 8.2)"), op)
            self.assertEqual(rows["A100"], (False, "Needs an RTX card"), op)
            self.assertEqual(rows["L4"], (False, "Needs an RTX card"), op)

    def test_the_transcode_query_fields_pass_through(self):
        status, payload = cloud_api.offers(self.cl, {"op": "transcode", "transcode_height": "720",
                                                      "transcode_quality": "26"}, SCENE)
        self.assertEqual(status, 200)
        self.assertEqual(payload["for"]["op"], "transcode")
        self.assertIsNone(payload["for"]["preset"])
        self.assertEqual(payload["for"]["label"], "Transcode to 720p")
        self.assertTrue(all(o["estimate"]["up_min"] > 0 for o in payload["offers"] if o["compatible"]))


class RunTest(cf.CloudCase):
    """A transcode on the rented machine, start to finish, on a scripted fake box."""
    keys = {"vast_api_key": KEY}

    def setUp(self):
        super().setUp()
        cj._ffmpeg_cache.update(at=0.0, data=None)
        self.vast.offers = market(extra=[(21, "RTX 3080", 0.15, {"vram_mb": 10240})])
        self.working = list(cj.TRANSCODE_ENCODERS)
        self.scripts, self.cmds, self.stdins = [], [], []
        self.install_code = 0
        self.run_code = 0
        self.tp.handlers.insert(0, (re.compile(r"."), self.handle))
        self.src = os.path.join(self.dir, "My Scene [1080p].mkv")
        with open(self.src, "wb") as fh:
            fh.write(b"x" * 1000)
        self.out = os.path.join(self.dir, "out")
        os.makedirs(self.out)
        self.status, self.logs, self.stamps = [], [], []

    def handle(self, tag, cmd, kw):
        script = script_of(cmd)
        self.stdins.append(kw.get("stdin_data"))
        if "nvidia-smi" in script:
            return ssh.RunResult(0, ["NVIDIA GeForce RTX 3080, 10240 MiB, 580.95.05"])
        if "ffmpeg.tar.xz" in script:
            self.scripts.append(script)
            return ssh.RunResult(self.install_code, ["[stashify] step: installed"] if not self.install_code
                                 else ["E: boom"])
        if "STASHIFY_ENCODER" in script:
            self.scripts.append(script)
            for enc in cj.TRANSCODE_ENCODERS:
                if enc in self.working:
                    return ssh.RunResult(0, ["STASHIFY_ENCODER=" + enc])
            return ssh.RunResult(0, ["STASHIFY_ENCODER=none"])
        if "transcode_cli.py" in script:
            self.cmds.append(script)
            self.tp.simulate(75, kw["cancel"])
            for line in ("transcode: 10%| |Processed: 00:08 (240f) | Remaining: 01:10 | Speed: 30.0f/s 1.00x",
                         "transcode: 60%| |Processed: 00:36 (1080f) | Remaining: 00:30 | Speed: 30.0f/s 1.00x",
                         "transcode: 100%| |Processed: 01:00 (1800f) | Remaining: 00:00 | Speed: 30.0f/s 1.00x"):
                if kw["on_line"]:
                    kw["on_line"](line)
            return ssh.RunResult(self.run_code, ["done"] if not self.run_code else ["ffmpeg exited 1"])
        if "stat -c %s" in script:
            return ssh.RunResult(0, ["16"])
        return ssh.RunResult(0, [])

    def cfg(self, **body):
        plan = cj.plan_from_body(dict(body, op="transcode"))
        est = cj.build_quote(self.cl, SCENE, None, plan=plan, release_gb=0.154)["est_usd"]
        return {"cloud": {"preset": None, "scene": SCENE, "plan": plan.as_dict(), "est_usd": est,
                          "confirm_usd": est, "job_id": "job1"}}

    def run_job(self, cfg=None, **body):
        return cj.run_decensor(cfg or self.cfg(**body), self.src, self.out, cl=self.cl, log_cb=self.logs.append,
                               status_cb=self.status.append, stamp=lambda **f: self.stamps.append(f),
                               ffmpeg_fn=lambda: FFMPEG)

    def test_the_remote_command_is_transcode_cli_with_the_requests_fields(self):
        produced = self.run_job(transcode_height=720, transcode_quality=28)
        (cmd,) = self.cmds
        self.assertEqual(
            cmd, "python3 /root/transcode_cli.py --input /root/in/source.mkv --output-dir /root/out "
                 "--ffmpeg /root/ffmpeg/bin/ffmpeg --ffprobe /root/ffmpeg/bin/ffprobe --encoder hevc_nvenc "
                 "--quality 28 --container mp4 --height 720 "
                 "&& mv -f /root/out/source.transcoded.mp4 /root/out/result.mp4")
        self.assertEqual(os.path.basename(produced), "My_Scene_1080p__transcoded.mp4")
        self.assertEqual(self.vast.live(), [])
        (row,) = self.ledger()
        self.assertEqual((row["status"], row["op"], row["preset"]), ("ok", "transcode", None))

    def test_the_defaults_keep_the_height_and_use_quality_24(self):
        self.run_job()
        (cmd,) = self.cmds
        self.assertIn("--quality 24 --container mp4 &&", cmd)
        self.assertNotIn("--height", cmd)

    def test_the_script_and_ffmpeg_reach_the_box_and_no_jasna_is_installed(self):
        self.run_job()
        puts = {remote: local for _tag, local, remote in self.tp.puts}
        self.assertTrue(puts["/root/transcode_cli.py"].endswith("transcode_cli.py"))
        self.assertTrue(os.path.isfile(puts["/root/transcode_cli.py"]))
        self.assertEqual(puts["/root/in/source.mkv"], self.src)
        install = self.scripts[0]
        self.assertIn(FFMPEG["url"], install)
        self.assertIn("python3-minimal", install)
        everything = "\n".join(script_of(c) for c in self.tp.commands())
        for jasna in ("jasna", "zstd", "JASNA_LICENSE_CODE", "--online"):
            self.assertNotIn(jasna, everything)

    def test_no_licence_is_injected(self):
        self.assertFalse(self.cl.settings.get("jasna_license"))
        self.run_job()
        self.assertTrue(all(s is None for s in self.stdins), self.stdins)           # nothing arrived on stdin
        self.assertNotIn("Activating Jasna", [s["message"] for s in self.status])

    def test_progress_and_phases_are_the_transcodes(self):
        self.run_job()
        messages = [s["message"] for s in self.status]
        self.assertIn("Transcoding in the cloud", messages)
        self.assertIn("Starting the transcode (hevc_nvenc)", messages)
        self.assertIn("Looking up the ffmpeg download", messages)
        phases = [s["cloud_phase"] for s in self.stamps if "cloud_phase" in s]
        self.assertEqual([p for i, p in enumerate(phases) if p not in phases[:i]],
                         ["boot", "upload", "run", "download", "cleanup"])
        last = [s for s in self.stamps if "cloud_usd" in s][-1]
        self.assertEqual((last["engine"], last["cloud_gpu"]), ("ffmpeg", "RTX 3080"))

    def test_the_encoder_chain_is_hevc_nvenc_then_h264_nvenc_then_x265(self):
        script = cj.transcode_encoder_script()
        self.assertIn("for enc in hevc_nvenc h264_nvenc libx265; do", script)
        self.assertIn("-f lavfi -i testsrc2=duration=1:size=1280x720 -c:v $enc -f null -", script)
        self.working = ["h264_nvenc", "libx265"]
        self.run_job()
        self.assertIn("--encoder h264_nvenc", self.cmds[-1])
        self.working = ["libx265"]
        self.run_job()
        self.assertIn("--encoder libx265", self.cmds[-1])
        self.assertTrue(any("encoding on its CPU" in line for line in self.logs))

    def test_a_machine_with_no_encoder_is_rejected_and_nothing_stays_rented(self):
        self.working = []
        with self.assertRaises(hireling.CloudError):
            self.run_job()
        self.assertEqual(self.cmds, [])
        self.assertEqual(self.vast.live(), [])

    def test_a_failed_install_rejects_the_machine(self):
        self.install_code = 1
        with self.assertRaises(hireling.CloudError):
            self.run_job()
        self.assertEqual(self.vast.live(), [])

    def test_a_failed_transcode_is_a_job_failure_and_the_box_is_destroyed(self):
        self.run_code = 1
        with self.assertRaises(cj.CloudJobFailed) as cm:
            self.run_job()
        self.assertIn("The transcode failed on the rented machine", str(cm.exception))
        self.assertEqual(self.vast.live(), [])

    def test_the_auto_choice_rents_a_3080_when_it_is_the_cheapest(self):
        self.run_job()
        rented = [re.search(r"/asks/(\d+)/", p).group(1) for m, p, _b in self.vast.calls
                  if m == "PUT" and "/asks/" in p]
        self.assertEqual(rented, ["21"])                                         # RTX 3080, $0.15/h: it was never on the old list

    def test_the_lookup_failing_rents_nothing(self):
        def boom():
            raise cj.CloudJobFailed("could not look up the ffmpeg download on GitHub: offline")
        with self.assertRaises(cj.CloudJobFailed):
            cj.run_decensor(self.cfg(), self.src, self.out, cl=self.cl, ffmpeg_fn=boom)
        self.assertEqual(self.vast.count("PUT", "/asks/"), 0)
        self.assertEqual(self.tp.runs, [])


class CoreTest(unittest.TestCase):
    def test_the_transcode_backend_goes_to_the_cloud_when_the_job_is_paid(self):
        import core
        from unittest import mock
        with mock.patch.object(core, "_run_cloud", return_value="x.mp4") as run, \
                mock.patch.object(core, "_runner_dispatch") as home:
            self.assertEqual(core.backend_transcode({"cloud": {"plan": {}}}, "in", "out", None, None, None), "x.mp4")
            self.assertEqual(run.call_count, 1)
            home.assert_not_called()
            core.backend_transcode({}, "in", "out")
            self.assertEqual(home.call_args.args[3], "transcode")


if __name__ == "__main__":
    unittest.main()
