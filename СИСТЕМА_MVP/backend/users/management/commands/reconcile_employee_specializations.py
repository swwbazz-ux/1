import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db import transaction

from users.models import Employee, PersonnelPosition
from users.oup_services import employee_work_category_blockers, emit_employee_changed, lock_current_crew_drafts, log_oup_action
from users.work_profiles import legacy_work_category_for_specialization, resolve_base_specialization


class Command(BaseCommand):
    help = 'Проверяет специализации действующих сотрудников. --commit заполняет только однозначные пропуски.'

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true')

    def handle(self, *args, commit=False, **options):
        positions = {
            ' '.join(position.name.casefold().split()): position
            for position in PersonnelPosition.objects.prefetch_related('allowed_specializations')
        }
        report = {'mode': 'commit' if commit else 'dry-run', 'fill': [], 'review': [], 'updated': 0}
        with transaction.atomic():
            employees = Employee.objects.filter(is_active=True, status=Employee.Status.ACTIVE).order_by('pk')
            if commit:
                lock_current_crew_drafts()
                employees = employees.select_for_update(of=('self',))
            employees = employees.select_related('personnel_position', 'base_specialization__access_role')
            for employee in employees:
                self.check_employee(employee, positions, report, commit)
        self.stdout.write(json.dumps(report, ensure_ascii=False))

    def check_employee(self, employee, positions, report, commit):
        if employee.is_protected:
            return
        text_position = positions.get(' '.join(employee.position.casefold().split()))
        position = employee.personnel_position or text_position
        row = {'personnel_number': employee.personnel_number, 'full_name': employee.full_name,
               'position': position.name if position else employee.position}

        def review(reason):
            report['review'].append({**row, 'reason': reason})

        if not position:
            if employee.base_specialization_id or employee.work_category != Employee.WorkCategory.OTHER:
                review('Должность не сопоставлена со справочником.')
            return
        if not position.is_active or (text_position and text_position != position):
            review('Проверьте кадровую должность.')
            return
        try:
            specialization = resolve_base_specialization(
                personnel_position=position, selected=employee.base_specialization,
            )
        except ValidationError as error:
            review(error.messages[0])
            return
        if not specialization:
            return
        category = legacy_work_category_for_specialization(specialization)
        if employee.base_specialization_id:
            if not employee.personnel_position_id or employee.work_category != category:
                review('Проверьте связь должности и рабочей категории с сохранённой специализацией.')
            return
        if employee.work_category not in {Employee.WorkCategory.OTHER, category}:
            review('Старая рабочая категория расходится со специализацией должности.')
            return
        blockers = employee_work_category_blockers(employee)
        if blockers:
            review('; '.join(blockers))
            return
        report['fill'].append({**row, 'specialization': specialization.name})
        if commit:
            employee.personnel_position = position
            employee.position = position.name
            employee.base_specialization = specialization
            employee.work_category = category
            employee.save(update_fields=['personnel_position', 'position', 'base_specialization', 'work_category', 'updated_at'])
            log_oup_action(None, 'заполнена специализация по должности', employee,
                           old_value='Специализация не указана', new_value=specialization.name)
            emit_employee_changed(employee, 'reconcile_specialization')
            report['updated'] += 1
