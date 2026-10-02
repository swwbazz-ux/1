# Пакет доказательств E2-R1

Кандидат: `f7936b9f83db0160dcc9070c1b2565d82fb6bc57`.

Ветка: `codex/e2-r1-dependency-recovery-20261002`.

Release base: `3ae7a5690aef92bc53160bcdb8b056bee891d6bb`.

Задание docs: `ddc15b0e5d50ba03aaf67d455d0eab882435ef7a`

Пакет дополняет, но не заменяет исходные E2-отчёт и пакет. Cooldown повторно не реализован.

## Состав

- `raw/0001-fix-recover-removed-refusal-dependency-chains.patch` — patch R1 поверх перенесённого исходного E2.
- `raw/source-metadata.json` — SHA, ветки, версии и среда.
- `raw/django-e2-r1-targeted.log` — `290/290 OK`.
- `raw/node-e2-r1-targeted.log` — `47/47 PASS`.
- `raw/mobile-npm-test.log` — `44/44 PASS`.
- `raw/operational-fragment-runtime.log` — `10/10 PASS`.
- `raw/django-users.log` — 610 tests, OK, 3 skipped.
- `raw/django-check-drift.log` — check и отсутствие новых миграций.
- `raw/silent-port.log` — журнал listener, реально молчавшего после TCP accept.
- `raw/silent-port-browser-qa.json` — браузерная временная шкала жеста, restart и восстановления сети.
- `raw/silent-port-db-after.json` — точные receipt/trip/action после досылки.
- `tools/silent_http_port.ps1` — воспроизводимый silent-port listener.
- `raw/driver-drum-base-f1b144e8.log` и `raw/driver-drum-candidate-efb39c9a.log` — одинаковый Driver fail.
- `raw/driver-baseline-comparison.json` — машинно-читаемое сравнение.
- `SHA256SUMS.txt` — SHA-256 файлов пакета, вычисленные по нормализованным байтам Git blob и не зависящие от локального LF/CRLF checkout.

## Основные команды

Из `СИСТЕМА_MVP/backend` с проектным Python:

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

Молчащий порт:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\silent_http_port.ps1 -Port 8765 -LogPath .\raw\silent-port.log
```

Listener принимает TCP-соединения и намеренно не отвечает HTTP. Для полного сценария нужны изолированная QA-БД, подготовленная смена/назначение и PWA-экран; production использовать нельзя.

## Границы

- PostgreSQL: `NOT_RUN`.
- Физический Android/APK/Xiaomi: `NOT_RUN`.
- Production, merge, VERIFY и deploy: не выполнялись.
- Driver UI: не менялся.
