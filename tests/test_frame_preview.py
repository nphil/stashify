"""Preview generation, timestamp alignment and serving; no GPU or runner service."""
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import core
import server
from winrunner import frame_preview as fp
from winrunner.preview import JasnaFrames, SegmentPreview


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

    def test_final_runner_status_delivers_announced_segments_and_sample(self):
        output_dir = self.root / "output"
        output_dir.mkdir()
        segment = {"n": 0, "start": 1.0, "end": 2.0, "dur": 1.0}

        def submit(_url, *, headers, json, timeout):
            (Path(json["output_dir"]) / "finished.mp4").write_bytes(b"result")
            return mock.Mock(**{"json.return_value": {"id": "a1"}})

        def clip(url, **kwargs):
            response = mock.MagicMock(status_code=200)
            response.__enter__.return_value = response
            response.iter_content.return_value = [url.rsplit("/", 1)[-1].encode()]
            return response

        cfg = {"runnerScratch": str(self.root / "scratch"), "_preview_dir": str(self.pairs),
               "_finish_preview": server._mirror_segments}
        runner = {"name": "pc", "url": "http://runner", "token": "secret", "engine": "jasna"}
        status = {"state": "done", "segments": [segment], "sample": True}
        with mock.patch.object(core, "http", return_value=mock.Mock(post=submit, get=clip)), \
                mock.patch.object(core, "resolve_runner", return_value=runner), \
                mock.patch.object(core.runner_stream, "ensure", return_value=None), \
                mock.patch.object(core, "_poll_json", side_effect=[status, {}]), \
                mock.patch.object(core, "check_cancel"), mock.patch.object(core, "_chown_like"), \
                mock.patch.object(server, "_segments", {}):
            core._runner_dispatch(cfg, "source.mp4", output_dir, "decensor")
            self.assertEqual(server.job_segments("pairs"), [segment])
        self.assertEqual((self.pairs / "seg0_before.mp4").read_bytes(), b"before.mp4")
        self.assertEqual((self.pairs / "seg0_after.mp4").read_bytes(), b"after.mp4")
        self.assertEqual((self.pairs / "sample.mp4").read_bytes(), b"sample.mp4")

    def test_grab_does_not_use_removed_vsync_option_or_short_probe_timeout(self):
        with mock.patch.object(fp, "_run", return_value=mock.Mock(
                stderr=b"[Parsed_showinfo] n: 0 pts: 1000000", stdout=b"\xff\xd8data\xff\xd9")) as run:
            self.assertEqual(fp._grab("ffmpeg", "output.mp4", (0.0, 0.0, 12.0), 1.0),
                             (1.0, b"\xff\xd8data\xff\xd9"))
            args = run.call_args.args[0]
            self.assertNotIn("-vsync", args)
            index = args.index("-fps_mode")
            self.assertEqual(args[index + 1], "passthrough")
            self.assertGreater(run.call_args.kwargs["timeout"], 8)

    def test_failed_before_frame_does_not_replace_a_pair_and_durationless_output_advances(self):
        source = self.root / "source.mp4"
        output = self.root / "output.mp4"
        watch = fp.FramePreview(source, self.pairs, "ffmpeg", "ffprobe")

        def get_frame(_ffmpeg, path, _media, time, *, stop=None):
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
        self.ffmpeg("-ss", "1", "-i", self.source, "-t", "1", "-vf", "negate", "-c:v", "mpeg4", "-g", 1,
                    fragments / "0001-partial.nut")
        self.assertFalse(watcher.capture())   # later LTX spans may appear out of order
        self.assertIsNone(fp.latest_pair(self.pairs))
        self.ffmpeg("-i", self.source, "-t", "1", "-c:v", "mpeg4", "-g", 1, fragments / "0000-raw.nut")
        self.assertTrue(watcher.capture())    # no late assembly manifest needed
        self.assert_green_to_magenta()
        (fragments / "0001-partial.nut").rename(fragments / "0001-raw.nut")
        self.assertTrue(watcher.capture())
        self.assertEqual(watcher.completed, ((fragments / "0001-raw.nut", 1.0, 2.0),))

        stop = threading.Event()
        clips = self.root / "clips"
        segment = SegmentPreview(jasna=watcher, src=self.source, prev_dir=clips,
                                 ffmpeg="ffmpeg", ffprobe="ffprobe", encoder="mpeg4", height=40,
                                 on_update=lambda _: stop.set())
        worker = threading.Thread(target=segment.run, args=(stop, 0.01), daemon=True)
        worker.start()
        self.assertTrue(stop.wait(5))
        worker.join()
        self.assertEqual(segment.segments, [{"n": 0, "start": 1.1, "end": 1.9, "dur": 0.8}])
        sample = segment.finish()
        self.assertIsNotNone(sample)
        self.assert_green_to_magenta()


if __name__ == "__main__":
    unittest.main()
