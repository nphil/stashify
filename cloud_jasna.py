"""Cloud Jasna: decensor a scene on a rented Vast.ai GPU (opt-in, paid, always confirmed).

Built on cloud_vast.Cloud.rent(), which owns the safety net (always destroy, spend caps,
ledger, reaper). This module adds only the Jasna specifics, following upstream's
cloud guide (https://github.com/Kruk2/jasna/blob/main/docs/en/cloud.md):

  rent RTX-class GPU (driver >= 580, NVIDIA_DRIVER_CAPABILITIES=all) -> apt + unpack the
  Linux build in /root -> hevc_nvenc check (else next offer, max 3) -> activate with the
  licence (via stdin, never argv) -> upload -> run jasna (LTX download prompt answered)
  -> download the result -> destroy. The result then goes through the normal
  review/replace flow like any other decensor.

Exactly three presets exist. Never offered: Normal/BasicVSR++, plain LTX, --ltx-fast.
"""

import math
import os
import re
import shlex
import threading
import time

import cloud_ssh
import cloud_vast
from cloud_vast import CloudError, redact, register_secret

# -- the three presets (the ONLY ones) ------------------------------------------ #

PRESETS = {
    "close-ups": {"label": "Sharper close-ups", "model": "ltx", "large_canvas": True},
    "detail": {"label": "High detail", "model": "ltx-8step", "large_canvas": False},
    "detail-close-ups": {"label": "High detail + sharper close-ups", "model": "ltx-8step",
                         "large_canvas": True},
}


def preset_args(key):
    """The jasna options a preset stands for. Never --ltx-fast (RTX 50 only)."""
    p = PRESETS[key]
    return ["--restoration-model-name", p["model"]] + (["--ltx-large-canvas"] if p["large_canvas"] else [])


# -- hardware / pricing knowledge ------------------------------------------------ #

# RTX-class only: upstream says data-centre cards (A100/H100/B200) do not work. Best first.
GPUS = ("RTX 5090", "RTX 4090", "RTX 5080", "RTX 3090")
MIN_VRAM_GB = 16
MIN_DRIVER = 580
MIN_INET_DOWN_MBPS = 200
IMAGE = "ubuntu:24.04"
RELEASE_API = "https://api.github.com/repos/Kruk2/jasna/releases/latest"
RELEASE_URL_PREFIX = "https://github.com/Kruk2/jasna/releases/download/"
ASSET_RE = re.compile(r"^jasna-linux-nvidia-[A-Za-z0-9._-]*part\d+$")
RELEASE_GB_FALLBACK = 4.7          # v0.12.4: 3 parts, 4.63 GB (only used for the estimate)
RELEASE_CACHE_S = 3600

# Minutes of GPU time per minute of 1080p video, Standard model, mosaics throughout
# (upstream docs/en/models.md "How long it takes").
BASE_MIN_PER_MIN = {"RTX 5090": 1.0, "RTX 4090": 1.5, "RTX 5080": 2.0, "RTX 3090": 3.0}
TIER_SLOWDOWN = {"ltx": 1.0, "ltx-8step": 2.5}          # High is ~2.5x slower than Standard
LARGE_CANVAS_SLOWDOWN = 3.0                             # "a whole video up to ~3x longer"
MODEL_DOWNLOAD_GB = {"ltx": 15.0, "ltx-8step": 28.0}    # Standard 15 GB; High needs ~13 GB more
SAFETY = 1.2                  # estimate leans high: the runtime cap, not this, is the guard
BOOT_MIN = 4.0                # create -> ssh ready, incl. the image pull
INSTALL_FIXED_MIN = 4.0       # apt + unpacking 4.6 GB
ACTIVATE_MIN = 2.0            # "installation verified" can look stuck for up to a minute
FIRST_RUN_PREP_MIN = 4.0      # Jasna prepares for the GPU on the first LTX run
HOME_UPLINK_MBPS = 80         # assumed home-to-cloud speed (10 MB/s); documented guess
DEFAULT_DOWN_MBPS = 300


class CloudRefused(CloudError):
    """The job may not start (no key/licence, over a spend cap, no machine...)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class CloudJobFailed(CloudError):
    """The rental worked but the Jasna job did not (message is user-facing)."""


class ActivationFailed(CloudJobFailed):
    pass


# -- release lookup (on the coordinator, not on the box) ------------------------- #

_release_lock = threading.Lock()
_release_cache = {"at": 0.0, "data": None}


def _github_get(url):
    import requests         # lazy, like the rest of the codebase
    r = requests.get(url, headers={"Accept": "application/vnd.github+json",
                                   "User-Agent": "stashify"}, timeout=20)
    r.raise_for_status()
    return r.json()


def release_assets(http_get=None, now=None):
    """{'tag', 'assets': [{'name','url','size'}...], 'gb'} for the latest Linux NVIDIA
    build. Resolved here so a GitHub problem fails the job BEFORE anything is rented.
    Cached for an hour (GitHub allows 60 anonymous calls/hour)."""
    now = time.time() if now is None else now
    with _release_lock:
        if _release_cache["data"] and now - _release_cache["at"] < RELEASE_CACHE_S:
            return _release_cache["data"]
    try:
        data = (http_get or _github_get)(RELEASE_API)
        assets = []
        for a in data.get("assets") or []:
            name, url = str(a.get("name") or ""), str(a.get("browser_download_url") or "")
            if ASSET_RE.match(name) and url.startswith(RELEASE_URL_PREFIX) and url.endswith(name):
                assets.append({"name": name, "url": url, "size": int(a.get("size") or 0)})
        assets.sort(key=lambda a: a["name"])
        if not assets:
            raise CloudJobFailed("the latest Jasna release has no Linux NVIDIA download")
        nums = [int(re.search(r"part(\d+)$", a["name"]).group(1)) for a in assets]
        if nums != list(range(len(nums))):
            raise CloudJobFailed("the Jasna download parts are not complete: %s" % nums)
        out = {"tag": str(data.get("tag_name") or ""), "assets": assets,
               "gb": sum(a["size"] for a in assets) / 1e9 or RELEASE_GB_FALLBACK}
    except CloudJobFailed:
        raise
    except Exception as exc:  # noqa: BLE001 - any lookup problem means "do not rent"
        raise CloudJobFailed("could not look up the latest Jasna release on GitHub: %s" % redact(exc)) from None
    with _release_lock:
        _release_cache.update(at=now, data=out)
    return out


def cached_release_gb():
    """Download size for ESTIMATES: the last real lookup, else a fallback. Never touches the
    network, so typing in the dashboard cannot burn GitHub's anonymous rate limit or wait on
    it; a job always resolves the real release itself before renting anything."""
    with _release_lock:
        data = _release_cache["data"]
    return data["gb"] if data else RELEASE_GB_FALLBACK


# -- scene facts ------------------------------------------------------------------ #

def scene_meta(scene):
    """The few facts the estimate needs, from a Stash scene dict (files[0])."""
    f = ((scene or {}).get("files") or [{}])[0] or {}
    title = (scene or {}).get("title") or os.path.basename(str(f.get("path") or "")) or "Untitled"
    return {"id": (scene or {}).get("id"), "title": str(title)[:120],
            "duration": float(f.get("duration") or 0), "size": int(f.get("size") or 0),
            "height": int(f.get("height") or 0), "width": int(f.get("width") or 0)}


# -- estimate / quote ------------------------------------------------------------- #

def _transfer_min(gb, mbps):
    return gb * 8000.0 / max(mbps, 1.0) / 60.0


def rent_spec(cfg, size_bytes=0, environ=None):
    """What to rent for Jasna. Disk follows upstream (>= 80 GB) plus room for the video
    and the result."""
    env = os.environ if environ is None else environ
    gb = size_bytes / 1e9
    return cloud_vast.RentSpec(
        gpus=GPUS, min_vram_gb=MIN_VRAM_GB, max_price=cfg.max_price,
        disk_gb=max(int(cfg.disk_gb), 80 + int(math.ceil(3 * gb))),
        image=env.get("JASNA_CLOUD_IMAGE") or env.get("VAST_IMAGE") or IMAGE,
        env={"NVIDIA_DRIVER_CAPABILITIES": "all"},
        min_reliability=cfg.min_reliability, datacenter_only=cfg.datacenter_only,
        min_driver=MIN_DRIVER, min_inet_down=MIN_INET_DOWN_MBPS, max_bw_cost=cfg.max_bw_cost,
        max_hours=cfg.max_job_hours)


def estimate(scene, preset_key, offer, release_gb=RELEASE_GB_FALLBACK):
    """Minutes and dollars for one scene on one offer. An ESTIMATE: upstream's speed table
    is for 1080p; taller video is scaled linearly and everything is padded by SAFETY."""
    p = PRESETS[preset_key]
    base = BASE_MIN_PER_MIN.get(offer.gpu, max(BASE_MIN_PER_MIN.values()))
    slow = TIER_SLOWDOWN[p["model"]] * (LARGE_CANVAS_SLOWDOWN if p["large_canvas"] else 1.0)
    res = max(1.0, (scene.get("height") or 1080) / 1080.0)
    processing = scene["duration"] / 60.0 * base * slow * res * SAFETY
    down = max((offer.inet_down or DEFAULT_DOWN_MBPS) * 0.7, 100.0)
    model = _transfer_min(MODEL_DOWNLOAD_GB[p["model"]], down)
    install = INSTALL_FIXED_MIN + _transfer_min(release_gb, min(down, 800.0))
    transfer = 2 * _transfer_min((scene.get("size") or 0) / 1e9, HOME_UPLINK_MBPS)  # up + result down
    setup = BOOT_MIN + install + ACTIVATE_MIN + model + FIRST_RUN_PREP_MIN
    total = setup + transfer + processing
    return {"setup_min": round(setup, 1), "transfer_min": round(transfer, 1),
            "processing_min": round(processing, 1), "total_min": round(total, 1),
            "est_usd": math.ceil(total / 60.0 * offer.price * 100.0) / 100.0}


def build_quote(cl, scene, preset_key, queued_usd=0.0, release_gb=None):
    """Everything the dashboard shows before a paid run, and the server's refusal rules
    (the SAME function decides both, so the UI can't drift from the enforcement).

    queued_usd: estimates of cloud jobs already queued but not started."""
    if preset_key not in PRESETS:
        raise ValueError("unknown cloud preset %r" % (preset_key,))
    cfg = cl.config()
    committed = cl.month_spend_committed()
    out = {"preset": preset_key, "label": PRESETS[preset_key]["label"], "estimate": True,
           "scene_id": scene.get("id"), "title": scene.get("title"),
           "job_cap": cfg.job_cap, "month_cap": cfg.month_cap, "max_price": cfg.max_price,
           "month_to_date": round(committed, 2),
           "month_remaining": round(max(0.0, cfg.month_cap - committed - queued_usd), 2),
           "est_usd": None, "refusal": None}

    def refuse(code, message):
        out["refusal"] = {"code": code, "message": message}
        out["ok"] = False
        return out

    if not cfg.api_key:
        return refuse("no_key", "Add your Vast.ai API key in Cloud settings first.")
    if not cl.settings.get("jasna_license"):
        return refuse("no_licence", "Add your Jasna licence code in Cloud settings first.")
    if not (scene.get("duration") or 0) > 0:
        return refuse("no_duration", "Stash does not know this video's length, so the cost "
                                     "cannot be estimated.")
    try:
        offer = cl.best_offer(rent_spec(cfg, scene.get("size") or 0))
    except cloud_vast.VastQuotaError:
        return refuse("search_quota", "Vast's search quota for today is used up. Try tomorrow.")
    except CloudError as exc:
        return refuse("offers_unavailable", "Could not look up GPU prices: %s" % redact(exc))
    if offer is None:
        return refuse("no_offer", "No suitable GPU is available under $%.2f/hour right now." % cfg.max_price)
    est = estimate(scene, preset_key, offer, cached_release_gb() if release_gb is None else release_gb)
    out.update(est, gpu=offer.gpu, price_per_hour=round(offer.price, 3), ok=True)
    if est["est_usd"] > cfg.job_cap:
        return refuse("over_job_cap", "Estimated $%.2f is more than your per-job cap of $%.2f."
                      % (est["est_usd"], cfg.job_cap))
    if committed + queued_usd + est["est_usd"] > cfg.month_cap:
        return refuse("over_month_cap", "Estimated $%.2f would take this month past your $%.2f cap "
                      "($%.2f already spent or queued)." % (est["est_usd"], cfg.month_cap,
                                                              committed + queued_usd))
    return out


def price_changed(confirmed_usd, est_usd):
    """True when the fresh estimate is meaningfully above what the user confirmed."""
    return est_usd > confirmed_usd * 1.10 + 0.10


# -- remote scripts ------------------------------------------------------------------ #

JASNA_DIR_GLOB = "/root/jasna-linux-nvidia-*/"
CONFIG_HOME = "/root/jasna-config"
REMOTE_OUT = "/root/out/result.mp4"
OK_MARK, FAIL_MARK = "STASHIFY_ENCODER_OK", "STASHIFY_ENCODER_FAIL"


def install_script(urls):
    """Upstream's install, with ca-certificates explicit (--no-install-recommends would
    otherwise leave wget unable to verify HTTPS)."""
    lines = [
        "set -euo pipefail",
        "export DEBIAN_FRONTEND=noninteractive",
        "echo '[stashify] step: system packages'",
        "apt-get update -qq",
        "apt-get install -y -qq --no-install-recommends ca-certificates curl zstd wget libgl1 "
        "libglib2.0-0 rsync",
        "cd /root",
        "echo '[stashify] step: downloading Jasna'",
    ]
    lines += ["wget -nv -c --tries=5 --retry-connrefused --waitretry=5 %s" % shlex.quote(u) for u in urls]
    lines += [
        "echo '[stashify] step: unpacking Jasna'",
        "cat jasna-linux-nvidia-*.part* | zstd -dc | tar -xpf - --same-owner",
        "rm -f jasna-linux-nvidia-*.part*",
        "test -x %sjasna" % JASNA_DIR_GLOB,
        "echo '[stashify] step: installed'",
    ]
    return "\n".join(lines) + "\n"


def encoder_check_script():
    """Upstream: a machine whose hevc_nvenc fails cannot encode; delete it, rent another."""
    return ("cd %s\n"
            "if ./tools/ffmpeg -hide_banner -loglevel error -f lavfi -i testsrc2=duration=1:size=1280x720 "
            "-c:v hevc_nvenc -f null - ; then echo %s; else echo %s; fi\n"
            % (JASNA_DIR_GLOB, OK_MARK, FAIL_MARK))


def activation_script():
    """The licence code arrives on STDIN (a line), so it is never in a command line,
    a process list or a log. A bare `jasna` with the code set activates and exits."""
    return "\n".join([
        "export XDG_CONFIG_HOME=%s" % CONFIG_HOME,
        'mkdir -p "$XDG_CONFIG_HOME"',
        "IFS= read -r JASNA_LICENSE_CODE",
        "export JASNA_LICENSE_CODE",
        "cd %s" % JASNA_DIR_GLOB,
        "./jasna --online",
    ]) + "\n"


def run_command(remote_in, preset_key):
    args = " ".join(shlex.quote(a) for a in preset_args(preset_key))
    return ("export XDG_CONFIG_HOME=%s; cd %s && ./jasna --input %s --output %s %s --log-level info"
            % (CONFIG_HOME, JASNA_DIR_GLOB, shlex.quote(remote_in), REMOTE_OUT, args))


# Answer the LTX download prompt (upstream: type y); anything else asks -> no.
PROMPTS = [(re.compile(r"Download \([^)]*\)\? \[y/N\]:\s*$"), b"y\n"),
           (re.compile(r"\[y/N\]:\s*$"), b"n\n")]


# -- progress parsing ------------------------------------------------------------------ #

_RE_PCT_BAR = re.compile(r"(\d{1,3})\s*%\s*\|")
_RE_FRAMES = re.compile(r"\(([\d,]+)f\)")
_RE_REMAIN = re.compile(r"Remaining:\s*((?:\d+:)?\d{1,2}:\d{2})")
_RE_SPEED = re.compile(r"([\d.]+)\s*fps", re.IGNORECASE)
_RE_RSYNC_PCT = re.compile(r"\b(\d{1,3})%")


def _clock_to_s(text):
    secs = 0
    for part in text.split(":"):
        secs = secs * 60 + int(part)
    return secs


class RunProgress:
    """Turns jasna's output into dashboard updates. Progress never goes backwards and
    stays below 100% until the process exits. The formats are from upstream's source and
    the Windows wrapper; a line that does not match is simply ignored."""

    def __init__(self):
        self.pct = 0.0
        self._dl_bucket = -1

    def parse(self, line):
        """Returns a dict of runner-style fields, or None for a non-progress line. A "log"
        key carries a short note worth keeping in the job log (the model download, every
        10%: the per-frame lines would flood it)."""
        if "LTX download" in line:
            m = _RE_PCT_BAR.search(line)
            pct = int(m.group(1)) if m else None
            out = {"message": "Downloading the LTX model" + (" (%d%%)" % pct if m else ""),
                   "indeterminate": m is None, "progress": self.pct}
            if pct is not None and pct // 10 != self._dl_bucket:
                self._dl_bucket = pct // 10
                out["log"] = "LTX model download: %d%%" % pct
            return out
        m = _RE_PCT_BAR.search(line.replace(",", ""))
        if not m or "(" not in line:
            return None
        pct = min(99.0, float(m.group(1)))
        self.pct = max(self.pct, pct)
        out = {"progress": self.pct / 100.0, "indeterminate": False,
               "message": "Decensoring in the cloud"}
        f = _RE_FRAMES.search(line)
        if f:
            frame = int(f.group(1).replace(",", ""))
            out["frame"] = frame
            if self.pct >= 5:
                out["total_frames"] = int(round(frame / (self.pct / 100.0)))
        s = _RE_SPEED.search(line)
        if s:
            out["fps"] = float(s.group(1))
        r = _RE_REMAIN.search(line)
        if r:
            out["eta"] = _clock_to_s(r.group(1))
        return out


# -- the job ----------------------------------------------------------------------------- #

def _safe_ext(path):
    ext = os.path.splitext(path)[1].lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{1,5}", ext) else ".mp4"


def _jasna_failure(tail):
    text = " ".join(tail)
    if "three devices" in text:
        return ("Your Jasna licence is in use on three devices right now (a stopped run keeps its slot "
                "for about 15 minutes). Try again in 15 minutes.")
    return text[-300:] or "no output"


def _after_stop(cl, job, rented):
    """Honest tail for a stop message: say 'destroyed' only when nothing is left to retry."""
    if not rented:
        return "Nothing was rented."
    left = any((rec.get("meta") or {}).get("job_id") == job.get("job_id")
               for rec in cl.registry.all().values())
    if left:
        return "Stashify could not confirm the machine is gone yet and keeps retrying (see Cloud settings)."
    return "The machine was destroyed and nothing was kept."


def run_decensor(cfg, input_path, result_dir, *, on_line=None, log_cb=None, status_cb=None,
                 cl=None, stamp=None, check_cancel=None, release_fn=None):
    """Run one scene through cloud Jasna. Returns the produced file inside result_dir.

    cfg["cloud"] = {preset, scene, est_usd, confirm_usd, job_id}. check_cancel(): raises
    (or returns true) when the user cancels. stamp(**fields): live job fields
    (runner/spent). Raises CloudRefused / CloudJobFailed / cloud_vast errors; every one
    of them has already destroyed the rented box."""
    cl = cl or cloud_vast.cloud()
    job = cfg["cloud"]
    preset_key, scene = job["preset"], job["scene"]
    say = log_cb or (lambda _line: None)

    def phase(stage, progress, message, indeterminate=False, **extra):
        if status_cb:
            status_cb(dict({"stage": stage, "progress": progress, "message": message,
                            "indeterminate": indeterminate}, **extra))

    # 1. Re-check everything: the state may have changed since the user confirmed.
    quote = build_quote(cl, scene, preset_key)
    if quote.get("refusal"):
        raise CloudRefused(quote["refusal"]["code"], quote["refusal"]["message"])
    if price_changed(float(job.get("confirm_usd") or 0), quote["est_usd"]):
        raise CloudRefused("price_changed", "The price moved from the $%.2f you confirmed to about $%.2f. "
                           "Nothing was rented; submit again to confirm the new price."
                           % (float(job.get("confirm_usd") or 0), quote["est_usd"]))
    licence = cl.settings.get("jasna_license")
    register_secret(licence)

    # 2. Resolve the download links before spending anything.
    phase("copy", 0.0, "Looking up the latest Jasna release", True)
    rel = (release_fn or release_assets)()
    urls = [a["url"] for a in rel["assets"]]
    spec = rent_spec(cl.config(), scene.get("size") or 0)

    progress = RunProgress()
    last = {"up": -1, "down": -1, "t": -1e9, "msg": None}
    rented = []

    def tick(box):
        if stamp:
            stamp(runner="Vast.ai · %s" % box.gpu, engine="jasna", cloud=True,
                  cloud_usd=round(box.spent_total(), 2), cloud_cap=round(box.cap, 2))

    def on_offer(n, offer):
        rented.append(offer.id)
        phase("copy", 0.02 * n, "Renting %s at $%.2f/hour (machine %d of up to %d)"
              % (offer.gpu, offer.price, n, cloud_vast.MAX_ATTEMPTS), True)
        say("[cloud] renting %s at $%.2f/hour" % (offer.gpu, offer.price))

    def log_remote(line):
        say("[cloud] " + line)

    def verify(box):
        """Machine acceptance: install Jasna, then the encoder check. Failing either means
        THIS machine is bad (no internet, no NVENC) -> the next offer is tried."""
        phase("copy", 0.30, "Installing Jasna on the rented %s" % box.gpu, True)
        res = box.run_script(install_script(urls), on_line=log_remote, timeout=2400)
        if res.code != 0:
            raise cloud_vast.VerifyFailed("installing Jasna failed: %s" % " | ".join(res.tail[-3:]))
        phase("copy", 0.85, "Checking the video encoder", True)
        res = box.run_script(encoder_check_script(), on_line=log_remote, timeout=180)
        text = " ".join(res.tail)
        if OK_MARK not in text:
            raise cloud_vast.VerifyFailed("this machine's NVENC video encoder does not work")

    meta = {"job_id": job.get("job_id"), "scene_id": scene.get("id"), "scene": scene.get("title"),
            "preset": preset_key}
    try:
        with cl.rent(spec, cancel=check_cancel, verify=verify, meta=meta, on_tick=tick,
                     on_offer=on_offer) as box:
            tick(box)
            say("[cloud] machine ready: %s, $%.2f/hour, spend cap $%.2f" % (box.gpu, box.dph, box.cap))
            _on_box(box, cfg, job, input_path, result_dir, urls, licence, progress, phase, say, last)
            produced = _fetch_result(box, input_path, result_dir, phase, say, last)
            say("[cloud] finished: spent $%.2f (estimate was $%.2f)" % (box.spent_total(), quote["est_usd"]))
            return produced
    except cloud_vast.NoOffers:
        raise CloudRefused("no_offer", "No suitable GPU is available under $%.2f/hour right now. "
                           "Nothing was rented." % cl.config().max_price) from None
    except (cloud_vast.SpendCapReached, cloud_vast.LifetimeExceeded) as exc:
        raise CloudJobFailed("Stopped: %s. %s" % (exc, _after_stop(cl, job, bool(rented)))) from None


def _on_box(box, cfg, job, input_path, result_dir, urls, licence, progress, phase, say, last):
    """Activate, upload, run jasna. Any failure raises (and rent() destroys the box)."""
    def log_remote(line):
        say("[cloud] " + line)

    phase("scan", 0.05, "Activating Jasna", True)
    res = box.run_script(activation_script(), stdin_data=(licence + "\n").encode("utf-8"),
                         on_line=log_remote, timeout=600)
    if res.code != 0:
        box.outcome = ("error", "activation failed")
        raise ActivationFailed("Jasna activation failed: %s" % _jasna_failure(res.tail))

    remote_in = "/root/in/source" + _safe_ext(input_path)

    def up_line(line):
        m = _RE_RSYNC_PCT.search(line)
        if m and int(m.group(1)) > last["up"]:
            last["up"] = int(m.group(1))
            phase("scan", 0.15 + 0.85 * last["up"] / 100.0, "Uploading the video (%d%%)" % last["up"])

    phase("scan", 0.15, "Uploading the video", False)
    box.put(input_path, remote_in, on_line=up_line)

    def run_line(line):
        upd = progress.parse(line)
        if upd is None:
            say("[cloud] " + line)
            return
        note = upd.pop("log", None)
        if note:
            say("[cloud] " + note)
        now = box.now()
        if now - last["t"] >= 1.0 or upd["message"] != last["msg"]:      # ~1 update/s, plus any change
            last["t"], last["msg"] = now, upd["message"]
            phase("decensor", upd.pop("progress"), upd.pop("message"), upd.pop("indeterminate"), **upd)

    phase("decensor", 0.0, "Starting Jasna (the first run prepares the GPU and may download the model)", True)
    res = box.run(run_command(remote_in, job["preset"]), tty=True, prompts=PROMPTS,
                  grace=cloud_ssh.INT_GRACE_S, on_line=run_line)
    if res.code != 0:
        box.outcome = ("error", "jasna exited %d" % res.code)
        raise CloudJobFailed("Jasna failed on the rented machine (exit %d): %s"
                             % (res.code, _jasna_failure(res.tail)))


def _fetch_result(box, input_path, result_dir, phase, say, last):
    """Download the finished file. Only reached after a clean Jasna exit, so a partial
    result is never kept."""
    res = box.run("test -s %s && stat -c %%s %s" % (REMOTE_OUT, REMOTE_OUT), timeout=60)
    sizes = [t for t in res.tail if t.strip().isdigit()]
    if res.code != 0 or not sizes:
        raise CloudJobFailed("Jasna finished but produced no output file")
    remote_size = int(sizes[-1])
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(os.path.basename(input_path))[0])[:80] or "scene"
    local = os.path.join(result_dir, "%s_decensored.mp4" % stem)

    def down_line(line):
        m = _RE_RSYNC_PCT.search(line)
        if m and int(m.group(1)) > last["down"]:
            last["down"] = int(m.group(1))
            phase("decensor", 0.99, "Downloading the result (%d%%)" % last["down"])

    phase("decensor", 0.99, "Downloading the result", False)
    try:
        box.get(REMOTE_OUT, local, on_line=down_line)
        if not os.path.isfile(local) or os.path.getsize(local) != remote_size:
            raise CloudJobFailed("the downloaded result is incomplete; nothing was kept")
    except BaseException:
        try:                                # rsync --partial leaves half a file under the final name
            os.unlink(local)
        except OSError:
            pass
        raise
    return local
