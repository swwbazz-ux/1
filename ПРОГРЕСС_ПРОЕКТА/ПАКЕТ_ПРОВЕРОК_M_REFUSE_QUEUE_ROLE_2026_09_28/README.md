# Пакет проверок R2: M-REFUSE-1, M-QUEUE-1, M-ROLE-1

Пакет не меняет бизнес-код. Он подключается через `PYTHONPATH` к точно указанному checkout, требует полный ожидаемый SHA, отклоняет tracked-изменения и запускает изолированную test database. Внутри каждого Django-теста проверяются и печатаются source SHA, SHA-256 пакета, `connection.vendor` и имя тестовой БД.

## SQLite и исполняемый Node-тест очереди

```powershell
& .\run_sqlite.ps1 `
  -BackendRoot 'C:\codex-tmp\passport-proof-pr-1754269\СИСТЕМА_MVP\backend' `
  -ExpectedSha '1754269aa1858784f62177bc229ae18e392a67e3' `
  -PythonExe 'C:\path\to\python.exe'
```

SQLite создаётся во временном файле с отдельным именем для каждого SHA и удаляется после прогона. Это вспомогательная проверка, а не доказательство PostgreSQL.

## PostgreSQL

Нужны отдельная непроизводственная база и учётная запись с правом `CREATEDB`. Django создаст тестовую БД с именем `passport_r2_test_<12 символов SHA>` и удалит её штатным test runner.

```powershell
$env:POSTGRES_DB='passport_probe_base'
$env:POSTGRES_USER='passport_probe_runner'
$env:POSTGRES_PASSWORD='<secret>'
$env:POSTGRES_HOST='127.0.0.1'
$env:POSTGRES_PORT='5432'
& .\run_postgresql.ps1 `
  -BackendRoot 'C:\codex-tmp\passport-proof-pr-1754269\СИСТЕМА_MVP\backend' `
  -ExpectedSha '1754269aa1858784f62177bc229ae18e392a67e3' `
  -PythonExe 'C:\path\to\python.exe'
```

Runner до импорта Django settings задаёт `DJANGO_DB_ENGINE=postgres`, затем проверяет реальное соединение и `connection.vendor == 'postgresql'`. Секрет в raw log не пишется. Без успешного соединения результат обязан называться `NOT_RUN`, а не PostgreSQL PASS.

## Выходные доказательства

- `raw_logs/<source-sha>-sqlite.log` или `raw_logs/<source-sha>-postgresql.log` — сырой вывод без пароля;
- строки `EVIDENCE_R2 ...` — предметные результаты;
- `PACKAGE_SHA256` — хеш исполняемого содержимого пакета без `raw_logs`, `__pycache__` и `.pyc`;
- Node-тест исполняет функции очереди, извлечённые из фактического `dispatcher_control.html`. Он фиксирует вызов пользовательского уведомления и refresh-функций, но не доказывает отрисовку DOM или поведение установленного приложения.
