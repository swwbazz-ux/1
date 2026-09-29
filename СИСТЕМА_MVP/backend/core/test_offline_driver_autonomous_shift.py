"""Смена водителя, открытая и закрытая на телефоне без связи (30.09.2026).

Владелец: приложение не ждёт сервера — смена открывается и закрывается по
нажатию, сервер догоняет, когда появится связь, и ничего не отклоняет.
Открытие идёт в общую очередь как ``driver.shift.opened``; события в такой
смене ссылаются на неё через ``local_shift_id`` — тем же способом, что у
машиниста (core/test_offline_autonomous_shift.py).
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from assignments.models import HaulAssignment
from core.models import OfflineFieldEvent
from core import test_free_bucket_sync as free_bucket_fixtures
from shifts.models import EmployeeShift
from trips.models import Trip, TripStatus


class DriverAutonomousShiftTests(TestCase):
    create_registered_driver_shift = free_bucket_fixtures.FreeBucketServerIntegrationTests.create_registered_driver_shift
    driver_event = free_bucket_fixtures.FreeBucketServerIntegrationTests.driver_event
    sync = free_bucket_fixtures.FreeBucketServerIntegrationTests.sync
    sync_driver = free_bucket_fixtures.FreeBucketServerIntegrationTests.sync_driver

    def setUp(self):
        free_bucket_fixtures.FreeBucketServerIntegrationTests.setUp(self)
        self.base = timezone.now() - timedelta(hours=2)
        # Прежняя смена водителя закрыта: телефон открывает новую без связи.
        EmployeeShift.objects.filter(pk=self.truck_shift.pk).update(
            opened_at=self.base - timedelta(hours=8),
            closed_at=self.base - timedelta(minutes=30),
            end_fuel=Decimal('410'),
            end_mileage=Decimal('12000'),
            end_engine_hours=Decimal('3000'),
        )
        self.truck_shift.refresh_from_db()
        HaulAssignment.objects.filter(pk=self.assignment.pk).update(assigned_at=self.base - timedelta(hours=9))
        self.assignment.refresh_from_db()
        # Боевые модели самосвалов имеют настроенный бак — без него любое
        # закрытие уходило бы в запасной путь «показания без проверки».
        self.truck.model.fuel_capacity_limit_l = 800
        self.truck.model.save(update_fields=['fuel_capacity_limit_l'])

    def local_event(self, event_id, event_type, sequence, *, local_shift_id, payload, occurred_at, depends_on=None):
        event = self.driver_event(
            event_id, event_type, sequence,
            payload={**payload, 'local_shift_id': local_shift_id},
            occurred_at=occurred_at,
            depends_on=depends_on,
        )
        event['shift_id'] = None
        event['local_shift_id'] = local_shift_id
        return event

    def opening(self, event_id, sequence, occurred_at, *, depends_on=None, readings=None):
        return self.local_event(
            event_id, 'driver.shift.opened', sequence,
            local_shift_id=event_id,
            occurred_at=occurred_at,
            depends_on=depends_on,
            payload={
                'truck_id': self.truck.id,
                'shift_type': 'day',
                **(readings or {'start_fuel': '410', 'start_mileage': '12000', 'start_engine_hours': '3000'}),
            },
        )

    def closing(self, opening, event_id, sequence, occurred_at, readings=None):
        return self.local_event(
            event_id, 'driver.shift.closed', sequence,
            local_shift_id=opening['event_id'],
            occurred_at=occurred_at,
            depends_on=[opening['event_id']],
            payload=readings or {'end_fuel': '300', 'end_mileage': '12040', 'end_engine_hours': '3008'},
        )

    def manual_load(self, opening, event_id, sequence, occurred_at):
        from trips.free_bucket import canonical_free_bucket_work_context_snapshot

        context = canonical_free_bucket_work_context_snapshot(self.excavator)
        event = self.local_event(
            event_id, 'driver.trip.loaded', sequence,
            local_shift_id=opening['event_id'],
            occurred_at=occurred_at,
            depends_on=[opening['event_id']],
            payload={
                'manual_control': True,
                'truck_id': self.truck.id,
                'excavator_id': self.excavator.id,
                'dump_point_id': self.dump_point.id,
                'rock_type_id': self.rock.id,
                'placement_id': context['placement_id'],
                'loading_horizon': context['loading_horizon'],
                'loading_block': context['loading_block'],
                'assignment_id': self.assignment.id,
                'free_bucket_acceptance_id': None,
                'free_bucket_acceptance_local_id': None,
            },
        )
        event['local_trip_id'] = event_id
        event['context_snapshot'].update({
            'source': 'driver_manual',
            'dump_points': context['dump_points'],
            'selected_dump_point_id': self.dump_point.id,
            'selected_one_off': False,
        })
        return event

    def manual_complete(self, load, event_id, sequence, occurred_at):
        event = self.local_event(
            event_id, 'driver.trip.manual_completed', sequence,
            local_shift_id=load['local_shift_id'],
            occurred_at=occurred_at,
            depends_on=[load['event_id']],
            payload={
                'manual_control': True,
                'truck_id': self.truck.id,
                'excavator_id': self.excavator.id,
                'dump_point_id': self.dump_point.id,
            },
        )
        event['trip_id'] = None
        event['local_trip_id'] = load['event_id']
        event['context_snapshot'].update({
            'source': 'driver_manual', 'action': 'manual_completed',
            'selected_dump_point_id': self.dump_point.id,
        })
        return event

    def new_shifts(self):
        return list(
            EmployeeShift.objects.filter(employee=self.driver).exclude(pk=self.truck_shift.pk).order_by('opened_at')
        )

    def test_two_offline_shifts_reach_the_server_with_phone_times_and_trips_in_the_first(self):
        t0 = self.base
        open1 = self.opening('driver-shift-open:first', 1, t0)
        load = self.manual_load(open1, 'driver-manual-load:first', 2, t0 + timedelta(minutes=5))
        complete = self.manual_complete(load, 'driver-manual-complete:first', 3, t0 + timedelta(minutes=12))
        close1 = self.closing(open1, 'driver-shift-close:first', 4, t0 + timedelta(minutes=20))
        open2 = self.opening(
            'driver-shift-open:second', 5, t0 + timedelta(minutes=25),
            depends_on=[close1['event_id']],
            readings={'start_fuel': '300', 'start_mileage': '12040', 'start_engine_hours': '3008'},
        )

        response = self.sync_driver([open1, load, complete, close1, open2])

        self.assertEqual(response.status_code, 200, response.content)
        results = {item['event_id']: item for item in response.json()['results']}
        self.assertEqual(
            {event_id: item['status'] for event_id, item in results.items()},
            {event['event_id']: 'accepted' for event in (open1, load, complete, close1, open2)},
            results,
        )
        first, second = self.new_shifts()
        self.assertEqual(first.opened_at, t0)
        self.assertEqual(first.closed_at, t0 + timedelta(minutes=20))
        self.assertEqual(first.end_mileage, Decimal('12040'))
        self.assertEqual(first.workplace_code, 'driver')
        self.assertEqual(first.equipment_id, self.truck.id)
        self.assertEqual(first.start_fuel, Decimal('410'))
        self.assertEqual(second.opened_at, t0 + timedelta(minutes=25))
        self.assertIsNone(second.closed_at)
        self.assertEqual(results[open1['event_id']]['server_ids']['shift_id'], first.id)
        self.assertEqual(results[open2['event_id']]['server_ids']['shift_id'], second.id)
        trip = Trip.objects.get(pk=results[load['event_id']]['server_ids']['trip_id'])
        self.assertEqual(trip.driver_control_shift_id, first.id)
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.loaded_at, t0 + timedelta(minutes=5))
        self.assertEqual(OfflineFieldEvent.objects.get(event_id=load['event_id']).shift_id, first.id)

        repeated = self.sync_driver([open1, close1, open2])
        self.assertEqual(
            [item['status'] for item in repeated.json()['results']],
            ['deduplicated', 'deduplicated', 'deduplicated'],
        )
        self.assertEqual(len(self.new_shifts()), 2)

    def test_opening_closes_the_employees_still_open_shift_at_the_phone_time(self):
        """Решение Б «завершить и начать»: прежняя смена закрывается моментом новой."""
        EmployeeShift.objects.filter(pk=self.truck_shift.pk).update(closed_at=None)
        opened_at = timezone.now() - timedelta(minutes=10)

        response = self.sync_driver([self.opening('driver-shift-open:handover', 1, opened_at)])

        self.assertEqual(response.json()['results'][0]['status'], 'accepted', response.content)
        self.truck_shift.refresh_from_db()
        self.assertEqual(self.truck_shift.closed_at, opened_at)
        self.assertTrue(self.truck_shift.is_service_closed)
        (shift,) = self.new_shifts()
        self.assertEqual(shift.opened_at, opened_at)
        self.assertIsNone(shift.closed_at)

    def test_event_of_a_local_shift_waits_for_its_opening_instead_of_being_rejected(self):
        open1 = self.opening('driver-shift-open:late', 1, self.base)
        load = self.manual_load(open1, 'driver-manual-load:early', 2, self.base + timedelta(minutes=3))

        first = self.sync_driver([load]).json()['results'][0]
        self.assertEqual(first['status'], 'retry', first)
        self.assertEqual(self.new_shifts(), [])

        # Открытие дошло — отметка по местной смене проходит при повторе.
        results = self.sync_driver([open1, load]).json()['results']
        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted'], results)
        (shift,) = self.new_shifts()
        self.assertEqual(Trip.objects.get(pk=results[1]['server_ids']['trip_id']).driver_control_shift_id, shift.id)

    def test_event_without_dependency_on_an_unknown_local_shift_is_retried(self):
        load = self.manual_load(
            {'event_id': 'driver-shift-open:never-sent', 'local_shift_id': 'driver-shift-open:never-sent'},
            'driver-manual-load:orphan', 2, self.base + timedelta(minutes=3),
        )
        load['depends_on'] = []

        result = self.sync_driver([load]).json()['results'][0]

        self.assertEqual(result['status'], 'retry', result)
        self.assertEqual(result['code'], 'shift_reference_pending')

    def test_suspicious_closing_readings_do_not_keep_the_shift_open(self):
        """Показания «на сверку» раньше оставляли смену открытой — теперь пишутся как введены."""
        open1 = self.opening('driver-shift-open:odd', 1, self.base)
        close1 = self.closing(
            open1, 'driver-shift-close:odd', 2, self.base + timedelta(minutes=30),
            readings={'end_fuel': '900', 'end_mileage': '11000', 'end_engine_hours': '2000'},
        )

        results = self.sync_driver([open1, close1]).json()['results']

        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted'], results)
        (shift,) = self.new_shifts()
        self.assertEqual(shift.closed_at, self.base + timedelta(minutes=30))
        self.assertEqual(shift.end_mileage, Decimal('11000'))

    def test_closing_readings_the_server_cannot_validate_still_close_the_shift(self):
        """Топливо больше бака — раньше shift_close_failed и открытая смена навсегда."""
        open1 = self.opening('driver-shift-open:overfuel', 1, self.base)
        close1 = self.closing(
            open1, 'driver-shift-close:overfuel', 2, self.base + timedelta(minutes=40),
            readings={'end_fuel': '5000', 'end_mileage': '12050', 'end_engine_hours': '3010'},
        )
        open2 = self.opening(
            'driver-shift-open:after-overfuel', 3, self.base + timedelta(minutes=45),
            depends_on=[close1['event_id']],
        )

        results = self.sync_driver([open1, close1, open2]).json()['results']

        self.assertEqual([item['status'] for item in results], ['accepted'] * 3, results)
        first, second = self.new_shifts()
        self.assertEqual(first.closed_at, self.base + timedelta(minutes=40))
        self.assertEqual(first.end_fuel, Decimal('5000'))
        self.assertIsNone(second.closed_at)

    def test_opening_does_not_move_the_phone_time_for_a_quick_send(self):
        """Быстрая отправка (sent_live) не подменяет начало смены временем сервера."""
        opened_at = timezone.now() - timedelta(minutes=2)
        opening = self.opening('driver-shift-open:live', 1, opened_at)
        opening['sent_live'] = True

        result = self.sync_driver([opening]).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        (shift,) = self.new_shifts()
        self.assertEqual(shift.opened_at, opened_at)
