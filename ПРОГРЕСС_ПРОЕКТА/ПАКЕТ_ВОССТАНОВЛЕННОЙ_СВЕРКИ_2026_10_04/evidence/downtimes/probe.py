"""Execute the exact production _process_downtime AST with explicit ORM doubles.

This is a diagnostic counterexample, NOT a Django/PostgreSQL/device acceptance.
The pre-existing end is a fixture fact: trustworthy boundary for this same
interval. The late command is an ordinary stop, not an authorized correction.
"""
import ast
import hashlib
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
SOURCE = BASE / 'source/core/offline_sync.py'
COMMIT = 'ecb61af55b699a7a2417abba6df4dd490f3464a9'
manifest = json.loads((BASE / 'manifest.json').read_text())
for item in manifest:
    suffix = item['path'].split('/backend/', 1)[1]
    data = (BASE / 'source' / suffix).read_bytes()
    actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    assert actual == item['git_blob_sha'], (suffix, actual)

module = ast.parse(SOURCE.read_text())
function = next(x for x in module.body if isinstance(x, ast.FunctionDef) and x.name == '_process_downtime')
compiled = compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), 'exec')

class Equipment:
    id = pk = 7
    equipment_type = 'truck'
    def __str__(self):
        return 'fixture truck 7'

class Manager:
    def __init__(self, value):
        self.value = value
    def select_for_update(self, **kwargs):
        return self
    def filter(self, **kwargs):
        self.filter_kwargs = kwargs
        return self
    def get(self, **kwargs):
        return self.value
    def first(self):
        return self.value

def stamp(hour):
    return datetime(2026, 10, 3, hour, tzinfo=timezone.utc)

def run_case(initial_end, stop):
    equipment = Equipment()
    equipment.objects = Manager(equipment)
    Equipment.objects = equipment.objects
    event = types.SimpleNamespace(id=41, equipment_id=7, employee_id=3,
                                  started_at=stamp(10), ended_at=stamp(initial_end))
    saves = []
    event.save = lambda **kwargs: saves.append(kwargs)
    dtmodel = type('DowntimeEvent', (), {'objects': Manager(event)})
    sys.modules['downtimes'] = types.ModuleType('downtimes')
    workflow = types.ModuleType('downtimes.driver_workflow')
    workflow.driver_downtime_start_conflict = lambda *args: None
    models = types.ModuleType('downtimes.models')
    models.DowntimeEvent, models.DowntimeReason = dtmodel, object
    sys.modules['downtimes.driver_workflow'] = workflow
    sys.modules['downtimes.models'] = models
    shift = types.SimpleNamespace(id=5, equipment=equipment, equipment_id=7)
    discrepancies = []
    def fail(*args, **kwargs):
        raise AssertionError((args, kwargs))
    env = {'_locked_shift': lambda *args, **kwargs: shift,
           'lock_production_state': lambda: None,
           '_retry': fail, '_conflict': fail,
           '_log_discrepancy': lambda **kwargs: discrepancies.append(kwargs['code']),
           'bump_operational_state': lambda *args, **kwargs: types.SimpleNamespace(version=2)}
    exec(compiled, env)
    access = types.SimpleNamespace(employee_id=3, employee=types.SimpleNamespace(id=3))
    normalized = {'payload': {'downtime_event_id': 41}, 'local_downtime_id': '',
                  'occurred_at': stamp(stop), 'device_id': 'fixture-device'}
    result, relations = env['_process_downtime'](access, normalized, role_code='driver', close=True)
    assert relations['downtime_event'] is event
    return {'initial_end': stamp(initial_end).isoformat(), 'ordinary_stop': stamp(stop).isoformat(),
            'actual_end': event.ended_at.isoformat(), 'writes': len(saves),
            'discrepancy_codes': discrepancies, 'server_ids': result['server_ids']}

late = run_case(12, 18)
earlier = run_case(18, 12)
assert late['actual_end'] == stamp(18).isoformat()
assert earlier['actual_end'] == stamp(12).isoformat()
output = {
    'status': 'REPRODUCED_FUNCTION_COUNTEREXAMPLE',
    'commit': COMMIT,
    'function': '_process_downtime',
    'source_git_blob_sha': next(x['git_blob_sha'] for x in manifest if x['path'].endswith('/core/offline_sync.py')),
    'verified_source_files': len(manifest),
    'scope': 'Exact function AST; ORM, shift lock and state broadcaster are explicit doubles. No full Django or PostgreSQL.',
    'fixture_qualification': 'The existing end is a trustworthy boundary for the same work interval; the incoming stop is ordinary, not an authorized correction. This does not approve universal min(timestamp) reconciliation.',
    'cases': [late, earlier],
    'observation': 'The function overwrites ended_at in either direction and only logs downtime_already_closed.',
    'not_run': ['Full HTTP handler and transaction', 'PostgreSQL locking and concurrent requests', 'Installed Android/iOS/PWA', 'Production requests']
}
(BASE / 'result.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(output, ensure_ascii=False, indent=2))
