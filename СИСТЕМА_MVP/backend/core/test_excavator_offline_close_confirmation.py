"""Explicit on-device review survives durable delivery without an online challenge."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from core import test_excavator_offline_opening as fixtures
from core.models import OfflineFieldEvent
from shifts.models import EmployeeShift, ExcavatorShiftReadingConfirmation


class ExcavatorOfflineCloseConfirmationTests(TestCase):
    base_fixture = fixtures.ExcavatorOfflineOpeningTests
    create_registered_driver_shift = base_fixture.create_registered_driver_shift
    event = base_fixture.event
    sync = base_fixture.sync
    local = base_fixture.local
    opening = base_fixture.opening
    closing = base_fixture.closing
    result = base_fixture.result

    def setUp(self):
        self.base_fixture.setUp(self)
        self.excavator.model.fuel_capacity_limit_l = Decimal('7000')
        self.excavator.model.save(update_fields=['fuel_capacity_limit_l'])

    def confirmed_close(self):
        event = self.closing()
        event['depends_on'] = ['eo-open']
        event['payload'].update({
            'fuel': '8400', 'fuel_percent': '120', 'engine_hours': '1213', 'fuel_capacity_l': '7000',
            'reading_confirmation': {
                'version': 1, 'accepted': True, 'shift_ref': 'local:eo-open', 'equipment_id': self.excavator.pk,
                'fuel': '8400', 'fuel_percent': '120', 'engine_hours': '1213',
                'start_engine_hours': '1200', 'fuel_capacity_l': '7000',
                'warning_codes': ['fuel_above_capacity', 'engine_hours_delta_high'],
            },
        })
        return event

    def test_anomaly_without_explicit_confirmation_leaves_shift_open(self):
        self.result(self.opening())
        event = self.confirmed_close()
        del event['payload']['reading_confirmation']
        self.assertEqual(self.result(event)['code'], 'confirmation_required')
        self.assertTrue(EmployeeShift.objects.filter(employee=self.operator, closed_at__isnull=True).exists())
        self.assertFalse(ExcavatorShiftReadingConfirmation.objects.exists())

    def test_confirmation_before_open_replays_once_with_original_readings_and_time(self):
        event = self.confirmed_close()
        self.assertEqual(self.result(event)['status'], 'retry')
        original = deepcopy(OfflineFieldEvent.objects.get(event_id='eo-close').input_envelope)
        opened = self.result(self.opening())
        receipt = OfflineFieldEvent.objects.get(event_id='eo-close')
        self.assertEqual(receipt.status, 'accepted', receipt.error_message)
        self.assertEqual(receipt.input_envelope, original)
        shift = EmployeeShift.objects.get(pk=opened['server_ids']['shift_id'])
        self.assertEqual(shift.closed_at, self.base + timedelta(hours=1))
        self.assertEqual((shift.end_fuel, shift.end_engine_hours), (Decimal('8400'), Decimal('1213')))
        self.assertEqual(self.result(event)['status'], 'deduplicated')
        audit = ExcavatorShiftReadingConfirmation.objects.get()
        self.assertEqual(audit.shift_id, shift.pk)
        self.assertEqual([warning['code'] for warning in audit.warnings], event['payload']['reading_confirmation']['warning_codes'])

    def test_two_day_delivery_does_not_expire_the_original_confirmation(self):
        self.base -= timedelta(days=2)
        self.result(self.opening())
        event = self.confirmed_close()
        result = self.result(event)
        self.assertEqual(result['status'], 'accepted', result)
        self.assertEqual(ExcavatorShiftReadingConfirmation.objects.get().shift.closed_at, self.base + timedelta(hours=1))

    def test_changed_confirmation_context_cannot_close_or_create_audit(self):
        self.result(self.opening())
        changes = {
            'accepted': False, 'version': True, 'shift_ref': 'local:other-shift',
            'equipment_id': self.other_excavator.pk, 'fuel': '8399', 'fuel_percent': '119',
            'engine_hours': '1214', 'start_engine_hours': '1199', 'fuel_capacity_l': '8000',
            'warning_codes': ['fuel_above_capacity'],
        }
        for sequence, (key, value) in enumerate(changes.items(), start=2):
            with self.subTest(key=key):
                event = self.confirmed_close()
                event['event_id'] = f'bad-close-{key}'
                event['sequence'] = sequence
                event['payload']['reading_confirmation'][key] = value
                self.assertEqual(self.result(event)['code'], 'confirmation_context_changed')
        self.assertFalse(ExcavatorShiftReadingConfirmation.objects.exists())
        self.assertTrue(EmployeeShift.objects.filter(employee=self.operator, closed_at__isnull=True).exists())

    def test_changed_model_capacity_is_not_silently_accepted(self):
        self.result(self.opening())
        self.excavator.model.fuel_capacity_limit_l = Decimal('8000')
        self.excavator.model.save(update_fields=['fuel_capacity_limit_l'])
        event = self.confirmed_close()
        event['payload']['fuel'] = '9600'
        self.assertEqual(self.result(event)['code'], 'confirmation_context_changed')
        self.assertFalse(ExcavatorShiftReadingConfirmation.objects.exists())

    def test_confirmation_cannot_cross_device_and_original_event_cannot_be_amended(self):
        self.result(self.opening())
        event = self.confirmed_close()
        foreign = deepcopy(event)
        foreign['event_id'] = 'other-device-close'
        self.assertEqual(self.result(foreign, device_id='another-device-001')['code'], 'dependency_owner_mismatch')
        self.assertEqual(self.result(event)['status'], 'accepted')
        event['payload']['reading_confirmation']['engine_hours'] = '1214'
        self.assertEqual(self.result(event)['code'], 'event_id_reused')

    def test_boundary_close_without_anomalies_does_not_need_confirmation(self):
        self.result(self.opening())
        event = self.closing()
        event['payload'].update({'fuel': '7000', 'fuel_percent': '100', 'engine_hours': '1212'})
        self.assertEqual(self.result(event)['status'], 'accepted')
        self.assertFalse(ExcavatorShiftReadingConfirmation.objects.exists())

    def test_failure_rolls_back_audit_and_close_then_replays_the_same_original(self):
        self.result(self.opening())
        event = self.confirmed_close()
        with patch('core.models.bump_operational_state', side_effect=RuntimeError('forced close rollback')):
            self.assertEqual(self.result(event)['status'], 'retry')
        self.assertFalse(ExcavatorShiftReadingConfirmation.objects.exists())
        self.assertTrue(EmployeeShift.objects.filter(employee=self.operator, closed_at__isnull=True).exists())
        self.assertEqual(self.result(event)['status'], 'accepted')
        self.assertEqual(ExcavatorShiftReadingConfirmation.objects.count(), 1)

    def test_pre_existing_server_shift_can_use_device_confirmation_with_its_own_reference(self):
        opened = self.result(self.opening())
        event = self.confirmed_close()
        del event['local_shift_id']
        event['depends_on'] = []
        event['shift_id'] = opened['server_ids']['shift_id']
        event['payload']['reading_confirmation']['shift_ref'] = f"server:{event['shift_id']}"
        result = self.result(event)
        self.assertEqual(result['status'], 'accepted', result)

    def test_late_duplicate_does_not_change_the_next_shift(self):
        self.result(self.opening())
        event = self.confirmed_close()
        self.assertEqual(self.result(event)['status'], 'accepted')
        newer = self.result(self.opening('next-shift', 3, 120))
        self.assertEqual(newer['status'], 'accepted', newer)
        self.assertEqual(self.result(event)['status'], 'deduplicated')
        self.assertIsNone(EmployeeShift.objects.get(pk=newer['server_ids']['shift_id']).closed_at)
        self.assertEqual(ExcavatorShiftReadingConfirmation.objects.count(), 1)
