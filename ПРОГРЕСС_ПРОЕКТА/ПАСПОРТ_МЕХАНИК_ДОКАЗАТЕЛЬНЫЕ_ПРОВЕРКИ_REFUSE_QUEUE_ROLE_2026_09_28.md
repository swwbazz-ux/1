# Доказательные проверки M-REFUSE-1, M-QUEUE-1, M-ROLE-1

> **Приёмка Астры от 28.09.2026: ЧАСТИЧНО ПРИНЯТО, требуется R2.** Опубликованные Node 2/2 независимо повторены; второй тест проверяет текст шаблона, не исполняет очередь мастера. Инструкция PostgreSQL не задаёт необходимый DJANGO_DB_ENGINE; manager-матрица и проверки неизменности состояния не подтверждают весь заявленный охват. Подробные границы и исправления — [разбор, § 13](ПАСПОРТ_МЕХАНИК_РАЗБОР_РЕЦЕНЗИИ_РОЛИ_2026_09_28.md#13-приёмка-проверочного-пакета-кодекса-0480e8fd). Ниже сохранён исходный отчёт Кодекса, его формулировки читаются с этими оговорками.

Дата: 28.09.2026. Исходная документационная точка: `docs/mechanics-passport` @ `c7476ad781051de0b7003424ef2e0ab8496b7755`.

Проверяемые снимки кода:

- PR №106: `1754269aa1858784f62177bc229ae18e392a67e3`;
- release: `0fe60543de59bf7cbeca4868fc17e8e00689d2ba`.

Проверки выполнены в двух чистых detached worktree. Бизнес-код, PR №106, release и production не изменялись. Исполняемый пакет лежит рядом: [`ПАКЕТ_ПРОВЕРОК_M_REFUSE_QUEUE_ROLE_2026_09_28`](ПАКЕТ_ПРОВЕРОК_M_REFUSE_QUEUE_ROLE_2026_09_28/README.md).

## 1. Доступная изолированная среда

На этой машине нет запускаемого PostgreSQL-контура:

```json
{
  "docker": false,
  "podman": false,
  "psql": false,
  "pg_isready": false,
  "postgres_services": [],
  "tcp_5432": false,
  "wsl_distros": []
}
```

Поэтому обязательный адресный прогон именно на PostgreSQL имеет статус **NOT_RUN**. Он не заменён общим CI или SQLite. Пакет содержит `run_postgresql.ps1`; до запуска он проверяет реальное соединение и `connection.vendor == 'postgresql'`.

Точная команда для изолированного PostgreSQL, где пользователю БД разрешено создавать test database:

```powershell
$env:POSTGRES_DB='passport_probe'
$env:POSTGRES_USER='passport_probe'
$env:POSTGRES_PASSWORD='<secret>'
$env:POSTGRES_HOST='127.0.0.1'
$env:POSTGRES_PORT='5432'
& .\run_postgresql.ps1 -SourceRoot '<checkout exact SHA>' -PythonExe '<python-with-requirements>'
```

## 2. Выполненные команды

Один и тот же пакет выполнен без изменения против обоих SHA:

```powershell
& .\run_sqlite.ps1 -SourceRoot 'C:\codex-tmp\passport-proof-pr-1754269' -PythonExe '<project-venv-python>'
& .\run_sqlite.ps1 -SourceRoot 'C:\codex-tmp\passport-proof-rel-0fe605' -PythonExe '<project-venv-python>'
```

На каждом SHA результат: Django **20/20 PASS**, Node **2/2 PASS**. Отдельный усиленный повтор downtime-probe после добавления проверок версии: **1/1 PASS** на каждом SHA.

## 3. Спорный старт простоя при несовпадении серверной гружёности

### Фактический результат SQLite

На обоих SHA `driver.downtime.started` с причиной «Ожидание разгрузки», когда сервер не видит открытого гружёного рейса:

1. возвращает `accepted`;
2. создаёт один `DowntimeEvent`;
3. сохраняет `OfflineFieldEvent.status=accepted`;
4. связывает receipt с созданным простоем: `receipt.downtime_event_id == result.server_ids.downtime_event_id`;
5. сохраняет время действия без подмены: `receipt.occurred_at == DowntimeEvent.started_at == время события`;
6. повтор того же неизменного `event_id` возвращает `deduplicated` и не создаёт второй простой;
7. первая запись увеличивает global operational version `23 → 25`; повтор оставляет её `25 → 25`.

Фрагмент исполняемого доказательства на обоих SHA:

```text
first_status=accepted
second_status=deduplicated
downtime_count=1
receipt_id=1
downtime_id=1
receipt_downtime_id=1
occurred_at == started_at
version_before=23
version_after_first=25
version_after_second=25
result_version=25
```

Сервер одновременно пишет warning `loaded_trip_required` через `_log_discrepancy`, но отдельной структурированной записи расхождения в БД код не создаёт. Обработчик: `core/offline_sync.py:_process_downtime` (PR:3863+), receipt/idempotency: `process_one_offline_event` (PR:4282+).

### Граница доказательства

- **PostgreSQL: NOT_RUN** — причины и запускаемый пакет указаны в § 1.
- SQLite доказывает доменный результат и идемпотентность, но не доказывает PostgreSQL-locking/изоляцию при параллельной доставке.
- Снимок экрана не запускался: это серверная проверка receipt/БД/version, а не визуальная приёмка.

## 4. Четыре mutation-пути `manager`

Общий guard: `dispatcher_access_from_request` и `lock_dispatcher_mutation_access` (`trips/dispatcher_guards.py:84,96`); выбор смены: `get_active_dispatcher_shift` (`trips/dispatcher_header.py:73`). Фактические команды: topology `dispatcher_topology_commands.py:56,182`, забой `dispatcher_equipment_commands.py:10`, простой `dispatcher_downtime_commands.py:12`.

Матрица реально выполненных HTTP-проверок одинакова на PR и release:

| Путь | `manager`, чужая открытая диспетчерская смена | `manager`, диспетчерская смена закрыта | контроль `dispatcher` | контроль `mining_master` через диспетчерский endpoint |
|---|---:|---:|---:|---:|
| Перемещение экскаватора | `200`, placement изменён | `409`, мутации нет | `200` | `403` |
| Назначение/снятие самосвала | `200`, release принят | `409`, мутации нет | `200` | `403` |
| Настройка забоя | `200`, settings сохранены | `409`, мутации нет | `200` | `403` |
| Закрытие простоя | `200`, `ended_at` заполнен | `409`, `ended_at` не изменён | `200` | `403` |

Дополнительный исполняемый guard-matrix:

```json
{"dispatcher":true,"admin":true,"manager":true,"mining_master":false,"driver":false}
```

Это доказательство **фактического доступа**, а не решение о его нормативной допустимости. R-18 здесь не расширяется: собственные маршруты горного мастера остаются отдельными; проверка `403` относится именно к четырём диспетчерским endpoints.

## 5. Устаревшая команда общей расстановки и очередь

### 5.1. Серверное подтверждение и версия

На обоих SHA воспроизведены оба рабочих места:

- Диспетчер: устаревший `expected_assignment_state_id` → HTTP `409`, `code=state_conflict`;
- Горный мастер: устаревший `expected_assignment_state_id` → HTTP `409`, `conflict=true`;
- в обоих случаях серверная команда не меняет назначение;
- global version не увеличивается от отклонённой команды:

```text
dispatcher: version_before=16, version_after=16
mining_master: version_before=15, version_after=15
```

Это отдельная проверка от судьбы клиентской очереди.

### 5.2. Судьба команды в очереди

Выполненный Node runtime-probe использует реальный `static/js/dispatcher-transport-v1.js`:

- Диспетчерский direct-first путь: прямой `409` **не записывается** в durable queue;
- если команда ранее попала в очередь из-за сетевой ошибки, последующий `409` при `flush` удаляет её;
- после server 409 автоматического повтора нет;
- сетевой сбой до ответа сервера сохраняет команду и допускает retry.

Доказательство:

```json
{"direct_saved":0,"offline_saved":1,"after_409":0,"retry":false}
```

### 5.3. Различие двух рабочих мест и отображение

Исполняемый contract-probe реального `templates/trips/dispatcher_control.html` подтвердил:

- desktop Диспетчера использует `dispatcherPost` — сначала отправка, запись только после сетевого сбоя;
- мобильное рабочее место Горного мастера для общей расстановки использует `dispatcherPostQueued` — сначала durable запись, затем flush;
- terminal server response в `flushDispatcherSyncQueue` удаляет запись, вызывает `markMiningMasterBoardStale()` и `showDispatcherDnDError(error)`;
- при `error.conflict` вызываются оба пути обновления: `refreshMobileBoardFromServer({preserveScreen:true})` и `refreshDispatcherDesktopBoardFromServer()`.

Доказательство:

```json
{"dispatcher_direct_first":true,"master_queued_first":true,"conflict_notice":true,"mobile_refresh":true,"desktop_refresh":true}
```

Функции находятся в шаблоне PR на строках 898 (`flush`), 972 (`dispatcherPost`), 996 (`dispatcherPostQueued`), 1604 (`showDispatcherDnDError`).

### Граница доказательства отображения

Выполнены runtime очереди и contract wiring реального шаблона. Реальный DOM/WebView/телефон с визуальным модальным сообщением и фактическим завершением HTTP fragment-refresh **NOT_RUN**. Следовательно:

- удаление/сохранение/retry очереди — подтверждено исполнением;
- вызов notice и обоих refresh-функций — подтверждён wiring-тестом;
- фактическая отрисовка обновлённой доски на устройстве — остаётся неизвестной до браузерной/телефонной проверки.

## 6. Итог без нормативного вывода

1. Спорный старт простоя в текущих PR/release на SQLite принимается, receipt связан с одним простоем, время действия сохраняется, replay идемпотентен; PostgreSQL остаётся `NOT_RUN`.
2. `manager` фактически выполняет четыре диспетчерские mutation-команды только при наличии открытой диспетчерской смены; вопрос допустимости не решён этим отчётом.
3. Stale topology-команда получает 409 и не увеличивает version. Direct 409 не сохраняется; queued 409 удаляется и не повторяется. Оба refresh-пути связаны с конфликтом, но реальная визуальная приёмка не выполнена.
