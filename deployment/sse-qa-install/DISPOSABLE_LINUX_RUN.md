# Одноразовый Linux/systemd-прогон

Статус на 27.09.2026: **NOT_RUN**. На локальном компьютере WSL не установлен,
а Docker, Podman, Hyper-V tooling, VirtualBox, Vagrant и QEMU отсутствуют.
Windows mock/contract tests не заменяют этот gate.

## Рекомендуемый способ без VDS, домена и рабочего сервера

После отдельного разрешения создать отдельный ручной `workflow_dispatch` для
одноразового GitHub-hosted runner `ubuntu-24.04`. Это не production workflow и
не должно использовать GitHub Environment `production`, receiver, SSH, DNS,
Firebase или какие-либо production secrets.

Обязательные ограничения workflow:

- `permissions: contents: read`;
- checkout action закреплён полным SHA, `persist-credentials: false`;
- запуск только для проверенного полного commit SHA;
- все PIN/password/certificate создаются внутри runner, только синтетические,
  хранятся в `tmpfs` с `0600`, маскируются и не загружаются в artifacts;
- `allow_cidr=127.0.0.1/32`, тестовые hostnames направляются в loopback;
- после получения apt/wheel зависимостей исходящий доступ для QA service users
  блокируется; рабочие домены также направляются в loopback;
- создаётся обязательный marker `/run/sse-qa-disposable-test`;
- при отсутствии systemd как PID 1, cgroup v2 `cpu/memory/io/pids`,
  `/dev/loop-control`, PostgreSQL 16, Redis 7, nginx, Python 3.12 или 8 GiB
  свободного диска результат только `INFRA_BLOCKED/NOT_RUN`, без ослабления
  preflight.

Commit/push такого workflow и его dispatch являются внешними действиями и
требуют отдельного разрешения.

## Обязательный сценарий

1. Проверить archive/package manifest, runtime/wheelhouse SHA и выполнить все
   package tests без skip, включая буквальное имя `srv-sse\\x2dqa.mount`.
2. Зафиксировать чистое состояние units, mounts/loop, listeners, PG clusters,
   users/groups, managed paths и cgroup tree.
3. Выполнить `preflight → install → installed verify → enable → verify → smoke
   → disable → disabled verify → remove`.
4. Отдельно выполнить fault points `after_image_before_marker` и
   `after_postgres_redis_start`; каждый запуск обязан завершиться ошибкой и
   полным cleanup.
5. Cancel выполнять не по фиксированному `sleep`, а после подтверждения active
   install unit, `MainPID>0`, ожидаемой ownership phase и cgroup membership.
6. После каждого сценария выполнить независимый zero-residue scan: отсутствуют
   все QA units/drop-ins, PostgreSQL cluster/role/data, Redis/processes,
   user/group, mount/loop/image, listeners, nginx link, cgroup и managed files.

## Фактические доказательства

- UTC start/end, exact command и exit code каждой фазы;
- runner image/systemd/kernel/cgroup/tool versions и exact source SHA;
- `systemctl show` для parent `/sse.slice/sse-qa.slice` и каждого child;
- во время install — sampler `cpu.stat`, `memory.current/events`, `pids.current`,
  `io.stat`, process tree и кадр одновременных installer/PostgreSQL/Redis под
  одним parent;
- PostgreSQL limits/connections и Redis limits/clients без данных и секретов;
- masked HTTP/BasicAuth checks и строка
  `SSE_QA_BUSINESS_SMOKE_OK ... catchup=1 sse=1`;
- raw stdout/stderr всех normal/fault/cancel фаз;
- SHA-256 manifest evidence и secret/PII scan перед загрузкой.

Результат принимается только при полном PASS normal/fault/cancel, нуле Linux
skip и чистом удалении. Нагрузка 80–96 клиентов в этот workflow не входит.

## Подготовленный ручной workflow

Локально подготовлен отдельный workflow
`.github/workflows/sse-qa-disposable-linux.yml`; его точная копия для
независимого ревью находится в
`github-actions/sse-qa-disposable-linux.yml`. До отдельного разрешения эти
файлы не публикуются и workflow не запускается.

Контракт запуска:

- только `workflow_dispatch`, `ubuntu-24.04`, `permissions: contents: read`;
- обязательны две буквальные фразы подтверждения;
- checkout не принимает пользовательский ref: GitHub фиксирует выбранный ref в
  неизменяемом `GITHUB_SHA`, а `git rev-parse HEAD` после checkout обязан точно
  совпасть с этим SHA события;
- нет `environment`, repository secrets, receiver, SSH, production workflow,
  Firebase, DNS и внешних рабочих credentials;
- зависимости и wheelhouse скачиваются до тестового цикла; затем исходящий
  трафик QA-пользователей блокируется, кроме loopback;
- synthetic secrets и отдельный файл network-smoke создаются только в `/run`,
  имеют режим `0600`, маскируются и удаляются через `if: always()`;
- capability gate проверяет systemd PID 1, cgroup v2 и контроллеры
  `cpu/memory/io/pids`, transient service, loop device, версии ПО, свободные
  RAM/диск/порты и отсутствие управляемых QA-путей;
- отсутствие возможности runner даёт только `INFRA_BLOCKED/NOT_RUN` с
  ненулевым итоговым status; защиты не ослабляются;
- выполняются normal, оба fault point и phase-aware cancel; после каждого
  сценария Linux-cycle выполняет zero-residue scan, а workflow повторяет
  независимый итоговый scan;
- сетевой smoke проходит через настоящий HTTPS/nginx, Basic Auth и оба
  прикладных входа; ожидается реальная SSE-доставка нового synthetic рейса;
- нагрузка 80–96 клиентов отсутствует.

Artifact upload намеренно отсутствует: до запуска неизвестен доступный billing
storage, а создание платных расходов не разрешено. После secret/PII scan каждый
evidence-файл печатается в job log между фиксированными разделителями вместе с
SHA-256 manifest. После разрешённого запуска лог можно сохранить локально:

```bash
gh run view RUN_ID --repo swwbazz-ux/1 --log > sse-qa-disposable-RUN_ID.log
```

## Почему нужны регистрация и отдельный test ref

GitHub разрешает ручной запуск нового workflow только после появления файла в
default-ветке. Поэтому публикация после отдельного разрешения состоит из двух
явных частей:

1. workflow-only регистрация через отдельную ветку и PR в default-ветку;
2. публикация точного проверенного кандидата в
   `codex/sse-qa-disposable-linux-20260927` и dispatch именно его head SHA.

Планируемая команда dispatch (не выполнялась):

```bash
gh workflow run sse-qa-disposable-linux.yml \
  --repo swwbazz-ux/1 \
  --ref codex/sse-qa-disposable-linux-20260927 \
  -f confirmation=DISPOSABLE_SSE_QA_ONLY \
  -f actions_quota_confirmation=AVAILABLE_ACTIONS_QUOTA_CONFIRMED
```

Перед dispatch отдельно проверяется доступный бесплатный Actions quota. Ни
workflow registration, ни push, ни PR/merge, ни dispatch текущим локальным
этапом не разрешены и не выполнялись.

## Уточнения disposable workflow R2

До первого dispatch локально исправлены шесть замечаний независимого review:
wheelhouse manifest использует только basename; package tests выполняются в
новом Python 3.12 venv из этого wheelhouse; nginx явно запускается до цикла;
каждая попытка install читает journal только по своему `InvocationID`;
`SHA256SUMS` не включает себя и повторно проверяется; raw cycle evidence
публикуется только после quiet secret/PII scan, при заранее зарегистрированных
масках всех синтетических значений. Адресные тесты не заменяют реальный Linux
run, поэтому статус normal/fault/cancel остаётся `NOT_RUN`.
