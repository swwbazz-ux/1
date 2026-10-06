"""Адресные воспроизведения F4/F5/F6: факт старой истории не меняет новую."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from core import test_free_bucket_sync as fixtures
from core.models import OfflineFieldEvent
from core.offline_sync import normalize_offline_event
from downtimes.models import DowntimeEvent, DowntimeReason
from shifts.models import EmployeeShift, ShiftClientAction
from shifts.services import open_driver_shift_from_device
from trips.manual_loading import reconcile_expired_manual_trips, trip_driver_control_filter
from trips.models import Trip, TripClientAction, TripStatus


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class HistoricalBoundariesTests(TestCase):
    create_registered_driver_shift = fixtures.FreeBucketServerIntegrationTests.create_registered_driver_shift
    driver_event = fixtures.FreeBucketServerIntegrationTests.driver_event
    select_event = fixtures.FreeBucketServerIntegrationTests.select_event
    sync = fixtures.FreeBucketServerIntegrationTests.sync
    sync_driver = fixtures.FreeBucketServerIntegrationTests.sync_driver

    def setUp(self):
        fixtures.FreeBucketServerIntegrationTests.setUp(self)
        self.base = timezone.now() - timedelta(hours=8)
        EmployeeShift.objects.filter(pk__in=[self.shift.pk, self.truck_shift.pk]).update(opened_at=self.base)
        self.truck_shift.refresh_from_db()
        self.shift.refresh_from_db()
        self.wait = DowntimeReason.objects.get(name='Ожидание погрузки')

    def trip(self, **changes):
        values = dict(
            excavator=self.excavator, truck=self.truck, driver=self.driver,
            excavator_operator=self.operator, loading_shift=self.shift,
            rock_type=self.rock, dump_point=self.dump_point,
            assigned_dump_point=self.dump_point,
            driver_participation_recorded=True, driver_control_shift=self.truck_shift,
            status=TripStatus.LOADED_WAITING_UNLOAD, loaded_at=self.base + timedelta(minutes=5),
        )
        values.update(changes)
        return Trip.objects.create(**values)

    def stop(self, event, *, at, ident='history-stop'):
        return self.driver_event(
            ident, 'driver.downtime.ended', 1,
            occurred_at=at, payload={'downtime_event_id': event.pk},
        )

    def test_ordinary_late_stop_does_not_extend_a_closed_interval(self):
        end = self.base + timedelta(hours=2)
        event = DowntimeEvent.objects.create(
            equipment=self.truck, employee=self.driver, reason=self.wait,
            started_at=self.base + timedelta(hours=1), ended_at=end,
        )
        result = self.sync_driver([self.stop(event, at=self.base + timedelta(hours=7))]).json()['results'][0]
        self.assertEqual(result['status'], 'accepted', result)
        event.refresh_from_db()
        self.assertEqual(event.ended_at, end)

    def test_late_start_is_inserted_before_newer_downtime_without_changing_it(self):
        repair = DowntimeReason.objects.get(name='Ремонт')
        newer = DowntimeEvent.objects.create(
            equipment=self.truck, employee=self.driver, reason=repair,
            started_at=self.base + timedelta(hours=2),
        )
        event = self.driver_event('history-start', 'driver.downtime.started', 1,
                                  occurred_at=self.base + timedelta(hours=1),
                                  payload={'reason_id': self.wait.pk})
        result = self.sync_driver([event]).json()['results'][0]
        self.assertEqual(result['status'], 'accepted', result)
        older = DowntimeEvent.objects.get(pk=result['server_ids']['downtime_event_id'])
        newer.refresh_from_db()
        self.assertIsNone(newer.ended_at)
        self.assertEqual(older.started_at, self.base + timedelta(hours=1))
        self.assertEqual(older.ended_at, newer.started_at)

    def test_late_free_bucket_selection_does_not_release_newer_trip(self):
        trip = self.trip(loaded_at=self.base + timedelta(minutes=20))
        result = self.sync_driver([self.select_event(occurred_at=self.base + timedelta(minutes=10))]).json()['results'][0]
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD, result)
        self.assertIsNone(trip.operationally_closed_at)
        self.assertFalse(TripClientAction.objects.filter(trip=trip).exists())

    def opening(self, name, at):
        return open_driver_shift_from_device(
            employee=self.driver, equipment=self.truck, shift_type='day',
            readings={'start_fuel': Decimal('100'), 'start_mileage': Decimal('1000'),
                      'start_engine_hours': Decimal('100')},
            client_action_id=name, opened_at=at,
        )[0]

    def test_reverse_opening_keeps_newer_shift_open_and_closes_historical_at_its_boundary(self):
        EmployeeShift.objects.filter(pk=self.truck_shift.pk).update(closed_at=self.base + timedelta(minutes=1))
        newer = self.opening('history-open-b', self.base + timedelta(hours=2))
        newer_wait = DowntimeEvent.objects.create(
            equipment=self.truck, employee=self.driver, reason=self.wait,
            started_at=self.base + timedelta(hours=2, minutes=5),
        )
        older = self.opening('history-open-a', self.base + timedelta(hours=1))
        newer.refresh_from_db()
        newer_wait.refresh_from_db()
        self.assertIsNone(newer.closed_at)
        self.assertIsNone(newer_wait.ended_at)
        self.assertEqual(older.closed_at, newer.opened_at)

    def close_event(self, *, at, ident='history-own-close'):
        return self.driver_event(ident, 'driver.shift.closed', 1, occurred_at=at,
                                 payload={'end_fuel': '80', 'end_mileage': '1020', 'end_engine_hours': '102'})

    def test_r21_own_earlier_end_and_readings_replace_service_projection_keep_history(self):
        service_time = self.base + timedelta(hours=3)
        own_time = service_time - timedelta(minutes=30)
        EmployeeShift.objects.filter(pk=self.truck_shift.pk).update(
            closed_at=service_time, closed_by=self.operator, is_service_closed=True,
            service_close_kind='coordinated', service_close_note='исходная служебная запись',
        )
        event = self.close_event(at=own_time)
        result = self.sync_driver([event]).json()['results'][0]
        self.assertEqual(result['status'], 'accepted', result)
        self.truck_shift.refresh_from_db()
        self.assertEqual(self.truck_shift.closed_at, own_time)
        self.assertEqual(self.truck_shift.end_mileage, Decimal('1020'))
        action = ShiftClientAction.objects.get(client_action_id=event['event_id'])
        self.assertEqual(action.response_payload['previous_service_close']['closed_at'], service_time.isoformat())
        self.assertEqual(action.response_payload['previous_service_close']['closed_by_id'], self.operator.pk)
        self.assertEqual(self.sync_driver([event]).json()['results'][0]['status'], 'deduplicated')

    def test_sent_live_hint_does_not_change_reliable_original_time(self):
        at = self.base + timedelta(hours=1)
        event = self.close_event(at=at)
        event['sent_live'] = True
        normalized = normalize_offline_event(event, role_code='driver', device_id='history-device')
        self.assertEqual(normalized['occurred_at'], at)
        self.assertFalse(normalized['clock_adjusted'])

    def carryover(self):
        trip = self.trip(is_carryover=True)
        closed_at = self.base + timedelta(minutes=30)
        EmployeeShift.objects.filter(pk=self.truck_shift.pk).update(closed_at=closed_at)
        driver, access, shift = self.create_registered_driver_shift(
            self.truck, full_name='Водитель сменщик', access_code='history-d2')
        EmployeeShift.objects.filter(pk=shift.pk).update(opened_at=closed_at)
        shift.refresh_from_db()
        return trip, driver, access, shift

    def test_carryover_visible_to_successor_and_survives_background_expiry(self):
        trip, driver, access, shift = self.carryover()
        self.assertTrue(Trip.objects.filter(pk=trip.pk).filter(trip_driver_control_filter(shift)).exists())
        reconcile_expired_manual_trips(now=self.base + timedelta(hours=4))
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)

    def test_successor_unload_preserves_one_trip_and_original_loading_authorship(self):
        trip, driver, access, shift = self.carryover()
        original_driver = self.driver
        original_shift = self.truck_shift
        self.driver, self.driver_access, self.truck_shift = driver, access, shift
        session = self.driver_client.session
        session['employee_access_id'] = access.pk
        session.save()
        at = self.base + timedelta(hours=1)
        event = self.driver_event('history-carryover-unload', 'driver.trip.unloaded', 1,
                                  occurred_at=at, payload={'trip_id': trip.pk})
        event['trip_id'] = trip.pk
        result = self.sync_driver([event]).json()['results'][0]
        self.assertEqual(result['status'], 'accepted', result)
        trip.refresh_from_db()
        self.assertEqual(Trip.objects.count(), 1)
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.driver_id, original_driver.pk)
        self.assertEqual(trip.driver_control_shift_id, original_shift.pk)
        self.assertEqual(trip.unloading_shift_id, shift.pk)
        self.assertEqual(trip.completed_at, at)
        self.assertEqual(TripClientAction.objects.get(client_action_id=event['event_id']).actor_id, driver.pk)

    def test_late_unload_does_not_close_a_later_unloading_wait(self):
        from trips.views import finalize_trip_unloaded
        trip = self.trip()
        reason = DowntimeReason.objects.get(name='Ожидание разгрузки')
        newer = DowntimeEvent.objects.create(
            equipment=self.truck, employee=self.driver, reason=reason,
            started_at=self.base + timedelta(hours=2),
        )
        finalize_trip_unloaded(trip, driver=self.driver, unloading_shift=self.truck_shift,
                               occurred_at=self.base + timedelta(hours=1))
        newer.refresh_from_db()
        self.assertIsNone(newer.ended_at)
