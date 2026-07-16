<#  Remove the Stashify runner service, firewall rule, and tray autostart.
    Elevated PowerShell. Leaves the venv/models/config in place (delete $Root by hand
    to fully remove).  #>
param([string]$Root = "$env:LOCALAPPDATA\StashifyRunner")
$SvcId = "stashify-runner"
$winsw = "$Root\$SvcId.exe"
if (Test-Path $winsw) { & $winsw stop 2>$null; & $winsw uninstall 2>$null; Write-Host "service removed" }
Get-NetFirewallRule -DisplayName "Stashify Runner" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
$lnk = "$([Environment]::GetFolderPath('Startup'))\Stashify Runner Tray.lnk"
if (Test-Path $lnk) { Remove-Item $lnk -Force }
Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$Root*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Write-Host "uninstalled (kept $Root - delete manually to remove venv/models/config)"
