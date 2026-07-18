<#
  Stashify Windows Runner - installer.

  Sets up compute node #2 (this Windows box) for the Stashify coordinator:
  provisions a Python venv (torch cu124 + SR/video deps), installs ffmpeg, and
  registers the runner to AUTO-START as a scheduled task running AS YOU.

  WHY A LOGON TASK (not a service): the runner reaches your NAS media over SMB.
  A LocalSystem service authenticates as the *machine account*, which the NAS
  denies; and WinSW's per-user service account is unreliable. A scheduled task
  running as your own account uses your saved NAS credentials, needs no stored
  password, and starts when you log in (fine for a desktop you use). To instead
  run at boot before login you'd need a service with your password saved.

  Run in an ELEVATED PowerShell (Run as administrator):
      powershell -ExecutionPolicy Bypass -File install.ps1
  Add -SkipDeps to update code/task only (skip the venv/torch/ffmpeg/model steps).
#>
param(
  [string]$Root = "$env:LOCALAPPDATA\StashifyRunner",
  [int]   $Port = 8712,
  [string]$Token = "",
  [switch]$SkipDeps
)
$ErrorActionPreference = "Stop"
$SvcId = "stashify-runner"
$TaskName = "StashifyRunner"
$Src   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)   # PS 5.1 Set-Content adds a BOM that breaks json.load
function Write-Text($path, $text) { [System.IO.File]::WriteAllText($path, $text, $Utf8NoBom) }

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw "Run this in an elevated PowerShell (Run as administrator)."
}

Write-Host "== Stashify runner install ==" -ForegroundColor Cyan
New-Item -ItemType Directory -Force $Root, "$Root\app", "$Root\models", "$Root\logs", "$Root\tmp" | Out-Null

# --- 1. code ---
Copy-Item "$Src\*.py" "$Root\app\" -Force
Copy-Item "$Src\webui" "$Root\app\" -Recurse -Force
Copy-Item "$Src\tray-icon.png", "$Src\run-hidden.vbs" "$Root\app\" -Force
Write-Host "code -> $Root\app"

# --- 2. deps ---
if (-not $SkipDeps) {
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw "uv not found. Install: winget install astral-sh.uv" }
  if (-not (Test-Path "$Root\.venv")) { uv venv --python 3.12 "$Root\.venv" }
  $py = "$Root\.venv\Scripts\python.exe"
  Write-Host "installing torch (cu124) + deps (a few GB, one-time)..."
  uv pip install --python $py torch torchvision --index-url https://download.pytorch.org/whl/cu124
  # Fast mosaic scan for the live preview: direct TensorRT on jasna's pre-built rfdetr
  # engine (~20x faster inference than DirectML). Pinned to the exact version jasna
  # compiled the engine with; the DLL-bearing wheel is only on NVIDIA's index. The
  # scan degrades to DirectML automatically if this is absent, so failure is non-fatal.
  uv pip install --python $py --extra-index-url https://pypi.nvidia.com "tensorrt-cu12==10.16.1.11" 2>$null
  if ($LASTEXITCODE -ne 0) { Write-Warning "tensorrt install failed - live-preview scan will use DirectML (slower)" }
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
$py  = "$Root\.venv\Scripts\python.exe"
$pyw = "$Root\.venv\Scripts\pythonw.exe"
$ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
if (-not $ffmpeg) { $ffmpeg = (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe" | Select-Object -First 1).FullName }

# --- 3. config.json (BOM-free; keep existing token unless one is passed) ---
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
if ($existing -and $existing.path_map) { $cfg.path_map = $existing.path_map }
Write-Text $cfgPath ($cfg | ConvertTo-Json -Depth 6)
icacls $cfgPath /inheritance:r /grant:r "${env:USERNAME}:(R,W)" "Administrators:(F)" | Out-Null
Write-Host "config -> $cfgPath"
try { New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -Value 1 -PropertyType DWord -Force | Out-Null } catch {}

# --- 3b. reserve the port so WinNAT/Hyper-V can't grab it on reboot ---
# Windows reserves blocks of ports for WinNAT/Hyper-V at boot, and those ranges
# reshuffle on every reboot; if a block covers $Port the runner's bind fails with
# WinError 10013 and it shows offline. An *administered* persistent exclusion
# keeps $Port reserved for us across reboots and stays bindable by the runner.
$portRow = "^\s*$Port\s+$Port\b"
if (netsh int ipv4 show excludedportrange tcp | Select-String $portRow) {
  Write-Host "port $Port already reserved (persistent exclusion present)"
} else {
  netsh int ipv4 add excludedportrange protocol=tcp startport=$Port numberofports=1 store=persistent 2>&1 | Out-Null
  if (-not (netsh int ipv4 show excludedportrange tcp | Select-String $portRow)) {
    # $Port sits inside a live WinNAT dynamic reservation - bounce winnat to carve it out
    Write-Host "reserving port $Port (bouncing WinNAT)..."
    Stop-Service winnat -Force -ErrorAction SilentlyContinue
    netsh int ipv4 add excludedportrange protocol=tcp startport=$Port numberofports=1 store=persistent 2>&1 | Out-Null
    Start-Service winnat -ErrorAction SilentlyContinue
  }
  if (netsh int ipv4 show excludedportrange tcp | Select-String $portRow) {
    Write-Host "reserved TCP $Port for the runner (persistent; survives reboots)"
  } else {
    Write-Warning "could not reserve TCP $Port - if the runner shows WinError 10013, reserve it manually"
  }
}

# --- 4. remove any old WinSW service; register the logon task ---
$winsw = "$Root\$SvcId.exe"
if (Test-Path $winsw) { & $winsw stop 2>$null | Out-Null; & $winsw uninstall 2>$null | Out-Null; Write-Host "removed old WinSW service" }

# Launch via a VBS wrapper so there is NO console window (uv's venv pythonw.exe
# trampolines to the console python.exe, which would flash/hold a terminal).
$wscript = "$env:SystemRoot\System32\wscript.exe"
$vbs = "$Root\app\run-hidden.vbs"
$me = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $wscript -Argument "//nologo `"$vbs`" `"$py`" `"$Root\app\runner.py`""
$action.WorkingDirectory = "$Root\app"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

# (re)start now: stop any prior instance holding the port, then launch
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like "*StashifyRunner*runner.py*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 2
Start-ScheduledTask -TaskName $TaskName
Write-Host "task '$TaskName' registered + started (runs as $env:USERNAME at logon)" -ForegroundColor Green

# --- 5. firewall (LAN) ---
if (-not (Get-NetFirewallRule -DisplayName "Stashify Runner" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "Stashify Runner" -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private | Out-Null
  Write-Host "firewall: allowed TCP $Port (private networks)"
}

# --- 6. tray at login (also windowless, via the VBS wrapper) ---
$lnk = "$([Environment]::GetFolderPath('Startup'))\Stashify Runner Tray.lnk"
$sc = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$sc.TargetPath = $wscript
$sc.Arguments = "//nologo `"$vbs`" `"$py`" `"$Root\app\tray.py`""
$sc.WorkingDirectory = "$Root\app"; $sc.Save()
Start-Process $wscript -ArgumentList "//nologo `"$vbs`" `"$py`" `"$Root\app\tray.py`""
Write-Host "tray installed to Startup + launched"

# --- 7. health ---
Start-Sleep 5
try {
  $t = (Get-Content $cfgPath -Raw | ConvertFrom-Json).token
  $h = Invoke-RestMethod "http://localhost:$Port/health" -Headers @{ "X-Runner-Token" = $t } -TimeoutSec 8
  Write-Host "`nHEALTHY: node=$($h.node) ops=$($h.ops -join '/') encoders(ai=$($h.encoders.ai) transcode=$($h.encoders.transcode))" -ForegroundColor Green
  Write-Host "Dashboard: http://localhost:$Port/  (LAN: http://$($env:COMPUTERNAME):$Port/)"
} catch { Write-Warning "started but /health not answering yet - check $Root\logs\runner.log" }
