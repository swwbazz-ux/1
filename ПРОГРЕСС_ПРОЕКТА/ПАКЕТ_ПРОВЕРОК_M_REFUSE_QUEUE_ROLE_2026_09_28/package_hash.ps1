param([string]$PackageRoot = (Split-Path -Parent $MyInvocation.MyCommand.Path))
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $PackageRoot).Path.TrimEnd('\')
$rows = Get-ChildItem -LiteralPath $root -Recurse -File |
  Where-Object {
    $_.FullName -notmatch '[\\/]raw_logs[\\/]' -and
    $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and
    $_.Extension -ne '.pyc'
  } |
  Sort-Object FullName |
  ForEach-Object {
    $relative = $_.FullName.Substring($root.Length + 1).Replace('\','/')
    $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$relative`t$hash"
  }
$manifest = [string]::Join("`n", $rows)
$sha = [System.Security.Cryptography.SHA256]::Create()
try {
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($manifest)
  ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant()
} finally { $sha.Dispose() }
