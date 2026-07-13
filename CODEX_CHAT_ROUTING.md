# Маршрутизация рабочих чатов Codex

Документ фиксирует, какие специализированные чаты Codex используются для частей проекта Copper Resources, какие документы они должны читать перед работой и какие зоны им запрещено трогать.

Проект ведется в одной рабочей папке. Разные чаты не создают отдельные Git-деревья и не изолируют изменения друг от друга.

## Общие правила для всех новых чатов

- Перед каждой задачей сначала показать `git status`.
- Если рабочее дерево грязное, остановиться и спросить пользователя, что делать.
- Commit, push и deploy выполнять только по отдельному разрешению пользователя.
- Разные чаты не изолируют Git-дерево, рабочая папка одна.
- Нельзя параллельно вести две правки в разных чатах, пока первая не закоммичена или не откатана.
- Для мелких UI-правок типа A не читать весь `ПРОГРЕСС_ПРОЕКТА`.
- Для мелких UI-правок использовать `FAST_UI_MODE.md`.
- Для server/deploy-задач использовать `DEPLOYMENT_WORKFLOW.md`.
- Production, БД, `.env`, deploy, migrate и collectstatic не трогать без прямого разрешения.
- Перед изменениями фиксировать рабочий контур, маршрут/экран, разрешенные файлы и запрещенные зоны.

## 1. ШТАБ — Copper Resources

### Назначение

- Управление проектом.
- Структура.
- Правила Codex.
- Git-порядок.
- Workflow.
- Оптимизация скорости.
- Общая координация.

### Документы читать

- `AGENTS.md`
- `PROJECT_MAP.md`
- `CODEX_WORKFLOW.md`
- `FAST_UI_MODE.md`
- `DEPLOYMENT_WORKFLOW.md`, если речь о сервере или публикации.

### Запрещено

- Делать обычные UI-правки.
- Трогать production, БД, `.env`, deploy, migrate, collectstatic без прямого разрешения.
- Смешивать задачи контуров.

## 2. Водитель — PWA

### Назначение

- Контур водителя.
- Маршруты `/driver/` и `/driver/shift/`.
- Основной template: `СИСТЕМА_MVP/backend/templates/users/driver_shift.html`.
- View: `СИСТЕМА_MVP/backend/users/views.py` только при необходимости.

### Документы читать

- `AGENTS.md`
- `PROJECT_MAP.md`
- `CODEX_WORKFLOW.md`
- `FAST_UI_MODE.md`
- `СИСТЕМА_MVP/backend/AGENTS.md`

### Правила

- Мелкие UI-правки - тип A.
- Сначала template/static.
- View трогать только если без этого невозможно.
- Не трогать экскаваторщика, диспетчера, горного мастера, отчеты, models, migrations, БД, `.env`, production, deploy.

## 3. Экскаваторщик — PWA

### Назначение

- Контур машиниста экскаватора.
- Маршрут `/excavator/work/`.
- Template: `СИСТЕМА_MVP/backend/templates/trips/excavator_work.html`.
- CSS: `СИСТЕМА_MVP/backend/static/css/excavator-work-v55*.css`.
- View: `СИСТЕМА_MVP/backend/trips/views.py` только при необходимости.

### Документы читать

- `AGENTS.md`
- `PROJECT_MAP.md`
- `CODEX_WORKFLOW.md`
- `FAST_UI_MODE.md`
- `СИСТЕМА_MVP/backend/AGENTS.md`

### Правила

- Мелкие UI-правки - тип A.
- PWA/service worker/realtime-задачи - тип D.
- Не трогать водителя, диспетчера, горного мастера, reports, models, migrations, БД, `.env`, production, deploy.

## 4. Горный мастер — PWA

### Назначение

- Контур горного мастера.
- Маршрут `/mining-master/assignments/`.
- View: `СИСТЕМА_MVP/backend/assignments/views.py`.
- Перед UI-правкой обязательно определить фактический template и конкретный блок, потому что контур может использовать общий dispatcher-render.

### Документы читать

- `AGENTS.md`
- `PROJECT_MAP.md`
- `CODEX_WORKFLOW.md`
- `FAST_UI_MODE.md`
- `СИСТЕМА_MVP/backend/AGENTS.md`

### Правила

- Мелкие UI-правки - тип A.
- Не менять общий диспетчерский template шире нужного блока.
- Не трогать водителя, экскаваторщика, диспетчера, reports, models, migrations, БД, `.env`, production, deploy.

## 5. Диспетчер — PWA

### Назначение

- Диспетчерский контур.
- Маршрут `/dispatcher/control/`.
- Template: `СИСТЕМА_MVP/backend/templates/trips/dispatcher_control.html`.
- View: `СИСТЕМА_MVP/backend/trips/views.py` только при необходимости.

### Документы читать

- `AGENTS.md`
- `PROJECT_MAP.md`
- `CODEX_WORKFLOW.md`
- `FAST_UI_MODE.md`
- `СИСТЕМА_MVP/backend/AGENTS.md`

### Правила

- Мелкие UI-правки - тип A.
- Логика диспетчерских действий - тип B или C.
- Не трогать водителя, экскаваторщика, горного мастера, reports, models, migrations, БД, `.env`, production, deploy.

## 6. Сервер и публикация

### Назначение

- GitHub.
- Push.
- Deploy.
- `driverform.ru`.
- Production.
- Collectstatic.
- Systemd.
- Gunicorn.
- Nginx.
- Проверка URL.
- Rollback.

### Документы читать

- `AGENTS.md`
- `DEPLOYMENT_WORKFLOW.md`
- `CODEX_WORKFLOW.md`
- `PROJECT_MAP.md`

### Правила

- Не смешивать серверные задачи с UI-правками.
- Production не менять без прямого разрешения.
- Deploy, migrate, collectstatic, restart systemd/gunicorn/nginx запускать только после отдельного плана и подтверждения.
- Для deploy сначала описывать: что выкладываем, backup/rollback, нужен ли migrate, нужен ли collectstatic, нужен ли restart, какие URL проверяем.

## 7. Realtime и обмен данными

### Назначение

- `/realtime/state/`.
- Polling.
- Realtime-состояние.
- Обмен данных через backend.
- Связь рабочих контуров через сервер и БД.

### Документы читать

- `AGENTS.md`
- `PROJECT_MAP.md`
- `CODEX_WORKFLOW.md`
- `СИСТЕМА_MVP/backend/AGENTS.md`

### Правила

- Не делать UI-редизайн в этом чате.
- Не менять production, `.env`, deploy, migrate, collectstatic без прямого разрешения.
- Models, migrations и БД не менять без отдельного плана.
- Перед изменениями описывать: какой контракт обмена затронут, какие views участвуют, какие контуры зависят от изменения, какие тесты нужны.

## 8. ОУП — сотрудники

### Назначение

- Контур Отдела управления персоналом.
- Маршруты `/oup/`, `/oup/employees/`, `/oup/dismissed/`, `/oup/log/`.
- Создание, редактирование, фото и увольнение сотрудников.
- Собственная дневная вахтовая смена без ночного варианта.

### Документы читать

- `AGENTS.md`
- `PROJECT_MAP.md`
- `CODEX_WORKFLOW.md`
- `СИСТЕМА_MVP/backend/AGENTS.md`
- `ПРОГРЕСС_ПРОЕКТА/67_РАБОЧЕЕ_МЕСТО_ОУП.md`

### Правила

- ОУП управляет фактом появления и увольнения сотрудника в оперативной системе.
- Не дублировать кадровые приказы, паспорт, ИНН, СНИЛС и официальный документооборот 1С.
- Не трогать заселение, табель, больничные, отпуска, охрану труда, PIN и назначение техники без отдельной задачи.
- Сохранять общую сущность `Employee`; второй справочник сотрудников не создавать.
- Изменения сотрудников разрешены только в открытой дневной смене ОУП.
- Commit, push и deploy выполнять только по отдельному разрешению.

## Как восстановить контекст в длинном чате

Пользователь может написать:

```text
Перезагрузи профиль чата из CODEX_CHAT_ROUTING.md: [название профиля]
```

После этого Codex должен открыть `CODEX_CHAT_ROUTING.md`, найти соответствующий профиль и использовать его как рабочую рамку текущего чата.

## Короткие стартовые сообщения для новых чатов

### ШТАБ — Copper Resources

```text
Название чата: ШТАБ — Copper Resources

Профиль: управление проектом, структура, правила Codex, Git-порядок, workflow, оптимизация скорости, координация.

Работай по документам:
AGENTS.md
PROJECT_MAP.md
CODEX_WORKFLOW.md
FAST_UI_MODE.md
DEPLOYMENT_WORKFLOW.md — только если речь о публикации или сервере.

В этом чате не делать обычные UI-правки.
Production, БД, .env, deploy, migrate, collectstatic не трогать без прямого разрешения.

Перед задачей сначала git status.
Если рабочее дерево грязное — остановиться.
Commit/push/deploy только по отдельному разрешению.
```

### Водитель — PWA

```text
Название чата: Водитель — PWA

Профиль: контур водителя, маршруты /driver/ и /driver/shift/.

Работай по документам:
AGENTS.md
PROJECT_MAP.md
CODEX_WORKFLOW.md
FAST_UI_MODE.md
СИСТЕМА_MVP/backend/AGENTS.md

Основной template: СИСТЕМА_MVP/backend/templates/users/driver_shift.html.
View трогать только при необходимости: СИСТЕМА_MVP/backend/users/views.py.

Не трогать экскаваторщика, диспетчера, горного мастера, отчеты, models, migrations, БД, .env, production, deploy.

Перед задачей сначала git status.
Если рабочее дерево грязное — остановиться.
Commit/push/deploy только по отдельному разрешению.
```

### Экскаваторщик — PWA

```text
Название чата: Экскаваторщик — PWA

Профиль: контур машиниста экскаватора, маршрут /excavator/work/.

Работай по документам:
AGENTS.md
PROJECT_MAP.md
CODEX_WORKFLOW.md
FAST_UI_MODE.md
СИСТЕМА_MVP/backend/AGENTS.md

Основной template: СИСТЕМА_MVP/backend/templates/trips/excavator_work.html.
CSS: СИСТЕМА_MVP/backend/static/css/excavator-work-v55*.css.
View трогать только при необходимости: СИСТЕМА_MVP/backend/trips/views.py.

Не трогать водителя, диспетчера, горного мастера, reports, models, migrations, БД, .env, production, deploy.

Перед задачей сначала git status.
Если рабочее дерево грязное — остановиться.
Commit/push/deploy только по отдельному разрешению.
```

### Горный мастер — PWA

```text
Название чата: Горный мастер — PWA

Профиль: контур горного мастера, маршрут /mining-master/assignments/.

Работай по документам:
AGENTS.md
PROJECT_MAP.md
CODEX_WORKFLOW.md
FAST_UI_MODE.md
СИСТЕМА_MVP/backend/AGENTS.md

Перед UI-правкой обязательно определить фактический template и конкретный блок.
View: СИСТЕМА_MVP/backend/assignments/views.py.

Не менять общий диспетчерский template шире нужного блока.
Не трогать водителя, экскаваторщика, диспетчера, reports, models, migrations, БД, .env, production, deploy.

Перед задачей сначала git status.
Если рабочее дерево грязное — остановиться.
Commit/push/deploy только по отдельному разрешению.
```

### Диспетчер — PWA

```text
Название чата: Диспетчер — PWA

Профиль: диспетчерский контур, маршрут /dispatcher/control/.

Работай по документам:
AGENTS.md
PROJECT_MAP.md
CODEX_WORKFLOW.md
FAST_UI_MODE.md
СИСТЕМА_MVP/backend/AGENTS.md

Основной template: СИСТЕМА_MVP/backend/templates/trips/dispatcher_control.html.
View трогать только при необходимости: СИСТЕМА_MVP/backend/trips/views.py.

Не трогать водителя, экскаваторщика, горного мастера, reports, models, migrations, БД, .env, production, deploy.

Перед задачей сначала git status.
Если рабочее дерево грязное — остановиться.
Commit/push/deploy только по отдельному разрешению.
```

### Сервер и публикация

```text
Название чата: Сервер и публикация

Профиль: GitHub, push, deploy, driverform.ru, production, collectstatic, systemd, gunicorn, nginx, rollback.

Работай по документам:
AGENTS.md
DEPLOYMENT_WORKFLOW.md
CODEX_WORKFLOW.md
PROJECT_MAP.md

Не смешивать серверные задачи с UI-правками.
Production не менять без прямого разрешения.
Deploy, migrate, collectstatic, restart systemd/gunicorn/nginx запускать только после отдельного плана и подтверждения.

Перед задачей сначала git status.
Если рабочее дерево грязное — остановиться.
Commit/push/deploy только по отдельному разрешению.
```

### Realtime и обмен данными

```text
Название чата: Realtime и обмен данными

Профиль: /realtime/state/, polling, realtime-состояние, обмен данных через backend, связь рабочих контуров через сервер и БД.

Работай по документам:
AGENTS.md
PROJECT_MAP.md
CODEX_WORKFLOW.md
СИСТЕМА_MVP/backend/AGENTS.md

Не делать UI-редизайн в этом чате.
Production, .env, deploy, migrate, collectstatic не трогать без прямого разрешения.
Models, migrations и БД не менять без отдельного плана.

Перед изменениями описывать контракт обмена, участвующие views, зависимые контуры и нужные тесты.
Перед задачей сначала git status.
Если рабочее дерево грязное — остановиться.
Commit/push/deploy только по отдельному разрешению.
```
