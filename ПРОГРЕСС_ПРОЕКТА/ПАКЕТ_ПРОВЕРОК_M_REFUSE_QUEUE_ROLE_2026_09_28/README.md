# Пакет проверок M-REFUSE-1, M-QUEUE-1, M-ROLE-1

Пакет не меняет бизнес-код. Он подключается к указанному checkout через `PYTHONPATH` и запускает изолированные Django test databases и Node runtime-тест очереди.

SQLite и Node:

```powershell
& .\run_sqlite.ps1 -SourceRoot C:\codex-tmp\passport-proof-pr-1754269 -PythonExe '<python-with-requirements>'
```

PostgreSQL (учётной записи нужны права создания test database):

```powershell
$env:POSTGRES_DB='passport_probe'
$env:POSTGRES_USER='passport_probe'
$env:POSTGRES_PASSWORD='<secret>'
$env:POSTGRES_HOST='127.0.0.1'
$env:POSTGRES_PORT='5432'
& .\run_postgresql.ps1 -SourceRoot C:\codex-tmp\passport-proof-pr-1754269 -PythonExe '<python-with-requirements>'
```

Скрипт PostgreSQL сначала доказывает `connection.vendor == 'postgresql'`, поэтому SQLite не может быть ошибочно выдан за требуемый прогон.
