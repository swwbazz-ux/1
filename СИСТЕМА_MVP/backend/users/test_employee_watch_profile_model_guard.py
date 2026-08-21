from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from .models import (
    Employee,
    EmployeeWatchProfileManager,
    WatchComposition,
    WorkSchedule,
)


class EmployeeWatchProfileModelGuardTests(TestCase):
    error_code = 'users.employee.watch_profile_immutable'

    def setUp(self):
        self.schedule = WorkSchedule.objects.create(
            code='guard-schedule-1',
            name='Guard schedule 1',
            brigade_count=4,
        )
        self.other_schedule = WorkSchedule.objects.create(
            code='guard-schedule-2',
            name='Guard schedule 2',
            brigade_count=4,
        )
        self.composition = WatchComposition.objects.create(
            code='guard-composition-1',
            name='Guard composition 1',
        )
        self.other_composition = WatchComposition.objects.create(
            code='guard-composition-2',
            name='Guard composition 2',
        )
        self.employee = Employee.objects.create(
            full_name='Сотрудник С Исходным Профилем',
            work_schedule=self.schedule,
            brigade_number=1,
            watch_composition=self.composition,
            rotation='Guard schedule 1 Бригада №1',
        )

    def assert_guarded(self, callback):
        with self.assertRaises(ValidationError) as raised:
            callback()
        self.assertEqual(raised.exception.code, self.error_code)

    def test_first_insert_and_force_insert_accept_initial_profile(self):
        employee = Employee(
            full_name='Первичный INSERT',
            work_schedule=self.schedule,
            brigade_number=2,
            watch_composition=self.composition,
            rotation='Первичный профиль',
        )

        employee.save(force_insert=True)

        saved = Employee.objects.get(pk=employee.pk)
        self.assertEqual(saved.work_schedule_id, self.schedule.pk)
        self.assertEqual(saved.brigade_number, 2)
        self.assertEqual(saved.watch_composition_id, self.composition.pk)
        self.assertEqual(saved.rotation, 'Первичный профиль')

    def test_create_and_bulk_create_accept_initial_profile(self):
        created = Employee.objects.create(
            full_name='Создан Через Manager',
            work_schedule=self.schedule,
            brigade_number=3,
            watch_composition=self.composition,
            rotation='Manager create',
        )
        bulk = Employee(
            full_name='Создан Массово',
            work_schedule=self.other_schedule,
            brigade_number=4,
            watch_composition=self.other_composition,
            rotation='Bulk create',
        )

        Employee.objects.bulk_create([bulk])

        self.assertEqual(created.work_schedule_id, self.schedule.pk)
        self.assertEqual(
            Employee.objects.get(pk=bulk.pk).watch_composition_id,
            self.other_composition.pk,
        )

    def test_get_or_create_preserves_create_and_existing_contracts(self):
        created, was_created = Employee.objects.get_or_create(
            personnel_number='GUARD-GET-CREATE',
            defaults={
                'full_name': 'Get Or Create Новый',
                'work_schedule': self.schedule,
                'brigade_number': 2,
                'watch_composition': self.composition,
                'rotation': 'Get create',
            },
        )
        existing, existing_created = Employee.objects.get_or_create(
            pk=self.employee.pk,
            defaults={
                'work_schedule': self.other_schedule,
                'brigade_number': 4,
            },
        )

        self.assertTrue(was_created)
        self.assertEqual(created.work_schedule_id, self.schedule.pk)
        self.assertFalse(existing_created)
        self.assertEqual(existing.work_schedule_id, self.schedule.pk)
        self.assertEqual(existing.brigade_number, 1)

    def test_update_or_create_create_branch_accepts_initial_profile(self):
        employee, created = Employee.objects.update_or_create(
            personnel_number='GUARD-UPDATE-CREATE',
            defaults={
                'full_name': 'Update Or Create Новый',
                'work_schedule': self.schedule,
                'brigade_number': 2,
                'watch_composition': self.composition,
                'rotation': 'Update create',
            },
        )

        self.assertTrue(created)
        self.assertEqual(employee.watch_composition_id, self.composition.pk)

    def test_save_blocks_every_protected_field_and_fk_attname(self):
        mutations = (
            ('work_schedule', self.other_schedule),
            ('work_schedule_id', self.other_schedule.pk),
            ('brigade_number', 2),
            ('watch_composition', self.other_composition),
            ('watch_composition_id', self.other_composition.pk),
            ('rotation', 'Подложная вахта'),
        )
        for field_name, value in mutations:
            with self.subTest(field_name=field_name):
                employee = Employee.objects.get(pk=self.employee.pk)
                setattr(employee, field_name, value)
                self.assert_guarded(employee.save)

        self.employee.refresh_from_db()
        self.assertEqual(self.employee.work_schedule_id, self.schedule.pk)
        self.assertEqual(self.employee.brigade_number, 1)
        self.assertEqual(self.employee.watch_composition_id, self.composition.pk)
        self.assertEqual(self.employee.rotation, 'Guard schedule 1 Бригада №1')

    def test_save_update_fields_blocks_protected_field_and_attname(self):
        employee = Employee.objects.get(pk=self.employee.pk)
        employee.work_schedule = self.other_schedule
        self.assert_guarded(lambda: employee.save(update_fields=['work_schedule']))

        employee = Employee.objects.get(pk=self.employee.pk)
        employee.watch_composition_id = self.other_composition.pk
        self.assert_guarded(
            lambda: employee.save(update_fields=['watch_composition_id'])
        )

    def test_allowed_update_fields_restore_dirty_profile_in_database_and_instance(self):
        employee = Employee.objects.get(pk=self.employee.pk)
        employee.full_name = 'Разрешённое Новое Имя'
        employee.work_schedule = self.other_schedule
        employee.brigade_number = 4
        employee.watch_composition = self.other_composition
        employee.rotation = 'Ложное значение только в памяти'

        employee.save(update_fields=['full_name'])

        self.assertEqual(employee.work_schedule_id, self.schedule.pk)
        self.assertEqual(employee.brigade_number, 1)
        self.assertEqual(employee.watch_composition_id, self.composition.pk)
        self.assertEqual(employee.rotation, 'Guard schedule 1 Бригада №1')
        saved = Employee.objects.get(pk=employee.pk)
        self.assertEqual(saved.full_name, 'Разрешённое Новое Имя')
        self.assertEqual(saved.work_schedule_id, self.schedule.pk)
        self.assertEqual(saved.watch_composition_id, self.composition.pk)

    def test_queryset_update_blocks_protected_names_and_attnames(self):
        mutations = (
            {'work_schedule': self.other_schedule},
            {'work_schedule_id': self.other_schedule.pk},
            {'brigade_number': 2},
            {'watch_composition': self.other_composition},
            {'watch_composition_id': self.other_composition.pk},
            {'rotation': 'Подложная вахта'},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assert_guarded(
                    lambda mutation=mutation: Employee.objects
                    .filter(pk=self.employee.pk)
                    .update(**mutation)
                )

    def test_base_manager_has_same_update_guard(self):
        self.assertIsInstance(Employee._base_manager, EmployeeWatchProfileManager)
        self.assertFalse(Employee.objects.use_in_migrations)
        self.assert_guarded(
            lambda: Employee._base_manager
            .filter(pk=self.employee.pk)
            .update(rotation='Обход через base manager')
        )

    def test_bulk_update_blocks_profile_and_allows_other_fields(self):
        employee = Employee.objects.get(pk=self.employee.pk)
        employee.brigade_number = 3
        self.assert_guarded(
            lambda: Employee.objects.bulk_update([employee], ['brigade_number'])
        )

        employee = Employee.objects.get(pk=self.employee.pk)
        employee.phone = '+79990000001'
        self.assertEqual(Employee.objects.bulk_update([employee], ['phone']), 1)
        self.assertEqual(
            Employee.objects.get(pk=self.employee.pk).phone,
            '+79990000001',
        )

    def test_queryset_update_allows_other_fields(self):
        updated = Employee.objects.filter(pk=self.employee.pk).update(
            phone='+79990000002',
        )

        self.assertEqual(updated, 1)
        self.assertEqual(
            Employee.objects.get(pk=self.employee.pk).phone,
            '+79990000002',
        )

    def test_update_or_create_existing_profile_change_is_atomic(self):
        original_name = self.employee.full_name

        self.assert_guarded(
            lambda: Employee.objects.update_or_create(
                pk=self.employee.pk,
                defaults={
                    'full_name': 'Не Должно Сохраниться',
                    'work_schedule': self.other_schedule,
                },
            )
        )

        self.employee.refresh_from_db()
        self.assertEqual(self.employee.full_name, original_name)
        self.assertEqual(self.employee.work_schedule_id, self.schedule.pk)

    def test_repeating_same_profile_values_is_allowed(self):
        self.employee.phone = '+79990000003'
        self.employee.save()
        updated, created = Employee.objects.update_or_create(
            pk=self.employee.pk,
            defaults={
                'phone': '+79990000004',
                'work_schedule': self.schedule,
                'brigade_number': 1,
                'watch_composition': self.composition,
                'rotation': 'Guard schedule 1 Бригада №1',
            },
        )

        self.assertFalse(created)
        self.assertEqual(updated.phone, '+79990000004')

    def test_raw_fixture_style_insert_remains_available(self):
        now = timezone.now()
        employee = Employee(
            full_name='Raw Fixture Employee',
            work_schedule=self.schedule,
            brigade_number=1,
            watch_composition=self.composition,
            rotation='Raw fixture profile',
            created_at=now,
            updated_at=now,
        )

        employee.save_base(raw=True, force_insert=True)

        saved = Employee.objects.get(pk=employee.pk)
        self.assertEqual(saved.rotation, 'Raw fixture profile')
        self.assertEqual(saved.watch_composition_id, self.composition.pk)


class EmployeeWatchProfileGuardMigrationTests(TransactionTestCase):
    serialized_rollback = True

    def test_migration_is_schema_only_and_cycles_forward_reverse_forward(self):
        migration_path = (
            Path(__file__).resolve().parent
            / 'migrations'
            / '0018_employee_watch_profile_guard.py'
        )
        source = migration_path.read_text(encoding='utf-8')
        self.assertIn("('users', '0017_employee_sex')", source)
        self.assertNotIn('migrations.RunPython', source)
        self.assertNotIn('migrations.RunSQL', source)

        for target in (
            ('users', '0017_employee_sex'),
            ('users', '0018_employee_watch_profile_guard'),
            ('users', '0017_employee_sex'),
            ('users', '0018_employee_watch_profile_guard'),
        ):
            executor = MigrationExecutor(connection)
            executor.migrate([target])

        executor = MigrationExecutor(connection)
        apps = executor.loader.project_state([
            ('users', '0018_employee_watch_profile_guard'),
        ]).apps
        HistoricalEmployee = apps.get_model('users', 'Employee')
        self.assertEqual(HistoricalEmployee._meta.base_manager_name, 'objects')
