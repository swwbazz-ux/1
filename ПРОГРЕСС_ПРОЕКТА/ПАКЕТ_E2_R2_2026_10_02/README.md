# Пакет доказательств E2-R2

- Кандидат: `60994c919498ca12323d25e4bc0605f8f00efdec`.
- Ветка: `codex/e2-r2-retry-provenance-20261002`.
- E2-R1: `f7936b9f83db0160dcc9070c1b2565d82fb6bc57`.
- Release-base: `3ae7a5690aef92bc53160bcdb8b056bee891d6bb`.
- Задание docs: `9a25efd98d8b5325eaa989af2648751d336a6ea4`.

Пакет дополняет, а не заменяет исходные E2 и E2-R1. Снятие cooldown повторно не реализовывалось.

## Состав

- `raw/e2-r2.diff.gz` — gzip с точным diff единственного code commit R2 относительно E2-R1.
- `raw/source-metadata.json` — точные SHA, среда и ограничения.
- `raw/retry-provenance-contract.json` — машинно-читаемая граница допуска.
- `raw/django-e2-r2-red-before-fix.log` — сохранённый excerpt красного прогона: три `KeyError: retry_recovery`.
- `raw/django-e2-r2-targeted.log` — `293/293 OK`.
- `raw/node-e2-r2-targeted.log` — `48/48 PASS`.
- `raw/mobile-npm-test.log` — `44/44 PASS`.
- `raw/operational-fragment-runtime.log` — `10/10 PASS`.
- `raw/django-users.log` — `610 tests, OK, skipped=3`.
- `raw/django-check-drift.log` — Django check и отсутствие новых миграций.
- [BROWSER_QA_BOUNDARY_AND_COMMANDS.md](BROWSER_QA_BOUNDARY_AND_COMMANDS.md) — точные команды и честная граница сохранённой браузерной проверки R1.
- `SHA256SUMS.txt` — SHA-256 файлов пакета по байтам Git worktree.

## Основные команды R2

Из `СИСТЕМА_MVP/backend`:

```powershell
python manage.py test core.test_offline_sync.OfflineEventSyncTests core.test_free_bucket_sync.FreeBucketServerIntegrationTests trips.tests.ExcavatorWorkServerIntegrationTests trips.test_excavator_qa_simulator.ExcavatorQASimulatorTests
node --test static/js/tests/excavator-field-outbox.test.js static/js/tests/excavator-free-bucket-contract.test.js static/js/tests/excavator-native-capabilities-runtime.test.js
node --test static/js/tests/operational-fragment-runtime.test.js
python manage.py test users
python manage.py check
python manage.py makemigrations --check --dry-run
```

Из `mobile/capacitor-shell`:

```powershell
npm test
```

## Границы

- PostgreSQL: `NOT_RUN`.
- Физический Android/APK/Xiaomi: `NOT_RUN`.
- Браузер для R2 повторно не запускался: зафиксирована граница уже сохранённого E2-R1 run.
- Исправление старта оболочки при продолжающем молчать listener относится к OFF-C1 и в E2 не перенесено.
- PR, merge, VERIFY, deploy и production не выполнялись.
