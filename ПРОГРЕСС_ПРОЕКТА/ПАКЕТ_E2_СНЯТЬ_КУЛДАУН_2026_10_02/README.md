# Исполняемый пакет E2 — снятие post-unload cooldown

Дата фиксации: 02.10.2026.

## Неизменяемая основа

- release base: `f1b144e8b40b5c4fca9182a416a2de48abbac1e0`;
- candidate head: `efb39c9a7d41df10564ce0fb4f803c9d129117b4`;
- ветка: `codex/e2-remove-post-unload-cooldown-20261002`;
- версия Excavator, согласованная с координатором: `v264`;
- Driver и PR №145 не изменены.

## Содержимое

- `raw/0001-fix-post-unload-cooldown.patch` — воспроизводимый patch кандидата;
- `raw/candidate-scope.txt` — base/head, список и статистика файлов;
- `raw/django-targeted.log` — адресные Django-проверки, `285/285 PASS`;
- `raw/node-targeted.log` — адресные Node-проверки, `44/44 PASS`;
- `raw/django-check-drift.log` — `manage.py check` и отсутствие migration drift;
- `raw/manifest-check.log` — 131 manifest entry, отсутствие дублей/миграций и точное включение runtime-файлов;
- `raw/node-full.log` — широкий Node-прогон: `949 PASS`, `1 SKIP`, `1 FAIL` в незатронутом Driver-контракте;
- `raw/browser-qa-observations.json` — фактическая DOM/CDP и серверная временная шкала;
- `raw/source-metadata.json` — происхождение кандидата и команды;
- `raw/postgresql-NOT_RUN.txt`, `raw/apk-xiaomi-NOT_RUN.txt` — явно не выполненные проверки;
- `SHA256SUMS.txt` — хеши файлов пакета.

## Команды

Из `СИСТЕМА_MVP/backend` кандидата:

```powershell
python manage.py test trips.tests.ExcavatorWorkServerIntegrationTests core.test_offline_sync.OfflineEventSyncTests core.test_free_bucket_sync.FreeBucketServerIntegrationTests trips.test_excavator_qa_simulator.ExcavatorQASimulatorTests --verbosity 1
node --test static/js/tests/excavator-field-outbox.test.js static/js/tests/excavator-free-bucket-contract.test.js static/js/tests/excavator-native-capabilities-runtime.test.js
python manage.py check
python manage.py makemigrations --check --dry-run
node --test "static/js/tests/*.test.js"
```

## Граница доказательств

Использована изолированная SQLite-база и локальный браузерный стенд кандидата. Жест погрузки в браузере был отправлен синтетическим `PointerEvent` через CDP в реальном runtime страницы: это проверяет DOM, очередь и серверный путь, но не заменяет физический жест на APK/Xiaomi. PostgreSQL, APK/Xiaomi, CI, production, merge, VERIFY и deploy не выполнялись.
