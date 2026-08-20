import hashlib
import json
from unittest import mock

from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone

from assignments.models import CrewPlan, CrewPlanSlot, EquipmentAssignment
from rotations.models import (
    ArrivalRosterRoutingBatch,
    ArrivalRosterRoutingEvent,
    ArrivalRosterRoutingRow,
    ArrivalRosterVersion,
)
from shifts.models import (
    WatchPeriodBrigadePhaseRow,
    WatchPeriodBrigadePhaseVersion,
)
from users.models import Employee, EmployeeAccess, Role

from . import cohorts as cohort_service
from . import test_arrival_roster_cohort_readiness as readiness_fixtures
from .cohorts import (
    ERROR_ACCESS_BLOCKED,
    ERROR_ACCESS_INACTIVE,
    ERROR_ACCESS_NOT_FOUND,
    ERROR_ACCESS_WRONG_ROLE,
    ERROR_BATCH_NOT_FOUND,
    ERROR_COHORT_ALREADY_INCONSISTENT,
    ERROR_COHORT_NOT_READY,
    ERROR_PROVENANCE_INCONSISTENT,
    ERROR_ROUTING_STALE,
    ArrivalRosterCohortCreationError,
    create_approved_arrival_roster_cohort,
)
from .models import SettlementCohort, SettlementCohortMember, SettlementResident


class ArrivalRosterCohortCreationTests(TestCase):
    """Closed T3 writer over exact routing and confirmed calendar evidence."""

    def setUp(self):
        readiness_fixtures.ArrivalRosterCohortReadinessTests.setUp(self)
        self.clerk = self._employee(
            'Делопроизводитель T3 writer',
            watch_composition=self.composition,
        )
        self.clerk_access = EmployeeAccess.objects.create(
            employee=self.clerk,
            role=self.clerk_role,
            access_code='t3-cohort-clerk',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    _insert = readiness_fixtures.ArrivalRosterCohortReadinessTests._insert
    _employee = readiness_fixtures.ArrivalRosterCohortReadinessTests._employee
    _confirmed_batch = (
        readiness_fixtures.ArrivalRosterCohortReadinessTests._confirmed_batch
    )
    _routing_row = readiness_fixtures.ArrivalRosterCohortReadinessTests._routing_row
    _production_employee = (
        readiness_fixtures.ArrivalRosterCohortReadinessTests._production_employee
    )
    _publish_event = readiness_fixtures.ArrivalRosterCohortReadinessTests._publish_event
    _confirm_calendar = (
        readiness_fixtures.ArrivalRosterCohortReadinessTests._confirm_calendar
    )
    _event = readiness_fixtures.ArrivalRosterCohortReadinessTests._event
    _direct_row = readiness_fixtures.ArrivalRosterCohortReadinessTests._direct_row

    def _internal_employee(self, name, *, brigade=2, production=False):
        employee = readiness_fixtures.ArrivalRosterCohortReadinessTests._internal_employee(
            self,
            name,
            brigade=brigade,
            production=production,
        )
        Employee.objects.filter(pk=employee.pk).update(
            watch_composition_id=self.composition.pk,
        )
        employee.refresh_from_db()
        return employee

    def _create(self, *, batch=None, access=None):
        return create_approved_arrival_roster_cohort(
            batch_id=(batch or self.batch).pk,
            actor_access_id=(access or self.clerk_access).pk,
        )

    def _supersede_source_and_create_batch(self):
        ArrivalRosterVersion._base_manager.filter(pk=self.version.pk).update(
            status=ArrivalRosterVersion.Status.SUPERSEDED,
            superseded_at=timezone.now(),
        )
        version, batch = self._confirmed_batch(self.period)
        return version, batch

    @staticmethod
    def _canonical_sha(snapshot):
        payload = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    def test_mixed_cohort_is_complete_exact_canonical_private_and_source_read_only(self):
        self._confirm_calendar()
        direct_employee = self._internal_employee('Прямой T3 writer')
        Employee.objects.filter(pk=direct_employee.pk).update(phone='+79995550199')
        direct_employee.refresh_from_db()
        direct = self._direct_row(employee=direct_employee)
        production = self._routing_row(employee=self._internal_employee(
            'Производственный T3 writer', production=True,
        ))
        self._publish_event(production)
        not_arriving = self._routing_row(
            employee=self._internal_employee('Не заезжает T3 writer'),
            route_state=ArrivalRosterRoutingRow.RouteState.NOT_PARTICIPATING,
            participation='not_arriving',
        )
        production_event = ArrivalRosterRoutingEvent._base_manager.get(
            routing_row=production,
            event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
        )
        source_counts = {
            model: model._base_manager.count()
            for model in (
                Employee, SettlementResident, ArrivalRosterRoutingRow,
                ArrivalRosterRoutingEvent, WatchPeriodBrigadePhaseVersion,
                WatchPeriodBrigadePhaseRow, ArrivalRosterVersion,
                ArrivalRosterRoutingBatch, CrewPlan, CrewPlanSlot,
                EquipmentAssignment,
            )
        }
        source_timestamps = {
            'direct': direct.created_at,
            'production': production.created_at,
            'event': production_event.created_at,
            'assignment_status': production_event.equipment_assignment.status,
            'assignment_assigned_at': production_event.equipment_assignment.assigned_at,
            'calendar_confirmed_at': WatchPeriodBrigadePhaseVersion._base_manager.get(
                status=WatchPeriodBrigadePhaseVersion.Status.CONFIRMED,
            ).confirmed_at,
        }

        cohort = self._create()

        cohort.refresh_from_db()
        self.assertEqual(cohort.status, SettlementCohort.Status.APPROVED)
        self.assertEqual(cohort.routing_batch_id, self.batch.pk)
        self.assertIsNone(cohort.source_revision_id)
        self.assertEqual(cohort.created_by_id, self.clerk.pk)
        self.assertEqual(cohort.approved_by_id, self.clerk.pk)
        members = list(cohort.members.order_by('routing_row_id'))
        self.assertEqual([member.routing_row_id for member in members], [direct.pk, production.pk])
        direct_member, production_member = members
        self.assertEqual(
            direct_member.shift_source_kind,
            SettlementCohortMember.ShiftSourceKind.CONFIRMED_BRIGADE_PHASE,
        )
        self.assertIsNone(direct_member.official_equipment_assignment_id)
        self.assertEqual(
            direct_member.routing_event.event_type,
            ArrivalRosterRoutingEvent.EventType.SENT_TO_CLERK,
        )
        self.assertEqual(
            production_member.shift_source_kind,
            SettlementCohortMember.ShiftSourceKind.INTERNAL_ASSIGNMENT,
        )
        self.assertEqual(
            production_member.official_equipment_assignment_id,
            production_event.equipment_assignment_id,
        )
        self.assertEqual(production_member.routing_event_id, production_event.pk)
        for member in members:
            self.assertEqual(member.routing_row.resident_id, member.resident_id)
            self.assertEqual(member.brigade_phase_row.phase, member.work_shift)
            self.assertEqual(
                member.shift_source_fingerprint,
                self._canonical_sha(member.shift_source_snapshot),
            )
        snapshot = cohort.source_snapshot
        self.assertEqual(snapshot['source_kind'], 'arrival_roster_routing')
        self.assertEqual(snapshot['routing_batch_id'], self.batch.pk)
        self.assertEqual(snapshot['arrival_roster_version_id'], self.version.pk)
        self.assertEqual(snapshot['watch_period_id'], self.period.pk)
        self.assertEqual(
            [item['routing_row_id'] for item in snapshot['members']],
            [direct.pk, production.pk],
        )
        self.assertEqual(snapshot['excluded_not_arriving_row_ids'], [not_arriving.pk])
        self.assertEqual(cohort.input_fingerprint, self._canonical_sha(snapshot))
        serialized = json.dumps({
            'cohort': snapshot,
            'members': [
                {
                    'basis': member.basis_snapshot,
                    'shift': member.shift_source_snapshot,
                    'production': member.production_context_snapshot,
                }
                for member in members
            ],
        }, ensure_ascii=False)
        for forbidden in (
            direct.resident.employee.full_name,
            direct.resident.employee.phone,
            production.resident.employee.full_name,
            production.resident.employee.phone,
            'confirmation_snapshot',
            'confirmation_sha256',
        ):
            if forbidden:
                self.assertNotIn(forbidden, serialized)
        self.assertNotIn('access', serialized.lower())
        self.assertNotIn('phone', serialized.lower())
        self.assertNotIn('pin', serialized.lower())
        self.assertEqual(source_counts, {
            model: model._base_manager.count()
            for model in source_counts
        })
        self.assertEqual(source_timestamps, {
            'direct': ArrivalRosterRoutingRow._base_manager.get(pk=direct.pk).created_at,
            'production': ArrivalRosterRoutingRow._base_manager.get(pk=production.pk).created_at,
            'event': ArrivalRosterRoutingEvent._base_manager.get(pk=production_event.pk).created_at,
            'assignment_status': EquipmentAssignment._base_manager.get(
                pk=production_event.equipment_assignment_id,
            ).status,
            'assignment_assigned_at': EquipmentAssignment._base_manager.get(
                pk=production_event.equipment_assignment_id,
            ).assigned_at,
            'calendar_confirmed_at': WatchPeriodBrigadePhaseVersion._base_manager.get(
                status=WatchPeriodBrigadePhaseVersion.Status.CONFIRMED,
            ).confirmed_at,
        })

    def test_exact_clerk_access_is_fail_closed_without_role_fallback(self):
        self._confirm_calendar()
        self._direct_row(employee=self._internal_employee('Доступ T3 writer'))
        wrong = EmployeeAccess.objects.create(
            employee=self.clerk,
            role=self.timekeeper_role,
            access_code='t3-wrong-role',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cases = (
            ('missing', 999999, ERROR_ACCESS_NOT_FOUND),
            ('wrong', wrong.pk, ERROR_ACCESS_WRONG_ROLE),
        )
        for _label, access_id, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(ArrivalRosterCohortCreationError) as caught:
                    create_approved_arrival_roster_cohort(
                        batch_id=self.batch.pk,
                        actor_access_id=access_id,
                    )
                self.assertEqual(caught.exception.code, expected_code)
                self.assertEqual(SettlementCohort.objects.count(), 0)

        EmployeeAccess.objects.filter(pk=self.clerk_access.pk).update(is_active=False)
        with self.assertRaises(ArrivalRosterCohortCreationError) as caught:
            self._create()
        self.assertEqual(caught.exception.code, ERROR_ACCESS_INACTIVE)
        EmployeeAccess.objects.filter(pk=self.clerk_access.pk).update(
            is_active=True,
            status=EmployeeAccess.Status.BLOCKED,
        )
        with self.assertRaises(ArrivalRosterCohortCreationError) as caught:
            self._create()
        self.assertEqual(caught.exception.code, ERROR_ACCESS_BLOCKED)
        self.assertEqual(SettlementCohort.objects.count(), 0)

    def test_missing_batch_is_controlled_before_any_write(self):
        with self.assertRaises(ArrivalRosterCohortCreationError) as caught:
            create_approved_arrival_roster_cohort(
                batch_id=999999,
                actor_access_id=self.clerk_access.pk,
            )
        self.assertEqual(caught.exception.code, ERROR_BATCH_NOT_FOUND)
        self.assertEqual(SettlementCohort.objects.count(), 0)
        self.assertEqual(SettlementCohortMember.objects.count(), 0)

    def test_external_blocks_the_whole_cohort(self):
        self._confirm_calendar()
        self._direct_row(employee=self._internal_employee('Готовая строка T3'))
        self._direct_row(employee=None, phone='+79995550191')
        with self.assertRaises(ArrivalRosterCohortCreationError) as caught:
            self._create()
        self.assertEqual(caught.exception.code, ERROR_COHORT_NOT_READY)
        self.assertIn('external_shift_unresolved', caught.exception.blocker_codes)
        self.assertEqual(SettlementCohort.objects.count(), 0)
        self.assertEqual(SettlementCohortMember.objects.count(), 0)

    def test_assignment_phase_mismatch_blocks_the_whole_cohort(self):
        self._confirm_calendar()
        direct = self._direct_row(employee=self._internal_employee('Готовая строка T3'))
        production = self._routing_row(employee=self._internal_employee(
            'Mismatch T3 writer', brigade=1, production=True,
        ))
        self._publish_event(production)
        with self.assertRaises(ArrivalRosterCohortCreationError) as caught:
            self._create()
        self.assertEqual(caught.exception.code, ERROR_COHORT_NOT_READY)
        self.assertIn('assignment_phase_mismatch', caught.exception.blocker_codes)
        self.assertEqual(SettlementCohort.objects.count(), 0)
        self.assertTrue(ArrivalRosterRoutingRow._base_manager.filter(pk=direct.pk).exists())

    def test_late_review_and_stale_are_complete_immutable_blockers(self):
        self._confirm_calendar()
        review = self._direct_row(employee=self._internal_employee('Review T3 writer'))
        stale = self._direct_row(employee=self._internal_employee('Stale T3 writer'))
        self._event(review, ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW)
        self._event(stale, ArrivalRosterRoutingEvent.EventType.STALE)

        with self.assertRaises(ArrivalRosterCohortCreationError) as caught:
            self._create()

        self.assertEqual(caught.exception.code, ERROR_ROUTING_STALE)
        self.assertEqual(
            caught.exception.blocker_codes,
            ('routing_requires_review', 'routing_stale'),
        )
        self.assertIsInstance(caught.exception.blocker_codes, tuple)
        self.assertEqual(SettlementCohort.objects.count(), 0)

    def test_member_failure_rolls_back_draft_members_and_transitions(self):
        self._confirm_calendar()
        self._direct_row(employee=self._internal_employee('Первый rollback T3'))
        self._direct_row(employee=self._internal_employee('Второй rollback T3'))
        original = cohort_service._trusted_save_routing_member
        calls = 0

        def fail_second(member):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValidationError('test member failure')
            return original(member)

        with mock.patch.object(
            cohort_service,
            '_trusted_save_routing_member',
            side_effect=fail_second,
        ):
            with self.assertRaises(ArrivalRosterCohortCreationError) as caught:
                self._create()

        self.assertEqual(caught.exception.code, ERROR_PROVENANCE_INCONSISTENT)
        self.assertEqual(SettlementCohort.objects.count(), 0)
        self.assertEqual(SettlementCohortMember.objects.count(), 0)

    def test_sequential_idempotency_survives_historical_source_without_writes(self):
        self._confirm_calendar()
        self._direct_row(employee=self._internal_employee('Idempotent T3 writer'))
        first = self._create()
        before = {
            'cohort_count': SettlementCohort.objects.count(),
            'member_count': SettlementCohortMember.objects.count(),
            'updated_at': first.updated_at,
            'approved_at': first.approved_at,
            'created_by_id': first.created_by_id,
            'fingerprint': first.input_fingerprint,
        }
        ArrivalRosterVersion._base_manager.filter(pk=self.version.pk).update(
            status=ArrivalRosterVersion.Status.SUPERSEDED,
            superseded_at=timezone.now(),
        )

        second = self._create()
        second.refresh_from_db()

        self.assertEqual(second.pk, first.pk)
        self.assertEqual(before, {
            'cohort_count': SettlementCohort.objects.count(),
            'member_count': SettlementCohortMember.objects.count(),
            'updated_at': second.updated_at,
            'approved_at': second.approved_at,
            'created_by_id': second.created_by_id,
            'fingerprint': second.input_fingerprint,
        })

    def test_corrupted_existing_cohort_is_not_returned_or_repaired(self):
        self._confirm_calendar()
        self._direct_row(employee=self._internal_employee('Corrupt T3 writer'))
        cohort = self._create()
        SettlementCohort._base_manager.filter(pk=cohort.pk).update(source_id='forged')

        with self.assertRaises(ArrivalRosterCohortCreationError) as caught:
            self._create()

        self.assertEqual(caught.exception.code, ERROR_COHORT_ALREADY_INCONSISTENT)
        self.assertEqual(SettlementCohort.objects.get(pk=cohort.pk).source_id, 'forged')
        self.assertEqual(SettlementCohort.objects.count(), 1)

    def test_unready_replacement_does_not_supersede_previous(self):
        self._confirm_calendar()
        self._direct_row(employee=self._internal_employee('Первый cohort T3'))
        first = self._create()
        _next_version, next_batch = self._supersede_source_and_create_batch()
        self.batch = next_batch
        self._direct_row(employee=None, phone='+79995550192')

        with self.assertRaises(ArrivalRosterCohortCreationError):
            self._create()
        first.refresh_from_db()
        self.assertEqual(first.status, SettlementCohort.Status.APPROVED)
        self.assertIsNone(first.superseded_at)

    def test_ready_replacement_supersedes_previous_atomically(self):
        self._confirm_calendar()
        self._direct_row(employee=self._internal_employee('Первый cohort T3'))
        first = self._create()
        _next_version, next_batch = self._supersede_source_and_create_batch()
        self.batch = next_batch
        self._direct_row(employee=self._internal_employee('Второй cohort T3'))
        replacement = self._create()
        first.refresh_from_db()
        replacement.refresh_from_db()
        self.assertEqual(first.status, SettlementCohort.Status.SUPERSEDED)
        self.assertEqual(replacement.status, SettlementCohort.Status.APPROVED)
        self.assertEqual(replacement.supersedes_id, first.pk)
        self.assertEqual(replacement.version, first.version + 1)
        self.assertEqual(first.superseded_at, replacement.approved_at)
        self.assertEqual(
            SettlementCohort.objects.filter(
                watch_period=self.period,
                status=SettlementCohort.Status.APPROVED,
            ).count(),
            1,
        )

    def test_calendar_replacement_before_creation_uses_and_protects_new_phase_row(self):
        self._confirm_calendar()
        row = self._direct_row(employee=self._internal_employee(
            'Calendar replacement T3', brigade=1,
        ))
        replacement = self._confirm_calendar(
            order_number='Приказ T3 writer № 2',
            phases=[
                {'brigade_number': 1, 'phase': 'day'},
                {'brigade_number': 2, 'phase': 'night'},
                {'brigade_number': 3, 'phase': 'off'},
                {'brigade_number': 4, 'phase': 'off'},
            ],
        )

        cohort = self._create()
        member = cohort.members.get(routing_row=row)

        self.assertEqual(member.work_shift, 'day')
        self.assertEqual(member.brigade_phase_row.version_id, replacement.pk)
        with self.assertRaises(ProtectedError):
            QuerySet.delete(
                WatchPeriodBrigadePhaseVersion._base_manager.filter(pk=replacement.pk),
            )

    def test_inactive_employee_and_inactive_role_are_access_inactive(self):
        self._confirm_calendar()
        self._direct_row(employee=self._internal_employee('Actor state T3'))
        Employee.objects.filter(pk=self.clerk.pk).update(is_active=False)
        with self.assertRaises(ArrivalRosterCohortCreationError) as caught:
            self._create()
        self.assertEqual(caught.exception.code, ERROR_ACCESS_INACTIVE)
        Employee.objects.filter(pk=self.clerk.pk).update(is_active=True)
        Role.objects.filter(pk=self.clerk_role.pk).update(is_active=False)
        with self.assertRaises(ArrivalRosterCohortCreationError) as caught:
            self._create()
        self.assertEqual(caught.exception.code, ERROR_ACCESS_INACTIVE)
        self.assertEqual(SettlementCohort.objects.count(), 0)
