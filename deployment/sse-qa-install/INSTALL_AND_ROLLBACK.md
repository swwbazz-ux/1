# Установка, первый запуск и откат

## 1. Почему нужен отдельный receiver mode

Существующие `deploy`/`apply_data` работают с production release и не должны
использоваться для QA. Пакет добавляет только фиксированные режимы:

| Режим | Confirmation | Назначение |
|---|---|---|
| `verify_sse_qa` | `VERIFY_SSE_QA` | до установки — read-only preflight; после установки — контролируемая live-проверка QA |
| `install_sse_qa` | `INSTALL_SSE_QA` | создание отдельного отключённого QA-контура |
| `enable_sse_qa` | `ENABLE_SSE_QA` | первый запуск после всех gate, максимум 1–2 клиента |
| `smoke_sse_qa` | `SMOKE_SSE_QA` | два штатных входа, экраны, synthetic trip, catch-up и реальная SSE-доставка |
| `disable_sse_qa` | `DISABLE_SSE_QA` | немедленно закрыть ingress и остановить только QA units |
| `remove_sse_qa` | `REMOVE_SSE_QA` | удалить только marker-owned QA units, image и файлы |

Режимы не принимают shell-команд, пути, имена unit, порты или SQL. Release
builder вкладывает полный пакет и зафиксированный wheelhouse только для
`install_sse_qa`; `enable/smoke/disable/remove` получают малый control ZIP с
единственным allowlisted controller и не используют PyPI/runtime rebuild. Для `install_sse_qa`
зашифрованный GitHub Environment secret `SSE_QA_SECRETS_JSON_B64`. Receiver
проверяет exact target names, SHA-256, package manifest и commit SHA.

## 2. Последовательность через защищённый GitHub Actions

Все действия выполняются только из control-ветки
`codex/github-production-deploy-20260916`, с полным SHA и Environment
`production`. Прямой SSH не используется.

1. Независимо проверить пакет, patch `github-actions/control-channel.patch`,
   R3 source и логи локальных тестов.
2. После отдельного разрешения сделать commit/push patch в control-ветку,
   сохранив её актуальные изменения.
3. Выполнить `verify_receiver` → `update_receiver` для exact SHA, как в
   действующем протоколе. Это только обучает receiver новым фиксированным
   режимам.
4. Добавить в защищённый GitHub Environment один secret
   `SSE_QA_SECRETS_JSON_B64`, созданный из `secrets/secrets.example.json` после
   замены всех placeholder. Secret не входит в Git и логи.
5. Запустить `verify_sse_qa / VERIFY_SSE_QA`. PASS требует свободных портов
   55432/6381, отсутствия конфликтующих путей, cgroup v2 cpu/memory/io/pids,
   loop/ext4, PostgreSQL 16, Redis 7, nginx и минимум 8 GiB свободного диска.
6. Отдельным разрешением запустить `install_sse_qa / INSTALL_SSE_QA`.
   Установка оставляет ingress выключенным и не перезапускает production.
   Весь controller вместе с venv/pip, cluster init, migrations, seed и
   collectstatic выполняется в transient `sse-qa-install.service`, который
   вместе с QA PostgreSQL/Redis находится под единым parent `sse-qa.slice`
   (1 CPU, 2 GiB RAM, swap 0, 256 tasks). Первый запуск использует проверяемый
   runtime slice и после успеха передаёт lifecycle marker-owned persistent unit.
7. Повторить `verify_sse_qa`. Это уже не полностью read-only операция:
   проверяются owner marker, units, loop mount, лимиты, `max_connections`, role
   limits, live cgroup membership, Redis ACL, synthetic fixture и
   `SSE_PILOT_ENABLED=false`; сам installed verify выполняется внутри того же
   parent slice; выключенные
   QA PostgreSQL/Redis временно запускаются и обязательно снова останавливаются
   с проверкой фактического состояния.
8. Отдельно согласовать DNS-запись `sse-qa.driverform.ru`, TLS certificate,
   allowlist CIDR и окно первого запуска. Покупка домена не требуется.
9. Запустить `enable_sse_qa / ENABLE_SSE_QA`. Весь фиксированный enable-controller,
   включая проверки Django до и после старта служб, выполняется внутри общего
   QA parent slice; receiver остаётся в своей исходной cgroup. Controller
   сначала on-demand активирует persistent slice, поэтому путь работает и после
   reboot без постоянного включения пустого QA slice в boot target. Затем он
   включает ровно QA site и стартует ровно QA units, а ошибка или SIGTERM
   запускает обратное выключение kill-switch/ingress/services.
10. Запустить `smoke_sse_qa / SMOKE_SSE_QA` внутри того же parent slice: два штатных логина, оба рабочих
    экрана, один synthetic trip, journal catch-up и получение события по
    реальному ASGI SSE.
11. Подключить 1–2 тестовых клиента. Снять event-loop lag, heartbeat gaps,
    CPU/RAM/FD, DB connections, Redis clients и ошибки. Никакой нагрузки
    80–96 клиентов в этом этапе нет.

## 3. Kill switch и откат

### Быстрое выключение без удаления

`disable_sse_qa / DISABLE_SSE_QA`:

1. удаляет только symlink QA nginx site и проверяет `nginx -t`;
2. делает nginx reload, не restart;
3. останавливает `sse-qa-asgi`, `sse-qa-wsgi`, `sse-qa-reconcile`,
   `redis-sse-qa`, `postgresql@16-sseqa`;
4. оставляет 6 GiB image и данные для расследования/повторного запуска.

Production unit `accounting_mvp`, PostgreSQL cluster production, Redis 6379,
production nginx files и `/srv/accounting-mvp` не входят в allowlist.

### Полное удаление

Только после отдельного разрешения: `remove_sse_qa / REMOVE_SSE_QA`.

Receiver сначала повторяет disable, затем проверяет ownership journal
`SSE_QA_OWNERSHIP_V2`, marker `SSE_QA_INSTALLATION_V2`, хеш каждого внешнего
файла, реальный PG data directory и backing file loop mount. Затем отмонтирует `/srv/sse-qa`,
удаляет cluster registration `16/sseqa`, QA units/drop-ins/site, image и
пользователя `sseqa`. Неожиданный marker, symlink, mount source или unit
останавливает соответствующую опасную операцию и сохраняет журнал для
диагностики. Partial install можно повторно disable/remove. Production пути не
вычисляются из переменных и не удаляются.

### Откат receiver

После полного удаления QA подготовить revert только receiver/workflow patch,
затем штатно выполнить `verify_receiver` → `update_receiver` для exact revert
SHA. Application rollback mode для этого не используется.

## 4. Действия, требующие новых разрешений

- commit/push patch и обновление receiver;
- добавление GitHub Environment secret;
- `install_sse_qa`;
- DNS/TLS/nginx enable;
- `enable_sse_qa` и подключение 1–2 клиентов;
- `smoke_sse_qa`;
- любой тест реальным телефоном;
- последующий нагрузочный этап 80–96 клиентов;
- `remove_sse_qa` и откат receiver.
