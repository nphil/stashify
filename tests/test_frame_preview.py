"""Preview generation, timestamp alignment and serving; no GPU or runner service."""
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import core
import server
from winrunner import frame_preview as fp
from winrunner.preview import JasnaFrames


class PreviewCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.pairs = self.root / "pairs"


class PreviewStorageTest(PreviewCase):
    def test_http_pins_both_images_to_one_complete_generation(self):
        jid = "abcd"
        directory = self.pairs / jid
        first, second = "00000000000000000001-12345678", "00000000000000000002-12345678"
        incomplete = directory / first
        incomplete.mkdir(parents=True)
        (incomplete / "after.jpg").write_bytes(b"must not be served")

        def get(side, pair):
            h = server.Handler.__new__(server.Handler)
            h.path = f"/api/jobs/{jid}/preview/{side}.jpg?pair={pair}"
            h.wfile = io.BytesIO()
            h.send_response = mock.Mock()
            h.send_header = mock.Mock()
            h.end_headers = mock.Mock()
            h._cors = mock.Mock()
            h._send = lambda status, _body: h.send_response(status)
            h.do_GET()
            return h.send_response.call_args.args[0], h.wfile.getvalue()

        with mock.patch.object(server, "PREVIEW_DIR", str(self.pairs)):
            self.assertFalse(server.public({"id": jid})["preview"])
            self.assertEqual(get("after", first)[0], 404)
            shutil.rmtree(incomplete)
            fp._publish(directory, first, b"original 1", b"processed 1")
            self.assertEqual(server.public({"id": jid})["preview_id"], first)
            self.assertEqual(get("before", first), (200, b"original 1"))
            fp._publish(directory, second, b"original 2", b"processed 2")
            self.assertEqual(server.public({"id": jid})["preview_id"], second)
            self.assertEqual(get("after", first), (200, b"processed 1"))
            self.assertEqual(get("after", second), (200, b"processed 2"))
            self.assertEqual(get("before", "../../etc/passwd")[0], 404)
            self.assertEqual(get("after", "")[0], 404)

    def test_final_pair_is_mirrored_even_when_runner_finishes_before_first_poll(self):
        output_dir = self.root / "output"
        output_dir.mkdir()
        pair = "00000000000000000001-abcdef00"

        def submit(_url, *, headers, json, timeout):
            scratch = Path(json["output_dir"])
            (scratch / "finished.mp4").write_bytes(b"result")
            fp._publish(scratch / ".preview", pair, b"original", b"processed")
            return mock.Mock(**{"json.return_value": {"id": "a1"}})

        cfg = {"runnerScratch": str(self.root / "scratch"), "_preview_dir": str(self.pairs)}
        runner = {"name": "pc", "url": "http://runner", "token": "", "engine": "jasna"}
        with mock.patch.object(core, "http", return_value=mock.Mock(post=submit)), \
                mock.patch.object(core, "resolve_runner", return_value=runner), \
                mock.patch.object(core.runner_stream, "ensure", return_value=None), \
                mock.patch.object(core, "_poll_json", side_effect=[{"state": "done"}, {}]), \
                mock.patch.object(core, "check_cancel"), mock.patch.object(core, "_chown_like"):
            core._runner_dispatch(cfg, "source.mp4", output_dir, "decensor")
        self.assertEqual(list((self.root / "scratch").iterdir()), [])
        self.assertEqual(fp.latest_pair(self.pairs), pair)
        self.assertEqual(fp.pair_path(self.pairs, "after", pair).read_bytes(), b"processed")

    def test_failed_before_frame_does_not_replace_a_pair_and_durationless_output_advances(self):
        source = self.root / "source.mp4"
        output = self.root / "output.mp4"
        watch = fp.FramePreview(source, self.pairs, "ffmpeg", "ffprobe")

        def get_frame(_ffmpeg, path, _media, time):
            return round(time), b"frame"

        with mock.patch.object(fp, "_probe", return_value=(0.0, 0.0, None)), \
                mock.patch.object(fp, "_grab", side_effect=get_frame) as grab:
            output.write_bytes(b"growing")
            self.assertTrue(watch.capture(output))
            first = fp.latest_pair(self.pairs)
            output.write_bytes(b"growing further")
            grab.side_effect = [(1.0, b"new after"), None]
            self.assertFalse(watch.capture(output))
            self.assertEqual(fp.latest_pair(self.pairs), first)
            output.write_bytes(b"even longer")
            grab.side_effect = get_frame
            self.assertTrue(watch.capture(output))
            self.assertEqual(grab.call_args_list[-2].args[-1], 1.0)
            self.assertNotEqual(fp.latest_pair(self.pairs), first)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg binaries not available")
class FrameCaptureTest(PreviewCase):
    def ffmpeg(self, *args):
        return subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-filter_threads", "1",
                               "-filter_complex_threads", "1", *map(str, args)],
                              capture_output=True, check=True, timeout=10).stdout

    def setUp(self):
        super().setUp()
        self.source = self.root / "original.mp4"
        colors = ";".join(f"color={color}:s=64x40:r=10:d=1[{i}]" for i, color in enumerate(("red", "lime", "blue")))
        self.ffmpeg("-f", "lavfi", "-i", colors + ";[0][1][2]concat=n=3:v=1:a=0",
                    "-c:v", "mpeg4", "-g", 1, self.source)
        self.watch = fp.FramePreview(self.source, self.pairs, "ffmpeg", "ffprobe")

    def pixel(self, side):
        pair = fp.latest_pair(self.pairs)
        jpeg = fp.pair_path(self.pairs, side, pair)
        return tuple(self.ffmpeg("-i", jpeg, "-vf", "scale=1:1", "-pix_fmt", "rgb24",
                                 "-frames:v", 1, "-f", "rawvideo", "pipe:1"))

    def assert_green_to_magenta(self):
        rb, gb, bb = self.pixel("before")
        ra, ga, ba = self.pixel("after")
        self.assertGreater(gb, max(rb, bb) + 150)   # the green middle second, not a poster frame
        self.assertLess(ga + 100, min(ra, ba))     # the real processed (negated) green frame

    def test_direct_fragmented_export_uses_real_frames_in_the_same_moment(self):
        output = self.root / "direct.mp4"
        output.write_bytes(b"not a readable frame yet")
        self.assertFalse(self.watch.capture(output))
        self.assertIsNone(fp.latest_pair(self.pairs))
        self.ffmpeg("-i", self.source, "-vf", "negate", "-c:v", "mpeg4", "-g", 1,
                    "-movflags", "frag_keyframe+empty_moov+default_base_moof", output)
        self.assertTrue(self.watch.capture(output))
        self.assert_green_to_magenta()

    def test_first_restored_span_can_publish_while_smart_output_is_still_growing(self):
        output = self.root / "smart.mp4"
        watcher = JasnaFrames(self.watch, output)
        render = self.root / ".smart.jasna-resume" / "render"
        fragments = render / "segments"
        fragments.mkdir(parents=True)
        (render / "job.json").write_text(json.dumps({"inputs": {"latents": {"segments": [
            {"start": 1.1, "end": 1.9, "restoration": {"model": "ltx"}},
        ]}}}), encoding="utf-8")
        self.ffmpeg("-i", self.source, "-t", "2", "-vf", "negate", "-c:v", "mpeg4", "-g", 1,
                    fragments / "0000-partial.nut")
        self.assertTrue(watcher.capture())
        self.assert_green_to_magenta()

    def test_auto_split_uses_a_fragment_only_after_its_source_offset_is_known(self):
        output = self.root / "smart.mp4"
        watcher = JasnaFrames(self.watch, output)
        render = self.root / ".smart.jasna-resume" / "render"
        fragments = render / "segments"
        fragments.mkdir(parents=True)
        (render / "job.json").write_text(json.dumps({"inputs": {"latents": {"segments": [
            {"start": 1.1, "end": 1.9, "restoration": {"model": "ltx"}},
        ]}}}), encoding="utf-8")
        self.ffmpeg("-i", self.source, "-t", "1", "-c:v", "mpeg4", "-g", 1, fragments / "0000-raw.nut")
        self.ffmpeg("-ss", "1", "-i", self.source, "-t", "1", "-vf", "negate", "-c:v", "mpeg4", "-g", 1,
                    fragments / "0001-raw.nut")
        self.assertFalse(watcher.capture())         # the only known offset is the first, copied span
        self.assertIsNone(fp.latest_pair(self.pairs))
        (fragments / "fragments.ffconcat").write_text(
            "ffconcat version 1.0\nfile '0000.ts'\nduration 1\nfile '0001.ts'\nduration 1\n", encoding="utf-8")
        self.assertTrue(watcher.capture())
        self.assert_green_to_magenta()


if __name__ == "__main__":
    unittest.main()
