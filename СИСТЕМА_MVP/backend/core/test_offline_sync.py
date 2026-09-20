import json
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

from django.db import close_old_connections, connection
from django.core.management.color import no_style
from django.apps import apps
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assignments.models import (
    AssignmentStatus,
    ExcavatorDumpPointSetting,
    ExcavatorPlacement,
    HaulAssignment,
)
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

    def driver_manual_event(self, event_id='driver-manual-load-1', sequence=1, **changes):
        placement, _ = ExcavatorPlacement.objects.update_or_create(
            excavator=self.excavator,
            defaults={
                'zone': ExcavatorPlacement.Zone.ACTIVE,
                'work_rock_type': self.rock,
                'work_dump_point': self.dump_point,
                'loading_horizon': '125',
                'loading_block': '4',
                'transport_distance_km': '4.20',
                'work_context_updated_at': timezone.now() - timedelta(minutes=1),
                'changed_by': self.operator,
            },
        )
        ExcavatorDumpPointSetting.objects.update_or_create(
            placement=placement,
            dump_point=self.dump_point,
            defaults={
                'position': 1,
                'transport_distance_km': '4.20',
                'changed_by': self.operator,
            },
        )
        occurred_at = changes.pop('occurred_at', timezone.now())
        event = {
            'event_id': event_id,
            'event_type': 'driver.trip.loaded',
            'format_version': 1,
            'actor_id': self.driver.id,
            'access_id': self.driver_access.id,
            'role_code': 'driver',
            'occurred_at': occurred_at.isoformat(),
            'sequence': sequence,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'local_trip_id': event_id,
            'context_snapshot': {
                'source': 'driver_manual',
                'authority_type': 'assignment',
                'assignment_id': self.assignment.id,
                'excavator_id': self.excavator.id,
                'excavator_label': str(self.excavator),
                'placement_id': placement.id,
                'placement_updated_at': placement.work_context_updated_at.isoformat(),
                'rock_type_id': self.rock.id,
                'rock_type_name': str(self.rock),
                'loading_horizon': '125',
                'loading_block': '4',
                'dump_points': [{
                    'id': self.dump_point.id,
                    'name': str(self.dump_point),
                    'transport_distance_km': '4.20',
                }],
                'selected_dump_point_id': self.dump_point.id,
                'selected_dump_point_name': str(self.dump_point),
                'selected_one_off': False,
            },
            'payload': {
                'manual_control': True,
                'truck_id': self.truck.id,
                'excavator_id': self.excavator.id,
                'dump_point_id': self.dump_point.id,
                'rock_type_id': self.rock.id,
                'placement_id': placement.id,
                'placement_updated_at': placement.work_context_updated_at.isoformat(),
                'loading_horizon': '125',
                'loading_block': '4',
                'transport_distance_km': '4.20',
                'assignment_id': self.assignment.id,
                'free_bucket_acceptance_id': None,
                'free_bucket_acceptance_local_id': None,
            },
        }
        event.update(changes)
        return event

    def test_driver_manual_load_is_durable_deduplicated_and_truthfully_attributed(self):
        event = self.driver_manual_event()
        first = self.sync([event], client=self.driver_client(), role_code='driver').json()['results'][0]

        self.assertEqual(first['status'], 'accepted', first)
        self.assertEqual(first['trip_origin'], 'driver_manual')
        trip = Trip.objects.get()
        self.assertEqual(first['server_ids']['trip_id'], trip.id)
        self.assertEqual(trip.driver_id, self.driver.id)
        self.assertEqual(trip.driver_control_shift_id, self.truck_shift.id)
        self.assertTrue(trip.driver_participation_recorded)
        self.assertEqual(trip.load_time_source, 'driver_device')
        self.assertEqual(trip.excavator_id, self.excavator.id)
        self.assertEqual(trip.dump_point_id, self.dump_point.id)
        self.assertEqual(trip.rock_type_id, self.rock.id)
        self.assertTrue(TripClientAction.objects.filter(
            trip=trip,
            action_type='driver_manual_loaded',
            client_action_id=event['event_id'],
        ).exists())

        repeated = self.sync([event], client=self.driver_client(), role_code='driver').json()['results'][0]
        self.assertEqual(repeated['status'], 'deduplicated')
        self.assertEqual(Trip.objects.count(), 1)

    def test_driver_then_excavator_loads_are_one_physical_trip(self):
        manual = self.driver_manual_event('driver-first', 1)
        manual_result = self.sync(
            [manual], client=self.driver_client(), role_code='driver', device_id='driver-device',
        ).json()['results'][0]
        automatic = self.load_event(
            'excavator-after-driver',
            1,
            occurred_at=timezone.datetime.fromisoformat(manual['occurred_at']) + timedelta(seconds=1),
        )
        automatic_result = self.sync(
            [automatic], device_id='excavator-device',
        ).json()['results'][0]

        self.assertEqual(manual_result['status'], 'accepted', manual_result)
        self.assertEqual(automatic_result['status'], 'accepted', automatic_result)
        self.assertEqual(Trip.objects.count(), 1)
        trip = Trip.objects.get()
        self.assertEqual(manual_result['server_ids']['trip_id'], trip.id)
        self.assertEqual(automatic_result['server_ids']['trip_id'], trip.id)
        self.assertEqual(trip.excavator_operator_id, self.operator.id)
        self.assertEqual(trip.loading_shift_id, self.shift.id)
        self.assertEqual(trip.load_time_source, 'excavator_device')
        self.assertEqual(
            set(TripClientAction.objects.filter(trip=trip).values_list('action_type', flat=True)),
            {'driver_manual_loaded', 'truck_loaded'},
        )

    def test_excavator_then_driver_loads_are_one_physical_trip(self):
        automatic = self.load_event('excavator-first', 1)
        automatic_result = self.sync([automatic], device_id='excavator-device').json()['results'][0]
        manual = self.driver_manual_event(
            'driver-after-excavator',
            1,
            occurred_at=timezone.datetime.fromisoformat(automatic['occurred_at']) + timedelta(seconds=1),
        )
        manual_result = self.sync(
            [manual], client=self.driver_client(), role_code='driver', device_id='driver-device',
        ).json()['results'][0]

        self.assertEqual(automatic_result['status'], 'accepted', automatic_result)
        self.assertEqual(manual_result['status'], 'accepted', manual_result)
        self.assertEqual(manual_result['trip_origin'], 'excavator')
        self.assertEqual(Trip.objects.count(), 1)
        trip = Trip.objects.get()
        self.assertEqual(automatic_result['server_ids']['trip_id'], trip.id)
        self.assertEqual(manual_result['server_ids']['trip_id'], trip.id)
        self.assertEqual(
            set(TripClientAction.objects.filter(trip=trip).values_list('action_type', flat=True)),
            {'driver_manual_loaded', 'truck_loaded'},
        )

    def test_driver_manual_load_rejects_changed_context_without_creating_trip(self):
        event = self.driver_manual_event()
        event['payload']['loading_block'] = '99'
        result = self.sync(
            [event], client=self.driver_client(), role_code='driver', device_id='driver-device',
        ).json()['results'][0]
        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'manual_work_context_changed')
        self.assertEqual(Trip.objects.count(), 0)

    def test_manual_load_point_change_and_unload_share_one_local_trip_chain(self):
        changed_point = DumpPoint.objects.create(name='ККД ручного рейса')
        loaded = self.driver_manual_event('manual-chain-load', 1)
        loaded_at = timezone.datetime.fromisoformat(loaded['occurred_at'])
        point = {
            'event_id': 'manual-chain-point',
            'event_type': 'driver.trip.dump_point_changed',
            'format_version': 1,
            'occurred_at': (loaded_at + timedelta(seconds=1)).isoformat(),
            'sequence': 2,
            'depends_on': [loaded['event_id']],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'local_trip_id': loaded['local_trip_id'],
            'context_snapshot': {
                'selected_dump_point_id': changed_point.id,
                'selected_dump_point_name': str(changed_point),
            },
            'payload': {
                'dump_point_id': changed_point.id,
                'expected_actual_dump_point_id': self.dump_point.id,
            },
        }
        unloaded = {
            'event_id': 'manual-chain-unload',
            'event_type': 'driver.trip.unloaded',
            'format_version': 1,
            'occurred_at': (loaded_at + timedelta(seconds=2)).isoformat(),
            'sequence': 3,
            'depends_on': [point['event_id']],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'local_trip_id': loaded['local_trip_id'],
            'context_snapshot': {},
            'payload': {'local_trip_id': loaded['local_trip_id']},
        }

        results = self.sync(
            [unloaded, point, loaded],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-chain-device',
        ).json()['results']

        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted', 'accepted'])
        self.assertEqual(Trip.objects.count(), 1)
        trip = Trip.objects.get()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.assigned_dump_point_id, self.dump_point.id)
        self.assertEqual(trip.actual_dump_point_id, changed_point.id)
        self.assertEqual(trip.dump_point_id, changed_point.id)
        self.assertEqual({item['server_ids']['trip_id'] for item in results}, {trip.id})

    def test_next_manual_swipe_finishes_previous_cycle_without_separate_unload(self):
        first = self.driver_manual_event('manual-cycle-first', 1)
        first_result = self.sync(
            [first],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-cycle-device',
        ).json()['results'][0]
        first_trip = Trip.objects.get(pk=first_result['server_ids']['trip_id'])
        self.assertEqual(first_trip.status, TripStatus.LOADED_WAITING_UNLOAD)

        second = self.driver_manual_event(
            'manual-cycle-second',
            2,
            occurred_at=timezone.datetime.fromisoformat(first['occurred_at']) + timedelta(minutes=3),
        )
        second['depends_on'] = [first['event_id']]
        second_result = self.sync(
            [second],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-cycle-device',
        ).json()['results'][0]

        self.assertEqual(second_result['status'], 'accepted', second_result)
        self.assertEqual(Trip.objects.count(), 2)
        first_trip.refresh_from_db()
        second_trip = Trip.objects.get(pk=second_result['server_ids']['trip_id'])
        self.assertEqual(first_trip.status, TripStatus.COMPLETED)
        self.assertEqual(first_trip.completed_at, timezone.datetime.fromisoformat(second['occurred_at']))
        self.assertIsNotNone(first_trip.volume_m3)
        self.assertIsNotNone(first_trip.tonnage)
        self.assertEqual(second_trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertTrue(TripClientAction.objects.filter(
            trip=first_trip,
            action_type='driver_manual_cycle_advanced',
            client_action_id=second['event_id'],
        ).exists())

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
        new_shift.opened_at = self.truck_shift.closed_at + timedelta(seconds=1)
        new_shift.save(update_fields=['opened_at'])
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

    def test_dump_point_current_choice_is_accepted_without_business_change(self):
        self.truck_shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.truck_shift.save(update_fields=['opened_at'])
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
        event = {
            'event_id': 'dump-current-noop',
            'event_type': 'driver.trip.dump_point_changed',
            'format_version': 1,
            'occurred_at': timezone.now().isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'trip_id': trip.id,
            'payload': {
                'trip_id': trip.id,
                'dump_point_id': self.dump_point.id,
                'expected_actual_dump_point_id': self.dump_point.id,
            },
        }

        result = self.sync(
            [event], client=self.driver_client(), role_code='driver', device_id='driver-device-noop',
        ).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        self.assertTrue(result['no_change'])
        self.assertEqual(TripClientAction.objects.filter(action_type='change_actual_unload_point').count(), 0)
        trip.refresh_from_db()
        self.assertIsNone(trip.actual_dump_point_id)
        self.assertEqual(trip.dump_point_id, self.dump_point.id)

    def test_dump_point_change_rejects_inactive_reference(self):
        self.truck_shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.truck_shift.save(update_fields=['opened_at'])
        inactive = DumpPoint.objects.create(name='Закрытая точка offline', is_active=False)
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
        event = {
            'event_id': 'dump-inactive-point',
            'event_type': 'driver.trip.dump_point_changed',
            'format_version': 1,
            'occurred_at': timezone.now().isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'trip_id': trip.id,
            'payload': {
                'trip_id': trip.id,
                'dump_point_id': inactive.id,
                'expected_actual_dump_point_id': self.dump_point.id,
            },
        }

        result = self.sync(
            [event], client=self.driver_client(), role_code='driver', device_id='driver-device-inactive',
        ).json()['results'][0]

        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'dump_point_changed')
        trip.refresh_from_db()
        self.assertEqual(trip.dump_point_id, self.dump_point.id)
        self.assertEqual(TripClientAction.objects.filter(action_type='change_actual_unload_point').count(), 0)

    def test_dump_point_change_and_dependent_unload_complete_same_exact_trip(self):
        self.truck_shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.truck_shift.save(update_fields=['opened_at'])
        changed_point = DumpPoint.objects.create(name='Склад offline')
        loaded_at = timezone.now() - timedelta(minutes=2)
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
        changed_at = loaded_at + timedelta(minutes=1)
        point_event = {
            'event_id': 'dump-before-unload',
            'event_type': 'driver.trip.dump_point_changed',
            'format_version': 1,
            'occurred_at': changed_at.isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'trip_id': trip.id,
            'payload': {
                'trip_id': trip.id,
                'dump_point_id': changed_point.id,
                'expected_actual_dump_point_id': self.dump_point.id,
            },
        }
        unload_event = {
            'event_id': 'unload-after-dump-change',
            'event_type': 'driver.trip.unloaded',
            'format_version': 1,
            'occurred_at': (changed_at + timedelta(seconds=1)).isoformat(),
            'sequence': 2,
            'depends_on': [point_event['event_id']],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'trip_id': trip.id,
            'payload': {'trip_id': trip.id},
        }

        results = self.sync(
            [unload_event, point_event],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-device-change-unload',
        ).json()['results']

        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted'])
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.assigned_dump_point_id, self.dump_point.id)
        self.assertEqual(trip.actual_dump_point_id, changed_point.id)
        self.assertEqual(trip.dump_point_id, changed_point.id)
        self.assertEqual(Trip.objects.count(), 1)

    def test_late_equal_timestamp_dump_point_change_cannot_roll_back_newer_state(self):
        self.truck_shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.truck_shift.save(update_fields=['opened_at'])
        point_b = DumpPoint.objects.create(name='Склад равного времени Б')
        event_time = timezone.now() - timedelta(minutes=1)
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
            loaded_at=event_time - timedelta(minutes=1),
        )

        first = {
            'event_id': 'dump-equal-time-first',
            'event_type': 'driver.trip.dump_point_changed',
            'format_version': 1,
            'occurred_at': event_time.isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'trip_id': trip.id,
            'payload': {
                'trip_id': trip.id,
                'dump_point_id': point_b.id,
                'expected_actual_dump_point_id': self.dump_point.id,
            },
        }
        late_rollback = {
            'event_id': 'dump-equal-time-late',
            'event_type': 'driver.trip.dump_point_changed',
            'format_version': 1,
            'occurred_at': event_time.isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'trip_id': trip.id,
            'payload': {
                'trip_id': trip.id,
                'dump_point_id': self.dump_point.id,
                'expected_actual_dump_point_id': point_b.id,
            },
        }

        first_result = self.sync(
            [first], client=self.driver_client(), role_code='driver', device_id='driver-device-equal-a',
        ).json()['results'][0]
        late_result = self.sync(
            [late_rollback], client=self.driver_client(), role_code='driver', device_id='driver-device-equal-b',
        ).json()['results'][0]

        self.assertEqual(first_result['status'], 'accepted', first_result)
        self.assertEqual(late_result['status'], 'conflict', late_result)
        self.assertEqual(late_result['code'], 'stale_dump_point_change')
        trip.refresh_from_db()
        self.assertEqual(trip.actual_dump_point_id, point_b.id)
        self.assertEqual(trip.dump_point_id, point_b.id)

    def test_equal_timestamp_dependent_point_changes_keep_device_order(self):
        self.truck_shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.truck_shift.save(update_fields=['opened_at'])
        point_b = DumpPoint.objects.create(name='Склад одной миллисекунды Б')
        point_c = DumpPoint.objects.create(name='Склад одной миллисекунды В')
        event_time = timezone.now() - timedelta(minutes=1)
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
            loaded_at=event_time - timedelta(minutes=1),
        )

        first = {
            'event_id': 'dump-equal-dependent-first',
            'event_type': 'driver.trip.dump_point_changed',
            'format_version': 1,
            'occurred_at': event_time.isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'trip_id': trip.id,
            'payload': {
                'trip_id': trip.id,
                'dump_point_id': point_b.id,
                'expected_actual_dump_point_id': self.dump_point.id,
            },
        }
        second = {
            'event_id': 'dump-equal-dependent-second',
            'event_type': 'driver.trip.dump_point_changed',
            'format_version': 1,
            'occurred_at': event_time.isoformat(),
            'sequence': 2,
            'depends_on': [first['event_id']],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'trip_id': trip.id,
            'payload': {
                'trip_id': trip.id,
                'dump_point_id': point_c.id,
                'expected_actual_dump_point_id': point_b.id,
            },
        }

        results = self.sync(
            [second, first],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-device-equal-chain',
        ).json()['results']

        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted'])
        trip.refresh_from_db()
        self.assertEqual(trip.actual_dump_point_id, point_c.id)
        self.assertEqual(trip.dump_point_id, point_c.id)

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

    def test_driver_downtime_reason_switch_preserves_separate_intervals_and_total(self):
        self.truck_shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.truck_shift.save(update_fields=['opened_at'])
        first_reason = DowntimeReason.objects.create(
            name='Offline первая причина',
            equipment_type=self.truck_type,
            show_for_truck_driver=True,
        )
        second_reason = DowntimeReason.objects.create(
            name='Offline вторая причина',
            equipment_type=self.truck_type,
            show_for_truck_driver=True,
        )
        first_at = timezone.now() - timedelta(minutes=3)
        switched_at = first_at + timedelta(seconds=40)
        repeated_at = switched_at + timedelta(seconds=15)
        first = {
            'event_id': 'driver-downtime-switch-first',
            'event_type': 'driver.downtime.started',
            'format_version': 1,
            'occurred_at': first_at.isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'payload': {'reason_id': first_reason.id},
        }
        second = {
            'event_id': 'driver-downtime-switch-second',
            'event_type': 'driver.downtime.started',
            'format_version': 1,
            'occurred_at': switched_at.isoformat(),
            'sequence': 2,
            'depends_on': [first['event_id']],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'payload': {'reason_id': second_reason.id},
        }
        repeated = {
            'event_id': 'driver-downtime-switch-second-repeat',
            'event_type': 'driver.downtime.started',
            'format_version': 1,
            'occurred_at': repeated_at.isoformat(),
            'sequence': 3,
            'depends_on': [second['event_id']],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'payload': {'reason_id': second_reason.id},
        }

        results = self.sync(
            [repeated, second, first],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-device-downtime-switch',
        ).json()['results']

        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted', 'accepted'])
        intervals = list(DowntimeEvent.objects.filter(equipment=self.truck).order_by('started_at', 'id'))
        self.assertEqual(len(intervals), 2)
        self.assertEqual(intervals[0].reason, first_reason)
        self.assertEqual(intervals[0].started_at, first_at)
        self.assertEqual(intervals[0].ended_at, switched_at)
        self.assertEqual(intervals[1].reason, second_reason)
        self.assertEqual(intervals[1].started_at, switched_at)
        self.assertIsNone(intervals[1].ended_at)
        by_id = {item['event_id']: item for item in results}
        self.assertEqual(
            by_id[repeated['event_id']]['server_ids']['downtime_event_id'],
            intervals[1].id,
        )

    def test_downtime_start_event_id_is_backward_compatible_local_reference(self):
        self.truck_shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.truck_shift.save(update_fields=['opened_at'])
        reason = DowntimeReason.objects.create(
            name='Проверка совместимости offline простоя',
            equipment_type=self.truck_type,
            show_for_truck_driver=True,
        )
        started_at = timezone.now() - timedelta(minutes=2)
        ended_at = timezone.now() - timedelta(minutes=1)
        started = {
            'event_id': 'driver-downtime-start-fallback',
            'event_type': 'driver.downtime.started',
            'format_version': 1,
            'occurred_at': started_at.isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'payload': {'reason_id': reason.id},
        }
        ended = {
            'event_id': 'driver-downtime-end-fallback',
            'event_type': 'driver.downtime.ended',
            'format_version': 1,
            'occurred_at': ended_at.isoformat(),
            'sequence': 2,
            'depends_on': [started['event_id']],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'local_downtime_id': started['event_id'],
            'payload': {},
        }

        results = self.sync(
            [ended, started],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-device-down-fallback',
        ).json()['results']

        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted'])
        source = OfflineFieldEvent.objects.get(event_id=started['event_id'])
        event = DowntimeEvent.objects.get(reason=reason)
        self.assertEqual(source.local_downtime_id, started['event_id'])
        self.assertEqual(event.started_at, started_at)
        self.assertEqual(event.ended_at, ended_at)

    def test_legacy_excavator_downtime_end_accepts_numeric_active_reference(self):
        self.shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.shift.save(update_fields=['opened_at'])
        reason = DowntimeReason.objects.create(
            name='Legacy numeric offline downtime',
            equipment_type=self.excavator_type,
            show_for_excavator_operator=True,
        )
        started_at = timezone.now() - timedelta(minutes=2)
        ended_at = timezone.now() - timedelta(minutes=1)
        downtime = DowntimeEvent.objects.create(
            equipment=self.excavator,
            employee=self.operator,
            reason=reason,
            started_at=started_at,
        )
        ended = {
            'event_id': 'excavator-downtime-end-legacy-server-id',
            'event_type': 'excavator.downtime.ended',
            'format_version': 1,
            'occurred_at': ended_at.isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.shift.id,
            'equipment_id': self.excavator.id,
            'payload': {'active_downtime_id': str(downtime.id)},
        }

        result = self.sync([ended]).json()['results'][0]

        self.assertEqual(result['status'], 'accepted')
        downtime.refresh_from_db()
        self.assertEqual(downtime.ended_at, ended_at)

    def test_excavator_offline_downtime_switch_uses_the_same_interval_contract(self):
        self.shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.shift.save(update_fields=['opened_at'])
        first_reason = DowntimeReason.objects.create(
            name='Экскаватор offline причина 1',
            equipment_type=self.excavator_type,
            show_for_excavator_operator=True,
        )
        second_reason = DowntimeReason.objects.create(
            name='Экскаватор offline причина 2',
            equipment_type=self.excavator_type,
            show_for_excavator_operator=True,
        )
        first_at = timezone.now() - timedelta(minutes=2)
        switched_at = first_at + timedelta(seconds=25)
        first = {
            'event_id': 'excavator-downtime-switch-first',
            'event_type': 'excavator.downtime.started',
            'format_version': 1,
            'occurred_at': first_at.isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.shift.id,
            'equipment_id': self.excavator.id,
            'payload': {'reason_id': first_reason.id},
        }
        second = {
            'event_id': 'excavator-downtime-switch-second',
            'event_type': 'excavator.downtime.started',
            'format_version': 1,
            'occurred_at': switched_at.isoformat(),
            'sequence': 2,
            'depends_on': [first['event_id']],
            'shift_id': self.shift.id,
            'equipment_id': self.excavator.id,
            'payload': {'reason_id': second_reason.id},
        }

        results = self.sync([second, first]).json()['results']

        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted'])
        intervals = list(DowntimeEvent.objects.filter(equipment=self.excavator).order_by('started_at', 'id'))
        self.assertEqual(len(intervals), 2)
        self.assertEqual(intervals[0].reason, first_reason)
        self.assertEqual(intervals[0].ended_at, switched_at)
        self.assertEqual(intervals[1].reason, second_reason)
        self.assertEqual(intervals[1].started_at, switched_at)
        self.assertIsNone(intervals[1].ended_at)

    def test_legacy_excavator_downtime_end_accepts_local_active_reference(self):
        self.shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.shift.save(update_fields=['opened_at'])
        reason = DowntimeReason.objects.create(
            name='Legacy local offline downtime',
            equipment_type=self.excavator_type,
            show_for_excavator_operator=True,
        )
        started_at = timezone.now() - timedelta(minutes=2)
        ended_at = timezone.now() - timedelta(minutes=1)
        started = {
            'event_id': 'excavator-downtime-start-legacy-local',
            'event_type': 'excavator.downtime.started',
            'format_version': 1,
            'occurred_at': started_at.isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.shift.id,
            'equipment_id': self.excavator.id,
            'payload': {'reason_id': reason.id},
        }
        ended = {
            'event_id': 'excavator-downtime-end-legacy-local',
            'event_type': 'excavator.downtime.ended',
            'format_version': 1,
            'occurred_at': ended_at.isoformat(),
            'sequence': 2,
            'depends_on': [started['event_id']],
            'shift_id': self.shift.id,
            'equipment_id': self.excavator.id,
            'payload': {'active_downtime_id': started['event_id']},
        }

        results = self.sync([ended, started]).json()['results']

        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted'])
        downtime = DowntimeEvent.objects.get(reason=reason)
        self.assertEqual(downtime.started_at, started_at)
        self.assertEqual(downtime.ended_at, ended_at)

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
