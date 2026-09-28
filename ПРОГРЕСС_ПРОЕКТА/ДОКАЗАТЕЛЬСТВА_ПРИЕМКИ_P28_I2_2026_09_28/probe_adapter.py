"""Acceptance expectations missing from the submitted P28-I2 suite.

Tests intentionally fail on the reported candidate when evidence is lost.
Fixtures use the candidate's fixture helpers, HTTP requests use real handlers.
"""
import copy
import json

from django.test import TestCase, override_settings
from core.models import OfflineFieldEvent, OfflineFieldEventConflict
from core.offline_sync import _resolve_trip_reference, normalize_offline_event
from trips.test_route_projection_adapter import RouteProjectionAdapterTests as Fixtures
from trips.route_projection_adapter import read_trip_route_evidence


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class AdapterAcceptanceProbes(TestCase):
    setUp = Fixtures.setUp
    factory_trip = Fixtures.factory_trip
    legacy_trip = Fixtures.legacy_trip
    driver_client = Fixtures.driver_client
    driver_event = Fixtures.driver_event
    sync_driver = Fixtures.sync_driver
    create_registered_driver_shift = Fixtures.create_registered_driver_shift

    def test_raw_id_conflict_survives_route_normalization(self):
        trip = self.factory_trip()
        original = self.driver_event(trip, event_id='probe-id', sequence=1)
        first = self.sync_driver([original], device_id='probe-device').json()['results'][0]
        self.assertEqual(first['status'], 'accepted')
        incompatible = copy.deepcopy(original)
        incompatible['sequence'] = 2
        second = self.sync_driver([incompatible], device_id='probe-device').json()['results'][0]
        self.assertEqual(second['code'], 'event_id_reused')
        self.assertEqual(OfflineFieldEventConflict.objects.count(), 1)
        evidence = read_trip_route_evidence(trip.pk)
        print('P28_I2_PROBE', json.dumps({
            'case': 'raw_identity_collision', 'server': second,
            'projection': evidence.projection.status,
            'diagnostics': evidence.projection.diagnostics,
            'conflict_sources': sum(s['source_kind'] == 'offline_field_event_conflict' for s in evidence.sources),
            'expected': 'integrity_conflict',
        }, ensure_ascii=False), flush=True)
        self.assertEqual(evidence.projection.status, 'integrity_conflict')

    def test_local_reference_retry_is_collected_after_binding_exists(self):
        trip = self.factory_trip()
        early = self.driver_event(trip, event_id='probe-pending', sequence=2)
        early.pop('trip_id')
        early['payload'].pop('trip_id')
        early['local_trip_id'] = 'local-load-X'
        result = self.sync_driver([early], device_id='probe-local-device').json()['results'][0]
        self.assertEqual(result['status'], 'retry')
        self.assertEqual(result['code'], 'trip_reference_pending')
        pending = OfflineFieldEvent.objects.get(event_id=early['event_id'])
        self.assertIsNone(pending.trip_id)
        # Existing accepted mapping is an explicitly constructed DB fixture;
        # the dependent retry above came through the real HTTP sync handler.
        OfflineFieldEvent.objects.create(
            event_id='probe-load-map', event_type='driver.trip.loaded',
            actor=self.driver, access=self.driver_access, role_code='driver',
            device_id='probe-local-device', sequence=1,
            occurred_at=trip.loaded_at, local_trip_id='local-load-X',
            trip=trip, shift=self.truck_shift, equipment=self.truck,
            payload={}, fingerprint='fixture-load-map', status='accepted',
            result_payload={'server_ids': {'trip_id': trip.pk}},
        )
        normalized = normalize_offline_event(early, role_code='driver', device_id='probe-local-device')
        resolved = _resolve_trip_reference(self.driver_access, normalized)
        self.assertEqual(resolved.pk, trip.pk)
        evidence = read_trip_route_evidence(trip.pk)
        event_ids = [s['event_id'] for s in evidence.sources if s['source_kind'] == 'offline_field_event']
        print('P28_I2_PROBE', json.dumps({
            'case': 'local_reference_pending', 'server': result,
            'existing_resolver_trip_id': resolved.pk, 'collected_event_ids': event_ids,
            'missing_expected_event': early['event_id'],
        }, ensure_ascii=False), flush=True)
        self.assertIn(early['event_id'], event_ids)
