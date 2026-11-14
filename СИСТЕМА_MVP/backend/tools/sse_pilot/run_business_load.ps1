param(
    [string]$Python = 'C:\codex-tmp\sse-pilot-venv\Scripts\python.exe',
    [int]$Port = 8767,
    [int]$Clients = 96,
    [int]$WarmupSeconds = 3,
    [int]$MeasurementSeconds = 20,
    [string]$OutputDirectory = 'artifacts\sse-business-load96'
)

$ErrorActionPreference = 'Stop'
$backend = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$output = [System.IO.Path]::GetFullPath((Join-Path $backend $OutputDirectory))
New-Item -ItemType Directory -Path $output -Force | Out-Null

$tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$disposableRoot = Join-Path $tempBase ("sse-pilot-disposable-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $disposableRoot | Out-Null
$marker = Join-Path $disposableRoot '.sse-pilot-disposable-root'
[System.IO.File]::WriteAllText(
    $marker,
    "copper-resources-sse-pilot-disposable-v1`n",
    [System.Text.UTF8Encoding]::new($false)
)
$database = Join-Path $disposableRoot 'business-load.sqlite3'
$fixture = Join-Path $disposableRoot 'fixture-private.json'
$expected = Join-Path $disposableRoot 'expected.json'
$ready = Join-Path $disposableRoot 'clients-ready.json'

$serverOut = Join-Path $output 'server.stdout.log'
$serverErr = Join-Path $output 'server.stderr.log'
$setupOut = Join-Path $output 'fixture-public.log'
$emitOut = Join-Path $output 'business-sequence.log'
$clientOut = Join-Path $output 'client.stdout.log'
$clientErr = Join-Path $output 'client.stderr.log'
$clientJson = Join-Path $output 'business-load.json'
$samplesJson = Join-Path $output 'business-load-samples.json'
$resourcesCsv = Join-Path $output 'server-resources.csv'
$acceptanceJson = Join-Path $output 'acceptance.json'
$acceptanceOut = Join-Path $output 'acceptance.stdout.log'
$acceptanceErr = Join-Path $output 'acceptance.stderr.log'
$metadataJson = Join-Path $output 'run-metadata.json'

$server = $null
$client = $null
$monitorJob = $null
$startedUtc = [DateTime]::UtcNow
$clientExit = $null
$emitterExit = $null
$evaluatorExit = $null
try {
    Push-Location $backend
    try {
        & $Python -m tools.sse_pilot.setup_business_fixture `
            --database $database --disposable-root $disposableRoot --output $fixture `
            1> $setupOut 2>&1
        if ($LASTEXITCODE -ne 0) { throw "fixture setup failed: $LASTEXITCODE" }
    }
    finally { Pop-Location }

    $fixtureData = Get-Content -LiteralPath $fixture -Raw -Encoding UTF8 | ConvertFrom-Json
    $startCursor = [int]$fixtureData.start_cursor

    $env:DJANGO_SETTINGS_MODULE = 'config.sse_load_settings'
    $env:SSE_LOAD_DISPOSABLE_DB = $database
    $env:SSE_LOAD_DISPOSABLE_ROOT = $disposableRoot
    $env:SSE_PILOT_ENABLED = 'true'
    $env:SSE_REDIS_URL = ''
    $env:SSE_HEARTBEAT_SECONDS = '2'
    $env:SSE_SAFETY_SCAN_SECONDS = '1'
    $env:SSE_REAUTH_SECONDS = '2'
    $env:SSE_STREAM_MAX_SECONDS = '60'
    $env:SSE_BATCH_SIZE = '200'
    $env:DJANGO_ALLOWED_HOSTS = '127.0.0.1,localhost'

    $serverArgs = @(
        '-m', 'uvicorn', 'config.sse_asgi:application',
        '--host', '127.0.0.1', '--port', [string]$Port,
        '--workers', '1', '--log-level', 'info', '--access-log'
    )
    $server = Start-Process -FilePath $Python -ArgumentList $serverArgs `
        -WorkingDirectory $backend -RedirectStandardOutput $serverOut `
        -RedirectStandardError $serverErr -WindowStyle Hidden -PassThru
    $listening = $false
    for ($attempt = 0; $attempt -lt 100; $attempt++) {
        Start-Sleep -Milliseconds 100
        if ($server.HasExited) { throw "uvicorn exited: $($server.ExitCode)" }
        if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
            $listening = $true
            break
        }
    }
    if (-not $listening) { throw "uvicorn did not listen on loopback" }

    $monitorJob = Start-Job -ScriptBlock {
        param($PortNumber, $DatabasePath)
        while ($true) {
            $connections = @(Get-NetTCPConnection -LocalPort $PortNumber -ErrorAction SilentlyContinue)
            $listener = $connections | Where-Object { $_.State -eq 'Listen' } | Select-Object -First 1
            if (-not $listener) { break }
            $worker = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
            if (-not $worker) { break }
            $databaseFile = Get-Item -LiteralPath $DatabasePath -ErrorAction SilentlyContinue
            [pscustomobject]@{
                utc = [DateTime]::UtcNow.ToString('o')
                worker_pid = $worker.Id
                cpu_total_seconds = [math]::Round($worker.CPU, 6)
                working_set_bytes = $worker.WorkingSet64
                private_bytes = $worker.PrivateMemorySize64
                thread_count = $worker.Threads.Count
                handle_count = $worker.HandleCount
                established_tcp = @($connections | Where-Object { $_.State -eq 'Established' }).Count
                sqlite_bytes = if ($databaseFile) { $databaseFile.Length } else { 0 }
            }
            Start-Sleep -Milliseconds 250
        }
    } -ArgumentList @($Port, $database)

    $clientArgs = @(
        '-m', 'tools.sse_pilot.business_load_client',
        '--url', "http://127.0.0.1:$Port/realtime/stream/",
        '--clients', [string]$Clients,
        '--warmup-seconds', [string]$WarmupSeconds,
        '--measurement-seconds', [string]$MeasurementSeconds,
        '--readiness-timeout', '30',
        '--stall-timeout', '10', '--heartbeat-seconds', '2',
        '--min-heartbeats', '2', '--max-reconnects', '0', '--max-timeouts', '0',
        '--max-heartbeat-cadence-overrun-ms', '8000',
        '--after', [string]$startCursor,
        '--database', $database,
        '--disposable-root', $disposableRoot,
        '--fixture', $fixture, '--expected', $expected, '--ready', $ready,
        '--output', $clientJson,
        '--samples-output', $samplesJson
    )
    $client = Start-Process -FilePath $Python -ArgumentList $clientArgs `
        -WorkingDirectory $backend -RedirectStandardOutput $clientOut `
        -RedirectStandardError $clientErr -WindowStyle Hidden -PassThru

    $allReady = $false
    for ($attempt = 0; $attempt -lt 300; $attempt++) {
        Start-Sleep -Milliseconds 100
        if ($client.HasExited) { throw "client exited before readiness: $($client.ExitCode)" }
        if (Test-Path -LiteralPath $ready) {
            $allReady = $true
            break
        }
    }
    if (-not $allReady) { throw "clients did not all connect within 30 seconds" }

    Push-Location $backend
    try {
        & $Python -m tools.sse_pilot.emit_business_sequence `
            --database $database --disposable-root $disposableRoot `
            --fixture $fixture --output $expected --interval-ms 100 `
            1> $emitOut 2>&1
        $emitterExit = $LASTEXITCODE
    }
    finally { Pop-Location }
    if ($emitterExit -ne 0) { throw "business emitter failed: $emitterExit" }

    while (-not $client.HasExited) {
        $server.Refresh()
        if ($server.HasExited) { throw "uvicorn exited during load: $($server.ExitCode)" }
        Start-Sleep -Milliseconds 250
        $client.Refresh()
    }
    $client.WaitForExit()
    $client.Refresh()
    $clientExit = [int]$client.ExitCode
}
finally {
    if ($client -and -not $client.HasExited) {
        Stop-Process -Id $client.Id
        $client.WaitForExit()
    }
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id
        $server.WaitForExit()
    }
    if ($monitorJob) {
        Wait-Job -Job $monitorJob -Timeout 5 | Out-Null
        Receive-Job -Job $monitorJob | Export-Csv -LiteralPath $resourcesCsv -NoTypeInformation -Encoding UTF8
        Remove-Job -Job $monitorJob -Force
    }
    $metadata = [ordered]@{
        schema = 1
        started_utc = $startedUtc.ToString('o')
        finished_utc = [DateTime]::UtcNow.ToString('o')
        isolation = 'loopback; newly-created disposable SQLite below OS temp; DB marker verified; Redis/push disabled'
        parameters = [ordered]@{
            clients = $Clients
            contexts = 4
            warmup_seconds = $WarmupSeconds
            fixed_measurement_seconds = $MeasurementSeconds
            heartbeat_seconds = 2
            min_heartbeats_per_client = 2
            max_reconnects_per_client = 0
            max_timeouts_per_client = 0
            max_heartbeat_cadence_overrun_ms_per_client = 8000
            safety_scan_seconds = 1
            reauth_seconds = 2
            stream_max_seconds = 60
            batch_size = 200
            port = $Port
        }
        exit_codes = [ordered]@{
            client = $clientExit
            emitter = $emitterExit
            evaluator = $evaluatorExit
            server = 'terminated_by_harness'
        }
    }
    $metadata | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $metadataJson -Encoding UTF8

    $resolvedDisposable = [System.IO.Path]::GetFullPath($disposableRoot)
    $resolvedTemp = [System.IO.Path]::GetFullPath($tempBase).TrimEnd('\') + '\'
    $safeLeaf = Split-Path -Leaf $resolvedDisposable
    if (
        $resolvedDisposable.StartsWith($resolvedTemp, [System.StringComparison]::OrdinalIgnoreCase) -and
        $safeLeaf.StartsWith('sse-pilot-disposable-') -and
        (Test-Path -LiteralPath $marker)
    ) {
        Remove-Item -LiteralPath $resolvedDisposable -Recurse -Force
    }
    else {
        throw "refusing to remove unverified disposable root: $resolvedDisposable"
    }
}

if (-not (Test-Path -LiteralPath $clientJson)) {
    throw "business client did not produce result JSON: $clientJson"
}
if (-not (Test-Path -LiteralPath $samplesJson)) {
    throw "business client did not produce raw timing samples: $samplesJson"
}
if (-not (Test-Path -LiteralPath $resourcesCsv)) {
    throw "resource monitor did not produce CSV: $resourcesCsv"
}

Push-Location $backend
try {
    & $Python -m tools.sse_pilot.evaluate_business_load `
        --result $clientJson --resources $resourcesCsv --output $acceptanceJson `
        --clients $Clients `
        --max-commit-p95-ms 5000 `
        --max-heartbeat-overrun-p95-ms 8000 `
        --max-cpu-percent-one-core 800 `
        --max-private-bytes 805306368 `
        --max-threads 128 `
        --min-resource-samples 8 `
        --min-resource-coverage-fraction 0.8 `
        1> $acceptanceOut 2> $acceptanceErr
    $evaluatorExit = $LASTEXITCODE
}
finally { Pop-Location }

$metadata.exit_codes.evaluator = $evaluatorExit
$metadata | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $metadataJson -Encoding UTF8

Write-Output "RESULT=$clientJson"
Write-Output "TIMING_SAMPLES=$samplesJson"
Write-Output "METADATA=$metadataJson"
Write-Output "RESOURCES=$resourcesCsv"
Write-Output "ACCEPTANCE=$acceptanceJson"

if ($clientExit -ne 0 -or $emitterExit -ne 0 -or $evaluatorExit -ne 0) {
    exit 2
}
