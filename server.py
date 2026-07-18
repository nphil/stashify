"""On-demand decensor HTTP API (default container mode).

Drives the Stash UI button: the browser POSTs a scene id, this server runs the
pipeline on the GPU, imports a reviewable preview scene, and exposes live
progress. The user then replaces the original or discards the preview.

Endpoints (all JSON; send X-Decensor-Token if WORKER_TOKEN is set):
  GET  /api/health                     -> {ok, gpu, backend}
  POST /api/decensor                   -> {job_id}     body: {scene_id, ...overrides}
  GET  /api/jobs                        -> [job, ...]
  GET  /api/jobs/<id>                   -> job
  POST /api/jobs/<id>/replace           -> job         (replace original in place)
  POST /api/jobs/<id>/discard           -> job         (delete the preview)

Jobs run one at a time on a single worker thread so they don't contend for the
GPU. States: queued, running, review_ready, replacing, replaced, discarding,
discarded, error.
"""

import os
import re
import sys
import json
import time
import uuid
import queue
import logging
import subprocess
import mimetypes
import posixpath
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)

TOKEN = os.environ.get("WORKER_TOKEN", "")
PORT = core._int(os.environ.get("PORT", "8710"), 8710)
WEBUI_DIR = os.environ.get(
    "WEBUI_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui")
)
STASH_URL_RAW = os.environ.get("STASH_URL", "")
STASH_API_KEY = os.environ.get("STASH_API_KEY", "")

# Scene fragment the dashboard needs, proxied so the WebUI works from any origin
# (a dedicated subdomain can't use Stash's session cookie cross-origin).
SCENES_GQL = (
    "query($f: FindFilterType, $sf: SceneFilterType){ findScenes(filter:$f, scene_filter:$sf){ count scenes {"
    " id title date files { path width height duration size }"
    " studio { name } tags { name } } } }"
)

TAGS_GQL = (
    "query($f: FindFilterType){ findTags(filter:$f){"
    " tags { id name scene_count } } }"
)


def stash_base():
    u = STASH_URL_RAW
    if u and "://" not in u:
        u = "http://" + u
    return u.rstrip("/")


def stash_gql(query, variables=None):
    """Server-side GraphQL to Stash using the worker's API key."""
    import requests

    headers = {"Content-Type": "application/json"}
    if STASH_API_KEY:
        headers["ApiKey"] = STASH_API_KEY
    r = requests.post(
        stash_base() + "/graphql",
        json={"query": query, "variables": variables or {}},
        headers=headers, timeout=30,
    )
    r.raise_for_status()
    j = r.json()
    if j.get("errors"):
        raise RuntimeError(j["errors"][0].get("message", "GraphQL error"))
    return j["data"]

# Per-request overrides the UI may send -> cfg keys.
OVERRIDES = {
    "backend": "backend",                  # decensor | upscale | transcode | command
    "post_upscale": "postUpscale",         # decensor: chain decensor -> upscale on the runner
    "gpu_id": "gpuId",
    "detection_model": "ladaDetModel",     # v4-fast | v4-accurate | v2
    "restoration_model": "ladaRestModel",
    "upscale_model": "ladaUpscaleModel",
    "transcode_height": "transcodeHeight",
    "transcode_quality": "transcodeQuality",
    "runner": "targetRunner",              # pin this job to a specific runner (name or url)
    "engine": "targetEngine",              # pin this job to a specific engine (lada | jasna | span)
    "denoise": "denoise",                  # jasna primary spatial denoise (low|medium|high; jasna-only)
    "secondary": "secondary",              # decensor secondary restoration (rtx-super-res; jasna-only)
    "rtx_quality": "rtx_quality",          # rtx-super-res: low | medium | high | ultra
    "rtx_scale": "rtx_scale",              # rtx-super-res: 2 | 4
    "rtx_denoise": "rtx_denoise",          # rtx-super-res: none | low | medium | high | ultra
    "rtx_deblur": "rtx_deblur",            # rtx-super-res: none | low | medium | high | ultra
    "preview": "preview",                  # live segment preview (jasna >=0.8.0 smart mode)
}

# --------------------------------------------------------------------------- #
# runner registry: env RUNNERS (static bootstrap) + a persisted store the
# dashboard edits + live LAN discovery. core.resolve_runner() picks a capable
# one per job from the merged set.
# --------------------------------------------------------------------------- #

RUNNERS_STORE = os.environ.get("RUNNERS_STORE", "/config/runners.json")
_runners_lock = threading.Lock()


def _env_runners():
    raw = os.environ.get("RUNNERS")
    if not raw:
        return []
    try:
        rs = json.loads(raw)
        for r in rs:
            r["_source"] = "env"
        return rs
    except (ValueError, TypeError):
        return []


def load_store():
    try:
        with open(RUNNERS_STORE, encoding="utf-8") as fh:
            rs = json.load(fh)
        for r in rs:
            r["_source"] = "manual"
        return rs
    except Exception:  # noqa: BLE001 - no store yet / unreadable
        return []


def save_store(runners):
    try:
        os.makedirs(os.path.dirname(RUNNERS_STORE) or ".", exist_ok=True)
        clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in runners]
        with open(RUNNERS_STORE, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=2)
        return True
    except Exception as exc:  # noqa: BLE001
        logging.warning("could not save runners store: %s", exc)
        return False


def merged_runners():
    """env RUNNERS + persisted store, deduped by url (env wins)."""
    seen = {}
    for r in _env_runners() + load_store():
        url = str(r.get("url", "")).rstrip("/")
        if url and url not in seen:
            seen[url] = r
    return list(seen.values())


def probe_runner(r):
    """Live status for one runner (for the dashboard list)."""
    import requests

    url = str(r.get("url", "")).rstrip("/")
    out = {"name": r.get("name") or url, "url": url, "ops": r.get("ops"),
           "engines": r.get("engines"),
           "prefer": r.get("prefer") or [], "source": r.get("_source", "manual"),
           "online": False, "busy": None, "node": None, "kind": None, "note": None}
    if not url:
        return out
    tok = r.get("token") or os.environ.get("WORKER_TOKEN", "")
    try:
        h = requests.get(url + "/health", headers={"X-Runner-Token": tok, "X-Lada-Token": tok}, timeout=4).json()
        out.update(online=True, busy=h.get("busy"), node=h.get("node"),
                   kind=h.get("kind"), ops=h.get("ops") or r.get("ops"),
                   engines=h.get("engines") or r.get("engines"), paused=h.get("paused"))
    except Exception:  # noqa: BLE001 - maybe reachable but token-gated
        try:
            p = requests.get(url + "/ping", timeout=3).json()
            if p.get("stashify"):
                out.update(online=True, node=p.get("node"), kind=p.get("kind"),
                           ops=p.get("ops") or r.get("ops"),
                           engines=p.get("engines") or r.get("engines"), note="token mismatch")
        except Exception:  # noqa: BLE001
            pass
    return out


def discover_runners(timeout=0.5):
    """Scan the coordinator's LAN /24 on the runner ports for /ping responders."""
    import ipaddress
    import requests
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import urlparse as _up

    cidr = os.environ.get("DISCOVER_CIDR")
    if not cidr:
        host = _up(stash_base()).hostname or "192.168.1.0"
        try:
            cidr = str(ipaddress.ip_network(host + "/24", strict=False))
        except ValueError:
            cidr = "192.168.1.0/24"
    ports = [int(p) for p in os.environ.get("DISCOVER_PORTS", "8711,8712").split(",")]
    targets = [(str(ip), port) for ip in ipaddress.ip_network(cidr, strict=False).hosts() for port in ports]

    def probe(t):
        ip, port = t
        try:
            p = requests.get("http://%s:%d/ping" % (ip, port), timeout=timeout).json()
            if p.get("stashify"):
                return {"name": p.get("node") or ("%s:%d" % (ip, port)),
                        "url": "http://%s:%d" % (ip, port), "ops": p.get("ops"),
                        "engines": p.get("engines"),
                        "kind": p.get("kind"), "_source": "discovered"}
        except Exception:  # noqa: BLE001
            return None
    found = []
    with ThreadPoolExecutor(max_workers=64) as ex:
        for r in ex.map(probe, targets):
            if r:
                found.append(r)
    return found


_jobs = {}
_jobs_lock = threading.Lock()
_work = queue.Queue()
_stash = None
_stash_lock = threading.Lock()
_running_job_id = None          # the job whose subprocess is live (for cancel/pause)
_gpu = {}                       # active runner's GPU (back-compat: /api/gpu + job.gpu_stats)
_gpu_lock = threading.Lock()
_gpus = []                      # per-runner rows: {name,url,online,active,gpu:{...}}
_gpus_lock = threading.Lock()

# --------------------------------------------------------------------------- #
# per-job live log buffer
#
# Each job gets a bounded ring of recent lines the dashboard tails incrementally
# (GET /api/jobs/<id>/log?after=<seq>). Three sources feed it, each tagged with a
# level so the UI can colour them:
#   event  -> stage transitions (the progress messages: "Running deepmosaics"…)
#   info/warn/error -> pipeline logging (core.log, routed via JobLog below)
#   proc   -> raw subprocess output, throttled to a ~1.2s heartbeat (no spam)
# --------------------------------------------------------------------------- #

BASE_LOG = core.log             # the plain stdout logger; JobLog forwards to it
LOG_MAX = 400                   # lines retained per job
_job_logs = {}                  # job_id -> deque[{seq, t, level, text}]
_job_log_seq = {}               # job_id -> last sequence number
_job_logs_lock = threading.Lock()


def push_log(job_id, text, level="proc"):
    if not job_id or text is None:
        return
    text = str(text).strip()
    if not text:
        return
    if len(text) > 400:
        text = text[:400] + "…"
    with _job_logs_lock:
        dq = _job_logs.get(job_id)
        if dq is None:
            dq = _job_logs[job_id] = deque(maxlen=LOG_MAX)
        seq = _job_log_seq.get(job_id, 0) + 1
        _job_log_seq[job_id] = seq
        dq.append({"seq": seq, "t": round(time.time(), 3), "level": level, "text": text})


def job_log_cursor(job_id):
    with _job_logs_lock:
        return _job_log_seq.get(job_id, 0)


def job_log_since(job_id, after):
    with _job_logs_lock:
        dq = _job_logs.get(job_id)
        if not dq:
            return []
        return [dict(x) for x in dq if x["seq"] > after]


def raw_log_sink(job_id):
    """A throttled log_cb for raw subprocess lines: drops consecutive duplicates
    and rate-limits to one line / 1.2s so a chatty tqdm bar becomes a heartbeat
    rather than a flood."""
    st = {"t": 0.0, "last": None}

    def sink(line):
        line = (line or "").strip()
        if not line or line == st["last"]:
            return
        now = time.time()
        if now - st["t"] < 1.2:
            return
        st["t"] = now
        st["last"] = line
        push_log(job_id, line, "proc")

    return sink


class JobLog:
    """Routes core.log for the running job into its live-log buffer, while still
    forwarding everything to stdout. Safe because jobs are serialized on one
    worker thread; worker_loop swaps this in per job and restores BASE_LOG after.
    Debug is stdout-only (too noisy for the buffer)."""

    def __init__(self, job_id):
        self.job_id = job_id

    def debug(self, m):
        BASE_LOG.debug(m)

    def info(self, m):
        BASE_LOG.info(m)
        push_log(self.job_id, m, "info")

    def warning(self, m):
        BASE_LOG.warning(m)
        push_log(self.job_id, m, "warn")

    def error(self, m):
        BASE_LOG.error(m)
        push_log(self.job_id, m, "error")

    def progress(self, frac):
        BASE_LOG.progress(frac)


def _numf(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _gpu_targets():
    """Every runner to poll for GPU telemetry: the merged registry + the RUNNER_URL/
    LADA_URL fallback (deduped by url)."""
    targets, seen = [], set()
    for r in merged_runners():
        url = str(r.get("url", "")).rstrip("/")
        if url and url not in seen:
            seen.add(url)
            targets.append({"name": r.get("name") or url, "url": url,
                            "token": r.get("token") or os.environ.get("WORKER_TOKEN", "")})
    fb = (os.environ.get("RUNNER_URL") or os.environ.get("LADA_URL") or "").rstrip("/")
    if fb and fb not in seen:
        targets.append({"name": "default", "url": fb,
                        "token": (os.environ.get("RUNNER_TOKEN") or os.environ.get("LADA_TOKEN")
                                  or os.environ.get("WORKER_TOKEN", ""))})
    return targets


def _read_runner_gpu(url, token):
    """One runner's /gpu (flat {util,mem_used,mem_total,temp,power}), or None if unreachable."""
    import requests
    try:
        r = requests.get(url + "/gpu", headers={"X-Runner-Token": token, "X-Lada-Token": token}, timeout=3)
        if r.ok:
            d = r.json()
            if isinstance(d, dict) and d.get("util") is not None:
                return {"util": _numf(d.get("util")), "mem_used": _numf(d.get("mem_used")),
                        "mem_total": _numf(d.get("mem_total")), "temp": _numf(d.get("temp")),
                        "power": _numf(d.get("power"))}
    except Exception:  # noqa: BLE001
        pass
    return None


def _local_nvidia():
    """Local nvidia-smi, for a worker that still runs on the GPU host (no separate runner)."""
    gid = os.environ.get("GPU_ID", "0")
    if str(gid).strip() in ("", "-1"):
        return None
    try:
        q = "utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"
        out = subprocess.run(["nvidia-smi", "-i", str(gid), "--query-gpu=" + q,
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            v = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]
            if len(v) >= 5:
                return {"util": _numf(v[0]), "mem_used": _numf(v[1]), "mem_total": _numf(v[2]),
                        "temp": _numf(v[3]), "power": _numf(v[4])}
    except (Exception, FileNotFoundError):  # noqa: BLE001
        pass
    return None


def _active_runner_names():
    """Runners currently executing a Stashify job (so the UI can flag active GPU use)."""
    with _jobs_lock:
        return {j.get("runner") for j in _jobs.values()
                if j.get("state") in ("running", "replacing", "discarding") and j.get("runner")}


def gpu_poller():
    from concurrent.futures import ThreadPoolExecutor
    while True:
        try:
            active = _active_runner_names()
            targets = _gpu_targets()
            # Poll runners CONCURRENTLY so a powered-off/asleep runner's 3s connect
            # timeout doesn't serialize onto the others (cycle time = slowest, not sum).
            gpus = {}
            if targets:
                with ThreadPoolExecutor(max_workers=min(8, len(targets))) as ex:
                    futs = {ex.submit(_read_runner_gpu, t["url"], t["token"]): t for t in targets}
                    for fut, t in futs.items():
                        try:
                            gpus[t["url"]] = fut.result()
                        except Exception:  # noqa: BLE001
                            gpus[t["url"]] = None
            rows = [{"name": t["name"], "url": t["url"], "online": gpus.get(t["url"]) is not None,
                     "active": t["name"] in active, "gpu": gpus.get(t["url"]) or {}} for t in targets]
            if not rows:                                  # worker-on-GPU-host fallback
                lg = _local_nvidia()
                if lg is not None:
                    rows.append({"name": "local", "url": "", "online": True,
                                 "active": bool(active), "gpu": lg})
            # back-compat singular: the active runner's GPU, else the busiest online one
            pick = next((r["gpu"] for r in rows if r["active"] and r["gpu"]), None)
            if pick is None:
                on = [r for r in rows if r["gpu"] and r["gpu"].get("util") is not None]
                pick = max(on, key=lambda r: r["gpu"].get("util") or 0)["gpu"] if on else {}
            with _gpus_lock:
                _gpus[:] = rows
            with _gpu_lock:
                _gpu.clear(); _gpu.update(pick)
        except Exception:  # noqa: BLE001 - telemetry must never crash the poller
            pass
        time.sleep(2)


# --------------------------------------------------------------------------- #
# live before/after frame preview
#
# While a job runs, the backend registers where its work-in-progress lives
# (core.set_live_preview). Every few seconds this poller extracts the newest
# decensored frame plus the matching original frame as small JPEGs, which the
# dashboard shows side by side. Files land under PREVIEW_DIR/<job_id>/.
# --------------------------------------------------------------------------- #

PREVIEW_DIR = os.environ.get("PREVIEW_DIR", "/tmp/decensor_preview")


def _preview_runner(info, dest_dir):
    """The runner extracts the frames (its ffmpeg understands the growing
    fragmented mp4 and it knows the true encode position); just mirror them."""
    import requests

    tok = info.get("token", "")
    headers = {"X-Runner-Token": tok, "X-Lada-Token": tok}
    for which in ("after", "before"):
        try:
            r = requests.get("%s/jobs/%s/preview/%s.jpg" % (info["base"], info["rid"], which),
                             headers=headers, timeout=10)
        except Exception:  # noqa: BLE001
            return
        if r.status_code != 200 or not r.content:
            return
        tmp = os.path.join(dest_dir, which + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(r.content)
        os.replace(tmp, os.path.join(dest_dir, which + ".jpg"))


_seg_lock = threading.Lock()
_segments = {}   # job_id -> [ {n,start,end,dur} ] mirrored from the runner


def _mirror_segments(info, jid, dest):
    """Mirror the runner's live segment preview (jasna 0.8.0 smart mode): pull the
    segment list, then fetch each new seg<N>_before/after.mp4 into PREVIEW_DIR so the
    dashboard (served from here, cross-origin to the runner) can play them."""
    import requests

    tok = info.get("token", "")
    headers = {"X-Runner-Token": tok, "X-Lada-Token": tok}
    try:
        r = requests.get("%s/jobs/%s/segments" % (info["base"], info["rid"]),
                         headers=headers, timeout=10)
        if r.status_code != 200:
            return
        segs = (r.json() or {}).get("segments") or []
    except Exception:  # noqa: BLE001
        return
    with _seg_lock:
        have = {s["n"] for s in _segments.get(jid, [])}
    for s in segs:
        n = s.get("n")
        if n is None or n in have:
            continue
        ok = True
        for which in ("before", "after"):
            try:
                rr = requests.get("%s/jobs/%s/seg/%d/%s.mp4" % (info["base"], info["rid"], n, which),
                                  headers=headers, timeout=30)
            except Exception:  # noqa: BLE001
                ok = False
                break
            if rr.status_code != 200 or not rr.content:
                ok = False
                break
            tmp = os.path.join(dest, "seg%d_%s.tmp" % (n, which))
            with open(tmp, "wb") as fh:
                fh.write(rr.content)
            os.replace(tmp, os.path.join(dest, "seg%d_%s.mp4" % (n, which)))
        if ok:
            with _seg_lock:
                lst = _segments.setdefault(jid, [])
                if not any(x["n"] == n for x in lst):
                    lst.append(s)
    # mirror the concatenated decensored-only sample clip once the runner builds it
    samp = os.path.join(dest, "sample.mp4")
    if not os.path.isfile(samp):
        try:
            rr = requests.get("%s/jobs/%s/sample.mp4" % (info["base"], info["rid"]),
                              headers=headers, timeout=60)
            if rr.status_code == 200 and rr.content:
                tmp = samp + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(rr.content)
                os.replace(tmp, samp)
        except Exception:  # noqa: BLE001
            pass


def preview_poller():
    extractors = {"runner": _preview_runner, "lada": _preview_runner}   # accept legacy tag too
    while True:
        time.sleep(2)
        with _jobs_lock:
            jid = _running_job_id
        if not jid:
            continue
        info = core.get_live_preview()
        fn = extractors.get(info.get("type"))
        if not fn:
            continue
        dest = os.path.join(PREVIEW_DIR, jid)
        try:
            os.makedirs(dest, exist_ok=True)
            fn(info, dest)
            _mirror_segments(info, jid, dest)
        except Exception:  # noqa: BLE001 - preview is best-effort, never fatal
            pass


def preview_file(job_id, which):
    p = os.path.join(PREVIEW_DIR, job_id, which + ".jpg")
    return p if os.path.isfile(p) else None


def seg_file(job_id, n, which):
    p = os.path.join(PREVIEW_DIR, job_id, "seg%d_%s.mp4" % (int(n), which))
    return p if os.path.isfile(p) else None


def job_segments(job_id):
    with _seg_lock:
        return list(_segments.get(job_id, []))


def sample_file(job_id):
    p = os.path.join(PREVIEW_DIR, job_id, "sample.mp4")
    return p if os.path.isfile(p) else None


def _cleanup_preview(job_id):
    """After keep/discard, drop the transient preview artifacts (segment clips +
    the decensored sample reel). The full decensored video stays in Stash."""
    import shutil
    with _seg_lock:
        _segments.pop(job_id, None)
    shutil.rmtree(os.path.join(PREVIEW_DIR, job_id), ignore_errors=True)


def live_source(job_id):
    """The growing output file for the running runner job, or None."""
    with _jobs_lock:
        if _running_job_id != job_id:
            return None
    info = core.get_live_preview()
    if info.get("type") not in ("runner", "lada"):
        return None
    try:
        vids = [os.path.join(info["out_dir"], f) for f in os.listdir(info["out_dir"])
                if os.path.splitext(f)[1].lower() in core.VIDEO_EXTS]
    except OSError:
        return None
    return max(vids, key=os.path.getmtime) if vids else None


def get_stash():
    """Lazily build (and cache) the StashInterface on the worker thread."""
    global _stash
    if _stash is None:
        _stash = core.stash_from_env()
    return _stash


def new_job(scene_id, overrides):
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "scene_id": scene_id,
        "state": "queued",
        "progress": 0.0,
        "message": "Queued",
        "review_scene_id": None,
        "output_path": None,
        "error": None,
        "stage": None,
        "runner": None,
        "engine": None,
        "frame": None,
        "total_frames": None,
        "fps": None,
        "eta": None,
        "paused": False,
        "started_at": None,
        "_overrides": overrides,
        "_info": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job
    return job


def public(job):
    out = {k: v for k, v in job.items() if not k.startswith("_")}
    sa = job.get("started_at")
    if sa:
        out["elapsed"] = int((job.get("_ended_at") or time.time()) - sa)
    rn = job.get("runner")                 # this job's own runner GPU, not a global one
    g = None
    if rn:
        with _gpus_lock:
            row = next((r for r in _gpus if r.get("name") == rn), None)
        g = dict(row["gpu"]) if row and row.get("gpu") else None
    if not g:
        with _gpu_lock:
            g = dict(_gpu)
    out["gpu_stats"] = g or {}
    out["log_cursor"] = job_log_cursor(job.get("id"))
    out["preview"] = preview_file(job.get("id"), "after") is not None
    out["segments"] = job_segments(job.get("id"))   # live mosaic-segment before/after clips
    out["sample"] = sample_file(job.get("id")) is not None   # concatenated decensored-only reel
    return out


def set_job(job_id, **fields):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            job.update(fields)


def progress_cb(job_id):
    last = {"msg": None}

    def cb(frac, msg=None, stats=None):
        fields = {"progress": round(float(frac), 3)}
        if msg:
            fields["message"] = msg
            if msg != last["msg"]:            # a new stage/step -> log it once
                last["msg"] = msg
                push_log(job_id, msg, "event")
        if stats:
            for k in ("stage", "frame", "total_frames", "fps", "eta", "indeterminate"):
                if stats.get(k) is not None:
                    fields[k] = stats[k]
        set_job(job_id, **fields)
    return cb


# --------------------------------------------------------------------------- #
# worker thread
# --------------------------------------------------------------------------- #

def job_config(job):
    cfg = core.config_from_env()
    cfg["runners"] = merged_runners()      # env RUNNERS + dashboard-added/discovered
    for req_key, cfg_key in OVERRIDES.items():
        val = job["_overrides"].get(req_key)
        if val is not None and val != "":
            cfg[cfg_key] = val
    return cfg


def do_process(job):
    job_id = job["id"]
    cfg = job_config(job)
    set_job(job_id, state="running", message="Starting", started_at=time.time(),
            paused=False, stage=None, frame=None, total_frames=None, fps=None, eta=None,
            indeterminate=True, backend=cfg.get("backend"))
    stash = get_stash()
    info = core.process_to_review(stash, cfg, job["scene_id"],
                                  progress=progress_cb(job_id), log_cb=raw_log_sink(job_id))
    set_job(
        job_id, state="review_ready", progress=1.0, message="Preview ready to review",
        indeterminate=False,
        review_scene_id=info.get("review_scene_id"), output_path=info.get("output_path"),
        _info=info, _ended_at=time.time(),
    )


def do_replace(job):
    job_id = job["id"]
    info = job.get("_info")
    if not info:
        raise RuntimeError("Nothing to replace (no preview).")
    set_job(job_id, state="replacing", progress=0.0, message="Replacing original", indeterminate=False)
    cfg = job_config(job)
    core.replace_original(get_stash(), cfg, info, progress=progress_cb(job_id))
    set_job(job_id, state="replaced", progress=1.0, message="Original replaced")
    _cleanup_preview(job_id)          # keep the full video; drop the sample + segment clips


def do_discard(job):
    job_id = job["id"]
    info = job.get("_info")
    set_job(job_id, state="discarding", message="Discarding preview", indeterminate=False)
    if info:
        core.discard_review(get_stash(), job_config(job), info)
    set_job(job_id, state="discarded", message="Preview discarded")
    _cleanup_preview(job_id)


ACTIONS = {"process": do_process, "replace": do_replace, "discard": do_discard}


def worker_loop():
    global _running_job_id
    while True:
        action, job_id = _work.get()
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job and job.get("state") == "cancelled":
                job = None  # cancelled while still queued -> skip
        if not job:
            continue
        with _jobs_lock:
            _running_job_id = job_id
        core.set_log(JobLog(job_id))       # route this job's pipeline logs into its buffer
        core.set_job_stamp(lambda **f: set_job(job_id, **f))   # let the backend stamp runner/engine
        try:
            ACTIONS[action](job)
        except core.Cancelled:
            push_log(job_id, "Cancelled by user", "warn")
            set_job(job_id, state="cancelled", message="Cancelled", paused=False, _ended_at=time.time())
        except Exception as exc:  # noqa: BLE001
            logging.exception(f"Job {job_id} action {action} failed")
            push_log(job_id, "ERROR: " + str(exc), "error")
            set_job(job_id, state="error", message=str(exc), error=str(exc), _ended_at=time.time())
        finally:
            core.set_log(BASE_LOG)
            core.clear_job_stamp()
            with _jobs_lock:
                _running_job_id = None


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "Stashify/1.0"

    def log_message(self, fmt, *args):  # quieter access log
        logging.debug("%s - %s", self.address_string(), fmt % args)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Decensor-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if not TOKEN:
            return True
        return self.headers.get("X-Decensor-Token", "") == TOKEN

    def _body(self):
        length = core._int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def serve_static(self, raw):
        """Serve the bundled WebUI SPA for any GET not under /api."""
        rel = posixpath.normpath("/" + raw).lstrip("/") or "index.html"
        full = os.path.join(WEBUI_DIR, rel.replace("/", os.sep))
        root = os.path.abspath(WEBUI_DIR)
        if not os.path.abspath(full).startswith(root) or not os.path.isfile(full):
            full = os.path.join(WEBUI_DIR, "index.html")  # SPA fallback for unknown routes
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._send(404, {"error": "WebUI not installed on this worker"})
        if full.endswith("index.html"):
            body = body.replace(b"__WORKER_TOKEN__", TOKEN.encode())  # self-auth the dashboard
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def api_scenes(self):
        from urllib.parse import urlparse, parse_qs

        q = parse_qs(urlparse(self.path).query)

        def g(k, d):
            v = q.get(k)
            return v[0] if v else d

        sort = g("sort", "date")
        f = {
            "q": g("q", ""),
            "page": core._int(g("page", "1"), 1),
            "per_page": min(60, max(1, core._int(g("per_page", "36"), 36))),
            "sort": sort,
            "direction": (g("dir", "") or ("ASC" if sort == "title" else "DESC")).upper(),
        }
        sf = {}
        want_tag = g("tag", "").strip()          # live tag filter from the dashboard
        if want_tag:
            sf["tags"] = {"value": [want_tag], "modifier": "INCLUDES_ALL", "depth": 0}
        try:
            data = stash_gql(SCENES_GQL, {"f": f, "sf": sf or None})
            return self._send(200, data["findScenes"])
        except Exception as exc:  # noqa: BLE001
            return self._send(502, {"error": "stash: " + str(exc)})

    def proxy_media(self, kind, scene_id):
        """Relay a Stash screenshot/stream through the worker (with the API key)
        so the dashboard's <img>/<video> work without Stash's cookie."""
        import requests

        url = stash_base() + "/scene/" + scene_id + "/" + ("screenshot" if kind == "img" else "stream")
        headers = {}
        if STASH_API_KEY:
            headers["ApiKey"] = STASH_API_KEY
        rng = self.headers.get("Range")
        if rng:
            headers["Range"] = rng
        try:
            up = requests.get(url, headers=headers, stream=True, timeout=60)
        except Exception as exc:  # noqa: BLE001
            return self._send(502, {"error": "stash media: " + str(exc)})
        self.send_response(up.status_code)
        for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
            if h in up.headers:
                self.send_header(h, up.headers[h])
        self._cors()
        self.end_headers()
        try:
            for chunk in up.iter_content(65536):
                if chunk:
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self):
        raw = self.path.split("?", 1)[0]
        if not raw.startswith("/api"):
            return self.serve_static(raw)
        path = raw.rstrip("/")
        if path == "/api/health":
            with _gpu_lock:
                gpu_stats = dict(_gpu)
            return self._send(200, {
                "ok": True,
                "gpu": os.environ.get("GPU_ID", "0"),
                "backend": os.environ.get("BACKEND", "decensor"),
                "postUpscale": core.env_bool("POST_UPSCALE", False),
                "runner": bool((os.environ.get("RUNNER_URL") or os.environ.get("LADA_URL") or "").strip()),
                "lada": bool((os.environ.get("RUNNER_URL") or os.environ.get("LADA_URL") or "").strip()),  # legacy alias
                "gpu_stats": gpu_stats,
            })
        # Live GPU readout for the always-on topbar meter — cheap and unauth'd
        # (harmless telemetry; the worker sits behind your proxy's auth anyway).
        if path == "/api/gpu":
            with _gpu_lock:
                return self._send(200, dict(_gpu))
        if path == "/api/gpus":                 # per-runner GPU rows for the dashboard meters
            with _gpus_lock:
                return self._send(200, [dict(r) for r in _gpus])
        # Media proxies for the dashboard's <img>/<video> (which can't send the
        # token header) — left open; the worker sits behind your proxy's auth.
        m = re.match(r"^/api/(img|vid)/(\d+)$", path)
        if m:
            return self.proxy_media(m.group(1), m.group(2))
        # Live video feed: tail-follow the growing fragmented mp4 the lada
        # runner is writing. The browser plays it like a live stream — no
        # transcoding, a few seconds behind the encoder. Ends when the job does.
        m = re.match(r"^/api/jobs/([0-9a-f]+)/live\.mp4$", path)
        if m:
            job_id = m.group(1)
            part = live_source(job_id)
            if not part:
                return self._send(404, {"error": "no live stream for this job"})
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()   # no Content-Length: stream until the job ends
            try:
                with open(part, "rb") as fh:
                    while True:
                        chunk = fh.read(65536)
                        if chunk:
                            self.wfile.write(chunk)
                            continue
                        with _jobs_lock:
                            still = _running_job_id == job_id
                        if not still:
                            break            # job finished/cancelled -> end of stream
                        time.sleep(0.5)      # at EOF but encoder still writing: tail
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        # Live before/after preview frames (also <img>-loaded, so unauth'd).
        m = re.match(r"^/api/jobs/([0-9a-f]+)/preview/(before|after)\.jpg$", path)
        if m:
            p = preview_file(m.group(1), m.group(2))
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
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        # Decensored-only sample clip (loaded by <video>, unauth'd like the jpgs).
        m = re.match(r"^/api/jobs/([0-9a-f]+)/sample\.mp4$", path)
        if m:
            p = sample_file(m.group(1))
            if not p:
                return self._send(404, {"error": "no sample"})
            try:
                with open(p, "rb") as fh:
                    body = fh.read()
            except OSError:
                return self._send(404, {"error": "no sample"})
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        # Live segment preview clips (loaded by <video>, so unauth'd like the jpgs).
        m = re.match(r"^/api/jobs/([0-9a-f]+)/seg/(\d+)/(before|after)\.mp4$", path)
        if m:
            p = seg_file(m.group(1), m.group(2), m.group(3))
            if not p:
                return self._send(404, {"error": "no segment"})
            try:
                with open(p, "rb") as fh:
                    body = fh.read()
            except OSError:
                return self._send(404, {"error": "no segment"})
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if not self._authed():
            return self._send(401, {"error": "bad token"})
        if path == "/api/scenes":
            return self.api_scenes()
        if path == "/api/tags":
            # live tag list from Stash for the dashboard filter (busiest first)
            try:
                data = stash_gql(TAGS_GQL, {"f": {"per_page": 500, "sort": "scenes_count",
                                                  "direction": "DESC"}})
                tags = [t for t in data["findTags"]["tags"] if (t.get("scene_count") or 0) > 0]
                return self._send(200, tags)
            except Exception as exc:  # noqa: BLE001
                return self._send(502, {"error": "stash: " + str(exc)})
        if path == "/api/jobs":
            with _jobs_lock:
                data = [public(j) for j in _jobs.values()]
            return self._send(200, data)
        if path == "/api/runners":
            from concurrent.futures import ThreadPoolExecutor
            rs = merged_runners()
            # resolve_runner also treats RUNNER_URL as a candidate ("default"); surface
            # it here too (deduped by url) so the dashboard's pickers + enable gate see
            # every routable node, not just registered ones.
            runner_url = (os.environ.get("RUNNER_URL") or os.environ.get("LADA_URL") or "").strip().rstrip("/")
            if runner_url and not any(str(r.get("url", "")).rstrip("/") == runner_url for r in rs):
                rs = rs + [{"name": "default", "url": runner_url,
                            "token": (os.environ.get("RUNNER_TOKEN") or os.environ.get("LADA_TOKEN")
                                      or os.environ.get("WORKER_TOKEN", "")),
                            "_source": "env"}]
            with ThreadPoolExecutor(max_workers=8) as ex:
                return self._send(200, list(ex.map(probe_runner, rs)))
        if path == "/api/route-preview":
            # "which runner + engine would handle this right now" for the dashboard,
            # honoring the same targetEngine/targetRunner pins a real job would use.
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            # parse_qs decodes '+' to space; ops never contain spaces, so restore it
            # ('decensor upscale' -> 'decensor+upscale') to accept an unencoded plus.
            op = (q.get("op") or [""])[0].replace(" ", "+")
            if op not in ("decensor", "decensor+upscale", "upscale", "transcode"):
                return self._send(400, {"error": "valid op required"})
            cfg = core.config_from_env()
            cfg["runners"] = merged_runners()
            eng = (q.get("engine") or [""])[0]
            run = (q.get("runner") or [""])[0]
            if eng and eng.lower() != "auto":
                cfg["targetEngine"] = eng
            if run and run.lower() != "auto":
                cfg["targetRunner"] = run
            return self._send(200, core.preview_route(cfg, op))
        m = re.match(r"^/api/jobs/([0-9a-f]+)/log$", path)
        if m:
            from urllib.parse import urlparse, parse_qs
            after = core._int(parse_qs(urlparse(self.path).query).get("after", ["0"])[0], 0)
            job_id = m.group(1)
            with _jobs_lock:
                exists = job_id in _jobs
            if not exists:
                return self._send(404, {"error": "no such job"})
            return self._send(200, {"cursor": job_log_cursor(job_id),
                                    "lines": job_log_since(job_id, after)})
        m = re.match(r"^/api/jobs/([0-9a-f]+)$", path)
        if m:
            with _jobs_lock:
                job = _jobs.get(m.group(1))
            return self._send(200, public(job)) if job else self._send(404, {"error": "no such job"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if not self._authed():
            return self._send(401, {"error": "bad token"})

        if path == "/api/decensor":
            body = self._body()
            scene_id = body.get("scene_id") or body.get("sceneId")
            if not scene_id:
                return self._send(400, {"error": "scene_id required"})
            overrides = {k: body[k] for k in OVERRIDES if k in body}
            job = new_job(str(scene_id), overrides)
            _work.put(("process", job["id"]))
            return self._send(202, public(job))

        # --- runner registry management ---
        if path == "/api/runners":                     # add / update a runner
            b = self._body()
            url = str(b.get("url", "")).rstrip("/")
            if not url.startswith("http"):
                return self._send(400, {"error": "a valid http(s) url is required"})
            # discovered/added runners usually share the fleet token; without a
            # default they'd probe online (/ping is unauth) but 401 on dispatch
            entry = {"name": b.get("name") or url, "url": url,
                     "token": b.get("token") or os.environ.get("WORKER_TOKEN", ""),
                     "ops": b.get("ops") or None, "prefer": b.get("prefer") or []}
            with _runners_lock:
                store = [r for r in load_store() if str(r.get("url", "")).rstrip("/") != url]
                store.append(entry)
                save_store(store)
            return self._send(201, probe_runner(dict(entry, _source="manual")))
        if path == "/api/runners/remove":
            url = str(self._body().get("url", "")).rstrip("/")
            with _runners_lock:
                save_store([r for r in load_store() if str(r.get("url", "")).rstrip("/") != url])
            return self._send(200, {"ok": True})
        if path == "/api/runners/test":
            b = self._body()
            return self._send(200, probe_runner({"url": b.get("url"), "token": b.get("token"),
                                                 "name": b.get("name"), "_source": "test"}))
        if path == "/api/runners/discover":
            found = discover_runners()
            known = {str(r.get("url", "")).rstrip("/") for r in merged_runners()}
            for f in found:
                f["registered"] = f["url"] in known
            return self._send(200, found)

        m = re.match(r"^/api/jobs/([0-9a-f]+)/(replace|discard)$", path)
        if m:
            job_id, action = m.group(1), m.group(2)
            new_state = "replacing" if action == "replace" else "discarding"
            # Check state and claim the job atomically under the lock so a
            # concurrent/duplicate request (e.g. a double-clicked "Replace" or a
            # second tab) gets a 409 instead of both enqueuing — which would drive
            # an already-replaced job into a bogus error/discarded terminal state.
            with _jobs_lock:
                job = _jobs.get(job_id)
                if not job:
                    return self._send(404, {"error": "no such job"})
                if job["state"] != "review_ready":
                    return self._send(409, {"error": f"job is {job['state']}, not review_ready"})
                job["state"] = new_state
                job["message"] = f"Queued {action}"
                snapshot = public(job)
            _work.put((action, job_id))
            return self._send(202, snapshot)

        m = re.match(r"^/api/jobs/([0-9a-f]+)/(cancel|pause|resume)$", path)
        if m:
            job_id, action = m.group(1), m.group(2)
            with _jobs_lock:
                job = _jobs.get(job_id)
                running = (_running_job_id == job_id)
            if not job:
                return self._send(404, {"error": "no such job"})
            st = job["state"]
            if action == "cancel":
                if st not in ("queued", "running", "paused"):
                    return self._send(409, {"error": f"job is {st}, cannot cancel"})
                if running:
                    core.cancel_active()  # SIGKILL the live subprocess group
                set_job(job_id, state="cancelled", message="Cancelled", paused=False,
                        _ended_at=time.time())
            elif action == "pause":
                if not running or st != "running":
                    return self._send(409, {"error": "job is not running"})
                if not core.pause_active():
                    return self._send(409, {"error": "pause not supported here"})
                set_job(job_id, paused=True, message="Paused")
            else:  # resume
                if not running or not job.get("paused"):
                    return self._send(409, {"error": "job is not paused"})
                core.resume_active()
                set_job(job_id, paused=False, message="Resumed")
            with _jobs_lock:
                snapshot = public(_jobs[job_id])
            return self._send(202, snapshot)

        return self._send(404, {"error": "not found"})


def main():
    # Fail fast on obvious misconfig, but stay up so the UI can show errors.
    try:
        core.validate(core.config_from_env())
    except ValueError as exc:
        logging.warning(f"Config not fully valid yet: {exc}")
    if not os.environ.get("STASH_URL"):
        logging.warning("STASH_URL not set — jobs will fail until it is configured.")
    else:
        # Build the StashInterface now, while core.log is still BASE_LOG, so its
        # captured logger doesn't get pinned to a per-job JobLog later.
        try:
            get_stash()
        except Exception as exc:  # noqa: BLE001
            logging.warning(f"Could not pre-connect to Stash: {exc}")

    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=gpu_poller, daemon=True).start()
    threading.Thread(target=preview_poller, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    logging.info(f"Stashify server listening on :{PORT} (token {'on' if TOKEN else 'off'})")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
