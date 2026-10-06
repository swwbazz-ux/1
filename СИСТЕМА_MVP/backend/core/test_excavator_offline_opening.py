"""Серверный приём собственной автономной смены; клиентский UI — отдельный этап."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from core import test_free_bucket_sync as fixtures
from core.models import OfflineFieldEvent
from core.offline_sync import normalize_offline_event
from shifts.models import EmployeeShift, ShiftClientAction
from downtimes.models import DowntimeEvent


class ExcavatorOfflineOpeningTests(TestCase):
    create_registered_driver_shift = fixtures.FreeBucketServerIntegrationTests.create_registered_driver_shift
    event = fixtures.FreeBucketServerIntegrationTests.event
    sync = fixtures.FreeBucketServerIntegrationTests.sync

    def setUp(self):
        fixtures.FreeBucketServerIntegrationTests.setUp(self)
        self.base = timezone.now() - timedelta(hours=3)
        EmployeeShift.objects.filter(pk=self.shift.pk).update(
            opened_at=self.base - timedelta(hours=12), closed_at=self.base - timedelta(minutes=30),
        )
        self.shift.refresh_from_db()

    def local(self, ident, kind, sequence, opening='eo-open', payload=None, minutes=0):
        event = self.event(ident, kind, sequence, payload=payload or {},
                           occurred_at=self.base + timedelta(minutes=minutes))
        event['shift_id'] = None
        event['context_snapshot'].pop('shift_id', None)
        event['local_shift_id'] = opening
        return event

    def opening(self, ident='eo-open', sequence=1, minutes=0):
        return self.local(ident, 'excavator.shift.opened', sequence, opening=ident,
                          payload={'fuel': '100', 'engine_hours': '1200'}, minutes=minutes)

    def closing(self, opening='eo-open', ident='eo-close', sequence=2, minutes=60):
        return self.local(ident, 'excavator.shift.closed', sequence, opening=opening,
                          payload={'fuel': '90', 'engine_hours': '1201'}, minutes=minutes)

    def result(self, event, **options):
        response = self.sync([event], **options)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['results'][0]

    def test_open_preserves_time_and_repeated_delivery_has_one_shift(self):
        event = self.opening()
        result = self.result(event)
        self.assertEqual(result['status'], 'accepted', result)
        shift = EmployeeShift.objects.get(pk=result['server_ids']['shift_id'])
        self.assertEqual(shift.opened_at, self.base)
        self.assertEqual(shift.start_engine_hours, Decimal('1200'))
        self.assertEqual(self.result(event)['status'], 'deduplicated')
        self.assertEqual(ShiftClientAction.objects.filter(client_action_id='eo-open').count(), 1)

    def test_close_before_open_is_automatically_applied_without_phone_resend(self):
        child = self.closing()
        first = self.result(child)
        self.assertEqual(first['status'], 'retry', first)
        saved = deepcopy(OfflineFieldEvent.objects.get(event_id='eo-close').input_envelope)
        parent = self.result(self.opening())
        self.assertEqual(parent['status'], 'accepted', parent)
        receipt = OfflineFieldEvent.objects.get(event_id='eo-close')
        self.assertEqual(receipt.status, 'accepted', receipt.error_message)
        self.assertEqual(receipt.input_envelope, saved)
        self.assertIsNone(receipt.input_envelope['normalized']['shift_id'])
        shift = EmployeeShift.objects.get(pk=parent['server_ids']['shift_id'])
        self.assertEqual(shift.closed_at, self.base + timedelta(minutes=60))
        self.assertEqual(self.result(child)['status'], 'deduplicated')

    def test_open_close_next_own_open_keeps_both_histories(self):
        first = self.result(self.opening())
        self.assertEqual(first['status'], 'accepted', first)
        closed = self.result(self.closing())
        self.assertEqual(closed['status'], 'accepted', closed)
        second = self.result(self.opening('eo-next', 3, 120))
        self.assertEqual(second['status'], 'accepted', second)
        self.assertNotEqual(first['server_ids']['shift_id'], second['server_ids']['shift_id'])
        old = EmployeeShift.objects.get(pk=first['server_ids']['shift_id'])
        new = EmployeeShift.objects.get(pk=second['server_ids']['shift_id'])
        self.assertEqual(old.closed_at, self.base + timedelta(minutes=60))
        self.assertEqual(new.opened_at, self.base + timedelta(minutes=120))
        self.assertIsNone(new.closed_at)

    def test_work_before_open_replays_against_original_shift(self):
        child = self.local('eo-wait', 'excavator.downtime.started', 2,
                           payload={'reason_id': self.reason.pk}, minutes=10)
        self.assertEqual(self.result(child)['status'], 'retry')
        self.assertEqual(self.result(self.opening())['status'], 'accepted')
        receipt = OfflineFieldEvent.objects.get(event_id='eo-wait')
        self.assertEqual(receipt.status, 'accepted', receipt.error_message)
        event = DowntimeEvent.objects.get(pk=receipt.result_payload['server_ids']['downtime_event_id'])
        self.assertEqual(event.started_at, self.base + timedelta(minutes=10))

    def test_other_device_cannot_bind_to_saved_local_shift(self):
        self.assertEqual(self.result(self.opening())['status'], 'accepted')
        result = self.result(self.closing(), device_id='another-device-001')
        self.assertEqual(result['status'], 'retry', result)
        self.assertEqual(result['code'], 'shift_reference_pending')
        self.assertTrue(EmployeeShift.objects.filter(employee=self.operator, closed_at__isnull=True).exists())

    def test_explicit_server_id_cannot_override_local_parent(self):
        self.assertEqual(self.result(self.opening())['status'], 'accepted')
        child = self.closing()
        child['shift_id'] = self.shift.pk
        result = self.result(child)
        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'shift_context_changed')

    def test_local_identity_is_immutable_on_retry(self):
        child = self.closing()
        self.assertEqual(self.result(child)['status'], 'retry')
        child['local_shift_id'] = 'some-other-opening'
        result = self.result(child)
        self.assertEqual(result['code'], 'event_id_reused', result)

    def test_new_open_does_not_close_existing_shift(self):
        first = self.result(self.opening())
        self.assertEqual(first['status'], 'accepted', first)
        result = self.result(self.opening('eo-overlap', 2, 30))
        self.assertEqual(result['status'], 'conflict', result)
        self.assertEqual(result['code'], 'employee_shift_already_open')
        self.assertIsNone(EmployeeShift.objects.get(pk=first['server_ids']['shift_id']).closed_at)

    def test_open_rejects_equipment_outside_current_assignment(self):
        event = self.opening()
        event['equipment_id'] = self.other_excavator.pk
        result = self.result(event)
        self.assertEqual(result['code'], 'equipment_context_changed', result)
        self.assertFalse(EmployeeShift.objects.filter(employee=self.operator, closed_at__isnull=True).exists())

    def test_legacy_normalization_omits_optional_local_identity(self):
        event = self.closing()
        del event['local_shift_id']
        event['shift_id'] = self.shift.pk
        normalized = normalize_offline_event(event, role_code='excavator_operator', device_id='free-bucket-device-001')
        self.assertNotIn('local_shift_id', normalized)

    def test_local_references_must_agree(self):
        event = self.opening()
        event['payload']['local_shift_id'] = 'different-local-shift'
        result = self.result(event)
        self.assertEqual(result['status'], 'invalid', result)
        self.assertEqual(result['code'], 'local_shift_id_mismatch')

    def test_parent_sequence_must_precede_implicit_child(self):
        self.assertEqual(self.result(self.opening(sequence=3))['status'], 'accepted')
        result = self.result(self.closing(sequence=2))
        self.assertEqual(result['code'], 'dependency_order_invalid', result)

    def test_invalid_fuel_percent_is_a_domain_error_not_server_retry(self):
        event = self.opening()
        event['payload']['fuel_percent'] = 'invalid'
        result = self.result(event)
        self.assertEqual(result['status'], 'conflict', result)
        self.assertNotEqual(result['code'], 'temporary_server_error')

    def test_legacy_fingerprints_match_published_previous_code(self):
        # Golden values computed with e065571 normalizer before this change.
        from django.utils.dateparse import parse_datetime
        for role, prefix, expected in [
            ('driver', 'driver', '047150c6b2fee94fae5f4f5be9f91d8266d154e7b0ab568aa6adbc9db897d018'),
            ('excavator_operator', 'excavator', '82bbbe955d7e506107829d21f147cc6b7b79aae020f59b310fc07c46fb0cf50e'),
        ]:
            event = {'format_version': 1, 'event_id': 'legacy-fixed-close',
                     'event_type': prefix + '.shift.closed', 'sequence': 1,
                     'occurred_at': '2026-10-01T00:00:00Z', 'shift_id': 1, 'equipment_id': 2,
                     'payload': {'fuel': '90', 'engine_hours': '1201'}}
            with self.subTest(role=role):
                normalized = normalize_offline_event(
                    event, role_code=role, device_id='legacy-device-001',
                    received_at=parse_datetime('2026-10-01T01:00:00Z'),
                )
                self.assertEqual(normalized['fingerprint'], expected)
