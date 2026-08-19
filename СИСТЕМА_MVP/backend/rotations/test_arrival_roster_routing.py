from datetime import date
from unittest.mock import patch

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction
from django.db.migrations.executor import MigrationExecutor
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
from settlement.models import SettlementResident
from shifts.models import WatchPeriod
from users.models import (
    Employee,
    EmployeeAccess,
    PersonnelPosition,
    ProductionSpecialization,
    Role,
    TemporaryWorkTransfer,
    WatchComposition,
)

from .models import (
    ArrivalRosterMatch,
    ArrivalRosterRowReview,
    ArrivalRosterRoutingBatch,
    ArrivalRosterRoutingEvent,
    ArrivalRosterRoutingRow,
    ArrivalRosterVersion,
)
from .arrival_roster_routing import route_confirmed_arrival_roster_version
from .arrival_rosters import _canonical_sha256


class ArrivalRosterRoutingModelTests(TestCase):
    def _insert(self, instance):
        type(instance)._base_manager.bulk_create([instance])
        return type(instance)._base_manager.get(pk=instance.pk)

    def setUp(self):
        self.timekeeper_role, _ = Role.objects.get_or_create(
            code='timekeeper', defaults={'name': 'Табельщик'},
        )
        self.driver_role, _ = Role.objects.get_or_create(
            code='driver', defaults={'name': 'Водитель'},
        )
        self.employee = Employee.objects.create(
            full_name='Маршрутизация Внутренний',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=self.timekeeper_role,
            access_code='routing-access',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.composition = WatchComposition.objects.create(
            code='routing-composition', name='Routing composition', is_active=True,
        )
        self.period = WatchPeriod.objects.create(
            name='Routing period',
            watch_composition=self.composition,
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 9, 1),
            is_active=True,
        )
        self.resident = self._insert(SettlementResident(
            employee=self.employee,
            resident_type=SettlementResident.ResidentType.EMPLOYEE,
            status=SettlementResident.Status.ACTIVE,
        ))
        self.version = self._insert(ArrivalRosterVersion(
            watch_period=self.period,
            version_number=1,
            status=ArrivalRosterVersion.Status.CONFIRMED,
            source_kind=ArrivalRosterVersion.SourceKind.EMPLOYEE_POOL,
            created_by_access=self.access,
            source_fingerprint='a' * 64,
            confirmed_by_access=self.access,
            confirmed_at=timezone.now(),
            confirmation_snapshot={'schema': 1},
            confirmation_sha256='b' * 64,
        ))
        self.match = self._insert(ArrivalRosterMatch(
            version=self.version,
            status=ArrivalRosterMatch.Status.EXACT,
            method='routing-test',
            quality='exact',
            matched_resident=self.resident,
            evidence={},
        ))
        self.review = self._insert(ArrivalRosterRowReview(
            version=self.version,
            match=self.match,
            resident_resolution=ArrivalRosterRowReview.ResidentResolution.SELECTED,
            selected_resident=self.resident,
            participation_status=ArrivalRosterRowReview.ParticipationStatus.ARRIVING,
            arrival_mode=ArrivalRosterRowReview.ArrivalMode.SELF,
            arrival_on=date(2026, 8, 13),
            departure_on=date(2026, 9, 13),
            revision=1,
            updated_by_access=self.access,
        ))
        self.batch = self._insert(ArrivalRosterRoutingBatch(
            arrival_roster_version=self.version,
            watch_period=self.period,
            confirmation_sha256=self.version.confirmation_sha256,
            created_by_access=self.access,
        ))

    def _routing_row(self, **overrides):
        values = {
            'batch': self.batch,
            'row_review': self.review,
            'match': self.match,
            'resident': self.resident,
            'employee': self.employee,
            'participation_snapshot': {'status': 'arriving', 'arrival_mode': 'self'},
            'dates_snapshot': {'arrival_on': '2026-08-13', 'departure_on': '2026-09-13'},
            'role_snapshot': {'role_code': 'driver'},
            'role_basis_snapshot': {'source': 'оуп'},
            'route_state': ArrivalRosterRoutingRow.RouteState.TO_DEPUTY,
        }
        values.update(overrides)
        row = ArrivalRosterRoutingRow(**values)
        row.full_clean()
        return self._insert(row)

    def _external_subject(self):
        resident = self._insert(SettlementResident(
            resident_type=SettlementResident.ResidentType.CONTRACTOR,
            full_name='Маршрутизация Внешний',
            position_title='Специалист',
            organization='Подрядчик',
            phone='+70000000001',
            external_sex='male',
            status=SettlementResident.Status.ACTIVE,
            created_by_access=self.access,
        ))
        match = self._insert(ArrivalRosterMatch(
            version=self.version,
            status=ArrivalRosterMatch.Status.EXACT,
            method='routing-external-test',
            quality='exact',
            matched_resident=resident,
            evidence={},
        ))
        review = self._insert(ArrivalRosterRowReview(
            version=self.version,
            match=match,
            resident_resolution=ArrivalRosterRowReview.ResidentResolution.SELECTED,
            selected_resident=resident,
            participation_status=ArrivalRosterRowReview.ParticipationStatus.ADDITIONAL,
            arrival_on=date(2026, 8, 13),
            departure_on=date(2026, 9, 13),
            revision=1,
            updated_by_access=self.access,
        ))
        return resident, match, review

    def _official_assignment(self):
        equipment_type = EquipmentType.objects.create(name='Routing truck')
        equipment_model = EquipmentModel.objects.create(
            equipment_type=equipment_type, name='Routing model',
        )
        equipment = Equipment.objects.create(
            equipment_type=equipment_type, model=equipment_model,
            garage_number='RT-01', is_active=True,
        )
        plan = CrewPlan.objects.create(
            work_date=date(2026, 8, 13), role=self.driver_role,
            status=CrewPlanStatus.PUBLISHED, revision=1,
        )
        slot = CrewPlanSlot.objects.create(
            plan=plan, equipment=equipment, shift_type=WorkShiftType.SHIFT_1,
            employee=self.employee,
        )
        assignment = self._insert(EquipmentAssignment(
            employee=self.employee,
            role=self.driver_role,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_1,
            status=AssignmentStatus.ACCEPTED,
            source_kind=EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN,
            source_crew_plan_slot=slot,
        ))
        return slot, assignment

    def test_batch_is_exact_confirmed_version_period_sha_and_access(self):
        self.assertEqual(self.batch.arrival_roster_version_id, self.version.pk)
        self.assertEqual(self.batch.watch_period_id, self.period.pk)
        self.assertEqual(self.batch.confirmation_sha256, 'b' * 64)
        self.assertEqual(self.batch.created_by_access_id, self.access.pk)
        self.assertIsNotNone(self.batch.created_at)

    def test_batch_requires_exact_timekeeper_access(self):
        clerk_role, _ = Role.objects.get_or_create(
            code='settlement_clerk', defaults={'name': 'Делопроизводитель'},
        )
        wrong_access = EmployeeAccess.objects.create(
            employee=self.employee,
            role=clerk_role,
            access_code='routing-clerk-access',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.batch.created_by_access = wrong_access
        with self.assertRaises(ValidationError) as caught:
            self.batch.full_clean()
        self.assertIn('created_by_access', caught.exception.message_dict)

    def test_one_batch_per_version_is_enforced_by_database(self):
        duplicate = ArrivalRosterRoutingBatch(
            arrival_roster_version=self.version,
            watch_period=self.period,
            confirmation_sha256=self.version.confirmation_sha256,
            created_by_access=self.access,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArrivalRosterRoutingBatch._base_manager.bulk_create([duplicate])

    def test_routing_models_reject_public_writers(self):
        row = ArrivalRosterRoutingRow(
            batch=self.batch,
            row_review=self.review,
            match=self.match,
            resident=self.resident,
            employee=self.employee,
            participation_snapshot={'status': 'arriving'},
            dates_snapshot={},
            role_snapshot={},
            role_basis_snapshot={},
            route_state=ArrivalRosterRoutingRow.RouteState.TO_DEPUTY,
        )
        event = ArrivalRosterRoutingEvent(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.CREATED,
            actor_access=self.access,
        )
        for instance in (row, event):
            with self.assertRaises(ValidationError) as caught:
                instance.save()
            self.assertEqual(caught.exception.code, 'rotations.arrival_roster.public_write_forbidden')

    def test_row_preserves_exact_fks_and_oup_snapshots(self):
        row = self._routing_row()
        self.assertEqual(
            (row.batch_id, row.row_review_id, row.match_id, row.resident_id, row.employee_id),
            (self.batch.pk, self.review.pk, self.match.pk, self.resident.pk, self.employee.pk),
        )
        self.assertEqual(row.participation_snapshot, {'status': 'arriving', 'arrival_mode': 'self'})
        self.assertEqual(row.dates_snapshot['arrival_on'], '2026-08-13')
        self.assertEqual(row.role_snapshot, {'role_code': 'driver'})
        self.assertEqual(row.role_basis_snapshot, {'source': 'оуп'})

    def test_external_resident_has_nullable_employee(self):
        resident, match, review = self._external_subject()
        row = self._routing_row(
            resident=resident,
            match=match,
            row_review=review,
            employee=None,
            participation_snapshot={'status': 'additional'},
            route_state=ArrivalRosterRoutingRow.RouteState.TO_CLERK,
        )
        self.assertIsNone(row.employee_id)
        self.assertEqual(row.resident_id, resident.pk)

    def test_batch_and_review_pair_is_unique(self):
        self._routing_row()
        duplicate = ArrivalRosterRoutingRow(
            batch=self.batch,
            row_review=self.review,
            match=self.match,
            resident=self.resident,
            employee=self.employee,
            participation_snapshot={'status': 'arriving'},
            dates_snapshot={},
            role_snapshot={},
            role_basis_snapshot={},
            route_state=ArrivalRosterRoutingRow.RouteState.TO_DEPUTY,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArrivalRosterRoutingRow._base_manager.bulk_create([duplicate])

    def test_route_and_event_types_are_limited_and_event_fks_are_exact_or_null(self):
        self.assertEqual(
            set(ArrivalRosterRoutingRow.RouteState.values),
            {'to_deputy', 'to_clerk', 'not_participating', 'review_required'},
        )
        self.assertEqual(
            set(ArrivalRosterRoutingEvent.EventType.values),
            {
                'created', 'sent_to_deputy', 'sent_to_clerk',
                'official_assignment_published', 'requires_review', 'stale',
            },
        )
        row = self._routing_row()
        empty_event = self._insert(ArrivalRosterRoutingEvent(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.CREATED,
            actor_access=self.access,
        ))
        self.assertIsNone(empty_event.crew_plan_slot_id)
        self.assertIsNone(empty_event.equipment_assignment_id)
        slot, assignment = self._official_assignment()
        official_event = self._insert(ArrivalRosterRoutingEvent(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
            actor_access=self.access,
            crew_plan_slot=slot,
            equipment_assignment=assignment,
        ))
        self.assertEqual(official_event.crew_plan_slot_id, slot.pk)
        self.assertEqual(official_event.equipment_assignment_id, assignment.pk)

    def test_event_assignment_shape_is_enforced_by_database(self):
        row = self._routing_row()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArrivalRosterRoutingEvent._base_manager.bulk_create([
                    ArrivalRosterRoutingEvent(
                        routing_row=row,
                        event_type=ArrivalRosterRoutingEvent.EventType.CREATED,
                        actor_access=self.access,
                        crew_plan_slot=self._official_assignment()[0],
                    ),
                ])

    def test_fk_policies_and_privacy_fields_match_contract(self):
        self.assertIs(ArrivalRosterRoutingBatch._meta.get_field('arrival_roster_version').remote_field.on_delete, models.PROTECT)
        self.assertIs(ArrivalRosterRoutingRow._meta.get_field('batch').remote_field.on_delete, models.CASCADE)
        self.assertIs(ArrivalRosterRoutingRow._meta.get_field('row_review').remote_field.on_delete, models.PROTECT)
        self.assertIs(ArrivalRosterRoutingEvent._meta.get_field('routing_row').remote_field.on_delete, models.CASCADE)
        self.assertIs(ArrivalRosterRoutingEvent._meta.get_field('equipment_assignment').remote_field.on_delete, models.PROTECT)
        forbidden = {'phone', 'pin', 'confirmation_snapshot', 'fingerprint', 'source_fingerprint'}
        for model_class in (
            ArrivalRosterRoutingBatch,
            ArrivalRosterRoutingRow,
            ArrivalRosterRoutingEvent,
        ):
            self.assertFalse(forbidden.intersection(field.name for field in model_class._meta.fields))


class ArrivalRosterRoutingCommandTests(TestCase):
    def _insert(self, instance):
        type(instance)._base_manager.bulk_create([instance])
        return type(instance)._base_manager.get(pk=instance.pk)

    def setUp(self):
        self.timekeeper_role, _ = Role.objects.get_or_create(
            code='timekeeper', defaults={'name': 'Табельщик'},
        )
        self.driver_role, _ = Role.objects.get_or_create(
            code='driver', defaults={'name': 'Водитель'},
        )
        self.excavator_role, _ = Role.objects.get_or_create(
            code='excavator_operator', defaults={'name': 'Машинист экскаватора'},
        )
        self.clerk_role, _ = Role.objects.get_or_create(
            code='settlement_clerk', defaults={'name': 'Делопроизводитель'},
        )
        self.composition = WatchComposition.objects.create(
            code='routing-command-composition',
            name='Routing command composition',
            is_active=True,
        )
        self.period = WatchPeriod.objects.create(
            name='Routing command period',
            watch_composition=self.composition,
            starts_on=date(2026, 8, 1),
            ends_on=date(2026, 9, 1),
            is_active=True,
        )
        self.timekeeper = Employee.objects.create(
            full_name='Табельщик маршрутизации',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.timekeeper,
            role=self.timekeeper_role,
            access_code='routing-command-access',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.driver_specialization = ProductionSpecialization.objects.create(
            code='routing-command-driver',
            name='Routing command driver',
            access_role=self.driver_role,
            is_active=True,
        )
        self.excavator_specialization = ProductionSpecialization.objects.create(
            code='routing-command-excavator',
            name='Routing command excavator',
            access_role=self.excavator_role,
            is_active=True,
        )
        self.position = PersonnelPosition.objects.create(
            code='routing-command-position',
            name='Routing command position',
            requires_specialization=True,
            is_active=True,
        )
        self.version = self._confirmed_version()

    def _confirmed_version(self, *, period=None, version_number=1):
        period = period or self.period
        confirmation_snapshot = {
            'schema': 1,
            'kind': 'routing-command-test',
            'period_id': period.pk,
            'version_number': version_number,
        }
        return self._insert(ArrivalRosterVersion(
            watch_period=period,
            version_number=version_number,
            status=ArrivalRosterVersion.Status.CONFIRMED,
            source_kind=ArrivalRosterVersion.SourceKind.EMPLOYEE_POOL,
            created_by_access=self.access,
            source_fingerprint='a' * 64,
            confirmed_by_access=self.access,
            confirmed_at=timezone.now(),
            confirmation_snapshot=confirmation_snapshot,
            confirmation_sha256=_canonical_sha256(confirmation_snapshot),
        ))

    def _employee(self, suffix, *, specialization=None, work_category=Employee.WorkCategory.OTHER,
                  personnel_position=True, position=''):
        return Employee.objects.create(
            full_name=f'Маршрутизация {suffix}',
            personnel_position=self.position if personnel_position else None,
            base_specialization=specialization,
            work_category=work_category,
            position=position,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )

    def _subject(self, suffix, *, employee=None, participation=ArrivalRosterRowReview.ParticipationStatus.ARRIVING,
                 matched_resident=None, evidence=None, basis='', comment=''):
        if employee is None and matched_resident is None:
            resident = self._insert(SettlementResident(
                resident_type=SettlementResident.ResidentType.CONTRACTOR,
                full_name=f'Внешний {suffix}',
                position_title='Подрядчик',
                organization='Организация',
                phone='+79990000002',
                external_sex='male',
                status=SettlementResident.Status.ACTIVE,
                created_by_access=self.access,
            ))
        elif matched_resident is not None:
            resident = matched_resident
        else:
            resident = self._insert(SettlementResident(
                employee=employee,
                resident_type=SettlementResident.ResidentType.EMPLOYEE,
                status=SettlementResident.Status.ACTIVE,
            ))
        match = self._insert(ArrivalRosterMatch(
            version=self.version,
            status=ArrivalRosterMatch.Status.EXACT,
            method='routing-command-test',
            quality='exact',
            matched_resident=resident,
            evidence=evidence or {},
        ))
        review = self._insert(ArrivalRosterRowReview(
            version=self.version,
            match=match,
            resident_resolution=ArrivalRosterRowReview.ResidentResolution.SELECTED,
            selected_resident=resident,
            participation_status=participation,
            arrival_mode=(
                ArrivalRosterRowReview.ArrivalMode.SELF
                if participation == ArrivalRosterRowReview.ParticipationStatus.ARRIVING
                else None
            ),
            arrival_on=(None if participation == ArrivalRosterRowReview.ParticipationStatus.NOT_ARRIVING else date(2026, 8, 2)),
            departure_on=(None if participation == ArrivalRosterRowReview.ParticipationStatus.NOT_ARRIVING else date(2026, 8, 30)),
            basis=basis,
            comment=comment,
            revision=1,
            updated_by_access=self.access,
        ))
        return resident, match, review

    def _route(self):
        return route_confirmed_arrival_roster_version(
            version_id=self.version.pk,
            actor_access_id=self.access.pk,
        )

    def test_routes_all_five_variants_and_creates_one_complete_batch_atomically(self):
        absent_employee = self._employee('Не участвует', specialization=self.driver_specialization)
        _absent_resident, _absent_match, absent_review = self._subject(
            'Не участвует',
            employee=absent_employee,
            participation=ArrivalRosterRowReview.ParticipationStatus.NOT_ARRIVING,
        )
        _external_resident, _external_match, external_review = self._subject('Внешний')
        clerk_employee = self._employee('Делопроизводитель')
        _clerk_resident, _clerk_match, clerk_review = self._subject('Делопроизводитель', employee=clerk_employee)
        driver_employee = self._employee('Водитель', specialization=self.driver_specialization)
        _driver_resident, _driver_match, driver_review = self._subject('Водитель', employee=driver_employee)
        ambiguous_employee = self._employee('Неоднозначный')
        for specialization in (self.driver_specialization, self.excavator_specialization):
            TemporaryWorkTransfer.objects.create(
                employee=ambiguous_employee,
                target_specialization=specialization,
                watch_period=self.period,
                effective_from=self.period.starts_on,
                effective_to=self.period.ends_on,
                status=TemporaryWorkTransfer.Status.APPROVED,
            )
        _ambiguous_resident, _ambiguous_match, ambiguous_review = self._subject(
            'Неоднозначный', employee=ambiguous_employee,
        )

        batch = self._route()

        self.assertEqual(batch.arrival_roster_version_id, self.version.pk)
        self.assertEqual(batch.watch_period_id, self.period.pk)
        self.assertEqual(batch.confirmation_sha256, self.version.confirmation_sha256)
        self.assertEqual(batch.created_by_access_id, self.access.pk)
        rows = {
            row.row_review_id: row
            for row in ArrivalRosterRoutingRow._base_manager.filter(batch=batch)
        }
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[absent_review.pk].route_state, ArrivalRosterRoutingRow.RouteState.NOT_PARTICIPATING)
        self.assertEqual(rows[external_review.pk].route_state, ArrivalRosterRoutingRow.RouteState.TO_CLERK)
        self.assertEqual(rows[clerk_review.pk].route_state, ArrivalRosterRoutingRow.RouteState.TO_CLERK)
        self.assertEqual(rows[driver_review.pk].route_state, ArrivalRosterRoutingRow.RouteState.TO_DEPUTY)
        self.assertEqual(rows[ambiguous_review.pk].route_state, ArrivalRosterRoutingRow.RouteState.REVIEW_REQUIRED)
        self.assertEqual(rows[driver_review.pk].role_snapshot, {
            'role_code': 'driver', 'qualification_state': 'exact',
        })
        self.assertEqual(rows[driver_review.pk].role_basis_snapshot['source'], 'base_specialization')
        self.assertEqual(rows[driver_review.pk].participation_snapshot, {
            'participation_status': 'arriving', 'arrival_mode': 'self',
        })
        self.assertEqual(rows[driver_review.pk].dates_snapshot, {
            'arrival_on': '2026-08-02', 'departure_on': '2026-08-30',
        })
        self.assertEqual(rows[ambiguous_review.pk].role_snapshot['candidate_role_codes'], [
            'driver', 'excavator_operator',
        ])
        event_types = {
            row_id: list(
                ArrivalRosterRoutingEvent._base_manager.filter(routing_row_id=row_id)
                .order_by('created_at', 'pk')
                .values_list('event_type', flat=True)
            )
            for row_id in rows
        }
        self.assertEqual(event_types[rows[absent_review.pk].pk], ['created'])
        self.assertEqual(event_types[rows[external_review.pk].pk], ['created', 'sent_to_clerk'])
        self.assertEqual(event_types[rows[driver_review.pk].pk], ['created', 'sent_to_deputy'])
        self.assertEqual(event_types[rows[ambiguous_review.pk].pk], ['created', 'requires_review'])

    def test_qualification_uses_oup_data_not_excel_hints_position_or_unofficial_shift(self):
        employee = self._employee(
            'ОУП водитель',
            specialization=self.driver_specialization,
            position='Свободный текст: делопроизводитель',
        )
        _resident, _match, review = self._subject(
            'ОУП водитель',
            employee=employee,
            evidence={
                'excel_shift_hint': 'NIGHT',
                'excel_color': '#ff0000',
                'excel_row_order': 999,
                'unofficial_shift': 'DAY',
            },
            basis='Excel: машинист, NIGHT, строка 999',
            comment='Неофициальная смена DAY',
        )

        batch = self._route()

        row = ArrivalRosterRoutingRow._base_manager.get(batch=batch, row_review=review)
        self.assertEqual(row.route_state, ArrivalRosterRoutingRow.RouteState.TO_DEPUTY)
        self.assertEqual(row.role_snapshot['role_code'], 'driver')
        self.assertNotIn('excel_shift_hint', row.role_snapshot)
        self.assertNotIn('unofficial_shift', row.role_basis_snapshot)

    def test_requires_exact_active_timekeeper_access(self):
        employee = self._employee('Доступ')
        self._subject('Доступ', employee=employee)
        wrong_access = EmployeeAccess.objects.create(
            employee=self.timekeeper,
            role=self.clerk_role,
            access_code='routing-command-wrong-role',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        with self.assertRaises(ValidationError) as caught:
            route_confirmed_arrival_roster_version(
                version_id=self.version.pk,
                actor_access_id=wrong_access.pk,
            )
        self.assertEqual(caught.exception.code, 'arrival_roster.access_denied')
        EmployeeAccess.objects.filter(pk=self.access.pk).update(
            status=EmployeeAccess.Status.BLOCKED,
        )
        with self.assertRaises(ValidationError) as caught:
            self._route()
        self.assertEqual(caught.exception.code, 'arrival_roster.access_denied')
        EmployeeAccess.objects.filter(pk=self.access.pk).update(
            status=EmployeeAccess.Status.ACTIVATED,
        )
        EmployeeAccess.objects.filter(pk=self.access.pk).update(is_active=False)
        with self.assertRaises(ValidationError) as caught:
            self._route()
        self.assertEqual(caught.exception.code, 'arrival_roster.access_denied')
        self.assertEqual(ArrivalRosterRoutingBatch._base_manager.count(), 0)

    def test_rejects_not_confirmed_superseded_and_damaged_confirmation(self):
        employee = self._employee('Состояния')
        self._subject('Состояния', employee=employee)
        ArrivalRosterVersion._base_manager.filter(pk=self.version.pk).update(
            status=ArrivalRosterVersion.Status.SUPERSEDED,
            superseded_at=timezone.now(),
        )
        with self.assertRaises(ValidationError) as caught:
            self._route()
        self.assertEqual(caught.exception.code, 'arrival_roster.routing_stale')

        period = WatchPeriod.objects.create(
            name='Routing command draft period',
            watch_composition=self.composition,
            starts_on=date(2026, 10, 1),
            ends_on=date(2026, 11, 1),
            is_active=True,
        )
        draft = self._insert(ArrivalRosterVersion(
            watch_period=period,
            version_number=1,
            status=ArrivalRosterVersion.Status.DRAFT,
            source_kind=ArrivalRosterVersion.SourceKind.EMPLOYEE_POOL,
            created_by_access=self.access,
            source_fingerprint='c' * 64,
        ))
        with self.assertRaises(ValidationError) as caught:
            route_confirmed_arrival_roster_version(version_id=draft.pk, actor_access_id=self.access.pk)
        self.assertEqual(caught.exception.code, 'arrival_roster.routing_version_not_confirmed')

        version = self._confirmed_version(version_number=2, period=period)
        ArrivalRosterVersion._base_manager.filter(pk=version.pk).update(confirmation_sha256='0' * 64)
        with self.assertRaises(ValidationError) as caught:
            route_confirmed_arrival_roster_version(version_id=version.pk, actor_access_id=self.access.pk)
        self.assertEqual(caught.exception.code, 'arrival_roster.routing_confirmation_invalid')

    def test_rejects_incomplete_or_contradictory_graph_without_partial_writes(self):
        employee = self._employee('Неполный')
        resident = self._insert(SettlementResident(
            employee=employee,
            resident_type=SettlementResident.ResidentType.EMPLOYEE,
            status=SettlementResident.Status.ACTIVE,
        ))
        self._insert(ArrivalRosterMatch(
            version=self.version,
            status=ArrivalRosterMatch.Status.EXACT,
            method='routing-command-incomplete',
            quality='exact',
            matched_resident=resident,
            evidence={},
        ))
        with self.assertRaises(ValidationError) as caught:
            self._route()
        self.assertEqual(caught.exception.code, 'arrival_roster.routing_graph_incomplete')
        self.assertEqual(ArrivalRosterRoutingBatch._base_manager.count(), 0)
        self.assertEqual(ArrivalRosterRoutingRow._base_manager.count(), 0)
        self.assertEqual(ArrivalRosterRoutingEvent._base_manager.count(), 0)

    def test_rejects_contradictory_review_match_resident_graph_without_partial_writes(self):
        matched_employee = self._employee('Сопоставленный')
        selected_employee = self._employee('Выбранный')
        matched_resident = self._insert(SettlementResident(
            employee=matched_employee,
            resident_type=SettlementResident.ResidentType.EMPLOYEE,
            status=SettlementResident.Status.ACTIVE,
        ))
        selected_resident = self._insert(SettlementResident(
            employee=selected_employee,
            resident_type=SettlementResident.ResidentType.EMPLOYEE,
            status=SettlementResident.Status.ACTIVE,
        ))
        match = self._insert(ArrivalRosterMatch(
            version=self.version,
            status=ArrivalRosterMatch.Status.EXACT,
            method='routing-command-contradictory',
            quality='exact',
            matched_resident=matched_resident,
            evidence={},
        ))
        self._insert(ArrivalRosterRowReview(
            version=self.version,
            match=match,
            resident_resolution=ArrivalRosterRowReview.ResidentResolution.SELECTED,
            selected_resident=selected_resident,
            participation_status=ArrivalRosterRowReview.ParticipationStatus.ARRIVING,
            arrival_mode=ArrivalRosterRowReview.ArrivalMode.SELF,
            arrival_on=date(2026, 8, 2),
            departure_on=date(2026, 8, 30),
            revision=1,
            updated_by_access=self.access,
        ))

        with self.assertRaises(ValidationError) as caught:
            self._route()

        self.assertEqual(caught.exception.code, 'arrival_roster.routing_graph_inconsistent')
        self.assertEqual(ArrivalRosterRoutingBatch._base_manager.count(), 0)
        self.assertEqual(ArrivalRosterRoutingRow._base_manager.count(), 0)
        self.assertEqual(ArrivalRosterRoutingEvent._base_manager.count(), 0)

    def test_rolls_back_batch_rows_and_events_on_late_failure(self):
        employee = self._employee('Rollback')
        self._subject('Rollback', employee=employee)
        with patch(
            'rotations.arrival_roster_routing._trusted_insert_initial_events',
            side_effect=ValidationError('Тестовый откат', code='arrival_roster.routing_test_rollback'),
        ):
            with self.assertRaises(ValidationError) as caught:
                self._route()
        self.assertEqual(caught.exception.code, 'arrival_roster.routing_test_rollback')
        self.assertEqual(ArrivalRosterRoutingBatch._base_manager.count(), 0)
        self.assertEqual(ArrivalRosterRoutingRow._base_manager.count(), 0)
        self.assertEqual(ArrivalRosterRoutingEvent._base_manager.count(), 0)

    def test_is_idempotent_and_stale_after_superseding(self):
        employee = self._employee('Идемпотентность')
        self._subject('Идемпотентность', employee=employee)
        first = self._route()
        row_count = ArrivalRosterRoutingRow._base_manager.count()
        event_count = ArrivalRosterRoutingEvent._base_manager.count()

        second = self._route()

        self.assertEqual(second.pk, first.pk)
        self.assertEqual(ArrivalRosterRoutingBatch._base_manager.count(), 1)
        self.assertEqual(ArrivalRosterRoutingRow._base_manager.count(), row_count)
        self.assertEqual(ArrivalRosterRoutingEvent._base_manager.count(), event_count)
        ArrivalRosterVersion._base_manager.filter(pk=self.version.pk).update(
            status=ArrivalRosterVersion.Status.SUPERSEDED,
            superseded_at=timezone.now(),
        )
        with self.assertRaises(ValidationError) as caught:
            self._route()
        self.assertEqual(caught.exception.code, 'arrival_roster.routing_stale')
        self.assertEqual(ArrivalRosterRoutingEvent._base_manager.count(), event_count)

    def test_does_not_change_unrelated_domain_models_or_store_sensitive_json(self):
        employee = self._employee('Конфиденциальность', specialization=self.driver_specialization)
        employee.phone = '+79990000001'
        employee.save(update_fields=['phone'])
        self._subject('Конфиденциальность', employee=employee)
        before = {
            'employees': Employee.objects.count(),
            'residents': SettlementResident.objects.count(),
            'accesses': EmployeeAccess.objects.count(),
            'versions': ArrivalRosterVersion._base_manager.count(),
            'matches': ArrivalRosterMatch._base_manager.count(),
            'reviews': ArrivalRosterRowReview._base_manager.count(),
            'crew_plans': CrewPlan.objects.count(),
            'crew_slots': CrewPlanSlot.objects.count(),
            'equipment_assignments': EquipmentAssignment._base_manager.count(),
        }

        self._route()

        self.assertEqual(before, {
            'employees': Employee.objects.count(),
            'residents': SettlementResident.objects.count(),
            'accesses': EmployeeAccess.objects.count(),
            'versions': ArrivalRosterVersion._base_manager.count(),
            'matches': ArrivalRosterMatch._base_manager.count(),
            'reviews': ArrivalRosterRowReview._base_manager.count(),
            'crew_plans': CrewPlan.objects.count(),
            'crew_slots': CrewPlanSlot.objects.count(),
            'equipment_assignments': EquipmentAssignment._base_manager.count(),
        })
        for row in ArrivalRosterRoutingRow._base_manager.all():
            payload = str({
                'participation': row.participation_snapshot,
                'dates': row.dates_snapshot,
                'role': row.role_snapshot,
                'basis': row.role_basis_snapshot,
            })
            self.assertNotIn(employee.phone, payload)
            self.assertNotIn('access_id', payload)
            self.assertNotIn('confirmation_snapshot', payload)
            self.assertNotIn('fingerprint', payload)


class ArrivalRosterRoutingMigrationTests(TransactionTestCase):
    reset_sequences = True

    def _migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([('rotations', target)])
        return executor.loader.project_state([('rotations', target)]).apps

    def tearDown(self):
        self._migrate('0007_arrival_roster_routing')
        super().tearDown()

    def test_forward_reverse_and_cycle_0006_0007(self):
        apps_0006 = self._migrate('0006_arrival_roster_excel_revision')
        with self.assertRaises(LookupError):
            apps_0006.get_model('rotations', 'ArrivalRosterRoutingBatch')

        apps_0007 = self._migrate('0007_arrival_roster_routing')
        self.assertIsNotNone(apps_0007.get_model('rotations', 'ArrivalRosterRoutingBatch'))
        self.assertIsNotNone(apps_0007.get_model('rotations', 'ArrivalRosterRoutingRow'))
        self.assertIsNotNone(apps_0007.get_model('rotations', 'ArrivalRosterRoutingEvent'))

        apps_reversed = self._migrate('0006_arrival_roster_excel_revision')
        with self.assertRaises(LookupError):
            apps_reversed.get_model('rotations', 'ArrivalRosterRoutingRow')

        apps_forward = self._migrate('0007_arrival_roster_routing')
        self.assertIsNotNone(apps_forward.get_model('rotations', 'ArrivalRosterRoutingEvent'))
