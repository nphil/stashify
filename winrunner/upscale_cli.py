"""SPAN video upscaler for the Stashify Windows runner's AI (NVIDIA) lane.

PyAV decode -> SR model -> fragmented mp4 + audio remux. On the 3080 the SR
runs through a TensorRT engine compiled from the SPAN weights (fp16), which is
~5-7x faster than spandrel/PyTorch eager at the SAME quality; it transparently
falls back to spandrel (PyTorch fp16) if TensorRT is unavailable, the engine
build fails, or the input is too large to fit. Progress lines match the
lada-style format the runner parses.

The TensorRT engine is per-resolution and cached next to the weights
(<model>.trt_fp16_<W>x<H>.win.engine), built once per resolution.
"""
import os
import sys
import time
import argparse
import subprocess

import av
import torch
from spandrel import ModelLoader

IS_WIN = os.name == "nt"
NOWIN = subprocess.CREATE_NO_WINDOW if IS_WIN else 0

# Max input pixels to attempt on TensorRT (fp16 SPAN 1080p peaks ~6.3 GB; 4K input
# -> 8K output would blow past 10 GB). Larger inputs fall back to spandrel.
TRT_MAX_PIXELS = 1920 * 1088


def log(m):
    print(m, flush=True)


def hms(s):
    s = max(0, int(s))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# --------------------------------------------------------------------------- #
# TensorRT SR engine (compiled from the SPAN weights) - the fast path
# --------------------------------------------------------------------------- #
def _engine_path(model_path, w, h, fp16):
    stem = os.path.splitext(os.path.basename(model_path))[0]
    prec = "fp16" if fp16 else "fp32"
    return os.path.join(os.path.dirname(os.path.abspath(model_path)),
                        f"{stem}.trt_{prec}_{w}x{h}.win.engine")


def _build_engine(model, w, h, fp16, engine_path):
    """Export the SR model to ONNX at a fixed (1,3,h,w) shape and build an fp16
    TensorRT engine. One-time per resolution; the engine is cached on disk."""
    import tensorrt as trt

    onnx_path = engine_path + ".onnx.tmp"
    # Export on CPU so the fp32 1080p->4K activations don't hold GPU VRAM that the
    # TensorRT builder then needs (that starvation OOMs the build).
    dummy = torch.zeros(1, 3, h, w, dtype=torch.float32)
    torch.onnx.export(model.float().cpu(), dummy, onnx_path, opset_version=17,
                      input_names=["input"], output_names=["output"])
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    try:
        logger = trt.Logger(trt.Logger.ERROR)
        builder = trt.Builder(logger)
        try:
            flag = 1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)
        except AttributeError:            # TRT 10+: networks are explicit-batch by default
            flag = 0
        network = builder.create_network(flag)
        parser = trt.OnnxParser(network, logger)
        with open(onnx_path, "rb") as fh:
            if not parser.parse(fh.read()):
                raise RuntimeError("onnx parse failed: " +
                                   "; ".join(str(parser.get_error(i)) for i in range(parser.num_errors)))
        config = builder.create_builder_config()
        config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 4 << 30)
        if fp16 and getattr(builder, "platform_has_fast_fp16", True):
            config.set_flag(trt.BuilderFlag.FP16)
        serialized = builder.build_serialized_network(network, config)
        if serialized is None:
            raise RuntimeError("engine build returned None")
        tmp = engine_path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(serialized)
        os.replace(tmp, engine_path)
    finally:
        try:
            os.remove(onnx_path)
        except OSError:
            pass


class TrtUpscaler:
    """Runs a serialized SR engine with torch CUDA buffers (port of scan_cli's runner)."""

    def __init__(self, engine_path, dev):
        import tensorrt as trt
        self.dev = torch.device(dev)
        rt = trt.Runtime(trt.Logger(trt.Logger.ERROR))
        with open(engine_path, "rb") as fh:
            self.engine = rt.deserialize_cuda_engine(fh.read())
        if self.engine is None:
            raise RuntimeError("deserialize failed (TensorRT version mismatch?)")
        self.ctx = self.engine.create_execution_context()
        td = {trt.DataType.FLOAT: torch.float32, trt.DataType.HALF: torch.float16}
        ins, outs = [], []
        for i in range(self.engine.num_io_tensors):
            n = self.engine.get_tensor_name(i)
            (ins if self.engine.get_tensor_mode(n) == trt.TensorIOMode.INPUT else outs).append(n)
        self._in, self._out = ins[0], outs[0]
        self._in_dtype = td.get(self.engine.get_tensor_dtype(self._in), torch.float32)
        self.ctx.set_input_shape(self._in, tuple(self.engine.get_tensor_shape(self._in)))
        oshape = tuple(int(d) for d in self.ctx.get_tensor_shape(self._out))
        odtype = td.get(self.engine.get_tensor_dtype(self._out), torch.float32)
        self._out_t = torch.empty(oshape, dtype=odtype, device=self.dev)
        self.ctx.set_tensor_address(self._out, int(self._out_t.data_ptr()))
        self._stream = torch.cuda.current_stream(self.dev).cuda_stream

    def run(self, ten):
        # ten: (1,3,h,w) cuda tensor in [0,1]; .contiguous() is REQUIRED (data_ptr).
        xt = ten.to(self.dev, dtype=self._in_dtype).contiguous()
        self.ctx.set_tensor_address(self._in, int(xt.data_ptr()))
        self.ctx.execute_async_v3(self._stream)
        torch.cuda.synchronize()
        return self._out_t


def _make_trt(model, model_path, w, h, use_half, dev):
    """Build/load a cached TRT engine for this resolution, or None to use spandrel."""
    if h * w > TRT_MAX_PIXELS:
        log("upscale: %dx%d exceeds the TensorRT size cap; using PyTorch" % (w, h))
        return None
    try:
        eng = _engine_path(model_path, w, h, use_half)
        if not os.path.isfile(eng):
            log("upscale: compiling TensorRT engine for %dx%d (one-time, cached)..." % (w, h))
            _build_engine(model, w, h, use_half, eng)
            log("upscale: TensorRT engine ready")
        up = TrtUpscaler(eng, dev)
        log("upscale: using TensorRT engine %s" % os.path.basename(eng))
        return up
    except Exception as exc:  # noqa: BLE001 - any failure -> spandrel still upscales
        log("upscale: TensorRT unavailable (%s); using PyTorch" % str(exc)[:180])
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--encoder", default="hevc_qsv")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--fp16", action="store_true")
    ap.add_argument("--no-trt", action="store_true", help="force spandrel/PyTorch (skip TensorRT)")
    args = ap.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    dev = args.device if torch.cuda.is_available() else "cpu"
    use_half = args.fp16 and dev.startswith("cuda")

    desc = ModelLoader().load_from_file(args.model)
    scale = getattr(desc, "scale", 2)
    model = desc.model.eval()                # keep on CPU; the TRT build exports from here,
                                             # and we only move it to the GPU if TRT fails

    in_c = av.open(args.input)
    vs = in_c.streams.video[0]
    vs.thread_type = "AUTO"
    fps = vs.average_rate or 30
    total = vs.frames or 0
    if not total and vs.duration and vs.time_base:
        total = int(float(vs.duration * vs.time_base) * float(fps))
    w, h = vs.codec_context.width, vs.codec_context.height
    log(f"input: {w}x{h} @ {float(fps):.3f}fps ~{total} frames -> {w*scale}x{h*scale}")

    # Try the TensorRT fast path; fall back to spandrel on the GPU only if it fails.
    trt_up = None if (args.no_trt or not dev.startswith("cuda")) else _make_trt(model, args.model, w, h, use_half, dev)
    if trt_up is None:
        model = model.to(dev)
        if use_half:
            model = model.half()
    fb_model = model
    log(f"model: {os.path.basename(args.model)} scale={scale}x device={dev} "
        f"{'trt-fp16' if trt_up is not None else ('fp16' if use_half else 'fp32')} encoder={args.encoder}")

    stem = os.path.splitext(os.path.basename(args.input))[0]
    vtmp = os.path.join(args.output_dir, stem + ".upscaling.tmp.mp4")
    final = os.path.join(args.output_dir, stem + ".upscaled.mp4")
    out_c = av.open(vtmp, "w", options={
        "movflags": "frag_keyframe+empty_moov+default_base_moof"})
    # PyAV can encode NVENC/x264 from system-memory frames, but QSV needs a
    # hardware-frames context PyAV doesn't set up — fall back to CPU x264 for
    # the (cheap) encode; the SR compute still runs on the GPU.
    enc = args.encoder
    if "qsv" in enc.lower():
        log(f"note: {enc} can't encode via PyAV; using libx264 for the encode step")
        enc = "libx264"
    try:
        ostream = out_c.add_stream(enc, rate=fps)
    except Exception as exc:  # noqa: BLE001
        log(f"encoder {enc} unavailable ({exc}); using libx264")
        ostream = out_c.add_stream("libx264", rate=fps)
    ostream.width, ostream.height = w * scale, h * scale
    ostream.pix_fmt = "yuv420p"

    t0 = time.time()
    done = 0
    last = 0.0
    fb_dtype = torch.half if (use_half and trt_up is None) else torch.float32
    with torch.no_grad():
        for frame in in_c.decode(vs):
            img = frame.to_ndarray(format="rgb24")
            ten = torch.from_numpy(img).to(dev).permute(2, 0, 1).unsqueeze(0).float().div_(255.0)
            if trt_up is not None:
                out = trt_up.run(ten)
            else:
                out = fb_model(ten.to(fb_dtype))
            arr = out.float().clamp_(0, 1).mul_(255.0).round_().byte()[0].permute(1, 2, 0).cpu().numpy()
            for pkt in ostream.encode(av.VideoFrame.from_ndarray(arr, format="rgb24")):
                out_c.mux(pkt)
            done += 1
            now = time.time()
            if now - last >= 2.0:
                last = now
                rate = done / max(0.001, now - t0)
                pct = int(100 * done / total) if total else 0
                remain = hms((total - done) / rate) if total and rate > 0 else "?"
                log(f"upscaling: {pct:3d}%| |Processed: {hms(done / float(fps))} ({done}f) | "
                    f"Remaining: {remain} | Speed: {rate:.1f}f/s")
    for pkt in ostream.encode():
        out_c.mux(pkt)
    out_c.close()
    in_c.close()
    rate = done / max(0.001, time.time() - t0)
    log(f"upscaled {done} frames in {hms(time.time() - t0)} ({rate:.1f}f/s); muxing audio")

    try:
        r = subprocess.run([args.ffmpeg, "-y", "-loglevel", "error", "-i", vtmp, "-i", args.input,
                            "-map", "0:v", "-map", "1:a?", "-c", "copy", "-shortest", final],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=600, creationflags=NOWIN)
        rc = r.returncode
    except subprocess.TimeoutExpired:
        log("audio mux timed out; keeping video-only")
        rc = 1
    if rc != 0:
        os.replace(vtmp, final)
    else:
        os.remove(vtmp)
    log(f"done -> {final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
