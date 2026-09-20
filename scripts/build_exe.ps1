# Builds a single Coworker.exe the user can just double-click.
#   powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
#
# Speech-to-text models are NOT bundled - they download on first use, which
# keeps the executable around 25 MB instead of several hundred.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "Installing build dependencies..." -ForegroundColor Cyan
python -m pip install --quiet --upgrade pyinstaller
python -m pip install --quiet -r agent\requirements.txt

Write-Host "Building..." -ForegroundColor Cyan
python -m PyInstaller `
    --noconfirm --clean --onefile --windowed `
    --name Coworker `
    --paths agent `
    --collect-submodules coworker `
    --hidden-import websockets `
    --hidden-import httpx `
    --exclude-module matplotlib `
    --exclude-module notebook `
    agent\run.py

$exe = Join-Path $root "dist\Coworker.exe"
if (Test-Path $exe) {
    $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "`nTayyor: $exe  ($mb MB)" -ForegroundColor Green
} else {
    Write-Host "`nBuild failed." -ForegroundColor Red
    exit 1
}
