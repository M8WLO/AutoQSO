# AutoQSO Windows build script
# Run from the AutoQSO directory:  .\build_installer.ps1

param(
    [string]$Version = "1.0.0"
)

$ErrorActionPreference = "Stop"
$ScriptDir = $PSScriptRoot

Write-Host "=== AutoQSO Build v$Version ===" -ForegroundColor Cyan

# ── sanity checks
if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    Write-Error "PyInstaller not found. Run: pip install pyinstaller"
    exit 1
}

# ── clean previous build artefacts
Write-Host "Cleaning previous build..." -ForegroundColor Yellow
Remove-Item -Recurse -Force "$ScriptDir\build"  -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$ScriptDir\dist"   -ErrorAction SilentlyContinue

# ── build
Write-Host "Running PyInstaller..." -ForegroundColor Yellow
Set-Location $ScriptDir

pyinstaller --clean --noconfirm AutoQSO.spec

if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller failed with exit code $LASTEXITCODE"
    exit $LASTEXITCODE
}

# ── copy default config if not present in dist
$DistDir = "$ScriptDir\dist\AutoQSO"
if (-not (Test-Path "$DistDir\config.json")) {
    Write-Host "No config.json in dist — a fresh one will be created on first run."
}

# ── copy rigctld launcher into dist so users have it handy
Copy-Item "$ScriptDir\rigctld_start.ps1" "$DistDir\" -Force

# ── remove build folder so nobody accidentally runs the wrong exe
Write-Host "Removing build/ temp folder..." -ForegroundColor Yellow
Remove-Item -Recurse -Force "$ScriptDir\build" -ErrorAction SilentlyContinue

# ── create zip for distribution
$ZipName = "AutoQSO-v$Version-win64.zip"
$ZipPath = "$ScriptDir\dist\$ZipName"
Write-Host "Creating $ZipName..." -ForegroundColor Yellow
Compress-Archive -Path "$DistDir\*" -DestinationPath $ZipPath -Force

Write-Host ""
Write-Host "=== Build complete ===" -ForegroundColor Green
Write-Host "  Folder : $DistDir"
Write-Host "  Zip    : $ZipPath"
Write-Host ""
Write-Host "Run the app: $DistDir\AutoQSO.exe" -ForegroundColor Cyan
