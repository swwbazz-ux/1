import io
from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from assignments.models import AssignmentStatus, EquipmentAssignment
from references.models import Equipment
from shifts.models import ShiftType
from users.models import Employee, WorkSchedule

from .driver_rating_materialization import (
    DriverRatingMaterializationError,
    DriverRatingSnapshotUnavailable,
    get_materialized_driver_rating_assignment_group,
    refresh_driver_rating_assignment_group,
)
from .driver_rating_scope_membership import (
    discover_driver_rating_assignment_group_scope,
    discover_driver_rating_assignment_groups,
)
from .driver_watch_rating import (
    build_driver_rating_assignment_group_period,
    build_driver_rating_period,
)
from .models import DriverRatingPeriodMaterializedSnapshot, RatingPeriod
from .test_driver_watch_rating import DriverRatingFixtureMixin


@override_settings(
    PORTAL_SITE_CODE='rating-assignment-group-tests',
    PORTAL_EMPLOYEE_SCOPE_PROVIDER='',
    DRIVER_RATING_SNAPSHOT_REFRESH_SECONDS=300,
    DRIVER_RATING_SNAPSHOT_SOFT_STALE_SECONDS=600,
    DRIVER_RATING_SNAPSHOT_HARD_EXPIRE_SECONDS=1800,
)
class DriverRatingAssignmentGroupTests(
    DriverRatingFixtureMixin,
    TestCase,
):
    def setUp(self):
        super().setUp()
        self.rating_period = RatingPeriod.objects.create(
            name='Период проверки групп по назначениям',
            starts_on=self.watch.starts_on,
            ends_before=self.watch.ends_on + timedelta(days=1),
            comment='Только автоматическая проверка нового состава.',
        )
        self.schedule_a = WorkSchedule.objects.create(
            code='rating-group-a',
            name='График рейтинга A',
            brigade_count=4,
        )
        self.schedule_b = WorkSchedule.objects.create(
            code='rating-group-b',
            name='График рейтинга B',
            brigade_count=4,
        )
        self._equipment_ordinal = 0

    def _employee(self, name, *, schedule=None, brigade=1, active=True):
        employee = self.employee(name)
        employee.work_schedule = schedule or self.schedule_a
        employee.brigade_number = brigade
        if not active:
            employee.status = Employee.Status.DEACTIVATED
            employee.is_active = False
        employee.save(update_fields=[
            'work_schedule',
            'brigade_number',
            'status',
            'is_active',
        ])
        return employee

    def _equipment(self):
        self._equipment_ordinal += 1
        return Equipment.objects.create(
            equipment_type=self.truck.equipment_type,
            model=self.model,
            garage_number=f'RATING-GROUP-{self._equipment_ordinal:03d}',
        )

    def _assignment(
        self,
        employee,
        *,
        shift_type=ShiftType.DAY,
        status=AssignmentStatus.ACCEPTED,
        ended=False,
        equipment=None,
    ):
        return EquipmentAssignment.objects.create(
            employee=employee,
            role=self.driver_role,
            equipment=equipment or self._equipment(),
            shift_type=shift_type,
            status=status,
            accepted_at=(
                self.now
                if status == AssignmentStatus.ACCEPTED
                else None
            ),
            ended_at=self.now if ended else None,
        )

    def _scope(
        self,
        *,
        schedule=None,
        brigade=1,
        shift_type=ShiftType.DAY,
    ):
        schedule = schedule or self.schedule_a
        return discover_driver_rating_assignment_group_scope(
            self.rating_period,
            work_schedule_id=schedule.id,
            brigade_number=brigade,
            shift_type=shift_type,
        )

    def _refresh(
        self,
        *,
        schedule=None,
        brigade=1,
        shift_type=ShiftType.DAY,
    ):
        return refresh_driver_rating_assignment_group(
            self.rating_period,
            schedule or self.schedule_a,
            brigade_number=brigade,
            shift_type=shift_type,
        )

    def _read(
        self,
        *,
        schedule=None,
        brigade=1,
        shift_type=ShiftType.DAY,
    ):
        return get_materialized_driver_rating_assignment_group(
            self.rating_period,
            schedule or self.schedule_a,
            brigade_number=brigade,
            shift_type=shift_type,
        )

    def test_groups_are_partitioned_by_schedule_brigade_and_shift(self):
        target = self._employee('Целевой водитель')
        other_schedule = self._employee(
            'Другой график',
            schedule=self.schedule_b,
        )
        other_brigade = self._employee('Другая бригада', brigade=2)
        other_shift = self._employee('Другая смена')
        unassigned = self._employee('Без назначения')
        self._assignment(target)
        self._assignment(other_schedule)
        self._assignment(other_brigade)
        self._assignment(other_shift, shift_type=ShiftType.NIGHT)

        scope = self._scope()
        groups = discover_driver_rating_assignment_groups()

        self.assertEqual(scope.allowed_employee_ids, (target.id,))
        self.assertEqual(scope.expected_employee_ids, (target.id,))
        self.assertNotIn(unassigned.id, scope.allowed_employee_ids)
        self.assertEqual(
            {
                (
                    group.work_schedule_id,
                    group.brigade_number,
                    group.shift_type,
                )
                for group in groups
            },
            {
                (self.schedule_a.id, 1, ShiftType.DAY),
                (self.schedule_a.id, 1, ShiftType.NIGHT),
                (self.schedule_a.id, 2, ShiftType.DAY),
                (self.schedule_b.id, 1, ShiftType.DAY),
            },
        )

    def test_only_active_driver_with_current_accepted_assignment_is_member(self):
        current = self._employee('Действующее назначение')
        ended = self._employee('Завершённое назначение')
        pending = self._employee('Непринятое назначение')
        inactive = self._employee('Неактивный водитель', active=False)
        self._assignment(current)
        self._assignment(ended, ended=True)
        self._assignment(pending, status=AssignmentStatus.PENDING)
        self._assignment(inactive)

        scope = self._scope()

        self.assertEqual(scope.allowed_employee_ids, (current.id,))
        self.assertEqual(
            [participant.equipment_id for participant in scope.participants],
            [
                EquipmentAssignment.objects.get(
                    employee=current,
                ).equipment_id
            ],
        )

    def test_driver_without_closed_shift_is_published_as_not_observed(self):
        rated = self._employee('Водитель с результатом')
        not_observed = self._employee('Водитель без закрытой смены')
        self._assignment(rated)
        not_observed_assignment = self._assignment(not_observed)
        self.snapshot(rated, ordinal=1, trip_count=20)

        result = self._refresh()
        payload = self._read()
        rows = {
            row['employee_id']: row
            for row in payload['entries']
        }

        self.assertEqual(result.status, 'published')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[rated.id]['row_status'], 'rated')
        self.assertIsNotNone(rows[rated.id]['score'])
        self.assertIsNotNone(rows[rated.id]['place'])
        self.assertEqual(
            rows[not_observed.id]['row_status'],
            'not_observed',
        )
        self.assertEqual(
            rows[not_observed.id]['status_label'],
            'Нет результата',
        )
        self.assertIsNone(rows[not_observed.id]['score'])
        self.assertIsNone(rows[not_observed.id]['place'])
        self.assertEqual(
            rows[not_observed.id]['equipment_id'],
            not_observed_assignment.equipment_id,
        )
        snapshot = DriverRatingPeriodMaterializedSnapshot.objects.get(
            pk=result.snapshot_id,
        )
        self.assertIsNone(snapshot.watch_composition_id)
        self.assertEqual(snapshot.work_schedule_id, self.schedule_a.id)
        self.assertEqual(snapshot.brigade_number, 1)

    def test_rated_kpi_blocks_match_legacy_formula_for_same_passport(self):
        driver = self._employee('Водитель проверки неизменности KPI')
        assignment = self._assignment(driver)
        self.snapshot(driver, ordinal=1, trip_count=20)
        scope = self._scope()

        legacy = build_driver_rating_period(
            self.rating_period,
            self.composition,
            shift_type=ShiftType.DAY,
            allowed_employee_ids=(driver.id,),
            expected_employee_ids=(driver.id,),
        )
        assignment_group = build_driver_rating_assignment_group_period(
            self.rating_period,
            self.schedule_a,
            brigade_number=1,
            shift_type=ShiftType.DAY,
            participants=scope.participants,
        )

        self.assertEqual(
            scope.participants[0].equipment_id,
            assignment.equipment_id,
        )
        for field in ('score', 'blocks', 'confidence', 'place'):
            self.assertEqual(
                assignment_group['entries'][0][field],
                legacy['entries'][0][field],
            )
        self.assertEqual(
            assignment_group['shift_score_fingerprint'],
            legacy['shift_score_fingerprint'],
        )

    def test_published_group_is_frozen_after_card_and_assignment_change(self):
        frozen = self._employee('Зафиксированный водитель')
        colleague = self._employee('Неизменный водитель')
        frozen_assignment = self._assignment(frozen)
        self._assignment(colleague)
        self.snapshot(frozen, ordinal=1, trip_count=20)
        self.snapshot(colleague, ordinal=2, trip_count=18)
        first = self._refresh()
        before = DriverRatingPeriodMaterializedSnapshot.objects.get(
            pk=first.snapshot_id,
        )
        before_payload = before.payload
        before_participants = before.participant_group_snapshots

        frozen.work_schedule = self.schedule_b
        frozen.brigade_number = 3
        frozen.save(update_fields=['work_schedule', 'brigade_number'])
        frozen_assignment.equipment = self._equipment()
        frozen_assignment.shift_type = ShiftType.NIGHT
        frozen_assignment.save(update_fields=['equipment', 'shift_type'])

        second = self._refresh()
        after = DriverRatingPeriodMaterializedSnapshot.objects.get(
            pk=first.snapshot_id,
        )

        self.assertEqual(second.status, 'verified')
        self.assertFalse(second.changed)
        self.assertEqual(second.revision, first.revision)
        self.assertEqual(after.payload, before_payload)
        self.assertEqual(
            after.participant_group_snapshots,
            before_participants,
        )
        self.assertEqual(
            {row['employee_id'] for row in after.payload['entries']},
            {frozen.id, colleague.id},
        )
        with self.assertRaises(DriverRatingMaterializationError):
            self._refresh(
                schedule=self.schedule_b,
                brigade=3,
                shift_type=ShiftType.NIGHT,
            )
        published_rows = (
            DriverRatingPeriodMaterializedSnapshot.objects
            .filter(
                rating_period=self.rating_period,
                revision__gt=0,
                participant_group_snapshots__isnull=False,
            )
        )
        self.assertEqual(published_rows.count(), 1)

    def test_tampered_frozen_participant_context_is_rejected(self):
        driver = self._employee('Водитель проверки frozen fingerprint')
        self._assignment(driver)
        result = self._refresh()
        snapshot = DriverRatingPeriodMaterializedSnapshot.objects.get(
            pk=result.snapshot_id,
        )
        tampered = list(snapshot.participant_group_snapshots)
        tampered[0] = {
            **tampered[0],
            'equipment_id': self._equipment().id,
        }
        DriverRatingPeriodMaterializedSnapshot.objects.filter(
            pk=snapshot.pk,
        ).update(participant_group_snapshots=tampered)

        with self.assertRaises(DriverRatingSnapshotUnavailable) as error:
            self._read()

        self.assertEqual(error.exception.code, 'snapshot_scope_mismatch')
        with self.assertRaises(DriverRatingMaterializationError):
            self._refresh()
        with self.assertRaises(DriverRatingSnapshotUnavailable):
            self._read()

    def test_invalid_observed_shift_is_withheld_not_marked_no_result(self):
        driver = self._employee('Водитель удержанной смены')
        outsider = self._employee(
            'Технически перепривязанный сотрудник',
            schedule=self.schedule_b,
        )
        self._assignment(driver)
        passport = self.snapshot(driver, ordinal=1, trip_count=20)
        type(passport.shift).objects.filter(pk=passport.shift_id).update(
            employee=outsider,
        )

        self._refresh()
        payload = self._read()
        row = payload['entries'][0]

        self.assertEqual(row['employee_id'], driver.id)
        self.assertEqual(row['row_status'], 'withheld')
        self.assertEqual(row['status_label'], 'Результат удержан')
        self.assertIsNone(row['score'])
        self.assertEqual(payload['summary']['withheld_employee_count'], 1)
        self.assertEqual(payload['summary']['not_observed_employee_count'], 0)

    def test_database_rejects_hybrid_materialized_group_key(self):
        driver = self._employee('Водитель проверки ключа группы')
        self._assignment(driver)
        result = self._refresh()

        with self.assertRaises(IntegrityError), transaction.atomic():
            DriverRatingPeriodMaterializedSnapshot.objects.filter(
                pk=result.snapshot_id,
            ).update(watch_composition=self.composition)

    def test_watch_composition_option_requires_explicit_legacy_gate(self):
        with self.assertRaisesMessage(
            CommandError,
            '--legacy-watch-groups',
        ):
            call_command(
                'refresh_driver_rating_snapshots',
                rating_period=self.rating_period.id,
                watch_composition=self.composition.id,
                strict=True,
                verbosity=0,
            )

    def test_groups_of_two_and_forty_one_have_no_padding_rows(self):
        expected_sizes = {
            (self.schedule_a.id, 1, ShiftType.DAY): 2,
            (self.schedule_b.id, 3, ShiftType.NIGHT): 41,
        }
        for index in range(2):
            employee = self._employee(f'Малая группа {index + 1}')
            self._assignment(employee)
        for index in range(41):
            employee = self._employee(
                f'Большая группа {index + 1}',
                schedule=self.schedule_b,
                brigade=3,
            )
            self._assignment(employee, shift_type=ShiftType.NIGHT)

        for (schedule_id, brigade, shift_type), size in expected_sizes.items():
            schedule = WorkSchedule.objects.get(pk=schedule_id)
            self._refresh(
                schedule=schedule,
                brigade=brigade,
                shift_type=shift_type,
            )
            payload = self._read(
                schedule=schedule,
                brigade=brigade,
                shift_type=shift_type,
            )
            self.assertEqual(len(payload['entries']), size)
            self.assertEqual(payload['summary']['employee_count'], size)
            self.assertEqual(
                {row['row_status'] for row in payload['entries']},
                {'not_observed'},
            )
            self.assertEqual(
                {row['display_order'] for row in payload['entries']},
                set(range(1, size + 1)),
            )

    def test_new_materialization_does_not_read_watch_membership(self):
        driver = self._employee('Водитель без вахтового состава')
        driver.watch_composition = None
        driver.save(update_fields=['watch_composition'])
        self._assignment(driver)
        passport = self.snapshot(driver, ordinal=1, trip_count=20)
        type(passport.shift).objects.filter(pk=passport.shift_id).update(
            watch_period=None,
        )
        passport.shift.refresh_from_db()

        with patch(
            'reports.driver_rating_materialization.'
            'discover_driver_rating_group_scope',
            side_effect=AssertionError('legacy watch scope was read'),
        ), patch(
            'reports.driver_rating_materialization.build_driver_rating_period',
            side_effect=AssertionError('legacy watch formula was read'),
        ):
            result = self._refresh()

        payload = self._read()
        self.assertEqual(result.status, 'published')
        self.assertEqual(
            [row['employee_id'] for row in payload['entries']],
            [driver.id],
        )
        self.assertNotIn('watch_composition', payload)
        self.assertFalse(payload['linkage_audit']['watch_linkage_required'])

    def test_refresh_command_creates_only_existing_assignment_groups(self):
        day_driver = self._employee('Командная дневная группа')
        night_driver = self._employee('Командная ночная группа', brigade=2)
        self._assignment(day_driver)
        self._assignment(night_driver, shift_type=ShiftType.NIGHT)
        output = io.StringIO()

        call_command(
            'refresh_driver_rating_snapshots',
            rating_period=self.rating_period.id,
            strict=True,
            stdout=output,
            no_color=True,
            verbosity=0,
        )

        rows = DriverRatingPeriodMaterializedSnapshot.objects.filter(
            rating_period=self.rating_period,
            watch_composition__isnull=True,
        )
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            set(rows.values_list('brigade_number', 'shift_type')),
            {(1, ShiftType.DAY), (2, ShiftType.NIGHT)},
        )
        self.assertNotIn('watch', output.getvalue().lower())
