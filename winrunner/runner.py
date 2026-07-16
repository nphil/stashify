"""Stashify Windows runner — compute node #2.

A protocol-compatible sibling of lada_runner.py that runs on a Windows desktop
and puts BOTH GPUs to work:
  - AI lane      (NVIDIA, e.g. RTX 3080): upscale (SPAN, fp16) [+ decensor later]
  - Transcode lane (Intel iGPU QuickSync): distributed transcoding

The two lanes run concurrently (different hardware). The Stashify coordinator on
the NAS dispatches jobs here exactly as it does the P40 runner; this process
translates the container paths it receives (/stuff2/..., /scratch/...) to how
this machine reaches the same files (SMB/UNC), does the work as a subprocess,
and hands the output back on the shared /scratch mount.

Runs headless as a Windows service (see install-service.ps1); the tray app and
the served dashboard at http://localhost:<port>/ talk to it over HTTP.

Stdlib only (the heavy libs live in the venv the subprocesses use). psutil is
used for cross-tree suspend/resume/kill since Windows has no process groups.
"""
import os
import re
import sys
import json
import time
import uuid
import queue
import shutil
import logging
import subprocess
import threading
import mimetypes
import posixpath
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import psutil
except ImportError:
    psutil = None

HERE = os.path.dirname(os.path.abspath(__file__))
IS_WIN = os.name == "nt"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S", stream=sys.stdout)
log = logging.getLogger("stashify-runner")


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

def _expand(v):
    return os.path.expandvars(v) if isinstance(v, str) else v


def load_config():
    path = os.environ.get("STASHIFY_RUNNER_CONFIG") or os.path.join(
        os.environ.get("LOCALAPPDATA", HERE), "StashifyRunner", "config.json")
    cfg = {}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    else:
        log.warning("no config at %s — using defaults/example", path)
        ex = os.path.join(HERE, "config.example.json")
        if os.path.isfile(ex):
            with open(ex, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
    cfg.setdefault("node_name", os.environ.get("COMPUTERNAME", "windows-runner"))
    cfg.setdefault("port", 8712)
    cfg.setdefault("token", "")
    cfg.setdefault("path_map", [])
    cfg.setdefault("lanes", {"ai": True, "transcode": True})
    cfg.setdefault("ai_encoder", "auto")
    cfg.setdefault("transcode_encoder", "auto")
    cfg.setdefault("ai_fp16", True)
    cfg.setdefault("ai_gpu_index", 0)
    cfg.setdefault("copy_local", False)
    cfg["venv_python"] = _expand(cfg.get("venv_python") or sys.executable)
    cfg["upscale_model"] = _expand(cfg.get("upscale_model") or "")
    cfg["local_temp"] = _expand(cfg.get("local_temp") or os.path.join(HERE, "tmp"))
    cfg["ffmpeg"] = resolve_ffmpeg(cfg.get("ffmpeg", "auto"))
    # longest prefix first, so /stuff2 beats /stuff
    cfg["path_map"] = sorted(
        [{"prefix": p["prefix"].rstrip("/"), "local": _expand(p["local"])} for p in cfg["path_map"]],
        key=lambda x: len(x["prefix"]), reverse=True)
    return cfg


def resolve_ffmpeg(val):
    if val and val != "auto" and os.path.isfile(_expand(val)):
        return _expand(val)
    found = shutil.which("ffmpeg")
    if found:
        return found
    # winget/Gyan default install location
    import glob
    for pat in [os.path.join(os.environ.get("LOCALAPPDATA", ""),
                             "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "*", "bin", "ffmpeg.exe")]:
        hits = glob.glob(pat)
        if hits:
            return hits[0]
    return "ffmpeg"


CFG = load_config()
TOKEN = CFG["token"]
PORT = int(CFG["port"])
FFMPEG = CFG["ffmpeg"]
FFPROBE = (FFMPEG[:-len("ffmpeg.exe")] + "ffprobe.exe") if FFMPEG.lower().endswith("ffmpeg.exe") \
    else (shutil.which("ffprobe") or "ffprobe")
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm"}


# --------------------------------------------------------------------------- #
# path translation (container <-> this machine)
# --------------------------------------------------------------------------- #

def to_local(container_path):
    """/stuff2/a/b.mp4 -> \\\\192.168.1.69\\Stuff\\a\\b.mp4 (per path_map)."""
    p = container_path.replace("\\", "/")
    for m in CFG["path_map"]:
        if p == m["prefix"] or p.startswith(m["prefix"] + "/"):
            rest = p[len(m["prefix"]):].lstrip("/")
            local = m["local"]
            if rest:
                sep = "\\" if IS_WIN else "/"
                local = local.rstrip("\\/") + sep + rest.replace("/", sep)
            return local
    return container_path  # unmapped: pass through (may already be local)


def to_container(local_path):
    """Reverse: local output path -> the container path the coordinator reads."""
    lp = local_path.replace("/", "\\") if IS_WIN else local_path
    for m in CFG["path_map"]:
        base = m["local"].replace("/", "\\") if IS_WIN else m["local"]
        if lp.lower() == base.lower() or lp.lower().startswith(base.lower().rstrip("\\/") + ("\\" if IS_WIN else "/")):
            rest = lp[len(base.rstrip("\\/")):].lstrip("\\/")
            return (m["prefix"] + "/" + rest.replace("\\", "/")).replace("//", "/")
    return local_path


# --------------------------------------------------------------------------- #
# progress parsing (uniform: both upscale_cli and transcode_cli emit lada-style)
#   "upscaling: 42%| |Processed: 00:12 (1234f) | Remaining: 01:23 | Speed: 12.3f/s"
# --------------------------------------------------------------------------- #

_RE_FRAME = re.compile(r"\((\d+)f\)")
_RE_ETA = re.compile(r"Remaining:\s*(\d+):(\d+)(?::(\d+))?")
_RE_FPS = re.compile(r"([\d.]+)\s*f/s", re.IGNORECASE)
_RE_SPEED = re.compile(r"(\d+\.?\d*)x(?!\d)")   # "3.37x" but not the 1080 in "1080x1920"
_RE_PCT = re.compile(r"(\d{1,3})\s*%")
_RE_NM = re.compile(r"(\d+)\s*/\s*(\d+)")


def parse_progress(line):
    f = {}
    pm = _RE_PCT.search(line)
    if pm:
        f["progress"] = round(min(100, int(pm.group(1))) / 100.0, 3)
    lf = _RE_FRAME.search(line)
    if lf:
        f["frame"] = int(lf.group(1))
        if f.get("progress", 0) > 0.01:
            f["total_frames"] = max(f["frame"], int(round(f["frame"] / f["progress"])))
    elif not pm:
        nm = _RE_NM.search(line)
        if nm and int(nm.group(2)):
            f["frame"], f["total_frames"] = int(nm.group(1)), int(nm.group(2))
            f["progress"] = round(min(1.0, f["frame"] / f["total_frames"]), 3)
    fp = _RE_FPS.search(line)
    if fp:
        try:
            f["fps"] = float(fp.group(1))
        except ValueError:
            pass
    sp = _RE_SPEED.search(line)
    if sp:
        try:
            f["speed"] = float(sp.group(1))
        except ValueError:
            pass
    et = _RE_ETA.search(line)
    if et:
        h, m2, s = et.group(1), et.group(2), et.group(3)
        f["eta"] = (int(h) * 3600 + int(m2) * 60 + int(s)) if s else (int(h) * 60 + int(m2))
    return f


# --------------------------------------------------------------------------- #
# jobs + per-job log ring buffer
# --------------------------------------------------------------------------- #

_jobs = {}
_jobs_lock = threading.Lock()
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
        dq = _logs.get(jid) or _logs.setdefault(jid, deque(maxlen=LOG_MAX))
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
# compute lane: one queue + one worker thread, dedicated to one GPU
# --------------------------------------------------------------------------- #

def _newest_video(d):
    try:
        vids = [os.path.join(d, f) for f in os.listdir(d)
                if os.path.splitext(f)[1].lower() in VIDEO_EXTS and not f.endswith(".part")]
    except OSError:
        return None
    vids.sort(key=os.path.getmtime)
    return vids[-1] if vids else None


class Lane:
    def __init__(self, name, gpu_label, ops):
        self.name = name
        self.gpu_label = gpu_label
        self.ops = ops
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.proc = None          # live subprocess.Popen
        self.cancel = False
        self.paused = False
        self.running_id = None
        threading.Thread(target=self._loop, daemon=True, name="lane-" + name).start()

    def submit(self, jid):
        self.q.put(jid)

    def busy(self):
        with self.lock:
            return self.running_id is not None

    # ---- process control (Windows-safe via psutil) ----
    def _proc_tree(self):
        with self.lock:
            p = self.proc
        if not p or p.poll() is not None or not psutil:
            return None
        try:
            return psutil.Process(p.pid)
        except Exception:  # noqa: BLE001
            return None

    def do_cancel(self):
        with self.lock:
            self.cancel = True
            p = self.proc
        proc = self._proc_tree()
        if proc:
            for c in proc.children(recursive=True):
                try:
                    c.kill()
                except Exception:  # noqa: BLE001
                    pass
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        elif p and p.poll() is None:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
        return True

    def do_pause(self):
        proc = self._proc_tree()
        if not proc:
            return False
        for t in [proc] + proc.children(recursive=True):
            try:
                t.suspend()
            except Exception:  # noqa: BLE001
                pass
        self.paused = True
        return True

    def do_resume(self):
        proc = self._proc_tree()
        if not proc:
            return False
        for t in [proc] + proc.children(recursive=True):
            try:
                t.resume()
            except Exception:  # noqa: BLE001
                pass
        self.paused = False
        return True

    def _loop(self):
        while True:
            jid = self.q.get()
            with _jobs_lock:
                job = _jobs.get(jid)
                if job and job.get("state") == "cancelled":
                    job = None
            if not job:
                continue
            with self.lock:
                self.running_id = jid
                self.cancel = False
                self.paused = False
            try:
                run_job(self, job)
            except Exception as exc:  # noqa: BLE001
                log.exception("job %s failed", jid)
                set_job(jid, state="error", message=str(exc), error=str(exc), _ended_at=time.time())
                push_log(jid, "ERROR: " + str(exc), "error")
            finally:
                with self.lock:
                    self.running_id = None
                    self.proc = None
                    self.paused = False


LANES = {}


def lane_for(op):
    return "ai" if op in ("upscale", "decensor", "decensor+upscale") else "transcode"


# --------------------------------------------------------------------------- #
# encoder probe (reused idea from the P40 fix: NVENC needs a modern driver)
# --------------------------------------------------------------------------- #

_enc_cache = {}


def probe_encoder(prefer):
    """Return the first working encoder. 'auto' tries nvenc -> qsv -> x264."""
    if prefer and prefer != "auto":
        return prefer
    if "auto" in _enc_cache:
        return _enc_cache["auto"]
    for enc in ("hevc_nvenc", "hevc_qsv", "libx264"):
        try:
            r = subprocess.run(
                [FFMPEG, "-hide_banner", "-f", "lavfi", "-i",
                 "testsrc=duration=1:size=256x256:rate=10", "-pix_fmt", "yuv420p",
                 "-c:v", enc, "-f", "null", "-"],
                capture_output=True, text=True, timeout=30,
                creationflags=(subprocess.CREATE_NO_WINDOW if IS_WIN else 0))
            if r.returncode == 0:
                _enc_cache["auto"] = enc
                log.info("encoder probe: using %s", enc)
                return enc
        except Exception:  # noqa: BLE001
            continue
    _enc_cache["auto"] = "libx264"
    return "libx264"


# --------------------------------------------------------------------------- #
# the ops
# --------------------------------------------------------------------------- #

def _stream_subprocess(lane, jid, argv, n_phases=1, phase_idx=0):
    """Run a CLI, streaming its lada-style progress lines into the job."""
    push_log(jid, "$ " + subprocess.list2cmdline(argv), "event")
    kw = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(argv, **kw)
    with lane.lock:
        lane.proc = proc
    for line in proc.stdout:
        line = line.rstrip("\n")
        push_log(jid, line, "proc")
        f = parse_progress(line)
        if f:
            if "progress" in f:
                f["progress"] = round((phase_idx + f["progress"]) / n_phases, 3)
            set_job(jid, **f)
    proc.wait()
    with lane.lock:
        cancelled = lane.cancel
    return proc.returncode, cancelled


def run_job(lane, job):
    jid = job["id"]
    o = job["_opts"]
    op = o.get("op") or "upscale"
    out_dir_c = o["output_dir"]
    out_dir = to_local(out_dir_c)
    os.makedirs(out_dir, exist_ok=True)
    src = to_local(o["input"])

    set_job(jid, state="running", message="Starting " + op, started_at=time.time(), stage=op)

    # optional: copy the SMB source local first to avoid per-frame network stalls
    tmp_copy = None
    if CFG["copy_local"] and op in ("upscale", "decensor", "decensor+upscale"):
        os.makedirs(CFG["local_temp"], exist_ok=True)
        tmp_copy = os.path.join(CFG["local_temp"], "in_" + jid + os.path.splitext(src)[1])
        push_log(jid, "copying source local…", "event")
        shutil.copyfile(src, tmp_copy)
        src = tmp_copy

    # start the live-frame preview extractor (shared helper)
    stop_preview = threading.Event()
    threading.Thread(target=preview_loop, args=(jid, out_dir, src, stop_preview),
                     daemon=True).start()

    try:
        if op == "transcode":
            enc = o.get("encoder") or probe_encoder(CFG["transcode_encoder"])
            argv = [CFG["venv_python"], os.path.join(HERE, "transcode_cli.py"),
                    "--input", src, "--output-dir", out_dir,
                    "--ffmpeg", FFMPEG, "--ffprobe", FFPROBE, "--encoder", enc]
            for k in ("codec", "height", "quality", "container"):
                if o.get(k) not in (None, ""):
                    argv += ["--" + k, str(o[k])]
            rc, cancelled = _stream_subprocess(lane, jid, argv)
        elif op == "upscale":
            enc = o.get("encoder") or probe_encoder(CFG["ai_encoder"])
            argv = [CFG["venv_python"], os.path.join(HERE, "upscale_cli.py"),
                    "--input", src, "--output-dir", out_dir,
                    "--ffmpeg", FFMPEG, "--encoder", enc,
                    "--model", o.get("upscale_model") or CFG["upscale_model"],
                    "--device", "cuda:%d" % CFG["ai_gpu_index"]]
            if CFG["ai_fp16"] and not o.get("no_fp16"):
                argv.append("--fp16")
            rc, cancelled = _stream_subprocess(lane, jid, argv)
        else:
            raise RuntimeError("unsupported op on this runner: " + op)
    finally:
        stop_preview.set()
        if tmp_copy and os.path.isfile(tmp_copy):
            try:
                os.remove(tmp_copy)
            except OSError:
                pass

    if cancelled:
        set_job(jid, state="cancelled", message="Cancelled", _ended_at=time.time())
        push_log(jid, "Cancelled", "warn")
        return
    if rc != 0:
        set_job(jid, state="error", message="%s exited %s" % (op, rc),
                error="%s exited %s" % (op, rc), _ended_at=time.time())
        return
    produced = _newest_video(out_dir)
    if not produced:
        set_job(jid, state="error", message="no output produced",
                error="no output produced", _ended_at=time.time())
        return
    set_job(jid, state="done", progress=1.0, message="Done",
            output_path=to_container(produced), _ended_at=time.time())
    push_log(jid, "Done -> " + to_container(produced), "event")


# --------------------------------------------------------------------------- #
# live before/after preview (same approach as lada_runner)
# --------------------------------------------------------------------------- #

def _src_fps(path):
    try:
        r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=r_frame_rate", "-of",
                            "default=nw=1:nk=1", path],
                           capture_output=True, text=True, timeout=20,
                           creationflags=(subprocess.CREATE_NO_WINDOW if IS_WIN else 0))
        num, _, den = r.stdout.strip().partition("/")
        return float(num) / float(den or 1)
    except Exception:  # noqa: BLE001
        return None


def _grab(src, t, dest):
    tmp = dest + ".tmp"
    try:
        r = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", str(max(0.0, t)),
                            "-i", src, "-frames:v", "1", "-vf", "scale=480:-2",
                            "-q:v", "4", "-f", "image2", tmp],
                           capture_output=True, text=True, timeout=25,
                           creationflags=(subprocess.CREATE_NO_WINDOW if IS_WIN else 0))
        if r.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, dest)
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False


def preview_loop(jid, out_dir, input_path, stop_evt):
    pdir = os.path.join(out_dir, ".preview")
    try:
        os.makedirs(pdir, exist_ok=True)
    except OSError:
        return
    fps = _src_fps(input_path)
    if not fps:
        return
    announced = False
    misses = 0
    while not stop_evt.wait(2.0):
        try:
            with _jobs_lock:
                j = _jobs.get(jid)
                frame = j.get("frame") if j else None
            if not frame:
                continue
            t = frame / fps - 2.0
            if t < 1.0:
                continue
            part = _newest_video(out_dir)
            if not part:
                continue
            if _grab(part, t, os.path.join(pdir, "after.jpg")):
                misses = 0
                _grab(input_path, t, os.path.join(pdir, "before.jpg"))
                if not announced:
                    announced = True
                    push_log(jid, "preview: live frames available", "event")
            else:
                misses += 1
        except Exception:  # noqa: BLE001
            pass


def preview_path(jid, which):
    with _jobs_lock:
        j = _jobs.get(jid)
    if not j:
        return None
    p = os.path.join(to_local(j["_opts"]["output_dir"]), ".preview", which + ".jpg")
    return p if os.path.isfile(p) else None


# --------------------------------------------------------------------------- #
# GPU / iGPU telemetry
# --------------------------------------------------------------------------- #

_gpu = {"nvidia": {}, "igpu": {}}
_gpu_lock = threading.Lock()


def _numf(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def read_nvidia():
    try:
        q = "utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"
        out = subprocess.run(["nvidia-smi", "-i", str(CFG["ai_gpu_index"]),
                              "--query-gpu=" + q, "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5,
                             creationflags=(subprocess.CREATE_NO_WINDOW if IS_WIN else 0))
        v = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]
        if len(v) >= 5:
            return {"name": "RTX (AI)", "util": _numf(v[0]), "mem_used": _numf(v[1]),
                    "mem_total": _numf(v[2]), "temp": _numf(v[3]), "power": _numf(v[4])}
    except Exception:  # noqa: BLE001
        pass
    return {}


def read_igpu():
    """Intel iGPU utilization from Windows perf counters (Video engines, summed)."""
    if not IS_WIN:
        return {}
    try:
        out = subprocess.run(
            ["typeperf", r"\GPU Engine(*engtype_Video*)\Utilization Percentage", "-sc", "1"],
            capture_output=True, text=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)
        lines = [l for l in out.stdout.splitlines() if l.startswith('"')]
        if len(lines) >= 2:
            vals = [float(x.strip('"')) for x in lines[-1].split(",")[1:] if x.strip('"').replace(".", "", 1).replace("-", "", 1).isdigit()]
            if vals:
                return {"name": "iGPU (QSV)", "util": round(min(100.0, sum(vals)), 1)}
    except Exception:  # noqa: BLE001
        pass
    return {}


def gpu_poller():
    while True:
        nv = read_nvidia()
        ig = read_igpu()
        with _gpu_lock:
            _gpu["nvidia"] = nv
            _gpu["igpu"] = ig
        time.sleep(3)


def gpu_snapshot():
    with _gpu_lock:
        return {"nvidia": dict(_gpu["nvidia"]), "igpu": dict(_gpu["igpu"])}


# --------------------------------------------------------------------------- #
# node-level pause (drain): stop pulling new jobs
# --------------------------------------------------------------------------- #

_node = {"paused": False}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def enabled_ops():
    ops = []
    if CFG["lanes"].get("ai"):
        ops += ["upscale"]
        if CFG.get("enable_decensor"):
            ops += ["decensor", "decensor+upscale"]
    if CFG["lanes"].get("transcode"):
        ops += ["transcode"]
    return ops


class Handler(BaseHTTPRequestHandler):
    server_version = "stashify-winrunner/1.0"

    def log_message(self, fmt, *args):
        logging.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        return not TOKEN or self.headers.get("X-Lada-Token", "") == TOKEN

    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TypeError):
            return {}

    def _send_file(self, path, ctype):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._send(404, {"error": "not found"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- GET ----
    def do_GET(self):
        raw = self.path.split("?", 1)[0].rstrip("/")
        if raw in ("", "/"):
            return self._send_file(os.path.join(HERE, "webui", "index.html"), "text/html")
        if raw == "/health":
            return self._send(200, {
                "ok": True, "node": CFG["node_name"], "kind": "windows",
                "ops": enabled_ops(), "encoders": {"ai": probe_encoder(CFG["ai_encoder"]),
                                                   "transcode": probe_encoder(CFG["transcode_encoder"])},
                "lanes": {n: {"busy": l.busy(), "paused": l.paused, "gpu": l.gpu_label,
                              "job": l.running_id} for n, l in LANES.items()},
                "paused": _node["paused"], "busy": any(l.busy() for l in LANES.values())})
        if raw == "/gpu":
            # coordinator-compatible: the AI GPU in the flat shape
            with _gpu_lock:
                return self._send(200, dict(_gpu["nvidia"]))
        if raw == "/stats":
            return self._send(200, {"gpus": gpu_snapshot(),
                                    "lanes": {n: {"busy": l.busy(), "paused": l.paused,
                                                  "job": l.running_id} for n, l in LANES.items()},
                                    "paused": _node["paused"]})
        m = re.match(r"^/jobs/([0-9a-f]+)/preview/(before|after)\.jpg$", raw)
        if m:
            p = preview_path(m.group(1), m.group(2))
            return self._send_file(p, "image/jpeg") if p else self._send(404, {"error": "no preview"})
        if not self._authed():
            return self._send(401, {"error": "bad token"})
        if raw == "/jobs":
            with _jobs_lock:
                return self._send(200, [public(j) for j in _jobs.values()])
        m = re.match(r"^/jobs/([0-9a-f]+)/log$", raw)
        if m:
            from urllib.parse import urlparse, parse_qs
            after = int((parse_qs(urlparse(self.path).query).get("after", ["0"])[0]) or 0)
            return self._send(200, {"cursor": log_cursor(m.group(1)),
                                    "lines": log_since(m.group(1), after)})
        m = re.match(r"^/jobs/([0-9a-f]+)$", raw)
        if m:
            with _jobs_lock:
                j = _jobs.get(m.group(1))
            return self._send(200, public(j)) if j else self._send(404, {"error": "no such job"})
        return self._send(404, {"error": "not found"})

    # ---- POST ----
    def do_POST(self):
        raw = self.path.split("?", 1)[0].rstrip("/")
        if not self._authed():
            return self._send(401, {"error": "bad token"})
        if raw == "/run":
            b = self._body()
            op = b.get("op") or "upscale"
            if op not in enabled_ops():
                return self._send(400, {"error": "op '%s' not supported on this node" % op})
            if not b.get("input") or not b.get("output_dir"):
                return self._send(400, {"error": "input and output_dir required"})
            if _node["paused"]:
                return self._send(409, {"error": "node is paused/draining"})
            jid = uuid.uuid4().hex[:12]
            job = {"id": jid, "state": "queued", "progress": 0.0, "message": "Queued",
                   "op": op, "lane": lane_for(op), "stage": None, "frame": None,
                   "total_frames": None, "fps": None, "speed": None, "eta": None,
                   "output_path": None, "error": None, "started_at": None, "_opts": b}
            with _jobs_lock:
                _jobs[jid] = job
            LANES[lane_for(op)].submit(jid)
            return self._send(202, public(job))
        m = re.match(r"^/jobs/([0-9a-f]+)/(cancel|pause|resume)$", raw)
        if m:
            jid, action = m.group(1), m.group(2)
            with _jobs_lock:
                j = _jobs.get(jid)
            if not j:
                return self._send(404, {"error": "no such job"})
            lane = LANES.get(j.get("lane"))
            running = lane and lane.running_id == jid
            if action == "cancel":
                if running:
                    lane.do_cancel()
                elif j["state"] == "queued":
                    set_job(jid, state="cancelled", message="Cancelled", _ended_at=time.time())
                return self._send(202, {"ok": True})
            if not running:
                return self._send(409, {"error": "job not running"})
            ok = lane.do_pause() if action == "pause" else lane.do_resume()
            if ok:
                set_job(jid, paused=(action == "pause"),
                        message="Paused" if action == "pause" else "Resumed")
            return self._send(202 if ok else 409, {"ok": ok})
        m = re.match(r"^/node/(pause|resume)$", raw)
        if m:
            _node["paused"] = (m.group(1) == "pause")
            return self._send(202, {"paused": _node["paused"]})
        return self._send(404, {"error": "not found"})


def main():
    global LANES
    if psutil is None:
        log.warning("psutil not installed — pause/resume + clean cancel unavailable")
    LANES = {}
    if CFG["lanes"].get("ai"):
        LANES["ai"] = Lane("ai", "nvidia", ["upscale"])
    if CFG["lanes"].get("transcode"):
        LANES["transcode"] = Lane("transcode", "igpu", ["transcode"])
    threading.Thread(target=gpu_poller, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("stashify-winrunner '%s' on :%s | ops=%s | ffmpeg=%s | token %s",
             CFG["node_name"], PORT, enabled_ops(), FFMPEG, "on" if TOKEN else "off")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
