import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from io import BytesIO

from django.core.exceptions import ValidationError
from django.db import transaction
from openpyxl import load_workbook

from rotations.arrival_roster_parser import validate_xlsx_archive
from users.models import Employee, Role

from .models import CrewPlanSlot, WorkShiftType
from .services import (
    CREW_SLOT_POSITION_PRIMARY,
    CREW_SLOT_POSITION_SECONDARY,
    _locked_deputy_publish_access,
    _validate_crew_employee,
    get_or_create_crew_draft,
    production_work_date,
    publish_crew_plan,
    validate_employee_equipment_organization,
)


MAX_FILE_SIZE = 2 * 1024 * 1024
MAX_ROWS = 400
EXCEL_CATEGORIES = {'самосвал': 'driver', 'экскаватор': 'excavator_operator'}
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
            role_code = EXCEL_CATEGORIES.get(_key(category).split(' ', 1)[0])
            if not role_code:
                if not category:
                    raise ValidationError(f'Строка {index}. Укажи тип техники в столбце A.')
                skipped[category] += 1
                continue
            if not number:
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
        raise ValidationError('На «Лист1» нет заполненных назначений самосвалов и экскаваторов.')
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


def _find_employee(value, names):
    if _key(value) == 'снять':
        return None
    parts = _name_parts(value)
    candidates = []
    if parts:
        for tokens, employee in names.get(parts[0], ()):
            if len(parts) <= len(tokens) and all(
                source == target or (i > 0 and len(source) == 1 and target.startswith(source))
                for i, (source, target) in enumerate(zip(parts, tokens))
            ):
                candidates.append(employee)
    if len(candidates) != 1:
        issue = 'ФИО неоднозначно' if candidates else 'Сотрудник не найден'
        raise ValidationError(f'{issue} — {value}. Укажи ФИО из справочника.')
    return candidates[0]


def _stage_plan(plan, records, names, actor):
    slots = list(plan.slots.select_for_update(of=('self',)).select_related('equipment'))
    if any(
        (slot.employee_id, slot.secondary_employee_id)
        != (slot.baseline_employee_id, slot.baseline_secondary_employee_id)
        for slot in slots
    ):
        raise ValidationError(f'В категории «{plan.role.name}» уже есть ручные правки в черновике.')

    by_number = defaultdict(list)
    for slot in slots:
        key = (_number(slot.equipment.garage_number), slot.shift_type)
        by_number[key].append(slot)
    coordinates = {}
    changed = []
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
            for employee, position in zip(staff, (CREW_SLOT_POSITION_PRIMARY, CREW_SLOT_POSITION_SECONDARY)):
                if employee is not None:
                    _validate_crew_employee(employee, plan.role, position=position)
                    validate_employee_equipment_organization(employee, slot.equipment)
        except ValidationError as error:
            errors.extend(f'{record.address}. {message}' for message in error.messages)
            continue
        primary = staff[0].pk if staff[0] else None
        if len(staff) == 2:
            secondary = staff[1].pk if staff[1] else None
        else:
            secondary = slot.secondary_employee_id if primary else None
        if (primary, secondary) != (slot.employee_id, slot.secondary_employee_id):
            slot.employee_id, slot.secondary_employee_id = primary, secondary
            changed.append(slot)
    if errors:
        raise ValidationError(errors)

    occupied = {}
    for slot in slots:
        label = coordinates.get(slot.pk) or f'№ {slot.equipment.garage_number}, {slot.get_shift_type_display()}'
        for employee_id in (slot.employee_id, slot.secondary_employee_id):
            if employee_id is not None:
                if employee_id in occupied:
                    raise ValidationError(f'{label} и {occupied[employee_id]}. Сотрудник оказался назначен дважды.')
                occupied[employee_id] = label
    if not changed:
        return 0

    CrewPlanSlot.objects.filter(pk__in=[slot.pk for slot in changed]).update(
        employee=None, secondary_employee=None,
    )
    CrewPlanSlot.objects.bulk_update(changed, ['employee', 'secondary_employee'])
    plan.updated_by = actor
    plan.version += 1
    plan.save(update_fields=['updated_by', 'updated_at', 'version'])
    return len(changed)


@transaction.atomic
def publish_crew_excel(grouped, *, work_date, access):
    if work_date != production_work_date():
        raise ValidationError('Производственные сутки изменились. Открой форму загрузки заново.')
    actor, actor_access = _locked_deputy_publish_access(actor_access=access, actor=access.employee)
    names = defaultdict(list)
    for employee in Employee.objects.filter(is_active=True, status=Employee.Status.ACTIVE):
        parts = _name_parts(employee.full_name)
        if parts:
            names[parts[0]].append((parts, employee))

    publications = []
    for code in sorted(grouped):
        role = Role.objects.filter(code=code, is_active=True).first()
        if role is None:
            raise ValidationError('Категория техники недоступна для публикации.')
        plan, _ = get_or_create_crew_draft(role=role, work_date=work_date, actor=actor)
        changed = _stage_plan(plan, grouped[code], names, actor)
        if changed:
            published = publish_crew_plan(
                plan=plan, expected_version=plan.version, actor=actor, actor_access=actor_access,
            )
            publications.append((published, changed))
    return publications
