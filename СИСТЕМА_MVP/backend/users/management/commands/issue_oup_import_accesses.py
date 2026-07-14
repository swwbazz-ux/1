import csv
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from users.models import AdminActionLog, Employee, EmployeeAccess, Role
from users.oup_services import issue_employee_access


ROLE_BY_CATEGORY = {
    Employee.WorkCategory.DRIVER: 'driver',
    Employee.WorkCategory.EXCAVATOR_OPERATOR: 'excavator_operator',
}


class Command(BaseCommand):
    help = 'Выдаёт первичные PIN импортированным водителям и машинистам; без --commit работает как dry-run.'

    def add_arguments(self, parser):
        parser.add_argument('--source-label', required=True)
        parser.add_argument('--commit', action='store_true')
        parser.add_argument('--output', help='CSV-ведомость созданных первичных PIN.')

    def handle(self, *args, **options):
        if options['commit'] and not options.get('output'):
            raise CommandError('Для режима --commit укажите защищённый путь --output.')

        employee_ids = list(
            AdminActionLog.objects.filter(
                comment=options['source_label'],
                object_type='Employee',
            )
            .exclude(object_id='')
            .values_list('object_id', flat=True)
        )
        employees = list(
            Employee.objects.filter(
                id__in=employee_ids,
                is_active=True,
                status=Employee.Status.ACTIVE,
                work_category__in=ROLE_BY_CATEGORY,
            ).order_by('full_name')
        )
        roles = {
            role.code: role
            for role in Role.objects.filter(code__in=ROLE_BY_CATEGORY.values(), is_active=True)
        }
        missing_roles = set(ROLE_BY_CATEGORY.values()) - set(roles)
        if missing_roles:
            raise CommandError('Не найдены активные роли: ' + ', '.join(sorted(missing_roles)))

        created_rows = []
        skipped_existing = 0
        with transaction.atomic():
            for employee in employees:
                role = roles[ROLE_BY_CATEGORY[employee.work_category]]
                if EmployeeAccess.objects.filter(
                    employee=employee,
                    role=role,
                    is_active=True,
                ).exists():
                    skipped_existing += 1
                    continue
                if options['commit']:
                    _access, code, _created = issue_employee_access(
                        employee=employee,
                        role=role,
                        actor=None,
                    )
                    created_rows.append((
                        employee.full_name,
                        employee.personnel_number,
                        employee.phone,
                        role.name,
                        code,
                    ))
            if not options['commit']:
                transaction.set_rollback(True)

        if options['commit']:
            output_path = Path(options['output'])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open('w', encoding='utf-8-sig', newline='') as target:
                writer = csv.writer(target, delimiter=';')
                writer.writerow(['Сотрудник', 'Табельный номер', 'Телефон', 'Роль', 'Первичный PIN'])
                writer.writerows(created_rows)

        summary = {
            'mode': 'commit' if options['commit'] else 'dry-run',
            'eligible': len(employees),
            'would_create': len(employees) - skipped_existing if not options['commit'] else 0,
            'created': len(created_rows),
            'skipped_existing': skipped_existing,
        }
        self.stdout.write(json.dumps(summary, ensure_ascii=False, sort_keys=True))
