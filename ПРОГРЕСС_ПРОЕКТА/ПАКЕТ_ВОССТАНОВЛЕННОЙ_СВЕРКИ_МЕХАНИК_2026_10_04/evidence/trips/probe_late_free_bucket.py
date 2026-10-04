#!/usr/bin/env python3
"""Execute an exact production function with explicit ORM doubles, not Django/PG."""
import ast
import hashlib
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PATH = 'СИСТЕМА_MVP/backend/core/offline_sync.py'
REF = 'ecb61af55b699a7a2417abba6df4dd490f3464a9'
EXPECTED_BLOB = 'eb639a24f6651fcfeb3a58e580486f610c62ab34'
raw = (ROOT / 'source' / PATH).read_bytes()
blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
assert blob == EXPECTED_BLOB, (blob, EXPECTED_BLOB)
tree = ast.parse(raw.decode('utf-8'))
node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_release_open_trips_unknown_to_worker')

class Row:
    def __init__(self, ident, truck, loaded):
        self.id = ident
        self.truck = truck
        self.loaded_at = loaded
        self.created_at = loaded
        self.status = 'LOADED_WAITING_UNLOAD'
        self.operationally_closed_at = None
        self.closure_recorded_by = None
        self.saves = []

    def save(self, *, update_fields):
        self.saves.append(list(update_fields))

class Manager:
    def __init__(self):
        self.rows = []

    def select_for_update(self, **kwargs):
        return self

    def filter(self, *, truck, status__in):
        return [row for row in self.rows if row.truck == truck and row.status in status__in]

manager = Manager()
effects = []
logs = []
models = types.ModuleType('trips.models')
models.Trip = types.SimpleNamespace(objects=manager)
models.TripStatus = types.SimpleNamespace(UNCONTROLLED='UNCONTROLLED')
models.OPEN_TRIP_STATUSES = ('LOADED_WAITING_UNLOAD',)
bucket = types.ModuleType('trips.free_bucket')
bucket.close_free_bucket_acceptance_for_trip = lambda trip, closed_at: effects.append({'trip_id': trip.id, 'closed_at': closed_at.isoformat()})
sys.modules['trips'] = types.ModuleType('trips')
sys.modules['trips.models'] = models
sys.modules['trips.free_bucket'] = bucket
namespace = {'_log_discrepancy': lambda **kwargs: logs.append(kwargs['code'])}
exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / 'source' / PATH), 'exec'), namespace)
release = namespace[node.name]
actor = types.SimpleNamespace(employee='driver_A')
at = lambda minute: datetime(2026, 10, 3, 1, minute, tzinfo=timezone.utc)

def snapshot():
    return [{'trip_id': row.id, 'status': row.status,
             'loaded_at': row.loaded_at.isoformat(),
             'operationally_closed_at': row.operationally_closed_at.isoformat() if row.operationally_closed_at else None,
             'saves': len(row.saves)} for row in manager.rows]

def delivery(order):
    manager.rows.clear()
    effects.clear()
    logs.clear()
    for event in order:
        if event == 'X_free_bucket_at_10':
            release(actor, 'truck_A', occurred_at=at(10), process='Выбор свободного ковша')
        else:
            manager.rows.append(Row(20, 'truck_A', at(20)))
    return {'delivery': order, 'rows': snapshot(), 'reservation_close_calls': list(effects), 'log_codes': list(logs)}

forward = delivery(['X_free_bucket_at_10', 'Y_load_at_20'])
reverse = delivery(['Y_load_at_20', 'X_free_bucket_at_10'])
assert forward['rows'][0]['status'] == 'LOADED_WAITING_UNLOAD'
assert reverse['rows'][0]['status'] == 'UNCONTROLLED'
assert reverse['rows'][0]['operationally_closed_at'] == at(20).isoformat()
assert reverse['reservation_close_calls'][0]['trip_id'] == 20

# A stable already-closed row is not mutated again by this helper; this is
# not proof of the enclosing receipt's idempotency or transaction behavior.
prior_saves = manager.rows[0].saves.copy()
release(actor, 'truck_A', occurred_at=at(10), process='Выбор свободного ковша')
assert manager.rows[0].saves == prior_saves
assert len(effects) == 1

manager.rows = [Row(30, 'truck_B', at(20))]
release(actor, 'truck_A', occurred_at=at(10), process='Выбор свободного ковша')
assert manager.rows[0].status == 'LOADED_WAITING_UNLOAD'

selection = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_process_driver_free_bucket_selected')
release_lines = [n.lineno for n in ast.walk(selection) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == node.name]
guard_lines = [n.lineno for n in ast.walk(selection) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == '_free_bucket_trip_changed_between']
assert min(release_lines) < min(guard_lines)
result = {
    'probe_status': 'COUNTEREXAMPLE_REPRODUCED',
    'product_status': 'KNOWN_GAP',
    'scope': 'Exact AST function; ORM and close-reservation function doubled; new trip insertion is a fixture, not real load handler.',
    'not_run': ['Django HTTP endpoint', 'PostgreSQL transactions and concurrency', 'physical phones', 'field shift', 'production mutation'],
    'source': {'ref': REF, 'path': PATH, 'git_blob_sha': blob, 'sha256': hashlib.sha256(raw).hexdigest(), 'function_lines': [node.lineno, node.end_lineno]},
    'cases': [
        {'id': 'TRIP-PROBE-01', 'assertion': 'Changing delivery order closes later Y in the reverse order', 'status': 'COUNTEREXAMPLE_REPRODUCED', 'forward': forward, 'reverse': reverse},
        {'id': 'TRIP-PROBE-02', 'assertion': 'Already-closed row does not repeat the helper side effect', 'status': 'PASS_ISOLATED_FUNCTION'},
        {'id': 'TRIP-PROBE-03', 'assertion': 'Other truck is not touched', 'status': 'PASS_ISOLATED_FUNCTION'}
    ],
    'static_call_site': {'release_call_lines': release_lines, 'stale_guard_call_lines': guard_lines, 'qualification': 'Guard follows release and excludes the returned trip IDs; full request outcome not executed.'},
    'old_raw': 'Previous 6480-case package unavailable after workspace cleanup; this run does not recreate or validate it.'
}
target = ROOT / 'probe_late_free_bucket.result.json'
target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'probe_status': result['probe_status'], 'cases': len(result['cases']), 'source_blob': blob, 'result': target.name}, ensure_ascii=False))
