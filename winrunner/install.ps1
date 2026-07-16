<#
  Stashify Windows Runner — installer.

  Sets up compute node #2 (this Windows box) for the Stashify coordinator:
  provisions a Python venv (torch cu124 + SR/video deps), installs ffmpeg,
  registers the runner as an auto-starting Windows SERVICE via WinSW, opens the
  firewall, and wires the tray app to launch at login.

  WHY IT RUNS AS YOU (not LocalSystem): the runner reaches your NAS media over
  SMB. A LocalSystem service authenticates as the *machine account*, which the
  NAS denies — so the service must run under a user account whose Credential
  Manager holds the NAS creds. This script defaults to the current user.

  Run in an ELEVATED PowerShell (Run as administrator):
      powershell -ExecutionPolicy Bypass -File install.ps1

  Re-run any time to update code + restart the service.
#>
param(
  [string]$Root   = "$env:LOCALAPPDATA\StashifyRunner",
  [int]   $Port   = 8712,
  [string]$Token  = "",                    # blank -> read from an existing config.json
  [switch]$SkipDeps                        # skip venv/torch/ffmpeg (code/service update only)
)
$ErrorActionPreference = "Stop"
$SvcId = "stashify-runner"
$Src   = Split-Path -Parent $MyInvocation.MyCommand.Path   # the winrunner/ folder

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw "Run this in an elevated PowerShell (Run as administrator)."
}

Write-Host "== Stashify runner install ==" -ForegroundColor Cyan
New-Item -ItemType Directory -Force $Root, "$Root\app", "$Root\models", "$Root\logs", "$Root\tmp" | Out-Null

# --- 1. code ---
Copy-Item "$Src\*.py" "$Root\app\" -Force
Copy-Item "$Src\webui" "$Root\app\" -Recurse -Force
Copy-Item "$Src\tray-icon.png" "$Root\app\" -Force
Write-Host "code -> $Root\app"

# --- 2. deps: venv + torch (cu124) + reqs; ffmpeg; SPAN model ---
if (-not $SkipDeps) {
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw "uv not found. Install: winget install astral-sh.uv" }
  if (-not (Test-Path "$Root\.venv")) { uv venv --python 3.12 "$Root\.venv" }
  $py = "$Root\.venv\Scripts\python.exe"
  Write-Host "installing torch (cu124) + deps (a few GB, one-time)..."
  uv pip install --python $py torch torchvision --index-url https://download.pytorch.org/whl/cu124
  uv pip install --python $py -r "$Src\requirements.txt"

  if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -and
      -not (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Gyan.FFmpeg*" -ErrorAction SilentlyContinue)) {
    Write-Host "installing ffmpeg (Gyan full build)..."
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements --silent
  }
  $model = "$Root\models\2xLiveActionV1_SPAN_490000.pth"
  if (-not (Test-Path $model)) {
    Write-Host "downloading SPAN upscale model..."
    Invoke-WebRequest "https://raw.githubusercontent.com/jcj83429/upscaling/f73a3a02874360ec6ced18f8bdd8e43b5d7bba57/2xLiveActionV1_SPAN/2xLiveActionV1_SPAN_490000.pth" -OutFile $model
  }
}
$py     = "$Root\.venv\Scripts\python.exe"
$ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
if (-not $ffmpeg) { $ffmpeg = (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe" | Select-Object -First 1).FullName }

# --- 3. config.json (keep existing token unless one was passed) ---
$cfgPath = "$Root\config.json"
$existing = if (Test-Path $cfgPath) { Get-Content $cfgPath -Raw | ConvertFrom-Json } else { $null }
if (-not $Token) { $Token = if ($existing) { $existing.token } else { (Read-Host "Runner token (match the coordinator's WORKER_TOKEN)") } }
$cfg = [ordered]@{
  node_name = $env:COMPUTERNAME; port = $Port; token = $Token
  path_map = @(
    @{ prefix = "/stuff2";  local = "\\192.168.1.69\Stuff" },
    @{ prefix = "/stuff";   local = "\\192.168.1.69\Download\torrents\Stuff" },
    @{ prefix = "/scratch"; local = "\\192.168.1.69\appdata\stashify\scratch" }
  )
  lanes = @{ ai = $true; transcode = $true }
  ai_encoder = "auto"; transcode_encoder = "auto"
  venv_python = $py; ffmpeg = $ffmpeg
  upscale_model = "$Root\models\2xLiveActionV1_SPAN_490000.pth"
  ai_fp16 = $true; ai_gpu_index = 0; copy_local = $false; local_temp = "$Root\tmp"
}
if ($existing -and $existing.path_map) { $cfg.path_map = $existing.path_map }  # preserve user edits
$cfg | ConvertTo-Json -Depth 6 | Set-Content $cfgPath -Encoding utf8
icacls $cfgPath /inheritance:r /grant:r "$($env:USERNAME):(R,W)" "Administrators:(F)" | Out-Null
Write-Host "config -> $cfgPath (token-locked)"

# --- 4. WinSW service (runs as YOU, for SMB access) ---
$winsw = "$Root\$SvcId.exe"
if (-not (Test-Path $winsw)) {
  Invoke-WebRequest "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe" -OutFile $winsw
}
$cred = Get-Credential -UserName "$env:USERDOMAIN\$env:USERNAME" -Message "Windows account to run the runner service as (needs your NAS SMB creds saved). Your own account is fine."
$xml = @"
<service>
  <id>$SvcId</id>
  <name>Stashify Windows Runner</name>
  <description>GPU compute node for Stashify (upscale on NVIDIA, transcode on iGPU).</description>
  <executable>$py</executable>
  <arguments>-u "$Root\app\runner.py"</arguments>
  <workingdirectory>$Root\app</workingdirectory>
  <env name="STASHIFY_RUNNER_CONFIG" value="$cfgPath" />
  <env name="PYTHONUNBUFFERED" value="1" />
  <startmode>Automatic</startmode>
  <delayedAutoStart>true</delayedAutoStart>
  <depend>LanmanWorkstation</depend>
  <serviceaccount>
    <username>$($cred.UserName)</username>
    <password>$($cred.GetNetworkCredential().Password)</password>
    <allowservicelogon>true</allowservicelogon>
  </serviceaccount>
  <onfailure action="restart" delay="10 sec" />
  <onfailure action="restart" delay="30 sec" />
  <resetfailure>1 hour</resetfailure>
  <log mode="roll-by-size-time"><sizeThreshold>10240</sizeThreshold><pattern>yyyyMMdd</pattern></log>
  <logpath>$Root\logs</logpath>
  <stoptimeout>20 sec</stoptimeout>
</service>
"@
$xmlPath = "$Root\$SvcId.xml"
$xml | Set-Content $xmlPath -Encoding utf8
icacls $xmlPath /inheritance:r /grant:r "$($env:USERNAME):(R)" "Administrators:(F)" "SYSTEM:(F)" | Out-Null

& $winsw stop  2>$null | Out-Null
& $winsw uninstall 2>$null | Out-Null
Start-Sleep 1
& $winsw install
& $winsw start
Write-Host "service '$SvcId' installed + started" -ForegroundColor Green

# --- 5. firewall (LAN only) so the coordinator can reach the runner ---
if (-not (Get-NetFirewallRule -DisplayName "Stashify Runner" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "Stashify Runner" -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $Port -Profile Private | Out-Null
  Write-Host "firewall: allowed TCP $Port (private networks)"
}

# --- 6. tray at login ---
$startup = [Environment]::GetFolderPath("Startup")
$lnk = "$startup\Stashify Runner Tray.lnk"
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = "$Root\.venv\Scripts\pythonw.exe"
$sc.Arguments = "`"$Root\app\tray.py`""
$sc.WorkingDirectory = "$Root\app"
$sc.IconLocation = "$Root\app\tray-icon.png"
$sc.Save()
Start-Process "$Root\.venv\Scripts\pythonw.exe" -ArgumentList "`"$Root\app\tray.py`""
Write-Host "tray installed to Startup + launched"

Start-Sleep 4
try { $h = Invoke-RestMethod "http://localhost:$Port/health" -TimeoutSec 8
  Write-Host "`nHEALTHY: node=$($h.node) ops=$($h.ops -join '/') encoders(ai=$($h.encoders.ai), transcode=$($h.encoders.transcode))" -ForegroundColor Green
  Write-Host "Dashboard: http://localhost:$Port/  (or http://$($env:COMPUTERNAME):$Port/ on your LAN)"
} catch { Write-Warning "service started but /health not answering yet — check $Root\logs" }
