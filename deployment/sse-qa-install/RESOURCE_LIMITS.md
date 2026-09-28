# Ресурсные границы SSE-QA

Это консервативный **лимит первого запуска**, а не утверждённая ёмкость и не
доказательство готовности к 80–96 клиентам.

## Aggregate cgroup

`sse-qa.slice`:

| Ресурс | Hard/soft limit | Enforcement |
|---|---:|---|
| CPU | hard 100% одного CPU | `CPUQuota=100%`, `CPUQuotaPeriodSec=100ms` |
| RAM | high 1792 MiB, hard 2048 MiB | `MemoryHigh`, `MemoryMax` |
| Swap | 0 | `MemorySwapMax=0` |
| Processes/threads | 256 | `TasksMax=256` |
| IO priority | low relative weight 10 | `IOWeight=10` |
| Disk bytes | hard 6 GiB | fixed ext4 loop image |

`sse-qa-install.service`, PostgreSQL, Redis и остальные QA units являются
детьми **одного** `sse-qa.slice`; это не отдельные складывающиеся бюджеты.
До первого запуска receiver временно устанавливает точную копию slice unit в
`/run/systemd/system`, запускает installer внутри неё, а после успешной установки
удаляет runtime-копию и оставляет marker-owned unit в `/etc/systemd/system`.
При ошибке runtime slice останавливается и удаляется. `enable` и `smoke`
запускаются как `sse-qa-enable.service` и `sse-qa-smoke.service` внутри того же
parent slice. Начальный `verify_sse_qa` является no-service preflight и намеренно
не входит в slice и не запускает transient unit.
Receiver не переносится в
QA cgroup и остаётся в своей действующей группе.
Slice намеренно не включается в boot target. После reboot receiver перед каждым
фиксированным scoped-режимом выполняет только `systemctl start sse-qa.slice`,
проверяет точный `ControlGroup`, а затем запускает дочерний unit; ошибка старта
или иерархии останавливает операцию до запуска controller.

Controller fail-closed сверяет live `CPUQuotaPerSecUSec=1s`, период `100ms`,
`MemoryHigh=1879048192`, `MemoryMax=2147483648`, `MemorySwapMax=0`,
`TasksMax=256`, parent
`ControlGroup=/sse.slice/sse-qa.slice` и фактические `ControlGroup` installer,
PostgreSQL, Redis, enable и smoke. Installed runtime checks выполняются внутри
отдельно разрешённого install/enable lifecycle, а не начального preflight.

На измеренном сервере это не более одного из 8 logical CPU и примерно 12.8%
из 15.57 GiB RAM. Свободный диск в пятисекундном снимке был 122.84 GiB, но
preflight заново требует минимум 8 GiB перед созданием image.

## Внутреннее распределение

| Unit | CPUQuota | MemoryMax | TasksMax |
|---|---:|---:|---:|
| `postgresql@16-sseqa` | 35% | 768 MiB | 96 |
| `sse-qa-asgi` (1 worker) | 25% | 384 MiB | 48 |
| `sse-qa-wsgi` (1 worker, 2 threads) | 20% | 384 MiB | 48 |
| `sse-qa-reconcile` | 10% | 192 MiB | 32 |
| `redis-sse-qa` | 5% | 128 MiB | 24 |

Оставшиеся 5% CPU и 192 MiB RAM — расчётный запас slice для кратких
maintenance/verify процессов; это не отдельная гарантия под конкурирующей
нагрузкой. Если сумма child limits расходится с aggregate, выигрывает более
строгий aggregate limit.

WSGI/ASGI/reconciliation stdout/stderr, Redis, PostgreSQL, nginx QA access/error
и event-loop metrics записываются под `/srv/sse-qa`; ротация 3×5 MiB на файл.
Их абсолютная верхняя граница — общий 6 GiB filesystem вместе с данными.
GitHub Actions output установки не является постоянным локальным journald-файлом
QA. `IOWeight=10` остаётся только относительным приоритетом.

## PostgreSQL connections

- отдельный instance: `max_connections=16`;
- `superuser_reserved_connections=3`;
- `sseqa_app CONNECTION LIMIT 8`;
- `sseqa_maint CONNECTION LIMIT 2`;
- WSGI: 1 process × 2 threads;
- ASGI: 1 process, SSE DB access bounded приложением;
- reconciliation: 1 process;
- `CONN_MAX_AGE=0` на первом запуске.

Тем самым все прикладные QA-процессы вместе не могут открыть более 8 соединений
под app role; maintenance — более 2; весь QA instance не может принять более
16 client backends. Preflight/verify сверяет настройки SQL-запросом.

## Redis и ingress

- `maxmemory 64mb`, `maxclients 32`, `noeviction`;
- только `127.0.0.1:6381`, отдельный ACL user, только prefixed Pub/Sub;
- nginx `limit_conn` — максимум 2 одновременных SSE connections globally и
  максимум 8 HTTP connections с одного разрешённого IP;
- ASGI `--workers 1`, stream max 900s, heartbeat 10s, safety scan 5s.

## Критерии остановки первого запуска

Немедленно выполнить `disable_sse_qa`, если выполняется любое условие:

- production health check ухудшился или появился production 5xx;
- `sse-qa.slice` throttled более 10% любого 60-секундного окна;
- `memory.events` показывает `oom`/`oom_kill`;
- event-loop lag p99 > 100 ms либо max > 500 ms в двух подряд 5-минутных окнах;
- heartbeat gap > 25s или потеря/дубль актуального operation key;
- DB connections > 10 non-reserved либо появляется idle-in-transaction > 30s;
- Redis rejected connections/evictions > 0;
- QA image > 80% или PostgreSQL/Redis слушает не loopback;
- любой запрос/событие пересёк QA/production контекст.

Ступенчатая нагрузка и 80–96 клиентов проектируются только после отчёта этого
малого этапа и нового разрешения.
