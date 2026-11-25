"""Test doubles for the cloud modules: hireling's fakes (a fake clock, a fake Vast REST API and
a fake ssh transport) set up the way Stashify names things. Nothing here touches the network,
a real clock or a real machine. Needs the hireling package importable (installed, or
PYTHONPATH=<checkout>)."""

import logging
import os
import sys

os.environ.setdefault("STASHIFY_RUNNER_STREAM", "0")     # no background /events readers in tests that fake the network
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hireling import testing as ht      # noqa: E402
from hireling.testing import (INSTANCE_KEY, KEY, FakeClock, FakeTransport, FakeVast,   # noqa: E402,F401
                              Resp, charge_row, raw_offer)

import cloud_jasna                      # noqa: E402

logging.getLogger("hireling").addHandler(logging.NullHandler())   # quiet output; assertLogs still sees records

LICENCE = "JASNA-TEST-LICENCE-CODE-9876"          # the (fake) Jasna licence code


class CloudCase(ht.CloudCase):
    """setUp builds a Cloud on fakes in a throwaway directory: Stashify's label prefix, its
    Jasna licence field and its VAST_* environment names, as make_cloud() sets them up."""

    prefix = cloud_jasna.LABEL_PREFIX
    keys = {"vast_api_key": KEY, "jasna_license": LICENCE}
    settings_kw = {"secrets": cloud_jasna.SECRET_FIELDS, "env_names": cloud_jasna.ENV_NAMES}


# One of each kind of machine: the fastest card, the cheapest card, a plain community 4090 and the
# most reliable datacenter host.
MARKET = [(11, "RTX 5090", 0.90, {"vram_mb": 32607, "reliability": 0.99, "inet_down": 2000.0}),
          (12, "RTX 4090", 0.40, {"reliability": 0.985, "datacenter": False}),
          (13, "RTX 3090", 0.22, {"vram_mb": 24576, "reliability": 0.981, "datacenter": False,
                                  "inet_down": 500.0}),
          (14, "RTX 4090", 0.55, {"reliability": 0.999})]
UPSCALE = {"op": "decensor+upscale", "upscale": "span", "upscale_height": 2160}   # a plan body


def market(extra=()):
    """Raw Vast offers for MARKET (plus extra (id, gpu, price, more) rows), ready for FakeVast.offers."""
    return [raw_offer(oid, gpu, price, **more) for oid, gpu, price, more in list(MARKET) + list(extra)]
