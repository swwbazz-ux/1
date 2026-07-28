# Отчёт о production deploy трафикового пакета 29.07.2026

## 1. Итоговый статус

**ОПУБЛИКОВАНО / ОСНОВНАЯ ПОСЛЕРЕЛИЗНАЯ ПРОВЕРКА ПРОЙДЕНА.**

В production развёрнут точный Git commit:

`a0476ab74aed1f4e07b96aae69666767593e1392`

Его backend-дерево:

`6a3e37c3b2cb186ac35957349132f78773b96aea`

Основной трафиковый код находится в родительском commit:

`4e8048918ef0ea8831d7163d0cb50518f2afc464`

Production-маркеры `.deploy_commit` и `.deployed-commit` после публикации
содержат полный SHA `a0476ab74aed1f4e07b96aae69666767593e1392`.

Окончательное успешное развёртывание завершено
`29.07.2026 02:10:55` по Владивостоку
(`28.07.2026 16:10:55 UTC`).

## 2. Предрелизная проверка

Перед изменением production подтверждено:

- локальный и удалённый Git commit совпадали;
- ветка:
  `codex/ready-core-pilot-2026-07-27`;
- ahead/behind: `0/0`;
- исходный production-маркер:
  `8c1395a5048c813a1099983a4a5fa82b8a098b3d`;
- `manage.py check` — успешно;
- `makemigrations --check --dry-run` — `No changes detected`;
- `manage.py migrate --plan` — `No planned migration operations`;
- `nginx -t` — успешно;
- `accounting-mvp`, `nginx` и `postgresql` — активны;
- cumulative diff не содержал изменений `.env`, моделей, миграций,
  settings, requirements, nginx, systemd или deploy-конфигурации.

Точный release-архив создан через Git из указанного commit:

- файлов в backend-дереве: `471`;
- tar-записей: `528`;
- небезопасных путей: `0`;
- размер `tar.gz`: `42 191 657` байт;
- SHA-256:
  `1562D220365EF62018A529B4788BF8FD220890F2DD77FD07A2FEA7196B94EC03`.

## 3. Резервные копии

Rollback-комплект размещён на production-сервере:

`/srv/accounting-mvp/backups/deploy-20260728_200139-traffic-before-a0476ab7`

Состав:

| Резервная копия | Размер | SHA-256 |
|---|---:|---|
| `code-before.tar.gz` | `42 040 142` байта | `DD3097162D1CA4E849F5B8D26B97ACC0493CDE41B22BB0B7892F254FB8E32EDE` |
| `staticfiles-before.tar.gz` | `41 841 920` байт | `6B6C9C881925E602D11AF4FF29E3E0F99E16E96A6A3BD76F8DE3AB43BEAAC0F9` |
| `db-before.dump` | `602 656` байт | `2B154EDAE679351A87233B41A69E8167EE508AA8FA20EBDD19B6E9D8052E6861` |

Дополнительно сохранены:

- исходные deployment-маркеры;
- `449` существовавших файлов;
- список `22` новых файлов для адресного удаления при откате;
- SHA-256-манифест всего rollback-комплекта;
- исходный и итоговый migration plan;
- результаты `collectstatic` и серверных проверок.

Архивы проверены чтением, PostgreSQL dump проверен через
`pg_restore --list`, SHA-256-манифест перепроверен полностью.

## 4. Выполненное развёртывание

В коротком сервисном окне выполнено:

1. остановлен только `accounting-mvp`;
2. распакован точный release-архив;
3. все `471/471` файлов побайтово сверены со staging-копией;
4. выполнены `manage.py check`,
   `makemigrations --check --dry-run` и `migrate --plan`;
5. миграции не выполнялись, потому что план был пуст;
6. выполнен `collectstatic --noinput`;
7. запущен `accounting-mvp`;
8. проверены nginx, PostgreSQL, Django и внешний HTTPS.

`collectstatic` сообщил:

- `123` файла скопировано;
- `136` файлов не изменено.

После сборки все `123/123` относящихся к release static-файла
побайтово совпали с исходниками точного Git-архива.

Проверенные SHA-256 двух общих release-ресурсов на Linux:

- `static/js/realtime-client.js`:
  `58C45D382F71FB3D6470F1CDC6273D303801FBF5040005B2926B5A9167B279E1`;
- `static/css/app.css`:
  `A3E6A2CAD7EE13BD44F4D7728179D07935E268B1649F755D93BC6273EE2AAD6D`.

Отличие этих хэшей от хэшей файлов рабочей Windows-копии объясняется
только `LF/CRLF`: production-файлы совпадают именно с Git-архивом commit.

## 5. Проверка механизма отката

Перед окончательным успешным запуском защита дважды выполнила полный
автоматический откат:

1. первая попытка остановилась до распаковки из-за слишком строгой
   проверки штатного состояния `inactive`;
2. вторая попытка прошла до запуска, но немедленный HTTP-запрос попал в
   короткое окно до появления Gunicorn socket и получил `502`.

В обоих случаях автоматически восстановлены:

- исходный код;
- полный каталог `staticfiles`;
- оба deployment-маркера;
- активная служба `accounting-mvp`.

После каждого отката подтверждены исходный commit
`8c1395a5048c813a1099983a4a5fa82b8a098b3d`,
`manage.py check` и доступность HTTPS.

В окончательную попытку добавлено ожидание готовности Gunicorn. Корневой
HTTPS ответил успешно на второй проверке. После готовности новых ответов
`5xx` нет.

## 6. Серверная послерелизная проверка

Подтверждено:

- `accounting-mvp` — `active`;
- `nginx` — `active`;
- `postgresql` — `active`;
- `nginx -t` — успешно;
- `manage.py check` — без ошибок;
- migration plan — пуст;
- live payload `471/471` совпадает со staging;
- собранная статика `123/123` совпадает с исходниками;
- ошибок, traceback, critical или exception в журнале приложения после
  окончательного запуска — `0`;
- ответов `5xx` в nginx access log после готовности — `0`.

`.env` не входил в release payload и не изменялся; его production mtime
остался `18.07.2026 03:31:43 +03:00`.

Рабочие данные, `private_media` и миграции не изменялись. PostgreSQL dump
создан только как read-only резервная копия. Команда `migrate` не
выполнялась.

## 7. Внешняя HTTP/PWA-проверка

Основной адрес:

`https://driverform.ru/`

Результаты:

- корень — `200`, TLS проверен;
- все `11/11` manifest — `200`;
- все `11/11` service worker — `200`;
- contract у всех — `pwa-contract-v1`;
- все защищённые start URL без авторизации — штатный `302`;
- `/realtime/state/?include_events=0` без сессии — штатный `401`;
- `/api/achievements/current/` без сессии — штатный `403`;
- общий JavaScript и CSS — `200`, gzip включён;
- versioned static получает `Cache-Control: max-age=2592000`;
- manifest и service worker получают `Cache-Control: no-cache`.

Подтверждённые shell-версии:

| Роль | Версия |
|---|---|
| Водитель | `driver-mobile-shell-v114` |
| Машинист экскаватора | `excavator-mobile-shell-v127` |
| Горный мастер | `mining-master-mobile-shell-v120` |
| Заместитель начальника участка | `deputy-mining-manager-desktop-shell-v14` |
| Диспетчер | `dispatcher-desktop-shell-v41` |
| ОУП | `oup-shell-v21` |
| Табельщик | `timekeeper-shell-v8` |
| Начальник участка | `site-manager-shell-v8` |
| Механик | `mechanic-shell-v6` |
| Руководство | `management-shell-v5` |
| Системный администратор | `system-admin-shell-v19` |

Browser QA экрана единого входа:

- desktop `1280×720` — overflow `0`, основные элементы видимы,
  console errors/warnings `0`;
- mobile `390×844` — overflow `0`, элементы не перекрываются,
  console errors/warnings `0`;
- переход к `/driver/` без сессии штатно вернул экран единого входа.

Авторизованные действия в рабочей production-базе не выполнялись, чтобы
не создавать и не менять производственные записи. Их регрессии ранее
пройдены на тестовом PostgreSQL-профиле: полный набор `756/756`.

## 8. Обнаруженный отдельный инфраструктурный остаток

Через основной домен `driverform.ru` все 11 PWA доступны и отдают
правильные версии.

Отдельные адреса:

- `timekeeper.driverform.ru`;
- `site-manager.driverform.ru`

пока не готовы для выдачи пользователям:

- DNS уже указывает на production;
- имена отсутствуют в SAN действующего TLS-сертификата;
- имена отсутствуют в `nginx server_name`;
- Django не разрешает эти host names;
- поэтому HTTPS не проходит проверку имени, а запрос без проверки TLS
  получает `400`.

Этот остаток не вызван текущим deploy: release не менял сертификат,
nginx, `.env`, Django settings или инфраструктуру.

До отдельного разрешения эти два поддомена изменять нельзя. Безопасное
устранение требует одного rollback-safe инфраструктурного пакета:

1. добавить оба имени в TLS-сертификат;
2. добавить их в `nginx server_name`;
3. добавить их в `ALLOWED_HOSTS` и `CSRF_TRUSTED_ORIGINS`;
4. выполнить `nginx -t`;
5. повторить внешнюю HTTPS/PWA-проверку.

До этого Табельщик и Начальник участка должны использовать маршруты
основного домена `driverform.ru`, а не отдельные поддомены.

Также сохранён ранее отложенный инфраструктурный остаток
`QA-TRAFFIC-P2-004`: внешний протокол пока HTTP/1.1, изменение HTTP/2 в
этот deploy не входило.

## 9. План отката

При необходимости отката:

1. остановить `accounting-mvp`;
2. адресно удалить `22` файла из `payload.absent`;
3. распаковать `code-before.tar.gz` в `/srv/accounting-mvp`;
4. полностью восстановить `staticfiles-before.tar.gz`;
5. вернуть `.deploy_commit` и `.deployed-commit` на
   `8c1395a5048c813a1099983a4a5fa82b8a098b3d`;
6. запустить `accounting-mvp`;
7. выполнить `manage.py check`, `nginx -t` и внешний HTTPS smoke.

`db-before.dump` хранится как страховочная копия, но автоматически
восстанавливать его не требуется: миграции и изменения данных во время
deploy не выполнялись.

## 10. Где проверять

Основная ручная проверка:

`https://driverform.ru/`

После входа можно проверить рабочие PWA по штатным ролям. Для
Табельщика и Начальника участка пока использовать только маршруты
основного домена.

Изменение опубликовано в production. Сам commit уже находился в
`origin/codex/ready-core-pilot-2026-07-27`; дополнительный commit или
push для кода deploy не требовался.
