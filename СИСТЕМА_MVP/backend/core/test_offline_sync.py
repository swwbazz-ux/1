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
    HaulAssignmentAction,
)
from core.models import OfflineFieldEvent, OfflineFieldEventConflict
from core.offline_sync import normalize_offline_event
from downtimes.models import DowntimeEvent, DowntimeReason
from trips import tests as trip_fixtures
from trips.models import (
    FreeBucketAcceptance,
    FreeBucketAcceptanceStatus,
    Trip,
    TripClientAction,
    TripStatus,
)
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

    def driver_manual_cancel_event(
        self,
        loaded,
        event_id='driver-manual-load-cancel-1',
        sequence=2,
        *,
        trip_id=None,
        occurred_at=None,
    ):
        loaded_at = timezone.datetime.fromisoformat(loaded['occurred_at'])
        return {
            'event_id': event_id,
            'event_type': 'driver.trip.loaded.cancelled',
            'format_version': 1,
            'occurred_at': (occurred_at or loaded_at + timedelta(seconds=1)).isoformat(),
            'sequence': sequence,
            'depends_on': [] if trip_id else [loaded['event_id']],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'trip_id': trip_id,
            'local_trip_id': '' if trip_id else loaded['local_trip_id'],
            'context_snapshot': {
                'source': 'driver_manual',
                'selected_dump_point_id': loaded['payload']['dump_point_id'],
            },
            'payload': {
                'manual_control': True,
                'truck_id': self.truck.id,
                'excavator_id': loaded['payload']['excavator_id'],
                'dump_point_id': loaded['payload']['dump_point_id'],
            },
        }

    def driver_manual_complete_event(
        self,
        loaded,
        event_id='driver-manual-complete-1',
        sequence=2,
        *,
        trip_id=None,
        occurred_at=None,
    ):
        loaded_at = timezone.datetime.fromisoformat(loaded['occurred_at'])
        return {
            'event_id': event_id,
            'event_type': 'driver.trip.manual_completed',
            'format_version': 1,
            'occurred_at': (occurred_at or loaded_at + timedelta(minutes=1)).isoformat(),
            'sequence': sequence,
            'depends_on': [] if trip_id else [loaded['event_id']],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'trip_id': trip_id,
            'local_trip_id': '' if trip_id else loaded['local_trip_id'],
            'context_snapshot': {
                'source': 'driver_manual',
                'action': 'manual_completed',
                'selected_dump_point_id': loaded['payload']['dump_point_id'],
            },
            'payload': {
                'manual_control': True,
                'truck_id': self.truck.id,
                'excavator_id': loaded['payload']['excavator_id'],
                'dump_point_id': loaded['payload']['dump_point_id'],
            },
        }

    def driver_free_bucket_manual_event(
        self,
        event_id='driver-free-bucket-manual-load-1',
        sequence=1,
        *,
        requested_by=None,
        requesting_shift=None,
        accepted_at=None,
        occurred_at=None,
    ):
        from trips.free_bucket import canonical_free_bucket_work_context_snapshot

        event = self.driver_manual_event(
            event_id,
            sequence,
            occurred_at=occurred_at or timezone.now(),
        )
        accepted_at = accepted_at or (
            timezone.datetime.fromisoformat(event['occurred_at']) - timedelta(seconds=1)
        )
        acceptance = FreeBucketAcceptance.objects.create(
            client_acceptance_id=f'acceptance-{event_id}',
            truck=self.truck,
            excavator=self.excavator,
            operator=self.operator,
            loading_shift=self.shift,
            requested_by=requested_by,
            requesting_shift=requesting_shift,
            status=FreeBucketAcceptanceStatus.ACCEPTED,
            occurred_at=accepted_at,
            accepted_at=accepted_at,
            work_context_snapshot=canonical_free_bucket_work_context_snapshot(self.excavator),
        )
        event['context_snapshot']['authority_type'] = 'free_bucket'
        event['context_snapshot']['free_bucket_acceptance_id'] = acceptance.id
        event['payload']['assignment_id'] = None
        event['payload']['free_bucket_acceptance_id'] = acceptance.id
        return event, acceptance

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
        self.assertEqual(trip.driver_id, self.driver.id)
        self.assertEqual(trip.driver_control_shift_id, self.truck_shift.id)
        self.assertTrue(trip.driver_participation_recorded)
        self.assertEqual(
            set(TripClientAction.objects.filter(trip=trip).values_list('action_type', flat=True)),
            {'driver_manual_loaded', 'truck_loaded'},
        )

    def test_late_driver_manual_load_links_completed_excavator_trip(self):
        occurred_at = timezone.now() - timedelta(minutes=5)
        type(self.shift).objects.filter(
            pk__in=[self.shift.id, self.truck_shift.id],
        ).update(opened_at=occurred_at - timedelta(minutes=1))
        HaulAssignment.objects.filter(pk=self.assignment.id).update(
            assigned_at=occurred_at - timedelta(minutes=1),
        )
        self.shift.refresh_from_db()
        self.truck_shift.refresh_from_db()
        self.assignment.refresh_from_db()
        automatic = self.load_event(
            'excavator-completed-before-driver',
            1,
            occurred_at=occurred_at,
        )
        automatic_result = self.sync(
            [automatic], device_id='excavator-completed-device',
        ).json()['results'][0]
        self.assertEqual(automatic_result['status'], 'accepted', automatic_result)
        trip = Trip.objects.get(pk=automatic_result['server_ids']['trip_id'])
        automatic_at = timezone.datetime.fromisoformat(automatic['occurred_at'])
        completed_at = automatic_at + timedelta(minutes=2)
        self.assertTrue(finalize_trip_unloaded(
            trip,
            driver=self.driver,
            unloading_shift=self.truck_shift,
            occurred_at=completed_at,
        ))

        manual = self.driver_manual_event(
            'driver-arrived-after-completed-trip',
            1,
            occurred_at=automatic_at + timedelta(seconds=1),
        )
        result = self.sync(
            [manual],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-late-completed-device',
        ).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        self.assertEqual(result['trip_origin'], 'excavator')
        self.assertEqual(result['server_ids']['trip_id'], trip.id)
        self.assertEqual(Trip.objects.count(), 1)
        trip.refresh_from_db()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.completed_at, completed_at)
        self.assertTrue(TripClientAction.objects.filter(
            trip=trip,
            action_type='driver_manual_loaded',
            client_action_id=manual['event_id'],
        ).exists())

    def test_excavator_first_merge_requires_same_assignment_identity(self):
        automatic = self.load_event('excavator-assignment-a', 1)
        automatic_at = timezone.datetime.fromisoformat(automatic['occurred_at'])
        automatic_result = self.sync(
            [automatic], device_id='excavator-assignment-device',
        ).json()['results'][0]
        self.assertEqual(automatic_result['status'], 'accepted', automatic_result)

        switched_at = automatic_at + timedelta(seconds=1)
        self.assignment.ended_at = switched_at
        self.assignment.save(update_fields=['ended_at'])
        replacement = HaulAssignment.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            assigned_by=self.operator,
            action=HaulAssignmentAction.ASSIGN,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=switched_at,
        )
        HaulAssignment.objects.filter(pk=replacement.pk).update(assigned_at=switched_at)
        replacement.refresh_from_db()

        manual = self.driver_manual_event(
            'driver-assignment-b',
            1,
            occurred_at=switched_at + timedelta(seconds=1),
        )
        manual['payload']['assignment_id'] = replacement.id
        manual['context_snapshot']['assignment_id'] = replacement.id
        result = self.sync(
            [manual],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-assignment-device',
        ).json()['results'][0]

        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'open_trip_changed')
        self.assertEqual(Trip.objects.count(), 1)
        self.assertFalse(TripClientAction.objects.filter(
            action_type='driver_manual_loaded',
            client_action_id=manual['event_id'],
        ).exists())

    def test_old_excavator_event_cannot_attach_to_newer_driver_cycle(self):
        occurred_at = timezone.now() - timedelta(minutes=5)
        type(self.shift).objects.filter(
            pk__in=[self.shift.id, self.truck_shift.id],
        ).update(opened_at=occurred_at - timedelta(minutes=1))
        HaulAssignment.objects.filter(pk=self.assignment.id).update(
            assigned_at=occurred_at - timedelta(minutes=1),
        )
        self.shift.refresh_from_db()
        self.truck_shift.refresh_from_db()
        self.assignment.refresh_from_db()
        first_automatic = self.load_event(
            'excavator-cycle-one',
            1,
            occurred_at=occurred_at,
        )
        first_at = timezone.datetime.fromisoformat(first_automatic['occurred_at'])
        first_result = self.sync(
            [first_automatic], device_id='excavator-cycle-one-device',
        ).json()['results'][0]
        self.assertEqual(first_result['status'], 'accepted', first_result)
        first_trip = Trip.objects.get(pk=first_result['server_ids']['trip_id'])
        completed_at = first_at + timedelta(minutes=2)
        self.assertTrue(finalize_trip_unloaded(
            first_trip,
            driver=self.driver,
            unloading_shift=self.truck_shift,
            occurred_at=completed_at,
        ))

        manual = self.driver_manual_event(
            'driver-cycle-two',
            1,
            occurred_at=completed_at + timedelta(seconds=1),
        )
        manual_result = self.sync(
            [manual],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-cycle-two-device',
        ).json()['results'][0]
        self.assertEqual(manual_result['status'], 'accepted', manual_result)
        second_trip = Trip.objects.get(pk=manual_result['server_ids']['trip_id'])
        second_loaded_at = second_trip.loaded_at

        delayed_old = self.load_event(
            'excavator-old-event-after-new-cycle',
            1,
            occurred_at=first_at + timedelta(seconds=1),
        )
        delayed_result = self.sync(
            [delayed_old], device_id='excavator-delayed-old-device',
        ).json()['results'][0]

        self.assertEqual(delayed_result['status'], 'conflict', delayed_result)
        self.assertEqual(delayed_result['code'], 'open_trip_changed')
        self.assertEqual(Trip.objects.count(), 2)
        second_trip.refresh_from_db()
        self.assertEqual(second_trip.loaded_at, second_loaded_at)
        self.assertFalse(TripClientAction.objects.filter(
            trip=second_trip,
            action_type='truck_loaded',
            client_action_id=delayed_old['event_id'],
        ).exists())

    def test_operator_first_free_bucket_binds_current_driver_on_manual_load(self):
        event, acceptance = self.driver_free_bucket_manual_event()
        result = self.sync(
            [event],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-free-bucket-bind-device',
        ).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        acceptance.refresh_from_db()
        self.assertEqual(acceptance.requested_by_id, self.driver.id)
        self.assertEqual(acceptance.requesting_shift_id, self.truck_shift.id)
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.USED)
        self.assertEqual(Trip.objects.count(), 1)

    def test_driver_manual_free_bucket_trip_can_reroute_to_active_directory_point(self):
        loaded, acceptance = self.driver_free_bucket_manual_event(
            event_id='driver-free-bucket-manual-reroute-load',
        )
        loaded_result = self.sync(
            [loaded],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-free-bucket-reroute-device',
        ).json()['results'][0]
        self.assertEqual(loaded_result['status'], 'accepted', loaded_result)
        trip = Trip.objects.get(pk=loaded_result['server_ids']['trip_id'])
        outside_point = DumpPoint.objects.create(name='Подсыпка у бульдозера')
        changed = {
            'event_id': 'driver-free-bucket-manual-reroute-point',
            'event_type': 'driver.trip.dump_point_changed',
            'format_version': 1,
            'occurred_at': (trip.loaded_at + timedelta(seconds=1)).isoformat(),
            'sequence': 2,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'trip_id': trip.id,
            'payload': {
                'trip_id': trip.id,
                'dump_point_id': outside_point.id,
                'expected_actual_dump_point_id': self.dump_point.id,
            },
        }

        changed_result = self.sync(
            [changed],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-free-bucket-reroute-device',
        ).json()['results'][0]

        self.assertEqual(changed_result['status'], 'accepted', changed_result)
        trip.refresh_from_db()
        self.assertEqual(trip.assigned_dump_point_id, self.dump_point.id)
        self.assertEqual(trip.actual_dump_point_id, outside_point.id)
        self.assertEqual(trip.dump_point_id, outside_point.id)
        acceptance.refresh_from_db()
        self.assertEqual(acceptance.used_trip_id, trip.id)

    def test_driver_cannot_consume_free_bucket_owned_by_another_shift(self):
        event, acceptance = self.driver_free_bucket_manual_event(
            event_id='driver-free-bucket-foreign-owner',
            requested_by=self.operator,
            requesting_shift=self.shift,
        )
        result = self.sync(
            [event],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-free-bucket-owner-device',
        ).json()['results'][0]

        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'free_bucket_request_owner_changed')
        acceptance.refresh_from_db()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.ACCEPTED)
        self.assertIsNone(acceptance.used_trip_id)
        self.assertEqual(Trip.objects.count(), 0)

    def test_driver_free_bucket_load_cannot_predate_acceptance(self):
        event_at = timezone.now()
        event, acceptance = self.driver_free_bucket_manual_event(
            event_id='driver-free-bucket-before-accept',
            occurred_at=event_at,
            accepted_at=event_at + timedelta(seconds=1),
        )
        result = self.sync(
            [event],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-free-bucket-time-device',
        ).json()['results'][0]

        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'free_bucket_load_before_accept')
        acceptance.refresh_from_db()
        self.assertIsNone(acceptance.requested_by_id)
        self.assertIsNone(acceptance.requesting_shift_id)
        self.assertEqual(Trip.objects.count(), 0)

    def test_driver_manual_load_rejects_changed_context_without_creating_trip(self):
        event = self.driver_manual_event()
        event['payload']['loading_block'] = '99'
        result = self.sync(
            [event], client=self.driver_client(), role_code='driver', device_id='driver-device',
        ).json()['results'][0]
        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'manual_work_context_changed')
        self.assertEqual(Trip.objects.count(), 0)

    def test_driver_manual_load_accepts_old_context_marked_before_placement_change(self):
        # Боевой случай 25.09: водитель 43 отметил погрузку у ЭКС-4 с
        # действовавшими тогда настройками забоя. Пользователь штатно сменил
        # их уже ПОСЛЕ отметки, отметка дошла до сервера, когда там уже другие
        # настройки — но сама отметка сделана РАНЬШЕ их изменения, поэтому
        # рейс принимается со СТАРЫМИ настройками (теми, что были на месте
        # погрузки), а не отклоняется конфликтом.
        occurred_at = timezone.now() - timedelta(hours=1)
        type(self.shift).objects.filter(
            pk__in=[self.shift.id, self.truck_shift.id],
        ).update(opened_at=occurred_at - timedelta(minutes=1))
        HaulAssignment.objects.filter(pk=self.assignment.id).update(
            assigned_at=occurred_at - timedelta(minutes=1),
        )
        self.shift.refresh_from_db()
        self.truck_shift.refresh_from_db()
        self.assignment.refresh_from_db()
        event = self.driver_manual_event(occurred_at=occurred_at)

        placement = ExcavatorPlacement.objects.get(excavator=self.excavator)
        placement.loading_block = '99'
        placement.work_context_updated_at = timezone.now() - timedelta(minutes=1)
        placement.save(update_fields=['loading_block', 'work_context_updated_at'])

        result = self.sync(
            [event], client=self.driver_client(), role_code='driver', device_id='driver-device',
        ).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        trip = Trip.objects.get()
        self.assertEqual(trip.loading_block, '4')
        self.assertEqual(trip.loading_horizon, '125')
        self.assertEqual(trip.rock_type_id, self.rock.id)

    def test_driver_manual_load_rejects_stale_context_marked_after_placement_change(self):
        # Симметричный случай: настройки забоя уже поменялись, а отметка
        # сделана ПОСЛЕ этого момента — телефон прислал устаревшие значения,
        # а не «те, что действовали на месте погрузки». Это настоящее
        # устаревание, а не отметка, обогнавшая изменение, и остаётся
        # конфликтом.
        occurred_at = timezone.now()
        type(self.shift).objects.filter(
            pk__in=[self.shift.id, self.truck_shift.id],
        ).update(opened_at=occurred_at - timedelta(minutes=30))
        HaulAssignment.objects.filter(pk=self.assignment.id).update(
            assigned_at=occurred_at - timedelta(minutes=30),
        )
        self.shift.refresh_from_db()
        self.truck_shift.refresh_from_db()
        self.assignment.refresh_from_db()
        event = self.driver_manual_event(occurred_at=occurred_at)

        placement = ExcavatorPlacement.objects.get(excavator=self.excavator)
        placement.loading_block = '99'
        placement.work_context_updated_at = occurred_at - timedelta(minutes=10)
        placement.save(update_fields=['loading_block', 'work_context_updated_at'])

        result = self.sync(
            [event], client=self.driver_client(), role_code='driver', device_id='driver-device',
        ).json()['results'][0]

        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'manual_work_context_changed')
        self.assertEqual(Trip.objects.count(), 0)

    def test_driver_manual_load_accepts_context_resaved_with_same_values_before_mark(self):
        # Пересохранение формы забоя сдвигает placement_updated_at даже без
        # единого изменившегося значения (save_excavator_work_context ставит
        # эту метку всегда). Само по себе пересохранение — не конфликт: если
        # порода, горизонт и блок совпадают, рейс принимается как обычно.
        event = self.driver_manual_event()
        placement = ExcavatorPlacement.objects.get(excavator=self.excavator)
        placement.work_context_updated_at = timezone.now()
        placement.save(update_fields=['work_context_updated_at'])

        result = self.sync(
            [event], client=self.driver_client(), role_code='driver', device_id='driver-device',
        ).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        trip = Trip.objects.get()
        self.assertEqual(trip.loading_block, '4')
        self.assertEqual(trip.rock_type_id, self.rock.id)

    def test_driver_manual_load_rejects_old_context_with_now_invalid_rock_type(self):
        # Отметка сделана до изменения настроек, но порода, которая тогда
        # действовала, с тех пор деактивирована — принимать её нельзя, даже
        # если по времени отметка «успевала».
        from references.models import RockType

        occurred_at = timezone.now() - timedelta(hours=1)
        type(self.shift).objects.filter(
            pk__in=[self.shift.id, self.truck_shift.id],
        ).update(opened_at=occurred_at - timedelta(minutes=1))
        HaulAssignment.objects.filter(pk=self.assignment.id).update(
            assigned_at=occurred_at - timedelta(minutes=1),
        )
        self.shift.refresh_from_db()
        self.truck_shift.refresh_from_db()
        self.assignment.refresh_from_db()
        event = self.driver_manual_event(occurred_at=occurred_at)

        retired_rock_id = self.rock.id
        RockType.objects.filter(pk=retired_rock_id).update(is_active=False)
        placement = ExcavatorPlacement.objects.get(excavator=self.excavator)
        new_rock = RockType.objects.create(name='Новая порода 25.09')
        placement.work_rock_type = new_rock
        placement.work_context_updated_at = timezone.now() - timedelta(minutes=1)
        placement.save(update_fields=['work_rock_type', 'work_context_updated_at'])

        result = self.sync(
            [event], client=self.driver_client(), role_code='driver', device_id='driver-device',
        ).json()['results'][0]

        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'manual_work_context_changed')
        self.assertEqual(Trip.objects.count(), 0)

    def test_manual_load_rejects_separate_unload_after_point_change(self):
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

        self.assertEqual([item['status'] for item in results], ['conflict', 'accepted', 'accepted'])
        self.assertEqual(results[0]['code'], 'driver_manual_unload_not_required')
        self.assertEqual(Trip.objects.count(), 1)
        trip = Trip.objects.get()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertEqual(trip.assigned_dump_point_id, self.dump_point.id)
        self.assertEqual(trip.actual_dump_point_id, changed_point.id)
        self.assertEqual(trip.dump_point_id, changed_point.id)
        self.assertEqual({item['server_ids']['trip_id'] for item in results[1:]}, {trip.id})

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

    def test_leaving_manual_mode_completes_exact_current_cycle_once(self):
        loaded = self.driver_manual_event('manual-cycle-to-exit', 1)
        ended = self.driver_manual_complete_event(loaded)

        results = self.sync(
            [ended, loaded],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-manual-exit-device',
        ).json()['results']

        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted'])
        trip = Trip.objects.get()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.completed_at, timezone.datetime.fromisoformat(ended['occurred_at']))
        self.assertEqual(trip.unloading_shift_id, self.truck_shift.id)
        self.assertIsNotNone(trip.volume_m3)
        self.assertIsNotNone(trip.tonnage)
        self.assertTrue(TripClientAction.objects.filter(
            trip=trip,
            action_type='driver_manual_completed',
            client_action_id=ended['event_id'],
        ).exists())

        repeated = self.sync(
            [ended],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-manual-exit-device',
        ).json()['results'][0]
        self.assertEqual(repeated['status'], 'deduplicated')
        self.assertEqual(Trip.objects.count(), 1)

    def test_driver_manual_load_can_be_cancelled_by_exact_upward_swipe_event(self):
        loaded = self.driver_manual_event('driver-load-to-cancel', 1)
        cancelled = self.driver_manual_cancel_event(loaded)

        results = self.sync(
            [cancelled, loaded],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-cancel-device',
        ).json()['results']

        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted'])
        trip = Trip.objects.get()
        self.assertEqual(trip.status, TripStatus.CANCELLED)
        self.assertEqual({item['server_ids']['trip_id'] for item in results}, {trip.id})
        self.assertTrue(TripClientAction.objects.filter(
            trip=trip,
            action_type='driver_manual_loaded_cancel',
            client_action_id=cancelled['event_id'],
        ).exists())

        repeated = self.sync(
            [cancelled],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-cancel-device',
        ).json()['results'][0]
        self.assertEqual(repeated['status'], 'deduplicated')
        self.assertEqual(Trip.objects.count(), 1)

    def test_driver_manual_cancel_by_server_trip_rejects_second_distinct_event(self):
        loaded = self.driver_manual_event('driver-confirmed-load-to-cancel', 1)
        loaded_result = self.sync(
            [loaded],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-confirmed-cancel-device',
        ).json()['results'][0]
        trip = Trip.objects.get(pk=loaded_result['server_ids']['trip_id'])
        cancelled = self.driver_manual_cancel_event(
            loaded,
            'driver-confirmed-cancel',
            2,
            trip_id=trip.id,
        )
        accepted = self.sync(
            [cancelled],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-confirmed-cancel-device',
        ).json()['results'][0]
        self.assertEqual(accepted['status'], 'accepted', accepted)

        second = self.driver_manual_cancel_event(
            loaded,
            'driver-confirmed-cancel-again',
            3,
            trip_id=trip.id,
            occurred_at=timezone.datetime.fromisoformat(cancelled['occurred_at']) + timedelta(seconds=1),
        )
        rejected = self.sync(
            [second],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-confirmed-cancel-device',
        ).json()['results'][0]
        self.assertEqual(rejected['status'], 'conflict', rejected)
        self.assertEqual(rejected['code'], 'trip_not_cancellable')
        self.assertEqual(Trip.objects.count(), 1)

    def test_driver_cannot_cancel_trip_claimed_by_actual_excavator_load(self):
        loaded = self.driver_manual_event('driver-load-claimed-by-excavator', 1)
        driver_result = self.sync(
            [loaded],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-claimed-device',
        ).json()['results'][0]
        automatic = self.load_event(
            'excavator-claims-driver-load',
            1,
            occurred_at=timezone.datetime.fromisoformat(loaded['occurred_at']) + timedelta(milliseconds=500),
        )
        automatic_result = self.sync(
            [automatic],
            device_id='excavator-claims-device',
        ).json()['results'][0]
        self.assertEqual(automatic_result['server_ids']['trip_id'], driver_result['server_ids']['trip_id'])

        cancelled = self.driver_manual_cancel_event(
            loaded,
            'driver-cancel-after-actual-load',
            2,
            trip_id=driver_result['server_ids']['trip_id'],
        )
        result = self.sync(
            [cancelled],
            client=self.driver_client(),
            role_code='driver',
            device_id='driver-claimed-device',
        ).json()['results'][0]
        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'automatic_load_cannot_be_cancelled_by_driver')
        self.assertEqual(Trip.objects.get().status, TripStatus.LOADED_WAITING_UNLOAD)

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

    def _open_shift_two_hours_ago(self):
        moment = timezone.now() - timedelta(hours=2)
        self.shift.opened_at = moment
        self.shift.save(update_fields=['opened_at'])
        self.truck_shift.opened_at = moment
        self.truck_shift.save(update_fields=['opened_at'])
        self.assignment.assigned_at = moment
        self.assignment.save(update_fields=['assigned_at'])
        return moment

    def _excavator_downtime_event(self, *, event_id, sequence, reason, occurred_at, **extra):
        event = {
            'event_id': event_id,
            'event_type': 'excavator.downtime.started',
            'format_version': 1,
            'occurred_at': occurred_at.isoformat(),
            'sequence': sequence,
            'depends_on': [],
            'shift_id': self.shift.id,
            'equipment_id': self.excavator.id,
            'payload': {'reason_id': reason.id},
        }
        event.update(extra)
        return event

    def _excavator_downtime_reason(self, name):
        return DowntimeReason.objects.create(
            name=name,
            equipment_type=self.excavator_type,
            show_for_excavator_operator=True,
        )

    def test_lagging_clock_blocks_a_downtime_reason_switch(self):
        """Фиксация блокировки: отстающие часы не дают сменить причину простоя.

        Машинист с отведёнными назад часами не может переключить причину
        вообще — новое время оказывается раньше начала текущего простоя.
        """
        self._open_shift_two_hours_ago()
        first = self._excavator_downtime_reason('Экскаватор: первая причина')
        second = self._excavator_downtime_reason('Экскаватор: вторая причина')
        started = self.sync([self._excavator_downtime_event(
            event_id='eo-downtime-a', sequence=1, reason=first, occurred_at=timezone.now(),
        )]).json()['results'][0]
        self.assertEqual(started['status'], 'accepted', started)

        switch = self.sync([self._excavator_downtime_event(
            event_id='eo-downtime-b', sequence=2, reason=second,
            occurred_at=timezone.now() - timedelta(minutes=30),
        )]).json()['results'][0]

        self.assertEqual(switch['status'], 'conflict', switch)
        self.assertEqual(switch['code'], 'downtime_switch_before_start')

    def test_sent_live_unblocks_a_downtime_reason_switch(self):
        """Подмена времени идёт до проверок порядка, поэтому лечит блокировку.

        Тот же сценарий с подсказкой оболочки: переключение проходит, а обе
        записи простоя сходятся на времени расписки сервера — без разрыва.
        """
        self._open_shift_two_hours_ago()
        first = self._excavator_downtime_reason('Экскаватор: первая причина')
        second = self._excavator_downtime_reason('Экскаватор: вторая причина')
        self.sync([self._excavator_downtime_event(
            event_id='eo-live-a', sequence=1, reason=first, occurred_at=timezone.now(),
        )])

        switch = self.sync([self._excavator_downtime_event(
            event_id='eo-live-b', sequence=2, reason=second,
            occurred_at=timezone.now() - timedelta(minutes=30), sent_live=True,
        )]).json()['results'][0]

        self.assertEqual(switch['status'], 'accepted', switch)
        self.assertTrue(switch['device_clock_adjusted'])
        receipt = OfflineFieldEvent.objects.get(event_id='eo-live-b')
        opened = DowntimeEvent.objects.get(reason=second)
        closed = DowntimeEvent.objects.get(reason=first)
        self.assertEqual(opened.started_at, receipt.received_at)
        self.assertEqual(closed.ended_at, receipt.received_at)
        self.assertIsNone(opened.ended_at)

    def test_sent_live_event_is_applied_at_the_server_receipt(self):
        """Событие ушло сразу после нажатия — время сервера и есть настоящее.

        Отстающие часы телефона в пределах смены иначе проходят как есть и
        записывают погрузку задним числом.
        """
        self._open_shift_two_hours_ago()
        device_time = timezone.now() - timedelta(minutes=30)
        event = self.load_event(occurred_at=device_time)
        event['sent_live'] = True

        result = self.sync([event]).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        self.assertTrue(result['device_clock_adjusted'])
        receipt = OfflineFieldEvent.objects.get()
        trip = Trip.objects.get()
        self.assertEqual(receipt.occurred_at, device_time)
        self.assertEqual(trip.loaded_at, receipt.received_at)
        self.assertEqual(trip.load_time_source, 'server_receipt')

    def test_clock_unreliable_hint_inside_payload_is_honoured(self):
        """Подсказка принимается и из payload, и с верхнего уровня события.

        Клиентскую часть пишет другая роль; жёсткая привязка к одному месту
        уже расходилась между параллельными ветками.
        """
        self._open_shift_two_hours_ago()
        device_time = timezone.now() - timedelta(minutes=30)
        event = self.load_event(occurred_at=device_time)
        event['payload'] = dict(event['payload'], clock_unreliable='true')

        result = self.sync([event]).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        self.assertTrue(result['device_clock_adjusted'])
        receipt = OfflineFieldEvent.objects.get()
        self.assertEqual(receipt.occurred_at, device_time)
        self.assertEqual(Trip.objects.get().loaded_at, receipt.received_at)

    def test_offline_event_without_clock_hints_keeps_its_device_time(self):
        """Без подсказок поведение не меняется: очередь сохраняет хронологию.

        Отложенное офлайн-событие обязано остаться на своём времени, иначе
        накопленная за смену очередь схлопнется в момент восстановления связи.
        """
        self._open_shift_two_hours_ago()
        device_time = timezone.now() - timedelta(minutes=30)
        event = self.load_event(occurred_at=device_time)

        result = self.sync([event]).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        self.assertNotIn('device_clock_adjusted', result)
        trip = Trip.objects.get()
        self.assertEqual(trip.loaded_at, device_time)
        self.assertEqual(trip.load_time_source, 'excavator_device')

    def test_excavator_clock_one_minute_behind_is_accepted_without_adjustment(self):
        ten_minutes_ago = timezone.now() - timedelta(minutes=10)
        self.shift.opened_at = ten_minutes_ago
        self.shift.save(update_fields=['opened_at'])
        self.truck_shift.opened_at = ten_minutes_ago
        self.truck_shift.save(update_fields=['opened_at'])
        self.assignment.assigned_at = ten_minutes_ago
        self.assignment.save(update_fields=['assigned_at'])
        device_occurred_at = timezone.now() - timedelta(minutes=1)
        event = self.load_event(occurred_at=device_occurred_at)

        result = self.sync([event]).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        self.assertNotIn('device_clock_adjusted', result)
        receipt = OfflineFieldEvent.objects.get()
        trip = Trip.objects.get()
        self.assertEqual(receipt.occurred_at, device_occurred_at)
        self.assertEqual(trip.loaded_at, device_occurred_at)
        self.assertEqual(trip.load_time_source, 'excavator_device')

    def test_excavator_clock_ahead_uses_server_time_without_blocking_work(self):
        device_occurred_at = timezone.now() + timedelta(minutes=6)
        event = self.load_event(occurred_at=device_occurred_at)
        result = self.sync([event]).json()['results'][0]
        self.assertEqual(result['status'], 'accepted', result)
        self.assertTrue(result['device_clock_adjusted'])
        receipt = OfflineFieldEvent.objects.get()
        trip = Trip.objects.get()
        self.assertEqual(receipt.status, 'accepted')
        self.assertEqual(receipt.occurred_at, device_occurred_at)
        self.assertEqual(trip.loaded_at, receipt.received_at)
        self.assertEqual(trip.load_received_at, receipt.received_at)
        self.assertEqual(trip.load_time_source, 'server_receipt')
        self.assertEqual(result['device_occurred_at'], device_occurred_at.isoformat())
        self.assertEqual(result['effective_occurred_at'], receipt.received_at.isoformat())

    def test_legacy_excavator_clock_conflict_replays_same_event_without_duplicate(self):
        received_at = timezone.now()
        device_occurred_at = received_at + timedelta(minutes=6)
        event = self.load_event(event_id='legacy-clock-load', occurred_at=device_occurred_at)
        normalized = normalize_offline_event(
            event,
            role_code='excavator_operator',
            device_id='device-test-001',
            received_at=received_at,
        )
        OfflineFieldEvent.objects.create(
            event_id=event['event_id'],
            event_type=event['event_type'],
            format_version=event['format_version'],
            actor=self.operator,
            access=self.access,
            role_code='excavator_operator',
            device_id='device-test-001',
            sequence=event['sequence'],
            depends_on=event['depends_on'],
            occurred_at=device_occurred_at,
            received_at=received_at,
            shift=self.shift,
            equipment=self.excavator,
            local_trip_id=event['local_trip_id'],
            context_snapshot=event['context_snapshot'],
            payload=event['payload'],
            fingerprint=normalized['fingerprint'],
            status='conflict',
            error_code='device_clock_ahead',
            error_message='Часы устройства заметно опережают сервер. Требуется сверка.',
        )

        result = self.sync([event]).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        self.assertEqual(OfflineFieldEvent.objects.count(), 1)
        self.assertEqual(Trip.objects.count(), 1)
        receipt = OfflineFieldEvent.objects.get()
        self.assertEqual(receipt.status, 'accepted')
        self.assertEqual(receipt.occurred_at, device_occurred_at)
        self.assertEqual(Trip.objects.get().loaded_at, received_at)

    def _store_legacy_conflicted_event(self, event, *, received_at, error_code, error_message):
        """Recreate a receipt saved by a shell that had no clock recovery."""
        normalized = normalize_offline_event(
            event,
            role_code='excavator_operator',
            device_id='device-test-001',
            received_at=received_at,
        )
        return OfflineFieldEvent.objects.create(
            event_id=event['event_id'],
            event_type=event['event_type'],
            format_version=event['format_version'],
            actor=self.operator,
            access=self.access,
            role_code='excavator_operator',
            device_id='device-test-001',
            sequence=event['sequence'],
            depends_on=event['depends_on'],
            occurred_at=timezone.datetime.fromisoformat(event['occurred_at']),
            received_at=received_at,
            shift=self.shift,
            equipment=self.excavator,
            local_trip_id=event.get('local_trip_id', ''),
            context_snapshot=event.get('context_snapshot', {}),
            payload=event['payload'],
            fingerprint=normalized['fingerprint'],
            status='conflict',
            error_code=error_code,
            error_message=error_message,
        )

    def _open_shift_context_earlier(self, delta):
        moment = timezone.now() - delta
        self.shift.opened_at = moment
        self.shift.save(update_fields=['opened_at'])
        self.truck_shift.opened_at = moment
        self.truck_shift.save(update_fields=['opened_at'])
        self.assignment.assigned_at = moment
        self.assignment.save(update_fields=['assigned_at'])
        return moment

    def test_dependency_chain_recovers_when_only_the_parent_clock_was_ahead(self):
        """The phone may resync its clock between two queued offline actions.

        The dependent event then carries an honest timestamp and is rejected
        only because its parent was held by ``device_clock_ahead``.  Такая
        цепочка обязана восстанавливаться целиком, иначе второе действие
        машиниста теряется навсегда.
        """
        self._open_shift_context_earlier(timedelta(minutes=30))
        received_at = timezone.now() - timedelta(minutes=2)
        loaded = self.load_event(
            'chain-load', 1, occurred_at=received_at + timedelta(minutes=40),
        )
        cancelled = {
            'event_id': 'chain-cancel',
            'event_type': 'excavator.trip.loaded.cancelled',
            'format_version': 1,
            'occurred_at': (received_at - timedelta(seconds=30)).isoformat(),
            'sequence': 2,
            'depends_on': [loaded['event_id']],
            'shift_id': self.shift.id,
            'equipment_id': self.excavator.id,
            'local_trip_id': loaded['local_trip_id'],
            'payload': {'local_trip_id': loaded['local_trip_id']},
        }
        self._store_legacy_conflicted_event(
            loaded,
            received_at=received_at,
            error_code='device_clock_ahead',
            error_message='Часы устройства заметно опережают сервер. Требуется сверка.',
        )
        self._store_legacy_conflicted_event(
            cancelled,
            received_at=received_at,
            error_code='dependency_rejected',
            error_message='Предыдущее событие требует сверки или отклонено.',
        )

        results = self.sync([loaded, cancelled]).json()['results']

        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted'], results)
        self.assertEqual(OfflineFieldEvent.objects.count(), 2)
        trip = Trip.objects.get()
        self.assertEqual(trip.status, TripStatus.CANCELLED)
        self.assertEqual(trip.loaded_at, received_at)
        self.assertEqual(trip.cancelled_at, received_at)

    def test_real_conflict_chain_is_not_reopened_by_clock_recovery(self):
        """Настоящий доменный конфликт родителя остаётся терминальным."""
        self._open_shift_context_earlier(timedelta(minutes=30))
        received_at = timezone.now() - timedelta(minutes=2)
        loaded = self.load_event(
            'real-conflict-load', 1, occurred_at=received_at - timedelta(seconds=5),
        )
        cancelled = {
            'event_id': 'real-conflict-cancel',
            'event_type': 'excavator.trip.loaded.cancelled',
            'format_version': 1,
            'occurred_at': (received_at - timedelta(seconds=1)).isoformat(),
            'sequence': 2,
            'depends_on': [loaded['event_id']],
            'shift_id': self.shift.id,
            'equipment_id': self.excavator.id,
            'local_trip_id': loaded['local_trip_id'],
            'payload': {'local_trip_id': loaded['local_trip_id']},
        }
        self._store_legacy_conflicted_event(
            loaded,
            received_at=received_at,
            error_code='open_trip_changed',
            error_message='Незакрытый рейс самосвала уже изменился.',
        )
        self._store_legacy_conflicted_event(
            cancelled,
            received_at=received_at,
            error_code='dependency_rejected',
            error_message='Предыдущее событие требует сверки или отклонено.',
        )

        results = self.sync([loaded, cancelled]).json()['results']

        self.assertEqual([item['status'] for item in results], ['conflict', 'conflict'], results)
        self.assertEqual(results[0]['code'], 'open_trip_changed')
        self.assertEqual(results[1]['code'], 'dependency_rejected')
        self.assertEqual(Trip.objects.count(), 0)

    def test_excavator_wrong_timezone_behind_shift_start_uses_server_time(self):
        """Часовой пояс может увести время устройства и назад, не только вперёд.

        Действие, привязанное к смене, не может произойти раньше её открытия.
        Машинист обязан продолжать работу так же, как водитель.
        """
        self.shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.shift.save(update_fields=['opened_at'])
        reason = DowntimeReason.objects.create(
            name='Экскаватор: часовой пояс назад',
            equipment_type=self.excavator_type,
            show_for_excavator_operator=True,
        )
        device_occurred_at = timezone.now() - timedelta(hours=10)
        event = {
            'event_id': 'excavator-clock-timezone-behind',
            'event_type': 'excavator.downtime.started',
            'format_version': 1,
            'occurred_at': device_occurred_at.isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.shift.id,
            'equipment_id': self.excavator.id,
            'payload': {'reason_id': reason.id},
        }

        result = self.sync([event]).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        self.assertTrue(result['device_clock_adjusted'])
        receipt = OfflineFieldEvent.objects.get(event_id=event['event_id'])
        downtime = DowntimeEvent.objects.get(reason=reason)
        self.assertEqual(receipt.occurred_at, device_occurred_at)
        self.assertEqual(downtime.started_at, receipt.received_at)

    def test_excavator_event_before_shift_stays_a_conflict_when_receipt_is_outside(self):
        """Коррекция не превращается в универсальную отмену проверки смены."""
        self.shift.opened_at = timezone.now() + timedelta(minutes=30)
        self.shift.save(update_fields=['opened_at'])
        reason = DowntimeReason.objects.create(
            name='Экскаватор: смена ещё не открыта',
            equipment_type=self.excavator_type,
            show_for_excavator_operator=True,
        )
        event = {
            'event_id': 'excavator-before-shift',
            'event_type': 'excavator.downtime.started',
            'format_version': 1,
            'occurred_at': timezone.now().isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.shift.id,
            'equipment_id': self.excavator.id,
            'payload': {'reason_id': reason.id},
        }

        result = self.sync([event]).json()['results'][0]

        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'event_before_shift')
        self.assertFalse(DowntimeEvent.objects.filter(reason=reason).exists())

    def test_late_event_after_shift_close_is_accepted_into_its_own_shift_history(self):
        """Телефон — источник истины: связь подвела, событие честно опоздало,
        но реально случилось до того, как смена была открыта на сервере в
        этом окне. Раньше это был отказ (event_after_shift); теперь событие
        ложится в историю СВОЕЙ смены её собственным временем."""
        opened_at = timezone.now() - timedelta(hours=6)
        closed_at = timezone.now() - timedelta(hours=1)
        self.shift.opened_at = opened_at
        self.shift.closed_at = closed_at
        self.shift.save(update_fields=['opened_at', 'closed_at'])
        reason = DowntimeReason.objects.create(
            name='Простой, опоздавший после закрытия смены',
            equipment_type=self.excavator_type,
            show_for_excavator_operator=True,
        )
        device_occurred_at = closed_at + timedelta(minutes=5)
        event = {
            'event_id': 'excavator-after-shift',
            'event_type': 'excavator.downtime.started',
            'format_version': 1,
            'occurred_at': device_occurred_at.isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.shift.id,
            'equipment_id': self.excavator.id,
            'payload': {'reason_id': reason.id},
        }

        result = self.sync([event]).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        downtime = DowntimeEvent.objects.get(reason=reason)
        self.assertEqual(downtime.equipment_id, self.excavator.id)
        self.assertEqual(downtime.started_at, device_occurred_at)

    def test_excavator_downtime_clock_ahead_uses_server_time(self):
        self.shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.shift.save(update_fields=['opened_at'])
        reason = DowntimeReason.objects.create(
            name='Простой при неверных часах',
            equipment_type=self.excavator_type,
            show_for_excavator_operator=True,
        )
        device_occurred_at = timezone.now() + timedelta(minutes=6)
        event = {
            'event_id': 'excavator-clock-downtime-start',
            'event_type': 'excavator.downtime.started',
            'format_version': 1,
            'occurred_at': device_occurred_at.isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.shift.id,
            'equipment_id': self.excavator.id,
            'payload': {'reason_id': reason.id},
        }

        result = self.sync([event]).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        receipt = OfflineFieldEvent.objects.get()
        downtime = DowntimeEvent.objects.get(reason=reason)
        self.assertEqual(receipt.occurred_at, device_occurred_at)
        self.assertEqual(downtime.started_at, receipt.received_at)
        self.assertTrue(result['device_clock_adjusted'])

    def test_driver_clock_ahead_no_longer_blocks_the_action(self):
        """A future driver clock is tolerated the same way an excavator's is.

        This case used to be rejected with ``device_clock_ahead``, which stopped
        a driver whose phone was manually set forward. The device value stays in
        the receipt for audit while the action runs on server receipt time.
        """
        self.truck_shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.truck_shift.save(update_fields=['opened_at'])
        reason = DowntimeReason.objects.create(
            name='Driver future clock',
            equipment_type=self.truck_type,
            show_for_truck_driver=True,
        )
        device_occurred_at = timezone.now() + timedelta(minutes=6)
        event = {
            'event_id': 'driver-clock-downtime-start',
            'event_type': 'driver.downtime.started',
            'format_version': 1,
            'occurred_at': device_occurred_at.isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'payload': {'reason_id': reason.id},
        }

        result = self.sync(
            [event], client=self.driver_client(), role_code='driver', device_id='driver-clock-device',
        ).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        self.assertTrue(result['device_clock_adjusted'])
        self.assertEqual(result['time_source'], 'server_receipt')
        receipt = OfflineFieldEvent.objects.get(event_id=event['event_id'])
        downtime = DowntimeEvent.objects.get(reason=reason)
        self.assertEqual(receipt.occurred_at, device_occurred_at)
        self.assertEqual(downtime.started_at, receipt.received_at)
        self.assertEqual(DowntimeEvent.objects.filter(reason=reason).count(), 1)

    def driver_downtime_event(self, *, event_id, sequence, reason, occurred_at, depends_on=()):
        return {
            'event_id': event_id,
            'event_type': 'driver.downtime.started',
            'format_version': 1,
            'occurred_at': occurred_at.isoformat(),
            'sequence': sequence,
            'depends_on': list(depends_on),
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'payload': {'reason_id': reason.id},
        }

    def test_driver_clock_one_minute_behind_is_accepted_without_adjustment(self):
        self.truck_shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.truck_shift.save(update_fields=['opened_at'])
        reason = DowntimeReason.objects.create(
            name='Driver clock one minute behind',
            equipment_type=self.truck_type,
            show_for_truck_driver=True,
        )
        device_occurred_at = timezone.now() - timedelta(minutes=1)
        event = self.driver_downtime_event(
            event_id='driver-clock-behind', sequence=1,
            reason=reason, occurred_at=device_occurred_at,
        )

        result = self.sync(
            [event], client=self.driver_client(), role_code='driver',
            device_id='driver-clock-behind-device',
        ).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        self.assertNotIn('device_clock_adjusted', result)
        receipt = OfflineFieldEvent.objects.get(event_id=event['event_id'])
        downtime = DowntimeEvent.objects.get(reason=reason)
        self.assertEqual(receipt.occurred_at, device_occurred_at)
        self.assertEqual(downtime.started_at, device_occurred_at)

    def test_driver_wrong_timezone_ahead_uses_server_time_and_is_idempotent(self):
        self.truck_shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.truck_shift.save(update_fields=['opened_at'])
        reason = DowntimeReason.objects.create(
            name='Driver wrong timezone',
            equipment_type=self.truck_type,
            show_for_truck_driver=True,
        )
        device_occurred_at = timezone.now() + timedelta(hours=10)
        event = self.driver_downtime_event(
            event_id='driver-clock-timezone', sequence=1,
            reason=reason, occurred_at=device_occurred_at,
        )
        client = self.driver_client()

        first = self.sync(
            [event], client=client, role_code='driver', device_id='driver-clock-timezone-device',
        ).json()['results'][0]
        repeated = self.sync(
            [event], client=client, role_code='driver', device_id='driver-clock-timezone-device',
        ).json()['results'][0]

        self.assertEqual(first['status'], 'accepted', first)
        self.assertTrue(first['device_clock_adjusted'])
        self.assertEqual(first['time_source'], 'server_receipt')
        self.assertEqual(repeated['status'], 'deduplicated', repeated)
        receipt = OfflineFieldEvent.objects.get(event_id=event['event_id'])
        downtime = DowntimeEvent.objects.get(reason=reason)
        self.assertEqual(receipt.occurred_at, device_occurred_at)
        self.assertEqual(downtime.started_at, receipt.received_at)
        self.assertEqual(first['device_occurred_at'], device_occurred_at.isoformat())
        self.assertEqual(first['effective_occurred_at'], receipt.received_at.isoformat())
        self.assertEqual(DowntimeEvent.objects.filter(reason=reason).count(), 1)

    def test_driver_wrong_timezone_behind_shift_start_uses_server_time(self):
        self.truck_shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.truck_shift.save(update_fields=['opened_at'])
        reason = DowntimeReason.objects.create(
            name='Driver wrong timezone behind',
            equipment_type=self.truck_type,
            show_for_truck_driver=True,
        )
        device_occurred_at = timezone.now() - timedelta(hours=10)
        event = self.driver_downtime_event(
            event_id='driver-clock-timezone-behind', sequence=1,
            reason=reason, occurred_at=device_occurred_at,
        )

        result = self.sync(
            [event], client=self.driver_client(), role_code='driver',
            device_id='driver-clock-timezone-behind-device',
        ).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        self.assertTrue(result['device_clock_adjusted'])
        self.assertEqual(result['time_source'], 'server_receipt')
        receipt = OfflineFieldEvent.objects.get(event_id=event['event_id'])
        downtime = DowntimeEvent.objects.get(reason=reason)
        self.assertEqual(receipt.occurred_at, device_occurred_at)
        self.assertEqual(downtime.started_at, receipt.received_at)

    def test_legacy_driver_clock_conflict_and_dependent_chain_recover_in_order(self):
        self.truck_shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.truck_shift.save(update_fields=['opened_at'])
        reason = DowntimeReason.objects.create(
            name='Driver legacy clock chain',
            equipment_type=self.truck_type,
            show_for_truck_driver=True,
        )
        received_at = timezone.now()
        device_started_at = received_at + timedelta(hours=8)
        # The phone clock was corrected before the dependent action. Its own
        # timestamp is valid; recovery must still follow the now-accepted
        # clock-conflict parent rather than leaving the chain terminal.
        device_ended_at = received_at + timedelta(seconds=30)
        started = self.driver_downtime_event(
            event_id='driver-legacy-clock-start', sequence=1,
            reason=reason, occurred_at=device_started_at,
        )
        ended = {
            'event_id': 'driver-legacy-clock-end',
            'event_type': 'driver.downtime.ended',
            'format_version': 1,
            'occurred_at': device_ended_at.isoformat(),
            'sequence': 2,
            'depends_on': [started['event_id']],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'local_downtime_id': started['event_id'],
            'payload': {'local_downtime_id': started['event_id']},
        }
        for raw, code in ((started, 'device_clock_ahead'), (ended, 'dependency_rejected')):
            normalized = normalize_offline_event(
                raw, role_code='driver', device_id='driver-legacy-clock-device',
                received_at=received_at,
            )
            OfflineFieldEvent.objects.create(
                event_id=raw['event_id'], event_type=raw['event_type'],
                format_version=raw['format_version'], actor=self.driver,
                access=self.driver_access, role_code='driver',
                device_id='driver-legacy-clock-device', sequence=raw['sequence'],
                depends_on=raw['depends_on'], occurred_at=timezone.datetime.fromisoformat(raw['occurred_at']),
                received_at=received_at, shift=self.truck_shift, equipment=self.truck,
                local_downtime_id=normalized['local_downtime_id'],
                context_snapshot=normalized['context_snapshot'], payload=normalized['payload'],
                fingerprint=normalized['fingerprint'], status='conflict', error_code=code,
                error_message='legacy clock conflict',
            )

        results = self.sync(
            [ended, started], client=self.driver_client(), role_code='driver',
            device_id='driver-legacy-clock-device',
        ).json()['results']

        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted'])
        interval = DowntimeEvent.objects.get(reason=reason)
        self.assertEqual(interval.started_at, received_at)
        self.assertEqual(interval.ended_at, device_ended_at)
        self.assertEqual(OfflineFieldEvent.objects.filter(status='accepted').count(), 2)
        repeated = self.sync(
            [ended, started], client=self.driver_client(), role_code='driver',
            device_id='driver-legacy-clock-device',
        ).json()['results']
        self.assertEqual([item['status'] for item in repeated], ['deduplicated', 'deduplicated'])
        self.assertEqual(DowntimeEvent.objects.filter(reason=reason).count(), 1)

    def test_driver_clock_adjustment_does_not_hide_real_context_conflict(self):
        self.truck_shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.truck_shift.save(update_fields=['opened_at'])
        reason = DowntimeReason.objects.create(
            name='Driver real conflict',
            equipment_type=self.truck_type,
            show_for_truck_driver=True,
        )
        event = self.driver_downtime_event(
            event_id='driver-clock-real-conflict', sequence=1, reason=reason,
            occurred_at=timezone.now() + timedelta(hours=9),
        )
        event['equipment_id'] = self.excavator.id

        result = self.sync(
            [event], client=self.driver_client(), role_code='driver',
            device_id='driver-clock-real-conflict-device',
        ).json()['results'][0]

        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(result['code'], 'equipment_context_changed')
        self.assertFalse(DowntimeEvent.objects.filter(reason=reason).exists())

    def test_driver_unload_clock_ahead_uses_server_operational_time(self):
        self.truck_shift.opened_at = timezone.now() - timedelta(minutes=10)
        self.truck_shift.save(update_fields=['opened_at'])
        loaded_at = timezone.now() - timedelta(minutes=2)
        trip = Trip.objects.create(
            excavator=self.excavator, truck=self.truck,
            excavator_operator=self.operator, loading_shift=self.shift,
            rock_type=self.rock, dump_point=self.dump_point,
            assigned_dump_point=self.dump_point,
            driver_participation_recorded=True,
            driver_control_shift=self.truck_shift,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            loaded_at=loaded_at,
        )
        device_occurred_at = timezone.now() + timedelta(minutes=6)
        event = {
            'event_id': 'driver-clock-unload',
            'event_type': 'driver.trip.unloaded',
            'format_version': 1,
            'occurred_at': device_occurred_at.isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'trip_id': trip.id,
            'payload': {'trip_id': trip.id},
        }

        result = self.sync(
            [event], client=self.driver_client(), role_code='driver',
            device_id='driver-clock-unload-device',
        ).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        trip.refresh_from_db()
        receipt = OfflineFieldEvent.objects.get(event_id=event['event_id'])
        self.assertEqual(receipt.occurred_at, device_occurred_at)
        self.assertEqual(trip.completed_at, receipt.received_at)
        self.assertEqual(trip.unload_received_at, receipt.received_at)
        self.assertEqual(trip.unload_time_source, 'server_receipt')

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
    create_registered_driver_shift = (
        trip_fixtures.ExcavatorWorkServerIntegrationTests.create_registered_driver_shift
    )

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Тест конкурентности требует отдельную PostgreSQL-БД.')
        with connection.cursor() as cursor:
            for sql in connection.ops.sequence_reset_sql(no_style(), apps.get_models()):
                cursor.execute(sql)
        DowntimeReason.objects.get_or_create(
            name='Ожидание самосвалов',
            defaults={
                'short_label': 'Ожидание самосвалов',
                'show_for_excavator_operator': True,
            },
        )
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
        driver_client = Client()
        driver_session = driver_client.session
        driver_session['employee_access_id'] = self.driver_access.id
        driver_session.save()
        self.driver_session_cookie = driver_client.cookies['sessionid'].value

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
                'loading_horizon': '125',
                'loading_block': '4',
                'transport_distance_km': '4.20',
            },
        }

    def driver_event(self, event_id, sequence):
        return OfflineEventSyncTests.driver_manual_event(self, event_id, sequence)

    def driver_cancel_event(self, loaded, event_id, sequence, trip_id):
        return OfflineEventSyncTests.driver_manual_cancel_event(
            self,
            loaded,
            event_id,
            sequence,
            trip_id=trip_id,
        )

    def post_from_thread(
        self,
        event,
        device_id,
        *,
        role_code='excavator_operator',
        session_cookie=None,
    ):
        close_old_connections()
        client = Client()
        client.cookies['sessionid'] = session_cookie or self.session_cookie
        response = client.post(
            self.url,
            data=json.dumps({
                'protocol_version': 1,
                'role_code': role_code,
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

    def test_same_driver_manual_event_parallel_requests_create_one_trip(self):
        event = self.driver_event('parallel-driver-same-event', 1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _: self.post_from_thread(
                    event,
                    'parallel-driver-device-001',
                    role_code='driver',
                    session_cookie=self.driver_session_cookie,
                ),
                range(2),
            ))

        self.assertEqual([item[0] for item in results], [200, 200])
        self.assertEqual(
            sorted(item[1]['status'] for item in results),
            ['accepted', 'deduplicated'],
        )
        self.assertEqual(Trip.objects.count(), 1)
        trip = Trip.objects.get()
        self.assertEqual(OfflineFieldEvent.objects.count(), 1)
        self.assertEqual(
            TripClientAction.objects.filter(
                trip=trip,
                action_type='driver_manual_loaded',
            ).count(),
            1,
        )

    def test_driver_and_excavator_parallel_loads_converge_to_one_trip(self):
        driver_event = self.driver_event('parallel-driver-load', 1)
        excavator_event = self.event('parallel-excavator-load', 1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    self.post_from_thread,
                    driver_event,
                    'parallel-driver-device-002',
                    role_code='driver',
                    session_cookie=self.driver_session_cookie,
                ),
                pool.submit(
                    self.post_from_thread,
                    excavator_event,
                    'parallel-excavator-device-002',
                ),
            ]
            results = [future.result() for future in futures]

        trip = Trip.objects.get()
        diagnostic = {
            'results': results,
            'trip': {
                'excavator_id': trip.excavator_id,
                'truck_id': trip.truck_id,
                'dump_point_id': trip.dump_point_id,
                'rock_type_id': trip.rock_type_id,
                'loading_horizon': trip.loading_horizon,
                'loading_block': trip.loading_block,
                'free_bucket_acceptance_id': getattr(trip, 'free_bucket_acceptance_id', None),
            },
            'driver_payload': driver_event['payload'],
        }
        self.assertEqual([item[0] for item in results], [200, 200], results)
        self.assertEqual(
            [item[1]['status'] for item in results],
            ['accepted', 'accepted'],
            diagnostic,
        )
        self.assertEqual(Trip.objects.count(), 1)
        self.assertEqual(
            {item[1]['server_ids']['trip_id'] for item in results},
            {trip.id},
        )
        self.assertEqual(
            set(TripClientAction.objects.filter(trip=trip).values_list('action_type', flat=True)),
            {'driver_manual_loaded', 'truck_loaded'},
        )
        self.assertEqual(
            set(OfflineFieldEvent.objects.filter(trip=trip).values_list('event_type', flat=True)),
            {'driver.trip.loaded', 'excavator.trip.loaded'},
        )
        self.assertEqual(trip.driver_id, self.driver.id)
        self.assertEqual(trip.driver_control_shift_id, self.truck_shift.id)
        self.assertTrue(trip.driver_participation_recorded)

    def test_driver_cancel_racing_actual_excavator_load_keeps_one_open_trip(self):
        driver_event = self.driver_event('parallel-driver-before-cancel', 1)
        _, driver_result = self.post_from_thread(
            driver_event,
            'parallel-driver-cancel-device',
            role_code='driver',
            session_cookie=self.driver_session_cookie,
        )
        self.assertEqual(driver_result['status'], 'accepted', driver_result)
        first_trip_id = driver_result['server_ids']['trip_id']
        cancel_event = self.driver_cancel_event(
            driver_event,
            'parallel-driver-cancel',
            2,
            first_trip_id,
        )
        excavator_event = self.event('parallel-excavator-actual-after-cancel', 1)
        excavator_event['occurred_at'] = (
            timezone.datetime.fromisoformat(driver_event['occurred_at']) + timedelta(milliseconds=500)
        ).isoformat()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    self.post_from_thread,
                    cancel_event,
                    'parallel-driver-cancel-device',
                    role_code='driver',
                    session_cookie=self.driver_session_cookie,
                ),
                pool.submit(
                    self.post_from_thread,
                    excavator_event,
                    'parallel-excavator-after-cancel-device',
                ),
            ]
            results = [future.result() for future in futures]

        self.assertEqual([item[0] for item in results], [200, 200], results)
        self.assertEqual(results[1][1]['status'], 'accepted', results)
        self.assertIn(results[0][1]['status'], {'accepted', 'conflict'}, results)
        self.assertEqual(
            Trip.objects.filter(status=TripStatus.LOADED_WAITING_UNLOAD).count(),
            1,
            results,
        )
        self.assertEqual(
            TripClientAction.objects.filter(action_type='truck_loaded').count(),
            1,
            results,
        )

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
