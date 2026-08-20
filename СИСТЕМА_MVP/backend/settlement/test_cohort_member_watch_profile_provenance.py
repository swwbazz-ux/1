from datetime import date, datetime, timedelta
from importlib import import_module

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction
from django.db.migrations import RunPython, RunSQL
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from rotations.models import (
    ArrivalRosterMatch,
    ArrivalRosterRoutingBatch,
    ArrivalRosterRoutingEvent,
    ArrivalRosterRoutingRow,
    ArrivalRosterRowReview,
    ArrivalRosterVersion,
    EmployeeWatchProfileChange,
)
from shifts.models import (
    WatchPeriod,
    WatchPeriodBrigadePhaseRow,
    WatchPeriodBrigadePhaseVersion,
)
from users.models import (
    Employee,
    EmployeeAccess,
    Role,
    WatchComposition,
    WorkSchedule,
)

from .models import (
    SettlementCohort,
    SettlementCohortMember,
    SettlementResident,
    SettlementRevision,
    SettlementSource,
)


class CohortMemberWatchProfileProvenanceSchemaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.role = Role.objects.create(
            code='cohort-watch-profile-role',
            name='Табельщик для provenance профиля',
        )
        cls.actor = Employee.objects.create(
            full_name='Табельщик provenance профиля',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.access = EmployeeAccess.objects.create(
            employee=cls.actor,
            role=cls.role,
            access_code='COHORT-WATCH-PROFILE-ACCESS',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.schedule = WorkSchedule.objects.create(
            code='cohort-watch-profile-schedule',
            name='График provenance профиля',
            brigade_count=4,
            is_active=True,
        )
        cls.composition = WatchComposition.objects.create(
            code='cohort-watch-profile-composition',
            name='Состав provenance профиля',
            is_active=True,
        )
        cls.source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.SYSTEM,
            title='Источник historical provenance профиля',
        )
        cls.revision = SettlementRevision.objects.create(
            code='COHORT-WATCH-PROFILE-REVISION',
            source=cls.source,
            reason='Проверка обратной совместимости provenance профиля',
        )

    @staticmethod
    def _insert(instance):
        models.QuerySet.bulk_create(type(instance)._base_manager.all(), [instance])
        return type(instance)._base_manager.get(pk=instance.pk)

    def _assert_integrity_error(self, instance):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._insert(instance)

    def _employee(self, index):
        return Employee.objects.create(
            full_name=f'Сотрудник provenance профиля {index}',
            status=Employee.Status.ACTIVE,
            is_active=True,
            work_schedule=self.schedule,
            brigade_number=1,
            watch_composition=self.composition,
        )

    def _period(self, index):
        starts_on = date(2038, 1, 1) + timedelta(days=index * 40)
        return WatchPeriod.objects.create(
            name=f'Период provenance профиля {index}',
            watch_composition=self.composition,
            starts_on=starts_on,
            ends_on=starts_on + timedelta(days=29),
            is_active=True,
        )

    def _resident(self, employee):
        return self._insert(SettlementResident(
            employee=employee,
            resident_type=SettlementResident.ResidentType.EMPLOYEE,
            status=SettlementResident.Status.ACTIVE,
        ))

    def _legacy_graph(self, index):
        employee = self._employee(index)
        period = self._period(index)
        resident = self._resident(employee)
        cohort = self._insert(SettlementCohort(
            watch_composition=self.composition,
            watch_period=period,
            version=1,
            source_revision=self.revision,
            source_type='schema_test',
            source_id=f'legacy-{index}',
            source_snapshot={'schema': 1, 'family': 'legacy'},
            input_fingerprint='a' * 64,
            created_by=self.actor,
        ))
        return {
            'employee': employee,
            'period': period,
            'resident': resident,
            'cohort': cohort,
        }

    def _routing_graph(self, index):
        employee = self._employee(index)
        period = self._period(index)
        resident = self._resident(employee)
        version = self._insert(ArrivalRosterVersion(
            watch_period=period,
            version_number=1,
            status=ArrivalRosterVersion.Status.CONFIRMED,
            source_kind=ArrivalRosterVersion.SourceKind.EMPLOYEE_POOL,
            created_by_access=self.access,
            source_fingerprint=f'{index % 10}' * 64,
            confirmed_by_access=self.access,
            confirmed_at=timezone.now(),
            confirmation_snapshot={'schema': 1, 'index': index},
            confirmation_sha256=f'{(index + 1) % 10}' * 64,
        ))
        match = self._insert(ArrivalRosterMatch(
            version=version,
            status=ArrivalRosterMatch.Status.EXACT,
            method='watch-profile-schema-test',
            quality='exact',
            matched_resident=resident,
            evidence={},
        ))
        review = self._insert(ArrivalRosterRowReview(
            version=version,
            match=match,
            resident_resolution=ArrivalRosterRowReview.ResidentResolution.SELECTED,
            selected_resident=resident,
            participation_status=ArrivalRosterRowReview.ParticipationStatus.ARRIVING,
            arrival_mode=ArrivalRosterRowReview.ArrivalMode.SELF,
            arrival_on=period.starts_on,
            departure_on=period.ends_on,
            revision=1,
            updated_by_access=self.access,
        ))
        batch = self._insert(ArrivalRosterRoutingBatch(
            arrival_roster_version=version,
            watch_period=period,
            confirmation_sha256=version.confirmation_sha256,
            created_by_access=self.access,
        ))
        routing_row = self._insert(ArrivalRosterRoutingRow(
            batch=batch,
            row_review=review,
            match=match,
            resident=resident,
            employee=employee,
            participation_snapshot={'participation_status': 'arriving'},
            dates_snapshot={
                'arrival_on': period.starts_on.isoformat(),
                'departure_on': period.ends_on.isoformat(),
            },
            role_snapshot={'qualification_state': 'not_production'},
            role_basis_snapshot={'source': 'watch_profile_schema_test'},
            route_state=ArrivalRosterRoutingRow.RouteState.TO_CLERK,
        ))
        routing_event = self._insert(ArrivalRosterRoutingEvent(
            routing_row=routing_row,
            event_type=ArrivalRosterRoutingEvent.EventType.SENT_TO_CLERK,
            actor_access=self.access,
        ))
        phase_version = self._insert(WatchPeriodBrigadePhaseVersion(
            watch_period=period,
            work_schedule=self.schedule,
            version_number=1,
            status=WatchPeriodBrigadePhaseVersion.Status.CONFIRMED,
            created_by_access=self.access,
            confirmed_by_access=self.access,
            confirmed_at=timezone.now(),
            source_snapshot={'source_kind': 'watch_profile_schema_test'},
            source_fingerprint='b' * 64,
        ))
        phase_row = self._insert(WatchPeriodBrigadePhaseRow(
            version=phase_version,
            brigade_number=1,
            phase=WatchPeriodBrigadePhaseRow.Phase.DAY,
        ))
        cohort = self._insert(SettlementCohort(
            watch_composition=self.composition,
            watch_period=period,
            version=1,
            routing_batch=batch,
            source_type='schema_test',
            source_id=f'routing-{index}',
            source_snapshot={'schema': 1, 'family': 'routing'},
            input_fingerprint='c' * 64,
            created_by=self.actor,
        ))
        return {
            'employee': employee,
            'period': period,
            'resident': resident,
            'cohort': cohort,
            'routing_row': routing_row,
            'routing_event': routing_event,
            'phase_row': phase_row,
        }

    def _legacy_member(self, graph, **overrides):
        values = {
            'cohort': graph['cohort'],
            'resident': graph['resident'],
            'arrival_at': timezone.make_aware(datetime.combine(
                graph['period'].starts_on,
                datetime.min.time(),
            )),
            'departure_at': timezone.make_aware(datetime.combine(
                graph['period'].ends_on,
                datetime.min.time(),
            )),
            'participation_status': SettlementCohortMember.ParticipationStatus.PARTICIPATING,
            'work_shift': '',
            'shift_source_kind': SettlementCohortMember.ShiftSourceKind.UNVERIFIED_LEGACY,
            'shift_source_snapshot': {},
            'shift_source_fingerprint': '',
            'source_revision': self.revision,
            'basis_type': 'schema_test',
            'basis_id': f'legacy-{graph["resident"].pk}',
            'basis_snapshot': {'schema': 1},
            'watch_profile_source_kind': (
                SettlementCohortMember.WatchProfileSourceKind.UNVERIFIED_LEGACY
            ),
            'watch_profile_fingerprint': '',
        }
        values.update(overrides)
        return SettlementCohortMember(**values)

    def _routing_member(self, graph, **overrides):
        values = {
            'cohort': graph['cohort'],
            'resident': graph['resident'],
            'arrival_at': timezone.make_aware(datetime.combine(
                graph['period'].starts_on,
                datetime.min.time(),
            )),
            'departure_at': timezone.make_aware(datetime.combine(
                graph['period'].ends_on,
                datetime.min.time(),
            )),
            'participation_status': SettlementCohortMember.ParticipationStatus.PARTICIPATING,
            'work_shift': SettlementCohortMember.WorkShift.DAY,
            'shift_source_kind': SettlementCohortMember.ShiftSourceKind.CONFIRMED_BRIGADE_PHASE,
            'shift_source_snapshot': {'source': 'watch_profile_schema_test'},
            'shift_source_fingerprint': 'd' * 64,
            'routing_row': graph['routing_row'],
            'routing_event': graph['routing_event'],
            'brigade_phase_row': graph['phase_row'],
            'basis_type': 'schema_test',
            'basis_id': f'routing-{graph["routing_row"].pk}',
            'basis_snapshot': {'schema': 1},
            'watch_profile_source_kind': (
                SettlementCohortMember.WatchProfileSourceKind.LEGACY_BASELINE
            ),
            'watch_profile_work_schedule': self.schedule,
            'watch_profile_brigade_number': 1,
            'watch_profile_watch_composition': self.composition,
            'watch_profile_fingerprint': 'e' * 64,
        }
        values.update(overrides)
        return SettlementCohortMember(**values)

    def _change(self, graph, index=1):
        now = timezone.now()
        return self._insert(EmployeeWatchProfileChange(
            employee=graph['employee'],
            effective_watch_period=graph['period'],
            effective_on=graph['period'].starts_on,
            version_number=index,
            old_work_schedule=self.schedule,
            old_brigade_number=1,
            old_watch_composition=self.composition,
            new_work_schedule=self.schedule,
            new_brigade_number=1,
            new_watch_composition=self.composition,
            basis_kind=EmployeeWatchProfileChange.BasisKind.OFFICIAL_ORDER,
            basis_number=f'ПРИКАЗ-{graph["employee"].pk}-{index}',
            basis_date=timezone.localdate(),
            basis='Официальное основание для проверки схемы.',
            source_snapshot={'schema': 1, 'index': index},
            source_fingerprint='f' * 64,
            created_by_access=self.access,
            applied_by_access=self.access,
            applied_at=now,
            status=EmployeeWatchProfileChange.Status.APPLIED,
        ))

    def test_fields_enum_defaults_related_names_and_protect(self):
        meta = SettlementCohortMember._meta
        expected_fks = {
            'employee_watch_profile_change': (
                EmployeeWatchProfileChange,
                'settlement_cohort_members_by_watch_profile_change',
            ),
            'watch_profile_work_schedule': (
                WorkSchedule,
                'settlement_cohort_members_by_watch_profile_schedule',
            ),
            'watch_profile_watch_composition': (
                WatchComposition,
                'settlement_cohort_members_by_watch_profile_composition',
            ),
        }
        for name, (remote_model, related_name) in expected_fks.items():
            with self.subTest(field=name):
                field = meta.get_field(name)
                self.assertIs(field.remote_field.model, remote_model)
                self.assertTrue(field.null)
                self.assertTrue(field.blank)
                self.assertIs(field.remote_field.on_delete, models.PROTECT)
                self.assertEqual(field.remote_field.related_name, related_name)
        self.assertEqual(
            set(SettlementCohortMember.WatchProfileSourceKind.values),
            {'unverified_legacy', 'legacy_baseline', 'applied_change'},
        )
        source_kind = meta.get_field('watch_profile_source_kind')
        fingerprint = meta.get_field('watch_profile_fingerprint')
        self.assertFalse(source_kind.null)
        self.assertFalse(source_kind.has_default())
        self.assertFalse(fingerprint.null)
        self.assertFalse(fingerprint.has_default())
        self.assertFalse(fingerprint.unique)
        self.assertEqual(fingerprint.max_length, 64)
        brigade = meta.get_field('watch_profile_brigade_number')
        self.assertIsInstance(brigade, models.PositiveSmallIntegerField)
        self.assertTrue(brigade.null)

    def test_unverified_legacy_and_historical_routing_shapes(self):
        legacy = self._insert(self._legacy_member(self._legacy_graph(1)))
        self.assertEqual(
            legacy.watch_profile_source_kind,
            SettlementCohortMember.WatchProfileSourceKind.UNVERIFIED_LEGACY,
        )
        routing_graph = self._routing_graph(2)
        routing = self._insert(self._routing_member(
            routing_graph,
            watch_profile_source_kind=(
                SettlementCohortMember.WatchProfileSourceKind.UNVERIFIED_LEGACY
            ),
            watch_profile_work_schedule=None,
            watch_profile_brigade_number=None,
            watch_profile_watch_composition=None,
            watch_profile_fingerprint='',
        ))
        self.assertIsNotNone(routing.routing_row_id)

    def test_legacy_baseline_accepts_complete_and_nullable_profiles(self):
        complete = self._insert(self._routing_member(self._routing_graph(3)))
        self.assertEqual(complete.watch_profile_brigade_number, 1)

        nullable = self._insert(self._routing_member(
            self._routing_graph(4),
            watch_profile_work_schedule=None,
            watch_profile_brigade_number=None,
            watch_profile_watch_composition=None,
        ))
        self.assertIsNone(nullable.watch_profile_work_schedule_id)

        partial = self._insert(self._routing_member(
            self._routing_graph(5),
            watch_profile_brigade_number=None,
            watch_profile_watch_composition=None,
        ))
        self.assertEqual(partial.watch_profile_work_schedule_id, self.schedule.pk)

    def test_schedule_null_brigade_zero_and_bad_fingerprints_are_blocked(self):
        graph = self._routing_graph(6)
        self._assert_integrity_error(self._routing_member(
            graph,
            watch_profile_work_schedule=None,
            watch_profile_brigade_number=1,
        ))
        self._assert_integrity_error(self._routing_member(
            graph,
            watch_profile_brigade_number=0,
        ))
        for fingerprint in ('a' * 63, 'A' * 64, 'g' * 64):
            with self.subTest(fingerprint=fingerprint[:4]):
                self._assert_integrity_error(self._routing_member(
                    graph,
                    watch_profile_fingerprint=fingerprint,
                ))

    def test_applied_change_requires_exact_change_schedule_and_composition(self):
        graph = self._routing_graph(7)
        change = self._change(graph)
        valid = self._insert(self._routing_member(
            graph,
            watch_profile_source_kind=(
                SettlementCohortMember.WatchProfileSourceKind.APPLIED_CHANGE
            ),
            employee_watch_profile_change=change,
        ))
        self.assertEqual(valid.employee_watch_profile_change_id, change.pk)

        for override in (
            {'employee_watch_profile_change': None},
            {'watch_profile_work_schedule': None},
            {'watch_profile_watch_composition': None},
        ):
            other_graph = self._routing_graph(8 + len(override))
            other_change = self._change(other_graph)
            values = {
                'watch_profile_source_kind': (
                    SettlementCohortMember.WatchProfileSourceKind.APPLIED_CHANGE
                ),
                'employee_watch_profile_change': other_change,
            }
            values.update(override)
            self._assert_integrity_error(self._routing_member(other_graph, **values))

    def test_source_revision_rejects_verified_profile(self):
        graph = self._legacy_graph(12)
        self._assert_integrity_error(self._legacy_member(
            graph,
            watch_profile_source_kind=(
                SettlementCohortMember.WatchProfileSourceKind.LEGACY_BASELINE
            ),
            watch_profile_work_schedule=self.schedule,
            watch_profile_brigade_number=1,
            watch_profile_watch_composition=self.composition,
            watch_profile_fingerprint='1' * 64,
        ))

    def test_fingerprint_is_not_unique(self):
        fingerprint = '2' * 64
        first = self._routing_member(
            self._routing_graph(13),
            watch_profile_fingerprint=fingerprint,
        )
        second = self._routing_member(
            self._routing_graph(14),
            watch_profile_fingerprint=fingerprint,
        )
        self._insert(first)
        self._insert(second)
        self.assertEqual(
            SettlementCohortMember._base_manager.filter(
                watch_profile_fingerprint=fingerprint,
            ).count(),
            2,
        )

    def test_new_fields_are_immutable(self):
        graph = self._routing_graph(15)
        change = self._change(graph)
        member = self._insert(self._routing_member(
            graph,
            watch_profile_source_kind=(
                SettlementCohortMember.WatchProfileSourceKind.APPLIED_CHANGE
            ),
            employee_watch_profile_change=change,
        ))
        changes = {
            'watch_profile_source_kind': (
                SettlementCohortMember.WatchProfileSourceKind.UNVERIFIED_LEGACY
            ),
            'employee_watch_profile_change_id': None,
            'watch_profile_work_schedule_id': None,
            'watch_profile_brigade_number': 2,
            'watch_profile_watch_composition_id': None,
            'watch_profile_fingerprint': '3' * 64,
        }
        for field_name, value in changes.items():
            with self.subTest(field=field_name):
                current = SettlementCohortMember._base_manager.get(pk=member.pk)
                setattr(current, field_name, value)
                with self.assertRaises(ValidationError):
                    current.save()

    def test_profile_foreign_keys_are_protected(self):
        graph = self._routing_graph(16)
        change = self._change(graph)
        self._insert(self._routing_member(
            graph,
            watch_profile_source_kind=(
                SettlementCohortMember.WatchProfileSourceKind.APPLIED_CHANGE
            ),
            employee_watch_profile_change=change,
        ))
        with self.assertRaises(ProtectedError):
            models.QuerySet.delete(
                EmployeeWatchProfileChange._base_manager.filter(pk=change.pk)
            )
        with self.assertRaises(ProtectedError):
            self.schedule.delete()
        with self.assertRaises(ProtectedError):
            self.composition.delete()

    def test_migration_contract_is_schema_only_and_leaf_is_0019(self):
        migration = import_module(
            'settlement.migrations.0019_cohort_member_watch_profile_provenance'
        ).Migration
        self.assertEqual(
            migration.dependencies,
            [
                ('settlement', '0018_arrival_roster_cohort_provenance'),
                ('rotations', '0008_employee_watch_profile_change'),
            ],
        )
        self.assertFalse(any(
            isinstance(operation, (RunPython, RunSQL))
            for operation in migration.operations
        ))
        defaults = {
            operation.name: operation
            for operation in migration.operations
            if operation.__class__.__name__ == 'AddField'
        }
        self.assertFalse(defaults['watch_profile_source_kind'].preserve_default)
        self.assertEqual(
            defaults['watch_profile_source_kind'].field.default,
            'unverified_legacy',
        )
        self.assertFalse(defaults['watch_profile_fingerprint'].preserve_default)
        self.assertEqual(defaults['watch_profile_fingerprint'].field.default, '')
        self.assertEqual(
            MigrationLoader(None).graph.leaf_nodes('settlement'),
            [('settlement', '0019_cohort_member_watch_profile_provenance')],
        )


class CohortMemberWatchProfileProvenanceMigrationTests(TransactionTestCase):
    migrate_from = ('settlement', '0018_arrival_roster_cohort_provenance')
    migrate_to = ('settlement', '0019_cohort_member_watch_profile_provenance')

    def setUp(self):
        self.addCleanup(self._restore_latest_migrations)

    @staticmethod
    def _restore_latest_migrations():
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    @staticmethod
    def _migrate(target):
        executor = MigrationExecutor(connection)
        targets = [target, ('rotations', '0008_employee_watch_profile_change')]
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    @staticmethod
    def _create_historical_members(apps):
        Employee = apps.get_model('users', 'Employee')
        EmployeeAccess = apps.get_model('users', 'EmployeeAccess')
        Role = apps.get_model('users', 'Role')
        WorkSchedule = apps.get_model('users', 'WorkSchedule')
        WatchComposition = apps.get_model('users', 'WatchComposition')
        WatchPeriod = apps.get_model('shifts', 'WatchPeriod')
        PhaseVersion = apps.get_model('shifts', 'WatchPeriodBrigadePhaseVersion')
        PhaseRow = apps.get_model('shifts', 'WatchPeriodBrigadePhaseRow')
        RosterVersion = apps.get_model('rotations', 'ArrivalRosterVersion')
        RosterMatch = apps.get_model('rotations', 'ArrivalRosterMatch')
        RowReview = apps.get_model('rotations', 'ArrivalRosterRowReview')
        RoutingBatch = apps.get_model('rotations', 'ArrivalRosterRoutingBatch')
        RoutingRow = apps.get_model('rotations', 'ArrivalRosterRoutingRow')
        RoutingEvent = apps.get_model('rotations', 'ArrivalRosterRoutingEvent')
        SettlementSource = apps.get_model('settlement', 'SettlementSource')
        SettlementRevision = apps.get_model('settlement', 'SettlementRevision')
        SettlementResident = apps.get_model('settlement', 'SettlementResident')
        SettlementCohort = apps.get_model('settlement', 'SettlementCohort')
        SettlementCohortMember = apps.get_model('settlement', 'SettlementCohortMember')

        composition = WatchComposition.objects.create(
            code='migration-watch-profile-composition',
            name='Состав migration provenance профиля',
            is_active=True,
        )
        employee = Employee.objects.create(
            full_name='Сотрудник migration provenance профиля',
            status='active',
            is_active=True,
        )
        role = Role.objects.create(
            code='migration-watch-profile-role',
            name='Роль migration provenance профиля',
        )
        access = EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code='MIGRATION-WATCH-PROFILE-ACCESS',
            status='activated',
            is_active=True,
        )
        schedule = WorkSchedule.objects.create(
            code='migration-watch-profile-schedule',
            name='График migration provenance профиля',
            brigade_count=4,
            is_active=True,
        )
        period = WatchPeriod.objects.create(
            name='Период migration provenance профиля',
            watch_composition=composition,
            starts_on=date(2040, 1, 1),
            ends_on=date(2040, 1, 30),
            is_active=True,
        )
        source = SettlementSource.objects.create(
            source_type='system',
            title='Migration source provenance профиля',
        )
        revision = SettlementRevision.objects.create(
            code='MIGRATION-WATCH-PROFILE-REVISION',
            source=source,
            reason='Проверка one-off markers',
        )
        resident = SettlementResident.objects.create(
            employee=employee,
            resident_type='EMPLOYEE',
            status='ACTIVE',
        )
        cohort = SettlementCohort.objects.create(
            watch_composition=composition,
            watch_period=period,
            version=1,
            source_revision=revision,
            source_type='migration_test',
            source_id='migration-legacy',
            source_snapshot={'schema': 1},
            input_fingerprint='a' * 64,
            created_by=employee,
        )
        legacy_member = SettlementCohortMember.objects.create(
            cohort=cohort,
            resident=resident,
            arrival_at=timezone.make_aware(datetime(2040, 1, 1)),
            departure_at=timezone.make_aware(datetime(2040, 1, 30)),
            participation_status='participating',
            work_shift='',
            shift_source_kind='unverified_legacy',
            shift_source_snapshot={},
            shift_source_fingerprint='',
            source_revision=revision,
            basis_type='migration_test',
            basis_id='migration-legacy-row',
            basis_snapshot={'schema': 1},
        )

        routing_employee = Employee.objects.create(
            full_name='Сотрудник historical routing provenance профиля',
            status='active',
            is_active=True,
            work_schedule=schedule,
            brigade_number=1,
            watch_composition=composition,
        )
        routing_period = WatchPeriod.objects.create(
            name='Период historical routing provenance профиля',
            watch_composition=composition,
            starts_on=date(2040, 3, 1),
            ends_on=date(2040, 3, 30),
            is_active=True,
        )
        routing_resident = SettlementResident.objects.create(
            employee=routing_employee,
            resident_type='EMPLOYEE',
            status='ACTIVE',
        )
        roster_version = RosterVersion.objects.create(
            watch_period=routing_period,
            version_number=1,
            status='confirmed',
            source_kind='employee_pool',
            created_by_access=access,
            source_fingerprint='b' * 64,
            confirmed_by_access=access,
            confirmed_at=timezone.now(),
            confirmation_snapshot={'schema': 1},
            confirmation_sha256='c' * 64,
        )
        match = RosterMatch.objects.create(
            version=roster_version,
            status='exact',
            method='migration-watch-profile',
            quality='exact',
            matched_resident=routing_resident,
            evidence={},
        )
        review = RowReview.objects.create(
            version=roster_version,
            match=match,
            resident_resolution='selected',
            selected_resident=routing_resident,
            participation_status='arriving',
            arrival_mode='self',
            arrival_on=routing_period.starts_on,
            departure_on=routing_period.ends_on,
            revision=1,
            updated_by_access=access,
        )
        batch = RoutingBatch.objects.create(
            arrival_roster_version=roster_version,
            watch_period=routing_period,
            confirmation_sha256=roster_version.confirmation_sha256,
            created_by_access=access,
        )
        routing_row = RoutingRow.objects.create(
            batch=batch,
            row_review=review,
            match=match,
            resident=routing_resident,
            employee=routing_employee,
            participation_snapshot={'participation_status': 'arriving'},
            dates_snapshot={
                'arrival_on': routing_period.starts_on.isoformat(),
                'departure_on': routing_period.ends_on.isoformat(),
            },
            role_snapshot={'qualification_state': 'not_production'},
            role_basis_snapshot={'source': 'migration_watch_profile'},
            route_state='to_clerk',
        )
        routing_event = RoutingEvent.objects.create(
            routing_row=routing_row,
            event_type='sent_to_clerk',
            actor_access=access,
        )
        phase_version = PhaseVersion.objects.create(
            watch_period=routing_period,
            work_schedule=schedule,
            version_number=1,
            status='confirmed',
            created_by_access=access,
            confirmed_by_access=access,
            confirmed_at=timezone.now(),
            source_snapshot={'source_kind': 'migration_watch_profile'},
            source_fingerprint='d' * 64,
        )
        phase_row = PhaseRow.objects.create(
            version=phase_version,
            brigade_number=1,
            phase='day',
        )
        routing_cohort = SettlementCohort.objects.create(
            watch_composition=composition,
            watch_period=routing_period,
            version=1,
            routing_batch=batch,
            source_type='migration_test',
            source_id='migration-routing',
            source_snapshot={'schema': 1},
            input_fingerprint='e' * 64,
            created_by=employee,
        )
        routing_member = SettlementCohortMember.objects.create(
            cohort=routing_cohort,
            resident=routing_resident,
            arrival_at=timezone.make_aware(datetime(2040, 3, 1)),
            departure_at=timezone.make_aware(datetime(2040, 3, 30)),
            participation_status='participating',
            work_shift='day',
            shift_source_kind='confirmed_brigade_phase',
            shift_source_snapshot={'source': 'migration_watch_profile'},
            shift_source_fingerprint='f' * 64,
            routing_row=routing_row,
            routing_event=routing_event,
            brigade_phase_row=phase_row,
            basis_type='migration_test',
            basis_id='migration-routing-row',
            basis_snapshot={'schema': 1},
        )
        return legacy_member.pk, routing_member.pk

    def test_forward_reverse_forward_cycle_uses_only_unverified_markers(self):
        old_apps = self._migrate(self.migrate_from)
        old_pks = self._create_historical_members(old_apps)

        new_apps = self._migrate(self.migrate_to)
        NewMember = new_apps.get_model('settlement', 'SettlementCohortMember')
        for old_pk in old_pks:
            with self.subTest(member_pk=old_pk):
                migrated = NewMember.objects.get(pk=old_pk)
                self.assertEqual(migrated.watch_profile_source_kind, 'unverified_legacy')
                self.assertEqual(migrated.watch_profile_fingerprint, '')
                self.assertIsNone(migrated.employee_watch_profile_change_id)
                self.assertIsNone(migrated.watch_profile_work_schedule_id)
                self.assertIsNone(migrated.watch_profile_brigade_number)
                self.assertIsNone(migrated.watch_profile_watch_composition_id)

        restored_apps = self._migrate(self.migrate_from)
        RestoredMember = restored_apps.get_model('settlement', 'SettlementCohortMember')
        self.assertEqual(
            RestoredMember.objects.filter(pk__in=old_pks).count(),
            2,
        )
        self.assertNotIn(
            'watch_profile_source_kind',
            {field.name for field in RestoredMember._meta.fields},
        )

        final_apps = self._migrate(self.migrate_to)
        FinalMember = final_apps.get_model('settlement', 'SettlementCohortMember')
        for old_pk in old_pks:
            with self.subTest(final_member_pk=old_pk):
                final = FinalMember.objects.get(pk=old_pk)
                self.assertEqual(final.watch_profile_source_kind, 'unverified_legacy')
                self.assertEqual(final.watch_profile_fingerprint, '')
                self.assertIsNone(final.employee_watch_profile_change_id)
