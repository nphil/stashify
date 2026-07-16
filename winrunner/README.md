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
| AI | NVIDIA (e.g. RTX 3080) | `upscale` | SPAN 2× (spandrel), fp16. `decensor` is a future lane (needs a Lada Windows build). |
| Transcode | Intel iGPU | `transcode` | QuickSync HEVC (`hevc_qsv`); target resolution + quality. |

Both lanes are independent queues, so an upscale and a transcode run at the same
time. A served dashboard (`http://localhost:8712/`) shows both GPUs' live
telemetry, per-lane jobs (fps/speed/ETA), and controls; a **tray app** gives
quick status + Pause/Resume/Open.

## Requirements

- Windows 10/11, an NVIDIA GPU (+ optionally an Intel iGPU for the transcode lane)
- [`uv`](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`)
- SMB access to the NAS shares **with credentials saved in your account's
  Credential Manager** (Explorer → connect once with "remember credentials").
- **NVENC needs NVIDIA driver ≥ 610.** On older drivers the runner auto-falls
  back to QuickSync/CPU for encoding (the SR compute still runs on the GPU). AV1
  hardware encode is not available on Ampere/UHD-770 — HEVC is the target.

## Install (as a service)

Run in an **elevated** PowerShell:

```powershell
cd winrunner
powershell -ExecutionPolicy Bypass -File install.ps1
```

It provisions the venv (torch cu124 + deps), installs ffmpeg, downloads the SPAN
model, writes `config.json` (edit `path_map` if your NAS shares differ),
registers the **`stashify-runner`** service via WinSW **running as you** (so it
can reach SMB — a LocalSystem service authenticates as the machine account,
which the NAS denies), opens the firewall for the LAN, and adds the tray to
Startup. Re-run any time to update. `uninstall.ps1` removes it.

> The service runs under your Windows account because that's whose saved NAS
> credentials work over SMB. For a hardened setup, create a dedicated local
> account, save the NAS creds into *its* Credential Manager, and pass it to
> `install.ps1`'s credential prompt.

## Point the coordinator at it

On the NAS worker, add this box to the `RUNNERS` registry (JSON array) — the
coordinator health-checks each runner and routes by capability, **skipping this
node whenever the desktop is off**:

```
RUNNERS=[{"name":"desktop-3080","url":"http://<desktop-lan-ip>:8712",
          "token":"<same as WORKER_TOKEN>",
          "ops":["upscale","transcode"],"prefer":["upscale","transcode"]}]
```

Keep `LADA_URL` pointed at the P40 for decensoring. With `prefer` set, upscale
and transcode route to this box when it's up and fall back to the P40 otherwise.
Give the desktop a **reserved DHCP lease** so the URL stays valid.

## Files

- `runner.py` — the HTTP service: 2-lane scheduler, path translation, per-lane
  process control (psutil), dual-GPU telemetry, serves the dashboard.
- `upscale_cli.py` / `transcode_cli.py` — the lane workers (subprocesses).
- `webui/index.html` — the served node dashboard.
- `tray.py` — the login tray companion.
- `install.ps1` / `uninstall.ps1` — service lifecycle.
- `config.example.json` — copy to `%LOCALAPPDATA%\StashifyRunner\config.json`.
