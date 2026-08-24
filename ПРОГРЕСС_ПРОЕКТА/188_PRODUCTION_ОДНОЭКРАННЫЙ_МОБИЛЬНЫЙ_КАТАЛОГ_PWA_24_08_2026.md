# Production: одноэкранный мобильный каталог PWA — 24.08.2026

## Результат

- восемь приложений размещены сеткой 2×4 в пределах одного мобильного экрана;
- второстепенные подписи на карточках скрыты, иконки и названия сохранены;
- каталог и фон всплывающего окна не прокручиваются;
- окно фиксировано поверх страницы и полностью вмещает QR, production-ссылку,
  отправку, копирование и отдельное открытие приложения;
- серверный fallback отображается тем же фиксированным окном, а не длинной
  страницей;
- CSS/JS query повышен до `v6`.

Кодовый commit: `ed03535` (`fix(app-catalog): fit mobile catalog and modal in viewport`).

## Проверки

- Django `9/9`, JavaScript `6/6`, `manage.py check`, `git diff --check`: PASS;
- production 390×844: page 390×844, последний ряд в пределах 834 px;
- production 320×720: page 320×720, последняя карточка заканчивается на 713 px;
- popup 390×844 и 320×720: `scrollHeight == clientHeight`, overflow отсутствует;
- production desktop 1280×720: восемь карточек и popup без прокрутки;
- console errors/warnings `0`, HTTP/2 `200`, app/nginx/PostgreSQL active,
  `NRestarts=0`, ошибок журнала нет;
- рабочая SQLite неизменна:
  `404EE7D40A09EAEA6E5A87BFD39D6D99A0CEC3AA73812830249110731684DC22`.

Миграции и рабочие данные не изменялись.

## Откат

`/srv/accounting-mvp/backups/deploy-20260824T010104Z-apps-mobile-one-screen-before-ed03535`
