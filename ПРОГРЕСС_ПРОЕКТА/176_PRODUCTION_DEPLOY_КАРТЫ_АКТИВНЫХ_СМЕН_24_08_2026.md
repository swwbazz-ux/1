# Production deploy карты активных смен

Дата: 24.08.2026.

Статус: **ОПУБЛИКОВАНО / PRODUCTION QA PASS**.

## Git checkpoint

- ветка: `codex/admin-live-shift-monitor-2026-08-24`;
- commit: `baeeb99d62752e0dda6cc10e6ece73dc380ebdfa`;
- локальный и удалённый SHA совпали;
- в commit вошли 36 адресных файлов без базы, `.env`, секретов, логов и
  локальных QA-артефактов.

## Граница production-пакета

Production содержал более новые составные версии `users/context_processors.py`
и `users/role_apps.py`. Они не заменялись старыми файлами feature-ветки.
Изменения карты смен наложены поверх фактической серверной версии с сохранением
каталога приложений, новых PWA-релизов и traffic-контракта. Общий статический
релиз поднят `ready-core-traffic-v10 → v11`, Админка —
`system-admin-shell-v19 → v20`.

Серверный staging-каталог прошёл `manage.py check`, контроль миграций,
`migrate --plan` и `collectstatic` до изменения рабочего кода.

## Резервная копия и откат

Rollback-комплект:

`/srv/accounting-mvp/backups/deploy-20260824T071500Z-admin-live-before-baeeb99`

В нём находятся проверенные SHA-256:

- `code-before.tar.gz`;
- `database-before.sql.gz` — полный PostgreSQL dump;
- root-only `rollback.sh`;
- контрольные количества и планы миграций.

Откат восстанавливает прежний код и статику, удаляет новые runtime-файлы и
перезапускает приложение. Аддитивная таблица `users.0019` и её данные при
обычном кодовом откате сохраняются, чтобы откат интерфейса не удалял новые
рабочие heartbeat-записи.

## Миграция и данные

Применена только `users.0019_activeapplicationsession`. Повторный план миграций
пуст.

До и после deploy совпали:

- `Employee=519`;
- `EmployeeAccess=347`;
- `EmployeeShift=98`;
- открытые смены — `3`;
- `AdminActionLog=2612`;
- Django Session — `543`.

До Browser QA новая таблица была пустой. Авторизованная проверка создала ровно
один штатный heartbeat Администратора (`app_code=admin`). Синтетические
сотрудники, смены, рейсы и действия на production не создавались.

Первая техническая попытка после всех успешных проверок автоматически вернула
код из rollback-комплекта из-за символа перевода строки в последней команде
оболочки. Схема и данные не повреждены. Фактическое состояние было подтверждено,
после чего код опубликован повторно с очищенным shell-вводом; итоговый deploy
завершён кодом 0.

## Послерелизные проверки

- `manage.py check`: PASS;
- `makemigrations --check --dry-run`: `No changes detected`;
- migration plan: пуст;
- `collectstatic`: 3 файла скопированы, 279 без изменений;
- `accounting-mvp`, nginx и PostgreSQL: active;
- `accounting-mvp NRestarts=0`;
- `nginx -t`: PASS;
- `https://driverform.ru/`: HTTP 200;
- закрытая `/system-admin/live/` без сессии: штатный 302;
- GET heartbeat endpoint: 405;
- ошибок уровня `err` в журнале приложения после deploy нет.

Авторизованная Browser QA production:

- desktop `1396×700`: 12 карточек приложений, горизонтального overflow нет;
- mobile `390×844`: ширина body/root 390, overflow действий и форм отсутствует;
- три реальные открытые смены видны, опасные формы присутствуют, но не
  отправлялись;
- heartbeat Администратора появился без перезагрузки;
- ошибок и предупреждений консоли нет.

Проверять результат:

`https://driverform.ru/system-admin/live/`
