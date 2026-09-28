param(
    [Parameter(Mandatory = $true)]
    [string]$CandidateRoot,
    [string]$DjangoPython = 'python',
    [string]$PurePython = 'python'
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = 'utf-8'
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
$env:NUMEXPR_NUM_THREADS = '1'

$ExpectedHead = 'e1cdef3c319889662985a65909726130a596a3c6'
$ExpectedBase = '9869b29348cae038a88abbde1d8f75bd8ab99dd4'
$ExpectedCoreBlob = '64b2519fb8bb0874012577fd2d779d7b9a619b74'
$ManagePy = Get-ChildItem -LiteralPath $CandidateRoot -Recurse -File -Filter 'manage.py' |
    Select-Object -First 1
if ($null -eq $ManagePy) { throw 'manage.py not found under candidate root' }
$Backend = $ManagePy.DirectoryName
$CoreFile = Get-ChildItem -LiteralPath $CandidateRoot -Recurse -File -Filter 'route_projection_core.py' |
    Select-Object -First 1
if ($null -eq $CoreFile) { throw 'route_projection_core.py not found under candidate root' }
$ProgressRoot = Split-Path -Parent $PSScriptRoot
$R1Test = Get-ChildItem -LiteralPath $ProgressRoot -Recurse -File -Filter 'test_route_core_r1.py' |
    Select-Object -First 1
if ($null -eq $R1Test) { throw 'P28_I1_R1 source package not found' }
$R1Root = $R1Test.DirectoryName

function Invoke-NativeStep {
    param(
        [string]$Label,
        [string]$Executable,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )
    Write-Output "=== $Label ==="
    Push-Location -LiteralPath $WorkingDirectory
    try {
        & $Executable @Arguments
        $ExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    Write-Output "EXIT=$ExitCode"
    if ($ExitCode -ne 0) {
        throw "$Label failed with exit $ExitCode"
    }
}

$ActualHead = (& git -C $CandidateRoot rev-parse HEAD).Trim()
$MergeBase = (& git -C $CandidateRoot merge-base HEAD $ExpectedBase).Trim()
$CoreBlob = (& git -C $CandidateRoot hash-object -- $CoreFile.FullName).Trim()
if ($ActualHead -ne $ExpectedHead) { throw "Unexpected candidate HEAD: $ActualHead" }
if ($MergeBase -ne $ExpectedBase) { throw "Expected release is not candidate base: $MergeBase" }
if ($CoreBlob -ne $ExpectedCoreBlob) { throw "Transferred core blob changed: $CoreBlob" }

Invoke-NativeStep 'P28-I2 adapter tests' $DjangoPython @(
    'manage.py', 'test', 'trips.test_route_projection_adapter', '--verbosity', '1'
) $Backend

Invoke-NativeStep 'Existing route handler tests' $DjangoPython @(
    'manage.py', 'test',
    'core.test_offline_sync.OfflineEventSyncTests.test_dump_point_a_to_b_to_a_keeps_distinct_ordered_events',
    'core.test_offline_sync.OfflineEventSyncTests.test_dump_point_current_choice_is_accepted_without_business_change',
    'core.test_offline_sync.OfflineEventSyncTests.test_dump_point_change_and_dependent_unload_complete_same_exact_trip',
    'core.test_offline_sync.OfflineEventSyncTests.test_late_equal_timestamp_dump_point_change_cannot_roll_back_newer_state',
    '--verbosity', '1'
) $Backend

Invoke-NativeStep 'Django system check' $DjangoPython @('manage.py', 'check') $Backend
Invoke-NativeStep 'Migration drift check' $DjangoPython @(
    'manage.py', 'makemigrations', '--check', '--dry-run'
) $Backend
Invoke-NativeStep 'Transferred R1 core tests' $PurePython @(
    '-m', 'unittest', 'discover', '-s', '.', '-p', 'test_*.py', '-q'
) $R1Root

Write-Output 'P28-I2_REPLAY_OK'
