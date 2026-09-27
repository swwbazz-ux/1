# Кандидат атомарного stale assign — M-ASSIGN-1, M-CONTROL-1

Дата: 28.09.2026. Основание: § 16.1 [разбора R2](ПАСПОРТ_МЕХАНИК_РАЗБОР_РЕЦЕНЗИИ_РОЛИ_2026_09_28.md). Статус: узкий технический кандидат; не release и не разрешение на merge/deploy.

## 1. Идентичность кандидата

- база: актуальный на начало работы commit release-ветки `codex/github-production-deploy-20260916` — `0fe60543de59bf7cbeca4868fc17e8e00689d2ba`;
- отдельная ветка: `codex/stale-assign-atomic-20260928`;
- commit кандидата: `57fe2971b9d14118a05610514b978043e9bd7759`;
- PR для кандидата не создавался;
- PR №106 (`fix/driver-truth-wave2`) не изменялся и не сливался.

Точный mail-format diff приложен: [0001-fix-rollback-stale-dispatcher-assign-side-effects.patch](ДОКАЗАТЕЛЬСТВА_STALE_ASSIGN_2026_09_28/0001-fix-rollback-stale-dispatcher-assign-side-effects.patch).

Изменены только два файла:

1. `СИСТЕМА_MVP/backend/trips/dispatcher_topology_commands.py` — `+20/-13`;
2. `СИСТЕМА_MVP/backend/trips/test_dispatcher_topology_commands.py` — `+344/-1`.

Модели, миграции, права, очередь клиента, release/deploy-файлы и алгоритм выбора между встречными назначениями не изменялись.

## 2. Причина и исправление

`dispatcher_assign_truck_view` уже выполняется внутри внешнего `transaction.atomic`. До кандидата ветка assign:

1. создавала либо активировала `ExcavatorPlacement`;
2. signal `post_save` поднимал `OperationalStateVersion` и создавал `OperationalStateEvent`;
3. `schedule_haul_assignment` обнаруживал stale `expected_state_id` и выбрасывал `HaulAssignmentStateConflict`;
4. обработчик ловил исключение внутри внешней транзакции и возвращал обычный HTTP 409, поэтому внешняя транзакция коммитила предварительные эффекты.

Кандидат оборачивает создание/активацию placement и `schedule_haul_assignment` во внутренний `transaction.atomic`. Исключение пересекает границу savepoint, Django откатывает связанные записи, version/event и `on_commit` callback; только после отката исключение преобразуется в прежний 409 `state_conflict`.

Это не простой перенос проверки перед записью: блокировка production-state и truck внутри существующих транзакций сохраняется, а конкурентный запрос повторно проверяет actual assignment state после получения блокировок.

## 3. Адресные проверки

### До исправления

Новые stale-тесты были запущены до изменения production-кода и дали два ожидаемых FAIL:

- существующая inactive placement после 409 становилась active, version `13 → 14`;
- отсутствующая placement после 409 создавалась active, version `13 → 15`.

### После исправления

`DispatcherAssignTruckAtomicityTests`, 3/3 PASS:

- inactive placement после stale 409 полностью совпадает с исходным снимком;
- отсутствующая placement не создаётся;
- assignments не меняются;
- `OperationalStateVersion` и полный набор `OperationalStateEvent` не меняются;
- успешная `ShiftClientAction` для 409 не создаётся;
- generic placement-event с `action=save` по текущей политике не создаёт push callback; вызова `send_dispatcher_push_for_event` нет;
- успешное назначение создаёт active placement, pending assignment, action log и одну receipt; его доменный assignment-event регистрирует и выполняет ровно один `on_commit` push callback;
- повтор того же `client_action_id` возвращает `deduplicated`, не создаёт второй assignment/log/receipt/event, не поднимает version и не регистрирует новый push callback.

Расширенный SQLite-набор:

```text
manage.py test \
  trips.test_dispatcher_topology_commands \
  assignments.tests.HaulAssignmentTransactionTests \
  trips.tests.DispatcherAssignmentRealtimeTests \
  trips.test_dispatcher_assignment_commands --verbosity 2
```

Результат: 32 tests; 31 PASS, 1 SKIP (`PostgreSQL concurrency`), exit 0. Дополнительно:

- `manage.py check` — PASS;
- `manage.py makemigrations --check --dry-run` — `No changes detected`;
- `git diff --check` — PASS.

Сырой UTF-8 журнал: [57fe2971…-sqlite.log](ДОКАЗАТЕЛЬСТВА_STALE_ASSIGN_2026_09_28/57fe2971b9d14118a05610514b978043e9bd7759-sqlite.log).

## 4. PostgreSQL и конкурентный сценарий

PostgreSQL: **NOT_RUN**. На машине нет `psql`, Docker/Podman, установленного WSL-дистрибутива и доступного `127.0.0.1:5432`; параметры изолированной базы не заданы. SQLite не считается доказательством конкурентной атомарности.

В candidate commit включён исполняемый `TransactionTestCase` `DispatcherAssignTruckPostgreSQLConcurrencyTests.test_competing_assigns_commit_one_target_without_loser_side_effects`. Два параллельных клиента отправляют разные назначения одного самосвала от одной базовой ревизии. Тест не выбирает нормативного победителя, а требует:

- ровно один HTTP 200 и один HTTP 409 `state_conflict`;
- ровно один pending assignment и одну receipt;
- active placement только у фактически применённой цели;
- отсутствие placement у отклонённой цели;
- соответствие прироста version количеству сохранённых `OperationalStateEvent`.

Команда для изолированной PostgreSQL test DB:

```powershell
$env:DJANGO_DB_ENGINE='postgres'
$env:POSTGRES_DB='<isolated_base_database>'
$env:POSTGRES_USER='<test_user_with_CREATEDB>'
$env:POSTGRES_PASSWORD='<secret>'
$env:POSTGRES_HOST='<isolated_host>'
$env:POSTGRES_PORT='5432'
python manage.py test `
  trips.test_dispatcher_topology_commands.DispatcherAssignTruckPostgreSQLConcurrencyTests `
  --verbosity 2
```

Фиксация недоступности среды: [postgresql-NOT_RUN.log](ДОКАЗАТЕЛЬСТВА_STALE_ASSIGN_2026_09_28/postgresql-NOT_RUN.log).

## 5. Signals, cache и уведомления

- `ExcavatorPlacement.post_save` вызывает `bump_operational_state`; version и event являются строками той же БД и откатываются savepoint — проверено снимками.
- generic signal-event с `action=save` намеренно исключён из системных push. Успешный богатый `assignment_pending` регистрирует ровно один `transaction.on_commit` callback; идемпотентный повтор не регистрирует новый — проверено с выполнением захваченных callbacks.
- в проверенном пути `dispatcher_topology_commands → schedule_haul_assignment → core.signals/bump_operational_state` отдельной записи в Django cache нет. Серверный snapshot опирается на DB version/events. Поэтому отдельного cache rollback в кандидате нет.
- реальная внешняя доставка push/SSE, браузер и установленное приложение не запускались; для отклонённой команды доказано отсутствие опубликованного DB-event, а фактическая push-политика generic save callback не создаёт.

## 6. Manifest хеша R2

Исходный manifest «путь → SHA-256», из которого в R2 был получен общий хеш `f4cd29c1…`, **не сохранился**. `package_hash.ps1` держал список в переменной `$rows`, вычислял итоговый digest и печатал только его; в commit/log manifest не записан. Git tree пакета позволяет построить новый перечень, но он не является сохранённым исходным manifest и не выдаётся за него.

## 7. Ограничения

- PostgreSQL concurrency — NOT_RUN; до него кандидат не имеет окончательной конкурентной приёмки.
- Кандидат сохраняет существующий 409 и отсутствие успешной receipt. Он не реализует будущий журнал команд из § 15.
- Не менялись права manager/мастера, клиентская очередь, встречные назначения, перегон, смены, расчёты и производственные правила.
- Production, release, PR №106 и deploy не затрагивались.
