# Production deploy обновлённого экрана входа — 10.09.2026

## Результат

Для мобильных приложений Водителя самосвала, Машиниста экскаватора и
Горного мастера опубликован единый обновлённый экран входа в визуальном языке
стартовой страницы Copper Resources.

Изменены только два runtime-файла:

- `СИСТЕМА_MVP/backend/templates/users/login.html`;
- `СИСТЕМА_MVP/backend/static/css/mobile-role-login-start-v1.css`.

Формы, серверная авторизация, модели, база данных, миграции, `.env`, service
worker и остальные ролевые интерфейсы не изменялись.

## Git и фактический production baseline

- ветка: `codex/login-restyle-2026-09-10`;
- runtime commit: `75cb5d79d62bb41e8bd9861444a8634f20734b5e`;
- parent: `d581931aa7c5ae912c10f73ca911495aaafbac19`;
- перед заменой production-шаблон `login.html` после нормализации окончаний
  строк точно совпал с parent;
- production является составным серверным контуром, поэтому выполнен только
  точечный overlay двух файлов, а общий `DEPLOYED_COMMIT`
  `bb784a73e414c484ce48978d7b93970d94ec0595` не изменялся.

## Проверки

- локальный `manage.py check` — успешно;
- профильные тесты входа и PWA — `64/64 OK`;
- серверный `manage.py check` — успешно;
- `collectstatic --noinput` — успешно;
- SHA-256 source и collected CSS совпадает:
  `2357f6501695771d2f619e8e8e7a3d955fd3c5aa8698a1e8299c70c5e4576926`;
- `nginx -t` — успешно;
- `accounting-mvp` — `active/running`, `ExecMainStatus=0`, `NRestarts=0`;
- после рестарта критических записей в журнале сервиса нет;
- стартовая страница и входы Водителя, Машиниста экскаватора и Горного
  мастера отвечают HTTP 200 и отдают новый CSS и hero-блок;
- на физическом Android `22011119UY` production-экран проверен при viewport
  `392×732`: CSS загружен, горизонтального переполнения нет;
- при открытой экранной клавиатуре поле телефона и кнопка остаются целиком
  видимыми, страница не уходит в горизонтальный или ручной вертикальный
  скроллинг;
- desktop-представление `1280×800` проверено визуально.

## Откат

До замены создан rollback-архив:

`/srv/accounting-mvp/backups/code/login-restyle-20260910-060738-before-75cb5d79.tar.gz`

Архив содержит прежний `templates/users/login.html` и исходный
`DEPLOYED_COMMIT`. Новый CSS до публикации отсутствовал, поэтому при откате
его нужно удалить отдельно из `static/css` и `staticfiles/css`, затем повторно
выполнить `collectstatic` и перезапустить `accounting-mvp`.

## Где проверять

- `https://driver.driverform.ru/?form=1`;
- `https://excavator.driverform.ru/?form=1`;
- `https://mining-master.driverform.ru/?form=1`.
