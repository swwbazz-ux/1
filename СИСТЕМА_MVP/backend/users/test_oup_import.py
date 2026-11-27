import csv
import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from assignments.models import AssignmentStatus, EquipmentAssignment, WorkShiftType
from references.models import Equipment, EquipmentType
from users.models import AdminActionLog, Employee, EmployeeAccess, PersonnelDepartment, PersonnelPosition, ProductionSpecialization, Role, WorkSchedule
from users.management.commands.import_oup_employees import REQUIRED_COLUMNS


class OupEmployeeImportCommandTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.csv_path = Path(self.temp_dir.name) / 'employees.csv'
        role, _ = Role.objects.get_or_create(code='driver', defaults={'name': 'Водитель самосвала'})
        self.truck_specialization = ProductionSpecialization.objects.get(code='haul_truck_driver')
        self.truck_specialization.access_role = role
        self.truck_specialization.save(update_fields=['access_role'])

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_rows(self, rows):
        fieldnames = [
            'full_name', 'personnel_number', 'position', 'hired_at', 'rotation',
            'birth_date', 'department', 'phone', 'work_category', 'source_state', 'base_specialization',
        ]
        with self.csv_path.open('w', encoding='utf-8', newline='') as target:
            writer = csv.DictWriter(target, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def base_row(self, **overrides):
        row = {
            'full_name': 'Иванов Иван Иванович',
            'personnel_number': '101',
            'position': PersonnelPosition.objects.get(code='position_001').name,
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

    def test_import_without_sex_column_preserves_explicit_value(self):
        employee = Employee.objects.create(
            full_name='Иванов Иван Иванович',
            personnel_number='101',
            phone='+79000000001',
            sex=Employee.Sex.MALE,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.write_rows([self.base_row(phone='+7 900 000-00-02')])

        call_command('import_oup_employees', str(self.csv_path), '--commit')

        employee.refresh_from_db()
        self.assertNotIn('sex', REQUIRED_COLUMNS)
        self.assertEqual(employee.sex, Employee.Sex.MALE)

    def test_import_resolves_unique_specialization_and_requires_an_explicit_ambiguous_choice(self):
        generic_position = PersonnelPosition.objects.get(code='position_003')
        self.write_rows([
            self.base_row(work_category=Employee.WorkCategory.OTHER),
            self.base_row(personnel_number='102', full_name='Петров Пётр Петрович', position=generic_position.name,
                          work_category=Employee.WorkCategory.OTHER, phone='+79000000002'),
            self.base_row(personnel_number='103', full_name='Сидоров Сидор Сидорович', position=generic_position.name,
                          work_category=Employee.WorkCategory.OTHER, base_specialization='cargo_kdm_driver',
                          phone='+79000000003'),
        ])
        output = StringIO()
        call_command('import_oup_employees', str(self.csv_path), '--commit', stdout=output)
        employee = Employee.objects.get(personnel_number='101')
        self.assertEqual(employee.personnel_position.code, 'position_001')
        self.assertEqual(employee.base_specialization, self.truck_specialization)
        self.assertEqual(employee.work_category, Employee.WorkCategory.DRIVER)
        self.assertFalse(Employee.objects.filter(personnel_number='102').exists())
        self.assertEqual(json.loads(output.getvalue())['skipped_rows'][0]['row'], 3)
        self.assertEqual(Employee.objects.get(personnel_number='103').base_specialization.code, 'cargo_kdm_driver')
        self.assertFalse(EmployeeAccess.objects.exists())

    def test_reconciliation_is_read_only_by_default_and_only_fills_unambiguous_gaps(self):
        exact = Employee.objects.create(
            full_name='Однозначный сотрудник', personnel_position=PersonnelPosition.objects.get(code='position_001'),
            status=Employee.Status.ACTIVE,
        )
        ambiguous = Employee.objects.create(
            full_name='Требует уточнения', personnel_position=PersonnelPosition.objects.get(code='position_003'),
            status=Employee.Status.ACTIVE,
        )
        output = StringIO()
        call_command('reconcile_employee_specializations', stdout=output)
        preview = json.loads(output.getvalue())
        self.assertEqual([row['full_name'] for row in preview['fill']], [exact.full_name])
        self.assertEqual([row['full_name'] for row in preview['review']], [ambiguous.full_name])
        exact.refresh_from_db()
        self.assertIsNone(exact.base_specialization_id)

        applied = StringIO()
        call_command('reconcile_employee_specializations', '--commit', stdout=applied)
        self.assertEqual(json.loads(applied.getvalue())['updated'], 1)
        exact.refresh_from_db()
        ambiguous.refresh_from_db()
        self.assertEqual(exact.base_specialization.code, 'haul_truck_driver')
        self.assertIsNone(ambiguous.base_specialization_id)
        self.assertFalse(EmployeeAccess.objects.exists())

        repeated = StringIO()
        call_command('reconcile_employee_specializations', '--commit', stdout=repeated)
        self.assertEqual(json.loads(repeated.getvalue())['updated'], 0)

    def test_bulk_import_keeps_all_work_profile_fields_together_when_employee_has_assignment(self):
        position = PersonnelPosition.objects.get(code='position_001')
        employee = Employee.objects.create(
            full_name='Иванов Иван Иванович', personnel_number='101', phone='+79000000001',
            position=position.name, personnel_position=position, base_specialization=self.truck_specialization,
            work_category=Employee.WorkCategory.DRIVER, status=Employee.Status.ACTIVE,
        )
        equipment_kind = EquipmentType.objects.create(name='Самосвал для проверки импорта')
        equipment = Equipment.objects.create(equipment_type=equipment_kind, garage_number='IMPORT-01')
        EquipmentAssignment.objects.create(
            employee=employee, equipment=equipment, role=self.truck_specialization.access_role,
            status=AssignmentStatus.ACCEPTED, shift_type=WorkShiftType.SHIFT_1,
        )
        row = self.base_row(position=PersonnelPosition.objects.get(code='position_002').name,
                            phone='+79000000101', work_category=Employee.WorkCategory.OTHER)
        self.write_rows([row])
        output = StringIO()
        call_command('import_oup_employees', str(self.csv_path), '--commit', stdout=output)
        employee.refresh_from_db()
        self.assertEqual(json.loads(output.getvalue())['work_profile_preserved_rows'], [2])
        self.assertEqual(employee.phone, '+79000000101')
        self.assertEqual(employee.position, position.name)
        self.assertEqual(employee.personnel_position, position)
        self.assertEqual(employee.base_specialization, self.truck_specialization)
        self.assertEqual(employee.work_category, Employee.WorkCategory.DRIVER)
