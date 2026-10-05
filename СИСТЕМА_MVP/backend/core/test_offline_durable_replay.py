import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.db import close_old_connections, connection
from django.apps import apps
from django.core.management.color import no_style
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from core.models import NotificationIntent, OfflineFieldEvent
from core.offline_replay import run_offline_replay
from core.offline_sync import PROCESSORS, process_offline_batch
from core import test_offline_sync as offline_fixtures
from trips.models import Trip, TripClientAction
from users.models import EmployeeAccess, PushNotification


class OfflineReplayFixture:
    create_registered_driver_shift = offline_fixtures.OfflineEventSyncTests.create_registered_driver_shift
    load_event = offline_fixtures.OfflineEventSyncTests.load_event
    sync = offline_fixtures.OfflineEventSyncTests.sync
    setUp = offline_fixtures.OfflineEventSyncTests.setUp

    def chain(self, count=3):
        start = timezone.now()
        events = [self.load_event(f'durable-{index}', index + 1,
                                 occurred_at=start + timedelta(seconds=index))
                  for index in range(count)]
        for parent, child in zip(events, events[1:]):
            child['depends_on'] = [parent['event_id']]
            child['payload']['expected_open_trip_local_id'] = parent['local_trip_id']
        return events

    @staticmethod
    def make_due():
        OfflineFieldEvent.objects.filter(status='retry').update(next_retry_at=timezone.now() - timedelta(seconds=1))


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class OfflineDurableReplayTests(OfflineReplayFixture, TestCase):
    def test_original_envelope_preserves_claims_top_level_ids_and_clock_hints(self):
        event = self.load_event('original-envelope', 2, trip_id=888999,
                                device_id='device-test-001', clock_unreliable=True,
                                sent_live=True, depends_on=['missing-parent'])
        original = copy.deepcopy(event)
        result = self.sync([event]).json()['results'][0]
        self.assertEqual(result['status'], 'retry', result)
        receipt = OfflineFieldEvent.objects.get()
        envelope = copy.deepcopy(receipt.input_envelope)
        self.assertEqual(envelope['raw_event'], original)
        self.assertEqual(envelope['authority']['actor_id'], self.operator.pk)
        self.assertEqual(envelope['normalized']['trip_id'], 888999)
        self.assertEqual(envelope['normalized']['claimed_access_id'], self.access.pk)
        self.assertEqual(envelope['normalized']['shift_id'], self.shift.pk)
        self.assertEqual(envelope['normalized']['equipment_id'], self.excavator.pk)
        self.assertEqual(envelope['normalized']['received_at'], receipt.received_at.isoformat())
        self.assertIsNone(receipt.trip_id)
        self.assertIsNone(receipt.shift_id)  # Application links are not source IDs.
        event['clock_unreliable'] = False  # Top-level legacy hints are not fingerprinted.
        event['sent_live'] = False
        self.sync([event])
        receipt.refresh_from_db()
        self.assertEqual(receipt.input_envelope, envelope)

    def test_retry_and_worker_keep_first_effective_time_and_original_author(self):
        first_received = timezone.now()
        event = self.load_event('clock-retry', occurred_at=first_received + timedelta(hours=2))
        with patch.dict(PROCESSORS, {'excavator.trip.loaded': Mock(side_effect=TimeoutError)}):
            with patch('core.offline_sync.timezone.now', return_value=first_received):
                result = self.sync([event]).json()['results'][0]
        self.assertEqual(result['status'], 'retry')
        receipt = OfflineFieldEvent.objects.get()
        source = copy.deepcopy(receipt.input_envelope)
        self.make_due()
        with patch('core.offline_sync.timezone.now', return_value=first_received + timedelta(seconds=45)):
            self.assertEqual(run_offline_replay()['accepted'], 1)
        receipt.refresh_from_db()
        trip = Trip.objects.get()
        self.assertEqual(trip.loaded_at, first_received)
        self.assertEqual(trip.excavator_operator_id, self.operator.pk)
        self.assertEqual(trip.loading_shift_id, self.shift.pk)
        self.assertEqual(receipt.received_at, first_received)
        self.assertEqual(receipt.input_envelope, source)
        self.assertEqual(self.sync([event]).json()['results'][0]['status'], 'deduplicated')
        self.assertEqual(TripClientAction.objects.filter(action_type='truck_loaded').count(), 1)

    def test_children_resume_in_order_when_only_parent_is_sent_and_duplicates_do_nothing(self):
        parent, child, grandchild = self.chain()
        results = self.sync([grandchild, child]).json()['results']
        self.assertEqual([item['status'] for item in results], ['retry', 'retry'])
        envelopes = {row.event_id: row.input_envelope for row in OfflineFieldEvent.objects.all()}
        self.assertEqual(self.sync([parent]).json()['results'][0]['status'], 'accepted')
        self.assertEqual(list(OfflineFieldEvent.objects.order_by('sequence').values_list('status', flat=True)),
                         ['accepted'] * 3)
        trips = list(Trip.objects.order_by('loaded_at'))
        self.assertEqual(len(trips), 3)
        self.assertEqual(trips[0].superseded_by_id, trips[1].pk)
        self.assertEqual(trips[1].superseded_by_id, trips[2].pk)
        for row in OfflineFieldEvent.objects.exclude(event_id=parent['event_id']):
            self.assertEqual(row.input_envelope, envelopes[row.event_id])
        repeated = self.sync([parent, child, grandchild]).json()['results']
        self.assertEqual([item['status'] for item in repeated], ['deduplicated'] * 3)
        self.assertEqual(TripClientAction.objects.filter(action_type='truck_loaded').count(), 3)

    def test_bounded_parent_replay_leaves_durable_work_for_next_worker_pass(self):
        parent, child, grandchild = self.chain()
        self.sync([child, grandchild])
        with patch('core.offline_replay.MAX_REPLAY', 1):
            self.sync([parent])
        self.assertEqual(OfflineFieldEvent.objects.get(event_id=child['event_id']).status, 'accepted')
        self.assertEqual(OfflineFieldEvent.objects.get(event_id=grandchild['event_id']).status, 'retry')
        self.make_due()
        self.assertEqual(run_offline_replay(limit=1), {'processed': 1, 'accepted': 1})
        self.assertEqual(Trip.objects.count(), 3)

    def test_transient_child_failure_rolls_back_effect_and_intent_then_worker_recovers(self):
        parent, child = self.chain(2)
        child['payload']['manual_control'] = False
        self.sync([child])
        real_processor = PROCESSORS['excavator.trip.loaded']

        def fail_after_effect(access, normalized):
            result = real_processor(access, normalized)
            if normalized['event_id'] == child['event_id']:
                raise TimeoutError('after trip and notification intent')
            return result

        with patch.dict(PROCESSORS, {'excavator.trip.loaded': fail_after_effect}):
            self.assertEqual(self.sync([parent]).json()['results'][0]['status'], 'accepted')
        self.assertEqual(Trip.objects.count(), 1)
        self.assertEqual(NotificationIntent.objects.count(), 0)
        child_receipt = OfflineFieldEvent.objects.get(event_id=child['event_id'])
        self.assertEqual(child_receipt.status, 'retry')
        self.make_due()
        self.assertEqual(run_offline_replay()['accepted'], 1)
        self.assertEqual(Trip.objects.count(), 2)
        self.assertEqual(NotificationIntent.objects.count(), 1)
        self.assertEqual(PushNotification.objects.count(), 1)

    def test_bad_event_between_valid_events_does_not_rollback_or_block_them(self):
        first, bad, last = self.chain()
        last['depends_on'] = [first['event_id']]
        last['payload']['expected_open_trip_local_id'] = first['local_trip_id']
        real_processor = PROCESSORS['excavator.trip.loaded']

        def fail_middle(access, normalized):
            result = real_processor(access, normalized)
            if normalized['event_id'] == bad['event_id']:
                raise TimeoutError('middle event')
            return result

        with patch.dict(PROCESSORS, {'excavator.trip.loaded': fail_middle}):
            results = self.sync([first, bad, last]).json()['results']
        self.assertEqual([item['status'] for item in results], ['accepted', 'retry', 'accepted'])
        self.assertEqual(Trip.objects.count(), 2)
        self.assertEqual(TripClientAction.objects.filter(action_type='truck_loaded').count(), 2)

    def test_revoked_access_is_not_replaced_by_current_worker_authority(self):
        parent, child = self.chain(2)
        self.sync([child])
        with patch('core.offline_replay.MAX_REPLAY', 0):
            self.sync([parent])
        EmployeeAccess.objects.filter(pk=self.access.pk).update(is_active=False)
        self.make_due()
        self.assertEqual(run_offline_replay()['accepted'], 0)
        receipt = OfflineFieldEvent.objects.get(event_id=child['event_id'])
        self.assertEqual(receipt.error_code, 'original_access_unavailable')
        self.assertEqual(receipt.actor_id, self.operator.pk)
        self.assertEqual(Trip.objects.count(), 1)
        EmployeeAccess.objects.filter(pk=self.access.pk).update(is_active=True)
        self.make_due()
        self.assertEqual(run_offline_replay()['accepted'], 1)
        self.assertEqual(Trip.objects.order_by('-pk').first().excavator_operator_id, self.operator.pk)

    def test_legacy_receipt_requires_explicit_resubmission_and_is_not_worker_input(self):
        event = self.load_event('legacy-explicit')
        with patch.dict(PROCESSORS, {'excavator.trip.loaded': Mock(side_effect=TimeoutError)}):
            self.sync([event])
        OfflineFieldEvent.objects.update(input_envelope={})
        self.make_due()
        self.assertEqual(run_offline_replay()['processed'], 0)
        with patch.dict(PROCESSORS, {'excavator.trip.loaded': Mock(side_effect=TimeoutError)}):
            self.sync([event])
        self.assertEqual(OfflineFieldEvent.objects.get().input_envelope['source'], 'legacy_resubmission')
        self.make_due()
        self.assertEqual(run_offline_replay()['processed'], 0)
        self.assertEqual(self.sync([event]).json()['results'][0]['status'], 'accepted')

    def test_missing_parent_backoff_does_not_starve_ready_receipt(self):
        missing = self.load_event('unknown-parent-child', 1, depends_on=['unknown-parent'])
        ready = self.load_event('ready-after-transient', 2)
        self.sync([missing])
        with patch.dict(PROCESSORS, {'excavator.trip.loaded': Mock(side_effect=TimeoutError)}):
            self.sync([ready])
        self.make_due()
        self.assertEqual(run_offline_replay(limit=1), {'processed': 1, 'accepted': 0})
        self.assertEqual(run_offline_replay(limit=1), {'processed': 1, 'accepted': 1})
        self.assertEqual(Trip.objects.count(), 1)

    def test_assignment_intent_rolls_back_and_distinct_application_time_has_own_effect(self):
        from assignments.models import HaulAssignment
        from assignments.services import notify_excavator_assignment_changed
        from django.db import transaction

        with patch('users.native_push._deliver_fcm') as native, patch('users.webpush._deliver') as web:
            with self.assertRaisesRegex(RuntimeError, 'rollback'):
                with transaction.atomic():
                    notify_excavator_assignment_changed(self.assignment, released=False)
                    self.assertEqual(NotificationIntent.objects.count(), 1)
                    raise RuntimeError('rollback')
            self.assertEqual(NotificationIntent.objects.count(), 0)
            self.assertEqual(PushNotification.objects.count(), 0)
            native.assert_not_called()
            web.assert_not_called()
        notify_excavator_assignment_changed(self.assignment, released=False)
        notify_excavator_assignment_changed(self.assignment, released=False)
        self.assertEqual(NotificationIntent.objects.count(), 1)
        HaulAssignment.objects.filter(pk=self.assignment.pk).update(accepted_at=timezone.now() + timedelta(seconds=1))
        notify_excavator_assignment_changed(self.assignment, released=False)
        self.assertEqual(NotificationIntent.objects.count(), 2)

    def test_missing_claimed_fk_still_has_durable_input_and_terminal_result(self):
        event = self.load_event('missing-shift', shift_id=987654321)
        result = self.sync([event]).json()['results'][0]
        self.assertIn(result['status'], ['conflict', 'invalid'])
        receipt = OfflineFieldEvent.objects.get()
        self.assertEqual(receipt.input_envelope['raw_event']['shift_id'], 987654321)
        self.assertIsNone(receipt.shift_id)
        self.assertEqual(Trip.objects.count(), 0)

    def test_corrupt_saved_envelope_conflicts_without_aborting_next_http_event(self):
        bad = self.load_event('corrupt-http-input', 1)
        good = self.load_event('valid-after-corrupt-input', 2)
        with patch.dict(PROCESSORS, {'excavator.trip.loaded': Mock(side_effect=TimeoutError)}):
            self.sync([bad])
        receipt = OfflineFieldEvent.objects.get()
        damaged = copy.deepcopy(receipt.input_envelope)
        damaged['schema'] = 999
        OfflineFieldEvent.objects.filter(pk=receipt.pk).update(input_envelope=damaged)
        response = self.sync([bad, good])
        self.assertEqual(response.status_code, 200)
        results = response.json()['results']
        self.assertEqual([item['status'] for item in results], ['conflict', 'accepted'])
        self.assertEqual(results[0]['code'], 'saved_envelope_invalid')
        self.assertEqual(Trip.objects.count(), 1)
        receipt.refresh_from_db()
        self.assertEqual(receipt.input_envelope, damaged)
        self.assertIsNone(receipt.next_retry_at)

    def test_worker_quarantines_malformed_saved_input_and_continues(self):
        bad = self.load_event('corrupt-worker-input', 1)
        good = self.load_event('valid-worker-input', 2)
        with patch.dict(PROCESSORS, {'excavator.trip.loaded': Mock(side_effect=TimeoutError)}):
            self.sync([bad, good])
        receipt = OfflineFieldEvent.objects.get(event_id=bad['event_id'])
        damaged = copy.deepcopy(receipt.input_envelope)
        damaged['normalized'] = []
        OfflineFieldEvent.objects.filter(pk=receipt.pk).update(input_envelope=damaged)
        self.make_due()
        self.assertEqual(run_offline_replay(), {'processed': 2, 'accepted': 1})
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, 'conflict')
        self.assertEqual(receipt.error_code, 'saved_envelope_invalid')
        self.assertEqual(Trip.objects.count(), 1)

    def test_parent_acceptance_survives_corrupted_child(self):
        parent, child = self.chain(2)
        self.sync([child])
        receipt = OfflineFieldEvent.objects.get()
        damaged = copy.deepcopy(receipt.input_envelope)
        damaged['normalized']['payload'] = []
        OfflineFieldEvent.objects.filter(pk=receipt.pk).update(input_envelope=damaged)
        self.assertEqual(self.sync([parent]).json()['results'][0]['status'], 'accepted')
        receipt.refresh_from_db()
        self.assertEqual(receipt.error_code, 'saved_envelope_invalid')
        self.assertEqual(Trip.objects.count(), 1)


@skipUnless(connection.vendor == 'postgresql', 'Real PostgreSQL advisory/row locks required')
@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class OfflineDurableReplayPostgresTests(OfflineReplayFixture, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        from downtimes.models import DowntimeReason
        with connection.cursor() as cursor:
            for sql in connection.ops.sequence_reset_sql(no_style(), apps.get_models()):
                cursor.execute(sql)
        DowntimeReason.objects.get_or_create(
            name='Ожидание самосвалов',
            defaults={'short_label': 'Ожидание самосвалов', 'show_for_excavator_operator': True},
        )
        super().setUp()

    def run_competing(self, actions):
        barrier = Barrier(len(actions))

        def worker(action):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('lock_timeout', '8s', false)")
                    cursor.execute('SELECT pg_backend_pid()')
                    pid = cursor.fetchone()[0]
                barrier.wait(timeout=10)
                return pid, action()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=len(actions)) as pool:
            results = list(pool.map(worker, actions))
        self.assertEqual(len({pid for pid, _ in results}), len(actions))
        return [result for _, result in results]

    def send_direct(self, event):
        access = EmployeeAccess.objects.select_related('employee', 'role').get(pk=self.access.pk)
        return process_offline_batch(access, role_code='excavator_operator',
                                     device_id='device-test-001', events=[event])[0]

    def test_phone_duplicate_competes_with_restart_worker_one_effect_and_author(self):
        event = self.load_event('pg-worker-duplicate')
        with patch.dict(PROCESSORS, {'excavator.trip.loaded': Mock(side_effect=TimeoutError)}):
            self.send_direct(event)
        source = OfflineFieldEvent.objects.get().input_envelope
        self.make_due()
        self.run_competing([lambda: self.send_direct(event), run_offline_replay])
        receipt = OfflineFieldEvent.objects.get()
        self.assertEqual(receipt.status, 'accepted')
        self.assertEqual(receipt.input_envelope, source)
        self.assertEqual(Trip.objects.count(), 1)
        self.assertEqual(Trip.objects.get().excavator_operator_id, self.operator.pk)
        self.assertEqual(TripClientAction.objects.filter(action_type='truck_loaded').count(), 1)

    def test_parent_receive_competes_with_due_child_replay_and_finishes_both(self):
        parent, child = self.chain(2)
        self.send_direct(child)
        self.make_due()
        self.run_competing([lambda: self.send_direct(parent), run_offline_replay])
        self.assertEqual(list(OfflineFieldEvent.objects.order_by('sequence').values_list('status', flat=True)),
                         ['accepted', 'accepted'])
        self.assertEqual(Trip.objects.count(), 2)
        self.assertEqual(TripClientAction.objects.filter(action_type='truck_loaded').count(), 2)

    def test_foreign_dependency_cycle_is_rejected_before_cross_stream_row_locks(self):
        from core import offline_sync
        first = self.load_event('foreign-cycle-a', 2, depends_on=['foreign-cycle-b'])
        second = self.load_event('foreign-cycle-b', 1, depends_on=['foreign-cycle-a'])

        def send(event, device):
            access = EmployeeAccess.objects.select_related('employee', 'role').get(pk=self.access.pk)
            return process_offline_batch(access, role_code='excavator_operator',
                                         device_id=device, events=[event])[0]

        send(first, 'foreign-device-a')
        send(second, 'foreign-device-b')
        OfflineFieldEvent.objects.update(status='retry', retryable=True)
        held_receipts = Barrier(2)
        original = offline_sync._dependency_state

        def check_dependencies(access, normalized):
            # Both workers now hold their own receipt row. Locking the foreign
            # parent here would deterministically deadlock A -> B / B -> A.
            held_receipts.wait(timeout=10)
            return original(access, normalized)

        with patch('core.offline_sync._dependency_state', side_effect=check_dependencies):
            results = self.run_competing([
                lambda: send(first, 'foreign-device-a'),
                lambda: send(second, 'foreign-device-b'),
            ])
        self.assertEqual([item['code'] for item in results], ['dependency_owner_mismatch'] * 2)
        self.assertEqual([item['status'] for item in results], ['conflict'] * 2)
        self.assertEqual(Trip.objects.count(), 0)
