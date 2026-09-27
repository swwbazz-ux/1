param(
    [Parameter(Mandatory=$true)][string]$SourceRoot
)
$ErrorActionPreference = 'Stop'
$expected = 'f2248cb79c737e98b580e784abea394335a2700b'
$actual = (git -C $SourceRoot rev-parse HEAD).Trim()
if ($actual -ne $expected) { throw "Expected $expected, got $actual" }
$dirty = git -C $SourceRoot status --porcelain
if ($dirty) { throw "Source worktree is dirty: $dirty" }
$managePy = Get-ChildItem -LiteralPath $SourceRoot -Recurse -Filter manage.py | Select-Object -First 1
if (-not $managePy) { throw 'manage.py not found.' }
$backend = $managePy.Directory.FullName
$python = Join-Path $SourceRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $baselineManage = Get-ChildItem 'C:\codex-tmp\ci-green-baseline-20260921' -Recurse -Filter manage.py | Select-Object -First 1
    if ($baselineManage) { $python = Join-Path $baselineManage.Directory.Parent.FullName '.venv\Scripts\python.exe' }
}
if (-not (Test-Path $python)) { throw 'Python with project dependencies not found.' }
$env:P28_R1_BACKEND = $backend
$env:P28_R1_SOURCE_SHA = $actual
$env:P28_R1_SOURCE_CLEAN = 'true'
$env:P28_R1_LOG = Join-Path $PSScriptRoot 'raw-run-utf8.log'
$env:PYTHONUTF8 = '1'
& $python (Join-Path $PSScriptRoot 'run_p28_c1_r1.py')
exit $LASTEXITCODE
