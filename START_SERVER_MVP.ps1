$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$MvpDir = Get-ChildItem -LiteralPath $ProjectRoot -Directory -Filter "*_MVP" | Select-Object -First 1

if (-not $MvpDir) {
    Write-Host "MVP directory was not found."
    Read-Host "Press Enter to exit"
    exit 1
}

$Backend = Join-Path $MvpDir.FullName "backend"
$ProjectPython = Join-Path $MvpDir.FullName ".venv\Scripts\python.exe"
$RuntimePython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$Python = $ProjectPython
if (Test-Path -LiteralPath $RuntimePython) {
    $Python = $RuntimePython
}
$SitePackages = Join-Path $MvpDir.FullName ".venv\Lib\site-packages"

Write-Host ""
Write-Host "Starting MVP local server..."
Write-Host "URL: http://127.0.0.1:8000/"
Write-Host ""

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host "Python was not found:"
    Write-Host $Python
    Read-Host "Press Enter to exit"
    exit 1
}

if (-not (Test-Path -LiteralPath $Backend)) {
    Write-Host "Backend directory was not found:"
    Write-Host $Backend
    Read-Host "Press Enter to exit"
    exit 1
}

$env:PYTHONPATH = $SitePackages
$env:PYTHONUTF8 = "1"
Set-Location -LiteralPath $Backend

$StopFile = Join-Path $Backend ".server-stop"
$LogFile = Join-Path $Backend "runserver.supervisor.log"
if (Test-Path -LiteralPath $StopFile) {
    Remove-Item -LiteralPath $StopFile -Force
}

Write-Host "Applying database migrations..."
& $Python manage.py migrate
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Migration failed. Server was not started."
    Read-Host "Press Enter to exit"
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Server is starting with auto-restart. Keep this window open while using the site."
Write-Host "Stop with Ctrl+C or run STOP_SERVER_MVP.ps1."
Write-Host ""

while ($true) {
    $startedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "[$startedAt] starting server" | Out-File -FilePath $LogFile -Append -Encoding utf8
    & $Python manage.py runserver 127.0.0.1:8000 --noreload
    $exitCode = $LASTEXITCODE
    $stoppedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "[$stoppedAt] server stopped with exit code $exitCode" | Out-File -FilePath $LogFile -Append -Encoding utf8

    if (Test-Path -LiteralPath $StopFile) {
        "[$stoppedAt] stop marker found, supervisor exits" | Out-File -FilePath $LogFile -Append -Encoding utf8
        break
    }

    Write-Host ""
    Write-Host "Server process stopped. Restarting in 2 seconds..."
    Write-Host ""
    Start-Sleep -Seconds 2
}

Write-Host ""
Write-Host "Server stopped."
Read-Host "Press Enter to exit"
