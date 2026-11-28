# Локальное сравнение A / B / C

## Назначение

Сравнение отделяет влияние общих исправлений восстановления от эффекта SSE:

- **A** — неизменённый production source
  `f99c48cacdc718c303d6b30197701107d33e688c`;
- **B** — общие исправления cursor/native/голоса/reconciliation, SSE выключен;
- **C** — тот же exact commit, данные и настройки B, но SSE включён.

Production и существующий QA в этом runbook не используются. Экономия батареи
не является результатом локального прогона — она измеряется на телефоне.

## Неподвижные условия

Для каждого варианта сохранить в `qa_artifacts/sse-pilot/<run-id>/manifest.json`:

- полный Git SHA и `git status --porcelain`;
- Python/Node/Android toolchain versions;
- SHA-256 исходного snapshot тестовой БД и fixture;
- роль, synthetic access/equipment IDs без ФИО/телефонов;
- все интервалы polling, native heartbeat, SSE heartbeat/safety-scan;
- начало/конец, число клиентов и fault-сценарий.

Использовать один seed из полностью синтетических Driver/Excavator данных.
Перед каждым вариантом восстанавливать один и тот же snapshot. FCM и WebPush
credentials должны быть пустыми. Рабочие cookies, push tokens и PIN запрещены.

## Подготовка A

Создать отдельный read-only baseline worktree, не меняя текущий pilot worktree:

```powershell
git worktree add --detach C:\codex-tmp\sse-pilot-baseline-f99 `
  f99c48cacdc718c303d6b30197701107d33e688c
git -C C:\codex-tmp\sse-pilot-baseline-f99 status --short
git -C C:\codex-tmp\sse-pilot-baseline-f99 rev-parse HEAD
```

Ожидается пустой status и exact SHA `f99c…`. A запускается штатным локальным
WSGI/runserver способом. Никакие файлы из pilot worktree в A не копируются.

## Запросы и интервалы, которые нужно считать

| Канал | A — baseline | B — общие исправления, без SSE | C — SSE включён |
|---|---|---|---|
| Web `/realtime/state/` | 2 с в видимом Driver/Excavator | 2 с, прежний backoff до 30 с | частый poll остановлен после sync; 2 с только fallback |
| Web `/session-heartbeat/` | 30 с в видимом WebView | 30 с | 30 с; нужен для session/presence/auth |
| Native `/realtime/state/` | 10 с при активной смене | 10 с | 10 с остаётся, пока отдельно не доказан безопасный owner handoff |
| Server-truth fragment | по событию и страховочно 20 с | то же | по событию и страховочно 60 с |
| SSE heartbeat | нет | нет | data-bearing event каждые 10 с |
| SSE watchdog/reconnect | нет | нет | timeout 30 с; reconnect backoff до 30 с |
| SSE DB safety scan | нет | нет | серверный scan каждые 5 с, это не HTTP клиента |
| SSE reauth/max stream | нет | нет | серверная reauth 10 с; принудительный reconnect 900 с |
| Driver offline outbox | при наличии очереди; retry 5 с→5 мин | без изменений | без изменений |
| Excavator offline outbox | при наличии очереди; retry 2 с→60 с | без изменений | без изменений |
| FCM | wake по событию | без изменений | без изменений; после wake native catch-up |
| HTML/static/audio | load/cache/fragment | без изменений | без изменений |
| APK manifest | QA profiles отключены | отключён | отключён |

Эта таблица фиксирует фактический pilot-budget. Нельзя приписывать C снижение
native heartbeat: Android-код в данном локальном этапе его не отключает.

## Подготовка B и C

B и C собираются из одного pilot commit. Различаются только:

```text
B: SSE_PILOT_ENABLED=false
C: SSE_PILOT_ENABLED=true
```

Перед измерением C подтвердить:

- ASGI слушает только loopback/private socket;
- Redis отдельный на `127.0.0.1:6381`;
- namespace уникален для run-id;
- FCM/WebPush settings пусты;
- отключение флага возвращает прежний foreground polling;
- offline outbox и общий applied cursor не очищаются kill switch-ом.

## Одинаковый сценарий нагрузки

Для A, B и C воспроизвести один журнал действий:

1. Driver и Excavator входят под синтетическими access.
2. Открываются обе смены.
3. Создаётся назначение и принимается водителем.
4. Создаётся погруженный рейс и точка разгрузки.
5. Рейс отменяется, затем создаётся замена.
6. WebView уходит в background в момент одного события и возвращается.
7. Сеть рвётся и восстанавливается.
8. Создаётся offline action, подтверждается сохранность outbox и одно применение.
9. Выполняется logout/login другим synthetic user и смена техники.
10. Проверяется, что старый stream/cursor не продолжает доставку новому контексту.

Один burst событий должен приводить не более чем к одному fragment refresh на
экран за coalescing window. Отдельно считать poll, SSE, heartbeat, fragment,
offline-sync и static/media requests.

## Fault-матрица C

| Сценарий | Действие | Обязательная проверка |
|---|---|---|
| Потеря Redis wakeup | не публиковать один wakeup, запись оставить в БД | событие найдено safety-scan |
| Subscribe/read race | commit между subscribe и DB replay | событие получено один раз после dedupe |
| Reconnect | оборвать ASGI connection | запрос идёт от `last_applied`, не `last_received` |
| Зависание | держать TCP без data heartbeat | watchdog включает fallback polling |
| Redis restart | перезапустить только disposable Redis | DB catch-up закрывает диапазон |
| ASGI restart | завершить локальный ASGI процесс | reconnect + replay, outbox не затронут |
| Gap/truncate | cursor старше journal floor | `reset_required`, snapshot, затем `> snapshot_version` |
| Reverse/duplicate | подать повтор/обратный порядок | применено один раз, cursor не регрессирует |
| Cancel/replace | догрузить старое + актуальное | старое не звучит, актуальное сохраняет уведомление |
| Channel race | одновременно SSE/poll/native wake | один смысловой operation claim |

Падение между реальным звуком и сохранением отметки имеет неразрешимое окно:
либо возможен повтор, либо потеря. В отчёте фиксировать выбранную семантику,
не писать «ровно один слышимый звук при любом сбое».

## 80–96 SSE connections

Самотест harness:

```powershell
Set-Location СИСТЕМА_MVP\backend
python -m unittest tools.sse_pilot.test_sse_load_harness -v
```

Детерминированная локальная нагрузка без приложения:

```powershell
python -m tools.sse_pilot.sse_load_harness exercise `
  --scenario clean --clients 96 --events 12 --duration 3 `
  --output qa_artifacts/sse-pilot/load-clean-96.json
```

Против локального C endpoint использовать `load` по инструкции инструмента.
Не включать `--expect-contiguous-ids` для role-filtered глобального журнала.

## Итоговая таблица

| Метрика | A | B | C |
|---|---:|---:|---:|
| foreground `/realtime/state/` requests/час | | | |
| native heartbeat requests/час | | | |
| SSE reconnects/час | n/a | n/a | |
| fragment requests/100 операций | | | |
| median/p95 commit→apply | | | |
| потерянные актуальные операции | | | |
| повторные operation claims/озвучки | | | |
| HTTP bytes RX/TX | | | |
| server CPU/RSS/FD/DB connections | | | |

Локальный критерий C: нет потерь актуального состояния, нет чужих событий,
fallback восстанавливается, 96 соединений не создают 5xx/утечку idle DB
transactions. Battery drain, Doze и реальная FCM-доставка остаются непроверенными.

## Выполненный локальный прогон 26.09.2026

- A (`f99c…`): 1292 Django tests — OK, 40 skipped; 767 Node tests —
  766 OK, 1 skipped; mobile — 44/44 OK.
- B/C общий код: полный Node suite — 781 tests, 780 OK, 1 skipped;
  mobile — 56/56 OK; четыре Android unit variants — BUILD SUCCESSFUL;
  полный Django suite — 1326/1326 OK, 40 skipped.
- C server: `core.test_sse_pilot` — 29/29 OK; реальный Uvicorn/SQLite —
  80/80 SSE clients, без reconnect/timeout/duplicate/gap, но p95 первого
  события 8.875 s. SQLite и один Windows ASGI worker не являются оценкой
  production PostgreSQL.
- Отдельный protocol harness: 96/96 clients, 1152 events, p95 78 ms, без
  потерь/дублей/регрессии; fault-сценарии disconnect/stall восстановили все
  1152 события, а duplicate/gap/out-of-order были обнаружены валидатором.
- Потерянный Redis wakeup проверен на реальном открытом SSE без Redis:
  DB safety scan доставил version 13 от сохранённого applied 12 без reconnect.
- Усечение реально проверено: после удаления versions 1–10 поток с applied 0
  вернул `reset`/`journal_gap`, `snapshot_required=true`, min=11, max=13.

Первоначальный 15-секундный прогон 96 real clients: 96/96 получили replay,
92/96 — heartbeat, было 22 reconnect после stall timeout. Сырые логи, точная
команда, stall threshold и server metrics не были сохранены, поэтому причина
`SQLite/Windows` не установлена и этот прогон не принимается как доказательство
capacity.

Повторный инструментированный прогон: 96 clients, 60 секунд, stall timeout
30 секунд, heartbeat 10 секунд, safety scan 5 секунд, один Uvicorn worker,
SQLite, Redis off. Результат: 96/96 connected/data/heartbeat, 0 reconnect,
timeout, client/server error и HTTP 5xx. Heartbeat interval p95 11.015 s,
max 11.422 s. Измеренный worker: RSS p95 223.102 MiB, private bytes p95
588.152 MiB, 109–112 threads, средний CPU 264.352% одного logical core.
Heartbeat cadence overrun p95 1009.935 ms — только proxy, не прямая метрика
event-loop lag. Эти Windows/SQLite цифры не доказывают QA capacity;
обязательный следующий gate — 96 клиентов на PostgreSQL/Redis/ASGI с direct
event-loop и DB metrics.

## Отключение

1. `SSE_PILOT_ENABLED=false`.
2. Перезапустить только локальный ASGI/WSGI процесс.
3. Подтвердить возврат штатного foreground polling и индикаторов связи.
4. Не удалять applied cursor, voice operation keys и offline outbox.
5. Остановить disposable Redis и удалить только каталог конкретного run-id.

Cursor/native/voice/reconciliation — общие изменения B и C. Kill switch SSE их
не откатывает; для полного возврата к A используется отдельный baseline worktree.
