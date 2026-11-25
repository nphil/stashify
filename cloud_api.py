"""Dashboard-facing side of cloud Jasna: settings, key test, quotes, and the admission
decision for a paid job.

Plain functions on dicts (no HTTP), so server.py stays a thin router and the rules that
protect the wallet are testable on their own. The refusal rules themselves live in
cloud_jasna.build_quote: the SAME function produces the quote the user confirms and the
refusal the server enforces, so the dashboard cannot disagree with the enforcement.

Every function returns (http_status, payload) when it can fail; payloads never contain a
secret (the key and the licence are write-only)."""

import math
import re

import cloud_jasna
import hireling
from hireling import CloudError, redact

REFUSED = 409          # a refusal conflicts with the current settings, budget or market


def _num(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# -- settings --------------------------------------------------------------------------- #

def settings_get(cl):
    return 200, cl.settings.public()


def _key_changes(cl, field, value):
    current = cl.settings.get(field)
    if value is None:
        return bool(current)
    return isinstance(value, str) and bool(value.strip()) and value.strip() != current


def settings_save(cl, body):
    """A bad value is a 400 with a plain message and changes nothing. A provider's key cannot
    change while one of its machines is running: the new key could not destroy it."""
    if not isinstance(body, dict):
        return 400, {"error": "expected a JSON object"}
    for provider in cl.settings.providers:
        field = "%s_api_key" % provider
        if field in body and _key_changes(cl, field, body[field]) and cl.busy(provider):
            return REFUSED, {"error": "A cloud machine is still running, or not yet confirmed destroyed. "
                                      "Change the %s key after it is gone." % provider.capitalize(),
                             "code": "busy"}
    try:
        return 200, cl.settings.update(body)
    except ValueError as exc:
        return 400, {"error": str(exc)}


def test_key(cl, body):
    """The settings page's Test button. The dashboard sends {vast_api_key}; another provider
    sends {provider, <provider>_api_key}. A good answer keeps `balance`, the dashboard's name."""
    body = body if isinstance(body, dict) else {}
    provider = body["provider"] if isinstance(body.get("provider"), str) else "vast"
    pasted = body.get("%s_api_key" % provider)
    res = cl.test_key(provider, pasted if isinstance(pasted, str) else None)
    if res.get("ok"):
        res["balance"] = res["credit"]
    return 200, res


def status(cl):
    """Settings-page status: spend, running machines, and each provider with its credit
    (looked up at most once a minute)."""
    return 200, cl.status(with_credit=True)


# -- market view ------------------------------------------------------------------------ #

# Query parameters that belong to Stashify, not to hireling's market contract.
OFFER_PARAMS = ("scene", "preset", "op", "upscale", "upscale_height", "hours")


def offers(cl, query):
    """GET /api/cloud/offers : the whole GPU market behind the enabled providers, with
    hireling's filter / sort / page parameters, every offer marked compatible or not with
    what Jasna needs (and why not)."""
    if not cl.enabled_providers():
        return REFUSED, {"error": "Add your Vast.ai API key in Cloud settings first.", "code": "no_key"}
    try:
        payload = hireling.offers_payload(cl, query, requirements=cloud_jasna.requirements(cl.config()),
                                          ignore=OFFER_PARAMS)
    except ValueError as exc:
        return 400, {"error": str(exc), "code": "bad_query"}
    except hireling.NotConfigured as exc:
        return REFUSED, {"error": redact(exc), "code": "no_key"}
    except CloudError as exc:
        return 502, {"error": redact(exc), "code": "offers_unavailable"}
    if payload["errors"] and not payload["offers"]:        # nothing to show and a provider failed: say why
        return 502, {"error": next(iter(payload["errors"].values())), "code": "offers_unavailable",
                     "errors": payload["errors"]}
    return 200, payload


# -- scenes ----------------------------------------------------------------------------- #

_ID = re.compile(r"\d{1,12}")


def scene_metas(gql, ids):
    """Stash's facts for each scene id, in order (None for an unknown id), in one round trip.
    gql(query) is server.stash_gql; ids are digits only, so building the query is safe."""
    ids = [str(i) for i in ids]
    good = [i for i in ids if _ID.fullmatch(i)]
    if not good:
        return [None] * len(ids)
    frag = "id title files { path width height duration size }"
    query = "query{" + " ".join('s%d: findScene(id:"%s"){%s}' % (n, i, frag)
                                for n, i in enumerate(good)) + "}"
    data = gql(query) or {}
    found = {i: data.get("s%d" % n) for n, i in enumerate(good)}
    return [cloud_jasna.scene_meta(found[i]) if found.get(i) else None for i in ids]


# -- quotes ----------------------------------------------------------------------------- #

def quotes(cl, items, preset, queued_usd=0.0):
    """One quote per (scene_id, meta). Each quote counts the ones before it as queued, so
    the monthly cap is checked cumulatively across a multi-scene selection."""
    if preset not in cloud_jasna.PRESETS:
        return 400, {"error": "Unknown cloud quality preset.", "code": "bad_preset"}
    out, running = [], float(queued_usd)
    for scene_id, meta in items:
        if meta is None:
            out.append({"scene_id": scene_id, "ok": False, "est_usd": None,
                        "refusal": {"code": "no_scene", "message": "Scene not found in Stash."}})
            continue
        q = cloud_jasna.build_quote(cl, meta, preset, queued_usd=running)
        if q.get("ok"):
            running += q["est_usd"]
        out.append(q)
    return 200, {"preset": preset, "label": cloud_jasna.PRESETS[preset]["label"], "quotes": out,
                 "total_usd": round(sum(q["est_usd"] for q in out if q.get("ok")), 2),
                 "queued_usd": round(float(queued_usd), 2)}


# -- admission -------------------------------------------------------------------------- #

def admit(cl, body, meta, queued_usd, backend):
    """The server-side gate for a paid job, whatever the UI did. Returns (202, cloud_record)
    or (status, {error, code, quote?}). Refuses: no key / no licence, an estimate over the
    per-job cap, month-to-date + queued + estimate over the monthly cap, a price that moved
    since the user confirmed, and any job that did not carry a confirmed price."""
    preset = body.get("cloud_preset")
    if preset not in cloud_jasna.PRESETS:
        return 400, {"error": "Unknown cloud quality preset.", "code": "bad_preset"}
    if backend not in ("decensor", "lada") or body.get("post_upscale"):
        return 400, {"error": "Cloud quality applies to plain Decensor only.", "code": "bad_op"}
    confirm = _num(body.get("confirm_usd"))
    if confirm is None or confirm <= 0:
        return 400, {"error": "A paid job must carry the cost the user confirmed.", "code": "no_confirm"}
    if meta is None:
        return 404, {"error": "Scene not found in Stash.", "code": "no_scene"}
    quote = cloud_jasna.build_quote(cl, meta, preset, queued_usd=queued_usd)
    refusal = quote.get("refusal")
    if refusal:
        return REFUSED, {"error": refusal["message"], "code": refusal["code"], "quote": quote}
    if cloud_jasna.price_changed(confirm, quote["est_usd"]):
        return REFUSED, {"error": "The price moved from the $%.2f you confirmed to about $%.2f. "
                                  "Nothing was rented; confirm the new price to continue."
                                  % (confirm, quote["est_usd"]),
                         "code": "price_changed", "quote": quote}
    return 202, {"preset": preset, "label": quote["label"], "scene": meta, "est_usd": quote["est_usd"],
                 "confirm_usd": confirm, "cap": quote["job_cap"], "gpu": quote["gpu"]}
