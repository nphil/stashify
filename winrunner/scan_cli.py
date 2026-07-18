"""Headless mosaic scan for the Stashify Windows runner's live-preview feature.

The frozen jasna.exe has no scan CLI, so this reproduces jasna's whole-video
mosaic scan without torch/TensorRT: decode sampled frames via ffmpeg (NVDEC when
available), run the bundled rfdetr-v5.onnx through onnxruntime (DirectML -> the
3080 via DirectX, no CUDA/cuDNN), take jasna's per-frame score
(sigmoid(logits).max()), and merge above-floor samples into padded time ranges
(jasna segments_from_scores). Prints one JSON line with the `--segments` string.

jasna re-detects PRECISELY inside whatever ranges we pass, so the scan only needs
recall: a low floor + generous padding + fine sampling. False positives just get
harmlessly re-encoded.

Deps: numpy + onnxruntime-directml (no opencv - ffmpeg does decode + resize).
"""
import argparse
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


def segments_from_scores(times, scores, threshold, stride, duration, pad=None):
    if pad is None:
        pad = stride / 2.0
    hits = []
    for t, sc in zip(times, scores):
        if sc < threshold:
            continue
        hits.append((max(0.0, t - pad), min(duration, t + stride + pad)))
    return _normalize(hits, duration=duration)


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


def make_session(onnx_path, provider):
    _add_nvidia_dll_dirs()
    import onnxruntime as ort
    prov = {"dml": "DmlExecutionProvider", "cuda": "CUDAExecutionProvider",
            "cpu": "CPUExecutionProvider"}.get(provider, provider)
    so = ort.SessionOptions()
    sess = ort.InferenceSession(onnx_path, sess_options=so,
                                providers=[prov, "CPUExecutionProvider"])
    active = sess.get_providers()
    log("scan: onnxruntime %s providers=%s" % (ort.__version__, active))
    return sess, active


def ffprobe_meta(ffprobe, path):
    r = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=r_frame_rate:format=duration", "-of", "json", path],
        capture_output=True, text=True, timeout=30, creationflags=NOWIN)
    j = json.loads(r.stdout)
    dur = float(j["format"]["duration"])
    rate = j["streams"][0].get("r_frame_rate", "30/1")
    num, _, den = rate.partition("/")
    fps = float(num) / float(den or 1)
    return dur, fps


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


def score_output(outputs, out_names):
    # logits = the (B,Q,C) output that isn't boxes (last dim 4) or masks (ndim 4)
    boxes = next(i for i, o in enumerate(outputs) if o.ndim == 3 and o.shape[-1] == 4)
    masks = next(i for i, o in enumerate(outputs) if o.ndim == 4)
    logits_i = next(i for i in range(len(outputs)) if i not in (boxes, masks))
    logits = outputs[logits_i]
    prob = 1.0 / (1.0 + np.exp(-logits))
    return prob.max(axis=(1, 2))                      # (B,)


def run_scan(args):
    dur, fps = ffprobe_meta(args.ffprobe, args.input)
    rate = 1.0 / max(0.05, args.stride_seconds)
    log("scan: %.1fs @ %.2ffps, sampling %gfps (stride %.2fs)" % (dur, fps, rate, args.stride_seconds))
    sess, active = make_session(args.onnx, args.provider)
    inp = sess.get_inputs()[0]
    batch = inp.shape[0] if isinstance(inp.shape[0], int) else 4
    in_name = inp.name

    times, scores = [], []
    pending, pend_t = [], []
    idx = 0
    t0 = time.time()

    def flush():
        if not pending:
            return
        xs = list(pending)
        while len(xs) < batch:
            xs.append(xs[-1])
        x = np.stack(xs).astype(np.float32)
        outs = sess.run(None, {in_name: x})
        sc = score_output(outs, sess.get_outputs())
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
        flush()
    except RuntimeError as exc:
        if args.hwaccel == "cuda" and not args._no_hw:
            log("scan: hwaccel failed (%s); retrying software decode" % str(exc)[:120])
            args._no_hw = True
            return run_scan(args)
        raise

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
        "provider": active[0] if active else "?",
        "max_score": round(max(scores), 4) if scores else 0.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--onnx", required=True, help="path to rfdetr-v5.onnx")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default="ffprobe")
    ap.add_argument("--stride-seconds", type=float, default=1.0)
    ap.add_argument("--threshold", type=float, default=SCAN_SCORE_FLOOR)
    ap.add_argument("--pad", type=float, default=None, help="seconds each side (default stride/2)")
    ap.add_argument("--provider", default="dml")
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
