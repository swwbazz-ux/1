# Production: серверный мобильный fallback каталога PWA — 24.08.2026

## Причина

После превращения карточек в кнопки один реальный мобильный браузер не запускал
актуальный обработчик: прямой переход был безопасно заблокирован, но окно тоже
не открывалось.

## Решение

- карточка снова имеет ссылку, но она ведёт не в PWA, а на безопасный внутренний
  адрес `/apps/?app=<роль>#connect`;
- актуальный JavaScript перехватывает нажатие и открывает окно без навигации;
- старый/отключённый JavaScript переходит на внутренний адрес, где сервер уже
  отрисовывает открытое окно, QR и production-ссылку;
- каталог получил `Cache-Control: no-cache`, CSS/JS query повышен до `v5`;
- неизвестная роль не открывает окно и не выходит за утверждённый allowlist.

Кодовый commit: `3f34b77` (`fix(app-catalog): add server-rendered mobile fallback`).

## Проверки

- Django `9/9 PASS`, JavaScript `6/6 PASS`;
- `manage.py check`, контроль миграций и `git diff --check`: PASS;
- локально и в production на 390×844 проверены оба пути: перехват на месте и
  прямой серверный fallback;
- production: HTTP/2 `200`, `Cache-Control: no-cache`, overflow `0`, console
  errors/warnings `0`;
- app/nginx/PostgreSQL active, `NRestarts=0`, ошибок журнала нет;
- рабочая SQLite до/после неизменна:
  `404EE7D40A09EAEA6E5A87BFD39D6D99A0CEC3AA73812830249110731684DC22`.

Миграции и рабочие данные не изменялись.

## Откат

`/srv/accounting-mvp/backups/deploy-20260824T001543Z-apps-mobile-fallback-before-3f34b77`
