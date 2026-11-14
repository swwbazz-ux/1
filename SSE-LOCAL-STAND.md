# Локальный стенд SSE — runbook T-016 WP-2

Владелец файла: `copper-realtime` (T-016). Это локальный стенд, не production.
Рабочая копия: `D:/CopperAgentTeam/pilot/state/tmp/sse-work-20261112`
(без `.git`; версия = base `37e5747f` + дельта копии, манифест SHA-256 делает
release). Основной checkout `D:/Projects/copper-resources` — только чтение.

## 1. Что где слушает

| Что | Адрес | Кто владелец | Назначение |
|---|---|---|---|
| PostgreSQL 16.4 | `127.0.0.1:55432`, БД/пользователь `sse_local` / `sse_local_only` | Build | общая БД стенда |
| Redis 5.0.14.1 | `127.0.0.1:56381`, пароль `sse_local_only` | Build | wake-канал `accounting-mvp:sse:local-20261112:operational-state` |
| ASGI только `/realtime/stream/` | `127.0.0.1:8001` | realtime (этот файл) | T-split, ASGI-поток |
| WSGI-страницы | `127.0.0.1:8005` | realtime (этот файл) | T-split, страницы |
| Полное приложение на ASGI + `/static/` | `127.0.0.1:8006` | realtime (этот файл) | T-origin для настоящего `EventSource` |

Порты `8010` (backend), `8030–8032` (e2e) и `8040–8042` (QA) не занимать.

## 2. Подготовка (один раз)

`СИСТЕМА_MVP/backend/.env` копии уже содержит локальный профиль
(`DJANGO_SETTINGS_MODULE=config.settings_sse_local`, `DJANGO_DEBUG=false`,
PostgreSQL `55432`, Redis `56381`, `SSE_PILOT_ENABLED=true`).
`config/settings.py` и `.env` основного checkout не меняются.

```powershell
Set-Location 'D:/CopperAgentTeam/pilot/state/tmp/sse-work-20261112/СИСТЕМА_MVP/backend'
$py = 'D:/Projects/copper-resources/СИСТЕМА_MVP/.venv/Scripts/python.exe'
& $py manage.py migrate --noinput --settings=config.settings_sse_local
& $py manage.py setup_sse_stand --settings=config.settings_sse_local
```

`setup_sse_stand` идемпотентна и создаёт ровно две роли. Локальные одноразовые
коды (не production):

| Роль | Телефон | Код | Что открыто |
|---|---|---|---|
| `driver` | `+79000000101` | `111111` | смена на `SSE-1`, принятое рабочее назначение, путевое назначение |
| `excavator_operator` | `+79000000102` | `222222` | смена на `SSE-ЭКС-1`, принятое рабочее назначение |

Проверка данных: `psql` только к `55432`, `redis-cli` только к `56381`.

## 3. Топология T-split (production-подобная)

Страницы — WSGI, поток — отдельный ASGI, который отдаёт **только**
`/realtime/stream/` (`config/sse_asgi.py:17-29`). Cookie `sessionid` не
различает порты, поэтому сессия, выданная на `8005`, принимается на `8001`;
`SECRET_KEY` и БД у процессов общие (один `.env`).

```powershell
# окно 1 — страницы (DEBUG=false, статика через --insecure)
& $py manage.py runserver 127.0.0.1:8005 --noreload --insecure --settings=config.settings_sse_local

# окно 2 — только SSE-поток
& $py -m uvicorn config.sse_asgi:application --host 127.0.0.1 --port 8001 --env-file .env
```

Что доказано на этом стенде (T-016, 2026-11-12):

- `GET http://127.0.0.1:8001/realtime/stream/` без cookie → `401`,
  `{"ok":false,"error":"authentication_required"}`;
- `GET http://127.0.0.1:8001/driver/` → `404`,
  `{"ok":false,"error":"sse_endpoint_only"}`;
- `GET http://127.0.0.1:8005/` → `200` и cookie `csrftoken`;
- вход `POST /` (`phone`, `access_code`, `action=login`) на `8005` → `302` и
  cookie `sessionid`; тот же cookie на `8001` → `200 text/event-stream` и первый
  кадр `event: operational` с `version`/`context` роли (проверено для `driver`
  и `excavator_operator`).

Чего T-split **не** доказывает: браузер на `8005` обратится к
`/realtime/stream/` того же origin (`8005`), а не к `8001`. Переадресация
маршрута — задача nginx, которого локально нет. Поэтому браузерный прогон идёт
в T-origin, а T-split остаётся HTTP-доказательством одной сессии. CORS в
продукт не добавлялся.

## 4. Топология T-origin (для настоящего EventSource)

Всё приложение на ASGI на одном origin; `/static/**` отдаёт локальная обёртка
`tools/sse_pilot/static_asgi.py` (не продукт, `config/settings.py` не менялся,
CORS не добавлялся).

```powershell
# окно 3 — тот же Django, но ASGI + локальная статика
& $py -m uvicorn tools.sse_pilot.static_asgi:application --host 127.0.0.1 --port 8006 --env-file .env
```

Это **локальная адаптация**, а не production-маршрут: в production перед
приложением стоит nginx и статику отдаёт он. Порядок `8005`+`8001` при этом
можно остановить — для T-origin достаточно `8006`.

## 5. Статика и same-origin (по уточнению copper-lead)

- `config/settings.py:252` — `STATIC_URL='static/'` (без ведущего слэша), но в
  **отрендеренном HTML** ссылки абсолютные: `GET /` и `GET /excavator/work/`
  (после входа) содержат `/static/css/app.css?...`, `/static/js/...`.
  Относительного `static/...` и «сломанного» `/driver/static/...` не
  воспроизведено; страница `/excavator/work/` (два уровня) отдаёт `200` и
  абсолютные ссылки. Механизм — нормализация в staticfiles-хранилище Django;
  `config/settings.py` не правился.
- При `DJANGO_DEBUG=false` `runserver` без `--insecure` и `uvicorn` без обёртки
  статику не отдают. Поэтому: T-split — `runserver --insecure`; T-origin —
  `tools/sse_pilot/static_asgi.py`. Обёртка отвечает только на `GET/HEAD
  /static/**` и добавляет заголовок `X-SSE-Stand-Static: local-tool`; остальное
  уходит в `config.asgi:application`.
- Проверено: `GET http://127.0.0.1:8006/static/js/realtime-client.js` → `200`,
  101 483 байта; тот же путь на ограниченном `8001` → `404 sse_endpoint_only`.

## 6. Живой дым и повторная доставка (harness)

Готовый harness: `tools/sse_pilot/t016_stand.live.test.mjs`
(`node --test`, Node v24.19.0; глобального `EventSource` в Node нет — браузер
запускается через CDP: Edge 157.0.4341.46, `--headless=new`).

```powershell
$env:SSE_STAND_PHONE='+79000000101'; $env:SSE_STAND_ACCESS_CODE='111111'
node --test tools/sse_pilot/t016_stand.live.test.mjs

# вторая роль (проверка одной сессии и потока; браузерный шаг пропускается)
$env:SSE_STAND_PHONE='+79000000102'; $env:SSE_STAND_ACCESS_CODE='222222'
$env:SSE_STAND_ROLE_APP_CODE='excavator_operator'
node --test tools/sse_pilot/t016_stand.live.test.mjs
```

Что делает harness: 401 без cookie, 404 на чужой путь, вход на WSGI и поток на
ASGI с тем же cookie, статика T-origin, браузерный `EventSource`
(`withCredentials:true`) с ловлей `operational`/`heartbeat`/`reset`, одно живое
бизнес-событие (простой водителя `POST /driver/downtime/`, затем закрытие) и
замер `commit→receive`.

Observed 2026-11-12 (loopback, один прогон): snapshot-кадр 32 события /
10 474 байта, heartbeat 807 байт, живой кадр 1 323 байта, `commit→receive`
220 мс (< `SSE_SAFETY_SCAN_SECONDS=5`, то есть сработал Redis-wakeup, а не
аварийный скан БД), ошибок `EventSource` 0. Это единичные замеры, не p95/p99;
30-минутный прогон и трафик — e2e (`T-016` WP-5).

Серверная часть replay/идемпотентности закреплена тестами
`core/test_sse_pilot.py:1402-1642`: повторная доставка того же кадра с тем же
`id`, `applied` приоритетнее `Last-Event-ID`, смена точки разгрузки в одном рейсе
даёт два события с разными `version`, разные события доставляются оба,
идентичные дубли схлопываются, но курсор доходит до high-water.

## 7. Прогоны тестов SSE на PostgreSQL (итерация 2)

Тестовая БД `test_sse_local` создаётся вручную: у `sse_local` нет `CREATEDB`.
`TransactionTestCase` после каждого теста делает `flush`, поэтому после тяжёлого
прогона база теряет строки миграций-сидов (`down_times`, роли) и для следующего
«полного» прогона её надо пересоздавать.

```powershell
& 'D:/CopperDevTools/sse-stand/pgsql/bin/psql.exe' -h 127.0.0.1 -p 55432 -U postgres -d postgres -c "drop database if exists test_sse_local;"
& 'D:/CopperDevTools/sse-stand/pgsql/bin/psql.exe' -h 127.0.0.1 -p 55432 -U postgres -d postgres -c "create database test_sse_local owner sse_local encoding 'UTF8' template template0;"

# 41 тест, ~16 минут (на PG ~24 с/тест из-за flush + post_migrate)
& $py manage.py test core.test_sse_pilot --settings=config.settings_sse_local --keepdb

# совместный прогон с командой стенда: 48 тестов, ~18 минут
& $py manage.py test core.test_sse_stand_setup core.test_sse_pilot --settings=config.settings_sse_local --keepdb

# SQLite-контроль (in-memory), секунды
$env:DJANGO_DB_ENGINE='sqlite'
& $py manage.py test core.test_sse_pilot --settings=config.settings_sse_local
```

Ловушка, из-за которой PG-прогон был красным при зелёном SQLite:
`SseStreamDatabaseTests` нельзя помечать `reset_sequences = True`. Django на
PostgreSQL сбрасывает **каждую** последовательность в 1
(`django/db/backends/postgresql/operations.py:216-233`), а тестовая база после
миграций уже содержит seed-роли `users_role` (id 1..5). Первый же
`Role.objects.create` получает id=1 и падает на `users_role_pkey`. На SQLite
сброс последовательностей не поддерживается, поэтому ошибки не было; в
изоляции она тоже не воспроизводилась, если база уже была очищена прошлым
`TransactionTestCase`. В `setUp` роли теперь создаются через `get_or_create`,
а `effective_at` у проверяемых `PENDING` — в будущем, чтобы экран состояния не
применял их сам (`core/views.py:199-201`, окно reconcile 5 с).

## 8. Останов

Стенд останавливается `Ctrl+C` в окне каждого процесса (`8001`, `8005`, `8006`).
В профиле `copper-realtime` нет прав на `Stop-Process`/`taskkill`, поэтому
процессы, запущенные в фоне другой ролью, останавливает её сессия.

После третьей неудачи (5-секундный предел на попытку) экран применяется с диагностикой
`operational-voice-handoff-degraded` (`reason: voice_handoff_failed`), а
неподтверждённая голосовая пачка уходит один раз при следующем событии/снимке.
Поток SSE после фона/паузы перезапускает собственный watchdog клиента (≤2 с +
backoff), независимо от доступности `/realtime/state/` — это проверено тестами
`T-016 §10.3`/`§10.1` и живым прогоном на `8006` (возврат за ~2.5 с при
заблокированном опросе).

## 9. Ограничения

- Нет nginx: production-маршрутизация «страницы WSGI + поток ASGI на одном
  origin» локально не воспроизводится, только HTTP-доказательство сессии.
- Браузерный прогон — только T-origin; он не заменяет T-split.
- 30+ минут, ≥ 6 разрывов, p95/p99, трафик в покое — e2e/QA, не этот runbook.
- На время браузерного прогона в окно инжектируется скрипт антивируса
  (`me.kis.v2.scr.kaspersky-labs.com`); на поток он не влиял, но в отчётах его
  не считать частью приложения.
- `tools/sse_pilot/_probe_*.test.mjs` — разовые пробы этой сессии; при release
  их нужно удалить (роль realtime не имеет прав на удаление файлов).
