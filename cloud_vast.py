"""Vast.ai GPU rental for Stashify: rent a box, run commands on it, ALWAYS destroy it.

Money safety is the point of this module, so every path ends in a destroy:

* `Cloud.rent()` is a context manager. Leaving it (normally, by an error, by a user
  cancel, by the spend cap, by Ctrl-C) destroys the box. A failed destroy keeps the
  rental in the registry so the reaper retries it.
* The reaper (`start_reaper`) sweeps Vast's own instance list every few minutes and
  destroys anything labelled `stashify-*` that this process is not actively using, so
  even a crash / container restart cannot leave a box running.
* Every rental has a hard spend cap (per job AND per month). It is checked on every
  poll of every phase (boot, install, upload, run, download), not only at the end.
* Every rental is written to an append-only ledger (/config/cloud-ledger.jsonl).
* Secrets (API key, licence code, per-instance API key) are never logged or returned;
  everything that leaves this module goes through redact().

State lives under CLOUD_DIR (default /config): cloud.json (dashboard settings),
cloud-instances.json (registry), cloud-ledger.jsonl, cloud-ssh/ (coordinator key).

Vast endpoints used (all verified against https://docs.vast.ai, see README):
  POST   /api/v0/bundles/                 search offers
  PUT    /api/v0/asks/{offer_id}/         create instance
  GET    /api/v0/instances/{id}/          show one instance
  GET    /api/v1/instances/               list instances (paginated)
  DELETE /api/v0/instances/{id}/          destroy instance
  POST   /api/v0/instances/{id}/ssh/      attach ssh key (fallback only)
  GET    /api/v0/users/current            account balance ("Test key")
"""

import base64
import contextlib
import dataclasses
import json
import logging
import math
import os
import re
import shlex
import threading
import time
import uuid
from datetime import datetime, timezone

import cloud_ssh
from cloud_ssh import Endpoint

API_BASE = "https://console.vast.ai"
LABEL_PREFIX = "stashify-"
DEFAULT_IMAGE = "ubuntu:24.04"        # plain image: Vast installs sshd itself in ssh mode
MAX_ATTEMPTS = 3                      # offers tried per rental (create -> boot -> verify)
MAX_CONCURRENT_RENTALS = 2
BOOT_TIMEOUT_S = 600
POLL_S = 5.0
TEARDOWN_RESERVE_S = 90               # the spend cap leaves room for the destroy call
REAP_EVERY_S = 180
QUOTE_CACHE_S = 120
FAILED_STATES = ("exited", "unknown", "offline")   # never reach "running" (Vast docs)
PREFIX = "[cloud] "

log = logging.getLogger("stashify.cloud")


# --------------------------------------------------------------------------- #
# redaction
# --------------------------------------------------------------------------- #

_secrets = set()
_secrets_lock = threading.Lock()
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")


def register_secret(value):
    """Everything registered here is masked by redact(). Too-short strings are
    ignored (they would mangle ordinary text)."""
    v = str(value or "").strip()
    if len(v) >= 6:
        with _secrets_lock:
            _secrets.add(v)


def redact(text):
    s = str(text)
    with _secrets_lock:
        known = sorted(_secrets, key=len, reverse=True)
    for sec in known:
        s = s.replace(sec, "***")
    return _BEARER.sub(r"\1***", s)


class _RedactFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


log.addFilter(_RedactFilter())


# --------------------------------------------------------------------------- #
# errors (messages are always redacted before they are raised)
# --------------------------------------------------------------------------- #

class CloudError(Exception):
    """Base class for everything this module raises on purpose."""


class NotConfigured(CloudError):
    """No Vast API key yet."""


class VastApiError(CloudError):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status, self.code = status, code


class VastAuthError(VastApiError):
    """The API key was rejected."""


class VastQuotaError(VastApiError):
    """Vast's daily search quota is used up; retrying today will not help."""


class NoOffers(CloudError):
    """Nothing on the market matches the filters at an acceptable price."""


class OfferUnavailable(CloudError):
    """Someone else rented the offer first; try the next one."""


class BoxFailed(CloudError):
    """The instance never became a usable machine (try another offer)."""


class BoxUnreachable(BoxFailed):
    pass


class VerifyFailed(BoxFailed):
    """The machine booted but is not what we need (no GPU, wrong driver, no encoder...)."""


class RentalCancelled(CloudError):
    pass


class LifetimeExceeded(CloudError):
    pass


class SpendCapReached(CloudError):
    pass


class TooManyRentals(CloudError):
    pass


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

class _RealClock:
    """Injectable: tests use a fake clock whose sleep() just advances time."""

    def time(self):
        return time.time()

    def monotonic(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _month(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m")


def _parse_iso(text):
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _num(value, default=0.0):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _atomic_write(path, text, mode=0o600):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# settings: the dashboard's Cloud section (cloud.json, mode 0600)
# --------------------------------------------------------------------------- #

SECRET_FIELDS = ("vast_api_key", "jasna_license")
NUMBER_FIELDS = {          # name: (default, largest sane value)
    "max_price": (1.00, 50.0),     # $/hour a rented machine may cost
    "job_cap": (3.00, 1000.0),     # $ one rental may cost before it is stopped
    "month_cap": (10.00, 10000.0),  # $ per calendar month
}
ENV_NAMES = {
    "vast_api_key": "VAST_API_KEY", "jasna_license": "JASNA_LICENSE_CODE",
    "max_price": "VAST_MAX_PRICE", "job_cap": "VAST_JOB_CAP", "month_cap": "VAST_MONTH_CAP",
}


@dataclasses.dataclass
class Config:
    api_key: str = dataclasses.field(default="", repr=False)
    max_price: float = 1.00
    job_cap: float = 3.00
    month_cap: float = 10.00
    gpus: tuple = ("RTX 5090", "RTX 4090", "RTX 3090")
    min_vram_gb: float = 16
    min_reliability: float = 0.98
    datacenter_only: bool = False      # community hosts allowed; data centres still rank first
    max_job_hours: float = 3.0
    disk_gb: int = 100
    max_bw_cost: float = 0.02      # $/GB internet cost ceiling (bandwidth is billed on top)


def _env_bool(env, name, default):
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    """Reads/writes /config/cloud.json. Environment variables only OVERRIDE what the
    file says (handy for a locked-down deployment); the dashboard is the normal way."""

    def __init__(self, path, env=None):
        self.path = path
        self.env = os.environ if env is None else env
        self._lock = threading.Lock()
        self._cache = (None, {})

    # -- file ---------------------------------------------------------------- #

    def _read_file(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return {}
        key = (st.st_mtime_ns, st.st_size)
        with self._lock:
            if self._cache[0] == key:
                return dict(self._cache[1])
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            log.warning("cloud settings file is unreadable; ignoring it")
            data = {}
        with self._lock:
            self._cache = (key, dict(data))
        return data

    def _write_file(self, data):
        _atomic_write(self.path, json.dumps(data, indent=2, sort_keys=True), 0o600)
        with self._lock:
            self._cache = (None, {})

    # -- effective values ---------------------------------------------------- #

    def _env(self, field):
        v = (self.env.get(ENV_NAMES[field]) or "").strip()
        return v or None

    def get(self, field):
        """Effective value: environment override > file > default."""
        env = self._env(field)
        if field in SECRET_FIELDS:
            value = env if env is not None else str(self._read_file().get(field) or "")
            register_secret(value)          # masked by redact() from the first log line on
            return value
        default, _hi = NUMBER_FIELDS[field]
        raw = env if env is not None else self._read_file().get(field)
        v = _num(raw, default)
        return v if v > 0 else default

    def source(self, field):
        if self._env(field) is not None:
            return "env"
        if self._read_file().get(field) not in (None, ""):
            return "file"
        return ""

    def config(self):
        cfg = Config(api_key=self.get("vast_api_key"), max_price=self.get("max_price"),
                     job_cap=self.get("job_cap"), month_cap=self.get("month_cap"))
        env = self.env
        gpus = [g.strip() for g in (env.get("VAST_GPUS") or "").split(",") if g.strip()]
        if gpus:
            cfg.gpus = tuple(gpus)
        cfg.min_vram_gb = _num(env.get("VAST_MIN_VRAM_GB"), cfg.min_vram_gb)
        cfg.min_reliability = _num(env.get("VAST_MIN_RELIABILITY"), cfg.min_reliability)
        # dashboard setting (cloud.json), unless the environment pins it
        cfg.datacenter_only = _env_bool(env, "VAST_DATACENTER_ONLY",
                                        bool(self._read_file().get("datacenter_only", cfg.datacenter_only)))
        cfg.max_job_hours = _num(env.get("VAST_MAX_JOB_HOURS"), cfg.max_job_hours)
        cfg.disk_gb = int(_num(env.get("VAST_DISK_GB"), cfg.disk_gb))
        cfg.max_bw_cost = _num(env.get("VAST_MAX_BW_COST"), cfg.max_bw_cost)
        return cfg

    # -- what the API may show ----------------------------------------------- #

    def _secret_view(self, field):
        value = self.get(field)
        return {"set": bool(value), "last4": value[-4:] if len(value) >= 12 else "",
                "source": self.source(field)}

    def public(self):
        """Never contains a secret value: only whether it is set + its last 4 chars."""
        out = {f: self._secret_view(f) for f in SECRET_FIELDS}
        for f in NUMBER_FIELDS:
            out[f] = self.get(f)
        out["datacenter_only"] = self.config().datacenter_only
        out["env_overrides"] = sorted(f for f in ENV_NAMES if self._env(f) is not None)
        return out

    # -- writing ------------------------------------------------------------- #

    @staticmethod
    def _clean_secret(field, value):
        v = value.strip()
        if not 8 <= len(v) <= 512 or re.search(r"[\s\x00-\x1f\x7f]", v):
            raise ValueError("%s looks wrong (8-512 characters, no spaces)" % field)
        return v

    @staticmethod
    def _clean_number(field, value, hi):
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError("%s must be a number" % field) from None
        if not math.isfinite(v) or v <= 0 or v > hi:
            raise ValueError("%s must be more than 0 and at most %g" % (field, hi))
        return round(v, 2)

    def update(self, body):
        """Apply a dashboard save. Secret fields are write-only: absent/blank keeps the
        stored value, a string replaces it, JSON null clears it."""
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object")
        data = dict(self._read_file())
        for field in SECRET_FIELDS:
            if field not in body:
                continue
            v = body[field]
            if v is None:
                data.pop(field, None)
            elif isinstance(v, str) and not v.strip():
                continue
            elif isinstance(v, str):
                data[field] = self._clean_secret(field, v)
                register_secret(data[field])
            else:
                raise ValueError("%s must be text" % field)
        for field, (_default, hi) in NUMBER_FIELDS.items():
            if field in body and body[field] not in (None, ""):
                data[field] = self._clean_number(field, body[field], hi)
        if isinstance(body.get("datacenter_only"), bool):
            data["datacenter_only"] = body["datacenter_only"]
        self._write_file(data)
        return self.public()


# --------------------------------------------------------------------------- #
# registry: every rental we started, so a crash can't strand a box
# --------------------------------------------------------------------------- #

class Registry:
    """cloud-instances.json: {"rentals": {label: {created_at, max_hours, meta, inst}}}.
    `inst` is the live instance of that rental ({id, created_at, gpu, dph, offer_id}) or
    null before create / after a confirmed destroy."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("rentals"), dict):
                return data
        except FileNotFoundError:
            return {"rentals": {}}
        except (OSError, ValueError):
            pass
        try:                                   # corrupt: keep it for a human, start clean
            os.replace(self.path, "%s.corrupt-%d" % (self.path, int(time.time())))
        except OSError:
            pass
        return {"rentals": {}}

    def _save(self, data):
        _atomic_write(self.path, json.dumps(data, indent=1, sort_keys=True), 0o600)

    def begin(self, label, created_at, max_hours, meta):
        with self._lock:
            data = self._load()
            data["rentals"][label] = {"created_at": created_at, "max_hours": max_hours,
                                      "meta": meta or {}, "inst": None}
            self._save(data)

    def set_inst(self, label, inst):
        with self._lock:
            data = self._load()
            rec = data["rentals"].get(label)
            if rec is None:
                return
            rec["inst"] = inst
            self._save(data)

    def pop_inst(self, label, instance_id):
        """Atomically take the live-instance record (so only one of the rental, the
        reaper and a sweep writes its ledger line). Returns it, or None if gone."""
        with self._lock:
            data = self._load()
            rec = data["rentals"].get(label)
            if not rec or not rec.get("inst") or rec["inst"].get("id") != instance_id:
                return None
            inst, rec["inst"] = rec["inst"], None
            self._save(data)
            return inst

    def remove(self, label):
        with self._lock:
            data = self._load()
            if data["rentals"].pop(label, None) is not None:
                self._save(data)

    def all(self):
        with self._lock:
            return self._load()["rentals"]


# --------------------------------------------------------------------------- #
# ledger: append-only record of every rental
# --------------------------------------------------------------------------- #

class Ledger:
    """cloud-ledger.jsonl, one JSON object per line. Month-to-date spend (UTC month of
    the END time) is what the monthly cap is checked against."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def append(self, entry):
        line = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
        with self._lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)

    def entries(self):
        out = []
        try:
            with open(self.path, encoding="utf-8") as fh:
                for raw in fh:
                    try:
                        e = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(e, dict):
                        out.append(e)
        except OSError:
            pass
        return out

    def month_to_date(self, now):
        month = _month(now)
        return round(sum(_num(e.get("usd")) for e in self.entries()
                         if _month(_parse_iso(e.get("ended")) or 0) == month), 4)

    def last(self, n=10):
        return list(reversed(self.entries()[-n:]))


# --------------------------------------------------------------------------- #
# Vast REST client
# --------------------------------------------------------------------------- #

def _default_http(method, url, *, headers, params, json, timeout, allow_redirects):
    import requests          # lazy, like core.py/server.py: tests inject their own http
    return requests.request(method, url, headers=headers, params=params, json=json,
                            timeout=timeout, allow_redirects=allow_redirects)


def _error_fields(resp):
    """(code, message) from a Vast error body, tolerant of any shape."""
    try:
        data = resp.json()
    except ValueError:
        return None, (resp.text or "")[:200]
    if isinstance(data, dict):
        code = data.get("error") or data.get("code")
        msg = data.get("msg") or data.get("message") or data.get("detail") or ""
        return (str(code) if code else None), str(msg)[:300]
    return None, str(data)[:200]


class VastApi:
    """Thin client. Every path ends in "/" (Vast answers a slash-less one with a
    redirect that does nothing for PUT/POST/DELETE), non-GET calls never follow
    redirects, 429 honours Retry-After, only idempotent calls are retried after 5xx or
    a network error, and a create is NEVER retried (an unseen success would rent two
    machines)."""

    def __init__(self, key_fn, http=None, sleep=time.sleep, base=API_BASE):
        self._key_fn = key_fn
        self._http = http or _default_http
        self._sleep = sleep
        self._base = base

    def _request(self, method, path, *, params=None, body=None, timeout=30, idempotent=True,
                 tries=5):
        key = self._key_fn()
        if not key:
            raise NotConfigured("No Vast.ai API key is set (Cloud settings).")
        register_secret(key)
        headers = {"Authorization": "Bearer " + key, "Accept": "application/json",
                   "Content-Type": "application/json"}
        url = self._base + path
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._http(method, url, headers=headers, params=params, json=body,
                                  timeout=timeout, allow_redirects=(method == "GET"))
            except OSError as exc:           # requests' errors subclass IOError
                if idempotent and attempt < tries:
                    self._sleep(min(2 ** attempt, 20))
                    continue
                raise VastApiError("%s %s: network error: %s" % (method, path, redact(exc))) from None
            code = resp.status_code
            err_code, err_msg = _error_fields(resp) if code >= 400 else (None, "")
            low = ("%s %s" % (err_code or "", err_msg)).lower()
            if code == 429:
                if "quota" in low:
                    raise VastQuotaError("Vast search quota used up for today", code, err_code)
                if attempt < tries:
                    try:
                        wait = min(float(resp.headers.get("Retry-After")), 60.0)
                    except (TypeError, ValueError):
                        wait = min(3.0 * attempt, 15.0)
                    self._sleep(wait)
                    continue
                raise VastApiError("%s %s: rate limited" % (method, path), code, err_code)
            if code in (401, 403) or "invalid user key" in low or "invalid_user_key" in low:
                raise VastAuthError("Vast rejected the API key (HTTP %s)" % code, code, err_code)
            if 300 <= code < 400:
                raise VastApiError("%s %s: unexpected redirect" % (method, path), code)
            if code >= 500:
                if idempotent and attempt < tries:
                    self._sleep(min(2 ** attempt, 20))
                    continue
                raise VastApiError("%s %s: HTTP %d %s" % (method, path, code, redact(err_msg)),
                                   code, err_code)
            if code >= 400:
                raise VastApiError("%s %s: HTTP %d %s %s" % (method, path, code, err_code or "",
                                                              redact(err_msg)), code, err_code)
            try:
                data = resp.json()
            except ValueError:
                data = {}
            return data if isinstance(data, dict) else {"_": data}

    # -- endpoints ----------------------------------------------------------- #

    def search_offers(self, body):
        data = self._request("POST", "/api/v0/bundles/", body=body, timeout=45)
        return data.get("offers") or []

    def create_instance(self, offer_id, payload):
        data = self._request("PUT", "/api/v0/asks/%d/" % int(offer_id), body=payload,
                             idempotent=False, tries=3, timeout=60)
        key = data.get("instance_api_key")
        if key:
            register_secret(key)
        return data

    def show_instance(self, instance_id):
        """The instance dict, or None when Vast no longer knows it."""
        try:
            data = self._request("GET", "/api/v0/instances/%d/" % int(instance_id),
                                 params={"owner": "me"})
        except VastApiError as exc:
            if exc.status == 404:
                return None
            raise
        inst = data.get("instances")
        return inst if isinstance(inst, dict) else None

    def list_instances(self):
        rows, token = [], None
        for _page in range(40):          # hard stop: 1000 instances is already absurd
            params = {"select_filters": "{}", "limit": "25",
                      "order_by": json.dumps([{"col": "id", "dir": "asc"}])}
            if token:
                params["after_token"] = token
            data = self._request("GET", "/api/v1/instances/", params=params)
            rows.extend(r for r in (data.get("instances") or []) if isinstance(r, dict))
            token = data.get("next_token")
            if not token:
                return rows
        raise VastApiError("instance list never ended (40 pages)")

    def destroy_instance(self, instance_id):
        """True once Vast says it is gone (404 / no_such_instance counts as gone)."""
        try:
            self._request("DELETE", "/api/v0/instances/%d/" % int(instance_id),
                          body=None, timeout=30)
        except VastApiError as exc:
            if exc.status == 404 or "no_such_instance" in (exc.code or ""):
                return True
            raise
        return True

    def attach_ssh_key(self, instance_id, public_key):
        self._request("POST", "/api/v0/instances/%d/ssh/" % int(instance_id),
                      body={"ssh_key": public_key}, idempotent=False, tries=3)

    def balance(self):
        """What is left to spend. Vast keeps prepaid money in `credit`; its `balance`
        field read 0 on a freshly topped-up account (checked 2026-11-24), so credit
        is the number to show."""
        data = self._request("GET", "/api/v0/users/current")
        return _num(data.get("credit", data.get("balance")), None)


# --------------------------------------------------------------------------- #
# offers
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class Offer:
    id: int
    gpu: str
    price: float           # $/hour including the storage allocation
    vram_gb: float
    reliability: float
    driver: int            # NVIDIA driver major (0 = not reported)
    cuda: float
    inet_down: float       # Mbps
    datacenter: bool


@dataclasses.dataclass
class RentSpec:
    """What machine to look for and how long to wait for it."""
    gpus: tuple = ()                   # preference order, best first; () = any GPU
    min_vram_gb: float = 0
    max_price: float = 1.00            # $/h ceiling (search filter + post-create guard)
    disk_gb: int = 100
    image: str = DEFAULT_IMAGE
    env: dict = dataclasses.field(default_factory=dict)
    min_reliability: float = 0.98
    datacenter_only: bool = False      # False still RANKS datacenter machines first
    min_driver: int = 0                # NVIDIA driver major, e.g. 580
    min_inet_down: float = 0           # Mbps
    max_bw_cost: float = 0.02          # $/GB
    boot_timeout: int = BOOT_TIMEOUT_S
    max_hours: float = 12.0            # hard lifetime of the rental


def _driver_major(text):
    m = re.match(r"\s*(\d+)", str(text or ""))
    return int(m.group(1)) if m else 0


def _gpu_names(gpus):
    """Both spellings: Vast shows 'RTX 4090' but its CLI queries 'RTX_4090'."""
    out = []
    for g in gpus:
        for v in (g, g.replace(" ", "_")):
            if v not in out:
                out.append(v)
    return out


def _search_body(spec):
    body = {
        "verified": {"eq": True}, "rentable": {"eq": True}, "rented": {"eq": False},
        "type": "on-demand", "num_gpus": {"eq": 1},
        "gpu_ram": {"gte": int(spec.min_vram_gb * 1000)},       # MB, not GB
        "reliability": {"gte": spec.min_reliability},
        "direct_port_count": {"gte": 1},
        "disk_space": {"gte": spec.disk_gb},
        "dph_total": {"lte": spec.max_price},
        "inet_up_cost": {"lte": spec.max_bw_cost}, "inet_down_cost": {"lte": spec.max_bw_cost},
        "allocated_storage": spec.disk_gb,
        "order": [["dph_total", "asc"]], "limit": 64,
    }
    if spec.gpus:
        body["gpu_name"] = {"in": _gpu_names(spec.gpus)}
    if spec.datacenter_only:
        body["datacenter"] = {"eq": True}
    if spec.min_driver:
        body["driver_version"] = {"gte": spec.min_driver * 1000000}   # "580.0.0" -> 580000000
    if spec.min_inet_down:
        body["inet_down"] = {"gte": spec.min_inet_down}
    return body


def _parse_offer(raw):
    try:
        oid = int(raw["id"])
        price = float(raw["dph_total"])
    except (KeyError, TypeError, ValueError):
        return None                      # no id / no price: never rent blind
    if not math.isfinite(price) or price <= 0:
        return None
    return Offer(
        id=oid, gpu=str(raw.get("gpu_name") or "").replace("_", " ").strip(), price=price,
        vram_gb=_num(raw.get("gpu_ram"), 0.0) / 1000.0,
        reliability=_num(raw.get("reliability2", raw.get("reliability")), 1.0),
        driver=_driver_major(raw.get("driver_version")), cuda=_num(raw.get("cuda_max_good"), 0.0),
        inet_down=_num(raw.get("inet_down"), 0.0),
        # Vast marks data-centre machines with hosting_type 1; its offers carry no
        # "datacenter" field (that name only exists as a search filter).
        datacenter=(_num(raw.get("hosting_type"), 0) == 1 or bool(raw.get("datacenter"))))


def _acceptable(o, spec):
    """Re-check the filters client-side: the server-side ones are best effort, and a
    field Vast leaves out of an offer must not disqualify it (except price)."""
    if o.price > spec.max_price:
        return False
    if spec.gpus and o.gpu.lower() not in {g.lower() for g in spec.gpus}:
        return False
    if o.vram_gb and o.vram_gb < spec.min_vram_gb * 0.95:
        return False
    if o.reliability < spec.min_reliability:
        return False
    if spec.min_driver and o.driver and o.driver < spec.min_driver:
        return False
    if spec.datacenter_only and not o.datacenter:
        return False
    return True


def _rank(offers, spec):
    gpus = [g.lower() for g in spec.gpus]
    order = lambda o: gpus.index(o.gpu.lower()) if o.gpu.lower() in gpus else len(gpus)
    return sorted(offers, key=lambda o: (order(o), 0 if o.datacenter else 1, o.price))


def _endpoints(info):
    """Direct (public ip + mapped 22/tcp) first, then Vast's ssh proxy."""
    eps = []
    ip = str(info.get("public_ipaddr") or "").strip()
    try:
        hp = ((info.get("ports") or {}).get("22/tcp") or [{}])[0].get("HostPort")
        if ip and hp:
            eps.append(Endpoint(ip, int(hp)))
    except (TypeError, ValueError, IndexError, AttributeError):
        pass
    host, port = str(info.get("ssh_host") or "").strip(), info.get("ssh_port")
    try:
        if host and port:
            ep = Endpoint(host, int(port))
            if ep not in eps:
                eps.append(ep)
    except (TypeError, ValueError):
        pass
    return eps


# --------------------------------------------------------------------------- #
# one rental
# --------------------------------------------------------------------------- #

class _Rental:
    """Budget + cancel state shared by every attempt (offer) of one rent() call."""

    def __init__(self, label, limit, cancel, max_hours, meta, on_tick):
        self.label, self.limit, self.cancel = label, limit, cancel
        self.max_hours, self.meta, self.on_tick = max_hours, meta, on_tick
        self.spent = 0.0              # cost of attempts already destroyed
        self.box = None               # attempt currently alive

    def accrued(self, now_mono):
        return self.spent + (self.box.accrued(now_mono) if self.box else 0.0)


class Box:
    """A rented, reachable machine. Created only by Cloud.rent()."""

    def __init__(self, cloud, rental, instance_id, offer, started_wall, started_mono, dph):
        self._cloud, self._r = cloud, rental
        self.id, self.offer = instance_id, offer
        self.gpu, self.dph = offer.gpu, dph
        self.started_wall, self.started_mono = started_wall, started_mono
        self.endpoint = None
        self.outcome = None               # (status, reason) a caller may set before leaving rent()
        self._destroyed = False
        self._last_tick = 0.0

    # -- cost / safety ------------------------------------------------------- #

    def accrued(self, now_mono=None):
        now = self._cloud.clock.monotonic() if now_mono is None else now_mono
        return max(0.0, now - self.started_mono) / 3600.0 * self.dph

    def spent_total(self):
        return self._r.accrued(self._cloud.clock.monotonic())

    def now(self):
        """Monotonic seconds on the rental's clock (a fake one in tests)."""
        return self._cloud.clock.monotonic()

    @property
    def cap(self):
        """Most dollars this rental may spend in total (per-job cap, limited by the month)."""
        return self._r.limit

    def check(self):
        """Raise if the rental must stop NOW: user cancel, spend cap, or lifetime. Every
        wait/run/transfer loop calls this (through the transports' cancel hook)."""
        r, clock = self._r, self._cloud.clock
        if r.cancel is not None and r.cancel():          # core.check_cancel() raises itself
            raise RentalCancelled("cancelled")
        now = clock.monotonic()
        spent = r.accrued(now)
        reserve = self.dph * TEARDOWN_RESERVE_S / 3600.0
        if spent + reserve >= r.limit:
            raise SpendCapReached("spend cap of $%.2f reached ($%.2f spent)" % (r.limit, spent))
        if now - self.started_mono > r.max_hours * 3600:
            raise LifetimeExceeded("rental ran longer than %g h" % r.max_hours)
        if r.on_tick is not None and now - self._last_tick >= 5.0:
            self._last_tick = now
            try:
                r.on_tick(self)
            except Exception:  # noqa: BLE001 - a status hook must never break the rental
                pass

    def sleep(self, seconds):
        """Sleep in short slices so cancel/cap are noticed within a second."""
        clock, end = self._cloud.clock, self._cloud.clock.monotonic() + seconds
        while True:
            self.check()
            left = end - clock.monotonic()
            if left <= 0:
                return
            clock.sleep(min(1.0, left))

    # -- remote work --------------------------------------------------------- #

    def _tp(self):
        if self.endpoint is None:
            raise BoxUnreachable("box has no working ssh endpoint")
        return self._cloud.transport

    def run(self, cmd, *, env=None, stdin_data=None, on_line=None, timeout=None, tty=False,
            prompts=(), grace=0.0):
        """Run a shell command on the box. Output lines go to on_line (redacted). The
        timeout is clamped to the rental's remaining lifetime."""
        left = self._r.max_hours * 3600 - (self._cloud.clock.monotonic() - self.started_mono)
        timeout = min(timeout, left) if timeout else left
        try:
            return self._tp().run(self.endpoint, self.id, cmd, env=env, stdin_data=stdin_data,
                                  on_line=on_line, timeout=timeout, cancel=self.check, tty=tty,
                                  prompts=prompts, grace=grace)
        except cloud_ssh.Cancelled:
            raise RentalCancelled("cancelled") from None
        except cloud_ssh.TimedOut:
            self.check()                   # lifetime / cap beat the plain timeout
            raise

    def run_script(self, script, **kw):
        """Run a multi-line bash script. It is passed as ONE argument (base64 through a
        command substitution) so its commands cannot eat it from stdin and nothing
        needs shell quoting."""
        b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        return self.run('bash -c "$(echo %s | base64 -d)"' % b64, **kw)

    def put(self, local, remote, *, on_line=None, timeout=None):
        try:
            self._tp().put(self.endpoint, self.id, local, remote, on_line=on_line,
                           timeout=timeout, cancel=self.check)
        except cloud_ssh.Cancelled:
            raise RentalCancelled("cancelled") from None

    def get(self, remote, local, *, on_line=None, timeout=None):
        try:
            self._tp().get(self.endpoint, self.id, remote, local, on_line=on_line,
                           timeout=timeout, cancel=self.check)
        except cloud_ssh.Cancelled:
            raise RentalCancelled("cancelled") from None

    # -- teardown ------------------------------------------------------------ #

    def destroy(self, status="ok", reason=""):
        """Destroy this machine. Idempotent, never raises, True once it is verifiably
        gone. On failure the registry keeps the record so the reaper retries."""
        if self._destroyed:
            return True
        try:
            gone = self._cloud._destroy_instance(self.id)
        except Exception as exc:  # noqa: BLE001 - destroy must never raise
            log.error("destroy of instance %s failed: %s", self.id, redact(exc))
            gone = False
        if not gone:
            return False
        self._destroyed = True
        cost = self._cloud._record(self._r.label, self.id, status, reason,
                                   fallback=dict(id=self.id, created_at=self.started_wall,
                                                 gpu=self.gpu, dph=self.dph,
                                                 offer_id=self.offer.id))
        self._r.spent += cost
        self._r.box = None
        self._cloud.transport.cleanup(self.id)
        return True


# --------------------------------------------------------------------------- #
# the Cloud object
# --------------------------------------------------------------------------- #

class Cloud:
    def __init__(self, settings, base_dir, *, http=None, transport=None, clock=None):
        self.settings = settings
        self.base_dir = base_dir
        self.clock = clock or _RealClock()
        self.api = VastApi(lambda: self.settings.get("vast_api_key"), http=http,
                           sleep=self.clock.sleep)
        self.registry = Registry(os.path.join(base_dir, "cloud-instances.json"))
        self.ledger = Ledger(os.path.join(base_dir, "cloud-ledger.jsonl"))
        self.transport = transport or cloud_ssh.SshTransport(
            os.environ.get("VAST_SSH_DIR") or os.path.join(base_dir, "cloud-ssh"), redact=redact)
        self._lock = threading.Lock()
        self._rentals = {}                      # label -> _Rental (this process's live rentals)
        self._quote_cache = {}
        self.reaper_last = None                 # (wall time, result) of the latest sweep

    def config(self):
        return self.settings.config()

    # -- bookkeeping --------------------------------------------------------- #

    def _is_active(self, label):
        with self._lock:
            return label in self._rentals

    def busy(self):
        """True while any machine is running or not yet confirmed destroyed. The API key must
        not change then: the new key could not destroy a box that the old one rented."""
        with self._lock:
            if self._rentals:
                return True
        return any(rec.get("inst") for rec in self.registry.all().values())

    def inflight_usd(self):
        """What the rentals running right now have accrued but not yet ledgered."""
        now = self.clock.monotonic()
        with self._lock:
            return round(sum(r.accrued(now) for r in self._rentals.values()), 4)

    def month_to_date(self):
        return self.ledger.month_to_date(self.clock.time())

    def month_spend_committed(self):
        return round(self.month_to_date() + self.inflight_usd(), 4)

    def _record(self, label, instance_id, status, reason, fallback=None, row=None):
        """Write the ledger line for a destroyed instance, exactly once; returns its cost.
        The registry record is popped atomically, so the rental, the reaper and a label
        sweep cannot all book the same instance."""
        inst = self.registry.pop_inst(label, instance_id)
        if inst is None:
            if any(e.get("instance_id") == instance_id for e in self.ledger.entries()):
                return 0.0                       # another path already booked it
            if fallback is not None:
                inst = fallback
            elif row is not None:
                inst = {"id": instance_id, "created_at": _num(row.get("start_date"), self.clock.time()),
                        "gpu": str(row.get("gpu_name") or ""), "dph": _num(row.get("dph_total"), 0.0),
                        "offer_id": None}
            else:
                return 0.0
        meta = (self.registry.all().get(label) or {}).get("meta") or {}
        ended = self.clock.time()
        started = _num(inst.get("created_at"), ended)
        hours = max(0.0, ended - started) / 3600.0
        usd = round(hours * _num(inst.get("dph")), 4)
        self.ledger.append({
            "ended": _iso(ended), "started": _iso(started),
            "job_id": meta.get("job_id"), "scene_id": meta.get("scene_id"),
            "scene": meta.get("scene"), "preset": meta.get("preset"),
            "offer_id": inst.get("offer_id"), "instance_id": instance_id, "gpu": inst.get("gpu"),
            "dph": inst.get("dph"), "hours": round(hours, 4), "usd": usd,
            "status": status, "reason": redact(reason)[:300], "label": label})
        return usd

    # -- destroying ---------------------------------------------------------- #

    def _destroy_instance(self, instance_id):
        """Destroy with retries; True only when Vast confirms it is gone."""
        for attempt in range(1, 9):
            try:
                if self.api.destroy_instance(instance_id):
                    return True
            except VastAuthError:
                raise
            except CloudError as exc:
                log.warning("destroy attempt %d for instance %s failed: %s", attempt,
                            instance_id, redact(exc))
            self.clock.sleep(min(3.0 * attempt, 15.0))
            try:                               # an unseen success: it may be gone already
                if self.api.show_instance(instance_id) is None:
                    return True
            except CloudError:
                pass
        return False

    def _destroy_by_label(self, label, status, reason):
        """Destroy every instance carrying this label (leftovers, ambiguous creates).
        Returns how many it could NOT destroy, or None when the list was unavailable."""
        try:
            rows = self.api.list_instances()
        except CloudError as exc:
            log.warning("label sweep for %s could not list instances: %s", label, redact(exc))
            return None
        failed = 0
        for row in rows:
            iid = row.get("id")
            if row.get("label") != label or iid is None:
                continue
            try:
                gone = self._destroy_instance(iid)
            except CloudError as exc:          # e.g. the key was revoked meanwhile
                log.warning("label sweep could not destroy %s: %s", iid, redact(exc))
                gone = False
            if gone:
                self._record(label, iid, status, reason, row=row)
            else:
                failed += 1
        return failed

    def _sweep_after_failed_create(self, label):
        """A create that errored may still have rented the machine. Remove it, and STOP
        the job if that cannot be confirmed: renting a second box on top would double
        the bill (the registry keeps the label, so the reaper finishes the job)."""
        left = self._destroy_by_label(label, "error", "create failed; swept by label")
        if left is None or left:
            raise CloudError("a failed create may have left a machine running and it could "
                             "not be removed yet; the reaper will retry")

    # -- search / quote ------------------------------------------------------ #

    def search(self, spec, cached=False):
        """Acceptable offers, best first. cached=True reuses a result <= 2 min old (the
        quote endpoint; the daily search quota is shared with real rentals)."""
        key = json.dumps(dataclasses.asdict(spec), sort_keys=True, default=str)
        now = self.clock.monotonic()
        if cached:
            with self._lock:
                hit = self._quote_cache.get(key)
            if hit and now - hit[0] < QUOTE_CACHE_S:
                return list(hit[1])
        raw = self.api.search_offers(_search_body(spec))
        offers = _rank([o for o in (_parse_offer(r) for r in raw) if o and _acceptable(o, spec)], spec)
        with self._lock:
            self._quote_cache[key] = (now, list(offers))
        return offers

    def best_offer(self, spec):
        offers = self.search(spec, cached=True)
        return offers[0] if offers else None

    # -- renting ------------------------------------------------------------- #

    def _create_payload(self, spec, label):
        pub = self.transport.public_key()
        # Vast runs this once at boot. `.no_auto_tmux` stops Vast's ssh login wrapper from
        # dropping our commands into a tmux session.
        onstart = ("mkdir -p /root/.ssh && chmod 700 /root/.ssh && echo %s >> /root/.ssh/authorized_keys"
                   " && chmod 600 /root/.ssh/authorized_keys && touch /root/.no_auto_tmux" % shlex.quote(pub))
        return {"client_id": "me", "image": spec.image, "disk": spec.disk_gb, "label": label,
                "runtype": "ssh_direct", "env": dict(spec.env), "onstart": onstart,
                "cancel_unavail": True}

    def _create(self, rental, spec, offer):
        """Create the instance for one offer. Registers it BEFORE anything can fail."""
        label = rental.label
        payload = self._create_payload(spec, label)
        try:
            data = self.api.create_instance(offer.id, payload)
        except (VastAuthError, VastQuotaError, NotConfigured):
            raise
        except VastApiError as exc:
            if exc.status == 410 or "no_such_ask" in (exc.code or ""):
                raise OfferUnavailable("offer %d was taken" % offer.id) from None
            # Ambiguous: Vast may have rented it anyway. Sweep before trying another offer.
            self._sweep_after_failed_create(label)
            raise OfferUnavailable("create failed: %s" % redact(exc)) from None
        iid = data.get("new_contract")
        if not data.get("success", True) or iid in (None, ""):
            self._sweep_after_failed_create(label)
            raise OfferUnavailable("create refused: %s" % redact(data.get("msg") or data.get("error") or ""))
        iid = int(iid)
        wall, mono = self.clock.time(), self.clock.monotonic()
        self.registry.set_inst(label, {"id": iid, "created_at": wall, "gpu": offer.gpu,
                                       "dph": offer.price, "offer_id": offer.id})
        box = Box(self, rental, iid, offer, wall, mono, offer.price)
        rental.box = box
        return box

    def _wait_ready(self, box, spec):
        """Poll until ssh works. Never trusts `actual_status == running` alone: the
        mapped port can be dead while the proxy works, and the reverse."""
        deadline = self.clock.monotonic() + spec.boot_timeout
        denied, attached, priced = 0, False, False
        while True:
            box.check()
            info = self.api.show_instance(box.id)
            if info is None:
                raise BoxFailed("the instance disappeared")
            status = info.get("actual_status")
            if status in FAILED_STATES:
                raise BoxFailed("the instance went to state %r: %s" % (status, redact(info.get("status_msg") or "")))
            dph = _num(info.get("dph_total"), 0.0)
            if dph and not priced:
                priced = True
                if dph > spec.max_price * 1.10 + 0.01:      # price drifted between search and create
                    raise BoxFailed("price is $%.2f/h, above the $%.2f/h limit" % (dph, spec.max_price))
                box.dph = max(box.dph, dph)
            for ep in _endpoints(info):
                state, detail = self.transport.probe(ep, box.id, cancel=box.check)
                if state == "ok":
                    box.endpoint = ep
                    return
                denied = denied + 1 if state == "denied" else 0
            if denied >= 3 and not attached:                 # the onstart key never landed
                attached = True
                try:
                    self.api.attach_ssh_key(box.id, self.transport.public_key())
                except CloudError as exc:
                    log.warning("could not attach the ssh key to %s: %s", box.id, redact(exc))
            if self.clock.monotonic() > deadline:
                raise BoxUnreachable("no ssh connection after %d s" % spec.boot_timeout)
            box.sleep(POLL_S)

    def _bootstrap(self, box):
        """Make the dirs we use and make sure rsync exists (plain images lack it)."""
        script = ("set -e; mkdir -p /root/in /root/out; "
                  "command -v rsync >/dev/null 2>&1 || (export DEBIAN_FRONTEND=noninteractive; "
                  "apt-get update -qq && apt-get install -y -qq --no-install-recommends rsync)")
        res = box.run(script, timeout=300)
        if res.code != 0:
            raise BoxFailed("could not prepare the box: " + " | ".join(res.tail[-3:]))

    def _builtin_verify(self, box, spec):
        res = box.run("nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader",
                      timeout=60)
        text = " ".join(res.tail)
        if res.code != 0 or not res.tail:
            raise VerifyFailed("nvidia-smi does not work on this box: " + text[-200:])
        m = re.search(r"(\d+)\s*MiB\s*,\s*([\d.]+)", text)
        if m and int(m.group(1)) / 1024.0 < spec.min_vram_gb * 0.9:
            raise VerifyFailed("the GPU has only %d MiB of memory" % int(m.group(1)))
        if spec.min_driver and m and _driver_major(m.group(2)) < spec.min_driver:
            raise VerifyFailed("NVIDIA driver %s is older than %d" % (m.group(2), spec.min_driver))

    def _acquire(self, rental, spec, verify, on_offer):
        offers = self.search(spec)
        if not offers:
            raise NoOffers("no GPU offer matches (%s, up to $%.2f/h)" %
                           (", ".join(spec.gpus) or "any GPU", spec.max_price))
        last = None
        for n, offer in enumerate(offers[:MAX_ATTEMPTS], 1):
            if rental.spent >= rental.limit:
                raise SpendCapReached("spend cap of $%.2f reached" % rental.limit)
            if on_offer:
                on_offer(n, offer)
            box = None
            try:
                box = self._create(rental, spec, offer)
                self._wait_ready(box, spec)
                self._bootstrap(box)
                self._builtin_verify(box, spec)
                if verify is not None:
                    verify(box)
                return box
            except (OfferUnavailable, BoxFailed) as exc:
                last = exc
                log.warning("offer %d (%s) rejected: %s", offer.id, offer.gpu, redact(exc))
                if box is not None and not box.destroy("failed-offer", str(exc)):
                    raise CloudError("could not destroy rejected box %s; the reaper will retry" % box.id) from None
            except BaseException as exc:
                if box is not None:
                    box.destroy(*self._outcome(exc))
                raise
        raise BoxFailed("tried %d offers, none worked (last: %s)" % (min(len(offers), MAX_ATTEMPTS), redact(last)))

    @staticmethod
    def _outcome(exc):
        """Ledger (status, reason) for an exception that ended a rental."""
        if exc is None:
            return "ok", ""
        if isinstance(exc, SpendCapReached):
            return "cap", str(exc)
        if isinstance(exc, LifetimeExceeded):
            return "lifetime", str(exc)
        if isinstance(exc, (RentalCancelled, cloud_ssh.Cancelled)):
            return "cancelled", "cancelled"
        if isinstance(exc, Exception) and type(exc).__name__ == "Cancelled":
            return "cancelled", "cancelled"
        return "error", "%s: %s" % (type(exc).__name__, redact(exc))

    def _budget(self, cfg, max_cost):
        """Dollars this rental may spend: the per-job cap, but never more than what is
        left of the month (so the monthly cap is a hard cap too)."""
        left = cfg.month_cap - self.month_spend_committed()
        limit = min(cfg.job_cap if max_cost is None else max_cost, left)
        if limit <= 0:
            raise SpendCapReached("the monthly cap of $%.2f is used up" % cfg.month_cap)
        return round(limit, 4)

    @contextlib.contextmanager
    def rent(self, spec, *, cancel=None, verify=None, meta=None, max_cost=None, on_tick=None,
             on_offer=None):
        """Rent a machine for the duration of the with-block and destroy it afterwards.

        cancel: callable polled everywhere; raise from it or return true to stop.
        verify(box): extra acceptance test (raise VerifyFailed to try the next offer).
        meta: dict stored with the ledger lines (job_id, scene_id, scene, preset).
        max_cost: $ for this rental (default: the per-job cap, limited by the month).
        on_tick(box): called about every 5 s while the box is alive (status display).
        on_offer(n, offer): called before each machine is rented (n counts attempts from 1).
        """
        cfg = self.config()
        if not cfg.api_key:
            raise NotConfigured("No Vast.ai API key is set (Cloud settings).")
        register_secret(cfg.api_key)
        with self._lock:
            if len(self._rentals) >= MAX_CONCURRENT_RENTALS:
                raise TooManyRentals("already renting %d machines" % len(self._rentals))
        limit = self._budget(cfg, max_cost)
        label = LABEL_PREFIX + uuid.uuid4().hex[:8]
        rental = _Rental(label, limit, cancel, min(spec.max_hours, cfg.max_job_hours), meta or {}, on_tick)
        with self._lock:
            self._rentals[label] = rental          # active BEFORE the create: the reaper skips it
        box = failure = None
        try:
            self.registry.begin(label, self.clock.time(), rental.max_hours, meta)
            box = self._acquire(rental, spec, verify, on_offer)
            yield box
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                status, reason = box.outcome if (box is not None and box.outcome) else self._outcome(failure)
                clean = True
                for b in {id(x): x for x in (box, rental.box) if x is not None}.values():
                    clean = b.destroy(status, reason) and clean   # never raises; retried via rental.box
                left = self._destroy_by_label(label, status, reason)
                if clean and left == 0:
                    self.registry.remove(label)    # verified nothing is left: forget the rental
            finally:
                with self._lock:
                    self._rentals.pop(label, None)

    # -- reaper -------------------------------------------------------------- #

    def reap_once(self):
        """One sweep over Vast's own instance list. Destroys anything labelled
        stashify-* (or recorded in the registry) that no live rental of this process
        owns, and anything older than its lifetime even if it looks active."""
        cfg = self.config()
        if not cfg.api_key:
            return {"skipped": "no api key"}
        try:
            rows = self.api.list_instances()
        except CloudError as exc:
            log.warning("reaper: could not list instances: %s", redact(exc))
            return {"error": redact(exc)}
        now = self.clock.time()
        reg = self.registry.all()
        by_id = {rec["inst"]["id"]: label for label, rec in reg.items() if rec.get("inst")}
        destroyed, failed, seen = [], [], set()
        for row in rows:
            iid, label = row.get("id"), str(row.get("label") or "")
            if iid is None or not (label.startswith(LABEL_PREFIX) or iid in by_id):
                continue
            seen.add(iid)
            label = label if label.startswith(LABEL_PREFIX) else by_id[iid]
            rec = reg.get(label) or {}
            started = _num(row.get("start_date"), 0.0) or _num((rec.get("inst") or {}).get("created_at"), 0.0) \
                or _num(rec.get("created_at"), now)
            max_age = _num(rec.get("max_hours"), cfg.max_job_hours) * 3600
            if now - started > max_age:
                reason = "reaper: older than %g h" % (max_age / 3600)
            elif self._is_active(label):
                continue
            else:
                reason = "reaper: orphan (no job owns it)"
            if self._destroy_instance(iid):
                self._record(label, iid, "orphan", reason, row=row)
                destroyed.append(iid)
                log.warning("reaper destroyed instance %s (%s)", iid, reason)
            else:
                failed.append(iid)
        for label, rec in reg.items():           # forget rentals with nothing left to kill
            inst = rec.get("inst")
            if self._is_active(label):
                continue
            stale = now - _num(rec.get("created_at"), now) > 600
            if inst and inst.get("id") not in seen:
                self.registry.remove(label)
            elif not inst and stale:
                self.registry.remove(label)
        result = {"destroyed": destroyed, "failed": failed, "checked": len(rows)}
        self.reaper_last = (now, result)
        return result

    def start_reaper(self, every=REAP_EVERY_S):
        """Daemon thread: sweeps at once, then every `every` seconds, never dies."""
        def loop():
            while True:
                try:
                    self.reap_once()
                except Exception as exc:  # noqa: BLE001 - the safety net must outlive any bug
                    log.error("reaper crashed (continuing): %s", redact(exc))
                self.clock.sleep(every)
        t = threading.Thread(target=loop, name="cloud-reaper", daemon=True)
        t.start()
        return t

    # -- read-only views for the API ----------------------------------------- #

    def status(self):
        """No network, no secrets: safe to call as often as the dashboard likes."""
        cfg = self.config()
        now = self.clock.monotonic()
        with self._lock:
            live = [{"label": r.label, "gpu": r.box.gpu if r.box else None,
                     "dph": r.box.dph if r.box else None,
                     "spent": round(r.accrued(now), 4), "cap": r.limit} for r in self._rentals.values()]
        return {
            "configured": bool(cfg.api_key), "max_price": cfg.max_price, "job_cap": cfg.job_cap,
            "month_cap": cfg.month_cap, "month": _month(self.clock.time()),
            "month_to_date": self.month_to_date(), "inflight": self.inflight_usd(),
            "active": live, "recent": self.ledger.last(10),
            "reaper": ({"last_run": _iso(self.reaper_last[0]), **self.reaper_last[1]}
                       if self.reaper_last else None)}

    def offers(self, spec):
        """Market view for /api/cloud/offers: a few best offers (cached, no secrets)."""
        offers = self.search(spec, cached=True)
        return [{"gpu": o.gpu, "price": round(o.price, 3), "vram_gb": round(o.vram_gb, 1),
                 "reliability": round(o.reliability, 3), "datacenter": o.datacenter,
                 "inet_down": round(o.inet_down)} for o in offers[:8]]

    def test_key(self, pasted=None):
        """The dashboard's "Test key" button: asks Vast for the account balance with the key
        just typed (not stored yet) or, if none was typed, the stored one."""
        pasted = (pasted or "").strip()
        api = self.api
        if pasted:
            api = VastApi(lambda: pasted, http=self.api._http, sleep=self.clock.sleep)
        try:
            return {"ok": True, "balance": api.balance()}
        except CloudError as exc:
            return {"ok": False, "error": redact(exc)}


# --------------------------------------------------------------------------- #
# module-level default instance (the server uses these)
# --------------------------------------------------------------------------- #

_default = None
_default_lock = threading.Lock()


def cloud():
    global _default
    with _default_lock:
        if _default is None:
            base = os.environ.get("CLOUD_DIR", "/config")
            _default = Cloud(Settings(os.path.join(base, "cloud.json")), base)
        return _default


def set_cloud(instance):
    """Replace the process-wide instance (tests inject one built on fakes)."""
    global _default
    with _default_lock:
        _default = instance
