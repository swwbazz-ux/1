from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from assignments.models import (
    AssignmentStatus,
    CrewPlan,
    CrewPlanSlot,
    CrewPlanStatus,
    EquipmentAssignment,
    WorkShiftType,
)
from assignments.services import (
    get_or_create_crew_draft,
    publish_crew_plan,
)
from core.production_time import (
    BUSINESS_TIME_ZONE,
    production_day_bounds,
    production_shift_type,
    production_work_date,
)
from references.models import Equipment, EquipmentModel, EquipmentType
from reports.driver_watch_observation import (
    build_driver_watch_linkage_audit,
    build_driver_watch_observation,
)
from users.models import (
    Employee,
    EmployeeAccess,
    Role,
    WatchComposition,
    WorkSchedule,
)

from .admin import EmployeeShiftAdmin, WatchPeriodAdmin
from .models import EmployeeShift, WatchPeriod
from .services import open_driver_shift, resolve_published_watch_period_for_shift


class DriverWatchPeriodLinkageTests(TestCase):
    def setUp(self):
        self.driver_role = Role.objects.create(
            code='driver',
            name='Водитель самосвала',
            is_active=True,
        )
        self.deputy_role, _created = Role.objects.update_or_create(
            code='deputy_mining_manager',
            defaults={
                'name': 'Заместитель начальника горного участка',
                'is_active': True,
            },
        )
        self.deputy = Employee.objects.create(
            full_name='ТЕСТ_ВАХТА_Заместитель начальника участка',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.deputy_access = EmployeeAccess.objects.create(
            employee=self.deputy,
            role=self.deputy_role,
            access_code='290000',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.watch_composition = WatchComposition.objects.create(
            code='test-watch-composition-main',
            name='ТЕСТ_ВАХТА_Утверждённый состав',
        )
        self.driver = Employee.objects.create(
            full_name='ТЕСТ_ВАХТА_Водитель',
            status=Employee.Status.ACTIVE,
            is_active=True,
            watch_composition=self.watch_composition,
        )
        EmployeeAccess.objects.create(
            employee=self.driver,
            role=self.driver_role,
            access_code='290001',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        truck_type = EquipmentType.objects.create(
            name='Самосвал',
            is_active=True,
        )
        truck_model = EquipmentModel.objects.create(
            equipment_type=truck_type,
            name='ТЕСТ_ВАХТА_Самосвал',
            fuel_capacity_limit_l=Decimal('2000'),
            is_active=True,
        )
        self.truck = Equipment.objects.create(
            equipment_type=truck_type,
            model=truck_model,
            garage_number='ТЕСТ_ВАХТА_001',
            is_active=True,
        )
        self.assignment = EquipmentAssignment.objects.create(
            employee=self.driver,
            role=self.driver_role,
            equipment=self.truck,
            shift_type=production_shift_type(),
            assigned_by=self.deputy,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )

    def publish_current_deputy_plan(self):
        plan, _created = get_or_create_crew_draft(
            role=self.driver_role,
            actor=self.deputy,
        )
        publish_crew_plan(
            plan=plan,
            expected_version=plan.version,
            actor=self.deputy,
        )
        plan.refresh_from_db()
        return plan

    def create_current_watch_period(
        self,
        *,
        name='ТЕСТ_ВАХТА_Текущая',
        watch_composition=None,
    ):
        work_date = production_work_date()
        return WatchPeriod.objects.create(
            name=name,
            watch_composition=watch_composition or self.watch_composition,
            starts_on=work_date - timedelta(days=14),
            ends_on=work_date + timedelta(days=14),
            is_active=True,
        )

    def create_published_plan_for(
        self,
        *,
        work_date,
        opened_at,
        shift_type,
        employee=None,
        published_at=None,
    ):
        plan = CrewPlan.objects.create(
            work_date=work_date,
            role=self.driver_role,
            revision=1,
            status=CrewPlanStatus.PUBLISHED,
            created_by=self.deputy,
            updated_by=self.deputy,
            published_by=self.deputy,
            published_at=published_at or opened_at - timedelta(minutes=1),
        )
        CrewPlanSlot.objects.create(
            plan=plan,
            equipment=self.truck,
            shift_type=shift_type,
            employee=employee or self.driver,
        )
        return plan

    def open_shift(self, *, action='watch-open', expected_created=True):
        shift, created = open_driver_shift(
            employee=self.driver,
            work_assignment=self.assignment,
            readings={
                'start_fuel': Decimal('1000'),
                'start_mileage': Decimal('10000'),
                'start_engine_hours': Decimal('1000'),
            },
            client_action_id=action,
        )
        self.assertEqual(created, expected_created)
        return shift

    def test_published_deputy_placement_links_shift_to_unambiguous_watch(self):
        watch_period = self.create_current_watch_period()
        plan = self.publish_current_deputy_plan()

        shift = self.open_shift()

        self.assertEqual(plan.published_by, self.deputy)
        self.assertEqual(shift.shift_type, self.assignment.shift_type)
        self.assertEqual(shift.equipment, self.truck)
        self.assertEqual(shift.watch_period, watch_period)

    def test_assignment_without_published_deputy_plan_does_not_guess_watch(self):
        self.create_current_watch_period()

        shift = self.open_shift()

        self.assertIsNone(shift.watch_period)

    def test_employee_without_approved_watch_composition_is_unlinked(self):
        self.create_current_watch_period()
        self.publish_current_deputy_plan()
        self.driver.watch_composition = None
        self.driver.save(update_fields=['watch_composition'])

        shift = self.open_shift(action='employee-without-watch-composition')

        self.assertIsNone(shift.watch_period)

    def test_period_for_another_approved_watch_composition_is_unlinked(self):
        other_composition = WatchComposition.objects.create(
            code='test-watch-composition-other',
            name='ТЕСТ_ВАХТА_Другой утверждённый состав',
        )
        self.create_current_watch_period(
            watch_composition=other_composition,
        )
        self.publish_current_deputy_plan()

        shift = self.open_shift(action='another-watch-composition')

        self.assertIsNone(shift.watch_period)

    def test_other_composition_period_does_not_make_membership_ambiguous(self):
        selected_period = self.create_current_watch_period()
        other_composition = WatchComposition.objects.create(
            code='test-watch-composition-parallel',
            name='ТЕСТ_ВАХТА_Параллельный утверждённый состав',
        )
        self.create_current_watch_period(
            name='ТЕСТ_ВАХТА_Параллельная',
            watch_composition=other_composition,
        )
        self.publish_current_deputy_plan()

        shift = self.open_shift(action='parallel-watch-composition')

        self.assertEqual(shift.watch_period, selected_period)

    def test_inactive_employee_watch_composition_is_unlinked(self):
        self.create_current_watch_period()
        self.publish_current_deputy_plan()
        self.watch_composition.is_active = False
        self.watch_composition.save(update_fields=['is_active'])

        shift = self.open_shift(action='inactive-watch-composition')

        self.assertIsNone(shift.watch_period)

    def test_legacy_period_without_structural_composition_is_unlinked(self):
        work_date = production_work_date()
        WatchPeriod.objects.create(
            name='ТЕСТ_ВАХТА_Legacy без состава',
            watch_composition=None,
            starts_on=work_date - timedelta(days=14),
            ends_on=work_date + timedelta(days=14),
            is_active=True,
        )
        self.publish_current_deputy_plan()

        shift = self.open_shift(action='legacy-watch-without-composition')

        self.assertIsNone(shift.watch_period)

    def test_schedule_brigade_and_legacy_rotation_do_not_choose_watch(self):
        selected_period = self.create_current_watch_period()
        other_composition = WatchComposition.objects.create(
            code='test-watch-composition-misleading',
            name='ТЕСТ_ВАХТА_Состав из legacy-подписи',
        )
        self.create_current_watch_period(
            name='ТЕСТ_ВАХТА_Параллельная по legacy-подписи',
            watch_composition=other_composition,
        )
        schedule = WorkSchedule.objects.create(
            code='test-watch-misleading-schedule',
            name='ТЕСТ_ВАХТА_График не является составом',
            brigade_count=4,
        )
        self.driver.work_schedule = schedule
        self.driver.brigade_number = 4
        self.driver.rotation = other_composition.name
        self.driver.save(update_fields=[
            'work_schedule',
            'brigade_number',
            'rotation',
        ])
        self.publish_current_deputy_plan()

        shift = self.open_shift(action='schedule-brigade-independent')

        self.assertEqual(shift.watch_period, selected_period)

    def test_overlapping_watch_periods_do_not_block_shift_or_guess_watch(self):
        self.create_current_watch_period(name='ТЕСТ_ВАХТА_Первая')
        self.create_current_watch_period(name='ТЕСТ_ВАХТА_Вторая')
        self.publish_current_deputy_plan()

        shift = self.open_shift()

        self.assertIsNone(shift.watch_period)
        self.assertTrue(
            EmployeeShift.objects.filter(pk=shift.pk, closed_at__isnull=True).exists()
        )

    def test_inactive_watch_period_is_not_linked(self):
        watch_period = self.create_current_watch_period()
        watch_period.is_active = False
        watch_period.save(update_fields=['is_active'])
        self.publish_current_deputy_plan()

        shift = self.open_shift()

        self.assertIsNone(shift.watch_period)

    def test_linked_closed_shift_is_visible_in_read_only_watch_observation(self):
        watch_period = self.create_current_watch_period()
        self.publish_current_deputy_plan()
        shift = self.open_shift()
        work_date = production_work_date(shift.opened_at)
        production_start, _production_end = production_day_bounds(work_date)
        opened_at = production_start + timedelta(hours=1)
        closed_at = opened_at + timedelta(hours=11)
        EmployeeShift.objects.filter(pk=shift.pk).update(
            opened_at=opened_at,
            closed_at=closed_at,
            closed_by=self.driver,
            end_fuel=Decimal('900'),
            end_mileage=Decimal('10100'),
            end_engine_hours=Decimal('1010'),
        )

        observation = build_driver_watch_observation(
            watch_period,
            as_of=closed_at + timedelta(minutes=1),
        )
        linkage = build_driver_watch_linkage_audit(watch_period)

        self.assertFalse(observation['official_rating_eligible'])
        self.assertEqual(observation['summary']['closed_shift_count'], 1)
        self.assertEqual(observation['row_count'], 1)
        self.assertEqual(linkage['candidate_closed_shift_count'], 1)
        self.assertEqual(linkage['linked_to_selected_watch_count'], 1)
        self.assertEqual(linkage['unlinked_shift_count'], 0)

    def test_early_day_shift_without_structural_early_complex_marker_is_unlinked(self):
        opened_at = datetime(2026, 8, 10, 6, 0, tzinfo=BUSINESS_TIME_ZONE)
        work_date = production_work_date(opened_at)
        self.assignment.shift_type = WorkShiftType.SHIFT_1
        self.assignment.save(update_fields=['shift_type'])
        WatchPeriod.objects.create(
            name='ТЕСТ_ВАХТА_Ранний комплекс внутри периода',
            watch_composition=self.watch_composition,
            starts_on=work_date - timedelta(days=2),
            ends_on=opened_at.date() + timedelta(days=2),
            is_active=True,
        )
        self.create_published_plan_for(
            work_date=work_date,
            opened_at=opened_at,
            shift_type=WorkShiftType.SHIFT_1,
        )

        with patch('shifts.services.timezone.now', return_value=opened_at):
            shift = self.open_shift(action='early-day-inside')

        self.assertIsNone(shift.watch_period)
        self.assertEqual(shift.opened_at, opened_at)

    def test_early_day_shift_on_watch_boundary_is_left_unlinked(self):
        opened_at = datetime(2026, 8, 10, 6, 0, tzinfo=BUSINESS_TIME_ZONE)
        previous_work_date = production_work_date(opened_at)
        self.assignment.shift_type = WorkShiftType.SHIFT_1
        self.assignment.save(update_fields=['shift_type'])
        WatchPeriod.objects.create(
            name='ТЕСТ_ВАХТА_Предыдущая',
            watch_composition=self.watch_composition,
            starts_on=previous_work_date - timedelta(days=14),
            ends_on=previous_work_date,
            is_active=True,
        )
        WatchPeriod.objects.create(
            name='ТЕСТ_ВАХТА_Следующая',
            watch_composition=self.watch_composition,
            starts_on=opened_at.date(),
            ends_on=opened_at.date() + timedelta(days=14),
            is_active=True,
        )
        self.create_published_plan_for(
            work_date=previous_work_date,
            opened_at=opened_at,
            shift_type=WorkShiftType.SHIFT_1,
        )

        with patch('shifts.services.timezone.now', return_value=opened_at):
            shift = self.open_shift(action='early-day-boundary')

        self.assertIsNone(shift.watch_period)
        self.assertTrue(
            EmployeeShift.objects.filter(pk=shift.pk, closed_at__isnull=True).exists()
        )

    def test_night_replacement_after_midnight_uses_previous_production_date(self):
        opened_at = datetime(2026, 8, 10, 1, 0, tzinfo=BUSINESS_TIME_ZONE)
        work_date = production_work_date(opened_at)
        watch_period = WatchPeriod.objects.create(
            name='ТЕСТ_ВАХТА_Ночная после полуночи',
            watch_composition=self.watch_composition,
            starts_on=work_date - timedelta(days=14),
            ends_on=work_date,
            is_active=True,
        )
        self.assignment.shift_type = WorkShiftType.SHIFT_2
        self.assignment.save(update_fields=['shift_type'])
        self.create_published_plan_for(
            work_date=work_date,
            opened_at=opened_at,
            shift_type=WorkShiftType.SHIFT_2,
        )

        with patch('shifts.services.timezone.now', return_value=opened_at):
            shift = self.open_shift(action='night-after-midnight')

        self.assertEqual(shift.shift_type, WorkShiftType.SHIFT_2)
        self.assertEqual(shift.watch_period, watch_period)

    def test_repeated_client_action_preserves_original_watch_snapshot(self):
        watch_period = self.create_current_watch_period()
        self.publish_current_deputy_plan()
        first_shift = self.open_shift(action='same-watch-action')
        watch_period.is_active = False
        watch_period.save(update_fields=['is_active'])

        repeated_shift = self.open_shift(
            action='same-watch-action',
            expected_created=False,
        )

        self.assertEqual(repeated_shift.pk, first_shift.pk)
        self.assertEqual(repeated_shift.watch_period_id, watch_period.pk)

    def test_employee_membership_change_does_not_rewrite_open_shift_snapshot(self):
        watch_period = self.create_current_watch_period()
        self.publish_current_deputy_plan()
        shift = self.open_shift(action='membership-snapshot')
        new_composition = WatchComposition.objects.create(
            code='test-watch-composition-transfer',
            name='ТЕСТ_ВАХТА_Новый утверждённый состав',
        )

        self.driver.watch_composition = new_composition
        self.driver.save(update_fields=['watch_composition'])
        shift.refresh_from_db()

        self.assertEqual(shift.watch_period_id, watch_period.pk)

    def test_publisher_without_deputy_access_does_not_prove_placement(self):
        self.create_current_watch_period()
        self.deputy_access.delete()
        self.publish_current_deputy_plan()

        shift = self.open_shift(action='publisher-without-deputy-access')

        self.assertIsNone(shift.watch_period)

    def test_inactive_deputy_access_states_do_not_prove_placement(self):
        self.create_current_watch_period()
        self.publish_current_deputy_plan()
        opened_at = timezone.now()

        cases = (
            ('not-activated', {'status': EmployeeAccess.Status.NOT_ACTIVATED}),
            ('blocked', {'status': EmployeeAccess.Status.BLOCKED}),
            ('deactivated', {'status': EmployeeAccess.Status.DEACTIVATED}),
            ('inactive-access', {'is_active': False}),
            ('inactive-role', {'role_is_active': False}),
            ('inactive-employee', {'employee_is_active': False}),
        )
        for label, changes in cases:
            with self.subTest(label=label):
                self.deputy_access.status = changes.get(
                    'status',
                    EmployeeAccess.Status.ACTIVATED,
                )
                self.deputy_access.is_active = changes.get('is_active', True)
                self.deputy_access.save(update_fields=['status', 'is_active'])
                self.deputy_role.is_active = changes.get('role_is_active', True)
                self.deputy_role.save(update_fields=['is_active'])
                self.deputy.is_active = changes.get('employee_is_active', True)
                self.deputy.save(update_fields=['is_active'])

                watch_period = resolve_published_watch_period_for_shift(
                    employee=self.driver,
                    equipment=self.truck,
                    shift_type=self.assignment.shift_type,
                    role_code='driver',
                    opened_at=opened_at,
                )

                self.assertIsNone(watch_period)

        self.deputy_access.status = EmployeeAccess.Status.ACTIVATED
        self.deputy_access.is_active = True
        self.deputy_access.save(update_fields=['status', 'is_active'])
        self.deputy_role.is_active = True
        self.deputy_role.save(update_fields=['is_active'])
        self.deputy.is_active = True
        self.deputy.save(update_fields=['is_active'])

    def test_plan_published_after_shift_opening_does_not_link_retroactively(self):
        opened_at = datetime(2026, 8, 10, 10, 0, tzinfo=BUSINESS_TIME_ZONE)
        work_date = production_work_date(opened_at)
        WatchPeriod.objects.create(
            name='ТЕСТ_ВАХТА_Поздняя публикация',
            watch_composition=self.watch_composition,
            starts_on=work_date - timedelta(days=14),
            ends_on=work_date + timedelta(days=14),
            is_active=True,
        )
        self.assignment.shift_type = WorkShiftType.SHIFT_1
        self.assignment.save(update_fields=['shift_type'])
        self.create_published_plan_for(
            work_date=work_date,
            opened_at=opened_at,
            shift_type=WorkShiftType.SHIFT_1,
            published_at=opened_at + timedelta(minutes=1),
        )

        with patch('shifts.services.timezone.now', return_value=opened_at):
            shift = self.open_shift(action='published-after-open')

        self.assertIsNone(shift.watch_period)

    def test_published_slot_for_another_employee_does_not_link(self):
        opened_at = datetime(2026, 8, 10, 10, 0, tzinfo=BUSINESS_TIME_ZONE)
        work_date = production_work_date(opened_at)
        WatchPeriod.objects.create(
            name='ТЕСТ_ВАХТА_Другой сотрудник',
            watch_composition=self.watch_composition,
            starts_on=work_date - timedelta(days=14),
            ends_on=work_date + timedelta(days=14),
            is_active=True,
        )
        other_driver = Employee.objects.create(
            full_name='ТЕСТ_ВАХТА_Другой водитель',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=other_driver,
            role=self.driver_role,
            access_code='290002',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.assignment.shift_type = WorkShiftType.SHIFT_1
        self.assignment.save(update_fields=['shift_type'])
        self.create_published_plan_for(
            work_date=work_date,
            opened_at=opened_at,
            shift_type=WorkShiftType.SHIFT_1,
            employee=other_driver,
        )

        with patch('shifts.services.timezone.now', return_value=opened_at):
            shift = self.open_shift(action='different-plan-employee')

        self.assertIsNone(shift.watch_period)

    def test_existing_watch_period_structural_fields_are_immutable(self):
        watch_period = self.create_current_watch_period()
        self.publish_current_deputy_plan()
        shift = self.open_shift(action='immutable-watch-period')
        replacement_composition = WatchComposition.objects.create(
            code='test-watch-composition-immutable-replacement',
            name='ТЕСТ_ВАХТА_Замена неизменяемого состава',
        )
        original = {
            'name': watch_period.name,
            'watch_composition_id': watch_period.watch_composition_id,
            'starts_on': watch_period.starts_on,
            'ends_on': watch_period.ends_on,
        }
        changes = {
            'name': f'{watch_period.name} изменена',
            'watch_composition_id': replacement_composition.pk,
            'starts_on': watch_period.starts_on - timedelta(days=1),
            'ends_on': watch_period.ends_on + timedelta(days=1),
        }

        for field, value in changes.items():
            with self.subTest(field=field):
                watch_period.refresh_from_db()
                setattr(watch_period, field, value)
                with self.assertRaises(ValidationError):
                    watch_period.save()
                watch_period.refresh_from_db()
                self.assertEqual(getattr(watch_period, field), original[field])

        shift.refresh_from_db()
        self.assertEqual(shift.watch_period_id, watch_period.pk)
        self.assertEqual(
            shift.watch_period.watch_composition_id,
            self.watch_composition.pk,
        )

    def test_existing_watch_period_can_be_deactivated(self):
        watch_period = self.create_current_watch_period()

        watch_period.is_active = False
        watch_period.save(update_fields=['is_active'])
        watch_period.refresh_from_db()

        self.assertFalse(watch_period.is_active)

    def test_existing_watch_period_structural_fields_are_readonly_in_admin(self):
        model_admin = WatchPeriodAdmin(WatchPeriod, admin.site)
        watch_period = self.create_current_watch_period()

        self.assertEqual(
            model_admin.get_readonly_fields(None, watch_period),
            ('name', 'watch_composition', 'starts_on', 'ends_on'),
        )
        self.assertEqual(model_admin.get_readonly_fields(None, None), ())

    def test_employee_shift_watch_period_snapshot_reassignment_is_rejected(self):
        watch_period = self.create_current_watch_period()
        self.publish_current_deputy_plan()
        shift = self.open_shift(action='immutable-shift-watch-snapshot')
        replacement = WatchPeriod.objects.create(
            name='ТЕСТ_ВАХТА_Другой календарный период',
            watch_composition=self.watch_composition,
            starts_on=watch_period.starts_on,
            ends_on=watch_period.ends_on,
        )

        for replacement_id in (replacement.pk, None):
            with self.subTest(replacement_id=replacement_id):
                shift.refresh_from_db()
                shift.watch_period_id = replacement_id
                with self.assertRaises(ValidationError):
                    shift.save(update_fields=['watch_period'])
                shift.refresh_from_db()
                self.assertEqual(shift.watch_period_id, watch_period.pk)

    def test_employee_shift_null_watch_snapshot_cannot_be_backfilled_by_save(self):
        watch_period = self.create_current_watch_period()
        shift = self.open_shift(action='immutable-null-watch-snapshot')
        self.assertIsNone(shift.watch_period_id)

        shift.watch_period = watch_period
        with self.assertRaises(ValidationError):
            shift.save(update_fields=['watch_period'])
        shift.refresh_from_db()

        self.assertIsNone(shift.watch_period_id)

    def test_employee_shift_watch_snapshot_does_not_block_normal_close(self):
        watch_period = self.create_current_watch_period()
        self.publish_current_deputy_plan()
        shift = self.open_shift(action='close-with-immutable-watch-snapshot')

        shift.closed_at = timezone.now()
        shift.closed_by = self.driver
        shift.save(update_fields=['closed_at', 'closed_by'])
        shift.refresh_from_db()

        self.assertIsNotNone(shift.closed_at)
        self.assertEqual(shift.closed_by_id, self.driver.pk)
        self.assertEqual(shift.watch_period_id, watch_period.pk)

    def test_employee_shift_watch_snapshot_is_readonly_in_admin(self):
        model_admin = EmployeeShiftAdmin(EmployeeShift, admin.site)
        watch_period = self.create_current_watch_period()
        shift = EmployeeShift.objects.create(
            employee=self.driver,
            shift_type=WorkShiftType.SHIFT_1,
            workplace_code='driver',
            equipment=self.truck,
            watch_period=watch_period,
            opened_at=timezone.now(),
        )

        self.assertIn(
            'watch_period',
            model_admin.get_readonly_fields(None, shift),
        )
        self.assertNotIn(
            'watch_period',
            model_admin.get_readonly_fields(None, None),
        )
