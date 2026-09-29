import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.db import close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
from core.models import OfflineFieldEvent
from shifts.models import EmployeeShift, ShiftClientAction
from trips import tests as trip_fixtures
from trips.models import Trip


class AutonomousExcavatorShiftMixin:
    create_registered_driver_shift = (
        trip_fixtures.ExcavatorWorkServerIntegrationTests.create_registered_driver_shift
    )
    create_driver_assignment = (
        trip_fixtures.ExcavatorWorkServerIntegrationTests.create_driver_assignment
    )

    def prepare_fixture(self):
        trip_fixtures.ExcavatorWorkServerIntegrationTests.setUp(self)
        self.create_driver_assignment(self.truck, driver=self.driver, shift=self.truck_shift)
        baseline = timezone.now() - timedelta(hours=1)
        self.truck_shift.opened_at = baseline
        self.truck_shift.save(update_fields=['opened_at'])
        EmployeeShift.objects.filter(employee=self.operator).delete()
        self.url = reverse('offline_events_sync')
        self.haul_assignment = HaulAssignment.objects.get(
            truck=self.truck,
            excavator=self.excavator,
            status=AssignmentStatus.ACCEPTED,
        )
        self.haul_assignment.assigned_at = baseline
        self.haul_assignment.save(update_fields=['assigned_at'])

    def opening(self, event_id='local-shift-1', sequence=1, occurred_at=None, **changes):
        occurred_at = occurred_at or timezone.now() - timedelta(minutes=2)
        event = {
            'event_id': event_id,
            'event_type': 'excavator.shift.opened',
            'format_version': 1,
            'actor_id': self.operator.id,
            'access_id': self.access.id,
            'role_code': 'excavator_operator',
            'device_id': 'off-c1-device-001',
            'local_shift_id': event_id,
            'shift_id': 0,
            'equipment_id': self.excavator.id,
            'occurred_at': occurred_at.isoformat(),
            'sequence': sequence,
            'depends_on': [],
            'payload': {
                'local_shift_id': event_id,
                'excavator_id': self.excavator.id,
                'fuel_percent': '50',
                'fuel': '3500',
                'engine_hours': '1200',
            },
        }
        event.update(changes)
        return event

    def load(self, opening, event_id='local-load-1', sequence=2, occurred_at=None, **changes):
        occurred_at = occurred_at or timezone.now() - timedelta(minutes=1)
        event = {
            'event_id': event_id,
            'event_type': 'excavator.trip.loaded',
            'format_version': 1,
            'actor_id': self.operator.id,
            'access_id': self.access.id,
            'role_code': 'excavator_operator',
            'device_id': 'off-c1-device-001',
            'local_shift_id': opening['local_shift_id'],
            'shift_id': 0,
            'equipment_id': self.excavator.id,
            'local_trip_id': f'trip-{event_id}',
            'occurred_at': occurred_at.isoformat(),
            'sequence': sequence,
            'depends_on': [opening['event_id']],
            'payload': {
                'local_shift_id': opening['local_shift_id'],
                'truck_id': self.truck.id,
                'assignment_id': self.haul_assignment.id,
                'dump_point_id': self.dump_point.id,
                'rock_type_id': self.rock.id,
                'manual_control': True,
                'loading_horizon': '125',
                'loading_block': '4',
            },
        }
        event.update(changes)
        return event

    def sync(self, events, *, client=None):
        return (client or self.client).post(
            self.url,
            data=json.dumps({
                'protocol_version': 1,
                'actor_id': self.operator.id,
                'access_id': self.access.id,
                'role_code': 'excavator_operator',
                'device_id': 'off-c1-device-001',
                'events': events,
            }),
            content_type='application/json',
        )


class AutonomousExcavatorShiftTests(AutonomousExcavatorShiftMixin, TestCase):
    def setUp(self):
        self.prepare_fixture()

    def test_open_and_child_preserve_local_identity_time_and_deduplicate(self):
        opened_at = timezone.now() - timedelta(minutes=3)
        loaded_at = opened_at + timedelta(minutes=1)
        opening = self.opening(occurred_at=opened_at)
        load = self.load(opening, occurred_at=loaded_at)

        first = self.sync([load, opening])

        self.assertEqual(first.status_code, 200, first.content)
        by_id = {item['event_id']: item for item in first.json()['results']}
        self.assertEqual(by_id[opening['event_id']]['status'], 'accepted')
        self.assertEqual(by_id[load['event_id']]['status'], 'accepted', by_id[load['event_id']])
        shift = EmployeeShift.objects.get(employee=self.operator)
        trip = OfflineFieldEvent.objects.get(event_id=load['event_id']).trip
        self.assertEqual(shift.opened_at, opened_at)
        self.assertEqual(trip.loaded_at, loaded_at)
        self.assertEqual(trip.loading_shift_id, shift.id)
        self.assertEqual(by_id[opening['event_id']]['server_ids']['shift_id'], shift.id)

        repeated = self.sync([opening, load])

        self.assertEqual(repeated.status_code, 200, repeated.content)
        self.assertEqual(EmployeeShift.objects.filter(employee=self.operator).count(), 1)
        self.assertEqual(Trip.objects.count(), 1)
        self.assertEqual(
            ShiftClientAction.objects.filter(
                action_type='excavator_shift_opened',
                client_action_id=opening['event_id'],
            ).count(),
            1,
        )

    def test_child_received_before_opening_retries_then_links_automatically(self):
        opening = self.opening()
        load = self.load(opening)

        child_first = self.sync([load]).json()['results'][0]
        self.assertEqual(child_first['status'], 'retry')
        self.assertEqual(child_first['code'], 'dependency_pending')
        self.assertEqual(Trip.objects.count(), 0)

        self.assertEqual(self.sync([opening]).json()['results'][0]['status'], 'accepted')
        replayed = self.sync([load]).json()['results'][0]

        self.assertEqual(replayed['status'], 'accepted', replayed)
        self.assertEqual(Trip.objects.count(), 1)
        receipt = OfflineFieldEvent.objects.get(event_id=load['event_id'])
        self.assertEqual(receipt.shift_id, EmployeeShift.objects.get(employee=self.operator).id)

    def test_incompatible_repeat_keeps_original_opening(self):
        opening = self.opening()
        accepted = self.sync([opening]).json()['results'][0]
        changed = json.loads(json.dumps(opening))
        changed['payload']['engine_hours'] = '1300'

        conflict = self.sync([changed]).json()['results'][0]

        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual(conflict['status'], 'conflict')
        self.assertEqual(conflict['code'], 'event_id_reused')
        self.assertEqual(EmployeeShift.objects.get(employee=self.operator).start_engine_hours, 1200)

    def test_opening_rejects_foreign_identity_device_and_equipment_without_mutation(self):
        cases = (
            ('actor', {'actor_id': self.driver.id}, 'actor_context_changed'),
            ('device', {'device_id': 'foreign-device'}, 'device_context_changed'),
            ('equipment', {'equipment_id': self.truck.id}, 'equipment_context_changed'),
        )
        for index, (label, changes, expected_code) in enumerate(cases, start=1):
            with self.subTest(label=label):
                event = self.opening(f'foreign-{label}', sequence=index, **changes)
                result = self.sync([event]).json()['results'][0]
                self.assertEqual(result['status'], 'conflict', result)
                self.assertEqual(result['code'], expected_code, result)
                self.assertEqual(EmployeeShift.objects.filter(employee=self.operator).count(), 0)

    def test_conflicting_server_shift_keeps_child_retryable_without_cascade(self):
        other = self.driver
        self.truck_shift.closed_at = timezone.now()
        self.truck_shift.save(update_fields=['closed_at'])
        EmployeeShift.objects.create(
            employee=other,
            equipment=self.excavator,
            shift_type='day',
            workplace_code='excavator_operator',
            opened_at=timezone.now() - timedelta(minutes=5),
        )
        opening = self.opening()
        load = self.load(opening)

        open_result = self.sync([opening]).json()['results'][0]
        child_result = self.sync([load]).json()['results'][0]

        self.assertEqual(open_result['status'], 'conflict')
        self.assertEqual(open_result['code'], 'equipment_shift_already_open')
        self.assertEqual(child_result['status'], 'retry')
        self.assertEqual(child_result['code'], 'shift_reference_pending')
        self.assertEqual(Trip.objects.count(), 0)
        self.assertTrue(OfflineFieldEvent.objects.filter(event_id=load['event_id']).exists())

    def test_future_clock_is_explicit_conflict_but_declared_unreliable_uses_receipt(self):
        future = timezone.now() + timedelta(minutes=10)
        disputed = self.opening('local-shift-future', occurred_at=future)
        disputed_result = self.sync([disputed]).json()['results'][0]
        self.assertEqual(disputed_result['status'], 'conflict')
        self.assertEqual(disputed_result['code'], 'device_clock_ahead')

        fallback = self.opening('local-shift-fallback', sequence=2, occurred_at=future)
        fallback['clock_unreliable'] = True
        fallback_result = self.sync([fallback]).json()['results'][0]

        self.assertEqual(fallback_result['status'], 'accepted')
        self.assertTrue(fallback_result['device_clock_adjusted'])
        self.assertEqual(fallback_result['time_source'], 'server_receipt')
        receipt = OfflineFieldEvent.objects.get(event_id=fallback['event_id'])
        self.assertEqual(receipt.occurred_at, future)
        self.assertEqual(receipt.shift.opened_at, receipt.received_at)

    def test_close_then_next_local_open_maps_to_two_distinct_server_shifts(self):
        first_at = timezone.now() - timedelta(minutes=8)
        close_at = first_at + timedelta(minutes=3)
        second_at = close_at + timedelta(minutes=1)
        first = self.opening('local-shift-first', sequence=1, occurred_at=first_at)
        close = {
            'event_id': 'local-shift-first-close',
            'event_type': 'excavator.shift.closed',
            'format_version': 1,
            'actor_id': self.operator.id,
            'access_id': self.access.id,
            'role_code': 'excavator_operator',
            'device_id': 'off-c1-device-001',
            'local_shift_id': first['local_shift_id'],
            'shift_id': 0,
            'equipment_id': self.excavator.id,
            'occurred_at': close_at.isoformat(),
            'sequence': 2,
            'depends_on': [first['event_id']],
            'payload': {
                'local_shift_id': first['local_shift_id'],
                'excavator_id': self.excavator.id,
                'fuel_percent': '50',
                'fuel': '3500',
                'engine_hours': '1200',
            },
        }
        second = self.opening('local-shift-second', sequence=3, occurred_at=second_at)
        second['depends_on'] = [close['event_id']]

        response = self.sync([second, close, first])

        self.assertEqual(response.status_code, 200, response.content)
        by_id = {item['event_id']: item for item in response.json()['results']}
        self.assertEqual(by_id[first['event_id']]['status'], 'accepted', by_id)
        self.assertEqual(by_id[close['event_id']]['status'], 'accepted', by_id)
        self.assertEqual(by_id[second['event_id']]['status'], 'accepted', by_id)
        shifts = list(EmployeeShift.objects.filter(employee=self.operator).order_by('opened_at'))
        self.assertEqual(len(shifts), 2)
        self.assertEqual(shifts[0].opened_at, first_at)
        self.assertEqual(shifts[0].closed_at, close_at)
        self.assertEqual(shifts[1].opened_at, second_at)
        self.assertIsNone(shifts[1].closed_at)
        self.assertNotEqual(
            by_id[first['event_id']]['server_ids']['shift_id'],
            by_id[second['event_id']]['server_ids']['shift_id'],
        )


@skipUnlessDBFeature('has_select_for_update')
class AutonomousExcavatorShiftPostgreSQLTests(AutonomousExcavatorShiftMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.prepare_fixture()

    def test_concurrent_same_opening_creates_one_shift_and_mapping(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL is required for real row-lock concurrency.')
        opening = self.opening()

        def send():
            close_old_connections()
            client = Client()
            session = client.session
            session['employee_access_id'] = self.access.id
            session.save()
            try:
                return self.sync([opening], client=client).json()['results'][0]
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: send(), range(2)))

        self.assertTrue(all(item['status'] in {'accepted', 'deduplicated'} for item in results), results)
        self.assertEqual(EmployeeShift.objects.filter(employee=self.operator).count(), 1)
        self.assertEqual(OfflineFieldEvent.objects.filter(event_id=opening['event_id']).count(), 1)
        self.assertEqual(
            ShiftClientAction.objects.filter(client_action_id=opening['event_id']).count(),
            1,
        )
