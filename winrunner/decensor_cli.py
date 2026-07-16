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
HEARTBEAT_SECS = 60
# stop heartbeating after this much CONTINUOUS silence: long enough for a
# worst-case first-run TensorRT compile, short enough that a truly hung jasna
# (dead SMB read) is eventually handed to the runner's stall watchdog to kill
HEARTBEAT_MAX_QUIET = 2 * 3600
PROGRESS_THROTTLE = 2.0

_RE_HAS_PCT = re.compile(r"\d{1,3}\s*%")
_RE_FPS_GLUED = re.compile(r"([\d.]+)\s*fps\b", re.IGNORECASE)


def log(m):
    print(m, flush=True)


def normalize(seg):
    """Jasna tqdm segment -> lada-style progress line, or None if not progress."""
    seg = seg.replace(",", "")            # tqdm thousands separators in frame counts
    if not _RE_HAS_PCT.search(seg) or "(" not in seg:
        return None
    # "Speed: 12.3fps" -> "Speed: 12.3 f/s" (the runner's fps regex wants f/s)
    seg = _RE_FPS_GLUED.sub(lambda m: m.group(1) + " f/s", seg)
    seg = re.sub(r"^\s*Processing video:\s*", "", seg)
    return "decensor: " + seg.strip()


def main():
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
                         "our own cleanup never runs.")
    ap.add_argument("--extra", default="", help="extra raw args appended to jasna")
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

    if args.working_dir:
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
            "--working-directory", work_dir,
            "--log-level", "info"]
    if args.encoder_settings:
        argv += ["--encoder-settings", args.encoder_settings]
    if args.no_compile:
        argv.append("--no-compile-basicvsrpp")
    if args.extra:
        argv += args.extra.split()

    log("decensor: starting jasna (first run compiles TensorRT engines, "
        "which can take 15-60 min with no output)")
    log("$ " + subprocess.list2cmdline(argv))

    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=NOWIN)
    last_out = [time.time()]
    done = threading.Event()

    def heartbeat():
        # last_out is written ONLY by the reader thread (real jasna output), so
        # `quiet` measures continuous real silence; last_beat throttles our own
        # lines without polluting that measurement.
        last_beat = 0.0
        while not done.wait(15):
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
            log(f"decensor: jasna still running, no output for {int(quiet)}s "
                f"(engine compile / long encode phases are silent)")
    threading.Thread(target=heartbeat, daemon=True).start()

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
                    pending = p
                    if time.time() - last_progress >= PROGRESS_THROTTLE:
                        last_progress = time.time()
                        log(pending)
                        pending = None
                else:
                    log("jasna: " + seg)
        if pending:
            log(pending)            # flush the final (usually 100%) update
        tail = buf.decode("utf-8", "replace").strip()
        if tail:
            log(("jasna: " + tail) if not normalize(tail) else normalize(tail))
        rc = proc.wait()
    finally:
        done.set()
        if proc.poll() is None:      # we're exiting abnormally: don't orphan jasna
            try:
                proc.kill()
            except OSError:
                pass
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
