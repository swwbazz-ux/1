"""P28-I2-R1 acceptance: a local-only ORIGINAL must carry its ID collision.

The pending receipt and incompatible repeat pass through real sync HTTP views.
The already accepted load mapping is an explicit DB fixture, as in I2-C2.
This module does not modify the candidate or the original acceptance probes.
"""
import copy
import json

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from core.models import OfflineFieldEvent, OfflineFieldEventConflict
from core.offline_sync import _resolve_trip_reference, normalize_offline_event
from references.models import Equipment
from trips.route_projection_adapter import read_trip_route_evidence
from trips.test_route_projection_adapter import RouteProjectionAdapterTests as Fixtures


@override_settings(EXCAVATOR_MANUAL_LOADING_ENABLED=True)
class LocalOriginalCollisionProbe(TestCase):
    setUp = Fixtures.setUp
    factory_trip = Fixtures.factory_trip
    legacy_trip = Fixtures.legacy_trip
    driver_client = Fixtures.driver_client
    driver_event = Fixtures.driver_event
    sync_driver = Fixtures.sync_driver
    create_registered_driver_shift = Fixtures.create_registered_driver_shift

    def test_locally_bound_original_collision_reaches_both_trips(self):
        trip_x = self.factory_trip()
        trip_y = self.legacy_trip(truck=self.other_truck)
        truck_z = Equipment.objects.create(
            equipment_type=self.truck_type,
            model=self.truck_model,
            garage_number='P28-R1-C1C2-Z',
        )
        trip_z = self.legacy_trip(truck=truck_z)
        z_before = read_trip_route_evidence(trip_z.pk).to_dict()
        device = 'probe-r1-local-original-device'
        local_id = 'probe-r1-local-original-X'

        original = self.driver_event(
            trip_x, event_id='probe-r1-local-original-id', sequence=2,
        )
        original.pop('trip_id')
        original['payload'].pop('trip_id')
        original['local_trip_id'] = local_id
        pending_result = self.sync_driver(
            [original], device_id=device,
        ).json()['results'][0]
        self.assertEqual(pending_result['status'], 'retry')
        self.assertEqual(pending_result['code'], 'trip_reference_pending')
        pending = OfflineFieldEvent.objects.get(event_id=original['event_id'])
        self.assertIsNone(pending.trip_id)

        # Explicit mapping fixture only. The original and duplicate themselves
        # are real HTTP-handler outcomes, not hand-built conflict rows.
        OfflineFieldEvent.objects.create(
            event_id='probe-r1-local-original-load-map',
            event_type='driver.trip.loaded',
            actor=self.driver, access=self.driver_access, role_code='driver',
            device_id=device, sequence=1, occurred_at=trip_x.loaded_at,
            local_trip_id=local_id, trip=trip_x, shift=self.truck_shift,
            equipment=self.truck, payload={},
            fingerprint='fixture-r1-local-original-load-map', status='accepted',
            result_payload={'server_ids': {'trip_id': trip_x.pk}},
        )
        normalized = normalize_offline_event(
            original, role_code='driver', device_id=device,
        )
        resolved = _resolve_trip_reference(self.driver_access, normalized)
        self.assertEqual(resolved.pk, trip_x.pk)

        incompatible = copy.deepcopy(original)
        incompatible.pop('local_trip_id')
        incompatible['trip_id'] = trip_y.pk
        incompatible['payload']['trip_id'] = trip_y.pk
        incompatible['sequence'] = 3
        repeat_result = self.sync_driver(
            [incompatible], device_id=device,
        ).json()['results'][0]
        self.assertEqual(repeat_result['status'], 'conflict')
        self.assertEqual(repeat_result['code'], 'event_id_reused')
        conflict = OfflineFieldEventConflict.objects.get(
            attempted_event_id=original['event_id'],
        )
        self.assertEqual(conflict.existing_event_id, pending.pk)
        pending.refresh_from_db()
        self.assertIsNone(pending.trip_id)

        with CaptureQueriesContext(connection) as captured:
            evidence_x = read_trip_route_evidence(trip_x.pk)
            evidence_y = read_trip_route_evidence(trip_y.pk)
            evidence_z = read_trip_route_evidence(trip_z.pk)
        sources_by_trip = {
            label: [
                source for source in evidence.sources
                if source['source_kind'] == 'offline_field_event_conflict'
                and source['source_pk'] == conflict.pk
            ]
            for label, evidence in [('X', evidence_x), ('Y', evidence_y)]
        }
        x_original_sources = [
            source for source in evidence_x.sources
            if source['source_kind'] == 'offline_field_event'
            and source['source_pk'] == pending.pk
        ]
        dml = [
            query['sql'] for query in captured.captured_queries
            if query['sql'].lstrip().upper().startswith(
                ('INSERT', 'UPDATE', 'DELETE', 'REPLACE')
            )
        ]
        print('P28_I2_R1_PROBE', json.dumps({
            'case': 'local_only_original_collision_to_numeric_other_trip',
            'original_http_result': pending_result,
            'repeat_http_result': repeat_result,
            'trip_ids': {'X': trip_x.pk, 'Y': trip_y.pk, 'Z': trip_z.pk},
            'resolver_local_trip_id': resolved.pk,
            'original_receipt_trip_id': pending.trip_id,
            'original_collected_for_X': bool(x_original_sources),
            'X_binding': evidence_x.local_reference_bindings,
            'projection_X': evidence_x.projection.status,
            'projection_Y': evidence_y.projection.status,
            'projection_Z': evidence_z.projection.status,
            'conflict_id': conflict.pk,
            'conflict_source_count_X': len(sources_by_trip['X']),
            'conflict_source_count_Y': len(sources_by_trip['Y']),
            'Z_unchanged': evidence_z.to_dict() == z_before,
            'reader_DML_count': len(dml),
            'expected': 'X and Y integrity_conflict with both raw envelopes; Z unchanged',
        }, ensure_ascii=False), flush=True)

        self.assertEqual(dml, [])
        self.assertEqual(evidence_z.to_dict(), z_before)
        self.assertTrue(x_original_sources)
        self.assertEqual(
            x_original_sources[0]['evidence_association'],
            'resolved_local_trip_reference',
        )
        for label, evidence in [('X', evidence_x), ('Y', evidence_y)]:
            with self.subTest(trip=label):
                self.assertEqual(evidence.projection.status, 'integrity_conflict')
                self.assertIsNone(evidence.projection.selected_event_id)
                self.assertIsNone(evidence.projection.notification_key)
                self.assertEqual(len(sources_by_trip[label]), 1)
                source = sources_by_trip[label][0]
                self.assertEqual(source['submitted_event'], conflict.submitted_event)
                self.assertEqual(source['fingerprint'], conflict.fingerprint)
                preserved = source['existing_event']
                self.assertEqual(preserved['source_pk'], pending.pk)
                self.assertEqual(preserved['event_id'], pending.event_id)
                self.assertEqual(preserved['fingerprint'], pending.fingerprint)
                self.assertEqual(preserved['payload'], pending.payload)
                self.assertEqual(preserved['context_snapshot'], pending.context_snapshot)
                self.assertEqual(preserved['sequence'], pending.sequence)
                self.assertEqual(preserved['actor_id'], pending.actor_id)
                self.assertEqual(preserved['access_id'], pending.access_id)
                self.assertEqual(preserved['device_id'], pending.device_id)
                self.assertEqual(preserved['local_trip_id'], local_id)
                self.assertEqual(preserved['occurred_at'], pending.occurred_at.isoformat())
                self.assertEqual(preserved['status'], 'retry')
