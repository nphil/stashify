<#
  Stashify Windows Runner - one-file setup bootstrap.

  Download this single file onto a fresh Windows machine and run it; it does
  the rest: self-elevates, installs uv (via winget) if missing, downloads the
  runner payload from the GitHub release, and runs the full installer
  (Python venv + torch cu124, ffmpeg, SPAN upscale model, config wizard,
  auto-start scheduled task, firewall rule).

      powershell -ExecutionPolicy Bypass -File setup-stashify-runner.ps1

  Options are passed through to the installer:
      -Root  <dir>    install dir            (default %LOCALAPPDATA%\StashifyRunner)
      -Port  <n>      runner port            (default 8712)
      -Token <secret> fleet WORKER_TOKEN     (prompted by the installer if empty)
      -WithJasna      also install the Jasna decensor engine (~4.1 GB download;
                      NVIDIA GPU with compute >= 7.5 and driver >= 580 required)
      -JasnaDir <dir> where Jasna + its models go (default <Root>\jasna; put this
                      on a big drive, e.g. "F:\AI Stuff\Models\jasna")
#>
param(
  [string]$Root = "$env:LOCALAPPDATA\StashifyRunner",
  [int]   $Port = 8712,
  [string]$Token = "",
  [switch]$WithJasna,
  [string]$JasnaDir = ""
)
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$PayloadUrl = "https://github.com/nphil/stashify/releases/latest/download/stashify-winrunner.zip"

# --- self-elevate (install.ps1 needs admin for the task + firewall rule) ---
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
  Write-Host "elevating..." -ForegroundColor Yellow
  $args = @("-ExecutionPolicy","Bypass","-File","`"$($MyInvocation.MyCommand.Path)`"",
            "-Root","`"$Root`"","-Port",$Port)
  if ($Token)     { $args += @("-Token","`"$Token`"") }
  if ($WithJasna) { $args += "-WithJasna" }
  if ($JasnaDir)  { $args += @("-JasnaDir","`"$JasnaDir`"") }
  Start-Process powershell -Verb RunAs -ArgumentList $args
  exit
}

Write-Host "== Stashify runner bootstrap ==" -ForegroundColor Cyan

# --- uv (python/venv manager the installer uses) ---
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "installing uv..." -ForegroundColor Cyan
  winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements --silent
  $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
              [Environment]::GetEnvironmentVariable("Path","User")
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv installed but not on PATH yet - open a NEW terminal and re-run this script"
  }
}

# --- fetch + unpack the runner payload ---
$work = Join-Path $env:TEMP ("stashify-setup-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force $work | Out-Null
Write-Host "downloading runner payload..." -ForegroundColor Cyan
& curl.exe -L --fail --retry 3 -o "$work\payload.zip" $PayloadUrl
if ($LASTEXITCODE -ne 0) { throw "payload download failed: $PayloadUrl" }
Expand-Archive "$work\payload.zip" "$work\payload" -Force
$src = (Get-ChildItem "$work\payload" -Recurse -Filter "install.ps1" | Select-Object -First 1).DirectoryName
if (-not $src) { throw "payload did not contain install.ps1" }

# --- run the real installer ---
$instArgs = @{ Root = $Root; Port = $Port }
if ($Token) { $instArgs.Token = $Token }
& "$src\install.ps1" @instArgs

# --- optional: Jasna decensor engine ---
if ($WithJasna) {
  $jArgs = @{}
  if ($JasnaDir) { $jArgs.InstallDir = $JasnaDir }
  & "$src\install-jasna.ps1" @jArgs
}

Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "Bootstrap complete. Check the tray icon / http://localhost:$Port/" -ForegroundColor Green
if (-not $WithJasna) {
  Write-Host "To add mosaic-removal later: re-run with -WithJasna, or run install-jasna.ps1." -ForegroundColor DarkGray
}
Write-Host "Then register this runner in the Stashify dashboard (Runners > Discover)." -ForegroundColor DarkGray
