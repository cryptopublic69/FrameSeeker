$root = $PSScriptRoot
$pidFile = "$root\.ui-pids.json"

if (Test-Path $pidFile) {
    $saved = Get-Content -Raw $pidFile | ConvertFrom-Json
    foreach ($processId in $saved.pids) {
        if (Get-Process -Id $processId -ErrorAction SilentlyContinue) {
            taskkill.exe /PID $processId /T /F | Out-Null
        }
    }
    Remove-Item -LiteralPath $pidFile -Force
}

& docker compose -f "$root\docker-compose.yml" stop qdrant | Out-Null
