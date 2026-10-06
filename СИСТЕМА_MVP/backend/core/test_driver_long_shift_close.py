"""Полный водительский журнал остаётся предком закрытия через группы по 32."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core import test_offline_driver_autonomous_shift as fixtures
from core.models import OfflineFieldEvent, OperationalStateVersion
from core.offline_replay import run_offline_replay
from shifts.models import EmployeeShift, ShiftClientAction
from trips.models import Trip


class DriverLongShiftCloseTests(TestCase):
    create_registered_driver_shift = fixtures.DriverAutonomousShiftTests.create_registered_driver_shift
    driver_event = fixtures.DriverAutonomousShiftTests.driver_event
    sync = fixtures.DriverAutonomousShiftTests.sync
    sync_driver = fixtures.DriverAutonomousShiftTests.sync_driver
    local_event = fixtures.DriverAutonomousShiftTests.local_event
    opening = fixtures.DriverAutonomousShiftTests.opening
    closing = fixtures.DriverAutonomousShiftTests.closing
    manual_load = fixtures.DriverAutonomousShiftTests.manual_load
    manual_complete = fixtures.DriverAutonomousShiftTests.manual_complete
    new_shifts = fixtures.DriverAutonomousShiftTests.new_shifts

    def setUp(self):
        fixtures.DriverAutonomousShiftTests.setUp(self)
        self.open = self.opening('driver-open', 1, self.base)

    def result(self, event, **kwargs):
        return self.sync_driver([event], **kwargs).json()['results'][0]

    def fact(self, ident, sequence):
        return self.local_event(
            ident, 'driver.assignment.accepted', sequence, local_shift_id='driver-open',
            payload={'assignment_id': self.assignment.pk}, occurred_at=self.base + timedelta(minutes=5),
            depends_on=['driver-open'],
        )

    def group(self, ident, sequence, parents, *, local_id='driver-open', shift_id=None):
        event = self.local_event(
            ident, 'driver.shift.checkpoint', sequence, local_shift_id=local_id,
            payload={}, occurred_at=self.base + timedelta(minutes=60), depends_on=parents,
        )
        event['shift_id'] = shift_id
        return event

    def batch(self, events):
        with patch('core.offline_sync._log_discrepancy'):
            results = self.sync_driver(events).json()['results']
        self.assertEqual(len(results), len(events))
        for result in results:
            self.assertEqual(result['status'], 'accepted', result)

    def test_235_actions_and_trip_wait_for_last_source_then_replay_without_phone_and_open_next_shift(self):
        load = self.manual_load(self.open, 'load', 237, self.base + timedelta(minutes=10))
        end = self.manual_complete(load, 'unload', 238, self.base + timedelta(minutes=20))
        sources = [self.open] + [self.fact(f'action-{i}', i + 2) for i in range(235)] + [load, end]
        groups = [self.group(f'group-{i // 32}', 239 + i // 32,
                             [event['event_id'] for event in sources[i:i + 32]])
                  for i in range(0, len(sources), 32)]
        close = self.closing(self.open, 'close', 247, self.base + timedelta(minutes=60))
        close['depends_on'] = [group['event_id'] for group in groups]
        next_open = self.opening('next-open', 248, self.base + timedelta(minutes=65), depends_on=['close'])
        for event in [next_open, close, *reversed(groups)]:
            self.assertEqual(self.result(event)['status'], 'retry')
        saved = {row.event_id: deepcopy(row.input_envelope) for row in OfflineFieldEvent.objects.all()}
        for offset in range(0, len(sources) - 1, 80):
            self.batch(sources[offset:min(offset + 80, len(sources) - 1)])
        first = EmployeeShift.objects.get(pk=OfflineFieldEvent.objects.get(event_id='driver-open').shift_id)
        self.assertIsNone(first.closed_at)
        self.assertEqual(OfflineFieldEvent.objects.get(event_id='close').status, 'retry')
        with patch('core.offline_sync.resume_dependents', return_value=0):
            self.batch(sources[-1:])
        OfflineFieldEvent.objects.filter(status='retry').update(next_retry_at=timezone.now() - timedelta(seconds=1))
        run_offline_replay()
        self.assertFalse(OfflineFieldEvent.objects.exclude(status='accepted').exists())
        first.refresh_from_db()
        self.assertEqual(first.closed_at, self.base + timedelta(minutes=60))
        self.assertEqual(len(self.new_shifts()), 2)
        self.assertEqual(Trip.objects.count(), 1)
        self.assertEqual(Trip.objects.get().driver_control_shift_id, first.pk)
        self.assertEqual(ShiftClientAction.objects.filter(client_action_id='close').count(), 1)
        for row in OfflineFieldEvent.objects.all():
            if row.event_id in saved:
                self.assertEqual(row.input_envelope, saved[row.event_id])
        before = (EmployeeShift.objects.count(), Trip.objects.count(), OperationalStateVersion.objects.get(key='production').version)
        for event in [groups[0], close, next_open]:
            self.assertEqual(self.result(event)['status'], 'deduplicated')
        self.assertEqual(before, (EmployeeShift.objects.count(), Trip.objects.count(), OperationalStateVersion.objects.get(key='production').version))
        archive = self.driver_client.get(reverse('driver_shift_archive'), {
            'device_id': OfflineFieldEvent.objects.get(event_id='close').device_id,
            'local_shift_id': 'driver-open', 'close_event_id': 'close',
        })
        self.assertEqual(archive.status_code, 200)
        proof = archive.json()
        self.assertEqual(proof['event_count'], len(sources) + len(groups) + 1)
        self.assertEqual(set(proof['projection']['source_event_ids']),
                         {event['event_id'] for event in [*sources, *groups, close]})
        self.assertEqual(proof['projection']['completed_trip_count'], 1)

    def test_server_opened_shift_uses_numeric_id_without_a_local_opening(self):
        EmployeeShift.objects.filter(pk=self.truck_shift.pk).update(closed_at=None, workplace_code='')
        fact = self.driver_event('server-fact', 'driver.assignment.accepted', 1,
                                 payload={'assignment_id': self.assignment.pk}, occurred_at=self.base)
        self.batch([fact])
        group = self.group('server-group', 2, ['server-fact'], local_id='', shift_id=self.truck_shift.pk)
        self.assertEqual(self.result(group)['status'], 'accepted')
        close = self.driver_event('server-close', 'driver.shift.closed', 3,
                                  payload={'end_fuel': '300', 'end_mileage': '12040', 'end_engine_hours': '3008'},
                                  occurred_at=self.base + timedelta(minutes=60), depends_on=['server-group'])
        self.assertEqual(self.result(close)['status'], 'accepted')
        self.truck_shift.refresh_from_db()
        self.assertEqual(self.truck_shift.closed_at, self.base + timedelta(minutes=60))

    def test_missing_and_rejected_sources_block_every_level_and_do_not_close(self):
        self.batch([self.open])
        bad = self.fact('bad', 2)
        bad['equipment_id'] = self.excavator.pk
        self.assertEqual(self.result(bad)['status'], 'conflict')
        for event in [self.group('lower', 3, ['driver-open', 'bad']), self.group('upper', 4, ['lower'])]:
            self.assertEqual(self.result(event)['code'], 'dependency_rejected')
        close = self.closing(self.open, 'close', 5, self.base + timedelta(minutes=60))
        close['depends_on'] = ['upper']
        self.assertEqual(self.result(close)['code'], 'dependency_rejected')
        self.assertIsNone(self.new_shifts()[0].closed_at)
        self.assertEqual(self.result(self.group('missing', 6, ['not-here']))['status'], 'retry')

    def test_device_sequence_equipment_and_shift_links_are_checked(self):
        self.batch([self.open, self.fact('later', 5)])
        self.assertEqual(self.result(self.group('forward', 3, ['later']))['code'], 'dependency_order_invalid')
        self.assertEqual(self.result(self.group('other-device', 6, ['driver-open']), device_id='different-device')['code'], 'dependency_owner_mismatch')
        wrong = self.group('wrong-equipment', 7, ['driver-open'])
        wrong['equipment_id'] = self.excavator.pk
        self.assertEqual(self.result(wrong)['code'], 'equipment_context_changed')
        mismatch = self.group('wrong-shift', 8, ['driver-open'], shift_id=self.truck_shift.pk)
        self.assertEqual(self.result(mismatch)['code'], 'shift_context_changed')

    def test_accepted_parent_of_another_own_shift_cannot_prove_this_shift(self):
        self.batch([self.open])
        other = self.opening('second-open', 2, self.base + timedelta(minutes=30))
        self.batch([other])
        group = self.group('foreign-shift-parent', 3, ['driver-open'], local_id='second-open')
        self.assertEqual(self.result(group)['code'], 'checkpoint_shift_mismatch')

    def test_nonempty_context_and_32_parent_limit_are_preserved(self):
        self.batch([self.open] + [self.fact(f'f-{i}', i + 2) for i in range(32)])
        self.assertEqual(self.result(self.group('group32', 34, [f'f-{i}' for i in range(32)]))['status'], 'accepted')
        oversized = self.group('group33', 35, ['driver-open'] + [f'f-{i}' for i in range(32)])
        self.assertEqual(self.result(oversized)['code'], 'invalid_dependencies')
        self.assertEqual(self.result(self.group('empty', 36, []))['code'], 'checkpoint_context_required')
        missing = self.group('no-shift', 37, ['driver-open'], local_id='')
        self.assertEqual(self.result(missing)['code'], 'shift_id_required')

    def test_changed_retry_cannot_drop_a_missing_source(self):
        self.batch([self.open])
        group = self.group('group', 3, ['driver-open', 'missing'])
        self.assertEqual(self.result(group)['status'], 'retry')
        saved = deepcopy(OfflineFieldEvent.objects.get(event_id='group').input_envelope)
        group['depends_on'] = ['driver-open']
        self.assertEqual(self.result(group)['code'], 'event_id_reused')
        self.assertEqual(OfflineFieldEvent.objects.get(event_id='group').input_envelope, saved)
