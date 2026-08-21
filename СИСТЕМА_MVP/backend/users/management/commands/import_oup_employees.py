import csv
import json
import re
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import bump_operational_state
from users.models import Employee, PersonnelDepartment, WatchComposition, WorkSchedule
from users.oup_services import (
    employee_audit_snapshot,
    employee_work_category_blockers,
    format_employee_changes,
    log_oup_action,
)
from users.oup_undo import (
    OUP_ACTION_BULK_EMPLOYEE_CREATED,
    OUP_ACTION_BULK_EMPLOYEE_UPDATED,
)


REQUIRED_COLUMNS = {
    'full_name',
    'personnel_number',
    'position',
    'hired_at',
    'rotation',
    'birth_date',
    'department',
    'phone',
    'work_category',
}
VALID_CATEGORIES = {choice for choice, _label in Employee.WorkCategory.choices}
OPTIONAL_WATCH_PROFILE_COLUMNS = {
    'work_schedule',
    'brigade_number',
    'watch_composition',
}
WATCH_PROFILE_FIELD_LABELS = {
    'work_schedule': 'график работы (work_schedule)',
    'brigade_number': 'бригада (brigade_number)',
    'watch_composition': 'состав вахты (watch_composition)',
}
EXISTING_EMPLOYEE_UPDATE_FIELDS = (
    'full_name',
    'personnel_number',
    'position',
    'department',
    'personnel_department',
    'work_category',
    'hired_at',
    'birth_date',
    'phone',
)
NEW_EMPLOYEE_FIELDS = EXISTING_EMPLOYEE_UPDATE_FIELDS + (
    'rotation',
    'work_schedule',
    'brigade_number',
    'watch_composition',
)
SCHEDULE_WITH_BRIGADE_PATTERN = re.compile(
    r'^(?P<name>.+?)\s+бригада\s*№?\s*(?P<brigade>[1-4])$',
    re.IGNORECASE,
)


def normalize_phone(value):
    digits = re.sub(r'\D', '', value or '')
    if len(digits) == 11 and digits.startswith('8'):
        digits = f'7{digits[1:]}'
    elif len(digits) == 10 and digits.startswith('9'):
        digits = f'7{digits}'
    return f'+{digits}' if len(digits) == 11 and digits.startswith('7') else ''


def parse_date(value, field_name):
    try:
        return datetime.strptime((value or '').strip(), '%d.%m.%Y').date()
    except ValueError as error:
        raise ValueError(f'{field_name}: ожидается дата ДД.ММ.ГГГГ') from error


def normalized_catalogue_name(value):
    return re.sub(r'\s+', ' ', (value or '').strip()).casefold()


def catalogue_index(queryset):
    return {normalized_catalogue_name(record.name): record for record in queryset}


def build_legacy_rotation(work_schedule, brigade_number):
    if work_schedule is None:
        return ''
    if brigade_number is not None:
        return f'{work_schedule.name} Бригада №{brigade_number}'
    return work_schedule.name


class Command(BaseCommand):
    help = 'Безопасно импортирует кадровые карточки ОУП из нормализованного CSV; без --commit работает как dry-run.'

    def add_arguments(self, parser):
        parser.add_argument('csv_path')
        parser.add_argument('--commit', action='store_true', help='Записать прошедшие проверку строки в базу.')
        parser.add_argument('--source-label', default='Массовый импорт ОУП')

    def handle(self, *args, **options):
        csv_path = Path(options['csv_path'])
        if not csv_path.is_file():
            raise CommandError(f'Файл не найден: {csv_path}')

        with csv_path.open(encoding='utf-8-sig', newline='') as source:
            reader = csv.DictReader(source)
            missing_columns = REQUIRED_COLUMNS - set(reader.fieldnames or [])
            if missing_columns:
                raise CommandError('Не хватает колонок: ' + ', '.join(sorted(missing_columns)))
            source_rows = list(reader)

        self.departments_by_name = catalogue_index(PersonnelDepartment.objects.all())
        self.schedules_by_name = catalogue_index(WorkSchedule.objects.all())
        self.watch_compositions_by_name = catalogue_index(WatchComposition.objects.all())

        prepared = []
        skipped = []
        for row_number, row in enumerate(source_rows, start=2):
            try:
                item = self._prepare_row(row)
            except ValueError as error:
                skipped.append({'row': row_number, 'reason': str(error)})
                continue
            item['row_number'] = row_number
            prepared.append(item)

        summary = {
            'mode': 'commit' if options['commit'] else 'dry-run',
            'source_rows': len(source_rows),
            'created': 0,
            'updated': 0,
            'unchanged': 0,
            'skipped': len(skipped),
            'category_preserved': 0,
            'skip_reasons': {},
        }

        with transaction.atomic():
            changed_employee_ids = []
            for item in prepared:
                outcome, employee_id, reason = self._import_item(
                    item,
                    commit=options['commit'],
                    source_label=options['source_label'],
                )
                if outcome == 'skipped':
                    skipped.append({'row': item['row_number'], 'reason': reason})
                    summary['skipped'] += 1
                else:
                    summary[outcome] += 1
                    if employee_id:
                        changed_employee_ids.append(employee_id)
                if reason == 'category_preserved':
                    summary['category_preserved'] += 1

            if options['commit'] and changed_employee_ids:
                bump_operational_state(
                    'Employee:bulk_import',
                    event_type='personnel_changed',
                    object_type='Employee',
                    payload={
                        'action': 'bulk_import',
                        'employee_ids': changed_employee_ids,
                    },
                )
            if not options['commit']:
                transaction.set_rollback(True)

        for item in skipped:
            reason = item['reason']
            summary['skip_reasons'][reason] = summary['skip_reasons'].get(reason, 0) + 1

        self.stdout.write(json.dumps(summary, ensure_ascii=False, sort_keys=True))

    def _prepare_row(self, row):
        values = {key: (row.get(key) or '').strip() for key in REQUIRED_COLUMNS}
        for key in ('full_name', 'personnel_number', 'position', 'department', 'rotation'):
            if not values[key]:
                raise ValueError(f'не заполнено поле {key}')
        if len(values['full_name'].split()) < 2:
            raise ValueError('ФИО должно содержать фамилию и имя')
        if len(values['position']) > Employee._meta.get_field('position').max_length:
            raise ValueError('должность длиннее допустимого значения')
        if len(values['department']) > Employee._meta.get_field('department').max_length:
            raise ValueError('подразделение длиннее допустимого значения')
        if len(values['rotation']) > Employee._meta.get_field('rotation').max_length:
            raise ValueError('график длиннее допустимого значения')
        phone = normalize_phone(values['phone'])
        if not phone:
            raise ValueError('некорректный или отсутствующий телефон')
        if values['work_category'] not in VALID_CATEGORIES:
            raise ValueError('неизвестная рабочая категория')
        personnel_department = self.departments_by_name.get(normalized_catalogue_name(values['department']))
        provided_watch_profile_fields = set()
        explicit_schedule_name = (row.get('work_schedule') or '').strip()
        explicit_brigade = (row.get('brigade_number') or '').strip()
        explicit_watch_composition = (row.get('watch_composition') or '').strip()

        work_schedule = None
        brigade_number = None
        schedule_match = SCHEDULE_WITH_BRIGADE_PATTERN.match(
            re.sub(r'\s+', ' ', values['rotation']),
        )
        if schedule_match:
            work_schedule = self.schedules_by_name.get(normalized_catalogue_name(schedule_match.group('name')))
            candidate_brigade = int(schedule_match.group('brigade'))
            if work_schedule and work_schedule.is_active and candidate_brigade <= work_schedule.brigade_count:
                brigade_number = candidate_brigade
            else:
                work_schedule = None

        if explicit_schedule_name:
            provided_watch_profile_fields.add('work_schedule')
            work_schedule = self.schedules_by_name.get(normalized_catalogue_name(explicit_schedule_name))
            if work_schedule is None:
                raise ValueError('неизвестный график работы')
            if not work_schedule.is_active:
                raise ValueError('график работы неактивен')

        if explicit_brigade:
            provided_watch_profile_fields.add('brigade_number')
            try:
                brigade_number = int(explicit_brigade)
            except ValueError as error:
                raise ValueError('бригада должна быть целым числом') from error

        watch_composition = None
        if explicit_watch_composition:
            provided_watch_profile_fields.add('watch_composition')
            watch_composition = self.watch_compositions_by_name.get(
                normalized_catalogue_name(explicit_watch_composition),
            )
            if watch_composition is None:
                raise ValueError('неизвестный состав вахты')
            if not watch_composition.is_active:
                raise ValueError('состав вахты неактивен')
        return {
            'full_name': ' '.join(values['full_name'].split()),
            'personnel_number': values['personnel_number'],
            'position': values['position'],
            'department': values['department'],
            'personnel_department': personnel_department,
            'work_category': values['work_category'],
            'hired_at': parse_date(values['hired_at'], 'дата приёма'),
            'birth_date': parse_date(values['birth_date'], 'дата рождения'),
            'rotation': build_legacy_rotation(work_schedule, brigade_number),
            'work_schedule': work_schedule,
            'brigade_number': brigade_number,
            'watch_composition': watch_composition,
            'phone': phone,
            'provided_watch_profile_fields': tuple(sorted(provided_watch_profile_fields)),
        }

    def _find_employee(self, item):
        by_number = list(
            Employee.objects.select_for_update()
            .filter(personnel_number__iexact=item['personnel_number'])
            .order_by('id')[:2]
        )
        if len(by_number) == 1:
            return by_number[0], ''
        if len(by_number) > 1:
            return None, 'табельный номер уже относится к нескольким карточкам'

        by_name = list(
            Employee.objects.select_for_update()
            .filter(full_name__iexact=item['full_name'])
            .order_by('id')[:2]
        )
        if not by_name:
            return None, ''
        if len(by_name) > 1:
            return None, 'найдено несколько карточек с таким ФИО'
        employee = by_name[0]
        if employee.personnel_number.strip():
            return None, 'ФИО уже существует с другим табельным номером'
        return employee, ''

    @staticmethod
    def _new_profile_error(item):
        work_schedule = item['work_schedule']
        brigade_number = item['brigade_number']
        if work_schedule is None and brigade_number is not None:
            return 'бригада указана без графика работы'
        if work_schedule is not None:
            if not work_schedule.is_active:
                return 'график работы неактивен'
            if work_schedule.brigade_count == 0 and brigade_number is not None:
                return 'для графика без бригад бригада не допускается'
            if work_schedule.brigade_count > 0 and brigade_number is None:
                return 'для графика требуется бригада'
            if brigade_number is not None and not 1 <= brigade_number <= work_schedule.brigade_count:
                return 'бригада не соответствует графику работы'
        if len(item['rotation']) > Employee._meta.get_field('rotation').max_length:
            return 'сформированное значение графика длиннее допустимого'
        return ''

    def _import_item(self, item, *, commit, source_label):
        employee, conflict = self._find_employee(item)
        if conflict:
            return 'skipped', None, conflict
        if employee and (not employee.is_active or employee.status in {
            Employee.Status.DISMISSED,
            Employee.Status.ARCHIVED,
            Employee.Status.DELETED,
        }):
            return 'skipped', None, 'совпавшая карточка не является действующей'

        if employee is None:
            profile_error = self._new_profile_error(item)
            if profile_error:
                return 'skipped', None, profile_error
            if not commit:
                return 'created', None, ''
            employee = Employee.objects.create(
                **{key: item[key] for key in NEW_EMPLOYEE_FIELDS},
                status=Employee.Status.ACTIVE,
                is_active=True,
                dismissed_at=None,
            )
            snapshot = employee_audit_snapshot(employee)
            log_oup_action(
                None,
                'создан сотрудник массовым импортом',
                employee,
                action_code=OUP_ACTION_BULK_EMPLOYEE_CREATED,
                new_value='; '.join(f'{label}: {value}' for label, value in snapshot.values()),
                comment=source_label,
            )
            return 'created', employee.id, ''

        conflicting_fields = []
        for field_name in item['provided_watch_profile_fields']:
            if field_name == 'work_schedule':
                values_match = employee.work_schedule_id == (
                    item['work_schedule'].id if item['work_schedule'] else None
                )
            elif field_name == 'watch_composition':
                values_match = employee.watch_composition_id == (
                    item['watch_composition'].id if item['watch_composition'] else None
                )
            else:
                values_match = employee.brigade_number == item['brigade_number']
            if not values_match:
                conflicting_fields.append(field_name)
        if conflicting_fields:
            raise CommandError(
                f"Строка {item['row_number']}: конфликт защищённого профиля: "
                + ', '.join(WATCH_PROFILE_FIELD_LABELS[field] for field in conflicting_fields),
            )

        before = employee_audit_snapshot(employee)
        category_preserved = False
        for field_name in EXISTING_EMPLOYEE_UPDATE_FIELDS:
            value = item[field_name]
            if field_name == 'work_category' and employee.work_category != value:
                if employee_work_category_blockers(employee):
                    category_preserved = True
                    continue
            setattr(employee, field_name, value)
        after = employee_audit_snapshot(employee)
        changes = format_employee_changes(before, after)
        if changes == 'Данные без изменений':
            return 'unchanged', None, 'category_preserved' if category_preserved else ''
        if not commit:
            return 'updated', None, 'category_preserved' if category_preserved else ''
        employee.save(update_fields=[*EXISTING_EMPLOYEE_UPDATE_FIELDS, 'updated_at'])
        log_oup_action(
            None,
            'обновлена карточка массовым импортом',
            employee,
            action_code=OUP_ACTION_BULK_EMPLOYEE_UPDATED,
            old_value=changes,
            new_value='Изменения сохранены',
            comment=source_label,
        )
        return 'updated', employee.id, 'category_preserved' if category_preserved else ''
