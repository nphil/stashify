"""Jasna mosaic-removal wrapper for the Stashify Windows runner's AI lane.

Drives a frozen jasna.exe (github.com/Kruk2/jasna) as a subprocess and
normalizes its tqdm progress (carriage-return updates on stderr, "Speed: Nfps")
into the lada-style newline-terminated lines the runner parses:

    decensor: 42%| |Processed: 00:12 (1234f) | Remaining: 01:23 | Speed: 12.3 f/s

Also emits heartbeat lines while jasna is silent: the first run compiles
TensorRT engines for 15-60 minutes with no output, which would otherwise trip
the runner's stall watchdog (STALL_SECS). Jasna's temp files (raw .hevc +
timecodes) go to a per-job dir under --working-dir so a NAS-scratch output dir
doesn't take the double-write over SMB.

Stdlib only; jasna.exe is self-contained (bundled Python/TensorRT/ffmpeg).
"""
import os
import re
import sys
import time
import shutil
import argparse
import tempfile
import threading
import subprocess

IS_WIN = os.name == "nt"
NOWIN = subprocess.CREATE_NO_WINDOW if IS_WIN else 0
HEARTBEAT_SECS = 25   # report liveness this often during silent phases (was 60)
# stop heartbeating after this much CONTINUOUS silence: long enough for a
# worst-case first-run TensorRT compile, short enough that a truly hung jasna
# (dead SMB read) is eventually handed to the runner's stall watchdog to kill
HEARTBEAT_MAX_QUIET = 2 * 3600
PROGRESS_THROTTLE = 2.0

_RE_HAS_PCT = re.compile(r"\d{1,3}\s*%")
_RE_FPS_GLUED = re.compile(r"([\d.]+)\s*fps\b", re.IGNORECASE)


def log(m):
    print(m, flush=True)


def _gpu_stats():
    """Best-effort (util%, nvenc%, mem_MB) so a silent phase can still prove it's
    alive. None if nvidia-smi isn't reachable."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,utilization.encoder,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6, creationflags=NOWIN)
        gu, eu, mem = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
        return int(gu), int(eu), int(mem)
    except Exception:  # noqa: BLE001
        return None


def _bytes_in(paths):
    """Total bytes across the output file + work dir, to detect encode/mux progress
    when jasna emits no tqdm (metadata only - never reads file contents)."""
    total = 0
    for p in paths:
        try:
            if os.path.isfile(p):
                total += os.path.getsize(p)
            elif os.path.isdir(p):
                for root, _dirs, files in os.walk(p):
                    for f in files:
                        try:
                            total += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
        except OSError:
            pass
    return total


def normalize(seg):
    """Jasna tqdm segment -> lada-style progress line, or None if not progress."""
    seg = seg.replace(",", "")            # tqdm thousands separators in frame counts
    # tqdm redraws with \r (no \n), so a following log line gets glued onto the
    # bar; cut at that log timestamp so the progress line stays clean.
    seg = re.split(r"\d{2}:\d{2}:\d{2}\s+jasna\.", seg, maxsplit=1)[0]
    if not _RE_HAS_PCT.search(seg) or "(" not in seg:
        return None
    # "Speed: 12.3fps" -> "Speed: 12.3 f/s" (the runner's fps regex wants f/s)
    seg = _RE_FPS_GLUED.sub(lambda m: m.group(1) + " f/s", seg)
    seg = re.sub(r"^\s*Processing video:\s*", "", seg)
    return "decensor: " + seg.strip()


# debug-level noise we DON'T want in the dashboard log (per-batch pipeline internals)
_RE_SPAM = re.compile(r"\[(decode|primary|secondary)\]|frame_start=|clip_q=|pending=|encode_q=")
_RE_CLIP = re.compile(r"\bclip=(\d+)")


def _make_forwarder():
    """Turn jasna's --log-level debug stream into clean narration: collapse the
    repetitive engine loads, surface a throttled 'restoring mosaic clip #N'
    milestone (so a long silent-looking restore shows real motion), drop the
    per-batch spam, and pass real phase/info/warning lines through."""
    st = {"clip": -1, "eng": False, "last_clip": 0.0}

    def forward(seg):
        if "Loading TensorRT export" in seg or "using TRT sub-engines" in seg:
            if not st["eng"]:
                st["eng"] = True
                log("jasna: loading models / TensorRT engines")
            return
        if "[remux]" in seg:                       # the final (otherwise silent) mux phase
            log("decensor: muxing final output (audio + metadata)")
            return
        m = _RE_CLIP.search(seg)
        if m:
            c = int(m.group(1))
            now = time.time()
            if c != st["clip"] and now - st["last_clip"] >= 3.0:
                st["clip"] = c
                st["last_clip"] = now
                log("decensor: restoring mosaic clip #%d" % c)
            return
        if _RE_SPAM.search(seg):
            return
        log("jasna: " + seg)
    return forward


def main():
    # jasna emits tqdm block glyphs + non-ASCII paths; the runner reads us as
    # utf-8, so force utf-8 out regardless of the spawn locale (Windows piped
    # stdout defaults to cp1252 and would crash on those characters).
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--jasna", required=True, help="path to jasna.exe")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--detection-model", default="rfdetr-v5")
    ap.add_argument("--max-clip-size", default="90")
    ap.add_argument("--encoder-settings", default="")
    ap.add_argument("--no-compile", action="store_true",
                    help="pass --no-compile-basicvsrpp (less VRAM, slower)")
    ap.add_argument("--working-dir", default="",
                    help="dir for jasna temp files, created if missing (default: a "
                         "throwaway under the output dir). Pass a job-scoped dir the "
                         "CALLER also cleans up: if we are tree-killed (cancel/stall) "
                         "our own cleanup never runs. Ignored with --in-place.")
    ap.add_argument("--in-place", action="store_true",
                    help="jasna >=0.8.0 dropped --working-directory and writes the "
                         "output in place; skip the temp working dir entirely.")
    ap.add_argument("--extra", default="", help="extra raw args appended to jasna")
    ap.add_argument("--secondary-restoration", default="none",
                    help="jasna secondary restoration: none | rtx-super-res | unet-4x | tvai")
    ap.add_argument("--rtx-scale", default="4")
    ap.add_argument("--rtx-quality", default="ultra")
    ap.add_argument("--rtx-denoise", default="")
    ap.add_argument("--rtx-deblur", default="")
    args = ap.parse_args()

    if not os.path.isfile(args.jasna):
        log(f"error: jasna not found at {args.jasna}")
        return 2
    if not os.path.isfile(args.input):
        log(f"error: input not found: {args.input}")
        return 2
    os.makedirs(args.output_dir, exist_ok=True)

    stem = os.path.splitext(os.path.basename(args.input))[0]
    out_path = os.path.join(args.output_dir, stem + "_decensored.mp4")

    # jasna >=0.8.0 has no --working-directory (writes output in place); older
    # builds need one so the temp .hevc doesn't double-write over SMB.
    if args.in_place:
        work_dir = None
    elif args.working_dir:
        work_dir = args.working_dir
        os.makedirs(work_dir, exist_ok=True)
    else:
        os.makedirs(args.output_dir, exist_ok=True)
        work_dir = tempfile.mkdtemp(prefix="jasna_", dir=args.output_dir)

    argv = [args.jasna,
            "--input", args.input,
            "--output", out_path,
            "--device", args.device,
            "--detection-model", args.detection_model,
            "--max-clip-size", str(args.max_clip_size),
            "--log-level", "debug"]   # parsed into clean narration below (spam suppressed)
    if work_dir:
        argv += ["--working-directory", work_dir]
    if args.encoder_settings:
        argv += ["--encoder-settings", args.encoder_settings]
    if args.no_compile:
        argv.append("--no-compile-basicvsrpp")
    sec = (args.secondary_restoration or "none").strip()
    if sec and sec != "none":
        argv += ["--secondary-restoration", sec]
        if sec == "rtx-super-res":
            argv += ["--rtx-scale", str(args.rtx_scale), "--rtx-quality", args.rtx_quality]
            if args.rtx_denoise:
                argv += ["--rtx-denoise", args.rtx_denoise]
            if args.rtx_deblur:
                argv += ["--rtx-deblur", args.rtx_deblur]
        extra = f" (rtx {args.rtx_quality}/{args.rtx_scale}x)" if sec == "rtx-super-res" else ""
        log("decensor: secondary restoration = " + sec + extra)
    if args.extra:
        argv += args.extra.split()

    log("decensor: starting jasna (first run compiles TensorRT engines, "
        "which can take 15-60 min with no output)")
    log("$ " + subprocess.list2cmdline(argv))

    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=NOWIN)
    last_out = [time.time()]
    done = threading.Event()
    progress_seen = [False]

    def heartbeat():
        # last_out is written ONLY by the reader thread (real jasna output), so
        # `quiet` measures continuous real silence; last_beat throttles our own
        # lines without polluting that measurement.
        last_beat = 0.0
        prev_bytes = [None]
        while not done.wait(10):
            now = time.time()
            quiet = now - last_out[0]
            if quiet < HEARTBEAT_SECS or now - last_beat < HEARTBEAT_SECS:
                continue
            if quiet > HEARTBEAT_MAX_QUIET:
                # go silent: the runner's stall watchdog takes over and kills us
                log("decensor: jasna silent for over %dh - assuming hung, "
                    "letting the stall watchdog end this" % (HEARTBEAT_MAX_QUIET // 3600))
                return
            last_beat = now
            # Prove liveness during jasna's silent phases: GPU busy => compiling or
            # restoring; output growing => encoding/muxing. Both idle => maybe hung.
            cur = _bytes_in([out_path] + ([work_dir] if work_dir else []))
            grew = 0 if prev_bytes[0] is None else max(0, cur - prev_bytes[0])
            prev_bytes[0] = cur
            g = _gpu_stats()
            parts = []
            if g:
                parts.append("GPU %d%% · NVENC %d%% · %.1fGB" % (g[0], g[1], g[2] / 1024))
            if grew:
                parts.append("output +%dMB/%ds" % (grew // (1 << 20), HEARTBEAT_SECS))
            busy = (g is not None and (g[0] > 5 or g[1] > 5)) or grew > 0
            if not progress_seen[0]:
                phase = "loading / compiling TensorRT engines (one-time, up to ~60 min)"
            elif g is not None and g[0] > 15:
                phase = "restoring a segment on the GPU"
            elif grew:
                phase = "encoding / assembling output"
            elif busy:
                phase = "working"
            else:
                phase = "no GPU or disk activity - may be stalled (watchdog will end a true hang)"
            tail = (" - " + " · ".join(parts)) if parts else ""
            log("decensor: working [%ds no tqdm]: %s%s" % (int(quiet), phase, tail))
    threading.Thread(target=heartbeat, daemon=True).start()

    forward = _make_forwarder()
    rc = 1
    try:
        buf = b""
        last_progress = 0.0
        pending = None
        while True:
            # read1: return as soon as ANY bytes arrive (read(n) would block
            # until a full 4096B accumulates - minutes at tqdm's output rate)
            chunk = proc.stdout.read1(4096)
            if not chunk:
                break
            last_out[0] = time.time()
            buf += chunk
            # tqdm redraws with \r; logs end with \n - treat both as delimiters
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                seg = raw.decode("utf-8", "replace").strip()
                if not seg:
                    continue
                p = normalize(seg)
                if p:
                    progress_seen[0] = True
                    pending = p
                    if time.time() - last_progress >= PROGRESS_THROTTLE:
                        last_progress = time.time()
                        log(pending)
                        pending = None
                else:
                    forward(seg)
        if pending:
            log(pending)            # flush the final (usually 100%) update
        tail = buf.decode("utf-8", "replace").strip()
        if tail:
            t = normalize(tail)
            log(t) if t else forward(tail)
        rc = proc.wait()
    finally:
        done.set()
        if proc.poll() is None:      # we're exiting abnormally: don't orphan jasna
            try:
                proc.kill()
            except OSError:
                pass
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)

    if rc == 0 and not os.path.isfile(out_path):
        log("error: jasna exited 0 but produced no output")
        return 1
    if rc == 0:
        log(f"decensor: 100%| |done -> {out_path}")
    else:
        log(f"error: jasna exited {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
