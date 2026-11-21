# Локальные заготовки инфраструктуры SSE-пилота

Все файлы в этом каталоге — **примеры, не установленная конфигурация**.
Они не входят в production/QA deploy manifest и не должны копироваться на
сервер без отдельного разрешения, проверки exact SHA и плана отката.

Цели примеров:

- отдельный ASGI-процесс только для `/realtime/stream/`;
- прежний WSGI остаётся владельцем остальных запросов;
- SSE по умолчанию выключен;
- отдельный Redis instance и обязательный channel namespace;
- FCM/WebPush credentials отсутствуют, внешние отправки невозможны;
- endpoint доступен только на loopback до отдельного QA-этапа.

Файлы:

- `requirements-sse-pilot.txt` — отдельная зависимость ASGI-процесса; основной WSGI requirements не меняется;
- `asgi-pilot.env.example` — безопасные локальные переменные;
- `accounting-mvp-sse-pilot.service.example` — отдельный ASGI unit;
- `nginx-sse-location.conf.example` — только location для SSE;
- `redis-sse-pilot.conf.example` — отдельный Redis на loopback;
- `redis-sse-pilot.acl.example` — ограничение channel namespace;
- `A_B_C_RUNBOOK.md` — воспроизводимое сравнение A/B/C;
- `QA_PHONE_PLAN.md` — следующий, ещё не выполненный этап.

Локальная установка отдельного ASGI runner:

```powershell
python -m pip install -r deployment\sse-pilot\requirements-sse-pilot.txt
```

## Локальный запуск и выключение

Рабочий прототип подготовлен в
`C:\codex-tmp\sse-pilot-f99` на ветке
`codex/local-sse-pilot-f99` от production source
`f99c48cacdc718c303d6b30197701107d33e688c`. Неизменённое сравнение A лежит в
`C:\codex-tmp\sse-pilot-baseline-f99`.

Из `СИСТЕМА_MVP\backend` с отдельными локальными переменными окружения:

```powershell
$env:SSE_PILOT_ENABLED='true'
$env:SSE_REDIS_URL=''  # допустимо только для проверки DB safety scan без Redis
python -m uvicorn config.sse_asgi:application --host 127.0.0.1 --port 8001
```

В отдельном процессе обязательно запустить независимое от HTTP polling
согласование назначений и просроченных ручных рейсов:

```powershell
python manage.py run_operational_reconciliation --tick-seconds 1
```

Полный изолированный вариант использует переменные из
`asgi-pilot.env.example`, disposable PostgreSQL и Redis `127.0.0.1:6381` с
уникальным namespace. Локальный SQLite годится только для функциональной
проверки, но не для приёмки нагрузки.

Kill switch:

```powershell
$env:SSE_PILOT_ENABLED='false'
```

После перезапуска локальных процессов клиент остаётся на прежнем foreground
polling. Applied cursor, голосовые operation keys и offline outbox не удалять:
они нужны для безопасного восстановления и не являются очередью SSE.

Собранные debug-артефакты (не установлены и не предназначены для сотрудников):

- `mobile\capacitor-shell\dist\driver_sse_pilot-debug.apk` —
  `ru.copperresources.driver.ssepilot`;
- `mobile\capacitor-shell\dist\excavator_sse_pilot-debug.apk` —
  `ru.copperresources.excavator.ssepilot`.

Проверка синтаксиса после появления локального Linux-стенда, без установки:

```bash
systemd-analyze verify ./accounting-mvp-sse-pilot.service.example
nginx -t -c /absolute/path/to/a/local/test-nginx.conf
redis-server ./redis-sse-pilot.conf.example --test-memory 2
```

Последняя команда проверяет память, а не весь конфиг Redis. Полная проверка —
запуск отдельного локального процесса на порту `6381` и `redis-cli -p 6381 ping`.
