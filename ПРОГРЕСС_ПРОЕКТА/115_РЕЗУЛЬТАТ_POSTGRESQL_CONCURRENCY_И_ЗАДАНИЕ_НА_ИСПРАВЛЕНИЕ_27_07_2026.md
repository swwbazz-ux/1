# Результат настоящего PostgreSQL concurrency-прогона и задание на исправление

Дата: 27.07.2026  
Основание: документы `113_...` и `114_...`, а также прямое разрешение
владельца продолжить локальную PostgreSQL-проверку.

Итоговый статус:
**POSTGRESQL 16 — 3 PASS / 10 FAIL / 1 ERROR. READY-CORE НЕ ГОТОВ К
ПИЛОТУ НА POSTGRESQL ДО ИСПРАВЛЕНИЯ QA-PG-P1-001.**

Это первый настоящий запуск ранее пропускавшихся конкурентных тестов.
SQLite-результат `673 PASS / 14 skipped` не мог обнаружить эти ошибки,
поскольку SQLite не реализует проверяемые блокировки строк PostgreSQL.

## 1. Границы

Проверялись только уже готовые производственные контуры:

- переключение активной роли;
- завершение и отмена рейса;
- разгрузка Водителем;
- служебное завершение Диспетчером;
- одновременный старт смены Машиниста;
- конкурентное создание и завершение простоев;
- идемпотентность рейсов и закрытия смен.

Незавершённые рабочие места Механика, Табельщика, Делопроизводителя,
Охраны труда и будущих подразделений не расширялись.

Production, рабочая SQLite, рабочий `.env`, файлы миграций, deploy, nginx,
systemd и collectstatic не использовались и не изменялись.

## 2. Изолированный PostgreSQL 16

Использован официальный переносимый архив PostgreSQL:

- версия: `16.14`;
- источник: официальный EDB binary archive, на который ссылается
  `postgresql.org`;
- архив:
  `postgresql-16.14-2-windows-x64-binaries.zip`;
- размер архива: `325741585` байт;
- SHA-256 архива:
  `8A7F54C1968D5D49BDCD3F66B1291F736C74B8CB6A26E9874771FCC7837DBF38`.

Среда создана вне проекта:

`C:\Users\swwba\AppData\Local\Temp\codex-pg16-qa-20260727-01`

Параметры:

- сервер слушал только `127.0.0.1:55432`;
- Windows-служба не создавалась;
- права администратора и UAC не использовались;
- администратор временного кластера: `qa_cluster_admin`;
- отдельная роль Django: `copper_qa_runner`;
- роль Django имела `LOGIN + CREATEDB`, но не имела `SUPERUSER` и
  `CREATEROLE`;
- базовая QA-база: `copper_resources_qa`;
- Django создал только `test_copper_resources_qa`;
- существующие миграции применялись только к этой тестовой базе;
- после прогона Django удалил `test_copper_resources_qa`;
- PostgreSQL остановлен;
- процессов `postgres` нет;
- слушателя `55432` нет.

Кластер использовал временную доверенную авторизацию только на loopback.
Сервер уже остановлен. Переносимые бинарники и остановленный QA-кластер
временно сохранены вне проекта для быстрой независимой перепроверки после
исправлений.

## 3. Точная команда

Переменные задавались только в процессе PowerShell:

```powershell
$env:PYTHON_DOTENV_DISABLED = '1'
$env:DJANGO_DB_ENGINE = 'postgres'
$env:POSTGRES_DB = 'copper_resources_qa'
$env:POSTGRES_USER = 'copper_qa_runner'
$env:POSTGRES_PASSWORD = ''
$env:POSTGRES_HOST = '127.0.0.1'
$env:POSTGRES_PORT = '55432'
$env:POSTGRES_CONN_MAX_AGE = '0'
```

Запуск:

```powershell
..\.venv\Scripts\python.exe manage.py test `
  trips.test_chaos_trip_terminal_idempotency.TripTerminalPostgreSQLConcurrencyTests `
  trips.test_chaos_dispatcher_active_role.DispatcherActiveRolePostgreSQLConcurrencyTests `
  shifts.test_chaos_p04_009_postgresql.ExcavatorConcurrentShiftStartPostgreSQLTests `
  downtimes.test_chaos_p01_005_postgresql.DowntimePostgreSQLConcurrencyRegressionTests `
  --noinput -v 2
```

Фактически найдено и выполнено ровно `14` тестов. `--parallel` не
использовался: сами тесты создают конкурирующие потоки.

## 4. Результаты 14 тестов

### 4.1. PASS — 3

1. `TripTerminalPostgreSQLConcurrencyTests.`
   `test_same_shift_action_id_same_object_returns_same_shift`;
2. `ExcavatorConcurrentShiftStartPostgreSQLTests.`
   `test_two_simultaneous_starts_leave_one_shift_and_domain_conflict`;
3. `DowntimePostgreSQLConcurrencyRegressionTests.`
   `test_two_mechanics_create_only_one_open_downtime`.

### 4.2. FAIL — 10

Шесть терминальных тестов рейса:

1. `test_unload_races_dispatcher_cancel`;
2. `test_unload_races_dispatcher_service_complete`;
3. `test_two_unloads_with_different_action_ids`;
4. `test_two_dispatcher_service_completions`;
5. `test_same_trip_action_id_same_object_returns_success_to_both_clients`;
6. `test_same_trip_action_id_different_objects_returns_original_result`.

Два теста переключения активной роли:

7. `test_role_switch_wins_against_dispatcher_service_close`;
8. `test_role_switch_wins_against_dispatcher_assignment_cancel`.

Два теста простоев:

9. `test_mechanic_and_operator_create_only_one_open_excavator_downtime`;
10. `test_successful_loading_and_mechanic_close_keep_one_end_boundary_and_audit`.

### 4.3. ERROR тестовой фикстуры — 1

`TripTerminalPostgreSQLConcurrencyTests.`
`test_same_shift_action_id_different_objects_returns_original_result`

Ошибка:

```text
psycopg.errors.UniqueViolation:
duplicate key value violates unique constraint "users_role_pkey"
DETAIL: Key (id)=(1) already exists.
```

## 5. QA-PG-P1-001 — несовместимая блокировка nullable JOIN

Приоритет: **P1**.

Основная ошибка production-путей:

```text
NotSupportedError:
FOR UPDATE cannot be applied to the nullable side of an outer join
```

`EmployeeShift.equipment` и `Trip.loading_shift` допускают `NULL`.
Несколько запросов используют одновременно:

```python
.select_for_update()
.select_related(...)
```

Django строит `LEFT OUTER JOIN`, а обычный `FOR UPDATE` пытается блокировать
также nullable-сторону соединения. PostgreSQL это запрещает.

### 5.1. Подтверждённые точки падения

1. `users/active_role.py`, `activate_role_session()`:
   блокировка смен сотрудника вместе с nullable `equipment`;
2. `trips/views.py`, `excavator_truck_loaded_view()`:
   чтение заблокированной смены Машиниста;
3. `trips/views.py`, `excavator_downtime_action_view()`:
   чтение заблокированной смены Машиниста;
4. `trips/views.py`, `driver_complete_trip_view()`:
   чтение заблокированной смены Водителя;
5. `trips/views.py`, `dispatcher_complete_trip_view()`:
   блокировка рейса вместе с nullable `loading_shift`.

Два теста активной роли не дошли до своего конкурентного барьера именно
из-за раннего PostgreSQL-исключения в `activate_role_session()`.

Два теста простоев также не дошли до проверяемой конкуренции:

- действие Машиниста вернуло HTTP `500` до блокировки техники;
- погрузка завершилась до `DowntimeEvent.save`, а тест затем показал
  вторичный таймаут ожидания.

Поэтому эти конкурентные гарантии имеют статус **НЕ ПРОВЕРЕНО ПОСЛЕ
ДОСТИЖЕНИЯ БАРЬЕРА**, а не функциональный `FAIL` самой будущей победившей
ветви.

### 5.2. Соседние подтверждённые риски того же класса

Аудит должен включить как минимум:

- `trips/views.py`:
  - отмену созданной Машинистом погрузки;
  - изменение рабочих настроек Машиниста;
  - legacy-путь создания рейса Машинистом;
  - изменение фактической точки разгрузки Водителем;
  - служебное закрытие смены Диспетчером;
- `users/views.py`:
  - принятие назначения Водителем;
  - действие простоя Водителя;
- `shifts/services.py`:
  - штатное закрытие смены Машиниста;
- другие запросы ready-core, где `select_for_update()` совмещён с
  `select_related()` через nullable-связь.

Нельзя механически добавлять `of=('self',)` везде. Если бизнес-операция
должна блокировать также технику, после сужения блокировки основной строки
необходимо сохранить или добавить отдельный явный:

```python
Equipment.objects.select_for_update().get(...)
```

## 6. QA-PG-P2-001 — `reset_sequences=True` ломает первую фикстуру

Приоритет: **P2 / дефект теста**.

В `TripTerminalPostgreSQLConcurrencyTests` задано:

```python
reset_sequences = True
```

До первого теста Django принудительно возвращает PostgreSQL sequence к `1`,
хотя существующие миграции уже создали роли ОУП, Заместителя, Табельщика,
Начальника участка и Сотрудника.

Первый алфавитный тест затем выполняет:

```python
Role.objects.create(code='driver', ...)
```

и получает конфликт первичного ключа `id=1`.

Тесты не проверяют конкретные значения PK. Нужно убрать
`reset_sequences=True` либо иначе сделать фикстуру независимой от
миграционно созданных ролей.

Это не дефект `close_driver_shift`: соседний тест того же production-сервиса
прошёл.

## 7. QA-PG-P2-002 — конкурентные тесты скрывают раннее исключение

Приоритет: **P2 / диагностика регрессий**.

Сейчас ранний HTTP `500` или исключение worker может маскироваться сообщением:

- `Переключение роли не получило блокировку Employee`;
- `Погрузка не сохранила production ended_at под блокировкой`.

Нужно:

1. если future завершился до ожидаемого barrier/event, сразу выводить
   фактический результат worker;
2. сохранять `response.exc_info` для HTTP `500`;
3. не объявлять ошибку конкурентного контракта, если production-путь до
   барьера не дошёл.

## 8. Точное задание чату исправлений

1. Прочитать документы `113`, `114` и `115`.
2. Исправить `QA-PG-P1-001` во всех подтверждённых ready-core путях.
3. Провести адресный аудит аналогичных nullable JOIN под `FOR UPDATE`.
4. Не расширять работу на незавершённые роли и интерфейсы.
5. Сохранить фактические блокировки `Employee`, `Equipment`, `Trip`,
   `EmployeeShift` и idempotency-key в установленном порядке.
6. Исправить `QA-PG-P2-001` и `QA-PG-P2-002`.
7. Добавить одиночные PostgreSQL-regression smoke для каждого изменённого
   рабочего POST-пути, чтобы ошибка обнаруживалась без конкурентного
   тайминга.
8. Не менять модели и не создавать миграции: текущая причина находится в
   ORM-запросах и тестах.
9. Не трогать production, рабочую SQLite, `.env`, private media, deploy,
   nginx, systemd и collectstatic.
10. Не выполнять commit, push и deploy.
11. Создать документ `116_ОТЧЕТ_ОБ_ИСПРАВЛЕНИИ_POSTGRESQL_CONCURRENCY_...`.
12. Передать результат текущему тестовому чату со статусом
    `ГОТОВО К ПЕРЕПРОВЕРКЕ`, но не объявлять `FIXED` самостоятельно.

## 9. Критерии независимой перепроверки

После отчёта исправлений текущий тестовый чат выполнит:

1. свежую тестовую PostgreSQL-базу;
2. ровно 14 исходных PostgreSQL-only тестов;
3. не менее пяти последовательных успешных повторов 14 конкурентных тестов;
4. новые одиночные PostgreSQL-регрессии изменённых POST-путей;
5. полный `manage.py test` на PostgreSQL 16;
6. полный `manage.py test` на штатной SQLite;
7. `manage.py check`;
8. `makemigrations --check --dry-run`;
9. `git diff --check`;
10. контроль неизменности рабочей SQLite и `private_media`.

Только после полностью зелёной независимой PostgreSQL-перепроверки можно
вернуться к Android smoke, Git-фиксации и ограниченному пилоту.

## 10. Контроль рабочих данных

Рабочая SQLite после прогона:

- размер: `2584576` байт;
- mtime UTC: `2026-07-23T17:11:26.0764680Z`;
- SHA-256:
  `A5CC387E15454B107FE5E04ADC6801E983873AD62EB79DE76E91283E02733222`.

Рабочий `private_media`:

- файлов: `78`;
- общий размер: `3042` байта;
- SHA-256 манифеста:
  `ABEDC5CD377D2FA49BE966E878CD34F1C0EAA972366DCC9E20062D05AFDF1B35`.

Значения совпадают с документами 113–114.

## 11. Место результата

- изменения проекта: только новый документ 115 и обновления файлов
  прогресса;
- production-код в этом тестовом этапе не изменялся;
- PostgreSQL: переносимый, остановлен, вне проекта;
- Windows-служба: не создавалась;
- commit: не выполнялся;
- push: не выполнялся;
- deploy: не выполнялся;
- production: не затрагивался;
- рабочая база: не изменялась;
- файлы миграций: не изменялись.
