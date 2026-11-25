<#
  Stashify Windows Runner - installer.

  Sets up compute node #2 (this Windows box) for the Stashify coordinator:
  provisions a Python venv (torch cu124 + SR/video deps), installs ffmpeg, and
  registers the runner to AUTO-START AT BOOT as a scheduled task running AS YOU.

  WHY A BOOT TASK AS YOU (not a service, not SYSTEM): the runner reaches your
  NAS media over SMB. A LocalSystem service authenticates as the *machine
  account*, which the NAS denies; and WinSW's per-user service account is
  unreliable. A task "run whether user is logged on or not" as your own
  account carries your credentials, so SMB works before anyone signs in - which
  is what lets a Wake-on-LAN boot (from sleep or full shutdown) serve jobs with
  nobody at the keyboard. The price: Task Scheduler stores your Windows
  password (in its own protected store). The installer asks for it once, on
  the PC's desktop; re-running it keeps the stored password. The tray icon is a
  second, password-less task that starts at logon.

  Run in an ELEVATED PowerShell on the PC's own desktop (Run as administrator):
      powershell -ExecutionPolicy Bypass -File install.ps1
  Add -SkipDeps to update code/task only (skip the venv/torch/ffmpeg/model steps).
  Add -ResetPassword after you change your Windows password.
#>
param(
  [string]$Root = "$env:LOCALAPPDATA\StashifyRunner",
  [int]   $Port = 8712,
  [string]$Token = "",
  [switch]$SkipDeps,
  [switch]$ResetPassword
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
Copy-Item "$Src\tray-icon.png", "$Src\supervise.ps1", "$Src\tray-launch.ps1" "$Root\app\" -Force
Remove-Item "$Root\app\run-hidden.vbs" -Force -ErrorAction SilentlyContinue   # pre-supervisor launcher
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
# Re-running the installer is the update path, so everything already in
# config.json survives it (jasna_exe, model and temp folders, alert_url ...).
# Only the paths this script has just resolved are refreshed.
if ($existing) {
  foreach ($p in $existing.PSObject.Properties) {
    if ($p.Name -notin "token", "port", "venv_python", "ffmpeg") { $cfg[$p.Name] = $p.Value }
  }
}
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

# --- 4. remove any old WinSW service; register the boot task + the logon tray task ---
$winsw = "$Root\$SvcId.exe"
if (Test-Path $winsw) { & $winsw stop 2>$null | Out-Null; & $winsw uninstall 2>$null | Out-Null; Write-Host "removed old WinSW service" }

# StashifyRunner runs supervise.ps1, which keeps runner.py alive (restart +
# backoff, captured stderr, optional alert). conhost --headless gives it NO
# window without VBScript, which Windows is removing. Two triggers: at STARTUP,
# and a 5-minute repeat that only matters if the supervisor itself died - a
# second copy exits at once (IgnoreNew + its own machine-wide mutex).
# It runs as you with your password stored by Task Scheduler ("run whether
# user is logged on or not"): that is what gives it your SMB credentials
# before anyone signs in. S4U/no-password tasks have no network credentials.
$conhost = "$env:SystemRoot\System32\conhost.exe"
$me = [Security.Principal.WindowsIdentity]::GetCurrent().Name   # not $env:USERDOMAIN: an SSH session reports WORKGROUP
$action = New-ScheduledTaskAction -Execute $conhost -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Root\app\supervise.ps1`" -Root `"$Root`""
$action.WorkingDirectory = "$Root\app"
$atBoot = New-ScheduledTaskTrigger -AtStartup
$ensure = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
# no -WakeToRun: the PC must only wake on purpose (Wake-on-LAN), never for a task
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

$old = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$haveBootTask = $old -and $old.Principal.LogonType -eq "Password" -and
  ($old.Triggers | Where-Object { $_.CimClass.CimClassName -eq "MSFT_TaskBootTrigger" })
if ($haveBootTask -and -not $ResetPassword) {
  # Re-running the installer is the update path. The task only points at
  # app\supervise.ps1, which was just refreshed above, so it needs no rewrite -
  # and Task Scheduler refuses ANY change to a stored-password task without the
  # password (Set-ScheduledTask, schtasks /change, COM all fail with 0x8007052E).
  # Run with -ResetPassword to rewrite the definition itself.
  Write-Host "boot task kept (stored password unchanged; use -ResetPassword to rewrite it)"
} else {
  Write-Host "`nThe runner must start at boot, before anyone signs in, and reach your NAS as you." -ForegroundColor Yellow
  Write-Host "Windows Task Scheduler needs your Windows password for that; it keeps it in its own"
  Write-Host "protected store (the password is not written to any file or log)."
  $registered = $false
  for ($try = 0; $try -lt 3 -and -not $registered; $try++) {
    $cred = $null
    try { $cred = Get-Credential -UserName $me -Message "Stashify runner: Windows password for $env:USERNAME (stored by Task Scheduler so the runner starts at boot, before sign-in)" } catch { throw "password prompt unavailable here ($_). Run install.ps1 in an elevated PowerShell on the PC's own desktop." }
    if (-not $cred) { throw "cancelled - no password entered" }
    # Task Scheduler checks the password itself when the task is registered, so a
    # wrong one is refused here (and the existing task is left untouched).
    try {
      Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $atBoot, $ensure -Settings $settings `
        -User $me -Password $cred.GetNetworkCredential().Password -RunLevel Limited -Force -ErrorAction Stop | Out-Null
      $registered = $true
    } catch { Write-Warning "Task Scheduler refused it ($($_.Exception.Message.Trim())) - try again" }
    $cred = $null
  }
  if (-not $registered) { throw "no accepted password after 3 tries" }
  Write-Host "boot task registered (password stored by Task Scheduler)"
}

# The tray is a window onto the runner, so it belongs to the signed-in desktop:
# its own logon task, no stored password.
$trayAction = New-ScheduledTaskAction -Execute $conhost -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Root\app\tray-launch.ps1`" -Root `"$Root`""
$trayAction.WorkingDirectory = "$Root\app"
$trayPrincipal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "${TaskName}Tray" -Action $trayAction -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $me) `
  -Principal $trayPrincipal -Settings $settings -Force | Out-Null

# --- 5. firewall (LAN) - BEFORE the runner starts, or Windows pops its "allow python?" prompt ---
if (-not (Get-NetFirewallRule -DisplayName "Stashify Runner" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "Stashify Runner" -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private | Out-Null
  Write-Host "firewall: allowed TCP $Port (private networks)"
}
# A port rule is not enough for the prompt: Windows asks per program image. The venv
# python.exe is a launcher; the process that really listens is the base interpreter
# (uv keeps it under a versioned folder AND a minor-version link - both spellings).
Get-NetFirewallRule -DisplayName "Stashify Runner python*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
$pyImages = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
if (Test-Path $py) {
  [void]$pyImages.Add($py)
  # The venv's pyvenv.cfg names the interpreter folder. Read it as text (running python
  # here can be blocked, and some sessions cannot follow uv's folder link). A rule may
  # name a path that is not visible from this session, so no existence checks.
  $home_ = (Get-Content "$Root\.venv\pyvenv.cfg" -ErrorAction SilentlyContinue | Where-Object { $_ -match "^\s*home\s*=" } | Select-Object -First 1) -replace "^\s*home\s*=\s*", ""
  if ($home_) {
    $home_ = $home_.Trim().TrimEnd("\")
    [void]$pyImages.Add("$home_\python.exe")
    # the versioned twin of uv's minor-version link (cpython-3.12-... -> cpython-3.12.13-...)
    Get-ChildItem (Split-Path $home_) -Directory -Filter ((Split-Path $home_ -Leaf) -replace "^(cpython-\d+\.\d+).*", '$1*') -ErrorAction SilentlyContinue |
      ForEach-Object { [void]$pyImages.Add($_.FullName + "\python.exe") }
  }
}
$n = 0
foreach ($img in $pyImages) {
  $n++
  New-NetFirewallRule -DisplayName "Stashify Runner python $n" -Direction Inbound -Action Allow -Program $img -Profile Private | Out-Null
}
Write-Host "firewall: allowed the runner's python ($n image paths, private networks)"

# (re)start now: stop any prior instance holding the port, then launch
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Stop-ScheduledTask -TaskName "${TaskName}Tray" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='powershell.exe'" |
  Where-Object { $_.CommandLine -like "*StashifyRunner*runner.py*" -or $_.CommandLine -like "*StashifyRunner*tray.py*" -or
                 $_.CommandLine -like "*StashifyRunner*supervise.ps1*" -or $_.CommandLine -like "*StashifyRunner*tray-launch.ps1*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 2
Start-ScheduledTask -TaskName $TaskName
if (Get-Process -Name explorer -ErrorAction SilentlyContinue) { Start-ScheduledTask -TaskName "${TaskName}Tray" -ErrorAction SilentlyContinue }
Write-Host "task '$TaskName' started (runs as $env:USERNAME at boot, before sign-in); tray task starts at logon" -ForegroundColor Green

# --- 6. tray: the StashifyRunnerTray logon task starts it; drop the old Startup shortcut ---
Remove-Item "$([Environment]::GetFolderPath('Startup'))\Stashify Runner Tray.lnk" -Force -ErrorAction SilentlyContinue

# --- 6b. keep SSH reachable after every boot (the Windows OpenSSH server installs as Manual start) ---
if (Get-Service sshd -ErrorAction SilentlyContinue) {
  Set-Service sshd -StartupType Automatic
  if ((Get-Service sshd).Status -ne "Running") { Start-Service sshd }
  Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue | Enable-NetFirewallRule
  Write-Host "sshd: start type Automatic, running, firewall rule enabled"
}

# --- 6c. wake ONLY on purpose: Wake-on-LAN magic packet, nothing else; Fast Startup off ---
# Fast Startup (hybrid shutdown) can leave the NIC unarmed for Wake-on-LAN from a
# full shutdown. Keyboards, mice, Wi-Fi, Bluetooth and "pattern match" must not wake it.
$fs = "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power"
if ((Get-ItemProperty $fs).HiberbootEnabled -ne 0) { Set-ItemProperty $fs -Name HiberbootEnabled -Value 0; Write-Host "Fast Startup turned off" }
$wired = Get-NetAdapter -Physical | Where-Object { $_.InterfaceDescription -notmatch "Wi-?Fi|Wireless|Bluetooth|802\.11" }
foreach ($nic in $wired) {
  Set-NetAdapterPowerManagement -Name $nic.Name -WakeOnMagicPacket Enabled -WakeOnPattern Disabled -ErrorAction SilentlyContinue
  Get-NetAdapterAdvancedProperty -Name $nic.Name -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -eq "Wake on Link Settings" -and $_.DisplayValue -ne "Disabled" -and $_.ValidDisplayValues -contains "Disabled" } |
    ForEach-Object { Set-NetAdapterAdvancedProperty -Name $nic.Name -DisplayName $_.DisplayName -DisplayValue "Disabled" }
}
Get-NetAdapter -Physical | Where-Object { $_.InterfaceDescription -match "Wi-?Fi|Wireless|Bluetooth|802\.11" } | ForEach-Object {
  Set-NetAdapterPowerManagement -Name $_.Name -WakeOnMagicPacket Disabled -WakeOnPattern Disabled -ErrorAction SilentlyContinue
}
$wiredDesc = @($wired | ForEach-Object { $_.InterfaceDescription })
powercfg -devicequery wake_armed | Where-Object { $_ -and $_ -notin $wiredDesc -and $_ -notmatch "^No devices" } | ForEach-Object {
  powercfg -devicedisablewake "$_" | Out-Null
  Write-Host "wake disarmed: $_"
}
Write-Host "wake policy: magic packet on $($wiredDesc -join ', ') only"

# --- 7. health ---
Start-Sleep 5
try {
  $t = (Get-Content $cfgPath -Raw | ConvertFrom-Json).token
  $h = Invoke-RestMethod "http://localhost:$Port/health" -Headers @{ "X-Runner-Token" = $t } -TimeoutSec 8
  Write-Host "`nHEALTHY: node=$($h.node) ops=$($h.ops -join '/') encoders(ai=$($h.encoders.ai) transcode=$($h.encoders.transcode))" -ForegroundColor Green
  Write-Host "Dashboard: http://localhost:$Port/  (LAN: http://$($env:COMPUTERNAME):$Port/)"
} catch { Write-Warning "started but /health not answering yet - check $Root\logs\runner.log" }
