"""Test doubles for the cloud modules: a fake clock, a fake Vast REST API and a fake ssh
transport. Nothing here touches the network, a real clock or a real machine."""

import json
import logging
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cloud_ssh      # noqa: E402
import cloud_vast     # noqa: E402

cloud_vast.log.addHandler(logging.NullHandler())      # keep test output quiet; assertLogs still sees records

KEY = "vast-test-key-0123456789abcdef"            # the (fake) Vast API key
LICENCE = "JASNA-TEST-LICENCE-CODE-9876"          # the (fake) Jasna licence code
INSTANCE_KEY = "instance-api-key-ZZZZZZZZZZ"      # what a create answers with


class FakeClock:
    """sleep() only moves the clock, so a '10 minute boot' costs no real time."""

    def __init__(self):
        self.wall = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        self.mono = 1000.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        self.advance(seconds)

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


def raw_offer(oid, gpu="RTX 4090", price=0.40, vram_mb=24564, driver="580.95.05",
              reliability=0.99, inet_down=900.0, datacenter=True):
    # mirrors a real offer: no "datacenter" key, data-centre hosts have hosting_type 1
    return {"id": oid, "gpu_name": gpu, "dph_total": price, "gpu_ram": vram_mb,
            "driver_version": driver, "reliability2": reliability, "inet_down": inet_down,
            "hosting_type": 1 if datacenter else 0, "cuda_max_good": 12.9}


class FakeVast:
    """Stands in for console.vast.ai. Pass an instance as `http=`. Unknown routes fail
    loudly, so a wrong path (e.g. a missing trailing slash) cannot pass unnoticed."""

    def __init__(self, clock, offers=None):
        self.clock = clock
        self.offers = list(offers) if offers is not None else [raw_offer(1), raw_offer(2, price=0.45),
                                                               raw_offer(3, price=0.50), raw_offer(4, price=0.55)]
        self.instances = {}            # id -> raw instance dict
        self.calls = []                # (method, path, json body)
        self.auth = []                 # Authorization headers seen
        self.search_bodies = []
        self.next_id = 7001
        self.create_modes = []         # per create call: ok|500|false-but-rented|500-but-rented|gone|auth
        self.status = "running"        # actual_status of a new instance
        self.status_seq = {}           # id -> statuses to answer with, in order (last one repeats)
        self.destroy_fail = 0          # that many destroy calls answer 500 first (inf = always)
        self.list_fail = False
        self.page_size = None          # paginate the instance list
        self.balance = 12.34
        self.destroyed = []
        self.once = []                 # [method, path-regex, Resp or exception] one-shot overrides
        self.follow = []               # (method, allow_redirects) of every call
        self.price_override = {}       # new instance id -> its actual $/h (differs from the offer)

    # -- helpers for tests ---------------------------------------------------- #

    def live(self):
        return sorted(self.instances)

    def count(self, method, path_re):
        return sum(1 for m, p, _b in self.calls if m == method and re.search(path_re, p))

    def add_instance(self, label, price=0.40, start_age_s=0.0, status="running"):
        """An instance that exists on Vast without this process having created it."""
        iid = self.next_id
        self.next_id += 1
        self.instances[iid] = self._inst(iid, label, price, "RTX 4090", start_age_s, status)
        return iid

    def _inst(self, iid, label, price, gpu, start_age_s=0.0, status=None):
        return {"id": iid, "label": label, "actual_status": status or self.status,
                "public_ipaddr": "203.0.113.%d" % (iid % 200),
                "ports": {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(40000 + iid % 1000)}]},
                "ssh_host": "ssh%d.vast.ai" % (iid % 9), "ssh_port": 20000 + iid % 1000,
                "dph_total": price, "start_date": self.clock.time() - start_age_s,
                "gpu_name": gpu, "status_msg": ""}

    # -- the REST surface ------------------------------------------------------- #

    def __call__(self, method, url, *, headers, params, json, timeout, allow_redirects):
        path = url.split("console.vast.ai", 1)[1]
        self.calls.append((method, path, json))
        self.follow.append((method, allow_redirects))
        self.auth.append(headers.get("Authorization"))
        for entry in list(self.once):
            if entry[0] == method and re.search(entry[1], path):
                self.once.remove(entry)
                if isinstance(entry[2], BaseException):
                    raise entry[2]
                return entry[2]
        for verb, rx, name in (
                ("POST", r"^/api/v0/bundles/$", "_search"),
                ("PUT", r"^/api/v0/asks/(\d+)/$", "_create"),
                ("GET", r"^/api/v0/instances/(\d+)/$", "_show"),
                ("GET", r"^/api/v1/instances/$", "_list"),
                ("DELETE", r"^/api/v0/instances/(\d+)/$", "_destroy"),
                ("POST", r"^/api/v0/instances/(\d+)/ssh/$", "_attach"),
                ("GET", r"^/api/v0/users/current$", "_user")):
            m = re.match(rx, path)
            if verb == method and m:
                return getattr(self, name)(*[int(g) for g in m.groups()], body=json, params=params or {})
        raise AssertionError("unexpected Vast call %s %s" % (method, path))

    def _search(self, body, params):
        self.search_bodies.append(body)
        return Resp(200, {"offers": list(self.offers)})

    def _create(self, offer_id, body, params):
        mode = self.create_modes.pop(0) if self.create_modes else "ok"
        if mode == "gone":
            return Resp(410, {"error": "no_such_ask", "msg": "offer gone"})
        if mode == "auth":
            return Resp(401, {"error": "invalid_user_key"})
        if mode == "500":
            return Resp(500, {"error": "server_error", "msg": "boom"})
        offer = next((o for o in self.offers if o["id"] == offer_id), None)
        price = offer["dph_total"] if offer else 0.50
        iid = self.next_id
        self.next_id += 1
        price = self.price_override.get(iid, price)
        self.instances[iid] = self._inst(iid, body["label"], price,
                                         (offer or {}).get("gpu_name", "RTX 4090"))
        if mode == "false-but-rented":
            return Resp(200, {"success": False, "msg": "internal error"})
        if mode == "500-but-rented":
            return Resp(500, {"error": "server_error", "msg": "boom"})
        return Resp(200, {"success": True, "new_contract": iid, "instance_api_key": INSTANCE_KEY})

    def _show(self, iid, body, params):
        inst = self.instances.get(iid)
        if inst is None:
            return Resp(200, {"instances": None})
        seq = self.status_seq.get(iid)
        if seq:
            inst["actual_status"] = seq.pop(0) if len(seq) > 1 else seq[0]
        return Resp(200, {"instances": dict(inst)})

    def _list(self, body, params):
        if self.list_fail:
            return Resp(500, {"error": "server_error", "msg": "list is down"})
        rows = [dict(self.instances[i]) for i in sorted(self.instances)]
        if self.page_size:
            start = int(params.get("after_token") or 0)
            page = rows[start:start + self.page_size]
            nxt = str(start + self.page_size) if start + self.page_size < len(rows) else None
            return Resp(200, {"instances": page, "next_token": nxt})
        return Resp(200, {"instances": rows, "next_token": None})

    def _destroy(self, iid, body, params):
        if self.destroy_fail:
            self.destroy_fail -= 1
            return Resp(500, {"error": "server_error", "msg": "cannot destroy now"})
        if iid not in self.instances:
            return Resp(404, {"error": "no_such_instance", "msg": "gone"})
        del self.instances[iid]
        self.destroyed.append(iid)
        return Resp(200, {"success": True})

    def _attach(self, iid, body, params):
        return Resp(200, {"success": True})

    def _user(self, body, params):
        # the real response carries both; only `credit` holds the prepaid money
        return Resp(200, {"id": 1, "email": "owner@example.invalid",
                          "balance": 0, "credit": self.balance})


class FakeTransport(cloud_ssh.Transport):
    """A scripted ssh transport. `handlers` = [(regex on the command, fn)]; fn(tag, cmd,
    kw) returns a RunResult (or raises). A command no handler claims succeeds quietly.
    simulate(seconds) lets a handler take fake time while polling cancel() like the real
    transport does."""

    def __init__(self, clock):
        self.clock = clock
        self.probe_script = {}         # instance id -> states (last repeats); default always ok
        self.runs = []                 # (tag, cmd, kwargs)
        self.puts, self.gets, self.cleaned = [], [], []
        self.handlers = [(re.compile(r"nvidia-smi"), self._smi)]
        self.put_error = self.get_error = None

    @staticmethod
    def _smi(tag, cmd, kw):
        return cloud_ssh.RunResult(0, ["NVIDIA GeForce RTX 4090, 24564 MiB, 580.95.05"])

    def public_key(self):
        return "ssh-ed25519 AAAATESTKEYONLY stashify-test"

    def probe(self, ep, tag, cancel=None):
        if cancel:
            cancel()
        seq = self.probe_script.get(tag)
        state = "ok"
        if seq:
            state = seq.pop(0) if len(seq) > 1 else seq[0]
        return state, ""

    def simulate(self, seconds, cancel, step=10.0):
        done = 0.0
        while done < seconds:
            if cancel:
                cancel()
            self.clock.sleep(min(step, seconds - done))
            done += step

    def run(self, ep, tag, cmd, *, env=None, stdin_data=None, on_line=None, timeout=None,
            cancel=None, tty=False, prompts=(), grace=0.0):
        kw = dict(env=env, stdin_data=stdin_data, on_line=on_line, timeout=timeout, cancel=cancel,
                  tty=tty, prompts=prompts, grace=grace)
        self.runs.append((tag, cmd, kw))
        if cancel:
            cancel()
        for rx, fn in self.handlers:
            if rx.search(cmd):
                return fn(tag, cmd, kw)
        return cloud_ssh.RunResult(0, [])

    def put(self, ep, tag, local, remote, *, on_line=None, timeout=None, cancel=None):
        self.puts.append((tag, local, remote))
        if cancel:
            cancel()
        if self.put_error:
            raise self.put_error
        if on_line:
            on_line("1,000  100%  10.00MB/s  0:00:01 (xfr#1, to-chk=0/1)")

    def get(self, ep, tag, remote, local, *, on_line=None, timeout=None, cancel=None):
        self.gets.append((tag, remote, local))
        if cancel:
            cancel()
        if self.get_error:
            raise self.get_error
        if on_line:
            on_line("1,000  100%  10.00MB/s  0:00:01 (xfr#1, to-chk=0/1)")
        with open(local, "wb") as fh:
            fh.write(b"decensored-bytes")

    def cleanup(self, tag):
        self.cleaned.append(tag)

    def commands(self):
        return [cmd for _t, cmd, _kw in self.runs]


class CloudCase(unittest.TestCase):
    """setUp builds a Cloud on fakes in a throwaway directory."""

    caps = {"max_price": 1.00, "job_cap": 3.00, "month_cap": 10.00}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cloudtest-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.clock = FakeClock()
        self.vast = FakeVast(self.clock)
        self.tp = FakeTransport(self.clock)
        self.settings = cloud_vast.Settings(os.path.join(self.dir, "cloud.json"), env={})
        self.settings.update(dict(self.caps, vast_api_key=KEY, jasna_license=LICENCE))
        self.cl = cloud_vast.Cloud(self.settings, self.dir, http=self.vast, transport=self.tp,
                                   clock=self.clock)

    def spec(self, **kw):
        base = dict(gpus=("RTX 4090", "RTX 3090"), min_vram_gb=16, max_price=1.0, min_driver=580)
        base.update(kw)
        return cloud_vast.RentSpec(**base)

    def ledger(self):
        return self.cl.ledger.entries()

    def read(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as fh:
            return fh.read()
