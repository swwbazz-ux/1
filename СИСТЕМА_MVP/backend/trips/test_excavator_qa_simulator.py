from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, EquipmentAssignment, HaulAssignment
from assignments.services import apply_pending_haul_assignment
from core.qa_environment import require_excavator_qa_environment
from downtimes.models import DowntimeReason
from shifts.models import (
    EmployeeShift,
    EquipmentPlanGroup,
    PlanAssignmentStatus,
    PlanCalculationMode,
)
from shifts.services import open_driver_shift

from .models import Trip, TripStatus
from .qa_simulator import (
    prepare_excavator_qa_scenario,
    run_excavator_qa_tick,
)
from .trip_creation import create_loaded_waiting_unload_trip


class ExcavatorQASimulatorTests(TestCase):
    def qa_settings(self, **extra):
        values = {
            'EXCAVATOR_QA_ENABLED': True,
            'EXCAVATOR_QA_DATABASE_NAME': str(connection.settings_dict['NAME']),
            'EXCAVATOR_QA_PHONE': '+7 900 000-00-03',
            'EXCAVATOR_QA_PIN': '314159',
            'DRIVER_QA_PHONE': '+7 911 111-11-12',
            'DRIVER_QA_PIN': '271828',
            'EXCAVATOR_QA_TRUCK_COUNT': 3,
            'EXCAVATOR_QA_TRANSIT_SECONDS': 5,
            'EXCAVATOR_QA_PLAN_TRIPS': 20,
            'DRIVER_QA_LOADING_SECONDS': 5,
        }
        values.update(extra)
        return override_settings(**values)

    def test_guard_rejects_disabled_and_wrong_database(self):
        with self.assertRaisesMessage(CommandError, 'not enabled'):
            require_excavator_qa_environment()
        with override_settings(
            EXCAVATOR_QA_ENABLED=True,
            EXCAVATOR_QA_DATABASE_NAME='accounting_mvp_qa_other',
        ):
            with self.assertRaisesMessage(CommandError, 'guard rejected'):
                require_excavator_qa_environment()

    def test_prepare_is_idempotent_and_keeps_secret_out_of_output_state(self):
        with self.qa_settings():
            first = prepare_excavator_qa_scenario()
            placement = first.excavator.excavator_placement
            placement.loading_horizon = '999'
            placement.save(update_fields=['loading_horizon'])
            open_shift = EmployeeShift.objects.create(
                employee=first.operator,
                equipment=first.excavator,
                shift_type='day',
                workplace_code='excavator_operator',
                start_fuel='6000',
                start_engine_hours='1200',
                opened_at=timezone.now(),
                opened_by=first.operator,
            )
            second = prepare_excavator_qa_scenario()

        self.assertEqual(first.operator.pk, second.operator.pk)
        self.assertEqual(first.excavator.pk, second.excavator.pk)
        self.assertEqual(first.human_driver.pk, second.human_driver.pk)
        self.assertEqual(first.human_driver_truck.pk, second.human_driver_truck.pk)
        self.assertEqual(first.driver_bot_operator.pk, second.driver_bot_operator.pk)
        self.assertEqual(first.driver_bot_excavator.pk, second.driver_bot_excavator.pk)
        self.assertEqual(len(first.trucks), 3)
        self.assertEqual(len(second.trucks), 3)
        self.assertEqual(
            DowntimeReason.for_workplace(
                'excavator_operator', first.excavator.equipment_type
            ).count(),
            12,
        )
        self.assertEqual(
            DowntimeReason.for_workplace(
                'truck_driver', first.human_driver_truck.equipment_type
            ).count(),
            14,
        )
        self.assertEqual(
            first.operator.accesses.get(role__code='excavator_operator').access_code,
            '314159',
        )
        self.assertEqual(
            first.human_driver.accesses.get(role__code='driver').access_code,
            '271828',
        )
        self.assertEqual(first.human_driver.phone, '79111111112')
        self.assertFalse(
            EmployeeShift.objects.filter(
                employee=first.human_driver,
                closed_at__isnull=True,
            ).exists()
        )
        self.assertEqual(
            EmployeeShift.objects.filter(
                employee=first.driver_bot_operator,
                equipment=first.driver_bot_excavator,
                closed_at__isnull=True,
            ).count(),
            1,
        )
        placement.refresh_from_db()
        self.assertEqual(placement.loading_horizon, '999')
        plan_group = EquipmentPlanGroup.objects.get(code='rustore-qa-excavator')
        self.assertEqual(plan_group.calculation_mode, PlanCalculationMode.TRIPS)
        self.assertEqual(plan_group.plan_value, 20)
        self.assertEqual(list(plan_group.equipment.all()), [first.excavator])
        open_shift.refresh_from_db()
        self.assertEqual(open_shift.plan_group, plan_group)
        self.assertEqual(open_shift.plan_status, PlanAssignmentStatus.ASSIGNED)
        self.assertEqual(open_shift.plan_calculation_mode, PlanCalculationMode.TRIPS)
        self.assertEqual(open_shift.plan_value, 20)
        driver_plan_group = EquipmentPlanGroup.objects.get(code='rustore-qa-driver')
        self.assertEqual(driver_plan_group.calculation_mode, PlanCalculationMode.TRIPS)
        self.assertEqual(driver_plan_group.plan_value, 20)
        self.assertEqual(
            list(driver_plan_group.equipment.all()),
            [first.human_driver_truck],
        )

    def test_prepare_command_reports_both_scenarios_without_credentials(self):
        stdout = StringIO()
        with self.qa_settings():
            call_command('prepare_excavator_qa', stdout=stdout)

        output = stdout.getvalue()
        self.assertIn('самосвалов-ботов 3', output)
        self.assertIn('ручной самосвал QA-DRIVER-T-01', output)
        self.assertNotIn('79111111112', output)
        self.assertNotIn('271828', output)

    def test_tick_waits_for_human_to_open_excavator_shift(self):
        with self.qa_settings():
            prepare_excavator_qa_scenario()
            result = run_excavator_qa_tick()

        self.assertEqual(result['state'], 'waiting_for_excavator_shift')
        self.assertEqual(result['driver_state'], 'waiting_for_driver_shift')
        self.assertFalse(HaulAssignment.objects.exists())

    def test_human_driver_opens_shift_accepts_loading_and_unloads_manually(self):
        now = timezone.now()
        with self.qa_settings():
            scenario = prepare_excavator_qa_scenario()
            work_assignment = EquipmentAssignment.objects.get(
                employee=scenario.human_driver,
                equipment=scenario.human_driver_truck,
                role__code='driver',
                shift__isnull=True,
                ended_at__isnull=True,
            )
            driver_shift, created = open_driver_shift(
                employee=scenario.human_driver,
                work_assignment=work_assignment,
                readings={
                    'start_fuel': 1000,
                    'start_mileage': 10000,
                    'start_engine_hours': 2000,
                },
                client_action_id='qa-human-driver-open-1',
            )
            assignment_tick = run_excavator_qa_tick(now=now)
            assignment = HaulAssignment.objects.get(
                truck=scenario.human_driver_truck,
                excavator=scenario.driver_bot_excavator,
                ended_at__isnull=True,
            )
            pending_status = assignment.status
            apply_pending_haul_assignment(assignment.id, now=now)
            assignment.refresh_from_db()
            accepted_status = assignment.status
            waiting_tick = run_excavator_qa_tick(
                now=now + timedelta(seconds=4)
            )
            loaded_tick = run_excavator_qa_tick(
                now=now + timedelta(seconds=6)
            )
            trip = Trip.objects.get(
                truck=scenario.human_driver_truck,
                excavator=scenario.driver_bot_excavator,
            )
            untouched_tick = run_excavator_qa_tick(
                now=now + timedelta(minutes=30)
            )
            status_before_manual_unload = Trip.objects.values_list(
                'status', flat=True
            ).get(pk=trip.pk)

            from trips.views import finalize_trip_unloaded

            unloaded = finalize_trip_unloaded(
                trip,
                driver=scenario.human_driver,
                unloading_shift=driver_shift,
            )

        self.assertTrue(created)
        self.assertEqual(driver_shift.plan_group.code, 'rustore-qa-driver')
        self.assertEqual(assignment_tick['state'], 'waiting_for_excavator_shift')
        self.assertEqual(assignment_tick['driver_state'], 'assignment_pending')
        self.assertEqual(assignment_tick['driver_assigned'], 1)
        self.assertEqual(pending_status, AssignmentStatus.PENDING)
        self.assertEqual(accepted_status, AssignmentStatus.ACCEPTED)
        self.assertEqual(waiting_tick['driver_state'], 'waiting_for_bot_load')
        self.assertEqual(waiting_tick['driver_loaded'], 0)
        self.assertEqual(loaded_tick['driver_state'], 'loaded_waiting_unload')
        self.assertEqual(loaded_tick['driver_loaded'], 1)
        self.assertEqual(
            status_before_manual_unload,
            TripStatus.LOADED_WAITING_UNLOAD,
        )
        self.assertEqual(untouched_tick['driver_state'], 'loaded_waiting_unload')
        trip.refresh_from_db()
        self.assertTrue(unloaded)
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.driver, scenario.human_driver)
        self.assertEqual(trip.unloading_shift, driver_shift)

    def test_prepared_driver_completes_the_real_http_workflow(self):
        with self.qa_settings():
            scenario = prepare_excavator_qa_scenario()
            access = scenario.human_driver.accesses.get(role__code='driver')
            session = self.client.session
            session['employee_access_id'] = access.pk
            session.save()

            open_response = self.client.post(
                reverse('driver_shift'),
                {
                    'shift_type': 'day',
                    'truck': scenario.human_driver_truck.pk,
                    'start_fuel': '1000',
                    'start_mileage': '10000',
                    'start_engine_hours': '2000',
                    'client_action_id': 'qa-driver-http-open-1',
                },
                HTTP_HOST='localhost',
            )
            assignment_tick = run_excavator_qa_tick()
            assignment = HaulAssignment.objects.get(
                truck=scenario.human_driver_truck,
                excavator=scenario.driver_bot_excavator,
                ended_at__isnull=True,
            )
            pending_page = self.client.get(
                reverse('driver_work'),
                HTTP_HOST='localhost',
            )
            accept_response = self.client.post(
                reverse('driver_accept_assignment', args=[assignment.pk]),
                HTTP_HOST='localhost',
            )
            assignment.refresh_from_db()
            loaded_tick = run_excavator_qa_tick(
                now=assignment.accepted_at + timedelta(seconds=6)
            )
            trip = Trip.objects.get(truck=scenario.human_driver_truck)
            loaded_page = self.client.get(
                reverse('driver_work'),
                HTTP_HOST='localhost',
            )
            complete_response = self.client.post(
                reverse('driver_complete_trip', args=[trip.pk]),
                {'client_action_id': 'qa-driver-http-unload-1'},
                HTTP_HOST='localhost',
            )

        self.assertEqual(open_response.status_code, 302)
        self.assertEqual(assignment_tick['driver_state'], 'assignment_pending')
        self.assertContains(pending_page, 'ПРИНЯТЬ')
        self.assertEqual(accept_response.status_code, 302)
        self.assertEqual(assignment.status, AssignmentStatus.ACCEPTED)
        self.assertEqual(loaded_tick['driver_loaded'], 1)
        self.assertContains(loaded_page, 'ТОЧКА РАЗГРУЗКИ')
        self.assertContains(loaded_page, 'Дробилка')
        self.assertEqual(complete_response.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.driver, scenario.human_driver)

    def test_tick_assigns_trucks_then_completes_human_loaded_trip(self):
        now = timezone.now()
        with self.qa_settings():
            scenario = prepare_excavator_qa_scenario()
            loading_shift = EmployeeShift.objects.create(
                employee=scenario.operator,
                equipment=scenario.excavator,
                shift_type='day',
                workplace_code='excavator_operator',
                start_fuel='6000',
                start_engine_hours='1200',
                opened_at=now,
                opened_by=scenario.operator,
            )
            first_tick = run_excavator_qa_tick(now=now)
            assignments = list(
                HaulAssignment.objects.filter(
                    excavator=scenario.excavator,
                    status=AssignmentStatus.ACCEPTED,
                    ended_at__isnull=True,
                ).order_by('truck__garage_number')
            )
            placement = scenario.excavator.excavator_placement
            trip = create_loaded_waiting_unload_trip(
                assignment=assignments[0],
                excavator_operator=scenario.operator,
                loading_shift=loading_shift,
                rock_type=placement.work_rock_type,
                dump_point=placement.work_dump_point,
                loading_horizon=placement.loading_horizon,
                loading_block=placement.loading_block,
            )
            Trip.objects.filter(pk=trip.pk).update(
                created_at=now - timedelta(seconds=6)
            )
            second_tick = run_excavator_qa_tick(now=now)

        self.assertEqual(first_tick['state'], 'running')
        self.assertEqual(first_tick['assigned'], 3)
        self.assertEqual(len(assignments), 3)
        self.assertEqual(second_tick['completed'], 1)
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertIsNotNone(trip.driver_id)
        self.assertIsNotNone(trip.unloading_shift_id)

    def test_tick_unloads_due_trucks_one_at_a_time_in_arrival_order(self):
        now = timezone.now()
        with self.qa_settings():
            scenario = prepare_excavator_qa_scenario()
            loading_shift = EmployeeShift.objects.create(
                employee=scenario.operator,
                equipment=scenario.excavator,
                shift_type='day',
                workplace_code='excavator_operator',
                start_fuel='6000',
                start_engine_hours='1200',
                opened_at=now,
                opened_by=scenario.operator,
            )
            run_excavator_qa_tick(now=now)
            assignments = list(
                HaulAssignment.objects.filter(
                    excavator=scenario.excavator,
                    status=AssignmentStatus.ACCEPTED,
                    ended_at__isnull=True,
                ).order_by('truck__garage_number')
            )
            placement = scenario.excavator.excavator_placement
            trips = [
                create_loaded_waiting_unload_trip(
                    assignment=assignment,
                    excavator_operator=scenario.operator,
                    loading_shift=loading_shift,
                    rock_type=placement.work_rock_type,
                    dump_point=placement.work_dump_point,
                    loading_horizon=placement.loading_horizon,
                    loading_block=placement.loading_block,
                )
                for assignment in assignments[:2]
            ]
            Trip.objects.filter(pk=trips[0].pk).update(
                created_at=now - timedelta(seconds=7)
            )
            Trip.objects.filter(pk=trips[1].pk).update(
                created_at=now - timedelta(seconds=6)
            )

            first_unload = run_excavator_qa_tick(now=now)
            first_statuses = list(
                Trip.objects.filter(pk__in=[trip.pk for trip in trips])
                .order_by('created_at', 'id')
                .values_list('status', flat=True)
            )
            second_unload = run_excavator_qa_tick(now=now + timedelta(seconds=2))
            second_statuses = list(
                Trip.objects.filter(pk__in=[trip.pk for trip in trips])
                .order_by('created_at', 'id')
                .values_list('status', flat=True)
            )

        self.assertEqual(first_unload['completed'], 1)
        self.assertEqual(
            first_statuses,
            [TripStatus.COMPLETED, TripStatus.LOADED_WAITING_UNLOAD],
        )
        self.assertEqual(second_unload['completed'], 1)
        self.assertEqual(
            second_statuses,
            [TripStatus.COMPLETED, TripStatus.COMPLETED],
        )

    def test_invalid_credentials_are_rejected_before_seed(self):
        with self.qa_settings(EXCAVATOR_QA_PIN='12.34'):
            with self.assertRaisesMessage(ValueError, 'exactly 6 digits'):
                prepare_excavator_qa_scenario()

        with self.qa_settings(DRIVER_QA_PHONE='+7 800 000-00-00'):
            with self.assertRaisesMessage(ValueError, 'DRIVER_QA_PHONE'):
                prepare_excavator_qa_scenario()

        with self.qa_settings(DRIVER_QA_PHONE='+7 900 000-00-03'):
            with self.assertRaisesMessage(ValueError, 'must differ'):
                prepare_excavator_qa_scenario()
