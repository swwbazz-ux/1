"""Temporary Django contract probes for exact release/PR #106 worktrees.

Copy this file to ``СИСТЕМА_MVP/backend/core/test_s106_c1_probe.py`` in an
isolated detached worktree. It does not belong to either production branch.
"""

from datetime import timedelta

from django.utils import timezone

from core.models import OfflineFieldEvent
from core.offline_sync import normalize_offline_event
from core.test_offline_sync import OfflineEventSyncTests
from trips.models import Trip, TripStatus


class S106C1ReplayContractProbe(OfflineEventSyncTests):
    def _cancel_event(self, loaded, *, event_id, occurred_at):
        return {
            'event_id': event_id,
            'event_type': 'excavator.trip.loaded.cancelled',
            'format_version': 1,
            'occurred_at': occurred_at.isoformat(),
            'sequence': 2,
            'depends_on': [loaded['event_id']],
            'shift_id': self.shift.id,
            'equipment_id': self.excavator.id,
            'local_trip_id': loaded['local_trip_id'],
            'payload': {'local_trip_id': loaded['local_trip_id']},
        }

    def test_target_child_keeps_its_own_time_when_only_parent_clock_was_bad(self):
        self._open_shift_context_earlier(timedelta(minutes=30))
        received_at = timezone.now() - timedelta(minutes=2)
        loaded = self.load_event(
            's106-clock-parent', 1, occurred_at=received_at + timedelta(minutes=40),
        )
        child_time = received_at - timedelta(seconds=30)
        cancelled = self._cancel_event(
            loaded, event_id='s106-clock-child', occurred_at=child_time,
        )
        self._store_legacy_conflicted_event(
            loaded,
            received_at=received_at,
            error_code='device_clock_ahead',
            error_message='legacy clock conflict',
        )
        self._store_legacy_conflicted_event(
            cancelled,
            received_at=received_at,
            error_code='dependency_rejected',
            error_message='legacy dependency conflict',
        )

        results = self.sync([loaded, cancelled]).json()['results']

        self.assertEqual([item['status'] for item in results], ['accepted', 'accepted'], results)
        trip = Trip.objects.get()
        self.assertEqual(trip.loaded_at, received_at)
        self.assertEqual(trip.cancelled_at, child_time)

    def test_target_child_replays_after_parent_was_already_accepted(self):
        self._open_shift_context_earlier(timedelta(minutes=30))
        loaded_at = timezone.now() - timedelta(minutes=4)
        loaded = self.load_event('s106-accepted-parent', 1, occurred_at=loaded_at)
        first = self.sync([loaded]).json()['results'][0]
        self.assertEqual(first['status'], 'accepted', first)
        child_time = loaded_at + timedelta(minutes=1)
        cancelled = self._cancel_event(
            loaded, event_id='s106-child-after-confirmed-parent', occurred_at=child_time,
        )
        self._store_legacy_conflicted_event(
            cancelled,
            received_at=timezone.now() - timedelta(minutes=1),
            error_code='dependency_rejected',
            error_message='legacy dependency conflict after accepted parent',
        )

        result = self.sync([cancelled]).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        trip = Trip.objects.get()
        self.assertEqual(trip.status, TripStatus.CANCELLED)
        self.assertEqual(trip.cancelled_at, child_time)

    def test_target_foreign_parent_neither_vetoes_nor_supplies_child_references(self):
        self._open_shift_context_earlier(timedelta(minutes=30))
        received_at = timezone.now() - timedelta(minutes=2)
        foreign = self.load_event(
            's106-foreign-parent', 1, occurred_at=received_at - timedelta(minutes=1),
        )
        foreign_normalized = normalize_offline_event(
            foreign,
            role_code='excavator_operator',
            device_id='foreign-device',
            received_at=received_at,
        )
        OfflineFieldEvent.objects.create(
            event_id=foreign['event_id'],
            event_type=foreign['event_type'],
            format_version=foreign['format_version'],
            actor=self.operator,
            access=self.access,
            role_code='excavator_operator',
            device_id='foreign-device',
            sequence=foreign['sequence'],
            depends_on=[],
            occurred_at=timezone.datetime.fromisoformat(foreign['occurred_at']),
            received_at=received_at,
            shift=self.shift,
            equipment=self.excavator,
            local_trip_id=foreign['local_trip_id'],
            context_snapshot=foreign.get('context_snapshot', {}),
            payload=foreign['payload'],
            fingerprint=foreign_normalized['fingerprint'],
            status='accepted',
        )
        child = self.load_event(
            's106-self-contained-child',
            2,
            occurred_at=received_at - timedelta(seconds=30),
            depends_on=[foreign['event_id']],
        )
        self._store_legacy_conflicted_event(
            child,
            received_at=received_at,
            error_code='dependency_owner_mismatch',
            error_message='legacy foreign dependency',
        )

        result = self.sync([child]).json()['results'][0]

        self.assertEqual(result['status'], 'accepted', result)
        trip = Trip.objects.get()
        self.assertEqual(trip.loaded_at, timezone.datetime.fromisoformat(child['occurred_at']))
        self.assertNotEqual(trip.loaded_at, timezone.datetime.fromisoformat(foreign['occurred_at']))

    def test_same_event_id_from_another_device_cannot_bypass_identity(self):
        event = self.load_event('s106-device-identity', 1)
        first = self.sync([event], device_id='device-test-001').json()['results'][0]
        repeated = self.sync([event], device_id='device-test-001').json()['results'][0]
        changed_device = self.sync([event], device_id='device-test-002').json()['results'][0]

        self.assertEqual(first['status'], 'accepted', first)
        self.assertEqual(repeated['status'], 'deduplicated', repeated)
        self.assertEqual(changed_device['status'], 'conflict', changed_device)
        self.assertEqual(changed_device['code'], 'event_id_reused')
        self.assertEqual(Trip.objects.count(), 1)

    def test_same_code_on_unrelated_event_type_is_not_replayed(self):
        self._open_shift_context_earlier(timedelta(minutes=30))
        reason = self._excavator_downtime_reason('S106 unrelated event type')
        occurred_at = timezone.now() - timedelta(minutes=1)
        event = self._excavator_downtime_event(
            event_id='s106-unrelated-open-trip-code',
            sequence=1,
            reason=reason,
            occurred_at=occurred_at,
        )
        self._store_legacy_conflicted_event(
            event,
            received_at=timezone.now(),
            error_code='open_trip_changed',
            error_message='same code, unrelated event type',
        )

        result = self.sync([event]).json()['results'][0]

        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'open_trip_changed')
