# Первый запуск и граница следующего этапа

## Разрешённый первым этапом сценарий

Продолжительность: 30 минут после 10-минутного прогрева. Клиенты: сначала один
Водитель, затем один Экскаваторщик; одновременно не более двух SSE streams.

Собираются:

- every-second `event-loop-lag.jsonl` из ASGI loop: p50/p95/p99/max;
- heartbeat scheduled/sent/received gaps;
- commit→receive и receive→apply для 10 синтетических операций;
- cursor/replay/reset/deduplication и голосовой semantic handoff;
- `systemd-cgtop`, `cpu.stat`, `memory.current/events`, tasks, FD;
- PostgreSQL connections/locks/idle-in-transaction/temp bytes;
- Redis clients/memory/rejected/evicted/pubsub;
- production public health до, во время и после.

SSE сначала включается для одной synthetic role. Второй клиент подключается
только если первые 10 минут не достигли ни одного stop criterion из
`RESOURCE_LIMITS.md`.

Перед физическими клиентами фиксированный `smoke_sse_qa` подтверждает два
штатных входа, оба рабочих экрана, создание synthetic trip и его получение
Driver одновременно через journal catch-up и настоящий ASGI SSE stream.

Summarizer принимает обязательные `--start-unix-ns`/`--end-unix-ns` и предел
свежести. Каждое из двух пятиминутных окон считается отдельно; накопленный файл
целиком больше не выдаётся за отдельное окно.

## Следующий этап, не входящий в пакетный запуск

Отдельное решение после отчёта 1–2 клиентов:

- наблюдение полного сменного пика;
- A/B/C сравнение;
- ступени 5 → 10 → 20 → 40 → 80 → 96 клиентов;
- lost wakeup, Redis/ASGI restart, reconnect storm;
- критерии остановки каждой ступени и внешний генератор нагрузки;
- Firebase, APK, Doze и физический Android.

Ни workflow, ни nginx config этого пакета не позволяют открыть более двух SSE
connections без нового reviewed commit, поэтому случайный запуск 80–96
клиентов в первом этапе блокируется технически.
