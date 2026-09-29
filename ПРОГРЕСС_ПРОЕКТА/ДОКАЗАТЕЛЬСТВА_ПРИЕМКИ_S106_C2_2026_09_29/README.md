# Собственные доказательства приёмки S106-C2

Источник runtime: candidate `f238aa51fbb80909d5e7f9e84e49b67f1ee5184e`. Источник исходного пакета: docs `6195b82c9e1e8acf6f5d1616c946a524df9ce6ab`.

Астра выполнила на Node v24.19.0 неизменённые тесты:

```bash
node --test СИСТЕМА_MVP/backend/static/js/tests/driver-offline-outbox-v2.test.js СИСТЕМА_MVP/backend/static/js/tests/excavator-field-outbox.test.js
```

Для запуска получены только два runtime-модуля, два теста и `core/fixtures/offline_replay_contract.json`. Все пять Git blob SHA совпали с кандидатом. Результат: 79/79 PASS, 0 SKIP, exit 0. Приложение/DOM/телефон/Django/PostgreSQL локально этим прогоном не запускались; HTTP и хранилище в тестах замещены тестовыми реализациями.

- `client.stdout.log`, `client.stderr.log` — собственные исходные потоки без нормализации.
- `source-check-result.json` — Git blob сверка пяти исходников и двух приложений пользователя с опубликованными файлами; локальные пути — provenance проверки.
- `published-manifest-check.json` — независимая сверка всех 38 SHA-256 исходного пакета и 39 Git blobs с manifest.
- `raw-manifest-comparison.json` — отдельная сверка внутреннего списка рабочих хешей; не путать с внешним Git manifest. Конверсионные сопоставления диагностические, не восстановленные оригиналы.
- `SHA256SUMS.json` — хеши файлов этого дополнения; собственный manifest исключён из самоподсчёта.

Live CI [36564441695](https://github.com/swwbazz-ux/1/actions/runs/36564441695) проверен чтением job logs: harness `05399cb…`, actual checkout/target `f238aa51…`, PostgreSQL 9/9 без skip, JavaScript/mobile/Android/summary success. Повторный CI не запускался. Полные ограничения и статус — [приёмка Астры](../ПАСПОРТ_МЕХАНИК_ПРИЕМКА_S106_C2_2026_09_29.md).
