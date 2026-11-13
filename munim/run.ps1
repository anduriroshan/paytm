# Munim: one command to start the demo on http://127.0.0.1:8000
#   .\run.ps1            build the UI if missing, start the API (serves the UI too)
#   .\run.ps1 -Mock      force the offline scripted brain (no keys / no credits / venue Wi-Fi down)
#   .\run.ps1 -Reset     re-seed the demo database first
#   .\run.ps1 -Build     rebuild the UI first
param([switch]$Mock, [switch]$Reset, [switch]$Build, [int]$Port = 8000)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$py = Join-Path $backend ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "Creating Python venv..." -ForegroundColor Cyan
    python -m venv (Join-Path $backend ".venv")
    & $py -m pip install -q -r (Join-Path $backend "requirements.txt")
}

if ($Build -or -not (Test-Path (Join-Path $frontend "dist\index.html"))) {
    Write-Host "Building UI..." -ForegroundColor Cyan
    Push-Location $frontend
    if (-not (Test-Path "node_modules")) { npm install --no-fund --no-audit }
    npm run build
    Pop-Location
}

Push-Location $backend
if ($Reset) { & $py -m munim.db.seed }
if ($Mock) { $env:MUNIM_FORCE_MOCK = "1" } else { Remove-Item Env:MUNIM_FORCE_MOCK -ErrorAction SilentlyContinue }
Write-Host "Munim on http://127.0.0.1:$Port   (replay backup: http://127.0.0.1:$Port/?replay=s1_zombie_rent)" -ForegroundColor Green
& $py -m uvicorn munim.api.main:app --host 127.0.0.1 --port $Port
Pop-Location
