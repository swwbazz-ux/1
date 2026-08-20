from datetime import date, timedelta
from importlib import import_module

from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.db.models.deletion import ProtectedError
from django.db.migrations import RunPython, RunSQL
from django.test import TestCase
from django.utils import timezone

from users.models import WorkSchedule

from .models import (
    WatchPeriod,
    WatchPeriodBrigadePhaseRow,
    WatchPeriodBrigadePhaseVersion,
)


class BrigadePhaseCalendarSchemaTests(TestCase):
    forbidden_code = 'shifts.brigade_phase.public_write_forbidden'
    source_fingerprint = 'a' * 64

    def setUp(self):
        self.work_schedule = WorkSchedule.objects.create(
            code='schedule-12-schema-test',
            name='График № 12 — schema test',
            brigade_count=4,
        )
        self.watch_period = WatchPeriod.objects.create(
            name='Тестовый период календаря фаз',
            starts_on=date(2030, 1, 1),
            ends_on=date(2030, 2, 15),
        )

    def _version(
        self,
        version_number,
        *,
        status=WatchPeriodBrigadePhaseVersion.Status.DRAFT,
        based_on_version=None,
        confirmed_at=None,
        superseded_at=None,
        watch_period=None,
        work_schedule=None,
    ):
        return WatchPeriodBrigadePhaseVersion(
            watch_period=watch_period or self.watch_period,
            work_schedule=work_schedule or self.work_schedule,
            version_number=version_number,
            status=status,
            based_on_version=based_on_version,
            confirmed_at=confirmed_at,
            superseded_at=superseded_at,
            source_snapshot=self._source_snapshot(),
            source_fingerprint=self.source_fingerprint,
        )

    @staticmethod
    def _source_snapshot():
        return {
            'source_kind': 'official_schedule_order',
            'order': {
                'number': 'TEST-ORDER-001',
                'date': '2030-01-01',
                'effective_from': '2030-01-01',
                'document_sha256': 'b' * 64,
            },
            'schedule': {
                'designation': 'TEST-SCHEDULE',
                'document_sha256': 'c' * 64,
            },
        }

    @staticmethod
    def _insert(model_class, instances):
        models.QuerySet.bulk_create(model_class._base_manager.all(), instances)
        return instances

    def _insert_version(self, version):
        self._insert(WatchPeriodBrigadePhaseVersion, [version])
        return WatchPeriodBrigadePhaseVersion._base_manager.get(pk=version.pk)

    def _insert_row(self, row):
        self._insert(WatchPeriodBrigadePhaseRow, [row])
        return WatchPeriodBrigadePhaseRow._base_manager.get(pk=row.pk)

    def _assert_forbidden(self, operation):
        with self.assertRaises(ValidationError) as caught:
            operation()
        self.assertEqual(caught.exception.code, self.forbidden_code)

    def _assert_integrity_error(self, operation):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                operation()

    def test_field_structure_fk_policies_and_enums(self):
        version_fields = WatchPeriodBrigadePhaseVersion._meta
        row_fields = WatchPeriodBrigadePhaseRow._meta

        self.assertIs(
            version_fields.get_field('watch_period').remote_field.on_delete,
            models.PROTECT,
        )
        self.assertIs(
            version_fields.get_field('work_schedule').remote_field.on_delete,
            models.PROTECT,
        )
        self.assertIs(
            version_fields.get_field('based_on_version').remote_field.on_delete,
            models.PROTECT,
        )
        self.assertIs(
            row_fields.get_field('version').remote_field.on_delete,
            models.CASCADE,
        )
        self.assertTrue(version_fields.get_field('created_at').auto_now_add)
        self.assertEqual(
            list(WatchPeriodBrigadePhaseVersion.Status.values),
            ['draft', 'confirmed', 'superseded'],
        )
        self.assertEqual(
            list(WatchPeriodBrigadePhaseRow.Phase.values),
            ['day', 'night', 'off'],
        )
        forbidden_fields = {
            'actor', 'access', 'role', 'created_by_access',
            'confirmed_by_access', 'superseded_by_access',
        }
        self.assertFalse(
            forbidden_fields.intersection(field.name for field in version_fields.fields)
        )

    def test_declared_database_constraint_names_are_complete(self):
        version_constraints = {
            constraint.name
            for constraint in WatchPeriodBrigadePhaseVersion._meta.constraints
        }
        row_constraints = {
            constraint.name
            for constraint in WatchPeriodBrigadePhaseRow._meta.constraints
        }
        self.assertEqual(
            version_constraints,
            {
                'uniq_watch_phase_revision',
                'uniq_watch_phase_confirmed',
                'watch_phase_version_gte_1',
                'watch_phase_status_valid',
                'watch_phase_status_dates',
                'watch_phase_supersede_order',
                'watch_phase_not_self_based',
            },
        )
        self.assertEqual(
            row_constraints,
            {
                'uniq_watch_phase_brigade',
                'watch_phase_brigade_gte_1',
                'watch_phase_value_valid',
            },
        )

    def test_source_provenance_fields_are_required_and_bounded(self):
        meta = WatchPeriodBrigadePhaseVersion._meta
        snapshot_field = meta.get_field('source_snapshot')
        fingerprint_field = meta.get_field('source_fingerprint')

        self.assertIsInstance(snapshot_field, models.JSONField)
        self.assertFalse(snapshot_field.null)
        self.assertFalse(snapshot_field.has_default())
        self.assertFalse(fingerprint_field.null)
        self.assertFalse(fingerprint_field.has_default())
        self.assertEqual(fingerprint_field.max_length, 64)
        self.assertFalse(fingerprint_field.unique)

        self._assert_integrity_error(
            lambda: self._insert_version(
                WatchPeriodBrigadePhaseVersion(
                    watch_period=self.watch_period,
                    work_schedule=self.work_schedule,
                    version_number=1,
                    source_snapshot=None,
                    source_fingerprint=self.source_fingerprint,
                )
            )
        )
        self._assert_integrity_error(
            lambda: self._insert_version(
                WatchPeriodBrigadePhaseVersion(
                    watch_period=self.watch_period,
                    work_schedule=self.work_schedule,
                    version_number=2,
                    source_snapshot=self._source_snapshot(),
                    source_fingerprint=None,
                )
            )
        )

    def test_same_source_fingerprint_is_allowed_for_different_versions(self):
        versions = [self._version(1), self._version(2)]
        self._insert(WatchPeriodBrigadePhaseVersion, versions)

        saved = WatchPeriodBrigadePhaseVersion._base_manager.order_by(
            'version_number'
        )
        self.assertEqual(saved.count(), 2)
        self.assertEqual(
            list(saved.values_list('source_fingerprint', flat=True)),
            [self.source_fingerprint, self.source_fingerprint],
        )

    def test_source_provenance_is_immutable_through_public_writers(self):
        version = self._insert_version(self._version(1))
        original_snapshot = version.source_snapshot
        original_fingerprint = version.source_fingerprint

        version.source_snapshot = {'tampered': True}
        self._assert_forbidden(version.save)
        self._assert_forbidden(
            lambda: WatchPeriodBrigadePhaseVersion.objects.filter(
                pk=version.pk,
            ).update(source_snapshot={'tampered': True})
        )

        version.source_fingerprint = 'd' * 64
        self._assert_forbidden(
            lambda: WatchPeriodBrigadePhaseVersion.objects.bulk_update(
                [version],
                ['source_snapshot', 'source_fingerprint'],
            )
        )

        version.refresh_from_db()
        self.assertEqual(version.source_snapshot, original_snapshot)
        self.assertEqual(version.source_fingerprint, original_fingerprint)

    def test_unique_version_number_and_partial_unique_confirmed(self):
        now = timezone.now()
        self._insert_version(self._version(1))
        self._assert_integrity_error(
            lambda: self._insert_version(self._version(1))
        )

        self._insert_version(
            self._version(
                2,
                status=WatchPeriodBrigadePhaseVersion.Status.CONFIRMED,
                confirmed_at=now,
            )
        )
        self._assert_integrity_error(
            lambda: self._insert_version(
                self._version(
                    3,
                    status=WatchPeriodBrigadePhaseVersion.Status.CONFIRMED,
                    confirmed_at=now,
                )
            )
        )
        self._insert_version(
            self._version(
                3,
                status=WatchPeriodBrigadePhaseVersion.Status.SUPERSEDED,
                confirmed_at=now,
                superseded_at=now,
            )
        )

    def test_version_number_status_and_self_lineage_constraints(self):
        self._assert_integrity_error(
            lambda: self._insert_version(self._version(0))
        )
        self._assert_integrity_error(
            lambda: self._insert_version(self._version(1, status='unknown'))
        )

        version = self._insert_version(self._version(1))
        self._assert_integrity_error(
            lambda: WatchPeriodBrigadePhaseVersion._base_manager.filter(
                pk=version.pk,
            ).update(based_on_version_id=version.pk)
        )

    def test_status_date_shapes_are_enforced_by_database(self):
        now = timezone.now()
        later = now + timedelta(seconds=1)

        valid_versions = [
            self._version(1),
            self._version(
                2,
                status=WatchPeriodBrigadePhaseVersion.Status.CONFIRMED,
                confirmed_at=now,
            ),
            self._version(
                3,
                status=WatchPeriodBrigadePhaseVersion.Status.SUPERSEDED,
                confirmed_at=now,
                superseded_at=later,
            ),
        ]
        self._insert(WatchPeriodBrigadePhaseVersion, valid_versions)

        invalid_versions = [
            self._version(4, confirmed_at=now),
            self._version(5, superseded_at=now),
            self._version(
                6,
                status=WatchPeriodBrigadePhaseVersion.Status.CONFIRMED,
            ),
            self._version(
                7,
                status=WatchPeriodBrigadePhaseVersion.Status.CONFIRMED,
                confirmed_at=now,
                superseded_at=later,
            ),
            self._version(
                8,
                status=WatchPeriodBrigadePhaseVersion.Status.SUPERSEDED,
                superseded_at=later,
            ),
            self._version(
                9,
                status=WatchPeriodBrigadePhaseVersion.Status.SUPERSEDED,
                confirmed_at=now,
            ),
            self._version(
                10,
                status=WatchPeriodBrigadePhaseVersion.Status.SUPERSEDED,
                confirmed_at=later,
                superseded_at=now,
            ),
        ]
        for invalid in invalid_versions:
            with self.subTest(version_number=invalid.version_number):
                self._assert_integrity_error(
                    lambda invalid=invalid: self._insert_version(invalid)
                )

    def test_row_constraints_and_brigade_number_above_four(self):
        version = self._insert_version(self._version(1))
        row = self._insert_row(
            WatchPeriodBrigadePhaseRow(
                version=version,
                brigade_number=1,
                phase=WatchPeriodBrigadePhaseRow.Phase.DAY,
            )
        )
        self.assertEqual(row.phase, 'day')

        self._assert_integrity_error(
            lambda: self._insert_row(
                WatchPeriodBrigadePhaseRow(
                    version=version,
                    brigade_number=1,
                    phase=WatchPeriodBrigadePhaseRow.Phase.NIGHT,
                )
            )
        )
        self._assert_integrity_error(
            lambda: self._insert_row(
                WatchPeriodBrigadePhaseRow(
                    version=version,
                    brigade_number=0,
                    phase=WatchPeriodBrigadePhaseRow.Phase.OFF,
                )
            )
        )
        self._assert_integrity_error(
            lambda: self._insert_row(
                WatchPeriodBrigadePhaseRow(
                    version=version,
                    brigade_number=2,
                    phase='invalid',
                )
            )
        )

        future_brigade = self._insert_row(
            WatchPeriodBrigadePhaseRow(
                version=version,
                brigade_number=5,
                phase=WatchPeriodBrigadePhaseRow.Phase.OFF,
            )
        )
        self.assertEqual(future_brigade.brigade_number, 5)
        brigade_constraint = next(
            constraint
            for constraint in WatchPeriodBrigadePhaseRow._meta.constraints
            if constraint.name == 'watch_phase_brigade_gte_1'
        )
        self.assertNotIn('lte', str(brigade_constraint.condition))

    def test_public_writers_are_closed_for_both_models(self):
        public_version = self._version(1)
        self._assert_forbidden(public_version.save)
        self._assert_forbidden(
            lambda: WatchPeriodBrigadePhaseVersion.objects.create(
                watch_period=self.watch_period,
                work_schedule=self.work_schedule,
                version_number=1,
            )
        )
        self._assert_forbidden(
            lambda: WatchPeriodBrigadePhaseVersion.objects.get_or_create(
                watch_period=self.watch_period,
                work_schedule=self.work_schedule,
                version_number=1,
            )
        )
        self._assert_forbidden(
            lambda: WatchPeriodBrigadePhaseVersion.objects.update_or_create(
                watch_period=self.watch_period,
                work_schedule=self.work_schedule,
                version_number=1,
            )
        )
        self._assert_forbidden(
            lambda: WatchPeriodBrigadePhaseVersion.objects.bulk_create(
                [self._version(1)]
            )
        )

        version = self._insert_version(self._version(1))
        self._assert_forbidden(
            lambda: WatchPeriodBrigadePhaseVersion.objects.filter(pk=version.pk).update(
                version_number=2,
            )
        )
        self._assert_forbidden(
            lambda: WatchPeriodBrigadePhaseVersion.objects.bulk_update(
                [version],
                ['version_number'],
            )
        )
        self._assert_forbidden(
            lambda: WatchPeriodBrigadePhaseVersion.objects.filter(pk=version.pk).delete()
        )
        self._assert_forbidden(version.delete)

        public_row = WatchPeriodBrigadePhaseRow(
            version=version,
            brigade_number=1,
            phase=WatchPeriodBrigadePhaseRow.Phase.DAY,
        )
        self._assert_forbidden(public_row.save)
        self._assert_forbidden(
            lambda: WatchPeriodBrigadePhaseRow.objects.create(
                version=version,
                brigade_number=1,
                phase=WatchPeriodBrigadePhaseRow.Phase.DAY,
            )
        )
        self._assert_forbidden(
            lambda: WatchPeriodBrigadePhaseRow.objects.bulk_create([public_row])
        )

        row = self._insert_row(public_row)
        self._assert_forbidden(
            lambda: WatchPeriodBrigadePhaseRow.objects.filter(pk=row.pk).update(
                phase=WatchPeriodBrigadePhaseRow.Phase.NIGHT,
            )
        )
        self._assert_forbidden(
            lambda: WatchPeriodBrigadePhaseRow.objects.bulk_update([row], ['phase'])
        )
        self._assert_forbidden(
            lambda: WatchPeriodBrigadePhaseRow.objects.filter(pk=row.pk).delete()
        )
        self._assert_forbidden(row.delete)

    def test_internal_test_writer_proves_cascade_and_protect_policies(self):
        base = self._insert_version(self._version(1))
        replacement = self._insert_version(
            self._version(2, based_on_version=base)
        )
        row = self._insert_row(
            WatchPeriodBrigadePhaseRow(
                version=replacement,
                brigade_number=1,
                phase=WatchPeriodBrigadePhaseRow.Phase.DAY,
            )
        )

        with self.assertRaises(ProtectedError):
            WatchPeriodBrigadePhaseVersion._base_manager.filter(pk=base.pk).delete()
        with self.assertRaises(ProtectedError):
            WatchPeriod.objects.filter(pk=self.watch_period.pk).delete()
        with self.assertRaises(ProtectedError):
            WorkSchedule.objects.filter(pk=self.work_schedule.pk).delete()

        WatchPeriodBrigadePhaseVersion._base_manager.filter(pk=replacement.pk).delete()
        self.assertFalse(
            WatchPeriodBrigadePhaseRow._base_manager.filter(pk=row.pk).exists()
        )

    def test_migration_dependencies_and_no_data_operations(self):
        migration_module = import_module(
            'shifts.migrations.0014_watch_period_brigade_phases'
        )
        self.assertEqual(
            migration_module.Migration.dependencies,
            [
                ('shifts', '0013_unique_open_oup_period'),
                ('users', '0017_employee_sex'),
            ],
        )
        self.assertEqual(
            [operation.name for operation in migration_module.Migration.operations],
            [
                'WatchPeriodBrigadePhaseVersion',
                'WatchPeriodBrigadePhaseRow',
            ],
        )
        self.assertFalse(
            any(
                isinstance(operation, (RunPython, RunSQL))
                for operation in migration_module.Migration.operations
            )
        )
        version_operation = migration_module.Migration.operations[0]
        version_fields = dict(version_operation.fields)
        self.assertFalse(version_fields['source_snapshot'].null)
        self.assertFalse(version_fields['source_snapshot'].has_default())
        self.assertFalse(version_fields['source_fingerprint'].null)
        self.assertFalse(version_fields['source_fingerprint'].has_default())
        self.assertEqual(version_fields['source_fingerprint'].max_length, 64)
