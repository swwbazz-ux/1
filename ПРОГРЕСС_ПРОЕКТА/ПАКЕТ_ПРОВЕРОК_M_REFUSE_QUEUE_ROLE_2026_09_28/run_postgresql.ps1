param(
  [Parameter(Mandatory=$true)][string]$SourceRoot,
  [string]$PythonExe = 'python'
)
$ErrorActionPreference = 'Stop'
$required = 'POSTGRES_DB','POSTGRES_USER','POSTGRES_PASSWORD','POSTGRES_HOST','POSTGRES_PORT'
foreach ($name in $required) {
  if (-not [Environment]::GetEnvironmentVariable($name)) { throw "Required environment variable is missing: $name" }
}
$managePy = Get-ChildItem -LiteralPath $SourceRoot -Recurse -Filter manage.py | Select-Object -First 1
if (-not $managePy) { throw "manage.py not found under $SourceRoot" }
$backend = $managePy.Directory.FullName
$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONPATH = "$packageRoot;$backend"
New-Item -ItemType Directory -Force (Join-Path $backend 'media/employee_photos') | Out-Null
Push-Location $backend
try {
  & $PythonExe -c "import django,os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup(); from django.db import connection; connection.ensure_connection(); print('DB_VENDOR='+connection.vendor); assert connection.vendor == 'postgresql'"
  if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL connection verification failed' }
  & $PythonExe manage.py test passport_probes.django_probes.PassportDowntimePostgreSQLProbe.test_disputed_start_is_idempotent_and_preserves_action_time --verbosity 2
  if ($LASTEXITCODE -ne 0) { throw "PostgreSQL probe failed: $LASTEXITCODE" }
} finally { Pop-Location }
