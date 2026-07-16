"""SPAN video upscaler for the Stashify Windows runner's AI (NVIDIA) lane.

Same pipeline as the NAS runner's upscale_cli.py (PyAV decode -> spandrel SR
model -> fragmented mp4 + audio remux), but fp16/autocast-capable: on an Ampere
3080 that is a large speedup over the P40's forced fp32. Progress lines match
the lada-style format the runner parses.
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


def log(m):
    print(m, flush=True)


def hms(s):
    s = max(0, int(s))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--encoder", default="hevc_qsv")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--fp16", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    dev = args.device if torch.cuda.is_available() else "cpu"
    use_half = args.fp16 and dev.startswith("cuda")

    desc = ModelLoader().load_from_file(args.model)
    scale = getattr(desc, "scale", 2)
    model = desc.model.eval().to(dev)
    if use_half:
        model = model.half()
    log(f"model: {os.path.basename(args.model)} scale={scale}x device={dev} "
        f"{'fp16' if use_half else 'fp32'} encoder={args.encoder}")

    in_c = av.open(args.input)
    vs = in_c.streams.video[0]
    vs.thread_type = "AUTO"
    fps = vs.average_rate or 30
    total = vs.frames or 0
    if not total and vs.duration and vs.time_base:
        total = int(float(vs.duration * vs.time_base) * float(fps))
    w, h = vs.codec_context.width, vs.codec_context.height
    log(f"input: {w}x{h} @ {float(fps):.3f}fps ~{total} frames -> {w*scale}x{h*scale}")

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
    dtype = torch.half if use_half else torch.float32
    with torch.no_grad():
        for frame in in_c.decode(vs):
            img = frame.to_ndarray(format="rgb24")
            ten = torch.from_numpy(img).to(dev).permute(2, 0, 1).unsqueeze(0).to(dtype).div_(255.0)
            out = model(ten)
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

    r = subprocess.run([args.ffmpeg, "-y", "-loglevel", "error", "-i", vtmp, "-i", args.input,
                        "-map", "0:v", "-map", "1:a?", "-c", "copy", "-shortest", final],
                       capture_output=True, text=True, creationflags=NOWIN)
    if r.returncode != 0:
        log("audio mux failed; keeping video-only")
        os.replace(vtmp, final)
    else:
        os.remove(vtmp)
    log(f"done -> {final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
