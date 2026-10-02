# E2-PG1 — команды воспроизведения

Команды выполнялись из `СИСТЕМА_MVP/backend` test-only worktree `bd6358337d316f3097902657314531eb0f5885f9`.

## Изолированный PostgreSQL

```powershell
py -3.12 -m pip download --only-binary=:all: --no-deps `
  --dest C:\codex-tmp\e2-pg1-postgresql-runtime `
  postgresql-binaries==16.15.0
py -3.12 -m pip install --no-deps `
  --target C:\codex-tmp\e2-pg1-postgresql-runtime\site `
  C:\codex-tmp\e2-pg1-postgresql-runtime\postgresql_binaries-16.15.0-py3-none-win_amd64.whl
```

Далее `initdb` создаёт новый каталог `C:\codex-tmp\e2-pg1-postgresql-runtime\data` с локальным `trust`, сервер запускается только на `127.0.0.1:55439`, а `createdb` создаёт `accounting_e2_pg1`. После прогона:

```powershell
pg_ctl.exe -D C:\codex-tmp\e2-pg1-postgresql-runtime\data -w stop -m fast
```

## Переменные Django

```powershell
$env:DJANGO_DB_ENGINE='postgres'
$env:POSTGRES_DB='accounting_e2_pg1'
$env:POSTGRES_USER='postgres'
$env:POSTGRES_PASSWORD=''
$env:POSTGRES_HOST='127.0.0.1'
$env:POSTGRES_PORT='55439'
$env:POSTGRES_CONN_MAX_AGE='0'
```

## Адресный прогон

```powershell
python manage.py test `
  core.test_offline_sync.OfflineEventSyncTests.test_offline_excavator_load_after_completed_trip_is_accepted_at_all_old_boundaries `
  core.test_offline_sync.OfflineEventSyncTests.test_legacy_post_unload_cooldown_conflict_replays_original_event_once `
  core.test_offline_sync.OfflineEventSyncTests.test_removed_cooldown_replays_saved_dependency_chain_with_original_envelopes `
  core.test_offline_sync.OfflineEventSyncTests.test_removed_cooldown_child_stays_replayable_until_root_is_accepted `
  core.test_offline_sync.OfflineEventSyncTests.test_removed_cooldown_chain_rejects_unrelated_dependency_and_envelope_change `
  core.test_offline_sync.OfflineEventSyncTests.test_removed_cooldown_root_provenance_survives_concurrent_retry `
  core.test_offline_sync.OfflineEventSyncTests.test_removed_cooldown_child_provenance_survives_temporary_server_error `
  core.test_offline_sync.OfflineEventSyncTests.test_removed_cooldown_child_provenance_survives_trip_reference_pending `
  core.test_offline_sync.OfflineEventPostgreSQLConcurrencyTests.test_removed_cooldown_root_parallel_retries_create_one_effect `
  core.test_offline_sync.OfflineEventPostgreSQLConcurrencyTests.test_removed_cooldown_root_and_child_overlap_then_converge `
  --verbosity 2
```

Ожидаемая test DB — `test_accounting_e2_pg1`. Успешный итог сохранён в raw: `Ran 10 tests`, `OK`, `EXIT_CODE=0`, без skip.

