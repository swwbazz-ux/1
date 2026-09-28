param([string]$DocsRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path)
$ErrorActionPreference = 'Stop'
$expectedDocs = '1226645bfb9d177e822b1de7526ea93a54a8da36'
$expectedRelease = 'f2248cb79c737e98b580e784abea394335a2700b'
$head = (git -C $DocsRoot rev-parse HEAD).Trim()
git -C $DocsRoot merge-base --is-ancestor $expectedDocs $head
if ($LASTEXITCODE -ne 0) { throw "Docs HEAD $head does not descend from required base $expectedDocs" }
$trackedDirty = git -C $DocsRoot diff --name-only
if ($trackedDirty) { throw "Unexpected tracked changes before package run: $trackedDirty" }
$env:P28_I1_DOCS_SHA = $expectedDocs
$env:P28_I1_RELEASE_SHA = $expectedRelease
$env:P28_I1_SOURCE_CLEAN = 'package-only'
$env:P28_I1_LOG = Join-Path $PSScriptRoot 'raw-run-utf8.log'
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
python (Join-Path $PSScriptRoot 'run_tests.py')
exit $LASTEXITCODE
