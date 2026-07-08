# Очистка старых CSS-версий Экскаваторщика

Дата: 08.07.2026

## Суть задачи

На production-сервере `driverform.ru` выполнена ограниченная cleanup-задача по старым CSS-файлам PWA Машиниста экскаватора.

Задача касалась только статических CSS-файлов в текущем серверном контуре Django MVP:

- `/srv/accounting-mvp/static/css/`
- `/srv/accounting-mvp/staticfiles/css/`

Код Python, шаблоны, views, модели, URL, база данных, `.env`, deploy-процесс и сервисы production не менялись.

## Удаленные файлы

Из обоих каталогов удалены старые неиспользуемые промежуточные версии:

- `excavator-work-v35.css` ... `excavator-work-v53.css`
- `excavator-work-v46-final.css` ... `excavator-work-v53-final.css`

Итого удалено:

- 54 файла;
- 3.1 MB.

Перед удалением была выполнена проверка подключений в:

- `templates`
- `users`
- `trips`
- `assignments`
- `reports`
- `static`
- `staticfiles`

Ссылок на удаляемые CSS-файлы не найдено.

## Rollback-архив

Перед удалением создан rollback-архив только удаляемых файлов:

`/home/deploy/static-css-cleanup-backup-20260708-070351.tar.gz`

В архиве сохранены относительные пути:

- `static/css/...`
- `staticfiles/css/...`

## Оставленные файлы

Оставлены актуальные и резервные CSS-файлы:

- `app.css`
- `excavator-work-v54.css`
- `excavator-work-v54-final.css`
- `excavator-work-v55.css`
- `excavator-work-v55-final.css`
- `excavator-work-v55-shift.css`

Версия `v55` является актуальной production-версией PWA Экскаваторщика.

Версия `v54` оставлена как временный резерв. Ее можно удалить отдельной cleanup-задачей после периода стабильности `v55`.

## Проверки после очистки

После удаления подтверждено:

- `https://driverform.ru/excavator-sw.js` содержит `excavator-mobile-shell-v55`;
- `https://driverform.ru/static/css/excavator-work-v55.css` отдает HTTP 200;
- `https://driverform.ru/static/css/excavator-work-v55-final.css` отдает HTTP 200;
- `https://driverform.ru/static/css/excavator-work-v55-shift.css` отдает HTTP 200;
- `https://driverform.ru/excavator/work/` не отдает 500/502;
- старый `https://driverform.ru/static/css/excavator-work-v53.css` теперь отдает 404.

## Что не выполнялось

Во время cleanup-задачи не выполнялись:

- deploy;
- `collectstatic`;
- `migrate`;
- restart `nginx`;
- restart `gunicorn`;
- restart `systemd`;
- `git pull`;
- изменения `.env`;
- изменения базы данных.

## Правило на будущее

Очистку production static-файлов выполнять только отдельной задачей после аудита фактических подключений и с rollback-архивом удаляемых файлов.
