"""The one live status of a job: the runner's progress lines, the coordinator's whole-job progress
(monotonic, weighted by time across passes), the log that keeps one live line, and the quiet,
retrying poll of the runner. No network, no real runner."""

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "winrunner"))

_scratch = tempfile.mkdtemp(prefix="stashify-jobstatus-")
atexit.register(shutil.rmtree, _scratch, ignore_errors=True)
os.environ["LOCALAPPDATA"] = _scratch          # the runner writes its log under here when imported

import core                          # noqa: E402
import jobprogress                   # noqa: E402
import runner                        # noqa: E402
import server                        # noqa: E402
import test_dashboard as td          # noqa: E402,F401  (installs a stand-in for `requests` when it is missing)

requests = sys.modules["requests"]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def tracker():
    clk = Clock()
    return jobprogress.Tracker(0.0, 0.97, clock=clk), clk


def job(**kw):
    base = {"stage": "decensor", "progress": 0.0, "message": "x", "indeterminate": False}
    base.update(kw)
    return base


class RunnerLinesTest(unittest.TestCase):
    LTX = "decensor: 20%| |Remaining: 1:39:22 | Speed: 79.52 f/s | LTX step 2 of 4 (encode)"

    def test_an_ltx_line_names_the_step_and_says_it_is_an_estimate(self):
        f = runner.parse_progress(self.LTX)
        self.assertEqual((f["phase"], f["step"], f["steps"], f["detail"]), ("Restoring in high detail", 2, 4, "encode"))
        self.assertEqual((f["progress"], f["fps"], f["eta"], f["estimate"]), (0.2, 79.52, 5962, True))

    def test_a_plain_line_clears_the_step_and_names_the_work(self):
        f = runner.parse_progress("upscaling: 42%| |Processed: 00:12 (1234f) | Remaining: 01:23 | Speed: 12.3f/s")
        self.assertEqual((f["phase"], f["step"], f["steps"], f["estimate"]), ("Upscaling", None, None, False))
        self.assertEqual(runner.parse_progress("decensor: 9%| |Processed: 00:12 (10f)")["phase"], "Restoring mosaics")

    def test_a_silent_phase_gets_a_phase_but_a_milestone_does_not_flicker_the_title(self):
        self.assertEqual(runner.parse_narration("[no tqdm]: loading models - 30s")["phase"], "Loading models")
        self.assertNotIn("phase", runner.parse_narration("decensor: restoring mosaic clip #4"))
        self.assertEqual(runner.parse_narration("decensor: muxing final output")["phase"], "Finishing the video")

    def test_silence_is_reported_as_seconds_and_only_while_running(self):
        jid = "t1"
        job_ = {"id": jid, "state": "running", "_active": 100.0}
        with mock.patch.object(runner.time, "time", return_value=190.0):
            self.assertEqual(runner.public(job_)["idle_s"], 90)
            self.assertNotIn("idle_s", runner.public(dict(job_, paused=True)))
            self.assertNotIn("idle_s", runner.public(dict(job_, state="done")))


class TrackerTest(unittest.TestCase):
    def test_progress_never_goes_backwards(self):
        t, clk = tracker()
        seen = []
        for p in (0.1, 0.4, 0.3, 0.5, 0.2, 0.9):
            clk.t += 5
            seen.append(t.update(job(progress=p))[0])
        self.assertEqual(seen, sorted(seen))
        self.assertLessEqual(max(seen), 0.97)

    def test_the_early_stages_sit_in_their_bands(self):
        t, clk = tracker()
        copy = t.update(job(stage="copy", progress=1.0, phase="Copying video to the PC"))
        self.assertAlmostEqual(copy[0], 0.06 * 0.97)
        self.assertIsNone(copy[2]["eta"])        # a copy's own time left is not the job's
        self.assertEqual(copy[2]["phase"], "Copying video to the PC")

    def test_two_passes_are_weighted_by_the_time_each_took_not_by_halves(self):
        t, clk = tracker()
        # decensor takes 2 h; halfway its ETA says 1 h left
        t.update(job(passes=2, pass_no=1, progress=0.0, pass_progress=0.0))
        clk.t += 3600
        f1, _, s1 = t.update(job(passes=2, pass_no=1, progress=0.25, pass_progress=0.5, eta=3600))
        # equal shares would say 25%; by time the first pass is 2 h of an estimated 3 h
        self.assertTrue(s1["estimate"])
        self.assertAlmostEqual(s1["eta"], 3600 + 0.5 * 7200)
        clk.t += 3600
        f2, _, s2 = t.update(job(passes=2, pass_no=1, progress=0.5, pass_progress=1.0, eta=0))
        self.assertGreater(f2, f1)
        self.assertEqual((s2["pass_no"], s2["passes"]), (1, 2))
        # the upscale begins: the bar holds where it was, then moves on its own speed
        clk.t += 10
        f3, _, s3 = t.update(job(stage="upscale", passes=2, pass_no=2, progress=0.5, pass_progress=0.0))
        self.assertGreaterEqual(f3, f2)
        self.assertEqual(s3["pass_no"], 2)
        clk.t += 600
        f4, _, s4 = t.update(job(stage="upscale", passes=2, pass_no=2, progress=0.75, pass_progress=0.5, eta=600))
        self.assertGreater(f4, f3)
        self.assertFalse(s4["estimate"])         # nothing left to guess: the last pass is measured
        self.assertEqual(s4["eta"], 600)
        clk.t += 600
        f5, _, s5 = t.update(job(stage="upscale", passes=2, pass_no=2, progress=1.0, pass_progress=1.0, eta=0))
        self.assertAlmostEqual(f5, 0.97, places=2)
        # the measured share of the decensor is far more than half of the whole
        self.assertGreater(f2, 0.5 * 0.88 * 0.97)

    def test_a_pass_with_nothing_measured_falls_back_to_equal_shares_and_says_estimate(self):
        t, clk = tracker()
        f, _, s = t.update(job(passes=2, pass_no=1, progress=0.1, pass_progress=0.2))
        self.assertTrue(s["estimate"])
        self.assertIsNone(s["eta"])

    def test_an_older_runner_still_maps_as_before(self):
        t, clk = tracker()
        f, msg, s = t.update({"stage": "decensor", "progress": 0.5, "message": "Restoring mosaics - 50%", "eta": 90})
        self.assertAlmostEqual(f, 0.97 * (0.12 + 0.88 * 0.5))
        self.assertEqual((msg, s["eta"], s["phase"], s["indeterminate"]), ("Restoring mosaics - 50%", 90, "Restoring mosaics", False))
        self.assertEqual((s["step"], s["pass_no"], s["estimate"]), (None, None, False))
        # frame/total only (lada)
        t2, _ = tracker()
        f2, _, s2 = t2.update({"stage": "upscale", "frame": 50, "total_frames": 100, "message": "Working"})
        self.assertAlmostEqual(f2, 0.97 * (0.12 + 0.88 * 0.5))
        self.assertFalse(s2["indeterminate"])

    def test_last_update_comes_from_runner_silence_else_from_change(self):
        t, clk = tracker()
        s = t.update(job(idle_s=12))[2]
        self.assertEqual(s["updated_at"], clk.t - 12)
        t, clk = tracker()                                  # an older runner: stamped when something changes
        first = t.update(job(progress=0.1))[2]["updated_at"]
        clk.t += 30
        self.assertEqual(t.update(job(progress=0.1))[2]["updated_at"], first)       # same numbers: still the old stamp
        clk.t += 1
        self.assertEqual(t.update(job(progress=0.2))[2]["updated_at"], clk.t)


class ProgressCallbackTest(unittest.TestCase):
    def setUp(self):
        self.id = "jobstatus-1"
        server._jobs[self.id] = {"id": self.id, "state": "running", "progress": 0.0}
        self.addCleanup(server._jobs.pop, self.id, None)
        self.addCleanup(server._job_logs.pop, self.id, None)

    def test_the_bar_holds_and_a_cleared_step_does_not_linger(self):
        cb = server.progress_cb(self.id)
        cb(0.5, "Restoring", {"phase": "Restoring in high detail", "step": 2, "steps": 4, "estimate": True})
        cb(0.4, "Next", {"phase": "Finishing the video", "step": None, "steps": None, "estimate": False})
        j = server._jobs[self.id]
        self.assertEqual((j["progress"], j["phase"], j["step"], j["estimate"]), (0.5, "Finishing the video", None, False))
        self.assertGreater(j["updated_at"], 0)

    def test_the_coordinators_own_message_becomes_the_phase(self):
        cb = server.progress_cb(self.id)
        cb(0.98, "Importing preview into Stash", {"stage": "import", "indeterminate": True})
        self.assertEqual(server._jobs[self.id]["phase"], "Importing preview into Stash")

    def test_silence_is_reported_for_a_running_local_job_only(self):
        job_ = {"id": self.id, "state": "running", "updated_at": 100.0, "stage": "decensor"}
        with mock.patch.object(server.time, "time", return_value=160.0):
            self.assertEqual(server.public(job_)["idle_s"], 60)
            self.assertNotIn("idle_s", server.public(dict(job_, paused=True)))
            self.assertNotIn("idle_s", server.public(dict(job_, cloud=True)))
            self.assertNotIn("idle_s", server.public(dict(job_, stage="import")))
            self.assertNotIn("idle_s", server.public(dict(job_, state="review_ready")))

    def test_progress_bar_lines_are_one_live_line_in_the_log(self):
        bar = "decensor: %d%%| |Remaining: 1:39:22 | Speed: 79.52 f/s | LTX step 2 of 4 (encode)"
        server.push_log(self.id, "$ run decensor", "proc")
        for i in range(1, 50):
            server.push_log(self.id, bar % i)
        server.push_log(self.id, "restored clip 4", "event")
        server.push_log(self.id, bar % 60)
        server.push_log(self.id, bar % 61)
        lines = server.job_log_since(self.id, 0)
        self.assertEqual([x["text"][:12] for x in lines], ["$ run decens", bar[:12] % 49, "restored cli", bar[:12] % 61])
        self.assertEqual([bool(x.get("live")) for x in lines], [False, True, False, True])
        self.assertTrue(lines[-1]["seq"] > lines[1]["seq"])


class PollTest(unittest.TestCase):
    def reply(self, body):
        return mock.Mock(json=lambda: body)

    def test_one_dropped_connect_is_retried_quietly(self):
        with mock.patch.object(requests, "get", side_effect=[requests.ConnectionError("x"), self.reply({"state": "running"})]) as get, \
                mock.patch.object(core.time, "sleep"):
            self.assertEqual(core._poll_json("http://pc/jobs/1", {}), {"state": "running"})
        self.assertEqual(get.call_count, 2)

    def test_two_in_a_row_raise(self):
        with mock.patch.object(requests, "get", side_effect=requests.Timeout("slow")) as get, \
                mock.patch.object(core.time, "sleep"):
            with self.assertRaises(requests.Timeout):
                core._poll_json("http://pc/jobs/1", {})
        self.assertEqual(get.call_count, 2)

    def dispatch(self, replies):
        """Run _runner_dispatch against scripted replies to its GETs; returns (warnings, result)."""
        out_dir = tempfile.mkdtemp(prefix="dispatch-", dir=_scratch)
        calls = iter(replies)

        def get(url, **kw):
            r = next(calls)
            if isinstance(r, Exception):
                raise r
            return self.reply(r)

        warnings = []
        log = mock.Mock(warning=warnings.append, info=lambda m: None, debug=lambda m: None, error=lambda m: None)
        runner_cfg = {"name": "pc", "url": "http://pc:8720", "token": "", "engine": "jasna"}
        seen = []
        with mock.patch.object(core, "resolve_runner", return_value=runner_cfg), \
                mock.patch.object(core, "log", log), \
                mock.patch.object(core, "newest_video", return_value=os.path.join(out_dir, "o.mp4")), \
                mock.patch.object(core, "_chown_like"), \
                mock.patch.object(core.shutil, "move"), \
                mock.patch.object(core.shutil, "rmtree"), \
                mock.patch.object(core.os, "makedirs"), \
                mock.patch.object(core.os, "chmod"), \
                mock.patch.object(core.time, "sleep"), \
                mock.patch.object(requests, "post", return_value=mock.Mock(json=lambda: {"id": "r1"}, raise_for_status=lambda: None)), \
                mock.patch.object(requests, "get", side_effect=get):
            core._runner_dispatch({"runnerScratch": out_dir}, "/in.mp4", out_dir, "decensor",
                                  status_cb=seen.append)
        return warnings, seen

    def test_a_flaky_link_stays_quiet_until_three_polls_in_a_row_fail(self):
        drop = requests.ConnectionError("dropped")
        ok = {"state": "running", "progress": 0.1}
        done = {"state": "done"}
        log_ok = {"cursor": 0, "lines": []}
        # polls 1-2 fail after their quick retry (4 drops), then it recovers: no warning at all
        warnings, seen = self.dispatch([drop, drop, drop, drop, ok, log_ok, done, log_ok])
        self.assertEqual(warnings, [])
        self.assertEqual([s["state"] for s in seen], ["running", "done"])   # state came only from real replies
        # three failed polls in a row: one warning
        warnings, _ = self.dispatch([drop] * 6 + [done, log_ok])
        self.assertEqual(len(warnings), 1)
        self.assertIn("3 in a row", warnings[0])


if __name__ == "__main__":
    unittest.main()
