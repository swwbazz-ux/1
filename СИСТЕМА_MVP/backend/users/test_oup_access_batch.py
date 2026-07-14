import csv
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from users.models import AdminActionLog, Employee, EmployeeAccess, Role


class OupImportedAccessBatchTests(TestCase):
    def setUp(self):
        self.driver_role = Role.objects.create(code='driver', name='Водитель самосвала')
        Role.objects.create(code='excavator_operator', name='Машинист экскаватора')
        self.employee = Employee.objects.create(
            full_name='Импортированный Водитель',
            personnel_number='501',
            phone='+79000000501',
            work_category=Employee.WorkCategory.DRIVER,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        AdminActionLog.objects.create(
            action='ОУП: создан сотрудник массовым импортом',
            object_type='Employee',
            object_id=str(self.employee.id),
            object_repr=str(self.employee),
            comment='Импорт 15.07',
        )

    def test_dry_run_does_not_issue_access(self):
        call_command('issue_oup_import_accesses', '--source-label', 'Импорт 15.07')
        self.assertFalse(EmployeeAccess.objects.exists())

    def test_commit_issues_access_and_writes_register(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'pins.csv'
            call_command(
                'issue_oup_import_accesses',
                '--source-label', 'Импорт 15.07',
                '--commit',
                '--output', str(output),
            )
            issued_access = EmployeeAccess.objects.get(employee=self.employee, role=self.driver_role)
            self.assertEqual(issued_access.status, EmployeeAccess.Status.NOT_ACTIVATED)
            with output.open(encoding='utf-8-sig', newline='') as source:
                rows = list(csv.reader(source, delimiter=';'))
            self.assertEqual(rows[1][0], self.employee.full_name)
            self.assertEqual(rows[1][4], issued_access.access_code)
