# Текущее состояние актуального HEAD

Версия документа: 0.3
Дата сверки: 13.08.2026
Статус: заполнено по полному read-only аудиту Codex
Источник доказательств: `Вставленная уценка(20260813-103732).md`

## 1. Репозиторий и baseline

### Текущая локальная точка после нормализации

| Параметр | Значение |
|---|---|
| Локальный HEAD | `e71dc4e0b7ca26c0402e6e2ac990c8cae5fd1d1b` |
| Сообщение | `fix(settlement): finalize transferred-room map safeguards` |
| Родитель | `4df3c6276a8f08b86d86e003e8985aeb3b991c61` |
| Рабочее дерево | Чистое |
| Settlement tests | 272/272 PASS |
| origin/main | Остался на `4df3c6276a8f08b86d86e003e8985aeb3b991c61` |
| Публикация | Push/merge/deploy не выполнялись |

Commit содержит ровно пять UI/test-файлов принятого v29. Доменная логика, модели, services и миграции не менялись.

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

Актуальный clean baseline — `4df3c62`, а не ранее переданный `0f4c2a1`.

## 2. Рабочее дерево до нормализации

В рабочем дереве 11 файлов со статусом `.M`.

Содержательные изменения есть в пяти файлах:

- `backend/settlement/tests.py`;
- `backend/static/css/settlement-clerk.css`;
- `backend/static/js/settlement-clerk.js`;
- `backend/templates/settlement/_room_card.html`;
- `backend/templates/settlement/clerk_map.html`.

Объём содержательного diff: `202 insertions(+), 75 deletions(-)`.

Эти изменения содержат принятую заказчиком UI-итерацию v29, удаление фильтра передачи и дополнительные UI/DnD guards. Они не входят в HEAD и не должны быть потеряны при следующих изменениях.

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
| `AccommodationAnchorBedAssignment` | Версионированная interval-связь anchor→bed | PARTIAL |
| `EmployeeBedOccupancy` | Canonical интервалы и manual lifecycle | PARTIAL |
| `build_auto_settlement_preview()` | Узкий GET-only in-memory preview по EquipmentAssignment | PARTIAL/CONTRADICTED |
| `settle_employee_on_bed()` | Атомарный ручной writer | PARTIAL |
| `relocate_employee_to_bed()` | Атомарный ручной перенос | PARTIAL |
| release | Досрочное прекращение через `terminated_at` | PARTIAL |
| Карта/панели/drawer/DnD | Рабочий ручной интерфейс | READY/PARTIAL |
| Apply | Endpoint/service/model отсутствуют | ABSENT |

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

`bulk_create`, `bulk_update` и `QuerySet.update` обходят interval validation. Удаление confirmed/cancelled записей защищено сигналом.

### 4.4. EmployeeBedOccupancy

Поля включают employee, physical bed, тип permanent/temporary/proposed, legacy `settled_at/ended_at` и canonical `starts_at/ends_at/terminated_at`.

Runtime использует единую полуоткрытую семантику:

`starts_at <= moment < min(ends_at, terminated_at)`.

Legacy `ended_at` в runtime activity больше не участвует.

После миграции `0006` DB-level защита overlap bed/employee отсутствует. Модель не имеет FK на cohort, anchor, binding, source, revision или run; отсутствуют reason, release actor и idempotency key. Прямые create/save/update/bulk/delete обходят доменный writer.

### 4.5. CrewPlan и EquipmentAssignment

`CrewPlan` имеет work date, role, revision, status, version и publication metadata. `CrewPlanSlot` хранит equipment, shift, employee и `baseline_employee`.

`publish_crew_plan()` закрывает старые и создаёт новые accepted `EquipmentAssignment`, но новый EquipmentAssignment не хранит FK на CrewPlan или его ревизию. Provenance публикации теряется.

`EquipmentAssignment` не различает постоянное и временное назначение и не является жилищным основанием.

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
- lock Employee → current occupancy → beds → rooms → related occupancies;
- закрывает старую запись через `terminated_at` и создаёт новую;
- mutual A→B/B→A может привести к PostgreSQL deadlock; retry/нормализация DatabaseError отсутствуют.

### Release

- одна transaction;
- заполняет `terminated_at`;
- actor/reason/source/revision не сохраняются.

### Общая оценка

Штатные ручные writers атомарны и полезны, но защищены application-level блокировками только при использовании именно этих writers. Глобального DB-инварианта нет. Полный typed COMMIT contract существует как заготовка, но writer вызывает только SET-R033/SET-R034 overlap subset.

## 8. UI и HTTP

- `/clerk/` и `/clerk/settlement/` используют один map view;
- preview — только GET `preview=1`;
- результат живёт только в context response;
- кнопки Apply и Apply route нет;
- POST endpoint поддерживает только `settle`, `relocate`, `release`;
- CSRF включён;
- непереданный destination закрыт UI и server writer;
- в чистом HEAD занятую койку непереданной комнаты можно визуально начать тащить как source, но destination и server закрыты;
- принятая локальная v29 добавляет дополнительные guards;
- clean HEAD использует cache-buster v23, v29 находится только в dirty worktree.

## 9. Тесты на дату аудита

| Набор | Команда | Результат | Ограничение |
|---|---|---|---|
| Settlement | `python manage.py test settlement --verbosity 1` | 272/272 PASS, 0 skipped, 15.832 сек | SQLite in-memory; не доказывает PostgreSQL concurrency |
| Полный | `python manage.py test --verbosity 1` | 1444 выполнено: 1401 PASS, 1 FAIL, 42 skipped, 172.655 сек | SQLite in-memory |

Единственный текущий failure находится вне settlement:

`DriverWatchPeriodLinkageTests.test_night_replacement_after_midnight_uses_previous_production_date` — фактический `shift.watch_period` равен `None` вместо ожидаемого `WatchPeriod`.

Исторический зелёный результат 1444/42 не является актуальным текущим прогоном.

## 10. Матрица относительно целевой архитектуры

| Целевой элемент | Статус | Фактический разрыв |
|---|---|---|
| Physical A1–A3/B1–B3 | READY | Физический stable fund представлен |
| Непереданный фонд закрыт | READY | Preview/manual destination guards существуют |
| AccommodationAnchor как atomic identity | PARTIAL | Нет строгой type-shape/capacity/calendar semantics |
| Два atomic anchor одной Equipment | CONTRADICTED | Schema допускает, preview требует ровно один |
| Equipment pair `An+Bn` | ABSENT | Нет pair/slot semantics и validation |
| Anchor-bed interval integrity | PARTIAL | Normal save защищает; DB и bulk обходы остаются |
| Authoritative SettlementCohort | PARTIAL | Upstream-факты есть, immutable roster отсутствует |
| AccommodationAnchorCalendarSlot | ABSENT | Модель отсутствует |
| EmployeeAccommodationBinding | ABSENT | Employee→housing право отсутствует |
| Permanent/temporary assignment | CONTRADICTED | Discriminator и CrewPlan provenance отсутствуют |
| Конечная factual occupancy | PARTIAL | Интервалы есть; provenance/DB overlap отсутствуют |
| Room profiles | PARTIAL | transfer/sex/type есть; режим и 2+1 отсутствуют |
| Rule 2+1 | ABSENT | Нет модели, resolver или тестов |
| Non-equipment resolvers | ABSENT | Preview использует только EQUIPMENT |
| Explicit KEEP | PARTIAL | Совпадение employee допускается, action/provenance нет |
| Saved AutoSettlementRun | ABSENT | Модель отсутствует |
| Immutable run rows | ABSENT | Отсутствуют |
| Input hash/stale detection | ABSENT | Отсутствуют |
| Transactional Apply | ABSENT | Endpoint/service отсутствуют |
| Idempotency | ABSENT | Отсутствует |
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
6. permanent/temporary provenance;
7. room profiles и resolver rules.

Только после этого строятся saved preview и transactional Apply.
