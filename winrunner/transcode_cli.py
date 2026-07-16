"""Distributed transcode for the Stashify Windows runner's iGPU lane.

ffmpeg hardware transcode (Intel QuickSync by default, NVENC or CPU too),
emitting lada-style progress lines so runner.py's single parser reads it:
    transcode: 42%| |Processed: 00:12 (1234f) | Remaining: 01:23 | Speed: 3.2f/s

Output is a fragmented mp4 (readable while growing) so the live preview/feed
work mid-transcode, matching the decensor/upscale ops.
"""
import os
import re
import sys
import time
import argparse
import subprocess

IS_WIN = os.name == "nt"
NOWIN = subprocess.CREATE_NO_WINDOW if IS_WIN else 0


def log(m):
    print(m, flush=True)


def hms(s):
    s = max(0, int(s))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def probe(ffprobe, path):
    """(total_frames, fps) best-effort."""
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=r_frame_rate,nb_frames,duration",
                            "-of", "default=nw=1", path],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30, creationflags=NOWIN)
        d = {}
        for line in r.stdout.splitlines():
            k, _, v = line.partition("=")
            d[k.strip()] = v.strip()
        num, _, den = d.get("r_frame_rate", "0/1").partition("/")
        fps = float(num) / float(den or 1) if float(den or 1) else 0.0
        nb = d.get("nb_frames", "")
        if nb.isdigit() and int(nb) > 0:
            return int(nb), fps
        dur = float(d.get("duration") or 0)
        return int(dur * fps) if dur and fps else 0, fps
    except Exception:  # noqa: BLE001
        return 0, 0.0


# encoder -> (quality-flag, extra args). CQ/CRF/global_quality all ~0-51-ish.
ENCODER_ARGS = {
    "hevc_qsv":  (["-global_quality", "{q}", "-look_ahead", "1"], []),
    "h264_qsv":  (["-global_quality", "{q}", "-look_ahead", "1"], []),
    "av1_qsv":   (["-global_quality", "{q}"], []),
    "hevc_nvenc": (["-preset", "p5", "-rc", "vbr", "-cq", "{q}"], []),
    "h264_nvenc": (["-preset", "p5", "-rc", "vbr", "-cq", "{q}"], []),
    "av1_nvenc":  (["-preset", "p5", "-rc", "vbr", "-cq", "{q}"], []),
    "libx264":   (["-preset", "medium", "-crf", "{q}"], []),
    "libx265":   (["-preset", "medium", "-crf", "{q}"], []),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default="ffprobe")
    ap.add_argument("--encoder", default="hevc_qsv")
    ap.add_argument("--codec", default="")            # informational; encoder decides
    ap.add_argument("--height", type=int, default=0)  # 0 = keep source resolution
    ap.add_argument("--quality", default="24")
    ap.add_argument("--container", default="mp4")
    args = ap.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    total, fps = probe(args.ffprobe, args.input)
    stem = os.path.splitext(os.path.basename(args.input))[0]
    out = os.path.join(args.output_dir, f"{stem}.transcoded.{args.container}")
    log(f"transcode: {os.path.basename(args.input)} -> {args.encoder} q{args.quality}"
        + (f" @{args.height}p" if args.height else "") + f" (~{total} frames)")

    qargs, extra = ENCODER_ARGS.get(args.encoder, ENCODER_ARGS["libx264"])
    qargs = [a.replace("{q}", str(args.quality)) for a in qargs]

    cmd = [args.ffmpeg, "-y", "-hide_banner", "-nostats",
           "-i", args.input]
    if args.height:
        cmd += ["-vf", f"scale=-2:{args.height}"]
    cmd += ["-c:v", args.encoder] + qargs + extra
    cmd += ["-c:a", "copy", "-c:s", "copy",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-progress", "pipe:1", out]

    log("$ " + subprocess.list2cmdline(cmd))
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                            creationflags=NOWIN)
    frame = 0
    speed = 0.0
    cur_fps = 0.0
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if ("error" in low or "invalid" in low or "unable" in low) and "=" not in line:
            log(line)                       # surface real ffmpeg errors (the merged stderr)
            continue
        # -progress emits clean "key=value" (no spaces); ffmpeg's human stats line
        # ("frame=  96 fps= 89 q=...") also starts with frame= — guard by strict parse.
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key, val = key.strip(), val.strip()
        if key == "frame" and val.isdigit():
            frame = int(val)
        elif key == "fps":
            try:
                cur_fps = float(val)
            except ValueError:
                pass
        elif key == "speed" and val not in ("N/A", ""):
            try:
                speed = float(val.rstrip("x"))
            except ValueError:
                pass
        elif key == "progress":
            pct = int(100 * frame / total) if total else 0
            rate = cur_fps or (frame / max(0.001, time.time() - t0))
            eta = hms((total - frame) / rate) if total and rate > 0 else "?"
            log(f"transcode: {pct:3d}%| |Processed: {hms(frame / fps if fps else 0)} ({frame}f) | "
                f"Remaining: {eta} | Speed: {rate:.1f}f/s {speed:.2f}x")
            if val == "end":
                break
    proc.wait()
    if proc.returncode != 0:
        log(f"ffmpeg exited {proc.returncode}")
        return proc.returncode
    log(f"done -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
