param(
    [string]$ProductRoot = 'C:\codex-tmp\off-c1-r1-autonomous-shift-20260930',
    [string]$Python = '',
    [string]$ExpectedSha = '6c31e8545fd5f5da727ce077774ab4eaf07fd21b',
    [string]$ReleaseBase = 'f7302f4346f64f5e8bfe89c7a4da232f7caed648',
    [string]$OutputDir = '',
    [switch]$RequirePostgres
)

$ErrorActionPreference = 'Stop'
$ProductRoot = (Resolve-Path -LiteralPath $ProductRoot).Path
$managePy = Get-ChildItem -LiteralPath $ProductRoot -Recurse -File -Filter 'manage.py' |
    Where-Object { $_.Directory.Name -eq 'backend' } |
    Select-Object -First 1
if (-not $managePy) { throw "Django backend not found below $ProductRoot" }
$BackendRoot = $managePy.Directory.FullName
if (-not $Python) {
    throw 'Pass -Python with an isolated project venv.'
}
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python runtime not found: $Python. Pass -Python with an isolated project venv."
}
if (-not $OutputDir) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $OutputDir = Join-Path $env:TEMP "off-c1-r1-$stamp"
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$OutputDir = (Resolve-Path -LiteralPath $OutputDir).Path
$requestedDbEngine = $env:DJANGO_DB_ENGINE
$requestedPostgresDb = $env:POSTGRES_DB

function Invoke-Gate {
    param(
        [string]$Name,
        [string]$WorkingDirectory,
        [string]$Executable,
        [string[]]$Arguments
    )
    $log = Join-Path $OutputDir "$Name.log"
    $stdout = Join-Path $OutputDir "$Name.stdout.log"
    $stderr = Join-Path $OutputDir "$Name.stderr.log"
    $quotedArguments = @($Arguments | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + $_.Replace('"', '\"') + '"' } else { $_ }
    })
    "COMMAND: $Executable $($Arguments -join ' ')" | Set-Content -Encoding utf8 -LiteralPath $log
    $process = Start-Process `
        -FilePath $Executable `
        -ArgumentList $quotedArguments `
        -WorkingDirectory $WorkingDirectory `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -Wait `
        -PassThru
    if (Test-Path -LiteralPath $stdout) {
        Get-Content -LiteralPath $stdout | Add-Content -Encoding utf8 -LiteralPath $log
    }
    if (Test-Path -LiteralPath $stderr) {
        Get-Content -LiteralPath $stderr | Add-Content -Encoding utf8 -LiteralPath $log
    }
    "EXIT_CODE: $($process.ExitCode)" | Add-Content -Encoding utf8 -LiteralPath $log
    if ($process.ExitCode -ne 0) {
        throw "$Name failed with exit code $($process.ExitCode); see $log"
    }
}

$status = & git -C $ProductRoot status --porcelain
if ($LASTEXITCODE -ne 0) { throw 'git status failed' }
if ($status) { throw "Dirty candidate worktree; refusing evidence run:`n$status" }

$actualSha = (& git -C $ProductRoot rev-parse HEAD).Trim()
if ($actualSha -ne $ExpectedSha) {
    throw "Unexpected candidate SHA: $actualSha (expected $ExpectedSha)"
}
& git -C $ProductRoot merge-base --is-ancestor $ReleaseBase $ExpectedSha
if ($LASTEXITCODE -ne 0) {
    throw "Release base $ReleaseBase is not an ancestor of $ExpectedSha"
}

$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
$env:DJANGO_DB_ENGINE = 'sqlite'
$renderedShell = Join-Path $OutputDir 'excavator-prepared-shell.html'
$env:EXCAVATOR_RENDERED_SHELL_PATH = $renderedShell

Invoke-Gate 'django-offline' $BackendRoot $Python @(
    'manage.py', 'test',
    'core.test_offline_sync',
    'core.test_free_bucket_sync',
    'core.test_offline_autonomous_shift',
    '--noinput'
)

Invoke-Gate 'django-trips' $BackendRoot $Python @(
    'manage.py', 'test',
    'trips.test_excavator_hourly_report',
    'trips.tests.ExcavatorWorkServerIntegrationTests',
    '--noinput'
)

Invoke-Gate 'django-safe-shell' $BackendRoot $Python @(
    'manage.py', 'test',
    'core.test_offline_autonomous_shift.AutonomousExcavatorShiftTests.test_authenticated_shell_can_be_exported_as_safe_service_worker_fixture',
    '--noinput'
)
if (-not (Test-Path -LiteralPath $renderedShell)) {
    throw 'The safe rendered-shell fixture was not produced.'
}

$nodeTests = @(
    'static/js/tests/excavator-local-shift-v1.test.js',
    'static/js/tests/excavator-field-outbox.test.js',
    'static/js/tests/excavator-autonomous-shift-runtime.test.js',
    'static/js/tests/excavator-service-worker-update-runtime.test.js',
    'static/js/tests/excavator-free-bucket-contract.test.js',
    'static/js/tests/excavator-free-bucket-swipe-runtime.test.js',
    'static/js/tests/excavator-shift-reading-confirmation-runtime.test.js',
    'static/js/tests/excavator-face-dump-state-runtime.test.js',
    'static/js/tests/excavator-manual-pickup.test.js',
    'static/js/tests/excavator-hourly-report-runtime.test.js',
    'static/js/tests/excavator-hourly-report-contract.test.js'
)
Invoke-Gate 'node-off-c1-r1' $BackendRoot 'node' (@('--test') + $nodeTests)
Invoke-Gate 'django-check' $BackendRoot $Python @('manage.py', 'check')
Invoke-Gate 'django-migration-drift' $BackendRoot $Python @('manage.py', 'makemigrations', '--check', '--dry-run')

$pythonFiles = @(
    'core/offline_sync.py',
    'core/test_free_bucket_sync.py',
    'core/test_offline_autonomous_shift.py',
    'core/test_offline_sync.py',
    'trips/excavator_hourly_report.py',
    'trips/test_excavator_hourly_report.py',
    'trips/tests.py',
    'trips/views.py'
)
Invoke-Gate 'python-syntax' $BackendRoot $Python (@('-m', 'py_compile') + $pythonFiles)
Invoke-Gate 'node-field-outbox-syntax' $BackendRoot 'node' @('--check', 'static/js/excavator-field-outbox-v1.js')
Invoke-Gate 'node-local-shift-syntax' $BackendRoot 'node' @('--check', 'static/js/excavator-local-shift-v1.js')

$manifestPath = Join-Path $ProductRoot '.github\deploy\production-files.txt'
$manifestLines = Get-Content -Encoding utf8 -LiteralPath $manifestPath
$backendRelative = $BackendRoot.Substring($ProductRoot.Length).TrimStart('\').Replace('\', '/')
$runtimePaths = @(
    "$backendRelative/core/offline_sync.py",
    "$backendRelative/shifts/services.py",
    "$backendRelative/static/js/excavator-field-outbox-v1.js",
    "$backendRelative/static/js/excavator-hourly-report-v1.js",
    "$backendRelative/static/js/excavator-local-shift-v1.js",
    "$backendRelative/templates/includes/excavator_dashboard_source_card.html",
    "$backendRelative/templates/includes/mobile_shift_screen.html",
    "$backendRelative/templates/trips/excavator_work.html",
    "$backendRelative/trips/excavator_hourly_report.py",
    "$backendRelative/trips/views.py",
    "$backendRelative/users/role_apps.py"
)
foreach ($path in $runtimePaths) {
    $count = @($manifestLines | Where-Object { $_ -eq $path }).Count
    if ($count -ne 1) { throw "Manifest count for $path is $count, expected 1" }
}
$duplicates = $manifestLines | Group-Object | Where-Object { $_.Name -and $_.Count -gt 1 }
if ($duplicates) { throw "Duplicate production manifest entries: $($duplicates.Name -join ', ')" }
$migrationEntries = @($manifestLines | Where-Object { $_ -match '/migrations/' })
if ($migrationEntries.Count -ne 0) { throw "Unexpected migrations in production manifest: $($migrationEntries -join ', ')" }
"11 changed runtime files are listed exactly once; duplicate_count=0; migration_count=0" |
    Set-Content -Encoding utf8 -LiteralPath (Join-Path $OutputDir 'manifest.log')

$roleApps = Get-Content -Raw -Encoding utf8 -LiteralPath (Join-Path $BackendRoot 'users\role_apps.py')
$template = Get-Content -Raw -Encoding utf8 -LiteralPath (Join-Path $BackendRoot 'templates\trips\excavator_work.html')
if ($roleApps -notmatch "driver-mobile-shell-v370") { throw 'Driver shell v370 not found in role registry' }
if ($roleApps -notmatch "excavator-mobile-shell-v263") { throw 'Excavator shell v263 not found in role registry' }
if ($template -notmatch "excavator-mobile-shell-v263") { throw 'Excavator shell v263 not found in template' }
"Driver=v370; Excavator=v263" | Set-Content -Encoding utf8 -LiteralPath (Join-Path $OutputDir 'versions.log')

$hashScript = @'
import hashlib, subprocess, sys
base, head, output_path = sys.argv[1:4]
paths = subprocess.check_output(['git', 'diff', '--name-only', '-z', f'{base}..{head}']).decode('utf-8').split('\0')
lines = []
for path in paths:
    if not path:
        continue
    data = subprocess.check_output(['git', 'show', f'{head}:{path}'])
    lines.append(f'{hashlib.sha256(data).hexdigest().upper()} *{path}')
with open(output_path, 'w', encoding='utf-8', newline='\n') as output:
    output.write('\n'.join(lines) + '\n')
'@
$gitHashOutput = Join-Path $OutputDir 'git-bytes-sha256.txt'
& $Python -c $hashScript $ReleaseBase $ExpectedSha $gitHashOutput
if ($LASTEXITCODE -ne 0) { throw 'Git-byte hash generation failed' }

$postgresStatus = Join-Path $OutputDir 'postgresql.status.txt'
$postgresReady = (
    $requestedDbEngine -eq 'postgres' -and
    $requestedPostgresDb -and
    $requestedPostgresDb -match '(?i)(test|qa|ci)'
)
if (-not $postgresReady) {
    @(
        'NOT_RUN',
        'Required: DJANGO_DB_ENGINE=postgres and POSTGRES_DB containing test, qa, or ci.',
        'The runner intentionally does not infer PostgreSQL from DJANGO_DB_NAME and does not touch production.'
    ) | Set-Content -Encoding utf8 -LiteralPath $postgresStatus
    if ($RequirePostgres) { throw 'PostgreSQL was required but an isolated test configuration was not supplied.' }
}
else {
    $savedEngine = $env:DJANGO_DB_ENGINE
    try {
        $env:DJANGO_DB_ENGINE = $requestedDbEngine
        Invoke-Gate 'postgres-vendor' $BackendRoot $Python @(
            'manage.py', 'shell', '-c',
            "from django.db import connection; assert connection.vendor == 'postgresql', connection.vendor; print(connection.vendor)"
        )
        Invoke-Gate 'postgres-concurrency' $BackendRoot $Python @(
            'manage.py', 'test',
            'core.test_offline_sync.OfflineEventPostgreSQLConcurrencyTests',
            'core.test_free_bucket_sync.FreeBucketPostgreSQLConcurrencyTests',
            'core.test_offline_autonomous_shift.AutonomousExcavatorShiftPostgreSQLTests',
            '--noinput'
        )
        'PASS: real Django connection.vendor=postgresql; isolated PostgreSQL concurrency classes completed.' |
            Set-Content -Encoding utf8 -LiteralPath $postgresStatus
    }
    finally {
        $env:DJANGO_DB_ENGINE = $savedEngine
    }
}

@(
    "PASS",
    "candidate=$ExpectedSha",
    "release_base=$ReleaseBase",
    "output=$OutputDir",
    "rendered_shell_sha256=$((Get-FileHash -Algorithm SHA256 -LiteralPath $renderedShell).Hash)",
    "postgres=$((Get-Content -LiteralPath $postgresStatus -First 1))"
) | Set-Content -Encoding utf8 -LiteralPath (Join-Path $OutputDir 'RESULT.txt')

Write-Host "OFF-C1-R1 gates PASS. Evidence: $OutputDir"
