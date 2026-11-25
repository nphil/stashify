"""The NAS (P40) runner advertises transcode and builds sane ffmpeg encoder choices."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "winrunner"))
import lada_runner as lr          # noqa: E402
import transcode_cli as tc        # noqa: E402


class NasTranscode(unittest.TestCase):
    def test_advertised(self):
        self.assertIn("transcode", lr.RUNNER_OPS)
        self.assertEqual(lr.RUNNER_ENGINES["transcode"], "ffmpeg")
        h = lr.health_payload()
        self.assertIn("transcode", h["ops"])
        self.assertEqual(h["engines"]["transcode"], "ffmpeg")

    def test_encoder_preference(self):
        both = " V....D hevc_nvenc  NVIDIA\n V....D h264_nvenc x\n V....D libx265 y"
        self.assertEqual(lr.pick_transcode_encoder(both), "hevc_nvenc")
        self.assertEqual(lr.pick_transcode_encoder(" V h264_nvenc\n V libx265"), "h264_nvenc")
        self.assertEqual(lr.pick_transcode_encoder(" V libx265"), "libx265")
        self.assertEqual(lr.pick_transcode_encoder(""), "libx265")

    def test_argv(self):
        for enc in ("hevc_nvenc", "libx265"):
            a = lr._transcode_argv({"transcode_encoder": enc, "height": "720", "quality": "24"},
                                   "/in/a.mkv", "/out", "libx264")
            self.assertEqual(a[a.index("--encoder") + 1], enc)
            self.assertEqual(a[a.index("--height") + 1], "720")
            self.assertEqual(a[a.index("--quality") + 1], "24")
            self.assertIn(enc, tc.ENCODER_ARGS)
            self.assertNotIn("--codec", a)   # empty fields are not forwarded
        self.assertIn("-cq", tc.ENCODER_ARGS["hevc_nvenc"][0])
        self.assertIn("-crf", tc.ENCODER_ARGS["libx265"][0])


if __name__ == "__main__":
    unittest.main()
