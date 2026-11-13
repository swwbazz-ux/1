# Локальный стенд SSE — данные и доступы (T-016, WP-1)

Файл создаётся/обновляется backend-ролью. Это **локальный throwaway-стенд**, не
production: телефоны, коды и id ниже пригодны только для `sse_local`
(`127.0.0.1:55432`) и `test_sse_local`.

Команда создания: `core/management/commands/setup_sse_stand.py`.
Полные команды запуска — `SSE-LOCAL-STAND.md` (realtime) и отчёты
`D:/CopperAgentTeam/pilot/state/handoffs/T-016-backend-1.md` … `-6.md`.

> **Данные ниже — после двух `--reset` итерации 6, 12.11.2026 11:5x
> (`handoffs/T-016-backend-6.md`).** До них: QA-2 — id 15/16 (`T-016-qa-2.md`
> §2–3), `T-016-backend-5.md` §5 — 9/10, `T-016-backend-4.md` — 5/6.
> **Каждый `--reset` меняет id**; актуальные id — из свежего вывода команды /
> этого файла на момент последнего reset. Открытые сессии и сохранённые в
> клиентах id после reset недействительны.

## Учётные записи

| Роль | Телефон | Код | employee_id | access_id | Открытая смена | Техника |
|---|---|---|---|---|---|---|
| `driver` | `+79000000101` | `111111` | 19 | 19 | `shift_id=19`, `workplace_code=driver` | самосвал `SSE-1` (id=21) |
| `excavator_operator` | `+79000000102` | `222222` | 20 | 20 | `shift_id=20`, `workplace_code=excavator_operator` | экскаватор `SSE-ЭКС-1` (id=22) |

Оба доступа: `status=activated`, `is_active=true`. Вход — `POST /` с полями
`phone` и `access_code` (ветка `login_action=login`, `users/views.py:1424-1426`);
после успеха — 302 на рабочий экран роли.

## Назначения

| Что | id | Состояние |
|---|---|---|
| Рабочее назначение водителя (`EquipmentAssignment`) | 19 | `accepted`, `shift_type=day`, `shift_id=null` |
| Рабочее назначение машиниста (`EquipmentAssignment`) | 20 | `accepted`, `shift_type=day`, `shift_id=null` |
| Путевое назначение самосвала (`HaulAssignment`) | 10 | `accepted`, `action=assign`, `truck=SSE-1`, `excavator=SSE-ЭКС-1` |
| Исторический рейс (`Trip`) | 123 | `completed` (создан для данных экранов) |

Состояние журнала на момент записи: `key=production`, `version=2221`,
`journal_epoch=fd29a711-6e8b-43b9-9c6f-6e4ffba0baa3` (растёт от действий ролей,
эпоха при `--reset` не меняется; дублей `(key, version)` — 0).


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

# пересоздать и сразу проверить вход обеих ролей (POST / -> 302, экраны -> 200)
& '...python.exe' manage.py setup_sse_stand --reset --verify-login --settings=config.settings_sse_local
```

Особенности:

- повторный запуск без `--reset` не создаёт новых строк и не нагоняет
  `OperationalStateVersion` (проверено тестом
  `core.test_sse_stand_setup.SetupSseStandTests.test_second_run_is_idempotent`);
- `--reset` меняет id сотрудников, доступов и смен — открытые сессии и
  сохранённые в клиентах id после него недействительны;
- `--reset` работает и на стенде, который уже использовали приложением, и снимает
  PROTECT-зависимости штатной работы, причём по разным правилам:
  - `assignments.ExcavatorPlacement` / `ExcavatorDumpPointSetting` — удаляются
    строки стендовой техники; у чужих строк обнуляется `changed_by` (ссылка на
    стендового сотрудника), сами строки остаются;
  - `reports.DriverShiftPassportCaptureRequest` (изменяемая outbox-строка) —
    удаляются запросы стендовых смен; у чужих запросов, инициированных стендовым
    сотрудником, обнуляется `captured_by`, строка не удаляется;
  - `reports.DriverShiftPassportSnapshot` — удаляются **только снимки стендовых
    смен** (отбор по `shift_id`); чужие снимки не изменяются вовсе: если чужой
    снимок ссылается на стендового сотрудника, reset честно падает
    `CommandError`, а не правит аудит-запись (review-1, m-1/m-2);
  - `shifts.AchievementUnlock` — удаляются строки стендовых сотрудников, техники
    и смен;
- после `--reset` телефоны/коды/id надо брать из свежего вывода команды: таблица
  в начале этого файла описывает стенд до последнего reset;
- `--reset` не удаляет чужие справочники, созданные другими ролями на стенде:
  например, `DumpPoint` «SSE Dump 2 (T-016 e2e)», id=2, остаётся, удаляется только
  стендовый «SSE Отвал 1» (его id меняется с каждым reset — брать из вывода
  команды);
- команда выравнивает id-последовательности таблиц стенда, но **только при
  `--reset`** и **только вперёд** (берётся `GREATEST(max(id), last_value)`; пустая
  таблица не трогается): другие роли вставляли строки с явными id (например,
  `SSE Dump 2 (T-016 e2e)`), из-за чего `INSERT` падал `duplicate key … pkey`.
  Обычный запуск без `--reset` последовательности не меняет (review-1, m-4/m-5);
- команда отказывается работать на базе, имя которой не `sse_local`/`test_sse_local`
  (защита от случайного запуска на product-подобной базе; обход —
  `--allow-any-database`, только осознанно);
- коды доступа — локальные, из production их переносить нельзя.

## Что видно на экранах

- `/driver/shift/` — открытая смена на `SSE-1`, принятое назначение, разметка
  содержит `data-driver-shell` и `data-driver-shift-open="true"`.
- `/excavator/work/` — открытая смена на `SSE-ЭКС-1`, карточка самосвала
  `SSE-1`, разметка содержит `data-eo-shell` и
  `data-eo-current-excavator-id="22"`.
- Оба экрана наследуют `base.html` и подключают `static/js/realtime-client.js`
  (маркер `realtime-client.js` в HTML).
- Проверено фактически 12.11.2026 флагом `--verify-login`: итерация 6 — два reset
  подряд (`T-016-backend-6.md` §4, id 19/20); QA — три reset (`T-016-qa-2.md`
  §2–3, id 15/16); `T-016-backend-5.md` §4 — два reset (id 9/10). Везде `POST /`
  → 302, оба экрана → 200, маркеры на месте.
