# Изоляция PostgreSQL

Отдельная database и role в рабочем PostgreSQL разделяют права и данные, но
**не изолируют ресурсы**. Они всё равно делят:

- общий `max_connections` и reserved connections;
- shared buffers, WAL/checkpointer/autovacuum;
- CPU, RAM, disk latency/IOPS и fsync queue;
- аварийный restart/upgrade одного instance;
- kernel page cache и лимиты systemd unit.

Поэтому этот пакет не создаёт `sseqa` database в production instance.

Он создаёт отдельный PostgreSQL cluster/instance `16/sseqa`:

- port `55432`, loopback only;
- отдельные data/WAL/config/logs внутри 6 GiB QA image;
- отдельный `postgresql@16-sseqa.service` в `sse-qa.slice`;
- собственные `max_connections=16`, memory/temp/WAL limits;
- отдельные roles/database и `pg_hba.conf`;
- отсутствие production dump и `dblink`/FDW/replication permissions.

Это даёт принудительные CPU/RAM/process/disk-byte границы, однако instance всё
ещё делит с production ядро ОС, physical disk и общий IO scheduler. `IOWeight`
— относительная, а не абсолютная гарантия latency. Поэтому даже такая схема
должна начинаться с 1–2 клиентов и немедленно выключаться при влиянии на
production. Полную независимость даёт только отдельная VM/host.

