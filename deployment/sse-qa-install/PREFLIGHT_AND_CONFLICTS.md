# Проверка конфликтов и механизмов изоляции

## Уже подтверждено пятисекундным read-only снимком 27.09.2026

- Ubuntu/systemd server, 8 logical CPU;
- RAM 15.57 GiB, available в снимке 14.20 GiB, swap отсутствует;
- disk total 156.40 GiB, available 122.84 GiB;
- PostgreSQL 16.15 работает; production использует собственный instance;
- Redis 7.0.15 работает на 6379;
- обращение к 127.0.0.1:6381 завершилось `unreachable`;
- production application и nginx активны;
- снимок не показал исчерпания DB connections/locks/Redis memory.

Это не подтверждает текущее состояние портов, loop devices, cgroup controllers,
свободного места в момент установки или возможность создать новый cluster.

## Обязательный server-side preflight перед записью

`verify_sse_qa` проверяет и останавливается при любой ошибке:

1. x86_64 architecture and `systemctl`, `systemd-analyze`, `losetup`, `/dev/loop-control`, `mkfs.ext4`,
   `findmnt`, `fallocate`;
2. единый cgroup v2 и controllers `cpu memory io pids`;
3. `pg_createcluster`/`pg_dropcluster`, PostgreSQL major 16;
4. `redis-server`, `redis-cli`, `nginx`, точный `/usr/bin/python3.12` и Linux
   wheelhouse с SHA-256 каждого wheel;
5. bind 127.0.0.1:18080/18082/55432/6381;
6. отсутствие cluster `16/sseqa` и любого объекта из единого полного списка:
   WSGI/ASGI/reconcile/target/mount/drop-in/logrotate/nginx/config/app/state;
   при повторной проверке — валидный ownership journal и хеши;
7. минимум 8 GiB free под `/var/lib` и 3 GiB `MemAvailable`;
8. отсутствие symlink/junction/archive traversal, duplicates и undeclared
   package files;
9. отсутствие пересечения с `/srv/accounting-mvp`, production PostgreSQL
   `16/main`, Redis 6379 и production env;
10. для enable: готовый TLS certificate, непустой CIDR allowlist, htpasswd,
    `nginx -t`, только loopback listeners PostgreSQL/Redis и ровно два synthetic
    accounts.

Предустановочный `systemd-analyze verify` заменяет только отсутствующие
Exec*-executables и runtime paths во временной копии unit, не игнорирует exit
code и продолжает проверять структуру, лимиты и связи. После install исходные
units проверяются без подстановок, включая исполнимость Python/gunicorn/uvicorn.

Повторный установленный verify не является полностью read-only: он различает
выключенную и включённую фазу и проверяет backing
file mount, фактические cgroup properties всех QA units, SQL max/role limits,
Redis ACL от `sseqa`, synthetic fixture и точное значение kill switch. Для
выключенной фазы PG/Redis кратко запускаются только на время проверки и снова
останавливаются; частичный запуск также попадает под гарантированный cleanup,
а неподтверждённая остановка завершает verify ошибкой.

Пока `verify_sse_qa` не выполнен на сервере, доступность loop/cgroup и
отсутствие новых конфликтов считаются **не установленными**, а не
предполагаемыми.
