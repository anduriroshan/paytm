# Munim: deploy to Google Cloud Run and print the public URL.
#   .\deploy.ps1          live: passes the provider keys and settings from backend/.env
#   .\deploy.ps1 -Mock    offline scripted brain only, no keys leave this machine (safe for a public link)
# Needs the Google Cloud CLI (gcloud) and a one-time `gcloud auth login`.
# Builds in Cloud Build from this folder (no local Docker needed). Re-run to redeploy.
param(
    [switch]$Mock,
    [string]$Project = "musim-agent",
    [string]$Region = "asia-south1",      # Mumbai
    [string]$Service = "munim"
)

$root = $PSScriptRoot

function Run-Gcloud {
    & gcloud @args
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($args -join ' ') failed (exit $LASTEXITCODE)" }
}

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    throw "gcloud not found. Install the Google Cloud CLI, open a new terminal, then run: gcloud auth login"
}

$account = (& gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>$null)
if (-not $account) {
    Write-Host "Not signed in to gcloud, opening the browser..." -ForegroundColor Cyan
    Run-Gcloud auth login
}

Write-Host "Project $Project, region $Region" -ForegroundColor Cyan
Run-Gcloud config set project $Project --quiet
Write-Host "Enabling Cloud Run, Cloud Build, Artifact Registry (first time takes a minute)..." -ForegroundColor Cyan
Run-Gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com --quiet

# ── environment for the container: written to a temp YAML (values never printed), deleted afterwards ──
$vars = [ordered]@{}
if ($Mock) {
    $vars["MUNIM_FORCE_MOCK"] = "1"
} else {
    $envFile = Join-Path $root "backend\.env"
    if (-not (Test-Path $envFile)) { throw "backend\.env not found. Copy backend\.env.example, add keys, or run with -Mock." }
    foreach ($line in Get-Content $envFile -Encoding UTF8) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith("#") -or -not $t.Contains("=")) { continue }
        $k = $t.Substring(0, $t.IndexOf("=")).Trim()
        $v = $t.Substring($t.IndexOf("=") + 1).Trim()
        if ($v.Length -ge 2 -and (($v[0] -eq '"' -and $v[-1] -eq '"') -or ($v[0] -eq "'" -and $v[-1] -eq "'"))) {
            $v = $v.Substring(1, $v.Length - 2)
        } elseif ($v.Contains(" #")) {
            $v = $v.Substring(0, $v.IndexOf(" #")).Trim()
        }
        if (-not $v) { continue }
        if ($k -in @("MUNIM_DB", "MUNIM_SOURCES_DIR", "PORT")) { continue }   # local paths / Cloud Run's own
        $vars[$k] = $v
    }
}
$yaml = foreach ($k in $vars.Keys) { "${k}: '" + ($vars[$k] -replace "'", "''") + "'" }
$tmp = Join-Path $env:TEMP "munim-cloudrun-env-$PID.yaml"
[System.IO.File]::WriteAllLines($tmp, [string[]]$yaml, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ("Mode: " + $(if ($Mock) { "MOCK (no keys)" } else { "LIVE" }) + ", passing: " + (($vars.Keys) -join ", ")) -ForegroundColor Cyan

try {
    Write-Host "Building and deploying (about 3-5 minutes)..." -ForegroundColor Cyan
    # max 1 instance: the demo database is SQLite inside the container, so every visitor must hit the same one.
    # min 1 instance: keeps it warm and keeps cases between visits. Set --min-instances 0 after the event to stop paying for idle.
    Run-Gcloud run deploy $Service `
        --source $root `
        --region $Region `
        --allow-unauthenticated `
        --min-instances 1 --max-instances 1 `
        --cpu 1 --memory 1Gi `
        --timeout 3600 `
        --execution-environment gen2 `
        --env-vars-file $tmp `
        --quiet
} finally {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}

$url = (& gcloud run services describe $Service --region $Region --format="value(status.url)")
Write-Host ""
Write-Host "Munim is live: $url" -ForegroundColor Green
Write-Host "Replay backup: $url/?replay=s1_zombie_rent" -ForegroundColor Green
Write-Host "Merchant app:  $url/?view=merchant" -ForegroundColor Green
