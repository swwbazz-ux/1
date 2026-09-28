import json
import sys
from pathlib import Path


def main():
    path = Path(__file__).with_name('contract_cases.json')
    payload = json.loads(path.read_text(encoding='utf-8'))
    assert payload['schema'] == 's106-c1-contract-examples-v1'
    assert payload['status'] == 'PROPOSED_NOT_APPROVED'

    ids = [item['id'] for item in payload['cases']]
    assert len(ids) == len(set(ids)), 'case ids must be unique'
    required = {
        'roles', 'event_types', 'codes', 'release_client_trigger',
        'release_server_gate', 'pr106_client_trigger', 'pr106_server_gate',
        'target_client_trigger', 'target_server_gate', 'target_domain_checks',
        'target_time_policy', 'passport_basis', 'tests',
    }
    for item in payload['cases']:
        missing = sorted(required - item.keys())
        assert not missing, f"{item['id']}: missing {missing}"
        assert item['target_time_policy']
        assert item['passport_basis']
        immutable_contract = (
            item['target_client_trigger'] + ' ' + item['target_server_gate']
        ).lower()
        assert (
            'unchanged envelope' in immutable_contract
            or 'same immutable' in immutable_contract
            or item['id'] in {
            'own-clock-ahead', 'driver-legacy-dependency', 'excavator-legacy-dependency',
            'factual-load-worker-truth',
            }
        ), f"{item['id']}: target trigger must preserve the immutable envelope"

    actual = set()
    for item in payload['cases']:
        if not item.get('pr106_worker_truth_branch'):
            continue
        for event_type in item['event_types']:
            for code in item['codes']:
                actual.add((event_type, code))

    expected = {
        ('*driver-supported-event*', 'dependency_rejected'),
        ('*driver-supported-event*', 'dependency_owner_mismatch'),
        ('*driver-supported-event*', 'dependency_order_invalid'),
        ('*excavator-supported-event*', 'dependency_rejected'),
        ('*excavator-supported-event*', 'dependency_owner_mismatch'),
        ('*excavator-supported-event*', 'dependency_order_invalid'),
        *{('driver.trip.unloaded', code) for code in (
            'open_trip_changed', 'late_unload_after_supersede',
            'trip_driver_shift_changed', 'unload_before_load',
        )},
        ('driver.trip.dump_point_changed', 'trip_driver_shift_changed'),
        ('driver.free_bucket.selected', 'driver_shift_closed'),
        ('driver.shift.closed', 'shift_already_closed'),
        ('excavator.shift.closed', 'shift_already_closed'),
        *{
            (event_type, code)
            for event_type in (
                'driver.trip.loaded', 'excavator.trip.loaded',
                'excavator.free_bucket.loaded',
            )
            for code in (
                'active_downtime', 'equipment_downtime_active',
                'excavator_unavailable', 'free_bucket_excavator_unavailable',
                'free_bucket_truck_unavailable', 'open_trip_changed',
            )
        },
    }
    assert actual == expected, {
        'missing': sorted(expected - actual),
        'unexpected': sorted(actual - expected),
    }
    assert len(actual) == 32  # 29 server pairs + role-split dependency rows.
    print(json.dumps({
        'result': 'PASS',
        'schema': payload['schema'],
        'cases': len(payload['cases']),
        'expanded_pr106_role_aware_pairs': len(actual),
        'release_sha': payload['release_sha'],
        'pr106_sha': payload['pr106_sha'],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'FAIL: {exc}', file=sys.stderr)
        raise
