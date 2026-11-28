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
  POST /api/decensor with cloud_preset (or cloud:true) + confirm_usd -> a PAID cloud run on a rented
       GPU: Decensor, Decensor + upscale, Upscale or Transcode (`op`, `upscale`, `upscale_height`,
       `transcode_height`, `transcode_quality`), optionally steered to
       a machine (`offer_id` + `provider`, `hardware`, `gpu`, `cloud_filters`)            (cloud_api.py)
  GET  /api/cloud/{settings,status,offers,quote} and POST /api/cloud/{settings,test}   (cloud_api.py)

Jobs run one at a time on a single worker thread so they don't contend for the
GPU. States: queued, running, review_ready, replacing, replaced, discarding,
discarded, error.
"""

import os
import re
import sys
import gzip
import json
import time
import urllib.parse
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
import events
import cloud_api
import cloud_jasna
import runner_stream
from winrunner.frame_preview import latest_pair, pair_path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)

TOKEN = os.environ.get("WORKER_TOKEN", "")
PORT = core._int(os.environ.get("PORT", "8710"), 8710)
WEBUI_DIR = os.environ.get(      # the built dashboard (web/ -> `npm run build`)
    "WEBUI_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "dist")
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
SCENE_SORTS = ("date", "created_at", "file_mod_time", "title", "random")

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
    "restoration": "restoration",          # jasna restoration model: ltx-basic | ltx | ltx-4step | ltx-8step
    "ltx_auto_split": "ltx_auto_split",    # LTX only for mosaics >= this percent of the frame height
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
# Finished jobs waiting for Replace/Discard, kept on disk: an update or restart of this
# container used to forget them (the preview stayed in Stash with no way to act on it).
REVIEW_STORE = os.environ.get("REVIEW_STORE") or os.path.join(os.path.dirname(RUNNERS_STORE), "reviews.json")
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


def wake_eta():
    """Seconds a woken runner is given to answer (the same budget job routing waits)."""
    return core._int(core.config_from_env().get("runnerWakeSecs"), 120)


def _runner_gpu_facts(name, label):
    """(GPU name, VRAM in GB) for the runner card: the name a runner reports, the size from the
    meter poll. Either may be unknown."""
    with _gpus_lock:
        row = next((r for r in _gpus if r.get("name") == name), None)
    mem = ((row or {}).get("gpu") or {}).get("mem_total")
    return label or None, (round(mem / 1024.0) if mem else None)


def _runner_qualities(ops, engines):
    """Quality levels a runner offers for decensoring: all four with Jasna, one with Lada."""
    if ops is not None and "decensor" not in ops:
        return None
    eng = (engines or {}).get("decensor")
    if eng == "jasna":
        return ["fast", "balanced", "high", "max"]
    return ["fast"] if eng else None


def _runner_queue_eta(name):
    """Seconds until a runner is free of the job it is running (0 when idle), None if unknown."""
    with _jobs_lock:
        etas = [j.get("eta") for j in _jobs.values() if j.get("state") == "running" and j.get("runner") == name]
    if not etas:
        return 0
    return int(max(etas)) if all(e is not None for e in etas) else None


# A runner's last good answer, held briefly. The NAS drops a share of connections to
# a healthy PC; without this every dropped probe blanked the runner card (it flapped
# between full details, "offline" and a false "token mismatch") and its GPU meters.
STICKY_SECS = 20
_last_probe = {}                # url -> (time, row)
_last_gpu = {}                  # url -> (time, gpu dict)


def _stream_state(url):
    """"live" (updates are pushed), "reconnecting", "unsupported" (an older runner: polled), ..."""
    lk = runner_stream.peek(url)
    return lk.state() if lk is not None else None


def probe_runner(r):
    """Live status for one runner (for the dashboard list)."""
    import requests

    url = str(r.get("url", "")).rstrip("/")
    can_wake = bool(r.get("mac"))
    out = {"name": r.get("name") or url, "url": url, "ops": r.get("ops"),
           "engines": r.get("engines"),
           "prefer": r.get("prefer") or [], "source": r.get("_source", "manual"),
           "online": False, "busy": None, "node": None, "kind": None, "note": None,
           "asleep": False, "can_wake": can_wake, "wake_eta_s": None, "queue_eta_s": None,
           "gpu": None, "vram_gb": None, "qualities": None, "stream": None}
    if not url:
        return out
    tok = r.get("token") or os.environ.get("WORKER_TOKEN", "")
    try:
        h = core.runner_health(url, tok)
        label = next((ln.get("gpu") for ln in (h.get("lanes") or {}).values() if ln.get("gpu")), None)
        out.update(online=True, busy=h.get("busy"), node=h.get("node"),
                   kind=h.get("kind"), ops=h.get("ops") or r.get("ops"),
                   engines=h.get("engines") or r.get("engines"), paused=h.get("paused"))
        out["gpu"], out["vram_gb"] = _runner_gpu_facts(out["name"], label)
        out["qualities"] = _runner_qualities(out["ops"], out["engines"])
        out["queue_eta_s"] = _runner_queue_eta(out["name"])
        out["stream"] = _stream_state(url)
        _last_probe[url] = (time.time(), dict(out))
    except PermissionError:      # reachable, and it really did refuse our token
        out.update(online=True, note="token mismatch")
    except Exception:  # noqa: BLE001 - no answer this time
        held = _last_probe.get(url)
        if held and time.time() - held[0] < STICKY_SECS:
            row = dict(held[1])
            row["stream"] = _stream_state(url)
            return row
        if can_wake:
            out.update(asleep=True, note="asleep - wakes when a job needs it",
                       wake_eta_s=wake_eta())
    return out


def runners_snapshot():
    """Every routable runner with its live status: the list GET /api/runners and the event
    stream both send."""
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
        return list(ex.map(probe_runner, rs))


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


_RE_BAR_LINE = re.compile(r"\d{1,3}\s*%\s*\|")     # a tqdm / lada progress bar line


def is_progress_line(text):
    return bool(_RE_BAR_LINE.search(text))


def push_log(job_id, text, level="proc"):
    """Append a line to a job's live log. A progress-bar line REPLACES the previous one when that
    was also a bar (a two-hour job is one live line, not thousands, and the events before it are
    not pushed out of the buffer); the entry is flagged `live` so the dashboard pins it."""
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
        row = {"seq": seq, "t": round(time.time(), 3), "level": level, "text": text}
        if level == "proc" and is_progress_line(text):
            row["live"] = True
            if dq and dq[-1].get("live"):
                dq.pop()
        dq.append(row)


def job_log_cursor(job_id):
    with _job_logs_lock:
        return _job_log_seq.get(job_id, 0)


def job_log_since(job_id, after):
    with _job_logs_lock:
        dq = _job_logs.get(job_id)
        if not dq:
            return []
        return [dict(x) for x in dq if x["seq"] > after]


def _running_ids():
    with _jobs_lock:
        return [j["id"] for j in _jobs.values() if j.get("state") in ("running", "replacing")]


def log_batches(cursors):
    """New log lines of each running job since `cursors` (job id -> last seq), as event
    payloads; advances the cursors and forgets jobs that are no longer running."""
    ids = _running_ids()
    for gone in [j for j in cursors if j not in ids]:
        del cursors[gone]
    out = []
    for jid in ids:
        lines = job_log_since(jid, cursors.get(jid, 0))
        if lines:
            cursors[jid] = lines[-1]["seq"]
            out.append({"job_id": jid, "cursor": lines[-1]["seq"], "lines": lines})
    return out


def log_snapshot():
    """Every buffered line of every running job: what a client that just connected is sent."""
    return [{"job_id": jid, "cursor": job_log_cursor(jid), "lines": lines}
            for jid in _running_ids() for lines in [job_log_since(jid, 0)] if lines]


def gpus_snapshot():
    with _gpus_lock:
        return [dict(r) for r in _gpus]


def cloud_snapshot():
    return cloud_api.status(cloud_jasna.cloud())[1]


_hub = None
_hub_lock = threading.Lock()


def event_hub():
    """The process's one event hub (GET /api/events)."""
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = events.EventHub([("jobs", jobs_snapshot, 0.5), ("gpus", gpus_snapshot, 1.0),
                                    ("runners", runners_snapshot, 5.0, True), ("cloud", cloud_snapshot, 10.0)],
                                   logs=(log_batches, log_snapshot))
        return _hub


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


def _gpu_row(d):
    return {"util": _numf(d.get("util")), "mem_used": _numf(d.get("mem_used")),
            "mem_total": _numf(d.get("mem_total")), "temp": _numf(d.get("temp")),
            "power": _numf(d.get("power"))}


def _read_runner_gpu(url, token):
    """One runner's GPU (flat {util,mem_used,mem_total,temp,power}), or None if unreachable. From
    the runner's pushed /events stream while that is live (no request); else its /gpu."""
    import requests
    lk = runner_stream.ensure(url, token)
    d = lk.live_gpu() if lk is not None else None
    if isinstance(d, dict) and d.get("util") is not None:
        return _gpu_row(d)
    try:
        r = core.http().get(url + "/gpu", headers={"X-Runner-Token": token, "X-Lada-Token": token}, timeout=3)
        if r.ok:
            d = r.json()
            if isinstance(d, dict) and d.get("util") is not None:
                return _gpu_row(d)
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
            now = time.time()
            for u_, g_ in list(gpus.items()):
                if g_ is not None:
                    _last_gpu[u_] = (now, g_)
                elif u_ in _last_gpu and now - _last_gpu[u_][0] < STICKY_SECS:
                    gpus[u_] = _last_gpu[u_][1]
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
# The runners publish matched JPEGs together in shared scratch. The poller
# keeps a copy under PREVIEW_DIR/<job_id>/ so the pair survives job cleanup.
# --------------------------------------------------------------------------- #

PREVIEW_DIR = os.environ.get("PREVIEW_DIR", "/tmp/decensor_preview")


_seg_lock = threading.Lock()
_segments = {}   # job_id -> [ {n,start,end,dur} ] mirrored from the runner


def _mirror_segments(info):
    """Copy announced preview clips; only advertise a comparison once both files exist."""
    import tempfile
    import requests

    dest = info["preview_dir"]
    jid = os.path.basename(dest)
    base = "%s/jobs/%s" % (info["base"], info["rid"])
    tok = info["token"]
    headers = {"X-Runner-Token": tok, "X-Lada-Token": tok}

    def fetch(url_path, name):
        path = os.path.join(dest, name)
        if os.path.isfile(path):
            return True
        temp = None
        try:
            with core.http().get(base + url_path, headers=headers, stream=True, timeout=(4, 20)) as response:
                if response.status_code != 200:
                    return False
                os.makedirs(dest, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=dest, delete=False) as out:
                    temp = out.name
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        out.write(chunk)
                    if not out.tell():
                        return False
            os.replace(temp, path)
            return True
        except (OSError, requests.RequestException):
            return False
        finally:
            if temp and os.path.exists(temp):
                os.unlink(temp)

    with _seg_lock:
        have = {s["n"] for s in _segments.get(jid, [])}
    for segment in info.get("segments", ()):
        n = segment.get("n")
        if not isinstance(n, int) or n < 0 or n in have:
            continue
        if all(fetch(f"/seg/{n}/{side}.mp4", f"seg{n}_{side}.mp4") for side in ("before", "after")):
            with _seg_lock:
                items = _segments.setdefault(jid, [])
                if not any(s["n"] == n for s in items):
                    items.append(segment)
            have.add(n)
    if info.get("sample"):
        fetch("/sample.mp4", "sample.mp4")


def preview_poller():
    while True:
        time.sleep(2)
        info = core.get_live_preview()
        if not info.get("preview_dir") or not (info.get("segments") or info.get("sample")):
            continue
        try:
            _mirror_segments(info)
        except Exception:  # noqa: BLE001 - preview is best-effort, never fatal
            pass


def preview_file(job_id, which, pair=None):
    directory = os.path.join(PREVIEW_DIR, job_id)
    return pair_path(directory, which, latest_pair(directory) if pair is None else pair)


def seg_file(job_id, n, which):
    base = os.path.join(PREVIEW_DIR, job_id, "seg%d_" % int(n))
    if all(os.path.isfile(base + side + ".mp4") for side in ("before", "after")):
        return base + which + ".mp4"
    return None


def job_segments(job_id):
    with _seg_lock:
        return list(_segments.get(job_id, []))


def sample_file(job_id):
    p = os.path.join(PREVIEW_DIR, job_id, "sample.mp4")
    return p if os.path.isfile(p) else None


def _cleanup_preview(job_id):
    """Drop the comparison frames, segment clips and sample; keep the full video."""
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


_CLOUD_QUALITY = {"close-ups": "fast", "detail": "balanced", "detail-close-ups": "high"}


def job_op(overrides, cloud=None):
    """The dashboard's name for what a job does: decensor | decensor_up | upscale | transcode."""
    if cloud:
        return {"upscale": "upscale", "decensor+upscale": "decensor_up",
                "transcode": "transcode"}.get(cloud.get("op"), "decensor")
    backend = overrides.get("backend") or core.config_from_env().get("backend")
    if backend in ("upscale", "transcode"):
        return backend
    post = overrides.get("post_upscale")
    return "decensor_up" if (cloud_jasna.truthy(post) if post is not None else core.env_bool("POST_UPSCALE", False)) \
        else "decensor"


def job_quality(overrides, cloud=None):
    """fast | balanced | high for a decensor job (None where the idea does not apply)."""
    if cloud:
        return _CLOUD_QUALITY.get(cloud.get("preset"))
    if job_op(overrides) not in ("decensor", "decensor_up"):
        return None
    rest = str(overrides.get("restoration") or "")
    if rest in ("basicvsrpp", "ltx-basic"):
        return "fast"
    if rest.startswith("ltx"):
        return "balanced" if overrides.get("ltx_auto_split") else "high"
    return None


def scene_title(scene_id):
    """A scene's display name (title, else file name) or None; never raises."""
    try:
        (meta,) = cloud_api.scene_metas(stash_gql, [scene_id])
        return (meta or {}).get("title") or None
    except Exception:  # noqa: BLE001 - a name is nice to have, never a reason to refuse a job
        return None


def new_job(scene_id, overrides, cloud=None, title=None):
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
        "phase": None,
        "step": None,
        "steps": None,
        "detail": None,
        "pass_no": None,
        "passes": None,
        "estimate": False,
        "updated_at": None,
        "paused": False,
        "started_at": None,
        "created": time.time(),
        "scene_title": title,
        "op": job_op(overrides, cloud),
        "quality": job_quality(overrides, cloud),
        "result": None,
        "_overrides": overrides,
        "_info": None,
    }
    if cloud:      # a paid cloud Jasna job: the private record, plus what the dashboard may show
        job.update({"_cloud": cloud, "cloud": True, "cloud_est": cloud["est_usd"], "cloud_usd": 0.0,
                    "cloud_cap": cloud["cap"], "cloud_label": cloud["label"], "cloud_op": cloud.get("op"),
                    "cloud_gpu": cloud.get("gpu"), "cloud_offer_id": cloud.get("offer_id"),
                    "cloud_provider": cloud.get("provider"), "cloud_hardware": cloud.get("hardware"),
                    "cloud_est_usd": cloud["est_usd"], "_est_min": cloud.get("est_min")})
    with _jobs_lock:
        _jobs[job_id] = job
    return job


_cloud_admit_lock = threading.Lock()     # admission + enqueue is one step, so two submits can't share a budget


def queued_cloud_usd():
    """Estimates of paid jobs still waiting in the queue: they count against the monthly cap."""
    with _jobs_lock:
        return round(sum(j.get("cloud_est") or 0.0 for j in _jobs.values()
                         if j.get("cloud") and j.get("state") == "queued"), 2)


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
    if not g and not job.get("cloud"):     # a rented GPU is not this machine's GPU
        with _gpu_lock:
            g = dict(_gpu)
    out["gpu_stats"] = g or {}
    ua = job.get("updated_at")
    if ua and job.get("state") == "running" and not job.get("paused") and not job.get("cloud") \
            and job.get("stage") != "import":
        out["idle_s"] = max(0, int(time.time() - ua))   # how long since the job last gave a sign of life
    out["log_cursor"] = job_log_cursor(job.get("id"))
    out["preview_id"] = latest_pair(os.path.join(PREVIEW_DIR, job["id"]))
    out["preview"] = out["preview_id"] is not None
    out["segments"] = job_segments(job.get("id"))   # live mosaic-segment before/after clips
    out["sample"] = sample_file(job.get("id")) is not None   # concatenated decensored-only reel
    return out


def _queue_etas(jobs):
    """Seconds each queued job waits before it can start: what the running job has left plus the
    estimated minutes of the paid jobs ahead of it. Only given when every step of that is known."""
    wait = 0
    for j in jobs:
        if j.get("state") in ("running", "replacing", "discarding"):
            wait = None if (wait is None or j.get("eta") is None) else wait + j["eta"]
    out = {}
    for j in jobs:
        if j.get("state") != "queued":
            continue
        out[j["id"]] = None if wait is None else int(wait)
        own = j.get("_est_min")
        wait = None if (wait is None or own is None) else wait + own * 60
    return out


def jobs_snapshot():
    """Every job, oldest first, as the dashboard shows it."""
    with _jobs_lock:
        jobs = list(_jobs.values())
        etas = _queue_etas(jobs)
        rows = [public(j) for j in jobs]
    for row in rows:
        if row["id"] in etas:
            row["eta"] = etas[row["id"]]
    return rows


def set_job(job_id, **fields):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        before = job.get("state")
        job.update(fields)
        # any state change: the Replace/Discard handler moves a job out of review_ready
        # directly, so a review_ready-only check missed it and the job came back on restart
        changed = before != job.get("state")
    if changed:
        save_reviews()


def save_reviews():
    """Write every job awaiting review to REVIEW_STORE (best effort)."""
    with _jobs_lock:
        rows = [dict(j) for j in _jobs.values() if j.get("state") == "review_ready"]
    if not os.path.isdir(os.path.dirname(REVIEW_STORE) or "."):
        return                               # no config folder (a dev box): nothing to persist to
    try:
        tmp = REVIEW_STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, default=str)
        os.replace(tmp, REVIEW_STORE)
    except Exception as exc:  # noqa: BLE001
        logging.warning(f"Could not save the review list: {exc}")


def load_reviews():
    """Bring back the jobs that were awaiting review when the process last stopped."""
    try:
        with open(REVIEW_STORE, encoding="utf-8") as fh:
            rows = json.load(fh)
    except Exception:  # noqa: BLE001 - nothing saved yet
        return 0
    with _jobs_lock:
        for j in rows:
            if isinstance(j, dict) and j.get("id") and j.get("_info"):
                j.setdefault("_overrides", {})
                _jobs.setdefault(j["id"], j)
    return len(rows)


# What the card shows besides the number. Always set from a status (a None clears it), so a step or
# time left never outlives the phase it belonged to.
STATUS_KEYS = ("phase", "step", "steps", "detail", "pass_no", "passes", "estimate",
               "frame", "total_frames", "fps", "eta")


def progress_cb(job_id):
    last = {"msg": None, "frac": 0.0}

    def cb(frac, msg=None, stats=None):
        last["frac"] = max(last["frac"], round(float(frac), 3))      # the bar never goes backwards
        fields = {"progress": last["frac"]}
        stats = stats or {}
        if msg:
            fields["message"] = msg
            if msg != last["msg"]:            # a new stage/step -> log it once
                last["msg"] = msg
                push_log(job_id, msg, "event")
            if "phase" not in stats:          # the coordinator's own step: its message is already plain words
                stats = dict(stats, phase=msg, step=None, steps=None, detail=None, pass_no=None, passes=None,
                             estimate=False)
        for k in ("stage", "indeterminate", "cloud_phase"):
            if stats.get(k) is not None:
                fields[k] = stats[k]
        for k in STATUS_KEYS:
            if k in stats:
                fields[k] = stats[k]
        fields["updated_at"] = stats.get("updated_at") or time.time()
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
    if job.get("_cloud"):                  # paid cloud run: backend_decensor sees cfg["cloud"], rents a GPU
        cfg["cloud"] = dict(job["_cloud"], job_id=job["id"])
    return cfg


def do_process(job):
    job_id = job["id"]
    cfg = job_config(job)
    cfg["_preview_dir"] = os.path.join(PREVIEW_DIR, job_id)
    cfg["_finish_preview"] = _mirror_segments
    set_job(job_id, state="running", message="Starting", started_at=time.time(),
            paused=False, stage=None, frame=None, total_frames=None, fps=None, eta=None,
            indeterminate=True, backend=cfg.get("backend"), phase="Starting", step=None, steps=None,
            detail=None, pass_no=None, passes=None, estimate=False, updated_at=time.time())
    stash = get_stash()
    info = core.process_to_review(stash, cfg, job["scene_id"],
                                  progress=progress_cb(job_id), log_cb=raw_log_sink(job_id))
    set_job(
        job_id, state="review_ready", progress=1.0, message="Preview ready to review",
        indeterminate=False, phase="Ready to review", step=None, steps=None, detail=None, eta=None,
        pass_no=None, passes=None, estimate=False,
        review_scene_id=info.get("review_scene_id"), output_path=info.get("output_path"),
        result=info.get("result") or None, _info=info, _ended_at=time.time(),
    )


def do_replace(job):
    job_id = job["id"]
    info = job.get("_info")
    if not info:
        raise RuntimeError("Nothing to replace (no preview).")
    # fresh clock + no leftovers from the processing run: this phase shows its own steps and elapsed only
    set_job(job_id, state="replacing", progress=0.0, message="Replacing original", indeterminate=False,
            started_at=time.time(), eta=None, fps=None, estimate=False)
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
            msg = cloud_jasna.redact(exc)      # a key or licence must never reach the job log
            push_log(job_id, "ERROR: " + msg, "error")
            set_job(job_id, state="error", message=msg, error=msg, _ended_at=time.time())
        finally:
            core.set_log(BASE_LOG)
            core.clear_job_stamp()
            with _jobs_lock:
                _running_job_id = None


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

_GZIP_TYPES = (".html", ".js", ".css", ".svg", ".json", ".webmanifest", ".txt")
_gz_cache = {}                      # path -> (mtime_ns, size, gzipped bytes)


def static_file(full, accept_gzip):
    """(body, content type, is gzipped) for one file of the built dashboard. index.html gets the
    worker token written in (it is how the dashboard signs itself in); gzipped copies of the
    other text files are made once and kept until the file changes."""
    ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
    st = os.stat(full)
    zip_it = accept_gzip and full.endswith(_GZIP_TYPES)
    if zip_it and not full.endswith("index.html"):
        hit = _gz_cache.get(full)
        if hit and hit[:2] == (st.st_mtime_ns, st.st_size):
            return hit[2], ctype, True
    with open(full, "rb") as fh:
        body = fh.read()
    if full.endswith("index.html"):
        body = body.replace(b"__WORKER_TOKEN__", TOKEN.encode())
    if zip_it:
        body = gzip.compress(body, 6, mtime=0)
        if not full.endswith("index.html"):
            _gz_cache[full] = (st.st_mtime_ns, st.st_size, body)
    return body, ctype, zip_it


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
        """Serve the built dashboard for any GET not under /api: hashed files under assets/
        cache for a year, index.html never (it carries the token and names the assets), and
        text files go out gzipped when the browser accepts it. A path with no file extension
        is a screen of the single-page app and gets index.html; a missing file does not."""
        rel = posixpath.normpath("/" + raw).lstrip("/") or "index.html"
        root = os.path.abspath(WEBUI_DIR)
        full = os.path.abspath(os.path.join(root, rel.replace("/", os.sep)))
        if os.path.commonpath([full, root]) != root or not os.path.isfile(full):
            if posixpath.splitext(rel)[1]:
                return self._send(404, {"error": "not found"})
            rel, full = "index.html", os.path.join(root, "index.html")
        try:
            body, ctype, encoded = static_file(full, "gzip" in self.headers.get("Accept-Encoding", ""))
        except OSError:
            return self._send(404, {"error": "WebUI not installed on this worker"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "public, max-age=31536000, immutable" if rel.startswith("assets/")
                         else "no-cache" if rel == "index.html" else "public, max-age=3600")
        if encoded:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
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
        if sort not in SCENE_SORTS:
            return self._send(400, {"error": "sort must be one of " + ", ".join(SCENE_SORTS)})
        direction = (g("dir", "") or ("ASC" if sort == "title" else "DESC")).upper()
        if direction not in ("ASC", "DESC"):
            return self._send(400, {"error": "dir must be asc or desc"})
        if sort == "random":                     # a stable shuffle: the same seed gives the same order
            sort = "random_" + (re.sub(r"\D", "", g("seed", "")) or "0")[:9]
        f = {
            "q": g("q", ""),
            "page": core._int(g("page", "1"), 1),
            "per_page": min(60, max(1, core._int(g("per_page", "36"), 36))),
            "sort": sort,
            "direction": direction,
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

    def proxy_media(self, kind, scene_id, hls=False, segment=None):
        """Relay Stash's media without exposing its API key to the browser."""
        import requests

        base = stash_base() + "/scene/" + scene_id + "/"
        path = "screenshot" if kind == "img" else "stream"
        if hls:
            path += ".m3u8" if segment is None else f".m3u8/{segment}.ts"
        headers = {"Accept-Encoding": "identity"}
        if STASH_API_KEY:
            headers["ApiKey"] = STASH_API_KEY
        rng = self.headers.get("Range")
        if rng and not (hls and segment is None):
            headers["Range"] = rng
        try:
            up = requests.get(base + path, headers=headers, stream=True, timeout=60,
                              allow_redirects=not hls, params={"resolution": "FULL_HD"} if hls else None)
        except requests.RequestException:
            return self._send(502, {"error": "Could not connect to Stash media"})
        with up:
            if hls and 300 <= up.status_code < 400:
                return self._send(502, {"error": "Unexpected Stash stream redirect"})
            if hls and segment is None and up.status_code == 200:
                try:
                    playlist = up.text
                except requests.RequestException:
                    return self._send(502, {"error": "Could not read the Stash video playlist"})
                return self._send_hls_playlist(playlist, base + path, scene_id)
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
            except OSError:
                pass  # browser cancelled a range, or Stash closed the stream

    def _send_hls_playlist(self, playlist, url, scene_id):
        # Stash emits a VOD playlist of MPEG-TS segments. Only its exact same-scene
        # segment URLs are allowed, and their credentials/query never leave the server.
        origin = urllib.parse.urlsplit(url)
        pattern = re.escape(origin.path) + r"/([0-9]+)\.ts"
        tags = {"#EXTM3U", "#EXT-X-VERSION", "#EXT-X-MEDIA-SEQUENCE", "#EXT-X-TARGETDURATION",
                "#EXT-X-PLAYLIST-TYPE", "#EXTINF", "#EXT-X-ENDLIST", "#EXT-X-DISCONTINUITY",
                "#EXT-X-INDEPENDENT-SEGMENTS"}
        lines = playlist.strip().splitlines()
        if not lines or lines[0] != "#EXTM3U":
            return self._send(502, {"error": "Invalid Stash video playlist"})
        for i, line in enumerate(lines):
            if not line:
                continue
            if line.startswith("#"):
                if line.split(":", 1)[0] not in tags:
                    return self._send(502, {"error": "Unsupported Stash video playlist"})
                continue
            part = urllib.parse.urlsplit(urllib.parse.urljoin(url, line))
            match = re.fullmatch(pattern, part.path)
            if not match or (part.scheme, part.netloc) != (origin.scheme, origin.netloc):
                return self._send(502, {"error": "Unexpected Stash video segment"})
            lines[i] = f"{scene_id}.m3u8/{match.group(1)}.ts"
        body = ("\n".join(lines) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def send_video_file(self, path):
        """Serve local sample/moment clips with the byte ranges required by iPhone video."""
        try:
            fh = open(path, "rb")
        except OSError:
            return self._send(404, {"error": "Review clip is missing"})
        with fh:
            size = os.fstat(fh.fileno()).st_size
            first, last = 0, size - 1
            match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", self.headers.get("Range", ""))
            partial = bool(match and any(match.groups()))
            if partial:
                start, end = match.groups()
                if start:
                    first = int(start)
                    if end:
                        last = min(last, int(end))
                else:
                    first = max(0, size - int(end))
                if first > last:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self._cors()
                    self.end_headers()
                    return
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            if partial:
                self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
            left = last - first + 1
            self.send_header("Content-Length", str(left))
            self._cors()
            self.end_headers()
            fh.seek(first)
            try:
                while left and (chunk := fh.read(min(65536, left))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    # -- paid cloud Jasna: thin routing only; the rules and refusals live in cloud_api.py --

    def cloud_get(self, path):
        from urllib.parse import urlparse, parse_qs
        query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        cl = cloud_jasna.cloud()
        if path == "/api/cloud/settings":
            return self._send(*cloud_api.settings_get(cl))
        if path == "/api/cloud/status":
            return self._send(*cloud_api.status(cl))
        if path == "/api/cloud/offers":
            meta = None
            # cost the market for one scene, or for several (`scene_ids`, comma-separated): each
            # is its own rental on the machine, so the estimate is their sum
            ids = [i.strip() for i in (query.get("scene_ids") or query.get("scene") or "").split(",") if i.strip()][:20]
            if ids:
                try:
                    meta = cloud_api.scene_metas(stash_gql, ids)
                except Exception as exc:  # noqa: BLE001
                    return self._send(502, {"error": "stash: " + cloud_jasna.redact(exc)})
                if any(m is None for m in meta):
                    return self._send(404, {"error": "Scene not found in Stash.", "code": "no_scene"})
            return self._send(*cloud_api.offers(cl, query, meta))
        if path == "/api/cloud/quote":
            ids = [i.strip() for i in query.get("scenes", "").split(",") if i.strip()][:20]
            try:
                metas = cloud_api.scene_metas(stash_gql, ids)
            except Exception as exc:  # noqa: BLE001
                return self._send(502, {"error": "stash: " + cloud_jasna.redact(exc)})
            return self._send(*cloud_api.quotes(cl, list(zip(ids, metas)), query.get("preset", ""),
                                                queued_cloud_usd(), query))
        return self._send(404, {"error": "not found"})

    def cloud_post(self, path):
        cl = cloud_jasna.cloud()
        if path == "/api/cloud/settings":
            return self._send(*cloud_api.settings_save(cl, self._body()))
        if path == "/api/cloud/test":
            return self._send(*cloud_api.test_key(cl, self._body()))
        return self._send(404, {"error": "not found"})

    def submit_cloud(self, scene_id, body, overrides):
        """A paid run. Key/licence, both caps and the confirmed price are re-checked HERE,
        whatever the dashboard did."""
        try:
            (meta,) = cloud_api.scene_metas(stash_gql, [scene_id])
        except Exception as exc:  # noqa: BLE001
            return self._send(502, {"error": "stash: " + cloud_jasna.redact(exc)})
        backend = overrides.get("backend") or core.config_from_env().get("backend")
        with _cloud_admit_lock:
            status, payload = cloud_api.admit(cloud_jasna.cloud(), body, meta, queued_cloud_usd(), backend)
            if status != 202:
                return self._send(status, payload)
            # what the job IS follows the validated plan, whatever else the body carried
            op = payload["op"]
            overrides = dict(overrides, backend=op if op in ("upscale", "transcode") else "decensor",
                             post_upscale=op == "decensor+upscale")
            job = new_job(scene_id, overrides, cloud=payload, title=(meta or {}).get("title"))
            _work.put(("process", job["id"]))
        return self._send(202, public(job))

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
            return self._send(200, gpus_snapshot())
        # Media proxies for the dashboard's <img>/<video> (which can't send the
        # token header) — left open; the worker sits behind your proxy's auth.
        m = re.match(r"^/api/(img|vid)/(\d+)$", path)
        if m:
            return self.proxy_media(m.group(1), m.group(2))
        m = re.fullmatch(r"/api/vid/([0-9]+)\.m3u8(?:/([0-9]+)\.ts)?", path)
        if m:
            return self.proxy_media("vid", m.group(1), hls=True, segment=m.group(2))
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
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            p = preview_file(m.group(1), m.group(2), query.get("pair", [None])[0])
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
            return self.send_video_file(p)
        # Live segment preview clips (loaded by <video>, so unauth'd like the jpgs).
        m = re.match(r"^/api/jobs/([0-9a-f]+)/seg/(\d+)/(before|after)\.mp4$", path)
        if m:
            p = seg_file(m.group(1), m.group(2), m.group(3))
            if not p:
                return self._send(404, {"error": "no segment"})
            return self.send_video_file(p)
        if not self._authed():
            return self._send(401, {"error": "bad token"})
        if path == "/api/events":
            return events.serve(self, event_hub())
        if path.startswith("/api/cloud/"):
            return self.cloud_get(path)
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
            return self._send(200, jobs_snapshot())
        if path == "/api/runners":
            return self._send(200, runners_snapshot())
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

        if path.startswith("/api/cloud/"):
            return self.cloud_post(path)

        if path == "/api/decensor":
            body = self._body()
            scene_id = body.get("scene_id") or body.get("sceneId")
            if not scene_id:
                return self._send(400, {"error": "scene_id required"})
            overrides = {k: body[k] for k in OVERRIDES if k in body}
            if body.get("cloud_preset") or cloud_jasna.truthy(body.get("cloud")):
                return self.submit_cloud(str(scene_id), body, overrides)
            job = new_job(str(scene_id), overrides, title=scene_title(str(scene_id)))
            _work.put(("process", job["id"]))
            return self._send(202, public(job))

        # --- runner registry management ---
        if path == "/api/runners":                     # add / update a runner
            b = self._body()
            url = str(b.get("url", "")).rstrip("/")
            if not url.startswith("http"):
                return self._send(400, {"error": "a valid http(s) url is required"})
            with _runners_lock:
                store = load_store()
                old = next((r for r in store if str(r.get("url", "")).rstrip("/") == url), {})
                # a field the edit leaves out keeps what is stored; a runner added without a
                # token of its own gets the fleet token (it probes online either way, but would
                # 401 on dispatch)
                entry = {"name": b.get("name") or old.get("name") or url, "url": url,
                         "token": b.get("token") or old.get("token") or os.environ.get("WORKER_TOKEN", ""),
                         "ops": (b["ops"] or None) if "ops" in b else old.get("ops"),
                         "prefer": b["prefer"] or [] if "prefer" in b else old.get("prefer") or []}
                mac = b["mac"] if "mac" in b else old.get("mac")
                if mac:
                    entry["mac"] = mac
                save_store([r for r in store if r is not old] + [entry])
            return self._send(201, probe_runner(dict(entry, _source="manual")))
        if path == "/api/runners/remove":
            url = str(self._body().get("url", "")).rstrip("/")
            with _runners_lock:
                save_store([r for r in load_store() if str(r.get("url", "")).rstrip("/") != url])
            return self._send(200, {"ok": True})
        if path == "/api/runners/test":
            b = self._body()
            r = probe_runner({"url": b.get("url"), "token": b.get("token"), "name": b.get("name"), "_source": "test"})
            return self._send(200, {"ok": r["online"], "error": None if r["online"] and not r["note"]
                                    else (r["note"] or "Could not reach it."),
                                    "ops": r["ops"], "engines": r["engines"], "node": r["node"], "kind": r["kind"]})
        if path.startswith("/api/runners/") and path.endswith("/wake") and len(path) > len("/api/runners//wake"):
            name = urllib.parse.unquote(path[len("/api/runners/"):-len("/wake")])   # a Wake-on-LAN packet for a sleeping runner
            entry = next((r for r in merged_runners() if name in (r.get("name"), str(r.get("url", "")).rstrip("/"))), None)
            if entry is None:
                return self._send(404, {"error": "no such runner"})
            if not entry.get("mac"):
                return self._send(400, {"error": "This runner has no MAC address, so it cannot be woken."})
            if not core.wake_runner(entry):
                return self._send(502, {"error": "The wake packet could not be sent."})
            return self._send(200, {"ok": True, "eta_s": wake_eta()})
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

    load_reviews()
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=gpu_poller, daemon=True).start()
    threading.Thread(target=preview_poller, daemon=True).start()
    try:
        cloud_jasna.cloud().start_reaper()   # destroys stray rented machines at startup and every few minutes
    except Exception as exc:  # noqa: BLE001 - a cloud problem must never stop the server
        logging.warning(f"Cloud reaper not started: {exc}")
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    logging.info(f"Stashify server listening on :{PORT} (token {'on' if TOKEN else 'off'})")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
