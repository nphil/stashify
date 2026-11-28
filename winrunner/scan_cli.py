"""Headless mosaic scan for the Stashify Windows runner's live-preview feature.

The frozen jasna.exe has no scan CLI, so this reproduces jasna's whole-video
mosaic scan: decode sampled frames via ffmpeg (NVDEC when available), run RF-DETR
with a compatible TensorRT engine or through onnxruntime (DirectML), take jasna's
per-frame score (sigmoid(logits).max()), and merge above-floor samples into
padded time ranges. Prints one JSON line with the `--segments` string.

jasna re-detects PRECISELY inside whatever ranges we pass, so the scan only needs
recall: a low floor + generous padding + fine sampling. False positives just get
harmlessly re-encoded.

Deps: numpy and ffmpeg, plus TensorRT/torch or onnxruntime-directml.
"""
import argparse
import math
import glob
import importlib.util
import json
import os
import subprocess
import sys
import time

import numpy as np

RES = 768
MEAN = np.array([0.485, 0.456, 0.406], np.float32)[:, None, None]
STD = np.array([0.229, 0.224, 0.225], np.float32)[:, None, None]
SCAN_SCORE_FLOOR = 0.05          # jasna mosaic_scan.SCAN_SCORE_FLOOR
IS_WIN = os.name == "nt"
NOWIN = subprocess.CREATE_NO_WINDOW if IS_WIN else 0


def log(m):
    print(m, file=sys.stderr, flush=True)


# --- jasna segments.py, ported (self-contained; scan venv has no jasna) --------
def _fmt_ts(seconds):
    total_ms = max(0, round(float(seconds) * 1000))
    h, r = divmod(total_ms, 3_600_000)
    m, r = divmod(r, 60_000)
    s, ms = divmod(r, 1000)
    return "%02d:%02d:%02d.%03d" % (h, m, s, ms)


def _normalize(ranges, duration=None):
    ordered = sorted((max(0.0, float(s)), float(e)) for s, e in ranges if e > s)
    merged = []
    for s, e in ordered:
        if duration is not None:
            e = min(e, float(duration))
        if s >= e:
            continue
        if merged and s <= merged[-1][1] + 1e-9:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _floor_ms(seconds):
    # Never round UP: jasna rejects a segment that ends past its own duration (which has
    # sub-millisecond digits, e.g. 1538.666667 -> "01:25:38.667" would overshoot it).
    return math.floor(float(seconds) * 1000 + 1e-6)


def _on_ms_grid(ranges):
    """Snap ranges to the millisecond grid the --segments string can express, rounding
    both ends down, so the string, the ranges we report and jasna's check all agree."""
    out = []
    for s, e in ranges:
        s_ms, e_ms = _floor_ms(s), _floor_ms(e)
        if e_ms <= s_ms:
            continue                                   # sliver at the very end of the video
        if out and s_ms <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e_ms))
        else:
            out.append((s_ms, e_ms))
    return [(s / 1000.0, e / 1000.0) for s, e in out]


def segments_from_scores(times, scores, threshold, stride, duration, pad=None):
    if pad is None:
        pad = stride / 2.0
    hits = []
    for t, sc in zip(times, scores):
        if sc < threshold:
            continue
        hits.append((max(0.0, t - pad), min(duration, t + stride + pad)))
    return _on_ms_grid(_normalize(hits, duration=duration))


def format_segments(ranges):
    return ",".join("%s-%s" % (_fmt_ts(s), _fmt_ts(e)) for s, e in ranges)


# --- onnxruntime (DirectML) ----------------------------------------------------
def _add_nvidia_dll_dirs():
    if not IS_WIN:
        return
    spec = importlib.util.find_spec("nvidia")
    if spec and spec.submodule_search_locations:
        nv = spec.submodule_search_locations[0]
        for binp in glob.glob(os.path.join(nv, "*", "bin")):
            try:
                os.add_dll_directory(binp)
            except OSError:
                pass


class OrtScorer:
    """onnxruntime scorer (DirectML/CUDA/CPU). ~150 ms/frame on DML for rfdetr."""

    def __init__(self, onnx_path, provider):
        _add_nvidia_dll_dirs()
        import onnxruntime as ort
        prov = {"dml": "DmlExecutionProvider", "cuda": "CUDAExecutionProvider",
                "cpu": "CPUExecutionProvider"}.get(provider, provider)
        self.sess = ort.InferenceSession(onnx_path, sess_options=ort.SessionOptions(),
                                         providers=[prov, "CPUExecutionProvider"])
        b = self.sess.get_inputs()[0].shape[0]
        self.batch = b if isinstance(b, int) else 4
        self._in = self.sess.get_inputs()[0].name
        self.provider_name = self.sess.get_providers()[0]
        log("scan: onnxruntime %s provider=%s" % (ort.__version__, self.provider_name))

    def score(self, x):
        outs = self.sess.run(None, {self._in: x.astype(np.float32)})
        boxes = next(i for i, o in enumerate(outs) if o.ndim == 3 and o.shape[-1] == 4)
        masks = next(i for i, o in enumerate(outs) if o.ndim == 4)
        logits = outs[next(i for i in range(len(outs)) if i not in (boxes, masks))]
        prob = 1.0 / (1.0 + np.exp(-logits))
        return prob.max(axis=(1, 2))


class TrtScorer:
    """Direct TensorRT: deserialize jasna's pre-built rfdetr engine and run it with
    torch CUDA buffers (port of jasna's TrtRunner). ~7 ms/frame on the 3080 - ~20x
    faster than onnxruntime, whose CPU-fallback of rfdetr's deformable-attention ops
    is the whole reason DML is slow. Engine is version-locked, so this needs the
    exact tensorrt build jasna compiled it with (tensorrt-cu12==10.16.1.11)."""
    batch = 4

    def __init__(self, engine_path):
        import torch
        import tensorrt as trt
        self._torch = torch
        if not os.path.isfile(engine_path):
            raise FileNotFoundError("engine not found: " + engine_path)
        self.dev = torch.device("cuda:0")
        rt = trt.Runtime(trt.Logger(trt.Logger.ERROR))
        with open(engine_path, "rb") as fh:
            self.engine = rt.deserialize_cuda_engine(fh.read())
        if self.engine is None:
            raise RuntimeError("deserialize failed (TensorRT version mismatch?)")
        td = {trt.DataType.FLOAT: torch.float32, trt.DataType.HALF: torch.float16,
              trt.DataType.INT32: torch.int32, trt.DataType.BOOL: torch.bool}
        ins, outs = [], []
        for i in range(self.engine.num_io_tensors):
            n = self.engine.get_tensor_name(i)
            (ins if self.engine.get_tensor_mode(n) == trt.TensorIOMode.INPUT else outs).append(n)
        self._in = ins[0]
        wanted = (self.batch, 3, RES, RES)
        shape = tuple(self.engine.get_tensor_shape(self._in))
        if len(shape) != len(wanted) or any(d not in (-1, w) for d, w in zip(shape, wanted)):
            raise ValueError("engine input %s does not support %s" % (shape, wanted))
        if -1 in shape:
            # The execution context uses optimization profile 0 by default.
            lo, _, hi = self.engine.get_tensor_profile_shape(self._in, 0)
            if not all(a <= w <= b for a, w, b in zip(lo, wanted, hi)):
                raise ValueError("engine profile %s to %s does not support %s" % (tuple(lo), tuple(hi), wanted))
        self.ctx = self.engine.create_execution_context()
        if self.ctx is None or not self.ctx.set_input_shape(self._in, wanted):
            raise RuntimeError("could not set TensorRT input shape %s" % (wanted,))
        self._in_dtype = td[self.engine.get_tensor_dtype(self._in)]
        self._out_t, shapes = {}, {}
        for n in outs:
            shape = tuple(int(d) for d in self.ctx.get_tensor_shape(n))
            t = torch.empty(shape, dtype=td[self.engine.get_tensor_dtype(n)], device=self.dev)
            self._out_t[n] = t
            shapes[n] = shape
            self.ctx.set_tensor_address(n, int(t.data_ptr()))
        self._logits = next(n for n, s in shapes.items() if len(s) == 3 and s[-1] != 4)
        self._stream = torch.cuda.current_stream(self.dev).cuda_stream
        self.provider_name = "TensorRT(%s)" % trt.__version__
        log("scan: TensorRT engine %s" % os.path.basename(engine_path))

    def score(self, x):
        torch = self._torch
        # .contiguous() is REQUIRED: without it the .to() buffer's data_ptr feeds
        # garbage to the engine (scores collapse to the no-input baseline).
        xt = torch.from_numpy(x).to(self.dev, dtype=self._in_dtype).contiguous()
        self.ctx.set_tensor_address(self._in, int(xt.data_ptr()))
        self.ctx.execute_async_v3(self._stream)
        torch.cuda.synchronize()
        return torch.sigmoid(self._out_t[self._logits].float()).amax(dim=(1, 2)).cpu().numpy()


def make_scorer(args):
    """Try compatible engines for the selected ONNX model, then DirectML."""
    if args.provider != "trt":
        return OrtScorer(args.onnx, args.provider)
    if args.engine:
        candidates = [args.engine]
    else:
        # Jasna cache filenames vary with the release/GPU and don't guarantee the input size.
        stem = os.path.splitext(args.onnx)[0]
        candidates = sorted(glob.glob(glob.escape(stem) + ".bs*.win.engine"))
    for path in candidates:
        try:
            return TrtScorer(path)
        except Exception as exc:  # noqa: BLE001
            log("scan: skipping TensorRT engine %s (%s)" % (os.path.basename(path), str(exc)[:160]))
    log("scan: no compatible TensorRT engine for %dx%d; falling back to DirectML" % (RES, RES))
    return OrtScorer(args.onnx, "dml")


def _ffprobe_json(ffprobe, path, *options):
    r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", *options, "-of", "json", path],
                       capture_output=True, text=True, timeout=30, creationflags=NOWIN)
    return json.loads(r.stdout)


def _rate(text, default):
    num, _, den = str(text or "").partition("/")
    try:
        num, den = float(num), float(den or 1)
    except ValueError:
        return default
    return num / den if den else num


def jasna_duration(stream, fmt, count_packets=None):
    """The video length jasna validates --segments against (jasna/media/probe.py
    get_video_meta_data): the VIDEO stream's duration, else the container's, else
    frames / fps. The container duration is the longest stream, so audio that runs past
    the video makes it larger, and a segment ending there is rejected by jasna."""
    value = stream.get("duration") or fmt.get("duration")
    if value:
        return float(value)
    frames = int(stream.get("nb_frames") or 0)
    if frames <= 0 and count_packets:
        frames = count_packets()
    fps = _rate(stream.get("avg_frame_rate"), 0.0) or _rate(stream.get("r_frame_rate"), 30.0)
    return frames / fps


def ffprobe_meta(ffprobe, path):
    j = _ffprobe_json(ffprobe, path, "-show_entries",
                      "stream=r_frame_rate,avg_frame_rate,duration,nb_frames:format=duration")
    stream, fmt = j["streams"][0], j.get("format") or {}

    def count_packets():
        c = _ffprobe_json(ffprobe, path, "-count_packets", "-show_entries", "stream=nb_read_packets")
        return int(c["streams"][0]["nb_read_packets"])

    return jasna_duration(stream, fmt, count_packets), _rate(stream.get("r_frame_rate"), 30.0)


def frames_via_ffmpeg(ffmpeg, path, rate, hwaccel):
    """Yield 768x768 rgb24 frames sampled at `rate` fps via an ffmpeg pipe."""
    pre = ["-hwaccel", "cuda"] if hwaccel == "cuda" else []
    argv = [ffmpeg, "-nostdin", "-loglevel", "error", *pre, "-i", path,
            "-an", "-sn", "-vf", "fps=%g,scale=%d:%d:flags=bilinear" % (rate, RES, RES),
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=NOWIN)
    fbytes = RES * RES * 3
    buf = b""
    try:
        while True:
            chunk = proc.stdout.read(fbytes - len(buf) if len(buf) < fbytes else fbytes)
            if not chunk:
                break
            buf += chunk
            while len(buf) >= fbytes:
                frame = np.frombuffer(buf[:fbytes], np.uint8).reshape(RES, RES, 3)
                buf = buf[fbytes:]
                yield frame
    finally:
        proc.stdout.close()
        rc = proc.wait()
        err = proc.stderr.read().decode("utf-8", "replace")
        proc.stderr.close()
        if rc not in (0, None) and not err_is_benign(err):
            raise RuntimeError("ffmpeg decode failed (rc=%s): %s" % (rc, err[:300]))


def err_is_benign(err):
    return err.strip() == ""


def preprocess(frame_rgb):
    x = frame_rgb.astype(np.float32) / 255.0          # HWC, already 768x768
    chw = np.transpose(x, (2, 0, 1))
    return (chw - MEAN) / STD


def run_scan(args):
    dur, fps = ffprobe_meta(args.ffprobe, args.input)
    rate = 1.0 / max(0.05, args.stride_seconds)
    log("scan: %.1fs @ %.2ffps, sampling %gfps (stride %.2fs)" % (dur, fps, rate, args.stride_seconds))
    scorer = make_scorer(args)
    batch = scorer.batch

    times, scores = [], []
    pending, pend_t = [], []
    idx = 0
    t0 = time.time()
    est_total = max(1, int(dur * rate))
    last_prog = [0.0]

    def _progress():
        now = time.time()
        if now - last_prog[0] >= 2.0:
            last_prog[0] = now
            pct = min(99, int(100 * len(scores) / est_total))
            log("scan-progress: %d%% (%d/%d samples)" % (pct, len(scores), est_total))

    def flush():
        if not pending:
            return
        xs = list(pending)
        while len(xs) < batch:
            xs.append(xs[-1])
        x = np.stack(xs).astype(np.float32)
        sc = scorer.score(x)
        for j in range(len(pending)):
            times.append(pend_t[j])
            scores.append(float(sc[j]))
        pending.clear()
        pend_t.clear()

    try:
        for frame in frames_via_ffmpeg(args.ffmpeg, args.input, rate,
                                       args.hwaccel if not args._no_hw else "none"):
            pending.append(preprocess(frame))
            pend_t.append(idx / rate)
            idx += 1
            if len(pending) == batch:
                flush()
                _progress()
        flush()
    except RuntimeError as exc:
        if args.hwaccel == "cuda" and not args._no_hw:
            log("scan: hwaccel failed (%s); retrying software decode" % str(exc)[:120])
            args._no_hw = True
            return run_scan(args)
        raise

    # Coverage guard. NVDEC (-hwaccel cuda) can hit a CLEAN early EOF on some HEVC files -
    # ffmpeg exits 0 with no error, so the except above never fires, and we'd silently scan
    # only the first minutes and leave the rest of the video un-restored. If we covered
    # materially less than the probed duration, retry with software decode; if even that
    # falls short, flag the scan incomplete so the caller can fall back to a full decensor
    # rather than ship a video with most of its mosaics untouched.
    expected = max(1, int(dur * rate))
    covered_s = len(scores) / rate if scores else 0.0
    incomplete = len(scores) < 0.9 * expected
    if incomplete and args.hwaccel == "cuda" and not args._no_hw:
        log("scan: decode covered only ~%.0f/%.0fs (%d/%d samples); retrying software decode"
            % (covered_s, dur, len(scores), expected))
        args._no_hw = True
        return run_scan(args)
    if incomplete:
        log("scan: WARNING decode reached only ~%.0f/%.0fs (%d/%d samples) - scan is INCOMPLETE; "
            "mosaics after that point would NOT be restored" % (covered_s, dur, len(scores), expected))

    ranges = segments_from_scores(times, scores, args.threshold, args.stride_seconds,
                                  dur, pad=args.pad)
    n_hits = sum(1 for s in scores if s >= args.threshold)
    took = time.time() - t0
    log("scan: %d samples, %d over floor, %d ranges, %.1fs (%.0f ms/frame)"
        % (len(scores), n_hits, len(ranges), took, 1000 * took / max(1, len(scores))))
    return {
        "segments": format_segments(ranges),
        "ranges": [[round(s, 3), round(e, 3)] for s, e in ranges],
        "duration": round(dur, 3),
        "n_samples": len(scores),
        "n_hits": n_hits,
        "coverage": round(len(scores) / expected, 3),   # fraction of the video actually scanned
        "incomplete": bool(incomplete),
        "provider": scorer.provider_name,
        "max_score": round(max(scores), 4) if scores else 0.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--onnx", required=True, help="path to the RF-DETR ONNX model")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default="ffprobe")
    ap.add_argument("--stride-seconds", type=float, default=1.0)
    ap.add_argument("--threshold", type=float, default=SCAN_SCORE_FLOOR)
    ap.add_argument("--pad", type=float, default=None, help="seconds each side (default stride/2)")
    ap.add_argument("--provider", default="dml", choices=["trt", "dml", "cuda", "cpu"],
                    help="trt = direct TensorRT on jasna's engine (fast); dml = onnxruntime DirectML")
    ap.add_argument("--engine", default="",
                    help="try this TensorRT engine only (default: discover engines next to the ONNX model)")
    ap.add_argument("--hwaccel", default="cuda", choices=["cuda", "none"])
    args = ap.parse_args()
    args._no_hw = False
    if not os.path.isfile(args.input):
        log("scan: input not found: " + args.input); return 2
    if not os.path.isfile(args.onnx):
        log("scan: onnx not found: " + args.onnx); return 2
    result = run_scan(args)
    print(json.dumps(result), flush=True)   # single machine-readable line on stdout
    return 0


if __name__ == "__main__":
    sys.exit(main())
