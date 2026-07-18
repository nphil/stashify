"""Lada compute-node runner — a tiny HTTP service that wraps one `lada-cli`
invocation with live progress/log + cancel/pause/resume.

This is the first "runner" of the eventual coordinator/runner distributed
system: the Stashify worker (coordinator) POSTs a job here, tails the log, and
reads the produced file back off a shared scratch mount. Nothing Stash-aware
lives here — it only turns an input video path into a restored output file.

Endpoints (JSON; send X-Runner-Token if RUNNER_TOKEN is set; X-Lada-Token/LADA_TOKEN still accepted):
  GET  /health                         -> {ok, device, models, busy}
  POST /run   {input, output_dir, ...} -> {id}
  GET  /jobs/<id>                      -> job
  GET  /jobs/<id>/log?after=<seq>      -> {cursor, lines}
  POST /jobs/<id>/(cancel|pause|resume)

Only one job runs at a time (single GPU). Standard-library only.
"""
import os
import re
import sys
import json
import time
import uuid
import queue
import signal
import logging
import subprocess
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LADA_CLI = os.environ.get("LADA_CLI", "/opt/lada/.venv/bin/lada-cli")
LADA_DIR = os.environ.get("LADA_DIR", "/opt/lada")
VENV_PY = os.environ.get("LADA_VENV_PY", "/opt/lada/.venv/bin/python")
UPSCALE_SCRIPT = os.environ.get("UPSCALE_SCRIPT", "/opt/lada/upscale_cli.py")
MODELS_DIR = os.environ.get("LADA_MODEL_WEIGHTS_DIR", "/models")
TOKEN = os.environ.get("RUNNER_TOKEN") or os.environ.get("LADA_TOKEN", "")   # RUNNER_TOKEN preferred; LADA_TOKEN legacy
PORT = int(os.environ.get("PORT", "8711"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S", stream=sys.stdout)

# lada-cli progress format:
#   "Processing video:  88%|########  |Processed: 00:02 (84f) | Remaining: 00:12 | Speed: 5.3f/s"
_RE_FRAMES = re.compile(r"(\d+)\s*/\s*(\d+)")                      # generic "N/M" fallback
_RE_LADA_FRAME = re.compile(r"\((\d+)f\)")                          # "(84f)"
_RE_LADA_ETA = re.compile(r"Remaining:\s*(\d+):(\d+)(?::(\d+))?")   # mm:ss or hh:mm:ss
_RE_FPS = re.compile(r"([\d.]+)\s*(?:f/s|frame/s|frames/s|it/s|fps)", re.IGNORECASE)
_RE_PCT = re.compile(r"(\d{1,3})\s*%")

_HAVE_PGID = hasattr(os, "killpg") and hasattr(os, "getpgid")
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm"}

# one job at a time
_active = {"proc": None, "cancel": False}
_active_lock = threading.Lock()
_jobs = {}
_jobs_lock = threading.Lock()
_work = queue.Queue()
_running_id = None

LOG_MAX = 600
_logs = {}
_logseq = {}
_logs_lock = threading.Lock()


def push_log(jid, text, level="proc"):
    text = str(text or "").strip()
    if not text:
        return
    if len(text) > 400:
        text = text[:400] + "…"
    with _logs_lock:
        dq = _logs.get(jid)
        if dq is None:
            dq = _logs[jid] = deque(maxlen=LOG_MAX)
        seq = _logseq.get(jid, 0) + 1
        _logseq[jid] = seq
        dq.append({"seq": seq, "t": round(time.time(), 3), "level": level, "text": text})


def log_cursor(jid):
    with _logs_lock:
        return _logseq.get(jid, 0)


def log_since(jid, after):
    with _logs_lock:
        dq = _logs.get(jid)
        return [dict(x) for x in dq if x["seq"] > after] if dq else []


def set_job(jid, **f):
    with _jobs_lock:
        j = _jobs.get(jid)
        if j:
            j.update(f)


def public(job):
    out = {k: v for k, v in job.items() if not k.startswith("_")}
    sa = job.get("started_at")
    if sa:
        out["elapsed"] = int((job.get("_ended_at") or time.time()) - sa)
    out["log_cursor"] = log_cursor(job["id"])
    return out


# --------------------------------------------------------------------------- #
# subprocess control (process-group so lada-cli + its ffmpeg pause/die together)
# --------------------------------------------------------------------------- #

def _signal(sig):
    with _active_lock:
        proc = _active["proc"]
    if not proc or proc.poll() is not None:
        return False
    try:
        if _HAVE_PGID:
            os.killpg(os.getpgid(proc.pid), sig)
        else:
            proc.send_signal(sig)
        return True
    except (ProcessLookupError, OSError):
        return False


def cancel():
    with _active_lock:
        _active["cancel"] = True
    return _signal(getattr(signal, "SIGKILL", signal.SIGTERM))


def pause():
    return _signal(signal.SIGSTOP) if hasattr(signal, "SIGSTOP") else False


def resume():
    return _signal(signal.SIGCONT) if hasattr(signal, "SIGCONT") else False


def _newest_video(d):
    vids = [os.path.join(d, f) for f in os.listdir(d)
            if os.path.splitext(f)[1].lower() in VIDEO_EXTS]
    vids.sort(key=os.path.getmtime)
    return vids[-1] if vids else None


def _stage_of(line):
    low = line.lower()
    if "upscal" in low:
        return "upscale"
    if "detect" in low:
        return "detection"
    if "restor" in low or "export" in low or "encod" in low or "clip" in low:
        return "restoration"
    return None


# --------------------------------------------------------------------------- #
# live before/after frame extraction
#
# The output is a growing fragmented mp4, whose container duration reads 0
# mid-write — so we derive the encode position from our own parsed progress
# (frames done / source fps) and extract with the bundled ffmpeg 7.x. JPEGs
# land in <out_dir>/.preview/ and are served at /jobs/<id>/preview/*.jpg.
# --------------------------------------------------------------------------- #

def _src_fps(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=r_frame_rate",
                            "-of", "default=nw=1:nk=1", path],
                           capture_output=True, text=True, timeout=15)
        num, _, den = r.stdout.strip().partition("/")
        return float(num) / float(den or 1)
    except Exception:  # noqa: BLE001
        return None


def _grab(src, t, dest):
    """Extract one frame; returns (ok, error-string)."""
    tmp = dest + ".tmp"
    try:
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                            "-ss", str(max(0.0, t)), "-i", src,
                            "-frames:v", "1", "-vf", "scale=480:-2",
                            "-q:v", "4", "-f", "image2", tmp],
                           capture_output=True, text=True, timeout=25)
        if r.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, dest)
            return True, ""
        err = (r.stderr or "").strip()[-200:] or ("rc=%s empty output" % r.returncode)
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False, err


def _preview_loop(jid, out_dir, input_path, stop_evt):
    pdir = os.path.join(out_dir, ".preview")
    os.makedirs(pdir, exist_ok=True)
    fps = _src_fps(input_path)
    if not fps:
        push_log(jid, "preview: could not read source fps; preview disabled", "warn")
        return
    announced = warned = False
    misses = 0
    while not stop_evt.wait(2.0):
        try:
            with _jobs_lock:
                j = _jobs.get(jid)
                frame = j.get("frame") if j else None
            if not frame:
                continue
            t = frame / fps - 2.0        # trail the encoder by a safety margin
            if t < 1.0:
                continue
            part = _newest_video(out_dir)
            if not part:
                continue
            ok, err = _grab(part, t, os.path.join(pdir, "after.jpg"))
            if ok:
                misses = 0
                _grab(input_path, t, os.path.join(pdir, "before.jpg"))
                if not announced:
                    announced = True
                    push_log(jid, "preview: live frames available", "event")
            else:
                # The first attempts routinely race the encoder's first fragment
                # flush ("Invalid data found") and self-heal — only warn when it
                # keeps failing, which indicates a real problem.
                misses += 1
                if misses >= 5 and not warned:
                    warned = True
                    push_log(jid, "preview grabs failing repeatedly: " + err, "warn")
        except Exception as exc:  # noqa: BLE001 - preview must never break the job
            if not warned:
                warned = True
                push_log(jid, "preview loop error: " + str(exc), "warn")


def preview_path(jid, which):
    with _jobs_lock:
        j = _jobs.get(jid)
    if not j:
        return None
    p = os.path.join(j["_opts"]["output_dir"], ".preview", which + ".jpg")
    return p if os.path.isfile(p) else None


def read_gpu():
    """GPU telemetry via the runtime-injected nvidia-smi. The (GPU-less) worker
    polls this to show live stats in the dashboard."""
    gid = os.environ.get("GPU_ID", "0")
    if str(gid).strip() in ("", "-1"):
        return {}
    try:
        q = "utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"
        out = subprocess.run(
            ["nvidia-smi", "-i", str(gid), "--query-gpu=" + q, "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        if out.returncode != 0 or not out.stdout.strip():
            return {}
        vals = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]
        if len(vals) < 5:
            return {}

        def numf(s):
            try:
                return float(s)
            except (ValueError, TypeError):
                return None
        return {"util": numf(vals[0]), "mem_used": numf(vals[1]), "mem_total": numf(vals[2]),
                "temp": numf(vals[3]), "power": numf(vals[4])}
    except Exception:  # noqa: BLE001
        return {}


def _lada_argv(o, input_path, out_dir, encoder):
    return [
        LADA_CLI,
        "--input", input_path,
        "--output", out_dir,
        "--device", o.get("device") or "cuda",
        ("--fp16" if o.get("fp16") else "--no-fp16"),
        "--mosaic-restoration-model", o.get("restoration_model") or "basicvsrpp-v1.2",
        "--mosaic-detection-model", o.get("detection_model") or "v4-fast",
        "--encoder", encoder,
        "--mp4-fast-start",                # fragmented mp4: the growing file is decodable
        "--temporary-directory", out_dir,  # keep the in-progress file on the shared mount
    ]


def _upscale_argv(o, input_path, out_dir, encoder):
    argv = [VENV_PY, UPSCALE_SCRIPT, "--input", input_path,
            "--output-dir", out_dir, "--encoder", encoder,
            "--device", o.get("device") or "cuda"]
    if o.get("upscale_model"):
        argv += ["--model", o["upscale_model"]]
    return argv


def _run_phase(jid, argv, phase_idx, n_phases):
    """Run one tool subprocess, streaming progress into the job. Progress is
    mapped into the phase's slice of [0,1] so a decensor+upscale chain doesn't
    jump backwards. Returns (returncode, cancelled)."""
    push_log(jid, "$ " + " ".join(argv), "event")
    env = os.environ.copy()
    env["LADA_MODEL_WEIGHTS_DIR"] = MODELS_DIR
    kw = {"cwd": LADA_DIR, "env": env, "stdout": subprocess.PIPE,
          "stderr": subprocess.STDOUT, "text": True, "bufsize": 1}
    if _HAVE_PGID:
        kw["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **kw)
    except FileNotFoundError as exc:
        push_log(jid, "ERROR: " + str(exc), "error")
        return 127, False
    with _active_lock:
        _active["proc"] = proc

    last_stage = None
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            push_log(jid, line, "proc")
            fields = {}
            st = _stage_of(line)
            if st and st != last_stage:
                last_stage = st
                fields["stage"] = st
            pct = None
            pm = _RE_PCT.search(line)
            if pm:
                pct = min(100, int(pm.group(1))) / 100.0
                fields["progress"] = round((phase_idx + pct) / n_phases, 3)
            lf = _RE_LADA_FRAME.search(line)
            if lf:                                   # "(84f)" done-frames
                fr = int(lf.group(1))
                fields["frame"] = fr
                if pct and pct > 0.01:               # estimate total from pct
                    fields["total_frames"] = max(fr, int(round(fr / pct)))
            else:
                m = _RE_FRAMES.search(line)          # generic "N/M" fallback
                if m and int(m.group(2)):
                    fr, tot = int(m.group(1)), int(m.group(2))
                    fields["frame"] = fr
                    fields["total_frames"] = tot
                    fields["progress"] = round((phase_idx + min(1.0, fr / tot)) / n_phases, 3)
            fp = _RE_FPS.search(line)
            if fp:
                try:
                    fields["fps"] = float(fp.group(1))
                except ValueError:
                    pass
            et = _RE_LADA_ETA.search(line)
            if et:
                h, m2, s = et.group(1), et.group(2), et.group(3)
                fields["eta"] = (int(h) * 3600 + int(m2) * 60 + int(s)) if s else (int(h) * 60 + int(m2))
            if fields:
                set_job(jid, **fields)
        proc.wait()
    finally:
        with _active_lock:
            cancelled = _active["cancel"]
            _active["proc"] = None
    return proc.returncode, cancelled


def run_job(job):
    jid = job["id"]
    o = job["_opts"]
    out_dir = o["output_dir"]
    os.makedirs(out_dir, exist_ok=True)
    op = o.get("op") or "decensor"
    # Encoder priority: per-job override > LADA_DEFAULT_ENCODER (set by the
    # entrypoint's NVENC probe) > libx264.
    encoder = o.get("encoder") or os.environ.get("LADA_DEFAULT_ENCODER", "libx264")

    set_job(jid, state="running", message="Starting " + op, started_at=time.time())
    with _active_lock:
        _active["cancel"] = False
    stop_preview = threading.Event()
    threading.Thread(target=_preview_loop, args=(jid, out_dir, o["input"], stop_preview),
                     daemon=True).start()

    # Phase plan. For the chain, phase 2 consumes phase 1's newest output.
    phases = []                       # list of (name, argv_builder)
    if op in ("decensor", "decensor+upscale"):
        phases.append(("lada", _lada_argv))
    if op in ("upscale", "decensor+upscale"):
        phases.append(("upscale", _upscale_argv))
    if not phases:
        set_job(jid, state="error", message="unknown op " + op, error="unknown op " + op,
                _ended_at=time.time())
        return

    try:
        current_input = o["input"]
        for idx, (name, build) in enumerate(phases):
            set_job(jid, message="Running " + name)
            rc, cancelled = _run_phase(jid, build(o, current_input, out_dir, encoder),
                                       idx, len(phases))
            if cancelled or rc is None:
                set_job(jid, state="cancelled", message="Cancelled", _ended_at=time.time())
                push_log(jid, "Cancelled", "warn")
                return
            if rc != 0:
                msg = "%s exited %s" % (name, rc)
                set_job(jid, state="error", message=msg, error=msg, _ended_at=time.time())
                push_log(jid, "ERROR: " + msg, "error")
                return
            nxt = _newest_video(out_dir)
            if not nxt:
                msg = "%s produced no video" % name
                set_job(jid, state="error", message=msg, error=msg, _ended_at=time.time())
                push_log(jid, "ERROR: " + msg, "error")
                return
            current_input = nxt
    finally:
        stop_preview.set()

    set_job(jid, state="done", progress=1.0, message="Done", output_path=current_input,
            _ended_at=time.time())
    push_log(jid, "Done -> " + current_input, "event")


def worker_loop():
    global _running_id
    while True:
        jid = _work.get()
        with _jobs_lock:
            j = _jobs.get(jid)
            if j and j.get("state") == "cancelled":
                j = None
            _running_id = jid if j else None
        if not j:
            continue
        try:
            run_job(j)
        except Exception as exc:  # noqa: BLE001
            logging.exception("job %s failed", jid)
            set_job(jid, state="error", message=str(exc), error=str(exc), _ended_at=time.time())
        finally:
            with _jobs_lock:
                _running_id = None


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "stashify-runner/1.0"

    def log_message(self, fmt, *args):
        logging.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if not TOKEN:
            return True
        got = self.headers.get("X-Runner-Token") or self.headers.get("X-Lada-Token", "")
        return got == TOKEN

    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TypeError):
            return {}

    def do_GET(self):
        raw = self.path.split("?", 1)[0].rstrip("/")
        if raw == "/ping":            # unauth discovery beacon (no sensitive data)
            import socket
            return self._send(200, {"stashify": True,
                                    "node": os.environ.get("NODE_NAME") or socket.gethostname(),
                                    "kind": "linux", "ops": ["decensor", "upscale", "decensor+upscale"],
                                    "engines": {"decensor": "lada", "decensor+upscale": "lada", "upscale": "span"}})
        if raw == "/health" or raw == "":
            models = sorted(os.listdir(MODELS_DIR)) if os.path.isdir(MODELS_DIR) else []
            with _jobs_lock:
                busy = _running_id is not None
            return self._send(200, {"ok": True, "device": os.environ.get("RUNNER_DEVICE") or os.environ.get("LADA_DEVICE", "cuda"),
                                    "models": models, "busy": busy,
                                    "ops": ["decensor", "upscale", "decensor+upscale"],
                                    "engines": {"decensor": "lada", "decensor+upscale": "lada", "upscale": "span"}})
        if raw == "/gpu":
            return self._send(200, read_gpu())
        m = re.match(r"^/jobs/([0-9a-f]+)/preview/(before|after)\.jpg$", raw)
        if m:
            p = preview_path(m.group(1), m.group(2))
            if not p:
                return self._send(404, {"error": "no preview yet"})
            try:
                with open(p, "rb") as fh:
                    body = fh.read()
            except OSError:
                return self._send(404, {"error": "no preview yet"})
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if not self._authed():
            return self._send(401, {"error": "bad token"})
        m = re.match(r"^/jobs/([0-9a-f]+)/log$", raw)
        if m:
            from urllib.parse import urlparse, parse_qs
            after = int((parse_qs(urlparse(self.path).query).get("after", ["0"])[0]) or 0)
            jid = m.group(1)
            with _jobs_lock:
                exists = jid in _jobs
            if not exists:
                return self._send(404, {"error": "no such job"})
            return self._send(200, {"cursor": log_cursor(jid), "lines": log_since(jid, after)})
        m = re.match(r"^/jobs/([0-9a-f]+)$", raw)
        if m:
            with _jobs_lock:
                j = _jobs.get(m.group(1))
            return self._send(200, public(j)) if j else self._send(404, {"error": "no such job"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        raw = self.path.split("?", 1)[0].rstrip("/")
        if not self._authed():
            return self._send(401, {"error": "bad token"})
        if raw == "/run":
            b = self._body()
            if not b.get("input") or not b.get("output_dir"):
                return self._send(400, {"error": "input and output_dir required"})
            jid = uuid.uuid4().hex[:12]
            job = {"id": jid, "state": "queued", "progress": 0.0, "message": "Queued",
                   "stage": None, "frame": None, "total_frames": None, "fps": None,
                   "output_path": None, "error": None, "started_at": None, "_opts": b}
            with _jobs_lock:
                _jobs[jid] = job
            _work.put(jid)
            return self._send(202, public(job))
        m = re.match(r"^/jobs/([0-9a-f]+)/(cancel|pause|resume)$", raw)
        if m:
            jid, action = m.group(1), m.group(2)
            with _jobs_lock:
                j = _jobs.get(jid)
                running = (_running_id == jid)
            if not j:
                return self._send(404, {"error": "no such job"})
            if action == "cancel":
                if running:
                    cancel()
                elif j["state"] == "queued":
                    set_job(jid, state="cancelled", message="Cancelled", _ended_at=time.time())
                return self._send(202, {"ok": True})
            if not running:
                return self._send(409, {"error": "job is not running"})
            ok = pause() if action == "pause" else resume()
            return self._send(202 if ok else 409, {"ok": ok})
        return self._send(404, {"error": "not found"})


def main():
    threading.Thread(target=worker_loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    logging.info("stashify-runner listening on :%s (token %s, models %s)",
                 PORT, "on" if TOKEN else "off", MODELS_DIR)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
