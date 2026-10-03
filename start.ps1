<#
Starts Tax Automaton for everyday use.

  .\start.ps1            Only this computer can open the app (http://localhost:3000).
  .\start.ps1 -Lan       Other computers in the office can open it too, at
                         http://<this computer's address>:3000.
  .\start.ps1 -Rebuild   Rebuild the web app first (after installing an update).
  .\start.ps1 -NoBrowser Don't open the browser.

Keep the window open while the app is in use. Press Ctrl+C to stop it.
#>
param([switch]$Lan, [switch]$Rebuild, [switch]$NoBrowser)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Join-Path $root "backend"
$web = Join-Path $root "web"

function Require($command, $message) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        Write-Host $message -ForegroundColor Red
        exit 1
    }
}

function Run($what, [scriptblock]$block) {
    Write-Host $what
    & $block
    if ($LASTEXITCODE -ne 0) {
        Write-Host "That step failed. Read the messages above, then run start.ps1 again." -ForegroundColor Red
        exit 1
    }
}

Require "uv" "uv is not installed. Install it from https://docs.astral.sh/uv/ and run this again."
Require "node" "Node.js is not installed. Install the LTS version from https://nodejs.org and run this again."

Push-Location $backend
try {
    Run "Preparing the app..." { uv sync --quiet }
    # Makes a backup first whenever the database needs updating.
    Run "Checking the database..." { uv run python -m app.devtools.upgrade }
} finally { Pop-Location }

Push-Location $web
try {
    if (-not (Test-Path (Join-Path $web "node_modules"))) {
        Run "Installing the web app (first run only)..." { npm ci --no-audit --no-fund }
    }
    if ($Rebuild -or -not (Test-Path (Join-Path $web ".next\BUILD_ID"))) {
        Run "Building the web app (takes a minute)..." { npm run build }
    }
} finally { Pop-Location }

# The API only ever listens on this computer: the web app talks to it, browsers never do.
# The web app listens on all network cards only when -Lan is given (Next's own default would
# be all of them).
$webHost = if ($Lan) { "0.0.0.0" } else { "127.0.0.1" }
$api = Start-Process -FilePath "uv" -WorkingDirectory $backend -NoNewWindow -PassThru `
    -ArgumentList "run", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"
$site = Start-Process -FilePath "npm.cmd" -WorkingDirectory $web -NoNewWindow -PassThru `
    -ArgumentList "run", "start", "--", "--port", "3000", "--hostname", $webHost

function Stop-App {
    foreach ($p in @($site, $api)) {
        if ($p -and -not $p.HasExited) { & taskkill /PID $p.Id /T /F 2>$null | Out-Null }
    }
}

try {
    $ready = $false
    for ($i = 0; $i -lt 60 -and -not $ready; $i++) {
        Start-Sleep -Seconds 1
        try {
            # The API first, directly: asking through the web app before the API is up only
            # fills the window with connection errors.
            $apiUp = (Invoke-WebRequest "http://127.0.0.1:8000/api/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200
            $ready = $apiUp -and (Invoke-WebRequest "http://127.0.0.1:3000/login" -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200
        } catch { }
        if ($api.HasExited -or $site.HasExited) { break }
    }
    if (-not $ready) {
        Write-Host "The app did not start. Read the messages above." -ForegroundColor Red
        exit 1
    }
    Write-Host ""
    Write-Host "Tax Automaton is running: http://localhost:3000" -ForegroundColor Green
    if ($Lan) {
        $ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
            Select-Object -ExpandProperty IPAddress
        foreach ($ip in $ips) { Write-Host "Other computers in the office: http://${ip}:3000" }
    }
    Write-Host "Keep this window open while you use the app. Press Ctrl+C to stop it."
    if (-not $NoBrowser) { Start-Process "http://localhost:3000" }
    while (-not $api.HasExited -and -not $site.HasExited) { Start-Sleep -Seconds 2 }
    Write-Host "The app stopped unexpectedly. Read the messages above, then run start.ps1 again." -ForegroundColor Red
} finally {
    Stop-App
}
