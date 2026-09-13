import json
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

from django.db import close_old_connections, connection
from django.core.management.color import no_style
from django.apps import apps
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
from core.models import OfflineFieldEvent, OfflineFieldEventConflict
from downtimes.models import DowntimeEvent, DowntimeReason
from trips import tests as trip_fixtures
from trips.models import Trip, TripClientAction, TripStatus
from trips.views import finalize_trip_unloaded
from references.models import DumpPoint


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class OfflineEventSyncTests(TestCase):
    """Contract tests for durable receipts, chronology and local trip mapping."""

    create_registered_driver_shift = (
        trip_fixtures.ExcavatorWorkServerIntegrationTests.create_registered_driver_shift
    )

    def setUp(self):
        trip_fixtures.ExcavatorWorkServerIntegrationTests.setUp(self)
        self.url = reverse('offline_events_sync')
        from shifts.models import EmployeeShift

        self.shift = EmployeeShift.objects.get(employee=self.operator, closed_at__isnull=True)
        self.assignment = HaulAssignment.objects.get(
            truck=self.truck,
            excavator=self.excavator,
            status=AssignmentStatus.ACCEPTED,
        )

    def load_event(self, event_id='load-1', sequence=1, **changes):
        occurred_at = changes.pop('occurred_at', timezone.now())
        event = {
            'event_id': event_id,
            'event_type': 'excavator.trip.loaded',
            'format_version': 1,
            'actor_id': self.operator.id,
            'access_id': self.access.id,
            'role_code': 'excavator_operator',
            'occurred_at': occurred_at.isoformat(),
            'sequence': sequence,
            'depends_on': [],
            'shift_id': self.shift.id,
            'equipment_id': self.excavator.id,
            'local_trip_id': f'local-{event_id}',
            'context_snapshot': {
                'actor_id': self.operator.id,
                'access_id': self.access.id,
                'role_code': 'excavator_operator',
            },
            'payload': {
                'truck_id': self.truck.id,
                'assignment_id': self.assignment.id,
                'dump_point_id': self.dump_point.id,
                'rock_type_id': self.rock.id,
                'manual_control': True,
                'loading_horizon': '125',
                'loading_block': '4',
            },
        }
        event.update(changes)
        return event

    def sync(self, events, *, client=None, role_code='excavator_operator', device_id='device-test-001'):
        return (client or self.client).post(
            self.url,
            data=json.dumps({
                'protocol_version': 1,
                'actor_id': self.operator.id if role_code == 'excavator_operator' else self.driver.id,
                'access_id': self.access.id if role_code == 'excavator_operator' else self.driver_access.id,
                'role_code': role_code,
                'device_id': device_id,
                'events': events,
            }),
            content_type='application/json',
        )

    def driver_client(self):
        client = Client()
        session = client.session
        session['employee_access_id'] = self.driver_access.id
        session.save()
        return client

    def test_exact_retry_is_deduplicated_and_payload_reuse_is_conflict(self):
        event = self.load_event()
        first = self.sync([event])
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(first.json()['results'][0]['status'], 'accepted')
        trip = Trip.objects.get()
        self.assertEqual(trip.loaded_at, timezone.datetime.fromisoformat(event['occurred_at']))
        self.assertIsNotNone(trip.load_received_at)
        self.assertEqual(trip.load_time_source, 'excavator_device')

        repeated = self.sync([event])
        self.assertEqual(repeated.json()['results'][0]['status'], 'deduplicated')
        self.assertEqual(Trip.objects.count(), 1)

        altered = json.loads(json.dumps(event))
        altered['payload']['loading_block'] = '99'
        conflict = self.sync([altered])
        self.assertEqual(conflict.json()['results'][0]['status'], 'conflict')
        self.assertEqual(conflict.json()['results'][0]['code'], 'event_id_reused')
        self.assertEqual(OfflineFieldEventConflict.objects.count(), 1)
        self.assertEqual(Trip.objects.count(), 1)

        changed_claim = json.loads(json.dumps(event))
        changed_claim['actor_id'] = self.driver.id
        claim_conflict = self.sync([changed_claim])
        self.assertEqual(claim_conflict.json()['results'][0]['status'], 'conflict')
        self.assertEqual(claim_conflict.json()['results'][0]['code'], 'event_id_reused')
        self.assertEqual(OfflineFieldEventConflict.objects.count(), 2)

    def test_partial_batch_and_dependency_local_trip_mapping(self):
        first = self.load_event('load-chain-1', 10)
        second = self.load_event('load-chain-2', 11)
        second['depends_on'] = [first['event_id']]
        second['payload']['expected_open_trip_local_id'] = first['local_trip_id']
        invalid = {'event_id': 'broken', 'event_type': 'excavator.trip.loaded', 'format_version': 1}

        response = self.sync([second, invalid, first])

        self.assertEqual(response.status_code, 200, response.content)
        results = response.json()['results']
        self.assertEqual([item['status'] for item in results], ['accepted', 'invalid', 'accepted'])
        trips = list(Trip.objects.order_by('id'))
        self.assertEqual(len(trips), 2)
        self.assertEqual(trips[0].status, TripStatus.UNCONTROLLED)
        self.assertEqual(trips[0].superseded_by_id, trips[1].id)
        self.assertEqual(trips[1].status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertEqual(results[0]['server_ids']['trip_id'], trips[1].id)
        self.assertEqual(OfflineFieldEvent.objects.count(), 2)
        self.assertEqual(
            OfflineFieldEventConflict.objects.filter(existing_event__isnull=True).count(),
            1,
        )

    def test_missing_dependency_is_retry_then_succeeds(self):
        event = self.load_event('load-after-missing', 2)
        event['depends_on'] = ['load-before-missing']
        event['payload']['expected_open_trip_local_id'] = 'local-load-before-missing'

        waiting = self.sync([event]).json()['results'][0]
        self.assertEqual(waiting['status'], 'retry')
        self.assertTrue(waiting['retryable'])
        self.assertEqual(Trip.objects.count(), 0)

        dependency = self.load_event('load-before-missing', 1)
        accepted = self.sync([dependency, event]).json()['results']
        self.assertEqual([item['status'] for item in accepted], ['accepted', 'accepted'])
        self.assertEqual(Trip.objects.count(), 2)

    def test_loaded_event_can_be_cancelled_by_dependent_local_reference(self):
        loaded = self.load_event('load-to-cancel', 1)
        cancelled = {
            'event_id': 'cancel-local-load',
            'event_type': 'excavator.trip.loaded.cancelled',
            'format_version': 1,
            'occurred_at': (timezone.datetime.fromisoformat(loaded['occurred_at']) + timedelta(seconds=1)).isoformat(),
            'sequence': 2,
            'depends_on': [loaded['event_id']],
            'shift_id': self.shift.id,
            'equipment_id': self.excavator.id,
            'local_trip_id': loaded['local_trip_id'],
            'payload': {'local_trip_id': loaded['local_trip_id']},
        }

        results = self.sync([cancelled, loaded]).json()['results']

        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted'])
        trip = Trip.objects.get()
        self.assertEqual(trip.status, TripStatus.CANCELLED)
        self.assertEqual(results[0]['server_ids']['trip_id'], trip.id)

    def test_device_clock_ahead_is_persisted_conflict(self):
        event = self.load_event(occurred_at=timezone.now() + timedelta(minutes=6))
        result = self.sync([event]).json()['results'][0]
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(result['code'], 'device_clock_ahead')
        self.assertEqual(OfflineFieldEvent.objects.get().status, 'conflict')
        self.assertEqual(Trip.objects.count(), 0)

    def test_historical_assignment_is_used_but_event_after_end_conflicts(self):
        occurred_at = timezone.now()
        ended_at = occurred_at + timedelta(seconds=2)
        HaulAssignment.objects.filter(pk=self.assignment.id).update(
            status=AssignmentStatus.CANCELLED,
            ended_at=ended_at,
        )
        HaulAssignment.objects.create(
            truck=self.truck,
            excavator=self.other_excavator,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=ended_at,
        )

        historical = self.load_event('historical-load', 1, occurred_at=occurred_at)
        accepted = self.sync([historical]).json()['results'][0]
        self.assertEqual(accepted['status'], 'accepted', accepted)
        self.assertEqual(Trip.objects.get().excavator_id, self.excavator.id)

        too_late = self.load_event('late-load', 2, occurred_at=ended_at + timedelta(seconds=1))
        rejected = self.sync([too_late]).json()['results'][0]
        self.assertEqual(rejected['status'], 'conflict')
        self.assertEqual(rejected['code'], 'assignment_time_mismatch')

    def test_delayed_load_keeps_driver_shift_from_occurrence_not_new_shift(self):
        occurred_at = timezone.now()
        self.truck_shift.opened_at = occurred_at - timedelta(minutes=10)
        self.truck_shift.closed_at = occurred_at + timedelta(seconds=1)
        self.truck_shift.save(update_fields=['opened_at', 'closed_at'])
        _new_driver, _new_access, new_shift = self.create_registered_driver_shift(
            self.truck,
            full_name='Новый водитель',
            access_code='200022',
        )
        event = self.load_event('load-before-driver-change', 1, occurred_at=occurred_at)
        event['payload']['manual_control'] = False

        result = self.sync([event]).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        trip = Trip.objects.get()
        self.assertEqual(trip.driver_control_shift_id, self.truck_shift.id)
        self.assertNotEqual(trip.driver_control_shift_id, new_shift.id)

    def test_late_unload_completes_original_not_new_trip(self):
        loaded_at = timezone.now() - timedelta(minutes=10)
        self.truck_shift.opened_at = loaded_at - timedelta(minutes=10)
        self.truck_shift.save(update_fields=['opened_at'])
        old = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            excavator_operator=self.operator,
            loading_shift=self.shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            assigned_dump_point=self.dump_point,
            driver_participation_recorded=True,
            driver_control_shift=self.truck_shift,
            status=TripStatus.UNCONTROLLED,
            loaded_at=loaded_at,
            load_received_at=loaded_at,
            load_time_source='server_receipt',
            operationally_closed_at=loaded_at + timedelta(minutes=5),
        )
        new = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            excavator_operator=self.operator,
            loading_shift=self.shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            assigned_dump_point=self.dump_point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
        )
        occurred_at = loaded_at + timedelta(minutes=4)
        event = {
            'event_id': 'driver-unload-old',
            'event_type': 'driver.trip.unloaded',
            'format_version': 1,
            'occurred_at': occurred_at.isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'trip_id': old.id,
            'payload': {'trip_id': old.id},
        }

        result = self.sync(
            [event], client=self.driver_client(), role_code='driver', device_id='driver-device-001',
        ).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        old.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual(old.status, TripStatus.COMPLETED)
        self.assertEqual(old.completed_at, occurred_at)
        self.assertEqual(new.status, TripStatus.LOADED_WAITING_UNLOAD)

    def test_legacy_unload_action_is_imported_without_second_mutation(self):
        loaded_at = timezone.now() - timedelta(minutes=5)
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            excavator_operator=self.operator,
            loading_shift=self.shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            assigned_dump_point=self.dump_point,
            driver_participation_recorded=True,
            driver_control_shift=self.truck_shift,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            loaded_at=loaded_at,
        )
        occurred_at = timezone.now() - timedelta(minutes=1)
        finalize_trip_unloaded(
            trip,
            driver=self.driver,
            unloading_shift=self.truck_shift,
            occurred_at=occurred_at,
        )
        TripClientAction.objects.create(
            action_type='trip_unloaded',
            client_action_id='legacy.unload:42',
            trip=trip,
            actor=self.driver,
        )
        event = {
            'event_id': 'legacy.unload:42',
            'event_type': 'driver.trip.unloaded',
            'format_version': 1,
            'occurred_at': occurred_at.isoformat(),
            'sequence': 42,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'trip_id': trip.id,
            'payload': {'trip_id': trip.id},
        }

        result = self.sync(
            [event], client=self.driver_client(), role_code='driver', device_id='driver-device-legacy',
        ).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        self.assertTrue(result['legacy_action_imported'])
        self.assertEqual(TripClientAction.objects.filter(action_type='trip_unloaded').count(), 1)
        self.assertEqual(OfflineFieldEvent.objects.get().trip_id, trip.id)

    def test_dump_point_a_to_b_to_a_keeps_distinct_ordered_events(self):
        self.truck_shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.truck_shift.save(update_fields=['opened_at'])
        point_b = DumpPoint.objects.create(name='Склад Б')
        trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            excavator_operator=self.operator,
            loading_shift=self.shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            assigned_dump_point=self.dump_point,
            driver_participation_recorded=True,
            driver_control_shift=self.truck_shift,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            loaded_at=timezone.now() - timedelta(minutes=2),
        )

        def changed(event_id, sequence, point_id, expected_point_id, depends_on=()):
            return {
                'event_id': event_id,
                'event_type': 'driver.trip.dump_point_changed',
                'format_version': 1,
                'occurred_at': (timezone.now() + timedelta(seconds=sequence)).isoformat(),
                'sequence': sequence,
                'depends_on': list(depends_on),
                'shift_id': self.truck_shift.id,
                'equipment_id': self.truck.id,
                'trip_id': trip.id,
                'payload': {
                    'trip_id': trip.id,
                    'dump_point_id': point_id,
                    'expected_actual_dump_point_id': expected_point_id,
                },
            }

        to_b = changed('dump-a-to-b', 1, point_b.id, self.dump_point.id)
        back_to_a = changed(
            'dump-b-to-a', 2, self.dump_point.id, point_b.id, [to_b['event_id']],
        )
        client = self.driver_client()

        result = self.sync(
            [back_to_a, to_b], client=client, role_code='driver', device_id='driver-device-dump',
        ).json()['results']

        self.assertEqual([item['status'] for item in result], ['accepted', 'accepted'])
        trip.refresh_from_db()
        self.assertEqual(trip.assigned_dump_point_id, self.dump_point.id)
        self.assertEqual(trip.actual_dump_point_id, self.dump_point.id)
        self.assertEqual(trip.dump_point_id, self.dump_point.id)
        repeated = self.sync(
            [to_b, back_to_a], client=client, role_code='driver', device_id='driver-device-dump',
        ).json()['results']
        self.assertEqual([item['status'] for item in repeated], ['deduplicated', 'deduplicated'])
        self.assertEqual(TripClientAction.objects.filter(action_type='change_actual_unload_point').count(), 2)

    def test_downtime_local_reference_maps_to_server_id(self):
        self.truck_shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.truck_shift.save(update_fields=['opened_at'])
        reason = DowntimeReason.objects.create(
            name='Проверка offline простоя',
            equipment_type=self.truck_type,
            show_for_truck_driver=True,
        )
        started_at = timezone.now() - timedelta(minutes=2)
        ended_at = timezone.now() - timedelta(minutes=1)
        started = {
            'event_id': 'driver-downtime-start',
            'event_type': 'driver.downtime.started',
            'format_version': 1,
            'occurred_at': started_at.isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'local_downtime_id': 'driver-downtime-local-1',
            'payload': {'reason_id': reason.id},
        }
        ended = {
            'event_id': 'driver-downtime-end',
            'event_type': 'driver.downtime.ended',
            'format_version': 1,
            'occurred_at': ended_at.isoformat(),
            'sequence': 2,
            'depends_on': [started['event_id']],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'payload': {'local_downtime_id': 'driver-downtime-local-1'},
        }

        results = self.sync(
            [ended, started],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-device-down',
        ).json()['results']

        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted'])
        event = DowntimeEvent.objects.get(reason=reason)
        self.assertEqual(event.started_at, started_at)
        self.assertEqual(event.ended_at, ended_at)
        self.assertEqual(results[0]['server_ids']['downtime_event_id'], event.id)
        self.assertEqual(results[1]['server_ids']['downtime_event_id'], event.id)

    def test_auth_and_envelope_errors_are_classified(self):
        anonymous = Client().post(
            self.url,
            data=json.dumps({'format_version': 1, 'events': []}),
            content_type='application/json',
        )
        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(anonymous.json()['status'], 'auth_required')

        invalid = self.client.post(
            self.url,
            data='{',
            content_type='application/json',
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()['status'], 'invalid')

    def test_legacy_driver_shift_close_preserves_occurred_at(self):
        self.truck_model.fuel_capacity_limit_l = 500
        self.truck_model.save(update_fields=['fuel_capacity_limit_l'])
        self.truck_shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.truck_shift.start_fuel = 100
        self.truck_shift.start_mileage = 1000
        self.truck_shift.start_engine_hours = 100
        self.truck_shift.save(update_fields=[
            'opened_at', 'start_fuel', 'start_mileage', 'start_engine_hours',
        ])
        occurred_at = timezone.now() - timedelta(seconds=30)

        response = self.driver_client().post(
            reverse('driver_close_shift'),
            data={
                'shift_id': self.truck_shift.id,
                'client_action_id': 'legacy-driver-close-occurred',
                'end_fuel': '90',
                'end_mileage': '1010',
                'end_engine_hours': '101',
                'occurred_at': occurred_at.isoformat(),
            },
            HTTP_ACCEPT='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.truck_shift.refresh_from_db()
        self.assertEqual(self.truck_shift.closed_at, occurred_at)
        action = self.truck_shift.client_actions.get(
            action_type='driver_shift_closed',
            client_action_id='legacy-driver-close-occurred',
        )
        self.assertGreater(action.created_at, self.truck_shift.closed_at)

    def test_offline_driver_shift_close_preserves_occurrence_and_receipt(self):
        self.truck_model.fuel_capacity_limit_l = 500
        self.truck_model.save(update_fields=['fuel_capacity_limit_l'])
        self.truck_shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.truck_shift.start_fuel = 100
        self.truck_shift.start_mileage = 1000
        self.truck_shift.start_engine_hours = 100
        self.truck_shift.save(update_fields=[
            'opened_at', 'start_fuel', 'start_mileage', 'start_engine_hours',
        ])
        occurred_at = timezone.now() - timedelta(seconds=30)
        event = {
            'event_id': 'offline-driver-close',
            'event_type': 'driver.shift.closed',
            'format_version': 1,
            'occurred_at': occurred_at.isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'payload': {
                'end_fuel': '90',
                'end_mileage': '1010',
                'end_engine_hours': '101',
            },
        }

        result = self.sync(
            [event],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-device-close',
        ).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        self.truck_shift.refresh_from_db()
        receipt = OfflineFieldEvent.objects.get(event_id=event['event_id'])
        self.assertEqual(self.truck_shift.closed_at, occurred_at)
        self.assertEqual(receipt.occurred_at, occurred_at)
        self.assertGreater(receipt.received_at, receipt.occurred_at)


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class OfflineEventPostgreSQLConcurrencyTests(TransactionTestCase):
    """Real row/advisory-lock tests; skipped by design on SQLite."""

    reset_sequences = True
    serialized_rollback = True
    create_registered_driver_shift = (
        trip_fixtures.ExcavatorWorkServerIntegrationTests.create_registered_driver_shift
    )

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Тест конкурентности требует отдельную PostgreSQL-БД.')
        with connection.cursor() as cursor:
            for sql in connection.ops.sequence_reset_sql(no_style(), apps.get_models()):
                cursor.execute(sql)
        trip_fixtures.ExcavatorWorkServerIntegrationTests.setUp(self)
        from shifts.models import EmployeeShift

        self.shift = EmployeeShift.objects.get(employee=self.operator, closed_at__isnull=True)
        self.assignment = HaulAssignment.objects.get(
            truck=self.truck,
            excavator=self.excavator,
            status=AssignmentStatus.ACCEPTED,
        )
        self.url = reverse('offline_events_sync')
        self.session_cookie = self.client.cookies['sessionid'].value

    def event(self, event_id, sequence):
        return {
            'event_id': event_id,
            'event_type': 'excavator.trip.loaded',
            'format_version': 1,
            'occurred_at': timezone.now().isoformat(),
            'sequence': sequence,
            'depends_on': [],
            'shift_id': self.shift.id,
            'equipment_id': self.excavator.id,
            'local_trip_id': f'local-{event_id}',
            'payload': {
                'truck_id': self.truck.id,
                'assignment_id': self.assignment.id,
                'dump_point_id': self.dump_point.id,
                'rock_type_id': self.rock.id,
                'manual_control': True,
            },
        }

    def post_from_thread(self, event, device_id):
        close_old_connections()
        client = Client()
        client.cookies['sessionid'] = self.session_cookie
        response = client.post(
            self.url,
            data=json.dumps({
                'protocol_version': 1,
                'role_code': 'excavator_operator',
                'device_id': device_id,
                'events': [event],
            }),
            content_type='application/json',
        )
        close_old_connections()
        return response.status_code, response.json()['results'][0]

    def test_same_event_parallel_requests_create_one_trip(self):
        event = self.event('parallel-same-event', 1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _: self.post_from_thread(event, 'parallel-device-001'),
                range(2),
            ))

        self.assertEqual([item[0] for item in results], [200, 200])
        self.assertEqual(
            sorted(item[1]['status'] for item in results),
            ['accepted', 'deduplicated'],
        )
        self.assertEqual(Trip.objects.count(), 1)
        self.assertEqual(OfflineFieldEvent.objects.count(), 1)
        self.assertEqual(TripClientAction.objects.filter(action_type='truck_loaded').count(), 1)

    def test_two_devices_racing_for_truck_do_not_create_two_open_trips(self):
        events = [
            (self.event('parallel-device-a-load', 1), 'parallel-device-a'),
            (self.event('parallel-device-b-load', 1), 'parallel-device-b'),
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda args: self.post_from_thread(*args), events))

        statuses = sorted(item[1]['status'] for item in results)
        self.assertEqual(statuses, ['accepted', 'conflict'])
        self.assertEqual(Trip.objects.count(), 1)
        self.assertEqual(
            Trip.objects.filter(status=TripStatus.LOADED_WAITING_UNLOAD).count(),
            1,
        )
        self.assertEqual(OfflineFieldEvent.objects.count(), 2)
