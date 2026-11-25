"""Dashboard-facing side of cloud Jasna: settings, key test, the GPU market, quotes, and the
admission decision for a paid job.

Plain functions on dicts (no HTTP), so server.py stays a thin router and the rules that
protect the wallet are testable on their own. The refusal rules themselves live in
cloud_jasna.quote_with_spec: the SAME function produces the quote the user confirms and the
refusal the server enforces, so the dashboard cannot disagree with the enforcement.

Every function returns (http_status, payload) when it can fail; payloads never contain a
secret (the key and the licence are write-only)."""

import dataclasses
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
    (looked up at most once a minute). Each running machine also says which scene and quality
    it is for, what it has cost so far (`usd`) and when it started (`started`, epoch seconds)."""
    out = cl.status(with_credit=True)
    registry = cl.registry.all()
    for row in out["active"]:
        rec = registry.get(row.get("label")) or {}
        meta, inst = rec.get("meta") or {}, rec.get("inst") or {}
        row.update(scene=meta.get("scene"), scene_id=meta.get("scene_id"), preset=meta.get("preset"),
                   usd=row.get("spent"), started=inst.get("created_at") or None)
    return 200, out


# -- market view ------------------------------------------------------------------------ #

# Query parameters that belong to Stashify, not to hireling's market contract.
OFFER_PARAMS = ("scene", "scene_ids", "preset", "op", "upscale", "upscale_height", "hours")


def _rental_estimator(hours, disk_gb):
    """No scene: cost a plain rental of `hours` (disk and nothing else moved)."""
    note = ("A plain rental of %g hours: no transfers and no GPU work counted." % hours,)
    return lambda offer: hireling.JobEstimate(hours, hireling.estimate_cost(offer, hours, disk_gb=disk_gb), note)


def _caps(cl):
    """The owner's limits and this month's spend, so the browser can mark what is over them."""
    cfg = cl.config()
    return {"max_price": cfg.max_price, "job_cap": cfg.job_cap, "month_cap": cfg.month_cap,
            "max_job_hours": cfg.max_job_hours, "month_to_date": round(cl.month_spend_committed(), 2)}


def offers(cl, query, meta=None):
    """GET /api/cloud/offers : the whole GPU market behind the enabled providers, with
    hireling's filter / sort / page parameters, every offer marked compatible or not with
    what Jasna needs (and why not).

    `meta` is the scene's facts (cloud_jasna.scene_meta) when the query names a scene, or a
    list of them for `scene_ids`. Every offer then carries what THIS job would take and cost on
    it (minutes, price, the compute / storage / bandwidth breakdown), the cost and time sorts
    work, and the hardware choices (fastest, best value) are picked for the job. Several scenes
    are one rental each, run one after another on the machine: the estimate is their sum.
    `op`, `preset`, `upscale` and `upscale_height` say which job. Without a scene, `hours`
    costs a plain rental of that length instead."""
    if not cl.enabled_providers():
        return REFUSED, {"error": "Add your Vast.ai API key in Cloud settings first.", "code": "no_key"}
    cfg = cl.config()
    try:
        plan = cloud_jasna.plan_from_body({k: query[k] for k in ("op", "upscale", "upscale_height") if k in query})
    except cloud_jasna.PlanError as exc:
        return 400, {"error": str(exc), "code": exc.code}
    metas = [] if meta is None else list(meta) if isinstance(meta, (list, tuple)) else [meta]
    requirements, estimator, target = cloud_jasna.requirements(cfg), None, None
    if metas:
        restoring = plan.op != "upscale"
        preset = query.get("preset") or cloud_jasna.DEFAULT_PRESET
        if restoring and preset not in cloud_jasna.PRESETS:
            return 400, {"error": "Unknown cloud quality preset.", "code": "bad_preset"}
        # one rental per scene: the machine must fit the scene that needs the most disk
        requirements = max((cloud_jasna.requirements(cfg, m.get("size") or 0, ratio=cloud_jasna.result_ratio(m, plan))
                            for m in metas), key=lambda r: r.disk_gb)
        known = all((m.get("duration") or 0) > 0 for m in metas)
        if known:
            estimator = (cloud_jasna.make_estimator(cfg, metas[0], preset, plan) if len(metas) == 1
                         else cloud_jasna.make_estimator_many(cfg, metas, preset, plan))
        target = {"scene_id": metas[0].get("id"), "title": metas[0].get("title"), "op": plan.op,
                  "preset": preset if restoring else None, "upscale": plan.upscale or None,
                  "upscale_height": plan.upscale_height or None,
                  "label": cloud_jasna.plan_label(plan, preset), "estimated": known}
        if len(metas) > 1:
            target.update(scene_ids=[m.get("id") for m in metas], scenes=len(metas))
    elif "hours" in query:
        hours = _num(query["hours"])
        if hours is None or not 0 < hours <= 168:
            return 400, {"error": "hours must be a number up to 168.", "code": "bad_query"}
        estimator = _rental_estimator(hours, requirements.disk_gb)
    try:
        payload = hireling.offers_payload(cl, query, requirements=requirements, estimator=estimator,
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
    payload["for"] = target
    payload["caps"] = _caps(cl)
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

def quotes(cl, items, preset, queued_usd=0.0, params=None):
    """One quote per (scene_id, meta). Each quote counts the ones before it as queued, so
    the monthly cap is checked cumulatively across a multi-scene selection. `params` carries
    the plan fields (op, upscale, upscale_height, offer_id, provider, gpu, hardware,
    cloud_filters); without them it is the plain automatic Decensor."""
    try:
        plan = cloud_jasna.plan_from_body(params)
    except cloud_jasna.PlanError as exc:
        return 400, {"error": str(exc), "code": exc.code}
    restoring = plan.op != "upscale"
    if restoring and preset not in cloud_jasna.PRESETS:
        return 400, {"error": "Unknown cloud quality preset.", "code": "bad_preset"}
    preset = preset if restoring else None
    out, running = [], float(queued_usd)
    for scene_id, meta in items:
        if meta is None:
            out.append({"scene_id": scene_id, "ok": False, "est_usd": None,
                        "refusal": {"code": "no_scene", "message": "Scene not found in Stash."}})
            continue
        q = cloud_jasna.build_quote(cl, meta, preset, queued_usd=running, plan=plan)
        if q.get("ok"):
            running += q["est_usd"]
        out.append(q)
    return 200, {"op": plan.op, "preset": preset, "label": cloud_jasna.plan_label(plan, preset),
                 "plan": plan.as_dict(), "quotes": out,
                 "total_usd": round(sum(q["est_usd"] for q in out if q.get("ok")), 2),
                 "queued_usd": round(float(queued_usd), 2)}


# -- admission -------------------------------------------------------------------------- #

def admit(cl, body, meta, queued_usd, backend):
    """The server-side gate for a paid job, whatever the UI did. Returns (202, cloud_record)
    or (status, {error, code, quote?}). Refuses: an operation, model, height or machine
    request it does not know (400), no key / no licence, an estimate over the per-job cap or
    the per-rental time limit, month-to-date + queued + estimate over the monthly cap, a
    machine that is gone, a price that moved since the user confirmed, and any job that did
    not carry a confirmed price. A hardware choice (cheapest, fastest...) is resolved to ONE
    machine here; the job then rents exactly that one or nothing."""
    try:
        plan = cloud_jasna.plan_from_body(body, backend)
    except cloud_jasna.PlanError as exc:
        return 400, {"error": str(exc), "code": exc.code}
    restoring = plan.op != "upscale"
    preset = body.get("cloud_preset") if restoring else None
    if restoring and preset not in cloud_jasna.PRESETS:
        return 400, {"error": "Unknown cloud quality preset.", "code": "bad_preset"}
    confirm = _num(body.get("confirm_usd"))
    if confirm is None or confirm <= 0:
        return 400, {"error": "A paid job must carry the cost the user confirmed.", "code": "no_confirm"}
    if meta is None:
        return 404, {"error": "Scene not found in Stash.", "code": "no_scene"}
    quote, _spec = cloud_jasna.quote_with_spec(cl, meta, preset, plan, queued_usd, fresh=True)
    refusal = quote.get("refusal")
    if refusal:
        return REFUSED, {"error": refusal["message"], "code": refusal["code"], "quote": quote}
    if cloud_jasna.price_changed(confirm, quote["est_usd"]):
        return REFUSED, {"error": "The price moved from the $%.2f you confirmed to about $%.2f. "
                                  "Nothing was rented; confirm the new price to continue."
                                  % (confirm, quote["est_usd"]),
                         "code": "price_changed", "quote": quote}
    offer = quote["offer"]
    if plan.hardware or plan.offer_id:       # a choice stands for this machine, on its provider, and only it
        plan = dataclasses.replace(plan, offer_id=str(offer["id"]), provider=offer["provider"], hardware="")
    return 202, {"preset": preset, "label": quote["label"], "scene": meta, "est_usd": quote["est_usd"],
                 "confirm_usd": confirm, "cap": quote["job_cap"], "gpu": quote["gpu"], "op": plan.op,
                 "plan": plan.as_dict(), "offer_id": plan.offer_id or None, "provider": plan.provider or None,
                 "hardware": body.get("hardware") or None, "est_min": quote.get("total_min")}
