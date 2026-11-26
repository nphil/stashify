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
