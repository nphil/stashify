# Backend needs for the new dashboard

The dashboard (`web/`) is a static single-page app. It talks to the existing `api/...` routes with relative URLs and the `X-Decensor-Token` header. Everything marked **required** must exist before the old UI in `webui/` can be retired; everything marked **optional** has a working fallback in the UI.

## Status: integrated

Everything below is implemented in `server.py`, `core.py`, `cloud_api.py`, `cloud_jasna.py` and `events.py`, and the old `webui/` is gone. The server serves `web/dist` (override with `WEBUI_DIR`). The differences from what this file asked for:

- **Declined: `POST api/cloud/rent`.** A plain rental with no job is out of scope, so the Rent page and its entry points were removed from the app instead.
- **Declined: `rented` flag and `facets.rented`.** The Vast search never returns rented machines; the UI only sends `rented=1` once the server has reported `facets.rented`.
- **Declined: runner `speed`.** Nothing measures it. `wake_eta_s` is the configured `RUNNER_WAKE_SECS` budget, not a measured time.
- **Declined: the `/stashify` to `/stashify/` redirect.** The reverse proxy strips the prefix and the site is served at the root.
- **Not sent: `sent_bytes`** (upload progress); the field is optional and the UI shows a dash.
- **Adapter, not backend:** the real status shape (`providers[]` rows, `active[].provider` ids, a paid job's `cloud_gpu`/`cloud_provider`/`cloud_est`) is mapped in `src/lib/api/cloudAdapter.ts` (`adaptStatus`, `adaptJob`).
- **Runner flapping:** the server retries a failed probe once after 0.25 s; the UI flips a runner offline only after 2 polls in a row (the runner event is re-sent every 5 s so this counts).

## 1. Serving the built app (required)

1. Build once: `cd web && npm ci && npm run build` → `web/dist/`. (A Docker multi-stage `node` build, or committing `dist/`, both work. The build needs Node 20+; the runtime needs none.)
2. Point `WEBUI_DIR` (default `./webui`) at `web/dist`. `index.html` keeps the `__WORKER_TOKEN__` placeholder (`window.__WT = "…"`); the existing replace-in-`index.html` step in `server.py` already covers it.
3. Cache headers: files under `assets/` and the font are content-hashed → `Cache-Control: public, max-age=31536000, immutable`. `index.html` → `Cache-Control: no-cache`. Enable gzip or brotli for `.js` and `.css` (the app is ~3x smaller on the wire).
4. Sub-paths work (the build uses `base: './'` and hash routes such as `#/jobs`), but the page URL must end in `/` (or `/index.html`) so relative `api/...` calls resolve: redirect `/stashify` → `/stashify/`.
5. No SPA rewrite rules are needed (routing is in the `#hash`). An unknown path may still fall back to `index.html` as today.

## 2. Live updates: `GET api/events` (optional, recommended)

Without it the app polls (jobs 1.5 s, GPU meters 3 s, runners 5 s, cloud status 15 s) and the UI still works. With it the polls slow to a 30 s safety net and updates arrive instantly.

- Server-Sent Events, `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`. The client uses `fetch` streaming (not `EventSource`) so it can send `X-Decensor-Token`; it also sends `Last-Event-ID` after a reconnect.
- Send a comment line (`: ping`) at least every 20 s: the client drops a stream that is silent for 45 s and reconnects with back-off (1 s → 30 s).
- Return **404/405/501** if you do not implement it: the client stops asking for 5 minutes and polls instead.
- Events (each `data:` is one JSON line):

| event | payload | same as |
|---|---|---|
| `jobs` | full job list, oldest first | `GET api/jobs` |
| `gpus` | list of `{name,url,online,active,gpu:{util,mem_used,mem_total,temp,power}}` | `GET api/gpus` |
| `runners` | runner list | `GET api/runners` |
| `cloud` | cloud status object | `GET api/cloud/status` |
| `log` | `{job_id, cursor, lines:[{seq,t,level,text[,live]}]}`, batched ≤ 4 per second per running job | `GET api/jobs/<id>/log?after=` |
| `ping` | anything (ignored) | keep-alive |

Send each snapshot event once when a client connects, then on change.

## 3. Endpoint gaps the UI already codes against (all optional: the UI falls back when a field is missing)

**Scenes** (`GET api/scenes`, `GET api/tags`)
- Honour `dir=asc|desc` and `sort=created_at|file_mod_time|title|date|random`; `per_page` up to 60; `count` must reflect the search and tag filter.
- `sort=random` needs `seed=<int>` and a stable order per seed, or paging repeats scenes (the client drops duplicates meanwhile).
- `GET api/tags` → `[{id, name, scene_count}]`.

**Jobs** (`GET api/jobs`, SSE `jobs`)
- Add `scene_title` (else the UI shows "Scene 123"), `op` (`decensor|upscale|transcode`), `quality` (`fast|balanced|high|max`), `created` (epoch s), `eta` on queued jobs.
- Cloud jobs: `cloud_phase` (`boot|upload|run|download|cleanup`), `provider`, `cloud_est_usd`.
- `review_ready` jobs: `result: {duration, from_height, to_height, from_bytes, to_bytes}` (Review size facts and the restored-moments timeline).
- `POST api/decensor`: accept `cloud: true` for a paid upscale that has no preset. A paid multi-scene submit needs either per-scene quotes (`perScene`) or one total `confirm_usd` (the UI splits one total by duration today, which can drift from the server's per-scene check).

**Runners** (`GET api/runners`, SSE `runners`)
- `prefer` is a list of ops. New optional fields: `gpu`, `vram_gb`, `speed`, `asleep`, `can_wake` (true when `mac` is set), `wake_eta_s`, `queue_eta_s`, `qualities`. `ops` use `decensor|upscale|transcode`; `engines` name `jasna` or `lada` per op (the Jasna-only controls depend on it). `ops` and `engines` may be `null` in the probe result; the UI guards for that.
- New `POST api/runners/wake {url}` → `{ok, eta_s}`; 409 when the runner has no `mac`. Submitting to a sleeping runner that `can_wake` should wake it.
- `POST api/runners` (update by url) must keep the stored token when `token` is omitted (today it is replaced by `WORKER_TOKEN`, so the "prefer" switch would wipe a custom token). It returns one runner object; `POST api/runners/remove` returns `{ok: true}`.
- `POST api/runners/test` → `{ok, error?, ops?, engines?}` (today it returns the raw probe: `online`, `note`).

**Cloud** (`api/cloud/*`)
- `status`: `active[]` needs `scene, preset, usd, started`; `recent[]` needs `scene, preset, gpu, provider, ended` (epoch s). Today they carry `label, spent, dph`.
- `settings`: always return `jasna_license`; a secret with `source: 'env'` is shown read-only. Optional `max_bw_cost` and `max_job_hours` (the two limit rows only appear when the server reports them).
- `offers`: the app reads the real `offers_payload` shape (adapter in `src/lib/api/cloudAdapter.ts`). Wanted, all optional: a per-quality quote or at least `runMin` on every quote (the quality rows show "~" estimates until then), `estimate.detail`, `rented` and `hide_incompatible` query params, per-op runner `speed`.
- A paid run's `confirm_usd` is checked server-side; the UI sends one total (see Jobs above).

**One live status per job (served, 2026-11).** `GET api/jobs` and the `jobs` event carry, per job: `phase` (plain words, e.g. "Restoring in high detail"), `step`/`steps` (the four LTX steps), `detail`, `pass_no`/`passes` (only when more than one, e.g. decensor then upscale), `estimate` (progress or time left is a guess), `progress` (0-1, never goes backwards), `eta`, `fps`, `updated_at` (epoch s) and `idle_s` (seconds since the last sign of life, on the server's clock; only for a running local job, so its absence means "do not show a stall hint"). The card shows "No update for ..." from `STALL_S` (120 s). A log line with `live: true` is a progress bar: the server keeps only the newest one, and the Details view pins it as one line. All of it is optional: an older server shows `message` as the title and no stall hint.

## 4. Mock mode (no backend needed)

`?mock=1` (or `VITE_MOCK=1` at build time) swaps every `api/...` call for fixtures in `src/lib/api/mock/`. Extra switches: `?s=<screen>` (compose, quality, where, cloud, breakdown, browse, details, sortmenu, filters, confirm, jobs, review, replace, runners, cloudset, edge), `?theme=dark|light|<theme id>`, `?prov=2`, `?ts=2` (200% text), `?job=0`, `?job=ltx` (one scripted LTX decensor + upscale job walking every phase in about two minutes; add `&at=copy|scan|load|ltx1..ltx4|upscale-load|upscale|saving|stalled|error` to hold one moment), `?lat=<ms>`, `?scenes=<n>`, `?offers=<n>`, `?shot=1` (hides the mock bar, no animation). The mock chunk is a separate file that real-mode pages never download.

## 5. Cloud picker and browser: what they send and what is missing

Requests (all through the adapter `src/lib/api/cloudAdapter.ts`, which also accepts the simplified shape):
- `GET api/cloud/offers`: `sort`, `limit` (the UI pages by 100; cap is 200), `offset`, and the job: `scene` (one) or `scene_ids` (several), `op` (`decensor+upscale` for `decensor_up`), `preset` (fast → `close-ups`, balanced → `detail`, high and max → `detail-close-ups`; omitted for upscale). The browser adds `max_price`, `max_bw_cost`, `min_reliability`, `verified=any|1`, `datacenter_only` and, only when set, `gpu`, `num_gpus` with `max_gpus`, `countries`, `min_disk_gb`, `min_duration_hours`, `min_cuda`, `min_driver`, `provider` (only when exactly one), `rented=1` (only once `facets.rented` was seen). A single-machine lookup sends `offer_id`, `provider`, `limit=1`.
- `GET api/cloud/quote?scenes&op&preset&offer_id&provider`.

Missing (the UI degrades gracefully; `scene_ids`, `caps`, a per-offer region and `provider` on submit are now served, so ignore those four below): `scene_ids` on offers (a 400 `bad_query` makes the UI say "one scene at a time"); `caps` in the offers payload; a per-offer region; a `rented` flag and `facets.rented`; a per-direction GB split in `estimate.breakdown`; `gpu_mem_bw`; budget reasons in `incompatible_reason`; a hide-can't-run filter and `sort=auto` best fit for a job; an `all` total before filters (estimated client-side); facets counted over the whole market, not only the filtered set; `submit` accepting `provider` with `offer_id` (`startRun` sends only `offer_id`); a 409 `price_changed` that returns the new quote; interruptible and reserved rentals (shown disabled). The confirm dialog quotes the whole selection as one job although the runner rents per scene.
