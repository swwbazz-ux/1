import json
import unittest

from route_core import LifecycleEvidence, RouteEvent, RouteLedger, TripRouteContext, route_event


def event(event_id='D', point='P2', *, trip='X', role='driver', actor='driver-A',
          ancestors=(), action_at='10:00', received_at='receipt-1'):
    return route_event(
        event_id=event_id, trip_id=trip, actor_id=actor, actor_role=role,
        target_point_id=point, observed_ancestor_ids=tuple(ancestors),
        action_at=action_at, received_at=received_at,
        loading_event_id=f'load-{trip}',
    )


def context(trip='X', *, complete=True):
    return TripRouteContext(
        trip_id=trip, loading_event_id=f'load-{trip}', loading_actor_id='loader',
        loading_excavator_id='EX-1', original_point_id='P1',
        history_complete=complete,
    )


def contains_record(value, expected):
    if isinstance(value, dict):
        return value == expected or any(contains_record(item, expected) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_record(item, expected) for item in value)
    return False


def trace(label, value):
    print('P28_I1_R1_TRACE ' + json.dumps(
        {'label': label, 'value': value}, ensure_ascii=False, sort_keys=True,
    ))


class CollisionPreservationTests(unittest.TestCase):
    def test_full_conflicting_envelope_survives_json_snapshot_and_restart(self):
        original = event(point='P2', actor='driver-A', action_at='10:00', received_at='r1')
        conflict = event(point='P9', actor='driver-B', ancestors=('O7',),
                         action_at='10:05', received_at='r2')
        ledger = RouteLedger(); ledger.extend((original, conflict))
        restored = RouteLedger.from_snapshot(json.loads(json.dumps(ledger.snapshot())))
        snapshot = restored.snapshot()
        self.assertTrue(contains_record(snapshot, original.to_record()))
        self.assertTrue(contains_record(snapshot, conflict.to_record()))
        self.assertEqual(snapshot['collisions'][0]['incoming'], conflict.to_record())
        trace('C1_full_conflicting_envelope', snapshot['collisions'][0])

    def test_collision_diagnoses_both_trips_and_does_not_change_third(self):
        y = event(point='PY', trip='Y', received_at='y1')
        x = event(point='PX', trip='X', received_at='x1')
        z = event(event_id='Z1', point='PZ', trip='Z', received_at='z1')
        ledger = RouteLedger(); ledger.extend((y, x, z))
        for trip in ('X', 'Y'):
            with self.subTest(trip=trip):
                projection = ledger.project(context(trip))
                self.assertEqual(projection.status, 'integrity_conflict')
                self.assertIn('id_collision:D', projection.diagnostics)
        unaffected = ledger.project(context('Z'))
        self.assertEqual((unaffected.status, unaffected.selected_event_id, unaffected.selected_point_id),
                         ('resolved', 'Z1', 'PZ'))

    def test_repeated_same_conflicting_envelope_has_one_evidence_and_no_business_effect(self):
        original = event(point='P2', received_at='r1')
        conflict = event(point='P9', actor='driver-B', received_at='r2')
        repeated = RouteEvent(**{**conflict.__dict__, 'received_at': 'r3'})
        ledger = RouteLedger()
        self.assertEqual(ledger.append(original), 'stored')
        self.assertEqual(ledger.append(conflict), 'id_conflict')
        self.assertEqual(ledger.append(repeated), 'id_conflict_duplicate')
        self.assertEqual(len(ledger.snapshot()['collisions']), 1)
        self.assertEqual(ledger.duplicate_count, 1)
        self.assertEqual(ledger.project(context()).status, 'integrity_conflict')
        restored = RouteLedger.from_snapshot(json.loads(json.dumps(ledger.snapshot())))
        self.assertEqual(len(restored.snapshot()['collisions']), 1)
        self.assertEqual(restored.append(repeated), 'id_conflict_duplicate')
        self.assertEqual(len(restored.snapshot()['collisions']), 1)


class IncompleteHistoryTests(unittest.TestCase):
    def test_zero_one_and_multiple_events_are_not_resolved_when_history_is_incomplete(self):
        cases = (
            (),
            (event('D', 'P2'),),
            (event('D', 'P2'), event('O', 'P3', role='excavator_operator', actor='operator-A')),
        )
        for events in cases:
            with self.subTest(count=len(events)):
                ledger = RouteLedger(); ledger.extend(events)
                result = ledger.project(context(complete=False))
                self.assertEqual(result.status, 'causality_incomplete')
                self.assertEqual(result.selection_reason, 'legacy_history_incomplete')
                self.assertIn('legacy_history_incomplete', result.diagnostics)
                self.assertIsNone(result.selected_event_id)
                self.assertIsNone(result.notification_key)

    def test_same_ledger_recalculates_after_history_becomes_complete_without_new_action(self):
        ledger = RouteLedger(); ledger.append(event('D', 'P2'))
        before_snapshot = ledger.snapshot()
        incomplete = ledger.project(context(complete=False))
        complete = ledger.project(context(complete=True))
        self.assertEqual(incomplete.status, 'causality_incomplete')
        self.assertEqual((complete.status, complete.selected_event_id, complete.selected_point_id),
                         ('resolved', 'D', 'P2'))
        self.assertEqual(before_snapshot, ledger.snapshot())
        trace('C2_recalculate_without_new_action', {
            'incomplete': incomplete.business_dict(),
            'complete': complete.business_dict(),
        })

    def test_incomplete_terminal_history_never_emits_notification(self):
        ledger = RouteLedger(); ledger.append(event('D', 'P2'))
        result = ledger.project(
            context(complete=False),
            LifecycleEvidence(state='unloaded', evidence_ids=('unload-proof',)),
        )
        self.assertEqual(result.status, 'causality_incomplete')
        self.assertFalse(result.operational_allowed)
        self.assertIsNone(result.notification_key)


class BusinessConvergenceAndAuditTests(unittest.TestCase):
    def test_receipt_order_changes_audit_but_not_business_projection(self):
        first = event(received_at='10:01')
        later = RouteEvent(**{**first.__dict__, 'received_at': '10:03'})
        ledgers = []
        for delivery in ((first, later), (later, first)):
            ledger = RouteLedger(); ledger.extend(delivery); ledgers.append(ledger)
        business = [ledger.project(context()).business_dict() for ledger in ledgers]
        audits = [ledger.audit_snapshot() for ledger in ledgers]
        self.assertEqual(business[0], business[1])
        self.assertNotEqual(audits[0], audits[1])
        self.assertEqual(
            [[item['reported_received_at'] for item in audit['receipt_observations']]
             for audit in audits],
            [['10:01', '10:03'], ['10:03', '10:01']],
        )
        self.assertNotIn('received_at', business[0]['history'][0])
        trace('C3_business_equal_audit_distinct', {
            'business_equal': business[0] == business[1],
            'audit_equal': audits[0] == audits[1],
            'audits': audits,
        })

    def test_receipt_audit_survives_restart_with_honest_field_name(self):
        original = event(received_at='adapter-value-1')
        retry = RouteEvent(**{**original.__dict__, 'received_at': 'adapter-value-2'})
        ledger = RouteLedger(); ledger.extend((original, retry))
        restored = RouteLedger.from_snapshot(json.loads(json.dumps(ledger.snapshot())))
        audit = restored.audit_snapshot()
        self.assertEqual(audit, ledger.audit_snapshot())
        self.assertEqual(
            [item['reported_received_at'] for item in audit['receipt_observations']],
            ['adapter-value-1', 'adapter-value-2'],
        )
        self.assertTrue(all('first_received_at' not in item
                            for item in audit['receipt_observations']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
