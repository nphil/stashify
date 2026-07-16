# Stashify Windows Runner

A **GPU compute node** for Stashify that runs on a Windows desktop and adds a
second machine to the pool. The always-on NAS (Tesla P40) keeps decensoring;
this box takes **upscaling** (on an NVIDIA card, fp16) and **transcoding** (on
the Intel iGPU via QuickSync) — two lanes that run **concurrently** on different
hardware.

It speaks the same HTTP protocol as the NAS runner, so the Stashify coordinator
dispatches to it identically. It translates the container paths the coordinator
sends (`/stuff2/…`, `/scratch/…`) to how this machine reaches the same files
over SMB, does the work, and hands the output back on the shared `/scratch`.

## What it does

| Lane | Hardware | Op | Notes |
|---|---|---|---|
| AI | NVIDIA (e.g. RTX 3080) | `upscale` | SPAN 2× (spandrel), fp16. |
| AI | NVIDIA (Turing+, cc ≥ 7.5) | `decensor`, `decensor+upscale` | [Jasna](https://github.com/Kruk2/jasna) (RF-DETR detection + BasicVSR++ restore, TensorRT). Optional — run `install-jasna.ps1` (~4.1 GB self-contained release; needs NVIDIA driver ≥ 580). First job compiles TensorRT engines (15–60 min, cached). The chain op decensors then SPAN-upscales. |
| Transcode | Intel iGPU | `transcode` | QuickSync HEVC (`hevc_qsv`); target resolution + quality. |

Both lanes are independent queues, so an upscale and a transcode run at the same
time (decensor shares the AI lane and serializes with upscales). A served
dashboard (`http://localhost:8712/`) shows both GPUs' live telemetry, per-lane
jobs (fps/speed/ETA), and controls. A **tray flyout** (PySide6, Kanagawa-themed)
anchors to the tray icon OneDrive-style: node status, both GPU gauges, active
jobs with progress, and a live log tail that updates numbers-only changes
in place (a tqdm stream stays one line). It auto-hides on click-out, or pin it
(⤢) into a draggable floating window.

## Requirements

- Windows 10/11, an NVIDIA GPU (+ optionally an Intel iGPU for the transcode lane)
- [`uv`](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`)
- SMB access to the NAS shares **with credentials saved in your account's
  Credential Manager** (Explorer → connect once with "remember credentials").
- **NVENC needs NVIDIA driver ≥ 610.** On older drivers the runner auto-falls
  back to QuickSync/CPU for encoding (the SR compute still runs on the GPU). AV1
  hardware encode is not available on Ampere/UHD-770 — HEVC is the target.

## Install

**New machine, one file:** download `setup-stashify-runner.ps1` from the
[latest release](https://github.com/nphil/stashify/releases/latest) and run it —
it self-elevates, installs `uv` if missing, downloads the runner payload, and
runs the full installer below. Add `-WithJasna` (and `-JasnaDir "D:\big\drive"`)
to include the decensor engine.

**From a checkout:** run in an **elevated** PowerShell:

```powershell
cd winrunner
powershell -ExecutionPolicy Bypass -File install.ps1          # full install
powershell -ExecutionPolicy Bypass -File install-jasna.ps1    # optional: decensor engine
```

`install.ps1` provisions the venv (torch cu124 + deps), installs ffmpeg,
downloads the SPAN model, writes `config.json` (edit `path_map` if your NAS
shares differ), registers the **`StashifyRunner`** scheduled task running **as
you at logon** (so your saved SMB credentials work — a LocalSystem service
authenticates as the machine account, which the NAS denies; the VBS launcher
keeps it windowless), opens the firewall for the LAN, and starts the tray.
Re-run any time to update. `uninstall.ps1` removes it.

`install-jasna.ps1` downloads the ~4.1 GB self-contained
[Jasna](https://github.com/Kruk2/jasna) release, extracts it (7-Zip, auto-fetched
if missing), points `config.json` at `jasna.exe`, and restarts the runner —
after which the node advertises `decensor` + `decensor+upscale` automatically.
The **first** decensor job compiles TensorRT engines (15–60 min of "still
running" heartbeats; engines are cached next to the model files). 10 GB VRAM is
fine at the default `jasna_max_clip_size` of 90.

## Point the coordinator at it

Easiest: Stashify dashboard → **⚙ Runners** → **🔎 Discover on network** — the
runner serves an unauthenticated `/ping` beacon, so the coordinator finds and
registers it in one click (persisted in its `/config` mount). Or declare it in
the worker's `RUNNERS` env (env entries win on duplicate URLs):

```
RUNNERS=[{"name":"desktop-3080","url":"http://<desktop-lan-ip>:8712",
          "token":"<same as WORKER_TOKEN>",
          "ops":["upscale","transcode","decensor","decensor+upscale"],
          "prefer":["upscale","transcode","decensor","decensor+upscale"]}]
```

The coordinator health-checks every candidate and routes by capability,
**skipping this node whenever the desktop is off** — a Docker runner on the NAS
(`LADA_URL`) stays the always-on fallback. Declaring `decensor` here is safe
before Jasna is installed: routing also requires the node's live `/health` to
advertise the op. Give the desktop a **reserved DHCP lease** so the URL stays
valid.

## Files

- `runner.py` — the HTTP service: 2-lane scheduler, path translation, per-lane
  process control (psutil), dual-GPU telemetry, serves the dashboard.
- `upscale_cli.py` / `transcode_cli.py` / `decensor_cli.py` — the lane workers
  (subprocesses); `decensor_cli.py` wraps `jasna.exe` and normalizes its tqdm
  output into the runner's progress protocol.
- `webui/index.html` — the served node dashboard.
- `tray.py` — the tray flyout (PySide6).
- `install.ps1` / `install-jasna.ps1` / `uninstall.ps1` — lifecycle;
  `setup-stashify-runner.ps1` — the one-file release bootstrap.
- `config.example.json` — the installer writes the real one to
  `%LOCALAPPDATA%\StashifyRunner\config.json`.
