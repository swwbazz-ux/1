# Локальный стенд SSE — данные и доступы (T-016, WP-1)

Файл создаётся/обновляется backend-ролью. Это **локальный throwaway-стенд**, не
production: телефоны, коды и id ниже пригодны только для `sse_local`
(`127.0.0.1:55432`) и `test_sse_local`.

Команда создания: `core/management/commands/setup_sse_stand.py`.
Полные команды запуска — `SSE-LOCAL-STAND.md` (realtime) и отчёты
`D:/CopperAgentTeam/pilot/state/handoffs/T-016-backend-1.md` … `-5.md`.

> **Данные ниже — после `--reset` №3 QA-2, 12.11.2026 (итерация 2 QA,
> `handoffs/T-016-qa-2.md` §2–3; до него в `T-016-backend-5.md` §5 были id 9/10,
> в `T-016-backend-4.md` — 5/6).** **Каждый `--reset` меняет id**; актуальные id —
> из свежего вывода команды / этого файла на момент последнего reset. Открытые
> сессии и сохранённые в клиентах id после reset недействительны.

## Учётные записи

| Роль | Телефон | Код | employee_id | access_id | Открытая смена | Техника |
|---|---|---|---|---|---|---|
| `driver` | `+79000000101` | `111111` | 15 | 15 | `shift_id=15`, `workplace_code=driver` | самосвал `SSE-1` (id=17) |
| `excavator_operator` | `+79000000102` | `222222` | 16 | 16 | `shift_id=16`, `workplace_code=excavator_operator` | экскаватор `SSE-ЭКС-1` (id=18) |

Оба доступа: `status=activated`, `is_active=true`. Вход — `POST /` с полями
`phone` и `access_code` (ветка `login_action=login`, `users/views.py:1424-1426`);
после успеха — 302 на рабочий экран роли.

## Назначения

| Что | id | Состояние |
|---|---|---|
| Рабочее назначение водителя (`EquipmentAssignment`) | 15 | `accepted`, `shift_type=day`, `shift_id=null` |
| Рабочее назначение машиниста (`EquipmentAssignment`) | 16 | `accepted`, `shift_type=day`, `shift_id=null` |
| Путевое назначение самосвала (`HaulAssignment`) | 8 | `accepted`, `action=assign`, `truck=SSE-1`, `excavator=SSE-ЭКС-1` |
| Исторический рейс (`Trip`) | 121 | `completed` (создан для данных экранов) |

Состояние журнала на момент записи: `key=production`, `version=2141`,
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
- `--reset` работает и на стенде, который уже использовали приложением: он
  снимает PROTECT-зависимости штатной работы (размещение экскаватора и его
  рабочие точки `assignments.ExcavatorPlacement`/`ExcavatorDumpPointSetting`,
  запросы и снимки паспорта смены водителя `reports.*`) **только** для техники,
  смен и сотрудников стенда; у чужих строк обнуляется ссылка на стендового
  сотрудника, сами строки не удаляются;
- после `--reset` телефоны/коды/id надо брать из свежего вывода команды: таблица
  в начале этого файла описывает стенд до последнего reset;
- `--reset` не удаляет чужие справочники, созданные другими ролями на стенде:
  например, `DumpPoint` «SSE Dump 2 (T-016 e2e)», id=2, остаётся, удаляется только
  стендовый «SSE Отвал 1» (его id меняется с каждым reset — брать из вывода
  команды);
- команда выравнивает id-последовательности таблиц стенда перед записью: другие
  роли вставляли строки с явными id (например, `SSE Dump 2 (T-016 e2e)`), из-за
  чего `INSERT` падал `duplicate key … pkey`;
- команда отказывается работать на базе, имя которой не `sse_local`/`test_sse_local`
  (защита от случайного запуска на product-подобной базе; обход —
  `--allow-any-database`, только осознанно);
- коды доступа — локальные, из production их переносить нельзя.

## Что видно на экранах

- `/driver/shift/` — открытая смена на `SSE-1`, принятое назначение, разметка
  содержит `data-driver-shell` и `data-driver-shift-open="true"`.
- `/excavator/work/` — открытая смена на `SSE-ЭКС-1`, карточка самосвала
  `SSE-1`, разметка содержит `data-eo-shell` и
  `data-eo-current-excavator-id="18"`.
- Оба экрана наследуют `base.html` и подключают `static/js/realtime-client.js`
  (маркер `realtime-client.js` в HTML).
- Проверено фактически 12.11.2026 флагом `--verify-login`: три reset подряд и
  вход обеих ролей — QA (`T-016-qa-2.md` §2–3, id 15/16), два reset —
  `T-016-backend-5.md` §4 (id 9/10); `POST /` → 302, оба экрана → 200, маркеры на
  месте.
