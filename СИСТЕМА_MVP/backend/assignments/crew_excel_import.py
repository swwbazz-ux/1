import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from io import BytesIO

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from openpyxl import load_workbook

from rotations.arrival_roster_parser import validate_xlsx_archive
from users.models import Employee, Role

from .crew_categories import crew_equipment_number
from .models import CrewPlanSlot, WorkShiftType
from .services import (
    CREW_SLOT_POSITION_PRIMARY,
    CREW_SLOT_POSITION_SECONDARY,
    _locked_deputy_publish_access,
    _validate_crew_employee,
    get_or_create_crew_draft,
    production_work_date,
    _publish_crew_plan_once,
    validate_plan_crew_bookings,
    validate_employee_equipment_organization,
)


MAX_FILE_SIZE = 2 * 1024 * 1024
MAX_ROWS = 400
CONFIRMATION_SALT = 'deputy-crew-excel-replace'
EXCEL_CATEGORIES = {
    'самосвал': 'driver', 'экскаватор': 'excavator_operator', 'робот': 'driver',
    'вахтовый автобус': 'crew_bus', 'кдм': 'crew_kdm', 'прм': 'crew_prm',
    'планировщик': 'crew_planner', 'бульдозер': 'crew_dozer', 'колесник': 'crew_wheel_dozer',
    'грейдер': 'crew_grader', 'каток': 'crew_roller',
}
SHIFT_COLUMNS = (WorkShiftType.SHIFT_1, WorkShiftType.SHIFT_2)
TRANSITION = re.compile(
    r'(.+?)\s*\(\s*(до|по)\s*(\d{1,2})\s*\.\s*(\d{1,2})\s*\.?\s*\)\s*затем\s+(.+)',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExcelSlot:
    number: str
    shift: str
    value: str
    address: str


class DraftConfirmationRequired(Exception):
    def __init__(self, categories, token):
        self.categories = categories
        self.token = token


def _text(value):
    return str(value).strip() if value is not None else ''


def _key(value):
    return value.casefold().replace('ё', 'е')


def _number(value):
    text = _text(value).upper()
    return str(int(text)) if re.fullmatch(r'[0-9]+', text) else text


def read_crew_excel(payload):
    if not payload or len(payload) > MAX_FILE_SIZE:
        raise ValidationError('Выбери файл .xlsx размером до 2 МБ.')
    try:
        validate_xlsx_archive(payload)
        workbook = load_workbook(BytesIO(payload), read_only=True, keep_links=False)
    except ValidationError:
        raise
    except Exception as error:
        raise ValidationError('Не удалось открыть файл .xlsx.') from error

    slots = defaultdict(list)
    skipped = Counter()
    try:
        if 'Лист1' not in workbook.sheetnames:
            raise ValidationError('В книге нужен лист «Лист1» с расстановкой.')
        rows = workbook['Лист1'].iter_rows(max_col=4, max_row=MAX_ROWS + 1)
        header = next(rows)
        if [_key(_text(cell.value)) for cell in header[2:]] != ['смена 1', 'смена 2']:
            raise ValidationError('На «Лист1» в C1 и D1 нужны заголовки «смена 1» и «смена 2».')
        for index, cells in enumerate(rows, start=2):
            category, number, *assignments = [_text(cell.value) for cell in cells]
            if not category and not number and not any(assignments):
                break
            if index > MAX_ROWS:
                raise ValidationError(f'Таблица расстановки должна занимать до {MAX_ROWS} строк.')
            if any(cell.data_type == 'f' for cell in cells):
                raise ValidationError(f'Строка {index}. Замени формулы готовыми значениями.')
            key = _key(category)
            role_code = EXCEL_CATEGORIES.get(key)
            if not role_code and key.split(' ', 1)[0] in {'самосвал', 'экскаватор'}:
                role_code = EXCEL_CATEGORIES[key.split(' ', 1)[0]]
            if key == 'робот' and _number(number) != '53':
                raise ValidationError(f'B{index}. Для робота ожидается номер 53.')
            if not role_code:
                if not category:
                    raise ValidationError(f'Строка {index}. Укажи тип техники в столбце A.')
                skipped[category] += 1
                continue
            if not number and role_code != 'crew_roller':
                raise ValidationError(f'Строка {index}. Укажи номер техники в столбце B.')
            for field, shift, value in zip('CD', SHIFT_COLUMNS, assignments):
                if len(value) > 300:
                    raise ValidationError(f'{field}{index}. В ячейке слишком длинное назначение.')
                if value:
                    slots[role_code].append(ExcelSlot(number, shift, value, f'{field}{index}'))
    except ValidationError:
        raise
    except Exception as error:
        raise ValidationError('Не удалось прочитать строки на «Лист1».') from error
    finally:
        workbook.close()
    if not slots:
        raise ValidationError('На «Лист1» нет заполненных назначений по технике.')
    return slots, skipped


def _name_parts(value):
    return tuple(re.findall(r'[а-яa-z-]+', _key(value)))


def _crew_values(value, work_date):
    transition = TRANSITION.fullmatch(value)
    if transition:
        before, bound, day, month, after = transition.groups()
        try:
            cutoff = date(work_date.year, int(month), int(day))
        except ValueError as error:
            raise ValidationError('Проверь дату замены сотрудника.') from error
        if work_date == cutoff and _key(bound) == 'до':
            raise ValidationError('На дату замены укажи одного сотрудника без примечания «затем».')
        value = before if work_date <= cutoff else after
    values = [
        re.sub(r'\s*\([^()]*\)\s*$', '', part).strip()
        for part in re.split(r'[,\n]+', value)
        if part.strip()
    ]
    if not 1 <= len(values) <= 2:
        raise ValidationError('Укажи одного сотрудника или два имени для экипажа.')
    return values


def _similar_directory_names(parts, names, personnel_number):
    if len(parts) not in (2, 3) or any(len(part) < 2 for part in parts):
        return []
    ranked = {}
    for surname_group in names.values():
        for tokens, employee in surname_group:
            if personnel_number is not None and employee.personnel_number != personnel_number:
                continue
            if len(tokens) < len(parts):
                continue
            differences = [(source, target) for source, target in zip(parts, tokens) if source != target]
            if len(differences) != 1:
                continue
            source, target = differences[0]
            score = SequenceMatcher(None, source, target).ratio()
            # A full three-part name has two corroborating fields. For a short
            # name, also require a close spelling of the differing field.
            if len(parts) == 3 or score >= 0.75:
                ranked[employee.full_name] = score
    return sorted(ranked, key=lambda name: (-ranked[name], name))[:5]


def _find_employee(value, names):
    if _key(value) == 'снять':
        return None
    name, separator, number = value.partition(' [таб. ')
    if separator and not number.endswith(']'):
        raise ValidationError('Проверь табельный номер в квадратных скобках.')
    parts = _name_parts(name)
    candidates = []
    if parts:
        for tokens, employee in names.get(parts[0], ()):
            if separator and employee.personnel_number != number[:-1]:
                continue
            if len(parts) <= len(tokens) and all(
                source == target or (i > 0 and len(source) == 1 and target.startswith(source))
                for i, (source, target) in enumerate(zip(parts, tokens))
            ):
                candidates.append(employee)
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise ValidationError(
            f'В справочнике несколько действующих сотрудников с ФИО «{name}». '
            'Уточни полное ФИО или добавь табельный номер в формате [таб. 123].'
        )
    similar = _similar_directory_names(parts, names, number[:-1] if separator else None)
    if similar:
        variants = ', '.join(f'«{full_name}»' for full_name in similar)
        raise ValidationError(
            f'В Excel — «{value}». Похожие записи в справочнике — {variants}. '
            'Сверь, кто назначен на смену, и укажи его ФИО из справочника.'
        )
    raise ValidationError(
        f'Среди действующих сотрудников справочника нет совпадения для «{value}». '
        'Проверь написание ФИО и статус карточки.'
    )


def _assigned_crew(slot):
    return slot.employee_id, slot.secondary_employee_id, slot.reported_employee_id


def _baseline_crew(slot):
    return slot.baseline_employee_id, slot.baseline_secondary_employee_id, slot.baseline_reported_employee_id


def _stage_plan(plan, slots, records, names, actor, *, factual=False):
    original = {slot.pk: _assigned_crew(slot) for slot in slots}
    by_number = defaultdict(list)
    for slot in slots:
        slot.employee_id, slot.secondary_employee_id, slot.reported_employee_id = _baseline_crew(slot)
        key = (_number(crew_equipment_number(slot.equipment)), slot.shift_type)
        by_number[key].append(slot)
        if plan.role.code == 'crew_roller' and key[0]:
            by_number[('', slot.shift_type)].append(slot)
    coordinates = {}
    errors = []
    for record in records:
        matching_slots = by_number.get((_number(record.number), record.shift), ())
        if len(matching_slots) != 1:
            errors.append(f'{record.address}. Номер техники «{record.number}» не найден или неоднозначен.')
            continue
        slot = matching_slots[0]
        if slot.pk in coordinates:
            errors.append(f'{record.address}. Техника и смена уже указаны в {coordinates[slot.pk]}.')
            continue
        coordinates[slot.pk] = record.address
        try:
            staff = [_find_employee(value, names) for value in _crew_values(record.value, plan.work_date)]
            reported_id = None
            for employee, position in zip(staff, (CREW_SLOT_POSITION_PRIMARY, CREW_SLOT_POSITION_SECONDARY)):
                if employee is not None:
                    try:
                        _validate_crew_employee(employee, plan.role, position=position)
                    except ValidationError as error:
                        if not (
                            factual and error.code == 'invalid_work_category'
                            and position == CREW_SLOT_POSITION_PRIMARY
                        ):
                            raise
                        reported_id = employee.pk
                    validate_employee_equipment_organization(employee, slot.equipment)
        except ValidationError as error:
            errors.extend(f'{record.address}. {message}' for message in error.messages)
            continue
        primary = staff[0].pk if staff[0] else None
        if len(staff) == 2:
            secondary = staff[1].pk if staff[1] else None
        else:
            secondary = slot.secondary_employee_id if primary else None
        slot.employee_id = None if reported_id else primary
        slot.secondary_employee_id = secondary
        slot.reported_employee_id = reported_id
    if errors:
        raise ValidationError(errors)

    occupied = {}
    for slot in slots:
        label = coordinates.get(slot.pk) or f'№ {slot.equipment.garage_number}, {slot.get_shift_type_display()}'
        for employee_id in _assigned_crew(slot):
            if employee_id is not None:
                if employee_id in occupied:
                    raise ValidationError(f'{label} и {occupied[employee_id]}. Сотрудник оказался назначен дважды.')
                occupied[employee_id] = label
    changed = [slot for slot in slots if original[slot.pk] != _assigned_crew(slot)]
    if changed:
        CrewPlanSlot.objects.filter(pk__in=[slot.pk for slot in changed]).update(
            employee=None, secondary_employee=None, reported_employee=None,
        )
        CrewPlanSlot.objects.bulk_update(changed, ['employee', 'secondary_employee', 'reported_employee'])
        plan.updated_by = actor
        plan.version += 1
        plan.save(update_fields=['updated_by', 'updated_at', 'version'])
    return sum(_assigned_crew(slot) != _baseline_crew(slot) for slot in slots)


def _require_draft_confirmation(plans, *, confirmation, file_digest, work_date, access):
    revisions = []
    categories = []
    for plan, slots in plans:
        edited = sum(_assigned_crew(slot) != _baseline_crew(slot) for slot in slots)
        draft_version = [plan.pk, plan.version] if edited else None
        revisions.append([plan.role.code, plan.revision, draft_version])
        if edited:
            categories.append((plan.role.code, edited))
    expected = [access.pk, work_date.isoformat(), file_digest, revisions]
    if confirmation:
        try:
            supplied = signing.loads(confirmation, salt=CONFIRMATION_SALT)
        except signing.BadSignature:
            supplied = None
        if supplied == expected:
            return
        if not categories:
            raise ValidationError('Файл или расстановка изменились. Запусти импорт ещё раз.')
    if categories:
        raise DraftConfirmationRequired(categories, signing.dumps(expected, salt=CONFIRMATION_SALT))


@transaction.atomic
def publish_crew_excel(grouped, *, work_date, access, file_digest, confirmation='', factual=False):
    if work_date != production_work_date():
        raise ValidationError('Производственные сутки изменились. Открой форму загрузки заново.')
    actor, actor_access = _locked_deputy_publish_access(actor_access=access, actor=access.employee)
    plans = []
    for code in sorted(grouped):
        role = Role.objects.filter(code=code, is_active=True).first()
        if role is None:
            raise ValidationError('Категория техники недоступна для публикации.')
        plan, _ = get_or_create_crew_draft(role=role, work_date=work_date, actor=actor)
        slots = list(plan.slots.select_for_update(of=('self',)).select_related('equipment__equipment_type'))
        plans.append((plan, slots))
    if factual:
        file_digest = f"factual:{file_digest}"
    _require_draft_confirmation(
        plans, confirmation=confirmation, file_digest=file_digest, work_date=work_date, access=actor_access,
    )
    names = defaultdict(list)
    for employee in Employee.objects.filter(is_active=True, status=Employee.Status.ACTIVE):
        parts = _name_parts(employee.full_name)
        if parts:
            names[parts[0]].append((parts, employee))

    publications = []
    for plan, slots in plans:
        changed = _stage_plan(plan, slots, grouped[plan.role.code], names, actor, factual=factual)
        if changed:
            published = _publish_crew_plan_once(
                plan=plan, expected_version=plan.version, actor=actor, actor_access=actor_access,
            )
            publications.append((published, changed))
    # Validate the final workbook as one transaction so a slot cleared on a later
    # sheet/category does not look like a second booking.
    # A confirmed import can restore a draft to baseline without publishing a revision.
    # Reserve those people too, as manual slot edits take the same employee row locks.
    occupants = {pk for _, slots in plans for slot in slots for pk in _assigned_crew(slot) if pk}
    list(Employee.objects.select_for_update().filter(pk__in=occupants).order_by('pk').values_list('pk', flat=True))
    for plan, _ in plans:
        validate_plan_crew_bookings(plan)
    return publications
