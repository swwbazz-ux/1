from datetime import timedelta

from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
from core.qa_environment import require_excavator_qa_environment
from shifts.models import EmployeeShift

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
            'EXCAVATOR_QA_TRUCK_COUNT': 3,
            'EXCAVATOR_QA_TRANSIT_SECONDS': 5,
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
            second = prepare_excavator_qa_scenario()

        self.assertEqual(first.operator.pk, second.operator.pk)
        self.assertEqual(first.excavator.pk, second.excavator.pk)
        self.assertEqual(len(first.trucks), 3)
        self.assertEqual(len(second.trucks), 3)
        self.assertEqual(
            first.operator.accesses.get(role__code='excavator_operator').access_code,
            '314159',
        )
        placement.refresh_from_db()
        self.assertEqual(placement.loading_horizon, '999')

    def test_tick_waits_for_human_to_open_excavator_shift(self):
        with self.qa_settings():
            prepare_excavator_qa_scenario()
            result = run_excavator_qa_tick()

        self.assertEqual(result['state'], 'waiting_for_excavator_shift')
        self.assertFalse(HaulAssignment.objects.exists())

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

    def test_invalid_credentials_are_rejected_before_seed(self):
        with self.qa_settings(EXCAVATOR_QA_PIN='12.34'):
            with self.assertRaisesMessage(ValueError, 'exactly 6 digits'):
                prepare_excavator_qa_scenario()
