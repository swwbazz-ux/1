import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from django.db import close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, HaulAssignment
from assignments.models import ExcavatorPlacement
from core.models import OfflineFieldEvent, OfflineFieldEventStatus
from core.offline_sync import normalize_offline_event
from downtimes.models import DowntimeEvent, DowntimeReason
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

    def work_context(self, opening, event_id='local-context-1', sequence=2, occurred_at=None, **payload):
        return {
            'event_id': event_id,
            'event_type': 'excavator.work_context.changed',
            'format_version': 1,
            'actor_id': self.operator.id,
            'access_id': self.access.id,
            'role_code': 'excavator_operator',
            'device_id': 'off-c1-device-001',
            'local_shift_id': opening['local_shift_id'],
            'shift_id': 0,
            'equipment_id': self.excavator.id,
            'occurred_at': (occurred_at or timezone.now() - timedelta(minutes=1)).isoformat(),
            'sequence': sequence,
            'depends_on': [opening['event_id']],
            'payload': {
                'local_shift_id': opening['local_shift_id'],
                'rock_type_id': self.rock.id,
                'dump_point_ids': [self.dump_point.id],
                'loading_horizon': '777',
                'loading_block': '8',
                **payload,
            },
        }

    def downtime_start(self, opening, reason, *, event_id, sequence, occurred_at, depends_on):
        return {
            'event_id': event_id,
            'event_type': 'excavator.downtime.started',
            'format_version': 1,
            'actor_id': self.operator.id,
            'access_id': self.access.id,
            'role_code': 'excavator_operator',
            'device_id': 'off-c1-device-001',
            'local_shift_id': opening['local_shift_id'],
            'shift_id': 0,
            'equipment_id': self.excavator.id,
            'local_downtime_id': event_id,
            'occurred_at': occurred_at.isoformat(),
            'sequence': sequence,
            'depends_on': list(depends_on),
            'payload': {
                'local_shift_id': opening['local_shift_id'],
                'local_downtime_id': event_id,
                'reason_id': reason.id,
            },
        }

    def downtime_stop(self, opening, start, *, event_id, sequence, occurred_at):
        return {
            'event_id': event_id,
            'event_type': 'excavator.downtime.ended',
            'format_version': 1,
            'actor_id': self.operator.id,
            'access_id': self.access.id,
            'role_code': 'excavator_operator',
            'device_id': 'off-c1-device-001',
            'local_shift_id': opening['local_shift_id'],
            'shift_id': 0,
            'equipment_id': self.excavator.id,
            'local_downtime_id': start['event_id'],
            'occurred_at': occurred_at.isoformat(),
            'sequence': sequence,
            'depends_on': [start['event_id']],
            'payload': {
                'local_shift_id': opening['local_shift_id'],
                'local_downtime_id': start['event_id'],
            },
        }

    def close_event(self, opening, *, event_id, sequence, occurred_at, depends_on):
        return {
            'event_id': event_id,
            'event_type': 'excavator.shift.closed',
            'format_version': 1,
            'actor_id': self.operator.id,
            'access_id': self.access.id,
            'role_code': 'excavator_operator',
            'device_id': 'off-c1-device-001',
            'local_shift_id': opening['local_shift_id'],
            'shift_id': 0,
            'equipment_id': self.excavator.id,
            'occurred_at': occurred_at.isoformat(),
            'sequence': sequence,
            'depends_on': list(depends_on),
            'payload': {
                'local_shift_id': opening['local_shift_id'],
                'fuel_percent': '50',
                'fuel': '3500',
                'engine_hours': '1200',
            },
        }

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

    def test_offline_work_context_survives_repeat_and_drives_the_following_load(self):
        opened_at = timezone.now() - timedelta(minutes=12)
        opening = self.opening(occurred_at=opened_at)
        context = self.work_context(
            opening,
            occurred_at=opened_at + timedelta(minutes=1),
        )
        load = self.load(
            opening,
            sequence=3,
            occurred_at=opened_at + timedelta(minutes=2),
        )
        load['depends_on'] = [context['event_id']]
        load['payload']['loading_horizon'] = '777'
        load['payload']['loading_block'] = '8'

        results = self.sync([load, context, opening]).json()['results']
        repeated = self.sync([context]).json()['results'][0]

        by_id = {item['event_id']: item for item in results}
        self.assertEqual(by_id[opening['event_id']]['status'], 'accepted', by_id)
        self.assertEqual(by_id[context['event_id']]['status'], 'accepted', by_id)
        self.assertEqual(by_id[load['event_id']]['status'], 'accepted', by_id)
        self.assertEqual(repeated['status'], 'deduplicated', repeated)
        placement = ExcavatorPlacement.objects.get(excavator=self.excavator)
        trip = OfflineFieldEvent.objects.get(event_id=load['event_id']).trip
        self.assertEqual(placement.loading_horizon, '777')
        self.assertEqual(placement.loading_block, '8')
        self.assertEqual(trip.loading_horizon, '777')
        self.assertEqual(trip.loading_block, '8')
        self.assertEqual(
            OfflineFieldEvent.objects.filter(event_id=context['event_id']).count(),
            1,
        )

    def test_suspicious_offline_close_retries_same_event_with_confirmation_then_allows_next_cycle(self):
        opened_at = timezone.now() - timedelta(minutes=15)
        first = self.opening('confirm-shift-a', sequence=1, occurred_at=opened_at)
        close = self.close_event(
            first,
            event_id='confirm-shift-a-close',
            sequence=2,
            occurred_at=opened_at + timedelta(minutes=5),
            depends_on=[first['event_id']],
        )
        close['payload']['engine_hours'] = '1199'

        self.assertEqual(self.sync([first]).json()['results'][0]['status'], 'accepted')
        warning = self.sync([close]).json()['results'][0]
        self.assertEqual(warning['status'], 'conflict', warning)
        self.assertEqual(warning['code'], 'confirmation_required', warning)
        self.assertTrue(warning['confirmation_token'])

        confirmed = dict(close)
        confirmed['confirmation_token'] = warning['confirmation_token']
        confirmed_result = self.sync([confirmed]).json()['results'][0]
        self.assertEqual(confirmed_result['status'], 'accepted', confirmed_result)
        receipt = OfflineFieldEvent.objects.get(event_id=close['event_id'])
        self.assertEqual(receipt.status, OfflineFieldEventStatus.ACCEPTED)
        self.assertEqual(receipt.fingerprint, normalize_offline_event(
            close,
            role_code='excavator_operator',
            device_id='off-c1-device-001',
            received_at=receipt.received_at,
        )['fingerprint'])

        second = self.opening(
            'confirm-shift-b', sequence=3,
            occurred_at=opened_at + timedelta(minutes=6),
        )
        second['depends_on'] = [close['event_id']]
        child = self.load(
            second,
            event_id='confirm-shift-b-load',
            sequence=4,
            occurred_at=opened_at + timedelta(minutes=7),
        )
        child['depends_on'] = [second['event_id']]
        by_id = {
            item['event_id']: item
            for item in self.sync([child, second]).json()['results']
        }
        self.assertEqual(by_id[second['event_id']]['status'], 'accepted', by_id)
        self.assertEqual(by_id[child['event_id']]['status'], 'accepted', by_id)

    def test_authenticated_shell_can_be_exported_as_safe_service_worker_fixture(self):
        """Render the real shell only from the isolated test DB.

        The optional output is intentionally generated from synthetic fixtures,
        never copied from an installed phone, production response or a private
        browser profile.  The Node service-worker test consumes the exact bytes.
        """
        opening = self.opening('rendered-shell-shift', sequence=1)
        result = self.sync([opening]).json()['results'][0]
        self.assertEqual(result['status'], 'accepted', result)
        response = self.client.get(reverse('excavator_work'))
        self.assertEqual(response.status_code, 200)
        rendered = bytes(response.content)
        self.assertIn(b'data-eo-shell', rendered)
        self.assertGreater(rendered.count(b'/static/'), 10)
        output_path = str(os.getenv('EXCAVATOR_RENDERED_SHELL_PATH') or '').strip()
        if output_path:
            Path(output_path).write_bytes(rendered)

    def test_late_offline_chain_keeps_auto_and_manual_downtime_boundaries(self):
        from trips.views import EXCAVATOR_AUTO_DOWNTIME_WAITING_TRUCKS

        opened_at = timezone.now() - timedelta(minutes=40)
        opening = self.opening('timeline-shift', sequence=1, occurred_at=opened_at)
        load = self.load(
            opening,
            event_id='timeline-load',
            sequence=2,
            occurred_at=opened_at + timedelta(minutes=2),
        )
        manual_reason = DowntimeReason.objects.create(
            name='Ручной ремонт OFF-C1-R1',
            equipment_type=self.excavator.equipment_type,
            show_for_excavator_operator=True,
        )
        start = self.downtime_start(
            opening,
            manual_reason,
            event_id='timeline-manual-start',
            sequence=3,
            occurred_at=opened_at + timedelta(minutes=5),
            depends_on=[load['event_id']],
        )
        stop = self.downtime_stop(
            opening,
            start,
            event_id='timeline-manual-stop',
            sequence=4,
            occurred_at=opened_at + timedelta(minutes=8),
        )
        close = self.close_event(
            opening,
            event_id='timeline-close',
            sequence=5,
            occurred_at=opened_at + timedelta(minutes=10),
            depends_on=[stop['event_id']],
        )

        neighbor_reason = DowntimeReason.objects.create(
            name='Соседний несвязанный простой OFF-C1-R1',
            equipment_type=self.excavator.equipment_type,
            show_for_excavator_operator=True,
        )
        neighbor = DowntimeEvent.objects.create(
            equipment=self.excavator,
            employee=self.operator,
            reason=neighbor_reason,
            started_at=opened_at + timedelta(minutes=50),
            ended_at=opened_at + timedelta(minutes=54),
        )

        first_results = self.sync([load, opening]).json()['results']
        self.assertTrue(all(item['status'] == 'accepted' for item in first_results), first_results)
        auto_before_poll = DowntimeEvent.objects.get(
            reason__name=EXCAVATOR_AUTO_DOWNTIME_WAITING_TRUCKS,
        )
        self.assertEqual(auto_before_poll.started_at, opened_at + timedelta(minutes=2))

        # A normal screen refresh between queue parts is allowed to reconcile
        # production state, but it must not replace the action-time boundary
        # with request time or create a second automatic interval.
        response = self.client.get(reverse('excavator_work'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            DowntimeEvent.objects.filter(
                reason__name=EXCAVATOR_AUTO_DOWNTIME_WAITING_TRUCKS,
            ).count(),
            1,
        )
        auto_before_poll.refresh_from_db()
        self.assertEqual(auto_before_poll.started_at, opened_at + timedelta(minutes=2))

        results = self.sync([close, stop, start]).json()['results']
        self.assertTrue(all(item['status'] == 'accepted' for item in results), results)
        auto = DowntimeEvent.objects.get(reason__name=EXCAVATOR_AUTO_DOWNTIME_WAITING_TRUCKS)
        manual = DowntimeEvent.objects.get(reason=manual_reason)
        self.assertEqual(auto.started_at, opened_at + timedelta(minutes=2))
        self.assertEqual(auto.ended_at, opened_at + timedelta(minutes=5))
        self.assertEqual(manual.started_at, opened_at + timedelta(minutes=5))
        self.assertEqual(manual.ended_at, opened_at + timedelta(minutes=8))
        self.assertEqual((auto.ended_at - auto.started_at), timedelta(minutes=3))
        self.assertEqual((manual.ended_at - manual.started_at), timedelta(minutes=3))
        self.assertEqual(
            OfflineFieldEvent.objects.get(event_id=start['event_id']).downtime_event_id,
            manual.id,
        )
        self.assertEqual(OfflineFieldEvent.objects.filter(event_id__in=[
            opening['event_id'], load['event_id'], start['event_id'], stop['event_id'], close['event_id'],
        ]).count(), 5)
        neighbor.refresh_from_db()
        self.assertEqual(neighbor.started_at, opened_at + timedelta(minutes=50))
        self.assertEqual(neighbor.ended_at, opened_at + timedelta(minutes=54))

    def test_manual_start_with_auto_reason_reuses_one_interval_and_repeat_is_idempotent(self):
        from trips.views import EXCAVATOR_AUTO_DOWNTIME_WAITING_TRUCKS

        opened_at = timezone.now() - timedelta(minutes=35)
        opening = self.opening('same-reason-shift', sequence=1, occurred_at=opened_at)
        load = self.load(
            opening,
            event_id='same-reason-load',
            sequence=2,
            occurred_at=opened_at + timedelta(minutes=2),
        )
        self.assertTrue(all(
            item['status'] == 'accepted'
            for item in self.sync([load, opening]).json()['results']
        ))
        reason = DowntimeReason.objects.get(name=EXCAVATOR_AUTO_DOWNTIME_WAITING_TRUCKS)
        start = self.downtime_start(
            opening,
            reason,
            event_id='same-reason-start',
            sequence=3,
            occurred_at=opened_at + timedelta(minutes=5),
            depends_on=[load['event_id']],
        )
        stop = self.downtime_stop(
            opening,
            start,
            event_id='same-reason-stop',
            sequence=4,
            occurred_at=opened_at + timedelta(minutes=8),
        )
        close = self.close_event(
            opening,
            event_id='same-reason-close',
            sequence=5,
            occurred_at=opened_at + timedelta(minutes=10),
            depends_on=[stop['event_id']],
        )

        first = self.sync([close, stop, start]).json()['results']
        repeated = self.sync([start, stop, close]).json()['results']
        self.assertTrue(all(item['status'] == 'accepted' for item in first), first)
        self.assertTrue(all(item['status'] == 'deduplicated' for item in repeated), repeated)

        intervals = list(DowntimeEvent.objects.filter(
            equipment=self.excavator,
            reason=reason,
        ))
        self.assertEqual(len(intervals), 1)
        interval = intervals[0]
        self.assertEqual(interval.started_at, opened_at + timedelta(minutes=2))
        self.assertEqual(interval.ended_at, opened_at + timedelta(minutes=8))
        self.assertEqual(interval.ended_at - interval.started_at, timedelta(minutes=6))
        start_receipt = OfflineFieldEvent.objects.get(event_id=start['event_id'])
        stop_receipt = OfflineFieldEvent.objects.get(event_id=stop['event_id'])
        self.assertEqual(start_receipt.downtime_event_id, interval.id)
        self.assertEqual(stop_receipt.downtime_event_id, interval.id)
        self.assertEqual(OfflineFieldEvent.objects.filter(
            event_id__in=[start['event_id'], stop['event_id'], close['event_id']],
        ).count(), 3)

    def test_legacy_negative_downtime_conflict_and_child_recover_with_same_ids(self):
        from trips.views import EXCAVATOR_AUTO_DOWNTIME_COMMENT, EXCAVATOR_AUTO_DOWNTIME_WAITING_TRUCKS

        opened_at = timezone.now() - timedelta(minutes=45)
        opening = self.opening('legacy-chain-shift', sequence=1, occurred_at=opened_at)
        load = self.load(
            opening,
            event_id='legacy-chain-load',
            sequence=2,
            occurred_at=opened_at + timedelta(minutes=2),
        )
        manual_reason = DowntimeReason.objects.create(
            name='Ручной ремонт legacy chain',
            equipment_type=self.excavator.equipment_type,
            show_for_excavator_operator=True,
        )
        start = self.downtime_start(
            opening,
            manual_reason,
            event_id='legacy-chain-start',
            sequence=3,
            occurred_at=opened_at + timedelta(minutes=5),
            depends_on=[load['event_id']],
        )
        self.assertTrue(all(
            item['status'] == 'accepted'
            for item in self.sync([opening, load, start]).json()['results']
        ))
        auto = DowntimeEvent.objects.get(reason__name=EXCAVATOR_AUTO_DOWNTIME_WAITING_TRUCKS)
        manual = DowntimeEvent.objects.get(reason=manual_reason)
        legacy_boundary = timezone.now() - timedelta(minutes=5)
        auto.started_at = legacy_boundary - timedelta(seconds=1)
        auto.ended_at = legacy_boundary
        auto.comment = EXCAVATOR_AUTO_DOWNTIME_COMMENT
        auto.save(update_fields=['started_at', 'ended_at', 'comment'])
        manual.started_at = legacy_boundary
        manual.ended_at = None
        manual.save(update_fields=['started_at', 'ended_at'])

        stop = self.downtime_stop(
            opening,
            start,
            event_id='legacy-chain-stop',
            sequence=4,
            occurred_at=opened_at + timedelta(minutes=8),
        )
        normalized = normalize_offline_event(
            stop,
            role_code='excavator_operator',
            device_id='off-c1-device-001',
        )
        OfflineFieldEvent.objects.create(
            event_id=stop['event_id'],
            event_type=stop['event_type'],
            actor=self.operator,
            access=self.access,
            role_code='excavator_operator',
            device_id='off-c1-device-001',
            sequence=stop['sequence'],
            depends_on=stop['depends_on'],
            occurred_at=normalized['device_occurred_at'],
            shift=OfflineFieldEvent.objects.get(event_id=opening['event_id']).shift,
            equipment=self.excavator,
            local_downtime_id=start['event_id'],
            payload=stop['payload'],
            context_snapshot={},
            fingerprint=normalized['fingerprint'],
            status=OfflineFieldEventStatus.CONFLICT,
            retryable=False,
            error_code='downtime_end_before_start',
            error_message='legacy negative interval',
        )
        close = self.close_event(
            opening,
            event_id='legacy-chain-close',
            sequence=5,
            occurred_at=opened_at + timedelta(minutes=10),
            depends_on=[stop['event_id']],
        )
        rejected_child = self.sync([close]).json()['results'][0]
        self.assertEqual(rejected_child['code'], 'dependency_rejected', rejected_child)

        repaired_stop = self.sync([stop]).json()['results'][0]
        repaired_close = self.sync([close]).json()['results'][0]

        self.assertEqual(repaired_stop['status'], 'accepted', repaired_stop)
        self.assertEqual(repaired_close['status'], 'accepted', repaired_close)
        auto.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual(auto.started_at, opened_at + timedelta(minutes=2))
        self.assertEqual(auto.ended_at, opened_at + timedelta(minutes=5))
        self.assertEqual(manual.started_at, opened_at + timedelta(minutes=5))
        self.assertEqual(manual.ended_at, opened_at + timedelta(minutes=8))
        self.assertEqual(OfflineFieldEvent.objects.filter(event_id=stop['event_id']).count(), 1)
        self.assertEqual(OfflineFieldEvent.objects.filter(event_id=close['event_id']).count(), 1)


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
