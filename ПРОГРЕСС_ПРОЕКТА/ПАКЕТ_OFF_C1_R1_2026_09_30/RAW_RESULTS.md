# OFF-C1-R1: фактические результаты до публикации отчёта

Точная проверяемая версия: `6c31e8545fd5f5da727ce077774ab4eaf07fd21b`.

| Команда / группа | Фактический результат |
|---|---|
| `manage.py test core.test_offline_sync core.test_free_bucket_sync core.test_offline_autonomous_shift --noinput` | `Ran 152 tests`; `OK (skipped=8)` на SQLite. |
| `manage.py test trips.test_excavator_hourly_report trips.tests.ExcavatorWorkServerIntegrationTests --noinput` | `Ran 148 tests`; `OK`. |
| safe rendered-shell test | `Ran 1 test`; `OK`; файл 418421 байт, SHA-256 итогового runner-прогона `23E73E9FE3504D6863DB4533C28FE3211E3897BDB3F8366F74594473FF6582C4`. |
| 11 адресных Node-файлов | `111/111 PASS`, `0 SKIP`, `0 FAIL`. |
| `manage.py check` | PASS, ошибок не найдено. |
| `makemigrations --check --dry-run` | PASS, `No changes detected`. |
| `py_compile` затронутых Python-файлов | PASS. |
| `node --check` двух изменённых runtime-модулей | PASS. |
| production manifest | 11 изменённых runtime-файлов ровно по одному разу; дублей `0`; migrations `0`. |
| версии | Driver `v370`; Excavator `v263`. |
| PostgreSQL | `NOT_RUN`: переменные изолированной PostgreSQL test DB отсутствуют; `psql`, Docker, Podman и PostgreSQL service не найдены. |
| CDP/browser, APK/Xiaomi, радиосеть, production | `NOT_RUN`. |

Ожидаемый `RuntimeError: forced rollback` в группе `trips.tests.ExcavatorWorkServerIntegrationTests` принадлежит существующему тесту отката; тест и вся группа проходят.

Первый объединённый запуск был остановлен ОС сообщением `OpenBLAS Memory allocation failed`, пока два чужих Driver-сервера занимали память. Процессы не останавливались. Повтор с `OPENBLAS_NUM_THREADS=1` и `OMP_NUM_THREADS=1` полностью прошёл; это ограничение закреплено в runner и не классифицируется как дефект продукта.

Сам опубликованный PowerShell runner после двух исправлений переносимости (кириллический path literal в Windows PowerShell 5 и stderr нативных команд) выполнен целиком и завершён `PASS`; их критерии тестов не менялись.
