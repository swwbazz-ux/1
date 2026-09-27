import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from core.models import OfflineFieldEvent
from core.test_offline_sync import OfflineEventSyncTests
from references.models import DumpPoint
from reports.dispatcher_shift_forms import build_dispatcher_shift_report
from reports.driver_shift_passport_snapshots import _trip_manifest_item
from reports.views import update_shift_report_trip
from core.production_time import production_shift_context
from trips.excavator_hourly_report import build_excavator_hourly_report
from trips.models import DispatcherActionLog, Trip, TripClientAction, TripStatus
from trips.test_manual_loading import ManualLoadingTests
from trips import manual_loading as manual_loading_module
from users.models import Employee, EmployeeAccess, Role


def point_state(trip):
    return {
        'trip_id': trip.pk,
        'status': trip.status,
        'dump_point_id': trip.dump_point_id,
        'assigned_dump_point_id': trip.assigned_dump_point_id,
        'actual_dump_point_id': trip.actual_dump_point_id,
        'excavator_operator_id': trip.excavator_operator_id,
        'driver_id': trip.driver_id,
        'loaded_at': trip.loaded_at.isoformat() if trip.loaded_at else None,
        'completed_at': trip.completed_at.isoformat() if trip.completed_at else None,
    }


def evidence(label, payload):
    print('P28_EVIDENCE ' + json.dumps({'label': label, **payload}, ensure_ascii=False, default=str, sort_keys=True))


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class P28ExistingHandlersTraceTests(TestCase):
    """Only public/current handlers are used for production actions."""

    load_event = OfflineEventSyncTests.load_event
    sync = OfflineEventSyncTests.sync
    driver_client = OfflineEventSyncTests.driver_client
    driver_manual_event = OfflineEventSyncTests.driver_manual_event
    create_registered_driver_shift = OfflineEventSyncTests.create_registered_driver_shift

    def setUp(self):
        OfflineEventSyncTests.setUp(self)
        now = timezone.now()
        self.shift.opened_at = now - timedelta(minutes=15)
        self.shift.save(update_fields=['opened_at'])
        self.truck_shift.opened_at = now - timedelta(minutes=15)
        self.truck_shift.save(update_fields=['opened_at'])
        self.p2 = DumpPoint.objects.create(name='P28 P2')
        self.p3 = DumpPoint.objects.create(name='P28 P3')

    def _driver_event(self, *, event_id, event_type, sequence, occurred_at, trip, payload, depends_on=()):
        return {
            'event_id': event_id,
            'event_type': event_type,
            'format_version': 1,
            'occurred_at': occurred_at.isoformat(),
            'sequence': sequence,
            'depends_on': list(depends_on),
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'trip_id': trip.id,
            'payload': payload,
        }

    def test_p1_driver_p2_unload_dispatcher_p3_and_report_projections(self):
        loaded_at = timezone.now() - timedelta(minutes=2)
        load = self.load_event('p28-load-x', 1, occurred_at=loaded_at)
        load['payload']['manual_control'] = False
        load_result = self.sync([load]).json()['results'][0]
        self.assertEqual(load_result['status'], 'accepted', load_result)
        trip = Trip.objects.get(pk=load_result['server_ids']['trip_id'])
        evidence('01_loaded_p1', {'trip': point_state(trip), 'receipt': load_result})

        changed_at = loaded_at + timedelta(minutes=1)
        point_event = self._driver_event(
            event_id='p28-driver-p2', event_type='driver.trip.dump_point_changed', sequence=1,
            occurred_at=changed_at, trip=trip,
            payload={
                'trip_id': trip.id,
                'dump_point_id': self.p2.id,
                'expected_actual_dump_point_id': self.dump_point.id,
            },
        )
        point_result = self.sync(
            [point_event], client=self.driver_client(), role_code='driver', device_id='p28-driver',
        ).json()['results'][0]
        self.assertEqual(point_result['status'], 'accepted', point_result)
        trip.refresh_from_db()
        self.assertEqual((trip.assigned_dump_point_id, trip.actual_dump_point_id, trip.dump_point_id),
                         (self.dump_point.id, self.p2.id, self.p2.id))
        evidence('02_driver_selected_p2', {'trip': point_state(trip), 'receipt': point_result})

        unload_at = changed_at + timedelta(seconds=1)
        unload_event = self._driver_event(
            event_id='p28-unload-x', event_type='driver.trip.unloaded', sequence=2,
            occurred_at=unload_at, trip=trip, payload={'trip_id': trip.id},
            depends_on=(point_event['event_id'],),
        )
        unload_result = self.sync(
            [unload_event], client=self.driver_client(), role_code='driver', device_id='p28-driver',
        ).json()['results'][0]
        self.assertEqual(unload_result['status'], 'accepted', unload_result)
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.completed_at, unload_at)
        evidence('03_unloaded_p2', {'trip': point_state(trip), 'receipt': unload_result})

        ctx = production_shift_context(loaded_at)
        self.shift.shift_type = ctx.shift_type
        self.shift.save(update_fields=['shift_type'])
        dispatcher_role, _ = Role.objects.get_or_create(code='dispatcher', defaults={'name': 'Диспетчер P28'})
        dispatcher = Employee.objects.create(full_name='Диспетчер P28')
        access = EmployeeAccess.objects.create(
            employee=dispatcher, role=dispatcher_role, access_code='p28-dispatcher',
            status=EmployeeAccess.Status.ACTIVATED, is_active=True,
        )
        request = RequestFactory().post('/reports/p28/', data={
            'trip_id': str(trip.id),
            'correction_reason': 'P28-C1 доказательная корректировка',
            'volume_m3': str(trip.volume_m3 or Decimal('10.00')),
            'transport_distance_km': str(trip.transport_distance_km or Decimal('1.00')),
            'loading_horizon': trip.loading_horizon or '',
            'loading_block': trip.loading_block or '',
            'rock_type_id': str(trip.rock_type_id),
            'actual_dump_point_id': str(self.p3.id),
            'downtime_text': trip.downtime_text or '',
            'note': trip.note or '',
        })
        request.session = {}
        setattr(request, '_messages', FallbackStorage(request))
        with patch('reports.views.reports_mutation_role_barrier', return_value=None):
            response = update_shift_report_trip(
                request, access, ctx.production_date, ctx.shift_type, 'trucks',
            )
        self.assertEqual(response.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual((trip.assigned_dump_point_id, trip.actual_dump_point_id, trip.dump_point_id),
                         (self.dump_point.id, self.p3.id, self.p2.id))
        action = DispatcherActionLog.objects.get(action_type='report_source_correction', trip=trip)

        hourly = build_excavator_hourly_report(self.excavator, captured_at=timezone.now() + timedelta(seconds=1))
        hourly_rows = [row for hour in hourly['hours'] for row in hour['rows']]
        report = build_dispatcher_shift_report(ctx.production_date, ctx.shift_type)
        manifest = _trip_manifest_item(trip)
        evidence('04_dispatcher_corrected_p3', {
            'trip': point_state(trip),
            'dispatcher_action': {
                'id': action.id, 'actor_id': action.actor_id, 'created_at': action.created_at,
                'reason': action.reason,
            },
            'report_188_rows': hourly_rows,
            'dispatcher_excavation_dump_points': sorted({
                row.get('dump_point') for row in report['excavation_rows'] if row.get('dump_point')
            }),
            'passport_manifest': {
                key: manifest[key] for key in (
                    'dump_point_id', 'assigned_dump_point_id', 'actual_dump_point_id',
                    'status', 'excavator_operator_id', 'driver_id', 'completed_at',
                )
            },
        })
        self.assertTrue(any(row['dump_point_id'] == self.dump_point.id for row in hourly_rows))
        self.assertEqual(manifest['actual_dump_point_id'], self.p3.id)

    def test_driver_manual_handler_exposes_absence_of_manual_loading_model_flag(self):
        event = self.driver_manual_event('p28-driver-manual', 1)
        result = self.sync(
            [event], client=self.driver_client(), role_code='driver', device_id='p28-driver-manual',
        ).json()['results'][0]
        self.assertEqual(result['status'], 'accepted', result)
        trip = Trip.objects.get(pk=result['server_ids']['trip_id'])
        # The release handler is real, but the Trip model at this SHA has no persisted
        # `manual_loading` field. This is an observed schema fact, not a proposed change.
        self.assertFalse(hasattr(trip, 'manual_loading'))
        evidence('05_driver_manual_created', {
            'trip': point_state(trip),
            'manual_loading_field_present': hasattr(trip, 'manual_loading'),
            'driver_control_shift_id': trip.driver_control_shift_id,
        })


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class P28RestoreBranchesTests(TestCase):
    send = ManualLoadingTests.send
    presence = ManualLoadingTests.presence
    create_registered_driver_shift = ManualLoadingTests.create_registered_driver_shift

    def setUp(self):
        ManualLoadingTests.setUp(self)

    def _cancel(self, trip, action):
        return self.client.post(
            __import__('django.urls').urls.reverse('excavator_truck_loaded_cancel'),
            json.dumps({
                'client_action_id': action, 'trip_id': trip.id, 'truck_id': trip.truck_id,
                'dump_point_id': trip.dump_point_id,
            }),
            content_type='application/json',
        )

    def test_open_x_is_linked_and_restored(self):
        first = self.send(action='p28-x')
        x = Trip.objects.get(pk=first.json()['trip_id'])
        second = self.send(previous=x, action='p28-y')
        self.assertEqual(second.status_code, 200, second.content)
        y = Trip.objects.get(pk=second.json()['trip_id'])
        x.refresh_from_db()
        self.assertEqual(x.superseded_by_id, y.id)
        linked_before_cancel = x.superseded_by_id
        cancelled = self._cancel(y, 'p28-cancel-y')
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        x.refresh_from_db(); y.refresh_from_db()
        self.assertEqual(x.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertEqual(y.status, TripStatus.CANCELLED)
        evidence('06_linked_x_restored', {
            'x': point_state(x), 'y': point_state(y),
            'superseded_by_before_cancel': linked_before_cancel,
            'superseded_by_after_cancel': x.superseded_by_id,
        })

    def test_reconciled_x_has_no_link_and_is_not_restored(self):
        first = self.send(action='p28-expired-x')
        x = Trip.objects.get(pk=first.json()['trip_id'])
        stale = timezone.now() - timedelta(minutes=6)
        Trip.objects.filter(pk=x.pk).update(created_at=stale, loaded_at=stale)
        cleared = manual_loading_module.reconcile_expired_manual_trips(now=timezone.now())
        self.assertIn(x.id, cleared)
        x.refresh_from_db()
        self.assertEqual(x.status, TripStatus.UNCONTROLLED)
        second = self.send(action='p28-after-expiry-y')
        self.assertEqual(second.status_code, 200, second.content)
        y = Trip.objects.get(pk=second.json()['trip_id'])
        x.refresh_from_db()
        self.assertIsNone(x.superseded_by_id)
        cancelled = self._cancel(y, 'p28-cancel-after-expiry-y')
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        x.refresh_from_db(); y.refresh_from_db()
        self.assertEqual(x.status, TripStatus.UNCONTROLLED)
        evidence('07_expired_x_not_restored', {'x': point_state(x), 'y': point_state(y), 'next_reconcile': manual_loading_module.reconcile_expired_manual_trips(now=timezone.now())})
