import csv
import json
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from users.models import AdminActionLog, Employee, PersonnelDepartment, WorkSchedule


class OupEmployeeImportCommandTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.csv_path = Path(self.temp_dir.name) / 'employees.csv'

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_rows(self, rows):
        fieldnames = [
            'full_name', 'personnel_number', 'position', 'hired_at', 'rotation',
            'birth_date', 'department', 'phone', 'work_category', 'source_state',
        ]
        with self.csv_path.open('w', encoding='utf-8', newline='') as target:
            writer = csv.DictWriter(target, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def base_row(self, **overrides):
        row = {
            'full_name': 'Иванов Иван Иванович',
            'personnel_number': '101',
            'position': 'Водитель автомобиля грузового',
            'hired_at': '01.07.2026',
            'rotation': 'График сменности №12 (2-х сменный, 4-х бригадный)(30 дней, 30 ночей) Бригада №1',
            'birth_date': '02.03.1980',
            'department': 'Горный участок №2',
            'phone': '+7 900 000-00-01',
            'work_category': Employee.WorkCategory.DRIVER,
            'source_state': 'Работа',
        }
        row.update(overrides)
        return row

    def test_dry_run_does_not_change_database(self):
        self.write_rows([self.base_row()])
        output = tempfile.SpooledTemporaryFile(mode='w+')
        call_command('import_oup_employees', str(self.csv_path), stdout=output)
        output.seek(0)
        summary = json.loads(output.read())
        self.assertEqual(summary['created'], 1)
        self.assertEqual(Employee.objects.count(), 0)

    def test_commit_creates_updates_and_skips_conflict_and_invalid_phone(self):
        existing = Employee.objects.create(
            full_name='Петров Петр Петрович',
            personnel_number='',
            phone='+79000000002',
            work_category=Employee.WorkCategory.OTHER,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        Employee.objects.create(
            full_name='Сидоров Сидор Сидорович',
            personnel_number='OLD-1',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.write_rows([
            self.base_row(),
            self.base_row(
                full_name='Петров Петр Петрович', personnel_number='102',
                phone='89000000002', work_category=Employee.WorkCategory.OTHER,
            ),
            self.base_row(
                full_name='Сидоров Сидор Сидорович', personnel_number='103',
                phone='+79000000003',
            ),
            self.base_row(
                full_name='Без Телефона', personnel_number='104', phone='',
            ),
        ])
        output = tempfile.SpooledTemporaryFile(mode='w+')
        call_command(
            'import_oup_employees', str(self.csv_path), '--commit',
            '--source-label', 'Тестовый импорт', stdout=output,
        )
        output.seek(0)
        summary = json.loads(output.read())
        existing.refresh_from_db()
        self.assertEqual(summary['created'], 1)
        self.assertEqual(summary['updated'], 1)
        self.assertEqual(summary['skipped'], 2)
        self.assertEqual(existing.personnel_number, '102')
        self.assertEqual(existing.phone, '+79000000002')
        created = Employee.objects.get(personnel_number='101')
        self.assertEqual(created.personnel_department, PersonnelDepartment.objects.get(code='department_001'))
        self.assertEqual(created.work_schedule, WorkSchedule.objects.get(code='schedule_12'))
        self.assertEqual(created.brigade_number, 1)
        self.assertEqual(AdminActionLog.objects.filter(action__contains='массовым импортом').count(), 2)
