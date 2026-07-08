$ErrorActionPreference = "SilentlyContinue"

Write-Host "Stopping local server on port 8000..."

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$MvpDir = Get-ChildItem -LiteralPath $ProjectRoot -Directory -Filter "*_MVP" | Select-Object -First 1
if ($MvpDir) {
    $Backend = Join-Path $MvpDir.FullName "backend"
    New-Item -ItemType File -Path (Join-Path $Backend ".server-stop") -Force | Out-Null
}

$connections = Get-NetTCPConnection -LocalPort 8000 -State Listen
foreach ($connection in $connections) {
    $serverProcessId = $connection.OwningProcess
    if ($serverProcessId) {
        Write-Host "Stopping process $serverProcessId"
        Stop-Process -Id $serverProcessId -Force
    }
}

Write-Host "Done."
