# Установка, первый запуск и откат

## Обязательный gate хранения секретов

Перед отдельно разрешённой установкой receiver должен подтвердить fixed
`/usr/bin/systemd-creds` и существующий root-only
`/var/lib/systemd/credential.secret`. Установщик шифрует все QA credentials до
первой мутации и прекращает работу, если key отсутствует, ciphertext не
создаётся или не может быть проверен. Создание/замена host key на существующем
сервере не входит в install и требует отдельного решения. Порядок хранения,
runtime-доступа и recovery описан в `SECRET_STORAGE.md`.

## 1. Почему нужен отдельный receiver mode

Существующие `deploy`/`apply_data` работают с production release и не должны
использоваться для QA. Пакет добавляет только фиксированные режимы:

| Режим | Confirmation | Назначение |
|---|---|---|
| `verify_sse_qa` | `VERIFY_SSE_QA` | только строгий начальный read-only preflight; при найденной полной или частичной установке — отказ без запуска служб |
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

При `install_sse_qa` decoded JSON передаётся builder и receiver только через
stdin. Временный transport tar.gz содержит plaintext JSON, поэтому builder
создаёт его `0600` до первого байта, не перезаписывает существующий/symlink
destination, а workflow удаляет архив шагом `always()` после передачи или
ошибки. Постоянное хранение на сервере при этом остаётся только зашифрованным
через systemd credentials.

Control checkout и источник QA разделены явно. Workflow остаётся на exact SHA
control-ветки, а application/runtime извлекает только из зашитого в коде
candidate `fb81480a9709e3a26ccbeb74aaefbaa08a3d722c`. Исправленный controller
входит в проверяемый control patch отдельным файлом и допускается receiver
только при exact SHA-256. Пользовательского candidate input нет. Manifest и
логи раздельно фиксируют control SHA, candidate SHA, controller/runtime и
собранный package hash.

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
4. Запустить ровно один `verify_sse_qa / VERIFY_SSE_QA` и остановиться с
   отчётом. Эта операция не входит в `sse-qa.slice`, не вызывает `systemd-run`,
   не стартует и не останавливает QA PostgreSQL/Redis и отказывает при marker,
   ownership journal, любом managed path, user/group/cluster conflict или
   неизвестном результате `id/getent/pg_lsclusters`.
5. Только по фактическому preflight-отчёту запросить новое разрешение. Тогда
   добавить в защищённый GitHub Environment secret `SSE_QA_SECRETS_JSON_B64`,
   созданный из `secrets/secrets.example.json` после замены placeholder.
6. Отдельным новым разрешением запустить `install_sse_qa / INSTALL_SSE_QA`.
   Установка оставляет ingress выключенным и не перезапускает production.
   Весь controller вместе с venv/pip, cluster init, migrations, seed и
   collectstatic выполняется в transient `sse-qa-install.service`, который
   вместе с QA PostgreSQL/Redis находится под единым parent `sse-qa.slice`
   (1 CPU, 2 GiB RAM, swap 0, 256 tasks). Первый запуск использует проверяемый
   runtime slice и после успеха передаёт lifecycle marker-owned persistent unit.
7. Не повторять `verify_sse_qa` после установки: этот mode намеренно остаётся
   только начальным preflight и откажет при marker/journal. Фактическая
   post-install проверка выполняется внутри самого `install_sse_qa`: mount,
   применённые cgroup limits/membership, PostgreSQL/Redis, миграции, synthetic
   fixture, kill switch и итог `enabled=false clients=0`. Это controller result,
   а не отдельный независимый zero-residue scanner.
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

`remove_sse_qa` подтверждает собственные ownership guards и итоговые controller
checks. Отдельный независимый `linux_zero_residue_scan.sh` доказан только в
disposable harness и receiver автоматически не запускает его на рабочем
сервере. Для серверного этапа фактическое доказательство — точный controller
summary плюс отдельный последующий read-only inventory разрешённого канала.
Fault/cancel disposable cycle на рабочем сервере запускать нельзя.

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
