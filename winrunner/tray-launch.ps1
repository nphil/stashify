<#
  Stashify runner tray launcher - the StashifyTray task runs this at LOGON
  (interactive session, no stored password). The runner itself starts at boot
  from supervise.ps1; the tray is only a window onto it, so it belongs to the
  signed-in desktop.

  It waits for the network first for the same reason supervise.ps1 does
  (Smart App Control checks the unsigned python.exe against Microsoft's cloud),
  starts tray.py unless one is already running, and stays alive for as long as
  the tray does (Task Scheduler would otherwise end the tray with the task).
#>
param([string]$Root = "$env:LOCALAPPDATA\StashifyRunner")
$ErrorActionPreference = "Continue"

$py   = "$Root\.venv\Scripts\python.exe"
$app  = "$Root\app"
$log  = "$Root\logs\supervisor.log"
New-Item -ItemType Directory -Force "$Root\logs" | Out-Null
function Note($msg) { Add-Content $log ("{0} tray: {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg) }

$mutex = New-Object System.Threading.Mutex($false, "Local\StashifyRunnerTrayLauncher")
try { if (-not $mutex.WaitOne(0)) { exit 0 } }
catch [System.Threading.AbandonedMutexException] { }

for ($i = 0; $i -lt 30; $i++) {
  try { [System.Net.Dns]::GetHostAddresses("www.microsoft.com") | Out-Null; break } catch { Start-Sleep 3 }
}

$running = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like "*$app\tray.py*" }
if ($running) { exit 0 }

try {
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $py
  $psi.Arguments = "`"$app\tray.py`""
  $psi.WorkingDirectory = $app
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $p = [System.Diagnostics.Process]::Start($psi)
  Note "started (pid $($p.Id))"
  $p.WaitForExit()
  Note "exited (code $($p.ExitCode))"
} catch { Note "not started: $_" }
