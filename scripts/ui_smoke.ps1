# Windows equivalent of scripts/ui_smoke.sh, which is bash-only and this
# environment has no bash. Boots web_app.py against a temp data dir, seeds the
# same fixture, and drives it with the same ui_smoke.cjs.
param(
    [string]$Chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path $Chrome)) { Write-Error "Chrome not found at $Chrome"; exit 2 }
if (-not (Test-Path "scripts\node_modules\puppeteer")) { Write-Error "puppeteer is not installed (npm ci --prefix scripts)"; exit 2 }

$dataDir = Join-Path $env:TEMP ("obx-ui-smoke-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $dataDir | Out-Null
$env:OPENBOX_DATA_DIR = $dataDir
$env:PUPPETEER_EXECUTABLE_PATH = $Chrome
$env:PUPPETEER_SKIP_DOWNLOAD = "true"

$server = $null
try {
    $server = Start-Process -FilePath "python" -ArgumentList "-B", "web_app.py", "--no-browser" `
        -WorkingDirectory (Get-Location) -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $dataDir "server.log") `
        -RedirectStandardError  (Join-Path $dataDir "server.err")

    $tokenFile = Join-Path $dataDir "server.token"
    $portFile  = Join-Path $dataDir "server.port"
    for ($i = 0; $i -lt 60; $i++) {
        if ((Test-Path $tokenFile) -and (Test-Path $portFile)) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not (Test-Path $tokenFile)) {
        Write-Host "server never wrote a token; log:"
        Get-Content (Join-Path $dataDir "server.log") -ErrorAction SilentlyContinue
        Get-Content (Join-Path $dataDir "server.err") -ErrorAction SilentlyContinue
        exit 2
    }
    $env:TOKEN = (Get-Content $tokenFile -Raw).Trim()
    $env:PORT  = (Get-Content $portFile  -Raw).Trim()

    # Same two-game fixture as the bash script.
    $seed = @'
import os
from openbox import save_state
save_state({"games": [
    {"name": "Quake", "platform": "PC", "genre": "FPS", "year": "1996", "developer": "id Software", "path": "/bin/true", "favorite": True, "rating": 5, "progress": "Beaten", "play_count": 12, "playtime_seconds": 5400},
    {"name": "Chrono Trigger", "platform": "SNES", "genre": "RPG", "year": "1995", "path": "/bin/true"},
], "profiles": {}, "history": [], "settings": {}, "playlists": []})
print("seeded")
'@
    $seed | Out-File -FilePath (Join-Path $dataDir "seed.py") -Encoding utf8
    $env:PYTHONIOENCODING = "utf-8"
    # The seed script lives in the temp data dir, so `openbox` is not importable
    # from there; the bash heredoc gets it for free because it runs in the repo root.
    $env:PYTHONPATH = (Get-Location).Path
    python -B (Join-Path $dataDir "seed.py")
    if ($LASTEXITCODE -ne 0) { Write-Error "seeding failed"; exit 2 }

    Write-Host "running ui_smoke.cjs against port $env:PORT"
    node scripts/ui_smoke.cjs
    $status = $LASTEXITCODE
    if ($status -ne 0) {
        Write-Host "--- server log tail ---"
        Get-Content (Join-Path $dataDir "server.log") -Tail 40 -ErrorAction SilentlyContinue
    }
    exit $status
}
finally {
    if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }
}
