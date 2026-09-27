param(
  [Parameter(Mandatory=$true)][string]$BackendRoot,
  [Parameter(Mandatory=$true)][string]$ExpectedSha,
  [string]$PythonExe = 'python',
  [string]$RawLogPath = ''
)
$ErrorActionPreference = 'Stop'
$backend = (Resolve-Path -LiteralPath $BackendRoot).Path
if (-not (Test-Path -LiteralPath (Join-Path $backend 'manage.py'))) { throw 'BackendRoot must point to the directory containing manage.py' }
$repo = (& git -C $backend rev-parse --show-toplevel).Trim()
$actualSha = (& git -C $repo rev-parse HEAD).Trim()
if ($actualSha -ne $ExpectedSha) { throw "Source SHA mismatch: expected=$ExpectedSha actual=$actualSha" }
$dirty = (& git -C $repo status --porcelain --untracked-files=no)
if ($dirty) { throw 'Tracked source worktree is dirty' }
$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$packageHash = (& (Join-Path $packageRoot 'package_hash.ps1') -PackageRoot $packageRoot).Trim()
if (-not $RawLogPath) { $RawLogPath = Join-Path $packageRoot "raw_logs/$actualSha-sqlite.log" }
New-Item -ItemType Directory -Force (Split-Path -Parent $RawLogPath) | Out-Null
$testDb = Join-Path ([System.IO.Path]::GetTempPath()) "passport_r2_test_$($actualSha.Substring(0,12)).sqlite3"
$env:DJANGO_DB_ENGINE = 'sqlite'
$env:DJANGO_SETTINGS_MODULE = 'passport_probes.settings_sqlite'
$env:PASSPORT_EXPECTED_VENDOR = 'sqlite'
$env:PASSPORT_TEST_DB = $testDb
$env:PASSPORT_SOURCE_SHA = $actualSha
$env:PASSPORT_PACKAGE_SHA256 = $packageHash
$env:PYTHONPATH = "$packageRoot;$backend"
$env:PASSPORT_BACKEND = $backend
@(
  'PASSPORT_R2_RUNNER=sqlite', "SOURCE_SHA=$actualSha",
  'SOURCE_TRACKED_CLEAN=true', "PACKAGE_SHA256=$packageHash",
  'DJANGO_DB_ENGINE=sqlite', 'EXPECTED_VENDOR=sqlite', "TEST_DATABASE=$testDb"
) | Set-Content -LiteralPath $RawLogPath -Encoding utf8
Push-Location $backend
try {
  $ErrorActionPreference = 'Continue'
  & $PythonExe manage.py test `
    passport_probes.django_probes_r2.PassportDowntimeR2Probe.test_disputed_start_exact_scope_and_idempotency `
    passport_probes.django_probes_r2.DispatcherMutationFixture `
    passport_probes.django_probes_r2.StaleAssignPlacementR2Probe.test_stale_assign_activates_existing_inactive_placement_before_409 `
    passport_probes.django_probes_r2.StaleAssignPlacementR2Probe.test_stale_assign_creates_missing_placement_before_409 `
    --settings passport_probes.settings_sqlite --verbosity 2 2>&1 | Tee-Object -FilePath $RawLogPath -Append
  $djangoCode = $LASTEXITCODE
  "DJANGO_EXIT_CODE=$djangoCode" | Tee-Object -FilePath $RawLogPath -Append
  if ($djangoCode -ne 0) { throw "Django R2 probes failed: $djangoCode" }
  node --test $packageRoot/queue_master_runtime_r2.test.js 2>&1 | Tee-Object -FilePath $RawLogPath -Append
  $nodeCode = $LASTEXITCODE
  "NODE_EXIT_CODE=$nodeCode" | Tee-Object -FilePath $RawLogPath -Append
  if ($nodeCode -ne 0) { throw "Node R2 probes failed: $nodeCode" }
} finally {
  $ErrorActionPreference = 'Stop'
  Pop-Location
  Remove-Item -LiteralPath $testDb -Force -ErrorAction SilentlyContinue
}
