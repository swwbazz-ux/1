import json
from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
from core.models import OfflineFieldEvent
from trips import tests as trip_fixtures
from trips.models import FreeBucketAcceptance, FreeBucketAcceptanceStatus, Trip, TripStatus
from users.models import EmployeeAccess


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class FreeBucketServerIntegrationTests(TestCase):
    """Server contract for a one-load acceptance that does not reassign a truck."""

    create_registered_driver_shift = (
        trip_fixtures.ExcavatorWorkServerIntegrationTests.create_registered_driver_shift
    )

    def setUp(self):
        trip_fixtures.ExcavatorWorkServerIntegrationTests.setUp(self)
        from shifts.models import EmployeeShift

        self.url = reverse('offline_events_sync')
        self.shift = EmployeeShift.objects.get(employee=self.operator, closed_at__isnull=True)
        self.assignment = HaulAssignment.objects.get(
            truck=self.truck,
            excavator=self.excavator,
            status=AssignmentStatus.ACCEPTED,
        )

    def event(
        self,
        event_id,
        event_type,
        sequence,
        *,
        payload,
        occurred_at=None,
        depends_on=None,
        actor=None,
        access=None,
        shift=None,
        excavator=None,
        local_trip_id='',
    ):
        actor = actor or self.operator
        access = access or self.access
        shift = shift or self.shift
        excavator = excavator or self.excavator
        return {
            'event_id': event_id,
            'event_type': event_type,
            'format_version': 1,
            'actor_id': actor.id,
            'access_id': access.id,
            'role_code': 'excavator_operator',
            'occurred_at': (occurred_at or timezone.now()).isoformat(),
            'sequence': sequence,
            'depends_on': list(depends_on or []),
            'shift_id': shift.id,
            'equipment_id': excavator.id,
            'local_trip_id': local_trip_id,
            'context_snapshot': {
                'actor_id': actor.id,
                'access_id': access.id,
                'role_code': 'excavator_operator',
            },
            'payload': payload,
        }

    def sync(self, events, *, client=None, actor=None, access=None, device_id='free-bucket-device-001'):
        actor = actor or self.operator
        access = access or self.access
        return (client or self.client).post(
            self.url,
            data=json.dumps({
                'protocol_version': 1,
                'actor_id': actor.id,
                'access_id': access.id,
                'role_code': 'excavator_operator',
                'device_id': device_id,
                'events': events,
            }),
            content_type='application/json',
        )

    def accept_event(self, event_id='free-accept-1', sequence=1, **identity):
        return self.event(
            event_id,
            'excavator.free_bucket.accepted',
            sequence,
            payload={'truck_id': self.truck.id},
            **identity,
        )

    def load_event(
        self,
        acceptance_event,
        event_id='free-load-1',
        sequence=2,
        *,
        occurred_at=None,
        **identity,
    ):
        occurred_at = occurred_at or (
            timezone.datetime.fromisoformat(acceptance_event['occurred_at']) + timedelta(seconds=1)
        )
        return self.event(
            event_id,
            'excavator.free_bucket.loaded',
            sequence,
            occurred_at=occurred_at,
            depends_on=[acceptance_event['event_id']],
            local_trip_id=f'local-{event_id}',
            payload={
                'truck_id': self.truck.id,
                'free_bucket_acceptance_local_id': acceptance_event['event_id'],
                'dump_point_id': self.dump_point.id,
                'rock_type_id': self.rock.id,
                'manual_control': False,
                'loading_horizon': '125',
                'loading_block': '4',
            },
            **identity,
        )

    def test_accept_then_load_creates_one_trip_without_changing_primary_assignment(self):
        other_client, other_operator, other_shift = (
            trip_fixtures.ExcavatorWorkServerIntegrationTests.create_other_excavator_client(self)
        )
        other_access = EmployeeAccess.objects.get(employee=other_operator, role=self.role)
        identity = {
            'actor': other_operator,
            'access': other_access,
            'shift': other_shift,
            'excavator': self.other_excavator,
        }
        accepted = self.accept_event(**identity)
        loaded = self.load_event(accepted, **identity)

        response = self.sync(
            [loaded, accepted],
            client=other_client,
            actor=other_operator,
            access=other_access,
            device_id='free-bucket-other-001',
        )

        self.assertEqual(response.status_code, 200, response.content)
        results = {item['event_id']: item for item in response.json()['results']}
        self.assertEqual(results[accepted['event_id']]['status'], 'accepted', results)
        self.assertEqual(results[loaded['event_id']]['status'], 'accepted', results)
        trip = Trip.objects.get()
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertEqual(trip.excavator_id, self.other_excavator.id)
        self.assertEqual(trip.truck_id, self.truck.id)
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.USED)
        self.assertEqual(acceptance.used_trip_id, trip.id)
        self.assertEqual(acceptance.primary_assignment_id, self.assignment.id)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(self.assignment.ended_at)
        self.assertEqual(
            results[loaded['event_id']]['server_ids']['free_bucket_acceptance_id'],
            acceptance.id,
        )

        repeated = self.sync(
            [accepted, loaded],
            client=other_client,
            actor=other_operator,
            access=other_access,
            device_id='free-bucket-other-001',
        )
        self.assertEqual(
            {item['event_id']: item['status'] for item in repeated.json()['results']},
            {accepted['event_id']: 'deduplicated', loaded['event_id']: 'deduplicated'},
        )
        self.assertEqual(Trip.objects.count(), 1)

    def test_accept_then_cancel_releases_without_creating_trip(self):
        accepted = self.accept_event()
        cancelled = self.event(
            'free-cancel-1',
            'excavator.free_bucket.cancelled',
            2,
            occurred_at=timezone.datetime.fromisoformat(accepted['occurred_at']) + timedelta(seconds=1),
            depends_on=[accepted['event_id']],
            payload={'free_bucket_acceptance_local_id': accepted['event_id']},
        )

        response = self.sync([cancelled, accepted])

        self.assertEqual(response.status_code, 200, response.content)
        results = {item['event_id']: item for item in response.json()['results']}
        self.assertEqual(results[accepted['event_id']]['status'], 'accepted', results)
        self.assertEqual(results[cancelled['event_id']]['status'], 'accepted', results)
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CANCELLED)
        self.assertIsNotNone(acceptance.cancelled_at)
        self.assertEqual(Trip.objects.count(), 0)

    def test_cancelling_loaded_trip_closes_consumed_acceptance(self):
        accepted = self.accept_event()
        loaded = self.load_event(accepted)
        cancelled = self.event(
            'free-loaded-cancel-1',
            'excavator.trip.loaded.cancelled',
            3,
            occurred_at=timezone.datetime.fromisoformat(loaded['occurred_at']) + timedelta(seconds=1),
            depends_on=[loaded['event_id']],
            local_trip_id=loaded['local_trip_id'],
            payload={'local_trip_id': loaded['local_trip_id']},
        )

        response = self.sync([cancelled, loaded, accepted])

        self.assertEqual(response.status_code, 200, response.content)
        results = {item['event_id']: item for item in response.json()['results']}
        self.assertEqual(
            {key: results[key]['status'] for key in (accepted['event_id'], loaded['event_id'], cancelled['event_id'])},
            {accepted['event_id']: 'accepted', loaded['event_id']: 'accepted', cancelled['event_id']: 'accepted'},
        )
        trip = Trip.objects.get()
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(trip.status, TripStatus.CANCELLED)
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CLOSED)
        self.assertEqual(acceptance.used_trip_id, trip.id)
        self.assertIsNotNone(acceptance.closed_at)

    def test_confirmed_other_excavator_acceptance_blocks_online_and_offline_ordinary_load(self):
        other_client, other_operator, other_shift = (
            trip_fixtures.ExcavatorWorkServerIntegrationTests.create_other_excavator_client(self)
        )
        other_access = EmployeeAccess.objects.get(employee=other_operator, role=self.role)
        accepted = self.accept_event(
            actor=other_operator,
            access=other_access,
            shift=other_shift,
            excavator=self.other_excavator,
        )
        accepted_result = self.sync(
            [accepted],
            client=other_client,
            actor=other_operator,
            access=other_access,
            device_id='free-bucket-other-guard-001',
        ).json()['results'][0]
        self.assertEqual(accepted_result['status'], 'accepted', accepted_result)

        online = trip_fixtures.ExcavatorWorkServerIntegrationTests.post_truck_loaded(
            self,
            client_action_id='ordinary-online-blocked',
            assignment=self.assignment,
        )
        self.assertEqual(online.status_code, 409, online.content)
        self.assertEqual(online.json()['code'], 'free_bucket_acceptance_required')

        ordinary_offline = self.event(
            'ordinary-offline-blocked',
            'excavator.trip.loaded',
            1,
            local_trip_id='local-ordinary-offline-blocked',
            payload={
                'truck_id': self.truck.id,
                'assignment_id': self.assignment.id,
                'dump_point_id': self.dump_point.id,
                'rock_type_id': self.rock.id,
                'manual_control': False,
            },
        )
        offline_result = self.sync(
            [ordinary_offline],
            device_id='free-bucket-ordinary-guard-001',
        ).json()['results'][0]
        self.assertEqual(offline_result['status'], 'conflict', offline_result)
        self.assertEqual(offline_result['code'], 'free_bucket_acceptance_required')
        self.assertEqual(Trip.objects.count(), 0)
        self.assertEqual(FreeBucketAcceptance.objects.get().status, FreeBucketAcceptanceStatus.ACCEPTED)
        self.assertEqual(
            OfflineFieldEvent.objects.get(event_id=ordinary_offline['event_id']).status,
            'conflict',
        )
