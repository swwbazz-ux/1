# Текущее состояние реализации settlement

Версия документа: 0.6
Дата последней сверки отчётов: 18.08.2026
Статус: актуализирован по clean HEAD `c52ae35f2a204556320541cfa989e4d2e71b2725`; migration leaves — `assignments.0007_equipment_assignment_provenance` и `settlement.0016_shift_scoped_apply_and_occupancy`
Источники доказательств: принятые M4–M8, официальный источник смены, раздельное применение ночной и дневной смены, ручной сменный контур и нормативное решение ADR-031; production в этой актуализации не проверялась и не объявляется равной локальному HEAD

## 1. Репозиторий и baseline

### Текущая локальная точка

| Параметр | Значение |
|---|---|
| Проверенный baseline документации v0.4 | `45de5d068f3b8c7f971e6065b974e4857e872f76` |
| Сообщение baseline v0.4 | `docs(settlement): finalize architecture specification v0.4` |
| Служебная синхронизация v0.4 | `31f09903ca8682e9e8635ca1b593e8dbd0d394cf` |
| Предыдущий baseline документации v0.3 | `aba37c44e39b6f6f3bc0ab2caa51d9dae4c4ed9c` |
| Кодовый baseline | `e71dc4e0b7ca26c0402e6e2ac990c8cae5fd1d1b` |
| Текущий локальный HEAD | `c52ae35f2a204556320541cfa989e4d2e71b2725` |
| Ветка | `main` |
| Рабочее дерево до документальных изменений | Чистое; staged/unstaged/untracked = 0/0/0 |
| Migration leaves | `assignments.0007_equipment_assignment_provenance`; `settlement.0016_shift_scoped_apply_and_occupancy` |
| Версия ресурсов карты | `settlement-map-v33` |
| origin/main | Ahead 6, behind 0 |
| Публикация | Шесть текущих локальных commits не pushed/deployed; production не объявляется равной локальному HEAD |

После прежнего checkpoint реализованы официальный источник смены (`assignments.0007`), структурированная смена участника состава (`settlement.0015`), независимое применение ночной и дневной смены, сменная идентичность фактического проживания (`settlement.0016`), две команды интерфейса и серверная классификация ручного проживания. Эти изменения находятся только в локальной цепочке из шести commits и не объявляются опубликованными или развёрнутыми в production.

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
| `EmployeeBedOccupancy` | Subject — `SettlementResident`; смена и неизменяемое происхождение ручных и автоматических записей; исторические строки без смены сохраняются | READY/PARTIAL |
| `SettlementResident` | Единый subject: internal wrapper Employee и внешняя карточка без Employee/Access/Role/PIN | READY |
| `AccommodationAnchorCalendarSlot/EmployeeAccommodationBinding` | M4 реализован; binding ссылается на resident, actor/audit остаются Employee | READY |
| `SettlementCohort/SettlementCohortMember` | M5 реализован; member ссылается на resident, хранит структурированную смену и доказуемое происхождение, APPROVED overlap проверяется по resident | READY |
| `build_auto_settlement_preview()` | Узкий GET-only in-memory preview по EquipmentAssignment | PARTIAL/CONTRADICTED |
| `settle_employee_on_bed()` | Атомарный ручной writer | PARTIAL |
| `relocate_employee_to_bed()` | Атомарный ручной перенос | PARTIAL |
| release | Досрочное прекращение через `terminated_at` | PARTIAL |
| Карта/панели/drawer/DnD | Рабочий ручной интерфейс | READY/PARTIAL |
| Применение предварительного плана | Один утверждённый план применяется независимо по ночной и дневной смене; отдельные Application и неизменяемая история | READY |
| `SettlementControlLease/SettlementControlEvent` | Schema, migration `0007` и bootstrap FREE singleton присутствуют в репозитории | PRESENT |
| Control lifecycle | Внутренние ensure/acquire/heartbeat/release/expire, HMAC session binding, token/fencing и audit events реализованы | PRESENT |
| Административный takeover | Внутренняя атомарная команда с обязательной причиной и fencing реализована | PRESENT |
| Settlement write gate | Ручные writers и каждое сменное применение требуют server-side control context и общий кадровый lock plan | READY |
| Control HTTP/session integration | Lifecycle и credentials привязаны к server-side session; browser payload не получает secrets | READY |
| Control UI integration | Панель управления, heartbeat, read-only режим и потеря lease реализованы; административный takeover остаётся отдельным контуром | READY/PARTIAL |

## 4. Фактическая модель данных

### 4.1. Physical fund

`PhysicalRoom` хранит общежитие, этаж, номер, тип, transfer status, sex restriction, capacity и координаты карты. `PhysicalBed` хранит room, stable ID, block A/B/ITR и position 1..3.

DB гарантирует уникальность комнаты и позиции койки, но не гарантирует равенство числа физических коек полю capacity.

`PhysicalBed.is_available` означает только переданность комнаты, а не фактическую незанятость.

### 4.2. AccommodationAnchor

Одна Equipment уже может иметь два и более active anchor: unique constraint на Equipment отсутствует. Поэтому для целевой пары не требуется ломать FK-cardinality.

Проблема находится в preview: он требует ровно один active equipment anchor и объявляет любое другое количество неоднозначностью.

Текущий anchor семантически является атомарным местом. M4 добавил `AccommodationAnchorCalendarSlot` для конкретного `WatchPeriod`; отдельная модель equipment pair по-прежнему отсутствует и полнота пары `An+Bn` остаётся задачей последующего контура.

### 4.3. Anchor-bed assignment

Штатный `save()` блокирует anchor, затем bed, выполняет `full_clean()` и запрещает interval overlap.

DB-level conditional unique покрывает только открытые confirmed записи `valid_to IS NULL`. Две пересекающиеся конечные confirmed записи можно создать в обход штатного `save()`.

Публичные `bulk_create`, `bulk_update` и `QuerySet.update` запрещены специализированным QuerySet с кодом `anchor_bed_mass_write_forbidden`; normal instance `save()` и draft delete сохранены. Private ORM API, raw SQL и внешний DB-клиент остаются техническими обходами. Удаление confirmed/cancelled записей защищено сигналом. DB-level finite interval overlap ещё не закрыт.

### 4.4. EmployeeBedOccupancy

Класс сохраняет legacy-имя, но обязательный subject — `resident` → `SettlementResident`; прямого subject-FK Employee больше нет. Поля включают physical bed, тип permanent/temporary/proposed, legacy `settled_at/ended_at` и canonical `starts_at/ends_at/terminated_at`. `settled_by` и другие actor/audit-поля остаются Employee.

Runtime использует единую полуоткрытую семантику:

`starts_at <= moment < min(ends_at, terminated_at)`.

Legacy `ended_at` в runtime activity больше не участвует.

После migration `0013` uniqueness, overlap и interval guards используют resident identity. DB-level защита всех конечных interval overlap по-прежнему неполна. Модель не имеет FK на cohort, anchor, binding, source, revision или run; отсутствуют reason, release actor и idempotency key. Публичные `update/bulk_create/bulk_update` запрещены специализированным QuerySet с кодом `employee_bed_occupancy_mass_write_forbidden`. Разрешённые `create/save/delete`, private ORM API, raw SQL и внешний DB-клиент всё ещё требуют дисциплины writer/следующих DB-гарантий.

### 4.5. CrewPlan и EquipmentAssignment

`CrewPlan` имеет work date, role, revision, status, version и publication metadata. `CrewPlanSlot` хранит equipment, shift, employee и `baseline_employee`.

`publish_crew_plan()` закрывает прежние и создаёт новые принятые `EquipmentAssignment` с `source_kind=deputy_published_plan` и точным `source_crew_plan_slot`. Миграция `assignments.0007_equipment_assignment_provenance` помечает старые и ручные назначения как `unverified` без угадывания происхождения.

Официальное происхождение нельзя установить через публичные `save`, `create`, `bulk_create`, `update` или `bulk_update`; доверенный внутренний путь публикации повторно проверяет сотрудника, технику, роль, смену и статус плана. История прежнего назначения не переписывается.

### 4.6. Вахта и приезд

В upstream уже существуют:

- `WatchComposition`;
- текущая `Employee.watch_composition` без истории;
- `WatchPeriod`;
- `EmployeeShift`;
- `RotationResponse` с arrival/departure/намерением.

M5 использует эти upstream-факты через отдельные `SettlementCohort/SettlementCohortMember`: cohort имеет версию, lifecycle и immutable provenance, а member хранит конкретного resident, конечный интервал участия, структурированную смену и её доказуемое происхождение. Для внутреннего жильца смена допускается только от единственного назначения `deputy_published_plan` с точным `source_crew_plan_slot`; для внешнего — от явного выбора делопроизводителя с точным Access и основанием. Исторические значения не угадываются.

### 4.7. SettlementResident и subject transition M4/M5

`SettlementResident` является единой identity расселения. Тип `EMPLOYEE` имеет защищённую связь с `Employee`; кадровые сведения и принадлежность к `WatchComposition` проверяются по Employee. Типы `CONTRACTOR`, `BUSINESS_TRIP`, `EXTERNAL_OTHER` не имеют Employee и не получают login, PIN, `Role`, `EmployeeAccess` или выдуманную корпоративную composition.

Migration `0011_resident_subject_transition` заменила subject `employee` на `resident` в `EmployeeAccommodationBinding` и `SettlementCohortMember`. Имена моделей сохранены; actor/audit-поля продолжают ссылаться на `Employee`. Binding overlap, correction и supersede, а также member uniqueness и overlapping APPROVED memberships используют `resident_id`.

Forward migration создаёт или переиспользует ровно один внутренний wrapper для существующих subject Employee и не создаёт `EmployeeAccess`. Reverse восстанавливает Employee subject только для internal resident и останавливается fail-closed при external subject. M4/M5 domain-команды используют полный порядок `Employee внутренних resident по PK → SettlementResident по PK с OF self → WatchPeriod → Cohort/Member → Slot/Binding → Anchor/Assignment/Bed → revalidation/write`.

Внешний resident уже может быть subject cohort member, calendar binding и фактического проживания через canonical resident API. Migration `0013_resident_occupancy_subject` переводит существующие Employee occupancy на internal wrappers без EmployeeAccess; forward fail-closed при external resident без authoritative sex, reverse — при external occupancy. M6, M7, применение по сменам и ручной сменный контур реализованы. Не реализованы: UI создания и выбора внешней карточки, полноценная отдельная плановая карта, точечные исправления будущего плана и trainee adapter.

Canonical occupancy API: `settle_resident_on_bed`, `relocate_resident_to_bed`, `release_resident_from_bed`. Employee-based функции — совместимые адаптеры внутреннего HTTP UI и не создают resident скрыто. Internal sex берётся только из `Employee.sex`; external resident обязан иметь `external_sex=male/female`. Unknown sex не обходит room guard, несовместимое изменение пола при открытой occupancy отклоняется, а изменение external sex влияет на M6 fingerprint и stale M7 preview.

### 4.8. Read-only resolver M6

`resolve_settlement_cohort(*, cohort_id)` возвращает immutable deterministic placements/unresolved и fingerprint без ORM writes. Для внутреннего resident порядок волн: `CONFIRMED binding → EquipmentAssignment → PersonnelPosition → controlled unresolved`.

Equipment route применяет официальный selection contract: `ACCEPTED`, заполненная `role`, отсутствующая `shift`, `assigned_at` не позже начала WatchPeriod, `accepted_at` отсутствует либо уже наступила, `ended_at` отсутствует либо ещё не наступила. Полная цепочка: `SettlementResident/Employee → EquipmentAssignment → active Equipment → единственный active equipment AccommodationAnchor → единственный effective CONFIRMED AnchorBedAssignment → CONFIRMED CalendarSlot того же WatchPeriod → PhysicalBed`.

Binding имеет приоритет над equipment route, equipment route — над position route. Множественные assignment/anchor/bed assignment не разрешаются первой строкой. DAY/NIGHT остаётся provenance/compatibility context и не создаёт две identity койки; равноправная конкуренция двух resident за одну bed возвращает `hard_rule_conflict`. Fingerprint включает assignment, equipment, anchor, anchor-bed assignment, slot и bed relations.

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

### Общая оценка исторического manual baseline

Штатные ручные writers атомарны, используют согласованный порядок Employee → Beds → Rooms → Occupancies и закрыты от публичных массовых ORM-изменений. Глобального DB interval-инварианта и полного typed COMMIT contract всё ещё нет; writer вызывает только SET-R033/SET-R034 overlap subset.

### Исторический runtime baseline до полного control-контура

Текущий `settlement_clerk_access_from_request()` проверяет session `employee_access_id`, активность `EmployeeAccess`/Employee/Role и допускает к settlement роли `settlement_clerk` и `admin`. `role_session_state()` ограничивает активную роль внутри одной сессии/сотрудника, но не между разными сотрудниками и устройствами. В schema уже существуют singleton `SettlementControlLease`, session hash, token и fencing revision, а внутренние lifecycle/takeover команды меняют их атомарно и создают `SettlementControlEvent`.

В том историческом runtime baseline HTTP-контур ещё не вызывал эти команды: manual POST напрямую запускал `settle_employee_on_bed()`, `relocate_employee_to_bed()` или `release_employee_from_bed()` без write gate. Control endpoints/URLs, хранение credentials в пользовательской session и UI тогда отсутствовали. Этот абзац не описывает текущий HEAD: control gate, exact resident/occupancy и сменная классификация ручных операций впоследствии реализованы.

Следствия для исторического runtime в implementation baseline `902e3886` (не описание текущего M8 HEAD):

- два разных делопроизводителя либо делопроизводитель и администратор могут одновременно пройти серверную авторизацию;
- две вкладки/два HTTP-запроса одного пользователя могут выполняться параллельно;
- кадровый график, `WatchComposition`, `WatchPeriod` и открытая смена не являются precondition ручного POST writer;
- в том историческом baseline background Apply отсутствовал и без общего control gate создал бы дополнительный конкурентный writer;
- schema/lifecycle/takeover доступны только как внутренний сервисный контур и пока не дают пользователю техническую исключительность управления.

## 8. Исторический UI/HTTP baseline до control UI v30 и M8

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
| Resident/M4/M5/migration SQLite | четыре целевых класса direct и `--reverse` | 57/57 PASS в каждом порядке | Не является полным settlement suite |
| Migration `0011` cycle | чистая временная SQLite DB: `0010 → 0011 → 0010 → 0011` | PASS | Временная БД удалена; production не затрагивалась |
| Migration drift | `makemigrations --check --dry-run` | Изменений нет | Leaf `settlement.0011_resident_subject_transition` соответствует model state |

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
| Authoritative SettlementCohort | READY | M5 cohort/member, lifecycle, immutable provenance и resident identity реализованы migration `0009/0011` |
| AccommodationAnchorCalendarSlot | READY | M4 concrete WatchPeriod slot, boundaries/stale/overlap guards реализованы migration `0008` |
| EmployeeAccommodationBinding | READY | Subject — `SettlementResident`; прямого subject-FK Employee нет; actor/audit остаются Employee |
| Initial-binding provenance snapshot | READY/PARTIAL | Binding хранит immutable basis/source snapshot; M8 применяет только сохранённую placement provenance, отдельное расширение создания binding остаётся вне текущего Apply |
| Конечная factual occupancy | READY/PARTIAL | Resident-based конечные интервалы и M8 AUTO/MANUAL provenance реализованы; глобальный DB interval constraint остаётся отдельным усилением |
| Room profiles | PARTIAL | transfer/sex/type есть; M6 умеет безопасно вычислить внешний residual pool только из подтверждённых M4 evidence; persistent режим и 2+1 отсутствуют |
| Rule 2+1 | ABSENT | Нет нормативно однозначной модели/конфигурации; read-only resolver возвращает `resolver_not_configured` |
| M6 resolver | READY/PARTIAL | Минимальное MVP-ядро COMPLETE: binding → equipment assignment → position → controlled unresolved; persistent RoomUseProfile/ResolverRule и специальные волны ещё отсутствуют |
| Explicit category assignments | ABSENT | Нет версионируемого назначения RESERVE и аналогичных категорий |
| Trainee structured-state route | ABSENT | Существующий authoritative trainee state/adapter не найден; Vacancy исключена ADR-030 |
| Authoritative SettlementResident | READY | Migration `0010_settlement_residents` ввела resident lifecycle; migration `0011_resident_subject_transition` перевела M4 binding и M5 member на resident identity без создания EmployeeAccess |
| Group capacity conflict | PARTIAL | M6 read-only resolver не выбирает равноправного resident при дефиците и возвращает всей непосредственно конфликтующей группе `equal_priority_conflict`; полный configured group resolver ещё отсутствует |
| Explicit KEEP/reuse | READY | Unchanged AUTO resident/bed/interval/WatchPeriod фиксируется ApplicationItem как `REUSED` без переписывания occupancy |
| Saved preview M7 | READY | `SettlementPreviewRun` с version и lifecycle `DRAFT → CONFIRMED → SUPERSEDED`; один CONFIRMED на WatchPeriod |
| Immutable run rows | READY | Отдельные `SettlementPreviewPlacement` и `SettlementPreviewUnresolved`, immutable provenance и public mass-mutation guards |
| Input hash/stale detection | READY | Resolver/normalized fingerprints, source snapshot, повторный M6 при confirmation и read-only stale helper |
| Транзакционное применение | READY | M8 атомарен; ночная и дневная смены применяются независимо через server-side команды и интерфейс |
| Idempotency | READY | Один M7 run имеет не более одного immutable Application на каждую смену; повтор возвращает прежний результат |
| Единственный активный управляющий | PARTIAL | Singleton lease, lifecycle и fencing реализованы внутренне; write gate, HTTP/session binding и server read-only enforcement отсутствуют |
| Административный takeover | PARTIAL | Внутренняя команда реализована; endpoint, UI подтверждения и fencing действующих manual writers отсутствуют |
| Единый полный COMMIT validator | PARTIAL | Подключены только overlap rules |
| Temporary manual exception | PARTIAL | Temporary/relocate/release есть; binding/reason нет |
| Permanent binding correction | READY | Correction/supersede реализованы по resident identity с сохранением истории |

## 11. Решение архитектора

Строить Apply поверх legacy GET preview запрещено. Backend M8 построен только поверх сохранённого `CONFIRMED` M7 run.

Сохраняются:

- physical fund и карта;
- source/revision;
- anchor как атомарная идентичность;
- anchor-bed history после усиления инвариантов;
- canonical occupancy interval;
- manual writers после унификации validator/locks/audit;
- CrewPlan, WatchComposition/WatchPeriod и RotationResponse как upstream-факты;
- UI-каркас, панели и DnD.

После завершения backend M8 отдельными конфигурационными/UI-этапами остаются:

1. усиление текущих interval/writer invariants;
2. два atomic equipment slots и validation пары `An+Bn`;
3. ~~calendar slots~~ — реализованы M4;
4. ~~permanent resident binding~~ — реализован M4 и переведён на `SettlementResident` migration `0011`;
5. ~~authoritative versioned cohort~~ — реализован M5 и переведён на `SettlementResident` migration `0011`;
6. snapshot первичного CrewPlan/EquipmentAssignment при создании binding;
7. явные category assignments и маршрут стажёра по настоящей должности через authoritative structured state/adapter;
8. room profiles, resolver rules и групповые конфликты дефицита.

Сохранённый M7-план подтверждается без изменения фактического проживания, а M8/HTTP/UI-контур атомарно применяет актуальный `CONFIRMED` план отдельно по ночной и дневной смене. Специальные M6-конфигурации продолжают возвращать controlled unresolved. Следующий активный этап ADR-031 — точечные исправления будущего плана и полноценная отдельная плановая карта следующего заезда.

### 11.1. Фактический M7

Migration leaf — `settlement.0012_m7_saved_previews`. Migration schema-only, зависит от `settlement.0011_resident_subject_transition`, не выполняет backfill и создаёт три M7-таблицы. Публичные API: `create_settlement_preview_run(*, cohort_id, control_context)`, `confirm_settlement_preview_run(*, run_id, control_context)` и `settlement_preview_is_stale(*, run_id)`.

Подтверждение требует exact server-side control context, повторяет M6 и fail-closed сравнивает fingerprints/snapshot/rows. Unresolved residents сохраняются с reason codes и допускаются в `CONFIRMED` preview. M7 сам не меняет occupancy, M4/M5, residents или физический фонд.

### 11.2. Фактический M8

Текущий migration leaf — `settlement.0016_shift_scoped_apply_and_occupancy`; он следует после `settlement.0015_cohort_member_work_shift`. Canonical API `apply_confirmed_settlement_preview(*, run_id, work_shift, control_context, confirm_replace_manual=False)` использует exact Access, повторно сверяет план, отпечатки и строки выбранной смены и выполняет один атомарный пакет.

Неизменившееся автоматическое проживание переиспользуется как `REUSED`; переселение, обмен местами и переход в unresolved заменяют только доказанную основу текущего состава, периода и выбранной смены. Ручная запись требует явного `confirm_replace_manual=True`; история не удаляется. Старый endpoint применения всего плана возвращает controlled HTTP 409. UI внешнего жильца не реализован.

### 11.3. Состояние ADR-031

Реализованы: официальный источник смены, структурированная смена участника M5–M7, независимое применение ночной и дневной смены, две календарно защищённые команды интерфейса, неизменяемая история применения и сменная идентичность ручного фактического проживания.

Не реализованы и не помечаются `COMPLETE`: точечные исправления поверх `CONFIRMED` плана, полноценная отдельная плановая карта «Следующий заезд», UI создания/поиска/выбора внешнего жильца, загрузка списка табельщика, модуль табельщика и reconciliation исторических записей фактического проживания без смены.

Отдельное состояние «Активирован» не вводится. Доказательством применения служат сменные Application-записи; после применения дальнейшие действия выполняются как фактическое ручное заселение, переселение или освобождение в режиме «Текущая вахта».
