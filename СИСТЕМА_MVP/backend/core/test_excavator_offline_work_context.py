"""Immutable offline face settings and non-destructive live projection."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from assignments.models import ExcavatorPlacement
from core import test_excavator_offline_opening as fixtures
from core.models import OfflineFieldEvent, OperationalStateVersion
from downtimes.models import DowntimeEvent


class ExcavatorOfflineWorkContextTests(TestCase):
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
        ExcavatorPlacement.objects.filter(excavator=self.excavator).update(
            work_context_updated_at=self.base - timedelta(minutes=1),
        )

    def context(self, ident='face', sequence=2, minutes=10, horizon='135'):
        return self.local(
            ident, 'excavator.work_context.changed', sequence, minutes=minutes,
            payload={'rock_type_id': self.rock.pk, 'dump_point_ids': [self.dump_point.pk],
                     'loading_horizon': horizon, 'loading_block': '05'},
        )

    def placement(self):
        return ExcavatorPlacement.objects.get(excavator=self.excavator)

    def test_context_before_open_replays_without_resend_and_preserves_input(self):
        event = self.context()
        self.assertEqual(self.result(event)['status'], 'retry')
        saved = deepcopy(OfflineFieldEvent.objects.get(event_id='face').input_envelope)
        self.assertEqual(self.result(self.opening())['status'], 'accepted')
        receipt = OfflineFieldEvent.objects.get(event_id='face')
        self.assertEqual(receipt.status, 'accepted', receipt.error_message)
        self.assertEqual(receipt.input_envelope, saved)
        self.assertTrue(receipt.result_payload['projection_applied'])
        self.assertEqual(self.placement().loading_horizon, '135')
        self.assertEqual(self.placement().work_context_updated_at, self.base + timedelta(minutes=10))

    def test_duplicate_has_one_effect_and_settings_never_start_a_move(self):
        self.result(self.opening())
        before = DowntimeEvent.objects.count()
        event = self.context()
        self.assertEqual(self.result(event)['status'], 'accepted')
        version = OperationalStateVersion.objects.get(key='production').version
        self.assertEqual(self.result(event)['status'], 'deduplicated')
        self.assertEqual(OperationalStateVersion.objects.get(key='production').version, version)
        self.assertEqual(DowntimeEvent.objects.count(), before)

    def test_older_setting_is_retained_without_overwriting_newer_context(self):
        self.result(self.opening())
        self.assertTrue(self.result(self.context('new', 3, 20, '145'))['projection_applied'])
        result = self.result(self.context('old', 2, 10, '125'))
        self.assertEqual(result['status'], 'accepted', result)
        self.assertFalse(result['projection_applied'])
        self.assertEqual(self.placement().loading_horizon, '145')
        self.assertEqual(OfflineFieldEvent.objects.get(event_id='old').payload['loading_horizon'], '125')

    def test_equal_timestamp_uses_sequence_only_in_the_same_original_stream(self):
        self.result(self.opening())
        self.assertTrue(self.result(self.context('first', 2, 10, '125'))['projection_applied'])
        self.assertTrue(self.result(self.context('next', 3, 10, '145'))['projection_applied'])
        self.assertEqual(self.placement().loading_horizon, '145')

    def test_new_shift_prevents_old_context_from_changing_current_equipment(self):
        self.result(self.opening())
        self.result(self.closing(sequence=3))
        self.result(self.opening('next-shift', 4, 120))
        result = self.result(self.context('old-face', 2, 30, '175'))
        self.assertEqual(result['status'], 'accepted', result)
        self.assertFalse(result['projection_applied'])
        self.assertEqual(self.placement().loading_horizon, '125')

    def test_missing_destination_rejects_entire_selection_without_partial_update(self):
        self.result(self.opening())
        event = self.context()
        event['payload']['dump_point_ids'].append(987654)
        result = self.result(event)
        self.assertEqual(result['code'], 'dump_reference_unavailable', result)
        self.assertEqual(self.placement().loading_horizon, '125')

    def test_operator_cannot_change_dispatcher_owned_haul_distance(self):
        placement = self.placement()
        placement.dump_point_settings.update(transport_distance_km=Decimal('6.50'))
        self.result(self.opening())
        event = self.context()
        event['payload']['transport_distance_km'] = '999'
        event['payload']['destinations'] = [{'dump_point_id': self.dump_point.pk, 'transport_distance_km': '999'}]
        self.assertTrue(self.result(event)['projection_applied'])
        self.assertEqual(self.placement().dump_point_settings.get().transport_distance_km, Decimal('6.50'))

    def test_other_device_cannot_apply_context_to_this_local_shift(self):
        self.result(self.opening())
        result = self.result(self.context(), device_id='another-device-001')
        self.assertEqual(result['code'], 'shift_reference_pending', result)
        self.assertEqual(self.placement().loading_horizon, '125')

    def test_projection_failure_rolls_back_placement_and_retries_original(self):
        self.result(self.opening())
        event = self.context()
        with patch('core.offline_sync.bump_operational_state', side_effect=RuntimeError('forced context failure')):
            result = self.result(event)
        self.assertEqual(result['status'], 'retry', result)
        self.assertEqual(self.placement().loading_horizon, '125')
        self.assertEqual(self.result(event)['status'], 'accepted')
        self.assertEqual(self.placement().loading_horizon, '135')
