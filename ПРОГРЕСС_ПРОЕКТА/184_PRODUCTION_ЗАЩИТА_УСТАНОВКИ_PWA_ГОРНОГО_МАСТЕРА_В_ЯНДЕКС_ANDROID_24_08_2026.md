# Production-защита установки PWA Горного мастера в Яндекс Браузере Android

Дата: 24.08.2026 по Владивостоку.

## Причина

На одном Android-телефоне ссылка Горного мастера была добавлена через
мобильный Яндекс Браузер как обычный ярлык страницы. Нижняя строка поиска
Яндекс Браузера является признаком вкладки браузера, а не standalone PWA.

Само production-приложение исправно: manifest и service worker отдавались с
HTTP 200, `display=standalone`, корректным scope и shell v120. Ошибка была не в
серверном PWA-контракте, а в способе установки на конкретном телефоне.

## Изменения

- Android + Яндекс Браузер распознаются отдельно от обычного Android Chrome.
- На странице входа вместо неоднозначной установки показывается заметная
  инструкция: удалить обычный ярлык, открыть адрес в Google Chrome и выбрать
  «Установить приложение».
- В рабочем экране Горного мастера, если он открыт из Яндекс Браузера не в
  standalone-режиме, показывается предупреждение «Открыто не как приложение».
- Предупреждение позволяет скопировать точный origin приложения; сотрудник
  может временно закрыть его и продолжить работу в браузере.
- В корректно установленном standalone PWA предупреждение не появляется.
- Mining Master shell повышен с v120 до v121; installer включён в его
  service-worker cache.
- Подсказка на странице входа остаётся видимой даже при открытой мобильной
  клавиатуре.

## Проверки

- JavaScript: `5/5 PASS`.
- Django: `52/52 PASS`.
- `manage.py check`: PASS.
- `makemigrations --check --dry-run`: `No changes detected`.
- `git diff --check`: PASS.
- Локальный Browser QA: Android Yandex 390×844, обычный Chrome 1280×720,
  overflow 0, console errors/warnings 0.
- Production Browser QA: Android Yandex 390×844, инструкция видима, overflow
  0, console errors/warnings 0.
- Production manifest, service worker, installer JS и CSS: HTTP 200; shell
  `mining-master-mobile-shell-v121`; `display=standalone` сохранён.

## Git и production

- Кодовый commit:
  `499ee21ebd43818d6f61d2ab817b29e4f58aa4d1`.
- Ветка: `codex/pwa-app-catalog-2026-08-23`; push выполнен, local/remote SHA
  совпали.
- Production обновлён адресно. Пять совпадающих файлов заменены из commit;
  в общем `users/role_apps.py` изменена только версия Горного мастера
  v120→v121, поэтому более новый Excavator shell v129 сохранён.
- Миграции и база данных не изменялись. `.env`, nginx-конфигурация и другие
  PWA не менялись. Выполнен `collectstatic`, скопированы два файла.
- `accounting-mvp`, nginx и PostgreSQL active; `NRestarts=0`; `nginx -t` PASS.

## Откат

Rollback-комплект:

`/srv/accounting-mvp/backups/deploy-20260823T213208Z-pwa-yandex-android-before-499ee21e`

SHA-256 архива:

`f098d1a8c0e7cd43dc8033b4f896fcf1fd43d48bfbb77870ceb66fe7f0cf5369`

В комплекте находятся исходные и собранные static-файлы, контрольная сумма и
точная инструкция `ROLLBACK.txt`.

## Инструкция сотруднику

1. Удалить с главного экрана старый ярлык, который открывается с нижней строкой
   Яндекс Браузера.
2. Открыть Google Chrome.
3. Перейти на `https://mining-master.driverform.ru/`.
4. Нажать «Установить приложение».
5. Запускать новую иконку с главного экрана; строки браузера быть не должно.
