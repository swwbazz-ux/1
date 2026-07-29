import json
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
from core.production_time import production_day_bounds, production_work_date
from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import (
    DumpPoint,
    Equipment,
    EquipmentModel,
    EquipmentType,
    RockType,
)
from shifts.models import EmployeeShift, ShiftType, WatchPeriod
from trips.models import Trip, TripStatus
from users.active_role import (
    ACTIVE_ROLE_CODE_SESSION_KEY,
    ACTIVE_ROLE_GENERATION_SESSION_KEY,
    ACTIVE_ROLE_SESSION_KEY,
)
from users.models import Employee, EmployeeAccess, Role, WatchComposition

from .driver_shift_timeline import (
    TimelineCategory,
    build_driver_shift_timeline,
)
from .driver_watch_observation import (
    _aggregate_shift_passports,
    build_driver_period_shadow_observation,
    build_driver_watch_linkage_audit,
    build_driver_watch_observation,
)


class DriverWatchObservationTests(TestCase):
    def setUp(self):
        self.end = timezone.now() - timedelta(hours=1)
        self.start = self.end - timedelta(hours=12)
        self.watch = WatchPeriod.objects.create(
            name='Тестовая вахта рейтинга',
            starts_on=timezone.localdate() - timedelta(days=15),
            ends_on=timezone.localdate() + timedelta(days=15),
        )
        truck_type = EquipmentType.objects.create(name='Самосвал watch rating')
        truck_model = EquipmentModel.objects.create(
            equipment_type=truck_type,
            name='БелАЗ watch rating',
        )
        self.day_truck = Equipment.objects.create(
            equipment_type=truck_type,
            model=truck_model,
            garage_number='WR-DAY',
        )
        self.night_truck = Equipment.objects.create(
            equipment_type=truck_type,
            model=truck_model,
            garage_number='WR-NIGHT',
        )
        self.driver = Employee.objects.create(
            full_name='Водитель сводки вахты',
            personnel_number='WR-001',
            work_category=Employee.WorkCategory.DRIVER,
            status=Employee.Status.ACTIVE,
        )

    def create_shift(self, shift_type, equipment):
        return EmployeeShift.objects.create(
            employee=self.driver,
            shift_type=shift_type,
            workplace_code='driver',
            watch_period=self.watch,
            equipment=equipment,
            opened_at=self.start,
            closed_at=self.end,
        )

    def test_keeps_day_and_night_observations_in_separate_rows(self):
        day_shift = self.create_shift(ShiftType.DAY, self.day_truck)
        night_shift = self.create_shift(ShiftType.NIGHT, self.night_truck)
        night_shift.opened_at = self.start - timedelta(hours=12)
        night_shift.closed_at = self.start
        night_shift.save(update_fields=['opened_at', 'closed_at'])
        waiting = DowntimeReason.objects.get(name='Ожидание погрузки')
        for shift in (day_shift, night_shift):
            DowntimeEvent.objects.create(
                equipment=shift.equipment,
                employee=self.driver,
                reason=waiting,
                started_at=shift.opened_at,
                ended_at=shift.closed_at,
            )

        result = build_driver_watch_observation(
            self.watch,
            as_of=self.end,
        )

        self.assertEqual(result['row_count'], 2)
        self.assertEqual(result['summary']['usable_shift_count'], 0)
        self.assertEqual(result['summary']['withheld_shift_count'], 2)
        self.assertFalse(
            result['summary']['data_ready_for_formula_review']
        )
        self.assertEqual(
            {row['shift_type'] for row in result['rows']},
            {ShiftType.DAY, ShiftType.NIGHT},
        )
        for row in result['rows']:
            self.assertEqual(row['shift_count'], 1)
            self.assertEqual(row['coverage_percent'], 100.0)
            self.assertEqual(row['observation_status'], 'observed')
            self.assertNotIn('personnel_number', row)
            self.assertEqual(
                row['seconds_by_category'][
                    TimelineCategory.DOWNTIME_EXTERNAL
                ],
                12 * 3600,
            )

    def test_filters_requested_shift_type(self):
        self.create_shift(ShiftType.DAY, self.day_truck)
        self.create_shift(ShiftType.NIGHT, self.night_truck)

        result = build_driver_watch_observation(
            self.watch,
            shift_type=ShiftType.NIGHT,
            as_of=self.end,
        )

        self.assertEqual(result['row_count'], 1)
        self.assertEqual(result['rows'][0]['shift_type'], ShiftType.NIGHT)

    def test_excludes_open_shift_from_official_shadow_observation(self):
        shift = self.create_shift(ShiftType.DAY, self.day_truck)
        shift.closed_at = None
        shift.save(update_fields=['closed_at'])

        result = build_driver_watch_observation(
            self.watch,
            as_of=self.end,
        )

        self.assertEqual(result['row_count'], 0)
        self.assertEqual(result['summary']['closed_shift_count'], 0)
        self.assertFalse(
            result['summary']['data_ready_for_formula_review']
        )

    def test_loads_any_number_of_shift_passports_in_six_queries(self):
        first_shift = self.create_shift(ShiftType.DAY, self.day_truck)
        second_driver = Employee.objects.create(
            full_name='Второй водитель сводки',
            work_category=Employee.WorkCategory.DRIVER,
        )
        second_shift = EmployeeShift.objects.create(
            employee=second_driver,
            shift_type=ShiftType.DAY,
            workplace_code='driver',
            watch_period=self.watch,
            equipment=self.night_truck,
            opened_at=self.start,
            closed_at=self.end,
        )
        excavator_type = EquipmentType.objects.create(
            name='Экскаватор query budget',
        )
        excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='WR-EXC',
        )
        rock = RockType.objects.create(name='Порода query budget')
        dump_point = DumpPoint.objects.create(name='Разгрузка query budget')
        for index, shift in enumerate((first_shift, second_shift), start=1):
            trip = Trip.objects.create(
                excavator=excavator,
                truck=shift.equipment,
                driver=shift.employee,
                unloading_shift=shift,
                rock_type=rock,
                dump_point=dump_point,
                status=TripStatus.COMPLETED,
                completed_at=self.start + timedelta(hours=index + 1),
            )
            Trip.objects.filter(pk=trip.pk).update(
                created_at=self.start + timedelta(hours=index),
            )

        with CaptureQueriesContext(connection) as queries:
            result = build_driver_watch_observation(
                self.watch,
                as_of=self.end,
            )

        self.assertEqual(result['row_count'], 2)
        self.assertEqual(len(queries), 6)

    def test_exposes_shift_and_employee_period_passports_without_scores(self):
        shift = self.create_shift(ShiftType.DAY, self.day_truck)
        excavator_type = EquipmentType.objects.create(
            name='Экскаватор passport output',
        )
        excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='WR-PASS-EXC',
        )
        rock = RockType.objects.create(name='Порода passport output')
        dump_point = DumpPoint.objects.create(
            name='Разгрузка passport output',
        )
        trip = Trip.objects.create(
            excavator=excavator,
            truck=shift.equipment,
            driver=shift.employee,
            unloading_shift=shift,
            rock_type=rock,
            dump_point=dump_point,
            volume_m3=Decimal('50.00'),
            tonnage=Decimal('100.00'),
            transport_distance_km=Decimal('2.00'),
            status=TripStatus.COMPLETED,
            completed_at=self.start + timedelta(hours=2),
        )
        Trip.objects.filter(pk=trip.pk).update(
            created_at=self.start + timedelta(hours=1),
        )

        result = build_driver_watch_observation(
            self.watch,
            as_of=self.end,
        )
        row = result['rows'][0]

        self.assertEqual(len(row['shift_passports']), 1)
        self.assertEqual(
            row['shift_passports'][0]['passport_schema_version'],
            2,
        )
        self.assertEqual(row['passport']['passport_schema_version'], 2)
        self.assertEqual(
            row['passport']['time']['scheduled_window_status'],
            'standard_production_shift_inferred',
        )
        self.assertEqual(
            row['passport']['time']['schedule_confidence_percent'],
            0,
        )
        self.assertFalse(
            row['passport']['time']['work_time_rating_available']
        )
        self.assertEqual(
            row['passport']['time']['work_time_rating_status'],
            'neutral_structural_schedule_and_reason_policy_unavailable',
        )
        self.assertEqual(
            row['passport']['production']['completed_trip_count'],
            1,
        )
        self.assertEqual(
            row['passport']['production']['m3_km']['value'],
            Decimal('100.0000'),
        )
        self.assertFalse(
            row['passport']['quality']['official_rating_eligible']
        )
        self.assertIsNone(
            row['passport']['expected']['actual_to_expected_ratio']
        )
        serialized_keys = str(row['passport']).lower()
        self.assertNotIn("'score'", serialized_keys)
        self.assertNotIn("'place'", serialized_keys)
        self.assertNotIn("'weight'", serialized_keys)
        encoded = json.dumps(
            result,
            cls=DjangoJSONEncoder,
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertIn('"passport_schema_version": 2', encoded)

    def test_mixed_passport_schedule_schema_aggregates_fail_closed(self):
        shift = self.create_shift(ShiftType.DAY, self.day_truck)
        timeline = build_driver_shift_timeline(shift, as_of=self.end)
        current_passport = deepcopy(timeline.passport)
        legacy_passport = deepcopy(timeline.passport)
        legacy_passport['passport_schema_version'] = 1
        legacy_time = legacy_passport['time']
        legacy_time['scheduled_window_status'] = (
            'schedule_snapshot_unavailable'
        )
        for key in (
            'schedule_source',
            'schedule_confidence_percent',
            'inferred_schedule_gap_seconds',
            'work_time_rating_available',
            'work_time_rating_status',
        ):
            legacy_time.pop(key, None)

        aggregate = _aggregate_shift_passports(
            (legacy_passport, current_passport),
            (timeline.cycle_samples, timeline.cycle_samples),
        )
        time_data = aggregate['time']

        self.assertEqual(
            time_data['scheduled_window_status'],
            'mixed_schedule_sources_unavailable',
        )
        self.assertEqual(time_data['schedule_confidence_percent'], 0)
        self.assertFalse(time_data['work_time_rating_available'])
        self.assertEqual(
            time_data['work_time_rating_status'],
            'neutral_structural_schedule_and_reason_policy_unavailable',
        )
        self.assertIsNone(time_data['inferred_schedule_gap_seconds'])
        self.assertIsNone(time_data['observed_short_shift_seconds'])

    def test_period_passport_withholds_total_when_one_shift_is_incomplete(self):
        current_shift = self.create_shift(ShiftType.DAY, self.day_truck)
        previous_shift = EmployeeShift.objects.create(
            employee=self.driver,
            shift_type=ShiftType.DAY,
            workplace_code='driver',
            watch_period=self.watch,
            equipment=self.night_truck,
            opened_at=self.start - timedelta(hours=12),
            closed_at=self.start,
        )
        excavator_type = EquipmentType.objects.create(
            name='Экскаватор aggregate passport',
        )
        excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='WR-AGG-EXC',
        )
        rock = RockType.objects.create(name='Порода aggregate passport')
        dump_point = DumpPoint.objects.create(
            name='Разгрузка aggregate passport',
        )
        complete_trip = Trip.objects.create(
            excavator=excavator,
            truck=current_shift.equipment,
            driver=self.driver,
            unloading_shift=current_shift,
            rock_type=rock,
            dump_point=dump_point,
            volume_m3=Decimal('50.00'),
            tonnage=Decimal('100.00'),
            transport_distance_km=Decimal('2.00'),
            status=TripStatus.COMPLETED,
            completed_at=self.start + timedelta(hours=2),
        )
        Trip.objects.filter(pk=complete_trip.pk).update(
            created_at=self.start + timedelta(hours=1),
        )
        incomplete_trip = Trip.objects.create(
            excavator=excavator,
            truck=previous_shift.equipment,
            driver=self.driver,
            unloading_shift=previous_shift,
            rock_type=rock,
            dump_point=dump_point,
            volume_m3=None,
            tonnage=Decimal('90.00'),
            transport_distance_km=Decimal('2.00'),
            status=TripStatus.COMPLETED,
            completed_at=previous_shift.opened_at + timedelta(hours=2),
        )
        Trip.objects.filter(pk=incomplete_trip.pk).update(
            created_at=previous_shift.opened_at + timedelta(hours=1),
        )

        result = build_driver_watch_observation(
            self.watch,
            as_of=self.end,
        )
        passport = result['rows'][0]['passport']

        self.assertEqual(passport['shift_count'], 2)
        self.assertEqual(
            passport['production']['completed_trip_count'],
            2,
        )
        self.assertIsNone(passport['production']['volume_m3']['value'])
        self.assertEqual(
            passport['production']['volume_m3']['known_value'],
            Decimal('50.00'),
        )
        self.assertEqual(
            passport['production']['volume_m3']['missing_trip_count'],
            1,
        )
        self.assertIsNone(
            passport['rates_per_available_hour']['volume_m3']['value']
        )

    def test_period_passport_keeps_assignment_mismatch_seconds(self):
        shift = self.create_shift(ShiftType.DAY, self.day_truck)
        excavator_type = EquipmentType.objects.create(
            name='Экскаватор mismatch aggregate',
        )
        assigned_excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='WR-MISMATCH-ASSIGNED',
        )
        actual_excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='WR-MISMATCH-ACTUAL',
        )
        HaulAssignment.objects.create(
            excavator=assigned_excavator,
            truck=shift.equipment,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=self.start,
            ended_at=self.end,
        )
        rock = RockType.objects.create(name='Порода mismatch aggregate')
        dump_point = DumpPoint.objects.create(
            name='Разгрузка mismatch aggregate',
        )
        trip = Trip.objects.create(
            excavator=actual_excavator,
            truck=shift.equipment,
            driver=shift.employee,
            unloading_shift=shift,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.COMPLETED,
            completed_at=self.start + timedelta(hours=2),
        )
        Trip.objects.filter(pk=trip.pk).update(
            created_at=self.start + timedelta(hours=1),
        )

        result = build_driver_watch_observation(
            self.watch,
            as_of=self.end,
        )

        self.assertEqual(
            result['rows'][0]['passport']['quality'][
                'quality_metrics'
            ]['trip_assignment_mismatch_seconds'],
            3600,
        )
        self.assertEqual(
            result['rows'][0]['quality_metrics'][
                'trip_assignment_mismatch_seconds'
            ],
            3600,
        )
        self.assertEqual(
            result['summary']['quality_metrics'][
                'trip_assignment_mismatch_seconds'
            ],
            3600,
        )

    def test_linkage_audit_finds_closed_shift_without_watch(self):
        linked = self.create_shift(ShiftType.DAY, self.day_truck)
        unlinked_driver = Employee.objects.create(
            full_name='Водитель без привязки к вахте',
            work_category=Employee.WorkCategory.DRIVER,
        )
        EmployeeShift.objects.create(
            employee=unlinked_driver,
            shift_type=ShiftType.DAY,
            workplace_code='driver',
            equipment=self.night_truck,
            opened_at=linked.opened_at,
            closed_at=linked.closed_at,
        )

        audit = build_driver_watch_linkage_audit(self.watch)

        self.assertEqual(audit['candidate_closed_shift_count'], 2)
        self.assertEqual(audit['linked_to_selected_watch_count'], 1)
        self.assertEqual(audit['unlinked_shift_count'], 1)
        self.assertFalse(audit['linkage_ready'])

    def test_date_range_shadow_observes_unlinked_closed_shift_without_rating(self):
        shift = self.create_shift(ShiftType.DAY, self.day_truck)
        EmployeeShift.objects.filter(pk=shift.pk).update(watch_period=None)
        shift.refresh_from_db()
        work_date = production_work_date(shift.opened_at)

        result = build_driver_period_shadow_observation(
            work_date,
            work_date,
            as_of=self.end,
        )

        self.assertEqual(result['scope_type'], 'date_range_shadow')
        self.assertFalse(result['official_rating_eligible'])
        self.assertEqual(result['summary']['closed_shift_count'], 1)
        self.assertFalse(
            result['summary']['data_ready_for_formula_review']
        )
        self.assertEqual(result['linkage_audit']['unlinked_shift_count'], 1)

    def test_legacy_driver_shift_survives_later_specialization_change(self):
        shift = self.create_shift(ShiftType.DAY, self.day_truck)
        shift.workplace_code = ''
        shift.save(update_fields=['workplace_code'])
        self.driver.work_category = Employee.WorkCategory.OTHER
        self.driver.save(update_fields=['work_category'])

        result = build_driver_watch_observation(
            self.watch,
            as_of=self.end,
        )

        self.assertEqual(result['row_count'], 1)
        self.assertEqual(result['rows'][0]['employee_id'], self.driver.id)

    def test_watch_date_mismatch_is_visible_and_blocks_readiness(self):
        outside_start = production_day_bounds(
            self.watch.starts_on - timedelta(days=1)
        )[0]
        outside_end = outside_start + timedelta(hours=12)
        EmployeeShift.objects.create(
            employee=self.driver,
            shift_type=ShiftType.DAY,
            workplace_code='driver',
            watch_period=self.watch,
            equipment=self.day_truck,
            opened_at=outside_start,
            closed_at=outside_end,
        )

        result = build_driver_watch_observation(
            self.watch,
            as_of=self.end,
        )
        audit = build_driver_watch_linkage_audit(self.watch)

        self.assertEqual(result['row_count'], 1)
        self.assertIn(
            'watch_period_date_mismatch',
            result['rows'][0]['quality_flags'],
        )
        self.assertEqual(
            result['rows'][0]['observation_status'],
            'needs_review',
        )
        self.assertFalse(
            result['summary']['data_ready_for_formula_review']
        )
        self.assertEqual(
            audit['selected_watch_outside_period_count'],
            1,
        )
        self.assertFalse(audit['linkage_ready'])

    def test_source_counts_deduplicate_one_carryover_across_two_drivers(self):
        replacement_driver = Employee.objects.create(
            full_name='Сменщик переходящего рейса',
            work_category=Employee.WorkCategory.DRIVER,
            status=Employee.Status.ACTIVE,
        )
        first_start = self.start - timedelta(hours=12)
        first_shift = EmployeeShift.objects.create(
            employee=self.driver,
            shift_type=ShiftType.DAY,
            workplace_code='driver',
            watch_period=self.watch,
            equipment=self.day_truck,
            opened_at=first_start,
            closed_at=self.start,
        )
        second_shift = EmployeeShift.objects.create(
            employee=replacement_driver,
            shift_type=ShiftType.NIGHT,
            workplace_code='driver',
            watch_period=self.watch,
            equipment=self.day_truck,
            opened_at=self.start,
            closed_at=self.end,
        )
        excavator_type = EquipmentType.objects.create(
            name='Экскаватор carryover watch',
        )
        excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='WR-CARRY-EXC',
        )
        rock = RockType.objects.create(name='Порода carryover watch')
        dump_point = DumpPoint.objects.create(
            name='Разгрузка carryover watch',
        )
        trip = Trip.objects.create(
            excavator=excavator,
            truck=self.day_truck,
            driver=replacement_driver,
            unloading_shift=second_shift,
            rock_type=rock,
            dump_point=dump_point,
            status=TripStatus.COMPLETED,
            completed_at=self.start + timedelta(minutes=30),
            is_carryover=True,
        )
        Trip.objects.filter(pk=trip.pk).update(
            created_at=self.start - timedelta(minutes=30),
        )

        result = build_driver_watch_observation(
            self.watch,
            as_of=self.end,
        )

        self.assertEqual(result['row_count'], 2)
        self.assertEqual(
            result['summary']['source_counts']['trip_count'],
            1,
        )
        self.assertEqual(
            result['summary']['source_counts']['carryover_trip_count'],
            1,
        )
        self.assertTrue(all(
            row['source_counts']['trip_count'] == 1
            for row in result['rows']
        ))

    def test_quality_flag_prevents_observed_status(self):
        long_start = self.end - timedelta(hours=17)
        shift = EmployeeShift.objects.create(
            employee=self.driver,
            shift_type=ShiftType.DAY,
            workplace_code='driver',
            watch_period=self.watch,
            equipment=self.day_truck,
            opened_at=long_start,
            closed_at=self.end,
        )
        waiting = DowntimeReason.objects.get(name='Ожидание погрузки')
        DowntimeEvent.objects.create(
            equipment=shift.equipment,
            employee=self.driver,
            reason=waiting,
            started_at=long_start,
            ended_at=self.end,
        )

        result = build_driver_watch_observation(
            self.watch,
            as_of=self.end,
        )

        self.assertEqual(
            result['rows'][0]['observation_status'],
            'needs_review',
        )
        self.assertIn(
            'shift_duration_over_16h',
            result['rows'][0]['quality_flags'],
        )


class DriverWatchObservationApiTests(TestCase):
    def setUp(self):
        self.client = Client()
        composition = WatchComposition.objects.create(
            code='watch-observation-api',
            name='Состав API наблюдения',
        )
        self.watch = WatchPeriod.objects.create(
            name='Вахта API рейтинга',
            watch_composition=composition,
            starts_on=timezone.localdate(),
            ends_on=timezone.localdate() + timedelta(days=29),
        )
        self.scope_driver = Employee.objects.create(
            full_name='Водитель области API наблюдения',
            status=Employee.Status.ACTIVE,
            is_active=True,
            work_category=Employee.WorkCategory.DRIVER,
            watch_composition=composition,
        )
        self.dispatcher = Employee.objects.create(
            full_name='Диспетчер рейтинга',
            status=Employee.Status.ACTIVE,
        )
        self.dispatcher_role = Role.objects.create(
            code='dispatcher',
            name='Диспетчер рейтинга',
        )
        self.dispatcher_access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=self.dispatcher_role,
            access_code='5500',
            status=EmployeeAccess.Status.ACTIVATED,
        )

    def login_as(self, access):
        login_at = timezone.now()
        access.last_login_at = login_at
        access.save(update_fields=['last_login_at'])
        session = self.client.session
        session['employee_access_id'] = access.id
        session[ACTIVE_ROLE_SESSION_KEY] = access.id
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = login_at.isoformat()
        session[ACTIVE_ROLE_CODE_SESSION_KEY] = access.role.code
        session.save()
        return login_at

    def test_returns_read_only_watch_payload_for_dispatcher(self):
        self.login_as(self.dispatcher_access)

        response = self.client.get(
            reverse('driver_watch_observation_api'),
            {'watch_period': self.watch.id, 'shift_type': ShiftType.DAY},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['watch_period']['id'], self.watch.id)
        self.assertEqual(payload['shift_type'], ShiftType.DAY)
        self.assertEqual(payload['rows'], [])
        self.assertEqual(len(payload['available_watch_periods']), 1)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertFalse(
            payload['summary']['data_ready_for_formula_review']
        )
        self.assertEqual(
            payload['linkage_audit']['candidate_closed_shift_count'],
            0,
        )

    def test_rejects_employee_role_outside_reports(self):
        driver = Employee.objects.create(
            full_name='Водитель без доступа',
            status=Employee.Status.ACTIVE,
        )
        driver_role = Role.objects.create(code='driver', name='Водитель')
        access = EmployeeAccess.objects.create(
            employee=driver,
            role=driver_role,
            access_code='2200',
            status=EmployeeAccess.Status.ACTIVATED,
        )
        self.login_as(access)

        response = self.client.get(reverse('driver_watch_observation_api'))

        self.assertEqual(response.status_code, 403)

    def test_rejects_not_activated_access(self):
        employee = Employee.objects.create(
            full_name='Неактивированный диспетчер',
            status=Employee.Status.ACTIVE,
        )
        access = EmployeeAccess.objects.create(
            employee=employee,
            role=self.dispatcher_role,
            access_code='5510',
            status=EmployeeAccess.Status.NOT_ACTIVATED,
        )
        self.login_as(access)

        response = self.client.get(reverse('driver_watch_observation_api'))

        self.assertEqual(response.status_code, 401)

    def test_rejects_deactivated_access(self):
        self.dispatcher_access.is_active = False
        self.dispatcher_access.status = EmployeeAccess.Status.DEACTIVATED
        self.dispatcher_access.save(update_fields=['is_active', 'status'])
        self.login_as(self.dispatcher_access)

        response = self.client.get(reverse('driver_watch_observation_api'))

        self.assertEqual(response.status_code, 401)

    def test_rejects_dismissed_employee(self):
        self.dispatcher.status = Employee.Status.DISMISSED
        self.dispatcher.is_active = False
        self.dispatcher.save(update_fields=['status', 'is_active'])
        self.login_as(self.dispatcher_access)

        response = self.client.get(reverse('driver_watch_observation_api'))

        self.assertEqual(response.status_code, 401)

    def test_rejects_inactive_role(self):
        self.dispatcher_role.is_active = False
        self.dispatcher_role.save(update_fields=['is_active'])
        self.login_as(self.dispatcher_access)

        response = self.client.get(reverse('driver_watch_observation_api'))

        self.assertEqual(response.status_code, 401)

    def test_rejects_stale_active_role_generation(self):
        login_at = self.login_as(self.dispatcher_access)
        EmployeeAccess.objects.filter(
            pk=self.dispatcher_access.pk,
        ).update(last_login_at=login_at + timedelta(seconds=1))

        response = self.client.get(reverse('driver_watch_observation_api'))

        self.assertEqual(response.status_code, 401)

    def test_rejects_session_without_server_login_generation(self):
        session = self.client.session
        session['employee_access_id'] = self.dispatcher_access.id
        session[ACTIVE_ROLE_SESSION_KEY] = self.dispatcher_access.id
        session[ACTIVE_ROLE_GENERATION_SESSION_KEY] = (
            timezone.now().isoformat()
        )
        session[ACTIVE_ROLE_CODE_SESSION_KEY] = (
            self.dispatcher_access.role.code
        )
        session.save()
        work_date = timezone.localdate()

        watch_response = self.client.get(
            reverse('driver_watch_observation_api')
        )
        shadow_response = self.client.get(
            reverse('driver_period_shadow_observation_api'),
            {
                'date_from': work_date.isoformat(),
                'date_to': work_date.isoformat(),
            },
        )

        self.assertEqual(watch_response.status_code, 401)
        self.assertEqual(shadow_response.status_code, 401)

    def test_validates_filter_values(self):
        self.login_as(self.dispatcher_access)

        response = self.client.get(
            reverse('driver_watch_observation_api'),
            {'shift_type': 'all'},
        )

        self.assertEqual(response.status_code, 400)

    def test_returns_date_range_shadow_without_watch_period(self):
        self.login_as(self.dispatcher_access)
        work_date = timezone.localdate()

        response = self.client.get(
            reverse('driver_period_shadow_observation_api'),
            {
                'date_from': work_date.isoformat(),
                'date_to': work_date.isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['scope_type'], 'date_range_shadow')
        self.assertFalse(payload['official_rating_eligible'])

    def test_shadow_rejects_period_longer_than_thirty_one_days(self):
        self.login_as(self.dispatcher_access)
        starts_on = timezone.localdate()

        response = self.client.get(
            reverse('driver_period_shadow_observation_api'),
            {
                'date_from': starts_on.isoformat(),
                'date_to': (starts_on + timedelta(days=31)).isoformat(),
            },
        )

        self.assertEqual(response.status_code, 400)

    def test_empty_watch_list_keeps_complete_shadow_schema(self):
        self.login_as(self.dispatcher_access)
        normal_response = self.client.get(
            reverse('driver_watch_observation_api')
        )
        self.watch.delete()

        response = self.client.get(reverse('driver_watch_observation_api'))

        self.assertEqual(normal_response.status_code, 200)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            set(payload),
            set(normal_response.json()),
        )
        self.assertEqual(payload['scope_type'], 'watch_period')
        self.assertFalse(payload['official_rating_eligible'])
        self.assertIsNone(payload['watch_period'])
        self.assertIn('generated_at', payload)
        self.assertFalse(payload['linkage_audit']['linkage_ready'])
        self.assertEqual(
            payload['linkage_audit'][
                'selected_watch_outside_period_count'
            ],
            0,
        )
        self.assertIn('usable_shift_count', payload['summary'])
