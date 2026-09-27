param(
    [Parameter(Mandatory=$true)][string]$ReleaseWorktree,
    [string]$PythonExe = ''
)
$ErrorActionPreference = 'Stop'
$expected = 'f2248cb79c737e98b580e784abea394335a2700b'
$actual = (git -C $ReleaseWorktree rev-parse HEAD).Trim()
if ($actual -ne $expected) { throw "Expected release $expected, got $actual" }
if (-not $PythonExe) {
    $baselineManage = Get-ChildItem 'C:\codex-tmp\ci-green-baseline-20260921' -Recurse -Filter manage.py | Select-Object -First 1
    if ($baselineManage) {
        $PythonExe = Join-Path $baselineManage.Directory.Parent.FullName '.venv\Scripts\python.exe'
    }
}
if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe)) { throw 'Python with project dependencies not found; pass -PythonExe.' }
$managePy = Get-ChildItem -LiteralPath $ReleaseWorktree -Recurse -Filter manage.py | Select-Object -First 1
if (-not $managePy) { throw 'manage.py not found under release worktree.' }
$env:P28_BACKEND = $managePy.Directory.FullName
$previousPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $PythonExe (Join-Path $PSScriptRoot 'run_p28_c1.py')
$ErrorActionPreference = $previousPreference
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$voiceTest = Join-Path $PSScriptRoot 'p28_c1_voice.test.cjs'
node --test $voiceTest
exit $LASTEXITCODE
