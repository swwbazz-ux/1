# Production deploy разделения флагов рейтинга 31.07.2026

Дата: 31.07.2026.

Статус: commit, push и точечный production deploy выполнены успешно;
оба интерфейсных флага оставлены выключенными.

## Git checkpoint

- ветка: `codex/rating-interface-flags-2026-07-31`;
- commit реализации:
  `fb31b391d7ca7ab5b18b3629098dad19436862db`;
- push: выполнен в `origin`;
- локальные проверки перед commit: `83/83 PASS`, `manage.py check`,
  отсутствие новых миграций и чистый `git diff --check`.

## Граница production-пакета

Фактический production-файл `reports/views.py` соответствовал commit
`665f04b`, тогда как локальный родитель содержал ещё не опубликованный
ускоренный QA-live контур. Поэтому полный локальный файл не копировался.
Поверх production перенесены только смысловые изменения текущей задачи:

- общий resolver авторизации и области участка;
- независимый TV-флаг для TV, data и фотографии;
- независимый портальный флаг перед внутренним resolver снимка.

Файл `reports/rating_tv_live_qa.py` в production не добавлялся: его
добавление означало бы частичную публикацию отдельного, не входящего в
задачу QA-live пакета.

Опубликованы ровно семь runtime-файлов:

1. `portal/context_processors.py`;
2. `portal/services.py`;
3. `portal/views.py`;
4. `reports/views.py`;
5. `static/portal/css/portal-shell-v5.css`;
6. `templates/portal/base_internal.html`;
7. `templates/portal/dashboard.html`.

Тесты и проектные документы на сервер не копировались.

## Rollback

Перед заменой создан архив:

`/srv/accounting-mvp/backups/code/rating-interface-flags-before-20260731T135128Z-fb31b39.tar.gz`

SHA-256:

`8552d1cce6bca0c85c57afb20f2e91bf151908f442b03f6345cf8073f0fce07b`

Архив имеет режим `600`, проверен через `sha256sum -c` и содержит семь
исходных runtime-файлов плюс прежний собранный
`staticfiles/portal/css/portal-shell-v5.css`.

## Серверные проверки

- `manage.py check` — PASS;
- `collectstatic --noinput` — скопирован один изменённый CSS-файл;
- `accounting-mvp.service` — `active` после перезапуска;
- `nginx -t` — PASS;
- `RATING_TV_SCREEN_ENABLED=False`;
- `PORTAL_WORKING_DRIVER_RATING_ENABLED=False`;
- service journal после deploy: ошибок уровня `err` нет;
- nginx journal после deploy: ошибок уровня `err` нет.

## Проверка поведения

- публичный `/reports/rating/tv/data/` — `404`, заголовки содержат
  `private, no-store` и `nosniff`;
- `/reports/rating/tv/` после пройденной авторизации — `404`;
- `/portal/rating/` после пройденной портальной авторизации — `404`;
- для гостя оба HTML-маршрута сохраняют прежний порядок безопасности и
  сначала перенаправляют на вход (`302`), что не ослаблялось задачей;
- desktop/mobile-ссылки рейтинга, «Моё место» и «Пятёрка лидеров» при
  выключенном портальном флаге не рендерятся;
- настроенный production provider не вызывается для рейтинга и личных
  KPI при выключенном портальном флаге;
- независимый `shift_results` продолжает вызываться и его блок остаётся
  в шаблоне;
- ошибок консоли на проверенных production-страницах входа и переходах
  отключённых интерфейсов нет.

Авторизованные `404`, скрытие элементов, запрет обхода provider и
независимость итогов смены дополнительно подтверждены read-only
проверкой production-кода через `RequestFactory`, template render и
mock provider. Она не создавала сессий и не изменяла базу.

## Не затронуто

- база данных и её содержимое;
- миграции и команды migrate;
- `.env`;
- формула рейтинга и replay;
- утверждённая четырёхколоночная TV-компоновка;
- флаги: оба остались выключенными.

Следующий отдельный шаг после решения владельца — включить только
`RATING_TV_SCREEN_ENABLED` и повторить эксплуатационную TV-проверку.
Портальный флаг пока не включать.
