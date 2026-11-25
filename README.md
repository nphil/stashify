# Stashify

**Stashify — upscale & decensor for Stash.**

Decensor or upscale a scene **on demand from the Stash UI**: open a scene, hit
a button, watch live progress (stats, log, even a live video feed of the
in-progress render), **review** the result, then **replace the original** — all
without tagging or batch jobs, and with nothing installed on your Stash/unRAID
host.

> **Scope:** operates on your own legal adult media in your self-hosted Stash
> library. Mosaic removal is a *generative guess* at lost detail, not recovery
> of real pixels — results vary with mosaic strength.

## How it works

A coordinator/runner design — a thin coordinator, **one or more GPU compute
runners**, and a UI plugin, all talking over HTTP:

1. **Stashify worker** (`Dockerfile` → `server.py`, image
   `ghcr.io/nphil/stashify`, ~131 MB) — a **thin coordinator**: HTTP API,
   dashboard, Stash GraphQL integration, job queue, preview/replace logic, and
   **capability-based routing** across every registered runner (it health-checks
   candidates, skips offline nodes, and prefers idle + preferred ones). No GPU,
   no CUDA, no models.
2. **Compute runners** — any mix of:
   - **Linux/Docker runner** (`Dockerfile.lada` → `lada_runner.py`, image
     `ghcr.io/nphil/stashify-runner`):
     [Lada](https://github.com/ladaapp/lada) mosaic removal (YOLO11 detection +
     BasicVSR++ temporal restoration), **SPAN upscaling** (via
     [spandrel](https://github.com/chaiNNer-org/spandrel)), and NVENC encoding.
     Runs on cards as old as Pascal (Tesla P40).
   - **Windows runner** (`winrunner/`, [one-file installer](https://github.com/nphil/stashify/releases/latest)):
     turns a Windows desktop into a second node with **two concurrent lanes** —
     AI (NVIDIA: SPAN upscale fp16 + **[Jasna](https://github.com/Kruk2/jasna)
     mosaic removal**, Turing or newer) and transcode (Intel iGPU QuickSync
     HEVC). Ships a Kanagawa-themed **tray flyout** (anchored popup with live
     GPU gauges, job progress, and a coalescing log tail; undockable).

   Results hand off through a shared `/scratch` mount (SMB for the Windows box).
3. **Stash UI plugin** (`stashify.yml` + `stashify.js` + `stashify.css`) — adds
   a panel to the scene page. Your browser calls the worker directly; progress,
   the review player, and Replace/Discard all render right there. The worker
   also serves a full dashboard (e.g. `https://stashify.nateshome.net`) with an
   engine picker, live stats (frames/fps/ETA + GPU telemetry), a live-log
   terminal, before/after frame previews, cancel/pause/resume, and a **Runners
   panel** (add nodes manually or auto-discover them on the LAN).

```
Scene page → [Decensor] → worker queues job → routed to the best runner:
      decensor          → Jasna (Windows, Turing+) or Lada (Docker, Pascal+)
      upscale/transcode → SPAN fp16 / QuickSync lanes
           → preview scene → review in panel/dashboard → [Replace original]  (in place)
                                                       → [Discard]           (delete preview)
```

Replace overwrites the original file **in place with no backup**, so the scene
keeps its tags, o-counter, markers, and play history; Stash just re-fingerprints
the new media.

> **History:** earlier versions bundled DeepMosaics + Real-ESRGAN in a single
> ~6 GB container; both were removed in favor of Lada (decensor) and SPAN
> (upscale) on the dedicated runner.

---

## Images (GHCR)

Both images are built by **GitHub Actions on every push to `main`**
(`.github/workflows/docker-publish.yml`) and pushed to GitHub Container
Registry, tagged `latest` + the commit `sha`; pushing a `v*` tag adds semver
tags.

| Image | Contents |
|---|---|
| `ghcr.io/nphil/stashify` | Worker/coordinator (API + dashboard), ~131 MB |
| `ghcr.io/nphil/stashify-runner` | GPU runner: Lada + SPAN + NVENC ffmpeg |

To `docker pull` them, either make the ghcr packages **public** (GitHub →
Packages → package settings → Change visibility) or log in once:
`docker login ghcr.io -u <your-user>`.

> **unRAID version checks need Docker-media-type manifests.** unRAID's
> Docker-tab update checker sends an `Accept` header without
> `application/vnd.oci.image.manifest.v1+json`, and ghcr returns 404 for a
> media type the client didn't accept — so images pushed as bare OCI
> manifests show **"not available"** under Version forever. The publish
> workflow therefore pushes with `oci-mediatypes=false`. Public packages are
> checked anonymously; a private package also works once the server has done
> `docker login ghcr.io`.

---

## 1) Worker + runner on unRAID

### Prerequisites

- unRAID **Nvidia Driver** plugin (Community Apps) — driver + NVIDIA Container
  Toolkit. Pascal cards (e.g. Tesla P40) are fully supported — see the runner
  notes below.
- Your media share + an output folder inside a Stash library path.

### Option A: unRAID templates

Proper Docker-tab templates ship in the repo: **`unraid/stashify.xml`** (worker)
and **`unraid/stashify-runner.xml`** (runner). Drop them into
`/boot/config/plugins/dockerMan/templates-user/` on your flash drive, then add
both containers from the Docker tab (**Add Container** → pick the template) and
fill in the paths/secrets.

If the Docker tab's **Version** column shows "not available", the pulled
image was published with OCI media types — see [Images (GHCR)](#images-ghcr);
images built by the current workflow check fine.

### Option B: docker compose

1. Clone the repo (e.g. `git clone https://github.com/nphil/stashify /mnt/user/appdata/stashify`).
2. Edit `docker-compose.yml`: set `STASH_URL`, put `STASH_API_KEY` and
   `WORKER_TOKEN` in a `.env` file next to it, and fix the volume mounts (see
   the gotcha below). The compose defines two services: `stashify` (worker,
   container `Stashify`, port **8710**) and `runner` (container
   `Stashify-Runner`, GPU, port 8711 internal). The worker reaches the runner
   at `LADA_URL=http://runner:8711`.
3. `docker compose up -d` (pulls from GHCR) or `docker compose up -d --build`
   to build locally.

### ⚠️ The one gotcha: path matching

Stash stores **absolute paths as its own container sees them**. The worker and
runner must see the same files at the **same path**. If Stash maps
`/mnt/user/media → /data`, mount the media identically in both:

```yaml
volumes:
  - /mnt/user/media:/data     # identical to Stash's mapping (ro is fine for the runner)
```

`OUTPUT_DIR` must be inside a Stash library folder, at a path both Stash and
the worker agree on (e.g. `/data/stashify`).

Additionally, worker and runner share a **scratch** mount at the same container
path (`/scratch` by default, `LADA_SCRATCH`). Keep it **outside** your Stash
libraries so half-written intermediates never get scanned as scenes.

### Models

Nothing to download by hand: on first start the runner fetches the Lada weights
into its persisted `/models` volume. The default SPAN upscale checkpoint
(`2xLiveActionV1_SPAN`) lives there too.

### Worker environment variables

| Var | Default | Notes |
|---|---|---|
| `STASH_URL` | — | e.g. `http://192.168.1.50:9999`. Required. |
| `STASH_API_KEY` | — | Stash → Settings → Security. Needed if auth is on. |
| `WORKER_TOKEN` | — | Optional shared secret; set the same value in the plugin's *Worker Token*. |
| `PORT` | `8710` | HTTP API + dashboard port (publish it). |
| `RUN_MODE` | `server` | `server` = UI button + dashboard; `worker` = tag batch. |
| `BACKEND` | `lada` | `lada` (decensor) \| `upscale` (SPAN only) \| `command`. |
| `POST_UPSCALE` | `false` | `lada` backend: chain decensor → SPAN upscale on the runner. |
| `LADA_URL` | — | Runner base URL, e.g. `http://runner:8711`. Required for `lada`/`upscale`. |
| `LADA_TOKEN` | — | Shared secret for the runner (compose reuses `WORKER_TOKEN`). |
| `LADA_SCRATCH` | `/scratch` | Handoff dir both worker & runner mount at the same path. |
| `LADA_DETECTION_MODEL` | `v4-fast` | or `v4-accurate` (better detection, slower), `v2`. |
| `LADA_RESTORATION_MODEL` | `basicvsrpp-v1.2` | Lada restoration model. |
| `LADA_FP16` | `false` | **Keep false on Pascal** (no usable fp16). |
| `LADA_ENCODER` | — | Empty = runner default (NVENC probe result). |
| `LADA_UPSCALE_MODEL` | — | Empty = runner default (`2xLiveActionV1_SPAN`). |
| `OUTPUT_DIR` | `/data/stashify` | Inside a Stash library path. |
| `RUNNERS` | — | Optional JSON array of extra compute runners: `[{"name":"desktop","url":"http://IP:8712","token":"...","ops":["upscale","transcode","decensor","decensor+upscale"],"prefer":["upscale"]}]`. Merged with dashboard-managed runners (env wins on duplicate URLs). Add `"mac":"AA:BB:CC:DD:EE:FF"` to a runner that sleeps: when a job would go to it, the coordinator sends a Wake-on-LAN packet and waits `RUNNER_WAKE_SECS` for it before routing elsewhere. Waking a PC that is asleep works from the default bridge network; one that is powered off needs the coordinator on host networking (the broadcast can't leave a bridge). |
| `RUNNER_WAKE_SECS` | `120` | How long a woken runner gets to come online. |
| `RUNNERS_STORE` | `/config/runners.json` | Where dashboard-added runners persist — mount `/config` to keep them across recreations. |
| `TRIGGER_TAG` | `Decensor` | Tag that marks scenes for batch mode. |
| `DONE_TAG` | `Decensored` | Applied after processing. |
| `IMPORT_RESULT` | `true` | Scan the result into Stash + copy metadata. |
| `GPU_ID` | `0` | `-1` = CPU. |
| `COMMAND_TEMPLATE` | — | For `BACKEND=command`: `{input}` `{output_dir}` `{gpu}`. |

### Runner environment variables

| Var | Default | Notes |
|---|---|---|
| `PORT` | `8711` | Runner HTTP port (only the worker needs to reach it). |
| `LADA_TOKEN` | — | Must match the worker's `LADA_TOKEN` (sent as `X-Lada-Token`). |
| `LADA_DEVICE` | `cuda` | Torch device. |
| `GPU_ID` | `0` | GPU index for telemetry + CUDA. |
| `LADA_MODEL_WEIGHTS_DIR` | `/models` | Persisted weights volume. |
| `LADA_DEFAULT_ENCODER` | probe | Override the NVENC startup probe (e.g. `libx264`). |
| `UPSCALE_MODEL` | `/models/2xLiveActionV1_SPAN_490000.pth` | SPAN/Compact/ESRGAN checkpoint (any spandrel-loadable SR model). |

---

## More compute: adding runners & discovery

The coordinator routes each job by **capability**: every runner advertises its
ops (`decensor`, `upscale`, `decensor+upscale`, `transcode`), the coordinator
health-checks the candidates, skips offline/paused nodes, and picks an idle one
(a runner's `prefer` list breaks ties). If a preferred node is busy or off, the
job just goes to the next capable runner.

### Dashboard

The coordinator serves a single-page dashboard at its root (`/`): browse and
search the Stash library, select scenes, pick a quality and where to run
(a local runner or a rented cloud GPU), watch jobs live, review results, and
manage runners and cloud limits. It follows the system light/dark setting and
has 42 selectable palettes. Live updates arrive over one Server-Sent Events
stream (`GET /api/events`); if a proxy blocks it the page falls back to
polling. The app lives in `web/` (Svelte 5 + Vite) and is built into the image
by the Dockerfile; to work on it: `cd web && npm ci && npm run dev` (add
`?mock=1` to the URL to run it against built-in fixtures with no server).
`WEBUI_DIR` points the server at a different build folder.

Manage the fleet from the dashboard — the **runner chip** in the top bar:

- **🔎 Discover on network** — scans your /24 on the runner ports (8711/8712,
  override with `DISCOVER_CIDR` / `DISCOVER_PORTS`) for the unauthenticated
  `/ping` beacon every runner serves, and one-click registers what it finds.
  Discovered runners default to the fleet `WORKER_TOKEN`.
- **+ Add runner** — register by URL with a test-before-add probe; pick which
  ops the node should be *preferred* for.
- Runners added here persist in `RUNNERS_STORE` (mount `/config`); runners from
  the `RUNNERS` env show with an *env* badge and win on duplicate URLs.

## Cloud GPU (Vast.ai) — paid, optional, off by default

For a scene that deserves the best quality, Stashify can **rent a GPU on
[Vast.ai](https://vast.ai) for that one job**, run Jasna on it, hand the result to the
normal review / replace flow, and **destroy the machine**. Nothing is ever rented unless
you ask for it, job by job.

The renting itself (market search, creating and destroying machines, the reaper, the spend
ledger) is done by the separate, provider-neutral package
[**hireling**](https://github.com/nphil/hireling), installed from a tagged release (pinned in
`requirements.txt`; the Dockerfile installs from there). Vast.ai is the only provider that
works today. Stashify decides what to run and what it may cost.

> **Privacy trade-off — read first.** The video is uploaded to a machine owned by a
> stranger (a Vast host), not to anything you control. The copy is deleted when the
> machine is destroyed, but you cannot audit the host. Don't send scenes you wouldn't
> hand to a third party. Local runners never leave your network.

**Set up once (dashboard → top bar → ☁ Cloud):**

1. Paste your **Vast API key** and your **Jasna licence code**. They are separate,
   write-only fields: stored in `/config/cloud.json` (mode 0600), never shown again
   (the dashboard only says *set · …abcd*). Add prepaid credit on Vast first. The Jasna
   licence is only needed by the Jasna operations; a cloud transcode runs without one.
2. Set the three limits: **max price per hour** (default $1.00), **cap per job**
   ($3.00) and **cap per month** ($10.00). Three more limits are saved the same way
   (`POST /api/cloud/settings`) or by environment variable: **max job hours** (default 3,
   how long any one rental may live), **max traffic price** (default $0.02 per GB: a host
   that charges more for internet traffic is skipped) and **datacenter machines only**
   (default off).
3. **Test key** calls Vast and shows your credit balance.

**What can run in the cloud** (`op` in the request; `decensor` when it is left out):

| `op` | What happens | Also needs |
|---|---|---|
| `decensor` | Jasna restoration | `cloud_preset` |
| `decensor+upscale` | restoration, then an upscale of the result, in the same rental | `cloud_preset`, `upscale`, `upscale_height` |
| `upscale` | upscale only. Refused when the video is already that tall | `upscale`, `upscale_height` |
| `transcode` | re-encode with ffmpeg on the rented GPU: `hevc_nvenc`, else `h264_nvenc`, else `libx265`. Same request fields and progress line as the Windows runner's `transcode_cli.py` | optional `transcode_height` (144–4320, default: keep the size), `transcode_quality` (1–51, default 24) |

`upscale` is `span` (the default), `realplksr` or `rtx-super-res`; `upscale_height` is
`1080`, `1440` or `2160` (the default). Anything else (custom commands, other
upscale models) is refused (`bad_op`, `bad_upscale`, `bad_upscale_height`, `bad_transcode`).

**Which machines can do what.** One rule per operation, in `cloud_caps.py`, used for the
market list, the automatic pick and the quote. A machine that cannot do the job stays in the
list, greyed, with the reason (*No video encoder*, *Needs an RTX card*, *Needs 10 GB (this has 8.2)*).
There is no card allow-list: an RTX 3080 qualifies like any other RTX card.

| Operation | Card | VRAM | Driver | Download speed |
|---|---|---|---|---|
| Jasna ones (`decensor`, `upscale`, `decensor+upscale`) | RTX class | 10 GB | 580+ | 200 Mbit/s |
| `transcode` | any card with a video encoder (NVENC): GeForce, RTX, L4, A10, T4... Not A100/H100/V100-style compute cards | 2 GB | 570+ | 100 Mbit/s |

**The transcode box.** The same plain `ubuntu:24.04` image as the Jasna jobs. At boot it
downloads one static ffmpeg build (BtbN, GPL, NVENC + x265 in one file, about 150 MB) into
`/root/ffmpeg`, uploads `transcode_cli.py`, checks that the encoder really starts, then runs it.
The quote adds the upload from home, the download of the result, the ffmpeg download and the
disk. Its time comes from a stated rule of thumb: NVENC at about 8× real time for 1080p video.

**Use it:** select scenes → *Decensor*, then choose **Cloud quality (paid)**. It is
*off* every time the page loads (never remembered) and is not part of the runner
picker. Exactly three presets exist:

| Preset | Jasna options |
|---|---|
| Sharper close-ups | `--restoration-model-name ltx --ltx-large-canvas` |
| High detail | `--restoration-model-name ltx-8step` |
| High detail + sharper close-ups | `--restoration-model-name ltx-8step --ltx-large-canvas` |

Choosing one shows a **quote** (the GPU and its $/hour, estimated minutes, estimated
total, what is left of this month's cap). The Run button then shows the price and
needs a **confirm click every time**.

**Which machine.** By default Stashify picks one for you (the cheapest machine that passes
the operation's rule above, datacenter machines first; up to three tries). A request can also choose:

- `hardware`: `cheapest`, `fastest`, `best_value` or `most_reliable`. Each names **one**
  machine for that scene, worked out from that job's own estimate;
- `offer_id` (with `provider`): one exact machine from the market browser below;
- `gpu`, `provider` and `cloud_filters` (a JSON object: price, reliability, VRAM, GPU
  count...) to narrow the automatic search. Filters only ever tighten what the operation and your
  limits already require.

A chosen machine is rented **exactly, or not at all**: if somebody took it meanwhile,
nothing is rented and the job is refused (`offer_gone`); it is never swapped for another
one. A `hardware` choice is turned into one machine when the job is queued.

**The estimate.** Per machine: setup (apt, Jasna or ffmpeg, model download) + upload and
download of the video at home speed + GPU time, padded by 20%. Jasna jobs use Jasna's published
speed table (a card outside it is placed by its raw compute and the estimate says so);
a transcode uses the stated 8× real time for 1080p, slower for taller video. The estimate shows
the upload and the download as separate minutes (`up_min`, `down_min`). The price is compute +
disk + traffic (each shown separately, with the GB moved), rounded up to a whole cent. The
real cost is whatever the rental accrues. Upscale and transcode speeds are rough guesses
(not measured on rented machines yet), and every estimate that uses them says so.

**API** (all routes need the worker token, `X-Decensor-Token`):

| Route | What it does |
|---|---|
| `GET /api/cloud/offers` | The market behind the enabled providers: hireling's filters, sorts (`sort=`) and paging (`limit`, `offset`), `facets`, `hardware_presets`, each offer `compatible` or not (with the reason). Add `scene=<id>` (and `op`, `preset`, `upscale`, `upscale_height`, `transcode_height`, `transcode_quality`) to get, per offer, what **that job** would take and cost: `estimate{minutes, est_usd, setup_min, up_min, down_min, transfer_min, processing_min, disk_gb, breakdown{compute, storage, bandwidth, bw_down, bw_up, gb_down, gb_up}, assumptions}`. Add `hours=<n>` instead of a scene to cost a plain rental. |
| `GET /api/cloud/quote?scenes=5,6&preset=…` | What the dashboard shows before a run: one quote per scene, with the machine, the estimate and, when refused, `refusal{code, message}`. Takes the same plan keys (`op`, `upscale`, `upscale_height`, `transcode_height`, `transcode_quality`, `hardware`, `offer_id`, `provider`, `gpu`). |
| `POST /api/decensor` | `cloud: true` (or `cloud_preset`) + `confirm_usd` + the plan keys queues one paid job. The job shows `cloud_op`, `cloud_gpu`, `cloud_offer_id`, `cloud_provider`. |
| `GET /api/cloud/status` | Spend, running machines, the last rentals, and every provider with its prepaid `credit`. |
| `GET`/`POST /api/cloud/settings`, `POST /api/cloud/test` | The key and the limits (`max_price`, `job_cap`, `month_cap`, `max_bw_cost`, `max_job_hours`, `datacenter_only`); the key check. |

**Safety rules** — enforced by the server, not just the page:

- Refused when: no key or no licence; the estimate is over the per-job cap; the estimate
  needs longer than **max job hours**; this month's spend so far + queued paid jobs + the
  estimate is over the monthly cap; an upscale would not change the size; the machine you
  picked is gone; or the price moved well above what you confirmed. Queueing the job and the
  run itself both look the market up afresh (the quote on the page may be up to two minutes
  old), so a stale page cannot rent a machine that is no longer offered.
- While running, the cost is tracked continuously. At the per-job cap the job is
  stopped, nothing partial is kept, the machine is destroyed, and the job says so.
- A machine is destroyed on **every** path: success, failure, cancel, crash. In automatic mode a bad
  machine (unreachable, no working video encoder, failed install) is dropped and the
  next offer is tried, at most 3.
- Every rental is appended to `/config/cloud-ledger.jsonl` (job, scene, operation, preset, GPU,
  start/end, $/hour, actual $). The Cloud dialog shows month-to-date and the last 10.
- **Reaper:** at startup and every few minutes, any machine labelled `stashify-<8 hex>` on your
  Vast account that this coordinator doesn't own (after a crash or restart), or that has
  outlived its time limit (**max job hours**, default 3 h), is destroyed. So **use one Vast account per
  Stashify coordinator** — a second one on the same account would have its machines
  destroyed. The Vast key can't be changed while a machine is running.

Files written before hireling (`cloud.json`, `cloud-ledger.jsonl`, `cloud-instances.json`)
are read and written in the same format: a record without a `provider` is a Vast one.

Needs `openssh-client` + `rsync` (in the image) and a persistent `/config` mount (in
the unRAID template). Environment variables are optional **overrides** that win over
the dashboard: `VAST_API_KEY`, `JASNA_LICENSE_CODE`, `VAST_MAX_PRICE`, `VAST_JOB_CAP`,
`VAST_MONTH_CAP`; advanced: `VAST_GPUS`, `VAST_MIN_VRAM_GB`, `VAST_MIN_RELIABILITY`,
`VAST_DATACENTER_ONLY`, `VAST_MAX_JOB_HOURS`, `VAST_DISK_GB`, `VAST_MAX_BW_COST`,
`JASNA_CLOUD_IMAGE`, `CLOUD_DIR`, `VAST_SSH_DIR`.

## Windows runner (second machine: RTX + iGPU)

`winrunner/` turns a Windows desktop into an extra compute node — see
[winrunner/README.md](winrunner/README.md) for details. **Install on a new
machine with one file:** grab `setup-stashify-runner.ps1` from the
[latest release](https://github.com/nphil/stashify/releases/latest) and run:

```powershell
powershell -ExecutionPolicy Bypass -File setup-stashify-runner.ps1 -WithJasna -JasnaDir "D:\Models\jasna"
```

It self-elevates, installs `uv` if needed, downloads the runner payload, and
sets up everything: Python venv (torch cu124), ffmpeg, the SPAN model, a config
wizard (NAS paths + token), an auto-start logon task (runs **as you**, so your
saved SMB credentials work), and the firewall rule. `-WithJasna` adds the
[Jasna](https://github.com/Kruk2/jasna) decensor engine (~4.1 GB self-contained
release; NVIDIA compute ≥ 7.5 i.e. Turing+, driver ≥ 580, 10 GB VRAM is
enough at the default clip size) — its first job compiles TensorRT engines
(15–60 min, cached; subsequent jobs start instantly). `-JasnaDir` keeps the
~8 GB engine + model cache off `C:`.

The runner exposes two independent lanes, so an upscale (NVIDIA) and a
transcode (Intel QuickSync) run **concurrently**; decensor shares the AI lane.
A tray flyout (click the tray icon) shows node status, both GPUs, active jobs
with progress, and a live log tail that coalesces progress ticks into a single
updating line; it auto-hides on click-out and can be pinned as a floating
window. Register the node afterwards via dashboard → Runners → Discover.

---

## 2) Stash UI plugin

Install via Stash's Plugin Manager — one paste:

1. **Settings → Plugins → Available Plugins → Add Source**, and set the URL to:
   ```
   https://nphil.github.io/stashify/index.yml
   ```
2. Install **Stashify** from the new source, and make sure it's enabled.
3. Under **Settings → Plugins → Stashify**, set:
   - **Worker URL** — the worker as reachable from your **browser**, e.g.
     `http://192.168.1.50:8710` or `https://stashify.nateshome.net`.
   - **Worker Token** — match the container's `WORKER_TOKEN` (if you set one).

(Manual alternative: clone `https://github.com/nphil/stashify` into a subfolder
of your Stash `plugins` directory and hit **Reload plugins** — Stash reads
`stashify.yml`/`stashify.js`/`stashify.css`; the worker files in the same repo
are ignored.)

## Use

0. First time: on any scene, hit **Test connection** in the panel — it pings the
   worker and shows the backend/GPU and whether the token is accepted, so you can
   confirm the Worker URL, token, and CORS before running a real job.
1. Open any scene. A **Stashify** panel appears at the bottom-right.
2. Hit **Decensor this scene** → watch the progress bar. The dashboard shows
   the same job with full stats, live log, and a live preview; you can
   cancel/pause/resume from there.
3. When it's done, the panel shows a **preview player**. Review it.
4. **Replace original** (overwrites the file in place, keeps all metadata) or
   **Discard** (deletes the preview).

---

## Engines

| Backend | Role |
|---|---|
| `lada` | Mosaic **removal + temporal restoration**. On the Docker runner this is [ladaapp/lada](https://github.com/ladaapp/lada) (YOLO11 detection + BasicVSR++); on the Windows runner the same op runs [Jasna](https://github.com/Kruk2/jasna) (RF-DETR detection + BasicVSR++ via TensorRT — faster and higher quality, Turing+ only). Optionally chains a SPAN upscale pass (`POST_UPSCALE`). |
| `upscale` | **Upscale only** — SPAN (default `2xLiveActionV1_SPAN`, a live-action 2× model) or any spandrel-loadable checkpoint. No mosaic removal. |
| `transcode` | **Re-encode only** (HEVC, target resolution/quality) — runs on the Windows runner's Intel QuickSync lane. |
| `command` | Any external CLI — e.g. a **TecoGAN** or **JavPlayer** wrapper — via `COMMAND_TEMPLATE`. |

Pick per job with the dashboard's engine dropdown; the env vars just set
defaults. Which physical node executes an op is the routing's job — see
*adding runners & discovery* above.

### The runner image and the Tesla P40

Lada's stock Docker image is CUDA 12.8 and **does not run on Pascal** (Tesla
P40/GTX 10xx: "no kernel image"). `Dockerfile.lada` builds Lada from source with
its `nvidia-legacy` extra — torch 2.8.0 from the **cu126** index, which still
ships `sm_61` kernels — and runs fp32 (`--no-fp16`). Model weights download on
first start into the `/models` volume, results hand off via the shared
`/scratch` mount, and one GPU job runs at a time.

**NVENC, latest-first with automatic Pascal fallback.** Current ffmpeg/PyAV
builds are compiled against NVENC API **13.1**, which requires driver ≥ 610 —
and 580 is the *final* driver branch for Pascal, so stock builds can never
NVENC-encode on a P40. The image therefore ships two PyAV builds: the stock
wheel (latest bundled libav) and one linked against an ffmpeg 7.1 built with the
`sdk/13.0` headers (P40-compatible; newer drivers run it too). On every start
the entrypoint *actually opens* an `hevc_nvenc` encoder to probe: latest works →
keep latest; otherwise swap to the legacy build; neither → `libx264` on CPU.
So the same image uses the newest ffmpeg on a modern card and still hardware-
encodes on the P40. Override per job (`encoder`) or via `LADA_DEFAULT_ENCODER`.

---

## Optional: batch / bare-metal modes

- **Tag batch** — set `RUN_MODE=worker` (and `POLL_INTERVAL`) to process every
  scene carrying the trigger tag (`Decensor` by default) instead of the
  on-demand button.
- **In-Stash plugin tasks** — `plugin.py` still provides Tasks (*Decensor
  Tagged Scenes*, *Import Cleaned Files From Folder*) for bare-metal setups.
  Not needed for the container flow.

---

## Troubleshooting

- **Panel doesn't appear** — plugin not enabled, or you're not on a `/scenes/…`
  page. Reload plugins; hard-refresh the browser.
- **"Set the Worker URL…"** — fill in Worker URL under the plugin settings.
- **Worker unreachable / CORS** — the browser must reach `Worker URL` directly.
  The API sends `Access-Control-Allow-Origin: *`. If your Stash sets a strict
  Content-Security-Policy (uncommon on self-hosted), put the worker behind the
  same origin via a reverse proxy.
- **`bad token` (401)** — `WORKER_TOKEN` and the plugin's *Worker Token* differ.
- **Runner unreachable** — check `LADA_URL` (compose default
  `http://runner:8711`) and that `LADA_TOKEN` matches on both containers.
- **`file not found` for a scene** — media isn't mounted at the same path Stash
  uses. Fix the volume mapping (worker **and** runner).
- **`no scene found` after processing** — `OUTPUT_DIR` isn't inside a Stash
  library path.
- **Slow on Pascal** — expected: Lada's temporal model runs fp32 on a P40.
  Confirm `LADA_FP16=false` (fp16 on Pascal is catastrophically slow), use
  `LADA_DETECTION_MODEL=v4-fast`, and check the dashboard's GPU panel to
  confirm the card is actually being used.
- **CPU-encoded output on a modern card** — check the runner start log for the
  NVENC probe result; force with `LADA_DEFAULT_ENCODER`.
- **Replace with a different container** — if the output is `.mp4` but the
  original was e.g. `.avi`, the file is still written to the original path (to
  preserve scene identity) and re-scanned; the extension may then not match the
  container. Most players/Stash handle it, but it's logged as a warning.
- Originals are never touched until you hit **Replace**; the worker only writes
  previews into `OUTPUT_DIR` otherwise.
