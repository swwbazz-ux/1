param(
    [string]$DocsRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$OutputPath = (Join-Path $PSScriptRoot 'raw-run-r1-utf8.log')
)
$ErrorActionPreference = 'Stop'
$requiredBase = '8d8948647d22ecefc69b0ef214a665344637cd9b'
$releaseSha = 'f2248cb79c737e98b580e784abea394335a2700b'
$head = (git -C $DocsRoot rev-parse HEAD).Trim()
git -C $DocsRoot merge-base --is-ancestor $requiredBase $head
if ($LASTEXITCODE -ne 0) { throw "Docs HEAD $head does not descend from required base $requiredBase" }
$dirty = @(git -C $DocsRoot status --porcelain=v1 --untracked-files=all)
if ($dirty.Count -gt 0) { throw "Worktree is not clean (tracked/staged/untracked): $($dirty -join '; ')" }
$env:P28_I1_R1_REQUIRED_BASE = $requiredBase
$env:P28_I1_R1_ACTUAL_HEAD = $head
$env:P28_I1_R1_RELEASE_SHA = $releaseSha
$env:P28_I1_R1_CLEAN_GATE = 'tracked-staged-untracked-clean'
$env:P28_I1_R1_LOG = $OutputPath
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
python (Join-Path $PSScriptRoot 'run_tests.py')
$testExit = $LASTEXITCODE
if ($testExit -ne 0) { exit $testExit }
$acceptanceLog = Join-Path $PSScriptRoot 'acceptance-replay-r1-utf8.log'
python (Join-Path $PSScriptRoot 'run_acceptance_probe.py') $acceptanceLog
exit $LASTEXITCODE
