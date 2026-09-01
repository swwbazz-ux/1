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
- `portal` — открытый сайт компании, закрытый портал сотрудников участка № 2, редакционный контур, опросы, обращения и витрина готовых производственных показателей.
- `settlement` — физическая карта общежитий, размещения сотрудников и первая функциональная вкладка рабочего места делопроизводителя.

## Рабочие контуры

### Водитель

- Template: `СИСТЕМА_MVP/backend/templates/users/driver_shift.html`.
- View: `СИСТЕМА_MVP/backend/users/views.py`.
- Маршруты: `/driver/`, `/driver/shift/`.
- Manifest: `/driver.webmanifest`.
- Service worker: `/driver-sw.js`.
- Shell: `driver-mobile-shell-v178` (опубликован на production 02.09.2026).

### Машинист экскаватора

- Template: `СИСТЕМА_MVP/backend/templates/trips/excavator_work.html`.
- View: `СИСТЕМА_MVP/backend/trips/views.py`.
- Маршрут: `/excavator/work/`.
- Manifest: `/excavator.webmanifest`.
- Service worker: `/excavator-sw.js`.
- Shell: `excavator-mobile-shell-v175` (опубликован в production 02.09.2026).

### Общий адаптивный экран «Смена»

- Общий поздний слой двух мобильных ролей:
  `СИСТЕМА_MVP/backend/static/css/mobile-shift-unified-v1.css`.
- Единая разметка:
  `СИСТЕМА_MVP/backend/templates/includes/mobile_shift_screen.html`; единая
  механика полей, IME и удержания:
  `СИСТЕМА_MVP/backend/static/js/mobile-shift-unified-v1.js`.
- Водитель и Машинист экскаватора используют одинаковые компонентные классы
  для заголовка, показаний, итогов, действий и иконки «Смена»; отдельный блок
  «Назначение» удалён из обеих ролей.
- Компоновка делит доступную высоту адаптивными долями `minmax` и не создаёт
  горизонтальную, вертикальную или внутреннюю прокрутку; правил под конкретные
  размеры снимков нет.
- Ролевой состав данных сохраняется: у Водителя три показателя, у Машиниста
  экскаватора два; серверные формы и доменные проверки остаются раздельными.
- Основное действие у обеих ролей использует одинаковую механику удержания:
  одна секунда для начала смены и две секунды для закрытия.
- Общий компонент закрыт по умолчанию и становится `display:grid` только при
  явно активной вкладке `Смена`; на соседних вкладках он имеет нулевой размер
  и не перехватывает касания, даже если инициализация JavaScript задержалась.
- Актуальная реализация опубликована commit `bac8c90c` как Driver shell `v178`
  и Excavator shell `v175`; два прежних ролевых DOM-фрагмента и дублирующие
  механизмы заменены одним общим компонентом.

### Нативные Android-приложения

- Общий Capacitor-контур: `mobile/capacitor-shell`; нативная логика одна для
  Водителя и Машиниста экскаватора, параметры сборки находятся в
  `mobile/capacitor-shell/profiles/<профиль>/app.properties`.
- На production остаются подписанные профили: Водитель `0.1.8 (10)`
  (`driver-10.apk`) и Машинист экскаватора `0.1.12 (15)`
  (`excavator-15.apk`). Понижать versionCode или перезаписывать эти versioned
  APK другим содержимым нельзя.
- Native phone handoff через второй шаг и App Link отвергнут пользователем
  01.09.2026 и удалён из исходного кода и production в commit `8dee7c5c`.
  `/start/` снова показывает
  прямую APK-ссылку одним действием и отдельный браузерный вариант; после
  установки APK номер вводится в приложении вручную.
- Прежняя передача номера в браузерную/PWA-версию через `?phone=...&install=1`
  сохраняется: она работает внутри ролевого web-origin и не относится к APK.
- Опубликованные APK `10/15` содержат старый, теперь недостижимый App Link
  listener, но обычный launcher-запуск его не использует. Следующую сборку
  обязательно выпускать с versionCode выше `10/15`; повторно публиковать эти
  коды из изменённого исходника запрещено. Серверные handoff- и
  `assetlinks.json`-маршруты удалены и публично отвечают `404`.

### Горный мастер

- View: `СИСТЕМА_MVP/backend/assignments/views.py`.
- Маршрут: `/mining-master/assignments/`.
- Service worker: `/mining-master-sw.js`.
- Shell: `mining-master-mobile-shell-v101`.
- Шаблон: фактически экран Горного мастера проходит через `assignments/views.py`, который использует диспетчерский контур рендера из `trips.views`. Общий рабочий template/shell сейчас связан с `СИСТЕМА_MVP/backend/templates/trips/dispatcher_control.html`; перед UI-правкой нужно подтвердить конкретный блок шаблона по коду.

### Диспетчер

- Template: `СИСТЕМА_MVP/backend/templates/trips/dispatcher_control.html`.
- View: `СИСТЕМА_MVP/backend/trips/views.py`.
- Маршрут: `/dispatcher/control/`.
- Service worker: `/dispatcher-sw.js`.
- Shell: `dispatcher-desktop-shell-v29`.

### Отдел управления персоналом

- Views: `СИСТЕМА_MVP/backend/users/oup_views.py`.
- Forms: `СИСТЕМА_MVP/backend/users/oup_forms.py`.
- Domain services: `СИСТЕМА_MVP/backend/users/oup_services.py`.
- Templates: `СИСТЕМА_MVP/backend/templates/users/oup_*.html`.
- Header include: `СИСТЕМА_MVP/backend/templates/includes/oup_header.html`.
- CSS: `СИСТЕМА_MVP/backend/static/css/oup-workplace-v1.css`.
- JavaScript: `СИСТЕМА_MVP/backend/static/js/oup-workplace.js`.
- Маршруты: `/oup/`, `/oup/employees/`, `/oup/dismissed/`, `/oup/log/`.
- Рабочий период ОУП: один активный период на всю 30-дневную вахту, без деления на дневную и ночную смены и без техники; технически хранится в `EmployeeShift`.
- Границы: создание, редактирование, фото и увольнение сотрудников; без PIN, проживания, табеля, охраны труда и назначения техники.

### Делопроизводитель

- Общая основа шаблонов: `СИСТЕМА_MVP/backend/templates/clerk/base.html`.
- Общая шапка и навигация: `СИСТЕМА_MVP/backend/templates/includes/clerk_workplace_header.html`.
- Первая функциональная вкладка: `СИСТЕМА_MVP/backend/templates/settlement/clerk_map.html`.
- Views и маршруты: `СИСТЕМА_MVP/backend/settlement/views.py`, `urls.py`.
- CSS оболочки: `СИСТЕМА_MVP/backend/static/css/clerk-workplace.css`.
- CSS и JavaScript вкладки: `СИСТЕМА_MVP/backend/static/css/settlement-clerk.css`, `static/js/settlement-clerk.js`.
- Главная точка входа: `/clerk/`; карта: `/clerk/settlement/`; целевой вход: `/clerk/login/`.
- Manifest: `/clerk/manifest.webmanifest`; service worker: `/clerk/sw.js`; shell: `clerk-workplace-shell-v1`.
- Техническая роль: `settlement_clerk`; пользовательское имя рабочего места: `Делопроизводитель`; доступ также разрешён `admin`.
- Старый `/settlement/` является только переходным адресом и не образует отдельное PWA.

### Корпоративный портал

- Views и серверные правила: `СИСТЕМА_MVP/backend/portal/views.py`, `services.py`, `auth.py`, `login_security.py`.
- Модели и формы: `СИСТЕМА_MVP/backend/portal/models.py`, `forms.py`.
- Шаблоны: `СИСТЕМА_MVP/backend/templates/portal/`.
- CSS и JavaScript: `СИСТЕМА_MVP/backend/static/portal/`.
- Открытая часть: `/company/`.
- Закрытая часть сотрудников: `/portal/`.
- Редакционный контур: `/portal/manage/`.
- Граница: портал не рассчитывает рейтинг, KPI и результаты смен; он получает готовый снимок через настраиваемый серверный провайдер.

## Правило локализации задачи

Перед изменением нужно записать для себя:

- тип задачи;
- рабочий контур;
- маршрут или экран;
- конкретные `template`, `static`, `view`, тесты и service worker, если они участвуют;
- зоны, которые запрещено трогать.

Если задача является мелкой UI-правкой, рабочая область ограничивается конкретным экраном и связанными файлами. Модели, миграции, база данных, production, deploy и соседние контуры не входят в такую задачу.
