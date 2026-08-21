import csv
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from rotations.models import EmployeeWatchProfileChange

from .models import Employee, WatchComposition, WorkSchedule


class OupImportWatchProfilePermissionTests(TestCase):
    fieldnames = [
        'full_name',
        'personnel_number',
        'position',
        'hired_at',
        'rotation',
        'birth_date',
        'department',
        'phone',
        'work_category',
        'work_schedule',
        'brigade_number',
        'watch_composition',
    ]

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.csv_path = Path(self.temp_dir.name) / 'employees.csv'
        self.schedule_a = WorkSchedule.objects.create(
            code='oup_import_schedule_a',
            name='График импорта А',
            brigade_count=2,
        )
        self.schedule_b = WorkSchedule.objects.create(
            code='oup_import_schedule_b',
            name='График импорта Б',
            brigade_count=4,
        )
        self.composition_a = WatchComposition.objects.create(
            code='oup_import_composition_a',
            name='Состав импорта А',
        )
        self.composition_b = WatchComposition.objects.create(
            code='oup_import_composition_b',
            name='Состав импорта Б',
        )
        self.employee = Employee.objects.create(
            full_name='Иванов Иван Иванович',
            personnel_number='101',
            position='Исходная должность',
            hired_at='2026-07-01',
            birth_date='1980-03-02',
            department='Исходное подразделение',
            phone='+79000000001',
            work_category=Employee.WorkCategory.OTHER,
            work_schedule=self.schedule_a,
            brigade_number=1,
            watch_composition=self.composition_a,
            rotation=f'{self.schedule_a.name} Бригада №1',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_rows(self, rows):
        with self.csv_path.open('w', encoding='utf-8', newline='') as target:
            writer = csv.DictWriter(target, fieldnames=self.fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def base_row(self, **overrides):
        row = {
            'full_name': self.employee.full_name,
            'personnel_number': self.employee.personnel_number,
            'position': 'Обновлённая должность',
            'hired_at': '01.07.2026',
            'rotation': 'Подложный текст из файла',
            'birth_date': '02.03.1980',
            'department': 'Обновлённое подразделение',
            'phone': '+7 900 000-00-02',
            'work_category': Employee.WorkCategory.OTHER,
            'work_schedule': self.schedule_a.name,
            'brigade_number': '1',
            'watch_composition': self.composition_a.name,
        }
        row.update(overrides)
        return row

    def run_commit(self):
        call_command('import_oup_employees', str(self.csv_path), '--commit')

    def assert_existing_profile_unchanged(self):
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.work_schedule_id, self.schedule_a.pk)
        self.assertEqual(self.employee.brigade_number, 1)
        self.assertEqual(self.employee.watch_composition_id, self.composition_a.pk)
        self.assertEqual(self.employee.rotation, f'{self.schedule_a.name} Бригада №1')
        self.assertFalse(EmployeeWatchProfileChange.objects.exists())

    def assert_conflict_rolls_back(self, **overrides):
        self.write_rows([self.base_row(**overrides)])

        with self.assertRaises(CommandError) as caught:
            self.run_commit()

        self.assert_existing_profile_unchanged()
        self.assertEqual(self.employee.position, 'Исходная должность')
        self.assertEqual(self.employee.department, 'Исходное подразделение')
        return str(caught.exception)

    def test_new_employee_receives_initial_profile_and_server_built_rotation(self):
        self.write_rows([self.base_row(
            full_name='Петров Петр Петрович',
            personnel_number='202',
            rotation=f'{self.schedule_b.name} Бригада №3',
            work_schedule='',
            brigade_number='',
            watch_composition=self.composition_b.name,
        )])

        self.run_commit()

        employee = Employee.objects.get(personnel_number='202')
        self.assertEqual(employee.work_schedule_id, self.schedule_b.pk)
        self.assertEqual(employee.brigade_number, 3)
        self.assertEqual(employee.watch_composition_id, self.composition_b.pk)
        self.assertEqual(employee.rotation, f'{self.schedule_b.name} Бригада №3')
        self.assertFalse(EmployeeWatchProfileChange.objects.exists())

    def test_matching_profile_allows_personnel_update_and_ignores_raw_rotation(self):
        self.write_rows([self.base_row()])

        self.run_commit()

        self.assert_existing_profile_unchanged()
        self.assertEqual(self.employee.position, 'Обновлённая должность')
        self.assertEqual(self.employee.department, 'Обновлённое подразделение')
        self.assertEqual(self.employee.phone, '+79000000002')

    def test_different_work_schedule_blocks_import(self):
        error = self.assert_conflict_rolls_back(work_schedule=self.schedule_b.name, brigade_number='1')
        self.assertIn('Строка 2', error)
        self.assertIn('work_schedule', error)

    def test_different_brigade_blocks_import(self):
        error = self.assert_conflict_rolls_back(brigade_number='2')
        self.assertIn('brigade_number', error)

    def test_different_watch_composition_blocks_import(self):
        error = self.assert_conflict_rolls_back(watch_composition=self.composition_b.name)
        self.assertIn('watch_composition', error)

    def test_empty_protected_values_do_not_clear_existing_profile(self):
        self.write_rows([self.base_row(
            work_schedule='',
            brigade_number='',
            watch_composition='',
        )])

        self.run_commit()

        self.assert_existing_profile_unchanged()
        self.assertEqual(self.employee.position, 'Обновлённая должность')

    def test_independent_matching_protected_column_is_allowed(self):
        self.write_rows([self.base_row(
            rotation='Неавторитетный текст без структурированного графика',
            work_schedule='',
            brigade_number='1',
            watch_composition='',
        )])

        self.run_commit()

        self.assert_existing_profile_unchanged()
        self.assertEqual(self.employee.position, 'Обновлённая должность')

    def test_conflict_rolls_back_all_rows(self):
        self.write_rows([
            self.base_row(
                full_name='Новый Новый Сотрудник',
                personnel_number='303',
                rotation=f'{self.schedule_a.name} Бригада №1',
                work_schedule='',
                brigade_number='',
                watch_composition='',
            ),
            self.base_row(work_schedule=self.schedule_b.name, brigade_number='1'),
        ])

        with self.assertRaises(CommandError):
            self.run_commit()

        self.assertFalse(Employee.objects.filter(personnel_number='303').exists())
        self.assert_existing_profile_unchanged()
        self.assertEqual(self.employee.position, 'Исходная должность')

    def test_conflict_error_is_safe_and_contains_only_row_and_field_names(self):
        error = self.assert_conflict_rolls_back(
            work_schedule=self.schedule_b.name,
            brigade_number='2',
        )

        self.assertIn('Строка 2', error)
        self.assertIn('work_schedule', error)
        self.assertIn('brigade_number', error)
        self.assertNotIn('+79000000002', error)
        self.assertNotIn('PIN', error)
        self.assertNotIn('Access', error)
        self.assertNotIn('snapshot', error)
