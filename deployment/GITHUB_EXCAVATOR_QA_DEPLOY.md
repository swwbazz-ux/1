# Изолированный QA-канал проверки связи приложений

## Назначение

Этот канал предназначен только для стенда приложений Водителя и
Экскаваторщика:

- `qa-admin.driverform.ru`;
- `qa-driver.driverform.ru`;
- `qa-excavator.driverform.ru`.

Канал не публикует production, APK или nginx-конфигурацию. Он не использует
production receiver, production secrets и `production-files.txt`.

## Жёсткие границы

QA receiver содержит фиксированные значения, которые пакет изменить не может:

- каталог: `/srv/accounting-mvp-excavator-qa`;
- база: `accounting_mvp_excavator_qa`;
- Redis: отдельный endpoint/ACL-user, ненулевая DB и QA-префикс из `.env`;
- приложение: `accounting-mvp-excavator-qa`;
- симулятор: `accounting-mvp-excavator-qa-simulator`;
- socket: `/run/accounting-mvp-excavator-qa/app.sock`;
- Firebase: `/etc/accounting-mvp-excavator-qa/firebase-service-account.json`;
- резервные копии: `/var/backups/accounting-mvp-excavator-qa/releases`
  (root-owned, вне writable application root).

Receiver отклоняет обычные режимы `deploy`, `rollback`, runtime-каталоги,
секреты, symlink, traversal и любой пакет не из QA-протокола.

## Однократная серверная подготовка

До первого запуска отдельным инфраструктурным изменением должны быть
подготовлены:

1. TLS и отдельные nginx vhost для трёх QA-хостов. Каждый vhost направляется
   только в QA socket.
2. QA `.env` с `DEBUG=False`, secure cookies, точными host aliases, отдельными
   PostgreSQL/Redis и QA Firebase project. Файлы `.env` и
   `/etc/accounting-mvp-excavator-qa/firebase-service-account.json`
   должны принадлежать `root:accounting-qa` и иметь режим `0640`.
3. Отдельные системные user/group `accounting-qa`. Оба QA systemd service
   обязаны содержать `User=accounting-qa` и `Group=accounting-qa`.
   Этот пользователь не должен входить в production-группу и не должен читать
   production `.env`. Candidate Django-код receiver запускает только с этим
   UID/GID, очищенными supplementary groups и `NoNewPrivileges`.
   Корень приложения остаётся root-owned; код имеет режимы `0640/0750`.
   Только runtime-каталоги `media`, `private_media` и
   `staticfiles` заранее выдаются QA-пользователю. Для nginx сохраняется
   только необходимый traverse/read доступ к этим runtime-каталогам.
   Пользователь `www-data` добавляется только в группу `accounting-qa` для
   чтения QA static/media и доступа к socket с umask `0007`; сам
   `accounting-qa` не включается в `www-data` или production-группы.
4. Отдельный systemd service приложения и симулятора.
5. Root-owned policy `/etc/accounting-mvp-excavator-qa/release-policy.json`
   с правами `0600`:

   ```json
   {
     "schema": 1,
     "target": "accounting-mvp-excavator-qa",
     "instance_id": "уникальный-идентификатор-длиной-не-менее-16-символов",
     "database_name": "accounting_mvp_excavator_qa",
     "database_user": "отдельная-роль-qa",
     "redis_scheme": "redis",
     "redis_host": "127.0.0.1",
     "redis_port": 6380,
     "redis_username": "accounting-qa",
     "redis_database": 2,
     "cache_prefix": "accounting-mvp-excavator-qa",
     "firebase_project_id": "отдельный-firebase-qa-project"
   }
   ```

   Policy не содержит паролей. Receiver сравнивает с ним фактические Django
   settings, а не доверяет одним значениям из `.env`.
   Каталог `/var/backups/accounting-mvp-excavator-qa/releases` создаётся заранее
   как `root:root` с режимом `0700`; все его родители должны быть root-owned и
   недоступны для записи группе и остальным. Receiver не создаёт и не чинит этот
   каталог автоматически, а завершает операцию с ошибкой при неверном владельце
   или режиме.
6. Отдельный SSH key с forced command:

   `restrict,command="sudo -n /usr/local/sbin/accounting-github-qa-deploy-receiver"`

7. Root-owned копия
   `deployment/server/accounting_github_qa_deploy_receiver.py` в указанном
   forced command. Builder и receiver используют protocol schema 2; старый
   schema-1 receiver обязан отклонить новый пакет.
8. GitHub Environment `qa` с обязательным reviewer и ограничением только
   на control-ветку, а также секретами:
   `QA_DEPLOY_HOST`, `QA_DEPLOY_USER`, `QA_DEPLOY_KEY`, `QA_KNOWN_HOSTS`.

Production SSH key и production receiver использовать запрещено.

## Режимы workflow

Workflow: `.github/workflows/qa-backend-deploy.yml`.

Оба job имеют жёсткий общий timeout, а receiver задаёт отдельные timeout для
`systemctl`, Django, PostgreSQL и HTTP-проверок. Изменяющая фаза receiver
ограничена 30 минутами, отдельный recovery — 15 минутами, а внешний release job
— 60 минутами. Поэтому receiver обязан завершить применение либо откат раньше,
чем GitHub оборвёт SSH. При зависании после остановки QA-сервисов выполняется тот
же точный файловый/БД-откат; если сам откат не завершился, сервисы остаются
остановленными.

- `qa_audit` + подтверждение `QA_AUDIT` — проверяет действующий QA runtime,
  три HTTPS-маршрута, роли, БД, Redis, Firebase, миграции и QA-администратора.
  При первом bootstrap, когда receiver ещё не создавал `current.json`, успешный
  результат явно содержит `QA_AUDIT_UNTRACKED_RUNTIME=1` и
  `deployed_commit=untracked`: runtime и границы проверены, но commit старого
  стенда ещё не аттестован. После первого `qa_deploy` отсутствие tracked commit
  больше не допускается как нормальное состояние: receiver сохраняет отдельный
  root-owned initialized marker и fail-closed отклоняет удалённый, подменённый
  каталогом или symlink `current.json`.
- `qa_verify` + `QA_VERIFY` — собирает полный tracked backend выбранного
  commit, проверяет его во временном staging и не меняет файлы/сервисы live-
  контура. Candidate-код выполняется без root, а PostgreSQL-команды проверки
  получают read-only `PGOPTIONS`. Receiver сохраняет короткоживущую
  server-side квитанцию и выводит `QA_VERIFICATION_ID`,
  `QA_MIGRATION_PLAN_SHA256` и `QA_SNAPSHOT_SHA256`.
- `qa_deploy` + `QA_DEPLOY_MIGRATIONS` — допускается только с
  тремя значениями предыдущего успешного `qa_verify`:
  `verified_snapshot_sha256`, `verification_id` и
  `migration_plan_sha256`. Receiver повторно сверяет квитанцию и состояние
  БД до и после остановки QA-сервисов, затем создаёт файловую и PostgreSQL-
  копию, применяет одобренный план, готовит QA-сценарий, собирает статику и
  проверяет readiness.
- `qa_rollback` + `QA_ROLLBACK` — принимает только точный
  `qa-github-...-before` из успешного deploy.

Для `qa_verify` и `qa_deploy` пакет сначала фиксируется неизменяемым
artifact, затем в отдельном source-каталоге выполняются целевые Django-тесты;
release job ждёт их успешного завершения.
Изменение `requirements.txt` receiver намеренно блокирует: зависимости должны
быть отдельно подготовлены в QA `.venv`.

## Порядок первого запуска

1. Создать и защитить control-ветку
   `codex/github-qa-deploy-20260923`.
2. Выполнить серверную подготовку и установить QA receiver.
3. Запустить `qa_audit`. До исправления TLS/host/Redis/FCM он обязан падать.
4. Запустить `qa_verify` для полного SHA кандидата и сохранить
   `QA_SNAPSHOT_SHA256`, `QA_VERIFICATION_ID` и
   `QA_MIGRATION_PLAN_SHA256`.
5. Повторить запуск как `qa_deploy`, указав тот же SHA и полученный snapshot
   hash, verification id и migration-plan hash.
6. Выполнить `qa_audit`.
7. Только после этого установить side-by-side `.qa` APK на тестовый телефон и
   пройти foreground/background/screen-off/network-off E2E.

## Откат

Каждый deploy до изменения сохраняет:

- прежние файлы и перечень вновь созданных файлов;
- PostgreSQL dump custom format;
- SHA-256 дампа;
- release manifest.

При ошибке deploy receiver автоматически возвращает файлы и базу. Для ручного
отката используется backup id из строки `QA_RELEASE_OK`. Receiver не вызывает
restart/reload production-сервисов или nginx.
