"""187/190: server truth survives lost responses; transport does not consume events."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
from core.models import OfflineFieldEvent, OperationalStateEvent, OperationalStateVersion
from core.realtime import event_is_relevant
from shifts.models import EmployeeShift
from trips import tests as trip_fixtures
from trips.models import Trip, TripClientAction, TripStatus
from users.models import ActiveApplicationSession


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True, ALLOWED_HOSTS=['testserver', '.localhost'])
class ConnectionRecoveryServerContractTests(TestCase):
    create_registered_driver_shift = trip_fixtures.ExcavatorWorkServerIntegrationTests.create_registered_driver_shift

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        trip_fixtures.ExcavatorWorkServerIntegrationTests.setUp(self)
        self.operator_shift = EmployeeShift.objects.get(employee=self.operator, closed_at__isnull=True)
        # Current role contract uses explicit workplaces, never incident production IDs.
        self.operator_shift.workplace_code = 'excavator_operator'
        self.operator_shift.save(update_fields=['workplace_code'])
        self.truck_shift.workplace_code = 'driver'
        self.truck_shift.save(update_fields=['workplace_code'])
        self.assignment = HaulAssignment.objects.get(truck=self.truck, excavator=self.excavator,
                                                    status=AssignmentStatus.ACCEPTED)
        self.driver_client = Client()
        session = self.driver_client.session
        session['employee_access_id'] = self.driver_access.pk
        session.save()
        throttle = patch('assignments.services.reconcile_due_haul_assignments_throttled')
        throttle.start()
        self.addCleanup(throttle.stop)

    def load_event(self, *, manual=False):
        return {
            'event_id': 'connection-recovery-load', 'event_type': 'excavator.trip.loaded',
            'format_version': 1, 'actor_id': self.operator.pk, 'access_id': self.access.pk,
            'role_code': 'excavator_operator', 'occurred_at': timezone.now().isoformat(),
            'sequence': 1, 'depends_on': [], 'shift_id': self.operator_shift.pk,
            'equipment_id': self.excavator.pk, 'local_trip_id': 'local-connection-recovery',
            'context_snapshot': {'actor_id': self.operator.pk, 'access_id': self.access.pk,
                                 'role_code': 'excavator_operator'},
            'payload': {'truck_id': self.truck.pk, 'assignment_id': self.assignment.pk,
                        'dump_point_id': self.dump_point.pk, 'rock_type_id': self.rock.pk,
                        'manual_control': manual, 'loading_horizon': '125', 'loading_block': '4'},
        }

    def sync(self, event):
        return self.client.post(reverse('offline_events_sync'), data=json.dumps({
            'format_version': 1, 'actor_id': self.operator.pk, 'access_id': self.access.pk,
            'role_code': 'excavator_operator', 'device_id': 'contract-recovery-test', 'events': [event],
        }), content_type='application/json')

    def accept_load(self, *, manual=False):
        event = self.load_event(manual=manual)
        response = self.sync(event)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['results'][0]['status'], 'accepted', response.content)
        trip = Trip.objects.get()
        published = OperationalStateEvent.objects.filter(event_type='trip_changed',
                     object_id=str(trip.pk), payload__action='truck_loaded').latest('version')
        return event, trip, published, response.json()['results'][0]

    def test_offline_load_publishes_complete_addressed_server_truth(self):
        _, trip, event, result = self.accept_load()
        expected = {
            'action': 'truck_loaded', 'trip_id': trip.pk, 'truck_id': self.truck.pk,
            'excavator_id': self.excavator.pk, 'excavator_ids': [self.excavator.pk],
            'driver_control_shift_id': self.truck_shift.pk,
            'driver_participation_recorded': True,
            'assigned_dump_point_id': self.dump_point.pk, 'actual_dump_point_id': None,
            'dump_point_name': str(self.dump_point), 'status': TripStatus.LOADED_WAITING_UNLOAD,
        }
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertIn(key, event.payload)
                self.assertEqual(event.payload[key], value)
        self.assertEqual(result['server_ids']['trip_id'], trip.pk)
        self.assertEqual(result['version'], event.version)

    def test_retry_after_lost_sync_response_preserves_one_receipt_one_trip_and_version(self):
        event, trip, published, first = self.accept_load()
        version_before_retry = OperationalStateVersion.objects.get(key='production').version
        response = self.sync(event)
        self.assertEqual(response.status_code, 200)
        second = response.json()['results'][0]
        self.assertEqual(second['status'], 'deduplicated')
        self.assertEqual(second['server_ids'], first['server_ids'])
        self.assertEqual(second['version'], first['version'])
        self.assertEqual(Trip.objects.count(), 1)
        self.assertEqual(TripClientAction.objects.filter(client_action_id=event['event_id']).count(), 1)
        self.assertEqual(OfflineFieldEvent.objects.filter(event_id=event['event_id']).count(), 1)
        self.assertEqual(OperationalStateVersion.objects.get(key='production').version, version_before_retry)

    def test_online_load_retry_keeps_one_trip_and_the_same_addressed_payload(self):
        self.driver_client.get(reverse('operational_state_version'),
                    {'include_events': '0', 'role_app_code': 'driver'},
                    HTTP_HOST='driver.localhost', HTTP_USER_AGENT='CopperResourcesNative/driver/0.1.28')
        version_before = OperationalStateVersion.objects.get(key='production').version
        payload = dict(self.load_event()['payload'], client_action_id='connection-online-load')
        response = self.client.post(reverse('excavator_truck_loaded'), json.dumps(payload),
                                    content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()
        trip = Trip.objects.get()
        event = OperationalStateEvent.objects.get(version=result['version'])
        self.assertGreater(event.version, version_before)
        self.assertEqual(event.payload['driver_control_shift_id'], self.truck_shift.pk)
        self.assertEqual(event.payload['dump_point_name'], str(self.dump_point))
        self.assertEqual(event.payload['assigned_dump_point_id'], self.dump_point.pk)
        self.assertEqual(event.payload['excavator_ids'], [self.excavator.pk])
        repeated = self.client.post(reverse('excavator_truck_loaded'), json.dumps(payload),
                                    content_type='application/json')
        self.assertEqual(repeated.status_code, 200)
        self.assertTrue(repeated.json()['deduplicated'])
        self.assertEqual(repeated.json()['trip_id'], trip.pk)
        self.assertEqual(Trip.objects.count(), 1)
        self.assertEqual(TripClientAction.objects.filter(client_action_id=payload['client_action_id']).count(), 1)

    def test_driver_shift_scope_is_preserved_even_when_truck_equipment_set_changes(self):
        _, trip, event, _ = self.accept_load()
        self.assertTrue(event_is_relevant(event, self.driver_access, equipment_ids=set(),
                                         driver_shift_ids={trip.driver_control_shift_id}))
        self.assertFalse(event_is_relevant(event, self.driver_access, equipment_ids={self.truck.pk},
                                          driver_shift_ids=set()))
        self.assertTrue(event_is_relevant(event, self.access, equipment_ids={self.excavator.pk}))
        self.assertFalse(event_is_relevant(event, self.access, equipment_ids={self.other_excavator.pk}))

    def test_passive_manual_load_does_not_notify_late_driver_as_participant(self):
        _, trip, event, _ = self.accept_load(manual=True)
        self.assertIsNone(trip.driver_control_shift_id)
        self.assertTrue(trip.driver_participation_recorded)
        self.assertFalse(event_is_relevant(event, self.driver_access, equipment_ids={self.truck.pk},
                                          driver_shift_ids={self.truck_shift.pk}))
        self.assertTrue(event_is_relevant(event, self.access, equipment_ids={self.excavator.pk}))

    def test_native_heartbeat_does_not_consume_realtime_delta_for_webview(self):
        _, trip, event, _ = self.accept_load()
        after = event.version - 1
        native = self.driver_client.get(reverse('operational_state_version'),
                    {'after': after, 'include_events': '0', 'role_app_code': 'driver'},
                    HTTP_HOST='driver.localhost', HTTP_USER_AGENT='CopperResourcesNative/driver/0.1.28')
        self.assertEqual(native.status_code, 200)
        self.assertEqual(native.json()['events'], [])
        self.assertTrue(native.json()['has_active_shift'])
        self.assertTrue(native.json()['background_connection_required'])
        webview = self.driver_client.get(reverse('operational_state_version'), {'after': after})
        self.assertEqual(webview.status_code, 200)
        received = [item for item in webview.json()['events'] if item['payload'].get('action') == 'truck_loaded']
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]['payload']['trip_id'], trip.pk)
        self.assertEqual(received[0]['version'], event.version)
        again = self.driver_client.get(reverse('operational_state_version'), {'after': after})
        self.assertEqual(again.json()['events'], webview.json()['events'])

    def test_foreground_heartbeat_is_no_store_204_for_both_roles_and_requires_session(self):
        url = reverse('application_session_heartbeat')
        for client, access, path in [(self.driver_client, self.driver_access, '/driver/'),
                                     (self.client, self.access, '/excavator/work/')]:
            with self.subTest(role=access.role.code):
                response = client.post(url, {'path': path, 'client_kind': 'android_apk', 'client_version': '0.1.28'})
                self.assertEqual(response.status_code, 204)
                self.assertIn('no-store', response['Cache-Control'])
                session = ActiveApplicationSession.objects.get(access=access)
                self.assertIsNotNone(session.foreground_seen_at)
        self.assertEqual(Client().post(url, {'path': '/driver/'}).status_code, 401)
        self.assertEqual(Client().get(reverse('operational_state_version')).status_code, 401)

    def test_invalid_offline_envelope_and_expired_session_do_not_create_trips(self):
        url = reverse('offline_events_sync')
        for body in ['[]', '{broken', '{"format_version": 0}']:
            with self.subTest(body=body):
                self.assertEqual(self.client.post(url, body, content_type='application/json').status_code, 400)
        response = Client().post(url, '{}', content_type='application/json')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['status'], 'auth_required')
        self.assertEqual(Trip.objects.count(), 0)

    def test_unscoped_truncated_legacy_event_remains_relevant_for_both_field_roles(self):
        event = SimpleNamespace(event_type='trip_changed', payload={'action': 'truck_loaded'})
        self.assertTrue(event_is_relevant(event, self.driver_access, equipment_ids=set()))
        self.assertTrue(event_is_relevant(event, self.access, equipment_ids=set()))
