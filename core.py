"""Shared decensor pipeline logic.

Used by two entrypoints:
  - plugin.py : in-Stash raw plugin (reads server_connection from stdin)
  - worker.py : standalone Docker daemon (connects via STASH_URL + API key)

Both build a StashInterface + a config dict and call run(). Logging is
injectable via set_log() so the plugin can route to Stash's native log/progress
while the worker logs plainly to stdout.
"""

import os
import re
import json
import time
import uuid
import shlex
import signal
import shutil
import logging
import tempfile
import threading
import subprocess
from urllib.parse import urlparse


# --------------------------------------------------------------------------- #
# logging (injectable)
# --------------------------------------------------------------------------- #

class _StdLog:
    """Default logger: plain Python logging, plus a progress() shim so it is
    API-compatible with stashapi.log."""

    def __init__(self, logger=None):
        self._l = logger or logging.getLogger("decensor")

    def debug(self, m):
        self._l.debug(m)

    def info(self, m):
        self._l.info(m)

    def warning(self, m):
        self._l.warning(m)

    def error(self, m):
        self._l.error(m)

    def progress(self, frac):
        try:
            self._l.info(f"progress: {float(frac) * 100:.0f}%")
        except Exception:  # noqa: BLE001
            pass


log = _StdLog()


def set_log(obj):
    """Swap the logger (e.g. plugin.py passes stashapi.log)."""
    global log
    log = obj


# --------------------------------------------------------------------------- #
# constants / config
# --------------------------------------------------------------------------- #

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".wmv", ".flv", ".ts"}

# Suffixes the various tools tack on; stripped when matching a cleaned file back
# to its original scene during folder import.
SUFFIX_RE = re.compile(r"(_decensored|_out|_clean|_cleaned|_tg|_upscaled|_x\d+|_\d+)+$", re.IGNORECASE)

DEFAULTS = {
    # selection / bookkeeping
    "triggerTag": "Decensor",
    "doneTag": "Decensored",
    "outputDir": "",
    "importResult": True,
    "gpuId": 0,
    # pipeline: all GPU work happens on a runner (lada_runner.py); the worker
    # is a thin coordinator.  backend: lada | upscale | command
    "backend": "lada",
    "postUpscale": False,            # lada backend: chain decensor -> upscale on the runner
    # generic command backend
    "commandTemplate": "",
    # Lada/compute runner (separate GPU container; see lada_runner.py)
    "ladaUrl": "",                   # e.g. http://runner:8711
    "ladaToken": "",
    "ladaScratch": "/scratch",       # shared mount both worker + runner see
    "ladaRestModel": "basicvsrpp-v1.2",
    "ladaDetModel": "v4-fast",       # v4-fast | v4-accurate | v2
    "ladaFp16": False,               # Pascal P40: keep False (no usable fp16)
    "ladaDevice": "cuda",
    "ladaEncoder": "",               # "" = runner default (NVENC probe result)
    "ladaUpscaleModel": "",          # "" = runner default (2xLiveActionV1_SPAN)
    # multi-runner registry (optional): a JSON array in the RUNNERS env var of
    # {name,url,token,ops:[...],prefer:[...]}; jobs route to a capable node,
    # falling back to the single ladaUrl runner. Lets the always-on P40 keep
    # decensoring while an intermittent desktop box takes upscale/transcode.
    "runners": [],
    # transcode op params (backend=transcode)
    "transcodeCodec": "",            # informational; encoder decides
    "transcodeHeight": "",           # "" = keep source resolution
    "transcodeQuality": "24",
}

SCENE_FRAGMENT = """
id
title
details
date
rating100
files { path }
tags { id name }
performers { id }
studio { id }
"""


def _int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def validate(cfg):
    problems = []
    if not str(cfg["outputDir"]).strip():
        problems.append("Output Directory")

    backend = cfg["backend"]
    if backend not in BACKENDS:
        problems.append(f"backend '{backend}' (use lada | upscale | transcode | command)")
    if backend == "command" and not str(cfg["commandTemplate"]).strip():
        problems.append("Command Template")
    if backend in ("lada", "upscale", "transcode") and not (
            str(cfg["ladaUrl"]).strip() or cfg.get("runners")):
        problems.append("a runner (LADA_URL or RUNNERS)")

    if problems:
        message = "Missing/invalid config: " + ", ".join(problems)
        log.error(message)
        raise ValueError(message)


# --------------------------------------------------------------------------- #
# subprocess helpers
# --------------------------------------------------------------------------- #

def cuda_env(cfg):
    """Pin the CUDA device (or force CPU) for tools that honor the env var."""
    env = os.environ.copy()
    gpu = _int(cfg["gpuId"])
    env["CUDA_VISIBLE_DEVICES"] = "" if gpu < 0 else str(gpu)
    return env


class Cancelled(Exception):
    """Raised when the user cancels the running job."""


# The currently-running subprocess + a cancel flag, so the HTTP layer can
# cancel/pause/resume it. Only one job runs at a time (single worker thread).
_active = {"proc": None, "cancel": False}
_active_lock = threading.Lock()
_HAVE_PGID = hasattr(os, "killpg") and hasattr(os, "getpgid")

# Optional hooks for a job whose real work runs on a remote runner (the Lada
# backend): the local process signals do nothing there, so pause/resume/cancel
# forward to the runner via these. Set by the backend for its duration.
_remote = {"cancel": None, "pause": None, "resume": None}


def set_remote_controls(cancel=None, pause=None, resume=None):
    _remote["cancel"], _remote["pause"], _remote["resume"] = cancel, pause, resume


def clear_remote_controls():
    _remote["cancel"] = _remote["pause"] = _remote["resume"] = None


def _fire_remote(name):
    fn = _remote.get(name)
    if not fn:
        return False
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001 - a remote hiccup must not crash control flow
        return False


# Live-preview registration: the running backend advertises where its
# work-in-progress frames can be tapped (server.py's preview poller extracts
# before/after JPEGs from it). Single running job -> a simple module global.
_live_preview = {}


def set_live_preview(**info):
    _live_preview.clear()
    _live_preview.update(info)


def clear_live_preview():
    _live_preview.clear()


def get_live_preview():
    return dict(_live_preview)


# Stamp metadata (which runner + engine actually handled the job) onto the
# running job so the dashboard can show it. The server registers a setter bound
# to the current job id; single running job -> a module global is enough (same
# assumption as set_log / the remote controls above).
_job_stamp = {"fn": None}


def set_job_stamp(fn):
    _job_stamp["fn"] = fn


def clear_job_stamp():
    _job_stamp["fn"] = None


def stamp_job(**fields):
    fn = _job_stamp.get("fn")
    if fn:
        try:
            fn(**fields)
        except Exception:  # noqa: BLE001 - a stamp failure must never affect the job
            pass


def reset_cancel():
    with _active_lock:
        _active["cancel"] = False


def check_cancel():
    with _active_lock:
        c = _active["cancel"]
    if c:
        raise Cancelled("cancelled by user")


def _signal_active(sig):
    """Signal the running subprocess's whole process group (tool + its ffmpeg)."""
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


def cancel_active():
    with _active_lock:
        _active["cancel"] = True
    # SIGKILL the whole group: reliable, and works even if the job is paused (SIGSTOP).
    _signal_active(getattr(signal, "SIGKILL", signal.SIGTERM))
    _fire_remote("cancel")   # also stop a remote runner job, if any
    return True              # cancel is authoritative: the flag makes check_cancel() raise


def pause_active():
    # Prefer the remote hook (Lada runner) when present; else signal the local group.
    if _remote.get("pause"):
        return _fire_remote("pause")
    return _signal_active(signal.SIGSTOP) if hasattr(signal, "SIGSTOP") else False


def resume_active():
    if _remote.get("resume"):
        return _fire_remote("resume")
    return _signal_active(signal.SIGCONT) if hasattr(signal, "SIGCONT") else False


def run_cmd(cmd, cwd=None, env=None, tag="proc", on_line=None, log_cb=None):
    """Run a command, streaming output. Each line is passed to on_line (for live
    progress parsing) and log_cb (for the live-log buffer); the last 15 lines are
    logged at debug on completion. The child is its own session leader so the
    whole group can be paused/cancelled."""
    log.debug("Running: " + " ".join(cmd))
    kw = {"cwd": cwd, "env": env, "stdout": subprocess.PIPE,
          "stderr": subprocess.STDOUT, "text": True, "bufsize": 1}
    if _HAVE_PGID:
        kw["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kw)
    with _active_lock:
        _active["proc"] = proc
    tail = []
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            tail.append(line)
            if len(tail) > 15:
                tail.pop(0)
            if on_line:
                try:
                    on_line(line)
                except Exception:  # noqa: BLE001 - progress parsing must never break the run
                    pass
            if log_cb:
                try:
                    log_cb(line)
                except Exception:  # noqa: BLE001 - log streaming must never break the run
                    pass
        proc.wait()
    finally:
        with _active_lock:
            _active["proc"] = None
    for line in tail:
        log.debug(f"[{tag}] {line}")
    with _active_lock:
        cancelled = _active["cancel"]
    if cancelled:
        raise Cancelled(f"{tag} cancelled")
    if proc.returncode != 0:
        raise RuntimeError(f"{tag} exited with code {proc.returncode}")


def newest_video(result_dir):
    vids = [
        os.path.join(result_dir, f)
        for f in os.listdir(result_dir)
        if os.path.splitext(f)[1].lower() in VIDEO_EXTS
    ]
    if not vids:
        raise RuntimeError(f"No video output was produced in {result_dir}")
    vids.sort(key=os.path.getmtime)
    return vids[-1]


# --------------------------------------------------------------------------- #
# backends: (cfg, input_path, result_dir) -> produced_video_path
# --------------------------------------------------------------------------- #

def backend_command(cfg, input_path, result_dir, on_line=None, log_cb=None):
    """Generic backend. Template placeholders (each must be its own token):
       {input}      absolute path to the source video
       {output_dir} directory to write the result into
       {gpu}        configured GPU id
    """
    tokens = shlex.split(cfg["commandTemplate"])
    subs = {"{input}": input_path, "{output_dir}": result_dir,
            "{output}": result_dir, "{gpu}": str(_int(cfg["gpuId"]))}
    argv = []
    for tok in tokens:
        for placeholder, value in subs.items():
            tok = tok.replace(placeholder, value)
        argv.append(tok)
    run_cmd(argv, env=cuda_env(cfg), tag="command", on_line=on_line, log_cb=log_cb)
    return newest_video(result_dir)


def resolve_runner(cfg, op):
    """Pick a runner (url, token, name, engine) that can do `op`. Health-checks
    each candidate, skips offline/unreachable ones, and prefers a runner that
    tags `op` in its 'prefer' list and is idle. Falls back to the single ladaUrl
    runner (which is treated as capable of everything it reports).

    Optional per-job pins (from the dashboard, via cfg):
      - targetRunner: only this runner (matched on name OR url) is eligible.
      - targetEngine: only runners whose engine for `op` equals this are eligible.
    A pin that matches no online/capable runner raises (fail loud) rather than
    silently rerouting - the user's intent is honored."""
    import requests

    target_runner = str(cfg.get("targetRunner") or "").strip().rstrip("/")
    target_engine = str(cfg.get("targetEngine") or "").strip()
    if target_runner.lower() == "auto":
        target_runner = ""
    if target_engine.lower() == "auto":
        target_engine = ""

    candidates = list(cfg.get("runners") or [])
    if str(cfg.get("ladaUrl") or "").strip():
        candidates.append({"name": "default", "url": cfg["ladaUrl"],
                           "token": cfg.get("ladaToken", ""), "ops": None,
                           "prefer": [], "engines": None})
    scored = []
    for c in candidates:
        url = str(c.get("url") or "").rstrip("/")
        if not url:
            continue
        name = c.get("name", "runner")
        if target_runner and target_runner not in (name, url):
            continue
        declared = c.get("ops")
        if declared is not None and op not in declared:
            continue
        try:
            h = requests.get(url + "/health",
                             headers={"X-Lada-Token": c.get("token", "")}, timeout=4).json()
        except Exception:  # noqa: BLE001 - offline node: skip, try the next
            continue
        if op not in (h.get("ops") or []) or h.get("paused"):
            continue
        engine = (h.get("engines") or {}).get(op) or (c.get("engines") or {}).get(op)
        if target_engine and engine != target_engine:
            continue
        # idle dominates preference: an idle node always outranks a busy one, and
        # 'prefer' only breaks ties among equally-idle/equally-busy nodes.
        score = (2 if not h.get("busy") else 0) + (1 if op in (c.get("prefer") or []) else 0)
        scored.append((score, {"url": url, "token": c.get("token", ""),
                               "name": name, "engine": engine}))
    if not scored:
        if target_runner:
            raise RuntimeError(f"runner '{target_runner}' is offline or can't do '{op}'")
        if target_engine:
            raise RuntimeError(f"no online runner provides engine '{target_engine}' for '{op}'")
        raise RuntimeError(f"no online runner can handle '{op}'")
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def preview_route(cfg, op):
    """Dry-run of resolve_runner for the dashboard: which runner + engine WOULD
    handle this op right now (honoring any targetRunner/targetEngine pin).
    Returns {"runner","engine"} or {"error"}; never dispatches a job."""
    try:
        r = resolve_runner(cfg, op)
        return {"runner": r.get("name"), "engine": r.get("engine")}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _runner_dispatch(cfg, input_path, result_dir, op, on_line=None, log_cb=None):
    """Dispatch a GPU job (decensor / upscale / transcode / chain) to a compute
    runner (lada_runner.py or the Windows runner) over HTTP.

    The runner writes its output into a shared scratch dir (both containers
    mount it at the same path); we tail its log — relaying each line through
    on_line (so the existing frame/fps/progress parser drives the job's stats)
    and log_cb (live-log) — then move the produced file into result_dir.
    Cancel/pause/resume forward to the runner via the remote-control hooks."""
    import requests

    runner = resolve_runner(cfg, op)
    base = runner["url"]
    token = runner["token"]
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Lada-Token"] = token
    log.info(f"routing {op} -> runner '{runner['name']}' ({base}) engine={runner.get('engine')}")
    # record which node/engine actually handled this job, for the dashboard
    stamp_job(runner=runner.get("name"), engine=runner.get("engine"))

    scratch = str(cfg.get("ladaScratch") or "/scratch")
    sub = os.path.join(scratch, "lada_" + uuid.uuid4().hex[:10])
    os.makedirs(sub, exist_ok=True)
    try:
        os.chmod(sub, 0o777)   # a remote (Windows/SMB) runner must be able to write here
    except OSError:
        pass

    payload = {
        "op": op,
        "input": input_path,
        "output_dir": sub,
        "restoration_model": cfg.get("ladaRestModel") or "basicvsrpp-v1.2",
        "detection_model": cfg.get("ladaDetModel") or "v4-fast",
        "fp16": bool(cfg.get("ladaFp16", False)),
        "device": cfg.get("ladaDevice") or "cuda",
        "encoder": cfg.get("ladaEncoder") or "",
        "upscale_model": cfg.get("ladaUpscaleModel") or "",
        "codec": cfg.get("transcodeCodec") or "",
        "height": cfg.get("transcodeHeight") or "",
        "quality": cfg.get("transcodeQuality") or "24",
    }

    def _post(action):
        try:
            r = requests.post(f"{base}/jobs/{rid}/{action}", headers=headers, timeout=10)
            return r.ok
        except Exception:  # noqa: BLE001
            return False

    try:
        r = requests.post(base + "/run", headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        rid = r.json()["id"]
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(sub, ignore_errors=True)
        raise RuntimeError(f"Lada runner unreachable at {base}: {exc}")

    log.info(f"Runner job {rid} ({op}) on {base}")
    set_remote_controls(cancel=lambda: _post("cancel"),
                        pause=lambda: _post("pause"),
                        resume=lambda: _post("resume"))
    # The runner writes a fragmented mp4 (--mp4-fast-start): the worker can
    # tail-follow the growing file for the live video feed, and the runner
    # extracts before/after compare frames (rid/base let the worker fetch them).
    set_live_preview(type="lada", out_dir=sub, input=input_path,
                     rid=rid, base=base, token=token)
    cursor = 0
    try:
        while True:
            check_cancel()
            try:
                jb = requests.get(f"{base}/jobs/{rid}", headers=headers, timeout=20).json()
            except Exception as exc:  # noqa: BLE001 - transient; keep polling
                log.warning(f"Lada poll error: {exc}")
                time.sleep(2)
                continue
            try:
                lg = requests.get(f"{base}/jobs/{rid}/log?after={cursor}", headers=headers, timeout=20).json()
                for ln in lg.get("lines", []):
                    text = ln.get("text", "")
                    if on_line:
                        try:
                            on_line(text)      # feeds the frame/fps/progress parser
                        except Exception:      # noqa: BLE001
                            pass
                    if log_cb:
                        try:
                            log_cb(text)
                        except Exception:      # noqa: BLE001
                            pass
                cursor = lg.get("cursor", cursor)
            except Exception:  # noqa: BLE001
                pass
            # The runner parses lada-cli's progress into structured fields; relay
            # them as a synthetic tqdm-style line so _band's parser (frame "N/M [",
            # fps "it/s") is guaranteed to fire regardless of lada's raw format.
            if on_line and jb.get("frame") and jb.get("total_frames"):
                synth = f"{jb['frame']}/{jb['total_frames']} ["
                if jb.get("fps"):
                    synth += f" {jb['fps']} it/s"
                try:
                    on_line(synth)
                except Exception:  # noqa: BLE001
                    pass
            state = jb.get("state")
            if state == "done":
                break
            if state == "error":
                raise RuntimeError("Lada runner: " + str(jb.get("error") or jb.get("message") or "failed"))
            if state == "cancelled":
                raise Cancelled("cancelled on lada runner")
            time.sleep(2)

        produced = newest_video(sub)
        dest = os.path.join(result_dir, os.path.basename(produced))
        shutil.move(produced, dest)
        _chown_like(dest, input_path)
        return dest
    finally:
        clear_remote_controls()
        clear_live_preview()
        shutil.rmtree(sub, ignore_errors=True)


def backend_lada(cfg, input_path, result_dir, on_line=None, log_cb=None):
    op = "decensor+upscale" if cfg.get("postUpscale") else "decensor"
    return _runner_dispatch(cfg, input_path, result_dir, op, on_line=on_line, log_cb=log_cb)


def backend_upscale(cfg, input_path, result_dir, on_line=None, log_cb=None):
    return _runner_dispatch(cfg, input_path, result_dir, "upscale", on_line=on_line, log_cb=log_cb)


def backend_transcode(cfg, input_path, result_dir, on_line=None, log_cb=None):
    return _runner_dispatch(cfg, input_path, result_dir, "transcode", on_line=on_line, log_cb=log_cb)


BACKENDS = {
    "lada": backend_lada,
    "upscale": backend_upscale,
    "transcode": backend_transcode,
    "command": backend_command,
}


# --------------------------------------------------------------------------- #
# stash import / metadata
# --------------------------------------------------------------------------- #

def _chown_like(target, reference):
    """Best-effort: give `target` the same owner/group as `reference`.

    The worker runs as root, so files it writes are root-owned, whereas the media
    library is owned by Stash's user (e.g. PUID 99 on unRAID). Matching the
    reference keeps Stash able to delete/organize the file. No-op where os.chown
    is unavailable (e.g. Windows) or not permitted.
    """
    chown = getattr(os, "chown", None)
    if chown is None:
        return
    try:
        st = os.stat(reference)
    except OSError:
        return
    try:
        chown(target, st.st_uid, st.st_gid)
    except OSError:
        pass


def unique_path(directory, stem, ext):
    candidate = os.path.join(directory, f"{stem}{ext}")
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem}_{counter}{ext}")
        counter += 1
    return candidate


def apply_metadata(stash, new_id, original, trigger_tag_id, done_tag_id):
    tag_ids = {t["id"] for t in original.get("tags", [])}
    tag_ids.discard(trigger_tag_id)
    tag_ids.add(done_tag_id)
    update = {
        "id": new_id,
        "tag_ids": list(tag_ids),
        "performer_ids": [p["id"] for p in original.get("performers", [])],
    }
    if original.get("title"):
        update["title"] = f"{original['title']} (Decensored)"
    if original.get("studio"):
        update["studio_id"] = original["studio"]["id"]
    for field in ("details", "date", "rating100"):
        if original.get(field) is not None:
            update[field] = original[field]
    stash.update_scene(update)


def scan_and_wait(stash, path):
    job_id = stash.metadata_scan(paths=[path])
    try:
        stash.wait_for_job(job_id)
    except Exception as exc:  # noqa: BLE001 - scan may still have succeeded
        log.warning(f"Timed out waiting for scan job; continuing. ({exc})")


def import_result(stash, cfg, original, cleaned_path, trigger_tag_id, done_tag_id):
    scan_and_wait(stash, cfg["outputDir"])
    matches = stash.find_scenes(
        f={"path": {"value": cleaned_path, "modifier": "EQUALS"}}, fragment="id"
    )
    if not matches:
        log.warning(
            f"Cleaned file scanned but no scene found for {cleaned_path}. "
            "Is the output directory inside a Stash library path?"
        )
        return
    apply_metadata(stash, matches[0]["id"], original, trigger_tag_id, done_tag_id)
    log.info(f"Imported cleaned scene as id {matches[0]['id']}")


def mark_original_done(stash, original, trigger_tag_id, done_tag_id):
    tag_ids = {t["id"] for t in original.get("tags", [])}
    tag_ids.discard(trigger_tag_id)
    tag_ids.add(done_tag_id)
    stash.update_scene({"id": original["id"], "tag_ids": list(tag_ids)})


# --------------------------------------------------------------------------- #
# processing
# --------------------------------------------------------------------------- #

def process_scene(stash, cfg, scene, trigger_tag_id, done_tag_id):
    files = scene.get("files") or []
    if not files:
        log.warning(f"Scene {scene['id']} has no file, skipping.")
        return False
    input_path = files[0]["path"]
    if not os.path.isfile(input_path):
        log.error(
            f"Scene {scene['id']} file not found at {input_path}. "
            "The worker/plugin must see the media at the same path Stash uses."
        )
        return False

    log.info(f"[{cfg['backend']}] Decensoring: {scene.get('title') or os.path.basename(input_path)}")

    stage_dirs = []

    def new_dir():
        d = tempfile.mkdtemp(prefix="decensor_")
        stage_dirs.append(d)
        return d

    try:
        produced = BACKENDS[cfg["backend"]](cfg, input_path, new_dir())

        os.makedirs(cfg["outputDir"], exist_ok=True)
        stem = re.sub(r"\s+", "_", os.path.splitext(os.path.basename(input_path))[0])
        ext = os.path.splitext(produced)[1] or ".mp4"
        dest = unique_path(cfg["outputDir"], f"{stem}{op_suffix(cfg['backend'])}", ext)
        shutil.move(produced, dest)
        _chown_like(dest, input_path)
        log.info(f"Wrote cleaned file: {dest}")

        if cfg["importResult"]:
            import_result(stash, cfg, scene, dest, trigger_tag_id, done_tag_id)

        mark_original_done(stash, scene, trigger_tag_id, done_tag_id)
        return True
    finally:
        for d in stage_dirs:
            shutil.rmtree(d, ignore_errors=True)


def base_stem(name):
    return SUFFIX_RE.sub("", os.path.splitext(os.path.basename(name))[0])


def find_original_by_name(stash, cleaned_path, output_dir):
    """Best-effort match a cleaned file back to its source scene by filename.

    Skips files under the output dir — those are cleaned outputs, not originals.
    """
    base = base_stem(cleaned_path)
    if not base:
        return None
    out_norm = os.path.normpath(output_dir)
    out_prefix = out_norm + os.sep
    candidates = stash.find_scenes(
        f={"path": {"value": base, "modifier": "INCLUDES"}}, fragment=SCENE_FRAGMENT
    )
    for scene in candidates:
        for fobj in scene.get("files", []):
            path = fobj["path"]
            norm = os.path.normpath(path)
            # Skip the cleaned file itself and anything genuinely under output_dir
            # (use a separator boundary so /out/decensored doesn't match /out/decensored_src).
            if path == cleaned_path or norm == out_norm or norm.startswith(out_prefix):
                continue
            if base_stem(path) == base:
                return scene
    return None


def import_folder(stash, cfg, trigger_tag_id, done_tag_id):
    """Manual JavPlayer/GUI flow: scan the output dir, then copy metadata from
    each cleaned file's matching original."""
    scan_and_wait(stash, cfg["outputDir"])
    scenes = stash.find_scenes(
        f={"path": {"value": cfg["outputDir"], "modifier": "INCLUDES"}},
        fragment=SCENE_FRAGMENT,
    )
    if not scenes:
        log.info(f"No scenes found under {cfg['outputDir']}.")
        return

    total, done = len(scenes), 0
    for index, scene in enumerate(scenes):
        if any(t["id"] == done_tag_id for t in scene.get("tags", [])):
            log.progress((index + 1) / total)
            continue
        cleaned_path = (scene.get("files") or [{}])[0].get("path", "")
        original = find_original_by_name(stash, cleaned_path, cfg["outputDir"])
        if original:
            apply_metadata(stash, scene["id"], original, trigger_tag_id, done_tag_id)
            mark_original_done(stash, original, trigger_tag_id, done_tag_id)
            log.info(f"Matched {os.path.basename(cleaned_path)} -> original {original['id']}")
            done += 1
        else:
            stash.update_scene({"id": scene["id"], "tag_ids": [done_tag_id]})
            log.warning(f"No original matched for {os.path.basename(cleaned_path)}; tagged done only.")
        log.progress((index + 1) / total)
    log.info(f"Folder import complete. {done}/{total} matched to an original.")


def scenes_to_process(stash, cfg, scene_ids=None):
    if scene_ids:
        out = []
        for sid in scene_ids:
            scene = stash.find_scene(int(sid), fragment=SCENE_FRAGMENT)
            if scene:
                out.append(scene)
        return out
    tag = stash.find_tag(cfg["triggerTag"])
    if not tag:
        log.info(f"Trigger tag '{cfg['triggerTag']}' does not exist yet — nothing to do.")
        return []
    return stash.find_scenes(
        f={"tags": {"value": [tag["id"]], "modifier": "INCLUDES_ALL", "depth": 0}},
        fragment=SCENE_FRAGMENT,
    )


def run(stash, cfg, mode="tagged", scene_ids=None):
    """Entrypoint shared by plugin.py and worker.py. Returns processed count."""
    validate(cfg)
    trigger_tag = stash.find_tag(cfg["triggerTag"], create=True)
    done_tag = stash.find_tag(cfg["doneTag"], create=True)

    if mode == "import":
        import_folder(stash, cfg, trigger_tag["id"], done_tag["id"])
        return 0

    scenes = scenes_to_process(stash, cfg, scene_ids)
    if not scenes:
        log.info("No scenes to process.")
        return 0

    total = len(scenes)
    chain = " + upscale" if cfg["postUpscale"] and cfg["backend"] == "lada" else ""
    log.info(f"Processing {total} scene(s) with backend '{cfg['backend']}'{chain}.")
    succeeded = 0
    for index, scene in enumerate(scenes):
        if any(t["id"] == done_tag["id"] for t in scene.get("tags", [])):
            log.debug(f"Scene {scene['id']} already done, skipping.")
            log.progress((index + 1) / total)
            continue
        try:
            if process_scene(stash, cfg, scene, trigger_tag["id"], done_tag["id"]):
                succeeded += 1
        except Exception as exc:  # noqa: BLE001 - one bad scene shouldn't abort the batch
            log.error(f"Failed to decensor scene {scene['id']}: {exc}")
        log.progress((index + 1) / total)

    log.info(f"Done. {succeeded}/{total} scene(s) processed.")
    return succeeded


# --------------------------------------------------------------------------- #
# env config + connection (used by worker.py and server.py)
# --------------------------------------------------------------------------- #

_ENV_MAP = {
    "triggerTag": "TRIGGER_TAG",
    "doneTag": "DONE_TAG",
    "outputDir": "OUTPUT_DIR",
    "gpuId": "GPU_ID",
    "backend": "BACKEND",
    "commandTemplate": "COMMAND_TEMPLATE",
    "ladaUrl": "LADA_URL",
    "ladaToken": "LADA_TOKEN",
    "ladaScratch": "LADA_SCRATCH",
    "ladaRestModel": "LADA_RESTORATION_MODEL",
    "ladaDetModel": "LADA_DETECTION_MODEL",
    "ladaDevice": "LADA_DEVICE",
    "ladaEncoder": "LADA_ENCODER",
    "ladaUpscaleModel": "LADA_UPSCALE_MODEL",
    "transcodeCodec": "TRANSCODE_CODEC",
    "transcodeHeight": "TRANSCODE_HEIGHT",
    "transcodeQuality": "TRANSCODE_QUALITY",
}
_ENV_BOOL = {
    "postUpscale": "POST_UPSCALE",
    "importResult": "IMPORT_RESULT",
    "ladaFp16": "LADA_FP16",
}


def env_bool(name, default):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def config_from_env():
    cfg = dict(DEFAULTS)
    for key, env in _ENV_MAP.items():
        val = os.environ.get(env)
        if val is not None and val != "":
            cfg[key] = val
    for key, env in _ENV_BOOL.items():
        cfg[key] = env_bool(env, cfg[key])
    raw = os.environ.get("RUNNERS")
    if raw:
        try:
            cfg["runners"] = json.loads(raw)
        except (ValueError, TypeError):
            log.warning("RUNNERS env is not valid JSON — ignoring the registry.")
            cfg["runners"] = []
    return cfg


def stash_from_env():
    """Build a StashInterface from STASH_URL + STASH_API_KEY."""
    from stashapi.stashapp import StashInterface  # local import; only needed here

    url = os.environ.get("STASH_URL")
    if not url:
        raise ValueError("STASH_URL is required (e.g. http://192.168.1.50:9999).")
    parsed = urlparse(url if "://" in url else f"http://{url}")
    conn = {
        "Scheme": parsed.scheme or "http",
        "Host": parsed.hostname or "localhost",
        "Port": parsed.port or (443 if parsed.scheme == "https" else 9999),
        "Logger": log,
    }
    api_key = os.environ.get("STASH_API_KEY")
    if api_key:
        conn["ApiKey"] = api_key
    return StashInterface(conn)


# --------------------------------------------------------------------------- #
# on-demand: decensor one scene -> reviewable preview -> replace / discard
# --------------------------------------------------------------------------- #

PREVIEW_TAG = "Decensored (preview)"   # legacy name; preview tag is op-aware now


def op_label(backend):
    return {"upscale": "Upscaled", "transcode": "Transcoded"}.get(backend, "Decensored")


def op_suffix(backend):
    return {"upscale": "_upscaled", "transcode": "_transcoded"}.get(backend, "_decensored")


def op_name(backend):
    """User-facing operation name. 'lada' is an internal backend id; the actual
    engine (Lada vs Jasna) is chosen per job and stamped separately."""
    return {"upscale": "upscale", "transcode": "transcode"}.get(backend, "decensor")


def op_tag_names(cfg):
    """Tags describing what was done to the scene: Decensored, Upscaled,
    Transcoded, or a combination (lada + postUpscale chain). Applied to the
    original on replace."""
    b = cfg["backend"]
    if b == "upscale":
        return ["Upscaled"]
    if b == "transcode":
        return ["Transcoded"]
    tags = [str(cfg.get("doneTag") or "Decensored")]
    if cfg.get("postUpscale"):
        tags.append("Upscaled")
    return tags


_RE_FRAMES = re.compile(r"(\d+)\s*/\s*(\d+)\s*\[")  # tqdm "27/60 [..]" — avoids stray ratios
_RE_FPS = re.compile(r"([\d.]+)\s*(?:frame/s|frames/s|it/s|fps)", re.IGNORECASE)
_RE_PCT = re.compile(r"(\d{1,3})%")


def _band(progress, lo, hi, stage=None):
    """on_line callback: parse frame/total, fps and % from tool output, map into
    [lo,hi], and report rich stats (stage / frame / total_frames / fps / eta)."""
    def on_line(line):
        if not progress:
            return
        frame = total = fps = eta = None
        m = _RE_FRAMES.search(line)
        if m:
            frame, total = int(m.group(1)), int(m.group(2))
        f = _RE_FPS.search(line)
        if f:
            try:
                fps = float(f.group(1))
            except ValueError:
                fps = None
        if frame is not None and total:
            frac = lo + (hi - lo) * min(1.0, frame / total)
            if fps and fps > 0 and total >= frame:
                eta = int((total - frame) / fps)
        else:
            pm = _RE_PCT.search(line)
            if not pm:
                return
            frac = lo + (hi - lo) * (min(100, int(pm.group(1))) / 100.0)
        progress(frac, None, {"stage": stage, "frame": frame,
                              "total_frames": total, "fps": fps, "eta": eta})
    return on_line


def process_to_review(stash, cfg, scene_id, progress=None, log_cb=None):
    """Decensor a single scene into a reviewable preview scene. Does NOT touch
    the original. Returns an info dict for a later replace/discard.

    log_cb(line), if given, receives every raw subprocess line for the live-log
    buffer (the HTTP layer throttles it)."""
    def p(frac, msg=None, stats=None):
        check_cancel()
        if progress:
            progress(frac, msg, stats)

    validate(cfg)
    reset_cancel()
    p(0.02, "Fetching scene")
    scene = stash.find_scene(int(scene_id), fragment=SCENE_FRAGMENT)
    if not scene:
        raise RuntimeError(f"Scene {scene_id} not found")
    files = scene.get("files") or []
    if not files:
        raise RuntimeError(f"Scene {scene_id} has no file")
    input_path = files[0]["path"]
    if not os.path.isfile(input_path):
        raise RuntimeError(
            f"File not found at {input_path}. The worker's media mount must match "
            "the path Stash uses."
        )

    stage_dirs = []

    def new_dir():
        d = tempfile.mkdtemp(prefix="decensor_")
        stage_dirs.append(d)
        return d

    try:
        backend = cfg["backend"]
        p(0.05, f"Running {op_name(backend)}", {"stage": op_name(backend)})
        produced = BACKENDS[backend](cfg, input_path, new_dir(),
                                     on_line=_band(progress, 0.05, 0.85, op_name(backend)), log_cb=log_cb)

        p(0.88, "Importing preview into Stash", {"stage": "import"})
        os.makedirs(cfg["outputDir"], exist_ok=True)
        stem = re.sub(r"\s+", "_", os.path.splitext(os.path.basename(input_path))[0])
        ext = os.path.splitext(produced)[1] or ".mp4"
        # suffix matches the operation (upscaled / transcoded / decensored)
        dest = unique_path(cfg["outputDir"], f"{stem}{op_suffix(cfg['backend'])}", ext)
        shutil.move(produced, dest)
        _chown_like(dest, input_path)

        scan_and_wait(stash, cfg["outputDir"])
        matches = stash.find_scenes(
            f={"path": {"value": dest, "modifier": "EQUALS"}}, fragment="id"
        )
        review_id = matches[0]["id"] if matches else None
        label = op_label(cfg["backend"])
        if review_id:
            preview_tag = stash.find_tag(f"{label} (preview)", create=True)
            update = {"id": review_id, "tag_ids": [preview_tag["id"]]}
            if scene.get("title"):
                update["title"] = f"{scene['title']} ({label} preview)"
            stash.update_scene(update)
        else:
            log.warning(
                f"Preview file scanned but no scene found for {dest}. "
                "Is OUTPUT_DIR inside a Stash library path?"
            )

        p(1.0, "Preview ready")
        return {
            "orig_scene_id": scene["id"],
            "orig_path": input_path,
            "output_path": dest,
            "review_scene_id": review_id,
            "op_tags": op_tag_names(cfg),   # applied to the original on replace
        }
    finally:
        for d in stage_dirs:
            shutil.rmtree(d, ignore_errors=True)


def replace_original(stash, cfg, info, progress=None):
    """Overwrite the original file with the decensored one (no backup),
    preserving the original scene's metadata/history, and remove the preview."""
    def p(frac, msg=None):
        if progress:
            progress(frac, msg)

    orig_path = info["orig_path"]
    dest = info["output_path"]
    review_id = info.get("review_scene_id")
    if not os.path.isfile(dest):
        raise RuntimeError(f"Decensored file missing at {dest}")
    # Capture the original owner so the replaced file keeps it (worker runs as root;
    # a root-owned file would block Stash from deleting/organizing it afterwards).
    try:
        orig_stat = os.stat(orig_path)
    except OSError:
        orig_stat = None

    p(0.2, "Removing preview entry")
    if review_id:
        try:
            # Delete the DB entry but NOT the file — we are about to move it.
            stash.destroy_scene(int(review_id), delete_file=False)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Could not destroy preview scene {review_id}: {exc}")

    if os.path.splitext(dest)[1].lower() != os.path.splitext(orig_path)[1].lower():
        log.warning(
            "Decensored container differs from the original extension; keeping the "
            f"original path {os.path.basename(orig_path)} so the scene identity is "
            "preserved."
        )

    p(0.4, "Replacing original file")
    shutil.move(dest, orig_path)  # overwrite original bytes in place
    if orig_stat is not None and hasattr(os, "chown"):
        try:
            os.chown(orig_path, orig_stat.st_uid, orig_stat.st_gid)
        except OSError:
            pass

    p(0.6, "Rescanning original")
    scan_and_wait(stash, os.path.dirname(orig_path))
    # Clear the now-moved preview file from the output library.
    scan_and_wait(stash, cfg["outputDir"])

    # Tag the original with what was done to it (Decensored / Upscaled / both)
    # so dashboards + Stash tag views can filter accordingly.
    try:
        tag_names = info.get("op_tags") or [cfg.get("doneTag", "Decensored")]
        cur = stash.find_scene(int(info["orig_scene_id"]), fragment="id tags { id }")
        if cur:
            ids = {t["id"] for t in cur.get("tags", [])}
            for name in tag_names:
                tag = stash.find_tag(name, create=True)
                if tag:
                    ids.add(tag["id"])
            stash.update_scene({"id": info["orig_scene_id"], "tag_ids": list(ids)})
    except Exception as exc:  # noqa: BLE001 - tagging is best-effort
        log.warning(f"Could not tag original: {exc}")

    p(1.0, "Replaced")
    return info["orig_scene_id"]


def discard_review(stash, cfg, info):
    """Delete the preview scene and its file."""
    review_id = info.get("review_scene_id")
    if review_id:
        try:
            stash.destroy_scene(int(review_id), delete_file=True)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Could not destroy preview scene {review_id}: {exc}")
    dest = info.get("output_path")
    if dest and os.path.isfile(dest):
        try:
            os.remove(dest)
        except OSError:
            pass
