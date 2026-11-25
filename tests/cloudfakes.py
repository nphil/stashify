"""Test doubles for the cloud modules: hireling's fakes (a fake clock, a fake Vast REST API and
a fake ssh transport) set up the way Stashify names things. Nothing here touches the network,
a real clock or a real machine. Needs the hireling package importable (installed, or
PYTHONPATH=<checkout>)."""

import logging
import os
import sys

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
