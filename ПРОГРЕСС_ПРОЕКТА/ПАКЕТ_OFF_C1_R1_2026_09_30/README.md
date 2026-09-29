# OFF-C1-R1: исполняемый доказательный пакет

Пакет относится к отдельному кандидату автономной смены машиниста:

- release base: `f7302f4346f64f5e8bfe89c7a4da232f7caed648`;
- исходная точка OFF-C1 в его истории: `478997edb9579fde27fef33aa7c93a6e9da69a55`;
- итоговый кандидат R1: `6c31e8545fd5f5da727ce077774ab4eaf07fd21b`;
- ветка: `codex/off-c1-r1-autonomous-shift-20260930`;
- оболочки: Driver `v370` без изменений, Excavator `v263`;
- PR, merge, VERIFY и deploy не выполняются этим пакетом.

## Запуск

```powershell
& '.\run_off_c1_r1.ps1' `
  -ProductRoot 'C:\codex-tmp\off-c1-r1-autonomous-shift-20260930' `
  -Python 'C:\codex-tmp\off-c1-autonomous-shift-20260929\СИСТЕМА_MVP\.venv\Scripts\python.exe' `
  -OutputDir "$env:TEMP\off-c1-r1-evidence"
```

Runner сначала требует чистый worktree, точный candidate SHA и ancestry release base. Затем он:

1. принудительно запускает адресные Django-проверки на изолированной SQLite test DB;
2. получает безопасную Django-rendered фикстуру подготовленной оболочки;
3. исполняет 11 Node/runtime-наборов, включая эту фикстуру;
4. выполняет `check`, migration drift, Python/Node syntax;
5. проверяет production manifest и версии оболочек;
6. считает SHA-256 непосредственно от опубликованных Git-байтов `candidate:path`;
7. явно пишет `NOT_RUN`, если реальная изолированная PostgreSQL-конфигурация отсутствует.

Параметры OpenBLAS/OMP ограничены одним потоком, чтобы доказательный прогон не зависел от параллельно запущенных локальных серверов.

## PostgreSQL

PostgreSQL не включается догадкой. Settings читает `POSTGRES_DB`, поэтому пакет требует именно его:

```powershell
$env:DJANGO_DB_ENGINE='postgres'
$env:POSTGRES_DB='off_c1_r1_test'
$env:POSTGRES_USER='<isolated test user>'
$env:POSTGRES_PASSWORD='<secret only in environment>'
$env:POSTGRES_HOST='<isolated test host>'
$env:POSTGRES_PORT='5432'

& '.\run_off_c1_r1.ps1' `
  -ProductRoot '<candidate worktree>' `
  -Python '<isolated venv python>' `
  -RequirePostgres
```

До тестовых операций runner проверяет `connection.vendor == 'postgresql'`. Имя базы обязано содержать `test`, `qa` или `ci`. Значения секретов в лог не выводятся.

## Состав доказательств

- [`C1_ОБЩИЙ_КОНТРАКТ.md`](C1_ОБЩИЙ_КОНТРАКТ.md) — узкая переносимая часть normalizer для Driver;
- [`GIT_BYTES_SHA256.txt`](GIT_BYTES_SHA256.txt) — SHA-256 29 файлов полного candidate diff от release, рассчитанные из Git blobs;
- [`RAW_RESULTS.md`](RAW_RESULTS.md) — команды и фактические итоги текущего прогона;
- [`raw/`](raw/) — журналы повторного запуска опубликованного runner;
- [итоговый отчёт](../ПАСПОРТ_МЕХАНИК_ДОКАЗАТЕЛЬНЫЙ_ОТЧЕТ_OFF_C1_R1_2026_09_30.md).

Safe rendered shell создаётся runner из синтетической test DB, проверяется Node-тестом и хешируется. Сам HTML не публикуется: в нём есть одноразовый CSRF-токен тестовой сессии, который не нужен для воспроизведения. Точная команда его получения — сам runner выше.

## Граница доказательства

SQLite/Node/runtime подтверждают адресные контракты C1–C10. PostgreSQL-конкурентность, CDP на итоговом SHA, установленный APK/Xiaomi, реальная потеря радиосети и production — отдельные проверки и не подменяются этим пакетом.
