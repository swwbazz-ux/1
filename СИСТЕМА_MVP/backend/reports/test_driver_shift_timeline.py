from datetime import datetime, timedelta

from django.test import TestCase
from django.utils import timezone

from assignments.models import (
    AssignmentStatus,
    HaulAssignment,
    HaulAssignmentAction,
)
from assignments.services import (
    apply_pending_haul_assignment,
    schedule_haul_assignment,
    schedule_haul_release,
)
from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import (
    DumpPoint,
    Equipment,
    EquipmentModel,
    EquipmentType,
    RockType,
)
from shifts.models import EmployeeShift, ShiftType
from trips.models import Trip, TripStatus
from users.models import Employee

from .driver_shift_timeline import (
    TimelineCategory,
    build_driver_shift_timeline,
    build_driver_shift_timelines,
    classify_downtime_reason,
)


class DriverShiftTimelineTests(TestCase):
    def setUp(self):
        self.start = timezone.make_aware(datetime(2026, 7, 28, 7, 0))
        self.end = self.start + timedelta(hours=12)
        self.driver = Employee.objects.create(
            full_name='Водитель временной ленты',
            work_category=Employee.WorkCategory.DRIVER,
            status=Employee.Status.ACTIVE,
        )
        truck_type = EquipmentType.objects.create(name='Самосвал timeline')
        truck_model = EquipmentModel.objects.create(
            equipment_type=truck_type,
            name='БелАЗ timeline',
        )
        self.truck = Equipment.objects.create(
            equipment_type=truck_type,
            model=truck_model,
            garage_number='TL-TRUCK',
        )
        excavator_type = EquipmentType.objects.create(name='Экскаватор timeline')
        self.excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='TL-EXC',
        )
        self.excavator_operator = Employee.objects.create(
            full_name='Машинист временной ленты',
            work_category=Employee.WorkCategory.EXCAVATOR_OPERATOR,
        )
        self.loading_shift = EmployeeShift.objects.create(
            employee=self.excavator_operator,
            shift_type=ShiftType.DAY,
            workplace_code='excavator',
            equipment=self.excavator,
            opened_at=self.start,
            closed_at=self.end,
        )
        self.rock = RockType.objects.create(name='Порода timeline')
        self.dump_point = DumpPoint.objects.create(name='Разгрузка timeline')
        self.shift = EmployeeShift.objects.create(
            employee=self.driver,
            shift_type=ShiftType.DAY,
            workplace_code='driver',
            equipment=self.truck,
            opened_at=self.start,
            closed_at=self.end,
        )

    def create_assignment(self, start=None, end=None):
        start = start or self.start
        assignment = HaulAssignment.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=start,
            ended_at=end,
        )
        HaulAssignment.objects.filter(pk=assignment.pk).update(assigned_at=start)
        assignment.refresh_from_db()
        return assignment

    def create_trip(self, start, end):
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            driver=self.driver,
            loading_shift=self.loading_shift,
            unloading_shift=self.shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.COMPLETED,
            completed_at=end,
        )
        Trip.objects.filter(pk=trip.pk).update(created_at=start)
        trip.refresh_from_db()
        return trip

    def test_builds_complete_shift_timeline_from_existing_server_events(self):
        self.create_assignment()
        self.create_trip(
            self.start + timedelta(hours=1),
            self.start + timedelta(hours=2),
        )
        reason = DowntimeReason.objects.get(name='Ожидание погрузки')
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=reason,
            started_at=self.start + timedelta(hours=3),
            ended_at=self.start + timedelta(hours=4),
        )

        timeline = build_driver_shift_timeline(self.shift, as_of=self.end)

        self.assertEqual(timeline.total_seconds, 12 * 3600)
        self.assertEqual(timeline.productive_seconds, 3600)
        self.assertEqual(
            timeline.seconds_by_category[TimelineCategory.DOWNTIME_EXTERNAL],
            3600,
        )
        self.assertEqual(timeline.unexplained_seconds, 10 * 3600)
        self.assertEqual(timeline.no_assignment_seconds, 0)
        self.assertEqual(timeline.coverage_percent, 16.67)

    def test_unload_to_next_loading_gap_stays_neutral_unexplained_time(self):
        self.create_assignment()
        self.create_trip(
            self.start + timedelta(hours=1),
            self.start + timedelta(hours=2),
        )
        self.create_trip(
            self.start + timedelta(hours=3),
            self.start + timedelta(hours=4),
        )

        timeline = build_driver_shift_timeline(self.shift, as_of=self.end)

        gap = [
            interval
            for interval in timeline.intervals
            if (
                interval.start == self.start + timedelta(hours=2)
                and interval.end == self.start + timedelta(hours=3)
            )
        ]
        self.assertEqual(len(gap), 1)
        self.assertEqual(gap[0].category, TimelineCategory.UNEXPLAINED)
        self.assertNotIn('trip_without_assignment', timeline.quality_flags)
        self.assertNotIn('trip_assignment_mismatch', timeline.quality_flags)

    def test_marks_period_without_accepted_assignment_as_external_gap(self):
        self.create_assignment(
            start=self.start + timedelta(hours=1),
            end=self.end - timedelta(hours=1),
        )

        timeline = build_driver_shift_timeline(self.shift, as_of=self.end)

        self.assertEqual(timeline.no_assignment_seconds, 2 * 3600)
        self.assertEqual(timeline.unexplained_seconds, 10 * 3600)

    def test_never_accepted_assignment_does_not_explain_shift_time(self):
        pending = HaulAssignment.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            action=HaulAssignmentAction.ASSIGN,
            status=AssignmentStatus.PENDING,
            effective_at=self.start,
        )
        cancelled = HaulAssignment.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            action=HaulAssignmentAction.ASSIGN,
            status=AssignmentStatus.CANCELLED,
            effective_at=self.start,
            ended_at=self.start + timedelta(hours=6),
        )
        HaulAssignment.objects.filter(pk__in=(pending.pk, cancelled.pk)).update(
            assigned_at=self.start,
        )

        timeline = build_driver_shift_timeline(self.shift, as_of=self.end)

        self.assertEqual(timeline.no_assignment_seconds, 12 * 3600)
        self.assertEqual(timeline.source_counts['assignment_count'], 0)

    def test_invalid_accepted_assignment_window_is_withheld_but_visible(self):
        HaulAssignment.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            action=HaulAssignmentAction.ASSIGN,
            status=AssignmentStatus.CANCELLED,
            accepted_at=self.end + timedelta(hours=3),
            ended_at=self.start - timedelta(hours=3),
        )
        waiting = DowntimeReason.objects.get(name='Ожидание погрузки')
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=waiting,
            started_at=self.start,
            ended_at=self.end,
        )

        timeline = build_driver_shift_timeline(self.shift, as_of=self.end)

        self.assertEqual(timeline.coverage_percent, 100.0)
        self.assertEqual(timeline.source_counts['assignment_count'], 1)
        self.assertFalse(timeline.usable_for_formula_review)
        self.assertIn('invalid_assignment_window', timeline.quality_flags)

    def test_closed_timeline_keeps_assignment_after_later_reassignment(self):
        original_assignment = self.create_assignment()
        self.create_trip(
            self.start + timedelta(hours=1),
            self.start + timedelta(hours=2),
        )
        before_reassignment = build_driver_shift_timeline(
            self.shift,
            as_of=self.end,
        )
        replacement_excavator = Equipment.objects.create(
            equipment_type=self.excavator.equipment_type,
            garage_number='TL-EXC-2',
        )
        scheduled_at = self.end + timedelta(hours=1)
        pending, created = schedule_haul_assignment(
            truck=self.truck,
            excavator=replacement_excavator,
            now=scheduled_at,
        )
        self.assertTrue(created)
        apply_pending_haul_assignment(
            pending.id,
            now=scheduled_at + timedelta(minutes=5),
        )

        original_assignment.refresh_from_db()
        after_reassignment = build_driver_shift_timeline(
            self.shift,
            as_of=self.end,
        )

        self.assertEqual(
            original_assignment.status,
            AssignmentStatus.CANCELLED,
        )
        self.assertEqual(
            original_assignment.ended_at,
            scheduled_at + timedelta(minutes=5),
        )
        self.assertEqual(
            after_reassignment.intervals,
            before_reassignment.intervals,
        )
        self.assertEqual(
            after_reassignment.source_counts,
            before_reassignment.source_counts,
        )
        self.assertEqual(
            after_reassignment.quality_metrics[
                'trip_without_assignment_seconds'
            ],
            0,
        )
        self.assertEqual(after_reassignment.no_assignment_seconds, 0)
        self.assertEqual(
            after_reassignment.source_counts['assignment_count'],
            1,
        )
        self.assertNotIn(
            'trip_without_assignment',
            after_reassignment.quality_flags,
        )

    def test_closed_timeline_keeps_assignment_after_later_release(self):
        original_assignment = self.create_assignment()
        self.create_trip(
            self.start + timedelta(hours=1),
            self.start + timedelta(hours=2),
        )
        before_release = build_driver_shift_timeline(
            self.shift,
            as_of=self.end,
        )
        scheduled_at = self.end + timedelta(hours=1)
        pending, created = schedule_haul_release(
            truck=self.truck,
            now=scheduled_at,
        )
        self.assertTrue(created)
        apply_pending_haul_assignment(
            pending.id,
            now=scheduled_at + timedelta(minutes=5),
        )

        original_assignment.refresh_from_db()
        after_release = build_driver_shift_timeline(
            self.shift,
            as_of=self.end,
        )

        self.assertEqual(
            original_assignment.status,
            AssignmentStatus.CANCELLED,
        )
        self.assertEqual(after_release.intervals, before_release.intervals)
        self.assertEqual(
            after_release.source_counts,
            before_release.source_counts,
        )
        self.assertEqual(after_release.no_assignment_seconds, 0)
        self.assertEqual(
            after_release.quality_metrics[
                'trip_without_assignment_seconds'
            ],
            0,
        )

    def test_withholds_trip_linked_to_incompatible_unloading_shift(self):
        self.create_assignment()
        replacement_driver = Employee.objects.create(
            full_name='Водитель ошибочной смены разгрузки',
            work_category=Employee.WorkCategory.DRIVER,
        )
        replacement_shift = EmployeeShift.objects.create(
            employee=replacement_driver,
            shift_type=ShiftType.NIGHT,
            workplace_code='driver',
            equipment=self.truck,
            opened_at=self.end,
            closed_at=self.end + timedelta(hours=12),
        )
        waiting = DowntimeReason.objects.get(name='Ожидание погрузки')
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=waiting,
            started_at=self.start,
            ended_at=self.start + timedelta(hours=1),
        )
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=waiting,
            started_at=self.start + timedelta(hours=2),
            ended_at=self.end,
        )
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            driver=replacement_driver,
            loading_shift=self.loading_shift,
            unloading_shift=replacement_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.COMPLETED,
            completed_at=self.start + timedelta(hours=2),
        )
        Trip.objects.filter(pk=trip.pk).update(
            created_at=self.start + timedelta(hours=1),
        )

        timeline = build_driver_shift_timeline(
            self.shift,
            as_of=replacement_shift.closed_at,
        )

        self.assertFalse(timeline.usable_for_formula_review)
        self.assertIn(
            'trip_unloading_shift_time_mismatch',
            timeline.quality_flags,
        )

    def test_unloading_shift_employee_must_match_completed_trip_driver(self):
        self.create_assignment()
        other_driver = Employee.objects.create(
            full_name='Ошибочно сохранённый водитель рейса',
            work_category=Employee.WorkCategory.DRIVER,
        )
        waiting = DowntimeReason.objects.get(name='Ожидание погрузки')
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=waiting,
            started_at=self.start,
            ended_at=self.end,
        )
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            driver=other_driver,
            loading_shift=self.loading_shift,
            unloading_shift=self.shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.COMPLETED,
            completed_at=self.start + timedelta(hours=2),
        )
        Trip.objects.filter(pk=trip.pk).update(
            created_at=self.start + timedelta(hours=1),
        )

        timeline = build_driver_shift_timeline(self.shift, as_of=self.end)

        self.assertEqual(timeline.source_counts['trip_count'], 1)
        self.assertEqual(timeline.productive_seconds, 0)
        self.assertFalse(timeline.usable_for_formula_review)
        self.assertIn(
            'trip_driver_unloading_shift_mismatch',
            timeline.quality_flags,
        )

    def test_withholds_trip_under_different_accepted_excavator(self):
        self.create_assignment()
        other_excavator = Equipment.objects.create(
            equipment_type=self.excavator.equipment_type,
            garage_number='TL-EXC-MISMATCH',
        )
        waiting = DowntimeReason.objects.get(name='Ожидание погрузки')
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=waiting,
            started_at=self.start,
            ended_at=self.start + timedelta(hours=1),
        )
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=waiting,
            started_at=self.start + timedelta(hours=2),
            ended_at=self.end,
        )
        trip = Trip.objects.create(
            excavator=other_excavator,
            truck=self.truck,
            driver=self.driver,
            loading_shift=self.loading_shift,
            unloading_shift=self.shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.COMPLETED,
            completed_at=self.start + timedelta(hours=2),
        )
        Trip.objects.filter(pk=trip.pk).update(
            created_at=self.start + timedelta(hours=1),
        )

        timeline = build_driver_shift_timeline(self.shift, as_of=self.end)

        self.assertFalse(timeline.usable_for_formula_review)
        self.assertIn('trip_assignment_mismatch', timeline.quality_flags)
        self.assertIn('trip_without_assignment', timeline.quality_flags)
        self.assertEqual(
            timeline.quality_metrics['trip_without_assignment_seconds'],
            3600,
        )
        self.assertEqual(timeline.productive_seconds, 3600)
        self.assertEqual(timeline.conflict_seconds, 0)

    def test_assignment_is_matched_only_at_trip_loading_time(self):
        trip_start = self.start + timedelta(hours=1)
        reassigned_at = trip_start + timedelta(minutes=5)
        HaulAssignment.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            action=HaulAssignmentAction.ASSIGN,
            status=AssignmentStatus.CANCELLED,
            accepted_at=self.start,
            ended_at=reassigned_at,
        )
        other_excavator = Equipment.objects.create(
            equipment_type=self.excavator.equipment_type,
            garage_number='TL-EXC-AFTER-LOAD',
        )
        HaulAssignment.objects.create(
            excavator=other_excavator,
            truck=self.truck,
            action=HaulAssignmentAction.ASSIGN,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=reassigned_at,
        )
        waiting = DowntimeReason.objects.get(name='Ожидание погрузки')
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=waiting,
            started_at=self.start,
            ended_at=trip_start,
        )
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=waiting,
            started_at=trip_start + timedelta(hours=1),
            ended_at=self.end,
        )
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            driver=self.driver,
            loading_shift=self.loading_shift,
            unloading_shift=self.shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.COMPLETED,
            completed_at=trip_start + timedelta(hours=1),
        )
        Trip.objects.filter(pk=trip.pk).update(created_at=trip_start)

        timeline = build_driver_shift_timeline(self.shift, as_of=self.end)

        self.assertTrue(timeline.usable_for_formula_review)
        self.assertEqual(timeline.productive_seconds, 3600)
        self.assertEqual(timeline.source_counts['assignment_count'], 2)
        self.assertNotIn('trip_without_assignment', timeline.quality_flags)
        self.assertNotIn('trip_assignment_mismatch', timeline.quality_flags)

    def test_carryover_keeps_assignment_that_ended_before_unloading_shift(self):
        trip_start = self.start - timedelta(minutes=30)
        reassigned_at = self.start - timedelta(minutes=15)
        HaulAssignment.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            action=HaulAssignmentAction.ASSIGN,
            status=AssignmentStatus.CANCELLED,
            accepted_at=self.start - timedelta(hours=2),
            ended_at=reassigned_at,
        )
        other_excavator = Equipment.objects.create(
            equipment_type=self.excavator.equipment_type,
            garage_number='TL-EXC-CARRY-REASSIGN',
        )
        HaulAssignment.objects.create(
            excavator=other_excavator,
            truck=self.truck,
            action=HaulAssignmentAction.ASSIGN,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=reassigned_at,
        )
        waiting = DowntimeReason.objects.get(name='Ожидание погрузки')
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=waiting,
            started_at=self.start + timedelta(minutes=30),
            ended_at=self.end,
        )
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            driver=self.driver,
            loading_shift=self.loading_shift,
            unloading_shift=self.shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.COMPLETED,
            completed_at=self.start + timedelta(minutes=30),
            is_carryover=True,
        )
        Trip.objects.filter(pk=trip.pk).update(created_at=trip_start)

        timeline = build_driver_shift_timeline(self.shift, as_of=self.end)

        self.assertTrue(timeline.usable_for_formula_review)
        self.assertEqual(timeline.productive_seconds, 30 * 60)
        self.assertEqual(timeline.source_counts['assignment_count'], 2)
        self.assertNotIn('trip_without_assignment', timeline.quality_flags)
        self.assertNotIn('trip_assignment_mismatch', timeline.quality_flags)

    def test_assignment_accepted_after_loading_does_not_validate_trip(self):
        trip_start = self.start + timedelta(hours=1)
        HaulAssignment.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            action=HaulAssignmentAction.ASSIGN,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=trip_start + timedelta(minutes=15),
        )
        waiting = DowntimeReason.objects.get(name='Ожидание погрузки')
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=waiting,
            started_at=self.start,
            ended_at=trip_start,
        )
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=waiting,
            started_at=trip_start + timedelta(hours=1),
            ended_at=self.end,
        )
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            driver=self.driver,
            loading_shift=self.loading_shift,
            unloading_shift=self.shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.COMPLETED,
            completed_at=trip_start + timedelta(hours=1),
        )
        Trip.objects.filter(pk=trip.pk).update(created_at=trip_start)

        timeline = build_driver_shift_timeline(self.shift, as_of=self.end)

        self.assertFalse(timeline.usable_for_formula_review)
        self.assertIn('trip_without_assignment', timeline.quality_flags)
        self.assertNotIn('trip_assignment_mismatch', timeline.quality_flags)
        self.assertEqual(
            timeline.quality_metrics['trip_without_assignment_seconds'],
            3600,
        )

    def test_classifies_current_driver_downtime_groups(self):
        waiting = DowntimeReason.objects.get(name='Ожидание разгрузки')
        repair = DowntimeReason.objects.get(name='Ремонт')
        lunch = DowntimeReason.objects.get(name='Обед')
        other = DowntimeReason.objects.get(name='Прочие')

        self.assertEqual(
            classify_downtime_reason(waiting),
            TimelineCategory.DOWNTIME_EXTERNAL,
        )
        self.assertEqual(
            classify_downtime_reason(repair),
            TimelineCategory.DOWNTIME_TECHNICAL,
        )
        self.assertEqual(
            classify_downtime_reason(lunch),
            TimelineCategory.DOWNTIME_REGULATED,
        )
        self.assertEqual(
            classify_downtime_reason(other),
            TimelineCategory.DOWNTIME_REVIEW,
        )

    def test_marks_trip_and_downtime_overlap_as_data_conflict(self):
        self.create_assignment()
        self.create_trip(
            self.start + timedelta(hours=1),
            self.start + timedelta(hours=2),
        )
        reason = DowntimeReason.objects.get(name='Ремонт')
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=reason,
            started_at=self.start + timedelta(hours=1, minutes=15),
            ended_at=self.start + timedelta(hours=1, minutes=45),
        )

        timeline = build_driver_shift_timeline(self.shift, as_of=self.end)

        self.assertEqual(timeline.conflict_seconds, 30 * 60)
        self.assertTrue(any(
            interval.category == TimelineCategory.DATA_CONFLICT
            for interval in timeline.intervals
        ))

    def test_open_events_are_capped_at_requested_as_of_time(self):
        self.shift.closed_at = None
        self.shift.save(update_fields=['closed_at'])
        self.create_assignment()
        reason = DowntimeReason.objects.get(name='Поломка')
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=reason,
            started_at=self.start + timedelta(hours=2),
        )
        as_of = self.start + timedelta(hours=3)

        timeline = build_driver_shift_timeline(self.shift, as_of=as_of)

        self.assertEqual(timeline.total_seconds, 3 * 3600)
        self.assertEqual(
            timeline.seconds_by_category[TimelineCategory.DOWNTIME_TECHNICAL],
            3600,
        )

    def test_invalid_trip_timestamps_are_never_formula_usable(self):
        self.create_assignment()
        waiting = DowntimeReason.objects.get(name='Ожидание погрузки')
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=waiting,
            started_at=self.start,
            ended_at=self.end,
        )
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            driver=self.driver,
            loading_shift=self.loading_shift,
            unloading_shift=None,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.COMPLETED,
            completed_at=self.start - timedelta(hours=1),
        )
        Trip.objects.filter(pk=trip.pk).update(
            created_at=self.end + timedelta(hours=1),
        )

        timeline = build_driver_shift_timeline(self.shift, as_of=self.end)

        self.assertEqual(timeline.coverage_percent, 100.0)
        self.assertEqual(timeline.source_counts['trip_count'], 1)
        self.assertEqual(timeline.productive_seconds, 0)
        self.assertFalse(timeline.usable_for_formula_review)
        self.assertIn('invalid_trip_window', timeline.quality_flags)

    def test_completed_status_without_completion_time_is_withheld(self):
        self.create_assignment()
        waiting = DowntimeReason.objects.get(name='Ожидание погрузки')
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=waiting,
            started_at=self.start,
            ended_at=self.end,
        )
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            driver=self.driver,
            loading_shift=self.loading_shift,
            unloading_shift=self.shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.COMPLETED,
            completed_at=None,
        )
        Trip.objects.filter(pk=trip.pk).update(
            created_at=self.start + timedelta(hours=1),
        )

        timeline = build_driver_shift_timeline(self.shift, as_of=self.end)

        self.assertEqual(timeline.source_counts['trip_count'], 1)
        self.assertEqual(timeline.productive_seconds, 0)
        self.assertFalse(timeline.usable_for_formula_review)
        self.assertIn(
            'completed_trip_without_completed_at',
            timeline.quality_flags,
        )

    def test_open_status_with_completion_time_is_withheld(self):
        self.create_assignment()
        waiting = DowntimeReason.objects.get(name='Ожидание погрузки')
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=waiting,
            started_at=self.start,
            ended_at=self.end,
        )
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            driver=self.driver,
            loading_shift=self.loading_shift,
            unloading_shift=self.shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            completed_at=self.start + timedelta(hours=2),
        )
        Trip.objects.filter(pk=trip.pk).update(
            created_at=self.start + timedelta(hours=1),
        )

        timeline = build_driver_shift_timeline(self.shift, as_of=self.end)

        self.assertEqual(timeline.source_counts['trip_count'], 1)
        self.assertEqual(timeline.productive_seconds, 0)
        self.assertFalse(timeline.usable_for_formula_review)
        self.assertIn(
            'open_status_trip_with_completed_at',
            timeline.quality_flags,
        )

    def test_cancelled_trip_is_never_productive(self):
        self.create_assignment()
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            driver=self.driver,
            loading_shift=self.loading_shift,
            unloading_shift=self.shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.CANCELLED,
            completed_at=self.start + timedelta(hours=2),
        )
        Trip.objects.filter(pk=trip.pk).update(
            created_at=self.start + timedelta(hours=1),
        )

        timeline = build_driver_shift_timeline(self.shift, as_of=self.end)

        self.assertEqual(timeline.source_counts['trip_count'], 0)
        self.assertEqual(timeline.productive_seconds, 0)

    def test_invalid_downtime_timestamps_are_never_formula_usable(self):
        self.create_assignment()
        self.create_trip(self.start, self.end)
        waiting = DowntimeReason.objects.get(name='Ожидание погрузки')
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=waiting,
            started_at=self.end + timedelta(hours=2),
            ended_at=self.start - timedelta(hours=2),
        )

        timeline = build_driver_shift_timeline(self.shift, as_of=self.end)

        self.assertEqual(timeline.coverage_percent, 100.0)
        self.assertEqual(timeline.source_counts['downtime_event_count'], 1)
        self.assertFalse(timeline.usable_for_formula_review)
        self.assertIn('invalid_downtime_window', timeline.quality_flags)

    def test_withholds_overlapping_driver_shifts_on_same_truck(self):
        other_driver = Employee.objects.create(
            full_name='Другой водитель той же техники',
            work_category=Employee.WorkCategory.DRIVER,
        )
        other_shift = EmployeeShift.objects.create(
            employee=other_driver,
            shift_type=ShiftType.NIGHT,
            workplace_code='driver',
            equipment=self.truck,
            opened_at=self.start,
            closed_at=self.end,
        )
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            driver=other_driver,
            loading_shift=self.loading_shift,
            unloading_shift=other_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.COMPLETED,
            completed_at=self.start + timedelta(hours=2),
        )
        Trip.objects.filter(pk=trip.pk).update(
            created_at=self.start + timedelta(hours=1),
        )

        timelines = build_driver_shift_timelines(
            (self.shift, other_shift),
            as_of=self.end,
        )

        self.assertTrue(all(
            not timeline.usable_for_formula_review
            for timeline in timelines
        ))
        self.assertTrue(all(
            'equipment_shift_overlap' in timeline.quality_flags
            for timeline in timelines
        ))

    def test_day_filter_still_sees_unselected_overlapping_night_shift(self):
        other_driver = Employee.objects.create(
            full_name='Ночной водитель вне выбранной группы',
            work_category=Employee.WorkCategory.DRIVER,
        )
        EmployeeShift.objects.create(
            employee=other_driver,
            shift_type=ShiftType.NIGHT,
            workplace_code='driver',
            equipment=self.truck,
            opened_at=self.start + timedelta(hours=1),
            closed_at=self.end,
        )

        timeline = build_driver_shift_timeline(
            self.shift,
            as_of=self.end,
        )

        self.assertFalse(timeline.usable_for_formula_review)
        self.assertIn(
            'equipment_shift_overlap',
            timeline.quality_flags,
        )

    def test_splits_carryover_trip_between_consecutive_driver_shifts(self):
        replacement_driver = Employee.objects.create(
            full_name='Водитель принимающей смены',
            work_category=Employee.WorkCategory.DRIVER,
        )
        replacement_end = self.end + timedelta(hours=12)
        replacement_shift = EmployeeShift.objects.create(
            employee=replacement_driver,
            shift_type=ShiftType.NIGHT,
            workplace_code='driver',
            equipment=self.truck,
            opened_at=self.end,
            closed_at=replacement_end,
        )
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            driver=replacement_driver,
            loading_shift=self.loading_shift,
            unloading_shift=replacement_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.COMPLETED,
            completed_at=self.end + timedelta(minutes=30),
            is_carryover=True,
        )
        Trip.objects.filter(pk=trip.pk).update(
            created_at=self.end - timedelta(minutes=30),
        )

        first, second = build_driver_shift_timelines(
            (self.shift, replacement_shift),
            as_of=replacement_end,
        )

        self.assertEqual(first.productive_seconds, 30 * 60)
        self.assertEqual(second.productive_seconds, 30 * 60)
        self.assertEqual(
            first.productive_seconds + second.productive_seconds,
            60 * 60,
        )

    def test_completed_carryover_remains_visible_in_middle_shift(self):
        middle_driver = Employee.objects.create(
            full_name='Водитель средней смены переходящего рейса',
            work_category=Employee.WorkCategory.DRIVER,
        )
        final_driver = Employee.objects.create(
            full_name='Водитель конечной смены переходящего рейса',
            work_category=Employee.WorkCategory.DRIVER,
        )
        middle_shift = EmployeeShift.objects.create(
            employee=middle_driver,
            shift_type=ShiftType.NIGHT,
            workplace_code='driver',
            equipment=self.truck,
            opened_at=self.end,
            closed_at=self.end + timedelta(hours=12),
        )
        final_shift = EmployeeShift.objects.create(
            employee=final_driver,
            shift_type=ShiftType.DAY,
            workplace_code='driver',
            equipment=self.truck,
            opened_at=middle_shift.closed_at,
            closed_at=middle_shift.closed_at + timedelta(hours=12),
        )
        self.create_assignment(start=self.start, end=final_shift.closed_at)
        waiting = DowntimeReason.objects.get(name='Ожидание погрузки')
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=middle_driver,
            reason=waiting,
            started_at=middle_shift.opened_at,
            ended_at=middle_shift.closed_at,
        )
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            driver=final_driver,
            loading_shift=self.loading_shift,
            unloading_shift=final_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.COMPLETED,
            completed_at=final_shift.opened_at + timedelta(hours=1),
            is_carryover=True,
        )
        Trip.objects.filter(pk=trip.pk).update(
            created_at=self.end - timedelta(hours=1),
        )

        timeline = build_driver_shift_timeline(
            middle_shift,
            as_of=final_shift.closed_at,
        )

        self.assertEqual(timeline.source_counts['trip_count'], 1)
        self.assertEqual(timeline.source_counts['carryover_trip_count'], 1)
        self.assertFalse(timeline.usable_for_formula_review)
        self.assertIn('data_conflict', timeline.quality_flags)

    def test_open_carryover_on_closed_shift_is_visible_but_withheld(self):
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            loading_shift=self.loading_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            is_carryover=True,
        )
        Trip.objects.filter(pk=trip.pk).update(
            created_at=self.end - timedelta(hours=1),
        )

        timeline = build_driver_shift_timeline(self.shift, as_of=self.end)

        self.assertEqual(timeline.productive_seconds, 3600)
        self.assertFalse(timeline.usable_for_formula_review)
        self.assertIn(
            'open_trip_on_closed_shift',
            timeline.quality_flags,
        )

    def test_withholds_implausibly_short_shift_from_future_rating(self):
        self.shift.closed_at = self.start + timedelta(minutes=30)
        self.shift.save(update_fields=['closed_at'])

        timeline = build_driver_shift_timeline(
            self.shift,
            as_of=self.shift.closed_at,
        )

        self.assertFalse(timeline.usable_for_formula_review)
        self.assertIn('shift_duration_under_1h', timeline.quality_flags)

    def test_withholds_shift_longer_than_sixteen_hours(self):
        self.shift.closed_at = self.start + timedelta(hours=17)
        self.shift.save(update_fields=['closed_at'])

        timeline = build_driver_shift_timeline(
            self.shift,
            as_of=self.shift.closed_at,
        )

        self.assertFalse(timeline.usable_for_formula_review)
        self.assertIn('shift_duration_over_16h', timeline.quality_flags)

    def test_invalid_closed_window_is_withheld_without_crashing_batch(self):
        self.shift.closed_at = self.start
        self.shift.save(update_fields=['closed_at'])

        timeline = build_driver_shift_timeline(
            self.shift,
            as_of=self.end,
        )

        self.assertEqual(timeline.total_seconds, 0)
        self.assertFalse(timeline.usable_for_formula_review)
        self.assertIn('invalid_shift_window', timeline.quality_flags)

    def test_future_shift_is_withheld_without_crashing_batch(self):
        future_start = self.end + timedelta(hours=1)
        self.shift.opened_at = future_start
        self.shift.closed_at = future_start + timedelta(hours=12)
        self.shift.save(update_fields=['opened_at', 'closed_at'])

        timeline = build_driver_shift_timeline(
            self.shift,
            as_of=self.end,
        )

        self.assertEqual(timeline.total_seconds, 0)
        self.assertFalse(timeline.usable_for_formula_review)
        self.assertIn('invalid_shift_window', timeline.quality_flags)

    def test_realistic_twenty_trip_shift_remains_shadow_only_with_gaps(self):
        self.create_assignment()
        for index in range(20):
            trip_start = self.start + timedelta(minutes=30 * index)
            self.create_trip(
                trip_start,
                trip_start + timedelta(minutes=10),
            )

        timeline = build_driver_shift_timeline(
            self.shift,
            as_of=self.end,
        )

        self.assertEqual(timeline.source_counts['trip_count'], 20)
        self.assertEqual(timeline.productive_seconds, 20 * 10 * 60)
        self.assertGreater(timeline.unexplained_seconds, 0)
        self.assertFalse(timeline.usable_for_formula_review)
        self.assertIn('unexplained_time', timeline.quality_flags)
