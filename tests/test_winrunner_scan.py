"""Windows preview scanner engine selection without NumPy, TensorRT, or a GPU."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "winrunner_scan_for_test", Path(__file__).resolve().parents[1] / "winrunner" / "scan_cli.py")
scan = importlib.util.module_from_spec(spec)
with mock.patch.dict(sys.modules, numpy=mock.MagicMock()):
    spec.loader.exec_module(scan)

SHAPE = (4, 3, 768, 768)


class ScanScorerTest(unittest.TestCase):
    def setUp(self):
        self.models = Path(self.enterContext(tempfile.TemporaryDirectory())) / "Jasna [models]"
        self.models.mkdir()
        self.args = SimpleNamespace(provider="trt", engine="", onnx=str(self.models / "rfdetr-v6.onnx"))
        self.engines = {}
        self.torch = mock.Mock()
        self.torch.empty.return_value.data_ptr.return_value = 42
        self.trt = mock.Mock(__version__="test")
        self.deserialize = self.trt.Runtime.return_value.deserialize_cuda_engine
        self.deserialize.side_effect = self.engines.get
        self.enterContext(mock.patch.dict(sys.modules, torch=self.torch, tensorrt=self.trt))
        self.ort = self.enterContext(mock.patch.object(scan, "OrtScorer"))
        self.enterContext(mock.patch.object(scan, "log"))

    def engine(self, name, shape):
        data = name.encode()
        (self.models / name).write_bytes(data)
        engine = self.engines[data] = mock.Mock(num_io_tensors=2)
        engine.get_tensor_name.side_effect = ("input", "logits")
        engine.get_tensor_mode.side_effect = (self.trt.TensorIOMode.INPUT, self.trt.TensorIOMode.OUTPUT)
        engine.get_tensor_shape.return_value = shape
        engine.get_tensor_dtype.return_value = self.trt.DataType.FLOAT
        ctx = engine.create_execution_context.return_value
        ctx.set_input_shape.return_value = True
        ctx.get_tensor_shape.return_value = (4, 1, 1)
        return engine

    def assert_directml(self):
        self.assertIs(scan.make_scorer(self.args), self.ort.return_value)
        self.ort.assert_called_once_with(self.args.onnx, "dml")

    def test_skips_576_and_uses_768_engine_for_the_same_model(self):
        wrong = self.engine("rfdetr-v6.bs1-4.fp16.sm86.win.engine", (-1, 3, 576, 576))
        right = self.engine("rfdetr-v6.bs4.fp16.win.engine", (-1, 3, 768, 768))
        right.get_tensor_profile_shape.return_value = ((1, 3, 768, 768), SHAPE, SHAPE)

        self.assertIs(scan.make_scorer(self.args).engine, right)
        wrong.create_execution_context.assert_not_called()
        right.create_execution_context.return_value.set_input_shape.assert_called_once_with("input", SHAPE)
        self.ort.assert_not_called()

    def test_wrong_profile_falls_back_without_using_other_models(self):
        wrong_name = "rfdetr-v6.bs4.fp16.win.engine"
        wrong = self.engine(wrong_name, (-1, 3, -1, -1))
        wrong.get_tensor_profile_shape.return_value = ((1, 3, 576, 576), (4, 3, 576, 576), (4, 3, 576, 576))
        self.engine("rfdetr-v5.bs4.fp16.win.engine", SHAPE)

        self.assert_directml()
        self.deserialize.assert_called_once_with(wrong_name.encode())
        wrong.create_execution_context.assert_not_called()

    def test_input_rejected_by_context_falls_back_before_allocating_outputs(self):
        wrong = self.engine("rfdetr-v6.bs4.fp16.win.engine", SHAPE)
        wrong.create_execution_context.return_value.set_input_shape.return_value = False

        self.assert_directml()
        self.torch.empty.assert_not_called()

    def test_explicit_engine_is_validated_and_only_that_engine_is_tried(self):
        wrong_name = "override.engine"
        wrong = self.engine(wrong_name, (4, 3, 576, 576))
        self.engine("rfdetr-v6.bs4.fp16.win.engine", SHAPE)
        self.args.engine = str(self.models / wrong_name)

        self.assert_directml()
        self.deserialize.assert_called_once_with(wrong_name.encode())
        wrong.create_execution_context.assert_not_called()

    def test_directml_request_bypasses_tensorrt(self):
        self.args.provider = "dml"
        self.engine("rfdetr-v6.bs4.fp16.win.engine", SHAPE)

        self.assert_directml()
        self.trt.Runtime.assert_not_called()


# The real scene that jasna rejected: video stream 1538.666667 s, container 1538.699667 s
# (audio runs ~33 ms past the video), 30 fps, 46160 frames.
STREAM = {"r_frame_rate": "30/1", "avg_frame_rate": "30/1", "duration": "1538.666667", "nb_frames": "46160"}
FORMAT = {"duration": "1538.699667"}
VIDEO_END = 1538.666667


def jasna_accepts(segments, duration):
    """jasna.segments.parse_segments(..., duration=): end must not exceed duration + 1e-6."""
    for item in segments.split(","):
        end = sum(float(p) * 60 ** i for i, p in enumerate(reversed(item.partition("-")[2].split(":"))))
        if end > duration + 1e-6:
            return False
    return True


class ScanDurationTest(unittest.TestCase):
    def test_uses_the_video_stream_duration_not_the_longer_container_duration(self):
        self.assertEqual(scan.jasna_duration(STREAM, FORMAT), VIDEO_END)

    def test_falls_back_to_container_duration_then_frame_count(self):
        no_dur = {k: v for k, v in STREAM.items() if k != "duration"}
        self.assertEqual(scan.jasna_duration(no_dur, FORMAT), 1538.699667)
        self.assertAlmostEqual(scan.jasna_duration(no_dur, {}), 46160 / 30)
        no_frames = dict(no_dur, nb_frames="0")
        self.assertAlmostEqual(scan.jasna_duration(no_frames, {}, lambda: 900), 30.0)

    def test_ffprobe_meta_reads_stream_and_format_in_one_call(self):
        out = mock.Mock(stdout='{"streams": [%s], "format": %s}' % (
            str(STREAM).replace("'", '"'), str(FORMAT).replace("'", '"')))
        with mock.patch.object(scan.subprocess, "run", return_value=out) as run:
            self.assertEqual(scan.ffprobe_meta("ffprobe", "x.mkv"), (VIDEO_END, 30.0))
        argv = run.call_args.args[0]
        self.assertIn("stream=r_frame_rate,avg_frame_rate,duration,nb_frames:format=duration", argv)


class ScanSegmentEndTest(unittest.TestCase):
    def scan_end(self, times, scores, duration=VIDEO_END, stride=0.75, pad=None):
        ranges = scan.segments_from_scores(times, scores, 0.05, stride, duration, pad=pad)
        return ranges, scan.format_segments(ranges)

    def test_final_segment_never_ends_past_the_video(self):
        ranges, text = self.scan_end([1500.0, 1538.0], [0.9, 0.9])
        self.assertEqual(text, "00:24:59.625-00:25:01.125,00:25:37.625-00:25:38.666")
        self.assertTrue(jasna_accepts(text, VIDEO_END))
        self.assertLessEqual(ranges[-1][1], VIDEO_END)

    def test_rounding_to_the_nearest_millisecond_would_have_been_rejected(self):
        self.assertFalse(jasna_accepts("00:25:00.000-00:25:38.667", VIDEO_END))     # why ends round DOWN

    def test_scanning_to_the_container_end_is_clamped_to_the_video(self):
        # a segment reaching the (longer) container duration is what jasna refused
        self.assertFalse(jasna_accepts("00:25:00.000-00:25:38.700", VIDEO_END))
        _, text = self.scan_end([1538.5], [0.9], duration=VIDEO_END)
        self.assertTrue(jasna_accepts(text, VIDEO_END))

    def test_whole_millisecond_duration_is_kept_exactly(self):
        _, text = self.scan_end([599.5], [0.9], duration=600.0)
        self.assertTrue(text.endswith("-00:10:00.000"))

    def test_sliver_that_vanishes_on_the_millisecond_grid_is_dropped(self):
        # start 1538.6662, clamped end 1538.666667 -> both floor to 1538.666
        ranges, text = self.scan_end([1538.6662 + 0.1], [0.9], stride=0.0, pad=0.1)
        self.assertEqual((ranges, text), ([], ""))

    def test_ranges_match_the_string(self):
        ranges, text = self.scan_end([10.0], [0.9])
        self.assertEqual(ranges, [(9.625, 11.125)])
        self.assertEqual(text, "00:00:09.625-00:00:11.125")
