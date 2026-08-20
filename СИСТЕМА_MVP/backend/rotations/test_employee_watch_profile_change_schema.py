from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from shifts.models import WatchPeriod
from users.models import (
    Employee,
    EmployeeAccess,
    Role,
    WatchComposition,
    WorkSchedule,
)

from .models import EmployeeWatchProfileChange


class EmployeeWatchProfileChangeSchemaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.role = Role.objects.get(code='timekeeper')
        cls.employee = Employee.objects.create(
            full_name='Сотрудник для изменения профиля',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.actor = Employee.objects.create(
            full_name='Табельщик для изменения профиля',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.access = EmployeeAccess.objects.create(
            employee=cls.actor,
            role=cls.role,
            access_code='schema-timekeeper',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.old_schedule = WorkSchedule.objects.create(
            code='schema-old-schedule',
            name='Прежний график для проверки схемы',
            brigade_count=2,
            is_active=True,
        )
        cls.new_schedule = WorkSchedule.objects.create(
            code='schema-new-schedule',
            name='Новый график для проверки схемы',
            brigade_count=4,
            is_active=True,
        )
        cls.old_composition = WatchComposition.objects.create(
            code='schema-old-composition',
            name='Прежний состав для проверки схемы',
            is_active=True,
        )
        cls.new_composition = WatchComposition.objects.create(
            code='schema-new-composition',
            name='Новый состав для проверки схемы',
            is_active=True,
        )
        today = timezone.localdate()
        cls.period = WatchPeriod.objects.create(
            name='Будущий период для проверки схемы',
            watch_composition=cls.new_composition,
            starts_on=today + timedelta(days=30),
            ends_on=today + timedelta(days=59),
            is_active=True,
        )
        cls.second_period = WatchPeriod.objects.create(
            name='Второй будущий период для проверки схемы',
            watch_composition=cls.new_composition,
            starts_on=today + timedelta(days=60),
            ends_on=today + timedelta(days=89),
            is_active=True,
        )

    def _change(self, **overrides):
        values = {
            'employee': self.employee,
            'effective_watch_period': self.period,
            'effective_on': self.period.starts_on,
            'version_number': 1,
            'old_work_schedule': self.old_schedule,
            'old_brigade_number': 1,
            'old_watch_composition': self.old_composition,
            'new_work_schedule': self.new_schedule,
            'new_brigade_number': 2,
            'new_watch_composition': self.new_composition,
            'basis_kind': EmployeeWatchProfileChange.BasisKind.EMPLOYEE_APPLICATION,
            'basis_number': 'ЗАЯВЛЕНИЕ-1',
            'basis_date': timezone.localdate(),
            'basis': 'Заявление сотрудника о смене рабочего профиля.',
            'source_snapshot': {'schema': 1, 'basis_number': 'ЗАЯВЛЕНИЕ-1'},
            'source_fingerprint': 'a' * 64,
            'created_by_access': self.access,
            'status': EmployeeWatchProfileChange.Status.DRAFT,
        }
        values.update(overrides)
        return EmployeeWatchProfileChange(**values)

    def _change_kwargs(self, **overrides):
        row = self._change(**overrides)
        return {
            field.name: getattr(row, field.name)
            for field in EmployeeWatchProfileChange._meta.fields
            if field.name not in {'id', 'created_at'}
        }

    @staticmethod
    def _trusted_insert(*rows):
        models.QuerySet.bulk_create(
            EmployeeWatchProfileChange._base_manager.all(),
            list(rows),
        )

    def _assert_integrity_error(self, row):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._trusted_insert(row)

    def _assert_public_write_forbidden(self, operation):
        with self.assertRaises(ValidationError) as caught:
            operation()
        self.assertEqual(
            caught.exception.code,
            'rotations.employee_watch_profile_change.public_write_forbidden',
        )

    def test_fields_nullability_related_names_and_protect_policy(self):
        model = EmployeeWatchProfileChange
        expected = {
            'employee': (False, 'watch_profile_changes'),
            'effective_watch_period': (False, 'employee_watch_profile_changes'),
            'supersedes': (True, 'replacement'),
            'old_work_schedule': (True, 'employee_watch_profile_changes_from_schedule'),
            'old_watch_composition': (True, 'employee_watch_profile_changes_from_composition'),
            'new_work_schedule': (False, 'employee_watch_profile_changes_to_schedule'),
            'new_watch_composition': (False, 'employee_watch_profile_changes_to_composition'),
            'created_by_access': (False, 'created_employee_watch_profile_changes'),
            'applied_by_access': (True, 'applied_employee_watch_profile_changes'),
            'superseded_by_access': (True, 'superseded_employee_watch_profile_changes'),
            'cancelled_by_access': (True, 'cancelled_employee_watch_profile_changes'),
        }
        for field_name, (nullable, related_name) in expected.items():
            with self.subTest(field=field_name):
                field = model._meta.get_field(field_name)
                self.assertEqual(field.null, nullable)
                self.assertEqual(field.remote_field.related_name, related_name)
                self.assertIs(field.remote_field.on_delete, models.PROTECT)
        self.assertFalse(model._meta.get_field('source_snapshot').has_default())
        self.assertFalse(model._meta.get_field('source_fingerprint').has_default())
        self.assertFalse(model._meta.get_field('source_fingerprint').unique)
        self.assertEqual(model._meta.get_field('source_fingerprint').max_length, 64)

    def test_enum_values_are_closed(self):
        self.assertEqual(
            set(EmployeeWatchProfileChange.BasisKind.values),
            {'employee_application', 'official_order', 'other_official_document'},
        )
        self.assertEqual(
            set(EmployeeWatchProfileChange.Status.values),
            {'draft', 'applied', 'superseded', 'cancelled'},
        )

    def test_version_number_is_unique_inside_employee_and_period(self):
        self._trusted_insert(self._change())
        self._assert_integrity_error(self._change(source_fingerprint='b' * 64))

    def test_only_one_applied_version_exists_for_employee_and_period(self):
        now = timezone.now()
        self._trusted_insert(self._change(
            status=EmployeeWatchProfileChange.Status.APPLIED,
            applied_by_access=self.access,
            applied_at=now,
        ))
        self._assert_integrity_error(self._change(
            version_number=2,
            status=EmployeeWatchProfileChange.Status.APPLIED,
            applied_by_access=self.access,
            applied_at=now,
        ))

    def test_different_periods_allow_their_own_applied_versions(self):
        now = timezone.now()
        first = self._change(
            status=EmployeeWatchProfileChange.Status.APPLIED,
            applied_by_access=self.access,
            applied_at=now,
        )
        second = self._change(
            effective_watch_period=self.second_period,
            effective_on=self.second_period.starts_on,
            status=EmployeeWatchProfileChange.Status.APPLIED,
            applied_by_access=self.access,
            applied_at=now,
        )
        self._trusted_insert(first, second)
        self.assertEqual(EmployeeWatchProfileChange._base_manager.count(), 2)

    def test_supersedes_is_one_to_one(self):
        original = self._change()
        self._trusted_insert(original)
        replacement = self._change(
            version_number=2,
            supersedes=original,
            source_fingerprint='b' * 64,
        )
        self._trusted_insert(replacement)
        self._assert_integrity_error(self._change(
            version_number=3,
            supersedes=original,
            source_fingerprint='c' * 64,
        ))

    def test_each_valid_status_audit_shape_is_accepted(self):
        now = timezone.now()
        rows = [
            self._change(version_number=1),
            self._change(
                version_number=2,
                status=EmployeeWatchProfileChange.Status.APPLIED,
                applied_by_access=self.access,
                applied_at=now,
            ),
            self._change(
                version_number=3,
                status=EmployeeWatchProfileChange.Status.SUPERSEDED,
                applied_by_access=self.access,
                applied_at=now,
                superseded_by_access=self.access,
                superseded_at=now,
            ),
            self._change(
                version_number=4,
                status=EmployeeWatchProfileChange.Status.CANCELLED,
                cancelled_by_access=self.access,
                cancelled_at=now,
            ),
        ]
        self._trusted_insert(*rows)
        self.assertEqual(EmployeeWatchProfileChange._base_manager.count(), 4)

    def test_invalid_status_audit_shapes_are_blocked_by_database(self):
        now = timezone.now()
        invalid_rows = [
            self._change(applied_at=now),
            self._change(
                status=EmployeeWatchProfileChange.Status.APPLIED,
                applied_at=now,
            ),
            self._change(
                status=EmployeeWatchProfileChange.Status.SUPERSEDED,
                applied_by_access=self.access,
                applied_at=now,
                superseded_at=now,
            ),
            self._change(
                status=EmployeeWatchProfileChange.Status.CANCELLED,
                cancelled_by_access=self.access,
            ),
        ]
        for index, row in enumerate(invalid_rows, start=1):
            row.version_number = index
            with self.subTest(status=row.status, version=index):
                self._assert_integrity_error(row)

    def test_version_status_basis_and_brigade_checks_are_database_enforced(self):
        invalid_rows = [
            self._change(version_number=0),
            self._change(version_number=2, old_brigade_number=0),
            self._change(version_number=3, new_brigade_number=0),
            self._change(version_number=4, status='unknown'),
            self._change(version_number=5, basis_kind='unknown'),
            self._change(version_number=7, basis_number=''),
            self._change(version_number=8, basis=''),
        ]
        for row in invalid_rows:
            with self.subTest(version=row.version_number):
                self._assert_integrity_error(row)
        self._trusted_insert(self._change(
            version_number=6,
            old_brigade_number=None,
            new_brigade_number=None,
        ))

    def test_same_fingerprint_is_allowed_for_different_versions(self):
        self._trusted_insert(
            self._change(version_number=1),
            self._change(version_number=2),
        )
        self.assertEqual(EmployeeWatchProfileChange._base_manager.count(), 2)

    def test_public_create_paths_are_closed(self):
        self._assert_public_write_forbidden(self._change().save)
        self._assert_public_write_forbidden(
            lambda: EmployeeWatchProfileChange.objects.create(
                **self._change_kwargs(),
            ),
        )
        self._assert_public_write_forbidden(
            lambda: EmployeeWatchProfileChange.objects.get_or_create(
                employee=self.employee,
                effective_watch_period=self.period,
                version_number=1,
                defaults={
                    key: value
                    for key, value in self._change_kwargs().items()
                    if key not in {'employee', 'effective_watch_period', 'version_number'}
                },
            ),
        )
        self._assert_public_write_forbidden(
            lambda: EmployeeWatchProfileChange.objects.update_or_create(
                employee=self.employee,
                effective_watch_period=self.period,
                version_number=1,
                defaults={
                    'effective_on': self.period.starts_on,
                    'new_work_schedule': self.new_schedule,
                    'new_watch_composition': self.new_composition,
                    'basis_kind': EmployeeWatchProfileChange.BasisKind.EMPLOYEE_APPLICATION,
                    'basis_number': 'ЗАЯВЛЕНИЕ-1',
                    'basis_date': timezone.localdate(),
                    'basis': 'Основание.',
                    'source_snapshot': {'schema': 1},
                    'source_fingerprint': 'a' * 64,
                    'created_by_access': self.access,
                },
            ),
        )
        self._assert_public_write_forbidden(
            lambda: EmployeeWatchProfileChange.objects.bulk_create([self._change()]),
        )
        self.assertFalse(EmployeeWatchProfileChange._base_manager.exists())

    def test_public_update_and_delete_paths_are_closed(self):
        row = self._change()
        self._trusted_insert(row)
        row.basis = 'Подменённое основание.'
        self._assert_public_write_forbidden(row.save)
        self._assert_public_write_forbidden(
            lambda: EmployeeWatchProfileChange.objects.filter(pk=row.pk).update(
                basis='Подменённое основание.',
            ),
        )
        self._assert_public_write_forbidden(
            lambda: EmployeeWatchProfileChange.objects.bulk_update([row], ['basis']),
        )
        self._assert_public_write_forbidden(row.delete)
        self._assert_public_write_forbidden(
            lambda: EmployeeWatchProfileChange.objects.filter(pk=row.pk).delete(),
        )
        row.refresh_from_db()
        self.assertEqual(row.basis, 'Заявление сотрудника о смене рабочего профиля.')

    def test_historical_foreign_keys_are_protected(self):
        row = self._change()
        self._trusted_insert(row)
        with self.assertRaises(ProtectedError):
            self.period.delete()
        with self.assertRaises(ProtectedError):
            self.new_schedule.delete()
        with self.assertRaises(ProtectedError):
            self.new_composition.delete()
        with self.assertRaises(ProtectedError):
            self.access.delete()

    def test_existing_employee_does_not_receive_historical_row(self):
        untouched = Employee.objects.create(
            full_name='Сотрудник без выдуманной истории',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.assertFalse(
            EmployeeWatchProfileChange._base_manager.filter(employee=untouched).exists(),
        )

    def test_migration_is_schema_only_and_has_minimal_dependency(self):
        migration_path = (
            Path(__file__).resolve().parent
            / 'migrations'
            / '0008_employee_watch_profile_change.py'
        )
        source = migration_path.read_text(encoding='utf-8')
        self.assertIn("('rotations', '0007_arrival_roster_routing')", source)
        self.assertNotIn('migrations.RunPython', source)
        self.assertNotIn('migrations.RunSQL', source)
        self.assertNotIn('bulk_create', source)
        self.assertNotIn('objects.create', source)


class EmployeeWatchProfileChangeMigrationCycleTests(TransactionTestCase):
    serialized_rollback = True

    def test_forward_reverse_forward_cycle(self):
        table_name = EmployeeWatchProfileChange._meta.db_table
        targets = (
            ('rotations', '0007_arrival_roster_routing'),
            ('rotations', '0008_employee_watch_profile_change'),
            ('rotations', '0007_arrival_roster_routing'),
            ('rotations', '0008_employee_watch_profile_change'),
        )
        expected_presence = (False, True, False, True)
        for target, should_exist in zip(targets, expected_presence, strict=True):
            executor = MigrationExecutor(connection)
            executor.migrate([target])
            self.assertEqual(
                table_name in connection.introspection.table_names(),
                should_exist,
            )
