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
| `RUNNERS` | — | Optional JSON array of extra compute runners: `[{"name":"desktop","url":"http://IP:8712","token":"...","ops":["upscale","transcode","decensor","decensor+upscale"],"prefer":["upscale"]}]`. Merged with dashboard-managed runners (env wins on duplicate URLs). |
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

Manage the fleet from the dashboard — **⚙ Runners** in the top bar:

- **🔎 Discover on network** — scans your /24 on the runner ports (8711/8712,
  override with `DISCOVER_CIDR` / `DISCOVER_PORTS`) for the unauthenticated
  `/ping` beacon every runner serves, and one-click registers what it finds.
  Discovered runners default to the fleet `WORKER_TOKEN`.
- **+ Add runner** — register by URL with a test-before-add probe; pick which
  ops the node should be *preferred* for.
- Runners added here persist in `RUNNERS_STORE` (mount `/config`); runners from
  the `RUNNERS` env show with an *env* badge and win on duplicate URLs.

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
