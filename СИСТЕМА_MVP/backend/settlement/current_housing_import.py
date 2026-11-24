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
from django.db.models import Prefetch, Q
from django.utils import timezone
from openpyxl import load_workbook

from core.production_time import business_localtime, production_day_bounds
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
from .residents import create_external_resident, get_or_create_employee_resident
from .room_sex import resident_sex
from .services import (
    _create_occupancy, _locked_related_occupancies, _validate_occupancy_conflicts,
    _validate_resident_state, _validate_room_sex, effective_occupancy_at_q,
    release_resident_from_bed, validate_placement_input_contract,
)
from .special_fund import room_anchor_types_by_room


# Columns for employee, shift, check-in, check-out and comment in the supplied template.
LAYOUT = {5: (10, 11, 15, 16, 18), 6: (8, 9, 13, 14, 16)}
MAX_SHEET_ROWS = 6000
BED_LABEL = re.compile(r'(\d{1,5})([АБAB])-([1-3])', re.IGNORECASE)
NAME_PART = r'[А-ЯЁа-яё]+(?:-[А-ЯЁа-яё]+)*'
FULL_NAME = re.compile(fr'{NAME_PART}\s+{NAME_PART}\s+{NAME_PART}(?:\s+(?i:оглы|кызы))?(?=\s|[,(.]|$)')
COMMENT_NAME = re.compile(r'[А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+')
COMMENT_INITIALS = re.compile(r'([А-ЯЁ][а-яё-]+)\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.')
VACANT = re.compile(r'^(?:свобод?\w*|резерв|пусто|нет\s+места)(?:\W|$)', re.IGNORECASE)
SHORT_DATE = r'\d{1,2}\.\d{1,2}(?:\.(?:\d{4}|\d{2})\b)?'
WORKBOOK_PERIOD = re.compile(fr'\bна\s+({SHORT_DATE})\.?\s+по\s+({SHORT_DATE})(?!\d)', re.IGNORECASE)
ARRIVAL_PREFIX = re.compile(
    fr'^(?:(?:заезд|заселиться(?:\s+(?:утром|вечером))?)\s+)?({SHORT_DATE})(?:\.\s*|\s+)(?=[А-ЯЁа-яё])',
    re.IGNORECASE,
)
ARRIVAL_NOTE = re.compile(fr'\b(?:заезд|заехал[аи]?|заселил(?:ся|ась)|приехал[аи]?)\s+({SHORT_DATE})', re.IGNORECASE)
DEPARTURE_NOTE = re.compile(
    fr'(?:\b(?:увольнение(?:\s+с)?|увол(?:ен[а]?|ился|илась|ьняется)(?:\s+с)?'
    fr'|уезжает|уедет|выехал[аи]?|съехал[аи]?|продление\)?\s+(?:по|до))'
    fr'|^\(?\s*до)\s+({SHORT_DATE})(?!\d|\.\d|\.?\s+в\b)',
    re.IGNORECASE,
)
OTHER_ROOM_NOTE = re.compile(
    r'\bна продлении (?:жив[её]т )?в\s+(?:КИС[\s-]*([56])\s+)?'
    r'(?:(?:комнате|комн\.?)\s+)?(\d{1,5})([АБAB])?\b',
    re.IGNORECASE,
)
ABSENCE_NOTE = re.compile(
    r'\b(?:пока\s+на\s+вахту\s+не\s+заезжает|межвахта|увол\w*|выехал[аи]?|съехал[аи]?)\b',
    re.IGNORECASE,
)
CATEGORY_LABELS = {'worker': 'Рабочий', 'itr': 'ИТР'}
WORK_SHIFTS = {'День': 'day', 'Ночь': 'night'}
APPLICABLE_GROUPS = ('ready', 'needs_resident')
BED_CONFLICT = 'На эту койку есть несколько записей на выбранную дату.'
PERSON_CONFLICT = 'Сотрудник повторяется в строках на выбранную дату.'
INTERWATCH_CONFLICT = 'По этому же ФИО в Excel есть голубая строка межвахты.'
OCCUPIED_BED_CONFLICT = 'Койка занята другим жильцом в пересекающийся период.'
OTHER_BED_CONFLICT = 'У сотрудника есть размещение на другой койке в этот период.'
BED_LABEL_MISMATCH = 'Номер койки не соответствует комнате в Excel.'
BED_MISSING = 'Такой койки нет в жилфонде.'
ROOM_UNAVAILABLE = 'Комната не передана в распоряжение компании.'
BED_HELD = 'Койка подготовлена и удерживается до приезда жильца.'
BED_RESERVED = 'Койка находится в резерве.'
ROOM_ANCHORED = 'У комнаты есть специальное закрепление. Проверьте назначение жилфонда.'
BED_PERIOD_CONFLICT = 'В системе уже есть запись на этой койке. Сверьте даты и смену.'
LIVE_ROOM_SEX_CONFLICT = 'Пол жильцов в комнате отличается или не указан.'
DRAFT_ROOM_SEX_CONFLICT = 'В одну комнату попадают жильцы разного пола.'
MANUAL_BED_BLOCKERS = {
    BED_MISSING, ROOM_UNAVAILABLE, BED_HELD, BED_RESERVED, ROOM_ANCHORED, BED_CONFLICT,
    OCCUPIED_BED_CONFLICT, OTHER_BED_CONFLICT, BED_PERIOD_CONFLICT,
    LIVE_ROOM_SEX_CONFLICT, DRAFT_ROOM_SEX_CONFLICT,
}

GROUPED_ISSUES = {
    BED_CONFLICT, PERSON_CONFLICT, INTERWATCH_CONFLICT, OCCUPIED_BED_CONFLICT, OTHER_BED_CONFLICT,
}


@dataclass(frozen=True)
class HousingMapMatch:
    name: str
    place: str
    starts: datetime
    ends: datetime | None
    shift: str


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
    comment: str = ''
    arrival_prefix: str = ''
    other_room: tuple[int, int, str] | None = None
    block: str = ''
    position: int | None = None
    issues: list[str] = field(default_factory=list)
    date_sources: dict[str, str] = field(default_factory=dict)
    floor: int | None = None
    bed_id: str = ''
    physical_bed_id: int | None = None
    reviewed_bed_id: int | None = None
    physical_room_id: int | None = None
    reviewed_place: str = ''
    excel_place: str = ''
    employee_id: int | None = None
    reviewed_employee: Employee | None = None
    reviewed_resident: SettlementResident | None = None
    external_draft: dict = field(default_factory=dict)
    sex: str = ''
    applied_resident_id: int | None = None
    match_identity: int | str = ''
    reviewed: bool = False
    marked_interwatch: bool = False
    interwatch_occupancy: EmployeeBedOccupancy | None = None
    confirmed_in_review: bool = False
    review_peers: set[str] = field(default_factory=set)
    review_interwatch: list['HousingRow'] = field(default_factory=list)
    review_map_matches: list[HousingMapMatch] = field(default_factory=list)
    review_source_note: str = ''
    review_notes: bool = False
    review_identity: bool = False
    review_dates: bool = False
    invalid_excel_dates: bool = False
    category: str = ''
    needs_resident: bool = False
    status: str = ''

    @property
    def placement_subject(self):
        if self.reviewed_resident:
            return f'resident:{self.reviewed_resident.pk}'
        if self.external_draft:
            return _external_draft_subject(self.external_draft)
        return self.employee_id

    @property
    def external_label(self):
        if self.reviewed_resident:
            return self.reviewed_resident.get_resident_type_display()
        if self.external_draft:
            return SettlementResident.ResidentType(self.external_draft['resident_type']).label
        return ''

    @property
    def category_label(self):
        return CATEGORY_LABELS.get(self.category, '—')

    @property
    def place(self):
        if self.reviewed_place:
            return self.reviewed_place
        parts = [f'КИС-{self.building}']
        if self.floor:
            parts.append(f'{self.floor} этаж')
        if self.room_number:
            parts.append(f'комната {self.room_number}')
        if self.bed_label:
            parts.append(f'койка {self.bed_label}')
        return ' · '.join(parts)

    @property
    def bed_key(self):
        return self.physical_bed_id or ((self.building, self.room_number, self.block, self.position) if self.block else None)

    @property
    def review_detail_issues(self):
        return [issue for issue in self.issues if issue not in GROUPED_ISSUES]

    @property
    def interwatch_release_place(self):
        bed = self.interwatch_occupancy.physical_bed
        return f'{_housing_room_label(bed.room)} · койка {_housing_bed_label(bed)}'


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


def _short_date(value, year):
    if not value:
        return None
    parts = list(map(int, value.split('.')))
    if len(parts) == 3:
        year = parts[2] + 2000 if len(value.rsplit('.', 1)[1]) == 2 else parts[2]
    try:
        return date(year, parts[1], parts[0])
    except ValueError:
        return None


def _workbook_end(filename, as_of):
    match = WORKBOOK_PERIOD.search(filename)
    if not match:
        return None
    for start_year in (as_of.year, as_of.year - 1):
        starts = _short_date(match[1], start_year)
        if not starts:
            continue
        ends = _short_date(match[2], starts.year)
        if ends and ends < starts and match[2].count('.') == 1:
            ends = _short_date(match[2], starts.year + 1)
        if ends and starts <= as_of < ends and (ends - starts).days <= 366:
            return ends
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
        prefix = ARRIVAL_PREFIX.match(raw_name)
        name_text = raw_name[prefix.end():] if prefix else raw_name
        name_match = FULL_NAME.match(name_text)
        name = name_match[0] if name_match else raw_name[:250]
        remainder = name_text[name_match.end():].strip(' .,') if name_match else ''
        comment = str(sheet.cell(number, note_col).value or '').strip()
        raw_starts = sheet.cell(number, start_col).value
        raw_ends = sheet.cell(number, end_col).value
        shift = _text(sheet.cell(number, shift_col).value).casefold()
        row = HousingRow(
            building=building, room_number=room_number, bed_label=bed_label,
            name=name, name_key=_name_key(name) if name_match else '',
            shift={'день': 'День', 'ночь': 'Ночь'}.get(shift, shift[:35]),
            starts=_date(raw_starts), ends=_date(raw_ends),
            source=f'{sheet.title}!{cell.coordinate}', interwatch=_interwatch(cell),
            note=remainder[:500], comment=comment[:500],
            arrival_prefix=prefix[1] if prefix else '',
        )
        if bed_match and room_number == int(bed_match[1]):
            row.block = {'А': 'A', 'Б': 'B'}.get(bed_match[2].upper(), bed_match[2].upper())
            row.position = int(bed_match[3])
        else:
            row.issues.append(BED_LABEL_MISMATCH)
        if (_text(raw_starts) and not row.starts) or (_text(raw_ends) and not row.ends):
            row.invalid_excel_dates = True
            row.review_dates = True
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


def _current_person_notes(row):
    # A comment naming the previous occupant does not describe the resident in
    # the name column. Split the remaining notes so a next-watch move is not
    # treated as a current move.
    yield row.note
    for part in re.split(fr'\s*(?:[;\n]|\s{{2,}}(?={COMMENT_NAME.pattern}))\s*', row.comment):
        part = _text(part)
        named = FULL_NAME.match(part) if COMMENT_NAME.match(part) else None
        if named and _name_key(named[0]) == row.name_key:
            yield part[named.end():]
        elif not named:
            initials = COMMENT_INITIALS.match(part)
            if not initials or _name_key(initials[1]) == row.name_key.partition(' ')[0]:
                yield part[initials.end():] if initials else part


def _note_date(value, as_of):
    dates = [_short_date(value, year) for year in (as_of.year, as_of.year - 1, as_of.year + 1)]
    return min(filter(None, dates), key=lambda day: abs(day - as_of), default=None)


def _resolve_row_dates(row, as_of):
    if row.confirmed_in_review:
        return
    notes = tuple(_current_person_notes(row))
    if row.starts is None:
        match = next((match for note in notes if (match := ARRIVAL_NOTE.search(note))), None)
        row.starts = _note_date(row.arrival_prefix or (match[1] if match else ''), as_of)

    departures = {
        day for note in notes for match in DEPARTURE_NOTE.finditer(note)
        if not OTHER_ROOM_NOTE.search(note) and (day := _note_date(match[1], as_of))
    }
    note_end = next(iter(departures)) if len(departures) == 1 else None
    if len(departures) > 1 and row.ends is None:
        row.review_dates = True
        row.review_source_note = next(note for note in notes if DEPARTURE_NOTE.search(note))
        row.issues.append('В Excel указано несколько дат выезда.')
    if row.ends is None:
        row.ends = note_end
    elif row.date_sources.get('ends') != 'clerk_review' and note_end and note_end <= as_of < row.ends:
        row.review_notes = True
        row.review_source_note = next(note for note in notes if DEPARTURE_NOTE.search(note))
        row.issues.append('Даты проживания и пометка о выезде противоречат друг другу.')
    move = next((match for note in notes if (match := OTHER_ROOM_NOTE.search(note))), None)
    if move:
        block = (move[3] or '').upper().translate(str.maketrans('АБ', 'AB'))
        destination = (int(move[1] or row.building), int(move[2]), block)
        if destination[:2] != (row.building, row.room_number) or (block and block != row.block):
            row.other_room = destination
            row.review_source_note = next(note for note in notes if OTHER_ROOM_NOTE.search(note))
    absent = next((note for note in notes if ABSENCE_NOTE.search(note) and not DEPARTURE_NOTE.search(note)), '')
    if absent:
        row.review_notes = True
        row.review_source_note = absent
        row.issues.append('В Excel есть пометка, что сотрудник выбыл или пока не заехал.')


def _mark_duplicate_rows(rows):
    same_place = defaultdict(list)
    for row in rows:
        if not row.status and not row.issues and not row.review_dates and not row.other_room and row.match_identity and row.bed_key:
            same_place[row.match_identity, row.bed_key].append(row)
    for matches in same_place.values():
        if len(matches) < 2:
            continue
        values = {
            field: {value for row in matches if (value := getattr(row, field))}
            for field in ('starts', 'ends', 'shift')
        }
        if any(len(alternatives) > 1 for alternatives in values.values()):
            continue
        primary = max(matches, key=lambda row: sum(bool(getattr(row, field)) for field in values))
        for field, alternatives in values.items():
            if alternatives:
                setattr(primary, field, next(iter(alternatives)))
        for row in matches:
            if row is not primary:
                row.status = 'duplicate'


def _excel_conflicts(rows, as_of, workbook_end):
    active_names = defaultdict(list)
    active_beds = defaultdict(list)
    interwatch = defaultdict(list)
    for row in rows:
        if row.status:
            continue
        if row.interwatch:
            row.status = 'interwatch'
            if row.name_key and (not row.starts or row.starts <= as_of) and (not row.ends or as_of < row.ends):
                interwatch[row.name_key].append(row)
            continue
        _resolve_row_dates(row, as_of)
        if row.starts and row.ends and row.starts >= row.ends:
            row.review_dates = True
            row.issues.append('Дата выезда должна быть позже даты заселения.')
        elif (row.starts and row.starts > as_of) or (row.ends and row.ends <= as_of):
            row.status = 'outside'

    _mark_duplicate_rows(rows)
    for row in rows:
        if row.status or not row.other_room:
            continue
        building, room, block = row.other_room
        if row.match_identity and any(
            not match.status and not match.other_room and match.match_identity == row.match_identity
            and (match.building, match.room_number) == (building, room)
            and (not block or match.block == block)
            for match in rows
        ):
            row.status = 'duplicate'
        else:
            row.review_notes = True
            row.issues.append('В Excel указано, что жилец на продлении в другой комнате.')
    for row in rows:
        if row.status:
            continue
        if row.match_identity and not row.other_room:
            active_names[row.match_identity].append(row)
        if row.bed_key and not row.other_room:
            active_beds[row.bed_key].append(row)
        if not row.ends and workbook_end:
            row.ends = workbook_end
            row.date_sources['end'] = 'workbook_filename'
        if not row.ends:
            row.review_dates = True
            row.issues.append('Нет даты выезда и периода в названии файла.')
        if not row.starts:
            # A current snapshot proves presence on the selected date only.
            row.starts = as_of
            row.date_sources['start'] = 'snapshot_date'
        if not row.name_key:
            row.review_identity = True
            row.issues.append('Уточните ФИО жильца.')
        if row.invalid_excel_dates:
            row.issues.append('Дата проживания в Excel не распознана.')

    for overlapping in active_beds.values():
        if len(overlapping) > 1:
            for row in overlapping:
                row.review_peers.update(other.source for other in overlapping if other is not row)
                row.issues.append(BED_CONFLICT)
    for matches in active_names.values():
        for row in matches:
            if len(matches) > 1:
                row.review_peers.update(other.source for other in matches if other is not row)
                row.issues.append(PERSON_CONFLICT)
            if row.name_key in interwatch and not row.confirmed_in_review:
                row.review_notes = True
                row.review_interwatch = interwatch[row.name_key]
                row.issues.append(INTERWATCH_CONFLICT)


def _bed_for_row(room, row, by_position):
    if room.room_type == PhysicalRoom.RoomType.ITR:
        position = {('A', 1): 1, ('B', 2): 2}.get((row.block, row.position))
        return by_position.get((room.pk, 'ITR', position))
    return by_position.get((room.pk, row.block, row.position))


def _housing_room_label(room):
    dormitory = room.dormitory
    building = f'КИС-{dormitory.number}' if dormitory.kind == Dormitory.Kind.OWN else str(dormitory)
    return f'{building} · {room.floor} этаж · комната {room.number}'


def _housing_bed_label(bed):
    return f'{bed.get_block_display()}-{bed.position}'


def _housing_rooms():
    return PhysicalRoom.objects.filter(dormitory__kind=Dormitory.Kind.OWN, dormitory__number__in=['5', '6'])


def _housing_bed_restrictions(room_ids, day_start, day_end):
    held = set(SettlementBedPreparation.objects.filter(
        physical_bed__room_id__in=room_ids, status=SettlementBedPreparation.Status.PREPARED,
        created_at__lt=day_end, hold_until__gt=day_start,
    ).values_list('physical_bed_id', flat=True))
    reserved = set(SettlementBedReserve.objects.filter(
        physical_bed__room_id__in=room_ids, is_active=True,
    ).values_list('physical_bed_id', flat=True))
    return held, reserved


def housing_review_map(groups, row, as_of):
    """Read-only room layout and the placements relevant to this review."""
    day_start, day_end = production_day_bounds(as_of)
    start = production_day_bounds(row.starts)[0] if row.starts else day_start
    end = production_day_bounds(row.ends)[0] if row.ends else day_end
    if not start <= day_start < end:
        start, end = day_start, day_end
    rooms = list(_housing_rooms().select_related('dormitory').prefetch_related(
        Prefetch('beds', queryset=PhysicalBed.objects.order_by('block', 'position')),
    ).order_by('dormitory__number', 'floor', 'number'))
    room_ids = [room.pk for room in rooms]
    anchors = room_anchor_types_by_room(room_ids=room_ids)
    held, reserved = _housing_bed_restrictions(room_ids, day_start, day_end)
    occupancies = defaultdict(list)
    for occupancy in EmployeeBedOccupancy.objects.filter(
        Q(ends_at__isnull=True) | Q(ends_at__gt=start),
        Q(terminated_at__isnull=True) | Q(terminated_at__gt=start),
        physical_bed__room_id__in=room_ids, starts_at__lt=end,
        replaced_by_application__isnull=True, replaced_by_occupancy__isnull=True,
    ).select_related(
        'resident__employee__personnel_position',
        'shift_official_assignment__equipment__equipment_type',
        'shift_official_assignment__equipment__model',
    ).order_by('starts_at', 'pk'):
        begins, finishes = _occupied_interval(occupancy)
        if finishes is None or finishes > begins:
            occupancies[occupancy.physical_bed_id].append((occupancy, finishes))
    draft_beds = defaultdict(list)
    for group in ('ready', 'needs_resident', 'conflict'):
        for entry in groups[group]:
            if (entry.physical_bed_id and entry.source != row.source
                    and not entry.other_room and entry.starts and entry.ends
                    and entry.starts <= as_of < entry.ends
                    and (not row.match_identity or entry.match_identity != row.match_identity)):
                draft_beds[entry.physical_bed_id].append(entry.name)

    now = timezone.now()
    map_time = now if day_start <= now < day_end else day_start

    def bed_payload(bed, room_available):
        live = occupancies[bed.pk]
        already_here = bool(live) and row.placement_subject and all(
            _occupancy_subject(occupancy) == row.placement_subject
            and occupancy.starts_at <= start
            and (finish is None or finish >= end)
            and occupancy.work_shift == WORK_SHIFTS.get(row.shift)
            for occupancy, finish in live
        )
        draft_people = draft_beds[bed.pk]
        bed.active_occupancies = [
            occupancy for occupancy, finish in live
            if occupancy.starts_at <= map_time and (finish is None or map_time < finish)
        ]
        if not room_available:
            status, label = 'unavailable', 'Комната недоступна'
        elif bed.pk in held or bed.pk in reserved:
            status, label = 'unavailable', 'Койка в резерве'
        elif live and not already_here:
            status, label = 'occupied', 'Занята в этот период'
        elif draft_people:
            status, label = 'draft', 'Указана в Excel за другим жильцом'
        elif already_here:
            status, label = 'free', 'Жилец уже на этой койке'
        else:
            status, label = 'free', 'Свободна'
        bed.review_badge = ''
        if status == 'draft':
            bed.review_badge = 'Excel'
        elif room_available and status == 'unavailable':
            bed.review_badge = 'Резерв'
        elif status == 'occupied' and not bed.active_occupancies:
            bed.review_badge = 'Занята по датам'
        return {
            'id': bed.pk, 'label': _housing_bed_label(bed),
            'status': status, 'status_label': label,
            'residents': [
                {'id': occupancy.pk, 'name': occupancy.resident.display_name,
                 'starts': business_localtime(occupancy.starts_at).strftime('%d.%m.%Y'),
                 'ends': business_localtime(finishes).strftime('%d.%m.%Y') if finishes else '',
                 'shift': next((name for name, code in WORK_SHIFTS.items() if code == occupancy.work_shift), '')}
                for occupancy, finishes in live
            ],
            'draft': draft_people,
        }

    for room in rooms:
        room.anchor_types = anchors.get(room.pk, frozenset())

    catalog = [{
        'id': room.pk, 'label': _housing_room_label(room), 'number': room.number,
        'dormitory': room.dormitory.number, 'floor': room.floor,
        'beds': [bed_payload(bed, room.dormitory.is_active and room.is_transferred and not room.anchor_types)
                 for bed in room.beds.all()],
    } for room in rooms]
    return rooms, catalog, map_time


def _occupied_interval(occupancy):
    return occupancy.starts_at, effective_ends_at(occupancy.ends_at, occupancy.terminated_at)


def _add_map_match(row, occupancy, begins, finishes):
    bed = occupancy.physical_bed
    match = HousingMapMatch(
        occupancy.resident.display_name,
        f'{_housing_room_label(bed.room)} · койка {_housing_bed_label(bed)}',
        business_localtime(begins),
        business_localtime(finishes) if finishes else None,
        next((label for label, shift in WORK_SHIFTS.items() if shift == occupancy.work_shift), ''),
    )
    if match not in row.review_map_matches:
        row.review_map_matches.append(match)


def _occupancy_subject(occupancy):
    return occupancy.resident.employee_id or f'resident:{occupancy.resident_id}'


def interwatch_occupancy_id(row):
    if row.reviewed_resident:
        subject = Q(resident_id=row.reviewed_resident.pk)
    elif row.employee_id:
        subject = Q(resident__employee_id=row.employee_id)
    else:
        return None
    active = list(EmployeeBedOccupancy.objects.filter(
        subject, effective_occupancy_at_q(timezone.now()),
    ).order_by('pk').values_list('pk', flat=True)[:2])
    if len(active) > 1:
        raise ValidationError('На карте несколько действующих размещений жильца. Уточните, какое место освободить.')
    return active[0] if active else None


def _check_occupancy(row, bed, starts, ends, bed_occupancies, subject_occupancies, room_occupancies):
    same = False
    for occupancy in bed_occupancies:
        begins, finishes = _occupied_interval(occupancy)
        if not intervals_overlap(starts, ends, begins, finishes):
            continue
        if _occupancy_subject(occupancy) != row.placement_subject:
            row.issues.append(OCCUPIED_BED_CONFLICT)
            _add_map_match(row, occupancy, begins, finishes)
            continue
        if (begins <= starts and (finishes is None or finishes >= ends)
                and occupancy.work_shift == WORK_SHIFTS.get(row.shift)):
            same = True
        else:
            row.issues.append(BED_PERIOD_CONFLICT)
            _add_map_match(row, occupancy, begins, finishes)
    for occupancy in subject_occupancies:
        begins, finishes = _occupied_interval(occupancy)
        if occupancy.physical_bed_id != bed.pk and intervals_overlap(starts, ends, begins, finishes):
            row.issues.append(OTHER_BED_CONFLICT)
            _add_map_match(row, occupancy, begins, finishes)
    if row.sex:
        for occupancy in room_occupancies:
            resident = occupancy.resident
            if _occupancy_subject(occupancy) == row.placement_subject or not intervals_overlap(starts, ends, *_occupied_interval(occupancy)):
                continue
            if resident_sex(resident) != row.sex:
                row.issues.append(LIVE_ROOM_SEX_CONFLICT)
    row.issues = list(dict.fromkeys(row.issues))
    return same


def _external_draft_subject(values):
    key = f"{_name_key(values['full_name'])}|{_name_key(values['organization'])}"
    return f"external:{sha256(key.encode()).hexdigest()}"


def external_housing_card_exists(values):
    # Also detect archived cards so that the clerk can restore one instead of duplicating it.
    name = _name_key(values['full_name'])
    organization = _name_key(values['organization'])
    phone = re.sub(r'\D', '', values['phone'])
    return any(
        _name_key(card_name) == name and (
            _name_key(card_org) == organization or (phone and re.sub(r'\D', '', card_phone) == phone)
        )
        for card_name, card_org, card_phone in SettlementResident.objects.filter(employee_id__isnull=True)
        .values_list('full_name', 'organization', 'phone').iterator()
    )


def search_housing_subjects(query):
    """Search the directory across departments and the existing external cards."""
    tokens = _name_key(query).split()
    if sum(map(len, tokens)) < 2 or len(query) > 100:
        return []
    employees = Employee.objects.filter(is_active=True, status=Employee.Status.ACTIVE).order_by(
        'full_name', 'pk',
    ).values_list('pk', 'full_name', 'personnel_department__name', 'department',
                  'personnel_position__name', 'position', 'personnel_number')
    cards = SettlementResident.objects.filter(employee_id__isnull=True, status='ACTIVE').order_by(
        'full_name', 'pk',
    ).values_list('pk', 'full_name', 'organization', 'position_title', 'resident_type')
    def relevance(full_name):
        name = _name_key(full_name)
        surname = name.split(' ', 1)[0]
        if name == ' '.join(tokens):
            return 0
        if surname == tokens[0]:
            return 1
        return 2 if surname.startswith(tokens[0]) else 3

    results = []
    for pk, name, directory_department, department, directory_position, position, personnel_number in employees.iterator():
        details = ' · '.join(filter(None, (directory_department or department, directory_position or position)))
        if all(token in _name_key(f'{name} {details} {personnel_number or ""}') for token in tokens):
            results.append({'id': f'employee:{pk}', 'name': name, 'details': details, 'kind': 'Сотрудник'})
    for pk, name, organization, position, resident_type in cards.iterator():
        details = ' · '.join(filter(None, (organization, position)))
        if all(token in _name_key(f'{name} {details}') for token in tokens):
            results.append({
                'id': f'resident:{pk}', 'name': name, 'details': details,
                'kind': SettlementResident.ResidentType(resident_type).label,
            })
    return sorted(results, key=lambda item: relevance(item["name"]))[:24]


def _apply_review_decisions(rows, review):
    excluded = {source for decision in review.values() for source in decision.get('peers', ())}
    interwatch = {source for decision in review.values() for source in decision.get('interwatch_rows', ())}
    employees = Employee.objects.select_related('personnel_position', 'settlement_resident').in_bulk(
        [decision['employee_id'] for decision in review.values() if 'employee_id' in decision],
    )
    residents = SettlementResident.objects.filter(employee_id__isnull=True, status='ACTIVE').in_bulk(
        [decision['resident_id'] for decision in review.values() if 'resident_id' in decision],
    )
    for row in rows:
        decision = review.get(row.source, {})
        row.reviewed = bool(decision)
        if row.source in interwatch:
            row.marked_interwatch = True
            row.reviewed = 'interwatch_rows' in decision
            row.status = 'interwatch'
        elif row.source in excluded or decision.get('omit'):
            row.status = 'omitted'
            continue
        row.confirmed_in_review = decision.get('confirm', False)
        row.reviewed_bed_id = decision.get('bed_id')
        if row.reviewed_bed_id and BED_LABEL_MISMATCH in row.issues:
            row.issues.remove(BED_LABEL_MISMATCH)
        if 'employee_id' in decision:
            row.reviewed_employee = employees.get(decision['employee_id'])
            if row.reviewed_employee:
                row.name = row.reviewed_employee.full_name
                row.name_key = _name_key(row.name)
            else:
                row.issues.append('Выбранная карточка сотрудника больше недоступна.')
        elif 'resident_id' in decision:
            row.reviewed_resident = residents.get(decision['resident_id'])
            if row.reviewed_resident:
                row.name = row.reviewed_resident.full_name
                row.name_key = _name_key(row.name)
            else:
                row.issues.append('Выбранная карточка жильца больше недоступна.')
                row.review_identity = True
        elif 'external' in decision:
            row.external_draft = decision['external']
            row.name = row.external_draft['full_name']
            row.name_key = _name_key(row.name)
            if external_housing_card_exists(row.external_draft):
                row.issues.append('Такая карточка жильца уже есть. Найдите её по ФИО или восстановите из архива.')
                row.review_identity = True
        for field in ('starts', 'ends'):
            if field in decision:
                setattr(row, field, date.fromisoformat(decision[field]))
                row.date_sources[field] = 'clerk_review'
        if 'starts' in decision and 'ends' in decision:
            row.review_dates = False
            row.invalid_excel_dates = False


def build_current_housing_preview(rows, as_of, *, filename='', review=None):
    if review:
        _apply_review_decisions(rows, review)
    roster = defaultdict(list)
    needed = {row.name_key for row in rows if not row.status and row.name_key}
    for person in Employee.objects.select_related('personnel_position', 'settlement_resident'):
        key = _name_key(person.full_name)
        if key in needed:
            roster[key].append(person)
    for row in rows:
        matches = roster.get(row.name_key, ())
        employee = row.reviewed_employee or (matches[0] if len(matches) == 1 else None)
        row.match_identity = row.placement_subject or (employee.pk if employee else row.name_key)
    rooms = defaultdict(list)
    rooms_by_id = {}
    for room in _housing_rooms().select_related('dormitory'):
        rooms[int(room.dormitory.number), room.number].append(room)
        rooms_by_id[room.pk] = room
    room_ids = set(rooms_by_id)
    beds = PhysicalBed.objects.filter(room_id__in=room_ids).in_bulk()
    by_position = {(bed.room_id, bed.block, bed.position): bed for bed in beds.values()}
    row_places = {}
    for row in rows:
        if row.reviewed_bed_id:
            bed = beds.get(row.reviewed_bed_id)
            room = rooms_by_id[bed.room_id] if bed else None
            if bed:
                row.excel_place = row.place
                row.reviewed_place = f'{_housing_room_label(room)} · койка {_housing_bed_label(bed)}'
                row.bed_label = _housing_bed_label(bed)
        else:
            options = rooms[row.building, row.room_number]
            room = options[0] if len(options) == 1 else None
            bed = _bed_for_row(room, row, by_position) if room else None
        row.floor = room.floor if room else None
        row.physical_room_id = room.pk if room else None
        row.bed_id = bed.stable_id if bed else ''
        row.physical_bed_id = bed.pk if bed else None
        row_places[row.source] = room, bed
    _excel_conflicts(rows, as_of, _workbook_end(filename, as_of))
    anchors = room_anchor_types_by_room(room_ids=room_ids)
    selected_day_start, selected_day_end = production_day_bounds(as_of)
    held_beds, reserved_beds = _housing_bed_restrictions(room_ids, selected_day_start, selected_day_end)
    candidates = [row for row in rows if not row.status and not row.other_room and row.ends and row.starts <= as_of < row.ends]
    bed_use, room_use, subject_use = defaultdict(list), defaultdict(list), defaultdict(list)
    if candidates:
        first, _ = production_day_bounds(min(row.starts for row in candidates))
        last, _ = production_day_bounds(max(row.ends for row in candidates))
        employee_ids = [person.pk for people in roster.values() for person in people]
        external_ids = [row.reviewed_resident.pk for row in candidates if row.reviewed_resident]
        relevant = EmployeeBedOccupancy.objects.filter(
            Q(physical_bed__room_id__in=room_ids) | Q(resident__employee_id__in=employee_ids)
            | Q(resident_id__in=external_ids),
            Q(ends_at__isnull=True) | Q(ends_at__gt=first),
            Q(terminated_at__isnull=True) | Q(terminated_at__gt=first),
            replaced_by_application__isnull=True, replaced_by_occupancy__isnull=True, starts_at__lt=last,
        ).select_related('physical_bed__room__dormitory', 'resident__employee__personnel_position')
        for occupancy in relevant:
            begins, finishes = _occupied_interval(occupancy)
            if finishes is not None and finishes <= begins:
                continue
            bed_use[occupancy.physical_bed_id].append(occupancy)
            room_use[occupancy.physical_bed.room_id].append(occupancy)
            subject_use[_occupancy_subject(occupancy)].append(occupancy)

    matched_rooms = defaultdict(list)
    already_placed = set()
    for row in rows:
        room, bed = row_places[row.source]
        if row.status:
            continue
        if row.reviewed_bed_id and not bed:
            row.issues.append(BED_MISSING)
        elif not room:
            row.issues.append('Комната отсутствует в жилфонде или номер встречается на нескольких этажах.')
        elif not room.dormitory.is_active or not room.is_transferred:
            row.issues.append(ROOM_UNAVAILABLE)
        elif not bed:
            row.issues.append(BED_MISSING)
        elif bed.pk in held_beds:
            row.issues.append(BED_HELD)
        elif bed.pk in reserved_beds:
            row.issues.append(BED_RESERVED)
        if room and anchors.get(room.pk):
            row.issues.append(ROOM_ANCHORED)

        people = [row.reviewed_employee] if row.reviewed_employee else roster[row.name_key]
        if row.external_label:
            people = []
            row.needs_resident = bool(row.external_draft)
            row.sex = row.reviewed_resident.external_sex if row.reviewed_resident else row.external_draft['sex']
        employee = people[0] if len(people) == 1 else None
        if len(people) > 1:
            row.review_identity = True
            row.issues.append('По ФИО найдено несколько карточек. Выберите человека по подразделению или должности.')
        elif employee and not (employee.is_active and employee.status == Employee.Status.ACTIVE):
            row.issues.append('Карточка сотрудника неактивна.')
            employee = None
        elif row.name_key and not employee and not row.external_label:
            row.review_identity = True
            row.issues.append('По этому ФИО нет точного совпадения. Найдите человека или оформите карточку жильца.')
        if employee:
            row.employee_id = employee.pk
            row.sex = employee.sex
            resident = getattr(employee, 'settlement_resident', None)
            row.needs_resident = resident is None
            if resident and resident.status != SettlementResident.Status.ACTIVE:
                row.issues.append('Карточка жильца в архиве. Проверьте её статус.')
            row.category = employee_settlement_category(employee) or ''
            if employee.sex not in (Employee.Sex.MALE, Employee.Sex.FEMALE):
                row.issues.append('В карточке сотрудника не указан пол.')
        if bed and not row.other_room and row.ends and row.starts <= as_of < row.ends:
            starts, _ = production_day_bounds(row.starts)
            ends, _ = production_day_bounds(row.ends)
            if row.sex:
                matched_rooms[room.pk].append(row)
            if _check_occupancy(row, bed, starts, ends, bed_use[bed.pk],
                                subject_use[row.placement_subject], room_use[room.pk]):
                already_placed.add(row.source)

    for entries in matched_rooms.values():
        sexes = {row.sex for row in entries if row.sex in (Employee.Sex.MALE, Employee.Sex.FEMALE)}
        for row in entries:
            if len(sexes) > 1:
                row.issues.append(DRAFT_ROOM_SEX_CONFLICT)

    groups = {key: [] for key in (
        'ready', 'needs_resident', 'conflict', 'interwatch', 'outside', 'duplicate', 'omitted', 'existing',
    )}
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
    if review:
        release_ids = {
            decision['interwatch_occupancy_id'] for decision in review.values()
            if decision.get('interwatch_rows') and decision.get('interwatch_occupancy_id')
        }
        if release_ids:
            active = EmployeeBedOccupancy.objects.filter(
                effective_occupancy_at_q(timezone.now()),
            ).select_related('physical_bed__room__dormitory').in_bulk(release_ids)
            for row in groups['interwatch']:
                row.interwatch_occupancy = active.get(review.get(row.source, {}).get('interwatch_occupancy_id'))
    return groups


REVIEW_LABELS = {
    'person': 'Указано несколько мест',
    'bed': 'Несколько жильцов на койку',
    'linked': 'Пересекаются места и жильцы',
    'interwatch': 'Проживание или межвахта',
    'map': 'Расхождение с картой',
    'identity': 'Уточнить сотрудника',
    'dates': 'Уточнить даты',
    'notes': 'Пометка в Excel',
    'other': 'Уточнить размещение',
}


def housing_review_cases(conflicts):
    by_source = {row.source: row for row in conflicts}
    order = {source: index for index, source in enumerate(by_source)}
    visited = set()
    cases = []
    for row in conflicts:
        if row.source in visited:
            continue
        pending = [row.source]
        entries = []
        while pending:
            source = pending.pop()
            if source in visited:
                continue
            visited.add(source)
            selected = by_source[source]
            entries.append(selected)
            pending.extend(peer for peer in selected.review_peers if peer in by_source and peer not in visited)
        entries.sort(key=lambda entry: order[entry.source])
        if len(entries) > 1:
            if row.match_identity and all(entry.match_identity == row.match_identity for entry in entries):
                kind = 'person'
            elif row.physical_bed_id and all(entry.physical_bed_id == row.physical_bed_id for entry in entries):
                kind = 'bed'
            else:
                kind = 'linked'
        elif row.review_interwatch:
            kind = 'interwatch'
        elif row.review_map_matches:
            kind = 'map'
        elif row.review_identity:
            kind = 'identity'
        elif row.review_dates:
            kind = 'dates'
        elif row.review_notes:
            kind = 'notes'
        else:
            kind = 'other'
        cases.append({
            'kind': kind, 'label': REVIEW_LABELS[kind], 'first': row, 'entries': entries,
            'interwatch_choice': kind == 'interwatch' and row.review_notes and not row.review_dates,
        })
    return cases


def preview_placements(groups):
    """Identify the exact people and beds the clerk saw in the preview."""
    return {
        row.source: [row.placement_subject, row.physical_bed_id]
        for group in APPLICABLE_GROUPS for row in groups[group]
    }


def preview_releases(groups):
    return {
        row.source: [row.interwatch_occupancy.pk, row.interwatch_occupancy.physical_bed_id]
        for row in groups['interwatch'] if row.interwatch_occupancy
    }


def _import_one_placement(row, *, bed, actor, control, workbook_digest, moment, using):
    starts, _ = production_day_bounds(row.starts)
    ends, _ = production_day_bounds(row.ends)
    if not starts <= moment < ends:
        raise ValidationError('Период строки не действует на момент применения.')

    created = False
    if row.reviewed_resident:
        resident = row.reviewed_resident
    elif row.external_draft:
        if external_housing_card_exists(row.external_draft):
            raise ValidationError('Карточка жильца уже есть. Выберите её по ФИО.')
        resident = create_external_resident(**row.external_draft, control_context=control, using=using)
        created = True
    else:
        resident, created = get_or_create_employee_resident(employee_id=row.employee_id, using=using)
    assignment_type = EmployeeBedOccupancy.AssignmentType.TEMPORARY
    source = f'Подтверждено при загрузке текущего расселения из Excel, {row.source}'
    # The locked preview checks the transfer, anchors, reserves and holds. This
    # import records actual occupancy without applying category rules for new placements.
    _validate_resident_state(resident=resident)
    _validate_room_sex(
        resident=resident, room=bed.room, starts_at=starts, ends_at=ends, using=using,
    )
    validate_placement_input_contract(
        resident=resident, bed_stable_id=bed.stable_id, assignment_type=assignment_type,
        starts_at=starts, ends_at=ends, checked_at=moment,
    )
    _validate_occupancy_conflicts(
        resident_id=resident.pk, bed=bed, starts_at=starts, ends_at=ends,
        persisted_occupancies=_locked_related_occupancies(resident_id=resident.pk, bed_ids=(bed.pk,), using=using),
    )
    shift = WORK_SHIFTS.get(row.shift)
    shift_evidence = {
        'shift_source_kind': EmployeeBedOccupancy.ShiftSourceKind.CLERK_SELECTED,
        'shift_source_fingerprint': sha256(f'{workbook_digest}:{row.source}'.encode()).hexdigest(),
        'shift_selected_by_access': actor,
        'shift_selected_at': moment,
        'shift_selection_basis': source,
    } if shift else {}
    occupancy = _create_occupancy(
        resident=resident, bed=bed, assignment_type=assignment_type, starts_at=starts,
        ends_at=ends, settled_by=actor.employee, work_shift=shift, using=using, **shift_evidence,
    )
    SettlementPlacementEvent.objects.using(using).create(
        event_type=SettlementPlacementEvent.EventType.TEMPORARY_CREATED,
        resident=resident, physical_bed=bed, actor_access=actor, reason=source,
        payload={
            'occupancy_id': occupancy.pk, 'workbook_sha256': workbook_digest,
            'source_row': row.source, 'resident_created': created,
            'date_sources': row.date_sources,
            'reviewed_by_clerk': row.reviewed,
            'bed_selected_by_clerk': bool(row.reviewed_bed_id),
        },
    )
    row.applied_resident_id = resident.pk
    return created


def apply_current_housing(
    rows, as_of, *, confirmed, confirmed_releases, workbook_digest, access_id, session_key, filename='', review=None,
):
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
        rows, as_of, confirmed=confirmed, workbook_digest=workbook_digest, control=control, filename=filename,
        review=review, confirmed_releases=confirmed_releases,
    )
    return groups, created_count, grant


@transaction.atomic
def _apply_confirmed_housing(rows, as_of, *, confirmed, confirmed_releases, workbook_digest, control, filename, review):
    using = DEFAULT_DB_ALIAS
    lease = lock_settlement_write_lease(control_context=control, using=using)
    release_ids = {occupancy_id for occupancy_id, _ in confirmed_releases.values()}
    release_employees = EmployeeBedOccupancy.objects.filter(
        pk__in=release_ids, resident__employee_id__isnull=False,
    ).values_list('resident__employee_id', flat=True)
    employee_ids = tuple(Employee.objects.filter(
        Q(pk__in={subject for subject, _ in confirmed.values() if isinstance(subject, int)})
        | Q(pk__in=release_employees),
    ).order_by('pk').values_list('pk', flat=True))
    locked = lock_settlement_write_access(
        lease=lease, control_context=control, employee_ids=employee_ids, using=using,
    )
    list(PersonnelPosition.objects.select_for_update().filter(
        pk__in=Employee.objects.filter(pk__in=employee_ids).values('personnel_position_id'),
    ).order_by('pk'))
    actor = locked.access_by_id(control.owner_access_id)

    external_ids = {
        int(subject.removeprefix('resident:')) for subject, _ in confirmed.values()
        if isinstance(subject, str) and subject.startswith('resident:')
    }
    list(SettlementResident.objects.select_for_update(of=('self',)).filter(pk__in=external_ids).order_by('pk'))

    # Housing-fund edits take the dormitory lock in their own service.
    list(Dormitory.objects.select_for_update().filter(number__in=['5', '6']).order_by('pk'))
    beds = {
        bed.pk: bed for bed in PhysicalBed.objects.select_for_update(of=('self',))
        .select_related('room')
        .filter(pk__in={bed_id for _, bed_id in confirmed.values()}).order_by('pk')
    }
    groups = build_current_housing_preview(rows, as_of, filename=filename, review=review)
    groups['released'] = []
    pending_releases = preview_releases(groups)
    for row in groups['interwatch']:
        occupancy = row.interwatch_occupancy
        if occupancy and pending_releases[row.source] == confirmed_releases.get(row.source):
            release_resident_from_bed(
                occupancy_id=occupancy.pk, bed_stable_id=occupancy.physical_bed.stable_id,
                reason='Жилец на межвахте, подтверждено при сверке Excel.', control_context=control,
            )
            groups['released'].append(row)

    groups['applied'] = []
    created_count = 0
    moment = timezone.now()
    for group in APPLICABLE_GROUPS:
        for row in groups[group]:
            if confirmed.get(row.source) != [row.placement_subject, row.physical_bed_id]:
                row.issues.append('Строка изменилась после предпросмотра. Проверьте Excel повторно.')
            else:
                try:
                    # A rejected row must not leave a newly created resident by itself.
                    with transaction.atomic():
                        created_count += _import_one_placement(
                            row, bed=beds[row.physical_bed_id], actor=actor, control=control,
                            workbook_digest=workbook_digest, moment=moment, using=using,
                        )
                except ValidationError as error:
                    row.issues.extend(error.messages)
            row.status = 'conflict' if row.issues else 'applied'
            groups[row.status].append(row)
        groups[group] = []
    return groups, created_count
