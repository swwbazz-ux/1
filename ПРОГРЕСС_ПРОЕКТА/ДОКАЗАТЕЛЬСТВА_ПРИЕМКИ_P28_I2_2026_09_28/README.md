# Собственные доказательства приёмки P28-I2

Дата: 28.09.2026. Кандидат `e1cdef3c319889662985a65909726130a596a3c6`; release base `9869b29348cae038a88abbde1d8f75bd8ab99dd4`; исходный docs `6ada74a87e9fa356f22b39f06c69c6e684760d34`.

Результат: 13 тестов адаптера и 4 существующих handler-теста прошли. Три дополнительных приёмочных ожидания не выполнены: подтверждённый конфликт ID теряет диагностику, local_trip_id запись не находится, поздняя повторная погрузка ошибочно объявляется исходной. Всего **20 тестов, 3 failures, exit 1**. `acceptance-django.log` содержит полный stdout/stderr, SHA, чистоту source checkout, vendor и временную DB.

## Воспроизведение

Нужны отдельная чистая копия кандидата и зависимости Django backend. Для исходного результата checkout должен быть точно на e1cdef3c. Для проверки исправления указать его отдельный чистый checkout; actual HEAD печатается в журнал. Скрипты не вносят правки в кандидат.

Установить переменную окружения `P28_I2_CANDIDATE_ROOT` в путь к этой копии. Запустить из папки доказательств:

```text
python run_isolated.py trips.test_route_projection_adapter core.test_offline_sync.OfflineEventSyncTests.test_dump_point_a_to_b_to_a_keeps_distinct_ordered_events core.test_offline_sync.OfflineEventSyncTests.test_dump_point_current_choice_is_accepted_without_business_change core.test_offline_sync.OfflineEventSyncTests.test_dump_point_change_and_dependent_unload_complete_same_exact_trip core.test_offline_sync.OfflineEventSyncTests.test_late_equal_timestamp_dump_point_change_cannot_roll_back_newer_state probe_adapter.AdapterAcceptanceProbes probe_load_origin.LateLoadOriginProbe
```

`run_isolated.py` принудительно задаёт SQLite `:memory:` до открытия connection, отказывается от `.env` в checkout, использует локальный cache и отдельный runtime MEDIA_ROOT. Проверяет чистоту source checkout и реальный vendor. Django миграции выполняются только в одноразовой test DB и удаляются с нею. Несовместимые ожидания намеренно оформлены assert: exit 1 исходного кандидата является доказательством замечаний, а не PASS.

## Состав и происхождение

- `probe_adapter.py`: конфликт ID через настоящий HTTP sync; ожидающая local_trip_id запись также через HTTP. Появившаяся подтверждённая привязка погрузки во втором сценарии — явно обозначенная DB-фикстура; настоящий `_resolve_trip_reference` используется как контроль.
- `probe_load_origin.py`: исходная фабричная погрузка A, сменщик B, следующий Y; поздний дубль проходит настоящий `process_offline_batch`/`same_load`. Receipt не подделывается. Подготовка смен/назначений — изолированная фикстура.
- `manifest-verification.json`: собственная проверка 9/9 хешей пакета исполнителя, трёх файлов checkout и Git blob неизменённого ядра.
- `python-environment.txt`: фактически установленные зависимости проверки, не изменение requirements проекта.
- `environment-initial-check.log`: первый подготовительный запуск остановился до исполнения тестов из-за отсутствующего MEDIA_ROOT. Затем внешнему runner назначена временная media-директория; system check не отключался.
- `SHA256SUMS.txt`: хеши файлов этой папки, кроме самого manifest.

NOT_RUN: PostgreSQL, конкурентный writer, PowerShell, DOM/озвучка, телефон, production/постоянный QA. Pure-Python 30/30 R1 в этом шаге заново не запускались: принятие прежнее и blob ядра совпадает; опубликованный повтор исполнителя сохранён в его пакете. Рабочее приложение, release, PR №106/№120 и production не менялись; merge/deploy не выполнялись.
