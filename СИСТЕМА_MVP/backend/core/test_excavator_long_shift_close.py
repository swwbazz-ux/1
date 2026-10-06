"""Bounded checkpoint receipts must prove every leaf before applying close."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from core import test_excavator_offline_work_context as fixtures
from core.models import OfflineFieldEvent, OperationalStateVersion
from core.offline_replay import run_offline_replay
from downtimes.models import DowntimeEvent
from shifts.models import EmployeeShift, ShiftClientAction


class ExcavatorLongShiftCloseTests(TestCase):
    base_fixture = fixtures.ExcavatorOfflineWorkContextTests.base_fixture
    create_registered_driver_shift = base_fixture.create_registered_driver_shift
    event = base_fixture.event
    sync = base_fixture.sync
    local = base_fixture.local
    opening = base_fixture.opening
    closing = base_fixture.closing
    result = base_fixture.result
    context = fixtures.ExcavatorOfflineWorkContextTests.context
    placement = fixtures.ExcavatorOfflineWorkContextTests.placement

    def setUp(self):
        fixtures.ExcavatorOfflineWorkContextTests.setUp(self)

    def checkpoint(self, ident, sequence, parents):
        event = self.local(ident, 'excavator.shift.checkpoint', sequence, minutes=60)
        event['depends_on'] = list(parents)
        return event

    def batch(self, events):
        results = self.sync(events).json()['results']
        self.assertEqual(len(results), len(events))
        for result in results:
            self.assertEqual(result['status'], 'accepted', result)

    def test_235_facts_close_waits_for_last_leaf_and_worker_recovers_originals_without_phone(self):
        sources = [self.opening()] + [self.context(f'face-{i}', i + 2, minutes=1 + i / 10)
                                    for i in range(235)]
        groups = [self.checkpoint(f'group-{i // 32}', 237 + i // 32,
                                 [event['event_id'] for event in sources[i:i + 32]])
                  for i in range(0, len(sources), 32)]
        close = self.closing(sequence=245)
        close['depends_on'] = [event['event_id'] for event in groups]
        self.assertEqual(self.result(close)['status'], 'retry')
        for group in reversed(groups):
            self.assertEqual(self.result(group)['status'], 'retry')
        saved = {receipt.event_id: deepcopy(receipt.input_envelope)
                 for receipt in OfflineFieldEvent.objects.all()}
        for offset in range(0, len(sources) - 1, 80):
            self.batch(sources[offset:min(offset + 80, len(sources) - 1)])
        shift = EmployeeShift.objects.get(pk=OfflineFieldEvent.objects.get(event_id='eo-open').shift_id)
        self.assertIsNone(shift.closed_at)
        self.assertEqual(OfflineFieldEvent.objects.get(event_id='eo-close').status, 'retry')
        # Simulate a worker restart after the final leaf committed but before
        # HTTP wakeup. Stored envelopes alone must complete all remaining work.
        with patch('core.offline_sync.resume_dependents', return_value=0):
            self.batch(sources[-1:])
        OfflineFieldEvent.objects.filter(status='retry').update(next_retry_at=timezone.now() - timedelta(seconds=1))
        run_offline_replay()
        self.assertFalse(OfflineFieldEvent.objects.exclude(status='accepted').exists())
        self.assertEqual(OfflineFieldEvent.objects.count(), 245)
        shift.refresh_from_db()
        self.assertEqual(shift.closed_at, self.base + timedelta(hours=1))
        for receipt in OfflineFieldEvent.objects.all():
            if receipt.event_id in saved:
                self.assertEqual(receipt.input_envelope, saved[receipt.event_id])
        for source in sources:
            self.assertEqual(OfflineFieldEvent.objects.get(event_id=source['event_id']).input_envelope['raw_event'], source)
        self.assertEqual(self.result(close)['status'], 'deduplicated')
        self.assertEqual(ShiftClientAction.objects.filter(client_action_id='eo-close').count(), 1)

    def test_multilevel_receipts_resume_after_open_and_never_duplicate_domain_effects(self):
        lower = self.checkpoint('lower', 2, ['eo-open'])
        upper = self.checkpoint('upper', 3, ['lower'])
        close = self.closing(sequence=4)
        close['depends_on'] = ['upper']
        for event in [close, upper, lower]:
            self.assertEqual(self.result(event)['status'], 'retry')
        self.assertEqual(self.result(self.opening())['status'], 'accepted')
        self.assertFalse(OfflineFieldEvent.objects.exclude(status='accepted').exists())
        before = (EmployeeShift.objects.count(), DowntimeEvent.objects.count(),
                  ShiftClientAction.objects.count(), OperationalStateVersion.objects.get(key='production').version)
        for event in [lower, upper, close]:
            self.assertEqual(self.result(event)['status'], 'deduplicated')
        self.assertEqual(before, (EmployeeShift.objects.count(), DowntimeEvent.objects.count(),
                                 ShiftClientAction.objects.count(), OperationalStateVersion.objects.get(key='production').version))

    def test_rejected_leaf_blocks_every_parent_and_close(self):
        self.result(self.opening())
        bad = self.context('bad', 2)
        bad['payload']['dump_point_ids'] = [987654]
        self.assertEqual(self.result(bad)['status'], 'conflict')
        for event in [self.checkpoint('lower', 3, ['eo-open', 'bad']),
                      self.checkpoint('upper', 4, ['lower'])]:
            self.assertEqual(self.result(event)['code'], 'dependency_rejected')
        close = self.closing(sequence=5)
        close['depends_on'] = ['upper']
        self.assertEqual(self.result(close)['code'], 'dependency_rejected')
        self.assertTrue(EmployeeShift.objects.filter(employee=self.operator, closed_at__isnull=True).exists())

    def test_checkpoint_cannot_bypass_device_or_sequence_ownership(self):
        self.result(self.opening())
        group = self.checkpoint('foreign-device', 2, ['eo-open'])
        self.assertEqual(self.result(group, device_id='different-device-001')['code'], 'dependency_owner_mismatch')
        context = self.context('later', 5)
        self.assertEqual(self.result(context)['status'], 'accepted')
        self.assertEqual(self.result(self.checkpoint('forward', 3, ['later']))['code'], 'dependency_order_invalid')

    def test_checkpoint_validates_shift_and_equipment_even_with_accepted_parents(self):
        self.result(self.opening())
        for index, field, value, expected in [
            (0, 'equipment_id', self.other_excavator.pk, 'equipment_context_changed'),
            (1, 'shift_id', self.shift.pk, 'shift_context_changed'),
            (2, 'local_shift_id', 'missing-shift', 'shift_reference_pending'),
        ]:
            group = self.checkpoint(f'wrong-{index}', index + 2, ['eo-open'])
            group[field] = value
            self.assertEqual(self.result(group)['code'], expected)

    def test_normal_protocol_limits_and_nonempty_local_context_remain_required(self):
        self.result(self.opening())
        empty = self.checkpoint('empty', 2, [])
        self.assertEqual(self.result(empty)['code'], 'checkpoint_context_required')
        nonlocal_event = self.checkpoint('nonlocal', 3, ['eo-open'])
        del nonlocal_event['local_shift_id']
        self.assertEqual(self.result(nonlocal_event)['code'], 'checkpoint_context_required')
        oversized = self.checkpoint('oversized', 4, [f'leaf-{i}' for i in range(33)])
        self.assertEqual(self.result(oversized)['status'], 'invalid')

    def test_altered_retry_cannot_drop_a_missing_leaf_from_saved_proof(self):
        self.result(self.opening())
        group = self.checkpoint('group', 3, ['eo-open', 'missing'])
        self.assertEqual(self.result(group)['status'], 'retry')
        saved = deepcopy(OfflineFieldEvent.objects.get(event_id='group').input_envelope)
        group['depends_on'] = ['eo-open']
        self.assertEqual(self.result(group)['code'], 'event_id_reused')
        self.assertEqual(OfflineFieldEvent.objects.get(event_id='group').input_envelope, saved)
