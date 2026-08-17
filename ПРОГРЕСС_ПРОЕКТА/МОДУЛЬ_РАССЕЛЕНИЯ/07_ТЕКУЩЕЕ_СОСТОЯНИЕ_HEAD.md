# Текущее состояние реализации settlement

Версия документа: 0.5
Дата последней сверки отчётов: 16.08.2026
Статус: актуализирован по проверенному baseline реализации перед текущей документационной актуализацией — `902e38860f7439343cddd47d8494c993ba3e345b`; schema, внутренний lifecycle и административный takeover реализованы, а write gate, HTTP/session integration и UI integration отсутствуют
Источники доказательств: Git history до implementation baseline `902e38860f7439343cddd47d8494c993ba3e345b`, migration `0007`, код моделей и lifecycle, PostgreSQL 14.24 concurrency-проверки и read-only сверка runtime wiring; SHA последующего документационного commit намеренно не фиксируется внутри самого commit

## 1. Репозиторий и baseline

### Текущая локальная точка

| Параметр | Значение |
|---|---|
| Проверенный baseline документации v0.4 | `45de5d068f3b8c7f971e6065b974e4857e872f76` |
| Сообщение baseline v0.4 | `docs(settlement): finalize architecture specification v0.4` |
| Служебная синхронизация v0.4 | `31f09903ca8682e9e8635ca1b593e8dbd0d394cf` |
| Предыдущий baseline документации v0.3 | `aba37c44e39b6f6f3bc0ab2caa51d9dae4c4ed9c` |
| Кодовый baseline | `e71dc4e0b7ca26c0402e6e2ac990c8cae5fd1d1b` |
| Проверенный implementation baseline перед текущей документационной актуализацией | `902e38860f7439343cddd47d8494c993ba3e345b` |
| Рабочее дерево в implementation baseline | Чистое |
| Settlement tests после takeover | SQLite: 324 PASS / 8 PostgreSQL-only skip; PostgreSQL 14.24: 332/332 PASS |
| origin/main | Локальная цепочка не опубликована; точное ahead-число намеренно не фиксируется |
| Публикация | Push/merge/deploy не выполнялись |

Цепочка baseline и локальных усилений реализации: `e71dc4e0 → aba37c44 → 45de5d0 → 31f09903 → b8b54cbe → 85df6213 → 4e88278e → 1e701208 → f7719c7d → ef5b96b → 33e09a8 → d19b040 → d2e3404 → 902e388`. После документации 0.4 исправлены PostgreSQL employee lock, lifecycle тестового `FileResponse`, публичные массовые ORM-обходы anchor-bed/occupancy и порядок блокировок ручных writers; затем добавлены schema control lease/event, migration `0007`, внутренний lifecycle и административный takeover. Migration `0007` проверена только в изолированных тестовых средах и не объявляется применённой к production. Локальная цепочка не опубликована в `origin/main`.

Резервный patch до ADR-014: SHA-256 `8D34729F75247EEFCE37535F35C1CE8D7D0C4D8CC7ABAE3997A956821C6BBC47`.

### Предыдущий clean baseline аудита

| Параметр | Подтверждённое значение |
|---|---|
| Checkout | `C:\Users\swwba\Desktop\Проект учетная система\ПОЕКТ-integration-settlement` |
| Ветка | `main` |
| HEAD | `4df3c6276a8f08b86d86e003e8985aeb3b991c61` |
| origin/main | Совпадает с HEAD |
| Сообщение | `feat(settlement): complete clerk occupancy workflow` |
| Старый контрольный commit | `79bcc7b5d64d64eff97bc5f5365dc06e04806c53` существует |
| Ранее заявленный `0f4c2a1` | Не существует в репозитории |
| Production | В аудите не подключался; совпадение с HEAD не доказано |

Исходный clean baseline аудита — `4df3c62`, а не ранее переданный `0f4c2a1`. Текущая локальная цепочка baseline продолжена commit `e71dc4e0`, документацией v0.3 в commit `aba37c44` и принятым baseline документации v0.4 в commit `45de5d0`; эти commits не опубликованы в `origin/main`.

## 2. Историческое рабочее дерево до нормализации

До commit `e71dc4e0` в рабочем дереве было 11 файлов со статусом `.M`.

Содержательные изменения есть в пяти файлах:

- `backend/settlement/tests.py`;
- `backend/static/css/settlement-clerk.css`;
- `backend/static/js/settlement-clerk.js`;
- `backend/templates/settlement/_room_card.html`;
- `backend/templates/settlement/clerk_map.html`.

Объём содержательного diff: `202 insertions(+), 75 deletions(-)`.

Эти изменения содержали принятую заказчиком UI-итерацию v29, удаление фильтра передачи и дополнительные UI/DnD guards. Они вошли в commit `e71dc4e0` и сохранены в актуальном HEAD.

После scope-сверки 13.08.2026 точный baseline определён так: button styling и обновлённые settlement-summary карточки сохраняются; room-transfer legend, связанный CSS и тесты исключаются; числовое количество переданных комнат сохраняется.

Ещё шесть файлов отмечены изменёнными только из-за LF/CRLF; content hash совпадает с HEAD:

- `assignments/admin.py`;
- `assignments/models.py`;
- `assignments/services.py`;
- `assignments/test_crew_planning.py`;
- `settlement/admin.py`;
- `settlement/models.py`.

Этот dirty state устранён commit `e71dc4e0`. Шесть LF/CRLF-only статусов очищены без содержательных изменений. В итоговом worktree staged, unstaged и untracked изменения отсутствуют.

## 3. Подтверждённые компоненты

| Компонент | Фактическое состояние | Статус относительно цели |
|---|---|---|
| `PhysicalRoom/PhysicalBed` | Физическая карта, A/B/ITR, transfer status, sex restriction | READY/PARTIAL |
| `SettlementSource/SettlementRevision` | Источники и версии с сильной ORM-защитой | READY/PARTIAL |
| `AccommodationAnchor` | Типы equipment/function/reserve/group/service/protected; Equipment→Anchor = 1:N | PARTIAL |
| `AccommodationAnchorBedAssignment` | Версионированная interval-связь anchor→bed; публичные mass-write API запрещены | PARTIAL |
| `EmployeeBedOccupancy` | Canonical интервалы, manual lifecycle; публичные mass-write API запрещены | PARTIAL |
| `build_auto_settlement_preview()` | Узкий GET-only in-memory preview по EquipmentAssignment | PARTIAL/CONTRADICTED |
| `settle_employee_on_bed()` | Атомарный ручной writer | PARTIAL |
| `relocate_employee_to_bed()` | Атомарный ручной перенос | PARTIAL |
| release | Досрочное прекращение через `terminated_at` | PARTIAL |
| Карта/панели/drawer/DnD | Рабочий ручной интерфейс | READY/PARTIAL |
| Apply | Endpoint/service/model отсутствуют | ABSENT |
| `SettlementControlLease/SettlementControlEvent` | Schema, migration `0007` и bootstrap FREE singleton присутствуют в репозитории | PRESENT |
| Control lifecycle | Внутренние ensure/acquire/heartbeat/release/expire, HMAC session binding, token/fencing и audit events реализованы | PRESENT |
| Административный takeover | Внутренняя атомарная команда с обязательной причиной и fencing реализована | PRESENT |
| Settlement write gate | Manual writers не проверяют lease token/revision и не используют общий кадровый lock plan | ABSENT |
| Control HTTP/session integration | Endpoints/URLs и хранение lease credentials в пользовательской session отсутствуют | ABSENT |
| Control UI integration | Browser heartbeat, owner indicator, read-only banner и takeover UI отсутствуют | ABSENT |

## 4. Фактическая модель данных

### 4.1. Physical fund

`PhysicalRoom` хранит общежитие, этаж, номер, тип, transfer status, sex restriction, capacity и координаты карты. `PhysicalBed` хранит room, stable ID, block A/B/ITR и position 1..3.

DB гарантирует уникальность комнаты и позиции койки, но не гарантирует равенство числа физических коек полю capacity.

`PhysicalBed.is_available` означает только переданность комнаты, а не фактическую незанятость.

### 4.2. AccommodationAnchor

Одна Equipment уже может иметь два и более active anchor: unique constraint на Equipment отсутствует. Поэтому для целевой пары не требуется ломать FK-cardinality.

Проблема находится в preview: он требует ровно один active equipment anchor и объявляет любое другое количество неоднозначностью.

Текущий anchor семантически ближе к атомарному месту, потому что штатная anchor-bed связь допускает не более одной effective bed. Отдельной модели пары, capacity или calendar slot нет.

### 4.3. Anchor-bed assignment

Штатный `save()` блокирует anchor, затем bed, выполняет `full_clean()` и запрещает interval overlap.

DB-level conditional unique покрывает только открытые confirmed записи `valid_to IS NULL`. Две пересекающиеся конечные confirmed записи можно создать в обход штатного `save()`.

Публичные `bulk_create`, `bulk_update` и `QuerySet.update` запрещены специализированным QuerySet с кодом `anchor_bed_mass_write_forbidden`; normal instance `save()` и draft delete сохранены. Private ORM API, raw SQL и внешний DB-клиент остаются техническими обходами. Удаление confirmed/cancelled записей защищено сигналом. DB-level finite interval overlap ещё не закрыт.

### 4.4. EmployeeBedOccupancy

Поля включают employee, physical bed, тип permanent/temporary/proposed, legacy `settled_at/ended_at` и canonical `starts_at/ends_at/terminated_at`.

Runtime использует единую полуоткрытую семантику:

`starts_at <= moment < min(ends_at, terminated_at)`.

Legacy `ended_at` в runtime activity больше не участвует.

После миграции `0006` DB-level защита overlap bed/employee отсутствует. Модель не имеет FK на cohort, anchor, binding, source, revision или run; отсутствуют reason, release actor и idempotency key. Публичные `update/bulk_create/bulk_update` запрещены специализированным QuerySet с кодом `employee_bed_occupancy_mass_write_forbidden`. Разрешённые `create/save/delete`, private ORM API, raw SQL и внешний DB-клиент всё ещё требуют дисциплины writer/следующих DB-гарантий.

### 4.5. CrewPlan и EquipmentAssignment

`CrewPlan` имеет work date, role, revision, status, version и publication metadata. `CrewPlanSlot` хранит equipment, shift, employee и `baseline_employee`.

`publish_crew_plan()` закрывает старые и создаёт новые accepted `EquipmentAssignment`, но новый EquipmentAssignment не хранит FK на CrewPlan или его ревизию. Provenance публикации теряется.

`EquipmentAssignment` не хранит provenance опубликованного CrewPlan и сам по себе не является сохранённым жилищным основанием. Целевая версия допускает использовать текущий опубликованный контекст только при создании первого binding с обязательным snapshot; такого механизма в HEAD нет.

### 4.6. Вахта и приезд

В upstream уже существуют:

- `WatchComposition`;
- текущая `Employee.watch_composition` без истории;
- `WatchPeriod`;
- `EmployeeShift`;
- `RotationResponse` с arrival/departure/намерением.

Ни одна из этих сущностей не является утверждённым immutable ArrivalRoster конкретного авторасселения.

## 5. Фактическая трассировка preview

```mermaid
flowchart TD
    A["Accepted EquipmentAssignment"] --> B["Equipment"]
    B --> C["Ровно один active equipment anchor"]
    C --> D["Ровно одна confirmed effective bed"]
    D --> E["Transferred room"]
    E --> F["Target-bed occupancy check"]
    F --> G["In-memory preview"]
```

Preview не использует WatchComposition, WatchPeriod, RotationResponse, CrewPlan provenance, постоянный binding, пол, room sex restriction, room type, работодателя, режим комнаты или 2+1.

Scope содержит только effective `EquipmentAssignment` со статусом accepted, заполненной ролью, без operational `EmployeeShift` FK и с подходящим assigned/accepted/ended interval.

Следствия:

- сотрудники без техники, ИТР, службы, reserve/protected/group не входят;
- сотрудник может попасть в preview, даже если фактически не заезжает;
- временная производственная перестановка может быть ошибочно воспринята как жилищное основание;
- календарная группа игнорируется.

## 6. Главная доказанная ошибка preview

Preview группирует конкуренцию по `(physical_bed_id, shift_type)`. Поэтому DAY и NIGHT одной техники могут получить одну PhysicalBed и обе строки будут считаться успешными.

Это противоречит утверждённой физической модели:

- одной машине соответствует пара `An + Bn`;
- работники противоположных смен занимают разные койки пары;
- A/B не означают DAY/NIGHT;
- одновременно отдыхающие три человека распределяются по комнате 2+1.

Текущий manual writer запрещает обычное интервальное занятие одной bed двумя сотрудниками. Следовательно, preview способен вернуть результат, который невозможно корректно применить существующим writer.

Это не отсутствие отдельной проверки, а фундаментальное противоречие алгоритма целевой модели.

## 7. Ручной контур

### Settle

- одна transaction;
- lock Employee → target Bed → Room → связанные occupancies;
- проверяются active employee, transferred room, sex, interval и overlap;
- создаётся occupancy;
- idempotency и provenance отсутствуют.

### Relocate

- одна transaction;
- требует ровно одну active occupancy;
- неблокирующее discovery только ID текущей occupancy/source bed;
- lock Employee → beds по PK → rooms по PK → related occupancies по PK;
- `FOR UPDATE OF` ограничен базовой таблицей каждого набора и не блокирует joined room/dormitory/bed;
- после блокировок повторно проверяются current occupancy, source bed, active interval и conflicts;
- закрывает старую запись через `terminated_at` и создаёт новую;
- mutual A→B/B→A и pairwise manual-writer matrix проверены на PostgreSQL 14.24 без `40P01`, `55P03` и HTTP 500.

### Release

- одна transaction;
- заполняет `terminated_at`;
- actor/reason/source/revision не сохраняются.

### Общая оценка

Штатные ручные writers атомарны, используют согласованный порядок Employee → Beds → Rooms → Occupancies и закрыты от публичных массовых ORM-изменений. Глобального DB interval-инварианта и полного typed COMMIT contract всё ещё нет; writer вызывает только SET-R033/SET-R034 overlap subset.

### Исключительность управляющего

Текущий `settlement_clerk_access_from_request()` проверяет session `employee_access_id`, активность `EmployeeAccess`/Employee/Role и допускает к settlement роли `settlement_clerk` и `admin`. `role_session_state()` ограничивает активную роль внутри одной сессии/сотрудника, но не между разными сотрудниками и устройствами. В schema уже существуют singleton `SettlementControlLease`, session hash, token и fencing revision, а внутренние lifecycle/takeover команды меняют их атомарно и создают `SettlementControlEvent`.

Однако runtime HTTP-контур не вызывает эти команды: manual POST по-прежнему напрямую запускает `settle_employee_on_bed()`, `relocate_employee_to_bed()` или `release_employee_from_bed()` без write gate. Control endpoints/URLs, хранение credentials в пользовательской session и UI отсутствуют. Кроме того, общий порядок `Lease → все Employee → все EmployeeAccess → доменные строки` ещё не реализован: acquire/takeover используют существующий access-root lock и должны быть приведены к утверждённому кадровому префиксу до подключения gate.

Следствия для runtime в implementation baseline `902e3886`:

- два разных делопроизводителя либо делопроизводитель и администратор могут одновременно пройти серверную авторизацию;
- две вкладки/два HTTP-запроса одного пользователя могут выполняться параллельно;
- кадровый график, `WatchComposition`, `WatchPeriod` и открытая смена не являются precondition ручного POST writer;
- background Apply отсутствует, но будущий Apply без общего control gate создал бы дополнительный конкурентный writer;
- schema/lifecycle/takeover доступны только как внутренний сервисный контур и пока не дают пользователю техническую исключительность управления.

## 8. UI и HTTP

- `/clerk/` и `/clerk/settlement/` используют один map view;
- preview — только GET `preview=1`;
- результат живёт только в context response;
- кнопки Apply и Apply route нет;
- POST endpoint поддерживает только `settle`, `relocate`, `release`;
- CSRF включён;
- непереданный destination закрыт UI и server writer;
- в чистом HEAD занятую койку непереданной комнаты можно визуально начать тащить как source, но destination и server закрыты;
- принятая v29 добавляет дополнительные guards и cache-buster v29;
- изменения v29 зафиксированы локальным commit `e71dc4e0`; dirty UI-worktree устранён.

## 9. Актуальные проверки

| Набор | Команда | Результат | Ограничение |
|---|---|---|---|
| Settlement SQLite после takeover | `python manage.py test settlement --verbosity 1 --noinput` | 324 PASS, 8 PostgreSQL-only skip | Последний подтверждённый локальный полный settlement suite |
| Settlement PostgreSQL 14.24 после takeover | та же команда с изолированным PostgreSQL profile | 332/332 PASS | Последний подтверждённый полный settlement suite и concurrency |
| Mutual relocate | PostgreSQL TransactionTestCase, три последовательных запуска | 1/1 PASS каждый | Barrier/timeouts; mutation старого порядка воспроизводит `40P01` |
| Pairwise manual writers | relocate/relocate, settle/relocate, relocate/release, settle/settle | PASS | Нет `40P01`, `55P03`, HTTP 500 и двойной occupancy |
| Migration drift | `makemigrations --check --dry-run` | Изменений нет | Migration `0007` соответствует model state |

После PostgreSQL-проверок test database удалялась. Эти результаты относятся к изолированным локальным средам и не доказывают применение migration `0007` к production.

## 10. Матрица относительно целевой архитектуры

| Целевой элемент | Статус | Фактический разрыв |
|---|---|---|
| Physical A1–A3/B1–B3 | READY | Физический stable fund представлен |
| Непереданный фонд закрыт | READY | Preview/manual destination guards существуют |
| AccommodationAnchor как atomic identity | PARTIAL | Нет строгой type-shape/capacity/calendar semantics |
| Два atomic anchor одной Equipment | CONTRADICTED | Schema допускает, preview требует ровно один |
| Equipment pair `An+Bn` | ABSENT | Нет pair/slot semantics и validation |
| Anchor-bed interval integrity | PARTIAL | Normal save и публичный QuerySet guard защищают; private/raw и DB-level finite interval overlap остаются |
| Authoritative SettlementCohort | PARTIAL | Upstream-факты есть, immutable roster отсутствует |
| AccommodationAnchorCalendarSlot | ABSENT | Модель отсутствует |
| EmployeeAccommodationBinding | ABSENT | Employee→housing право отсутствует |
| Initial-binding provenance snapshot | ABSENT | CrewPlan provenance теряется; binding и снимок основания отсутствуют |
| Конечная factual occupancy | PARTIAL | Интервалы есть; provenance/DB overlap отсутствуют |
| Room profiles | PARTIAL | transfer/sex/type есть; режим и 2+1 отсутствуют |
| Rule 2+1 | ABSENT | Нет модели, resolver или тестов |
| Non-equipment resolvers | ABSENT | Preview использует только EQUIPMENT |
| Explicit category assignments | ABSENT | Нет версионируемого назначения RESERVE и аналогичных категорий |
| Trainee structured-state route | ABSENT | Существующий authoritative trainee state/adapter не найден; Vacancy исключена ADR-030 |
| Authoritative SettlementResident | READY | Internal Employee wrapper и внешние карточки с control/revision/provenance реализованы migration `0010`; M4/M5 ещё используют Employee FK |
| Group capacity conflict | ABSENT | Preview не агрегирует потребность/дефицит и не защищает от скрытого выбора людей |
| Explicit KEEP | PARTIAL | Совпадение employee допускается, action/provenance нет |
| Saved AutoSettlementRun | ABSENT | Модель отсутствует |
| Immutable run rows | ABSENT | Отсутствуют |
| Input hash/stale detection | ABSENT | Отсутствуют |
| Transactional Apply | ABSENT | Endpoint/service отсутствуют |
| Idempotency | ABSENT | Отсутствует |
| Единственный активный управляющий | PARTIAL | Singleton lease, lifecycle и fencing реализованы внутренне; write gate, HTTP/session binding и server read-only enforcement отсутствуют |
| Административный takeover | PARTIAL | Внутренняя команда реализована; endpoint, UI подтверждения и fencing действующих manual writers отсутствуют |
| Единый полный COMMIT validator | PARTIAL | Подключены только overlap rules |
| Temporary manual exception | PARTIAL | Temporary/relocate/release есть; binding/reason нет |
| Permanent binding correction | ABSENT | Binding отсутствует |

## 11. Решение архитектора

Строить Apply поверх текущего preview запрещено.

Сохраняются:

- physical fund и карта;
- source/revision;
- anchor как атомарная идентичность;
- anchor-bed history после усиления инвариантов;
- canonical occupancy interval;
- manual writers после унификации validator/locks/audit;
- CrewPlan, WatchComposition/WatchPeriod и RotationResponse как upstream-факты;
- UI-каркас, панели и DnD.

До нового preview необходимы:

1. усиление текущих interval/writer invariants;
2. два atomic equipment slots и validation пары `An+Bn`;
3. calendar slots;
4. permanent employee binding;
5. authoritative versioned cohort;
6. snapshot первичного CrewPlan/EquipmentAssignment при создании binding;
7. явные category assignments и маршрут стажёра по настоящей должности через authoritative structured state/adapter;
8. room profiles, resolver rules и групповые конфликты дефицита.

Только после этого строятся saved preview и transactional Apply.
