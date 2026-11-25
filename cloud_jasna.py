"""Cloud Jasna: decensor a scene on a rented GPU (opt-in, paid, always confirmed).

Built on the hireling package (https://github.com/nphil/hireling): its Cloud.rent() owns
the safety net (always destroy, spend caps, ledger, reaper). This module adds only the
Jasna specifics, following upstream's cloud guide
(https://github.com/Kruk2/jasna/blob/main/docs/en/cloud.md):

  rent RTX-class GPU (driver >= 580, NVIDIA_DRIVER_CAPABILITIES=all) -> apt + unpack the
  Linux build in /root -> hevc_nvenc check (else next offer, max 3) -> activate with the
  licence (via stdin, never argv) -> upload -> run jasna (LTX download prompt answered)
  -> download the result -> destroy. The result then goes through the normal
  review/replace flow like any other decensor.

Transcode runs on the same rentals without Jasna or its licence: a static ffmpeg build with
NVENC is installed at boot (see ffmpeg_asset) and winrunner/transcode_cli.py runs it with the
same request fields and progress lines as the home runners.

Which cards can do which operation is decided in cloud_caps.py (one place).

Exactly three Jasna presets exist. Never offered: Normal/BasicVSR++, plain LTX, --ltx-fast.
"""

import dataclasses
import json
import math
import os
import re
import shlex
import threading
import time

import cloud_caps
import hireling
from hireling import CloudError, redact, register_secret

# -- the one Cloud of this process ---------------------------------------------------- #

LABEL_PREFIX = "stashify-"           # rented machines are named stashify-<8 hex>; only those are ever reaped
SECRET_FIELDS = {"jasna_license": "JASNA_LICENSE_CODE"}      # write-only, next to the provider keys
# The settings file and the environment keep the names they always had (VAST_*).
ENV_NAMES = {
    "max_price": "VAST_MAX_PRICE", "job_cap": "VAST_JOB_CAP", "month_cap": "VAST_MONTH_CAP",
    "max_bw_cost": "VAST_MAX_BW_COST", "max_job_hours": "VAST_MAX_JOB_HOURS",
    "datacenter_only": "VAST_DATACENTER_ONLY", "gpus": "VAST_GPUS",
    "min_vram_gb": "VAST_MIN_VRAM_GB", "min_reliability": "VAST_MIN_RELIABILITY",
    "disk_gb": "VAST_DISK_GB"}


def make_cloud(base_dir=None, env=None, **kw):
    """A hireling Cloud with Stashify's names: state in CLOUD_DIR (default /config)."""
    env = os.environ if env is None else env
    base = base_dir or env.get("CLOUD_DIR") or "/config"
    settings = hireling.Settings(os.path.join(base, "cloud.json"), env, secrets=SECRET_FIELDS,
                                 env_names=ENV_NAMES)
    return hireling.Cloud(settings, base, label_prefix=LABEL_PREFIX,
                          ssh_dir=env.get("VAST_SSH_DIR") or None, **kw)


_default = None
_default_lock = threading.Lock()


def cloud():
    """The process-wide Cloud (made on first use)."""
    global _default
    with _default_lock:
        if _default is None:
            _default = make_cloud()
        return _default


def set_cloud(instance):
    """Replace the process-wide instance (tests inject one built on fakes)."""
    global _default
    with _default_lock:
        _default = instance

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

# Which cards can do which operation (GPU family, VRAM, driver) is cloud_caps.py's business.
IMAGE = "ubuntu:24.04"
RELEASE_API = "https://api.github.com/repos/Kruk2/jasna/releases/latest"
RELEASE_URL_PREFIX = "https://github.com/Kruk2/jasna/releases/download/"
ASSET_RE = re.compile(r"^jasna-linux-nvidia-[A-Za-z0-9._-]*part\d+$")
RELEASE_GB_FALLBACK = 4.7          # v0.12.4: 3 parts, 4.63 GB (only used for the estimate)
RELEASE_CACHE_S = 3600
# Transcode needs an ffmpeg with NVENC. The static GPL build from the FFmpeg-Builds project has it
# (and libx265) in one file that loads the driver's encoder library at run time, so the plain
# ubuntu image is enough. Their release keeps only the current stable branches, so the asset is
# looked up (FFMPEG_API), never hard-coded.
FFMPEG_API = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/latest"
FFMPEG_URL_PREFIX = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
FFMPEG_ASSET_RE = re.compile(r"^ffmpeg-n(\d+)\.(\d+)-latest-linux64-gpl-\d+\.\d+\.tar\.xz$")
FFMPEG_MASTER_ASSET = "ffmpeg-master-latest-linux64-gpl.tar.xz"
FFMPEG_GB_FALLBACK = 0.16          # about 154 MB (only used for the estimate)
FFMPEG_DIR = "/root/ffmpeg"
TRANSCODE_CLI_REMOTE = "/root/transcode_cli.py"

# Minutes of GPU time per minute of 1080p video, Standard model, mosaics throughout
# (upstream docs/en/models.md "How long it takes").
BASE_MIN_PER_MIN = {"RTX 5090": 1.0, "RTX 4090": 1.5, "RTX 5080": 2.0, "RTX 3090": 3.0}
# Any other card is placed between those by its raw compute: the 3090 (35.6 TFLOPS) is the anchor,
# time scales with the 0.8 power of the compute ratio (the 4090 has 2.3x the compute and is 2x faster).
REF_TFLOPS, REF_MIN_PER_MIN, TFLOPS_EXPONENT = 35.6, 3.0, 0.8
UNKNOWN_MIN_PER_MIN = 4.0            # compute not reported: assume a bit slower than a 3090
MIN_PER_MIN_RANGE = (1.0, 8.0)
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
# Transcode rules of thumb. ESTIMATES, not measurements on a rented machine: ffmpeg decodes on the
# CPU and NVENC encodes, which on a typical rented host runs about 8x real time for 1080p video
# (roughly 250 fps for 30 fps material), and about 1/pixels of that for taller video. NVENC speed is
# set by the chip generation, not the price of the card, so it is not scaled per GPU.
NVENC_X_REALTIME_1080P = 8.0
TRANSCODE_INSTALL_MIN = 2.0   # apt + unpacking a 150 MB ffmpeg
ENCODER_CHECK_MIN = 0.5       # the one-second test encode
TRANSCODE_RESULT_SHARE = 0.45  # HEVC at quality 24 is about this share of a typical source's size
TRANSCODE_DEFAULT_QUALITY = 24     # the home runners' default (winrunner/transcode_cli.py)

# The kinds of paid run. "decensor+upscale" runs Jasna's own upscaler after the mosaic
# restoration; "upscale" only upscales (it asks for no restoration beyond Jasna's default);
# "transcode" re-encodes with ffmpeg (no Jasna, no licence).
OPS = cloud_caps.OPS
TRANSCODE_HEIGHTS = (144, 4320)                             # accepted output heights, inclusive
TRANSCODE_QUALITIES = (1, 51)                               # CQ / CRF, as the home runners take it
UPSCALE_MODELS = ("span", "realplksr", "rtx-super-res")     # Jasna's others need more than a rented RTX box has
UPSCALE_HEIGHTS = (1080, 1440, 2160)                        # the output heights Jasna accepts
DEFAULT_UPSCALE, DEFAULT_UPSCALE_HEIGHT = "span", 2160
DEFAULT_PRESET = "detail"     # what the market browser costs a scene with when none was named
# Upscale-step GPU minutes per minute of video on an RTX 5090 at a 1080p output. NOT measured on a
# rented Linux machine yet: generous guesses, listed as an assumption on every estimate that uses them.
UPSCALE_MIN_PER_MIN = {"span": 0.35, "realplksr": 0.9, "rtx-super-res": 0.5}
PASS_THROUGH_SHARE = 0.25     # an upscale-only run still scans every frame: this share of a restoration pass
APT_GB = 0.2                  # what apt downloads to install Jasna (or ffmpeg's few packages)


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


_ffmpeg_cache = {"at": 0.0, "data": None}


def ffmpeg_asset(http_get=None, now=None):
    """{'tag', 'name', 'url', 'size', 'gb'} of the static ffmpeg build a transcode installs on
    the box. Resolved here, before anything is rented. The release holds a few stable branches
    (ffmpeg-n<major>.<minor>-...) next to the moving master build: the OLDEST stable branch is
    taken, because its NVENC headers ask the least of the host's driver; master is the last resort.
    Cached for an hour like the Jasna release."""
    now = time.time() if now is None else now
    with _release_lock:
        if _ffmpeg_cache["data"] and now - _ffmpeg_cache["at"] < RELEASE_CACHE_S:
            return _ffmpeg_cache["data"]
    try:
        data = (http_get or _github_get)(FFMPEG_API)
        found = []
        for a in data.get("assets") or []:
            name, url = str(a.get("name") or ""), str(a.get("browser_download_url") or "")
            if not (url.startswith(FFMPEG_URL_PREFIX) and url.endswith(name)):
                continue
            m = FFMPEG_ASSET_RE.match(name)
            rank = (0, int(m.group(1)), int(m.group(2))) if m else (1, 0, 0) if name == FFMPEG_MASTER_ASSET else None
            if rank:
                found.append((rank, {"name": name, "url": url, "size": int(a.get("size") or 0)}))
        if not found:
            raise CloudJobFailed("the ffmpeg download for transcoding is not on GitHub right now")
        best = min(found, key=lambda f: f[0])[1]
        out = dict(best, tag=str(data.get("tag_name") or ""), gb=best["size"] / 1e9 or FFMPEG_GB_FALLBACK)
    except CloudJobFailed:
        raise
    except Exception as exc:  # noqa: BLE001 - any lookup problem means "do not rent"
        raise CloudJobFailed("could not look up the ffmpeg download on GitHub: %s" % redact(exc)) from None
    with _release_lock:
        _ffmpeg_cache.update(at=now, data=out)
    return out


def cached_tool_gb(op):
    """Download size of the software `op` installs, for ESTIMATES (never touches the network)."""
    if op == cloud_caps.TRANSCODE:
        with _release_lock:
            data = _ffmpeg_cache["data"]
        return data["gb"] if data else FFMPEG_GB_FALLBACK
    return cached_release_gb()


# -- scene facts ------------------------------------------------------------------ #

def scene_meta(scene):
    """The few facts the estimate needs, from a Stash scene dict (files[0])."""
    f = ((scene or {}).get("files") or [{}])[0] or {}
    title = (scene or {}).get("title") or os.path.basename(str(f.get("path") or "")) or "Untitled"
    return {"id": (scene or {}).get("id"), "title": str(title)[:120],
            "duration": float(f.get("duration") or 0), "size": int(f.get("size") or 0),
            "height": int(f.get("height") or 0), "width": int(f.get("width") or 0)}


# -- what to run, and on which machine ------------------------------------------------ #

class PlanError(ValueError):
    """The request asks for something Stashify will not run (an operation, model, height,
    machine or filter it does not know). `code` is the machine-readable reason; str(exc) is
    safe to show a user."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclasses.dataclass(frozen=True)
class Plan:
    """What one paid run does and where: the operation, the upscale settings and how the
    machine is chosen. The default is the plain, automatic Decensor."""
    op: str = "decensor"          # decensor | decensor+upscale | upscale
    upscale: str = ""             # Jasna's upscale model (only for ops that upscale)
    upscale_height: int = 0       # output height in pixels (same)
    offer_id: str = ""            # pin ONE machine: nothing else is rented in its place
    provider: str = ""            # ... the provider that machine is on (ids are per provider)
    gpu: str = ""                 # or: only this GPU model (another machine of it is fine)
    hardware: str = ""            # or: the machine a hardware choice (cheapest, fastest...) means right now
    filters: dict = dataclasses.field(default_factory=dict)   # extra search filters; they only tighten
    transcode_height: int = 0     # transcode only: output height in pixels, 0 = keep the source's
    transcode_quality: int = 0    # transcode only: CQ/CRF, 0 = the default (24)

    @property
    def jasna(self):
        """True when the run needs Jasna and its licence (everything but transcode)."""
        return cloud_caps.uses_jasna(self.op)

    @property
    def restoring(self):
        """True when mosaics are restored (so a quality preset applies)."""
        return self.op in ("decensor", "decensor+upscale")

    @property
    def chosen(self):
        """True when ONE specific machine was picked (by id, or by a hardware choice)."""
        return bool(self.offer_id or self.hardware)

    @property
    def steered(self):
        """True when the owner narrowed the machine choice at all."""
        return bool(self.offer_id or self.hardware or self.gpu or self.filters)

    def as_dict(self):
        """The request fields that make this plan again (plan_from_body(plan.as_dict()) == plan)."""
        return {"op": self.op, "upscale": self.upscale, "upscale_height": self.upscale_height,
                "offer_id": self.offer_id, "provider": self.provider, "gpu": self.gpu,
                "hardware": self.hardware, "cloud_filters": dict(self.filters),
                "transcode_height": self.transcode_height, "transcode_quality": self.transcode_quality}


_GPU_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.+/-]{0,39}$")
_OFFER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_MODEL_NAMES = {"span": "SPAN", "realplksr": "RealPLKSR", "rtx-super-res": "RTX Super Resolution"}
_BAD_OP = "Cloud GPUs run Decensor, Decensor + upscale, Upscale or Transcode."


def truthy(value):
    """A request flag: JSON true / 1, or the usual query-string spellings."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return value == 1


def plan_from_body(body, backend=None):
    """Validate the "what and where" part of a request (the POST /api/decensor body, or the
    quote's query string) into a Plan. `backend` ('decensor' | 'lada' | 'upscale' | 'transcode')
    names the operation when the request does not (`op`, else `post_upscale` on a Decensor).
    Raises PlanError."""
    body = body or {}
    op = str(body.get("op") or "").strip().replace(" ", "+")     # a query string turns '+' into ' '
    if not op:
        if backend in ("upscale", "transcode"):
            op = backend
        elif backend in (None, "", "decensor", "lada"):
            op = "decensor+upscale" if truthy(body.get("post_upscale")) else "decensor"
        else:
            raise PlanError("bad_op", _BAD_OP)
    if op not in OPS:
        raise PlanError("bad_op", _BAD_OP)
    upscale, height = "", 0
    if op in ("upscale", "decensor+upscale"):
        upscale = str(body.get("upscale") or DEFAULT_UPSCALE).strip().lower()
        if upscale not in UPSCALE_MODELS:
            raise PlanError("bad_upscale", "Cloud upscaling uses one of: %s." % ", ".join(UPSCALE_MODELS))
        raw = body.get("upscale_height")
        try:
            height = DEFAULT_UPSCALE_HEIGHT if raw in (None, "") else int(float(raw))
        except (TypeError, ValueError):
            height = 0
        if height not in UPSCALE_HEIGHTS:
            raise PlanError("bad_upscale_height", "The upscale height must be one of: %s."
                            % ", ".join(str(h) for h in UPSCALE_HEIGHTS))
    offer_id = body.get("offer_id")
    offer_id = "" if offer_id in (None, "") else str(offer_id).strip()
    if offer_id and not _OFFER_ID.match(offer_id):
        raise PlanError("bad_offer", "That is not a machine id.")
    provider = str(body.get("provider") or "").strip().lower()
    if provider and provider not in hireling.REGISTRY:
        raise PlanError("bad_provider", "That GPU provider is not known.")
    gpu = str(body.get("gpu") or "").strip()
    if gpu and not _GPU_NAME.match(gpu):
        raise PlanError("bad_gpu", "That is not a GPU name.")
    hardware = str(body.get("hardware") or "").strip()
    if hardware and hardware not in hireling.PRESET_IDS:
        raise PlanError("bad_hardware", "The hardware choice must be one of: %s." % ", ".join(hireling.PRESET_IDS))
    if hardware and offer_id:
        raise PlanError("bad_hardware", "Pick a machine or a hardware choice, not both.")
    filters = body.get("cloud_filters")
    if isinstance(filters, str):
        try:
            filters = json.loads(filters) if filters.strip() else {}
        except ValueError:
            raise PlanError("bad_filters", "cloud_filters must be a JSON object.") from None
    filters = {} if filters is None else filters
    if not isinstance(filters, dict):
        raise PlanError("bad_filters", "cloud_filters must be an object.")
    try:
        hireling.RentSpec.from_dict(filters, allowed=hireling.FILTER_FIELDS)
    except ValueError as exc:
        raise PlanError("bad_filters", str(exc)) from None
    t_height, t_quality = _transcode_fields(body) if op == cloud_caps.TRANSCODE else (0, 0)
    return Plan(op, upscale, height, offer_id, provider, gpu, hardware, dict(filters), t_height, t_quality)


def _transcode_fields(body):
    """(output height, quality) of a transcode request; 0 for "the default" (keep the height / 24)."""
    out = []
    for key, (lo, hi), label in (("transcode_height", TRANSCODE_HEIGHTS, "height"),
                                 ("transcode_quality", TRANSCODE_QUALITIES, "quality")):
        raw = body.get(key)
        try:
            value = 0 if raw in (None, "") else int(float(raw))
        except (TypeError, ValueError):
            value = -1
        if value != 0 and not lo <= value <= hi:
            raise PlanError("bad_transcode", "The transcode %s must be a number from %d to %d."
                            % (label, lo, hi))
        out.append(value)
    return tuple(out)


def plan_label(plan, preset_key=None):
    """The words the dashboard shows for a run: the quality preset, plus what it upscales to."""
    if plan.op == "transcode":
        return "Transcode" + (" to %dp" % plan.transcode_height if plan.transcode_height else " \u00b7 same size")
    if plan.op == "upscale":
        return "Upscale to %dp \u00b7 %s" % (plan.upscale_height, _MODEL_NAMES[plan.upscale])
    label = PRESETS[preset_key]["label"]
    if plan.op == "decensor+upscale":
        label += " + upscale to %dp" % plan.upscale_height
    return label


# Filters the owner adds can only TIGHTEN what Jasna and the caps require, never loosen it.
_RAISE = ("min_vram_gb", "min_driver", "min_cuda", "min_inet_down", "min_inet_up", "min_reliability",
          "min_open_ports", "min_cpu_cores", "min_ram_gb", "min_disk_gb", "min_duration_hours")
_LOWER = ("max_price", "max_bw_cost")                      # None = no limit
_KEEP = ("num_gpus", "max_gpus", "verified", "rental_type")   # Stashify's own choice; a filter cannot change it


def narrow(base, filters):
    """`base` (what Jasna and the owner's caps require) with the owner's extra search filters
    applied. A filter only ever tightens: minimums take the larger value, price limits the
    smaller, "datacenter only" can be switched on but never off, and the GPU count, the
    verified-host rule and the rental type stay as `base` has them. `base` has no GPU list;
    the filters' own `gpus` / `gpu` / `countries` / `provider` / `offer_id` just restrict."""
    if not filters:
        return base
    spec = hireling.RentSpec.from_dict(filters, base=base, allowed=hireling.FILTER_FIELDS)
    for key in filters:
        want, have = getattr(spec, key), getattr(base, key)
        if key in _RAISE:
            setattr(spec, key, max(want, have))
        elif key in _LOWER:
            if want is None or (have is not None and have <= want):
                setattr(spec, key, have)
        elif key == "max_vram_gb":
            if want and have:
                setattr(spec, key, min(want, have))
            else:
                setattr(spec, key, want or have)
        elif key == "datacenter_only":
            setattr(spec, key, bool(have or want))
        elif key in _KEEP:
            setattr(spec, key, have)
    return spec.validate()


def disk_needed(size_bytes, ratio=1.0, op="decensor"):
    """GB of rental disk: the software and models (upstream's 80 GB for Jasna, 20 GB for ffmpeg),
    plus the source, a working copy and the result (`ratio` times the source's size)."""
    return cloud_caps.need_for(op).base_disk_gb + int(math.ceil((2.0 + ratio) * size_bytes / 1e9))


def requirements(cfg, size_bytes=0, environ=None, ratio=1.0, op="decensor"):
    """What `op` needs from ANY machine (cloud_caps), plus the owner's limits, as data: it marks
    offers compatible or not in the market browser and is the base of every rental. No GPU list:
    every card that can do the job is allowed."""
    env = os.environ if environ is None else environ
    need = cloud_caps.need_for(op)
    return hireling.RentSpec(
        gpu_pattern=need.gpu_pattern, min_vram_gb=need.min_vram_gb, max_price=cfg.max_price,
        disk_gb=max(int(cfg.disk_gb), disk_needed(size_bytes, ratio, op)),
        image=env.get("JASNA_CLOUD_IMAGE") or env.get("VAST_IMAGE") or IMAGE,
        env={"NVIDIA_DRIVER_CAPABILITIES": "all"},
        min_reliability=cfg.min_reliability, datacenter_only=cfg.datacenter_only,
        min_driver=need.min_driver, min_inet_down=need.min_inet_down, max_bw_cost=cfg.max_bw_cost,
        max_hours=cfg.max_job_hours)


def result_ratio(scene, plan):
    """How many times bigger the result file is than the source: more pixels when upscaled, a
    fraction when transcoded (HEVC at the chosen quality, at the chosen height), 1.0 otherwise."""
    if plan.op == cloud_caps.TRANSCODE:
        ratio = TRANSCODE_RESULT_SHARE * 2.0 ** ((TRANSCODE_DEFAULT_QUALITY - (plan.transcode_quality
                                                  or TRANSCODE_DEFAULT_QUALITY)) / 6.0)
        src = scene.get("height") or 0
        if plan.transcode_height and src and plan.transcode_height < src:
            ratio *= (plan.transcode_height / float(src)) ** 2
        return min(max(ratio, 0.05), 1.5)
    if not plan.upscale:
        return 1.0
    return max(1.0, (plan.upscale_height / float(scene.get("height") or 1080)) ** 2)


def plan_spec(plan, cfg, size_bytes=0, environ=None, ratio=1.0):
    """The RentSpec a plan searches and rents with. Nothing steered: every machine that can do
    the operation, cheapest first (datacentre hosts ahead of community ones). Otherwise the
    requirements narrowed to what the owner asked for."""
    base = requirements(cfg, size_bytes, environ, ratio, plan.op)
    if not plan.steered:
        return base
    chosen = dict(plan.filters)
    for key, value in (("gpu", plan.gpu), ("offer_id", plan.offer_id), ("provider", plan.provider)):
        if value:
            chosen[key] = value
    return narrow(base, chosen)


# -- estimate / quote ------------------------------------------------------------- #

def _transfer_min(gb, mbps):
    return gb * 8000.0 / max(mbps, 1.0) / 60.0


@dataclasses.dataclass(frozen=True)
class JobCost(hireling.JobEstimate):
    """hireling's JobEstimate (hours, the compute / storage / bandwidth breakdown, the
    assumptions) plus Stashify's minutes and a price rounded UP to the cent: the number a
    user confirms. The market browser and the quote use this one class, so they agree."""
    setup_min: float = 0.0
    transfer_min: float = 0.0
    processing_min: float = 0.0
    disk_gb: int = 0
    up_min: float = 0.0          # of transfer_min: the video going up from home
    down_min: float = 0.0        # ... and the result coming back

    @property
    def est_usd(self):
        return math.ceil(round(self.cost.total * 100.0, 6)) / 100.0

    def as_dict(self):
        out = super().as_dict()
        out.update(est_usd=self.est_usd, setup_min=round(self.setup_min, 1),
                   transfer_min=round(self.transfer_min, 1), processing_min=round(self.processing_min, 1),
                   total_min=out["minutes"], disk_gb=self.disk_gb,
                   up_min=round(self.up_min, 1), down_min=round(self.down_min, 1))
        return out


def gpu_min_per_min(offer):
    """(minutes of GPU time per minute of 1080p video, True when upstream published it for this
    card). Other cards are placed by their raw compute, or assumed slowish when it is unknown."""
    known = BASE_MIN_PER_MIN.get(offer.gpu)
    if known:
        return known, True
    tflops = (offer.total_flops or 0) / max(offer.num_gpus or 1, 1)
    if tflops <= 0:
        return UNKNOWN_MIN_PER_MIN, False
    est = REF_MIN_PER_MIN * (REF_TFLOPS / tflops) ** TFLOPS_EXPONENT
    return min(max(est, MIN_PER_MIN_RANGE[0]), MIN_PER_MIN_RANGE[1]), False


def transcode_x_realtime(scene):
    """How many times faster than real time the encode runs: a rule of thumb (see
    NVENC_X_REALTIME_1080P), slower in proportion to the pixels per frame."""
    height = scene.get("height") or 1080
    width = scene.get("width") or height * 16.0 / 9.0
    return NVENC_X_REALTIME_1080P / max(0.25, (width * height) / (1920.0 * 1080.0))


def _estimate_transcode(scene, offer, tool_gb, plan, disk_gb):
    """What a transcode costs on one offer. The WHOLE file goes up from home and the (smaller)
    result comes back, so on a quick encode the transfers are most of the time and the bill's
    traffic line: both ways are counted, and so is the disk for as long as the box is rented."""
    minutes = (scene.get("duration") or 0) / 60.0
    speed = transcode_x_realtime(scene)
    processing = minutes / speed * SAFETY
    size_gb = (scene.get("size") or 0) / 1e9
    ratio = result_ratio(scene, plan)
    result_gb = size_gb * ratio
    down = max((offer.inet_down or DEFAULT_DOWN_MBPS) * 0.7, 100.0)
    setup = BOOT_MIN + TRANSCODE_INSTALL_MIN + _transfer_min(tool_gb, min(down, 800.0)) + ENCODER_CHECK_MIN
    up_min, down_min = _transfer_min(size_gb, HOME_UPLINK_MBPS), _transfer_min(result_gb, HOME_UPLINK_MBPS)
    hours = (setup + up_min + down_min + processing) / 60.0
    disk = disk_needed(scene.get("size") or 0, ratio, plan.op) if disk_gb is None else disk_gb
    cost = hireling.estimate_cost(offer, hours, disk_gb=disk, gb_down=size_gb + tool_gb + APT_GB, gb_up=result_gb)
    notes = ["Encoding time is a rule of thumb, not a measurement: about %.0fx real time for 1080p on the "
             "NVENC video encoder (slower for taller video), padded by %d%%. It is much the same on "
             "any card that has one." % (NVENC_X_REALTIME_1080P, round((SAFETY - 1.0) * 100)),
             "The whole video goes up from home (%.1f GB) and the result comes back (about %.1f GB), "
             "assumed at %.0f MB/s each way: %.0f of the estimated %.0f minutes are file transfer."
             % (size_gb, result_gb, HOME_UPLINK_MBPS / 8.0, up_min + down_min, hours * 60.0)]
    return JobCost(hours=hours, cost=cost, assumptions=tuple(notes), setup_min=setup,
                   transfer_min=up_min + down_min, processing_min=processing, disk_gb=disk,
                   up_min=up_min, down_min=down_min)


def estimate_job(scene, preset_key, offer, release_gb=RELEASE_GB_FALLBACK, plan=None, disk_gb=None):
    """What one scene costs on one offer, in time and money (a JobCost). An ESTIMATE:
    upstream's speed table is for 1080p with mosaics throughout; taller video is scaled
    linearly and everything is padded by SAFETY. The runtime cap, not this number, guards the
    money. disk_gb is the rental disk (default: what the plan needs for this scene).
    release_gb is the size of what the machine installs (the Jasna release; ffmpeg for a transcode)."""
    plan = plan or Plan()
    if plan.op == cloud_caps.TRANSCODE:
        return _estimate_transcode(scene, offer, release_gb, plan, disk_gb)
    restoring = plan.restoring
    minutes = (scene.get("duration") or 0) / 60.0
    base, published = gpu_min_per_min(offer)
    res = max(1.0, (scene.get("height") or 1080) / 1080.0)
    if restoring:
        p = PRESETS[preset_key]
        slow = TIER_SLOWDOWN[p["model"]] * (LARGE_CANVAS_SLOWDOWN if p["large_canvas"] else 1.0)
        model_gb = MODEL_DOWNLOAD_GB[p["model"]]
    else:
        slow, model_gb = PASS_THROUGH_SHARE, 0.0
    processing = minutes * base * slow * res * SAFETY
    if plan.upscale:
        processing += (minutes * UPSCALE_MIN_PER_MIN[plan.upscale] * (plan.upscale_height / 1080.0) ** 2
                       * base / BASE_MIN_PER_MIN["RTX 5090"] * SAFETY)
    size_gb = (scene.get("size") or 0) / 1e9
    ratio = result_ratio(scene, plan)
    result_gb = size_gb * ratio
    down = max((offer.inet_down or DEFAULT_DOWN_MBPS) * 0.7, 100.0)
    setup = (BOOT_MIN + INSTALL_FIXED_MIN + _transfer_min(release_gb, min(down, 800.0)) + ACTIVATE_MIN
             + _transfer_min(model_gb, down) + FIRST_RUN_PREP_MIN)
    up_min, down_min = _transfer_min(size_gb, HOME_UPLINK_MBPS), _transfer_min(result_gb, HOME_UPLINK_MBPS)
    total = setup + up_min + down_min + processing
    hours = total / 60.0
    disk = disk_needed(scene.get("size") or 0, ratio, plan.op) if disk_gb is None else disk_gb
    cost = hireling.estimate_cost(offer, hours, disk_gb=disk, gb_down=size_gb + release_gb + model_gb + APT_GB,
                                  gb_up=result_gb)
    notes = ["GPU time follows Jasna's published speeds for %s, padded by %d%%."
             % (offer.gpu or "this GPU", round((SAFETY - 1.0) * 100))]
    if not published:
        notes[0] = ("%s is not in Jasna's published speed table: its speed is worked out from its raw "
                    "compute, padded by %d%%." % (offer.gpu or "This GPU", round((SAFETY - 1.0) * 100)))
    if not restoring:
        notes.append("No mosaic restoration is wanted, but Jasna still scans every frame "
                     "(counted as %d%% of a full restoration pass)." % round(PASS_THROUGH_SHARE * 100))
    if plan.upscale:
        notes.append("Upscaling time is a rough guess: it has not been measured on rented machines yet.")
    notes.append("Uploading from home is assumed to run at %.0f MB/s." % (HOME_UPLINK_MBPS / 8.0))
    return JobCost(hours=hours, cost=cost, assumptions=tuple(notes), setup_min=setup,
                   transfer_min=up_min + down_min, processing_min=processing, disk_gb=disk,
                   up_min=up_min, down_min=down_min)


def estimate(scene, preset_key, offer, release_gb=RELEASE_GB_FALLBACK, plan=None, disk_gb=None):
    """estimate_job as a plain dict (setup_min, transfer_min, processing_min, total_min, est_usd,
    hours, minutes, breakdown, assumptions, disk_gb)."""
    return estimate_job(scene, preset_key, offer, release_gb, plan, disk_gb).as_dict()


def make_estimator(cfg, scene, preset_key, plan=None, release_gb=None):
    """callable(offer) -> JobCost for hireling's market browser: the same numbers a quote
    shows, so a machine's listed price is what the user is later asked to confirm."""
    plan = plan or Plan()
    rel = cached_tool_gb(plan.op) if release_gb is None else release_gb
    disk = requirements(cfg, scene.get("size") or 0, ratio=result_ratio(scene, plan), op=plan.op).disk_gb
    return lambda offer: estimate_job(scene, preset_key, offer, rel, plan, disk)


@dataclasses.dataclass(frozen=True)
class JobCostSum(JobCost):
    """Several scenes on one machine: each scene is its own rental, so the times and the bill
    add up, and the price is the sum of the per-scene prices (each rounded up to the cent,
    which is what each scene's own confirmation is checked against)."""
    usd_sum: float = 0.0

    @property
    def est_usd(self):
        return round(self.usd_sum, 2)


def sum_costs(costs):
    """One JobCostSum from the JobCost of each scene."""
    costs = list(costs)
    cb = [c.cost for c in costs]
    total = hireling.cost.CostBreakdown(
        hours=sum(b.hours for b in cb), compute=sum(b.compute for b in cb), storage=sum(b.storage for b in cb),
        bw_down=sum(b.bw_down for b in cb), bw_up=sum(b.bw_up for b in cb),
        gb_down=sum(b.gb_down for b in cb), gb_up=sum(b.gb_up for b in cb),
        notes=tuple(dict.fromkeys(n for b in cb for n in b.notes)))
    return JobCostSum(hours=sum(c.hours for c in costs), cost=total,
                      assumptions=tuple(dict.fromkeys(a for c in costs for a in c.assumptions)),
                      setup_min=sum(c.setup_min for c in costs), transfer_min=sum(c.transfer_min for c in costs),
                      processing_min=sum(c.processing_min for c in costs),
                      up_min=sum(c.up_min for c in costs), down_min=sum(c.down_min for c in costs),
                      disk_gb=max(c.disk_gb for c in costs), usd_sum=sum(c.est_usd for c in costs))


def make_estimator_many(cfg, scenes, preset_key, plan=None, release_gb=None):
    """make_estimator for several scenes run one after another on the same machine."""
    one = [make_estimator(cfg, s, preset_key, plan, release_gb) for s in scenes]
    return lambda offer: sum_costs(e(offer) for e in one)


_BRIEF = ("provider", "id", "gpu", "num_gpus", "vram_gb", "price_per_hour", "reliability", "datacenter",
          "host_type", "location", "country", "inet_down", "inet_up", "storage_per_gb_hr",
          "bw_up_per_gb", "bw_down_per_gb")


def offer_brief(offer):
    """The machine a quote is for, in a few fields."""
    row = offer.as_dict()
    return {key: row[key] for key in _BRIEF}


def pick_hardware(cl, spec, estimator, hardware, fresh=False):
    """The machine a hardware choice (cheapest / fastest / best_value / most_reliable) means
    right now, among every machine `spec` accepts, or None."""
    listed = cl.offers(spec, estimator=estimator, limit=1, cached=not fresh)
    if listed.errors and not listed.pool:
        raise CloudError(next(iter(listed.errors.values())))
    return next((p.offer for p in hireling.resolve_presets(listed.pool, listed.estimates)
                 if p.id == hardware), None)


def quote_with_spec(cl, scene, preset_key, plan=None, queued_usd=0.0, release_gb=None, fresh=False):
    """Everything the dashboard shows before a paid run, and the server's refusal rules (the
    SAME function decides both, so the UI can't drift from the enforcement). Returns
    (quote, spec): the RentSpec a job for this quote rents with, None when refused.

    queued_usd: estimates of cloud jobs already queued but not started.
    fresh: look the market up again instead of reusing a result up to two minutes old
    (admission and the run itself do, so what is rented is what was priced)."""
    plan = plan or Plan()
    restoring = plan.restoring
    if restoring and preset_key not in PRESETS:
        raise ValueError("unknown cloud preset %r" % (preset_key,))
    cfg = cl.config()
    committed = cl.month_spend_committed()
    out = {"op": plan.op, "preset": preset_key if restoring else None,
           "label": plan_label(plan, preset_key), "estimate": True,
           "scene_id": scene.get("id"), "title": scene.get("title"),
           "job_cap": cfg.job_cap, "month_cap": cfg.month_cap, "max_price": cfg.max_price,
           "max_job_hours": cfg.max_job_hours, "month_to_date": round(committed, 2),
           "month_remaining": round(max(0.0, cfg.month_cap - committed - queued_usd), 2),
           "est_usd": None, "refusal": None, "plan": plan.as_dict(),
           "machine": "chosen" if plan.chosen else "auto"}

    def refuse(code, message):
        out["refusal"] = {"code": code, "message": message}
        out["ok"] = False
        return out, None

    if not cl.enabled_providers():
        return refuse("no_key", "Add your Vast.ai API key in Cloud settings first.")
    if plan.jasna and not cl.settings.get("jasna_license"):
        return refuse("no_licence", "Add your Jasna licence code in Cloud settings first.")
    if not (scene.get("duration") or 0) > 0:
        return refuse("no_duration", "Stash does not know this video's length, so the cost "
                                     "cannot be estimated.")
    height = scene.get("height") or 0
    if plan.upscale and 0 < height >= plan.upscale_height:
        return refuse("upscale_not_needed", "This video is already %dp, so upscaling it to %dp would "
                      "add nothing. Pick a larger size." % (height, plan.upscale_height))
    spec = plan_spec(plan, cfg, scene.get("size") or 0, ratio=result_ratio(scene, plan))
    rel = cached_tool_gb(plan.op) if release_gb is None else release_gb
    try:
        if plan.hardware and not plan.offer_id:
            offer = pick_hardware(cl, spec, make_estimator(cfg, scene, preset_key, plan, rel),
                                  plan.hardware, fresh)
            if offer is not None:
                spec = dataclasses.replace(spec, offer_id=str(offer.id), provider=offer.provider)
        else:
            offer = cl.best_offer(spec, cached=not fresh)
    except hireling.QuotaError:
        return refuse("search_quota", "Vast's search quota for today is used up. Try tomorrow.")
    except CloudError as exc:
        return refuse("offers_unavailable", "Could not look up GPU prices: %s" % redact(exc))
    if offer is None:
        if plan.offer_id:
            return refuse("offer_gone", "The machine you picked is no longer available at this price. "
                                        "Nothing was rented; pick another.")
        if plan.hardware:
            return refuse("no_offer", "No machine fits that hardware choice right now.")
        if plan.steered:
            return refuse("no_offer", "No machine matches your choices under $%.2f/hour right now." % cfg.max_price)
        return refuse("no_offer", "No suitable GPU is available under $%.2f/hour right now." % cfg.max_price)
    est = estimate_job(scene, preset_key, offer, rel, plan, spec.disk_gb)
    out.update(est.as_dict(), gpu=offer.gpu, price_per_hour=round(offer.price, 3), ok=True,
               offer=offer_brief(offer))
    if est.est_usd > cfg.job_cap:
        return refuse("over_job_cap", "Estimated $%.2f is more than your per-job cap of $%.2f."
                      % (est.est_usd, cfg.job_cap))
    if est.hours > cfg.max_job_hours:
        return refuse("over_job_hours", "Estimated at about %.1f hours, which is longer than the %.1f hour "
                      "limit on one rental. Nothing was rented." % (est.hours, cfg.max_job_hours))
    if committed + queued_usd + est.est_usd > cfg.month_cap:
        return refuse("over_month_cap", "Estimated $%.2f would take this month past your $%.2f cap "
                      "($%.2f already spent or queued)." % (est.est_usd, cfg.month_cap,
                                                              committed + queued_usd))
    credit = provider_credit(cl, offer.provider)
    if credit is not None:
        out["credit"] = round(credit, 2)
        if queued_usd + est.est_usd > credit:
            return refuse("over_credit", "This needs about $%.2f but you only have $%.2f of %s credit. "
                                         "Add credit or pick a cheaper run." % (est.est_usd, credit, offer.provider.capitalize()))
    return out, spec


def provider_credit(cl, name):
    """The provider's prepaid balance (cached a minute by hireling), or None when it cannot be read."""
    prov = getattr(cl, "_providers", {}).get(name)
    return cl._credit(prov)[0] if prov is not None else None


def build_quote(cl, scene, preset_key, queued_usd=0.0, release_gb=None, plan=None):
    """quote_with_spec without the spec: what the dashboard shows before a paid run."""
    return quote_with_spec(cl, scene, preset_key, plan, queued_usd, release_gb)[0]


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


ENCODER_RE = re.compile(r"STASHIFY_ENCODER=(hevc_nvenc|h264_nvenc|libx265|none)\b")
TRANSCODE_ENCODERS = ("hevc_nvenc", "h264_nvenc", "libx265")      # best first: GPU HEVC, GPU H.264, CPU HEVC


def transcode_install_script(url):
    """ffmpeg for a transcode: a static build (it carries NVENC and libx265 in one file), plus
    python3 for winrunner/transcode_cli.py and rsync for the transfers."""
    return "\n".join([
        "set -euo pipefail",
        "export DEBIAN_FRONTEND=noninteractive",
        "echo '[stashify] step: system packages'",
        "apt-get update -qq",
        "apt-get install -y -qq --no-install-recommends ca-certificates wget xz-utils python3-minimal rsync",
        "cd /root",
        "echo '[stashify] step: downloading ffmpeg'",
        "wget -nv -c --tries=5 --retry-connrefused --waitretry=5 -O ffmpeg.tar.xz %s" % shlex.quote(url),
        "echo '[stashify] step: unpacking ffmpeg'",
        "mkdir -p %s" % FFMPEG_DIR,
        "tar -xJf ffmpeg.tar.xz -C %s --strip-components=1" % FFMPEG_DIR,
        "rm -f ffmpeg.tar.xz",
        "test -x %s/bin/ffmpeg" % FFMPEG_DIR,
        "test -x %s/bin/ffprobe" % FFMPEG_DIR,
        "echo '[stashify] step: installed'",
    ]) + "\n"


def transcode_encoder_script():
    """Try the encoders in order (GPU HEVC, GPU H.264, CPU HEVC) with a one-second test encode and
    print STASHIFY_ENCODER=<the first that works>, or =none."""
    return ("for enc in %s; do\n"
            "  if %s/bin/ffmpeg -hide_banner -loglevel error -f lavfi -i testsrc2=duration=1:size=1280x720 "
            "-c:v $enc -f null - ; then echo \"STASHIFY_ENCODER=$enc\"; exit 0; fi\n"
            "done\n"
            "echo STASHIFY_ENCODER=none\n" % (" ".join(TRANSCODE_ENCODERS), FFMPEG_DIR))


def transcode_encoder(lines):
    """The encoder the check chose (from its output lines), or None when none worked."""
    for line in reversed(list(lines or ())):
        m = ENCODER_RE.search(str(line))
        if m:
            return None if m.group(1) == "none" else m.group(1)
    return None


def transcode_command(remote_in, encoder, plan):
    """The remote command of a transcode: winrunner/transcode_cli.py with the request's fields, its
    result moved to the one place the download looks (REMOTE_OUT)."""
    args = ["python3", TRANSCODE_CLI_REMOTE, "--input", remote_in, "--output-dir", "/root/out",
            "--ffmpeg", FFMPEG_DIR + "/bin/ffmpeg", "--ffprobe", FFMPEG_DIR + "/bin/ffprobe",
            "--encoder", encoder, "--quality", str(plan.transcode_quality or TRANSCODE_DEFAULT_QUALITY),
            "--container", "mp4"]
    if plan.transcode_height:
        args += ["--height", str(plan.transcode_height)]
    stem = os.path.splitext(os.path.basename(remote_in))[0]
    return "%s && mv -f /root/out/%s.transcoded.mp4 %s" % (" ".join(shlex.quote(a) for a in args), stem, REMOTE_OUT)


def transcode_cli_path():
    """Where this process finds the transcode script it uploads to the box (the same file the home
    runners use)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "transcode_cli.py"), os.path.join(here, "winrunner", "transcode_cli.py")):
        if os.path.isfile(cand):
            return cand
    raise CloudJobFailed("Stashify's transcode script is missing, so nothing was rented")


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


def job_args(preset_key, plan=None):
    """Jasna's options for a plan, after --input/--output. Upscale-only runs name no
    restoration preset and skip the long BasicVSR++ compile (nothing is restored beyond Jasna's
    default); the upscale step is Jasna's own, run after any restoration."""
    plan = plan or Plan()
    args = ["--no-compile-basicvsrpp"] if plan.op == "upscale" else list(preset_args(preset_key))
    if plan.upscale:
        args += ["--upscale", plan.upscale, "--upscale-height", str(plan.upscale_height)]
    return args


def output_suffix(plan):
    """The file-name suffix core.op_suffix gives what this operation produces."""
    return {"upscale": "_upscaled", "transcode": "_transcoded"}.get(plan.op, "_decensored")


def run_command(remote_in, preset_key, plan=None):
    args = " ".join(shlex.quote(a) for a in job_args(preset_key, plan))
    return ("export XDG_CONFIG_HOME=%s; cd %s && ./jasna --input %s --output %s %s --log-level info"
            % (CONFIG_HOME, JASNA_DIR_GLOB, shlex.quote(remote_in), REMOTE_OUT, args))


# Answer the LTX download prompt (upstream: type y); anything else asks -> no.
PROMPTS = [(re.compile(r"Download \([^)]*\)\? \[y/N\]:\s*$"), b"y\n"),
           (re.compile(r"\[y/N\]:\s*$"), b"n\n")]


# -- progress parsing ------------------------------------------------------------------ #

_RE_PCT_BAR = re.compile(r"(\d{1,3})\s*%\s*\|")
_RE_FRAMES = re.compile(r"\(([\d,]+)f\)")
_RE_REMAIN = re.compile(r"Remaining:\s*((?:\d+:)?\d{1,2}:\d{2})")
_RE_SPEED = re.compile(r"([\d.]+)\s*(?:fps|f/s)", re.IGNORECASE)     # jasna: 3.0fps; transcode_cli: 3.2f/s
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

    def __init__(self, op="decensor"):
        self.pct = 0.0
        self._dl_bucket = -1
        self._message = "Transcoding in the cloud" if op == cloud_caps.TRANSCODE else "Decensoring in the cloud"

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
               "message": self._message}
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


# The dashboard's five steps of a rental (boot, upload, run, download, cleanup), from the
# message each stage reports; a message that starts none of these changes nothing.
_PHASES = (("boot", ("Looking up", "Renting", "Installing", "Checking the video encoder", "Activating")),
           ("upload", ("Uploading",)),
           ("run", ("Starting Jasna", "Starting the transcode", "Decensoring", "Transcoding",
                    "Downloading the LTX")),
           ("download", ("Downloading the result",)))


def cloud_phase_of(message):
    for phase, starts in _PHASES:
        if str(message or "").startswith(starts):
            return phase
    return None


def run_decensor(cfg, input_path, result_dir, *, on_line=None, log_cb=None, status_cb=None,
                 cl=None, stamp=None, check_cancel=None, release_fn=None, ffmpeg_fn=None):
    """Run one scene on a cloud GPU (Decensor, Decensor + upscale, Upscale or Transcode). Returns
    the produced file inside result_dir.

    cfg["cloud"] = {preset, scene, plan, est_usd, confirm_usd, job_id}. check_cancel(): raises
    (or returns true) when the user cancels. stamp(**fields): live job fields
    (runner/spent). Raises CloudRefused / CloudJobFailed / hireling errors; every one
    of them has already destroyed the rented box."""
    cl = cl or cloud()
    job = cfg["cloud"]
    preset_key, scene = job.get("preset"), job["scene"]
    plan = plan_from_body(job.get("plan"))
    say = log_cb or (lambda _line: None)

    def phase(stage, progress, message, indeterminate=False, **extra):
        step = cloud_phase_of(message)
        if step and stamp:
            stamp(cloud_phase=step)
        if status_cb:
            status_cb(dict({"stage": stage, "progress": progress, "message": message,
                            "indeterminate": indeterminate}, **extra))

    # 1. Re-check everything: the state may have changed since the user confirmed. The market
    # is looked up again, so what is rented is what is priced here.
    quote, spec = quote_with_spec(cl, scene, preset_key, plan, fresh=True)
    if quote.get("refusal"):
        raise CloudRefused(quote["refusal"]["code"], quote["refusal"]["message"])
    if price_changed(float(job.get("confirm_usd") or 0), quote["est_usd"]):
        raise CloudRefused("price_changed", "The price moved from the $%.2f you confirmed to about $%.2f. "
                           "Nothing was rented; submit again to confirm the new price."
                           % (float(job.get("confirm_usd") or 0), quote["est_usd"]))
    licence = cl.settings.get("jasna_license") if plan.jasna else None
    if licence:
        register_secret(licence)

    # 2. Resolve the download links before spending anything.
    if plan.jasna:
        phase("copy", 0.0, "Looking up the latest Jasna release", True)
        rel = (release_fn or release_assets)()
        urls = [a["url"] for a in rel["assets"]]
    else:
        phase("copy", 0.0, "Looking up the ffmpeg download", True)
        urls = [(ffmpeg_fn or ffmpeg_asset)()["url"]]

    progress = RunProgress(plan.op)
    chosen = {}                       # what verify() found on the machine that was kept
    last = {"up": -1, "down": -1, "t": -1e9, "msg": None}
    rented = []

    def tick(box):
        if stamp:
            stamp(runner="%s \u00b7 %s" % (cl.provider(box.provider).display_name, box.gpu),
                  engine="jasna" if plan.jasna else "ffmpeg",
                  cloud=True, cloud_usd=round(box.spent_total(), 2), cloud_cap=round(box.cap, 2),
                  cloud_gpu=box.gpu, cloud_offer_id=str(box.offer.id), cloud_provider=box.provider,
                  provider=cl.provider(box.provider).display_name,
                  cloud_tier="dc" if box.offer.datacenter else "comm")

    def on_offer(n, offer):
        rented.append(offer.id)
        phase("copy", 0.02 * n, "Renting %s at $%.2f/hour (machine %d of up to %d)"
              % (offer.gpu, offer.price, n, hireling.cloud.MAX_ATTEMPTS), True)
        say("[cloud] renting %s at $%.2f/hour" % (offer.gpu, offer.price))

    def log_remote(line):
        say("[cloud] " + line)

    def verify(box):
        """Machine acceptance: install the software, then the encoder check. Failing either means
        THIS machine is bad (no internet, no NVENC) -> the next offer is tried."""
        if not plan.jasna:
            phase("copy", 0.30, "Installing ffmpeg on the rented %s" % box.gpu, True)
            res = box.run_script(transcode_install_script(urls[0]), on_line=log_remote, timeout=1200)
            if res.code != 0:
                raise hireling.VerifyFailed("installing ffmpeg failed: %s" % " | ".join(res.tail[-3:]))
            phase("copy", 0.85, "Checking the video encoder", True)
            res = box.run_script(transcode_encoder_script(), on_line=log_remote, timeout=180)
            chosen["encoder"] = transcode_encoder(res.tail)
            if not chosen["encoder"]:
                raise hireling.VerifyFailed("this machine has no working video encoder")
            return
        phase("copy", 0.30, "Installing Jasna on the rented %s" % box.gpu, True)
        res = box.run_script(install_script(urls), on_line=log_remote, timeout=2400)
        if res.code != 0:
            raise hireling.VerifyFailed("installing Jasna failed: %s" % " | ".join(res.tail[-3:]))
        phase("copy", 0.85, "Checking the video encoder", True)
        res = box.run_script(encoder_check_script(), on_line=log_remote, timeout=180)
        text = " ".join(res.tail)
        if OK_MARK not in text:
            raise hireling.VerifyFailed("this machine's NVENC video encoder does not work")

    meta = {"job_id": job.get("job_id"), "scene_id": scene.get("id"), "scene": scene.get("title"),
            "preset": preset_key, "op": plan.op}
    try:
        with cl.rent(spec, cancel=check_cancel, verify=verify, meta=meta, on_tick=tick,
                     on_offer=on_offer) as box:
            tick(box)
            say("[cloud] machine ready: %s, $%.2f/hour, spend cap $%.2f" % (box.gpu, box.dph, box.cap))
            if chosen.get("encoder") == "libx265":
                say("[cloud] no GPU video encoder works on this machine: encoding on its CPU (slow)")
            _on_box(box, cfg, job, input_path, result_dir, urls, licence, progress, phase, say, last, plan,
                    chosen.get("encoder"))
            produced = _fetch_result(box, input_path, result_dir, phase, say, last, output_suffix(plan))
            if stamp:
                stamp(cloud_phase="cleanup")
            say("[cloud] finished: spent $%.2f (estimate was $%.2f)" % (box.spent_total(), quote["est_usd"]))
            return produced
    except hireling.NoOffers:
        if plan.offer_id:
            raise CloudRefused("offer_gone", "The machine you picked is no longer available. Nothing was "
                               "rented; submit again to pick another.") from None
        raise CloudRefused("no_offer", "No suitable GPU is available under $%.2f/hour right now. "
                           "Nothing was rented." % cl.config().max_price) from None
    except (hireling.SpendCapReached, hireling.LifetimeExceeded) as exc:
        raise CloudJobFailed("Stopped: %s. %s" % (exc, _after_stop(cl, job, bool(rented)))) from None


def _on_box(box, cfg, job, input_path, result_dir, urls, licence, progress, phase, say, last, plan, encoder=None):
    """Activate (Jasna only), upload, run. Any failure raises (and rent() destroys the box)."""
    def log_remote(line):
        say("[cloud] " + line)

    if plan.jasna:
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
    if not plan.jasna:
        box.put(transcode_cli_path(), TRANSCODE_CLI_REMOTE)
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

    if plan.jasna:
        phase("decensor", 0.0, "Starting Jasna (the first run prepares the GPU%s)"
              % (" and may download the model" if plan.restoring else ""), True)
        res = box.run(run_command(remote_in, job.get("preset"), plan), tty=True, prompts=PROMPTS,
                      grace=hireling.ssh.INT_GRACE_S, on_line=run_line)
        if res.code != 0:
            box.outcome = ("error", "jasna exited %d" % res.code)
            raise CloudJobFailed("Jasna failed on the rented machine (exit %d): %s"
                                 % (res.code, _jasna_failure(res.tail)))
        return
    phase("decensor", 0.0, "Starting the transcode (%s)" % encoder, True)
    res = box.run(transcode_command(remote_in, encoder, plan), tty=True,
                  grace=hireling.ssh.INT_GRACE_S, on_line=run_line)
    if res.code != 0:
        box.outcome = ("error", "transcode exited %d" % res.code)
        raise CloudJobFailed("The transcode failed on the rented machine (exit %d): %s"
                             % (res.code, " ".join(res.tail)[-300:] or "no output"))


def _fetch_result(box, input_path, result_dir, phase, say, last, suffix="_decensored"):
    """Download the finished file. Only reached after a clean Jasna exit, so a partial
    result is never kept."""
    res = box.run("test -s %s && stat -c %%s %s" % (REMOTE_OUT, REMOTE_OUT), timeout=60)
    sizes = [t for t in res.tail if t.strip().isdigit()]
    if res.code != 0 or not sizes:
        raise CloudJobFailed("The cloud job finished but produced no output file")
    remote_size = int(sizes[-1])
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(os.path.basename(input_path))[0])[:80] or "scene"
    local = os.path.join(result_dir, "%s%s.mp4" % (stem, suffix))

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
