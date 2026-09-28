import itertools
import json
import unittest

from route_core import (
    LifecycleEvidence,
    RouteEvent,
    RouteLedger,
    TripRouteContext,
    route_event,
)


def event(event_id, role, point, *, ancestors=(), trip='X', actor=None,
          action_at='', received_at='', loading_event_id='load-X'):
    return route_event(
        event_id=event_id,
        trip_id=trip,
        actor_id=actor or f'{role}-{event_id}',
        actor_role=role,
        target_point_id=point,
        observed_ancestor_ids=tuple(ancestors),
        action_at=action_at,
        received_at=received_at,
        loading_event_id=loading_event_id,
    )


def context(trip='X', *, complete=True, original='P1', loading_actor='loader-A'):
    return TripRouteContext(
        trip_id=trip,
        loading_event_id=f'load-{trip}',
        loading_actor_id=loading_actor,
        loading_excavator_id='excavator-7',
        original_point_id=original,
        history_complete=complete,
    )


def project(events, *, ctx=None, lifecycle=None):
    ledger = RouteLedger()
    ledger.extend(events)
    return ledger, ledger.project(ctx or context(), lifecycle)


def trace(label, result):
    print('P28_I1_TRACE ' + json.dumps(
        {'label': label, **result.to_dict()}, ensure_ascii=False, sort_keys=True,
    ))


class ApprovedDirectionTests(unittest.TestCase):
    def test_independent_driver_and_operator_choose_driver_in_both_orders(self):
        d = event('D', 'driver', 'P2')
        o = event('O', 'excavator_operator', 'P3')
        for delivery in ((d, o), (o, d)):
            with self.subTest(order=[item.event_id for item in delivery]):
                _, result = project(delivery)
                self.assertEqual((result.status, result.selected_event_id, result.selected_point_id),
                                 ('resolved', 'D', 'P2'))
                self.assertEqual({item['event_id'] for item in result.history}, {'D', 'O'})
        trace('independent_driver_operator', result)

    def test_causal_operator_delivered_before_driver_waits_then_wins(self):
        d = event('D', 'driver', 'P2')
        o = event('O', 'excavator_operator', 'P3', ancestors=('D',))
        ledger = RouteLedger(); ledger.append(o)
        incomplete = ledger.project(context())
        self.assertEqual(incomplete.status, 'causality_incomplete')
        self.assertEqual(incomplete.missing_ancestor_ids, ('D',))
        ledger.append(d)
        final = ledger.project(context())
        self.assertEqual((final.status, final.selected_event_id, final.selected_point_id),
                         ('resolved', 'O', 'P3'))
        trace('causal_operator_after_missing_parent', final)

    def test_operator_then_causal_driver_selects_driver_without_changing_original(self):
        o = event('O', 'excavator_operator', 'P1')
        d = event('D', 'driver', 'P2', ancestors=('O',))
        _, result = project((o, d))
        self.assertEqual((result.selected_event_id, result.selected_point_id), ('D', 'P2'))
        self.assertEqual(result.original_point_id, 'P1')

    def test_clock_skew_does_not_override_causal_operator(self):
        d = event('D', 'driver', 'P2', action_at='2026-09-28T10:00:00+10:00')
        o = event('O', 'excavator_operator', 'P3', ancestors=('D',),
                  action_at='2026-09-28T09:57:00+10:00')
        _, result = project((o, d))
        self.assertEqual(result.selected_event_id, 'O')
        times = {item['event_id']: item['action_at'] for item in result.history}
        self.assertEqual(times, {'D': '2026-09-28T10:00:00+10:00',
                                 'O': '2026-09-28T09:57:00+10:00'})

    def test_operator_sees_operator_but_not_independent_driver_driver_still_wins(self):
        d = event('D', 'driver', 'P2')
        o1 = event('O1', 'excavator_operator', 'P1')
        o2 = event('O2', 'excavator_operator', 'P3', ancestors=('O1',))
        _, result = project((o2, d, o1))
        self.assertEqual(result.causal_maxima, ('D', 'O2'))
        self.assertEqual(result.selected_event_id, 'D')

    def test_all_24_delivery_permutations_converge(self):
        d1 = event('D1', 'driver', 'P2')
        o1 = event('O1', 'excavator_operator', 'P3', ancestors=('D1',))
        o2 = event('O2', 'excavator_operator', 'P4')
        d2 = event('D2', 'driver', 'P5', ancestors=('O2',))
        outcomes = set()
        for delivery in itertools.permutations((d1, o1, o2, d2)):
            _, result = project(delivery)
            outcomes.add(json.dumps(result.to_dict(), sort_keys=True))
        self.assertEqual(len(outcomes), 1)
        result = project((d1, o1, o2, d2))[1]
        self.assertEqual((result.causal_maxima, result.selected_event_id), (('D2', 'O1'), 'D2'))
        trace('permutations_24_converged', result)

    def test_two_causal_chains_with_operator_maxima_are_unresolved(self):
        d1 = event('D1', 'driver', 'P2')
        o1 = event('O1', 'excavator_operator', 'P3', ancestors=('D1',))
        d2 = event('D2', 'driver', 'P4')
        o2 = event('O2', 'excavator_operator', 'P5', ancestors=('D2',))
        _, result = project((o2, d1, o1, d2))
        self.assertEqual((result.status, result.causal_maxima),
                         ('policy_unresolved', ('O1', 'O2')))
        self.assertIsNone(result.selected_event_id)


class IdentityAndIntegrityTests(unittest.TestCase):
    def test_duplicate_lost_receipt_and_restart_keep_one_record_and_same_result(self):
        d = event('D', 'driver', 'P2', received_at='first')
        repeated_delivery = RouteEvent(**{**d.__dict__, 'received_at': 'retry-after-lost-receipt'})
        ledger = RouteLedger()
        self.assertEqual(ledger.append(d), 'stored')
        self.assertEqual(ledger.append(repeated_delivery), 'duplicate')
        before = ledger.project(context()).to_dict()
        restored = RouteLedger.from_snapshot(json.loads(json.dumps(ledger.snapshot())))
        after = restored.project(context()).to_dict()
        self.assertEqual(before, after)
        self.assertEqual(len(after['history']), 1)
        self.assertEqual(restored.duplicate_count, 1)

    def test_same_id_different_payload_preserves_original_and_diagnoses(self):
        original = event('D', 'driver', 'P2')
        conflicting = event('D', 'driver', 'P9')
        ledger = RouteLedger(); ledger.append(original)
        self.assertEqual(ledger.append(conflicting), 'id_conflict')
        result = ledger.project(context())
        self.assertEqual(result.status, 'integrity_conflict')
        self.assertEqual(result.history[0]['target_point_id'], 'P2')
        self.assertIn('id_collision:D', result.diagnostics)

    def test_bad_fingerprint_is_preserved_but_not_selected(self):
        good = event('D', 'driver', 'P2')
        bad = RouteEvent(**{**good.__dict__, 'fingerprint': 'not-the-content-hash'})
        _, result = project((bad,))
        self.assertEqual(result.status, 'integrity_conflict')
        self.assertEqual(result.history[0]['event_id'], 'D')

    def test_missing_ancestor_is_not_assumed_independent(self):
        o = event('O', 'excavator_operator', 'P3', ancestors=('D-missing',))
        _, result = project((o,))
        self.assertEqual(result.status, 'causality_incomplete')
        self.assertEqual(result.missing_ancestor_ids, ('D-missing',))
        self.assertIsNone(result.selected_event_id)

    def test_cycle_has_no_winner(self):
        a = event('A', 'driver', 'P2', ancestors=('B',))
        b = event('B', 'excavator_operator', 'P3', ancestors=('A',))
        _, result = project((a, b))
        self.assertEqual(result.status, 'causality_cycle')
        self.assertIsNone(result.selected_event_id)

    def test_cross_trip_ancestor_is_ignored_without_mutating_neighbor(self):
        y = event('Y1', 'driver', 'PY', trip='Y', loading_event_id='load-Y')
        x = event('X1', 'excavator_operator', 'PX', ancestors=('Y1',))
        ledger = RouteLedger(); ledger.extend((y, x))
        result_x = ledger.project(context('X'))
        result_y = ledger.project(context('Y', original='Y0'))
        self.assertEqual((result_x.selected_event_id, result_x.selected_point_id), ('X1', 'PX'))
        self.assertEqual((result_y.selected_event_id, result_y.selected_point_id), ('Y1', 'PY'))
        self.assertTrue(any(item.startswith('cross_trip_ancestor_ignored') for item in result_x.diagnostics))

    def test_incomplete_legacy_history_does_not_prove_independence(self):
        d = event('D', 'driver', 'P2')
        o = event('O', 'excavator_operator', 'P3')
        _, result = project((d, o), ctx=context(complete=False))
        self.assertEqual(result.status, 'causality_incomplete')
        self.assertIn('legacy_history_incomplete', result.diagnostics)


class OpenPolicyAndAuthorshipTests(unittest.TestCase):
    def test_two_independent_drivers_are_policy_unresolved(self):
        _, result = project((event('D1', 'driver', 'P2'), event('D2', 'driver', 'P3')))
        self.assertEqual(result.status, 'policy_unresolved')

    def test_two_independent_operators_are_policy_unresolved(self):
        _, result = project((event('O1', 'excavator_operator', 'P2'),
                             event('O2', 'excavator_operator', 'P3')))
        self.assertEqual(result.status, 'policy_unresolved')

    def test_replacement_operator_authorship_does_not_replace_loading_author(self):
        o = event('O-next', 'excavator_operator', 'P3', actor='operator-shift-2')
        _, result = project((o,), ctx=context(loading_actor='operator-shift-1'))
        self.assertEqual(result.loading_actor_id, 'operator-shift-1')
        self.assertEqual(result.history[0]['actor_id'], 'operator-shift-2')
        self.assertEqual(result.selected_event_id, 'O-next')

    def test_notification_key_is_selected_event_identity_not_snapshot_version(self):
        d = event('D', 'driver', 'P2')
        _, result = project((d,))
        self.assertIn(':D:', result.notification_key)
        restarted = RouteLedger.from_snapshot(RouteLedgerWith(d).snapshot()).project(context())
        self.assertEqual(result.notification_key, restarted.notification_key)


class LifecycleProtectionTests(unittest.TestCase):
    def test_technical_uncontrolled_does_not_invent_unload(self):
        _, result = project((event('D', 'driver', 'P2'),),
                            lifecycle=LifecycleEvidence(state='technical_uncontrolled', evidence_ids=('timer-1',)))
        self.assertTrue(result.operational_allowed)
        self.assertEqual(result.operational_reason, 'technical_uncontrolled_is_not_unload')
        self.assertEqual(result.selected_event_id, 'D')

    def test_cancel_unload_and_supersede_keep_history_but_suppress_operation_and_notification(self):
        for state in ('cancelled', 'unloaded', 'superseded'):
            with self.subTest(state=state):
                _, result = project(
                    (event('D', 'driver', 'P2'),),
                    lifecycle=LifecycleEvidence(
                        state=state, evidence_ids=(f'{state}-proof',),
                        successor_trip_id='Y' if state == 'superseded' else None,
                        successor_proven_without_fk=state == 'superseded',
                    ),
                )
                self.assertEqual(result.selected_event_id, 'D')
                self.assertFalse(result.operational_allowed)
                self.assertIsNone(result.notification_key)
                self.assertEqual(result.lifecycle_evidence_ids, (f'{state}-proof',))
                if state == 'superseded':
                    self.assertEqual(result.successor_trip_id, 'Y')
                    self.assertTrue(result.successor_proven_without_fk)

    def test_unknown_replacement_history_is_conservative(self):
        _, result = project((event('D', 'driver', 'P2'),),
                            lifecycle=LifecycleEvidence(state='replacement_unknown'))
        self.assertEqual(result.selected_event_id, 'D')
        self.assertFalse(result.operational_allowed)
        self.assertEqual(result.operational_reason, 'replacement_history_incomplete')

    def test_legacy_actual_without_origin_creates_no_driver_event_or_notification(self):
        ctx = TripRouteContext(
            trip_id='X', loading_event_id='load-X', loading_actor_id='loader',
            loading_excavator_id='excavator-7', original_point_id='P1',
            legacy_actual_point_id='P3', legacy_actual_origin='unknown',
        )
        _, result = project((), ctx=ctx)
        self.assertEqual((result.status, result.selected_point_id), ('no_route_event', 'P1'))
        self.assertEqual(result.history, ())
        self.assertIsNone(result.notification_key)


def RouteLedgerWith(*events):
    ledger = RouteLedger(); ledger.extend(events); return ledger


if __name__ == '__main__':
    unittest.main(verbosity=2)
