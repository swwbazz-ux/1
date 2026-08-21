from dataclasses import FrozenInstanceError
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from assignments import test_deputy_arrival_roster_routing as routing_fixtures
from assignments.models import CrewPlan, CrewPlanSlot, EquipmentAssignment, WorkShiftType
from rotations.arrival_roster_routing import (
    BATCH_STATE_CURRENT,
    EVIDENCE_SENT_TO_CLERK,
    ArrivalRosterRoutingBatchEvidence,
    ArrivalRosterRoutingRowEvidence,
)
from rotations.employee_watch_profile_changes import (
    SOURCE_KIND_APPLIED_CHANGE,
    SOURCE_KIND_LEGACY_BASELINE,
    apply_employee_watch_profile_change,
    create_employee_watch_profile_change_draft,
    resolve_employee_watch_profile,
)
from rotations.models import (
    ArrivalRosterRoutingBatch,
    ArrivalRosterRoutingEvent,
    ArrivalRosterRoutingRow,
    ArrivalRosterVersion,
    EmployeeWatchProfileChange,
)
from shifts.brigade_phase_calendar import (
    WORK_SCHEDULE_CODE_12,
    confirm_watch_period_brigade_phase_version,
    create_watch_period_brigade_phase_draft,
    resolve_confirmed_brigade_phase,
)
from shifts.models import (
    WatchPeriodBrigadePhaseRow,
    WatchPeriodBrigadePhaseVersion,
)
from users.models import Employee, WatchComposition, WorkSchedule

from .cohort_readiness import (
    ERROR_ASSIGNMENT_PHASE_MISMATCH,
    ERROR_BRIGADE_OFF_BUT_ARRIVING,
    ERROR_CALENDAR_INCONSISTENT,
    ERROR_CALENDAR_NOT_CONFIRMED,
    ERROR_DUPLICATE_RESIDENT,
    ERROR_DUPLICATE_ROUTING_ROW,
    ERROR_EMPLOYEE_BRIGADE_MISSING,
    ERROR_EMPLOYEE_INACTIVE,
    ERROR_EMPLOYEE_SCHEDULE_MISSING,
    ERROR_EMPLOYEE_WATCH_COMPOSITION_MISSING,
    ERROR_EMPLOYEE_WATCH_COMPOSITION_MISMATCH,
    ERROR_EMPLOYEE_WATCH_PROFILE_INCONSISTENT,
    ERROR_EXTERNAL_SHIFT_UNRESOLVED,
    ERROR_NO_ARRIVING_MEMBERS,
    CohortReadyMember,
    SettlementCohortReadiness,
    SettlementCohortReadinessError,
    build_arrival_roster_cohort_readiness,
)
from .models import SettlementCohort, SettlementCohortMember


class ArrivalRosterCohortReadinessTests(TestCase):
    """T3 read-only orchestration over exact routing and calendar evidence."""

    def setUp(self):
        routing_fixtures.DeputyArrivalRosterRoutingQueueTests.setUp(self)
        self.schedule = WorkSchedule.objects.get(code=WORK_SCHEDULE_CODE_12)

    _insert = routing_fixtures.DeputyArrivalRosterRoutingQueueTests._insert
    _employee = routing_fixtures.DeputyArrivalRosterRoutingQueueTests._employee
    _confirmed_batch = (
        routing_fixtures.DeputyArrivalRosterRoutingQueueTests._confirmed_batch
    )
    _routing_row = routing_fixtures.DeputyArrivalRosterRoutingQueueTests._routing_row
    _production_employee = (
        routing_fixtures.DeputyArrivalRosterRoutingQueueTests._production_employee
    )
    _publish_event = routing_fixtures.DeputyArrivalRosterRoutingQueueTests._publish_event

    def _confirm_calendar(self, *, phases=None, order_number='Приказ T3 readiness № 1'):
        phases = phases or [
            {'brigade_number': 1, 'phase': 'night'},
            {'brigade_number': 2, 'phase': 'day'},
            {'brigade_number': 3, 'phase': 'off'},
            {'brigade_number': 4, 'phase': 'off'},
        ]
        draft = create_watch_period_brigade_phase_draft(
            watch_period_id=self.period.pk,
            work_schedule_id=self.schedule.pk,
            actor_access_id=self.timekeeper_access.pk,
            order_number=order_number,
            order_date='2026-08-01',
            effective_from=self.period.starts_on,
            order_document_sha256='a' * 64,
            schedule_designation='График № 12/1',
            schedule_document_sha256='b' * 64,
            brigade_phases=phases,
        )
        return confirm_watch_period_brigade_phase_version(
            version_id=draft.pk,
            actor_access_id=self.timekeeper_access.pk,
        )

    def _internal_employee(
        self,
        name,
        *,
        brigade=2,
        schedule=True,
        production=False,
        composition=...,
    ):
        composition = (
            self.period.watch_composition
            if composition is ...
            else composition
        )
        values = {
            'work_schedule': self.schedule if schedule else None,
            'brigade_number': brigade,
            'watch_composition': composition,
        }
        if production:
            return self._employee(
                name,
                phone='+79995550124',
                personnel_position=self.position,
                base_specialization=self.driver_specialization,
                **values,
            )
        return self._employee(name, **values)

    def _force_legacy_watch_profile_drift_for_test(
        self,
        employee,
        *,
        work_schedule_id=...,
        brigade_number=...,
        watch_composition_id=...,
        rotation=...,
    ):
        # Test-only corruption: imitates historical/pre-guard or external DB drift.
        with connection.cursor() as cursor:
            if work_schedule_id is not ...:
                cursor.execute(
                    'UPDATE users_employee SET work_schedule_id = %s WHERE id = %s',
                    [work_schedule_id, employee.pk],
                )
            if brigade_number is not ...:
                cursor.execute(
                    'UPDATE users_employee SET brigade_number = %s WHERE id = %s',
                    [brigade_number, employee.pk],
                )
            if watch_composition_id is not ...:
                cursor.execute(
                    'UPDATE users_employee SET watch_composition_id = %s WHERE id = %s',
                    [watch_composition_id, employee.pk],
                )
            if rotation is not ...:
                cursor.execute(
                    'UPDATE users_employee SET rotation = %s WHERE id = %s',
                    [rotation, employee.pk],
                )
        employee.refresh_from_db()

    def _event(self, row, event_type):
        return self._insert(ArrivalRosterRoutingEvent(
            routing_row=row,
            event_type=event_type,
            actor_access=self.timekeeper_access,
        ))

    def _direct_row(self, *, employee=None, phone='+79995550931'):
        row = self._routing_row(
            employee=employee,
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

    def _readiness(self):
        return build_arrival_roster_cohort_readiness(batch_id=self.batch.pk)

    def _create_profile_draft(
        self,
        employee,
        *,
        brigade,
        schedule=None,
        composition=None,
        basis_suffix='1',
    ):
        return create_employee_watch_profile_change_draft(
            employee_id=employee.pk,
            effective_watch_period_id=self.period.pk,
            new_work_schedule_id=(schedule or self.schedule).pk,
            new_brigade_number=brigade,
            new_watch_composition_id=(
                composition or self.period.watch_composition
            ).pk,
            basis_kind=EmployeeWatchProfileChange.BasisKind.EMPLOYEE_APPLICATION,
            basis_number=f'ЗАЯВЛЕНИЕ-READINESS-{basis_suffix}',
            basis_date=timezone.localdate(),
            basis='Заявление сотрудника для проверки готовности состава.',
            actor_access_id=self.timekeeper_access.pk,
        )

    def _apply_profile_change(self, employee, *, brigade, basis_suffix='1'):
        draft = self._create_profile_draft(
            employee,
            brigade=brigade,
            basis_suffix=basis_suffix,
        )
        return apply_employee_watch_profile_change(
            change_id=draft.pk,
            actor_access_id=self.timekeeper_access.pk,
        )

    def test_ready_direct_internal_day_and_night_use_exact_phase_rows(self):
        self._confirm_calendar()
        night_row = self._direct_row(employee=self._internal_employee(
            'Прямой NIGHT T3 readiness', brigade=1,
        ))
        day_row = self._direct_row(employee=self._internal_employee(
            'Прямой DAY T3 readiness', brigade=2,
        ))

        result = self._readiness()

        self.assertTrue(result.is_ready)
        self.assertEqual(result.blockers, ())
        self.assertEqual(
            [member.routing_row_id for member in result.ready_members],
            [night_row.pk, day_row.pk],
        )
        self.assertEqual(
            [member.work_shift for member in result.ready_members],
            ['night', 'day'],
        )
        for member in result.ready_members:
            phase = resolve_confirmed_brigade_phase(
                watch_period_id=self.period.pk,
                work_schedule_id=self.schedule.pk,
                brigade_number=(
                    1 if member.routing_row_id == night_row.pk else 2
                ),
            )
            self.assertEqual(member.brigade_phase_row_id, phase.row_id)
            self.assertIsNone(member.equipment_assignment_id)
            self.assertIsNone(member.crew_plan_slot_id)

    def test_legacy_baseline_ready_member_has_exact_profile_provenance(self):
        self._confirm_calendar()
        employee = self._internal_employee(
            'Legacy baseline T3 readiness',
            brigade=2,
        )
        row = self._direct_row(employee=employee)
        expected = resolve_employee_watch_profile(
            employee_id=employee.pk,
            watch_period_id=self.period.pk,
        )

        result = self._readiness()

        self.assertTrue(result.is_ready)
        member = result.ready_members[0]
        self.assertEqual(member.routing_row_id, row.pk)
        self.assertEqual(
            member.watch_profile_source_kind,
            SOURCE_KIND_LEGACY_BASELINE,
        )
        self.assertIsNone(member.employee_watch_profile_change_id)
        self.assertEqual(member.watch_profile_work_schedule_id, self.schedule.pk)
        self.assertEqual(member.watch_profile_brigade_number, 2)
        self.assertEqual(
            member.watch_profile_watch_composition_id,
            self.period.watch_composition_id,
        )
        self.assertEqual(
            member.watch_profile_fingerprint,
            expected.profile_fingerprint,
        )

    def test_applied_change_drives_phase_and_exact_member_provenance(self):
        self._confirm_calendar()
        employee = self._internal_employee(
            'Applied profile T3 readiness',
            brigade=1,
        )
        applied = self._apply_profile_change(employee, brigade=2)
        row = self._direct_row(employee=employee)
        expected_phase = resolve_confirmed_brigade_phase(
            watch_period_id=self.period.pk,
            work_schedule_id=self.schedule.pk,
            brigade_number=2,
        )

        result = self._readiness()

        self.assertTrue(result.is_ready)
        member = result.ready_members[0]
        self.assertEqual(member.routing_row_id, row.pk)
        self.assertEqual(member.work_shift, 'day')
        self.assertEqual(member.brigade_phase_row_id, expected_phase.row_id)
        self.assertEqual(
            member.watch_profile_source_kind,
            SOURCE_KIND_APPLIED_CHANGE,
        )
        self.assertEqual(member.employee_watch_profile_change_id, applied.pk)
        self.assertEqual(member.watch_profile_work_schedule_id, self.schedule.pk)
        self.assertEqual(member.watch_profile_brigade_number, 2)
        self.assertEqual(
            member.watch_profile_watch_composition_id,
            self.period.watch_composition_id,
        )
        self.assertRegex(member.watch_profile_fingerprint, r'^[0-9a-f]{64}$')

    def test_draft_profile_change_does_not_affect_readiness(self):
        self._confirm_calendar()
        employee = self._internal_employee(
            'Draft profile T3 readiness',
            brigade=2,
        )
        draft = self._create_profile_draft(employee, brigade=1)
        self._direct_row(employee=employee)

        result = self._readiness()

        self.assertTrue(result.is_ready)
        member = result.ready_members[0]
        self.assertEqual(
            member.watch_profile_source_kind,
            SOURCE_KIND_LEGACY_BASELINE,
        )
        self.assertIsNone(member.employee_watch_profile_change_id)
        self.assertNotEqual(member.employee_watch_profile_change_id, draft.pk)
        self.assertEqual(member.watch_profile_brigade_number, 2)

    def test_corrupted_effective_profile_history_is_blocked(self):
        self._confirm_calendar()
        employee = self._internal_employee(
            'Corrupted profile T3 readiness',
            brigade=1,
        )
        applied = self._apply_profile_change(employee, brigade=2)
        EmployeeWatchProfileChange._base_manager.filter(pk=applied.pk).update(
            source_fingerprint='0' * 64,
        )
        row = self._direct_row(employee=employee)

        result = self._readiness()

        self.assertFalse(result.is_ready)
        self.assertEqual(result.ready_members, ())
        self.assertEqual(
            [(blocker.routing_row_id, blocker.code) for blocker in result.blockers],
            [(row.pk, ERROR_EMPLOYEE_WATCH_PROFILE_INCONSISTENT)],
        )

    def test_resolved_watch_composition_must_match_exact_period(self):
        self._confirm_calendar()
        other_composition = WatchComposition.objects.create(
            code='readiness-other-watch-composition',
            name='Другой состав вахты для readiness',
            is_active=True,
        )
        employee = self._internal_employee(
            'Composition mismatch T3 readiness',
            brigade=2,
            composition=other_composition,
        )
        row = self._direct_row(employee=employee)

        result = self._readiness()

        self.assertFalse(result.is_ready)
        self.assertEqual(result.ready_members, ())
        self.assertEqual(
            [(blocker.routing_row_id, blocker.code) for blocker in result.blockers],
            [(row.pk, ERROR_EMPLOYEE_WATCH_COMPOSITION_MISMATCH)],
        )

    def test_production_assignment_uses_resolved_profile_phase(self):
        self._confirm_calendar()
        employee = self._internal_employee(
            'Production applied profile T3 readiness',
            brigade=1,
            production=True,
        )
        applied = self._apply_profile_change(employee, brigade=2)
        row = self._routing_row(employee=employee)
        self._publish_event(row)

        result = self._readiness()

        self.assertTrue(result.is_ready)
        member = result.ready_members[0]
        self.assertEqual(member.work_shift, 'day')
        self.assertEqual(member.employee_watch_profile_change_id, applied.pk)
        self.assertEqual(member.watch_profile_brigade_number, 2)
        self.assertIsNotNone(member.equipment_assignment_id)

    def test_ready_production_requires_matching_exact_assignment_and_phase(self):
        self._confirm_calendar()
        row = self._routing_row(employee=self._internal_employee(
            'Производственный DAY T3 readiness', brigade=2, production=True,
        ))
        self._publish_event(row)
        event = ArrivalRosterRoutingEvent._base_manager.get(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
        )

        result = self._readiness()

        self.assertTrue(result.is_ready)
        self.assertEqual(len(result.ready_members), 1)
        member = result.ready_members[0]
        self.assertEqual(member.routing_event_id, event.pk)
        self.assertEqual(member.crew_plan_slot_id, event.crew_plan_slot_id)
        self.assertEqual(
            member.equipment_assignment_id,
            event.equipment_assignment_id,
        )
        self.assertEqual(member.work_shift, WorkShiftType.SHIFT_1)

    def test_production_assignment_phase_mismatch_is_blocked(self):
        self._confirm_calendar()
        row = self._routing_row(employee=self._internal_employee(
            'Производственный mismatch T3 readiness', brigade=1, production=True,
        ))
        self._publish_event(row)

        result = self._readiness()

        self.assertFalse(result.is_ready)
        self.assertEqual(result.ready_members, ())
        self.assertEqual(
            [(blocker.routing_row_id, blocker.code) for blocker in result.blockers],
            [(row.pk, ERROR_ASSIGNMENT_PHASE_MISMATCH)],
        )

    def test_participating_employee_in_off_phase_is_blocked(self):
        self._confirm_calendar()
        row = self._direct_row(employee=self._internal_employee(
            'Межвахта T3 readiness', brigade=3,
        ))

        result = self._readiness()

        self.assertEqual(result.ready_members, ())
        self.assertEqual(result.blockers[0].routing_row_id, row.pk)
        self.assertEqual(result.blockers[0].code, ERROR_BRIGADE_OFF_BUT_ARRIVING)

    def test_external_sent_to_clerk_stays_unresolved_without_calendar_call(self):
        row = self._direct_row(employee=None, phone='+79995550932')

        with mock.patch(
            'settlement.cohort_readiness.resolve_confirmed_brigade_phase',
        ) as calendar, mock.patch(
            'settlement.cohort_readiness.resolve_employee_watch_profile',
        ) as profile_resolver:
            result = self._readiness()

        calendar.assert_not_called()
        profile_resolver.assert_not_called()
        self.assertEqual(result.ready_members, ())
        self.assertEqual(
            [(blocker.routing_row_id, blocker.code) for blocker in result.blockers],
            [(row.pk, ERROR_EXTERNAL_SHIFT_UNRESOLVED)],
        )
        with self.assertRaises(FrozenInstanceError):
            result.blockers[0].code = 'changed'

    def test_employee_state_schedule_and_brigade_are_fail_closed(self):
        self._confirm_calendar()
        inactive = self._internal_employee('Неактивный T3 readiness', brigade=2)
        Employee.objects.filter(pk=inactive.pk).update(is_active=False)
        inactive_row = self._direct_row(employee=inactive)
        no_schedule = self._internal_employee(
            'Без графика T3 readiness', brigade=2, schedule=False,
        )
        no_schedule_row = self._direct_row(employee=no_schedule)
        no_brigade = self._internal_employee(
            'Без бригады T3 readiness', brigade=None,
        )
        no_brigade_row = self._direct_row(employee=no_brigade)
        no_composition = self._internal_employee(
            'Без состава вахты T3 readiness',
            brigade=2,
            composition=None,
        )
        no_composition_row = self._direct_row(employee=no_composition)

        result = self._readiness()

        self.assertEqual(result.ready_members, ())
        self.assertEqual(
            [(blocker.routing_row_id, blocker.code) for blocker in result.blockers],
            [
                (inactive_row.pk, ERROR_EMPLOYEE_INACTIVE),
                (no_schedule_row.pk, ERROR_EMPLOYEE_SCHEDULE_MISSING),
                (no_brigade_row.pk, ERROR_EMPLOYEE_BRIGADE_MISSING),
                (
                    no_composition_row.pk,
                    ERROR_EMPLOYEE_WATCH_COMPOSITION_MISSING,
                ),
            ],
        )

    def test_absent_and_corrupted_confirmed_calendar_are_mapped_safely(self):
        missing_row = self._direct_row(employee=self._internal_employee(
            'Нет календаря T3 readiness', brigade=2,
        ))
        missing = self._readiness()
        self.assertEqual(missing.blockers[0].routing_row_id, missing_row.pk)
        self.assertEqual(missing.blockers[0].code, ERROR_CALENDAR_NOT_CONFIRMED)

        confirmed = self._confirm_calendar()
        WatchPeriodBrigadePhaseVersion._base_manager.filter(pk=confirmed.pk).update(
            source_fingerprint='f' * 64,
        )
        corrupted = self._readiness()
        self.assertEqual(corrupted.blockers[0].routing_row_id, missing_row.pk)
        self.assertEqual(corrupted.blockers[0].code, ERROR_CALENDAR_INCONSISTENT)

    def test_routing_pending_review_stale_and_inconsistent_are_not_lost(self):
        pending = self._routing_row(employee=self._internal_employee(
            'Pending T3 readiness', brigade=2, production=True,
        ))
        review = self._direct_row(employee=self._internal_employee(
            'Review T3 readiness', brigade=2,
        ))
        self._event(review, ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW)
        stale = self._direct_row(employee=self._internal_employee(
            'Stale T3 readiness', brigade=2,
        ))
        self._event(stale, ArrivalRosterRoutingEvent.EventType.STALE)
        inconsistent = self._direct_row(employee=self._internal_employee(
            'Inconsistent T3 readiness', brigade=2,
        ))
        ArrivalRosterRoutingRow._base_manager.filter(pk=inconsistent.pk).update(
            route_state='unknown',
        )

        result = self._readiness()

        self.assertEqual(result.ready_members, ())
        self.assertEqual(
            [(blocker.routing_row_id, blocker.code) for blocker in result.blockers],
            [
                (pending.pk, 'official_assignment_missing'),
                (review.pk, 'routing_requires_review'),
                (stale.pk, 'routing_stale'),
                (inconsistent.pk, 'unknown_route_state'),
            ],
        )

    def test_stale_batch_has_batch_blocker_first_and_no_ready_members(self):
        row = self._direct_row(employee=self._internal_employee(
            'Старая передача T3 readiness', brigade=2,
        ))
        ArrivalRosterVersion._base_manager.filter(pk=self.version.pk).update(
            status=ArrivalRosterVersion.Status.SUPERSEDED,
            superseded_at=timezone.now(),
        )

        result = self._readiness()

        self.assertFalse(result.is_ready)
        self.assertEqual(result.ready_members, ())
        self.assertIsNone(result.blockers[0].routing_row_id)
        self.assertEqual(result.blockers[0].code, 'batch_stale')
        self.assertEqual(result.blockers[1].routing_row_id, row.pk)

    def test_not_arriving_is_excluded_and_never_calls_calendar(self):
        row = self._routing_row(
            employee=self._internal_employee('Не приезжает T3 readiness', brigade=2),
            route_state=ArrivalRosterRoutingRow.RouteState.NOT_PARTICIPATING,
            participation='not_arriving',
        )

        with mock.patch(
            'settlement.cohort_readiness.resolve_confirmed_brigade_phase',
        ) as calendar, mock.patch(
            'settlement.cohort_readiness.resolve_employee_watch_profile',
        ) as profile_resolver:
            result = self._readiness()

        calendar.assert_not_called()
        profile_resolver.assert_not_called()
        self.assertEqual(result.excluded_not_arriving_row_ids, (row.pk,))
        self.assertEqual(result.ready_members, ())
        self.assertEqual(result.blockers[0].code, ERROR_NO_ARRIVING_MEMBERS)
        self.assertFalse(result.is_ready)

    def test_mixed_batch_has_complete_exclusive_deterministic_coverage(self):
        self._confirm_calendar()
        ready = self._direct_row(employee=self._internal_employee(
            'Готов T3 readiness', brigade=2,
        ))
        excluded = self._routing_row(
            employee=self._internal_employee('Исключён T3 readiness', brigade=2),
            route_state=ArrivalRosterRoutingRow.RouteState.NOT_PARTICIPATING,
            participation='not_arriving',
        )
        external = self._direct_row(employee=None, phone='+79995550933')
        pending = self._routing_row(employee=self._internal_employee(
            'Ожидает T3 readiness', brigade=2, production=True,
        ))

        first = self._readiness()
        second = self._readiness()

        self.assertEqual(first, second)
        self.assertEqual([item.routing_row_id for item in first.ready_members], [ready.pk])
        self.assertEqual(first.excluded_not_arriving_row_ids, (excluded.pk,))
        self.assertEqual(
            [item.routing_row_id for item in first.blockers],
            [external.pk, pending.pk],
        )
        categorized = (
            {item.routing_row_id for item in first.ready_members}
            | {item.routing_row_id for item in first.blockers if item.routing_row_id}
            | set(first.excluded_not_arriving_row_ids)
        )
        self.assertEqual(categorized, {ready.pk, excluded.pk, external.pk, pending.pk})

    def test_duplicate_row_and_resident_are_controlled_blockers(self):
        duplicate_row = ArrivalRosterRoutingRowEvidence(
            routing_row_id=101,
            resident_id=201,
            employee_id=301,
            route_state='to_clerk',
            participating=True,
            evidence_state=EVIDENCE_SENT_TO_CLERK,
            blocker_code=None,
            latest_event_id=401,
            latest_event_type='sent_to_clerk',
            crew_plan_slot_id=None,
            equipment_assignment_id=None,
            assignment_shift_type=None,
        )
        resident_first = ArrivalRosterRoutingRowEvidence(
            routing_row_id=102,
            resident_id=202,
            employee_id=302,
            route_state='to_clerk',
            participating=True,
            evidence_state=EVIDENCE_SENT_TO_CLERK,
            blocker_code=None,
            latest_event_id=402,
            latest_event_type='sent_to_clerk',
            crew_plan_slot_id=None,
            equipment_assignment_id=None,
            assignment_shift_type=None,
        )
        resident_second = ArrivalRosterRoutingRowEvidence(
            routing_row_id=103,
            resident_id=202,
            employee_id=303,
            route_state='to_clerk',
            participating=True,
            evidence_state=EVIDENCE_SENT_TO_CLERK,
            blocker_code=None,
            latest_event_id=403,
            latest_event_type='sent_to_clerk',
            crew_plan_slot_id=None,
            equipment_assignment_id=None,
            assignment_shift_type=None,
        )
        routing = ArrivalRosterRoutingBatchEvidence(
            batch_id=self.batch.pk,
            version_id=self.version.pk,
            watch_period_id=self.period.pk,
            batch_state=BATCH_STATE_CURRENT,
            batch_blocker_code=None,
            rows=(duplicate_row, duplicate_row, resident_first, resident_second),
        )
        with mock.patch(
            'settlement.cohort_readiness.resolve_arrival_roster_routing_evidence',
            return_value=routing,
        ):
            result = self._readiness()

        self.assertEqual(result.ready_members, ())
        self.assertEqual(
            [(blocker.routing_row_id, blocker.code) for blocker in result.blockers],
            [
                (101, ERROR_DUPLICATE_ROUTING_ROW),
                (102, ERROR_DUPLICATE_RESIDENT),
                (103, ERROR_DUPLICATE_RESIDENT),
            ],
        )

    def test_corrupted_structured_ready_shape_is_not_accepted(self):
        self._confirm_calendar()
        employee = self._internal_employee('Повреждённый shape T3 readiness', brigade=2)
        evidence = ArrivalRosterRoutingRowEvidence(
            routing_row_id=501,
            resident_id=502,
            employee_id=employee.pk,
            route_state='to_clerk',
            participating=True,
            evidence_state=EVIDENCE_SENT_TO_CLERK,
            blocker_code=None,
            latest_event_id=503,
            latest_event_type='created',
            crew_plan_slot_id=None,
            equipment_assignment_id=None,
            assignment_shift_type=None,
        )
        routing = ArrivalRosterRoutingBatchEvidence(
            batch_id=self.batch.pk,
            version_id=self.version.pk,
            watch_period_id=self.period.pk,
            batch_state=BATCH_STATE_CURRENT,
            batch_blocker_code=None,
            rows=(evidence,),
        )
        with mock.patch(
            'settlement.cohort_readiness.resolve_arrival_roster_routing_evidence',
            return_value=routing,
        ):
            result = self._readiness()

        self.assertEqual(result.ready_members, ())
        self.assertEqual(
            [(blocker.routing_row_id, blocker.code) for blocker in result.blockers],
            [(evidence.routing_row_id, 'routing_inconsistent')],
        )

    def test_calendar_replacement_uses_new_exact_phase_row(self):
        self._confirm_calendar()
        row = self._direct_row(employee=self._internal_employee(
            'Replacement T3 readiness', brigade=1,
        ))
        first = self._readiness().ready_members[0]

        self._confirm_calendar(
            order_number='Приказ T3 readiness № 2',
            phases=[
                {'brigade_number': 1, 'phase': 'day'},
                {'brigade_number': 2, 'phase': 'night'},
                {'brigade_number': 3, 'phase': 'off'},
                {'brigade_number': 4, 'phase': 'off'},
            ],
        )
        replacement = self._readiness().ready_members[0]

        self.assertEqual(first.routing_row_id, row.pk)
        self.assertEqual(first.work_shift, 'night')
        self.assertEqual(replacement.work_shift, 'day')
        self.assertNotEqual(first.brigade_phase_row_id, replacement.brigade_phase_row_id)

    def test_result_is_immutable_and_repeated_call_is_fully_read_only(self):
        calendar = self._confirm_calendar()
        employee = self._internal_employee('Read-only T3 readiness', brigade=2)
        row = self._direct_row(employee=employee)
        tracked_models = (
            ArrivalRosterRoutingBatch,
            ArrivalRosterRoutingRow,
            ArrivalRosterRoutingEvent,
            ArrivalRosterVersion,
            WatchPeriodBrigadePhaseVersion,
            WatchPeriodBrigadePhaseRow,
            Employee,
            SettlementCohort,
            SettlementCohortMember,
            CrewPlan,
            CrewPlanSlot,
            EquipmentAssignment,
            EmployeeWatchProfileChange,
        )
        before_counts = {
            model: model._base_manager.count()
            for model in tracked_models
        }
        before_state = {
            'employee_updated': Employee.objects.get(pk=employee.pk).updated_at,
            'routing_created': ArrivalRosterRoutingRow._base_manager.get(pk=row.pk).created_at,
            'version_updated': ArrivalRosterVersion._base_manager.get(pk=self.version.pk).updated_at,
            'calendar_created': calendar.created_at,
            'calendar_confirmed': calendar.confirmed_at,
            'calendar_actor': calendar.confirmed_by_access_id,
        }

        first = self._readiness()
        second = self._readiness()

        self.assertIsInstance(first, SettlementCohortReadiness)
        self.assertIsInstance(first.ready_members[0], CohortReadyMember)
        with self.assertRaises(FrozenInstanceError):
            first.is_ready = False
        with self.assertRaises(FrozenInstanceError):
            first.ready_members[0].work_shift = 'night'
        self.assertEqual(first, second)
        self.assertEqual(before_counts, {
            model: model._base_manager.count()
            for model in tracked_models
        })
        self.assertEqual(before_state, {
            'employee_updated': Employee.objects.get(pk=employee.pk).updated_at,
            'routing_created': ArrivalRosterRoutingRow._base_manager.get(pk=row.pk).created_at,
            'version_updated': ArrivalRosterVersion._base_manager.get(pk=self.version.pk).updated_at,
            'calendar_created': WatchPeriodBrigadePhaseVersion._base_manager.get(
                pk=calendar.pk,
            ).created_at,
            'calendar_confirmed': WatchPeriodBrigadePhaseVersion._base_manager.get(
                pk=calendar.pk,
            ).confirmed_at,
            'calendar_actor': WatchPeriodBrigadePhaseVersion._base_manager.get(
                pk=calendar.pk,
            ).confirmed_by_access_id,
        })

    def test_missing_batch_is_a_controlled_domain_error(self):
        with self.assertRaises(SettlementCohortReadinessError) as caught:
            build_arrival_roster_cohort_readiness(batch_id=999999)
        self.assertEqual(caught.exception.code, 'batch_not_found')
        self.assertNotIn('Traceback', str(caught.exception))
