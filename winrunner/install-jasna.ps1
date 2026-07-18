# Installs the Jasna decensor engine for the Stashify Windows runner.
#
# Downloads the frozen Jasna release (github.com/Kruk2/jasna, self-contained:
# bundled Python/TensorRT/ffmpeg/mkvmerge + model weights, ~4.1 GB in 3 parts),
# extracts it, points the runner's config.json at jasna.exe, and restarts the
# runner task. Needs 7-Zip for the split .7z (uses an installed 7z.exe if
# found, else downloads the standalone 7zr.exe from 7-zip.org).
#
#   powershell -ExecutionPolicy Bypass -File install-jasna.ps1
#   ... -InstallDir "F:\Jasna"      # default: %LOCALAPPDATA%\StashifyRunner\jasna
#   ... -SkipDownload               # parts already in $InstallDir\parts
param(
    [string]$Version = "0.8.0",
    [string]$Root = "$env:LOCALAPPDATA\StashifyRunner",
    [string]$InstallDir = "",
    [switch]$SkipDownload
)
$ErrorActionPreference = "Stop"
if (-not $InstallDir) { $InstallDir = Join-Path $Root "jasna" }
$rel = "https://github.com/Kruk2/jasna/releases/download/v$Version"
# asset naming changed at v0.8.0: jasna-<ver>-win.7z.00N (was jasna-windows-v<ver>.7z.00N)
$verNum = ($Version -replace '^v', '' -replace '-.*$', '')
$isNewNaming = try { [version]$verNum -ge [version]"0.8.0" } catch { $true }
$parts = if ($isNewNaming) {
    @("jasna-$Version-win.7z.001", "jasna-$Version-win.7z.002", "jasna-$Version-win.7z.003")
} else {
    @("jasna-windows-v$Version.7z.001", "jasna-windows-v$Version.7z.002", "jasna-windows-v$Version.7z.003")
}
$partsDir = Join-Path $InstallDir "parts"
$cfgPath = Join-Path $Root "config.json"

New-Item -ItemType Directory -Force $partsDir | Out-Null

# --- download the release parts (resumable via curl.exe, ships with Win10+) ---
if (-not $SkipDownload) {
    foreach ($p in $parts) {
        $dest = Join-Path $partsDir $p
        Write-Host "downloading $p ..." -ForegroundColor Cyan
        & curl.exe -L --fail --retry 3 -C - -o $dest "$rel/$p"
        if ($LASTEXITCODE -ne 0) { throw "download failed: $p" }
    }
}
foreach ($p in $parts) {
    if (-not (Test-Path (Join-Path $partsDir $p))) { throw "missing part: $p (use -SkipDownload only if parts are present)" }
}

# --- find or fetch a 7z extractor ---
$sevenZip = @("$env:ProgramFiles\7-Zip\7z.exe", "${env:ProgramFiles(x86)}\7-Zip\7z.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $sevenZip) {
    $sevenZip = Join-Path $partsDir "7zr.exe"
    if (-not (Test-Path $sevenZip)) {
        Write-Host "7-Zip not installed - fetching standalone 7zr.exe" -ForegroundColor Yellow
        & curl.exe -L --fail -o $sevenZip "https://7-zip.org/a/7zr.exe"
        if ($LASTEXITCODE -ne 0) { throw "could not fetch 7zr.exe" }
    }
}

# --- extract (first part implies the rest) ---
Write-Host "extracting to $InstallDir ..." -ForegroundColor Cyan
& $sevenZip x -y "-o$InstallDir" (Join-Path $partsDir $parts[0])
if ($LASTEXITCODE -ne 0) { throw "extraction failed" }

$exe = Get-ChildItem -Path $InstallDir -Recurse -Filter "jasna*.exe" |
    Where-Object { $_.Name -match '^jasna(-cli)?\.exe$' } |
    Sort-Object { $_.Name -ne 'jasna-cli.exe' } | Select-Object -First 1
if (-not $exe) { throw "no jasna executable found under $InstallDir after extraction" }
Write-Host "jasna executable: $($exe.FullName)" -ForegroundColor Green

# --- point the runner config at it (BOM-free write; PS5 Set-Content adds a BOM) ---
if (-not (Test-Path $cfgPath)) { throw "runner config not found at $cfgPath - run install.ps1 first" }
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
$cfg | Add-Member -NotePropertyName jasna_exe -NotePropertyValue $exe.FullName -Force
# jasna >=0.8.0 dropped --working-directory (writes output in place); the runner
# must not pass it. (v0.8.0 also needs Nvidia driver >=610 on Windows.)
$inPlace = try { [version]$verNum -ge [version]"0.8.0" } catch { $true }
$cfg | Add-Member -NotePropertyName jasna_in_place -NotePropertyValue $inPlace -Force
$json = $cfg | ConvertTo-Json -Depth 8
[System.IO.File]::WriteAllText($cfgPath, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "config.json: jasna_exe set (jasna_in_place=$inPlace)" -ForegroundColor Green

# --- restart the runner so /health advertises decensor ---
# (cmd /c swallows stderr: under EAP=Stop, PS 5.1 turns native stderr into a
# terminating error, and schtasks /End complains if the task isn't running)
cmd /c "schtasks /End /TN StashifyRunner >nul 2>&1"
Start-Sleep 2
cmd /c "schtasks /Run /TN StashifyRunner >nul 2>&1"
Start-Sleep 6
try {
    $h = Invoke-RestMethod "http://localhost:8712/ping" -TimeoutSec 5
    Write-Host ("runner ops now: " + ($h.ops -join ", ")) -ForegroundColor Green
    if ($h.ops -notcontains "decensor") { Write-Warning "runner is up but decensor not advertised - check jasna_exe in $cfgPath" }
} catch { Write-Warning "runner /ping not answering yet - check the tray icon" }

Write-Host ""
Write-Host "NOTE: the FIRST decensor job compiles TensorRT engines (15-60 min" -ForegroundColor Yellow
Write-Host "of silence before frames start; engines are cached after that)." -ForegroundColor Yellow
Write-Host "Cleanup: the downloaded parts in $partsDir can be deleted." -ForegroundColor DarkGray
