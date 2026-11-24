<#
  Stashify runner supervisor - keeps runner.py alive for as long as you are
  signed in. The scheduled task launches THIS (windowless, via conhost
  --headless), not python directly, because a bare launcher loses three things:

    - restarts: the runner is relaunched after any exit, forever, with a
      backoff (5 s doubling to 5 min) so a broken install can't spin the CPU;
    - evidence: python's stderr is kept, so a crash before runner.py's own
      logging is up (missing interpreter, import error) still leaves a reason
      in logs\supervisor.log;
    - an alert: after 3 quick failures in a row it posts once to the optional
      "alert_url" in config.json (any ntfy-style URL that accepts a POST body).
    - no sign-in popup: it waits for the network before the first start.
      Windows Smart App Control asks Microsoft's cloud about the unsigned
      python.exe; with no network yet (the first seconds after sign-in) it
      blocks the launch and shows an error. The tray is started from here
      for the same reason, not from the Startup folder.

  One supervisor per session: a second copy exits at once, which is what lets
  the task also fire every few minutes as a "make sure it is running" check.
#>
param([string]$Root = "$env:LOCALAPPDATA\StashifyRunner")
$ErrorActionPreference = "Continue"

$py     = "$Root\.venv\Scripts\python.exe"
$app    = "$Root\app"
$logDir = "$Root\logs"
$log    = "$logDir\supervisor.log"
New-Item -ItemType Directory -Force $logDir | Out-Null

function Note($msg) {
  if ((Test-Path $log) -and (Get-Item $log).Length -gt 1MB) { Move-Item $log "$log.1" -Force }
  Add-Content $log ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg)
}

$mutex = New-Object System.Threading.Mutex($false, "Local\StashifyRunnerSupervisor")
try { if (-not $mutex.WaitOne(0)) { exit 0 } }
catch [System.Threading.AbandonedMutexException] { }   # previous supervisor was killed: the mutex is ours

function Send-Alert($msg) {
  try {
    $url = (Get-Content "$Root\config.json" -Raw | ConvertFrom-Json).alert_url
    if ($url) { Invoke-RestMethod -Uri $url -Method Post -Body $msg -TimeoutSec 10 | Out-Null }
  } catch { Note "alert not sent: $_" }
}

Note "supervisor started (pid $PID)"
for ($i = 0; $i -lt 30; $i++) {
  try { [System.Net.Dns]::GetHostAddresses("www.microsoft.com") | Out-Null; break } catch { Start-Sleep 3 }
}

function Start-Hidden($script) {
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $py
  $psi.Arguments = "`"$app\$script`""
  $psi.WorkingDirectory = $app
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $psi.RedirectStandardError = ($script -eq "runner.py")
  [System.Diagnostics.Process]::Start($psi)
}

$fails = 0
while ($true) {
  # A runner left over from a killed supervisor still holds the port; the new
  # one could never bind. Nothing else here is entitled to run runner.py.
  Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*$app\runner.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  if (-not (Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
            Where-Object { $_.CommandLine -like "*$app\tray.py*" })) {
    try { Start-Hidden "tray.py" | Out-Null } catch { Note "tray not started: $_" }
  }

  $started = Get-Date
  $code = $null; $err = ""
  if (-not (Test-Path $py)) {
    $err = "python not found at $py - re-run install.ps1 to rebuild the venv"
  } else {
    try {
      $p = Start-Hidden "runner.py"
      $err = $p.StandardError.ReadToEnd()    # returns when the runner exits
      $p.WaitForExit()
      $code = $p.ExitCode
    } catch { $err = "could not start python: $_" }
  }

  $ran = [int]((Get-Date) - $started).TotalSeconds
  $tail = if ($err.Length -gt 3000) { $err.Substring($err.Length - 3000) } else { $err }
  Note "runner exited (code $code) after $ran s. stderr: $($tail.Trim())"

  if ($ran -ge 60) { $fails = 0 } else { $fails++ }
  if ($fails -eq 3) {
    $last = ($tail.Trim() -split "`n" | Select-Object -Last 1)
    Send-Alert "Stashify runner on $env:COMPUTERNAME keeps failing to start: $last"
  }
  $delay = [Math]::Min(300, 5 * [Math]::Pow(2, [Math]::Min($fails, 6)))
  Start-Sleep -Seconds $delay
}
