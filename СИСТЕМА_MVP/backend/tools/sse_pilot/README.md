# Локальный SSE load/fault harness

Инструмент открывает `80–96` независимых SSE-соединений, измеряет первый event,
heartbeat, reconnect, тишину, дубли, обратный порядок и пропуски числовых `id`.
Зависимостей вне стандартной библиотеки Python нет.

## Защитные ограничения

- разрешены только `localhost`, `127.0.0.0/8` и `::1`;
- credentials в URL запрещены;
- инструмент выполняет только `GET` и не создаёт рейсы/назначения;
- cookie читается только из локального однострочного файла и не выводится;
- рабочие cookies и токены использовать запрещено;
- встроенный источник генерирует только синтетические события в памяти.

Поэтому этот harness нельзя случайно направить на production или действующий QA.

## Самотесты

Из `СИСТЕМА_MVP/backend`:

```powershell
python -m unittest tools.sse_pilot.test_sse_load_harness -v
```

Полный локальный прогон 96 клиентов без отдельного сервера:

```powershell
python -m tools.sse_pilot.sse_load_harness exercise `
  --scenario clean --clients 96 --events 12 --duration 3 `
  --output qa_artifacts/sse-pilot/load-clean-96.json
```

Fault-профили:

```powershell
$scenarios = 'disconnect','stall','duplicate','gap','out_of_order'
foreach ($scenario in $scenarios) {
  python -m tools.sse_pilot.sse_load_harness exercise `
    --scenario $scenario --clients 96 --events 12 --duration 3 `
    --output "qa_artifacts/sse-pilot/load-$scenario-96.json"
}
```

`duplicate`, `gap` и `out_of_order` намеренно дают нарушения. Команда завершится
с кодом `0`, только если harness увидел соответствующий fault; детали сохраняются
в `totals.duplicate_ids`, `totals.id_gaps` и `totals.regressing_ids`.

## Прогон против локального прототипа

Сначала запустить локальный ASGI-контур на loopback, затем:

```powershell
python -m tools.sse_pilot.sse_load_harness load `
  --url http://127.0.0.1:8001/realtime/stream/?role_app_code=driver `
  --clients 96 --duration 60 --stall-timeout 30 `
  --min-data-events 1 `
  --output qa_artifacts/sse-pilot/local-driver-96.json
```

Если локальный endpoint требует session cookie, создать отдельную синтетическую
локальную учётную запись и записать только её cookie в git-ignored файл:

```powershell
python -m tools.sse_pilot.sse_load_harness load `
  --url http://127.0.0.1:8001/realtime/stream/?role_app_code=driver `
  --cookie-file .local-sse-pilot-cookie.txt `
  --clients 96 --duration 60 --output qa_artifacts/sse-pilot/local-driver-96.json
```

Файл cookie после прогона удалить. Никогда не использовать cookie с
`driverform.ru` или `qa-*.driverform.ru`.

`after` — единственная application-позиция harness. На reconnect он отправляет
`after=<last_applied>` и дополнительно передаёт `Last-Event-ID` только как
позицию получения. В harness событие считается применённым после проверки
`id`/payload; в приложении этот boundary должен наступать после успешного
snapshot/DOM/native commit.

Флаг `--expect-contiguous-ids` подходит только для нефильтрованного тестового
потока. В адресованном production-подобном потоке номера глобального журнала
могут законно иметь промежутки; gap там должен определяться серверным
`reset_required`/диапазоном журнала, а не арифметикой соседних `id`.

## Проверка релевантных бизнес-событий на одноразовой БД

`run_business_load.ps1` отличается от синтетического `MockSSEServer`: он
поднимает настоящий Django ASGI endpoint и во время 96 подключений создаёт в
новой одноразовой SQLite реальные доменные записи и journal-события:

- назначение каждого самосвала своему экскаватору;
- автоматическую и ручную погрузку;
- отмену назначения;
- новое назначение взамен отменённого.

Используются четыре независимых контекста (два водителя и два машиниста), по 24
клиента на контекст. Для каждого заранее выделенного recipient проверяются
event ID, ровно одно применение, отсутствие чужих событий и семантический ключ
голосовой операции. Отдельно измеряются `commit→receive`, `receive→apply` и
`извлечение semantic operation key`, а также heartbeat и ресурсы ASGI-процесса. Последняя
метрика не доказывает durable handoff в Android, помещение в native-очередь или озвучивание.

Помимо агрегата `business-load.json` runner сохраняет
`business-load-samples.json`. В нём есть исходные UTC timestamps и интервалы
для `commit→receive` и heartbeat, а также рассчитанные из них распределения.
Реальные client/recipient/event ID в этот файл не попадают: внутри одного
прогона используются только порядковые псевдонимы `client-0001` и
`event-0001`. Поэтому p50/p95/p99 можно независимо пересчитать без раскрытия
идентификаторов. Старые прогоны без samples не восстанавливаются расчётными
или выдуманными наблюдениями.

Из `СИСТЕМА_MVP/backend`:

```powershell
powershell -ExecutionPolicy Bypass -File tools\sse_pilot\run_business_load.ps1 `
  -Clients 96 -WarmupSeconds 3 -MeasurementSeconds 20 -Port 8771 `
  -OutputDirectory artifacts\sse-business-load96-final
```

Защита fixture выполняется до `django.setup()` и до любой записи:

- БД обязана отсутствовать и иметь явно заданное имя `*.sqlite3`;
- имя `db.sqlite3`, `default.sqlite3`, `production.sqlite3` или `qa.sqlite3`
  запрещено;
- БД обязана быть прямым потомком нового каталога
  `%TEMP%\sse-pilot-disposable-*`;
- компоненты пути, корень, цель БД и marker отклоняются при symlink/Windows reparse point;
- файловый marker каталога проверяется до `django.setup()`, а marker внутри БД —
  до создания fixture;
- settings принудительно выбирают SQLite с `CONN_MAX_AGE=0`, loopback,
  отключённые Redis и внешние push credentials;
- приватный fixture с локальными session cookie удаляется вместе с одноразовой
  БД; итоговые artifacts cookie не содержат.

Это измерение проверяет серверную адресацию, parser/apply границы и извлечение semantic key в harness.
Оно не является измерением WebView, Android audio queue или слышимого
голоса и не заменяет PostgreSQL/Redis/ASGI нагрузочный этап QA.
