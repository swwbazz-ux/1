import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.messages.storage.fallback import FallbackStorage
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
from core.models import OfflineFieldEvent
from core.test_free_bucket_sync import FreeBucketServerIntegrationTests
from core.test_offline_sync import OfflineEventSyncTests
from references.models import DumpPoint, Equipment
from reports.views import update_shift_report_trip
from trips import manual_loading as manual_loading_module
from trips import tests as trip_fixtures
from trips.test_manual_loading import ManualLoadingTests
from trips.free_bucket import free_bucket_acceptance_expires_at, reconcile_expired_free_bucket_acceptances
from trips.models import (
    DispatcherActionLog,
    FreeBucketAcceptance,
    FreeBucketAcceptanceStatus,
    Trip,
    TripStatus,
)
from users.models import Employee, EmployeeAccess, Role


def evidence(label, **payload):
    print('P28_R1_EVIDENCE ' + json.dumps(
        {'label': label, **payload}, ensure_ascii=False, default=str, sort_keys=True,
    ))


def trip_state(trip):
    return {
        'id': trip.id,
        'status': trip.status,
        'assigned_dump_point_id': trip.assigned_dump_point_id,
        'actual_dump_point_id': trip.actual_dump_point_id,
        'dump_point_id': trip.dump_point_id,
        'superseded_by_id': trip.superseded_by_id,
        'driver_participation_recorded': trip.driver_participation_recorded,
    }


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class P28R1FreeBucketTests(TestCase):
    create_registered_driver_shift = trip_fixtures.ExcavatorWorkServerIntegrationTests.create_registered_driver_shift
    event = FreeBucketServerIntegrationTests.event
    sync = FreeBucketServerIntegrationTests.sync
    accept_event = FreeBucketServerIntegrationTests.accept_event
    load_event = FreeBucketServerIntegrationTests.load_event
    driver_event = FreeBucketServerIntegrationTests.driver_event
    sync_driver = FreeBucketServerIntegrationTests.sync_driver

    def setUp(self):
        FreeBucketServerIntegrationTests.setUp(self)

    def _create_used(self, *, prefix, loaded_at):
        accepted = self.accept_event(event_id=f'{prefix}-accept')
        accepted['occurred_at'] = (loaded_at - timedelta(seconds=1)).isoformat()
        loaded = self.load_event(
            accepted, event_id=f'{prefix}-load', occurred_at=loaded_at,
        )
        result = self.sync([loaded, accepted], device_id=f'{prefix}-device').json()['results']
        by_id = {item['event_id']: item for item in result}
        self.assertEqual(by_id[loaded['event_id']]['status'], 'accepted', by_id)
        trip = Trip.objects.get(pk=by_id[loaded['event_id']]['server_ids']['trip_id'])
        acceptance = FreeBucketAcceptance.objects.get(used_trip=trip)
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.USED)
        return trip, acceptance, loaded

    def _ordinary_offline(self, *, event_id, at):
        event = self.event(
            event_id, 'excavator.trip.loaded', 20, occurred_at=at,
            local_trip_id=f'local-{event_id}',
            payload={
                'truck_id': self.truck.id,
                'assignment_id': self.assignment.id,
                'dump_point_id': self.dump_point.id,
                'rock_type_id': self.rock.id,
                'manual_control': False,
                'loading_horizon': '125',
                'loading_block': 'R1-next',
            },
        )
        return self.sync([event], device_id=f'{event_id}-device').json()['results'][0]

    def _ordinary_direct(self, *, action, at):
        payload = {
            'client_action_id': action,
            'truck_id': self.truck.id,
            'excavator_id': self.excavator.id,
            'dump_point_id': self.dump_point.id,
            'rock_type': self.rock.id,
            'assignment_id': self.assignment.id,
            'manual_control': False,
            'loading_horizon': '125',
            'loading_block': 'R1-next',
        }
        with patch('trips.views.timezone.now', return_value=at):
            return self.client.post(
                reverse('excavator_truck_loaded'), json.dumps(payload),
                content_type='application/json',
            )

    def _unload(self, trip, *, event_id, at):
        event = self.driver_event(
            event_id, 'driver.trip.unloaded', 30, occurred_at=at,
            payload={'trip_id': trip.id},
        )
        event['trip_id'] = trip.id
        return self.sync_driver([event], device_id=f'{event_id}-driver').json()['results'][0]

    def test_accepted_unload_closes_used_then_direct_ordinary_load_is_real(self):
        loaded_at = timezone.now() - timedelta(minutes=2)
        trip, acceptance, _ = self._create_used(prefix='r1-accepted-direct', loaded_at=loaded_at)
        assignment_id = self.assignment.id
        unload = self._unload(trip, event_id='r1-accepted-direct-unload', at=loaded_at + timedelta(minutes=1))
        self.assertEqual(unload['status'], 'accepted', unload)
        trip.refresh_from_db(); acceptance.refresh_from_db(); self.assignment.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CLOSED)
        self.assertEqual(self.assignment.id, assignment_id)
        response = self._ordinary_direct(action='r1-after-accepted-direct', at=loaded_at + timedelta(minutes=2))
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json().get('load_block_reason_code'), 'post_unload_cooldown')
        evidence('accepted_unload_then_direct', unload=unload, acceptance=acceptance.status,
                 old_trip=trip_state(trip), ordinary=response.json(), assignment_id=self.assignment.id)

    def test_accepted_unload_closes_used_then_offline_ordinary_load_is_real(self):
        loaded_at = timezone.now() - timedelta(minutes=2)
        trip, acceptance, _ = self._create_used(prefix='r1-accepted-offline', loaded_at=loaded_at)
        unload = self._unload(trip, event_id='r1-accepted-offline-unload', at=loaded_at + timedelta(minutes=1))
        self.assertEqual(unload['status'], 'accepted', unload)
        result = self._ordinary_offline(event_id='r1-after-accepted-offline', at=loaded_at + timedelta(minutes=2))
        self.assertEqual((result['status'], result['code']), ('conflict', 'post_unload_cooldown'))
        acceptance.refresh_from_db(); trip.refresh_from_db()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CLOSED)
        evidence('accepted_unload_then_offline', unload=unload, ordinary=result,
                 acceptance=acceptance.status, old_trip=trip_state(trip), assignment_id=self.assignment.id)

    def test_delayed_unload_direct_obstacles_before_on_and_after_used_boundary(self):
        loaded_at = timezone.now() - timedelta(minutes=4)
        trip, acceptance, _ = self._create_used(prefix='r1-delayed-direct', loaded_at=loaded_at)
        deadline = free_bucket_acceptance_expires_at(acceptance)
        before = self._ordinary_direct(action='r1-direct-before', at=deadline - timedelta(microseconds=1))
        self.assertEqual(before.status_code, 409, before.content)
        self.assertEqual(before.json().get('code'), 'free_bucket_acceptance_required')
        reconcile_expired_free_bucket_acceptances(now=deadline)
        acceptance.refresh_from_db()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CLOSED)
        on = self._ordinary_direct(action='r1-direct-on', at=deadline)
        after = self._ordinary_direct(action='r1-direct-after', at=deadline + timedelta(microseconds=1))
        self.assertEqual((on.status_code, after.status_code), (409, 409))
        self.assertNotEqual(on.json().get('code'), 'free_bucket_acceptance_required')
        self.assertNotEqual(after.json().get('code'), 'free_bucket_acceptance_required')
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        evidence('delayed_unload_direct_boundary', before=before.json(), on=on.json(), after=after.json(),
                 deadline=deadline, acceptance=acceptance.status, trip=trip_state(trip), assignment_id=self.assignment.id)

    def test_delayed_unload_offline_obstacles_before_on_and_after_used_boundary(self):
        loaded_at = timezone.now() - timedelta(minutes=4)
        trip, acceptance, _ = self._create_used(prefix='r1-delayed-offline', loaded_at=loaded_at)
        deadline = free_bucket_acceptance_expires_at(acceptance)
        before = self._ordinary_offline(event_id='r1-offline-before', at=deadline - timedelta(microseconds=1))
        self.assertEqual((before['status'], before['code']), ('conflict', 'free_bucket_acceptance_required'))
        reconcile_expired_free_bucket_acceptances(now=deadline)
        on = self._ordinary_offline(event_id='r1-offline-on', at=deadline)
        after = self._ordinary_offline(event_id='r1-offline-after', at=deadline + timedelta(microseconds=1))
        acceptance.refresh_from_db(); trip.refresh_from_db()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CLOSED)
        self.assertEqual(on['status'], 'conflict', on)
        self.assertEqual(after['status'], 'conflict', after)
        self.assertNotEqual(on.get('code'), 'free_bucket_acceptance_required')
        self.assertNotEqual(after.get('code'), 'free_bucket_acceptance_required')
        evidence('delayed_unload_offline_boundary', before=before, on=on, after=after,
                 deadline=deadline, acceptance=acceptance.status, trip=trip_state(trip), assignment_id=self.assignment.id)


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class P28R1DriverBranchesTests(TestCase):
    create_registered_driver_shift = trip_fixtures.ExcavatorWorkServerIntegrationTests.create_registered_driver_shift
    load_event = OfflineEventSyncTests.load_event
    sync = OfflineEventSyncTests.sync
    driver_client = OfflineEventSyncTests.driver_client

    def setUp(self):
        OfflineEventSyncTests.setUp(self)
        self.p2 = DumpPoint.objects.create(name='P28-R1 P2')
        self.p3 = DumpPoint.objects.create(name='P28-R1 P3')

    def _point(self, *, event_id, sequence, trip, at, dump_point, expected):
        return {
            'event_id': event_id, 'event_type': 'driver.trip.dump_point_changed',
            'format_version': 1, 'occurred_at': at.isoformat(), 'sequence': sequence,
            'depends_on': [], 'shift_id': self.truck_shift.id, 'equipment_id': self.truck.id,
            'trip_id': trip.id,
            'payload': {'trip_id': trip.id, 'dump_point_id': dump_point.id,
                        'expected_actual_dump_point_id': expected.id if expected else None},
        }

    def _driver_sync(self, event, device):
        return self.sync([event], client=self.driver_client(), role_code='driver', device_id=device).json()['results'][0]

    def _open_trip(self, event_id='r1-branch-load'):
        load = self.load_event(event_id, 1, occurred_at=timezone.now() - timedelta(minutes=2))
        load['payload']['manual_control'] = False
        result = self.sync([load]).json()['results'][0]
        self.assertEqual(result['status'], 'accepted', result)
        return Trip.objects.get(pk=result['server_ids']['trip_id'])

    def test_dump_point_refusal_registry_and_uncontrolled_branch(self):
        trip = self._open_trip()
        at = trip.loaded_at + timedelta(seconds=10)
        state_changed = self._driver_sync(self._point(
            event_id='r1-state-changed', sequence=1, trip=trip, at=at,
            dump_point=self.p2, expected=self.p3,
        ), 'r1-state-device')
        self.assertEqual((state_changed['status'], state_changed['code']), ('conflict', 'dump_point_state_changed'))

        first = self._driver_sync(self._point(
            event_id='r1-point-new', sequence=2, trip=trip, at=at + timedelta(seconds=1),
            dump_point=self.p2, expected=self.dump_point,
        ), 'r1-new-device')
        self.assertEqual(first['status'], 'accepted', first)
        stale = self._driver_sync(self._point(
            event_id='r1-point-stale', sequence=1, trip=trip, at=at + timedelta(seconds=1),
            dump_point=self.p3, expected=self.p2,
        ), 'r1-stale-device')
        self.assertEqual((stale['status'], stale['code']), ('conflict', 'stale_dump_point_change'))

        trip.status = TripStatus.UNCONTROLLED
        trip.operationally_closed_at = at + timedelta(seconds=2)
        trip.save(update_fields=['status', 'operationally_closed_at'])
        not_editable = self._driver_sync(self._point(
            event_id='r1-point-uncontrolled', sequence=3, trip=trip, at=at + timedelta(seconds=3),
            dump_point=self.p3, expected=self.p2,
        ), 'r1-uncontrolled-device')
        self.assertEqual((not_editable['status'], not_editable['code']), ('conflict', 'trip_not_editable'))
        evidence('driver_point_refusals', trip=trip_state(trip), state_changed=state_changed,
                 stale=stale, trip_not_editable=not_editable)

    def test_recorded_false_is_outside_reconcile_while_true_expires_at_boundary(self):
        now = timezone.now()
        self.truck_shift.closed_at = now - timedelta(minutes=5)
        self.truck_shift.save(update_fields=['closed_at'])
        true_trip = Trip.objects.create(
            truck=self.truck, excavator=self.excavator, rock_type=self.rock,
            dump_point=self.dump_point, assigned_dump_point=self.dump_point,
            actual_dump_point=self.dump_point, status=TripStatus.LOADED_WAITING_UNLOAD,
            loaded_at=now - timedelta(minutes=5), driver_control_shift=self.truck_shift,
            driver_participation_recorded=True,
        )
        other_truck = Equipment.objects.create(
            garage_number='R1-FALSE', equipment_type=self.truck.equipment_type,
            model=self.truck.model, is_active=True,
        )
        false_trip = Trip.objects.create(
            truck=other_truck, excavator=self.excavator, rock_type=self.rock,
            dump_point=self.dump_point, assigned_dump_point=self.dump_point,
            actual_dump_point=self.dump_point, status=TripStatus.LOADED_WAITING_UNLOAD,
            loaded_at=now - timedelta(minutes=5), driver_participation_recorded=False,
        )
        before = manual_loading_module.reconcile_expired_manual_trips(now=now - timedelta(microseconds=1))
        true_trip.refresh_from_db(); false_trip.refresh_from_db()
        self.assertEqual((true_trip.status, false_trip.status),
                         (TripStatus.LOADED_WAITING_UNLOAD, TripStatus.LOADED_WAITING_UNLOAD))
        at_boundary = manual_loading_module.reconcile_expired_manual_trips(now=now)
        true_trip.refresh_from_db(); false_trip.refresh_from_db()
        self.assertEqual(true_trip.status, TripStatus.UNCONTROLLED)
        self.assertEqual(false_trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        evidence('recorded_false_reconcile', before=before, at_boundary=at_boundary,
                 recorded_true=trip_state(true_trip), recorded_false=trip_state(false_trip))

    def test_driver_p2_then_direct_and_offline_y_are_both_observed(self):
        trip = self._open_trip('r1-p2-x')
        p2 = self._driver_sync(self._point(
            event_id='r1-p2-select', sequence=1, trip=trip,
            at=trip.loaded_at + timedelta(seconds=5), dump_point=self.p2, expected=self.dump_point,
        ), 'r1-p2-driver')
        self.assertEqual(p2['status'], 'accepted', p2)
        direct = trip_fixtures.ExcavatorWorkServerIntegrationTests.post_truck_loaded(
            self, client_action_id='r1-p2-direct-y', assignment=self.assignment,
        )
        offline_event = self.load_event('r1-p2-offline-y', 2, occurred_at=trip.loaded_at + timedelta(minutes=1))
        offline_event['payload']['manual_control'] = False
        offline_event['payload']['loading_block'] = 'R1-Y'
        offline = self.sync([offline_event], device_id='r1-p2-excavator').json()['results'][0]
        trip.refresh_from_db()
        self.assertEqual(trip.actual_dump_point_id, self.p2.id)
        self.assertEqual(Trip.objects.filter(truck=self.truck).count(), 1)
        self.assertEqual(direct.status_code, 409)
        self.assertIn(offline['status'], {'conflict', 'retry'})
        evidence('p2_then_y_paths', x=trip_state(trip), direct_status=direct.status_code,
                 direct=direct.json(), offline=offline, trip_count=Trip.objects.filter(truck=self.truck).count())

    def test_legacy_recorded_false_x_keeps_p2_after_real_y_cancel_and_reconcile(self):
        now = timezone.now()
        x = Trip.objects.create(
            truck=self.truck, excavator=self.excavator, rock_type=self.rock,
            dump_point=self.dump_point, assigned_dump_point=self.dump_point,
            actual_dump_point=self.dump_point, status=TripStatus.LOADED_WAITING_UNLOAD,
            loaded_at=now - timedelta(minutes=1), loading_shift=self.shift,
            driver_participation_recorded=False,
        )
        p2 = self._driver_sync(self._point(
            event_id='r1-legacy-x-p2', sequence=1, trip=x, at=now,
            dump_point=self.p2, expected=self.dump_point,
        ), 'r1-legacy-x-driver')
        self.assertEqual(p2['status'], 'accepted', p2)
        y_response = ManualLoadingTests.send(self, previous=x, action='r1-legacy-y')
        self.assertEqual(y_response.status_code, 200, y_response.content)
        y = Trip.objects.get(pk=y_response.json()['trip_id'])
        cancel = self.client.post(
            reverse('excavator_truck_loaded_cancel'),
            json.dumps({'client_action_id': 'r1-legacy-y-cancel', 'truck_id': self.truck.id,
                        'trip_id': y.id, 'dump_point_id': y.dump_point_id}),
            content_type='application/json',
        )
        self.assertEqual(cancel.status_code, 200, cancel.content)
        x.refresh_from_db(); y.refresh_from_db()
        self.assertEqual(x.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertEqual((x.actual_dump_point_id, x.dump_point_id), (self.p2.id, self.p2.id))
        self.assertEqual(y.status, TripStatus.CANCELLED)
        from trips.manual_loading import manual_dump_card_is_visible
        visible_now = manual_dump_card_is_visible(x, now=now + timedelta(minutes=1))
        reconciled = manual_loading_module.reconcile_expired_manual_trips(now=now + timedelta(minutes=10))
        x.refresh_from_db()
        self.assertTrue(visible_now)
        self.assertEqual(x.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertNotIn(x.id, reconciled)
        evidence('recorded_false_restore_with_p2', p2=p2, x=trip_state(x), y=trip_state(y),
                 cancel=cancel.json(), visible_immediately=visible_now, reconcile_ids=reconciled)


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class P28R1ProjectionTests(P28R1DriverBranchesTests):
    def test_real_server_projections_follow_p1_p2_unload_p3(self):
        trip = self._open_trip('r1-projection-load')
        driver_loaded = self.driver_client().get(reverse('driver_shift'))
        excavator_loaded = self.client.get(reverse('excavator_work'))
        self.assertEqual(driver_loaded.context['active_trip'].id, trip.id)
        excavator_card = next(card for card in excavator_loaded.context['truck_cards'] if card['assignment'].truck_id == trip.truck_id)
        self.assertEqual(int(excavator_card['open_trip_id']), trip.id)

        p2 = self._driver_sync(self._point(
            event_id='r1-projection-p2', sequence=1, trip=trip,
            at=trip.loaded_at + timedelta(seconds=5), dump_point=self.p2, expected=self.dump_point,
        ), 'r1-projection-driver')
        self.assertEqual(p2['status'], 'accepted', p2)
        driver_p2 = self.driver_client().get(reverse('driver_shift'))
        self.assertEqual(driver_p2.context['active_trip'].id, trip.id)
        self.assertEqual(driver_p2.context['active_trip_current_dump_point'].id, self.p2.id)

        unload_event = {
            'event_id': 'r1-projection-unload', 'event_type': 'driver.trip.unloaded',
            'format_version': 1, 'occurred_at': (trip.loaded_at + timedelta(seconds=10)).isoformat(),
            'sequence': 2, 'depends_on': ['r1-projection-p2'], 'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id, 'trip_id': trip.id, 'payload': {'trip_id': trip.id},
        }
        unload = self._driver_sync(unload_event, 'r1-projection-driver')
        self.assertEqual(unload['status'], 'accepted', unload)
        trip.refresh_from_db()
        driver_done = self.driver_client().get(reverse('driver_shift'))
        excavator_done = self.client.get(reverse('excavator_work'))
        self.assertIsNone(driver_done.context['active_trip'])
        done_card = next(card for card in excavator_done.context['truck_cards'] if card['assignment'].truck_id == trip.truck_id)
        self.assertNotEqual(str(done_card['open_trip_id']), str(trip.id))

        dispatcher_role, _ = Role.objects.get_or_create(code='dispatcher', defaults={'name': 'Диспетчер R1'})
        dispatcher = Employee.objects.create(full_name='Диспетчер P28 R1')
        access = EmployeeAccess.objects.create(
            employee=dispatcher, role=dispatcher_role, access_code='p28-r1-d',
            status=EmployeeAccess.Status.ACTIVATED, is_active=True,
        )
        request = RequestFactory().post('/reports/p28-r1/', data={
            'trip_id': str(trip.id), 'correction_reason': 'P28-C1-R1 причина P3',
            'volume_m3': str(trip.volume_m3 or Decimal('10.00')),
            'transport_distance_km': str(trip.transport_distance_km or Decimal('1.00')),
            'loading_horizon': trip.loading_horizon or '', 'loading_block': trip.loading_block or '',
            'rock_type_id': str(trip.rock_type_id), 'actual_dump_point_id': str(self.p3.id),
            'downtime_text': trip.downtime_text or '', 'note': trip.note or '',
        })
        request.session = {}; setattr(request, '_messages', FallbackStorage(request))
        from core.production_time import production_shift_context
        ctx = production_shift_context(trip.loaded_at)
        with patch('reports.views.reports_mutation_role_barrier', return_value=None):
            response = update_shift_report_trip(request, access, ctx.production_date, ctx.shift_type, 'trucks')
        self.assertEqual(response.status_code, 302)
        trip.refresh_from_db()
        action = DispatcherActionLog.objects.get(action_type='report_source_correction', trip_id=trip.id)
        self.assertEqual(action.actor_id, dispatcher.id)
        self.assertEqual(action.reason, 'P28-C1-R1 причина P3')
        self.assertEqual(trip.actual_dump_point_id, self.p3.id)

        from trips.views import build_dispatcher_dashboard_context
        dashboard = build_dispatcher_dashboard_context(
            dispatcher_shift=self.shift,
            active_trips=Trip.objects.filter(status__in=(TripStatus.ACTIVE, TripStatus.LOADED_WAITING_UNLOAD)),
            pending_assignments=HaulAssignment.objects.filter(status=AssignmentStatus.PENDING),
            accepted_assignments=HaulAssignment.objects.filter(status=AssignmentStatus.ACCEPTED),
            recent_completed_trips=Trip.objects.filter(pk=trip.id),
            open_shifts=type(self.shift).objects.filter(closed_at__isnull=True).exclude(pk=self.shift.pk),
            open_mechanic_downtimes=__import__('downtimes.models', fromlist=['DowntimeEvent']).DowntimeEvent.objects.filter(ended_at__isnull=True),
            trucks=Equipment.objects.filter(pk=self.truck.id), excavators=Equipment.objects.filter(pk=self.excavator.id),
            recent_dispatcher_actions=[action],
        )
        self.assertEqual(dashboard['mobile_shift_report']['completed_trip_count'], 1)
        self.assertEqual(dashboard['mobile_shift_report']['active_trip_count'], 0)
        evidence('real_server_projections', trip=trip_state(trip),
                 driver={'loaded_trip_id': driver_loaded.context['active_trip'].id,
                         'p2_trip_id': driver_p2.context['active_trip'].id,
                         'p2_point_id': driver_p2.context['active_trip_current_dump_point'].id,
                         'after_unload_active_trip': None},
                 excavator={'loaded_open_trip_id': excavator_card['open_trip_id'],
                            'after_unload_open_trip_id': done_card['open_trip_id']},
                 dispatcher={'completed_trip_count': dashboard['mobile_shift_report']['completed_trip_count'],
                             'active_trip_count': dashboard['mobile_shift_report']['active_trip_count'],
                             'completed_trip_id_not_exposed_by_panel_builder': True,
                             'correction_trip_id': action.trip_id,
                             'correction_actor_id': action.actor_id,
                             'correction_reason': action.reason})


class P28R1EnvironmentTest(TestCase):
    def test_00_vendor_and_isolated_test_database(self):
        vendor = connection.vendor
        name = str(connection.settings_dict.get('NAME') or '')
        if vendor == 'postgresql':
            self.assertIn('test', name.lower())
        else:
            self.assertEqual(vendor, 'sqlite')
            self.assertTrue(name.startswith('file:memorydb_') or ':memory:' in name, name)
        evidence('environment', vendor=vendor, test_database=name)
