# Production-fix контракта обновления Диспетчера — 06.09.2026

## Причина

После публикации оболочки `dispatcher-desktop-shell-v58` каталог ролевых
приложений продолжал объявлять `dispatcher-desktop-shell-v57`. Публичный
`/dispatcher-sw.js` содержал код `v58`, но заголовок `X-App-Shell-Version`
сообщал `v57`. Защитный PWA-контракт исчерпывал повторные попытки обновления,
показывал «Обновление не удалось» и блокировал изменяющие действия.

## Исправление и публикация

В `users/role_apps.py` версия Диспетчера синхронизирована с `v58`.
Runtime-коммит `7b9c594027dcd77c5d1bc35d175399018c802349` отправлен в ветку
`codex/fix-dispatcher-shared-reauth-2026-09-06` и опубликован в production.

Rollback-архив:

- `/srv/accounting-mvp/backups/code/deploy-20260906T114736Z-dispatcher-contract-v58-before.tar.gz`;
- SHA-256: `36896a56726edcb5d294667e6d2773a88a2fc69c842481a993f8430cd5a126a2`.

## Проверка

- `manage.py check`: без ошибок;
- `accounting-mvp.service`: `active`;
- публичный `/dispatcher-sw.js`: HTTP `200`;
- тело service worker: `dispatcher-desktop-shell-v58`;
- заголовок `X-App-Shell-Version`: `dispatcher-desktop-shell-v58`;
- реальный авторизованный пульт открыт в Chrome: плашка ошибки отсутствует,
  восстановленная расстановка отображается.

База данных, миграции и `.env` не изменялись.
