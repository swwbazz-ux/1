"""Адресные воспроизведения F4/F5/F6: факт старой истории не меняет новую."""
from datetime import timedelta
from decimal import Decimal
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.apps import apps
from django.core.management.color import no_style
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import test_free_bucket_sync as fixtures
from core.models import OfflineFieldEvent
from assignments.models import HaulAssignment
from core.offline_sync import normalize_offline_event
from downtimes.models import DowntimeEvent, DowntimeReason
from shifts.models import EmployeeShift, ShiftClientAction
from shifts.services import open_driver_shift_from_device
from trips.manual_loading import reconcile_expired_manual_trips, trip_driver_control_filter
from trips.models import FreeBucketAcceptance, FreeBucketAcceptanceStatus, Trip, TripClientAction, TripStatus


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class HistoricalBoundariesTests(TestCase):
    create_registered_driver_shift = fixtures.FreeBucketServerIntegrationTests.create_registered_driver_shift
    driver_event = fixtures.FreeBucketServerIntegrationTests.driver_event
    select_event = fixtures.FreeBucketServerIntegrationTests.select_event
    sync = fixtures.FreeBucketServerIntegrationTests.sync
    sync_driver = fixtures.FreeBucketServerIntegrationTests.sync_driver
    event = fixtures.FreeBucketServerIntegrationTests.event

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
        self.assertEqual(result['status'], 'accepted', result)
        self.assertTrue(result['historical_superseded'])
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD, result)
        self.assertIsNone(trip.operationally_closed_at)
        self.assertFalse(TripClientAction.objects.filter(trip=trip).exists())

    def test_late_selection_leaves_newer_used_free_bucket_reservation_intact(self):
        trip = self.trip(loaded_at=self.base + timedelta(minutes=20))
        current = FreeBucketAcceptance.objects.create(
            client_acceptance_id='history-current-right', truck=self.truck, excavator=self.excavator,
            operator=self.operator, loading_shift=self.shift,
            occurred_at=self.base + timedelta(minutes=15), accepted_at=self.base + timedelta(minutes=15),
            status=FreeBucketAcceptanceStatus.USED, used_trip=trip, used_at=trip.loaded_at,
        )
        result = self.sync_driver([self.select_event(occurred_at=self.base + timedelta(minutes=10))]).json()['results'][0]
        self.assertEqual(result['status'], 'accepted', result)
        current.refresh_from_db()
        self.assertEqual(current.status, FreeBucketAcceptanceStatus.USED)
        self.assertIsNone(current.closed_at)
        self.assertIsNone(current.cancelled_at)

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

    def test_reverse_three_openings_reconstruct_only_proven_predecessor_boundary(self):
        first = self.truck_shift
        wait = DowntimeEvent.objects.create(
            equipment=self.truck, employee=self.driver, reason=self.wait,
            started_at=self.base + timedelta(minutes=15),
        )
        newer = self.opening('history-three-b', self.base + timedelta(hours=2))
        newer_wait = DowntimeEvent.objects.create(
            equipment=self.truck, employee=self.driver, reason=self.wait,
            started_at=self.base + timedelta(hours=2, minutes=5),
        )
        middle = self.opening('history-three-a', self.base + timedelta(hours=1))
        first.refresh_from_db()
        newer.refresh_from_db()
        wait.refresh_from_db()
        newer_wait.refresh_from_db()
        self.assertEqual(first.closed_at, middle.opened_at)
        self.assertEqual(middle.closed_at, newer.opened_at)
        self.assertIsNone(newer.closed_at)
        self.assertEqual(wait.ended_at, middle.opened_at)
        self.assertIsNone(newer_wait.ended_at)

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

    def test_reverse_four_openings_preserve_boundary_provenance_transitively(self):
        first = self.truck_shift
        last = self.opening('history-four-d', self.base + timedelta(hours=3))
        third = self.opening('history-four-c', self.base + timedelta(hours=2))
        second = self.opening('history-four-b', self.base + timedelta(hours=1))
        for shift in (first, second, third, last):
            shift.refresh_from_db()
        self.assertEqual(first.closed_at, second.opened_at)
        self.assertEqual(second.closed_at, third.opened_at)
        self.assertEqual(third.closed_at, last.opened_at)
        self.assertIsNone(last.closed_at)

    def test_r21_does_not_treat_second_own_close_as_a_correction(self):
        service_time = self.base + timedelta(hours=3)
        EmployeeShift.objects.filter(pk=self.truck_shift.pk).update(
            closed_at=service_time, closed_by=self.operator, is_service_closed=True,
        )
        own_time = service_time - timedelta(minutes=30)
        self.sync_driver([self.close_event(at=own_time)])
        second = self.close_event(at=own_time - timedelta(minutes=10), ident='history-second-own-close')
        self.sync_driver([second])
        self.truck_shift.refresh_from_db()
        self.assertEqual(self.truck_shift.closed_at, own_time)
        self.assertEqual(ShiftClientAction.objects.filter(shift=self.truck_shift, action_type='driver_shift_closed').count(), 1)

    def test_r21_distrusted_clock_does_not_move_service_boundary(self):
        service_time = self.base + timedelta(hours=3)
        EmployeeShift.objects.filter(pk=self.truck_shift.pk).update(
            closed_at=service_time, closed_by=self.operator, is_service_closed=True,
        )
        event = self.close_event(at=service_time - timedelta(minutes=30))
        event['clock_unreliable'] = True
        self.sync_driver([event])
        self.truck_shift.refresh_from_db()
        self.assertEqual(self.truck_shift.closed_at, service_time)

    def test_r21_corrects_only_own_wait_at_service_boundary_and_keeps_new_repair(self):
        service_time = self.base + timedelta(hours=3)
        own_time = service_time - timedelta(minutes=30)
        EmployeeShift.objects.filter(pk=self.truck_shift.pk).update(
            closed_at=service_time, closed_by=self.operator, is_service_closed=True,
        )
        wait = DowntimeEvent.objects.create(
            equipment=self.truck, employee=self.driver, reason=self.wait,
            started_at=self.base + timedelta(hours=2), ended_at=service_time,
        )
        repair = DowntimeEvent.objects.create(
            equipment=self.truck, employee=self.driver, reason=DowntimeReason.objects.get(name='Ремонт'),
            started_at=self.base + timedelta(hours=3, minutes=5),
        )
        self.sync_driver([self.close_event(at=own_time)])
        wait.refresh_from_db()
        repair.refresh_from_db()
        self.assertEqual(wait.ended_at, own_time)
        self.assertIsNone(repair.ended_at)

    def test_excavator_r21_preserves_service_history_and_earlier_own_readings(self):
        service_time = self.base + timedelta(hours=3)
        own_time = service_time - timedelta(minutes=30)
        EmployeeShift.objects.filter(pk=self.shift.pk).update(
            closed_at=service_time, closed_by=self.driver, is_service_closed=True,
        )
        event = self.event('history-eo-own-close', 'excavator.shift.closed', 1,
                           occurred_at=own_time, payload={'fuel': '90', 'engine_hours': '1202'})
        result = self.sync([event]).json()['results'][0]
        self.assertEqual(result['status'], 'accepted', result)
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.closed_at, own_time)
        self.assertEqual(self.shift.end_engine_hours, Decimal('1202'))
        self.assertEqual(ShiftClientAction.objects.get(client_action_id=event['event_id']).response_payload[
            'previous_service_close']['closed_at'], service_time.isoformat())

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
        self.assertEqual(self.sync_driver([event]).json()['results'][0]['status'], 'deduplicated')
        from reports.driver_shift_timeline import _trip_quality_flags
        self.assertNotIn('trip_driver_unloading_shift_mismatch', _trip_quality_flags(trip))

    def test_carryover_does_not_grant_control_to_overlapping_or_other_truck_shift(self):
        trip, driver, access, shift = self.carryover()
        shift.opened_at = self.base + timedelta(minutes=10)
        self.assertFalse(Trip.objects.filter(pk=trip.pk).filter(trip_driver_control_filter(shift)).exists())
        shift.opened_at = self.base + timedelta(hours=1)
        shift.equipment_id = self.other_truck.pk
        self.assertFalse(Trip.objects.filter(pk=trip.pk).filter(trip_driver_control_filter(shift)).exists())

    def test_direct_successor_unload_uses_current_shift(self):
        trip, driver, access, shift = self.carryover()
        session = self.driver_client.session
        session['employee_access_id'] = access.pk
        session.save()
        response = self.driver_client.post(reverse('driver_complete_trip', args=[trip.pk]),
                                           {'client_action_id': 'history-direct-d2'},
                                           HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.driver_id, self.driver.pk)
        self.assertEqual(trip.unloading_shift_id, shift.pk)

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

    def test_late_unload_does_not_reconcile_next_excavator_wait(self):
        from trips.views import EXCAVATOR_AUTO_DOWNTIME_COMMENT, finalize_trip_unloaded
        trip = self.trip()
        newer = DowntimeEvent.objects.create(
            equipment=self.excavator, employee=self.operator, reason=self.reason,
            started_at=self.base + timedelta(hours=2), comment=EXCAVATOR_AUTO_DOWNTIME_COMMENT,
        )
        finalize_trip_unloaded(trip, driver=self.driver, unloading_shift=self.truck_shift,
                               occurred_at=self.base + timedelta(hours=1))
        newer.refresh_from_db()
        self.assertIsNone(newer.ended_at)

    def test_r19_face_settings_during_manual_repair_do_not_start_transfer(self):
        repair = DowntimeEvent.objects.create(
            equipment=self.excavator, employee=self.operator,
            reason=DowntimeReason.objects.get(name='Ремонт'), started_at=self.base,
        )
        response = self.client.post(reverse('excavator_work_settings'), data=json.dumps({
            'client_action_id': 'history-face-settings', 'rock_type_id': self.rock.pk,
            'dump_point_ids': [self.dump_point.pk], 'loading_horizon': '222', 'loading_block': '7',
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        repair.refresh_from_db()
        self.assertIsNone(repair.ended_at)
        self.assertFalse(DowntimeEvent.objects.filter(equipment=self.excavator, reason__name='Перегон экскаватора').exists())

    def test_r19_explicit_transfer_keeps_original_time_without_changed_face(self):
        at = self.base + timedelta(hours=1)
        event = self.event('history-explicit-transfer', 'excavator.downtime.started', 1,
                           occurred_at=at,
                           payload={'reason_id': DowntimeReason.objects.get(name='Перегон экскаватора').pk})
        result = self.sync([event]).json()['results'][0]
        self.assertEqual(result['status'], 'accepted', result)
        transfer = DowntimeEvent.objects.get(pk=result['server_ids']['downtime_event_id'])
        self.assertEqual(transfer.started_at, at)

    def test_late_old_shift_handover_does_not_mark_new_shift_trip_as_carryover(self):
        from shifts.services import handover_other_role_shift
        old = self.truck_shift
        boundary = self.base + timedelta(hours=1)
        EmployeeShift.objects.filter(pk=old.pk).update(closed_at=boundary)
        driver, access, new_shift = self.create_registered_driver_shift(
            self.truck, full_name='Новая смена', access_code='history-new-shift')
        EmployeeShift.objects.filter(pk=new_shift.pk).update(opened_at=boundary)
        new_trip = self.trip(driver=driver, driver_control_shift=new_shift,
                             loaded_at=boundary + timedelta(minutes=10))
        handover_other_role_shift(old, closed_by=self.driver, closed_at=boundary)
        new_trip.refresh_from_db()
        self.assertFalse(new_trip.is_carryover)

    def test_earlier_second_ordinary_stop_cannot_rewrite_own_stop(self):
        end = self.base + timedelta(hours=2)
        event = DowntimeEvent.objects.create(
            equipment=self.truck, employee=self.driver, reason=self.wait,
            started_at=self.base + timedelta(hours=1),
        )
        self.sync_driver([self.stop(event, at=end)])
        self.sync_driver([self.stop(event, at=end - timedelta(minutes=10), ident='history-second-stop')])
        event.refresh_from_db()
        self.assertEqual(event.ended_at, end)


    def _complete_manual_carryover_as_successor(self, event_type):
        trip, driver, access, shift = self.carryover()
        original_driver, original_shift = self.driver, self.truck_shift
        TripClientAction.objects.create(
            trip=trip, action_type='driver_manual_loaded', actor=self.driver,
            client_action_id='review-manual-D1-load',
        )
        self.driver, self.driver_access, self.truck_shift = driver, access, shift
        session = self.driver_client.session
        session['employee_access_id'] = access.pk
        session.save()
        event = self.driver_event(
            'review-D2-manual', event_type, 1, occurred_at=self.base + timedelta(hours=1),
            payload={'trip_id': trip.pk, 'truck_id': self.truck.pk,
                     'excavator_id': self.excavator.pk, 'manual_control': True},
        )
        event['trip_id'] = trip.pk
        result = self.sync_driver([event]).json()['results'][0]
        self.assertEqual(result['status'], 'accepted', result)
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.driver_id, original_driver.pk)
        self.assertEqual(trip.driver_control_shift_id, original_shift.pk)
        self.assertEqual(trip.unloading_shift_id, shift.pk)
        self.assertEqual(Trip.objects.count(), 1)
        self.assertEqual(self.sync_driver([event]).json()['results'][0]['status'], 'deduplicated')

    def test_manual_carryover_has_successor_regular_unload(self):
        self._complete_manual_carryover_as_successor('driver.trip.unloaded')

    def test_manual_carryover_has_successor_manual_completion(self):
        self._complete_manual_carryover_as_successor('driver.trip.manual_completed')

    def test_original_driver_direct_historical_unload_remains_available(self):
        trip, driver, access, shift = self.carryover()
        Trip.objects.filter(pk=trip.pk).update(created_at=self.base)
        response = self.driver_client.post(reverse('driver_complete_trip', args=[trip.pk]), {'client_action_id': 'review-D1-late-direct', 'occurred_at': (self.base + timedelta(minutes=20)).isoformat()}, HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200)
        trip.refresh_from_db()
        self.assertEqual(trip.unloading_shift_id, self.truck_shift.pk)
        self.assertEqual(trip.driver_id, self.driver.pk)
        self.assertEqual(trip.completed_at, self.base + timedelta(minutes=20))

    def test_late_load_after_handover_is_carryover(self):
        boundary = self.base + timedelta(minutes=30)
        EmployeeShift.objects.filter(pk=self.truck_shift.pk).update(closed_at=boundary, is_service_closed=True)
        driver, access, shift = self.create_registered_driver_shift(self.truck, full_name='Review late load D2', access_code='review-late-D2')
        EmployeeShift.objects.filter(pk=shift.pk).update(opened_at=boundary)
        shift.refresh_from_db()
        HaulAssignment.objects.filter(pk=self.assignment.pk).update(assigned_at=self.base)
        event = self.event('review-late-auto-load', 'excavator.trip.loaded', 1, occurred_at=self.base + timedelta(minutes=5), local_trip_id='review-late-load', payload={'truck_id': self.truck.pk, 'assignment_id': self.assignment.pk, 'dump_point_id': self.dump_point.pk, 'rock_type_id': self.rock.pk, 'manual_control': False})
        result = self.sync([event]).json()['results'][0]
        self.assertEqual(result['status'], 'accepted', result)
        trip = Trip.objects.get(pk=result['server_ids']['trip_id'])
        visible = Trip.objects.filter(pk=trip.pk).filter(trip_driver_control_filter(shift)).exists()
        self.assertTrue(visible)
        self.assertEqual(trip.driver_id, self.driver.pk)
        self.assertEqual(trip.driver_control_shift_id, self.truck_shift.pk)
        self.assertEqual(trip.loaded_at, self.base + timedelta(minutes=5))
        self.assertTrue(trip.is_carryover)

    def test_late_ordinary_stop_does_not_shorten_prior_direct_own_stop(self):
        at = self.base + timedelta(hours=2)
        downtime = DowntimeEvent.objects.create(equipment=self.truck, employee=self.driver, reason=self.wait, started_at=self.base + timedelta(hours=1))
        with patch('users.views.timezone.now', return_value=at):
            response = self.driver_client.post(reverse('driver_downtime_action'), data=json.dumps({'action': 'close'}), content_type='application/json', HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        downtime.refresh_from_db()
        self.assertEqual(downtime.ended_at, at)
        result = self.sync_driver([self.stop(downtime, at=at - timedelta(minutes=20), ident='review-earlier-second-stop')]).json()['results'][0]
        downtime.refresh_from_db()
        self.assertEqual(downtime.ended_at, at)

@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class HistoricalCarryoverPostgresTests(TransactionTestCase):
    """Два реальных соединения; SQLite результатом этой приёмки не является."""
    create_registered_driver_shift = HistoricalBoundariesTests.create_registered_driver_shift
    trip = HistoricalBoundariesTests.trip
    carryover = HistoricalBoundariesTests.carryover
    driver_event = HistoricalBoundariesTests.driver_event

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Нужен PostgreSQL: два независимых соединения и row locks.')
        with connection.cursor() as cursor:
            for sql in connection.ops.sequence_reset_sql(no_style(), apps.get_models()):
                cursor.execute(sql)
        for name in ('Ожидание самосвалов', 'Ожидание погрузки'):
            DowntimeReason.objects.get_or_create(name=name)
        HistoricalBoundariesTests.setUp(self)

    def test_parallel_duplicate_successor_unloads_after_expiry_keep_one_load_and_authors(self):
        """Два запроса; порядок expiry/unload между потоками не фиксирован."""
        from core.offline_sync import process_offline_batch
        from users.models import EmployeeAccess

        trip, driver, access, shift = self.carryover()
        original_driver, original_shift = self.driver, self.truck_shift
        original_loading_shift, original_operator = trip.loading_shift_id, trip.excavator_operator_id
        self.driver, self.driver_access, self.truck_shift = driver, access, shift
        event = self.driver_event('pg-carryover-unload', 'driver.trip.unloaded', 1,
                                  occurred_at=self.base + timedelta(hours=1), payload={'trip_id': trip.pk})
        event['trip_id'] = trip.pk
        barrier = Barrier(2)

        def submit(_):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pid = cursor.fetchone()[0]
                actor_access = EmployeeAccess.objects.select_related('employee', 'role').get(pk=access.pk)
                barrier.wait(timeout=10)
                reconcile_expired_manual_trips(now=self.base + timedelta(hours=4))
                results = process_offline_batch(actor_access, role_code='driver',
                                                 device_id='pg-history-device', events=[event])
                return pid, results
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, (1, 2)))
        self.assertEqual(len({pid for pid, _ in results}), 2)
        statuses = [result[0]['status'] for _, result in results]
        self.assertEqual(sorted(statuses), ['accepted', 'deduplicated'], results)
        trip.refresh_from_db()
        self.assertEqual(Trip.objects.count(), 1)
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.driver_id, original_driver.pk)
        self.assertEqual(trip.driver_control_shift_id, original_shift.pk)
        self.assertEqual(trip.unloading_shift_id, shift.pk)
        self.assertEqual(trip.loading_shift_id, original_loading_shift)
        self.assertEqual(trip.excavator_operator_id, original_operator)
        actions = TripClientAction.objects.filter(trip=trip, action_type='trip_unloaded')
        self.assertEqual(actions.count(), 1)
        self.assertEqual(actions.get().actor_id, driver.pk)
        receipts = OfflineFieldEvent.objects.filter(event_id=event['event_id'])
        self.assertEqual(receipts.count(), 1)
        receipt = receipts.get()
        self.assertEqual(receipt.actor_id, driver.pk)
        self.assertEqual(receipt.access_id, access.pk)
        self.assertEqual(receipt.trip_id, trip.pk)
        self.assertEqual(receipt.shift_id, shift.pk)
        self.assertEqual(receipt.status, 'accepted')
