"""Addressed acceptance probes; expect failures on P28-I1 at 64f900b.

Only imports the isolated prototype. No Django, database or network.
"""
import argparse
import json
from pathlib import Path
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--package', type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.package.resolve()))
from route_core import RouteLedger, TripRouteContext, route_event


def event(point, trip='X', receipt='first'):
    return route_event(event_id='D', trip_id=trip, actor_id='driver-A',
                       actor_role='driver', target_point_id=point,
                       loading_event_id='load-' + trip, action_at='10:00',
                       received_at=receipt)


def context(trip='X', complete=True):
    return TripRouteContext(trip, 'load-' + trip, 'loader', 'EX-1', 'P1',
                            history_complete=complete)


failures = 0


def check(label, passed, observed):
    global failures
    failures += int(not passed)
    print(json.dumps({'case': label, 'contract_pass': passed, 'observed': observed},
                     ensure_ascii=False, sort_keys=True))


a, b = event('P2'), event('P9')
ledger = RouteLedger()
ledger.extend((a, b))
snapshot = RouteLedger.from_snapshot(json.loads(json.dumps(ledger.snapshot()))).snapshot()
# Look for the full record, not a particular storage field name.
def contains_record(value, expected):
    if isinstance(value, dict):
        return value == expected or any(contains_record(v, expected) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_record(v, expected) for v in value)
    return False

check('C1_full_incompatible_record_after_restart', contains_record(snapshot, b.to_record()), snapshot)

ledger = RouteLedger()
ledger.extend((event('PY', trip='Y'), event('PX', trip='X')))
px = ledger.project(context('X'))
check('C1b_collision_visible_for_incoming_trip',
      px.status == 'integrity_conflict' and bool(px.diagnostics), px.to_dict())

ledger = RouteLedger()
ledger.append(event('P2'))
p = ledger.project(context(complete=False))
check('C2_one_event_in_explicitly_incomplete_history',
      p.status == 'causality_incomplete' and p.notification_key is None, p.to_dict())

results = []
for inputs in ((event('P2', receipt='10:01'), event('P2', receipt='10:03')),
               (event('P2', receipt='10:03'), event('P2', receipt='10:01'))):
    ledger = RouteLedger()
    ledger.extend(inputs)
    results.append(ledger.project(context()).to_dict())
print(json.dumps({'case': 'C3_business_vs_audit_projection',
                  'business_direction_equal': results[0]['selected_point_id'] == results[1]['selected_point_id'],
                  'full_projection_equal': results[0] == results[1],
                  'stored_receipts': [p['history'][0]['received_at'] for p in results]}, sort_keys=True))

print('ACCEPTANCE_EXPECTATION_FAILURES=' + str(failures))
print('ACCEPTANCE_EXIT_CODE=' + str(int(bool(failures))))
raise SystemExit(int(bool(failures)))
