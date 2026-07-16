<#  Remove the Stashify runner (scheduled task or old WinSW service), firewall
    rule, and tray autostart. Run elevated. Leaves $Root (venv/models/config).  #>
param([string]$Root = "$env:LOCALAPPDATA\StashifyRunner")
$SvcId = "stashify-runner"
$TaskName = "StashifyRunner"

# stop the runner process + task
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like "*StashifyRunner*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# remove any legacy WinSW service
$winsw = "$Root\$SvcId.exe"
if (Test-Path $winsw) { & $winsw stop 2>$null; & $winsw uninstall 2>$null }

Get-NetFirewallRule -DisplayName "Stashify Runner" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
$lnk = "$([Environment]::GetFolderPath('Startup'))\Stashify Runner Tray.lnk"
if (Test-Path $lnk) { Remove-Item $lnk -Force }
Write-Host "uninstalled (kept $Root - delete manually to remove venv/models/config)"
