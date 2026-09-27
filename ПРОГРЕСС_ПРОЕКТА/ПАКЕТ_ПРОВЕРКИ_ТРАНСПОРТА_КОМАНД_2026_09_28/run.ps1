param(
    [Parameter(Mandatory = $true)]
    [string]$ReleaseWorktree
)

$ErrorActionPreference = "Stop"
$ExpectedSha = "0fe60543de59bf7cbeca4868fc17e8e00689d2ba"
$ActualSha = (git -C $ReleaseWorktree rev-parse HEAD).Trim()
if ($ActualSha -ne $ExpectedSha) {
    throw "Expected release SHA $ExpectedSha, got $ActualSha"
}

$Backend = Get-ChildItem -LiteralPath $ReleaseWorktree -Directory |
    ForEach-Object { Join-Path $_.FullName "backend" } |
    Where-Object { Test-Path -LiteralPath $_ } |
    Select-Object -First 1
if (-not $Backend) {
    throw "Backend directory not found under $ReleaseWorktree"
}

$env:PASSPORT_BACKEND = $Backend
node --test (Join-Path $PSScriptRoot "transport_commands_evidence.test.cjs")
if ($LASTEXITCODE -ne 0) {
    throw "Node evidence package failed with exit code $LASTEXITCODE"
}
