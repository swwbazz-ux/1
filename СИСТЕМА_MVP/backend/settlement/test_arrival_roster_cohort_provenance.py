from datetime import date, datetime, timedelta
from importlib import import_module

from django.db import IntegrityError, connection, models, transaction
from django.db.migrations import RunPython, RunSQL
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from assignments.models import (
    AssignmentStatus,
    CrewPlan,
    CrewPlanSlot,
    CrewPlanStatus,
    EquipmentAssignment,
    WorkShiftType,
)
from references.models import Equipment, EquipmentModel, EquipmentType
from rotations.models import (
    ArrivalRosterMatch,
    ArrivalRosterRoutingBatch,
    ArrivalRosterRoutingEvent,
    ArrivalRosterRoutingRow,
    ArrivalRosterRowReview,
    ArrivalRosterVersion,
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


class ArrivalRosterCohortProvenanceSchemaTests(TestCase):
    def setUp(self):
        self.timekeeper_role = Role.objects.create(
            code='cohort-provenance-timekeeper',
            name='Табельщик provenance cohort',
        )
        self.driver_role = Role.objects.create(
            code='cohort-provenance-driver',
            name='Водитель provenance cohort',
        )
        self.employee = self._employee('Основной', 1)
        self.access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.timekeeper_role,
            access_code='COHORT-PROVENANCE-ACCESS',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.composition = WatchComposition.objects.create(
            code='cohort-provenance-composition',
            name='Состав provenance cohort',
            is_active=True,
        )
        self.employee.watch_composition = self.composition
        self.employee.save(update_fields=['watch_composition'])
        self.work_schedule = WorkSchedule.objects.create(
            code='cohort-provenance-schedule',
            name='График provenance cohort',
            brigade_count=4,
        )
        self.legacy_source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.SYSTEM,
            title='Legacy provenance source',
        )
        self.legacy_revision = SettlementRevision.objects.create(
            code='COHORT-PROVENANCE-REVISION',
            source=self.legacy_source,
            reason='Schema-only legacy compatibility',
        )
        self.graph = self._routing_graph(1, employee=self.employee)
        self.phase_version = self._insert(WatchPeriodBrigadePhaseVersion(
            watch_period=self.graph['period'],
            work_schedule=self.work_schedule,
            version_number=1,
            status=WatchPeriodBrigadePhaseVersion.Status.CONFIRMED,
            created_by_access=self.access,
            confirmed_by_access=self.access,
            confirmed_at=timezone.now(),
            source_snapshot={'source_kind': 'schema_test'},
            source_fingerprint='a' * 64,
        ))
        self.phase_row = self._insert(WatchPeriodBrigadePhaseRow(
            version=self.phase_version,
            brigade_number=1,
            phase=WatchPeriodBrigadePhaseRow.Phase.DAY,
        ))

    def _employee(self, label, index):
        return Employee.objects.create(
            full_name=f'Provenance {label} {index}',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )

    @staticmethod
    def _insert(instance):
        models.QuerySet.bulk_create(type(instance)._base_manager.all(), [instance])
        return type(instance)._base_manager.get(pk=instance.pk)

    def _assert_integrity_error(self, instance):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._insert(instance)

    def _routing_graph(self, index, *, employee=None):
        employee = employee or self._employee('Routing', index)
        if employee.watch_composition_id != self.composition.pk:
            employee.watch_composition = self.composition
            employee.save(update_fields=['watch_composition'])
        period = WatchPeriod.objects.create(
            name=f'Routing provenance period {index}',
            watch_composition=self.composition,
            starts_on=date(2035, 1, 1) + timedelta(days=index * 60),
            ends_on=date(2035, 1, 31) + timedelta(days=index * 60),
            is_active=True,
        )
        resident = self._insert(SettlementResident(
            employee=employee,
            resident_type=SettlementResident.ResidentType.EMPLOYEE,
            status=SettlementResident.Status.ACTIVE,
        ))
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
            method='cohort-provenance-test',
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
        row = self._insert(ArrivalRosterRoutingRow(
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
            role_basis_snapshot={'source': 'schema_test'},
            route_state=ArrivalRosterRoutingRow.RouteState.TO_CLERK,
        ))
        event = self._insert(ArrivalRosterRoutingEvent(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.SENT_TO_CLERK,
            actor_access=self.access,
        ))
        return {
            'employee': employee,
            'period': period,
            'resident': resident,
            'version': version,
            'batch': batch,
            'row': row,
            'event': event,
        }

    def _cohort(self, graph, version, *, source_revision=None, routing_batch=None):
        return SettlementCohort(
            watch_composition=self.composition,
            watch_period=graph['period'],
            version=version,
            source_revision=source_revision,
            routing_batch=routing_batch,
            source_type='schema_test',
            source_id=f'cohort-{graph["period"].pk}-{version}',
            source_snapshot={'source': 'schema_test'},
            input_fingerprint='b' * 64,
            created_by=self.employee,
        )

    def _member(
        self,
        *,
        cohort,
        graph,
        source_revision=None,
        routing_row=None,
        routing_event=None,
        brigade_phase_row=None,
        shift_source_kind=SettlementCohortMember.ShiftSourceKind.CONFIRMED_BRIGADE_PHASE,
        work_shift=SettlementCohortMember.WorkShift.DAY,
        assignment=None,
        selected_access=None,
        selected_at=None,
        selection_basis='',
    ):
        return SettlementCohortMember(
            cohort=cohort,
            resident=graph['resident'],
            arrival_at=timezone.make_aware(datetime.combine(graph['period'].starts_on, datetime.min.time())),
            departure_at=timezone.make_aware(datetime.combine(graph['period'].ends_on, datetime.min.time())),
            participation_status=SettlementCohortMember.ParticipationStatus.PARTICIPATING,
            work_shift=work_shift,
            shift_source_kind=shift_source_kind,
            official_equipment_assignment=assignment,
            shift_source_snapshot={'source': 'schema_test'} if work_shift else {},
            shift_source_fingerprint='c' * 64 if work_shift else '',
            shift_selected_by_access=selected_access,
            shift_selected_at=selected_at,
            shift_selection_basis=selection_basis,
            source_revision=source_revision,
            routing_row=routing_row,
            routing_event=routing_event,
            brigade_phase_row=brigade_phase_row,
            basis_type='schema_test',
            basis_id=f'row-{graph["row"].pk}',
            basis_snapshot={'source': 'schema_test'},
        )

    def _official_assignment(self, graph):
        equipment_type = EquipmentType.objects.create(name='Provenance equipment type')
        equipment_model = EquipmentModel.objects.create(
            equipment_type=equipment_type,
            name='Provenance equipment model',
        )
        equipment = Equipment.objects.create(
            equipment_type=equipment_type,
            model=equipment_model,
            garage_number='PROV-01',
            is_active=True,
        )
        plan = CrewPlan.objects.create(
            work_date=graph['period'].starts_on,
            role=self.driver_role,
            status=CrewPlanStatus.PUBLISHED,
            revision=1,
        )
        slot = CrewPlanSlot.objects.create(
            plan=plan,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_1,
            employee=graph['employee'],
        )
        assignment = self._insert(EquipmentAssignment(
            employee=graph['employee'],
            role=self.driver_role,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_1,
            status=AssignmentStatus.ACCEPTED,
            source_kind=EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN,
            source_crew_plan_slot=slot,
        ))
        event = self._insert(ArrivalRosterRoutingEvent(
            routing_row=graph['row'],
            event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
            actor_access=self.access,
            crew_plan_slot=slot,
            equipment_assignment=assignment,
        ))
        return assignment, event

    def test_fk_structure_nullability_uniqueness_and_protect(self):
        cohort_meta = SettlementCohort._meta
        member_meta = SettlementCohortMember._meta
        expected = {
            (cohort_meta, 'routing_batch'): (ArrivalRosterRoutingBatch, True),
            (member_meta, 'routing_row'): (ArrivalRosterRoutingRow, True),
            (member_meta, 'routing_event'): (ArrivalRosterRoutingEvent, True),
            (member_meta, 'brigade_phase_row'): (WatchPeriodBrigadePhaseRow, False),
        }
        for (meta, field_name), (remote_model, one_to_one) in expected.items():
            with self.subTest(field_name=field_name):
                field = meta.get_field(field_name)
                self.assertIs(field.remote_field.model, remote_model)
                self.assertTrue(field.null)
                self.assertTrue(field.blank)
                self.assertIs(field.remote_field.on_delete, models.PROTECT)
                self.assertEqual(field.one_to_one, one_to_one)
                self.assertEqual(field.unique, one_to_one)
        for meta in (cohort_meta, member_meta):
            source_revision = meta.get_field('source_revision')
            self.assertTrue(source_revision.null)
            self.assertIs(source_revision.remote_field.on_delete, models.PROTECT)
        self.assertFalse(member_meta.get_field('brigade_phase_row').unique)
        self.assertNotIn(
            'brigade_phase_version',
            {field.name for field in member_meta.fields},
        )

    def test_cohort_source_families_and_one_batch_uniqueness(self):
        legacy_graph = self._routing_graph(2)
        legacy = self._insert(self._cohort(
            legacy_graph,
            1,
            source_revision=self.legacy_revision,
        ))
        self.assertIsNone(legacy.routing_batch_id)

        routing = self._insert(self._cohort(
            self.graph,
            1,
            routing_batch=self.graph['batch'],
        ))
        self.assertIsNone(routing.source_revision_id)
        self._assert_integrity_error(self._cohort(
            self.graph,
            2,
            routing_batch=self.graph['batch'],
        ))

        both_graph = self._routing_graph(3)
        self._assert_integrity_error(self._cohort(
            both_graph,
            1,
            source_revision=self.legacy_revision,
            routing_batch=both_graph['batch'],
        ))
        neither_graph = self._routing_graph(4)
        self._assert_integrity_error(self._cohort(neither_graph, 1))

    def test_member_source_families_and_shared_phase_row(self):
        legacy_graph = self._routing_graph(5)
        legacy_cohort = self._insert(self._cohort(
            legacy_graph,
            1,
            source_revision=self.legacy_revision,
        ))
        legacy_member = self._insert(self._member(
            cohort=legacy_cohort,
            graph=legacy_graph,
            source_revision=self.legacy_revision,
            shift_source_kind=SettlementCohortMember.ShiftSourceKind.UNVERIFIED_LEGACY,
            work_shift='',
        ))
        self.assertIsNone(legacy_member.routing_row_id)

        first_cohort = self._insert(self._cohort(
            self.graph,
            1,
            routing_batch=self.graph['batch'],
        ))
        first = self._insert(self._member(
            cohort=first_cohort,
            graph=self.graph,
            routing_row=self.graph['row'],
            routing_event=self.graph['event'],
            brigade_phase_row=self.phase_row,
        ))
        second_graph = self._routing_graph(6)
        second_cohort = self._insert(self._cohort(
            second_graph,
            1,
            routing_batch=second_graph['batch'],
        ))
        second = self._insert(self._member(
            cohort=second_cohort,
            graph=second_graph,
            routing_row=second_graph['row'],
            routing_event=second_graph['event'],
            brigade_phase_row=self.phase_row,
        ))
        self.assertEqual(first.brigade_phase_row_id, second.brigade_phase_row_id)

        mixed_graph = self._routing_graph(7)
        mixed_cohort = self._insert(self._cohort(
            mixed_graph,
            1,
            routing_batch=mixed_graph['batch'],
        ))
        self._assert_integrity_error(self._member(
            cohort=mixed_cohort,
            graph=mixed_graph,
            source_revision=self.legacy_revision,
            routing_row=mixed_graph['row'],
            routing_event=mixed_graph['event'],
            brigade_phase_row=self.phase_row,
        ))
        empty_graph = self._routing_graph(15)
        empty_cohort = self._insert(self._cohort(
            empty_graph,
            1,
            routing_batch=empty_graph['batch'],
        ))
        self._assert_integrity_error(self._member(
            cohort=empty_cohort,
            graph=empty_graph,
        ))

    def test_one_to_one_row_and_event_are_database_enforced(self):
        first_cohort = self._insert(self._cohort(
            self.graph,
            1,
            routing_batch=self.graph['batch'],
        ))
        self._insert(self._member(
            cohort=first_cohort,
            graph=self.graph,
            routing_row=self.graph['row'],
            routing_event=self.graph['event'],
            brigade_phase_row=self.phase_row,
        ))

        row_graph = self._routing_graph(8)
        row_cohort = self._insert(self._cohort(
            row_graph,
            1,
            routing_batch=row_graph['batch'],
        ))
        self._assert_integrity_error(self._member(
            cohort=row_cohort,
            graph=row_graph,
            routing_row=self.graph['row'],
            routing_event=row_graph['event'],
            brigade_phase_row=self.phase_row,
        ))

        event_graph = self._routing_graph(9)
        event_cohort = self._insert(self._cohort(
            event_graph,
            1,
            routing_batch=event_graph['batch'],
        ))
        self._assert_integrity_error(self._member(
            cohort=event_cohort,
            graph=event_graph,
            routing_row=event_graph['row'],
            routing_event=self.graph['event'],
            brigade_phase_row=self.phase_row,
        ))

    def test_confirmed_phase_and_internal_assignment_shapes(self):
        routing_cohort = self._insert(self._cohort(
            self.graph,
            1,
            routing_batch=self.graph['batch'],
        ))
        valid_phase = self._member(
            cohort=routing_cohort,
            graph=self.graph,
            routing_row=self.graph['row'],
            routing_event=self.graph['event'],
            brigade_phase_row=self.phase_row,
        )
        valid_phase.full_clean()

        for overrides in (
            {'work_shift': ''},
            {'brigade_phase_row': None},
            {'selected_access': self.access, 'selected_at': timezone.now(), 'selection_basis': 'manual'},
        ):
            values = {
                'cohort': routing_cohort,
                'graph': self.graph,
                'routing_row': self.graph['row'],
                'routing_event': self.graph['event'],
                'brigade_phase_row': self.phase_row,
            }
            values.update(overrides)
            self._assert_integrity_error(self._member(**values))

        production_graph = self._routing_graph(10)
        assignment, publication_event = self._official_assignment(production_graph)
        production_cohort = self._insert(self._cohort(
            production_graph,
            1,
            routing_batch=production_graph['batch'],
        ))
        self._assert_integrity_error(self._member(
            cohort=production_cohort,
            graph=production_graph,
            routing_row=production_graph['row'],
            routing_event=publication_event,
            brigade_phase_row=self.phase_row,
            assignment=assignment,
        ))
        production_member = self._member(
            cohort=production_cohort,
            graph=production_graph,
            routing_row=production_graph['row'],
            routing_event=publication_event,
            brigade_phase_row=self.phase_row,
            shift_source_kind=SettlementCohortMember.ShiftSourceKind.INTERNAL_ASSIGNMENT,
            assignment=assignment,
        )
        self._insert(production_member)
        self.assertEqual(production_member.brigade_phase_row_id, self.phase_row.pk)

        invalid_internal_graph = self._routing_graph(11)
        invalid_internal_cohort = self._insert(self._cohort(
            invalid_internal_graph,
            1,
            routing_batch=invalid_internal_graph['batch'],
        ))
        self._assert_integrity_error(self._member(
            cohort=invalid_internal_cohort,
            graph=invalid_internal_graph,
            routing_row=invalid_internal_graph['row'],
            routing_event=invalid_internal_graph['event'],
            brigade_phase_row=self.phase_row,
            shift_source_kind=SettlementCohortMember.ShiftSourceKind.INTERNAL_ASSIGNMENT,
        ))

    def test_legacy_shift_sources_remain_and_are_forbidden_for_routing(self):
        legacy_graph = self._routing_graph(12)
        legacy_cohort = self._insert(self._cohort(
            legacy_graph,
            1,
            source_revision=self.legacy_revision,
        ))
        external = self._insert(self._member(
            cohort=legacy_cohort,
            graph=legacy_graph,
            source_revision=self.legacy_revision,
            shift_source_kind=SettlementCohortMember.ShiftSourceKind.EXTERNAL_CLERK,
            selected_access=self.access,
            selected_at=timezone.now(),
            selection_basis='legacy basis',
        ))
        self.assertEqual(
            external.shift_source_kind,
            SettlementCohortMember.ShiftSourceKind.EXTERNAL_CLERK,
        )

        for index, shift_kind, work_shift, audit in (
            (13, SettlementCohortMember.ShiftSourceKind.EXTERNAL_CLERK, 'day', True),
            (14, SettlementCohortMember.ShiftSourceKind.UNVERIFIED_LEGACY, '', False),
        ):
            graph = self._routing_graph(index)
            cohort = self._insert(self._cohort(
                graph,
                1,
                routing_batch=graph['batch'],
            ))
            self._assert_integrity_error(self._member(
                cohort=cohort,
                graph=graph,
                routing_row=graph['row'],
                routing_event=graph['event'],
                brigade_phase_row=self.phase_row,
                shift_source_kind=shift_kind,
                work_shift=work_shift,
                selected_access=self.access if audit else None,
                selected_at=timezone.now() if audit else None,
                selection_basis='legacy basis' if audit else '',
            ))

    def test_used_provenance_is_protected(self):
        cohort = self._insert(self._cohort(
            self.graph,
            1,
            routing_batch=self.graph['batch'],
        ))
        self._insert(self._member(
            cohort=cohort,
            graph=self.graph,
            routing_row=self.graph['row'],
            routing_event=self.graph['event'],
            brigade_phase_row=self.phase_row,
        ))
        querysets = (
            ArrivalRosterRoutingBatch._base_manager.filter(pk=self.graph['batch'].pk),
            ArrivalRosterRoutingRow._base_manager.filter(pk=self.graph['row'].pk),
            ArrivalRosterRoutingEvent._base_manager.filter(pk=self.graph['event'].pk),
            WatchPeriodBrigadePhaseRow._base_manager.filter(pk=self.phase_row.pk),
        )
        for queryset in querysets:
            with self.subTest(model=queryset.model.__name__):
                with self.assertRaises(ProtectedError):
                    with transaction.atomic():
                        models.QuerySet.delete(queryset)

    def test_textual_provenance_fields_are_preserved(self):
        cohort_fields = {field.name for field in SettlementCohort._meta.fields}
        member_fields = {field.name for field in SettlementCohortMember._meta.fields}
        self.assertTrue({'source_type', 'source_id', 'source_snapshot'} <= cohort_fields)
        self.assertTrue({'basis_type', 'basis_id', 'basis_snapshot'} <= member_fields)

    def test_migration_is_schema_only_and_graph_is_acyclic(self):
        migration = import_module(
            'settlement.migrations.0018_arrival_roster_cohort_provenance'
        ).Migration
        self.assertEqual(
            migration.dependencies,
            [
                ('settlement', '0017_m9_preview_corrections'),
                ('rotations', '0007_arrival_roster_routing'),
                ('shifts', '0015_brigade_phase_actor_accesses'),
            ],
        )
        self.assertFalse(
            any(isinstance(operation, (RunPython, RunSQL)) for operation in migration.operations)
        )
        self.assertIn(
            ('settlement', '0019_cohort_member_watch_profile_provenance'),
            MigrationLoader(None).graph.leaf_nodes('settlement'),
        )


class ArrivalRosterCohortProvenanceMigrationTests(TransactionTestCase):
    migrate_from = ('settlement', '0017_m9_preview_corrections')
    migrate_to = ('settlement', '0018_arrival_roster_cohort_provenance')

    def setUp(self):
        self.addCleanup(self._restore_latest_migrations)

    @staticmethod
    def _restore_latest_migrations():
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def _migrate(self, target):
        executor = MigrationExecutor(connection)
        targets = [
            target,
            ('rotations', '0007_arrival_roster_routing'),
            ('shifts', '0015_brigade_phase_actor_accesses'),
        ]
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def test_forward_reverse_forward_cycle(self):
        old_apps = self._migrate(self.migrate_from)
        old_cohort_fields = {
            field.name
            for field in old_apps.get_model('settlement', 'SettlementCohort')._meta.fields
        }
        self.assertNotIn('routing_batch', old_cohort_fields)

        new_apps = self._migrate(self.migrate_to)
        new_cohort_fields = {
            field.name
            for field in new_apps.get_model('settlement', 'SettlementCohort')._meta.fields
        }
        self.assertIn('routing_batch', new_cohort_fields)

        restored_apps = self._migrate(self.migrate_from)
        restored_member_fields = {
            field.name
            for field in restored_apps.get_model(
                'settlement', 'SettlementCohortMember'
            )._meta.fields
        }
        self.assertNotIn('routing_row', restored_member_fields)

        final_apps = self._migrate(self.migrate_to)
        final_member_fields = {
            field.name
            for field in final_apps.get_model(
                'settlement', 'SettlementCohortMember'
            )._meta.fields
        }
        self.assertTrue(
            {'routing_row', 'routing_event', 'brigade_phase_row'}
            <= final_member_fields
        )
