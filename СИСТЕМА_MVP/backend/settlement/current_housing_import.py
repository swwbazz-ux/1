"""Preview and apply the clerk's КИС-5/КИС-6 housing workbook."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from hashlib import sha256
from io import BytesIO
import re
import unicodedata

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, transaction
from django.db.models import Q
from django.utils import timezone
from openpyxl import load_workbook

from core.production_time import production_day_bounds
from references.models import Dormitory
from rotations.arrival_roster_parser import MAX_FILE_SIZE, validate_xlsx_archive
from users.models import Employee, PersonnelPosition

from .control import (
    SettlementControlWriteContext, acquire_control_lease,
    lock_settlement_write_access, lock_settlement_write_lease,
)
from .intervals import effective_ends_at, intervals_overlap
from .models import (
    EmployeeBedOccupancy, PhysicalBed, PhysicalRoom, SettlementBedPreparation, SettlementBedReserve,
    SettlementPlacementEvent, SettlementResident,
)
from .personnel_categories import employee_settlement_category
from .residents import get_or_create_employee_resident
from .room_sex import resident_sex
from .services import (
    _create_occupancy, _locked_related_occupancies, _validate_occupancy_conflicts,
    _validate_resident_and_destination, validate_placement_input_contract,
)
from .special_fund import room_anchor_types_by_room


# Columns for employee, shift, check-in, check-out and comment in the supplied template.
LAYOUT = {5: (10, 11, 15, 16, 18), 6: (8, 9, 13, 14, 16)}
MAX_SHEET_ROWS = 6000
BED_LABEL = re.compile(r'(\d{1,5})([АБAB])-([1-3])', re.IGNORECASE)
NAME_PART = r'[А-ЯЁа-яё]+(?:-[А-ЯЁа-яё]+)*'
FULL_NAME = re.compile(fr'({NAME_PART})\s+({NAME_PART})\s+({NAME_PART})(?=\s|[,(.]|$)')
VACANT = re.compile(r'^(?:свобод?\w*|резерв|пусто|нет\s+места)(?:\W|$)', re.IGNORECASE)
REVIEW_NOTE = re.compile(r'\b(?:увол\w*|выбыл\w*|убыл\w*|выех\w*|съех\w*|пересел\w*|переех\w*|межвахт\w*)', re.IGNORECASE)
CATEGORY_LABELS = {'worker': 'Рабочий', 'itr': 'ИТР'}
WORK_SHIFTS = {'День': 'day', 'Ночь': 'night'}
APPLICABLE_GROUPS = ('ready', 'needs_resident')


@dataclass
class HousingRow:
    building: int
    room_number: int | None
    bed_label: str
    name: str
    name_key: str
    shift: str
    starts: date | None
    ends: date | None
    source: str
    interwatch: bool
    note: str
    block: str = ''
    position: int | None = None
    issues: list[str] = field(default_factory=list)
    floor: int | None = None
    bed_id: str = ''
    physical_bed_id: int | None = None
    employee_id: int | None = None
    category: str = ''
    needs_resident: bool = False
    status: str = ''

    @property
    def category_label(self):
        return CATEGORY_LABELS.get(self.category, '—')


def _text(value):
    return ' '.join(str(value if value is not None else '').split())


def _name_key(value):
    return _text(unicodedata.normalize('NFKC', value)).casefold().replace('ё', 'е')


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for pattern in ('%d.%m.%Y', '%Y-%m-%d'):
            try:
                return datetime.strptime(value.strip(), pattern).date()
            except ValueError:
                pass
    return None


def _interwatch(cell):
    color = cell.fill.fgColor
    if cell.fill.patternType != 'solid':
        return False
    if color.type == 'theme':
        return color.theme == 4 and abs(color.tint - .6) < .001
    return color.type == 'rgb' and color.rgb[-6:].upper() in {'D9E1F2', 'B4C6E7'}


def _room_rows(sheet):
    rooms = {
        number: value for number in range(3, sheet.max_row + 1)
        if type(value := sheet.cell(number, 2).value) is int and value > 0
    }
    for merged in sheet.merged_cells.ranges:
        if merged.min_col != 2 or merged.max_col != 2 or merged.min_row < 3:
            continue
        value = sheet.cell(merged.min_row, 2).value
        if type(value) is int and value > 0:
            rooms.update(dict.fromkeys(range(merged.min_row, merged.max_row + 1), value))
    return rooms


def _parse_sheet(sheet, building):
    if sheet.max_row > MAX_SHEET_ROWS:
        raise ValidationError(f'На листе «{sheet.title}» слишком много строк.')
    name_col, shift_col, start_col, end_col, note_col = LAYOUT[building]
    headers = ('номер кровати', 'ф и о сотрудника', 'смена', 'дата заселения', 'дата выезда')
    positions = (3, name_col, shift_col, start_col, end_col)
    actual = tuple(_text(_text(sheet.cell(2, col).value).casefold().replace('.', ' ')) for col in positions)
    if actual != headers:
        raise ValidationError(f'На листе «{sheet.title}» не распознаны колонки расселения.')

    rooms = _room_rows(sheet)
    if not rooms:
        raise ValidationError(f'На листе «{sheet.title}» не найдены номера комнат в колонке Б.')

    for number in range(3, sheet.max_row + 1):
        cell = sheet.cell(number, name_col)
        raw_name = _text(cell.value)
        if not raw_name or VACANT.match(raw_name):
            continue
        bed_label = _text(sheet.cell(number, 3).value).upper()
        bed_match = BED_LABEL.fullmatch(bed_label)
        room_number = rooms.get(number)
        if room_number is None and bed_match is None:
            # Ignore sheet legends and totals outside the housing grid.
            continue
        name_match = FULL_NAME.match(raw_name)
        name = ' '.join(name_match.groups()) if name_match else raw_name[:250]
        remainder = raw_name[name_match.end():].strip(' .,') if name_match else ''
        comment = _text(sheet.cell(number, note_col).value)
        shift = _text(sheet.cell(number, shift_col).value).casefold()
        row = HousingRow(
            building=building, room_number=room_number, bed_label=bed_label,
            name=name, name_key=_name_key(name) if name_match else '',
            shift={'день': 'День', 'ночь': 'Ночь'}.get(shift, shift[:35]),
            starts=_date(sheet.cell(number, start_col).value), ends=_date(sheet.cell(number, end_col).value),
            source=f'{sheet.title}!{cell.coordinate}', interwatch=_interwatch(cell),
            note=' '.join((remainder, comment))[:500],
        )
        if bed_match and room_number == int(bed_match[1]):
            row.block = {'А': 'A', 'Б': 'B'}.get(bed_match[2].upper(), bed_match[2].upper())
            row.position = int(bed_match[3])
        else:
            row.issues.append('Номер койки не соответствует комнате в Excel.')
        yield row


def parse_current_housing_workbook(payload):
    if not payload or len(payload) > MAX_FILE_SIZE:
        raise ValidationError('Выберите файл Excel размером до 10 МБ.')
    validate_xlsx_archive(payload)
    try:
        workbook = load_workbook(BytesIO(payload), data_only=True, keep_links=False)
    except Exception as error:
        raise ValidationError('Не удалось прочитать книгу Excel.') from error
    try:
        sheets = {}
        for sheet in workbook.worksheets:
            label = re.fullmatch(r'КИС[\s-]*([56])', _text(sheet.title), re.IGNORECASE)
            if not label:
                continue
            building = int(label[1])
            if building in sheets:
                raise ValidationError(f'Найдено несколько листов КИС-{building}.')
            sheets[building] = sheet
        if not sheets:
            raise ValidationError('В книге нужен лист «КИС 5» или «КИС 6» с текущей расстановкой.')
        rows = [row for building, sheet in sorted(sheets.items()) for row in _parse_sheet(sheet, building)]
        if not rows:
            raise ValidationError('В книге нет строк с жильцами.')
        return rows
    finally:
        workbook.close()


def _excel_conflicts(rows, as_of):
    active_names = defaultdict(list)
    active_beds = defaultdict(list)
    undated = defaultdict(list)
    interwatch = set()
    for row in rows:
        if row.interwatch:
            row.status = 'interwatch'
            if row.name_key:
                interwatch.add(row.name_key)
            continue
        key = (row.building, row.room_number, row.block, row.position)
        if not row.starts or not row.ends:
            row.issues.append('Нет одной или обеих дат проживания.')
            if row.block:
                undated[key].append(row)
        elif row.starts >= row.ends:
            row.issues.append('Дата выезда должна быть позже даты заселения.')
        elif not row.starts <= as_of < row.ends:
            row.status = 'outside'
            continue
        else:
            if row.name_key:
                active_names[row.name_key].append(row)
            if row.block:
                active_beds[key].append(row)
        if not row.name_key:
            row.issues.append('Уточните ФИО в Excel.')
        if row.shift not in ('День', 'Ночь'):
            row.issues.append('Уточните смену в Excel.')
        if REVIEW_NOTE.search(row.note):
            row.issues.append('В Excel есть пометка о кадровом изменении или перемещении.')

    for key, overlapping in active_beds.items():
        for row in overlapping:
            if len(overlapping) > 1:
                row.issues.append('На эту койку есть несколько записей на выбранную дату.')
            if any(other.name_key != row.name_key and {row.shift, other.shift} != {'День', 'Ночь'}
                   for other in undated[key]):
                row.issues.append('На эту койку есть строка без полного периода.')
    for name, matches in active_names.items():
        for row in matches:
            if len(matches) > 1:
                row.issues.append('Сотрудник повторяется в строках на выбранную дату.')
            if name in interwatch:
                row.issues.append('По этому же ФИО в Excel есть голубая строка межвахты.')


def _bed_for_row(room, row, by_position):
    if room.room_type == PhysicalRoom.RoomType.ITR:
        position = {('A', 1): 1, ('B', 2): 2}.get((row.block, row.position))
        return by_position.get((room.pk, 'ITR', position))
    return by_position.get((room.pk, row.block, row.position))


def _occupied_interval(occupancy):
    return occupancy.starts_at, effective_ends_at(occupancy.ends_at, occupancy.terminated_at)


def _check_occupancy(row, bed, employee, starts, ends, bed_occupancies, employee_occupancies, room_occupancies):
    same = False
    for occupancy in bed_occupancies:
        begins, finishes = _occupied_interval(occupancy)
        if not intervals_overlap(starts, ends, begins, finishes):
            continue
        if not employee or occupancy.resident.employee_id != employee.pk:
            row.issues.append('Койка занята другим жильцом в пересекающийся период.')
            break
        if (begins <= starts and (finishes is None or finishes >= ends)
                and occupancy.work_shift == WORK_SHIFTS.get(row.shift)):
            same = True
        else:
            row.issues.append('В системе уже есть запись на этой койке. Сверьте даты и смену.')
    if any(occupancy.physical_bed_id != bed.pk and intervals_overlap(starts, ends, *_occupied_interval(occupancy))
           for occupancy in employee_occupancies):
        row.issues.append('У сотрудника есть размещение на другой койке в этот период.')
    if employee:
        for occupancy in room_occupancies:
            resident = occupancy.resident
            if resident.employee_id == employee.pk or not intervals_overlap(starts, ends, *_occupied_interval(occupancy)):
                continue
            other_category = employee_settlement_category(resident.employee)
            if resident.employee_id and other_category != row.category:
                row.issues.append('Категория жильцов в комнате отличается или не указана.')
            if resident_sex(resident) != employee.sex:
                row.issues.append('Пол жильцов в комнате отличается или не указан.')
        row.issues = list(dict.fromkeys(row.issues))
    return same


def build_current_housing_preview(rows, as_of):
    _excel_conflicts(rows, as_of)
    rooms = defaultdict(list)
    room_query = PhysicalRoom.objects.filter(dormitory__kind=Dormitory.Kind.OWN, dormitory__number__in=['5', '6'])
    for room in room_query.select_related('dormitory'):
        rooms[int(room.dormitory.number), room.number].append(room)
    room_ids = {room.pk for matches in rooms.values() for room in matches}
    by_position = {
        (bed.room_id, bed.block, bed.position): bed
        for bed in PhysicalBed.objects.filter(room_id__in=room_ids)
    }
    anchors = room_anchor_types_by_room(room_ids=room_ids)
    selected_day_start, selected_day_end = production_day_bounds(as_of)
    held_beds = set(SettlementBedPreparation.objects.filter(
        physical_bed__room_id__in=room_ids, status=SettlementBedPreparation.Status.PREPARED,
        created_at__lt=selected_day_end, hold_until__gt=selected_day_start,
    ).values_list('physical_bed_id', flat=True))
    reserved_beds = set(SettlementBedReserve.objects.filter(
        physical_bed__room_id__in=room_ids, is_active=True,
    ).values_list('physical_bed_id', flat=True))
    roster = defaultdict(list)
    needed = {row.name_key for row in rows if not row.status and row.name_key}
    for person in Employee.objects.select_related('personnel_position', 'settlement_resident'):
        key = _name_key(person.full_name)
        if key in needed:
            roster[key].append(person)

    candidates = [row for row in rows if not row.status and row.starts and row.ends and row.starts <= as_of < row.ends]
    bed_use, room_use, employee_use = defaultdict(list), defaultdict(list), defaultdict(list)
    if candidates:
        first, _ = production_day_bounds(min(row.starts for row in candidates))
        last, _ = production_day_bounds(max(row.ends for row in candidates))
        employee_ids = [person.pk for people in roster.values() for person in people]
        relevant = EmployeeBedOccupancy.objects.filter(
            Q(physical_bed__room_id__in=room_ids) | Q(resident__employee_id__in=employee_ids),
            Q(ends_at__isnull=True) | Q(ends_at__gt=first),
            Q(terminated_at__isnull=True) | Q(terminated_at__gt=first),
            replaced_by_application__isnull=True, replaced_by_occupancy__isnull=True, starts_at__lt=last,
        ).select_related('physical_bed', 'resident__employee__personnel_position')
        for occupancy in relevant:
            begins, finishes = _occupied_interval(occupancy)
            if finishes is not None and finishes <= begins:
                continue
            bed_use[occupancy.physical_bed_id].append(occupancy)
            room_use[occupancy.physical_bed.room_id].append(occupancy)
            employee_use[occupancy.resident.employee_id].append(occupancy)

    matched_rooms = defaultdict(list)
    already_placed = set()
    for row in rows:
        options = rooms[row.building, row.room_number]
        room = options[0] if len(options) == 1 else None
        bed = _bed_for_row(room, row, by_position) if room else None
        row.floor = room.floor if room else None
        row.bed_id = bed.stable_id if bed else ''
        row.physical_bed_id = bed.pk if bed else None
        if row.status:
            continue
        if not room:
            row.issues.append('Комната отсутствует в жилфонде или номер встречается на нескольких этажах.')
        elif not room.dormitory.is_active or not room.is_transferred:
            row.issues.append('Комната не передана в распоряжение компании.')
        elif not bed:
            row.issues.append('Такой койки нет в жилфонде.')
        elif bed.pk in held_beds:
            row.issues.append('Койка подготовлена и удерживается до приезда жильца.')
        elif bed.pk in reserved_beds:
            row.issues.append('Койка находится в резерве.')
        if room and anchors.get(room.pk):
            row.issues.append('У комнаты есть специальное закрепление. Проверьте назначение жилфонда.')

        people = roster[row.name_key]
        employee = people[0] if len(people) == 1 else None
        if len(people) > 1:
            row.issues.append('По ФИО найдено несколько карточек. Нужен табельный номер.')
        elif employee and not (employee.is_active and employee.status == Employee.Status.ACTIVE):
            row.issues.append('Карточка сотрудника неактивна.')
            employee = None
        elif row.name_key and not employee:
            row.issues.append('Точное ФИО не найдено в справочнике сотрудников.')
        if employee:
            row.employee_id = employee.pk
            resident = getattr(employee, 'settlement_resident', None)
            row.needs_resident = resident is None
            if resident and resident.status != SettlementResident.Status.ACTIVE:
                row.issues.append('Карточка жильца в архиве. Проверьте её статус.')
            row.category = employee_settlement_category(employee) or ''
            if not row.category:
                row.issues.append('В справочнике должностей не указано «Рабочий / ИТР».')
            elif room and room.room_type == PhysicalRoom.RoomType.ITR and row.category == 'worker':
                row.issues.append('Это комната ИТР. Сотрудник относится к рабочим.')
            if employee.sex not in (Employee.Sex.MALE, Employee.Sex.FEMALE):
                row.issues.append('В карточке сотрудника не указан пол.')
        if bed and row.starts and row.ends and row.starts <= as_of < row.ends:
            starts, _ = production_day_bounds(row.starts)
            ends, _ = production_day_bounds(row.ends)
            if employee:
                matched_rooms[room.pk].append((row, employee))
            if _check_occupancy(row, bed, employee, starts, ends, bed_use[bed.pk],
                                employee_use[employee.pk] if employee else (), room_use[room.pk]):
                already_placed.add(row.source)

    for entries in matched_rooms.values():
        categories = {row.category for row, _ in entries if row.category}
        sexes = {employee.sex for _, employee in entries if employee.sex in (Employee.Sex.MALE, Employee.Sex.FEMALE)}
        for row, _ in entries:
            if len(categories) > 1 and row.category:
                row.issues.append('В одну комнату попадают и рабочие, и ИТР.')
            if len(sexes) > 1:
                row.issues.append('В одну комнату попадают жильцы разного пола.')

    groups = {key: [] for key in ('ready', 'needs_resident', 'conflict', 'interwatch', 'outside', 'existing')}
    for row in rows:
        if not row.status:
            if row.issues:
                row.status = 'conflict'
            elif row.source in already_placed:
                row.status = 'existing'
            elif row.needs_resident:
                row.status = 'needs_resident'
            else:
                row.status = 'ready'
        groups[row.status].append(row)
    return groups


def preview_placements(groups):
    """Identify the exact people and beds the clerk saw in the preview."""
    return {
        row.source: [row.employee_id, row.physical_bed_id]
        for group in APPLICABLE_GROUPS for row in groups[group]
    }


def _import_one_placement(row, *, bed, actor, workbook_digest, moment, using):
    starts, _ = production_day_bounds(row.starts)
    ends, _ = production_day_bounds(row.ends)
    if not starts <= moment < ends:
        raise ValidationError('Период строки не действует на момент применения.')

    resident, created = get_or_create_employee_resident(employee_id=row.employee_id, using=using)
    assignment_type = EmployeeBedOccupancy.AssignmentType.TEMPORARY
    source = f'Подтверждено при загрузке текущего расселения из Excel, {row.source}'
    _validate_resident_and_destination(
        resident=resident, room=bed.room, starts_at=starts, ends_at=ends,
        confirm_itr=True, placement_reason=source, using=using,
    )
    validate_placement_input_contract(
        resident=resident, bed_stable_id=bed.stable_id, assignment_type=assignment_type,
        starts_at=starts, ends_at=ends, checked_at=moment,
    )
    _validate_occupancy_conflicts(
        resident_id=resident.pk, bed=bed, starts_at=starts, ends_at=ends,
        persisted_occupancies=_locked_related_occupancies(resident_id=resident.pk, bed_ids=(bed.pk,), using=using),
    )
    occupancy = _create_occupancy(
        resident=resident, bed=bed, assignment_type=assignment_type, starts_at=starts,
        ends_at=ends, settled_by=actor.employee, work_shift=WORK_SHIFTS[row.shift],
        shift_source_kind=EmployeeBedOccupancy.ShiftSourceKind.CLERK_SELECTED,
        shift_source_fingerprint=sha256(f'{workbook_digest}:{row.source}'.encode()).hexdigest(),
        shift_selected_by_access=actor, shift_selected_at=moment, shift_selection_basis=source,
        using=using,
    )
    SettlementPlacementEvent.objects.using(using).create(
        event_type=SettlementPlacementEvent.EventType.TEMPORARY_CREATED,
        resident=resident, physical_bed=bed, actor_access=actor, reason=source,
        payload={
            'occupancy_id': occupancy.pk, 'workbook_sha256': workbook_digest,
            'source_row': row.source, 'resident_created': created,
        },
    )
    return created


def apply_current_housing(rows, as_of, *, confirmed, workbook_digest, access_id, session_key):
    # The normal control lease serializes this operation with map edits and other
    # clerk imports. Acquire it before locking the target employees so that all
    # employee rows are acquired in the standard lock order.
    grant = acquire_control_lease(
        owner_access_id=access_id, raw_session_key=session_key, source='current-housing-excel',
    )
    control = SettlementControlWriteContext(
        owner_access_id=access_id, raw_session_key=session_key,
        lease_token=grant.lease_token, fencing_revision=grant.fencing_revision,
    )
    groups, created_count = _apply_confirmed_housing(
        rows, as_of, confirmed=confirmed, workbook_digest=workbook_digest, control=control,
    )
    return groups, created_count, grant


@transaction.atomic
def _apply_confirmed_housing(rows, as_of, *, confirmed, workbook_digest, control):
    using = DEFAULT_DB_ALIAS
    lease = lock_settlement_write_lease(control_context=control, using=using)
    employee_ids = tuple(Employee.objects.filter(
        pk__in={employee_id for employee_id, _ in confirmed.values()},
    ).order_by('pk').values_list('pk', flat=True))
    locked = lock_settlement_write_access(
        lease=lease, control_context=control, employee_ids=employee_ids, using=using,
    )
    list(PersonnelPosition.objects.select_for_update().filter(
        pk__in=Employee.objects.filter(pk__in=employee_ids).values('personnel_position_id'),
    ).order_by('pk'))
    actor = locked.access_by_id(control.owner_access_id)

    # Housing-fund edits take the dormitory lock in their own service.
    list(Dormitory.objects.select_for_update().filter(number__in=['5', '6']).order_by('pk'))
    beds = {
        bed.pk: bed for bed in PhysicalBed.objects.select_for_update(of=('self',))
        .select_related('room')
        .filter(pk__in={bed_id for _, bed_id in confirmed.values()}).order_by('pk')
    }
    groups = build_current_housing_preview(rows, as_of)
    groups['applied'] = []
    created_count = 0
    moment = timezone.now()
    for group in APPLICABLE_GROUPS:
        for row in groups[group]:
            if confirmed.get(row.source) != [row.employee_id, row.physical_bed_id]:
                row.issues.append('Строка изменилась после предпросмотра. Проверьте Excel повторно.')
            else:
                try:
                    # A rejected row must not leave a newly created resident by itself.
                    with transaction.atomic():
                        created_count += _import_one_placement(
                            row, bed=beds[row.physical_bed_id], actor=actor,
                            workbook_digest=workbook_digest, moment=moment, using=using,
                        )
                except ValidationError as error:
                    row.issues.extend(error.messages)
            row.status = 'conflict' if row.issues else 'applied'
            groups[row.status].append(row)
        groups[group] = []
    return groups, created_count
