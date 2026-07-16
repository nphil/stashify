"""Stashify Windows runner - compute node #2.

A protocol-compatible sibling of lada_runner.py that runs on a Windows desktop
and puts BOTH GPUs to work:
  - AI lane      (NVIDIA, e.g. RTX 3080): upscale (SPAN, fp16) [+ decensor later]
  - Transcode lane (Intel iGPU QuickSync): distributed transcoding

The two lanes run concurrently (different hardware). The Stashify coordinator on
the NAS dispatches jobs here exactly as it does the P40 runner; this process
translates the container paths it receives (/stuff2/..., /scratch/...) to how
this machine reaches the same files (SMB/UNC), does the work as a subprocess,
and hands the output back on the shared /scratch mount.

Runs headless as a Windows service (see install.ps1); the tray app and the
served dashboard at http://localhost:<port>/ talk to it over HTTP. All API
routes require the shared token; the served page has the token injected so the
same-origin dashboard works. With no token set the listener binds 127.0.0.1.

Stdlib only (the heavy libs live in the venv the subprocesses use). psutil is
used for cross-tree suspend/resume/kill since Windows has no process groups.
"""
import os
import re
import sys
import json
import time
import uuid
import hmac
import queue
import atexit
import shutil
import logging
import subprocess
import threading
from collections import deque, OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import psutil
except ImportError:
    psutil = None

HERE = os.path.dirname(os.path.abspath(__file__))
IS_WIN = os.name == "nt"
NOWIN = subprocess.CREATE_NO_WINDOW if IS_WIN else 0
STALL_SECS = int(os.environ.get("STASHIFY_STALL_SECS", "600"))   # kill a job with no output for this long
MAX_JOBS = 60                                                    # retained terminal jobs before eviction
MAX_BODY = 64 * 1024                                             # /run body cap

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S", stream=sys.stdout)
log = logging.getLogger("stashify-runner")


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

def _expand(v):
    return os.path.expandvars(v) if isinstance(v, str) else v


def resolve_ffmpeg(val):
    if val and val != "auto" and os.path.isfile(_expand(val)):
        return _expand(val)
    found = shutil.which("ffmpeg")
    if found:
        return found
    import glob
    for pat in [os.path.join(os.environ.get("LOCALAPPDATA", ""),
                             "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "*", "bin", "ffmpeg.exe")]:
        hits = glob.glob(pat)
        if hits:
            return hits[0]
    return "ffmpeg"


def load_config():
    path = os.environ.get("STASHIFY_RUNNER_CONFIG") or os.path.join(
        os.environ.get("LOCALAPPDATA", HERE), "StashifyRunner", "config.json")
    cfg = {}
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8-sig") as fh:   # tolerate a BOM
                cfg = json.load(fh)
        else:
            log.warning("no config at %s - using example defaults", path)
            ex = os.path.join(HERE, "config.example.json")
            if os.path.isfile(ex):
                with open(ex, "r", encoding="utf-8-sig") as fh:
                    cfg = json.load(fh)
    except Exception as exc:  # noqa: BLE001 - degrade, don't crash-loop
        log.error("config load failed (%s); continuing with defaults", exc)
        cfg = {}
    cfg.setdefault("node_name", os.environ.get("COMPUTERNAME", "windows-runner"))
    cfg.setdefault("port", 8712)
    cfg.setdefault("token", os.environ.get("LADA_TOKEN", ""))
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
    cfg["path_map"] = sorted(
        [{"prefix": p["prefix"].rstrip("/"), "local": _expand(p["local"])} for p in cfg["path_map"]],
        key=lambda x: len(x["prefix"]), reverse=True)
    # trust boundary: only models under this dir may be loaded (pickle RCE guard)
    cfg["models_dir"] = os.path.dirname(cfg["upscale_model"]) if cfg["upscale_model"] else \
        os.path.join(os.environ.get("LOCALAPPDATA", HERE), "StashifyRunner", "models")
    return cfg


CFG = load_config()
TOKEN = CFG["token"]
PORT = int(CFG["port"])
FFMPEG = CFG["ffmpeg"]
FFPROBE = (FFMPEG[:-len("ffmpeg.exe")] + "ffprobe.exe") if FFMPEG.lower().endswith("ffmpeg.exe") \
    else (shutil.which("ffprobe") or "ffprobe")
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm"}
# child processes must speak UTF-8 (Japanese filenames etc.)
CHILD_ENV = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")


# --------------------------------------------------------------------------- #
# path translation (container <-> this machine), with containment + long paths
# --------------------------------------------------------------------------- #

def _longpath(p):
    """Prefix a Windows UNC/drive path so >260-char paths work."""
    if not IS_WIN or p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p.lstrip("\\")
    if len(p) >= 3 and p[1] == ":":
        return "\\\\?\\" + p
    return p


def to_local(container_path, longpath=True):
    """/stuff2/a/b.mp4 -> \\\\192.168.1.69\\Stuff\\a\\b.mp4. Returns None if the
    path isn't under any configured prefix (containment)."""
    p = str(container_path).replace("\\", "/")
    if ".." in p.split("/"):
        return None
    for m in CFG["path_map"]:
        if p == m["prefix"] or p.startswith(m["prefix"] + "/"):
            rest = p[len(m["prefix"]):].lstrip("/")
            local = m["local"]
            if rest:
                sep = "\\" if IS_WIN else "/"
                local = local.rstrip("\\/") + sep + rest.replace("/", sep)
            return _longpath(local) if longpath else local
    return None


def to_container(local_path):
    lp = local_path.replace("\\\\?\\UNC\\", "\\\\").replace("\\\\?\\", "")
    lp = lp.replace("/", "\\") if IS_WIN else lp
    for m in sorted(CFG["path_map"], key=lambda x: len(x["local"]), reverse=True):
        base = m["local"].replace("/", "\\") if IS_WIN else m["local"]
        sep = "\\" if IS_WIN else "/"
        if lp.lower() == base.lower() or lp.lower().startswith(base.rstrip("\\/").lower() + sep):
            rest = lp[len(base.rstrip("\\/")):].lstrip("\\/")
            return (m["prefix"] + "/" + rest.replace("\\", "/")).replace("//", "/")
    return local_path


# --------------------------------------------------------------------------- #
# progress parsing - anchored to the lada-style lines our CLIs emit
#   "upscaling: 42%| |Processed: 00:12 (1234f) | Remaining: 01:23 | Speed: 12.3f/s"
# --------------------------------------------------------------------------- #

_RE_FRAME = re.compile(r"\((\d+)f\)")
_RE_ETA = re.compile(r"Remaining:\s*(\d+):(\d+)(?::(\d+))?")
_RE_FPS = re.compile(r"([\d.]+)\s*f/s", re.IGNORECASE)
_RE_SPEED = re.compile(r"(\d+\.?\d*)x(?!\d)")
_RE_PCT = re.compile(r"(\d{1,3})\s*%")
_PROGRESS_PREFIX = re.compile(r"^\s*(upscaling|transcode|decensor|processing)", re.IGNORECASE)


def parse_progress(line):
    # only trust our own progress lines, not incidental ratios in tool output
    if not _PROGRESS_PREFIX.match(line) and "(" not in line:
        return {}
    if not _PROGRESS_PREFIX.match(line) and not _RE_FRAME.search(line):
        return {}
    f = {}
    pm = _RE_PCT.search(line)
    if pm:
        f["progress"] = round(min(100, int(pm.group(1))) / 100.0, 3)
    lf = _RE_FRAME.search(line)
    if lf:
        f["frame"] = int(lf.group(1))
        if f.get("progress", 0) > 0.01:
            f["total_frames"] = max(f["frame"], int(round(f["frame"] / f["progress"])))
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
# jobs + per-job log ring buffer (bounded)
# --------------------------------------------------------------------------- #

_jobs = OrderedDict()
_jobs_lock = threading.Lock()
LOG_MAX = 600
_logs = {}
_logseq = {}
_logs_lock = threading.Lock()


def _evict_jobs():
    # keep active jobs + the most recent terminal ones
    terminal = [jid for jid, j in _jobs.items()
                if j.get("state") in ("done", "error", "cancelled")]
    while len(_jobs) > MAX_JOBS and terminal:
        jid = terminal.pop(0)
        _jobs.pop(jid, None)
        with _logs_lock:
            _logs.pop(jid, None)
            _logseq.pop(jid, None)


def push_log(jid, text, level="proc"):
    text = str(text or "").strip()
    if not text:
        return
    if len(text) > 400:
        text = text[:400] + "..."
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
    def __init__(self, name, gpu_label):
        self.name = name
        self.gpu_label = gpu_label
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.proc = None
        self.cancel = False
        self.paused = False
        self.running_id = None
        threading.Thread(target=self._loop, daemon=True, name="lane-" + name).start()

    def submit(self, jid):
        self.q.put(jid)

    def busy(self):
        with self.lock:
            return self.running_id is not None

    def _tree(self):
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
        proc = self._tree()
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

    def _suspend_resume(self, resume):
        proc = self._tree()
        if not proc:
            return False
        for t in [proc] + proc.children(recursive=True):
            try:
                t.resume() if resume else t.suspend()
            except Exception:  # noqa: BLE001
                pass
        self.paused = not resume
        return True

    def do_pause(self):
        return self._suspend_resume(False)

    def do_resume(self):
        return self._suspend_resume(True)

    def _loop(self):
        while True:
            jid = self.q.get()
            with self.lock:
                self.running_id = jid
                self.cancel = False
                self.paused = False
            # re-check under lock: a cancel that raced the dequeue must win
            with _jobs_lock:
                job = _jobs.get(jid)
                skip = (not job) or job.get("state") == "cancelled"
            if skip:
                with self.lock:
                    self.running_id = None
                continue
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
# encoder capability probe - per lane, once at startup (never in a request)
# --------------------------------------------------------------------------- #

_enc_cache = {}
_enc_lock = threading.Lock()
AI_ORDER = ["hevc_nvenc", "hevc_qsv", "libx264"]         # NVENC-first (3080)
TX_ORDER = ["hevc_qsv", "hevc_nvenc", "libx264"]         # QSV-first (iGPU)


def _test_encoder(enc):
    try:
        r = subprocess.run(
            [FFMPEG, "-hide_banner", "-f", "lavfi", "-i",
             "testsrc=duration=1:size=256x256:rate=10", "-pix_fmt", "yuv420p",
             "-c:v", enc, "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=NOWIN)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def resolve_lane_encoder(lane):
    prefer = CFG["ai_encoder"] if lane == "ai" else CFG["transcode_encoder"]
    if prefer and prefer != "auto":
        return prefer
    key = "ai" if lane == "ai" else "transcode"
    with _enc_lock:
        if key in _enc_cache:
            return _enc_cache[key]
        order = AI_ORDER if lane == "ai" else TX_ORDER
        chosen = next((e for e in order if _test_encoder(e)), "libx264")
        _enc_cache[key] = chosen
        log.info("%s lane encoder: %s", lane, chosen)
        return chosen


# --------------------------------------------------------------------------- #
# the ops
# --------------------------------------------------------------------------- #

def _stream_subprocess(lane, jid, argv):
    """Run a CLI, streaming lada-style progress into the job. A watchdog kills a
    stalled subprocess (no output for STALL_SECS) so a dead SMB mount can't wedge
    the lane forever."""
    push_log(jid, "$ " + subprocess.list2cmdline(argv), "event")
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                            env=CHILD_ENV, creationflags=NOWIN)
    with lane.lock:
        lane.proc = proc
    last = {"t": time.time()}
    stop = threading.Event()

    def watchdog():
        while not stop.wait(15):
            if lane.paused:
                last["t"] = time.time()
                continue
            if time.time() - last["t"] > STALL_SECS:
                push_log(jid, "watchdog: no output for %ss - killing stalled job" % STALL_SECS, "error")
                lane.do_cancel()
                return
    threading.Thread(target=watchdog, daemon=True).start()

    try:
        for line in proc.stdout:
            last["t"] = time.time()
            line = line.rstrip("\n")
            push_log(jid, line, "proc")
            f = parse_progress(line)
            if f:
                set_job(jid, **f)
        proc.wait()
    finally:
        stop.set()
    with lane.lock:
        cancelled = lane.cancel
    return proc.returncode, cancelled


def _safe_model(o):
    """Only honor a client-supplied upscale_model if it lives under models_dir
    (defense against pickle-RCE via torch.load of an arbitrary path)."""
    m = o.get("upscale_model") or ""
    if m:
        try:
            real = os.path.realpath(to_local(m, longpath=False) or m)
            root = os.path.realpath(CFG["models_dir"])
            if os.path.commonpath([real, root]) == root and os.path.isfile(real):
                return real
        except Exception:  # noqa: BLE001
            pass
        push_log(o.get("_jid", ""), "ignoring out-of-tree upscale_model", "warn")
    return CFG["upscale_model"]


def run_job(lane, job):
    jid = job["id"]
    o = job["_opts"]
    o["_jid"] = jid
    op = o.get("op") or "upscale"
    out_dir = to_local(o["output_dir"])
    src = to_local(o["input"])
    if not out_dir or not src:
        raise RuntimeError("input/output_dir not within a configured path prefix")
    os.makedirs(out_dir, exist_ok=True)

    set_job(jid, state="running", message="Starting " + op, started_at=time.time(), stage=op)

    tmp_copy = None
    if CFG["copy_local"] and op in ("upscale", "decensor", "decensor+upscale"):
        os.makedirs(CFG["local_temp"], exist_ok=True)
        tmp_copy = os.path.join(CFG["local_temp"], "in_" + jid + os.path.splitext(src)[1])
        push_log(jid, "copying source local...", "event")
        shutil.copyfile(src, tmp_copy)
        src = tmp_copy

    stop_preview = threading.Event()
    threading.Thread(target=preview_loop, args=(jid, out_dir, src, stop_preview), daemon=True).start()

    try:
        if op == "transcode":
            enc = o.get("encoder") or resolve_lane_encoder("transcode")
            argv = [CFG["venv_python"], os.path.join(HERE, "transcode_cli.py"),
                    "--input", src, "--output-dir", out_dir,
                    "--ffmpeg", FFMPEG, "--ffprobe", FFPROBE, "--encoder", enc]
            for k in ("codec", "height", "quality", "container"):
                if o.get(k) not in (None, ""):
                    argv += ["--" + k, str(o[k])]
            rc, cancelled = _stream_subprocess(lane, jid, argv)
        elif op == "upscale":
            enc = o.get("encoder") or resolve_lane_encoder("ai")
            argv = [CFG["venv_python"], os.path.join(HERE, "upscale_cli.py"),
                    "--input", src, "--output-dir", out_dir,
                    "--ffmpeg", FFMPEG, "--encoder", enc,
                    "--model", _safe_model(o),
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
# live before/after preview
# --------------------------------------------------------------------------- #

def _src_fps(path):
    try:
        r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=r_frame_rate", "-of",
                            "default=nw=1:nk=1", path],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=20, creationflags=NOWIN)
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
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=25, creationflags=NOWIN)
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
            if part and _grab(part, t, os.path.join(pdir, "after.jpg")):
                _grab(input_path, t, os.path.join(pdir, "before.jpg"))
        except Exception:  # noqa: BLE001
            pass


def preview_path(jid, which):
    with _jobs_lock:
        j = _jobs.get(jid)
    if not j:
        return None
    od = to_local(j["_opts"]["output_dir"])
    if not od:
        return None
    p = os.path.join(od, ".preview", which + ".jpg")
    return p if os.path.isfile(p) else None


# --------------------------------------------------------------------------- #
# GPU / iGPU telemetry
# --------------------------------------------------------------------------- #

_gpu = {"nvidia": {}, "igpu": {}}
_gpu_lock = threading.Lock()


def _numf(s):
    try:
        return float(str(s).replace(",", "."))       # locale-tolerant
    except (ValueError, TypeError):
        return None


def read_nvidia():
    try:
        q = "utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"
        out = subprocess.run(["nvidia-smi", "-i", str(CFG["ai_gpu_index"]),
                              "--query-gpu=" + q, "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=5, creationflags=NOWIN)
        v = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]
        if len(v) >= 5:
            return {"name": "RTX (AI)", "util": _numf(v[0]), "mem_used": _numf(v[1]),
                    "mem_total": _numf(v[2]), "temp": _numf(v[3]), "power": _numf(v[4])}
    except Exception:  # noqa: BLE001
        pass
    return {}


def read_igpu():
    if not IS_WIN:
        return {}
    try:
        out = subprocess.run(
            ["typeperf", r"\GPU Engine(*engtype_Video*)\Utilization Percentage", "-sc", "2"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, creationflags=NOWIN)
        rows = [l for l in out.stdout.splitlines() if l.startswith('"')]
        if len(rows) >= 2:
            vals = []
            for x in rows[-1].split(",")[1:]:
                n = _numf(x.strip().strip('"'))
                if n is not None:
                    vals.append(n)
            if vals:
                return {"name": "iGPU (QSV)", "util": round(min(100.0, sum(vals)), 1)}
    except Exception:  # noqa: BLE001
        pass
    return {}


def gpu_poller():
    tick = 0
    while True:
        with _gpu_lock:
            _gpu["nvidia"] = read_nvidia()
            if tick % 2 == 0:                         # iGPU counter is costly - poll half as often
                _gpu["igpu"] = read_igpu()
        tick += 1
        time.sleep(3)


def gpu_snapshot():
    with _gpu_lock:
        return {"nvidia": dict(_gpu["nvidia"]), "igpu": dict(_gpu["igpu"])}


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
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if not TOKEN:
            return False   # no token configured -> deny (server also binds localhost)
        return hmac.compare_digest(self.headers.get("X-Lada-Token", ""), TOKEN)

    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n <= 0 or n > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TypeError):
            return {}

    def _send_file(self, path, ctype, inject_token=False):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._send(404, {"error": "not found"})
        if inject_token:
            body = body.replace(b"__RUNNER_TOKEN__", TOKEN.encode())
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        raw = self.path.split("?", 1)[0].rstrip("/")
        if raw in ("", "/"):
            return self._send_file(os.path.join(HERE, "webui", "index.html"),
                                   "text/html", inject_token=True)
        if not self._authed():
            return self._send(401, {"error": "bad token"})
        if raw == "/health":
            return self._send(200, {
                "ok": True, "node": CFG["node_name"], "kind": "windows",
                "ops": enabled_ops(),
                "encoders": {"ai": resolve_lane_encoder("ai"),
                             "transcode": resolve_lane_encoder("transcode")},
                "lanes": {n: {"busy": l.busy(), "paused": l.paused, "gpu": l.gpu_label,
                              "job": l.running_id} for n, l in LANES.items()},
                "paused": _node["paused"], "busy": any(l.busy() for l in LANES.values())})
        if raw == "/gpu":
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
            if to_local(b["input"]) is None or to_local(b["output_dir"]) is None:
                return self._send(400, {"error": "input/output_dir must be within a configured path prefix"})
            if _node["paused"]:
                return self._send(409, {"error": "node is paused/draining"})
            jid = uuid.uuid4().hex[:12]
            job = {"id": jid, "state": "queued", "progress": 0.0, "message": "Queued",
                   "op": op, "lane": lane_for(op), "stage": None, "frame": None,
                   "total_frames": None, "fps": None, "speed": None, "eta": None,
                   "output_path": None, "error": None, "started_at": None, "_opts": b}
            with _jobs_lock:
                _jobs[jid] = job
                _evict_jobs()
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
                else:
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


def _shutdown():
    for lane in LANES.values():
        if lane.busy():
            lane.do_cancel()


def main():
    global LANES
    if psutil is None:
        log.error("psutil not installed - cancel can't kill child trees and pause/resume no-op. "
                  "Install it in the venv.")
    LANES = {}
    if CFG["lanes"].get("ai"):
        LANES["ai"] = Lane("ai", "nvidia")
    if CFG["lanes"].get("transcode"):
        LANES["transcode"] = Lane("transcode", "igpu")
    atexit.register(_shutdown)
    # warm encoder probes at startup so /health never blocks on ffmpeg
    for lane in ("ai", "transcode"):
        if CFG["lanes"].get(lane):
            resolve_lane_encoder(lane)
    threading.Thread(target=gpu_poller, daemon=True).start()
    bind = "0.0.0.0" if TOKEN else "127.0.0.1"
    if not TOKEN:
        log.warning("no token set - binding 127.0.0.1 only and DENYING API calls. Set a token to accept remote jobs.")
    httpd = ThreadingHTTPServer((bind, PORT), Handler)
    log.info("stashify-winrunner '%s' on %s:%s | ops=%s | ffmpeg=%s",
             CFG["node_name"], bind, PORT, enabled_ops(), FFMPEG)
    try:
        httpd.serve_forever()
    finally:
        _shutdown()


if __name__ == "__main__":
    main()
