# Карта проекта Copper Resources

Документ фиксирует фактическую карту проекта после инвентаризации. Он нужен, чтобы Codex перед маленькими задачами находил конкретный рабочий контур и не анализировал весь проект без необходимости.

## Корень и основные зоны

- Git root: `C:\Users\swwba\Desktop\Проект учетная система\ПОЕКТ`.
- Основной код MVP: `СИСТЕМА_MVP/backend`.
- Документация и память проекта: `ПРОГРЕСС_ПРОЕКТА`.
- Резервные копии: `РЕЗЕРВНЫЕ_КОПИИ`, `backups`.
- Выходные артефакты и отчеты: `outputs`.
- Медиа и загружаемые файлы Django: `СИСТЕМА_MVP/backend/media`.
- Иконки техники: `Иконки техники`.
- Django backend: `СИСТЕМА_MVP/backend`.

## Django MVP

Backend является основным рабочим приложением. Frontend/PWA-контуры реализованы внутри Django через `templates`, `static`, `views`, service worker и manifest.

Основные Django-приложения:

- `core` — общие сущности и базовые правила проекта.
- `users` — пользователи, роли и рабочие экраны пользователей.
- `references` — справочники техники, материалов, участков и связанных производственных сущностей.
- `shifts` — смены и сменный контекст.
- `assignments` — назначения, задания и контур Горного мастера.
- `trips` — рейсы, диспетчерский контур, контур экскаватора и связанные действия.
- `downtimes` — простои, причины простоев и связанные события.
- `reports` — отчеты и управленческие представления.

## Рабочие контуры

### Водитель

- Template: `СИСТЕМА_MVP/backend/templates/users/driver_shift.html`.
- View: `СИСТЕМА_MVP/backend/users/views.py`.
- Маршруты: `/driver/`, `/driver/shift/`.
- Manifest: `/driver.webmanifest`.
- Service worker: `/driver-sw.js`.
- Shell: `driver-mobile-shell-v32`.

### Машинист экскаватора

- Template: `СИСТЕМА_MVP/backend/templates/trips/excavator_work.html`.
- View: `СИСТЕМА_MVP/backend/trips/views.py`.
- Маршрут: `/excavator/work/`.
- Manifest: `/excavator.webmanifest`.
- Service worker: `/excavator-sw.js`.
- Shell: `excavator-mobile-shell-v55`.

### Горный мастер

- View: `СИСТЕМА_MVP/backend/assignments/views.py`.
- Маршрут: `/mining-master/assignments/`.
- Service worker: `/mining-master-sw.js`.
- Shell: `mining-master-mobile-shell-v97`.
- Шаблон: фактически экран Горного мастера проходит через `assignments/views.py`, который использует диспетчерский контур рендера из `trips.views`. Общий рабочий template/shell сейчас связан с `СИСТЕМА_MVP/backend/templates/trips/dispatcher_control.html`; перед UI-правкой нужно подтвердить конкретный блок шаблона по коду.

### Диспетчер

- Template: `СИСТЕМА_MVP/backend/templates/trips/dispatcher_control.html`.
- View: `СИСТЕМА_MVP/backend/trips/views.py`.
- Маршрут: `/dispatcher/control/`.
- Service worker: `/dispatcher-sw.js`.
- Shell: `dispatcher-desktop-shell-v22`.

## Правило локализации задачи

Перед изменением нужно записать для себя:

- тип задачи;
- рабочий контур;
- маршрут или экран;
- конкретные `template`, `static`, `view`, тесты и service worker, если они участвуют;
- зоны, которые запрещено трогать.

Если задача является мелкой UI-правкой, рабочая область ограничивается конкретным экраном и связанными файлами. Модели, миграции, база данных, production, deploy и соседние контуры не входят в такую задачу.
