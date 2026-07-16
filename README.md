# Decensor for Stash

Decensor a scene **on demand from the Stash UI**: open a scene, hit a button,
watch live progress, **review** the result, then **replace the original** — all
without tagging or batch jobs. DeepMosaics (mosaic removal) and Real-ESRGAN
(restore/upscale) run in **one Docker container**, so nothing is installed on
your Stash/unRAID host.

> **Scope:** operates on your own legal adult media in your self-hosted Stash
> library. Mosaic removal is a *generative guess* at lost detail, not recovery
> of real pixels — results vary with mosaic strength.

## How it works

Two pieces that talk over HTTP:

1. **Worker container** (`Dockerfile` → `server.py`) — bundles DeepMosaics +
   Real-ESRGAN + CUDA and exposes a small HTTP API. It does the GPU work and
   talks to Stash's GraphQL API.
2. **Stash UI plugin** (`decensor.yml` + `decensor.js` + `decensor.css`) — adds
   a panel to the scene page. Your browser calls the worker directly; progress,
   the review player, and Replace/Discard all render right there.

```
Scene page → [Decensor] → worker runs DeepMosaics→Real-ESRGAN → preview scene
           → review in panel → [Replace original]  (overwrites file in place)
                              → [Discard]           (deletes the preview)
```

Replace overwrites the original file **in place with no backup**, so the scene
keeps its tags, o-counter, markers, and play history; Stash just re-fingerprints
the new media.

---

## 1) Worker container (unRAID)

### What's inside, and the Tesla P40

DeepMosaics + Real-ESRGAN + CUDA + all Python deps, tuned for the **NVIDIA Tesla
P40**:

- **CUDA 11.6 / torch 1.13.1** — needs only driver **≥ 450.80**, so it runs on
  effectively any unRAID NVIDIA driver (newer drivers are backward compatible).
  That's why the P40's "limited driver support" isn't a problem here.
- **Real-ESRGAN forced to `--fp32`** — Pascal fp16 is ~1/64 speed and can NaN;
  fp32 is both faster and correct. Keep `REALESRGAN_FP32=true`.

### Prerequisites

- unRAID **Nvidia Driver** plugin (Community Apps) — driver + NVIDIA Container
  Toolkit. The P40 (Pascal) is supported by current drivers.
- Your media share + an output folder inside a Stash library path.

### Run it

1. Clone this repo to your box (e.g. `git clone https://github.com/nphil/stash-decensor /mnt/user/appdata/decensor`).
2. Edit `docker-compose.yml`: set `STASH_URL`, `STASH_API_KEY`, an optional
   `WORKER_TOKEN`, and the volume mounts (see the gotcha below).
3. `docker compose up -d --build` (or the `docker run` line at the bottom of the
   compose file; on unRAID's Docker tab add `--runtime=nvidia` in **Extra
   Parameters** and publish port **8710**).

### ⚠️ The one gotcha: path matching

Stash stores **absolute paths as its own container sees them**. The worker must
see the same files at the **same path**. If Stash maps `/mnt/user/media → /data`,
mount the worker's media the same way:

```yaml
volumes:
  - /mnt/user/media:/data     # identical to Stash's mapping
```

`OUTPUT_DIR` must be inside a Stash library folder, at a path both containers
agree on (e.g. `/data/decensored`).

### The DeepMosaics models

Real-ESRGAN weights are baked in. DeepMosaics `clean` mode needs **two** weights
from its Google-Drive `pretrained_models` folder, so on **first run** the
container fetches both into the persisted `/models` volume:

- `clean_youknow_video.pth` — the mosaic-removal model (`MODEL_PATH`).
- `mosaic_position.pth` — locates the mosaic. **Required:** without it beside the
  clean model, DeepMosaics falls back to an interactive `input()` prompt and the
  headless container aborts the job.

If Drive rate-limits, set `DEEPMOSAICS_MODEL_URL` / `MOSAIC_POSITION_MODEL_URL` to
direct links, or drop **both** `.pth` files into `/models` yourself (from the
[DeepMosaics models folder](https://drive.google.com/open?id=1LTERcN33McoiztYEwBxMuRjjgxh4DEPs)).

### Environment variables

| Var | Default | Notes |
|---|---|---|
| `STASH_URL` | — | e.g. `http://192.168.1.50:9999`. Required. |
| `STASH_API_KEY` | — | Stash → Settings → Security. Needed if auth is on. |
| `WORKER_TOKEN` | — | Optional shared secret; set the same value in the plugin's *Worker Token*. |
| `PORT` | `8710` | HTTP API port (publish it). |
| `RUN_MODE` | `server` | `server` = UI button; `worker` = tag batch. |
| `BACKEND` | `deepmosaics` | `deepmosaics` \| `realesrgan` \| `lada` \| `command`. |
| `POST_UPSCALE` | `true` | Real-ESRGAN after the backend (best quality; skipped for `lada`). |
| `LADA_URL` | — | Lada runner base URL, e.g. `http://lada:8711`. Enables the `lada` backend + dashboard engine picker. |
| `LADA_TOKEN` | — | Shared secret for the runner (compose reuses `WORKER_TOKEN`). |
| `LADA_SCRATCH` | `/scratch` | Handoff dir both worker & runner mount at the same path. |
| `LADA_DETECTION_MODEL` | `v4-fast` | or `v4-accurate` (better detection, slower). |
| `REALESRGAN_MODEL` | `realesr-animevideov3` | or `realesr-general-x4v3`, `RealESRGAN_x4plus`. |
| `REALESRGAN_SCALE` | `2` | Upscale factor. |
| `REALESRGAN_FP32` | `true` | **Keep true on the P40.** |
| `REALESRGAN_TILE` | `0` | Set e.g. `512` if you OOM on 4K. |
| `GPU_ID` | `0` | `-1` = CPU. |
| `OUTPUT_DIR` | `/data/decensored` | Inside a Stash library path. |
| `MASK_THRESHOLD` | `64` | DeepMosaics detection sensitivity 0–255. |

---

## 2) Stash UI plugin

Because this is a **private** repo, Stash's "Add Source" (Plugin Manager) can't
be used — that needs a public index URL. Install by cloning into Stash's plugins
directory instead:

1. Clone this repo into a subfolder of your Stash `plugins` directory
   (Settings → System → Application Paths shows where that is):
   ```bash
   cd <stash>/plugins && git clone https://github.com/nphil/stash-decensor
   ```
   Stash reads `decensor.yml`/`decensor.js`/`decensor.css`; the worker files in
   the same repo are ignored. (This is also the clone you build the worker from.)
2. **Settings → Plugins → Reload plugins**, and make sure **Decensor** is
   enabled.
3. Under **Settings → Plugins → Decensor**, set:
   - **Worker URL** — the worker as reachable from your **browser**, e.g.
     `http://192.168.1.50:8710`.
   - **Worker Token** — match the container's `WORKER_TOKEN` (if you set one).

## Use

0. First time: on any scene, hit **Test connection** in the panel — it pings the
   worker and shows the backend/GPU and whether the token is accepted, so you can
   confirm the Worker URL, token, and CORS before running a real job.
1. Open any scene. A **Decensor** panel appears at the bottom-right.
2. Hit **Decensor this scene** → watch the progress bar (DeepMosaics, then
   Real-ESRGAN).
3. When it's done, the panel shows a **preview player**. Review it.
4. **Replace original** (overwrites the file in place, keeps all metadata) or
   **Discard** (deletes the preview).

---

## Best-quality guidance

| Backend | Role |
|---|---|
| `deepmosaics` | Mosaic **removal** ([HypoX64/DeepMosaics](https://github.com/HypoX64/DeepMosaics)). Fast on the P40. |
| `lada` | Mosaic **removal + temporal restoration** ([ladaapp/lada](https://github.com/ladaapp/lada): YOLO11 detection + BasicVSR++). Best quality; runs on a separate runner container. Slow on Pascal (fp32). |
| `realesrgan` | **Restore/upscale** ([xinntao/Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN)). Does *not* remove mosaics. |
| `command` | Any external CLI — **TecoGAN**, a **JavPlayer** wrapper, etc. |

**Recommended:** `lada` when you can afford the runtime (temporal model = less
flicker, better reconstruction), `deepmosaics` **+ Real-ESRGAN upscale**
(`POST_UPSCALE=true`, default) for fast batch work. Pick per job with the
dashboard's engine dropdown.

### The Lada runner (`Dockerfile.lada`)

Lada's stock Docker image is CUDA 12.8 and **does not run on Pascal** (Tesla
P40/GTX 10xx: "no kernel image"). `Dockerfile.lada` builds Lada from source with
its `nvidia-legacy` extra — torch 2.8.0 from the **cu126** index, which still
ships `sm_61` kernels — and runs fp32 (`--no-fp16`). The compose `lada` service
wraps it in a small HTTP runner (`lada_runner.py`) the worker dispatches to;
model weights download on first start into `lada-models/` (~130 MB) and results
hand off via the shared `/scratch` mount. One Lada job runs at a time.

**NVENC, latest-first with automatic Pascal fallback.** Current ffmpeg/PyAV
builds are compiled against NVENC API **13.1**, which requires driver ≥ 610 —
and 580 is the *final* driver branch for Pascal, so stock builds can never
NVENC-encode on a P40. The image therefore ships two PyAV builds: the stock
wheel (latest bundled libav) and one linked against an ffmpeg built with the
`sdk/13.0` headers (P40-compatible; newer drivers run it too). On every start
the entrypoint *actually opens* an `hevc_nvenc` encoder to probe: latest works →
keep latest; otherwise swap to the legacy build; neither → `libx264` on CPU.
So the same image uses the newest ffmpeg on a modern card and still hardware-
encodes on the P40. Override per job (`encoder`) or via `LADA_DEFAULT_ENCODER`.

---

## Optional: batch / bare-metal modes

- **Tag batch** — set `RUN_MODE=worker` (and `POLL_INTERVAL`) to process every
  scene carrying the trigger tag instead of the on-demand button.
- **In-Stash plugin tasks** — if Stash runs bare-metal with Python + the ML
  tools installed, `plugin.py` still provides Tasks (*Decensor Tagged Scenes*,
  *…Upscale*, *Import Cleaned Files From Folder*). Not needed for the container
  flow.

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
- **`file not found` for a scene** — media isn't mounted at the same path Stash
  uses. Fix the volume mapping.
- **`no scene found` after processing** — `OUTPUT_DIR` isn't inside a Stash
  library path.
- **Very slow Real-ESRGAN** — confirm `REALESRGAN_FP32=true`; fp16 on Pascal is
  catastrophically slow. **OOM on 4K** — set `REALESRGAN_TILE=512`.
- **Replace with a different container** — if the decensored output is `.mp4`
  but the original was e.g. `.avi`, the file is still written to the original
  path (to preserve scene identity) and re-scanned; the extension may then not
  match the container. Most players/Stash handle it, but it's logged as a
  warning.
- Originals are never touched until you hit **Replace**; the worker only writes
  previews into `OUTPUT_DIR` otherwise.
