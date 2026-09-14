import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from django.apps import apps
from django.core.management.color import no_style
from django.db import close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
from core.models import OfflineFieldEvent
from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import Equipment
from shifts.models import EmployeeShift
from trips import tests as trip_fixtures
from trips.models import FreeBucketAcceptance, FreeBucketAcceptanceStatus, Trip, TripStatus
from users.models import AdminConflict, EmployeeAccess


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

    def sync(
        self,
        events,
        *,
        client=None,
        actor=None,
        access=None,
        role_code='excavator_operator',
        device_id='free-bucket-device-001',
    ):
        actor = actor or self.operator
        access = access or self.access
        return (client or self.client).post(
            self.url,
            data=json.dumps({
                'protocol_version': 1,
                'actor_id': actor.id,
                'access_id': access.id,
                'role_code': role_code,
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
        self.assertEqual(trip.assigned_dump_point_id, self.dump_point.id)
        self.assertEqual(trip.driver_control_shift_id, self.truck_shift.id)
        self.assertTrue(trip.driver_participation_recorded)
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
        from trips.excavator_hourly_report import build_excavator_hourly_report

        report_at = trip.loaded_at + timedelta(minutes=1)
        temporary_report = build_excavator_hourly_report(
            self.other_excavator,
            captured_at=report_at,
        )
        primary_report = build_excavator_hourly_report(
            self.excavator,
            captured_at=report_at,
        )
        self.assertEqual(sum(hour['source_trip_count'] for hour in temporary_report['hours']), 1)
        self.assertEqual(sum(hour['source_trip_count'] for hour in primary_report['hours']), 0)

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

    def test_out_of_order_free_bucket_load_retries_then_creates_exactly_one_trip(self):
        """A saved load must wait for its acceptance, not be discarded or duplicated."""
        accepted = self.accept_event('free-accept-later', 1)
        loaded = self.load_event(accepted, event_id='free-load-earlier', sequence=2)

        waiting = self.sync([loaded]).json()['results'][0]
        self.assertEqual(waiting['status'], 'retry', waiting)
        self.assertTrue(waiting['retryable'])
        self.assertEqual(Trip.objects.count(), 0)

        accepted_result = self.sync([accepted]).json()['results'][0]
        self.assertEqual(accepted_result['status'], 'accepted', accepted_result)

        replayed = self.sync([loaded]).json()['results'][0]
        self.assertEqual(replayed['status'], 'accepted', replayed)
        trip = Trip.objects.get()
        self.assertEqual(trip.loaded_at, timezone.datetime.fromisoformat(loaded['occurred_at']))
        self.assertEqual(trip.excavator_id, self.excavator.id)
        self.assertEqual(FreeBucketAcceptance.objects.get().used_trip_id, trip.id)

        deduplicated = self.sync([loaded]).json()['results'][0]
        self.assertEqual(deduplicated['status'], 'deduplicated', deduplicated)
        self.assertEqual(Trip.objects.count(), 1)

    def test_second_confirmed_acceptance_of_the_same_truck_is_a_stable_conflict(self):
        first = self.accept_event('free-accept-first', 1)
        first_result = self.sync([first], device_id='free-bucket-first-device').json()['results'][0]
        self.assertEqual(first_result['status'], 'accepted', first_result)

        other_client, other_operator, other_shift = (
            trip_fixtures.ExcavatorWorkServerIntegrationTests.create_other_excavator_client(self)
        )
        other_access = EmployeeAccess.objects.get(employee=other_operator, role=self.role)
        second = self.accept_event(
            'free-accept-second', 1,
            actor=other_operator,
            access=other_access,
            shift=other_shift,
            excavator=self.other_excavator,
        )
        second_result = self.sync(
            [second], client=other_client, actor=other_operator, access=other_access,
            device_id='free-bucket-second-device',
        ).json()['results'][0]
        self.assertEqual(second_result['status'], 'conflict', second_result)
        self.assertEqual(second_result['code'], 'free_bucket_already_accepted')
        self.assertEqual(FreeBucketAcceptance.objects.count(), 1)

    def test_second_acceptance_after_free_bucket_load_is_a_stable_conflict(self):
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
        accepted = self.accept_event('free-accept-used', 1, **identity)
        loaded = self.load_event(accepted, event_id='free-load-used', sequence=2, **identity)
        first = self.sync(
            [loaded, accepted],
            client=other_client,
            actor=other_operator,
            access=other_access,
            device_id='free-bucket-used-device',
        ).json()['results']
        self.assertEqual({item['status'] for item in first}, {'accepted'}, first)

        second = self.accept_event('free-accept-after-used', 1)
        result = self.sync(
            [second],
            device_id='free-bucket-after-used-device',
        ).json()['results'][0]
        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'open_trip_exists')
        self.assertEqual(Trip.objects.count(), 1)
        self.assertEqual(
            FreeBucketAcceptance.objects.get().status,
            FreeBucketAcceptanceStatus.USED,
        )

    def test_free_bucket_load_preserves_passive_manual_control(self):
        accepted = self.accept_event()
        loaded = self.load_event(accepted)
        loaded['payload']['manual_control'] = True

        response = self.sync([loaded, accepted])
        self.assertEqual(response.status_code, 200, response.content)
        result = {item['event_id']: item for item in response.json()['results']}
        self.assertEqual(result[loaded['event_id']]['status'], 'accepted', result)
        trip = Trip.objects.get()
        self.assertTrue(trip.driver_participation_recorded)
        self.assertIsNone(trip.driver_control_shift_id)

    def test_driver_offline_unload_completes_the_normal_free_bucket_trip(self):
        accepted = self.accept_event()
        loaded = self.load_event(accepted)
        response = self.sync([loaded, accepted])
        self.assertEqual(response.status_code, 200, response.content)
        trip = Trip.objects.get()
        occurred_at = trip.loaded_at + timedelta(minutes=3)
        unload = {
            'event_id': 'driver-unload-free-bucket-1',
            'event_type': 'driver.trip.unloaded',
            'format_version': 1,
            'actor_id': self.driver.id,
            'access_id': self.driver_access.id,
            'role_code': 'driver',
            'occurred_at': occurred_at.isoformat(),
            'sequence': 1,
            'depends_on': [],
            'shift_id': self.truck_shift.id,
            'equipment_id': self.truck.id,
            'trip_id': trip.id,
            'context_snapshot': {
                'actor_id': self.driver.id,
                'access_id': self.driver_access.id,
                'role_code': 'driver',
            },
            'payload': {'trip_id': trip.id},
        }
        driver_client = Client()
        session = driver_client.session
        session['employee_access_id'] = self.driver_access.id
        session.save()

        unload_response = self.sync(
            [unload], client=driver_client, actor=self.driver, access=self.driver_access,
            role_code='driver',
            device_id='free-bucket-driver-offline-001',
        )
        self.assertEqual(unload_response.status_code, 200, unload_response.content)
        unload_result = unload_response.json()['results'][0]
        self.assertEqual(unload_result['status'], 'accepted', unload_result)
        trip.refresh_from_db()
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(trip.status, TripStatus.COMPLETED)
        self.assertEqual(trip.completed_at, occurred_at)
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CLOSED)

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
        primary_screen = self.client.get(reverse('excavator_work'))
        self.assertContains(primary_screen, 'Свободный ковш')
        self.assertContains(primary_screen, str(self.other_excavator.garage_number))
        self.assertContains(primary_screen, '"updated_at"')
        self.assertContains(primary_screen, '"availability_label"')

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

    def test_conflicting_free_bucket_load_is_retained_in_existing_review_queue(self):
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
        self.sync(
            [accepted], client=other_client, actor=other_operator, access=other_access,
            device_id='free-bucket-other-review-001',
        )
        conflicting_load = self.load_event(accepted)
        result = self.sync([conflicting_load]).json()['results'][0]

        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(Trip.objects.count(), 0)
        review = AdminConflict.objects.get(process='Свободный ковш')
        self.assertEqual(review.employee_id, self.operator.id)
        self.assertIn(conflicting_load['event_id'], review.description)

    def test_driver_completion_closes_used_acceptance_without_changing_primary_assignment(self):
        accepted = self.accept_event()
        loaded = self.load_event(accepted)
        response = self.sync([loaded, accepted])
        self.assertEqual(response.status_code, 200, response.content)
        trip = Trip.objects.get()

        from trips.views import finalize_trip_unloaded

        self.assertTrue(finalize_trip_unloaded(
            trip,
            driver=self.truck_shift.employee,
            unloading_shift=self.truck_shift,
            occurred_at=timezone.now(),
        ))
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.CLOSED)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(self.assignment.ended_at)

    def test_changed_primary_assignment_does_not_break_accepted_free_bucket_load(self):
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
        accepted_response = self.sync(
            [accepted], client=other_client, actor=other_operator, access=other_access,
            device_id='free-bucket-primary-changed-001',
        )
        self.assertEqual(accepted_response.json()['results'][0]['status'], 'accepted')

        changed_at = timezone.now()
        self.assignment.status = AssignmentStatus.CANCELLED
        self.assignment.ended_at = changed_at
        self.assignment.save(update_fields=['status', 'ended_at'])
        new_primary = HaulAssignment.objects.create(
            truck=self.truck,
            excavator=self.other_excavator,
            status=AssignmentStatus.ACCEPTED,
            assigned_at=changed_at,
            accepted_at=changed_at,
        )

        loaded = self.load_event(accepted, **identity)
        loaded_response = self.sync(
            [loaded], client=other_client, actor=other_operator, access=other_access,
            device_id='free-bucket-primary-changed-001',
        )
        result = loaded_response.json()['results'][0]
        self.assertEqual(result['status'], 'accepted', result)
        trip = Trip.objects.get()
        self.assertEqual(trip.excavator_id, self.other_excavator.id)
        acceptance = FreeBucketAcceptance.objects.get()
        self.assertEqual(acceptance.primary_assignment_id, self.assignment.id)
        self.assertEqual(acceptance.status, FreeBucketAcceptanceStatus.USED)
        new_primary.refresh_from_db()
        self.assertEqual(new_primary.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(new_primary.ended_at)

    def test_dispatcher_keeps_primary_tile_and_shows_free_bucket_marker(self):
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
        response = self.sync(
            [accepted], client=other_client, actor=other_operator, access=other_access,
            device_id='free-bucket-dispatcher-marker-001',
        )
        self.assertEqual(response.json()['results'][0]['status'], 'accepted')

        from trips.views import build_dispatcher_dashboard_context

        dashboard = build_dispatcher_dashboard_context(
            dispatcher_shift=self.shift,
            active_trips=Trip.objects.filter(
                status__in=(TripStatus.ACTIVE, TripStatus.LOADED_WAITING_UNLOAD),
            ),
            pending_assignments=HaulAssignment.objects.filter(status=AssignmentStatus.PENDING),
            accepted_assignments=HaulAssignment.objects.filter(status=AssignmentStatus.ACCEPTED),
            recent_completed_trips=Trip.objects.none(),
            open_shifts=EmployeeShift.objects.filter(closed_at__isnull=True).exclude(pk=self.shift.pk),
            open_mechanic_downtimes=DowntimeEvent.objects.filter(ended_at__isnull=True),
            trucks=Equipment.objects.filter(equipment_type=self.truck_type).order_by('garage_number'),
            excavators=Equipment.objects.filter(equipment_type=self.excavator_type).order_by('garage_number'),
            recent_dispatcher_actions=[],
        )
        primary_tiles = [
            tile
            for complex_card in dashboard['complex_cards']
            for tile in complex_card.get('active_truck_tiles', [])
            if tile.get('card_id') == str(self.truck.id)
        ]
        self.assertEqual(len(primary_tiles), 1)
        self.assertIn('Свободный ковш', primary_tiles[0]['free_bucket_label'])
        self.assertIn(str(self.other_excavator.garage_number), primary_tiles[0]['free_bucket_label'])


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class FreeBucketPostgreSQLConcurrencyTests(TransactionTestCase):
    """The two-device acceptance race needs real PostgreSQL row locks."""

    reset_sequences = True
    serialized_rollback = True
    create_registered_driver_shift = (
        trip_fixtures.ExcavatorWorkServerIntegrationTests.create_registered_driver_shift
    )

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Конкурентный приём под свободный ковш проверяется только на PostgreSQL.')
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
        self.url = reverse('offline_events_sync')
        self.shift = EmployeeShift.objects.get(employee=self.operator, closed_at__isnull=True)
        self.assignment = HaulAssignment.objects.get(
            truck=self.truck,
            excavator=self.excavator,
            status=AssignmentStatus.ACCEPTED,
        )
        self.first_session_key = self.client.cookies['sessionid'].value
        self.other_client, self.other_operator, self.other_shift = (
            trip_fixtures.ExcavatorWorkServerIntegrationTests.create_other_excavator_client(self)
        )
        self.other_access = EmployeeAccess.objects.get(employee=self.other_operator, role=self.role)
        self.other_session_key = self.other_client.cookies['sessionid'].value

    def acceptance_event(self, *, event_id, sequence, access, shift):
        return {
            'event_id': event_id,
            'event_type': 'excavator.free_bucket.accepted',
            'format_version': 1,
            'actor_id': access.employee_id,
            'access_id': access.id,
            'role_code': 'excavator_operator',
            'occurred_at': timezone.now().isoformat(),
            'sequence': sequence,
            'depends_on': [],
            'shift_id': shift.id,
            'equipment_id': shift.equipment_id,
            'context_snapshot': {
                'actor_id': access.employee_id,
                'access_id': access.id,
                'role_code': 'excavator_operator',
            },
            'payload': {'truck_id': self.truck.id},
        }

    def post_from_thread(self, *, session_key, access, device_id, event, start):
        close_old_connections()
        try:
            start.wait(timeout=10)
            client = Client()
            client.cookies['sessionid'] = session_key
            response = client.post(
                self.url,
                data=json.dumps({
                    'protocol_version': 1,
                    'actor_id': access.employee_id,
                    'access_id': access.id,
                    'role_code': 'excavator_operator',
                    'device_id': device_id,
                    'events': [event],
                }),
                content_type='application/json',
            )
            return response.status_code, response.json()['results'][0]
        finally:
            close_old_connections()

    def test_two_offline_excavators_accept_same_truck_once(self):
        start = Barrier(2)
        first = self.acceptance_event(
            event_id='free-pg-accept-a',
            sequence=1,
            access=self.access,
            shift=self.shift,
        )
        second = self.acceptance_event(
            event_id='free-pg-accept-b',
            sequence=1,
            access=self.other_access,
            shift=self.other_shift,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda payload: self.post_from_thread(start=start, **payload),
                [
                    {
                        'session_key': self.first_session_key,
                        'access': self.access,
                        'device_id': 'free-pg-device-a',
                        'event': first,
                    },
                    {
                        'session_key': self.other_session_key,
                        'access': self.other_access,
                        'device_id': 'free-pg-device-b',
                        'event': second,
                    },
                ],
            ))

        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(sorted(result['status'] for _, result in results), ['accepted', 'conflict'])
        self.assertEqual(FreeBucketAcceptance.objects.count(), 1)
        self.assertEqual(Trip.objects.count(), 0)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(self.assignment.ended_at)
