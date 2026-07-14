$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$pids = @()
$pidFile = "$root\.ui-pids.json"
$apiKeyFile = "$root\.framefinder-api-key"

function Protect-ApiKeyFile([string]$path) {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls $path /inheritance:r /grant:r "${identity}:(R,W)" "SYSTEM:(F)" *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to restrict permissions on the FrameFinder API key file."
    }
}

# The formal /api/v1 contract always requires an API key. Keep it in a private,
# git-ignored file for the double-click launcher, while allowing administrators
# to override it with the FRAMEFINDER_API_KEY environment variable.
if (-not $env:FRAMEFINDER_API_KEY) {
    if (Test-Path -LiteralPath $apiKeyFile) {
        $env:FRAMEFINDER_API_KEY = (Get-Content -LiteralPath $apiKeyFile -Raw).Trim()
    } else {
        $randomBytes = [byte[]]::new(32)
        $randomGenerator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $randomGenerator.GetBytes($randomBytes)
        } finally {
            $randomGenerator.Dispose()
        }
        $env:FRAMEFINDER_API_KEY = [Convert]::ToBase64String($randomBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
        Set-Content -LiteralPath $apiKeyFile -Value $env:FRAMEFINDER_API_KEY -NoNewline -Encoding ASCII
    }
    Protect-ApiKeyFile $apiKeyFile
}

# Reusing the launcher must not discard the process IDs recorded by an earlier
# successful start. Otherwise stop_ui.cmd would no longer be able to stop them.
if (Test-Path $pidFile) {
    try {
        $saved = Get-Content -Raw $pidFile | ConvertFrom-Json
        foreach ($processId in @($saved.pids)) {
            if ($processId -and (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
                $pids += [int]$processId
            }
        }
    } catch {
        # A damaged/stale state file is safe to replace after a healthy start.
        $pids = @()
    }
}

function Get-LanIPv4 {
    $udp = [System.Net.Sockets.UdpClient]::new()
    try {
        $udp.Connect("8.8.8.8", 53)
        return ([System.Net.IPEndPoint]$udp.Client.LocalEndPoint).Address.IPAddressToString
    } finally {
        $udp.Dispose()
    }
}

$lanIp = Get-LanIPv4
$lanUiUrl = "http://${lanIp}:3417"
$lanApiUrl = "http://${lanIp}:8000"

function Test-DockerEngine {
    & docker info *> $null
    return $LASTEXITCODE -eq 0
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is not installed. Install Docker Desktop before starting FrameFinder."
}

if (-not (Test-DockerEngine)) {
    $dockerDesktop = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path $dockerDesktop)) {
        throw "Docker Desktop is not running and could not be found at: $dockerDesktop"
    }

    Write-Host "Starting Docker Desktop..."
    Start-Process -FilePath $dockerDesktop | Out-Null

    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        Start-Sleep -Seconds 2
        if (Test-DockerEngine) { break }
    }

    if (-not (Test-DockerEngine)) {
        throw "Docker Desktop did not become ready within 3 minutes."
    }
}

& docker compose -f "$root\docker-compose.yml" up -d qdrant
if ($LASTEXITCODE -ne 0) { throw "Qdrant failed to start." }

function Test-Url([string]$url) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

for ($attempt = 0; $attempt -lt 30; $attempt++) {
    if (Test-Url "http://127.0.0.1:6333/healthz") { break }
    Start-Sleep -Milliseconds 500
}

if (-not (Test-Url "http://127.0.0.1:6333/healthz")) {
    throw "Qdrant failed its health check."
}

if (-not (Test-Url "http://127.0.0.1:8000/api/health")) {
    $backend = Start-Process -FilePath "$root\.venv\Scripts\python.exe" `
        -ArgumentList "server.py" -WorkingDirectory $root -WindowStyle Hidden -PassThru
    $pids += $backend.Id
}

if (-not (Test-Url "http://127.0.0.1:3417")) {
    $frontend = Start-Process -FilePath "npm.cmd" -ArgumentList "run", "dev" `
        -WorkingDirectory "$root\web" -WindowStyle Hidden -PassThru
    $pids += $frontend.Id
}

for ($attempt = 0; $attempt -lt 60; $attempt++) {
    if ((Test-Url "http://127.0.0.1:8000/api/health") -and (Test-Url "http://127.0.0.1:3417")) {
        break
    }
    Start-Sleep -Milliseconds 500
}

if (-not ((Test-Url "http://127.0.0.1:8000/api/health") -and (Test-Url "http://127.0.0.1:3417"))) {
    throw "FrameFinder failed to start."
}

@{
    pids = $pids
    lan_ui_url = $lanUiUrl
    lan_api_url = $lanApiUrl
} | ConvertTo-Json | Set-Content -Encoding UTF8 "$root\.ui-pids.json"

Write-Host "FrameFinder LAN UI:  $lanUiUrl"
Write-Host "FrameFinder LAN API: $lanApiUrl"
Start-Process $lanUiUrl
