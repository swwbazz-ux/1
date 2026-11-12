# Локальный стенд SSE — данные и доступы (T-016, WP-1)

Файл создаётся/обновляется backend-ролью. Это **локальный throwaway-стенд**, не
production: телефоны, коды и id ниже пригодны только для `sse_local`
(`127.0.0.1:55432`) и `test_sse_local`.

Команда создания: `core/management/commands/setup_sse_stand.py`.
Полные команды запуска — `SSE-LOCAL-STAND.md` (realtime) и отчёт
`D:/CopperAgentTeam/pilot/state/handoffs/T-016-backend-1.md`.

## Учётные записи

| Роль | Телефон | Код | employee_id | access_id | Открытая смена | Техника |
|---|---|---|---|---|---|---|
| `driver` | `+79000000101` | `111111` | 1 | 1 | `shift_id=1`, `workplace_code=driver` | самосвал `SSE-1` (id=1) |
| `excavator_operator` | `+79000000102` | `222222` | 2 | 2 | `shift_id=2`, `workplace_code=excavator_operator` | экскаватор `SSE-ЭКС-1` (id=2) |

Оба доступа: `status=activated`, `is_active=true`. Вход — `POST /` с полями
`phone` и `access_code` (ветка `login_action=login`, `users/views.py:1424-1426`);
после успеха — 302 на рабочий экран роли.

## Назначения

| Что | id | Состояние |
|---|---|---|
| Рабочее назначение водителя (`EquipmentAssignment`) | 1 | `accepted`, `shift_type=day`, `shift_id=null` |
| Рабочее назначение машиниста (`EquipmentAssignment`) | 2 | `accepted`, `shift_type=day`, `shift_id=null` |
| Путевое назначение самосвала (`HaulAssignment`) | 1 | `accepted`, `action=assign`, `truck=SSE-1`, `excavator=SSE-ЭКС-1` |
| Исторический рейс (`Trip`) | 1 | `completed` (создан для данных экранов) |

Незакрытых рейсов команда **не создаёт намеренно**: открытый рейс запретил бы
погрузку на экране машиниста (`active_trip` в блокировках). Первый рейс стенда
создаётся кнопкой «Погрузил» в приложении машиниста — это и есть живой сценарий.

## Справочники, которые создаёт команда

- `EquipmentType`: `Самосвал`, `Экскаватор` (общие строки, `--reset` их не удаляет).
- `EquipmentModel`: `SSE Самосвал 40 т` (лимит топлива 600 л), `SSE Экскаватор 1,2 м3`
  (лимит/вместимость 7450 л — нужны форме смены).
- `Equipment`: `SSE-1`, `SSE-ЭКС-1`.
- `RockType`: `Первичная сульфидная руда` (каноническое имя, плотность 2.60,
  разрыхление 1.50).
- `DumpPoint`: `SSE Отвал 1`.
- `TruckCapacityRule`: модель самосвала × порода = 25.00 м3.

## Команды

```powershell
# создать/дополнить (идемпотентно, дублей не делает)
& 'D:/Projects/copper-resources/СИСТЕМА_MVP/.venv/Scripts/python.exe' manage.py setup_sse_stand --settings=config.settings_sse_local

# пересоздать с нуля (удаляет только записи с маркерами SSE-STAND/SSE-*)
& '...python.exe' manage.py setup_sse_stand --reset --settings=config.settings_sse_local
```

Особенности:

- повторный запуск без `--reset` не создаёт новых строк и не нагоняет
  `OperationalStateVersion` (проверено тестом
  `core.test_sse_stand_setup.SetupSseStandTests.test_second_run_is_idempotent`);
- `--reset` меняет id сотрудников, доступов и смен — открытые сессии и
  сохранённые в клиентах id после него недействительны;
- команда отказывается работать на базе, имя которой не `sse_local`/`test_sse_local`
  (защита от случайного запуска на product-подобной базе; обход —
  `--allow-any-database`, только осознанно);
- коды доступа — локальные, из production их переносить нельзя.

## Что видно на экранах

- `/driver/shift/` — открытая смена на `SSE-1`, принятое назначение, разметка
  содержит `data-driver-shell` и `data-driver-shift-open="true"`.
- `/excavator/work/` — открытая смена на `SSE-ЭКС-1`, карточка самосвала
  `SSE-1`, разметка содержит `data-eo-shell` и
  `data-eo-current-excavator-id="2"`.
- Оба экрана наследуют `base.html` и подключают `static/js/realtime-client.js`
  (маркер `realtime-client.js` в HTML).
