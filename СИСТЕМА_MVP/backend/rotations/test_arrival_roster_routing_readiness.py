from dataclasses import FrozenInstanceError, fields
from datetime import date

from django.core.exceptions import ValidationError
from django.db import models
from django.test import TestCase
from django.utils import timezone

from assignments.models import CrewPlan, CrewPlanSlot, EquipmentAssignment, WorkShiftType
from assignments import test_deputy_arrival_roster_routing as deputy_test_fixtures
from shifts.models import WatchPeriod

from .arrival_roster_routing import (
    BATCH_STATE_CURRENT,
    BATCH_STATE_INCONSISTENT,
    BATCH_STATE_STALE,
    ERROR_BATCH_INCONSISTENT,
    ERROR_BATCH_NOT_FOUND,
    ERROR_BATCH_STALE,
    ERROR_OFFICIAL_ASSIGNMENT_INCONSISTENT,
    ERROR_OFFICIAL_ASSIGNMENT_MISSING,
    ERROR_ROUTING_INCONSISTENT,
    ERROR_ROUTING_PENDING,
    ERROR_ROUTING_REQUIRES_REVIEW,
    ERROR_ROUTING_STALE,
    ERROR_UNKNOWN_ROUTE_STATE,
    EVIDENCE_INCONSISTENT,
    EVIDENCE_NOT_ARRIVING,
    EVIDENCE_OFFICIAL_ASSIGNMENT_PUBLISHED,
    EVIDENCE_PENDING,
    EVIDENCE_REQUIRES_REVIEW,
    EVIDENCE_SENT_TO_CLERK,
    EVIDENCE_STALE,
    ArrivalRosterRoutingBatchEvidence,
    ArrivalRosterRoutingRowEvidence,
    deputy_arrival_roster_routing_queue,
    resolve_arrival_roster_routing_evidence,
    settlement_clerk_arrival_roster_routing_queue,
)
from .models import (
    ArrivalRosterRoutingBatch,
    ArrivalRosterRoutingEvent,
    ArrivalRosterRoutingRow,
    ArrivalRosterVersion,
)


class ArrivalRosterRoutingEvidenceTests(TestCase):
    """Structured, exact and read-only routing provenance for T3 consumers."""

    def setUp(self):
        deputy_test_fixtures.DeputyArrivalRosterRoutingQueueTests.setUp(self)

    _insert = deputy_test_fixtures.DeputyArrivalRosterRoutingQueueTests._insert
    _employee = deputy_test_fixtures.DeputyArrivalRosterRoutingQueueTests._employee
    _confirmed_batch = (
        deputy_test_fixtures.DeputyArrivalRosterRoutingQueueTests._confirmed_batch
    )
    _routing_row = deputy_test_fixtures.DeputyArrivalRosterRoutingQueueTests._routing_row
    _production_employee = (
        deputy_test_fixtures.DeputyArrivalRosterRoutingQueueTests._production_employee
    )
    _publish_event = deputy_test_fixtures.DeputyArrivalRosterRoutingQueueTests._publish_event

    def _event(self, row, event_type):
        return self._insert(ArrivalRosterRoutingEvent(
            routing_row=row,
            event_type=event_type,
            actor_access=self.timekeeper_access,
        ))

    def _direct_row(self, *, employee=None, batch=None, phone='+79995550831'):
        row = self._routing_row(
            employee=employee,
            batch=batch,
            route_state=ArrivalRosterRoutingRow.RouteState.TO_CLERK,
            role_code=None,
            phone=phone,
        )
        ArrivalRosterRoutingRow._base_manager.filter(pk=row.pk).update(
            role_snapshot={
                'role_code': None,
                'qualification_state': 'not_production',
            },
        )
        row.refresh_from_db()
        self._event(row, ArrivalRosterRoutingEvent.EventType.SENT_TO_CLERK)
        return row

    def _resolved_row(self, row, *, batch=None):
        result = resolve_arrival_roster_routing_evidence(
            batch_id=(batch or row.batch).pk,
        )
        return next(item for item in result.rows if item.routing_row_id == row.pk)

    def test_contract_is_frozen_safe_and_direct_rows_keep_exact_ids(self):
        internal = self._direct_row(employee=self._employee('Внутренний T3 resolver'))
        external = self._direct_row(employee=None, phone='+79995550832')

        result = resolve_arrival_roster_routing_evidence(batch_id=self.batch.pk)

        self.assertIsInstance(result, ArrivalRosterRoutingBatchEvidence)
        self.assertIsInstance(result.rows, tuple)
        self.assertEqual(result.batch_id, self.batch.pk)
        self.assertEqual(result.version_id, self.version.pk)
        self.assertEqual(result.watch_period_id, self.period.pk)
        self.assertEqual(result.batch_state, BATCH_STATE_CURRENT)
        self.assertIsNone(result.batch_blocker_code)
        by_id = {row.routing_row_id: row for row in result.rows}
        self.assertEqual(by_id[internal.pk].employee_id, internal.employee_id)
        self.assertEqual(by_id[external.pk].resident_id, external.resident_id)
        self.assertIsNone(by_id[external.pk].employee_id)
        self.assertEqual(by_id[internal.pk].evidence_state, EVIDENCE_SENT_TO_CLERK)
        self.assertEqual(by_id[external.pk].evidence_state, EVIDENCE_SENT_TO_CLERK)
        self.assertTrue(by_id[internal.pk].participating)
        self.assertTrue(by_id[external.pk].participating)
        with self.assertRaises(FrozenInstanceError):
            result.batch_state = 'changed'
        with self.assertRaises(FrozenInstanceError):
            by_id[internal.pk].evidence_state = 'changed'
        exposed_names = {field.name for field in fields(ArrivalRosterRoutingRowEvidence)}
        self.assertTrue(exposed_names.isdisjoint({
            'name', 'full_name', 'phone', 'source_snapshot', 'actor_access_id',
            'confirmation_sha256', 'confirmation_snapshot', 'fingerprint',
        }))

    def test_production_assignment_returns_exact_event_slot_assignment_and_shift(self):
        employee = self._production_employee('Водитель T3 resolver')
        row = self._routing_row(employee=employee)
        self._publish_event(row)
        event = ArrivalRosterRoutingEvent._base_manager.get(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
        )

        resolved = self._resolved_row(row)

        self.assertEqual(resolved.evidence_state, EVIDENCE_OFFICIAL_ASSIGNMENT_PUBLISHED)
        self.assertIsNone(resolved.blocker_code)
        self.assertEqual(resolved.latest_event_id, event.pk)
        self.assertEqual(resolved.crew_plan_slot_id, event.crew_plan_slot_id)
        self.assertEqual(
            resolved.equipment_assignment_id,
            event.equipment_assignment_id,
        )
        self.assertEqual(resolved.assignment_shift_type, WorkShiftType.SHIFT_1)

    def test_not_arriving_is_included_and_marked_without_blocker(self):
        row = self._routing_row(
            employee=self._production_employee('Не участвует T3 resolver'),
            route_state=ArrivalRosterRoutingRow.RouteState.NOT_PARTICIPATING,
            participation='not_arriving',
        )

        resolved = self._resolved_row(row)

        self.assertFalse(resolved.participating)
        self.assertEqual(resolved.evidence_state, EVIDENCE_NOT_ARRIVING)
        self.assertIsNone(resolved.blocker_code)

    def test_direct_and_production_pending_have_stable_distinct_codes(self):
        direct = self._routing_row(
            employee=self._employee('Прямой ожидает T3 resolver'),
            route_state=ArrivalRosterRoutingRow.RouteState.TO_CLERK,
            role_code=None,
        )
        ArrivalRosterRoutingRow._base_manager.filter(pk=direct.pk).update(
            role_snapshot={'role_code': None, 'qualification_state': 'not_production'},
        )
        production = self._routing_row(
            employee=self._production_employee('Назначение ожидает T3 resolver'),
        )

        direct_result = self._resolved_row(direct)
        production_result = self._resolved_row(production)

        self.assertEqual(
            (direct_result.evidence_state, direct_result.blocker_code),
            (EVIDENCE_PENDING, ERROR_ROUTING_PENDING),
        )
        self.assertEqual(
            (production_result.evidence_state, production_result.blocker_code),
            (EVIDENCE_PENDING, ERROR_OFFICIAL_ASSIGNMENT_MISSING),
        )

    def test_later_review_and_stale_events_override_ready_evidence(self):
        direct = self._direct_row(employee=self._employee('Review позже T3 resolver'))
        production = self._routing_row(
            employee=self._production_employee('Stale позже T3 resolver'),
        )
        self._publish_event(production)
        review = self._event(direct, ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW)
        stale = self._event(production, ArrivalRosterRoutingEvent.EventType.STALE)

        direct_result = self._resolved_row(direct)
        production_result = self._resolved_row(production)

        self.assertEqual(direct_result.latest_event_id, review.pk)
        self.assertEqual(
            (direct_result.evidence_state, direct_result.blocker_code),
            (EVIDENCE_REQUIRES_REVIEW, ERROR_ROUTING_REQUIRES_REVIEW),
        )
        self.assertEqual(production_result.latest_event_id, stale.pk)
        self.assertEqual(
            (production_result.evidence_state, production_result.blocker_code),
            (EVIDENCE_STALE, ERROR_ROUTING_STALE),
        )

    def test_latest_event_order_is_created_at_then_primary_key(self):
        row = self._direct_row(employee=self._employee('Порядок событий T3 resolver'))
        review = self._event(row, ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW)
        stale = self._event(row, ArrivalRosterRoutingEvent.EventType.STALE)
        same_time = timezone.now()
        models.QuerySet.update(
            ArrivalRosterRoutingEvent._base_manager.filter(pk__in=[review.pk, stale.pk]),
            created_at=same_time,
        )

        resolved = self._resolved_row(row)

        self.assertGreater(stale.pk, review.pk)
        self.assertEqual(resolved.latest_event_id, stale.pk)
        self.assertEqual(resolved.evidence_state, EVIDENCE_STALE)

    def test_damaged_official_assignment_is_inconsistent_not_ready(self):
        row = self._routing_row(
            employee=self._production_employee('Повреждённое назначение T3 resolver'),
        )
        self._publish_event(row)
        event = ArrivalRosterRoutingEvent._base_manager.get(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
        )
        EquipmentAssignment._base_manager.filter(
            pk=event.equipment_assignment_id,
        ).update(shift_type=None)

        resolved = self._resolved_row(row)

        self.assertEqual(resolved.evidence_state, EVIDENCE_INCONSISTENT)
        self.assertEqual(
            resolved.blocker_code,
            ERROR_OFFICIAL_ASSIGNMENT_INCONSISTENT,
        )
        self.assertEqual(resolved.latest_event_id, event.pk)
        self.assertEqual(resolved.crew_plan_slot_id, event.crew_plan_slot_id)
        self.assertEqual(
            resolved.equipment_assignment_id,
            event.equipment_assignment_id,
        )
        self.assertIsNone(resolved.assignment_shift_type)

    def test_unknown_route_and_impossible_event_combination_are_explicit(self):
        unknown = self._direct_row(employee=self._employee('Unknown route T3 resolver'))
        ArrivalRosterRoutingRow._base_manager.filter(pk=unknown.pk).update(
            route_state='unknown',
        )
        unknown.refresh_from_db()
        impossible = self._routing_row(
            employee=self._production_employee('Impossible event T3 resolver'),
        )
        self._event(impossible, ArrivalRosterRoutingEvent.EventType.SENT_TO_CLERK)
        review_route = self._routing_row(
            employee=self._employee('Review route T3 resolver'),
            route_state=ArrivalRosterRoutingRow.RouteState.REVIEW_REQUIRED,
            role_code=None,
        )

        unknown_result = self._resolved_row(unknown)
        impossible_result = self._resolved_row(impossible)
        review_result = self._resolved_row(review_route)

        self.assertEqual(
            (unknown_result.evidence_state, unknown_result.blocker_code),
            (EVIDENCE_INCONSISTENT, ERROR_UNKNOWN_ROUTE_STATE),
        )
        self.assertEqual(
            (impossible_result.evidence_state, impossible_result.blocker_code),
            (EVIDENCE_INCONSISTENT, ERROR_ROUTING_INCONSISTENT),
        )
        self.assertEqual(
            (review_result.evidence_state, review_result.blocker_code),
            (EVIDENCE_REQUIRES_REVIEW, ERROR_ROUTING_REQUIRES_REVIEW),
        )

    def test_batch_not_found_current_stale_and_inconsistent_are_controlled(self):
        current = resolve_arrival_roster_routing_evidence(batch_id=self.batch.pk)
        self.assertEqual(current.batch_state, BATCH_STATE_CURRENT)

        with self.assertRaises(ValidationError) as caught:
            resolve_arrival_roster_routing_evidence(batch_id=999999)
        self.assertEqual(caught.exception.code, ERROR_BATCH_NOT_FOUND)

        stale_version, stale_batch = self._confirmed_batch(
            self.period,
            status=ArrivalRosterVersion.Status.SUPERSEDED,
        )
        stale_row = self._direct_row(
            employee=self._employee('Историческая строка T3 resolver'),
            batch=stale_batch,
        )
        stale = resolve_arrival_roster_routing_evidence(batch_id=stale_batch.pk)
        self.assertEqual(stale.version_id, stale_version.pk)
        self.assertEqual(stale.batch_state, BATCH_STATE_STALE)
        self.assertEqual(stale.batch_blocker_code, ERROR_BATCH_STALE)
        self.assertEqual([item.routing_row_id for item in stale.rows], [stale_row.pk])

        models.QuerySet.update(
            ArrivalRosterRoutingBatch._base_manager.filter(pk=self.batch.pk),
            confirmation_sha256='c' * 64,
        )
        inconsistent = resolve_arrival_roster_routing_evidence(batch_id=self.batch.pk)
        self.assertEqual(inconsistent.batch_state, BATCH_STATE_INCONSISTENT)
        self.assertEqual(inconsistent.batch_blocker_code, ERROR_BATCH_INCONSISTENT)

    def test_exact_batch_does_not_mix_rows_from_other_batches(self):
        first = self._direct_row(employee=self._employee('Первый batch T3 resolver'))
        next_period = WatchPeriod.objects.create(
            name='Следующая вахта T3 resolver',
            watch_composition=self.composition,
            starts_on=date(2026, 10, 1),
            ends_on=date(2026, 10, 31),
            is_active=True,
        )
        _next_version, next_batch = self._confirmed_batch(next_period)
        second = self._direct_row(
            employee=self._employee('Второй batch T3 resolver'),
            batch=next_batch,
        )

        first_result = resolve_arrival_roster_routing_evidence(batch_id=self.batch.pk)
        second_result = resolve_arrival_roster_routing_evidence(batch_id=next_batch.pk)

        self.assertEqual([item.routing_row_id for item in first_result.rows], [first.pk])
        self.assertEqual([item.routing_row_id for item in second_result.rows], [second.pk])

    def test_repeated_resolution_is_read_only_and_preserves_existing_queues(self):
        direct = self._direct_row(employee=self._employee('Без записи T3 resolver'))
        production = self._routing_row(
            employee=self._production_employee('Без записи production T3 resolver'),
        )
        self._publish_event(production)
        before_queues = (
            deputy_arrival_roster_routing_queue(),
            settlement_clerk_arrival_roster_routing_queue(),
        )
        tracked_models = (
            ArrivalRosterRoutingBatch,
            ArrivalRosterRoutingRow,
            ArrivalRosterRoutingEvent,
            ArrivalRosterVersion,
            CrewPlan,
            CrewPlanSlot,
            EquipmentAssignment,
        )
        before_counts = {
            model: model._base_manager.count()
            for model in tracked_models
        }
        before_timestamps = {
            'batch': ArrivalRosterRoutingBatch._base_manager.get(pk=self.batch.pk).created_at,
            'version_created': ArrivalRosterVersion._base_manager.get(pk=self.version.pk).created_at,
            'version_updated': ArrivalRosterVersion._base_manager.get(pk=self.version.pk).updated_at,
            'row_created': ArrivalRosterRoutingRow._base_manager.get(pk=direct.pk).created_at,
        }

        first = resolve_arrival_roster_routing_evidence(batch_id=self.batch.pk)
        second = resolve_arrival_roster_routing_evidence(batch_id=self.batch.pk)

        self.assertEqual(first, second)
        self.assertEqual(before_counts, {
            model: model._base_manager.count()
            for model in tracked_models
        })
        self.assertEqual(
            before_timestamps,
            {
                'batch': ArrivalRosterRoutingBatch._base_manager.get(pk=self.batch.pk).created_at,
                'version_created': ArrivalRosterVersion._base_manager.get(pk=self.version.pk).created_at,
                'version_updated': ArrivalRosterVersion._base_manager.get(pk=self.version.pk).updated_at,
                'row_created': ArrivalRosterRoutingRow._base_manager.get(pk=direct.pk).created_at,
            },
        )
        self.assertEqual(before_queues, (
            deputy_arrival_roster_routing_queue(),
            settlement_clerk_arrival_roster_routing_queue(),
        ))
