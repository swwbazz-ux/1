# Пакет E2-PG1

Пакет подтверждает узкий PostgreSQL gate для E2 на test-only commit `bd6358337d316f3097902657314531eb0f5885f9` поверх принятого E2-R2 `60994c919498ca12323d25e4bc0605f8f00efdec`.

## Состав

- [RUN_COMMANDS.md](RUN_COMMANDS.md) — воспроизводимая подготовка и точная команда теста;
- [raw/django-e2-pg1-postgresql.stdout.log](raw/django-e2-pg1-postgresql.stdout.log) — stdout;
- [raw/django-e2-pg1-postgresql.stderr.log](raw/django-e2-pg1-postgresql.stderr.log) — stderr и десять имён тестов;
- [raw/django-e2-pg1-postgresql.exit.log](raw/django-e2-pg1-postgresql.exit.log) — exit code;
- [raw/django-postgresql-identity.log](raw/django-postgresql-identity.log) — `connection.vendor`, configured DB и имя test DB;
- [raw/postgresql-version.log](raw/postgresql-version.log) — версия сервера;
- [raw/postgresql-package-sha256.log](raw/postgresql-package-sha256.log) — хеш бинарного пакета;
- [raw/postgres-server.log](raw/postgres-server.log) — журнал одноразового сервера;
- [raw/postgres-stop.log](raw/postgres-stop.log) — подтверждение остановки;
- [raw/source-sha.log](raw/source-sha.log), [raw/source-parent-sha.log](raw/source-parent-sha.log) — проверенный test-only SHA и родитель E2-R2;
- [SHA256SUMS.txt](SHA256SUMS.txt) — хеши опубликованного raw.

Итог: `10 PASS`, `0 FAIL`, `0 SKIP`, exit `0`. SQLite, production и постоянная БД не использовались.

